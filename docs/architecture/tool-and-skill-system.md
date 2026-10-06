# G3KU 工具与技能系统说明

本文档解释 G3KU 当前的工具/技能模型，重点面向新接手者说明：

- 工具是如何注册和执行的
- skill 是如何被发现和加载的
- 为什么 Agent 每轮只能看到一部分工具
- candidate tool/skill、callable tool、hydration 分别是什么意思

## 1. 总体设计

G3KU 的工具/技能体系分成几层，而不是一次性把所有东西注入给模型：

1. 固定内置工具
   当前轮直接可调用。

2. 候选工具
   这轮只“可见但不可直接调用”，需要先 `load_tool_context(...)`。

3. 候选技能
   这轮只“可见但不自动注入正文”，需要 `load_skill_context(...)`。

4. 已 hydration 的工具
   某个候选工具在前一轮被显式加载后，下一轮进入真正可调用集合。

这个设计的核心目标，是控制上下文大小、减少工具误用、同时保留扩展能力。

## 2. 关键模块

### `g3ku/agent/tools/registry.py`

`ToolRegistry` 是底层工具执行容器，负责：

- 工具注册与动态替换
- 参数校验
- 注入 runtime context
- 接入 tool watchdog 和资源管理器

这是“工具执行层”的核心，不负责策略选择。

### `g3ku/agent/skills.py`

`SkillsLoader` 负责从共享 `ResourceManager` 中列出和加载 skills：

- `list_skills()`
- `load_skill()`
- `load_skills_for_context()`
- `build_skills_summary()`

它负责“技能资源读取”，不负责“本轮 skill 是否可见”。

### `g3ku/runtime/context/`

这是“本轮上下文选择层”，决定：

- 本轮候选工具有哪些
- 本轮候选 skill 有哪些
- 节点上下文怎么拼
- 执行模型应该看到哪些可调用工具

对新人最重要的文件：

- `node_context_selection.py`
- `execution_tool_selection.py`

### `main/service/runtime_service.py`

这是工具/技能系统与任务运行时的集成中心。它把：

- 固定内置工具
- 治理/RBAC
- 候选池选择
- hydration 状态
- model 可见工具集

全部接到任务运行时里。

### 建议阅读顺序

1. `g3ku/agent/tools/registry.py`
2. `g3ku/agent/skills.py`
3. `g3ku/runtime/context/node_context_selection.py`
4. `g3ku/runtime/context/execution_tool_selection.py`
5. `g3ku/runtime/frontdoor/prompt_builder.py`
6. `main/service/runtime_service.py`

## 3. 四个概念必须分清

### 3.1 fixed builtin tools

指系统预定义、可直接调用的核心工具。

从 `main/service/runtime_service.py` 当前常量看，节点固定内置工具包括：

- `submit_next_stage`
- `submit_final_result`
- `spawn_child_nodes`
- `exec`
- `load_skill_context`
- `load_tool_context`

CEO/frontdoor 另有任务生命周期与分发控制类固定工具。各工具/家族的当前契约与守卫如下：

