# 节点通知尾块窗口化与历史检索落地计划

日期：2026-11-06
状态：**已实现（P1–P4）；P5 真机验收已通过**
取证基线：`main` @ `75eebcd6`
实施提交：`095df2cb`（尾窗 + 检索面 + 索引 + 文档）、`6b69ed9e`（latest_at 修正）
工作区提示：`main/runtime/node_runner.py`、`main/runtime/task_actor_service.py`、`main/storage/sqlite_store.py` 已有未提交改动，非本计划产出；动 P2 前先确认归属，不覆盖、不代提交。

## 一句话结论

把节点的 `[NOTICE_TAIL]` 从"已并入过的通知永久重投"改成"未消费 + 最近 N 条已消费留在尾块，其余只留台账"，退场点放在**压缩边界**；节点侧把 `view_message_history` 扩成"信箱 + 通知台账"的统一检索入口（`content_search` 按 ref 查关键词）。不新增存储、不迁移数据库、不改 frontdoor 的 `[RUNTIME_NOTICE]` 行为。

## 现状与问题（已核）

1. **尾块里到底是什么**：`build_append_notice_tail_messages` 只读 `append_notice_context.notice_records`（`main/runtime/append_notice_context.py:453`），而这些记录在 `_record_consumed_notice_context` 归档时已经"并入过上下文"（`main/runtime/node_runner.py:1426`、`main/runtime/append_notice_context.py:187`）。未消费的 pending root 记录走另一条路——作为普通 user 消息注入（`main/runtime/node_runner.py:1289`、`1368`），不在 `[NOTICE_TAIL]` 里。
2. **真正无限增长的是"已并入过的记录被反复重投"**：`select_uncovered_append_notices` 只排除"已被代持/已作废"，不排除"已消费"（`main/runtime/append_notice_context.py:323`）。
3. **代持段同样只增不减**：`roll_append_notice_context_for_compression_stage` 每次摘要漏带追加一段（`append_notice_context.py:268`），`build_append_notice_tail_messages` 无条件渲染所有段（`append_notice_context.py:465`），`supersede_append_notice_records` 只标记记录、不清旧段（`append_notice_context.py:228`）。
4. **没有条数上限**：单条正文有 16k 内联额度（`main/runtime/node_runner.py:160`），但条数无上限；尾块是压缩后的不可压缩下限，接近窗口时压缩救不回来。
5. **工具缺口**：节点 `view_message_history` 只读信箱行 `list_task_node_notifications`（`main/runtime/node_runner.py:1101`），不含 `append_notice_context.notice_records` 与 `pending_append_notice_records`；`content_search` 已支持按 ref 搜关键词（`tools/content_search/resource.yaml:21`）。
6. **前门工具不能直接复用**：`search_session_history` 是 CEO-only、owner-session-only，数据源是 `Session.messages`（`tools/search_session_history/resource.yaml:99`、`tools/search_session_history/main/tool.py:78`），节点没有这条会话转录。

## 目标行为（验收口径）

1. 尾块 live 集合 = `notice_records` 中"未并入过"的记录 + 最近 N 条已并入记录（默认 N=5，按 `received_at` / `consumed_at` 排序，稳定去重）；`superseded_at` 非空的一律排除。
2. 已并入且落在 N 之外的记录：不再进入 `[NOTICE_TAIL]`、不再进入压缩"必须保留段"、不再新建代持段；记录本身保留在 `notice_records` 与信箱表，可检索。
3. 退场点 = **压缩边界**：压缩跳本来整段重写正文，此时重建尾块是免费的；两次压缩之间尾块保持 append-only，不按跳动态删（否则会从删除点起重付缓存，且 `frame.messages` 里的旧块仍需删除点，实际做不到"立即退场"）。
4. 代持段：只渲染与 live 集合有交集的段；只含已消费记录的段在压缩边界退场。
5. `view_message_history` 返回的 artifact 覆盖：信箱行 + `notice_records` + `pending_append_notice_records`；按 message 文本去重；带 status（delivered/consumed/merged/superseded/pending）、source、时间、kind、正文。
6. 模型发现面：工具描述与 `main/prompts/node_execution.md` 明确"历史通知可能已从上下文退场，用 view_message_history + content_search 查"；P3 可选加一条极小尾索引行。
7. 验收节点 / spawn review 行为不变：它们从 metadata 拉 `notice_records` / `pending_append_notice_records`（`main/runtime/node_runner.py:6964`、`4615`），不依赖执行节点提示词里的副本。
8. 不新增 config 字段、不迁移数据库、不改变 frontdoor 的 `[RUNTIME_NOTICE]` 行为。

