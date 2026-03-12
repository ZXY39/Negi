# 技能创建者 (Skill Creator)

创建或更新 `skills/` 和 `tools/` 下的顶级资源。

## 何时构建

当任务是添加、修订或迁移以下内容时，使用此技能：
- `skills/<skill_id>/` 中的一个技能
- `tools/<tool_id>/` 中的一个工具
- `toolskills/` 中捆绑的工具使用指南

统一资源系统直接从 `resource.yaml` 读取元数据，而不是从 Markdown 的 Frontmatter 读取。

## 标准布局

### 技能布局 (Skill layout)

```text
skills/
  <skill_id>/
    resource.yaml
    SKILL.md
    references/    # 可选
    scripts/       # 可选
    assets/        # 可选
```

规则：
- `resource.yaml` 是唯一的元数据源。
- `SKILL.md` 仅包含普通 Markdown 内容。不要添加 YAML frontmatter。
- 将长篇参考材料放入 `references/`，可执行的辅助程序放入 `scripts/`，输出资产放入 `assets/`。

### 工具布局 (Tool layout)

```text
tools/
  <tool_id>/
    resource.yaml
    toolskills/
      SKILL.md
      references/  # 可选
      scripts/     # 可选
      assets/      # 可选
    main/
      tool.py
      ...
```

规则：
- 工具根目录只能包含 `resource.yaml`、`toolskills/` 和 `main/`。
- `toolskills/` 不需要自己的 `resource.yaml`。
- `main/tool.py` 是固定的入口点。不要随意添加其他的入口点字段。
- 将面向用户的用法指南放入 `toolskills/SKILL.md`，而不是 `README.md`。

## 创建流程

1. **先检查附近的资源**。
   - 复用现有相似技能或工具的 `resource.yaml` 结构、参数样式和措辞。
   - 对于工具，同时检查 `resource.yaml` 和 `main/tool.py`。

2. **仔细选择资源 ID**。
   - 技能 ID 使用小写字母、数字和连字符 (`-`)。
   - 工具 ID 使用小写字母、数字和下划线 (`_`)，以匹配现有的工具命名。
   - 更新现有资源时，请保持 ID 稳定。

3. **在编写内容之前先编写 `resource.yaml`**。
   - 在此处放置触发和发现元数据。
   - 技能：包含 `trigger`、`requires`、`content` 和 `exposure`。
   - 工具：包含 `description`、`parameters`、`permissions`、`requires`、`config_namespace`、`exposure` 和 `toolskill.enabled`。

4. **编写主体内容**。
   - 技能：保持 `SKILL.md` 具有操作性且简洁。
   - 工具：编写 `toolskills/SKILL.md`，务必让另一个 Agent 知道何时调用该工具、每个参数的含义以及要避免的陷阱。

5. **仅在创建或更新工具时实现代码**。
   - `main/tool.py` 必须暴露 `build(runtime)` 或 `execute(...)`。
   - 当工具需要服务、配置切片、工作空间路径或其他运行时对象时，首选 `build(runtime)`。
   - 使用 `runtime.services`、`runtime.workspace`、`runtime.resource_root`、`runtime.main_root` 和 `runtime.toolskills_root` 代替硬编码路径。

6. **验证结果**。
   - 确认资源可以从根目录 `skills/` 或 `tools/` 被发现。
   - 确认工具根目录不包含过时的 `README.md`、`research/`、`tool.yaml` 或 `capability.yaml`。
   - 如果可能，运行针对性的冒烟测试或通过 `ResourceManager` 重新加载路径进行验证。

## 编写准则

### 元数据优先

资源发现依赖于 `resource.yaml`，因此请像编写触发合同一样在其中编写描述。

技能：
- 在 `resource.yaml -> description` 中说明“它做什么”以及“何时使用它”。
- 保持 `trigger.keywords` 最小化且具体。

工具：
- 确保 `description` 足够清晰，以便系统能够正确选择工具。
- 保持参数名称稳定且描述具体。

### 保持 Markdown 简洁

- 仅在 `SKILL.md` 或 `toolskills/SKILL.md` 中放入操作流程。
- 将长篇 schema、示例和供应商文档移动到 `references/` 中。
- 不要跨主体和参考资料重复相同的详细内容。

### 避免遗留模式

不要引入或保留：
- `SKILL.md` 中的 YAML frontmatter
- `capability.yaml`
- `tool.yaml`
- 将根目录下的 `README.md` 作为权威的工具指南
- 将根目录下的 `research/` 作为权威的工具指南

将这些内容迁移到：
- `resource.yaml`
- `SKILL.md`
- `toolskills/SKILL.md`
- `references/`

## 最小模板

### 技能 `resource.yaml`

```yaml
schema_version: 1
kind: skill
name: example-skill
description: 描述该 skill 的作用与适用场景。
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
  org_graph: true
```

### 工具 `resource.yaml`

```yaml
schema_version: 1
kind: tool
name: example_tool
description: 描述该 tool 的作用与调用时机。
config_namespace: ''
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
  org_graph: true
toolskill:
  enabled: true
```

## 更新工作流

更新现有资源时：
- 除非明确要求重命名，否则保留现有 ID。
- 在编辑前，检查当前的 `resource.yaml` 和主体文件。
- 当“能力 (capability)”指代根资源时，迁移过时的术语。
- 保持变更范围限于实际的触发器、工作流或运行时合同。

迁移旧的内置或遗留资源时：
- 将元数据移入 `resource.yaml`
- 将技能内容移入 `SKILL.md`
- 将工具指南移入 `toolskills/SKILL.md`
- 将实现代码移入 `main/tool.py`
- 在新路径运行正常后，删除旧的并行入口点。
