# 修复工具

当某个工具已经注册，但当前不可用、缺少本地安装、未出现在 callable function tool list，或用户明确要求“修好工具再继续”时，使用本技能。

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

### B. 内置工具本体损坏

适用条件：

- `tool_type: internal`
- 工具本体在 `tools/<tool_id>/main/`

处理方法：

1. 修复 `main/tool.py`、settings 读取、参数契约或路径解析。
2. 若 availability 取决于本地依赖，补 `requires.paths` / `requires.bins` / `requires.env`。
3. 若工具依赖第三方本体，确认该本体不在 `tools/`，而在 `externaltools/<tool_id>/`。

### C. 外置工具未安装或安装目录错误

适用条件：

- `tool_type: external`

处理方法：

1. 检查 `install_dir` 是否位于 `externaltools/<tool_id>/`。
2. 若不存在，使用 `exec` 安装到该目录。
3. 下载、缓存、解压中转统一放到 `temp/<tool_id>/`。
4. 若 `install_dir` 指到了 `tools/`、用户目录或全局目录，改回工作区内正确位置。
5. 同步更新 `toolskills/SKILL.md` 的安装 / 更新说明。

### D. 内置包装层 + 外部本体

这是最容易误判的一类，`agent_browser` 就属于这种模式。

特征：

- `tool_type: internal`
- 可调用包装层在 `tools/<tool_id>/main/`
- 第三方本体在 `externaltools/<tool_id>/`

处理方法：

1. 保持包装层是 `internal`，不要硬改成纯 `external`。
2. 把真实第三方工具安装到 `externaltools/<tool_id>/`。
3. 在 `resource.yaml` 用 `requires.paths` 明确本地安装存在性。
4. 在包装层代码里优先从 `externaltools/<tool_id>/` 自动定位本地可执行文件。
5. 若需要浏览器、模型、运行时或其他大依赖，也要优先使用工作区内副本，而不是用户全局目录。

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
