# G3KU External Agent API 架构说明

本文档是 External Agent API（`/api/v1`，渠道无关的 headless agent 面）的唯一契约归属文档。渠道通信重建的第一期产物：默认保留 web 会话，IM 平台接入交给第三方桥接应用，g3ku 只暴露本契约。

## 1. 定位与边界

- 消费方是第三方桥接应用（IM bot、自动化桥）；唯一内置例外是官方 QQ 适配器（见「内置官方 QQ 适配器」），它同样只经本契约消费。web CEO 面（`/api/*`、`/ws/ceo`）不受本面影响。
- 本契约之上另有两个开箱即用的 agent 对接面（OpenAI 兼容端点 `POST /api/v1/chat/completions`、MCP stdio 网关 `g3ku mcp serve`），契约详见 `agent-gateway.md`。
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
- `ext:` 前缀在 `session_agent.py` 的 frontdoor 连续性前缀表中。目录分组与「历史不可改」两轴收口在 `g3ku/runtime/session_keys.py::is_channel_session_key`（legacy `china:` 归档同语义）。第三轴「能否向该会话投递输入」由注册表决定，与只读判定无关：`/ws/ceo` 对 `ext:` 会话按 `get_external_session_registry().get_by_session_key()` 解析一次（连接期），有条目即接受网页输入、按「回合契约」提交，无条目（`china:*`、注册表丢失的孤儿转录）回 `channel_session_readonly`。
- 会话 key 编解码的规范实现位于核心模块 `g3ku/runtime/session_keys.py`（`china:*` 格式与历史转录字节级一致，存量转录保持可读）。

## 4. 回合契约

- `POST /sessions/{id}/messages` 异步提交：空闲 → 启动回合并返回 `{turn_id, status:"started"}`；会话被 hold（有回合在跑，**或该会话正在跑一次手动上下文压缩**）→ `queue_follow_up_batch` 排队并返回 `{status:"queued", receipt:"收到，将在当前任务中一并处理。"}`。渠道不因此丢消息：排队条目以转录 `pending` 行为 durable 记录，会话回到空闲后由派发通道发出（合同见 `runtime-overview.md`「压缩窗口：入站闸门与基线写入仲裁」）。
- 执行走 `SessionRuntimeBridge.prompt/prompt_batch`（与 web/CLI/cron 同一语义基座）。
- 提交方可以是 HTTP 桥，也可以是 `/ws/ceo` 的网页输入车道（进程内经 `get_external_turn_service()` 调 `submit`，不经 Bearer 鉴权中间件）。两条发起方共用 `submit` 这一处裁决：入站 hold、幂等位（网页发起方不带 `Idempotency-Key`，`duplicate` 分支对它不可达）、排队回执、以及「回合契约」的终态不变量。回合的出站能力长在车道上而不是发起方上：`make_session_event_relay` 只在 `_execute_turn` 挂载，所以网页输入必须走这条车道，否则 hub 上没有 `reply.final`，表现为「网页看得到回复、渠道端什么都没有」。
- 出站只承载 agent 回复与主动推送：**发起方的用户消息不回显到渠道**（事件集合里没有 user-echo 类型，投递它需要经总线走 `outbound.created`，那是主动消息额度）。网页在渠道会话里的接管只在转录与 web 侧可见。
- **终态不变量**：每回合在全部路径上恰好发一个 `turn.completed` 或 `turn.failed`；`asyncio.CancelledError` 单独捕获、先发终态再上抛（缺终态曾卡死旧宿主的按会话串行队列，此为硬契约）。`turn.failed.error` 是用户可读全文（空则回退友好文案），`detail` 供排障。失败不作废这次提交：该输入在转录里保持未回答态，由下一个可见用户回合接回后回答（状态机见 `context-and-cache-troubleshooting.md`「残留 paused / pending 转录条目与未回答的用户输入」）。`turn.failed` 不在桥的投递事件集合里（见「内置官方 QQ 适配器」消息流），宿主若要把失败原因告诉用户，得自己消费事件流，g3ku 不替它发。
- 排空兜底：prompt 返回后循环 `drain_queued_follow_up_messages` → `archive_follow_up_chain_transition` → `prompt_batch` 续跑，整条回合链对外只有一个终态。`prompt_batch` 只以批次最后一条输入驱动回合，较早输入的内容块在请求构建期并入（合同见 `runtime-overview.md`「prompt_batch 批次回合内容合并」）。
- 回合任务以 `register_task(None, task)` 注册：以真实 session key 注册会在暂停时被 `cancel_session_tasks` 的 gather 自聚集死锁。
- `Idempotency-Key` 头去重（进程内有界映射）：同会话同键重复提交返回 `status:"duplicate"` + `original_status`——原回合记录仍在则回报其状态与 `turn_id`；排队条目回报 `queued`（无 `turn_id`）；记录已被淘汰的带 id 条目回报 `completed`。排队提交同样占幂等位：否则同一条渠道消息在回合运行期间重试/重发会反复入队，用户收到多份重复回复。回合记录表有界，超限从最旧终态记录淘汰、运行中记录永不淘汰；幂等条目不随记录淘汰失效。
- 附件：`data_base64` 落盘 `.g3ku/external-uploads/<session>/`，双上限——`kind:"image"` ≤5MiB（与 web 上传同一常量），其余文件类 ≤20MiB（`EXTERNAL_FILE_UPLOAD_MAX_BYTES`），超限一律 413 `attachment_too_large`；桥侧按同值预过滤。图片构造 `image_url` 块，能否进模型由模型绑定 `image_multimodal_enabled` 门控（与 web 上传同语义）；非图片附件只以「本地路径提示」文本块告知模型（落盘路径可直接用工具读盘），不构造多模态内容块。
- 控制端点复用桥语义：`POST /turns/{turn_id}/pause`（running guard，空闲返回未暂停）、`POST /sessions/{id}/cancel`。
- `DELETE /sessions/{id}` 是清除语义（对齐 web-and-admin.md「Channel Session Clear Contract」）：转录清空、内存失效、side artifacts 全清，注册表条目保留。

