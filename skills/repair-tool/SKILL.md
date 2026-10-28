# 修复工具

当某个工具已经注册，但当前不可用、缺少本地安装、未出现在 callable function tool list，或用户明确要求“修好工具再继续”时，使用本技能。

## 待修复信号

以下任一出现，表示工具已注册且你有调用权限，但当前不能执行真实能力：

- 函数工具描述以 `【待修复】` 开头；
- 调用返回 `repair_required=true` 或 `tool_state="repair_required"`；
- 运行时合同的 `repair_required_tools:` 列出了该 `tool_id`。

它不是目标能力，而是修复入口：把它当正常能力继续排程，或直接重试原始调用，都会把“待修复”报成“已执行”。处置顺序固定为：

1. `load_tool_context(tool_id="<tool_id>")` 读取安装、排障、更新与使用说明。
2. 用 `filesystem_write` / `filesystem_edit` / `filesystem_copy` / `filesystem_move` / `filesystem_propose_patch` / `exec` 补齐缺失依赖、路径、环境变量或注册信息。
3. 刷新并重查该工具的可用性。
4. 只有恢复可用后，才重试原始调用或继续依赖它完成验证。

分型排查见下文「故障分类」，复核口径见「刷新与复核」。

合同 `repair_required_skills:` 列出的是 skill 而不是工具：那类资源按 `main/prompts/shared_repair_required.md`「技能待修复规则」与 skill `writing-skills` 处置，不在本技能范围内。

只有权限、环境和可用工具都已穷尽，才把剩余缺口（具体缺哪个 bin/env/tool、影响哪个资源、已做过哪些修复动作）如实写进交付说明；禁止笼统写“待复核”，也不许把未修复状态报成已完成。

## 目标

- 不要停在“诊断出问题”。
- 优先把工具修到 `available=true`。
- 如果工具应当可调用，还要尽量恢复到当前 turn 的 callable function tool list。
- 所有临时内容放到 `temp/<tool_id>/`。
- 所有第三方工具本体放到 `externaltools/<tool_id>/`。

## 先做什么

1. 先调用 `load_tool_context(tool_id="<tool_id>")` 读取安装、更新、排障和使用说明。
2. 用 `filesystem` 打开这些文件并理解当前契约：
   - `tools/<tool_id>/resource.yaml`
   - `tools/<tool_id>/toolskills/SKILL.md`
   - `tools/<tool_id>/main/tool.py`（若存在）
3. 记录当前症状，至少确认：
   - 是否 `callable`
   - 是否 `available`
   - warnings / errors 是什么
   - 是否“启用但当前未注册到 callable function tool list”

## 修复原则

- 优先修复根因，不要只改提示文案。
- 先尊重 `resource.yaml` 和 `toolskills/SKILL.md` 的契约，再改代码。
- 如果安装位置、版本或运行路径变了，必须同步更新：
  - `resource.yaml`
  - `toolskills/SKILL.md`
  - 必要时 `main/tool.py`
- 不要把第三方工具装进 `tools/`。
- 不要把下载、缓存、解压产物写到 `tmp/`、系统临时目录、桌面、下载目录或用户主目录。
- 工具反复被统一超时打断（`Error executing <tool>: timed out after 600s ...`）时，先判断它是否属于运行时长天然无界的长时编排：是则在 `resource.yaml` 补 `timeout_policy.exempt_universal: true`（豁免只豁免超时、不豁免取消，强取消必须完整）；只是单次调用偏慢的，让调用方显式传更大的 `timeout_seconds` 参数（单位秒、支持小数）。不要为个案放大全局默认 `agents.tool_default_timeout_seconds`。

## 故障分类

### A. 已启用，但没进 callable function tool list

常见表现：

- Tool 管理里显示启用
- `load_tool_context` 仍能看到
- 主 agent 却说“我没有这个工具”

处理方法：

1. 先视为“可修复工具”，不是“彻底不可用”。
2. 检查 `resource.yaml` 里的：
   - `tool_type`
   - `requires.tools`
   - `requires.bins`
   - `requires.paths`
   - `requires.env`
3. 如果工具是 `callable=true` 但依赖缺失，先把依赖修到满足。
4. 如果依赖已经满足，但当前 turn 仍没注册上，继续做“刷新与复核”步骤。

### B. handler 本体损坏

适用条件：

- `tool_type: internal`
- 工具本体在 `tools/<tool_id>/main/`

处理方法：

1. 修复 `main/tool.py`、settings 读取、参数契约或路径解析。
2. 若 availability 取决于本地依赖，补 `requires.paths` / `requires.bins` / `requires.env`。
3. 若工具依赖第三方本体，确认该本体不在 `tools/`，而在 `externaltools/<tool_id>/`。

