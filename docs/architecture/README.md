# Negi Architecture Docs

Start here when you are new to the repository or when a change crosses subsystem boundaries.

## Reading Order

1. `runtime-overview.md`
2. `main-task-runtime.md` when the change touches the node mailbox / wake paths, acceptance verdicts and handoff, node-level pause and the watchdog, or shutdown pause / startup auto-resume
3. `operations-and-maintenance.md` when you need to run, debug, or deploy the system
4. `context-and-cache-troubleshooting.md` when the change touches prompt caching, context retention, append-only request growth, or request artifact forensics
5. `tool-and-skill-system.md`
6. `tool-hydration-and-callable-chain.md` when the change touches contract presence and delivery, the staged-argument reuse lane, parameter-error guidance, the universal tool timeout, or the strict plain-text rejection
7. `web-and-admin.md`
8. `heartbeat-system.md` when the change touches heartbeat turns, task terminal / stall / distribution-error wakeups, or shutdown-resume session wakes
9. `config-and-models.md` when the change touches runtime config, provider/model routing, or model bindings
10. `external-agent-api.md` when the change touches the external bridge API (`/api/v1`), external sessions, outbound routing to bridges, or the built-in official QQ adapter
11. `agent-gateway.md` when the change touches the OpenAI-compatible endpoint (`/api/v1/chat/completions`) or the MCP stdio gateway (`negi mcp serve`)
12. `speech-to-text.md` when the change touches local voice transcription: the composer mic button, inbound channel voice, the `stt` config section, or the vendored whisper.cpp binary
13. `sandbox-execution-plane.md` when the change touches session trust, third-party channel gating, the read-only tier, tool allowlists by tier, or the read-root boundary

## Topic Guide

- `runtime-overview.md`
  Use for session lifecycle, frontdoor/runtime flow, tool execution flow, cross-module runtime behavior, frontdoor context compression, the persona head block and persona note, and node model route resolution.
- `main-task-runtime.md`
  Use for the task side of the runtime: node mailbox and wake semantics, acceptance three-tier verdicts, node-level pause/resume/cancel and the structural watchdog, and graceful shutdown pause with startup auto-resume.
- `operations-and-maintenance.md`
  Use for startup workflows (including the `negi` command stub, icons and the uninstall entry that install writes), troubleshooting order, high-risk change types, memory queue/reset workflows, the persona photo folder and onboarding state, and Docker deployment.
- `context-and-cache-troubleshooting.md`
  Use for prompt cache misses, context shrink/continuity regressions, actual-request artifact forensics, and before changing node or CEO context strategies.
- `tool-and-skill-system.md`
  Use for the four tool/skill concepts, fixed builtin tool contracts, candidate tools, skill loading, tool RBAC, and resource-directory generation checks.
- `tool-hydration-and-callable-chain.md`
  Use for what makes a tool callable this hop (contract presence), the delivery hop that returns the contract and stages arguments, the `reuse_args` batch lane, re-read and fingerprints, parameter-error lane, the strict plain-text rejection, externalized result envelope, universal tool timeout, tool rerun-safe declaration for the recovery lane, stage gating, and the context→callable chain.
- `web-and-admin.md`
  Use for websocket contracts, frontend/backend responsibility boundaries, the first-run onboarding flow, the read-only persona dialog, and operator-visible UI behavior.
- `heartbeat-system.md`
  Use for heartbeat turns, task-terminal/stall/distribution-error wakeups, cron at-most-once delivery, and shutdown-resume session wakes.
- `config-and-models.md`
  Use for config source-of-truth rules and role-to-model resolution.
- `external-agent-api.md`
  Use for the channel-agnostic headless API consumed by third-party bridges and the built-in official QQ adapter: auth, external session registry, turn terminal invariant, SSE event mapping, and ext outbound routing.
- `agent-gateway.md`
  Use for the out-of-the-box agent integration surfaces built on the External Agent API: the OpenAI-compatible chat endpoint (session mapping, wait/timeout and streaming semantics) and the MCP stdio gateway (`negi mcp serve`, tool surface, stdout purity).
- `speech-to-text.md`
  Use for local voice-to-text: the whisper.cpp subprocess engine and why it is not kept resident, audio normalization and silence gating, binary/model provisioning, and what the model tier choice trades in latency and accuracy.
- `sandbox-execution-plane.md`
  Use for the trust tier of a session (owner vs third party), the install-state-driven tier model (`restricted` / `unrestricted` resolving to `readonly` / `jailed` / `host_full`), the read-only tool allowlist and default-deny rule, the read-root boundary and its `ContextVar` pinning, control-plane rejection on the synthetic lane, the memory gate's four intake points, and the third-party sender stamp in the model-visible projection.

## Debugging Entry Points

