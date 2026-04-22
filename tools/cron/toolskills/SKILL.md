# cron

使用 `cron` 安排未来要提醒 agent 自己去做的事。

创建于会话中的 cron 任务会优先回到原聊天线程；如果原线程后来被删除，结果会回落到独立线程 `cron:<job_id>`。自动调度只由 Web 主进程负责；CLI 可以创建、查看、移除和手动执行任务，但需要 Web 运行时在线时才会自动触发。

## 当前合同

- `message` 必须写成给未来自己的提醒动作，不要写成面向用户的成品回复。
- `cron` 是提醒机制，不是自然语言停止条件引擎。
- 重复提醒靠 `max_runs` 控制；每次成功送达给 agent 后计 1 次，到达上限会自动删除。
- 如果没有明确写提醒次数，系统默认按一次性提醒处理，即 `max_runs=1`。
- `at` 一次性提醒总是按 `max_runs=1` 处理。
- 如果 `at` 的目标时间在工具真正执行时已经过去，运行时会直接拒绝创建，并提示 `任务定时已过期，当前时间为<service-local time>，请立即执行或视情况废弃而不要创建过期任务`。
- `stop_condition` 仅保留为兼容参数，不再参与运行时停止判定。

## 写法要求

推荐写法：

- `message="向用户汇报当前时间"`
- `message="检查任务 task:xxx 的最新进展并汇报"`
- `message="提醒用户会议即将开始"`

不要这样写：

- `message="当前最新时间：请查看本条消息的发送时间。"`
- `message="提醒你该开会了"`
- `message="完成 3 次后自动停止"`

## 示例

重复提醒 3 次：

```python
cron(
    action="add",
    message="向用户汇报当前时间",
    every_seconds=300,
    max_runs=3,
)
```

工作日提醒 5 次：

```python
cron(
    action="add",
    message="提醒用户准备参加站会",
    cron_expr="0 9 * * 1-5",
    tz="America/Vancouver",
    max_runs=5,
)
```

一次性提醒：

```python
cron(
    action="add",
    message="提醒用户会议即将开始",
    at="<ISO datetime>",
)
```

列出 / 移除：

```python
cron(action="list")
cron(action="remove", job_id="abc123")
```

## 时间表达

| 用户说法 | 参数 |
| --- | --- |
| every 20 minutes | `every_seconds=1200` |
| every hour | `every_seconds=3600` |
| every day at 8am | `cron_expr="0 8 * * *"` |
| weekdays at 5pm | `cron_expr="0 17 * * 1-5"` |
| 9am Vancouver time daily | `cron_expr="0 9 * * *", tz="America/Vancouver"` |
| at a specific time | `at="<ISO datetime>"` |

## 恢复语义

- Web 运行时重启后，错过的单次提醒会补执行一次。
- 周期提醒如果在停机期间错过执行，恢复后只补最近一次，不会把所有漏掉的周期全部回放。
