# Upstream Skill: `dogfood`

## Source

- Original file: `tools/agent_browser/main/agent-browser/skills/dogfood/SKILL.md`
- Mirrored local reference docs:
  - `tools/agent_browser/toolskills/references/dogfood/issue-taxonomy.md`
- Upstream templates:
  - `tools/agent_browser/main/agent-browser/skills/dogfood/templates/dogfood-report-template.md`

## 何时加载

- 用户要你 dogfood、QA、做 exploratory testing、bug hunt、系统化验收某个站点或 Web 应用。
- 任务输出要包含问题列表、复现步骤、截图、视频或结构化报告。

## 核心要点

- 它不是单次网页操作指南，而是完整的测试工作流：初始化、认证、探查、记录问题、收尾。
- 输出强调“带证据的交付物”，包括可复现步骤、截图与最终报告。
- 默认以最少提问启动，仅在提到认证但缺少凭据时再向用户要信息。

## 使用建议

- 当 `agent_browser` 被用于质量检查，而不是只做一次网页操作时，优先加载本 reference。
- 如果需要问题分类标准或报告模板，再继续打开 `issue-taxonomy.md` 和 `dogfood-report-template.md`。
