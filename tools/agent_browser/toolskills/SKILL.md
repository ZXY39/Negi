# agent_browser

内置的 `agent_browser` 工具只包装官方 `agent-browser` CLI，不 vendoring 上游源码。

## 何时使用

- 需要真实浏览器页面导航、点击、输入、截图或会话隔离时。
- 需要一个符合 g3ku 资源治理、权限控制和上下文加载规范的浏览器工具时。
- 需要让 agent 在 CLI 缺失时也能先读到安装/更新帮助时。

## 安装

- 上游项目地址：`https://github.com/vercel-labs/agent-browser`
- 推荐按上游文档安装官方 CLI，并确保 `agent-browser` 可从 `PATH` 直接调用。
- 安装后，可用 `exec` 检查命令是否可见，例如：
  - Windows: `where agent-browser`
  - macOS / Linux: `which agent-browser`
- 如果需要自定义命令位置，可在 `tools/agent_browser/resource.yaml -> settings.command_prefix` 中改为绝对路径或固定前缀。

## 更新

- 优先按上游仓库的官方更新方式更新 CLI。
- 更新后，重新运行路径检查命令，确认 `agent-browser` 仍可从 `PATH` 调用。
- 如果你修改过 `command_prefix`，确认更新后的可执行文件路径没有变化。

## 使用

- 常规调用：`mode=run`，并通过 `args` 传递原始 CLI 参数。
- 默认会使用 g3ku 专用 profile 根目录：`.g3ku/tool-data/agent_browser/profiles`
- 默认会注入会话名：`g3ku-agent-browser`
- 若显式传入相对 `profile`，会相对 workspace 解析并自动创建目录。
- 如果 CLI 缺失，可先调用：
  - `load_tool_context(tool_id="agent_browser")`
  - 或 `agent_browser(mode="install_help")`

## 故障排查

- 如果返回 `agent-browser CLI not found`：说明当前环境里没有找到 CLI，可先查看安装帮助并使用 `exec` 检查 PATH。
- 如果出现 `--profile ignored` 或 `daemon already running`：工具会先尝试关闭当前 session，再自动重试一次。
- 如果命令超时：工具会尝试关闭当前 session，并在结果里返回 `session_cleanup` 细节。