## 待确认决策（默认值）

| 决策 | 默认 | 备选 | 影响 |
|---|---|---|---|
| N（保留最近已消费条数） | 5 | 0 / 3 / 10 | N=0 即"已消费全部退场"，语义最激进；5 保留一个最近工作集 |
| 退场点 | 压缩边界 | 每次装配 | 每次装配会改前缀缓存，且旧块仍在 `frame.messages` 里，实际做不到"立即退场" |
| 发现面 | 工具描述 + 提示词 + P3 索引行 | 仅工具描述 | 仅描述时模型可能不知道有东西可搜 |
| 旧代持段 | 压缩边界清理 | 只做渲染过滤 | 清理会改 metadata；只过滤不改数据，但元数据继续增长 |

## 阶段与落点

### P0 前置（不动代码）

- 确认 `main/runtime/node_runner.py` 的在途改动归属；本计划会改 `_view_message_history` 与工具注册处，必须避免覆盖。
- 复跑现有基线：
  `python -m pytest tests/resources/test_append_notice_compression_preservation.py tests/resources/test_append_notice_tail_dedupe.py tests/resources/test_mid_insert_registry.py -q`

### P1 尾窗选择与压缩边界退场（核心，先上）

1. `main/runtime/append_notice_context.py`
   - 新常量 `NODE_NOTICE_CONSUMED_TAIL_WINDOW = 5`。
   - 新函数 `select_append_notice_tail_records(context, *, consumed_window=...)`：
     - live = 未 merged（兼容旧记录）+ 最近 N 条 merged；排除 `superseded_at` 非空与空正文。
     - 排序键 `(received_at or consumed_at or "", notification_id)`，稳定。
   - `select_uncovered_append_notices` 改为委托该函数（保留函数名与签名，避免调用面扩散）；它就是尾块与"必须保留段"共用的唯一判据（`append_notice_context.py:326-330`）。
   - `build_append_notice_tail_messages`：
     - `compressed_notice_window` 段只在 `notice_ids ∩ live_ids` 非空时渲染。
     - `raw_notice_window` 用 live 集合。
     - 解析失败的旧块按"未知 = 保守保留"处理。
   - 新函数 `prune_append_notice_context_segments(context, *, live_notice_ids)`：删除只含非 live id 的段；保留 `notice_records` 全量。压缩边界调用。
   - `score_append_notice_preservation` / `render_append_notice_must_preserve_block` 不变，输入变成 live 集合。
2. `main/runtime/react_loop.py`
   - `_node_compression_must_preserve_records`（`6588`）：改用新选择器；压缩指令只钉 live 集合。
   - `_note_node_compression_notice_preservation`（`6617`）：只对 live 集合判 miss / roll；`roll...` 只收 live ids。
   - `_split_request_messages_for_token_compaction`（`6413`；深埋 raw window 在 `6461`）：
     - 只把 live 的 deep `raw_notice_window` 捞出；只含已消费记录的深埋窗口留在 compressible history 里，交给摘要/丢弃。
     - 解析不出 id 的旧块保守保留。
   - `_rewrite_request_messages_for_token_compaction`（`6538`）：
     - 接收 `live_notice_ids`（或重建后的尾块列表），压缩后尾块从 live 集合重建，而不是原样搬运旧 carry 块。
     - 保持 `system_prefix → bootstrap_user → append_notice_tail → 压缩块 → ...` 顺序不变（`6577-6585`）。
   - 压缩调用点（`7957`）：
     - 计算一次 live 集合，贯穿 must-preserve / rewrite / roll，避免同一跳 metadata 漂移。
     - 压缩后把 `retained/dropped` 写进 `token_preflight_diagnostics.notice_preservation`（新增 `archived_consumed_count`、`dropped_consumed_count`），并落一条 INFO 日志。
   - 不在 `_prepare_messages_with_parts`（`10035`）里做按跳删除；只保留现有 dedupe。两次压缩之间的尾块仍可能增长，这是本方案的接受成本。
