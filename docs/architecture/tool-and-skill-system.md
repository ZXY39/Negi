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
- `exec`
- `load_skill_context`
- `load_tool_context`

Maintenance note for CEO/frontdoor task lifecycle tools:

- CEO/frontdoor now has two fixed task-lifecycle builtins with different contracts:
  - `create_async_task` for spawning new detached work
  - `task_append_notice` for appending new requirements, constraints, or acceptance expectations to an existing unfinished task in the current session
- `create_async_task` no longer creates work unconditionally. Before `MainRuntimeService` creates a task, it runs a duplicate precheck against the current session's unfinished task pool.
- That duplicate precheck is intentionally hybrid:
  - a deterministic exact-match layer for normalized target text and exact keyword fingerprints
  - an inspection-model review for fuzzy duplicates and "this should update an existing task instead" cases
- The inspection-model review may return `approve_new`, `reject_duplicate`, or `reject_use_append_notice`.
- The tool path now also performs one last deterministic exact-duplicate revalidation immediately before `create_task(...)`. This create-time guard exists specifically to catch stale read views or replay races after the earlier precheck already returned `approve_new`.
- `reject_use_append_notice` means the caller should update an existing unfinished task instead of creating a new detached task. The rejection wording and frontdoor parser now point explicitly at `task_append_notice`.
- Tool-result parsing on the frontdoor side must only treat the explicit success form `创建任务成功task:...` as a verified dispatch. A rejection message may still mention an existing `task:...` id, but that text must not be treated as a newly created task.
- `task_append_notice` success text must stay in the "updated existing task" lane, for example `已向任务 task:xxx 追加通知。`; it must not look like detached task creation and must not create `verified_task_ids` / `route_kind=task_dispatch`.
- The old `continue_task` tool and its continuation / retry-in-place semantics are removed.
- Maintainers should treat any later follow-up on a failed task as ordinary new planning/execution, not as a hidden continuation lane.

Maintenance note for node distribution mode:

- v1 keeps `task_append_notice` CEO-only. Ordinary execution and acceptance nodes do not receive this tool in their builtin set.
- The runtime does introduce a separate internal control tool for node distribution turns: `submit_message_distribution`.
- That tool is not part of ordinary execution visibility. It is only used inside task message distribution mode together with compact prompt `main/prompts/node_message_distribution.md`.
- In distribution mode, the node is temporarily in a control-only lane: inspect the current mailbox message, inspect the current live execution children, and decide which children receive rewritten follow-up messages.
- Distribution mode must not expose ordinary node tools. If maintainers see `exec`, `spawn_child_nodes`, content tools, or other ordinary executors during a compact distribution turn, treat that as a contract regression.

Maintenance note for split content navigation executors:

- `content_describe` / `content_open` / `content_search` no longer belong to the fixed-builtin callable pool for CEO/frontdoor or execution/acceptance nodes.
- They now behave like ordinary candidate tools: visible in the current turn, loadable through `load_tool_context(tool_id="content_*")`, and only callable on a later turn after hydration promotion.
- Because they are no longer fixed builtins, they now consume ordinary candidate-selection and hydration-LRU budget just like other concrete extension executors.

其中 `content_describe`、`content_open`、`content_search` 是新的 split content navigation concrete tools，但它们并不是三套彼此独立的读取逻辑。维护上应把它们理解成“同一 content navigation 契约的三个入口”：

- `artifact:` 外部化内容引用仍应优先传 `ref`
- 本地文件仍应优先传绝对 `path`
- `path` 参数本身不接受 `artifact:`；如果把 `artifact:` 塞进 `path`，底层仍会返回 path-mode 错误
- `content_search` 与 `content_open` 在同时收到 `ref` 和 `path` 时，会分别尝试两个目标并返回组合结果；某一侧失败不会覆盖另一侧成功结果
- `content_open` 的 agent-facing tool contract 现在只暴露 `start_line` / `end_line` 这一组行范围参数，用来降低模型把两套选段方式混传的概率
- 底层 content navigation service 与 legacy `content(action=open)` 仍保留 `around_line` / `window` 支持；如果在维护时看到这组参数继续出现在 REST / service / legacy wrapper 层，不要误判为 split `content_open` callable contract 回退
- `content_open` 暴露给 agent 的行号参数仍是 1-based；非正数 `start_line`、`end_line` 无效
- `content_open` 现在还有一个图片 reopen 契约：当 `path` / `ref` 指向图片时，成功结果不会把图片字节直接塞回普通工具文本，而是返回结构化 payload（如 `content_kind=image`、`multimodal_open_pending=true`、`runtime_image_target`），交给运行时决定是否把视觉内容附带到下一次模型请求
- 这条图片 reopen 契约仍然沿用同一个 `content_open` 工具；不要把它理解成独立的新图片工具。CEO/frontdoor 与 execution / acceptance 节点看到的是同一套 tool result 语义
- 图片 reopen 是否允许，取决于当前运行时/模型绑定是否启用了 `image_multimodal_enabled`，而不是底层 provider 理论上能不能收图。未启用时，`content_open` 应直接返回 `非多模态模型无法打开图片`
- 历史上下文里保留的图片 `path` / `ref` 只是 reopen 入口，不等于模型已经再次看到了图片像素。后续轮次若要重新直接查看图片内容，仍需再次调用 `content_open`

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

Maintenance note for `filesystem_edit`:

- The surfaced/runtime contract now exposes an explicit `mode` field with `text_replace` / `line_range`.
- Provider-facing callable schema should require `mode`, but runtime still tolerates omitted `mode` for backward compatibility and infers the lane from the supplied arguments when possible.
- The edit executor now strips placeholder values from the opposite lane before mode selection when intent is otherwise clear:
  - text-replace calls may auto-carry `start_line=0`, `end_line=0`, `replacement=""`
  - line-range calls may auto-carry `old_text=""`, `new_text=""`
- This cleanup exists specifically to absorb adapter/provider auto-fill noise; maintainers should not treat it as permission for real mixed-mode edits with non-empty fields from both lanes.
- `load_tool_context` / `get_tool_toolskill` agent-facing parameter summaries should now prefer the callable/model-visible schema when a surfaced executor exposes one, rather than blindly replaying the validator-only runtime schema. This is why `filesystem_edit` tool context shows `mode` in the callable contract even though runtime still keeps backward-compatible mode inference.

