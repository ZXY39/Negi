# Negi Architecture Docs

Start here when you are new to the repository or when a change crosses subsystem boundaries.

## Reading Order

1. `runtime-overview.md`
2. `main-task-runtime.md` when the change touches task message distribution / append notices, acceptance handoff and spawn review, node-level pause and recovery, or shutdown pause / startup auto-resume
3. `operations-and-maintenance.md` when you need to run, debug, or deploy the system
4. `context-and-cache-troubleshooting.md` when the change touches prompt caching, context retention, append-only request growth, or request artifact forensics
5. `tool-and-skill-system.md`
6. `tool-hydration-and-callable-chain.md` when the change touches tool hydration and promotion, parameter-error guidance, the universal tool timeout, or stage gating
7. `web-and-admin.md`
8. `heartbeat-system.md` when the change touches heartbeat, long-running CEO tool wakeups, or live reminder behavior
9. `config-and-models.md` when the change touches runtime config, provider/model routing, or model bindings
10. `external-agent-api.md` when the change touches the external bridge API (`/api/v1`), external sessions, outbound routing to bridges, or the built-in official QQ adapter
11. `agent-gateway.md` when the change touches the OpenAI-compatible endpoint (`/api/v1/chat/completions`) or the MCP stdio gateway (`negi mcp serve`)
12. `speech-to-text.md` when the change touches local voice transcription: the composer mic button, inbound channel voice, the `stt` config section, or the vendored whisper.cpp binary

## Topic Guide

- `runtime-overview.md`
  Use for session lifecycle, frontdoor/runtime flow, tool execution flow, cross-module runtime behavior, frontdoor context compression, and node model route resolution.
- `main-task-runtime.md`
  Use for the task side of the runtime: append-notice distribution epochs, mailbox delivery and barriers, acceptance node lifecycle and spawn review, node-level pause/resume/cancel, and graceful shutdown pause with startup auto-resume.
- `operations-and-maintenance.md`
  Use for startup workflows, troubleshooting order, high-risk change types, memory queue/reset workflows, and Docker deployment.
- `context-and-cache-troubleshooting.md`
  Use for prompt cache misses, context shrink/continuity regressions, actual-request artifact forensics, and before changing node or CEO context strategies.
- `tool-and-skill-system.md`
  Use for the four tool/skill concepts, fixed builtin tool contracts, candidate tools, skill loading, tool RBAC, and resource-directory generation checks.
- `tool-hydration-and-callable-chain.md`
  Use for how one successful `load_tool_context` becomes a next-turn callable: hydration ledger, promotion and re-read, parameter-error lane, externalized result envelope, universal tool timeout, tool rerun-safe declaration for the recovery lane, stage gating, and the context→callable chain.
- `web-and-admin.md`
  Use for websocket contracts, frontend/backend responsibility boundaries, and operator-visible UI behavior.
- `heartbeat-system.md`
  Use for heartbeat turns, task-terminal/stall/distribution-error wakeups, shutdown-resume session wakes, and the boundary between heartbeat and the CEO inline tool reminder sidecar.
- `config-and-models.md`
  Use for config source-of-truth rules and role-to-model resolution.
- `external-agent-api.md`
  Use for the channel-agnostic headless API consumed by third-party bridges and the built-in official QQ adapter: auth, external session registry, turn terminal invariant, SSE event mapping, and ext outbound routing.
- `agent-gateway.md`
  Use for the out-of-the-box agent integration surfaces built on the External Agent API: the OpenAI-compatible chat endpoint (session mapping, wait/timeout and streaming semantics) and the MCP stdio gateway (`negi mcp serve`, tool surface, stdout purity).
- `speech-to-text.md`
  Use for local voice-to-text: the whisper.cpp subprocess engine and why it is not kept resident, audio normalization and silence gating, binary/model provisioning, and what the model tier choice trades in latency and accuracy.

## Debugging Entry Points

