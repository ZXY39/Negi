# G3KU External Agent API 架构说明

本文档是 External Agent API（`/api/v1`，渠道无关的 headless agent 面）的唯一契约归属文档。渠道通信重建的第一期产物：默认保留 web 会话，IM 平台接入交给第三方桥接应用，g3ku 只暴露本契约。

## 1. 定位与边界

- 消费方是第三方桥接应用（IM bot、自动化桥）；唯一内置例外是官方 QQ 适配器（见「内置官方 QQ 适配器」），它同样只经本契约消费。web CEO 面（`/api/*`、`/ws/ceo`）不受本面影响。
- 平台协议、消息分段、频控、触发词识别全部属于桥接层；g3ku 核心不出现随平台变化的代码。判别准则：换一个 IM 平台就要改的代码不属于核心。
- 参考实现：`bridges/qq-onebot/`（独立包，零 g3ku import，行为对拍表见其 README）。

## 2. 启用与鉴权

- 配置段 `externalApi`：`enabled`（默认关，显式 opt-in）、`tokens`（bridge_id → `{token, label, enabled}` 的字典）、`eventBufferSize`（每会话事件环形缓冲，默认 512）。
- token 密文走 bootstrap secret overlay 三件套（保存时剥离、落盘只留占位、解锁时回填）；配置文件落盘时 `tokens[].token` 剥离进覆盖层。
- 请求带 `Authorization: Bearer <token>`；`require_external_api` 依赖（`g3ku/runtime/api/external_auth.py`）做常量时间比较，匹配启用的条目即注入 `bridge_id`。enabled=false → 403 `external_api_disabled`；token 缺失/错误 → 401。项目锁定仍由全局 423 中间件兜底。
- 每个桥只能看见/操作自己 `bridge_id` 名下的会话（越权访问一律 404）。
- 管理面：Web「外部接入」页（`g3ku/web/frontend/org_graph_external.js`）背后是 `main/api/admin_rest.py` 的 `/api/external-api/settings`（GET/PUT，总开关与缓冲大小）与 `/api/external-api/tokens`（POST 签发——自定义 token 可省略、省略时服务端自动生成，与存量 token 重复返回 409 `token_exists` / PATCH 改 label·enabled·`regenerate` / DELETE）。明文 token 只在签发/重新生成的响应里返回一次，其余读取只回掩码；保存走统一的 `save_config` + overlay 剥离链路。契约由 `tests/resources/test_external_api_admin_tokens.py` 锁定。只读诊断命令 `g3ku external status` / `g3ku external sessions` 提供掩码与注册表视图（锁定时显示占位提示），供 shell 与 agent exec 验收连通状态。渠道对接总纲（架构边界、token 交付纪律、协议速览、新桥构建清单、排障）由 skill `skills/g3ku-bridge-onboarding/` owning，其中 `references/integration-manual.md`（接入手册：curl 样例与事件/错误码参考）、`references/building-a-bridge.md`（适配层编写指南）为权威正文；web CEO 目录对 `ext:*` 会话只读分组展示，详见 `web-and-admin.md`「External Agent API」段。

## 3. 会话注册表与 key 命名空间

- `ExternalSessionRegistry`（`g3ku/runtime/external_sessions.py`）维护 `(bridge_id, external_key) ↔ session_key` 双向映射，原子持久化在 `.g3ku/external-sessions/registry.json`。
- session key 形态 `ext:{bridge_id}:{sha1(external_key)[:16]}`（文件名安全；键里不含外部标识原文，真相只在注册表）。`build_external_session_key` 的 digest 长度参数只是注册表碰撞逃生口。
- `POST /sessions` 按 `external_key` 幂等 get-or-create；转录复用共享 `SessionManager`（`sessions/ext_*.jsonl`），无独立持久化机制。首次创建成功与 `PATCH` 改名成功后，以尽力而为方式向 web CEO 端推送一次全局 `ceo.sessions.snapshot`（web 运行时未就绪时静默跳过，不影响接口返回）——浏览器只在页面加载时拉取 REST 会话列表，新渠道会话的实时可见性依赖该推送。
- `ext:` 前缀在 `session_agent.py` 的 frontdoor 连续性前缀表中；web 侧对 `ext:` 会话与 legacy `china:` 归档同语义（目录分组、只读拒绝），判定收口在 `g3ku/runtime/session_keys.py::is_channel_session_key`。
- 会话 key 编解码的规范实现位于核心模块 `g3ku/runtime/session_keys.py`（`china:*` 格式与历史转录字节级一致，存量转录保持可读）。

