# G3KU 上下文与缓存排查指南

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

- 如果 `frontdoor_token_preflight_diagnostics.final_request_tokens` 已经接近 `effective_trigger_tokens`，但 usage 还是明显更高，优先按“估算偏小”排查，不要先怀疑 cache family churn。

常见情况：`request_messages` 看起来公共前缀很长，但 `provider_request_body.input` 的第一个分叉点更早——说明高层 projection 没问题，真正的问题出在 adapter 最终 payload。

Memory guard 维护要点：

- frontdoor 与节点的 actual-request 持久化都带 memory guard：正常路径完整保留 `request_messages`、`tool_schemas` / `actual_tool_schemas` 与 adapter-final `provider_request_body`；节点侧正常路径直接写专用 JSON，只有该写入本身命中 `MemoryError` 才降级。
- 若 artifact 带 `artifact_persistence_mode=memory_guard_degraded` 或 `memory_guard_minimal`：它仍能证明这次 send 发生过，但 `provider_request_body` 可能故意为空，`request_messages` 可能是剥离后的取证摘要（大请求内容可能移入 `request_messages_summary` / `provider_request_body_summary`）。
- 此时不要仅凭 `provider_request_body.input` 缺失就断言“provider 从未看到图像”；应视为“artifact 写入成功，但原始 transport payload 为避免 turn 级 `MemoryError` 被省略”，再与相邻 artifact、usage、transcript 时间线互相印证。

### 2.3 计费行与 actual request JSON 可能并不总是一一对应

已踩坑：同一时间窗里 usage/billing 出现了 `/responses` 调用，但对应窗口没有落地 `frontdoor-request-*.json`；你以为某个 persisted request 对应某条计费记录，实际那条记录可能来自另一条隐藏/漏落盘请求。

维护要点：

- 区分 “artifact 缺失” 和 “artifact 被 memory guard 降级”：只要文件存在且带 `artifact_persistence_mode!=full`，它仍能证明这次 send 发生过，只是不能再逐字节证明完整的原始多模态 payload。
- manual pause、pause 后新 turn、internal/hidden round 这些窗口，必须先确认 artifact 是否完整。
- 每次怀疑缓存异常时，先检查 artifact 时间线是否完整；如果 artifact 不全，先修 artifact，再改上下文策略，再信任何 cache 结论。

## 3. 已经确认踩过的坑

### 3.1 同 turn 的 append-only 规则被破坏

- 坑：旧 contract snapshot 被提前剥掉，新 round 直接从 stripped body 继续，provider 前缀在第 3 条就断；或 overlay 被错误拼回已有消息，而不是作为新的 request-tail 消息追加。
- 不变量：同一 visible turn 内 request 只增长、不重排；`request_messages` 在真实 transcript（system/user/历次 assistant 工具调用 + 工具结果）上保持 append-only，命中前缀逐轮变长。每个请求尾部区域恰好 1 份最新 `frontdoor_runtime_tool_contract` / `node_runtime_tool_contract`，被携带历史里 0 份；turn-only note / 当前 user 回合或合法的 assistant/tool 序列之前，契约不占据模型回复位置，末位保持当前 user 回合或合法的后续 assistant/tool 结果；契约块占据末位会诱导模型把契约抬头回显进下一条回复，而携带工具调用的回显消息若被当成契约剥离会留下孤儿工具结果。契约块以 `## Runtime Tool Contract` 开头的 system 消息形式出现（运行时元数据、非对话内容，避免模型把它错当"自己上一轮说的话/发给用户的清单"），仍属于 stable prefix 之外的动态契约尾记录，每轮整份替换；剥离尾部契约/note 不算非法 shrink，也不算前缀断裂。durable baseline / continuity 持久化前必须剥掉契约 summary，以及 `## 长期记忆` 这类当轮 overlay 的 assistant 记录（它们只在当轮请求可见，落史会逐轮累积）；后续轮若把旧 summary / overlay 当普通稳定历史重放而没有 `token_compression` / `stage_compaction`，按非法上下文携带排查。节点同 turn 请求同样走 append-only scaffold：上一请求 body + 上一轮 assistant/tool delta + 最新契约 / turn-only note 尾部（契约在前、note 在后）。
- 症状：命中前缀不再逐轮变长；前缀在契约或 overlay 位置提前分叉；旧契约出现在历史中段；或模型最终文本 / 渠道消息以 `## Runtime Tool Contract` 开头。

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
- 不变量：当前 turn 已有真实 provider request 落盘后，`finalize` 阶段产出的可见最终回复必须补回 session-owned baseline；该规则不限于 `direct_reply`，普通工具（如 `message`）之后产出的用户可见 `final_output` 同样要补回。该规则同样覆盖 heartbeat/cron 内部回合的可见回复：补回与否按回复内容判定（非空且非 `HEARTBEAT_OK`），不看回合类型，仅静默 ACK（空输出或 `HEARTBEAT_OK`）豁免。“同 turn 已有 actual-request 证据”与“fresh turn 没有 actual-request 证据”是两种不同状态，不能用同一条覆盖规则处理。
- 症状：transcript 里能看到上一轮最终回答，但下一轮第一跳 request body 里完全没有它。

