# 资源类 Skill 准确性修复计划

日期：2026-11-09
状态：待确认（未开始改文件）
范围：只改 agent-facing 的 skill / toolskill 文档；不改运行时行为。

## 1. 背景

2026-11-09 的线上排障发现：新增工具目录后运行中进程不装载，重启才生效；模型当时读过 `update-tool`，并按 skill 的验证清单自查为"全绿"，但实际调不动。

对照代码核实后确认，资源类 skill 存在三类问题：

- 写错的契约（照做会做出不可用或不可见的资源）；
- 只验证读路径（`load_tool_context` 成功被当成"工具可用"）；
- 陈旧描述（旧字段、旧行号、旧入口、旧状态文本）。

本计划按"文档对齐代码"处理。代码侧的机制缺陷（执行进程不热刷新、worker 不接收 reload、`requires.tools` 对 tool 不探测）不在本次改动内，另立计划。

## 2. 总原则

1. 只改文档，不改 `g3ku/`、`main/` 代码；任何行为修正另开计划。
2. 不碰 `Parameter Contract Discipline` 与 `Result Delivery Flag` 两节：`tests/resources/test_tool_resource_admin_api.py:4452-4486` 按关键词钉住了这些段落，改动会红。
3. 每处修复必须同时给出"文档怎么写"和"代码事实在哪"，不留无法复核的结论。
4. 修完后统一跑 smoke 与相关用例，并逐条 grep 禁用词。

## 3. 改动清单

### 阶段一：高危（照做会产出不可用/不可见资源）

#### F1 `find-skills` 的安装路径

- 文件：`skills/find-skills/SKILL.md`（第 21-32、96-104、126-142 行）
- 现状：整篇教模型用 `npx skills add <owner/repo@skill> -g -y` 安装；`-g` 是用户级全局目录。
- 代码事实：
  - Negi 只扫 `<workspace>/skills`（`g3ku/resources/manager.py:824`）；
  - 发现要求每个 skill 目录存在 `resource.yaml`（`g3ku/resources/registry.py:91-93`）；
  - 开放标准 skill 由 `skill-installer` 安装并自动补 manifest（`tools/skill-installer/main/tool.py:304-315`）。
- 改法（默认方案，整体改写而非删除）：
  1. 保留"发现"部分：skills.sh 榜单、来源信誉、质量判断；
  2. 删掉所有 `npx skills add` / `npx skills init` 指令；
  3. 安装一律改为：先给出 GitHub `owner/repo/path`，再调用 `skill-installer`，按它的三态复核；
  4. 补一句：`-g` / 用户级目录安装不会被 Negi 发现；
  5. `npx skills init` 的场景改为转 `skill-creator`；
  6. 更新 `skills/find-skills/resource.yaml` 的 description，点明"经 skill-installer 安装"。
- 验收：全文不再出现 `npx skills add`；安装段落包含 `skill-installer`、`resource.yaml`、三态复核。
- 风险：这是行为改写，不只换词。保留"发现"可降低用途收窄的风险。

#### F2 资源规范里的 "External tool" 模板

- 文件：`skills/skill-creator/references/g3ku-resource-spec.md:37-48`
- 现状：给出一个只有 `resource.yaml + toolskills/`、没有 `main/tool.py` 的 "External tool" 模板，与同文第 104 行"必须有 main/tool.py"矛盾。
- 代码事实：
  - 只有一类工具；`main/tool.py` 缺失即 `available=false`（`g3ku/resources/registry.py:257-259`）；
  - `tool_type: external` 会被判错（`g3ku/resources/registry.py:232-236`）；
  - 第三方本体位置是约定，不是第二个类型（`registry.py:239-248` 只校验 `install_dir`）。
- 改法：
  1. 删除 "External tool" 小节标题与模板；
  2. 换成"驱动第三方安装的工具"模板：`main/tool.py` + `toolskills/`，本体在 `externaltools/<tool_id>/`；
  3. 留一句反例说明：不存在 external 类型，写 `tool_type: external` 会被拒绝。
- 验收：全文不再出现 "External tool"；模板内含 `main/tool.py`；与第 104 行一致。

