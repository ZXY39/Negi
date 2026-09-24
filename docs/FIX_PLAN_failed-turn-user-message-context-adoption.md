# Fix Plan: 失败回合把用户消息标成"已回答"，导致它永久进不了模型上下文

> Incident: `ext:qq-official:f8a8001865631301` / 失败轮 `turn:1c77fbc070034a…` / 后续轮 `turn:37fd27472c974a2c`（"继续"）
>
> Observed window: 2026-09-24 13:13:39 – 13:29:04 Asia/Shanghai
>
> Status: 已实现（P0–P3 + 回归 + 文档）。实盘验收未做，进程未重启＝未生效。实现期对计划的两处更正见 §9
>
> Scope: 用户输入在转录里的"未被回答"状态机、失败回合的收尾动作、待重投行的接走者范围；web 与 ext 两类会话同时适用

---

## 0. Executive Summary

五个决定性事实，它们决定方案形状：

| # | 测量 | 结果 | 决定 |
| --- | --- | --- | --- |
| D1 | 续跑上下文读什么 | **不读转录**。种子是 `session._frontdoor_request_body_messages`（= 上一次**真发出去**的请求体），恢复顺序 paused_snapshot > inflight_snapshot > completed_continuity（`g3ku/runtime/session_agent.py:762-783`）。13:23 那轮工件实测 `frontdoor_restore_source=completed_continuity`，79 条消息里搜本轮之前的用户输入，只到 09:05 那条 | 不变量的落点不能指望"基线多存一份"，得让转录侧的**未回答状态**有牙齿 |
| D2 | 基线什么时候推进 | 只在有 authoritative actual request 时（`g3ku/runtime/frontdoor/_ceo_runtime_ops.py:4084`、`:4154`、`:4177-4192`）。13:13 那轮在 `main/runtime/chat_backend.py:828` 构造 provider 时就抛（链上 `glm-5.2-5→-4→-3…` 共 24 次 `MODEL CHAIN: FALLBACK`，全是 `401 code=16 Forbidden`，兜底位 `sensenova-6.8-flash-lite-2` 无 key）⇒ 请求从未发出 ⇒ 无 actual-request 工件 | 失败轮**没有**基线可写，这是设计而非疏漏；不要去污染 `actual_request_hash` / 边界快照 |
| D3 | 提交点有没有落盘 | **有**。`internal_source is None` 的回合在 `_run_message` 之前就把本批次全部用户输入写成 `_transcript_state=pending`（`session_agent.py:3350-3354` → `:1740-1780`） | 用户消息的 durable 一份在提交点已经有了，本计划**不新建采纳点** |
| D4 | pending 行有没有读者 | 有，但只在**会话对象构造时**读（`_rehydrate_queued_follow_ups`，唯一生产调用点 `:249`）。进程内连续两回合不会重建对象 ⇒ 13:22 那轮没有任何接回动作 | 必须补一个"每回合开始前"的接回点，否则 D5 修完仍然不生效 |
| D5 | 谁把 pending 毁掉 | **错误路径自己**。`_prompt_locked` 的 `except Exception`（`:3531`）调 `_persist_turn_transcript`，其中 `internal_source is None` 分支无条件按 `_TRANSCRIPT_STATE_COMPLETED` upsert 用户行（`:2713-2724`）⇒ 一条从未被回答的输入被盖上"已完成"。而 `_rehydrate_queued_follow_ups` 的第二道闸门（`:3884-3903` `answered_turn_ids`：该 turn 有助手行就不接回）会把错误助手行当成"已回答" | 两处必须同时改：只改状态不改 `answered_turn_ids`，接回仍被挡；只改 `answered_turn_ids` 不改状态，没有 pending 可接 |

### 频率与既有规避手段

- 全仓转录里 **18 例**"失败回合 + 其 preceding 用户行"，状态**全部**是 `completed`（无一例外 ⇒ D5 是普适形状，不是偶发）。本会话 8 例，web 会话 8 例 ⇒ **契约必须覆盖 web**。
- 唯一实际出口是**用户逐字重发**：实盘两例 —— 09-23 15:15:21 与 15:20:18 同文本；09-24 13:13:39 与 13:26:03 同文本。
- 重发之所以有效：那次它落在**排队**车道上（13:26 行仍是 `pending`），13:27 起请求体里就有原文了。⇒ **排队车道本来就满足我们要的不变量**，缺的只有"本轮 live 输入"这一种形状。

### 本计划的取舍

不新增状态、不新增上下文来源、不动基线：把失败回合的用户行**留在 pending**，并让下一回合开始前的接回点把它经既有的批次合并（`prompt_batch`，合同见 `runtime-overview.md`「prompt_batch 批次回合内容合并」）带进上下文。等价于把排队车道已经有的语义，补到 live 输入车道上。

