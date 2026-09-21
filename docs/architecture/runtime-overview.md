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
  运行时工厂。在这里把 provider、middleware、`AgentLoop` 装起来。

- `g3ku/agent/loop.py`
  是 `AgentRuntimeEngine` 的兼容包装层，本身逻辑不多，但定义了真正运行时类型 `AgentLoop`。

- `g3ku/runtime/engine.py`
  运行时核心容器。负责：
  - `ToolRegistry`
  - `ToolExecutionManager`
  - session 取消令牌
  - memory/commit service
  - bootstrap bridge 初始化默认工具和多 agent 运行时

- `g3ku/runtime/manager.py`
  `SessionRuntimeManager`。按 `session_key` 复用 `RuntimeAgentSession`，是所有入口共享的 session 路由器。

- `g3ku/runtime/bridge.py`
  `SessionRuntimeBridge`。给 Web、CLI、cron、External Agent API（`/api/v1`，外部桥接应用）提供统一的 prompt / prompt_batch / continue / cancel / pause API。pause 带运行状态前置检查（空闲会话返回 0，避免空闲暂停产生多余转录归档）；外部桥接用它实现控制命令（暂停/取消）与运行中消息注入，回合契约详见 `external-agent-api.md`「回合契约」。每个 prompt 系列调用带慢回合看门狗：超过 `G3KU_SLOW_PROMPT_WATCHDOG_SECONDS`（默认 900s，`<=0` 关闭）仍未返回就打 WARNING 并 dump 会话任务的活体 await 链（挂起回合的取证入口，合同详见 `heartbeat-system.md`「Cron Reminder Contract」的 hang forensics 条目）。

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

### 3.3 静默回复（`[G3KU_SILENT]`）

任意一轮（用户轮、heartbeat、cron）的最终回复若为单独的 `[G3KU_SILENT]`（整串精确匹配，去除首尾空白），runtime 把它识别为静默并以 `_prompt_locked` 的 finalize 为归一化点：识别分两层——frontdoor `_graph_finalize_turn` 依据 token 判定静默、用于把该回复排除出请求体基线回填，但**必须**把 `final_output` 原文（连同 `silent_reply` 标记）经 `run_turn` 原样回传，由 `_prompt_locked` 再做一次精确匹配归一化；若 finalize 层吞掉 token，`_prompt_locked` 只会看到空输出，`task_terminal` 心跳修复循环会把合法静默误判为"无效空回复"并一路撞到"连续失败"兜底文案。

归一化后的边界是"吞掉回复文本，保留回合活动"：可见回复不投递到任何渠道（`RunResult.output=""`，channel transport 与 cron dispatch 的空输出守卫自然不投；`message_end` 文本置空并带 `silent_reply` 标记，外部 relay 据此跳过）、不回填请求体基线、不进 memory 复核；回合生命周期照常走完（`turn_end` / `agent_end` / `state_snapshot`、状态 `completed`），本回合已发布的阶段与工具调用保持完整可见。transcript 落一条**空文本** assistant 行，带 `silent_reply=true`、`prompt_visible=false`、`ui_visible=true`：这一行存在的唯一理由是承载本回合的 `canonical_context`，让会话框刷新后仍能重建阶段轨道；`prompt_visible=false` 与基线规则对齐——被吞掉的回复既不进请求体基线，也不经转录重放回到模型上下文。内部轮（heartbeat/cron）的静默同样落这一行，只有 `HEARTBEAT_OK` 与空输出仍完全不落 assistant 行。

Web 侧没有"静默占位文案"这个概念：静默回合走与普通回合**同一条** `ceo.reply.final` 通道，`text=""` + `silent_reply=true`，并照常带上 `source` / `turn_id` / `user_messages` / `usage` / canonical context 合并结果。前端 `finalizeCeoTurn` 与 `renderPersistedCeoAssistantTurn` 见到 `silent_reply` 只隐藏回复气泡本身（`hideCeoAssistantText`），阶段轨道与工具步骤按普通回合收尾。新维护者最容易误读的一点：静默 final 一旦缺少 canonical context（早退或自行裁剪字段），`finalizeCeoTurn` 会退到"无回合元素"兜底分支并 `discardPendingCeoTurns`，整条阶段轨道连同工具步骤一起被删掉——表现为"静默回合什么都没显示"，根因在 final 载荷字段不全，不在渲染层。会话列表 preview 在没有可见文本时保持原值（`update_ceo_session_after_turn` 对空 `preview_source` 不写回）。存量转录里可能仍写着历史占位文案，`_build_ceo_snapshot` 把这类行与空文本静默行一律归一化为 `silent_reply` + 空 content；没有 `canonical_context` 的静默行整行不进快照，所以既不会显示文案也不会出空气泡。

`HEARTBEAT_OK` 仍是内部轮（heartbeat/cron）专属的 live-only ACK；`[G3KU_SILENT]` 是更宽的静默信号——任何来源都可用，包括 `task_terminal` / `shutdown_resume` 这类本不允许静默的事件（见 `heartbeat-system.md`「Task Terminal Repair Contract」）。

## 4. frontdoor 与任务运行时的关系

G3KU 并不是所有问题都在 CEO 单次对话内完成。frontdoor 的职责更像是：

- 识别当前用户请求
- 组织提示词与上下文
- 判断当前这轮能直接回答，还是需要走任务运行时
- 在必要时触发任务工具，如 `create_async_task`

`create_async_task` / `task_append_notice` 的完整工具契约与守卫（重复预检与重验、`file_targets` reopen 车道、拒绝语义）详见 `tool-and-skill-system.md`「fixed builtin tools」。运行时记账与任务级控制合同要点如下：

