# 添加工具

当任务是新增、迁移、合并或审查 `tools/<tool_id>/` 下的工具时，使用本 skill。

## 标准工具结构

所有标准工具都必须使用下面的资源结构：

```text
tools/
  <tool_id>/
    resource.yaml
    main/
      tool.py
    toolskills/
      SKILL.md
      references/   # 可选
      scripts/      # 可选
      assets/       # 可选
```

约束：

- `tool_id` 使用 `snake_case`。
- 工具根目录只允许 `resource.yaml`、`main/`、`toolskills/`。
- `main/tool.py` 是唯一入口；不要再并列放 `tool.yaml`、`capability.yaml`、`README.md` 之类的旧结构文件。
- 提供给模型的参数契约必须写在 `resource.yaml -> parameters` 中。
- 提供给模型的调用说明必须写在 `toolskills/SKILL.md` 中。

## `resource.yaml` 必填信息

- `schema_version: 1`
- `kind: tool`
- `name`
- `description`
- `protocol: mcp`
- `mcp.transport: embedded`
- `config_namespace`
- `requires`
- `permissions`
- `parameters`
- `exposure`
- `toolskill.enabled`

如果一个工具要承载多个动作，额外在 `resource.yaml` 中写清楚 `governance`：

- `governance.family`: 工具族 id
- `governance.display_name`: 工具族展示名
- `governance.description`: 工具族说明
- `governance.actions`: 每个 action 的 `id`、`label`、`risk_level`、`destructive`、`allowed_roles`

## 标准 `resource.yaml` 模板

```yaml
schema_version: 1
kind: tool
name: example_tool
description: 说明工具做什么，以及什么时候应该调用。
protocol: mcp
mcp:
  transport: embedded
config_namespace: ''
requires:
  tools: []
  bins: []
  env: []
permissions:
  network: false
  filesystem:
    - workspace
parameters:
  type: object
  properties:
    input:
      type: string
      description: 输入内容
  required:
    - input
exposure:
  agent: true
  main_runtime: true
toolskill:
  enabled: true
```

## `main/tool.py` 契约

- 优先导出 `build(runtime)`；需要工作区、服务、配置切片或运行时上下文时使用它。
- 极简单的工具可以直接导出 `execute(...)`。
- 不要自己实现 MCP transport；运行时会把本地 handler 包装成 embedded MCP tool。
- 优先使用 `runtime.workspace`、`runtime.config_slice`、`runtime.services`、`runtime.resource_root`、`runtime.main_root`、`runtime.toolskills_root`，不要硬编码仓库路径。
- 如果工具依赖会话、任务或角色上下文，可在 `execute` 中接收 `__g3ku_runtime`。
- 如果工具持有长生命周期资源，实现 `close()`，便于热重载时回收旧实例。

## `toolskills/SKILL.md` 必须说明

- 何时调用该工具。
- 每个参数的含义、约束和默认行为。
- 返回结果的大致格式。
- 常见失败场景和回退方案。
- 与其他工具的依赖关系或先后顺序。

## 多动作工具约定

如果多个旧工具被整合成一个标准工具：

- 用一个显式参数如 `action` 区分动作。
- 在 `parameters` 中写清楚每个动作需要哪些附加参数。
- 在 `toolskills/SKILL.md` 中逐个说明动作、输入和输出。
- 在 `governance.actions` 中保留细粒度 action 权限，不要只写一个宽泛动作。

## 检查清单

- `resource.yaml` 已声明 `protocol: mcp` 与 `mcp.transport: embedded`。
- `parameters` 与实现逻辑一致，没有幽灵参数。
- `main/tool.py` 已放工具本体，`toolskills/SKILL.md` 已放使用说明。
- 工具目录没有多余文件。
- 改完后至少做一次资源发现或最小执行验证。
