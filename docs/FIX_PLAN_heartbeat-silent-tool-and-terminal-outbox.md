# Fix Plan: `silent` 常驻内置工具取代两个文案静默出口（痕迹进模型上下文 + 压缩豁免 + 前端折叠行），并修好终态 outbox 的滞留重放

> Origin: operator request, 2026-09-23 —— 起点是「QQ 会话一启动就自动回一条几小时前的旧任务结果」。取证见 §1.6。追加要求依次为：① 推送与否必须交给模型、不许机器压着等下次输入；② 静默轮必须在转录里留下「模型可见 / 用户不可见」的痕迹；③ 静默出口从文案改成工具调用，且**调用该工具的 turn 不得被阶段压缩裁掉**；④ `G3KU_SILENT` 与 `HEARTBEAT_OK` 两个文案出口全部删除；⑤ 网页侧把静默回合折叠成一行「已静默」，展开可看原文。
>
> Status: 设计已定，六个阶段。P0（终态 outbox 可靠性）与其余各项**完全独立**，可先行单独上线。P1→P2→P3 构成静默车道本体，P4 是删除旧出口，P5 是网页渲染，P6 文档。
>
> Scope: 前门（CEO / `web:` / `ext:` / `china:` 全部会话）的静默判定与痕迹落盘、两条历史压缩车道的豁免、心跳 prompt 的收尾指令、`task_terminal_outbox` 的投递与重放、网页静默回合渲染。心跳事件的**入队与唤醒语义一字不改**（照旧无条件唤醒并交给模型，见 §2.0 的既定裁决）。

---

## 0. Executive Summary

三个决定性的实盘测量，它们决定了本计划的形状：

| # | 测量 | 结果 | 决定 |
| --- | --- | --- | --- |
| M1 | 静默文案出口的成功率（3,271 个 frontdoor 工件 / 160,675 条 assistant 消息 / 248 轮 `[SESSION EVENTS]` 心跳轮） | 精确等于 `[G3KU_SILENT]` 的模型输出 **0 次**；「正文 + 空行 + token 结尾」的失败形态 **11 次，形态 11/11 全同**；`HEARTBEAT_OK` 精确或前缀 **0 次** | 文案出口**从未成功过一次**。且 11/11 都是"正文照写、token 当后缀" ⇒ 模型的真实意图不是"什么都不说"，而是"这段写下来但别发出去" —— 与要求②是同一个动作 |
| M2 | 常驻工具的前缀代价 | 当前 32 个工具 schema 合计 12,406 字符，最小一条 174 字符；`tool_signature_hash` 跨轮恒定（`5bd2004a540d36f8…`，18:35 与 18:37 两轮相同） | 加一条 ~200 字符工具 = 前缀 **+0.08%**，且只付一次冷启动。「常驻 schema」的代价可忽略，要求②的形态成立 |
| M3 | 转录行能否跨压缩存活（`ext_qq-official_f8a8001865631301` 301 个工件） | 296 个不同 call_id 中 **279 个（94%）在到达最新工件前永久消失**；相邻两轮消息总数下降 **36 次，全部标 `stage_compaction`**（最大 338→242）。shrink 分布：`stage_compaction` 272 / `token_compression` 29 | 裸 tool_call 行当痕迹存活率 **6%**。要求③**不是补充项，是本设计能否成立的前提** |

一处**必须翻掉的既定决定**（本计划最大的一块风险来源）：

`g3ku/runtime/session_agent.py:3559-3564` 现在把静默轮写出的 assistant 行标成 `prompt_visible: False, ui_visible: True`，注释写明理由：

> `prompt_visible=False` 与 `_graph_finalize_turn` 一致——静默输出既不回填请求体基线，也不该经转录重放回到模型上下文。

也就是说，今天的设计是**故意让模型看不见自己静默过**。要求②与之正相反。所以这不是"补一个字段"，而是**反转一条有配对约束的决定**：`prompt_visible` 与 `_graph_finalize_turn:8164-8177` 的基线回填口径必须同时改，改一边会让转录与请求体基线互不相认（该配对的历史事故见 §6 第 1 条）。

技术上的好消息 —— **没有新机制要造**：

