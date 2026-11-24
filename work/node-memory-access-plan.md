# 节点接入长期记忆（默认只 continuous 根节点）

状态：待批（未动代码）。
靶子来源：用户第一轮要求「长期任务根节点和本地会话一样会同步本地记忆在上下文，同时拥有记忆更新工具，但是没有 5 回合自动入队」；本机实测 `memory/MEMORY.md`、`D:\NEGI\.g3ku\main-runtime\runtime.sqlite3` 的 `task_model_calls`、`.g3ku/main-runtime/governance.sqlite3`。
前置：记忆车道合批/砍收口跳已交付（`c0332bc5` / `213d849a`），本计划依赖其主动道合批与空闲封口。

## 0. 两个待确认的默认值（用户可否决，不改机制）

- **覆盖面**：默认只 `continuous` 根节点（`is_continuous_metadata(node.metadata)` 且无 `parent_node_id`）。子节点与验收节点不变。
- **人设**：默认不注入 `## 我的人设`。人设是 CEO 前台身份，节点是执行体。

范围问题已异步问出，尚未收到答复；本计划按上述默认写，翻档只改 §4 的门判据，不动 §2 的机制。

## 1. 现状：要求里的三条都不成立

- **上下文没有记忆块**。节点请求体是 system prompt + 一个 JSON 载荷（`main/runtime/node_runner.py:3546-3589`），system 由 `node_execution.md` / `acceptance_execution.md` 拼成、continuous 根把完成协议块换成 `shared_continuous_duty.md`（`:3591-3607`）。上下文增强器只加 seed 契约与动态工具契约，并写着明话 `Long-term MEMORY.md snapshot injection is CEO/frontdoor-only`（`main/service/runtime_service.py:15720-15722`）。`main/` 全树零个 `snapshot_text` / `memory_view` 调用方。
- **`memory_update` 节点调不到**。默认 `allowed_roles: ['ceo']`（`main/governance/action_mapper.py:42`），节点常驻内置名单也没有它（`g3ku/runtime/tool_visibility.py:20-26`）。本机治理库 `tools` 表里 `memory_update` 持久化角色就是 `["ceo"]`；而 `memory_note` 持久化是 `["ceo","execution","inspection"]`——**默认表与存量不一致**，因为 `_merge_tools` 用持久化角色覆盖新发现默认值（`main/governance/resource_registry.py:96-103`）。
- **「没有 5 回合自动入队」成立，但原因是整条复核车道没接**：`record_turn_for_review` 唯一生产调用方在 `g3ku/runtime/session_agent.py:4651-4662`，`main/` 零调用。
- 提示词还在反向承诺：`main/prompts/shared_tool_discipline.md:14`「当前链路不提供直接长期记忆搜索」。无测试钉住这句，节点三个模板也都不提记忆。

## 2. 设计决定：骑现有三条机制，不新造车道

**记忆块进节点的 pinned system 头部，与 skill 名单同一份块、同一个重印边界。**

先例就是 `_apply_node_skill_roster_head`（`main/runtime/react_loop.py:3991-4034`）：名单/策略没变时**逐字节幂等**，只在 token 压缩边界重印。两个调用点已经把契约写死——每跳应用（`:676`）、压缩边界重印并 `_bump_skill_roster_epoch`（`:943-950`），注释明写「其它任何变化都不许提前重印头部：缓存命中第一，变化由一次性 `[RUNTIME_NOTICE]` 纠正」。

因此：

