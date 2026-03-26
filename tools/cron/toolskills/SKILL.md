# cron

使用 `cron` 安排提醒或周期任务。

创建于会话中的 cron 任务会优先回到原聊天线程；如果原线程后来被删除，结果会回落到独立线程 `cron:<job_id>`。自动调度只由 Web 主进程负责；CLI 可以创建、查看、移除和手动执行任务，但需要 Web 运行时在线时才会自动触发。

## 硬约束

- `cron(action="add")` 创建循环任务时，必须同时提供 `stop_condition`。
- `stop_condition` 必须写成：`此任务的具体退出条件 + 或用户要求取消`。
- `message` 只写用户可见的提醒或任务内容，不要把退出条件写进 `message`。
- 一次性 `at` 任务可以不写 `stop_condition`。
- 定时任务触发后，运行时会先检查是否满足 `stop_condition` 或用户是否已明确要求取消；满足则会立即自取消。

## 三种模式

1. 提醒：消息直接发送给用户。
2. 任务：消息是任务描述，系统定时执行并发送结果。
3. 一次性：指定时间执行一次后自动删除。

## 示例

固定提醒：

```python
cron(
    action="add",
    message="提醒我起来活动一下",
    every_seconds=1200,
    stop_condition="今天下班后或用户要求取消",
)
```

周期任务：

```python
cron(
    action="add",
    message="检查 HKUDS/g3ku GitHub stars 并汇报",
    every_seconds=600,
    stop_condition="仓库 stars 达到目标后或用户要求取消",
)
```

带时区的 cron：

```python
cron(
    action="add",
    message="工作日早上站会提醒",
    cron_expr="0 9 * * 1-5",
    tz="America/Vancouver",
    stop_condition="本周项目冲刺结束后或用户要求取消",
)
```

一次性任务：

```python
cron(
    action="add",
    message="提醒我参加会议",
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

- Web 运行时重启后，错过的单次任务会补执行一次。
- 周期任务如果在停机期间错过执行，恢复后只补最近一次，不会把所有漏掉的周期全部回放。