| 需要 | 现成的 |
| --- | --- |
| 常驻、不参与候选挑选的内置控制工具 | `main/runtime/internal_tools.py:25` `SubmitNextStageTool`，三处硬注入见 §1.3 |
| 「prompt 可见 / UI 不可见」这一格 | `_ceo_runtime_ops.py:484-500` `_hidden_internal_prompt_message_metadata`（`prompt_visible:True, ui_visible:False`），心跳与 cron 的内部提示词已经住在里面 |
| finalize 处读本轮工具调用参数 | `_ceo_runtime_ops.py:8162 _graph_finalize_turn` 的 `state["messages"]`（`:8180`）含 assistant `tool_calls`（`:8038-8046`）与配对 role=tool 结果（`:8036/8052`） |
| 跨车道把工具参数送回 `session_agent` | `_ceo_create_agent_impl.py:931-939` `run_turn` 已在 `setattr(session, "_last_route_kind"/"_last_verified_task_ids")`，同形状加一个即可 |
| 前端静默回合的渲染入口 | `org_graph_app.js:6442-6450 hideCeoAssistantText`、历史 `:6987-7037`、live `:8743-8805`；折叠交互复用 `setCeoTurnUsageCollapsed` 那套 |
| 心跳事件"按时送到" | `session_service.py:205-219` 入队即 `_wake.request(delay_s=0.25)`，本计划不动它 |

---

## 1. Current State (verified at HEAD `002bf3b1`)

### 1.1 静默判定的现有分布

识别点全部基于"输出字符串等于 token"，共 4 个语义族：

| 族 | 位置 |
| --- | --- |
| 归一化（output → `output=''` + `is_silent_reply`） | `_ceo_runtime_ops.py:8164`；`session_agent.py:3386`、`:3527`、`:4135` |
| 出站闸门 | `g3ku/cron/runtime_dispatch.py:73`；`g3ku/transports/channel_session.py:85`；`g3ku/runtime/external_events.py:218`；`g3ku/heartbeat/session_service.py:1783` |
| WS 转发/帧 | `g3ku/runtime/api/websocket_ceo.py:1122`（`_should_forward_message_end`）、`:1634-1703`（`silent_reply` 写于 `:1691`） |
| 定义 | `g3ku/runtime/reply_tokens.py:15/18-20` |

`HEARTBEAT_OK` 另有 4 个生产落点：定义 `heartbeat/session_service.py:51`，判定 `:1201` 与 `:1783`，prompt 指令 `:996/1016/1023/1043/1049/1059`，WS `websocket_ceo.py:98/1124/1126/1136`，转录生命周期 `_ceo_runtime_ops.py:8201`、`session_agent.py:3542`。

注意 `:1783` 那一行把两个 token 合并成同一个"静默"出口 —— 换成工具时这是**一个收口点**，不是两个。

### 1.2 心跳 prompt 里的静默指令自相矛盾

`heartbeat/session_service.py:993-1006`（`task_terminal` 分支）顺序是：

- `:996` 禁止 `HEARTBEAT_OK`/空文本
- `:998-1001` 提供 `[G3KU_SILENT]` 静默出口
- `:1002-1004` 要求必须收尾
- `:1005` **"If the task is already sufficiently complete for the user, summarize the usable conclusion now instead of staying silent."**

`:1005` 在最后、且明确反对静默。今天那条自动回复就是模型照 `:1005` 执行的结果 —— 它已经正确判出了覆盖关系（从 `prompt_lane.py:378-381` 的唤醒时刻与 `:312-316` 的 `Finished at:` 推出的，这两个字段早就在场），是这行把它的判决压回去的。

### 1.3 常驻工具的**唯一**可用先例

- ✅ `submit_next_stage` = `main/runtime/internal_tools.py:25`（`Tool` 子类，`hide_universal_timeout_parameter = True`）；常驻靠三处硬注入：`_ceo_runtime_ops.py:6025-6028`（塞进 `all_tools`）、`:3287-3314`（`_frontdoor_callable_tool_names_for_state` 恒含）、`:3316-3334`（`_frontdoor_runtime_visible_tool_names_for_state` 恒含）；预算/闸门豁免 `main/runtime/stage_budget.py:7,24-41`。`parameters` 完整形 `:51-105`、provider 侧精简 `model_parameters` `:107-143` —— **reason/task_id 参数照抄这个双份形状**。
- ❌ **不要照抄 `stop_tool_execution`。** 它的 `RESERVED_INTERNAL_TOOLS`（`message_builder.py:200`）只保证"已可见时不被语义 top-k 挤掉"，**不具备注入能力**；`tools/*/resource.yaml` 里没有任何族声明它 ⇒ 进不了 `exposure['tool_names']`。
- ❌ 只加进 `CEO_FIXED_BUILTIN_TOOL_NAMES`（`g3ku/runtime/tool_visibility.py:6-18`，11 个名字）也不够：它在 `message_builder.py:388` 仅用于"排除出 top-k"，`:538` 仍要求已可见。
- 纯内置工具不在 `tools/` 目录 ⇒ **不计入资源管理器那个 "33 tools"**（`g3ku/resources/registry.py:110-125` 按目录 iterdir，无索引文件需同步），但也因此永远不被 RBAC 返回（`main/service/runtime_service.py:5512-5519` 的 `supported` 只取 `tool_instances()`），必须在上面三处硬注入里绕过 exposure 过滤。这是 §2.1 的全部难点。

