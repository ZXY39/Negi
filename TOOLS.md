# 工具使用说明

工具通过 `tools/*/resource.yaml` 发现。

- `internal` 工具：通过 `main/tool.py` 加载，并进入 callable tool 列表。
- `external` 工具：只注册目录和 `toolskills/SKILL.md`，不加载 `main/tool.py`，也不会进入 callable tool 列表。

外置工具的真实安装位置来自 `resource.yaml -> install_dir`。

## runtime 约定

- `runtime.workspace`: 当前工作区
- `runtime.tool_settings`: 当前工具 `resource.yaml -> settings`
- `runtime.tool_secrets`: `.g3ku/config.json -> toolSecrets[tool_name]`
- `runtime.services`: 运行时注入的服务集合

## exec

- 运行 shell 命令
- 默认使用进程当前目录；只有显式传入 `working_dir` 时才会切换目录，且应提供绝对路径
- 不要假设 Bash、Unix heredoc 或 `rg` 一定可用；命令语法必须匹配当前节点拿到的 OS / shell 环境信息
- 命令会继承当前 G3KU 进程的 Python 环境；如需调用 Python，优先使用运行时提供的项目解释器提示，或读取 `G3KU_PROJECT_PYTHON`
- 结果过长时，优先查看 `stdout_ref` / `stderr_ref`，再用 `content.search` 和 `content.open` 做局部定位
- 更完整的 `exec` 使用细则见 `tools/exec/toolskills/SKILL.md`
- `timeout`、`PATH` 补充、`restrict_to_workspace` 和 `enable_safety_guard` 都来自 `tools/exec/resource.yaml -> settings`
- 默认不启用命令安全守卫；如需恢复拦截，可在 `tools/exec/resource.yaml -> settings` 中设置 `enable_safety_guard: true`

## filesystem

- 统一文件工具，支持 `read`、`list`、`write`、`edit`、`delete`、`propose_patch`
- `path` 必须是绝对本地路径；`artifact:` / content 引用不是 filesystem 路径
- `restrict_to_workspace` 来自 `tools/filesystem/resource.yaml -> settings`
- `write` 写入后可按 `write_validation_*` 配置立即执行校验；校验失败时会回滚：新建文件会删除，已有文件会恢复原内容
- `edit` 支持两种互斥模式：`old_text + new_text` 文本替换，或 `start_line + end_line + replacement` 行号区间替换
- `edit` 修改后可按 `edit_validation_*` 配置立即执行校验；校验失败时会自动回滚到修改前内容
- 若实际执行了校验命令，`write` / `edit` 的成功消息会附带 `validated by N command(s)` 后缀
- `propose_patch` 生成补丁工件，不直接修改文件

## content

- 统一的大内容导航工具，支持 `describe`、`search`、`open`、`head`、`tail`
- 读取 `artifact:` 这类外部化内容时，使用 `ref`
- `path` 只接受绝对本地路径；`restrict_to_workspace` 来自 `tools/content/resource.yaml -> settings`

## memory_search

- 搜索长期记忆
- 默认结果数来自 `tools/memory_search/resource.yaml -> settings.default_limit`

## 配置原则

- 非机密工具配置放到各自 `resource.yaml -> settings`
- 机密工具配置统一放到 `.g3ku/config.json -> toolSecrets`
- 不再使用 `.g3ku/config.json -> tools`