| 工具/家族 | 当前契约 | 守卫 |
| --- | --- | --- |
| `create_async_task` | CEO/frontdoor 创建 detached 任务的固定 builtin。参数：`task`、`core_requirement`、`execution_policy`、可选 final-acceptance 字段、可选结构化 `file_targets`；`file_targets` 条目若带 `path`，必须已是绝对路径且指向已存在的文件。frontdoor 运行时合同的 `attachment_reopen_targets` 提供可 reopen 的上传条目，模型需自己把精确 `path` / `ref` 抄进 `file_targets`；`user_uploads`、`current_uploads`、`user_image_and_docx` 等占位符不是有效目标（出现时按 frontdoor prompt/contract 引导失败处理，不是 content 工具漂移）。 | 创建前对当前会话**正在运行**的任务池做两层重复预检：已暂停（`is_paused`）或已进入终态（success/failed）的任务不参与匹配，暂停旧任务或等待其失败后即可重新创建同一需求的任务。第一层是确定性精确匹配（规范化目标文本 + 精确关键词指纹），命中后不再短路首条：收集池中**全部**命中任务的 id（`matched_task_ids`，`matched_task_id` 只保留第一个做兼容），`reason` 标注命中类型。第二层是巡检模型语义审查（返回 `approve_new` / `reject_duplicate` / `reject_use_append_notice`），仅当 `main_runtime.duplicate_precheck.llm_review_enabled=true`（默认）且池非空时执行；关闭或巡检不可用时 fail-open 放行（`decision_source` 分别为 `rule` / `fallback`），关闭语义审查后丢失模糊重复与 append-notice 识别。`create_task(...)` 前再做一次确定性精确重验，拦截预检放行后的陈旧读视图或重放竞争。唯一实现是资源工具 `tools/create_async_task_cn`（委托 `MainRuntimeService.precheck_async_task_creation(...)` / `revalidate_async_task_creation_before_create(...)`）；出现重复 detached 任务时，先核对创建是否走这条预检路径而非并行的 create 路径。`reject_duplicate` 拒绝文案枚举全部重复 id，并把拦截口径同步回传给调用模型（只有正在运行的任务才会拦截；暂停旧任务或等待其进入终态（如失败）后即可重新创建），再给出两条出路：补充约束/验收细节 → `task_append_notice`；确需重做 → 先暂停旧任务或等待其失败后重新创建，文案不引导模型自行删除，删除旧任务仍需用户同意（删除侧另有 paused/terminal 守卫与 preview+confirm 两段式）。前门解析按消息前缀区分拒绝语义（`任务未创建：现有任务` 才是 append-notice），重复拒绝文案里出现的 `task_append_notice` 引导词不改变分类。`file_targets` 路径校验是 reject-only，不做自动改写/补全。前门解析只把显式成功形式 `创建任务成功task:...` 当作已核实派发；拒绝消息里提到的 `task:...` id 不算新建任务。派发核实成功后，graph 与 legacy 两条执行路径都会在下一次请求尾部注入一次性提示 `Dispatch result is already available. Reply naturally based on the verified task id ...`，明确允许模型直接以文本收尾（轮末收尾契约见「阶段门控与 callable 收紧」）。 |
| `task_append_notice` | CEO-only 固定 builtin：向当前会话中既有的未完成任务追加新需求、约束或验收预期。成功文本形如 `已向任务 task:xxx 追加通知。`。普通执行/验收节点的内置集合不包含此工具。 | 成功文本必须停留在“更新既有任务”车道，不能形似 detached 任务创建，不产生 `verified_task_ids` / `route_kind=task_dispatch`。`reject_use_append_notice` 表示调用方应改为更新既有任务；拒绝措辞与前门解析显式指向本工具。失败任务的后续跟进一律走普通新规划/执行，没有隐藏续跑车道。 |
| `submit_final_result` | 执行/验收节点结束当前回合的结构化结果工具。验收节点的 `success + final` 表示通过；`failed + final` 表示已汇总全部可修复问题、继续打回；`failed + blocked` 仅表示执行结果异常或不可通过再次提交解决，作为终局失败且不再进入拒收循环。普通拒收没有次数上限。 | 结果协议要求 `summary`、`answer`、`evidence`、`remaining_work`、`blocking_reason`；终局失败的 `blocking_reason` 必须说明证据与不再打回原因，`remaining_work` 不得要求执行节点重复提交。可打回拒绝（`failed + final`）的 `blocking_reason` 传空串，给执行节点的反馈正文由 `summary` 与 `remaining_work` 承载。详见 `main-task-runtime.md`「验收节点：提前创建、激活与握手重派发」。 |
| `task_summary` / `task_list` | CEO 任务查询工具（资源工具 `tools/task_summary_cn` / `tools/task_fetch_cn`；`runtime_service` 内另有同名内嵌 handler，两处参数契约必须同步修改）。默认按当前 `session_key` 只查**本会话**任务；模型可传 `查询范围=全局` 跨会话查询全部任务。summary 文本自带口径标注（`Tasks[global]` / `Tasks[session <id>]`）并把 in_progress 中处于 paused 的数量单列。 | 全局口径的 task_list 每行输出 `[status] (session_id)`，in_progress 且 paused 标为 `in_progress/paused`——paused 任务并没有真在跑，转述时不得当作运行中任务。「某会话查不到其它会话的任务」是默认口径而非数据丢失，先确认调用是否带了 `查询范围=全局`。 |
| `perf_inspect` | CEO-only 普通候选工具（资源 `tools/perf_inspect_cn`，family `task_runtime`，action `perf_inspect_cn`；先 `load_tool_context(tool_id="perf_inspect")`，hydration 后下一轮可调）。用途：读任务大厅顶部性能条背后的同一份数据，判断任务停滞是资源排队还是节点/工具逻辑造成。参数 `mode` ∈ `window`（默认，只读降采样历史）/ `live`（当前 worker 快照 + 同一窗口历史），`window_minutes` 1-1440（默认 10）。返回有界文本报告：档位区间与 `restricted_share`、工具/节点队列等待、CPU/内存/磁盘、库层写等待与事件循环延迟、采样空档、最多 40 行的序列。数据源、采样节拍与聚合口径的唯一契约见 `runtime-overview.md`「Worker Performance History」。 | 不进 fixed builtin：停滞判读是低频排障动作，常驻会挤占 provider `tools[]` 前缀稳定性，代价是起手多一轮 load。`allowed_roles` 只含 `ceo`，执行/验收节点拿不到它（不暴露资源排障能力到每条子任务上下文）。报告里的 `Samples=0` 与采样空档只表示"没有记录"，不得转述成"当时没有压力"；窗口超过 24h 保留期时读到的是空区间而非错误。 |
| `submit_message_distribution` | 节点消息分发模式的内部控制工具，配合 compact prompt `main/prompts/node_message_distribution.md`：检查当前 mailbox 消息与当前 live 执行子节点，决定哪些子节点收到改写后的后续消息。 | 分发模式是 control-only 车道，不暴露普通节点工具；分发轮次中出现 `exec`、`spawn_child_nodes`、content 工具或其他普通执行器，按契约回归处理。决策输入 `live_children` 对每个子节点携带 `latest_tool_round`（运行时帧的最新工具轮实况：`phase`、活跃轮 id、`tool_calls` 的 `tool_name`/`status`/`started_at`，`status=running` 即该子节点正在等待该工具输出）；分发决策以该实况为准，不得依据落后一步的静态输出快照臆断子节点尚未开始工作。 |
| `content_describe` / `content_open` / `content_search` | 普通候选工具（先 `load_tool_context(tool_id="content_*")`，hydration 后下一轮可调），并消耗普通候选选择与 hydration LRU 预算。三者是同一 content navigation 契约的三个入口：`artifact:` 外部化内容引用优先传 `ref`；本地文件优先传绝对 `path`；`path` 不接受 `artifact:`（塞进 `path` 会返回 path-mode 错误）；`path` 只接受**单个已存在文件**：目录按 `path is not a file` 失败，文案自带两条出路（列目录走 `filesystem_stat`、跨文件正文检索走 `exec`）。这条目录口径必须同时住在三处——`path` 参数描述、该工具自己的 toolskill、错误文案本身——只写其中一处时，另两个工具的调用方要重试同一目录才学得到，因此改一处必须三处同改（`tests/test_content_path_mode_copy.py` 钉住三份同时在场）。组合目标结果里失败的一侧只落在 `targets.ref` / `targets.path` 的内层 `error`，顶层仍可以是 `ok: true`，判读目录误用按内层而不是顶层状态；`content_search` / `content_open` 同时收到 `ref` 和 `path` 时分别尝试两个目标并返回组合结果，一侧失败不覆盖另一侧成功结果。artifact content ref 的规范形态是单前缀 `artifact:<hex>`（artifact id 本身即 `artifact:<hex>`，ref 与 id 同形）；历史双前缀 `artifact:artifact:<hex>` 仍按同一 artifact 解析，两套形态可互换。ref 由系统分配，模型必须原样使用任务终态事件、任务/节点详情或先前 content 工具结果中给出的 ref，不得猜测或改写 artifact id；artifact 未命中时错误文案会显式提示这一点。content ref 白名单（`enforce_content_ref_allowlist` + `allowed_content_refs`，逐轮从请求消息现收）只由 legacy `content(action=...)` 实现，split `content_*` 不查它；因此该白名单不构成对 `content_open` 指针的降级理由。 | `content_open` 的 agent-facing 寻址参数分三族且互斥：1-based 行范围 `start_line` / `end_line`，以及 1-based 字符偏移 `start_char` / `end_char`（单行/超大内容按字符分页）；`around_line` / `window` 只保留在 content navigation service 与 legacy `content(action=open)` 的 REST / service / legacy wrapper 层，出现在那些层不算 split 契约回退。图片 reopen 仍走 `content_open` 本身：成功结果返回结构化 payload（如 `content_kind=image`、`multimodal_open_pending=true`、`runtime_image_target`），由运行时决定是否把视觉内容附带到下一次模型请求；是否允许取决于当前运行时/模型绑定启用 `image_multimodal_enabled`，未启用时直接返回 `非多模态模型无法打开图片`；历史上下文里的图片 `path` / `ref` 只是 reopen 入口，重新查看像素仍需再次调用。path 形态的图片目标与文本目标共用同一存在性校验：路径不存在或不是文件时直接返回 `ok:false`（`path not found: …` / `path is not a file: …`），不返回 `ok:true` + 待附带承诺——路径写错必须在当轮就以工具错误回到模型手里。真正读字节的时机在下一次请求的图像 overlay 构造处，而那一点在工具错误道之外：那里抛出的异常不会变成 `Error executing <tool>` 结果，会一路撞到节点执行的兜底 except 并把整个节点转成 error-pause，同时待附带队列只是请求期局部状态、不落帧，resume 后图片静默丢失而模型手里仍留着那条 `ok:true`。所以 overlay 按单张图降级：读失败（缺文件、非文件、不可读）在该图位置放一条 `[图片 <display_name> …未能附带]` 文本说明并跳过，其余图片照常附带。frontdoor 道额外把超过 5 MiB 的目标图压缩到上限以内（先降 JPEG 质量、不够再降分辨率），压不进上限时同样跳过该图并附文本说明。单张图问题不中断整轮、不波及后续轮次。`content_open` 节选与 `content_search` 结果的 inline 守卫只量正文 `excerpt` 字符、不计元数据：正文超过当次模式上限（行模式 16000 / 字符模式 128000）才外置为新 artifact，模型只拿到 summary + ref（超大结果以 content envelope 出现在上下文里属于预期交付）。元数据不计入正文上限，避免“节选 + 元数据”合计撑过闸门被误外部化、触发打开→外部化→指回原 ref 的循环。`content_open` 节选按字符上限行对齐取整：行模式上限 `OPEN_EXCERPT_CHAR_LIMIT`（16000）、字符模式上限 `CHAR_MODE_OPEN_CHAR_LIMIT`（128000），只回显完整行、不在行中途截断；未传寻址参数时从第 1 行起按上限行对齐读取（小文件一次读全）。单行超长内容走行字符兜底：截断到上限并提示改用 `start_char` / `end_char` 分页，MB 级单行文件提示改用 `exec` 定向提取，绝不把整行内联进上下文；显式 `start_line` / `end_line` 区间仍受 `MAX_OPEN_LINES`（200 行）span 约束。legacy `content(action=...)` 兼容包装与 split tools 走同一底层 content service，split tools 并不更宽松。 |
| `filesystem` 家族 | `filesystem` 是稳定的 family/tool_id，只承担治理、候选工具归类、`g3ku://resource/tool/filesystem` URI 与 `load_tool_context("filesystem")` 家族级上下文加载。可执行的变更工具全部是 concrete executors：`filesystem_write`（整文件“创建或替换”）、`filesystem_edit`（单文件文本区域替换，契约见下一行）、`filesystem_copy` / `filesystem_move`（`operations=[{source, destination}, ...]`）、`filesystem_delete`（`paths=[...]`）、`filesystem_propose_patch`。`copy` / `move` / `delete` 是路径级批量操作，变更单位本来就是整个文件系统对象路径，不采用文本区域定位契约。 | 所有路径必须绝对路径，并先经过现有 workspace policy（拦截系统临时目录与 legacy `tmp/`、`.g3ku/tmp`；`tools/` 只放行 resource.yaml/main/toolskills 注册内容；`tmp_*` / `temp_*` 前缀命名——含点前缀 `.tmp_*` / `.temp_*`——以及日志、压缩包、下载产物等临时工件必须落在 temp_root（runtime 注入的 `task_temp_dir`，缺省 `<workspace>/temp`）或 `externaltools/` 下）。目录 `copy` / `move` 只允许目标路径不存在；目录 `delete` 必须显式 `recursive=true`。不存在可调用的 legacy 兼容入口（如 `filesystem(action=...)`、`filesystem.search`、`filesystem.open`）。 `filesystem_stat` 是目录清单与度量的归属工具（`paths` 收到目录时返回 `entries` 与整树聚合），它只列与量、不搜文件正文；content path 模式拒绝目录时给出的两条出路里，列目录归它，跨文件正文检索归 `exec`。 |
| `filesystem_edit` | 模型可见契约是**平面必填字段对**：`path + old_text + new_text`，不含任何 object 字段。`old_text` 须逐字节匹配且唯一，用相邻行扩到唯一为止；`new_text: ""` 即删除该区域。定位方式只有这一条车道，因此没有 `by` / `mode` 判别器、也没有“车道互斥”的散文约束。成功文本回显被替换的行区间（`resolved lines N-M`），调用方不必重读文件即可确认改在了哪一段。 | 校验面 `parameters` 有意宽于模型面：`target.by` ∈ `exact_text` / `anchor_pair` / `line_range` 与 `mode + old_text/new_text`、`mode + start_line/end_line/replacement` 仍是可执行车道，历史转录与 adapter 形状继续可跑（`anchor_pair` 车道因此仍然存在，只是不再向模型曝光——模型面没有 object，就不存在“把替换文本塞进定位器对象”这个错法）；判定 legacy 类别前先剥离对侧车道的占位值（`start_line=0` / `replacement=""` 等）吸收自动填充噪声，但 `target + legacy 字段` 不是受支持组合。模型面不出现 object 字段的原因：provider schema 管线无法表达互斥与条件必填（组合关键字会被压平），所以定位变体只能拆字段车道或拆工具，不能靠散文约束。重复提交同一编辑安全：`old_text` 已不在文件而 `new_text` 唯一在盘（锚点车道为区域已等于 `new_text`）时返回 `Already applied:` 且不写文件，属成功车道；`main/runtime/recovery_check.py` 判定“结果丢失但磁盘已生效”用同一条判据。`load_tool_context` / `get_tool_toolskill` 的参数摘要与示例只渲染模型面。 |

