# G3KU 工具与技能系统说明

本文档解释 G3KU 当前的工具/技能模型，重点面向新接手者说明：

- 工具是如何注册和执行的
- skill 是如何被发现和加载的
- 为什么 Agent 每轮只能看到一部分工具
- candidate tool/skill、callable tool、hydration 分别是什么意思

## 1. 总体设计

G3KU 当前的工具/技能体系已经不是“所有东西都一次性注入给模型”，而是分成了几层：

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
- `frontdoor_catalog_selection.py`
- `frontdoor_query_rewriter.py`

### `main/service/runtime_service.py`

这是工具/技能系统与任务运行时的集成中心。它把：

- 固定内置工具
- 治理/RBAC
- 候选池选择
- hydration 状态
- model 可见工具集

全部接到任务运行时里。

## 3. 四个概念必须分清

### 3.1 fixed builtin tools

指系统预定义、可直接调用的核心工具。

从 `main/service/runtime_service.py` 当前常量看，节点固定内置工具包括：

- `submit_next_stage`
- `submit_final_result`
- `spawn_child_nodes`
- `content_describe`
- `content_open`
- `content_search`
- `exec`
- `load_skill_context`
- `load_tool_context`

其中 `content_describe`、`content_open`、`content_search` 是新的 split content navigation builtin tools，但它们并不是三套彼此独立的读取逻辑。维护上应把它们理解成“同一 content navigation 契约的三个入口”：

- `artifact:` 外部化内容引用仍应优先传 `ref`
- 本地文件仍应优先传绝对 `path`
- `path` 参数本身不接受 `artifact:`；如果把 `artifact:` 塞进 `path`，底层仍会返回 path-mode 错误
- `content_search` 与 `content_open` 在同时收到 `ref` 和 `path` 时，会分别尝试两个目标并返回组合结果；某一侧失败不会覆盖另一侧成功结果
- `content_open` 的 `start_line` / `end_line` 与 `around_line` / `window` 仍是同一目标上的两种选段方式；维护时不要假设组合模式会自动消除这组参数歧义

legacy `content(action=...)` 仍然存在兼容包装，但它与 split tools 最终走的是同一底层 content service；不要假设 split tools 会比 legacy wrapper “更宽松”。

filesystem 家族现在与 content 家族不同：它不再保留可执行的 legacy monolith wrapper。

- `filesystem` 仍然是稳定的 family/tool_id，用于治理、候选工具归类、`g3ku://resource/tool/filesystem` URI，以及 `load_tool_context("filesystem")` 这类家族级上下文加载。
- 真正可执行的 filesystem 变更工具已经全部拆成 concrete executors：`filesystem_write`、`filesystem_edit`、`filesystem_copy`、`filesystem_move`、`filesystem_delete`、`filesystem_propose_patch`。
- 维护者不要再假设存在 `filesystem(action=...)`、`filesystem.search`、`filesystem.open` 之类的可调用兼容入口；它们已经退出运行时 callable tool 集合。
- `filesystem_copy` / `filesystem_move` / `filesystem_delete` 现在都采用批量容器契约：
  - `copy` / `move` 使用 `operations=[{source, destination}, ...]`
  - `delete` 使用 `paths=[...]`
  - 所有路径都必须是绝对路径，并先经过现有 workspace policy
- v1 的目录语义是刻意收紧的：
  - 目录 `copy` / `move` 只允许目标路径不存在
  - 目录 `delete` 必须显式 `recursive=true`

### 3.2 candidate tools

Maintenance note for surfaced fixed builtins:

- Some concrete executors are both resource-backed and fixed builtins, for example CEO `exec` / `memory_search` / `memory_write` and node `content_open` / `content_search` / `exec` / loader tools.
- These executors still remain directly callable through the fixed-builtin path when the runtime exposes them.
- They no longer consume semantic top-k budget. CEO/frontdoor semantic narrowing now removes fixed builtin executors from the retrieval-side visible family/executor set before dense/rerank. Node context selection does the same before applying its 16-tool semantic candidate cap.
- Treat semantic top-k as an extension-tool budget, not as a budget shared with fixed builtins. If an extension tool is missing from the shortlist, debug semantic recall against the filtered non-fixed executor set first.