### 1.4 两条压缩车道如何杀行

| 车道 | 实现 | 杀法 | 现有豁免 |
| --- | --- | --- | --- |
| stage 压缩 | `g3ku/runtime/stage_prompt_compaction.py:428` `compact_stage_prompt_messages_in_place`，调用点 `message_builder.py:2191/2202-2208`，标记值产生于 `message_builder.py:2238` | `:595-624` 逐 call_id 命中 `expired_call_ids`（来源 `stage.rounds[].tool_call_ids`，`:511-523`）即整行移除，配对 tool 行成对移除（`:633-647`） | **只有 `STAGE_TOOL_NAME`**：归属写入时排除（`_ceo_runtime_ops.py:5129-5133`、`:5411-5415`），另有 `:559-583` 双邻特例 |
| token 压缩 | `_ceo_runtime_ops.py:2113 _run_frontdoor_llm_token_compression`，调用 `:2365`，标记体 `:2336-2343` | `:2158-2159` 位置型切分，`older_history_messages = normalized_body[:-recent_tail_count]` 整段换成一条纯文本块（`:2344`） | 无。尾部宽度 `_frontdoor_compaction_tail_count`（`:1884-1938`）从最近 4 条起 ⇒ **位置型永远保不住指定行** |

跨压缩活下来的**唯一**现存通道是元数据 carry-forward：`stage_archive` / `archived_through_created_at` 写进 `[G3KU_TOKEN_COMPACT_V2]` 的 JSON 载荷（`_ceo_runtime_ops.py:2017-2079`），靠下一轮复读上一块载荷逐轮继承（`stage_prompt_compaction.py:1001-1064`）。`canonical_context`/`cc_upsert`（`canonical_context.py:635-695`）**与此无关**，它只是 delta 存储编码，不阻止任何裁行。

### 1.5 前端静默回合现状

后端标记 `metadata.silent_reply`（写于 `session_agent.py:3559-3564`）→ 快照 `websocket_ceo.py:893-899`：**强制 `item['content'] = ''`，且没有阶段轨道则整行 `continue` 跳过**。前端 `hideCeoAssistantText`（`org_graph_app.js:6442-6450`）隐藏气泡但保持轨道可见。

既有 JS 契约（`tests/resources/org_graph_app.ceo_silent_turn.test.js`）：`:306-310` 气泡 hidden、轨道必须可见；**`:324-326` 断言 `row.content === ""` 且 `silent_reply === true`**；`:339` 找不到回合元素不得补空 system 气泡；`:356-362` 历史静默行渲染为回合、`textEl.hidden`；`:373` 无轨道则整行不渲染。

⇒ 要求⑤（保留原文 + 折叠 + 展开）**必然破 `:324-326` 与 `:373` 两条**，是合同变更而非新增断言。

### 1.6 触发本次取证的生产事故

`task_terminal_outbox` 里 `task-terminal:task:543e0f15d798:success:2026-09-23T16:55:17` 一行：`attempts=4`、`last_attempt_at=16:55:32`、`delivered_at=18:35:08`。同批 6 行里其余 5 行全部 `attempts=0` 且一分钟内投递。历史 46 行中 2 行滞留（4.3%），另一条 `task:7e2a270eec34` 滞留 **10h16m**（09-20 05:07→15:23），同型。