#### F3 `add-tool` 的治理段

- 文件：`skills/add-tool/SKILL.md:175-181`
- 现状：写"新工具默认显式填写 governance；默认 action id 用 `run`"；主文档无任何 `allowed_roles` 说明。
- 代码事实：
  - 没有 family/actions 层（`skills/skill-creator/references/g3ku-resource-spec.md:102`）；
  - 显式 `governance` 但缺 `allowed_roles` → 空集 → 全拒（`main/governance/resource_bridge.py:98-102`）；
  - 无显式 governance 且不在 `DEFAULT_TOOL_GOVERNANCE` → 不生成治理记录（`main/governance/resource_bridge.py:59-63`）。
- 改法：
  1. 删掉 "默认 action id 用 run"；
  2. 写入现行治理块示例（display_name / description / risk_level / destructive / allowed_roles）；
  3. 明写两条硬规则：`allowed_roles` 空或缺失 = 对该工具一律拒绝；未声明 governance 且不在默认表的工具不进管理面；
  4. 给出新建工具的推荐默认（`ceo` + `execution`，与 MCP reference 的"只给 ceo"二选一，统一为一处口径，另一处引用）。
- 验收：`add-tool` 主文档出现 `allowed_roles` 与"空=全拒"；不再出现 action id。

#### F4 技能加载的第三种状态写错

- 文件：
  - `tools/skill-installer/toolskills/SKILL.md:73-82`
  - `skills/skill-creator/references/g3ku-resource-spec.md:138-146`
- 现状：把 C 态写成"返回「当前运行时技能未包含 `<id>`」"，并把"未注册"当成唯一解释。
- 代码事实（`main/service/runtime_service.py:10029-10039`）：
  - 注册表无记录 → `error="skill_deleted"`（"Skill 已删除…"）；
  - 有记录但角色不可见 → `error="skill_not_permitted"`（"权限不允许…"）；
  - 待修复 → `error="skill_repair_required"`（`runtime_service.py:13321-13335`）；
  - 成功 → `ok=true`。
- 改法：
  1. 把 C 态拆成 `skill_deleted` 与 `skill_not_permitted` 两态；
  2. 删除对不存在文案的引用；
  3. 分别给出动作：deleted → 核查落盘与 `resource_refresh` / `catalog`；not_permitted → 核查 RBAC 角色，不得重装；
  4. 保留 B 态（repair）不变。
- 验收：两份文档的态名与代码逐字一致；不再出现"当前运行时技能未包含"。

#### F5 `add-tool` 夸大的目录约束

- 文件：
  - `skills/add-tool/SKILL.md:40-46`
  - `skills/skill-creator/references/tool-integration-workflow.md:18-30`（可选 `main/<upstream_project>/` 一行）
- 现状：写"第三方本体出现在 `tools/` 会被发现阶段拒绝"；同时另一份把 `<upstream_project>/` 列在 `main/` 下作为可选项。
- 代码事实：
  - 根目录多余条目只会得到 `unexpected root entries` 警告（`g3ku/resources/registry.py:250-256`）；
  - 硬拒只有两种：`install_dir` 指向 `tools/` 内（`registry.py:239-246`）、`tool_type != internal`（`registry.py:232-236`）；
  - `main/` 下内容不受限（`g3ku/agent/tools/filesystem_mutation.py:476-481`）。
- 改法：
  1. 把"会被发现阶段拒绝"改成"只会得到 warning，仍会注册"，并列出两种真正会被拒的形态；
  2. `tool-integration-workflow.md` 删掉 `main/<upstream_project>/` 这一可选行，避免与"本体在 externaltools"冲突；
  3. 统一口径：第三方本体放 `externaltools/<tool_id>/`，`main/` 只放 handler。
- 验收：三处（add-tool 主文、toolskill-checklist、tool-integration-workflow）口径一致。

#### F6 "注册文件写完即触发资源刷新、无需重启"的适用范围

