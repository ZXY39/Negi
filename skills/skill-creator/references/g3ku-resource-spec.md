# Negi Resource Spec

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
tool_type: internal
source:
  type: ''
  url: ''
  ref: ''
current_version:
  summary: ''
  compare_rule: ''
  source_of_truth: ''
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
governance:
  display_name: Example Tool
  description: Example governance block.
  risk_level: medium           # low | medium | high
  destructive: false
  allowed_roles:               # 精确白名单；空列表 = 对该资源一律拒绝
    - ceo
    - execution
  # 可选：agent_visible: false / admin_mode: readonly_system（系统只读项，不可编辑角色）
exposure:
  agent: true
  main_runtime: true
toolskill:
  enabled: true
timeout_policy:            # 可选：统一 timeout 合同行为声明（缺省全 false）
  exempt_universal: false  # true = 不套外层最大超时，仅限长时编排类工具
  self_enforced: false     # true = handler 自行消费 timeout_seconds 入参并结构化收尾
  hide_parameter: false    # true = 模型 schema 不注入 timeout_seconds 参数（保底仍适用）
```

规则：

- 工具只有一类：可执行资源。`tool_type` 缺省即 `internal`，写其他值会在发现阶段被拒（错误文案点名两条出路：包成工具、或改成 skill）
- 工具是唯一的治理单位：一个目录 = 一条治理记录 = 一个 `tool_id` = 一个可调用执行器。没有 family / actions 分组层，`governance` 里也不要写分组名；`tool_id` 必须与 `name` 一致被调用与授权
- 既没有 `governance` 块、也不在默认治理表（`main/governance/action_mapper.py` 的 `DEFAULT_TOOL_GOVERNANCE`）里的工具不会出现在 Tool 管理面与 exposure 名单里
- 必须有 `main/tool.py`
- `install_dir` 不是清单字段；发现阶段仍读取它，唯一用途是让陈旧记录被删除时连带收走它的安装目录
- 第三方本体安装在 `externaltools/<tool_id>/`，不得出现在 `tools/` 内
- `timeout_policy` 可选；与 handler 类属性是 OR 语义（清单只能追加豁免/自持/隐藏，不能撤销代码级合同）。`exempt_universal` 只给运行时长天然无界的长时编排工具，且要求强取消实现完整。合同细节见 `docs/architecture/tool-and-skill-system.md`「统一工具 Timeout 合同」

## Toolskill 规则

- 所有工具都要提供 `toolskills/SKILL.md`
- `toolskills/SKILL.md` 需要和 `resource.yaml`、实际实现保持一致；参数、默认行为、输出结构变化时必须同步更新
- 驱动第三方安装的工具必须说明：
  - 何时使用
  - 如何安装
  - 如何更新
  - 如何从 `externaltools/<tool_id>/` 使用
- 纯仓库内实现必须说明：
  - 代码位于 `main/`
  - 无需额外安装
  - 更新方式是修改仓库内实现

## `requires` 声明与可用性探测

`requires.tools / bins / env`（skill 与 tool manifest 通用）不是描述性文字，而是可用性硬探测；资源刷新时运行时逐项校验：

- `bins`：用 `shutil.which` 按**目标机实际可解析的命令名**探测；任一解析不到 → 资源 `available=false` + `missing required bins` 警告，进入待修复车道
- `env`：探测 `os.environ`；任一缺失 → `available=false`
- `tools`：与已注册工具名比对；任一缺失 → `available=false`

声明规则：

- 只写目标机上真实存在的命令：Windows 上通常是 `python` 而不是 `python3`；本机只有 Edge 时不要声明 `chrome`，浏览器能力改挂已注册工具（如 `web-access`）或写进 SKILL.md 外部依赖说明
- 禁止照抄上游 README / pyproject 的依赖名，必须先实测再写
- 可选的重运行时（Go、数据库服务等）不要轻易写进 `requires`——写入即探测，缺失会把整个资源判为待修复；确属必需才写，并在 SKILL.md 里同步给出安装方式

## 创建后复核与修复（三态）

skill 落盘后必须实测 `load_skill_context(skill_id="<skill_id>")`，按返回三态处置：

- **A｜返回正文（`ok=true`）**：可加载，复核通过
- **B｜返回修复指引**（`error="skill_repair_required"`，带 `warnings` / `errors` / `next_actions`，或合同摘要 `repair_required_skills` 列出了它）：已注册但不可用，处于待修复状态。按 warnings 修复——声明与本机不符就改 `resource.yaml` 的 `requires`（对照上一节声明规则）；真实缺依赖就用 `exec` 安装补齐（filesystem 编辑会自动触发资源刷新）。修复后必须重新 load 复核，直到返回 A，或确认剩余缺口属于需用户安装/决策的运行时
- **C｜返回「当前运行时技能未包含 `<id>`」**：不在治理注册表。核查落盘是否成功、资源刷新是否生效、（经 `skill-installer` 安装时）`resource_refresh` / `catalog` 字段是否报错，补齐注册后再复核；不要把这个状态当作待修复或搁置

无法自行解决的缺口，要在交付说明里精确列出：缺什么（具体 bin/env/tool 名）、影响哪个资源、已做过哪些修复动作；禁止笼统写“待复核”。tool 侧的对应规则（`【待修复】` 前缀 / `repair_required=true` / `load_tool_context` 修复车道）见 `main/prompts/shared_repair_required.md`。
