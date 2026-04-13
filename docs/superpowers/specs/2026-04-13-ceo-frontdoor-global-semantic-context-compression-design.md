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

### 7.1 同步触发

在 frontdoor 组装一次真实模型调用的 prompt 之前，满足任一条件时，同步刷新 global summary：

1. 当前不存在可用的 `global semantic summary`
2. 现有 summary 的覆盖边界落后于当前可压缩全局区
3. 现有 summary 已存在，但完整 prompt 仍接近上下文窗口上限
4. 上一次 summary 生成失败，cooldown 已结束，且本轮仍需要总压缩

### 7.2 延后刷新

若仅存在“有新历史进入全局压缩区”，但当前 prompt 仍远低于安全阈值，则不立即刷新。  
此时只记录：

- `needs_refresh = true`

等下一次真正需要压缩时再刷新。

### 7.3 推荐配置语义

新增 token-based 配置，不再使用旧的 message-count 压缩阈值：

- `frontdoor_global_summary_trigger_tokens`
  - 更老全局区达到该 token 量后，允许生成或更新总压缩摘要
- `frontdoor_global_summary_hard_pressure_ratio`
  - 组装后 prompt 接近模型上下文窗口多少比例时，必须刷新或重算摘要
- `frontdoor_global_summary_min_delta_tokens`
  - 只有新进入压缩区的增量达到该值，才值得刷新摘要
- `frontdoor_global_summary_model`
  - 可选，允许单独指定压缩摘要模型
- `frontdoor_global_summary_failure_cooldown_seconds`
  - 摘要失败后的冷却期

## 8. 心跳、Cron 与临时内部消息兼容

### 8.1 输入过滤规则

总压缩输入必须遵守以下硬规则：

- `heartbeat_internal` 对应的内部 user message 不进入总压缩源数据
- `cron_internal` 对应的内部 user message 不进入总压缩源数据
- `metadata.history_visible == false` 的 assistant 消息不进入总压缩源数据
- 非 `user|assistant|tool` 的非历史可见消息不进入总压缩源数据

### 8.2 间接影响规则

heartbeat / cron 可以通过 durable state 间接影响总压缩，例如：

- 更新 frontdoor stage state
- 更新 task ledger
- 产生新的 archive refs
- 写入用户可见的 assistant 最终结果

但 heartbeat / cron 的原始临时消息本身，不作为长期语义总结对象。

### 8.3 触发兼容规则

heartbeat / cron 兼容原则如下：

- 内部 turn 本身不强制触发 global summary 刷新
- 其引起的 durable state 变化只设置 `needs_refresh`
- 若 heartbeat / cron 自己这一轮 prompt 已经接近上限，可复用现有 global summary
- 仅在确有上下文压力时，才允许 heartbeat / cron 触发一次同步摘要刷新

## 9. 参考 Hermes 的可借鉴做法

参考项目：`D:\NewProjects\hermes-agent-main`

本设计明确借鉴 Hermes 的以下技术路线：

1. `lossy summarization` 作为总压缩主路线
2. handoff/reference 风格的摘要前缀
3. 结构化摘要模板，而不是自然语言散文
4. 迭代式摘要更新，而不是每轮从零总结
5. 内容量驱动的摘要预算
6. 摘要失败后的 cooldown

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
5. heartbeat / cron / `history_visible=false` 消息不进入总压缩输入
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

- 总压缩输入统一走 `history_visible` / internal-source 过滤
- 临时内部消息只通过 durable state 间接影响 summary

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
- 心跳、cron、`history_visible=false` 消息不会污染长期摘要
- compression_state 能反映新的语义压缩刷新状态
- 相关测试通过
- 架构文档已根据最终落地结果更新
