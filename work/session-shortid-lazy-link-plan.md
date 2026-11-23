# 会话短 ID 取代 s{n} 代号 + 关联边懒创建

## 0. 靶子与现状（已核到行）

用户报的两件事：

1. 模型写记忆时把对端会话记成 `s1` 这类代号，而不是能指认具体会话的 id。
2. 关联会话只能操作员在弹窗里手动勾；希望「只要向某个 id 的会话发过消息，
   它就自动出现在关联会话里」。

现状（2026-11-23 核）：

- `main/storage/sqlite_store.py:669` `session_aliases(seq AUTOINCREMENT,
  session_key UNIQUE, created_at)`；`get_or_create_session_alias` 产出 `s{seq}`
  （`:3005`），`resolve_session_alias` 只认 `s\d+`（`:3032`）。别名是**全局**的，
  但它是序号不是标识：读者（另一条会话、记忆页、压缩后的自己）无法从 `s1`
  反推出是哪条会话，名册里没列它时更是无从对照。
- 模型面出现别名的四处：头部名册 `render_reachable_session_roster`
  （`g3ku/runtime/frontdoor/tool_contract.py:562`）、来信签名与回投指引
  （`g3ku/heartbeat/prompt_lane.py:749-789`）、`legal_targets`
  （`main/service/runtime_service.py:7304`）、`list_sessions`
  （`tools/list_sessions/main/tool.py:48`）。delta 通知走
  `g3ku/runtime/skill_roster_notice.py:274-310` + `render_session_link_lines:1125`。
- ACL = 关联边：`deliver_session_message` 无边直接 `session_not_linked`
  （`main/service/runtime_service.py:7143`）。可关联性判据只在 REST 层
  `_link_target_problem`（`g3ku/runtime/api/ceo_sessions.py:834`）。
- 头部渲染器版本 `pinned-contract-v7`（`tool_contract.py:41`），名册**不进重印
  判据键**：不 bump 版本的话，存量会话头部会一直显示旧 `s{n}` 直到下次压缩。
- `ensure_session_alias`（`runtime_service.py:7364`）全仓无调用方，是死代码。

## 1. 设计决定（带默认值，用户可否决）

| # | 决定 | 默认 | 理由 / 代价 |
|---|---|---|---|
| D1 | 短 id 的来源 | 规范 key 的尾段前 8 字符（`web:ceo-3f9a1b2c4d5e` → `3f9a1b2c`；`ext:qq:<hash>` → hash 前 8），小写、只留 `[a-z0-9]` | 它**是真实会话 id 的一段**，记忆里的这串能前缀匹配回具体会话；比全 key 短 60% |
| D2 | 唯一性与稳定性 | 落库 `session_aliases.short_id`，部分唯一索引（`WHERE short_id <> ''`）；撞号先延长到 12 / 全尾段，再 `-2`/`-3`；**一旦分配永不改、永不复用** | 派生式读时算会随新会话加入而变号，记忆里的旧号就废了；落库分配一次即冻结 |
| D3 | 旧 `s{n}` | 只保留**读**解析（存量信箱行 `source_alias`、存量钉住头部、存量记忆都带它），不再新分配、不再显示 | 同 AGENTS.md 对 legacy 标记的口径：数据滚完才删读元组 |
| D4 | 头部何时换成新 id | bump `pinned-contract-v8`，存量会话首回合头部重印一次 | 不 bump 就要等压缩边界，用户报的「记忆里还是 s1」会持续很久。代价＝每会话一次性非缓存 input 冲高（v7 上线时同形） |
| D5 | 懒创建的授权闸 | **发送方 trust=owner 才自动建边**；第三方会话（群/陌生人私聊）保持 `session_not_linked`，要操作员先勾 | 关联边是 ACL。无闸则任何第三方会话都能凭 id 闯进操作员会话的名册并唤醒它。owner 之间（本地会话 + 操作员自己的渠道私聊）全自动 |
| D6 | 目标合法性 | 复用 REST 那套判据，抽成单一作者模块：自身/`internal:`/`china:`/隐藏 ext 不可关联，不存在 → `session_missing` | 懒创建不能绕过弹窗现有的拒绝面 |
| D7 | 模型怎么拿到未关联会话的 id | `list_sessions` 对 owner 调用方返回**全部可关联候选**（带 `linked` 标记，上限 50 + `truncated`）；第三方调用方仍只返回已关联 | 否则懒创建无入口：模型只能靠用户贴 id。头部名册仍只列已关联 ≤8 行，钉住块不膨胀 |
| D8 | 自动建边要不要记 operator action | 不记（不是操作员干的），可达性变化由既有 delta 通知交代 | 避免把模型行为伪装成操作员授权 |
| D9 | 前端 | 关联弹窗每行标题右侧加一枚短 id 标签（mono、muted）；目录 API 的 item 带 `short_id` | 操作员能把记忆里的短 id 对回具体会话。不做新页面 |
| D10 | 命名 | 模型面字段统一改叫 `short_id`（roster item、payload `source_short_id`、`list_sessions` 输出）；两处 legacy 读保留旧键名 | 概念变了就别留旧名混用；legacy 读按 D3 保留 |

