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
  - SQLite checkpointer maintenance for short-term thread history. Treat it as a bounded per-thread cache rather than an append-forever log: CEO session deletion purges that session key from the checkpointer (as a background task after the delete response), and the engine opportunistically trims older checkpoints per `(thread_id, checkpoint_ns)` while preserving only the newest retained rows. VACUUM is operator-triggered only — see `operations-and-maintenance.md`「SQLite Checkpointer Capacity Governance」.
  - bootstrap bridge 初始化默认工具和多 agent 运行时

- `g3ku/runtime/manager.py`
  `SessionRuntimeManager`。按 `session_key` 复用 `RuntimeAgentSession`，是所有入口共享的 session 路由器。

- `g3ku/runtime/bridge.py`
  `SessionRuntimeBridge`。给 Web、CLI、China bridge、cron 提供统一的 prompt / prompt_batch / continue / cancel / pause API。pause 带运行状态前置检查（空闲会话返回 0，避免空闲暂停产生多余转录归档）；China bridge 的 QQ 渠道用它实现端内「暂停」命令与运行中消息注入（详见 `china-channels.md`「QQ 渠道增强」）。

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

在当前 Web CEO 路径里，`RuntimeAgentSession` 还维护“当前可显示 turn 的身份”，核心不变量：

- 每个可显示的 inflight turn 都有稳定 `turn_id`：`inflight_turn_snapshot`、`message_end`、heartbeat discard/final reply 都沿这个 `turn_id` 传递；`inflight_turn_snapshot()` 只表达当前真实在跑的 turn，等待 `ceo.turn.discard` 收口的旧可见气泡放在单独的 preserved snapshot（live payload 里 `inflight_turn` 与 `preserved_turn` 可并存）。`turn_id` 传播断裂的典型回归是同 source 的多个 pending turn 被错误合并、heartbeat 清理误删旧 turn、前端残留只有“处理中...”的气泡。
- session 侧同步保存当前 turn 的 hydrated tool state 与 `frontdoor_selection_debug`；它们和 stage trace 一样属于“当前进行中 turn 的运行时事实”，不是长期 transcript。candidate/hydrated 默认上限与诊断读取方式详见 `tool-and-skill-system.md`「四个概念必须分清」。

另外两条不变量：

- 手动 pause 的语义是“冻结上一轮”，不是“等待下一条输入来补写原请求”：session 以 `completed` + `stop_reason=user_pause` 收尾，pause 当下的轮次上下文照常持久化，收尾时立即写 completed continuity sidecar，并把 paused assistant 气泡归档成带 `status=paused`、`history_visible=false`、`source=manual_pause_archive` 的 durable 记录。后续输入必须作为新一轮 user turn 发送，不得走 `resume(additional_context=...)`。被暂停回合的用户消息经续跑种子对账继承进下一轮模型上下文，即使暂停发生在任何 provider 请求发出之前；对账规则详见 `context-and-cache-troubleshooting.md`「Baseline 合同与恢复顺序」。残留的 paused 转录条目随下一个用户可见回合正常完成被对账退役一次；退役边界与反复注入风险详见 `context-and-cache-troubleshooting.md`「残留 paused 转录条目」。
- 运行中补充的消息作为一批独立 user message 持久化，在同一轮下一次 `call_model` 前一起注入，不拼接成一条文本；可见用户顺序的权威是 `inflight_turn.user_messages` 与 `ceo.reply.final.user_messages`（兼容字段 `user_message` 只保留批内最后一条），`pending` user rows 只是 durability/continuity 记录。

手动暂停恢复规则、排队补充消息与 follow-up 消费的完整契约详见 `web-and-admin.md`「Manual Pause Resume Rule」与「Queued Follow-Ups」。

可以把它看成“一个会话的状态机 + turn 执行器”：名字看起来像简单 session 封装，实际上 user turn、heartbeat / cron internal turn、async dispatch 错误恢复、paused execution context 与 frontdoor stage / hydrated tool state 都在这里汇合。

## 4. frontdoor 与任务运行时的关系

G3KU 并不是所有问题都在 CEO 单次对话内完成。frontdoor 的职责更像是：

- 识别当前用户请求
- 组织提示词与上下文
- 判断当前这轮能直接回答，还是需要走任务运行时
- 在必要时触发任务工具，如 `create_async_task`

`create_async_task` / `task_append_notice` 的完整工具契约与守卫（重复预检与重验、`file_targets` reopen 车道、拒绝语义）详见 `tool-and-skill-system.md`「fixed builtin tools」。运行时记账与任务级控制合同要点如下：

