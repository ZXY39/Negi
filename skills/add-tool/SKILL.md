# 添加工具

当任务是新增、迁移、合并、重构或审核 `tools/<tool_id>/` 下的工具资源时，使用本规范。它同时适用于两类场景：

- 编写一个标准的 G3KU 工具。
- 将一个已有的开源项目、CLI、SDK 或 vendored 仓库接入为 G3KU 工具。

## 目标

- 工具是插件：默认只改 `tools/<tool_id>/`，删除该目录并重启后，G3KU 仍应正常运行。
- 元数据完整：`resource.yaml` 必须能说明能力、来源、依赖、权限、参数、暴露范围和运行设置。
- 版本可维护：如果工具来自外部项目，必须记录上游更新地址与固定版本来源，方便后续升级。
- 运行边界清晰：工具的代码、上游仓库、运行时数据和文档位置要可预测、可清理、可迁移。

## 标准工具结构

```text
tools/
  <tool_id>/
    resource.yaml
    main/
      tool.py
      <upstream_project>/   # 可选：接入已有项目时放这里
    toolskills/
      SKILL.md
      references/           # 可选
      scripts/              # 可选
      assets/               # 可选
```

约束：

- `tool_id` 使用 `snake_case`。
- 工具根目录只保留 `resource.yaml`、`main/`、`toolskills/`。
- `main/tool.py` 是唯一入口。
- 如果接入已有项目，源码或二进制包装目录放在 `main/` 下，不要直接散落在工具根目录。
- 运行时数据默认写入 `.g3ku/tool-data/<tool_id>/` 或工作区明确约定的位置，不要写入 G3KU 内核源码目录。

## 设计原则

- **插件隔离**：默认不得修改 `g3ku/` 内核代码来适配单个工具。只有发现通用框架缺陷，且该修复对其他工具也成立时，才单独说明并修改内核。
- **可删除性**：删除 `tools/<tool_id>/` 后，资源发现、启动与主流程仍应正常。
- **版本固定**：接入已有项目时，优先固定到仓库内 vendored 版本、锁文件版本、标签、commit 或 release 版本，而不是依赖不受控的全局安装。
- **结果稳定**：工具返回结构保持稳定。包装 CLI 时，优先返回结构化 JSON 字符串或字典，而不是混杂的人类输出。
- **副作用收敛**：下载、缓存、profile、截图、状态文件等副作用必须有明确落点与清理策略。

## References

- `references/toolskill-checklist.md`：可复用的 toolskill 编写检查清单模板。接入已有开源项目、CLI 或 vendored 仓库时，默认在收尾前对照勾一遍，避免只写了包装层契约而遗漏上游知识层。
- `skills/update-tool/SKILL.md`：当工具已经接入完成、后续需要根据 `source` 查询上游最新更新并同步升级代码与 toolskill 时，转入这个后续流程技能。

## `resource.yaml` 必填字段

- `schema_version: 1`
- `kind: tool`
- `name`
- `description`
- `source`
- `current_version`
- `protocol: mcp`
- `mcp.transport: embedded`
- `requires`
- `permissions`
- `parameters`
- `exposure`
- `governance`（新工具默认必填）
- `toolskill.enabled`

仅以下字段可选：

- `display_name`
- `settings`

说明：

- 对新的用户可见工具，`governance` 默认视为必填，而不是可省略项。
- 只有历史兼容工具，且已经在内核默认映射中声明了治理规则时，才可以暂时不写 `governance`。
- 不要为新工具依赖 `main/governance/action_mapper.py` 里的默认映射；新工具应在自己的 `resource.yaml` 中显式声明治理信息。

## `governance` 字段要求

`governance` 决定工具是否能进入 Tool 管理、是否能被主运行时治理、以及是否能按 action 维度授权。

推荐结构：

```yaml
governance:
  family: <tool_family_id>
  display_name: <管理页显示名>
  description: <管理页描述>
  actions:
    - id: <action_id>
      label: <动作名称>
      risk_level: low | medium | high
      destructive: true | false
      allowed_roles:
        - ceo
        - execution
        - inspection
```

