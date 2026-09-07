# G3KU External Agent API 架构说明

本文档是 External Agent API（`/api/v1`，渠道无关的 headless agent 面）的唯一契约归属文档。渠道通信重建的第一期产物：默认保留 web 会话，IM 平台接入交给第三方桥接应用，g3ku 只暴露本契约。

## 1. 定位与边界

- 消费方是第三方桥接应用（IM bot、自动化桥），不是自家前端；web CEO 面（`/api/*`、`/ws/ceo`）不受本面影响。
- 平台协议、消息分段、频控、触发词识别全部属于桥接层；g3ku 核心不出现随平台变化的代码。判别准则：换一个 IM 平台就要改的代码不属于核心。
- 参考实现：`bridges/qq-onebot/`（独立包，零 g3ku import，行为对拍表见其 README）。

## 2. 启用与鉴权

- 配置段 `externalApi`：`enabled`（默认关，显式 opt-in）、`tokens`（bridge_id → `{token, label, enabled}` 的字典）、`eventBufferSize`（每会话事件环形缓冲，默认 512）。
- token 密文走 bootstrap secret overlay 三件套（保存时剥离、落盘只留占位、解锁时回填）；配置文件落盘时 `tokens[].token` 剥离进覆盖层。
- 请求带 `Authorization: Bearer <token>`；`require_external_api` 依赖（`g3ku/runtime/api/external_auth.py`）做常量时间比较，匹配启用的条目即注入 `bridge_id`。enabled=false → 403 `external_api_disabled`；token 缺失/错误 → 401。项目锁定仍由全局 423 中间件兜底。
- 每个桥只能看见/操作自己 `bridge_id` 名下的会话（越权访问一律 404）。
- 管理面：Web「外部接入」页（`g3ku/web/frontend/org_graph_external.js`）背后是 `main/api/admin_rest.py` 的 `/api/external-api/settings`（GET/PUT，总开关与缓冲大小）与 `/api/external-api/tokens`（POST 签发 / PATCH 改 label·enabled·`regenerate` / DELETE）。明文 token 只在签发/重新生成的响应里返回一次，其余读取只回掩码；保存走统一的 `save_config` + overlay 剥离链路。契约由 `tests/resources/test_external_api_admin_tokens.py` 锁定。

## 3. 会话注册表与 key 命名空间

- `ExternalSessionRegistry`（`g3ku/runtime/external_sessions.py`）维护 `(bridge_id, external_key) ↔ session_key` 双向映射，原子持久化在 `.g3ku/external-sessions/registry.json`。
- session key 形态 `ext:{bridge_id}:{sha1(external_key)[:16]}`（文件名安全；键里不含外部标识原文，真相只在注册表）。`build_external_session_key` 的 digest 长度参数只是注册表碰撞逃生口。
- `POST /sessions` 按 `external_key` 幂等 get-or-create；转录复用共享 `SessionManager`（`sessions/ext_*.jsonl`），无独立持久化机制。
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
| `outbound.created` | 出站总线（见 §6） | 主动推送 |

## 6. 出站路由（主动推送）

- 会话键为 `ext:` 的 heartbeat/cron/task-terminal 回复：`_notify_heartbeat_channel_reply` 的 ext 分支发布 `OutboundMessage(channel="ext", chat_id=<ext session key>)`；`_derive_session_channel_chat` 对 ext 键保留完整会话键为 chat_id。cron 零改动（payload `channel="ext"`、`to=<session key>` 即达）。
- 共享出站 drain（`g3ku/shells/web.py::_start_outbound_drain`）只路由 `ext`：注册表 `find_by_any_key`（接受会话键或 external_key）→ 该会话 hub 发 `outbound.created`；其他 channel 无消费方，告警跳过。
- drain 生命周期：`ensure_web_runtime_services` 拉起，仅进程关闭时取消。
- 未知目标告警丢弃；清洗后为空的纯内部文本静默 ack（不触发重试）。

## 7. 常见排障入口

- 桥拿到 403 `external_api_disabled`：配置 `externalApi.enabled` 与 tokens；401：token 不匹配或条目被禁用。
- 消息提交成功但桥收不到回复：确认桥订阅的 SSE 会话与消息提交的会话一致（同一 `session_id`）；看事件缓冲是否被 `eventBufferSize` 淘汰（长断线超过缓冲窗口）。
- 主动推送不到达：沿 §6 链路查——发布侧（`source=heartbeat` 日志）→ drain 分支（`external outbound drained` / dropped 告警）→ 桥的 `outbound.created` 消费。
- 终态缺失导致桥状态悬挂：属实现缺陷，对照 §4 终态不变量检查执行器改动。
