# 技能创建器

创建或更新 `skills/` 和 `tools/` 下的顶级资源。

## 何时使用

- 新增或修改 `skills/<skill_id>/`
- 新增或修改 `tools/<tool_id>/`
- 迁移旧式 capability/tool 结构到标准资源结构

## 标准结构

### Skill

```text
skills/
  <skill_id>/
    resource.yaml
    SKILL.md
    references/    # 可选
    scripts/       # 可选
    assets/        # 可选
```

### Tool

```text
tools/
  <tool_id>/
    resource.yaml
    main/
      tool.py
    toolskills/
      SKILL.md
      references/  # 可选
      scripts/     # 可选
      assets/      # 可选
```

规则：

- `resource.yaml` 是唯一元数据入口
- 不再使用 `config_namespace`
- 工具的非机密配置写进 `resource.yaml -> settings`
- 工具机密配置统一保留在 `.g3ku/config.json -> toolSecrets`
- `main/tool.py` 是工具唯一入口
- 面向模型的调用说明写在 `toolskills/SKILL.md`

## 创建流程

1. 先检查相邻资源，复用现有结构和命名模式。
2. 先写 `resource.yaml`，再写主体内容。
3. Tool 资源至少包含 `description`、`parameters`、`permissions`、`requires`、`exposure`、`toolskill.enabled`，按需补 `settings` 和 `governance`。
4. 如果是工具，再实现 `main/tool.py`，优先使用 `build(runtime)`。
5. 最后做一次资源发现或最小执行验证。

## 最小模板

### Skill resource.yaml

```yaml
schema_version: 1
kind: skill
name: example-skill
description: 描述 skill 的作用与适用场景。
trigger:
  keywords: []
  always: false
requires:
  tools: []
  bins: []
  env: []
content:
  main: SKILL.md
  references: references
  scripts: scripts
exposure:
  agent: true
  main_runtime: true
```

### Tool resource.yaml

```yaml
schema_version: 1
kind: tool
name: example_tool
description: 描述 tool 的作用与调用时机。
protocol: mcp
mcp:
  transport: embedded
requires:
  tools: []
  bins: []
  env: []
permissions:
  network: false
  filesystem: []
settings: {}
parameters:
  type: object
  properties: {}
exposure:
  agent: true
  main_runtime: true
toolskill:
  enabled: true
```
