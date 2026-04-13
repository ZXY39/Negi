# CEO Frontdoor 总压缩与二段式阶段压缩设计

## 1. 背景

当前主 agent（CEO frontdoor）已经具备两类与上下文缩减相关的能力：

1. 对话历史层面的规则式压缩  
   由 `g3ku/runtime/frontdoor/history_compaction.py` 提供，超过阈值后把旧消息折叠成：
   - `COMPACT BOUNDARY: ...`
   - `Conversation summary: ...`

2. 阶段状态层面的归档与压缩  
   由 frontdoor stage state 和 stage archive 提供，completed normal stages 超过 20 个时，按 10 个一批归档，并插入 `stage_kind = compression` 的占位阶段。

与此同时，节点执行侧还具备一套更适合模型推理的 prompt 级阶段上下文压缩机制：

- 最近 3 个 completed stage 保留原始窗口
- 更早 completed stages 改写为节点格式的 compact / externalized blocks
- 当前 active stage 保留原始窗口

当前 CEO frontdoor 缺少这套“近场阶段工作集”机制，也缺少一层真正面向长上下文的语义压缩。现有规则式历史压缩虽然能缩短 prompt，但保留的信息密度和结构稳定性不足，容易在长对话、多阶段、多任务场景中丢失关键上下文。

本设计的目标是：

- 为 CEO frontdoor 引入**总压缩 + 二段式阶段压缩**的双层上下文模型
- 将节点侧的“最近 3 个阶段保留 + 更早阶段压缩”机制完整复用到主 agent
- 将“更老的全局上下文”改造成 Hermes 风格的 `lossy summarization`
- 删除旧的规则式历史压缩机制
- 明确总压缩的触发时机，并兼容 heartbeat、cron 等临时内部消息

## 2. 目标

本设计达成后，CEO frontdoor 的上下文组织应满足以下目标：

- 主 agent 在长会话中仍能保留高质量近场工作集，不丢失当前任务阶段的细粒度信息。
- 局部工作集之外的更老上下文被压缩成高质量语义摘要，而不是字符串拼接式摘要。
- 心跳、cron 等临时内部消息不污染长期语义摘要。
- 总压缩与 3 段/10 段阶段压缩互不干扰，职责边界明确。
- 删除旧规则式历史压缩后，prompt 组装语义更稳定，缓存抖动更可控。

## 3. 非目标

本设计不做以下事情：

- 不改变 frontdoor stage archive 的 `completed_stage_summary` 生成方式。
- 不改变 completed normal stages 超过 20 时按 10 个一批归档的机制。
- 不将语义摘要回写成 transcript 的 canonical 历史替代品。
- 不把心跳、cron 的原始内部消息当成长期用户可见历史来总结。
- 不在第一版中实现异步后台 summary 刷新。

## 4. 设计总览

新的 CEO frontdoor prompt 由 5 层上下文组成：

1. `system prompt`
2. `global semantic summary block`
3. `local workset`
4. `current user message`
5. `turn overlay / retrieved context / capability blocks`

其中：

- `global semantic summary block`  
  覆盖局部工作集之外的更老全局上下文，采用 LLM 进行 `lossy summarization`，作为 handoff/reference 注入 prompt。

- `local workset`  
  由 frontdoor 阶段状态生成，完全复用节点侧 prompt 级阶段压缩语义：
  - 最近 3 个 completed stages 保留原始窗口
  - 更早 completed stages 继续转换为节点格式的 stage compact / externalized blocks
  - 当前 active stage 保留原始窗口

这个模型是“外层总压缩 + 内层局部工作集”的双层设计：

- 外层负责长距离背景
- 内层负责当前轮执行相关的高分辨率上下文

## 5. 二段式阶段压缩

### 5.1 保留语义

CEO frontdoor 将完全复用节点侧的 prompt 级阶段工作集语义：

- 最近 3 个 completed stages 保留原始消息窗口
- 更早 completed stages 转换为节点格式 block
- 当前 active stage 保留原始消息窗口

