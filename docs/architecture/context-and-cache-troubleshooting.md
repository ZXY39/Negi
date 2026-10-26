# Negi 上下文与缓存排查指南

本文档用于指导维护者排查以下两类高频问题，并为后续修改节点上下文策略提供约束与检查清单：

- prompt cache 命中异常下降
- 上下文在非预期时机缩短、重排或丢失

它把已经踩过的坑、有效的排查方法、以及仍需重点验证的边界整理成可复用的维护手册；排查结论一律以 actual provider request JSON 为权威。

## 1. 先分清你看到的到底是什么问题

### 1.1 缓存命中下降，不等于输入 token 下降

排查时先把计费行拆成三部分看：

- `Input Tokens`
- `Cache Read Tokens`
- 二者之和代表本次请求的总可计费输入规模

常见误判：`Input Tokens` 变低就以为上下文变短了；其实也可能只是 cache hit 变差，更多 token 从 cache read 变成了重新计费。

正确做法：

- 先比较 `Cache Read Tokens` 是否显著下降
- 再比较 `Input Tokens + Cache Read Tokens` 是否真的缩短
- 最后再决定这是“总上下文缩短”还是“总上下文近似不变但前缀复用消失”
- 对 OpenAI 风格 `/responses` usage，`input_tokens_details.cached_tokens` 这类嵌套字段是总输入的内部分解，不是额外的计费通道；比较前先把 `input_tokens` 归一化回未缓存通道。

### 1.2 caller-side family churn，不等于 provider request 前缀断裂

要把五类 hash 分开看：

| hash | 含义 |
| --- | --- |
| `prompt_cache_key_hash` | caller-side prompt cache family |
| `stable_prefix_hash` | 稳定前缀是否变化 |
| `dynamic_appendix_hash` | 动态尾部是否变化 |
| `actual_request_hash` | 真实 request 是否变化 |
| `actual_tool_schema_hash` / `tool_signature_hash` | provider-facing tool schema 是否变化 |

排查结论要分类：

- `prompt_cache_key_hash` 变了：优先怀疑 stable prefix 或 tool schemas 变了
- `prompt_cache_key_hash` 没变，但 cache 命中掉了：优先怀疑 actual request 前缀断裂
- `stable_prefix_hash` 没变，但 `actual_request_hash` 变了：通常是 dynamic appendix、request scaffold 或同 turn append-only 逻辑出问题

## 2. 先看哪份数据，后看哪份数据

### 2.1 最权威的是 per-request JSON，不是 transcript

CEO/frontdoor 的 provider-facing request 以 `.g3ku/web-ceo-requests/<session>/...json` 为准（路径细节详见 `web-and-admin.md`「Actual Request Debugging Contract」）。优先看：`request_messages`、`tool_schemas`、`provider_request_body`、`usage`、`frontdoor_token_preflight_diagnostics`、`frontdoor_history_shrink_reason`、以及 §1.2 的各组 hash。

原因：

- transcript 只说明用户和 UI 看到了什么
- session snapshot 说明 runtime 当时记住了什么
- 但真正发给 provider 的顺序、schema 和 transport payload，只有 actual request JSON 才是权威

### 2.2 `provider_request_body.input` 比 `request_messages` 更接近最终 transport truth

如果 provider adapter 提供了 `provider_request_body`，排 cache miss 时优先用它校验。特别是 OpenAI `/responses` 路径，要看 `provider_request_body.input`、`provider_request_body.tools`、`provider_request_body.parallel_tool_calls`。

- 两份 adapter 的副本形状不同，别把差异读成"body 没落"：Responses 路径带完整请求体（`input` / `tools` 等），Chat Completions 路径带的是**参数面副本**（`model` / `max_tokens` / `temperature` / `reasoning_effort` / `tool_choice` / `parallel_tool_calls`，工具只记 `tool_count`），正文与工具束本体在 actual-request artifact 的 `request_messages` / `actual_tool_schemas` 里。所以 Chat 路径上 `provider_request_body.input` 缺席是正常形状，不是证据缺失。
- 取"这一跳声明了多少输出上限/哪个档位"必须按两条协议各自的键名读：Chat 是 `max_tokens` 与平铺 `reasoning_effort`，Responses 是 `max_output_tokens` 与 `reasoning.effort`。单一来源在 `g3ku/providers/base.py`（`provider_body_output_cap` / `provider_body_reasoning_effort`）；只读 Chat 键名会让 Responses 跳的 `sent_max_tokens` 恒为 `None`，据此做的截断判档全部落 `unknown`。
- 台账逐跳另存 `sent_max_tokens` / `sent_reasoning_effort` 两个标量（未上报为 `None`，与合法的 `0` 分开）。判"这一跳是被声明的输出上限截断、还是被窗口剩余挤掉"靠的就是它们与回执 `output_tokens` 的关系，不依赖 `context_window_tokens`（详见 `config-and-models.md`「Frontdoor Context Window Contract」）。

- 如果 `frontdoor_token_preflight_diagnostics.final_request_tokens` 已经接近 `effective_trigger_tokens`，但 usage 还是明显更高，优先按“估算偏小”排查，不要先怀疑 cache family churn。

常见情况：`request_messages` 看起来公共前缀很长，但 `provider_request_body.input` 的第一个分叉点更早——说明高层 projection 没问题，真正的问题出在 adapter 最终 payload。

#### 线体角色合同：system 至多一份、且只能在首位

一条 provider 请求里 `system` 至多一份，且只能占首位。严格 OpenAI-compatible 网关对违规直接 400（`The input messages must contain no more than one system message. And the system message, if exists, must be the first message.`，`InternalError.Algo.InvalidParameter`），而这类故障按请求形状归类：不换 key、不占重试预算，直接推进模型链——链里只有一个配置时回合就此终态。合同在两条车道共用的发送咽喉点 `chat_backend.sanitize_provider_messages` 上强制：第一条之后的任何 `system` 都改写成 `user`，正文与位置都不动，所以装配产物、`provider_request_body` 读数与实际线体三者同源。同一份列表里出现两份开头连续 system 时，第二份同样降级为 `user`。

运行时注入的非首位块因此一律以 **user 角色**产出：前门与节点的 `frontdoor_runtime_tool_contract` / `node_runtime_tool_contract` 尾块、心跳/cron 的规则行与事件束，以及节点的动态契约尾块与活状态块。不用 assistant，是因为模型会把块当成"自己上一轮说过的话"、在续写位置仿造回显；不用 system，就是上面那条线体不变量。独立 helper 请求（token 压缩、分块归并、重复预检等）把 system 放在自己的 index 0，符合同一合同。

识别端 `is_frontdoor_tool_contract_message` / `is_node_dynamic_contract_message` 按 assistant/system/user 三种角色认块：存量 durable baseline、continuity sidecar、续跑 seed 与 actual-request scaffold 里仍带旧角色的块既不重复也不丢，出咽喉点时统一改写为 user。取证时 `request_messages` 是改写前的装配视图，可能仍显示 `role=system` 的中段块；判断会不会被供应商拒要读 `provider_request_body`。

内部规则行（`metadata.internal_prompt_kind` 为 `heartbeat_rule` / `cron_rule`）不是本轮用户内容。判"末位是不是本轮 user 回合"的地方（`_history_has_current_user`、`_transcript_has_current_user`、继承路径的追加闸门）都先经 `web_ceo_sessions.trailing_turn_record` 跳过它——否则本轮事件束被判定"已在历史里"、永不追加，模型收不到触发这一轮的那件事。事件束行（`heartbeat_event_bundle` / `cron_event_bundle`）确实代表本轮内容，同回合重投仍由它挡住重复追加。

Responses 协议（`/responses` 路径）对角色之外的内容是**位置保持**语义：adapter（`responses_protocol_helpers._convert_messages`）只把数组开头连续的 system 并进 `input` 头部那一份 `[SYSTEM]…[END SYSTEM]` user 项（`\n\n` 分隔；顶层 `instructions` 会被代理到 Chat 后端的供应商整单拒绝，字段口径见 `config-and-models.md`「`llm_config` 子系统」），其余消息留在原位发成独立项，`openai_chat` 同样原样透传。两条车道因此共用一条不变量：运行时工具契约落回自己的位置。取证要核对的是"块在不在原位、有没有被后写覆盖丢失"，不是"块在不在 `input` 里"。

运行时块的落位本身就是缓存边界。前门把运行时工具契约作为数组**末尾**的 user 消息发出，位置保持使它排在整段历史之后，只重写请求尾巴；同样的块放进 `input[0]`，改动点之后的整段存活上下文都按新输入重计费。塌陷跳的代价按字符深度量而不是按改了几条消息量：`cache_hit` 落在首个改动字符的 token 位置（实测两条独立会话，断点第 12,797 字符 → `cache_hit` 10,240；第 22,992 字符 → 15,232），断点之后的历史整段进入 `input_tokens`。

Memory guard 维护要点：

- frontdoor 与节点的 actual-request 持久化都带 memory guard：正常路径完整保留 `request_messages`、`tool_schemas` / `actual_tool_schemas` 与 adapter-final `provider_request_body`；节点侧正常路径直接写专用 JSON，只有该写入本身命中 `MemoryError` 才降级。
- 若 artifact 带 `artifact_persistence_mode=memory_guard_degraded` 或 `memory_guard_minimal`：它仍能证明这次 send 发生过，但 `provider_request_body` 可能故意为空，`request_messages` 可能是剥离后的取证摘要（大请求内容可能移入 `request_messages_summary` / `provider_request_body_summary`）。
- 此时不要仅凭 `provider_request_body.input` 缺失就断言“provider 从未看到图像”；应视为“artifact 写入成功，但原始 transport payload 为避免 turn 级 `MemoryError` 被省略”，再与相邻 artifact、usage、transcript 时间线互相印证。

Disk guard 维护要点（写保护契约本体见 `runtime-overview.md`「磁盘写保护与治理」）：

- 节点侧 actual-request 写入前还有一道应急写预算：磁盘剩余低于紧急线时跳过 full/degraded payload，只写 minimal 形态（preview 标 `disk-emergency minimal`、降级 reason `disk_emergency_minimal`）。它同样能证明 send 发生过，但消息内容是剥离到极致的摘要；与 memory guard 降级的区分只看 `artifact_persistence_mode` / preview 标记。
- **actual-request 每 (task, node) 只保留最新一份**：新快照创建后即删同节点旧快照（文件+DB 行）。取证时拿到的永远是该节点最近一次真实请求体；更早轮次的请求序列不留存，需要历史对比时只能靠 `task_model_calls` 行的 usage/计数元数据。这条剪枝在每次模型调用上都要走一遍，所以它按 `(task, node)` 取行（`list_artifacts_for_node`），不读整任务清单——后者会把该任务几百行 artifact 逐行建模。 **正文查重同理只看哈希**：`create_text_artifact` 判"这份正文是否已存过"走 `list_artifact_dedupe_rows`（SQLite `json_extract` 取 artifact_id / path / content_hash / size_bytes / content_encoding）→ 比字节数 → 比 `content_hash`，命中才验文件在不在并回表取那一行。`content_hash` 为空的存量行由 `_legacy_content_hash` 读一次正文，哈希记在进程内 `_artifact_hash_by_id`，同进程不复读。这条链原来实盘占事件循环约 35%（逐候选回读正文 + 整任务建模），是回合闸 lag 轴那些秒级停摆的来源。
- actual-request 及其他 artifact 超过大小阈值即以 `.gz` gzip 形态落盘（记录 `content_encoding='gzip'`、`size_bytes` 为原始字节数）。取证读取一律走 `artifact_store.read_artifact_text`（或手工 gunzip）；对 `.gz` 直接 `read_text` 得到乱码/解码错误不是“artifact 损坏”的证据。
- 排查窗口内 artifact 完全缺失时，先查 `write_failure_disk_full` 计数与 worker 日志的 SQLITE_FULL 行再下结论：磁盘满会让可降级写被预算跳过、关键写抛 `DiskFullError`，缺失属于“落盘被治理策略放弃”，不是“send 未发生”。

### 2.3 计费行与 actual request JSON 可能并不总是一一对应

已踩坑：同一时间窗里 usage/billing 出现了 `/responses` 调用，但对应窗口没有落地 `frontdoor-request-*.json`；你以为某个 persisted request 对应某条计费记录，实际那条记录可能来自另一条隐藏/漏落盘请求。

维护要点：

- 区分 “artifact 缺失” 和 “artifact 被 memory/disk guard 降级”：只要文件存在且带 `artifact_persistence_mode!=full`（或 preview 带 `disk-emergency minimal`），它仍能证明这次 send 发生过，只是不能再逐字节证明完整的原始多模态 payload。
- manual pause、pause 后新 turn、internal/hidden round 这些窗口，必须先确认 artifact 是否完整。
- 每次怀疑缓存异常时，先检查 artifact 时间线是否完整；如果 artifact 不全，先修 artifact，再改上下文策略，再信任何 cache 结论。

## 3. 已经确认踩过的坑

### 3.1 同 turn 的 append-only 规则被破坏

