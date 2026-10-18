# 添加工具

当任务是新增、迁移、合并、重构或审查 `tools/<tool_id>/` 下的工具资源时，使用本规范。

本仓库的工具只有一类：**可执行资源**。

- 工具本体是 `tools/<tool_id>/main/tool.py` 的 handler，注册后即可进入 callable tool 列表。
- 第三方程序（上游项目、CLI、SDK、MCP 服务）安装在 `<workspace>/externaltools/<tool_id>/`，由某个工具的 handler 或 `exec` 驱动；`tools/<tool_id>/` 仍是唯一注册入口，不存放第三方本体。

外部能力**本身是一个 MCP server**（stdio / http）时，走 `references/mcp-client-tool.md`：产出一个由 handler 当 MCP 客户端的工具，远端本体仍安装在 `externaltools/<tool_id>/`。运行时不新增传输类型。

## 目标

- 统一注册入口仍然是 `tools/<tool_id>/`。
- `resource.yaml` 必须明确来源、版本、参数、权限、治理与结果交付方式。
- 需要接收上游更新的第三方能力，更新的是 `externaltools/<tool_id>/` 里的安装与它的 handler；不要把上游项目 vendored 进 `tools/<tool_id>/main/`。
- `toolskills/SKILL.md` 除了说明如何使用，还必须说明如何安装、如何更新。
- 临时下载、缓存、解压中转文件统一放在工作区根目录的 `temp/` 下。

## 标准目录

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

externaltools/          # 仅当这个工具驱动一份第三方安装时
  <tool_id>/
    ...
