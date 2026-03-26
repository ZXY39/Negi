# Skill Creator

为 G3KU 创建、更新、迁移、校验与导出 skill / tool 资源。先判断任务属于“创建 skill”还是“接入 tool”，再按对应中文工作流执行。

## 使用原则

- 先读完用户给出的流程、代码、文档、链接、PDF、示例输入输出和约束，再开始设计。
- 默认优先产出 **G3KU 本地资源**；只有用户明确需要跨平台分发时，才追加开放标准 `SKILL.md` 技能。
- 主 `SKILL.md` 只保留入口、分流规则和关键决策；细节步骤、模板和检查项放到 `references/`。
- 能复用已有脚本、示例和上游资料就复用，不要重复造轮子。
- 当用户要从 ClawHub 搜索、下载、安装或更新现成 skill 时，不在本工作流中处理；转用 `clawhub-skill-manager`。ClawHub 来源的 skill 默认视为第三方项目 skill；若后续要在 G3KU 内使用，通常仍需评估并重写相关内容后，再继续本工作流。
- 当用户明确要“从 GitHub repo/path 安装现成 skill”时，不要先输出迁移方案或等待用户二次确认；优先直接调用 `skill-installer` 工具。只有安装后仍需改造结构、补充资源或重写触发规则时，才继续本工作流。

## 优先转交给 skill-installer

出现下面这些请求时，先用 `skill-installer`，不要把它当成纯技能设计题：

- “安装这个 GitHub skill”
- “把这个 repo/path 里的 skill 接入当前 G3KU”
- “把现成 skill 导入到项目里的 `skills/`”

处理规则：

1. 先调用 `skill-installer`
2. 如果安装结果已经是可发现、可触发的本地 skill，就在此结束
3. 只有当上游 skill 需要拆分、重命名、补 references、补治理信息或改造成更符合 G3KU 的结构时，再继续使用 `skill-creator`

## 先分流

### 路线 A：创建 skill

在这些情况走“创建 skill”工作流：

- 用户要新增或改造 `skills/<skill_id>/`
- 用户要把一个工作流、知识域、操作规范或协作模式做成可复用 skill
- 用户给的是文档、流程说明、模板、现有 skill、FAQ、执行清单、最佳实践，而不是一个要直接调用的运行时工具
- 用户已经通过 `skill-installer` 把 GitHub repo/path 里的现成 skill 拉到本地，但还需要继续做结构适配或能力扩展

执行时：

1. 先读 `references/create-skill-workflow.md`
2. 再读 `references/g3ku-resource-spec.md`
3. 如果用户要跨平台分发，再补读 `references/pipeline-phases.md`、`references/export-guide.md` 与 `references/cross-platform-guide.md`

### 路线 B：接入 tool

在这些情况走“接入 tool”工作流：

- 用户要新增或改造 `tools/<tool_id>/`
- 用户要把现有 CLI、脚本、SDK、服务端接口、vendored 仓库或本地自动化流程封装成 G3KU tool
- 用户需要 `main/tool.py`、`toolskills/SKILL.md`、参数、权限、设置、治理信息和运行时边界

执行时：

1. 先读 `references/tool-integration-workflow.md`
2. 再读 `references/g3ku-resource-spec.md`
3. 如涉及复杂工具接入，再参考 `skills/add-tool/SKILL.md`

## 两条工作流的共同要求

- 先明确真实目标、触发条件、输入来源、输出格式、失败模式和验收标准。
- 保持“简洁主文件 + 丰富 references”的双层结构，不要把所有长文档塞进主 `SKILL.md`。
- 从现有仓库或 skill 迁移时，优先把关键参考资料镜像到本地 `references/`，不要只留下外链。
- 不要留下 `TODO`、空函数、占位字段、`__pycache__` 或一次性导出产物。

## 参考路由

- **创建 skill 主流程**：`references/create-skill-workflow.md`
- **接入 tool 主流程**：`references/tool-integration-workflow.md`
- **G3KU 资源结构与最小模板**：`references/g3ku-resource-spec.md`
- **完整 skill 创建流水线**：`references/pipeline-phases.md`
- **需求澄清与隐式约束提取**：`references/phase1-discovery.md`
- **设计规格与输出定义**：`references/phase2-design.md`
- **单 skill / suite / 组件拆分**：`references/architecture-guide.md`、`references/phase3-architecture.md`
- **触发词、激活描述与检测逻辑**：`references/phase4-detection.md`
- **实现质量与交付标准**：`references/quality-standards.md`、`references/phase5-implementation.md`
- **模板化创建**：`references/templates-guide.md`、`references/templates/README-activation-template.md`
- **多 agent 套件**：`references/multi-agent-guide.md`
- **交互式向导**：`references/interactive-mode.md`
- **跨平台兼容、安装与导出**：`references/cross-platform-guide.md`、`references/export-guide.md`、`references/upstream-README.md`、`references/upstream-SKILL.md`
- **渐进学习 / AgentDB**：`references/agentdb-integration.md`
- **示例 skill**：`references/examples/stock-analyzer/`
- **整合说明**：`references/adaptation-notes.md`

## 可复用脚本

- `python scripts/validate.py <skill_dir>`：校验开放标准 skill 的 frontmatter、命名和结构。
- `python scripts/security_scan.py <skill_dir>`：扫描硬编码密钥、危险模式与敏感文件。
- `python scripts/staleness_check.py <skill_dir>`：检查时间元数据、依赖健康与漂移。
- `python scripts/export_utils.py <skill_dir> [--variant desktop|api]`：导出跨平台包。
- `python scripts/skill_registry.py ...`：维护团队 skill 注册表。
- `bash scripts/install-skill.sh <skill_path_or_url>`、`bash scripts/bootstrap.sh`、`bash scripts/install-template.sh`：安装与引导脚本。

这些脚本主要服务于“生成出来的开放标准 skill”；G3KU 本地资源本身应通过资源发现、最小运行或针对性测试来验证。