### 3.5 普通 fresh-turn 第一跳没有沿用上一轮 actual request scaffold

- 坑：fresh turn 直接从 stripped body + 新 user + 新 contract 开始，第一条 provider request 在索引 3 就和上一轮分叉；或 fresh-turn seed 按原始 message dict 逐字节比对 `stable_messages[:body_len] == previous_request_body`，被微小格式漂移（如某条 `tool` 输出末尾空格被后续规范化裁掉）击穿，误判“不再是同一条 baseline”而退回未复用路径。
- 不变量：durable baseline 可保持 stripped/finalized 形态，但普通 fresh-turn 第一条 provider request 应借上一轮 persisted actual request scaffold 保前缀；scaffold 只是 request-construction aid，不能反过来变成新的 durable source of truth。seed 判定基于 provider-facing 结构等价，最多容忍无语义影响的行尾空白 / 换行归一化差异，不做脆弱的原始 dict 全等比较。
- 症状：每个新 turn 第一跳都在低索引处与上一轮分叉。

### 3.6 跨普通 fresh turn 的 tool schema churn

这类问题会直接造成 `prompt_cache_key_hash` 变化，因为 family key 会把 stable prefix 和 tool schemas 一起算进去。

- 坑：相邻两轮 stable prefix 没变，但 provider-visible `tool_schemas` 发生不必要变化（某工具在后一轮出现或消失，例如 `message`），family key 跟着 churn；哪怕 request body 还能复用一部分前缀，caller-side family 也已经断了。
- 字段语义：`tool_names` 是当前轮权威 callable 合同；`provider_tool_names` 只是用于稳定缓存前缀的 provider-facing schema bundle；`pending_provider_tool_names` 与 `provider_tool_exposure_commit_reason` 是兼容字段，新写入保持 `[]` / `""`；`provider_tool_exposure_revision` 标识该次 send 实际到达 provider 的 active bundle。
- 不变量：普通 fresh turn 第一跳的 provider-facing `tools[]` 必须继续使用上一轮已提交的 active schema anchor；“当前 visible tool set 是上一轮超集”不是自动扩张 schema 的合法理由。当前轮因 RBAC、hydration、阶段状态变化得到的新 desired provider tool 集，只在下一次 membership 刷新点提交进 active bundle；刷新前的各次 send 仍按已持久化的 active bundle 构造，不得按 desired 集扩张。`stage_compaction`、fresh-turn continuity、pause/resume、completed continuity restore 都不能让 provider schema 自动扩张或切换。RBAC 收回在执行时立即生效，即使 active provider schema 尚未收敛；membership 刷新前 active provider schema 与 RBAC 可见性的短暂滞后是预期的，收回后的权限副作用不是。
- 症状：没有真实 membership 变化却在同一轮内观察到 provider-visible `tool_schemas` 切换（或 `provider_tool_exposure_revision` 在 desired 集不变时变化）——直接按 runtime bug 排查。

### 3.7 manual pause 之后的新 turn，与“暂停回复”不是一回事

- 坑：把“pause 后用户发的新 turn”误判成“暂停回复”，错误地拿 pause ack 或 paused snapshot 去解释 cache miss。
- 不变量：`已暂停` 是上一轮的 pause ack；pause 之后用户再发消息是全新的 visible turn，按 fresh-turn continuity 规则排查。对普通用户 manual pause，后端会把当前轮 terminalize 成 `completed`；若还在按长期 paused/resume 语义排查，结论通常会跑偏。
- 症状：用错恢复源，第一跳形态与真实上一轮请求对不上。

### 3.8 重启后继续，不能退回 transcript/history fallback

- 坑：项目重启后 reopened completed session 直接从 transcript/history 重建；`frontdoor_request_body_messages`、actual-request scaffold、stage/compression 状态没有按最新 authoritative state 恢复，表现成“上下文像还在，但第一跳 cache 命中突然掉光”。
- 不变量：恢复顺序见 §4 的 baseline/restore 顺序；sidecar 必须携带可用 baseline 才算获胜。节点侧同理：节点重启/resume 后第一条重建 provider request 必须继续使用持久化的 append-only scaffold，即使 durable `runtime_frame.messages` 更短（`runtime_frame.messages` 只是 projected-history 视角，`actual_request_ref.request_messages` 才是第一跳重建的权威）。若恢复出的 `frontdoor_actual_request_path/history` 缺失、不可读或 body 不匹配，用最新匹配的 actual-request artifact 做 trace enrichment：只修复 request-trace 权威，不得用 artifact 的完整 same-turn scaffold 替换恢复出的 baseline，也不得凭空制造 visible-set bridge 资格。
- 边界：只有当 continuity 快照里的 visible tool/skill 集与当前完全一致（`visible_tool_ids` / `visible_skill_ids`）时，第一跳才继续借用上一轮的 family/schema anchor（`provider_tool_schema_names` / `cache_family_revision`）；visible 集漂移时，上下文仍应恢复，但 cache miss 可接受，不应误判成上下文丢失。
- 症状：重启后第一跳 request 明显变短却没有合法 shrink reason。