| `silent` | CEO/frontdoor 专属的常驻内置控制工具：调用它 = 本轮不向用户投递任何回复，同时把正文留在上下文里（参数 `reason` 必填，`subject` / `superseded_by` 可选，三者都进转录行元数据供审计与后续轮次判读）。它是回合终态：同批其余工具照常执行完，随后直接收尾，不回模型。节点侧不暴露。合同与理由见 `runtime-overview.md`「3.3 静默回复（`silent` 工具）」。 | 常驻靠一条独立机制，不靠 `RESERVED_INTERNAL_TOOLS`，也不靠 `CEO_FIXED_BUILTIN_TOOL_NAMES`——那两个集合都只保证"已可见时不被语义 top-k 挤掉"，**不具备注入能力**（`stop_tool_execution` 就是反例：没有任何 `resource.yaml` 声明它的族，因此它其实进不了 exposure 名单）。真正让它恒定可用的是**四处**：① 执行侧的工具对象字典——`_frontdoor_execution_bundle` 的 `all_tools` 里与 `submit_next_stage` 一起就地构造，不依赖注册表；② `ALWAYS_CALLABLE_INTERNAL_TOOLS`（不要求已可见）；③④ exposure 与 runtime 可见集两处的恒定点名。顺序上一律追加在真实能力之后、`submit_next_stage` 之前，两条路径同一位置口径。它不参与阶段预算、不受阶段闸门拦截，所以没有活动阶段时也能收尾——但这条豁免**必须同时写在提示词里**（`ceo_frontdoor.md` 的静默条目与契约的 `silent_help` 都点名"无需先开阶段、不要为静默建阶段"），因为阶段协议那句"没活动阶段就先 `submit_next_stage`"字面上覆盖所有工具：实盘出现过模型为静默一轮专门建一个"静默收尾"阶段、白占一次工具轮。纯内置工具不在 `tools/` 目录下，因此不计入资源管理器的工具计数、也不被 RBAC 的 `supported` 集合返回——这四处恒定注入正是为了绕过这一点，**漏掉任何一处都会出现"模型看得见名字却调不动"**：2026-09-24 01:17 实盘就是②③④齐全而①缺失，模型确实发出了 `silent` 调用、被 `_run_single` 的 `visible_tools.get()` 判成 `Error: tool not available: silent`。名字可见 ≠ 对象可解析。名字出现在 callable 名单时，契约另渲染一行 `silent_help:` 告诉模型何时用它，并说明"静默没有文本写法"——措辞里刻意不重提被禁的字面串：在纠正里拼写它等于把它送回模型上下文（`runtime-overview.md`「3.3 静默回复（`silent` 工具）」）。 |

### 3.1.1 为什么"常驻"需要独立机制

`fixed builtin`（不参与候选挑选）与"恒定 callable/visible"（一定进 provider `tools[]`）是两件事，容易混为一谈：

- `CEO_FIXED_BUILTIN_TOOL_NAMES` 与 `RESERVED_INTERNAL_TOOLS` 的作用点都在**已经可见**的名单内部：把它们从语义 top-k 候选里排除、或保证它们不被挤掉。两者都会跳过不在 exposure 名单里的名字。
- 让一个不属于任何 `resource.yaml` 家族的工具恒定出现，需要：① 在工具注册表注册实例（否则 provider 侧解析不到 schema，会静默丢条目）；② 一条不看 exposure 的追加名单（`ALWAYS_CALLABLE_INTERNAL_TOOLS`）；③ 前门 callable 与 runtime-visible 两条状态路径各自恒定点名。

新增内置控制工具时按这三步核对，少一步的症状是"模型从不使用它"或"调了就报错"，都不会指向注册环节。

### 3.2 candidate tools

候选工具是“本轮推荐给 agent 的具体工具列表”，默认可见但不可直接调用；需要先 `load_tool_context(tool_id="...")`。来源链路：资源注册表 → RBAC 过滤 → 检索/排序 → 节点上下文选择。

当前选择规则：

- `candidate_tool_names` 的上界是 RBAC 治理可见集，**不是**本轮已构建的可执行对象字典：候选 = 治理可见 concrete executor −（本轮 callable ∪ 已提升），并剔除已拆出 concrete executor 的家族 id / legacy 单体名。候选生成是 inventory-only，没有语义召回层。`candidate_skill_ids` 与它同一语义。
- 由此得到一条必须成立的三态划分：任一治理可见的 concrete executor 在任一时刻恰属于「本轮可调用」/「本轮可加载并在下一轮提升」/「不可见（RBAC 关闭或 repair-required）」之一。存在第四态（可加载、不提升、也调不动）就是水合合同被破坏，排查路径见 `context-and-cache-troubleshooting.md`「节点侧排查要点」。
- 普通候选数量由 RBAC 可见家族/执行器决定，节点与 CEO/frontdoor 都不再按语义 top-k 截断。
- 当 query 明显表达写入、改写、删除、移动、复制、补丁等变更意图时，本地候选打分优先推 `filesystem_write` / `filesystem_edit` / `filesystem_delete` / `filesystem_move` / `filesystem_copy` / `filesystem_propose_patch` 这类 concrete ids；`exec` 虽然可作为固定 builtin 保持可调用，但在这类意图下不作为候选文件变更方案的首选。

加载门控：

