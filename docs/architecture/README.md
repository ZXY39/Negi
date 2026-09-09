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
- Execution/final-acceptance reflation (node vanishing from browser tree, acceptance visibility) → `runtime-overview.md` + `web-and-admin.md`
- Multimodal image not reaching model or fabricated image content → `runtime-overview.md` + `web-and-admin.md` (web upload/reopen) or `external-agent-api.md` (bridge inbound attachments)
- A config refresh disrupts an in-flight turn → `config-and-models.md`「配置热刷新」
- Same task result pushed to the channel multiple times → `heartbeat-system.md`「Task Terminal Repair Contract」
- Channel/bridge reply emits `## Runtime Tool Contract` or other internal contract text -> `runtime-overview.md` + `external-agent-api.md` (outbound sanitize contract)
- 第三方桥接应用接入（/api/v1 鉴权、外部会话、事件流、主动推送不到达）→ `external-agent-api.md`「常见排障入口」
- 会话转录/Web UI 有回复但渠道端（QQ 等）收不到、渠道「能收不能发」、重启后旧提醒补投或重复 → `external-agent-api.md`「出站路由（主动推送）」+「持久 outbox」+「内置官方 QQ 适配器」
- 官方 QQ 机器人面板报错、不连接或收不到消息 → `external-agent-api.md`「内置官方 QQ 适配器」+「常见排障入口」
- OpenAI 兼容端点 401/403/423/503、回复总是 "still working"、流式文本与终稿不一致 → `agent-gateway.md`「常见排障入口」
- MCP 工具全部 connection_failed、MCP 客户端协议解析错误（stdout 被污染）→ `agent-gateway.md`「常见排障入口」+「MCP stdio 网关契约」
- 渠道会话短暂出现在本地 web 会话列表、刷新后激活会话被切回本地会话、渠道回合无法在网页暂停 → `web-and-admin.md`「CEO Session List Interaction Contract」+「Active-Turn Button Semantics」
- 用户连续发送消息时助手只看到最后一条、或渠道消息收到重复回复 → `runtime-overview.md`「prompt_batch 批次回合内容合并」+ `external-agent-api.md`「回合契约」与「内置官方 QQ 适配器」
- Node error pause is not delivered to the source session, or node-error heartbeats retry forever -> `heartbeat-system.md`「Task Node Error Delivery」
- 节点失败但无系统报错、模型回复疑似被输出上限截断(无工具调用、顶格 output_tokens) → `web-and-admin.md`「Node Detail Error History」+ `config-and-models.md`「Model Request Parameter Defaults」
- Node pause or resume behaves unexpectedly -> `runtime-overview.md`「Node-Level Pause and Recovery」
- 重启后任务未自动恢复、优雅重启后仍停在 paused、或出现「本任务遇到异常停止」toast → `runtime-overview.md`「Graceful Shutdown Pause and Startup Auto-Resume」+ `operations-and-maintenance.md`「重启后任务未自动恢复 / 出现“异常停止”toast」
- 任务大厅持续显示「worker stale」、托管 worker 崩溃后一直不自动重启 → `operations-and-maintenance.md`「托管 worker 看门狗」
- 会话在重启后自动续跑（`shutdown_resume` 内部轮）行为异常 → `heartbeat-system.md`「Shutdown Resume Wake」
- 记忆复核批次不足窗口阈值轮数就入队，或阶段跨批次重复出现 → `runtime-overview.md`「Memory Runtime Notes」
- Broken image icons, file-route 400s, snapshot path mismatch → `web-and-admin.md` "Inline Markdown Image Rendering Contract"
- 模型重复处理已回答的问题、连续请求尾部反复出现同一条无回复的用户消息 → `context-and-cache-troubleshooting.md`「残留 paused 转录条目」
- 同一份 heartbeat 规则 / event bundle 在请求体里重复多份、token 逐轮线性上涨而对话无实质推进、或模型被已 success 节点的过期暂停通知误导 → `context-and-cache-troubleshooting.md`「heartbeat / cron 上下文残骸」
- 模型报告的日期/时间与事实不符（心算毫秒时间戳出错、引用陈旧时间、日报归属日期错误）→ `heartbeat-system.md`「Internal-turn time anchors」+ `runtime-overview.md`「用户消息时间锚点」
- 用户消息在请求体里同时出现原文与带 `[消息送达时间]` 行的两个版本，或装饰后缓存命中率骤降 → `context-and-cache-troubleshooting.md`「用户消息时间装饰破坏前缀稳定或相等性去重」
- 入站到首个 provider 请求发出耗时异常 → `context-and-cache-troubleshooting.md`「Prompt Cache Family 与 Actual Request」
- 会话/节点疑似卡在 provider 退避重试，但界面没有重试次数与错误信息 → `runtime-overview.md`「Chat provider 超时与重试边界」+ `web-and-admin.md`「Model Retry Visibility UI Contract」
- `temp/tasks/` 出现大量无主 `task_*` 目录（目录数远超任务数）→ `operations-and-maintenance.md`「关键状态文件与目录」+ `runtime-overview.md`「任务侧」
- 临时文件散落在工作区根目录（`.tmp_*` / `tmp_*`、命令重定向落盘）→ `runtime-overview.md`「任务侧」+ `tool-and-skill-system.md`「四个概念必须分清」