## 5. 事件流

- 每会话一个 `SessionEventHub`（`g3ku/runtime/external_events.py`）：有界环形缓冲 + 单调 `seq` + 订阅扇出；事件形态 `{type, seq, ts, turn_id?, ...}`。
- `GET /sessions/{id}/events` 为 SSE（`id:` = seq）；断线以 `Last-Event-ID` 回放续订；15s 心跳注释行保活。hub 的 seq 是内存态、随服务端重启清零：`Last-Event-ID` 若越过本进程已发布 seq（旧进程遗留的大序号），端点按全量重放处理——第三方桥重连即收到缓冲内全部积压，不因旧序号被静默过滤。
- 事件集合：

| 事件 | 来源 | 说明 |
|---|---|---|
| `turn.started` / `turn.completed` / `turn.failed` | 回合执行器 | 终态恰好一个；`turn.completed.cancelled=true` 表示被暂停/取消 |
| `reply.delta` | `assistant_stream_delta` | text 为最新思考段权威全文（桥做全量替换而非追加） |
| `progress` | `message_delta`（progress/analysis）、工具开始/出错 | `kind: milestone/tool/tool_error`；**g3ku 发全量，节流是桥的职责** |
| `reply.final` | `message_end` | 权威全文：经出站清洗 + 附件提取（见下）+ 剩余本地引用改写为签名媒体 URL（`ceo_media` HMAC token）；可携带 `attachments`；内部心跳 ack（heartbeat_internal）不转发 |
| `outbound.created` | 出站总线（见「出站路由（主动推送）」） | 主动推送，可携带 `attachments` |