### 3.9 节点 selection cache / frame restore 漂移，不等于 prompt 自己丢了 skill

常见于 execution / acceptance 节点：

- 坑：节点某一轮先拿到空的 `contract_visible_skill_ids` / `candidate_skill_ids`，之后外部 resource/governance refresh 已把 skills 或 tools 刷进 live runtime，但节点仍沿用旧的 node-context selection cache 或旧的 persisted frame restore。
- 不变量：节点运行时在复用 cached / restored selection 前，必须至少比对 `session_key`、`actor_role`、`visible_tool_names`、`contract_visible_skill_ids`、`registry_skill_ids`；任一字段漂移就丢弃旧 selection，回到 `_node_context_selection_inputs()` 与 `build_node_context_selection(...)` 重算。“旧 frame 里是空 skill 集”不再自动代表“下一轮也应继续空”，必须先确认 live visibility 仍为空。
- 症状：`load_skill_context(skill_id="...")` 明明应可用却一直报当前候选技能未包含目标 skill；`runtime frame` / `runtime-frame-messages` 里 `candidate_skill_ids` 连续多轮不变，而 `visible_tool_names` / `contract_visible_skill_ids` / `skill_visibility_diagnostics.registry_skill_ids` 已能看到新值。这属于 runtime freshness gate / cache invalidation 回归，不是 prompt wording 问题。

### 3.10 heartbeat / cron 按普通 continuation shrink 规则排查

Heartbeat / cron 不再在主 CEO/frontdoor 路径上使用单独的短 `ceo_heartbeat` 通道，而是把隐藏 internal prompt 消息追加到普通 turn 共用的 session-owned `frontdoor_request_body_messages` / actual-request scaffold 上。

- 坑：把 heartbeat shrink/缓存差异解释成“现在是另一条 prompt 通道”；或把内部 heartbeat artifact 直接提升为新的 durable baseline。
- 不变量：heartbeat/cron 请求变短必须由 `token_compression` 或 `stage_compaction` 解释；隐藏的 heartbeat 规则与 event-bundle 消息是 durable prompt history，应出现在 actual-request artifact、completed continuity sidecar 与 prompt assembly 中（UI 以 `ui_visible=false` 隐藏）。若候选 body 以 heartbeat 文本为主、明显劣于现有 baseline 且无合法 shrink reason，视为被拦截的 baseline 覆盖，内部请求只作 forensics-only 证据。
- 冷启动边界：内部事件消息只有在存在真实续跑基线时才并入续跑种子；无基线的内部轮（重启后首轮、全新会话首轮）必须走新建组装路径注入内部消息，基础系统提示保持首位。绝不能让仅有的内部事件消息冒充"完整旧请求体"触发续跑分支——续跑分支假定种子自带基础系统提示，会把基础提示静默丢掉，表现为内部轮模型行为失范、看不到人设/规则。
- 症状：把上一真实请求与新的 heartbeat/cron 请求作 append-only 续接比较时出现大面积前缀断裂——通常说明 baseline 恢复、tool-schema seeding 或压缩发生了变化，而不是 runtime 切到了单独通道。

### 3.11 残留 paused 转录条目反复补进种子尾部

- 坑：手动暂停终止的回合把用户消息以 `_transcript_state=paused` 写进转录；暂停回合不会再走回合完成路径，缺少退役路径时条目一直卡在 paused。对账在每个回合 prepare 阶段把它补到种子尾部——紧邻当前用户消息、中间没有任何助手回复——轮末回写后嵌进基线；`token_compression` 把它改写掉，下一轮对账又重新补到新请求尾部。同一条问题以“从未被回答的用户提问”形态在请求尾部反复出现，诱导模型重复处理早已回答过的问题。
- 不变量：残留 paused 条目随下一个用户可见回合正常完成被对账退役一次；运行时错误路径不退役（出错回合的请求体未必完成基线回写，提前退役会让对账停止补发、用户消息从模型上下文永久消失）。退役与对账的完整生命周期见「Baseline 合同与恢复顺序」。
- 症状：连续多轮 actual request artifact 里同一条历史用户消息反复出现在请求体尾部，紧邻当前轮用户消息且前后都没有助手回复；模型重复处理或批量补答早已回答过的历史问题，甚至把多条这样的消息合并成一条虚构的用户请求。

## 4. Prompt Cache Family 与 Actual Request

本节是 actual request 的取证合同：family/key 语义、per-request 取证顺序、baseline 与恢复顺序、shrink 原因边界。

### 4.1 Family 与 key 合同

