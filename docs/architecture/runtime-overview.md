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

这意味着 `RuntimeAgentSession` 不只是维护“session 是否正在运行”，还维护了“当前可显示 turn 的身份”。如果这里的 `turn_id` 传播断掉，典型回归就是：

- 同 source 的多个 pending turn 被错误合并
- heartbeat 清理时误删或漏删旧 turn
- 前端残留一个只有“处理中...”的气泡

另外，手动 pause 之后的后续输入语义也有一个容易误改的点：

- Web 会话里，用户在手动 pause 后补充的新输入，不应直接被当成全新的独立 prompt
- 正确语义是：恢复原 paused request，并把补充输入作为 additional context 合并回该请求
- 因此前端/网关层若检测到 session 处于可恢复的 manual-pause 状态，应优先走 `resume(additional_context=...)`，而不是重新 `prompt(...)`

如果这里被改回“手动 pause 后的补充输入直接新开 turn”，典型回归就是：

- agent 只理解到补充片段，例如“前10个”，却丢掉原问题
- transcript 中出现语义割裂的新 turn
- 用户误以为模型忘记了上一句，其实是恢复路径走错了

可以把它看成“一个会话的状态机 + turn 执行器”。

新人容易低估这个文件的复杂度，因为它名字看起来像简单 session 封装，实际上它承担了：

- user turn
- heartbeat internal turn
- cron internal turn
- async dispatch 错误恢复
- paused execution context
- frontdoor stage state

## 4. frontdoor 与任务运行时的关系

G3KU 并不是所有问题都在 CEO 单次对话内完成。frontdoor 的职责更像是：

- 识别当前用户请求
- 组织提示词与上下文
- 判断当前这轮能直接回答，还是需要走任务运行时
- 在必要时触发任务工具，如 `create_async_task`

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
- `g3ku/runtime/frontdoor/message_builder.py` 继续按本轮会话状态动态注入可见 skill 摘要、候选工具、retrieved context、memory hint、全局语义摘要等 turn 级内容。

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

The memory runtime is now journal-first. `memory/sync_journal.jsonl` is the source of truth; the other files under `memory/` are derived projections or retrieval indexes.

- `memory/structured_current.jsonl` is the active long-term fact projection.
- `memory/structured_history.jsonl` and `memory/audit.jsonl` are history/audit projections.
- SQLite / FTS / Qdrant / `memory/context_store/` are unified-context retrieval projections.

Two maintenance boundaries changed with the single-user long-term memory model:

- Structured long-term memory no longer relies on frontdoor-provided `scope` to separate user preference slots. Dedup/replacement semantics now depend on stable identity fields such as category, entity, attribute, and time semantics.
- Runtime process notes, pause/resume control data, background-task progress notes, and other transient execution state should not enter long-term memory through `memory_write`. Those belong in transcript/session/task runtime state.

Because tool/skill catalog retrieval shares the unified context store, a full reset of `memory/` removes both user memory and catalog retrieval data. Catalog retrieval must be rebuilt after startup.

## CEO Frontdoor Legacy Compression Removal

The CEO frontdoor no longer keeps a separate legacy history-compaction layer.

- The earlier `_summarize_messages()` compatibility hook has been removed from runtime execution paths.
- Frontdoor history now reaches the model either as local workset stage windows/blocks or as the global semantic summary block.
- If you are debugging long-context behavior, there is no intermediate "message-count compaction" stage to inspect anymore.

This leaves two distinct mechanisms only:

- Stage workset compaction for the near-field prompt, shared with the execution-stage prompt logic.
- Global semantic summary refresh for older context outside that near-field workset.

When a maintainer sees a prompt continuity issue, the first questions should now be:

- Was the relevant context still inside the retained stage workset?
- If not, did the semantic summary coverage/cooldown/token-pressure decision allow a refresh or reuse?

## Internal Turn Contract Notes

Heartbeat and cron turns now share the same strict internal-turn contract.

- They still execute through `RuntimeAgentSession.prompt(...)` with their own internal source metadata.
- Service-layer code must not auto-retry tasks or synthesize fallback assistant replies on behalf of the model.
- An internal turn that ends with `HEARTBEAT_OK` may surface a live-only UI ACK event, but it does not become transcript history.
- Maintainers should distinguish live UI state such as inflight snapshots and internal ACK bubbles from durable assistant transcript messages.

## Repeated Tool Call Guard Notes

Execution-stage duplicate-call handling in `main/runtime/react_loop.py` now uses a soft-reject path instead of escalating directly to an engine failure.

- When the model repeats the same ordinary tool signature several consecutive times in the same node, the runtime records the repeated assistant tool call, appends an error tool message explaining that the call is duplicated, and lets the next model turn repair itself.
- This means a task should no longer fail immediately with `RuntimeError: repeated tool call detected: ...` only because the model repeated a non-control tool call such as `exec` with identical arguments.
- Read-only duplicate retrieval guidance (`content.open/search`, `filesystem.open/search`, `task_progress`, etc.) is still a separate guard path with its own repair messaging and escalation semantics.
- If a maintainer is debugging a "stuck on the same tool" report, first inspect the latest tool/error messages in the node transcript and runtime frame before assuming the task was terminated by the runtime itself.
