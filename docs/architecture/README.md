# G3KU Architecture Docs

Start here when you are new to the repository or when a change crosses subsystem boundaries.

## Reading Order

1. `runtime-overview.md`
2. `operations-and-maintenance.md` when you need to run, debug, or deploy the system
3. `context-and-cache-troubleshooting.md` when the change touches prompt caching, context retention, append-only request growth, or request artifact forensics
4. `tool-and-skill-system.md`
5. `web-and-admin.md`
6. `heartbeat-system.md` when the change touches heartbeat, long-running CEO tool wakeups, or live reminder behavior
7. `config-and-models.md` when the change touches runtime config, provider/model routing, or model bindings
8. `external-agent-api.md` when the change touches the external bridge API (`/api/v1`), external sessions, outbound routing to bridges, or the built-in official QQ adapter
9. `agent-gateway.md` when the change touches the OpenAI-compatible endpoint (`/api/v1/chat/completions`) or the MCP stdio gateway (`g3ku mcp serve`)

## Topic Guide

- `runtime-overview.md`
  Use for session lifecycle, frontdoor/runtime flow, tool execution flow, cross-module runtime behavior, and graceful shutdown pause / startup auto-resume.
- `operations-and-maintenance.md`
  Use for startup workflows, troubleshooting order, high-risk change types, memory queue/reset workflows, and Docker deployment.
- `context-and-cache-troubleshooting.md`
  Use for prompt cache misses, context shrink/continuity regressions, actual-request artifact forensics, and before changing node or CEO context strategies.
- `tool-and-skill-system.md`
  Use for candidate tools, hydrated tools, skill loading, tool RBAC, and runtime tool contracts.
- `web-and-admin.md`
  Use for websocket contracts, frontend/backend responsibility boundaries, and operator-visible UI behavior.
- `heartbeat-system.md`
  Use for heartbeat turns, task-terminal/stall/distribution-error wakeups, shutdown-resume session wakes, and the boundary between heartbeat and the CEO inline tool reminder sidecar.
- `config-and-models.md`
  Use for config source-of-truth rules and role-to-model resolution.
- `external-agent-api.md`
  Use for the channel-agnostic headless API consumed by third-party bridges and the built-in official QQ adapter: auth, external session registry, turn terminal invariant, SSE event mapping, and ext outbound routing.
- `agent-gateway.md`
  Use for the out-of-the-box agent integration surfaces built on the External Agent API: the OpenAI-compatible chat endpoint (session mapping, wait/timeout and streaming semantics) and the MCP stdio gateway (`g3ku mcp serve`, tool surface, stdout purity).

## Debugging Entry Points