约束：

- `family` 必填，表示治理层中的工具家族 id。
- `actions` 必填，且至少有一个 action。
- 每个 action 都要写 `id`、`label`、`risk_level`、`destructive`、`allowed_roles`。
- `allowed_roles` 只写公开角色：`ceo`、`execution`、`inspection`。
- 如果工具要出现在 Tool 管理页，必须定义 `governance`；否则资源层虽然能发现工具，但治理层不会生成 `tool_family`，管理页也看不到。
- 包装已有开源项目时，也必须补 `governance`，不要只写 `source` 和 `parameters`。

## `source` 字段要求

`source` 用于记录项目来源与后续更新入口，方便维护。所有工具都应填写；如果是内部工具，也要填当前仓库或规范化来源地址。

推荐结构：

```yaml
source:
  type: internal | git | registry | vendor
  url: <项目更新地址>
  ref: <tag / branch / commit / version>
  vendor_dir: <相对工具根目录的路径，可选>
  notes: <维护说明，可选>
```

约束：

- `source.url` 必填，填写后续维护时会去查看的地址，例如 Git 仓库、包注册页、release 页。
- 接入开源项目时，`source.url` 优先填写 git 地址，例如 `https://github.com/vercel-labs/agent-browser.git`。
- `source.ref` 推荐填写当前固定的 tag、commit、release 版本或 lockfile 约束来源。
- `source.vendor_dir` 推荐在 vendored 项目场景下填写，例如 `main/agent-browser`。
- `source.notes` 可写更新命令、构建入口、二进制来源等维护信息。

## `current_version` 字段要求

`current_version` 用于记录工具当前实际固定或发布的版本，方便后续更新时和上游最新版本做对比。

推荐结构：

```yaml
current_version:
  summary: <文本化的当前版本说明>
  compare_rule: <文本化的对比标准>
  source_of_truth: <当前版本依据或真值来源>
```

约束：

- `current_version.summary` 必填，用自然语言说明“当前版本是什么”。如果有明确版本号，直接写在句子里。
- `current_version.compare_rule` 必填，用自然语言说明“后续更新时拿什么比、怎么判断要不要更新”。
- `current_version.source_of_truth` 必填，说明当前版本描述依据什么得到，例如 `package.json.version`、release tag、vendored commit 或仓库内实现。
- 接入开源项目时，`summary` 里应明确写出当前固定版本号，`compare_rule` 里应明确写出对比来源，例如 git tag、release 页、registry 版本或锁文件版本。
- 内部工具如果没有独立上游版本，也不要留空；应明确写出“内部工具”以及更新时应对比的仓库内信号，例如参数契约、治理配置、实现代码和文档变化。

## 标准模板

### 1. 标准自研工具

```yaml
schema_version: 1
kind: tool
name: example_tool
description: 说明工具做什么，以及什么情况下应该调用。
source:
  type: internal
  url: repo://D:/projects/G3KU
  ref: main
current_version:
  summary: 当前为本仓库内维护的内部工具版本。
  compare_rule: 更新时对比本仓库内该工具的参数契约、治理配置、实现代码和文档变化。
  source_of_truth: 以当前仓库中的工具代码、resource.yaml 和 toolskills 文档为准。
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
governance:
  family: example_tool
  display_name: Example Tool
  description: 说明治理层如何理解这个工具。
  actions:
    - id: run
      label: Run Tool
      risk_level: medium
      destructive: false
      allowed_roles:
        - ceo
        - execution
        - inspection
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

### 2. 接入已有开源项目

```yaml
schema_version: 1
kind: tool
name: example_vendor_tool
description: 包装上游 CLI，为 G3KU 提供稳定的工具接口。
source:
  type: git
  url: https://github.com/example-org/example-tool.git
  ref: v1.2.3
  vendor_dir: main/example-tool
  notes: 优先使用仓库内固定版本；不要默认回退到全局 PATH。
current_version:
  summary: 当前固定版本为 v1.2.3。
  compare_rule: 更新时对比上游 release tag、registry 版本或仓库内锁定版本；若上游版本高于 v1.2.3，再评估兼容性后升级。
  source_of_truth: 取自 vendored 仓库当前固定版本，并与 source.ref 对齐。