死因链（全部已验证）：worker 投递梯子 `retry_delays=[0.0,0.5,2.0,5.0]`、每次 `timeout=2.0`（`runtime_service.py:4976-5023`），15.0s ≈ 7.5s 间隔 + 4×2s 超时；那 15s 内 web access log **零条** `POST /api/internal/task-terminal` ⇒ 全部死在客户端超时。`SessionHeartbeatEventQueue._events` 是纯内存 dict（`session_events.py:25`），web 没收到 ⇒ 队列里没有挂起项可重试。全代码库只有两个补投点，都在进程启动时：worker `runtime_service.py:812-816`、web `g3ku/shells/web.py:1038 replay_pending_outbox=True`。那个 60s 的 `_distribution_reconcile_loop`（`:7326-7340`）只管分发 epoch，**不扫 terminal outbox**。取数条件 `list_pending_task_terminal_outbox` = `WHERE delivery_state != 'delivered' ORDER BY created_at ASC LIMIT 500`（`sqlite_store.py:1978-1984`），无时效上限。

---

## 2. Design

### 2.0 两条已定裁决（不再讨论）

1. **推送与否归模型。** 机器侧只做一件事：把心跳事件按时送到并唤醒。不许"入队不唤醒"、不许按时效丢弃、不许"等用户下次输入再带出"。陈旧事件的正确处理是让它进模型、由模型决定说或不说。
2. **`silent` 全轮生效**（含用户轮），配 §2.4 的网页折叠行作为可见性补偿。

代价与边界写进 §6 第 3 条（渠道轮无折叠 UI 可展），不额外加开关。

### 2.1 工具本体：`silent`

形状照抄 `SubmitNextStageTool`（`internal_tools.py:25`），包括 `hide_universal_timeout_parameter = True` 与 `parameters`/`model_parameters` 双份。

```
name: silent
parameters:
  reason      (required, string)  —— 为什么静默；同时是审计载荷与用户轮兜底正文
  subject     (optional, string)  —— 被静默的对象，通常是 task_id 或 event_id
  superseded_by (optional, string)—— 若因"已被更新结果覆盖"而静默，填覆盖它的那个 task_id
```

`superseded_by` 是本计划唯一新增的语义参数。理由：M1 那 11 次失败全是"覆盖型静默"（旧结果已被新汇报吃掉），要求模型显式填这个字段，等于把"我判过覆盖关系"变成一个**可校验、可审计**的动作，而不是一句内心活动。它同时喂 §2.3 的痕迹与 §2.5 的旧出口替换。

注册与常驻：在 `internal_tools.py` 新增 `SilentTool`，并在 §1.3 的三处硬注入里与 `STAGE_TOOL_NAME` 并列。豁免 `stage_budget` 与工具闸门（`_ceo_support.py:107`、`react_loop.py:224-225` 同 `stop_tool_execution` 待遇）。`execute` 无副作用，只回一个定长 JSON（`{"ok":true,"silenced":true}`）—— **工具本身不产生任何用户可见输出**，真正的动作发生在 §2.2 的 finalize。

不新增 config 开关。要退回旧行为就是回滚 P1 这一整个 commit。

### 2.2 识别点：从"输出文本"改成"本轮调过 silent"

`_graph_finalize_turn`（`_ceo_runtime_ops.py:8162`）里用 `state["messages"]` 扫出最后一次 `name == "silent"` 的 call，取 `reason`：

- `silent_reply = True`（覆盖现在的 `is_silent_reply_token(output)`，`:8164`）
- **`output` 保留模型写的正文**（当前 `visible_output = "" if silent_reply else output`，`:8176` 要改：静默不再清空 output，而是把它降级成"模型可见、UI 折叠"的痕迹正文）
- 新增 `state` 字段 `silent_reason`（`state_models.py:96` 的 `silent_reply` 旁边），供 §2.3 落 metadata
- **不要用 `used_tools`**（`:8055-8063` 剔除了控制工具名，拿不到 `silent`）
- 从 `strip` 到 `is_silent_reply_token` 的 4 处归一化点（`session_agent.py:3386/3527/4135`、`_ceo_runtime_ops.py:8164`）中，`session_agent` 那三处只有字符串 `output` ⇒ 走 `_ceo_create_agent_impl.py:931-939` 的 runner 回填（同 `_last_route_kind` 形状）把 `silent_reason` 送出去，不在 `session_agent` 里重新解析文本

同批并发的裁决（口径②）：`parallel_tool_calls: true`，所以同批可能同时有 `silent` 和普通工具。**该批其余工具照常跑完，本轮再以静默收尾** —— 不为 `silent` 做批次截断。这也让 `silent` 与 `submit_next_stage` 同批时不产生顺序歧义。