- `candidate_tool_names` / `candidate_skill_ids` 现在都采用同一语义：`RBAC 可见 ∩ 语义召回命中` 的当前候选集合。
- 如果语义召回不可用，候选集合直接退化为 `RBAC 可见集合`，而不是停止运行。
- 对 agent 来说，candidate 仍然是“可见但默认不可直接调用”的资源；candidate 不会自动进入 callable tool 集合。
- `load_tool_context` / `load_skill_context` 现在也只允许命中当前 canonical candidate 集合，不再允许“RBAC 可见但不在 candidate 中”的旁路加载。
- 对执行/验收节点，skill 候选不再只依赖当前轮的 `node_runtime_tool_contract` user 消息存活；canonical `candidate_skill_ids` 继续落在 runtime frame，`visible_skills` 也会随 frame 一起持久化，供阶段切换、prompt compaction 之后的下一轮 contract 刷新从 frame 恢复。
- 维护时如果看到 `load_skill_context` 报“当前运行时候选技能未包含 ...”，不要只检查模型当轮提示里是否还看得到那条 dynamic contract；先看节点 runtime frame 里的 `candidate_skill_ids` / `visible_skills` 是否仍在，若 frame 还在而动态消息丢了，说明是 contract 重建链路问题，而不是 selector 一开始没选中。
- 节点路径里，语义可用时的 candidate skills / candidate tools 上限都固定为 16；语义不可用时，直接退化为全部 RBAC 可见集合。
- CEO/frontdoor 路径里，语义可用时默认也按 16 取候选：`skill_inventory_top_k=16`、`extension_tool_top_k=16`；这两个值的实际来源是 `tools/memory_runtime/resource.yaml` / `MemoryAssemblyConfig`，而不是前门内的额外 6/8 fallback。
- frontdoor 的 `extension_tool_top_k` 现在只控制“最终候选工具数”，不等于 dense 检索宽度；前门会先用更宽的 `tool_limit` 做 dense/rerank，再在最后一层把 candidate 工具收敛到 `extension_tool_top_k`。

候选工具是“本轮推荐给 agent 的具体工具列表”，但默认不能直接调用。

来源通常是：

- 资源注册表
- RBAC 过滤
- 检索/排序
- 节点上下文选择

它们会出现在 prompt 中，告诉 agent：

- 这些工具和你当前目标相关
- 如果想用，先 `load_tool_context(tool_id="...")`
- 维护上还要区分“canonical candidate state”和“prompt display shape”：
  - runtime frame / frontdoor persistent state 里的权威候选集合仍然是 concrete tool name 列表，例如 `candidate_tool_names=["filesystem_edit"]`
  - 但真正注入给模型阅读的显示层，现在会把 candidate tools 包装成 display-only 的结构化摘要，例如 `candidate_tools=[{tool_id, description}]`
  - 这个显示层只服务于模型理解，不改变 `load_tool_context` 的准入规则，也不替代 canonical candidate name 列表
- 当前前门的语义召回和 fallback 打分也都会优先面向 concrete executor，而不是泛化的 family：
  - 当 query 明显表达文件写入、改写、删除、移动、复制、补丁意图时，query rewrite fallback 与本地候选打分都会优先把 `filesystem_write`、`filesystem_edit`、`filesystem_delete`、`filesystem_move`、`filesystem_copy`、`filesystem_propose_patch` 这类 concrete ids 往前推
  - `exec` 仍可作为固定 builtin 保持可调用，但在这类 mutating intent 下，不应再被当成候选文件变更方案的首选

### 3.3 candidate skills

候选 skill 与 candidate tools 类似，但机制更轻：

- skill 不进入 tool callable 集合
- 需要显式 `load_skill_context(skill_id="...")`
- skill 加载通常是“当前轮立即消费正文”
- 不走 hydration 状态机

