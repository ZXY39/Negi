# 接入 Tool 工作流

本工作流用于创建或更新 `tools/<tool_id>/`，把现有能力稳定接入 G3KU 运行时。

## 适用场景

- 封装现有 CLI、脚本、SDK、API、服务端接口或 vendored 仓库
- 新增一个需要参数、权限、设置、运行时代码入口的能力
- 用户要的是“可调用工具”，而不是纯知识型 skill

## 目标产物

当前仓库区分两类工具：

- `internal`：`tools/<tool_id>/main/tool.py` 中有实际运行时代码，可直接进入 callable tool 列表
- `external`：第三方项目安装在 `tools/` 之外，`tools/<tool_id>/` 只负责注册，不包含 `main/`

```text
tools/
  <tool_id>/
    resource.yaml
    main/
      tool.py
      <upstream_project>/   # 可选
    toolskills/
      SKILL.md
      references/           # 可选
      scripts/              # 可选
      assets/               # 可选
```

## 步骤 1：确认为什么要做成 tool

优先做成 tool，当且仅当：

- 需要稳定的参数接口
- 需要显式权限声明
- 需要读写文件、调用外部程序、访问网络或产生副作用
- 需要一个真正可执行的工具入口；`internal` 走 `main/tool.py`，`external` 则通过注册目录 + `install_dir` 接入

如果核心是知识说明、步骤路由和上下文组织，改走 `references/create-skill-workflow.md`。

## 步骤 2：盘点上游能力和运行边界

至少确认：

- 能力来自哪里：CLI、Python 包、REST API、脚本仓库、已有内部逻辑
- 运行时依赖什么：二进制、环境变量、配置文件、网络权限、文件权限
- 是否需要 vendored 固定版本
- 运行时数据写到哪里
- 删除 `tools/<tool_id>/` 后，仓库是否仍应正常运行

## 步骤 3：设计目录与接入方式

- `tool_id` 使用 `snake_case`
- 先决定是 `internal` 还是 `external`
- `internal` 的新代码放在 `main/`
- `internal` 包装已有项目时，把上游项目放进 `main/<project>/`
- `external` 不写 `main/`，而是把第三方项目安装到 `tools/` 外，并在 `resource.yaml` 里声明 `install_dir`
- 面向模型的说明统一写到 `toolskills/SKILL.md`
- 非机密配置写 `resource.yaml -> settings`
- 机密配置保留在 `.g3ku/config.json -> toolSecrets`

## 步骤 4：先写 `resource.yaml`

至少补齐：

- `schema_version: 1`
- `kind: tool`
- `name`
- `description`
- `tool_type: internal | external`
- `protocol: mcp`
- `mcp.transport: embedded`
- `requires`
- `permissions`
- `parameters`
- `exposure`
- `toolskill.enabled`

若工具面向长期维护，优先补齐：

- `source`
- `current_version`
- `settings`
- `governance`

额外约束：

- `internal` 必须有 `main/tool.py`，且不得写 `install_dir`
- `external` 必须写 `install_dir`，不得包含 `main/`
- `external.install_dir` 必须位于 `tools/` 之外

## 步骤 5：实现入口

- `internal`：`main/tool.py` 是唯一入口
- `external`：在外部安装目录维护真实入口，`tools/<tool_id>/` 只保留注册与 toolskill
- 内置入口优先遵循仓库已有 `build(runtime)` 或装载模式
- 处理参数校验、运行时上下文、错误信息和返回结构
- 不要把副作用散落到 G3KU 内核目录

## 步骤 6：写 `toolskills/SKILL.md`

必须说明：

- 什么情况下调用这个工具
- 每个参数是什么意思、有哪些约束
- 返回结果的大致结构
- 常见失败模式与回退方案
- 与其他工具的前后关系
- 如果包装了上游项目，上游版本和来源在哪里

如果上游仓库自带 `skills/`、`references/`、`templates/` 或系统化文档，不要只写包装层参数说明；要把关键知识也落到本地 `toolskills/references/`。

## 步骤 7：做治理与删除安全检查

至少确认：

- 权限声明与真实行为一致
- `settings` 和 `toolSecrets` 分工明确
- `governance` 字段满足工具管理需要
- 删除整个 `tools/<tool_id>/` 后，资源发现和主流程不会崩溃

## 步骤 8：验证

至少做这些中的一部分：

- 资源发现验证
- 加载并最小执行一次工具
- 校验参数映射、权限读取和 `settings` 生效
- 如果包装了上游仓库，确认实际运行的是仓库内固定版本，而不是不可控的全局版本

## 补充说明

涉及复杂工具接入时，再参考 `skills/add-tool/SKILL.md` 里的更严格检查项，特别是 `source`、`current_version`、`governance`、`toolskills` 完整性和删除安全原则。
