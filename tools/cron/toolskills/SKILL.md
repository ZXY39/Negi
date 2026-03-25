# cron

使用 `cron` 安排提醒或周期任务。

创建于会话中的 cron 任务会优先回到原聊天线程；如果原线程后来被删除，结果会回落到独立线程 `cron:<job_id>`。
自动调度只由 Web 主进程负责。CLI 可以创建、查看、移除和手动执行任务，但需要 Web 运行时在线时才会自动触发。

## 三种模式

1. **提醒**：消息直接发送给用户
2. **任务**：消息是任务描述，系统定时执行并发送结果
3. **一次性**：指定时间执行一次后自动删除

## 示例

固定提醒：
```
cron(action="add", message="Time to take a break!", every_seconds=1200)
```

动态任务（每次由 agent 执行）：
```
cron(action="add", message="Check HKUDS/g3ku GitHub stars and report", every_seconds=600)
```

一次性任务（需要计算 ISO 时间）：
```
cron(action="add", message="Remind me about the meeting", at="<ISO datetime>")
```

带时区的 cron：
```
cron(action="add", message="Morning standup", cron_expr="0 9 * * 1-5", tz="America/Vancouver")
```

列出/移除：
```
cron(action="list")
cron(action="remove", job_id="abc123")
```

## 时间表达

| 用户说法 | 参数 |
|-----------|------------|
| every 20 minutes | every_seconds: 1200 |
| every hour | every_seconds: 3600 |
| every day at 8am | cron_expr: "0 8 * * *" |
| weekdays at 5pm | cron_expr: "0 17 * * 1-5" |
| 9am Vancouver time daily | cron_expr: "0 9 * * *", tz: "America/Vancouver" |
| at a specific time | at: ISO datetime 字符串（从当前时间计算） |

## 时区

使用 `tz` 搭配 `cron_expr` 以指定 IANA 时区；未设置时，使用服务器本地时区。

## 恢复语义

- Web 运行时重启后，错过的单次任务会补执行一次。
- 周期任务如果在停机期间错过执行，恢复后只补最近一次，不会把所有漏掉的周期全部回放。
