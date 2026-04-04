# task_stats

用于查询已保存任务的统计视图，默认作用域是全局所有任务，不按当前 session 过滤。

## 何时调用

- 需要在一个时间窗口内盘点任务时，用 `mode="list"`。
- 已知一批任务 id，想直接看它们的状态、摘要和占用时，用 `mode="id"`。

## 参数规则

- `mode` 只能是 `list` 或 `id`。
- `mode="list"` 时：
  - `from` 必填，格式固定为 `YYYY/M/D`
  - `to` 必填，格式固定为 `YYYY/M/D`
  - `任务关键词` 可空；传多个时按 OR 匹配
- `mode="id"` 时：
  - `任务id列表` 必填
  - 返回顺序与输入顺序一致

## 统计口径

- `prompt_preview_100` 取任务初始提示词 `user_request` 的前 100 个字符摘要。
- `disk_usage_bytes` 是以下 4 类数据大小之和：
  - task files
  - artifacts
  - event-history 归档目录
  - `temp/tasks/<safe_task_id>/` 任务级临时目录
- 关键词只匹配任务初始提示词 `user_request`，不匹配中间日志和节点输出。

## 使用提醒

- `list` 模式的时间过滤基于任务 `created_at`。
- 如果任务不存在，返回项会标记为 `not_found`，而不是整批失败。
