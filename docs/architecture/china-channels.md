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

## 7. 出站消息链路

Python 输出到渠道时，主要走：

1. 运行时产出 `OutboundMessage`
2. Web shell 中的 outbound drain 把 China channel 消息挑出来
3. `ChinaBridgeTransport.send_outbound(...)`
4. 转成 `deliver_message` frame
5. Node host 调对应平台 sender 发送

当前设计里：

- `_progress` / `_tool_hint` / `_session_event` 等内部消息不会直接发往渠道
- 只发送最终对用户可见的文本

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

## 10. 当前已知维护风险

### `controlToken` 默认值偏弱

schema 中默认是空串，说明 control WS 虽有认证流程，但默认部署下保护较弱。单机开发问题不大，但部署到更复杂环境时要显式配置。

### `sendProgress/sendToolHints` 目前更像保留字段

Python 侧 `send_outbound()` 会过滤 `_progress`、`_tool_hint`、`_session_event`，Node 侧也主要按最终可见消息发送。因此不要假设这两个开关现在已经形成完整可见行为。

### “注册表是单一事实来源”只实现了一部分

注册表确实是渠道集合的核心真相源，但 schema、某些 Web session 视图仍保留硬编码。新增或删除渠道时，不能只改 `channel_registry.json`。

### vendor 与 native 层边界必须守住

`src/vendor/*` 应尽量贴近上游。G3KU 自定义逻辑优先放在 native wrapper 层，否则后续同步 upstream 会非常痛苦。

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

## 12. 维护高风险区域

- `g3ku/china_bridge/transport.py`
  因为它是 Python 会话层与 Node 协议层的边界。

- `g3ku/china_bridge/supervisor.py`
  因为它同时管构建、进程、配置导出、状态写入。

- `subsystems/china_channels_host/src/vendor/*`
  这里应尽量贴近上游；不要把 G3KU 特化逻辑随意混进去。

- `subsystems/china_channels_host/src/host.ts`
  这是 Node host 的真正调度中心。
## Containerized China Bridge

Docker deployment does not split China bridge into a third long-lived service. The current contract remains:

- the web process or web container owns `ChinaBridgeSupervisor`
- the Node host still runs as a supervised child process of that web runtime
- the worker container does not own China bridge startup

This matters for image construction and troubleshooting:

- Node 20 and the package-manager toolchain belong in the web image build
- the built host entry `subsystems/china_channels_host/dist/index.js` should already exist in the image, rather than relying on first-request ad-hoc setup
- bridge runtime state still persists under `.g3ku/china-bridge/`

If containerized China bridge fails while the rest of the web runtime is healthy, debug in this order:

1. web container image contents for `subsystems/china_channels_host/dist/index.js`
2. web container Node / pnpm availability
3. shared `.g3ku/china-bridge/` status and logs
4. exported host runtime config under the bridge state directory
