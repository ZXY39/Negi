# enter_wait 等待声明与死等待消除

## 目标

消除节点间的「死等待」：A 停靠在等 B，但 B 已经终态、已被判停，或 B 也在等 A。
今天这两种形态都不被任何机制处理——看门狗明确豁免停靠相位，环检测只覆盖验收握手，
只能等 20 分钟后的失速告警。

## 不改什么

- **不改「任意消息唤醒」这条**。`wake_ready` 仍是"有任意可注入未读就醒"，
  `waiting_for` 只用于建边与反查，不参与唤醒判定。任何消息仍然能叫醒任何停靠节点。
- **不改停靠的持久判据**。`waiting_explicit` 相位仍是唯一判据，新字段与它同帧写入。
- **不新增 message kind**。运行时以 `origin='frontdoor'` 投递即得
  `KIND_FRONTDOOR_NOTICE` + `priority=high`（`node_message_kind.py:60-62`），
  无条件唤醒，与 `_notify_wait_cycle` 同一档。

## 阶段一：声明与持久化

**参数**：`enter_wait(note='', waiting_for=None)`

`waiting_for` 取值：
- 省略 / 空 → 不声明，行为与今天逐字一致（不建边、不被反查）
- `frontdoor` → 等操作员/用户。**不建边**（外部活性来源，合法的无限等待）
- `parent` → 解析成 `parent_node_id`；根节点无父，按 `frontdoor` 处理
- 节点 id 或 id 列表 → 解析成本任务内的节点

**落点**：`main/runtime/node_runner.py:2933` `_enter_wait`
1. 解析 `waiting_for`，逐目标校验：
   - 不在本任务 → `wait_target_not_in_task`
   - 已是终态 → `wait_target_terminal`
   - 不能给我发消息（`node_relationships` 判据）→ `wait_target_not_messageable`
2. 任一目标不合法 → **拒绝停靠**，返回
   `{'status': 'not_parked_invalid_targets', 'problems': [...]}`，回合继续。
   这把一类死锁从"挂 20 分钟"变成"工具直接报错"。
3. 合法 → 与现有 `node_wait_note` 一起，把解析后的 id 列表写进**帧**的
   `wait_targets`（`node_runner.py:2966-2970` 的 `update_frame` 处）。
   `react_loop.py:1939-1954` 的批后帧写用 `{**frame, ...}`，新字段自动保留。

**时机**：`_enter_wait` 已有先读未读再决定停靠的判据，校验插在它之后、写帧之前。

## 阶段二：等待边

**落点**：`main/runtime/wait_graph.py`

1. `WaitNode` 增加 `wait_targets: tuple[str, ...]`，由 `build_wait_nodes` 从
   `frame.get('wait_targets')` 读出（该函数已经拿得到 `frames`）。
2. `_wait_edges`（`:81-106`）增加一类边：相位为 `waiting_explicit` 且声明了目标时，
   对每个目标建边，**仅当目标非终态且处于停靠相位**。
   - 目标是运行中 → 不建边（它在推进，成不了环）
   - 目标已终态 → 不建边（由阶段三的定向唤醒负责）

**为什么这样不会误报**：环只在全员停靠时成立；`_break_wait_cycles` 只在
"没有任何可运行节点"那一拍扫描（`task_actor_service.py:1501-1503`），
此时强制唤醒最浅节点是正确的处置。声明为 `frontdoor` 的停靠不产生边，
所以"等操作员"永远不会被判成环。

**打破侧不改**：Tarjan、按深度取最浅、强制唤醒、frontdoor 破环通知全部复用。

## 阶段三：对象消失时的定向唤醒

**新增** `MainRuntimeService.notify_node_waiters(task_id, gone_node_id, *, kind, detail)`：
1. 列出任务内所有节点，读各自帧的 `wait_targets`
2. 命中 `gone_node_id` 的停靠节点各投一条 `origin='frontdoor'` 的消息
3. 正文把 `node_wait_note` 回显与消失原因绑在一起：

   ```
   你之前在等：<note>
   节点「<goal>」<已终态：<summary>|已被暂停：<reason>>
   你要等的东西不会再来了。请根据当前事实决定继续推进、换个方式推进还是结束这条分支。
   ```

**挂点**（两个，各覆盖一类事件）：
- 终态：`runtime_service.py:5223` `_deliver_child_final_message`，父节点投递之后
- 判停：`runtime_service.py:5435` `_deliver_child_abnormal_pause`，父节点投递之后

这两个是 `child_final_sink` / `abnormal_pause_sink` 的唯一实现
（`runtime_service.py:866-868`），覆盖全部终态与全部异常暂停写入方。

**为什么不需要去重表**：正文带节点 id 与原因，同一事件重投时指纹相同，
`node_mailbox` 的"未处理期间去重"会吞掉；节点消费过之后再重投才是新意图，
而这正是想要的行为（它可能已重新停靠）。

## 阶段四：提示词与文档

- `main/prompts/node_execution.md`：停靠指引补 `waiting_for`，说明三种取值、
  "等人的用 frontdoor"、"等所有子节点用 wait_all_children 不要用 enter_wait"
- `docs/architecture/main-task-runtime.md`：
  - §1 第 17 条「`enter_wait` 只有 `note`」→ 改为声明式停靠，并说明边与反查
  - §1 第 23 条工具参数清单同步
  - 新增一条说明两类死等待各自由谁处理，以及 `frontdoor` 的豁免语义

## 验收

1. **单元测试**（`tests/`）：
   - `waiting_for` 解析：合法 id / `parent` / `frontdoor` / 非法目标四档
   - 目标已终态 → 拒绝停靠，返回 `not_parked_invalid_targets`
   - `_wait_edges`：互等成环、单向等待不成环、`frontdoor` 不建边、目标运行中不建边
   - `notify_node_waiters`：命中与不命中、正文含 note 回显
2. **回归**：`python -m pytest tests/test_node_enter_wait.py tests/resources/test_resource_runtime_smoke.py -q`
3. **风格**：`python -m ruff check .`
4. **真机验收**（不做单测替代）：起服务，构造两个兄弟节点互等，确认环检测在
   同一拍内强制唤醒且任务继续；构造 A 等 B、B 自行终态，确认 A 立即收到通知。

## 风险

- **模型不填**：退回今天的行为，严格增量，无回归。
- **填了不可发的目标**：停靠时直接拒绝，不会产生新的悬挂。
- **帧字段陈旧**：`wait_targets` 只对停靠相位生效，节点醒来跑新回合时相位已变，
  不产生幻影边；重新停靠时 `_enter_wait` 总是覆写该字段（未声明写空列表）。
- **唤醒风暴**：定向唤醒只发给"声明在等这个节点"的节点，不是广播；
  等操作员的节点不声明内部 id，不会被扫到。

## 交接

同批次未改动：验收握手环检测逻辑、看门狗判停判据、失速通知器。
若后续要给 `waiting_children`（`wait_all_children`）也加边，需要另议——
那条路径由子节点终局自然解开，不属于死等待。
