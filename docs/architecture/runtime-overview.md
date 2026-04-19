# G3KU 运行时总览

本文档解释 G3KU 的核心运行时主线：消息如何进入系统、会话如何被执行、frontdoor 与任务运行时如何分工。

## 1. 运行时分层

如果只看 Python 主运行时，可以按下面四层理解：

1. 入口与装配
   `g3ku/runtime/bootstrap_factory.py`
   负责根据配置创建 provider 与 `AgentLoop`。

2. 会话与 turn 执行
   `g3ku/runtime/manager.py`
   `g3ku/runtime/bridge.py`
   `g3ku/runtime/session_agent.py`
   负责 session 生命周期、一次 turn 的锁、事件、持久化与恢复。

3. Agent 执行引擎
   `g3ku/agent/loop.py`
   `g3ku/runtime/engine.py`
   负责工具注册、memory、multi-agent、watchdog、模型客户端接入。

4. frontdoor 与任务下沉
   `g3ku/runtime/frontdoor/`
   `main/service/runtime_service.py`
   负责 CEO/frontdoor 提示词、阶段状态、任务创建、异步执行树。

## 2. 主入口文件

新维护者最应该先看的运行时文件：

- `g3ku/runtime/bootstrap_factory.py`
  运行时工厂。说明项目只支持 `langgraph` runtime，并在这里把 provider、middleware、`AgentLoop` 装起来。

- `g3ku/agent/loop.py`
  是 `AgentRuntimeEngine` 的兼容包装层，本身逻辑不多，但定义了真正运行时类型 `AgentLoop`。

- `g3ku/runtime/engine.py`
  运行时核心容器。负责：
  - `ToolRegistry`
  - `ToolExecutionManager`
  - session 取消令牌
  - memory/checkpointer/store
  - bootstrap bridge 初始化默认工具和多 agent 运行时

- `g3ku/runtime/manager.py`
  `SessionRuntimeManager`。按 `session_key` 复用 `RuntimeAgentSession`，是所有入口共享的 session 路由器。

- `g3ku/runtime/bridge.py`
  `SessionRuntimeBridge`。给 Web、CLI、China bridge、cron 提供统一的 prompt/continue/cancel API。

- `g3ku/runtime/session_agent.py`
  单次 turn 的核心执行器，也是最复杂、最值得精读的文件之一。

## 3. 一条消息如何被执行

### 3.1 从入口到 Session

无论消息来自 CLI、Web 还是渠道桥接，通常都会走到：

1. 拿到 `AgentLoop`
2. 创建 `SessionRuntimeManager`
3. 调 `SessionRuntimeBridge.prompt(...)`
4. Bridge 取出或创建 `RuntimeAgentSession`
5. `RuntimeAgentSession.prompt(...)` 执行 turn

`SessionRuntimeManager` 的职责很纯粹：

- 以 `session_key` 做缓存键
- 维护 `channel/chat_id` 和 memory 相关 live context
- 把 prompt/continue/cancel 转发给具体 session

这意味着：

- 会话路由规则首先要看 `session_key` 是否稳定
- 如果同一个 session 行为异常，先看路由参数是否被错误复用

### 3.2 `RuntimeAgentSession` 内部做什么

`RuntimeAgentSession` 是整个同步会话路径最关键的对象，负责：

- turn 锁，避免同一 session 并发踩踏
- transcript 持久化
- event log / state snapshot
- 工具调用跟踪与 background tool 状态
- pause / resume / cancel
- frontdoor interrupt 恢复
- heartbeat / cron 等内部消息的特殊处理

在当前 Web CEO 路径里，还要特别注意一个维护语义：

- 每个可显示的 inflight turn 现在都有稳定的 `turn_id`
- `inflight_turn_snapshot`、`message_end`、heartbeat discard/final reply 都会沿这个 `turn_id` 传递
- 前端不应再仅靠 `source=user|heartbeat` 去猜“当前该收口的是哪个 pending bubble”
- `inflight_turn_snapshot()` 现在必须只表达“当前真实在跑的 turn”。如果 heartbeat / cron 在运行前需要暂时保留上一条可见 user bubble，运行时会把它放在单独的 preserved snapshot，而不是再让 preserved turn 覆盖当前 inflight turn。
- 对 Web CEO websocket 来说，这意味着 live payload 里可能同时出现 `inflight_turn` 和 `preserved_turn`：前者是当前 heartbeat/cron/user turn，后者只是等待后续 `ceo.turn.discard` 收口的旧可见气泡。
- 对 Web CEO/frontdoor 来说，session 侧还会同步保存当前 turn 的 hydrated tool state；它和 stage trace 一样属于“当前进行中 turn 的运行时事实”，不是长期 transcript。
- session 侧现在还会同步保存 `frontdoor_selection_debug`，用于记录当前 turn 的 frontdoor 候选生成诊断：原始 query、rewrite 后的 skill/tool query、dense/rerank trace、tool selection trace，以及当轮 callable/candidate/hydrated 合同快照。
- 维护上还要记住两个默认上限：CEO/frontdoor 的默认 candidate skills / candidate tools 上限现在都是 16；frontdoor 与节点的默认 hydrated tool LRU 上限也都是 16。若线上行为不像 16，优先检查运行时状态或 `tools/memory_runtime/resource.yaml` 是否显式改写。

这意味着 `RuntimeAgentSession` 不只是维护“session 是否正在运行”，还维护了“当前可显示 turn 的身份”。如果这里的 `turn_id` 传播断掉，典型回归就是：

- 同 source 的多个 pending turn 被错误合并
- heartbeat 清理时误删或漏删旧 turn
- 前端残留一个只有“处理中...”的气泡
- heartbeat 新气泡错误继承上一条 user bubble 的 `Interaction Flow`

另外，手动 pause 之后的后续输入语义也有一个容易误改的点：

- Web 会话里，手动 pause 的语义现在是“冻结上一轮”，不是“等待下一条输入来补写原请求”
- UI 文案仍然可以显示“暂停”，但普通用户 manual pause 的后端语义已经改成“停止当前轮并固化当前进度”：session 最终会落成 `completed`，并带 `stop_reason=user_pause`
- pause 当下的 user message、阶段状态、工具调用轨迹、压缩状态和中间 assistant 文本都会继续持久化，作为上一轮上下文保留
- manual pause 收尾时会立即写一份 completed continuity sidecar 到 `.g3ku/web-ceo-continuity/<session>.json`，然后清掉普通用户路径上的 paused / inflight restorable snapshot；后续输入不再依赖“继续 paused turn”
- 因此前端/网关层在普通用户点击 pause 之后，不应再走 `resume(additional_context=...)`；后续输入必须作为新一轮 user turn 发送
- paused assistant 气泡现在会在 manual pause 收尾时立即归档成 durable assistant transcript entry；这条记录会带 `status=paused`，并标记 `history_visible=false` 与 `source=manual_pause_archive`
- 这条 archived paused assistant 的职责是让刷新/重连后的 UI 能重建上一轮暂停气泡，并保留审计痕迹；它不是下一轮 prompt 的可见原始历史
- 因此普通用户下一轮 frontdoor/history compaction 现在应继续依赖可见 transcript 加 completed continuity sidecar 恢复出的 baseline / stage / compression / semantic 状态；session 列表的 `preview_text` / `message_count` 这类 operator summary 也应继续过滤掉这条 hidden paused assistant
- 如果用户在运行中连续补充多条消息，运行时会把它们作为“同一轮 LLM 调用前的一批独立 user message”一起持久化并一起注入模型，而不是拼接成一条 `补充要求` 文本

如果这里被改回“pause 后仍把新输入合并回原 user message”，典型回归就是：

- 被暂停前的 transcript user message 被覆盖，上一轮真实输入丢失
- 工具调用和阶段轨迹虽然还在 snapshot 里，但 transcript 与上下文压缩看到的是被篡改后的 user 文本
- 前端多个补充气泡在后端被折叠成一条 `补充要求`，导致 UI 和 LLM 实际收到的消息结构不一致

可以把它看成“一个会话的状态机 + turn 执行器”。

新人容易低估这个文件的复杂度，因为它名字看起来像简单 session 封装，实际上它承担了：

