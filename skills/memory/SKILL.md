# 核心记忆 (Memory)

## 结构

- `memory/MEMORY.md` — 长期事实（偏好、项目背景、关系）。始终加载到您的上下文中。
- `memory/HISTORY.md` — 仅追加的事件日志。**不**加载到上下文中。优先使用 `exec` + `rg` 搜索；如果环境没有 `rg`，再退回 PowerShell `Select-String`。每个条目都以 [YYYY-MM-DD HH:MM] 开头。

## 搜索过去发生的事件

```bash
rg -n -i "关键词" memory/HISTORY.md
```

使用 `exec` 工具运行搜索。组合模式：`rg -n -i "会议|截止日期" memory/HISTORY.md`

如果当前环境没有 `rg`，改用 PowerShell：

```powershell
Select-String -Path memory/HISTORY.md -Pattern "会议|截止日期" -CaseSensitive:$false
```

## 何时更新 MEMORY.md

立即使用 `filesystem` 记录重要事实：
- 优先使用 `action=edit` 做精确更新。
- 需要完整重写文件时使用 `action=write`。
- 用户偏好（"我更喜欢深色模式"）
- 项目背景（"API 使用 OAuth2"）
- 人际关系（"Alice 是项目负责人"）

## 自动整合

当会话内容过多时，旧的对话会自动总结并追加到 `HISTORY.md` 中。长期事实会被提取到 `MEMORY.md`。您不需要手动管理这个过程。
