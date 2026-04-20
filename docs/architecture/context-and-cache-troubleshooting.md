# G3KU 上下文与缓存排查指南

本文档用于指导维护者排查以下两类高频问题，并为后续修改节点上下文策略提供约束与检查清单：

- prompt cache 命中异常下降
- 上下文在非预期时机缩短、重排或丢失

它不是某一次修复的变更记录，而是把目前已经踩过的坑、有效的排查方法、以及仍需重点验证的边界整理成可复用的维护手册。

## 1. 先分清你看到的到底是什么问题

### 1.1 缓存命中下降，不等于输入 token 下降

排查时先把计费行拆成三部分看：

- `Input Tokens`
- `Cache Read Tokens`
- 二者之和代表本次请求的总可计费输入规模

常见误判：

- `Input Tokens` 变低了，就以为上下文变短了
- 其实也可能只是 cache hit 变差，更多 token 从 cache read 变成了重新计费

正确做法：

- 先比较 `Cache Read Tokens` 是否显著下降
- 再比较 `Input Tokens + Cache Read Tokens` 是否真的缩短
- 最后再决定这是“总上下文缩短”还是“总上下文近似不变但前缀复用消失”

### 1.2 caller-side family churn，不等于 provider request 前缀断裂

要把下面几类 hash 分开看：

- `prompt_cache_key_hash`
  表示 caller-side prompt cache family
- `stable_prefix_hash`
  表示稳定前缀是否变化
- `dynamic_appendix_hash`
  表示动态尾部是否变化
- `actual_request_hash`
  表示真实 request 是否变化
- `actual_tool_schema_hash` / `tool_signature_hash`
  表示 provider-facing tool schema 是否变化

排查结论要分类：

- `prompt_cache_key_hash` 变了：优先怀疑 stable prefix 或 tool schemas 变了
- `prompt_cache_key_hash` 没变，但 cache 命中掉了：优先怀疑 actual request 前缀断裂
- `stable_prefix_hash` 没变，但 `actual_request_hash` 变了：通常是 dynamic appendix、request scaffold 或同 turn append-only 逻辑出问题

## 2. 先看哪份数据，后看哪份数据

### 2.1 最权威的是 per-request JSON，不是 transcript

CEO/frontdoor 的 provider-facing request 以 `.g3ku/web-ceo-requests/<session>/...json` 为准。

优先看：

- `request_messages`
- `tool_schemas`
- `provider_request_body`
- `usage`
- `frontdoor_token_preflight_diagnostics`
- `frontdoor_history_shrink_reason`
- `prompt_cache_key_hash`
- `actual_request_hash`
- `stable_prefix_hash`
- `dynamic_appendix_hash`

原因：

- transcript 只说明用户和 UI 看到了什么
- session snapshot 说明 runtime 当时记住了什么
- 但真正发给 provider 的顺序、schema 和 transport payload，只有 actual request JSON 才是权威

### 2.2 `provider_request_body.input` 比 `request_messages` 更接近最终 transport truth

如果 provider adapter 提供了 `provider_request_body`，排 cache miss 时优先用它校验。

特别是 OpenAI `/responses` 路径，要看：

- `provider_request_body.input`
- `provider_request_body.tools`
- `provider_request_body.parallel_tool_calls`
- 濡傛灉 `frontdoor_token_preflight_diagnostics.final_request_tokens` 宸茬粡鎺ヨ繎 `effective_trigger_tokens`锛屼絾 usage 杩樻槸鏄庢樉鏇撮珮锛屼紭鍏堟寜鈥滀及绠楀亸灏忊€濇帓鏌ワ紝涓嶈鍏堟€€鐤?cache family churn

常见情况：

- `request_messages` 看起来公共前缀很长
- 但 `provider_request_body.input` 的第一个分叉点更早
- 这说明高层 projection 没问题，真正的问题出在 adapter 最终 payload

### 2.3 计费行与 actual request JSON 可能并不总是一一对应