- **P0-b 基座**：记忆投影经 `_merge_pinned_contract_into_system_text` 并进 system 首条，两次重印之间字节冻结。
- **P0-c 增量**：中途记忆变化走节点 `[RUNTIME_NOTICE]`。`deliver_for_scope` **已支持** `memory_cursor` / `group_memory_cursor` / `persona_cursor`（`g3ku/runtime/skill_roster_notice.py:173-239`），节点调用点只是没传 `memory_provider`（`main/runtime/react_loop.py:4273-4282`，默认 `None` 在 `skill_roster_notice.py:135-136`）——**这个参数就是扩展点**，不需要新通知车道。
- **重印边界**：节点有内联 token 压缩（`_apply_node_send_token_preflight`，`react_loop.py:8363`，调用点 `:905`），压缩后已有 `_reset_node_runtime_notice_after_compaction`（`:4349-4371`）与 baseline 采纳（`:916-933`）。记忆基座在这里换版本、`memory_cursor` 同时对齐，顺序照会话车道：先取当刻投影重印头部，再对齐游标。

### 为什么不用会话那样的 index-1 assistant 块

会话的记忆块是 index 1 的 `assistant` 消息（`g3ku/runtime/frontdoor/message_builder.py:538-555`）。节点照搬会撞两处：

- scaffold 采纳比对**前两条**记录（`react_loop.py:10848`），插入 index 1 会让升级时在飞节点的 seed 与 stable 头两条不一致 → `fallback_seed_prefix_drift`，丢掉续跑脚手架。
- 节点在 `mid_insert` 里**没有** durable/compaction/projection stage 条目（`main/runtime/mid_insert.py:428-433`），`retrieval_memory_snapshot` 的 `strip_at` 只列前门（`:355-369`）；index-1 块会被压缩当普通历史裁掉。

进 system 头部则复用已经存在的重印/幂等/压缩机制，代价是它属于 cache key 输入（见 §3）。

### 节点 cache key 的构成（决定成本的关键）

`_execution_prompt_cache_key`（`react_loop.py:4494-4500`）→ `build_stable_prompt_cache_key`（`main/runtime/chat_backend.py:611-628`），输入**只有**首条 system 内容签名、首条 user 内容签名、模型身份；`tools` 显式丢弃。节点道**没有** `cache_family_revision` 锚点（那只在前门，`chat_backend.py:721-742`）。所以记忆块放进 system 后，**字节冻结即 family 稳定**；重印只发生在压缩边界，而那里 roster 头部本来就重印、可复用前缀本来就断——**不新增任何 family 轮转事件**。

## 3. 成本模型（本机实测，口径写明）

- 常驻块：`memory/MEMORY.md` **5250 字符 = 3201 tokens**（用项目自带 `estimate_tokens`，不是估算比例）。
- 节点车道：`task_model_calls` **86 次调用**（2026-10-29 → 11-16，集中在 3 天：54 / 28 / 4）。场均**未缓存 input 3368、output 1039、`cache_hit_tokens` 24790**；53/86 次有命中；**缓存占总输入 88%**；`final_request_tokens` 中位 **17970**。
- 因此注入 3201 tokens 的块，相对节点当前请求体约 **+18%** 字节；两次重印之间它走缓存读价，只在首次写入与每次压缩重印时付写价。**不是每跳全价**。
- 撤回一条错误读数：本轮曾按 `cache_read_tokens` 读出「节点车道零命中」，该字段在节点台账里叫 **`cache_hit_tokens`**；零命中说法作废，以上为更正后的数。
- 关掉之后消失的成本：节点请求体回到当前字节，`memory_cursor` 不再对节点投递，节点不再产生主动记忆队列行。常开成本为零（不新增进程/依赖/定时器）。

## 4. 范围门与非目标

**RBAC 是角色粒度，不是节点粒度。** 把 `memory_update` 放开到 `execution` 会让所有执行节点都看得见它。要限到 continuous 根，必须在节点工具判据上再加一道门——先例是 `_execution_control_tool_names(node_kind=..., can_spawn_children=...)`（`main/service/runtime_service.py:11062-11066`）。默认门：`is_continuous_metadata` 且无 `parent_node_id`；不满足则该工具不进可见集（不是进了再拒）。

非目标：

