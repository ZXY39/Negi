# create_async_task

把用户请求转交为后台异步任务。

## 何时使用
- 任务复杂、耗时长、需要后台持续推进。
- CEO 不应在当前会话里长时间自行处理，而应尽快派发。
- 需要把核心需求稳定传递给后续任务树时。

## 必填参数
- `task`：给下游执行链路的完整任务说明。
- `core_requirement`：一句高度概括的核心需求；不能留空，也不要简单复制 `task` 原文。
- `execution_policy`：执行策略对象，必须显式提供 `mode`。
  - `mode="focus"`：只做最高价值、最必要、与当前目标直接相关的动作。
  - `mode="coverage"`：先做最高价值动作，并在需要时允许扩展范围、补做边缘分支或系统性全量操作。

## 推荐参数
- `requires_final_acceptance=true`
- `final_acceptance_prompt`：写清最终验收标准。

## 任务说明要求
- 写清目标范围、关键线索和预期产出。
- 如果需要 skill 或工具上下文，明确要求下游节点先查看并使用相关 skill / tool context。
- 不要粘贴大段原文；优先给文件路径、目录路径、搜索关键词、引用和目标产出。

## 返回结果
- 成功时返回创建结果文本，其中会包含新任务的 `task_id`。

## 注意
- `core_requirement` 不能为空。
- 必须显式传入 `execution_policy.mode`，不能省略。
- 当 `requires_final_acceptance=true` 时，必须同时提供 `final_acceptance_prompt`。
