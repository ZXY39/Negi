# 接入说明

- 上游来源：`https://github.com/openclaw/skills/tree/main/skills/jakelin/ddg-web-search`
- 接入时间：本次任务执行时
- 类型判断：skill，而不是 tool
- 判断依据：上游只提供说明性 `SKILL.md`，核心能力依赖宿主环境已有的 `web_fetch`；未见需要单独注册的可执行程序、安装脚本或函数工具封装。

## 拉取方式

由于当前 Windows 工作区直接 `git clone` 上游仓库时，遇到仓库内其他文件名包含 Windows 非法字符（如 `:`）导致 checkout 失败，因此本次改用 `raw.githubusercontent.com` 直接抓取目标 skill 文档完成迁移。

## 使用前提

- 当前运行环境需要具备网页抓取能力，例如 `web_fetch`
- 当前运行环境需要允许访问外网

## 风险与注意事项

- 本仓库当前 `resource.yaml` 无法精确声明 `web_fetch` 这类非本地函数工具依赖，因此这里只做文档说明
- 若后续需要更强可发现性，可在主索引或相关搜索/联网类技能中增加引用