- **不接复核窗口**（硬约束）。节点不调 `record_turn_for_review`，不产生 `ordinary_turn_window` / `stale_buffer` / `token_compression` / `session_boundary` 行。节点入队量只由模型主动调用决定，不随节点轮数线性膨胀。加守卫测试钉住这条。
- 不注入人设块（默认）。
- 不动 `NODE_FIXED_BUILTIN_TOOL_NAMES`（见 P0-e 的取舍）。
- 不做节点侧记忆压缩/归并的新判据——容量梯子、压缩、归并全部沿用记忆车道现有合同。
- 不改会话车道任何行为。

## 5. P0 阶段与落点

### P0-a 作用域解析与快照取数

节点道今天不解析任何 `MemoryScopeView`。`task.metadata['memory_scope']` 在创建时写入（`runtime_service.py:9981-10001`），`node_runner._runtime_context` 把它渲成 `memory_channel` / `memory_chat_id`（`main/runtime/node_runner.py:3706-3731`），但**全仓没有生产消费方**，`_task_memory_scope`（`runtime_service.py:10013`）零调用。

- 路由键是** originating session_key 本身**（`task.session_id`），不是 channel/chat_id：`MemoryScopeRouter` 按 session_key 查注册表判 group/guild（`g3ku/runtime/memory_scope_router.py:120-147,277-283,328-335`）。
- 由 `runtime_service` 解析视图并把投影文本经 side-channel 递给节点道，形状照 `exec` 策略那条（`_node_exec_runtime_policy_payload`，`react_loop.py:4037`）。**不在 `main/` 里直接 import 记忆管理器**。
- 群源会话：视图 = 本地 + 本群，写只进本群 manager。第三方私聊源：只读本地、无可写 manager，`memory_update` 返回可读拒绝（工具已有该分支）。
- 取数失败（读盘瞬时失败）保留上一份冻结值，不把节点钉成空记忆——照会话车道同一条规则。

### P0-b pinned head 注入与冻结

- 落点 `_apply_node_skill_roster_head`（`react_loop.py:3991-4034`）同一份合并路径，记忆块排在 roster 块之后、位置固定。
- 每跳字节幂等；只在压缩边界（`:943-950`）用当刻投影重印。
- 块标题沿用 `## 长期记忆`，正文逐字节原样带过（含归并区），**不二次渲染**——再派生就会在头部造出镜像本身没有的字节。

### P0-c 增量接通

- 节点通知调用点传 `memory_provider`（`react_loop.py:4273-4282`），游标用 `memory_cursor`；群源另接 `group_memory_cursor`。
- 压缩后游标对齐到最新修订（`_reset_node_runtime_notice_after_compaction`，`:4349-4371`），增量不重投。
- 人设游标默认不接（§0）。

### P0-d RBAC 放开与存量角色一次性迁移

- 默认表：`memory_update` 放开到 `execution`（`action_mapper.py:42`）；`memory_note` 默认表与本机存量不一致（默认 `['ceo']` vs 存量三角色），一并归正到同一口径。
- **存量迁移必须新写一个 meta 门**：现有 `implicit_tool_role_backfill_v1_applied` 只填**空**角色（`resource_registry.py:10,96-98,113-129`），而 `memory_update` 存量是非空 `["ceo"]`，走不到那条分支。可参照的一次性重写先例是 `_migrate_legacy_tool_families`（`main/governance/store.py:127-162`，meta 键 `tool_granularity_migration_v1_applied`）。
- **迁移守卫（机制级）**：只在「持久化角色恰好等于历史默认 `["ceo"]`」**且**该工具没有操作员策略变更审计记录时才放宽。审计来源是 `PUT /resources/tools/{tool_id}/policy` 写的 `operator_actions`（`main/api/admin_rest.py:2904-2937,2932-2936` → `runtime_service.py:13484-13503`）。有审计记录就跳过——操作员刻意收窄不许被迁移覆盖。
- 残留风险如实写明：操作员若曾把角色改成 `["ceo"]` 但审计记录已过保留期，迁移无法区分，会放宽一次。缓解是迁移落一条 INFO 与一条 operator action，让事后可查可回退。
- 迁移必须幂等可重入（meta 门），崩溃在标记前重跑只重复同一次放宽。

