# 连续（待命）节点：实施计划

基线：`main`（`ef7744b8`）。

## 前置闸门（动手前必须处理）

工作树里有**他人/前序会话的在建改动**，且与本计划落点重叠：

```
M main/monitoring/log_service.py            (+25  externalize_acceptance_external_event_list)
M main/runtime/node_runner.py               (+167 验收尾块的外部订阅事件摘要)
M tests/test_acceptance_pass_unseen_notices.py
M tests/test_node_acceptance_scheduler.py
```

约定：

1. **不 stash**（共享工作树禁用）。先确认这批改动归属：若属于其他在跑会话，等它提交后再开工；若确认是遗留且可提交，按作者身份 `-c user.name=ZXY39 -c user.email=1045273950@qq.com` 单独提交，不与本计划混提。
2. 本计划只 `git add` 自己新增/修改的文件。
3. `node_runner.py` 的在建改动位于 7096–7300 区间，本计划要动的 `_compose_acceptance_prompt`(7338)、`create_acceptance_node`(6847) 紧随其后——**所有行号在那批改动落地后必须重新定位**，本计划里的行号一律当锚点提示，不当绝对坐标。
4. `work/` 下其余未跟踪文件属于其他工作，本计划只新增自身这一份。

## 0. 目标与非目标

### 目标

1. 节点可以被标记为**连续节点**：职责是长期待命、按消息/事件响应，而不是一次性交付。
2. 连续节点**不能自己终态化**：它的 `submit_final_result` 必须经验收裁决，`pass` 才落 `success`，`reject` 则复活继续待命。
3. 创建连续节点时**不要求模型写验收标准**：系统自动分配验收节点，并注入连续语义的验收条款；模型自己写了 `acceptance_prompt` 时，条款叠加在其上。
4. 父节点的 `spawn_child_nodes` 回执明确告知「因为是连续节点，已自动创建验收节点」，避免父节点困惑。
5. 连续节点空闲时**不被误判**：不报任务失速、不被孤儿收尸、不被等待图强制点火、纯文本回复不转错误暂停。
6. 界面能区分「待命」与「运行」。

### 非目标

- **不做归档状态**，不改任务终态清理（`_run_terminal_intermediate_cleanup` 保持现状）。
- **不做终态复活 / reopen**：终态仍是最终态，所有终态闸门（投递 `target_terminal`、resume `node_terminal` / `task_terminal`、`_run_node_turn` 短路）不动。
- **不摘除 `submit_final_result`**：摘除会让根节点失去唯一成功出口（`_derive_task_status` 只认根），也会让验收失去可核验载荷（`finalize_inspected_execution_node` 依赖 `result_payload`）。
- **不加 `end_authorized` 授权门**：本轮只做「必过验收」；若日后需要「必须父节点/用户点头」，再叠加，两者不冲突。
- 不改「任意来源消息都能唤醒停靠节点」这条判据（`_delivery_wakes_target`）。
- 不改暂停语义（A5=A：暂停节点消息落箱不唤醒）。
- 不新增外部事件订阅机制（复用现有 `event_subscription` 工具与节点信箱投递）。

## 1. 现状与复用落点

