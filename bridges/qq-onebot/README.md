# QQ/OneBot 参考桥（Negi External Agent API）

独立桥接应用：把 NapCat / LLOneBot / Lagrange 的 OneBot 11 端点接到 Negi 的
`/api/v1` headless agent API。**零 g3ku import**——只经 HTTP + SSE 通信，
是渠道通信重建"平台代码不进核心"边界的参考实现。

## 运行前提

1. negi web 已启动，且 `.g3ku/config.json` 中启用 External Agent API 并发放 token：

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
| 运行中追发回执「收到，将在当前任务中一并处理。」 | 不转发：messages 回 `status:"queued"` 时桥保持静默（`receipt` 仍在响应里供对接方读，排队条目由服务端以 durable `pending` 行接住，会话回到空闲后一并回答） |
| progressMode 里程碑（5s 节流、≤3 行、🔧/⚠️） | 消费 `progress` 事件，桥侧按 `progress_min_interval_seconds` / `progress_max_lines_per_message` 节流合并（**g3ku 发全量，节流职责在桥**——这是重建的关键职责转移） |
| replyFinalOnly | `behavior.final_only=true`：忽略 progress，只发 `reply.final` |
| QQ 单条长度上限 | `split_outbound_text` 按 `max_message_length` 优先按行分段 |
| 入站图片 | OneBot image 段 → 下载 → base64 → `messages.attachments`（runtime 侧 `image_multimodal_enabled` 门控生效）。URL 由消息发送者写，抓取前按 socket 判目标，放行三档：① 公网地址；② **配置里的 donor**（`onebot.http_url` 那一台，示例部署就是 `http://127.0.0.1:3000`——群图片链接常由它发出，操作员已表达过的意图不再要求抄白名单）；③ `behavior.allowed_media_hosts` 点名的 `host[:port]`。其余一律拒取（日志 `inbound media URL rejected`，理由里写清怎么放行），其中最要紧的是同机其它私有服务：管理面对回环没有鉴权，`http://127.0.0.1:18790/api/llm/configs` 无 token 直接 200，放任意私网过去等于把这份读口借给群里任何会发消息的人。链路本地（含云元数据 `169.254.169.254`）、保留段、组播、未指定地址任何配置下都拒。比对按解析出的 IP:port 而非主机名：donor 写 `127.0.0.1:3000` 时 `localhost:3000` 会多带一枚 `::1` 端点而不算命中，那种写法请点进 `allowed_media_hosts` |
| 入站文件 | OneBot file 段（数组或 `[CQ:file,...]`）→ 取 `url`（群文件缺 URL 时经 `get_group_file_url` 换取）→ 下载（≤20MiB、每条消息至多 4 个附件）→ `kind:"file"` 提交；取不到链接时正文追加「[文件 x 未能获取]」提示，不静默吞 |
| 出站图片/文件 | 事件 `attachments`（见 external-agent-api.md「出站附件契约」）→ 桥下载签名媒体 URL：image 以 `[CQ:image,file=base64://...]` 段发送，其余走 `upload_private_file`/`upload_group_file`（base64 直传，无公网要求）；失败降级为签名链接文本行 |
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
→final；运行中追发→渠道端静默、会话空闲后一并回答；「暂停」→暂停生效；g3ku heartbeat 定时提醒→主动送达；
重启桥→SSE 重连不丢事件。