复用的目标不是“近似一致”，而是“格式和裁剪边界尽量完全一致”，从而统一维护心智、测试方式和调试语义。

### 5.2 与 10 段归档的关系

这里的“3 段保留”是 prompt 视图层逻辑；“10 段归档”是状态存储层逻辑。两者保留并叠加：

- completed normal stages 超过 20 时，stage state 仍然按 10 个一批归档。
- prompt 组装时，最近 3 个 completed stages 仍保持原始窗口。
- 更老 completed stages 若已经成为 compression stage，则以 externalized block 的形式进入 prompt。

因此：

- 10 段归档继续控制 state 规模
- 3 段保留继续控制近场推理质量

## 6. 总压缩设计

### 6.1 总压缩覆盖范围

总压缩只覆盖“局部工作集之外的更老全局上下文”，包括：

- 更老 transcript / checkpoint 历史
- 已归档阶段经 `archive_ref` 可追溯的更老内容
- 不再属于最近 3 个 completed stages 或当前 active stage 的历史工作轨迹

总压缩**不覆盖**：

- 当前 active stage 的原始窗口
- 最近 3 个 completed stages 的原始窗口
- 当前用户输入
- turn overlay / retrieved context / capability appendices

### 6.2 输出形式

总压缩结果以一个稳定的 assistant message block 注入 prompt，建议使用稳定前缀，例如：

- `[G3KU_LONG_CONTEXT_SUMMARY_V1]`

该 block 必须显式声明自己是：

- 较早上下文的 handoff / background reference
- 不是当前轮的 active instructions
- 仅用于帮助后续模型理解更老历史

### 6.3 摘要模板

总压缩采用结构化摘要模板，建议包含以下部分：

- `长期目标`
- `已确认约束与偏好`
- `已完成里程碑`
- `未关闭事项`
- `已解决问题`
- `待回答的用户诉求`
- `关键任务 / 引用 / refs`
- `关键决策`
- `重要运行时发现`

模板必须偏“handoff context”，而不是“下一步执行命令”。

## 7. 触发时机

总压缩的触发从旧的 message-count 机制迁移为 token-pressure + coverage-based 机制。

### 7.1 Hermes 对齐的默认阈值

总压缩默认值采用与 Hermes `ContextCompressor` 相同或同语义对齐的基线参数：

- `frontdoor_global_summary_trigger_ratio = 0.50`  
  参考 Hermes `threshold_percent = 0.50`。当组装后的完整 prompt 估算 token 达到模型上下文窗口的 50% 时，进入总压缩工作区判定。
- `frontdoor_global_summary_target_ratio = 0.20`  
  参考 Hermes `summary_target_ratio = 0.20` 和 `_SUMMARY_RATIO = 0.20`。总压缩摘要目标长度约为“被压缩上下文 token 量”的 20%。
- `frontdoor_global_summary_min_output_tokens = 2000`  
  参考 Hermes `_MIN_SUMMARY_TOKENS = 2000`。即使被压缩区不大，也不将语义摘要预算压得过短。
- `frontdoor_global_summary_max_output_ratio = 0.05`  
  参考 Hermes `self.max_summary_tokens = min(int(context_length * 0.05), 12000)`。摘要上限默认为主模型上下文窗口的 5%。
- `frontdoor_global_summary_max_output_tokens_ceiling = 12000`  
  参考 Hermes `_SUMMARY_TOKENS_CEILING = 12000`。即使主模型上下文窗口很大，也不让单个摘要无限膨胀。
- `frontdoor_global_summary_pressure_warn_ratio = 0.85`  
  参考 Hermes 的 context pressure tier，85% 视为高压区。到达该比例时，如果 global summary 缺失、过旧或 `needs_refresh = true`，应同步刷新。
