# Fix Plan: 静默轮收尾时收口自己的活动阶段（总结槽留空，理由留在轮上）

> **SHIPPED on `main`, 2026-09-25（P1 + P2 + P3 同批）。** 未推送；进程未重启＝未生效。
>
> Verification as observed: 新增 `tests/resources/test_ceo_silent_turn_stage_close.py` **5 passed**，其中 `test_silent_turn_closes_its_active_stage` 在把条件临时改回 `if visible_output:` 后失败（`assert 'frontdoor-stage-1' == ''`），确认该用例咬住本次改动而非只描述现状。回归面：静默与阶段车道（`test_frontdoor_silent_turn` + `test_frontdoor_silent_tool_exposure` + `test_ceo_frontdoor_stage_runtime` + `test_ceo_frontdoor_persistence` + 新文件）**96 passed**；`test_ceo_frontdoor_regressions` + `test_ceo_runtime_progress` + smoke **269 passed / 5 xfailed**；上下文保留 + graph runtime + summary stage + 阶段归档压缩 + 压缩尾窗 + 上下文装配 + `test_heartbeat_prompt_lane` **152 passed / 1 failed**，该失败 `test_normalize_model_output_strips_contract_suffix_from_visible_answer` 已在干净 HEAD worktree 复现同一条（存量红，与本次无关）。`ruff` 对触及的两个文件 All checks passed；全仓 725 findings 属存量。
>
> **尚未验证**：§4.4 / §4.5 的实盘判据需要操作者重启 worker，本会话未重启；§4.5 那条反向探针（含 `silent` 轮且其后还有非静默轮的阶段数应为零）也未在实盘跑过。M1–M5 全部来自 `.g3ku/web-ceo-continuity/*.json` 的静态读数，只有 `representation=raw` 的阶段携带 `rounds`，因此轮级计数是**下限**。
>
> Deviations from this plan, with reasons：
> - **未改 `tool-and-skill-system.md`**（§3 P3 原预计要补一句）：`silent` 的契约唯一归属是 `runtime-overview.md`「3.3」，该文档已有指针，且"不需要活动阶段 / 不占阶段预算"两条不受本次影响——按反堆积规则不加第二处正文。
> - **测试写成 5 条而不是 4 条**：多一条负向 `test_visible_turn_closure_is_unchanged`，把"可见轮同样不写指针摘要"钉住，防止后来者把静默分支的留空规则反向复制到可见分支。
> - **补了 `.gitignore` 一行 allowlist**：`docs/*` 默认忽略，历史 FIX_PLAN 同样逐条 `!docs/FIX_PLAN_*.md` 放行。
> - **`arguments_text` 的说法在写作中被更正**：它是 `_tool_invocation_hint` 的 48 字截断提示（`_ceo_support.py:394-402`），完整理由在 `arguments.reason`；模型不给正文时静默行的可见正文也退回 `reason`（`:8265`）。M4 与代码注释都按更正后的字段写。
> - **文档体积**：`runtime-overview.md` 实测 196.6 KB，README 规则 6 的参考值是 68 KB（band 上限约 88 KB），早已越界。本次只做了一处 in-place 段落追加（约 +1.2 KB），**没有**顺手抬参考值，也没有借机拆分——拆分是独立事项。

---

> Origin: 操作者要求，2026-09-25 —— 起点是「静默消息中创建的阶段会叠到后面正常响应的阶段，要不要叠下去，以 silent 工具调用为判断的分界线是否可行」。中途两次追加裁决：① 提出「把 silent 的 `reason` 当阶段总结并终结活动阶段」；② 明确**否决**"先写占坑总结槽"的形态，要求在不占坑的前提下实现。
>
> Status: 单个运行时改动（P1）+ 测试（P2）+ 架构文档（P3）。无新字段、无迁移、无前端改动。
>
> Scope: 前门（CEO / `web:` / `ext:` / `china:` 全部会话）finalize 的静默分支与阶段账本收尾。心跳/cron 的唤醒与入队语义、`silent` 工具的曝光与闸门豁免、转录行的 `silent_reply` 判据**一字不改**。

---

## 0. Executive Summary

四个实盘测量决定了本计划的形状（数据源：`.g3ku/web-ceo-continuity/*.json` 48 份带阶段账本的会话，共 1146 条 durable 阶段；只有 `representation=raw` 的阶段携带 `rounds`，所以轮级计数是**下限**）：

