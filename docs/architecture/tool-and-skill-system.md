# G3KU 工具与技能系统说明

本文档解释 G3KU 当前的工具/技能模型，重点面向新接手者说明：

- 工具是如何注册和执行的
- skill 是如何被发现和加载的
- 为什么 Agent 每轮只能看到一部分工具
- candidate tool/skill、callable tool、hydration 分别是什么意思

## 1. 总体设计

G3KU 的工具/技能体系分成几层，而不是一次性把所有东西注入给模型：

1. 固定内置工具
   当前轮直接可调用。

2. 候选工具
   这轮只“可见但不可直接调用”，需要先 `load_tool_context(...)`。

3. 候选技能
   这轮只“可见但不自动注入正文”，需要 `load_skill_context(...)`。

4. 已 hydration 的工具
   某个候选工具在前一轮被显式加载后，下一轮进入真正可调用集合。

这个设计的核心目标，是控制上下文大小、减少工具误用、同时保留扩展能力。

## 2. 关键模块

### `g3ku/agent/tools/registry.py`

`ToolRegistry` 是底层工具执行容器，负责：

- 工具注册与动态替换
- 参数校验
- 转换成 LangChain `StructuredTool`
- 注入 runtime context
- 接入 tool watchdog 和资源管理器

这是“工具执行层”的核心，不负责策略选择。

### `g3ku/agent/skills.py`

`SkillsLoader` 负责从共享 `ResourceManager` 中列出和加载 skills：

- `list_skills()`
- `load_skill()`
- `load_skills_for_context()`
- `build_skills_summary()`

它负责“技能资源读取”，不负责“本轮 skill 是否可见”。

### `g3ku/runtime/context/`

这是“本轮上下文选择层”，决定：

- 本轮候选工具有哪些
- 本轮候选 skill 有哪些
- 节点上下文怎么拼
- 执行模型应该看到哪些可调用工具

对新人最重要的文件：

- `node_context_selection.py`
- `execution_tool_selection.py`

### `main/service/runtime_service.py`

这是工具/技能系统与任务运行时的集成中心。它把：

- 固定内置工具
- 治理/RBAC
- 候选池选择
- hydration 状态
- model 可见工具集

全部接到任务运行时里。

### 建议阅读顺序

1. `g3ku/agent/tools/registry.py`
2. `g3ku/agent/skills.py`
3. `g3ku/runtime/context/node_context_selection.py`
4. `g3ku/runtime/context/execution_tool_selection.py`
5. `g3ku/runtime/frontdoor/prompt_builder.py`
6. `main/service/runtime_service.py`

## 3. 四个概念必须分清

### 3.1 fixed builtin tools

指系统预定义、可直接调用的核心工具。

从 `main/service/runtime_service.py` 当前常量看，节点固定内置工具包括：

- `submit_next_stage`
- `submit_final_result`
- `spawn_child_nodes`
- `exec`
- `load_skill_context`
- `load_tool_context`

CEO/frontdoor 另有任务生命周期与分发控制类固定工具。各工具/家族的当前契约与守卫如下：

