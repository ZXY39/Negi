# G3KU Resource Spec

统一资源目录：

- `skills/<skill_id>/`
- `tools/<tool_id>/`

## Skill 结构

```text
skills/
  <skill_id>/
    resource.yaml
    SKILL.md
    references/
    scripts/
    assets/
```

## Tool 结构

### Internal tool

```text
tools/
  <tool_id>/
    resource.yaml
    main/
      tool.py
    toolskills/
      SKILL.md
      references/
      scripts/
      assets/
```

### External tool

```text
tools/
  <tool_id>/
    resource.yaml
    toolskills/
      SKILL.md
      references/
      scripts/
      assets/
```

## Tool manifest 关键字段

```yaml
schema_version: 1
kind: tool
name: example_tool
description: What the tool does.
tool_type: internal | external
install_dir: <external only>
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
parameters:
  type: object
  properties: {}
exposure:
  agent: true
  main_runtime: true
toolskill:
  enabled: true
```

规则：

- `tool_type` 缺省时按 `internal`
- `internal` 必须有 `main/tool.py`
- `internal` 禁止写 `install_dir`
- `external` 禁止有 `main/`
- `external` 必须写 `install_dir`
- `external.install_dir` 必须在 `tools/` 之外
- `external` 禁止写 `source.vendor_dir`

## Toolskill 规则

- 所有工具都要提供 `toolskills/SKILL.md`
- `external` 工具必须说明：
  - 何时使用
  - 如何安装
  - 如何更新
  - 如何从 `install_dir` 使用
- `internal` 工具必须说明：
  - 代码位于 `main/`
  - 无需额外安装
  - 更新方式是修改仓库内实现