### 2.3 痕迹：翻转 `prompt_visible` + 双车道豁免

一行搞定三件事：把静默轮那行写成

```
prompt_visible: True,   # 反转 session_agent.py:3559-3564 的既定决定
ui_visible: True,       # 保留，配合 §2.4 的折叠
silent_reply: True,     # 沿用
silent_reason: "...",   # 新增
silent_subject: "task:xxx",
```

**配对约束**：`prompt_visible` 必须与 `_graph_finalize_turn` 的基线回填同步改（那行注释点名的就是这个配对）。只翻 metadata 不改基线侧 ⇒ 同一行在"转录重放"与"请求体基线"两条路上表现不一致，正是历史事故的形状（§6 第 1 条）。

压缩豁免（要求③，两条车道都要动）：

- **P2a stage 车道**（便宜）：`stage_prompt_compaction.py:607-621` 循环内命中 `SILENT_TOOL_NAME` 时置 `removable_all=False`；并在 `_ceo_runtime_ops.py:5129-5133`、`:5411-5415` 把它加入 `visible_calls` 排除集，使 call_id 永不进 `expired_call_ids`。约 2+2 行。
- **P2b token 车道**（有真实风险）：`_frontdoor_compaction_tail_count` 是位置型，无法靠白名单解决。必须在 `_ceo_runtime_ops.py:2158` 之前把 `silent` 的 assistant 行**连同配对 role=tool 行**一起从 `older_history_messages` 摘出，并在 `:2344` 的 `rewritten_messages` 里回插。**配对行必须一起搬**，否则 provider 拒孤儿工具结果。回插位置与压缩块的时序关系是本计划最需要实盘验证的一处。

若 P2b 的语义代价谈不拢，退路是把静默台账借用 §1.4 那条 carry-forward 通道（把 `silent_reason` 写进 `[G3KU_TOKEN_COMPACT_V2]` 载荷逐轮继承）—— 结构上不可能被裁，但真相源变成两处。**默认走 P2b**，退路只在验证阶段被证伪时启用。

### 2.4 网页折叠行（要求⑤）

- 后端：`websocket_ceo.py:893-899` 不再抹 `content`，改为随快照下发 `silent_reply: True` + 原文 + `silent_reason`；"无轨道则整行跳过"的条件放宽为"无轨道**且**无原文"才跳。
- 前端：`hideCeoAssistantText`（`org_graph_app.js:6442-6450`）从"隐藏气泡"改成"渲染一行折叠条：`已静默 · <reason 摘要>`"，点击展开显示原文；折叠交互复用 `setCeoTurnUsageCollapsed`。live 路径 `finalizeCeoTurn:8743-8805`（判定点 `:8751/:8788/:8802`）与历史路径 `renderPersistedCeoAssistantTurn:6987-7037`（`:6994/:7003/:7021/:7036`）两个调用点同步。
- 文案：折叠条只有「已静默」四个字 + reason 摘要，不加解释性句子（`UI copy must be minimal`）。

### 2.5 删两个旧文案出口（要求④）

**代码识别面全部删除**，不留兼容分支：

- `g3ku/runtime/reply_tokens.py` 整个文件退役（`SILENT_REPLY_TOKEN` / `is_silent_reply_token`），7 个 import 点随之清理
- `HEARTBEAT_OK`：常量 `heartbeat/session_service.py:51` 删除；判定 `:1201`、`:1783` 与 `websocket_ceo.py:1124/1126/1136`、`_ceo_runtime_ops.py:8201`、`session_agent.py:3542` 全部换成 §2.2 的工具信号
- prompt 指令改写：`heartbeat/session_service.py:993-1064` 里所有"reply with exactly HEARTBEAT_OK"/"output exactly [G3KU_SILENT]"统一为**"本轮无需对用户说话时，调用 `silent` 工具"**
- `_LEGACY_SILENT_REPLY_TEXT`（`websocket_ceo.py:895` 引用）与其历史兼容分支一并清掉
- 修复轮 `repair_attempt` 相关文案（`:1008-1018`、`:1035-1045`）与 `_visible_reply_requires_repair`（`:1201`）按新信号重写；`_task_terminal_invalid_output_label`（`:702-705`）的"空输出/HEARTBEAT_OK"判读随之调整