- caller-side prompt cache family 只由 stable prefix 加显式 cache-family revision 输入定义；普通 callable/candidate/hydrated 工具漂移、阶段门控 schema 变化、loader 触发的 hydration 升级可以改变 actual request，但不得单独轮转 family key。
- CEO/frontdoor `call_model` 必须把为该轮重建的 request 与匹配重建的 `prompt_cache_key` 一起发送；重建 request 配旧 key 属于 runtime bug——它会掩盖 miss 到底来自 family churn 还是请求本身变化。节点同理：静态前缀保持前置 append-only、尾部恰好 1 份当前契约，`prompt_cache_key_hash` 与 `actual_request_hash` 分开排查。
- provider-facing `tools[]` 保持与曝光可见的具体工具集对齐的稳定超集，而不是更窄的当前轮 callable 集；bundle 刻意最小化，丰富的工具/技能说明放在尾部契约里，让 cache miss 更容易归因到真实请求增长而不是 schema 文本漂移。

### 4.2 Per-request 取证顺序

- CEO/frontdoor 与节点都为每次 `call_model` 落 per-round actual-request artifact。frontdoor 写 `.g3ku/web-ceo-requests/<session>/...json`，session snapshot 只保留最新文件路径与短元数据历史——per-round JSON 是精确请求取证权威，快照保持轻量以服务 websocket restore 与 UI 调试。
- `visible_frontdoor`、`token_compression`、`inline_tool_reminder` 都落在同一 artifact 族：普通 send 与 internal subrequest 可以从同一条时间线解释。
- artifact 内保存 runtime-side 投影（`model_messages` / `request_messages`、`actual_tool_schemas`、cache-family 诊断）与可用时的最终 transport payload（`provider_request_meta`、`provider_request_body`）；`request_messages` 是保存请求顺序的兼容权威，不要重新引入旧 `messages` 全量镜像。
- 每条 artifact 还带归一化的 `usage`、`frontdoor_history_shrink_reason`、`frontdoor_token_preflight_diagnostics`：用它们比较“preflight 以为要发什么”与“provider 实际计费了什么”，不要只靠 transcript 时间线重建这层关系。
- CEO/frontdoor artifact 另带首跳取证三字段：`turn_inbound_received_at`（bridge 接收回合时写入用户消息 metadata）、`provider_request_started_at`（该 `call_model` 首次真正发请求前写入；provider 重试沿用同一值）、`inbound_to_request_start_seconds`（两者差值，缺少入站时间戳时为 `null`）。`created_at` 是响应完成后的 artifact 写入时间，不能用它归因派发、转录持久化、prompt 组装或 preflight；“入站后迟迟没有请求”先看这三个字段。
- `.g3ku/web-ceo-requests/<session>/` 保留最新 300 份 artifact，以及被 paused/inflight/completed sidecar 引用的 artifact；更早且未被引用的文件按写入计数定期清理（每 25 次持久化触发一次，因此磁盘上限为 keep + 25）。排查长时间线时按现有 artifact 与 continuity history 的 `path` 对齐，不因目录缩短就认定历史请求不存在。
- 前端每轮 token 展示与本取证合同同源：实时轮次读会话内存累积（`_frontdoor_turn_usage`），历史轮次重载按 `turn_id` 聚合本目录 artifact 的 `usage`（`input_tokens` / `output_tokens` / `cache_hit_tokens`）。聚合只读计费三通道、只服务 UI 展示，不参与 baseline/恢复判定——UI 合同见 `web-and-admin.md`「Per-Turn Token Usage Contract」。
- 节点同样为每次 `call_model` 写专用 JSON，`actual_request_ref` / latest-context 指向该 artifact，不复用 runtime-frame `messages_ref`。restart/resume 的第一跳可以从最新 `actual_request_ref.request_messages` 取种子，让 same-turn append-only 增长跨过进程重启；该种子是一次性 scaffold，只服务第一条重建请求，一旦本轮重建出 pending tool/child turn 或发出新真实请求即丢弃，不得替换 durable 节点历史。

### 4.3 Baseline 合同与恢复顺序

