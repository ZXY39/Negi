# agent_browser

`agent_browser` 是对 vendored 上游项目 `tools/agent_browser/main/agent-browser` 的 G3KU 包装器。主 skill 只保留本工具自己的调用契约；上游仓库自带的各个 skill 被拆成按需加载的 `references/` 文档，避免主 skill 过长。

## 何时调用

- 需要真实浏览器自动化时调用，例如打开网页、点击、输入、截图、导出 PDF、抓取可访问性快照、读取页面文本、等待页面状态变化。
- 任务需要跨多次调用维持浏览器上下文时优先使用它，可通过 `session` 或 `session_name` 明确指定会话标识。
- 需要保留登录态、Cookie、localStorage 或浏览器 profile 时继续使用它。默认会为工具自己的默认会话创建独立 `--profile` 目录。
- 如果只是读写本地文件，优先用 `filesystem`；如果只是执行普通 shell，优先用 `exec`。

## 参数

- `args`: 必填，字符串数组。表示传给 `agent-browser` 的 CLI 参数，不包含可执行文件本身。
- `working_dir`: 可选，命令工作目录。相对路径按 G3KU 工作区解析，且必须位于工作区内部。
- `stdin`: 可选，写入标准输入的文本，适合 `eval --stdin` 之类的命令。
- `timeout`: 可选，超时秒数，默认使用 `tools/agent_browser/resource.yaml` 中的 `settings.timeout`。
- `session`: 可选，透传为 `--session`，用于隔离 agent-browser 会话。
- `profile`: 可选，覆盖自动注入的 `--profile`。相对路径按工作区解析。
- `session_name`: 可选，透传为 `--session-name`，用于 agent-browser 的状态持久化命名。

## 默认行为

- 可执行文件解析顺序为：`settings.executable` → 工具目录内仓库的本地二进制 → 首次调用时按仓库 `package.json.version` 下载匹配 release 二进制 → `cargo run --manifest-path cli/Cargo.toml --`。
- 默认不再依赖 G3KU 运行时会话注入；如需稳定会话，请显式传 `session`，或开启 `settings.auto_session` 使用 `settings.default_session`。
- Tool 管理页中的治理入口来自 `resource.yaml -> governance`；它与这里的 toolskill 文档互补，不互相替代。

## 返回结果

工具始终返回 JSON 字符串，常见字段如下：

- `ok`: 是否执行成功。
- `command`: 实际执行的完整命令数组。
- `cwd`: 实际工作目录。
- `exit_code`: 进程退出码。
- `stdout`: 标准输出文本。
- `stderr`: 标准错误文本。
- `stdout_json`: 当 `stdout` 本身是合法 JSON 时，附带解析后的结构化内容。
- `error`: 启动失败、参数错误或超时时返回。

## 常见失败场景与回退

- 仓库二进制不存在：工具会先尝试按仓库版本号下载匹配 release 二进制；如果仍失败，再检查 `cargo` 或 `settings.executable`。
- 首次运行较慢：如果仓库尚未准备二进制，第一次调用可能需要下载匹配版本文件，或回退到 `cargo run` 编译 Rust CLI。
- Chrome 未安装：先执行 `args=["install"]`，必要时再补充上游支持的安装参数。
- 页面交互失败：先执行 `snapshot` 或 `get text` 确认元素引用和页面状态，再继续点击或输入。
- 需要保存截图、PDF 或导出文件：把输出路径放到工作区内，再配合 `filesystem` 读取或检查产物。

## 与其他工具的关系

- 与 `filesystem` 配合：读取截图、PDF、导出状态文件，或检查工作区中的自动化产物。
- 与 `exec` 配合：仅在需要诊断二进制、Chrome、环境变量或仓库构建状态时使用；正常浏览器自动化优先直接调用 `agent_browser`。

## References

上游仓库 `tools/agent_browser/main/agent-browser/skills/` 中的所有 skill 都作为本工具的按需 references 提供。默认不要一次性加载全部，只读取与当前任务最相关的一份或少量几份：

- `tools/agent_browser/toolskills/references/agent-browser.md`: 通用网页自动化主流程、认证、状态、快照与常用命令。
- `tools/agent_browser/toolskills/references/dogfood.md`: 面向 QA / exploratory testing / bug hunt 的系统化测试流程与证据产出。
- `tools/agent_browser/toolskills/references/electron.md`: Electron 桌面应用自动化，如何连到 CDP 端口并复用同样的快照交互模式。
- `tools/agent_browser/toolskills/references/slack.md`: Slack 工作区导航、未读检查、频道与消息提取等常见任务。
- `tools/agent_browser/toolskills/references/vercel-sandbox.md`: 在 Vercel Sandbox microVM 里运行 agent-browser + Chrome 的集成模式。

这些 reference 文件都指回各自的上游原始路径，并说明如果需要更深细节，应继续打开上游 skill 自带的 `references/`、`templates/` 或示例文件。

已镜像的上游 `references/` 子文档位于：

- `tools/agent_browser/toolskills/references/agent-browser/`
- `tools/agent_browser/toolskills/references/dogfood/`
- `tools/agent_browser/toolskills/references/slack/`

## 推荐调用示例

```json
{"args": ["open", "https://example.com"]}
```

```json
{"args": ["snapshot"]}
```

```json
{"args": ["screenshot", "artifacts/example.png"], "working_dir": "."}
```

```json
{"args": ["eval", "--stdin"], "stdin": "document.title"}
```
