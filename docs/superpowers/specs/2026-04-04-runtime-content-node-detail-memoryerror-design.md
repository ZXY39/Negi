# Runtime Content and Node Detail MemoryError Design

## Goal

修复 `task:8b13aaebb95d` 暴露出的运行时内存膨胀问题，重点阻断两条主要放大链路：

1. `content` 对已经 externalize 的 `tool_result:content` / 包装 artifact 再次做 `content.search/open` 时，把外层包装 JSON 当作正文继续展开，形成递归膨胀。
2. `task_node_detail` 默认返回过重的节点详情对象，父节点在汇总阶段反复读取 full detail、execution trace 和 artifact 列表，导致上下文和内存快速失控。

本设计选择平衡方案：同时修复 canonical content ref 解析、`task_node_detail` 瘦身，以及父节点汇总约束，避免只止血一侧而另一侧继续增长。

## Problem Summary

当前系统的文本结果 externalization 机制已经能够把大文本落到 artifact 文件里，但“引用协议”和“读取协议”没有闭环。工具结果在 externalize 后，系统知道它来自哪个 `origin_ref`，却没有把这个来源作为稳定结构字段继续向下传递。后续 `content` 工具拿到的常常只是最外层包装 ref，因此会继续打开包装 JSON，而不是打开原始网页、原始工具结果或原始正文。

与此同时，`task_node_detail` 的默认返回对象并非“面向汇总的轻量详情”，而是会拼出包含执行轨迹、工具调用参数、工具输出摘要、artifact 列表以及某些外部 ref 全文回填的重型对象。对于工具调用很多、artifact 很多的节点，这个对象本身就足以达到数十万字符。父节点如果把多个这类 detail 再次拿回上下文并继续调用 `content`，就会形成任务树级别的上下文和内存放大。

## Success Criteria

- 默认 `content.search/open/describe` 对包装型 tool result 使用 canonical ref，而不是外层 wrapper ref。
- 默认 `task_node_detail` 返回值应适合父节点汇总，不应包含大段 execution trace 正文、完整 artifact 列表或被自动解引用的 full output。
- 仍然保留调试路径，允许显式请求 raw wrapper 或 full detail。
- 现有运行时树、artifact 存储和 UI 不需要整体重做；只调整引用与 detail 返回协议。
- 为这类问题增加回归测试，确保不会再次出现 `content -> content` 自我放大。

## Non-Goals

- 不重写整个 artifact 存储层。
- 不更换 `content`、`task_node_detail`、`spawn_child_nodes` 的工具名。
- 不把所有历史 task event / runtime frame 全部迁移成新格式。
- 不在本轮改动中追求“模型绝不会误用工具”；本轮目标是让默认路径安全、误用成本变高。

## Chosen Approach

采用三段式修复：

1. 引入 canonical content reference 语义，让 `content` 默认跟随规范来源读取内容，而不是读取最外层包装 artifact。
2. 将 `task_node_detail` 改为 summary-first 接口，full detail 改成显式 opt-in。
3. 在工具描述和节点执行提示中约束父节点汇总行为，优先使用 ref 定位和轻量摘要，而不是反复拉全量 detail。

## Design

### 1. Canonical Content Reference Model

#### 1.1 问题

当前 `ContentNavigationService` 能通过 `_extract_origin_ref()` 提取来源引用，但 `content_summary_and_ref()` 仅返回 `summary` 和最外层 `ref`。后续 `log_service.record_tool_result_batch()` 将这个最外层 `ref` 写入 `task_node_tool_results.output_ref`。因此，消费者再拿这个 `output_ref` 调 `content.search/open` 时，只会读取 wrapper artifact 的 JSON 文本。

#### 1.2 设计

为 content envelope 和 content handle 引入明确的 ref 语义：

- `requested_ref`: 当前调用方传入的 ref。
- `resolved_ref`: 默认应被 `content` 读取和搜索的规范 ref。
- `origin_ref`: 如果当前内容是某个包装结果，指向它包裹的原始 ref。
- `wrapper_ref`: 指向当前包装 artifact 自身。
- `wrapper_depth`: 当前包装层级，便于调试和保护。

规范规则：

- 默认使用 `resolved_ref`。
- 若结果没有包装关系，则 `resolved_ref == wrapper_ref == requested_ref`。
- 若结果是包装型 tool result，则 `resolved_ref` 指向最靠近原始正文的 canonical ref，`wrapper_ref` 指向本层 artifact。

