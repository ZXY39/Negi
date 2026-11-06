# 连续（待命）节点：实机验收交接

交给在**生产设备**上执行验收的 agent。本文件自包含，不需要读取其他对话。
你的任务是按用例执行、采集证据、如实回报；**不要修改代码**；发现失败时保留现场并回报，不要自行修复。

## 0. 一句话

连续节点（任务根或派生子节点）不进终态、靠 `enter_wait(frontdoor)` 长期待命、任意消息/事件唤醒并接续上下文；它的 `submit_final_result` 只是"结束请求"，必须经验收 `pass` 才终态。本次验收确认该链路在真机上成立，且不影响一次性任务。

## 1. 版本基线

生产设备代码必须包含以下 7 个提交（`git log --oneline` 核对，顺序不限，必须全部在场）：

```
b267218b docs(plan): add continuous node implementation plan
995404aa feat(runtime): mark continuous nodes and force their acceptance
75afbc10 feat(spawn): report auto-created acceptance for continuous children
8af1f385 feat(node): give continuous nodes their own turn exit contract
5c55b413 feat(stall): treat declared external waits as designed idle
b7cc052e feat(ui): show continuous nodes and idle tasks as standby
334e9461 docs(architecture): document continuous standby nodes
```

若设备上没有这些提交：**停止验收**，回报"基线不满足"。不要自行 cherry-pick 或合并。

可选的前置自检（不影响验收结论，但能提前排除环境问题）：

```bash
python -m ruff check .
python -m pytest tests/test_continuous_nodes.py -q
python -m pytest tests/resources/test_resource_runtime_smoke.py -q
```

## 2. 验收范围

**要验**

1. 连续根任务创建后进入待命，任务卡与任务树显示「待命」。
2. 任意来源消息唤醒节点，上一轮上下文接续（不是从 bootstrap 重开）。
3. 提交是"结束请求"：验收 `pass` 才终态；`reject` 则节点复活继续待命。
4. 连续子节点不写 `acceptance_prompt` 时自动获得验收节点，spawn 回执带 `acceptance_auto_created` 说明。
5. 连续子节点存活期间父节点不能提交终态。
6. 重启后待命任务不重跑，下一条消息仍能唤醒。
7. 暂停语义不变：暂停节点的消息只落箱、不唤醒。
8. 一次性任务行为不变（回归）。

**不验 / 不要求**

- 20 分钟真实失速窗口（常量写死，建议不实等；用单测与本报告的替代读法）。
- 终态任务复活、归档状态——本次**没有**这些能力，也没有计划。
- 任意终态子节点的独立复活——设计上只开根节点连续；子节点用新的 spawn 轮表达。
- 外部事件订阅唤醒可选，不算金路径。

## 3. 环境与前置

- 生产设备上 Negi 的启动方式以该设备自身为准（`negi` / `start-negi.*` / 服务管理器）。
  Web 默认端口 `18790`（以设备配置为准）。
- 数据根解析：仓库下 `.g3ku/data-root.json` 的 `data_dir`（没有该文件时数据根 = 安装根）。
  运行库路径 = 数据根 + config `mainRuntime.storePath`（默认 `.g3ku/main-runtime/runtime.sqlite3`）。
  例：`D:\NEGI\.g3ku\main-runtime\runtime.sqlite3`。
- 日志：
  - web 进程：`<仓库>/.g3ku/logs/console.log`
  - 托管 worker：`<数据根>/.g3ku/main-runtime/managed-worker.log`
- 确认模型链可用（随便发一句普通对话有回复），并确认磁盘余量充足。
- **重启会打断设备上正在跑的会话/任务**（优雅停机暂停、启动自动恢复）。执行前必须确认操作员知情；有重要任务在跑时先等待或改期。

## 4. 重启（加载新代码）

按该设备的优雅退出协议执行（示例，端口按实际替换）：

```bash
curl -s -X POST http://127.0.0.1:18790/api/bootstrap/exit
```

等待进程退出后，用设备正常的启动方式拉起。确认：

```bash
curl -s "http://127.0.0.1:18790/api/tasks?session_id=all&scope=1" | head -c 300
```

返回 `"ok":true` 且 `GET /api/tasks/worker-status` 显示 worker 在线。若有会话/任务因停机被自动恢复，等它们静止后再开始用例（否则读数和截图会被恢复回合污染）。

记下重启时刻，报告里带上。

## 5. 用例

每个用例记录：**通过/失败**、关键截图、API 原文、SQL 读数、日志片段、任务 id / 节点 id。
操作入口优先用网页；模型侧动作（建任务、发消息）通过 CEO 前门对话完成。

### T1 连续根任务进入待命（金路径）

