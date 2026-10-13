# Negi Agent Gateway 架构说明（OpenAI 兼容端点 + MCP 网关）

本文档是「外部 AI agent 开箱即用对接面」的唯一契约归属文档：OpenAI 兼容端点（`POST /api/v1/chat/completions`、`GET /api/v1/models`）与 MCP stdio 网关（`negi mcp serve`）。底层回合/事件/鉴权契约属于 `external-agent-api.md`「回合契约」「事件流」，本文只写网关层叠加的语义；操作向快速上手见 skill `skills/negi-bridge-onboarding/references/integration-manual.md`「开箱即用集成」。

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
- HTTP 错误只留给请求级失败：400/413/503 用 OpenAI `{"error":{message,type,param,code}}` 形状；401/403（鉴权依赖）与 423（锁中间件）保持平台既有 `{"detail":…}` 形状。框架层先于路由还有两种，也不是 OpenAI 形状：请求体解不出 UTF-8 → 400 `{"detail":"There was an error parsing the body"}`；UTF-8 合法但 JSON 语法错 → 422 `[{"type":"json_invalid","loc":["body",…]}]`。撞前者最常见的原因是调用方把中文 argv 按本地 ANSI 码页重编码后才发出（示例纪律见 `skills/negi-bridge-onboarding/references/integration-manual.md`「curl 样例」）。
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

- 进程模型：`negi mcp serve --token <t> [--base-url http://127.0.0.1:18790/api/v1] [--conversation-prefix mcp]`；token 可由 env `NEGI_EXTERNAL_TOKEN` 提供（旧名 `G3KU_EXTERNAL_TOKEN` 继续读作兜底，装好的人不必改 env）。MCP server 名与工具名都是 `negi` / `negi_*`（改名当日不留别名：客户端配置里的 `claude mcp add g3ku` 要重跑一次）。独立轻量进程，连**运行中的** web 运行时；`negi mcp check` 提供接入前连通性自检（回显 bridge_id 与会话数）。
- **stdout 纯净铁律**：stdio 上 stdout 是 JSON-RPC 协议通道，serve 路径一切提示走 stderr（`typer.echo(..., err=True)`；loguru 与 FastMCP 日志均 stderr-only）。任何 stdout 字节都会毁帧。
- 会话映射：`conversation` 不带 `:` 时 → `external_key = mcp:{conversation}`（本网关命名空间，不存在即建）；**带 `:` 时按全键寻址**（`web:ceo-…`、`qq:group:…`、`ext:…`），不加前缀、不建会话，能不能寻到别名下的会话由 token 的跨桥作用域裁决（`external-agent-api.md`「启用与鉴权」）。**前缀不是会话名的一部分**：把 `mcp:foo` 当名字传，谁的会话都不匹配，工具回 `{ok:false, error:"http_404"}`（实盘踩过一次，读起来像"会话丢了"）。这条寻址规则写在 FastMCP 的 `instructions` 里一处，供全部工具共用，**不再逐工具重复**（描述是每请求都进上下文的，重复打印只多花 token）。每次工具调用内"开 SSE 流 → 发消息 → 有界等待 → 关流"（流先开=零事件间隙；无驻留泵）。
- 工具面（`g3ku/mcp_gateway/server.py`，全部返回 dict、**永不抛异常**——HTTP/传输错误转 `{ok:false, error, status_code?}` 可读载荷）：