| 位置 | 可复用事实 |
|---|---|
| `main/models.py:138-145` | `SpawnChildSpec` 已有 `acceptance_prompt` / `requires_acceptance`，新增 `continuous` 是同形扩展 |
| `main/runtime/node_runner.py:225-231` | `_spec_requires_acceptance` 是「要不要建验收节点」的唯一判据 |
| `main/runtime/node_runner.py:6464-6473` | spawn 物化时按该判据调 `_ensure_spawn_acceptance_node`，验收节点与子节点同轮创建、绑定 entry、不派发 |
| `main/runtime/node_runner.py:6476-6536` | `_ensure_spawn_acceptance_node` 已含复用/收养/新建三档，新建走 `create_acceptance_node` |
| `main/runtime/node_runner.py:6847-6892` | `create_acceptance_node` 在创建时定稿 bootstrap，并把 `acceptance_prompt` 写进 `acceptance_prompt_template` 与恢复指纹 |
| `main/runtime/node_runner.py:7338-7355` | `_compose_acceptance_prompt` 是验收提示词的唯一合成点 |
| `main/runtime/node_runner.py:221-222` | `_spawn_acceptance_goal` = `accept:` + goal；goal 参与树渲染与恢复指纹 |
| `main/runtime/node_runner.py:5774-5847` | `_spawn_creation_receipts` 是创建回执的唯一构造点，**也是重放路径的唯一输出** |
| `main/models.py:158-178` | `SpawnChildResult` 已带 `child_node_id` / `acceptance_node_id` / `state` / `gate`，新字段是增量 |
| `main/runtime/internal_tools.py:545-623` | `spawn_child_nodes` 的 description / model_description / child_schema 三处都要同步 |
| `main/service/runtime_service.py:1997-2035` | `create_task` 已在 `final_acceptance.required` 且无 node_id 时建最终验收节点 |
| `main/runtime/task_actor_service.py:2563-2656` | `_run_final_acceptance_if_needed` + `_get_or_create_final_acceptance_node`：根提交后必过验收，终态验收节点会被作废重验 |
| `main/runtime/node_runner.py:7303-7329` | `finalize_inspected_execution_node`：只有验收 `pass` 才给执行节点签 `success` |
| `main/runtime/react_loop.py:1698-1711` | `_final_submission_blockers`：父节点在有非终态子节点时不能提交——连续子节点天然拖住父节点，无需新逻辑 |
| `main/runtime/wait_graph.py:149-200` | `find_all_parked_waits`：任一停靠节点声明 `wait_external` 即判「不是 stuck」，不强制点火 |
| `main/runtime/node_relationships.py:118-176` | `resolve_wait_targets`：`frontdoor` → `external=True`；根节点写 `parent` 同样解析成 external |
| `main/runtime/task_actor_service.py:2144-2146` | 孤儿收尸器已跳过 `waiting_explicit` / `waiting_acceptance` |
| `main/runtime/node_watchdog.py:35,123` | 看门狗已排除停靠相位 |
| `main/service/runtime_service.py:6764-6789` | `classify_task_stall_reason` **不看停靠语义**——这是唯一会误报空闲连续任务的地方 |
| `main/runtime/plain_text_reply.py` + `react_loop.py:2130-2147` | 纯文本打回给模型的合法出口写的是「继续调工具或 `submit_final_result`」，3 次 strike 转 `failed+blocked` 错误暂停 |
| `main/runtime/node_runner.py:3224-3259` | `_build_messages` 的 bootstrap user JSON 是节点级稳定事实的唯一落点 |
| `g3ku/web/frontend/org_graph_app.js:9686` / `org_graph_tasks.js:7-21` / `org_graph.css:645-691` | 任务状态派生、标签映射、徽标配色三处 |

## 2. 目标语义

### 2.1 单一真值：节点级 `continuous`

- 落在 `node.metadata.continuous: bool`，**创建时写定，之后不可变**（可变会让 `tools[]`、验收要求、恢复指纹同时漂移）。
- 子节点：`spawn_child_nodes` 的 child spec 新增 `continuous`（默认 `false`）。
- 根节点：`create_async_task_cn` 新增 `continuous`（默认 `false`），写进根节点 metadata；任务级不另存一份，显示态从节点派生。
- 验收节点**永不**连续：`create_acceptance_node` 不继承该标记。

### 2.2 连续 ⇒ 必过验收

- `_spec_requires_acceptance(spec)`：`spec.continuous` 为真时**无条件返回 True**，显式 `requires_acceptance=false` 被覆盖（覆盖事实写进回执）。
- 根节点连续 ⇒ 建任务时强制 `final_acceptance.required=true`，`prompt` 为空则填连续条款。
- 自动分配的验收提示词 = 连续条款常量 + 模型自己写的 `acceptance_prompt`（若有）。条款必须是**确定性常量**（不含时间戳/节点 id），因为它进 `acceptance_prompt_template` 与恢复指纹；不确定会让崩溃重放建出第二颗验收节点。

连续条款草稿（实现时定稿，进 `main/prompts/` 或 `node_runner` 常量，二选一并在文档里指明唯一落点）：

