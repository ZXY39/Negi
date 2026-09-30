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

- 手动 pause 的语义是“冻结上一轮”，不是“等待下一条输入来补写原请求”：session 以 `completed` + `stop_reason=user_pause` 收尾，pause 当下的轮次上下文照常持久化，收尾时立即写 completed continuity sidecar，并把 paused assistant 气泡归档成带 `status=paused`、`history_visible=false`、`source=manual_pause_archive` 的 durable 记录。后续输入必须作为新一轮 user turn 发送，不得走 `resume(additional_context=...)`。被暂停回合的用户消息经续跑种子对账继承进下一轮模型上下文，即使暂停发生在任何 provider 请求发出之前；对账规则详见 `context-and-cache-troubleshooting.md`「Baseline 合同与恢复顺序」。残留的 paused 转录条目随下一个用户可见回合正常完成被对账退役一次；退役边界与反复注入风险详见 `context-and-cache-troubleshooting.md`「残留 paused / pending 转录条目」。
- 运行中补充的消息作为一批独立 user message 持久化，在同一轮下一次 `call_model` 前一起注入，不拼接成一条文本；可见用户顺序的权威是 `inflight_turn.user_messages` 与 `ceo.reply.final.user_messages`（兼容字段 `user_message` 只保留批内最后一条），`pending` user rows 只是 durability/continuity 记录。

手动暂停恢复规则、排队补充消息与 follow-up 消费的完整契约详见 `web-and-admin.md`「Manual Pause Resume Rule」与「Queued Follow-Ups」。

可以把它看成“一个会话的状态机 + turn 执行器”：名字看起来像简单 session 封装，实际上 user turn、heartbeat / cron internal turn、async dispatch 错误恢复、paused execution context 与 frontdoor stage / hydrated tool state 都在这里汇合。

### 3.3 静默回复（`silent` 工具）

静默由一个**工具调用**表达，而不是由回复文本来表达。模型在任意一轮（用户轮、heartbeat、cron）调用 `silent`，本轮就不向用户投递任何回复；参数 `reason`（必填）/ `subject` / `superseded_by` 同时是审计载荷和痕迹内容。这个工具是前门专属的常驻内置控制工具，曝光与终态语义详见 `tool-and-skill-system.md`「fixed builtin tools」。

为什么用工具而不用一个哨兵字符串：文本一条通道表达不了"这段留下但别发出去"这个意图。实盘扫 3,271 个 frontdoor 请求工件 / 160,675 条 assistant 消息，文本哨兵精确匹配成功 0 次，"正文 + 空行 + 哨兵"的失败形态 11 次且形态全同——模型想把正文留在上下文里，同时不想让它外发，而 `output == 哨兵` 这种"整串相等"的判据只能二选一。工具调用与正文彼此独立，两个意图才有各自的通道。

`silent` 是**回合终态**：批次里其余工具照常执行完，随后本轮直接收尾，不再回到模型要一句收尾话。前门其他工具都不具备这个性质（`submit_final_result` 只存在于节点侧），所以它是 `_graph_execute_tools` 里一条独立的 `finalize` 边。收尾正文取同一条助手消息里随工具一起给出的文本；模型只给工具不给正文时退回 `reason`。

归一化后的边界是"不投递，但保留正文"：外部渠道与 cron 都不外发（`RunResult.is_silent_reply=true`，`RunResult.output` 为空，channel transport 与 cron dispatch 据此闸门）、`message_end` 带 `text=正文` + `silent_reply` + `silent_reason`（外部 relay 依据 flag 跳过）、回合生命周期照常走完（`turn_end` / `agent_end` / `state_snapshot`、状态 `completed`），本回合已发布的阶段与工具调用保持完整可见。

轮末收口与普通可见回合共用同一个 `_complete_active_frontdoor_stage_state`，区别只在于**不传** `completed_stage_summary`：静默轮把当时的活动阶段置为 `completed`、写 `finished_at` 并清空 `active_stage_id`。这一步是阶段归属的分界，不是展示层的修饰——没收口的活动阶段会被下一轮继承（`frontdoor_stage_state` 跨回合不清空，新轮次继续往同一个 `stage_id` 里长轮），于是后面那条可见响应的轨道里出现一张归属上一个静默轮的卡。总结槽留空同样是契约的一部分：槽位由下一次 `submit_next_stage` 书写该阶段的真实结论，而"这轮为什么不出声"已经有完整去处（silent 那一轮的 `tools[].arguments.reason`，模型未给正文时静默行的可见正文本身就是 `reason`），写进总结槽等于用元理由顶掉结论并留下两份真相。`silent` 不需要活动阶段，所以无活动阶段的静默轮收口是 no-op，不产生幻影阶段；副作用是 `transition_required` 随收口归零，下一轮的闸门读数从"预算耗尽"变成"无活动阶段"，两条出口都要求 `submit_next_stage`。

transcript 落一条 assistant 行，带 `silent_reply=true`、`prompt_visible=true`、`ui_visible=true` 以及 `silent_reason` / `silent_subject` / `silent_superseded_by`，正文原样保留。`prompt_visible=true` 是这条车道的关键不变量：取消机器侧的静默闸门之后，"这一轮为什么没说话"的唯一记录者就是模型自己，而它要能反悔就必须看得见自己上次的选择。基线侧同样不重复回填该正文——带 `tool_calls` 的那行助手记录已经把它带进请求体基线，再 append 一次就是同文两份。因此静默回合的正文在请求体里只有一份，这一点由回归断言钉住。

两条历史压缩车道对这条行都豁免，否则痕迹活不下来（裸 tool_call 行实盘存活率约 6%）：阶段车道把它排除出可删集合、配对的 tool 结果行随之一起保留；token 车道是位置型切分，白名单无效，所以整组摘出待压缩区间、压缩完成后回插在摘要块与最近尾部之间（顺序单调，且 assistant 与 tool 结果必须同进同出，否则 provider 拒孤儿结果）。详见 `context-and-cache-troubleshooting.md`「Shrink 原因与压缩边界」。

**模型没给正文的可见回合同样按静默收尾**，机器不替它编一条回复：`_graph_finalize_turn` 里 `final_output` 经旧哨兵清洗后为空、且本轮不是心跳内部轮时，直接置 `silent_reply=true` 而不产出任何可见文本。这条替换掉的是历史遗留的英文兜底文案（"No visible reply was generated for: …"）——实盘 2026-09-23 23:25 一轮读文件数后模型选择沉默，那句内部文案被当正常回复投给了 QQ 用户。判据与工具静默共用同一个 flag，只在 `silent_reason` 上分两种："模型未给出可见正文" 与 "旧静默哨兵剥除后无正文"，后者顺带把"模型仍在沿用已删除的文本哨兵"变成可计数信号（成因是压缩摘要里可能残留旧契约措辞，见下）。心跳内部轮不走这条，它有自己的修复车道与上限文案。

可见回合从哪儿知道有这个出口：`_render_frontdoor_contract_summary` 在 `silent` 出现在 callable 名单时渲染一行 `silent_help:`，指路之外只说"静默没有文本写法"。心跳车道那份措辞（`heartbeat/session_service.py` 的 `_silent_tool_instruction`）只覆盖事件轮，覆盖不到普通用户回合，而工具名恒定出现在契约里并不等于模型知道该在什么时候用它。

**静默判据必须自带轮次作用域。** 心跳的 `This is a background heartbeat…` 行连同 `# Heartbeat Rules` 块按 append-only 规则长期留在请求体基线里，于是紧随其后的可见用户轮读到的是几条消息之前那段"本轮必须三选一收尾、可以调 `silent`"——该轮的第一句（"你正在处理内部事件，不是在处理新的用户输入"）在新用户轮里是一句错误陈述。因此规则块首句显式限定"只适用于本轮携带事件束的那一次唤醒"，前言同样声明只治理本轮；`ceo_frontdoor.md` 面向可见轮补一条正向判据：用户在当前轮提问或追问已交付物的内容时必须回答，此前"跑完等我问""别急着告诉我"这类要求只免除主动推送，用户一旦开口就不再适用。触发这条改动的实盘形状：2026-09-27 10:15 用户问「大体结论是什么」，模型不给任何可见正文只调 `silent`，而其 `reason` 通篇在论证"我应该基于任务结果给出摘要"、`subject` 与 `superseded_by` 双空，用户再发一个「？」后模型自己翻案。机器侧不设闸门（既定裁决：推不推由模型决定，机器只负责按时送到），所以这条边界只存在于措辞里，守卫见 `tests/resources/test_frontdoor_silent_tool_exposure.py`。写这段措辞时有一条硬约束：**规则文本里不得出现事件束标记本身**——稳定规则文本是按该标记 `partition` 出来的，把它写进规则会把后面的整段规则切掉（实盘踩过一次）。

**旧哨兵的字面串不得出现在任何逐字送达模型的面。** 删除识别（P4）只覆盖代码侧判据，指令本身还长在提示词里：`g3ku/runtime/prompts/ceo_frontdoor.md` 与 `heartbeat_rules.md` 曾继续要求"整条回复只输出哨兵"，于是每个新会话的 system prompt 都在教模型用一条已经没人认的写法——2026-09-24 00:34 一个新开网页会话照做并把机制解释给用户，就是这条。同一句话还可能从**长期记忆**注入（记忆条目每轮进 index 1），那属于操作员数据，走 `/api/memory/current/delete` 清理。回归守卫见 `tests/resources/test_frontdoor_silent_tool_exposure.py`：提示词目录与契约渲染器里出现该字面串即失败。代码注释与维护文档里保留这个词是刻意的——它们记录"为什么删"，不进上下文。

Web 侧没有"静默占位文案"这个概念：静默回合走与普通回合**同一条** `ceo.reply.final` 通道，带上正文、`silent_reply=true` 与 `silent_reason`，并照常携带 `source` / `turn_id` / `user_messages` / `usage` / canonical context 合并结果。前端把整条响应折成气泡外的一行「静默消息 HH:MM:SS」，点开露出原文与轨道（不再单独挂原因行：工具静默时正文本身就是 reason），UI 合同详见 `web-and-admin.md`「CEO Turn Silent Reply Contract」。新维护者最容易误读的一点：静默 final 一旦缺少 canonical context 又缺少正文，`finalizeCeoTurn` 会退到"无回合元素"兜底分支并 `discardPendingCeoTurns`，整条阶段轨道连同工具步骤一起被删掉——表现为"静默回合什么都没显示"，根因在 final 载荷字段不全，不在渲染层。会话列表 preview 在没有可见文本时保持原值（`update_ceo_session_after_turn` 对空 `preview_source` 不写回）。

内部轮的静默与用户轮同一种画法：模型调用 `silent` 结束的 heartbeat/cron 回合照常收到带 `silent_reply` 的 `ceo.reply.final`，前端据此画折叠「静默消息」行（没有 live 回合元素时按需补一个），渠道侧仍由同一个 flag 闸门不投递。`ceo.internal.ack` 只剩机器侧兜底一种情形——内部轮空输出且本轮不强制可见回复（模型没做静默决定），此时既没有 final 帧也没有转录痕迹，ack 是唯一的"已按时送到"证据。两条车道不得同时出声：过去 WS 与心跳唤醒层各发一条 ack、hub 排空不按类型过滤、前端也不按 `turn_id` 去重，同一轮会落两行。

## 4. frontdoor 与任务运行时的关系

G3KU 并不是所有问题都在 CEO 单次对话内完成。frontdoor 的职责更像是：

- 识别当前用户请求
- 组织提示词与上下文
- 判断当前这轮能直接回答，还是需要走任务运行时
- 在必要时触发任务工具，如 `create_async_task`

`create_async_task` / `task_append_notice` 的完整工具契约与守卫（重复预检与重验、`file_targets` reopen 车道、拒绝语义）详见 `tool-and-skill-system.md`「fixed builtin tools」。运行时记账与任务级控制合同要点如下：

- frontdoor dispatch 记账必须区分“工具调用发生了”与“新任务真的创建了”：只有显式成功形式才算已核实派发；拒绝消息里提到的旧 `task:...` id 不算新建任务。同一可见轮可以多次调用 `create_async_task`，是否真正创建由 `MainRuntimeService` 的唯一创建路径决定。

任务侧的合同——追加通知的子树级控制事务、分发状态机与 epoch 驱动器、验收节点交接、任务树深度上限、节点级暂停与恢复、优雅停机与启动自动恢复——归 `main-task-runtime.md`。本节只保留 frontdoor 侧的判定与记账。

对于异步任务的回传：任务终态通过 task terminal callback / heartbeat 回到原 CEO 会话；heartbeat 的修复/回退语义与 `terminal_output` / `root_output` 双车道详见 `heartbeat-system.md`「Task Terminal Repair Contract」。

当前 frontdoor 的上下文组织以阶段工作集为近场上下文：**未被点名的完成普通阶段与当前 active 阶段都保留完整原始窗口（含工具调用）**，没有条数上限；只有两条出口能让一条阶段离开可见层——模型在关闭它时点名裁撤（`context_evicted`），或压缩把它收口（`context_visible: false`）。离开之后按阶段归属原位移除工具调用，前者以 compact 块回插原位、后者连块都不出，阶段之外的用户可见对话原位保留（表示规则见本文「Runtime Contract Lane」）。阶段块（`[G3KU_STAGE_COMPACT_V1]` / `[G3KU_STAGE_EXTERNALIZED_V1]` / `[G3KU_STAGE_RAW_V1]`）以 system 角色落地：运行时标注的已完成阶段摘要属压缩元数据、非对话内容，assistant 角色会诱导模型把块当成"自己上一轮说的话"而在续写位置仿造/回显（角色合同与迁移期双角色识别见 `context-and-cache-troubleshooting.md`「压缩块的格式与字段语义」）。归档压缩阶段（`stage_kind="compression"`）是历史遗留表示数据：继续规范化与渲染为外置块，运行时不产生新的归档。已在 `token_compression` 边界收口的阶段（`context_visible: false`）不再渲染任何块，也不占 raw 保留名额（合同见本文「Runtime Contract Lane」与「Frontdoor Context Compression」）。全局语义摘要层不参与 prompt assembly；长会话的远场连续性由权威请求体基线、canonical context 链与压缩合同承担，收缩边界详见本文「Frontdoor Context Compression」。

前门提示词分成“静态协议层”和“动态注入层”两部分理解：

- `g3ku/runtime/prompts/ceo_frontdoor.md` 承载 CEO frontdoor 的稳定协议（角色规则、任务/工具通用约束、stage-first 高优先级协议）；稳定 system prompt 只保留最小的 capability exposure revision 锚点，不把可见 tool/skill 名单写进稳定前缀。
- `g3ku/runtime/frontdoor/prompt_builder.py` 负责把稳定协议与少量环境提示装成 base prompt；`g3ku/runtime/frontdoor/message_builder.py` 按本轮会话状态动态注入 retrieved context、memory hint 与当前轮运行时工具合同所需的数据。

### 用户消息时间锚点

- 转录持久化始终保持用户原文（RAG ingest、web UI 等消费方依赖 raw content），`message_builder._history_message` 在投影历史时把记录自带的 `timestamp` 渲染为 `[消息送达时间] <本地时间 +08:00 星期>` 行追加到用户消息投影副本末尾（渲染 helper 在 `g3ku/core/timefmt.py`；heartbeat/cron 内部消息不装饰，各自携带时间锚点，见 `heartbeat-system.md`「Internal-turn time anchors」）。装饰值派生自记录固定字段而非 now()，跨回合字节稳定，不打断 provider 前缀缓存；仅"当前用户不在历史、直接追加进请求"的兜底路径用 now() 盖章，下一回合投影自动切换回记录时间。硬性规则：所有基于内容相等性的比较（当前用户匹配 `_cron_prompt_equal`、暂停回合种子对账 `_reconcile_paused_user_turns_into_seed`）必须先 `strip_arrival_time_stamp` 剥离装饰再比较，否则已装饰投影与原文不相等，会被误判为新消息造成重复注入；新增任何"按用户消息文本去重/匹配"的逻辑都适用同一规则。

### `prompt_batch` 批次回合内容合并