目前已经遇到过一个高风险现象：

- usage/billing 里出现了 `/responses` 调用
- 但对应时间窗内没有落地 `frontdoor-request-*.json`

这会导致：

- 你拿本地 request artifact 去解释一条计费记录时，可能根本对不上号
- 尤其在 manual pause、pause 后新 turn、internal/hidden round 这些窗口，必须先确认 artifact 是否完整

所以：

- 如果 cache 数据和本地 request artifact 强烈矛盾，先不要急着改上下文策略
- 先排查 actual request artifact 是否漏落盘

## 3. 目前已经确认踩过的坑

## 3.1 同 turn 的 append-only 规则被破坏

正确语义：

- 同一 visible turn 内，request 应该只增长，不该重排
- 新增内容应当追加到尾部
- 旧前缀应尽量保持不变

已踩坑：

- 旧 contract snapshot 被提前剥掉
- 新 round 直接从 stripped body 继续，导致 provider 前缀在第 3 条就断
- overlay 被错误地拼回已有消息，而不是作为新的 request-tail 消息追加

维护要点：

- 同 turn 的 `request_messages` 必须尽量保持 append-only
- 旧 `frontdoor_runtime_tool_contract` 可以继续保留在 actual request 中作为 prefix scaffold
- durable history 才负责把它们剥回去

## 3.2 assistant 空文本 + tool_calls 被当成“空消息”丢掉

这类消息虽然 `content=""`，但它们仍是实际 provider request 的结构化一部分。

已踩坑：

- request-body seed 重建时按“空 content”过滤
- 导致 tool-call 结构记录消失
- 下一轮虽然文本看似还在，但 request shape 已经变化，缓存前缀断裂

维护要点：

- 带 `tool_calls` 的 assistant 记录必须保留
- 带 `tool_call_id` / `name` 的 tool 记录必须保留
- 不能用“文本为空”来判定它们是否可删

## 3.3 prepare-only planned request 抢走了真实 baseline

正确语义：

- `prepare_turn` 可以算出一个 planned request
- 但在真实 provider request 发出去之前，它不是 cross-turn baseline 的新权威

已踩坑：

- manual pause/no-provider turn 只走到了 prepare 或 paused snapshot
- 却把 session-owned baseline 覆盖成了 planned body
- 下一轮 fresh turn 直接从这个“未发出过的 baseline”续，cache 前缀大面积消失

维护要点：

- 只有带真实 actual-request 证据的状态，才允许覆盖 cross-turn baseline
- planned prompt-cache diagnostics 不能当成 actual-request 证据

## 3.4 finalize 没把 direct reply 补回 baseline

正确语义：

- 一旦当前 turn 已经有真实 provider request 落盘
- `finalize` 阶段追加的 direct assistant reply 应继续补回 session-owned baseline

已踩坑：

- finalize payload 没重复带 `frontdoor_actual_request_path/history`
- 同步逻辑误判为“没有 actual-request 证据”
- 结果 final assistant reply 没进下一轮 baseline

表现为：

- transcript 里能看到上一轮最终回答
- 但下一轮第一跳 request body 里完全没有它

维护要点：

- “同 turn 已有 actual-request 证据” 与 “fresh turn 没有 actual-request 证据” 是两种不同状态
- 不能用同一条覆盖规则处理

## 3.5 普通 fresh-turn 第一跳没有沿用上一轮 actual request scaffold

正确语义：

- durable baseline 仍可保持 stripped/finalized 形态
- 但普通 fresh-turn 的第一条 provider request，为了保前缀，可以借上一轮 persisted actual request scaffold

已踩坑：

- fresh turn 直接从 stripped body + 新 user + 新 contract 开始
- 第一条 provider request 在索引 3 就和上一轮分叉
- fresh-turn seed 若按原始 message dict 逐字节比对 `stable_messages[:body_len] == previous_request_body`，会被微小格式漂移击穿
- 真实案例里仅因某条 `tool` 输出末尾空格被后续规范化裁掉，seed 就误判为“不再是同一条 baseline”，退回未复用路径