- 文件：`skills/add-tool/references/mcp-client-tool.md:19`
- 现状：无条件承诺"无需重启"，并引用已失效的 `filesystem_mutation.py:492` / `:532`。
- 代码事实：
  - `filesystem_write/edit/copy/move/delete` → `refresh_resource_paths` 定向刷新（`filesystem_mutation.py:579/734/901/965/1019 → 1549-1557`）；
  - `exec` → `refresh_changed_resources`，首拍只记基线（`main/service/runtime_service.py:10638-10641`）；
  - web 与 worker 各一份 ResourceManager，各刷各的。
- 改法：
  1. 把这句限定为"经 `filesystem_*` 写注册文件"；
  2. 增加一段"exec 安装"的差异：走指纹比对、首拍只记基线、web/worker 分离；
  3. 删除失效行号，改为引用函数名；
  4. 在 `add-tool` 新增一小节"安装后如何让它生效"（定向刷新 / 显式 reload / worker 单独刷新），供 `update-tool` 与 `repair-tool` 引用，避免三份各写一套。
- 验收：全文不再出现"无需重启"的无限定表述；新增的激活小节被另外两份 skill 引用。

### 阶段二：中危（验证假阳性与排障误导）

#### F7 `requires.tools` 对 tool 不探测

- 文件：
  - `skills/skill-creator/references/g3ku-resource-spec.md:124-136`
  - `skills/repair-tool/SKILL.md:69-79`
- 现状：规范说 `requires.tools/bins/env` 对 skill 与 tool 通用；repair-tool 让模型检查 tool 的 `requires.tools`。
- 代码事实：
  - skill：tools / bins / env 三项都探测（`g3ku/resources/registry.py:64-74`）；
  - tool：只探测 bins / paths / env（`registry.py:313-323`），`requires.tools` 被解析但从未使用（`registry.py:204` 仅存储）。
- 改法：
  1. 规范改为分别列两套探测项：skill = tools/bins/env；tool = bins/paths/env；
  2. 明写 `requires.tools` 对 tool 当前不生效；
  3. repair-tool §A 的检查项把 `requires.tools` 换成 `requires.paths`，并加一句"tool 的 requires.tools 不参与可用性判定"。
- 验收：规范与 repair-tool 的探测项与 `registry.py:313-323` 一致。

#### F8 验收清单只验读路径

- 文件：
  - `skills/update-tool/references/update-tool-checklist.md:31-36`
  - `skills/add-tool/references/toolskill-checklist.md:61-65`
  - `skills/update-tool/SKILL.md:92-98`
  - `skills/skill-creator/SKILL.md:64`
- 现状：验收都止于 `load_tool_context` 返回契约与正文。
- 代码事实：`load_tool_context` 在读路径成功时仍可能 `available=false`、`repair_required=true`（`main/service/runtime_service.py:10176-10199`）；执行束按名取实例，实例缺失才报 `tool not available`（`g3ku/runtime/frontdoor/_ceo_support.py:396-406`）。
- 改法：
  1. 验收增加"真实调用一次"（优先只读动作 / dry_run；无只读入口时至少确认 `available=true` 且 `callable=true`）；
  2. 增加"执行侧确认"：在任务节点车道，需确认 worker 进程已完成资源刷新，不是只看 web 的 Tool 管理页；
  3. 明确"load 成功 ≠ 可调用"，并给出三个要读的字段：`available` / `callable` / `repair_required`。
- 验收：两份 checklist 都出现"真实调用"与"执行侧"两个词；update-tool 主文同步。

#### F9 `content.*` 被当成硬要求

- 文件：
  - `tools/skill-installer/toolskills/SKILL.md:88-98`（第 6 项）
  - `skills/skill-creator/references/create-skill-workflow.md:50-63`
- 现状："`content.main` 必须指向 SKILL.md；references/scripts/assets 也要在 content 中声明"。
- 代码事实：`_build_skill` 硬编码 `root/SKILL.md`，references/scripts/assets 按目录是否存在自动探测（`g3ku/resources/registry.py:129-142`）；`content` 不被读取。
- 改法：
  1. 把 `content.main` 从"必须"降为"生成的兼容字段，运行时以 SKILL.md 为准"；
  2. 删掉"references/scripts/assets 必须在 content 中声明"；
  3. 保留 skill-installer 生成该字段的行为不改（`tools/skill-installer/main/tool.py:596-615`）。