- Reminder UI or `ceo.tool.reminder` timeout-stop failures → `heartbeat-system.md` (+ `web-and-admin.md` for UI rendering)
- Node cache misses, restart-seed continuity, token preflight/compression questions → `context-and-cache-troubleshooting.md` (+ `runtime-overview.md`)
- Append-notice delivery, `waiting_children` replay, task-tree banner after distribution → `runtime-overview.md` + `operations-and-maintenance.md`
- 追加通知后任务停在 paused、任务树出现红色「消息分发失败」横幅、epoch state=failed → `runtime-overview.md`「frontdoor 与任务运行时的关系」+ `operations-and-maintenance.md`「task_append_notice / task message distribution 维护要点」
- 父节点在验收节点仍非终态时提前进入 `before_model`、或出现意外 `superseded by newer spawn round` → `operations-and-maintenance.md`「spawn 轮次过早完成或子节点被意外 supersede」+ `runtime-overview.md`「Node-Level Pause and Recovery」
- 任务已终态但仍有节点显示处理中（`in_progress`）→ `operations-and-maintenance.md`「残留节点自愈」+ `runtime-overview.md`「Node-Level Pause and Recovery」
- `task_progress` 显示的运行/检验状态与真实执行不符（陈旧帧被当作运行中、未派发的验收节点被当作检验中）→ `runtime-overview.md`「Node-Level Pause and Recovery」（活性标注与判读合同）+ `tools/task_progress_cn/resource.yaml`（工具描述判读规则）
- 任务疑似卡死、要定位任务在等哪个节点，或要查某节点最后一批工具调用的完整入参/状态/出参 → `runtime-overview.md`「Node-Level Pause and Recovery」（等待节点输出行）+「任务侧」（`task_node_detail` summary 档 `latest_tool_calls_full`）
- 恢复后节点上下文突然只剩几条消息、`task_model_calls` 里 `request_seed_source` 出现 `fallback_seed_*`、帧 `messages_ref` 为空 → `runtime-overview.md`「任务侧」（帧写入的 messages_ref 保留规则）+ `context-and-cache-troubleshooting.md`「append-only 规则」
- 磁盘满（Errno 28 / SQLITE_FULL）、0 字节错误日志、节点连锁 error-pause、artifact 变成 .gz、手动删除任务后产出在 deliverables/、任务大厅按大小排序定位大任务、managed-worker.log 轮转、runtime.sqlite3 收缩、task_events 表静默零写入 → `operations-and-maintenance.md`「磁盘满」+ `runtime-overview.md`「磁盘写保护与治理」+ `web-and-admin.md`（治理 UI 与大小/排序契约）
- Execution/final-acceptance reflation (node vanishing from browser tree, acceptance visibility) → `runtime-overview.md` + `web-and-admin.md`
- 任务大厅卡顿/滚动卡死、Edge 窗口「未响应」或崩溃 → `operations-and-maintenance.md`「任务大厅卡顿 / 冻结（浏览器端）」+ `web-and-admin.md`「Web Event Loop Contract」
- Multimodal image not reaching model or fabricated image content → `runtime-overview.md` + `web-and-admin.md` (web upload/reopen) or `external-agent-api.md` (bridge inbound attachments)
- A config refresh disrupts an in-flight turn → `config-and-models.md`「配置热刷新」
- Same task result pushed to the channel multiple times → `heartbeat-system.md`「Task Terminal Repair Contract」
- Channel/bridge reply emits `## Runtime Tool Contract` or `[G3KU_STAGE_*]` stage-block/internal context text -> `runtime-overview.md`「frontdoor 与任务运行时的关系」(回显守卫) + `external-agent-api.md` (outbound sanitize contract)
- 请求体里阶段块成批堆在上下文最前面、与自己的用户消息/最终回复脱节 → `runtime-overview.md`「stage_compaction」（块锚点三级取定与顺序不变量）
- 第三方桥接应用接入（/api/v1 鉴权、外部会话、事件流、主动推送不到达）→ `external-agent-api.md`「常见排障入口」
- 会话转录/Web UI 有回复但渠道端（QQ 等）收不到、渠道「能收不能发」、重启后旧提醒补投或重复 → `external-agent-api.md`「出站路由（主动推送）」+「持久 outbox」+「内置官方 QQ 适配器」
- 渠道端只收到文件签名链接、模型回复里引用的图片/文件没有作为媒体消息送达 → `external-agent-api.md`「事件流」出站附件契约 +「内置官方 QQ 适配器」
- 官方 QQ 机器人面板报错、不连接或收不到消息 → `external-agent-api.md`「内置官方 QQ 适配器」+「常见排障入口」
- OpenAI 兼容端点 401/403/423/503、回复总是 "still working"、流式文本与终稿不一致 → `agent-gateway.md`「常见排障入口」
- MCP 工具全部 connection_failed、MCP 客户端协议解析错误（stdout 被污染）→ `agent-gateway.md`「常见排障入口」+「MCP stdio 网关契约」
- 渠道会话短暂出现在本地 web 会话列表、刷新后激活会话被切回本地会话、渠道回合无法在网页暂停 → `web-and-admin.md`「CEO Session List Interaction Contract」+「Active-Turn Button Semantics」
- 用户连续发送消息时助手只看到最后一条、或渠道消息收到重复回复 → `runtime-overview.md`「prompt_batch 批次回合内容合并」+ `external-agent-api.md`「回合契约」与「内置官方 QQ 适配器」
- Node error pause is not delivered to the source session, or node-error heartbeats retry forever -> `heartbeat-system.md`「Task Node Error Delivery」
- 验收反复打回同一交付、任务长时间停在「执行→验收」循环而没有判失败（是否存在打回次数上限）→ `runtime-overview.md`「frontdoor 与任务运行时的关系」（验收拒收无次数上限：打回只发反馈并复活执行节点，不终态化任务）
- 验收节点上下文里堆着历次交付全文、或对已被取代的提交下结论、验收 bootstrap 每轮都在变 → `context-and-cache-troubleshooting.md`「验收 bootstrap 定稿与回合尾块」+ `runtime-overview.md`「frontdoor 与任务运行时的关系」（验收段：交接通知与回合尾块只给 ref + 有界摘要）
- False "task may be stalled" heartbeat while a long node tool (e.g. `exec` with a large `timeout_seconds`) legitimately runs, or a genuine hang after a tool timeout goes unreported -> `heartbeat-system.md`「Task Stall Detection」
- 节点失败但无系统报错、模型回复疑似被输出上限截断(无工具调用、顶格 output_tokens) → `web-and-admin.md`「Node Detail Error History」+ `config-and-models.md`「Model Request Parameter Defaults」
- 任务树过大打不开（`Failed to open task: Request timeout`）、打开时长时间无树或「加载中(x/xx)」进度异常、从任务树返回任务大厅后长时间卡顿 → `web-and-admin.md`「Task Tree Chunked Load Contract」
- `/api/tasks` 系列请求长时间挂起、任务大厅列表或 worker-status 数据迟迟不到、浏览器标签页唤醒/切回任务大厅后整个页面短暂失联 → `web-and-admin.md`「Web Event Loop Contract」
- 验收/节点把合法 PDF（或图片、xlsx 等二进制交付物）判成几十字节空壳、或在其上搜索 `%PDF` 等签名 0 命中 → `tool-and-skill-system.md`「外置工具结果信封」（二进制目标的展示契约、字节级搜索与 `filesystem_stat` 只读测量通道）
- Token统计窗口打开期间表格不随实时事件变化、搜索/筛选与搜索框内容保留、需点「刷新」才更新、模型调用明细按时间倒序/搜索跨全部记录的行为疑问、任务级统计有数字却显示「尚无按模型明细」，或总耗时/首 Token 耗时/思考 Token 显示 `--` → `web-and-admin.md`「Task Token Stats Window Contract」
- Node pause or resume behaves unexpectedly -> `runtime-overview.md`「Node-Level Pause and Recovery」
- 任务树节点已显示暂停但任务大厅仍显示处理中、或全局恢复后大厅卡在已暂停 -> `runtime-overview.md`「Node-Level Pause and Recovery」+ `web-and-admin.md`「Task Hall Action Contract」（状态胶囊判读）
- 重启后任务未自动恢复、优雅重启后仍停在 paused、或出现「本任务遇到异常停止」toast → `runtime-overview.md`「Graceful Shutdown Pause and Startup Auto-Resume」+ `operations-and-maintenance.md`「重启后任务未自动恢复 / 出现“异常停止”toast」
- 验收节点长期停在「待检验」、被检验的执行节点却在一轮轮重跑（半截验收回合无人接手）→ `runtime-overview.md`「frontdoor 与任务运行时的关系」（最终验收的两个派发选择器：通知账本 + 握手承诺重派发），worker 日志锚点 `final acceptance round re-dispatched after interruption`
- 任务显示「已取消」但无人取消过（payload `cancel_requested=false`，常伴随 `Managed task worker exited` 日志）→ `runtime-overview.md`「Node-Level Pause and Recovery」
- 任务大厅持续显示「worker stale」、托管 worker 崩溃后一直不自动重启、managed-worker.log 长时间不滚动但心跳与进程仍在 → `operations-and-maintenance.md`「托管 worker 看门狗」
- 会话在重启后自动续跑（`shutdown_resume` 内部轮）行为异常 → `heartbeat-system.md`「Shutdown Resume Wake」
- cron 定时任务到点不触发、`jobs.json` 停在 `running`/`timeout`/`interrupted`、调度器长时间静默或某次投递疑似挂死 → `heartbeat-system.md`「Cron Reminder Contract」+ `operations-and-maintenance.md`「任务没创建或没推进」
- 记忆复核批次不足窗口阈值轮数就入队，或阶段跨批次重复出现 → `runtime-overview.md`「Memory Runtime Notes」
- 限流/上游故障期间某轮记忆没写进去、`memory/failed.jsonl` 有停车记录、或医生检查报 `failed_parked` → `runtime-overview.md`「队列状态机与失败停车语义」+ `operations-and-maintenance.md`「Memory Queue Workflow」
- 日志审计侧栏角标不更新、原始日志为空或翻页停在空白页、时间显示与事件时间戳不一致、`/api/audit` 503 → `web-and-admin.md`「Log Audit Page And Event Contract」
- Broken image icons, file-route 400s, snapshot path mismatch → `web-and-admin.md` "Inline Markdown Image Rendering Contract"
- 模型重复处理已回答的问题、连续请求尾部反复出现同一条无回复的用户消息 → `context-and-cache-troubleshooting.md`「残留 paused 转录条目」
- 同一份 heartbeat 规则 / event bundle 在请求体里重复多份、token 逐轮线性上涨而对话无实质推进、或模型被已 success 节点的过期暂停通知误导 → `context-and-cache-troubleshooting.md`「heartbeat / cron 上下文残骸」
- 模型报告的日期/时间与事实不符（心算毫秒时间戳出错、引用陈旧时间、日报归属日期错误）→ `heartbeat-system.md`「Internal-turn time anchors」+ `runtime-overview.md`「用户消息时间锚点」
- 用户消息在请求体里同时出现原文与带 `[消息送达时间]` 行的两个版本，或装饰后缓存命中率骤降 → `context-and-cache-troubleshooting.md`「用户消息时间装饰破坏前缀稳定或相等性去重」
- 入站到首个 provider 请求发出耗时异常 → `context-and-cache-troubleshooting.md`「Prompt Cache Family 与 Actual Request」
- 会话/节点疑似卡在 provider 退避重试，但界面没有重试次数与错误信息 → `runtime-overview.md`「Chat provider 超时与重试边界」+ `web-and-admin.md`「Model Retry Visibility UI Contract」
- `temp/tasks/` 出现大量无主 `task_*` 目录（目录数远超任务数）→ `operations-and-maintenance.md`「关键状态文件与目录」+ `runtime-overview.md`「任务侧」
- 临时文件散落在工作区根目录（`.tmp_*` / `tmp_*`、命令重定向落盘）→ `runtime-overview.md`「任务侧」+ `tool-and-skill-system.md`「四个概念必须分清」
- 会话固定了指定模型却仍走模型链、固定模型被删除/禁用后未回退、或切换后用量表按旧模型窗口显示 → `config-and-models.md`「会话级固定模型优先于角色链」+ `web-and-admin.md`「Composer Model Mode Panel」
- 上下文脑图标只按新输入变化、读数长期低于上一请求的真实输入规模 → `context-and-cache-troubleshooting.md`「同 turn 的 append-only 规则被破坏」+ `web-and-admin.md`「Composer Context Usage Meter」
- 长按脑图标不发起压缩、区分线停在「压缩已暂停」、压缩中区分线凭空消失刷新后才出现、渠道会话脑图标没有读数 → `web-and-admin.md`「Manual Context Compression」+ `context-and-cache-troubleshooting.md`「Shrink 原因与压缩边界」
- 切回仍在处理的会话看不到期间产生的阶段/工具调用（刷新网页才出现）→ `web-and-admin.md`「CEO Feed View State And Scroll Preservation Contract」
- 翻看工具输出时新输出把输出框滚动条顶回顶部 → `web-and-admin.md`「CEO Feed View State And Scroll Preservation Contract」