被否决的备选：直接显示全规范 key（用户当初正是嫌长 key 易抄错才要的别名）；
纯随机短号（与真实 id 无字面关系，记忆里的号只能靠表反查，对不上用户诉求）。

## 2. 阶段与落点

### P1 存储层：短 id 分配与解析
`main/storage/sqlite_store.py`
- 建表段 `session_aliases` 加 `short_id TEXT NOT NULL DEFAULT ''`；
  `_ensure_column` 补列（存量库），**在建索引之前**（同 `:736` 那条事故注释）。
- 新增部分唯一索引 `idx_session_aliases_short_id`。
- 新增 `_short_id_candidate(key)`、`get_or_create_session_short_id(key, created_at)`
  （撞号延长/加后缀，循环有界）、`get_session_short_id(key)`（只读）、
  `resolve_session_short_id(text)`。
- 保留 `get_session_alias` / `resolve_session_alias` 作 legacy 读；
  删 `get_or_create_session_alias`（新分配一律走 short_id）。

### P2 服务层：寻址、名册、懒创建
`main/service/runtime_service.py`
- `session_short_id(key)`（get-or-create，幂等）、`resolve_session_address(token)`
  （短 id → legacy `s{n}` → 规范 key）。删死代码 `ensure_session_alias` 与
  `get_session_alias`/`resolve_session_alias` 包装（调用方改新名）。
- `reachable_session_roster` 返回 `{'session_key', 'short_id'}`。
- `replace_session_links` 新边两端分配 short_id。
- `_frontdoor_legal_targets` 追加 short_id。
- `_deliver_frontdoor_target` 裸 id 解析顺序不变（node → task → 会话地址）；
  `s\d+` 形状落空仍 `session_missing`，其余未知裸 id 仍 `target_not_in_task`。
- `deliver_session_message`：无边时按 D5/D6 判，过则 `add_session_link` +
  两端 short_id + 回执 `auto_linked: True`，再过原有 chain/hop/dedupe 逻辑；
  payload 盖 `source_short_id`。
- `purge_session_link_data` 不变（边与信箱清、标识行留）。

### P3 共享判据单一作者
新增 `g3ku/runtime/session_link_policy.py`
- `session_link_problem(target, *, self_key) -> str`：从 REST 搬来，
  惰性 import SessionManager / ext registry（同 `tool_contract._session_title_for_roster`
  的取数形态，worker 进程可读盘）。
- `may_auto_link(sender) -> bool`：`session_trust(sender) == TRUST_OWNER`。
- `g3ku/runtime/api/ceo_sessions.py::_link_target_problem` 改为委托，HTTP 码不变。

### P4 模型面四处 + delta
- `tool_contract.py`：`PINNED_CONTRACT_RENDERER_VERSION = 'pinned-contract-v8'`；
  roster 渲染/解析改 `short_id`（`reachable_session_aliases_from_text` →
  `reachable_session_ids_from_text`）；`frontdoor_reachable_session_roster` 同步。
- `skill_roster_notice.py`：provider 条目与游标 `known` 改 short_id；
  **legacy 归一**：`known` / covered 里的 `s{n}` 先解析成 short_id 再 diff，
  避免升级首跳播一场假的「全移除 + 全新增」；hint 文案改短 id 并说明
  「向一个会话 id 发消息即自动建立关联」。
- `prompt_lane.py::_session_message_lines`：读 `source_short_id`，
  回退 legacy `source_alias`（若是 `s{n}` 则映射成 short_id）；措辞改「id is signed」。
- `session_message_scanner.py`：富化同上。
- `_ceo_runtime_ops.py:4613` provider 与 `covered_session_aliases` 参数名同步。

