# G3KU 运维与维护建议

本文档从“接手项目后怎么跑、怎么验证、怎么排障”的角度总结维护要点。

如果问题与 prompt cache 命中、跨 turn 上下文连续性、append-only request growth、或 actual request artifact 对账有关，请同时阅读：

- `runtime-overview.md`
- `context-and-cache-troubleshooting.md`

## 1. 基本启动方式

### 新设备首次安装与升级

用户侧一行指令（Windows `install.ps1`，Linux / macOS `install.sh`，仓库根），随后进入一键启动脚本同一条链路。

维护上要记住：

- 安装器只负责补齐它上游的两件事：**取解释器**（缺 uv 就装 uv，再由 uv 按 `.python-version` 提供 Python）与**取代码**（有 git 走 `git clone --branch <ref>`，没有 git 退化成 GitHub 源码包下载）。依赖安装与环境复用全部交给既有的 `g3ku_bootstrap.py`，安装器不实现第二套
- 最后一步用 **venv 里的 python** 跑 `g3ku_bootstrap.py web`。这既让 `g3ku_bootstrap.py` 的宿主 Python 版本检查落在刚装好的解释器上，也保持了分发形态的前提：本项目安装的是**完整 checkout 就地运行**（bootstrap 会 `chdir` 到仓库根，`main/` 从工作目录导入），不是一个自包含的 Python 包 —— wheel/sdist 目前不含 `main/`，所以安装器与 `uv sync --frozen` / `pip install -e .` 才是唯一可用路径
- 安装得到的资源集等于 git 跟踪集：仓库自带 skills/tools 在内，操作员本地通过市场安装的 skills、`externaltools/`、`.g3ku/` 不在内。"新设备上少了一批 skill/tools" 属预期，不是安装失败
- 一行指令指向不可变 ref（发布标签）。要覆盖用 `-Ref` / `--ref`，换目录用 `-Dir` / `--dir`，只建环境不启动用 `-NoStart` / `--no-start`
- 镜像源不设安装器参数：uv 直接读 `UV_DEFAULT_INDEX` 与 `UV_PYTHON_INSTALL_MIRROR` 环境变量
- 安装完成的终点是项目口令设置页，不是可用系统（解锁合同见 `config-and-models.md`「Deployment Unlock Contract」）
- 不带 `-Upgrade` 时对已存在的目录是**幂等不动代码**（只补环境与启动）。升级是显式动作：git 检出走 `fetch --depth 1` + `checkout --detach FETCH_HEAD`，无 git 的源码包安装走"下归档 + 逐顶层覆盖"，两条路都只换代码，`.venv/` 与 `.g3ku/` 保留
- 升级前置校验：`git status --porcelain` 非空即拒绝执行，不静默覆盖用户改动
- 两种取码方式**不可混用**：把源码包盖在 git 检出上会让整棵树在 autocrlf 下变成永久"脏"，从而被下一次升级的脏检查挡住。因此"有 `.git` 但 git 不可用"时报错，而不是退化成覆盖
- 版本识别通道是 `git ls-remote --tags origin`，只接受 `refs/tags/vX.Y.Z` 形状（`backup/*` 这类路径标签与 peeled `^{}` 行都按形状过滤掉），与 `g3ku/__init__.py` 的 `__version__` 比对，结果只落在 `g3ku status` 的 `Release:` 行。约束：只读不外发、超时 2 秒、失败即整行不出现（离线设备不得显示"已是最新"）
- 发版动作 = 打标签 + 同步 `pyproject.toml` 与 `g3ku/__init__.py` 两处版本号 + 跑 `uv lock`（`uv.lock` 里钉着 `g3ku-ai` 自身版本，漏这一步会让所有 `uv sync --frozen` 的安装与升级直接失败）+ 更新安装脚本与 README 里钉住的 ref 默认值

### 首选一键启动脚本

- Windows PowerShell: `.\start-g3ku.ps1`
- Linux / macOS: `./start-g3ku.sh`

适合：

- 普通用户快速启动项目
- 本地单机直接拉起 Web 与托管 worker
- 让脚本自动处理 `.venv`、依赖安装、基础配置兜底与已有托管进程重启

维护上要记住：

- 这两个脚本是普通用户首选入口
- 它们会在启动前默认清理当前仓库下已有的 g3ku web / worker 进程，但**先请求优雅退出**：脚本向 `POST /api/bootstrap/exit`（`pause_running_work=true`）发起请求并等待运行时把全部会话与任务持久化暂停、自行退出；只有在接口不可达、返回失败或等待超时（约 40 秒）时才回退到强制杀进程。因此“重跑启动脚本”对运行中的工作是一次优雅暂停而不是异常中断，下次启动会自动恢复（生命周期合同见 `runtime-overview.md`「Graceful Shutdown Pause and Startup Auto-Resume」）
- 强制回退路径（强杀）对应的是异常中断：下次启动任务走恢复清洗，任务卡片会以 toast 提示「本任务遇到异常停止」；toast 可点击关闭（UI 合同见 `web-and-admin.md`「Task Recovery Notice UI Contract」）
- 它们最终仍然是调用 `g3ku` bootstrap，再进入 `g3ku web`
- 当脚本使用 reload 模式时，Web 侧自动托管 worker 会关闭；这时要单独运行 `g3ku worker`
- `g3ku.cmd` / `g3ku.ps1` / `g3ku.sh` 是 CLI 透传包装；无参调用默认启动 `web`。任何入口的 web 启动都会在终端报告结果：成功横幅带 URL，失败横幅带子进程退出码和 `.g3ku/logs/console.log` 指引
- web 启动在拿单实例锁（`.g3ku/start.lock`）之前会先自愈：杀掉本工作区残留的 g3ku web 服务进程（`-m g3ku web` 或 `-c ...run_web_server_entrypoint...` 形态，按 venv python 路径或进程 cwd 归属本工作区）。因此“端口/锁被占”不是永久失败：再次启动会替换残留实例；若报错里 `pid=unknown`（持有者在加锁与写元数据之间被杀），用 `netstat` 查 web 端口定位占用者

### CLI

- `g3ku agent -m "Hello"`

适合验证：

- 配置是否正确
- CEO 模型是否可用
- 同步会话路径是否正常

### Web

- `g3ku web`

适合验证：

- Web UI
- main task runtime
- heartbeat

### Worker

- `g3ku worker`

适合验证：

- 后台 task worker 路径
- Web/runtime 分离执行场景

## 2. 新接手后的第一轮检查

建议按下面顺序做环境确认：

1. 检查 `.g3ku/config.json`
2. 运行 `g3ku status`
3. 运行一次 `g3ku agent -m "test"`
4. 先用 `.\start-g3ku.ps1`（Windows）或 `./start-g3ku.sh`（Linux / macOS）启动项目
5. 确认 Web API、任务面板、模型配置页能正常返回

如果你要拆开验证启动链路，或排查“是脚本包装层问题还是 Web/runtime 本身问题”，再回退到手动运行 `g3ku web` / `g3ku worker`。