维护要点：

- fresh-turn 第一跳可以临时使用 previous actual request scaffold
- 但这个 scaffold 只是 request-construction aid
- 它不能反过来变成新的 durable source of truth
- seed 判定应基于 provider-facing 结构等价，最多只容忍这类无语义影响的行尾空白 / 换行归一化差异；不要再做脆弱的原始 dict 全等比较

## 3.6 跨普通 fresh turn 的 tool schema churn

这类问题会直接造成 `prompt_cache_key_hash` 变化，因为 family key 会把 stable prefix 和 tool schemas 一起算进去。

已踩坑：

- 相邻两轮 stable prefix 没变
- 但 provider-visible `tool_schemas` 从一轮到下一轮发生了不必要变化
- 常见表现是某个工具在后一轮出现或消失，例如 `message`
- 结果 family key 跟着 churn，哪怕 request body 还能复用一部分前缀，caller-side family 也已经断了

维护要点：

- 对普通 fresh-turn 第一跳，如果当前 visible tool set 只是上一轮的超集，可以临时沿用上一轮 tool schema 集
- 这是“第一跳 cache-stability 规则”，不是永久替代当前 RBAC 可见集
- 如果当前 visible tool set 真变小了，不能继续重放旧 schema

## 3.7 manual pause 之后的新 turn，与“暂停回复”不是一回事

已踩坑：

- 把“pause 后用户发的新 turn”误判成“暂停回复”
- 然后错误地拿 pause ack 或 paused snapshot 去解释 cache miss

正确分类：

- `已暂停` 是上一轮的 pause ack
- pause 之后用户再发消息，是全新的 visible turn
- 它应该按 fresh-turn continuity 规则排查，而不是当作 pause ack 排查
- 对普通用户 manual pause，后端现在会把当前轮 terminalize 成 `completed`；若还在按长期 paused/resume 语义排查，结论通常会跑偏

## 3.8 重启后继续 completed session，不能退回 transcript/history fallback

已踩坑：

- 项目重启后 reopened completed session 直接从 transcript/history 重建
- `frontdoor_request_body_messages`、actual-request scaffold、stage/compression/semantic 状态没有按最新 authoritative state 恢复
- 结果表现成“上下文像还在，但第一跳 cache 命中突然掉光”

正确分类：

- inflight 恢复优先看 inflight snapshot
- 技术性 paused / interrupt 恢复优先看 paused snapshot
- reopened completed session 优先看 `.g3ku/web-ceo-continuity/<session>.json`
- 只有这些都没有时，才退回 transcript/history fallback

- 如果 continuity sidecar 里的 visible tool/skill 集与当前完全一致，第一跳应继续借上一轮的 family/schema anchor
- 如果 visible 集发生变化，上下文仍应恢复，但 cache miss 可以接受，不应误判成上下文丢失

## 3.9 request artifact 持久化缺口会污染结论

已踩坑：

- 同一时间窗里 usage 记录显示发生了 provider 调用
- 但 request artifact 目录里没有对应文件

这会带来很大的分析偏差：

- 你以为某个 persisted request 对应某条计费记录
- 实际那条计费记录可能来自另一条隐藏/漏落盘请求

维护要点：

- 每次怀疑缓存异常时，先检查 artifact 时间线是否完整
- 如果 artifact 不全，先修 artifact，再改上下文策略

## 3.10 heartbeat / cron 现在按普通 continuation shrink 规则排查

Heartbeat / cron no longer use a separate short `ceo_heartbeat` lane on the main CEO/frontdoor path. They now append hidden internal prompt messages onto the same session-owned `frontdoor_request_body_messages` / actual-request scaffold used by ordinary turns.

This changes the troubleshooting rule:

- Do not explain heartbeat shrink/caching differences as "it is a different prompt lane now." That older explanation is obsolete on the main CEO path.
- If heartbeat/cron request shape becomes shorter, it must still be explained by the same allowed shrink reasons as ordinary turns, mainly `token_compression` or `stage_compaction`.
- The hidden heartbeat/cron rule and event-bundle messages are durable prompt history. They should appear in actual-request artifacts, completed continuity sidecars, and prompt assembly even though UI transcript surfaces hide them with `ui_visible=false`.
- When debugging cache misses, compare the previous actual request with the new heartbeat/cron request as an append-only continuation. A large prefix break now usually means baseline restoration, tool-schema seeding, or compression changed, not that runtime intentionally switched to a separate heartbeat lane.

## 4. 排查流程

## 4.1 先画时间线

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

## 4.2 再分类这次断的是哪一层

先看：

- `prompt_cache_key_hash`
- `stable_prefix_hash`
- `dynamic_appendix_hash`
- `actual_tool_schema_hash`
- `actual_request_hash`

判定规则：

- `prompt_cache_key_hash` 变了，优先排 family churn
- family 不变但 `actual_request_hash` 变了，优先排 request shape
- family 与 shape 都没明显异常，但 usage 仍显示 0 cache，优先排 artifact 对应关系

## 4.3 再比公共前缀

至少比两层：

- `request_messages`
- `provider_request_body.input`

如果二者结论不一致：

- 以 `provider_request_body.input` 为更高优先级

要重点记：

- 公共前缀长度
- 第一个分叉索引
- 分叉点前后分别是什么类型的消息

最常见的分叉模式：

- 旧请求是 `contract`
- 新请求已经变成 `assistant tool_calls`

这基本就说明你不是在上一条真实 provider request 上继续，而是在 stripped/scaffold-less body 上继续。

## 4.4 最后判断是不是合法 shrink

目前已确认的允许 shrink 原因只有：

- `token_compression`
- `stage_compaction`

如果下一轮 baseline 变短，却没有这两个原因之一：

- 按 runtime bug 处理
- 不要把它解释成“正常上下文整理”

## 4.5 token preflight 之后要看哪份诊断

当前 CEO/frontdoor 在真正发 provider 请求前还有最后一层 token preflight。

排查顺序应是：

- 先看 actual request artifact，确认最终 `request_messages` / `provider_request_body`
- 再看 `frontdoor_token_preflight_diagnostics`
- 最后再回头看 builder 里的 `pre_summary_prompt_tokens`

原因：

- builder 里的 token 估算仍然只说明“组装阶段看到的上下文压力”
- 真正决定本轮是否在发送前压缩的，是 provider-send 前的最终 preflight

如果 `frontdoor_token_preflight_diagnostics.applied=true`：

- 预期 `frontdoor_history_shrink_reason` 为 `token_compression`
- 如果 diagnostics 显示已触发 preflight，但 shrink reason 不是 `token_compression`，优先按 runtime bug 排查
- 先区分当前看的到底是压缩前还是压缩后的值：top-level `final_request_tokens` / `estimated_total_tokens` 现在代表压缩后真正要发出去的 request；若需要解释为什么会触发压缩，再看 `pre_compaction_*`

如果 diagnostics 显示未触发 preflight，但请求仍明显缩短：

- 优先回到 `stage_compaction` / continuity baseline handoff 链路
- 不要把所有缩短都归因到 token preflight

额外要看三类 ground-truth 字段：

- `effective_input_tokens`
- `delta_estimate_tokens`
- `comparable_to_previous_request`

排查经验：

- `estimate_source=usage_plus_delta` 说明 runtime 已经确认 continuity 足够稳定，并且 `final_request_tokens` 可能明显高于 preview-only 估算
- `estimate_source=preview_estimate` 不一定表示 usage 不存在，也可能只是 continuity 不可证明；这时优先看 `comparable_to_previous_request=false` 的原因，而不是先怀疑阈值
- 如果 top-level已是压缩后值，而你想知道“为什么这轮会先压缩”，应看 `pre_compaction_estimate_source` / `pre_compaction_final_estimate_tokens` / `pre_compaction_effective_input_tokens`