| 工具/家族 | 当前契约 | 守卫 |
| --- | --- | --- |
| `create_async_task` | CEO/frontdoor 创建 detached 任务的固定 builtin。参数：`task`、`core_requirement`、`execution_policy`、可选 final-acceptance 字段、可选结构化 `file_targets`；`file_targets` 条目若带 `path`，必须已是绝对路径且指向已存在的文件。frontdoor 运行时合同的 `attachment_reopen_targets` 提供可 reopen 的上传条目，模型需自己把精确 `path` / `ref` 抄进 `file_targets`；`user_uploads`、`current_uploads`、`user_image_and_docx` 等占位符不是有效目标（出现时按 frontdoor prompt/contract 引导失败处理，不是 content 工具漂移）。 | 创建前对当前会话未完成任务池做混合重复预检：确定性精确匹配层（规范化目标文本 + 精确关键词指纹）+ 巡检模型审查（返回 `approve_new` / `reject_duplicate` / `reject_use_append_notice`）；`create_task(...)` 前再做一次确定性精确重验，拦截预检放行后的陈旧读视图或重放竞争。唯一实现是资源工具 `tools/create_async_task_cn`（委托 `MainRuntimeService.precheck_async_task_creation(...)` / `revalidate_async_task_creation_before_create(...)`）；出现重复 detached 任务时，先核对创建是否走这条预检路径而非并行的 create 路径。`file_targets` 路径校验是 reject-only，不做自动改写/补全。前门解析只把显式成功形式 `创建任务成功task:...` 当作已核实派发；拒绝消息里提到的 `task:...` id 不算新建任务。 |
| `task_append_notice` | CEO-only 固定 builtin：向当前会话中既有的未完成任务追加新需求、约束或验收预期。成功文本形如 `已向任务 task:xxx 追加通知。`。普通执行/验收节点的内置集合不包含此工具。 | 成功文本必须停留在“更新既有任务”车道，不能形似 detached 任务创建，不产生 `verified_task_ids` / `route_kind=task_dispatch`。`reject_use_append_notice` 表示调用方应改为更新既有任务；拒绝措辞与前门解析显式指向本工具。失败任务的后续跟进一律走普通新规划/执行，没有隐藏续跑车道。 |
| `submit_message_distribution` | 节点消息分发模式的内部控制工具，配合 compact prompt `main/prompts/node_message_distribution.md`：检查当前 mailbox 消息与当前 live 执行子节点，决定哪些子节点收到改写后的后续消息。 | 分发模式是 control-only 车道，不暴露普通节点工具；分发轮次中出现 `exec`、`spawn_child_nodes`、content 工具或其他普通执行器，按契约回归处理。 |
| `content_describe` / `content_open` / `content_search` | 普通候选工具（先 `load_tool_context(tool_id="content_*")`，hydration 后下一轮可调），并消耗普通候选选择与 hydration LRU 预算。三者是同一 content navigation 契约的三个入口：`artifact:` 外部化内容引用优先传 `ref`；本地文件优先传绝对 `path`；`path` 不接受 `artifact:`（塞进 `path` 会返回 path-mode 错误）；`content_search` / `content_open` 同时收到 `ref` 和 `path` 时分别尝试两个目标并返回组合结果，一侧失败不覆盖另一侧成功结果。artifact content ref 的规范形态是单前缀 `artifact:<hex>`（artifact id 本身即 `artifact:<hex>`，ref 与 id 同形）；历史双前缀 `artifact:artifact:<hex>` 仍按同一 artifact 解析，两套形态可互换。ref 由系统分配，模型必须原样使用任务终态事件、任务/节点详情或先前 content 工具结果中给出的 ref，不得猜测或改写 artifact id；artifact 未命中时错误文案会显式提示这一点。 | `content_open` 的 agent-facing 行范围参数只有 1-based `start_line` / `end_line`（非正数无效）；`around_line` / `window` 只保留在 content navigation service 与 legacy `content(action=open)` 的 REST / service / legacy wrapper 层，出现在那些层不算 split 契约回退。图片 reopen 仍走 `content_open` 本身：成功结果返回结构化 payload（如 `content_kind=image`、`multimodal_open_pending=true`、`runtime_image_target`），由运行时决定是否把视觉内容附带到下一次模型请求；是否允许取决于当前运行时/模型绑定启用 `image_multimodal_enabled`，未启用时直接返回 `非多模态模型无法打开图片`；历史上下文里的图片 `path` / `ref` 只是 reopen 入口，重新查看像素仍需再次调用。超过 5 MiB 的目标图自动压缩到上限以内（先降 JPEG 质量、不够再降分辨率）；无法压缩到上限以内或文件缺失时跳过该图并附文本说明，单张图问题不中断整轮、不波及后续轮次。`content_search` 结果与 legacy search 走同一套 inline 尺寸守卫：良构 payload 超过 16000 字符或 260 行时外置为新 artifact，模型只拿到 summary + ref（超大结果以 content envelope 出现在上下文里属于预期交付）。legacy `content(action=...)` 兼容包装与 split tools 走同一底层 content service，split tools 并不更宽松。 |
| `filesystem` 家族 | `filesystem` 是稳定的 family/tool_id，只承担治理、候选工具归类、`g3ku://resource/tool/filesystem` URI 与 `load_tool_context("filesystem")` 家族级上下文加载。可执行的变更工具全部是 concrete executors：`filesystem_write`（整文件“创建或替换”）、`filesystem_copy` / `filesystem_move`（`operations=[{source, destination}, ...]`）、`filesystem_delete`（`paths=[...]`）、`filesystem_propose_patch`。`copy` / `move` / `delete` 是路径级批量操作，不采用 `filesystem_edit` 的 target-resolution 契约，因为其变更单位本来就是整个文件系统对象路径。 | 所有路径必须绝对路径，并先经过现有 workspace policy。目录 `copy` / `move` 只允许目标路径不存在；目录 `delete` 必须显式 `recursive=true`。不存在可调用的 legacy 兼容入口（如 `filesystem(action=...)`、`filesystem.search`、`filesystem.open`）。 |
| `filesystem_edit` | agent-facing 首选 target-first 契约：`path + target + new_text`，`target.by` ∈ `exact_text`（旧文本块已知且应唯一匹配时的首选车道）、`anchor_pair`（知道包围锚点但不知道确切正文时；运行时先解析锚点间的有序区域再应用编辑）、`line_range`（兼容/回退车道，仅适用于刚读取后已知确切行号的场景，不作为主要契约）。 | 运行时校验保留更宽的向后兼容车道：`mode + old_text/new_text` 与 `mode + start_line/end_line/replacement`。判定 legacy 调用类别前先剥离对侧车道的占位值（text-replace 可能自带 `start_line=0` / `end_line=0` / `replacement=""`；line-range 可能自带 `old_text=""` / `new_text=""`）；这只用于吸收 adapter/provider 自动填充噪声，不允许两车道都非空的真混合模式，`target + legacy 字段`也不是受支持的组合契约。`load_tool_context` / `get_tool_toolskill` 的参数摘要优先暴露 target-first callable 契约，即使底层校验器 schema 更宽。 |

### 3.2 candidate tools

候选工具是“本轮推荐给 agent 的具体工具列表”，默认可见但不可直接调用；需要先 `load_tool_context(tool_id="...")`。来源链路：资源注册表 → RBAC 过滤 → 检索/排序 → 节点上下文选择。

当前选择规则：

- `candidate_tool_names` / `candidate_skill_ids` 同一语义：当前候选集合等于 RBAC 可见集合；候选生成是 inventory-only，没有语义召回层。
- 普通候选数量由 RBAC 可见家族/执行器决定，节点与 CEO/frontdoor 都不再按语义 top-k 截断。
- 当 query 明显表达写入、改写、删除、移动、复制、补丁等变更意图时，本地候选打分优先推 `filesystem_write` / `filesystem_edit` / `filesystem_delete` / `filesystem_move` / `filesystem_copy` / `filesystem_propose_patch` 这类 concrete ids；`exec` 虽然可作为固定 builtin 保持可调用，但在这类意图下不作为候选文件变更方案的首选。

加载门控：

- 精确 `tool_id` 加载对两类 concrete tool 开放：当前 canonical `candidate_tool_names` 中的，以及当前 `rbac_visible_tool_names` 中仍然 surfaced 的，都可以读取 toolskill / 参数说明。`load_tool_context(search_query=...)` 维持可见工具搜索路径，不是枚举所有 RBAC 可见工具的 API。`load_skill_context` 只允许命中当前 canonical skill candidate 集合。
- 只有普通 candidate tool load 会进入 hydration/promotion、占用 hydration LRU；对已经 callable、已经 hydrated、fixed builtin，或当前仅 RBAC 可见但不在 candidate 里的 direct-load lane，`load_tool_context` 都是 read-only toolskill 加载。
- repair-required 资源从普通候选中剥离：工具进 `repair_required_tools`（不进入 agent-facing `candidate_tools` / `callable_tools` / `hydrated_tools`），skill 进 `repair_required_skills`（不进入 `candidate_skills`）；这两个列表只影响 agent-facing runtime contract，不等于 provider-facing `tools[]` 变化。

exec 与 memory 工具家族：

