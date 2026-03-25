# 添加工具

当任务是新增、迁移、合并、重构或审查 `tools/<tool_id>/` 下的工具资源时，使用本规范。

本仓库现在区分两类工具：

- `internal`：工具本体位于 `tools/<tool_id>/main/`，可直接进入 callable tool 列表。
- `external`：第三方工具本体安装在工作区根目录的 `externaltools/<tool_id>/`，`tools/<tool_id>/` 只负责注册和说明，不包含第三方工具本体。

## 目标

- 统一注册入口仍然是 `tools/<tool_id>/`。
- `resource.yaml` 必须明确工具类型、来源、版本、参数、权限、治理和安装位置。
- 需要接收上游更新的第三方工具默认走 `external`，不要把上游项目继续塞进 `tools/<tool_id>/main/`。
- `toolskills/SKILL.md` 除了说明如何使用，还必须说明如何安装、如何更新。
- 临时下载、缓存、解压中转文件统一放在工作区根目录的 `temp/` 下。

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

externaltools/
  <tool_id>/
    ...
```

约束：

- `tool_id` 使用 `snake_case`。
- `external` 工具目录下不允许存在 `main/`。
- `external.install_dir` 必须位于工作区根目录的 `externaltools/<tool_id>/`。
- `external` 的下载包、解压中转目录、安装日志、一次性脚本应放在工作区根目录的 `temp/<tool_id>/`。
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
  - 不得填写 `install_dir`
- `tool_type=external`
  - 必须填写 `install_dir`
  - `install_dir` 必须是工作区根目录下的 `externaltools/<tool_id>/`
  - 不得落在 `tools/` 内
  - 不得填写 `source.vendor_dir`

## 选择哪种模式

默认判定：

- 第三方开源项目、CLI、SDK，且后续需要接收上游更新：选 `external`
- 仓库自研工具：选 `internal`
- 明确要把某个外部项目 frozen / vendored 到仓库内长期维护：才选 `internal`

不要把“需要单独安装”的第三方项目做成 vendored 特例，除非用户明确要求把它固定进仓库。

## 新增流程

### A. 外置工具

1. 获取上游下载地址、更新入口和推荐安装方式。
2. `install_dir` 固定为 `<workspace>/externaltools/<tool_id>`。
3. 若需要下载、解压或校验，先把中间产物放到 `<workspace>/temp/<tool_id>/`。
4. 把第三方工具本体安装到 `<workspace>/externaltools/<tool_id>/`。
5. 在 `tools/<tool_id>/` 下创建注册目录，只写：
   - `resource.yaml`
   - `toolskills/`
6. 在 `resource.yaml` 中写清：
   - `tool_type: external`
   - `install_dir`
   - `source.url`
   - `source.ref`
   - `current_version`
7. 在 `toolskills/SKILL.md` 中写清安装、更新、使用方法，并明确说明：
   - 下载/缓存/解压中转目录在 `temp/<tool_id>/`
   - 真实安装目录在 `externaltools/<tool_id>/`
   - `tools/<tool_id>/` 只负责注册

### B. 内置工具

1. 在 `tools/<tool_id>/` 下创建：
   - `resource.yaml`
   - `main/`
   - `toolskills/`
2. 在 `resource.yaml` 中写 `tool_type: internal`。
3. 在 `main/tool.py` 中实现工具。
4. 在 `toolskills/SKILL.md` 中写清如何使用，以及它无需额外安装；更新方式是修改仓库内实现。

## 异步与不阻塞要求

这是新增工具时的硬性要求：

- 工具入口必须适配当前运行时的异步调用方式，不要把长时间阻塞操作直接塞进事件循环。
- 如果工具要做网络请求、`git` / CLI 调用、压缩解压、大文件复制、目录扫描、数据库重操作或其他阻塞 I/O，必须使用异步客户端，或显式放到后台线程 / 子进程执行。
- 不要在工具的 `execute()` 路径里直接长期占用主线程，例如裸 `subprocess.run(...)`、裸 `urllib`、大批量同步文件遍历或无超时等待。
- 任何外部命令、HTTP 请求、轮询等待都必须有明确超时与失败返回，不能无限 `Working...`。
- 对于超过几秒的任务，优先补充阶段性进度反馈，让用户和前端知道卡在哪个阶段。
- 如果一个工具天然是重任务，应把重活移到后台执行，再返回可轮询结果或明确进度，而不是长时间阻塞当前请求。

## 强取消

这是内置工具和外置工具都必须满足的硬性要求：

- 工具必须支持统一的 cancellation token 或等价取消信号，不能只依赖外层 `task.cancel()`。
- 只要工具内部存在长任务、后台线程、子进程、轮询等待或外部命令，就必须能响应“暂停 / 取消”。
- 对子进程必须保存句柄，并在用户暂停或取消时主动 `terminate()`；必要时在短暂等待后再 `kill()`。
- 对下载、解压、复制、大扫描、大批量写入等长步骤，必须在阶段边界主动检查取消状态并尽快退出。
- 收到暂停 / 取消请求后，优先给用户输出类似“用户已请求暂停，正在安全停止...”的中间状态，而不是长时间无响应。
- 外置工具虽然不直接进入 callable tool 列表，也必须在其真实执行入口里实现同等的强取消能力；`toolskills/SKILL.md` 需要明确写出暂停 / 取消行为。

## `toolskills/SKILL.md` 要求

### 外置工具必须包含这四段

- `## 何时使用`
- `## 安装`
- `## 更新`
- `## 使用`

必须说明：

- `tools/<tool_id>/` 是注册目录，`externaltools/<tool_id>/` 是真实安装目录
- 临时下载和解压中转位于 `temp/<tool_id>/`
- 安装命令
- 更新命令
- 如何从 `install_dir` 找到真实可执行入口
- 常见失败场景和回退方法

### 内置工具也要说明安装 / 更新

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
- 外置工具的 `install_dir` 在 `externaltools/<tool_id>/`
- 外置工具的下载 / 缓存 / 解压中转目录在 `temp/<tool_id>/`
- 工具执行路径不会阻塞事件循环；长耗时步骤已异步化或移到后台线程 / 子进程
- 网络、子进程和等待逻辑已设置明确超时，不会把整个项目卡住
- 工具已接入强取消：支持 cancellation token、子进程显式 terminate / kill、长任务阶段性检查取消状态
- 用户暂停 / 取消时会收到“正在安全停止...”之类的中间反馈

## References

- `references/toolskill-checklist.md`
- `skills/update-tool/SKILL.md`