```

约束：

- `tool_id` 使用 `snake_case`。
- `tools/<tool_id>/` 下只允许 `resource.yaml`、`main/`、`toolskills/`；第三方本体出现在这里会被发现阶段拒绝。
- 第三方本体安装在 `<workspace>/externaltools/<tool_id>/`，永远不落进 `tools/`（`exec` 侧同一判据，写错会被直接拒）。
- 下载包、解压中转目录、安装日志、一次性脚本放在 `<workspace>/temp/<tool_id>/`。
- `install_dir` 不是清单字段。发现阶段仍会读取它，唯一用途是让一条陈旧记录被删除时连带收走它指向的安装目录；新写的清单不要带。

## `resource.yaml` 关键字段

所有工具都要写：

- `schema_version: 1`
- `kind: tool`
- `name`
- `description`
- `tool_type: internal`（唯一合法值，其他值在发现阶段被拒）
- `source`
- `current_version`
- `requires`
- `permissions`
- `parameters`
- `exposure`
- `toolskill.enabled`

按需声明：

- `timeout_policy` —— 工具在统一 timeout 合同下的行为（三个键都是布尔，缺省视为 false）：
  - `exempt_universal: true`：完全不套外层最大超时（全局默认 600s）。仅限运行时长天然无界的长时编排工具（例如一次调用内要跑完整个子任务流水线）。豁免只豁免超时、不豁免取消：必须完整实现强取消。
  - `self_enforced: true`：handler 自己消费统一注入的 `timeout_seconds` 入参并负责结构化收尾（终止进程树、关闭会话、抢救部分输出）。声明后 handler 必须真的实现收尾，否则该工具处于无任何时限状态。
  - `hide_parameter: true`：模型 schema 不注入 `timeout_seconds` 参数，机械保底仍适用。用于瞬时完成的内部协议类工具。
  - 清单声明与 handler 类属性是 OR 语义：清单只能追加这些 opt-in 行为，不能撤销代码级已声明的合同；handler 已在代码里设置标志的，清单不必重复声明。
- `recovery_policy` —— 进程被硬杀或暂停打断一批工具调用后，恢复车道能不能替模型重放这一次调用（只有一个键 `rerun_safe`，布尔，缺省视为 false）：
  - `rerun_safe: true`：重复这次调用**可证明**无危害——纯读、写入按 `tool_call_id` 归键、或重跑会被持久状态闸门拒掉。三选一并写进声明处的注释。
  - 保持缺省：会另起一行记录、再发一条消息、再排一个定时器、再建一个节点的工具一律不声明；恢复时把裁定交回模型，运行时不代劳重放。想让它可重放，先给写入加幂等键，再来加声明。
  - 内置工具（无清单目录的那批）只能写 handler 类属性 `rerun_safe = True`；两条车道同样 OR。`exec` / `shell` 与 filesystem 写族由代码判档，清单声明放不轻它们。合同正文归 `docs/architecture/tool-hydration-and-callable-chain.md`「工具可重放声明」。

额外规则：

- 必须有 `main/tool.py`，且它能被装载成工具对象。
- `install_dir` 只作为陈旧记录的读取痕迹存在，新写的清单不要带。
- 第三方本体落在 `<workspace>/externaltools/<tool_id>/`，不在 `tools/` 内。

## 一份能力该长成什么样

- 仓库自研逻辑：直接写 handler。
- 第三方开源项目、CLI、SDK，后续要吃上游更新：写一个 handler 去驱动 `externaltools/<tool_id>/` 里的安装；更新时重装那份安装，仓库里只留 handler 和说明。
- 外部能力本身是 MCP server（stdio / http）：按 `references/mcp-client-tool.md` 的粒度判据决定包几个工具。
- 只有"该怎么用"而没有"要执行什么"：那是 skill，不是工具。

不要把需要单独安装的第三方项目 vendored 进仓库，除非用户明确要求把它固定进仓库。

## 新增流程

1. 在 `tools/<tool_id>/` 下创建：
   - `resource.yaml`
   - `main/`
   - `toolskills/`
2. 在 `resource.yaml` 中写 `tool_type: internal`、参数契约、权限、治理与结果交付方式。
3. 在 `main/tool.py` 中实现 handler。
4. 若这个工具驱动一份第三方安装：
   - 先取上游下载地址与更新入口，写进 `source.url` / `source.ref` / `current_version`。
   - 下载、解压、校验的中间产物放 `<workspace>/temp/<tool_id>/`，本体安装到 `<workspace>/externaltools/<tool_id>/`。
   - 在 `toolskills/SKILL.md` 里写清安装、更新，以及如何从安装目录找到真实可执行入口。
5. 在 `toolskills/SKILL.md` 中写清如何使用；纯仓库内实现要写明它无需额外安装、更新方式是修改 `main/`。

## 异步与不阻塞要求

这是新增工具时的硬性要求：

- 工具入口必须适配当前运行时的异步调用方式，不要把长时间阻塞操作直接塞进事件循环。
- 如果工具要做网络请求、`git` / CLI 调用、压缩解压、大文件复制、目录扫描、数据库重操作或其他阻塞 I/O，必须使用异步客户端，或显式放到后台线程 / 子进程执行。
- 不要在工具的 `execute()` 路径里直接长期占用主线程，例如裸 `subprocess.run(...)`、裸 `urllib`、大批量同步文件遍历或无超时等待。
- 任何外部命令、HTTP 请求、轮询等待都必须有明确超时与失败返回，不能无限 `Working...`。
- 对于超过几秒的任务，优先补充阶段性进度反馈，让用户和前端知道卡在哪个阶段。
- 如果一个工具天然是重任务，应把重活移到后台执行，再返回可轮询结果或明确进度，而不是长时间阻塞当前请求。
- 如果一次工具调用天然就是长时编排（要等多个子任务 / 子流水线跑完，无法后台化），在 `resource.yaml` 声明 `timeout_policy.exempt_universal: true`，避免被统一 600s 保底中途打断；豁免只豁免超时、不豁免取消，强取消仍必须完整实现。

## 强取消

这是所有工具都必须满足的硬性要求：

- 工具必须支持统一的 cancellation token 或等价取消信号，不能只依赖外层 `task.cancel()`。
- 只要工具内部存在长任务、后台线程、子进程、轮询等待或外部命令，就必须能响应“暂停 / 取消”。
- 对子进程必须保存句柄，并在用户暂停或取消时主动 `terminate()`；必要时在短暂等待后再 `kill()`。
- 对下载、解压、复制、大扫描、大批量写入等长步骤，必须在阶段边界主动检查取消状态并尽快退出。
- 收到暂停 / 取消请求后，优先给用户输出类似“用户已请求暂停，正在安全停止...”的中间状态，而不是长时间无响应。
- 驱动第三方安装的工具，其真实执行入口（handler 起的子进程、远端会话）也要实现同等强取消能力；`toolskills/SKILL.md` 需要明确写出暂停 / 取消行为。

## `toolskills/SKILL.md` 要求

### 驱动第三方安装的工具必须包含这四段

- `## 何时使用`
- `## 安装`
- `## 更新`
- `## 使用`

