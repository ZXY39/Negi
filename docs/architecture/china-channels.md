# G3KU China Channels 架构说明

本文档说明 G3KU 中中国渠道接入的总体架构，以及 Python 与 Node 两侧的职责边界。

## 1. 设计原则

当前系统明确采用单一 China communication runtime：

- Python 负责 Agent brain、会话、任务、记忆、治理。
- Node.js 负责国内 IM 平台 SDK、Webhook/WebSocket 接入、协议归一化和消息发送。

也就是说：

- Python 不直接接平台 SDK
- Node 不做 AI 决策

## 2. 关键目录

### Python 侧

- `g3ku/china_bridge/transport.py`
  Python 与 Node host 之间的消息桥。

- `g3ku/china_bridge/supervisor.py`
  负责构建、启动、监控 Node host。

- `g3ku/china_bridge/client.py`
  控制 WebSocket 客户端。

- `g3ku/china_bridge/session_keys.py`
  China 会话 key 规则。

- `g3ku/china_bridge/protocol.py`
  Python/Node 共识的 frame 协议。

### Node 侧

- `subsystems/china_channels_host/src/index.ts`
  Node host 入口。

- `subsystems/china_channels_host/src/host.ts`
  `ChinaChannelsHost` 主宿主。

- `subsystems/china_channels_host/src/vendor/*`
  平台 vendor 层，尽量贴近上游。

- `subsystems/china_channels_host/src/*.ts`
  G3KU native wrapper、注册表、bridge glue。

## 3. 支持的 canonical channel ids

统一使用以下 canonical ids：

- `qqbot`
- `dingtalk`
- `wecom`
- `wecom-app`
- `wecom-kf`
- `wechat-mp`
- `feishu-china`

维护上要注意：

- Python 和 Node 都应消费同一注册表
- 不要在新代码里手写散落的渠道集合

## 4. Python 与 Node 的边界

### Python 负责

- 会话路由
- Agent prompt / turn 执行
- 任务创建与取消
- memory 与 governance
- China bridge 启停、构建、状态输出

### Node 负责

- 平台 SDK 认证与连接
- webhook / websocket ingress
- 平台 outbound 发送
- 把 inbound 统一成 bridge protocol

这是维护时最重要的边界线。若问题是：

- 平台签名/回调/媒体发送异常，先看 Node host
- session 路由/模型回复/任务调度异常，先看 Python

## 5. 启动链路

在 Web runtime 内，China bridge 通常由 `g3ku/shells/web.py` 触发：

1. Web runtime 启动
2. 检查 `chinaBridge.enabled && autoStart`
3. 创建 `ChinaBridgeTransport`
4. 创建 `ChinaBridgeSupervisor`
5. Supervisor 检查 Node / package manager / build 产物
6. 必要时自动 `install` 和 `build`
7. 启动 Node 进程
8. Python 用 `ChinaBridgeClient` 连上控制 WebSocket

这意味着：

- China bridge 不是独立守护进程体系，而是由 Web runtime 管理
- 调试时既要看 Python 进程，也要看 Node 子进程

## 6. 入站消息链路

一条入站渠道消息大致经历：

1. 平台把消息打到 Node host
2. Node host 归一化成 `inbound_message`
3. Python `ChinaBridgeClient` 收到 frame
4. `ChinaBridgeTransport.handle_frame(...)`
5. 计算 `session_key` / runtime chat id / memory chat id
6. 构造 `UserInputMessage`
7. 调 `SessionRuntimeBridge.prompt(...)`

关键点：

- 渠道消息最终仍进入统一 `RuntimeAgentSession`
- China 渠道不是单独的 Agent 实现
- 入站用户内容必须保持「用户原始消息」。cron 隐藏契约注入已在宿主侧停用（`appendCronHiddenPrompt` 为 no-op），Python `ChinaBridgeTransport` 入站构造时会用 `strip_cron_hidden_prompt` 兜底剥除；cron 的正确使用规范由 cron 工具 toolskill 按需加载。若出现「用户消息里多出 cron 说明」或「定时相关消息答非所问」，见 §11「常见排障入口」。

### 渠道入站图片 / 媒体