### 3.4 hydrated tools

Maintenance note for hydration LRU:

- The hydration LRU is still concrete-tool-only and still defaults to 16 entries.
- Resource-backed fixed builtin executors no longer enter hydration LRU. Loading tool context for a tool that is already fixed-callable may still return contract/help text, but it should not spend a hydration slot or produce a promoted callable entry for the next turn.
- This applies on both sides: node runtime frame hydration (`hydrated_executor_state` / `hydrated_executor_names`) and CEO/frontdoor session-state hydration (`hydrated_tool_names`).
- When debugging a missing hydration promotion, first distinguish between "ordinary extension executor" and "resource-backed fixed builtin". The latter is expected to stay outside LRU by design.

- 节点的 hydration canonical state 在 runtime frame 中：`hydrated_executor_state` / `hydrated_executor_names`。这是节点生命周期级 LRU，会跨多轮、阶段切换、pause/resume、frame restore 保留。
- CEO/frontdoor 的 hydration canonical state 在 session/frontdoor state 中：`RuntimeAgentSession._frontdoor_hydrated_tool_names` 与前门 persistent state 的 `hydrated_tool_names`。这是 session 生命周期级 LRU，会跨 turn 保留，但每轮都按当前 RBAC 可见集合过滤。
- 节点与 CEO/frontdoor 的 hydration LRU 都只接受 concrete tool names；family id 不能进入 canonical hydration state。
- 节点与 CEO/frontdoor 的默认 hydration LRU 上限现在都是 16；如果维护者看到 promoted tool 在第 17 个之后被逐出，优先检查各自运行时对象上的 `_hydrated_tool_limit` 是否被显式改小。

某工具在前一轮成功 `load_tool_context` 后，会进入节点级 hydration 状态。

效果是：

- 下一轮它进入 `model_visible_tool_names`
- 模型可以直接调用它

这是工具系统里最容易被误解的概念：`load_tool_context` 不是执行工具，而是把它提升成后续 turn 的 callable tool。

对 CEO frontdoor 还要额外记住一个现在已经成立的维护语义：

