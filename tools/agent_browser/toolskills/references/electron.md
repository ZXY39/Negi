# Upstream Skill: `electron`

## Source

- Original file: `tools/agent_browser/main/agent-browser/skills/electron/SKILL.md`

## 何时加载

- 用户要操作 Electron 桌面应用，例如 VS Code、Slack、Discord、Figma、Notion、Spotify。
- 任务是连接到已运行的 Electron 应用、打开带 `--remote-debugging-port` 的桌面程序，或者测试 Electron 应用的 UI。

## 核心要点

- Electron 自动化的关键不是 `open <url>`，而是先给应用启用远程调试端口，再用 `agent-browser connect <port>` 连上 CDP。
- 连上以后，后续仍然沿用网页自动化的标准模式：`snapshot -i -> click/fill -> re-snapshot -> screenshot`。
- 当用户说“桌面应用”“原生应用”“Electron app”时，应优先想到这份 reference，而不是普通网页导航。

## 使用建议

- 如果任务对象是 Chromium/Electron 桌面应用，优先读这份。
- 如果连上 CDP 之后仍需通用交互技巧，再补读 `references/agent-browser.md`。