配套把 §1.2 的 `:1005` 那句改掉 —— 它现在是**唯一**把模型推向"必须说"的力量，删掉静默文案后必须同时换成窄判据，否则静默工具会像 M1 一样零采纳：

> 仅当本次会话上文里已有一条更晚的回复明确覆盖了同一交付物（把它的 id 填进 `superseded_by`）时调用 `silent`；判不准就正常汇报。

工具 schema 常驻 ⇒ 这条指令放动态附录（心跳前言），实测今天两轮 `stable_prefix_hash` 相同、`dynamic_appendix_hash` 不同 ⇒ **零缓存代价**。

### 2.6 终态 outbox 可靠性（P0，独立于以上全部）

- `delivery_state` 补第三态 `abandoned`：`sqlite_store.py:1937-2047` 一族函数 + `list_pending_task_terminal_outbox`（`:1978-1984`）的取数条件从 `!= 'delivered'` 收窄为 `= 'pending'`。**必须留痕**（`attempts`、`last_error` 不清空），不得静默删行。`mark_task_terminal_outbox_delivered`（`:2040-2047`）现在会把 `last_error` 写成空串 —— 这正是 §1.6 里"原始错误查不出来"的原因，改为保留。
- 常驻补投：复用 `_distribution_reconcile_loop` 的 60s 节拍（`runtime_service.py:7326-7340`），在同一次扫描里追加 `list_pending_task_terminal_outbox` 的重新驱动，**不新起循环**。超 `attempts` 上限（默认 10）落 `abandoned`。
- `_post_internal_callback` 超时 2.0 → 放宽（`runtime_service.py:4976-5023` 的 5 处 `timeout=2.0` 同一常量），并在 `retry_delays` 耗尽后不再靠"下次重启"续命，改由上面的 60s 节拍续。加在途并发上限，避免 web 宕机时 pending 行全部长挂造成 task 堆积。
- 顺带同族的 4 张表（`task_summary_outbox` / `task_worker_status_outbox` / `task_stall_outbox` / `task_distribution_error_outbox`）在 `:812-816` 有完全相同的"只在启动时补投"缺陷。P0 先把 terminal 做对，其余四张是否同步收口留给验证结论。

---

## 3. Implementation Order

| Phase | Deliverable | Commit gate（必须观察到，不靠推断） |
| --- | --- | --- |
| **P0** | §2.6 全部：`abandoned` 第三态 + 60s 常驻补投 + 超时/并发 | 手插一行 `delivery_state='pending'`、`created_at` 为 1 小时前的 terminal 行 → 不等重启，60s 内被拾取并投递成功，行转 `delivered`；把 `internal-callback.json` token 改错 → 连续失败后落 `abandoned` 且 `attempts`/`last_error` 留痕；恢复 token 后**不再**被拾取 |
| **P1** | `SilentTool` 注册 + 三处常驻注入 + 预算/闸门豁免 + §2.5 的 prompt 指令改写与 `:1005` 窄判据 | `tool_schemas` 里出现 `silent`（用户轮与心跳轮**都在**，且 `hydrated_tools` 变化时不消失）；`tool_signature_hash` 相对 HEAD 变化**仅一次**，之后连续两轮恒定 |
| **P2** | §2.2 finalize 识别 + §2.3 `prompt_visible` 翻转与配对基线 + P2a stage 豁免 | 脚本化会话里让模型调 `silent`：(a) 无外发、无渠道投递；(b) 转录行的 `prompt_visible=True`；(c) **下一轮 `request_messages` 里能看到这条行**；(d) 走完一次 stage 压缩后该行仍在 |
| **P3** | §2.3 P2b token 车道摘出/回插 | 构造一个必须触发 token 压缩的长会话 → 压缩后 `request_messages` 中 `silent` 行与其配对 tool 行**成对存在**、provider 不拒、消息顺序无空洞 |
| **P4** | §2.5 删除 `reply_tokens.py` / `HEARTBEAT_OK` 全部分支与 7 个 import 点 | `rg "G3KU_SILENT|HEARTBEAT_OK"` 在 `g3ku/` `main/` 生产代码**零命中**；受影响测试文件按新信号改写 |
| **P5** | §2.4 折叠行 | JS 测试：静默回合渲染出「已静默」折叠条、`row.content` 保留原文、展开后原文可见；**同步修正** `ceo_silent_turn.test.js:324-326` 与 `:373` 两条断言（合同变更，在 commit message 里点名） |
| **P6** | 文档（见 §5） | 跑 `g3ku-architecture-maintenance` 后按其判定落笔 |