出站附件契约（`reply.final` 与 `outbound.created` 通用）：正文中以 markdown 链接/图片语法引用、且解析为**存在的本地文件**的条目，在事件发布前被提取为 `attachments` 数组项 `{name, mime_type, size, url}`——`url` 是根相对的签名媒体链接（`/api/ceo/media/original?token=...`，24h 时效，桥按自己的服务端 origin 拼绝对地址下载），原 markdown 标记在正文中替换为其标签文本（无标签用文件名）。单条事件最多提取 4 个附件（超出与重复路径保留原样，走签名改写成为可点击兜底链接）。**仅** markdown 引用触发，裸路径不提取——桥收到 `attachments` 时应作为真实文件/图片消息投递，投递失败的条目降级为签名链接文本行（详见「内置官方 QQ 适配器」与桥自身文档）。普通回合的 `reply.final` 在 relay 侧提取；`outbound.created` 在出站 drain 统一提取（见「出站路由（主动推送）」）。

## 6. 出站路由（主动推送）

- 会话键为 `ext:` 的 heartbeat/cron/task-terminal 回复：`_notify_heartbeat_channel_reply` 的 ext 分支发布 `OutboundMessage(channel="ext", chat_id=<ext session key>)`；`_derive_session_channel_chat` 对 ext 键保留完整会话键为 chat_id。cron 零改动（payload `channel="ext"`、`to=<session key>` 即达）。
- 共享出站 drain（`g3ku/shells/web.py::_start_outbound_drain`）只路由 `ext`：注册表 `find_by_any_key`（接受会话键或 external_key；**external_key 不保证全局唯一**，同一键命中多条时按 `created_at` 取最新并落一条 WARNING——被点名说明有推送/定时任务仍指着已被取代的旧会话键，需要改指）→ **持久 outbox 登记**（见下）→ 该会话 hub 发 `outbound.created`（事件携带 `outbox_id`，提取到的附件携带 `attachments`，见「事件流」出站附件契约）；其他 channel 无消费方，告警跳过。附件提取发生在 drain 这一统一出口：首次出站从正文提取；启动重放/对账重注入的消息自带账本持久化的 `attachments`（正文已是提取后文本），直接透传不二次提取——心跳、cron、任务终态等全部主动推送因此自动获得文件投递能力，`reply_notifier` 与 `OutboundMessage` 形态保持纯文本。日志 `external outbound published to hub` 只代表事件已进内存 hub，**不代表已送达渠道**；送达以桥侧回执日志为准（QQ 适配器为 `qq-official delivered ...`）。发布时 hub 无任何订阅者则升级为 WARNING（`no live subscriber`）：live 扇出必然蒸发，投递交由持久 outbox 周期对账兜底（见下）。
- drain 生命周期：`ensure_web_runtime_services` 拉起，仅进程关闭时取消。outbox 对账循环（`_outbox_reconcile_loop`，60s）与其同生命周期、同款幂等启动器（`_ensure_outbox_reconcile_running`），关停时先于桥服务收割（防对账在 shutdown 后复活桥）；每轮先幂等复活 drain——drain 死亡时重放进总线无人路由，对账把它降级为 ≤60s 自愈。
- 未知目标告警丢弃；清洗后为空的纯内部文本静默 ack（不触发重试）。

### 持久 outbox（`.g3ku/external-outbox/outbox.jsonl`）

bus/hub 全是内存态：桥 pump 断连或进程重启窗口里滞留的主动推送会随内存清空蒸发。`g3ku/runtime/external_outbox.py` 是这条链路的持久账本，投递语义 **at-least-once**：