- 坑：旧 contract snapshot 被提前剥掉，新 round 直接从 stripped body 继续，provider 前缀在第 3 条就断；或 overlay 被错误拼回已有消息，而不是作为新的 request-tail 消息追加。
- 不变量：同一 visible turn 内 request 只增长、不重排；`request_messages` 在真实 transcript（system/user/历次 assistant 工具调用 + 工具结果）上保持 append-only，命中前缀逐轮变长。请求尾部区域至多 1 份 turn-only note，尾部不带 `## Runtime Tool Contract` 整条消息；静态声明在头部快照（`## Runtime Contract (pinned)`），变化由一次性 `[RUNTIME_NOTICE]` 投递；存量 transcript 里的合同消息按识别函数在装配/持久化时剥离（`upsert_*` 只剥离、不追加）。稳定块只放"这一跳新发生或可推导出来放不下的事实"：待修复条目行、附件句柄行、声明滞后差集 `undeclared_candidates`；整回合不变的处置文案（待修复处置顺序、附件 reopen 规则、无权限名单的处置语）一律住稳定提示词，能从 `tools[]` / `callable_tools` / `denied_tools` 推出来的候选名单不进正文，`contract_revision` 整份请求只出现一次。这条边界是可量的：逐跳差分 `## Runtime Tool Contract` 正文，就能算出"恒定或可推导"那几段占尾块的字节份额。旧的三项拆分（候选整表行 / 待修复处置文案 / 重复 revision 的字节数）量的是候选整表仍在尾块时的那份形状，与现在的块不可比，所以这里不带数——要按现形状重新量，别拿旧数当既成事实。原因：把带活状态的块锚在携带正文中间（例如最新 user 消息之前）会让该消息之后的整段同 turn 正文从那里断缓存——实测契约落在 index 2 时每次重写只剩契约之前的前缀命中，其余整段重传。尾块可能诱导模型回显抬头，这条风险由识别 + 修复道兜住：`is_frontdoor_tool_contract_echo_text` / `is_node_dynamic_contract_echo_text` 判回显，`tool_contract_echo_attempt_count` 计数，修复 overlay 要求只输出用户可见正文，并以这一节点的交付目标收尾（`react_loop._node_goal_anchor_text` 取 `goal`／`prompt` 前 160 字），让模型有一个能核对的对象；回显文本不得进入用户可见正文，而携带工具调用的回显消息不得当成契约剥离（会留下孤儿工具结果）。存量 transcript 里的契约块以 `## Runtime Tool Contract` 开头的 user 消息形式出现（角色理由见本文「线体角色合同」），装配时整条剥离；剥离旧契约 / turn-only note 不算非法 shrink，也不算前缀断裂。durable baseline / continuity 持久化前必须剥掉契约 summary，以及 `## 长期记忆` 这类当轮 overlay 的 assistant 记录（它们只在当轮请求可见，落史会逐轮累积）；后续轮若把旧 summary / overlay 当普通稳定历史重放而没有 `token_compression`，按非法上下文携带排查。节点同 turn 请求同样走 append-only scaffold：上一请求 body + 上一轮 assistant/tool delta + 当轮一次性通知与 turn-only note（末位是 user 角色提示）。
- 症状：命中前缀不逐轮变长；前缀在契约或 overlay 位置提前分叉；旧契约出现在历史中段；或模型最终文本 / 渠道消息以 `## Runtime Tool Contract` 开头。
- 不变量（send preflight 的 append-only 比对）：比对两方必须在同一投影下取值——已发出的真实请求与下一轮请求（由 durable baseline 拼装）都要先剥契约 / 回合内 note，再剥 `## 长期记忆` 这类每轮重新生成的动态块，然后做前缀比对。真实请求带当轮动态块、durable baseline 不带，两侧投影不一致会让前缀恒不等，`comparable_to_previous_request` 恒为 false，usage-first 估算静默退化成全量 preview（空闲 composer 预估表现为长期偏低且不随真实请求增长）。同一份最新 actual request 还必须是重建种子（`_frontdoor_seed_actual_request_record`）：fresh visible turn 开始才把实时轨迹搬进 previous 槽位，而空闲 composer 预估发生在回合开始之前，只认 previous 槽位会种到上一轮之前的陈旧请求上，比对随之失效。
- 不变量（锚点与压缩投影同源）：让两侧可比的那次 trim 若真从上一跳正文里移除过消息，上一跳的真实 usage 就仍含着那份已被裁掉的肉身，锚点必须在同一投影下按「未裁体量 − 已裁体量」扣回（差额记在 `anchor_projection_shrink_tokens`，扣完的锚点即 `effective_input_tokens`）。判据是 `usage_based_estimate_tokens == effective_input_tokens + delta_estimate_tokens` 恒成立。不扣的表现为可比性判真而读数停在裁撤前的大小：过期点那一跳高报数倍、下一跳才回落，且触发发送前压缩的是同一个数，高占用会话会被顶得提前压缩。扣回只发生在 trim 真正移除过消息的跳上，其余跳锚点等于真实 usage；读数在压缩边界冲高而 `comparable_to_previous_request` 仍为 `true` 时，先看 `anchor_projection_shrink_tokens` 是否为 0，再怀疑 estimator。

### 3.2 assistant 空文本 + tool_calls 被当成“空消息”丢掉

- 坑：request-body seed 重建时按“空 content”过滤，tool-call 结构记录消失；下一轮文本看似还在，request shape 已变，缓存前缀断裂。
- 不变量：带 `tool_calls` 的 assistant 记录、带 `tool_call_id` / `name` 的 tool 记录必须保留；不能用“文本为空”判定它们是否可删。
- 症状：baseline 复用后 provider request 缺失历史 tool-call 结构；前缀在丢失位置分叉。

### 3.3 prepare-only planned request 抢走了真实 baseline

- 坑：manual pause / no-provider turn 只走到 prepare 或 paused snapshot，却把 session-owned baseline 覆盖成 planned body；下一轮 fresh turn 直接从“未发出过的 baseline”续，cache 前缀大面积消失。
- 不变量：只有带真实 actual-request 证据的状态才允许覆盖 cross-turn baseline；planned prompt-cache diagnostics 不是 actual-request 证据。
- 症状：没有真实请求发出的下一轮，第一跳 request 形态与上一轮完全对不上。
- 边界：no-provider turn 不覆盖基线，不代表被暂停回合的用户消息应缺席下一轮——它由 prepare 阶段的种子转录对账补回续跑种子，见「Baseline 合同与恢复顺序」。

### 3.4 finalize 没把 direct reply 补回 baseline

- 坑：finalize payload 没重复带 `frontdoor_actual_request_path/history`，同步逻辑误判“没有 actual-request 证据”，final assistant reply 没进下一轮 baseline。
- 不变量：当前 turn 已有真实 provider request 落盘后，`finalize` 阶段产出的可见最终回复必须补回 session-owned baseline；该规则不限于 `direct_reply`，普通工具（如 `message`）之后产出的用户可见 `final_output` 同样要补回。该规则同样覆盖 heartbeat/cron 内部回合的可见回复：补回与否按回复内容判定，不看回合类型；调用 `silent` 的回合豁免——它的正文已经随带 `tool_calls` 的那行助手记录进了基线，再补一次就是同文两份。“同 turn 已有 actual-request 证据”与“fresh turn 没有 actual-request 证据”是两种不同状态，不能用同一条覆盖规则处理。
- 症状：transcript 里能看到上一轮最终回答，但下一轮第一跳 request body 里完全没有它。

### 3.5 普通 fresh-turn 第一跳没有沿用上一轮 actual request scaffold

- 坑：fresh turn 直接从 stripped body + 新 user + 新 contract 开始，第一条 provider request 在低索引处就和上一轮分叉；或第一跳要求“投影整体与 seed 逐字节/结构等价”——投影（压缩块、工具正文外部化、归档 notice tail 重渲染）与发送侧形态在中段必然分叉，等价校验恒失败，跨 run 唤醒每次静默降级成重组装小请求：命中钉死在静态系统头、`comparable_to_previous_request` 长期 `false`、小请求还会覆盖唯一的 actual-request artifact 污染重启种子。
- 不变量：durable baseline 可保持 stripped/finalized 形态，但普通 fresh-turn 第一条 provider request 以 persisted actual request scaffold 为前缀（`_adopt_fresh_turn_seed_scaffold` adoption）：seed 取 artifact 的内部形态 `request_messages`（`provider_request_body.input` 仅 legacy 兜底——/responses wire 项无 `role`，回灌 adapter 会被静默丢弃）；seed 剥契约/turn-only note/图像 overlay 尾迹后做头探针（前两条归一化相等，容差止于行尾空白/换行）+ 多锚点尾对齐（以 seed 尾部记录为锚在投影末段自尾向前找覆盖点），投影超出覆盖点的记录（held notice、恢复重放、当前 user 回合）即显式 delta，请求 = seed + delta + 新契约/note 尾部。seed 不可用（缺失、guard 降级、头漂移、对齐失败）才允许回退投影重组装，且必须落 `request_seed_source=fallback_*`——静默回退按 bug 处理。scaffold 只是 request-construction aid，不能反过来变成新的 durable source of truth。
- 症状：`request_seed_source` 持续为 `fallback_*`；或每个新 turn 第一跳都在低索引处与上一轮分叉。

### 3.6 跨普通 fresh turn 的 tool schema churn

这类问题会直接造成 `prompt_cache_key_hash` 变化，因为 family key 会把 stable prefix 和 tool schemas 一起算进去。

- 坑：相邻两轮 stable prefix 没变，但 provider-visible `tool_schemas` 发生不必要变化（某工具在后一轮出现或消失，例如 `message`），family key 跟着 churn；哪怕 request body 还能复用一部分前缀，caller-side family 也已经断了。
- 字段语义：`tool_names` 是当前轮权威 callable 合同；`provider_tool_names` 是这一跳实际发给 provider 的 schema bundle；`pinned_provider_tool_names` 是"若此刻重新播种会得到什么"的钉住集，节点压缩重印跳取的就是它；`provider_tool_bundle_mode` 取 `pinned_seeded`（首跳播种）/ `pinned_recommitted`（重印边界）/ `pinned_frozen`（钉住中），节点道除选择器载荷外还逐跳写进 `task_model_calls` 行与 actual-request 工件（与 `provider_tool_bundle_seeded` 同一批字段）；`provider_tool_exposure_revision` 标识该次 send 实际到达 provider 的 active bundle；`pending_provider_tool_names` 与 `provider_tool_exposure_commit_reason` 是兼容字段，新写入保持 `[]` / `""`。
- **数换束次数只能按 `provider_tool_names` 逐跳差分（或 `provider_tool_exposure_revision` 变化）**，不能用 `provider_tool_bundle_seeded`：该字段记的是"选择器这一跳重铺了一次 bundle"，当它从 frame 读到的 prior 为空时也会置真，而重铺出来的清单常常与上一跳逐字相同（实盘 73,072 跳里 seeded=True 共 1,364 次，其中 1,174 次发出的 bundle 没有任何变化，绝大多数落在 `request_seed_source=scaffold_seed_with_delta`）。同一窗口里按逐跳差分的真实换束是 190 次，且**顺序变化 0 次**——bundle 一直按稳定顺序发出，没有隐藏的顺序轮换。
- 不变量：清单来源与刷新点的合同本体在 `tool-and-skill-system.md`「Provider Tool Surface」。缓存侧只按这一条读：两条车道的普通跳都必须逐字节沿用上一跳清单原文，成员变化只有两个合法来源——这一跳落在压缩类重印边界（内联压缩 `token_compression`；前门另加手动压缩）、或这一跳是编辑重发/Fork 截断到会话开头之后的第一跳（空基线把钉住声明一起撤掉，那一跳按当轮 RBAC 全集重新播种）。当轮新增能力不改成员；曝光收窄（候选塌缩、LRU 淘汰 callable）与 RBAC 收回都不改变成员——收回只关掉执行准入，参数表由声明侧继续供着、`denied_tools` 由一次性通知点名；实测一条活节点连续 7 跳 `tools[]` 恒为同一份 22 条，而 `callable_tool_names` 在 6–8 之间波动；前门一份会话收回 `web_fetch` 后的两跳仍带着同一份 32 条清单，只有这两跳的通知多出 `denied_tools`。**资源被删除同样不改变成员**：删除流程在实例消失前抄下该工具的 schema（`g3ku/resources/retired_schemas.py`），渲染侧用它继续发出同一份 `tools[]`，通知照旧点名——删除与收回在缓存上是同一类事件。名字真的离开清单只可能是没留过快照的资源卸载，或重印边界；把"删了就少一条"当成正常现象去查装配，会追错方向。
- 重印边界只有压缩类：压缩那一跳正文已被整段重写，重印免费。**空闲时长不是重印边界**；两次重印之间的策略/临时目录/denied/candidate/repair/附件变化由单生产者投一条 `[RUNTIME_NOTICE]`，比"到点重付整段头部"便宜得多。会话道上尤其如此：久不活跃的那一跳命中本来就低，按时间重付整段头部换不来收益。
- 症状：没有真实 membership 变化却在同一轮内观察到 provider-visible `tool_schemas` 切换（或 `provider_tool_exposure_revision` 在 desired 集不变时变化）——直接按 runtime bug 排查。存量快照里的 `history_shrink_reason=stage_compaction` 那一跳出现新的 `actual_tool_schema_hash` 同样按回归读：它不是清单重印点，而写入侧也不产出该值。名字还在清单里而这一跳的参数表换了来源，读 `model_visible_tool_selection_trace` 里整表那一组字段定位：`bundle_ref` / `bundle_revision`（这一跳字节源的身份；revision 变了说明整表被重写过，而 singleton 的 ref 就地覆盖、从不随内容变，所以只有 revision 能证明换过）、`bundle_compat_rebuild`（整表缺后来新增的成员或读不出来时的兼容重建，每个在飞会话/节点最多一次）、`bundle_fault`（整表读不出或写不出，非 0 说明磁盘/内存保护正在侵蚀这份载体）、`bundle_placeholder_names`（这一跳定影成占位的名字——占位与上一跳的实例字节必然不同，但它不删名）；开关关闭那一路还多三个逐名回填字段 `declaration_schema_placeholders` / `declaration_schema_ref` / `declaration_schema_ref_fault`。前门侧看工件里的 `frontdoor_tool_schema_hits` / `frontdoor_tool_schema_fallback_opportunities`，节点侧看 `model_visible_tool_selection_trace.schema_source_hits` / `schema_source_fallback_opportunities`：分母是"名单里但这一跳需要兜底"的名字数，没有它，命中 0 分不清"这档没机会"与"这档没用"。判"名单与顺序都没动、字节却变了"看每跳的三枚 wire 读数（`wire_tool_schema_hash` / `wire_tool_name_sequence_hash` / `wire_tool_schema_item_hashes`；不一致时 WARNING `tools[] bytes changed outside a reprint boundary` 并点名变化的名字）——`actual_tool_schema_hash` 由 `_tool_signature_hash` 算，签名按名字排序之后才哈希，**它对 `tools[]` 的顺序是盲的**，顺序变化只有序列哈希看得见。首跳与上一跳没有这三枚读数时不比对：**没有可比对象不等于通过**。
- 工件里的 `tool_schemas` 与线上发送的那一份是同一数组：发送预检的入参就是渲染器渲染出的 wire，它不自己按名单另渲一份参数表，所以 `actual_tool_schema_hash`、`tool_signature_hash`、token 估算与 `build_actual_request_diagnostics` 记录面读的都是发出去的那一份。口径字段 `tools_record_basis` 逐跳记 `wire`；读侧遇到没有这个字段的存量记录按 `proxy` 处理（那是预检另渲的那份，比线上少名单尾部那颗合成控制工具 `silent`）。**跨 `proxy`→`wire` 的那一跳两侧哈希不可比**：`actual_tool_schema_hash` 变了不代表清单换了束。增量估算据此放行——`_frontdoor_append_only_delta_estimate_tokens` 在两侧口径不一致时跳过"工具哈希相等"这一条比对，其余可比条件不放宽。放宽是必需的：`comparable=False` 把那一跳的 usage-first 估算整条作废、退化成一次全量 preview，并连带影响 `estimated_total_tokens` → `required_context_window_tokens`（路由成员筛）与压缩触发判定，一条纯口径变化不该让这些下游跟着动。`prompt_cache_key` 对 `tool_schemas` 不敏感（由 `test_prompt_cache_key_is_insensitive_to_tool_schemas` 钉住），所以记录面对齐不产生缓存断点。
- 归因顺序（零命中跳先做完这三步再怀疑上下文形状）：把这一跳与上一跳的 `actual_tool_schema_hash`、`provider_tool_exposure_revision`、稳定前缀哈希（节点 `model_prefix_hash` / 前门 `stable_prefix_hash`）逐项对齐，**三者全同而命中为 0** 的跳归网关侧，不往装配路上找。实盘读数：节点 `same_turn_chain` 零命中 112 跳里 109 跳（97.3%）三者全同；前门一条真会话 4 个零命中跳同样三者全同（其中一跳跳间隔 4 小时、一跳 20 秒），而相邻命中跳在 2–6 秒间隔上是 88–97% 命中——命中丢失与本地形状、也与跳间隔都没有稳定相关。另注意 `prompt_cache_key_hash` 会随稳定前缀一起变：前缀一变 key 就变，按 key 分区的网关会把这一跳判成全量未命中。