- Reminder UI or `ceo.tool.reminder` timeout-stop failures → `heartbeat-system.md` (+ `web-and-admin.md` for UI rendering)
- Node cache misses, restart-seed continuity, token preflight/compression questions → `context-and-cache-troubleshooting.md` (+ `runtime-overview.md`)
- Append-notice delivery, `waiting_children` replay, task-tree banner after distribution → `runtime-overview.md` + `operations-and-maintenance.md`
- 追加通知后任务停在 paused、任务树出现红色「消息分发失败」横幅、epoch state=failed → `main-task-runtime.md`「分发状态机与屏障」+ `operations-and-maintenance.md`「task_append_notice / 任务消息分发维护要点」
- 分发期间想知道"每个节点到底卡在哪一步"（消息已受理但未投递 / 节点停在检查点等屏障释放 / 还压在一枚在飞模型请求上）、或节点详情已显示重试中而 toast 不出现 → `web-and-admin.md`「Task Message Distribution UI Contract」+「Model Retry Visibility UI Contract」
- 任务非终态、一批验收节点长期 `in_progress` 而派发计数为 0（worker 日志只有 `node frozen by distribution hold`，零 ERROR 零告警）→ `main-task-runtime.md`「子树级控制事务与排空账本」（屏障释放集=冻结集）
- 同一工具反复返回同一条 `Error executing <tool>`、节点长期 `in_progress` 且从不进错误暂停 → `main-task-runtime.md`「Node-Level Pause and Recovery」（`runtime_fault:` 断路器）
- 操作员点了暂停、节点 `pause_requested=True` 而 `is_paused` 长期不变 → `main-task-runtime.md`「Node-Level Pause and Recovery」（脱离派发面后的暂停直落）
- 渠道会话（`ext:`）任务长期静默却收不到失速提醒 → `heartbeat-system.md`「Task Stall Detection」（受众谓词与它要过的两道闸门）
- 父节点在验收节点仍非终态时提前进入 `before_model`、或出现意外 `superseded by newer spawn round`（含节点被 resume 后白跑一段再落该原因）、或 `resume` 返回 `entry_settled` → `operations-and-maintenance.md`「spawn 轮次过早完成或子节点被意外 supersede」+ `main-task-runtime.md`「Node-Level Pause and Recovery」
- 派生评审把整批候选全拦下来、把带 `requires_acceptance` 的候选判成"验收内嵌/自产自销"、或想知道评审请求本身有多大/重发过几次 → `main-task-runtime.md`「任务树深度上限」（送审候选的解析结构、派生审查的 fail-closed 默认结果与 `review_attempts` / `review_request_chars`）
- 任务已终态但仍有节点显示处理中（`in_progress`）→ `operations-and-maintenance.md`「残留节点自愈」+ `main-task-runtime.md`「Node-Level Pause and Recovery」
- `task_progress` 显示的运行/检验状态与真实执行不符（陈旧帧被当作运行中、未派发的验收节点被当作检验中）→ `main-task-runtime.md`「Node-Level Pause and Recovery」（活性标注与判读合同）+ `tools/task_progress_cn/resource.yaml`（工具描述判读规则）
- 任务树节点徽标与真实执行不符（未派发的检验节点显示「检验中」、被检验的执行节点跟着显示「检验中」、非终态节点出现 `IN_PROGRESS` 原文、检验节点不见了）→ `web-and-admin.md`「Task Tree Live Sync And Self-Healing Contract」+「Task Message Distribution UI Contract」
- 任务树刷新时轮次下拉框自己收起/跳回默认轮次 → `web-and-admin.md`「Task Tree Live Sync And Self-Healing Contract」（重绘后的稳定身份与展开态恢复）
- 节点详情刷新时展开的阶段卡或某轮的工具面板自己折回去、切走再切回对不上 → `web-and-admin.md`「Node Detail Redraw And Expansion State Contract」（捕获时机与按轮账本）
- 本地会话列表拖动后的顺序被打回原样、或换浏览器顺序不一致 → `web-and-admin.md`「CEO Session List Interaction Contract」（手动顺序只存前端 localStorage）
- 改了密码旧密码还能用、勾了自动解锁重启仍要输密码、或「锁定项目」后界面仍可读数 → `config-and-models.md`「Deployment Unlock Contract」（改密只换信封不换钥匙、解锁顺序 env→本地密钥文件→口令、锁定只清 web 进程内存主密钥）
- 任务与会话数据不在代码检出目录里、首次初始化时选过别的盘、重启后旧任务像空库一样找不到、或按文档路径找不到 `console.log`/`managed-worker.log` → `operations-and-maintenance.md`「关键状态文件与目录」（安装根与数据根各装什么、`G3KU_DATA_DIR`→指针→cwd 的解析顺序、盘上两个 `.g3ku` 的嵌套、`get_data_dir()` 其实是安装根、指针丢失即回退 cwd 的形态）
- 导出配置包报 `project_locked`、导入后旧登录密码解不开、导入的包被判成越界路径、新设备导入只报一个没有 detail 的 `HTTP 500`（该机还没走完首屏、`config.json` 不存在）、或导入完成后 worker 仍走旧模型链 → `config-and-models.md`「Config Bundle Export And Import」（包带的是主密钥本身、master.key 与 auto-unlock.key 不进包、导入是整体替换且需重启）
- 保存模型密钥报 `secret overlay is undecryptable with the current master key; refusing to overwrite`、或所有 apikey 同时变空 → `config-and-models.md`「secret 的真实去向」（覆盖层被截成 0 字节时守卫会拒绝一切写入；先取证改名该文件再 锁定→解锁 解除，写入耐久性与残留缺口同段）
- 任务疑似卡死、要定位任务在等哪个节点，或要查某节点最后一批工具调用的完整入参/状态/出参 → `main-task-runtime.md`「Node-Level Pause and Recovery」（等待节点输出行）+「任务侧」（`task_node_detail` summary 档 `latest_tool_calls_full`）
- 恢复后节点上下文突然只剩几条消息、`task_model_calls` 里 `request_seed_source` 出现 `fallback_seed_*`、帧 `messages_ref` 为空 → `runtime-overview.md`「任务侧」（帧写入的 messages_ref 保留规则）+ `context-and-cache-troubleshooting.md`「append-only 规则」
- 磁盘满（Errno 28 / SQLITE_FULL）、0 字节错误日志、节点连锁 error-pause、artifact 变成 .gz、手动删除任务后产出在 deliverables/、任务大厅按大小排序定位大任务、managed-worker.log 轮转、console.log 长跑不收敛/被日志风暴撑大、runtime.sqlite3 收缩、task_events 表静默零写入 → `operations-and-maintenance.md`「磁盘满」+ `runtime-overview.md`「磁盘写保护与治理」+ `web-and-admin.md`（治理 UI 与大小/排序契约）
- Execution/final-acceptance reflation (node vanishing from browser tree, acceptance visibility) → `runtime-overview.md` + `web-and-admin.md`
- worker 恢复后内存涨到 GB 级、RSS 风暴后不回落、要判"泄漏还是瞬态峰"、要查某一刻同时驻留的是谁 → `operations-and-maintenance.md`「内存峰值 / 恢复风暴后 RSS 不回落」
- 任务大厅卡顿/滚动卡死、Edge 窗口「未响应」或崩溃 → `operations-and-maintenance.md`「任务大厅卡顿 / 冻结（浏览器端）」+ `web-and-admin.md`「Web Event Loop Contract」
- 任务卡片 token 数字在涨却看不到任何反馈 → `web-and-admin.md`「Web Event Loop Contract」（卡片增量补丁的增长提示与 reduced-motion 例外）
- 打开/切换 CEO 或渠道会话等待数十秒到几分钟、首帧快照体积异常 → `web-and-admin.md`「Web Event Loop Contract」+「CEO Turn Timeline Rendering Contract」（快照只发 delta 与滚动投影合同）
- 回合进行中看不到新工具调用/新阶段，最终答复落地才一口气出现 → `web-and-admin.md`「CEO Websocket Lane Failure Contract」（单写者与丢帧计数）+「Streamed Reply Delta Contract」（轨道补丁的合并窗口）
- Multimodal image not reaching model or fabricated image content → `runtime-overview.md` + `web-and-admin.md` (web upload/reopen) or `external-agent-api.md` (bridge inbound attachments)
- A config refresh disrupts an in-flight turn → `config-and-models.md`「配置热刷新」
- Same task result pushed to the channel multiple times → `heartbeat-system.md`「Task Terminal Repair Contract」
- Channel/bridge reply emits `## Runtime Tool Contract` or `[G3KU_STAGE_*]` stage-block/internal context text -> `runtime-overview.md`「frontdoor 与任务运行时的关系」(回显守卫) + `external-agent-api.md` (outbound sanitize contract)
- 请求体里阶段块成批堆在上下文最前面、与自己的用户消息/最终回复脱节 → `runtime-overview.md`「stage_compaction」（块锚点三级取定与顺序不变量）
- 工具上一轮还能调、这一轮从 `callable_tools` 消失，但仍留在 `tools[]` 上（既不在 `denied_tools`、也不在 `undeclared_candidates`），且 `hydration_evicted_executor_names` 里没有它 → 不是 LRU 也不是权限收回，而是它的 toolskill 正文被阶段裁撤或压缩移出上下文，按"契约不在 ⇒ 能力不在"被撤销；判据、两个载体与 `keep_tools` 保留道见 `tool-hydration-and-callable-chain.md`「hydrated tools」
- 裁撤后阶段块里没有 `## 保留契约` 段、或 `keep_tools` 点了名字却没留住 → 先确认同批带了 `drop_completed_stage_tool_detail=true` 与 `completed_stage_summary`（不带 drop 时 `keep_*` 按参数非法/忽略处理），再看该名字是否属于本阶段真的加载过的目标集；提取失败不写条目，回执会点名，机制见 `tool-hydration-and-callable-chain.md`「阶段门控与 callable 收紧」
- 第三方桥接应用接入（/api/v1 鉴权、外部会话、事件流、主动推送不到达）→ `external-agent-api.md`「常见排障入口」
- 会话转录/Web UI 有回复但渠道端（QQ 等）收不到、渠道「能收不能发」、重启后旧提醒补投或重复 → `external-agent-api.md`「出站路由（主动推送）」+「持久 outbox」+「内置官方 QQ 适配器」
- 渠道端只收到文件签名链接、模型回复里引用的图片/文件没有作为媒体消息送达 → `external-agent-api.md`「事件流」出站附件契约 +「内置官方 QQ 适配器」
- 桥接端 `reply.final` 的 `usage` 缺失或数值不像本轮 → `external-agent-api.md`「事件流」（usage 随 `message_end` 由发出侧带出，relay 只透传）+ `web-and-admin.md`「Per-Turn Token Usage Contract」
- 渠道会话在网页目录里删了还在、清过历史的行仍显示可发消息、或注销身份被拒（`outbox_pending` / `scheduled_target`）→ `web-and-admin.md`「Channel Session Clear Contract」+ `external-agent-api.md`「会话注册表与 key 命名空间」
- 官方 QQ 机器人面板报错、不连接或收不到消息 → `external-agent-api.md`「内置官方 QQ 适配器」+「常见排障入口」
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
- 节点详情里 `submit_final_result` 的 `summary` 是 `auto-wrapped plain-text final result`、`evidence` 全是 `Auto-collected tool result from X.` → 该轮没有真实提交，纯文本被当成规划的残留；判据与两条打回车道见 `tool-hydration-and-callable-chain.md`「阶段门控与 callable 收紧」
- 验收节点上下文里堆着历次交付全文、或对已被取代的提交下结论、验收 bootstrap 每轮都在变 → `context-and-cache-troubleshooting.md`「验收 bootstrap 定稿与回合尾块」+ `main-task-runtime.md`「信箱投递、重激活与上行传播」（交接通知与回合尾块只给 ref + 有界摘要）
- False "task may be stalled" heartbeat while a long node tool (e.g. `exec` with a large `timeout_seconds`) legitimately runs, or a genuine hang after a tool timeout goes unreported -> `heartbeat-system.md`「Task Stall Detection」
- 想判断任务停滞是不是资源排队造成、或性能条只看得到「现在」而要看过去那段区间 → `runtime-overview.md`「Worker Performance History」（失速事件已自带一行窗口判读，更长的窗口用 `perf_inspect`）；性能条「节点队列」等待恒 0 而闸口其实在排队 → `runtime-overview.md`「节点回合闸（执行器存在的成本也要闸）」
- 同时在跑节点数上不去、或界面「限」这个数与配置 `node_dispatch_concurrency` 不一致、想知道闸位这一拍为什么加/为什么收 → `runtime-overview.md`「节点回合闸」
- 网页刷新打不开（服务端一条 `GET /` 都没记下来，而 `/api/*` 仍照常 200）、日志刷 `WinError 10055`、模型调用频率同时塌下去 → `runtime-overview.md`「worker→web 事件回流车道」（回环连接攒满内核缓冲的取证与判读口径都在那里）
- 节点失败但无系统报错、模型回复疑似被输出上限截断(无工具调用、顶格 output_tokens) → `web-and-admin.md`「Node Detail Error History」+ `config-and-models.md`「Model Request Parameter Defaults」
- 任务树过大打不开（`Failed to open task: Request timeout`）、打开时长时间无树或「加载中(x/xx)」进度异常、从任务树返回任务大厅后长时间卡顿 → `web-and-admin.md`「Task Tree Chunked Load Contract」
- `/api/tasks` 系列请求长时间挂起、任务大厅列表或 worker-status 数据迟迟不到、浏览器标签页唤醒/切回任务大厅后整个页面短暂失联 → `web-and-admin.md`「Web Event Loop Contract」
- 真实回合 HTTP 400 而保存时的探测没拦住（配置来自手改或迁移）、或要确认探测到底发了什么字段 → `config-and-models.md`「llm_config 子系统」
- 模型配置弹窗里一打开下拉，整张表单就往上跳或弹窗变高 → `web-and-admin.md`「Frontend Theme And Layout Contract」（展开面板必须脱离滚动容器挂到 body）
- 验收/节点把合法 PDF（或图片、xlsx 等二进制交付物）判成几十字节空壳、或在其上搜索 `%PDF` 等签名 0 命中 → `tool-hydration-and-callable-chain.md`「外置工具结果信封」（二进制目标的展示契约、字节级搜索与 `filesystem_stat` 只读测量通道）
- 节点 error_text 是不带 `Error executing` 前缀的裸异常（`FileNotFoundError: …png` 一类）而对应工具结果显示 success、或 `content_open` 声称图片已打开却没有进上下文 → `tool-and-skill-system.md`「fixed builtin tools」（图片 reopen 的存在性校验与 overlay 单图降级）
- Token统计窗口打开期间表格不随实时事件变化、搜索/筛选与搜索框内容保留、需点「刷新」才更新、点「刷新」看似没反应或总数与明细都不动、副标题的总输入比顶部大一个缓存命中、明细行数开着开着变少（跨节点同序号顶行）、模型调用明细按时间倒序/搜索只覆盖窗口或本页的行为疑问、底栏共 N 页但翻页时才发消息、停在历史页收不到新调用或「回到最新」后行数变化、任务级统计有数字却显示「尚无按模型明细」、分发期那一段调用在明细里读不到或「类型」列分不清，或总耗时/首 Token 耗时/思考 Token 显示 `--` → `web-and-admin.md`「Task Token Stats Window Contract」
- Node pause or resume behaves unexpectedly -> `main-task-runtime.md`「Node-Level Pause and Recovery」
- 节点被恢复后仍不推进（暂停标志已清、再无模型调用，最后被 `orphan reaped at task resume` 收尸）→ `main-task-runtime.md`「Node-Level Pause and Recovery」（恢复的 entry 判读与延迟校验清扫）
- 节点因一次 `submit_final_result` 参数错误就终止，或参数错误文本里没有必填项与类型 → `main-task-runtime.md`「Node-Level Pause and Recovery」（模型交付违约与回包形态故障分账，各有同值上限）+ `tool-hydration-and-callable-chain.md`「参数错误与状态分类」（必填与可选字段都带类型的契约回贴）
- 模型反复提交空串 / 空数组 / `start_line=0` 这类越界值、而它看到的 schema 里没有那条边界 → `tool-hydration-and-callable-chain.md`「参数错误与状态分类」（模型面投影只裁篇幅不裁判定；字段级 description 到不了模型）
- 节点反复收到 `Invalid final result submission detected` 而任务不被判死、错误首行写 `provider output limit truncated` 或 `reasoning-only` → `main-task-runtime.md`「Node-Level Pause and Recovery」（回包形态故障单独计数，不占模型的无效提交预算）
- 节点被可恢复暂停且理由写着 `closed before finish_reason`、渠道看到「响应流未正常终止」，或 worker 日志某跳写着 `finish_reason_seen=0` 而 Token统计该行输入/缓存为 `--` → `main-task-runtime.md`「Node-Level Pause and Recovery」（未终止的流由 provider 标成提供侧故障、链内换槽，不记交付违约）+ `web-and-admin.md`「Task Token Stats Window Contract」
- 停机或暂停打断一批工具调用后，已完成的那条被重新执行一遍（重复外部动作）、或新加的工具在恢复时被自动重放／该重放的只读调用被白问一次模型 → `main-task-runtime.md`「Node-Level Pause and Recovery」（帧活状态留痕与恢复逐条判档）+ `tool-hydration-and-callable-chain.md`「工具可重放声明」
- 任务树节点已显示暂停但任务大厅仍显示处理中、或全局恢复后大厅卡在已暂停 -> `main-task-runtime.md`「Node-Level Pause and Recovery」+ `web-and-admin.md`「Task Hall Action Contract」（状态胶囊判读）
- 重启后任务未自动恢复、优雅重启后仍停在 paused、恢复跑过一次却又落回暂停（上一个进程遗留的 `pause_task` 命令被新 worker 迟到应用）、或出现「本任务遇到异常停止」toast → `main-task-runtime.md`「Graceful Shutdown Pause and Startup Auto-Resume」+ `operations-and-maintenance.md`「重启后任务未自动恢复 / 出现“异常停止”toast」
- 验收节点长期停在「待检验」、被检验的执行节点却在一轮轮重跑（半截验收回合无人接手）→ `main-task-runtime.md`「验收节点：提前创建、激活与握手重派发」（最终验收的两个派发选择器：通知账本 + 握手承诺重派发），worker 日志锚点 `final acceptance round re-dispatched after interruption`
- 验收节点下了「交付物不存在」的结论、而被检验的执行节点从没提交过结果（批量恢复节点后尤其成串），或一份旧拒收被当成本轮裁定 → `main-task-runtime.md`「验收节点：提前创建、激活与握手重派发」（闭合提交冻结闸门 + 派验前前置检查与消费点退回 + 终态判词作废）
- 任务显示「已取消」但无人取消过（payload `cancel_requested=false`，常伴随 `Managed task worker exited` 日志）→ `main-task-runtime.md`「Node-Level Pause and Recovery」
- 任务大厅持续显示「worker stale」、托管 worker 崩溃后一直不自动重启、managed-worker.log 长时间不滚动但心跳与进程仍在 → `operations-and-maintenance.md`「托管 worker 看门狗」
- 会话在重启后自动续跑（`shutdown_resume` 内部轮）行为异常 → `heartbeat-system.md`「Shutdown Resume Wake」
- 一条 CEO 请求在网页上并排画出两个气泡（同一阶段两张卡，上面那张停在一句中途旁白）→ `web-and-admin.md`「CEO Turn Timeline Rendering Contract」+ `runtime-overview.md`「`RuntimeAgentSession` 内部做什么」
- cron 定时任务到点不触发、`jobs.json` 停在 `running`/`timeout`/`interrupted`、调度器长时间静默或某次投递疑似挂死 → `heartbeat-system.md`「Cron Reminder Contract」+ `operations-and-maintenance.md`「任务没创建或没推进」
- 记忆复核批次不足窗口阈值轮数就入队，或阶段跨批次重复出现 → `runtime-overview.md`「Memory Runtime State」
- 限流/上游故障期间某轮记忆没写进去、`memory/failed.jsonl` 有停车记录、或医生检查报 `failed_parked` → `runtime-overview.md`「队列状态机与失败停车语义」+ `operations-and-maintenance.md`「Memory Queue Workflow」
- 用户说「记住/忘掉」但模型照旧，或某轮命中前缀在第二条消息就分叉 → `runtime-overview.md`「Memory Runtime State」（快照会话级冻结与采纳点）+ `context-and-cache-troubleshooting.md`「长期记忆快照的会话级冻结」
- 日志审计侧栏角标不更新、原始日志为空或翻页停在空白页、时间显示与事件时间戳不一致、`/api/audit` 503 → `web-and-admin.md`「Log Audit Page And Event Contract」
- Broken image icons, file-route 400s, snapshot path mismatch → `web-and-admin.md`「Inline Markdown Image Rendering And Media Middle Layer」
- 模型重复处理已回答的问题、连续请求尾部反复出现同一条无回复的用户消息、渠道会话里旧提问冒到最新回复下面（像用户重发）；或反向——某轮失败后用户发"继续"，模型答的是上一件成功的事 → `context-and-cache-troubleshooting.md`「残留 paused / pending 转录条目与未回答的用户输入」
- 同一份 heartbeat 规则 / event bundle 在请求体里重复多份、token 逐轮线性上涨而对话无实质推进、或模型被已 success 节点的过期暂停通知误导 → `context-and-cache-troubleshooting.md`「heartbeat / cron 上下文残骸」
- CEO 阶段卡里被裁撤/收口的阶段只剩一颗「取回本阶段的调用记录」、点开才出轮次 → 显示层带行与按 `rounds_archive_ref` 取档的车道，见 `web-and-admin.md`「CEO Stage Trace Round Rendering Contract」的阶段轨道条目
- 同一个阶段在会话里刷好几遍、收尾总结只长在最后那遍 → 同一文档的副本归并条目（只归并已收尾的副本，未收尾的半截各画一张是设计）
- 回合进行中最新气泡夹着历史阶段一起出现（新阶段带着旧阶段）、旧阶段排在新阶段下面，或刷新网页才恢复正常 → 同上条目：live 帧 delta 只按投影互比，源正文的回填发生在 delta 定型之后；轨道顺序按 `stage_index` 升序合并，不按到达顺序
- 被模型点名移出上下文的阶段，卡片徽章仍写「完成」而不是「已移出上下文」（或反过来）→ 裁撤标记有没有穿过出帧面与前端阶段白名单，见 `web-and-admin.md`「CEO Turn Timeline Rendering Contract」
- 一批里 `submit_next_stage` 被拒之后，同批其它工具全收到 `failed earlier in this turn` / `failed earlier in this batch` → `tool-hydration-and-callable-chain.md`「阶段门控与 callable 收紧」（只有阶段账本被动过才牵连同批）
- 工具执行时先在阶段外面冒出来、过一会才进阶段卡（气泡跳一下），或某条工具的"加载中"迟迟不消失 → 同一文档的在飞工具行条目（落位、按 `tool_call_id` 退场、`submit_next_stage` 不建行）
- 项目一启动会话就自动回一条几小时前的旧任务结果、或某个任务结果迟迟没汇报却也没报错 → `heartbeat-system.md`「Task Terminal Repair Contract」（终态 outbox 投递耐久性与 `abandoned` 留痕）+ `operations-and-maintenance.md`「会话无回复」
- 会话该静默却发话、或该发话却整轮无声；模型写了像哨兵的文本却没静默 → `runtime-overview.md`「3.3 静默回复（`silent` 工具）」+ `web-and-admin.md`「CEO Turn Silent Reply Contract」
- 新增常驻内置控制工具后模型从不使用它、或调用即报错 → `tool-and-skill-system.md`「3.1.1 为什么"常驻"需要独立机制」
- 某工具上一轮还能调、这一轮报 `tool not available`，且 `load_tool_context` 回 `ok:true` 也不把它提升 → `tool-hydration-and-callable-chain.md`「hydrated tools」「加载门控」+ `context-and-cache-troubleshooting.md`「节点侧排查要点」（压缩不是这条的原因；名字仍在 `tools[]` 里而本轮调不动，是水合台账被淘汰的合法状态，见 `tool-and-skill-system.md`「Provider Tool Surface」）
- 相邻跳 provider `tools[]` 成员在变、且没有真实授权变化（缓存命中随之掉）→ `tool-and-skill-system.md`「Provider Tool Surface」+ `context-and-cache-troubleshooting.md`「跨普通 fresh turn 的 tool schema churn」
- 前门改了尾块运行时契约的一个字段却不出现在请求体里 → `tool-and-skill-system.md`「Provider Tool Surface」（发送前会用 state 重建契约块，装配层那份会被覆盖）
- 上下文里找不到 `candidate_skills`、或头部那份技能名单看着过期、或相邻跳头部字节反复变 → `tool-and-skill-system.md`「Pinned Static Declarations」+ `context-and-cache-troubleshooting.md`「静态声明钉在请求体头部」
- 交付物/脚本出现在项目根以外的意外目录（尤其 `C:\Users\<近似拼写的用户名>\...` 这种镜像树）、或模型传的相对路径落点与预期不符 → `tool-and-skill-system.md`「Model Path Anchoring Contract」+ `config-and-models.md`「`agents`」
- 模型报告的日期/时间与事实不符（心算毫秒时间戳出错、引用陈旧时间、日报归属日期错误）→ `heartbeat-system.md`「Internal-turn time anchors」+ `runtime-overview.md`「用户消息时间锚点」
- 用户消息在请求体里同时出现原文与带 `[消息送达时间]` 行的两个版本，或装饰后缓存命中率骤降 → `context-and-cache-troubleshooting.md`「用户消息时间装饰破坏前缀稳定或相等性去重」
- 某配置整条模型链 400「The input messages must contain no more than one system message」（`InternalError.Algo.InvalidParameter`）、换 key 无效 → `context-and-cache-troubleshooting.md`「线体角色合同」
- 入站到首个 provider 请求发出耗时异常 → `context-and-cache-troubleshooting.md`「Prompt Cache Family 与 Actual Request」
- 会话/节点疑似卡在 provider 退避重试，但界面没有重试次数与错误信息；或回合长时间不出新内容而连接没断（分不清"退避等待中"与"上游只滴分片不给载荷"） → `runtime-overview.md`「Chat provider 超时与重试边界」+ `web-and-admin.md`「Model Retry Visibility UI Contract」
- 点开会话一直停在 Loading（WebSocket 连上了却一帧不来），而会话列表/任务接口都正常 → `web-and-admin.md`「Local Startup And Launcher Contract」
- 渠道/会话收到英文 "The turn completed without visible assistant text…"，或回合以「这一轮处理失败：响应流未正常终止」结束 → `runtime-overview.md`「Chat provider 超时与重试边界」（空响应与未终止流两条车道、`finish_reason_seen` 日志锚点）
- `temp/tasks/` 出现大量无主 `task_*` 目录（目录数远超任务数）→ `operations-and-maintenance.md`「关键状态文件与目录」+ `runtime-overview.md`「任务侧」
- 临时文件散落在工作区根目录（`.tmp_*` / `tmp_*`、命令重定向落盘）→ `runtime-overview.md`「任务侧」+ `tool-and-skill-system.md`「四个概念必须分清」
- 直接编辑 `skills/` 或 `tools/` 下的文件后注册表不认（新 skill 看不到、改过的正文仍以旧内容参与）→ `tool-and-skill-system.md`「资源目录代检查与语义目录新鲜度」
- 会话固定了指定模型却仍走模型链、固定模型被删除/禁用后未回退、或切换后用量表按旧模型窗口显示 → `config-and-models.md`「会话级模型链与固定模型」+ `web-and-admin.md`「Composer Model Mode Panel」
- 脑图标面板改了链却影响别的会话、会话链保存后仍按全局链发请求、或混链会话带图不发 → `config-and-models.md`「会话级模型链与固定模型」+ `web-and-admin.md`「Image Upload Gating」
- 模型面板显示的不是模型链链首、或显示名与实跑绑定对不上（多条绑定共用同一 provider 模型名）→ `config-and-models.md`「角色路由：有序 fallback 与负载均衡组」+ `web-and-admin.md`「Composer Model Mode Panel」
- 节点一直打同一个模型、组内分布不均、或"改了组配置没生效" → `runtime-overview.md`「节点模型路由与准入绑定」+ `operations-and-maintenance.md`「节点模型负载不均 / 一直打同一个模型」
- 组车道打分看着很闲但链首一直被 429、或某次模型调用的重试轮数不等于任何组的 `maxRetryRounds` → `runtime-overview.md`「节点模型路由与准入绑定」辅助
- 模型链在预算没跑满时就报"耗尽"、审计里 `retryable: true` 与"链耗尽"同时出现、或错误文案里写着与实绑供应商不匹配的名字 → `runtime-overview.md`「Chat provider 超时与重试边界」（两级判据与"链位进过即耗尽"）车道那条（spawn 送审评审与重复预检刻意不过准入，属维持现状的已知边界）
- 按协议车道判断缓存命中（"这条车道不发 `prompt_cache_key` 所以没命中"）→ `context-and-cache-troubleshooting.md`「Family 与 key 合同」
- 上下文脑图标只按新输入变化、读数长期低于上一请求的真实输入规模 → `context-and-cache-troubleshooting.md`「同 turn 的 append-only 规则被破坏」+ `web-and-admin.md`「Composer Context Usage Meter」
- 长按脑图标不发起压缩、区分线停在「压缩已暂停」、压缩中区分线凭空消失刷新后才出现、渠道会话脑图标没有读数 → `web-and-admin.md`「Manual Context Compression」+ `context-and-cache-troubleshooting.md`「Shrink 原因与压缩边界」
- 压缩报成功、`post_tokens` 也降了，但下一回合请求体又回到原大小；或压缩途中发的消息没被回答 → `runtime-overview.md`「压缩窗口：入站闸门与基线写入仲裁」+ `context-and-cache-troubleshooting.md`「Shrink 原因与压缩边界」
- 压缩后 `[G3KU_STAGE_COMPACT_V1]` 块数量没下降、模型报告"压缩了但历史阶段还在"、或摘要里查不到某条证据引用 → `context-and-cache-troubleshooting.md`「Shrink 原因与压缩边界」（阶段收口判读）+ `runtime-overview.md`「Frontdoor Context Compression」；节点 artifact 带 `stage_archive` 而账本没有 `context_visible` 不属该症状（节点不应用收口标记，见同节）
- 阶段边界那一跳 `cache_hit` 塌下去、非缓存 `input` 冲高，但下一跳就回血，且此后正文天花板明显更低 → 这是裁撤生效的正常账单（断点位置不动、动的是断点后面的载荷），读数口径见 `context-and-cache-troubleshooting.md`「Shrink 原因与压缩边界」
- `cache_hit` 每阶段推进都塌回同一小值（头部块末尾的 token 位）而历史没被裁 → 活状态块被搬到了请求头部，位置与角色合同见 `context-and-cache-troubleshooting.md`「线体角色合同」
- 脑图标读数在阶段边界那一跳冲高数倍、下一跳回落，而 `comparable_to_previous_request` 仍为 `true` → `context-and-cache-troubleshooting.md`「同 turn 的 append-only 规则被破坏」（锚点与阶段投影同源）
- 节点反复收到"缺必填参数"式拒绝、错误首行逐字相同而 arguments 后缀不同，眼看要被无效提交上限判死 → `context-and-cache-troubleshooting.md`「Shrink 原因与压缩边界」（provider 参数块未转义 + 宽容解析吞参数，按协议故障单独计数）
- 节点连续多轮重交同一份被拒载荷、越到后面收到的参数契约越短（只剩 `summary` 残段、`ref` 为空） → `tool-and-skill-system.md`「Duplicate Tool Call Guard」（控制工具 error 回执不参与消息级折叠）
- 模型报告"上一轮我想到哪了"却读不到任何思路、或开了思考回放的 binding 一上线就整条链报 400 → `runtime-overview.md`「思考内容（reasoning）的上下文回放」（写入闸门按整条链取交集，咽喉点只透传）
- 一个回合跑了一两百跳、请求字符与 `effective_input_tokens` 全程只涨不降 → 先确认有没有阶段被模型点名裁撤过：**没点名的阶段一直逐轮重发是设计**（只有点名或压缩收口会让它降）。确有裁撤却仍不降，按 `runtime-overview.md`「stage_compaction」（两条车道都在过期点换基线；节点另看投影与发送体是否分叉）查
- 模型或节点声称"已把某阶段移出"却找不到 `archive_ref`、或点名裁撤后那批工具帧仍逐轮重发、提交时写的阶段总结在账本里是空的 → `runtime-overview.md`「stage_compaction」（跨结清的收尾对象判据与 `stage_closure` 回执，前门与节点同一条）+ `context-and-cache-troubleshooting.md`「Shrink 原因与压缩边界」（粘着的 shrink 原因不能当"裁过"的证据）
- 切回仍在处理的会话看不到期间产生的阶段/工具调用（刷新网页才出现）→ `web-and-admin.md`「CEO Feed View State And Scroll Preservation Contract」
- 用户气泡的「编辑」或模型回复元信息行里的「Fork」要刷新网页才出现、会话被暂停/审批/压缩挡住输入时找不到 Fork 入口、点了按钮提示「当前不可编辑」、或点击消息后用量/复制仍不显形 → `web-and-admin.md`「Message Edit-Resend And Session Fork Contract」+「Per-Turn Token Usage Contract」
- 回合进行中发的补充没被回答、待发送条目上的「插话 / 编辑 / 删除」行为疑问、撤下后条目又被画回来，或条目挂着不动、点删除只回 `follow_up_not_queued` → `web-and-admin.md`「Queued Follow-Ups」
- 回合卡在半截、下方一直转圈，刷新网页才同步到最终回复（本地与渠道会话同症状，转录里其实已有完整回复）→ `web-and-admin.md`「CEO Websocket Lane Failure Contract」
- 节点详情页的「加载 skill / 加载 tool」chip 风险色全部同一档、或展开看不到正文 → `web-and-admin.md`「Context Loader Notices」
- 工具面板的「参数」只有 48 字调用提示、超长入参点开也不回补 → `web-and-admin.md`「CEO Stage Trace Round Rendering Contract」（`arguments_truncated` 标记与 `tool-arguments` 按需回取车道）
- 翻看工具输出时新输出把输出框滚动条顶回顶部 → `web-and-admin.md`「CEO Feed View State And Scroll Preservation Contract」
- 「回到最新」按钮该不该呼吸（回合早已收尾还在闪、或正在进行却不闪）、呼吸光晕盖到周围消息上 → `web-and-admin.md`「CEO Feed View State And Scroll Preservation Contract」
- 日志板块反复出现「接口返回 500：/api/content/read」、节点详情输出框报「加载完整输出失败」或显示「完整输出已被清理」 → `web-and-admin.md`「Node Output Content Read Contract」
- 主题切换不生效或刷新后丢失、亮色主题出现大面积灰底、亮色主题下解锁/初始化窗口仍是暗卡片、侧栏折叠后审计角标消失、页面卡片列数在某宽度错乱 → `web-and-admin.md`「Frontend Theme And Layout Contract」
- 新设备一行指令装完发现 skill/tools 比开发机少、重跑安装指令为什么没更新代码、`negi status` 的 `Release:` 行为什么不出现、仓库改名之后已装设备为什么照常更新、`install -Upgrade` 为什么动的是另一个目录 → `operations-and-maintenance.md`「新设备首次安装与升级」
- 设置按钮红点不亮或误亮、面板显示"已是最新"但设备其实离线、「重启并更新」点了服务没回来 → `web-and-admin.md`「Update Notification And Restart-And-Upgrade Contract」+ `operations-and-maintenance.md`「自动检查新版本与「重启并更新」」
- 点输入框麦克风提示未就绪／未启用、新设备首点迟迟不出录音、录音结果仍是繁体、或局域网 http 打开时浏览器拒绝麦克风 → `speech-to-text.md`「分发与就绪」「已知边界」
- QQ 发来的语音变成一行本地路径注记、或只有「用户语音，机器识别失败：…」、或语音条迟迟等不到回复 → `external-agent-api.md`「内置官方 QQ 适配器」+ `speech-to-text.md`「常见排障入口」
- 语音气泡播放不出来、语音条右端的 `T` 点开是空的、或用户气泡里仍然显示 `用户语音，机器识别结果：` 这行字 → `speech-to-text.md`「语音气泡与音频字段的存放」+ `web-and-admin.md`「Attachment Bubble Rendering Contract」