- user turn
- heartbeat internal turn
- cron internal turn
- async dispatch 错误恢复
- paused execution context
- frontdoor stage state
- frontdoor hydrated tool state

## 4. frontdoor 与任务运行时的关系

G3KU 并不是所有问题都在 CEO 单次对话内完成。frontdoor 的职责更像是：

- 识别当前用户请求
- 组织提示词与上下文
- 判断当前这轮能直接回答，还是需要走任务运行时
- 在必要时触发任务工具，如 `create_async_task`

Maintenance note for `create_async_task` duplicate precheck:

- CEO/frontdoor may still call `create_async_task` more than once in the same visible turn; the runtime no longer enforces a hidden "one turn, one async task" rule.
- Whether a detached task is actually created is now decided by `MainRuntimeService`, not by prompt wording alone.
- Before creating a new task, the service compares the candidate request against the current session's unfinished task pool.
- The deterministic rule layer only blocks exact duplicates, using normalized target text and exact keyword fingerprint matching.
- If the exact-rule layer allows the request and unfinished tasks exist, an inspection-model review may still return `approve_new`, `reject_duplicate`, or `reject_use_append_notice`.
- `reject_use_append_notice` means the new request is really an update to an existing unfinished task rather than a new detached work item. The rejection now points explicitly at CEO/frontdoor builtin `task_append_notice`.
- Frontdoor dispatch bookkeeping must therefore distinguish "tool call happened" from "new task was actually created". A rejection message may still mention an old `task:...` id, but that does not count as a fresh dispatch.

Maintenance note for `task_append_notice` and task message distribution:

- `task_append_notice` is a CEO-only task-lifecycle builtin for appending new requirements, constraints, or acceptance expectations to an unfinished task in the current session. It is not a generic task-editing tool and it must not be treated as detached task creation.
- The runtime now treats appended task messages as a task-level control transaction: it persists a distribution epoch plus durable node mailboxes before ordinary execution sees the new message.
- The first public step is queue/coalesce in `MainRuntimeService.task_append_notice(...)`. That step validates session/task scope, requests the existing pause barrier, and writes operator-visible `runtime_meta.distribution`.
- Once the task enters distribution mode, the work still goes through the existing `TaskActorService` / `TaskNodeDispatcher` / `NodeRunner` path. There is no sidecar execution lane for node distribution turns.
- Distribution turns are compact control turns. They use `node_message_distribution.md` plus internal tool `submit_message_distribution`, not the ordinary execution prompt/tool bundle.
- Delivery to child execution nodes creates durable mailbox rows. If the target execution node was terminal, delivery reactivates it; if its old acceptance node exists, that acceptance node is invalidated and detached from the current effective spawn-entry chain instead of being deleted.
- Ordinary execution consumes delivered mailbox rows only at safe boundaries inside `_resume_react_state(...)`: the previous active stage is closed, mailbox rows become user messages in durable node history order, and the rows are then marked `consumed`.
- Consumed append-notice messages now also persist into node-local `append_notice_context` metadata. This is a second prompt-assembly truth source used specifically to keep notice requirements visible across stage compaction and later compression-stage archival.
- During ordinary node prompt assembly, raw notices received since the last compression-stage rollover render as a dedicated non-compressible tail block before any `STAGE_COMPACT` / `STAGE_EXTERNALIZED` blocks.
- When execution-stage archival creates a new compression stage, the runtime rolls the previously uncompressed notice interval into a compressed notice-tail segment and keeps that segment ahead of the archived stage block. Maintainers debugging “notice disappeared after compression” should inspect `append_notice_context`, not only the raw mailbox rows.
- Force delete wins over message distribution. Before task rows disappear, the runtime cancels active epochs, cancels or purges mailbox rows, clears `runtime_meta.distribution`, and prevents later queue wakeups from resuming the deleted task.

对于异步任务的回传，还要补一个当前维护边界：

- 任务本身在 `main/` 中结束后，会通过 task terminal callback / heartbeat 回到原 CEO 会话。
- For `task_terminal`, heartbeat is no longer allowed to swallow the result silently; the normal path must produce a user-visible reply.
- Heartbeat may still enter repair rounds to produce that visible reply, but it no longer creates replacement tasks and no longer retries failed or unpassed tasks in place.
- If repair rounds still only produce empty output or `HEARTBEAT_OK`, the service emits a fixed fallback error; any further work must come from a later explicit user/frontdoor decision, typically through `create_async_task`.
- If the task panel says success but the CEO session never receives a final reply, treat that as a heartbeat repair/fallback bug first, not as an ordinary UI rendering issue.

当前 frontdoor 的上下文组织需要按“双层模型”理解：

- 内层是阶段工作集：
  - 最近 3 个 completed stages 保留原始窗口
  - 更早 completed stages 改写为 compact / externalized stage blocks
  - 当前 active stage 保留原始窗口
- 外层是全局语义摘要：
  - 覆盖 local workset 之外的更老历史
  - 采用 lossy summarization 生成 handoff/reference block
  - 不替代 canonical transcript，也不替代 frontdoor stage archive

这两层不是互斥关系。阶段工作集负责“当前轮还需要精读的近场上下文”，全局语义摘要负责“防止长会话失忆的远场上下文”。

前门提示词本身还要再分成“静态协议层”和“动态注入层”两部分理解：

- `g3ku/runtime/prompts/ceo_frontdoor.md` 承载 CEO frontdoor 的稳定协议，包括角色规则、任务/工具通用约束，以及 stage-first 这类高优先级协议。
- `g3ku/runtime/frontdoor/prompt_builder.py` 负责把稳定协议与少量环境提示装成 base prompt。
- `g3ku/runtime/frontdoor/message_builder.py` 继续按本轮会话状态动态注入 retrieved context、memory hint、全局语义摘要，以及当前轮运行时工具合同所需的数据。
- CEO/frontdoor 的生产执行面现在是显式 `StateGraph`，入口到收尾固定经过 `prepare_turn -> call_model -> normalize_model_output -> review_tool_calls -> execute_tools -> finalize`。
- `call_model` 和 `execute_tools` 现在共用同一份 frontdoor runtime tool bundle。像 `submit_next_stage` 这类运行时注入的 stage protocol tool，必须同时对模型“可见”并且在 `execute_tools` 里可真实执行；维护时不要再在执行环节单独从 `state.tool_names` 重建第二套工具表。
- `execute_tools` 现在会在真正执行前落实 stage gate：普通工具在无活动阶段或阶段预算耗尽时会直接得到 gate error；如果同一批 tool calls 里把 `submit_next_stage` 和其他普通工具混在一起，整个批次会被拒绝，要求模型先单独完成阶段切换。
- 成功的 `submit_next_stage` 会在同一轮 `execute_tools` 完成后立刻写回 `frontdoor_stage_state`；下一次 `call_model` 看到的已经是 promotion 后、阶段已推进后的 runtime state，而不是等额外的后处理链再补写。
- CEO websocket now attaches the current visible turn's authoritative final `canonical_context` directly to `ceo.reply.final`; maintainers should not assume the frontend must wait for a later `state_snapshot` to reconstruct the closing stage view.
- 新的前门暴露边界要分两层理解：只要当前没有“有效阶段”（`active_stage_id` 为空，或当前阶段已 `transition_required=true`），agent-facing `frontdoor_runtime_tool_contract.callable_tool_names` 会收紧到只剩 `submit_next_stage`；候选 tool/skill 列表仍继续显示，供模型先看能力边界、再开阶段。
- 但 provider-facing `tools` schemas 现在不再随着 `transition_required=true` 收紧到只剩 `submit_next_stage`。前门会继续向 provider 发送稳定的 runtime-visible tool bundle，以减少阶段切换回合对 prompt cache 前缀命中的破坏。
- 当前唯一保留的例外是 `cron_internal`。这类内部轮次仍保留自己的 callable tool 集合，以便按既有协议继续调用 `cron(action="remove")` 完成自移除；维护时不要把这个 internal lane 与普通 CEO user turn 混为一谈。
- 维护上不要把这个规则误读成“前门内部状态已经只剩 `submit_next_stage`”。`tool_names` 仍保存阶段内可恢复的完整工具池，供同一 turn 成功开阶段后的下一次 model call 立即恢复完整 callable 列表；agent-facing 决策边界由前门的 callable-tool helper 与动态合同消息表达，而 provider-facing schema 稳定性由 runtime-visible tool bundle 保证。
- `g3ku/runtime/frontdoor/_ceo_create_agent_impl.py` 仍是 runner 入口，但它不再把 `create_agent + middleware` 当作前门主执行链；维护时应把 `_graph_*` 节点看成唯一权威路径。
- frontdoor 与节点动态合同现在还会携带 `exec_runtime_policy`。这让 prompt 中不再需要把 exec 的“只读/受监管”规则写死为静态事实；维护者应优先把当前 exec 模式视为 runtime contract 的一部分，而不是 prompt 文案的一部分。
- CEO/frontdoor 的稳定 system prompt 现在只保留最小的 capability exposure revision 锚点，不再把可见 tool/skill 名单整块写进稳定前缀。
- 对 CEO/frontdoor，当前轮真正给模型看的 tool/skill catalog 现在只有一份 `frontdoor_runtime_tool_contract` user 消息。它位于 request 尾部，也就是所有稳定前缀、持久化历史和当前 user message 之后。
- 这份 frontdoor runtime tool contract 属于“当前轮临时合同”，不是 durable history。后续轮次的 `stable_messages` / transcript 不应再继承旧轮的 tool/skill 名单；如果维护者在下一轮历史里又看到旧 contract，优先排查 prompt contract 组装或 state replay 是否把 dynamic appendix 错写回了 stable history。
- 对 CEO/frontdoor 主链路，`dynamic_appendix_messages` 的持久化形态仍然只保留“当前 authoritative 的 `frontdoor_runtime_tool_contract`”。但活动中的 turn state / provider-facing request 现在允许保留同一 turn 里更早轮次的 contract snapshots，目的不是持久化历史，而是让同一 turn 的 request body 保持 append-only。
- 维护时要区分两层：`dynamic_appendix_messages` 表示“下一次重建时应追加的最新合同”，而活动中的 `messages` / actual request JSON 可能还包含更早轮次的 contract snapshot。若同一 turn 里出现多个 contract，最后一条才是权威；这些旧 snapshot 不应写入后续 durable history。

