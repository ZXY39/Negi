# G3KU External Agent API 接入手册

本文是给「任何专做渠道通信的项目/桥」看的接入说明。**它不是任何具体项目的预置适配**——渠道协议、登录、事件订阅、触发词、频控这些平台差异全部归你的项目，g3ku 只在你我之间保留一套固定的 HTTP + SSE 接口。照着这份手册，任何人/任何语言都能把桥接到 g3ku。

契约的权威定义在 `docs/architecture/external-agent-api.md`；本文是它的「怎么用」展开，两者冲突时以契约为准。

## 1. 边界与分工

| | 你的桥（渠道项目） | g3ku |
|---|---|---|
| 负责 | 平台登录、收发消息、事件订阅、触发词、频控、消息分段 | agent 逻辑、会话记忆、模型调用、工具执行、主动推送 |
| 语言/技术 | 任意，与 g3ku 无关 | Python |
| 与 g3ku 的往来 | 只用 HTTP + SSE，**零 g3ku import** | 只认 `/api/v1` + Bearer token |

接口是**项目无关**的：会话身份是你给的 `external_key`（随便什么字符串），g3ku 不关心它背后是 QQ 群、飞书单聊还是别的。

## 2. 一次最小接入闭环

1. **拿到 token**：在 g3ku Web「外部接入」面板签发（明文只显示一次），或由运维提供。记下 `bridge_id` 与 token。
2. **建会话**：`POST /sessions`，body 带 `external_key`，拿回 `session_id`（形如 `ext:{bridge_id}:{hash}`）。
3. **发消息**：`POST /sessions/{session_id}/messages`，带 `Idempotency-Key` 头，拿回 `turn_id` + `status`。
4. **订阅事件**：`GET /sessions/{session_id}/events`（SSE），收 `turn.started → reply.delta* → reply.final → turn.completed`。
5. **收终态**：看到 `turn.completed`（或 `turn.failed`）才算这一回合结束。
6. **验收**：`g3ku external sessions` 能看到映射；Web 会话列表渠道分组出现该会话（只读）。

## 3. 接口参考

- 基址：`http://{web.host}:{web.port}/api/v1`，默认 `127.0.0.1:18790`。
- 鉴权：所有请求带 `Authorization: Bearer <token>`。
- 会话键形态：`ext:{bridge_id}:{sha1(external_key)[:16]}`——**桥不要自行解析**，一律把 `session_id` 当不透明串用。

### 端点与请求/响应

| 方法 | 路径 | 请求 | 响应 | 错误 |
|---|---|---|---|---|
| POST | `/sessions` | `{"external_key":"…","title?":"…"}` | `{ok, session_id, external_key, title, created_at, created}` | 400 `external_key_required` |
| GET | `/sessions` | `?external_key=` | `{ok, bridge_id, items:[{session_id, external_key, title, created_at}]}` | — |
| PATCH | `/sessions/{id}` | `{"title":"…"}` | `{ok, session_id, title}` | 400 `title_required`、404 |
| DELETE | `/sessions/{id}` | — | `{ok, cleared, session_id}` | 404 |
| GET | `/sessions/{id}/state` | — | `{ok, session_id, external_key, running, queued_follow_ups, inflight_turn_id, last_error}` | 404 |
| POST | `/sessions/{id}/messages` | `{"text?":"…","attachments?":[…],"sender?":{id,name},"metadata?":{}}` | `{ok, session_id, turn_id, status}` | 400 `message_required`/`attachments_must_be_list`、404、503 |
| POST | `/turns/{turn_id}/pause` | — | `{ok, paused, turn_id, session_id}` | 404 `turn_not_found` |
| POST | `/sessions/{id}/cancel` | — | `{ok, cancelled, session_id}` | 503 |
| GET | `/sessions/{id}/events` | SSE，头 `Last-Event-ID` | 见 §4 | 404 |

鉴权错误码（`require_external_api`）：

| 状态 | detail | 含义 |
|---|---|---|
| 403 | `external_api_disabled` | 总开关 `externalApi.enabled=false` |
| 401 | `invalid_api_token` | 未带 token / token 不匹配 / 条目被禁用 |
| 423 | （全局锁中间件） | 项目已锁定 |
| 404 | `session_not_found` | 会话不存在或不属于本桥（越权一律 404） |

### curl 样例