#### 1.3 Resolver 行为

`content.describe/search/open/head/tail` 新增可选视图参数：

- `view="canonical"`：默认值。沿 `resolved_ref` 读取。
- `view="raw"`：读取 wrapper artifact 原文，仅用于调试。

resolver 规则：

- 若 `ref` 指向的 artifact 内容本身是 content envelope，则继续向内追踪 `resolved_ref` / `origin_ref`。
- 限制最大跳数，例如 4。
- 使用 visited set 防止 A -> B -> A 循环。
- 如检测到循环，返回结构化错误，不再继续追踪。

#### 1.4 持久化规则

工具结果记录时：

- `task_node_tool_results.output_ref` 改为 canonical ref。
- `task_node_tool_results.payload.wrapper_ref` 存最外层 ref。
- `task_node_tool_results.payload.origin_ref` 存 origin ref。
- 允许 `output_preview_text` 继续保留 compact summary。

这样做以后，后续 `task_node_detail`、`execution_trace`、UI 和模型继续使用 `output_ref` 时，会天然命中 canonical 内容。

### 2. Summary-First Task Node Detail

#### 2.1 问题

`task_node_detail` 当前返回对象对“父节点汇总”来说过重。其体积主要来自：

- execution trace 中完整的 stage/tool call 列表。
- 每个工具步骤保留 `arguments_text` 和 `output_text`。
- 节点级 artifact 全量列表。
- 对 `final_output_ref` / `check_result_ref` 的自动全文回填。

#### 2.2 新接口语义

`task_node_detail` 新增 `detail_level` 参数：

- `summary`：默认。
- `full`：显式请求。

默认 `summary` 返回结构包含：

- 基础节点信息：`node_id`、`task_id`、`parent_node_id`、`depth`、`node_kind`、`status`、`goal`、`prompt_summary`、`updated_at`
- 轻量文本字段：
  - `input_preview`
  - `output_preview`
  - `check_result_preview`
  - `final_output_preview`
  - `failure_reason`
- 对应 refs：
  - `input_ref`
  - `output_ref`
  - `check_result_ref`
  - `final_output_ref`
- `artifact_count`
- `artifact_refs_preview`: 最多返回前 N 个 artifact 的精简元数据，不返回完整列表
- `execution_trace_summary`
  - 每个 stage 仅返回 `stage_goal`
  - 每个 stage 最近 N 个 tool calls
  - tool call 仅返回 `tool_name`、`status`、`output_ref`
- `execution_trace_ref`

`full` 模式才返回：

- 全量 execution trace
- 全量 artifact 列表
- 可选全文回填字段

#### 2.3 取消默认全文回填

默认 `summary` 模式下，不再自动通过 `final_output_ref` / `check_result_ref` 把正文全文塞回 detail。

改成：

- detail 返回 preview + ref。
- 调用方若确实需要正文，再对相应 ref 调 `content.open`。

这能显著降低 detail 的默认体积，也减少一次 detail 调用里隐含的二次解引用成本。

### 3. Execution Trace Externalization

#### 3.1 问题

`TaskProjectionNodeDetailRecord` 目前预留了 `execution_trace_ref` 字段，但没有真正使用。运行时每次构造 detail 时都即时拼 full trace，导致 detail 查询天然偏重。

#### 3.2 设计

为节点执行轨迹增加 externalized 存储：

- full execution trace 生成后落成 artifact。
- `TaskProjectionNodeDetailRecord.execution_trace_ref` 指向该 artifact。
- summary detail 只包含 compact trace summary。
- full detail 在显式请求时：
  - 优先从 `execution_trace_ref` 加载
  - 再按需拼接缺失部分

compact trace 规则：

- 每个 stage 只保留：
  - `stage_goal`
  - `tool_calls`
- 每个 tool call 只保留：
  - `tool_name`
  - `status`
  - `output_ref`

默认不返回：

- `arguments_text`
- `output_text`
- 大块 inline payload

这些字段只保留在 full trace artifact 中。

### 4. Artifact List Compaction

`node_detail()` 不再默认返回节点的完整 artifact 列表。默认改为：

- `artifact_count`
- `artifacts_preview`
  - `artifact_id`
  - `kind`
  - `title`
  - `ref`
  - `created_at`

