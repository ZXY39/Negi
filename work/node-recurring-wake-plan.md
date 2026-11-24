# 节点周期自醒：`schedule_wake` 节点道周期档

状态：**已实现并真机端到端验收通过**（P1-a…f 全部落地）。
提交：`f27f9c1b`（共享 schedule math）→ `05f50ef2`（两档 timer 表+迁移）→ `f38fcd7a`（节点周期 arm+工具路由）→ `eef1ec2a`（清扫重臂）→ `e48a4c79`（designed_idle 计入周期）→ `f20d2a14`（架构文档）。
共享工作树提示：验收期间另一 agent 提交了 `4dfb934a`（cross-process load balance）并在 `main/service/runtime_service.py` 留有未暂存改动，与本计划无关，未触碰。
靶子来源：frontdoor 问题报告（2026-11-24 13:30，IF 线 `task:93f215c1db72` 要"每天 06:00 节点自己醒"）+ 本会话根因分析。
关系：P0（cron 默认治理角色收成 `['ceo']`，口径一致化）独立可做，**不在本计划范围**；本计划只做节点侧真周期定时。

## 1. 为什么做，以及为什么不是修 cron

报告原始需求是"执行节点每天 06:00 自主唤醒"。分析已坐实 cron 给不了这个收益，三道独立的闸：

1. worker 进程建 loop 不传 `cron_service`（`g3ku/web/launcher.py:375`）→ cron 在节点侧恒 `tool_handler_unavailable`（`g3ku/resources/manager.py:640`）。
2. 就算接上 handler，节点 runtime_context 没有 `channel`/`chat_id`（`main/runtime/node_runner.py:3718` 只给 `session_key=task.session_id`）→ `CronTool._add_job` 守卫直接拒（`g3ku/agent/tools/cron.py:157`），这是文档明文合同（`docs/architecture/tool-and-skill-system.md:237`「节点里建不了提醒」）。
3. cron 派发唤的是创建会话（`g3ku/cron/runtime_dispatch.py:108`），不是节点；节点没有自己的会话。

节点定时的现行后端是 `task_node_timers`（一次性、重设即替换、5s 清扫、耐久跨重启）。报告 §8 的替代链"一次性闹钟 + 每轮续挂"有断链风险，且 continuous 节点豁免失速检测 → 断了无运行时告警（属实）。**P1 给节点道加周期档，把"续挂"从模型自觉变成机制保证。**

## 2. 现状落点（逐条带 path:line）

- 表 schema：`main/storage/sqlite_store.py:242-251`，`PRIMARY KEY (task_id, node_id)`，列 `wake_at_ms / note / armed_at / claimed_by / claimed_at_ms`，**无周期列**。
- 记录模型：`main/models.py:370-386`（`TaskNodeTimerRecord`，docstring 写明"每个节点至多一份待触发唤醒"）。
- store 方法：upsert `:2138-2167`（`ON CONFLICT(task_id,node_id)` 覆盖、连领取凭证一起清）/ get `:2169` / list `:2177` / claim_due `:2185-2223`（`RETURNING` 防同毫秒重领，过期凭证可再领）/ delete `:2225` / `_parse_task_node_timer` `:2231`。
- 常量：`_WAKE_CLAIM_STALE_MS = 120_000`（`sqlite_store.py:2132`）、`_NODE_WAKE_TIMER_POLL_SECONDS = 5.0`（`main/service/runtime_service.py:398`）。
- arm 写点：`main/runtime/node_runner.py:3323-3360`（`_arm_node_wake`，已有 `node_terminal`/`task_terminal`/`wake_at_ms_required` 拒收）。
- 工具：`main/runtime/internal_tools.py:308-403`（`ScheduleWakeTool`，`rerun_safe=True`、`NOTE_LIMIT=500`、`hide_universal_timeout_parameter=True`）；时刻解析 `resolve_wake_moment` 来自 `g3ku/runtime/wake_clock.py:8`。
- 注入：`main/runtime/node_runner.py:3474-3477`（`time`/`schedule_wake` 对**执行节点和验收节点都常驻**）。
- 清扫：`main/service/runtime_service.py:1309-1402`（`_node_wake_timer_loop` + `sweep_due_node_wakes`）。三结局：终态删行 / 暂停落箱 `wake=False` / 投递异常留 claim 退避 2min。正文 `_node_wake_notice_text` `:1404`（英文、自带"这是你自己设的"、晚 ≥60s 提示对钟）。
- 失速豁免：`main/runtime/designed_idle.py:18-64`（`task_is_designed_idle`），**只认 `node_wait_external`（`:59`），不认在臂 timer**。
- cron 解析复用件：`CronSchedule`（`g3ku/cron/types.py`）、`_compute_next_run`（`g3ku/services/cron.py:43-67`，`every`/`cron`/`at`，cron 用 croniter）、`resolve_timezone`/`validate_timezone_name`（`g3ku/cron/timezones.py`）。**`croniter>=6.0.0,<7.0.0` 已是依赖**（`pyproject.toml:37`），无新依赖。
- 迁移机制：`_ensure_column`（`sqlite_store.py:773`，加列）、版本闸门 `PRAGMA user_version`（`:779-810`）、表重建机制（`:839`，从 `PRAGMA table_info` 反向合成表定义，避免与建表语句两处漂移）。
- 测试：`tests/test_node_wake_timer_sweep.py`（四条静默丢唤醒判据）、`tests/test_node_time_and_wake_tools.py`；失速侧 `tests/resources/test_task_stall_standby_reanchor.py`、`tests/test_continuous_nodes.py`。

