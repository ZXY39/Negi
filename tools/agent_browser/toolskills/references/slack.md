# Upstream Skill: `slack`

## Source

- Original file: `tools/agent_browser/main/agent-browser/skills/slack/SKILL.md`
- Mirrored local reference docs:
  - `tools/agent_browser/toolskills/references/slack/slack-tasks.md`
- Upstream templates:
  - `tools/agent_browser/main/agent-browser/skills/slack/templates/slack-report-template.md`

## 何时加载

- 用户要查看 Slack 未读、频道、DM、消息摘要，或自动化常见 Slack 页面任务。
- 任务目标是 Slack Web 或 Slack 桌面端的导航、信息提取、截图取证。

## 核心要点

- Slack 任务往往从“连接现有会话或打开 Slack”开始，再用快照定位侧栏、Activity、DM、频道等区域。
- 常见流程包括检查未读、进入频道、展开侧栏条目、读取可见消息与截图。
- 如果任务更像“桌面 Slack 应用自动化”，可以与 `references/electron.md` 联合使用。

## 使用建议

- Slack 是高频垂直场景，优先读这份，而不是在通用网页参考里自己摸索 Slack 页面结构。
- 如果需要更细的 Slack 任务清单或报告模板，再打开 `slack-tasks.md` 和 `slack-report-template.md`。
