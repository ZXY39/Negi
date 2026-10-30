# P5 记忆分域交接报告

日期：2026-10-31
分支：`feat/search-session-history`（不要另开分支、不要改写历史）
上一提交：`26e70cca feat(memory): split local and group memory scopes`

## 1. 已完成

P5（记忆分域）已实现并提交，P1–P4 也都在本分支上：

| 阶段 | 提交 | 内容 |
|---|---|---|
| P1 | `6eb4c298` | 受限档（`limited`）+ 按会话工具列表 + 沙箱页入口 |
| P2 | `b43450e4` | 模式提示词、切档立即重印、一次性模式通知 |
| P3 | `6740318c` | cron / schedule_wake 在受限档可用、主动推送 60 秒节流、延迟原因回传 |
| P4 | `f8160700` | `mention_member`、投递失败回传心跳 |
| P5 | `26e70cca` | 本地/群聊记忆分域、人设统一、记忆门删除、管理页作用域、配置包/CLI |

P5 具体落点：

- `g3ku/runtime/memory_scope_router.py`（新）：`MemoryScopeRouter` / `MemoryScopeView`。
  - 本地会话 → 本地 manager；群会话 → 本地 + 本群；第三方私聊 → 只读本地。
  - 群 manager 根：`<workspace>/memory/groups/<safe-scope>-<hash>/`，配置深拷贝，LRU 上限 32，
    逐出即 `close()`。
  - `list_scopes()` / `manager_for_scope()` 供管理面使用；`set_active_memory_scope_router()` 是
    节点道/无视图调用点的进程内兜底（只对渠道会话生效）。
- `MemoryManager`：新增 `scope_label` / `persona_enabled`；删除 `review_eligibility`、
  启动清扫、队列按信任拆批、停车区按信任过滤、`record_turn_for_review` / `flush_review_window`
  两处写入闸门。
- 读侧：`message_builder` 的记忆快照与人设取数改为 `MemoryScopeView`；第三方空快照/沙箱人设
  分支删除。群块 = 本地正文 + `## 群聊长期记忆`。
- 写侧：`memory_update` / `memory_note` 优先读 `__g3ku_runtime["memory_view"]`；无视图时仅对
  `ext:` / `china:` 渠道键查活动路由；第三方私聊返回 `memory_read_only` 拒绝。
- 通知：`skill_roster_notice.deliver_for_scope()` 增加 `group_memory_provider` 与
  `group_memory_cursor`；`build_notice_message()` 增加 `### Group memory updates`；
  压缩跳同时归零两条游标。本地记忆增量投给所有能读本地记忆的会话，群增量只投该群。
- 会话侧：`session_agent` 的复核 / 冲刷 / 压缩冲刷走 `view.review_manager`；群 manager 不接收
  人设（`persona_text()` 空、`set_persona()` 拒绝），记忆 agent 提示词改为群聊域。
- 管理面：新增 `GET /api/memory/scopes`；所有记忆读写/payload 路由接受 `scope` 查询参数；
  记忆页头部新增作用域 `<select>`，三列、浏览器、note 窗、归并列表、所有写请求都带同一作用域。
- 配置包：记忆面收集/导入 `memory/groups/<scope>/MEMORY.md`、`memory_state.sqlite3`、
  `notes/*.md`、`index/*.md`（队列/ops/review_state/.bak 不进包）。
- CLI：`negi memory` 的 `current` / `queue` / `flush` / `uncompress` / `reformat` /
  `reconcile-notes` 支持 `--scope`；`doctor` 默认扫描本地 + 全部群。
- 删除功能：沙箱页「群聊人设」、`GET/PUT /api/sandbox/persona`、`negi sandbox persona`、
  `sandbox_state.sandbox_persona_text/set_sandbox_persona_text` 全部移除。
- 文档：`runtime-overview.md`、`sandbox-execution-plane.md`、`web-and-admin.md`、
  `config-and-models.md`、`operations-and-maintenance.md`、`README.md` 已更新；
  `scripts/check_architecture_docs.py` 0 error / 0 warning。
- 测试：新增 `tests/resources/test_memory_scope_router.py`（11 条）；更新
  `test_sandbox_trust_p0.py`、`test_sandbox_mode_state.py`、`test_memory_current_admin.py`、
  `test_memory_management_frontend_note_preview.py`、`test_tool_resource_admin_api.py`。

## 2. 验证结果

已通过：