P0 与 P1–P6 无依赖，先走。P1→P2→P3→P4 严格串行（P4 必须在 P2 之后，否则中间会出现"两个出口都没有"的窗口）。P5 可在 P1 之后并行。

---

## 4. Verification

Python（新增 `tests/resources/test_frontdoor_silent_tool.py`，命名沿用同目录风格）：

1. **静默成功**：脚本化模型输出一次 `silent(reason=…, superseded_by=…)` → 断言 `message_end` 不外发、`reply_notifier` 未被调、`ext` 会话无 `outbox.jsonl` 新行。
2. **痕迹进上下文**：同一条 → 转录行 `prompt_visible is True` → 下一次请求体的 `request_messages` 里能找到该 assistant 行**及其 `tool_calls`**（这是本计划的核心断言，M3 的 94% 消失率就是它要否证的对象）。
3. **压缩存活**：同一会话分别触发 stage 压缩与 token 压缩，两次之后重判断言 2。这是 P2a/P2b 的独立关卡，必须分别验。
4. **配对不破坏**：静默轮同时调了普通工具 → 该批工具全部执行完、结果行与 `silent` 行都进上下文、provider 无孤儿拒绝。
5. **用户轮生效**：`web:` 会话上用户发一条 → 模型调 `silent` → 网页收到折叠行、QQ/渠道侧无投递；`reason` 在快照里可读。
6. **旧出口彻底失效**：模型输出字面量 `[G3KU_SILENT]` 或 `HEARTBEAT_OK` → **正常外发该文本**（证明不再有字符串识别），这是防回潮的负向断言。
7. **回归面**：`test_ceo_runtime_progress`（138 项，心跳主车道）、`test_heartbeat_task_terminal_root_output`、`test_ceo_frontdoor_context_retention`（`:659-692` 静默回归）、`test_ceo_frontdoor_regressions`（`:511-570`）、`test_shutdown_graceful_pause_resume`（`:608-611`）、`test_task_stall_runtime`、`test_paused_turn_transcript_lifecycle`。
8. P0 的三条见 §3 表。

JS：`node --test tests/resources/*.test.js`（带已知的 `org_graph_task_view.js` 与 `HTML*Element` stub 前置）。

命令口径：`.venv/Scripts/python.exe -m pytest tests/resources/<file> -q` **逐文件**跑（合并跑本机超时）；`.venv/Scripts/python.exe -m ruff check .` 对齐基线。**全量套件在 HEAD 上本就不是全绿**（`test_barrier_draining_reentry`、`test_context_window_overflow` 预检用例单独跑即红；另 2 项只在同跑时红；前端存量 1 红为 `ceo_context_compression.test.js` 的 reduced-motion 断言），**不得以"全量绿"为门禁**，也不得据此报绿。

实盘验收（需操作者亲自重启 worker —— 无 `--reload`，且不带 bootstrap 环境变量重启会落进 423 `project_locked`；代理不得重启）：

9. **端到端复现今天的事故**：手插一行 `task:543e0f15d798` 的 pending 行、`created_at` 填 2 小时前 → 重启 web → 期望：**心跳照常唤醒**（P0 未改唤醒），模型看到陈旧事件，若它判为已覆盖则调 `silent` → **QQ 侧无消息**、转录留痕、下一次用户输入时模型知道自己上次对哪个任务静默了。这一条同时验 §2.0 裁决①与要求②③。
10. **P3 的真实压力**：`ext_qq-official_f8a8001865631301` 是 268 条消息 / 107k token 的活体，跑够轮次必然触发 stage 压缩；判据看新工件的 `request_messages` 里 `silent` 行是否仍在。

---

## 5. Documentation Contract

架构相关（工具可见性与常驻合同、回合收尾、静默语义、转录行元数据合同、两条压缩车道的豁免规则、终态 outbox 投递与接管、操作者排障），P6 必须先跑 `g3ku-architecture-maintenance` 再判定落笔。预期改动：