- 部分 concrete executors 同时是资源支撑与固定内置（CEO 的 `exec` / `memory_write` / `memory_delete` / `memory_note`，节点的 `exec` / loader tools），通过 fixed-builtin 路径直接可调用。
- `exec` 除 RBAC 外还有一条契约轴：surfaced family `exec_runtime` 可携带持久化 `metadata.execution_mode`；`governed` 保留 exec 侧守卫，`full_access` 移除 exec 侧 read-only / 路径 / 安全检查，但不绕过 Tool Admin 启用状态与 RBAC。当前模式的权威暴露位置是运行时工具合同 / `load_tool_context` payload。
- `exec` 是发现/探测工具（目录结构、文件名搜索、环境检查）；具体本地文件正文证据应来自 `content_open(path=..., start_line, end_line)`。`exec` 长输出的 agent-facing payload 是有界流式捕获：`head_preview` + `tail_preview` + 截断/捕获字节元数据，供排查“关键结果在命令末尾”的场景；普通结果没有稳定的 `stdout_ref` / `stderr_ref`，不应期待隐藏全量输出 ref。节点反复用 `exec` 提取源码片段时，应把引导转向 `content_open(path)`。
- `exec` 解码子进程 stdout/stderr 优先 UTF-8，Windows 上先回退宿主首选代码页再替换字节；Windows 子进程 Python 命令注入 `PYTHONIOENCODING=utf-8`（只稳定 Python traceback / `print()` 输出，不改变 RBAC 或 `execution_mode`）；文件系统校验命令、`agent_browser` 等子进程车道共用同一输出解码 helper——某条 Windows 路径仍乱码时，先确认它是否走了共享 subprocess-text helper。
- committed 长期记忆通过注入的 `MEMORY.md` 快照交付（display-only：剥离 memory id 与日期/来源头，只保留以 `---` 分隔的记忆文本块）；agent-facing 契约没有记忆检索工具，`memory_note(ref)` 是唯一的按需详细记忆加载器。节点执行/验收路径不注入额外记忆检索块。
- `memory_write` / `memory_delete` 是 queue-submit 工具：只请求记忆运行时稍后批处理，不在当前轮同步改写 committed 记忆；`memory_delete(content=...)` 接受对要遗忘内容的自然语言描述。实际改写/删除决策委托给带受限工具面的内部记忆 agent（不属于常规 agent-facing 目录，也不按 surfaced Tool Admin family 排查）；它把描述解析成具体 SQLite id，并可能就实质影响该批次的行报告 `inspired_memory_ids`。

状态与显示层：

- 对 CEO/frontdoor，`candidate_tool_names` / `candidate_skill_ids` 属于 internal canonical state；暴露给模型的当前轮显示合同只保留一份 `frontdoor_runtime_tool_contract`（结构化 `candidate_tools=[{tool_id, description}]`）。旧轮 candidate/tool/skill catalog 不进入 durable history，后续轮次只继承真实工具调用轨迹与上下文。
- `candidate_tool_names` 是运行时去重、hydration 排除、恢复和 gate 判断用的 canonical name list；`candidate_tool_items`（`{tool_id, description}`）只是它的显示层缓存，用于 contract rebuild / refresh 后保留描述文本。agent 只应看到结构化 `candidate_tools`；`candidate_tool_items` 不是第二份权威候选集——canonical `candidate_tool_names=[]` 时，agent-facing `candidate_tools` 也必须为空，不从旧 contract、旧 items 或旧动态消息把失效候选补回 prompt。
- 对执行/验收节点，canonical `candidate_skill_ids` 落在 runtime frame，`candidate_skill_items` 随 frame 持久化，供阶段切换、prompt compaction 之后的下一轮 contract 刷新从 frame 恢复。`_enrich_node_messages()` 注入、且已携带 `candidate_skills` / `contract_visible_skill_ids` / `skill_visibility_diagnostics` 的 fresh skill 合同是 first-turn truth source：默认空 bootstrap frame 只负责占位与 phase 跟踪，不能把这些字段覆写成空；同一 turn 内 `_prepare_messages()` 裁掉尾部合同消息后，fresh skill 合同摘要沿 runtime context 继续传给 `react_loop`。
- 字段级排障入口：`contract_visible_skill_ids` 是 `runtime_service._node_context_selection_inputs()` 当轮记下的 contract-visible skill 快照（输入层证据，随 runtime frame 与 `runtime-frame-messages:{node_id}` artifact 落盘）；`skill_visibility_diagnostics.entries` 携带 `registry_skill_ids` 与逐 skill 的 `enabled` / `available` / `allowed_for_actor_role` / `policy_effect` / `included_in_contract_visible`，用于定位是 live `resource_registry`、`allowed_roles` 还是治理策略拦掉了 skill；节点 context selection cache 与 `persisted_frame_router` 都带 live-visibility freshness gate——复用旧 selection 前重新对照当前 `session_key` / `actor_role` / `visible_tool_names` / `contract_visible_skill_ids` / `registry_skill_ids`，一旦漂移就丢弃旧 selection，重新跑 `_node_context_selection_inputs()` 与 `build_node_context_selection(...)`。这挡的是“外部 resource/governance refresh 改了可见性，但节点长期沿用旧 cache / 旧 frame”的回归（尤其“首轮 skill 可见集为空，后续轮次一直空”）。
- 前门候选生成诊断同步在 session snapshot 的 `frontdoor_selection_debug`：`tool_selection` 回答命中项为什么没进最终 `candidate_tool_names`。

### 3.3 candidate skills

候选 skill 与 candidate tools 类似，但机制更轻：

- skill 不进入 tool callable 集合
- 需要显式 `load_skill_context(skill_id="...")`
- skill 加载是“当前轮立即消费正文”
- 不走 hydration 状态机
- 对 CEO/frontdoor，`frontdoor_runtime_tool_contract` 摘要会把 `candidate_skills` 明确标成“可通过 `load_skill_context` 读取正文”的候选，避免模型把它们误读成需要安装/水合的候选工具
- repair-required skill 有更强的门控：它仍可作为“待修复资源”出现在 agent-facing `repair_required_skills` 中，但修复完成前 `load_skill_context(...)` / `load_skill_context_v2(...)` 直接返回 repair-required 错误与修复指引，不返回正文；遇到“模型知道这个 skill 存在却无法 load”，先检查 skill 资源本身的 `available` / warnings / errors，而不是先怀疑 selector 没选中

### 3.4 hydrated tools

某工具在前一轮成功 `load_tool_context` 后，会进入 hydration 状态；下一轮它进入 `model_visible_tool_names`，模型可以直接调用它。这是工具系统里最容易被误解的概念：`load_tool_context` 不是执行工具，而是把它提升成后续 turn 的 callable tool。

canonical 状态与 LRU：