- Node 侧把平台图片下载到本地并作为 attachments 归一化进 `inbound_message`；Python `ChinaBridgeTransport` 据此构造请求，生成 provider 可见的 `image_url` 块（`china_bridge_attachments` 元数据记录这些附件）。
- 渠道入站图片能否真正进入模型，和 Web 上传一样由所选模型绑定的 `image_multimodal_enabled` 门控：当前轮内容携带图片块且模型多模态开启时，frontdoor 即注入图片块；未注入时模型只能看到 `[Image: source: 路径]` 文本标记。
- 与 Web 一致：图片块只注入到达当轮的 live 请求，历史/持久化仍是纯文本，后续轮次不会自动重发图片像素。跨轮再次查看同一张图需要重新 `content_open` 该路径。
- 排障"渠道图识别错 / 编造内容"：先确认该轮请求是否真的携带 image 块（而不是只看文本标记），再确认模型绑定是否 `image_multimodal_enabled=true`。

### 回合完整性：终止帧契约

- 每条 `inbound_message` 的 `_run_turn(...)` 在**所有路径**上恰好发出一个终止帧（`turn_complete` 或 `turn_error`）。
- `asyncio.CancelledError` 是 `BaseException`，会穿过 `except Exception`（例如 Web 端暂停、任务被取消）；传输层单独捕获它，先发 `turn_complete` 再 re-raise。
- `turn_error` 帧的 `error` 字段是面向渠道用户的固定友好文案（`TURN_FAILED_FRIENDLY_TEXT`），原始异常文本放在 `detail` 字段，仅供排障、宿主不展示。渠道用户不会看到诸如 `Cannot operate on a closed database` 这类原始报错。
- 背景：宿主按 `event_id` 关联每回合的 pending Promise，每会话串行派发队列依赖 pending settle。终止帧缺失 → pending 永不 settle → 该会话后续消息永久排队。历史上真实卡死过：Web 端暂停 QQ 会话后，QQ 再发消息永久无响应。
- 排障「某渠道会话卡死不再响应」时，先确认对应回合的 Python 侧是否发出了终止帧。

## QQ 渠道增强（仅 qqbot）：暂停 / 即时读取 / 过程信息流

以下行为仅在 `envelope.channel == "qqbot"` 时启用（Python 传输层门控）；其他中国渠道保持原有行为。卡死修复（终止帧契约）对所有渠道生效。

### QQ 暂停命令

- 触发词：「暂停」/「暫停」/「pause」/「/pause」；带标点变体（「暂停。」）两侧归一化对齐（宿主 `isQQBotPauseCommandText` / Python `QQBOT_PAUSE_COMMANDS` + `_normalize_control_command_text`）。
- 宿主 `handleQQBotDispatch` 识别暂停 → `markSessionDispatchAbort`（abort 代数抑制被取消回合的残留回复）+ `runImmediateSessionDispatch` 绕过忙碌队列立即执行；**暂停不丢弃排队消息**（区别于 stop：stop 额外 `dropQueuedSessionDispatches`）。
- Python 侧经 `SessionRuntimeBridge.pause(session_key, manual=True)`：先做运行状态检查（`state.is_running` 或 `status == "running"`），空闲返回 0（避免空闲会话上 `session.pause(manual=True)` 产生多余转录归档）；运行中调用 `session.pause(manual=True)`。
- 回执：「已暂停。」/「当前没有正在进行的任务。」+ `turn_complete`。

### 运行中消息即时读取

- 宿主：`hasSessionDispatchBacklog(queueKey)` 为真时，运行中到达的消息不再排队等当前回合结束，而是 `runImmediateSessionDispatch` 立即派发。
- Python 分流：会话运行中 → `session.queue_follow_up_batch([msg], persist_transcript=True)` + 回执「收到，将在当前任务中一并处理。」+ `turn_complete`（frontdoor 在下一次调模型前注入排队内容，与 `websocket_ceo` 的 Web 行为一致）；空闲 → 正常新回合。
- 排空兜底循环：`prompt` 正常返回后 `drain_queued_follow_up_messages()` 可能仍有残留（消息到达时模型已进入最后一段生成）；循环 `prompt_batch` 续跑直至排空（`archive_follow_up_chain_transition` 记录回合链），整个回合对外只发一个 `turn_complete`。
- 保留语义：`session.cancel` 不清空 follow-up 队列；停止/暂停时已入队的消息会在下一回合继续跑。暂停恰好发生在入队与回执之间的窗口内，回执会被 abort 抑制但消息仍在队列（已知边缘情形）。