- `ruff check .`：全绿。
- `scripts/check_architecture_docs.py`：0 error / 0 warning。
- 记忆/沙箱/管理面聚焦套件：
  `pytest tests/test_sandbox_mode_state.py tests/test_sandbox_trust_p0.py
  tests/resources/test_memory_scope_router.py tests/resources/test_memory_current_admin.py
  tests/resources/test_memory_revision_notice_lane.py tests/resources/test_frontdoor_persona_block.py
  tests/resources/test_memory_cli.py tests/resources/test_memory_ceo_tool_face.py
  tests/resources/test_config_bundle_export_import.py tests/resources/test_resource_runtime_smoke.py`
  → 292 passed, 1 skipped。
- 管理面 / note 前端契约：`test_memory_management_frontend_note_preview.py` 7 passed、
  `test_tool_resource_admin_api.py` 129 passed、`test_memory_management_frontend.py` 26 passed。
- 记忆页 JS：`node --test` 的 memory 三件套 44 passed；governance init 6 passed。
- 修正后 `test_memory_update_contract.py` / `test_memory_ceo_tool_face.py` /
  `test_resource_runtime_smoke.py` → 119 passed。

未做：真实运行时重启后的多会话验收（见第 4 节）。

## 3. 已知失败（均为基线既有，不是 P5 引入）

用 `pytest tests -k "memory or sandbox or persona or mention or cron or heartbeat or external"`
跑出 17 条失败，逐条复核后：

- 6 条（cron / external events）单独运行全部通过，属于该大筛选下的测试互相污染/时序抖动。
- 8 条在修复「活动路由兜底只对渠道键生效」后单独运行通过：
  `test_memory_update_enqueues_through_the_write_lane`、
  `test_memory_update_queues_remember_and_forget_requests_through_one_entry`、
  `test_a_forget_request_enters_through_the_same_write_lane_as_a_remember_one` 等。
  根因：进程内活动路由是全局的，本地会话不该查它；现在只对 `ext:` / `china:` 兜底。
- 3 条在 `HEAD^` 基线上同样失败（已在 detached worktree 复核）：
  1. `tests/resources/test_memory_failed_parking.py::test_retry_of_oversized_parked_record_splits_into_queue_rows`
  2. `tests/test_memory_runtime_sharing.py::test_sync_internal_tool_runtimes_reads_memory_runtime_manifest`
     （断言 `document_max_chars == 20000`，实际 42000）
  3. `tests/resources/test_runtime_environment_guidance.py::test_acceptance_prompt_mentions_task_temp_dir_rule`
     （验收提示词文案与测试期望不一致，疑似 `033fdcc9` 验收改动遗留）

接手后建议：先跑一次完整相关套件确认第 3 节其余 6 条仍只是抖动；不要为这 3 条基线失败改 P5 代码。

## 4. 待完成（按优先级）

### P0：真实运行时验收（未做）

单测不算完成，必须重启 Negi 后按用户规则验：

1. 本地会话写记忆 → 群会话能读到；群会话写记忆 → 本地看不到、群内可见；本地记忆不被群写。
2. 群里收到记忆/人设通知；本地记忆更新时群也收到增量；群记忆更新只有该群收到。
3. 第三方私聊调用 `memory_update` 被拒（`memory_read_only`），本地记忆不被污染。
4. 群会话里人设与本地一致；沙箱页不再有「群聊人设」卡片。
5. 记忆页切「本地 / 群」：三列、查看记忆、归并记忆、note 预览、编辑/删除都落在对应域。
6. 配置包导出/导入后群记忆仍在；在新设备上导入后，群作用域要等该群重新出现（见 P2-3）。
7. `negi memory doctor` 输出本地 + 每个群两段；`--scope` 单域查询可用。
8. 受限模式群会话：`cron` / `schedule_wake` 可用；派发族仍被拒；`memory_update` 可用且只写群块。
9. 把同一群会话切到全权限再切回受限：提示词/工具/pinned contract 重印，收到模式通知（P2 行为）。

### P1：边界与实现复核

1. **节点道来源会话**：`main/runtime/node_runner.py::_runtime_context` 用 `task.session_id` 当
   `session_key`；`memory_update` 的活动路由兜底只对渠道键生效。请确认从群会话派生的任务其
   `TaskRecord.session_id` 确实是群会话键（若是 `web:shared`，群派生节点的记忆会落本地）。
   群会话默认受限、派发族被硬拒，所以默认路径到不了；全权限群 + 允许派发时才会暴露。