### P0-e 工具暴露形态（默认走契约候选道）

- 默认**不动** `NODE_FIXED_BUILTIN_TOOL_NAMES`。角色放开后，`memory_update` 作为资源工具走候选 → `load_tool_context` → 契约在场 → 下一跳可调用（`runtime_service.py:11002-11041,11098-11133`）。代价：节点第一次记东西要多一跳加载契约。
- 备选（可否决）：加进 `NODE_FIXED_BUILTIN_TOOL_NAMES` 换常驻可直接调。副作用要一起认：进 `NODE_NEVER_CONTRACT`（冲动路径免契约闸、seed 不再预注入）、进 `NON_HYDRATABLE`（`reuse_args` 豁免）、`fixed_builtin_tool_name_set_for_actor_role` 变成无 hydration target 与内置错误指引类（`tool_visibility.py:63-105`）。而且该名单是**角色级**，与 §4 的 continuous-only 门冲突，需要额外按节点种类过滤。
- 描述预算：`tools/memory_update/resource.yaml` 的 description 实测 **153 字符**，上限 160（`description_limits.py:27`）。本计划不改它；若改必须跑全仓预算测试。

### P0-f 提示词归正

- `main/prompts/shared_tool_discipline.md:14` 是**共享块**（执行与验收都读）。默认改写成条件式而非删除：去掉「当前链路不提供直接长期记忆搜索」这句无条件断言，保留「如未在当前上下文里给出相关长期记忆，就不要自行模拟或替代这类能力」。这样对没有记忆的节点种类仍然为真。
- `shared_continuous_duty.md` 增一条：记忆块在上下文里时按需用 `memory_note` 读细节；要记/改/忘用 `memory_update`，一次调用交任意数量要求（与 CEO 同一条约束）。
- 不在节点提示词里印字数上限/阈值数字（沿用记忆车道「数字只住配置」的口径）。

## 6. 机制级保证

- 节点**结构上不可能**产生复核队列行：节点道不调 `record_turn_for_review`，守卫测试按调用面钉住。
- 记忆块字节在两次压缩重印之间**不可能**变化：走 roster 头部同一条幂等合并路径，任何中途变化只以一次性通知纠正。
- 不满足范围门的节点**看不见**该工具（可见集层面排除），不是调用时才拒。
- 操作员刻意收窄的角色**不被迁移覆盖**（审计守卫）。

## 7. 风险

- **升级时在飞节点的 scaffold 漂移**：头部字节变化会让旧 seed 与新 stable 首条不一致 → `fallback_seed_prefix_drift`（`react_loop.py:10848`），该节点这一跳退回重建。属一次性、有界，但要在验收里量到并如实记录。
- **压缩边界重印成本**：记忆块进 system 即进 cache key，重印那一跳 family 轮转。因为与 roster 同边界，不新增轮转次数；但若将来有人把记忆重印挪到别的边界，就会额外破缓存——这条要写进文档当维护约束。
- **群源节点的写路由**：写进本群 manager 而不是本地。若作用域解析拿不到 originating session_key，会静默落本地——必须 fail-closed 到只读并回报，不许猜。
- **节点入队量**：continuous 根是长跑节点，主动提交频次由模型决定。已有主动道空闲封口与合批兜住 burst，但**没有节点侧频次上限**；如实写明这是残留暴露面，观测口径是台账 `trigger_sources=memory_update_tool` 且 `session_keys` 为任务会话的行数。
- **共享提示词改动波及验收节点**：P0-f 改的是共享块，验收节点也会读到新措辞。改写保持对无记忆种类为真，但要在验收里跑一次验收节点确认行为不变。
- **迁移误放宽**：见 P0-d 残留风险。
- **相邻文档漂移（本次不顺手改）**：节点每跳尾部契约已退役（`main/runtime/node_prompt_contract.py:351-362` 只做剥离），但 `context-and-cache-troubleshooting.md:271,508` 与 `tool-hydration-and-callable-chain.md:174` 仍写「尾部恰好 1 份当前契约」。本计划会改到这两份文档的节点头部段落，**改到时一并核实并归正**；不新增附录。