## Maintenance Rules

These rules prevent the docs from re-accumulating redundancy. Every edit to this directory must follow them:

1. Update in place, never append. Rewrite the section that owns the topic; never add trailing addendum sections ("X Update", "X Notes").
2. One contract, one home. State each contract only in its owning doc below; everywhere else use a pointer: `See <doc> "<topic>"` / `详见 <doc>「<topic>」`.
3. Pointers name topics, never section numbers.
4. Present tense only. No "now / no longer / previously / 现在 / 不再 / 曾经" — that is changelog language.
5. Superseded text is deleted outright, never left as "obsolete notes".
6. Structure is the metric; bytes are only an observation. Two invariants: **(a) one doc = one subsystem** — when a doc is being asked to hold a second subsystem, split it into a new doc and register it here (Reading Order, Topic Guide, Topic Ownership). **(b) one leaf section = one contract, at most 25 KB** — a leaf section (a heading whose body contains no deeper heading) above 25 KB is subdivided into `###` sections or split out; a single bullet that carries a whole contract body is a misplaced contract: give it its own heading, or move it to the owning doc and leave a pointer. This README's Debugging Entry Points is a pointer index (rule 5), not a contract leaf. Measure with `python scripts/check_architecture_docs.py` — it walks headings and reports oversized leaves, dangling pointers, and pointers that name a doc which no longer exists; raw totals come from `wc -c docs/architecture/*.md` (bytes are stable for mixed CJK/English prose; word counts are not). Within the invariants: take no size action — never trim wording or drop facts just to hit a number; per-contract clarity beats bytes, and contract facts are never deleted to satisfy a size. When a doc outgrows itself, work the ladder in order: (a) delete dead/duplicated/superseded content, (b) move misplaced content to its owning doc, (c) split the genuinely grown subsystem into a new doc, (d) subdivide an oversized leaf into one contract per section. Observed sizes when this rule was rewritten (informational, not caps): `web-and-admin` 238 KB / `runtime-overview` 158 KB / `context-and-cache-troubleshooting` 88 KB / `operations-and-maintenance` 71 KB / `main-task-runtime` 66 KB / `tool-and-skill-system` 60 KB / `config-and-models` 47 KB / `tool-hydration-and-callable-chain` 46 KB / `heartbeat-system` 39 KB / `external-agent-api` 36 KB / `speech-to-text` 16 KB / `agent-gateway` 10 KB / `README` 40 KB. The previous per-doc byte table (`runtime-overview` 68 KB, `operations-and-maintenance` 24 KB, …) was deleted here: it had never constrained anything — `runtime-overview` entered this directory at 168 KB with a 68 KB reference, and five of eleven docs ended up over band while every one of their sections was live contract.

