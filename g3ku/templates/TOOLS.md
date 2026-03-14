# 工具使用说明

工具签名通过函数调用自动提供。
此文件记录了非显而易见的约束条件和使用模式。

## exec - 安全限制

- 命令有可配置的超时时间（默认 60 秒）
- 危险命令会被阻止（`rm -rf`、`format`、`dd`、`shutdown` 等）
- 输出被截断为 10,000 个字符
- `restrictToWorkspace` 配置可以限制对工作区的文件访问

## cron - 定时提醒

- 请参阅 cron 技能了解用法。

## filesystem - 工作区文件操作

- `action` 必填，可选值：`read`、`list`、`write`、`edit`、`delete`、`propose_patch`
- `path` 始终必填；相对路径按 workspace 解析
- `write` 需要 `content`
- `edit` 和 `propose_patch` 需要 `old_text`、`new_text`
- `propose_patch` 可选 `summary`，返回补丁工件 JSON，不会直接修改文件
## agent_browser - 外部 agent-browser 浏览器自动化

- 由外部 `agent-browser` CLI/daemon 提供的浏览器自动化。
- 用于打开网站、搜索、点击、填写表单、登录、读取页面文本、截图、下载、Cookie 以及存储和状态复用。
- 推荐工作流：
  1. `open` (打开)
  2. `snapshot -i` (交互快照)
  3. 使用如 `@e1`, `@e2` 的引用进行 `click` (点击) / `fill` (填写) / `get` (获取)
  4. 在导航或 DOM 变更后重新运行 `snapshot` (快照)
- 当用户明确要求查看浏览器窗口时，使用 `headless=false`。
- 仅在后台探测或静默内部任务时使用 `headless=true`。
- 返回结构化的 JSON，包含 `success` (成功)、`data` (数据)、`error` (错误)、`hint` (提示)、`stdout_raw` (标准输出原文) 和 `stderr` (错误输出原文)。



