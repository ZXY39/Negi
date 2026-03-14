# 添加工具

当任务是新增、迁移、合并或审查 `tools/<tool_id>/` 下的工具时，使用这个规范。

## 标准工具结构

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

- `tool_id` 使用 `snake_case`
- 工具根目录只保留 `resource.yaml`、`main/`、`toolskills/`
- `main/tool.py` 是唯一入口
- `resource.yaml` 负责元数据、参数契约、权限、暴露范围、可选 `settings`
- `toolskills/SKILL.md` 负责调用时机、参数解释、返回结构和失败回退

## resource.yaml 必填字段

- `schema_version: 1`
- `kind: tool`
- `name`
- `description`
- `protocol: mcp`
- `mcp.transport: embedded`
- `requires`
- `permissions`
- `parameters`
- `exposure`
- `toolskill.enabled`

可选字段：

- `settings`: 工具运行时的非机密配置
- `governance`: 多 action 工具的治理定义

## 标准模板

```yaml
schema_version: 1
kind: tool
name: example_tool
description: 说明工具做什么，以及什么情况下应该调用。
protocol: mcp
mcp:
  transport: embedded
requires:
  tools: []
  bins: []
  env: []
permissions:
  network: false
  filesystem:
    - workspace
settings: {}
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

## main/tool.py 契约

- 优先导出 `build(runtime)`
- 极简工具也可以直接导出 `execute(...)`
- 统一使用 `runtime.workspace`、`runtime.tool_settings`、`runtime.tool_secrets`、`runtime.services`
- 不要依赖 `.g3ku/config.json.tools.*`
- 如果需要会话或任务上下文，在 `execute` 中接收 `__g3ku_runtime`
- 如果工具持有长生命周期资源，实现 `close()` 供热重载回收

## toolskills/SKILL.md 必须说明

- 什么时候调用这个工具
- 每个参数的意义、约束和默认行为
- 返回结果的大致格式
- 常见失败场景与回退方案
- 与其他工具的先后关系

## 多 action 工具

如果多个旧工具合并成一个标准工具：

- 用显式参数例如 `action` 区分动作
- 在 `parameters` 里写清每个 action 需要哪些附加参数
- 在 `toolskills/SKILL.md` 中逐个说明 action 的输入输出
- 在 `governance.actions` 中保留细粒度权限，不要只写一个宽泛动作