## 3. 非目标

- 不动 frontdoor cron 工具 / 调度器 / `jobs.json`（P0 另议）。
- 不建第二调度器、不做 worker→web 跨进程 RPC。
- 不做会话级周期（会话侧 `schedule_wake` 仍走 cron 一次性 `at`，合同不变）。
- 不改一次性档既有语义：重设替换、late 提示、claim 退避、终态删行全部保留。
- 不做秒级 cron（croniter 标准 5 段最小分钟级）；`every_seconds` 最小 60s（D3）。
- 不做节点详情 UI 显示 `next_run`（可选后续，另立）。
- 不动 `enter_wait` / 等待图 / 信箱投递矩阵本体。

## 4. 设计决策（带默认值，逐条可否决）

**D1 共存形状（默认：单表，`kind` 入 PK）。** 一个节点至多一份一次性 + 至多一份周期，互不覆盖。PK 改 `(task_id, node_id, kind)`，`kind ∈ {once, recurring}`。理由：报告这类采集节点既挂每日周期、又可能设临时重试闹钟；单行替换会让一次临时 arm **静默取消每日 schedule**。`rerun_safe` 仍成立（重放替换同一 `(task,node,kind)` 行）。
- 备选（更低迁移风险）：周期单独建 `task_node_schedules` 表，一次性表完全不动。代价是两套 claim 代码路径。若否决 D1 默认，走这条。

**D2 参数面。** `schedule_wake` 节点道新增 `every_seconds` / `cron_expr` / `tz`（与 cron 工具同名同义），与现有 `at` / `after_seconds` **四选一**，多传拒收。复用 `CronSchedule` + croniter，不引第二套解析。`tz` 仅随 `cron_expr`（沿用 `_validate_schedule_for_add` 判据，`g3ku/services/cron.py:70`）。

**D3 最小间隔（机制级 clamp）。** 常量 `_MIN_RECURRING_INTERVAL_SECONDS = 60`：`every_seconds < 60` 拒收；`cron_expr` 天然分钟级起步。防模型设 5s 周期烧 token 热循环，不靠提示词自觉。

**D4 重臂语义。** 周期行投递成功后按 schedule 算下一跳（复用 D2 的 next-run helper），更新 `wake_at_ms`、清 claim；`once` 行仍删行。重臂是 upsert 同一行，不新增行。

**D5 暂停期（默认：静默推进，不投递）。** 节点/任务暂停时，周期行**只推进 `next_run`、不落箱**（区别于一次性档的"落箱 `wake=False`"）。理由：长暂停下每周期投一条会攒一箱未读 `wake_timer`。暂停位保持不变（A5=A）。

**D6 停机错过（默认：补发一次，再重臂到下一未来槽）。** 重启后 `wake_at_ms` 已过期多个周期的行，清扫**补发一次**（带现行 late 提示），随后重臂到下一未来槽，**绝不补发成一串**。保住"每日采集"意图（机器 06:00 关机、09:00 开机仍补当天一次）。
- 备选：skip-to-next（与 cron recurring 重启恢复同语义，不补发）。若否决默认走这条；产品取舍点，明写。

