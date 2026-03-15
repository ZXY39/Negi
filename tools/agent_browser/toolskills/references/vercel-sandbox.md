# Upstream Skill: `vercel-sandbox`

## Source

- Original file: `tools/agent_browser/main/agent-browser/skills/vercel-sandbox/SKILL.md`

## 何时加载

- 用户要在 Vercel 部署环境里运行 agent-browser。
- 任务涉及 Vercel Sandbox microVM、服务端调用、无浏览器二进制限制的部署方案、或跨命令保持浏览器环境。

## 核心要点

- 这是集成模式参考，不是一般网页交互参考。
- 重点在于：如何在 Vercel Sandbox 中准备 Chromium 依赖、安装 agent-browser、使用 snapshot 提速，并在框架服务端代码里调用浏览器命令。
- 如果任务是 Next.js / SvelteKit / Remix / Astro / Nuxt 中的服务端集成，而不是本地直接执行 CLI，应优先加载本 reference。

## 使用建议

- 当问题从“如何用 agent-browser 操作页面”转为“如何在 Vercel 里运行 agent-browser”时，切到这份 reference。
- 如果部署逻辑确定后仍需具体浏览器交互，再补读 `references/agent-browser.md`。