3. 迁移
   - 旧 `compression_segments` 不重写；只按 live 交集渲染，压缩边界清理。
   - 旧记录缺 `merged_at`：按 `consumed_at` 兜底；两者都缺按"未消费"保留（保守）。

### P2 节点历史检索面（核心，随 P1 或紧随）

1. `main/runtime/node_runner.py::_view_message_history`（`1101`）
   - 合并三个来源：`list_task_node_notifications`、`normalize_append_notice_context(metadata).notice_records`、`normalize_pending_append_notice_records(metadata)`。
   - 统一行形状：`message_id / received_at / source_node_id / from / kind / status / content / ref / source`。
   - 状态优先级：superseded > merged > consumed > delivered > pending；held 仍隐藏 content。
   - 按 message 文本去重（与验收块同口径，`6964-6987`）；保留首次出现顺序，最新在末尾。
   - artifact 仍走 `externalize_node_artifact`，返回值仍是 `{ok,count,ref,summary,latest_at}`。
2. `main/runtime/internal_tools.py::ViewMessageHistoryTool`（`491-511`）
   - 描述改为"包括已从上下文退场的已消费 / 已作废通知，返回可 content_open / content_search 的 artifact ref"。
3. `main/prompts/node_execution.md:86`
   - 把"查看收到的消息记录用 view_message_history"扩成"历史通知可能已从上下文退场；需要核对旧要求时用 view_message_history，再用 content_search(ref=...) 查关键词"。
4. 不新增工具（P3 可选）。

### P3 可选增强（默认做，可后置）

- 新增 `[NOTICE_INDEX]` 尾块：`archived_consumed_count`、`oldest_received_at`、检索指引；只在计数 > 0 时出现。
- 位置 `POSITION_TAIL`、singleton、每跳重建、`compaction=drop`、`strip_at=(LANE_NODE, STAGE_PROMPT_TAIL)`；在 `main/runtime/mid_insert.py` 注册新 family，补 `tests/resources/test_mid_insert_registry.py`。
- 替代方案：不做新 family，把这行并入现有 turn-only note；或只留工具描述。三选一，默认新 family 最清晰。

### P4 文档（必须，随代码同批）

- `docs/architecture/context-and-cache-troubleshooting.md`
  - §4.6 表格 `append_notice` 行：寿命从"压缩免疫"改为"未消费 + 最近 N 条已消费进尾块；其余在压缩边界退场，台账保留可检索"。
  - §4.4（约 `306`）："`[NOTICE_TAIL]` 未消费窗口原样保留"补上"仅 live 集合"。
  - §5.6（约 `469-471`）：append-notice 拾取 / 裁剪口径。
  - §6.5 测试矩阵：新增"压缩后尾块不含已消费-only 块；验收块仍能读台账"。
  - §5.6 诊断表（约 `437`）：`notice_preservation.open_count` 语义改为 live 集合，补两个新字段。
- `docs/architecture/runtime-overview.md`（约 `718`）：节点追加通知因果保留段改写为新窗口 / 退场规则。
- `docs/architecture/main-task-runtime.md`
  - §1（约 `66`）英文句 "Consumed notices persist ... so they survive token compression" 改为 "persist in the ledger; only the live window is re-injected after compression; the rest are searchable"。
  - §1（约 `30`）`view_message_history` 描述补"含台账归档通知"。
  - §1.1（约 `74`）验收块：明确它读 metadata，不受执行节点尾块裁剪影响。
- 按 `AGENTS.md`，完成前过 `skills/negi-architecture-maintenance/SKILL.md`；若该 skill 认为还要动 `README.md`，一并更新。