### P5 工具面
- `tools/list_sessions/`：输出 `short_id | title | trust | linked`；owner 调用方
  取全部候选（D7），第三方只取已关联；上限 50 + `truncated`；空态文案改。
  `resource.yaml` description / governance 同步（版本相关变更）。
- `tools/send_message/resource.yaml`：description 与 `to` 参数说明改短 id，
  写明「未关联的会话 id 会在投递时自动建立关联（owner 会话）」。
- `tools/send_message/toolskills/SKILL.md`：补「会话目标」一节（现在整篇只讲节点）。

### P6 操作员面
- `ceo_sessions.py`：目录 item 带 `short_id`；`_record_link_operator_action` 用短 id。
- `g3ku/web/frontend/org_graph_app.js::ceoSessionLinkRow`：加短 id 标签。
- 样式落在已加载的样式文件（按 web-and-admin 的主题契约）。

### P7 测试
- 改：`tests/test_session_link_store.py`、`test_session_link_delivery.py`、
  `test_session_link_roster_delta.py`、`test_session_link_api.py`、
  `tests/resources/test_owned_process_roster_block.py`（v8 钉子）、
  `tests/resources/org_graph_app.session_links.test.js`（行markup）。
- 新增：短 id 派生/撞号延长/永不复用/legacy `s{n}` 只读解析；
  懒创建四档（owner 自动建边且对称、第三方拒、不可关联目标拒、不存在拒）；
  自动建边后 `legal_targets` 与名册含新 id；`list_sessions` 按 trust 分档；
  升级首跳 delta 不因 legacy 游标播假变化。

### P8 文档
`docs/architecture/session-messaging.md`（owning doc，就地重写「关系模型与短别名」
「寻址与投递」「头部名册」三节 + 排障入口）、`web-and-admin.md`
「Session Link Dialog And API Contract」（短 id 标签、目录字段）、
`README.md` Topic Ownership 行与 Debugging Entry Points 里涉及别名/手动关联的条目。
走 `skills/negi-architecture-maintenance/SKILL.md` 判定。

## 3. 验收

### 3.1 离线
- `python -m ruff check .`
- `python -m pytest tests/test_session_link_*.py tests/resources/test_owned_process_roster_block.py -q`
- `python -m pytest tests/resources/test_resource_runtime_smoke.py -q`
- 前端：`node --test tests/resources/org_graph_app.session_links.test.js`（按该文件现有跑法）
- `python scripts/check_architecture_docs.py`

### 3.2 真机（本机 web 18790，数据根 D:\NEGI）
优雅重启（exit-check → exit `pause_running_work` → `work/start-web-logged.cmd`），
新建两条本地会话 A/B，**不预先关联**，脚本（httpx `trust_env=False`）验：

1. A 的头部名册/`list_sessions` 里出现的是 8 位短 id，全仓模型面文本无 `s\d+` 新分配。
2. A 用 B 的短 id 发 `send_message` → 回执 `delivered` + `auto_linked`，
   `session_links` 双向出现边，B 的信箱 pending。
3. B 被扫描器入队并醒来，来信签名是 A 的短 id；B 用 `reply_to` 回投成功、hop=1。
4. 第三方档会话（注册表 trust=third_party_*）向未关联 owner 会话发 → 仍
   `session_not_linked`，且**没有**建边。
5. 不可关联目标（`internal:persona_photo`、自身）→ `session_not_linkable`，不建边。
6. 操作员弹窗勾/取消关联照旧生效；取消后 A 再发 → 又自动建边（懒创建语义，如实记录）。
7. 存量会话（升级前已钉住 v7 头部）首回合头部重印成 v8 且名册是短 id；
   delta 不播假「全移除」。
8. 真模型一轮：让 A「记住 B 会话负责什么」，读记忆条目里落的是短 id 而非 `s1`；
   再用该短 id 直接发消息可达。
9. 转录/请求体取证：名册行字节形如 `- 3f9a1b2c | 标题`；缓存命中在重印那一跳
   掉一次、下一跳回血（按 context-and-cache 的口径读台账，不靠模型自述）。

产物：`work/session-shortid-lazy-link-result.json` + 脚本
`work/live_session_shortid_lazy_link.py`。

## 4. 风险与残留

- **R1 一次性缓存成本**：v8 让每条存量会话首回合头部重印（非缓存 input 冲高一次）。
  这是 D4 的自觉代价，换「立刻不再显示 s1」。
- **R2 ACL 语义变松（owner 域内）**：owner 会话之间不再有「操作员先批准」这道门。
  第三方域的门保留（D5）。解除关联后对方再发消息会重新建边——这是懒创建的
  定义本身，不是缺陷；要永久挡住只能删会话或收回工具面。