当前 `g3ku status` 的记忆区块应按 queued Markdown runtime 理解：

- 它会显示 `Memory Notebook`、`Memory Notes Dir`、`Memory Queue`、`Memory Ops Log`
- `Memory Mode`、`Memory Store(SQLite)`、`Memory Store(Qdrant)`、`pending_facts.jsonl`、`audit.jsonl`、`Memory Checkpointer` 不作为当前长期记忆健康指标

## 3. 关键状态文件与目录

日常排障时，先熟悉这些目录：

- `.g3ku/config.json`
  项目配置

- `.g3ku/llm-config/`
  模型配置仓库（provider/binding record）

- `.g3ku/main-runtime/`
  任务运行时 SQLite、artifacts、event history

  其中 SQLite（`runtime.sqlite3` 或配置指定的 store 路径）里除任务/节点/帧等表外还有 `shutdown_pause_registry` 台账表：记录上次优雅关闭时被暂停的会话与任务，重启自动恢复后逐行删除。排查“重启后任务没有自动恢复”先查这张表（语义见 `runtime-overview.md`「Graceful Shutdown Pause and Startup Auto-Resume」）。

  通过 `g3ku web` 启动并启用 auto worker 时，这里还应重点关注两份日志：

  - `.g3ku/main-runtime/manual-web-run.log`
    Web 主进程日志
  - `.g3ku/main-runtime/managed-worker.log`
    自动拉起的后台 task worker 日志

- `.g3ku/web-ceo-continuity/`
  completed Web CEO session continuity sidecars。重启后继续 completed session、manual pause terminal stop、以及异常中断后的最新 authoritative frontdoor baseline 恢复都先看这里。

- `.g3ku/web-ceo-turn-boundaries/`
  每轮连续性边界快照（`<session>/<turn_id>.json.gz`，每会话保留最近 3 轮）。用户消息编辑重发/Fork 的唯一截断数据源；契约见 `web-and-admin.md`「Message Edit-Resend And Session Fork」。会话删除时随其它 sidecar 一并清理。

- `.g3ku/external-outbox/`
  外部渠道主动推送的持久账本（append-only jsonl，msg 记录 + ack tombstone）。排查「渠道端收不到主动推送」先看这里有无 pending 滞留；契约与重放语义详见 `external-agent-api.md`「持久 outbox」。

- `memory/`
  记忆目录；排障入口详见本文档「Memory Queue Workflow」章节。

- `sessions/`
  会话持久化数据

- `temp/tasks/`
  任务临时目录，每个任务一个 `task_<id>` 子目录；根目录解析与隔离规则见 `runtime-overview.md`「任务侧」。任务进入终态（success/failed）后该目录**默认原样保留**；仅当开启 `main_runtime.disk_guard.terminal_temp_dir_cleanup_enabled`（环境变量 `G3KU_TERMINAL_TEMP_DIR_CLEANUP_ENABLED=1`）时才随终态清理硬删（保留清单与行为契约见 `runtime-overview.md`「磁盘写保护与治理」）。孤儿目录（`runtime.sqlite3` 的 tasks 表中已无对应任务却残留的 `task_*` 目录，含从未走到终态的卡死任务遗留）用 `scripts/cleanup_orphan_task_temp_dirs.py` 清理：默认 dry-run 只报数；`--apply` 删除空孤儿；非空孤儿要么 `--apply --move-non-empty` 移入 `temp/tasks_orphan_backup/`（可逆），要么 `--apply --purge-non-empty` 直接删除（不可逆）。清理脚本保留在库任务目录，以数据库为准，与运行中的任务互不影响。由于终态后目录保留，`temp/tasks/` 会随任务累积增长，磁盘紧张时优先按该脚本处置而非手删。

- `temp/ceo/`
  CEO/frontdoor 会话级临时目录，每个会话一个 `<safe_session_key>` 子目录（如 `web_ceo-xxxx`）。作为 CEO 会话工具 runtime 的 `task_temp_dir`：`exec` 缺省 cwd 与临时文件规范落点，避免临时产物散落到工作区根目录。解析与惰性创建规则见 `runtime-overview.md`「任务侧」。该目录不保证持久保留：正式交付物禁止以此为最终落点（此约束同时写进 CEO 提示词契约）。

## 4. 测试结构

测试以 `tests/` 为主，很多测试按资源/运行时主题分布在：

- `tests/resources/`

从现有命名看，测试重点覆盖：

- main runtime
- CEO/frontdoor
- tool registry 与 hydration
- web runtime
- heartbeat prompt lane
- memory runtime

新增运行时测试时，`MainRuntimeService` 构造要显式传 `workspace_root=tmp_path`，把任务临时目录隔离进 pytest 临时目录；`tests/resources/conftest.py` 的 autouse fixture 对漏传的用例兜底替换 cwd 回退。判断测试是否泄漏了真实工作区的快速办法：跑完测试后 `.venv/Scripts/python.exe scripts/cleanup_orphan_task_temp_dirs.py` 只看空孤儿数量是否增长。

## 5. 推荐的排障顺序

### 会话无回复

先看：

- `g3ku/runtime/session_agent.py`
- `g3ku/runtime/bridge.py`
- Web 场景下再看 `g3ku/heartbeat/session_service.py`

静默相关的三种不同症状分岔查（合同见 `runtime-overview.md`「3.3 静默回复（`silent` 工具）」）：

- **会话回复了本不该再说的旧结果**（典型为"项目一启动就自动回一条几小时前的任务汇报"）：`main-runtime/runtime.sqlite3` 的 `task_terminal_outbox` 里有滞留行被启动或 60s 节拍重放。按 `created_at` 与 `delivered_at` 的差值、以及 `attempts` / `last_error` 判读；`delivery_state='abandoned'` 表示投递上限耗尽后留痕，不再被拾取。
- **该静默却把话发出去了**：转录里那一轮有没有 `silent` 的 tool_call 行。文本不再参与静默判定，所以模型写出的任何"看起来像哨兵"的句子都只会作为正文投递；唯一例外是整行就是旧哨兵 `[G3KU_SILENT]`，它会被清洗成空正文并落进"无正文即静默"那条道。
- **该回复却整轮没出声**：同一轮的 `silent_reply` 行是否被写进了转录，以及它的 `silent_reason` 是哪一种——工具静默带的是模型写的理由，"模型未给出可见正文" / "旧静默哨兵剥除后无正文" 则是收尾兜底，后者说明模型还在沿用已删除的文本出口（多半是压缩摘要里残留了旧契约措辞）。再查它是否被两条压缩车道裁掉（豁免规则见 `runtime-overview.md`「Frontdoor Context Compression」）。

### 任务没创建或没推进

先看：

- `main/service/runtime_service.py`
- `main/runtime/node_runner.py`

如果问题是“一次性 cron / 定时提醒为什么没有真正创建或没有后续触发”，还要先区分两类情况：