### P5 测试与验收

单测（新增 / 修改）：

- `tests/resources/test_append_notice_compression_preservation.py`：窗口选择、N 边界、superseded、legacy 无 `merged_at`、段交集过滤。
- `tests/resources/test_append_notice_tail_dedupe.py`：去重不变；索引块 singleton（若 P3）。
- `tests/resources/test_react_runtime_regressions.py`：现有 `4513` / `4563` 两条保持；新增"已消费深埋 raw window 不捞出"。
- `tests/test_node_message_history_tool.py`：三来源合并、文本去重、held 隐藏、superseded 可见、pending root 可见。
- `tests/test_node_acceptance_scheduler.py`：尾块裁剪后验收块仍包含已消费通知。
- `tests/resources/test_mid_insert_registry.py`：新 family × stage 矩阵（若 P3）。

命令：

- `python -m ruff check .`
- `python -m pytest tests/resources/test_resource_runtime_smoke.py -q`
- `python -m pytest tests/resources/test_append_notice_compression_preservation.py tests/resources/test_append_notice_tail_dedupe.py tests/resources/test_mid_insert_registry.py tests/resources/test_react_runtime_regressions.py tests/test_node_message_history_tool.py tests/test_node_acceptance_scheduler.py -q`
- 本机 `python` 不在 PATH 时用完整路径。

真机验收（必须，重启 + 真任务）：

1. 造一个节点，投 > N+2 条通知并跑过消费；检查 `view_message_history` artifact 条数 = 全部（去重后）。
2. 触发一次 `token_compression`；检查压缩后 actual-request：
   - 无"只含已消费通知"的 `[NOTICE_TAIL]` 块；
   - 最近 N 条与未消费仍在；
   - `token_preflight_diagnostics.notice_preservation.archived_consumed_count` 与 `dropped_consumed_count` 合理；
   - `prepared_message_chars` 台阶式下降。
3. 同一节点验收回合：`追加任务要求` 仍列出被验节点的已消费 user / frontdoor 通知。
4. 重启 / resume 第一跳：`request_seed_source` 仍为 `scaffold_seed*`，搜索面完整，旧通知可 `content_search` 命中。
5. frontdoor 不受影响：`[RUNTIME_NOTICE]` 压缩 drop + 状态快照行为不变。

观测 / 验收读数：

- `token_preflight_diagnostics.notice_preservation`：`open_count`（live 数）、`hit_count`、`miss_count`、`rolled_stage_id`，新增 `archived_consumed_count` / `dropped_consumed_count`。
- 日志：`node notice tail pruned: task=... node=... retained=... dropped=...`（压缩跳）。
- 对比压缩前后 artifact 的消息条数与字符数。

## 风险与回滚

1. **已消费 ≠ 已解决**：N 之外的已消费通知退场后，仍生效的约束只能靠模型记得或检索。缓解：N=5 的最近工作集 + 工具描述 / 提示词 + P3 索引行。若实测漏读，优先加大 N 或加显式 resolved 标记，不回退整个机制。
2. **发现性**：模型不会搜不知道存在的东西。缓解：P2 工具描述 + 提示词；P3 索引行是兜底。
3. **验收块自身增长**：它从 metadata 读全部已消费通知（`6964`），不在本计划裁剪范围；后续可复用同一 live 窗口策略，单列计划。
4. **旧段迁移**：历史段可能同时含 live 与非 live id，按"有交集就保留"处理；只含非 live 的段在压缩边界清掉。
5. **缓存**：退场只在压缩边界，压缩跳本来就是重写边界；两次压缩之间尾块 append-only，不新增断点。若把退场改成按跳，必须预期缓存命中下降。
6. **并发**：live 集合在压缩跳只算一次并贯穿三个调用点；metadata 更新走 `update_node_metadata` 单点，避免与在途通知消费竞态。
7. **回滚**：P1 的过滤是一个选择器 + 一个 rewrite 分支，回滚即恢复"所有 uncovered 记录进尾块"；台账从未删除，不会丢数据。

## 提交边界与交接