### 3.2 candidate tools

Maintenance note for surfaced fixed builtins:

- Some concrete executors are both resource-backed and fixed builtins, for example CEO `exec` / `memory_write` / `memory_delete` / `memory_note` and node `exec` / loader tools.
- These executors still remain directly callable through the fixed-builtin path when the runtime exposes them.
- They no longer consume semantic top-k budget. CEO/frontdoor semantic narrowing now removes fixed builtin executors from the retrieval-side visible family/executor set before dense/rerank. Node context selection does the same before applying its 16-tool semantic candidate cap.
- Treat semantic top-k as an extension-tool budget, not as a budget shared with fixed builtins. If an extension tool is missing from the shortlist, debug semantic recall against the filtered non-fixed executor set first.
- `exec` now has a second contract axis besides RBAC: surfaced family `exec_runtime` may carry persisted `metadata.execution_mode`, and the runtime tool contract / `load_tool_context` payload is the authoritative place where the current mode is exposed to models.
- The current supported modes are `governed` and `full_access`. `governed` keeps exec-side guardrails; `full_access` removes exec-side read-only / path / safety checks, but it does not bypass Tool Admin enablement or RBAC.
- Maintainers should also treat the `exec`/`content_open` boundary as part of the agent-facing contract: `exec` is the discovery/probing tool for directory layout, file-name search, and environment inspection, while concrete local file-body evidence should come from `content_open(path=..., start_line, end_line)`.
- This matters especially when `exec` output is long. The current shell payload now reads child-process stdout/stderr through a bounded streaming capture and returns compact previews, not stable `stdout_ref` / `stderr_ref` handles in ordinary agent-facing results.
- The agent-facing payload keeps `head_preview` for the first visible lines and now also carries `tail_preview` plus truncation/captured-byte metadata. Maintainers should treat that as a debugging aid for "the important result was at the end of the command" cases, not as permission to rely on hidden full-output refs or unbounded shell transcripts.
- If a node keeps trying to extract source snippets through repeated `exec`, fix prompt/runtime guidance toward `content_open(path)` rather than teaching the model to expect hidden full-output refs.
- `exec` now also has a platform-text boundary that maintainers should treat as part of the tool contract. Runtime still prefers UTF-8 when decoding child-process stdout/stderr, but on Windows it now falls back to the host-preferred code page before giving up and replacing bytes. If a Windows `exec` result or `head_preview` shows garbled source snippets such as `վ�...` / `�`, inspect child-process output encoding before blaming prompt assembly or frontend rendering.
- For Windows child Python commands specifically, `exec` now seeds `PYTHONIOENCODING=utf-8` into the subprocess environment. This is intentionally narrow: it stabilizes Python traceback / `print()` output without changing Tool Admin RBAC or `exec_runtime.execution_mode`.
- The same output-decoding helper is now also used by subprocess-backed validation/browser lanes such as filesystem validation commands and `agent_browser`. If one Windows tool path shows readable Chinese output while another still shows mojibake, first check whether that path is using the shared subprocess-text helper before debugging the individual tool payload.
- `content_describe` / `content_open` / `content_search` no longer belong to this fixed-builtin category. If they disappear from a fresh-turn callable list, debug candidate selection and hydration state before suspecting fixed-builtin exposure.

Maintenance note for the memory tool family:

- `memory_search` is removed from the agent-facing contract.
- CEO/frontdoor now receives committed long-term memory through the injected `MEMORY.md` snapshot, not through a retrieval tool call. The surfaced snapshot is display-only: it strips memory ids and date/source headers and keeps only the remembered text blocks separated by `---`.
- `memory_note(ref)` is the only on-demand detailed-memory loader.
- Node execution and acceptance paths no longer inject extra memory retrieval blocks; they only use the catalog bridge for tool/skill narrowing.
- That catalog bridge is now catalog-only: it owns context-record storage and dense/sparse tool-skill narrowing data, but it no longer delegates through the old `rag_memory` long-term memory runtime.
- `memory_write` and `memory_delete` are queue-submit tools. They ask the memory runtime to process a later batch; they do not synchronously rewrite committed memory during the current turn.
- `memory_delete(content=...)` now takes a natural-language description of the remembered content to forget; surfaced agents no longer pass memory ids.
- The actual rewrite/delete decision is now delegated to a dedicated internal memory agent with a restricted tool surface. That internal agent is not part of the normal agent-facing tool catalog and should not be debugged as if it were a surfaced Tool Admin family. It resolves delete descriptions to concrete SQLite ids and may report `inspired_memory_ids` for rows that materially affected the batch.

- `candidate_tool_names` / `candidate_skill_ids` 现在都采用同一语义：`RBAC 可见 ∩ 语义召回命中` 的当前候选集合。
- 如果语义召回不可用，候选集合直接退化为 `RBAC 可见集合`，而不是停止运行。
- 对 agent 来说，candidate 仍然是“可见但默认不可直接调用”的资源；candidate 不会自动进入 callable tool 集合。
- `load_tool_context(tool_id="...")` 的精确工具加载现在不再是 candidate-only：当前 canonical `candidate_tool_names` 中的 concrete tool 与当前 `rbac_visible_tool_names` 中仍然 surfaced 的 concrete tool 都可以读取 toolskill / 参数说明。
- 这条放宽只适用于精确 `tool_id` load。`load_tool_context(search_query=...)` 仍然保持原来的可见工具搜索路径，不应被理解成“枚举所有 RBAC 可见工具”的新 API。
- `load_skill_context` 仍然只允许命中当前 canonical skill candidate 集合；这次补丁没有放宽 skill loader gate。
- 但只有普通 candidate tool load 才会进入 hydration/promotion。对已经 callable、已经 hydrated、fixed builtin，或任何当前仅 RBAC 可见但不在 candidate 里的 direct-load lane，`load_tool_context` 都只是 read-only toolskill 加载，不会推进 hydration，也不会占用 hydration LRU。
- 但“普通 candidate”不再覆盖 repair-required 资源：
  - repair-required tools 会从 agent-facing `candidate_tools` / `callable_tools` / `hydrated_tools` 中剥离，单独进入 `repair_required_tools`
  - repair-required skills 会从 agent-facing `candidate_skills` 中剥离，单独进入 `repair_required_skills`
  - 这两个 repair 列表只影响 agent-facing runtime contract；它们不等于 provider-facing `tools[]` 变化