**D7 验收节点拒收周期。** arm 时 `node_kind == acceptance` 且传周期参数 → 拒收（`acceptance_no_recurring`），保留一次性。理由：`wake_timer` 永不唤醒验收节点（`docs/architecture/main-task-runtime.md:18`），周期档对它是纯噪声。

**D8 失速豁免。** `task_is_designed_idle` 把"在臂周期 timer"计入外部唤醒源：读 `store.list_task_node_timers(task_id)`，存在 `kind=recurring` 且节点非终态的行即满足 `external`（与 `node_wait_external` 同档）。理由：每日档节点常态停靠 24h，今天不算 external 会每 10min 档收 `task_stall`。时钟本就是合法唤醒源，与"只等任务内节点才算死锁"的意图一致；未读闸（`:61`）不变，唤醒派发真丢失仍按 `suspected_stall` 报。

**D9 失败退避。** 投递异常沿用现行"留 claim、过 2min stale 窗口再重试"；**重臂不得重置失败行的 claim**（否则磁盘满一类故障下 5s 拍热循环）。

**D10 模型面合同。** 更新 `ScheduleWakeTool.description` / `model_description` / `parameters`（`internal_tools.py:332-373`）：写清"周期档一次设好、到点自醒、重设替换同档、与一次性互不覆盖、最小 60s"。synthetic resident 工具无 toolskill，函数级描述是唯一模型可见载体（`docs/architecture/tool-and-skill-system.md:203`）。实现时 grep `schedule_wake` 于 `main/prompts` 与 g3ku 提示词，若有引用一并核口径。

## 5. 数据模型与迁移（D1 默认）

新增列（随表重建一次到位，不必单独 `_ensure_column`）：

- `kind TEXT NOT NULL DEFAULT 'once'`
- `schedule_kind TEXT NOT NULL DEFAULT ''`（`every` / `cron`，仅 recurring）
- `every_ms INTEGER NOT NULL DEFAULT 0`
- `cron_expr TEXT NOT NULL DEFAULT ''`
- `tz TEXT NOT NULL DEFAULT ''`

PK 重建步骤（SQLite 不能 ALTER PK，用 `:839` 那套反向合成机制）：

1. 版本闸门：新增一个 `PRAGMA user_version` 常量（参照 `_DETAIL_TRACE_SUMMARY_RETIREMENT_VERSION` 模式 `:779-810`），判"要不要跑"只跑一次。
2. 建 `task_node_timers_new`，PK `(task_id, node_id, kind)`，含全部新旧列。
3. `INSERT INTO ... SELECT task_id, node_id, wake_at_ms, note, armed_at, claimed_by, claimed_at_ms, 'once', '', 0, '', '' FROM task_node_timers`（存量行全部落 `kind='once'`，语义不变）。
4. drop 旧表、rename 新表、重建索引 `idx_task_node_timers_wake_at`（`:694`）。
5. 整个迁移在 `_run_write` 事务内；失败回滚不留半截表。

`TaskNodeTimerRecord`（`main/models.py:370`）加对应字段（默认值保后兼容）。store 方法改：

- `upsert_task_node_timer` 增 `kind`/schedule 参数，`ON CONFLICT(task_id,node_id,kind)`。
- `get`/`delete` 增 `kind` 参数（默认 `once` 保既有调用点）。
- `claim_due_task_node_timers` `RETURNING` 带新列；`_parse_task_node_timer` 解析新列。
- 新增 `rearm_task_node_timer(task_id, node_id, kind, wake_at_ms)`：更新时刻 + 清 claim（D4），与 upsert 分开避免误清 schedule 字段。

回滚：迁移前自动备份 `runtime.sqlite3`（参照仓库既有备份惯例）；版本闸门保证升级幂等。

## 6. 实现子项（阶段，每完成一项即提交）