- 验收：两份文档不再把 `content` 当合规硬项。

#### F10 repair-tool 的刷新指引

- 文件：`skills/repair-tool/SKILL.md:136-152`
- 现状：承认 `externaltools/` 变化不一定自动刷新（正确），但"没有显式刷新入口"的表述与事实不符。
- 代码事实：存在 `POST /api/resources/reload`（`main/api/admin_rest.py:2996-3004`），但它只刷新 web 侧 service（`admin_rest.py:76-81, 783-787`）；worker 需自身触发或重启。
- 改法：
  1. 明确两条刷新道：`filesystem_edit` 触发定向刷新；显式 reload 只作用于当前进程；
  2. 增加"任务节点车道需确认 worker 已刷新"；
  3. 删除"如果当前环境没有显式刷新入口"这种不可判定的条件句。
- 验收：三句话分别对应两条刷新道与一个进程边界。

#### F11 临时目录写法

- 文件：
  - `skills/add-tool/SKILL.md:45, 103, 155, 187`
  - `skills/update-tool/SKILL.md:12, 36, 83, 98`
  - `skills/repair-tool/SKILL.md:31, 108, 128, 131, 144`
- 现状：统一写 `<workspace>/temp/<tool_id>/`。
- 代码事实：`{temp}` 是数据根的 `temp/`（`g3ku/agent/tools/filesystem_mutation.py:342-352`）；带目录段的相对路径锚到项目根；项目根 `temp/` 下的 `.zip` / `.whl` / `.exe` / `.log` 会被 managed-artifact 闸门拒绝（`filesystem_mutation.py:540-545`）。
- 改法：
  1. 统一改为 `{temp}/<tool_id>/`；
  2. 补一句：数据根与工作区不同盘时，项目根 `temp/` 不是运行时临时根；
  3. `externaltools/<tool_id>/` 保持工作区相对路径不变。
- 验收：三份 skill 不再出现 `<workspace>/temp/<tool_id>`。

#### F12 `description_too_long` 的返回形状

- 文件：`skills/skill-creator/references/g3ku-resource-spec.md:112`
- 现状："`load_tool_context` / `load_skill_context` 返回 `description_too_long`"。
- 代码事实：
  - tool：`ok=true` + `repair_required=true` + `available=false`，`description_too_long: …` 在 `errors` 里（`g3ku/resources/manager.py:699-705`、`main/service/runtime_service.py:10176-10199`）；
  - skill：`error=skill_repair_required`，原因在 `warnings` / `errors`。
- 改法：写成"按工具/技能两种回执形态判读；不要找顶层 `error=description_too_long`"。
- 验收：表述与两处回执形态一致。

### 阶段三：低危清理

#### F13 陈旧引用与措辞

- `skills/add-tool/references/mcp-client-tool.md:188`：`base.py:214-239` → 现为 276-290；`shell.py:213` → 现为 220。改为函数/属性名引用，不再写行号。
- `skills/add-tool/references/mcp-client-tool.md:19`：见 F6 一并处理。
- `skills/skill-creator/references/create-skill-workflow.md:83`："Codex 实例" → "后续 agent 实例"。
- `tools/skill-installer/toolskills/SKILL.md:67`："`resource_refresh.ok=false` 表示…新 skill 不会进入治理注册表" → 改为"本次定向刷新失败，可能在后续资源代检查中被补上；若持续失败才视为未注册"。

## 4. 实施顺序与批次

- 批次 1（F1-F6）：五份文件的高危修订。先改 `g3ku-resource-spec.md`、`add-tool`，再改 `find-skills`、`mcp-client-tool.md`、`tool-integration-workflow.md`。
- 批次 2（F7-F12）：四份文件的验证与探测修订。
- 批次 3（F13）：清理。
- 每批结束跑一次验收命令并提交一个 commit；不跨批混提交。

依赖：F6 新增的"安装后生效"小节是 F8、F10 的引用目标，先写 F6 再写后两者。

## 5. 验收

### 5.1 命令