### 3.7 manual pause 之后的新 turn，与“暂停回复”不是一回事

- 坑：把“pause 后用户发的新 turn”误判成“暂停回复”，错误地拿 pause ack 或 paused snapshot 去解释 cache miss。
- 不变量：`已暂停` 是上一轮的 pause ack；pause 之后用户再发消息是全新的 visible turn，按 fresh-turn continuity 规则排查。对普通用户 manual pause，后端会把当前轮 terminalize 成 `completed`；若还在按长期 paused/resume 语义排查，结论通常会跑偏。
- 症状：用错恢复源，第一跳形态与真实上一轮请求对不上。

### 3.8 重启后继续，不能退回 transcript/history fallback

- 坑：项目重启后 reopened completed session 直接从 transcript/history 重建；`frontdoor_request_body_messages`、actual-request scaffold、turns/compression 状态没有按最新 authoritative state 恢复，表现成“上下文像还在，但第一跳 cache 命中突然掉光”。
- 不变量：恢复顺序见 §4 的 baseline/restore 顺序；sidecar 必须携带可用 baseline 才算获胜。节点侧同理：节点重启/resume 后第一条重建 provider request 必须继续使用持久化的 append-only scaffold，即使 durable `runtime_frame.messages` 更短（`runtime_frame.messages` 只是 projected-history 视角，`actual_request_ref.request_messages` 才是第一跳重建的权威）。若恢复出的 `frontdoor_actual_request_path/history` 缺失、不可读或 body 不匹配，用最新匹配的 actual-request artifact 做 trace enrichment：只修复 request-trace 权威，不得用 artifact 的完整 same-turn scaffold 替换恢复出的 baseline。
- 边界：**进程重启不是清单重印点**。续跑第一跳借用上一跳的 family/schema 锚（`provider_tool_schema_names` / `cache_family_revision`）的判据只有一条——上一跳的 actual-request trace 带得起来（`_recompute_frontdoor_completed_continuity_bridge_pending`：artifact 路径在盘、名单与曝光锚非空）；当轮 visible 集与侧车里那份**不比对**。停机期间新增或删除 skill/工具都不改成员：新能力由尾块 `undeclared_candidates` 点名、参数契约由 load 返回承载，收回与下架由执行准入加 `denied_tools` 承担（合同见 `tool-and-skill-system.md`「Provider Tool Surface」）。钉进头部的静态声明原文与它的重印代数一起落侧车（`pinned_contract_text` / `pinned_contract_revision` / `pinned_contract_skill_ids` / `skill_roster_epoch`），恢复时同时写回会话属性与进程表两份载体，判据键对得上就逐字节复用原文——头部是第一条消息，重渲一行就让身后全部存活正文重付。钉住整表的参数表载体同批恢复：侧车只带 `pinned_tool_bundle_path` + `pinned_tool_bundle_revision`，按这对值从 `.g3ku/web-ceo-tool-bundle/` 读回整表（本体不进侧车；载体与判据见 `tool-and-skill-system.md`「Provider Tool Surface」），所以停机期间被删除的工具在续跑第一跳仍由整表供出删除前的字节，而不是从名单里掉出去。
- 节点道同一条判据有两处耐久载体：帧里的 `model_visible_tool_selection_trace['pinned_head']`（钉头原文 + revision + 名单 + roster epoch，进程载体为空时才读回，合同见 `tool-and-skill-system.md`「Pinned Static Declarations」）与逐跳台账/工件里的 `provider_tool_bundle_mode`。后者的判读形状：`pinned_seeded` 只允许出现在链首与重印边界那一跳；**链中间某一跳名字少了一名而 `provider_tool_bundle_mode` 仍是 `pinned_frozen`**，说明少的那个名字在上一跳写进帧的那份 prior 里就已经没了——shrink 发生在帧的写回路上（历史形态：自动水合进来的名字随派发字典塌缩被过滤掉），往写回点找，不要往执行准入找。
- 读数：重启那一跳的 `stable_prefix_hash` 与 `actual_tool_schema_hash` 应与上一跳逐项相同。任一项不同就说明钉住内容没被带过来，先查侧车里那四个键在不在（侧车载荷是白名单装配的，键没列进 `_normalized_completed_continuity_snapshot` 就永远落不了盘），再查 baseline 是否被 trace enrichment 换过；不要读成"缓存本来就保不住"。实盘一条前门会话：停机期间删掉一个探针 skill 与一个工具，那次重启的第一跳头部少一行名单、清单少一名，该跳 input 10,452；同会话下一跳 2,281；作为对照的另一条会话只重启、能力集没动，第一跳 input 835。
- 症状：重启后第一跳 request 明显变短却没有合法 shrink reason。

### 3.9 节点 selection cache / frame restore 漂移，不等于 prompt 自己丢了 skill

常见于 execution / acceptance 节点：

- 坑：节点某一轮先拿到空的 `contract_visible_skill_ids` / `candidate_skill_ids`，之后外部 resource/governance refresh 已把 skills 或 tools 刷进 live runtime，但节点仍沿用旧的 node-context selection cache 或旧的 persisted frame restore。
- 不变量：节点运行时在复用 cached / restored selection 前，必须至少比对 `session_key`、`actor_role`、`visible_tool_names`、`contract_visible_skill_ids`、`registry_skill_ids`；任一字段漂移就丢弃旧 selection，回到 `_node_context_selection_inputs()` 与 `build_node_context_selection(...)` 重算。“旧 frame 里是空 skill 集”不自动代表“下一轮也应继续空”，必须先确认 live visibility 仍为空。
- 症状：`load_skill_context(skill_id="...")` 明明应可用却一直报当前候选技能未包含目标 skill；`runtime frame` / `runtime-frame-messages` 里 `candidate_skill_ids` 连续多轮不变，而 `visible_tool_names` / `contract_visible_skill_ids` / `skill_visibility_diagnostics.registry_skill_ids` 已能看到新值。这属于 runtime freshness gate / cache invalidation 回归，不是 prompt wording 问题。

### 3.10 heartbeat / cron 按普通 continuation shrink 规则排查

Heartbeat / cron 不在主 CEO/frontdoor 路径上使用单独的短 `ceo_heartbeat` 通道，而是把隐藏 internal prompt 消息追加到普通 turn 共用的 session-owned `frontdoor_request_body_messages` / actual-request scaffold 上。

- 坑：把 heartbeat shrink/缓存差异解释成“现在是另一条 prompt 通道”；或把内部 heartbeat artifact 直接提升为新的 durable baseline。
- 不变量：heartbeat/cron 请求变短必须由 `token_compression` 解释；隐藏的 heartbeat 规则与 event-bundle 消息是 durable prompt history，应出现在 actual-request artifact、completed continuity sidecar 与 prompt assembly 中（UI 以 `ui_visible=false` 隐藏）——其中失败回合的内部提示词被标 `discarded` 排除、重复的按内容折叠，详见「heartbeat / cron 上下文残骸」。若候选 body 以 heartbeat 文本为主、明显劣于现有 baseline 且无合法 shrink reason，视为被拦截的 baseline 覆盖，内部请求只作 forensics-only 证据。
- 冷启动边界：内部事件消息只有在存在真实续跑基线时才并入续跑种子；无基线的内部轮（重启后首轮、全新会话首轮）必须走新建组装路径注入内部消息，基础系统提示保持首位。绝不能让仅有的内部事件消息冒充"完整旧请求体"触发续跑分支——续跑分支假定种子自带基础系统提示，会把基础提示静默丢掉，表现为内部轮模型行为失范、看不到人设/规则。
- 症状：把上一真实请求与新的 heartbeat/cron 请求作 append-only 续接比较时出现大面积前缀断裂——通常说明 baseline 恢复、tool-schema seeding 或压缩发生了变化，而不是 runtime 切到了单独通道。

### 3.11 残留 paused / pending 转录条目与未回答的用户输入

- 坑（paused）：手动暂停终止的回合把用户消息以 `_transcript_state=paused` 写进转录；暂停回合不会再走回合完成路径，缺少退役路径时条目一直卡在 paused。对账在每个回合 prepare 阶段把它补到种子尾部——紧邻当前用户消息、中间没有任何助手回复——轮末回写后嵌进基线；`token_compression` 把它改写掉，下一轮对账又重新补到新请求尾部。同一条问题以“从未被回答的用户提问”形态在请求尾部反复出现，诱导模型重复处理早已回答过的问题。
- 坑（pending）：排队消息以 `_transcript_state=pending` 写进转录，而"谁消费了它"不由行自己决定。内部（心跳/cron）回合会在 prepare 阶段吃掉排队的 follow-up，但它的完成回写整段跳过用户行，这些行要靠收尾退役那一步才升态（drain 时条目被放回 `_active_user_batch_inputs`，退役判据据此认得它们）。漏掉这一步，下一次会话重建按 pending 把它们当作未回答的提问接回并成批发出：实测一条渠道会话 9 条跨 10 天的旧提问被并成一个批次重答了一遍，当轮真正要回答的输入被挤到批次中间，回复答的是那个旧问题。
- 坑（被中断的回合）：`_prompt_locked` 的助手行只在正常收尾那一条路上写。取消/暂停落在收尾窗口（正文已经流到界面、回合还没走到 `_persist_turn_transcript`）时，这一行哪儿都没落：刷新后气泡消失，而它那条 pending 用户行既不在内存队列也没被任何回合认走过。这条形状在界面上的表现是"回答出现过又不见了"，在转录里的表现是用户行长期停在 pending 且同 turn 没有助手行。审批中断的续跑回合同样要在收尾补写自己那一行，只发 `message_end` 等于没写。
- 坑（失败回合）：运行时错误收尾写的助手行只证明"这一轮跑坏了"，不证明"用户被回答了"，但它带 `metadata.source=runtime_error`。把这种行算进"已回答"集合，等于把失败回合的用户输入永久锁在接回之外；同一条输入若又被按已消费翻成 `completed`，它就既不在内存队列、也不在待接回名单里，模型上下文里彻底消失。用户接着发"继续"，模型答的是上一件成功的事——这条与上面两条是同一族缺陷的相反方向（重复投喂 vs 永久缺席）。
- 不变量：残留 paused 条目随下一个用户可见回合正常完成被对账退役一次；pending 行只在**真正接走过它的**那个回合收尾时退役（该行 `turn_id` 在本轮批次里），无关的成功回合不替别人升态。被取消/暂停而没收口的回合由该出口自己同步归档可见正文行，不落行就把终态留给别的回合是错的。运行时错误路径既不退役也不升格，并把未获真实模型轮次的输入放回内存队列；状态机、为什么"队列才是真相"、以及哪条车道认领待重投输入，归 `runtime-overview.md`「压缩窗口：入站闸门与基线写入仲裁」。退役与对账在持久化侧的完整生命周期见「Baseline 合同与恢复顺序」。
- 症状：连续多轮 actual request artifact 里同一条历史用户消息反复出现在请求体尾部，紧邻当前轮用户消息且前后都没有助手回复；模型重复处理或批量补答早已回答过的历史问题，甚至把多条这样的消息合并成一条虚构的用户请求。渠道/Web 会话里另一副面孔是旧提问冒到最新回复下面，读起来像用户重发。反向症状是某轮失败后用户补一句"继续/刚才的问题"，模型回的内容与失败那轮无关。
- 取证：日志锚点 `Rehydrated N queued follow-up message(s) for <session>`、`Requeued N unanswered user message(s) for <session> after turn failure` 与 `Retired N consumed pending transcript user message(s) for <session>`——前两个在健康系统里应当罕见且 N 很小，长期不为 0 说明退役没有发生；`Retired` 只在排队补充被某轮消化时出现，读数为 0 而转录里有 pending 行通常意味着那些行从未被接走。转录侧数 `role=user` 且 `metadata._transcript_state=pending` 的行，再按它的 `turn_id` 找同轮真正的助手行：有回答行却还是 pending 即漏网（既不会被接回也拦得住下一轮），没有回答行则它仍在重投通道上；被中断回合的正确形态是那条 pending 行配一行 `status=paused` 的归档行，两个都没有才说明收尾出口没落 durable 记录。再数 `role=assistant` 且 `metadata.source=runtime_error` 的行，逐个核它前面那条用户行的状态是否仍是 `pending`（成了 `completed` 就是这条被吃掉的路径）。

### 3.12 heartbeat / cron 上下文残骸

- 坑：内部提示词（心跳规则 system + event-bundle user）在模型调用前就以 `completed` 落盘；回合失败时事件不出队、被反复重投，于是每轮请求体多堆一份相同规则+bundle。一个配额耗尽的心跳会话可累积到 10 份规则（去重后 2）+ 10 份 bundle（去重后 3），占请求上下文约 39%，且其中可能含早已 success 的节点的过期暂停通知。
- 不变量：失败回合的内部提示词在错误路径按 turn id + `internal_prompt_kind` 翻成 `_transcript_state=discarded`，`is_prompt_visible_message` 将 `discarded` 排除出 prompt assembly（jsonl 行保留供审计）。仍可见的内部提示词按 `(internal_prompt_kind, content)` 只保留最后一份。当前轮自己的 bundle 由 user_input 注入、不受折叠影响；折叠用 keep-last 保证最新事件仍落在请求体末尾。
- 折叠必须覆盖**两条**历史来源，否则 warm 路径仍堆积：(1) cold/transcript 路径在 `prompt_history_messages → fold_internal_prompt_history` 折叠（消息带 metadata，按 `internal_prompt_kind` 识别）；(2) warm 路径走续跑基线 `frontdoor_request_body_messages`——它由 `_persist_frontdoor_actual_request` 在**每个请求**（含失败回合）从 `request_messages` 提交，且只存 `{role, content}` **无 metadata**，故折叠在基线 chokepoint `_durable_frontdoor_request_body_messages` 再做一次，`_internal_prompt_kind` 对无 metadata 的消息**按内容前缀**（`[SESSION EVENTS]` / `This is a background heartbeat` / `# Heartbeat Rules`，均 startswith 精确匹配）识别。只在 transcript 路径折叠而漏掉基线，会让 provider 长期不可用、同一事件每轮重投时 bundle 在基线里线性堆积（实测某会话基线累积到 19 份 bundle / 6 个唯一内容）。
- 取证：在 actual-request JSON 的 `request_messages` 里统计 `content` 以 "This is a background heartbeat" 开头的规则副本数，与 `content` 以 "[SESSION EVENTS]" 开头的 bundle 副本数（折叠按内容标记识别，角色不参与判据：规则与 bundle 都是 user）；健康状态是规则 1 份、每种 distinct 事件 bundle 各 1 份。`provider_request_body` 为空时以 `request_messages` 为准。
- 症状：同一份 event bundle / heartbeat 规则在一个请求体里出现多份；token 估算逐轮近似线性上涨而对话无实质推进；模型对同一暂停节点反复反应，或被过期通知误导去处理已完成的节点。