- 每阶段单独提交；只 `git add` 本计划产生的文件，不碰上面提到的在途改动。
- P1 提交信息建议：`feat(node): bound append-notice tail to live window at compression`
- P2：`feat(node): include notice ledger in view_message_history`
- P3：`feat(node): add notice index tail line`
- P4：`docs: update append-notice tail contract`
- 交接段（每批完成后补）：实际改动文件、测试命令与结果、未决问题（N 的实测值、索引行是否保留、验收块是否要同样窗口化）。

## 未决问题（请拍板）

1. N 取 5 还是 0？我建议 5；N=0 也只需改常量。
2. P3 索引行做不做？我建议做，成本是一行 + 一个 family。
3. `view_message_history` 是否包含 superseded 行？我建议包含并标状态，检索 / 取证都需要。
4. 验收块是否在后续计划里同样窗口化？本次只登记风险，不改。

## 实施记录（2026-11-06）

已按默认值落地：

- N = `NODE_NOTICE_CONSUMED_TAIL_WINDOW = 5`（`main/runtime/append_notice_context.py`）。
- 尾窗、代持段过滤/裁剪、深埋 raw window 按 live 过滤、压缩边界重建尾块：`append_notice_context.py` + `react_loop.py`。
- `view_message_history` 合并信箱行 / `notice_records` / pending root 通知，按正文去重并带 status/source：`node_runner.py` + `internal_tools.py` + `node_execution.md`。
- `[NOTICE_INDEX]` 尾索引：`append_notice_context.py` + `mid_insert.py` + `react_loop.py`。
- 文档：`context-and-cache-troubleshooting.md`、`runtime-overview.md`、`main-task-runtime.md`、`tool-and-skill-system.md`。
- 测试：新增 `tests/resources/test_node_notice_tail_window.py`；扩展 `tests/test_node_message_history_tool.py`。已通过：
  - `test_node_notice_tail_window.py`、`test_node_message_history_tool.py`、`test_append_notice_compression_preservation.py`、`test_append_notice_tail_dedupe.py`、`test_mid_insert_registry.py`、`test_react_runtime_regressions.py`、`test_node_acceptance_scheduler.py`（222 passed / 7 skipped）
  - `test_node_send_compaction_diagnostics.py`、`test_node_compaction_baseline_adoption.py`、`test_resource_runtime_smoke.py`（282 passed / 7 skipped）
  - `test_task_web_worker_runtime.py`（222 passed）
  - `ruff check` 与 `scripts/check_architecture_docs.py` 均通过。

P5 真机验收（2026-11-06，已通过）：

- 重启本机 web（托管 worker）加载新代码；用临时模型 `acceptance-notice-tail-window` + 本地假 provider，把 `context_window_tokens` 临时调到 30000（触发线 22800）。
- 真实节点任务 `task:5f8586d345ea` / `node:8d604fb5b7ca`，投递 20 条通知；任务最终 `success`。
- `token_compression` 落地：`prepared_message_chars=66011`、`final_request_tokens=28816 > 22800`、`compaction_mode=llm`。
- `notice_preservation`：`live_notice_count=5`、`archived_consumed_count=3`、`miss_count=5`、`rolled_stage_id=token-compact:node:8d604fb5b7ca:21`。
- 压缩后 `[NOTICE_TAIL]` 块为 `compressed_notice_window`，`notice_count=5`，正文只含 live 的通知 4–8；旧通知 1–3、9–20 不在尾块里。
- 节点调用 `view_message_history`，工具回执 `count=20`；台账 `notice_records` 共 20 条。
- 清理：临时模型 / 会话 / 测试任务已删除，execution 角色恢复原链；本机 web 保留新代码继续运行。
- 验收脚本：`work/live_notice_tail_window_acceptance.py`；请求日志证据：`work/notice-window-requests.jsonl`。

工作区注意：`main/runtime/node_runner.py` 有一处与本改造无关的未提交改动（`started = request_started_at` → `boundary = anchor`，约在 `_unseen...` 时区比较处），未纳入本次提交。动它前先确认归属。