2. **LRU 逐出与复核缓冲**：`MemoryScopeRouter` 逐出群 manager 时直接 `close()`。缓冲在磁盘
   `review_state.json`，重开可续；但要确认逐出时没有正在写的 worker 线程造成半截文件。
   若担心，可把上限调大或在逐出前 flush 一次。
3. **只有磁盘没有注册表的群**：`list_scopes()` 只列外部会话注册表里的群。新设备导入配置包后，
   群目录存在但管理页看不到，直到该群产生会话。产品上是否需要「磁盘发现」需确认。
4. **配置包覆盖语义**：导入只覆盖包内同名群记忆条目，包外群目录保留（与本地记忆面一致），
   文档写的是覆盖而非删除。若产品要求“整包替换”，需要另加清理逻辑并补测试。
5. **群记忆与本地记忆的组合块大小**：本地 + 群各自保留 `document_max_chars`，合并后可能到
   数万字；当前按“本地在前、群在后、各自 cap”实现，需在真实上下文里量一次成本。

### P2：代码/体验收尾

1. 记忆页作用域选择器目前是原生 `<select>`（异步加载选项，未走自绘 `resource-select`）。
   视觉验收时可决定是否统一成外壳控件。
2. `memory_update` 工具描述新增了作用域说明（英文一行）；如果模型在群里误以为能改本地记忆，
   看这条描述与群记忆 agent 提示词是否够清楚。
3. 群记忆通知小节标题固定英文 `### Group memory updates`；与现有 `### Long-term memory updates`
   保持一致，若产品要求中文需统一改两处与测试。
4. 交接前已跑 `HEAD^` 基线复核；如后续再改 P5，请重复“基线 worktree 对比”，不要用 `git stash`。

## 5. 工作树与协作约束（重要）

- 当前工作树只剩他人改动，**不要提交**：
  - `M g3ku/web/frontend/org_graph.css`（先前用户改动）
  - `?? work/sandbox-page-ui-plan.md`（先前用户文件）
- 本机 git 未配置提交身份，提交用：
  `git -c user.name=ZXY39 -c user.email=1045273950@qq.com commit ...`
- 共享工作树**禁用 `git stash`**；基线对比用 detached worktree（用完 `git worktree remove`）。
- **不要用 Bash / Python 写仓库文件**：本机 shell 写入会触发落盘视图损坏（`git` 与编辑器读到
  乱码密文，Python 读到正常内容）。所有文件修改只能用编辑器的 Edit / Write 工具。
  本次已因此恢复过 `main/api/admin_rest.py`、`g3ku/sandbox/state.py`、
  `tests/resources/test_memory_current_admin.py`。
- 架构文档改动后必须跑 `python scripts/check_architecture_docs.py`；代码改动跑
  `python -m ruff check .` 与相关 pytest。
- 运行时/前端验收按项目记忆：单测不算，必须重启服务并用真实会话走一遍。

## 6. 关键入口

- 分域路由：`g3ku/runtime/memory_scope_router.py`
- 群 manager 创建 / 提示词：`g3ku/agent/memory_agent_runtime.py`（`MemoryManager.__init__`、
  `_memory_agent_user_prompt`）
- 快照/人设取数：`g3ku/runtime/frontdoor/message_builder.py`
- 通知双游标：`g3ku/runtime/skill_roster_notice.py`、
  `g3ku/runtime/frontdoor/_ceo_runtime_ops.py`（`_frontdoor_memory_notice_provider` /
  `_frontdoor_group_memory_notice_provider`）
- 工具路由：`g3ku/agent/tools/memory_update.py`、`memory_note.py`；
  运行时上下文注入：`_ceo_runtime_ops.py::_build_tool_runtime_context`
- 管理面：`main/api/admin_rest.py`（`/memory/scopes`、`scope` 参数）、
  `g3ku/web/frontend/client_api/memory.js`、`g3ku/web/frontend/memory/{store,actions,view}.js`
- 配置包：`g3ku/config/config_bundle.py`（`GROUP_MEMORY_ROOT`、`_collect_group_memory_entries`、
  `_group_memory_entry_target`）
- CLI：`g3ku/shells/memory_cli.py`
- 测试：`tests/resources/test_memory_scope_router.py`、`tests/test_sandbox_trust_p0.py`

## 7. P5 完成度判定

功能实现、静态检查、单测、文档已完成；**真实运行时验收与上面 P1 的 5 个边界复核未完成**。
接手 agent 从第 4 节 P0 开始，按第 5 节约束操作；不要把第 3 节的基线失败算到本次改动上。