### 3.13 节点 400 不一定是 payload 畸形——先比 `actual_request_hash`

- 坑：节点报 `400 - Failed to build prompt: Can only get item pairs from a mapping`（或其它 provider 侧 400）时，容易直接断定"我们发的 payload 形状不合法"并去改消息构造/加发送前校验。但 provider 网关在限速/配额风暴期间会降级、返回与 payload 无关的瞬时 400。
- 判别：节点请求 artifact（`.g3ku/main-runtime/artifacts/task_<id>/artifact_*.json`）带 `actual_request_hash`。取失败那次与同节点之后成功那次对比——**若 hash 完全相同（payload 逐字节一致）却一次 400、一次成功，则是 provider 侧瞬时错误，不是 payload bug**，不要去改消息构造。实测某节点 call15/call16（400）与 call17（成功）`actual_request_hash` 同为 `5d70e9a396337067`，且两次 400 正落在 429 配额风暴时段内。
- 推论：瞬时 400 被当成终态节点错误（`pause_reason=error`）会喂给心跳重投循环。让瞬时 provider 错误（含网关降级期的伪 400）走重试而非终态，属于错误分类/熔断范畴，不在取证层解决。
- 注意：`provider_request_body` 在这些 artifact 里为空 `{}`，落盘的是内部 `request_messages`（非 wire payload）；内部消息带 `_node_runtime_tool_contract_payload`、tool 消息带 `started_at/finished_at/elapsed_seconds/ephemeral` 等非标准字段，`_sanitize_empty_content` 原样透传给 provider——同 hash 既失败又成功证明 provider 平时容忍它们，不要据此臆断是这些字段致错。
- 同一类的第三种形状是**这颗成员实际塞不下**：网关对超窗请求返回通用 400（如 `inference request is invalid`），body 里不带任何 token 数，而记录声明的窗口比它真实可收的上限大，于是发送压缩与准入都不收小这一发。`main/runtime/model_size_span.py` 按成员记两臂同尺的读数——它真正答过的最大发送前自算体积、以及它被拒时的最小自算体积——只有"先收过更小的一发、随后被更大的一发拒"才成立区间，并按 `model_size_span:` 锚点落一行节点错误日志与一条 worker WARN，文案同时带上声明窗口与当轮触发线，口径是「运行时不改配置」：只把数字交给操作员，不改路由、不否决准入、不写记录。拒绝臂有两个来源：带回包的那一跳（`finish_reason=error`）与**整发请求根本没拿到回包**的那一次——后者不写逐跳台账，若在异常路径上不单独记账，区间就永远只有接受臂；那时接单成员由本轮路由租约点名（不是诊断里那颗），并且只在结构化 HTTP 400/422（`is_request_shape_error`）时才记账，429、超时、连接失败一律不算体积证据，否则墙会被限流算低。成立区间的那句报告同时挂在这次异常上，随子节点的异常暂停正文上去（见 `main-task-runtime.md`「节点信箱与唤醒（现行）」）。判据用「同成员更小的一发收过」当前置条件是有意的：400 同样覆盖真实畸形，缺这条证据就报区间会把这条锚点训成噪声。区间里两臂都取发送前自算那一格（`final_request_tokens`），不与 provider 回执混用。

### 3.14 用户消息时间装饰破坏前缀稳定或相等性去重

- 坑：用户消息投影副本会追加 `[消息送达时间]` 装饰行（合同见 `runtime-overview.md`「用户消息时间锚点」）。两类回归路径：(1) 把装饰改成用 now() 每次请求重渲染——历史消息文本逐请求漂移，provider 前缀缓存从第一条被装饰的历史消息起整体断裂，表现为缓存命中率骤降而消息数没变；(2) 新增"按用户消息文本去重/匹配"的逻辑时没有剥离装饰——已装饰的投影/种子文本与转录原文永不相等，同一条消息被判为新消息重复注入（症状同 3.11 的重复补种）。
- 不变量：装饰文本只派生自记录自带的 `timestamp`（固定值，跨请求字节一致）；所有跨"投影副本 vs 原文"的内容相等性比较先过 `strip_arrival_time_stamp`（`g3ku/core/timefmt.py`）；持久化转录、RAG ingest、web UI 永远只见原文，装饰只存在于请求投影层。
- 症状：缓存命中率下降且 stable prefix hash 逐轮变化，但请求消息数与回合结构无变化；或同一条用户消息在请求体里出现原文与带时间行两个版本。

### 3.15 帧的 messages 指针被读-改-写回路写没

- 坑：帧正文外置在 `messages_ref` 指向的 artifact 里。hydrate → sanitize → record 的读-改-写回路（启动恢复的 `replace_runtime_frames`、`update_frame`）在 ref 解析失败时只给出空 messages，若无条件换指针就等于用一份空正文覆盖旧历史——不报错、不留痕。
- 不变量：写帧规则本体见 `runtime-overview.md`「任务侧」（只有携带 messages 正文的写才允许换 `messages_ref`）。本文只登记它的**取证读法**：一次恢复不得缩减任何节点的 durable 历史，缩了必然在 `request_seed_source` 上留痕。
- 症状：恢复后某节点 `model_message_count` 突然掉到个位数、`request_seed_source=fallback_seed_*`，或 `task_runtime_frames` 行 `messages_ref=''` 而节点 `latest_runtime_messages_ref` 仍在；WARN `keeping existing messages_ref` / `resolved to no messages` 是保留规则被触发的直接证据。**这两行的行数不是一个量**：前者按 `(task, node, messages_ref)` 只报第一次（不带正文的轻读改写是常态，逐次提警会把真正的故障行埋掉），累计次数读它行内的 `bodyless_writes_total=`；后者每次解析失败都打。

### 3.16 验收 bootstrap 定稿与回合尾块

- 坑：验收节点的 bootstrap（`node.prompt`/`node.input`）若随每次交付刷新，就等于每轮搬走头探针的比对基准——头探针取投影首两条记录与 seed 前缀对齐（`_adopt_fresh_turn_seed_scaffold`），第二条正是 `_build_messages` 用 `node.prompt` 装成的 bootstrap user。基准一动，跨轮/跨重启的第一跳只能报 `fallback_seed_prefix_drift`，退回整段重建。
- 不变量：验收 bootstrap 在 `create_acceptance_node` 一次定稿，之后没有任何写路径回写它（`_refresh_acceptance_node_metadata` 只维护 metadata）。每轮的变化量全部落在**只进本轮的尾块**里：交接通知（`_acceptance_handoff_message`）与重建尾块（`_acceptance_turn_tail`）都只给 `result_payload_ref` / `final_output_ref` + 按内联额度（`_NODE_MESSAGE_INLINE_CHAR_LIMIT`）交付的摘要——额度内整篇、超限才给头段并指回同一个 ref，正文的完整副本住在 ref 指向的 artifact 里，上下文里不堆历次全文；尾块是每轮重付的未缓存段，因此它的实际尺寸就是这套额度的成本（实盘提交摘要与打回反馈都落在额度内）；恢复指纹同样只认验收标准模板 + 提交载荷 ref。派发侧合同见 `main-task-runtime.md`「验收节点：提前创建、激活与握手重派发」。
- 症状：验收节点上下文里堆着历次交付全文、或对已被取代的提交下结论 → 先确认 `node.prompt` 跨轮逐字节未变（变了就是刷新路径回写了 bootstrap），再看 `request_seed_source` 是否仍在 `scaffold_seed*`。

### 3.17 长期记忆的头部基座与增量

- 坑：这个块只在**压缩边界**更新（内联 `token_compression` 轮末、手动压缩），不是每跳现取。块字节一变，可复用前缀就只剩 `system` 与 `tools[]`：实测中位请求要重铺 84% 的正文，其中块本身只占 8%，其余全是被它顶掉的历史——所以更新点被限制在"前缀反正已经断了"的压缩跳；非压缩跳不更新，变化由一次性通知纠正。
- 不变量：取数是**会话级基座**，首次请求采纳一次，之后只在压缩边界换版本（规则本体见 `runtime-overview.md`「Memory Runtime State」）。两处更新都必须先冲刷复核窗口并落盘记忆批次，再读盘，反序会采纳到旧文档。压缩之间新增/改写/删除的行由修订流增量进上下文（`### Long-term memory updates`，退场行在 `Deleted memories:` 一节）；增量不重投整篇。跨轮可比性投影与 durable 基线两侧都剥这个块，`build_stable_prompt_cache_key` 同样不含它——本地 cache key 对这个块是盲的，判读看 artifact 的 `memory_snapshot` 字段与通知消息。
- 人设块：`## 我的人设` 是独立稳定消息，排在长期记忆块之后；它同样在首次请求与两处压缩边界采纳，重印之间字节冻结。中途改人设不碰头部字节，由同一条 `[RUNTIME_NOTICE]` 的 `### Persona updates` 投最新正文（人设为空时投询问指令），`persona_cursor` 按 digest 去重；压缩后头部重印、游标对齐。通知消息在压缩跳被丢弃，人设正文由重印后的头部承担，空人设的询问指令不重投。判读顺序：先确认头部块是不是压缩后的版本，再看通知里有没有 Persona updates；两者都在而模型仍按旧人设说话，才是投递链问题。合同见 `runtime-overview.md`「人设块、人设 note 与身份」。
- 症状：`adoption_reason` 读成 `idle_refresh` → 这条来自历史 artifact；运行时只产 `session_first_request` / `token_compression` / `manual_compression` 三档。某轮命中前缀在第二条消息就分叉、`memory_snapshot.hash` 同时变了、而该跳不是压缩跳 → 基座被非边界路径改写，属缺陷。「用户说记住/忘掉，模型照旧」→ 先看该会话 `memory_cursor` 是否落后于最新修订，再看下一跳有没有 `### Long-term memory updates`；两者都在却没进上下文，才是投递链问题。

### 3.18 静态声明钉在请求体头部

- 坑：`candidate_skills` 这类回合内常量跟着运行时契约尾块每跳重铺一份——尾块重铺只在它前面不动时便宜，而这一行是稳定块里最大的一段（实盘 5,285 跳：中位 281 token、占稳定块 23.1%，名单成员本身只在 21 跳（0.4%）里变过）。把它整体搬到头部也不是免费动作：首条 `system` 的字节一改，它身后的全部前缀（含记忆快照与整段历史）从那个位置起不命中。
- 不变量：四条静态声明（skill 全量名单 / `exec_runtime_policy` / `session_temp_dir` / 路径锚点两行 `path_anchor` + `path_anchor_rule`）钉在首条 `system` 末尾的 `## Runtime Contract (pinned)` 里，逐字节复用；头部带 `snapshot_note`，声明它是快照；尾块已退役。锚点两行是常量文本：`{workspace}`/`{temp}`/`{data}` 三个 token 恒定列出，不随解析出的数据根是否与项目根重合而增减——按重合与否增删会让操作员第一次设置数据目录那一跳改写头部，每个在跑的会话重付一遍前缀。重印判据**只有** skill roster epoch（随 token 压缩 / 手动压缩递增，与 `tools[]` 重印边界同源）；执行策略、临时目录、可见集都不进判据。两次重印之间的变化由发送前的单生产者投一条 `[RUNTIME_NOTICE]`：`exec_runtime_policy` / `session_temp_dir` 以头部卡住的那一行为初值，`denied_tools` / `candidate_tools` / `repair_required` / `attachment_targets` 按值摘要 diff；压缩跳头部重印后旧通知被剥离、台账清零，下一跳只补一条当前非头部状态快照。新增可读 skill 同在这条通知里（压缩后由新头部覆盖，旧名单不搬运）；同一条消息还承载长期记忆增量 `### Long-term memory updates`（按 `memory_cursor` 消费，规则见 `runtime-overview.md`「Memory Runtime State」）与人设更新 `### Persona updates`（按 `persona_cursor` 消费，规则见同文档「人设块、人设 note 与身份」），被删/被收回的名字留在头部直到下一次重印（`load_skill_context` 三态回执告知原因）。合同本体见 `tool-and-skill-system.md`「Pinned Static Declarations」，落点规则本体见同文档「Model Path Anchoring Contract」。
- 不变量（省缓存不许省声明）：尾块能不能省略，判据取头部那条记录里的原文，不取"本轮算出来了"。前门一个回合内有四条装配路（`message_builder` 的两条、回合起点 send-preflight、同回合每一跳的 prompt contract），它们拿到的 session 不是同一实例，`state` 的键又会被归一化白名单静默丢掉，所以钉住内容除会话对象属性外还落一张按 `session_key` 分桶的进程表；两处都对不上才重钉。拿不到载体（既无会话对象又无 `session_key`）或名单为空时整块不钉，三段声明全留在尾块。
- 症状：请求体头部出现 `## Runtime Contract (pinned)`（内含全量 skill 名单）而尾块没有 skill 名单行＝正常形状。重印点之间新变为可读的 skill 不进尾块：它以 `[RUNTIME_NOTICE]` 那条历史消息出现一次（同一条消息也可能只带 `### Long-term memory updates` 一节），之后随 durable 基线待在可复用前缀里（合同见 `tool-and-skill-system.md`「Pinned Static Declarations」）。两处都没有而治理侧确有可见技能＝缺陷：模型手里没有可加载的 skill_id，按"省缓存把声明省没了"排查，不要当缓存优化问题处理。相邻跳之间头部字节在变＝两个装配点算出的刷新键不一致（多为临时目录或 roster epoch 不同源），这种交替比不钉糟糕得多。

### 3.19 系统提示词的重读跟着记忆块与曝光的戳走

- 坑：`ceo_frontdoor.md` 逐装配点从磁盘重读。会话中途改一个字节，首条 `system` 就变了，而它身后是 `tools[]`、长期记忆块和整段历史——一次编辑等于给这个会话重铺全部前缀。
- 不变量：`CeoPromptBuilder.build_base_prompt(cache_stamp=...)` 的复用戳是「记忆块采纳时间 + `capability_snapshot.exposure_revision`」，即记忆块与 `tools[]` 各自决定何时换字节的那两个时钟；戳不动就返回同一份渲染结果，戳一动就重新读盘。注入值是 `stage_budget.ceo_stage_free_tool_names()`（免阶段名单，代码常量的派生值），不随轮变化，也不与本轮可见面取交集。空戳不缓存：新会话与不走这条装配路的调用方每次都读盘。
- 症状：改了 `ceo_frontdoor.md` 而某个活会话毫无反应＝预期行为，它要等下一次记忆采纳或曝光变化；新开一个会话立刻带新措辞。判"这一跳用的到底是哪份提示词"要比 `system` 字节与文件 mtime 的先后，别拿提交时间当依据。

### 3.20 跨档跳的未命中按台账的档位归属读，不按 worker 日志