另外要额外排一类很隐蔽的误判：

- preflight 的 token estimator 必须基于原始 `provider_request_body` 估算
- 不能复用面向摘要/展示的 serializer，再把超长字段截成固定前后两段后估算

典型症状是：

- `provider_request_body` 明明已经很大
- 但 `frontdoor_token_preflight_diagnostics.final_request_tokens` 却长期卡在一个异常偏小、几乎不随请求增长变化的常数

如果看到这种现象，先按“estimator 低估导致压缩阈值永远打不到”排查，不要先怀疑 `trigger_tokens` 配置失效。

## 4.6 节点 / distribution token preflight 排查要点

当前 execution / acceptance 节点在真正发 provider 请求前也会走最后一层 node send token preflight，同样包括 `message_distribution` 控制轮。

但有两个边界必须分清：

- `message_distribution` 包含在 node send preflight 里
- `spawn review` 是外部检验通道，故意不在 node preflight 合同里，不要把它的 prompt / request 行为和节点正常运行路径混为一谈

排查时先看节点 runtime frame 或 actual-request artifact 里的：

- `token_preflight_diagnostics`
- `history_shrink_reason`
- `prompt_cache_key_hash`
- `actual_request_hash`
- `actual_request_message_count`

如果 `token_preflight_diagnostics.applied=true`：

- 预期 `history_shrink_reason` 为 `token_compression`
- 如果 actual request 看起来明显变短，但 `prompt_cache_key_hash` 没变，这是正常的“live request 被压缩但 caller-side family 未换”行为，不要误判成 family churn
- 节点启动后的 restart / resume 第一跳新请求可以复用“已经过 token compression 的 actual request scaffold”；这也不是 context loss，而是合法的延续路径
- 节点 diagnostics 的 top-level字段现在同样在 compaction 后切到“最终真正要发的 request”，而压缩前 hybrid 判断保留在 `pre_compaction_*`

如果 preflight 在节点端还没有发送模型前就失败：

- 先看是否是 `context_window_tokens <= 25000` 这种硬错误配置
- 再看是否是 preview builder / provider payload 估算出错
- 这类“没有模型请求”的卡顿，优先查 preflight 合同和配置解析，而不是先怀疑 tool loop 或 queue scheduler

如果你在节点侧看到“上一轮 provider usage 明明很大，但这轮还是没触发压缩”，按下面顺序排：

- 先看 `effective_input_tokens` 是否真的落盘到了最新 `observed_input_truth`
- 再看 `actual_request_hash` 是否与上一轮 actual-request artifact 对得上
- 再看 `comparable_to_previous_request` 是否因为 append-only 或 tool schema 校验失败而退回了 preview-only

只有这三项都成立、而 `final_estimate_tokens` 仍明显偏小，才该怀疑 hybrid estimator 本身。

## 5. 修改节点上下文策略时必须重点验证的地方

下面这些检查点不只适用于 CEO/frontdoor，后续改 node context strategy 时也必须逐条过。

## 5.1 stable prefix 与 live scaffold 必须分层

要分别定义：

- stable prefix 的 durable source
- fresh-turn 或 fresh-round 第一跳的 request scaffold
- dynamic appendix 的尾部合同

不要把三者混成一个列表再寄希望于后处理自动修好。

## 5.2 durable baseline 与 request-construction scaffold 不能互相替代

节点如果也要借上一轮 actual request 保前缀，必须明确：

- 哪些内容只是“第一跳 request scaffold”
- 哪些内容才是 durable baseline

否则会复现 CEO/frontdoor 已经踩过的坑：

- planned scaffold 抢 durable baseline
- durable baseline 过早丢 contract
- 下一轮从错误形态继续