- `prompt_batch` 只以批次最后一条输入驱动回合，`prepare_turn` 因此会把同批次其它输入的内容块（文本与附件，各自按其元数据展开，如 web 上传、外部 `image_url` 块）按到达顺序并入当前回合的模型请求内容，相等块去重——否则用户连续发送时较早消息的文本与图片会彻底缺席模型请求。较早输入的附件文件过期/缺失时，该输入降级为原文并入（图片缺席），不拖垮整批回合；当前回合输入仍保持严格失败语义。合并只发生在请求构建期：批次各输入本体不改写，转录行各按原文落盘；中途追加的 follow-up 在 prepare 之后才进入批次上下文（`call_model` 前消费边界），与该合并无重叠；心跳/cron 等内部回合不配置批次上下文，不参与合并。
- CEO/frontdoor 的生产执行面是自研步骤循环，入口到收尾固定经过 `prepare_turn -> call_model -> normalize_model_output -> review_tool_calls -> execute_tools -> finalize`。`call_model` 和 `execute_tools` 共用同一份 frontdoor runtime tool bundle；`submit_next_stage` 这类运行时注入的 stage protocol tool 必须同时对模型“可见”且在 `execute_tools` 里可真实执行，执行环节不得从 `state.tool_names` 重建第二套工具表。`normalize_model_output` 不因阶段预算耗尽拦截纯文本回复：文本收尾直接 finalize，由 finalize 关闭活动阶段；唯一的文本打回（阶段无实质工具轮时限一次）与派发后的 Reply naturally 尾注，详见 `tool-hydration-and-callable-chain.md`「阶段门控与 callable 收紧」。阶段门控与 mixed-batch 语义详见 `tool-and-skill-system.md`「四个概念必须分清」。
- 可见 `call_model` 轮次有专用的流式 assistant 文本 lane，`RuntimeAgentSession` 是其合并边界：流式块追加进 `latest_message`，`inflight_turn_snapshot().assistant_text` 保持最新，并以节流轻量事件代替整份 `state_snapshot` 重建；该 lane 只承载文本，工具进度 / stage trace / canonical context 仍走各自的低频事件/快照通道。可见发送的硬回退边界：首个可见流式文本块出现前允许 provider retry / API-key rotation / model-chain fallback，首个可见块之后同一发送必须停止透明回退——一个可见气泡由多个 provider/model attempt 拼接属于 runtime bug。内部不可见发送（如 `token_compression` helper）不得复用该可见回调路径。
- 内部运行时错误后的 async-dispatch 恢复遵守同一可见性规则：若 `create_async_task` 已成功且当前轮已有用户可见 assistant 文案，恢复必须保留该文案；通用回退文案仅适用于没有任何可见文本幸存的窄场景。
- 没有“有效阶段”（`active_stage_id` 为空，或当前阶段已 `transition_required=true`）时，CEO/frontdoor 的 agent-facing `frontdoor_runtime_tool_contract.callable_tool_names` 保留全量 callable 并把 `submit_next_stage` 置首，配合同批提交协议与执行期宽限兜底；execution / acceptance 节点同样不再收紧，`submit_next_stage` 置首。这些都只影响模型决策边界，不同步收紧 provider-facing `tools` schemas——前门继续发送当前路径上 RBAC-visible concrete tools 对应的 `provider_tool_names` bundle，仅在 membership 真正变化时刷新并保持已持久化顺序稳定，`token_compression` 所在 send 沿用压缩前已持久化的 bundle。阶段门控、候选/修复车道与 provider 工具面详见 `tool-hydration-and-callable-chain.md`「阶段门控与 callable 收紧」与「CEO Provider Tool Surface」。
- `g3ku/runtime/frontdoor/_ceo_create_agent_impl.py` 是 runner 入口，但前门主执行链以 `_graph_*` 节点为唯一权威路径。
- 对 CEO/frontdoor 主链路，每个请求携带两份运行时块、都在全部携带正文之后：回合内常量部分的 `frontdoor_runtime_tool_contract`，和排在最末位、每跳按阶段状态重写的 `frontdoor_runtime_stage_gate`（callable / hydrated / 活动阶段）。活状态单独成块的目的就是让重写只顶掉它自己那几百字符；锚在携带正文中间（例如最新 user 消息之前）会让它之后的整段同 turn 正文断前缀缓存。两份块都是 system 角色的 summary（运行时元数据、非对话内容）。块占据模型续写位置带来的回显风险，由下面的统一收口车道承担。它属于“当前轮临时合同”，不是 durable history（剥掉/重注规则详见 `context-and-cache-troubleshooting.md`「append-only 规则」）。`normalize_model_output` 把 standalone 内部消息回显统一收口：运行时工具合同回显与阶段压缩块回显（`stage_prompt_compaction.py` 的三个 `[G3KU_STAGE_*]` 前缀，守卫按文本前缀自行判定）首次各导入一次私有修复提示——合同回显禁止引用合同，阶段块回显要求改用 `submit_next_stage` 或面向用户自然语言；重复回显转为不含内部材料的用户友好回退，阶段块原文绝不作为最终回复持久化或投递。可见答案尾部的契约/阶段块片段的剥除与渠道投递边界 `sanitize_channel_outbound_text` 的两类回显剥除共用 `stage_prompt_compaction.ECHO_STRIP_ENABLED` 开关；开关处于关闭状态时，尾部片段随回复原样放行。裁剪按“任意位置子串匹配块前缀”实现，会把用户答案中合法引用的 `[G3KU_STAGE_*]` 前缀一并截断，因此该开关在替换为指纹比对实现（仅剥离与当前注入真块逐字一致的回显）并同步恢复对应回归测试断言（`test_ceo_frontdoor_regressions` 尾段剥离与 `test_session_keys` 出站消毒）之前保持关闭。standalone 内部消息回显的收口（私有修复提示 + 重复回显用户友好回退）不受该开关影响。维护上区分 `dynamic_appendix_messages`（下一次重建时应追加的最新合同）与活动中的 `messages` / actual request JSON。

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

维护上，动态 skill/tool 提示块里的说明不能覆盖 `ceo_frontdoor.md` 的 stage-first 协议。权威顺序是：无活动阶段时若需调工具，必须把 `submit_next_stage` 与目标工具同批提交（`submit_next_stage` 起手、普通工具紧随，普通工具记入新阶段第一轮）；单独只提 `submit_next_stage` 不带工具、或单独调普通工具，都只获一次宽限执行，随后被硬拦；动态暴露里的 `load_skill_context` / `load_tool_context` 提示排在活动阶段之后（提示口径），执行层闸门对上下文加载器恒定放行，loader 调用不撞闸、不消耗宽限。执行层豁免不改变这套协议口径：节点暂停（`task_node_error`）心跳轮由运行时自动补开 `system_generated` 阶段（免模型起手 `submit_next_stage`），`memory_write` / `memory_delete` / `memory_note` 三工具免活动阶段即可调用，上下文加载器无论阶段状态均可调用——详见 `tool-hydration-and-callable-chain.md`「阶段门控与 callable 收紧」。排查“普通工具撞上 no active stage”时，先检查稳定协议与 `stage_messages.py` 状态 overlay 是否一致，再检查 `prompt_builder.py` / `message_builder.py` 的动态提示是否与主协议竞争。`candidate_tools` / `candidate_skills` 两类候选的不对称语义详见 `tool-and-skill-system.md`「四个概念必须分清」。

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
- 重试循环在每个退避边界对比 runtime config revision：revision 变化时**不中止回合**，而是重新解析当前角色链并按新链重启重试（丢弃旧链已试集合、刷新 revision 基线、从新链链首重新评估）；链未真正变化则只刷新基线并沿用既有重试账本。跨重启次数由 `DEFAULT_MAX_CHAIN_CHANGE_RESTARTS` 累计封顶，只兜底链反复变化的病态抖动，正常一次改链只消耗一次。模型前进边界同样活解析模型链，但**含组的链只以 route plan 为真源**（`model_routes_supplier`）：刷新时按新 plan 重建组槽位表，保持 `refs` 与槽位「一槽一位」同长同序，并在每个组槽位上保留本请求已轮换到的成员（否则刷新会把槽位抹回配置首成员，那个成员往往正是刚被试过的）。候选展开的扁平数组不是 fallback 顺序，也不能当作链替换进去：`refs` 一旦长于槽位表，下标 ≥ 槽位数 的成员就查不到自己所属的组，会被当成 direct 模型按配置顺序取用，该组的 `maxRetryRounds` 预算、组内 least_load 选人与换人退避节拍一并失效，日志上表现为「这个组从来没被均衡过」。纯 direct 链仍由 `model_refs_resolver` 交回新链。两条车道都保证：运行中新加入的 fallback 模型在下一个模型边界可见，已试过的模型不回头重试。上层回合级重建见 `config-and-models.md`「模型链变更何时作用于在途回合」。
- 换 key（轮换）判据（`should_rotate_api_key_error`）：**内部运行时错误不换、请求体形状错误不换、`retry_on` 命中（判定可重试）不换**（改走本模型退避重试轮）；其余错误（未命中且非请求形状，如 401 坏 key、503）才换 key：**每个 key 各试一次（单趟轮换，「重试次数」不参与），轮完即前进到链上下一个模型**，都不可用时抛耗尽错误。请求形状错误跳过其余 key 直接前进下一模型。形状错误只按结构化 HTTP 状态判定（`LLMResponse.error_status` / SDK 异常 `status_code`）：status 可得时只有 400/422 算形状错误（换一把 key 修不了畸形 payload），其他状态（429 限流、401 等）与 status 不可得的错误一律不按形状错误处理——错误正文里的网关 type 字段（如 `invalid_request_error`）不是 400 专属标识，文本匹配会把 429 限流误判成形状错误。配置脚枪：把 401 之类配进 `retry_on` 会让坏 key 只重试不换 key（详见 `config-and-models.md`）。
- provider 终态错误（`finish_reason="error"`）由 frontdoor normalize 抛 `ModelProviderResponseError`（继承 `RuntimeError`，兼容既有 `except RuntimeError`），携带结构化 `code`/`status`/`kind`——来自 `LLMResponse.error_code`/`error_status`/`error_kind`，由 provider 从 SDK 异常提取。`session_agent` 错误分类器据此把真实 provider code（如 `insufficient_quota`）写进 `StructuredError.code` 与转录 metadata（含 `error_status`/`error_kind`），而非退化成 `legacy_session_error`，使上层能按 code/status **程序化分支**而不必 substring 匹配文案。完整错误原文（含 `code=/status=/body=`）始终保留在 message 里并透传到用户气泡、`.g3ku/errors/*.log` 与节点 pause remark，**不脱敏**。
- **没有任何可用输出的响应**在 frontdoor 模型调用节点内原地重放（同一请求体重发、退避封顶 10s），与节点车道同构：`empty_response_retry_count` 达到 `_PROVIDER_RETRY_LIMIT` 即抛 `ModelProviderExhaustedError`，由 `session_agent` 错误车道落成「这一轮处理失败：…」并写 `.g3ku/errors`，不把运行时内部文案当作助手回复落库或投递到渠道。判定分两条车道：`_is_empty_model_response` 管**正常收尾的空响应**（有 `reasoning_content` 即算非空，不重放）；`_is_unterminated_empty_response` 管**未正常终止的流**——上游在思考或生成中途关闭 SSE 时不携带 `finish_reason`，消费层把该事实单独记在 `LLMResponse.stream_incomplete`（`finish_reason` 仍归一化为 `stop`，既有消费者与节点车道不受影响），经 adapter 落到 `AIMessage.additional_kwargs`。后者只在「无正文且无工具调用且无错误信号」同时成立时才重放，因此省略 `finish_reason` 的非规范 provider、以及已经产出可用输出的回合都不会被误伤；无正文本身也保证没有可见增量流出过，与下一条不冲突。日志锚点：`stream diagnostics` 行的 `finish_reason_seen=0`（`outcome=completed` 只代表分片迭代自然结束，不代表模型说完了）。同一行的 `chunk_kinds=<kind>:<count>` 给出分片成分（缺项即 0）：chat 侧 kind 为 `text_delta` / `reasoning_delta` / `tool_call_delta` / `chunk` / `non_choice_chunk`，responses 侧为 `event:<名字>` / `data:<名字>` / `line`。`chunk_count` 很大而三类增量 kind 全缺 = 上游只滴不携带任何内容的长空转流：它的分片间隔远小于 idle-chunk 阈值，按间隙算的超时永不触发，只有单次响应总上限能截——这一族故障过去只能从 `chunk_count` 反推。
- Responses 流的失败收尾（`response.failed` / `error` 事件）必须带上上游给的原因：消费层从事件的 `error`、`response.error`、`response.incomplete_details` 里取 `message` / `type` / `code` / `reason` 拼进异常文案，节点错误栏与心跳通知因此不再是一句无信息的固定文案。已有正文流出的失败照旧走「返回部分内容 + `finish_reason="error"`」，未流出正文的照旧抛错。上游错误体可能整份回传 response 对象，所以文案只保留 `CODEX_FAILURE_DETAIL_LIMIT` 以内的摘要并标注原文长度，完整错误体由 provider 以 `Responses stream failure body:` 单独一行落到 worker 日志：先读节点文案，再按这一行取全量。
- 已经出现可见流式文本的发送不做透明重试/回退（一个可见气泡不得由多个 provider/model attempt 拼接）。

维护判断上要记住：

- “首 token”在本项目语义里是“首个 chunk 到达”，不是“首个文本 token”
- `request_timeout_seconds` 对上述 provider 表示“首 chunk / idle chunk 超时阈值”，不是“整次请求必须在 N 秒内完成”
- 节点因限流长时间停在模型等待（`await_marker=model.chat.await_response`）可能是退避重试在正常推进，先看日志里的 `Retryable model failure for <model_ref> (round N/预算)`，再看会话/节点重试 toast 是否处于 `retrying` 并携带 provider 错误与最新/下次重试时刻。toast 表达"正在重试（模型退避轮或同模型换 key 重发）"，其 `retry_count` 是 provider 实际请求次数；每个模型的重试受其轮数预算约束（绑定 `retry_count`，默认 10 轮），预算耗尽即前进下一模型、全链耗尽按 exhausted 冒泡，不是无限重试。重试结束或中止时状态必须清理。UI 合同见 `web-and-admin.md`「Model Retry Visibility UI Contract」

### 5.2 节点模型路由与准入绑定（维护者必须掌握）

`execution` / `inspection` 两条车道的模型选择由三层协作完成，边界是这条链路里最容易踩错的地方：

- `main/runtime/model_route.py`：把配置里的 route entry 解析成运行时 route plan（链、组、候选展开、能力视图、稳定签名）。只做解析。
- `main/runtime/model_load_balancer.py`：组内平级选择、配额桶观测、节点绑定与 lease。只回答「这个节点该绑哪个成员」。链上有组时，准入还依赖同一进程里的另一件事：写出 route plan 的那一步必须同时把组定义注册进 balancer，否则每个 `load_balance` entry 都以 `unknown_group` 被跳过、节点永远停在排队队列（落地契约见 `config-and-models.md`「配置热刷新」）。
- `main/runtime/node_turn_controller.py` + `main/runtime/chat_backend.py`：前者在**准入/排队**时执行首次选择，后者管理单次请求生命周期、重试与 fallback。

一次典型流程是：preflight 定下请求体量与是否含图像 → `acquire_turn()` 带着 route plan 入队 → pump 在同一把锁内向 balancer 要候选并拿到该成员某把 key 的 permit，把 `route_index / group_key / model_ref / key_index / permit` 记进 `NodeTurnLease` → `ConfigChatBackend` 的第一次 provider attempt **消费这颗 permit**（key 轮序按选中的 key 旋转，保证不会二次 acquire）→ 该成员的组内预算耗尽后先释放旧 lease，再由同一个 `NodeTurnLease` 就地重绑下一个候选；整组耗尽才前进到链上下一个 entry。

绑定的粒度是**节点**：一个节点在其生命周期内沿用同一成员，换人只有三条事实会触发——本次请求的过滤条件不再匹配（窗口/多模态）、绑定成员拿不出 permit、绑定成员被移出组（配置刷新按节点逐个摘除：减成员只解绑正用它的那些节点，加成员不动任何既有绑定）。**429 惩罚不在这三条里**：惩罚只决定一次新绑定选谁，从不拆掉已有绑定——被限流的成员让位走组内轮换那条既有车道（跑满该成员的 `maxRetryRounds` 才换人，见下一段），与旧模型链同一口径。把惩罚接进准入重绑换来的是"风暴期每个回合都重绑"：上游按网关整体打回 429 时组内每个成员都带着同样的惩罚，换成员得不到任何配额缓解，只把同一节点的上下文在成员之间来回搬。阶段推进不换人：绑定跨回合、跨阶段保留。节点落终态时清除绑定。逐回合按负载重选会把同一节点的上下文在成员之间来回搬，而换 `model_key` 等于换前缀缓存命名空间，断点之后的整段存活上下文都要重传（取证口径见 `context-and-cache-troubleshooting.md`「Family 与 key 合同」）。