如需全量列表，提供：

- `detail_level="full"` 时返回 `artifacts`
- 或新增单独列表接口参数，如 `include_artifacts=full`

### 5. Parent Aggregation Guardrails

仅靠返回结构变轻还不够，需要把父节点默认工作流改成低风险路径。

#### 5.1 工具描述

更新 `task_node_detail` 描述：

- 明确其默认返回 summary。
- 明确重内容应通过返回的 ref 再调用 `content`。
- 明确 full detail 仅适用于调试，不适合作为批量汇总输入。

#### 5.2 Prompt 规则

调整节点执行和验收提示：

- 汇总子节点时优先读取：
  - `final_output_ref`
  - `check_result_ref`
  - `execution_trace_ref`
  - `artifact_refs_preview`
- 不得为了汇总多个子节点而反复请求 full `task_node_detail`，除非 summary 证据不足。
- 对 `artifact:` 引用优先局部 `content.search/open`，避免全文打开。

### 6. Failure Handling

新增以下保护：

- `content` resolver 检测 wrapper 循环时返回结构化错误。
- `task_node_detail(full)` 如果 trace/artifact 体积超过阈值，允许自动 externalize 并返回 ref。
- summary 模式保证单次返回体有上限，避免再产生 20 万字符级 JSON。

## File-Level Responsibilities

- [g3ku/content/navigation.py](/D:/NewProjects/G3KU/g3ku/content/navigation.py)
  负责 canonical ref 解析、wrapper/origin 追踪、`view=canonical|raw` 语义。
- [main/monitoring/log_service.py](/D:/NewProjects/G3KU/main/monitoring/log_service.py)
  负责工具结果持久化时写 canonical ref、wrapper ref，以及 execution trace externalization。
- [main/monitoring/query_service.py](/D:/NewProjects/G3KU/main/monitoring/query_service.py)
  负责 summary-first node detail 组装，以及避免默认全文回填。
- [main/service/runtime_service.py](/D:/NewProjects/G3KU/main/service/runtime_service.py)
  负责 `task_node_detail` 工具层响应格式、`detail_level` 切换和 compact artifact 列表输出。
- [main/runtime/internal_tools.py](/D:/NewProjects/G3KU/main/runtime/internal_tools.py)
  负责更新工具 description 和参数定义。
- [main/prompts/node_execution.md](/D:/NewProjects/G3KU/main/prompts/node_execution.md)
  负责执行节点汇总策略约束。
- [main/prompts/acceptance_execution.md](/D:/NewProjects/G3KU/main/prompts/acceptance_execution.md)
  负责验收节点证据读取策略约束。

## Testing Strategy

### Unit Tests

- `content` canonical resolver:
  - wrapper -> origin 正常追踪
  - wrapper -> wrapper -> origin 正常追踪
  - A -> B -> A 循环安全失败
- `content` raw view:
  - `view=raw` 仍能读取 wrapper JSON 本体

### Runtime Query Tests

- `task_node_detail(summary)`：
  - 不返回 full trace
  - 不返回 full artifact list
  - 不自动回填 final/check 全文
- `task_node_detail(full)`：
  - 可以拿到 full trace 或其 ref

### Regression Tests

- 构造包含大量 wrapper artifact 的节点，验证 repeated `content.search/open` 不再出现包装层层放大。
- 构造包含大量 tool results 和 artifacts 的节点，验证 `task_node_detail(summary)` 的响应体受控。
- 构造父节点汇总多个子节点的场景，验证默认 detail + content 流程不会生成数十万字符级中间结果。

## Risks and Mitigations

- 风险：调整返回结构会影响依赖旧字段的调用方。
  - 缓解：保留 `full` 模式作为过渡路径，并补调用方测试。
- 风险：canonical resolver 误追踪到不该追的内容。
  - 缓解：只对明确识别为 content envelope 的 payload 做递归回源，并限制跳数。
- 风险：summary 过度压缩导致调试不便。
  - 缓解：保留 `view=raw` 和 `detail_level=full`。

## Rollout Order

1. 实现 canonical ref / wrapper ref / raw view。
2. 切换 tool result 持久化使用 canonical `output_ref`。
3. 改造 `task_node_detail` 为 summary-first，并启用 `execution_trace_ref`。
4. 更新 prompt/tool descriptions。
5. 增加回归测试并做压力验证。