### 过程信息流（progressMode）

- 背景：QQ 端此前只能看到最终回复，工具调用与阶段进展不可见。QQ 官方机器人 API 不能编辑已发消息且有频率限制，因此实现为节流里程碑消息，而非 token 级流式。
- 每回合构造 `_QQBotProgressCollector` 收集事件：`tool_execution_start`（「🔧 …」）、`tool_execution_end` 出错 / `error`（「⚠️ …」）、`message_delta` 的 progress/analysis channel（跳过 `deep_progress` 高噪声内容）。
- 回合内单一 flush 任务按 `QQBOT_PROGRESS_MIN_INTERVAL_SECONDS`（5 秒）节流，合并至多 `QQBOT_PROGRESS_MAX_LINES_PER_FRAME`（3）行，以 `deliver_message(mode="progress", metadata.progress_kind="milestone")` 发出，**复用同一 `event_id`**（宿主按 event_id 关联 pending）。
- 宿主 `runtime_bridge.ts` 仅在对应 `event_id` 的 pending 存在时转发 progress 帧（回合结束后的残留过程帧是噪声，不走 lateDeliverRoutes）；`bot.ts` deliver 识别 `kind: "progress"`，以纯文本发送、绕过 C2C markdown 缓冲，同样受 abort 代数抑制。
- 配置：`channels.qqbot.progressMode: "off" | "milestones"`（默认 milestones）；`replyFinalOnly: true` 时进入最少回复模式——只投递最终回答，过程帧、非 final 中间回复与中途长任务提示（「任务处理时间较长…」）全部抑制。

## 7. 出站消息链路

Python 输出到渠道时，主要走：

1. 运行时产出 `OutboundMessage`（cron 提醒、heartbeat 渠道回复等发布到总线；普通回合回复由 `transport._run_turn` 直发，不经总线）
2. Web shell 中的 outbound drain 把 China channel 消息挑出来
3. `ChinaBridgeTransport.send_outbound(...)`
4. 转成 `deliver_message` frame
5. Node host 调对应平台 sender 发送

outbound drain（`g3ku/shells/web.py` 的 `_drain_outbound`）是总线出站队列的唯一消费者，维护契约：

- 发送失败绝不能杀死任务：控制 WS 未连通等临时错误（`RuntimeError`）保留消息、每秒重试；其他异常只丢弃该条消息并记 error 日志。历史根因正是该任务只捕两种异常、静默死亡且无重启，导致定时提醒永久滞留队列。
- `send_outbound(...)` 在 sender 未初始化时抛错而非静默返回，保证消息进入重试而不是无声丢失。
- 非 China 渠道（`channel not in CHINA_CHANNELS`）的消息不应到达 drain；若到达说明发布方解析出了错误渠道，会记 warning 并丢弃，不再静默。
- 发布/投递各有一条日志（`cron outbound published` / `china outbound drained`）；「队列有消息但渠道没收到」时先看这两条，再看是否出现 `skipped non-china message`。

发布方的 `channel` 必须来自任务/会话的正确渠道，而不是从 session key 粗略拆分。历史上 heartbeat 回合曾用 `key.split(":", 1)` 把 `china:qqbot:default:dm` 拆成 `channel="china"`，并用它覆写运行时会话元数据，导致后续回以 `channel="china"` 发布、被 drain 静默丢弃。现在：`_derive_session_channel_chat` 对 `china:*` 键走 china session-key 解析；`SessionRuntimeManager` 的会话元数据「首次注册生效」，后来者不得覆写所属传输层登记的权威 `(channel, chat_id)`。

Node 侧 `deliver_message` final 帧无对应 pending 回合时：qqbot 一律不复用 `lateDeliverRoutes` 里旧回合的 deliver 闭包（闭包内的 C2C markdown 缓冲是回合级状态，回合结束后无人冲洗，结构化文本会被永久困住；被动回复上下文也已过期），直接用帧自带的 channel/account/target 走平台主动发送兜底（`sendProactiveC2CMessage`）；其他渠道暂无主动兜底，仍按旧路由投递。兜底成功/失败分别有 `proactive fallback` 日志。`lateDeliverRoutes` 是内存表、宿主重启即清空——兜底同样覆盖这种"有帧无路由"场景。