- frontdoor dispatch 记账必须区分“工具调用发生了”与“新任务真的创建了”：只有显式成功形式才算已核实派发；拒绝消息里提到的旧 `task:...` id 不算新建任务。同一可见轮可以多次调用 `create_async_task`，是否真正创建由 `MainRuntimeService` 的唯一创建路径决定。
- 运行时把追加的任务消息视为任务级控制事务：`MainRuntimeService.task_append_notice(...)` 把当前执行树快照进 epoch payload，请求任务级 barrier，并写入操作员可见的 `runtime_meta.distribution`。
- Public distribution states are `barrier_requested -> barrier_draining -> distributing -> resume_ready`; the authoritative source is the task-level distribution state plus the epoch payload's `barrier_node_ids` / `drain_pending_node_ids`. `barrier_draining` is safe-boundary only: running leaves are not hard-interrupted, and a node drains only at an existing safe boundary (`before_model`, `waiting_tool_results`, `after_model`, `waiting_children`, `waiting_acceptance`). Draining also waits for active spawn rounds to finish materializing child execution nodes; distribution must not snapshot a partially materialized child round. `NodeRunner.reconcile_spawn_entry_child_bindings(...)` repairs durable parent-entry / child-row drift before spawn execution and barrier checks, binding the canonical child back into the parent entry and marking non-canonical siblings `duplicate_spawn_child` so they are excluded from the live tree.
- Distribution turns are compact control turns (`node_message_distribution.md` + internal tool `submit_message_distribution`) that run through the ordinary `TaskActorService` / `TaskNodeDispatcher` / `NodeRunner` path and the same node send-side token preflight. `submit_message_distribution.children` is a complete per-child decision set: every live child appears exactly once with one of three actions — `distribute` (non-empty `message` delivered, child advances the frontier), `skip` (nothing delivered, non-empty `reason`), or `terminate` (the child's whole subtree is cancelled and forced terminal and its spawn entry marked terminated, non-empty `reason`; the child receives no message and is excluded from the frontier). `action` is authoritative when present; without it, `should_distribute` maps true→distribute and false→skip. Empty or partial decisions fail the control turn. The whole decision set is processed in a single synchronous pass, so distributing to some children and terminating others is decided within the same turn. On `/responses`-style providers the control turn uses the flat function selector form for `tool_choice`. When a distribution turn produces `next_frontier_node_ids`, the task is re-enqueued immediately; each frontier turn may accumulate an append-only `payload.debug_trace` for control-turn forensics.
- Delivery to child execution nodes creates durable mailbox rows; delivering to a terminal execution node reactivates it and invalidates/detaches its old acceptance node instead of deleting it. Root appended messages keep node-local pending notice records plus epoch `decision_records`, not root mailbox rows. A notice flips to consumed as soon as the first ordinary model response that included those notice ids returns for the resumed node turn; until then it stays pending/delivered, including across `waiting_children` pauses. Ordinary execution re-scans newly delivered notices at each `before_model` safe boundary via append-only refresh, never a history rebuild. Consumed notices persist into node-local `append_notice_context` so they survive stage compaction; spawn review reads them as `consumed_distribution_notices` and treats the latest consumed distribution notice as the effective current requirement when it conflicts with older wording. Raw unconsumed notices render as a dedicated non-compressible tail block ahead of stage compact blocks (`STAGE_COMPACT` 与遗留 `STAGE_EXTERNALIZED`)；历史数据中已存在的归档压缩段（`compression_segments`）继续按压缩通知尾段渲染。
- Epoch completion and delayed notice consumption are separate states: once the epoch reaches `completed`, `runtime_meta.distribution.state/mode/active_epoch_id` are cleared even while `pending_notice_node_ids` / `pending_mailbox_count` remain — those fields only mean nodes still hold node-local pending notices or delivered mailbox rows, not that barrier/distribution is still active. Nodes blocked by `runtime_meta.distribution.blocked_node_ids` skip interrupted-turn recovery until the barrier releases them. A node holding an incomplete `spawn_child_nodes` round resumes with `resume_mode=wait_for_children` and consumes its held notice only after that round disappears (durable notice now, prompt consumption later). The first ordinary consumption of a held notice preserves the previous provider-facing request as an exact prefix: semantic node history comes from `runtime_frame.messages` / durable rebuild, while the latest actual request body is only a send-side seed scaffold.
- Acceptance nodes are created eagerly with the spawn entry (root `final_acceptance` at task creation) and activated by execution success. Execution success is not terminal when acceptance is required: `submit_final_result(success + final)` first persists the candidate result and moves the node into `acceptance_handshake.state="waiting_acceptance"`. Child and root acceptance share one reflation loop: the first and second rejections feed acceptance feedback back into the execution node and reactivate it, the third rejection is terminal, and execution failure during any retry cancels the waiting acceptance path. `NodeRunner._run_child_pipeline(...)` loops `execution -> acceptance` until acceptance returns a terminal result. Acceptance prompt/input refresh happens at activation time from the latest execution output (`result_payload_ref`). Root final acceptance adds two guards against notice/submission races. Freeze: while the inspected root execution node still holds an unconsumed notice (node-local pending notice record or delivered mailbox row), the final acceptance node is held — `NodeRunner.run_node` returns a deferred `partial` before any model turn even if a verification notification is delivered, so acceptance cannot reach a verdict ahead of the execution node consuming its notice and resubmitting; the hold releases once the execution node has no pending notices. Terminal reset: if the execution node submits `success + final` while its final acceptance node is already terminal (`success`/`failed`), the runtime resets that acceptance node to `in_progress` (clearing the prior verdict) and proceeds with the normal handshake so the new submission is re-verified, instead of delivering a verification notification to a node that can never consume it. Both guards are root-final-acceptance only; child spawn acceptance is driven synchronously by the child pipeline and is unaffected.
- Task-depth governance review is an execution-only inspection lane: acceptance/inspection nodes are filtered out of trigger stats and payload, each execution node's full `prompt` is the primary leaf-work evidence, and each review attempt writes its own `kind=task_governance_review` artifact referenced by `task_runtime_meta.governance.history[*].review_artifact_ref`.
- Force delete wins over message distribution: before task rows disappear, the runtime cancels active epochs, cancels or purges mailbox rows, clears `runtime_meta.distribution`, and prevents later queue wakeups from resuming the deleted task.

对于异步任务的回传：任务终态通过 task terminal callback / heartbeat 回到原 CEO 会话；heartbeat 的修复/回退语义与 `terminal_output` / `root_output` 双车道详见 `heartbeat-system.md`「Task Terminal Repair Contract」。

当前 frontdoor 的上下文组织以阶段工作集为近场上下文：最近 3 个完成普通阶段与当前 active 阶段保留完整原始窗口（含工具调用），更早的完成普通阶段按阶段归属原位移除工具调用并以 compact 块回插原位，阶段之外的用户可见对话原位保留（表示规则见本文「Runtime Contract Lane」）。归档压缩阶段（`stage_kind="compression"`）是历史遗留表示数据：继续规范化与渲染为外置块，运行时不产生新的归档。全局语义摘要层不参与 prompt assembly；长会话的远场连续性由权威请求体基线、canonical context 链与压缩合同承担，收缩边界详见本文「Frontdoor Context Compression (Current Contract)」。

前门提示词分成“静态协议层”和“动态注入层”两部分理解：

- `g3ku/runtime/prompts/ceo_frontdoor.md` 承载 CEO frontdoor 的稳定协议（角色规则、任务/工具通用约束、stage-first 高优先级协议）；稳定 system prompt 只保留最小的 capability exposure revision 锚点，不把可见 tool/skill 名单写进稳定前缀。
- `g3ku/runtime/frontdoor/prompt_builder.py` 负责把稳定协议与少量环境提示装成 base prompt；`g3ku/runtime/frontdoor/message_builder.py` 按本轮会话状态动态注入 retrieved context、memory hint 与当前轮运行时工具合同所需的数据。
- CEO/frontdoor 的生产执行面是显式 `StateGraph`，入口到收尾固定经过 `prepare_turn -> call_model -> normalize_model_output -> review_tool_calls -> execute_tools -> finalize`。`call_model` 和 `execute_tools` 共用同一份 frontdoor runtime tool bundle；`submit_next_stage` 这类运行时注入的 stage protocol tool 必须同时对模型“可见”且在 `execute_tools` 里可真实执行，执行环节不得从 `state.tool_names` 重建第二套工具表。阶段门控与 mixed-batch 语义详见 `tool-and-skill-system.md`「四个概念必须分清」。
- 可见 `call_model` 轮次有专用的流式 assistant 文本 lane，`RuntimeAgentSession` 是其合并边界：流式块追加进 `latest_message`，`inflight_turn_snapshot().assistant_text` 保持最新，并以节流轻量事件代替整份 `state_snapshot` 重建；该 lane 只承载文本，工具进度 / stage trace / canonical context 仍走各自的低频事件/快照通道。可见发送的硬回退边界：首个可见流式文本块出现前允许 provider retry / API-key rotation / model-chain fallback，首个可见块之后同一发送必须停止透明回退——一个可见气泡由多个 provider/model attempt 拼接属于 runtime bug。内部不可见发送（如 `token_compression` helper）不得复用该可见回调路径。
- 内部运行时错误后的 async-dispatch 恢复遵守同一可见性规则：若 `create_async_task` 已成功且当前轮已有用户可见 assistant 文案，恢复必须保留该文案；通用回退文案仅适用于没有任何可见文本幸存的窄场景。
- 只要当前没有“有效阶段”（`active_stage_id` 为空，或当前阶段已 `transition_required=true`），agent-facing `frontdoor_runtime_tool_contract.callable_tool_names` 收紧到只剩 `submit_next_stage`；这些只影响模型决策边界，不同步收紧 provider-facing `tools` schemas——前门继续发送当前路径上 RBAC-visible concrete tools 对应的 `provider_tool_names` bundle，仅在 membership 真正变化时刷新并保持已持久化顺序稳定，`token_compression` 所在 send 沿用压缩前已持久化的 bundle。阶段门控、候选/修复车道与 provider 工具面详见 `tool-and-skill-system.md`「四个概念必须分清」与「CEO Provider Tool Surface」。
- `g3ku/runtime/frontdoor/_ceo_create_agent_impl.py` 是 runner 入口，但前门主执行链以 `_graph_*` 节点为唯一权威路径。
- 对 CEO/frontdoor 主链路，每个请求只有一份最新契约：位于所有稳定前缀与持久化历史之后、当前 user 回合之前的一份 `frontdoor_runtime_tool_contract` user 消息；模型输入末位保持当前 user 回合，避免契约块占据末位被模型当作“上一条发言”回显。它属于“当前轮临时合同”，不是 durable history（剥掉/重注规则详见 `context-and-cache-troubleshooting.md`「append-only 规则」）。维护上区分 `dynamic_appendix_messages`（下一次重建时应追加的最新合同）与活动中的 `messages` / actual request JSON。

frontdoor / 节点的工具状态分两层，且都由持久化状态维护而不是只存在于某一轮 prompt 文本里：`candidate_tool_names` / `candidate_skill_ids` 是“RBAC 可见 ∩ 语义召回命中”的当前候选集合（语义召回不可用时退化为 RBAC 可见集合，不报错中断）；`hydrated_tool_names`（节点侧为 `hydrated_executor_state` / `hydrated_executor_names`）是本轮成功读取契约后被提升为下一轮 callable 的 concrete tool 集合。节点侧 canonical state 落在 runtime frame，是节点生命周期级 LRU，跨阶段切换 / pause/resume / restore 保留；frontdoor 侧落在 `RuntimeAgentSession._frontdoor_hydrated_tool_names` 加前门 persistent state，是 session 生命周期级 LRU，跨 turn 保留但每轮按当前 RBAC 可见集合过滤。两侧 LRU 只接受 concrete tool names；restore / recovery 只认 canonical frame / session state 中的 callable/candidate/hydrated/skill 字段，缺失时直接报“运行时工具合同损坏/缺失”。另有一条运行时边界：tool result 的错误判定不止 `Error: ...` 文本，任何 top-level JSON 带 `ok=false` 的工具结果 payload 都进入节点执行与 CEO/frontdoor 的 error lane。fixed builtin / candidate tools / candidate skills / hydrated tools 四个概念的完整区分、loader 准入与预算合同详见 `tool-and-skill-system.md`「四个概念必须分清」。

### CEO Frontdoor Round Tool Ownership

CEO/frontdoor 路径上，`frontdoor_stage_state.stages[].rounds[].tools` 是“哪些工具调用属于某一轮”的权威记录：

- `_frontdoor_stage_state_after_tool_cycle()` 在工具循环完成时写入精确的 round 级工具记录。每条记录携带稳定身份（`tool_call_id`）与展示字段（`tool_name`、`status`、`arguments_text`、`output_preview_text` / `output_text`、`output_ref`、`timestamp`、`kind`、`source`）；`tool_names` / `tool_call_ids` 是派生提示，不是第二真相源。
- stage/round 账本还携带展示文本：每个 round 记录有 `text`（该循环的轮中叙述），`submit_next_stage` 创建的阶段携带 `preamble_text`（随阶段调用发出的叙述，属于新阶段并渲染在其上方）。两者都是展示导向、只服务 Web 时间线，必须存活于每一个归一化跳板（`_frontdoor_stage_state_snapshot`、`canonical_context.py`、`raw_stage_renderer.py`），且不得喂给 prompt 组装或转录权威链。
- durable 转录每轮只存最终 assistant 文本；每轮叙述在 reload 时从 assistant 条目上持久化的 `canonical_context` 恢复。
- `RuntimeAgentSession` 的 `latest_message` 只保存最新一段思考的 assistant 文本：模型调用开始标记段边界但不清空驻留文本，新一段首个流式 delta 到达时整体覆盖；`analysis` 进度事件直接替换驻留文本；UI 时间线从 stage/round 记录重建，`latest_message` 只是预览/回退气泡。

`RuntimeAgentSession` 重建 `canonical_context` 时的合同：

- round 已有 `tools` 时，直接信任 `round.tools`。
- 更老的 round 只有 `tool_call_ids` 时，只按精确 `tool_call_id` 回填。
- 仅按 `tool_name` 匹配是回归风险：会把后面同名的工具结果偷进更早的 round。若发现更晚的 `exec` 出现在更早的 round，先检查存储的 round 是否缺 `tools`、持久化的 `tool_call_ids` 是否稳定且唯一。

前门 tool promotion 与阶段工具显示是两条平行链路：执行循环直接基于 `raw_result` 处理 `load_tool_context` 的成功返回（不从 trailing `ToolMessage` / `result_text` 反推 hydration），`_frontdoor_stage_state_after_tool_cycle()` 只负责 round 记账与 `round.tools` 落盘；成功 payload 附带的 `tool_context_fingerprint` 只服务运行时 freshness / duplicate-read 判定，不属于 provider-facing schema 或 durable business state。`g3ku/runtime/frontdoor/ceo_agent_middleware.py` 只保留兼容/测试价值，线上行为排查优先看显式图节点与 checkpoint state。

维护上，动态 skill/tool 提示块里的说明不能覆盖 `ceo_frontdoor.md` 的 stage-first 协议。权威顺序是：无活动阶段时必须先经 `submit_next_stage` 进入新阶段（可以单独一轮，也可以是以 `submit_next_stage` 起手、紧跟普通工具的 mixed batch）；活动阶段存在后，动态暴露里的 `load_skill_context` / `load_tool_context` 提示才进入可执行顺序。排查“先调 `load_skill_context` 却撞上 no active stage”时，先检查稳定协议与 `stage_messages.py` 状态 overlay 是否一致，再检查 `prompt_builder.py` / `message_builder.py` 的动态提示是否与主协议竞争。`candidate_tools` / `candidate_skills` 两类候选的不对称语义详见 `tool-and-skill-system.md`「四个概念必须分清」。

heartbeat / cron 的维护语义分两条通道：UI 展示通道上前端继续通过 inflight / session snapshot 渲染 heartbeat / cron 的原始处理流程（开阶段、工具调用、执行轨迹、压缩状态）；普通历史注入通道上，下一次真实用户 turn 的近场 prompt 历史过滤 internal-only user 消息与 `history_visible=false` 的 assistant 消息。内部轮次的请求组装、隐藏提示消息与工具合同继承详见 `heartbeat-system.md`「Continuation Contract」。

相关文件：

- `g3ku/runtime/frontdoor/ceo_runner.py`
- `g3ku/runtime/frontdoor/prompt_builder.py`
- `g3ku/runtime/frontdoor/message_builder.py`
- `g3ku/runtime/stage_prompt_compaction.py`

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

### 5.1 Chat provider 超时与重试边界（维护者必须掌握）

chat 调用的时间边界只有一种：**单次（单轮）provider 请求的响应时间上限，默认 10 分钟**（`DEFAULT_PROVIDER_ATTEMPT_TIMEOUT_SECONDS`）。不存在跨 attempt、跨链轮的总时长预算——可重试的整链重试是无限次的，节奏由退避控制而不是总预算控制，因此外层不能给整个 chat 调用套 `wait_for` 总预算。

两类 provider 对单次上限的执行方式不同：

- 自己管理流式超时的（`manages_request_timeout_internally=True`，含 `ResponsesProvider`、`OpenAIChatProvider`）：外层不加硬截断（避免“流式持续出 chunk 却被总时长误杀”），provider 内部采用“streaming-first”语义——首个 chunk（任意 chunk，不要求文本 delta）与后续 idle chunk 的阈值都取单次请求上限；上游不支持流式时同一次 attempt 内自动回退非流式，非流式首响应阈值同样取该上限。
- 其余 provider：由外层 `wait_for_model_attempt` 按同一上限截断。

重试边界（`main/runtime/chat_backend.py` 与 `g3ku/providers/fallback.py` 共用同一套语义）：

- `retry_on` 关键词命中的错误（默认含网络类与 429/限流类）触发整链重试，**不限轮数**；轮与轮之间走封顶指数退避并带抖动（起点约 1s、封顶 60s），抖动用于打散并发节点的重试节奏，避免同步撞同一个限流窗口。
- 重试循环在每个链轮边界对比 runtime config revision：revision 变化时抛出带 `config_revision_changed` 标记的可重试耗尽错误（`ModelProviderExhaustedError`），由上层重启回合/重建链，而不是继续用旧链空转。任务运行时侧见 `config-and-models.md`「模型链变更何时作用于在途回合」。
- 关键词未命中的错误仍走有限路径：同轮内换 key、回退链上下一个模型；都不可用时抛出耗尽错误。
- 已经出现可见流式文本的发送不做透明重试/回退（一个可见气泡不得由多个 provider/model attempt 拼接）。

维护判断上要记住：

- “首 token”在本项目语义里是“首个 chunk 到达”，不是“首个文本 token”
- `request_timeout_seconds` 对上述 provider 表示“首 chunk / idle chunk 超时阈值”，不是“整次请求必须在 N 秒内完成”
- 节点因限流长时间停在模型等待（`await_marker=model.chat.await_response`）可能是退避重试在正常推进，先看日志里的 `Retryable model-chain failure (round …)` 再判断是否卡死

## 6. 运行时里的状态与持久化

同步会话和任务运行时有两套不同的持久化关注点：

### 会话侧

- transcript / session messages
- paused execution context
- inflight turn snapshot
- frontdoor completed continuity sidecar（`frontdoor_request_body_messages` 基线、actual-request trace、阶段/规范化/压缩状态）
- latest message / pending interrupts

主要由 `RuntimeAgentSession` 和 `g3ku/session/manager.py` 协调。frontdoor continuity 的写盘与恢复覆盖所有 frontdoor 会话命名空间（`web:`、`china:`、`cron:`），渠道会话的基线同样跨进程重启存活；恢复顺序详见 `context-and-cache-troubleshooting.md`「Baseline 合同与恢复顺序」。

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

关于节点运行帧还要额外记住一个维护语义：

- `task_runtime_messages` / `runtime-frame-messages:{node_id}` artifact 除当前 messages 列表外，还在同一个 artifact 里累计 `callable_tool_snapshots`、节点输入层的 `contract_visible_skill_ids`（`runtime_service._node_context_selection_inputs()` 当轮快照）与 `skill_visibility_diagnostics`（解释这些可见 skill 如何从 live registry / role / policy 收敛出来）。每条快照代表一次 `before_model` 轮次下本地运行时真正记录的 callable/candidate 截面：`callable_tool_names`、`candidate_tool_names`、`candidate_tool_items`、`candidate_skill_ids`、`candidate_skill_items`、`model_visible_tool_names`、`hydrated_executor_names` 和选择 trace。排查“这一轮模型 load 过工具却没法调用”时，先看最近一条快照再看 transcript / stage trace。
- 对 execution / acceptance 节点，无有效阶段的快照里 `callable_tool_names` 与 `model_visible_tool_names` 只剩 `submit_next_stage`；候选集合留在 `candidate_tool_names`，完整 callable pool 留在选择 trace（`model_visible_tool_selection_trace.full_callable_tool_names`）。canonical name/id 列表为空时，同快照的 `candidate_tool_items` / `candidate_skill_items` 也必须同步清空。
- 执行节点与检验节点是“两层消息结构”：稳定 bootstrap user JSON 只负责任务定义与稳定节点上下文（不含 `execution_stage`）；单独的动态 `node_runtime_tool_contract` 摘要消息负责当前轮的 callable/candidate tool/skill 合同，追加在当前 request 尾部、当轮 turn-only 阶段提示之前——请求末位保持 user 回合提示，契约块不占据末位（末位的 assistant 契约会诱导模型把契约抬头回显进下一条回复）。节点的 turn / repair overlay 同样遵守 append-only 边界：只能作为新的 request-tail 消息追加，不得回写已有 bootstrap 或持久化历史消息。
- 对节点运行时，`before_model` 当轮真正下发给模型的 schema 选择结果是权威工具来源；runtime frame、restore/recovery 和 runtime messages artifact 都从这份结果派生。`node_runtime_tool_contract` 是模型可见合同，但 runtime frame 才是 `candidate_skill_ids` / `candidate_skill_items` 的 canonical 恢复来源。
- 排查“节点为什么说没有 candidate skills”时，同时看 `contract_visible_skill_ids`（输入层可见性）与 `candidate_skill_ids`（selector 最终候选）；输入层为空时继续看 `skill_visibility_diagnostics`（registry 存在性 / role / policy effect）。首轮 `candidate_skill_ids=[]` 而 fresh contract 本应非空时，优先判断是否仍停留在 `initialize_task()` 的 bootstrap 空 frame。
- CEO/frontdoor 采用同样的分层思想：稳定会话前缀不承担当前轮 callable/candidate tool 状态，当前轮工具合同放在 dynamic appendix 并随 turn state 刷新，overlay 保持 append-only。prompt cache key 未变但命中下跌时，先检查是否有 overlay 被拼回已有 user 消息。

## Prompt Cache Family And Actual Request

基线合同摘要（完整取证与排查归缓存排查文档）：

- caller-side prompt cache family 只由稳定前缀加显式 cache-family revision 输入决定；普通 callable/candidate/hydrated 漂移、阶段门控收紧与 hydration promotion 可以改变 actual request，但不得自行轮转 family key。
- 每次 `call_model` 必须把该轮重建的 request 与匹配重建的 `prompt_cache_key` 一起发送；CEO/frontdoor 与节点侧都保持静态前缀前置 append-only、请求尾部区域恰好一份当前 runtime 契约，且契约排在该轮 turn-only note / 当前 user 回合之前（末位保持 user 消息），携带历史中的旧契约与 turn-only note 一律剥掉。
- 两侧都为每轮 `call_model` 持久化专用 actual-request artifact：CEO/frontdoor 写 `.g3ku/web-ceo-requests/<session>/...json`（`visible_frontdoor` / `token_compression` / `inline_tool_reminder` 共用同一条时间线），节点写专用 JSON 且 `actual_request_ref` / `latest-context` 指向它；这些 artifact 是 provider-facing 顺序的取证权威，durable baseline 与快照则在持久化前剥掉契约消息。
- artifact 写入带内存压力守卫：`MemoryError` 时降级为 forensics-first payload（`artifact_persistence_mode=memory_guard_degraded` / `memory_guard_minimal`），不得因 artifact 过大而失败整个 turn/节点；读取降级 artifact 前先检查该标记。
- `RuntimeAgentSession._frontdoor_request_body_messages` 是 session-owned request-body baseline：只在当轮已有真实 actual-request 证据后才允许跨回合替换，且只有 `token_compression` / `stage_compaction` 可以让下一轮基线变短；恢复顺序、scaffold 规则与收缩守卫细节归缓存排查文档。

完整的 cache family / actual-request 取证、baseline 恢复顺序与诊断字段详见 `context-and-cache-troubleshooting.md`「Prompt Cache Family 与 Actual Request」。

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

长期记忆运行时是队列化的 Markdown memory 子系统：

- `memory/memory_state.sqlite3` 是长期记忆权威状态：每行存完整记忆正文、最小摘要、`refresh_count`、`passed_count`、`is_compressed`、来源与 `from_user` 保护元数据。
- `memory/MEMORY.md` 是从 SQLite 状态再生的提示词快照，保留受管 Markdown 块形状供工具与内部 memory agent 检查，但不是权威元数据存储。
- `memory/notes/` 存 `ref:note_xxxx` 引用的可选详细 note 正文，保持小而人类可读。
- `memory/queue.jsonl` 是唯一持久队列，带每请求处理状态（`pending` / `processing`、重试计时、最新错误文本）。队列条目只有两种类型：`write`（显式或已提炼的记忆文本，等待真正的记忆处理）与 `delete`（自然语言记忆删除请求，等待内部 memory agent 解析成具体 id）。
- `memory/ops.jsonl` 是滚动终态历史，不是进行中重试日志，也不是 append-forever 归档：applied 批次与 `rejected` / `precheck_failed` 等 durable discarded 结果连同最终 snapshot / compression 元数据一起落在这里；暂时性 provider/配置失败仍留在队列头错误字段；超过 7 天的行在正常运行时读写中自动清理。终态行不记录入队侧 `trigger_source`；区分普通窗口批次与压缩冲刷批次要对照会话转录时间线。
- `memory/review_state.json` 是普通复核窗口的按会话缓冲元数据：缓冲轮次载荷、阶段 delta cursor、已上报可见工具记录 cursor；不是已提交的用户记忆。
- `.g3ku/memory-requests/` 存暴露请求元数据的 memory 请求 artifact；processed 行可以指向这些路径供后续取证。

维护边界：

- queue 文件是运行时元数据，不是用户记忆内容。
- 权威/快照/笔记分工见上面的子系统列表；memory-worker lease 单活规则见下面的运行时合同。

运行时合同：

- CEO/frontdoor 轮次在 prompt 组装时读取一份冻结的 `MEMORY.md` 快照，并把只含 `---` 分隔记忆文本的展示渲染注入稳定前缀（剥掉记忆 id 与日期/来源头；内部 memory agent 仍看到完整受管快照）。同轮热更新不影响当前提示词，新提交记忆只影响后续轮次。
- `memory_write` 与 `memory_delete(content=...)` 只向单一记忆队列入队，不内联修改已提交记忆；surfaced agent 请求删除时不传记忆 id。
- `RuntimeAgentSession` 把自主复核窗口缓冲在 `memory/review_state.json`，并在三个时点自动入队直接 `write` 批次：配置的普通轮窗口阈值（默认 5 轮）、token 压缩冲刷（`token_compression`）、会话结束/删除冲刷（`session_boundary`）。token 压缩冲刷只看“当轮内联压缩真实发生”这一事件信号：会话级标志在回合内任一请求压缩 applied 时置位、轮首清零，不读跨轮残留的 `frontdoor_history_shrink_reason`——后者是“baseline 为何比上一轮短”的解释（合同见 `context-and-cache-troubleshooting.md`「frontdoor_history_shrink_reason」），不是当轮压缩事件，用它判冲刷会把上一轮的压缩算到本轮头上。阶段压缩（`stage_compaction`）只裁会话侧提示词历史、不冲刷复核窗口。不支持的冲刷来源必须保持缓冲窗口原样，不得制造队列行。复核载荷与用户在界面看到的可见面一致：用户轮记录用户消息与助手回复；心跳/cron 内部轮只记录可见的助手回复与阶段可见面，隐藏事件束提示词不进载荷；静默 `HEARTBEAT_OK` 内部轮不入队。每轮载荷含 `stages:` 段（JSON，顺序与界面阶段轨一致）：回合记录时点从阶段可见面中仍保留 `rounds` 的阶段**全量捕获**所有新出现的工具调用，不设每轮条数上限——复核窗口内的阶段因此始终以完整未压缩形态送达内部 memory agent，不受会话侧提示词阶段压缩的影响；新记录按所属阶段嵌套进 `tool_records`，阶段内按目标 → `tool_records` → 阶段总结排列，携带新记录的阶段附带可见面当前完整目标与阶段总结。每条工具记录含工具名、状态、入参提示（写入侧已限长）、输出预览（截 240 字）、输出全文（截 400 字）与不截断的伴随中途输出（round.text，同一 round 只附一次）。工具记录按会话已上报 cursor（上限 5000；空 `tool_call_id` 用内容指纹）跨轮去重。`review_state.json` 除 `pending_turns` 外维护阶段 delta cursor 与工具记录 cursor：冲刷只清缓冲轮次、保留两个 cursor（普通窗口与 token 压缩冲刷）；会话结束/删除冲刷在入队后连同 cursor 一并清除该会话复核状态。`stages:` 段出现的阶段是：相对上一次复核新出现或实质变化的阶段（阶段 delta），以及本轮携带新捕获工具记录的阶段。
- 专用内部 memory agent 以 FIFO 同-op 批次（`write` 与 `delete` 不混批）消费队列，走 `memory` 模型路由，只有读写 `MEMORY.md` 与 note 文件的受限工具面；它把自然语言删除请求解析成具体 SQLite id，并可报告实质影响该批次的 `inspired_memory_ids`。
- 每次非读变更后，运行时先改 SQLite、再重建 `MEMORY.md`、最后检查快照大小：超过 `document.compress_trigger_chars`（默认 `16000`）时按 `passed_count DESC`、`refresh_count ASC` 顺序压缩，先把整行替换为 `minimal_memory`，再只删除已压缩的非 `from_user` 行，直到回到 `document.compress_target_chars`（默认 `13000`）或没有安全压缩工作。
- 队列消费跨进程单活：每次 `run_due_batch_once()` 必须先拿 workspace 级 memory-worker 文件锁；拿不到锁的进程保持队列不动并报告 `worker_lease_unavailable`。`request_id` 是持久幂等键：处理批次前会丢弃 `memory/ops.jsonl` 中已出现过 `request_id` 的队列行。

队列状态机语义：

- `pending` 表示请求尚未被认领进批次；`processing` 表示队列头批次当前由唯一 memory worker 持有。
- `memory` 角色未配置或 provider 调用失败时，队列头保持 `processing` 并携带错误，阻塞后续请求。
- 持久化的 `processing` 队列头是重启恢复状态，不证明仍有活跃 worker；重启后等待存储的 `retry_after` 再重试同一头批次。`processing_started_at` 记录队列头批次首次成功认领，跨重试保持稳定，不是“最后重试时间”。
- 语义非法的模型输出应在同一处理批次提交前完成自修复；若批次仍无法通过运行时 precheck 或 memory agent 始终给不出有效终态工具结果，运行时把该批次落入 durable discarded 历史，而不是无限重试。
- `ops.jsonl` 出现两条相同 `request_id` 的终态行是 bug 信号（历史多 worker 竞争或旧版本运行），不是正常重复写入。

瞬时执行状态明确在长期记忆边界之外：pause/resume 控制数据、进行中任务状态、临时修复标记与 runtime-only 协调笔记属于 transcript、session、task 或 stage 运行时状态，不进入 `MEMORY.md`。

队列卡住、重复写入、调试顺序、CLI 与 reset 等 operator 工作流详见 `operations-and-maintenance.md`「Memory Queue Workflow」。

## Memory Runtime Reset Guard

bootstrap bridge 的 `_reset_memory_runtime(...)` 会重置 commit service、memory manager 与 SQLite checkpointer。其中 checkpointer 是进程共享句柄：在途回合编译出的图仍持有旧实例引用，若此时被关闭，该回合最终 `aput_writes` 会报 `Cannot operate on a closed database`。因此：

- 存在活跃任务会话（`loop._active_tasks` 非空）且 checkpointer 存活时，重置**保留** checkpointer 及其 context manager 不关闭（`_checkpointer/_checkpointer_cm/_checkpointer_enabled/_checkpointer_backend/_checkpointer_path` 五字段作为整体保留，不允许中间态），只清理其余记忆资源，并置 `_checkpointer_reset_deferred = True`。
- `init_memory_runtime(...)` 检测到存活 checkpointer 时跳过 checkpointer 重建，避免 orphan 在途引用。
- `_sync_memory_runtime(...)` 入口处先做延迟补做：延迟标志置位且活跃会话已清空时，调 `_complete_deferred_checkpointer_reset(...)` 关闭保留句柄并强制走一次完整重置。这一步必须在指纹门控之前——延迟重置已经前移了存储指纹，若只靠门控，保留的旧 checkpointer 永远不会按新设置重建。
- 失活连接的自愈仍由 engine 的 `_ensure_checkpointer_ready(...)` 懒重建兜底。

排障「回合落库报 closed database」：先确认触发重置的来源（`reason=...`）发生在回合进行中，再看是否走了保留/延迟路径（日志 `Deferring checkpointer reset while active sessions exist` / `Completing deferred checkpointer reset`）。`force_memory_sync` 语义见 `config-and-models.md`「配置热刷新」。

## Internal Turn Contract Notes

Heartbeat 与 cron 内部轮次共享同一内部轮次合同，完整契约详见 `heartbeat-system.md`「Continuation Contract」与「Cron Reminder Contract」。本文只记运行时层不变量：

- 内部轮次与普通可见轮次一样通过 `RuntimeAgentSession.prompt(...)` 执行，携带各自的内部来源元数据；它们会清掉 live-only 调试面（`frontdoor_selection_debug`、每轮 actual-request 指针），但不在 prompt 组装前清零 session-owned 请求体 / 阶段 / 压缩连续性状态。
- 规则文本与事件载荷以隐藏内部提示消息追加：`prompt_visible=true`、`ui_visible=false`，带 `internal_prompt_kind`（`heartbeat_rule` / `heartbeat_event_bundle` / `cron_rule` / `cron_event_bundle`）；heartbeat 追加 `system` 规则 + `user` event-bundle，cron 追加两个隐藏 `system` 块。存在权威 frontdoor 基线时，内部轮次直接继承普通 CEO tool/skill 暴露合同（含绕过“无有效阶段只剩 `submit_next_stage`”的收紧）。
- 无基线的内部轮（重启后首轮、全新会话首轮）不进入续跑分支：内部事件消息单独交给 prompt 组装，由新建路径注入，基础系统提示保持首位。续跑分支只在存在真实请求体基线时使用——否则仅有的内部事件消息会冒充完整旧请求体、让基础提示被静默丢掉。内部轮基线/恢复细节见 `context-and-cache-troubleshooting.md`「heartbeat / cron 按普通 continuation shrink 规则排查」与「Baseline 合同与恢复顺序」。
- 服务层不得替模型自动重试任务，也不得合成回退 assistant 回复。
- `HEARTBEAT_OK` 是唯一的 live-only ACK 例外：ACK 事件可以在 UI 展示，但不得新建可见 assistant 转录条目；隐藏内部提示消息（`ui_visible=false`）是 durable 且 prompt-visible 的，与 live-only ACK 是两回事。

## Repeated Tool Call Guard Notes

执行阶段重复工具调用的软拒绝、修复消息、升级语义与只读检索分支契约详见 `tool-and-skill-system.md`「Duplicate Tool Call Guard」。

## Resource Generation Checks For Semantic Catalog Freshness

语义目录新鲜度、节流资源代检查（`resources.reload.poll_interval_ms`）、指纹刷新与元数据编辑失效规则详见 `tool-and-skill-system.md`「Catalog Freshness」。

## CEO Frontdoor Canonical Context Contract

`frontdoor_canonical_context` 是 CEO/frontdoor 唯一的跨回合阶段真相源：

- 它是 durable 的跨回合阶段/历史视图；turn finalization 把当前轮阶段账本并入该结构。`frontdoor_stage_state` 与 `compression_state` 是运行时工作状态，不需要在每个新用户 / heartbeat / cron 轮次的 prompt 组装前清空；graph-local 状态为空时，`prepare_turn` 可以复用 session-owned 请求体与这些快照重建下一个 provider 请求窗口。
- session/runtime 同步不得把 request-local 投影写回 `frontdoor_canonical_context`：只有 turn finalization 允许向 durable canonical 链追加 completed-stage 数据；`frontdoor_canonical_context + 当前 frontdoor_stage_state` 派生出的一切只是当前请求的可见 workset 数据。
- 近场 stage workset 从 `frontdoor_canonical_context + 当前 frontdoor_stage_state` 派生，不从 transcript `execution_trace_summary` 或平铺 `tool_events` 重建。round-level 工具记录同时保存归一化原始 `arguments`；小输出内联在 `output_text`，大输出外置为 `output_ref` + `output_preview_text`，prompt 渲染器不把 artifact 正文读回内联。
- 若当前轮阶段状态里已包含与 `frontdoor_canonical_context` 中实质相同的 completed stage，prompt 组装必须按重叠处理、跳过把它 rebase 成新的合成 stage id——否则一个 completed stage 会在 fresh-turn 重建中膨胀成重复的原始阶段块。
- UI 面向的 turn payload 暴露当前轮的 `canonical_context` 切片；prompt 组装读 durable 跨回合 canonical context，inflight / paused / final-reply payload 只描述可见轮自己的阶段轨迹。

第二条连续性合同：`frontdoor_request_body_messages` 是下一轮 CEO/frontdoor 的 session-owned provider 请求体基线，刻意不含 `frontdoor_runtime_tool_contract` 消息（动态工具暴露每轮作为新的尾部合同重建），也不含 `## 长期记忆` 快照（当轮 overlay，只在当轮请求可见，落史会逐轮累积污染上下文），且只允许在 `token_compression` 与同轮 `stage_compaction` 两个信息损失边界收缩（见本文「Frontdoor Context Compression (Current Contract)」）。fresh 可见轮次中该基线优先于任何 graph-local checkpoint 式阶段重放投影：两者同时存在时必须从请求体基线继续，而不是从阶段重放重建新的主前缀。

## Runtime Contract Lane

模型面向的运行时契约是以 `## Runtime Tool Contract` 开头的 assistant summary 块（summary 形式、修复车道、“尾部只带最新快照”与基线剥离规则详见 `tool-and-skill-system.md`「四个概念必须分清」与 `context-and-cache-troubleshooting.md`「append-only 规则」）。本节记录本文拥有的 canonical 表示规则与运行时边界。

Canonical 阶段状态按以下表示规则收敛（这是 canonical 链唯一允许的信息损失边界）：

- 最近 3 个完成的普通阶段保持 `raw`；该规则与当时是否存在活动阶段无关，纯对话回合（无活动阶段）同样适用。
- 更早的完成普通阶段变为 `compact`。
- canonical 链只有 `raw` 与 `compact` 两级表示：历史数据中已存在的归档压缩阶段（`stage_kind="compression"`，外置表示）继续规范化与渲染，运行时不把完成阶段合并成新的归档阶段；长会话的阶段体积由 `compact` 块承载，总体积兜底归 `token_compression`。

另有两条运行时边界：

- prompt token trace 只有两个字段：`pre_request_prompt_tokens` 是内联 `token_compression` 之前的发送前估算（必须包含 stage workset）；`effective_prompt_tokens` 是 prompt 组装完成后最终真实发送请求的估算。
- 节点侧 token 压缩只是针对当次请求的 live 重写：可以缩短 provider-bound `request_messages`，但不得改写持久阶段历史、frame `messages`，或从 `model_messages` 派生的稳定 prompt-cache family 输入。

若下一轮基线以两个收缩边界之外的任何理由变短，按意外上下文损失排查；守卫自愈行为见本文「Frontdoor Context Compression (Current Contract)」。

token preflight 估算、触发阈值、`effective_input_tokens` 真相车道、压缩优先顺序与诊断字段详见 `context-and-cache-troubleshooting.md`「Prompt Cache Family 与 Actual Request」；图片上传与 `content_open` 的多模态展开、`5 MiB` 守卫与单次发送 overlay 规则详见 `web-and-admin.md`「Image Upload Gating」。

## CEO Inline Tool Reminder Sidecar

CEO/frontdoor 直连长时工具有一条独立的 live-only 内联提醒侧车道，运行时边界如下：

- 内联执行注册在 `InlineToolExecutionRegistry`，与 detached `ToolExecutionManager` 后台执行语义相互独立；内联执行 id 不是 detached watchdog 执行 id。
- `CeoToolReminderService` 对会话持久化是只读车道：不调 `session.prompt(...)`、不取普通 turn 锁、不创建 heartbeat / internal turn；提醒文本与决策不写入 transcript、canonical context 或后续 prompt 历史注入。
- 侧车道优先复用最近一份已持久化的 CEO actual-request artifact 作为 provider-facing scaffold，与主轮共享同一段可缓存前缀；artifact 因内存守卫降级（`artifact_persistence_mode=memory_guard_degraded` / `memory_guard_minimal`）时，退回 `CeoMessageBuilder.build_for_ceo(..., ephemeral_tail_messages=...)` 重建。
- 侧车道停止决策以普通工具失败形式回到主轮（`reason_code=sidecar_timeout_stop`，per-tool 子取消令牌），与用户主动暂停/取消是两条不同路径。

提醒窗口、观测感知决策、`STOP` / `CONTINUE` 语义、失败兜底与 timeout-stop 错误合同详见 `heartbeat-system.md`「CEO Inline Tool Reminder Sidecar」。

## Node Provider Request Scaffold

执行与检验节点保持两套工具可见性视图：

- `tool_names`：运行时契约强制使用的每轮权威可调用工具集。
- `provider_tool_names`：构造真实模型请求时使用的 provider-facing schema bundle；新写入代表该 turn 家族已持久化的 RBAC-visible concrete-tool bundle。`pending_provider_tool_names` 仅是兼容字段，新写入保持 `[]`。
- 节点发送 artifact 还记录 `provider_tool_exposure_revision`（真正到达 provider 的已持久化 provider bundle 的短哈希）；`provider_tool_exposure_commit_reason` 仅是兼容字段，新写入保持 `""`。

这套分离只为在不削弱阶段门控与工具 hydration 规则的前提下提高 prompt cache 稳定性。同轮内节点多轮循环的 provider 请求构造走 append-only scaffold：上一份真实请求体 + 上一轮新增的 assistant / 工具结果消息 + 最新 `node_runtime_tool_contract` / turn-only note 尾部（契约在前、当轮 note 在后，末位是 user 回合提示）。它只是请求构造脚手架——不替代节点持久/压缩后的 `message_history`，也不重新定义哪些工具可调用；它存在的唯一目的是：当阶段压缩在回合边界修剪历史时，provider 看到的是 append-only 增长而不是早期前缀重写。`provider_tool_bundle_seeded` 只是兼容/诊断提示，真实行为由活跃/待定曝光状态加 token 压缩提交门控驱动。完整规则详见 `context-and-cache-troubleshooting.md`「append-only 规则」。

## Frontdoor Context Compression (Current Contract)

当前 CEO/frontdoor 请求收缩模型只有两个信息损失边界：

- `stage_compaction`
- `token_compression`

任何其他理由让下一轮请求基线变短，都应按回归排查。长上下文只由两项机制约束：近场 stage workset compaction（按阶段归属原位压缩过期完成阶段的工具调用，与执行阶段提示词逻辑共享），以及在最终请求接近所选模型窗口时改写旧 body history 的内联 `token_compression`。归档压缩阶段（`stage_kind="compression"` + `archive_ref`）是历史遗留表示数据：持久化状态中已存在的归档阶段继续规范化与渲染，运行时不产生新的归档阶段。

### `token_compression`

- 内联同轮 LLM 重写，在 provider 发送前立即执行：保留稳定 system 前缀、最新运行时工具契约尾与最近的 body-history 尾部，只把更早的 body-history 区间重写为一个 `G3KU_TOKEN_COMPACT_V2` 标记块。
- 触发阈值绑定运行时所选模型的 `context_window_tokens`：估算请求 `<= 80%` 模型窗口时直接发送；介于 `80%` 与 `100%` 之间时尝试一次内联压缩；已超过 `100%` 时先失败，因为连压缩尝试本身都无法安全装进当前模型窗口。
- 对 CEO/frontdoor 与节点运行时，`token_compression` 都不是 provider bundle 提升边界：压缩发送沿用已持久化的 `provider_tool_names`，任何 provider-bundle 刷新落在压缩后的第一个普通 turn。

### `stage_compaction`

- 修剪的唯一真相源是 `stage_prompt_compaction.compact_stage_prompt_messages_in_place()`：最近 3 个完成普通阶段与活动阶段保留完整窗口；过期完成阶段的工具调用消息（`assistant+tool_calls` 与其配对 `tool` 响应）成对移除，对应 compact 块回插在该阶段首条被移除消息的位置（既有块按记忆位置回插）；阶段之外的用户可见对话原位保留；内部事件束（heartbeat / cron 载荷）随过期内容移除。
- 原位放置是缓存硬约束：压缩块不整体收拢到上下文头部；除治愈遗留布局的一次性收敛外，每次压缩的前缀失效面从最早被压缩阶段的位置开始。
- 该规则与是否存在活动阶段无关；无活动阶段的纯对话回合同样压缩过期阶段并保留最近 3 个。
- 幂等：同一输入重写两次收敛为同一输出；归属保留阶段的残留旧块去重丢弃。
- 可以缩短活动历史窗口或 stage workset，仍是下一轮基线的合法收缩理由。
- 不是 provider schema 刷新边界：若某次发送的收缩原因是 `stage_compaction` 而 `provider_tool_names` 变了，按 provider-bundle 刷新路径 bug 排查。

### Removed Semantic Summary Path

- 旧的语义/全局摘要 lane 不再参与 prompt assembly；`compression_state` 只表示内联 `token_compression` 的实时进度，不再是“语义摘要就绪”的 durable 信号。
- 续跑恢复依赖权威 frontdoor 基线、阶段状态、请求痕迹与收缩原因，不依赖单独的 `semantic_context_state` 交接块。
- 不存在中间的“按消息条数压缩”阶段：一次性结构式 preflight compaction（`_run_frontdoor_token_preflight_compaction` / `compact_frontdoor_history_zone`）与 `_summarize_messages()` 兼容钩子都不在执行路径上。不要重新引入平行的结构式压缩——共享的 `stage_prompt_compaction` helper 是 stage-window 修剪的唯一真相源。
- 续跑 seed / 全量转录原始历史在拼接前先经过归属原位压缩（`_trim_frontdoor_seed_to_stage_window`，经 `compact_stage_prompt_messages_in_place`）：过期阶段的工具调用成对移除、块回插原位、对话保留，使存量大基线真正收缩，而不是携带未裁剪旧体重新膨胀。修剪需要阶段列表：优先取 `frontdoor_stage_state`，为空时退回 `frontdoor_canonical_context.stages`；两者都没有阶段时修剪是安全 no-op（不做有损删除），收缩交给 `token_compression` 兜底。排查“seed 从不收缩”时，先确认会话是否把阶段持久化进了这两个来源之一，再确认是否有阶段真正老化出最近 3 窗口（阶段数不足 4 个时本就没有可压缩对象）。

### Shrink-Guard Self-Heal（`context_shrink_quarantine`）

- finalize 前，运行时以同形归一方式比较下一轮请求体基线与会话基线：两侧都先剥掉工具契约消息、turn-only note 与多模态块，再估算 token。
- 若下一轮基线变短且收缩原因不是上面两个允许理由，守卫不裸抛异常冻结会话：它把拒绝后的新种子以受控原因 `context_shrink_quarantine` 写回会话基线，累计连续隔离计数并打警告日志，使后续回合的对比保持一致、会话可自愈。
- 连续隔离计数持续上升时，按 prompt 组装回归排查，不要解释成“正常上下文整理”。

### Pause During Compression

- 内联压缩进行中手动 pause 对该可见轮次是终态：运行时取消活跃的压缩生成，丢弃迟到的压缩结果，不让它更新基线或继续进入主 provider 发送。
- 下一次激活（新用户输入、heartbeat 唤醒等）必须以当时的模型链与上下文窗口重新走 prepare → estimate → 可选压缩 → send。

排查 prompt 连续性问题时的前两个问题：相关上下文是否仍在保留的 stage workset 内？若不在，内联 `token_compression` 或 `stage_compaction` 是否合法缩短了下一轮基线？基线与 artifact 的完整取证详见 `context-and-cache-troubleshooting.md`「Prompt Cache Family 与 Actual Request」。