必须说明：

- `tools/<tool_id>/` 是注册与 handler，`externaltools/<tool_id>/` 是第三方本体所在
- 临时下载和解压中转位于 `temp/<tool_id>/`
- 安装命令
- 更新命令
- 如何从 `externaltools/<tool_id>/` 找到真实可执行入口
- 常见失败场景和回退方法

### 纯仓库内工具也要说明安装 / 更新

写法要明确：

- 无需独立安装，代码位于 `main/`
- 更新方式是修改仓库内实现、参数契约和文档

## `source` 与 `current_version`

- `source.url` 填后续维护时真正会去看的地址。
- `source.ref` 记录当前锁定的 tag / commit / version。
- 驱动第三方安装的工具，`current_version` 要明确说明当前安装对应的版本与比较规则。
- 纯仓库内实现的工具，`current_version` 要说明更新时看哪些仓库内信号。

## 治理

新工具默认显式填写 `governance`。

推荐约定：

- 默认 action id 用 `run`

## 交付前检查

- 资源能被发现，handler 能被装载成工具对象
- `toolskills/SKILL.md` 已同时覆盖“使用 + 安装 + 更新”
- 驱动第三方安装的工具：本体在 `externaltools/<tool_id>/`，下载 / 缓存 / 解压中转目录在 `temp/<tool_id>/`
- `load_tool_context(tool_id="<tool_id>")` 能取到参数契约与 toolskill 正文
- 工具执行路径不会阻塞事件循环；长耗时步骤已异步化或移到后台线程 / 子进程
- 网络、子进程和等待逻辑已设置明确超时，不会把整个项目卡住
- 长时编排类工具已在 `resource.yaml` 声明 `timeout_policy.exempt_universal`；自持收尾类已声明 `self_enforced` 且 handler 确实消费 `timeout` 入参
- 恢复车道档位已判定：只有能说出"重复这次调用为什么无害"的工具才声明 `recovery_policy.rerun_safe`（或 handler `rerun_safe = True`），其余保持缺省
- 工具已接入强取消：支持 cancellation token、子进程显式 terminate / kill、长任务阶段性检查取消状态
- 用户暂停 / 取消时会收到“正在安全停止...”之类的中间反馈

## References

- `references/toolskill-checklist.md`
- `references/mcp-client-tool.md`
- `skills/update-tool/SKILL.md`

## Parameter Contract Discipline

- Treat `tools/<tool_id>/resource.yaml -> parameters` as the single source of truth for the callable schema.
- Every top-level and nested parameter should have a precise `description`.
- If the tool uses nested objects or arrays, `toolskills/SKILL.md` must include at least one complete JSON example call.
- Do not rely on prose-only toolskill text to communicate required fields or enum values.

## Result Delivery Flag

- Add `tool_result_inline_full` to the manifest when deciding how tool output is delivered back to the model.
- Default it to `false`. That is the safe baseline for tools that should return preview text plus structured `output_ref` access to full results.
- Opt in to `tool_result_inline_full: true` only when the tool's full result is intentionally small, stable, and should be delivered inline instead of forcing preview-only or follow-up fetch behavior.
- Document the expected delivery mode in `toolskills/SKILL.md` so callers know whether they should expect preview text, full inline text, or an `output_ref` to inspect later.