- job 已成功落进 `.g3ku/cron/jobs.json`，但后续没有被消费：再继续查 scheduler / Web 主进程 / session dispatch。按 state 与日志分诊：
  - `lastStatus` 是调度器自己写的分诊码：`running` 只应短暂存在（dispatch 进行中；进程活着时任何退出路径都会收尾，残留 `running` 说明进程在 claim 与 finalize 之间死掉，重启后会自愈为 `interrupted`）；`timeout` = 投递看门狗杀掉了挂起的 dispatch（同一条 ERROR 日志里带挂起任务的 await 链 dump，`dispatch watchdog timeout` 可 grep，这是定位“回合完成后 session.prompt 不返回”类挂起的第一手证据）；`interrupted` = 取消/停机收尾；`error` = handler 异常（`lastError` 带原文）。一次性 `at` job 的 `timeout`/`interrupted` 会按 at-most-once 抑制（禁用而非重发），契约详见 `heartbeat-system.md`「Cron Reminder Contract」
  - 到点没触发且无看门狗日志：grep `still in flight; skipping this tick`（同一 job 上一次 dispatch 仍未了结，后续 tick 主动跳过）与 `timer task died unexpectedly`（定时器任务意外死亡，调度器会延迟自愈重臂；这条出现说明 tick 路径本身炸了，看同段异常栈）
  - 回合慢但没挂死：`session prompt still awaiting`（bridge 慢回合看门狗，同样带 await 链）与 `turn finalize tail slow`（finalize 尾部分阶段耗时）用于区分“真的在干活”与“楔死”
- `cron add` 本身在创建阶段就失败，有两种已知拒绝：
  - 尤其是 `at` 单次提醒，如果真正执行 `add_job()` 时目标时间已经过去，服务会直接拒绝创建并提示 `任务定时已过期，当前时间为<service-local time>，请立即执行或视情况废弃而不要创建过期任务`；这时应优先排查前门/tool 调用延迟、重试、参数错误，而不是先怀疑 scheduler 没触发
  - 同一 session 在同一个 `at` 时间点已存在启用的一次性提醒时，重复注册会被拒绝并提示 `同一会话在 <time> 已存在一次性提醒 (id: …)`（按 `(session_key, at_ms)` 结构化匹配，不看 message 文案）。这是有意的防双触发约束而非 bug：如需改期，先 `remove` 旧 job 再重新创建，或改用其他时间；不要为了绕过它去禁用或删改冲突检查

Maintenance note for `task_append_notice` / task message distribution:

- If a task appears stuck in `barrier_requested`, `barrier_draining`, or `distributing`, do not blame the model first.
- 分发 epoch 处于 `failed`（任务树红色横幅、`runtime_meta.distribution.error_text` 非空）表示控制回合的修复重试（最多 5 次）耗尽、发送 preflight 失败，或波次异常超出重试预算（`error_text` 以 `distribution wave crashed` 开头，`payload.wave_crash_count` 记累计次数）；此时任务被置为 paused（任务大厅显示「任务暂停」）、消息未向子节点投放，并向源会话投递一次 `task_distribution_error` 心跳（详见 `heartbeat-system.md`「Task Distribution Error Delivery」）。先读该 epoch 的 `payload.debug_trace` 逐轮取证（每轮 attempt 都有 `control_turn_response` / `control_turn_validation_failed` / `control_turn_retry` 记录），`wave_crash_count` 形态的失败另需 grep worker 日志取该波的 traceback，再二选一介入：恢复任务（降级为根节点按 pending-notice 语义延迟消费）或重新追加通知（创建新 epoch 重新走完整分发）。如果 `error_text` 是 `inspection_decision_invalid_action`，优先确认 frontier 节点的 acceptance handshake 是否处于 `waiting_acceptance` / `waiting_block_verification`，并确认验收中通知决策回合收到并解析的是 `submit_notice_inspection_decision`；这是分发屏障内的决策校验失败，不是外部 bridge 回调失败。
- 冻结/复活/收尸类异常在 worker 日志有固定签名，先 grep 再看 DB：`node frozen by distribution hold`（hold 冻结落点，带 task/node/epoch）、`barrier drain self-heal: kicking stalled spawn-round parent`（drain 阶段踢起持有未物化 spawn 轮、已被停摆的父节点去完成子节点物化；同一 父节点+轮 有冷却期，记账为 epoch payload 的 `drain_kick_rounds`，踢了没进展会在冷却过后自动再踢）、`stale subtree hold ignored`（meta 与 epochs 表脱同步被防御放行）、`release verification`（释放后节点未复活，先 WARN 再 resume、仍卡死落 ERROR）、`distribution driver wave crashed`（波次异常，带 `crash_count`；预算耗尽后 epoch 显式 `failed`）、`distribution driver missing for active epoch, re-arming`（周期对账接管了缺席驱动器——同一任务反复出现说明每一波都在同一处崩，取该波 traceback）、`distribution release ledger unfinished, re-releasing`（完成序列在清 meta 之后、释放之中抛过，对账器补跑释放；反复出现要查该任务的节点行是否缺失或数据库写入是否失败）、`dispatch future was cancelled while node non-terminal` / `stranded exception`（搁浅 future 被释放路径重建）、`orphan node reaped` / `orphan node re-dispatched`（run_task 入口对孤儿子节点的决断，收尸同时写 `task_error_logs`）。子节点"显示进行中但无模型调用/无帧更新"时按此顺序核对：先把每条 `frozen` 的 node 与该 epoch 的释放集对齐（释放集按目标子树反算，合同见 `runtime-overview.md`「frontdoor 与任务运行时的关系」），落在释放集外即是释放漏跑，对这些节点直接下发 resume 即可在原 future 上重跑（meta 已清则不会二次冻结）。
- `barrier_draining` 期间「在飞 spawn 批次的子节点尚未物化」不是死锁征兆：该批次豁免 hold 直到物化完成，停摆的父节点由 drain 自愈踢起，两者都落上面两条日志。若 epoch 仍长期停在同一 `drain_pending_node_ids` 上，核对 `task_node_tool_results` 是否残留 `status='running'` 的 `spawn_child_nodes`，以及该父节点 runtime frame 是否仍保留该轮的 `pending_tool_calls` / `phase='waiting_children'`（重放意图）；帧里已无该轮时自愈不介入，需人工处置（恢复任务或重新追加通知）。
- barrier/epoch/spawn/acceptance 的契约字段与完整排查路径详见 `runtime-overview.md`「frontdoor 与任务运行时的关系」。

如果任务已经创建，但表现为“响应明显变慢”“长时间停在 `model.chat.await_response`”或“前端只看到 task-event 在刷”，优先同时对照：

- `.g3ku/main-runtime/manual-web-run.log`
- `.g3ku/main-runtime/managed-worker.log`

其中 worker 日志更关键，因为 provider 请求、SSE 诊断和模型超时通常发生在独立的 worker 进程里。

排查时优先搜索：

