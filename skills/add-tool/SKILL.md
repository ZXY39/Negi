# 添加工具

当任务是新增、迁移、合并、重构或审核 `tools/<tool_id>/` 下的工具资源时，使用本规范。

本仓库现在区分两类工具：

- `internal`：工具本体位于 `tools/<tool_id>/main/`，可直接进入函数工具列表。
- `external`：第三方项目安装在 `tools/` 之外，`tools/<tool_id>/` 只负责注册，不包含 `main/`。

## 目标

- 统一注册入口仍然是 `tools/<tool_id>/`。
- `resource.yaml` 必须明确工具类型、来源、版本、参数、权限、治理和安装位置。
- 第三方且需要接收上游更新的工具，默认走 `external`，不要把上游项目继续塞进 `tools/<tool_id>/main/`。
- `toolskills/SKILL.md` 除了说明如何使用，还必须说明如何安装、如何更新。

## 标准目录

### 内置工具

```text
tools/
  <tool_id>/
    resource.yaml
    main/
      tool.py
      ...
    toolskills/
      SKILL.md
      references/
      scripts/
      assets/
```

### 外置工具

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

约束：

- `tool_id` 使用 `snake_case`。
- `external` 工具目录下不允许存在 `main/`。
- `external.install_dir` 必须位于 `tools/` 目录之外。
- `internal` 工具禁止填写 `install_dir`。
- `external` 工具禁止填写 `source.vendor_dir`。

## `resource.yaml` 关键字段

所有工具都要写：

- `schema_version: 1`
- `kind: tool`
- `name`
- `description`
- `tool_type: internal | external`
- `source`
- `current_version`
- `requires`
- `permissions`
- `parameters`
- `exposure`
- `toolskill.enabled`

额外规则：

- `tool_type=internal`
  - 必须有 `main/tool.py`
  - 不得写 `install_dir`
- `tool_type=external`
  - 必须写 `install_dir`
  - `install_dir` 允许相对工作区路径，但运行时会解析成绝对路径
  - 不得落在 `tools/` 内
  - 不得写 `source.vendor_dir`

## 选择哪种模式

默认判定：

- 第三方开源项目、CLI、SDK，且后续需要接收上游更新：选 `external`
- 仓库自研工具：选 `internal`
- 明确要把某个外部项目 frozen/vendored 到仓库内长期维护：才选 `internal`

不要把“需要单独安装”的第三方项目做成 vendored 特例，除非用户明确要求把它固定进仓库。

## 新增流程

### A. 外置工具

1. 获取上游下载地址、更新入口和推荐安装方式。
2. 询问用户安装目录；若用户未指定，默认推荐 `.g3ku/external-tools/<tool_id>`。
3. 先把第三方项目安装到 `tools/` 外部。
4. 在 `tools/<tool_id>/` 下创建注册目录，只写：
   - `resource.yaml`
   - `toolskills/`
5. 在 `resource.yaml` 中写清：
   - `tool_type: external`
   - `install_dir`
   - `source.url`
   - `source.ref`
   - `current_version`
6. 在 `toolskills/SKILL.md` 中写清安装、更新、使用方法。

### B. 内置工具

1. 在 `tools/<tool_id>/` 下创建：
   - `resource.yaml`
   - `main/`
   - `toolskills/`
2. 在 `resource.yaml` 中写 `tool_type: internal`。
3. 在 `main/tool.py` 中实现工具。
4. 在 `toolskills/SKILL.md` 中写清如何使用，以及它无需额外安装、更新方式是修改仓库内实现。

## `toolskills/SKILL.md` 要求

### 外置工具必须包含这四段

- `## 何时使用`
- `## 安装`
- `## 更新`
- `## 使用`

必须说明：

- 安装目录如何确定
- 安装命令
- 更新命令
- 如何从 `install_dir` 找到真实可执行入口
- 常见失败场景和回退方法

### 内置工具也要说明安装/更新

写法要明确：

- 无需独立安装，代码位于 `main/`
- 更新方式是修改仓库内实现、参数契约和文档

## `source` 与 `current_version`

- `source.url` 填后续维护时真正会去看的地址。
- `source.ref` 记录当前锁定的 tag / commit / version。
- `external` 工具的 `current_version` 要明确说明当前外部安装对应的版本与比较规则。
- `internal` 工具的 `current_version` 要明确说明它是仓库内工具，并说明更新时看哪些仓库内信号。

## 治理

新工具默认显式填写 `governance`。

推荐约定：

- `internal` 默认 action id 用 `run`
- `external` 默认 action id 用 `use`

## 交付前检查

- 资源能被发现
- `external` 不会进入 callable tool 列表
- `external` 的 `load_tool_context` 能返回安装目录
- `toolskills/SKILL.md` 已同时覆盖“使用 + 安装 + 更新”

## References

- `references/toolskill-checklist.md`
- `skills/update-tool/SKILL.md`