负载打分由三部分组成：本地归一化在飞（running+waiting+reserved 比本地容量）、该配额桶最近 60 秒的请求启动数、按半衰期衰减的 429 惩罚。惩罚按**配额桶**聚合而不是按 binding：同一个 endpoint + 同一把 key（或同一个显式 `quotaPoolKey`）的多条绑定共享一份观测；解析不到密钥材料时每个成员各自记 `unresolved`，绝不互并。**429 是唯一的失败记忆**：其它失败（401/403、密钥被禁、5xx）一律当场交给链 fallback，不留任何跨请求状态——换节点后同样的请求可能就成功，而"这条配置坏了"是操作者按日志处理的问题。限流判据复用模型链自己的 `429` 关键字表（`g3ku/utils/retry_keywords.py`），负载均衡器不另起一套文本，否则同一个错误在两条车道上的归因会漂移。

新人常误读的四点：

- `models.roles.*` 与 `runtime_context.model_refs` 都是**候选展开视图**。含组时链上第一候选不是首选模型；真正用哪个只在 lease 里（`NodeTurnLease.selected_model_ref`）。诊断字段 `selected_model_key` / prompt cache 用的 `route:<signature>` 都从这里取。同样地，这份展开也不是链刷新可以替换进 `chat` 的链：一槽一位的对齐约束与后果见「Chat provider 超时与重试边界」。
- 「组 busy」只由事实推导：拿不到 permit、组里没有一个通过过滤的候选，或本请求已把组内成员全部试过。没有配 `singleApiKeyMaxConcurrency` 时本地容量恒为无限、`waiting` 恒为 0，用打分阈值造出来的 busy 是假的。
- 组内换成员之间**有**退避节拍（沿用同模型轮之间的封顶指数退避），跨 direct entry 前进仍然零等待。把组预算收缩当成「立刻换下一个」会把一次 pass 变成对同一分钟窗口的背靠背请求。
- 同一条角色链上还有两条**不过准入**的辅助车道：spawn 送审评审（`node_runner.py` 的评审调用）与异步任务重复预检（`runtime_service.py`）。它们把 `_acceptance_model_refs` / `_execution_model_refs`（展开视图）原样交给 chat，既不传 route plan 也不带 lease，于是三件事一起成立：预算取该成员目录的 `retryCount`（不是所在组的 `maxRetryRounds`）、前进序是摊平后的整池候选（链首永远先撞）、并且**不进均衡账**——`record_route_request_start` 与 `record_outcome` 都挂在 lease 上，所以这份真实消耗的配额对 60 秒滚动 RPM 与 429 惩罚不可见，组车道的打分因此会低估该链的实际负载。维持现状是量出来的结论：辅助车道一天个位数请求，而为它造一个轻量 lease 会把一次评审变成与节点回合抢准入槽的排队方，代价不对称。判据：`Retryable model failure … round x/N` 里 N 不等于任何在用组的 `maxRetryRounds`，这条请求就出自辅助车道，不是组车道出了错。

进程边界：balancer 与并发控制器只在 `execution_mode == 'worker'` 时存在。`embedded` / `web` 角色下选择层退化成无并发计数的直连绑定，不报错也不排队。多 worker 副本之间不共享这份内存态。

排障入口：日志锚点 `Model route selected` / `Model node binding rebound` / `Model route lease released` / `Model load-balance group exhausted`，每条都带决策时刻的负载读数与重绑原因；链追踪 `MODEL CHAIN: FALLBACK` 的 `next_model_ref` 在下一跳是组槽位时写 `group:<组名>` 而不是成员（成员此刻还没定），真实落点看紧随其后的那条 `Model node binding rebound`；跨进程读数看 worker 心跳里的 `model_route_groups`，管理面接口为 `GET /api/models/load-balance/status`。运行操作口径见 `operations-and-maintenance.md`。

## 6. 运行时里的状态与持久化

同步会话和任务运行时有两套不同的持久化关注点：

### 会话侧

- transcript / session messages
- paused execution context
- inflight turn snapshot
- frontdoor completed continuity sidecar（`frontdoor_request_body_messages` 基线、actual-request trace、阶段/规范化/压缩状态）
- 每轮边界快照 `.g3ku/web-ceo-turn-boundaries/<session>/<turn_id>.json.gz`（与 continuity sidecar 同一份载荷按 `_active_turn_id` upsert，轮末 finalize 写入即该轮终态；gzip、每会话保留最近 3 轮）。它是用户消息编辑重发/Fork 的唯一截断数据源：截断到某轮之前 = 读取该轮 prev_turn 的边界快照整体替换 continuity 状态；快照缺失的轮次不可截断（不做启发式重建）。失败轮拿不到 actual-request，但它同样按当前（= 上一成功轮的）基线写一份边界快照，否则一次失败会让紧随其后的那条用户消息失去截断资格；基线为空时不写（那等于把会话截成零上下文）。契约详情见 `web-and-admin.md`「Message Edit-Resend And Session Fork」
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

任务临时目录的根由 `MainRuntimeService._temp_root()` 解析：构造参数 `workspace_root` > 数据根（`g3ku/deployment/data_root.py`，默认等于进程 cwd，见 `operations-and-maintenance.md`「关键状态文件与目录」）。它与 `_workspace_root()` 是两条线：后者仍按 `resource_manager.workspace` 解释 skills/tools 等资源根，数据根换到别的盘不带动资源解析。该临时根既决定 `temp/tasks/` 的位置，也影响 `exec`/`filesystem` 类工具默认的 `task_temp_dir` 工作目录。需要掌握的两条维护约束：

- 任何不提供 workspace 的调用方（尤其是测试和一次性脚本）都会把任务目录落在数据根的 `temp/tasks/` 下；未配置数据根时那就是进程 cwd。测试通过构造参数 `workspace_root=tmp_path` 显式隔离，`tests/resources/conftest.py` 的 autouse fixture 再把数据根钉到 per-test 临时目录（`G3KU_DATA_DIR`）并替换 cwd 回退，保证测试运行既不向真实仓库写 `task_*` 目录，也不落到安装侧的存储。
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
- 执行节点与检验节点是“两层消息结构”：稳定 bootstrap user JSON 只负责任务定义与稳定节点上下文（不含 `execution_stage`）；请求尾部有三件运行时注入的东西，顺序固定为「稳定 `node_runtime_tool_contract` → 活 `node_runtime_stage_gate` → 当轮 turn-only note（user 角色，压末位）」。稳定块装当轮不变的 candidate/repair 名单，活块装这一跳真可调用什么、水合了什么、活动阶段是什么（每跳整份重写，所以只它能承担末位附近的重写代价）；两块都是 system 角色（运行时元数据、非对话内容）。行为口径与工具说明都不再抄进块里：前者在 `main/prompts/node_runtime_contract_shared.md`（由 `node_execution.md` / `acceptance_execution.md` 用 `{{> ...}}` include 进稳定 system 提示），后者在 provider `tools[]` 的 `function.description`。standalone 块回显（零工具调用、整条回复是块原文）由回显守卫收口成可恢复的 error pause，见本文「Node-Level Pause and Recovery」电路熔断清单。节点的 turn / repair overlay 同样遵守 append-only 边界：只能作为新的 request-tail 消息追加，不得回写已有 bootstrap 或持久化历史消息。
- 对节点运行时，`before_model` 当轮真正下发给模型的 schema 选择结果是权威工具来源；runtime frame、restore/recovery 和 runtime messages artifact 都从这份结果派生。`node_runtime_tool_contract` 是模型可见合同，但 runtime frame 才是 `candidate_skill_ids` / `candidate_skill_items` 的 canonical 恢复来源。
- 排查“节点为什么说没有 candidate skills”时，同时看 `contract_visible_skill_ids`（输入层可见性）与 `candidate_skill_ids`（selector 最终候选）；输入层为空时继续看 `skill_visibility_diagnostics`（registry 存在性 / role / policy effect）。首轮 `candidate_skill_ids=[]` 而 fresh contract 本应非空时，优先判断是否仍停留在 `initialize_task()` 的 bootstrap 空 frame。
- `task_node_detail` 的 summary 档执行轨迹摘要在 `stages` 之外还挂 `latest_tool_calls_full`：轨迹最后 5 步工具调用的完整入参（落盘 `arguments_text`）、状态（含运行中）与已结束调用的完整出参（按 `output_ref` 解析、单条超 8000 字符截断并置 `output_truncated`，`output_ref` 保留供按需再取）。它服务于卡点排查（agent-facing 工具 payload），与 Web 时间线渲染无关；round 级工具记录按既有存储契约落盘（小输出内联 `output_text`、大输出外置 `output_ref` + `output_preview_text`），该字段只做只读读取与按需解析。
- CEO/frontdoor 采用同样的分层思想：稳定会话前缀不承担当前轮 callable/candidate tool 状态，当前轮工具合同放在 dynamic appendix 并随 turn state 刷新，overlay 保持 append-only。prompt cache key 未变但命中下跌时，先检查是否有 overlay 被拼回已有 user 消息。
- 主运行时阶段账本同样携带展示文本：`record_execution_stage_round` / `record_execution_stage_free_pass_round` 把该轮工具调用同批响应里的模型叙述记入 round `text`（按 `_STAGE_ROUND_TEXT_CHAR_LIMIT` 截断），`submit_next_stage` 把 `completed_stage_summary` 记入完成的阶段（阶段摘要是模型自写的收尾内容，逐字保留：只折叠换行，不设字符上限）。两者经 `main/monitoring/execution_trace.py` 与两个 summary 装配器（`log_service` / `query_service`）进入 Web 节点详情 payload（full 与 summary 两级都保留），与 frontdoor 展示字段同属只服务 Web 时间线的展示数据（frontdoor 侧规则见本文「CEO Frontdoor Round Tool Ownership」），不得喂给 prompt 组装或转录权威链；渲染合同见 `web-and-admin.md`「CEO Stage Trace Round Rendering Contract」。

#### 投影表的列只承担主键与索引

- `task_node_details` 只留 `node_id / task_id / updated_at / payload_json`，`task_node_tool_results` 只留三个主键列 + `order_index`（被索引）+ `payload_json`；其余平铺列（`input_text / output_preview_text / arguments_text / tool_name / status / *_ref` 等）在建表后就不存在，因为全部读点一律 `SELECT payload_json`，平铺列零读者。`payload` 嵌套字典只允许放**没有平铺字段承载**的键（明细表的 `goal / parent_node_id / depth / node_kind / status / execution_trace_summary / execution_trace / actual_request_* / token_usage* / direct_child_results / spawn_review_rounds / tool_file_changes`，工具结果表的 `parsed_payload`）——与平铺字段同名的键一律不许再写进 payload，那是重复计数。存量库首次打开由 `SQLiteTaskStore._drop_legacy_columns` 做**一次**表重建收掉旧列：新表定义从 `PRAGMA table_info` 反向合成（含合成 `PRIMARY KEY`，否则工具结果表的复合 `ON CONFLICT` 失效），`INSERT..SELECT` 拷数据后改名，索引段在其后重建。绝不允许改回逐列 `ALTER TABLE DROP COLUMN`——SQLite 每删一列整表重写一次，实测 11 列把启动阻塞近 3 分钟（端口无人监听）、WAL 冲到 2.7 GB。重建失败只记 WARNING 不阻断启动，写侧按实际列集取值，该表维持旧形状。护栏测试 `tests/resources/test_task_node_detail_single_copy.py`。
- **节点当轮正文只有一个家**：正文写在 `nodes.payload_json.input`（作者唯一：`log_service.update_node_input`，由 `react_loop` 每轮调模型前与每批工具后触发），明细投影**不再抄一份**。读侧 `query_service.get_node_detail` 按 `payload → 明细平铺字段 → 运行节点` 的优先级取值，所以带正文的旧明细行第一次被读时会重投影自愈、第二份副本自行消失；该函数本来就已加载 `runtime_node`，不额外增加查询。取值式统一是 `payload.get(k) or record.<flat>`，新旧形状都能读、缺键静默回退——这条优先级既是"摘掉重复键不改行为"的前提，也是新增投影时的约束方向。`node_runner` 取子节点摘要走 `getattr(detail, 'output_text')` 直读平铺，不经 payload。仍然重复的是 `output / check_result / final_output`（`nodes` 与明细表各一份）；`nodes.payload_json` 不在任何裁剪清单里，见「终态大行裁剪」条。

### 磁盘写保护与治理（main/ 侧持久化契约）

main/ 侧所有持久化写在磁盘满（ENOSPC / SQLITE_FULL）条件下的行为由 `main/storage/disk_guard.py` 统一约束。该模块拥有 `DiskFullError`（`OSError` 子类，既有 `except OSError` 语义兼容）、`is_disk_full_error` / `classify_write_error` 分类器，以及 `DiskPolicies` 进程级策略单例——由 `runtime_service` 构造时从 `config.main_runtime.disk_guard` 注入（字段契约见 `config-and-models.md`「main_runtime」），`G3KU_*` 环境变量仅作测试与应急覆盖。

契约按写入类别分层：

- **写咽喉只分类不吞错**：全部 sqlite 写经 `SQLiteTaskStore._run_write`；磁盘满异常统一分类为 `DiskFullError` 后照常上抛，是否降级由调用点决定。writer 线程按类别累计失败计数，经 `write_failure_counts()` / `runtime_metrics_snapshot()` 暴露（`write_failure_disk_full` / `write_failure_other`），是判断"系统是否经历磁盘满"的第一指标。计数不只在本地：心跳线程把 `sqlite_write_failures` / `event_write_failures` 一并写入 `worker_leases` / `worker_status` 的 debug 块，事件写失败（`TaskLogService.append_task_event` 与 live.patch 快照冲刷路径）另有 300s 限流 WARNING `task_events write failure (rate-limited): total=…`——静默降级可观测但告警不刷屏。排障顺序见 `operations-and-maintenance.md`「磁盘满」。
- **关键写永远尝试**：任务/节点状态、pause 行、error_log 不做预检、失败靠调用点兜底。
- **可降级写先过应急写预算**：actual-request artifact、`task.live.patch` 单份快照、execution trace 外置在写前调用 `has_emergency_disk_budget`（剩余空间 < max(`emergency_min_bytes`, 盘总量 × `emergency_min_ratio`) 即跳过落盘，退回 slim/minimal 形态）。预检带 5s TTL 缓存；探测失败保守放行。live.patch 快照被跳过时不写文件，下一个补丁覆盖写自然补齐（覆盖写自愈）。剩余空间的采样锚点是数据根、store 父目录与 artifact 目录，`disk_waterline_snapshot` 取其中余量最小的盘——把数据根换到大容量盘即按新盘判定，安装根不再进采样列表（它只放代码与密钥，磁盘紧张不该由它触发任务暂停）。
- **error pause 记录是 best-effort**：`NodeRunner` 异常路径的 error_log 与 pause 两个写点各自独立 try/except（`_persist_error_and_pause_best_effort`），任一失败都不阻断 `NodePausedError` 传播——控制流不依赖 pause 落盘，磁盘满只降级可见性、绝不放大为连锁节点暂停；未落盘的错误文本进入有界内存队列（deque maxlen=64）并留 warning 日志。`TaskActorService` 的 `NodePausedError` / `TaskPausedError` 分支里的二次 pause 状态写同样包死。
- **观测旁路的成本约束**：工具看门狗每 `poll_interval_seconds`（默认 5s）取一次运行快照供告警与 handoff 使用。这条旁路有三条硬约束：① 快照构建跑在工作线程（`asyncio.to_thread`），不在事件循环上——生产规模单任务实测整份详情 p50 635ms、首次 1095ms，串在循环里等于每 5 秒一次的分钟级滞后放大；② 看门狗用的快照不带 `recent_model_calls`（`model_call_limit=0`）——`g3ku.runtime.tool_watchdog.summarize_runtime_snapshot` 只读 `task` / `root_node` / `frontier`，而那份账本是任务全量历史（实盘单任务 1545 行 / 1.43 MB，随调用数线性增长），建完即丢；③ 同一任务的并发拍次合流成一次构建（`MainRuntimeService._tool_watchdog_snapshot_flights` + `asyncio.shield`）——快照只按 `task_id` 取值，等待者要的是同一份东西；各建一份时生产规模副本实测单份 250–360ms / 30MB 分配，10 份并发同时驻留 193MB 且 wall 3.5s。掐掉某个等待者不会连带掐掉别人正在等的同一份构建。REST 的任务详情接口不受影响，仍带全量账本、不经合流。
- **live patch 单份快照覆盖写**：`task.live.patch` 不落 `task_events` 行——经窗口聚合（`live_patch_persist_window_ms`，终态/暂停立即冲刷）后覆盖写 `event-history/<safe_task_id>/latest.json.gz`（tmp+replace 原子写，读者永远看到完整快照；`SQLiteTaskStore.write_task_live_snapshot`）。写失败不重试不重入队，等下一个补丁覆盖即自愈；磁盘记账按新旧文件字节差修正。每任务只保留这一份最新最完整快照，SSE 实时推送走内存 pub/sub 与落盘无关。推送载荷在窗口缓冲里**按引用保存**（不再整包深拷贝）：它由 `_publish_task_live_patch_locked` 现装、两个消费者只做序列化，因此"最新覆盖"的语义靠覆盖写本身保证，不靠拷贝。

