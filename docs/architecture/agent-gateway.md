# G3KU Agent Gateway 架构说明（OpenAI 兼容端点 + MCP 网关）

本文档是「外部 AI agent 开箱即用对接面」的唯一契约归属文档：OpenAI 兼容端点（`POST /api/v1/chat/completions`、`GET /api/v1/models`）与 MCP stdio 网关（`g3ku mcp serve`）。底层回合/事件/鉴权契约属于 `external-agent-api.md`「回合契约」「事件流」，本文只写网关层叠加的语义；操作向快速上手见 skill `skills/g3ku-bridge-onboarding/references/integration-manual.md`「开箱即用集成」。

## 1. 定位与边界

- 两个对接面都是 External Agent API 之上的**薄适配**：同一套 `externalApi.tokens` Bearer 鉴权（`require_external_api`）、同一套 `ext:{bridge_id}:{hash}` 会话注册表、同一个 `ExternalTurnService` 回合执行器。零新配置段、零新依赖（`mcp` SDK 是既有核心依赖）。
- 分工：OpenAI 兼容端点服务"只会说 OpenAI 协议"的客户端（AstrBot/LangBot/各语言 SDK/框架）；MCP 网关服务 MCP 客户端（Claude Code、Cursor 等）。需要平台事件、主动推送、分段/频控的渠道仍然走自建桥（`external-agent-api.md` + bridge-onboarding skill）。
- OpenAI 兼容端点是 web 运行时**进程内** router（`g3ku/runtime/api/openai_compat.py`，挂载于 `g3ku/web/main.py`）；MCP 网关是**独立 stdio 进程**（`g3ku/mcp_gateway/` + `g3ku/shells/mcp_cli.py`），经 HTTP+SSE 消费 `/api/v1`，不 import web 运行时、不驻留事件泵。
- 每个对接方发独立 `bridge_id` token（如 `claude-code`、`astrbot`）：会话命名空间天然隔离、可单独吊销。

## 2. OpenAI 兼容端点契约

挂载在 `/api/v1` 前缀下（客户端 `base_url=http://{host}:{port}/api/v1`），因此继承全局项目锁中间件（锁定时 423）与前端 catch-all 豁免。无 CORS（服务端到服务端调用）。

### 会话映射与消息取舍

- `external_key = openai:{body.user|default}`（`user` 截 128 字符）→ 注册表 get-or-create，会话持久（g3ku 自持记忆）。会话首次创建时尽力推送一次 web CEO 目录快照（侧栏可见性）。
- **只转发最后一条 user 消息**：客户端携带的历史（含旧的 assistant 回复）一律忽略——重复注入会污染 g3ku 会话记忆。多会话隔离靠 `user` 字段。
- `system` 消息仅在该会话**首次创建**的第一回合前置进消息文本；之后忽略（g3ku 的记忆与提示词体系接管）。
- `model` 字段仅回显，不参与路由（g3ku 自持模型链选择）；`GET /models` 恒返回单模型 `g3ku`（同样要求 Bearer）。
- 图片：user content parts 里的 `image_url`——`data:` URL 解码落盘为附件（与 `/messages` 同一条 5MiB→413 链路），http(s) URL 作引用附件；其他 scheme → 400 `unsupported_image_url`。
- `Idempotency-Key` 请求头透传给回合提交（OpenAI SDK 原生支持），重复提交按 `external-agent-api.md`「回合契约」幂等语义处理；重复且原回复仍在事件缓冲时直接从缓冲返回。

### 等待与应答语义（200-诚实文本策略）

- 提交前先取 `after_seq = hub.last_seq`，提交后经 `wait_for_external_reply`（`g3ku/runtime/external_events.py`）等待，规则见「等待契约」。
- **回合级结果一律 HTTP 200**：正常回复、等待超时（"still working" + turn_id）、回合失败（"[g3ku] turn failed: …"）、被暂停/取消、完成但无可见回复，都以诚实 assistant 文本 + `finish_reason:"stop"` 返回，并附非标准顶层 `g3ku` 对象 `{session_id, turn_id, created_session, status, submit_status}` 供机读（status ∈ completed|running|failed|cancelled|no_reply|queued_receipt）。理由：5xx/504 会触发 OpenAI SDK 自动重试（默认 `max_retries=2`），重试通常不带幂等键 → 重复回合适暴；200 让调用方模型自己决定等待/追问。**不得把超时改回 504。**
- HTTP 错误只留给请求级失败：400/413/503 用 OpenAI `{"error":{message,type,param,code}}` 形状；401/403（鉴权依赖）与 423（锁中间件）保持平台既有 `{"detail":…}` 形状。
- `wait_seconds` 可选 body 字段（SDK 经 `extra_body` 传），clamp 5..3600，默认 600（与 OpenAI SDK 默认请求超时对齐）。
- 客户端断连：停止等待，**绝不取消回合**——回复照常落会话历史，可经 `/api/v1` SSE 回放取回。

### 流式（stream=true）

- 帧为 OpenAI `chat.completion.chunk`（首块 `delta.role`，内容块 `delta.content`，末块 `finish_reason:"stop"`，终止 `data: [DONE]`）；15s 心跳注释行。
- 增量算法：`reply.delta.text` 是**最新思考段的全量文本**（替换语义，见「事件流」），网关对"当前段已发文本"做前缀差分；段切换（新文本不以旧文本为前缀）发 `"\n\n"+新段全文`。`reply.final` 仅在其扩展了最后一段时补尾差。
- 已知局限：流式 `reply.delta` 是未消毒原文，出站消毒（`reply.final` 才做）要剥除的内容可能已经上线且无法撤回；多段思考时拼接文本与 `reply.final`（只含末条消息全文）可以不一致。对文本一致性敏感的调用方用非流式。
- 提交发生在构造流响应之前：客户端中途断开不取消回合。