- **P1-a 共享 next-run helper**：把 `_compute_next_run` 的 every/cron 计算提为公共件（如 `g3ku/cron/schedule_math.py`），CronService 与节点清扫共用，避免跨模块 import 私有函数。参数解析/校验（四选一、tz 归属、D3 clamp）落 `g3ku/runtime/wake_clock.py` 或新 helper。
- **P1-b 表迁移 + store**：第 5 节全部。守卫测试先于实现写（迁移幂等、存量行落 once、PK 共存）。
- **P1-c arm 写点**：`node_runner._arm_node_wake` 收周期参数 → 组 `CronSchedule` → 算首个 `wake_at_ms` → upsert `kind=recurring`；D7 验收拒收、D3 clamp、终态拒收沿用。
- **P1-d 清扫重臂**：`sweep_due_node_wakes` 周期分支——终态删行（继承）/ 暂停静默推进（D5）/ 正常投递后 rearm（D4）/ 异常留 claim（D9）/ 停机过期补发一次再重臂（D6）。`once` 分支一字不动。
- **P1-e 失速豁免**：`designed_idle.py` 计入在臂周期（D8）。
- **P1-f 模型面合同**：D10。

## 7. 验收

### 单测（扩现有文件，不新建平行测试面）

- `tests/test_node_wake_timer_sweep.py`：周期重臂（投递后 next 正确、claim 清）、暂停静默推进不落箱、终态删行、异常留 claim 不热循环、停机过期补发一次（D6）、once 分支回归。
- `tests/test_node_time_and_wake_tools.py`：四选一互斥、tz 归属、D3 clamp、D7 验收拒收、周期与一次性共存互不覆盖、`rerun_safe` 重放替换同档。
- 新增 store 迁移用例（落 `tests/` 下）：版本闸门只跑一次、存量行落 once、PK 共存、回滚。
- `tests/resources/test_task_stall_standby_reanchor.py` / `tests/test_continuous_nodes.py`：在臂周期计入 designed_idle（D8），未读闸仍报真丢失。

### 命令

- `python -m ruff check .`
- `python -m pytest tests/resources/test_resource_runtime_smoke.py -q`
- 上述测试文件 + `tests/test_node_wake_timer_sweep.py`、`tests/test_node_time_and_wake_tools.py`
- 本机存在先存环境性失败，基线用 detached worktree 复核后再归因，**不用 `git stash`**。

### 真机验收（单测不算完成）

重启本机 web + worker（端口 18790，数据根 `D:\NEGI`，优雅 exit + `work/start-web-logged.cmd`），用短周期（`every_seconds=60`）构造，不等 06:00：

1. 真任务节点 arm `every_seconds=60` → 表里一行 `kind=recurring`，到点自醒、投 `wake_timer`、重臂下一跳；连观察 2–3 跳。
2. arm `cron_expr="*/1 * * * *"` → 分钟级到点自醒（tz 缺省服务本地）。
3. 同节点再 arm 一个一次性 `after_seconds=120` → 两行共存，一次性到点删行、周期行不受影响（D1）。
4. 重启 worker → 周期行存活、到点照醒；构造过期行验证补发一次再重臂（D6）。
5. 暂停任务 → 周期静默推进、信箱不攒未读（D5）；恢复后照醒。
6. 节点终态 → 周期行删除（D10 继承）。
7. 验收节点 arm 周期 → 拒收（D7）。
8. 周期节点长停靠 → 不再收 `task_stall`（D8）；构造"该醒没醒"（未读停死）仍报 `suspected_stall`。
9. frontdoor 真对话 cron add/list/remove 回归照旧（本计划不动它，但要回归）；节点一次性 `schedule_wake` 回归。

## 8. 风险

- **PK 重建是唯一动存量数据的步骤**（P1-b）。版本闸门 + 事务 + 迁移前备份兜底；守卫测试先于实现写。若评审认为风险过高，否决 D1 走独立表备选（第 4 节）。
- **D6 补发一次是产品取舍**：补发保住每日采集意图，但重启瞬间可能多投一条；已用"至多一次 + 重臂到未来槽"挡住补发风暴。skip-to-next 是否决项。
- **D8 放宽失速豁免**可能遮蔽真死锁——未读闸（`designed_idle.py:61`）保留，唤醒派发真丢失仍报；守卫测试必须覆盖"在臂周期 + 未读停死 → 仍报 stall"。
- **D5 暂停静默推进**与一次性档"落箱 wake=False"语义不同，文档要写清两档差异，避免维护者按一次性档读周期档。
- **常驻工具 schema 变更**（P1-f）→ 钉住 `tools[]` 字节变 → 活跃会话/节点在下个重印边界一次性缓存未命中，此后回血（`docs/architecture/tool-and-skill-system.md`「Provider Tool Surface」）。如实记明，不声称零成本。
- **每次到点 = 一个节点回合**（模型调用成本），与 re-arm 链本来要付的相同；每日档≈可忽略，秒级被 D3 挡死。
- **解析复用面**：若 P1-a 提公共件改动 CronService 调用点，须回归 frontdoor cron（第 7 节真机第 9 条）。