- 精确 `tool_id` 加载对两类 concrete tool 开放：当前 canonical `candidate_tool_names` 中的，以及当前 `rbac_visible_tool_names` 中仍然 surfaced 的，都可以读取 toolskill / 参数说明。`load_tool_context(search_query=...)` 维持可见工具搜索路径，不是枚举所有 RBAC 可见工具的 API。`load_skill_context` 以当前 canonical skill candidate 集合为主门禁；候选未命中时回退查询实时治理可见集（`list_contract_visible_skill_resources`），命中即放行到服务层加载——这条回退让任务运行中途新注册的 skill（如 `skill-installer` 在节点运行中装入的）对当前节点立即可判定，RBAC 与 repair-required 语义仍由 `load_skill_context_v2` 强制；两边都未命中才返回「当前运行时技能未包含」门禁错误，该错误因此只表示“未注册/不可见”，不表示快照滞后。
- 门禁与"不可用"类错误文本必须自带可判定信息，不能把模型推回猜名字：`load_tool_context` 的候选/可见门禁拒绝要枚举它自己要求的那份名单（`本轮候选工具：…` / `本轮候选技能：…`，有界上限、超出只报总数）；被拒名字若是该 `actor_role` 的 fixed builtin（`exec` 等常驻内置工具，本就没有 toolskill），先答"无需加载即可直接调用"再说别的——模型常把常驻工具当候选去加载。`tool not available` 要区分「本轮只是候选、尚未水化 → 先 `load_tool_context`」与「既不可调用也不在候选里」，并分别给出动作与两个名单。三条渲染入口共用 `g3ku/runtime/tool_error_guidance` 的 `availability_hint` / `no_load_needed_hint` / `format_name_group`，各调用点不再自拼名单。
- 只有普通 candidate tool load 会进入 hydration/promotion、占用 hydration LRU；对已经 callable、已经 hydrated、fixed builtin，或当前仅 RBAC 可见但不在 candidate 里的 direct-load lane，`load_tool_context` 都是 read-only toolskill 加载。
- 例外：runtime 侧还有一条免模型的 hydration 触发——`exec` 结果发生输出截断（`stdout_truncated` / `stderr_truncated`）时，运行时自动把 `content_open` 水合到下一轮 callable，并在这条 exec 结果里注入提醒，让模型直接从"立即可用但截断的 exec"切换到"能按行/字符精确读本地文件的 content_open"，不必先手动 `load_tool_context`。若 `content_open` 不在当前 candidate 集合则静默跳过；同一结果只注入一次。
- 节点的可执行对象字典（`_tool_provider` 交付给执行循环的 `tools`）按治理可见集兜底构建，frame 里的候选被写成空时也不塌缩；家族已拆出 concrete executor 时，legacy 单体名不进对象字典也不进候选（模型面只该看到 concrete executor）。这一条与上一条是一对：候选决定"能不能被提升"，对象字典决定"提升后能不能真被解析执行"，两者各自按治理可见集取上界，才不会出现"名字可见却调不动"。
- repair-required 资源从普通候选中剥离：工具进 `repair_required_tools`（不进入 agent-facing `candidate_tools` / `callable_tools`），skill 进 `repair_required_skills`（不进入 `candidate_skills`）；这两个列表只影响 agent-facing runtime contract，不等于 provider-facing `tools[]` 变化。

exec 与 memory 工具家族：

- 部分 concrete executors 同时是资源支撑与固定内置（CEO 的 `exec` / `memory_write` / `memory_delete` / `memory_note`，节点的 `exec` / loader tools），通过 fixed-builtin 路径直接可调用。
- `exec` 除 RBAC 外还有一条契约轴：surfaced family `exec_runtime` 可携带持久化 `metadata.execution_mode`；`governed` 保留 exec 侧守卫，`full_access` 移除 exec 侧 read-only / 路径 / 安全检查，但不绕过 Tool Admin 启用状态与 RBAC。当前模式的权威暴露位置是运行时工具合同 / `load_tool_context` payload。governed 内部分两层：**路径监禁层**（temp/系统路径策略与工作区边界）永不可豁免；**命令形态层**（只读约束 + 破坏性命令黑名单，黑名单默认开启）命中后有两条放行通道——命令白名单豁免（归一化锚定模板匹配，作用域 all/ceo/tasks）或操作者审批（管理端 `/resources/tools/exec-*` 端点裁决；审批请求持久化于 governance sqlite，执行进程轮询等待、web 管理台裁决，天然跨进程；等待到期自动拒绝，时长可配置，近窗连续未获批的同命令快速拒绝防刷屏）。白名单与审批的裁决权只在管理端，模型侧无任何自我豁免参数。**另有不可绕过的宿主进程完整性边界**：`exec` 在进入 execution mode、白名单和审批判定之前，拒绝常见的进程终止表达（PowerShell `Stop-Process`/`Spps`、Windows `taskkill`、POSIX `pkill`/`killall`/`kill`、Python `os.kill`/`signal.kill`，以及 WMI/CIM terminate/delete 等）；该边界在 `full_access`、白名单和审批下仍生效。只读 `Get-Process` / `ps` 等检查允许。它是命令形态防护，不是 OS 沙箱；允许任意 native code、动态解码或外部二进制时，仍必须用独立 worker/container/低权限账户隔离。`exec` 自己启动的子进程应通过 `timeout_seconds`、任务 pause/cancel 或进程树收尾机制清理，不应让任务按 PID/名称清理宿主。
- `exec` 是发现/探测工具（目录结构、文件名搜索、环境检查）；具体本地文件正文证据应来自 `content_open(path=..., start_line, end_line)`。`exec` 长输出的 agent-facing payload 是有界流式捕获：`head_preview` + `tail_preview` + 截断/捕获字节元数据，供排查“关键结果在命令末尾”的场景；普通结果没有稳定的 `stdout_ref` / `stderr_ref`，不应期待隐藏全量输出 ref。节点反复用 `exec` 提取源码片段时，应把引导转向 `content_open(path)`。
- `exec` 解码子进程 stdout/stderr 优先 UTF-8，Windows 上先回退宿主首选代码页再替换字节；Windows 子进程 Python 命令注入 `PYTHONIOENCODING=utf-8`（只稳定 Python traceback / `print()` 输出，不改变 RBAC 或 `execution_mode`）；文件系统校验命令等子进程车道共用同一输出解码 helper——某条 Windows 路径仍乱码时，先确认它是否走了共享 subprocess-text helper。
- committed 长期记忆通过注入的 `MEMORY.md` 快照交付（display-only：剥离 memory id 与日期/来源头，只保留以 `---` 分隔的记忆文本块）；agent-facing 契约没有记忆检索工具，`memory_note(ref)` 是唯一的按需详细记忆加载器。节点执行/验收路径不注入额外记忆检索块。
- `memory_write` / `memory_delete` 是 queue-submit 工具：只请求记忆运行时稍后批处理，不在当前轮同步改写 committed 记忆；`memory_delete(content=...)` 接受对要遗忘内容的自然语言描述。实际改写/删除决策委托给带受限工具面的内部记忆 agent（不属于常规 agent-facing 目录，也不按 surfaced Tool Admin family 排查）；它把描述解析成具体 SQLite id，并可能就实质影响该批次的行报告 `inspired_memory_ids`。

状态与显示层：

- 对 CEO/frontdoor，`candidate_tool_names` / `candidate_skill_ids` 属于 internal canonical state；暴露给模型的当前轮显示合同是两份运行时块：回合内不变的 `frontdoor_runtime_tool_contract`（候选/待修复/附件/临时目录/执行策略）与每跳重写的 `frontdoor_runtime_stage_gate`（callable / 已声明但无权限 / 活动阶段），前者排后者的前面、后者在请求体末位。`candidate_tools` 只列名字，工具的说明文字由 provider `tools[]` 的 `function.description` 承载（同一请求里不抄第二份）。旧轮 candidate/tool/skill catalog 不进入 durable history，后续轮次只继承真实工具调用轨迹与上下文。
- `candidate_tool_names` 是运行时去重、hydration 排除、恢复和 gate 判断用的 canonical name list；`candidate_tool_items`（`{tool_id, description}`）只是它的显示层缓存，用于 contract rebuild / refresh 后保留描述文本。agent 只应看到结构化 `candidate_tools`；`candidate_tool_items` 不是第二份权威候选集——canonical `candidate_tool_names=[]` 时，agent-facing `candidate_tools` 也必须为空，不从旧 contract、旧 items 或旧动态消息把失效候选补回 prompt。
- 对执行/验收节点，canonical `candidate_skill_ids` 落在 runtime frame，`candidate_skill_items` 随 frame 持久化，供阶段切换、prompt compaction 之后的下一轮 contract 刷新从 frame 恢复。`_enrich_node_messages()` 注入、且已携带 `candidate_skills` / `contract_visible_skill_ids` / `skill_visibility_diagnostics` 的 fresh skill 合同是 first-turn truth source：默认空 bootstrap frame 只负责占位与 phase 跟踪，不能把这些字段覆写成空；同一 turn 内 `_prepare_messages()` 裁掉尾部合同消息后，fresh skill 合同摘要沿 runtime context 继续传给 `react_loop`。
- 字段级排障入口：`contract_visible_skill_ids` 是 `runtime_service._node_context_selection_inputs()` 当轮记下的 contract-visible skill 快照（输入层证据，随 runtime frame 与 `runtime-frame-messages:{node_id}` artifact 落盘）；`skill_visibility_diagnostics.entries` 携带 `registry_skill_ids` 与逐 skill 的 `enabled` / `available` / `allowed_for_actor_role` / `policy_effect` / `included_in_contract_visible`，用于定位是 live `resource_registry`、`allowed_roles` 还是治理策略拦掉了 skill；节点 context selection cache 与 `persisted_frame_router` 都带 live-visibility freshness gate——复用旧 selection 前重新对照当前 `session_key` / `actor_role` / `visible_tool_names` / `contract_visible_skill_ids` / `registry_skill_ids`，一旦漂移就丢弃旧 selection，重新跑 `_node_context_selection_inputs()` 与 `build_node_context_selection(...)`。这挡的是“外部 resource/governance refresh 改了可见性，但节点长期沿用旧 cache / 旧 frame”的回归（尤其“首轮 skill 可见集为空，后续轮次一直空”）。
- 前门候选生成诊断同步在 session snapshot 的 `frontdoor_selection_debug`：`tool_selection` 回答命中项为什么没进最终 `candidate_tool_names`。