- 对 CEO/frontdoor，`candidate_tool_names` / `candidate_skill_ids` 属于 internal canonical state；真正暴露给模型的当前轮显示合同只保留一份 `frontdoor_runtime_tool_contract`。
- 这还意味着 frontdoor 的旧轮 candidate/tool/skill catalog 不应进入 durable history。后续轮次只能继承真实工具调用轨迹与上下文，而不是再次看到上一轮的候选列表。
- 对执行/验收节点，skill 候选不再只依赖当前轮的 `node_runtime_tool_contract` user 消息存活；canonical `candidate_skill_ids` 继续落在 runtime frame，`candidate_skill_items` 也会随 frame 一起持久化，供阶段切换、prompt compaction 之后的下一轮 contract 刷新从 frame 恢复。
- 节点侧现在还会把 `runtime_service._node_context_selection_inputs()` 当轮拿到的 contract-visible skill 快照单独记成 `contract_visible_skill_ids`，并随 runtime frame 与 `runtime-frame-messages:{node_id}` artifact 一起落盘。它不是新的 candidate truth source，而是专门给排障用的输入层证据。
- 节点侧还会把更下一层的 skill 可见性切面一起落盘为 `skill_visibility_diagnostics`。这份诊断来自 `list_contract_visible_skill_resources(...)` 内部使用的 live resource registry / role / policy 判断，至少保留 `registry_skill_ids` 和逐 skill 的 `{enabled, available, allowed_for_actor_role, policy_effect, included_in_contract_visible}`。
- 对执行/验收节点，fresh skill 合同现在是明确的 first-turn truth source：`_enrich_node_messages()` 注入的 `node_runtime_tool_contract` 若已经带有 `candidate_skills` / `contract_visible_skill_ids` / `skill_visibility_diagnostics`，后续 `react_loop` 不应再让初始化默认空 frame 把这些字段覆写成空。
- 这意味着节点 runtime frame 现在分成“bootstrap 占位 frame”和“authoritative skill-contract frame”两类：默认空 frame 只负责占位与 phase 跟踪，不再被视为 skill candidate truth source；只有已经携带 skill 合同或 skill 诊断的 frame，才能在后续 contract rebuild 中优先于 fresh payload。
- 同一 turn 内，如果 `_prepare_messages()` 裁掉了旧的 `node_runtime_tool_contract` 尾部消息，NodeRunner 现在会把 fresh skill 合同摘要沿 runtime context 继续传给 `react_loop`。因此后续 round 的 contract rebuild 仍应保持最初那份 `candidate_skill_ids` / `contract_visible_skill_ids` / `skill_visibility_diagnostics`，直到 runtime frame 自己也变成 authoritative 为止。
- 节点侧的内存 `node context selection cache` 与 `persisted_frame_restore` 现在都带有一层 live-visibility freshness gate。运行时在复用旧 selection 之前，会重新对照当前 `session_key`、`actor_role`、`visible_tool_names`、`contract_visible_skill_ids`，以及 `skill_visibility_diagnostics.registry_skill_ids`。
- 只要这份 live snapshot 与旧 cache / 旧 frame 漂移，运行时就会丢弃旧 selection，重新跑 `_node_context_selection_inputs()` 与 `build_node_context_selection(...)`，而不是继续沿用旧的 `candidate_skill_ids` / `candidate_tool_names`。
- 这条规则是为了挡住“外部 resource/governance refresh 已经把 skill 或 tool visibility 改了，但当前节点仍长期沿用旧 cache / 旧 frame”这种回归，尤其是“首轮 skill 可见集为空，后续轮次一直空”的情况。
- 维护时如果看到 `load_skill_context` 报“当前运行时候选技能未包含 ...”，不要只检查模型当轮提示里是否还看得到那条 dynamic contract；先看节点 runtime frame 里的 `candidate_skill_ids` / `candidate_skill_items` 是否仍在，若 frame 还在而动态消息丢了，说明是 contract 重建链路问题，而不是 selector 一开始没选中。
- 如果排查的是“为什么这轮 `candidate_skills` 直接是空的”，先对照 `contract_visible_skill_ids` 与 `candidate_skill_ids`：
  - 前者也为空，优先怀疑 runtime-service 输入层的可见 skill 集合本来就空了
  - 前者非空但后者为空，优先怀疑 selector / canonical candidate 生成链路，而不是把 skill 当成了 callable tool 或 prompt 文本丢失
- 如果 `contract_visible_skill_ids` 与当前 live visibility 已经非空，但当前节点仍在复用更早一轮的空 `candidate_skill_ids` / 空 `contract_visible_skill_ids`，优先怀疑 selection freshness gate / cache invalidation，而不是 prompt wording。
- 如果还要继续往下拆，改看 `skill_visibility_diagnostics.entries`：
  - `registry_skill_ids` 都缺少目标 skill：优先怀疑 live `resource_registry` 没加载到该 skill
  - entry 存在但 `allowed_for_actor_role=false`：优先怀疑 `allowed_roles`
  - entry 存在且 `allowed_for_actor_role=true`，但 `policy_effect` 不是 `allow` 且 `included_in_contract_visible=false`：优先怀疑治理策略/role policy
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
  - CEO/frontdoor 真正注入给模型阅读的显示层，现在只通过当前轮 `frontdoor_runtime_tool_contract` 携带 display-only 的结构化摘要，例如 `candidate_tools=[{tool_id, description}]`
  - 这个显示层只服务于模型理解，不改变 `load_tool_context` 的准入规则，也不替代 canonical candidate name 列表
- 对 CEO/frontdoor，维护者现在还要再区分 `candidate_tool_names` 与 `candidate_tool_items`：
  - `candidate_tool_names` 是运行时去重、hydration 排除、恢复和 gate 判断用的 canonical name list
  - `candidate_tool_items` 是当前轮显示层缓存，通常是 `{tool_id, description}`，用于保证 contract rebuild / refresh 之后仍能保留描述文本
  - 这两者都可能存在于 runtime state，但 agent 只应看到结构化 `candidate_tools`，不应同时看到 `candidate_tool_names`
  - `candidate_tool_items` 只是 `candidate_tool_names` 的显示投影，不是第二份权威候选集。若 canonical `candidate_tool_names=[]`，则 agent-facing `candidate_tools` 也必须为空；不要再从旧 contract、旧 `candidate_tool_items` 或旧动态消息把已失效候选补回 prompt