## Topic Ownership

| Topic | Owning doc |
|---|---|
| Runtime layering, message execution chain, session/task relationship, task temp directory resolution, provider timeout boundary, worker performance history (`perf_samples` sampling, retention, and the shared perf read model), silent reply via the `silent` tool (turn-terminal semantics, transcript trace row, compaction exemption, turn-end stage closure with an empty summary slot) | `runtime-overview.md` |
| Append-notice distribution contract: sub-tree control transaction and drain ledger, epoch state machine and barriers, single-flight epoch driver with wave-retry/degraded skip, mailbox delivery and uplink propagation, epoch-completed vs deferred consumption, acceptance node lifecycle and handshake re-dispatch, spawn review, task tree depth ceiling, force-delete precedence, node-level pause/resume/cancel and circuit breakers, graceful shutdown pause and startup auto-resume | `main-task-runtime.md` |
| Frontdoor context compression contract (`token_compression` / `stage_compaction`, 阶段收口 `context_visible` 与证据索引回填), 思考内容的上下文回放（reasoning 写入闸门与随压缩退出）, provider/model binding flag `reasoningContextEnabled` 的运行时语义 | `runtime-overview.md` |
| Memory queue state/file semantics (`runtime-overview`); queue/reset operator workflows (`operations-and-maintenance`) | both, split as shown |
| Heartbeat continuation contract, cron at-most-once delivery, reminder sidecar decision semantics, timeout stop, task terminal repair (including terminal-outbox delivery durability and the `abandoned` state), node-error, distribution-error, and task-stall detection/delivery | `heartbeat-system.md` |
| Tool/skill four concepts, fixed builtin tool contracts, candidate tools and candidate skills, frontdoor head-pinned static declarations (`candidate_skills` / `exec_runtime_policy` / `session_temp_dir`) and their three reprint boundaries, Tool Admin RBAC semantics, provider-facing `tools[]` surface contract (frontdoor live RBAC superset vs node pinned bundle and its single reprint point), duplicate-call guard, resource-directory generation checks and semantic catalog freshness, always-callable resident internal control tools (`silent`, and why fixed-builtin membership does not inject a tool) | `tool-and-skill-system.md` |
| Tool hydration ledger and promotion, re-read and fingerprints, parameter-error guidance lane, externalized tool result envelope, universal tool timeout contract, tool rerun-safe declaration for the recovery lane, stage gating and callable tightening, context→callable chain | `tool-hydration-and-callable-chain.md` |
| Actual-request forensics, 线体角色合同（一条请求里 system 至多一份且只能在首位）, append-only rule, cache-miss triage, token preflight diagnostics | `context-and-cache-troubleshooting.md` |
| Websocket/UI contracts, composer/media rendering, image upload gating, frontend theme and layout contract, model config admin draft contract, log audit event sink and audit page contract, node detail redraw expansion-state contract, node output content-read API contract, per-call model-call ledger (its writer lanes and `call_kind`) and the Token统计 window, container deployment, frontend vendor asset state (tracked manifest vs data-root probe overlay) | `web-and-admin.md` |
| Config schema, hot refresh, model bindings, secret location, deployment unlock, config bundle export/import, role route entries and load-balance group config semantics | `config-and-models.md` |
| Node model route resolution and admission-time binding: ordered route chain vs in-chain load-balance group, quota-bucket observation (rolling RPM + decayed 429 penalty), per-node sticky binding and its rebind triggers, group member budget and intra-group pacing, worker-only process scope and the rollback switch | `runtime-overview.md` |
| External Agent API contract: `externalApi` config and token overlay, ext session registry/keys, turn terminal invariant, SSE event mapping, ext outbound routing, built-in official QQ adapter (`qqBot` config, in-process botpy bridge) | `external-agent-api.md` |
| Agent gateway contract: OpenAI-compatible endpoint (`/api/v1/chat/completions`, session mapping, wait/200-honest-text policy, streaming diff) and MCP stdio gateway (`negi mcp serve`, tool surface, stdout purity) | `agent-gateway.md` |
| Local speech-to-text contract: whisper.cpp subprocess engine and slot serialization, audio normalization and silence/language gating, traditional→simplified post-pass, binary/model provisioning (`stt` defaults' measured basis), voice-clip storage and the model-visibility exclusion, known latency and accuracy envelope | `speech-to-text.md` |
| Startup/deploy/troubleshooting order, install root vs data root storage layout (which paths hang on which root, where to read the effective data root) and data-root resolution order, memory CLI, Docker compose | `operations-and-maintenance.md` |
