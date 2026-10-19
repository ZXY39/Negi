# Skill 名单上下文改造计划（头部全量名单 + 按轮注入）

日期：2026-10-19
状态：**已全部落地**（P1 + P2-1 见 `e617e56`；P2-2 复核与节点道每跳 live 检测见文末「落地记录」）。用户拍板：头部名单带描述；节点道加头部名单、实现对齐会话道；换车道不改 `tools[]`/skill 名单（保缓存命中）——此项放 **P2**，等另一会话的 tool[] 修复落地并同步版本后再做。

## 背景与问题

当前 skill 上下文是三层：治理可见（`list_contract_visible_skill_resources`）→ 本轮语义选中（`message_builder._select_skills`，按 query 排序取 `skill_inventory_top_k`（默认 16））→ 头部钉住（`## Runtime Contract (pinned)` 的 `candidate_skills` 行，整回合不变）。尾块用 `granted_skills` / `unselected_skills` 差集行表达选中集漂移。

四个问题：

1. **top-16 语义筛选**让名单依赖查询语义，模型能不能看到某个 skill 取决于排序器，而不是治理可见性。
2. **撤走/删除 skill 会断一次前缀**：可见集变化 → `exposure_revision` 变 → 头部钉住块重印判据（`pinned_contract_revision_key` 含 `contract_revision`）失效 → 首条 system 改字节。
3. **尾块差集不点名原因**：`unselected_skills` 把"权限撤走 / 已删除 / 本轮没选中"混在一行，且常规路径下差集为空（重印同步了基准），模型侧几乎没有显式信号。
4. skill 与工具机制不同：skill 不需要先水合才能读，`load_skill_context` 按 id 直读正文，候选/水合那套三态表达对它没有意义。

对齐参照：另一会话的 tool[] 改造（工作树未提交）把 `tools[]` 钉住改成**完全冻结**——重印边界（token 压缩、手动压缩、换车道、空闲窗口第一跳）之前一个名字不增删；mid-turn 注册的工具靠尾块 `undeclared_candidates` 点名、水合台账放行执行、load 实时探针准入。skill 的目标行为与它同构。

## 目标行为（验收口径）

1. 头部名单 = **钉住时点的全量可见 skill**，每行 `skill_id | display_name | 描述`；只在 token 压缩 / 手动压缩 / 空闲边界（上下文刷新）重印。两次重印之间，可见集怎么变头部都逐字节不动。
2. 重印点之间新变为可读的 skill → **按轮注入块**（`## New Readable Skills`，每行 id | 名称 | 描述）追加进本轮上下文；台账只增不减；阶段被裁撤块也不丢。
3. 尾块 skill 机制全删：前门 `candidate_skills` 整份行、`granted_skills` / `unselected_skills` 差集行、`repair_required_skills` 行；节点 `candidate_skills` 行（由节点头部名单替代）。
4. 节点道加头部名单，实现与会话道同构（同一渲染函数、同一 epoch 语义）。
5. `load_skill_context` 三态回执：已删除 / 权限不允许 / 待修复；skill 候选闸门拆除。
6. 缓存不变量：非重印点，头部逐字节不变；注入块是尾部追加消息，不顶掉前面任何前缀。

## P1 实现项（可先行，注意与 tool[] 会话的文件冲突）

### 1. 头部名单全量化 + 描述

- `g3ku/runtime/frontdoor/message_builder.py:1491-1499`：`selected_skills = visible_skills` 全量，删 `_select_skills` 调用与 `skill_inventory_top_k` 消费（配置字段保留、标记废弃）；`semantic_catalog_rankings` 不再请求 skill 排名（工具侧 ranking 保留）。`visible_only` 分支随之消失（选中 ≡ 可见）。
- `tool_contract.py::render_pinned_contract_text`：`candidate_skills` 行改为逐行 `skill_id | display_name | description`（描述超长截断到一行）。描述源 = skill 记录（`display_name` / `description`），名单渲染入参从 `skill_ids` 扩为 `skill_items`（id+名称+描述），钉住条目存储同步扩。
- 体积护栏：单 skill 一行、描述截断；重印点之间零变化，体积只在重印点结算。

### 2. roster epoch 与判据键（头部只在该变的时候变）

- 新增会话级 `_frontdoor_skill_roster_epoch`（会话属性 + 按 session_key 的进程表双载体，复刻 `_PINNED_CONTRACT_STORES` 的模式）。
- bump 点：**与 `tools[]` 重印边界同源**——`_frontdoor_bundle_recommit_boundary` 返回 True 的同一处 +1。这样 P2 改边界（去掉换车道）时 skill 自动跟随，不需要第二处逻辑。
- `pinned_contract_revision_key`（`tool_contract.py:65`）：`contract_revision`（exposure_revision）替换为 roster epoch；`exec_policy_signature` / `session_temp_dir` 两项保留。
- 顺带效应：exposure_revision 不再触发头部重印 → 工具收回/删除也不再断头部（与 `tools[]` 快照方案互补，缓存待遇彻底对齐）。