### 3.3 candidate skills

候选 skill 与 candidate tools 类似，但机制更轻：

- skill 不进入 tool callable 集合
- 需要显式 `load_skill_context(skill_id="...")`
- skill 加载是“当前轮立即消费正文”
- 不走 hydration 状态机
- 对 CEO/frontdoor，`frontdoor_runtime_tool_contract` 摘要会把 `candidate_skills` 明确标成“可通过 `load_skill_context` 读取正文”的候选，避免模型把它们误读成需要安装/水合的候选工具
- repair-required skill 有更强的门控：它仍可作为“待修复资源”出现在 agent-facing `repair_required_skills` 中，但修复完成前 `load_skill_context(...)` / `load_skill_context_v2(...)` 直接返回 repair-required 错误与修复指引（`skill_repair_required` payload 携带 `warnings` / `errors` / `next_actions`），不返回正文；遇到“模型知道这个 skill 存在却无法 load”，先检查 skill 资源本身的 `available` / warnings / errors，而不是先怀疑 selector 没选中
- 节点运行中的 skill 自愈闭环依赖上面两条语义配合：候选快照在节点派发时定格（persisted frame），中途新装 skill 靠加载门禁的实时治理可见性回退获得真实状态——可加载则返回正文，`available=false`（如 `missing required bins`：`requires.bins` 声明了 `shutil.which` 解析不到的命令）则返回修复指引；节点用 `filesystem_*` 修正 manifest 声明或用 `exec` 补依赖（filesystem mutation 自动触发 `refresh_resource_paths` 重探可用性），再次 load 复核。修复规则文本由 `main/prompts/shared_repair_required.md`（执行/验收节点提示词共享块）、`tools/skill-installer/toolskills/SKILL.md`（安装后三态复核）与 `skills/skill-creator/references/g3ku-resource-spec.md`（创建后三态复核 + `requires` 探测声明规则）承载

### 3.4 hydrated tools

hydration 把一次成功的 `load_tool_context` 变成下一轮的 callable：候选在派发时定格、加载后进入水合台账、下一轮并入模型可见集合。台账、提升、重读、参数错误、外置结果信封、统一 timeout 与阶段门控的合同见 `tool-hydration-and-callable-chain.md`。

模型面每跳重写的可调用状态只有 `callable_tools` 一行（= 常驻可调用 ∪ 本轮已提升水合），不再单独渲染水合集，所以"本轮可用"与"已水合"不是两份信息而是一份的两个来源。推论：水合台账被 LRU 淘汰时对模型完全静默——参数表还在 `tools[]` 里（节点清单钉住，删名要等压缩重印），但本轮调不动，只有调用被拒时 `availability_hint` 那两条名单才暴露这个状态。排查"模型看得见却调不了"不要先怀疑 schema，先看该行是否缺这个名字。权限收回是唯一的显式例外：两条车道的尾块都会多出一行 `denied_tools` 提前点名，因为这类名字既不在 callable 也不在候选，不说的话模型只能靠撞一次拒绝才知道。

### 3.5 `cron` 工具合同

`cron` 工具是“结构化提醒”：`message` = 给未来 agent 的提醒动作；`max_runs` = 成功送达上限，省略默认 1；`at` = 只接受创建时仍在未来的单次触发时间，真正执行 `add_job()` 时该时间已过则拒绝创建，并提示 `任务定时已过期，当前时间为<service-local time>，请立即执行或视情况废弃而不要创建过期任务`；`stop_condition` 是兼容字段，不参与运行时停止判断。

- 定时任务触发的回合（`cron_internal`）里，`cron` 工具照常可用 `add` / `list`（新任务仍只绑定创建它的当前会话），只有 `remove` 被限制为只能删除当前正在触发的任务自身——改传其它 `job_id`、或运行上下文缺失 `cron_job_id` 时一律拒绝，防止提醒回合误拆其它任务。
- 使用规范（提醒写成内部指令、调度三选一、投递目标由运行时从当前会话上下文自动推导、模型不传 `delivery.*` / `sessionTarget` / `payload.*`）放在 cron 工具的 toolskill 里，按需 `load_tool_context("cron")` 加载；模型对 cron 用法理解过时时，改 toolskill 而不是改注入逻辑。
- 排查“为什么没有自动停止”先看 cron store 的 `payload.max_runs` / `state.delivered_runs`（到达上限由 scheduler 删除，提醒回合内手工 `remove` 只是辅助出口）；排查“cron 到点了但没创建/查询任务、只重复谈 cron 自己”，先检查 frontdoor tool exposure 是否被错误缩成 `cron`，而不是先怀疑 scheduler 没触发。

### `manage_task_nodes` 节点控制工具

`tools/manage_task_nodes_cn` 提供给 agent 处理错误暂停节点的 callable tool。它调用 `MainRuntimeService.control_nodes(...)`，一次请求可以包含多个同一任务的节点，并按节点返回结果。

- 三种互斥参数形态：单动作批量 `node_ids` + 单一 `action`（无级联时逐节点独立校验，单节点冲突不阻断批次其余节点；`cascade=true` 时同样进入原子路径）；`targets: [{node_id, action, cascade?}]`（每节点独立动作，一次调用可混合 pause/fail 等）；以及**整任务形态**——只给 `task_id` + `action`，不给 `node_ids`/`targets`。整任务形态与「传任务根节点 + `cascade=true`」是同一条代码路径：服务层解析 `task.root_node_id` 后进入同一个原子批次，两种写法语义完全一致。显式空 `targets` 数组按要求打回，不被静默升级为整任务。`cascade=true` 把动作向下传递到以该节点为根的整棵子树：批量形态用顶层 `cascade`，targets 条目未声明 `cascade` 时继承顶层值（显式参数不得被静默忽略）。子树成员是调用时快照，之后新 spawn 的后代不在集内（暂停的祖先会延迟其分发）。`remark` 是批级共享注记（fail 失败原因 / keep_paused 登记备注 / pause 注记），不支持逐条目独立 remark。
- 整任务作用域带任务级副作用，与 `pause_task` / `resume_task` 是同一实现：动作覆盖任务根节点时（整任务形态，或对根节点 `cascade=true`），除逐节点落暂停态外还调用 `pause_task` / `resume_task`，任务自身的暂停标志、调度排队取消、排队等待唤醒与分发失败态复位一并生效——「根节点 + cascade ≡ 全局」在状态与运行时行为两层都成立，不是只改显示。整任务 `fail` 终结整个任务，整任务 `keep_paused` 同样要求非空 `remark`。任务级暂停标志与根节点暂停态的恒等关系、以及任务大厅 Paused 徽章的判读归 `main-task-runtime.md`「Node-Level Pause and Recovery」。
- `action` 取 `resume`、`keep_paused`、`fail`、`pause`。`keep_paused` 必须提供非空 `remark`；该备注写入节点暂停登记，供后续 heartbeat 决策使用。
- `resume` 清除暂停并让运行中的 dispatcher 从持久化 runtime frame 续跑；节点属于「父节点已拿到结果的派生轮」且其 entry 已记为失败时，`resume` 在清旗前被拒（`entry_settled`），复活它得到的工作不会被任何等待方采纳，判据归 `main-task-runtime.md`「Node-Level Pause and Recovery」；`fail` 将暂停节点置为终态并释放父节点等待；`pause` 以 `pause_reason=agent` 登记 agent 发起的暂停。
- targets/级联路径是原子两阶段：先整体校验（节点存在、子树重叠、根节点前置条件、级联 fail 要求子树内所有非终态后代已暂停），任何一项不满足整批打回不生效，返回结构化错误码——`subtree_overlap` 携带 `conflicts`（哪些节点被哪些条目的子树覆盖）、`subtree_not_fully_paused` 携带 `blocking_node_ids`，另有 `node_not_found` / `node_terminal` / `node_already_paused` / `node_not_paused` / `entry_settled`（携带 `detail` 指明是哪个轮次、`hint` 给出口）。重叠判定只针对**跨动作**声明（同一节点被两个不同动作覆盖才是无法消解的二义性）；同动作的子树包含关系自动合并——树结构下两棵子树要么不相交要么一方包含另一方，子集条目被覆盖条目吸收、免根校验，响应 `merged` 列表记录吸收关系。通过校验的批次内，后代的状态冲突逐个跳过并在 `items` 报告，不打回整批。失败一棵子树是两步流程：先级联 `pause`，再级联 `fail`。
- 级联有两处不对称保护：级联 `pause` 容忍已暂停的根节点（跳过根继续级联后代），且跳过已暂停后代不覆写，保留其 `pause_reason=error` 登记与心跳重试计数；级联 `resume` 清除整棵子树的暂停标志，error-pause 登记与心跳重试追踪随之清除。级联 `fail` 按根先、后代 BFS 后的顺序施加（顺序理由见 `main-task-runtime.md`「Node-Level Pause and Recovery」）；对任务根节点执行 fail 会终结整个任务。
- web 模式下只有 `resume` / `fail` / `pause` 会入队 worker 命令（`resume_node` / `fail_node` / `pause_node`，worker 无 `keep_paused` 命令类型）；`keep_paused` 是 leader 本地操作，不产生任何 worker 命令。targets/级联路径每条目入队一条命令，payload 携带 leader 已展开的显式 `node_ids` 且 `cascade=false`，worker 不重展开子树（防两次展开漂移），保条目顺序与 remark 保真。`fail` 的备注随命令下发并作为失败原因兜底；命令派发细节见 `main-task-runtime.md`「Node-Level Pause and Recovery」。
- 工具层只负责参数与结果契约，节点暂停的安全边界、future 等待和恢复语义归 `main-task-runtime.md`「Node-Level Pause and Recovery」；错误暂停事件的投递归 `heartbeat-system.md`「Task Node Error Delivery」。不要通过普通 task 工具或直接改 SQLite 表替代此入口。