- `RuntimeAgentSession._frontdoor_request_body_messages` 是 session-owned request-body baseline：保存剥掉动态契约后的重建 provider request body，下一轮在尾部重建唯一一份新的权威契约。它是普通可见 turn 的 append-only 续接来源（不是旧会话兼容回退）：新 user/runtime 尾部内容直接追加其上，不再交给历史选择器判断“语义完整性”。
- visible turn 期间 baseline 必须跟上真实请求增长：每次 provider 调用后反映剥契约后的最新 body，每个工具循环后把新 assistant/tool transcript 折回同一份 baseline。它也必须跨过 finalize 与可恢复快照边界存活：`inflight_turn_snapshot` / paused execution context 必须连同 `frontdoor_history_shrink_reason` 一起携带它，否则重建的会话会悄悄退回 transcript/history replay，在允许路径之外缩短下一轮。
- 手动暂停发生在该轮首次 provider 请求发出之前时，被暂停回合的用户消息不进入基线（基线只在有真实 actual-request 证据后回写），而续跑种子路径也不读转录，该消息会因此从后续模型上下文缺席。prepare 阶段对续跑种子做转录对账来保住它：把 `_transcript_state=paused` 且 prompt-visible 的用户回合按转录顺序补到种子尾部；与种子既有用户消息或当前回合用户消息同文本的不重复补。对账只是尾部追加——不改动缓存前缀、不构成收缩、不改变恢复顺序。paused 条目没有独立的完成路径：暂停回合本身不会再走回合完成路径，残留 paused 条目随下一个用户可见回合正常完成被对账退役一次（同一路径同步执行 `clear_paused_execution_context()` 清掉暂停执行上下文）——此时它要么已被对账进种子并得到处理，要么已被新输入取代。运行时错误路径不退役 paused 条目：出错回合的请求体未必完成基线回写，提前退役会让对账停止补发，用户消息会从模型上下文永久消失。卡在 paused 的条目会触发「残留 paused 转录条目」陷阱。
- `frontdoor_canonical_context` 保持 durable 单写者：turn 收尾可以合并已完成阶段数据，但 session 同步与请求组装不得把 visible projection 写回 durable 链。副本折叠规则详见 `runtime-overview.md`「CEO Frontdoor Canonical Context Contract」；continuity sidecar 的阶段记录数应等于 distinct stage 身份数，持续增长即合并边界回归。
- UI 最新气泡把历史阶段全部叠加的排查点：Web UI delta 必须由 `ui_canonical_context_delta()` 同视图投影生成（旧阶段沿用基线表示、只回填 delta 保留阶段的实时正文）；若最新气泡阶段数等于历史阶段总数，先确认 delta 生成未退回原始 `canonical_context_delta`。合同见 `runtime-overview.md`「CEO Frontdoor Canonical Context Contract」。
- 图像边界：只有所选模型绑定启用图像多模态输入时，当前 turn 的 live request 才允许 provider 可见图像块，且同轮须把附件提示替换为直接视觉引导（不得暴露本地上传路径）；历史图像经 `content_open` 重开是独立的 live-only 通道——durable baseline 可保留 `path` / `ref`，但直接视觉复用需要后续 turn 重新 `content_open`。durable baseline、inflight/paused snapshot、completed continuity sidecar 持久化前必须把 `image_url` / `input_image` 剥回文本投影；saved actual-request artifact 是刻意的取证例外——验证图像是否到达 provider 要查 artifact，而不是 durable baseline。
- CEO continuity 恢复顺序：paused snapshot → inflight snapshot → completed continuity sidecar（`.g3ku/web-ceo-continuity/<session>.json`）→ 最新 actual-request artifact → transcript/history fallback。前三条是可信 sidecar 通道，第四条是保缓存的应急回退；sidecar 只有真正携带可用 `frontdoor_request_body_messages` baseline 时才获胜，文件存在本身不得压制更晚、更完整的恢复源。
- 重启恢复刻意分两步：先从最高优先级 sidecar 恢复 durable `frontdoor_request_body_messages` baseline，再校验恢复出的 actual-request trace 仍指向可读、且 durable body 与该 baseline 匹配的 artifact；不匹配时按 §3.8 做 trace enrichment。
- completed continuity sidecar 在每次真实 provider-backed 同步与 terminalized manual stop 之后，持久化最新权威 baseline、actual-request trace、stage/canonical/compression 状态、hydrated tools 与最近一次 visible tool/skill exposure 快照。写入与恢复覆盖所有 CEO frontdoor 会话命名空间（`web:`、`china:`、`cron:`）——渠道会话与兜底 cron 会话的基线同样跨进程重启存活；`task:` 等非 frontdoor 命名空间不参与该生命周期。渠道会话因此不会在重启后丢失基线、退化成无基础系统提示的冷启动。
- reopened completed session 把上一完成轮的 actual-request trace 恢复进当前会话状态，供下一个 fresh visible turn 滚入 `previous_*`；没有合法 shrink reason 时，不允许从“匹配的上一请求 scaffold 可用”悄悄退回“durable-only 第一跳”——先查 trace enrichment，再怪 prompt assembly。
- 若 fresh visible / heartbeat turn 发现内存 baseline 为空而 paused/inflight snapshot 或 completed continuity sidecar 里仍有，prompt assembly 应恢复该 baseline 并立即视为 session-owned append-only 前缀——按 baseline restoration 排查，不是普通 history compaction 路径。

### 4.4 Shrink 原因与压缩边界

