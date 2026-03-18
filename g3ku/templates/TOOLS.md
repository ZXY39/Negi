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
- 相对路径和默认 `working_dir` 基于 workspace；超时、`PATH` 补充和 `restrict_to_workspace` 都来自 `tools/exec/resource.yaml -> settings`
- 仍然带有危险命令拦截

## filesystem

- 统一文件工具，支持 `read`、`list`、`write`、`edit`、`delete`、`propose_patch`
- 相对路径基于 workspace；`restrict_to_workspace` 来自 `tools/filesystem/resource.yaml -> settings`
- `propose_patch` 生成补丁工件，不直接修改文件

## content

- 统一的大内容导航工具，支持 `describe`、`search`、`open`、`head`、`tail`
- 相对路径基于 workspace；`restrict_to_workspace` 来自 `tools/content/resource.yaml -> settings`

## memory_search

- 搜索长期记忆
- 默认结果数来自 `tools/memory_search/resource.yaml -> settings.default_limit`

## 配置原则

- 非机密工具配置放到各自 `resource.yaml -> settings`
- 机密工具配置统一放到 `.g3ku/config.json -> toolSecrets`
- 不再使用 `.g3ku/config.json -> tools`