## 4. 当前系统为什么这么设计

当前设计针对几个反复出现的问题：

- tool family 和 concrete tool 混在一起，agent 语义不稳定
- 候选池与真实可调用集边界模糊
- skill 与 tool 的加载模型不一致
- family 级别的抽象容易误导 agent

因此：

- agent 尽量只看到 concrete tool / concrete skill
- family 更多留给 UI、治理、后台管理层
- tool 通过 hydration 进入 callable
- skill 通过 direct load 获取正文，不做 hydration

`filesystem` 是这个边界最典型的例子：

- family `filesystem` 继续稳定存在，但它只承担 family/context 身份，不是 callable executor。
- `load_tool_context("filesystem")` 返回的仍是 family 级说明，不意味着会把 monolith `filesystem` 提升成下一轮可调工具。
- 真正会进入 `model_visible_tool_names` 的，只能是 `filesystem_write` / `filesystem_edit` / `filesystem_copy` / `filesystem_move` / `filesystem_delete` / `filesystem_propose_patch` 这些 concrete executors。

## 5. skill 与 tool 的差异

### tool

- 有参数 schema
- 由 `ToolRegistry` 执行
- 可进入 callable tool 集合
- 可能受 watchdog、resource lock、runtime context 影响

### skill

- 本质上是工作流文本/说明文档资源
- 由 `SkillsLoader` / `ResourceManager` 加载
- 不是直接 executable tool
- 是否使用取决于 prompt 约束和 agent 行为

## 6. 维护时最容易踩坑的点

- 把 candidate 当 callable。
- 把 skill 当 tool。
- 忘记 hydration 的“下一轮”语义。
- 把 family 当 agent-facing 语义。
- 误以为 `ToolRegistry` 决定了 RBAC；实际上它只负责执行层。
- 在 CEO frontdoor 中，只盯可见 skills / candidate tools，而忽略了 `ceo_frontdoor.md` 里的 stage-first 稳定协议。
- 把动态 skill/tool 暴露块里的“加载说明”误读成无条件立即执行指令；实际上它们仍受活动阶段存在与否的约束。
- 把节点 `runtime_environment.path_policy` 里的 content 路径约束误读成“content 工具总要传 `path`”；实际上 `artifact:` 必须走 `ref`，只有本地文件才走绝对 `path`。
- `content_*` 从新一轮 callable 列表消失时，先查候选选择与 hydration 状态，而不是 fixed-builtin 暴露。
- 用 transcript 里 loader 调用次数推断阶段预算；`load_tool_context` / `load_skill_context` 不计入 `tool_rounds_used`。

## 7. 维护高风险区域

- `main/service/runtime_service.py`
  因为 fixed builtin、candidate、governance、hydration 都在这里汇合。

- `g3ku/runtime/context/`
  小改动就会改变候选池和提示词，直接影响 agent 行为。

- `g3ku/agent/tools/registry.py`
  一旦 runtime context、watchdog 或 schema 处理出错，会影响所有工具执行。

## 8. Duplicate Tool Call Guard

Tool visibility and callable status do not guarantee that the runtime will keep executing the exact same call forever. `main/runtime/react_loop.py` guards duplicate ordinary calls at two layers keyed on the same signature: `tool_name` plus the normalized arguments serialized as sorted-key JSON. Control/stage/final tools (`stop_tool_execution`, `submit_next_stage`, `submit_final_result`) are exempt from both layers.

- **Same-turn dedupe (before execution).** When one model response contains multiple ordinary calls with an identical signature, only the first occurrence executes. `_execute_tool_calls` drops the extras from the execution batch and gives each of them a tool message in the same reuse contract as message-level dedupe — `status=reused`, `same_as` the first `tool_call_id`, `reason=duplicate_tool_call_in_same_turn`, plus `ref`/`summary` — and live-state status `reused`. This protects non-idempotent tools (such as `spawn_child_nodes`) from duplicated side effects when a model double-fires one call inside a single response. If the first occurrence is blocked by the stage gate or errors, the duplicates receive the same blocked/error result instead of a reuse marker.
- **Cross-turn soft reject (breaker).** When the model emits the same non-control tool call with the same normalized arguments several turns in a row (three consecutive identical signatures inside one node run), the runtime soft-rejects that turn: it records the repeated assistant tool call, appends an error tool message explaining that the call is duplicated, and lets the next model turn repair itself (reuse the prior result or change arguments) instead of escalating directly to an engine failure. Three identical signatures inside a single response also trip the breaker before execution and reject the whole turn, so same-turn dedupe in practice handles pairs and interleaved duplicates.
- After execution, `_dedupe_tool_messages` dedupes identical tool-result contents at the message-history level (later copies become `reused` with `same_as` the earlier `tool_call_id`); that is a context-bloat guard, independent of the two layers above.
  - 豁免只有 `submit_final_result` / `submit_next_stage` 的 error 回执（`_is_no_dedupe_receipt_message`）。理由是可回读性而不是体积：折叠信封的 `ref` 是空串，这份契约文本除了历史里那一拍之外没有第二处可取，而节点逐字重交同一份被拒载荷时，读的就是最新那一行回执——折成 `summary` 残段后，必填项尾段（`answer`/`evidence`/`remaining_work`/`blocking_reason`）正好落在省略号之后。同轮重复调用未执行的那份 reused 合同不受影响，它保护的是副作用不是指引。
- This duplicate-call guard is distinct from the read-only retrieval guard: repeated read-only calls such as legacy `content(action=open/search/describe)`, split `content_describe` / `content_open` / `content_search`, and `task_progress` use their own repair-guidance path with its own repair messaging and escalation semantics.
- The filesystem family does not participate in the read-only retrieval branch: `filesystem` is a family/context id only, and the execution runtime only hydrates concrete mutation executors; repeated filesystem mutations follow the ordinary duplicate-call soft-reject path, while retrieval-style guards remain reserved for read-only tools such as `content_*`, `task_progress`, and `task_node_detail`.
- If a node loops on one tool, inspect the transcript/tool messages first: a tool message with `status=reused` and `reason=duplicate_tool_call_in_same_turn` means that call was never executed, and its `same_as` points at the call whose result is authoritative; the absence of a fresh tool result may likewise mean the runtime intentionally rejected a duplicate call rather than that the tool executor failed.

`runtime-overview.md`「Repeated Tool Call Guard」一节是指向本节守卫的摘要引用。

## 9. Tool Admin RBAC For Surfaced Tool Families

There is an explicit maintenance boundary between:

- tool families that appear in Tool Admin (`/api/resources/tools`, Tool management UI), and
- internal fixed tools that never appear there, such as `submit_next_stage`.

For Tool Admin surfaced tool families, RBAC is the highest-priority access contract. Internal fixed tools remain outside this contract: they keep their runtime-only visibility rules, their access is not modeled through Tool Admin `allowed_roles`, and behavior questions about stage protocol tools such as `submit_next_stage` are debugged through the stage/runtime path rather than Tool Admin RBAC.