1. 在 CEO 前门发送（按设备路径替换；**明确要求 `continuous=true`**）：

   > 用 create_async_task 创建一个连续任务，`continuous=true`。任务内容：每收到一条消息就检查 `<某个真实存在的目录>` 下是否有新文件，并汇报变化；没有消息时保持待命。只有我明确说“结束任务”时才提交最终结果。

2. 等待根节点跑完第一个回合（通常 1–3 分钟）。它应当调用 `enter_wait` 而不是 `submit_final_result`。

期望（全部满足）：

- 任务大厅卡片状态胶囊显示**浅黄色「待命」**；筛选菜单有「待命」项且能筛出该任务。
- 任务树根节点显示「待命」（不是「运行中」/「等待（显式）」）。
- 数据库只读核对：

```sql
-- 根节点连续标记
SELECT node_id, status, json_extract(payload_json,'$.metadata.continuous') AS continuous
FROM nodes WHERE task_id = '<task_id>';

-- 帧相位
SELECT node_id, phase, active, runnable, waiting
FROM task_runtime_frames WHERE task_id = '<task_id>';

-- 外部唤醒声明
SELECT node_id,
       json_extract(payload_json,'$.metadata.node_wait_external') AS wait_external,
       json_extract(payload_json,'$.metadata.node_wait_targets') AS wait_targets
FROM nodes WHERE task_id = '<task_id>';

-- 最终验收节点与自动条款
SELECT node_id, node_kind,
       json_extract(payload_json,'$.metadata.acceptance_prompt_template') AS template
FROM nodes WHERE task_id = '<task_id>' AND node_kind = 'acceptance';
```

- 根节点 `continuous=1`、`phase='waiting_explicit'`、`wait_external=1`。
- 存在一颗 `node_kind='acceptance'` 的最终验收节点，`template` 含「被检验节点是连续型节点」。
- `GET /api/tasks?session_id=all&scope=1` 里该 task 的 item `standby` 为 `true`，`status` 仍为 `in_progress`。
- `GET /api/tasks/<task_id>/tree-snapshot` 里根节点带 `continuous:true`、`wait_external:true`。

证据：卡片与树截图各一张、上述 SQL 输出、该 item 的 API 原文。

### T2 消息唤醒 + 上下文接续

1. 给该任务根节点发一条**只有上一轮才知道答案**的消息，例如：

   > 你在第一个回合里看到的目录路径是什么？用一句话回答后继续待命。

   入口：任务详情里的节点消息框，或前门让它 `send_message` 给根节点。

2. 观察节点被唤醒并开回合（通常 1–3 分钟），回答后再次进入 `waiting_explicit`。

期望：

- 回答里出现 T1 回合中的真实目录（证明接续，不是从 bootstrap 重开）。
- 节点详情/上下文里能看到**上一轮的消息 + 新消息**同时存在；不是只剩系统提示 + 一条新用户消息的两条 bootstrap。
- 唤醒后卡片短暂离开「待命」，回到回合结束后再次显示「待命」。
- 没有 `task_stall` / `task_node_error` 事件。

失败签名：模型反问"什么目录"、或上下文只剩引导 JSON → 记录节点 id、`task_runtime_messages` artifact 首尾、`task_model_calls.request_seed_source`，按失败回报。

### T3 结束请求 → 验收 pass → 终态

1. 发送：**「结束任务」**。
2. 期望链路：根节点调用 `submit_final_result` → 节点转 `waiting_acceptance` → 最终验收节点开回合 → `pass` → 根节点 `success` → 任务 `success`。

核对：

```sql
SELECT node_id, node_kind, status FROM nodes WHERE task_id = '<task_id>';
SELECT status, json_extract(payload_json,'$.finished_at') AS finished_at
FROM tasks WHERE task_id = '<task_id>';
```

期望：根与验收节点均 `success`，任务 `success`、`finished_at` 非空；任务卡恢复「成功」胶囊（不再「待命」）。
证据：任务卡截图、`task_terminal` 事件或会话里的完成回复。

### T4 结束被验收拒绝 → 节点复活继续待命（确定性做法）

为避免验收"宽松放行"导致测不出来，T4 单独建一个连续任务，验收提示词写一个**可机械判定**的条件：

> 用 create_async_task 创建连续任务，`continuous=true`，`final_acceptance_prompt="只有当 <设备上某个绝对路径>/acceptance-proof.txt 存在且内容包含 DONE 时才通过"`。任务：收到消息就汇报状态，否则待命。

1. 不创建该文件，发送「结束任务」。期望：验收 `reject`，根节点被复活并继续待命，反馈里要求补齐条件。
2. 手工创建 `acceptance-proof.txt`（内容含 `DONE`），再次发送「结束任务」。期望：验收 `pass`，任务成功。

核对：第一次结束后根节点仍 `in_progress`、任务仍 `in_progress`（卡片回到「待命」）；第二次结束后两者 `success`。
证据：两次的验收节点 verdict、根节点状态、任务状态、反馈正文。