#### 帧写与推送不是一对一

- `TaskLogService.update_frame` 先算内容指纹，指纹与库中现有帧一致时既不写库也不推 `task.live.patch`；`NodeRunner._await_with_runtime_marker` 退出 await 时清空 `await_marker` 只落帧行、不推（下一次帧写自然带上清好的状态）。所以"少一次推送"不等于"状态没变没落库"——帧行的 `updated_at` 仍随写入前进，`stale` 活性判定与 `frame_liveness` 阈值不受影响。徽标依赖的 `model.chat.dispatch` / `model.chat.await_response` 由 `ReactLoop._set_model_await_marker` 写并推送，不在这条降级范围内。四个帧写入口（`update_frame` / `upsert_frame` / `remove_frame` / `replace_runtime_frames`）**不返回 runtime state**：它们内部无读者，代价是按节点数水合全部帧正文（实盘 135 帧实测 233ms/次），要读状态请显式调 `read_runtime_state`。
- **task_events 只记低噪审计**：`task_events` 表只接收低频生命周期/会话级事件——`task.terminal`、`task.intermediates.cleaned`、`runtime.disk_emergency`、`runtime.task_wiped`、`task.artifact.applied`。高频事件（`task.model.call` / `task.node.patch` / `task.summary.patch` / `task.artifact.added`）不落该表：各自权威持久化在 `task_model_calls` / `nodes`·`task_nodes` / `tasks` / `artifacts` 表，事件行全库无生产读者（`list_task_events` 无调用方；WS 断线重连走详情+整树快照重拉，不回放事件行）。实时推送仍走内存 `_dispatch_live_event_locked`，不受落库策略影响。新增事件写入前先确认它的读者——只写不读的簿记会以周为单位重新撑大库。
- **读模型重建与整任务节点遍历按节点流式取行**：三处只用一遍节点的循环都走 `SQLiteTaskStore.iter_nodes(task_id)`，不用 `list_nodes(task_id)` 一次性把整任务的 `NodeRecord` 建出来——`TaskLogService.sync_task_read_models`（启动恢复 `_recover_interrupted_task` 与任务级重建）、`TaskLogService.sweep_residual_nodes`（worker 启动自愈，对每个终态任务把残余 `in_progress` 节点落成 `failed`）、`TaskQueryService._build_tree_snapshot` 里只为少数 `missing_pending_ids` 取 runtime 节点的那一趟（留在结果里的只有命中的几个）。生产规模副本实测：284 节点整批解析驻留 88.5MB、逐条 45.9MB；`sync_task_read_models` 全程 5046ms/53.6MB 对旧写法 5569ms/89.1MB。这条链在实盘分配探针的调用链榜上是头名（`startup → _recover_interrupted_task → sync_task_read_models → list_nodes`）。**行文本仍一次取回**：store 的读连接是共享的，不能跨锁挂着游标逐行 fetch，所以省的是解析出的对象，不是那次 SELECT。
- **artifact 大小治理**：`TaskArtifactRecord` 携带 `size_bytes` / `content_encoding`（`plain`|`gzip`）/ `content_hash`（均带默认值，payload_json 序列化零迁移）。**单例正文（`task_runtime_messages` 等按 `(task, node, kind)` 唯一）的查重按 `(task_id, node_id)` 取行**（`SQLiteTaskStore.list_artifacts_for_node`，走 `idx_artifacts_node_id`）——这条查重跑在每次帧写里（`_summarize_content` 外置正文 → `_runtime_frame_record`），按整任务取行会把成本抬成 O(节点数×artifact 行数)（实盘单任务 741 行 / 0.65 MB，对比每节点 3 行）。内容超过 `artifact_gzip_threshold_bytes`（默认 1 MiB）即在同目录以 `.gz` 后缀 gzip+tmp+原子改名落盘（与 live.patch 单份快照同范式）。actual-request（`task_actual_request`）每 (task, node) 只保留最新一份——新快照创建后即删同节点旧快照（文件+DB 行+内存索引），历史请求序列不留存（排障只看最新一轮）。文本去重走 `content_hash` 快路径不回读文件；无 hash 的旧行回读解压兜底比对。**读取一律走 `artifact_store.read_artifact_text`**（rest 的 artifact full 读取、navigation 的 canonical 与 `_resolve` 分支、`apply_patch_artifact` 四个接入点）——绕过它直接 `read_text` 会把 gzip artifact 显示成 "[二进制文件]" 或乱码。artifact 写失败抛分类后的 `DiskFullError`，绝不返回指向不存在文件的记录（读端 `.exists()` 兜底会把这种记录伪装成空内容）。
- **终态即清确定不再使用的数据，任务临时目录默认保留**：任务迁移到 `success`/`failed` 时，终态监听器 `_cleanup_terminal_task_intermediates` 同步只做判定与 in-flight 去重，重活甩 daemon 后台线程：按保留清单删中间 artifact 的文件与 DB 行，并整目录删除 `event-history/<task>/`（live.patch 单份快照只服务 SSE 与任务树恢复，终态后无运行时读者；任务树恢复帧 `task_runtime_frames` 在终态转换时已清零）。**`temp/tasks/<id>` 草稿目录（含空壳）终态后默认原样保留**——仅当 `main_runtime.disk_guard.terminal_temp_dir_cleanup_enabled`（环境变量 `G3KU_TERMINAL_TEMP_DIR_CLEANUP_ENABLED=1`）开启时才随终态清理硬删。保留清单（唯一权威）：`kind=='patch'`、`kind=='final_output'`、`task.final_output_ref` 指向的 artifact、标题含 `report`/`summary`；其余（`task_actual_request` / `task_runtime_messages` / `task_execution_trace` / `node_output` / `tool_result*` 等）全部删除。`task_error_logs` 表、节点 `blocking_reason`、`task_events` 行一律不动；孤儿 event-history 目录由删除台账 sweep 清扫。磁盘治理没有任何自动删除任务的路径——任务只随用户手动删除（Web/REST）或模型删除工具彻底清除（同走 `delete_task` 全删链路，先导出产出）：任务临时目录按双路径兜底回收（runtime_meta 记录的实际路径优先，确定性默认路径兜底，与终态开关无关），硬删统一走 `fs_utils.remove_tree`——失败条目去只读位重试、Windows 下经扩展长度前缀删除超过 MAX_PATH 的深树（git 克隆的只读文件与超长路径不会造成静默残留），仍有残留时 loguru 告警，绝不静默。清理量记 loguru 日志并发 best-effort `task.intermediates.cleaned` 事件；读端对已删 artifact 由 `.exists()` / `read_artifact_text` 兜底，不炸。维护者常见误读：把 `temp/tasks/` 当"任务结束即回收"的 scratch 空间——它是保留目录，正式产出落入其中不会因终态清理丢失，但也因此不参与自动回收，需靠 `scripts/cleanup_orphan_task_temp_dirs.py`、显式开关，或页面上的手动清除（下条）治理；`_effective_task_temp_dir` 把等于 temp 根目录的 meta 兜底值视为未配置（真实任务创建时写入的一定是每任务子目录），防止对账/删除作用到整个 temp 根——但**它不挡 `temp/tasks` 这一级**（meta 可以合法地指向它），所以任何按该函数取路径做硬删的入口都必须自己排除 temp 根与 temp/tasks 根。

#### 手动清除任务临时文件（不删任务）

- `MainRuntimeService.clear_task_temp_files` / `POST /api/tasks/{task_id}/clear-temp` 只删该任务的临时目录，任务行、节点、明细、artifacts 与 event-history 一律保留，因此卡片不消失、历史照常可回放。门槛严格等于终态：`status ∈ {success, failed}`；`in_progress`（含 `is_paused` 的暂停态，仍可能被 resume）一律 `task_not_terminal` 拒绝——该目录是 exec / filesystem 工具的默认落点。与删除链路同口径取双路径（runtime_meta 实际路径 + 确定性默认路径），逐个 `remove_tree` 后立刻 `_reconcile_task_disk_usage` 重算占用，返回 `removed_dirs / failed_dirs / freed_bytes`；部分残留通过 `failed_dirs` 明示，不静默。刻意不落 `task_events` 行（该表只收有读者的低频审计）。

水位监控与紧急态（P1）在同一契约下运转：

- **采样**：`WorkerPressureMonitor` 每拍（1s）经 disk_guard 的 TTL 缓存读工作区/存储盘的 `(free, total)`，随 snapshot 以 `machine_disk_free_bytes / machine_disk_usage_percent / disk_emergency_active` 下发到 `worker_status_payload`；前端任务大厅性能条的「CPU/内存/磁盘」项把剩余空间并进磁盘段渲染（`0%(剩余10.1G)`，紧急=critical 着色），不单列「磁盘剩余」项；紧急态另渲染全局横幅。
- **唯一水位线是紧急线**：`max(emergency_min_bytes, total×emergency_min_ratio)`，判定带防抖（进入需连续 `emergency_streak_samples` 拍、解除需连续 `emergency_recovery_samples` 拍）。磁盘治理不含任何触发删除的水位线——磁盘紧张只暂停与限流，任务删除永远是手动动作（大小可视化与排序引导见 `web-and-admin.md`「任务大厅大小与排序」）。
- **紧急态硬闸的语义是"排队等待"而非拒绝**：controller 的 `set_disk_emergency(True)` 把 `target_limit` 置 0，新工具调用在预算队列等待（模型不会收到工具级错误）；`disk_emergency` 是独立于 `pressure_state` 的布尔硬闸——压力决策链（critical/throttle/ease）与 dwell/starvation 逃逸阀在紧急态整体冻结，`_reset_idle_locked` 三处调用点带守卫，任何路径都不得把 limit 从 0 抬起。

#### 节点回合闸（执行器存在的成本也要闸）

