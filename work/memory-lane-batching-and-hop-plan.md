# 记忆车道：主动合批 / 复核单行 / 砍收口跳 / 合并条数显示

状态：已实现并本机验收通过（提交 c0332bc5）。节点接入另立计划。
靶子来源：本机会话讨论（2026-11-24）+ 本机活体读数 `memory/ops.jsonl`、`memory/queue.jsonl`、`memory/review_state.json`、`.g3ku/memory-requests/`。

## 1. 为什么做这四件，以及为什么不做吞吐档

本机实测负载（口径：台账 7 天滚动窗口，2026-11-18 16:39 → 11-23 19:25，5.11 天）：

- 26 批 = **5.08 批/天**，最忙一天 13 批，批间中位间隔 **256 秒**。
- `queue.jsonl` **0 字节**、`failed.jsonl` **0 字节**、`review_state.json` 里 **23 个会话缓冲轮次全为 0**。
- 当前 `console.log`（10.7 万行）零条记忆批次停车、零条记忆 worker lease 争用、零条压缩冲刷超 30 秒告警。
- 消费侧单批约 10–60 秒（每批 2–4 跳、每跳实测 2.7–20.8 秒）→ 理论 1440–8640 批/天，**余量两到三个数量级**。
- 入队侧本身有硬限速：按龄冲刷每 60 秒最多 2 个会话（`g3ku/agent/memory_agent_runtime.py:168`），满速 2880 会话/天。

结论：**没有实测证据支持"扛住更多会话"改造**。跨会话复核合批 + 按龄发车会引入三个真实风险（压缩边界被拖长、失败放大到整批多会话、一次判多会话的判据质量未验证），去换一个当前不存在的收益，本版不做。

本版做的四件，理由各自独立于吞吐：

1. **归属清晰**：一条处理记录 = 一个会话的一次复核冲刷，或一次主动提交 burst。
2. **一轮 burst 合成一批是结构保证**，不再靠模型自觉把要求塞进一次调用。
3. **收口跳的产出没有任何消费方**，砍掉是纯收益。
4. **合并条数早就在台账里，界面没显示**。

## 2. 结构缺口（逐条带落点）

**A：批次判据不看来源。** `collect_due_batch`（`g3ku/agent/memory_agent_runtime.py:3313-3354`）只收队首连续段，判据是"连续 `pending` + 同 `op` + 累计载荷 ≤ `queue.batch_max_chars`"，**完全不读 `trigger_source`**（行结构见 `:208-227`）。所以主动提交与复核冲刷现在会混批。

**B：攒批窗是固定窗，不是"这轮提交完了没"。** `:3347-3353` 用队首年龄比 `queue.max_wait_seconds`（默认 3，`g3ku/config/schema.py:805`）。一轮里 38 次并行 `memory_update` 边到边落地，被 1 秒节拍（`:2231-2244`）切成两批，内部 agent 多跑一轮并重复付一次常驻块。

**C：收口跳白付。** 循环只在模型不调工具时 break（`:3942-3943`），所以 `memory_apply_confirm` 成功后还要再问一跳拿一句收尾文本。而 `confirm_batch` 只置标志（`:802-820`），真正落盘在下游 `_run_memory_agent_batch` 读 `attempt.session`（`:7100-7112`）；`final_text` 全文只有 `:3904/3940/3947/3976` 四处，**没有任何消费方**。本样本 66 跳里 26 跳是收口跳。

**D：合并条数没上界面。** 台账 `request_count` 随 `_project_processed_row_for_read` 的 `dict(row)`（`:7900-7911`）已经到前端；界面「请求数」显示的是 `usage_counted_requests`（模型发数，`g3ku/web/frontend/memory/view.js:191-195`），两个读数被混为一谈。

## 3. 非目标

- 不做跨会话复核合批、不做按龄发车、不做每批会话数封顶（吞吐档，已评估无必要）。
- 不做节点接入记忆（上下文注入 / `memory_update` 角色放开 / 存量持久化角色覆盖迁移）——另立计划。
- 不做并行写、不做乐观 revision 提交：行号在提交点对 `resident_lines()` 快照解析（`:6643-6647`），并发提交会静默改错行。
- 不做入队侧真合并成一行（`request_id` 改多值会牵动幂等、停车收窄、拆片、重排）。
- 不动纯 adds 批的预览确认门。
- 不改 `batch_max_chars`、`review_interval_turns`、容量梯子、压缩与归并判据、群 LRU 上限。
- 不改 `memory_update` 的函数级 description（"一次调用交任意数量"仍然成立）。

