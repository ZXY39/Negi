# Upstream Skill: `agent-browser`

## Source

- Original file: `tools/agent_browser/main/agent-browser/skills/agent-browser/SKILL.md`
- Mirrored local reference docs:
  - `tools/agent_browser/toolskills/references/agent-browser/authentication.md`
  - `tools/agent_browser/toolskills/references/agent-browser/commands.md`
  - `tools/agent_browser/toolskills/references/agent-browser/profiling.md`
  - `tools/agent_browser/toolskills/references/agent-browser/proxy-support.md`
  - `tools/agent_browser/toolskills/references/agent-browser/session-management.md`
  - `tools/agent_browser/toolskills/references/agent-browser/snapshot-refs.md`
  - `tools/agent_browser/toolskills/references/agent-browser/video-recording.md`
- Upstream templates:
  - `tools/agent_browser/main/agent-browser/skills/agent-browser/templates/authenticated-session.sh`
  - `tools/agent_browser/main/agent-browser/skills/agent-browser/templates/capture-workflow.sh`
  - `tools/agent_browser/main/agent-browser/skills/agent-browser/templates/form-automation.sh`

## 何时加载

- 当前任务是标准网页自动化。
- 需要导航、快照、表单填写、点击、等待、截图、导出或抓取数据。
- 需要认证、状态持久化、代理、录屏、profile 或 session 细节。

## 核心要点

- 主流程是 `open -> snapshot -i -> interact -> re-snapshot`。
- 如果中间步骤不需要读取输出，可在同一条 shell 命令里用 `&&` 链式执行；若需要先解析快照引用，再继续执行，则分步调用更安全。
- 认证路径覆盖：导入用户浏览器认证、持久 profile、`--session-name` 自动状态保存、auth vault、手动 `state save/load`。
- 当任务进入代理、性能分析、快照引用细节或视频录制时，继续打开本地镜像目录 `tools/agent_browser/toolskills/references/agent-browser/` 下的对应专题文件。

## 使用建议

- 默认优先把它当作本工具最常用的通用参考。
- 如果只是一般网页交互，读这一份通常就够了。
- 如任务明显偏向 QA、Slack、Electron 或 Vercel 集成，优先切换到对应 reference，再按需回到这份通用参考。