维护上现在还要区分 frontdoor 的两份工具状态：

- `candidate_tool_names` 表示当前轮对模型可见、但默认仍需先 `load_tool_context` 的 concrete tools。
- `hydrated_tool_names` 表示本 turn 里已经成功读过契约、并被提升为下一轮 callable 的 concrete tools。
- frontdoor 的语义工具选择现在直接面向 concrete tool，而不是“先命中 family 再按 family 顺序展开 executor”；因此 `frontdoor_selection_debug.semantic_frontdoor` 里的 `tool_ids` 应直接是诸如 `filesystem_write` 这样的 concrete tool names。

这两份状态都由 frontdoor persistent state 维护，而不是只存在于某一轮 prompt 文本里。其直接后果是：

- 同一用户 turn 内，`load_tool_context` 成功后，下一轮真正发给模型的函数工具列表会并入对应 hydrated tool。
- frontdoor approval interrupt、session inflight snapshot、paused execution context 也会携带这份 hydrated state，避免“暂停前已经 load 成功，恢复后又退回 candidate-only”。
- 同一套 inflight snapshot / paused execution context 现在也会带上 `frontdoor_selection_debug`。排查“rewrite 后 query 不对”“向量召回打到了哪些 tool/skill”“为什么某个工具没进 candidate”时，优先查看这份 snapshot，而不是只看最终 assistant 文本。
- approval interrupt 负载现在还会一起携带 `frontdoor_stage_state`、`compression_state`、`semantic_context_state`、`hydrated_tool_names`、`tool_call_payloads` 和 `frontdoor_selection_debug`；恢复时这些字段会直接回灌 session/runtime state，而不是再由 middleware 临时重建。

阶段预算上还有一个容易被误判的点：

- `load_tool_context` / `load_skill_context` 会照常进入 round 记录与执行轨迹，但它们现在不再增加 `tool_rounds_used`。
- 这条规则同时适用于 execution/acceptance 节点和 CEO/frontdoor；如果维护者看到很多 loader 调用，不要据此直接推断阶段预算已经被吃掉。
- 排查预算问题时，应优先检查 `rounds[*].budget_counted`、阶段快照和 runtime messages artifact，而不是按工具名手算。
- execution、acceptance 与 CEO/frontdoor 现在共用同一条阶段预算合同：`submit_next_stage.tool_round_budget` 必须落在 `1-10` 之间；这表示单阶段允许声明的上限窗口，而不是要求模型必须把预算耗尽后才能提前切到下一阶段。

### Canonical Tool Contract Notes

Additional maintenance note for fixed-builtin resource executors:

- CEO/frontdoor and node execution now treat semantic top-k as an extension-tool budget. Resource-backed fixed builtin executors are filtered out before semantic narrowing so they do not consume dense/rerank shortlist capacity.
- The same class of executors also stays out of hydration LRU. `load_tool_context` may still surface their contract/help payload, but a successful loader result should not promote an already fixed-callable executor into hydrated state.
- In practice this means "resource-backed fixed builtin" is now a third debugging category between "pure internal builtin" and "ordinary extension executor": it is resource-backed for catalog/help/RBAC purposes, but it does not spend semantic top-k or hydration-LRU budget.

- 节点与 CEO/frontdoor 的 `candidate_tool_names` / `candidate_skill_ids` 现在都表示“`RBAC 可见 ∩ 语义召回命中` 的当前候选集合”；语义召回不可用时，候选集合退化为 `RBAC 可见集合`，而不是报错中断。
- `load_tool_context` / `load_skill_context` 的准入只认当前 canonical candidate 集合；不再允许“RBAC 可见但不在 candidate 中”的旁路加载。
- 节点的 hydration canonical state 继续落在 runtime frame：`hydrated_executor_state` / `hydrated_executor_names`。这是节点生命周期级 LRU，跨多轮、阶段切换、pause/resume、restore 保留。
- 节点的 skill candidate canonical state 也继续落在 runtime frame：至少包括 `candidate_skill_ids`，以及供 dynamic contract 重建使用的 `candidate_skill_items` 显示缓存。阶段切换后的 prompt compaction 可以裁掉旧的 `node_runtime_tool_contract` user 消息，但下一轮 contract 刷新仍应从 frame 恢复这两份 skill 状态，而不是把 skill 候选清空。
- CEO/frontdoor 的 hydration canonical state 继续落在 session/frontdoor state：`RuntimeAgentSession._frontdoor_hydrated_tool_names` 加上前门 persistent state 中的 `hydrated_tool_names`。这是 session 生命周期级 LRU，跨 turn 保留，但每轮都会按当前 RBAC 可见集合过滤。
- 节点与 CEO/frontdoor 都只允许 concrete tool names 进入 hydration LRU；family id 不能进入 promoted callable 集合。
- execution / acceptance 节点现在也采用与 CEO/frontdoor 对齐的“有效阶段”合同：只要 `has_active_stage=false`，或当前阶段已经 `transition_required=true`，当前轮真正暴露给模型的 callable tool schemas 就只剩 `submit_next_stage`。
- 这条节点规则只收紧当前轮 callable，不收紧 candidate。`candidate_tool_names` / `candidate_skill_ids` 继续表达候选集合；维护时如果看到节点动态合同里 callable 只剩 `submit_next_stage`，不要据此误判 selector、hydration 或语义召回已经丢失。
- 节点侧的完整 callable pool 现在只通过本地 `model_visible_tool_selection_trace.full_callable_tool_names` 保留下来，并随 runtime frame 与 `runtime-frame-messages:{node_id}` artifact 一起落盘；它不再属于 agent-facing `node_runtime_tool_contract`。排障时应先看这份 trace，再决定是阶段锁定还是工具选择真的出错。
- restore / recovery 只认 canonical frame / session state 中的 callable/candidate/hydrated/skill 字段；缺失时直接报“运行时工具合同损坏/缺失”，不再回退 bootstrap 文本、旧 transcript 或旧动态消息。
- 排查“首轮明明选中了某个 skill，`submit_next_stage` 之后再 `load_skill_context` 却报 not candidate”时，先看节点 runtime frame 是否仍保留该 skill 的 `candidate_skill_ids` / `candidate_skill_items`。如果 frame 还在而当轮 dynamic contract 已空，优先判断为 contract 重建链路或阶段压缩边界问题，而不是 selector 没命中。
- 对 CEO/frontdoor，`frontdoor_stage_state`、`compression_state`、`semantic_context_state` 属于受保护运行时状态。工具合同刷新不能覆盖、清空或重置这三份状态。
- `exec` 的执行模式现在也是受保护的 runtime-owned state：`ExecTool` 会在每次调用时重新读取当前 `exec_runtime` family metadata，而不是只在工具实例初始化时拍死一份本地配置。因此 Tool Admin 修改 mode 后，后续新的 `exec` 调用会立即生效，不需要项目重启，也不依赖重建现有 tool 实例。