| # | 测量 | 结果 | 决定 |
| --- | --- | --- | --- |
| M1 | 含 `silent` 轮的阶段 / `silent` 轮次数 | 10 条阶段 / **9 次**（≈0.9% 阶段） | 触发面很小，可以承受无条件收口，不需要为省成本而加窄条件 |
| M2 | 这 9 次里，静默时刻该阶段的状态 | **2 次**之前已有非静默轮、**2 次**之后同阶段继续有非静默轮、**5 次**整条阶段没有任何非静默轮 | 「只收口已有实质进展的阶段」（V1）只触发 2/9，而病例恰是另外那些 → **V1 无效**，必须取 V2 |
| M3 | durable 里本来就没有 `completed_stage_summary` 的 completed 阶段 | **194 / 1146（17%）**，且全是 `compact` 表示、账本照常运转 | 收口时不写总结**不是新状态**，不需要为它造回填机制 |
| M4 | 静默理由现在的实际存放位置与长度 | 完整理由在轮上：`_frontdoor_round_tool_entry` 把 `arguments` 原样写进 `round.tools[]`（`_ceo_runtime_ops.py:5302-5318`），`arguments_text` 只是 48 字截断的调用提示（`_ceo_support.py:394-402`）；另外模型未给正文时 `final_output` 退回 `reason`（`:8265`），即静默行的可见正文就是理由全文。`reason` 长度实测 38–173 字，durable 现有总结中位 151 字 | 用户原提案要的"理由当总结"在信息层面**已经存在且完整**，搬到总结槽只是复制，代价见 §2.2 |
| M5 | 48 份账本里以 `active` 阶段结尾的会话数 | **3 / 48**（active 阶段总数 7 条 / 1146） | 未收口阶段是少数但持续存在的形态，且集中在跨回合驱动的会话上 ⇒ 改动面小、可定向验收 |

一处必须先讲清的机制（它是"叠"的真因，也是 V3 被否的理由）：

`_graph_finalize_turn` 里阶段收尾**全部挂在"本轮有可见正文"上** —— `_ceo_runtime_ops.py:8301` 先算 `visible_output = "" if silent_reply else output`，随后三个收尾调用点是 `:8339`（`should_append_visible_output`）、`:8345`（`direct_reply` 早退）、`:8361`（`if visible_output:`）。静默轮 `visible_output` 恒为空 ⇒ **一条都不走**，却照样在 `:8367-8371` 把"仍未收口的阶段"并进 durable 账本。下一轮从 payload 恢复同一份 `frontdoor_stage_state`（`session_agent.py:572`），新轮次继续往同一个 `stage_id` 里 append；`canonical_context_delta` 对这条阶段只重发新增轮（`canonical_context.py:1044-1106`），于是后面那条正常响应的轨道里就长出这张归属上一个静默轮的卡。

所以修法不是"以 `silent` 调用为分界"去切轮，而是**让静默轮自己把阶段收尾**——收口动作现成，只是没被调用：

| 需要 | 现成的 |
| --- | --- |
| 收口活动阶段 | `_complete_active_frontdoor_stage_state`（`:5526-5563`）：置 `completed` + `finished_at`、清 `active_stage_id`（`:5555`）、`transition_required` 归零（`:5556`） |
| 不写总结就收口 | 同一函数的 `completed_stage_summary` 是**可选参数**（`:5530`），为空时 `:5538` 得到空串 ⇒ `:5549` 的"仅在为空时填充"不触发，槽位原样留空 |
| 无活动阶段时不误伤 | `:5534-5535` 已经是 no-op —— `silent` 本就"不需要活动阶段"（`tool_contract.py:289`），这类静默轮天然不受影响 |
| 理由的可读位置 | 轮上的 `arguments_text`（见 M4），前端轨道/展开都能看到原文；无需第二个真相源 |

---

## 1. Current State (verified at HEAD `f5199f52`)

### 1.1 静默轮的收尾路径

| 环节 | 位置 | 现状 |
| --- | --- | --- |
| 静默判据 | `_ceo_runtime_ops.py:6280-6299` `_silent_signal_from_tool_payloads` | 取本轮**最后一次** `silent` 调用，`reason`/`subject`/`superseded_by` 一次解析后供 finalize 与落盘共用 |
| 可见正文置空 | `:8301` | `visible_output = "" if silent_reply else output`，`final_output` 保留原文 |
| 阶段收尾 | `:8339`、`:8345`、`:8361` | 三处均在 `visible_output` 为真的分支内 ⇒ 静默轮不收口 |
| 账本写回 | `:8367-8371` | 收不收口都写 `frontdoor_stage_state` 与 `_merged_frontdoor_canonical_context(...)` ⇒ 未收口的阶段进 durable |
| 静默轮的轮记账 | `:5338-5361` `_frontdoor_stage_state_after_tool_cycle` | 只把 `submit_next_stage` 摘出 `ordinary_calls`，`silent` 当普通调用进 `round.tools[]`，且 `budget_counted=False`（`stage_budget.py:39-48`） |