- `AdaptiveToolBudgetController` 除工具槽外还带一份**按角色的节点回合闸**（`acquire_entry_slot / release_entry_slot / entry_snapshot / set_entry_targets`；快照字段 `entry_gate_running / entry_gate_limit / entry_gate_queued / entry_gate_ceiling / entry_gate_targets`，`entry_snapshot()` 另给每角色 `ceiling`）。闸位由 `WorkerPressureMonitor` 每拍发布的目标驱动，**不再参与工具压力状态迁移**：`normal / easing / throttled / critical` 只作用于工具槽，回合闸只认磁盘紧急这一把硬闸（`set_disk_emergency(True)` → 0）。理由与实测：工具槽量的是"一次工具调用"的等待，回合闸量的是"同时在物化上下文的执行器"，成本量级差两个数量级（一个槽≈几十毫秒 vs 一份上下文物化≈70MB 与秒级 CPU）；把两者绑在一起时，一次 141 秒的 SQLite 读让 budget 落 critical，双闸被踩成 1/1 约 9 分钟，而那时内存与上游都还空着（09-30 17:06:15 实盘）。
- **目标 = 最紧那一根轴的比例 × 当前闸位**，比例直接复用压力状态已有的 warn 线，不为回合闸另起阈值：`比例 = warn / max(实测, warn/2)`，安静时封顶 2.0、贴到 warn 线是 1.0（保持当前节点数）、越过 warn 线低于 1。四根"自家积压"轴＝事件循环 lag（warn 250ms / critical 1500ms）、写入队列深度（50 / 100）、SQLite 写等待（200ms / 250ms）、SQLite 读延迟（150ms / 250ms）。
- **warn 只保持，critical 才下穿地板**：越过 warn 线时比例被夹回 1.0 ⇒ 目标＝当前 ⇒ 保持现有节点数；只有任一轴越过 **critical** 线（或 429 惩罚到 p99 档）才让比例真正小于 1，逐拍一格往下削，允许低于配置地板（最低 1）。没有这层夹住时，warn 级落后（300ms 对 250ms 线）会被一路削到 1 并钉死——几百个节点排队时"循环落后 0.3 秒"是常态而不是紧急，削到 1 是塌方而不是控制（离线回放实测）。
- **增长判据是边际吞吐，不是"闸卡住需求"**：抬一格的前提是这一拍的"每分钟实际发起的模型请求数"（`ModelLoadBalancer.rate_pressure()` 的 `rolling_rpm_60s_sum`，90 s 窗口均值）比上一次抬闸那一刻高出 `_ENTRY_MIN_THROUGHPUT_GAIN`（0.5 次/分，≈基线 7 次/分的 7%）；换不来吞吐就退回地板为止（只有越过 critical 线才允许下穿地板：地板是操作员的最低意图，“这一拍没涨”不该由它背锅）（山脊搜索，停在有效的那一档，而不是停在最高档）。退让不清空吞吐基准，但基准过了 `_ENTRY_PROBE_AFTER_SECONDS`（180 s＝4 个节拍）会再放行一次试探，免得负载自己降下来之后永远抬不起来。只用"闸卡住需求"会恒真：实盘把在飞从 8 抬到 46，完成频率只从 6.93 涨到 7.23 次/分（+4%），单次调用 p50 却从 38.6 s 涨到 138 s——多出来的格子没换来产出，只把并发变成了自家事件循环上的排队（按 Little 定律 7.2/min × 138 s ≈ 17 个真在等模型，其余近 30 个占着闸在做上下文物化）。
- **第五根轴与吞吐读数都来自 `ModelLoadBalancer.rate_pressure()`**：一次调用聚合出跨成员的最大衰减 429 惩罚（60 s 半衰，对齐上游的分钟窗口）、最大滚动 RPM，以及合计滚动 RPM＝本进程每分钟实际发起的模型请求数（增长判据用它）。同一把 key 挂在多个组里时按 `model_key` 去重再相加，否则合计会翻倍。监控每拍调一次，所以这里只取聚合数，不重建给人看的完整 snapshot。限流分档按实测分布定（09-30 全天 6255 条路由观测：penalty p50=0 / p90=0.18 / p99=4.16 / max=13.57）：越过 1.0 只保持、越过 4.0 才开始缓减。429 不砍在飞回合，只停止新增——它的代价是延迟而不是丢产出（全天 607 次 429 全部在 ≤10 轮重试内恢复，`round 10/10` 出现 0 次）。
- **内存余量只限制增长、不下压地板**：可用字节减去 warn 线以下的保留量（`machine_memory_warn_percent`，默认 88% ⇒ 保留 12%）得到还能用的量，除以一格成本得到还能塞几格，再加回已在飞的那几格（available 已经把在飞占的内存扣掉了）。一格成本取 10 分钟窗口内各估计的**最大值**，两种估计都算：跨度回归 `ΔRSS / Δ在飞格数`，以及相对空载基线的平均成本 `(RSS - 空载最小 RSS) / 在飞格数`；出格（<8 MB 或 >2 GB）的估计丢弃，全无估计用兜底 110 MB（实测 9 格 RSS 1.04 GB、凌晨 27 格 Private 2.4 GB ⇒ 89–115 MB/格）。必须取最大：实盘 21:46 起在飞恒 46-48、RSS 恒 2.0-2.1 GB，跨度回归只算出 9-29 MB/格，内存轴于是反过来抬高了上限、RSS 一路涨到 2.4 GB。**读不到内存读数时不许越过地板**：v1 正是在"内存读数滞后/不可用"的状态下 90 秒爬到 27 格、把 Private 推到 2.4 GB、机器 94.8% 落进 critical，随后在 1↔27 之间振荡；同一批 140 节点按循环积压放大时稳定在 `limit=8 / queued≈120 / Private≈1.1GB`。
- **节拍（beat）取代一拍一格**：一次移动之后要隔满 `_ENTRY_BEAT_SECONDS`（45 s，实测一次模型调用 p50 37 s）才允许再动；一个节拍内上行最多 +2 格、下行最多 −1 格，其余拍一律保持。按拍动会抖振：21:19 重启后按 1 s 一拍动时，闸位在 1↔38 之间来回（38 格→1 格只用了一次性能采样的 15 s），lag 同期常态 1.1–7.9 s，抖振本身就是事故。
- **复位与配置刷新**：闸口排空（`running=0` 且无等待者）时闸位收回地板，下一次风暴从地板按目标重爬，不继承上次的高水位（21:31 无闸事故的形态）。`main_runtime.node_dispatch_concurrency` 的数被调小 ⇒ 当场钳到新值；同值刷新 ⇒ 保留已爬到的位置。
- **判读**：`dispatch_limits`（任务大厅、`runtime_summary`、perf 历史）报的是**活闸位**而不是配置地板——只报地板时闸在动这件事在界面上读不出来。每拍进 `perf_samples` 的读数有：目标、落到的闸位、在飞总格数、一格成本与空载基线 RSS、worker RSS、机器内存 available/total、429 惩罚峰值、RPM 峰值与合计、这一拍的吞吐 rpm、上一次抬闸那一刻的 rpm、距上次移动多少秒、本进程占了几核。有了这些才能直接回答"节点数为什么停在这个数"和"这次抬闸有没有换来吞吐"。`limit == ceiling` 且 `queued > 0` ＝有轴或吞吐判据在拦；`entry_rpm_at_last_growth` 长期追不上去＝加并发已经换不来产出。
- 闸口位置不变：`TaskNodeDispatcher._run_entry` 调 `run_node` **之前**——每个执行器在拿到任何槽位之前就会把自己那份上下文物化出来，只拧工具/模型槽位拦不住这份"存在成本"；`_DispatchLease` 的"嵌套等待时释放、回来重取"语义对这份闸同样生效，所以等子节点的节点不占闸位。另有一层二值急停：`_node_turn_gate_allowed` 只认 `local_pressure_state == critical`（lag≥1500ms 且排队仍在增长 / 写入队列≥100 / SQLite 写≥250ms / 读≥250ms）拒绝新准入，采样过期则放行——比例缓减是调节，这一条是急停，两层并存。
- **紧急态自动暂停防死锁**：进入紧急态的边沿钩子（monitor 采样线程 → `call_soon_threadsafe` 回事件循环）对全部 `in_progress` 任务执行 `force_pause_task_durably`，随后 `controller.abort_task_waiters(task_id, TaskPausedError)` 唤醒该任务排队中的 acquire future——异常沿既有 pause 流转冒泡（acquire 在 `_run_call` 的工具 try 块之外，不会被误包装成工具级错误）。竞态封口：acquire 成功返回后补一次 `_check_pause_or_cancel`（失败归还槽）；`_check_pause_or_cancel` 的 pause 分支与 `pause_task` 成功路径同样调用 abort。web/worker 双进程各自检测、各自 pause，`force_pause_task_durably` 幂等，DB 是唯一真源。
- **解除紧急态不自动恢复任务**：空间回到紧急线之上只清硬闸与告警，被暂停的任务保持 `paused` 等手动 resume（避免水位反复抖动造成任务震荡）。
- **任务大小增量记账**：`task_disk_usage(task_id, total_bytes, updated_at)` 小表，口径 = 目录文件（files/artifacts/event-history/temp）+ 数据库明细字节（五张大行表 `SUM(LENGTH(payload_json))`，`sum_task_detail_bytes`）。目录写入在写入点 bump 字节数（artifact 落盘、live.patch 单份快照与 singleton 覆盖写按新旧 size 差值记账），DB 字节只在对账时整体刷新。对账 loop（embedded/worker 模式，每小时）只对 `in_progress` 任务跑目录实测 + DB 明细求和覆盖增量值，终态任务在终态清理时对账一次——已终态任务目录不再变化，不进小时级扫描（禁止高频全量遍历）。DB 字节因此存在 ≤1h 展示延迟（列表契约同）。列表端 `TaskListItem.disk_usage_bytes` 从该表批量读取。两个读数陷阱：`LENGTH()` 对 TEXT 返回**字符数**，CJK 正文按 UTF-8 落盘要再乘约 1.3；该值只计每段正文的一份，同一段内容在库内被重复存几次它就少算几次，所以它是"这个任务有多少内容"的排序依据，不是"删它能腾出多少字节"的估计量。
- **任务删除全量清除**：用户 `delete_task`（Web/REST）与模型删除工具共用 `_wipe_task_data` 核心，步骤顺序是契约——S0 路径快照（`_effective_task_temp_dir` 读 runtime_meta，必须在删 DB 前取值）→ S1 产出导出 → S2 写删除台账 → S3 删文件（artifacts / files / event-history / temp 双路径，逐项容错）→ S4 删 DB 行（全部任务作用域表，含 `task_disk_usage` 与 `heartbeat_node_retry_state`）→ S5 governance 审批行（`exec_command_approvals` 按 `context_id=task_id` 删，含命令明文）→ S6 台账 wiped 标记 + `task.deleted` 推送 + 会话级 `runtime.task_wiped` 审计事件（task_id=None）→ S7 进程内缓存清理（summary 字典、inflight 集合、log_service `discard_task_caches`、registry 订阅）。任务数据不存在无条件永久保留的类别。
- **无自动任务删除**：磁盘紧张不触发任何任务删除；任务全删仅两条入口——用户手动删除与模型删除工具（`task_delete_cn`，preview→confirm 双步），均走 `_wipe_task_data` 且删除前导出产出。磁盘紧张时的空间回收依靠：终态即清确定不再使用的数据（上一条）、任务临时目录的手动清除（上两条）、`detail_retention_days` 可选明细裁剪（下一条）、任务大小可视化与排序引导的手动删除（`web-and-admin.md`「任务大厅大小与排序」）。
- **产出导出（deliverables）**：删除前按终态保留清单判据（与终态清理同一权威）把命中 artifact 复制到 `.g3ku/main-runtime/deliverables/<safe_task_id>/`（文本经 `read_artifact_text` 解压后明文落盘，二进制原样字节，附 manifest.json 记录 kind/title/state）。该目录永久保留、不参与磁盘治理；导出失败仅告警不阻断删除；无命中不落盘目录。
- **删除台账（task_delete_ledger）**：先记账后删除。守卫三个迟到写复活点——`append_task_event`（同事务点查，命中直接返回 0）、`put_task_summary_outbox`、`write_task_live_snapshot`；`wiped=0` 的中断残留由小时级 sweep 幂等补偿（重放 S3-S5）；台账行满 7 天（`_LEDGER_RETENTION_DAYS` 模块常量）经最终补偿后删除，台账自身不构成永久保留。sweep（`claim_maintenance_run('delete_ledger_sweep', 23h)` 卡权 + 紧急态跳过）顺带清扫遗留 `metadata.purged_at` 墓碑任务（直接全删）与孤儿 event-history 目录（无 tasks 行 + 7 天 mtime 宽限）。
- **终态大行裁剪（P3，默认停用）**：`task_model_calls / task_runtime_frames / task_node_tool_results / task_node_rounds / task_node_details` 五张大行表按任务口径裁剪（终态且 `finished_at`（缺省 `updated_at`）早于 `detail_retention_days`）；默认 `0`=停用——任务明细与任务同生命周期，只随手动删除清除，配置 `>0` 恢复按天裁剪。启用时每批 200 任务单事务 + `wal_checkpoint(PASSIVE)`，宿主并入每小时对账 loop（23h 间隔经 `maintenance_runs` 表跨进程卡权，key `detail_prune`；紧急水位跳过——裁剪自身是写放大源）。error_logs、tasks/nodes 表、task_events 行保留至任务删除（wipe 全删）。注意 `nodes.payload_json` 存的是整个 `NodeRecord`，不是纯结构行：它持有节点正文的**唯一一份** `input`（裁剪掉明细反而只有它能留正文），同时也重复存着 `output/check_result/final_output`；它不在 `_DETAIL_PRUNE_TABLES` 里，把 `detail_retention_days` 调到多大都回收不到它。
- **sqlite 空间回收（P3）**：新库建库即 `PRAGMA auto_vacuum=INCREMENTAL`（必须在任何事务写入前设置，`sqlite_store.__init__` 里位于 WAL pragma 之前）。存量库（auto_vacuum=0）迁移与全量 VACUUM 走 `scripts/compact_task_database.py`（默认 dry-run、`--apply/--vacuum-full/--backup/--retention-days`；VACUUM 前检查剩余空间 ≥1.2× 库大小，不足拒绝）。约定在服务停机或排水后运行；运行时进程内不做 VACUUM（多进程 WAL 模型下需要独占，脚本/端点隔离最干净）。

维护者常见误读：把 `DiskFullError` 当新异常类型去 catch——它继承 `OSError`，既有 `except OSError` 分支自动覆盖；把终态清理当"数据丢失"——被删的只是确定不再使用的中间产物与恢复快照，保留清单与 error_logs / task_events 行保证可回顾性（产出在删除任务时自动导出 deliverables）；把磁盘紧张当"会自动删任务"——磁盘治理没有任何自动任务删除路径，跌破紧急线只做自动暂停与可降级写跳过，任务全删仅用户手动或模型删除工具两条入口；把「清除临时文件」当删除任务的轻量版——它只动 `temp/tasks/<id>`，任务行与过程数据全部保留，但反过来它也真的会删掉误写进临时目录的正式产出，这是两者唯一的差别；在磁盘满排障时只看 `.g3ku/errors/`——磁盘满期间错误日志本身可能是 0 字节空文件，权威信号是 `write_failure_counts` 与 worker 日志里的 SQLITE_FULL 行；把紧急态下"工具不动了"当卡死——那是 target_limit=0 的排队等待，任务随即被自动暂停，恢复磁盘空间后手动 resume。

## Worker Performance History (perf_samples)

任务大厅顶部性能条的整条数据链——`WorkerPressureMonitor` 每拍（1s）采样的单槽 snapshot、`worker_status` 按 `worker_id` 主键的 UPSERT 行、`SQLiteTaskStore` 的单槽 runtime metrics——只承载"最新值"。覆盖写意味着失速通知（20 分钟量级才送达）到达时，当时是否有压力已经查不出来了。`perf_samples` 表补的就是这个时间维度。

- **写入点紧跟状态落库**：`WorkerHeartbeatServiceV2` 一拍里，`upsert_worker_status` 之后第一件事就是 `_record_perf_history`（同一份 payload，白名单 `_PERF_HISTORY_FIELDS`），节拍门 15s，每写满 240 行顺带按 24h 保留期裁剪。顺序是契约而不是风格：`worker_status`/租约写入之后的 lease 续期与状态桥接投递任何一步抛异常，本拍就提前结束，排在它后面的步骤会长期不执行且无人知晓（实盘出现过心跳新鲜、历史 0 行、存活金丝雀一行没有的三连）。采样线程本身保持零写入。
- **节拍异常必须可见**：本拍最外层 `except` 不再静默——首次与此后每 60 次连续失败各打一条 WARNING `worker heartbeat beat failed (N consecutive): <类型>: <消息>`（最坏约每分钟一行），节拍成功即清零计数。没有这条限流日志，"心跳看起来正常"与"心跳每拍都断在同一个地方"在日志上完全同形。
- **状态桥接读事件循环，不读 AgentLoop**：一拍的最后一步是 `MainRuntimeService._publish_worker_status_from_any_thread`，它要 `is_running()` 与 `call_soon_threadsafe`；而 `_runtime_loop` 承载的是 `bind_runtime_loop` 绑进来的 **AgentLoop**（frontdoor 侧读者要它的 `sessions` / `web_session_heartbeat` / `tool_execution_manager`）。跨线程投递与 `_schedule_loop_task` 一律走 `startup()` 记下的 `_event_loop`。两件事共用一个字段的结果是：`worker_status` 行照常每秒新鲜，但最后一步恒抛 `AttributeError: 'AgentLoop' object has no attribute 'is_running'`——WS 状态推送长期不动，而看数据库会以为心跳是好的。
- **15s 而不是 1s**：判"停滞是不是性能造成的"只需要知道某段窗口处于哪个档位、队列有没有等待，1s 粒度对结论无增益，却把行数与库体积乘 15。实测代价：一行 731 字节、一天 5760 行 ≈ 4.15 MiB（24h 封顶）、writer 占空比 0.05%；读侧 10min 窗口 3ms、24h 窗口 ~100ms——因此工具执行体必须走 `asyncio.to_thread`，同步跑会把 web 事件循环按住（与 `rest.py` 卸载 worker-status 同一理由）。
- **落库而不是进程内环形缓冲**：web 与 task worker 是两个进程，读端（CEO 工具、失速判读）在 web 进程，跨进程可读依赖的正是这块共享 sqlite（`task_worker_status_outbox` 同前提）；内存缓冲在进程重启后即消失，而"重启后回看昨晚"恰是主用途。
- **行不是任务作用域**：性能是机器级事实，因此不进 `delete_task` 的级联删除，只随保留期裁剪。
- **读端只有一份聚合口径**：`MainRuntimeService._perf_sample_stats` 是唯一判读实现，两种渲染共用它——`perf_report()` 给模型（工具契约见 `tool-and-skill-system.md`「fixed builtin tools」表的 `perf_inspect` 行），`_perf_window_brief()` 给失速事件那一行（事件契约见 `heartbeat-system.md`「Task Stall Detection」）。新增消费方不得再写一份档位/队列判读。
- **必须保住的判读语义**：`Samples=0` 与 `Sampling gap` 表示那段区间没有记录，不等于机器空闲，也不等于性能停滞——读端因此只报事实（库里总行数、最新一行时间、worker 心跳是否存在/新鲜到什么程度），把原因留给 worker 日志的 `worker heartbeat beat failed` / `failed to record perf sample` 两条锚点，并明说"没有行的区间事后补不回来"。**不要**在报告文案里写"就是旧进程/重启即可"这类单一断因：这条文案会被模型逐字转述给用户，而实盘已出现过心跳完全新鲜、行数为 0、原因却在写侧下游的情况。序列最多 40 桶：窗口变长只放大桶宽，报告长度与窗口无关，这是它能安全进上下文的前提。
- **时间戳口径**：行按 worker 本地时区的 ISO 秒写入（与 `worker_status.updated_at` 同格式），查询锚点先 `.astimezone()` 再做字符串区间比较；带另一偏移的锚点（例如被规范化成 UTC 的失速静默时间）必须先转本地再比，否则整窗漏行。

节点级暂停与恢复、优雅停机与启动自动恢复的合同见 `main-task-runtime.md`。

## Prompt Cache Family And Actual Request

基线合同摘要（完整取证与排查归缓存排查文档）：

- caller-side prompt cache family 只由稳定前缀加显式 cache-family revision 输入决定；普通 callable/candidate/hydrated 漂移、阶段门控收紧与 hydration promotion 可以改变 actual request，但不得自行轮转 family key。
- 每次 `call_model` 必须把该轮重建的 request 与匹配重建的 `prompt_cache_key` 一起发送；CEO/frontdoor 与节点侧都保持静态前缀前置 append-only、请求尾部区域恰好一份当前 runtime 契约，且契约与 turn-only note 排在全部携带正文之后（前缀断点只能落在尾部），携带历史中的旧契约与 turn-only note 一律剥掉。
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

## 9. Memory Runtime State

长期记忆运行时是队列化的 Markdown memory 子系统：

- `memory/memory_state.sqlite3` 是长期记忆权威状态：每行存完整记忆正文、最小摘要、`refresh_count`、`passed_count`、`is_compressed`、来源与 `from_user` 保护元数据。
- `memory/MEMORY.md` 是从 SQLite 状态再生的提示词快照，保留受管 Markdown 块形状供工具与内部 memory agent 检查，但不是权威元数据存储。
- `memory/notes/` 存 `ref:note_xxxx` 引用的可选详细 note 正文，保持小而人类可读。note 正文可由记忆管理页的 note 窗编辑（保存前二次确认并写审计，无删除入口）；删除记忆时可在删除确认对话框勾选其引用的 note 同步删除（仍被其他记忆引用的 note 会标警并默认不勾选）。失去引用的孤儿 note 由 doctor 检查与 `reconcile-notes` 报告/清理；界面与端点契约见 `web-and-admin.md`「Memory Management Page And Admin Contract」。
- `memory/queue.jsonl` 是唯一持久队列，带每请求处理状态（`pending` / `processing`、重试计时、最新错误文本）。队列条目只有两种类型：`write`（显式或已提炼的记忆文本，等待真正的记忆处理）与 `delete`（自然语言记忆删除请求，等待内部 memory agent 解析成具体 id）。
- `memory/failed.jsonl` 是失败停车区：处理尝试失败的批次（含完整载荷与错误历史）整体移出主队列停在这里，主队列继续流动。每条记录带 `failed_id`、`category`（`provider_error` 瞬时类 / `protocol` 协议违规类）、`status`（`parked` 等待中 / `requeued` 已重排回队列）、`park_count`、`auto_requeue_count`、`manual_retry_count`、`error_history`（失败与重排事件的时间线）与累计 usage。停车不写终态记录，`request_id` 不进入已处理集合，重入队后不会被幂等去重误删。
- `memory/ops.jsonl` 是滚动终态历史，不是进行中重试日志，也不是 append-forever 归档：applied 批次、`precheck_failed`（载荷本身不可恢复）与 `operator_discarded`（操作员显式放弃停车记录）等 durable 终态结果连同最终 snapshot / compression 元数据一起落在这里；无变更的 applied 行用 `noop_reason`（write 批次）或 `already_satisfied`（delete 批次）携带原因；处理尝试失败先进失败停车区而非终态历史；超过 7 天的行在正常运行时读写中自动清理。终态行不记录入队侧 `trigger_source`；区分普通窗口批次与压缩冲刷批次要对照会话转录时间线。
- `memory/review_state.json` 是普通复核窗口的按会话缓冲元数据：缓冲轮次载荷、阶段 delta cursor、已上报可见工具记录 cursor；不是已提交的用户记忆。
- `.g3ku/memory-requests/` 存暴露请求元数据的 memory 请求 artifact；processed 行可以指向这些路径供后续取证。