| 工具 | 语义 | 关键返回 |
|---|---|---|
| `negi_chat(conversation, message, wait_seconds=120, attachments=[])` | 发送；`wait_seconds>0` 等终稿，`=0` 只提交立即返回（不开 SSE 流）。**会话在跑时再发是安全的**：并入该会话队列、在在跑回合终态之后按提交顺序回答，不重启回合 ⇒ 互相独立的问题应连着提交而不是一条条干等 | `status: completed\|submitted\|pending\|queued_receipt\|failed\|cancelled\|no_reply` + `reply`/`receipt`/`error`/`turn_id`/`usage`/**`task_ids`**；pending 与 submitted 都带续取 hint。撞进正在跑的回合时回执带 `running_turn_id` + `queued_position`（**派重的根因是提交那一刻看不见在飞回合**，实测有 agent 因此把同一道题派了两遍），pending/cancelled/no_reply 三类空手结果带 `reason` 与 `where_to_look`。正数仍按 5..1800 clamp，`0` 是"不等"而不是"等 5 秒"；超 8000 字的 `reply` 自动落盘（见「长回复落盘」注记）。带附件时回包多一个 `attachments` 逐项回显（`名字 (N bytes)` 或 URL），且这一行在 `completed`/`queued_receipt` 等每种终态形状里都在；附件被拒时**整条不提交**，回 `error`（`attachment_not_found`/`attachment_is_a_directory`/`attachment_too_large`/`unsupported_attachment_scheme`/`too_many_attachments`）+ `attachment_index` + `hint`，形状见「附件上行」 |
| `negi_get_reply(conversation, wait_seconds=60, turn_id="")` | 续取未消费的下一个终稿；给了 `turn_id` 就只等那一回合（`conversation` 原样传当初给 `negi_chat` 的那个名字） | `found` + `reply` + `task_ids` + `turn_id`/`transcript_turn_id`/`answered_seq`。两套 id 各管各的：`turn_id` 是外部回合记录 id（暂停/续取定址用它），`transcript_turn_id` 是转录行的 id（`negi_get_turn` 用它）。不传 `turn_id` 时沿用「等待契约」的 queued 规则：收 finals 到终态、取 max-seq。**`found:false` 不是一件事**，带 `reason`：`waited_out_reply_pending`（东西还在账本里，用 `negi_wait_wake` 取）、`no_final_written`（这轮没交正文）、`turn_cancelled`、`stream_closed`、`still_running`、`nothing_pending`（分不开"没写"与"已被别的消费者取走"，就照分不开说，不冒充 `already_consumed`），并附 `unconsumed_replies`/`session_running`/`stop_reason` 与 `where_to_look` 指针 |
| `negi_get_turn(conversation, turn_id)` | 按**转录行的 turn id**（即回包里的 `transcript_turn_id`）重取那一行的原文。durable：调用方自己被压缩掉之后仍能取回（内存 `TurnRecord` 只留 256 条终态记录且会淘汰，不能当历史读口） | `reply`/`created_at`/`usage`/`task_ids`/`silent_reply`；这会话没存过该 id → `{ok:false, error:"turn_not_found"}`。**两套 id 不强行合一**：曾试过在 `_execute_turn` 把 record id 盖进消息 metadata 当作盘键，实盘证明盖不到（转录行仍是另一个 id），故撤销该路径改为把两个 id 都交出去 |
| `negi_wait_wake(conversation, wait_seconds=120)` | 一次调用回答"**我没在听的时候发生了什么**"：本会话派发的任务里有哪些已落终态 + 账本里还没被消费的终稿。读的是落库后的 durable 事实（`tasks` 表 + 持久 outbox），不是活 SSE ⇒ 任务在半小时前跑完、当时没有任何连接，也照样报得出。`wait_seconds=0` 只看一眼立即返回；正数每 2 秒重读到有东西或超时 | `woke`/`reason: task_terminal\|reply\|empty\|timeout` + `tasks[]`（`task_id`/`status`/`finished_at`）+ `replies[]`（`outbox_id`/`ts`/`reply`，长回复同样落盘）+ `replies_pending`/`tasks_may_be_truncated`/`since`/`next_since`。**取回的回复即销账**（与官方桥同一条 ack 路），销账失败那条带 `ack_failed` 并可能再出现一次；游标落盘在网关本机（`<NEGI_GATEWAY_DIR>/wake-cursor.json`），只在真唤醒时推进 ⇒ 只在没唤醒时超时不会把历史跳过去。同一秒的行分不开"报过/没报过"（终态行只存到秒，下界又是包含式），所以游标旁还存**已报身份**（`task_id`/`outbox_id`）做去重——否则边界那一秒会被永久重报，会话再也不出现 `woke:false` |
| `negi_session_status(conversation)` | 运行/排队快照，**活动用时间表达而不是计数器**（光给一个 `last_seq`，调用方无法区分"在跑一个长活"与"已经卡死"——实测有 agent 为此累计阻塞近一小时） | `running`、`queued_follow_ups`、`inflight_turn_id`、`started_at`/`elapsed_seconds`（在飞回合，取自 `TurnRecord.started_at`）、`last_activity_at`/`silence_seconds`（取自 hub 最后一条事件的 `ts`，**零新状态**）、`last_activity_kind`/`last_activity_text`（此刻在做什么；**先过出站消毒再截**，hub 的 `reply.delta` 本身不消毒）、`latest_model_text`（最新一段模型输出：消毒后截 300 字，`latest_model_text_total_chars` 给截断前的真实字数；注意消毒只剥思考段/契约尾那类，实测**不剥** `[G3KU_STAGE_*]` 这种内部标记）、`recent_tool_calls`（最多 5 项、每项 `{tool_name, calls}`，取自 `progress` 事件的结构化 `tool_name`，不从 `🔧 xxx started` 的人读文本反解；**次数覆盖整个会话事件缓冲**，只给去重名字回答不了"是在磨还是要收了"——实测一条 87 分钟审计把 114 轮花在同样几颗名字上）、`stages_recent`（最近三个阶段的 `stage_index`/`status`/`stage_kind`/`goal` 前 120 字/`tool_rounds_used`/`tool_round_budget`/`round_count`；**不给** `completed_stage_summary` 与 `key_refs`——实测原样五阶段平均 87.8 KB、最大 600 KB ≈ 15 万 token，窄化后平均 666 B ≈ 166 token）。会话未驻留时 `stages_recent` 与 `stage_totals` 整键省略，含义是"读不到账本"，不是"没有阶段"。`stage_totals` 给跨阶段整数（`stages_total`/`stages_completed`/`rounds_used_total`/`rounds_budget_total`）——**只给最新 3 条 goal 会被读成"一共 3 个阶段"**，114 轮那种累计量此前没有任何一口给出。`stop_reason` 是这轮**怎么结束的**（原样透 `RuntimeAgentSession._last_stop_reason`，如 `user_pause`；交付轮为空串），配 `unconsumed_replies`/`turn_left_unconsumed_reply`：`last_activity_kind:"turn.completed"` 而阶段仍 `active` 时，这几行是分辨"交付完了"还是"中途早退"的唯一依据——此前调用方只能按最保守的解读重派。`tasks_running`/`tasks`（本会话仍未完成的任务，逐条 `task_id`/`status`/`model_calls`/`last_hop_at`，再加 `nodes`：至多 6 格 `node_id`/`node_kind`/`status`/`depth`/`updated_at` 按更新时间倒序 + 真实 `node_count`——任务是执行 + 验收的树，光一句 `in_progress` 说不清卡在哪一格）与 `task_activity_at`/`task_silence_seconds` 给任务侧活性：派发回合几十秒就收尾、节点要跑几十分钟，这段时间会话侧本来就没有新事件，只看 `silence_seconds` 会把在长跑的活读成卡死；读不到任务服务时这几键整组省略。四档读数的合同与"三种时间形状都要当可达输入"的纪律归 `external-agent-api.md`「会话状态读数」。`last_event_seq`/`last_error` 照旧。工具面里的 `last_seq` 仍是**网关自己的 SSE 事件游标**，不是步数也不是进度 |
| `negi_pause(conversation)` | 暂停在跑回合（不动已派发的任务） | 无 inflight → `{ok:false, error:"no_inflight_turn"}` |
| `negi_cancel(conversation)` | 取消该会话的在飞工作：**在跑的回合 + 本会话派出去还没落终态的任务**（以前只停协程，而外部回合刻意以 `None` 键注册，回合一收尾就永远报 0，调用方据此以为安全、任务却还在同一棵树上改文件） | `cancelled_turns`/`cancelled_tasks`/`task_ids` + `skipped:[{target,reason,how_to_clear}]` + `tasks_still_running`（取消后重新数一遍）。`cancelled` 是对接手册里的已发布字段，收口成"回合 + 任务"两数之和。**取消是请求式的**：分离部署下节点在下一个检查点才落终态，紧随的几次读数里 `tasks_still_running` 可能还是正数，那不是失败；真被拒的目标连拒绝原因一起出现在 `skipped` 里 |
| `negi_list_conversations(scope="all", limit=50, offset=0, query="", bridge_id="")` | 会话列表：`all` 走 `/sessions?scope=all` 拿全量目录，`own` 只列本网关命名空间。**两个 scope 的活动字段同形**（`preview_text`/`message_count`/`updated_at`/`is_running`/`transcript_path`/`has_transcript`）——own 侧由 `build_channel_ceo_session_item` 逐条构造，与 `all` 同一颗读法，不再回"只有五字段"的空读数 | `scope` + `requested_scope`（token 没有跨桥作用域时降级为 `own`，**两个字段都把降级说出来**）、`matched`（过滤后总数）、`count`（本页条数）、`offset`/`next_offset`（`next_offset` 到底时为 `null`）、`truncated`。`has_transcript` 只在服务端报了之后才出现在条目里——上游没说时省略该键，不折成 `false` |
| `negi_delete_conversation(conversation)` | 删掉本网关自己建的会话：转录文件 + 侧产出 + 注册表条目一起清 | `deleted`/`registry_forgotten`/`transcript_removed`。**只收短名**：带 `:` 的全键一律 `ok:false, error:"own_namespace_only"`——一个桥不该能删另一个桥的会话。服务端 `DELETE /sessions/{id}` 的默认清除语义（留条目、清正文）不变，删除走 `?purge=true` |

- 工具注解：每颗工具都写满四个 MCP tool annotations（`readOnlyHint` / `destructiveHint` / `idempotentHint` / `openWorldHint`），**不留缺省** —— 规范里这四个 hint 的缺省是 `destructiveHint=true`、`openWorldHint=true`，整块省略等于把"发一条消息"画成销毁性动作，客户端只能对每颗工具弹确认。判据轴取客户端要做的三个决定：要不要弹确认（`negi_cancel`、`negi_delete_conversation` 是唯一两颗 `destructiveHint=true`）；能不能自动重发（`negi_chat` 与取回复的两颗标 `idempotentHint=false`，重发会多派一个回合或多销一份终稿；`negi_get_turn`、`negi_session_status`、`negi_list_conversations` 是纯读，重发无副作用）；会不会驱动外部执行（只有 `negi_chat` 标 `openWorldHint=true`，它派出的任务会改仓库、跑命令、发渠道）。合同由 `tests/resources/test_mcp_tool_annotations.py` 从 `tools/list` 响应那一层钉住，新增工具漏标即红。
- 附件上行（`negi_chat` 的 `attachments`）：调用方是模型，工具入参只能是 JSON 字符串，所以这条车道**禁止 base64 参数**（5 MiB 的图 ≈ 700 万字符，先把调用方上下文烧光）。参数只收两种形状——跑 `negi mcp serve` 那台机器上的**本机路径**，或 http(s) URL。读盘动作留在网关进程：路径按 `mimetypes` 猜出 mime 后 base64 上行，落到运行时自己的上传根；URL 原样作引用附件上行（网关不抓字节，图片时由 provider 侧取）。字节闸门沿用运行时既有档位，MCP 侧不新立数字（`server.py` 顶部常量与运行时常量等值，由 `tests/resources/test_mcp_chat_attachments.py` 钉住）：图片 5 MiB（`WEB_CEO_IMAGE_UPLOAD_MAX_BYTES`）、其它 20 MiB（`EXTERNAL_FILE_UPLOAD_MAX_BYTES`）、一条消息 4 颗（`MAX_CHANNEL_ATTACHMENTS`，与"从正文里捡本地文件"那条道同一根轴）。任何一项被拒就**整条不提交**并回 `attachment_index` 与 `hint`——半提交会让调用方以为附件已经到账。超限的文件在读进内存之前就被 `stat` 挡掉。运行时那一侧：图片被无条件前置成 provider 可见的多模态块，非图片只在正文后附一行名字与路径，`kind=audio` 刻意不进模型视野（转写已在正文里，路径行只会诱使它去开文件）。异机部署（`--base-url` 指向别的机器）时路径读不到，报 `attachment_not_found`，不静默降级。
- 等待期间的通知：`negi_chat`/`negi_get_reply` 在阻塞等待中把 hub 的 `progress` 事件转成 MCP `notifications/progress`（`progress=<事件 seq>`，`message=<kind: 文本前 120 字>`，无 `total`）。FastMCP 的 `Context` 参数不进工具的 JSON schema，客户端无需改配置；**客户端不支持通知时只静默不发**，等待语义与最终回复不因进度通道而改变（两条都有用例钉住：收到 progress 事件仍取回同一份 final；`report_progress` 抛错不影响回复）。
- 长回复落盘（`negi_chat`/`negi_get_reply`/`negi_wait_wake` 共用一颗 `g3ku/mcp_gateway/local_store.py::spill_reply`）：回复正文超过 `REPLY_SPILL_MIN_CHARS=8000` 字时，全文写进 `<NEGI_GATEWAY_DIR|G3KU_GATEWAY_DIR|~/.g3ku/mcp-gateway>/replies/<会话段>/<回合段>.md`，回包的 `reply` 只剩前 600 字 + `（共 N 字，全文：<路径>）`，并带 `reply_spilled`/`reply_file`/`reply_chars`。阈值按实测尾部定：ext 会话 655 条终稿 p99=5121 字、最大 6287 字，>8000 当前 **0 命中**——这条是保险道，不是常态通道。落点在**网关本机**（不是运行时），因为"上下文被几万字占满"只是 MCP 调用方这一侧的问题：同一份 `reply.final` 投给渠道桥时必须还是全文，渠道形状一字不动。写失败（磁盘满/只读目录）**原样带回全文**并多一个 `reply_spill_error`——落盘失败不许让人拿不到答案。路径里不含调用方可控输入：两段都过 `safe_path_segment`，`..`、`/`、`\` 一律塌成 `_`。同一目录按 mtime 只留最近 20 份。唤醒游标 `wake-cursor.json` 也落在这个目录：网关进程一重启 `_last_seq` 与会话映射就归零，而"上次问到哪儿"不能跟着丢。
- 投全键会话时，那一轮的归属记在**发起方** bridge 上：回复事件流、幂等位与 `negi_get_reply` 都跟着发起它的这个网关走，目标会话原本的桥名下不留痕。跨桥发信与在本命名空间发信在运行时里是同一条 `submit` 车道，排队/回执/终态不变量完全一致。

- 客户端接入示例：`claude mcp add negi -- negi mcp serve --token <t>`（或等价 MCP JSON 配置）。

## 5. 安全注记

- token 即命名空间：明文只出现在签发响应与对接方自己的配置/env 里；g3ku 侧走 bootstrap secret overlay（`config-and-models.md`），日志不落 token。
- 网关不放大**内容**面：出入只有对话文本与附件，出站文本沿用 relay 的清洗与媒体签名 URL 改写；系统提示词与工具面不经网关暴露。附件按「附件上行」那条道走：`data_base64` 由**网关进程**读盘产生，运行时只写自己的上传根（图片 5 MiB、其它 20 MiB，超限 413），所以这条车道上运行时不会按调用方指名的路径去读本机文件；`url` 形状不作任何本地读盘，只在正文里留一行引用。**任务侧只开一格窄读数**：`negi_session_status` 的 `tasks[].nodes` 给节点 id/种类/状态/深度/最近更新时间（上限 6 项 + 真实 `node_count`），`negi_wait_wake` 给终态任务的 id/状态/时间——节点正文、轮次明细、工具结果与验收材料都不经网关，任务树的其它部分仍只在 web 侧。**会话面**按 token 的作用域决定：`tokens[].crossSession` 默认放开，所以任一 token 都能列出全部会话的预览与转录路径，并向任意会话发消息。`transcript_path` 只是把共享 `SessionManager` 算出的文件位置说给调用方——stdio 网关与运行时同机，调用方本来就有本机文件读权，这个字段不新增能力，只省去它自己拼键（拼错键会撞上会话键长度边界）。要把某个号收回去，就在「外部接入」页把它的 token 设为「仅本桥」：作用域只按号裁决，没有能一次影响所有桥的全局档。
- 423 项目锁先于鉴权生效（`/api/*` 全域）；锁定时两个对接面都不可用，MCP 工具收到可读错误而非挂起。

## 6. 常见排障入口

- OpenAI SDK 报 401/403：token 不匹配 / `externalApi.enabled=false`；423：项目锁定；503 `runtime_unavailable`：web 运行时未就绪。
- 回复总是 "still working"：回合确实还在跑（长任务）——加大 `wait_seconds`、改用 `stream=true`，或稍后对同一 `user`/conversation 再发一条询问结果；确认不是事件缓冲溢出（`eventBufferSize`）。
- MCP 工具全部 `connection_failed`：web 运行时没在跑或 `--base-url` 错误——先 `negi mcp check`。
- MCP 客户端报协议错误/解析失败：几乎总是有东西写了 stdout——检查是否改动了 serve 路径的输出（铁律见「MCP stdio 网关契约」）。
- 流式文本与最终回复不一致：多段思考 + 段切换分隔符是预期行为；对一致性敏感改用非流式（「流式」局限）。
- 同一会话串话：多个调用方共用同一 `user`/`conversation` 键——按调用方分配独立键或独立 bridge token。