- 数据源：`task_model_calls.payload_json` 里逐跳的 `route_admission_group` / `route_admission_index`（这一跳被授到链上哪一格）、`route_served_group` / `route_served_index`（真正把请求发出去时绑的档）、`route_escalation_count` / `route_escalation_reason`（这一跳沿链前进过几次、最后一次为什么）、`route_throttle_dimensions`（这一跳吃过的限流维度）。成员级归属在 `delta_usage_by_model[].route_index` / `.group_key` / `.quota_bucket_index`。负载均衡本体见 `runtime-overview.md`「节点模型路由与准入绑定」。
- 网页读口：`GET /api/tasks/{task_id}/model-call-page` 每行同名带这七列。这条车道是**按字段手工重建行**的，所以台账再加一格必须同步 `main/monitoring/query_service.py` 里的那一处重建，否则数据在库里而页面上没有；契约由 `tests/resources/test_task_model_call_page_lane.py` 从明细页进口钉住。
- 哨兵语义：`''` 与 `-1` 表示这一跳**没有租约**。前门车道与辅助车道跑在没有 balancer 的进程里，恒为哨兵；`0` 是合法的链首格号，所以缺省绝不能是 `0`，否则"没有租约"会被读成"落在第一档"。
- 判读形状：`route_admission_group != route_served_group` 的跳即跨档跳。同档内换成员不算跨档（格号不变），而同一网关下换成员也不换缓存命名空间，所以档位这条轴对缓存的影响只落在跨档那一跳；把两者混在一起数，会把"清单没变、命中掉了"错归给轮换。
- 为什么必须有这几格：worker 日志会滚掉，而且**升档那一跳不打 `Model route selected`**（它只打 `Model node binding rebound`），所以按 `selected` 行的 `route_index` 统计档位分布会把跨档跳读成 0 —— 这个 0 命中不代表没发生。要用日志交叉核对时，锚点是 `Model route lease released` 的 `route_index=` 与 `escalations=`。
- 症状：台账读出的组名与成员对不上（某跳 `route_served_group` 里的成员其实属于另一组）＝进程内组表与配置不一致，先读 `GET /api/models/load-balance/status` 的 `group_drift_count`，非 0 时任何档位归因都不可信，判据本体见 `runtime-overview.md`「节点模型路由与准入绑定」。

## 4. Prompt Cache Family 与 Actual Request

本节是 actual request 的取证合同：family/key 语义、per-request 取证顺序、baseline 与恢复顺序、shrink 原因边界。

### 4.1 Family 与 key 合同

- caller-side prompt cache family 只由 stable prefix 加显式 cache-family revision 输入定义；普通 callable/candidate/hydrated 工具漂移、loader 触发的 hydration 升级可以改变 actual request，但不得单独轮转 family key。
- CEO/frontdoor `call_model` 必须把为该轮重建的 request 与匹配重建的 `prompt_cache_key` 一起发送；重建 request 配旧 key 属于 runtime bug——它会掩盖 miss 到底来自 family churn 还是请求本身变化。节点同理：静态前缀保持前置 append-only、尾部恰好 1 份当前契约，`prompt_cache_key_hash` 与 `actual_request_hash` 分开排查。
- 命中量不按协议车道归因。`prompt_cache_key` 只有 Responses 车道转发（`g3ku/providers/responses_provider.py`），Chat Completions 车道在 provider 入口丢弃该参数（`g3ku/providers/openai_chat_provider.py`），而两家 usage 都以 `cached_tokens` 回报命中——缓存由网关按前缀复用自动发生。所以「这条车道不发 cache key」不等于它零命中，反过来有命中也不能证明请求带了 key；判读只看 `observed_input_truth.cache_hit_tokens` 与 family key 是否稳定，模型选择也不该被这个字段门控（见 `config-and-models.md`「角色路由：有序 fallback 与负载均衡组」）。
- 节点侧 cache family 里的模型身份是**稳定 route signature**（`route:lb:<group>[成员…] | model:<key>…`），不是候选数组：候选数组的第一项在含负载均衡组的链上不代表实际被选中的成员，把它当身份会让同一条链的 family 随展开顺序漂移。实际发送的模型只记在 `resolved_model_key`（= 该次 attempt 绑定的成员）。
- 负载均衡器把一个节点在其生命周期内粘滞绑在一个成员上，是**缓存命中契约的一部分**：换 `model_key` 等于换前缀缓存命名空间，断点之后的整段存活上下文都要重传，所以代价按「断点尾部存活量」计而不是按切换次数计。判读命中掉底先看该节点的 `resolved_model_key` 是否跨越了成员边界，再看重绑原因（过滤条件变化 / 拿不到 permit / 成员被移出组，见 `runtime-overview.md`「节点模型路由与准入绑定」）。429 惩罚不在原因列表里：它只进打分决定新绑定选谁，从不拆已有绑定，所以"命中掉底 + 风暴期 429 很多"这个组合要按成员让位（组内轮换）而不是按惩罚重绑读。绑定跨回合保留：只有节点结束（`node_runner._forget_node_route_binding`，由 `_mark_finished` / `_mark_failed` 触发）会清它，回合推进不在原因列表里。
- provider-facing `tools[]` 保持与曝光可见的具体工具集对齐的稳定超集，而不是更窄的当前轮 callable 集；bundle 刻意最小化，丰富的工具/技能说明放在尾部契约里，让 cache miss 更容易归因到真实请求增长而不是 schema 文本漂移。

### 4.2 Per-request 取证顺序

- CEO/frontdoor 与节点都为每次 `call_model` 落 per-round actual-request artifact。frontdoor 写 `.g3ku/web-ceo-requests/<session>/...json`，session snapshot 只保留最新文件路径与短元数据历史——per-round JSON 是精确请求取证权威，快照保持轻量以服务 websocket restore 与 UI 调试。
- `visible_frontdoor`、`token_compression`、`manual_context_compression` 都落在同一 artifact 族：普通 send 与 internal subrequest 可以从同一条时间线解释。`request_lane=manual_context_compression`（`request_kind=frontdoor_manual_compression_request`）是操作员发起的回合外压缩写下的**基线落点**，不是发往 provider 的请求：它的 `usage` 为空，正文即压缩后的 `[TOKEN_COMPACT_V2]` 请求体。它必须与当时的 durable 基线同源，因为重启恢复按 `request_messages` 字节对账挑 artifact 重建 trace，对不上就整体清空 trace（压缩随之丢失）。取证时不要把它当成一次真实发送，也不要用它的 `created_at` 归因某轮响应耗时。
- artifact 内保存 runtime-side 投影（`model_messages` / `request_messages`、`actual_tool_schemas`、cache-family 诊断）与可用时的最终 transport payload（`provider_request_meta`、`provider_request_body`）；`request_messages` 是保存请求顺序的兼容权威，不要重新引入旧 `messages` 全量镜像。
- 每条 artifact 还带归一化的 `usage`、`frontdoor_history_shrink_reason`、`frontdoor_token_preflight_diagnostics`：用它们比较“preflight 以为要发什么”与“provider 实际计费了什么”，不要只靠 transcript 时间线重建这层关系。另有 `memory_snapshot`（`hash` / `adopted_at` / `adoption_reason`）记录该请求携带的长期记忆快照是哪个采纳点定稿的——cache key 与 preflight 投影都不含这个块，它是一切记忆相关的命中判断的唯一取证入口（见 `runtime-overview.md`「Memory Runtime State」）。同形的 `persona`（`digest` / `adopted_at` / `adoption_reason`）记录头部人设块的采纳点；它不随中途的人设通知变化，通知只改变尾部消息。
- CEO/frontdoor artifact 另带首跳取证三字段：`turn_inbound_received_at`（bridge 接收回合时写入用户消息 metadata）、`provider_request_started_at`（该 `call_model` 首次真正发请求前写入；provider 重试沿用同一值）、`inbound_to_request_start_seconds`（两者差值，缺少入站时间戳时为 `null`）。`created_at` 是响应完成后的 artifact 写入时间，不能用它归因派发、转录持久化、prompt 组装或 preflight；“入站后迟迟没有请求”先看这三个字段。
- `.g3ku/web-ceo-requests/<session>/` 保留最新 300 份 artifact，以及被 paused/inflight/completed sidecar 引用的 artifact；更早且未被引用的文件按写入计数定期清理（每 25 次持久化触发一次，因此磁盘上限为 keep + 25）。排查长时间线时按现有 artifact 与 continuity history 的 `path` 对齐，不因目录缩短就认定历史请求不存在。
- 前端每轮 token 展示与本取证合同同源：实时轮次读会话内存累积（`_frontdoor_turn_usage`）；历史轮次重载优先读 transcript 每条 assistant 消息持久化的 `usage`（收尾时写入，不受本目录保留窗口影响），仅旧 transcript 缺该字段时才回退按 `turn_id` 聚合本目录 artifact。聚合只读计费三通道、只服务 UI 展示，不参与 baseline/恢复判定——UI 合同见 `web-and-admin.md`「Per-Turn Token Usage Contract」。
- 节点同样为每次 `call_model` 写专用 JSON，`actual_request_ref` / latest-context 指向该 artifact，不复用 runtime-frame `messages_ref`。restart/resume 的第一跳可以从最新 `actual_request_ref.request_messages` 取种子，让 same-turn append-only 增长跨过进程重启；该种子是一次性 scaffold，只服务第一条重建请求，一旦本轮重建出 pending tool/child turn 或发出新真实请求即丢弃，不得替换 durable 节点历史。

### 4.3 Baseline 合同与恢复顺序

- `RuntimeAgentSession._frontdoor_request_body_messages` 是 session-owned request-body baseline：保存剥掉动态契约后的重建 provider request body，下一轮在尾部重建唯一一份新的权威契约。它是普通可见 turn 的 append-only 续接来源（不是旧会话兼容回退）：新 user/runtime 尾部内容直接追加其上，不交给历史选择器判断“语义完整性”。
- visible turn 期间 baseline 必须跟上真实请求增长：每次 provider 调用后反映剥契约后的最新 body，每个工具循环后把新 assistant/tool transcript 折回同一份 baseline。它也必须跨过 finalize 与可恢复快照边界存活：`inflight_turn_snapshot` / paused execution context 必须连同 `frontdoor_history_shrink_reason` 一起携带它，否则重建的会话会悄悄退回 transcript/history replay，在允许路径之外缩短下一轮。
- 手动暂停发生在该轮首次 provider 请求发出之前时，被暂停回合的用户消息不进入基线（基线只在有真实 actual-request 证据后回写），而续跑种子路径也不读转录，该消息会因此从后续模型上下文缺席。prepare 阶段对续跑种子做转录对账来保住它：把 `_transcript_state=paused` 且 prompt-visible 的用户回合按转录顺序补到种子尾部；与种子既有用户消息或当前回合用户消息同文本的不重复补。对账只是尾部追加——不改动缓存前缀、不构成收缩、不改变恢复顺序。paused 条目没有独立的完成路径：暂停回合本身不会再走回合完成路径，残留 paused 条目随下一个用户可见回合正常完成被对账退役一次（同一路径同步执行 `clear_paused_execution_context()` 清掉暂停执行上下文）——此时它要么已被对账进种子并得到处理，要么已被新输入取代。运行时错误路径不退役 paused 条目：出错回合的请求体未必完成基线回写，提前退役会让对账停止补发，用户消息会从模型上下文永久消失。卡在 paused 的条目会触发「残留 paused 转录条目」陷阱。
- 图像边界：只有所选模型绑定启用图像多模态输入时，当前 turn 的 live request 才允许 provider 可见图像块，且同轮须把附件提示替换为直接视觉引导（不得暴露本地上传路径）；历史图像经 `content_open` 重开是独立的 live-only 通道——durable baseline 可保留 `path` / `ref`，但直接视觉复用需要后续 turn 重新 `content_open`。durable baseline、inflight/paused snapshot、completed continuity sidecar 持久化前必须把 `image_url` / `input_image` 剥回文本投影；saved actual-request artifact 是刻意的取证例外——验证图像是否到达 provider 要查 artifact，而不是 durable baseline。
- "图进了 body"与"上游看见了图"是两个问题，取证时不能合并：前者读 `image_count`，后者读 `image_blocks_at_tail`。契约尾块排在用户轮之后，所以入站上传的图块天然不在末位；大多数上游在任何位置都读得到图块，只读末条消息的那类会接受请求、不计图 token、也不回视觉内容（实盘没有失败可学）。运行时的入站车道只支持前一种形状，要把像素送到末位只有 `content_open` 那条尾部载体。判"这一发到底看不看得见"就比这两个读数，并在同一颗模型上做"图在末位 / 图后还跟一条纯文本消息"的两臂对照——计费侧的客观读数就是 `prompt_tokens` 里有没有那一份图（单图实测 ~1.4K），差值出现即那颗上游真的跑了视觉编码器；模型自述"我没收到图"不是证据，它的上下文里可能根本没有能力位那行字。形状与文案归属见 `web-and-admin.md`「Image Upload Gating」。
- CEO continuity 恢复顺序：paused snapshot → inflight snapshot → completed continuity sidecar（`.g3ku/web-ceo-continuity/<session>.json`）→ 最新 actual-request artifact → transcript/history fallback。前三条是可信 sidecar 通道，第四条是保缓存的应急回退；sidecar 只有真正携带可用 `frontdoor_request_body_messages` baseline 时才获胜，文件存在本身不得压制更晚、更完整的恢复源。
- 重启恢复刻意分两步：先从最高优先级 sidecar 恢复 durable `frontdoor_request_body_messages` baseline，再校验恢复出的 actual-request trace 仍指向可读、且 durable body 与该 baseline 匹配的 artifact；不匹配时按 §3.8 做 trace enrichment。
- completed continuity sidecar 在每次真实 provider-backed 同步与 terminalized manual stop 之后，持久化最新权威 baseline、actual-request trace、turns/compression 状态、hydrated tools、最近一次 visible tool/skill exposure 快照、钉住的 provider 名单（`provider_tool_schema_names`），以及钉进头部的静态声明原文与它的重印代数（`pinned_contract_text` / `pinned_contract_revision` / `pinned_contract_skill_ids` / `skill_roster_epoch`）——这四份是续跑第一跳不重渲头部、不改清单成员的依据。写入与恢复覆盖所有 CEO frontdoor 会话命名空间（`web:`、`china:`、`cron:`、`ext:`，即 `session_agent._FRONTDOOR_CONTINUITY_SESSION_KEY_PREFIXES`）——渠道会话与兜底 cron 会话的基线同样跨进程重启存活；`task:` 等非 frontdoor 命名空间不参与该生命周期。渠道会话因此不会在重启后丢失基线、退化成无基础系统提示的冷启动，也不因为“只是渠道会话”而失去手动压缩所需的可压缩基线。
- reopened completed session 把上一完成轮的 actual-request trace 恢复进当前会话状态，供下一个 fresh visible turn 滚入 `previous_*`；没有合法 shrink reason 时，不允许从“匹配的上一请求 scaffold 可用”悄悄退回“durable-only 第一跳”——先查 trace enrichment，再怪 prompt assembly。
- 若 fresh visible / heartbeat turn 发现内存 baseline 为空而 paused/inflight snapshot 或 completed continuity sidecar 里仍有，prompt assembly 应恢复该 baseline 并立即视为 session-owned append-only 前缀——按 baseline restoration 排查，不是普通 history compaction 路径。