- `frontdoor_global_summary_force_refresh_ratio = 0.95`  
  参考 Hermes 的第二压力 tier，95% 视为强制刷新区。到达该比例时，即使是 heartbeat / cron turn，也必须优先保证 global summary 可用。
- `frontdoor_global_summary_min_delta_tokens = 2000`  
  默认与 Hermes 的最小摘要预算对齐。只有新进入全局压缩区的增量达到 2000 tokens，才值得触发一次新的语义摘要刷新。
- `frontdoor_global_summary_failure_cooldown_seconds = 600`  
  参考 Hermes `_SUMMARY_FAILURE_COOLDOWN_SECONDS = 600`，摘要失败后进入 10 分钟冷却，期间复用旧摘要或回退。

在实现中，以上 ratio 默认值必须派生为明确 token 阈值，推荐直接采用以下公式（与 Hermes 数值口径对齐）：

- 设 `C = 主模型上下文窗口 tokens`
- 设 `HERMES_MIN_CONTEXT_FLOOR = 64000`（参考 Hermes `MINIMUM_CONTEXT_LENGTH = 64_000`）
- `global_summary_trigger_tokens = max(int(C * 0.50), 64000)`  
  说明：与 Hermes `threshold_percent=0.50` 和 64K floor 对齐。
- `global_summary_pressure_warn_tokens = int(C * 0.85)`  
  说明：高压区阈值，对齐 Hermes 85% pressure tier。
- `global_summary_force_refresh_tokens = int(C * 0.95)`  
  说明：强制刷新阈值，对齐 Hermes 95% pressure tier。
- `global_summary_max_output_tokens = min(int(C * 0.05), 12000)`  
  说明：对齐 Hermes `min(context_length*0.05, 12000)`。
- `global_summary_target_tokens = clamp(int(compressed_zone_tokens * 0.20), 2000, global_summary_max_output_tokens)`  
  说明：对齐 Hermes `_SUMMARY_RATIO=0.20`、`_MIN_SUMMARY_TOKENS=2000` 与输出上限规则。

为避免实现时出现二义性，本设计要求触发判定优先使用上述 token 阈值变量；ratio 仅作为配置输入与可观测展示。

### 7.2 同步触发

在 frontdoor 组装一次真实模型调用的 prompt 之前，满足任一条件时，同步刷新 global summary：

1. 当前不存在可用的 `global semantic summary`
2. 现有 summary 的覆盖边界落后于当前可压缩全局区，且新增进入压缩区的内容达到 `frontdoor_global_summary_min_delta_tokens`
3. 组装后的完整 prompt 估算 token 达到 `global_summary_pressure_warn_tokens`
4. 组装后的完整 prompt 估算 token 达到 `global_summary_force_refresh_tokens`
5. 上一次 summary 生成失败，cooldown 已结束，且当前 prompt 估算 token 已达到 `global_summary_trigger_tokens`

### 7.3 延后刷新

若仅存在“有新历史进入全局压缩区”，但同时满足以下条件，则不立即刷新：

- 新增进入压缩区的内容小于 `frontdoor_global_summary_min_delta_tokens`
- 完整 prompt 估算 token 低于 `global_summary_pressure_warn_tokens`

此时只记录：

- `needs_refresh = true`

等下一次真正进入高压区或增量足够大时再刷新。

### 7.4 推荐配置语义

新增 token / ratio-based 配置，不再使用旧的 message-count 压缩阈值：

- `frontdoor_global_summary_trigger_ratio`
  - 总压缩进入工作区的基线比例，默认 `0.50`
- `frontdoor_global_summary_target_ratio`
  - 摘要目标长度相对被压缩区的比例，默认 `0.20`
- `frontdoor_global_summary_min_output_tokens`
  - 摘要最小输出预算，默认 `2000`
- `frontdoor_global_summary_max_output_ratio`
  - 摘要最大输出预算占主模型上下文窗口的比例，默认 `0.05`
- `frontdoor_global_summary_max_output_tokens_ceiling`
  - 摘要输出预算硬上限，默认 `12000`