- 账本为 append-only jsonl：`{"kind":"msg",...}` 登记记录（含 `text`、提取到的 `attachments` 描述符）+ `{"kind":"ack","id":...}` 销账 tombstone。登记失败（如磁盘满）降级为仅内存投递并记 error，绝不阻断 hub 发布。
- drain 在发布 hub 前登记每条出站；桥在渠道 API 确认送达后调 `POST /sessions/{id}/outbox/{outbox_id}/ack` 销账（会话作用域防越权、幂等）。ack 丢失的代价是下次重启后重复投递一次。
- 启动重放：`ensure_web_runtime_services` 在 drain 拉起后把 24h 时效内未 ack 的条目带原 `outbox_id` 重新注入总线（drain 复用该 id，不重复登记），过期条目标记 `expired`，随后压实账本。
- 周期对账（`_outbox_reconcile_loop`，每 60s）：启动重放是一次性的，覆盖不了「重启时账本为空、滞留推送在重启后才入账」的窗口（重启落在 cron 触发与 agent 产出之间即如此）。每轮对账执行过期清理（`expired` 判定不依赖重启）；把 pending 中「年龄超过 120s 且对应会话 hub 无订阅者」的条目带原 `outbox_id` 重新注入总线——有订阅者说明 pump 在线，SSE `Last-Event-ID` 重放已兜底，再注入只会制造重复副本。重注入按记录指数退避（60s 起步、1h 封顶），压制永久无消费者会话（openai-compat 会话走同一账本但从不开 SSE）的重复注入噪声；到达桥侧的重复副本由 pump 的 `outbox_id` 去重吸收（见「内置官方 QQ 适配器」）。活动轮立即压实账本、稳态每小时压实一次，append-only 文件不随运行时间无界增长。
- `GET /outbox/pending` 返回本 bridge 名下会话的 pending 清单（只带路由身份 `outbox_id/session_id/external_key/ts`，不带正文——正文经 SSE 重放投递），供桥启动首跑与常驻对账循环重建 pump（见「内置官方 QQ 适配器」）。
- 账本只覆盖走出站总线的主动推送（`outbound.created`）；普通回合的 `reply.final` 仍只依赖 hub 环形缓冲（断线超过 `eventBufferSize` 即被逐出），其可达性由桥侧 pump 的重连契约兜底。

## 7. 内置官方 QQ 适配器（qq-official）