### 4.4 Shrink 原因与压缩边界

- `frontdoor_history_shrink_reason` 是 prompt assembly 与 session 持久化之间的运行时合同：只有 `token_compression` 是下一轮 baseline 变短的合法理由；`user_edit_truncation` 是操作员发起的编辑重发/Fork 在轮间对 continuity 状态的整体替换（数据源 = `.g3ku/web-ceo-turn-boundaries/` 每轮边界快照，契约见 `web-and-admin.md`「Message Edit-Resend And Session Fork」）；其余理由按 runtime bug 排查，不要解释成“正常上下文整理”。操作员在回合外手动发起的压缩**不新增第三种理由**——它复用 `token_compression`，因此一次与任何 provider 响应都无对应关系的历史缩短是预期的：先看该会话是否有一条 `request_lane=manual_context_compression` 的 artifact 落在缩短发生的时间点。
- 编辑重发/Fork 之后的取证预期：该会话 `frontdoor_history_shrink_reason=user_edit_truncation` 的 sidecar 属正常状态；编辑后首个请求的 prompt cache 家族一次性 miss 属预期（前缀被整体替换）；被截断轮的 actual-request artifact 已被删除，`web-ceo-requests` 时间线出现对应空洞是截断的一部分（空基线截断会清空整个 artifact 目录，防止重启兜底复活被删轮内容），不要按"artifact 丢失"排查。
- 终态提交反复被拒、每次错误文本首行相同但 arguments 后缀不同，先怀疑 provider 而不是模型。网关把参数块未转义拼进 `arguments` 时，客户端的宽容解析（`streaming_timeouts.py` / `openai_chat_provider.py` 优先 `json_repair`）会把闭合标记吸进第一个字符串值并吃掉后面的必填项，于是"解析故障"长成"模型漏填参数"。识别指纹是字符串值以半截标记收尾（残缺写法实测三种，按形态判不能穷举字符串）。这类行的行首是 `Upstream payload shape fault detected`，并带 provider 边界原样带出的 `upstream_raw_bytes` / `upstream_raw_tail`（字段合同在 `web-and-admin.md`「Node Detail Error History」），所以"标记之后的参数到底有没有被发过来"的判据就在那一行里，不依赖额外取证。运行时把它与同族的另外两种回包形态（只有 reasoning 没有正文且零工具调用、`finish_reason=length` 切断参数串）记在同一个独立计数器上，**不占**模型侧的无效提交预算，所以症状表现为反复收到协议提示后自行恢复，而不是节点被判死；这一族的截尾残片连着两次相同就直接转可恢复暂停（残片各不相同才继续计数），因为再问几跳拿回的是同一份错位。预算归属与处置的主体在 `main-task-runtime.md`「Node-Level Pause and Recovery」。这类帧会留在上面那块地板上（每链 4–8 帧），所以它同时也是正文里唯一值得清的重试残骸。
- `token_compression` 是 CEO/frontdoor 与节点的内联 LLM 重写（触发源、压缩请求 append-only 形态、超窗分块、尾部收敛保证等合同本体详见 `runtime-overview.md`「Frontdoor Context Compression」）。取证边界：inline 压缩后重算仍超窗，以 context-window 错误失败；压缩 helper 的空摘要与 provider 错误文本都按普通发送路径语义重试（两条车道同按 `_PROVIDER_RETRY_LIMIT` 封顶），耗尽后才让发送失败，空摘要与 provider 错误文本永不误报为「上下文超限」。失败原因按形态分四档（`_compression_failure_reason`）：单发耗尽、分块一块都没收下（`produced no summary in any chunk (0/N chunks committed)`）、容量非正的规划器拒因（`token compression skipped: per-chunk capacity is …`）、helper 抛异常；有 provider 原话就把它附在原因尾部——限流与"模型真的回空包"是两种处置，不能都写成 `empty summary`。分块失败按**最长成功前缀**记账：某块耗尽 ⇒ 停在那一块，之前的块照常交付，那块及其后的原文留在请求体里（读数 `chunk_committed_count` 与 `chunk_count` 的差、`deferred_message_count`），摘要尾部出现"仍有 N 条原始消息在本请求里"就是这条道走到的痕迹；`compression_pass_rounds` 到 2 说明本跳已经把两轮压满仍有剩余，规模问题在下一跳继续收。块容量、单发判据与块数上限的推导（窗口、实测信封、输出余量、实测跨度只夹块尺寸、每轮 12 块 × 2 轮、超容量组收成头尾三段）由 `runtime-overview.md`「Frontdoor Context Compression」持有，本处只给读法：`chunk_count` 是装箱结果不是配置项；看到 `per-chunk capacity is 0` 是窗口本身放不下一个压缩请求的信封（会话侧放弃这一 pass、节点侧拒发暂停）；`capacity_source=observed_request_span` 表示容量被该节点/会话的实测跨度夹过一刀（声明窗口虚高）；`truncation_group_count>0` 表示有原子组单独超容量、被收成头尾后仍进了摘要（那段的中间部分在摘要里不可见，原文仍在 artifact 里）；`deferred_message_count>0` 不是失败，是"本轮没压完"。压缩读数在拒发那跳也必须落全（`compression_mode`/`chunk_count`/`chunk_committed_count`/`chunk_failed_count`/`compression_pass_rounds`/`deferred_message_count`/`capacity_tokens`/`capacity_source`/`truncation_group_count`/`helper_message_count`）——那一跳本来没有 provider 调用可查，这些字段是唯一线索。两条压缩车道同源，记忆车道不切块、超容量直接带真实原因失败（记忆车道不套实测跨度那一刀，口径差异见 `runtime-overview.md`「Frontdoor Context Compression」）。可压缩区按**条数**切尾（节点 `_NODE_TOKEN_COMPACTION_RECENT_TAIL_COUNT=12`）：条数少而单条极大的历史（实盘 11 条 / 16.3 万 tok）会让可压段为 0，此时 helper 一次都不会被调用，直接落 `... with no compressible history` 拒发——这是准入形状问题，不是"压缩没生效"，也不是模型窗口配置问题。压缩请求本体与刚发出的正常请求共享字节级前缀（末尾追加 user 指令），取证时压缩 artifact 的 `request_lane=token_compression` 请求体 = 历史前缀 + 指令，`compression_mode` 区分单发（`llm`）与分块（`llm_chunked`，携带 `chunk_count`/`merge_pass_applied`）。节点未消费追加通知窗口（`raw_notice_window`）无论位置都被原样保留在压缩块之前，取证时不应指望它在压缩块之后出现；该剔除触发时节点压缩请求不构成纯前缀，缓存命中从剔除点起中断属预期。「压缩后重算始终超窗且无法收敛」时按尾部含超大工具结果排查，而不是按模型窗口配置排查。
- 两条车道都只在重印边界轮转 `tools[]`：`token_compression` 那一跳重印（正文已被 `[TOKEN_COMPACT_V2]` 整段重写，可复用前缀反正断在这里）。前门另有一条边界——手动压缩（`PENDING_PROVIDER_BUNDLE_RECOMMIT_ATTR` 标记，读一次即清）；**空闲时长不是边界**。**换车道不是边界**：心跳/定时内部轮与外部轮共用同一份冻结 `tools[]`，内部轮的工具面收窄落在执行准入（派发字典 ∩ 当轮治理可见集），声明面不因车道切换重印——所以"内部轮里某个工具还在 `tools[]` 里却调不动"是设计形态，不是清单没重算。字段读法见本节「跨普通 fresh turn 的 tool schema churn」，合同本体见 `tool-and-skill-system.md`「Provider Tool Surface」。
- manual pause 与压缩的两条边界：压缩进行中暂停会丢弃迟到的压缩结果，下一可见轮从当前权威 baseline 重新走 prepare → estimate → 可选压缩 → send；若 turn 在最终 preflight 即将进入 `token_compression` 时暂停，下一 fresh turn 可从 pending shrink marker、或经上一 actual-request history 关联的后续内部 `token_compression` artifact 恢复 `frontdoor_history_shrink_reason=token_compression`——这是“压缩刚开始就暂停”的合法竞态解决路径，不是任意裁剪。被中断的压缩既不写基线也不留摘要块，转录里留下的是一条 `compression_state=paused` 的区分线行（UI-only，不进模型上下文）；排查“历史变短却找不到对应 provider 请求”时，先看这条线是否紧邻一个未完成的压缩，再看有没有 `manual_context_compression` artifact——只有两者都在才算真丢过上下文。反过来，「区分线在压缩途中消失、刷新网页才出现」属于前端没收到终局（轮询被单次失败掐断，合同见 `web-and-admin.md`「Manual Context Compression」），不是基线丢失：只要该会话已有 `manual_context_compression` artifact 且转录里落了对应的 `compression_state` 标记行，服务端这一轮就是完整的，不要去查 prompt assembly。方向相反的那条读法：线本该切进回合里却整条钉在 feed 尾部，先比那一行的 `stats.boundary_turn_seq` 与该回合渲染出来的 `turns[].seq` 是否同一本账（合同见 `runtime-overview.md`「Frontdoor Context Compression」），同域且边界确实落在两张卡之间，才轮到查显示侧的安置判据。
- 手动压缩报 `completed`、`post_tokens` 也降了几倍，但下一回合的请求体又回到压缩前规模：这是"摘要落了盘又被写回去"，不是摘要没生成。取证的先后顺序是——比较相邻两条 actual-request 工件的 `request_messages` 字符规模与条数（压缩那条是 `request_lane=manual_context_compression`、条数极少，下一条若回到压缩前的几百条即被覆盖），再看 `GET /api/ceo/sessions/{id}/compress-context` 的 `reason`：`baseline_advanced` 表示运行时已经拒写这种覆盖（当次不落区分线、UI 明说没落盘），其余值指向压缩窗口与在途回合的时序问题（合同见 `runtime-overview.md`「压缩窗口：入站闸门与基线写入仲裁」）。压缩期间的入站不会另起回合，而是进候选/排队队列，所以这条症状也不该被解释成"用户消息把上下文撑回去了"。
- 完整压缩合同详见 `runtime-overview.md`「Frontdoor Context Compression」。

### 4.5 压缩块的格式与字段语义

压缩块在 transcript 与 request artifact 里落为**带前缀的单条消息**：首行前缀标记，其后 `
` 分隔的 JSON 元数据（`ensure_ascii=False, sort_keys=True` 序列化）；识别只需前缀匹配，不必解析 JSON。存量块里的旧 `[G3KU_*]` 拼写同样按前缀识别。

角色合同：`[TOKEN_COMPACT_V2]` 是 **assistant 角色**——其正文是模型直接继续阅读的自然语言会话摘要，语义上属于对话延续，用 user/system 会让模型把摘要当成新的用户指令或顶掉唯一一份首位 system（合同见本文「线体角色合同」）。节点通知块 `[NOTICE_TAIL]` 以 **user** 落地；它的识别只按"前缀 + 换行 + JSON"这个块形判，既不挑角色也不挑拼写——挑角色会让存量 assistant 块从去重视野里消失（同一份通知曾被叠成 36 份），只按裸前缀判又会把正文里提到前缀的用户句子误当块。

- `[TOKEN_COMPACT_V2]` — 内联 LLM 全局压缩。第二行 JSON 为 `{"kind":...,"history_message_count":N}`：kind 为 `frontdoor_token_compaction_llm`（CEO/frontdoor）或 `node_token_compaction_llm`（节点，另带 `node_id` 与各分区计数字段），空行后接压缩摘要正文（语言跟随被压缩历史，不强制中文）：模型直接继续阅读的自然语言会话内容，不是结构化数据，也不是 JSON。第二行必须是可独立解析的 JSON、且与正文之间是真空行——读盘点按「前缀 + 元数据行」取压缩标记；把分隔写成转义字面量就等于永远读不到。识别压缩块只看前缀与 `kind`，正文结构不是判定依据。
- `[NOTICE_TAIL]` — 追加通知的未消费窗口（`raw_notice_window`）与必须逐条保留的追加要求段，结构见 `main-task-runtime.md`「节点信箱与唤醒（现行）」；它随 `frame.messages` 回写，装配期按逐字节内容去重，同一份只插一次。
- 存量转录里以 `[STAGE_*]` 开头的消息按普通历史消息处理：读侧没有对应的识别端，不会剥离、去重或重渲染，它们随所在历史一起参与 `token_compression`。

### 4.6 中途插入消息的家族、位置与生命周期

“从哪个安全边界进入上下文”与“能活多久”是两条独立的轴：中途插入的消息有的只活一轮，有的活到下一次压缩，有的跨压缩保留，有的长期驻留。`main/runtime/mid_insert.py` 是这两条轴的唯一注册表；新增一类运行时消息只改这一处并补 `tests/resources/test_mid_insert_registry.py` 的家族用例，剥离点统一读表。

调用面固定为：`classify_message`（整条消息家族，metadata 优先）、`scan_markers`（消息正文里的 marker，不改变消息归属）、`strip_for_stage(stage, lane)`（按表剥离，`families=` 供单族子站点覆盖）、`keeps_record_for_stage`（逐条投影）、`insert_mid_messages`（identity 去重、固定优先级、位置安全）、`compaction_policy` / `strip_after_compaction`（压缩分派与压缩后下车钩子）。`stage` 取 `durable` / `prompt_tail` / `compaction` / `post_compaction` / `projection`，`lane` 取 `frontdoor` / `node`。

| family | 载体 | 插入位置 | 寿命 | 压缩策略 | generic strip 的落点 |
|---|---|---|---|---|---|
| `runtime_notice` | `[RUNTIME_NOTICE]` user 消息（skill 名单 / 记忆增量 / 合同增量） | 请求尾部；前门排在当轮 user 之后、turn-only 之前，节点排在历史末尾 | 活到下一次压缩 | `drop` + 下一跳状态快照 | `post_compaction` |
| `turn_only_note` | `System note for this turn only:` user 消息 | 请求尾部，每跳重建，单例 | 只活一轮 | `drop` | `durable` / `prompt_tail` / `projection` |
| `append_notice` | `[NOTICE_TAIL]` + JSON user 消息 | 节点 bootstrap user 之后（system 保持首位） | 压缩免疫 | `carry`；深层 `raw_notice_window` 由压缩切分原样捞出 | 任何 generic strip 都不许碰 |
| `retrieval_memory_snapshot` | `## 长期记忆\n` assistant 消息 | stable index 1（system 之后、历史之前） | 每轮重建 | `rebuild` | `durable` / `compaction` / `projection`；`prompt_tail` 保留 |
| `retrieval_overlay` | `## 长期记忆写入提示` / `## 已检索记忆使用提示` assistant 消息 | 由装配层逐轮重建 | 每轮重建 | `rebuild` | 只在 `projection` |
| `hidden_internal_event` | metadata `internal_prompt_kind`，内容前缀 `This is a background heartbeat` / `# Heartbeat Rules` / `[SESSION EVENTS]` / `[CRON INTERNAL EVENT]` | 心跳 / cron 的规则行与事件束 | 持久 | `keep`；durable 基线按 `(kind, content)` 折叠重复副本 | 任何 generic strip 都不许碰 |
| `legacy_runtime_contract` | `## Runtime Tool Contract` 整条消息（前门与节点头抬同形） | 已退役，只保留识别与剥离 | 无 | `drop` | `durable` / `prompt_tail` / `compaction` / `projection` |
| `tool_contract_block`（marker） | 嵌在 `role=tool` 结果里的 `[TOOL_CONTRACT:<id>:<fp>]` 块 | 随工具结果进入 | 随工具结果活到压缩 | `keep`；压缩掉工具结果即离场，下次 `load` 重新水合 | 不剥整条工具结果 |
| `silent_trace`（marker） | assistant 的 `silent` tool_call 行 + 它声明的 tool 结果 | 作为普通回合记录进入 | 到 `subject=task:<id>` 终态为止 | `conditional`：同一 assistant 行的全部 task subject 都 success/failed 才整组退休；非终态、非 task id、读取失败一律保留 | 只整组摘出，不单独摘行 |
| `system_relay_notice` | 以 `【任务收到用户定向通知】` 等前缀开头的 user 消息 | 通知投递时追加到目标节点历史 | 持久 | `keep` | 任何 generic strip 都不许碰 |