- Node cache misses, restart-seed continuity, token preflight/compression questions → `context-and-cache-troubleshooting.md` (+ `runtime-overview.md`)
- 节点信箱投递（`send_message` / 验收驳回 / `child_abnormal_pause`）、`waiting_children` 重放、停在等待档的节点被唤醒 → `main-task-runtime.md`「节点信箱与唤醒（现行）」+ `operations-and-maintenance.md`「节点投递与冻结/复活排查」
- 网页给暂停节点发了消息、节点照旧不动且没有报错 → 设计内：该档落箱不唤醒，恢复按钮在操作员手上，合同见 `main-task-runtime.md`「节点信箱与唤醒（现行）」+ `web-and-admin.md`「Task Message Distribution UI Contract（已删除）」的节点消息输入框一条
- （历史，已删除）追加通知后任务停在 paused / 红色「消息分发失败」横幅 / epoch state=failed：distribution epoch 已整体删除，按 `main-task-runtime.md`「附录 A：已删除的 distribution epoch（历史说明，勿据此实现）」理解旧库数据，不要按它排障。
- 想知道"每条节点消息走到哪一步"（待处理 / 已消费 / 旧库里的 received·frozen 投影）→ `web-and-admin.md`「Task Message Distribution UI Contract（已删除）」的信箱三态三条 + `main-task-runtime.md`「节点信箱与唤醒（现行）」；节点详情已显示重试中而 toast 不出现 → `web-and-admin.md`「Model Retry Visibility UI Contract」
- 任务非终态、一批验收节点长期 `in_progress` 而派发计数为 0 → 先按 `operations-and-maintenance.md`「节点投递与冻结/复活排查」的签名分诊（`final acceptance round re-dispatched after interruption` = 验收轮按持久状态重派；`orphan node parked as candidate` = 无人驱动又无重放路径，已可见地暂停），合同见 `main-task-runtime.md`「验收三档裁决（P4）」与「Node-Level Pause and Recovery」；派发计数始终为 0 时再核 `main-task-runtime.md`「验收节点：提前创建、激活与握手重派发」的两个催命点（提交后重排一趟、扫描先于通知复派）
- 节点突然显示 `暂停·异常`（pause_reason=error）而模型没有报错 → 先看 `metadata.watchdog_history`：三条结构判据（重复相同调用 / 工具连续错误 / 超时无事件）见 `main-task-runtime.md`「看门狗」；验收方裁 `reject_no_progress`、回合兜底异常道（provider 整条链耗尽）也落同一暂停形态，理由分别在被验节点 `failure_reason`、该节点错误行与父节点 `child_abnormal_pause` 里。
- 验收节点选了 `reject_no_progress` 却被工具拒绝 → 首轮无 `metadata.rejection_history` 时该档按设计禁用（`main-task-runtime.md`「验收三档裁决」）。
- 同一工具反复返回同一条 `Error executing <tool>`、节点长期 `in_progress` 且从不进错误暂停 → `main-task-runtime.md`「Node-Level Pause and Recovery」（`runtime_fault:` 断路器）
- 操作员点了暂停、节点 `pause_requested=True` 而 `is_paused` 长期不变 → `main-task-runtime.md`「Node-Level Pause and Recovery」（脱离派发面后的暂停直落）
- 渠道会话（`ext:`）任务长期静默却收不到失速提醒 → `heartbeat-system.md`「Task Stall Detection」（受众谓词与它要过的两道闸门）
- 群聊里让它记住某事、跑命令、发文件、读宿主配置 → `sandbox-execution-plane.md` 的「Debugging Entry Points」：先分是信任级判错（注册表 `trust`）、档位名单漏登记，还是读根没下发
- 父节点在验收节点仍非终态时提前进入 `before_model`、或出现意外 `superseded by newer spawn round`（含节点被 resume 后白跑一段再落该原因）、或 `resume` 返回 `entry_settled`、或工具行是 `Error executing spawn_child_nodes` 而它绑定的子节点还在跑、或同一个执行节点在任务树上挂出两颗检验节点（其中一颗的 FAILED 只是清扫疤、从没下过判词）、或一批子节点在存储抖动时刻整批被记成 `Error: CancelledError`（其中有的其实早已成功）、或节点 `failure_reason` 写着 `orphan reaped at task resume` → `operations-and-maintenance.md`「spawn 轮次过早完成或子节点被意外 supersede」+ `main-task-runtime.md`「Node-Level Pause and Recovery」「验收节点：提前创建、激活与握手重派发」+ `tool-hydration-and-callable-chain.md`「统一工具 Timeout 合同」「验收节点：提前创建、激活与握手重派发」
- 派生评审把整批候选全拦下来、把带 `requires_acceptance` 的候选判成"验收内嵌/自产自销"、或想知道评审请求本身有多大/重发过几次 → `main-task-runtime.md`「任务树深度上限」（送审候选的解析结构、派生审查的 fail-closed 默认结果与 `review_attempts` / `review_request_chars`）
- 任务已终态但仍有节点显示处理中（`in_progress`）→ `operations-and-maintenance.md`「残留节点自愈」+ `main-task-runtime.md`「Node-Level Pause and Recovery」
- `task_progress` 显示的运行/检验状态与真实执行不符（陈旧帧被当作运行中、未派发的验收节点被当作检验中）→ `main-task-runtime.md`「Node-Level Pause and Recovery」（活性标注与判读合同）+ `tools/task_progress_cn/resource.yaml`（工具描述判读规则）
- 任务树节点徽标与真实执行不符（未派发的检验节点显示「检验中」、被检验的执行节点跟着显示「检验中」、非终态节点出现 `IN_PROGRESS` 原文、检验节点不见了、打回后整个返工期间挂着 `FAILED`、验收节点已终局失败却一直读成非终态）→ `web-and-admin.md`「Task Tree Live Sync And Self-Healing Contract」+「Task Message Distribution UI Contract」
- 任务树刷新时轮次下拉框自己收起/跳回默认轮次 → `web-and-admin.md`「Task Tree Live Sync And Self-Healing Contract」（重绘后的稳定身份与展开态恢复）
- 节点详情刷新时展开的工具组或某轮的工具面板自己折回去、切走再切回对不上 → `web-and-admin.md`「Node Detail Redraw And Expansion State Contract」（捕获时机与按轮账本）
- 会话回合那行、消息元信息行与节点详情头部的 token 读数对不上，或想知道缓存命中后面的百分比与「轮数」各取哪个数 → `web-and-admin.md`「Per-Turn Token Usage Contract」
- 本地会话列表拖动后的顺序被打回原样、渠道分组折叠不记住或折叠后看不到新消息数、或换浏览器顺序不一致 → `web-and-admin.md`「CEO Session List Interaction Contract」（手动顺序与渠道折叠态都只存前端 localStorage）
- 改了密码旧密码还能用、勾了自动解锁重启仍要输密码、或「锁定项目」后界面仍可读数 → `config-and-models.md`「Deployment Unlock Contract」（改密只换信封不换钥匙、解锁顺序 env→本地密钥文件→口令、锁定只清 web 进程内存主密钥）
- 任务与会话数据不在代码检出目录里、首次初始化时选过别的盘、重启后旧任务像空库一样找不到、或按文档路径找不到 `console.log`/`managed-worker.log` → `operations-and-maintenance.md`「关键状态文件与目录」（安装根与数据根各装什么、`G3KU_DATA_DIR`→指针→cwd 的解析顺序、盘上两个 `.g3ku` 的嵌套、`get_data_dir()` 其实是安装根、指针丢失即回退 cwd 的形态）
- 导出配置包报 `project_locked`、导入被要求输入「当前项目解锁口令」（`bundle_current_password_required` / `bundle_current_password_invalid`）、导入的包被判成越界路径、勾的范围在包里不存在（`bundle_scope_not_in_package` / `bundle_scope_empty` / `bundle_memory_store_missing` / `bundle_persona_empty`）、新设备导入只报一个没有 detail 的 `HTTP 500`（该机还没走完首屏、`config.json` 不存在）、或导入完成后 worker 仍走旧模型链 → `config-and-models.md`「Config Bundle Export And Import」（配置/记忆/人设三面各自勾选、包带的是主密钥本身、master.key 与 auto-unlock.key 不进包、导入用本机口令重裹信封所以项目口令不变、整体替换且需重启）
- 保存模型密钥报 `secret overlay is undecryptable with the current master key; refusing to overwrite`、或所有 apikey 同时变空 → `config-and-models.md`「secret 的真实去向」（覆盖层被截成 0 字节时守卫会拒绝一切写入；先取证改名该文件再 锁定→解锁 解除，写入耐久性与残留缺口同段）
- 界面停在初始化向导、看着像新项目但 `memory/` 和 `sessions/` 都有东西（`/api/bootstrap/status` 报 `mode=setup` + `key_store.state=lost`）、要查凭据是哪一刻没的、或要从代际副本回滚密钥材料 → `operations-and-maintenance.md`「界面停在初始化向导（看着像新项目）但 memory/ 与 sessions/ 都有东西」+ `config-and-models.md`「secret 的真实去向」（滚动备份 `llm-config-backups/`、留证 `security-events.jsonl`、`negi security status|backup|restore`）
- 已解锁、`key_store.state=ok`，但记忆/任务/会话三个列表一起空（`runtime.ready` 恒 false）、模型行带「凭据记录缺失」、`/api/models` 报 `Config not found` → `operations-and-maintenance.md`「已解锁但 runtime.ready 一直 false」+ `config-and-models.md`「绑定记录丢失时的降级」
- 导入配置包后保存成功却「同步新配置失败：secret_overlay_decrypt_failed」、所有 apikey 读成空 → `operations-and-maintenance.md`「同步新配置失败 secret_overlay_decrypt_failed」+ `config-and-models.md`「secret 的真实去向」（`overlay_readable` 读数、auto-unlock bearer 与 worker 重启）
- 任务疑似卡死、要定位任务在等哪个节点，或要查某节点最后一批工具调用的完整入参/状态/出参 → `main-task-runtime.md`「Node-Level Pause and Recovery」（等待节点输出行）+「任务侧」（`task_node_detail` summary 档 `latest_tool_calls_full`）
- 恢复后节点上下文突然只剩几条消息、`task_model_calls` 里 `request_seed_source` 出现 `fallback_seed_*`、帧 `messages_ref` 为空 → `runtime-overview.md`「任务侧」（帧写入的 messages_ref 保留规则）+ `context-and-cache-troubleshooting.md`「append-only 规则」
- 磁盘满（Errno 28 / SQLITE_FULL）、0 字节错误日志、节点连锁 error-pause、artifact 变成 .gz、手动删除任务后产出在 deliverables/、任务大厅按大小排序定位大任务、managed-worker.log 轮转、console.log 长跑不收敛/被日志风暴撑大、runtime.sqlite3 收缩、task_events 表静默零写入 → `operations-and-maintenance.md`「磁盘满」+ `runtime-overview.md`「磁盘写保护与治理」+ `web-and-admin.md`（治理 UI 与大小/排序契约）
- Execution/final-acceptance reflation (node vanishing from browser tree, acceptance visibility) → `runtime-overview.md` + `web-and-admin.md`
- worker 恢复后内存涨到 GB 级、RSS 风暴后不回落、要判"泄漏还是瞬态峰"、要查某一刻同时驻留的是谁 → `operations-and-maintenance.md`「内存峰值 / 恢复风暴后 RSS 不回落」
- 任务大厅卡顿/滚动卡死、Edge 窗口「未响应」或崩溃 → `operations-and-maintenance.md`「任务大厅卡顿 / 冻结（浏览器端）」+ `web-and-admin.md`「Web Event Loop Contract」
- 任务卡片 token 数字在涨却看不到任何反馈 → `web-and-admin.md`「Web Event Loop Contract」（卡片增量补丁的增长提示与 reduced-motion 例外）
- 打开/切换 CEO 或渠道会话等待数十秒到几分钟、首帧快照体积异常 → `web-and-admin.md`「Web Event Loop Contract」+「CEO Turn Timeline Rendering Contract」（快照只发 delta 与滚动投影合同）
- 回合进行中看不到新工具调用/新工具组，最终答复落地才一口气出现 → `web-and-admin.md`「CEO Websocket Lane Failure Contract」（单写者与丢帧计数）+「Streamed Reply Delta Contract」（轨道补丁的合并窗口）
- Multimodal image not reaching model or fabricated image content → `runtime-overview.md` + `web-and-admin.md` (web upload/reopen) or `external-agent-api.md` (bridge inbound attachments)
- A config refresh disrupts an in-flight turn → `config-and-models.md`「配置热刷新」
- 改了负载均衡组的每成员重试次数或往组里加成员，正在重试的那一发照旧跑旧预算/判组耗尽 → `config-and-models.md`「模型链变更何时作用于在途回合」
- Same task result pushed to the channel multiple times → `heartbeat-system.md`「Task Terminal Repair Contract」
- Channel/bridge reply emits `## Runtime Tool Contract` or other internal context text -> `runtime-overview.md`「frontdoor 与任务运行时的关系」(回显守卫) + `external-agent-api.md` (outbound sanitize contract)
- 工具上一轮还直接执行、这一轮调用它只交回契约不产出结果，名字却仍留在 `tools[]` 上（既不在 `denied_tools`、也不在 `undeclared_candidates`）→ 它的 toolskill 契约块被 `token_compression` 移出了上下文，按"契约不在 ⇒ 那一跳先交回契约"处理，下一跳起照旧可调；判据与唯一载体见 `tool-hydration-and-callable-chain.md`「契约在场与 callable」，读数是 `tool_contract_presence`
- 一批里 `reuse_args=true` 只跑出比暂存份数少的结果、或整批报"没有可复用的入参" → 先确认这是**上一跳**落的条目（严格一跳），再比该工具当轮在场指纹与条目里的指纹是否同修订；份数与执行数都记在工具行的 `execution_path`/`items` 上，机制见 `tool-hydration-and-callable-chain.md`「入参暂存与 `reuse_args` 批量执行」
- 某条 `send_message` 大面积拒收、或某个数组参数在某颗上游上总是"对不到节点" → 先分档位（`target_not_in_task` 改地址 / `target_list_unparsed` 改形状），再看工具行的 `arguments_coerced` 数这跳有没有被读侧归一剥过；机制见 `tool-hydration-and-callable-chain.md`「参数错误与状态分类」与 `main-task-runtime.md`「信箱投递、重激活与上行传播」
- 第三方桥接应用接入（/api/v1 鉴权、外部会话、事件流、主动推送不到达）→ `external-agent-api.md`「常见排障入口」
- 外部 agent 看不见全部会话、或要把某个 token 的可见范围收回去 → `external-agent-api.md`「启用与鉴权」的跨桥作用域一档
- 会话转录/Web UI 有回复但渠道端（QQ 等）收不到、群里 @ 了没回复而单聊正常、回合跑坏了渠道端静默、渠道「能收不能发」、重启后旧提醒补投或重复 → `external-agent-api.md`「回合契约」终态不变量 +「出站路由（主动推送）」+「持久 outbox」+「内置官方 QQ 适配器」+「回复形状与主动消息权限」
- 渠道端只收到文件签名链接、模型回复里引用的图片/文件没有作为媒体消息送达 → `external-agent-api.md`「事件流」出站附件契约 +「内置官方 QQ 适配器」
- 桥接端 `reply.final` 的 `usage` 缺失或数值不像本轮 → `external-agent-api.md`「事件流」（usage 随 `message_end` 由发出侧带出，relay 只透传）+ `web-and-admin.md`「Per-Turn Token Usage Contract」
- 会话转录里明明有回复正文、拉取式读口却报 `no_final_written` / `unconsumed_replies:0` → `agent-gateway.md`「等待契约（wait_for_external_reply）」+ `external-agent-api.md`「持久 outbox」（心跳来源的回合只产 `outbound.created`；销账方与可取集合判据都在那里）
- 渠道会话在网页目录里删了还在、删过的卡片对方再发消息也不回来、清过历史的行仍显示可发消息、或注销身份被拒（`outbox_pending` / `scheduled_target`）→ `web-and-admin.md`「Channel Session Clear Contract」+ `external-agent-api.md`「会话注册表与 key 命名空间」
- 官方 QQ 机器人面板报错、不连接或收不到消息 → `external-agent-api.md`「内置官方 QQ 适配器」+「常见排障入口」
- 对接方问"我派的任务结清了没 / 我断开这段时间错过了哪份回复"没有一口可读，或状态读数在最需要的时候整包失败 → `external-agent-api.md`「唤醒读数」+「会话状态读数」
- 官方 QQ 桥每隔约一小时重登一次（`console.log` 里 `gateway session ended`）是不是故障 → `external-agent-api.md`「内置官方 QQ 适配器」的到期重登界线
- 加第二个 QQ 号之后原来那条渠道会话不再回话（网页有回复、QQ 端什么都没有）→ `external-agent-api.md`「常见排障入口」（存量会话不迁移；指向旧会话键的 cron/心跳目标要改指新键）
- 端口仍 LISTENING 但网页与所有 `/api/*` 一起超时、进程单核跑满，且「重启并更新」点了没反应 → `external-agent-api.md`「运行入口是硬约束」+「常见排障入口」
- OpenAI 兼容端点 401/403/423/503、回复总是 "still working"、流式文本与终稿不一致 → `agent-gateway.md`「常见排障入口」
- MCP 工具全部 connection_failed、MCP 客户端协议解析错误（stdout 被污染）→ `agent-gateway.md`「常见排障入口」+「MCP stdio 网关契约」
- 渠道会话短暂出现在本地 web 会话列表、刷新后激活会话被切回本地会话、渠道回合无法在网页暂停 → `web-and-admin.md`「CEO Session List Interaction Contract」+「Active-Turn Button Semantics」
- 网页在渠道会话里发不出消息（回 `channel_session_readonly`）、或发出去了但渠道端没有回复 → `external-agent-api.md`「会话注册表与 key 命名空间」+「回合契约」
- 节点反复调用文件编辑类工具失败、或同一编辑被重复提交 → `tool-and-skill-system.md`「fixed builtin tools」（`filesystem_edit` 平面字段契约）（模型面平面字段与校验面宽车道的分工、`Already applied:` 幂等车道）
- 用户连续发送消息时助手只看到最后一条、或渠道消息收到重复回复 → `runtime-overview.md`「prompt_batch 批次回合内容合并」+ `external-agent-api.md`「回合契约」与「内置官方 QQ 适配器」
- Node error pause is not delivered to the source session, or node-error heartbeats retry forever -> `heartbeat-system.md`「Task Node Error Delivery」
- 验收反复打回同一交付、任务长时间停在「执行→验收」循环而没有判失败（是否存在打回次数上限）→ `main-task-runtime.md`「验收节点：提前创建、激活与握手重派发」（验收拒收无次数上限：打回只发反馈并复活执行节点，不终态化任务）
- 节点详情里 `submit_final_result` 的 `summary` 是 `auto-wrapped plain-text final result`、`evidence` 全是 `Auto-collected tool result from X.` → 该轮没有真实提交，纯文本被当成规划的残留；判据与两条打回车道见 `tool-hydration-and-callable-chain.md`「严格纯文本打回与固定内置集合」
- 验收节点上下文里堆着历次交付全文、或对已被取代的提交下结论、验收 bootstrap 每轮都在变 → `context-and-cache-troubleshooting.md`「验收 bootstrap 定稿与回合尾块」+ `main-task-runtime.md`「信箱投递、重激活与上行传播」（交接通知与回合尾块给 ref + 内联额度内的摘要，超限才带头段指回 ref）
- False "task may be stalled" heartbeat while a long node tool (e.g. `exec` with a large `timeout_seconds`) legitimately runs, or a genuine hang after a tool timeout goes unreported -> `heartbeat-system.md`「Task Stall Detection」
- 想判断任务停滞是不是资源排队造成 → `heartbeat-system.md`「Task Stall Detection」（失速事件自带 `stall_detail`：共享池闸位、本任务闸外排队、上游限流与占位任务，只有事实没有建议）；要看过去那段区间 → `runtime-overview.md`「Worker Performance History」（同一行窗口判读，更长区间用 `perf_inspect`）；性能条「节点队列」等待恒 0 而闸口其实在排队 → `runtime-overview.md`「节点回合闸（执行器存在的成本也要闸）」
- 同时在跑节点数上不去、或界面「限」这个数与配置 `node_dispatch_concurrency` 不一致、想知道池位这一拍为什么加/为什么收、或"某类节点在闸外等而另一类有空格" → `runtime-overview.md`「节点回合闸」与 `main-task-runtime.md`「节点调度读数与共享池」
- 网页刷新打不开（服务端一条 `GET /` 都没记下来，而 `/api/*` 仍照常 200）、日志刷 `WinError 10055`、模型调用频率同时塌下去 → `runtime-overview.md`「worker→web 事件回流车道」（回环连接攒满内核缓冲的取证与判读口径都在那里）
- 节点失败但无系统报错、模型回复疑似被输出上限截断(无工具调用、顶格 output_tokens) → `web-and-admin.md`「Node Detail Error History」+ `config-and-models.md`「Model Request Parameter Defaults」
- 任务树过大打不开（`Failed to open task: Request timeout`）、打开时长时间无树或「加载中(x/xx)」进度异常、从任务树返回任务大厅后长时间卡顿 → `web-and-admin.md`「Task Tree Chunked Load Contract」
- `/api/tasks` 系列请求长时间挂起、任务大厅列表或 worker-status 数据迟迟不到、浏览器标签页唤醒/切回任务大厅后整个页面短暂失联 → `web-and-admin.md`「Web Event Loop Contract」
- 真实回合 HTTP 400 而这份配置从没跑过 `测试连接`（保存与手改、迁移都不探测），或要确认探测到底发了什么字段 → `config-and-models.md`「llm_config 子系统」
- 模型配置弹窗里一打开下拉，整张表单就往上跳或弹窗变高 → `web-and-admin.md`「Frontend Theme And Layout Contract」（展开面板必须脱离滚动容器挂到 body）
- 组弹窗点完确定还要再去右上角点一次保存、保存后组卡又回到旧值、或确认键没随窗口内容变色 → `web-and-admin.md`「Model Config Page And Admin Contract」（确定的写点、adoptLlmModelCatalog、`.is-dirty` 判据）
- 弹窗里点列表底部的勾选框后整窗上跳、标题被顶出可视区、底部多出一截空白，换对象重开还在、刷新才恢复 → `web-and-admin.md`「Frontend Theme And Layout Contract」（弹窗外壳不许当滚动口）
- 验收/节点把合法 PDF（或图片、xlsx 等二进制交付物）判成几十字节空壳、或在其上搜索 `%PDF` 等签名 0 命中 → `tool-hydration-and-callable-chain.md`「外置工具结果信封」（二进制目标的展示契约、字节级搜索与 `filesystem_stat` 只读测量通道）
- 交付物里「路径 + 行号范围」的证据按被引行号读不回原文（差 1 行、差 `start_line-1`、或差 preview 的上下文行数）→ `tool-hydration-and-callable-chain.md`「外置工具结果信封」（content 车道的行号栏）
- 节点 error_text 是不带 `Error executing` 前缀的裸异常（`FileNotFoundError: …png` 一类）而对应工具结果显示 success、或 `content_open` 声称图片已打开却没有进上下文 → `tool-and-skill-system.md`「fixed builtin tools」（图片 reopen 的存在性校验与 overlay 单图降级）
- Token统计窗口打开期间表格不随实时事件变化、搜索/筛选与搜索框内容保留、需点「刷新」才更新、点「刷新」看似没反应或总数与明细都不动、Token统计按钮的 tooltip 比窗口顶部五格多一个缓存命中、顶部五格与逐次明细的命中率对不上、明细行数开着开着变少（跨节点同序号顶行）、模型调用明细按时间倒序/搜索只覆盖窗口或本页的行为疑问、底栏共 N 页但翻页时才发消息、停在历史页收不到新调用或「回到最新」后行数变化、任务级统计有数字却显示「尚无按模型明细」、分发期那一段调用在明细里读不到或「类型」列分不清，或总耗时/首 Token 耗时/思考 Token 显示 `--` → `web-and-admin.md`「Task Token Stats Window Contract」
- Node pause or resume behaves unexpectedly -> `main-task-runtime.md`「Node-Level Pause and Recovery」
- 节点 error-pause 的 remark 是 `PermissionError: [WinError 5] 拒绝访问` 的改名失败，且网页同时在刷同一节点详情 → `runtime-overview.md`「磁盘写保护与治理」（原子改名的争用重试与错误码判据）
- 节点被恢复后仍不推进（暂停标志已清、再无模型调用，最后被 `orphan node parked as candidate` 停成人等）→ `main-task-runtime.md`「Node-Level Pause and Recovery」（恢复的 entry 判读与延迟校验清扫）
- 节点因一次 `submit_final_result` 参数错误就终止，或参数错误文本里没有必填项与类型 → `main-task-runtime.md`「Node-Level Pause and Recovery」（模型交付违约与回包形态故障分账，两道的上限与早停不同）+ `tool-hydration-and-callable-chain.md`「参数错误与状态分类」（必填与可选字段都带类型的契约回贴）
- 模型反复提交空串 / 空数组 / `start_line=0` 这类越界值、而它看到的 schema 里没有那条边界 → `tool-hydration-and-callable-chain.md`「参数错误与状态分类」（模型面投影只裁篇幅不裁判定；字段级 description 到不了模型）
- 节点错误历史行首是 `Upstream payload shape fault detected`（reason 带 `[parameter-marker]` / `[output-capped]` / `[window-clamped]`），或节点因此进入错误暂停 → `main-task-runtime.md`「Node-Level Pause and Recovery」（形态故障分账、早停与首跳处置）+ `web-and-admin.md`「Node Detail Error History」（`upstream_raw_*` 是 provider 边界的原样字节）+ `config-and-models.md`「Model Request Parameter Defaults」
- 节点被可恢复暂停且理由写着 `closed before finish_reason`、渠道看到「响应流未正常终止」，或 worker 日志某跳写着 `finish_reason_seen=0` 而 Token统计该行输入/缓存为 `--` → `main-task-runtime.md`「Node-Level Pause and Recovery」（未终止的流由 provider 标成提供侧故障、链内换槽，不记交付违约）+ `web-and-admin.md`「Task Token Stats Window Contract」
- 停机或暂停打断一批工具调用后，已完成的那条被重新执行一遍（重复外部动作）、或新加的工具在恢复时被自动重放／该重放的只读调用被白问一次模型 → `main-task-runtime.md`「Node-Level Pause and Recovery」（帧活状态留痕与恢复逐条判档）+ `tool-hydration-and-callable-chain.md`「工具可重放声明」
- 任务树节点已显示暂停但任务大厅仍显示处理中、或全局恢复后大厅卡在已暂停 -> `main-task-runtime.md`「Node-Level Pause and Recovery」+ `web-and-admin.md`「Task Hall Action Contract」（状态胶囊判读）
- 重启后任务未自动恢复、优雅重启后仍停在 paused、恢复跑过一次却又落回暂停（上一个进程遗留的 `pause_task` 命令被新 worker 迟到应用）、或出现「本任务遇到异常停止」toast → `main-task-runtime.md`「Graceful Shutdown Pause and Startup Auto-Resume」+ `operations-and-maintenance.md`「重启后任务未自动恢复 / 出现“异常停止”toast」
- 验收节点长期停在「待检验」、被检验的执行节点却在一轮轮重跑（半截验收回合无人接手）→ `main-task-runtime.md`「验收节点：提前创建、激活与握手重派发」（最终验收的两个派发选择器：通知账本 + 握手承诺重派发），worker 日志锚点 `final acceptance round re-dispatched after interruption`
- 验收节点下了「交付物不存在」的结论、而被检验的执行节点从没提交过结果（批量恢复节点后尤其成串），或一份旧拒收被当成本轮裁定 → `main-task-runtime.md`「验收节点：提前创建、激活与握手重派发」（闭合提交冻结闸门 + 派验前前置检查与消费点退回 + 终态判词作废）
- 验收节点被操作员/peer 消息或 `schedule_wake` 到点带起来、验收上下文看不到执行节点刚收到的未读或节点间消息、或给正在等验收的执行节点发消息回执说消息不会被消费 → 设计内：验收节点只由绑定执行节点的待裁定提交派发；验收回合尾块只镜像用户/前门消息（节点间消息不镜像）；给 `waiting_acceptance` 执行节点的用户/父级/前门消息会落箱不唤醒，验收通过则节点终局且不消费，打回后才消费。见 `main-task-runtime.md`「验收节点：提前创建、激活与握手重派发」与「节点信箱与唤醒（现行）」
- 任务显示「已取消」但无人取消过（payload `cancel_requested=false`，常伴随 `Managed task worker exited` 日志）→ `main-task-runtime.md`「Node-Level Pause and Recovery」
- 任务大厅持续显示「worker stale」、托管 worker 崩溃后一直不自动重启、managed-worker.log 长时间不滚动但心跳与进程仍在 → `operations-and-maintenance.md`「托管 worker 看门狗」
- 会话在重启后自动续跑（`shutdown_resume` 内部轮）行为异常 → `heartbeat-system.md`「Shutdown Resume Wake」
- 一条 CEO 请求在网页上并排画出两个气泡（同一回合两段轨道，上面那段停在一句中途旁白）→ `web-and-admin.md`「CEO Turn Timeline Rendering Contract」+ `runtime-overview.md`「`RuntimeAgentSession` 内部做什么」
- cron 定时任务到点不触发、`jobs.json` 停在 `running`/`timeout`/`interrupted`、调度器长时间静默或某次投递疑似挂死 → `heartbeat-system.md`「Cron Reminder Contract」+ `operations-and-maintenance.md`「任务没创建或没推进」
- 记忆复核批次什么时候入队（攒满轮数 / 缓冲躺够 1 小时 / 压缩轮末 / 会话边界），或某段历史的工具明细只剩名字没有原文 → `runtime-overview.md`「Memory Runtime State」（四个冲刷时点、进批次的工具记录按原子组装载、按轮次指纹摘除的写回）
- 看到 `memory/MEMORY.md.bak.*`，或库里条目与投影不一致 → `runtime-overview.md`「Memory Runtime State」（投影只由库再生、外部改动先留证再重绘）+ `operations-and-maintenance.md`「Memory Queue Workflow」
- 限流/上游故障期间某轮记忆没写进去、`memory/failed.jsonl` 有停车记录、或医生检查报 `failed_parked` → `runtime-overview.md`「队列状态机与失败停车语义」+ `operations-and-maintenance.md`「Memory Queue Workflow」
- 记忆页点重试/放弃提示失败、或弹条正文是 `Request timeout` → `web-and-admin.md`「Memory Management Page And Admin Contract」（四条失败正文各自的指向与 30s 客户端超时）
- 用户说「记住/忘掉」但模型照旧，或某轮命中前缀在第二条消息就分叉 → `runtime-overview.md`「Memory Runtime State」（头部基座的压缩边界更新与修订增量）+ `context-and-cache-troubleshooting.md`「长期记忆的头部基座与增量」
- 人设没生效、头部没有「我的人设」、没有人设时模型仍自称 Negi、改了人设旧会话没变、压缩后还留着旧人设通知、或同一条人设通知重复出现 → `runtime-overview.md`「人设块、人设 note 与身份」+ `context-and-cache-troubleshooting.md`「长期记忆的头部基座与增量」
- 初次引导不弹、卡在半途、照片没进 `my_image`、设置页画廊空、保存模型链后人设没入队 → `web-and-admin.md`「First-Run Onboarding And Persona Contract」+ `operations-and-maintenance.md`「人设与初次引导」
- 设置入口或人设按钮的红点不亮/一直亮、空人设点不进去、照片空态文案不对 → `web-and-admin.md`「First-Run Onboarding And Persona Contract」
- 隐藏的 `internal:persona_photo` 会话出现在网页会话列表或记忆复核里 → `web-and-admin.md`「First-Run Onboarding And Persona Contract」
- 某条记忆从长期记忆块里消失了、`memory/index/` 出现新文件、归并区的一整节摘不掉，或医生检查报 `broken_category_links` → `runtime-overview.md`「Memory Runtime State」（退场写 `consolidated_into`、归并区派生、摘整节按句柄寻址、机器压缩只降级不删除）
- 记忆批次停在停车区而错误文本是 `memory_apply_confirm is required`、`line N is outside the memory block`、`line N is outside the candidate list`、`names no category with retired memories` → `runtime-overview.md`「Memory Runtime State」（两次提交那道门与行号的解释时刻）
- 日志审计侧栏角标不更新、原始日志为空或翻页停在空白页、时间显示与事件时间戳不一致、`/api/audit` 503 → `web-and-admin.md`「Log Audit Page And Event Contract」
- Broken image icons, file-route 400s, snapshot path mismatch → `web-and-admin.md`「Inline Markdown Image Rendering And Media Middle Layer」
- 模型重复处理已回答的问题、连续请求尾部反复出现同一条无回复的用户消息、渠道会话里旧提问冒到最新回复下面（像用户重发）；或反向——某轮失败后用户发"继续"，模型答的是上一件成功的事 → `context-and-cache-troubleshooting.md`「残留 paused / pending 转录条目与未回答的用户输入」
- 回复当时显示在界面上、刷新或重启后气泡没了（取消/暂停落在收尾窗口，或批准续跑轮只发了 `message_end`）→ `runtime-overview.md`「`RuntimeAgentSession` 内部做什么」+ `context-and-cache-troubleshooting.md`「残留 paused / pending 转录条目与未回答的用户输入」
- 同一份 heartbeat 规则 / event bundle 在请求体里重复多份、token 逐轮线性上涨而对话无实质推进、或模型被已 success 节点的过期暂停通知误导 → `context-and-cache-troubleshooting.md`「heartbeat / cron 上下文残骸」
- 新增一类运行时中途插入消息、要判断某条消息在哪个阶段被剥 / 是否跨压缩存活、或同一份通知叠了两份 → `context-and-cache-troubleshooting.md`「中途插入消息的家族、位置与生命周期」
- 重启后停靠根节点被重跑、根下出现两颗同标题最终验收、或 `enter_wait` 之后突然新建验收节点 → `main-task-runtime.md`「验收节点：提前创建、激活与握手重派发」+「Node-Level Pause and Recovery」
- 项目一启动会话就自动回一条几小时前的旧任务结果、或某个任务结果迟迟没汇报却也没报错 → `heartbeat-system.md`「Task Terminal Repair Contract」（终态 outbox 投递耐久性与 `abandoned` 留痕）+ `operations-and-maintenance.md`「会话无回复」
- 会话该静默却发话、或该发话却整轮无声；模型写了像哨兵的文本却没静默 → `runtime-overview.md`「3.3 静默回复（`silent` 工具）」+ `web-and-admin.md`「CEO Turn Silent Reply Contract」
- 新增常驻内置控制工具后模型从不使用它、或调用即报错 → `tool-and-skill-system.md`「3.1.1 为什么"常驻"需要独立机制」
- 某工具这一轮报 `tool not available`，而名字仍挂在 `tools[]` 里 → 那是权限收回或资源下架（`denied_tools` 应当点名它），不是契约不在场；契约不在场的形态不报错，而是那一跳交回契约。两条判据与读数见 `tool-hydration-and-callable-chain.md`「契约在场与 callable」+ `context-and-cache-troubleshooting.md`「节点侧排查要点」
- 节点声明了 `seed_tools` 却仍两跳、或派发方读到一份空的/缺席的预置回执 → `tool-hydration-and-callable-chain.md`「派发时预置契约」（`seeded_contracts` / `seeded_dropped` 两份读数的判读口径也在那里）
- 相邻跳 provider `tools[]` 成员在变、且没有真实授权变化（缓存命中随之掉）→ `tool-and-skill-system.md`「Provider Tool Surface」+ `context-and-cache-troubleshooting.md`「跨普通 fresh turn 的 tool schema churn」
- 前缀与 `tools[]` 逐字相同而命中掉了、要数"这一跳跳了几档/为什么跳"，或日志里按 `Model route selected` 统计档位却读出 0 次跨档 → `context-and-cache-troubleshooting.md`「跨档跳的未命中按台账的档位归属读」；组名与成员对不上先读 `GET /api/models/load-balance/status` 的 `group_drift_count`
- 重启后第一跳把头部名单或 `tools[]` 重铺了一遍（停机期间增删过 skill/工具，该跳非缓存 input 冲高、下一跳就回血）→ `context-and-cache-troubleshooting.md`「重启后继续，不能退回 transcript/history fallback」+ `tool-and-skill-system.md`「Pinned Static Declarations」
- 工具被收回后模型仍带着那份参数表，或想知道这个滞后最长多久才消 → `tool-and-skill-system.md`「Provider Tool Surface」（钉住全冻结，增删都落在压缩边界；两次重印之间的变化由一次性通知点名）
- 在 Tool Admin 改了一颗工具的参数定义、在跑会话与在飞节点这一跳还是旧字节 → `tool-and-skill-system.md`「参数表的来源」（参数表的字节由钉住整表供出，编辑到重印边界才被吸收）+ `tool-hydration-and-callable-chain.md`「钉住的 `tools[]` 与它的滞后出口」
- 升级旧实例后，某条工具的角色/启停看起来回到默认，或工具停用后没有任何东西把它捞回来 → `tool-and-skill-system.md`「Tool Admin RBAC」（一次性展开迁移；核心工具允许停用、禁止删除，且没有自动拉起）
- 前门改了尾块运行时契约的一个字段却不出现在请求体里 → `tool-and-skill-system.md`「Provider Tool Surface」（发送前会用 state 重建契约块，装配层那份会被覆盖）
- 上下文里找不到 skill 名单、或头部那份名单看着过期、或相邻跳头部字节反复变 → `tool-and-skill-system.md`「Pinned Static Declarations」+ `context-and-cache-troubleshooting.md`「静态声明钉在请求体头部」
- 交付物/脚本出现在项目根以外的意外目录（尤其 `C:\Users\<近似拼写的用户名>\...` 这种镜像树）、或模型传的相对路径落点与预期不符 → `tool-and-skill-system.md`「Model Path Anchoring Contract」+ `config-and-models.md`「`agents`」
- 模型报告的日期/时间与事实不符（心算毫秒时间戳出错、引用陈旧时间、日报归属日期错误），或模型压根没有"现在是几点"的读数 → `tool-and-skill-system.md`「fixed builtin tools」的 `time` 一条 + `heartbeat-system.md`「Internal-turn time anchors」+ `runtime-overview.md`「用户消息时间锚点」
- 节点/会话设的自设唤醒没响、响得太晚，或一次设闹钟攒出好几份 → `main-task-runtime.md`「节点信箱与唤醒（现行）」+ `heartbeat-system.md`「Cron Reminder Contract」
- 用户消息在请求体里同时出现原文与带 `[消息送达时间]` 行的两个版本，或装饰后缓存命中率骤降 → `context-and-cache-troubleshooting.md`「用户消息时间装饰破坏前缀稳定或相等性去重」
- 某配置整条模型链 400「The input messages must contain no more than one system message」（`InternalError.Algo.InvalidParameter`）、换 key 无效 → `context-and-cache-troubleshooting.md`「线体角色合同」
- 某颗成员反复回通用 400（`inference request is invalid`）而同体量的请求在链上另一颗通过过，或想知道它实际塞得下多大 → `context-and-cache-troubleshooting.md`「节点 400 不一定是 payload 畸形」（`model_size_span:` 那一行报告）+ `config-and-models.md`「Frontdoor Context Window Contract」的观测值清单
- 入站到首个 provider 请求发出耗时异常 → `context-and-cache-troubleshooting.md`「Prompt Cache Family 与 Actual Request」
- 会话/节点疑似卡在 provider 退避重试，但界面没有重试次数与错误信息；或回合长时间不出新内容而连接没断（分不清"退避等待中"与"上游只滴分片不给载荷"） → `runtime-overview.md`「Chat provider 超时与重试边界」+ `web-and-admin.md`「Model Retry Visibility UI Contract」
- 点开会话一直停在 Loading（WebSocket 连上了却一帧不来），而会话列表/任务接口都正常 → `web-and-admin.md`「Local Startup And Launcher Contract」
- 渠道/会话收到英文 "The turn completed without visible assistant text…"，或回合以「这一轮处理失败：响应流未正常终止」结束 → `runtime-overview.md`「Chat provider 超时与重试边界」（空响应与未终止流两条车道、`finish_reason_seen` 日志锚点）
- `temp/tasks/` 出现大量无主 `task_*` 目录（目录数远超任务数）→ `operations-and-maintenance.md`「关键状态文件与目录」+ `runtime-overview.md`「任务侧」
- 临时文件散落在工作区根目录（`.tmp_*` / `tmp_*`、命令重定向落盘），或正式交付物没进默认交付落点 → `runtime-overview.md`「任务侧」+ `tool-and-skill-system.md`「Model Path Anchoring Contract」
- 直接编辑 `skills/` 或 `tools/` 下的文件后注册表不认（新 skill 看不到、改过的正文仍以旧内容参与）→ `tool-and-skill-system.md`「资源目录代检查与语义目录新鲜度」
- 会话固定了指定模型却仍走模型链、固定模型被删除/禁用后未回退、或切换后用量表按旧模型窗口显示 → `config-and-models.md`「会话级模型链与固定模型」+ `web-and-admin.md`「Composer Model Mode Panel」
- 脑图标面板改了链却影响别的会话、会话链保存后仍按全局链发请求、或带图回合模型回"看不到图/没勾多模态" → `config-and-models.md`「会话级模型链与固定模型」+ `web-and-admin.md`「Image Upload Gating」
- 图确实进了请求体、模型仍说没收到像素（勾了多模态也一样）→ `context-and-cache-troubleshooting.md`「Baseline 合同与恢复顺序」+ `web-and-admin.md`「Image Upload Gating」（比 `image_count` 与 `image_blocks_at_tail` 两个读数，别信模型自述）
- 模型面板显示的不是模型链链首、或显示名与实跑绑定对不上（多条绑定共用同一 provider 模型名）→ `config-and-models.md`「角色路由：有序 fallback 与负载均衡组」+ `web-and-admin.md`「Composer Model Mode Panel」
- 节点一直打同一个模型、组内分布不均、或"改了组配置没生效" → `runtime-overview.md`「节点模型路由与准入绑定」+ `operations-and-maintenance.md`「节点模型负载不均 / 一直打同一个模型」
- 加了一颗小窗口成员后压缩比预期更早发生、组弹窗里「组内最大上下文对齐」那行跟着变、或同一任务里有的跳塞得下有的被判没人塞得下 → `config-and-models.md`「Frontdoor Context Window Contract」+ `context-and-cache-troubleshooting.md`「节点侧排查要点」
- 组车道打分看着很闲但链首一直被 429、或某次模型调用的重试轮数不等于任何组的 `maxRetryRounds` → `runtime-overview.md`「节点模型路由与准入绑定」辅助
- 模型链在预算没跑满时就报"耗尽"、审计里 `retryable: true` 与"链耗尽"同时出现、或错误文案里写着与实绑供应商不匹配的名字 → `runtime-overview.md`「Chat provider 超时与重试边界」（两级判据与"链位进过即耗尽"）车道那条（spawn 送审评审与重复预检刻意不过准入，属维持现状的已知边界）
- 改了 `ceo_frontdoor.md` 但某个活会话没反应 → `context-and-cache-troubleshooting.md`「系统提示词的重读跟着记忆块与曝光的戳走」
- 按协议车道判断缓存命中（"这条车道不发 `prompt_cache_key` 所以没命中"）→ `context-and-cache-troubleshooting.md`「Family 与 key 合同」
- 上下文脑图标只按新输入变化、读数长期低于上一请求的真实输入规模 → `context-and-cache-troubleshooting.md`「同 turn 的 append-only 规则被破坏」+ `web-and-admin.md`「Composer Context Usage Meter」
- 拆分前端模块后页面白屏并报 `ApiCore` / `ApiClient is not defined`、新模块请求 404、或页面脚本顺序与测试装载对不上 → `web-and-admin.md`「Frontend Script Layout And Test Loading」
- 长按脑图标不发起压缩、区分线停在「压缩已暂停」、压缩中区分线凭空消失刷新后才出现、一条「上下文压缩中」长期钉住收不掉、中断线旁边像按钮却点不动、心跳唤醒的回合里压缩全程没有线、「会话已压缩」要刷新才出现、区分线跑到回合气泡之前或压缩之后那段内容的下方、刷新后区分线重复两条、往回翻页时区分线在窗口边界重复、渠道会话脑图标没有读数 → `web-and-admin.md`「CEO Compression UI Contract」+「Manual Context Compression」+ `context-and-cache-troubleshooting.md`「Shrink 原因与压缩边界」
- 压缩报成功、`post_tokens` 也降了，但下一回合请求体又回到原大小；或压缩途中发的消息没被回答 → `runtime-overview.md`「压缩窗口：入站闸门与基线写入仲裁」+ `context-and-cache-troubleshooting.md`「Shrink 原因与压缩边界」
- 节点反复收到"缺必填参数"式拒绝、错误首行逐字相同而 arguments 后缀不同，眼看要被无效提交上限判死 → `context-and-cache-troubleshooting.md`「Shrink 原因与压缩边界」（provider 参数块未转义 + 宽容解析吞参数，按协议故障单独计数）
- 节点连续多轮重交同一份被拒载荷、越到后面收到的参数契约越短（只剩 `summary` 残段、`ref` 为空） → `tool-and-skill-system.md`「Duplicate Tool Call Guard」（控制工具 error 回执不参与消息级折叠）
- 模型报告"上一轮我想到哪了"却读不到任何思路、或开了思考回放的 binding 一上线就整条链报 400 → `runtime-overview.md`「思考内容（reasoning）的上下文回放」（写入闸门按整条链取交集，咽喉点只透传）
- 一个回合只剩转圈、长时间没有任何输出，而 usage 里 `completion_tokens` 已经跑了几千 → 先分清"慢"和"不可见"：解码几乎全花在思考上时界面本来看不到任何东西，`web-and-admin.md`「Live Reasoning Row Contract」（实时思考行；Responses 车道没有增量分片，只有耗时与估算 token）+ `runtime-overview.md`「思考内容（reasoning）的上下文回放」
- 会话里「思考 N秒」比真实思考长（工具调用参数写了一大段、或该跳根本没思考也报了秒数）、或回合还在跑而那一行的秒数不再涨 → 计时窗口与两个收口点见 `web-and-admin.md`「Live Reasoning Row Contract」
- 节点详情展开后某个轮次块没有那一行思考读数，或正在跑的那一轮迟迟不出数 → 同一合同（按轮落账才有数，缺口径就不画）见 `web-and-admin.md`「Live Reasoning Row Contract」
- 切回仍在处理的会话看不到期间产生的工具组/工具调用（刷新网页才出现）→ `web-and-admin.md`「CEO Feed View State And Scroll Preservation Contract」
- 用户气泡的「编辑」或模型回复元信息行里的「Fork」要刷新网页才出现、会话被暂停/审批/压缩挡住输入时找不到 Fork 入口、点了按钮提示「当前不可编辑」、或点击消息后用量/复制仍不显形 → `web-and-admin.md`「Message Edit-Resend And Session Fork Contract」+「Per-Turn Token Usage Contract」
- 回合进行中发的补充没被回答、待发送条目上的「插话 / 编辑 / 删除」行为疑问、撤下后条目又被画回来，或条目挂着不动、点删除只回 `follow_up_not_queued` → `web-and-admin.md`「Queued Follow-Ups」
- 渠道会话（QQ/第三方桥）里某颗工具一调就立刻回一句英文"被拦截"、条件放行档位改了某会话没反应、白名单勾了不生效、或界面按风险度自己派生出一份和后端不一样的免审集合 → `tool-and-skill-system.md`「CEO Regulatory Governance Mode」（档位与名单都是一份全局设置，判据只在服务端派生一次）
- 会话页语音按钮右边那颗风险按钮找不到、它的黄色出现在条件放行档上、按钮比附件/语音大一圈、面板与模型链面板叠在一起，或 Tool 页头部还在找监管模式开关 / 命令白名单入口 → `web-and-admin.md`「Composer Risk Panel」+「Exec Approval And Allowlist Admin Contract」（命令白名单入口现在在 exec 工具详情的「执行模式」里）
- 回合卡在半截、下方一直转圈，刷新网页才同步到最终回复（本地与渠道会话同症状，转录里其实已有完整回复）→ `web-and-admin.md`「CEO Websocket Lane Failure Contract」
- 节点详情页的「加载 skill / 加载 tool」chip 风险色全部同一档、展开看不到正文，或契约不在场时直接调用工具的那一行仍画成普通工具行（会话轨道/节点卡片） → `web-and-admin.md`「Context Loader Notices」
- 工具面板的「参数」只有 48 字调用提示、超长入参点开也不回补 → `web-and-admin.md`「CEO Turn Timeline Rendering Contract」（`arguments_truncated` 标记与 `tool-arguments` 按需回取车道）
- 翻看工具输出时新输出把输出框滚动条顶回顶部 → `web-and-admin.md`「CEO Feed View State And Scroll Preservation Contract」
- 模型正在输出最终内容时整页被定住无法滚动（上翻每一帧都被拽回底部）、或回合进行中的流式文本要等收尾才从工具组里搬到外面 → `web-and-admin.md`「Streamed Reply Delta Contract」+「CEO Feed View State And Scroll Preservation Contract」
- 「回到最新」按钮该不该呼吸（回合早已收尾还在闪、或正在进行却不闪）、呼吸光晕盖到周围消息上 → `web-and-admin.md`「CEO Feed View State And Scroll Preservation Contract」
- 日志板块反复出现「接口返回 500：/api/content/read」、节点详情输出框报「加载完整输出失败」或显示「完整输出已被清理」、点开工具行只有开头一段或整页卡死 → `web-and-admin.md`「Node Output Content Read Contract」
- 主题切换不生效或刷新后丢失、亮色主题出现大面积灰底、亮色主题下解锁/初始化窗口仍是暗卡片、侧栏折叠后审计角标消失、页面卡片列数在某宽度错乱、某颗勾选框仍画成系统默认方框、样式写进了不加载的样式文件→ `web-and-admin.md`「Frontend Theme And Layout Contract」
- 新设备一行指令装完发现 skill/tools 比开发机少、重跑安装指令为什么没更新代码、`negi status` 的 `Release:` 行为什么不出现、仓库改名之后已装设备为什么照常更新、`install -Upgrade` 为什么动的是另一个目录 → `operations-and-maintenance.md`「新设备首次安装与升级」
- 装了但终端里 `negi` 不出现、图标点了没反应、系统「已安装的应用」里没有 Negi、卸载后重装说目录非空、或卸载只该删程序却连数据一起没了 → `operations-and-maintenance.md`「新设备首次安装与升级」+「首选一键启动脚本」
- 设置按钮红点不亮或误亮、没查过/查失败也被点亮成"有新版"、「重启并更新」点了服务没回来 → `web-and-admin.md`「Update Notification And Restart-And-Upgrade Contract」+ `operations-and-maintenance.md`「自动检查新版本与「重启并更新」」
- 升级已经成功红点仍亮到下一次检查、或没人改过目录却被报"有本地改动"拒绝换码 → `web-and-admin.md`「Update Notification And Restart-And-Upgrade Contract」+「Frontend Vendor Asset Update Contract」
- 点输入框麦克风提示未就绪／未启用、新设备首点迟迟不出录音、录音结果仍是繁体、或局域网 http 打开时浏览器拒绝麦克风 → `speech-to-text.md`「分发与就绪」「已知边界」
- QQ 发来的语音变成一行本地路径注记、或只有「用户语音，机器识别失败：…」、或语音条迟迟等不到回复 → `external-agent-api.md`「内置官方 QQ 适配器」+ `speech-to-text.md`「常见排障入口」
- 语音气泡播放不出来、语音条右端的 `T` 点开是空的、或用户气泡里仍然显示 `用户语音，机器识别结果：` 这行字 → `speech-to-text.md`「语音气泡与音频字段的存放」+ `web-and-admin.md`「Attachment Bubble Rendering Contract」
- 桌面窗口里点图片跳出浏览器、点「导出配置包」在窗口里没反应、或节点详情「查看完整」开成另一扇窗 → `web-and-admin.md`「In-App Media Viewer And Download Lane」+ `operations-and-maintenance.md`「首选一键启动脚本」
- `/api/tasks`、worker 状态、`perf_samples` 这类"需要 `ORDER BY` 的读"稳定 500 而别的端点正常，日志里 `unable to open database file` 印的又是正确的库绝对路径 → `operations-and-maintenance.md`「读口报 `SQLITE_CANTOPEN`：先分"库打不开"还是"临时目录建不出文件"」