- `g3ku/qq_official/` 是 g3ku 唯一内置渠道桥，以 in-process 桥身份消费本契约；QQ 协议收敛在该模块内，核心其余部分不出现 QQ 代码。一个 QQ 号 = 一个 AppID = 一条 bridge_id `qq-official-<appId>`（`messages.py::bridge_id_for_app_id`），号与号之间不共享任何句柄。bridge 身份决定三件事：用哪条 `externalApi.tokens` 凭证、会话键的 bridge 段、以及 registry / outbox 的可见集作用域——因此一个号的 pump 与周期对账按构造看不见另一个号的滞留推送（多号共用一条 bridge_id 时才会出现的互抢形态：拿别人的 openid 去投、5 次重试后跳毒消息）。QQ 开放平台的 openid 按 AppID 隔离，同一自然人在两号下是两个 `external_key`、两个会话、两份记忆作用域；跨号身份打通平台不提供手段（官方口径是"后续提供跨 AppID 绑定"）。
- 配置段 `qqBot`：`enabled`（总开关）与 `accounts`（以 AppID 为键的字典，每项 `appSecret / sandbox / enabled / label`）。每号的 `appSecret` 与 `externalApi.tokens[].token` 同走 overlay 三件套（保存剥离、落盘占位、解锁回填），覆盖层键是 `config.qqBot.accounts.<appId>.appSecret`。旧的单账号形状（顶层 `appId/appSecret/sandbox`）由 `g3ku/config/loader.py::_migrate_legacy_qq_bot_account` 折叠成 `accounts[<appId>]` 并复存一次；**折叠必须发生在覆盖层回填之后**，早于回填时账号折出来了却拿不到密钥，而保存会清掉那条旧覆盖层键，密钥就此丢失。管理面：Web「外部接入」页的官方 QQ 机器人面板按账号列行，对应 `main/api/admin_rest.py` 的 `/api/qq-bot/settings`（GET 返回 `accounts[]`，每行带掩码、`has_secret` 与该号的服务态；PUT 是整表替换，某行 `appSecret` 传空串表示保留该 AppID 的原密钥）与 `/api/qq-bot/status`（同样按号返回）。整表替换后不再对应任何账号的 `qq-official*` token 被置为停用（不删除，重新加回同号可复用）。
- 生命周期：每个账号一个 `QqOfficialService` 实例，注册表在 `g3ku/shells/web.py`（`_global_qq_official_services`，以 bridge_id 为键）随 web 运行时 refresh 与配置做 diff：新增建实例、签名变更重启、从配置里消失则 `stop()` 并摘除。状态机 `enabled_off / account_disabled / not_configured / connecting / connected / error` 按号独立（"总开关关"与"这一号被停用"必须可区分，否则管理面上一个号停用会把整列报成未启用）；启动时若该号的 `externalApi.tokens.<bridge_id>` 缺失则自动签发并 `save_config`。重启签名包含密钥摘要，因此只改 AppSecret 也能让停在旧凭证重试循环里的号被重建。桥异常崩溃由 `_run` 自身按退避重连（见下条「崩溃自愈契约」），不依赖外部复活；`sync_from_config` 负责的是桥任务已终结后的复活（`_run` 干净返回即终止循环），其触发点为 runtime refresh、启动序列与 outbox 对账循环（每 5 轮 ≈5 分钟），环境修好后因此最迟分钟级重新建桥。**sync 幂等是硬约束**：`_prompt_locked` 在每一个回合开头都调 `refresh_web_agent_runtime`，而后者无条件调 `_sync_qq_official_service`，所以每条入站消息都会 sync 一次自家桥——一次"配置未变也重启"的 sync 等价于每条消息拆掉自己所在的桥。已采纳的签名只能被真正的配置变化推翻，`stop()` 不清它（`_restart` 先写签名再 `await stop()`，在 `stop()` 里清空会让幂等判据永远不命中）；重启由"签名不同"决定，复活由"`_task` 为空或已终结"决定，两条判据互不依赖。`_sync_qq_official_service` 对整次 diff 持**一把**模块级 asyncio 锁而不是每号一锁：diff 必须原子，且 `sync_from_config` 在 `_restart` 的 `await stop()` 窗口内 `_task=None`，无锁的并发调用会各自建桥、先建者沦为无人持有的孤儿任务（双 botpy 连接、每条消息双份投递）。单号 `sync_from_config` 抛异常只记日志，不阻断其余账号建桥。
- 崩溃自愈契约（`service.py::_run`）：桥以异常终结（如 botpy 的 http 层对登录超时/限流只记 WARNING 返回 `None`，`Robot(None)` 抛 `AttributeError`）时，服务层按 1s→60s 指数退避自动重连，`error` 状态 detail 保留异常摘要与「将在 Ns 后重试」；崩溃前健康运行超过 60s 则退避重置回起始值，长期健康后的单次崩溃不被放大。桥**干净返回**是它自报的环境类终态（botpy 缺失/intents 不兼容），不重试；配置变更与停用由 `stop()` 取消任务终结循环，不走本重试路径。
- 运行入口是硬约束：botpy 的阻塞入口 `Client.run()`（内部对构造时捕获的 loop 调 `run_until_complete`）在已运行的 web 事件循环上会抛 "This event loop is already running"；桥必须走异步入口 `async with client: await client.start(...)`，使 botpy 与 uvicorn 共享同一事件循环，事件回调（`on_*`）因此可直接驱动回环 `/api/v1` 客户端。
- 消息流：入站 `on_*` 事件按 external_key 映射建会话（`qq:group:<group_openid>` / `qq:c2c:<user_openid>` / `qq:guild:<guild>:<channel>` / `qq:guilddm:<guild>:<author>`），经 `Idempotency-Key: qq-<消息id>` 提交回合——缺消息 id 时不带幂等键提交，绝不回退 external_key：external_key 对同一用户恒定，回退会撞掉该用户首条消息的幂等位并永久丢弃消息。提交返回 `status:"queued"`（会话正忙、消息已排队）时，桥必须把回执投递给用户（响应 `receipt` 为空则用兜底文案），静默会让用户以为消息被吞而重复发送。消息附件（botpy `message.attachments` 中带绝对 http(s) URL 的条目，每条消息至多 4 个）由桥下载后作为 `data_base64` 附件经本契约转发：`image/*` 为 `kind:"image"`（≤5MiB），其余 content_type（文档等）为 `kind:"file"`（≤20MiB），上限与 `/api/v1` 双上限一致；下载失败或超限降级为仅文本提交，纯附件消息照常提交。`audio/*` 是唯一不进附件车道的类型：本机语音识别就绪时桥在提交回合前就地转写（同一事件循环内的进程调用，不经 HTTP），文本以 `用户语音，机器识别结果：` 前缀并入本条正文，识别失败另起 `用户语音，机器识别失败：<原因>`——两条都必须带来源标记，否则听错的语音与手打文字在模型眼里同形、且用户以为机器人"听见了却装没听见"；失败另起措辞是因为把失败写进"机器识别结果："等于让模型把一句失败说明当成用户说的话。未就绪时语音退回上面的 `kind:"file"` 形状，能力开关不允许造成回退。QQ 媒体 URL 在事件落地那一瞬间不一定可读（实测同一条语音链接先抛异常、几分钟后返回 `200 audio/mp3`），因此附件下载带一次延时重试；重试仍失败且语音就绪时，正文写 `用户语音，机器识别失败：语音附件下载失败`——纯语音消息此时正文与附件皆空，`on_incoming` 会早退，用户端就是彻底的静默。每条带附件的入站消息都会 INFO 记录各附件的 `content_type` 列表，因为"事件没到"与"附件取不到"事后只能靠这行区分。转写引擎与容器格式约束见 `speech-to-text.md`。每会话一条 SSE pump 消费 `reply.final` 与 `outbound.created`（主动提醒消费分支，路由见「出站路由（主动推送）」），文本经 `post_message` / `post_group_message` / `post_c2c_message` / `post_dms` 投递。
- 出站附件投递：事件携带 `attachments`（见「事件流」出站附件契约）时，附件先于正文投递。群聊/单聊：桥先从本机签名媒体 URL 取字节（≤20MiB），再以 **`file_data`（base64）直传**平台媒体上传接口（`/v2/{groups,users}/…/files`）——不回源，服务端只监听回环地址也成立；botpy 的 `post_group_file` / `post_c2c_file` 包装只暴露 `url` 参数（那条路径由 QQ 服务器回源下载、要求公网可达），故桥用 SDK 自身的 `Route` + `http.request` 直发并把它留作回退。拿到 `file_info` 后以 `msg_type=7` 富媒体消息发出。mime→`file_type` 映射：图片→1、mp4→2、语音→3、其余（文档/压缩包等）→4。任一环节失败（取字节失败、超 20MiB、`file_data` 与 `url` 两条上传路径都被拒）该附件降级为签名下载链接文本行并入正文，用户至少拿到可下载链接，附件不静默丢失。频道/频道私信无文件接口：图片下载后经 `file_image` 字节直发，其余降级签名链接文本。
- 主动提醒约束：只能推给已注册过会话的目标（该用户/群先与机器人产生过消息）；频率受 QQ 开放平台主动消息额度与回复时间窗规则约束。普通回合的 `reply.final` 与主动提醒同样是 `msg_type=0` 的主动投递形状（桥内没有 `msg_id` 被动回复路径），所以**网页发起的回合也受这个窗口与额度约束**：平台拒投时按 pump 的毒消息上限跳过，症状是网页有完整回复、QQ 端什么都没有。
- 每会话 SSE pump 的存活契约（`bridge.py::_pump`）：流异常**或干净结束**（服务端重启/空闲关闭）都按 1s→60s 指数退避自动重连。重连与入站幂等重建是硬契约：pump 一旦终结且无人重建，该会话出站事件滞留内存 hub 无人消费，形成「入站正常、出站黑洞」的僵尸态。这条流的读超时独立于客户端默认值（`client.py` 的 `SSE_STREAM_READ_TIMEOUT_SECONDS`，90s）：服务端只在 `SSE_HEARTBEAT_INTERVAL_SECONDS`(15s) 发一条 keep-alive 注释行，沿用 30s 默认读超时会让任何一次事件循环抖动（大会话转录重写即如此）掐断这条流，把真故障埋进重连噪音；因此读超时按「容得下 3 次以上心跳缺席」取值，且 `httpx.TimeoutException` 走单行 WARNING（`qq-official event stream idle timeout ...`）而非异常栈。投递成功才推进 `seqs`：失败靠服务端 `Last-Event-ID` 重放自动重试（节奏随重连退避），同一事件连续失败 5 次判为毒消息，记 error 后跳过、不阻塞后续事件。`outbound.created` 另有 `outbox_id` 维度投递历史（进程内有界 LRU，1024 个 id）：已 ack 的 id 之重复副本（服务端周期对账/重启重放注入）直接跳过，用户不会收到重复推送；同一 id 跨 seq 累计投递失败达上限（5 次）后，其后续副本零尝试丢弃——重放副本各带新 seq，仅靠 per-seq 计数会让毒消息随每份副本重新获得完整重试预算。ack 失败不进去重集：副本可再投，维持 at-least-once。
- pump 建立时机：首条入站消息、每次入站的幂等存活校验（`_spawn_pump` 按 session 记录任务，已死则重建）、桥启动时按 `GET /outbox/pending` 的对账首跑、以及常驻对账循环（每 30s 轮询同一清单，为「有 pending 记录且无存活 pump」的会话重建 pump）。常驻循环覆盖启动首跑扑空的窗口：服务端重启落在 cron 触发与产出之间时账本尚空，滞留推送稍后才入账，而入站消息可能迟迟不来——周期对账保证分钟级补投。list 失败只记 warning，对账循环绝不因单轮失败而死。桥终结（重启/停用/崩溃收尾）会随 `finally` 取消它名下的全部 pump，而回合回复 `reply.final` 只在进程内 hub 的有界缓冲里（不落盘、不记 seq 水位），所以"回合在跑时桥被重建" ⇒ 这条回复此后无人消费，症状是网页有完整回复而渠道端永久静默，直到该会话下一条入站消息重建 pump 才按 `last_seq=0` 把整个缓冲重放一遍（其中已投递过的事件因此可能重复送达，去重集同样是进程内的、随桥一起消失）。
- 投递确认：`deliver` 校验 botpy 回执——botpy 的 http 层对请求超时只记 WARNING 就静默返回 `None`，无回执一律视为投递失败抛出并走 pump 重试；成功投递记 INFO 回执日志 `qq-official delivered ...`（含平台消息 id），排查「发没发出去」以该行为准。`outbound.created` 投递确认后经 `POST /sessions/{id}/outbox/{outbox_id}/ack` 销账（见「持久 outbox」）。
- `qq-botpy` 在核心依赖（pyproject `dependencies`）里，`pip install -e .` 即装；唯一 `import botpy` 的模块是 `bridge.py` 且为惰性导入，环境缺失时服务报 `error` 状态，不会拖垮 web 运行时。