### CEO Frontdoor Round Tool Ownership

For the CEO/frontdoor path, `frontdoor_stage_state.stages[].rounds[].tools` is now the authoritative record of which tool calls belong to a round.

- `_frontdoor_stage_state_after_tool_cycle()` writes the exact round-level tool entries at tool-cycle completion time.
- Each stored tool entry should carry the stable identity (`tool_call_id`) together with the display-oriented fields used by snapshots and transcript summaries, such as `tool_name`, `status`, `arguments_text`, `output_preview_text` / `output_text`, `output_ref`, `timestamp`, `kind`, and `source`.
- `tool_names` and `tool_call_ids` may still exist for compatibility or summary purposes, but maintainers should treat them as derived hints rather than a second source of truth.

When `RuntimeAgentSession` rebuilds `canonical_context`, the intended contract is:

- If a round already has `tools`, trust `round.tools` directly.
- If an older round only has `tool_call_ids`, backfill only by exact `tool_call_id`.
- Matching by `tool_name` alone is a regression risk because it can steal a later same-name tool result into an earlier round.

If a maintainer sees a CEO stage trace where a later `exec` appears inside an earlier round, first inspect whether the stored round was missing `tools` and whether the persisted `tool_call_ids` are stable and unique. Do not reintroduce any `tool_name`-only round fill path in session snapshot assembly.

这次前门工具合同收敛还明确了另一个边界：

- CEO/frontdoor 的 tool promotion 已改为执行循环直接基于 `raw_result` 处理 `load_tool_context` 的成功返回，而不是再从 trailing `ToolMessage` / `result_text` 反推 hydration。
- `_frontdoor_stage_state_after_tool_cycle()` 仍然只负责 round 记账和 `round.tools` 落盘；它不参与 tool promotion。
- 因此前门的“tool contract 更新”和“阶段工具显示”是两条平行链路：
  - promotion 改动只影响 callable/candidate/hydrated contract
- `round.tools` inside `canonical_context` still depends only on the display-oriented fields derived from `tool_call_payloads + tool_results`
- `g3ku/runtime/frontdoor/ceo_agent_middleware.py` 现在只剩兼容/测试价值，不再是生产前门的权威执行链。若线上 frontdoor 行为与预期不符，优先检查显式图节点和 checkpoint state，而不是先检查 middleware hook 次序。

维护上一个容易误判的点是：动态 skill/tool 提示块里虽然会出现“如何读取 skill 正文”或“如何读取 tool 契约”的说明，但这些说明不能覆盖 `ceo_frontdoor.md` 里的 stage-first 协议。当前前门的权威顺序是：

1. 先看静态协议是否要求“无活动阶段时必须先 `submit_next_stage`”
2. 只有在活动阶段已经存在后，动态 skill/tool 暴露里的 `load_skill_context` / `load_tool_context` 提示才真正进入可执行顺序

如果维护者在排查“为什么模型先调用了 `load_skill_context` 却撞上 no active stage”这类问题，不要只盯动态 skill 列表或 candidate tool 列表；要先检查 `ceo_frontdoor.md` 的稳定协议与 `stage_messages.py` 的当前状态 overlay 是否一致，再检查 `prompt_builder.py` / `message_builder.py` 是否把动态提示写成了会与主协议竞争的动作指令。

heartbeat / cron 的维护语义也要分三条通道理解：

- UI 展示通道：
  - 前端继续通过 inflight/session snapshot 渲染 heartbeat / cron 的原始处理流程，包括开阶段、工具调用、执行轨迹和压缩状态。
- 普通历史注入通道：
  - 下一次真实用户 turn 的近场 prompt 历史仍可过滤 internal-only user 消息与 `history_visible=false` 的 assistant 消息。
- 总压缩输入通道：
  - heartbeat / cron 的 agent-side 原始执行上下文会进入全局语义摘要素材池。
  - 因此前端能看到的 heartbeat / cron 处理流程，后续真实用户 turn 不一定原样继承 live snapshot，但应能通过 global summary 间接读到其关键语义。

相关文件：

- `g3ku/runtime/frontdoor/ceo_runner.py`
- `g3ku/runtime/frontdoor/prompt_builder.py`
- `g3ku/runtime/frontdoor/message_builder.py`
- `g3ku/runtime/semantic_context_summary.py`
- `g3ku/runtime/stage_prompt_compaction.py`
- `g3ku/runtime/frontdoor/task_ledger.py`

一个实用理解方式：

- `g3ku/runtime/` 负责“会话级 orchestration”
- `main/` 负责“任务级 execution engine”

两者不是替代关系，而是上下游关系。

## 5. 与 `main/` 任务运行时的衔接

当 Agent 选择任务型执行时，控制权会下沉到 `MainRuntimeService`：

- `main/service/runtime_service.py`
  任务运行时总入口，负责服务装配、工具提供、治理、worker 协调、内容服务、日志服务。

- `main/runtime/node_runner.py`
  节点执行器。每个任务节点都会走这里。

一个非常重要的事实：

- `MainRuntimeService` 既是服务层，也是系统集成层。
- 它把存储、治理、工具选择、worker 状态、内容服务都绑在一起。

### 5.1 Chat provider 超时边界（维护者必须掌握）

当前 chat 调用在 `main/runtime/chat_backend.py` 与 `g3ku/providers/fallback.py` 的外层重试逻辑里，会区分两类 provider：

- provider 自己管理流式超时（`manages_request_timeout_internally=True`）
- 仍由外层统一 `wait_for` 管理超时

对第一类（目前包括 `ResponsesProvider`、`OpenAICodexProvider`、`CustomProvider`、`LiteLLMProvider`）：

- 外层不会再施加单次 attempt 的硬总时长截断（避免“流式在持续出 chunk 但被总时长误杀”）
- provider 内部采用“streaming-first”语义：
  - 首个 chunk（任意 chunk，不要求文本 delta）60s 内必须到达
  - 流开始后，连续 60s 没有任何新 chunk 视为 idle timeout
  - 若上游不支持流式，provider 在同一次 attempt 内自动回退到非流式请求
  - 非流式回退路径采用 120s 首响应超时

维护判断上要记住：

- “首 token”在本项目语义里是“首个 chunk 到达”，不是“首个文本 token”
- `request_timeout_seconds` 对上述 provider 表示“首 chunk / idle chunk 超时阈值”，不再是“整次请求必须在 N 秒内完成”

## 6. 运行时里的状态与持久化

同步会话和任务运行时有两套不同的持久化关注点：

### 会话侧

- transcript / session messages
- paused execution context
- inflight turn snapshot
- latest message / pending interrupts

主要由 `RuntimeAgentSession` 和 `g3ku/session/manager.py` 协调。

### 任务侧

- task / node 元数据
- 执行日志和运行帧
- artifacts
- 事件历史
- 治理状态

主要由以下模块协同：

- `main/storage/sqlite_store.py`
- `main/monitoring/log_service.py`
- `main/monitoring/query_service_v2.py`
- `main/storage/artifact_store.py`
- `main/governance/`

关于节点运行帧还要额外记住一个新的维护语义：

