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

### 3.2 candidate tools

候选工具是“本轮推荐给 agent 的具体工具列表”，但默认不能直接调用。

来源通常是：

- 资源注册表
- RBAC 过滤
- 检索/排序
- 节点上下文选择

它们会出现在 prompt 中，告诉 agent：

- 这些工具和你当前目标相关
- 如果想用，先 `load_tool_context(tool_id="...")`

### 3.3 candidate skills

候选 skill 与 candidate tools 类似，但机制更轻：

- skill 不进入 tool callable 集合
- 需要显式 `load_skill_context(skill_id="...")`
- skill 加载通常是“当前轮立即消费正文”
- 不走 hydration 状态机

### 3.4 hydrated tools

某工具在前一轮成功 `load_tool_context` 后，会进入节点级 hydration 状态。

效果是：

- 下一轮它进入 `model_visible_tool_names`
- 模型可以直接调用它

这是工具系统里最容易被误解的概念：`load_tool_context` 不是执行工具，而是把它提升成后续 turn 的 callable tool。

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

对 CEO frontdoor 还要额外记住一个优先级边界：

- `ceo_frontdoor.md` 中的 stage-first 协议是高于本轮 skill/tool 暴露提示的稳定协议。
- 因此前门动态提示里出现“如需完整 workflow 正文可调用 `load_skill_context`”或“如需工具契约可调用 `load_tool_context`”，其真实语义都应理解为“仅在活动阶段已经存在后，才进入下一步可执行顺序”。
- 如果当前还没有活动阶段，前门模型即使已经看到了候选 skill 和候选 tool，也应该先走 `submit_next_stage`；否则运行时会在执行时返回 `no active stage` 门控错误。

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