- frontdoor 不再只把 hydration 当成提示词层面的“已读过契约”，而是会把成功 `load_tool_context` 的 concrete tool 写进自己的持久状态。
- 这份 frontdoor hydration 状态会在同一用户 turn 的后续模型轮次里直接并入 callable tool 集合，而不只是继续停留在 candidate tool 列表里。
- frontdoor approval interrupt、session inflight snapshot、paused execution context 也会带上这份 hydrated tool 状态；因此排查“load 成功但下一轮又看不见工具”时，不能只看 candidate tool 提示块，还要看 frontdoor 当前保存的 hydrated tool state。
- frontdoor 现在还会把候选生成诊断同步到 session snapshot 里的 `frontdoor_selection_debug`。这份结构化调试信息至少应包含：当前 raw query、rewrite 后的 `skill_query` / `tool_query`、dense 命中、rerank 结果、tool selection trace，以及最终 callable/candidate/hydrated 集合。
- CEO/frontdoor 现在也不再从 trailing `ToolMessage` / `result_text` 反推 hydration；tool promotion 的权威来源是执行循环里的 `raw_result.ok / raw_result.hydration_targets`。
- 对生产前门来说，这个“执行循环”现在就是显式 frontdoor `StateGraph` 的 `execute_tools` 节点，而不是 `create_agent` middleware 的后处理链。维护者应把 `_graph_execute_tools()` 视为唯一 promotion 入口。
- `_graph_execute_tools()` 现在还必须复用与模型暴露阶段相同的 runtime-visible tool bundle，其中包含运行时注入的 `submit_next_stage`。如果维护者再次在执行环节只按 `state.tool_names` 重建工具映射，就会重新制造“模型能看到 `submit_next_stage`，但执行时报 `tool not available`”的分裂。
- CEO/frontdoor 的 stage gate 现在由 `execute_tools` 真正执行，而不是只靠 prompt 约束。普通工具在无活动阶段或预算耗尽时会直接收到 gate error；`submit_next_stage` 与普通工具混在同一批 tool calls 里时，整批会被拒绝，要求模型先单独完成阶段切换。
- CEO/frontdoor 现在还多了一层更前置的 contract 收紧：当当前没有“有效阶段”时，模型真正看到的 callable tool 列表只剩 `submit_next_stage`。这条规则同样适用于阶段预算已耗尽、必须换阶段的时刻。
- execution / acceptance 节点现在也采用同样的 contract 收紧，而且没有类似 `cron_internal` 的例外：只要没有有效阶段，当前轮模型可见的 callable tool 列表就只剩 `submit_next_stage`。
- 当前保留的特例是 `cron_internal`：为了继续支持 cron 自移除，这类内部轮次不会被收紧到只剩 `submit_next_stage`。
- `submit_next_stage` 的阶段预算现在在 execution / acceptance / CEO-frontdoor 三条路径上统一为 `5-15`；运行时仍允许在预算未耗尽前提前切到下一阶段，因此预算应理解为“本阶段声明的上限窗口”，而不是“必须烧满的最小轮数”。
- 这不影响 candidate 语义。`candidate_tool_names` / `candidate_skill_ids` 仍继续表达“RBAC 可见 ∩ 语义召回命中”的候选集合，只是这些候选在无有效阶段时不会同时出现在 callable tool schemas 里。
- 维护时要区分两份前门工具集合：`tool_names` 继续保存阶段内可恢复的完整 callable pool，而“这一刻真正暴露给模型的 callable tools”要通过前门 callable-tool helper 结合 `frontdoor_stage_state` 再算一次。不要把前者直接当作当前轮的模型可见函数列表。
- approval interrupt 在暂停前会把 `frontdoor_stage_state`、`compression_state`、`semantic_context_state`、`hydrated_tool_names`、`tool_call_payloads` 与 `frontdoor_selection_debug` 一并写进 interrupt payload；恢复后如果这些字段丢失，应按“frontdoor canonical runtime contract / runtime state 损坏”排查。

维护上还要再记住一个和阶段预算相关的边界：

- `load_tool_context` / `load_skill_context` 属于上下文加载型工具调用，会写入 round 历史，但不会增加当前阶段的 `tool_rounds_used`。
- execution/acceptance 节点与 CEO/frontdoor 的阶段记账现在都按同一条规则处理这两类 loader；排查预算耗尽时，不要再把它们当成普通预算轮次。
- 真正的预算结论只看 `rounds[*].budget_counted` 与聚合后的 `tool_rounds_used`，不要根据 transcript 里看到了多少次 loader 调用自行推断。
- 对 CEO/frontdoor 的浏览器展示，还要再区分“运行时记账”和“用户可见步骤”：成功的 `load_tool_context` / `load_skill_context` 仍会保留在底层 round/tool 历史里，但前端 Interaction Flow 会把它们当成上下文加载提示而不是普通执行步骤。
- 当前 CEO UI 合同是：成功 loader 调用改为在输入框上方显示短暂的 live-only notice（尽量带上 `tool_id` / `skill_id`），而不是在 assistant 气泡下方长期保留一个工具步骤；如果 loader 失败，维护者仍应优先检查原始 round/tool 数据与 runtime snapshot，而不是只看 notice 是否出现。

## 4. 一条从上下文到 callable tools 的链路

1. 节点/CEO 进入一次新 turn。
2. `runtime/context` 模块根据 query、历史、治理规则挑出候选工具/技能。
3. prompt builder 把它们以 candidate 列表形式展示给模型。
4. 模型若决定需要某候选工具，先调用 `load_tool_context(tool_id="...")`。
5. 系统记录 hydration 状态。
6. 下一轮 `model_visible_tool_names = fixed builtin tools + hydrated tools`。
7. 只有这一轮，工具才真正成为 callable tool。

这条链路意味着：

- “看得见”不等于“现在就能调用”
- “load_tool_context 成功”也不等于“这一轮立刻可调”
- prompt 里的候选池与实际 callable tool 集合是两套集合