### C. 第三方本体未安装或位置错误

这一类最容易误判：handler 本身完好，缺的是 `externaltools/` 里的本体，于是表现为"工具在清单里但 `available=false`"。

特征：

- 可调用 handler 在 `tools/<tool_id>/main/`
- 第三方本体应在 `externaltools/<tool_id>/`
- `resource.yaml` 用 `requires.bins` / `requires.paths` 声明那份安装的存在性

处理方法：

1. 按 `toolskills/SKILL.md` 写明的安装目录确认本体在 `externaltools/<tool_id>/`。
2. 若不存在，使用 `exec` 安装到该目录。
3. 下载、缓存、解压中转统一放到 `temp/<tool_id>/`。
4. 若本体落到了 `tools/`、用户目录或全局目录，移回工作区内的正确位置，并同步 handler 里拼路径的那一处。
5. handler 优先从 `externaltools/<tool_id>/` 自动定位本地可执行文件；需要浏览器、模型、运行时等大依赖时同样用工作区内副本，不用用户全局目录。
6. 同步更新 `toolskills/SKILL.md` 的安装 / 更新说明。

## 安装与修复操作

- 使用 `exec` 时：
  - `working_dir` 显式设到工作区内
  - 临时目录使用 `temp/<tool_id>/`
  - 第三方工具目标目录使用 `externaltools/<tool_id>/`
- 若要下载、解压或装包，默认顺序是：
  1. 下载到 `temp/<tool_id>/`
  2. 校验或解压
  3. 安装 / 移动到 `externaltools/<tool_id>/`
- 如果 `toolskills/SKILL.md` 已经给了安装命令，优先遵循它；不要擅自改成全局安装。

## 刷新与复核

修完后不要立刻假设主 agent 已经能调用该工具。

按这个顺序复核：

1. 再次检查本地依赖是否存在：
   - `externaltools/<tool_id>/...`
   - `temp/<tool_id>/...`（如需要）
2. 重新读取 `load_tool_context(tool_id="<tool_id>")`，确认：
   - `available=true`
   - warnings / errors 已消失或明显减少
3. 如果工具依赖 `resource.yaml` 中的 availability 判定，而你这次只改了 `externaltools/`，注意：
   - 资源系统不一定会因为 `externaltools/` 变化而自动刷新
   - 优先使用平台已有的资源刷新能力
   - 如果当前环境没有显式刷新入口，就对 `tools/<tool_id>/resource.yaml` 或 `tools/<tool_id>/toolskills/SKILL.md` 做一次必要且真实的同步更新，促使资源快照刷新
4. 修完后若进入新一轮对话或新一轮工具选择，确认它已重新出现在 callable function tool list。

## 何时升级为 add-tool 或 update-tool

- 如果需要新增工具结构、重做 `resource.yaml`、改变 `tool_type`、补完整的安装 / 更新规范，转用 `add-tool` 或 `update-tool`。
- 如果只是把现有工具从“不可用”修回“可用”，优先留在本技能内完成。

## 完成标准

一个工具只有满足下面条件，才算“修到可用”：

- `load_tool_context` 显示它仍可见
- `available=true`
- 关键 warnings / errors 已消失，或剩余 warning 不影响当前任务
- 若它应当可调用，则在新的工具选择阶段能重新进入 callable function tool list
- 安装目录和临时目录仍符合 `externaltools/` / `temp/` 规范

## Parameter Contract Discipline

- If the repair touches tool arguments, update `resource.yaml -> parameters` first, then keep runtime validation and toolskill examples in sync.
- If the tool has nested object or array arguments, verify `load_tool_context` exposes the full shape and add or refresh a complete JSON example in `toolskills/SKILL.md`.

## Result Delivery Debugging

- When a tool has result-delivery bugs, inspect `tool_result_inline_full` first before assuming the runtime is truncating incorrectly.
- Debug the observed path explicitly:
  - preview-only text returned inline when full text was expected
  - full text returned inline when the caller should have been using `output_ref`
  - missing or stale `output_ref` even though the manifest expects preview-only delivery
- If the tool should return only a preview inline, keep `tool_result_inline_full: false` and verify the runtime still persists the full result behind `output_ref`.
- If the tool should return the full body inline, set `tool_result_inline_full: true` and confirm the caller is no longer depending on preview-only assumptions.
- Keep `resource.yaml`, runtime behavior, and `toolskills/SKILL.md` aligned so preview-only, full text, and `output_ref` do not drift into contradictory states.