## Maintenance Rules

These rules prevent the docs from re-accumulating redundancy. Every edit to this directory must follow them:

1. Update in place, never append. Rewrite the section that owns the topic; never add trailing addendum sections ("X Update", "X Notes").
2. One contract, one home. State each contract only in its owning doc below; everywhere else use a pointer: `See <doc> "<topic>"` / `详见 <doc>「<topic>」`.
3. Pointers name topics, never section numbers. A cross-doc pointer always carries the doc name (`详见 <doc>「<topic>」`); a bare `见「topic」` has to be a heading in the same doc.
4. Present tense only. No "now / no longer / previously / 现在 / 不再 / 曾经" — that is changelog language. Calendar dates belong to the same family (an incident date prefixed to a measured outcome); keep a log clock time only when the interval itself is the evidence, as in consecutive seconds on two log lines. `scripts/check_architecture_docs.py` reports this family as warnings: the sections whose job is to quote — Maintenance Rules, Debugging Entry Points, each doc's 常见排障入口 — are exempt from the wording patterns and never from dates.
5. Superseded text is deleted outright, never left as "obsolete notes".
6. Structure is the metric; bytes are only an observation. Two invariants: **(a) one doc = one subsystem** — when a doc is being asked to hold a second subsystem, split it into a new doc and register it here (Reading Order, Topic Guide, Topic Ownership). **(b) one leaf section = one contract, at most 25 KB** — a leaf section (a heading whose body contains no deeper heading) above 25 KB is subdivided into `###` sections or split out; a single bullet that carries a whole contract body is a misplaced contract: give it its own heading, or move it to the owning doc and leave a pointer. This README's Debugging Entry Points is a pointer index (rule 5), not a contract leaf. Measure with `python scripts/check_architecture_docs.py` — it walks headings and reports oversized leaves, dangling pointers, and pointers that name a doc which does not exist, plus the rule 4 wording warnings (mechanics in rule 4) and bare `见「topic」` pointers whose topic matches no heading; raw totals come from `wc -c docs/architecture/*.md` (bytes are stable for mixed CJK/English prose; word counts are not). Within the invariants: take no size action — never trim wording or drop facts just to hit a number; per-contract clarity beats bytes, and contract facts are never deleted to satisfy a size. When a doc outgrows itself, work the ladder in order: (a) delete dead/duplicated/superseded content, (b) move misplaced content to its owning doc, (c) split the genuinely grown subsystem into a new doc, (d) subdivide an oversized leaf into one contract per section.