- `frontdoor_global_summary_pressure_warn_ratio`
  - 高压区比例，默认 `0.85`
- `frontdoor_global_summary_force_refresh_ratio`
  - 强制刷新区比例，默认 `0.95`
- `frontdoor_global_summary_min_delta_tokens`
  - 刷新摘要所需的最小新增压缩区 token，默认 `2000`
- `frontdoor_global_summary_model`
  - 可选，允许单独指定压缩摘要模型
- `frontdoor_global_summary_failure_cooldown_seconds`
  - 摘要失败后的冷却期，默认 `600`

实现要求：运行时必须把上述 ratio 配置实时换算为 token 阈值并用于判定（见 7.1 公式），至少在 trace/metrics 中暴露：

- `global_summary_trigger_tokens`
- `global_summary_pressure_warn_tokens`
- `global_summary_force_refresh_tokens`
- `global_summary_max_output_tokens`

## 8. 心跳、Cron 与临时内部消息兼容

### 8.1 三条通道必须显式分离

本设计必须区分以下三条通道，避免“前端可见”和“后续 prompt 可读”混淆：

1. `UI 展示通道`  
   前端继续通过 inflight/session snapshot 渲染 heartbeat / cron 的处理流程，包括开阶段、工具调用、执行轨迹和压缩状态。
2. `普通历史注入通道`  
   用于下一次真实用户 turn 的近场 prompt 历史。该通道仍可继续过滤 heartbeat / cron 的内部 user 消息与 `history_visible = false` 的 assistant 消息。
3. `总压缩输入通道`  
   用于生成 global semantic summary。该通道不能简单复用普通历史注入的过滤结果，必须额外保留 heartbeat / cron 期间 agent-side 产生的原始执行上下文。

### 8.2 总压缩输入规则

总压缩输入必须遵守以下规则：

- `heartbeat_internal` / `cron_internal` 对应的内部 user message，作为控制包络默认不进入总压缩摘要正文
- 但 heartbeat / cron 期间 agent 侧产生的**原始执行上下文**必须进入总压缩输入源，包括：
  - 原始 assistant 回复
  - 原始工具调用与工具结果
  - execution trace / stage rounds / tool events
  - 与该 turn 直接相关的 frontdoor stage state 变化
- `metadata.history_visible == false` 不再等价于“禁止进入总压缩输入”；它只禁止进入普通历史注入通道
- 非 `user|assistant|tool` 的非历史消息仍默认排除

换句话说：

- heartbeat / cron 的原始内部 user 消息可以不进入近场历史
- 但其 agent-side 原始处理流程必须进入 global summary 的素材池，避免后续用户对话中出现“前端看见做过，agent 后面忘了”的失忆现象

### 8.3 与后续对话的关系

heartbeat / cron 那一轮留下的 frontdoor stage / compression live snapshot，仍然不要求直接原样继承到下一次真实用户 turn。  
后续真实用户 turn 能“读到” heartbeat / cron 处理过程，依赖的是：

- 这些 internal turns 的 agent-side raw execution context 被纳入总压缩输入源
- 然后经 global semantic summary 注回 prompt

因此：

- UI 侧继续看 snapshot
- 后续真实用户 turn 侧通过 global summary 看到压缩后的高质量 handoff

### 8.4 触发兼容规则

heartbeat / cron 兼容原则如下：

- heartbeat / cron turn 也参与总 prompt 的 token 压力计算
- 若当前 prompt 估算 token 低于 `global_summary_pressure_warn_tokens`，且仅有小增量进入总压缩区，则只设置 `needs_refresh = true`
- 若当前 prompt 估算 token 达到 `global_summary_pressure_warn_tokens`，则 heartbeat / cron turn 允许触发同步摘要刷新
- 若当前 prompt 估算 token 达到 `global_summary_force_refresh_tokens`，则 heartbeat / cron turn 必须优先保证 global summary 可用
- 当 heartbeat / cron 的 agent-side raw execution context 已进入总压缩区时，后续真实用户 turn 必须能通过 global summary 间接获得这些处理过程的关键语义