维护边界：

- queue 文件是运行时元数据，不是用户记忆内容。
- 权威/快照/笔记分工见上面的子系统列表；memory-worker lease 单活规则见下面的运行时合同。

运行时合同：

- `## 长期记忆` 快照注入在稳定前缀的固定位置（`system` 之后、全部历史之前，即 message index 1），所以它的字节变化等价于把身后整段历史重铺一遍。取数因此是**会话级冻结**：`adopted_memory_snapshot_text()` 在会话首次 prompt 组装时读一次 `MEMORY.md`，把只含 `---` 分隔记忆文本的展示渲染钉在会话状态上（剥掉记忆 id 与日期/来源头；内部 memory agent 仍看完整受管快照），之后所有 provider 往返——包括同一轮内的多次工具往返——复用同一份字节。改写冻结值只发生在采纳点：会话首次组装、内联 `token_compression` 轮末（`_flush_memory_review_after_compression()`：顺序上必须在复核窗口冲刷与 `run_due_batch_once()` 之后，否则读到的是冲刷前的旧文档）、手动压缩成功后（回合外车道够不到轮末采纳点，单独接一次）。读盘瞬时失败（记忆 worker 正在重写 `MEMORY.md`）保留上一份冻结值且不盖采纳时间戳，一次失败不得把整会话钉成空记忆。采纳点之间的记忆提交或删除对当前会话不可见，不同会话因此可以呈现文档的不同版本——这是规则本身；可见性窗口等于采纳点间隔，而压缩只在上下文逼近模型窗口时才发生。缓存口径与 artifact 判读见 `context-and-cache-troubleshooting.md`「长期记忆快照的会话级冻结」。
- `memory_write` 与 `memory_delete(content=...)` 只向单一记忆队列入队，不内联修改已提交记忆；surfaced agent 请求删除时不传记忆 id。
- `RuntimeAgentSession` 把自主复核窗口缓冲在 `memory/review_state.json`，并在三个时点自动入队直接 `write` 批次：配置的普通轮窗口阈值（默认 5 轮）、token 压缩冲刷（`token_compression`）、会话结束/删除冲刷（`session_boundary`）。token 压缩冲刷只看“当轮内联压缩真实发生”这一事件信号：会话级标志在回合内任一请求压缩 applied 时置位、轮首清零，不读跨轮残留的 `frontdoor_history_shrink_reason`——后者是“baseline 为何比上一轮短”的解释（合同见 `context-and-cache-troubleshooting.md`「Shrink 原因与压缩边界」），不是当轮压缩事件，用它判冲刷会把上一轮的压缩算到本轮头上。阶段压缩（`stage_compaction`）只裁会话侧提示词历史、不冲刷复核窗口。不支持的冲刷来源必须保持缓冲窗口原样，不得制造队列行。复核载荷与用户在界面看到的可见面一致：用户轮记录用户消息与助手回复；心跳/cron 内部轮只记录可见的助手回复与阶段可见面，隐藏事件束提示词不进载荷；调用 `silent` 的静默轮不入队（见「静默回复（`silent` 工具）」）。每轮载荷含 `stages:` 段（JSON，顺序与界面阶段轨一致）：回合记录时点从阶段可见面中仍保留 `rounds` 的阶段**全量捕获**所有新出现的工具调用，不设每轮条数上限——复核窗口内的阶段因此始终以完整未压缩形态送达内部 memory agent，不受会话侧提示词阶段压缩的影响；新记录按所属阶段嵌套进 `tool_records`，阶段内按目标 → `tool_records` → 阶段总结排列，携带新记录的阶段附带可见面当前完整目标与阶段总结。每条工具记录含工具名、状态、入参提示（写入侧已限长）、输出预览（截 240 字）、输出全文（截 400 字）与不截断的伴随中途输出（round.text，同一 round 只附一次）。工具记录按会话已上报 cursor（上限 5000；空 `tool_call_id` 用内容指纹）跨轮去重。`review_state.json` 除 `pending_turns` 外维护阶段 delta cursor 与工具记录 cursor：冲刷只清缓冲轮次、保留两个 cursor（普通窗口与 token 压缩冲刷）；会话结束/删除冲刷在入队后连同 cursor 一并清除该会话复核状态。`stages:` 段出现的阶段是：相对上一次复核新出现或实质变化的阶段（阶段 delta），以及本轮携带新捕获工具记录的阶段。
- 专用内部 memory agent 以 FIFO 同-op 批次（`write` 与 `delete` 不混批）消费队列，走 `memory` 模型路由，只有读写 `MEMORY.md` 与 note 文件的受限工具面；它把自然语言删除请求解析成具体 SQLite id，并可报告实质影响该批次的 `inspired_memory_ids`。
- `memory_apply_batch` 的零变更出口按批次类型分岔：write 批次交 `noop_reason`，delete 批次不接受"本轮无需变更"，只能交 `already_satisfied` 声明待删目标已不在当前正文（已被更早批次改写或删除），该批次按 applied 收尾且快照不变。op 专属规则与参数形状同在暂存层返回字段错误，模型可在同一批内改提交而不消耗跨批尝试次数；落盘前的校验再独立复查同一组规则。
- 每次非读变更后，运行时先改 SQLite、再重建 `MEMORY.md`、最后检查快照大小：超过 `document.compress_trigger_chars`（默认 `16000`）时按 `passed_count DESC`、`refresh_count ASC` 顺序压缩，先把整行替换为 `minimal_memory`，再只删除已压缩的非 `from_user` 行，直到回到 `document.compress_target_chars`（默认 `13000`）或没有安全压缩工作。
- 队列消费跨进程单活：每次 `run_due_batch_once()` 必须先拿 workspace 级 memory-worker 文件锁；拿不到锁的进程保持队列不动并报告 `worker_lease_unavailable`。`request_id` 是持久幂等键：处理批次前会丢弃 `memory/ops.jsonl` 中已出现过 `request_id` 的队列行。

### 队列状态机与失败停车语义

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

## Internal Turn Prompts

Heartbeat 与 cron 内部轮次共享同一内部轮次合同，完整契约详见 `heartbeat-system.md`「Continuation Contract」与「Cron Reminder Contract」。本文只记运行时层不变量：

- 内部轮次与普通可见轮次一样通过 `RuntimeAgentSession.prompt(...)` 执行，携带各自的内部来源元数据；它们会清掉 live-only 调试面（`frontdoor_selection_debug`、每轮 actual-request 指针），但不在 prompt 组装前清零 session-owned 请求体 / 阶段 / 压缩连续性状态。
- 规则文本与事件载荷以隐藏内部提示消息追加：`prompt_visible=true`、`ui_visible=false`，带 `internal_prompt_kind`（`heartbeat_rule` / `heartbeat_event_bundle` / `cron_rule` / `cron_event_bundle`）；heartbeat 追加 `system` 规则 + `user` event-bundle，cron 追加两个隐藏 `system` 块。存在权威 frontdoor 基线时，内部轮次直接继承普通 CEO tool/skill 暴露合同（含无有效阶段仍保留全量 callable 的合同）。
- 无基线的内部轮（重启后首轮、全新会话首轮）不进入续跑分支：内部事件消息单独交给 prompt 组装，由新建路径注入，基础系统提示保持首位。续跑分支只在存在真实请求体基线时使用——否则仅有的内部事件消息会冒充完整旧请求体、让基础提示被静默丢掉。内部轮基线/恢复细节见 `context-and-cache-troubleshooting.md`「heartbeat / cron 按普通 continuation shrink 规则排查」与「Baseline 合同与恢复顺序」。
- 服务层不得替模型自动重试任务，也不得合成回退 assistant 回复。
- `ceo.internal.ack` 帧是 live-only 的，且只兜"内部轮空输出"这一种：它不新建转录条目。模型自己选静默的回合走 `ceo.reply.final` + `silent_reply`，其转录行才是那两回事的载体——durable、prompt-visible 且 ui-visible（渲染成折叠行），模型要能在后续轮次读到自己上次的静默选择。隐藏内部提示消息（`ui_visible=false`）是第三类：durable 且 prompt-visible。合同详见「3.3 静默回复」。

## Repeated Tool Call Guard

执行阶段重复工具调用的同轮执行前去重（reused 合同）、跨轮软拒绝、修复消息、升级语义与只读检索分支契约详见 `tool-and-skill-system.md`「Duplicate Tool Call Guard」。

## CEO Frontdoor Canonical Context Contract

`frontdoor_canonical_context` 是 CEO/frontdoor 唯一的跨回合阶段真相源：

- 它是 durable 的跨回合阶段/历史视图；turn finalization 把当前轮阶段账本并入该结构。`frontdoor_stage_state` 与 `compression_state` 是运行时工作状态，不需要在每个新用户 / heartbeat / cron 轮次的 prompt 组装前清空；当前轮本地状态为空时，`prepare_turn` 可以复用 session-owned 请求体与这些快照重建下一个 provider 请求窗口。
- session/runtime 同步不得把 request-local 投影写回 `frontdoor_canonical_context`：只有 turn finalization 允许向 durable canonical 链追加 completed-stage 数据；`frontdoor_canonical_context + 当前 frontdoor_stage_state` 派生出的一切只是当前请求的可见 workset 数据。
- 近场 stage workset 从 `frontdoor_canonical_context + 当前 frontdoor_stage_state` 派生，不从 transcript `execution_trace_summary` 或平铺 `tool_events` 重建。round-level 工具记录同时保存归一化原始 `arguments`；小输出内联在 `output_text`，大输出外置为 `output_ref` + `output_preview_text`，prompt 渲染器不把 artifact 正文读回内联。
- canonical 归一化以 `stage_id` 和完成阶段内容身份做 last-write collapse：同一逻辑阶段被 rebase 后再次并入时保留最新副本，不重复追加整个携带 workset。排查 sidecar 膨胀时，记录数应与 distinct stage 身份数一致；持续增长说明合并边界回归。
- `project_canonical_context_for_transcript()` 只用于 assistant 转录记录：沿用同一套标记驱动的 canonical 表示（未点名未收口的完成阶段与活动阶段为 raw，带 `context_evicted` / `context_visible: false` 的为 compact），并截短 raw round 内超大工具正文与入参（`output_text` > 2000 置空、结构化 `arguments` > 2000 置 `{}`、`arguments_text` 与 `round.text` 各限 4000）。provider prompt 仍以 durable canonical context 和当前 stage state 为权威，不读这份转录投影。
- Web UI 载荷使用同一投影视图，而不是把未投影的 live workset 直接下发：`project_canonical_context_for_ui_payload()` 保留 raw 窗口阶段未投影的 round 正文；`ui_canonical_context_delta()` 先把前后两侧都按转录投影对齐，再让已存在阶段沿用基线表示（compact 不因新增阶段造成窗口移动而重新展开），因此新回合 delta 只携带新阶段与真实变化，并把 delta 保留阶段的正文回填为实时未投影值。UI 最新气泡重新出现全部历史阶段的回归通常是 UI delta 退回原始 `canonical_context_delta`。
- 转录投影对已投影输入幂等：带 `stage_window` 标记的转录行本身即转录投影视图，按序回放（如快照 delta 链）可直接作为基线视图使用；`ui_canonical_context_delta_from_views()` 接收两侧已投影的视图，输出与从原始输入投影的路径逐字节一致，逐行重投影整份转录属于平方级构建回归。
- assistant 轨道行的存储形态二选一：checkpoint 行（`canonical_context_projection: stage_window` + 全量视图）或 delta 行（`delta_window` + `cc_upsert`：stage 级 upsert，已有 `stage_id` 原位替换、未知追加，`headers` 承载顶层字段变化）。写入侧（`plan_transcript_cc_row`）相对**物理上一条轨道行**的视图编码——含 ui_visible False 的 heartbeat/cron 行，读侧游标必须同样遍历隐藏行；每行都过 `encode_cc_upsert` 的重放自检，不可编码、链行数达 40、链字节达 96KB 或没有前序锚点时回落 checkpoint，首条轨道行永远是 checkpoint。单行物化走 `materialize_transcript_view`（回溯最近全量锚点正向重放，链长有界）。就地替换轨道行（暂停归档）必须带替换前视图调 `repair_transcript_cc_chain` 重编码下游，否则旧链上的 delta 行静默错位。存量文件在 `_load` 里经 `migrate_transcript_rows_to_delta` 收敛（逐行重放校验、失败行自动落 checkpoint、幂等可重入），metadata 行 `cc_format: delta_window_v1` 使后续加载跳过迁移。
- 若当前轮阶段状态里已包含与 `frontdoor_canonical_context` 中实质相同的 completed stage，prompt 组装必须按重叠处理、跳过把它 rebase 成新的合成 stage id——否则一个 completed stage 会在 fresh-turn 重建中膨胀成重复的原始阶段块。
- UI 面向的 turn payload 暴露当前轮的 `canonical_context` 投影切片；prompt 组装读 durable 跨回合 canonical context，inflight / paused / final-reply payload 只描述可见轮自己的阶段轨迹。

第二条连续性合同：`frontdoor_request_body_messages` 是下一轮 CEO/frontdoor 的 session-owned provider 请求体基线，刻意不含 `frontdoor_runtime_tool_contract` 消息（动态工具暴露每轮作为新的尾部合同重建），也不含 `## 长期记忆` 快照（会话级冻结的展示块，只在当轮请求注入，落史会逐轮累积污染上下文；它的取数与采纳点合同见本文「Memory Runtime State」），且只允许在 `token_compression` 与同轮 `stage_compaction` 两个信息损失边界收缩，或经操作员发起的 `user_edit_truncation` 在轮间整体替换（见本文「Frontdoor Context Compression」）。fresh 可见轮次中该基线是连续性权威来源：必须从请求体基线继续，而不是从阶段重放重建新的主前缀。

## Runtime Contract Lane

模型面向的运行时契约是以 `## Runtime Tool Contract` 开头的 system summary 块（summary 形式、修复车道、“尾部只带最新快照”与基线剥离规则详见 `tool-and-skill-system.md`「四个概念必须分清」与 `context-and-cache-troubleshooting.md`「append-only 规则」）。本节记录本文拥有的 canonical 表示规则与运行时边界。

Canonical 阶段状态按以下表示规则收敛（这是 canonical 链唯一允许的信息损失边界）：

- 完成普通阶段默认全部保持 `raw`，与当时是否存在活动阶段无关：纯对话回合（无活动阶段）同样不因为"变老"而降级。**表示形式只由标记决定**，没有条数窗口。
- 带 `context_evicted: true`（模型点名裁撤）或 `context_visible: false`（压缩收口）的完成普通阶段变为 `compact`。
- canonical 链只有 `raw` 与 `compact` 两级表示：历史数据中已存在的归档压缩阶段（`stage_kind="compression"`，外置表示）继续规范化与渲染，运行时不把完成阶段合并成新的归档阶段；长会话的阶段体积由 `compact` 块承载，并在 `token_compression` 边界整体收口——块按阶段账本逐轮重渲染，收口（上一条）是压缩能真正收缩阶段体积的支点，缺了它压缩只删得掉工具肉身、删不掉块，总体积兜底也就无从谈起。
- 这些表示渲染为 `[G3KU_STAGE_*]` 消息块时以 system 角色落地（识别端接受 assistant/system 双角色以兼容存量旧块；角色合同本体见 `context-and-cache-troubleshooting.md`「压缩块的格式与字段语义」）。
- 完成阶段还可以带 `context_visible: false` 收口标记：它不是第四种表示，而是"这条阶段已经进过全局摘要"的 durable 记账。带标记的阶段不渲染任何块、也不影响其余阶段的表示形式，但记录本身留在账本里——Web 时间线与转录投影照常读到它。标记只在 `token_compression` 边界写入（见本文「Frontdoor Context Compression」），逐轮重算按回归排查。字段缺失即视为可见，存量账本与旧 sidecar 不需要迁移。标记必须在这两份账本各自的**归一化白名单**里都留位（canonical 的 `normalize_frontdoor_canonical_context` 与 stage_state 的 `_frontdoor_stage_state_snapshot`）：账本每过一回合都要重新过一次归一化，白名单漏掉这个字段等于逐轮抹掉标记，而渲染读的是 stage_state，所以只有一份留住标记的可观测结果就是"完全没收口"。