- `docs/architecture/tool-and-skill-system.md`：新增"常驻内置控制工具"这一类合同（`submit_next_stage` 与 `silent` 是仅有的两个成员；明确 `RESERVED_INTERNAL_TOOLS` 与 `CEO_FIXED_BUILTIN_TOOL_NAMES` **都不具备注入能力**，这是本计划踩过的坑，必须写死免得下次有人照抄 `stop_tool_execution`）。
- `docs/architecture/heartbeat-system.md`：静默出口从文案改工具；`session_service.py:993-1064` 的收尾指令新合同；`:1005` 窄判据；**并写明"推送与否归模型、机器只负责按时送到"这条裁决及其理由**（它约束以后所有想加机器闸门的改动）。
- `docs/architecture/runtime-overview.md`：回合收尾路径（`_graph_finalize_turn` 现依赖 `state["messages"]` 里的工具调用而非输出字符串）；静默行的 `prompt_visible=True` 与基线回填配对；两条压缩车道对 `silent` 行的豁免规则。
- `docs/architecture/web-and-admin.md`：快照的静默行合同（不再抹 `content`、折叠行、无轨道且有原文则仍渲染）。
- `docs/architecture/config-and-models.md`：`tool_signature_hash` 的一次性冷启动代价（M2 的 +0.08%）。
- `docs/architecture/operations-and-maintenance.md` + `README.md`：排障条目 —— "重启后自动回了一条几小时前的旧结果" = `task_terminal_outbox` 滞留行被启动重放，判据 `attempts>0` 且 `delivered_at - created_at > 5min`；以及"模型明明该静默却发了一条" = 查转录里有没有 `silent` 调用行。

---

## 6. Risks and Deliberate Non-Goals

1. **翻转 `prompt_visible` 是本计划风险最高的一处。** `session_agent.py:3559-3564` 的 `False` 是**刻意的**，注释点名与 `_graph_finalize_turn` 基线回填配对。只翻一侧会让转录重放与请求体基线对同一行作出不同判断 —— 这条配对断裂正是 `36da80fe`（纯文本 auto-wrap 伪提交）与 `321604599eb0`（阶段死锁）所在的同一区域。缓解：P2 的断言 2 与断言 3 分别覆盖两条路；P2 不许与 P4 合并进一个 commit。
2. **P2b（token 车道摘出/回插）会改写压缩后的消息顺序。** 位置型切分遇到"摘出再回插"必然产生时序空洞，provider 对 tool 调用与结果的相邻性有要求。若 §4 断言 3 在实盘（§4.10）被证伪，启用 §2.3 的 carry-forward 退路，接受"真相源两处"。
3. **`silent` 全轮生效在渠道轮没有兜底。** 网页有折叠行可展，QQ 用户发消息后被静默则**屏幕上什么都没有**，连"已静默"都看不到。这是口径①（全轮生效）的必然代价，接受；但要在 P6 写进 `external-agent-api.md`，并在 §4.9 专门验一次。历史证据：0 次实盘发生（M1 里成功静默 0 次），所以这条风险没有既有实例可参照，上线后需主动观察。
4. **放宽后"误静默"是重损方向。** 该推却静默 ⇒ 用户永久看不到结果（`_ack_task_terminal_events` 无条件标 delivered，无后补）。工具参数比文案可靠（M1 的 11/11 失败是纯格式问题），但工具**不会**替模型判"该不该推"。§2.5 那句窄判据是唯一的对冲，它属于 prompt 层、无强制力。明确接受，不加机器兜底 —— 那与 §2.0 裁决①冲突。
5. **不新增 config 开关**（silent 是否可用、是否全轮生效、压缩豁免名单）。回滚粒度靠 commit：P1 一个 commit 回滚即退回 P4 之前的旧出口（旧出口在 P4 才删，这个顺序就是为了留回滚点）。
6. **不做**：心跳事件的入队/唤醒语义改动；按时效丢弃陈旧事件；"入队但不唤醒"；静默台账在心跳前言里每轮重注入（被 §2.3 的压缩豁免替代 —— 两者解决同一个 M3，只保留转录行那条，避免同一事实两个真相源）；`stop_tool_execution` 的常驻修正（与本计划无关的既有问题，另案）；`china:*` 归档的渲染改动；`bridge.py` / `client.py` 任何一行；把 P0 的模式推广到另 4 张 outbox 表（先验 terminal）。
7. **M3 的 94% 消失率里包含大量本来就该消失的行** —— 这个数不能读成"94% 的痕迹丢了"，它衡量的是压缩正常工作的强度。它的唯一作用是证明"裸 tool_call 行不是可靠的承载"，P2a/P2b 因此不是可选优化。