## 5.3 tool schema 稳定性要单独验证

节点改上下文策略时，不要只看 messages。

必须同时检查：

- provider-facing tool schema 列表
- schema 顺序
- 是否有无意义的增删
- 这些变化是否影响 `prompt_cache_key_hash`

Maintenance note:

- Wrapped OpenAI-style tool definitions (`{"type":"function","function":{...}}`) and flat persisted function records (`{"type":"function","name":...,"parameters":...}`) now count as the same logical tool contract.
- Provider adapters and `actual_tool_schema_hash` / `tool_signature_hash` diagnostics should both use the same shared normalization path before transport or hashing.
- If a future provider needs a provider-specific tool wire format, normalize first and only then project into the final transport shape. Do not re-implement flat-vs-wrapped compatibility separately inside each provider.

如果 stable prefix 没变，但 family key 变了，通常就是 schema churn。

## 5.4 overlay 只能追加，不能回写旧消息

节点的：

- turn overlay
- repair overlay
- 临时诊断块

都只能当作新的 request-tail 消息追加。

不能：

- 拼回已有 user 消息
- 改写 stable history
- 改写 baseline 里的旧 assistant/tool 记录

## 5.5 same-turn append-only 与 fresh-turn continuity 要分开测

至少要有两套测试：

- 同一 turn 内 round 1 -> round 2 -> round 3
- 上一轮结束 -> 下一轮 fresh turn 第一跳

因为这两类 bug 的根因完全不同：

- same-turn bug 更像 request growth/append-only 问题
- fresh-turn bug 更像 baseline handoff / family churn / scaffold 选择问题

## 5.6 pause/new turn 与 ordinary fresh turn 也要分开测

节点如果以后支持 pause/resume 或类似边界，也要把下面三种情况拆开：

- 普通 fresh turn
- manual pause/no-provider turn
- same-turn finalize after actual request

不要用一条统一的“有没有 actual-request evidence”规则粗暴处理所有场景。

## 5.7 artifact 完整性必须纳入测试与排查

对于节点上下文策略，建议新增或保留以下检查：

- 每一次实际 provider 调用都应落 artifact
- artifact 中应包含：
  - request projection
  - provider transport payload
  - tool schemas
  - prompt-cache diagnostics

否则后续排 cache miss 时很难判断到底是哪一跳出了问题。

## 6. 建议的测试矩阵

至少覆盖下面这些场景：

1. 同一 turn 内多轮 tool 调用，前缀应持续增长。
2. direct reply 收尾后，新 turn 第一跳应保住上一轮 provider request 前缀。
3. finalize 补回 final assistant reply 后，下一轮 baseline 不能丢这条回复。
4. prepare-only/no-provider 状态不能覆盖 durable baseline。
5. manual pause 后新 turn 不能退回 transcript-only reconstruction。
6. restarted completed session 第一跳不能退回 transcript/history fallback。
7. stable prefix 不变时，tool schema 也应尽量稳定；必要时验证 family key 不抖。
8. `provider_request_body.input` 与高层 `request_messages` 的前缀对比结论不能长期分叉。
9. usage 记录与 request artifact 必须能在时间线层面对得上。
10. 非 `token_compression` / `stage_compaction` 的 shrink 一律视为失败。

## 7. 当前仍需继续盯的风险点

截至目前，下面这些地方仍值得持续关注：

- pause 窗口里的 actual request artifact 是否会漏落盘
- completed continuity sidecar 是否始终跟上最新 authoritative state，同步点是否会漏写
- 某些相邻 turn 的 provider-visible tool schema 是否仍会无意义抖动
- restarted completed session 第一跳的 visible-set equality bridge 是否还会被意外放宽成 superset
- `provider_request_body.input` 与 `request_messages` 是否还存在隐藏分叉
- 节点侧是否也存在“stripped durable baseline”和“first-hop scaffold”混淆
- 节点侧是否也有 finalize/direct reply 没补回 baseline 的问题