- 当前前门的语义召回和 fallback 打分也都会优先面向 concrete executor，而不是泛化的 family：
  - 当 query 明显表达文件写入、改写、删除、移动、复制、补丁意图时，query rewrite fallback 与本地候选打分都会优先把 `filesystem_write`、`filesystem_edit`、`filesystem_delete`、`filesystem_move`、`filesystem_copy`、`filesystem_propose_patch` 这类 concrete ids 往前推
  - `exec` 仍可作为固定 builtin 保持可调用，但在这类 mutating intent 下，不应再被当成候选文件变更方案的首选

### 3.3 candidate skills

候选 skill 与 candidate tools 类似，但机制更轻：

- skill 不进入 tool callable 集合
- 需要显式 `load_skill_context(skill_id="...")`
- skill 加载通常是“当前轮立即消费正文”
- 不走 hydration 状态机
- 对 CEO/frontdoor，最新的 `frontdoor_runtime_tool_contract` 摘要现在也会把 `candidate_skills` 明确标成“可通过 `load_skill_context` 读取正文”的候选，避免模型把它们误读成需要安装/水合的候选工具
- repair-required skill 现在还有一个更强的门控：
  - 它仍可作为“待修复资源”出现在 agent-facing `repair_required_skills` 中
  - 但在修复完成前，`load_skill_context(...)` / `load_skill_context_v2(...)` 不再返回正文，而是直接返回 repair-required 错误与修复指引
  - 维护者如果看到模型“知道这个 skill 存在却无法 load”，应先检查 skill 资源本身的 `available` / warnings / errors，而不是先怀疑 selector 没选中

### 3.4 hydrated tools

Maintenance note for hydration LRU:

- The hydration LRU is still concrete-tool-only and still defaults to 16 entries.
- Resource-backed fixed builtin executors no longer enter hydration LRU. Loading tool context for a tool that is already fixed-callable may still return contract/help text, but it should not spend a hydration slot or produce a promoted callable entry for the next turn.
- This applies on both sides: node runtime frame hydration (`hydrated_executor_state` / `hydrated_executor_names`) and CEO/frontdoor session-state hydration (`hydrated_tool_names`).
- Successful `load_tool_context` payloads now also carry an internal `tool_context_fingerprint`. The runtime uses it only to judge whether the current toolskill contract has changed enough to justify rereading; it is not a provider-facing schema field.
- For callable / hydrated / fixed-builtin direct-loads, node runtime and CEO/frontdoor both scan current uncompressed inline history for the latest successful `load_tool_context` result for the same resolved `tool_id`. If that inline result has the same `tool_context_fingerprint`, the runtime soft-rejects the reread and tells the model to reuse the existing toolskill.
- If the fingerprint changed, or the old direct-load result was already compacted away by `token_compression` / `stage_compaction`, rereading is allowed again.
- When debugging a missing hydration promotion, first distinguish between "ordinary extension executor" and "resource-backed fixed builtin". The latter is expected to stay outside LRU by design.
- `content_describe` / `content_open` / `content_search` are now in the first category, not the second: a successful `load_tool_context(tool_id="content_*")` should spend an ordinary hydration slot and promote that concrete tool on the next turn.

Maintenance note for parameter-error guidance:

- Tool parameter validation errors now share one maintenance contract across `ToolRegistry`, CEO/frontdoor direct-tool execution, and node `ReActToolLoop`.
- When runtime rejects parameters because `validate_params(...)` returned errors, because `validate_params(...)` itself crashed, or because tool execution raised `ValueError` / `TypeError`, the returned error text should preserve the original error and append a repair hint that points back to `load_tool_context(tool_id="<tool_name>")`.
- Do not extend this lane to unrelated runtime failures. Permission errors, path-policy errors, timeout-stop errors, watchdog stops, pause/cancel signals, and ordinary `RuntimeError` should keep their original semantics instead of being mislabeled as parameter errors.
- Separately from that parameter-guidance contract, runtime status classification now treats any structured tool result with top-level `{"ok": false, ...}` as an error-lane tool result across `ToolRegistry`, CEO/frontdoor, and node execution.
- This status rule is intentionally broader than the parameter-guidance rule. It exists so embedded tools that encode failures as JSON payloads still enter the runtime error lane, even when they do not raise Python exceptions outward.