维护者容易误读的四点：

- **识别只认块形，不认正文里提到的字符串**：`[TOOL_CONTRACT]` 只认工具结果开头的块，正文里举例的假块头不构成在场；用户正文中段提到 `[RUNTIME_NOTICE]` 或 `[NOTICE_TAIL]` 不会被剥。
- **旧合同两族合并**：前门与节点的 `## Runtime Tool Contract` 抬头相同，识别函数对同一字节都成立；注册表只登记一个 `legacy_runtime_contract` 家族，避免“一条消息被两族认领”。
- **hidden event 的 metadata 会丢**：`sanitize_provider_messages` 与 durable 基线只保留 `role` / `content`，重启后靠内容前缀识别；新增内部提示形态时两处都要能认出它。
- **silent 组必须整组同进同出**：只搬 assistant 行会留下无声明方的 tool 结果，只搬 tool 行会留下悬空声明；同一 assistant 行混合 silent 与非 silent 调用时，非 silent 结果不在这组里，家族测试要覆盖这种边界。

改动任一家族的插入位置、寿命或压缩策略时，先跑 `tests/resources/test_mid_insert_registry.py` 的 family × stage 矩阵，再用 actual-request artifact 核对两条车道的顺序：前门是 `system → 记忆快照 → 历史 → 当轮 user → runtime notice → 动态 appendix（retrieval / overlay）→ turn-only`，节点是 `system → bootstrap user → append notice → runtime notice → turn-only`。

## 5. 排查流程

### 5.1 先画时间线

至少收集：

- transcript 时间线
- request artifact 时间线
- usage/billing 时间线
- completed continuity sidecar（如果存在）
- paused/inflight snapshot（如果存在）

要确认：

- 每条 usage 记录大致对应哪一轮、哪一条 request
- 是否有 artifact 缺口
- 是否有 hidden/internal round 混进来

### 5.2 再分类这次断的是哪一层

按 §1.2 的 hash 分层判定：

- `prompt_cache_key_hash` 变了，优先排 family churn
- family 不变但 `actual_request_hash` 变了，优先排 request shape
- family 与 shape 都没明显异常，但 usage 仍显示 0 cache，优先排 artifact 对应关系

### 5.3 再比公共前缀

至少比两层：`request_messages` 与 `provider_request_body.input`；二者结论不一致时，以 `provider_request_body.input` 为更高优先级。

要重点记录：公共前缀长度、第一个分叉索引、分叉点前后分别是什么类型的消息。最常见的分叉模式是旧请求为 `contract`、新请求已变成 `assistant tool_calls`——这基本说明不是在上一条真实 provider request 上继续，而是在 stripped/scaffold-less body 上继续。

### 5.4 最后判断是不是合法 shrink

允许的 shrink 原因只有 `token_compression`（压缩触发边界、失败模式与暂停竞态见 §4.4）。另有两类**不**算非法 shrink 的合法变化：

- 从被携带历史中剥掉列尾的 turn-only note / 已轮次膨胀的旧契约——前端 shrink 守卫已对 note 做中性化比较，不会误判
- 去累积导致的单轮“尾部被剥再重注”，只要 body 的真实 transcript 没有缩短，就不算 shrink

下一轮 baseline 变短却没有上述原因之一：按 runtime bug 处理，不要解释成“正常上下文整理”。

### 5.5 frontdoor token preflight 之后要看哪份诊断

CEO/frontdoor 在真正发 provider 请求前有最后一层 token preflight。排查顺序：先看 actual request artifact（确认最终 `request_messages` / `provider_request_body`）→ 再看 `frontdoor_token_preflight_diagnostics` → 最后回头看 builder 的 `pre_summary_prompt_tokens`。builder 里的 token 估算只说明“组装阶段看到的上下文压力”；真正决定本轮是否在发送前压缩的，是 provider-send 前的最终 preflight。

- `frontdoor_token_preflight_diagnostics.applied=true` 时，预期 `frontdoor_history_shrink_reason` 为 `token_compression`；diagnostics 显示已触发但 reason 不符，优先按 runtime bug 排查。
- diagnostics 显示未触发但请求仍明显缩短：回到 continuity baseline handoff 链路，不要把所有缩短都归因到 token preflight。

关键 ground-truth 字段：

| 字段 | 排查含义 |
| --- | --- |
| `effective_input_tokens` | 用作锚点的上一轮 provider 输入规模真值（阶段裁撤扣回之后的值） |
| `anchor_projection_shrink_tokens` | 锚点从真实 usage 里扣回的体量：只在阶段过期点那一跳非 0，非 0 而读数仍按裁撤前规模报＝扣回没生效（见「同 turn 的 append-only 规则被破坏」锚点同源不变量） |
| `delta_estimate_tokens` | 相对上一请求的增量估算 |
| `comparable_to_previous_request` | 为 `false` 说明触发源退回 preview-only 估算（usage-first 合同的前提不成立）；先查不可比原因，而不是先怀疑阈值 |
| `estimate_source=usage_plus_delta` | 可 append-only 比对且上一请求 usage 真值可用：触发源＝上一请求有效输入（input + cache read）＋增量估算，preview 不覆盖它。这条前提要求"上一跳与这一跳是同一颗模型"判得对——身份按下一段那把尺子的同一优先序取，不取链首 |
| `estimate_source=preview_estimate` | usage 缺失或请求不可比对（首跳 / 重启 / schema churn / 压缩后首跳）时的兜底车道，不是默认 |
| `observed_input_truth.source=preflight_estimate` | provider 未给出可用输入侧 usage，runtime 用最终 send 时估算兜底；预期回退，不自动是 cache bug |
| `pre_compaction_*` | top-level `final_request_tokens` / `estimated_total_tokens` 已是压缩后值时，用它们解释“为什么这轮会先压缩” |

- artifact 的 `provider_model` 与 `observed_input_truth.provider_model` 记**这一跳真正发出去的模型**（取 `provider_request_body.model`），链首另存 `requested_provider_model`。跨档（429 降档）那一跳的 usage 因此记在服务它的那颗模型上；"同一颗模型才可比"的闸门也才真的会在跨档处判不可比。

另要排一类隐蔽误判：preflight estimator 必须基于原始 `provider_request_body` 估算，不能复用面向摘要/展示的 serializer 再把超长字段截成固定前后两段后估算。典型症状：`provider_request_body` 明明已经很大，`final_request_tokens` 却长期卡在异常偏小、几乎不随请求增长变化的常数——先按 estimator（preview 兜底与增量估算车道）低估导致压缩阈值打不到排查，不要先怀疑 `trigger_tokens` 配置失效。

### 5.6 节点侧排查要点（preflight / scaffold / append-notice）

execution / acceptance 节点在真正发 provider 请求前也走最后一层 node send token preflight，包括 `message_distribution` 控制轮。两个边界必须分清：`message_distribution` 包含在 node send preflight 里；`spawn review` 是外部检验通道，故意不在 node preflight 合同里，不要混为一谈。评审车道的正文规模与重发次数因此不出现在 `token_preflight_diagnostics` 里，要看 `spawn_review` 载荷上的 `review_attempts` / `review_request_chars`（合同详见 `main-task-runtime.md`「epoch 驱动器、波次与降级车道」）。

先看节点 runtime frame 或 actual-request artifact 里的：

| 字段 | 用途 |
| --- | --- |
| `token_preflight_diagnostics` | 是否触发发送前压缩 |
| `history_shrink_reason` | 核对合法 shrink 原因 |
| `prompt_cache_key_hash` | family 是否 churn |
| `actual_request_hash` | 请求形态是否变化 |
| `actual_request_message_count` | 消息数是否骤降 |
| `request_seed_source` / `request_seed_message_count` | 第一跳是否采用 scaffold、回退原因（`fallback_*`）或同轮链状态 |
| `compaction_applied` / `compaction_mode` / `projection_kind` | 这一跳是否被压缩重写、发出的是不是投影（`token_compacted`）|
| `final_request_tokens` / `effective_trigger_tokens` / `context_window_tokens` / `would_trigger_token_compression` | 这一发的**发送前自算**体积、当轮触发线、当轮尺子读数，以及它是否越过触发线。逐跳行原本只留 provider 回执那份，于是"被拒的一臂"没有可比数字：provider 回执与自算估算对同一正文差几个百分点，拿回执的最大接受去和估算的最小拒绝相减会得出**反向**区间（实测按回执的最大接受低于按估算的最小拒绝）。两臂同尺才谈得上比较，这四格就是为此在场；它们不挂在 `applied` 门后，没压的跳也要能回答"这一发当时多大" |
| `helper_usage` | 摘要 helper 那次调用的用量（不计入节点与任务累计，账外成本只在这里可见）|
| `token_preflight_diagnostics.ruler_source` | 这把尺子从哪儿来：`group:<key>`＝按准入预测的那一跳目标组取最小成员窗口，`chain_head:<key>`＝没有组预测（direct 链位、前门、记忆车道）时按这一发落到的那颗（链首）自己的窗口 |
| `token_preflight_diagnostics.error` | 预检为什么拒发这一跳（压缩助手空包 / 无可压缩历史且预估超过本轮发送窗口）|
| `notice_preservation` | 压缩对"中途变更要求"的保真读数（`open_count` / `hit_count` / `miss_count` / `rolled_stage_id`）|

压缩尺子的合同（一条，两个载体都要读它）：尺子 = **这一发落到的那颗模型的窗口**；落点是负载均衡组时，取那个组里最小的那颗成员窗口。压缩发生在选择之前，而选择又拿压缩后的体量当筛选条件（`required_context_window_tokens`），所以"打到谁"只能预测：准入侧 `ModelLoadBalancer.peek_selection()` / `NodeTurnController.peek_hop_windows()` 用与 `select()` 同一份资格判据（组启用、成员还在计划里、粘滞成员仍合格），但不看 permit、不看限流、也不看尺寸。组内取最小的理由：请求一旦超过某颗成员的窗口，准入就不把它授予这一跳，组内可用成员随之变少，攒到没人塞得下就是把共享队列钉死的那类停摆。direct 链位与没有回合准入的车道（前门、记忆）**不取整条链最小**，而是按链首那颗自己——拿链尾的窗口去压链首的请求，等于让一条不相干的记录压低本轮预算（实盘一条 CEO 链：链首 500,000 被链尾 390,000 压成 296,400 触发线，而不是它自己的 380,000）。链前进到塞不下的成员由那条尺寸筛处理（见 `runtime-overview.md`「Chat provider 超时与重试边界」），不由尺子提前买单。链首那颗也读不到窗口时才退回链级最小：一个 0 会把尺子打到硬门 `context_window_tokens <= 25000` 以下，等于让一条坏记录把该节点后续所有跳全部拒发。预测偏大不会误发：授予那侧仍按尺寸筛成员，塞不下时落 `node_turn_admission_fault:` 暂停，下一跳用新尺子自愈。

同一次预测还给出**这一发的模型身份**，usage-first 锚点用它判"上一跳与这一跳是不是同一颗"：优先预测成员的回执上游名，预测不到用上一跳回执名，两者都没有才用链首。链首在异构组里是错的锚点——成员的回执上游名可以各不相同，节点被粘在非链首成员上时"同一颗吗"永远判否，锚点整条链静默停用、每跳退回全量 preview（实盘：一条链 53/53 跳 `comparable_to_previous_request=false`，兄弟节点只在链首那一跳 false）。用上一跳回执名的代价是：真重绑的那一跳锚点偏一跳，而那一跳本来就要按新命名空间重铺，不额外付钱。判据落在哪一支可由 `ruler_source` 分桶（`group:<key>`＝有预测，`chain_head:<key>`＝没有）。

预检拒发这一跳不会发出请求，所以该节点在 `task_model_calls` 里没有对应行；它落一条以 `send_preflight_fault:` 开头、带 `预估=`/`本轮发送窗口=` 的错误行，并把节点转成可恢复错误暂停（判据与打回循环的隔离见 `main-task-runtime.md`「Node-Level Pause and Recovery」）。两种成因要先分开再处置：`estimated_total_tokens` 真的大于 `context_window_tokens` 且历史无可压缩段，是声明窗口或历史规模的问题；`token compression helper returned an empty summary` 是 helper 那一跳的回包形态故障（与"思考顶满输出"同源），此时预估与窗口的差值可能并不大。恢复该节点前，两者都不会因为 resume 而自行消失。

节点 token 压缩是否真的把成本收住，看的是**它压了几跳**：`compaction_applied` 为真的跳应当只在越线时出现一次，随后 `prepared_message_count` 逐跳 +新增、`request_seed_source` 在交接那一跳记 `same_turn_token_compacted`、`estimate_source` 回到 `usage_plus_delta`。读数是"连续多跳 `compaction_applied` 为真且 `cache_hit_tokens` 恒等于头部两条的 token 数"时，说明投影没有成为下一跳基线（每跳仍从全量账本重装配、每跳重新摘要），按发送面回归排查而不是按模型窗口配置排查；`projection_kind` 恒空而 `compaction_applied` 逐跳为真，就是同一条回归的另一面。摘要 helper 每次把整段可压缩历史重放一遍，所以这条回归的真实代价高于逐跳明细显示的数字——`helper_usage` 不进节点累计，`sum(task_model_calls.delta_usage) == 节点 token_usage` 恒成立。

“某个工具上一轮还直接执行、这一轮调用它只交回契约”的排查顺序（在场判据本身归 `tool-hydration-and-callable-chain.md`「契约在场与 callable」）：先看那一跳请求视图里该工具的 `[TOOL_CONTRACT:…]` 块在不在（耐久读数 `model_visible_tool_selection_trace.tool_contract_presence`，前门侧同名键在 `frontdoor_selection_debug`）；不在就按预期形态处理——那一跳交回契约，下一跳起执行，不需要任何修复动作。真报 `tool not available` 时看的不是在场与否，而是当轮治理实时读：名字仍挂在 `tools[]` 却调不动 = 权限收回或资源下架，尾块 `denied_tools` 应当点着它；`denied_tools` 里没有、callable 里也没有，才是读路径与写回不同源（第四态，属合同破坏）。**压缩本身不是缺陷**：`token_compression` 与阶段裁撤把契约块移出上下文，能力面随之收窄是这条判据的设计后果，实测同轮请求消息数不降。