- `frontdoor_history_shrink_reason` 是 prompt assembly 与 session 持久化之间的运行时合同：只有 `token_compression` 与 `stage_compaction` 是下一轮 baseline 变短的合法理由；否则按 runtime bug 排查，不要解释成“正常上下文整理”。
- `stage_compaction` 是按阶段归属的原位压缩：过期完成阶段的工具调用消息成对移除，compact 块插回该阶段首条被移除消息的位置（既有块按记忆位置回插），阶段外的用户可见对话原位保留。内部事件束按是否承载因果拆两类处理：**事件体保留**——心跳事件束（`## EVENT BUNDLE`）、定时任务中文包装与 `[CRON INTERNAL EVENT]` 是后续回合追溯"这一轮为什么做这些动作"的因果载荷，压缩不再删除，否则心跳/定时通过开阶段处理问题后下一轮会失去触发上下文、把孤立的工具流水误读成"无事发生/被拦截"；**规则文本移除**——`This is a background heartbeat.` / `# Heartbeat Rules` 这类每次重复注入的框架规则按缓存中性规则清理：只移除不早于本次压缩既有最早结构变化点的条目；本次压缩没有任何结构变化时一律保留、留待后续压缩顺路清理，因此“历史内部事件仍在携带前缀里”本身不是非法 shrink，也不得通过请求组装期的逐轮过滤制造新的前缀断裂。缓存不变量：压缩块不收拢到上下文头部；除治愈遗留布局的一次性收敛外，每次压缩相对上一请求的首个分叉点是最早被压缩阶段的位置，而不是历史头部；同一输入二次重写必须收敛为同一输出（幂等）。排查“某一轮起缓存命中骤降”且 shrink reason 为 `stage_compaction` 时，先比对首个分叉点是否落在最早被压缩阶段处——分叉出现在历史头部（压缩块置顶或整体重排）按块放置回归排查。
- `token_compression` 是 inline LLM 重写，仅当估算的 provider-bound request 超过所选模型 `context_window_tokens` 的 `80%` 且仍在窗口内时运行；估算已超窗时必须在发送前失败，不做任何 semantic/global-summary 回退；inline 压缩后重算仍超窗，则以同样的 context-window 错误失败。
- 两种 shrink 原因都不得轮转 provider-facing `tools[]`：`stage_compaction` 必须保持 active `provider_tool_names` 不变；`token_compression` 所在 send 沿用压缩前已持久化的 bundle 保持不变。RBAC / hydration 驱动的 provider `tools[]` 同步只发生在 membership 刷新点，不得由压缩路径触发。
- manual pause 与压缩的两条边界：压缩进行中暂停会丢弃迟到的压缩结果，下一可见轮从当前权威 baseline 重新走 prepare → estimate → 可选压缩 → send；若 turn 在最终 preflight 即将进入 `token_compression` 时暂停，下一 fresh turn 可从 pending shrink marker、或经上一 actual-request history 关联的后续内部 `token_compression` artifact 恢复 `frontdoor_history_shrink_reason=token_compression`——这是“压缩刚开始就暂停”的合法竞态解决路径，不是任意裁剪。
- 完整压缩合同详见 `runtime-overview.md`「Frontdoor Context Compression (Current Contract)」。

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

允许的 shrink 原因只有 `token_compression` / `stage_compaction`（压缩触发边界、失败模式与暂停竞态见 §4.4）。另有两类**不**算非法 shrink 的合法变化：

- 从被携带历史中剥掉列尾的 turn-only note / 已轮次膨胀的旧契约——前端 shrink 守卫已对 note 做中性化比较，不会误判
- 去累积导致的单轮“尾部被剥再重注”，只要 body 的真实 transcript 没有缩短，就不算 shrink

下一轮 baseline 变短却没有上述原因之一：按 runtime bug 处理，不要解释成“正常上下文整理”。

### 5.5 frontdoor token preflight 之后要看哪份诊断

CEO/frontdoor 在真正发 provider 请求前有最后一层 token preflight。排查顺序：先看 actual request artifact（确认最终 `request_messages` / `provider_request_body`）→ 再看 `frontdoor_token_preflight_diagnostics` → 最后回头看 builder 的 `pre_summary_prompt_tokens`。builder 里的 token 估算只说明“组装阶段看到的上下文压力”；真正决定本轮是否在发送前压缩的，是 provider-send 前的最终 preflight。

- `frontdoor_token_preflight_diagnostics.applied=true` 时，预期 `frontdoor_history_shrink_reason` 为 `token_compression`；diagnostics 显示已触发但 reason 不符，优先按 runtime bug 排查。
- diagnostics 显示未触发但请求仍明显缩短：回到 `stage_compaction` / continuity baseline handoff 链路，不要把所有缩短都归因到 token preflight。

关键 ground-truth 字段：

| 字段 | 排查含义 |
| --- | --- |
| `effective_input_tokens` | 上一轮 provider 输入规模的真值 |
| `delta_estimate_tokens` | 相对上一请求的增量估算 |
| `comparable_to_previous_request` | 为 `false` 说明退回 preview-only 估算；先查不可比原因，而不是先怀疑阈值 |
| `estimate_source=usage_plus_delta` | continuity 已确认足够稳定；`final_request_tokens` 可能明显高于 preview-only 估算 |
| `estimate_source=preview_estimate` | 不一定是 usage 缺失，也可能只是 continuity 不可证明 |
| `observed_input_truth.source=preflight_estimate` | provider 未给出可用输入侧 usage，runtime 用最终 send 时估算兜底；预期回退，不自动是 cache bug |
| `pre_compaction_*` | top-level `final_request_tokens` / `estimated_total_tokens` 已是压缩后值时，用它们解释“为什么这轮会先压缩” |