- `task_runtime_messages` / `runtime-frame-messages:{node_id}` artifact 不再只是当前 messages 列表；它现在还会在同一个 artifact 里累计 `callable_tool_snapshots`。
- 每条快照代表一次 `before_model` 轮次下，本地运行时真正记录的 callable/candidate 截面，包括 `callable_tool_names`、`candidate_tool_names`、`candidate_tool_items`、`candidate_skill_ids`、`candidate_skill_items`、`model_visible_tool_names`、`hydrated_executor_names` 和选择 trace。
- 对 execution / acceptance 节点，如果当轮没有有效阶段，那么这些快照里的 `callable_tool_names` 与 `model_visible_tool_names` 都应只剩 `submit_next_stage`；候选集合仍保留在 `candidate_tool_names`，完整 callable pool 则留在选择 trace 里。
- 因此当维护者排查“为什么这一轮模型明明 load 过工具却没法调用”时，先看这个 artifact 里的最近一条快照，再去看 transcript 或 stage trace；它比只看最终 messages 更能说明当轮可调用集到底是什么。
- 执行节点与检验节点现在都应理解为“两层消息结构”：稳定 bootstrap user JSON 负责任务定义，只保留稳定节点上下文；`execution_stage` 不再写在 bootstrap 里。单独的动态 `node_runtime_tool_contract` user 消息负责当前轮的 callable/candidate tool/skill 合同，并且固定追加在当前 request 尾部。
- 节点的 turn overlay / repair overlay 现在也必须遵守同一条 append-only 边界：它们只能作为新的 request-tail 消息追加，不能再把文本回写进任何已有的 bootstrap user 消息或持久化历史消息。排查缓存命中下降时，优先确认 stable prefix 是否仍保持逐轮不变。
- 对节点运行时来说，`before_model` 当轮真正下发给模型的 schema 选择结果才是权威工具来源；runtime frame、restore/recovery 和 runtime messages artifact 都应从这份结果派生，而不是再从旧 bootstrap 文本反推。
- 对节点运行时来说，skill 可见性也不能只绑定到“当前 prompt 里是否还保留着那条 dynamic contract user 消息”。`node_runtime_tool_contract` 仍是模型可见合同，但 runtime frame 才是 `candidate_skill_ids` / `candidate_skill_items` 的 canonical 恢复来源。
- CEO/frontdoor 也采用同样的分层思想：稳定会话前缀不再承担当前轮 callable/candidate tool 状态，当前轮工具合同放在 dynamic appendix，并随 turn state 刷新。
- CEO/frontdoor 的普通 turn overlay 与 repair overlay 也应保持 append-only：它们属于当前 request 的尾部临时内容，不能再改写已有 stable/request 消息。维护时如果看到 prompt cache key 没变但缓存命中突然下跌，先检查是否有 overlay 被拼回了已有 user 消息。
- 对维护者来说，这还意味着前门里的 `extension_tool_top_k` 只约束“最终候选工具数”，不是 dense 检索宽度；排查某个工具为何没进 candidate 时，要把 dense/rerank 命中和最终 top-k 截断分开看。

## Prompt Cache Family And Actual Request

- Caller-side prompt cache family is now defined only by the stable prefix plus explicit cache-family revision inputs.
- Ordinary callable/candidate/hydrated tool drift, stage-gated schema shrink, and loader-driven hydration promotion may still change the actual provider request, but they must not rotate the caller family key by themselves.
- CEO/frontdoor `call_model` must always send the request rebuilt for that exact turn together with the matching rebuilt `prompt_cache_key`. A rebuilt request paired with an older key is now considered a runtime bug because it hides whether a miss came from family churn or from the actual request changing.
- Node execution follows the same rule: keep the static prefix append-only at the front, keep the runtime contract at the tail, and debug `prompt_cache_key_hash` separately from `actual_request_hash`.
- Node runtime now also persists a dedicated per-round actual-request artifact for every `call_model`. `actual_request_ref` / `latest-context` should point to that artifact instead of reusing runtime-frame `messages_ref`.
- That node artifact stores the runtime-side request projection (`model_messages`, `request_messages`, `actual_tool_schemas`, cache-family diagnostics) and, when available from the provider adapter, the final transport payload under `provider_request_meta` and `provider_request_body`.
- When cache reuse drops while the family key stays stable, first inspect actual request growth or provider prefix reuse limits before blaming caller-side family churn.
- CEO/frontdoor provider `tools` should stay on a stable superset aligned to the exposure-visible concrete tool set, not the narrower current-turn callable set. Hydration promotion and stage gating still change the tail runtime contract, but they should not churn provider `tools` schemas every round.
- The provider-facing tool bundle is intentionally minimal. Rich tool and skill explanations belong in the tail `frontdoor_runtime_tool_contract`; provider `tools` keep only the smallest callable schema needed for function calling so cache misses are easier to attribute to real request growth instead of schema-text drift.
- CEO/frontdoor now also persists the full provider-facing request for every `call_model` round. The full payload is written under `.g3ku/web-ceo-requests/<session>/...json`, while the live session snapshot only keeps the latest file path plus a short metadata history.
- This split is intentional: the per-round JSON file is the authority for exact request-forensics, while inflight / paused session snapshots stay compact enough for websocket restore and UI debugging.
- For CEO/frontdoor specifically, the saved actual request JSON is now the authoritative source of truth for provider-facing order. Session state may persist the request body without the current contract so the next round can rebuild a single fresh tail contract; that is expected, not evidence that the provider request lost the contract.
- That same JSON now also stores the adapter-final request payload under `provider_request_meta` and `provider_request_body`. When debugging OpenAI `/responses` cache misses, treat those adapter fields as the final transport truth if they disagree with the higher-level `request_messages` / `tool_schemas` projection.
- The session-scoped request-artifact lane is no longer limited to visible provider sends. `visible_frontdoor`, `token_compression`, and `inline_tool_reminder` now all persist under the same `.g3ku/web-ceo-requests/<session>/...json` family so request-forensics can explain ordinary sends and internal subrequests from one timeline.
- Each saved frontdoor request artifact now also carries normalized `usage`, `frontdoor_history_shrink_reason`, and `frontdoor_token_preflight_diagnostics`. Maintainers should use those fields to compare "what preflight thought would be sent" against "what the provider actually billed," rather than reconstructing that relation from transcript timing alone.
- To preserve prefix reuse inside one visible CEO turn, same-turn request growth now follows: previous request body -> newly appended assistant/tool transcript -> newest contract snapshot. This means the actual request may contain multiple contract snapshots during one turn, but the durable post-turn transcript must still strip them back out.
- `frontdoor_canonical_context` remains durable single-writer state. Turn finalization may merge completed-stage data into it, but session sync and request assembly must not write a visible projection back into the durable chain.
- Provider-facing `tools[]` now have a separate stability rule from agent-facing callable tools: keep a stable superset aligned to exposure-visible concrete tools, keep the schema minimal, and let stage gating or hydration promotion change only the tail runtime contract unless the exposure set itself truly changes.
- `RuntimeAgentSession._frontdoor_request_body_messages` is now the session-owned request-body baseline for CEO/frontdoor continuity. It stores the rebuilt provider request body with dynamic contract messages stripped out, so the next round can append one fresh authoritative tail contract instead of replaying old catalogs as history.
- Fresh CEO/frontdoor turns may seed prompt assembly from that session-owned body baseline when the graph state itself does not already carry a request body. This is the append-only continuity path for visible turns; it is not an old-session compatibility fallback.
- That seed is no longer fed back into frontdoor prompt assembly as an ordinary checkpoint history candidate. Visible-turn continuation now treats it as an authoritative request-body prefix, so the next turn appends new user/runtime-tail content directly instead of asking the history selector whether the seed is "semantically complete".
- The paired `frontdoor_history_shrink_reason` field is now part of the runtime contract between prompt assembly and session persistence. Only `token_compression` and `stage_compaction` are valid reasons for a shorter next-round baseline.
- In practice, this means a visible CEO/frontdoor turn is now expected to keep growing or stay flat across turns unless the runtime explicitly records one of those two shrink reasons. A shorter baseline without an allowed reason should be treated as a runtime bug, not as normal prompt trimming.
- The authoritative request-body baseline must also survive turn finalization. When a visible CEO turn finishes with a direct assistant reply, that final assistant message is now appended onto `frontdoor_request_body_messages` before session sync, so the next visible turn does not fall back to a shorter transcript/canonical replay view.
- This visible-output append-back rule is not limited to `direct_reply`. If a visible CEO turn finishes with a user-visible `final_output` after ordinary tools such as `message`, that final assistant text must still be appended onto `frontdoor_request_body_messages`; otherwise the next fresh visible turn reopens from a stripped tool-only baseline and loses the last visible answer from continuity.
- The same baseline must also cross the restorable snapshot boundary. `inflight_turn_snapshot` / `paused execution context` now need to carry `frontdoor_request_body_messages` together with `frontdoor_history_shrink_reason`; otherwise a recreated `RuntimeAgentSession` would silently fall back to transcript/history replay and shorten the next visible turn outside the allowed shrink paths.
- Completed local CEO sessions now have a third restore source: `.g3ku/web-ceo-continuity/<session>.json`. It persists the latest authoritative request-body baseline, actual-request trace, stage/canonical/compression/semantic state, hydrated tools, and the last visible tool/skill exposure snapshot after each real provider-backed sync and after terminalized manual stop.
- `RuntimeAgentSession` should restore CEO continuity in this order: inflight snapshot first, paused snapshot second, completed continuity sidecar third, transcript/history fallback last. The completed-continuity lane is for reopened completed sessions and restart recovery; it must not override a real inflight turn or a technical paused/interrupt state.
- If a fresh visible turn finds the in-memory baseline empty but the paused snapshot still has it, prompt assembly may recover that saved request-body baseline from the paused snapshot and immediately treat it as the session-owned append-only prefix. Maintainers should debug that as baseline restoration, not as a normal history compaction path.
- Manual pause now has an extra token-compression continuity rule: if a visible turn pauses while the final preflight is already about to enter `token_compression`, the next fresh visible turn may recover `frontdoor_history_shrink_reason=token_compression` from the pending shrink marker or from the follow-up internal `token_compression` artifact linked through the previous actual-request history. This is the allowed race-resolution path for "pause right as compression starts," not arbitrary prompt trimming.
- During a visible CEO/frontdoor turn, the session-owned request-body baseline must also keep up with real request growth: after each provider call it should reflect the latest provider request body with contract messages stripped out, and after each tool cycle it should fold the new assistant/tool transcript back into the same baseline before the next round.
- When that baseline is reused as `request_body_seed_messages`, prompt assembly must preserve structural tool-call records as well as plain text. In particular, an assistant message with empty text but non-empty `tool_calls` is still part of the authoritative provider request body and must not be dropped as an "empty" history record.
- Maintain the boundary between "prepare-only planned request" and "real provider-backed request body". `prepare_turn` may compute a new planned body for the current fresh visible turn, but that planned body must not replace the session-owned cross-turn baseline until a real provider request has been persisted for that turn. Otherwise a manual-pause/no-provider turn can steal the next turn's cache prefix even though no real request was ever sent.
- In practice, new maintainers should treat `frontdoor_request_body_messages` and `frontdoor_actual_request_*` as a pair: cross-turn baseline replacement is only valid once the state already carries real actual-request evidence for that turn. Planned prompt-cache diagnostics alone are not enough to claim the baseline has advanced.
- The same pair also matters at turn finalization: after a real provider request has already been persisted for the current visible turn, a later `finalize` update may extend the session-owned request-body baseline with the direct assistant reply even if that finalize payload no longer repeats `frontdoor_actual_request_path/history`. Maintainers should treat that as "same-turn completion of an already authoritative baseline", not as a fresh planned-body overwrite.
- For ordinary fresh visible turns, the first rebuilt provider request may now borrow the previous turn's persisted actual-request scaffold to preserve provider-side prefix reuse, while still keeping the durable session baseline in stripped/finalized form. This is intentionally asymmetric: the scaffold is a request-construction aid for the first fresh turn only, not a new durable history source of truth.
- Reopened completed sessions now reuse that same scaffold rule by restoring the last completed turn's actual-request trace into current session state and letting the next fresh visible turn roll it into `previous_*`.
- The stricter bridge rule is now: only when `visible_tool_ids` and `visible_skill_ids` exactly match the completed continuity snapshot may that reopened first hop also reuse the previous `provider_tool_schema_names` and `cache_family_revision` anchor. Any visible-set drift should preserve context but allow cache miss.
- Runtime config refresh now also matters for in-flight retry loops. CEO/frontdoor provider retries and node execution provider retries no longer keep hammering the old model chain forever after a model-binding change has been applied to the current process.
- The intended rule is: a request already in flight is not hot-swapped mid-attempt, but if the runtime enters provider-failure or empty-response retry sleep and the runtime model revision changes before the next retry, the old retry loop is invalidated and the current round restarts with freshly resolved model refs.
- For CEO/frontdoor, that restart happens inside the current `call_model` round and the updated `model_refs` are written back into graph state for later rounds in the same visible turn.
- For node execution, the same invalidation restarts the current `ReActToolLoop` model round so `model_refs_supplier()` can re-read the latest execution/inspection model chain before the next dispatch.