## 3. 等待契约（wait_for_external_reply）

进程内"提交 → 等终稿"原语，网关两面共用同一套规则（MCP 侧是其 SSE 客户端镜像 `G3kuMcpClient.wait_for_reply`）：

- **无竞态次序**：调用方先取 `after_seq = hub.last_seq`，再 submit；等待器先 subscribe、先消费 `replay(after_seq)` 再进 live 队列，seen-seq 集合去重（publish 在锁外扇出，到达顺序不保证——匹配按类型/turn_id，选择按 max-seq，从不按到达序）。
- **started**：等 `seq > after_seq` 且 turn_id 匹配的第一个 `reply.final`。
- **queued**（消息并入在跑回合链）：链上**前一条消息**的 final 会先到，drain 批次在同一 turn_id 下追加新 final、且发生在唯一终态 `turn.completed` 之前——规则是收集 finals 直到终态、取 max-seq 的那条。**"等下一个 final"对 queued 是错的。**
- **duplicate**：原 final 可能已在缓冲（seq ≤ after_seq），先反扫 `replay(0)` 找匹配 turn_id 的 final，找不到再以 `after_seq=0` 活等。
- `turn.failed` → failed（error 为用户可读全文）；`turn.completed(cancelled)` 且无 final → cancelled；完成但无 final → no_reply。
- 边界：`after_seq` 捕获到 replay 之间超过事件缓冲（默认 512，`externalApi.eventBufferSize`）会丢事件 → 等待超时。

## 4. MCP stdio 网关契约

- 进程模型：`g3ku mcp serve --token <t> [--base-url http://127.0.0.1:18790/api/v1] [--conversation-prefix mcp]`；token 可由 env `G3KU_EXTERNAL_TOKEN` 提供。独立轻量进程，连**运行中的** web 运行时；`g3ku mcp check` 提供接入前连通性自检（回显 bridge_id 与会话数）。
- **stdout 纯净铁律**：stdio 上 stdout 是 JSON-RPC 协议通道，serve 路径一切提示走 stderr（`typer.echo(..., err=True)`；loguru 与 FastMCP 日志均 stderr-only）。任何 stdout 字节都会毁帧。
- 会话映射：`conversation` 参数 → `external_key = mcp:{conversation}`；每次工具调用内"开 SSE 流 → 发消息 → 有界等待 → 关流"（流先开=零事件间隙；无驻留泵）。
- 工具面（`g3ku/mcp_gateway/server.py`，全部返回 dict、**永不抛异常**——HTTP/传输错误转 `{ok:false, error, status_code?}` 可读载荷）：

| 工具 | 语义 | 关键返回 |
|---|---|---|
| `g3ku_chat(conversation, message, wait_seconds=120)` | 发送并等待终稿 | `status: completed\|pending\|queued_receipt\|failed\|cancelled\|no_reply` + `reply`/`receipt`/`error`/`turn_id`/`usage`；pending 带续取 hint |
| `g3ku_get_reply(conversation, wait_seconds=60)` | 续取未消费的下一个终稿 | `found` + `reply` |
| `g3ku_session_status(conversation)` | 运行/排队快照 | `running`、`queued_follow_ups`、`inflight_turn_id`、`last_seq` |
| `g3ku_pause(conversation)` | 暂停在跑回合 | 无 inflight → `{ok:false, error:"no_inflight_turn"}` |
| `g3ku_cancel(conversation)` | 取消会话任务 | `cancelled` 计数 |
| `g3ku_list_conversations()` | 本 bridge 会话列表 | `conversation`（external_key 剥前缀） |

- 客户端接入示例：`claude mcp add g3ku -- g3ku mcp serve --token <t>`（或等价 MCP JSON 配置）。

## 5. 安全注记

- token 即命名空间：明文只出现在签发响应与对接方自己的配置/env 里；g3ku 侧走 bootstrap secret overlay（`config-and-models.md`），日志不落 token。
- 网关不扩大数据面：出入只有对话文本与附件（5MiB 上限），出站文本沿用 relay 的清洗与媒体签名 URL 改写；系统提示词、工具面、任务树不经网关暴露。
- 423 项目锁先于鉴权生效（`/api/*` 全域）；锁定时两个对接面都不可用，MCP 工具收到可读错误而非挂起。

## 6. 常见排障入口

- OpenAI SDK 报 401/403：token 不匹配 / `externalApi.enabled=false`；423：项目锁定；503 `runtime_unavailable`：web 运行时未就绪。
- 回复总是 "still working"：回合确实还在跑（长任务）——加大 `wait_seconds`、改用 `stream=true`，或稍后对同一 `user`/conversation 再发一条询问结果；确认不是事件缓冲溢出（`eventBufferSize`）。
- MCP 工具全部 `connection_failed`：web 运行时没在跑或 `--base-url` 错误——先 `g3ku mcp check`。
- MCP 客户端报协议错误/解析失败：几乎总是有东西写了 stdout——检查是否改动了 serve 路径的输出（铁律见「MCP stdio 网关契约」）。
- 流式文本与最终回复不一致：多段思考 + 段切换分隔符是预期行为；对一致性敏感改用非流式（「流式」局限）。
- 同一会话串话：多个调用方共用同一 `user`/`conversation` 键——按调用方分配独立键或独立 bridge token。