- `responses stream diagnostics`
- `openai_codex stream diagnostics`
- `Error calling Responses API`
- `model attempt timeout`

Provider retry troubleshooting note:

- 节点 `react_loop` 与 CEO/frontdoor `call_model` 的外层 provider-exhaustion 自动重试是有限次：在底层 model/key/fallback chain 已经 exhausted 之后，当前 round 最多再做 3 次外层重试。
- 因此如果你看到 `Error calling Responses API` 连续刷屏，但任务/会话迟迟不结束，不要先假设“它还在无限自动重试”。先确认这些日志是否真的属于同一个 task/session。
- 当前 task 如果已经落到 `is_paused=true` / `pause_requested=true`，那说明另一个控制动作已经介入了；这和 provider retry 本身是两条不同的因果链。排查时应同时看 `task_commands` 是否出现 `pause_task`，而不是只盯着 provider 日志。

并结合 provider 超时边界判断“慢”是不是异常；超时语义详见 `runtime-overview.md`「Provider 超时边界」。

日志里优先看这些字段：

- `first_chunk_received_ms`
- `first_text_delta_received_ms`
- `chunk_count`
- `last_chunk_kind`
- `stream_completed_ms` / `stream_failed_ms`

### spawn 轮次过早完成或子节点被意外 supersede

如果事件流里出现：父节点在验收节点仍为 `in_progress` 时提前进入 `before_model`；同一轮内出现第二次 `spawn_child_nodes` 且旧轮子节点被打上 `superseded by newer spawn round`；或帧里出现 `entry.status=success + acceptance.status=in_progress` 这种无效组合——按下面顺序排查。

先看帧诊断字段（`_resume_waiting_children_turn_if_needed` 在恢复前写入当前帧）：

- `spawn_recovery_mode`：`wait_existing_pipeline`（有存活子/验收节点，应等待现有管线）、`rematerialize_round`、`replay_completed_result`、`no_active_round`；
- `spawn_recovery_round_id` / `spawn_recovery_round_ids` / `spawn_recovery_active_entry_indexes` / `spawn_recovery_active_node_ids`：定位仍活跃的 round、entry 与绑定节点。

再在 worker 日志里搜 `[g3ku spawn diagnostic]` 开头的警告：

- `title=spawn_blocked_review_over_materialized_round`：已物化轮被重新评审且判为 blocked，但运行时拒绝让其终结仍存活的子/验收节点（只写 `review_decision=blocked`，不把管线状态写成 `success`）；
- `title=spawn_entry_terminal_with_live_node`：entry 状态已是 terminal/blocked，但绑定节点仍非终态，属无效状态组合信号。
- `title=spawn_pause_reached_settlement_lane`：某条车道把子节点的暂停当成可结清的异常送到结算面（正常形态是子节点派发 future 保持 pending、父管线原地停等）。命中说明该轮会被父节点读成"子节点失败"，而节点其实可被 resume；核对是哪个调用点绕开了派发 entry。
- `title=spawn_supersede_forced_live_node`：新轮清扫时该子树取消后仍未落终态（协程当时真在执行），仍按 `superseded` 强判，`detail` 给出被掐断的节点 id 与深度。读法：拿该节点在 `task_model_calls` 的最后一格时间戳与 `task_commands` 里的 `resume_node` 行对照，可判断这是一次人工/agent 复活与重派的竞态，还是旧轮长期挂死。

修复语义详见 `runtime-overview.md`「Node-Level Pause and Recovery」。这四条 warning 只作诊断，不会自行终结节点；真正的修复在恢复逻辑——等待现有绑定节点到终态，而不是重新评审或重放合成结果。

### 残留节点自愈

任务终态仅由根节点 + 最终验收推导，终态流转本身不强制收尾残留节点（见 `runtime-overview.md`「Node-Level Pause and Recovery」）。真正“不会再被驱动”的残留节点由 worker 启动自愈清理：启动引导对每个终态任务调用 `log_service.sweep_residual_nodes`，把仍 `in_progress` 的节点置为 `failed`，`failure_reason` 带 `task_terminal_cleanup` 前缀并附产物定位（`execution_trace_ref` / `result_payload_ref`），只改状态、不删转录/产物，并发布 node patch 事件。因此终端里“重启后残留节点自动落终态”是预期行为，不是数据丢失；进行中任务不参与清扫。

### 任务执行了全局进程清理 / Web 与 worker 同时退出

如果任务执行了按进程名或宽泛 PID 范围清理的命令（例如 `Get-Process python | Stop-Process`、`taskkill /IM python.exe`、`pkill`），Web 主进程和托管 worker 可能被一起终止；这种退出没有正常 shutdown 日志，任务通常停在 `waiting_tool_results` 或被标记为异常停止。先看任务 artifact 中最后一个 `exec` 调用的 `arguments_text`，再对照 `.g3ku/logs/console.log`、`.g3ku/main-runtime/manual-web-run.log` 与 `.g3ku/main-runtime/managed-worker.log` 的最后时间戳；若三者在同一时刻截断且没有 graceful-exit 记录，优先判定为外部/任务侧强杀，不要先归因于模型或数据库。

当前 `exec` 在执行模式、白名单与审批判定之前增加宿主进程保护：常见 `Stop-Process` / `Spps` / `taskkill` / `pkill` / `killall` / `kill` / WMI-CIM terminate-delete / Python `os.kill` 等命令直接返回 `host-process termination` 错误；`full_access` 也不能绕过，白名单和操作者审批也不能放行。只读进程检查仍允许。保护是命令形态拦截，不是完整 OS 隔离；如果必须运行不可信的任意 native code 或外部二进制，仍应把任务放入独立 worker/container/低权限账户，并避免按进程名清理。

恢复后重点确认：托管 worker 看门狗是否重新拉起 worker、`worker_leases` 是否清掉陈旧租约、任务是否出现 `metadata.recovery_notice`。如果是误杀宿主后的遗留任务，不要用全局 `python` 清理；只使用任务级 pause/cancel，或由 `exec` 超时/取消路径清理该次调用自己启动的子进程树。

### 重启后任务未自动恢复 / 出现“异常停止”toast

先分清这次退出是优雅暂停还是异常中断：

