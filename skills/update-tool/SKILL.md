# 更新工具

当任务是升级、同步、追踪或审核 `tools/<tool_id>/` 下已有工具时，使用本规范。它适用于两类场景：

- 根据 `resource.yaml -> source` 查询上游最新更新，并决定是否升级当前工具。
- 在升级工具实现后，同步更新 `toolskills/SKILL.md`、本地 `references/`、`current_version`、`source.ref` 以及相关治理或文档内容。

## 目标

- 更新判断有依据：先看 `source` 和 `current_version`，再查上游最新状态，不凭印象升级。
- 版本对比可追溯：升级前后都能说明“当前版本是什么”“最新版本是什么”“为什么更新或不更新”。
- 文档与代码同步：工具实现更新后，toolskill、references、版本说明、示例和治理说明必须一起更新。
- 更新结果可维护：后续维护者能从 `resource.yaml` 和 `toolskills/` 直接看懂当前版本与升级依据。

## 核心原则

- **先查来源再改代码**：外部工具的升级入口来自 `resource.yaml -> source`，不要跳过来源直接凭 PATH、缓存或本地安装判断版本。
- **先读当前版本说明**：升级前先读 `resource.yaml -> current_version`，理解当前版本说明、对比标准和版本真值来源。
- **代码更新与知识更新同时完成**：只升级 vendored 代码而不更新 toolskill，视为不完整交付。
- **仍然遵守插件隔离**：默认只改 `tools/<tool_id>/`；若发现必须修改内核，需说明这是通用框架问题，而不是单个工具适配。

## 更新流程

### 1. 读取当前工具元数据

优先查看：

- `tools/<tool_id>/resource.yaml`
- `tools/<tool_id>/toolskills/SKILL.md`
- `tools/<tool_id>/toolskills/references/`
- `tools/<tool_id>/main/` 下 vendored 项目或包装层实现

重点读取字段：

- `source`
- `current_version`
- `governance`

## 2. 按 `source` 查询上游最新更新

按 `source.type` 选择查询方式：

- `git`: 使用 `source.url` 查询上游仓库的最新 tag、release、默认分支、变更日志或 commit。
- `registry`: 使用 `source.url` 或包注册页查询最新发布版本。
- `vendor`: 以 vendored 目录记录的上游信息为主，并补查 `source.url` 的最新状态。
- `internal`: 不查外部版本，改为对比当前仓库中的实现、契约、治理与文档变化。

要求：

- 如果问题涉及“最新”“最近更新”“当前版本是否落后”，必须查上游，而不是只读本地文件。
- 查询时要使用 `source.url` 作为首选入口；`source.ref` 用来理解当前锁定位置。
- 比较时必须参考 `current_version.compare_rule`，而不是自定义一套临时标准。

## 3. 判断是否需要升级

至少形成这三个结论：

- 当前版本：来自 `current_version.summary` 与 `current_version.source_of_truth`
- 上游最新状态：来自 `source.url` 的查询结果
- 升级判断：根据 `current_version.compare_rule` 得出的结论

如果决定不升级，也要说明原因，例如：

- 当前已与上游固定版本一致
- 上游虽有更新，但不兼容当前包装层
- 上游更新只影响当前不使用的子模块

## 4. 升级工具实现

根据工具类型执行：

- vendored 项目：更新 `main/<project>/` 中的版本、文件或固定引用
- 包装层：更新 `main/tool.py` 的启动逻辑、参数适配、路径、版本读取或错误处理
- `resource.yaml`：同步更新 `source.ref`、`current_version`，必要时更新 `permissions`、`governance`、`settings`、`parameters`

## 5. 同步更新 toolskill

升级后必须重新检查 `toolskills/`，不能把它当成与代码无关的静态说明。

至少检查并更新：

- 主 `toolskills/SKILL.md` 中的参数、默认行为、返回结果、失败场景、版本说明、引用索引
- `toolskills/references/` 中与上游版本相关的镜像文档
- 如果上游项目自带 `skills/`、`references/`、`templates/`，要检查这些内容是否也发生变化，并同步镜像或调整索引

这里的编写要求，沿用 `add-tool` 的规范，特别是：

- 不能只保留包装层参数说明，必须保留上游知识入口
- 如果上游 skill / references 已变化，必须同步本地 `references/`
- 主 `toolskills/SKILL.md` 必须继续明确“什么任务读哪份 reference”

收尾前，默认对照：

- `skills/add-tool/references/toolskill-checklist.md`
- `skills/update-tool/references/update-tool-checklist.md`

## 6. 验证

至少验证：

- `resource.yaml` 仍可被解析
- 工具资源仍能被发现与加载
- 若工具进入 Tool 管理，`governance` 仍能生成可见 `tool_family`
- `toolskills/SKILL.md` 与更新后的真实行为一致
- `current_version` 已改成新的当前状态，且对比标准仍正确

## 需要同步更新的字段

升级后通常要重新核对这些字段：

- `source.ref`
- `current_version.summary`
- `current_version.compare_rule`
- `current_version.source_of_truth`
- `parameters`
- `settings`
- `permissions`
- `governance`

## 何时必须更新 toolskill

出现以下任一情况，都必须同步更新 toolskill：

- 命令行参数变化
- 默认行为变化
- 输出结构变化
- 安装或启动方式变化
- 上游 skill / references / templates 变化
- 版本说明或升级依据变化
- Tool 管理中的 family / action 变化

## References

- `references/update-tool-checklist.md`：工具升级交付前的复核清单。
- `skills/add-tool/SKILL.md`：新增或重构工具时的完整规范，更新 toolskill 时沿用其中的要求。
- `skills/add-tool/references/toolskill-checklist.md`：toolskill 编写与知识层完整性检查清单。