- 节点的 hydration canonical state 在 runtime frame 中：`hydrated_executor_state` / `hydrated_executor_names`。这是节点生命周期级 LRU，会跨多轮、阶段切换、pause/resume、frame restore 保留。
- CEO/frontdoor 的 hydration canonical state 在 session/frontdoor state 中：`RuntimeAgentSession._frontdoor_hydrated_tool_names` 与前门 persistent state 的 `hydrated_tool_names`。这是 session 生命周期级 LRU，会跨 turn 保留，但每轮都按当前 RBAC 可见集合过滤。
- 节点与 CEO/frontdoor 的 hydration LRU 都只接受 concrete tool names；family id 不能进入 canonical hydration state。
- 节点与 CEO/frontdoor 的默认 hydration LRU 上限现在都是 16；如果维护者看到 promoted tool 在第 17 个之后被逐出，优先检查各自运行时对象上的 `_hydrated_tool_limit` 是否被显式改小。
- 对 CEO/frontdoor，RBAC 可见集合本身仍会保留在 internal runtime state 里，供过滤 hydration 与恢复链路使用；但 `rbac_visible_tool_names` / `rbac_visible_skill_ids` 不再属于 agent-facing prompt contract。
- 对执行/验收节点，同样的规则也成立：`rbac_visible_tool_names` / `rbac_visible_skill_ids`、`hydrated_executor_names`、`lightweight_tool_ids`、`model_visible_tool_selection_trace` 都属于本地 runtime state，不再进入 agent-facing `node_runtime_tool_contract`。

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
- CEO/frontdoor 的 stage gate 现在由 `execute_tools` 真正执行，而不是只靠 prompt 约束。普通工具在无活动阶段或预算耗尽时会直接收到 gate error；如果同一批 tool calls 同时包含 `submit_next_stage` 和普通工具，runtime 会先执行 `submit_next_stage`，再把同批普通工具当作新阶段的第一批调用处理，并在该新阶段上记账预算。
- CEO/frontdoor 现在还多了一层更前置的 contract 收紧：当当前没有“有效阶段”时，agent-facing `frontdoor_runtime_tool_contract.callable_tool_names` 会收紧到只剩 `submit_next_stage`。这条规则同样适用于阶段预算已耗尽、必须换阶段的时刻。
- 但这条前门规则不再等价于“provider-facing callable tool schemas 也只剩 `submit_next_stage`”。为了保持 prompt cache 前缀稳定，provider body 里的 `tools` 继续使用稳定的 runtime-visible tool bundle；真正的阶段控制回到动态合同和 `execute_tools` stage gate。
- 这里的“稳定”现在不再依赖 active/pending provider-bundle 状态机：普通轮次直接按当前 RBAC-visible concrete tools 条件刷新 `provider_tool_names`；如果当前 send 已经在做 `token_compression`，这一轮实际发给 provider 的 bundle 必须继续沿用压缩前已持久化的 bundle。
- execution / acceptance 节点现在也采用同样的 contract 收紧，而且没有类似 `cron_internal` 的例外：只要没有有效阶段，当前轮模型可见的 callable tool 列表就只剩 `submit_next_stage`。
- 但这不意味着 execution / acceptance 节点必须把 `submit_next_stage` 单独拆成一轮。若同一批 tool calls 里同时出现 `submit_next_stage` 和普通工具，执行循环会先推进阶段，再让这些普通工具作为新阶段首轮执行；若阶段切换失败，同批剩余普通工具会被批内阻断，而不会回退到旧阶段继续执行。
- 当前保留的内部-turn 特例是 `heartbeat_internal` 与 `cron_internal`：只要当前 session 已有权威的 frontdoor baseline 与前序 contract state，这类内部轮次都不会被收紧到只剩 `submit_next_stage`，也不会重新跑 candidate/hydration/skill selection，而是直接继承上一轮的 callable / candidate / hydrated / provider-tool / visible-skill 状态。
- 这意味着 `heartbeat_internal` / `cron_internal` 都不再是独立的“special lane”。从 agent 视角看，它们就是在上一轮 frontdoor contract 上追加隐藏内部提示后的普通 CEO/frontdoor 轮次；模型可以直接输出，也可以立即开始阶段并调用已继承的普通工具。
- 如果当前 session 还没有权威 frontdoor baseline，这条继承规则不会强行触发；内部轮次会回退到普通 CEO/frontdoor exposure assembly，同时继续遵守 provider-facing active/pending tool exposure 状态机。
- `cron_internal` 的真正特例只剩两点：
  - reminder 正文不再伪装成 `user` 事件消息，而是隐藏的结构化 `system` 事件块
  - cron 任务生命周期不再依赖模型自己调用 `cron(action="remove")` 或自然语言 `stop_condition` 推断停止；停止与删除由 scheduler 侧的 `payload.max_runs` / `state.delivered_runs` 计数器负责
- 因此，若维护者排查“cron 到点了但没有创建异步任务 / 没有查询任务 / 只会重复谈 cron 自己”，应优先检查 frontdoor tool exposure 是否被错误缩成了 `cron`，而不是先怀疑 scheduler 没触发。
- 当前 cron 工具合同应该理解为“结构化提醒”而不是“自然语言循环任务”：
  - `message` = 给未来 agent 的提醒动作
  - `max_runs` = 成功送达上限；省略时默认为 1
  - `at` = 只接受创建时仍在未来的单次触发时间；若真正执行 `add_job()` 时该时间已过，运行时会拒绝创建并提示 `任务定时已过期，当前时间为<service-local time>，请立即执行或视情况废弃而不要创建过期任务`
  - `stop_condition` = 兼容字段，不再参与 runtime 停止判断
- 因为停止逻辑已经转到 cron service 的计数器上，维护者排查“为什么没有自动停止”时应先看 cron store 里的 `payload.max_runs` / `state.delivered_runs`，而不是先看模型回复里有没有提到“已发送 N 次”。
- `submit_next_stage` 的阶段预算现在在 execution / acceptance / CEO-frontdoor 三条路径上统一为 `1-10`；运行时仍允许在预算未耗尽前提前切到下一阶段，因此预算应理解为“本阶段声明的上限窗口”，而不是“必须烧满的最小轮数”。
- 这不影响 candidate 语义。`candidate_tool_names` / `candidate_skill_ids` 仍继续表达“RBAC 可见 ∩ 语义召回命中”的候选集合，只是这些候选在无有效阶段时不会同时出现在 agent-facing callable contract 里。
- 维护时要区分两份前门工具集合：`tool_names` 继续保存阶段内可恢复的完整 callable pool，而“这一刻 agent-facing 合同里暴露给模型的 callable tools”要通过前门 callable-tool helper 结合 `frontdoor_stage_state` 再算一次。不要把前者直接当作当前轮的模型可见函数列表，也不要把 agent-facing callable 收紧误解成 provider body 里的 `tools` 已同步收紧。
- approval interrupt 在暂停前会把 `frontdoor_stage_state`、`compression_state`、`hydrated_tool_names`、`tool_call_payloads` 与 `frontdoor_selection_debug` 一并写进 interrupt payload；恢复后如果这些字段丢失，应按“frontdoor canonical runtime contract / runtime state 损坏”排查。

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
5. 只有当该工具仍属于 canonical candidate，且解析到的是普通 concrete extension executor 时，系统才记录 hydration 状态。
6. 下一轮 `model_visible_tool_names = fixed builtin tools + hydrated tools`。
7. 只有这一轮，工具才真正成为 callable tool。

这条链路意味着：

- “看得见”不等于“现在就能调用”
- “load_tool_context 成功”也不等于“这一轮立刻可调”
- “RBAC 可见且 surfaced”也不等于“必然会 promotion”；它也可能只是一个 read-only toolskill load
- prompt 里的候选池与实际 callable tool 集合是两套集合

对 frontdoor 还要再补一个边界：