## Maintenance Rules

These rules prevent the docs from re-accumulating redundancy. Every edit to this directory must follow them:

1. Update in place, never append. Rewrite the section that owns the topic; never add trailing addendum sections ("X Update", "X Notes").
2. One contract, one home. State each contract only in its owning doc below; everywhere else use a pointer: `See <doc> "<topic>"` / `详见 <doc>「<topic>」`.
3. Pointers name topics, never section numbers.
4. Present tense only. No "now / no longer / previously / 现在 / 不再 / 曾经" — that is changelog language.
5. Superseded text is deleted outright, never left as "obsolete notes".
6. Size bands, not hard caps. Metric: bytes via `wc -c docs/architecture/*.md` (stable for mixed CJK/English prose; word counts are not). Reference sizes: `runtime-overview` 68 KB / `web-and-admin` 65 KB / `tool-and-skill-system` 54 KB / `context-and-cache-troubleshooting` 51 KB / `operations-and-maintenance` 24 KB / `heartbeat-system` 26 KB / `config-and-models` 17 KB / `external-agent-api` 16 KB / `agent-gateway` 10 KB. Check sizes when you edit a doc. Within reference +30%: take no size action — never trim wording or drop facts just to hit a number; per-contract clarity beats bytes. Over the band: run the structural ladder in order — (a) delete dead/duplicated/superseded content; (b) move misplaced content to its owning doc; (c) split a genuinely grown subsystem topic into a new doc and update this README; (d) if none applies the doc legitimately needs the size — raise its reference with a one-line justification in the commit. Contract facts are never deleted to satisfy a size.

## Topic Ownership

| Topic | Owning doc |
|---|---|
| Runtime layering, message execution chain, session/task relationship, task temp directory resolution, distribution / append-notice contract, provider timeout boundary | `runtime-overview.md` |
| Frontdoor context compression contract (`token_compression` / `stage_compaction`) | `runtime-overview.md` |
| Memory queue state/file semantics (`runtime-overview`); queue/reset operator workflows (`operations-and-maintenance`) | both, split as shown |
| Heartbeat continuation contract, cron at-most-once delivery, reminder sidecar decision semantics, timeout stop, task terminal repair, node-error and distribution-error delivery | `heartbeat-system.md` |
| Tool/skill four concepts, candidate→callable chain, Tool Admin RBAC semantics, duplicate-call guard | `tool-and-skill-system.md` |
| Actual-request forensics, append-only rule, cache-miss triage, token preflight diagnostics | `context-and-cache-troubleshooting.md` |
| Websocket/UI contracts, composer/media rendering, image upload gating, model config admin draft contract, container deployment | `web-and-admin.md` |
| Config schema, hot refresh, model bindings, secret location, deployment unlock | `config-and-models.md` |
| External Agent API contract: `externalApi` config and token overlay, ext session registry/keys, turn terminal invariant, SSE event mapping, ext outbound routing, built-in official QQ adapter (`qqBot` config, in-process botpy bridge) | `external-agent-api.md` |
| Agent gateway contract: OpenAI-compatible endpoint (`/api/v1/chat/completions`, session mapping, wait/200-honest-text policy, streaming diff) and MCP stdio gateway (`g3ku mcp serve`, tool surface, stdout purity) | `agent-gateway.md` |
| Startup/deploy/troubleshooting order, memory CLI, Docker compose | `operations-and-maintenance.md` |