```bash
python -m ruff check .
python -m pytest tests/resources/test_tool_resource_admin_api.py tests/resources/test_skill_installer_tool.py tests/resources/test_resource_runtime_smoke.py -q
```

### 5.2 禁用词 grep（应为 0 命中）

- `action id 用`
- `npx skills add`（作为安装指令；`find-skills` 里"不要使用"的否定警示保留，不算违规）
- `当前运行时技能未包含`
- `<workspace>/temp/<tool_id>`
- `### External tool`
- `注册文件写完即触发资源刷新`

### 5.3 人工复核

- 对 6 份改过的 skill，逐个 `load_skill_context` 返回正文；
- 对 `skill-installer` 的 toolskill，用一个临时 GitHub skill 走一遍安装 → 三态复核；
- 抽查 `add-tool` 与 `g3ku-resource-spec.md` 的治理示例，与 `resource_bridge.py` 字段逐项对齐。

## 6. 风险与边界

- **测试钉住**：`test_tool_resource_admin_api.py:4452-4486` 断言了结果交付段落的关键词。任何改动不得触碰 `Result Delivery Flag` / `Parameter Contract Discipline` 两节。
- **F1 是行为改写**：`find-skills` 从"通用生态安装器"变成"Negi 安装路由"。保留发现部分、只改安装段，是默认折中。
- **文档修不了代码缺口**：执行侧首拍基线、worker reload、`requires.tools` 对 tool 不生效，这三项是运行时机制问题，本次只在文档里如实标注，不修代码。要修需另立计划并评估 `tests/resources/test_resource_tree_single_walk.py`、`test_resource_generation_check.py`。
- **不加新测试也是可接受的**：本次可只跑现有用例。若要把新口径钉住，建议新增 `tests/resources/test_resource_skill_contract_docs.py`（grep 禁用词 + 断言两份 checklist 含"真实调用"与"执行侧"），作为可选项，默认不在本次范围。

## 7. 架构文档影响判断

不需要更新 `docs/architecture/`。本次只改 agent-facing skill 文本，运行时代理行为、API 合同、配置字段、生命周期均未变化。若后续按第 6 节修代码，则必须按 `negi-architecture-maintenance` 复核 `tool-and-skill-system.md` 与 `tool-hydration-and-callable-chain.md`。

## 8. 交接

- 执行人只需按第 3 节逐条改、按第 4 节分批、按第 5 节验收；每条都带文件与当前行号。
- 若中途发现同类问题（其他 skill 也写了旧入口或旧状态），按同样判据追加到对应批次，并在提交说明里列明。
- 完成后交付：改动文件清单、每批 commit、验收命令输出、以及仍未修复的代码缺口列表（第 6 节三项）。

## 9. 执行记录（2026-11-09）

- 批次 1：commit `a560c7ad`，完成 F1-F6。
- 批次 2 + 批次 3：同一提交，完成 F7-F13。两批在 `g3ku-resource-spec.md`、`create-skill-workflow.md`、`skill-installer` toolskill 上共用文件，拆开会需要逐 hunk 分期，故合并提交。
- 验收：`ruff` 通过；`test_tool_resource_admin_api.py` + `test_skill_installer_tool.py` + `test_resource_runtime_smoke.py` 共 243 passed；五个被改 skill 经 `ResourceRegistry.discover()` 复核均为 `available=True`、无 warnings/errors。
- 运行时缺口（首拍基线、web/worker 刷新隔离、tool 的 `requires.tools`）已按第 10 节续修，commit `655e0520`。

## 10. 运行时缺口修复（2026-11-09 续）

原则不变：先补机制，再补测试与文档；只动这三处，不顺手重构。

### R1 基线为空时不再“只记不比”

- 落点：`main/service/runtime_service.py` 的 `refresh_changed_resources` 与 `maybe_refresh_external_resource_changes`；`g3ku/runtime/bootstrap_bridge.py` 的 `init_resource_runtime`。
- 改法：
  1. `refresh_changed_resources` 在基线与调用方快照都为空时，执行一次 `manager.reload_now(trigger=...)`，再做 registry / policy 同步并记基线——不允许“只记录、不重载”。
  2. `maybe_refresh_external_resource_changes` 在没有基线时走同一条 reconcile 路径，返回 `refreshed=True`，不再直接记基线返回。
  3. bootstrap 完成 `resource_registry.refresh()` + `sync_default_role_policies()` 后立即记基线，避免正常运行下首个回合多付一次全量重载。