## 4. 回合契约

- `POST /sessions/{id}/messages` 异步提交：空闲 → 启动回合并返回 `{turn_id, status:"started"}`；运行中 → `queue_follow_up_batch` 排队并返回 `{status:"queued", receipt:"收到，将在当前任务中一并处理。"}`。
- 执行走 `SessionRuntimeBridge.prompt/prompt_batch`（与 web/CLI/cron 同一语义基座）。
- **终态不变量**：每回合在全部路径上恰好发一个 `turn.completed` 或 `turn.failed`；`asyncio.CancelledError` 单独捕获、先发终态再上抛（缺终态曾卡死旧宿主的按会话串行队列，此为硬契约）。`turn.failed.error` 是用户可读全文（空则回退友好文案），`detail` 供排障。
- 排空兜底：prompt 返回后循环 `drain_queued_follow_up_messages` → `archive_follow_up_chain_transition` → `prompt_batch` 续跑，整条回合链对外只有一个终态。
- 回合任务以 `register_task(None, task)` 注册：以真实 session key 注册会在暂停时被 `cancel_session_tasks` 的 gather 自聚集死锁。
- `Idempotency-Key` 头去重：同会话同键重复提交返回原 `turn_id` + `status:"duplicate"`（进程内有界映射）。
- 附件：`data_base64` 落盘 `.g3ku/external-uploads/<session>/`（单附件 ≤5MiB）；图片构造 `image_url` 块，能否进模型由模型绑定 `image_multimodal_enabled` 门控（与 web 上传同语义）。
- 控制端点复用桥语义：`POST /turns/{turn_id}/pause`（running guard，空闲返回未暂停）、`POST /sessions/{id}/cancel`。
- `DELETE /sessions/{id}` 是清除语义（对齐 web-and-admin.md「Channel Session Clear Contract」）：转录清空、内存失效、side artifacts 全清，注册表条目保留。

## 5. 事件流

- 每会话一个 `SessionEventHub`（`g3ku/runtime/external_events.py`）：有界环形缓冲 + 单调 `seq` + 订阅扇出；事件形态 `{type, seq, ts, turn_id?, ...}`。
- `GET /sessions/{id}/events` 为 SSE（`id:` = seq）；断线以 `Last-Event-ID` 回放续订；15s 心跳注释行保活。
- 事件集合：

| 事件 | 来源 | 说明 |
|---|---|---|
| `turn.started` / `turn.completed` / `turn.failed` | 回合执行器 | 终态恰好一个；`turn.completed.cancelled=true` 表示被暂停/取消 |
| `reply.delta` | `assistant_stream_delta` | text 为最新思考段权威全文（桥做全量替换而非追加） |
| `progress` | `message_delta`（progress/analysis）、工具开始/出错 | `kind: milestone/tool/tool_error`；**g3ku 发全量，节流是桥的职责** |
| `reply.final` | `message_end` | 权威全文：经出站清洗 + 本地引用改写为签名媒体 URL（`ceo_media` HMAC token）；内部心跳 ack（heartbeat_internal）不转发 |
| `outbound.created` | 出站总线（见「出站路由（主动推送）」） | 主动推送 |

## 6. 出站路由（主动推送）

- 会话键为 `ext:` 的 heartbeat/cron/task-terminal 回复：`_notify_heartbeat_channel_reply` 的 ext 分支发布 `OutboundMessage(channel="ext", chat_id=<ext session key>)`；`_derive_session_channel_chat` 对 ext 键保留完整会话键为 chat_id。cron 零改动（payload `channel="ext"`、`to=<session key>` 即达）。
- 共享出站 drain（`g3ku/shells/web.py::_start_outbound_drain`）只路由 `ext`：注册表 `find_by_any_key`（接受会话键或 external_key）→ 该会话 hub 发 `outbound.created`；其他 channel 无消费方，告警跳过。
- drain 生命周期：`ensure_web_runtime_services` 拉起，仅进程关闭时取消。
- 未知目标告警丢弃；清洗后为空的纯内部文本静默 ack（不触发重试）。

