# 更新工具

当任务是升级、同步、追踪或审核 `tools/<tool_id>/` 下已有工具时，使用本规范。

更新前先看 `resource.yaml` 里的 `tool_type`，再决定走哪条分支。

## 目标

- 升级判断来自 `source` 和 `current_version`，不是来自印象。
- 更新后，代码/安装结果、`resource.yaml`、`toolskills/` 三者必须同步。
- `external` 工具更新的是外部安装目录，不得偷偷改回 vendored 模式。

## 通用步骤

1. 读取：
   - `tools/<tool_id>/resource.yaml`
   - `tools/<tool_id>/toolskills/SKILL.md`
   - `tools/<tool_id>/toolskills/references/`
2. 理解：
   - `tool_type`
   - `source`
   - `current_version`
   - `governance`
3. 查询上游最新状态。
4. 按 `current_version.compare_rule` 判断是否升级。
5. 同步更新代码或安装结果、`resource.yaml`、toolskill。

## A. 更新外置工具

适用条件：`tool_type: external`

流程：

1. 先读取 `install_dir`，确认真实安装位置。
2. 按 `source.url` 查询上游最新版本。
3. 在 `install_dir` 内执行安装/升级操作。
4. 不得在 `tools/<tool_id>/` 下新增 `main/`。
5. 更新这些内容：
   - `source.ref`
   - `current_version.summary`
   - `current_version.compare_rule`
   - `current_version.source_of_truth`
   - `toolskills/SKILL.md` 中的安装/更新/使用说明
6. 如果升级方式变了，必须同步改 `## 安装` 和 `## 更新` 两段。

## B. 更新内置工具

适用条件：`tool_type: internal`

流程：

1. 如果是自研工具，直接更新 `main/tool.py`、参数契约和文档。
2. 如果是仓库内 vendored 项目，更新 `main/<project>/` 或包装层实现。
3. 同步更新：
   - `source.ref`
   - `current_version.*`
   - `parameters`
   - `settings`
   - `permissions`
   - `toolskills/SKILL.md`

## 什么时候必须更新 toolskill

出现任一情况都要同步更新：

- 安装方式变化
- 更新方式变化
- 参数变化
- 默认行为变化
- 输出结构变化
- 外部安装目录约定变化
- 上游 docs / references / templates 变化

## 外置工具的特殊要求

- `install_dir` 不存在时，不要直接把工具判定为“无需处理”；这通常意味着需要修复或重装。
- toolskill 必须继续保留：
  - `## 何时使用`
  - `## 安装`
  - `## 更新`
  - `## 使用`
- 如果外部安装目录变更，必须同步更新 `resource.yaml -> install_dir` 和 toolskill 中的路径说明。

## 验证

- `resource.yaml` 仍可解析
- `external` 仍然不可调用，但在工具目录/API 中可见
- `load_tool_context` 返回的 `install_dir` 正确
- `internal` 工具仍能正常加载
- Tool 管理/API 返回的 `tool_type/install_dir/callable` 正确

## References

- `references/update-tool-checklist.md`
- `skills/add-tool/SKILL.md`
- `skills/add-tool/references/toolskill-checklist.md`