### T5 连续子节点自动验收 + 回执说明

1. 新建一个普通任务，明确要求根节点派生一个连续子节点：

   > 用 spawn_child_nodes 派生一个子节点，`continuous=true`，不要写 `acceptance_prompt`。子节点职责：收到消息就汇报状态，否则待命。

2. 期望：
   - 子节点 `continuous=1`，且自动出现一颗 `accept:<子节点 goal>` 的验收节点，模板含连续条款。
   - 父节点这次 `spawn_child_nodes` 的工具结果（节点详情的工具调用输出）里，该子节点条目带 `acceptance_auto_created: true` 与 `acceptance_note`（含验收节点 id），而不是让父节点以为漏写了判据。

核对：

```sql
SELECT node_id, status,
       json_extract(payload_json,'$.metadata.continuous') AS continuous,
       json_extract(payload_json,'$.metadata.spawn_owner_kind') AS owner_kind
FROM nodes WHERE task_id = '<task_id>';

SELECT node_id,
       json_extract(payload_json,'$.metadata.spawn_operations') AS ops
FROM nodes WHERE node_id = '<parent_node_id>';
```

在 `ops` 对应 entry 里应能看到 `acceptance_auto_created: true` 与 `acceptance_node_id`。
证据：父节点工具输出截图或原文、SQL 输出。

### T6 连续子节点拖住父节点

在 T5 的任务里，给父节点发消息要求"现在提交最终结果"。期望：提交被打回，理由是仍有非终态子节点（连续子节点还活着），父节点不得终态；先结束连续子节点后才可提交。

证据：父节点那一轮的工具结果/错误正文。

### T7 重启后待命不重跑

1. 让 T1 的连续任务处于待命，记录当前 `task_model_calls` 行数：

```sql
SELECT COUNT(*) FROM task_model_calls WHERE task_id = '<task_id>';
```

2. 优雅重启（同 §4），等恢复静止。
3. 期望：任务仍 `in_progress` + `waiting_explicit`；`task_model_calls` 行数**不增加**（重启没有给停靠根白跑回合）；卡片仍「待命」。
4. 再发一条消息，仍能唤醒并续跑。

证据：重启前后行数、帧相位、卡片截图、唤醒后的回复。

### T8 暂停语义不变（A5=A）

1. 对 T1 的连续任务执行任务级暂停（或暂停根节点）。
2. 发一条消息。期望：消息落箱、未读数增加，节点**不被唤醒**、不产生模型调用。
3. 恢复（`resume_task` / 节点 resume）。期望：消息被消费，节点开回合。

证据：暂停期间 `task_model_calls` 行数不变、未读计数；恢复后的调用与回复。

### T9 停滞豁免（可选用例）

- **推荐做法（不实等）**：T1 任务待命期间，在 `task_events` 里查该任务是否出现 `task_stall`：

```sql
SELECT created_at, event_type FROM task_events
WHERE task_id = '<task_id>' AND event_type = 'task_stall'
ORDER BY created_at DESC LIMIT 5;
```

  期望：待命期间没有新的 `task_stall` 行。
- **可选实测**：让任务空闲 21–25 分钟，确认期间无 `task_stall` 事件、会话没收到"任务可能停滞"的提醒。阈值写死在 `main/service/task_stall_notifier.py`，不可配置。
- 若跳过实测，在报告里注明"依据单测，未实等"。

### T10 显示态

截图：任务大厅卡片「待命」胶囊（浅黄）、筛选菜单「待命」项生效、任务树节点「待命」、唤醒后徽标变回「运行」、任务结束后变回「成功/失败」。
期望：没有把普通等待节点误标成「待命」——只有连续节点 + 声明了外部唤醒源才显示待命。

### T11 纯文本出口（尽力而为）

唤醒连续节点后，要求它"这一轮只回复一句纯文本、不要调用工具，然后继续待命"。期望：不出现 `failed + blocked` 错误暂停；若模型真的只回文本，下一轮应能看到含 `enter_wait` 的修复提醒并最终停靠。

如果模型总是主动调用工具，记录实际行为并标"未触发"，不算失败。

### T12 一次性任务回归

创建一个普通任务（`continuous` 不设置）：完成、验收、终态流程与改动前一致；不出现自动验收节点（除非写了 `final_acceptance_prompt`）、不显示「待命」。
证据：任务状态与卡片截图。

## 6. 证据采集清单

每个用例至少给：

