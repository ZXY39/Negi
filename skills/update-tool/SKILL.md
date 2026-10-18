# 更新工具

当任务是升级、同步、追踪或审查 `tools/<tool_id>/` 下已有工具时，使用本规范。

先看这个工具是否驱动一份第三方安装（判据：`source.url` 指向上游项目，且 toolskill 带 `## 安装` 段），再决定走哪条分支。工具只有一类——可执行资源，`tool_type` 不再是分支条件。

## 目标

- 升级判断来自 `source` 和 `current_version`，不是来自印象。
- 更新后，代码 / 安装结果、`resource.yaml`、`toolskills/` 三者必须同步；运行时长特征变化时（新增长时编排步骤、改为自持子进程收尾等），同步增删 `resource.yaml -> timeout_policy` 声明；写入行为变化时（新加一次落库、一次投递、一次注册），同步撤掉或保住 `resource.yaml -> recovery_policy.rerun_safe` 声明成立的前提。
- 驱动第三方安装的工具，更新的是 `<workspace>/externaltools/<tool_id>/` 里的真实安装与它的 handler，不得偷偷把上游 vendored 回 `main/`。
- 安装过程中的下载、缓存、解压、备份中转文件统一放在 `<workspace>/temp/<tool_id>/`。

## 通用步骤

1. 读取：
   - `tools/<tool_id>/resource.yaml`
   - `tools/<tool_id>/toolskills/SKILL.md`
   - `tools/<tool_id>/toolskills/references/`
2. 理解：
   - `source`
   - `current_version`
   - `governance`
   - handler 指向的安装目录（toolskill 的 `## 安装` / `## 使用` 段写明）
3. 查询上游最新状态。
4. 按 `current_version.compare_rule` 判断是否升级。
5. 同步更新代码或安装结果、`resource.yaml`、toolskill。

## A. 更新驱动第三方安装的工具

适用条件：这个工具的 handler 驱动 `<workspace>/externaltools/<tool_id>/` 里的一份安装。

流程：

1. 先按 toolskill 写明的路径确认真实安装位置，应为 `<workspace>/externaltools/<tool_id>/`。
2. 准备更新时，把下载包、校验文件、解压中转目录放到 `<workspace>/temp/<tool_id>/`。
3. 按 `source.url` 查询上游最新版本。
4. 在该安装目录内执行安装 / 升级操作，或把中转目录产物部署到该目录。
5. 上游本体不得进入 `tools/<tool_id>/`；`main/` 里只放 handler。
6. 更新这些内容：
   - `source.ref`
   - `current_version.summary`
   - `current_version.compare_rule`
   - `current_version.source_of_truth`
   - `toolskills/SKILL.md` 中的安装 / 更新 / 使用说明
7. 如果升级方式变了，必须同步改 `## 安装` 和 `## 更新` 两段。
8. 安装结果的新版本改变调用形状时（远端方法名、参数、返回结构），同步改 handler 与 `parameters`。

## B. 更新纯仓库内实现

适用条件：这个工具没有第三方安装，逻辑全在 `main/`。

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

## 驱动第三方安装的工具的特殊要求

- toolskill 写明的安装目录不存在时，不要把这个工具判定为“无需处理”；这通常意味着需要修复或重装。
- 必须继续满足：
  - 第三方本体在 `externaltools/<tool_id>/`
  - 临时下载 / 解压中转目录在 `temp/<tool_id>/`
  - `tools/<tool_id>/` 只放注册信息与 handler
- toolskill 必须继续保留：
  - `## 何时使用`
  - `## 安装`
  - `## 更新`
  - `## 使用`
- 安装目录或中转目录约定变更时，同步更新 toolskill 里的路径说明与 handler 中拼路径的那一处。

## 验证

- `resource.yaml` 仍可解析
- 工具仍能被资源发现，handler 仍能装载成工具对象
- `load_tool_context` 返回该工具的参数契约与 toolskill 正文
- Tool 管理 / API 里该工具状态正常（`available`、`warnings`、`errors` 无新增）
- 第三方本体仍在 `externaltools/<tool_id>/`，中转目录约定仍是 `temp/<tool_id>/`

## References

- `references/update-tool-checklist.md`
- `skills/add-tool/SKILL.md`
- `skills/add-tool/references/toolskill-checklist.md`

## Parameter Contract Discipline

- Keep `resource.yaml -> parameters`, runtime behavior, and `toolskills/SKILL.md` aligned in the same change.
- When nested object or array parameters change, update the toolskill example payload at the same time.
- Required fields, enum values, and shape changes must never live only in prose or only in code.

## Result Delivery Flag

- Re-check `tool_result_inline_full` whenever the tool's output size, output shape, truncation behavior, or result-delivery contract changes.
- Typical triggers: switching between preview text and full text, adding or removing `output_ref`, changing whether results are chunked, or changing the default verbosity of the tool response.
- Avoid unsupported gray states. The tool should clearly behave as one of these contracts:
  - preview-only inline text plus `output_ref` for full data
  - full inline delivery with `tool_result_inline_full: true`
- If the delivery contract changes, update both `resource.yaml` and `toolskills/SKILL.md` in the same change so callers do not infer the wrong follow-up behavior.