## 4. P0-a：车道分类判据（唯一一份）

新增 `_memory_request_lane(trigger_source) -> str`，与 `collect_due_batch` 同文件：

- `active`：`memory_update_tool`、`admin_ui_edit`、`admin_ui_delete`、`persona_settings`。
- `review`：`ordinary_turn_window`、`stale_buffer`、`token_compression`、`session_boundary`。
- **未知来源 fail-closed 落 `review`**（单行、不合并）。新增入队口时忘了归类，后果是"少合并"而不是"悄悄混批"。
- `op == "consolidate"` 不参与车道分类：它由 op 天然隔离、由 `_queue_consolidation_at_head`（`:5022-5049`）插队首、且同一时刻只允许一条在队。

分批规则：

- **active**：收连续同 op 的 active 行，字符上限 `batch_max_chars` 保留，撞边界立即发车。
- **review**：**恒 1 行**，立即发车，不等攒批窗（它入队时已按窗口拆片）。拆出来的片也不互相合并。
- `processing` 队首恢复分支（`:3318-3327`）语义不变：重启后仍先认领同一头批次。

### 机制级保证

一条 review 队列行永远只与它自己同批；一条 active 行永远不与 review 行同批。判据只有 `_memory_request_lane` 一处，不在消费侧另写第二份来源判断。

## 5. P0-b：主动 burst 空闲封口

active 分支的发车判据从"队首年龄 ≥ 固定窗"改为"**最后一条 active 行年龄 ≥ 空闲阈值**"：

- 空闲阈值**复用 `queue.max_wait_seconds`**（默认 3 秒），不新增配置字段、不做配置迁移。语义变化写进 `config-and-models.md` 该字段说明。
- 硬顶常量 `_ACTIVE_SEAL_MAX_SECONDS = 30`（与 `:162-182` 那批常量同风格）：从队首 `created_at` 起算，到顶强制发车，防连续主动流无限续等。
- 撞字符边界立即发车，不等空闲。

延迟口径（如实）：单条主动提交改前等满 3 秒固定窗、改后等满 3 秒空闲窗，**基本不变**；burst 是"最后一条到达 + 3 秒"，比现在多约 1 秒，换来不被切碎。

## 6. P0-c：主动优先 + 复核公平规则

- **主动插队**：`enqueue_operator_request`（`:6174-6207`）对 active 行改用新 `_insert_queue_request_active`，插到**第一条非 active 行之前**，而不是绝对队首。`consolidate` 行与 `processing` 头永远在它前面。落点参照 `_insert_queue_request_at_head`（`:2737-2743`）。
- **公平规则**：`collect_due_batch` 选题时，若存在等待 ≥ `_REVIEW_FAIRNESS_MAX_WAIT_SECONDS = 600` 的 review 行，本拍优先取那条（单行），即使前面排着 active。常量默认 600 秒，可否决。
- 两条合起来的保证：主动提交不被复核 backlog 堵，复核也不会被连续主动流饿死。

幂等、停车、重排都按 `request_id` 定位，不依赖队列位置，所以插队不影响它们；`capacity_replay_count` 与归并插队首的既有顺序不变。

## 7. P0-d：砍收口跳

落点 `_run_memory_agent_attempt` 循环（`:3905-3972`）：**执行完本跳全部工具调用后**判终态，命中即 break，不再发下一跳。

终态判据（三条，任一命中）：

1. `memory_apply_confirm` 返回 `ok:true`（`status == "batch_confirmed"`，`:820`）。
2. `memory_apply_batch` 返回 `ok:true`（`status == "batch_staged"`，`:1168`）**且 `session.batch_has_mutation()` 为 False**（`:790-800`）——即 `noop_reason` / `already_satisfied` 零变更出口。
3. consolidate 车道 `memory_consolidation_apply` 返回 `ok:true`（单次调用合同）。

**不终态**：`batch_staged` 且 `batch_has_mutation()` 为 True（等确认）、任何 `ok:false`、`preview.status == "unbuildable"`。

