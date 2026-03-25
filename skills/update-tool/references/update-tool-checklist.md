# Tool Update 检查清单

## 一、更新前

- [ ] 已读取 `tool_type`
- [ ] 已读取 `source`
- [ ] 已读取 `current_version`
- [ ] `external` 时已读取 `install_dir`
- [ ] 已按 `source.url` 查询上游最新状态

## 二、更新实现

- [ ] `external` 的升级发生在工作区根目录的 `externaltools/<tool_id>/`
- [ ] `external` 的下载 / 缓存 / 解压中转发生在工作区根目录的 `temp/<tool_id>/`
- [ ] `external` 未新增 `main/`
- [ ] `internal` 的升级发生在仓库内实现
- [ ] 已同步更新 `source.ref`
- [ ] 已同步更新 `current_version.summary`
- [ ] 已同步更新 `current_version.compare_rule`
- [ ] 已同步更新 `current_version.source_of_truth`

## 三、toolskill 同步

- [ ] `toolskills/SKILL.md` 已更新
- [ ] 外置工具的 `安装 / 更新 / 使用` 三段已同步
- [ ] 安装目录或中转目录变化时，toolskill 中的路径说明也已同步
- [ ] 继续满足 `skills/add-tool/references/toolskill-checklist.md`

## 四、验证

- [ ] `resource.yaml` 仍可解析
- [ ] `external` 仍不在 callable tools 中
- [ ] 工具目录 / API 返回的 `tool_type` / `install_dir` / `callable` 正确
- [ ] `load_tool_context` 返回的 `install_dir` 正确
