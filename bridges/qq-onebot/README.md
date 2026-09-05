# QQ/OneBot 参考桥（G3KU External Agent API）

独立桥接应用：把 NapCat / LLOneBot / Lagrange 的 OneBot 11 端点接到 G3KU 的
`/api/v1` headless agent API。**零 g3ku import**——只经 HTTP + SSE 通信，
是渠道通信重建"平台代码不进核心"边界的参考实现。

## 运行前提

1. g3ku web 已启动，且 `.g3ku/config.json` 中启用 External Agent API 并发放 token：

   ```json
   "externalApi": {
     "enabled": true,
     "tokens": { "qq-onebot": { "token": "<强随机串>", "label": "QQ 桥" } }
   }
   ```

2. NapCat（或其他 OneBot 11 协议端）已登录 QQ 并开启**正向 WebSocket + HTTP**
   （默认端口 3001/3000，见协议端文档）。

## 启动

```bash
cd bridges/qq-onebot
copy bridge.config.example.json bridge.config.json   # 填入 token 与端口
python -m qq_onebot_bridge --config bridge.config.json
```

## 行为对拍表（金标准 = 旧 China transport 的 QQ 语义）

| 旧语义 | 本桥实现 |
|---|---|
| 私聊/群聊会话隔离 | `external_key = qq:dm:{user_id} / qq:group:{group_id}`（群聊共享上下文） |
| 「暂停/暫停/pause//pause」+ 标点变体 | 桥侧归一化识别 → `POST /turns/{turn_id}/pause`；回执「已暂停。」/「当前没有正在进行的任务。」 |
| 「停止//stop」 | → `POST /sessions/{id}/cancel` |
| 运行中追发回执「收到，将在当前任务中一并处理。」 | messages 返回 `status:"queued"` 时转发 `receipt` |
| progressMode 里程碑（5s 节流、≤3 行、🔧/⚠️） | 消费 `progress` 事件，桥侧按 `progress_min_interval_seconds` / `progress_max_lines_per_message` 节流合并（**g3ku 发全量，节流职责在桥**——这是重建的关键职责转移） |
| replyFinalOnly | `behavior.final_only=true`：忽略 progress，只发 `reply.final` |
| QQ 单条长度上限 | `split_outbound_text` 按 `max_message_length` 优先按行分段 |
| 入站图片 | OneBot image 段 → 下载 → base64 → `messages.attachments`（runtime 侧 `image_multimodal_enabled` 门控生效） |
| event_id 重推去重 | OneBot `event_id`（缺省 `ob:{message_id}`）→ `Idempotency-Key` |
| heartbeat/cron 主动推送 | 消费 `outbound.created` → 主动消息 |
| @触发 | `group_require_at=true` 时群聊需 @机器人 才触发（at 段从正文剔除） |

## 事件流与重连

每会话一条持久 SSE（`GET /api/v1/sessions/{id}/events`），断线按
`Last-Event-ID` 从 g3ku 事件环形缓冲回放续订；OneBot WS 断线自动重连
（`reconnect_backoff_seconds`）。

## 测试

```bash
python -m pytest bridges/qq-onebot/tests -q
```

真机验证清单（需 QQ 凭据 + NapCat）：私聊/群聊各一轮 发消息→progress 里程碑
→final；运行中追发→回执；「暂停」→暂停生效；g3ku heartbeat 定时提醒→主动送达；
重启桥→SSE 重连不丢事件。
