# agent_browser

`agent_browser` 是仓库内的可调用包装层；真实第三方 CLI 本体安装在工作区根目录的 `externaltools/agent_browser/`，而 `tools/agent_browser/` 只负责注册、适配和说明。

## 何时使用

- 需要真实浏览器页面导航、点击、输入、截图或会话隔离时。
- 需要一个符合 g3ku 资源治理、权限控制和上下文加载规范的浏览器工具时。
- 需要让 agent 在 CLI 缺失时也能先读到安装 / 更新帮助时。

## 安装

- 上游项目地址：`https://github.com/vercel-labs/agent-browser`
- 注册目录固定在 `tools/agent_browser/`；不要把第三方 CLI 文件放进这里。
- 真实 CLI 本体必须安装到 `externaltools/agent_browser/`。
- 本地浏览器可执行文件应保存在 `externaltools/agent_browser/browsers/`，包装层会优先把 `AGENT_BROWSER_EXECUTABLE_PATH` 指向这里。
- 下载缓存、npm 缓存、一次性日志和其他临时内容应放到 `temp/agent_browser/`。
- 推荐安装流程：

```powershell
$env:TMP = "<workspace>\\temp"
$env:TEMP = "<workspace>\\temp"
$env:TMPDIR = "<workspace>\\temp"
$env:npm_config_cache = "<workspace>\\temp\\agent_browser\\npm-cache"
npm install agent-browser@0.22.2 --prefix "<workspace>\\externaltools\\agent_browser"
$env:AGENT_BROWSER_HOME = "<workspace>\\externaltools\\agent_browser\\home"
$env:PLAYWRIGHT_BROWSERS_PATH = "<workspace>\\externaltools\\agent_browser\\browsers"
& "<workspace>\\externaltools\\agent_browser\\node_modules\\.bin\\agent-browser.cmd" install
```

- 如果上游安装器仍把 Chrome 下载到用户目录而不是工作区，请把对应的 `chrome-*` 目录同步到 `externaltools/agent_browser/browsers/`，并继续以工作区里的副本为准。

- 安装后，优先验证本地安装路径，而不是验证全局 PATH：

```powershell
& "<workspace>\\externaltools\\agent_browser\\node_modules\\.bin\\agent-browser.cmd" --help
```

- 如果需要自定义启动前缀，只能让 `tools/agent_browser/resource.yaml -> settings.command_prefix` 指向工作区内的本地安装路径，仍应落在 `externaltools/agent_browser/` 下。

## 更新

- 更新时不要做全局安装，也不要写入 `tools/agent_browser/`。
- 先把临时下载 / 缓存放到 `temp/agent_browser/`，再在 `externaltools/agent_browser/` 内原地升级。
- 当前清单锁定的上游版本是 `v0.22.2`；若升级版本，必须同时更新：
  - `tools/agent_browser/resource.yaml -> source.ref`
  - `tools/agent_browser/resource.yaml -> current_version`
  - 本文件中的安装 / 更新说明
- 推荐更新流程：

```powershell
$env:TMP = "<workspace>\\temp"
$env:TEMP = "<workspace>\\temp"
$env:TMPDIR = "<workspace>\\temp"
$env:npm_config_cache = "<workspace>\\temp\\agent_browser\\npm-cache"
npm install agent-browser@0.22.2 --prefix "<workspace>\\externaltools\\agent_browser"
$env:AGENT_BROWSER_HOME = "<workspace>\\externaltools\\agent_browser\\home"
$env:PLAYWRIGHT_BROWSERS_PATH = "<workspace>\\externaltools\\agent_browser\\browsers"
& "<workspace>\\externaltools\\agent_browser\\node_modules\\.bin\\agent-browser.cmd" install
```

## 使用

- 常规调用：`mode=run`，并通过 `args` 传递原始 CLI 参数。
- `mode=install_help` / `mode=update_help` 是无副作用帮助模式：不会执行浏览器命令，且必须省略 `args`。
- 包装层会优先从 `externaltools/agent_browser/` 自动定位本地 CLI，而不是依赖全局 PATH。
- 运行时会把 `AGENT_BROWSER_HOME` 固定到 `externaltools/agent_browser/home/`，避免默认写入用户主目录下的 `~/.agent-browser/`。
- 运行时会优先把 `AGENT_BROWSER_EXECUTABLE_PATH` 指向 `externaltools/agent_browser/browsers/` 下的本地 Chrome。
- 运行时会把临时目录固定到 `temp/agent_browser/`。
- 默认会使用 g3ku 专用 profile 根目录：`.g3ku/tool-data/agent_browser/profiles`
- 默认会注入会话名：`g3ku-agent-browser`
- 若显式传入相对 `profile`，会相对 workspace 解析并自动创建目录。
- 若显式传入绝对 `profile`，该路径也必须留在 workspace 内。
- 如果 CLI 缺失，可先调用：
  - `load_tool_context(tool_id="agent_browser")`
  - 或 `agent_browser(mode="install_help")`

## 重要语义与预期管理

- 这个工具是 **官方 `agent-browser` CLI 的自动化包装器**，优先语义是“执行一个浏览器自动化命令”，**不是**“像桌面助手一样永久打开一个本地浏览器窗口并保持不动”。
- 因此，类似 `open <url>` 的命令应理解为：发起一次自动化导航动作；**不默认保证** 浏览器窗口会长期保持可见，也不保证在命令结束后继续常驻。
- 若工具结果表明执行仍在后台运行，应把它视为“后台自动化任务仍未结束”，而不是“用户侧页面已经稳定保持打开”。此时应继续用平台侧的等待 / 停止能力跟踪执行状态，而不是直接把动作描述成最终完成态。
- 当用户明确要求“打开后保持页面不动 / 让我持续看到这个页面”时，必须先说明该工具是否能稳定满足“常驻打开”的预期；若不能保证，要明确告知限制，而不是只回复“已打开”。

## 与后台执行配合

- `agent_browser` 负责发起浏览器命令；某些调用可能以后台执行形式返回。
- 如果平台返回后台执行标识，应继续使用平台提供的等待能力查看结束态，或在不再需要时停止该执行。
- 不要把后台执行中的 `background_running` 一类状态，误转述为“页面已稳定打开且会一直保留”。

## 故障排查

- 如果返回 `agent-browser CLI not found`：说明 `externaltools/agent_browser/` 下还没有可执行 CLI，或 `settings.command_prefix` 指向了错误位置。先查看安装帮助，再核对本地安装目录。
- 如果出现 `--profile ignored` 或 `daemon already running`：工具会先尝试关闭当前 session，再自动重试一次。
- 如果命令超时：工具会尝试关闭当前 session，并在结果里返回 `session_cleanup` 细节；这也意味着超时后的浏览器 / 会话可能已被清理，不应假设页面仍然保留。