### 1.2 展示层为何"看起来在叠"

轨道逐助手行各画一份（`org_graph_app.js:6736-6800`、`:7394-7410`），转录行 append-only、没人回写旧行；`reconcileCeoFeedStageStatuses`（`:7482-7509`）只按 `data-stage-id` 把更早副本的状态升到最新副本的终态。这两件事叠加的结果：静默轮的卡停在它被写入那一刻（多为"进行中"），而正常响应的卡里混着静默轮的阶段。本次改动**不动展示层**：静默轮自己写了终态之后，`reconcile` 在这些都是场景里不再需要升任何副本，正常响应的 delta 携带的是新 `stage_id`，继承面自然消失。

### 1.3 `silent` 在阶段语义上的既有一致性

- 闸门豁免与预算豁免同时成立（`stage_budget.py:29-48`），所以"只调 silent 的一轮"不消耗阶段预算 —— 收口不会因为预算问题提前发生或延后发生。
- `_frontdoor_stage_has_substantive_progress`（`:5053-5074`）的 `non_substantive` 只含 `submit_next_stage` 与 `stop_tool_execution`（`_ceo_support.py:107`），**`silent` 不在其中**：今天"只静默的一轮"会让阶段被判定为有实质进展，下一轮 submit 畅通。收口后这条阶段已不是活动阶段，该判定不再参与决策，本计划不改这个集合（改了会给 submit 新增一条拒绝路径，见 §5）。

---

## 2. Design

### 2.1 收口规则（P1，唯一的行为改动）

`_graph_finalize_turn` 的阶段收尾条件由 `if visible_output:` 扩为 `if visible_output or silent_reply:`，调用**不传** `completed_stage_summary`。效果：

1. 静默轮把当时的活动阶段置为 `completed`、写 `finished_at`、清 `active_stage_id`。
2. `completed_stage_summary` 保持为空 —— 总结槽留给"下一次真正交付结论的轮次"。
3. `direct_reply` 早退分支（`:8345`）不动：它要求 `visible_output` 且 `route_kind == "direct_reply"`，静默轮没有可见正文，走的是底部统一路径，与今天一致。
4. 无活动阶段的静默轮 no-op（`:5534-5535`），不产生幻影阶段。

### 2.2 否决的方案，带各自误伤表

| 方案 | 触发面 | 收益 | 误伤 / 否决理由 |
| --- | --- | --- | --- |
| V0 只改展示层（继承来的卡标"承上静默轮"） | 0 条阶段 | 不动账本 | 症状仍在（同一 `stage_id` 两处出卡），且不治"旧行永久进行中"。出帧侧补状态这条思路在 2026-09-24 已评估并否决：改存储行会打断 `cc_upsert` 编码链，只改出帧则症状变成"刷新才对"（结论见 `docs/architecture/web-and-admin.md` 的轨道对账一节与 `0a9678c6`） |
| V1 仅当该阶段此前已有非静默轮才收口 | **2/9** | 不误伤等待型阶段 | 病例是"静默之后同阶段继续长轮"，V1 恰恰放掉 7/9 ⇒ 治不到症状；那 2 例之后同阶段并无新工作，收不收口一样 |
| V2（采纳）每个有活动阶段的静默轮都收口 | 9/9 | 继承面归零；静默行的卡自带终态 | **5/9 阶段将没有任何非静默轮却被记为 completed**（带 `stage_goal` 与一轮 silent，M4 的 `arguments_text` 就是内容）；**2/9 之后同阶段本要继续干活**，那些调用改为走 stageless 宽限一次 + `STAGELESS_FREE_PASS_REMINDER`（`stage_budget.py:56-61`）+ 记 orphan 轮，随后必须 `submit_next_stage` |
| V3 = V2 + 把 `reason` 写进总结槽（用户最初提案） | 9/9 | 卡上多一行中文结论 | ① **占坑**：总结槽今天由下一次 `submit_next_stage(completed_stage_summary=…)` 书写（`:5127-5134`），先写元理由会把"这条阶段做成了什么"永久顶掉；② 信息重复：理由已在轮上（M4）；③ 体积/一致性代价：`completed_stage_summary` 参与重叠签名（`canonical_context.py:271-282`，字段本身由 `:140` 保留），事后补写会让 turn 副本与 durable 副本签名不一致 ⇒ rebase 认不出同一条阶段并按 `:311-332` 追加副本 |
| V2 + submit 回填车道（无活动阶段时把总结写回最近一条空总结的 completed 阶段） | 9/9 | 不丢真实总结 | 同上③：回填同样改变签名，造副本风险明确；且 M3 说明空总结是 17% 的常态，收益不抵风险 ⇒ **不做**，改后如果实盘出现"静默后总结丢失"的真实投诉再单独立项 |