Current RBAC rules for surfaced families:

- `actions[].allowed_roles` is an exact persisted whitelist. An empty list means deny-all for that action.
- `task_runtime` 家族的读取动作不再只归 CEO：`task_node_detail`（action `node_detail_cn`）现在对 `ceo` / `execution` / `inspection` 三个角色都开放，且**按维护者决定不设归属护栏**——任何节点只要报出真实的 `(task_id, node_id)` 就能读到该节点的完整记录，而 task_id 本就明文写在节点自己的引导消息里。这是决定不是待办，测试把它钉成断言，别在别处"顺手修回去"。同时该工具对 agent 固定返回 `full`（`detail_level` 已从工具 schema 移除，节点不再需要为它做一次决策），`summary` 视图只保留给 Web REST 传输层（`main/api/rest.py` 的查询参数不变）。被阶段裁撤移出上下文的原始工具入参/出参就是靠这条道回读：完整执行轨迹按 `stage.rounds[].tool_call_ids` 关联 `task_node_tool_results` 里逐条留存的 `arguments_text` / `output_ref`，账本与结果表都不因裁撤而减少。
- Refresh, reload, reopen, and store readback must preserve an explicit empty list; maintainers should treat `[]` as real state, not as “missing”.
- If an executor belongs to a surfaced tool family, its model visibility follows Tool Admin RBAC exactly. A surfaced fixed-builtin executor may still be listed in frontdoor or execution fixed-builtin sets, but it only becomes actually visible/callable when the surfaced family/action RBAC allows it. When debugging “the tool still appears after I removed all roles”, inspect the persisted `tool_families` record and the derived `role_policy_matrix` first; there is no fallback to `ceo` or `execution`.
- If a fresh workspace shows an unexpected default role set, compare the resource discovery governance with the first persisted `tool_families` row before debugging prompt assembly or frontend rendering.
- First-discovery seeding boundary (separate from persisted RBAC): when a surfaced tool action is discovered for the first time and there is no persisted `tool_families` row/action yet, runtime seeds `allowed_roles` from the tool's discovery governance — either explicit resource-local `governance.actions[].allowed_roles`, or the implicit default governance mapping in `main/governance/action_mapper.py`. This matters especially for merged surfaced families such as `skill_access`, where concrete executors like `load_skill_context` and `load_tool_context` share one family/action row. After that first persistence boundary, `tool_families.payload_json` becomes authoritative; reload/refresh must preserve operator edits, including an explicit persisted `[]`.
- One-time legacy repair boundary for older workspaces: before `governance_meta.implicit_tool_role_backfill_v1_applied` is set, refresh may backfill an older persisted empty `allowed_roles=[]` action from the newly discovered non-empty discovery-governance default, to heal historical fresh-start rows accidentally persisted as deny-all for implicit-governance surfaced families. Once that meta flag is written, later refreshes stop auto-healing empty lists; an operator-cleared `[]` stays authoritative.
- 鉴权读法是按列窄查询（`actor_role` + `resource_kind` + `resource_id`，动作取精确匹配或 `NULL` 通配，`action_id DESC` 让精确动作排在通配之前），不是全表读回再在 Python 里过滤。因此有一个会被静默破坏的不变量：**`role_policy_matrix` 的 `actor_role` 列必须与 `payload_json` 里的公开形态逐字一致**——写路径必须继续过 `to_public_actor_role`，把 `checker` / `acceptance` 这类别名原样写进列会让窄查询查不到策略而判成 `policy_denied`。这条改法的原因是一条实测：全表形态下 270 行意味着每次判定都要 `json.loads` + pydantic 校验 270 遍，单次 2,147 µs、一个回合 58 个技能的可见性扫描 124.6 ms；窄查询后是 11.9 µs 与 0.7 ms。`list_role_policies()` 仍然保留，只给管理端列表与对账用。

前端与 API 侧职责详见 `web-and-admin.md`「Tool Admin RBAC Contract」。

### Exec Runtime Mode Contract

- `exec_runtime` is currently the only surfaced tool family with an extra persisted mode field in Tool Admin: `tool_families.metadata.execution_mode`.
- The resource manifest may provide a default `settings.execution_mode`, but the persisted family metadata is the runtime source of truth once an operator saves an override; resource refresh must preserve that override, otherwise Tool Admin would show one mode while runtime execution silently falls back to another.
- `load_tool_context("exec")` / `load_tool_context("exec_runtime")`, node dynamic contracts, and frontdoor dynamic contracts should all agree on the same `exec_runtime_policy` payload. If they disagree, debug the persisted tool family record first, then the contract-injection path.

### CEO Regulatory Governance Mode

- Tool Admin also owns one global persisted switch for CEO/frontdoor: `ceo_frontdoor_regulatory_mode_enabled`. It is governance metadata used by CEO/frontdoor approval policy, not stored on a specific surfaced tool family.
- Its scope is narrower than generic Tool Admin RBAC: RBAC decides whether a surfaced tool family/action is visible or callable at all; regulatory mode only decides whether already-visible medium/high-risk CEO tool calls must pause for batch human review.
- With the switch enabled: risky CEO tool calls are grouped into one `frontdoor_tool_approval_batch` interrupt; `review_items` enumerate only the risky calls that require operator review; the resume side must submit one complete `submit_batch_review` payload covering every `review_item` exactly once. Pass-through low-risk tool calls in the same original tool batch remain part of the runtime-owned original tool-call ordering — “review items” are not “all tool calls in the round”.
- Rejected risky calls do not disappear: CEO/frontdoor constructs synthetic rejection tool results and merges them back with approved real tool results in original tool-call order before the next model round.
- Changing the switch affects future approval boundaries immediately, even for already-running CEO sessions, but must not silently rewrite an approval batch that is already paused and waiting for review.

### Removal Of `message` / `messaging`

The surfaced `message` executor and its Tool Admin family `messaging` are absent from the resource/tool contract entirely: resource discovery finds no `tools/message/resource.yaml`, Tool Admin lists no `messaging` family, and CEO/frontdoor fixed builtin exposure and the default `frontdoor_interrupt_tool_names` include no `message`; if either still appears in Tool Admin or in provider-facing web tool schemas, treat that as a contract regression rather than a disabled-by-default state. Browser/web replies travel through the websocket session path (`ceo.reply.final`, inflight snapshots, and related runtime events) and external-channel replies through the External Agent API event stream (`outbound.created`, see `external-agent-api.md`), so channel reply delivery is owned by those paths and independent of this tool-family contract.

### Provider Tool Surface

发给 provider 的 `tools[]` 声明清单（`provider_tool_names`）与"本轮真能调用"的 `tool_names` 是两份集合，两条车道都用这份区分做排查：`tool_names` 是当前轮权威 callable 合同；`provider_tool_names` 只决定 `tools[]` 里出现哪些名字与参数表。水合提升与阶段门控改动运行时合同尾块，不该每轮都重铺 `tools[]`。运行时合同是插在最新 user 消息之前的 system-role 摘要，不是 provider schema，也不是 durable history；模型若回显它，前门归一化只做一次私有修复，重复回显永不升级为最终输出。

两条车道发的是同一种形状，区别只在重印边界有几条：

- **CEO/frontdoor**：首跳按当轮 RBAC 可见 concrete executor 全集播种，之后钉住。重印边界三条——内联压缩（preflight 报 `token_compression`）、手动压缩（车道路在 session 上挂 `PENDING_PROVIDER_BUNDLE_RECOMMIT_ATTR`，读一次即清）、**换车道**（正常 ↔ 心跳/定时内部轮；内部轮的工具面是刻意收窄的，继承上一车道的宽面等于给内部轮多发一份执行面）。
- **执行/验收节点**：同样首跳按角色 RBAC 全集播种后钉住，重印边界只有 `token_compression` 一条。两条车道的钉住都是**只补不删**：当轮新增能力即时并入（否则模型手里只剩名字、拼不出合法参数表），删名一律推迟到重印点；那一跳正文已被 `[G3KU_TOKEN_COMPACT_V2]` 整段重写，可复用前缀反正断在这里，重印不额外破缓存。声明侧渲染脱离派发字典：清单里的名字若当轮已无实例，参数表取自 `declared_denied_tool_schemas`。

漂移规则：