```bash
BASE=http://127.0.0.1:18790/api/v1
TOKEN=<bridge token>

# 1) 建会话（幂等：同 external_key 重复调用返回同一个 session_id）
curl -s $BASE/sessions -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"external_key":"qq:dm:user-123","title":"张三的私聊"}'
# → {"ok":true,"session_id":"ext:napcat:a1b2c3d4e5f6a7b8","external_key":"qq:dm:user-123",...}

# 2) 发消息（Idempotency-Key 同键重复提交返回原 turn，status=duplicate）
curl -s $BASE/sessions/$SESSION_ID/messages -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" -H "Idempotency-Key: 20260907-0001" \
  -d '{"text":"你好"}'
# → {"ok":true,"session_id":"...","turn_id":"<hex>","status":"started"}

# 3) 订阅事件（SSE；断线重连带 Last-Event-ID 从上次 seq 之后回放）
curl -N $BASE/sessions/$SESSION_ID/events -H "Authorization: Bearer $TOKEN" \
  -H "Last-Event-ID: 3"
```

### POST /messages 的 `status` 语义

| status | turn_id | 含义 |
|---|---|---|
| `started` | 有 | 新回合已开跑，订阅事件收后续 |
| `queued` | `null` | 会话正忙，本次消息并入当前回合链（`receipt` 为回执文案） |
| `duplicate` | 有 | 同 `Idempotency-Key` 重复提交，`original_status` 是原回合状态 |

### 附件

`attachments` 数组每项：`{kind?, mime_type?, name?, path?|url?|data_base64?}`。`kind` 缺失时按 `mime_type` 前缀推断 `image/audio/video/file`。走 `data_base64` 落盘、单附件 ≤ 5 MiB；`path`/`url` 则只作引用。图片能否进模型由该会话所用模型绑定的 `image_multimodal_enabled` 决定。

## 4. 事件流（SSE）

- 帧格式：`id: {seq}\nevent: {type}\ndata: {json}\n\n`；`id:` 是单调 `seq`。
- 断线重连：带 `Last-Event-ID: {上次收到的 seq}` 从 `seq > N` 回放；缓冲默认 `eventBufferSize=512`，长断线超过窗口会丢，务必尽快重连。
- 心跳：约 15s 一条注释行保活。

事件公共字段 `{type, seq, ts, turn_id?}`：

| type | 附加字段 | 说明 |
|---|---|---|
| `turn.started` | — | 回合开始 |
| `turn.completed` | `cancelled?` | 终态：完成（`cancelled=true` 表示被暂停/取消） |
| `turn.failed` | `error`, `detail` | 终态：失败，`error` 为用户可读全文 |
| `reply.delta` | `text`, `source` | 最新思考段权威全文——**全量替换渲染，不追加** |
| `reply.final` | `text`, `source`, `usage?` | 权威全文（已出站清洗、媒体改写为签名 URL） |
| `progress` | `kind`, `text` | `kind: milestone/tool/tool_error`；g3ku 发全量，**节流是桥的职责** |
| `outbound.created` | `external_key`, `session_key`, `text`, `dedupe_key?` | 主动推送（cron/heartbeat/任务终态回流） |

**终态不变量**：每回合在**一切路径**上恰好发一个 `turn.completed` 或 `turn.failed`（含取消）。桥必须等到终态再发下一轮，或显式 pause/cancel；不要假设提交即串行排队。

## 5. 会话隔离与清除

- 每个桥只能看/操作自己 `bridge_id` 名下的会话，跨桥访问一律 404。
- `DELETE /sessions/{id}` 是**清除语义**：转录清空、内存失效、副产物全清，但注册表条目保留（下次同 `external_key` 复用同 session_id）。

## 6. 验收与排障

- `g3ku external status`：总开关 + 每桥 token 掩码 + 启停（锁定时显示占位提示）。
- `g3ku external sessions [--bridge X]`：注册表映射 external_key ↔ session_key。
- 桥收到 403：查 `externalApi.enabled` 与 token；401：token 不匹配或条目禁用；423：项目锁定。
- 提交成功但收不到回复：确认订阅的 SSE 会话与提交的是同一 `session_id`；事件是否被 `eventBufferSize` 淘汰。
- 主动推送不到：发布侧（heartbeat 日志）→ drain（`ext outbound drained`/dropped）→ 桥的 `outbound.created` 消费。
- 状态悬挂：查终态不变量是否被桥破坏。