## 8. 常见排障入口

- 桥拿到 403 `external_api_disabled`：配置 `externalApi.enabled` 与 tokens；401：token 不匹配或条目被禁用。
- 消息提交成功但桥收不到回复：先确认 pump 是否真的在跑——锚点 `qq-official pump connecting session=<会话键> last_seq=N`，**每次连接尝试一条 INFO**。它在、而 `qq-official delivered` 不在 ⇒ 事件没到或投递被判失败（找桥侧 WARNING）；它一条都没有 ⇒ pump 从未被执行（任务没被调度/桥没建 pump），此时 18790 上会只剩浏览器 WS 那条回环长连接，`outbox.jsonl` 也是空的——这是"入站正常、出站黑洞"的最小心证；`connecting` 之后紧跟一条 WARNING `qq-official pump cancelled for session <会话键>` 则是第三种形态，指向桥自身在该时刻被重建（pump 随桥的 `finally` 一起取消，判据是同进程里 `[botpy] 登录机器人账号中...` 的条数在涨），先查 sync 是否丢了幂等而不是查 pump。再确认桥订阅的 SSE 会话与消息提交的会话一致（同一 `session_id`）；看事件缓冲是否被 `eventBufferSize` 淘汰（长断线超过缓冲窗口）。
- 主动推送不到达：沿「出站路由（主动推送）」链路查——发布侧（`source=heartbeat` 日志）→ drain（`external outbound published to hub`，含 outbox_id；伴随 `no live subscriber` WARNING 说明发布时无人消费，属对账兜底路径）→ 桥侧回执（`qq-official delivered ...`）。有 published 无 delivered 说明事件没有消费者：查 pump 重连日志（真故障：`qq-official event pump error ... reconnecting` 带异常栈；流安静过头：WARNING `qq-official event stream idle timeout ...`；服务端干净关闭：WARNING `event stream ended ... reconnecting`）与 `GET /outbox/pending` 滞留清单。滞留消息的补投不依赖重启：服务端 60s 周期对账（动作日志 `external outbox reconcile: republished ...`）与桥侧 30s 对账（动作日志 `qq-official spawned ... pump(s) from pending outbox entries`）在 24h 时效内自动收敛；超过 24h 的滞留标记 `expired` 废弃。会话转录/Web UI 里看得到回复而渠道端收不到时，优先怀疑本链路——转录落盘与渠道投递是两条独立链路。回合由网页发起（`/ws/ceo` 的渠道输入车道）时，先按「内置官方 QQ 适配器」主动提醒约束确认该目标的回复时间窗与额度，再查 pump 与账本。
- 渠道端收不到文件（只收到链接或什么都没有）：先确认事件是否携带 `attachments`——出站附件只由正文中**存在的本地文件**的 markdown 链接触发（见「事件流」出站附件契约），裸路径引用不提取；再看桥侧日志：`qq-official file_data media upload failed ... falling back to url upload` 是 base64 直传被平台拒、正在回退回源上传（回源要求服务端公网可达，回退失败即降级）；`qq-official attachment delivery failed ... degrading to text link` 表示两条上传路径都失败、或媒体取字节失败/超 20MiB，用户会收到签名链接文本行。OneBot 桥走 `upload_*_file` base64 直传，失败同样降级为链接行。入站侧：桥只转发带绝对 http(s) URL 的附件，文件类入站上限 20MiB。
- 终态缺失导致桥状态悬挂：属实现缺陷，对照「回合契约」终态不变量检查执行器改动。
- QQ 机器人面板报错/不连接：`/api/qq-bot/status` 与 settings 的 `accounts[]` 都**按号**给状态，先定位是哪一号再看它的 detail——AppID/AppSecret 错误表现为登录失败，intents 未开通表现为网关拒绝；出现 "This event loop is already running" 说明桥被改回了阻塞 `Client.run()` 入口。detail 带「将在 Ns 后重试」说明瞬时崩溃正按「崩溃自愈契约」退避重连，等待其自愈即可；detail 为 botpy 缺失/intents 不兼容属环境终态，需修环境后刷新配置触发重启。状态值区分 `enabled_off`（总开关）与 `account_disabled`（本号停用）：只看到前者就去找单个号的配置会走弯路。改过 AppSecret 的号若一直停在旧凭证的重试里，确认重启签名是否含密钥摘要。
- 加第二个号之后"原来那条 QQ 会话不再回话"：新格式下同一个 AppID 也会得到新的 bridge 段与新会话键，**存量会话不迁移**（旧键的转录/sidecar/记忆作用域原样留在会话列表里作为历史）。症状是旧会话在网页里仍可读写、QQ 端什么都没有——那条会话已经没有桥在消费它的事件了，发布侧会打 `no live subscriber`，滞留的主动推送 24h 后标 `expired`。要保住的是指向旧会话键的 cron/心跳目标：把它们改指新会话键。