另有两条运行时边界：

- prompt token trace 只有两个字段：`pre_request_prompt_tokens` 是内联 `token_compression` 之前的发送前估算（必须包含 stage workset）；`effective_prompt_tokens` 是 prompt 组装完成后最终真实发送请求的估算。
- 节点侧 token 压缩是同一 `token_compression` 边界的节点实现：用任务目标针对型提示词对可压缩历史做一次 inline LLM 摘要后重写当次请求。它只是针对当次请求的 live 重写：可以缩短 provider-bound `request_messages`，但不得改写持久阶段历史、frame `messages`，或从 `model_messages` 派生的稳定 prompt-cache family 输入。

若下一轮基线以两个收缩边界与 `user_edit_truncation` 之外的任何理由变短，按意外上下文损失排查；守卫自愈行为见本文「Frontdoor Context Compression」。`user_edit_truncation` 的替换发生在轮间（守卫比较的是"会话基线 vs 本轮新请求"，替换后新请求只会更长），不会触发 quarantine。

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

这套分离只为在不削弱阶段门控与工具 hydration 规则的前提下提高 prompt cache 稳定性。同轮内节点多轮循环的 provider 请求构造走 append-only scaffold：上一份真实请求体 + 上一轮新增的 assistant / 工具结果消息 + 最新尾部三件（稳定契约、活状态块、当轮 turn-only note，末位是 user 角色提示）。跨 run 的第一跳（notice 唤醒、restart/resume、恢复重放）骑同一条链：请求以持久 actual-request scaffold（内部形态 `request_messages`）为前缀经头探针 + 多锚点尾对齐 adoption，投影超出 scaffold 覆盖点的记录（held notice、重放轮、当前 user 回合）作为显式 delta 追加；每轮请求的来源落 `request_seed_source` / `request_seed_message_count` 诊断（runtime frame、actual-request artifact、`task.model.call` 行同源），种子不可用（缺失、guard 降级、头漂移、对齐失败）时回退投影重组装并带 `fallback_*` 标记。scaffold 只是请求构造脚手架——不替代节点持久/压缩后的 `message_history`，也不重新定义哪些工具可调用；它存在的唯一目的是：当阶段压缩在回合边界修剪历史时，provider 看到的是 append-only 增长而不是早期前缀重写。`provider_tool_bundle_seeded` 只是兼容/诊断提示，真实行为由活跃/待定曝光状态加 token 压缩提交门控驱动。链式前缀在一种情况下让位：阶段过期点（见下节「stage_compaction」的"过期点换基线"），那一跳 `request_seed_source=same_turn_stage_compacted`，除此之外任何一轮的首个分叉点都必须落在历史尾部。完整规则详见 `context-and-cache-troubleshooting.md`「append-only 规则」。

## Frontdoor Context Compression

- 压缩只有两条车道：`token_compression` 与 `stage_compaction`，不存在中间的"按消息条数压缩"阶段。`stage_prompt_compaction` 是 stage-window 修剪的唯一真相源，不要再引入一条平行的结构式压缩车道与它共享 helper。
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
- 尾部收敛保证：保留的最近 body-history 尾部（CEO/frontdoor 基准 4 条，节点基准 12 条）是压缩后请求体的不可压缩下限。尾部边界必须对齐到完整的工具调用组：尾部首条是 `role=tool` 结果时，边界向前扩展，把声明它的 `assistant(tool_calls)` 消息连同结果一并保留进尾部；扩展上界是单个工具批次，最坏整个 body 成为尾部、无可压缩历史，由调用方按既有「无可压缩历史」分支处理（已超窗则发送失败，不静默）。未对齐的边界会留下声明已落入摘要区的 tool 结果，产生孤儿工具结果：节点通道触发孤儿检测熔断（`main-task-runtime.md`「Node-Level Pause and Recovery」），会话通道没有检测器、孤儿会静默直达 provider。对齐之后，再把尾部中超过字符上限（16000）的工具结果消息硬截断为「截断头部 + 检索指引」，保证压缩后估算必然收敛到窗口以内。若不做截断，一条超大尾部工具结果（例如 `content_open` 对单行巨型 artifact 的打开结果）本身就能让压缩后估算持续超窗——压缩检查必然抛错、回合必然失败，而该消息又始终落在保留尾部，形成每轮压缩、每轮失败的无限循环。CEO/frontdoor 的尾部是一条常数地板：最近 4 条（工具调用组对齐后可能更宽），不由账本反推。未被点名的阶段一直带着正文，被摘要取代的那些按收口记账，因此不存在"要为某一层阶段窗口把尾部放宽"的第三层。
- 静默痕迹行受两条车道豁免。`silent` 那行 assistant 记录的全部价值在于后续轮次能看见"上次对哪个任务选了沉默"，被摘要吞掉等于这条判据从未存在过（裸 tool_calls 行在两条车道下的实测存活率约 6%）。stage 车道给它一个无条件不可删位，配对的 tool 结果行按既有的成对规则随之保留；token 车道是位置型切分，白名单对它无效，所以把该组整组摘出待压缩区间、压缩完成后回插在摘要块与保留尾部之间（比尾部更早，顺序单调）。摘组必须 assistant 与 tool 结果同进同出，否则产生孤儿工具结果——节点通道会触发孤儿熔断，会话通道没有检测器。
- 节点追加通知的因果保留：`[G3KU_APPEND_NOTICE_TAIL_V1]` 未消费通知窗口（`raw_notice_window`）无论处在历史何处都原样保留在压缩块之前，不进入 LLM 压缩——追加通知往往是任务目标的最新变更；已消费汇总窗口（`compressed_notice_window`）本身已是摘要形态，随历史一起压缩。
- 阶段收口是 CEO/frontdoor 压缩的第三件事：本次真正被吞掉的阶段（= **终态**普通阶段 − 活动阶段 − 肉身或旧块仍留在保留尾部里的阶段；判据完全由正文派生）连同完整记录（逐条 `key_refs` 与轮次）确定性导出到 `session_temp_dir` 下的归档文件。终态判定是白名单（`completed` / `failed` / `完成` / `失败`）而不是"排除某个进行中字样"：两条车道各写一套状态词表（前门英文 `completed` / `active`，节点中文 `完成` / `进行中` / `失败`），认不出的状态一律当作还在跑——少收一轮只是多花 token，多收一轮就是把一个仍在写轮次的阶段收进摘要够不到的地方。"吞掉了哪一段"挂在压缩块的 `stage_archive.archived_through_created_at` 上——一个 created_at 上限，不是逐条 id 清单（375 条 id 要 8,469 字符 ≈ 2,300 token，比它守护的摘要正文还长，还得每轮随块重发），同一对象另带 `ref` / `stage_index_start` / `stage_index_end` / `stage_count`。**标记本身在 durable 基线推进的那一步（`_persist_frontdoor_actual_request`、回合收尾的账本提交点）才被应用**——压缩算完不等于 provider 看到了摘要，若那次发送随后失败或被暂停，基线仍是带块的旧请求体，此时翻标记就等于把那批阶段连同摘要一起丢掉。归档文件写不出来时整轮不收口（没有 `stage_archive` 就没有水位线）：宁可不缩，也不能把阶段收进一个模型打不开的地方。不删账本、不改内容、不动 `stage_index` 序列：阶段只是不再进 provider 上下文，账本仍是 Web 时间线的权威。水位线按 `created_at` 命中，顺带绕开了两套 stage_id 各一套序号的问题（同一会话实测 1..398 对 944..1341、交集为 0）：created_at 是同一条逻辑阶段在两份存储里共享的同一个值，所以 `frontdoor_canonical_context` 与 `frontdoor_stage_state` 各自就地标一遍即可。仍带 `stage_ids` 的存量块按内容身份（`created_at` + `finished_at` + `stage_goal` + `completed_stage_summary`，与跨 rebase 去重同一口径）跨存储匹配，读到那块被下一次压缩改写为止。水位线圈的是一段区间，落点再过一遍"内容肉身是否还在"：工具轮次或 `[G3KU_STAGE_RAW_V1]` 块仍在即将落定的请求体里的阶段、以及近场 raw 保留窗口占着的阶段都不收（那批内容没进摘要）；派生的 `[G3KU_STAGE_COMPACT_V1]` 块不算在场——标记一旦丢过块会整批长回来，那时必须还能再收一次。缺 created_at 的阶段水位线圈不到，只会多渲染一轮，不会丢内容。收口只能随压缩边界发生——逐轮按"块太多"自行收口会破坏 append-only。
- 证据引用由模型选号、由运行时抄写：单发压缩请求的指令消息尾部带一份带编号的候选清单（被吞阶段的全部 `key_refs`，同一 ref 取最后一次说明），模型只被要求输出 `## 证据索引` 小节、每行一个 `- [#编号]`，refs 正文由运行时按编号**逐字回填**进摘要块内部，越界编号丢弃，指向已不存在文件的路径剔除（`task:` / `artifact:` / `node:` / 裸 id 不做文件系统判定，会被误杀成死链）。摘要块内同时追加 `## 阶段归档` 一行，给出归档文件绝对路径与 stage 区间，模型要回看细节时用 `content_open` 打开。为什么这样分工：让模型自己抄引用路径的跨代逐字节保真实测约 30%（文件名对、完整串被改写），不透明 id 约 10%——抄写必然出错，选择才是它的强项；而索引段写在摘要正文内部，因此和摘要本身一样受下一轮压缩管辖，不会长成第二套逐轮重注入的地板。
- 分块压缩车道（`compression_mode=llm_chunked`）不要求模型选号：每块看不到全量候选，选择语义不成立。该车道照常收口与导档，摘要里只出现 `## 阶段归档` 指针，不出现 `## 证据索引`。
- 两条路径共用 `[G3KU_TOKEN_COMPACT_V2]` 前缀与同一份块形态：首行前缀、第二行 JSON 元数据、空行后接摘要正文，且元数据行必须能单独解析回来（前门的收口落点靠它读水位线）。压缩块 kind 分别为 `frontdoor_token_compaction_llm`（诊断 `mode=llm`）与 `node_token_compaction_llm`，靠 kind / `node_id` 字段区分。节点压缩产出同一份信封：证据引用候选与逐字回填、`## 阶段归档` 指针、`stage_archive` 水位线，归档文件落在该任务的 `task_temp_dir`（`kind=node_stage_archive`、`owner=task:<task_id>/node:<node_id>`）；被吞集合的判定与前门共用同一个 `summarized_stage_ids`（节点只有一份账本，没有两套 stage_id 的问题）。**阶段收口只在 CEO/frontdoor 应用标记，节点车道不应用**：节点的 `token_compression` 是 live-only（durable 写回用未收缩的投影），且没有路径给节点账本翻收口标记，所以"翻标记"这个动作对节点没有对象可施；节点 artifact 里带 `stage_archive` 而账本无 `context_visible` 是设计边界，不是漏做。节点的阶段块不归这条管——它随 `stage_compaction` 的过期点换基线真的进入发送体（见下节）。节点的 `## 阶段归档` 文案也只声明"本次压缩不再逐条展开"，不声称逐轮退出。
- 操作员可在回合外发起同一份 `token_compression`（CEO/frontdoor 通道，节点没有该入口）：`POST /api/ceo/sessions/{id}/compress-context` 立即返回 `running`，起跑的后台任务先 `await session.pause(manual=True)` 停掉在途工具与请求，再以**空草稿**重建 durable 基线作为压缩输入——没有新消息时预检重建出的请求体就是下一回合的起点，这正是被压缩对象。停手必须留在后台任务里而不是端点请求里：`pause` 与区分线落盘都要整份重写转录，渠道会话实测单份转录 70MB 级，放在请求内会顶穿浏览器侧 20 秒的请求超时，前端就把还在跑的压缩误读成失败（UI 侧合同见 `web-and-admin.md`「Manual Context Compression」）。摘要产物除返回 token 数外必须经 `_persist_frontdoor_actual_request` 同时成为新的 durable 基线与一条同源的 actual-request artifact，并把 `frontdoor_history_shrink_reason` 记为 `token_compression`；基线与 artifact 不同源会让重启后的字节对账判为失配、清空 trace 并静默丢掉这次压缩（详见 `context-and-cache-troubleshooting.md`「Shrink 原因与压缩边界」）。
- 回合外压缩没有 inflight turn 可承载进度，实时状态走 `state.compression`；它的取消复用同一套压缩代际软取消（`_cancel_active_frontdoor_compression_generation`），代际尚未建立时由端点直接掐任务。取消、provider 异常与进程中断都按「未得到摘要」处理，永不写出 `G3KU_TOKEN_COMPACT_V2` 基线。手动与自动两条路径都在终局向转录追加一条 UI-only 的压缩区分线行（`completed` / `paused`），UI 语义详见 `web-and-admin.md`「CEO Compression UI Contract」。

### `stage_compaction`

