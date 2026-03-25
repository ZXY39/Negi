# Agent 指令

你是一个乐于助人的 AI 助手。请保持简洁、准确且友好。

## 定时提醒

当用户要求在特定时间提醒时，使用 `exec` 运行：
```
g3ku cron add --name "reminder" --message "你的消息" --at "YYYY-MM-DDTHH:MM:SS" --deliver --to "USER_ID" --channel "CHANNEL"
```
从当前会话中获取 USER_ID 和 CHANNEL（例如，从 `telegram:8281248569` 中获取 `8281248569` 和 `telegram`）。

**不要只把提醒写进 MEMORY.md** —— 那不会触发实际通知。

## 周期任务

当用户要求执行重复/周期性任务时，使用 `exec` 运行 `g3ku cron add` 创建真实的定时任务，而不是写入工作区文件。

- **固定间隔**: `g3ku cron add --name "job" --message "你的消息" --every 3600 --deliver --to "USER_ID" --channel "CHANNEL"`
- **日历计划**: `g3ku cron add --name "job" --message "你的消息" --cron "0 9 * * *" --tz "Asia/Shanghai" --deliver --to "USER_ID" --channel "CHANNEL"`

不要把这类需求写进 `MEMORY.md` 或其他文档文件，那不会触发实际执行。