- 节点：runtime frame 中的 `hydrated_executor_state` / `hydrated_executor_names`，节点生命周期级 LRU，跨多轮、阶段切换、pause/resume、frame restore 保留。
- CEO/frontdoor：`RuntimeAgentSession._frontdoor_hydrated_tool_names` 与前门 persistent state 的 `hydrated_tool_names`，session 生命周期级 LRU，跨 turn 保留，每轮按当前 RBAC 可见集合过滤。
- 两侧 LRU 都只接受 concrete tool names；family id 不进入 canonical hydration state。默认上限都是 16；promoted tool 在第 17 个之后被逐出时，优先检查对应运行时对象上的 `_hydrated_tool_limit` 是否被显式改小。
- resource-backed fixed builtin executors 不进入 hydration LRU：为已经 fixed-callable 的工具加载 toolskill 可能返回契约/帮助文本，但不占 hydration 槽位、不产生下一轮 promotion 条目（节点 frame 与 frontdoor session state 两侧同规则）。排查缺失的 hydration promotion 时，先区分“普通扩展执行器”与“资源支撑固定内置”，后者按设计留在 LRU 之外；`content_describe` / `content_open` / `content_search` 属于前者，成功的 `load_tool_context(tool_id="content_*")` 应占用普通 hydration 槽位并在下一轮 promote 该 concrete tool。
- RBAC 可见集合（`rbac_visible_tool_names` / `rbac_visible_skill_ids`）、`lightweight_tool_ids`、`model_visible_tool_selection_trace` 等内部状态保留在运行时，供过滤 hydration 与恢复链路使用，但不进入 agent-facing 合同（`frontdoor_runtime_tool_contract` / `node_runtime_tool_contract`）。

重读与指纹：

- 成功的 `load_tool_context` payload 携带内部 `tool_context_fingerprint`（不是 provider-facing schema 字段），运行时只用它判断当前 toolskill 契约是否变化到值得重读。
- 对 callable / hydrated / fixed-builtin 的 direct-load，节点运行时与 CEO/frontdoor 都会扫描当前未压缩的 inline 历史，找同一 resolved `tool_id` 最近一次成功结果：fingerprint 未变时软拦截重读，提示模型复用既有 toolskill；fingerprint 变化，或旧结果已被 `token_compression` / `stage_compaction` 压缩掉，则允许再次重读。

promotion 与前门状态：

- 工具 promotion 的权威来源是执行循环里的 `raw_result.ok / raw_result.hydration_targets`，不从尾部 `ToolMessage` / `result_text` 反推。生产前门的唯一 promotion 入口是自研 frontdoor 步骤循环的 `execute_tools` 节点（`_graph_execute_tools()`），它必须复用与模型暴露阶段相同的 runtime-visible tool bundle（含运行时注入的 `submit_next_stage`）；执行环节只按 `state.tool_names` 重建工具映射，会重新制造“模型能看到 `submit_next_stage`，执行时报 `tool not available`”的分裂。
- frontdoor 会把成功 `load_tool_context` 的 concrete tool 写进自己的持久状态，并在同一用户 turn 的后续模型轮次里直接并入 callable tool 集合；frontdoor approval interrupt、session inflight snapshot、paused execution context 都会携带这份 hydrated 状态。排查“load 成功但下一轮又看不见工具”时，不能只看 candidate tool 提示块，还要看 frontdoor 当前保存的 hydrated tool state。

参数错误与状态分类：

- 工具参数校验错误在 `ToolRegistry`、CEO/frontdoor 直接工具执行、节点 `ReActToolLoop` 之间共享同一维护契约：`validate_params(...)` 返回错误、`validate_params(...)` 自身崩溃，或工具执行抛出 `ValueError` / `TypeError` 时，返回的错误文本保留原始错误，并追加指回 `load_tool_context(tool_id="<tool_name>")` 的修复提示。权限错误、路径策略错误、超时停止、watchdog 停止、pause/cancel 信号与普通 `RuntimeError` 保持原语义，不误标为参数错误。
- 任何顶层为 `{"ok": false, ...}` 的结构化工具结果，在三条路径上都按 error-lane 工具结果处理；这条规则有意比参数引导规则更宽，让以 JSON payload 编码失败的内嵌工具也进入错误车道。

阶段门控与 callable 收紧：