另要排一类隐蔽误判：preflight estimator 必须基于原始 `provider_request_body` 估算，不能复用面向摘要/展示的 serializer 再把超长字段截成固定前后两段后估算。典型症状：`provider_request_body` 明明已经很大，`final_request_tokens` 却长期卡在异常偏小、几乎不随请求增长变化的常数——先按“estimator 低估导致压缩阈值永远打不到”排查，不要先怀疑 `trigger_tokens` 配置失效。

### 5.6 节点侧排查要点（preflight / scaffold / append-notice）

execution / acceptance 节点在真正发 provider 请求前也走最后一层 node send token preflight，包括 `message_distribution` 控制轮。两个边界必须分清：`message_distribution` 包含在 node send preflight 里；`spawn review` 是外部检验通道，故意不在 node preflight 合同里，不要混为一谈。

先看节点 runtime frame 或 actual-request artifact 里的：

| 字段 | 用途 |
| --- | --- |
| `token_preflight_diagnostics` | 是否触发发送前压缩 |
| `history_shrink_reason` | 核对合法 shrink 原因 |
| `prompt_cache_key_hash` | family 是否 churn |
| `actual_request_hash` | 请求形态是否变化 |
| `actual_request_message_count` | 消息数是否骤降 |

preflight 判定：

- `applied=true` 预期 `history_shrink_reason=token_compression`；actual request 变短但 `prompt_cache_key_hash` 没变，是“live request 被压缩但 caller-side family 未换”的正常行为，不是 family churn。
- 节点 restart / resume 后的第一跳新请求可以复用“已经过 token compression 的 actual request scaffold”；这是合法延续路径，不是 context loss。
- 节点 diagnostics 的 top-level 字段同样在 compaction 后切到“最终真正要发的 request”，压缩前 hybrid 判断保留在 `pre_compaction_*`。
- 节点侧不再把 inline image `data:` URL 的 base64 字符串按普通文本估 token；当前轮 `content_open` 刚打开多张图片而下一跳超窗时，优先从 `estimated_image_tokens` / `image_count` 解释，不要再把根因归结为“base64 文本被算爆”。

preflight 在节点端发送模型前就失败时：先看是否 `context_window_tokens <= 25000` 这类硬错误配置，再看 preview builder / provider payload 估算错误；这类“没有模型请求”的卡顿优先查 preflight 合同与配置解析，不要先怀疑 tool loop 或 queue scheduler。

“上一轮 provider usage 明明很大，但这轮没触发压缩”的排查顺序：`effective_input_tokens` 是否真的落盘到最新 `observed_input_truth` → `actual_request_hash` 是否与上一轮 actual-request artifact 对得上 → `comparable_to_previous_request` 是否因 append-only 或 tool schema 校验失败退回 preview-only。三项都成立而 `final_estimate_tokens` 仍明显偏小，才怀疑 hybrid estimator 本身。

schema churn 停止后命中仍低时，先比较相邻节点 actual-request artifact：

- `actual_tool_schema_hash` 稳定但命中仍低 → 查 `provider_request_body.input` 是否停止 append-only；早期 `function_call` / `function_call_output` 记录被替换而非追加 → 按节点 request-scaffold 回归排查，而不是纯 tool-schema 问题。
- `prompt_cache_key_hash` 不变但 restart/resume 后第一条节点请求 `actual_request_message_count` 骤降 → 先比 `runtime_frame.messages` 与最新节点 `actual_request_ref.request_messages`；该模式通常是恢复后的第一跳从 projected history 重建，而不是从持久化 actual-request scaffold 重建。
- 消费 append-notice 的第一条节点请求命中下降 → 校验新 `provider_request_body.input` 在 JSON 归一化后逐字节以前一 artifact 的 `provider_request_body.input` 开头；预期行为是 append-only notice 投递（旧 provider 可见上下文保持前缀，新 notice 追加在尾部）。
- append-notice 延迟拾取分两条通道排查：`_resume_react_state(...)` 里的 resume 初始拾取（恢复后的 `run_node(...)` 第一次普通 send 之前）；`react_loop.run(...)` 里的同 run 刷新（长 turn 在较早一次模型响应后收到新 notice 时的下一个普通 `before_model` 边界）。notice 在 run 中途送达且节点继续迭代时，先查第二条通道，再假设 resume 逻辑丢了消息。
- 常见回归是混用两条恢复通道：用于 prompt 组装的权威历史（`runtime_frame.messages` / `latest_runtime_messages_ref` / durable rebuild）与来自最新 actual request 的发送侧种子 scaffold（`provider_request_body.input`）。若把 provider 形态的种子当权威节点历史喂回普通 prompt 组装，stage-compaction 块、运行时契约尾部与 tool-call 记录可能被重排——语义看似正确，但缓存命中大跌且没有 `token_compression` / `stage_compaction`。同 run 迟到的 notice 应作为增量 delta 追加到当前发送侧 scaffold：不要仅从剥离的消息历史整体重建请求，也不要把 notice 插回已恢复的 `spawn_child_nodes` 或 tool-result 轮之前。
- `resume_mode=wait_for_children` 仍有效时，刷新路径不得追加 notice；预期行为是“durable but held”：notice 保持 pending，直到活跃子轮结束，再出现在该已恢复 spawn/tool 轮之后的下一个合格普通 send。