```
被检验节点是连续型节点：它的职责是长期待命并按消息/事件响应，不是一次性交付。
判定口径：
- 只有当它明确声明职责已结束、且给出的收尾交付可核验时，裁 pass。
- 若它只是阶段性汇报、或仍在等待外部消息/事件，裁 reject，并在 feedback 里要求它继续待命。
- 首轮不要用 reject_no_progress（该档首轮禁用）。
```

### 2.3 回合出口

连续节点的**正常收尾是 `enter_wait(waiting_for=["frontdoor"])`**，不是提交。要求写死 `frontdoor` 的理由不是唤醒判据（任意来源都能唤醒），而是：

- `wait_external` 是等待图判「不是 stuck」的依据，空声明会被读成无人唤醒；
- 停滞豁免（阶段四）就以这个标记为判据。

只有收到明确结束指令时才 `submit_final_result`，随后走验收。

## 3. 阶段

每阶段可独立提交、独立验收。

### 阶段一：连续标记 + 验收强制（后端）

**落点**

- `main/models.py`：`SpawnChildSpec.continuous: bool = False`
- `main/runtime/internal_tools.py:580-623`：child_schema 增加 `continuous`，`required` 不变
- `main/runtime/node_runner.py`：
  - 建子节点时把 `spec.continuous` 写进 `child.metadata.continuous`（与 `spawn_owner_*` 同处）
  - `_spec_requires_acceptance` 增加连续分支
  - `_ensure_spawn_acceptance_node`：`acceptance_prompt` 为空且连续时用条款常量；非空时条款叠加在前
  - `create_acceptance_node` / `_compose_acceptance_prompt`：确认注入发生在**指纹计算之前**
- `main/service/runtime_service.py:1997-2035` + `_build_task_record`：根连续 ⇒ `final_acceptance.required=true` + 默认条款
- `tools/create_async_task_cn`：参数与描述

**验收（单测）**

- 连续子节点未写 `acceptance_prompt` → 建出验收节点，`acceptance_prompt_template` 含条款
- 连续子节点写了 `acceptance_prompt` → 条款与模型文案并存，模型文案不被覆盖
- 连续 + `requires_acceptance=false` → 仍建验收节点
- 非连续节点行为逐字不变（回归）
- 同一 spawn 轮重放（同 `call_id`）→ 恢复指纹一致，不产生第二颗验收节点
- 根连续 → `final_acceptance.required=true`；根提交后必跑最终验收
- 验收节点自身 `metadata.continuous` 恒为假

### 阶段二：spawn 回执与工具描述

**落点**

- `main/models.py:158-178`：`SpawnChildResult.acceptance_auto_created: bool = False`（`exclude_none`/默认值语义与既有增量字段一致）
- `main/runtime/node_runner.py`：`_update_spawn_entry` 持久化 `acceptance_auto_created` 到 entry；`_spawn_creation_receipts` **从 entry 读**，不现算
- `main/runtime/internal_tools.py:549-576`：description / model_description 说明「连续子节点自动获得验收节点」

**为什么必须从 entry 读**：`_spawn_creation_receipts` 是重放路径的唯一输出，同一 `call_id` 重放必须返回同一批回执；现算会让重放与首次不一致。

**回执文案**（短句，机器字段另给）：

```
该子节点是连续节点，运行时已自动为它创建验收节点 <id>；它提交结果后由验收节点裁定是否结束。
```

不要塞进 `node_output`：`created` 回执的 `node_output` 现为空，父节点可能读成子节点结果。

**验收（单测）**：首次回执带标记；重放回执逐字相同；非连续子节点不带该键。

### 阶段三：提示词与纯文本出口

**落点**

- `main/runtime/node_runner.py:3224-3259`：bootstrap user JSON 增加 `run_mode: "continuous"`（仅连续节点出现，保持非连续节点字节不变）
- `main/prompts/node_execution.md` + 新 partial（如 `shared_continuous_idle.md`）：连续节点收尾用 `enter_wait(waiting_for=["frontdoor"])`；只有收到明确结束指令才提交；提交即进验收
- `main/prompts/shared_final_result_protocol.md`：说明连续节点的提交是「请求结束」，由验收裁定
- `main/runtime/plain_text_reply.py`：`build_node_plain_text_reply_block_message(node_kind, *, continuous=False)`，连续分支的合法出口清单加 `enter_wait`
- `main/runtime/react_loop.py:2130-2147`：调用处传连续标记