- frontdoor 的 callable tool 集合并不只来自 fixed builtin；它还会把当前 turn 内已经 hydration 的 concrete tools 合并进去。
- frontdoor 的 `candidate_tool_names` 必须排除已经进入 hydrated state 的工具；如果一个工具同时出现在 candidate 列表和 callable tool schemas 里，通常表示状态推进漏了。
- frontdoor 现在还要再区分“candidate display”与“candidate state”：
  - agent-facing 当前轮显示合同只剩一份 `frontdoor_runtime_tool_contract`；它向模型显示结构化 `candidate_tools=[{tool_id, description}]`
  - 但真正驱动去重、hydration 排除与恢复的仍然是 persistent state 中的 `candidate_tool_names`
- 如果维护者在排查 `load_tool_context` 成功后下一轮仍然只会 `exec` / 再次 `load_tool_context`，优先检查 frontdoor persistent state 里的 `hydrated_tool_names`、`tool_names`、`candidate_tool_names` 是否一起更新，而不是只检查 toolskill 内容。
- 如果维护者看到模型反复对同一个 callable / hydrated / fixed-builtin 工具再次调用 `load_tool_context`，先检查当前消息历史里是否还保留着同一 resolved `tool_id` 且 `tool_context_fingerprint` 未变化的未压缩 inline 结果；这是预期中的 duplicate direct-load 软拦截，不是 tool registry 丢失。
- 如果线上 frontdoor 表现与测试里的 graph helper 一致、却和实际会话不一致，先确认 runner 是否真的走显式 graph checkpoint，而不是怀疑还存在第二条生产 promotion 路径；当前生产路径已经不再以 `ceo_agent_middleware.py` 为权威。
- 对执行节点和检验节点，`callable_tool_names` / `candidate_tools` / `candidate_skills` 现在都不应再视为 bootstrap user JSON 的静态字段；它们属于每轮动态 `node_runtime_tool_contract`。
- 对执行节点和检验节点，稳定 bootstrap user JSON 只保留稳定节点上下文；`execution_stage` 不再写进 bootstrap，而是只由当前轮尾部的 `node_runtime_tool_contract` 承载。
- 对执行节点和检验节点，和运行时合同同轮出现的 overlay / repair overlay 也只允许作为 request-tail 临时消息追加；它们不应再原地改写 bootstrap user 或任何更早的持久化消息，否则会破坏稳定前缀与 prompt cache 命中。
- 对执行节点和检验节点，`node_runtime_tool_contract` 里的 `candidate_tools` 现在是 display-oriented 的结构化候选摘要；如需恢复 canonical candidate name 列表，优先读取 runtime frame 里的 `candidate_tool_names` / `candidate_tool_items`。
- 对执行节点和检验节点，`node_runtime_tool_contract` 里的 `candidate_skills` 现在也是最小结构化摘要：`[{skill_id, description}]`。如需恢复 canonical skill state，优先读取 runtime frame 里的 `candidate_skill_ids` / `candidate_skill_items`。
- 对执行节点和检验节点，`candidate_tool_names` / `candidate_skill_ids` 现在是唯一 gate truth source；`candidate_tool_items` / `candidate_skill_items` 只是描述文本投影。只要 canonical name/id 列表已经为空，重建后的 `node_runtime_tool_contract` 也必须把 `candidate_tools` / `candidate_skills` 渲染为空，不能再从旧 contract item 列表或旧 frame item 缓存复活失效候选。
- For execution / acceptance restore specifically, restored `selected_tool_names` must not collapse to "callable only". It should keep the union of restored callable tools and restored candidate concrete tools so the next node tool-provider pass can hand both sets back to schema selection.
- The node tool provider must also expose restored candidate executors as visible tools even when only `callable_tool_names` are immediately callable. Otherwise a successful hydration promotion can accidentally collapse the next-round candidate pool to empty, and later `load_tool_context(tool_id="content_open")` / `load_tool_context(tool_id="filesystem_write")` calls will fail against an artificially shrunken candidate set.
- 对执行节点和检验节点，还要再区分“当前轮对模型暴露的 callable 合同”和“内部可恢复的完整 callable pool”：前者在无有效阶段时会被收紧到 `submit_next_stage`，后者只保留在本地 `model_visible_tool_selection_trace.full_callable_tool_names` 里供排障。
- 节点侧的 `runtime frame`、动态 `node_runtime_tool_contract` 与 `runtime-frame-messages:{node_id}` artifact 现在都必须写入同一份收紧后的 callable 列表；如果三者不一致，应按运行时合同分裂排查，而不是先怀疑 prompt 文本。
- 同理，节点侧的 runtime frame 与重建后的 `node_runtime_tool_contract` 也必须对 skill 候选保持一致：`candidate_skill_ids` 与 `candidate_skill_items` 若在 frame 中存在，就不应因为阶段压缩或 active window 裁剪而在下一轮 contract 中无故清空。
- 但这条“从 frame 恢复”规则现在有一个前提：frame 本身必须已经是 authoritative skill-contract frame。若当前 frame 只是初始化时写下的默认空 skill 字段，而本轮 message history / runtime context 已经携带 fresh skill 合同，contract rebuild 必须优先保留 fresh skill 合同，而不是把空 frame 当成真相源。
- 节点执行层的 `stage_gate_error_for_tool()` 仍然保留，它现在是 schema 收紧之外的兜底防线；如果模型通过恢复态或手工构造仍然尝试普通工具，执行层仍应返回 `no active stage` / `current stage budget is exhausted`。
- 对 CEO/frontdoor，当前 turn 的 callable/candidate tool 合同现在应只存在于 dynamic appendix 和持久状态；不要再从稳定 prompt 前缀或旧 transcript 文本恢复“当前可调用工具”。
- 对 CEO/frontdoor，turn overlay / repair overlay 也属于 dynamic appendix 一侧的尾部临时内容；它们只能尾部追加，不能回写进已有 stable/request user 消息，否则会把原本 append-only 的稳定前缀变成每轮不同的文本。
- 对 CEO/frontdoor 主链路，`dynamic_appendix_messages` 的持久化形态现在也进一步收紧为“只保留当前 `frontdoor_runtime_tool_contract`”。像 retrieved context 这类当次 request body 内容若需要跨同一 turn 的后续模型轮次保留，应留在 `messages` / stage state / canonical context 的重建链路里，而不是重新作为第二份 appendix 尾插。
- 因此排查 CEO/frontdoor cache drop 时，要区分两件事：`messages` 中保存的是“下一次重建 request body 的基线”，而 `dynamic_appendix_messages` 只是“当前轮唯一尾部合同”。如果两边都出现完整 catalog 或 retrieved context 副本，说明 runtime contract 已经重复注入。
- 为了修复同一 turn 内 contract 被新工具轨迹不断顶走的问题，CEO/frontdoor 的活动中 request 现在允许暂时保留更早轮次的 contract snapshots，并在最新一轮末尾再追加新的 authoritative contract。换句话说，同一 turn 的 provider-facing request 里可以有多条 contract，但只有最后一条有效。
- 这条规则只适用于活动中的 turn request / actual request JSON；turn 结束后写回 durable transcript 时，旧的 contract snapshots 仍然必须全部剥离。
- 对 CEO/frontdoor，当前轮 contract 的推荐排障顺序也变了：
  - 先看 request 尾部那条唯一的 `frontdoor_runtime_tool_contract`
  - 再看 internal state 中的 `tool_names` / `candidate_tool_names` / `candidate_tool_items` / `hydrated_tool_names`
  - 不要再把稳定 prompt 前缀、旧 overlay 文本或旧 transcript 里的 tool/skill 名单当成当前轮权威合同