- 优雅路径（重启脚本先调 `/api/bootstrap/exit`、或 Ctrl+C 让信号处理器收尾）：所有运行中的任务与会话被暂停并写 `shutdown_pause_registry` 台账，启动时自动恢复、不出现“异常停止”提示。退出前还有一次 ≤10 秒的排水等待（轮询 `pause_task` 命令直到 worker 真正停完 actor），停完才关闭托管 worker。若此时任务仍停在 paused：查台账行与任务 id 是否一致、`task_commands` 是否有未消费的 `pause_task` 残余、worker 是否拿到 lease 完成 startup（详见 `runtime-overview.md`「Graceful Shutdown Pause and Startup Auto-Resume」）。
- 异常路径（进程被强杀、worker 单进程被单独杀死）：任务恢复清洗照常执行，`metadata.recovery_notice` 写「本任务遇到异常停止…」，UI 以可关闭 toast 呈现（`web-and-admin.md`「Task Recovery Notice UI Contract」）。这是预期行为，点击关闭即可。托管 worker 被单杀后 Web 会由看门狗自动重启、无需人工拉起（见本节「托管 worker 看门狗」），但该 worker 当时正在跑的任务仍按异常中断走恢复清洗。
- 会话侧的自动恢复走 heartbeat `shutdown_resume` 内部轮（`heartbeat-system.md`「Shutdown Resume Wake」）：会话尾气泡会再现一条由系统恢复产生的回复；若没有出现，查启动日志里 `resume_shutdown_paused_sessions` / `auto-resumed` 与 heartbeat 事件投递日志。

### 托管 worker 看门狗 / 任务大厅持续显示「worker stale」

本地默认启动（非 `--no-worker` / 容器）下，Web 解锁后托管一个 task worker 子进程（`python -m g3ku worker`）：worker 每 1–2s 写 `worker_status` 心跳并续 `task_worker` 租约（`worker_leases`，TTL 20s）。`/api/tasks/worker-status` 在心跳 `updated_at` 距今超过 15s（有活动任务 60s）时报告 `stale`，前端据此冻结创建/恢复控件并显示条幅。

`g3ku/web/worker_control.py::run_managed_task_worker_watchdog`（周期 5s）在托管进程确实退出后自动重启它，因此单点崩溃不再导致任务大厅永久 stale。三条安全闸门：

- 只在 `managed_worker_pid()` 为空（托管进程已退出）时触发，不看心跳——「卡住但还活着」的 worker 不误判为死亡；
- 重启前探测租约 `holder_pid`：存活则跳过（避免与外部单独启动的 worker 双跑），确认已死才清理陈旧租约以跳过 TTL 等待，未知则不动租约、让新 worker 自行按租约接管；
- 持续失败做指数退避（5s→…→60s 封顶），避免崩溃循环。

排查「一直 stale」按序：`.g3ku/main-runtime/managed-worker.log` 末尾 `worker_lease_unavailable:<holder>:<expires_at>` 是旧租约未到期就被拉起（非根因，等 TTL 即可）；`managed task worker watchdog:` 打头的是看门狗决策日志；仍需确认没有外部 `g3ku worker` 残留占着租约（查 `worker_leases` 的 `holder_pid` 是否还活着）；worker 反复崩溃时继续按「任务没创建或没推进」查崩溃根因，而非只看门狗兜底。

worker 静默不等于 worker 死亡：空闲 worker 除心跳线程每 1–2s 写库续租外，其余职责全部静默，唯一日志节律是每 10 分钟一行的 `worker heartbeat alive: worker_id=… pid=… active_tasks=… beats=… sqlite_write_failures=…` 存活行。判读：日志超过约 15–20 分钟不滚动而 `worker_leases.heartbeat_at` 仍新鲜（或 `holder_pid` 在 tasklist 中存活）→ 日志输出层异常，继续按本节排查；日志与心跳同时停 → 进程已死，看门狗自动兜底重启，崩溃根因按「任务没创建或没推进」查。

### 缓存命中下降或上下文疑似丢失

先看：

- `docs/architecture/context-and-cache-troubleshooting.md`
- `.g3ku/web-ceo-requests/`
- `.g3ku/web-ceo-continuity/`
- `.g3ku/web-ceo-turn-boundaries/`（编辑重发/Fork 截断相关）
- `sessions/`
- 相关的 paused / inflight snapshot

### 工具调用异常

先看：

- `g3ku/agent/tools/registry.py`
- `g3ku/runtime/context/`
- `main/service/runtime_service.py`
- 如果症状是执行节点/验收节点说“当前没有 candidate skills”或把本应当作 skill 的东西误判成“缺失的 callable 联网工具”，优先同时检查节点 runtime frame 与 `runtime-frame-messages:{node_id}` artifact 里的两组字段：
  - `contract_visible_skill_ids`：回答 `runtime_service._node_context_selection_inputs()` 当轮实际看到了哪些 contract-visible skills
  - `candidate_skill_ids`：回答 selector 最终留下了哪些 canonical skill candidates
- 如果还需要继续往下拆，再看 `skill_visibility_diagnostics`：
  - `registry_skill_ids`：回答 live `resource_registry` 当轮到底列出了哪些 skill
  - `entries[*].allowed_for_actor_role`：回答是不是在 `allowed_roles` 这一层就被挡住了
  - `entries[*].policy_effect`：回答 role policy 当轮给到的效果是不是 `allow`
  - `entries[*].included_in_contract_visible`：回答该 skill 最终有没有进入 `contract_visible_skill_ids`
- 排障顺序应先分层：
  - `contract_visible_skill_ids=[]`：优先怀疑 RBAC / governance / resource visibility 输入层
  - `contract_visible_skill_ids` 非空但 `candidate_skill_ids=[]`：优先怀疑 selector 或 contract/frame 重建链路
  - 两者都非空但模型文本里的前门 contract 已经把 `candidate_skills` 渲染成 `none`（无论文案是旧的 `candidate_skills: none`，还是新的 loadable 提示版本）：优先怀疑动态 contract 重建或 stale frame/消息恢复问题

### 模型配置不生效

先看：

- `g3ku/config/loader.py`
- `g3ku/llm_config/facade.py`

如果问题表现为“前端模型配置页已经显示了新的 API key / 新的 key 数量，但 CEO 或 worker 还像在用旧 key”：

- 先确认对应 runtime refresh 是否真的执行到了目标进程
- Web 托管 worker 路径下，优先看 `task_commands` 里的 `refresh_runtime_config` 是否完成，以及 `.g3ku/main-runtime/managed-worker.log`
- 当前系统约定是：显式 runtime refresh 会同时重载已解锁进程里的 bootstrap security overlay 缓存；如果 refresh 没跑到，老进程可能继续拿旧 secret 快照
- 如果 refresh 已完成但行为仍不对，再考虑 worker 进程是否需要重启，或是否存在多个旧 worker / 旧 Web 进程残留

### 外部渠道桥接异常

内置渠道子系统已移除，IM 渠道由独立桥接进程经 External Agent API 接入。先看：

- `docs/architecture/external-agent-api.md`「常见排障入口」
- 桥接应用自身的日志与配置（如 `bridges/qq-onebot/README.md`）

### 磁盘满（Errno 28 / SQLITE_FULL）

症状族：worker 日志或 `.g3ku/errors/` 出现 `OSError [Errno 28] No space left on device` / `OperationalError: database or disk is full`；`.g3ku/errors/` 里的错误日志是 0 字节空文件；多个节点连锁 error-pause；渠道告警发不出去。

排障顺序：