protocol: mcp
mcp:
  transport: embedded
requires:
  tools: []
  bins: []
  env: []
permissions:
  network: true
  filesystem:
    - workspace
governance:
  family: example_vendor_tool
  display_name: Example Vendor Tool
  description: 包装上游 CLI，为 G3KU 提供稳定的治理入口。
  actions:
    - id: run
      label: Run Vendor Command
      risk_level: medium
      destructive: false
      allowed_roles:
        - ceo
        - execution
settings:
  executable: ''
  repo_root: main/example-tool
  bootstrap_on_first_use: true
parameters:
  type: object
  properties:
    args:
      type: array
      items:
        type: string
      description: 传给上游 CLI 的参数数组。
  required:
    - args
exposure:
  agent: true
  main_runtime: true
toolskill:
  enabled: true
```

## `main/tool.py` 契约

- 优先导出 `build(runtime)`。
- 极简工具也可以直接导出 `execute(...)`。
- `build(runtime)` 可以返回工具实例、可调用对象，或在当前环境不可用时返回 `None`。
- 统一使用这些运行时字段：
  - `runtime.workspace`
  - `runtime.tool_settings`
  - `runtime.tool_secrets`
  - `runtime.services`
  - `runtime.resource_root`
  - `runtime.main_root`
  - `runtime.toolskills_root`
  - `runtime.resource_descriptor`
- 不要依赖 `.g3ku/config.json.tools.*`。
- 如果需要任务或会话上下文，优先从 `kwargs.get("__g3ku_runtime")` 读取。对类方法，避免直接声明双下划线参数名，以免触发 Python name mangling。
- 如果工具持有长生命周期资源，实现 `close()` 供热重载回收。

## 接入已有开源工具的工作流

### 1. 先判断接入方式

- 如果上游本身就是稳定 CLI，优先做“轻包装”而不是重写核心逻辑。
- 如果上游只提供库接口，再决定是直接嵌入，还是先封装成内部 CLI/服务。

### 2. 固定到仓库版本

- 优先使用 vendored 仓库中的固定版本。
- 优先使用仓库自己的锁文件、release 版本、tag 或 commit。
- 除非用户明确要求，否则不要默认使用 PATH 上的全局版本。

### 3. 选择启动顺序

推荐顺序：

- `settings.executable`
- 仓库内已存在的本地二进制
- 根据仓库版本自动下载匹配 release 二进制
- 使用仓库源码在本地构建或运行
- 明确允许时，才回退到全局安装

### 4. 记录维护信息

在 `resource.yaml -> source` 中记录：

- 上游 git 地址或维护地址
- 当前固定版本
- vendored 目录位置
- 构建、下载或更新入口

### 5. 盘点上游知识材料

接入已有开源项目时，在写 `toolskills/SKILL.md` 之前，必须先盘点上游已有资料，而不是只看可执行入口。

至少检查这些位置是否存在可复用内容：

- `skills/`
- `references/`
- `templates/`
- `docs/`
- `examples/`
- 项目根目录的 `README`、命令帮助、使用示例

编写时必须区分两层内容：

- **包装层契约**：G3KU 这个工具自己的参数、返回结果、权限、治理、失败回退、运行边界。
- **上游知识层**：上游项目已经整理好的场景化工作流、专题说明、模板、参考文档。

要求：

- 不要只写包装层契约而忽略上游知识层。
- 如果上游存在 `skills/`，默认要把每个上游 skill 转成当前工具的本地 `references/` 入口，除非有明确理由排除。
- 如果上游 skill 下面还有 `references/`、`templates/` 或示例文件，默认要继续镜像到本地，或至少在本地 reference 中建立稳定跳转路径。
- 主 `toolskills/SKILL.md` 应作为入口和路由文档，不要既遗漏上游知识，也不要把所有上游内容整份塞进主文件。

### 6. 控制运行产物

- 上游源码放在 `main/<project>/`。
- 运行缓存、profile、状态文件等放在 `.g3ku/tool-data/<tool_id>/`。
- 如果会生成可清理产物，工具文档里要说明清理方式。

### 7. 验证删除安全

- 删除工具目录后，G3KU 启动与资源发现不应崩溃。
- 如果工具需要额外 bootstrap，也必须局限在工具目录或工具数据目录内部。

## `toolskills/SKILL.md` 必须说明

- 什么情况下调用这个工具。
- 每个参数的含义、约束和默认行为。
- 返回结果的大致结构。
- 常见失败场景与回退方案。
- 与其他工具的先后关系。
- 如果包装了上游项目，还要说明版本来源、仓库位置、是否会自动下载/构建。
- 如果 `resource.yaml` 里定义了 `current_version`，文档中应说明当前版本说明来自哪里，以及更新时应与什么来源做对比。
- 如果工具声明了 `governance.actions`，文档中还要解释每个 action 覆盖的行为范围。
- 如果上游项目已经自带 `skills/`、`references/`、`templates/` 或系统化文档，主 `toolskills/SKILL.md` 必须说明哪些内容保留在主文件、哪些内容被拆到本地 `references/` 中。
- 如果上游存在多个场景化 skill，主 `toolskills/SKILL.md` 必须给出“按任务类型加载哪份 reference”的索引，而不是只保留一个薄包装说明。
- 不允许只写参数和示例就结束；必须覆盖“包装层契约 + 上游知识入口”两层信息。

## Toolskill 完整性要求

接入已有开源项目时，编写 `toolskills/SKILL.md` 前后都要做一次完整性检查：

- 是否只写了包装器参数，而漏掉了上游项目原有的工作流知识。
- 是否扫描了上游 `skills/`、`references/`、`templates/`、`docs/`、`examples/`。
- 是否把上游 skill 转成了本地 `references/` 入口。
- 是否为每个重要上游 skill 指出了继续展开的本地镜像路径。
- 是否在主 `SKILL.md` 中明确了“什么时候读哪份 reference”。
- 是否把故意不镜像的内容写成了明确的排除决定，而不是遗漏。

## 多 `action` 工具

如果多个旧工具合并成一个标准工具：

- 用显式参数例如 `action` 区分动作。
- 在 `parameters` 里写清每个 `action` 需要哪些附加参数。
- 在 `toolskills/SKILL.md` 中逐个说明 `action` 的输入输出。
- 在 `governance.actions` 中保留细粒度权限，不要只写一个宽泛动作。

## Tool 管理可见性

- Tool 管理页展示的是治理层生成的 `tool_family`，不是原始 `tools/<tool_id>/resource.yaml` 条目本身。
- 因此，新工具如果没有 `governance.family` 和 `governance.actions`，即使资源系统已经发现并能加载，也可能不会出现在 Tool 管理里。
- 只有历史兼容工具，才应依赖默认 family 映射；新接入工具必须自带 `governance`。

## 验收清单

完成工具接入后，至少检查：

- `resource.yaml` 字段完整，尤其是 `source`。
- `resource.yaml` 字段完整，尤其是 `source` 与 `governance`。
- `resource.yaml` 字段完整，尤其是 `source`、`current_version` 与 `governance`。
- `toolskills/SKILL.md` 与工具真实行为一致。
- 如果上游项目自带 `skills/` 或系统化文档，`toolskills/SKILL.md` 已建立入口索引，本地 `references/` 已覆盖关键场景，而不是只剩包装层说明。
- 工具可以被资源发现并成功加载。
- 工具可以被治理层生成 `tool_family`，并在 Tool 管理中可见。
- 包装已有项目时，实际运行的是仓库固定版本，而不是不受控的全局版本。
- 删除 `tools/<tool_id>/` 后，G3KU 仍能正常启动。

## 后续流程

- 当工具首次接入完成后，后续的版本追踪、上游同步、升级判断与 toolskill 联动更新，统一使用 `skills/update-tool/SKILL.md`。
- 不要把“新增工具”和“升级已有工具”混成同一个流程：前者按本 skill 执行，后者按 `update-tool` 执行。