当前设计里：

- `_progress` / `_tool_hint` / `_session_event` 等内部消息不会直接发往渠道
- 只发送最终对用户可见的文本
- 所有 `deliver_message` 帧在 `build_deliver_frame(...)` 统一清洗出站文本（`sanitize_channel_outbound_text`）：截断 `[SESSION EVENTS]` 之后的内容。清洗后为空说明整条消息都是内部文本，直接跳过投递——`send_outbound(...)` 对此正常返回（drain 视为已送达并 ack），不抛 `RuntimeError`（那会触发重试风暴）。
- 例外：QQ 过程信息流在回合内直接发 `mode="progress"` 帧（见上文「QQ 渠道增强」），不走 `send_outbound(...)`；旧的渠道事件 → 出站路径（`build_channel_outbound_message`）已废弃（恒返回 None 且有测试锁定），不要复活。

## 8. session key 规则

China 渠道 session key 由 Python 统一生成。

实现上的格式是：

- DM: `china:{channel}:{account_id}:dm`
- Group: `china:{channel}:{account_id}:group:{peer_id}`
- Thread: 在后面追加 `:thread:{thread_id}`

注意：

- DM key 当前是“合并 DM”模式，不带 `peer_id`
- 不要按旧文档口径假设 DM key 一定含对端 id

这非常重要，因为：

- 会话复用完全依赖这个 key
- 若 key 生成不稳定，会导致上下文割裂或错误复用

## 9. 配置与构建

Supervisor 会把 Python 当前 runtime config 导出为 host config，再传给 Node host。

关键点：

- Node host 的运行配置不是自己维护一套独立真相源
- 真相源仍是 Python 项目的 `.g3ku/config.json` 及运行时导出 payload

如果 bridge 行为与配置不一致，重点看：

- `build_runtime_config_payload(...)`
- `ChinaBridgeSupervisor._write_host_runtime_config()`

## 10. 维护风险与高风险区域

### `controlToken` 默认值偏弱

schema 中默认是空串，说明 control WS 虽有认证流程，但默认部署下保护较弱。单机开发问题不大，但部署到更复杂环境时要显式配置。

### `sendProgress/sendToolHints` 目前更像保留字段

Python 侧 `send_outbound()` 会过滤 `_progress`、`_tool_hint`、`_session_event`，Node 侧也主要按最终可见消息发送。因此不要假设这两个开关现在已经形成完整可见行为。

### “注册表是单一事实来源”只实现了一部分

注册表确实是渠道集合的核心真相源，但 schema、某些 Web session 视图仍保留硬编码。新增或删除渠道时，不能只改 `channel_registry.json`。

### vendor 与 native 层边界必须守住

`src/vendor/*` 应尽量贴近上游，不要把 G3KU 特化逻辑混进去；自定义逻辑优先放在 native wrapper 层，否则后续同步 upstream 会非常痛苦。

既有偏离记录：vendor `qqbot/bot.ts` 已含 G3KU 特化的会话派发队列（串行/立即派发、abort 代数抑制）与停止/暂停控制命令处理——这些逻辑与上游差异较大，同步 upstream 时需逐段比对。新增渠道控制类行为时先评估能否放在 native 层。

### 不要把 `_run_turn` 任务注册到真实 session key

暂停链路 `_run_turn → bridge.pause → session.pause → cancel_session_tasks → gather` 全程在暂停任务自己的调用栈上。若 `_run_turn` 任务以真实 session key 注册进会话任务表，暂停时会自我聚集（`gather` 等待自己）→ 永久死锁。保持 `register_task(None, task)` 现状（engine 忽略空 key）。

### 高风险文件

- `g3ku/china_bridge/transport.py`：Python 会话层与 Node 协议层的边界。
- `g3ku/china_bridge/supervisor.py`：同时管构建、进程、配置导出、状态写入。
- `subsystems/china_channels_host/src/host.ts`：Node host 的真正调度中心。
- `subsystems/china_channels_host/src/vendor/*`：见上条 vendor / native 边界。

