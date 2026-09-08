# 适配层编写指南（任何渠道项目 → G3KU）

接入手册（`integration-manual.md`）讲了接口；本文讲**怎么写那座桥**——即把「某个专做渠道通信的项目」接到 G3KU 的那层胶水。平台差异全部收敛在这一层，g3ku 核心一行不改。

## 1. 适配层职责边界

桥 = 三层，职责严格分离：

```
平台适配层（渠道协议）──► 翻译层（一律转为 /api/v1 语义）──► G3kuClient（HTTP+SSE）
```

- 平台登录、收发、事件订阅、触发词、频控 → **平台适配层**（你的项目/你的代码）。
- 事件/消息与 g3ku 会话、回合、事件的互相转换 → **翻译层**（dispatcher）。
- 只认 HTTP+SSE 的 g3ku 调用 → **客户端**（照抄 `g3ku_client.py` 即可）。

**铁律**：桥零 g3ku import；桥与 g3ku 只经 `/api/v1` + Bearer token；token 不进任何仓库、不进日志。

## 2. 通用变换规则（不变量，任何平台照此写）

| 方向 | 平台侧 | 变换为 g3ku 侧 |
|---|---|---|
| 身份 | 群 ID / 用户 ID / 会话 ID | 拼成不透明 `external_key`（一个 external_key = 一个 g3ku 会话，dm/group/thread 的隔离粒度由桥定） |
| 入站 | 一条平台消息 | `POST /sessions`（幂等）→ `POST /messages`（带唯一 `Idempotency-Key`，重试复用同键） |
| 回复 | 需要展示给用户 | 订阅 SSE，`reply.final` 为权威全文（`reply.delta` 做流式全量替换，`progress` 做里程碑） |
| 媒体 | 平台附件字节 | `attachments[].data_base64`（≤5MiB），kind 按 mime 推断 |
| 推送 | 平台「发消息」能力 | 消费 `outbound.created` 主动推送 |

**必须遵守的纪律**：

1. **终态纪律**：等 `turn.completed`/`turn.failed` 再发下一轮，或显式 pause/cancel；不要假设提交即串行排队。
2. **幂等纪律**：每次用户消息生成唯一 `Idempotency-Key`，网络重试复用同键（同键重发返回原 turn，`status=duplicate`）。
3. **重连纪律**：SSE 断线即重连，带 `Last-Event-ID` 从上次 `seq` 后回放；`reply.delta` 是全量替换而非追加。
4. **节流纪律**：g3ku 发全量 progress，节流/合并是桥的职责（参考 `progress_min_interval_seconds`）。
5. **触发纪律**：@ 触发、白名单、频控都在桥侧（参考 `group_require_at`）。

## 3. 参考实现：bridges/qq-onebot 结构地图

它是官方参考样例（stand-alone 包，零 g3ku import）。要接新平台，**只需换第 1 层的 transport，第 2/3 层照抄**：

| 文件 | 角色 | 接新平台时 |
|---|---|---|
| `config.py` | `load_bridge_config` 读 `bridge.config.example.json`；三层 config dataclass | 改字段（平台连接参数） |
| `onebot.py` | `OnebotClient`：平台适配层——`receive_events`（WS/HTTP 收事件）、`send_private_msg`/`send_group_msg`/`call_action`/`download_bytes` | **整体替换**成目标平台的 SDK/transport |
| `dispatcher.py` | `Dispatcher`：翻译层——`external_key_for_event`（事件→external_key）、`handle_onebot_event`（入站→POST messages，@触发门控）、`_handle_g3ku_event`（SSE→平台回复）、`progress_loop`/`_ProgressBuffer`（节流）、`split_outbound_text`（分段） | 基本照抄，只改事件字段映射 |
| `g3ku_client.py` | `G3kuClient`：`ensure_session`/`send_message`/`pause_turn`/`cancel_session`/`stream_events`/`last_seq` | **原样照抄**（协议无关） |
| `__main__.py` | `run`/`main`：装配三层并启动 | 改装配 |

## 4. 最小可跑骨架（伪代码）

```python
# 0) 配置：base_url / token / bridge_id（token 来自外部接入面板，明文只出现一次）
# 1) 平台事件循环（替换成你平台的订阅方式）
async for raw_event in platform.subscribe_events():
    external_key = to_external_key(raw_event)          # 平台事件 → 会话身份
    text, attachments = to_message(raw_event)          # 平台消息 → 文本/附件
    if should_ignore(raw_event):                       # 触发词/@ 白名单/频控，都在这里
        continue
    session_id = await g3ku.ensure_session(external_key)
    await g3ku.send_message(session_id, text, attachments, idempotency_key=new_key())

# 2) 每个已经建过的会话起一条 SSE 消费循环
async for event in g3ku.stream_events(session_id, last_event_id=seen):
    if event["type"] in ("reply.final", "reply.delta", "progress"):
        await deliver(event)                            # 翻译回平台并发送
    elif event["type"] == "outbound.created":
        await deliver(event)
```

## 5. 验收清单（桥写完后逐条过）

- [ ] `g3ku external sessions` 出现 external_key ↔ session_key 映射。
- [ ] 一条平台消息 → g3ku 收到 → SSE `turn.started → … → turn.completed/failed`。
- [ ] 同 `Idempotency-Key` 重发返回 `status=duplicate`，不重跑。
- [ ] 断线重连后 `Last-Event-ID` 回放不丢终态。
- [ ] 图片/附件走 `data_base64` 能进模型（受 `image_multimodal_enabled` 门控）。
- [ ] 群聊 @ 触发、频控、消息分段按平台要求生效。
- [ ] Web「会话列表」渠道分组出现该会话（只读）。

## 6. 如果那项目只认 OpenAI 后端怎么办

g3ku 核心自带 OpenAI 兼容端点：`POST /api/v1/chat/completions` + `GET /api/v1/models`（同一套 `externalApi` Bearer 鉴权）。只把 agent 当 OpenAI 兼容后端的项目（AstrBot/LangBot 这类）直接把 `base_url` 指到 `http://{host}:{port}/api/v1`、`api_key` 填 bridge token 即可，不需要写桥。契约（会话映射、等待/超时、流式语义）见 `docs/architecture/agent-gateway.md`，接入样例见 `integration-manual.md`「开箱即用集成」。

自写桥仍然是正确选择的场景：平台有自己的事件/推送模型（主动提醒、群事件、卡片交互）、需要分段/频控/触发词等平台侧逻辑，或需要双向长连接。仅"单向问答 + OpenAI 协议"不要写桥。