**验收（单测）**：连续节点的打回文案含 `enter_wait`；非连续文案逐字不变；bootstrap payload 只在连续节点多一个键（断言渲染结果，不断言文件原文——`_load_prompt` 会展开 include）。

### 阶段四：停滞豁免

**落点**

- `main/service/runtime_service.py:6764-6789`：`classify_task_stall_reason` 在 `SUSPECTED_STALL` 之前增加一档 `DESIGNED_IDLE`
- 判据（复用等待图，不新造读法）：任务内**所有**非终态执行节点都停在 `waiting_explicit`，且**至少一个**声明了 `wait_external`；有未读、有在飞工具调用、或有节点在非停靠相位时不豁免
- 读法参照 `task_actor_service.py:1568-1582` 的 `build_wait_nodes` + `find_all_parked_waits` 用法；注意 `read_runtime_state(frame_mode='stall')` 只回 `node_id` 与 `tool_calls`，需要节点 metadata 时另读节点行
- `main/service/task_stall_notifier.py:230-255`：`DESIGNED_IDLE` 与 `USER_PAUSED` 同档处理（不投递）；是否 re-arm 按现有 blocked 车道

**不豁免的形态**（必须保持告警）：只等任务内节点的停靠（可能是真死锁）、有未读却不动、有在飞工具、验收节点卡住。

**验收（单测）**：连续节点空闲 → 无 `task_stall` 事件；把 `frontdoor` 换成具体节点 id 且全员停靠 → 仍走现有强制点火/告警路径；有未读 → 不豁免。

### 阶段五：显示态

**落点**

- 节点：`_node_state_label` / 树投影 `visual_state`，`waiting_explicit` + `wait_external` → 「待命」，与普通「等待中」区分
- 任务：`g3ku/web/frontend/org_graph_app.js:9686` `taskStatusKey` 增加派生态 `standby`（所有非终态节点均待命时），`org_graph_tasks.js:7-21` 标签与筛选桶，`org_graph.css:645-691` 徽标配色
- 配色默认：**琥珀/浅黄**（归档方案已放弃，该色位空出）；待命不新增卡片徽标，只改状态胶囊文案与颜色
- `tools/task_progress_cn/resource.yaml`：判读规则同步（`task_progress` 文本已渲染 `等待中`，需要区分「待命」）

**验收**：网页实测——连续任务空闲显示「待命」；发消息后转「运行」；节点详情显示验收绑定；筛选可单独看向命任务。

### 阶段六：文档与实盘验收

按 AGENTS.md 走 `skills/negi-architecture-maintenance/SKILL.md`，预计更新：

- `docs/architecture/main-task-runtime.md`：验收节点章节（连续 ⇒ 必过验收、自动分配、条款注入与指纹）、节点信箱与唤醒（连续节点待命形态）
- `docs/architecture/tool-and-skill-system.md`：`spawn_child_nodes` 合同新增 `continuous`、回执新字段
- `docs/architecture/heartbeat-system.md`：Task Stall Detection 增加 `DESIGNED_IDLE` 档与判据
- `docs/architecture/web-and-admin.md`：任务/节点「待命」显示态合同
- `docs/architecture/README.md`：Debugging Entry Points 增补（连续节点空闲不报失速、自动验收来源）

## 4. 风险与残留

