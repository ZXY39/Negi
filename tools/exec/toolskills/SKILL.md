# exec

执行 shell 命令并返回结构化结果。`exec` 不会再把未指定的 `working_dir` 绑定到工作区；只有你显式传入 `working_dir` 时，才会切换目录。

以下规则是 `exec` 工具的补充使用说明。

## 使用原则

- 优先使用更专用的工具。只有在 `filesystem` / `content` 不能直接完成任务时再使用 `exec`。
- 需要在特定目录执行时，显式传入绝对 `working_dir`。
- 不要假设 Bash、Unix heredoc 或 `rg` 一定可用；命令语法必须匹配当前节点拿到的 OS / shell 环境信息。
- `exec` 会继承当前 G3KU 进程的 Python 环境；做 Python 验证时，优先使用运行时提供的项目解释器提示，或环境变量 `G3KU_PROJECT_PYTHON`。
- 结果过长时，先看 `stdout_ref` / `stderr_ref`，再用 `content.search` 和 `content.open` 做局部定位。