Required invariants:

> **一条用户消息只要没有拿到一次真实模型轮次，它的转录状态就不得是终态。** 失败、取消、拒收都算"没拿到"。终态（`completed`）的授予权属于成功收尾路径，错误路径无权授予。

> **`answered_turn_ids` 的判据是"该 turn 产出过对用户的回答"，不是"该 turn 写过助手行"。** `source=runtime_error` / `discarded` 的助手行不算回答。

---

## 1. 事故链（实测）

1. 13:13:39 用户长指令（对项目机制检查报告）经外部车道进入回合 `1c77fbc070034a`，提交点写成 `pending`。
2. 13:13:41–48 模型链 24 次回退全 `401`，兜底位无 key ⇒ `ModelProviderExhaustedError`。
3. 错误路径落两份盘：助手错误行 `1236`（`source=runtime_error`）+ **把用户行 `1235` 升成 `completed`**。至此该消息既不在基线、也不是 pending、且 `answered_turn_ids` 里有它的 turn ⇒ 三条接回路全断。
4. 13:22:53 用户发"继续"。该轮种子 = 09:05 那份请求体 + "继续"，79 条消息，`执行管控四件套` 零命中。模型答的是 09:05 的性能话题 —— 它没看错，它没看见。
5. 13:26:03 用户逐字重发，这次落排队车道（`pending`）⇒ 13:27:33 起请求体含原文，任务被真正执行。**用户自己找到了正确 workaround，这不能算系统行为。**
6. 附带：QQ 端从 09:05:54 之后没有任何 `qq-official delivered`。失败回合只发 `turn.failed`（`g3ku/runtime/api/external_turns.py:211`），而桥的可投递白名单只有 `reply.final` / `outbound.created`（`g3ku/qq_official/messages.py:68`）⇒ 用户不知道该回合失败，才会盲发"继续"。见 §7，本计划不覆盖，但它是同一条事故链的可见性面。

---

## 2. 设计

### 2.1 失败收尾：把本轮 live 输入退回队列，而不是升成终态

- 判据：本轮存在提交点写的 `pending` 用户行（即 live 输入，非内部回合）。
- 动作（在 `_prompt_locked` 的 `except Exception` 分支，落完错误行之后）：
  1. **不再**由 `_persist_turn_transcript` 把用户行升 `completed` —— 让错误路径走 `internal_source is not None` 那一侧的"不碰用户行"分支，或显式跳过升格；行状态保持 `pending`。
  2. 把该 `UserInputMessage` 退回 `self._state.queued_follow_up_messages`（内存队列），使**同进程**的下一回合在 prepare 阶段按批次合并消费它，不依赖 D4 缺失的重建路径。
  3. 保持既有约定：错误路径**不**传 `retire_lingering_transcript_rows`（`:2728-2735` 只有成功路径传）⇒ 别的排队行也不会被失败轮误退役。
- 取消/暂停分支（`asyncio.CancelledError`、`CeoFrontdoorInterrupted`）已有自己的收尾（`_persist_manual_pause_user_messages` 写 `paused`），本计划不动它，但要求"取消路径不升 completed"这条一并核对——同一判据，别留第二种形状。

### 2.2 接回点的两处补丁

1. `answered_turn_ids`（`:3884-3903`）排除 `metadata.source in {runtime_error, discarded}` 的助手行。不改这条，§2.1 之后冷启动路径仍然判"已回答"。
2. 每回合开始前接回一次（`_prompt_locked` 里，仅 `internal_source is None` 且内存队列为空时）。这是 D4 的正解：**接回是每回合动作，不是重启动作**。

### 2.3 谁允许接走（决策：只允许面向用户的回合）

`_rehydrate_queued_follow_ups` 补出来的行会被"下一个 prepare 的回合"消费，而内部回合（heartbeat / cron）同样有这条路——这正是 2026-09-21「9 条跨 10 天旧提问成批重投」的机制面。因此：

- 带"来自失败/取消回合"标记的待重投行，**只允许 `internal_source is None` 的可见用户回合接走**；内部回合的 prepare 不得把它并入批次。
- 代价（已接受）：只被定时任务唤醒的会话里，这条消息会多等到下一次用户输入或下一次可见回合。不做计时兜底。
- 落法：区分接走者靠调用点（§2.2 的第 2 条只在 `internal_source is None` 分支注册），不新增字段。若实现中发现需要标记，标记的键用现有 `turn_id`，不引入新符号。

### 2.4 计划期自我更正（两条）