- 普通跳只有在成员集合真的变化时才重算 `tools[]`；同名不同序保持持久化顺序原样不动，不为零收益轮换 schema。
- `stage_compaction` 任何一侧都不得轮转 `tools[]`。`token_compression` 是两条车道共同的重印点，前门另加"手动压缩"与"换车道"两条。重印取的是"此刻按当轮 RBAC 重新播种会得到什么"，不是退回上一跳的窄清单。
- 清单滞后不是权限滞后，权限收回也不是删名的理由：两条车道的执行准入都与声明解耦——节点按当轮待派发字典查名，前门派发按 `_frontdoor_dispatch_tool_names` = 钉住清单 ∩ 当轮治理可见集（治理可见集取不到时退回当轮 callable pool，宁可窄不可宽）。RBAC 收回在执行侧当跳即拒，而清单继续带着该名字。两条车道的尾块都印 `denied_tools` 提前点名这一行。**前门那一行只有一个算点**：`_frontdoor_declared_denied_tool_names_for_state` = state 上钉住的清单 ∩ 实时授权读（`list_effective_tool_names(actor_role="ceo")`，取不到就不印，不拿回合内快照凑）。它必须生效在发送前用 state 重建契约块的那一处——装配路 `message_builder.py` 建的那份在落请求体前还会被 `_refresh_prompt_cache_state` 与发送预检各重算一次，所以尾块加新字段只写进装配层等于没写。拒绝文案三态分开：候选未水化 ⇒ 先 `load_tool_context`；已收回 ⇒ 重试与改名都不会放行；两者都不在 ⇒ 未知名字。
- 曝光收窄（候选塌缩、LRU 淘汰 callable）不得把名字从节点清单里删掉；LRU 只管 callable 层，与清单无关。被淘汰但清单仍带 ⇒ 模型看得见参数表、本轮调不动，需重新 `load_tool_context`。
- 工具曝光漂移改变当轮实际请求，但不因此轮换 caller-side prompt cache family；family 变化只留给稳定前缀重写、车道或模型切换、显式 cache-family revision bump 及其他刻意重置边界。`tool_signature_hash` / `actual_tool_schema_hash` 是观测字段，不是"该有新 family"的证据；缓存侧排查步骤见 `context-and-cache-troubleshooting.md`「跨普通 fresh turn 的 tool schema churn」。

provider-facing schema 刻意保持最小：

- 富文本说明留在尾部运行时合同里，provider `tools[]` 只保留 function calling 所需的最小 schema。repair-required 资源不靠轮换 provider `tools[]` 来表达：那两份清单只是运行时摘要侧的指引，清单稳定性优先于缓存连续性。
- 说明性文本与不支持的 JSON Schema 组合子（`anyOf` / `oneOf` / `allOf`）在传输前被剥平，provider-facing schema 只保留 function calling 需要的最小形状；参数正确性的权威仍在运行时校验侧，不要假设线上那份 schema 还保留了内部合同的每个分支。若缓存缺失与 `actual_tool_schema_hash` 的大幅变化同时出现，先查 provider schema 是否从这份最小/稳定形态回归成了富文本形态。

### Pinned Static Declarations（前门头部的钉住块）

三条回合内不变的声明——`candidate_skills`、`exec_runtime_policy`、`session_temp_dir`——在前门只声明一次：渲染成 `## Runtime Contract (pinned)`，并进请求体首条 system 文本的末尾（与 `## Capability Exposure Snapshot` 同一载体，不是新消息项，因此尾部重铺那条剥运行时块的路碰不到它）。尾块的省略判定**逐段**看头部原文里有没有那一段：有才省，缺的那段照旧整段留在尾块。判据取头部原文而不是"本轮算出来了"——前门一个回合内有四条装配路，任何一条没写进头部却照样让尾块省略，声明就在整份上下文里消失，模型看不到候选技能也不会报错。

- 名单是这块唯一的量：实盘 5,285 跳里 `candidate_skills` 一行中位 281 token、占整份稳定契约块 23.1%，而执行策略与会话临时目录各只一行。名单为空时整块不钉，三段声明全留在尾块：为两行短声明去动头部是不划算的买卖——头部一改就顶掉它身后的全部前缀。
- 重钉边界三条：曝光提交点（`cache_family_revision`，即 capability snapshot 的 `exposure_revision`）、执行策略签名、会话临时目录。名单本身不进边界，且钉住那份按 id 排序——每回合的语义挑选会换顺序也会换成员，进边界就每回合顶掉头部。
- 名单漂移的表达方式是差集，不是重印：尾块只在成员差非空时出 `granted_skills`（本轮新增可见、头部还没有）与 `unselected_skills`（头部声明了、本轮没选进候选，调不动）两行。头部因此会"过期"，这是设计状态而不是缺陷；只有三条边界动了才整段重印。
- 差集覆盖到名单的三分之二时改回整份 `candidate_skills` 行（`_roster_difference_too_wide`）：一个名字 ≈ 5 token、整份名单 ≈ 281 token，差到几十个名字时两行否定式清单既不更便宜也更难读。实盘 5,005 跳里名单只变过 21 跳（中位差 1 个名字），其中 3 跳的差达到 40–46 个——这条分支的命中量就是那 3 跳。
- 载体有两份：会话对象属性，外加按 `session_key` 分桶的进程表（LRU 封顶）。同回合内不同装配路拿到的 session 不是同一实例，而 `state` 里的键会被归一化白名单静默丢掉、不能当载体；两份载体都读，判据键相同就复用同一串原文。重启后首跳重钉一次，代价是那一次整段重算。
- 合头部一律**先摘再拼**，摘的位置是抬头行的**首次**出现（不是末次）：续跑回合的头部来自上一跳请求体种子、本身已带一份块，直接追加会把同一份块叠两遍并永久留在携带正文里（实盘抓到过 11,379→12,960 字符的双块头部，之后每跳都带着）。
- 节点车道的这份名单仍每跳进尾块：节点的运行时合同消息同时是机器状态（`extract_node_dynamic_contract_payload` 把 `candidate_skills` 交回派发与水合侧），把它移出正文会改掉派发行为，不是一次缓存搬家。

## 10. 资源目录代检查与语义目录新鲜度

skill / tool 目录可能被编辑器、git 或外部进程直接改动，注册表不会自己收到通知，所以运行时按节拍做一次"代"比对：

- 节拍由 `resources.reload.poll_interval_ms`（JSON 侧 `resources.reload.pollIntervalMs`）节流，默认 1000ms；配成 0 表示每个调用点都允许比对。
- 比对面是 `capture_resource_tree_state()` 产出的 `{"skills": {name: 目录树指纹}, "tools": {...}}`，指纹来自 `ResourceRegistry._tree_fingerprint(资源目录)`。注册表缺失或目录为空时快照为空，比对直接返回"无变化"，这不是错误态。
- 指纹覆盖目录树里每个文件的相对路径、`mtime_ns` 与字节数，`__pycache__` / `node_modules` / `.git` / `.venv` / `venv` / `env` / `.pytest_cache` / `.ruff_cache` 这些目录整体不参与；软链接目录不进入递归。也就是说"改了正文一个字节"能被发现，而"原地替换成一个同大小且 mtime 未变的文件"不能。
- 这条扫描跑在 worker 的事件循环线程上，且是每条 `exec` 命令都要付的成本，所以它的单价必须按实盘规模看：skills 60 个目录 1659 个文件 + tools 33 个目录 93 个文件，一次全量扫描实测 45–50 ms。指纹吃 `os.scandir` 随目录枚举一并返回的目录项元数据；改成"先收集路径再逐个 `stat()`"实测回到 132–150 ms。
- 首次调用只落基线：没有上一份快照就没有可比对象，因此第一拍永远不报刷新。
- 三个比对点共用服务侧那一份基线（`_resource_tree_state_cache`）：CEO 前门的节流拍、`exec` 工具每条命令结束（`trigger='tool:exec'`）、管理端显式路径。命令侧不携带自己的快照，所以一条命令只付一次扫描；又因为基线就是"上一次扫描时的树"，命令执行前后之外发生的外部编辑同样会在下一个比对点被抓到，不存在被跳过的变更。
- 指纹变化时才动作：`refresh_changed_resources(trigger='external-resource-generation-check')` 重建受影响资源，随后清空 `_node_context_selection_cache`（节点上下文选择里缓存着旧的候选/水合结论），并只对名字出现在差异集里的 skill / tool 重新同步语义目录条目。
- 管理端保存或编辑资源走的是显式路径（`refresh_resource_paths` / `refresh_paths`，trigger 为 `path-change`），它同步刷新注册表并当场重记基线；各条路径共用同一份基线，所以编辑后的一拍不会因为节流而漏掉变化。
- 维护要点：新增会进入上下文选择的缓存时，必须在这条差异路径上一起失效，否则外部改动的 skill/tool 正文会长期以旧指纹参与候选与描述投影。