落盘不受影响：confirm 只置标志，提交仍在下游按 `attempt.session` 走完整校验与写入顺序。

### 机制级保证

"预览已暂存"绝不被读成"批次已终态"。守卫测试必须覆盖：staged 未确认 → 继续下一跳；confirm 成功 → 终止；noop → 终止；`ok:false` → 继续；同跳里 confirm 与其他调用并存 → 全部执行完再终止。

### 读数口径变化

`usage_counted_requests`（管理页「请求数」）本样本中位 3 → 2，noop 批 2 → 1；本样本总跳数 66 → 40。历史台账与新台账的发数不可直接比较，文档要写明口径而不是写"变少了"。

## 8. P0-e：界面「合并 N 条」

纯前端改动（`request_count` 已在 API 返回体里）：

- `g3ku/web/frontend/memory/view.js:191-195` 旁加一个渲染函数，`request_count > 1` 才显示「合并 N 条」徽标。
- 与「请求数」并列且文案区分：**合并 N 条 = 并入这一批的队列请求条数**；**请求数 = 把 token 交进 usage 账的模型发数**。
- 抽屉里逐条载荷已按 `payload_texts` / `payload_char_counts` 同下标对齐（`:7900-7911`），不需要新增后端字段。

## 9. 验收

### 单测（扩现有文件，不新建平行测试面）

- `tests/resources/test_markdown_memory_runtime.py`：车道分类（含未知来源 fail-closed）、review 恒单行、active 空闲封口、30 秒硬顶、字符边界立即发车。
- `tests/resources/test_memory_worker_loop.py`：主动插队位置（consolidate / processing 头之后）、600 秒公平例外、重启后 processing 头恢复。
- `tests/resources/test_memory_note_confirm_gate.py`：P0-d 五条终态判据守卫。
- `tests/resources/test_memory_failed_parking.py`：active 合批停车仍按本批 `request_ids` 收窄，其余片照常流转。
- `tests/resources/test_memory_management_frontend.py`：`request_count` 透传 + 徽标只在 >1 时渲染。

### 命令

- `python -m ruff check .`
- `python -m pytest tests/resources/test_resource_runtime_smoke.py -q`
- 上述五个 memory 测试文件 + `tests/resources/test_memory_capacity_ladder.py`、`test_memory_consolidation_lane.py`、`test_memory_review_turns_payload.py`
- 本机存在先存环境性失败，基线用 detached worktree 复核后再归因，不用 `git stash`。

### 真机验收（单测不算完成）

重启本机 web（端口 18790，数据根 `D:\NEGI`，优雅 exit + `work/start-web-logged.cmd`），然后：

1. 一轮真对话里让模型发多次 `memory_update` → 队列合成一批、台账一行 `request_count = N`、界面显示「合并 N 条」。
2. 删除一个会话触发 `session_boundary` → 单独一行、`request_count = 1`、不与主动行混批。
3. 从 `.g3ku/memory-requests/` 数工件：noop 批 1 跳、变更批 2 跳（改前基线 2 / 3）。
4. 注入一条 `created_at` 早于 600 秒的 review 行 → 下一拍先处理它。
5. 压缩轮真发生一次 → 确认 `session_agent.py:2700-2705` 的 inline 冲刷与头部版本更新顺序未被破坏（本版不改它，但要回归）。

## 10. 风险

- **终态误判是唯一能造成数据不落地的风险**（P0-d）。守卫测试先于实现写。
- **复核恒单行使批次数略增**：本样本唯一 2 次自然合并都是 `session_boundary` 相撞，分离后 +2 批（+8%）。这是已接受的代价，换归属清晰。
- **主动插队是第二条插队车道**（归并已有第一条），两者顺序必须定死：consolidate → processing 头 → active → review。
- **空闲封口在连续主动流下靠 30 秒硬顶兜底**，硬顶到点发车可能仍切碎一次极大 burst；字符上限同时兜住载荷规模。
- **发数口径变化**影响历史对比，文档按当前契约写，不写时间副词。
- **吞吐墙没拆**：并行写仍被行号合同挡死，会话数上涨时先撞入队限速（记忆晚到、内容不丢），不是队列崩。

## 11. 架构文档（实现后必须走 `negi-architecture-maintenance`）