1. 先看 worker 心跳 debug 块里的写失败计数（`worker_leases` 行 `payload_json.status_payload.debug` 下的 `sqlite_write_failures` / `event_write_failures`，`worker_status` 行 payload 与存活日志行同步携带）与 `managed-worker.log` 里的 SQLITE_FULL 行、限流告警 `task_events write failure (rate-limited): total=…`（300s 至多一条）——磁盘满期间错误日志本身可能写不出来，`.g3ku/errors/` 不是唯一证据源（计数契约见 `runtime-overview.md`「磁盘写保护与治理」）。
2. 定位空间大户：`.g3ku/main-runtime/artifacts/`（历史任务产物）、`runtime.sqlite3`、`memory/`、`temp/tasks/`、`.tmp/`。目录统计命令要给足超时——磁盘近满时全量遍历极慢，短超时得到的数字不完整。
3. 运行时自动行为无需干预：可降级写按应急预算自动跳过、error pause 记录失败不连锁、终态任务的中间产物自动清理；磁盘剩余跌破紧急线（max(300MB, 1%)）时运行中任务被自动暂停（新工具调用排队等待、不报错），任务大厅出现红色横幅与性能条「CPU/内存/磁盘」项的紧急着色（磁盘段显示 `0%(剩余10.1G) · 紧急`），空间恢复后紧急态自动解除、**被暂停的任务需手动 resume**。
4. 需要人工的只有两类：回收历史存量（无写入者的死库文件），以及调整 `main_runtime.disk_guard` 配置（字段契约见 `config-and-models.md`「main_runtime」）。磁盘治理没有任何自动任务删除：任务终态即清确定不再使用的数据（中间产物 + event-history 单份快照）；任务本身只随用户页面删除或模型删除工具彻底清除（删除前报告类产出自动导出到 `.g3ku/main-runtime/deliverables/<task>/` 永久保留）。磁盘紧张时在任务大厅用「按大小」排序（或模型工具 `task_stats` 的 `sort=size`）定位大任务手动删除。每小时维护循环按 `detail_retention_days`（默认 0=停用，配置 >0 恢复）裁剪终态任务的五张大行表、经删除台账 sweep 补偿中断的删除并清扫孤儿 event-history 目录（契约见 `runtime-overview.md`「磁盘写保护与治理」）。event-history 每任务只存一份 live.patch 最新快照（latest.json.gz），终态清理时删除。从带 zip 归档/逐事件归档历史的旧版本升级时，先跑一次性迁移 `scripts/migrate_slim_task_storage.py`（停机/排水后，默认 dry-run 报数，`--apply` 执行：event-history 收敛单份、存量 zip 导出产出后删除、清 live.patch DB 行与孤儿记账行）。
5. `runtime.sqlite3` 收缩用 `scripts/compact_task_database.py`（默认 dry-run 报数；`--apply` 裁剪终态任务早于 `--retention-days`（默认 14，0=跳过裁剪）的五张大行表、`--backup` 先镜像、`--vacuum-full` 对存量库做 VACUUM 迁移，脚本自带 1.2× 空间预检）。**必须在服务停机或排水后运行**（VACUUM 需独占连接）。运行时侧新库自动 `auto_vacuum=INCREMENTAL`；运行时行裁剪由对账 loop 每 23h 卡权执行，且仅当 `detail_retention_days>0` 时生效（紧急水位跳过）。两点会让收缩"看起来无效"：`nodes.payload_json` 不在裁剪清单里，它是明细之外节点正文的另一份完整副本（明细存储形状见 `runtime-overview.md`「节点正文在库内只存一份」）；删行本身只把页还给 freelist，不 `--vacuum-full` / `incremental_vacuum` 就不会向操作系统归还空间，而这些页会被后续写入立刻复用。
6. 磁盘接近满时不要手工对大 sqlite 库执行 VACUUM——它需要约一倍库大小的临时空间，会立刻打穿剩余水位（脚本内置同款预检）。

### 任务大厅卡顿 / 冻结（浏览器端）

症状族：从任务详情返回大厅后滚动无响应或「一滑动就卡住」；重者整个 Edge 窗口挂「未响应」数十秒，甚至出现 WerFault 崩溃报告；卡顿有时在用户并未操作浏览器时自行发生。关键辨识：卡顿期间大厅的性能监控数字仍在正常刷新——页面主线程与绘制活着，阻塞在输入/合成层之下，或本质是滚动位置被反复清零。

已确认并修复的原因（契约详见 `web-and-admin.md`「Web Event Loop Contract」）：

1. worker 心跳时间戳进大厅网格渲染签名：每次心跳整网格重建，网格自身是滚动容器，`scrollTop` 归零——表现为「一滑动就卡死而数字照常刷新」。签名只保留影响像素的字段，同页重建还原滚动位置（测试 `tests/resources/org_graph_tasks.hall_scroll_survival.test.js`）。
2. 详情退出路径把状态捕获、大 DOM 拆除、大厅请求挤在同一点击帧：重步骤推迟一个宏任务执行（测试 `tests/resources/org_graph_app.task_detail_exit_defer.test.js`）。
3. 服务端事件循环阻塞族（SQLite 读串行、CEO 目录构建、netstat 子进程）均已卸载或缓存，见同一契约章节。

已排除的因素（含证据）：

- 服务端业务代码阻塞：超时时刻 py-spy 抓栈为事件循环空闲等待；探针监控未见新增服务端超时。
- 返回路径的请求差异：箭头直达与侧栏绕行发起的请求完全相同（`GET /api/tasks`、`/api/ws/tasks`、worker-status），控制台探针日志核对一致。
- 点击帧时序：两跳中转垫片（60ms 与 5s 停留）实测仍卡，垫片已移除。
- 陈旧缓存 JS：静态资源以 no-store 下发，刷新即最新。
- JS 长任务：卡顿窗口内 longtask ≤120ms、无帧缺口、无输入延迟——冻结发生在 JS 层之下（渲染器进程/操作系统）。
- 焦点抢占只解释「点击被吞」，不解释「未响应」：前台窗口监视记录过一次 visibility 闪烁 + 窗口失焦吞点击，与真挂起以此区分。

当前未解决的头号嫌疑：机器级内存超卖。7.7GB 内存空闲长期仅 800–1700MB，卡顿时刻伴随「内存风暴」——空闲内存数秒内跌数百 MB、硬缺页 10–40 万/秒、CPU 逼近满载，Edge 渲染器被裁剪挂起即「未响应」，极端时崩溃。风暴可在用户未操作浏览器时发生。已知大户与风暴嫌疑：抓取类任务周期性派生爬虫/CDP 子进程、重复启动的服务栈（两套 web+worker 并存）、Edge 自身 2–2.5GB/30+ 进程、远控推流、Defender 实时扫描。待验证项：后台驻留标签页（详情视图未关）持续接收 live 事件、DOM 与状态增长，疑似撑大标签页工作集。

排障顺序：