- frontdoor dispatch 记账必须区分“工具调用发生了”与“新任务真的创建了”：只有显式成功形式才算已核实派发；拒绝消息里提到的旧 `task:...` id 不算新建任务。同一可见轮可以多次调用 `create_async_task`，是否真正创建由 `MainRuntimeService` 的唯一创建路径决定。
- 运行时把追加的任务消息视为**子树级控制事务**：`MainRuntimeService.append_notice_to_targets(...)`（工具入口 `task_append_notice(...)` 与网页端 `POST /api/tasks/{task_id}/nodes/{node_id}/notice` 共用）解析出目标节点集合——`node_ids` 是真正的定向目标，`task_ids` 等价于以任务根为目标（根目标=全树冻结，即旧"全局分发"行为，系统只有一种 `subtree_barrier` 模式）。嵌套目标合并为最上层祖先，不相交目标共享一个 epoch。校验拒绝 acceptance 目标与终态（success/failed）目标（`append_notice_target_terminal`；终态目标的子树必然终态，不存在可下发的存活子树）。epoch payload 快照 `target_node_ids` 与各目标子树 ∩ 存活树的 `barrier_node_ids`，并写入操作员可见的 `runtime_meta.distribution`（含 `target_node_ids`）。追加通知**不做任务级 pause**：冻结由 hold 谓词强制（见下条）；任务处于人工暂停时 epoch 入队延迟（不自动恢复任务），目标节点处于人工暂停时该目标延迟到节点恢复（`payload.deferred_frontier_node_ids`，不拒绝）。
- Public distribution states are `barrier_requested -> barrier_draining -> distributing -> resume_ready`, with `failed` as the operator-visible terminal state of a distribution whose turn could not produce a valid decision; the authoritative source is the task-level distribution state plus the epoch payload's `target_node_ids` / `barrier_node_ids` / `drain_pending_node_ids`. **子树冻结（hold）不用任务暂停也不用 cancel**：`main/runtime/subtree_hold.py` 的 `resolve_subtree_hold_epoch_id(...)` 是单一判定源——分发状态在阻塞集（五个活跃态 + `failed`）内、且节点在 `blocked_node_ids` 快照中或沿 `parent_node_id` 链命中任一目标（每波重推，防 drain 期间新物化子孙逃逸；`distributing` 态的 frontier 成员豁免）时返回 epoch id。判定命中后还按 epochs 表回查校验（调用方注入 `get_epoch_state`）：epoch 库内状态已终态（completed/cancelled/cancelled_by_task_delete）或查无（`none`）说明 `runtime_meta.distribution` 是陈旧缓存——不冻结并经 `on_stale_hold` 落 WARN（meta 是缓存、epochs 表是权威）；`failed` 属设计性冻结不放行；校验通道自身异常时保守维持 hold。判定挂在 `NodeRunner.run_node` 入口/回环后与 `react_loop._check_pause_or_cancel` 的每个安全检查点（顺序：取消 > 人工节点暂停 > 分发 frontier 分支 > hold > 任务暂停），命中抛 `DistributionHoldError`；`TaskNodeDispatcher._run_entry` 对包括根在内的所有节点保持 future **pending**（绝不 set_exception——那会被父管线转成 spawn 运行时错误），父管线经 pending future 自然停摆，子树外分支照常执行。**冻结类控制信号绝不损坏在飞工具回合**：`DistributionHoldError`/`NodePausedError`/`TaskPausedError` 在 `_run_call` 的工具异常兜底之前直通重抛，不会变成模型可见的 `Error executing <tool>` 结果——批后帧写因此不执行，帧保留执行前快照（`pending_tool_calls` 携带原 tool_call_id），恢复后按原 id 重放（spawn 轮凭同 cache_key 幂等重入）。工具看门狗轮询在长等待工具（如 spawn）内部命中检查点而中止在飞协程时：hold 期间的取消按传播处理（`_should_propagate_child_pipeline_cancellation` 含父节点活动 hold 判据），spawn entries 不被盖 `error`、轮保持未完成可重入。**未物化豁免**：节点持有未完成且仍有未物化 entry 的 spawn 轮时（`main/runtime/subtree_hold.py` 的 `spawn_round_has_unmaterialized_entries(...)`，与 drain 的待物化集合同源），`run_node` 入口的 hold 检查（`_blocking_subtree_hold_epoch_id`）与 `react_loop._check_pause_or_cancel` 的每个安全检查点都放行而不抛 `DistributionHoldError`——子节点物化只可能由该节点自己的协程产出，在物化前中止它，就等于让 drain 等一个只有释放屏障才可能产生的结果。入口与检查点必须用同一判据：节点被停摆后重新进入 `run_node` 时，入口检查先于任何检查点执行，只改检查点会让它在毫秒级再次冻结。豁免只放宽「何时停」不放宽「停在哪」：物化完成（`_materialize_spawn_batch_children(...)`）后谓词立即为假，节点在下个检查点照常冻结在安全相位；同一谓词也是恢复优先级的放行判据（`_distribution_priority_blocks_recovery`），使持有该轮的节点按原 tool_call_id 重放这一轮，而不是让模型重发一轮新的 spawn（新 call id 会造成双轮并存）。任务取消、任务暂停、人工节点暂停不适用该豁免，仍即时生效；`run_node` 回环后的 hold 检查与「无标志却收到取消」的冻结转换不放行（那两处节点的回合已结束或来自引擎级中断，放行会让节点在屏障内落终态）。`run_node` 对无取消/暂停标志却收到取消且处于活动 hold 的节点转抛 `DistributionHoldError`（冻结而非落 failed，`cancel_nodes` 随后对仍 pending 的 future 强制落终态、不被 held 形态死锁）；无活动 hold 的同形取消同样不落终态（引擎级中断契约见本文「Node-Level Pause and Recovery」）。派发 future 的所有等待点（`_DispatchLease.wait_for` 与 `execute_node` 直接等待）经 `asyncio.shield` 隔离：等待方被取消只打断等待本身，不得顺着 `_fut_waiter` 取消子节点的派发 future（否则子节点"活着但 future 已死"，释放复活永久跳过）。drain 的完成判定是"协程确已停摆"：barrier 节点的 dispatcher entry task 已结束（held）或无 entry 且 frame 相位在安全集（`before_model`、`waiting_tool_results`、`after_model`、`waiting_children`、`waiting_acceptance`）；drain 也等待活动 spawn 轮完成子节点物化（`_barrier_materialize_pending_entries`，`status` 为 `queued`/`running` 且未被 review 拦下的未物化 entry 都把其父节点计入 `drain_pending_node_ids`）；该等待恒可满足——未物化豁免保证在飞批次先物化再停摆，已停摆的父节点由驱动器的 drain 自愈踢起收尾（见下条驱动器）。`NodeRunner.reconcile_spawn_entry_child_bindings(...)` 继续在 spawn 执行与屏障检查前修复 parent-entry/child-row 漂移。
- Epoch 由每任务**单飞侧置驱动器**推进（`TaskActorService.ensure_scoped_epoch_driver` → `_drive_scoped_epoch` 循环调用波次函数 `_run_distribution_epoch`，波次结果 `deferred/draining` 轮询等待、`advanced/promoted` 立即续波、`completed/failed/idle` 退出）。波次抛异常时按 epoch payload 的 `wave_crash_count` 有界重试（`_DISTRIBUTION_WAVE_CRASH_RETRY_LIMIT`），超限显式 `_fail_distribution_epoch`。驱动器不得「记一条日志就退出」：epoch 完成时才清 meta 并释放 held entries，所以驱动器的每次退出都必须落到一个显式终态，否则子树屏障无人解除（事故：`task:e5d3d0c2fbe1`——波次异常只记日志即退出，任务停在 `distributing`、两个节点 `in_progress` 无进展）。`draining` 波次带 **drain 自愈**：对 `materialize_pending_entries` 中「父节点非终态、未被人工暂停、无存活 dispatcher entry，且帧仍保留该轮重放意图（`_parent_frame_intends_round_replay`）」的已停摆父节点调 `resume_node(...)`，踢它按原 tool_call_id 重放该轮并完成子节点物化；没有这一步，drain 等的是只有「释放屏障」才可能产生的物化，与停摆的父节点互等。每个 `父节点+轮` 有冷却期（`_DRAIN_KICK_RETRY_SECONDS`，记账为 epoch payload 的 `drain_kick_rounds`：键 → 上次踢起时刻），避免逐秒重复 resume 同一个未缓存 review 的轮，同时保证「踢了但没进展」的轮能在冷却过后自动再踢（一次失败不能永久卡住 drain）；帧里已无该轮重放意图时不踢（重放不成立，踢了只会让模型发一轮新的 spawn）。ensure 挂点：追加通知（web 模式经 `run_distribution_epoch` worker 命令）、`run_task` 入口、节点恢复命令、以及 `MainRuntimeService._distribution_reconcile_loop` 每 60s 的对账（`TaskActorService.reconcile_distribution_drivers()`：只接管本进程持有派发器的任务——驱动器与释放对象都是进程内资源，扫全库会在多 worker 部署里替别的 worker 武装波次；进程级中断由 worker 重启后的 `run_task` 入口 ensure 负责。两条判据：活跃分发态而无在跑驱动器 → 重新 ensure；epoch 已终态而释放账 `release_pending` 未销 → 补跑释放，合同见下条完成序列）。`run_task` 对活跃 epoch 只 ensure 驱动器、**不再 control_only_return**：根在屏障内时根 entry 被 hold、`run_task` 自然等待到释放（等价旧全局冻结但任务不显示暂停）；根在屏障外时无关分支继续执行。排队 epoch 提升时按**自身** `target_node_ids` 重建屏障与 frontier（不得继承旧 epoch 快照）。分发回合是紧凑控制回合（`node_message_distribution.md` + internal tool `submit_message_distribution`），经 `NodeRunner.run_node` 直接执行（不经 dispatcher，避免解析被 hold 的 entry future），共用节点发送端 token preflight。每个 frontier 节点按**回合时自身状态**分支：被验收检验中（handshake `waiting_acceptance`/`waiting_block_verification`）→ 决策回合（`node_notice_inspection_decision.md` + `submit_notice_inspection_decision`，决策回合按当前工具名解析返回参数并校验 `action` / `reason`，二选一：`resume_execution` 打断验收——决定先落 epoch 的 `decision_records`，波次在前沿回合循环结束后按「`decision_records` × `wave_effects`」差额执行副作用（按序 cancel 协程→`invalidate_acceptance_node`→丢弃验收 frame→以合成结果解析 future，无存活 entry 时走 `_interrupt_acceptance_for_notice_retry` 冷路径恢复执行节点），并把 `(effect='acceptance_interrupt', node_id)` 记进执行账：验收已终态则记 `skipped='acceptance_terminal'` 不再补做。决策账与执行账分离是波次重放的前提——两者之间的任何中断都会在下一波被补做，已入账的不会重复作废验收；决策语义只经 epoch payload 传递，不经 `NodeFinalResult` 回传（该模型的字段是持久化交付契约，越界值在构造期即被 Literal 拒绝）。`_handle_acceptance_node_result` 对 `NOTICE_INTERRUPT_REASON` 走不消耗拒绝预算、不发交接消息的重试分支；或 `continue_acceptance` 验收继续、验收节点收到含原文的告知）；等子节点（wait_for_children + 存活子节点）→ 控制回合；自处理中/叶子 → 无模型回合，直接本地并入（目标落 pending 记录，级联接收者用已投递的信箱行）。`submit_message_distribution.children` 契约不变：每个存活子节点恰好一条决策，`distribute`（非空 `message`，子节点进入 next frontier）/ `skip`（非空 `reason`）/ `terminate`（子树取消并强制终态，非空 `reason`）；`action` 优先于 `should_distribute`。空/残缺决策触发回合内修复重试（≤5 次，逐次记入 `payload.debug_trace`）；最终失败或 preflight 失败 → epoch `failed`（`error_text` + `payload.failure_node_id`）。失败 epoch 保持子树冻结（`failed` 在 hold 阻塞集内）并标记**任务级暂停**作为操作员释放杠杆（任务大厅显示「任务暂停」）；queued 消息落为目标节点的 pending 记录保持持久；`runtime_meta.distribution` 保留 `state="failed"` + `error_text` + `blocked_node_ids`。每个失败 epoch 一次 `task_distribution_error` heartbeat 事件回到源会话（详见 `heartbeat-system.md`「Task Distribution Error Delivery」）。解除只能显式：resume 任务（可见状态降级 `resume_ready`、清空 blocked/targets，hold 随状态关闭；epoch 行保留 `failed` 作取证）或重新追加通知（新 epoch 覆盖）。控制回合的 `tool_choice` 强制与 provider 边界规范化不变。`next_frontier_node_ids` 由驱动器直接续波（不再逐波重入队任务）；epoch 完成时才 re-enqueue 以恢复普通执行。
- Delivery to child execution nodes creates durable mailbox rows; delivering to a terminal execution node reactivates it and invalidates/detaches its old acceptance node instead of deleting it. 目标节点自己的消息保持 node-local pending notice records（id `root-notice:`/`target-notice:`）+ epoch `decision_records`，不进自己的信箱。**消息三态账本**：`delivered`=待处理；控制/决策回合处理过→`consumed`（`consumed_at` 落时刻、`merged_at` 为空，节点详情显示「已消费」，即使内容要等子节点回合结束才真正注入）；内容真正并入模型上下文（在途刷新或恢复注入后的消费步骤）→`merged_at` 落值并归档进 `append_notice_context`，显示「已并入上下文」。普通路径消费即并入（consumed+merged 同步落），归档只发生在并入时——因此归档记录一律视为 merged（旧数据无 `merged_at` 字段时以 `consumed_at` 兜底，免迁移）。待注入判定（`_notification_awaits_injection`）= delivered ∪ consumed-未merged，恢复路径、在途刷新与 pending 计数共用。Ordinary execution re-scans newly delivered notices at each `before_model` safe boundary via append-only refresh, never a history rebuild. Consumed notices persist into node-local `append_notice_context` so they survive stage compaction; spawn review reads them as `consumed_distribution_notices` and treats the latest consumed distribution notice as the effective current requirement when it conflicts with older wording. Notice records are append-only history, never deleted or rewritten, but each record carries a `superseded_at` marker: a superseded record stays in the context for forensics yet is excluded from the raw notice tail window and from compression-segment rollups, so an already-answered directive (a resolved block-verification activation, a replaced handoff notice) stops resurfacing as a live instruction. Superseding is applied at handoff boundaries: when a blocked-verification round settles, and whenever a newer handoff message from the same source node replaces an older one (see the Acceptance paragraph below). The root final acceptance node likewise folds these consumed notices — plus the node's still-unconsumed pending records, deduplicated by message text — into its turn tail block on every rebuild (see the Acceptance paragraph below). Raw unconsumed notices render as a dedicated non-compressible tail block ahead of stage compact blocks (`STAGE_COMPACT` 与遗留 `STAGE_EXTERNALIZED`)；历史数据中已存在的归档压缩段（`compression_segments`）继续按压缩通知尾段渲染。
- **上行传播（epoch 完成时、hold 释放前）**：每个目标的完整祖先链、各祖先的存活验收子节点、以及任务最终验收节点各收到一条信箱投递（转述+原文，`source_node_id`=目标），无控制回合——祖先下次执行/恢复时自然消费，运行中的祖先经在途刷新并入；祖先多处于等子节点状态，`stamp_distribution_target_pending_notice_state` 使其按 `wait_for_children` 挂起到回合结束。失败 epoch 不上传播。根目标时祖先集为空，只有最终验收节点收到告知。
- Epoch completion and delayed notice consumption are separate states: once the epoch reaches `completed`, `runtime_meta.distribution.state/mode/active_epoch_id/blocked_node_ids` are cleared even while `pending_notice_node_ids` / `pending_mailbox_count` remain — those fields only mean nodes still hold node-local pending notices or mailbox rows awaiting injection, not that barrier/distribution is still active. 完成序列固定为：兜底补目标 pending 记录（幂等）→ 上行传播 → 清 meta（hold 谓词关闭）→ `_release_scoped_epoch_holds` 对每个 held entry `dispatcher.resume_node`（人工暂停的节点不被唤醒；future 已解析而节点非终态的搁浅 entry——被取消或携带未消费异常——弹出残骸重建 entry 复活并落日志）→ 释放校验清扫（延迟复查被复活的节点：仍呈"future pending + entry task 已停 + 无活动 hold"卡死形态则再 resume 一次并 WARN，复查仍卡死落 ERROR；每任务替换式单飞，`run_task` 退出时取消）→ 失速时钟复位 → re-enqueue → **销账**。`release_pending` 是这条尾段的释放账：epoch 置 `completed` 的那次写入里先记下 `barrier_node_ids`，整条尾段跑完才清空。它存在的必要在于「清 meta 之后、释放之中」抛异常时的形态无法被任何状态判据认出——meta 已清、hold 谓词永久关闭、A3 校验清扫只在释放跑完时才安排，任务会留成非终态节点 + pending future + 无横幅。对账器据此补跑第二条判据：epoch 已终态而 `release_pending` 仍非空 → 重新释放，成功后才销账（再抛则留给下一轮，不空跑也不漏跑），A3 承诺的"冻结→释放的终点只能是在跑或显式告警"由此闭合。冻结/复活的每个静默点都有日志锚点：`_run_entry` 的 hold 冻结落 WARN（task/node/epoch），entry 协程异常在 set_exception 前落 ERROR（future 可能无人消费，日志是唯一痕迹）。被 `blocked_node_ids` 覆盖的节点在屏障释放前跳过中断回合恢复。**失速监督**：分发冻结期间任务无可见输出是预期行为，`classify_task_stall_reason` 对活跃/失败分发态返回 `distribution_barrier`（不可操作，不发 `suspected_stall`）。A node holding an incomplete `spawn_child_nodes` round resumes with `resume_mode=wait_for_children` and consumes its held notice only after that round disappears (durable notice now, prompt consumption later). The first ordinary consumption of a held notice preserves the previous provider-facing request as an exact prefix: the first hop adopts the latest actual request's internal-form `request_messages` as the send-side seed scaffold and appends the projected records beyond the scaffold's coverage point (the held notice, replayed rounds) as an explicit delta tail, while `runtime_frame.messages` / durable rebuild remains the semantic node history and shapes the request body only when the scaffold is unusable — a round carrying a `fallback_*` `request_seed_source` diagnostic.
- Acceptance nodes are created eagerly with the spawn entry (root `final_acceptance` at task creation) and activated by execution success. Execution success is not terminal when acceptance is required: `submit_final_result(success + final)` first persists the candidate result and moves the node into `acceptance_handshake.state="waiting_acceptance"`. Child and root acceptance share one rejection loop (`NodeRunner._handle_acceptance_node_result(...)` plus the handshake `rejection_count` it maintains), and that loop has **no rejection cap**: every rejection feeds acceptance feedback back into the execution node and reactivates it (`NodeRunner._acceptance_feedback_text` composes that notice from `summary` plus the `remaining_work` repair items and never reads `blocking_reason` in the repairable lane — the field is mandatory in the tool schema, so a verdict-taxonomy note written there would crowd out the problem list the executor needs; the blocked-verification lane uses `_blocked_verification_feedback_text`, where `blocking_reason` is the contract slot for what the execution node must do next) — the counter is feedback/forensics only, the handshake persists no budget, and no rejection count ever terminalizes the node pair — while execution failure during any retry cancels the waiting acceptance path. Two drivers feed that loop. Child spawn acceptance is driven synchronously: `NodeRunner._run_child_pipeline(...)` loops `execution -> acceptance` until the acceptance verdict stops being a kickback (`partial`) — with the uncapped loop that means it only ends on a pass, an execution failure, or an external interruption. Root final acceptance is driven by the task actor through **two selectors that together cover every live round**. `TaskActorService._resume_pending_notice_nodes(...)` dispatches whoever still holds an unconsumed notice — that is how a freshly handed-off round starts. The acceptance node consumes and merges that notice as the very first step of its own round, so an interrupted round is invisible to that selector: it lives only in a dispatcher entry, which any process exit destroys. `_resume_inflight_final_acceptance(...)` therefore reads the commitment from the handshake instead (`NodeRunner.resumable_final_acceptance_node_id`: inspected root non-terminal, handshake still at `waiting_acceptance`/`waiting_block_verification` **and naming this node**, acceptance node non-terminal, not operator-paused, not frozen, no live dispatcher entry, no distribution hold state) and re-dispatches it with one `final acceptance round re-dispatched after interruption` WARN so the recovery is attributable in the worker log without DB forensics. Without that second selector `run_task` falls through to `dispatcher.execute_node(root)` and the inspected node runs a whole fresh round while a verdict on the previous submission is already outstanding (incident: `task:c7f1dbfae6e2` — acceptance interrupted mid-round, the task spent three hours re-executing instead). Both selectors share one settlement path (`_settle_final_acceptance_notice_result`) and one terminality decision (`_terminal_result_after_notice_resume`), and a hit ends the pass without dispatching the root, so the root and the acceptance node never both run in one `run_task` pass. Re-dispatch cannot become a loop: it matches only while the handshake names the node, and any landed verdict moves the handshake to `waiting_execution_retry` or a terminal state. The returned verdict is routed through the same loop — a kickback (`partial`) refreshes the distribution state so the rejection feedback notice drives the next `run_task` round onto the execution node, a pass settles `accepted`. The notice-resume terminal short-circuit (`_terminal_result_after_notice_resume`) never terminalizes a failed final acceptance: only `passed` settles the task, while a rejection hands control back so the driver reactivates the execution node. Task-level verdict ownership mirrors the split: `metadata.final_acceptance.status="failed"` is written only by the blocked-verification allow path (`_allow_blocked_failure`, where the execution node genuinely failed), while a raw node-level acceptance failure syncs only display-layer fields (`check_result` on the inspected node) — an out-of-band acceptance failure can never shortcut the task to a terminal state around the rejection loop. Root final acceptance adds three guards against notice/submission races. Premature-acceptance gate: the root final acceptance node is a notice-resume target only while its execution node's handshake sits at `waiting_acceptance` or `waiting_block_verification` (a submitted result awaiting verdict); while the execution node has not submitted (idle handshake — the typical shape after a restart recovery that still finds unconsumed acceptance notices), notice-resume skips the acceptance node and control falls back to executing the root, so acceptance never judges a deliverable that is still being produced. Freeze: while the inspected root execution node still holds an unconsumed notice (node-local pending notice record or delivered mailbox row), the final acceptance node is held — `NodeRunner.run_node` returns a deferred `partial` before any model turn even if a verification notification is delivered, so acceptance cannot reach a verdict ahead of the execution node consuming its notice and resubmitting; the hold releases once the execution node has no pending notices. Terminal reset: if the execution node submits `success + final` while its final acceptance node is already terminal (`success`/`failed`), the runtime resets that acceptance node to `in_progress` and proceeds with the normal handshake so the new submission is re-verified, instead of delivering a verification notification to a node that can never consume it. All three guards are root-final-acceptance only; child spawn acceptance is driven synchronously by the child pipeline and is unaffected. The acceptance bootstrap is written once, at `NodeRunner.create_acceptance_node`, and no refresh rewrites it: `NodeRunner._refresh_acceptance_node_metadata` maintains metadata only (criteria template + recovery fingerprint). What a given round must judge reaches the model through two append channels: the handoff notification (`_acceptance_handoff_message` — inspected node id, the submission's `result_payload_ref`, a summary bounded by `_ACCEPTANCE_SUMMARY_CHARS`, and the instruction to judge only what that ref resolves to), and a turn-only tail block `_build_messages` adds for acceptance nodes (`_acceptance_turn_tail` — current submission refs and bounded summary, plus the root's appended requirements). Delivery bodies therefore never stack up in the verifier's context: after a kickback resubmission the superseded text is no longer a judging target, and the child pipeline's continuation notification follows the same bounded shape. `result_payload_ref` is the pointer that makes this sound: it is a pointer to the current submission, not an append-only record — whenever a persisted result payload's content changes (a kickback re-run resubmits), the ref and its summary are cleared and the fresh payload is re-externalized as a new `node_result_payload` artifact, so the acceptance turn tail, the handshake `latest_execution_result_ref`, and any handoff notification embedding the ref all resolve to the latest submission; every submission keeps its own artifact as the audit trail, and a recovery reset that clears the payload clears the ref with it. Cache consequences (bootstrap = head-probe anchor) are owned by `context-and-cache-troubleshooting.md`「验收 bootstrap 定稿与回合尾块」. Every re-verification round of a child execution node is activated by that explicit continuation message, which supersedes that execution node's older handoff notices in the acceptance node's `append_notice_context`. Notice merge: the root's notices — unconsumed `pending_append_notice_records` plus consumed `append_notice_context.notice_records`, deduplicated by message text — render as a `追加任务要求` block inside the turn tail, and only when the accepted execution node is the task root (the same gate as the freeze guard), so appended requirements are judged alongside the criteria. The block is pull-on-rebuild (a pure function of the execution node's notice state, never an incremental push), and the acceptance recovery fingerprint carries no delivery text at all: it keys on the criteria template plus the submission's `result_payload_ref`, so notice arrival and resubmission alike leave acceptance-node reuse intact while still distinguishing which submission a stored verdict judged.
- A voluntary execution failure is not terminal either: `submit_final_result(failed + blocked)` passes through a blocked-verification gate (`NodeRunner._maybe_gate_blocked_submission`, invoked in `run_node` before any terminal marking, execution nodes only) that hands the blockage claim to an acceptance node regardless of whether the parent ever requested acceptance. Verifier resolution order: handshake-bound acceptance node, then an acceptance child, then the root final acceptance node, then an ad-hoc `blocked-check:` acceptance node created from the dedicated `blocked_verification.md` prompt (metadata `blocked_verification_only`; it never registers as the task's final acceptance and is closed as success once its execution node finishes normally). The activation notification carries the claim (summary, `blocking_reason`, `remaining_work`, result payload ref) plus mechanical stage signals from `execution_stage_gate_snapshot` (stage goal, tool-round budget vs used, whether the stage has substantive rounds), so the verifier judges against evidence instead of prose. Verdict semantics invert normal acceptance: verifier `success` with non-empty evidence means the blockage is justified and the failure is allowed; verifier `failed + final` means unjustified and the execution node is sent back through the ordinary rejection loop; `success` without evidence is an invalid verdict (one repair round, then treated as unjustified); verifier `failed + blocked` means verification itself is blocked (one retry round, then the failure is allowed with an "unverifiable" marker). Blocked rejections increment the same handshake `rejection_count` as quality rejections, and that loop has no cap either: a rejected blockage claim is always sent back to the execution node, so a repeated claim is never allowed merely because it was rejected a fixed number of times. A normal acceptance `failed + delivery_status="final"` is the repairable rejection path and always reactivates execution; an acceptance `failed + delivery_status="blocked"` is reserved for an execution anomaly or non-retryable block, terminalizes the acceptance/execution result, and does not re-enter the rejection loop. The handshake state while a claim is under review is `waiting_block_verification`; an allowed failure settles it to `rejected_terminal`. Every round is auditable in the execution node's `metadata.blocked_verification_log`. When a gate round settles — blockage allowed or rejected back into the ordinary rejection loop — the runtime supersedes that execution node's activation/repair notifications in the verifier's `append_notice_context`, so the resolved "verify only the blockage" directive cannot resurface from the notice tail window and steer a later ordinary acceptance round back into block-verification mode.
- Verdict text on acceptance nodes is never silently destroyed. Reactivation for retry (`_reactivate_node_for_retry`), terminal-acceptance reset, and execution-failure cancellation all stash the prior non-empty `final_output`/`failure_reason` into the acceptance node's `metadata.rejection_history` before clearing fields, and the cancellation path keeps an existing non-empty verdict in place, recording the cancellation only in `metadata.canceled_by_execution_failure`.
- Task tree depth is bounded by two independent layers. The static layer is a creation-time clamp: `main_runtime.default_max_depth` (default 1) applies when `create_task` receives no `max_depth`, `main_runtime.hard_max_depth` (default 4) caps any requested value, and each node carries `can_spawn_children=(depth + 1) < task.max_depth` so the `spawn_child_nodes` tool is structurally gated — these are mechanical backstops, not model judgment. The dynamic layer is the spawn review: every new spawn batch (per call `call_id`; cached for retries, materialized rounds are never re-reviewed) is judged per candidate by the external inspection lane `spawn_child_review.md` via `review_spawn_candidates`. Review context carries `path_nodes` (root→parent path with each node's `goal` / `prompt` / `stage_goal`, full prompts being the task-boundary evidence), `parent_stages` (all completed stages plus the active stage of the spawning node, so the verdict aligns with stage progression), and `tree_summary` (compact outline of branches outside the path plus node count, so width explosion still registers). **送审的候选 `spawn_request.requested_specs` 是解析后的派生结构，不是模型入参的字面拷贝**：每个候选带一份 `runtime_nodes`，元素与运行时真正创建的 NodeRecord 一一对应——`execution` 就是候选自身的 `goal`/`prompt`/`execution_policy`，当 `_spec_requires_acceptance` 判真时再多一条 `acceptance` 节点（goal 为 `_SPAWN_ACCEPTANCE_GOAL_PREFIX` + 候选 goal，挂在该 execution 子节点之下、只在它整条管线终态后激活）。这个前缀在节点创建与审查载荷之间只定义一次，回显即账本。把 `acceptance_prompt` 平铺回候选对象会让评审把它读成"验收内嵌在生成节点里、等于自产自销"，进而建议把生成节点和它的验收排进同一批，而那一形态才是被禁止的（事故口径）。原始工具入参另存于 `spawn_operations[round]['specs']`，两层账本并存。 The verdict either admits candidates or returns per-candidate block reasons and next-step suggestions to the parent node. One review rule is a same-batch data-dependency gate keyed strictly on *consuming sibling outputs*: children of a single `spawn_child_nodes` call start concurrently with no ordering guarantee and the call returns only after every child pipeline is terminal, so a candidate that inspects or aggregates its siblings' outputs is blocked with split-batch guidance — spawn the depended-on generators first, then the inspector in a later batch; a candidate's own `requires_acceptance` never counts as that violation. 建议本身同受门禁约束：按"无理由串行拆分"拦截时必须给出可一次执行完的合并指令（把互不依赖分支合进同一次调用的 `children`，验收用各自 `requires_acceptance`，不是再多几轮），且任何建议都不得指示另一条规则禁止的动作。 A review-blocked child is returned to the parent as `SpawnChildResult.review_blocked=true` — a machine-readable "not created, never executed" marker the parent must act on instead of counting the branch as completed; the blocked entry keeps `status=success` only as internal terminal bookkeeping and can never mask a live bound node (see the recovery invariant below). **两种拦截是分型的**：模型真给了否决裁决时 `check_result=派生已被拦截`、`failure_info=None`；检验车道自身故障（模型链缺失 / provider 异常 / 连续拿不到可解析判定）时 `check_result=派生未审查（系统故障）`、`failure_info.source='runtime'` + `delivery_status='blocked'`，`spawn_review.review_outcome` 同步落 `system_failure` 供账本与投影判定。故障态返回给父节点的文本直接给出可用的终态出口（`submit_final_result(status='failed', delivery_status='blocked', blocking_reason=...)`，且须是本回合唯一工具调用），因为「提交阻塞」这种说法在模型当轮工具表里没有对应项，只会诱发它找一个不存在的动作或用自然语言结束回合（节点不终态化）。注意分型不等于可重审：一个批次无论裁决还是故障收口，round 都会被标 `completed`，同 `call_id` 的重放走 `completed` 短路直接返回存量条目、不会再问模型——要重新评审只能由父节点以新的 `tool_call_id` 再派生。 Spawn review usage merges into the parent node's `token_usage` via node-level accounting, so the task-level token aggregation includes review calls. 该车道既不预检也不落 actual-request 工件，父节点聚合又无法反推是哪一次评审花的，因此它自带两份取证字段随 `spawn_review` 载荷持久化、并经节点详情的派生审查轮投影暴露：`review_attempts`（本次判定实际发出的请求次数）与 `review_request_chars`（检验请求正文的字符规模）。模型连续回不可解析的判定时按 `_SPAWN_REVIEW_MAX_ATTEMPTS` 封顶，封顶后与 provider 异常落到同一个 fail-closed 默认结果（`allowed_indexes` 为空、每个候选都带 `blocked_specs`、`error_text` 指明是耗尽还是异常），不存在无上限重发。There is no task-level depth capping lane.
- Force delete wins over message distribution: before task rows disappear, the runtime cancels active epochs, cancels or purges mailbox rows, clears `runtime_meta.distribution`, and prevents later queue wakeups from resuming the deleted task.

对于异步任务的回传：任务终态通过 task terminal callback / heartbeat 回到原 CEO 会话；heartbeat 的修复/回退语义与 `terminal_output` / `root_output` 双车道详见 `heartbeat-system.md`「Task Terminal Repair Contract」。

当前 frontdoor 的上下文组织以阶段工作集为近场上下文：最近 3 个完成普通阶段与当前 active 阶段保留完整原始窗口（含工具调用），更早的完成普通阶段按阶段归属原位移除工具调用并以 compact 块回插原位，阶段之外的用户可见对话原位保留（表示规则见本文「Runtime Contract Lane」）。阶段块（`[G3KU_STAGE_COMPACT_V1]` / `[G3KU_STAGE_EXTERNALIZED_V1]` / `[G3KU_STAGE_RAW_V1]`）以 system 角色落地：运行时标注的已完成阶段摘要属压缩元数据、非对话内容，assistant 角色会诱导模型把块当成"自己上一轮说的话"而在续写位置仿造/回显（角色合同与迁移期双角色识别见 `context-and-cache-troubleshooting.md`「压缩块的格式与字段语义」）。归档压缩阶段（`stage_kind="compression"`）是历史遗留表示数据：继续规范化与渲染为外置块，运行时不产生新的归档。已在 `token_compression` 边界收口的阶段（`context_visible: false`）不再渲染任何块，也不占 raw 保留名额（合同见本文「Runtime Contract Lane」与「Frontdoor Context Compression (Current Contract)」）。全局语义摘要层不参与 prompt assembly；长会话的远场连续性由权威请求体基线、canonical context 链与压缩合同承担，收缩边界详见本文「Frontdoor Context Compression (Current Contract)」。

前门提示词分成“静态协议层”和“动态注入层”两部分理解：

- `g3ku/runtime/prompts/ceo_frontdoor.md` 承载 CEO frontdoor 的稳定协议（角色规则、任务/工具通用约束、stage-first 高优先级协议）；稳定 system prompt 只保留最小的 capability exposure revision 锚点，不把可见 tool/skill 名单写进稳定前缀。
- `g3ku/runtime/frontdoor/prompt_builder.py` 负责把稳定协议与少量环境提示装成 base prompt；`g3ku/runtime/frontdoor/message_builder.py` 按本轮会话状态动态注入 retrieved context、memory hint 与当前轮运行时工具合同所需的数据。
- 用户消息时间锚点：转录持久化始终保持用户原文（RAG ingest、web UI 等消费方依赖 raw content），`message_builder._history_message` 在投影历史时把记录自带的 `timestamp` 渲染为 `[消息送达时间] <本地时间 +08:00 星期>` 行追加到用户消息投影副本末尾（渲染 helper 在 `g3ku/core/timefmt.py`；heartbeat/cron 内部消息不装饰，各自携带时间锚点，见 `heartbeat-system.md`「Internal-turn time anchors」）。装饰值派生自记录固定字段而非 now()，跨回合字节稳定，不打断 provider 前缀缓存；仅"当前用户不在历史、直接追加进请求"的兜底路径用 now() 盖章，下一回合投影自动切换回记录时间。硬性规则：所有基于内容相等性的比较（当前用户匹配 `_cron_prompt_equal`、暂停回合种子对账 `_reconcile_paused_user_turns_into_seed`）必须先 `strip_arrival_time_stamp` 剥离装饰再比较，否则已装饰投影与原文不相等，会被误判为新消息造成重复注入；新增任何"按用户消息文本去重/匹配"的逻辑都适用同一规则。
- `prompt_batch` 批次回合内容合并：`prompt_batch` 只以批次最后一条输入驱动回合，`prepare_turn` 因此会把同批次其它输入的内容块（文本与附件，各自按其元数据展开，如 web 上传、外部 `image_url` 块）按到达顺序并入当前回合的模型请求内容，相等块去重——否则用户连续发送时较早消息的文本与图片会彻底缺席模型请求。较早输入的附件文件过期/缺失时，该输入降级为原文并入（图片缺席），不拖垮整批回合；当前回合输入仍保持严格失败语义。合并只发生在请求构建期：批次各输入本体不改写，转录行各按原文落盘；中途追加的 follow-up 在 prepare 之后才进入批次上下文（`call_model` 前消费边界），与该合并无重叠；心跳/cron 等内部回合不配置批次上下文，不参与合并。
- CEO/frontdoor 的生产执行面是自研步骤循环，入口到收尾固定经过 `prepare_turn -> call_model -> normalize_model_output -> review_tool_calls -> execute_tools -> finalize`。`call_model` 和 `execute_tools` 共用同一份 frontdoor runtime tool bundle；`submit_next_stage` 这类运行时注入的 stage protocol tool 必须同时对模型“可见”且在 `execute_tools` 里可真实执行，执行环节不得从 `state.tool_names` 重建第二套工具表。`normalize_model_output` 不因阶段预算耗尽拦截纯文本回复：文本收尾直接 finalize，由 finalize 关闭活动阶段；唯一的文本打回（阶段无实质工具轮时限一次）与派发后的 Reply naturally 尾注，详见 `tool-and-skill-system.md`「阶段门控与 callable 收紧」。阶段门控与 mixed-batch 语义详见 `tool-and-skill-system.md`「四个概念必须分清」。
- 可见 `call_model` 轮次有专用的流式 assistant 文本 lane，`RuntimeAgentSession` 是其合并边界：流式块追加进 `latest_message`，`inflight_turn_snapshot().assistant_text` 保持最新，并以节流轻量事件代替整份 `state_snapshot` 重建；该 lane 只承载文本，工具进度 / stage trace / canonical context 仍走各自的低频事件/快照通道。可见发送的硬回退边界：首个可见流式文本块出现前允许 provider retry / API-key rotation / model-chain fallback，首个可见块之后同一发送必须停止透明回退——一个可见气泡由多个 provider/model attempt 拼接属于 runtime bug。内部不可见发送（如 `token_compression` helper）不得复用该可见回调路径。
- 内部运行时错误后的 async-dispatch 恢复遵守同一可见性规则：若 `create_async_task` 已成功且当前轮已有用户可见 assistant 文案，恢复必须保留该文案；通用回退文案仅适用于没有任何可见文本幸存的窄场景。
- 没有“有效阶段”（`active_stage_id` 为空，或当前阶段已 `transition_required=true`）时，CEO/frontdoor 的 agent-facing `frontdoor_runtime_tool_contract.callable_tool_names` 保留全量 callable 并把 `submit_next_stage` 置首，配合同批提交协议与执行期宽限兜底；execution / acceptance 节点同样不再收紧，`submit_next_stage` 置首。这些都只影响模型决策边界，不同步收紧 provider-facing `tools` schemas——前门继续发送当前路径上 RBAC-visible concrete tools 对应的 `provider_tool_names` bundle，仅在 membership 真正变化时刷新并保持已持久化顺序稳定，`token_compression` 所在 send 沿用压缩前已持久化的 bundle。阶段门控、候选/修复车道与 provider 工具面详见 `tool-and-skill-system.md`「阶段门控与 callable 收紧」与「CEO Provider Tool Surface」。
- `g3ku/runtime/frontdoor/_ceo_create_agent_impl.py` 是 runner 入口，但前门主执行链以 `_graph_*` 节点为唯一权威路径。
- 对 CEO/frontdoor 主链路，每个请求只有一份最新契约：位于所有稳定前缀与持久化历史之后、当前 user 回合之前的一份 `frontdoor_runtime_tool_contract` system summary（运行时元数据、非对话内容）；模型输入末位保持当前 user 回合或合法的后续 assistant/tool 序列，避免契约块占据模型续写位置被回显。它属于“当前轮临时合同”，不是 durable history（剥掉/重注规则详见 `context-and-cache-troubleshooting.md`「append-only 规则」）。`normalize_model_output` 把 standalone 内部消息回显统一收口：运行时工具合同回显与阶段压缩块回显（`stage_prompt_compaction.py` 的三个 `[G3KU_STAGE_*]` 前缀，守卫按文本前缀自行判定）首次各导入一次私有修复提示——合同回显禁止引用合同，阶段块回显要求改用 `submit_next_stage` 或面向用户自然语言；重复回显转为不含内部材料的用户友好回退，阶段块原文绝不作为最终回复持久化或投递。可见答案尾部的契约/阶段块片段的剥除与渠道投递边界 `sanitize_channel_outbound_text` 的两类回显剥除共用 `stage_prompt_compaction.ECHO_STRIP_ENABLED` 开关；开关处于关闭状态时，尾部片段随回复原样放行。裁剪按“任意位置子串匹配块前缀”实现，会把用户答案中合法引用的 `[G3KU_STAGE_*]` 前缀一并截断，因此该开关在替换为指纹比对实现（仅剥离与当前注入真块逐字一致的回显）并同步恢复对应回归测试断言（`test_ceo_frontdoor_regressions` 尾段剥离与 `test_session_keys` 出站消毒）之前保持关闭。standalone 内部消息回显的收口（私有修复提示 + 重复回显用户友好回退）不受该开关影响。维护上区分 `dynamic_appendix_messages`（下一次重建时应追加的最新合同）与活动中的 `messages` / actual request JSON。

frontdoor / 节点的工具状态分两层，且都由持久化状态维护而不是只存在于某一轮 prompt 文本里：`candidate_tool_names` / `candidate_skill_ids` 是“RBAC 可见 ∩ 语义召回命中”的当前候选集合（语义召回不可用时退化为 RBAC 可见集合，不报错中断）；`hydrated_tool_names`（节点侧为 `hydrated_executor_state` / `hydrated_executor_names`）是本轮成功读取契约后被提升为下一轮 callable 的 concrete tool 集合。节点侧 canonical state 落在 runtime frame，是节点生命周期级 LRU，跨阶段切换 / pause/resume / restore 保留；frontdoor 侧落在 `RuntimeAgentSession._frontdoor_hydrated_tool_names` 加前门 persistent state，是 session 生命周期级 LRU，跨 turn 保留但每轮按当前 RBAC 可见集合过滤。两侧 LRU 只接受 concrete tool names；restore / recovery 只认 canonical frame / session state 中的 callable/candidate/hydrated/skill 字段，缺失时直接报“运行时工具合同损坏/缺失”。另有一条运行时边界：tool result 的错误判定不止 `Error: ...` 文本，任何 top-level JSON 带 `ok=false` 的工具结果 payload 都进入节点执行与 CEO/frontdoor 的 error lane。fixed builtin / candidate tools / candidate skills / hydrated tools 四个概念的完整区分、loader 准入与预算合同详见 `tool-and-skill-system.md`「四个概念必须分清」。

### CEO Frontdoor Round Tool Ownership

CEO/frontdoor 路径上，`frontdoor_stage_state.stages[].rounds[].tools` 是“哪些工具调用属于某一轮”的权威记录：

- `_frontdoor_stage_state_after_tool_cycle()` 在工具循环完成时写入精确的 round 级工具记录。每条记录携带稳定身份（`tool_call_id`）与展示字段（`tool_name`、`status`、`arguments_text`、`output_preview_text` / `output_text`、`output_ref`、`timestamp`、`kind`、`source`）；`tool_names` / `tool_call_ids` 是派生提示，不是第二真相源。
- stage/round 账本还携带展示文本：每个 round 记录有 `text`（该循环的轮中叙述），`submit_next_stage` 创建的阶段携带 `preamble_text`（随阶段调用发出的叙述，属于新阶段并渲染在其上方）。两者都是展示导向、只服务 Web 时间线，必须存活于每一个归一化跳板（`_frontdoor_stage_state_snapshot`、`canonical_context.py`、`raw_stage_renderer.py`），且不得喂给 prompt 组装或转录权威链。
- durable 转录每轮只存最终 assistant 文本；每轮叙述在 reload 时从 assistant 条目上持久化的投影 `canonical_context` 恢复。投影沿用最近 raw 阶段窗口，更早完成阶段以 compact 摘要保留，并对 raw round 内的工具正文与入参做转录专用限长。
- `SessionManager` 以追加写入维护 JSONL：只新增转录记录时追加新记录与一条尾部 metadata；插入、删除、替换或外部改变文件布局时全量重写。读取以尾部 metadata 为当前会话状态，旧版超大 assistant 快照在加载时投影一次，下一次保存收敛文件体积。
- `RuntimeAgentSession` 的 `latest_message` 只保存最新一段思考的 assistant 文本：模型调用开始标记段边界但不清空驻留文本，新一段首个流式 delta 到达时整体覆盖；`analysis` 进度事件直接替换驻留文本；UI 时间线从 stage/round 记录重建，`latest_message` 只是预览/回退气泡。

`RuntimeAgentSession` 重建 `canonical_context` 时的合同：

- round 已有 `tools` 时，直接信任 `round.tools`。
- 更老的 round 只有 `tool_call_ids` 时，只按精确 `tool_call_id` 回填。
- 仅按 `tool_name` 匹配是回归风险：会把后面同名的工具结果偷进更早的 round。若发现更晚的 `exec` 出现在更早的 round，先检查存储的 round 是否缺 `tools`、持久化的 `tool_call_ids` 是否稳定且唯一。

前门 tool promotion 与阶段工具显示是两条平行链路：执行循环直接基于 `raw_result` 处理 `load_tool_context` 的成功返回（不从 trailing `ToolMessage` / `result_text` 反推 hydration），`_frontdoor_stage_state_after_tool_cycle()` 只负责 round 记账与 `round.tools` 落盘；成功 payload 附带的 `tool_context_fingerprint` 只服务运行时 freshness / duplicate-read 判定，不属于 provider-facing schema 或 durable business state。前门 promotion 的唯一生产路径是步骤循环的 `execute_tools` 节点。

维护上，动态 skill/tool 提示块里的说明不能覆盖 `ceo_frontdoor.md` 的 stage-first 协议。权威顺序是：无活动阶段时若需调工具，必须把 `submit_next_stage` 与目标工具同批提交（`submit_next_stage` 起手、普通工具紧随，普通工具记入新阶段第一轮）；单独只提 `submit_next_stage` 不带工具、或单独调普通工具，都只获一次宽限执行，随后被硬拦；动态暴露里的 `load_skill_context` / `load_tool_context` 提示排在活动阶段之后（提示口径），执行层闸门对上下文加载器恒定放行，loader 调用不撞闸、不消耗宽限。执行层豁免不改变这套协议口径：节点暂停（`task_node_error`）心跳轮由运行时自动补开 `system_generated` 阶段（免模型起手 `submit_next_stage`），`memory_write` / `memory_delete` / `memory_note` 三工具免活动阶段即可调用，上下文加载器无论阶段状态均可调用——详见 `tool-and-skill-system.md`「阶段门控与 callable 收紧」。排查“普通工具撞上 no active stage”时，先检查稳定协议与 `stage_messages.py` 状态 overlay 是否一致，再检查 `prompt_builder.py` / `message_builder.py` 的动态提示是否与主协议竞争。`candidate_tools` / `candidate_skills` 两类候选的不对称语义详见 `tool-and-skill-system.md`「四个概念必须分清」。

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

chat 调用有两类边界：**单次（单轮）provider 请求的响应时间上限，默认 10 分钟**（`DEFAULT_PROVIDER_ATTEMPT_TIMEOUT_SECONDS`）；以及**可重试错误的逐模型轮数预算**——每个模型绑定「重试次数」（`retry_count`，0/未设置用默认 `DEFAULT_RETRYABLE_MODEL_ROUNDS=10`），预算耗尽才前进到链上下一个模型，全链模型预算耗尽即按链耗尽冒泡错误。次数预算是唯一权威上限，不存在无限重试循环；外层仍不应给整个 chat 调用套 `wait_for` 总预算（请求耗时与退避等待是不同维度）。

两类 provider 对单次上限的执行方式不同：

- 自己管理流式超时的（`manages_request_timeout_internally=True`，含 `ResponsesProvider`、`OpenAIChatProvider`）：外层不加硬截断（避免“流式持续出 chunk 却被总时长误杀”），provider 内部采用“streaming-first”语义——首个 chunk（任意 chunk，不要求文本 delta）与后续 idle chunk 的阈值都取单次请求上限；上游不支持流式时同一次 attempt 内自动回退非流式，非流式首响应阈值同样取该上限。
- 其余 provider：由外层 `wait_for_model_attempt` 按同一上限截断。

重试边界（`main/runtime/chat_backend.py` 与 `g3ku/providers/fallback.py` 共用同一套语义）：

- `retry_on` 关键词命中的错误（默认含网络类与 429/限流类）走**本模型退避重试轮**：一轮 = 完整轮过该模型所有 key（轮内某 key 命中可重试错误继续轮下一个 key），一轮结束仍有可重试失败才消耗一个轮预算并退避；轮间走封顶指数退避并带抖动（起点约 1s、封顶 60s），抖动用于打散并发节点的重试节奏，避免同步撞同一个限流窗口。模型轮预算（绑定 `retry_count`，0/未设置用默认 10 轮）耗尽后才跨模型前进，链尾模型预算耗尽即抛带可重试标记的耗尽错误——次数预算是唯一上限，总请求次数 = Σ(每模型轮预算 × 该模型 key 数)。**可重试错误绝不零等待直接消费下游模型**：等主模型恢复优先于降级到弱回退模型，避免限流窗口内把关键请求交给弱模型拿到劣质响应。`retry_on` 是真开关：未设置时用默认关键字，**显式置空 `[]` 则无关键字、任何错误都不触发退避重试**（详见 `config-and-models.md`）。
- 重试可见性：`ConfigChatBackend` 通过可选 `on_model_retry_status` 回调发布 live-only 状态。发射点是两类真实重试——**退避重试前**与**同模型重发（换 key）前**；跨模型 fallback 属链路正常工作，只计入次数、不单独发射。负载为 `state=retrying`、`retry_count`（= **provider 实际已发出的请求次数**，含轮换与跨模型，故恒等于即将发生的这次重试的序号，与真实请求数一致）、当前模型轮次、模型链、截断后的 `error_message`、退避秒数（同模型重发发射为 0），以及最新/下次重试的绝对时刻 `last_retry_at`/`next_retry_at`（本地带偏移）；重试成功、配置回合重建或异常退出时发布 `state=cleared`。回调异常不得打断真实 provider 重试，原始终端错误仍走既有失败链。
- 重试循环在每个退避边界对比 runtime config revision：revision 变化时**不中止回合**，而是重新解析当前角色链并按新链重启重试（丢弃旧链已试集合、刷新 revision 基线、从新链链首重新评估）；链未真正变化则只刷新基线并沿用既有重试账本。跨重启次数由 `DEFAULT_MAX_CHAIN_CHANGE_RESTARTS` 累计封顶，只兜底链反复变化的病态抖动，正常一次改链只消耗一次。模型前进边界同样通过 `model_refs_resolver` 活解析模型链：运行中新加入的 fallback 模型在下一个模型边界可见，已试过的模型不回头重试。上层回合级重建见 `config-and-models.md`「模型链变更何时作用于在途回合」。
- 换 key（轮换）判据（`should_rotate_api_key_error`）：**内部运行时错误不换、请求体形状错误不换、`retry_on` 命中（判定可重试）不换**（改走本模型退避重试轮）；其余错误（未命中且非请求形状，如 401 坏 key、503）才换 key：**每个 key 各试一次（单趟轮换，「重试次数」不参与），轮完即前进到链上下一个模型**，都不可用时抛耗尽错误。请求形状错误跳过其余 key 直接前进下一模型。形状错误只按结构化 HTTP 状态判定（`LLMResponse.error_status` / SDK 异常 `status_code`）：status 可得时只有 400/422 算形状错误（换一把 key 修不了畸形 payload），其他状态（429 限流、401 等）与 status 不可得的错误一律不按形状错误处理——错误正文里的网关 type 字段（如 `invalid_request_error`）不是 400 专属标识，文本匹配会把 429 限流误判成形状错误。配置脚枪：把 401 之类配进 `retry_on` 会让坏 key 只重试不换 key（详见 `config-and-models.md`）。
- provider 终态错误（`finish_reason="error"`）由 frontdoor normalize 抛 `ModelProviderResponseError`（继承 `RuntimeError`，兼容既有 `except RuntimeError`），携带结构化 `code`/`status`/`kind`——来自 `LLMResponse.error_code`/`error_status`/`error_kind`，由 provider 从 SDK 异常提取。`session_agent` 错误分类器据此把真实 provider code（如 `insufficient_quota`）写进 `StructuredError.code` 与转录 metadata（含 `error_status`/`error_kind`），而非退化成 `legacy_session_error`，使上层能按 code/status **程序化分支**而不必 substring 匹配文案。完整错误原文（含 `code=/status=/body=`）始终保留在 message 里并透传到用户气泡、`.g3ku/errors/*.log` 与节点 pause remark，**不脱敏**。
- 已经出现可见流式文本的发送不做透明重试/回退（一个可见气泡不得由多个 provider/model attempt 拼接）。

维护判断上要记住：

- “首 token”在本项目语义里是“首个 chunk 到达”，不是“首个文本 token”
- `request_timeout_seconds` 对上述 provider 表示“首 chunk / idle chunk 超时阈值”，不是“整次请求必须在 N 秒内完成”
- 节点因限流长时间停在模型等待（`await_marker=model.chat.await_response`）可能是退避重试在正常推进，先看日志里的 `Retryable model failure for <model_ref> (round N/预算)`，再看会话/节点重试 toast 是否处于 `retrying` 并携带 provider 错误与最新/下次重试时刻。toast 表达"正在重试（模型退避轮或同模型换 key 重发）"，其 `retry_count` 是 provider 实际请求次数；每个模型的重试受其轮数预算约束（绑定 `retry_count`，默认 10 轮），预算耗尽即前进下一模型、全链耗尽按 exhausted 冒泡，不是无限重试。重试结束或中止时状态必须清理。UI 合同见 `web-and-admin.md`「Model Retry Visibility UI Contract」

## 6. 运行时里的状态与持久化

同步会话和任务运行时有两套不同的持久化关注点：

### 会话侧

- transcript / session messages
- paused execution context
- inflight turn snapshot
- frontdoor completed continuity sidecar（`frontdoor_request_body_messages` 基线、actual-request trace、阶段/规范化/压缩状态）
- 每轮边界快照 `.g3ku/web-ceo-turn-boundaries/<session>/<turn_id>.json.gz`（与 continuity sidecar 同一份载荷按 `_active_turn_id` upsert，轮末 finalize 写入即该轮终态；gzip、每会话保留最近 3 轮）。它是用户消息编辑重发/Fork 的唯一截断数据源：截断到某轮之前 = 读取该轮 prev_turn 的边界快照整体替换 continuity 状态；快照缺失的轮次不可截断（不做启发式重建）。契约详情见 `web-and-admin.md`「Message Edit-Resend And Session Fork」
- latest message / pending interrupts

主要由 `RuntimeAgentSession` 和 `g3ku/session/manager.py` 协调。frontdoor continuity 的写盘与恢复覆盖所有 frontdoor 会话命名空间（`web:`、`china:`、`cron:`、`ext:`），渠道会话（含存量 `china:*` 归档）的基线同样跨进程重启存活；恢复顺序详见 `context-and-cache-troubleshooting.md`「Baseline 合同与恢复顺序」。

审批中断的恢复通过持久化的 `graph_state` 重建：中断时 `_pause_for_frontdoor_interrupt` 把**预览前**的完整图状态以 `{"version": 2, "state": ...}` 存入 paused execution context 的 `graph_state` 字段；恢复时 `resume_turn` 读回该快照、校验版本后从 `review_tool_calls` 节点以 resume 决定重入（等价于在中断点重放）。存「预览前」而非「预览后」状态的原因：阶段预览对 `_record_frontdoor_stage_round` 非幂等——用预览后状态重建会二次记账（round 重复、阶段预算提前耗尽）。无标记 / 缺失 / 损坏的快照显式报错 `frontdoor_interrupt_resume_snapshot_unavailable` 且不清除 paused 快照，不回退成新 turn；跨进程重启后的恢复依赖该落盘快照。

### 任务侧

- task / node 元数据
- 执行日志和运行帧
- artifacts
- 事件历史
- 治理状态
- 任务临时目录 `temp/tasks/task_<id>`（每个任务的 scratch 空间，路径在任务创建时写入任务 runtime meta；终态后默认保留，清理契约见「磁盘写保护与治理」）

任务临时目录的根由 `MainRuntimeService._workspace_root()` 派生，解析链为：构造参数 `workspace_root` > `resource_manager.workspace` > 进程 cwd。它既决定 `temp/tasks/` 的位置，也影响 `exec`/`filesystem` 类工具默认的 `task_temp_dir` 工作目录。需要掌握的两条维护约束：

- 任何不提供 workspace 的调用方（尤其是测试和一次性脚本）都会把任务目录落在进程 cwd 的 `temp/tasks/` 下。测试通过构造参数 `workspace_root=tmp_path` 显式隔离，`tests/resources/conftest.py` 的 autouse fixture 再把 cwd 回退替换为 per-test 临时目录作为兜底，双层保证测试运行不会向真实仓库写入 `task_*` 目录。
- 测试任务的记录只存在于 pytest 的临时数据库里，其遗留目录不会被任何生产清理路径回收；这类孤儿目录用 `scripts/cleanup_orphan_task_temp_dirs.py` 处理，见 `operations-and-maintenance.md`「关键状态文件与目录」。

CEO/frontdoor 会话没有 `task_id`，其工具 runtime 注入的 `task_temp_dir` 解析为会话级目录 `temp/ceo/<safe_session_key>`（`session_key` 里的 `:` 等不安全字符按 `sessions/` 落盘同一口径规范化，如 `web:ceo-xxxx` → `web_ceo-xxxx`；无会话键时回退 `temp/ceo/shared`）。它与任务级 `temp/tasks/` 共用同一套下游约束：`exec` 未显式传 `working_dir` 时以它作默认 cwd，`exec`/`filesystem` 的路径策略以它作临时内容规范落点，目录惰性创建（exec 用作 cwd 或 filesystem 写入时才 mkdir）。该目录同时以 `session_temp_dir:` 行暴露进 frontdoor 运行时工具合同，让模型知道临时文件的绝对落点，避免经 `exec` 重定向散落到工作区根目录；合同渲染与临时文件落盘规则详见 `tool-and-skill-system.md`「四个概念必须分清」。该目录不保证持久保留：正式交付物禁止以此为最终落点，落点约束见 `tool-and-skill-system.md`「四个概念必须分清」与 frontdoor 提示词契约。

主要由以下模块协同：

- `main/storage/sqlite_store.py`
- `main/monitoring/log_service.py`
- `main/monitoring/query_service_v2.py`
- `main/storage/artifact_store.py`
- `main/governance/`

关于节点运行帧还要额外记住一个维护语义：

- `task_runtime_messages` / `runtime-frame-messages:{node_id}` artifact 除当前 messages 列表外，还在同一个 artifact 里累计 `callable_tool_snapshots`、节点输入层的 `contract_visible_skill_ids`（`runtime_service._node_context_selection_inputs()` 当轮快照）与 `skill_visibility_diagnostics`（解释这些可见 skill 如何从 live registry / role / policy 收敛出来）。每条快照代表一次 `before_model` 轮次下本地运行时真正记录的 callable/candidate 截面：`callable_tool_names`、`candidate_tool_names`、`candidate_tool_items`、`candidate_skill_ids`、`candidate_skill_items`、`model_visible_tool_names`、`hydrated_executor_names` 和选择 trace。排查“这一轮模型 load 过工具却没法调用”时，先看最近一条快照再看 transcript / stage trace。
- 写帧是这份正文唯一的销毁通道，因此 `log_service._runtime_frame_record` 守住一条保留规则：**只有携带 messages 正文的写才允许换 `messages_ref`**。空 messages 的写（典型来源是读-改-写回路：`read_runtime_state` 把帧正文按 `messages_ref` 解出来、调用方改几个字段、再 `replace_runtime_frames` / `update_frame` 写回，而解析恰好失败）必须原样保留既有 `messages_ref` / `messages_count` 并落 WARN，绝不把指针换成一份空正文。`_hydrate_runtime_frame_record` 与 `_sanitize_runtime_frame`（按枚举键清洗，漏键等于删键）都要把指针带在帧字典上，这条规则才在各写入点成立。指针被写没会伪装成“模型变笨”：`NodeRunner._resume_react_state` 拿不到帧正文就退回 `fresh` 重建，scaffold 头探针随之报 `fallback_seed_*`，表现为节点上下文突然只剩几条消息（本条是该写入规则的所在地；取证读法见 `context-and-cache-troubleshooting.md`「帧的 messages 指针被读-改-写回路写没」）。
- 对 execution / acceptance 节点，无有效阶段的快照里 `callable_tool_names` 与 `model_visible_tool_names` 保留全量并把 `submit_next_stage` 置首（`stage_locked_to_submit_next_stage` 标记恒为 False）；候选集合留在 `candidate_tool_names`，完整 callable pool 留在选择 trace（`model_visible_tool_selection_trace.full_callable_tool_names`）。canonical name/id 列表为空时，同快照的 `candidate_tool_items` / `candidate_skill_items` 也必须同步清空。
- 执行节点与检验节点是“两层消息结构”：稳定 bootstrap user JSON 只负责任务定义与稳定节点上下文（不含 `execution_stage`）；单独的动态 `node_runtime_tool_contract` 摘要消息以 system 角色落地（运行时元数据、非对话内容，避免模型把它错当成“自己上一轮说过/发给用户的消息”），负责当前轮的 callable/candidate tool/skill 合同，追加在当前 request 尾部、当轮 turn-only 阶段提示之前——请求末位保持 user 回合提示，契约块不占据末位（末位的契约会诱导模型把它整段回显进下一条回复）。standalone 契约回显（零工具调用、整条回复是契约原文）由回显守卫收口成可恢复的 error pause，见本文「Node-Level Pause and Recovery」电路熔断清单。节点的 turn / repair overlay 同样遵守 append-only 边界：只能作为新的 request-tail 消息追加，不得回写已有 bootstrap 或持久化历史消息。
- 对节点运行时，`before_model` 当轮真正下发给模型的 schema 选择结果是权威工具来源；runtime frame、restore/recovery 和 runtime messages artifact 都从这份结果派生。`node_runtime_tool_contract` 是模型可见合同，但 runtime frame 才是 `candidate_skill_ids` / `candidate_skill_items` 的 canonical 恢复来源。
- 排查“节点为什么说没有 candidate skills”时，同时看 `contract_visible_skill_ids`（输入层可见性）与 `candidate_skill_ids`（selector 最终候选）；输入层为空时继续看 `skill_visibility_diagnostics`（registry 存在性 / role / policy effect）。首轮 `candidate_skill_ids=[]` 而 fresh contract 本应非空时，优先判断是否仍停留在 `initialize_task()` 的 bootstrap 空 frame。
- `task_node_detail` 的 summary 档执行轨迹摘要在 `stages` 之外还挂 `latest_tool_calls_full`：轨迹最后 5 步工具调用的完整入参（落盘 `arguments_text`）、状态（含运行中）与已结束调用的完整出参（按 `output_ref` 解析、单条超 8000 字符截断并置 `output_truncated`，`output_ref` 保留供按需再取）。它服务于卡点排查（agent-facing 工具 payload），与 Web 时间线渲染无关；round 级工具记录按既有存储契约落盘（小输出内联 `output_text`、大输出外置 `output_ref` + `output_preview_text`），该字段只做只读读取与按需解析。
- CEO/frontdoor 采用同样的分层思想：稳定会话前缀不承担当前轮 callable/candidate tool 状态，当前轮工具合同放在 dynamic appendix 并随 turn state 刷新，overlay 保持 append-only。prompt cache key 未变但命中下跌时，先检查是否有 overlay 被拼回已有 user 消息。
- 主运行时阶段账本同样携带展示文本：`record_execution_stage_round` / `record_execution_stage_free_pass_round` 把该轮工具调用同批响应里的模型叙述记入 round `text`（按 `_STAGE_ROUND_TEXT_CHAR_LIMIT` 截断），`submit_next_stage` 把 `completed_stage_summary` 记入完成的阶段。两者经 `main/monitoring/execution_trace.py` 与两个 summary 装配器（`log_service` / `query_service`）进入 Web 节点详情 payload（full 与 summary 两级都保留），与 frontdoor 展示字段同属只服务 Web 时间线的展示数据（frontdoor 侧规则见本文「CEO Frontdoor Round Tool Ownership」），不得喂给 prompt 组装或转录权威链；渲染合同见 `web-and-admin.md`「CEO Stage Trace Round Rendering Contract」。

### 磁盘写保护与治理（main/ 侧持久化契约）

main/ 侧所有持久化写在磁盘满（ENOSPC / SQLITE_FULL）条件下的行为由 `main/storage/disk_guard.py` 统一约束。该模块拥有 `DiskFullError`（`OSError` 子类，既有 `except OSError` 语义兼容）、`is_disk_full_error` / `classify_write_error` 分类器，以及 `DiskPolicies` 进程级策略单例——由 `runtime_service` 构造时从 `config.main_runtime.disk_guard` 注入（字段契约见 `config-and-models.md`「main_runtime」），`G3KU_*` 环境变量仅作测试与应急覆盖。

契约按写入类别分层：

- **写咽喉只分类不吞错**：全部 sqlite 写经 `SQLiteTaskStore._run_write`；磁盘满异常统一分类为 `DiskFullError` 后照常上抛，是否降级由调用点决定。writer 线程按类别累计失败计数，经 `write_failure_counts()` / `runtime_metrics_snapshot()` 暴露（`write_failure_disk_full` / `write_failure_other`），是判断"系统是否经历磁盘满"的第一指标。计数不只在本地：心跳线程把 `sqlite_write_failures` / `event_write_failures` 一并写入 `worker_leases` / `worker_status` 的 debug 块，事件写失败（`TaskLogService.append_task_event` 与 live.patch 快照冲刷路径）另有 300s 限流 WARNING `task_events write failure (rate-limited): total=…`——静默降级可观测但告警不刷屏。排障顺序见 `operations-and-maintenance.md`「磁盘满」。
- **关键写永远尝试**：任务/节点状态、pause 行、error_log 不做预检、失败靠调用点兜底。
- **可降级写先过应急写预算**：actual-request artifact、`task.live.patch` 单份快照、execution trace 外置在写前调用 `has_emergency_disk_budget`（剩余空间 < max(`emergency_min_bytes`, 盘总量 × `emergency_min_ratio`) 即跳过落盘，退回 slim/minimal 形态）。预检带 5s TTL 缓存；探测失败保守放行。live.patch 快照被跳过时不写文件，下一个补丁覆盖写自然补齐（覆盖写自愈）。
- **error pause 记录是 best-effort**：`NodeRunner` 异常路径的 error_log 与 pause 两个写点各自独立 try/except（`_persist_error_and_pause_best_effort`），任一失败都不阻断 `NodePausedError` 传播——控制流不依赖 pause 落盘，磁盘满只降级可见性、绝不放大为连锁节点暂停；未落盘的错误文本进入有界内存队列（deque maxlen=64）并留 warning 日志。`TaskActorService` 的 `NodePausedError` / `TaskPausedError` 分支里的二次 pause 状态写同样包死。
- **live patch 单份快照覆盖写**：`task.live.patch` 不落 `task_events` 行——经窗口聚合（`live_patch_persist_window_ms`，终态/暂停立即冲刷）后覆盖写 `event-history/<safe_task_id>/latest.json.gz`（tmp+replace 原子写，读者永远看到完整快照；`SQLiteTaskStore.write_task_live_snapshot`）。写失败不重试不重入队，等下一个补丁覆盖即自愈；磁盘记账按新旧文件字节差修正。每任务只保留这一份最新最完整快照，SSE 实时推送走内存 pub/sub 与落盘无关。
- **task_events 只记低噪审计**：`task_events` 表只接收低频生命周期/会话级事件——`task.terminal`、`task.intermediates.cleaned`、`runtime.disk_emergency`、`runtime.task_wiped`、`task.artifact.applied`。高频事件（`task.model.call` / `task.node.patch` / `task.summary.patch` / `task.artifact.added`）不落该表：各自权威持久化在 `task_model_calls` / `nodes`·`task_nodes` / `tasks` / `artifacts` 表，事件行全库无生产读者（`list_task_events` 无调用方；WS 断线重连走详情+整树快照重拉，不回放事件行）。实时推送仍走内存 `_dispatch_live_event_locked`，不受落库策略影响。新增事件写入前先确认它的读者——只写不读的簿记会以周为单位重新撑大库。
- **artifact 大小治理**：`TaskArtifactRecord` 携带 `size_bytes` / `content_encoding`（`plain`|`gzip`）/ `content_hash`（均带默认值，payload_json 序列化零迁移）。内容超过 `artifact_gzip_threshold_bytes`（默认 1 MiB）即在同目录以 `.gz` 后缀 gzip+tmp+原子改名落盘（与 live.patch 单份快照同范式）。actual-request（`task_actual_request`）每 (task, node) 只保留最新一份——新快照创建后即删同节点旧快照（文件+DB 行+内存索引），历史请求序列不留存（排障只看最新一轮）。文本去重走 `content_hash` 快路径不回读文件；无 hash 的旧行回读解压兜底比对。**读取一律走 `artifact_store.read_artifact_text`**（rest 的 artifact full 读取、navigation 的 canonical 与 `_resolve` 分支、`apply_patch_artifact` 四个接入点）——绕过它直接 `read_text` 会把 gzip artifact 显示成 "[二进制文件]" 或乱码。artifact 写失败抛分类后的 `DiskFullError`，绝不返回指向不存在文件的记录（读端 `.exists()` 兜底会把这种记录伪装成空内容）。
- **终态即清确定不再使用的数据，任务临时目录默认保留**：任务迁移到 `success`/`failed` 时，终态监听器 `_cleanup_terminal_task_intermediates` 同步只做判定与 in-flight 去重，重活甩 daemon 后台线程：按保留清单删中间 artifact 的文件与 DB 行，并整目录删除 `event-history/<task>/`（live.patch 单份快照只服务 SSE 与任务树恢复，终态后无运行时读者；任务树恢复帧 `task_runtime_frames` 在终态转换时已清零）。**`temp/tasks/<id>` 草稿目录（含空壳）终态后默认原样保留**——仅当 `main_runtime.disk_guard.terminal_temp_dir_cleanup_enabled`（环境变量 `G3KU_TERMINAL_TEMP_DIR_CLEANUP_ENABLED=1`）开启时才随终态清理硬删。保留清单（唯一权威）：`kind=='patch'`、`kind=='final_output'`、`task.final_output_ref` 指向的 artifact、标题含 `report`/`summary`；其余（`task_actual_request` / `task_runtime_messages` / `task_execution_trace` / `node_output` / `tool_result*` 等）全部删除。`task_error_logs` 表、节点 `blocking_reason`、`task_events` 行一律不动；孤儿 event-history 目录由删除台账 sweep 清扫。磁盘治理没有任何自动删除任务的路径——任务只随用户手动删除（Web/REST）或模型删除工具彻底清除（同走 `delete_task` 全删链路，先导出产出）：任务临时目录按双路径兜底回收（runtime_meta 记录的实际路径优先，确定性默认路径兜底，与终态开关无关），硬删统一走 `fs_utils.remove_tree`——失败条目去只读位重试、Windows 下经扩展长度前缀删除超过 MAX_PATH 的深树（git 克隆的只读文件与超长路径不会造成静默残留），仍有残留时 loguru 告警，绝不静默。清理量记 loguru 日志并发 best-effort `task.intermediates.cleaned` 事件；读端对已删 artifact 由 `.exists()` / `read_artifact_text` 兜底，不炸。维护者常见误读：把 `temp/tasks/` 当"任务结束即回收"的 scratch 空间——它是保留目录，正式产出落入其中不会因终态清理丢失，但也因此不参与自动回收，需靠 `scripts/cleanup_orphan_task_temp_dirs.py` 或显式开关治理；`_effective_task_temp_dir` 把等于 temp 根目录的 meta 兜底值视为未配置（真实任务创建时写入的一定是每任务子目录），防止对账/删除作用到整个 temp 根。

水位监控与紧急态（P1）在同一契约下运转：

- **采样**：`WorkerPressureMonitor` 每拍（1s）经 disk_guard 的 TTL 缓存读工作区/存储盘的 `(free, total)`，随 snapshot 以 `machine_disk_free_bytes / machine_disk_usage_percent / disk_emergency_active` 下发到 `worker_status_payload`；前端任务大厅性能条的「CPU/内存/磁盘」项把剩余空间并进磁盘段渲染（`0%(剩余10.1G)`，紧急=critical 着色），不单列「磁盘剩余」项；紧急态另渲染全局横幅。
- **唯一水位线是紧急线**：`max(emergency_min_bytes, total×emergency_min_ratio)`，判定带防抖（进入需连续 `emergency_streak_samples` 拍、解除需连续 `emergency_recovery_samples` 拍）。磁盘治理不含任何触发删除的水位线——磁盘紧张只暂停与限流，任务删除永远是手动动作（大小可视化与排序引导见 `web-and-admin.md`「任务大厅大小与排序」）。
- **紧急态硬闸的语义是"排队等待"而非拒绝**：controller 的 `set_disk_emergency(True)` 把 `target_limit` 置 0，新工具调用在预算队列等待（模型不会收到工具级错误）；`disk_emergency` 是独立于 `pressure_state` 的布尔硬闸——压力决策链（critical/throttle/ease）与 dwell/starvation 逃逸阀在紧急态整体冻结，`_reset_idle_locked` 三处调用点带守卫，任何路径都不得把 limit 从 0 抬起。
- **紧急态自动暂停防死锁**：进入紧急态的边沿钩子（monitor 采样线程 → `call_soon_threadsafe` 回事件循环）对全部 `in_progress` 任务执行 `force_pause_task_durably`，随后 `controller.abort_task_waiters(task_id, TaskPausedError)` 唤醒该任务排队中的 acquire future——异常沿既有 pause 流转冒泡（acquire 在 `_run_call` 的工具 try 块之外，不会被误包装成工具级错误）。竞态封口：acquire 成功返回后补一次 `_check_pause_or_cancel`（失败归还槽）；`_check_pause_or_cancel` 的 pause 分支与 `pause_task` 成功路径同样调用 abort。web/worker 双进程各自检测、各自 pause，`force_pause_task_durably` 幂等，DB 是唯一真源。
- **解除紧急态不自动恢复任务**：空间回到紧急线之上只清硬闸与告警，被暂停的任务保持 `paused` 等手动 resume（避免水位反复抖动造成任务震荡）。
- **任务大小增量记账**：`task_disk_usage(task_id, total_bytes, updated_at)` 小表，口径 = 目录文件（files/artifacts/event-history/temp）+ 数据库明细字节（五张大行表 `SUM(LENGTH(payload_json))`，`sum_task_detail_bytes`）。目录写入在写入点 bump 字节数（artifact 落盘、live.patch 单份快照与 singleton 覆盖写按新旧 size 差值记账），DB 字节只在对账时整体刷新。对账 loop（embedded/worker 模式，每小时）只对 `in_progress` 任务跑目录实测 + DB 明细求和覆盖增量值，终态任务在终态清理时对账一次——已终态任务目录不再变化，不进小时级扫描（禁止高频全量遍历）。DB 字节因此存在 ≤1h 展示延迟（列表契约同）。列表端 `TaskListItem.disk_usage_bytes` 从该表批量读取。
- **任务删除全量清除**：用户 `delete_task`（Web/REST）与模型删除工具共用 `_wipe_task_data` 核心，步骤顺序是契约——S0 路径快照（`_effective_task_temp_dir` 读 runtime_meta，必须在删 DB 前取值）→ S1 产出导出 → S2 写删除台账 → S3 删文件（artifacts / files / event-history / temp 双路径，逐项容错）→ S4 删 DB 行（全部任务作用域表，含 `task_disk_usage` 与 `heartbeat_node_retry_state`）→ S5 governance 审批行（`exec_command_approvals` 按 `context_id=task_id` 删，含命令明文）→ S6 台账 wiped 标记 + `task.deleted` 推送 + 会话级 `runtime.task_wiped` 审计事件（task_id=None）→ S7 进程内缓存清理（summary 字典、inflight 集合、log_service `discard_task_caches`、registry 订阅）。任务数据不存在无条件永久保留的类别。
- **无自动任务删除**：磁盘紧张不触发任何任务删除；任务全删仅两条入口——用户手动删除与模型删除工具（`task_delete_cn`，preview→confirm 双步），均走 `_wipe_task_data` 且删除前导出产出。磁盘紧张时的空间回收依靠：终态即清确定不再使用的数据（上一条）、`detail_retention_days` 可选明细裁剪（下一条）、任务大小可视化与排序引导的手动删除（`web-and-admin.md`「任务大厅大小与排序」）。
- **产出导出（deliverables）**：删除前按终态保留清单判据（与终态清理同一权威）把命中 artifact 复制到 `.g3ku/main-runtime/deliverables/<safe_task_id>/`（文本经 `read_artifact_text` 解压后明文落盘，二进制原样字节，附 manifest.json 记录 kind/title/state）。该目录永久保留、不参与磁盘治理；导出失败仅告警不阻断删除；无命中不落盘目录。
- **删除台账（task_delete_ledger）**：先记账后删除。守卫三个迟到写复活点——`append_task_event`（同事务点查，命中直接返回 0）、`put_task_summary_outbox`、`write_task_live_snapshot`；`wiped=0` 的中断残留由小时级 sweep 幂等补偿（重放 S3-S5）；台账行满 7 天（`_LEDGER_RETENTION_DAYS` 模块常量）经最终补偿后删除，台账自身不构成永久保留。sweep（`claim_maintenance_run('delete_ledger_sweep', 23h)` 卡权 + 紧急态跳过）顺带清扫遗留 `metadata.purged_at` 墓碑任务（直接全删）与孤儿 event-history 目录（无 tasks 行 + 7 天 mtime 宽限）。
- **终态大行裁剪（P3，默认停用）**：`task_model_calls / task_runtime_frames / task_node_tool_results / task_node_rounds / task_node_details` 五张大行表按任务口径裁剪（终态且 `finished_at`（缺省 `updated_at`）早于 `detail_retention_days`）；默认 `0`=停用——任务明细与任务同生命周期，只随手动删除清除，配置 `>0` 恢复按天裁剪。启用时每批 200 任务单事务 + `wal_checkpoint(PASSIVE)`，宿主并入每小时对账 loop（23h 间隔经 `maintenance_runs` 表跨进程卡权，key `detail_prune`；紧急水位跳过——裁剪自身是写放大源）。error_logs、tasks/nodes 结构、task_events 行保留至任务删除（wipe 全删）。
- **sqlite 空间回收（P3）**：新库建库即 `PRAGMA auto_vacuum=INCREMENTAL`（必须在任何事务写入前设置，`sqlite_store.__init__` 里位于 WAL pragma 之前）。存量库（auto_vacuum=0）迁移与全量 VACUUM 走 `scripts/compact_task_database.py`（默认 dry-run、`--apply/--vacuum-full/--backup/--retention-days`；VACUUM 前检查剩余空间 ≥1.2× 库大小，不足拒绝）。约定在服务停机或排水后运行；运行时进程内不做 VACUUM（多进程 WAL 模型下需要独占，脚本/端点隔离最干净）。

维护者常见误读：把 `DiskFullError` 当新异常类型去 catch——它继承 `OSError`，既有 `except OSError` 分支自动覆盖；把终态清理当"数据丢失"——被删的只是确定不再使用的中间产物与恢复快照，保留清单与 error_logs / task_events 行保证可回顾性（产出在删除任务时自动导出 deliverables）；把磁盘紧张当"会自动删任务"——磁盘治理没有任何自动任务删除路径，跌破紧急线只做自动暂停与可降级写跳过，任务全删仅用户手动或模型删除工具两条入口；在磁盘满排障时只看 `.g3ku/errors/`——磁盘满期间错误日志本身可能是 0 字节空文件，权威信号是 `write_failure_counts` 与 worker 日志里的 SQLITE_FULL 行；把紧急态下"工具不动了"当卡死——那是 target_limit=0 的排队等待，任务随即被自动暂停，恢复磁盘空间后手动 resume。

## Node-Level Pause and Recovery

`main/runtime/` treats node pause as a node-local control state layered on `NodeRecord.status`. The pause reasons are `manual`, `agent`, and `error`; a paused node remains `status=in_progress` until it resumes or is explicitly failed.

- An `in_progress`-status paused node must stay distinguishable from a genuinely-running one. `is_paused` and `pause_reason` propagate onto the node's tree projection (`TaskProjectionNodeRecord` top-level fields, mirrored inside its `payload` dict), and `task_progress` renders a paused node as `(<node_id>,paused(<reason>),<goal>)` rather than `(<node_id>,in_progress,<goal>)`. The text label reads the projection's top-level flag and falls back to `payload`, so a paused node shows as `paused` even when the projection predates a fresh re-project. Callers that cross-check a `task_node_error` heartbeat against `task_progress` then see one consistent signal instead of a heartbeat "paused" against a tool-reported "in_progress".
- `task_progress` renders status truthfully: `in_progress` only means non-terminal, never "executing right now". Activity evidence comes from `task_runtime_frames`: a node line carries a fourth element only when evidence exists — `运行中` is granted solely to a fresh `active` frame (updated within 10 minutes); a stale `active` frame degrades to `疑似中断(X分钟未更新)`; `runnable`/`waiting`/flagless frames render as `排队待运行`/`等待中`/`无活跃调度`. Acceptance nodes have no `检验中` fallback: `检验中` appears only with a fresh active frame, an undispatched acceptance node renders `待检验` (or its recorded `check_result`), and a task-level final-acceptance state (`waiting_acceptance` / `waiting_execution_retry` / `waiting_block_verification` / `running` / `passed` / `failed`) surfaces as a `验收: <label>` line. For `in_progress` tasks the text header also carries `最近活动: <age>` and `调度: execution(运行X/排队Y) inspection(运行X/排队Y)` so a caller can judge liveness without trusting the status word alone, and leads with a `任务当前正在等待节点输出: (<ids>)` line that lists the nodes owning fresh active frames — or, when no frame is fresh (the suspected-stall case), the single most recently updated frame node, so a stuck task can be localized without walking the tree. The wait-line candidates are read-only derived from `task_runtime_frames` (never written back), and the line is omitted when no candidate exists. The judging contract is also stated in `tools/task_progress_cn/resource.yaml` and the CEO frontdoor prompt.
- `pause_node` targets the selected node by default. A parent with running descendants can be paused independently; the request takes effect when that parent reaches its next React-loop safe boundary. With `cascade=true`, the parent and every descendant, including inspection nodes, receive the pause request. Without cascade, descendants continue independently.
- A runtime exception in `NodeRunner` is recorded in `task_error_logs`, registered in `task_node_pauses` with `pause_reason=error`, and surfaced as `NodePausedError`. Both persistence writes are best-effort: a failure to record the error log or the pause row never blocks `NodePausedError` propagation, so disk-full conditions degrade visibility instead of cascading node pauses (contract: 本文「磁盘写保护与治理」). Cancellation and task-terminal checks have priority, so an intentional cancellation resolves as `failed/canceled` rather than becoming an error pause. A `CancelledError` that arrives without `cancel_requested` / pause flags is an engine-level interruption (worker process exit teardown, stray in-process cancellation), not an operator action: `NodeRunner.run_node` flushes the latest valid result and propagates the cancellation without any terminal write, the node stays `in_progress`, and `TaskActorService.run_task` returns control-only (never `failed/canceled`) plus a best-effort requeue — startup recovery owns the task from there (contract: 本文「Graceful Shutdown Pause and Startup Auto-Resume」). The task's terminal `canceled` state is written exclusively by the `cancel_task` service path (which sets `cancel_requested` before cancelling the executor) — a terminal `canceled` task without `cancel_requested` in its payload is forensic evidence that an engine interruption terminalized the task (the recovery path never runs for terminal tasks), and the operator remedy is recreating the task, not resuming it.
- ReAct protocol-repair circuit breakers (invalid final/stage submissions, repeated read-only calls, stage-only loops, XML pseudo-tool repair exhaustion, orphan tool-result detection, and standalone runtime-tool-contract echo) return an internal `failure_disposition=pause`. A standalone contract echo is repaired exactly once by re-prompting the model with a private repair note; a repeated echo is converted into this error pause instead of being auto-wrapped into a `success + final` node result. `NodeRunner` keeps that field out of the persisted result payload, records the guard reason in `task_error_logs`, and registers an `error` pause while the node stays `in_progress`; normal `submit_final_result(status="failed", ...)` business outcomes remain terminal failures. Explicit pause, cancellation, and task-terminal state take precedence over this conversion. Orphan tool-result detection additionally warns each strike into the worker log with the task, node, and orphan call ids, so this pause class is diagnosable from the worker log without DB forensics.
- A `waiting_children` frame with an incomplete `spawn_operations` round is a durable child-pipeline commitment. Recovery replays the original `spawn_child_nodes` call id and cached child specs before generic pending-tool recovery or another model request, reusing existing child bindings, dispatcher futures, and partial results. The governing invariant is that a spawn entry is not complete while its bound execution node or acceptance node is non-terminal, regardless of the entry's review status or synthetic result: recovery never re-reviews a round that already owns child/acceptance bindings, and a `review_decision=blocked` synthetic result cannot overwrite or mask a live pipeline status. Frame projection derives each pipeline's displayed status from this effective state, and `_run_child_pipeline` never short-circuits to a synthetic terminal result while a bound node is still running. `task_node_rounds` 的轮次计数同源推导：每个物化 entry 先解析绑定节点——终态节点双向覆盖 entry 记账，非终态绑定节点把陈旧 `error` 记账降为 running——绑定不可解析才回退 `entry.status`，投影不会对仍存活的节点报 `failed_children`。 The parent remains waiting while any original child is running, waiting, or paused; resuming the parent does not resume a previously paused child and cannot create a replacement round that supersedes the original subtree.
- `task_node_pauses` has one active row per node. It stores the reason, operator or agent remark, and heartbeat delivery marker. Terminal node cleanup removes the row; task deletion removes pause rows and error logs with the task.
- A task's terminal status is derived from the root node plus final acceptance alone, so a node still `in_progress` when the task turns terminal is orphaned — its result can no longer flow back. The terminal transition does not itself mutate those residual nodes: nodes that are still driven afterwards are closed by `NodeRunner.run_node`'s task-terminal short-circuit or `_run_child_pipeline`'s abort, which carry the task's terminal reason. The startup-time sweep that closes the genuinely-undriven leftovers lives in `operations-and-maintenance.md`「残留节点自愈」.
- An `in_progress` task can carry orphans of its own — spawn-tree children still non-terminal in the store while their dispatcher entries are gone (a `run_task` exit closed the dispatcher) and no parent pipeline awaits them. Every `run_task` pickup reconciles them before dispatching the root (`_reconcile_orphan_in_progress_nodes`; skipped entirely while any distribution hold state is active, since the barrier driver owns node lifecycle then). Candidates are execution nodes stamped `spawn_owner_kind='child'` with a non-empty `spawn_owner_round_id` (acceptance nodes are never reaped here — their ownership is the handshake, which the two `run_task` resume selectors read, see the Acceptance paragraph above; unbound execution shapes are left to the stall monitor). A candidate is re-dispatched via `dispatcher.resume_node` only when its whole upward chain is replay-intact — each parent non-terminal, its latest incomplete `spawn_operations` round still binding the node (child or acceptance id, entry status ignored), and the parent frame keeping replay intent for that round (`waiting_children` phase, or the round id in `pending_tool_calls`/`active_round_tool_call_ids`) — up to the root or a live entry; otherwise it is reaped with `fail_paused_node(reason='orphan reaped at task resume: …')`, a WARN, and a `task_error_logs` row. The invariant: a non-terminal store status always corresponds to "running / about to be dispatched" or "recoverable by parent replay" — a ghost `in_progress` with no executor is impossible beyond the next pickup.
- A child pause leaves its dispatcher future pending so the parent pipeline waits without treating the child as a failed result. The root pause propagates to `TaskActorService`, which leaves the task runnable state paused. Failing a paused child resolves the original future with a failed result so the parent can apply its ordinary failure handling.
- 任务级暂停标志（`TaskRecord.pause_requested` / `is_paused`，任务大厅 Paused 徽章的唯一依据，渲染合同见 `web-and-admin.md`）与**任务根节点**的暂停态按根节点驱动口径恒等：根节点处于暂停（含级联覆盖根）⇒ 任务标志置位；根节点恢复或转终态 ⇒ 任务标志清除。同步在 `log_service` 收口且双向幂等——`set_node_pause_state` 改到根节点时回填任务标志，`update_task_control` 写暂停标志时反向对齐根节点（任务侧暂停只置根节点 `pause_requested`、不把仍在运行的节点谎标成 `is_paused`；任务侧恢复才彻底清除两者）。反向那一半是必需的：只做节点→任务方向时，全局恢复清掉任务标志后会被回填路径立刻重新置为 Paused，表现为「恢复无效」。
- 「任务 Paused」由根节点驱动，不是「任意节点暂停」：只暂停子树、根仍在跑时任务标志保持不变；根节点转终态时任务标志一并清除，否则大厅先看 `is_paused` 会把已成功/失败的任务显示成 Paused。
- 任务级暂停的运行时语义（任务标志、调度排队取消、排队等待唤醒、分发失败态复位）由 `pause_task` / `resume_task` 实现；覆盖任务根节点的节点控制动作复用同一实现，使「根节点 + `cascade=true`」与整任务形态在状态和行为两个层面都与一次任务级暂停/恢复等价（工具侧合同见 `tool-and-skill-system.md`「`manage_task_nodes` 节点控制工具」）。
- A subtree-wide fail (`manage_task_nodes` cascade `fail`, contract in `tool-and-skill-system.md`「`manage_task_nodes` 节点控制工具」) applies root-first, descendants in BFS order after. `dispatcher.fail_node` resolves the entry future without cancelling running coroutines, so failing a descendant first would wake its still-`in_progress` parent into the child-failure branch; a root woken that way hits its pause boundary, resolves its own future with `NodePausedError`, and the batch's later `set_result` on the root is skipped as done — leaving the task parked as paused while the store already marks the root failed. With the root failed first, coroutines woken by descendant fails are already terminal and residual writes hit the terminal short-circuit. The gate that makes this safe: a cascade fail is rejected unless every non-terminal descendant is already paused, so the flow is cascade pause then cascade fail.
- Resume clears the node pause state and restarts the same dispatcher entry. `NodeRunner._resume_react_state` restores the persisted `task_runtime_frames` frame, allowing the node to continue from its prior runtime context instead of starting a fresh node turn. Distribution barriers treat paused nodes as part of the in-progress frontier and do not wait on a paused child as if it were an unresolved safe-boundary drain.
- In web mode, node-control actions dispatch to the task worker as one `task_commands` entry per action: `pause`→`pause_node`, `resume`→`resume_node`, `fail`→`fail_node`. `keep_paused` is leader-local only — it re-registers the pause row and remark and enqueues no worker command (the worker has no `keep_paused` command type, and an enqueued command with that action would otherwise be applied as a fail). A `fail_node` command carries the operator/agent remark in its payload, and the worker falls back to that remark as the failure reason, so a genuinely failed node records the actual diagnosis instead of the generic default text. The scoped targets/cascade path of `control_nodes` enqueues one command per target entry carrying the leader-expanded explicit `node_ids` with `cascade=false`, so the worker never re-expands subtrees (two expansions at different times could drift); worker replay is idempotent because `fail_paused_node` returns the recorded result for terminal nodes and re-pausing an `agent`-paused node rewrites the same state.

## Graceful Shutdown Pause and Startup Auto-Resume

进程以任何形式被关闭（重跑启动脚本、Ctrl+C / SIGTERM、`POST /api/bootstrap/exit`）时，web runtime 在收尾前先“暂停一切正在运行的工作”；下一次启动时这些工作自动恢复，且不产生「本任务遇到异常停止」提示。这是进程级生命周期合同，独立于上面的节点级暂停。

- 会话：对每个正在运行的会话执行 manual pause（等价于 UI 暂停按钮）：本轮上下文按暂停语义归档（`_transcript_state=paused` 的 user 行、暂停 archive 气泡、completed continuity sidecar），会话收为 `completed` + `user_pause`。
- 任务：web 模式走 `force_pause_task_durably`（worker 离线/starting 也生效：先直接落 `pause_requested=true / is_paused=true`，再下发 `pause_task` 命令让活着的 actor 在下一个安全边界停）；embedded / worker 模式走普通 `pause_task`（request_pause + scheduler cancel 等 actor 停）。
- **真实暂停保证（排水等待）**：落盘标志只是账本，不代表 actor 已停。shutdown 路径在把全部 `pause_task` 命令入队后轮询 `task_commands`（上限约 10 秒），直到这批命令全部 `completed`——命令 finished 意味着 worker 侧 `cancel_task` 已 await 完 actor 收尾（CancelledError → 持久化暂停 → dispatcher close）。wait 期间 worker 状态为 `offline/stopped` 时提前放弃（没有活的消费方，落盘标志 + 台账已保证重启正确）；超时只告警、不阻断退出。同一保证下才轮到关闭托管 worker。会话侧 manual pause 本身 await 了已注册 turn 任务的收尾（`cancel_session_tasks` gather），返回即该会话的模型/工具执行已停止。
- 语义边界：普通 UI 手动暂停仍是“落盘标志先行、安全边界后生效”的异步设计——UI 立即显示 paused 不等同于 actor 已停，两者之间有秒级的命令处理窗口；只有 shutdown 排水等待提供“全部真实暂停后才退出”的保证。
- 台账：每条被暂停的工作写一行 `shutdown_pause_registry`（主运行时 SQLite 表，key 形如 `task:<task_id>` / `session:<session_key>`，会话行带 channel/chat_id）。**用户/agent 手动暂停的工作不写台账**——只有 shutdown 路径亲自暂停的才写。
- 每次 shutdown 都会尝试一遍（信号、atexit、`/bootstrap/exit`、lifespan 收尾都会经过 `shutdown_web_runtime`）；已经暂停的工作跳过，因此多次收尾幂等。

启动自动恢复分两条轨道：

- 任务轨道（worker / embedded 进程的 `MainRuntimeService.startup()` 扫库）：任务仍 `in_progress` + paused，且台账里有该任务 → `resume_task`（清暂停 + 重新入队），不触发「异常停止」恢复清洗、不写 `metadata.recovery_notice`；paused 但无台账行的任务保持暂停（用户暂停跨重启存活）；`in_progress` 未暂停的任务仍走 `_recover_interrupted_task`（异常中断清洗 + 恢复提示）。恢复时若 `task_runtime_frames` 无遗留帧，补造的帧只标 `runnable`（可运行、待调度）、不标 `active`——执行资格由随后的调度轮次授予，保证 `task_progress` 不会把「恢复后还没人跑」的任务渲染成运行中。已有帧时的整帧重写只重置标志位：帧的 messages 正文指针由 `_runtime_frame_record` 的保留规则守住（见本文「任务侧」帧写入条目），一次恢复不得缩减任何节点的 durable 历史。台账行消费后即删；引用已不存在任务的残留行在 startup 时退役。引擎级中断还有进程内兜底：`TaskActorService` 收到无标志 `CancelledError` 时经 `interrupted_task_requeue_callback` 触发 `MainRuntimeService._requeue_interrupted_task`——延迟 1s 后仅对「仍 `in_progress`、无取消/暂停标志、调度器未关闭」的任务重新入队（避免任务在存活进程内悬空，也限制杂散取消未消失时的重排回转速度）；进程退出收尾中调度器已关闭，该重排自然无效，任务由上述启动恢复兜底。
- 会话轨道（web runtime `ensure_web_runtime_services` 收尾，heartbeat 启动之后）：按台账逐个重建 runtime session（恢复 frontdoor baseline），再经心跳内部轮 `shutdown_resume` 事件唤醒；被暂停的用户请求通过暂停回合种子对账回到模型上下文，模型继续执行并产出用户可见回复。lane 合同归 `heartbeat-system.md`「Shutdown Resume Wake」。会话文件已不存在的台账行直接退役。
- 恢复提示：`metadata.recovery_notice`（「本任务遇到异常停止…」）只在**非优雅中断**（进程被强杀、帧/事务被截断）的恢复清洗时写入；优雅暂停 + 自动恢复不产生该提示。UI 渲染合同见 `web-and-admin.md`「Task Recovery Notice UI Contract」。

维护者须知：

- “重启后任务自动继续”依赖台账与共享 SQLite（WAL），不依赖内存：web 进程写台账、worker 进程消费，二者交接的媒介是 store。
- 关闭托管 worker 之前，web 会先释放该 worker 的 `task_worker` lease 行（`keep_worker` 关闭且确实存在托管进程时）——否则新 worker 在 lease TTL（20 秒）内启动会撞 `worker_lease_unavailable`。
- 若重启后任务仍停在 paused 且不自动恢复：先查台账行是否与任务 id 一致、`task_commands` 是否有未消费的 `pause_task` 残余、worker 是否真正拿到 lease 完成 startup。
- 若“优雅重启”后仍出现异常停止 toast：说明该任务的暂停早于本次 shutdown（例如早已被暂停、退出前又被手动 resume），或退出路径没有经过 web runtime 收尾（如单独强杀 worker 进程）。
- 启动脚本的优雅退出协议（先调 `/api/bootstrap/exit` 再强制清理）归 `operations-and-maintenance.md`「基本启动方式」。

## Prompt Cache Family And Actual Request

基线合同摘要（完整取证与排查归缓存排查文档）：

- caller-side prompt cache family 只由稳定前缀加显式 cache-family revision 输入决定；普通 callable/candidate/hydrated 漂移、阶段门控收紧与 hydration promotion 可以改变 actual request，但不得自行轮转 family key。
- 每次 `call_model` 必须把该轮重建的 request 与匹配重建的 `prompt_cache_key` 一起发送；CEO/frontdoor 与节点侧都保持静态前缀前置 append-only、请求尾部区域恰好一份当前 runtime 契约，且契约排在该轮 turn-only note / 当前 user 回合之前（末位保持 user 消息），携带历史中的旧契约与 turn-only note 一律剥掉。
- 两侧都为每轮 `call_model` 持久化专用 actual-request artifact：CEO/frontdoor 写 `.g3ku/web-ceo-requests/<session>/...json`（`visible_frontdoor` / `token_compression` / `inline_tool_reminder` 共用同一条时间线），节点写专用 JSON 且 `actual_request_ref` / `latest-context` 指向它；这些 artifact 是 provider-facing 顺序的取证权威，durable baseline 与快照则在持久化前剥掉契约消息。节点 artifact 与 `task.model.call` 行同时携带 `request_seed_source` / `request_seed_message_count`（第一跳 scaffold adoption 的来源诊断）。
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
- `memory/notes/` 存 `ref:note_xxxx` 引用的可选详细 note 正文，保持小而人类可读。note 正文可由记忆管理页的 note 窗编辑（保存前二次确认并写审计，无删除入口）；删除记忆时可在删除确认对话框勾选其引用的 note 同步删除（仍被其他记忆引用的 note 会标警并默认不勾选）。失去引用的孤儿 note 由 doctor 检查与 `reconcile-notes` 报告/清理；界面与端点契约见 `web-and-admin.md`「Memory Management Page And Admin Contract」。
- `memory/queue.jsonl` 是唯一持久队列，带每请求处理状态（`pending` / `processing`、重试计时、最新错误文本）。队列条目只有两种类型：`write`（显式或已提炼的记忆文本，等待真正的记忆处理）与 `delete`（自然语言记忆删除请求，等待内部 memory agent 解析成具体 id）。
- `memory/failed.jsonl` 是失败停车区：处理尝试失败的批次（含完整载荷与错误历史）整体移出主队列停在这里，主队列继续流动。每条记录带 `failed_id`、`category`（`provider_error` 瞬时类 / `protocol` 协议违规类）、`status`（`parked` 等待中 / `requeued` 已重排回队列）、`park_count`、`auto_requeue_count`、`manual_retry_count`、`error_history`（失败与重排事件的时间线）与累计 usage。停车不写终态记录，`request_id` 不进入已处理集合，重入队后不会被幂等去重误删。
- `memory/ops.jsonl` 是滚动终态历史，不是进行中重试日志，也不是 append-forever 归档：applied 批次、`precheck_failed`（载荷本身不可恢复）与 `operator_discarded`（操作员显式放弃停车记录）等 durable 终态结果连同最终 snapshot / compression 元数据一起落在这里；处理尝试失败先进失败停车区而非终态历史；超过 7 天的行在正常运行时读写中自动清理。终态行不记录入队侧 `trigger_source`；区分普通窗口批次与压缩冲刷批次要对照会话转录时间线。
- `memory/review_state.json` 是普通复核窗口的按会话缓冲元数据：缓冲轮次载荷、阶段 delta cursor、已上报可见工具记录 cursor；不是已提交的用户记忆。
- `.g3ku/memory-requests/` 存暴露请求元数据的 memory 请求 artifact；processed 行可以指向这些路径供后续取证。

维护边界：

- queue 文件是运行时元数据，不是用户记忆内容。
- 权威/快照/笔记分工见上面的子系统列表；memory-worker lease 单活规则见下面的运行时合同。

运行时合同：

- `## 长期记忆` 快照注入在稳定前缀的固定位置（`system` 之后、全部历史之前，即 message index 1），所以它的字节变化等价于把身后整段历史重铺一遍。取数因此是**会话级冻结**：`adopted_memory_snapshot_text()` 在会话首次 prompt 组装时读一次 `MEMORY.md`，把只含 `---` 分隔记忆文本的展示渲染钉在会话状态上（剥掉记忆 id 与日期/来源头；内部 memory agent 仍看完整受管快照），之后所有 provider 往返——包括同一轮内的多次工具往返——复用同一份字节。改写冻结值只发生在采纳点：会话首次组装、内联 `token_compression` 轮末（`_flush_memory_review_after_compression()`：顺序上必须在复核窗口冲刷与 `run_due_batch_once()` 之后，否则读到的是冲刷前的旧文档）、手动压缩成功后（回合外车道够不到轮末采纳点，单独接一次）。读盘瞬时失败（记忆 worker 正在重写 `MEMORY.md`）保留上一份冻结值且不盖采纳时间戳，一次失败不得把整会话钉成空记忆。采纳点之间的记忆提交或删除对当前会话不可见，不同会话因此可以呈现文档的不同版本——这是规则本身；可见性窗口等于采纳点间隔，而压缩只在上下文逼近模型窗口时才发生。缓存口径与 artifact 判读见 `context-and-cache-troubleshooting.md`「长期记忆快照的会话级冻结」。
- `memory_write` 与 `memory_delete(content=...)` 只向单一记忆队列入队，不内联修改已提交记忆；surfaced agent 请求删除时不传记忆 id。
- `RuntimeAgentSession` 把自主复核窗口缓冲在 `memory/review_state.json`，并在三个时点自动入队直接 `write` 批次：配置的普通轮窗口阈值（默认 5 轮）、token 压缩冲刷（`token_compression`）、会话结束/删除冲刷（`session_boundary`）。token 压缩冲刷只看“当轮内联压缩真实发生”这一事件信号：会话级标志在回合内任一请求压缩 applied 时置位、轮首清零，不读跨轮残留的 `frontdoor_history_shrink_reason`——后者是“baseline 为何比上一轮短”的解释（合同见 `context-and-cache-troubleshooting.md`「frontdoor_history_shrink_reason」），不是当轮压缩事件，用它判冲刷会把上一轮的压缩算到本轮头上。阶段压缩（`stage_compaction`）只裁会话侧提示词历史、不冲刷复核窗口。不支持的冲刷来源必须保持缓冲窗口原样，不得制造队列行。复核载荷与用户在界面看到的可见面一致：用户轮记录用户消息与助手回复；心跳/cron 内部轮只记录可见的助手回复与阶段可见面，隐藏事件束提示词不进载荷；静默 `HEARTBEAT_OK` 内部轮与 `[G3KU_SILENT]` 静默轮（见本文「静默回复」）都不入队。每轮载荷含 `stages:` 段（JSON，顺序与界面阶段轨一致）：回合记录时点从阶段可见面中仍保留 `rounds` 的阶段**全量捕获**所有新出现的工具调用，不设每轮条数上限——复核窗口内的阶段因此始终以完整未压缩形态送达内部 memory agent，不受会话侧提示词阶段压缩的影响；新记录按所属阶段嵌套进 `tool_records`，阶段内按目标 → `tool_records` → 阶段总结排列，携带新记录的阶段附带可见面当前完整目标与阶段总结。每条工具记录含工具名、状态、入参提示（写入侧已限长）、输出预览（截 240 字）、输出全文（截 400 字）与不截断的伴随中途输出（round.text，同一 round 只附一次）。工具记录按会话已上报 cursor（上限 5000；空 `tool_call_id` 用内容指纹）跨轮去重。`review_state.json` 除 `pending_turns` 外维护阶段 delta cursor 与工具记录 cursor：冲刷只清缓冲轮次、保留两个 cursor（普通窗口与 token 压缩冲刷）；会话结束/删除冲刷在入队后连同 cursor 一并清除该会话复核状态。`stages:` 段出现的阶段是：相对上一次复核新出现或实质变化的阶段（阶段 delta），以及本轮携带新捕获工具记录的阶段。
- 专用内部 memory agent 以 FIFO 同-op 批次（`write` 与 `delete` 不混批）消费队列，走 `memory` 模型路由，只有读写 `MEMORY.md` 与 note 文件的受限工具面；它把自然语言删除请求解析成具体 SQLite id，并可报告实质影响该批次的 `inspired_memory_ids`。
- 每次非读变更后，运行时先改 SQLite、再重建 `MEMORY.md`、最后检查快照大小：超过 `document.compress_trigger_chars`（默认 `16000`）时按 `passed_count DESC`、`refresh_count ASC` 顺序压缩，先把整行替换为 `minimal_memory`，再只删除已压缩的非 `from_user` 行，直到回到 `document.compress_target_chars`（默认 `13000`）或没有安全压缩工作。
- 队列消费跨进程单活：每次 `run_due_batch_once()` 必须先拿 workspace 级 memory-worker 文件锁；拿不到锁的进程保持队列不动并报告 `worker_lease_unavailable`。`request_id` 是持久幂等键：处理批次前会丢弃 `memory/ops.jsonl` 中已出现过 `request_id` 的队列行。

队列状态机与失败停车语义：

- `pending` 表示请求尚未被认领进批次；`processing` 表示队列头批次当前由唯一 memory worker 持有。
- provider 调用失败（限流、超时、上游错误响应或抛错）与协议违规（memory agent 批内自修复用尽仍不给出有效终态工具结果）都把批次**停车**到 `memory/failed.jsonl` 并清空其在队列中的行：失败不阻塞队列，也不立即写终态历史。两类失败由响应形态区分：错误响应（`finish_reason="error"` 或带 `error_text`）按 `provider_error` 记录真实 provider 错误文本；模型真实回复但违反 `memory_apply_batch` 协议按 `protocol` 记录。
- `memory` 角色未配置、或运行时配置不可读时，队列头保持 `processing` 并携带错误（`blocked` / 配置错误路径），阻塞后续请求——这类是全局配置问题，停车重试没有意义。
- 成功信号自动重排：每次批次成功 applied 后，运行时清掉本批 `request_id` 对应的停车记录（终于成功的批次不留痕），并把停车区里最早的一条 `provider_error` 记录重排到队尾（`status=requeued`）。没有成功信号就没有任何自动重试；`protocol` 类记录不参与自动重排。`queue.auto_requeue_on_success`（默认 `true`）可关闭该信号。自动重排不设次数上限，节流完全来自成功信号。
- 人工裁决：管理员可对停车记录执行重试（按原 `request_id` 重新入队尾）或放弃（写 `operator_discarded` 终态记录进 `ops.jsonl` 并删除停车记录与队列残留行；`request_id` 由此进入已处理集合，重复入队被幂等去重）。端点与前端入口见 `web-and-admin.md`「Memory Management Page And Admin Contract」，operator 排查流程见 `operations-and-maintenance.md`「Memory Queue Workflow」。
- 持久化的 `processing` 队列头是重启恢复状态，不证明仍有活跃 worker；重启后等待存储的 `retry_after` 再重试同一头批次。`processing_started_at` 记录队列头批次首次成功认领，跨重试保持稳定，不是“最后重试时间”；停车记录随 `items` 保留该字段供跨重启排查。
- 语义非法的模型输出应在同一处理批次提交前完成自修复；自修复用尽后批次进入失败停车区等待人工裁决，而不是无限重试或静默丢弃。载荷本身不可恢复（空正文等 precheck 失败）仍直接写 durable discarded 终态。
- `ops.jsonl` 出现两条相同 `request_id` 的终态行是 bug 信号（历史多 worker 竞争或旧版本运行），不是正常重复写入。
- `doctor` 报告含 `failed_parked` 检查与 `failed_parked_count`：存在停车记录即报 issues_found，detail 列出前 5 条 `failed_id(category)`。

瞬时执行状态明确在长期记忆边界之外：pause/resume 控制数据、进行中任务状态、临时修复标记与 runtime-only 协调笔记属于 transcript、session、task 或 stage 运行时状态，不进入 `MEMORY.md`。

队列卡住、重复写入、调试顺序、CLI 与 reset 等 operator 工作流详见 `operations-and-maintenance.md`「Memory Queue Workflow」。

## Internal Turn Contract Notes

Heartbeat 与 cron 内部轮次共享同一内部轮次合同，完整契约详见 `heartbeat-system.md`「Continuation Contract」与「Cron Reminder Contract」。本文只记运行时层不变量：

- 内部轮次与普通可见轮次一样通过 `RuntimeAgentSession.prompt(...)` 执行，携带各自的内部来源元数据；它们会清掉 live-only 调试面（`frontdoor_selection_debug`、每轮 actual-request 指针），但不在 prompt 组装前清零 session-owned 请求体 / 阶段 / 压缩连续性状态。
- 规则文本与事件载荷以隐藏内部提示消息追加：`prompt_visible=true`、`ui_visible=false`，带 `internal_prompt_kind`（`heartbeat_rule` / `heartbeat_event_bundle` / `cron_rule` / `cron_event_bundle`）；heartbeat 追加 `system` 规则 + `user` event-bundle，cron 追加两个隐藏 `system` 块。存在权威 frontdoor 基线时，内部轮次直接继承普通 CEO tool/skill 暴露合同（含无有效阶段仍保留全量 callable 的合同）。
- 无基线的内部轮（重启后首轮、全新会话首轮）不进入续跑分支：内部事件消息单独交给 prompt 组装，由新建路径注入，基础系统提示保持首位。续跑分支只在存在真实请求体基线时使用——否则仅有的内部事件消息会冒充完整旧请求体、让基础提示被静默丢掉。内部轮基线/恢复细节见 `context-and-cache-troubleshooting.md`「heartbeat / cron 按普通 continuation shrink 规则排查」与「Baseline 合同与恢复顺序」。
- 服务层不得替模型自动重试任务，也不得合成回退 assistant 回复。
- `HEARTBEAT_OK` 是唯一的 live-only ACK 例外：ACK 事件可以在 UI 展示，但不得新建可见 assistant 转录条目；隐藏内部提示消息（`ui_visible=false`）是 durable 且 prompt-visible 的，与 live-only ACK 是两回事。

## Repeated Tool Call Guard Notes

执行阶段重复工具调用的同轮执行前去重（reused 合同）、跨轮软拒绝、修复消息、升级语义与只读检索分支契约详见 `tool-and-skill-system.md`「Duplicate Tool Call Guard」。

## Resource Generation Checks For Semantic Catalog Freshness

语义目录新鲜度、节流资源代检查（`resources.reload.poll_interval_ms`）、指纹刷新与元数据编辑失效规则详见 `tool-and-skill-system.md`「Catalog Freshness」。

## CEO Frontdoor Canonical Context Contract

`frontdoor_canonical_context` 是 CEO/frontdoor 唯一的跨回合阶段真相源：

- 它是 durable 的跨回合阶段/历史视图；turn finalization 把当前轮阶段账本并入该结构。`frontdoor_stage_state` 与 `compression_state` 是运行时工作状态，不需要在每个新用户 / heartbeat / cron 轮次的 prompt 组装前清空；当前轮本地状态为空时，`prepare_turn` 可以复用 session-owned 请求体与这些快照重建下一个 provider 请求窗口。
- session/runtime 同步不得把 request-local 投影写回 `frontdoor_canonical_context`：只有 turn finalization 允许向 durable canonical 链追加 completed-stage 数据；`frontdoor_canonical_context + 当前 frontdoor_stage_state` 派生出的一切只是当前请求的可见 workset 数据。
- 近场 stage workset 从 `frontdoor_canonical_context + 当前 frontdoor_stage_state` 派生，不从 transcript `execution_trace_summary` 或平铺 `tool_events` 重建。round-level 工具记录同时保存归一化原始 `arguments`；小输出内联在 `output_text`，大输出外置为 `output_ref` + `output_preview_text`，prompt 渲染器不把 artifact 正文读回内联。
- canonical 归一化以 `stage_id` 和完成阶段内容身份做 last-write collapse：同一逻辑阶段被 rebase 后再次并入时保留最新副本，不重复追加整个携带 workset。排查 sidecar 膨胀时，记录数应与 distinct stage 身份数一致；持续增长说明合并边界回归。
- `project_canonical_context_for_transcript()` 只用于 assistant 转录记录：保留当前 canonical 表示窗口（最近 3 个完成普通阶段与活动阶段为 raw，更早阶段为 compact），并截短 raw round 内超大工具正文与入参（`output_text` > 2000 置空、结构化 `arguments` > 2000 置 `{}`、`arguments_text` 与 `round.text` 各限 4000）。provider prompt 仍以 durable canonical context 和当前 stage state 为权威，不读这份转录投影。
- Web UI 载荷使用同一投影视图，而不是把未投影的 live workset 直接下发：`project_canonical_context_for_ui_payload()` 保留 raw 窗口阶段未投影的 round 正文；`ui_canonical_context_delta()` 先把前后两侧都按转录投影对齐，再让已存在阶段沿用基线表示（compact 不因新增阶段造成窗口移动而重新展开），因此新回合 delta 只携带新阶段与真实变化，并把 delta 保留阶段的正文回填为实时未投影值。UI 最新气泡重新出现全部历史阶段的回归通常是 UI delta 退回原始 `canonical_context_delta`。
- 转录投影对已投影输入幂等：带 `stage_window` 标记的转录行本身即转录投影视图，按序回放（如快照 delta 链）可直接作为基线视图使用；`ui_canonical_context_delta_from_views()` 接收两侧已投影的视图，输出与从原始输入投影的路径逐字节一致，逐行重投影整份转录属于平方级构建回归。
- assistant 轨道行的存储形态二选一：checkpoint 行（`canonical_context_projection: stage_window` + 全量视图）或 delta 行（`delta_window` + `cc_upsert`：stage 级 upsert，已有 `stage_id` 原位替换、未知追加，`headers` 承载顶层字段变化）。写入侧（`plan_transcript_cc_row`）相对**物理上一条轨道行**的视图编码——含 ui_visible False 的 heartbeat/cron 行，读侧游标必须同样遍历隐藏行；每行都过 `encode_cc_upsert` 的重放自检，不可编码、链行数达 40、链字节达 96KB 或没有前序锚点时回落 checkpoint，首条轨道行永远是 checkpoint。单行物化走 `materialize_transcript_view`（回溯最近全量锚点正向重放，链长有界）。就地替换轨道行（暂停归档）必须带替换前视图调 `repair_transcript_cc_chain` 重编码下游，否则旧链上的 delta 行静默错位。存量文件在 `_load` 里经 `migrate_transcript_rows_to_delta` 收敛（逐行重放校验、失败行自动落 checkpoint、幂等可重入），metadata 行 `cc_format: delta_window_v1` 使后续加载跳过迁移。
- 若当前轮阶段状态里已包含与 `frontdoor_canonical_context` 中实质相同的 completed stage，prompt 组装必须按重叠处理、跳过把它 rebase 成新的合成 stage id——否则一个 completed stage 会在 fresh-turn 重建中膨胀成重复的原始阶段块。
- UI 面向的 turn payload 暴露当前轮的 `canonical_context` 投影切片；prompt 组装读 durable 跨回合 canonical context，inflight / paused / final-reply payload 只描述可见轮自己的阶段轨迹。

第二条连续性合同：`frontdoor_request_body_messages` 是下一轮 CEO/frontdoor 的 session-owned provider 请求体基线，刻意不含 `frontdoor_runtime_tool_contract` 消息（动态工具暴露每轮作为新的尾部合同重建），也不含 `## 长期记忆` 快照（会话级冻结的展示块，只在当轮请求注入，落史会逐轮累积污染上下文；它的取数与采纳点合同见本文「Memory Runtime Notes」），且只允许在 `token_compression` 与同轮 `stage_compaction` 两个信息损失边界收缩，或经操作员发起的 `user_edit_truncation` 在轮间整体替换（见本文「Frontdoor Context Compression (Current Contract)」）。fresh 可见轮次中该基线是连续性权威来源：必须从请求体基线继续，而不是从阶段重放重建新的主前缀。

## Runtime Contract Lane

模型面向的运行时契约是以 `## Runtime Tool Contract` 开头的 system summary 块（summary 形式、修复车道、“尾部只带最新快照”与基线剥离规则详见 `tool-and-skill-system.md`「四个概念必须分清」与 `context-and-cache-troubleshooting.md`「append-only 规则」）。本节记录本文拥有的 canonical 表示规则与运行时边界。

Canonical 阶段状态按以下表示规则收敛（这是 canonical 链唯一允许的信息损失边界）：

- 最近 3 个完成的普通阶段保持 `raw`；该规则与当时是否存在活动阶段无关，纯对话回合（无活动阶段）同样适用。
- 更早的完成普通阶段变为 `compact`。
- canonical 链只有 `raw` 与 `compact` 两级表示：历史数据中已存在的归档压缩阶段（`stage_kind="compression"`，外置表示）继续规范化与渲染，运行时不把完成阶段合并成新的归档阶段；长会话的阶段体积由 `compact` 块承载，并在 `token_compression` 边界整体收口——块按阶段账本逐轮重渲染，收口（上一条）是压缩能真正收缩阶段体积的支点，缺了它压缩只删得掉工具肉身、删不掉块，总体积兜底也就无从谈起。
- 这些表示渲染为 `[G3KU_STAGE_*]` 消息块时以 system 角色落地（识别端接受 assistant/system 双角色以兼容存量旧块；角色合同本体见 `context-and-cache-troubleshooting.md`「压缩块的格式与字段语义」）。
- 完成阶段还可以带 `context_visible: false` 收口标记：它不是第四种表示，而是"这条阶段已经进过全局摘要"的 durable 记账。带标记的阶段不渲染任何块、也不占最近 3 个的 raw 保留名额，但记录本身留在账本里——Web 时间线与转录投影照常读到它。标记只在 `token_compression` 边界写入（见本文「Frontdoor Context Compression (Current Contract)」），逐轮重算按回归排查。字段缺失即视为可见，存量账本与旧 sidecar 不需要迁移。

另有两条运行时边界：

- prompt token trace 只有两个字段：`pre_request_prompt_tokens` 是内联 `token_compression` 之前的发送前估算（必须包含 stage workset）；`effective_prompt_tokens` 是 prompt 组装完成后最终真实发送请求的估算。
- 节点侧 token 压缩是同一 `token_compression` 边界的节点实现：用任务目标针对型提示词对可压缩历史做一次 inline LLM 摘要后重写当次请求。它只是针对当次请求的 live 重写：可以缩短 provider-bound `request_messages`，但不得改写持久阶段历史、frame `messages`，或从 `model_messages` 派生的稳定 prompt-cache family 输入。

若下一轮基线以两个收缩边界与 `user_edit_truncation` 之外的任何理由变短，按意外上下文损失排查；守卫自愈行为见本文「Frontdoor Context Compression (Current Contract)」。`user_edit_truncation` 的替换发生在轮间（守卫比较的是"会话基线 vs 本轮新请求"，替换后新请求只会更长），不会触发 quarantine。

token preflight 估算、触发阈值、`effective_input_tokens` 真相车道、压缩优先顺序与诊断字段详见 `context-and-cache-troubleshooting.md`「Prompt Cache Family 与 Actual Request」；图片上传与 `content_open` 的多模态展开、`5 MiB` 守卫与单次发送 overlay 规则详见 `web-and-admin.md`「Image Upload Gating」。

## CEO Inline Tool Reminder Sidecar

CEO/frontdoor 直连长时工具有一条独立的 live-only 内联提醒侧车道，运行时边界如下：

- 内联执行注册在 `InlineToolExecutionRegistry`，与 detached `ToolExecutionManager` 后台执行语义相互独立；内联执行 id 不是 detached watchdog 执行 id。
- `CeoToolReminderService` 对会话持久化是只读车道：不调 `session.prompt(...)`、不取普通 turn 锁、不创建 heartbeat / internal turn；提醒文本与决策不写入 transcript、canonical context 或后续 prompt 历史注入。
- 侧车道优先复用最近一份已持久化的 CEO actual-request artifact 作为 provider-facing scaffold，与主轮共享同一段可缓存前缀；artifact 因内存守卫降级（`artifact_persistence_mode=memory_guard_degraded` / `memory_guard_minimal`）时，退回 `CeoMessageBuilder.build_for_ceo(..., ephemeral_tail_messages=...)` 重建。
- 侧车道停止决策以普通工具失败形式回到主轮（`reason_code=sidecar_timeout_stop`，per-tool 子取消令牌），与用户主动暂停/取消是两条不同路径。

自定排程巡检（首档 120s、模型自定下次间隔）、观测感知决策、`STOP` / `CONTINUE <seconds>` 语义、失败兜底、统一工具 timeout 硬上限与 timeout-stop 错误合同详见 `heartbeat-system.md`「CEO Inline Tool Reminder Sidecar」。

## Node Provider Request Scaffold

执行与检验节点保持两套工具可见性视图：

- `tool_names`：运行时契约强制使用的每轮权威可调用工具集。
- `provider_tool_names`：构造真实模型请求时使用的 provider-facing schema bundle；新写入代表该 turn 家族已持久化的 RBAC-visible concrete-tool bundle。`pending_provider_tool_names` 仅是兼容字段，新写入保持 `[]`。
- 节点发送 artifact 还记录 `provider_tool_exposure_revision`（真正到达 provider 的已持久化 provider bundle 的短哈希）；`provider_tool_exposure_commit_reason` 仅是兼容字段，新写入保持 `""`。

这套分离只为在不削弱阶段门控与工具 hydration 规则的前提下提高 prompt cache 稳定性。同轮内节点多轮循环的 provider 请求构造走 append-only scaffold：上一份真实请求体 + 上一轮新增的 assistant / 工具结果消息 + 最新 `node_runtime_tool_contract` / turn-only note 尾部（契约在前、当轮 note 在后，末位是 user 回合提示）。跨 run 的第一跳（notice 唤醒、restart/resume、恢复重放）骑同一条链：请求以持久 actual-request scaffold（内部形态 `request_messages`）为前缀经头探针 + 多锚点尾对齐 adoption，投影超出 scaffold 覆盖点的记录（held notice、重放轮、当前 user 回合）作为显式 delta 追加；每轮请求的来源落 `request_seed_source` / `request_seed_message_count` 诊断（runtime frame、actual-request artifact、`task.model.call` 行同源），种子不可用（缺失、guard 降级、头漂移、对齐失败）时回退投影重组装并带 `fallback_*` 标记。scaffold 只是请求构造脚手架——不替代节点持久/压缩后的 `message_history`，也不重新定义哪些工具可调用；它存在的唯一目的是：当阶段压缩在回合边界修剪历史时，provider 看到的是 append-only 增长而不是早期前缀重写。`provider_tool_bundle_seeded` 只是兼容/诊断提示，真实行为由活跃/待定曝光状态加 token 压缩提交门控驱动。完整规则详见 `context-and-cache-troubleshooting.md`「append-only 规则」。

## Frontdoor Context Compression (Current Contract)

当前 CEO/frontdoor 请求收缩模型只有两个信息损失边界：

- `stage_compaction`
- `token_compression`

此外存在一个性质不同的合法替换边界：`user_edit_truncation`（操作员发起的编辑重发/Fork 截断）。它不是轮内自动收缩，而是轮间对 continuity 状态的显式整体替换——以每轮边界快照为数据源回退到某个干净轮边界（或截断到会话开头时替换为空基线），同时重写转录并删除被截断轮的 actual-request artifact。除此之外，任何其他理由让下一轮请求基线变短，都应按回归排查。长上下文只由两项机制约束：近场 stage workset compaction（按阶段归属原位压缩过期完成阶段的工具调用，与执行阶段提示词逻辑共享），以及在最终请求接近所选模型窗口时改写旧 body history 的内联 `token_compression`。归档压缩阶段（`stage_kind="compression"` + `archive_ref`）是历史遗留表示数据：持久化状态中已存在的归档阶段继续规范化与渲染，运行时不产生新的归档阶段。

### `token_compression`

- 内联同轮 LLM 重写，在 provider 发送前执行：保留稳定 system 前缀、最新运行时工具契约尾与最近的 body-history 尾部，只把更早的 body-history 区间重写为一个 `G3KU_TOKEN_COMPACT_V2` 标记块。
- 压缩请求本体是 append-only 形态：原请求体（去尾/去契约）末尾追加一条 user 指令（指令角色用 user 而非 system——部分 OpenAI-compatible 网关对非首位 system 处理不稳）。压缩请求前缀与刚发出的正常请求字节一致，provider 前缀缓存真实命中，新增 token 只有指令本身：与正常流量同形，快速成功或快速 429 重试。禁止把整段历史重序列化为单条 JSON 巨包——巨包缓存全失效，且在配额饱和时会被 provider 静默挂起（长时间零 chunk 直到 attempt 超时）。CEO/frontdoor 用通用历史摘要指令；节点用任务目标针对型指令（内嵌 `task_goal`），摘要须保住任务继续完成所需的关键结论、数据与待办；节点压缩块结构为 `system_prefix → bootstrap_user（任务目标始终保留）→ append_notice_tail → 压缩块 → recent_tail → 契约 → turn-only note`。节点深埋 `raw_notice_window` 剔除触发时压缩请求不构成纯前缀，接受部分缓存复用（仍优于整体重打包）。
- 超窗分块：单发压缩请求自身估算超过模型窗口时，按原子组（工具调用组不可分，`iter_compaction_atomic_groups`）贪心装箱，逐块用传统 system+user 形态（块不构成对话前缀，但体量与正常流量同级）生成摘要，块摘要带块标号拼入同一压缩块；合并后仍超预算时做且仅做一次归并。诊断字段：`compression_mode`（`llm`/`llm_chunked`）、`chunk_count`、`merge_pass_applied`。
- 空摘要重试对齐普通发送路径：frontdoor 对空响应（含 provider 错误文本）先试运行时配置失效重建模型链，否则退避重试、不设上限，每次尝试之间检查取消钩子，压缩中 pause/取消随时生效；节点 helper 空响应按 `_PROVIDER_RETRY_LIMIT` 封顶重试后退避耗尽。空摘要永不成为压缩产物，也永不误报为「上下文超限」——`frontdoor_context_window_exceeded` 只保留给真正无可压缩历史、或压缩后重算仍超窗。节点 helper 调用抛异常仍按 preflight 错误让发送失败，不做静默丢历史的 marker-only 回退；无可压缩历史且已超窗同样失败。压缩进行中 pause 视该轮为终态，下一次激活重新 prepare → estimate → 可选压缩 → send。
- 触发阈值绑定运行时所选模型的 `context_window_tokens`，触发源按 usage-first 合同取数：请求相对上一真实请求可 append-only 比对时，以上一请求 provider 回执的有效输入 token（`input + cache read`，锚定 `actual_request_hash`）加当轮增量估算为准；不可比对、无 usage 真值（首跳 / 重启 / 压缩后首跳）或 usage 缺失时才退回全量 preview 估算。超过 `80% × 0.95` 有效阈值（或直接超窗）先尝试一次压缩，压缩后重算仍超窗则失败；旧「preview 与 usage 估算取大者」的语义已废弃——可比对时 preview 不再覆盖 usage 真值。
- 跨轮可比性投影是 usage-first 合同的一部分：当前请求与上一真实请求在同一投影下比较——工具契约 / turn-only note / 长期记忆快照剥离、多模态块剥离、内部提示历史折叠、动态 overlay（长期记忆写入提示 / 已检索记忆使用提示）剥离；阶段窗口重写对两侧施加同一 trim（幂等）。前一槽位为空（会话重载/重启）时回退扫描会话 artifact 目录取最新一条可见请求，usage+delta 估算跨进程重启后的下一真实用户轮仍然可用。投影不一致会让前缀恒不等、估算静默退化为全量 preview 并在大会话上系统性高估、误触发压缩。
- 对 CEO/frontdoor 与节点运行时，`token_compression` 都不是 provider bundle 提升边界：压缩发送沿用已持久化的 `provider_tool_names`，任何 provider-bundle 刷新落在压缩后的第一个普通 turn。
- 尾部收敛保证：保留的最近 body-history 尾部（CEO/frontdoor 基准 4 条，节点基准 12 条）是压缩后请求体的不可压缩下限。尾部边界必须对齐到完整的工具调用组：尾部首条是 `role=tool` 结果时，边界向前扩展，把声明它的 `assistant(tool_calls)` 消息连同结果一并保留进尾部；扩展上界是单个工具批次，最坏整个 body 成为尾部、无可压缩历史，由调用方按既有「无可压缩历史」分支处理（已超窗则发送失败，不静默）。未对齐的边界会留下声明已落入摘要区的 tool 结果，产生孤儿工具结果：节点通道触发孤儿检测熔断（本文「暂停与恢复」），会话通道没有检测器、孤儿会静默直达 provider。对齐之后，再把尾部中超过字符上限（16000）的工具结果消息硬截断为「截断头部 + 检索指引」，保证压缩后估算必然收敛到窗口以内。若不做截断，一条超大尾部工具结果（例如 `content_open` 对单行巨型 artifact 的打开结果）本身就能让压缩后估算持续超窗——压缩检查必然抛错、回合必然失败，而该消息又始终落在保留尾部，形成每轮压缩、每轮失败的无限循环。CEO/frontdoor 的尾部起点还受 raw 阶段窗口约束：不得晚于最近 3 个完成阶段与活动阶段的首条消息（跨度超出上限时退回基准 4 条，尾部本来就是不可压缩部分，放太大就没压缩了）——收口把 compact 块摘掉之后，raw 窗口是压缩后上下文里唯一还带工具正文的层，被摘要一并吞掉就没有第三层可退。
- 节点追加通知的因果保留：`[G3KU_APPEND_NOTICE_TAIL_V1]` 未消费通知窗口（`raw_notice_window`）无论处在历史何处都原样保留在压缩块之前，不进入 LLM 压缩——追加通知往往是任务目标的最新变更；已消费汇总窗口（`compressed_notice_window`）本身已是摘要形态，随历史一起压缩。
- 阶段收口是 CEO/frontdoor 压缩的第三件事：本次真正被吞掉的阶段（= **终态**普通阶段 − 最近 3 个 raw 保留窗口 − 活动阶段 − 肉身或旧块仍留在保留尾部里的阶段）连同完整记录（逐条 `key_refs` 与轮次）确定性导出到 `session_temp_dir` 下的归档文件。终态判定是白名单（`completed` / `failed` / `完成` / `失败`）而不是"排除某个进行中字样"：两条车道各写一套状态词表（前门英文 `completed` / `active`，节点中文 `完成` / `进行中` / `失败`），认不出的状态一律当作还在跑——少收一轮只是多花 token，多收一轮就是把一个仍在写轮次的阶段收进摘要够不到的地方。"吞掉了哪一段"挂在压缩块的 `stage_archive.archived_through_created_at` 上——一个 created_at 上限，不是逐条 id 清单（375 条 id 要 8,469 字符 ≈ 2,300 token，比它守护的摘要正文还长，还得每轮随块重发），同一对象另带 `ref` / `stage_index_start` / `stage_index_end` / `stage_count`。**标记本身在 durable 基线推进的那一步（`_persist_frontdoor_actual_request`、回合收尾的账本提交点）才被应用**——压缩算完不等于 provider 看到了摘要，若那次发送随后失败或被暂停，基线仍是带块的旧请求体，此时翻标记就等于把那批阶段连同摘要一起丢掉。归档文件写不出来时整轮不收口（没有 `stage_archive` 就没有水位线）：宁可不缩，也不能把阶段收进一个模型打不开的地方。不删账本、不改内容、不动 `stage_index` 序列：阶段只是不再进 provider 上下文，账本仍是 Web 时间线的权威。水位线按 `created_at` 命中，顺带绕开了两套 stage_id 各一套序号的问题（同一会话实测 1..398 对 944..1341、交集为 0）：created_at 是同一条逻辑阶段在两份存储里共享的同一个值，所以 `frontdoor_canonical_context` 与 `frontdoor_stage_state` 各自就地标一遍即可。仍带 `stage_ids` 的存量块按内容身份（`created_at` + `finished_at` + `stage_goal` + `completed_stage_summary`，与跨 rebase 去重同一口径）跨存储匹配，读到那块被下一次压缩改写为止。水位线圈的是一段区间，落点再过一遍"内容肉身是否还在"：工具轮次或 `[G3KU_STAGE_RAW_V1]` 块仍在即将落定的请求体里的阶段、以及近场 raw 保留窗口占着的阶段都不收（那批内容没进摘要）；派生的 `[G3KU_STAGE_COMPACT_V1]` 块不算在场——标记一旦丢过块会整批长回来，那时必须还能再收一次。缺 created_at 的阶段水位线圈不到，只会多渲染一轮，不会丢内容。收口只能随压缩边界发生——逐轮按"块太多"自行收口会破坏 append-only。
- 证据引用由模型选号、由运行时抄写：单发压缩请求的指令消息尾部带一份带编号的候选清单（被吞阶段的全部 `key_refs`，同一 ref 取最后一次说明），模型只被要求输出 `## 证据索引` 小节、每行一个 `- [#编号]`，refs 正文由运行时按编号**逐字回填**进摘要块内部，越界编号丢弃，指向已不存在文件的路径剔除（`task:` / `artifact:` / `node:` / 裸 id 不做文件系统判定，会被误杀成死链）。摘要块内同时追加 `## 阶段归档` 一行，给出归档文件绝对路径与 stage 区间，模型要回看细节时用 `content_open` 打开。为什么这样分工：让模型自己抄引用路径的跨代逐字节保真实测约 30%（文件名对、完整串被改写），不透明 id 约 10%——抄写必然出错，选择才是它的强项；而索引段写在摘要正文内部，因此和摘要本身一样受下一轮压缩管辖，不会长成第二套逐轮重注入的地板。
- 分块压缩车道（`compression_mode=llm_chunked`）不要求模型选号：每块看不到全量候选，选择语义不成立。该车道照常收口与导档，摘要里只出现 `## 阶段归档` 指针，不出现 `## 证据索引`。
- 两条路径共用 `[G3KU_TOKEN_COMPACT_V2]` 前缀与同一份块形态：首行前缀、第二行 JSON 元数据、空行后接摘要正文，且元数据行必须能单独解析回来（前门的收口落点靠它读水位线）。压缩块 kind 分别为 `frontdoor_token_compaction_llm`（诊断 `mode=llm`）与 `node_token_compaction_llm`，靠 kind / `node_id` 字段区分。节点压缩产出同一份信封：证据引用候选与逐字回填、`## 阶段归档` 指针、`stage_archive` 水位线，归档文件落在该任务的 `task_temp_dir`（`kind=node_stage_archive`、`owner=task:<task_id>/node:<node_id>`）；被吞集合的判定与前门共用同一个 `summarized_stage_ids`（节点只有一份账本，没有两套 stage_id 的问题）。**阶段收口只在 CEO/frontdoor 应用标记，节点车道不应用**：节点的 `token_compression` 是 live-only（durable 写回用未收缩的投影），且阶段块从不进入发送体，所以账本翻标记对节点没有可收缩的对象；节点 artifact 里带 `stage_archive` 而账本无 `context_visible` 是设计边界，不是漏做。节点的 `## 阶段归档` 文案也只声明"本次压缩不再逐条展开"，不声称逐轮退出。
- 操作员可在回合外发起同一份 `token_compression`（CEO/frontdoor 通道，节点没有该入口）：`POST /api/ceo/sessions/{id}/compress-context` 立即返回 `running`，起跑的后台任务先 `await session.pause(manual=True)` 停掉在途工具与请求，再以**空草稿**重建 durable 基线作为压缩输入——没有新消息时预检重建出的请求体就是下一回合的起点，这正是被压缩对象。停手必须留在后台任务里而不是端点请求里：`pause` 与区分线落盘都要整份重写转录，渠道会话实测单份转录 70MB 级，放在请求内会顶穿浏览器侧 20 秒的请求超时，前端就把还在跑的压缩误读成失败（UI 侧合同见 `web-and-admin.md`「Manual Context Compression」）。摘要产物除返回 token 数外必须经 `_persist_frontdoor_actual_request` 同时成为新的 durable 基线与一条同源的 actual-request artifact，并把 `frontdoor_history_shrink_reason` 记为 `token_compression`；基线与 artifact 不同源会让重启后的字节对账判为失配、清空 trace 并静默丢掉这次压缩（详见 `context-and-cache-troubleshooting.md`「Shrink 原因与压缩边界」）。
- 回合外压缩没有 inflight turn 可承载进度，实时状态走 `state.compression`；它的取消复用同一套压缩代际软取消（`_cancel_active_frontdoor_compression_generation`），代际尚未建立时由端点直接掐任务。取消、provider 异常与进程中断都按「未得到摘要」处理，永不写出 `G3KU_TOKEN_COMPACT_V2` 基线。手动与自动两条路径都在终局向转录追加一条 UI-only 的压缩区分线行（`completed` / `paused`），UI 语义详见 `web-and-admin.md`「CEO Compression UI Contract」。

### `stage_compaction`

- 修剪的唯一真相源是 `stage_prompt_compaction.compact_stage_prompt_messages_in_place()`：最近 3 个完成普通阶段与活动阶段保留完整窗口；过期完成阶段的工具调用消息（`assistant+tool_calls` 与其配对 `tool` 响应）成对移除，对应 compact 块回插在该阶段首条被移除消息的位置。块锚点按「记忆位置 → 本次首条被移除帧位置 → 邻居夹逼」三级取定：两个直接定位来源都缺失时（请求体被重建，帧与旧块一并消失），锚点夹逼在前一个已确定锚点之后、后一个已知锚点之前，且锚点相对 `stage_index` 单调不减——「用户消息 → 该阶段块 → 该阶段最终回复」的相对顺序因此不依赖该阶段是否还有帧在体内；夹逼兜底可退化的只有精确邻接（原始定位信息已不存在），顺序不变量不受影响。块以 system 角色渲染，识别端接受 assistant/system 双角色，存量旧 assistant 块在下一次压缩渲染回插时自然收敛，过渡期不重复、不丢块）；阶段之外的用户可见对话原位保留；内部事件束（心跳规则/事件束、定时任务中文包装与 `[CRON INTERNAL EVENT]` 事件体）按缓存中性规则移除：只清理不早于本次压缩既有最早结构变化点的条目，本次压缩没有任何结构变化时一律保留，避免为清理历史内部事件额外打断 provider 前缀缓存。
- 阶段块识别排除工具调用回合：assistant 消息携带非空 `tool_calls` 时，即使正文以 `[G3KU_STAGE_*]` 前缀开头也不识别为阶段块——那是模型在发起工具调用的同一回合回显了阶段块。所有整块丢弃路径（前缀清洗、压缩 step-0、续尾重排守卫）都必须保留该消息，否则其配对的 `role=tool` 结果成为孤儿工具结果（节点通道触发孤儿检测熔断，会话通道静默直达 provider）；语义对齐节点侧契约剥离的 `_message_declares_tool_calls` 守卫。
- 原位放置是缓存硬约束：压缩块不整体收拢到上下文头部；除治愈遗留布局的一次性收敛外，每次压缩的前缀失效面从最早被压缩阶段的位置开始。
- 块是"按账本逐轮重渲染"的产物，不是历史消息：每轮从 `frontdoor_stage_state` / `frontdoor_canonical_context` 的合并视图重新生成一份块集合，因此仅重写请求体基线（`token_compression`）不会让块变少——收口标记才是。渲染集合的排除项固定为三类：活动阶段、`retained_completed_stage_ids` 的 raw 保留窗口、带 `context_visible: false` 的已收口阶段。已收口阶段若其工具肉身被别的路径重投影回体内，按原位移除且不再补块，这是收口的既定信息损失边界（正文已在摘要里）。
- 该规则与是否存在活动阶段无关；无活动阶段的纯对话回合同样压缩过期阶段并保留最近 3 个。
- 幂等：同一输入重写两次收敛为同一输出；归属保留阶段的残留旧块去重丢弃。
- 可以缩短活动历史窗口或 stage workset，仍是下一轮基线的合法收缩理由。
- 不是 provider schema 刷新边界：若某次发送的收缩原因是 `stage_compaction` 而 `provider_tool_names` 变了，按 provider-bundle 刷新路径 bug 排查。
- 压缩块的前缀标记与 JSON 字段语义（`G3KU_TOKEN_COMPACT_V2` / `G3KU_STAGE_COMPACT_V1` / `G3KU_STAGE_EXTERNALIZED_V1` / `G3KU_STAGE_RAW_V1`）详见 `context-and-cache-troubleshooting.md`「Prompt Cache Family 与 Actual Request」「压缩块的格式与字段语义」。

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