- CEO/frontdoor 的 stage gate 由 `execute_tools` 真正执行：普通工具在无活动阶段或预算耗尽时直接收到 gate error；同一批 tool calls 同时包含 `submit_next_stage` 和普通工具时，先执行 `submit_next_stage`，再把同批普通工具当作新阶段的第一批调用，并在该新阶段上记账预算。
- 当前没有“有效阶段”（含预算耗尽、必须换阶段）时，agent-facing `frontdoor_runtime_tool_contract.callable_tool_names` 收紧到只剩 `submit_next_stage`；execution / acceptance 节点采用同样收紧，没有例外。这不同步收紧 provider body 里的 `tools[]`：为保持 prompt cache 前缀稳定，provider-facing 继续使用稳定的 runtime-visible tool bundle，阶段控制交给动态合同与执行门控（详见下文「CEO Provider Tool Surface」）。
- execution / acceptance 节点不必把 `submit_next_stage` 单独拆成一轮：阶段切换成功时，同批普通工具作为新阶段首轮执行；切换失败时，同批剩余普通工具被批内阻断，不回退旧阶段继续执行。执行层的 `stage_gate_error_for_tool()` 是 schema 收紧之外的兜底防线：模型通过恢复态或手工构造仍尝试普通工具时，返回 `no active stage` / `current stage budget is exhausted`。
- 兜底闸门的两处豁免：① 落库类工具 `memory_write` / `memory_delete` / `memory_note` 是 CEO/frontdoor 全局白名单（`FRONTDOOR_STAGELESS_MEMORY_TOOL_NAMES`），无活动阶段也可调用——保证"用户口头给一条长期指令 → 写记忆"不会被 `no active stage` 拦下后一次性放弃、永久丢失；② 节点暂停（`task_node_error`）心跳轮 `allow_stageless` 放行首个实质性工具，阶段由 `_frontdoor_stage_state_after_tool_cycle` 自动补开（`system_generated`，预算 10，标题=「任务 ID xxx 中的节点出现自动暂停，检查原因并处理」），预算耗尽后 `transition_required` 置真、回到普通闸门。这两处豁免只改执行门控，不改 `callable_tool_names` 的收紧口径，也不改 provider `tools[]` 前缀稳定性。
- 这组收紧不改变 candidate 语义：`candidate_tool_names` / `candidate_skill_ids` 仍表达 RBAC 可见集合的候选集，只是无有效阶段时这些候选不同时出现在 agent-facing callable contract 里。
- 内部轮次继承：当前 session 已有权威 frontdoor baseline 与前序 contract state 时，`heartbeat_internal` / `cron_internal` 不被收紧、也不重跑 candidate/hydration/skill selection，而是直接继承上一轮的 callable / candidate / hydrated / provider-tool / visible-skill 状态；从 agent 视角看，它们就是在上一轮 frontdoor contract 上追加隐藏内部提示后的普通 CEO/frontdoor 轮次，可以直接输出，也可以立即开始阶段并调用已继承的普通工具。尚无权威 baseline 时，内部轮次回退到普通 exposure assembly。`cron_internal` 的其余特例只有两点：reminder 正文是隐藏的结构化 `system` 事件块；cron 任务的停止与删除由 scheduler 侧的 `payload.max_runs` / `state.delivered_runs` 计数器负责。
- 当前 `cron` 工具合同是“结构化提醒”：`message` = 给未来 agent 的提醒动作；`max_runs` = 成功送达上限，省略默认 1；`at` = 只接受创建时仍在未来的单次触发时间，真正执行 `add_job()` 时该时间已过则拒绝创建，并提示 `任务定时已过期，当前时间为<service-local time>，请立即执行或视情况废弃而不要创建过期任务`；`stop_condition` 是兼容字段，不参与运行时停止判断。`cron` 的使用规范（提醒写成内部指令、调度三选一、投递目标由运行时从当前会话上下文自动推导、模型不传 `delivery.*` / `sessionTarget` / `payload.*`）放在 cron 工具的 toolskill 里，按需 `load_tool_context("cron")` 加载；模型对 cron 用法理解过时时，改 toolskill 而不是改注入逻辑。排查“为什么没有自动停止”先看 cron store 的 `payload.max_runs` / `state.delivered_runs`；排查“cron 到点了但没创建/查询任务、只重复谈 cron 自己”，先检查 frontdoor tool exposure 是否被错误缩成 `cron`，而不是先怀疑 scheduler 没触发。
- `submit_next_stage` 的阶段预算在 execution / acceptance / CEO-frontdoor 三条路径统一为 `1-10`，允许在预算未耗尽前提前切到下一阶段；预算是“本阶段声明的上限窗口”，不是“必须烧满的最小轮数”。
- `load_tool_context` / `load_skill_context` 属于上下文加载型工具调用：写入 round 历史，但不增加当前阶段的 `tool_rounds_used`；节点与 CEO/frontdoor 记账同一规则，预算结论只看 `rounds[*].budget_counted` 与聚合后的 `tool_rounds_used`，不按 transcript 里的 loader 调用次数自行推断。CEO UI 上，成功 loader 调用在输入框上方显示短暂的 live-only notice（尽量带 `tool_id` / `skill_id`），不作为长期保留的工具步骤；loader 失败时仍优先检查原始 round/tool 数据与 runtime snapshot。
- 前门要区分两份工具集合：`tool_names` 保存阶段内可恢复的完整 callable pool；“当前轮合同暴露给模型的 callable tools”要通过前门 callable-tool helper 结合 `frontdoor_stage_state` 再算一次。不要把前者直接当作当前轮模型可见函数列表，也不要把 agent-facing 收紧误解成 provider `tools[]` 已同步收紧。
- `frontdoor_stage_state`、`compression_state`、`hydrated_tool_names` 是受保护运行时状态：工具合同刷新不能覆盖、清空或重置这些字段。

### `manage_task_nodes` 节点控制工具

`tools/manage_task_nodes_cn` 提供给 agent 处理错误暂停节点的 callable tool。它调用 `MainRuntimeService.control_nodes(...)`，一次请求可以包含多个同一任务的节点，并按节点返回结果。

- `action` 取 `resume`、`keep_paused`、`fail`、`pause`。`keep_paused` 必须提供非空 `remark`；该备注写入节点暂停登记，供后续 heartbeat 决策使用。
- `resume` 清除暂停并让运行中的 dispatcher 从持久化 runtime frame 续跑；`fail` 将暂停节点置为终态并释放父节点等待；`pause` 以 `pause_reason=agent` 登记 agent 发起的暂停。
- 工具层只负责参数与结果契约，节点暂停的安全边界、future 等待和恢复语义归 `runtime-overview.md`「Node-Level Pause and Recovery」；错误暂停事件的投递归 `heartbeat-system.md`「Task Node Error Delivery」。不要通过普通 task 工具或直接改 SQLite 表替代此入口。

## 4. 一条从上下文到 callable tools 的链路

1. 节点/CEO 进入一次新 turn。
2. `runtime/context` 模块根据 query、历史、治理规则挑出候选工具/技能。
3. prompt builder 把它们以 candidate 列表形式展示给模型。
4. 模型若决定需要某候选工具，先调用 `load_tool_context(tool_id="...")`。
5. 只有当该工具仍属于 canonical candidate，且解析到的是普通 concrete extension executor 时，系统才记录 hydration 状态。
6. 下一轮 `model_visible_tool_names = fixed builtin tools + hydrated tools`。
7. 只有这一轮，工具才真正成为 callable tool。

四条不变量：

- “看得见”不等于“现在就能调用”。
- “load_tool_context 成功”不等于“这一轮立刻可调”。
- “RBAC 可见且 surfaced”不等于“必然会被 promotion”；它也可能只是一个 read-only toolskill load。
- prompt 里的候选池与实际 callable tool 集合是两套集合。

frontdoor 边界：

- frontdoor 的 callable tool 集合不只来自 fixed builtin，还会并入当前 turn 内已 hydration 的 concrete tools；`candidate_tool_names` 必须排除已进入 hydrated state 的工具。一个工具同时出现在 candidate 列表和 callable tool schemas 里，通常表示状态推进漏了。
- 排查“`load_tool_context` 成功后下一轮仍只会 `exec` / 再次 load”：优先检查 frontdoor persistent state 里的 `hydrated_tool_names`、`tool_names`、`candidate_tool_names` 是否一起更新，而不是只看 toolskill 内容。模型反复对同一 callable / hydrated / fixed-builtin 工具再次 load 时，先检查消息历史里是否还保留同一 resolved `tool_id` 且 fingerprint 未变化的未压缩结果——那是预期中的 duplicate direct-load 软拦截，不是 registry 丢失。
- 线上 frontdoor 表现与测试 helper 不一致时，先确认 runner 是否真的走自研步骤循环；生产只有这一条 promotion 路径。

节点边界：