- 修剪的唯一真相源是 `stage_prompt_compaction.compact_stage_prompt_messages_in_place()`：未点名未收口的完成阶段与活动阶段保留完整窗口；已离开可见层阶段的工具调用消息（`assistant+tool_calls` 与其配对 `tool` 响应）成对移除，对应 compact 块回插在该阶段首条被移除消息的位置。块锚点按「记忆位置 → 本次首条被移除帧位置 → 邻居夹逼」三级取定：两个直接定位来源都缺失时（请求体被重建，帧与旧块一并消失），锚点夹逼在前一个已确定锚点之后、后一个已知锚点之前，且锚点相对 `stage_index` 单调不减——「用户消息 → 该阶段块 → 该阶段最终回复」的相对顺序因此不依赖该阶段是否还有帧在体内；夹逼兜底可退化的只有精确邻接（原始定位信息已不存在），顺序不变量不受影响。块以 system 角色渲染，识别端接受 assistant/system 双角色，存量旧 assistant 块在下一次压缩渲染回插时自然收敛，过渡期不重复、不丢块）；阶段之外的用户可见对话原位保留；内部事件束（心跳规则/事件束、定时任务中文包装与 `[CRON INTERNAL EVENT]` 事件体）按缓存中性规则移除：只清理不早于本次压缩既有最早结构变化点的条目，本次压缩没有任何结构变化时一律保留，避免为清理历史内部事件额外打断 provider 前缀缓存。
- 两条车道给这个函数喂的账本形态不同：CEO/frontdoor 是 dict，节点是 `normalize_execution_stage_metadata()` 的模型对象。所有账本字段读取（含过期阶段轮次 call_id 的收集，走 `stage_round_call_ids`）必须类型无关。dict-only 判读会让压缩退化成"只渲染摘要块、不删工具肉身"——块数照常增长、`stage_compaction_applied` 为假、投影单调上涨，从外表看不出任何异常，只有把同一份账本按 dict 与模型两种形态各跑一遍才能暴露。
- 过期判定用终态白名单（`stage_is_terminal`：`completed` / `failed` / `完成` / `失败`），与收口同一词表：两条车道各写一套状态词表，认不出的状态一律当作还在跑。判据方向是不对称的——少裁一轮只是多花 token，多裁一轮是把仍在往 `rounds` 里写轮次的阶段收进摘要够不到的地方。
- 节点侧的压缩必须落到发送体才算省 token，但让位的窗口只有一个：投影每轮都重算，同回合链默认续写上一份实发原文，因此**仅当上一份实发基线里仍留着被判过期阶段的工具肉身**（= 正踩在阶段过期点上）的那一跳，用投影整体替换基线，`request_seed_source` 记 `same_turn_stage_compacted`、`history_shrink_reason` 记 `stage_compaction`。换完基线后该判据立刻转假（裁过的正文不再含那些 call id），下一跳起 append-only 链照旧。这条"只换一次"的收敛性是硬要求：否则前缀失效面从"每个过期点一次"放大成"每轮一次"。fresh turn 的第一跳不参与该判定，它采纳持久 seed，过期点最多推迟到同 run 的第二跳生效。
- 过期点的账单形态是设计的一部分：那一跳 provider 前缀从最早被裁阶段的位置起重传，`cache_hit_tokens` 塌下去、非缓存 `input_tokens` 冲高一次，之后逐轮重新长回来；裁过的正文同时成为新的 actual-request 持久 seed，所以收益跨回合与跨重启自动保持，不需要额外的 adoption 分支。前门车道的收口标记（`context_visible`）仍只在 CEO/frontdoor 应用，节点没有等价动作。
- 同一条"只换一次"的规则两条车道都有，前门的落点在 `_graph_execute_tools`：本轮工具批次执行完、账本已带上新落点（`context_evicted` / 收口标记）时，把刚追加完的正文过一遍原位投影，裁过就以此作为下一跳的发送基线并记 `frontdoor_history_shrink_reason=stage_compaction`。前门没有 `request_seed_source`（它按回合装配，不按 run 续跑），取证看 shrink 原因与块数：点名之后的跳里 `[G3KU_STAGE_COMPACT_V1]` 块必须出现、被裁阶段的 `tool` 帧必须消失。判据与节点同样自灭——裁过的正文不再含那些 call id，下一跳回到 append-only 链。
- 阶段块识别排除工具调用回合：assistant 消息携带非空 `tool_calls` 时，即使正文以 `[G3KU_STAGE_*]` 前缀开头也不识别为阶段块——那是模型在发起工具调用的同一回合回显了阶段块。所有整块丢弃路径（前缀清洗、压缩 step-0、续尾重排守卫）都必须保留该消息，否则其配对的 `role=tool` 结果成为孤儿工具结果（节点通道触发孤儿检测熔断，会话通道静默直达 provider）；语义对齐节点侧契约剥离的 `_message_declares_tool_calls` 守卫。
- 原位放置是缓存硬约束：压缩块不整体收拢到上下文头部；除治愈遗留布局的一次性收敛外，每次压缩的前缀失效面从最早被压缩阶段的位置开始。断点**位置**钉在"最早一条被移出可见层的阶段"的首帧，节点上实测在 index 19，之前 19 帧 = system + 引导 user + 已裁阶段的块 ≈ 3.7 万字符 ≈ 1.25 万 tok，与实测裁后 `cache_hit` 1.2–1.7 万 tok 同量级），随裁撤点深浅变化的是**断点后面还要重传多少**：裁最老那条时，断点后面压着其后所有阶段（实测一条活节点 27.4 万字符 ≈ 9.2 万 tok，那一跳非缓存 `input` 从正常的 1.5–3 千冲到 7.4 万）；一关阶段就裁时断点后面只剩刚开的 0–2 轮（实测每跳增量 3 帧 ≈ 4.3 千字符 ≈ 1.4 千 tok）。所以"攒批再裁"是反向杠杆（实测按 3 批一次约多付 25 倍），要省只能更早裁；而断点次数只由阶段数决定，与窗口无关。
- 块是"按账本逐轮重渲染"的产物，不是历史消息：每轮从 `frontdoor_stage_state` / `frontdoor_canonical_context` 的合并视图重新生成一份块集合，因此仅重写请求体基线（`token_compression`）不会让块变少——收口标记才是。离开可见层的阶段只有两类：带 `context_visible: false` 的已收口阶段，与带 `context_evicted: true` 的已裁撤阶段。两者信息损失边界不同，不可混用：收口连块一起不再渲染（正文已进全局摘要），裁撤只移出工具肉身、块照旧逐轮在场（块里那条总结就是它承诺的唯一留存记录）。已收口阶段若其工具肉身被别的路径重投影回体内，按原位移除且不再补块，这是收口的既定信息损失边界。
- 裁撤由模型在关闭阶段时点名：`submit_next_stage` 的 `drop_completed_stage_tool_detail` 为真时，正在关闭的那条阶段落 `context_evicted: true`（前门写 dict 账本、节点写 `ExecutionStageRecord`，两条车道共用同一个 `retained_completed_stage_ids` 判据，前门的 `raw_stage_renderer` 直接 import 它：判据只允许有一份，两处各写一遍时改一处就会让 raw 块与 compact 块对同一条阶段同时漏渲或同时渲染。它要求同批带非空 `completed_stage_summary`——库里 46.2% 的终态阶段总结本就是空的，没有总结就裁等于把该阶段唯一的记录一起移走）。未点名的阶段一直留在上下文里，直到压缩把那段时间写进摘要——机器不按条数替它决定。
- "正在关闭的那条阶段"只有一个判据（`stage_prompt_compaction.closing_stage_target`，两车道、summary / `key_refs` / 裁撤三者共用，账本形态无关：前门是 dict、节点是 `ExecutionStageRecord`）：有活动阶段时是它；没有活动阶段时是**最后一条**阶段，且它必须是终态普通阶段、`completed_stage_summary` 还空着——也就是刚被自动结清、正在等模型补写蒸馏结论的那条。这条回退是必需而非优化：两条车道都会清空活动位——前门在回合尾（`_complete_active_frontdoor_stage_state` 按合同不代写摘要，最终回复紧邻块之后就是该阶段的记录），节点在每次 run 终局（`finalize_execution_stage`，**含失败态**，而失败节点会被恢复继续跑：错误恢复与验收打回都走这条路）。模型的实际习惯是把收尾材料留到下一次提交：渠道会话"一回合一个阶段、下一个回合才发 `submit_next_stage`"，节点是"失败结清后恢复再提交"。只认活动位会让模型写的总结、`key_refs` 与裁撤意图三者一起静默悬空，新阶段却照常追加，从外表看不出任何异常——实盘一条 QQ 渠道会话即 12/12 条阶段无总结、点名过的裁撤一次都没兑现、归档文件 0 个、被点名阶段的工具帧仍逐轮重发；同一库里另有 2 个 `in_progress` 节点处在活动位已空、末条总结为空的同形态窗口。回退窗口只有一条且要求总结为空，所以既不会回头改写更早的阶段，也不会把承接变成"代写摘要"的入口；跨结清补记不改动那条已有的 `finished_at`（收口水位线与阶段排序都按它命中）。
- 落空必须可见：模型给了收尾材料（非空总结或 `drop`）时，`submit_next_stage` 的返回带 `stage_closure`（`target_stage_id` / `summary_attached` / `evicted` / `reason`，节点侧多带一个已落盘的 `archive_ref`），它是"这次承接了没有"的权威回执，也是唯一让模型不必凭参数倒推结论的落点——缺了它模型会先向用户宣称"已移出"，下一轮发现没有 `archive_ref` 再改口猜"被压缩折叠了"。`reason` 取 `applied` / `no_closing_target` / `summary_required`。该字段只挂在给模型看的副本上，账本里的新阶段记录保持原形状。前门两条车道共用同一个提交入口，差别只在图闭包侧不导档：写在会被覆盖的工作副本上的 `archive_ref` 只会多留一份无人引用的归档文件。
- 移出只作用于 provider 可见层：账本 `rounds` 与 `task_node_tool_results` 一条不减。**裁撤导档是两条车道共同的动作**，块里那一行 `archive_ref` 就是 `content_open` 的入口：前门由 `_frontdoor_archive_evicted_stage` 在 finalize 重建 durable 账本那一步（`_frontdoor_stage_state_after_tool_cycle` 重放 `submit_next_stage` 之后）写 `kind=frontdoor_stage_eviction` 到 `session_temp_dir`；节点由 `log_service._export_execution_stage_eviction_locked` 在 `submit_next_stage` 落 durable 账本的同一步写 `kind=node_stage_eviction` 到 `task_temp_dir`，落点只认任务 runtime meta 里的绝对路径、**不回退工作区根目录**（那会让归档躲开磁盘治理）。`task_node_detail` 仍是节点的全量轨迹通道（按 `stage.rounds[].tool_call_ids` 关联逐条 `arguments_text` / `output_ref`），与文件指针互为备份。导档点都不放在会被覆盖的工作副本上：前门的图闭包改的是本轮副本，durable 账本随后被重放返回值整个覆盖，写在副本上的 ref 等于没写（文件照落盘，块里却永远只有 `evicted` 没有指针）。**前门不做专用读取工具**：送给 provider 的工具 schema 由 `_selected_tool_schemas` 从**全局** `loop.tools` 注册表按名字解析（`bootstrap_bridge` 里那句「光靠 callable 名单点名不够，工具必须先存在于 registry」就是这条），而阶段账本是**每会话**的——把带闭包的实例注册进全局表等于把 A 会话的账本暴露给 B 会话，所以绕开工具面复用 `content_open`。一条阶段只导一次（ref 已存在即复用）；导档失败或拿不到临时目录就不写 ref、块里也不出现指针，但裁撤标记照常落（移出这件事不依赖归档成功）。两条车道的块都带 `evicted` 字段标明"这条是你自己移走的"，没有它模型不知道该不该回读、事后也无法从发送体核对裁撤被用过没有；节点详情投影（`execution_trace`）同样只在成立时写 `context_evicted`，否则读时间线的人分不清"这条阶段本来就没留细节"和"细节被移进了归档文件"。有一批帧点名也裁不到（`submit_final_result` 被拒轮与单独提交的 `submit_next_stage` 边界轮不进 `rounds`），读正文尺寸前先按「Shrink 原因与压缩边界」的不可回收地板扣掉它。该标记穿过前门四处重写者（`_normalize_stage` 白名单、`stage_state` 快照白名单、`_dedupe_canonical_stages` 的跨副本继承、`_completed_stage_overlap_signature` 的剔除）——收口标记曾在同一批落点上漏过一次导致块整批长回来，裁撤标记走的是同一批。
- 该规则与是否存在活动阶段无关：没有活动阶段的纯对话回合同样只裁被点名/被收口的阶段，其余全部留在窗口里（阶段再多也不因"变老"降级）。
- 幂等：同一输入重写两次收敛为同一输出；归属保留阶段的残留旧块去重丢弃。
- 可以缩短活动历史窗口或 stage workset，仍是下一轮基线的合法收缩理由。
- 不是 provider schema 刷新边界：若某次发送的收缩原因是 `stage_compaction` 而 `provider_tool_names` 变了，按 provider-bundle 刷新路径 bug 排查。
- 压缩块的前缀标记与 JSON 字段语义（`G3KU_TOKEN_COMPACT_V2` / `G3KU_STAGE_COMPACT_V1` / `G3KU_STAGE_EXTERNALIZED_V1` / `G3KU_STAGE_RAW_V1`）详见 `context-and-cache-troubleshooting.md`「Prompt Cache Family 与 Actual Request」「压缩块的格式与字段语义」。

### Removed Semantic Summary Path

- 旧的语义/全局摘要 lane 不再参与 prompt assembly；`compression_state` 只表示内联 `token_compression` 的实时进度，不再是“语义摘要就绪”的 durable 信号。
- 续跑恢复依赖权威 frontdoor 基线、阶段状态、请求痕迹与收缩原因，不依赖单独的 `semantic_context_state` 交接块。
- 续跑 seed / 全量转录原始历史在拼接前先经过归属原位压缩（`_trim_frontdoor_seed_stage_compaction`，经 `compact_stage_prompt_messages_in_place`）：已离开可见层阶段的工具调用成对移除、块回插原位、对话保留，使存量大基线真正收缩，而不是携带未裁剪旧体重新膨胀。修剪需要阶段列表：优先取 `frontdoor_stage_state`，为空时退回 `frontdoor_canonical_context.stages`；两者都没有阶段时修剪是安全 no-op（不做有损删除），收缩交给 `token_compression` 兜底。排查“seed 从不收缩”时，先确认会话是否把阶段持久化进了这两个来源之一，再确认是否有阶段被点名裁撤或被收口（一条都没有时这里就是安全 no-op，正文只增不减）。

### Shrink-Guard Self-Heal（`context_shrink_quarantine`）

- finalize 前，运行时以同形归一方式比较下一轮请求体基线与会话基线：两侧都先剥掉工具契约消息、turn-only note 与多模态块，再估算 token。
- 若下一轮基线变短且收缩原因不是上面两个允许理由，守卫不裸抛异常冻结会话：它把拒绝后的新种子以受控原因 `context_shrink_quarantine` 写回会话基线，累计连续隔离计数并打警告日志，使后续回合的对比保持一致、会话可自愈。
- 连续隔离计数持续上升时，按 prompt 组装回归排查，不要解释成“正常上下文整理”。

### 压缩窗口：入站闸门与基线写入仲裁

- 内联压缩进行中手动 pause 对该可见轮次是终态：运行时取消活跃的压缩生成，丢弃迟到的压缩结果，不让它更新基线或继续进入主 provider 发送。
- 下一次激活（新用户输入、heartbeat 唤醒等）必须以当时的模型链与上下文窗口重新走 prepare → estimate → 可选压缩 → send。
- 手动压缩跑在回合外，且只在点击那一刻会话正在跑时才先 pause，所以"压缩在途"在会话状态上表现为一个空闲会话（`is_running`/`status` 全为 false）。入站因此不能只问 running：`RuntimeAgentSession.frontdoor_inbound_hold()` 是它的超集，额外认手动压缩的运行态；自动压缩不算 hold（它跑在回合内，那时本来就有回合在跑，判成 hold 会让自动车道自己等自己）。
- 三条入站车道问同一个判定，命中就绝不起回合：web WS 转入候选队列（`queue_follow_up_batch`）、渠道 `/api/v1`（QQ 官方 / onebot / openai-compat 都回环到这里）转入排队回执、heartbeat 沿用"忙则改期"。cron 刻意不加闸门：定时投递没有"稍后再说"的操作语义，排队等于把提醒改期到不可预期；它起的回合由下面的代号仲裁兜住，最坏情况是这一次压缩拒写而不是丢一次投递。
- durable 基线每前进一次换一代（`_frontdoor_baseline_revision`，只在唯一前进点递增）。手动压缩记下读取输入时的代号，落盘前对不上就不写，并回报 `baseline_advanced`：不落"已压缩"区分线，UI 给专门的文案让人等这条回复结束后再压一次。
- 为什么闸门还不够、必须再加代号：闸门只挡得住"压缩开始后到达的消息"，挡不住"压缩开始时已经在跑的回合"。渠道回合的任务刻意注册在 `None` 键上（真实键会让 pause 的 `cancel_session_tasks` 自我 gather 死锁），因此 pause 既停不掉它也等不到它，它照常会在摘要落盘之后用压缩前的种子把基线写回去。那种覆盖连摘要里的阶段收口水位线选择器一起抹掉，后果是压缩白做且收口也无从应用。宁可不缩，也不写一份"声称缩小、实际回退"的基线。
- 排队条目的 durable 记录是转录里的 `pending` 用户行：入队即写，不需要另建队列。可见用户回合开始前与会话构造时都会把仍是 pending、且该 turn 没有**真正的回答行**的条目按转录顺序接回队列（`source=runtime_error` 的错误行只证明回合跑坏了，不证明用户被回答过），接回时沿用行上的原始 `timestamp`——inflight 快照靠它报告送达时间，前端才能把气泡落在时间序上该在的位置。
- `pending` 升回 `completed` 有两条路，缺一不可：消费它的那一回合按 `turn_id` 就地升态（`_persist_turn_transcript` 遍历本批输入），以及**任何**正常完成回合收尾时把"内存队列已不再持有"的 pending 行统一退役。第二条不可省：内部（心跳/cron）回合会在 prepare 阶段消费排队的 follow-up，但它的完成回写整段跳过用户行，只靠第一条这些行永远停在 pending，下一次会话重建就把早已回答过的提问重新投喂，还会把当轮真正要回答的输入挤到批次中间。终态的授予权属于成功收尾路径：运行时错误路径既不退役也不升格，并把本轮没有拿到真实模型轮次的用户输入放回队列——内存队列是"仍打算派发"的真相，行只有被队列持有才受退役保护，否则下一次成功收尾会把它当成已消费的排队行翻掉，消息从此在模型上下文里消失。认领待重投输入的是可见用户回合车道，不是内部回合。网页队列上的撤回是唯一的删行入口（`withdraw_queued_follow_up`：内存队列与该 turn_id 的 pending 行一起消失，并重算 `commit_turn_counter`/`last_user_turn_at`），它不走上面两条升态路径，也只在条目尚未被 take/drain 时成立。转录状态机的取证与陷阱见 `context-and-cache-troubleshooting.md`「残留 paused / pending 转录条目与未回答的用户输入」。
- 排水通道共五条：两条请求内的（WS 的回合链、external 的排空循环，只有发起方进程还连着才有效），加三条"会话回到空闲"的——压缩收尾、WS 重连握手完成后、进程启动重放。启动重放只扫转录头一行加有界尾窗挑出值得构造的会话（全量构造所有会话去问一遍队列太贵），命中后逐个 await 而不是并发。派发器先问 hold 与工具审批，失败把条目放回队首；这三条缝都是低频事件，不构成重试热循环。派发出去的回合也遵守"恰好一个终态事件"的渠道契约，其 relay 只订阅在这次派发期间。

排查 prompt 连续性问题时的前两个问题：相关上下文是否仍在保留的 stage workset 内？若不在，内联 `token_compression` 或 `stage_compaction` 是否合法缩短了下一轮基线？基线与 artifact 的完整取证详见 `context-and-cache-troubleshooting.md`「Prompt Cache Family 与 Actual Request」。