## 8. 给节点上下文策略修改者的简版原则

如果只记住几条，请记这几条：

- 先区分 family churn 还是 request-shape 断裂，再决定改哪里。
- 先看 actual request artifact，再看 transcript。
- stable prefix、durable baseline、first-hop scaffold、dynamic appendix 必须分层。
- 只有真实 provider request 才能推进 durable baseline。
- same-turn append-only 和 fresh-turn continuity 是两套不同问题。
- tool schema 稳定性和消息前缀稳定性要一起验证。
- artifact 不全时，先修 artifact，再信任何 cache 结论。
## Node Cache Stability Addendum

This addendum records the April 2026 node-cache repair boundary.

- Node runtime now distinguishes `tool_names` from `provider_tool_names`.
- `tool_names` remain the authoritative callable contract for the current round.
- `provider_tool_names` are only the provider-facing schema bundle used to stabilize cache prefixes.
- Same-turn node requests now prefer an append-only scaffold:
  previous actual request body + last-round assistant/tool delta + newest overlay / tool-contract tail.
- After node restart/resume, the first rebuilt provider request must keep using that persisted append-only scaffold even if durable `runtime_frame.messages` is shorter. `runtime_frame.messages` is the projected-history lens; `actual_request_ref.request_messages` is the authority for first-hop request reconstruction.
- This scaffold must not weaken stage gating or hydration rules.

When node cache hits are still low after schema churn stops, compare consecutive node actual-request artifacts first.

- If `actual_tool_schema_hash` is stable but cache hits stay low, inspect whether `provider_request_body.input` stopped being append-only.
- If early `function_call` / `function_call_output` records are being replaced instead of appended, treat it as a node request-scaffold regression rather than a pure tool-schema issue.
- If `prompt_cache_key_hash` stays the same but `actual_request_message_count` drops sharply on the first node request after restart/resume, compare `runtime_frame.messages` against the latest node `actual_request_ref.request_messages` first. That pattern usually means the resumed first hop rebuilt from projected history instead of from the persisted actual-request scaffold.

Steady-state validation target for this repair:

- warm-up rounds may miss after a legitimate schema change;
- after schema stabilizes, the later consecutive node rounds should recover to roughly 90% cache-hit ratio or better.

## Current Compression Rules

The current CEO/frontdoor shrink rules are simpler than the older notes above:

- Only `token_compression` and `stage_compaction` are valid shrink reasons.
- `token_compression` is an inline LLM rewrite that runs only when the estimated provider-bound request is above `80%` of the selected model's `context_window_tokens` and still within that window.
- If the estimate is already above the selected model window, frontdoor must fail before send instead of attempting any semantic/global-summary fallback.
- If inline compression runs and the recomputed request is still above the selected model window, frontdoor fails with the same context-window error.
- Manual pause during compression discards any late compression result; the next visible turn re-runs prepare -> estimate -> optional compression -> send from the current authoritative baseline.

### Obsolete Notes To Ignore

Older references in historical troubleshooting notes to `semantic_context_state`, `global summary`, or `frontdoor_global_summary_*` should now be treated as obsolete. The current debugging authority is the actual provider request JSON plus the runtime-selected model's `context_window_tokens`.
## Runtime Contract Summary Checks

- `frontdoor_runtime_tool_contract` and `node_runtime_tool_contract` now normally appear as assistant summary blocks headed `## Runtime Tool Contract`, not as raw JSON message bodies.
- Treat those summary blocks as dynamic contract tail records during cache analysis. They are still outside the stable prefix even though the body is no longer JSON.
- Same-turn live requests may still contain multiple contract summaries because the request path stays append-only for cache preservation. The newest summary is the authoritative contract for that round.
- Durable continuity baselines should still strip runtime-contract summaries before persistence. If a later turn replays an old summary as ordinary stable history without `token_compression` or `stage_compaction`, treat that as illegal context carryover.