## 9. 架构文档（实现后必须走 `negi-architecture-maintenance`）

- `docs/architecture/main-task-runtime.md`「节点信箱与唤醒（现行）」`wake_timer` 那条（`:25`）：补周期档（kind 共存、重臂、暂停静默推进、停机补发一次、验收拒收、最小 60s）。
- `docs/architecture/tool-and-skill-system.md`「fixed builtin tools」/「3.1.1 常驻」：`schedule_wake` 节点道周期参数与合同；§3.5 cron 合同不动（P0 另议）。
- `docs/architecture/heartbeat-system.md`「Task Stall Detection」`designed_idle` 那条（`:171`）：在臂周期 timer 计入外部唤醒源。
- `docs/architecture/README.md`：Debugging Entry Points 补一条"节点周期闹钟没响/暂停期攒未读/重启补发"指向 main-task-runtime.md；预计无新主题、无阅读顺序变化，按 skill 清单复核。
- 就地改写、不追加附录；跑 `python scripts/check_architecture_docs.py`。

## 10. 交接段

- 本计划只覆盖节点周期自醒；**P0（cron 治理角色收 ceo）另议**，落点 `main/governance/action_mapper.py:19`，无测试钉住 cron 角色默认值。
- 冻结标识符不得改：`g3ku` 包名、`G3KU_*` 环境变量、`.g3ku` 数据根、上下文协议标记、`negi_*` 工具名、`schedule_wake` 工具名。
- 提交信息用英文；提交身份用 `-c` 临时指定历史作者 `ZXY39 <1045273950@qq.com>`，不改 git config；共享工作树只 add 自己的文件。
- 每完成一个 P1 子项即提交，不中途停；长任务按批 commit。
- 若上下文耗尽：先更新本文件状态行与已完成子项，再开新会话续跑。

## 11. 真机实测（2026-11-24，端口 18790，数据根 D:\NEGI，worker:08cb6d5e9ac8）

脚本：`work/live_recurring_wake_acceptance.py`（前门建连续任务）、`work/live_recurring_wake_directed.py`（定向强制 every_seconds）、`work/live_recurring_observe.py`（纯观察）；结果 `work/recurring-wake-observe-result.json`。

- **活库迁移**：重启后 `task_node_timers` PK 已是 `(task_id,node_id,kind)`，新列 `schedule_kind/every_ms/cron_expr/tz` 齐全；原表无待触发行，空表重建无数据丢失。worker 在线、启动无异常、`negi cron list` 回归正常。
- **工具 arm 路径**：首个探针节点模型习惯用了 `after_seconds=60`（一次性，连 arm 4 次——正是本计划要替代的续挂链）；定向消息强制 `every_seconds` 后，节点真 arm 周期档（`kind='recurring'`, `every_ms=60000`）。**模型 steering 发现**：节点模型默认偏好一次性，周期档需提示词/指令明确引导（机制本身正确，工具描述已写清周期档）。
- **清扫 firing+重臂**（活体）：baseline 已 10 条 wake 通知（每 60s 自醒、不堆积）；观察窗内新增 1 条，`wake_at_ms` +60114ms，claim 清空、递推参数保留；正文明写"周期档自己重臂、你不必再设"。
- **暂停静默推进**（活体）：pause ok，`wake_at_ms` +60097ms 但新增通知 **0**（D5 不攒未读成立）。
- **清理**：探针任务已删除，活库 recurring 行归零。

测试与 lint：`ruff check .` 全通过；定向测试集 271 通过（schedule_math / timer store / 节点 wake 工具 / sweep / designed_idle / cron 回归 / stall / continuous / smoke）；资源治理层快批 172 通过。`test_description_budget.py::test_repo_tool_manifests_fit_the_description_budget` 失败（`process_kill` 227>160），已在 detached worktree `2508ea0b` 基线复核为**先存失败**，与本次改动无关（本次未触及 process_kill）。