## 9. 参考 Hermes 的可借鉴做法

参考项目：`D:\NewProjects\hermes-agent-main`

本设计明确借鉴 Hermes 的以下技术路线：

1. `lossy summarization` 作为总压缩主路线
2. handoff/reference 风格的摘要前缀
3. 结构化摘要模板，而不是自然语言散文
4. 迭代式摘要更新，而不是每轮从零总结
5. 内容量驱动的摘要预算
6. 摘要失败后的 cooldown
7. 触发阈值采用 Hermes 的默认数值基线：
   - 50% 进入总压缩工作区
   - 20% 压缩目标比例
   - 最小 2000 tokens 摘要预算
   - 最大 5% 上下文窗口且不超过 12000 tokens
   - 85% / 95% 压力分层

本设计明确**不直接照搬** Hermes 的以下实现细节：

1. 不直接重写 canonical message history
2. 不把摘要与 tail message 动态合并
3. 不依赖“压完再修 orphan tool pairs”作为主路径设计中心

G3KU 的做法是：

- canonical transcript 保持原始事实源
- 总压缩只生成 prompt-view summary block
- 局部工作集继续由阶段机制主导

## 10. 删除旧上下文压缩机制

本次改造将删除旧的 frontdoor 规则式历史压缩机制，删除范围包括：

- `g3ku/runtime/frontdoor/history_compaction.py` 的运行时调用路径
- `_summarize_messages(...)` 中基于 message count 的旧 compaction 行为
- `prompt_cache_contract.py` 对旧 compaction marker 的特殊识别逻辑

### 10.1 保留但重定义的状态

`compression_state` 的对外结构暂不直接删除，用于兼容前端状态提示和 inflight snapshot。  
但其语义重定义为“semantic summary refresh state”，推荐状态值：

- `idle`
- `running`
- `ready`
- `error`

并将：

- `source = semantic`
- `text` 用作当前压缩状态文案
- `needs_recheck` 表示是否待刷新

### 10.2 配置迁移策略

旧配置字段停止参与运行时逻辑，但第一版保留 schema 字段为 deprecated no-op：

- `frontdoor_recent_message_count`
- `frontdoor_summary_trigger_message_count`
- `frontdoor_summarizer_trigger_message_count`
- `frontdoor_summarizer_keep_message_count`

运行时应记录明确 warning，后续版本再彻底删除字段。

## 11. 具体改动计划

### 11.1 新增共享阶段工作集模块

新增共享 helper 模块，例如：

- `g3ku/runtime/stage_prompt_compaction.py`

负责：

- 识别和清理 stage context message
- 计算最近 3 个 completed stage 的保留集合
- 生成 compact / externalized blocks
- 计算 active stage window

`main/runtime/react_loop.py` 改为复用该 helper，确保节点行为不变。

### 11.2 Frontdoor 接入局部工作集

修改：

- `g3ku/runtime/frontdoor/message_builder.py`
- `g3ku/runtime/frontdoor/_ceo_runtime_ops.py`

流程调整为：

- 先选定 history source（checkpoint/transcript）
- 再基于 `frontdoor_stage_state` 生成 local workset
- 再将 local workset 注入 turn context

### 11.3 新增总压缩摘要模块

新增模块，例如：

- `g3ku/runtime/semantic_context_summary.py`

负责：

- 计算 global zone
- 序列化摘要输入
- 生成结构化 handoff summary
- 维护 previous summary 与 coverage 边界
- cooldown / fallback 处理

### 11.4 扩展 Frontdoor 状态

新增状态，例如：

- `semantic_context_state`

建议字段：

- `summary_text`
- `coverage_history_source`
- `coverage_message_index` 或等价边界字段
- `coverage_stage_index`
- `needs_refresh`
- `failure_cooldown_until`
- `updated_at`