## Maintenance Rules

These rules prevent the docs from re-accumulating redundancy. Every edit to this directory must follow them:

1. Update in place, never append. Rewrite the section that owns the topic; never add trailing addendum sections ("X Update", "X Notes").
2. One contract, one home. State each contract only in its owning doc below; everywhere else use a pointer: `See <doc> "<topic>"` / `详见 <doc>「<topic>」`.
3. Pointers name topics, never section numbers.
4. Present tense only. No "now / no longer / previously / 现在 / 不再 / 曾经" — that is changelog language.
5. Superseded text is deleted outright, never left as "obsolete notes".
6. Size bands, not hard caps. Metric: bytes via `wc -c docs/architecture/*.md` (stable for mixed CJK/English prose; word counts are not). Reference sizes: `runtime-overview` 68 KB / `web-and-admin` 114 KB / `tool-and-skill-system` 54 KB / `context-and-cache-troubleshooting` 55 KB / `operations-and-maintenance` 24 KB / `heartbeat-system` 26 KB / `config-and-models` 26 KB / `external-agent-api` 16 KB / `agent-gateway` 10 KB. Check sizes when you edit a doc. Within reference +30%: take no size action — never trim wording or drop facts just to hit a number; per-contract clarity beats bytes. Over the band: run the structural ladder in order — (a) delete dead/duplicated/superseded content; (b) move misplaced content to its owning doc; (c) split a genuinely grown subsystem topic into a new doc and update this README; (d) if none applies the doc legitimately needs the size — raise its reference with a one-line justification in the commit. Contract facts are never deleted to satisfy a size.

