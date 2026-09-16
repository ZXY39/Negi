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
  - `sort` 可空：`time`（默认，按创建时间倒序）或 `size`（按占用字节倒序，快速定位占用空间大的任务）
- `mode="id"` 时：
  - `任务id列表` 必填
  - 返回顺序与输入顺序一致

## 统计口径

- `prompt_preview_100` 取任务初始提示词 `user_request` 的前 100 个字符摘要。
- `disk_usage_bytes` 是任务总占用，含两部分：
  - 目录文件：task files、artifacts、event-history 目录、`temp/tasks/<safe_task_id>/` 任务级临时目录
  - 数据库明细：五张大行表（模型调用、运行帧、工具结果、节点轮次/详情）的 payload 字节
- 取数来自任务大小记账表（写入点增量记账 + 小时级对账，展示延迟 ≤1h）；刚创建尚无记账的任务回退一次目录实测。
- 关键词只匹配任务初始提示词 `user_request`，不匹配中间日志和节点输出。

## 使用提醒

- `list` 模式的时间过滤基于任务 `created_at`。
- 如果任务不存在，返回项会标记为 `not_found`，而不是整批失败。
