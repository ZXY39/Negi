# task_delete

用于删除已保存任务，采用同一工具内的两阶段确认协议。

## 何时调用

- 想先核对待删任务，再执行删除时，先调用 `mode="preview"`。
- 确认预览结果无误后，再调用 `mode="confirm"` 执行实际删除。

## 参数规则

- `任务id列表` 必填，支持批量。
- `mode="preview"`：
  - 不会删除任何数据
  - 只返回候选任务、状态、摘要、磁盘占用和 `confirmation_token`
- `mode="confirm"`：
  - 必须带上 `confirmation_token`
  - `任务id列表` 必须与预览阶段完全一致

## 删除行为

- 已完成、已失败、已暂停任务会直接删除。
- 进行中的任务会先暂停/停止，再尝试删除。
- 如果任务仍未完全停下，会返回 `still_stopping`，本次不删除。
- 删除完成后会清理与统计口径一致的 4 类磁盘数据：
  - task files
  - artifacts
  - event-history 归档目录
  - `temp/tasks/<safe_task_id>/` 任务级临时目录

## 使用提醒

- `confirm` 是逐个任务处理、逐个返回结果，不是整批事务。
- token 只用于短期确认；批次不一致、session 不一致或 token 失效都会拒绝确认。