## 6. 修改节点上下文策略时必须重点验证的地方

下面这些检查点不只适用于 CEO/frontdoor，后续改 node context strategy 时也必须逐条过。

### 6.1 stable prefix 与 live scaffold 必须分层

分别定义：stable prefix 的 durable source、fresh-turn / fresh-round 第一跳的 request scaffold、dynamic appendix 的尾部合同。不要把三者混成一个列表再寄希望于后处理自动修好。

### 6.2 durable baseline 与 request-construction scaffold 不能互相替代

节点如果也要借上一轮 actual request 保前缀，必须明确哪些内容只是“第一跳 request scaffold”，哪些内容才是 durable baseline。否则会复现 CEO/frontdoor 已经踩过的坑：planned scaffold 抢 durable baseline、durable baseline 过早丢 contract、下一轮从错误形态继续。节点侧尤其不能混用两条恢复通道：权威历史（`runtime_frame.messages` / durable rebuild）只用于 prompt 组装，发送侧种子（最新 actual request 的 `provider_request_body.input`）只用于第一跳取种子；且借用的 scaffold 不得弱化阶段门控或 hydration 规则。

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

两套测试都要断言同一条不变量：**每个请求尾部区域恰好 1 份契约、至多 1 份 turn-only note，契约排在 note / 当前 user 回合之前（末位是 user 消息），被携带前缀里契约与 note 都是 0 份**。断言位置：

- same-turn：多轮 tool 调用后，检查实际请求 JSON 的契约/note 计数，以及“真实 transcript 前缀不携带陈旧契约/note”
- fresh-turn：上一请求带陈旧契约/note 时，第一跳 scaffold 仍应正确回退/复用真实前缀，不被静默禁用

### 6.6 pause/new turn 与 ordinary fresh turn 也要分开测

节点如果以后支持 pause/resume 或类似边界，把下面三种情况拆开：普通 fresh turn、manual pause/no-provider turn、same-turn finalize after actual request。不要用一条统一的“有没有 actual-request evidence”规则粗暴处理所有场景。

### 6.7 artifact 完整性必须纳入测试与排查

对于节点上下文策略，建议新增或保留以下检查：每一次实际 provider 调用都应落 artifact；artifact 中应包含 request projection、provider transport payload、tool schemas、prompt-cache diagnostics。否则后续排 cache miss 时很难判断到底是哪一跳出了问题。

## 7. 建议的测试矩阵

测试矩阵至少逐条覆盖 §3 所列每个坑对应的不变量（含 §6.5 的 same-turn / fresh-turn 两套断言），另需覆盖：

- `provider_request_body.input` 与高层 `request_messages` 的前缀对比结论不能长期分叉。
- usage 记录与 request artifact 必须能在时间线层面对得上。
- 非 `token_compression` / `stage_compaction` 的 shrink 一律视为失败（唯一例外：因去累积从历史中剥掉 turn-only note / 已膨胀的旧契约且真实 transcript 未缩短，经 note 中性化比较后不算非法）。

## 8. 仍需继续盯的风险点

- pause 窗口里的 actual request artifact 是否会漏落盘
- completed continuity sidecar 是否始终跟上最新 authoritative state，同步点是否会漏写
- 某些相邻 turn 的 provider-visible tool schema 是否仍会无意义抖动
- restarted completed session 第一跳的 visible-set equality bridge 是否还会被意外放宽成 superset
- `provider_request_body.input` 与 `request_messages` 是否还存在隐藏分叉
- 节点侧是否也存在“stripped durable baseline”和“first-hop scaffold”混淆
- 节点侧是否也有 finalize/direct reply 没补回 baseline 的问题

## 9. 给节点上下文策略修改者的简版原则

如果只记住几条，请记这几条：

- 先区分 family churn 还是 request-shape 断裂，再决定改哪里。
- 先看 actual request artifact，再看 transcript。
- stable prefix、durable baseline、first-hop scaffold、dynamic appendix 必须分层。
- 只有真实 provider request 才能推进 durable baseline。
- same-turn append-only 和 fresh-turn continuity 是两套不同问题。
- tool schema 稳定性和消息前缀稳定性要一起验证。
- artifact 不全时，先修 artifact，再信任何 cache 结论。