## 7. 内置官方 QQ 适配器（qq-official）

- `g3ku/qq_official/` 是 g3ku 唯一内置渠道桥，`bridge_id` 固定 `qq-official`，以 in-process 桥身份消费本契约；QQ 协议收敛在该模块内，核心其余部分不出现 QQ 代码。
- 配置段 `qqBot`：`enabled`、`appId`、`appSecret`、`sandbox`。`appSecret` 与 `externalApi.tokens[].token` 同走 overlay 三件套（保存剥离、落盘占位、解锁回填）。管理面：Web「外部接入」页的官方 QQ 机器人面板，对应 `main/api/admin_rest.py` 的 `/api/qq-bot/settings`（GET/PUT；appSecret 只写，PUT 空串表示保留原值）与 `/api/qq-bot/status`。
- 生命周期：`QqOfficialService` 随 web 运行时 refresh 启停（`g3ku/shells/web.py`），状态机 `enabled_off / not_configured / connecting / connected / error`；启动时若 `externalApi.tokens.qq-official` 缺失则自动签发并 `save_config`。
- 运行入口是硬约束：botpy 的阻塞入口 `Client.run()`（内部对构造时捕获的 loop 调 `run_until_complete`）在已运行的 web 事件循环上会抛 "This event loop is already running"；桥必须走异步入口 `async with client: await client.start(...)`，使 botpy 与 uvicorn 共享同一事件循环，事件回调（`on_*`）因此可直接驱动回环 `/api/v1` 客户端。
- 消息流：入站 `on_*` 事件按 external_key 映射建会话（`qq:group:<group_openid>` / `qq:c2c:<user_openid>` / `qq:guild:<guild>:<channel>` / `qq:guilddm:<guild>:<author>`），经 `Idempotency-Key: qq-<消息id>` 提交回合；消息中的图片附件（botpy `message.attachments` 中 `content_type` 为 `image/*` 且带绝对 http(s) URL 的条目）由桥下载后作为 `data_base64` 附件经本契约转发（单附件 ≤5MiB、每条消息至多 4 张；下载失败或超限降级为仅文本提交，纯图片消息照常提交）；每会话一条 SSE pump 消费 `reply.final` 与 `outbound.created`（主动提醒消费分支，路由见「出站路由（主动推送）」），分别经 `post_message` / `post_group_message` / `post_c2c_message` / `post_dms` 投递。
- 主动提醒约束：只能推给已注册过会话的目标（该用户/群先与机器人产生过消息）；频率受 QQ 开放平台主动消息额度与回复时间窗规则约束。
- `qq-botpy` 在核心依赖（pyproject `dependencies`）里，`pip install -e .` 即装；唯一 `import botpy` 的模块是 `bridge.py` 且为惰性导入，环境缺失时服务报 `error` 状态，不会拖垮 web 运行时。

## 8. 常见排障入口

- 桥拿到 403 `external_api_disabled`：配置 `externalApi.enabled` 与 tokens；401：token 不匹配或条目被禁用。
- 消息提交成功但桥收不到回复：确认桥订阅的 SSE 会话与消息提交的会话一致（同一 `session_id`）；看事件缓冲是否被 `eventBufferSize` 淘汰（长断线超过缓冲窗口）。
- 主动推送不到达：沿「出站路由（主动推送）」链路查——发布侧（`source=heartbeat` 日志）→ drain 分支（`external outbound drained` / dropped 告警）→ 桥的 `outbound.created` 消费。
- 终态缺失导致桥状态悬挂：属实现缺陷，对照「回合契约」终态不变量检查执行器改动。
- QQ 机器人面板报错/不连接：先看 `/api/qq-bot/status` 的 detail——AppID/AppSecret 错误表现为登录失败，intents 未开通表现为网关拒绝；出现 "This event loop is already running" 说明桥被改回了阻塞 `Client.run()` 入口。
