# 添加工具

当任务是新增、迁移或审查 `tools/<tool_id>/` 下的工具时，使用本 skill。

## 核心约束

- 所有工具必须在 `resource.yaml` 中声明 `protocol: mcp`。
- 当前仓库只允许 `mcp.transport: embedded`，由运行时自动挂载为嵌入式 MCP tool。
- 保持热插拔：工具根目录只允许 `resource.yaml`、`main/`、`toolskills/`。
- 提供给模型的参数契约来自 `resource.yaml -> parameters`，它同时也是 MCP 的 `inputSchema`。
- 用户使用说明必须放在 `toolskills/SKILL.md`，不要额外用根目录 `README.md` 代替。

## 标准结构

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
- 根目录不要放额外文件；否则会被资源发现器标记为非标准结构。
- `main/tool.py` 是唯一入口，不要再加并列入口文件约定。

## `resource.yaml` 最小模板

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
  org_graph: true
toolskill:
  enabled: true
```

## `main/tool.py` 契约

- 优先导出 `build(runtime)`；需要配置、服务或工作区信息时使用它。
- 简单工具可以直接导出 `execute(...)`。
- 不要自己实现 MCP transport；运行时会把本地 handler 包装成 embedded MCP tool。
- 优先使用 `runtime.workspace`、`runtime.config_slice`、`runtime.services`，不要硬编码路径。
- 如果工具依赖会话/进度上下文，可在底层 `execute` 中接收 `__g3ku_runtime`。
- 如果工具持有长生命周期资源，实现 `close()`，便于热重载时回收旧实例。

## `toolskills/SKILL.md` 必须说明

- 何时调用该工具。
- 每个参数的含义、约束和默认行为。
- 返回结果的大致格式。
- 常见失败场景和回退方案。
- 与其他工具的依赖关系或先后顺序。

## 热插拔检查清单

- `resource.yaml` 已声明 `protocol: mcp` 与 `mcp.transport: embedded`。
- `parameters` 与实现逻辑一致，没有幽灵参数。
- 工具尽量无全局可变状态；需要状态时放进 `build(runtime)` 返回的实例里。
- 目录结构没有多余文件。
- 变更后至少做一次资源发现或最小执行验证。