- **更正 A**：本计划初稿的 P0 是"在提交点把用户消息 append 进续跑基线"。该路线要求同时给失败轮补一份"从未发出的请求体"作为截断/Fork 数据源，污染面（`actual_request_hash`、`write_turn_boundary_snapshot`、Fork 载荷）明显大于收益，且 D2 表明基线本身对失败轮无内容可提交。**已废弃**，改为 §2.1 的"退回既有排队语义"。
- **更正 B**：初稿称"失败轮没有边界快照 ⇒ 它自己那条消息不可编辑重发"。实际判据是 **prev_turn** 的快照（`web_ceo_history_edit.py:206`、`:216`），所以一次失败吞掉的是**它后面那条消息**的编辑资格（本例即 13:22 的"继续"）。且保留窗口只有 3（`web_ceo_sessions.py:42` `TURN_BOUNDARY_SNAPSHOT_KEEP = 3`）⇒ 编辑重发本来就只覆盖最近约 4 条消息，13:13 那条早已出窗，任何修法都救不回它。
  - 连带结果：原"决策①（给失败轮补截断点）"从必做项降为 **P3 可选加固**，收益仅是"别因为一次失败就让后一条消息丢编辑资格"，与上下文丢失无关。

---

## 3. 实施步骤

| 阶段 | 内容 | 落点 |
| --- | --- | --- |
| P0 | 错误/取消收尾不再给用户行终态；本轮 live 输入退回内存队列 | `session_agent.py:2713-2724`（升格处）、`:3517-3548`（错误分支） |
| P1 | `answered_turn_ids` 排除 `runtime_error` / `discarded` 助手行 | `session_agent.py:3884-3903` |
| P2 | 每回合开始前的接回（仅可见用户回合）＋ 内部回合不得接走 | `session_agent.py:249`、`:3350-3354` 附近、`_rehydrate_queued_follow_ups` |
| P3（可选） | 失败轮补一份边界快照，恢复"后一条消息"的编辑资格；须先验证 Fork 载荷不吃未发出体 | `web_ceo_sessions.py:1470`、`web_ceo_history_edit.py:489` |
| R | 存量 18 条孤儿行**不追溯**。追溯会把"在不"/"喂"这类作废输入拽回上下文，正是 09-21 事故形状 | 无代码 |

---

## 4. 误伤表

| 担心 | 判定 |
| --- | --- |
| 下一轮是心跳/cron，用户输入被静默吃掉 | 真风险，由 §2.3 封堵（只允许可见用户回合接走）。这是本计划唯一会重演 09-21 形状的口子 |
| 同一条被回答两次（用户已经重发过） | 会被接回并再答一遍。缓解：批次合并按文本判据去重（`_reconcile_paused_user_turns_into_seed` 已有同文本跳过的先例可参照）；验收 §6 第 5 步专测此形状 |
| 陈旧重投（几天前的失败行某天被接回） | §2.3 已把接走者限死为可见用户回合，触发条件收敛为"用户自己又发了一条"。不加时间阈值——阈值是拍脑袋，且样本 n=18 全部落在"用户立刻重发"这一侧 |
| 上下文变长、缓存前缀被顶掉 | 接回的是**一条用户消息**（实测本例 1.2 KB），且落在批次尾部；不动 system/overlay 位置。对照记忆 [[project-memory-snapshot-cache-impact]]，结构顶掉前缀的是 index 1 的块，这里不涉及 |
| 转录里同文本两条 user 行（1235 与 1239）显示成两次提问 | 事实如此，用户确实发了两次。展示层不改 |
| `answered_turn_ids` 放宽后，真正答过的行被重复回答 | 该判据只在"该 turn 有**非错误**助手行"时才挡住，正常回答行照旧挡。风险面限于"错误行 + 真实回答行同 turn"，而错误行是本回合唯一输出 ⇒ 由 P1 的 source 白名单精确化 |

## 5. 用户视角前后对照

| 场景 | 现在 | 之后 |
| --- | --- | --- |
| 发一条 → 模型侧失败 | QQ 端无声；上下文里这条永远消失 | QQ 端仍可能无声（见 §7），但这条仍是"未回答"，下次任意用户回合自动带上 |
| 失败后发"继续" | 模型答上一件成功过的事（本例答 09:05 的性能话题） | "继续"与待重投那条一起进批次，模型能看到它要继续的对象 |
| 失败后逐字重发 | 依然正常（它本来就落排队车道） | 同上；多一条已回答判定，不会双答（§4 第 2 行） |
| 点失败那条消息「编辑重发」 | 灰（本来就出窗） | 不变（P3 未做时）；做 P3 改的是**后一条**消息的资格 |

## 6. 验收办法

复现配方（不需要真等 provider 挂）：把一个角色的链尾绑到无 key 的 binding，向 ext 会话发一条唯一可识别的长文本 → 该轮必然在 provider 构造阶段抛 ⇒ 无 actual-request 工件。