1. 拿到卡顿的本地墙钟时刻（精确到分钟）。
2. 对照四类监控（排查期部署在 `%TEMP%`，失效则按此清单重建）：`os_monitor.csv`（空闲内存/CPU/Edge 工作集/进程数/硬缺页，约 3s 采样）判当时有无风暴；`proc_top.csv`（每 5s 内存前八进程）定风暴起点谁在吃内存；`foreground_log.csv`（每 200ms 前台进程与窗口标题）——dwm 挂「未响应」幽灵标题=窗口真挂起、WerFault=崩溃、前台突变=焦点被抢；`stall_captures/`（200ms 探 worker-status，>0.5s 即 py-spy 抓 web 进程栈）排除服务端阻塞。
3. 有风暴：按 `proc_top.csv` 的大户处置——暂停对应抓取任务、消除重复服务栈；长期方案是加内存。
4. 无风暴：在被卡标签页重装控制台探针再复现，探针测量项：关键函数耗时、LoAF 阻塞归因（JS/渲染/非 JS）、帧缺口、输入延迟、wheel 事件与 scrollTop 生效对照、JS 堆悬崖、窗口焦点与可见性变化。
5. 滚动归零类的验证口径：滚到中部停留 10 秒位置不被拽回，且 `S.taskHallStats.task_hall_full_render_count` 不增长。

## 6. 维护时的高风险修改类型

### 修改 session turn 逻辑

涉及文件：

- `g3ku/runtime/session_agent.py`

风险：

- 容易同时影响 user turn、heartbeat turn、cron turn、pause/resume。

### 修改 task runtime 总装配

涉及文件：

- `main/service/runtime_service.py`

风险：

- 工具集、治理、日志、worker、内容服务可能一起被带坏。

### 修改工具可见性与候选池

涉及文件：

- `g3ku/runtime/context/`
- `main/service/runtime_service.py`

风险：

- 不一定会报错，但会显著改变 agent 行为，是高隐蔽性回归。

### Provider Bundle Refresh

- provider-facing tool bundle 的刷新时机、排序与压缩边界约定详见 `context-and-cache-troubleshooting.md`「Prompt Cache Family 与 Actual Request」；bundle 变化会直接影响 prompt cache 前缀稳定性，改动后应验证缓存表现。

## 7. 维护建议

### 先判断问题属于哪条主线

不要一上来全文搜索。先判断它属于：

- 会话主线
- 任务主线
- heartbeat
- Web/API
- 配置/模型
- 外部桥接（/api/v1）

### 优先从集成点往下看

很多问题不在最底层，而是在集成处。例如：

- Web runtime 装配失败
- session key 路由错误
- candidate tool 没进 callable set

### 改提示词也要当成代码改动看待

如：

- `g3ku/runtime/prompts/heartbeat_rules.md`
- `g3ku/runtime/prompts/ceo_frontdoor.md`

这些文件会直接改变 agent 行为，回归风险不低。

### 对 `main/service/runtime_service.py` 保持敬畏

它过于 central。每次改动后，最好至少验证：

- 任务创建
- 节点执行
- task detail API
- 工具可见性

## 8. 新功能接入时的建议切入点

### 加新 tool

先看：

- `g3ku/agent/tools/`
- `tools/<tool_name>/`
- `main/service/runtime_service.py`

### 加新 skill

先看：

- `skills/`
- `g3ku/agent/skills.py`
- `ResourceManager` 相关逻辑

### 接入新的外部渠道

先看：

- `docs/architecture/external-agent-api.md`（API 契约）
- `bridges/qq-onebot/`（参考桥实现）
- 新桥接作为独立进程/仓库开发，G3KU 本体零改动

## 9. 最小验证清单

做完较大改动后，先做自动化前置检查：

- `python -m ruff check .`（或 `scripts/lint.sh` / `scripts/lint.ps1`）
- `python -m pytest --collect-only -q`
- `python -m pytest tests/resources/test_resource_runtime_smoke.py -q`（冒烟子集）

自动化通过后，再至少人工验证下面的清单：

1. CLI 同步会话可用
2. Web 页面可打开
3. Web 会话可发送并返回
4. 至少一个异步任务可创建并完成
5. task detail / node detail API 正常
6. 若改动涉及工具系统，验证候选工具与 callable 工具行为
## Memory Queue Workflow

For the queued Markdown memory runtime, the first operator checks should be:

1. Inspect `memory/memory_state.sqlite3` for the authoritative memory rows, `refresh_count`, `passed_count`, `is_compressed`, and `from_user` state.
2. Inspect `memory/MEMORY.md` for the regenerated prompt snapshot currently injected into CEO/frontdoor.
3. Inspect `memory/queue.jsonl` for pending `write` / `delete` requests.
4. Inspect `memory/failed.jsonl` for parked failed batches: each row carries `category` (`provider_error` / `protocol`), `status` (`parked` / `requeued`), retry counters and a full `error_history`. A non-empty file means some turns of memory are waiting for a success signal or an operator decision.
5. Inspect `memory/ops.jsonl` for the latest terminal batch history (`applied`, `precheck_failed`, `operator_discarded`).
6. If a processed row exposes `request_artifact_paths`, inspect the referenced files under `.g3ku/memory-requests/` before blaming prompt assembly or the provider adapter.
7. Use `g3ku memory current`, `g3ku memory queue`, and `g3ku memory flush` when you need a quick operator view without manually opening files.
8. If the queue head is stuck in `processing`, inspect `.g3ku/config.json -> models.roles.memory` before debugging the frontend.

Operator workflow for parked failed batches:

1. The web `记忆管理` page shows the `失败记忆` panel only while parked records exist (left column, under the pending queue). Cards are red, carry the failure category and a retry icon; clicking a card opens the detail drawer with the full error history.
2. `provider_error` records rejoin the queue automatically: every successfully applied batch requeues the oldest parked provider-error record at the queue tail. During a provider outage nothing retries; recovery is paced by real traffic. `queue.auto_requeue_on_success=false` disables the signal entirely.
3. `protocol` records (the memory agent answered but never produced a valid `memory_apply_batch` result) never auto-requeue; they wait for an operator.
4. Manual retry and discard are ordinary operator actions on this surface (no environment flag): retry requeues the record under its original `request_id`s; discard writes an `operator_discarded` terminal row into `ops.jsonl`, removes the parked record and any stale queue rows, and makes those `request_id`s dedupe-processed. Both actions append to `memory/admin_audit.jsonl`. Only the legacy queue-head retry contract still requires `G3KU_ENABLE_MEMORY_ADMIN_MUTATIONS`.
5. The UI buttons stay usable for every operator; the protective layers are the in-app second-confirmation dialogs and the audit trail. Inspecting parked records needs no flag either.

Operator debugging order for a stuck queue head:

1. Check whether `models.roles.memory` is empty or points to an invalid model chain.
2. Check the queue-head `last_error_text`, `last_error_at`, `retry_after`, and `processing_started_at`.
3. Check `memory/failed.jsonl` and the worker logs for provider/tool-call failures or semantic validation failures — processing failures park instead of blocking, so an empty queue with a non-empty failed file is the normal signature of a provider outage, not a lost queue.
4. Only after the runtime side is healthy should you debug the web `记忆管理` page.