- 对执行/验收节点，`callable_tool_names` / `candidate_tools` / `candidate_skills` 都属于每轮动态 `node_runtime_tool_contract`；稳定 bootstrap user JSON 只保留稳定节点上下文，`execution_stage` 只由当前轮尾部合同承载。与运行时合同同轮出现的 overlay / repair overlay 只允许作为 request-tail 临时消息追加，不原地改写 bootstrap user 或任何更早的持久化消息，否则破坏稳定前缀与 prompt cache 命中。
- `candidate_tool_names` / `candidate_skill_ids` 是唯一 gate truth source；`candidate_tool_items` / `candidate_skill_items` 只是描述文本投影。canonical 列表为空时，重建后的合同也必须把 `candidate_tools` / `candidate_skills` 渲染为空，不从旧 contract item 列表或旧 frame item 缓存复活失效候选。
- 恢复链路：restored `selected_tool_names` 保持“恢复的 callable 工具 ∪ 恢复的 candidate concrete 工具”，不塌缩成“仅 callable”，让下一轮 node tool-provider 能把两个集合一起交回 schema selection；node tool provider 也要把恢复的 candidate executors 作为可见工具暴露，即使当前只有 `callable_tool_names` 立即可调——否则一次成功的 hydration promotion 就可能把下一轮候选池清空，后续 `load_tool_context(tool_id="content_open")` / `load_tool_context(tool_id="filesystem_write")` 会在人为缩小的候选集上失败。
- 区分“当前轮对模型暴露的 callable 合同”与“内部可恢复的完整 callable pool”：前者在无有效阶段时收紧到 `submit_next_stage`；后者只保留在本地 `model_visible_tool_selection_trace.full_callable_tool_names` 供排障。节点 `runtime frame`、动态 `node_runtime_tool_contract` 与 `runtime-frame-messages:{node_id}` artifact 必须写入同一份收紧后的 callable 列表；三者不一致按运行时合同分裂排查，而不是先怀疑 prompt 文本。frame 与重建合同对 skill 候选也必须一致：`candidate_skill_ids` / `candidate_skill_items` 在 frame 中存在时，不应因阶段压缩或 active window 裁剪而在下一轮合同中无故清空；但“从 frame 恢复”的前提是 frame 本身已是 authoritative skill-contract frame——当前 frame 只是初始化的默认空字段、而本轮已携带 fresh skill 合同时，rebuild 必须优先保留 fresh 合同。

CEO/frontdoor 合同载体：

- `turn overlay` / `repair overlay` 属于 dynamic appendix 一侧的当前轮临时内容；请求体中它们位于最新 user 回合之前，不能回写已有 stable/request user 消息。
- `dynamic_appendix_messages` 的持久化形态只保留当前 `frontdoor_runtime_tool_contract`；retrieved context 这类需要在同一 turn 后续模型轮次保留的内容，留在 `messages` / stage state / canonical context 的重建链路里，不作为第二份 appendix 尾插。每个 provider-bound request 在动态区域只携带一份最新 contract：每轮重建先剥掉携带历史里的旧 contract 与 turn-only note，再将当前 authoritative contract 插入最新 user 消息之前；turn 结束写回 durable transcript 时全部剥离。同一 turn 内，最新摘要块就是权威合同。
- 模型面向的运行时合同是以 `## Runtime Tool Contract` 开头的 assistant 摘要块，用紧凑文本向模型解释 callable / hydrated / candidate / stage 状态；provider 原生 callable schema 仍走 provider `tools[]`。合同识别有一条硬不变量：运行时注入的合同消息从不携带 `tool_calls`，因此任何携带 `tool_calls` 的 assistant 消息都是模型回合本身——即使其文本回显了合同抬头或合同 JSON，也不得判为合同消息而剥离，否则该回合只剩孤儿工具结果，会触发节点孤儿子工具结果熔断。execution / acceptance 节点使用同样的摘要式合同车道：节点摘要只以 names-only 暴露 `hydrated_executor_names`，详细 provider-call schema 留在节点 `provider_tool_names` 与 provider `tools[]`。摘要块还可携带：`attachment_reopen_targets`（runtime-owned 的上传 reopen 元数据，覆盖当前轮与 transcript 历史上传，是模型可见引导，不是浏览器/UI 表面；创建 detached 任务需要 reopen 上传文件/图片时，模型应把精确目标字符串抄进 `create_async_task.file_targets`，运行时不自动注入任务记录）；`repair_required_tools` / `repair_required_skills`（agent-facing-only 的修复车道，让模型看到“先修复再使用/查看正文”的资源，而不误读成普通能力）。摘要必须明确两条不对称语义：`candidate_tools` 是 candidate executor 摘要，通常仍需 `load_tool_context(...)` 后等待下一轮 hydration/promotion；`candidate_skills` 是 loadable skill 摘要，列出的 `skill_id` 应直接理解为 `load_skill_context(skill_id="...")` 的正文入口。
- CEO/frontdoor 还把下一轮 body baseline 持久化为 session-owned 的 `frontdoor_request_body_messages`：body-only，写回 session state 时剥掉动态 `frontdoor_runtime_tool_contract` 消息，让下一轮重建一份新的权威尾部合同。fresh visible CEO/frontdoor turn 通过直接 continuation 路径消费这份 baseline；在记录任何显式 shrink 原因之前，fresh visible turn 却从 transcript/stage replay 重建，就是 frontdoor continuity bug。direct-reply turn finalization 必须保住这份权威 baseline，并在 session sync 前追加最终 assistant 回复。baseline 变短只在 `token_compression` 与 `stage_compaction` 两种理由下合法（记录在配套的 `frontdoor_history_shrink_reason`）；没有这两个理由的变短按运行时上下文丢失处理，不是正常合同重建。
- approval interrupt 与 pause/recovery payload 携带这些 runtime-owned frontdoor 字段：`frontdoor_stage_state`、`compression_state`、`hydrated_tool_names`、`tool_call_payloads`、`frontdoor_selection_debug`；恢复后丢失按“frontdoor canonical runtime contract / runtime state 损坏”排查。

排查顺序：

- 当前轮合同先看 request 动态区域中唯一的 `frontdoor_runtime_tool_contract`，它位于最新 user 消息之前；再看 internal state 的 `tool_names` / `candidate_tool_names` / `candidate_tool_items` / `hydrated_tool_names`；稳定 prompt 前缀、旧 overlay 文本、旧 transcript 里的 tool/skill 名单都不是当前轮权威合同。“load 成功但下一轮没调用”时，对照 canonical runtime frame / frontdoor state 与 runtime messages snapshot；旧 bootstrap 文本与当前 snapshot 冲突时，以当前 snapshot 为准。“某工具为什么没进前门候选集”看 `frontdoor_selection_debug.tool_selection`（命中项为什么没进 `candidate_tool_names`）。
- 排查 CEO/frontdoor cache drop 时区分：`messages` 保存的是“下一次重建 request body 的基线”，`dynamic_appendix_messages` 只是“当前轮唯一尾部合同”；两边都出现完整候选/合同副本，说明 runtime contract 重复注入。