- 排查“load 成功但下一轮没调用”时，优先对照 canonical runtime frame / frontdoor state 与 runtime messages snapshot；如果旧 bootstrap 文本和当前 snapshot 冲突，应以当前 snapshot 为准。
- 排查“某个工具为什么没进前门候选集”时，优先看 `frontdoor_selection_debug.semantic_frontdoor` 与 `frontdoor_selection_debug.tool_selection`：
  - `semantic_frontdoor` 负责回答 rewrite 后 query 是什么、dense/rerank 命中了哪些 tool/skill
  - `tool_selection` 负责回答这些命中项为什么最终没有进入 `candidate_tool_names`
- 对 CEO/frontdoor，`frontdoor_stage_state`、`compression_state` 与 `hydrated_tool_names` 是受保护运行时状态；工具合同刷新不能覆盖、清空或重置这些字段。

对 CEO frontdoor 还要额外记住一个优先级边界：

- `ceo_frontdoor.md` 中的 stage-first 协议是高于本轮 skill/tool 暴露提示的稳定协议。
- 因此前门动态提示里出现“如需完整 workflow 正文可调用 `load_skill_context`”或“如需工具契约可调用 `load_tool_context`”，其真实语义都应理解为“仅在活动阶段已经存在后，才进入下一步可执行顺序”。
- 如果当前还没有活动阶段，前门模型即使已经看到了候选 skill 和候选 tool，也应该先走 `submit_next_stage`；否则运行时会在执行时返回 `no active stage` 门控错误。

- restore / recovery 现在只接受 frame 或 CEO/session state 中的 canonical callable/candidate/hydrated/skill 字段；缺失时直接视为“运行时工具合同损坏/缺失”，不再回退 bootstrap 或旧动态文本。

## CEO Frontdoor Request Body Baseline

- CEO/frontdoor now persists a separate session-owned request-body baseline as `frontdoor_request_body_messages`.
- This baseline is intentionally body-only: when it is written back to session state, dynamic `frontdoor_runtime_tool_contract` messages are stripped out so the next round can rebuild one fresh authoritative tail contract.
- Fresh visible CEO/frontdoor turns now consume that baseline through a direct continuation path instead of feeding it back into the ordinary checkpoint-history selector. If maintainers see a fresh visible turn rebuilding from transcript/stage replay before any explicit shrink reason is recorded, that is now a frontdoor continuity bug.
- Direct-reply turn finalization must preserve this authoritative body baseline and append the final assistant reply before the session sync runs. Otherwise the next visible turn will inherit a shorter transcript projection even though the prior provider-facing body was longer.
- Maintainers should therefore distinguish three different things when debugging frontdoor prompt continuity:
  - `frontdoor_request_body_messages` = the next-round body baseline
  - `dynamic_appendix_messages` = the fresh tail contract to append for the current round
  - actual request JSON = the exact provider-facing payload for a specific `call_model` round
- The paired `frontdoor_history_shrink_reason` field is now the only accepted explanation for a shorter next-round body baseline.
- Only `token_compression` and `stage_compaction` are valid shrink reasons. If the next-round body baseline is shorter without one of those reasons, treat that as runtime context loss rather than as normal contract rebuilding.

## Cache Diagnostics Versus Tool Drift

- Callable/candidate/hydrated changes still affect the actual provider request and may change the observed tool schema hash for that round.
- Those same changes no longer automatically rotate the caller-side prompt cache family. Family changes are reserved for stable-prefix rewrites, lane or model switches, explicit cache-family revision bumps, and other deliberate reset boundaries.
- For CEO/frontdoor and node execution alike, treat `tool_signature_hash` or `actual_tool_schema_hash` as observability fields, not as proof that a new prompt-cache family should exist.
- When debugging a cache drop, first separate "the family changed" from "the actual request changed". Tool exposure drift belongs to the second category unless it also rewrites the stable prefix.

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

### Exec Runtime Mode Contract

- `exec_runtime` is currently the only surfaced tool family with an extra persisted mode field in Tool Admin: `tool_families.metadata.execution_mode`.
- The resource manifest may still provide a default `settings.execution_mode`, but the persisted family metadata is the runtime source of truth once an operator saves an override.
- Resource refresh must preserve that metadata override; otherwise Tool Admin would show one mode while runtime execution silently falls back to another.
- `load_tool_context("exec")` / `load_tool_context("exec_runtime")`, node dynamic contracts, and frontdoor dynamic contracts should all agree on the same `exec_runtime_policy` payload. If they disagree, debug the persisted tool family record first, then the contract-injection path.

### CEO Regulatory Governance Mode

- Tool Admin now also owns one global persisted switch for CEO/frontdoor: `ceo_frontdoor_regulatory_mode_enabled`.
- This switch is not stored on a specific surfaced tool family. It is governance metadata used by CEO/frontdoor approval policy.
- Its scope is narrower than generic Tool Admin RBAC:
  - RBAC still decides whether a surfaced tool family/action is visible or callable at all.
  - regulatory mode only decides whether already-visible medium/high-risk CEO tool calls must pause for batch human review.
