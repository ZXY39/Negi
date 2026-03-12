# 认证与状态复用

登录任务推荐流程：`open` -> `snapshot -i` -> `fill` -> `click` -> `wait` -> `snapshot -i`。

当用户明确要求保存登录态时，使用 `state save`。
当用户要求继续之前的站点会话时，先 `state load`，再 `open` 目标页面。

后台 cookies / 授权探测任务统一使用 `headless=true`。
如果失败，优先阅读 `error`、`hint`、`stderr` 和 `stdout_raw`，不要直接猜原因。