### 3. 尾块 skill 机制删除

- `tool_contract.py::_render_frontdoor_contract_summary`：删 `candidate_skills` 整份行、`granted_skills` / `unselected_skills` 差集、`repair_required_skills` 行；连带删 `pinned_skill_difference`、`_roster_difference_too_wide`、`_render_candidate_skill_section`（前门版）。`pinned_skill_ids_for` / 钉住条目的 `skill_ids` **保留**——注入块的"已被头部覆盖"判据要用它。
- 节点 `main/runtime/node_prompt_contract.py`：删 `_render_candidate_skill_section` 与 `repair_required_skills` 行（节点 skill 信号改由节点头部名单 + 注入块承载）。

### 4. 按轮检测 + 注入块（替代差集两行）

- 检测点：前门每跳装配（`_frontdoor_prompt_contract` / `_build_tool_runtime_context`）与节点每跳（react_loop 装配处），调 `list_contract_visible_skill_resources`（几十条记录的策略评估，毫秒级）。
- 注入集合 = `live_visible − pinned_roster − injected_ledger`；非空则生成一块：

  ```
  ## New Readable Skills (round <n>)
  - `<skill_id>` | <display_name> | <description>
  ```

  以 dynamic appendix 消息追加（尾部，不顶前缀）。
- 台账 `injected_skill_ids`：会话级、**只增不减**，双载体（会话属性 + session_key 进程表）。块**每轮从台账整体重渲染**——阶段裁撤（`context_evicted`）不可能丢块，因为它不依赖历史里的任何一份拷贝；这比走 `kept_skill_contexts` 继承搬移（`canonical_context.py:230-243`）便宜且无漏。
- 体积收敛：台账条目一旦进入当前头部名单（重印发生后），从注入块里摘除——信息仍在（头部有），块不无限长大。"只增不减"约束的是 skill 本身，块只做去重。

### 5. `load_skill_context` 三态回执 + 闸门拆除

- `main/service/runtime_service.py::load_skill_context_v2`（:7708）：先查注册表记录（`resource_registry.get_skill`）：
  - 记录不存在 → `{"ok": false, "error": "skill_deleted", "message": "Skill 已删除：<id>"}`；
  - 存在但不在 `list_contract_visible_skill_resources` → `{"ok": false, "error": "skill_not_permitted", "message": "权限不允许：角色 <role> 无权读取 <id>"}`；
  - 待修复 → 现有 repair 载荷不变。
- `g3ku/agent/tools/main_runtime.py`：删 skill 的 `_candidate_gate_error` 调用（`candidate_skill_ids` 闸门）。可见性由 v2 强制；闸门拿回合初 stale 快照只会误拒 mid-turn 授权的 skill。**注意：该文件有另一会话未提交的工具侧同域改动，落地时 rebase 对齐。**

### 6. 节点道头部名单（对齐会话）

- 节点 system 首条消息钉同一形态的名单块：同一渲染函数、同一 epoch 语义；节点 epoch 源 = `_node_bundle_recommit_reason`（`token_compression` / `idle_refresh`，`react_loop.py:6712`）返回非空时 +1。
- 节点按轮检测同样接 `list_contract_visible_skill_resources`（actor_role 取节点角色），台账按 task/node 作用域或复用 session_key 台账——实现时按节点装配处实际拿到的 key 定，原则与会话道一致：每轮重渲染、只增不减。

### 7. 测试

- 头部：全量名单渲染（含描述行）、epoch 不变时可见集变化头部逐字节不动、epoch 跳变才重印。
- 注入：mid-turn 新可见 skill 下一跳出块（含名称+描述）、台账去重不重复注入、头部重印覆盖后块摘条、阶段裁撤后块仍在。
- load 三态回执；闸门拆除后 mid-turn 授权 skill 可 load。
- 节点：头部名单钉住/重印、`candidate_skills` 行删除后无残留。
- 回归：`test_frontdoor_pinned_contract.py`（granted/unselected 断言全删）、`test_ceo_create_agent_runner.py`、`test_node_prompt_contract.py`、`test_contract_presence_capability.py`、smoke。

### 8. 文档

- `tool-and-skill-system.md`：「Provider Tool Surface」增 skill 名单小节（全量+冻结+按轮注入）；「Pinned Static Declarations」重印边界改为 epoch；candidate skills 三态段（:182 附近）重写；「Tool Admin RBAC」不动。
- `context-and-cache-troubleshooting.md` §3.6：skill 撤走/删除的缓存语义改写（头部冻结、注入块、load 三态）。
- `runtime-overview.md` / `web-and-admin.md` 涉及 candidate_skills 的句子同步。