- 测试：更新 `test_resource_tree_single_walk.py` 的空基线用例；在 `test_resource_generation_check.py` 增加“空基线触发 reconcile”用例。
- 文档：`docs/architecture/tool-and-skill-system.md` §10 的“首次调用只落基线”改为“无基线必须先 reconcile”。

### R2 `/api/resources/reload` 下发到 worker

- 落点：`main/service/runtime_service.py` 的 worker 命令表与 `reload_resources_async`。
- 改法：
  1. 新增 worker 命令类型 `reload_resources`（`task_id=None`，与 `refresh_runtime_config` 同一车道）。
  2. `reload_resources_async` 在 `execution_mode == 'web'` 时，完成本地 reload 后入队该命令，并在响应里返回 `worker_reload: {requested, command_id, status: pending}`。
  3. worker 侧执行该命令时调用自身的 `reload_resources`。
- 测试：新增“web 模式入队 / worker 模式不入队”与“命令分发调用 reload_resources”两条单测。
- 文档：`docs/architecture/web-and-admin.md` 与 `tool-and-skill-system.md` §10 说明 reload 的进程边界与异步下发。

### R3 tool 的 `requires.tools` 真正参与探测

- 落点：`g3ku/resources/registry.py` 的发现与要求检查；`g3ku/resources/manager.py` 的定向刷新。
- 改法：
  1. `_discover_tools` 改为两遍：先建全部 descriptor，再用完整工具名集合检查 `requires.tools`。
  2. 新增 `apply_tool_requires_tools(descriptor, tool_names)`，缺失时 `available=False` + `missing required tools: ...`，与 skill 侧文案一致。
  3. 定向刷新在算出 `final_tool_names` 后对本次重建的 tool descriptor 应用同一检查；依赖本轮变更工具名的工具会被连带重建（依赖链到不动点）。
- 测试：新增 `tests/resources/test_tool_requires_tools.py`，覆盖“要求已注册工具 → 可用”“要求缺失工具 → unavailable + warning”。
- 文档：把 `g3ku-resource-spec.md` 与 `repair-tool` 里“tool 不探测 requires.tools”的临时表述改回“探测”。

### 验收与风险

- 命令：`ruff` + 上述测试文件 + `test_resource_runtime_smoke.py`。
- 风险：R3 会改变 tool manifest 的实际效果；仓库现有 tool 的 `requires.tools` 全为空，风险集中在第三方工具。R1 会让每个进程在“无基线”时多一次全量扫描（bootstrap 记基线后正常路径不触发）。
- 生效方式：需要重启 web 与 worker；改完后按运行时验收规范做一次真实对话与节点任务验证。

### 执行记录

- commit `655e0520`：R1 / R2 / R3 全部落地，含测试与两份架构文档。
- 验收：`ruff` 通过；资源/管理/安装器/生成检查/单走/requires.tools 共 254 passed；`test_task_web_worker_runtime.py` 222 passed；`scripts/check_architecture_docs.py` 0 error / 0 warning。
- 实盘验收（重启后 12:38–12:41）：
  - `POST /api/resources/reload` 返回 `worker_reload: {requested: true, command_id, status: pending}`，web 与 worker 日志均出现 `resources reloaded via manual`；
  - 重启后新增两个探针工具，首个前门回合触发 `resources reloaded via external-resource-generation-check: 41 tools`，说明 bootstrap 基线之后的落盘改动能被首个拍点捕获；
  - `reload_probe_ok`（requires `exec`）`available=true`；`reload_probe_bad`（requires 不存在的工具）`available=false` 且 warnings 带 `missing required tools: missing_probe_dependency`；
  - 删除探针并再次 reload 后 `GET /api/resources/tools` 回到 39 条，worker 同步重载。
- 剩余动作：无。重启后的 web/worker 已运行新代码。