## 11. 常见排障入口

### Node host 没拉起来

先看：

- `g3ku/china_bridge/supervisor.py`
- Node / pnpm/npm 是否存在
- `subsystems/china_channels_host/dist/index.js` 是否存在
- state 目录下 build/host 日志

### 能收消息，不能回消息

先看：

- `ChinaBridgeTransport.send_outbound(...)`
- Node host 对应 channel sender
- 平台 token / account 配置

### 能启动，但某个渠道完全没反应

先看：

- `channel_registry.json`
- 该渠道在 `chinaBridge.channels.<channel-id>` 下是否启用
- Node vendor 层该 channel 的 runtime / config / api 文件

### 消息进入了错误会话

先看：

- `g3ku/china_bridge/session_keys.py`
- `_run_turn(...)` 中 runtime chat id / memory chat id 的构造

### 用户消息里多出 cron 说明 / 定时相关消息答非所问

先看：

- Node host 的 cron 隐藏契约注入（`appendCronHiddenPrompt`）是否复活；它当前应为 no-op
- Python `ChinaBridgeTransport` 入站的 `strip_cron_hidden_prompt` 兜底剥除是否生效

### QQ 会话卡死 / 消息一直排队不处理

先看：

- 该回合 Python 侧是否发出了终止帧（`turn_complete` / `turn_error`）；CancelledError 漏发终止帧是历史根因（已修复，见 §6「回合完整性」）
- 宿主进程是否仍处于修复部署前的卡死状态：重启 China bridge 宿主与 Web runtime 解除
- 宿主日志是否按预期出现 `session busy; dispatching inbound immediately` / `session pause command detected; executing immediately`
- 运行中消息应收到回执「收到，将在当前任务中一并处理。」；若没有，确认 Python 侧 `get_existing_session` + 运行状态检查分支

### 定时提醒 / 任务结果 / 主动推送没送达（本地会话里能看到内容）

沿出站链路逐段看（§7）：

- console.log 有没有 `cron outbound published for job ...`：没有 → 发布侧未过门条件，查任务 payload 的 `deliver` / `channel` / `to` 与会话上下文注入
- 有没有 `china outbound drained: channel=...`：没有 → 先看是否出现 `china outbound drain skipped non-china message channel=china ...`；有 → 发布方渠道解析错误（历史根因是 heartbeat 用首个冒号拆 session key 并覆写会话元数据，现已修复），再看 drain 任务是否异常（历史根因是只捕两种异常、静默死亡且不重启，现有全异常防护 + 重试，`waiting for bridge connection` 属断连重试的正常告警）
- 任务结果（heartbeat 终态）推送还要确认心跳实例持有 `reply_notifier` 且事件未被滞留在未启动实例上：`build_web_session_heartbeat` 现复用存活实例而非每次重建
- host.out.log 有没有 `outbound action=...` 或 `late deliver_message sent via proactive fallback`：没有 → 帧未到宿主（查控制 WS 与 `.g3ku/china-bridge/status.json`）；`proactive fallback delivery failed` → 平台主动发送被拒（频率限制 / 主动推送权限）

## Containerized China Bridge

容器化部署不把 China bridge 拆成第三个长驻服务：`ChinaBridgeSupervisor` 仍由 web 进程 / web 容器持有，Node host 作为其受监管子进程运行，worker 容器不负责 China bridge 启动。整体容器部署契约详见 `web-and-admin.md`「Container Deployment Contract」。China bridge 特有要求：

- web 镜像构建需包含 Node 20 与包管理器工具链，且构建产物 `subsystems/china_channels_host/dist/index.js` 应已存在于镜像中，不依赖首次请求时的临时安装/构建。
- bridge 运行时状态仍持久化在 `.g3ku/china-bridge/` 下。

容器化 China bridge 失败而 web runtime 其余部分健康时，按此顺序排查：

1. web 镜像中是否包含 `subsystems/china_channels_host/dist/index.js`
2. web 容器内 Node / pnpm 是否可用
3. 共享的 `.g3ku/china-bridge/` 状态与日志
4. bridge 状态目录下的导出 host runtime config
