# exec

执行 shell 命令并返回结构化结果。`exec` 不会再把未指定的 `working_dir` 绑定到工作区；只有你显式传入 `working_dir` 时，才会切换目录。

以下规则是 `exec` 工具的补充使用说明。

## 使用原则

- 优先使用更专用的工具。只有在 `filesystem`、`content` 等工具不能直接完成任务时再使用 `exec`。
- 需要在特定目录执行时，显式传入绝对 `working_dir`。
- 不要假设 Bash、Unix heredoc 或 `rg` 一定可用；命令语法必须匹配当前节点拿到的 OS / shell 环境信息。
- `exec` 会继承当前 G3KU 进程的 Python 环境；做 Python 验证时，优先使用运行时提供的项目解释器提示，或环境变量 `G3KU_PROJECT_PYTHON`。
- 结果过长时，先看 `stdout_ref` / `stderr_ref`，再用 `content.search` 和 `content.open` 做局部定位。

## 路径规则

- 所有下载、抓取、缓存、解压、一次性生成文件，都应写到工作区根目录的 `temp/` 下。
- 所有第三方工具本体、运行时、可执行文件、解压结果、配套依赖，都应安装到工作区根目录的 `externaltools/<tool_id>/` 下。
- `tools/` 只保留注册入口、包装脚本、资源清单和 toolskill，不要把真实第三方工具文件放进去。
- 不要在 `tmp/`、`.g3ku/tmp/`、系统临时目录或工作区其他随机目录里下载、解压或安装工具。

## 下载与安装约束

- 做下载前，优先把 `working_dir` 设到 `<workspace>/temp`，或者在命令中显式把输出路径写到 `temp/` 下。
- 做第三方工具安装前，优先把 `working_dir` 设到 `<workspace>/externaltools/<tool_id>`，或者在命令中显式把目标路径写到该目录下。
- 如果只是为安装做中转下载，先下载到 `temp/`，校验后再移动或解压到 `externaltools/<tool_id>/`。
- 任何会把工具装到系统目录、用户目录、全局包目录或 `tools/` 目录的命令，都不应该直接执行。

## 推荐模式

- 下载压缩包：在 `temp/` 下执行，输出到 `temp/<tool_id>/...`
- 解压安装包：从 `temp/` 解压到 `externaltools/<tool_id>/`
- 更新第三方工具：在 `externaltools/<tool_id>/` 内执行，或显式指定该目录为目标路径
- 修改工具注册信息：用 `filesystem` 改 `tools/<tool_id>/resource.yaml`、`tools/<tool_id>/toolskills/`、必要时改包装层代码

## 先停一下再继续的情况

- 命令会把文件写到 `tmp/`、系统临时目录、桌面、下载目录或用户主目录。
- 命令会把第三方工具直接装进 `tools/`。
- 命令默认执行的是系统级全局安装，而不是落在 `externaltools/`。