## Topic Ownership

| Topic | Owning doc |
|---|---|
| Runtime layering, message execution chain, session/task relationship, task temp directory resolution, provider timeout boundary, worker performance history (`perf_samples` sampling, retention, and the shared perf read model), silent reply via the `silent` tool (turn-terminal semantics, transcript trace row, compaction exemption, turns ledger write/restore) | `runtime-overview.md` |
| Append-notice distribution contract: sub-tree control transaction and drain ledger, epoch state machine and barriers, single-flight epoch driver with wave-retry/degraded skip, mailbox delivery and uplink propagation, node self-armed wake-up (`task_node_timers` sweep, claim staleness, paused-target filing), epoch-completed vs deferred consumption, acceptance node lifecycle and handshake re-dispatch, spawn review, task tree depth ceiling, force-delete precedence, node-level pause/resume/cancel and circuit breakers, graceful shutdown pause and startup auto-resume | `main-task-runtime.md` |
| Frontdoor context compression contract (`token_compression`, 压缩边界 `boundary_turn_seq`), 思考内容的上下文回放（reasoning 写入闸门与随压缩退出）, provider/model binding flag `reasoningContextEnabled` | `runtime-overview.md` |
| Memory queue state/file semantics (`runtime-overview`); queue/reset operator workflows (`operations-and-maintenance`) | both, split as shown |
| Persona carrier (reserved note, frontdoor head block, tail notice) and first-run onboarding (state file, API, `my_image` photos, hidden `internal:` photo turn) | `runtime-overview.md` (carrier and notice), `web-and-admin.md` (first-run flow and settings surface), `operations-and-maintenance.md` (files and operator reset) |
| Heartbeat continuation contract, internal-turn time anchors, cron at-most-once delivery (including the session-facing `schedule_wake` producer lane and its `self-wake: ` replace family), task terminal repair (including terminal-outbox delivery durability and the `abandoned` state), node-error, distribution-error, and task-stall detection/delivery | `heartbeat-system.md` |
| Tool/skill four concepts, fixed builtin tool contracts, candidate tools and candidate skills, frontdoor head-pinned static declarations (`candidate_skills` / `exec_runtime_policy` / `session_temp_dir`) and their three reprint boundaries, Tool Admin RBAC semantics, CEO 审批档位（全部放行/条件放行）与免审工具名单的判据与 REST 载荷, provider-facing `tools[]` surface contract (frontdoor live RBAC superset vs node pinned bundle and its single reprint point), duplicate-call guard, resource-directory generation checks and semantic catalog freshness, always-callable resident internal control tools (`silent` / `time` / `schedule_wake`, and why fixed-builtin membership does not inject a tool), skill policy role contract (write-side normalization with 400, read-side per-row degradation, and the empty-set fallback that differs from tools) | `tool-and-skill-system.md` |
| Contract presence as the callability authority, contract delivery and the staged-argument `reuse_args` batch lane, re-read and fingerprints, parameter-error guidance lane, externalized tool result envelope, universal tool timeout contract, tool rerun-safe declaration for the recovery lane, strict plain-text rejection and the fixed builtin set, context→callable chain | `tool-hydration-and-callable-chain.md` |
| Actual-request forensics, 线体角色合同（一条请求里 system 至多一份且只能在首位）, append-only rule, cache-miss triage, token preflight diagnostics, mid-insert family registry (insertion position vs lifetime, strip stages, compaction policy) | `context-and-cache-troubleshooting.md` |
| Websocket/UI contracts, composer/media rendering, image upload gating, frontend theme and layout contract, model config admin draft contract, log audit event sink and audit page contract, node detail redraw expansion-state contract, node output content-read API contract, per-call model-call ledger (its writer lanes and `call_kind`) and the Token统计 window, exec approval and command-allowlist admin surface (`/api/resources/tools/exec-*` paths, 503-is-not-empty, the frontend↔mounted-route parity guard, 入口在 exec 工具详情「执行模式」里), composer risk panel surface, container deployment, frontend vendor asset state (tracked manifest vs data-root probe overlay) | `web-and-admin.md` |
| Config schema, hot refresh, model bindings, secret location, deployment unlock, config bundle export/import, role route entries and load-balance group config semantics | `config-and-models.md` |
| Node model route resolution and admission-time binding: ordered route chain vs in-chain load-balance group, quota-bucket observation (rolling RPM + decayed 429 penalty), per-node sticky binding and its rebind triggers, group member budget and intra-group pacing, worker-only process scope and the rollback switch | `runtime-overview.md` |
| External Agent API contract: `externalApi` config and token overlay, ext session registry/keys, turn terminal invariant, SSE event mapping, ext outbound routing, built-in official QQ adapter (`qqBot` config, in-process botpy bridge) | `external-agent-api.md` |
| Agent gateway contract: OpenAI-compatible endpoint (`/api/v1/chat/completions`, session mapping, wait/200-honest-text policy, streaming diff) and MCP stdio gateway (`negi mcp serve`, tool surface, stdout purity) | `agent-gateway.md` |
| Local speech-to-text contract: whisper.cpp subprocess engine and slot serialization, audio normalization and silence/language gating, traditional→simplified post-pass, binary/model provisioning (`stt` defaults' measured basis), voice-clip storage and the model-visibility exclusion, known latency and accuracy envelope | `speech-to-text.md` |
| Sandbox execution plane: session trust tiers and their derivation (`owner` / `third_party_dm` / `third_party_group`, sticky downgrade, fail-closed on unknown), install-state-driven tier model (`restricted` / `unrestricted` → `readonly` / `jailed` / `host_full`), read-only tool allowlist and default-deny rule, owner read-only denylist and `governed` exec clamp, read-root boundary and its call-scoped pinning, control-plane rejection on the synthetic lane, memory gate intake points and read-side suppression, third-party sender stamp | `sandbox-execution-plane.md` |
| Startup/deploy/troubleshooting order, the post-install entry surface (`negi` command stub, install-root pointer, desktop/Start Menu shortcuts, uninstall entry and its keep/purge guards), install root vs data root storage layout (which paths hang on which root, where to read the effective data root) and data-root resolution order, memory CLI, Docker compose | `operations-and-maintenance.md` |