## P2（依赖：另一会话 tool[] 修复落地 + 同步版本后）

1. **换车道出重印边界**：`_frontdoor_bundle_recommit_boundary` 去掉 lane 分支（`PROVIDER_BUNDLE_LANE_ATTR` / `PENDING_PROVIDER_BUNDLE_RECOMMIT_ATTR` 的换车道触发），`tools[]` 与 skill epoch 同步收敛为"压缩 + 手动压缩 + 空闲"。skill 侧因 epoch 与边界同源，自动跟随，无独立改动。
2. **内部轮工具面语义复核**：原"换车道重印"的动机是心跳/定时内部轮不继承外部轮的宽工具面。tools[] 冻结后此收窄由执行准入（dispatch/水合台账）承担，需复核内部轮在冻结宽面下的可见面是否符合预期；skill 面同理（内部轮会看到全量 skill 名单）。
3. **版本同步步骤**：等 tool[] 会话提交并推 main → P1 分支 rebase → 解决 `main_runtime.py`（工具探针 vs skill 闸门拆除）与 `tool-and-skill-system.md` 的同文件冲突 → 两侧测试合并跑。

## 风险与回滚

- 头部体积：全量名单 + 描述随 skill 数线性涨（当前 54 个 ≈ 2k token）。护栏：描述截断一行；若 skill 数上百，再议分页/摘要（不在本期）。
- 钉住条目存储形状变化（ids → items）：`_PINNED_CONTRACT_STORES` 是进程内存，重启即清，无迁移问题；但同进程内新旧形状共存窗口要注意渲染函数兼容（按 dict/item 双读）。
- 台账只增不减 + 头部摘条的组合，在"skill 删除后又同名重建"时会由 live 可见集自然纠正（重建即重新可见 → 进头部或注入块）。
- 回滚：P1 全部改动集中在名单装配与回执文案，不动 `tools[]` 与压缩机制；revert 单个 commit 即可回到现状。

## 决策记录

| 决策 | 结论 |
|---|---|
| 头部名单是否带描述 | 带，`id | 名称 | 描述` 一行一个 |
| 节点道 | 加头部名单，实现对齐会话；尾块 `candidate_skills` 删除 |
| `repair_required_skills` 尾块行 | 删（load 回执覆盖） |
| 换车道 | 不改名单；实现放 P2，等 tool[] 修复 + 版本同步 |
| top-16 语义筛选 | 删除；`skill_inventory_top_k` 字段废弃保留 |

## 落地记录（2026-10-19，收尾）

| 批次 | 内容 | 落点 |
|---|---|---|
| P1 + P2-1 | 头部全量名单、roster epoch、按轮注入、节点头部名单、load 三态、尾块 skill 机制下线、换车道出重印边界 | `e617e56` |
| 收尾-1 | 节点道 roster 检测从**回合粒度**改为**每跳**：`MainRuntimeService._node_skill_roster_supplier` → `NodeRunner._runtime_context['live_skill_roster_supplier']` → `ReActLoop._node_skill_roster_items`（supplier 抛错/缺失回落回合初 fresh payload → 帧）。同一跳头部名单与注入块共用一份取样；头部字节仍由 roster epoch 冻结。 | `react_loop.py` / `node_runner.py` / `runtime_service.py` |
| 收尾-2 | P2-2 内部轮复核：换车道不重印（断言化）、声明面冻结而派发面按治理可见集收窄、内部轮无活动阶段仍保留阶段工具、skill 读取按治理实时可见集裁决（三态回执）。复核结论与断言见 `tests/resources/test_frontdoor_internal_turn_contract.py`。 | 同上 + `_ceo_runtime_ops.py` 注释 |
| 收尾-3 | 注入块载荷补回 `display_name`：节点侧原先走 `_normalized_candidate_skill_items` 会把名字抹成 id，与「id \| 名称 \| 描述」的点名语义不符。 | `node_prompt_contract.py` |
| 收尾-4 | 文档对齐：`tool-and-skill-system.md` / `context-and-cache-troubleshooting.md` / `runtime-overview.md` / `tool-hydration-and-callable-chain.md` 里"换车道是重印边界"的残留全部改掉（代码早已去掉，文档滞后）。 | `docs/architecture/` |

遗留（非本计划）：`tests/resources/test_ceo_frontdoor_context_retention.py` 两例与 `test_export_node_prompt_rounds.py` 一例在 `e617e56` 上已红（`_FakeTaskStore` 缺 `get_node_pause_flags`、contract suffix 归一化），与本系列无关。