Operator debugging order for duplicated successful memory writes:

1. Compare `memory/ops.jsonl` rows by `request_id`, not only by `batch_id`.
2. If the same `request_id` appears in two successful processed rows, treat that as duplicate consumption of one queue request rather than proof that the frontdoor called `memory_write` twice.
3. Inspect live `python -m g3ku web` / `python -m g3ku worker` processes and verify only one runtime instance should be actively consuming the memory queue.
4. If multiple runtimes were active, treat duplicate rows as historical worker-contention evidence first, then inspect whether the current build already has the memory-worker lease and processed-request dedupe protections.

### Memory Maintenance CLI

The memory CLI keeps only the queued Markdown runtime operator surface:

- `g3ku memory current`
- `g3ku memory queue`
- `g3ku memory flush`
- `g3ku memory doctor`
- `g3ku memory reconcile-notes`
- `g3ku memory import-legacy <path>`
- `g3ku memory cleanup-legacy`

The old legacy-only commands such as runtime stats/trace/explain, `migrate-v2`, `reset-runtime`, decay, and pending-fact review are not part of the active operator contract.

The operator-oriented maintenance commands beyond `current`, `queue`, and `flush` are:

- `g3ku memory doctor`
  Read-only health check for the queued Markdown memory layout.
- `g3ku memory reconcile-notes`
  Explicit note-ref reconciliation for `MEMORY.md` and `memory/notes/`.
- `g3ku memory import-legacy <path>`
  Minimal one-shot importer for legacy memory exports.

Use them with these boundaries in mind:

- `doctor` is inspection-only: it never rewrites `MEMORY.md`, creates or deletes notes, mutates queue state, or bootstraps missing paths. It should be the first stop when an operator suspects notebook corruption or a blocked queue head. It checks the managed Markdown block format, note-ref consistency, orphan notes under `memory/notes/`, malformed `queue.jsonl` rows (line-level diagnostics), stuck `processing` heads, and parked failed batches (`failed_parked` check with `failed_parked_count`; any parked record reports issues_found and lists the first five `failed_id(category)`); malformed queue rows surface as explicit queue-parse issues with a non-zero exit instead of a bare JSON decode crash.
- `reconcile-notes` is the explicit repair path for note/file consistency. It may create placeholder note files for missing refs and deletes orphan note files only when the operator passes the explicit delete flag.
- `import-legacy` is dry-run by default: it parses the legacy payload and prints a summary without creating `memory/`, `MEMORY.md`, `queue.jsonl`, `ops.jsonl`, or note files. Writing requires `--apply`, and the target notebook should already be empty — do not use it as a merge tool for a live non-empty queue.
- `cleanup-legacy` is dry-run by default and lists removable legacy artifacts (`HISTORY.md`, structured projections, sync journals, pending/audit files, `context_store/`). `--apply` refuses to delete data-bearing legacy artifacts while `MEMORY.md` is still empty: import or review old data first, then delete leftovers once the new notebook already contains the migrated memory.

Recommended operator order:

1. Run `g3ku memory doctor` first.
2. If the only issues are missing note files or orphan notes, use `g3ku memory reconcile-notes`.
3. If the notebook is empty and you are doing a controlled migration, run `g3ku memory import-legacy <path>` once without `--apply`, inspect the summary, then rerun with `--apply`.
4. After migration is complete and `MEMORY.md` is already authoritative, run `g3ku memory cleanup-legacy` once in dry-run mode, review the paths, then rerun with `--apply` to remove leftovers.

Queue-head recovery caveats that matter during operations:

- A `processing` head that survives restart is expected durable state. Do not assume it means a live worker is still attached.
- If `retry_after` is still in the future, the restarted worker should leave that head untouched and keep later items blocked. Once `retry_after` has passed, the same head becomes eligible for retry. Blocking heads come from the configuration paths (`memory` role not configured, runtime config unreadable) or from builds that predate failure parking; processing failures in the current runtime park into `memory/failed.jsonl` and leave the queue flowing.
- `processing_started_at` is the first-claim timestamp for that head batch. It should remain stable across retries, so a new `last_error_at` with an old `processing_started_at` is normal. Parked records keep the original claim timestamp inside their stored `items`.

## Docker / Compose Startup

G3KU has two supported operator startup modes:

- direct local startup through `start-g3ku.ps1` / `start-g3ku.sh`
- container startup through `compose.yaml`

For the container path, the maintenance contract is:

- the `web` container owns Web shell startup, heartbeat, cron, and China bridge supervision
- the `worker` container owns the background task worker only
- both containers must share the same workspace state

The required durable paths are:

- `.g3ku/`
- `memory/`
- `sessions/`
- `temp/`
- `skills/`
- `tools/`
- `externaltools/`

Do not treat only `.g3ku/` as sufficient persistence. Detached task temp files live under `temp/tasks/`, external tool installs live under `externaltools/`, and mutable skill/tool resource copies may also need to survive restart.

Deployment unlock has an operator-facing env contract:

- `G3KU_BOOTSTRAP_PASSWORD` allows a locked project to auto-unlock at process start
- `.g3ku/llm-config/auto-unlock.key` (written when the operator checks 记住密码自动解锁 in the web 设置 dialog) unlocks the project at start without any env var; it holds the master key, so treat it as a password
- `G3KU_INTERNAL_CALLBACK_URL` allows the worker container to call back into the web container over the Compose network instead of assuming `127.0.0.1`
- The official container image also pins text/runtime locale explicitly with `LANG=C.UTF-8`, `LC_ALL=C.UTF-8`, and `PYTHONIOENCODING=utf-8`. If container-only `exec`, validation-command, or Python traceback output shows mojibake, verify those three env vars before blaming prompt assembly or websocket rendering.

If Docker startup appears healthy but detached tasks never report back, inspect these in order:

1. the shared `.g3ku/internal-callback.json` payload
2. the effective `G3KU_INTERNAL_CALLBACK_URL` in both containers
3. whether `web` is healthy at `/api/bootstrap/status`
4. whether the worker container actually reached unlocked state

## 10. Memory Reset Workflow

`memory/` contains both user long-term memory data and unified-context retrieval state, including tool/skill catalog retrieval indexes. A full physical reset of the directory removes all of these together.

Operator expectations:

- Use the explicit memory maintenance command to fully reset `memory/`; do not manually delete a subset of files.
- The reset recreates baseline managed files and sync state, but it does not immediately rebuild tool/skill catalog retrieval inside the command itself.
- After reset, user long-term memory is empty.
- After reset, tool/skill semantic retrieval is also empty until the next runtime startup.
- On startup, the runtime should rebuild catalog retrieval automatically by syncing the resource catalog back into the unified context store.

If tool/skill retrieval does not return after restart, first inspect resource runtime initialization and then confirm that the memory runtime reaches a healthy catalog-bridge state.