### 11.5 删除旧规则式压缩路径

移除：

- `history_compaction.py` 的核心调用
- `_summarize_messages()` 的旧逻辑
- `prompt_cache_contract.py` 对旧 compaction marker 的特殊处理

改为：

- 使用固定的 `global semantic summary block`
- 在 cache contract 中识别新的 summary block 稳定前缀

## 12. 测试计划

至少新增或调整以下测试：

1. CEO frontdoor 的 local workset 与节点侧保持一致
2. 最近 3 个阶段保留窗口正确
3. 更早 completed stages 正确生成 compact / externalized blocks
4. global summary 只覆盖 local workset 之外的历史
5. heartbeat / cron 的内部 user 控制消息不进入近场历史，但其 agent-side 原始执行上下文必须进入总压缩输入源；`history_visible=false` 仅约束普通历史注入，不屏蔽总压缩输入
6. 当 prompt 接近上限时，总压缩会同步刷新
7. 当仅有轻微增量时，总压缩只打 `needs_refresh`
8. summary 失败后进入 cooldown，并能回退
9. 删除旧机制后，不再生成旧 `COMPACT BOUNDARY` / `Conversation summary` 形式
10. 前端 `compression_state` 仍能消费新的语义压缩状态

## 13. 风险与缓解

### 风险 1：总压缩摘要被模型误当成当前指令

缓解：

- 强制 handoff/reference 风格前缀
- 使用结构化“背景摘要”模板
- 将摘要固定插入在 stable prompt 的独立槽位，而不是混入当前用户消息

### 风险 2：局部工作集和总压缩区边界不清，导致重复或冲突

缓解：

- 先统一 stage prompt compaction helper
- 明确 global zone 永远排除 local workset
- 对“同一事实既在 global summary 又在近场窗口出现”的情况，规定近场优先

### 风险 3：heartbeat / cron 污染长期摘要

缓解：

- 普通历史注入继续走 `history_visible` / internal-source 过滤
- 总压缩输入单独建模：保留 heartbeat / cron 的 agent-side 原始执行上下文，但对内部 user 控制消息做正文级抑制
- 通过结构化模板约束摘要写法，避免将内部控制流噪声写入长期手册化语义

### 风险 4：旧配置仍存在，用户误以为还生效

缓解：

- 将旧字段标记为 deprecated no-op
- 在日志中打印清晰 warning
- 在后续架构文档中写明迁移路径

## 14. 文档影响

该改造会改变维护者对以下内容的理解，属于架构相关改动：

- frontdoor prompt 组装
- 上下文压缩边界
- heartbeat / cron 与历史可见性的关系
- frontdoor stage state 与 global summary 的分工
- 旧规则式压缩的下线与新配置语义

因此在实现完成后，需要使用：

- `skills/g3ku-architecture-maintenance/SKILL.md`

并评估更新以下文档：

- `docs/architecture/runtime-overview.md`
- `docs/architecture/web-and-admin.md`
- 如有必要，更新 `docs/architecture/README.md`

## 15. 实施顺序

推荐按以下顺序实施：

1. 抽取共享阶段工作集 helper
2. frontdoor 接入节点格式 local workset
3. 新增 global semantic summary engine
4. 定义触发规则与状态模型
5. 删除旧规则式历史压缩机制
6. 补测试并更新架构文档

## 16. 验收标准

满足以下条件时，可认为本设计实现完成：

- 主 agent 已复用节点侧的最近 3 阶段保留与更早阶段 block 压缩机制
- 10 段归档机制保持不变
- 旧规则式历史压缩代码路径已退出运行时
- prompt 中存在稳定的 global semantic summary block
- heartbeat / cron 的 agent-side 原始执行上下文可被后续 turn 通过 global summary 间接读取，同时内部 user 控制消息不会污染近场历史注入
- compression_state 能反映新的语义压缩刷新状态
- 相关测试通过
- 架构文档已根据最终落地结果更新
