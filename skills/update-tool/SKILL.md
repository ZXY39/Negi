# 更新工具

当任务是升级、同步、追踪或审查 `tools/<tool_id>/` 下已有工具时，使用本规范。

更新前先看 `resource.yaml` 里的 `tool_type`，再决定走哪条分支。

## 目标

- 升级判断来自 `source` 和 `current_version`，不是来自印象。
- 更新后，代码 / 安装结果、`resource.yaml`、`toolskills/` 三者必须同步。
- `external` 工具更新的是工作区根目录 `externaltools/<tool_id>/` 里的真实安装，不得偷偷改回 vendored 模式。
- 外置工具更新过程中的下载、缓存、解压、备份中转文件统一放在工作区根目录 `temp/<tool_id>/`。

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
   - `install_dir`
3. 查询上游最新状态。
4. 按 `current_version.compare_rule` 判断是否升级。
5. 同步更新代码或安装结果、`resource.yaml`、toolskill。

## A. 更新外置工具

适用条件：`tool_type: external`

流程：

1. 先读取 `install_dir`，确认真实安装位置，应为 `<workspace>/externaltools/<tool_id>/`。
2. 准备更新时，把下载包、校验文件、解压中转目录放到 `<workspace>/temp/<tool_id>/`。
3. 按 `source.url` 查询上游最新版本。
4. 在 `install_dir` 内执行安装 / 升级操作，或把中转目录产物部署到该目录。
5. 不得在 `tools/<tool_id>/` 下新增 `main/`。
6. 更新这些内容：
   - `source.ref`
   - `current_version.summary`
   - `current_version.compare_rule`
   - `current_version.source_of_truth`
   - `toolskills/SKILL.md` 中的安装 / 更新 / 使用说明
7. 如果升级方式变了，必须同步改 `## 安装` 和 `## 更新` 两段。

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
- 临时下载 / 解压中转目录约定变化
- 上游 docs / references / templates 变化

## 外置工具的特殊要求

- `install_dir` 不存在时，不要直接把工具判定为“无需处理”；这通常意味着需要修复或重装。
- 外置工具必须继续满足：
  - 真实安装目录在 `externaltools/<tool_id>/`
  - 临时下载 / 解压中转目录在 `temp/<tool_id>/`
  - `tools/<tool_id>/` 只保留注册信息
- toolskill 必须继续保留：
  - `## 何时使用`
  - `## 安装`
  - `## 更新`
  - `## 使用`
- 如果外部安装目录或中转目录变更，必须同步更新 `resource.yaml -> install_dir` 和 toolskill 中的路径说明。

## 验证

- `resource.yaml` 仍可解析
- `external` 仍然不可直接调用，但在工具目录 API 中可见
- `load_tool_context` 返回的 `install_dir` 正确
- `internal` 工具仍能正常加载
- Tool 管理 / API 返回的 `tool_type` / `install_dir` / `callable` 正确
- 外置工具的安装目录仍在 `externaltools/<tool_id>/`
- 外置工具的临时下载 / 解压中转目录约定仍是 `temp/<tool_id>/`

## References

- `references/update-tool-checklist.md`
- `skills/add-tool/SKILL.md`
- `skills/add-tool/references/toolskill-checklist.md`

## Parameter Contract Discipline

- Keep `resource.yaml -> parameters`, runtime behavior, and `toolskills/SKILL.md` aligned in the same change.
- When nested object or array parameters change, update the toolskill example payload at the same time.
- Required fields, enum values, and shape changes must never live only in prose or only in code.