## 8. 验收

### 单测

- 范围门：continuous 根可见、子节点/验收节点不可见（可见集层面）。
- 头部幂等：同一投影连续两跳字节相同；投影变化但非压缩边界 → 头部不动、只出通知。
- 压缩边界：重印头部 + `memory_cursor` 对齐，增量不重投。
- 作用域：群源写本群、第三方私聊源只读拒绝、取数失败保留上一份冻结值。
- 复核隔离：节点跑若干轮后 `review_state.json` 无该节点会话条目、队列无复核来源行。
- 迁移：空角色/等于历史默认且无审计 → 放宽；有审计 → 跳过；重跑幂等。

### 命令

`python -m ruff check .`；smoke `tests/resources/test_resource_runtime_smoke.py`；记忆子集（含 `test_memory_scope_router.py`、`test_memory_update_contract.py`）；节点子集（`tests/resources/test_task_web_worker_runtime.py` 等按落点选）；改到工具描述才跑全仓预算测试。先存失败用 detached worktree 复核基线，不用 `git stash`。

### 真机（单测不算完成）

重启本机 web（18790，数据根 `D:\NEGI`，记忆工作区根是仓库 `D:\Project\Negi\memory\`），创建一个 continuous 根任务：

1. 从节点 `actual_request_ref` 工件确认 system 头部含 `## 长期记忆` 且正文与 `MEMORY.md` 逐字节一致。
2. 连续两跳比对 `prompt_cache_key_hash` 与 `model_prefix_hash`：非压缩边界必须相同。
3. 让节点用 `memory_update` 记一条临时标记 → 台账出现 `memory_update_tool` 行、`session_keys` 是该任务会话；**不出现任何复核来源行**。
4. 触发一次节点 token 压缩 → 头部重印、`memory_cursor` 对齐、family 轮转恰好一次。
5. 跑一个普通子节点与一个验收节点 → 头部无记忆块、工具不可见、行为与改前一致。
6. 清理临时标记与临时任务，确认常驻块回到基线条数。

## 9. 架构文档（实现后走 `negi-architecture-maintenance`）

- `runtime-overview.md`「Memory Runtime State」：记忆分域补节点车道；记忆块注入面从「CEO/frontdoor-only」改为按范围门列出。
- `main-task-runtime.md`：节点上下文构成与记忆基座/增量边界。
- `context-and-cache-troubleshooting.md`：节点 pinned 头部新增记忆块、重印边界唯一性、cache key 输入不变；顺带核实 §7 的尾部契约漂移。
- `tool-and-skill-system.md`：节点侧 `memory_update` 的可见性门与候选/契约路径、RBAC 迁移。
- `web-and-admin.md`：若迁移落 operator action，补审计面口径。
- `README.md`：预计不需要改（无新主题、无边界移动）；按 skill 清单复核。
- 就地改写、不追加附录；跑 `python scripts/check_architecture_docs.py`。

## 10. 交接段

- 本计划与 `work/memory-lane-batching-and-hop-plan.md` 是两份，后者已交付；本计划依赖其主动道合批。
- 冻结标识符不得改：`g3ku` 包名、`G3KU_*` 环境变量、`.g3ku` 数据根、上下文协议标记、`negi_*` 工具名。
- 提交信息英文；提交身份用 `-c` 临时指定历史作者；共享工作树只 add 自己的文件；每完成一个 P0 子项即提交。
- 若上下文耗尽：先更新本文件状态行与已完成子项，再开新会话续跑。