- **R3 短 id 撞号**：8 字符空间在千级会话下概率极低，且落库唯一索引 + 延长兜底；
  撞号只会让后分配者变长，不会改已分配的号。
- **R4 裸 id 遮蔽**：短 id 与节点/任务 id 同形时按 node → task → session 顺序解析，
  会话被遮蔽。显式 `session:<id>` 永远直达。
- **R5 legacy 数据**：存量信箱 `source_alias`、存量记忆里的 `s1` 不会改写。
  读侧保留解析，模型仍能用旧号发信；记忆里的旧号要等模型自己重写才消失。
- **R6 worker 侧判据取数**：`session_link_policy` 在 worker 进程读转录/注册表，
  读不到时 fail-closed 返回 `session_missing`，不建边。

## 5. 交接段（上下文耗尽时按此续跑）

- 分支：main（本地提交，不推送）。提交信息英文，作者临时
  `-c user.name=ZXY39 -c user.email=1045273950@qq.com`。
- 每完成一个 P 阶段即 commit；只 `git add` 本计划涉及的文件（工作树里有大量
  他人/历史未跟踪 `work/*`，不要裹进来）。
- 解释器：`C:/Users/周心宇/AppData/Local/Programs/Python/Python313/python.exe`
  不在 PATH；仓库跑测试用 `.venv/Scripts/python.exe`。
- 真机重启与路径事实见记忆 `project-local-web-runtime`（转录在安装根
  `D:\Project\Negi\sessions\`，侧车在数据根 `D:\NEGI\.g3ku\`）。
- 已核实的落点行号见 §0；动手前若发现行号漂移，以符号名为准重新定位。
- 完成判据：§3 全绿 + 文档已更新并在最终小结里点名。

## 6. 验收结果（2026-11-23）

- 离线：`tests/`（除 resources）1333 passed / 6 skipped；session-link 四件套 +
  heartbeat 车道 + 预算测试 + 前端 JS 9/9 全绿；ruff 全仓干净；
  `scripts/check_architecture_docs.py` 0 error。
- 真机：`work/session-shortid-lazy-link-result.json` **36 checks / 0 failed**
  （phase1 25 + phase2 10 + v8 重印触发 1）。含：无预关联按短 id 投递即双向建边 +
  `auto_linked` 回执、信箱短 id 签名、B 心跳回投 hop=1 同 chain、delta 不播假移除、
  压缩后头部名册 `- 2064a458 | 标题`、**记忆条目真机落盘带短 id**、解除关联后再发
  重新建边、重启后短 id/边稳定、删除清边留标识行、数据根清单与仓库逐字节一致、
  21 份存量 v7 侧车在 v8 下任何 tier/epoch 都复现不出旧键（下一跳必重印）。
- 第三方信任闸离线钉住（`test_third_party_sender_still_needs_operator_link` 等），
  真机不伪造第三方渠道身份。
- 过程中修掉一个自造缺陷：新描述超 160 字符预算 → 真机 repair_required，模型自改
  数据根清单；已压短描述、恢复数据根、reload 复核 available=True（提交 ae2733d0）。
- 验收脚本两处自身 fault 已修并留 observation：payload_json 是整条记录（签名/链在
  `.payload` 子对象）；驱模型回合前须等目标空闲，否则并进在飞心跳回合 silent 收尾。
- 先存失败（非本次改动，单列）：`tests/resources` 全量 5723 例中 72 红
  （5636 passed / 15 skipped）。在 b06de1f7 detached worktree 上按模块整批复跑，
  失败集合与本次**逐条相同**（72=72，comm diff 为空）→ 零回归。分布：
  offline_kit_consumer_lane 11、llm_frontend_* 27、setup_exe_lane 7、
  task_tree_frontend_sync 5（DOM 桩缺 `replaceChildren`）、web_frontend_asset_gate 4、
  sticker_admin 3、node_sent_message_frontdoor_lane 3（`_Store` 桩缺
  `list_chatbox_messages_by_task`，最后改动 60a1cc96）、filesystem_stat_tool 2、
  其余单例含 description_budget 的 `process_kill` 227 字符（ca9df26b 引入）。
  对照日志：work/resources-full-rerun.log、work/baseline-batch2.log、
  work/baseline-tasktree.log；基线 worktree 已移除。
- 前端全量：`node --test tests/resources/org_graph_app.*.test.js` 799/799 绿。