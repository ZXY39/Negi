# 创建 Skill 工作流

本工作流用于创建或更新 `skills/<skill_id>/`。

## 适用场景

- 把一个业务流程、知识域、协作模式、规范清单或标准操作流程做成 G3KU skill
- 迁移现有 skill、提示词集、长文档指南或工作说明到 G3KU 资源结构
- 用户要的是“让模型学会怎么做事”，而不是“新增一个直接执行代码的工具”

## 目标产物

```text
skills/
  <skill_id>/
    resource.yaml
    SKILL.md
    references/    # 可选
    scripts/       # 可选
    assets/        # 可选
```

## 步骤 1：判断是否真的应该做成 skill

优先做成 skill，而不是 tool，当且仅当：

- 核心价值在于知识组织、步骤路由、决策规则、上下文装配
- 输出主要是模型如何思考、如何执行、该看哪些参考资料
- 不需要一个独立的运行时代码入口来处理参数和副作用

如果任务核心是“执行命令、读写文件、访问接口、封装 CLI/SDK”，改走 `references/tool-integration-workflow.md`。

## 步骤 2：定义 skill 边界

至少明确这些问题：

- skill 解决什么问题
- 触发它的典型用户表达是什么
- 输入材料有哪些：文本、链接、代码、PDF、模板、规范、示例输出
- 输出是什么：计划、文档、结构化产物、代码、检查报告、执行建议
- 哪些内容必须放在主 `SKILL.md`
- 哪些内容应拆到 `references/`、`scripts/`、`assets/`

## 步骤 3：搭建目录并命名

- `skill_id` 使用稳定、可读、便于检索的名字
- 先创建目录，再写 `resource.yaml`
- 没有实际资源需求时，不要预创建空的 `references/`、`scripts/`、`assets/`

## 步骤 4：先写 `resource.yaml`

至少补齐：

- `schema_version: 1`
- `kind: skill`
- `name`
- `description`
- `trigger.keywords`
- `requires.tools / bins / env`
- `content.main`
- `exposure.agent` 与 `exposure.main_runtime`

`description` 既要说明 skill 做什么，也要说明在什么情境下使用它。

## 步骤 5：再写主 `SKILL.md`

主文件只保留这些内容：

- skill 的职责说明
- 何时触发 / 何时不要触发
- 核心工作流
- 如何选择 references
- 必要的产出要求与禁止事项

不要把所有长篇背景、模板和案例都塞进主文件。

## 步骤 6：补充 `references/`、`scripts/`、`assets/`

- **`references/`**：放详细场景说明、规范、模板说明、上游技能、示例、FAQ
- **`scripts/`**：放需要稳定复用的校验器、转换器、导出器、辅助脚本
- **`assets/`**：放模板、示例文件、静态资源

优先迁移“真正帮助后续 Codex 实例完成任务的资源”，而不是营销材料和演示截图。

## 步骤 7：检查完整性

完成前至少复核：

- 是否只写了薄薄一层包装，却丢掉了领域工作流
- 是否明确说明了何时读哪份 reference
- 是否把关键上游资料镜像到了本地，而不是只留链接
- 是否删掉了缓存、导出物和无关内容

## 步骤 8：验证

至少做一项：

- 资源发现验证
- 针对性最小运行验证
- 如果同时生成了开放标准 skill，再运行 `scripts/validate.py`、`scripts/security_scan.py`、`scripts/staleness_check.py` 或 `scripts/export_utils.py`