## 7. 新人阅读顺序建议

建议按下面顺序读运行时源码：

1. `g3ku/runtime/bootstrap_factory.py`
2. `g3ku/runtime/manager.py`
3. `g3ku/runtime/bridge.py`
4. `g3ku/runtime/session_agent.py`
5. `g3ku/runtime/frontdoor/prompt_builder.py`
6. `main/service/runtime_service.py`
7. `main/runtime/node_runner.py`

不要一开始就从 `main/runtime/react_loop.py` 入手，否则会看到大量局部机制，却不知道谁在驱动它。

## 8. 维护高风险区域

- `g3ku/runtime/session_agent.py`
  风险点：pause/resume、heartbeat internal turn、transcript 持久化、error recovery 彼此强耦合。

- `main/service/runtime_service.py`
  风险点：工具集、治理、日志、worker、内容服务等职责集中，任何小改动都可能影响广。

- `main/runtime/node_runner.py`
  风险点：节点状态机、spawn child、acceptance node、pause/cancel 恢复逻辑很细。

- `g3ku/runtime/frontdoor/`
  风险点：提示词、上下文组装、tool/skill 可见性改变后，agent 行为会明显变化。

## 9. Memory Runtime Notes

The long-term memory runtime is no longer journal-first or structured-fact-first, and it is no longer a rule-only batch rewriter.

- `memory/MEMORY.md` is now the only committed long-term memory source of truth.
- `memory/notes/` stores optional detailed note bodies referenced by `ref:note_xxxx`.
- `memory/queue.jsonl` stores the single durable queue, including per-request processing state such as `pending`, `processing`, retry timing, and the latest error text.
- `memory/ops.jsonl` now stores only successfully applied batches. Engineering failures remain visible through the queue head state instead of being copied into a second “failed history” lane.
- Dense/sparse catalog projections may still exist for tool/skill narrowing, but they are no longer the long-term memory truth source.

The runtime boundary changed in five important ways:

- CEO/frontdoor turns now read one frozen `MEMORY.md` snapshot at prompt-assembly time and inject it directly into the turn overlay.
- Same-turn hot memory updates do not change the current prompt; newly committed memory only affects later turns.
- `memory_write` and `memory_delete` now only enqueue requests into the single memory queue. They do not mutate committed memory inline.
- `RuntimeAgentSession` and session-delete paths now enqueue autonomous review, pre-compression flush, and session-boundary flush requests instead of writing structured memory facts directly.
- A dedicated internal memory agent now consumes the queue in FIFO same-op batches (`write` and `delete` never mix within one batch), runs with the `memory` model route, and only has a restricted tool surface for reading/writing `MEMORY.md` and note files.
- The queue consumer is now single-active across processes: every `run_due_batch_once()` attempt must first take a workspace-local memory-worker file lock, so extra web/worker processes become passive observers instead of double-consuming the same queue head.
- The consumer also treats `request_id` as the durable idempotency key. Before processing a batch it drops any queue rows whose `request_id` already appears in `memory/ops.jsonl`, so a stale duplicate row should not create a second successful processed batch.

Maintainers should read the queue state machine like this:

- `pending` means the request has not yet been claimed into a batch.
- `processing` means the queue head batch is currently owned by the single memory worker.
- If a process cannot take the memory-worker file lock, it should leave the queue untouched and report `worker_lease_unavailable`; this is normal in multi-process deployments where only one runtime instance should actively consume memory writes.
- If the `memory` role is unconfigured or a provider call fails, the queue head remains `processing` with an attached error and blocks later requests.
- A persisted `processing` head is durable restart state, not proof that a worker is currently live. After restart, the worker waits until the stored `retry_after` before retrying that same head batch.
- `processing_started_at` records the first successful claim of that queue head batch and must stay stable across later retries; it is not a "last retry time" field.
- Semantic-invalid model output is expected to repair inside the same processing batch before the runtime commits anything.
- Two successful `ops.jsonl` rows with the same `request_id` are now treated as a bug signal pointing to historical multi-worker contention or an old pre-fix run, not as normal repeated writes.

Maintainers should treat transient execution state as explicitly out of bounds for long-term memory:

- pause/resume control data
- in-progress task status
- temporary repair markers
- runtime-only coordination notes

Those belong in transcript, session, task, or stage runtime state, not in `MEMORY.md`.

Tool/skill catalog narrowing now goes through a catalog-only bridge and no longer delegates to the old `rag_memory` runtime. However, the catalog projection still lives under the same `memory/` tree, so a destructive reset of `memory/` may still remove catalog retrieval data together with user memory content. Catalog sync must be rebuilt after startup.

## CEO Frontdoor Legacy Compression Removal

The CEO frontdoor no longer keeps a separate legacy history-compaction layer.

- The earlier `_summarize_messages()` compatibility hook has been removed from runtime execution paths.
- Frontdoor history now reaches the model either as local workset stage windows/blocks or through a same-turn `token_compression` rewrite when request size approaches the selected model window.
- If you are debugging long-context behavior, there is no intermediate "message-count compaction" stage to inspect anymore.

This leaves two distinct mechanisms only:

- Stage workset compaction for the near-field prompt, shared with the execution-stage prompt logic.
- Inline `token_compression` for older body-history when the final provider-bound request approaches the selected model's `context_window_tokens`.

For the CEO/frontdoor path, the near-field stage workset now has a stricter source-of-truth split:

- Retained raw stage replay is rendered from `frontdoor_canonical_context` plus the current turn `frontdoor_stage_state`.
- Transcript / checkpoint history no longer serves as the authoritative source for uncompressed stage replay. It still carries non-stage conversational continuity and global-summary source material.
- Older completed stages still enter the prompt as `STAGE_COMPACT` / `STAGE_EXTERNALIZED` blocks, but those blocks now come from the canonical context chain rather than from historical assistant trace payloads.
- The retained completed-stage window is computed from the combined canonical stage view for the session, while the current turn stage ledger remains a separate runtime-only write log until turn finalization.
- Round-level tool records now preserve normalized raw `arguments` together with the existing output fields. Small outputs remain inline in `output_text`; large outputs still stay externalized as `output_ref` plus `output_preview_text`, and the prompt renderer does not read artifact bodies back inline.

When a maintainer sees a prompt continuity issue, the first questions should now be:

- Was the relevant context still inside the retained stage workset?
- If not, did inline `token_compression` or `stage_compaction` legitimately shorten the next-round baseline?

## Internal Turn Contract Notes

Heartbeat and cron turns now share the same strict internal-turn contract.

- They still execute through `RuntimeAgentSession.prompt(...)` with their own internal source metadata.
- Heartbeat / cron turns still clear live-only frontdoor debug surfaces such as `frontdoor_selection_debug` and per-round actual-request pointers, but they no longer blindly zero the session-owned request-body / stage / compression continuity state before prompt assembly.
- When an internal turn starts with no graph-local request body, CEO/frontdoor may seed prompt assembly from the session-owned `frontdoor_request_body_messages` baseline plus the saved stage/compression snapshots. However, `heartbeat_internal` using the dedicated `ceo_heartbeat` prompt lane is allowed to rebuild a much shorter internal prompt body than the visible-turn baseline; maintainers must not treat that lane-local shortening as a visible-turn context-loss regression.
- Service-layer code must not auto-retry tasks or synthesize fallback assistant replies on behalf of the model.
- An internal turn that ends with `HEARTBEAT_OK` may surface a live-only UI ACK event, but it does not become transcript history.
- Maintainers should distinguish live UI state such as inflight snapshots and internal ACK bubbles from durable assistant transcript messages.

## Repeated Tool Call Guard Notes

Execution-stage duplicate-call handling in `main/runtime/react_loop.py` now uses a soft-reject path instead of escalating directly to an engine failure.

- When the model repeats the same ordinary tool signature several consecutive times in the same node, the runtime records the repeated assistant tool call, appends an error tool message explaining that the call is duplicated, and lets the next model turn repair itself.
- This means a task should no longer fail immediately with `RuntimeError: repeated tool call detected: ...` only because the model repeated a non-control tool call such as `exec` with identical arguments.
- Read-only duplicate retrieval guidance (`content.open/search`, `task_progress`, etc.) is still a separate guard path with its own repair messaging and escalation semantics.
- If a maintainer is debugging a "stuck on the same tool" report, first inspect the latest tool/error messages in the node transcript and runtime frame before assuming the task was terminated by the runtime itself.

The filesystem family no longer participates in that read-only retrieval branch.

- `filesystem` is now a family/context id only, not a live callable tool.
- The execution runtime only hydrates concrete mutation executors such as `filesystem_write`, `filesystem_edit`, `filesystem_copy`, `filesystem_move`, `filesystem_delete`, and `filesystem_propose_patch`.
- This means repeated filesystem mutations now follow the ordinary duplicate-call soft-reject path, while retrieval-style guards remain reserved for `content_*`, `task_progress`, `task_node_detail`, and similar read-only tools.

## Resource Generation Checks For Semantic Catalog Freshness

The runtime still does not keep a full watcher on `skills/` and `tools/`, but semantic catalog freshness is no longer tied only to explicit admin reloads.

- `MainRuntimeService` keeps the last known top-level resource tree fingerprint state.
- The CEO/frontdoor assembly path and node-context selection path now perform a throttled external resource generation check before semantic catalog narrowing.
- The check interval is bounded by `resources.reload.poll_interval_ms`; the runtime does not rescan without limit on every low-level access.
- When fingerprints differ, the service refreshes only the changed roots and then performs a targeted catalog sync for the changed resource ids.

This gives maintainers a middle ground:

- The resource runtime itself is still manual/release-triggered and does not promise instant discovery.
- Runtime-facing semantic selection can still reconcile direct disk edits lazily on the next bounded generation check.
- Metadata-only edits such as `display_name` / `description` changes now also invalidate the catalog summary hash, so catalog `l0` / `l1` no longer stay stale just because the正文 body stayed the same.

## CEO Frontdoor Canonical Context Contract

The CEO/frontdoor path now has a single cross-turn stage truth source: `frontdoor_canonical_context`.

- `frontdoor_stage_state` and `compression_state` are still runtime working state, but CEO/frontdoor no longer assumes they must be blanked at every fresh user / heartbeat / cron turn before prompt assembly.
- When graph-local state is empty, `prepare_turn` may now reuse the session-owned frontdoor request body plus these session snapshots to rebuild the next provider request window.
- `frontdoor_canonical_context` is the durable cross-turn stage/history view. Turn-finalization merges the current turn stage ledger into this canonical structure.
- Session/runtime sync must not persist a request-local projection back into `frontdoor_canonical_context`. Only turn finalization is allowed to append completed-stage data into the durable canonical chain; anything derived from `frontdoor_canonical_context + current frontdoor_stage_state` is visible-workset data for the current request only.
- Prompt assembly no longer rebuilds retained stage history from transcript `execution_trace_summary` or flat `tool_events`.
- The near-field stage workset is derived from `frontdoor_canonical_context + current frontdoor_stage_state`.
- If the current turn stage state already contains a completed stage that is materially identical to one already present in `frontdoor_canonical_context`, prompt assembly must treat it as overlap and skip rebasing it into a new synthetic stage id. This prevents one completed stage from exploding into duplicated raw stage blocks across fresh-turn rebuilds.
- Session state still owns the durable full `frontdoor_canonical_context`, but UI-facing turn payloads now expose a current-turn `canonical_context` slice rather than `execution_trace_summary` / `tool_events`.
- In other words: prompt assembly reads the durable cross-turn canonical context, while inflight / paused / final-reply payloads should describe only the visible turn's own stage trace.