1. 失败当轮：`.g3ku/web-ceo-requests/<session>/` 内**无**新工件；转录用户行仍是 `pending`（不是 `completed`）；错误助手行 `source=runtime_error`。
2. 紧接着发"继续"：新工件里必须出现第 1 步那句原文，且**只出现一次**。
3. 守卫不报警：新工件的 `frontdoor_history_shrink_reason` 不得为 `context_shrink_quarantine`。
4. 内部回合不接走：把该会话的下一次唤醒排成 heartbeat/cron，确认待重投行未被并入其批次（日志锚点 `Rehydrated N queued follow-up message(s)` 不得出现在内部回合）。
5. 双答检查：在第 2 步之前先逐字重发一次，确认最终只有一份回答（§4 第 2 行）。
6. 重启不回归：kill 掉 web 进程再启，未回答行仍能经 `__init__` 路径接回。
7. 回归集：`tests/test_paused_turn_transcript_lifecycle.py`、`tests/resources/test_ceo_frontdoor_persistence.py`、`tests/resources/test_ceo_context_assembly_regressions.py`、`tests/resources/test_frontdoor_inbound_hold_during_compression.py`、`tests/resources/test_ceo_queued_follow_up_api.py`。

## 7. 本计划不会让它变好的

- 模型链本身：`glm-5.2` 全链 `401 code=16 Forbidden` + 兜底位无 key。不修它，用户仍完不成任务，只是不必再逐字重发。
- **失败在渠道端不可见**：`turn.failed` 不在 QQ 桥投递白名单（`g3ku/qq_official/messages.py:68`），错误正文只落转录助手行。这条是独立收口，另案。
- 该会话 09:05:54 之后连成功回复都没有 `qq-official delivered`。出站静默另案，与 [[project-ceo-ws-lane-freeze]] 相邻但不同。
- ext 会话缺 `inflight` / `paused` 两层可恢复快照（`session_agent.py:2631`、`:2648` 硬门在 `web:`）⇒ 渠道上"进程重启砍掉在途回合"仍未被本计划覆盖（P0/P2 只覆盖同进程失败）。

## 8. 文档影响

属"会话/回合生命周期行为 + 工具可见性以外的上下文采纳契约"，需用 skill `g3ku-architecture-maintenance` 判定并更新：

- `docs/architecture/context-and-cache-troubleshooting.md`：现有「坑（paused）」「坑（pending）」两段旁边补「失败回合」这一同族形状与 `answered_turn_ids` 判据；取证锚点日志一行。
- `docs/architecture/runtime-overview.md`：回合契约里写明"终态授予权属于成功收尾路径"。
- `docs/architecture/external-agent-api.md`：回合契约段补一句——失败回合的用户输入仍是未回答态，其接走者是下一个可见用户回合。

---

## 9. 实现期结论（P0–P3 已落地）

落点：`_persist_turn_transcript` 新增 `promote_user_transcript_rows` 参数；错误分支新增 `_requeue_unanswered_user_inputs` 与 `_mirror_completed_continuity_as_turn_boundary`；`_rehydrate_queued_follow_ups` 的已回答集合按 `_TRANSCRIPT_ERROR_REPLY_SOURCE` 排除；可见回合分支新增一次接回。

两处对计划的更正：

1. **P0 单独就足够完成进程内恢复**，P2 不是它的前提。失败收尾把输入放回内存队列后，该回合所属车道（WS 回合链 / external 排空循环）在回合末尾就会把它派发出去。P2 的每回合接回覆盖的是另一形状：转录行仍是 `pending` 而内存队列已不再持有它（会话对象被重建、或队列被截断清理过）。两条都保留，但读代码时不要把 P2 当成 P0 生效的条件。
2. 计划 §2.1 写的是"让错误路径走 internal 分支那套不碰用户行"。实现没有复用那条分支——它同时跳过 paused 退役与 ceo preview 计算，语义不等价——而是只关掉"升格"这一件事（参数化），错误助手行与 preview 保持原样。

已跑验证（观测值）：新增 7 例（`tests/test_failed_turn_user_message_lane.py`）；相邻套件 `test_paused_turn_transcript_lifecycle` 11、`test_ceo_queued_follow_up_api` + `test_frontdoor_inbound_hold_during_compression` + `test_ceo_frontdoor_persistence` 91、`test_ceo_runtime_progress` 138、`test_ceo_context_assembly_regressions` 79、`test_resource_runtime_smoke` + `test_web_ceo_history_edit_files` + `test_ceo_session_truncate_fork_api` 125（含 5 xfailed）、`test_qq_official_bridge` + 新测 35、外部车道 4 文件 43；`ruff check` 两个改动文件干净。**§6 的实盘配方未执行**（需要一次真实的 pre-request 失败），且进程未重启＝线上未生效。

仍未覆盖（见 §7）：`turn.failed` 不投递到渠道、ext 缺 inflight/paused 两层快照、存量 18 条孤儿行按 R 步不追溯。