| 风险 | 处置 |
|---|---|
| 连续节点反复提交 → 验收反复 reject（拒收无次数上限，每次两轮模型调用） | 靠提示词约束；实盘出现再加计数，不在本轮预埋 |
| 验收判不了用户真实意图，只能按「节点自己声明结束 + 交付可核验」判 | 条款里写清口径；日后需要「上游点头」再叠 `end_authorized` 门 |
| 父节点被 `_final_submission_blockers` 拦下而困惑 | 阶段三的 partial 里说明：连续子节点存活期间父节点不能提交，需先结束子节点 |
| 自动条款不确定 → 崩溃重放建出第二颗验收节点 | 条款为常量、注入早于指纹计算；阶段一用重放测试钉住 |
| 停滞豁免误掩盖真死锁 | 豁免要求 `wait_external`，只等任务内节点的停靠仍告警 |
| 长期上下文与磁盘增长 | 走现有节点侧压缩；本计划不改终态清理，任务结束后按现状回收 |
| 行号漂移 | 前置闸门第 3 条；每阶段开工前重新定位锚点 |

## 5. 测试与验收

**工具链**（本机 `python` 不在 PATH）：

```bash
cd /d/Project/Negi
./.venv/Scripts/python.exe -m ruff check .
./.venv/Scripts/python.exe -m pytest tests/resources/test_resource_runtime_smoke.py -q
./.venv/Scripts/python.exe -m pytest tests/test_node_acceptance_scheduler.py tests/test_acceptance_pass_unseen_notices.py -q
```

新增测试直接放 `tests/`（默认跟踪）。每阶段至少覆盖：连续/非连续两分支的回归对照（非连续必须逐字不变）。

**实盘验收（单测不算完成）**——按 memory 约定，运行时改动要重启 + 真任务验收，逐消费面验：

1. 建一个连续根任务 → 根节点收尾 `enter_wait(frontdoor)` → actor 退出、任务显示待命
2. 空闲 30 分钟 → 无 `task_stall` 事件、无孤儿收尸、无强制点火
3. 网页/前门发消息 → 节点被唤醒、`message_source=restored_frame`（上下文接续，不是 fresh）
4. 外部事件订阅投递 → 同样唤醒并接续
5. 让连续节点提交 → 进 `waiting_acceptance` → 验收 `reject` → 节点复活继续待命
6. 再次提交 → 验收 `pass` → 节点 `success` → `child_final` 到父 / 任务终态
7. 连续子节点存活期间父节点提交 → 被 blockers 拦下，文案可读
8. 重启 worker → 待命节点不被重跑，下一条消息仍能唤醒
9. 手动暂停连续节点 → 消息落箱不唤醒（A5=A 未破）

## 6. 提交纪律

- 每阶段一个 commit，完成即提交，不等催。
- 提交身份用 `-c user.name=ZXY39 -c user.email=1045273950@qq.com`，不改 git config。
- 只 `git add` 本计划涉及的文件；`work/` 下其他未跟踪文件不动。
- 禁用 `git stash`；需要基线对比用 detached worktree。
- 「提到 main」= 本地 ff 合并，不推送 origin。

## 7. 交接

- 当前状态：**阶段一至五已实现并提交，阶段六文档已更新；实盘验收待运行实例重启后执行。**
  - `995404aa` 阶段一：连续标记 + 验收强制
  - `75afbc10` 阶段二：spawn 回执自动验收标记
  - `8af1f385` 阶段三：连续节点收尾契约 + 纯文本出口
  - `5c55b413` 阶段四：设计内待命停滞豁免
  - `b7cc052e` 阶段五：待命显示态（后端派生态 + 树/大厅/子节点工具）
- 已确认的设计决定（本轮对话）：不做归档、不做终态复活、不摘 `submit_final_result`、连续 ⇒ 必过验收、缺省自动分配验收、spawn 回执要说明自动创建、`enter_wait(waiting_for=["frontdoor"])` 是必须的收尾但任意来源都能唤醒。
- 已落地的默认值：连续标记落 `node.metadata.continuous` 且创建后不可变；自动验收条款为确定性常量、叠加在模型自己写的 `acceptance_prompt` 之前；待命用浅黄徽标；停滞豁免只在「全员停靠 + 至少一个外部唤醒源 + 无暂停 + 无未读 + 验收不在飞」时成立。
- 实盘验收清单：见 §5「实盘验收（单测不算完成）」。运行实例当前在 `127.0.0.1:18790`（`python -m g3ku web --port 18790`），重启后加载新代码；重启会打断在跑会话，需操作员确认。
- 若上下文耗尽：本文件即交接载体，实盘清单与阶段落点都在上文。