- `docs/architecture/runtime-overview.md`「Memory Runtime State」运行时合同：改写"专用内部 memory agent 以 FIFO 同-op 批次消费队列"那条为车道分类 + 主动优先 + 公平例外；补收口跳终止判据与发数口径。
- 同文「队列状态机与失败停车语义」：队首语义变化（主动插队、公平例外、consolidate 仍最前）。
- `docs/architecture/web-and-admin.md`「Memory Management Page And Admin Contract」：「合并 N 条」与「请求数」两个读数的区分。
- `docs/architecture/config-and-models.md`：**实际无需改**——该文档不收录 `queue.max_wait_seconds` / `batch_max_chars` 这类记忆队列调参字段，空闲封口语义的归属地是 `runtime-overview.md`「Memory Runtime State」，已在那里写清；按"一个合同一个家"不另开小节。
- `docs/architecture/README.md`：预计不需要改（无新主题、无边界移动、无阅读顺序变化）；实现后按 skill 清单复核。
- 就地改写、不追加附录；跑 `python scripts/check_architecture_docs.py`。

## 12. 交接段

- 本计划只覆盖记忆车道四件；**节点接入记忆另立计划**，硬约束是"节点只主动提交、不接复核窗口"（控量形状），且要处理存量持久化 `allowed_roles` 覆盖新默认表的问题（`main/governance/resource_registry.py:96-103`）。
- 冻结标识符不得改：`g3ku` 包名、`G3KU_*` 环境变量、`.g3ku` 数据根、上下文协议标记、`negi_*` 工具名。
- 提交信息用英文；提交身份用 `-c` 临时指定历史作者，不改 git config；共享工作树只 add 自己的文件。
- 每完成一个 P0 子项即提交，不中途停；长任务按批 commit。
- 若上下文耗尽：先更新本文件的状态行与已完成子项，再开新会话续跑。

## 13. 本机验收实测（2026-11-24，端口 18790，新 worker `worker:b8e284269a8a`）

脚本：`work/_live_memory_acceptance.py`、`work/_live_memory_turn_acceptance.py`；结果：同名 `*_result.json` / `*_turn.json`。

| 场景 | 台账读数 | 结论 |
| --- | --- | --- |
| 管理页连发 3 条编辑（真实 HTTP，正文原样不改） | 1 批，`request_count=3`，`trigger_sources=[admin_ui_edit]`，**1 跳**，noop | 主动合批 + 零变更单跳成立；三条正文比对未变，常驻仍 45 条 |
| 真对话让模型记一条临时条目（WS `/api/ws/ceo`） | 1 批，`memory_update_tool`，`request_count=1`，**2 跳**，changes=1 | 变更批由基线 3 跳降为 2 跳 |
| 删除该临时会话 | 1 批，`session_boundary`，`request_count=1`，**1 跳**，noop | 复核恒单行成立，未与主动行混批 |
| 管理页删除该临时条目 | 1 批，`admin_ui_delete`，op=delete，**2 跳**，changes=1 | 删除车道同样省掉收口跳 |

收尾：临时标记 `ML-ACCEPT-20261124` 已不在常驻块，常驻回到 45 条，活动会话恢复为 `web:ceo-739a31050bf8`，验收用的两个临时会话均已删除。

浏览器实测（Chrome DevTools，真实页面非 VM）：已处理列表渲染出 `合并 3 条` 徽标（历史两条 `request_count=2` 的旧记录也正确显示 `合并 2 条`）；点开该批抽屉，`请求数 1` 与 `合并条数 3 条队列请求` 并列且互不冒充。

测试与 lint：`ruff check .` 全通过；`test_markdown_memory_runtime.py` 105 通过、`test_memory_note_confirm_gate.py` 12 通过、其余记忆子集 + smoke 198 通过。`test_memory_management_frontend.py::test_memory_management_view_keeps_admin_mutations_hidden_by_default` 失败，已在 detached worktree 基线复核为**先存失败**，与本次改动无关。

未做实机强制的两项，如实记明：
- **公平例外（600 秒）**：由单测覆盖（含归并/processing 头不被顶掉的顺序守卫），实机未构造背压场景。
- **压缩轮 inline 冲刷与头部版本更新顺序**：本次未改该路径，相关记忆测试子集全通过；未在实机强制触发一次真实压缩。