There is now a second explicit continuity contract for prompt assembly:

- `frontdoor_request_body_messages` is the canonical session-owned provider request body baseline for the next CEO/frontdoor round.
- That baseline intentionally excludes `frontdoor_runtime_tool_contract` messages; dynamic tool/skill exposure must still be rebuilt as a fresh tail contract for each round.
- Prompt assembly is allowed to reduce this baseline only at the two documented information-loss boundaries: `token_compression` and same-turn `stage_compaction`.
- For fresh visible CEO/frontdoor turns, this session-owned request body baseline now has priority over any graph-local checkpoint-style stage replay projection. If both exist, prompt assembly must continue from the body baseline instead of rebuilding a new main prefix from stage replay.
- If the next baseline is shorter for any other reason, `prepare_turn` treats that as unexpected context loss and fails fast instead of silently continuing with a truncated request.

Maintainers should treat the canonical context representation rules as the only allowed information-loss boundary:

- Latest 3 completed normal stages remain `raw`.
- Older completed normal stages become `compact`.
- When completed normal stages exceed 20, the oldest 10 are externalized into an archive-backed compression stage.

The prompt token trace also changed:

- `pre_request_prompt_tokens` is the pre-send estimate before any inline `token_compression`, and it must include the stage workset.
- `effective_prompt_tokens` is the estimate for the final model request actually sent after prompt assembly.
- CEO/frontdoor now also runs a final token preflight immediately before provider send. This happens after fresh-turn request seeding, completed-continuity bridge decisions, provider tool-schema seeding, and frozen `MEMORY.md` injection have already produced the authoritative request projection.
- That preflight is the final gate for provider-bound request size. If it compacts the request, `frontdoor_history_shrink_reason` must be `token_compression`; the runtime must not invent a second competing shrink-reason field.
- The preflight emits additive diagnostics as `frontdoor_token_preflight_diagnostics` on graph state, inflight/paused snapshots, and completed continuity sidecars. Maintainers should treat that payload as observability for the final gate, not as a replacement for the request-body baseline contract.
- That preflight estimate must be derived from the raw provider-bound payload, not from summary-oriented serializers that truncate long fields for readability. If a huge `provider_request_body` still produces a tiny, suspiciously stable `final_request_tokens`, treat the estimator as broken before questioning the compression threshold.
- For `/responses`-style providers, the preflight estimate now uses the adapter-final payload shape rather than the pre-adapter `request_messages` projection, because the transport rewrite can materially change the final token count.
- The trigger also keeps a small safety margin below the nominal compression threshold (`effective_trigger_tokens`). This is intentional: maintainers should treat it as protection against estimator drift, not as evidence that the model context window itself changed.

## CEO Inline Tool Reminder Sidecar

CEO frontdoor direct long-running tools now have a separate inline reminder lane.

- Direct CEO tool executions register in `InlineToolExecutionRegistry` when they run without a detached `ToolExecutionManager` handoff.
- This registry is separate from detached/background tool execution semantics. Maintainers should not treat an inline execution id as a detached watchdog execution id.
- Reminder windows are fixed at `30 / 60 / 120 / 240 / 600` seconds, then repeat every 600 seconds after that.
- The old inline watchdog poll text is no longer the operator-facing mechanism for CEO direct tools. The direct tool keeps running inline, while reminder decisions happen in the sidecar lane.

The reminder lane itself is deliberately read-only with respect to session persistence:

- `CeoToolReminderService` does not call `session.prompt(...)`.
- It does not take the normal turn lock or create a heartbeat/internal turn.
- Its first choice is to reuse the latest persisted CEO actual-request artifact as the provider-facing scaffold so the sidecar request shares the same cacheable prefix as the main turn; only if that scaffold is missing does it fall back to `CeoMessageBuilder.build_for_ceo(..., ephemeral_tail_messages=...)`.
- The reminder snapshot from `RuntimeAgentSession.reminder_context_snapshot()` therefore now needs to carry the latest actual-request pointer in addition to the current visible stage/canonical view, durable `frontdoor_canonical_context`, compression state, hydrated tools, selection debug, and current visible user/assistant text.
- Sidecar stop/continue decisions are now parsed from a text-only reminder reply (`STOP` / `CONTINUE`). Reusing the main turn's provider-visible tool bundle is a cache-stability tactic, not permission to execute arbitrary returned tool calls in the sidecar lane.
- The reminder text itself is live-only. It must not be written into transcript history, canonical context, or future prompt-history injection.

### Timeout Stop Error Contract

If the sidecar decides to stop a running tool, the main turn must see that as a normal tool failure with explicit provenance.

- The registry stores `InlineToolStopDecisionMetadata` with `reason_code=sidecar_timeout_stop` and the elapsed runtime / reminder count at the stop point.
- The direct CEO tool execution path checks that metadata and normalizes the tool result into `tool_error` / `status=error`.
- The failure text is intentionally explanatory because the main agent does not share the sidecar reasoning context.

Example shape:

`Error executing exec: stopped by sidecar timeout decision after 120.4s (2 reminders).`

This is distinct from user-requested cancel/pause behavior. Only sidecar reminder stops use the timeout-stop contract. If the tool finishes successfully before the stop lands, the runtime clears the sidecar stop metadata and preserves the successful result.

## Node Provider Request Scaffold

Execution and acceptance nodes now keep two separate views of tool visibility:

- `tool_names`: the authoritative per-round callable tool set used by runtime contract enforcement.
- `provider_tool_names`: the provider-facing schema bundle used when constructing the actual model request.

This separation exists to improve prompt-cache stability without weakening stage gates or tool hydration rules.

### Same-Turn Append-Only Rule

Within one node's multi-round loop, provider-facing `request_messages` should now prefer:

1. the previous actual request body,
2. plus the newly produced assistant/tool result messages from the last round,
3. plus the newest overlay / `node_runtime_tool_contract` tail.

Maintainers should treat this as a request-construction scaffold only.

- It does not replace the node's durable/compacted `message_history`.
- It does not redefine which tools are callable.
- It exists purely so the provider sees append-only growth instead of an early-prefix rewrite whenever stage compaction trims the active window.

## Frontdoor Context Compression (Current Contract)

The current CEO/frontdoor request-shrink model has only two information-loss boundaries:

- `stage_compaction`
- `token_compression`

Anything else that shortens the next-round request baseline should be treated as a regression.

### `token_compression`

- `token_compression` is an inline same-turn LLM rewrite that runs immediately before the provider send.
- It keeps the stable system prefix, the latest runtime tool-contract tail, and the most recent body-history tail, then rewrites only the older body-history zone into one `G3KU_TOKEN_COMPACT_V2` marker block.
- The trigger threshold is tied to the runtime-selected model's `context_window_tokens`, not to a separate semantic-summary config.
- If estimated request size is `<= 80%` of the selected model window, frontdoor sends directly.
- If estimated request size is between `80%` and `100%`, frontdoor attempts one inline compression.
- If estimated request size is already `> 100%`, frontdoor fails before compression because even the compression attempt would not fit safely in the current model window.

### Removed Semantic Summary Path

- The older semantic/global-summary lane is no longer part of prompt assembly.
- `compression_state` now means live progress for inline `token_compression` only; it is no longer a durable "semantic summary ready" signal.
- Continuity restore now depends on the authoritative frontdoor baseline, stage state, request traces, and shrink reason, not on a separate `semantic_context_state` handoff block.

### Pause During Compression

- Manual pause during inline compression is terminal for the visible turn.
- The runtime cancels the active compression generation and discards any late compression result instead of letting it update baseline or continue into the main provider send.
- The next activation (new user input, heartbeat wakeup, etc.) must re-run prepare -> estimate -> optional compression -> send using the then-current model chain and context window.