## Topic Ownership

| Topic | Owning doc |
|---|---|
| Runtime layering, message execution chain, session/task relationship, task temp directory resolution, distribution / append-notice contract, provider timeout boundary | `runtime-overview.md` |
| Frontdoor context compression contract (`token_compression` / `stage_compaction`) | `runtime-overview.md` |
| Memory queue state/file semantics (`runtime-overview`); queue/reset operator workflows (`operations-and-maintenance`) | both, split as shown |
| Heartbeat continuation contract, cron at-most-once delivery, reminder sidecar decision semantics, timeout stop, task terminal repair, node-error, distribution-error, and task-stall detection/delivery | `heartbeat-system.md` |
| Tool/skill four concepts, candidate→callable chain, Tool Admin RBAC semantics, duplicate-call guard, universal tool timeout contract | `tool-and-skill-system.md` |
| Actual-request forensics, append-only rule, cache-miss triage, token preflight diagnostics | `context-and-cache-troubleshooting.md` |
| Websocket/UI contracts, composer/media rendering, image upload gating, model config admin draft contract, log audit event sink and audit page contract, container deployment | `web-and-admin.md` |
| Config schema, hot refresh, model bindings, secret location, deployment unlock | `config-and-models.md` |
| External Agent API contract: `externalApi` config and token overlay, ext session registry/keys, turn terminal invariant, SSE event mapping, ext outbound routing, built-in official QQ adapter (`qqBot` config, in-process botpy bridge) | `external-agent-api.md` |
| Agent gateway contract: OpenAI-compatible endpoint (`/api/v1/chat/completions`, session mapping, wait/200-honest-text policy, streaming diff) and MCP stdio gateway (`g3ku mcp serve`, tool surface, stdout purity) | `agent-gateway.md` |
| Startup/deploy/troubleshooting order, memory CLI, Docker compose | `operations-and-maintenance.md` |
