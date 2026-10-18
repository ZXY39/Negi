# Tool Update 检查清单

## 一、更新前

- [ ] 已判定这个工具是否驱动一份第三方安装
- [ ] 已读取 `source`
- [ ] 已读取 `current_version`
- [ ] 驱动第三方安装时，已按 toolskill 写明的路径确认安装目录
- [ ] 已按 `source.url` 查询上游最新状态

## 二、更新实现

- [ ] 第三方本体的升级发生在 `<workspace>/externaltools/<tool_id>/`
- [ ] 下载 / 缓存 / 解压中转发生在 `<workspace>/temp/<tool_id>/`
- [ ] `tools/<tool_id>/main/` 里只有 handler，没有上游本体
- [ ] 纯仓库内实现的升级发生在 `main/`
- [ ] 远端调用形状变化时，handler 与 `parameters` 一起同步
- [ ] 已同步更新 `source.ref`
- [ ] 已同步更新 `current_version.summary`
- [ ] 已同步更新 `current_version.compare_rule`
- [ ] 已同步更新 `current_version.source_of_truth`

## 三、toolskill 同步

- [ ] `toolskills/SKILL.md` 已更新
- [ ] 驱动第三方安装的工具的 `安装 / 更新 / 使用` 三段已同步
- [ ] 安装目录或中转目录变化时，toolskill 与 handler 里的路径都已同步
- [ ] 继续满足 `skills/add-tool/references/toolskill-checklist.md`

## 四、验证

- [ ] `resource.yaml` 仍可解析
- [ ] 工具仍能被发现，handler 能装载成工具对象
- [ ] 工具目录 / API 里该工具没有新增 `errors` / `warnings`
- [ ] `load_tool_context` 返回该工具的参数契约与 toolskill 正文