优先级边界：

- `ceo_frontdoor.md` 中的 stage-first 协议高于本轮 skill/tool 暴露提示，是稳定协议。前门动态提示里的“如需完整 workflow 正文可调用 `load_skill_context`”“如需工具契约可调用 `load_tool_context`”，真实语义都是“仅在活动阶段已经存在后，才进入下一步可执行顺序”。当前没有活动阶段时，即使已经看到候选 skill 和候选 tool，也应先走 `submit_next_stage`；否则运行时会在执行时返回 `no active stage` 门控错误。

- restore / recovery 只接受 frame 或 CEO/session state 中的 canonical callable/candidate/hydrated/skill 字段；缺失时直接视为“运行时工具合同损坏/缺失”，不回退 bootstrap 或旧动态文本。

## 5. 当前系统为什么这么设计

当前设计针对几个反复出现的问题：

- tool family 和 concrete tool 混在一起，agent 语义不稳定
- 候选池与真实可调用集边界模糊
- skill 与 tool 的加载模型不一致
- family 级别的抽象容易误导 agent

因此：

- agent 尽量只看到 concrete tool / concrete skill
- family 更多留给 UI、治理、后台管理层
- tool 通过 hydration 进入 callable
- skill 通过 direct load 获取正文，不做 hydration

`filesystem` 是这个边界最典型的例子：

- family `filesystem` 继续稳定存在，但它只承担 family/context 身份，不是 callable executor。
- `load_tool_context("filesystem")` 返回的仍是 family 级说明，不意味着会把 monolith `filesystem` 提升成下一轮可调工具。
- 真正会进入 `model_visible_tool_names` 的，只能是 `filesystem_write` / `filesystem_edit` / `filesystem_copy` / `filesystem_move` / `filesystem_delete` / `filesystem_propose_patch` 这些 concrete executors。

## 6. skill 与 tool 的差异

### tool

- 有参数 schema
- 由 `ToolRegistry` 执行
- 可进入 callable tool 集合
- 可能受 watchdog、resource lock、runtime context 影响

### skill

- 本质上是工作流文本/说明文档资源
- 由 `SkillsLoader` / `ResourceManager` 加载
- 不是直接 executable tool
- 是否使用取决于 prompt 约束和 agent 行为

## 7. 维护时最容易踩坑的点

- 把 candidate 当 callable。
- 把 skill 当 tool。
- 忘记 hydration 的“下一轮”语义。
- 把 family 当 agent-facing 语义。
- 误以为 `ToolRegistry` 决定了 RBAC；实际上它只负责执行层。
- 在 CEO frontdoor 中，只盯可见 skills / candidate tools，而忽略了 `ceo_frontdoor.md` 里的 stage-first 稳定协议。
- 把动态 skill/tool 暴露块里的“加载说明”误读成无条件立即执行指令；实际上它们仍受活动阶段存在与否的约束。
- 把节点 `runtime_environment.path_policy` 里的 content 路径约束误读成“content 工具总要传 `path`”；实际上 `artifact:` 必须走 `ref`，只有本地文件才走绝对 `path`。
- `content_*` 从新一轮 callable 列表消失时，先查候选选择与 hydration 状态，而不是 fixed-builtin 暴露。
- 用 transcript 里 loader 调用次数推断阶段预算；`load_tool_context` / `load_skill_context` 不计入 `tool_rounds_used`。

## 8. 维护高风险区域

- `main/service/runtime_service.py`
  因为 fixed builtin、candidate、governance、hydration 都在这里汇合。

- `g3ku/runtime/context/`
  小改动就会改变候选池和提示词，直接影响 agent 行为。

- `g3ku/agent/tools/registry.py`
  一旦 runtime context、watchdog 或 schema 处理出错，会影响所有工具执行。

## 9. Duplicate Tool Call Guard

Tool visibility and callable status do not guarantee that the runtime will keep executing the exact same call forever.

- `main/runtime/react_loop.py` tracks repeated ordinary tool signatures inside a node.
- When the model emits the same non-control tool call with the same normalized arguments several turns in a row, the runtime soft-rejects that turn: it records the repeated assistant tool call, appends an error tool message explaining that the call is duplicated, and lets the next model turn repair itself (reuse the prior result or change arguments) instead of escalating directly to an engine failure.
- This duplicate-call guard is distinct from the read-only retrieval guard: repeated read-only calls such as legacy `content(action=open/search/describe)`, split `content_describe` / `content_open` / `content_search`, and `task_progress` use their own repair-guidance path with its own repair messaging and escalation semantics.
- The filesystem family does not participate in the read-only retrieval branch: `filesystem` is a family/context id only, and the execution runtime only hydrates concrete mutation executors; repeated filesystem mutations follow the ordinary duplicate-call soft-reject path, while retrieval-style guards remain reserved for read-only tools such as `content_*`, `task_progress`, and `task_node_detail`.
- If a node loops on one tool, inspect the transcript/tool messages first: the absence of a fresh tool result may mean the runtime intentionally rejected a duplicate call rather than that the tool executor failed.

`runtime-overview.md`「Repeated Tool Call Guard」一节是指向本节守卫的摘要引用。

## 11. Tool Admin RBAC For Surfaced Tool Families

There is an explicit maintenance boundary between:

- tool families that appear in Tool Admin (`/api/resources/tools`, Tool management UI), and
- internal fixed tools that never appear there, such as `submit_next_stage`.

For Tool Admin surfaced tool families, RBAC is the highest-priority access contract. Internal fixed tools remain outside this contract: they keep their runtime-only visibility rules, their access is not modeled through Tool Admin `allowed_roles`, and behavior questions about stage protocol tools such as `submit_next_stage` are debugged through the stage/runtime path rather than Tool Admin RBAC.

Current RBAC rules for surfaced families:

- `actions[].allowed_roles` is an exact persisted whitelist. An empty list means deny-all for that action.
- Refresh, reload, reopen, and store readback must preserve an explicit empty list; maintainers should treat `[]` as real state, not as “missing”.
- If an executor belongs to a surfaced tool family, its model visibility follows Tool Admin RBAC exactly. A surfaced fixed-builtin executor may still be listed in frontdoor or execution fixed-builtin sets, but it only becomes actually visible/callable when the surfaced family/action RBAC allows it. When debugging “the tool still appears after I removed all roles”, inspect the persisted `tool_families` record and the derived `role_policy_matrix` first; there is no fallback to `ceo` or `execution`.
- If a fresh workspace shows an unexpected default role set, compare the resource discovery governance with the first persisted `tool_families` row before debugging prompt assembly or frontend rendering.
- First-discovery seeding boundary (separate from persisted RBAC): when a surfaced tool action is discovered for the first time and there is no persisted `tool_families` row/action yet, runtime seeds `allowed_roles` from the tool's discovery governance — either explicit resource-local `governance.actions[].allowed_roles`, or the implicit default governance mapping in `main/governance/action_mapper.py`. This matters especially for merged surfaced families such as `skill_access`, where concrete executors like `load_skill_context` and `load_tool_context` share one family/action row. After that first persistence boundary, `tool_families.payload_json` becomes authoritative; reload/refresh must preserve operator edits, including an explicit persisted `[]`.
- One-time legacy repair boundary for older workspaces: before `governance_meta.implicit_tool_role_backfill_v1_applied` is set, refresh may backfill an older persisted empty `allowed_roles=[]` action from the newly discovered non-empty discovery-governance default, to heal historical fresh-start rows accidentally persisted as deny-all for implicit-governance surfaced families. Once that meta flag is written, later refreshes stop auto-healing empty lists; an operator-cleared `[]` stays authoritative.

前端与 API 侧职责详见 `web-and-admin.md`「Tool Admin RBAC Contract」。

### Exec Runtime Mode Contract

- `exec_runtime` is currently the only surfaced tool family with an extra persisted mode field in Tool Admin: `tool_families.metadata.execution_mode`.
- The resource manifest may provide a default `settings.execution_mode`, but the persisted family metadata is the runtime source of truth once an operator saves an override; resource refresh must preserve that override, otherwise Tool Admin would show one mode while runtime execution silently falls back to another.
- `load_tool_context("exec")` / `load_tool_context("exec_runtime")`, node dynamic contracts, and frontdoor dynamic contracts should all agree on the same `exec_runtime_policy` payload. If they disagree, debug the persisted tool family record first, then the contract-injection path.

### CEO Regulatory Governance Mode

- Tool Admin also owns one global persisted switch for CEO/frontdoor: `ceo_frontdoor_regulatory_mode_enabled`. It is governance metadata used by CEO/frontdoor approval policy, not stored on a specific surfaced tool family.
- Its scope is narrower than generic Tool Admin RBAC: RBAC decides whether a surfaced tool family/action is visible or callable at all; regulatory mode only decides whether already-visible medium/high-risk CEO tool calls must pause for batch human review.
- With the switch enabled: risky CEO tool calls are grouped into one `frontdoor_tool_approval_batch` interrupt; `review_items` enumerate only the risky calls that require operator review; the resume side must submit one complete `submit_batch_review` payload covering every `review_item` exactly once. Pass-through low-risk tool calls in the same original tool batch remain part of the runtime-owned original tool-call ordering — “review items” are not “all tool calls in the round”.
- Rejected risky calls do not disappear: CEO/frontdoor constructs synthetic rejection tool results and merges them back with approved real tool results in original tool-call order before the next model round.
- Changing the switch affects future approval boundaries immediately, even for already-running CEO sessions, but must not silently rewrite an approval batch that is already paused and waiting for review.

### Removal Of `message` / `messaging`

The surfaced `message` executor and its Tool Admin family `messaging` are absent from the resource/tool contract entirely: resource discovery finds no `tools/message/resource.yaml`, Tool Admin lists no `messaging` family, and CEO/frontdoor fixed builtin exposure and the default `frontdoor_interrupt_tool_names` include no `message`; if either still appears in Tool Admin or in provider-facing web tool schemas, treat that as a contract regression rather than a disabled-by-default state. Browser/web replies travel through the websocket session path (`ceo.reply.final`, inflight snapshots, and related runtime events) and China/channel replies through `SessionRuntimeBridge` plus `ChinaBridgeTransport` deliver frames, so channel reply delivery is transport-owned and independent of this tool-family contract.

### CEO Provider Tool Surface

For CEO/frontdoor prompt-cache debugging, maintainers distinguish two tool surfaces: `tool_names` represent the current turn's agent-facing callable pool, while `provider_tool_names` represent the provider-facing stable superset used to build `tools[]` for function calling. Hydration promotion and stage gating change the `frontdoor_runtime_tool_contract` overlay, but must not churn the provider-facing `tools[]` bundle every round. The contract is an assistant-format runtime summary inserted before the latest user message; it is not a provider tool schema and is not durable history. If a model echoes the summary, frontdoor output normalization performs one private repair attempt and never promotes a repeated contract echo to final output.

Drift rules:

- Ordinary turns may refresh provider-facing `tools[]` from the current RBAC-visible concrete tool set only when membership truly changed; if the recomputed bundle has the same names in a different order, keep the persisted order exactly as-is instead of rotating schemas for no behavioral gain.
- If the current send is already doing `token_compression`, keep that send's provider-facing `tools[]` unchanged and defer any refresh to the first post-compression ordinary turn.
- If RBAC removed a tool, execution must reject it immediately even if the provider-facing schema has not yet converged.
- Tool exposure drift changes that round's actual request but does not by itself rotate the caller-side prompt cache family; family changes are reserved for stable-prefix rewrites, lane or model switches, explicit cache-family revision bumps, and other deliberate reset boundaries. Treat `tool_signature_hash` / `actual_tool_schema_hash` as observability fields, not as proof that a new prompt-cache family should exist; cache-side debugging steps 详见 `context-and-cache-troubleshooting.md`「tool schema churn」.

The provider-facing bundle is intentionally minimal:

- Rich tool and skill descriptions stay in the tail runtime contract; provider `tools[]` keeps only the smallest callable schema required for function calling. Repair-required tool/skill exposure is not implemented by churning provider `tools[]`; repair-required lists are runtime-summary-only guidance, and provider bundle stability wins for cache continuity.
- `stage_compaction` must not be used as a shortcut to publish a new provider bundle. If artifacts show a new `actual_tool_schema_hash` together with `history_shrink_reason=stage_compaction`, treat that as a provider-bundle refresh regression.
- Provider-facing schemas are sanitized before transport: descriptive text and unsupported JSON Schema combinators such as `anyOf`, `oneOf`, and `allOf` are stripped or flattened into a simpler supported shape. Runtime-side tool validation remains the authority for argument correctness; do not assume a provider-facing schema still preserves every branch of the richer internal contract. If cache misses correlate with a large `actual_tool_schema_hash` delta, first check whether provider schemas accidentally regressed from this minimal/stable form.