### 2.3 明确不碰的东西

- `silent` 的 schema、曝光点、闸门/预算豁免、`silent_reply` 判据与 `prompt_visible=true` 痕迹落盘（`docs/architecture/runtime-overview.md:107-115`）。
- 前端 `org_graph_app.js` 与 `org_graph.css`：一行不改。
- 阶段压缩、`context_visible` 收口面、`canonical_context_delta` 的编码链。
- **live 侧"只读 `canonical_context_delta`、绝不回落整份 `canonical_context`"这条不变量继续有效**（`docs/architecture/web-and-admin.md:438`）。本改动只让静默轮收掉自己的活动阶段，`_frontdoor_stage_state` 照旧携带全会话阶段清单，回落整份仍会把历史阶段漏进新的 live 回合。
- 存量 durable 账本：只影响改动生效**之后**新产生的静默轮，不迁移历史阶段。

---

## 3. Phases

- **P1 运行时**：`g3ku/runtime/frontdoor/_ceo_runtime_ops.py` finalize 的静默分支加收口调用（不传 summary），并把 `:8361` 上方那段"轮末不写指针摘要"的注释补齐成"静默轮同样收口但不写总结"的两行说明。约 1 行条件 + 1 次调用 + 注释。
- **P2 测试**：新增 `tests/resources/test_ceo_silent_turn_stage_close.py`，四条：静默+活动阶段→`active_stage_id` 为空、该阶段 `status=completed`、`completed_stage_summary` 仍为空；静默+无活动阶段→no-op（不新增阶段）；同一状态把 `silent_reply` 去掉→与改前逐字一致（可见轮不受影响）；静默轮的 `reason` 仍能在 `round.tools[].arguments_text` 读到（防止有人日后把它挪进总结槽）。
- **P3 文档**：`docs/architecture/runtime-overview.md` 静默一节补"静默轮收口自己的阶段，总结槽留空"的契约与代价；`docs/architecture/tool-and-skill-system.md` 的 `silent` 契约同步一句阶段侧语义。按 `g3ku-architecture-maintenance` 判定 `README.md` 是否需要跟。

---

## 4. Verification

1. `python -m ruff check .`（用 `.venv/Scripts/python.exe`）。
2. `python -m pytest tests/resources/test_ceo_silent_turn_stage_close.py tests/resources/test_frontdoor_silent_turn.py tests/resources/test_frontdoor_silent_tool_exposure.py tests/resources/test_ceo_frontdoor_stage_runtime.py tests/resources/test_ceo_frontdoor_regressions.py tests/resources/test_ceo_frontdoor_persistence.py tests/resources/test_ceo_runtime_progress.py -q`。
3. smoke 子集 `tests/resources/test_resource_runtime_smoke.py`。
4. 实盘判据（需重启 worker 后）：新产生的静默轮，其 `web-ceo-continuity/*.json` 里 `frontdoor_canonical_context.active_stage_id` 应为空串，且该轮对应的阶段 `status=completed`；后续正常响应行的轨道里不再出现上一静默轮的 `stage_id`。
5. 反向探针：`representation=raw` 且 `rounds` 里含 `silent` 且**其后还有非静默轮**的阶段数量（本次 2 条的同类）应当趋零 —— 这是"叠"被治住的直接度量。

## 5. Risks / Rollback

- **阶段数量上升**：静默轮多时（心跳密集的会话）completed 阶段成比例增长，喂给阶段压缩与 `context_visible` 收口面。已知同源风险是 `canonical_context.py:1011-1015` 记录的"一次压缩把 429 条阶段落进同一行、delta 302KB"。当前频率 9 次/48 会话，可接受，但要看 M5 的反向指标：只有 3/48 会话的账本以 active 阶段结尾。
- **2/9 的续跑改道**：静默后同阶段继续工作的场景将先吃一次 stageless 宽限 + 提醒，多一段约 180 字的中文提醒进上下文。这是 V2 相对 V0 的唯一实质代价，选择它是因为 V1 治不到症状。
- **`transition_required` 被归零**（`:5556`）：预算耗尽后静默会清掉该标记，下一轮的闸门从"budget exhausted"变成"no active stage"，两条都要求 `submit_next_stage`，出口不变。
- **回滚**：单点条件改动，`revert` P1 一个 commit 即可；无字段、无迁移、无存量影响。