对 frontdoor 还要再补一个边界：

- frontdoor 的 callable tool 集合并不只来自 fixed builtin；它还会把当前 turn 内已经 hydration 的 concrete tools 合并进去。
- frontdoor 的 `candidate_tool_names` 必须排除已经进入 hydrated state 的工具；如果一个工具同时出现在 candidate 列表和 callable tool schemas 里，通常表示状态推进漏了。
- frontdoor 现在还要再区分“candidate display”与“candidate state”：
  - dynamic appendix / turn overlay 会向模型显示结构化 `candidate_tools=[{tool_id, description}]`
  - 但真正驱动去重、hydration 排除与恢复的仍然是 persistent state 中的 `candidate_tool_names`
- 如果维护者在排查 `load_tool_context` 成功后下一轮仍然只会 `exec` / 再次 `load_tool_context`，优先检查 frontdoor persistent state 里的 `hydrated_tool_names`、`tool_names`、`candidate_tool_names` 是否一起更新，而不是只检查 toolskill 内容。
- 如果线上 frontdoor 表现与测试里的 graph helper 一致、却和实际会话不一致，先确认 runner 是否真的走显式 graph checkpoint，而不是怀疑还存在第二条生产 promotion 路径；当前生产路径已经不再以 `ceo_agent_middleware.py` 为权威。
- 对执行节点和检验节点，`callable_tool_names` / `candidate_tools` 现在不应再视为 bootstrap user JSON 的静态字段；它们属于每轮动态 `node_runtime_tool_contract`。
- 对执行节点和检验节点，`node_runtime_tool_contract` 里的 `candidate_tools` 现在也是 display-oriented 的结构化候选摘要；如需恢复 canonical candidate name 列表，优先读取 runtime frame 里的 `candidate_tool_names`
- 对执行节点和检验节点，`visible_skills` / `candidate_skills` 也属于动态 `node_runtime_tool_contract` 的显示合同；但它们的恢复来源现在以 runtime frame 中的 canonical skill state 为准，而不是只靠旧的 dynamic contract user 消息。
- 对执行节点和检验节点，还要再区分“当前轮对模型暴露的 callable 合同”和“内部可恢复的完整 callable pool”：前者在无有效阶段时会被收紧到 `submit_next_stage`，后者保留在 `model_visible_tool_selection_trace.full_callable_tool_names` 里供排障。
- 节点侧的 `runtime frame`、动态 `node_runtime_tool_contract` 与 `runtime-frame-messages:{node_id}` artifact 现在都必须写入同一份收紧后的 callable 列表；如果三者不一致，应按运行时合同分裂排查，而不是先怀疑 prompt 文本。
- 同理，节点侧的 runtime frame 与重建后的 `node_runtime_tool_contract` 也必须对 skill 候选保持一致：`candidate_skill_ids` 与 `visible_skills` 若在 frame 中存在，就不应因为阶段压缩或 active window 裁剪而在下一轮 contract 中无故清空。
- 节点执行层的 `stage_gate_error_for_tool()` 仍然保留，它现在是 schema 收紧之外的兜底防线；如果模型通过恢复态或手工构造仍然尝试普通工具，执行层仍应返回 `no active stage` / `current stage budget is exhausted`。
- 对 CEO/frontdoor，当前 turn 的 callable/candidate tool 合同现在应只存在于 dynamic appendix 和持久状态；不要再从稳定 prompt 前缀或旧 transcript 文本恢复“当前可调用工具”。
- 排查“load 成功但下一轮没调用”时，优先对照 canonical runtime frame / frontdoor state 与 runtime messages snapshot；如果旧 bootstrap 文本和当前 snapshot 冲突，应以当前 snapshot 为准。
- 排查“某个工具为什么没进前门候选集”时，优先看 `frontdoor_selection_debug.semantic_frontdoor` 与 `frontdoor_selection_debug.tool_selection`：
  - `semantic_frontdoor` 负责回答 rewrite 后 query 是什么、dense/rerank 命中了哪些 tool/skill
  - `tool_selection` 负责回答这些命中项为什么最终没有进入 `candidate_tool_names`
