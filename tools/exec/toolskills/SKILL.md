# exec

执行 Shell 命令并返回结构化结果。

## 使用原则

- `exec` 只返回摘要和 `stdout_ref` / `stderr_ref`，不会直接返回完整原文。
- 当结果过长时，先用 `content.search` 定位，再用 `content.open` 读取局部。
- 优先使用更专用的工具；只有在 `exec` 是完成任务最直接的方式时才调用它。
