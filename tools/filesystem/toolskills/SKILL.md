# filesystem

统一的工作区文件工具。用一个工具完成读取文件、列出目录、写入文件、精确编辑、删除文件，以及生成可审阅补丁。

## 何时调用

- 当任务需要访问或修改工作区文件时，优先使用 `filesystem`。
- 只读场景使用 `action=read` 或 `action=list`。
- 直接落盘修改使用 `action=write`、`action=edit`、`action=delete`。
- 需要先生成可审阅变更而不是直接改文件时，使用 `action=propose_patch`。

## 参数

- `action`: 必填。可选值为 `read`、`list`、`write`、`edit`、`delete`、`propose_patch`。
- `path`: 必填。目标文件或目录路径；相对路径按 workspace 解析。
- `content`: `action=write` 时必填，表示完整写入内容。
- `old_text`: `action=edit` 或 `action=propose_patch` 时必填，必须在目标文件中唯一匹配。
- `new_text`: `action=edit` 或 `action=propose_patch` 时必填。
- `summary`: `action=propose_patch` 时可选，表示补丁摘要。

## 返回结果

- `read`: 返回文件正文。
- `list`: 返回目录条目列表，每行一个，前缀为 `DIR` 或 `FILE`。
- `write` / `edit` / `delete`: 返回成功或失败文本。
- `propose_patch`: 返回 JSON 字符串，包含 `success`、`artifact`、`summary`、`diff_preview` 等字段。

## 常见失败场景

- 路径不存在，或路径类型不匹配，例如把目录当成文件读取。
- `old_text` 未找到，或出现多次导致无法唯一替换。
- 当前角色没有对应 action 的权限。
- `propose_patch` 依赖补丁工件存储；若运行时没有 `main_task_service.artifact_store`，会返回错误。

## 使用建议

- 只需要查看文件内容时，不要用 `write` 或 `edit`。
- 需要精确小改动时优先用 `edit`；需要完整重写时用 `write`。
- 需要人工审阅、治理审批或保留补丁痕迹时，优先用 `propose_patch`。