- The runtime contract when this switch is enabled is:
  - risky CEO tool calls are grouped into one `frontdoor_tool_approval_batch` interrupt,
  - `review_items` enumerate only the risky calls that require operator review,
  - the resume side must submit one complete `submit_batch_review` payload covering every `review_item` exactly once.
- Pass-through low-risk tool calls in the same original tool batch still remain part of the runtime-owned original tool-call ordering. Maintainers should not assume “review items == all tool calls in the round”.
- Rejected risky calls no longer disappear. CEO/frontdoor constructs synthetic rejection tool results and merges them back with approved real tool results in original tool-call order before the next model round.
- Changing the switch should affect future approval boundaries immediately, even for already-running CEO sessions. It must not silently rewrite an approval batch that is already paused and waiting for review.

### Hard Removal Of `message` / `messaging`

The surfaced `message` executor and its Tool Admin family `messaging` are now removed from the resource/tool contract entirely.

- Resource discovery should no longer find `tools/message/resource.yaml`.
- Tool Admin should no longer list a `messaging` family.
- CEO/frontdoor fixed builtin exposure and the default `frontdoor_interrupt_tool_names` no longer include `message`.
- Web runtime no longer has a special-case startup auto-disable path or any `G3KU_WEB_DISABLE_MESSAGE_TOOL` override.
- If maintainers still see `messaging` in Tool Admin, or `message` in provider-facing web tool schemas, treat that as a contract regression rather than as a disabled-by-default state.

The removal only applies to the surfaced model/tool contract.

- Browser/web replies still travel through the websocket session path (`ceo.reply.final`, inflight snapshots, and related runtime events).
- China/channel replies still travel through `SessionRuntimeBridge` plus `ChinaBridgeTransport` deliver frames.
- Do not debug channel reply delivery as if it depended on a surfaced `message` tool family; that communication path is transport-owned, not Tool-Admin-owned.

Internal fixed tools that are not surfaced in Tool Admin remain outside this contract.

- They keep their existing runtime-only visibility rules.
- Do not try to model their access through Tool Admin `allowed_roles`.
- If a behavior question involves stage protocol tools such as `submit_next_stage`, debug the stage/runtime path rather than Tool Admin RBAC.

### CEO Provider Tool Surface

For CEO/frontdoor prompt-cache debugging, maintainers now need to distinguish two tool surfaces:

- `tool_names` still represent the current turn's agent-facing callable pool.
- `provider_tool_names` represent the provider-facing stable superset used to build `tools[]` for function calling.

The important rule is that hydration promotion and stage gating should change the tail `frontdoor_runtime_tool_contract`, but they should not churn the provider-facing `tools[]` bundle every round.

When debugging provider-tool drift, use this decision rule:

- Ordinary turns may refresh provider-facing `tools[]` immediately from the current RBAC-visible concrete tool set, but only when membership truly changed.
- If the recomputed provider bundle has the same names in a different order, keep the persisted order exactly as-is instead of rotating schemas for no behavioral gain.
- If the current send is already doing `token_compression`, keep that send's provider-facing `tools[]` unchanged and defer any later refresh to the first post-compression ordinary turn.
- If RBAC removed a tool, execution must reject it immediately even if the provider-facing schema has not yet converged.

## Runtime Contract Format

- CEO/frontdoor no longer sends the model-facing runtime contract as raw JSON inside a user message. The live contract is now an assistant summary block headed `## Runtime Tool Contract`.
- The provider-native callable schema still lives in provider `tools[]`. The summary block exists only to explain callable tools, hydrated tools, candidate tools, candidate skills, and stage state to the model in compact text form.
- 对 CEO/frontdoor，这个摘要现在要额外明确两条不对称语义：
  - `candidate_tools` 是 candidate executor 摘要，通常仍需 `load_tool_context(...)` 后等待下一轮 hydration/promotion
  - `candidate_skills` 是 loadable skill 摘要，应该把列出的 `skill_id` 直接理解为 `load_skill_context(skill_id="...")` 的正文入口，而不是 hydration/install 阶段
- The summary block may now also carry dedicated repair lanes:
  - `repair_required_tools`
  - `repair_required_skills`
- These lanes are agent-facing only. They exist so the model can see “repair before use/view” resources without misreading them as ordinary callable/candidate capabilities.
- Same-turn provider-bound requests may still accumulate older contract snapshots so the hot request path stays append-only. Within one turn, the newest summary block is the authoritative contract.
- Durable CEO continuity baselines still strip those contract messages before they are written back to session-owned request-body state. The next round rebuilds one fresh authoritative summary from canonical runtime state.
- Execution and acceptance nodes now use the same summary-style contract lane instead of a raw `node_runtime_tool_contract` JSON user message.
- Node summaries now expose `hydrated_executor_names` as names only. The detailed provider-call schema remains separate in node `provider_tool_names` and provider `tools[]`.

The provider-facing bundle is also intentionally minimal:

- Rich tool and skill descriptions stay in the tail runtime contract.
- Provider `tools[]` should keep only the smallest callable schema required for function calling.
- Repair-required tool/skill exposure must not be implemented by churning provider `tools[]`. Maintainers should treat repair-required lists as runtime-summary-only guidance; provider bundle stability still wins for cache continuity.
- `stage_compaction` must not be used as a shortcut to publish a new provider bundle. If artifacts show a new `actual_tool_schema_hash` together with `history_shrink_reason=stage_compaction`, treat that as provider-bundle refresh regression.
- Provider-facing schemas are now sanitized before transport: descriptive text and unsupported JSON Schema combinators such as `anyOf`, `oneOf`, and `allOf` are stripped or flattened into a simpler supported shape.
- Runtime-side tool validation remains the authority for argument correctness. Do not assume a provider-facing schema still preserves every branch of the richer internal contract.
- If cache misses correlate with a large `actual_tool_schema_hash` delta, first check whether provider schemas accidentally regressed from this minimal/stable form.

## Frontdoor Interrupt Payload Update

Approval-interrupt and pause/recovery payloads should now be understood as carrying these runtime-owned frontdoor fields:

- `frontdoor_stage_state`
- `compression_state`
- `hydrated_tool_names`
- `tool_call_payloads`
- `frontdoor_selection_debug`

`semantic_context_state` is no longer part of the active frontdoor runtime contract. If a maintainer still sees it in an old paused snapshot or legacy test fixture, treat it as stale compatibility data rather than as a live contract field.