- 对 CEO/frontdoor，`frontdoor_stage_state`、`compression_state`、`semantic_context_state` 是受保护运行时状态；工具合同刷新不能覆盖、清空或重置这三份状态。

对 CEO frontdoor 还要额外记住一个优先级边界：

- `ceo_frontdoor.md` 中的 stage-first 协议是高于本轮 skill/tool 暴露提示的稳定协议。
- 因此前门动态提示里出现“如需完整 workflow 正文可调用 `load_skill_context`”或“如需工具契约可调用 `load_tool_context`”，其真实语义都应理解为“仅在活动阶段已经存在后，才进入下一步可执行顺序”。
- 如果当前还没有活动阶段，前门模型即使已经看到了候选 skill 和候选 tool，也应该先走 `submit_next_stage`；否则运行时会在执行时返回 `no active stage` 门控错误。

- restore / recovery 现在只接受 frame 或 CEO/session state 中的 canonical callable/candidate/hydrated/skill 字段；缺失时直接视为“运行时工具合同损坏/缺失”，不再回退 bootstrap 或旧动态文本。

## 5. 当前系统为什么这么设计

从最近的 candidate pool / hydration 重构可以看出，旧模型存在几个问题：

- tool family 和 concrete tool 混在一起，agent 语义不稳定
- 候选池与真实可调用集边界模糊
- skill 与 tool 的加载模型不一致
- family 级别的抽象容易误导 agent

当前方案改成：

- agent 尽量只看到 concrete tool / concrete skill
- family 更多留给 UI、治理、后台管理层
- tool 通过 hydration 进入 callable
- skill 通过 direct load 获取正文，不做 hydration

`filesystem` 是这个边界最典型的例子：

- family `filesystem` 继续稳定存在，但它只承担 family/context 身份，不再是 callable executor。
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

## 8. 关键文件建议阅读顺序

1. `g3ku/agent/tools/registry.py`
2. `g3ku/agent/skills.py`
3. `g3ku/runtime/context/node_context_selection.py`
4. `g3ku/runtime/context/execution_tool_selection.py`
5. `g3ku/runtime/frontdoor/prompt_builder.py`
6. `main/service/runtime_service.py`

## 9. 维护高风险区域

- `main/service/runtime_service.py`
  因为 fixed builtin、candidate、governance、hydration 都在这里汇合。

- `g3ku/runtime/context/`
  小改动就会改变候选池和提示词，直接影响 agent 行为。

- `g3ku/agent/tools/registry.py`
  一旦 runtime context、watchdog 或 schema 处理出错，会影响所有工具执行。
## 10. Duplicate Tool Call Guard

Tool visibility and callable status do not guarantee that the runtime will keep executing the exact same call forever.

- `main/runtime/react_loop.py` tracks repeated ordinary tool signatures inside a node.
- When the model emits the same non-control tool call with the same normalized arguments several turns in a row, the runtime now soft-rejects that turn by returning an error tool message that tells the model to reuse the prior result or change arguments.
- This duplicate-call guard is distinct from the read-only retrieval guard. Read-only calls such as repeated legacy `content(action=open/search/describe)`, split `content_describe` / `content_open` / `content_search`, and `task_progress` still use their own repair-guidance path.
- If you see a node looping on one tool, inspect the transcript/tool messages first. The absence of a fresh tool result may mean the runtime intentionally rejected a duplicate call rather than that the tool executor failed.

## 11. Catalog Freshness And Layered Summaries

Tool/skill semantic retrieval depends on the unified context catalog under the `("catalog", "global")` namespace. Maintainers should keep three separate freshness boundaries in mind:

- The live resource registry is still the source of truth for what exists and what is visible.
- The catalog is a retrieval projection built from those resources.
- `l0` / `l1` are layered catalog summaries, not raw manifest fields.

Current catalog summary rules:

- For skills, the catalog summary source is `display_name + description + SKILL.md body` when the body exists. If `SKILL.md` is missing, the description becomes the body fallback.
- For tools, the catalog is now written per concrete executor, not per tool family. A `filesystem` family with `filesystem_copy` / `filesystem_write` now produces separate records such as `tool:filesystem_copy` and `tool:filesystem_write`.
- Each concrete tool record still carries its family context through tags such as `family:<tool_id>`; this lets targeted refresh/removal continue to work when a whole family changes.
- The tool catalog summary source is the concrete executor's toolskill body when present; if a concrete toolskill body is missing, the runtime falls back to the family description.
- `l0` stays the one-line semantic label; `l1` stays the short structured overview used by retrieval and prompt injection.

This matters because metadata-only edits now count as real catalog changes:

- Changing only `display_name` or `description` is enough to invalidate the existing catalog hash.
- The runtime no longer assumes "body unchanged" means the catalog summary is still current.
- Dense vectors still index `l1` first and fall back to `l0`, so refreshing `l0` / `l1` is what keeps the vector projection aligned.
- Because tools are now indexed by concrete executor, dense/rerank and frontdoor candidate selection no longer depend on “family hit后再按 family 里的 executor 顺序展开”。如果某个 concrete tool 没进候选，优先看它自己的 `tool:<executor_name>` record 是否命中，而不是先看 family 级命中。

## 12. External Disk Edits Without A Watcher

G3KU still does not run a full filesystem watcher for `skills/` and `tools/`.

- `ResourceManager` remains manual/release-triggered at the resource-runtime layer.
- Direct edits on disk are therefore not discovered immediately just because a file changed.

Instead, runtime-facing paths now use a throttled generation check:

- The CEO/frontdoor path and node-context selection path ask `MainRuntimeService` to perform an external resource generation check before semantic catalog selection.
- The check is throttled by `resources.reload.poll_interval_ms`; it is not performed on every internal access without limit.
- The check compares the last known top-level skill/tool tree fingerprints with the current fingerprints.
- When a difference is found, the service refreshes only the changed roots and then performs a targeted catalog sync for the changed `skill_id` / `tool_id` set.

For maintainers, the key implication is:

- "No watcher" does not mean "restart required".
- It means "external disk edits are picked up lazily at the next throttled runtime check, then reconciled through targeted refresh + targeted catalog sync."

## 13. Tool Admin RBAC For Surfaced Tool Families

There is now an explicit maintenance boundary between:

- tool families that appear in Tool Admin (`/api/resources/tools`, Tool management UI), and
- internal fixed tools that never appear there, such as `submit_next_stage`.

For Tool Admin surfaced tool families, RBAC is the highest-priority access contract.

- `actions[].allowed_roles` is now an exact persisted whitelist.
- An empty list means deny-all for that action.
- Refresh, reload, reopen, and store readback must preserve an explicit empty list; maintainers should treat `[]` as real state, not as "missing".
- Tool discovery no longer injects default roles for newly surfaced tool actions. If no persisted RBAC exists yet, the surfaced action starts deny-all until an operator grants roles explicitly.

This changes how maintainers should reason about surfaced tools such as `exec`, `content_*`, `memory_*`, and `task_runtime`:

- If the executor belongs to a surfaced tool family, its model visibility now follows Tool Admin RBAC exactly.
- A surfaced fixed-builtin executor may still be listed in frontdoor or execution fixed-builtin sets, but it only becomes actually visible/callable when the surfaced family/action RBAC allows it.
- When debugging "the tool still appears after I removed all roles", inspect the persisted `tool_families` record and the derived `role_policy_matrix` first. Do not assume there is still any fallback to `ceo` or `execution`.

Internal fixed tools that are not surfaced in Tool Admin remain outside this contract.

- They keep their existing runtime-only visibility rules.
- Do not try to model their access through Tool Admin `allowed_roles`.
- If a behavior question involves stage protocol tools such as `submit_next_stage`, debug the stage/runtime path rather than Tool Admin RBAC.