1. 任务 id / 节点 id / 触发消息原文。
2. 任务卡与任务树截图（关键状态）。
3. `GET /api/tasks?session_id=all&scope=1` 中该 task 的 item JSON。
4. `GET /api/tasks/<id>/tree-snapshot` 中相关节点 JSON（可截取相关键）。
5. 关键 SQLite 只读查询结果（见各用例 SQL；连接用 `file:...?mode=ro`）。
6. 相关时间窗口的日志片段：
   - `<仓库>/.g3ku/logs/console.log`
   - `<数据根>/.g3ku/main-runtime/managed-worker.log`
7. 若失败：失败时刻、复现步骤、最后一次模型请求的 `task_model_calls` 行、节点错误历史（`task_error_logs`）。

SQLite 只读连接示例（PowerShell / bash 均可，按设备路径替换）：

```bash
python - <<'PY'
import sqlite3
p = r'D:\NEGI\.g3ku\main-runtime\runtime.sqlite3'
con = sqlite3.connect(f'file:{p}?mode=ro', uri=True)
print(con.execute("SELECT task_id,status FROM tasks ORDER BY updated_at DESC LIMIT 10").fetchall())
PY
```

## 7. 失败分诊（先看这几条）

| 症状 | 先查 |
|---|---|
| 卡片一直「运行」不待命 | 根节点 `metadata.continuous` 是否为真；帧 `phase` 是否 `waiting_explicit`；`node_wait_external` 是否写入；`/api/tasks` item 的 `standby` |
| 待命任务仍收到失速提醒 | `task_events` 是否有 `task_stall`；该任务是否还有未读、暂停节点或验收回合在飞；`node_phases` 是否真的全员停靠 |
| 提交后直接终态、没过验收 | 是否真的建了验收节点；`metadata.acceptance_handshake.state`；是否连续根强制 `final_acceptance.required` |
| 同一子节点出现两颗验收节点 | 自动条款是否确定性写入 `acceptance_prompt_template`；spawn 是否被重放 |
| spawn 回执没有 `acceptance_auto_created` | 子节点 spec 是否真的 `continuous=true`；entry 是否落账 |
| 唤醒后像从零开始 | 帧是否被误删；`task_runtime_messages` artifact 是否还在；`task_model_calls.request_seed_source` |
| 重启后停靠根白跑一轮 | `_run_node_turn` 前的任务终态/停靠判断；worker 日志 `root turn deferred` |
| 普通任务行为变了 | 非连续节点不应出现 `run_mode`、连续条款、自动验收；对照 T12 |

日志关键词：`root turn deferred`、`node resume triage`、`node wake timer sweep`、`acceptance`、`task_stall`、`spawn_round`。

## 8. 回滚

代码改动全部是增量：新增 metadata 键（`continuous`、`node_wait_external` 既有）、新增可选字段（`standby`、`acceptance_auto_created`、`continuous`/`wait_external`）、新增 stall 原因字符串；没有数据库迁移。

回滚方式：逆向 revert §1 的 6 个代码/文档提交（保留计划与验收文件也可），重启进程即可。回滚后已创建的连续任务按普通节点处理（它仍有验收节点，提交照常）；不会遗留无法解析的数据。

## 9. 回报模板

```
结论：通过 / 部分通过 / 失败
基线：git HEAD=<sha>；重启时刻=<time>；端口=<port>；数据根=<path>
环境自检：ruff=<pass/fail>；smoke=<pass/fail>

T1 连续根待命：pass/fail  | 证据：<截图/JSON/SQL 路径> | 任务 id=<id>
T2 唤醒续跑：pass/fail    | 证据：… | 备注：<回答是否引用旧上下文>
T3 验收 pass：pass/fail   | 证据：…
T4 验收 reject：pass/fail | 证据：…
T5 自动验收+回执：pass/fail | 证据：…
T6 父节点被拦：pass/fail  | 证据：…
T7 重启不重跑：pass/fail  | 证据：… | 调用行数 before/after=<n>/<n>
T8 暂停语义：pass/fail    | 证据：…
T9 停滞豁免：pass/fail/跳过 | 依据：…
T10 显示态：pass/fail     | 证据：…
T11 纯文本出口：pass/fail/未触发 | 备注：…
T12 一次性回归：pass/fail | 证据：…

异常与现场：
- <现象、时间、任务/节点 id、日志片段、SQL 读数>

模型用量：<该任务 token 合计，若可从 Token 统计读到>
```

## 10. 已知边界（不要当失败报）

- 没有归档状态、没有终态复活：终态仍是最终态。
- 子节点终止后不能被"通知唤醒"：只有根节点连续；子节点由父节点的新 spawn 轮表达。对终态子节点发消息会被 `target_terminal` 拒收，这是设计。
- 验收拒收没有次数上限，连续节点反复提交会被反复打回——这属于模型行为，不是运行时缺陷。
- 停滞豁免只在「全员停靠 + 至少一个外部唤醒源 + 无暂停 + 无未读 + 验收不在飞」时成立；只等任务内节点的停靠仍会报失速。
