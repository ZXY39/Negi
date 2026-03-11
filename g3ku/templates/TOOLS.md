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

## picture_washing - DOUBAO 图像生成

- 必需参数：`prompt`、`image`、`ratio`
- 首选配置注入：设置 `tools.pictureWashing.baseUrl` 和 `tools.pictureWashing.authorization`
- 调用时参数 `base_url` 和 `authorization` 是可选的覆盖项（优先级高于配置）
- 可选生成参数：`style`、`model`、`stream`、`timeout_s`（均可在 `tools.pictureWashing` 中配置）
- 端点兼容性：接受以 host、`/v1`、`/v1/images`、`/v1/images/generations` 或 `/v1/responses` 结尾的 `base_url` 形式
- 图像提取后备顺序：`choices[0].message.images` -> `image_details.url` -> `image_details.original_url` -> 从 `message.content` 解析 URL
- 返回标准化的 JSON 字符串：`success`、`error`、`requestMeta`、`images`、`raw`
## agent_browser - External agent-browser browser automation

- Browser automation powered by the external `agent-browser` CLI/daemon.
- Use it for opening sites, searching, clicking, filling forms, logging in, reading page text, screenshots, downloads, cookies, storage, and state reuse.
- Recommended workflow:
  1. `open`
  2. `snapshot -i`
  3. use refs like `@e1`, `@e2` for `click` / `fill` / `get`
  4. re-run `snapshot` after navigation or DOM changes
- Use `headless=false` when the user explicitly wants to see the browser window.
- Use `headless=true` only for background probing or silent internal tasks.
- Returns structured JSON with `success`, `data`, `error`, `hint`, `stdout_raw`, and `stderr`.