节点 `token_compression` 是否生效，看**压缩那一跳的发送体是否塌下来**：`prepared_message_chars` / `observed_input_truth.effective_input_tokens` 应在该跳台阶式下降，同轮 `request_seed_source=same_turn_token_compacted`、`history_shrink_reason=token_compression`。除此之外同回合链仍是字面前缀增长，所以曲线是"平台 + 周期性台阶"，不是逐轮下降；`model_message_chars`（投影）与发送体的差只在过期点那一跳被抹平，其余轮次投影更早就等于发送体。判据反过来的情形才是回归：节点账本已有 >3 个终态阶段、`model_message_chars` 与 `prepared_message_chars` 却逐轮同规模同涨，说明压缩又变成"只加摘要不删肉身"（合同见 `runtime-overview.md`「Frontdoor Context Compression」）。台阶处 provider 前缀必然断一次：那一跳 `cache_hit_tokens` 塌回分叉点之前、非缓存 `input_tokens` 冲高，之后逐轮重新长回来——把这次断点读成缓存回归是错的。

preflight 判定：

- `applied=true` 预期 `history_shrink_reason=token_compression`；actual request 变短但 `prompt_cache_key_hash` 没变，是“live request 被压缩但 caller-side family 未换”的正常行为，不是 family churn。
- 节点 restart / resume 后的第一跳新请求可以复用“已经过 token compression 的 actual request scaffold”；这是合法延续路径，不是 context loss。
- 节点 diagnostics 的 top-level 字段同样在 compaction 后切到“最终真正要发的 request”，压缩前 hybrid 判断保留在 `pre_compaction_*`。
- 节点侧不把 inline image `data:` URL 的 base64 字符串按普通文本估 token；当前轮 `content_open` 刚打开多张图片而下一跳超窗时，优先从 `estimated_image_tokens` / `image_count` 解释，不要再把根因归结为”base64 文本被算爆”。
- 图片按块计价的尺寸只从表头读，来源是 `g3ku/runtime/context/image_metrics.py` 这一份（前门与发送预检两条估算车道都调它，各留各的落点映射）：png / gif / jpeg / webp（`VP8 `、`VP8L`、`VP8X` 三种容器）/ bmp 能读出真实画布尺寸，读不出的格式回落成 512×512 默认单块。判"这块到底算了几个 tile"要先确认格式在这份名单里——表头解析不到 ≠ 图片小，被 media 面收下的格式一旦缺席名单，超窗判据就按一块的钱放行一次实际上是四块、八块的请求。
- 节点侧 `token_compression` 是一次 helper LLM 调用（诊断 `compression_helper_call` 记录历史条数与 usage，压缩载荷带 `task_goal`）：helper 不属于节点的 actual-request / `observed_input_truth` 链，按 provider 调用计数取证时注意它多出的一次发送；helper 失败或空摘要按 preflight 错误让节点发送失败（`applied` 保持 `false`、诊断带 `error`）。
- 节点压缩不做 marker-only 改写：压缩真正生效时 `applied=true`、`mode=llm`、压缩块 `kind=node_token_compaction_llm`；实际请求没有压缩块却带压缩诊断，按回归排查。

preflight 在节点端发送模型前就失败时：先看是否 `context_window_tokens <= 25000` 这类硬错误配置，再看 preview builder / provider payload 估算错误；这类“没有模型请求”的卡顿优先查 preflight 合同与配置解析，不要先怀疑 tool loop 或 queue scheduler。

“上一轮 provider usage 明明很大，但这轮没触发压缩”的排查顺序：`effective_input_tokens` 是否真的落盘到最新 `observed_input_truth` → `actual_request_hash` 是否与上一轮 actual-request artifact 对得上 → `comparable_to_previous_request` 是否因 append-only 或 tool schema 校验失败退回 preview-only。三项都成立而 `final_estimate_tokens` 仍明显偏小，才怀疑 hybrid estimator 本身。

schema churn 停止后命中仍低时，先比较相邻节点 actual-request artifact：

- `actual_tool_schema_hash` 稳定但命中仍低 → 查 `provider_request_body.input` 是否停止 append-only；早期 `function_call` / `function_call_output` 记录被替换而非追加 → 按节点 request-scaffold 回归排查，而不是纯 tool-schema 问题。
- `prompt_cache_key_hash` 不变但 restart/resume 后第一条节点请求 `actual_request_message_count` 骤降 → 先看该轮 `request_seed_source`：`fallback_*` / `same_turn_reassembled` 即回退已现形（种子缺失、guard 降级、头漂移或尾对齐失败），按对应原因排查；`scaffold_seed*` 仍骤降才是新回归，再比 `runtime_frame.messages` 与最新节点 `actual_request_ref.request_messages`。
- 消费 append-notice 的第一条节点请求命中下降 → 预期行为是 adoption：请求以上一 artifact 的 `request_messages`（剥契约/note 后）为精确前缀、notice 作为派生 delta 追加在尾部（`request_seed_source=scaffold_seed_with_delta`）；前缀不成立时按 `request_seed_source` 的 fallback 原因排查。
- append-notice 延迟拾取分两条通道排查：`_resume_react_state(...)` 里的 resume 初始拾取（恢复后的 `run_node(...)` 第一次普通 send 之前）；`react_loop.run(...)` 里的同 run 刷新（长 turn 在较早一次模型响应后收到新 notice 时的下一个普通 `before_model` 边界）。notice 在 run 中途送达且节点继续迭代时，先查第二条通道，再假设 resume 逻辑丢了消息。
- 两条恢复通道职责分离：权威历史（`runtime_frame.messages` / `latest_runtime_messages_ref` / durable rebuild）负责 prompt 组装与 frame/展示；发送侧种子（最新 actual request 的内部形态 `request_messages`）只负责第一跳 adoption 前缀。若把 provider wire 形态的种子当权威节点历史喂回普通 prompt 组装，压缩块、运行时契约尾部与 tool-call 记录可能被重排——语义看似正确，但缓存命中大跌且没有 `token_compression`。同 run 迟到的 notice 无条件镜像进 delta 并追加到当前发送侧 scaffold（pending delta 为空时同样产出——工具轮之间到达的 notice 若只烤进投影历史，下一次 send 会因 delta 缺失退回投影重组装小请求）：不要仅从剥离的消息历史整体重建请求，也不要把 notice 插回已恢复的 `spawn_child_nodes` 或 tool-result 轮之前。
- 权威历史通道本身不得被帧重写缩减：帧正文外置在 `messages_ref` 指向的 artifact，解析失败时 hydrate 出来的帧仍带着 `phase` / `tool_calls` 等元数据、只有 messages 为空。写帧的 `_runtime_frame_record` 因此只在携带正文时才换指针，空写入保留既有 `messages_ref` / `messages_count` 并落 WARN（保留规则归 `runtime-overview.md`「任务侧」）。看到 `request_seed_source=fallback_seed_*` 且当轮 `actual_request_message_count` 骤降时，先在 worker 日志 grep `keeping existing messages_ref` / `resolved to no messages`，判定是"指针读不出来"还是"投影本身变短"——下一步完全不同。
- `resume_mode=wait_for_children` 仍有效时，刷新路径不得追加 notice；预期行为是“durable but held”：notice 保持 pending，直到活跃子轮结束，再出现在该已恢复 spawn/tool 轮之后的下一个合格普通 send。

## 6. 修改节点上下文策略时必须重点验证的地方

下面这些检查点不只适用于 CEO/frontdoor，后续改 node context strategy 时也必须逐条过。

### 6.1 stable prefix 与 live scaffold 必须分层

分别定义：stable prefix 的 durable source、fresh-turn / fresh-round 第一跳的 request scaffold、dynamic appendix 的尾部合同。不要把三者混成一个列表再寄希望于后处理自动修好。

### 6.2 durable baseline 与 request-construction scaffold 不能互相替代

节点如果也要借上一轮 actual request 保前缀，必须明确哪些内容只是“第一跳 request scaffold”，哪些内容才是 durable baseline。否则会复现 CEO/frontdoor 已经踩过的坑：planned scaffold 抢 durable baseline、durable baseline 过早丢 contract、下一轮从错误形态继续。节点侧尤其不能混用两条恢复通道：权威历史（`runtime_frame.messages` / durable rebuild）只用于 prompt 组装与 frame/展示，发送侧种子（最新 actual request 的内部形态 `request_messages`）只用于第一跳 adoption；且借用的 scaffold 不得弱化 hydration 规则。

### 6.3 tool schema 稳定性要单独验证

节点改上下文策略时不要只看 messages，必须同时检查：provider-facing tool schema 列表、schema 顺序、是否有无意义增删、这些变化是否影响 `prompt_cache_key_hash`。

维护要点：

- Wrapped OpenAI 风格 tool 定义（`{"type":"function","function":{...}}`）与扁平持久化函数记录（`{"type":"function","name":...,"parameters":...}`）算同一逻辑 tool contract。
- provider adapter 与 `actual_tool_schema_hash` / `tool_signature_hash` 诊断在 transport 或哈希之前都应走同一条共享归一化路径。
- 未来若有 provider 需要专用 tool wire format，先归一化、再投影到最终 transport 形状；不要在每个 provider 内部各自重新实现 flat-vs-wrapped 兼容。

如果 stable prefix 没变但 family key 变了，通常就是 schema churn。验收目标：合法 schema 变化后的 warm-up 轮允许 miss；schema 稳定后，后续连续节点轮应恢复到约 90% 或更高的缓存命中率。

### 6.4 overlay 只能追加，不能回写旧消息

节点的 turn overlay、repair overlay、临时诊断块，都只能当作新的 request-tail 消息追加。不能拼回已有 user 消息、不能改写 stable history、不能改写 baseline 里的旧 assistant/tool 记录。

### 6.5 same-turn append-only 与 fresh-turn continuity 要分开测

至少要两套测试：同一 turn 内 round 1 → round 2 → round 3；上一轮结束 → 下一轮 fresh turn 第一跳。两类 bug 根因完全不同：same-turn bug 更像 request growth/append-only 问题；fresh-turn bug 更像 baseline handoff / family churn / scaffold 选择问题。

两套测试都要断言同一条不变量：**每个请求尾部区域至多 1 份 turn-only note，尾部不带 `## Runtime Tool Contract` 整条消息；工具契约只以 `[TOOL_CONTRACT:<id>:<fp>]` marker 嵌在工具结果里（随结果保留到压缩）。note 排在全部携带正文之后（断点只能落在尾部），被携带前缀里 note 与旧合同消息都是 0 份**。位置断言之外还要断“活状态重写只动尾部”：同一 turn 内两次装配，若只有 `denied_tools` 变化，则前一份请求除末位 note 外的全部消息必须在后一份里原位、原字节复现。断言位置：

- same-turn：多轮 tool 调用后，检查实际请求 JSON 的 note 计数与工具契约 marker 数，以及“真实 transcript 前缀不携带陈旧契约/note”
- fresh-turn：上一请求带陈旧旧合同 / note 时，第一跳仍应 adoption scaffold 真实前缀并追加显式 delta；种子不可用导致的回退必须带 `fallback_*` 诊断，不被静默禁用

### 6.6 pause/new turn 与 ordinary fresh turn 也要分开测

节点如果以后支持 pause/resume 或类似边界，把下面三种情况拆开：普通 fresh turn、manual pause/no-provider turn、same-turn finalize after actual request。不要用一条统一的“有没有 actual-request evidence”规则粗暴处理所有场景。

### 6.7 artifact 完整性必须纳入测试与排查

对于节点上下文策略，建议新增或保留以下检查：每一次实际 provider 调用都应落 artifact；artifact 中应包含 request projection、provider transport payload、tool schemas、prompt-cache diagnostics。否则后续排 cache miss 时很难判断到底是哪一跳出了问题。

### 6.8 行上的扩展字段必须与投影、诊断哈希、线上体同源

给 assistant 行加字段（思考内容就是这一类）时，同一份字段集合必须在三处一致：durable 基线投影、进 `sanitize_provider_messages` 的诊断输入、真正发出的消息体。任何一处多带或少带，跨轮可比性判据（`comparable_to_previous_request`）与 usage-first 估算会静默退化，或让线上体与 artifact 分叉，而外表看不出异常。判据与写入也必须分开：决定"写不写进行"的闸门放在落盘点，咽喉点只做无条件透传——把策略塞进咽喉点就会让同一份历史在不同模型上给出不同形状。契约本体见 `runtime-overview.md`「思考内容（reasoning）的上下文回放」。

## 7. 建议的测试矩阵

测试矩阵至少逐条覆盖 §3 所列每个坑对应的不变量（含 §6.5 的 same-turn / fresh-turn 两套断言），另需覆盖：

- `provider_request_body.input` 与高层 `request_messages` 的前缀对比结论不能长期分叉。
- 同一批消息连过两次投影必须逐字节相同（含行上的扩展字段），否则诊断哈希与线上体分叉。
- usage 记录与 request artifact 必须能在时间线层面对得上。
- 非 `token_compression` 的 shrink 一律视为失败（唯一例外：因去累积从历史中剥掉 turn-only note / 已膨胀的旧契约且真实 transcript 未缩短，经 note 中性化比较后不算非法）。

## 8. 仍需继续盯的风险点

- pause 窗口里的 actual request artifact 是否会漏落盘
- completed continuity sidecar 是否始终跟上最新 authoritative state，同步点是否会漏写
- 某些相邻 turn 的 provider-visible tool schema 是否仍会无意义抖动
- restarted completed session 第一跳的 visible-set equality bridge 是否还会被意外放宽成 superset
- `provider_request_body.input` 与 `request_messages` 是否还存在隐藏分叉
- 节点第一跳 `fallback_seed_misaligned` 占比是否保持趋近 0：占比偏高说明尾对齐锚点在重复记录上撞车或 delta 上限设置不当，回退轮会重新引入重组装小请求
- 节点侧是否也有 finalize/direct reply 没补回 baseline 的问题

## 9. 给节点上下文策略修改者的简版原则

如果只记住几条，请记这几条：

- 先区分 family churn 还是 request-shape 断裂，再决定改哪里。
- 先看 actual request artifact，再看 transcript。
- stable prefix、durable baseline、first-hop scaffold、dynamic appendix 必须分层。
- 只有真实 provider request 才能推进 durable baseline。
- same-turn append-only 和 fresh-turn continuity 是两套不同问题。
- fresh-turn 第一跳回退必须带 `request_seed_source`；静默回退按 bug 处理。
- tool schema 稳定性和消息前缀稳定性要一起验证。
- artifact 不全时，先修 artifact，再信任何 cache 结论。
