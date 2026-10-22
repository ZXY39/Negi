# Negi 工具与技能系统说明

本文档解释 Negi 当前的工具/技能模型，重点面向新接手者说明：

- 工具是如何注册和执行的
- skill 是如何被发现和加载的
- 为什么 Agent 每轮只能看到一部分工具
- candidate tool/skill、callable tool、契约在场分别是什么意思

## 1. 总体设计

Negi 的工具/技能体系分成几层，而不是一次性把所有东西注入给模型：

1. 固定内置工具
   当前轮直接可调用。

2. 候选工具
   这轮只“可见但不可直接调用”，需要先 `load_tool_context(...)`。

3. 候选技能
   这轮只“可见但不自动注入正文”，需要 `load_skill_context(...)`。

4. 契约在场的工具
   它的 toolskill 契约块在这一跳的模型可见请求视图里，因此这一跳真能执行。块不在场也照样能调用它，只是那一跳交回契约。

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
- 契约在场判定
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

CEO/frontdoor 另有任务生命周期与分发控制类固定工具。治理记录、RBAC、启停、删除、核心保护都以**工具**为单位：一个 `tools/<name>/` 目录就是一条记录，没有中间的分组层。各工具的当前契约与守卫如下：

| 工具 | 当前契约 | 守卫 |
| --- | --- | --- |
| `create_async_task` | CEO/frontdoor 创建 detached 任务的固定 builtin。参数：`task`、`core_requirement`、`execution_policy`、可选 final-acceptance 字段、可选结构化 `file_targets`；`file_targets` 条目若带 `path`，必须已是绝对路径且指向已存在的文件。frontdoor 运行时合同的 `attachment_reopen_targets` 提供可 reopen 的上传条目，模型需自己把精确 `path` / `ref` 抄进 `file_targets`；`user_uploads`、`current_uploads`、`user_image_and_docx` 等占位符不是有效目标（出现时按 frontdoor prompt/contract 引导失败处理，不是 content 工具漂移）。 | 创建前对当前会话**正在运行**的任务池做两层重复预检：已暂停（`is_paused`）或已进入终态（success/failed）的任务不参与匹配，暂停旧任务或等待其失败后即可重新创建同一需求的任务。第一层是确定性精确匹配（规范化目标文本 + 精确关键词指纹），命中后不短路首条：收集池中**全部**命中任务的 id（`matched_task_ids`，`matched_task_id` 只保留第一个做兼容），`reason` 标注命中类型。第二层是巡检模型语义审查（返回 `approve_new` / `reject_duplicate` / `reject_use_append_notice`），仅当 `main_runtime.duplicate_precheck.llm_review_enabled=true`（默认）且池非空时执行；关闭或巡检不可用时 fail-open 放行（`decision_source` 分别为 `rule` / `fallback`），关闭语义审查后丢失模糊重复与 append-notice 识别。`create_task(...)` 前再做一次确定性精确重验，拦截预检放行后的陈旧读视图或重放竞争。唯一实现是资源工具 `tools/create_async_task_cn`（委托 `MainRuntimeService.precheck_async_task_creation(...)` / `revalidate_async_task_creation_before_create(...)`）；出现重复 detached 任务时，先核对创建是否走这条预检路径而非并行的 create 路径。`reject_duplicate` 拒绝文案枚举全部重复 id，并把拦截口径同步回传给调用模型（只有正在运行的任务才会拦截；暂停旧任务或等待其进入终态（如失败）后即可重新创建），再给出两条出路：补充约束/验收细节 → `task_append_notice`；确需重做 → 先暂停旧任务或等待其失败后重新创建，文案不引导模型自行删除，删除旧任务仍需用户同意（删除侧另有 paused/terminal 守卫与 preview+confirm 两段式）。前门解析按消息前缀区分拒绝语义（`任务未创建：现有任务` 才是 append-notice），重复拒绝文案里出现的 `task_append_notice` 引导词不改变分类。`file_targets` 路径校验是 reject-only，不做自动改写/补全。前门解析只把显式成功形式 `创建任务成功task:...` 当作已核实派发；拒绝消息里提到的 `task:...` id 不算新建任务。派发核实成功后，graph 与 legacy 两条执行路径都会在下一次请求尾部注入一次性提示 `Dispatch result is already available. Reply naturally based on the verified task id ...`，明确允许模型直接以文本收尾（轮末收尾契约见「阶段门控与 callable 收紧」）。 |
| `task_append_notice` | CEO-only 固定 builtin：向当前会话中既有的未完成任务追加新需求、约束或验收预期。成功文本形如 `已向任务 task:xxx 追加通知。`。普通执行/验收节点的内置集合不包含此工具。 | 成功文本必须停留在“更新既有任务”车道，不能形似 detached 任务创建，不产生 `verified_task_ids` / `route_kind=task_dispatch`。`reject_use_append_notice` 表示调用方应改为更新既有任务；拒绝措辞与前门解析显式指向本工具。失败任务的后续跟进一律走普通新规划/执行，没有隐藏续跑车道。 |
| `submit_final_result` | 执行/验收节点结束当前回合的结构化结果工具。验收节点的 `success + final` 表示通过；`failed + final` 表示已汇总全部可修复问题、继续打回；`failed + blocked` 仅表示执行结果异常或不可通过再次提交解决，作为终局失败且不进入拒收循环。普通拒收没有次数上限。 | 结果协议要求 `summary`、`answer`、`evidence`、`remaining_work`、`blocking_reason`；终局失败的 `blocking_reason` 必须说明证据与不打回原因，`remaining_work` 不得要求执行节点重复提交。可打回拒绝（`failed + final`）的 `blocking_reason` 传空串，给执行节点的反馈正文由 `summary` 与 `remaining_work` 承载。详见 `main-task-runtime.md`「验收节点：提前创建、激活与握手重派发」。 |
| `task_summary` / `task_list` | CEO 任务查询工具（资源工具 `tools/task_summary_cn` / `tools/task_fetch_cn`；`runtime_service` 内另有同名内嵌 handler，两处参数契约必须同步修改）。默认按当前 `session_key` 只查**本会话**任务；模型可传 `查询范围=全局` 跨会话查询全部任务。summary 文本自带口径标注（`Tasks[global]` / `Tasks[session <id>]`）并把 in_progress 中处于 paused 的数量单列。 | 全局口径的 task_list 每行输出 `[status] (session_id)`，in_progress 且 paused 标为 `in_progress/paused`——paused 任务并没有真在跑，转述时不得当作运行中任务。「某会话查不到其它会话的任务」是默认口径而非数据丢失，先确认调用是否带了 `查询范围=全局`。 |
| `perf_inspect` | CEO-only 普通候选工具（资源 `tools/perf_inspect_cn`，工具 id 就是 `perf_inspect`；先 `load_tool_context(tool_id="perf_inspect")`，或直接调用它——契约不在场时那一跳交回契约，下一跳起执行）。用途：读任务大厅顶部性能条背后的同一份数据，判断任务停滞是资源排队还是节点/工具逻辑造成。参数 `mode` ∈ `window`（默认，只读降采样历史）/ `live`（当前 worker 快照 + 同一窗口历史），`window_minutes` 1-1440（默认 10）。返回有界文本报告：档位区间与 `restricted_share`、工具/节点队列等待、CPU/内存/磁盘、库层写等待与事件循环延迟、采样空档、最多 40 行的序列。数据源、采样节拍与聚合口径的唯一契约见 `runtime-overview.md`「Worker Performance History」。 | 不进 fixed builtin：停滞判读是低频排障动作，常驻会挤占 provider `tools[]` 前缀稳定性，代价是起手多一轮 load。`allowed_roles` 只含 `ceo`，执行/验收节点拿不到它（不暴露资源排障能力到每条子任务上下文）。报告里的 `Samples=0` 与采样空档只表示"没有记录"，不得转述成"当时没有压力"；窗口超过 24h 保留期时读到的是空区间而非错误。 |
| `process_kill` | 普通候选工具（先 `load_tool_context(tool_id="process_kill")`，或直接调用它拿契约；不进常驻 `tools[]`）。只停 `exec background=true` 起的进程：`pid` 必须来自那次 exec 结果的 `pid`，且属于当前 scope（CEO 会话按 `session:<session_key>`，执行节点按 `task:<task_id>`）。返回 `status`（`terminated` / `already_exited` / `identity_mismatch` / `not_owned`）、`cmd`、`log_path` 与被终止进程的 `log_tail`；`not_owned` 时附本 scope 的 `owned_processes` 台账（有界）。 | 归属登记表是 `g3ku/runtime/owned_processes.py`，条目落数据根 `owned-processes/`（每个进程一个 json + 一个日志）。三条硬规矩：**scope 隔离**（跨会话/跨任务的 pid 等于没登记）、**`create_time` 身份校验**（PID 复用不会被误杀）、**只向下杀树**（`kill_process_tree_pid`，祖先不可达）。`allowed_roles` 默认 `ceo` + `execution`，验收/巡检拿不到。裸 shell 的终止形态仍被宿主进程保护拒绝——本工具是那条车道的唯一出口，不是绕过它的后门（见 `operations-and-maintenance.md`「任务执行了全局进程清理 / Web 与 worker 同时退出」）。生命周期三处回收：会话清空回收该 session scope、任务终态回收该 task scope、运行时启动回收 `owner_pid` 已死的孤儿。 |
| `submit_message_distribution` | 节点消息分发模式的内部控制工具，配合 compact prompt `main/prompts/node_message_distribution.md`：检查当前 mailbox 消息与当前 live 执行子节点，决定哪些子节点收到改写后的后续消息。 | 分发模式是 control-only 车道，不暴露普通节点工具；分发轮次中出现 `exec`、`spawn_child_nodes`、content 工具或其他普通执行器，按契约回归处理。决策输入 `live_children` 对每个子节点携带 `latest_tool_round`（运行时帧的最新工具轮实况：`phase`、活跃轮 id、`tool_calls` 的 `tool_name`/`status`/`started_at`，`status=running` 即该子节点正在等待该工具输出）；分发决策以该实况为准，不得依据落后一步的静态输出快照臆断子节点尚未开始工作。 |
| `content_describe` / `content_open` / `content_search` | 普通候选工具（先 `load_tool_context(tool_id="content_*")`，或直接调用它拿契约），并消耗普通候选选择预算。三者是同一 content navigation 契约的三个入口：`artifact:` 外部化内容引用优先传 `ref`；本地文件优先传绝对 `path`；`path` 不接受 `artifact:`（塞进 `path` 会返回 path-mode 错误）；`path` 只接受**单个已存在文件**：目录按 `path is not a file` 失败，文案自带两条出路（列目录走 `filesystem_stat`、跨文件正文检索走 `exec`）。这条目录口径必须同时住在三处——`path` 参数描述、该工具自己的 toolskill、错误文案本身——只写其中一处时，另两个工具的调用方要重试同一目录才学得到，因此改一处必须三处同改（`tests/test_content_path_mode_copy.py` 钉住三份同时在场）。组合目标结果里失败的一侧只落在 `targets.ref` / `targets.path` 的内层 `error`，顶层仍可以是 `ok: true`，判读目录误用按内层而不是顶层状态；`content_search` / `content_open` 同时收到 `ref` 和 `path` 时分别尝试两个目标并返回组合结果，一侧失败不覆盖另一侧成功结果。artifact content ref 的规范形态是单前缀 `artifact:<hex>`（artifact id 本身即 `artifact:<hex>`，ref 与 id 同形）；历史双前缀 `artifact:artifact:<hex>` 仍按同一 artifact 解析，两套形态可互换。ref 由系统分配，模型必须原样使用任务终态事件、任务/节点详情或先前 content 工具结果中给出的 ref，不得猜测或改写 artifact id；artifact 未命中时错误文案会显式提示这一点。 | `content_open` 的 agent-facing 寻址参数分三族且互斥：1-based 行范围 `start_line` / `end_line`，以及 1-based 字符偏移 `start_char` / `end_char`（单行/超大内容按字符分页）；`around_line` / `window` 只保留在 content navigation service 层，出现在那一层不算 split 契约回退。图片 reopen 仍走 `content_open` 本身：成功结果返回结构化 payload（如 `content_kind=image`、`multimodal_open_pending=true`、`runtime_image_target`），由运行时决定是否把视觉内容附带到下一次模型请求；是否允许取决于**这一发落到的那颗模型**的 `image_multimodal_enabled`（判据与装配层、咽喉点同源，见 `web-and-admin.md`「Image Upload Gating」），判为收不了图时返回点名该配置的 `当前模型配置名称是(xxx)没有勾选多模态能力因此无法打开图片…`，文案给出的出路只指向**本会话**的模型选择并明写不要为此改全局模型链（模型可见文案里的动作指引就是写路径，泛指"换配置"会被模型当成改全局 CEO 链的指令）；历史上下文里的图片 `path` / `ref` 只是 reopen 入口，重新查看像素仍需再次调用。path 形态的图片目标与文本目标共用同一存在性校验：路径不存在或不是文件时直接返回 `ok:false`（`path not found: …` / `path is not a file: …`），不返回 `ok:true` + 待附带承诺——路径写错必须在当轮就以工具错误回到模型手里。真正读字节的时机在下一次请求的图像 overlay 构造处，而那一点在工具错误道之外：那里抛出的异常不会变成 `Error executing <tool>` 结果，会一路撞到节点执行的兜底 except 并把整个节点转成 error-pause，同时待附带队列只是请求期局部状态、不落帧，resume 后图片静默丢失而模型手里仍留着那条 `ok:true`。所以 overlay 按单张图降级：读失败（缺文件、非文件、不可读）在该图位置放一条 `[图片 <display_name> …未能附带]` 文本说明并跳过，其余图片照常附带。frontdoor 道额外把超过 5 MiB 的目标图压缩到上限以内（先降 JPEG 质量、不够再降分辨率），压不进上限时同样跳过该图并附文本说明。单张图问题不中断整轮、不波及后续轮次。`content_open` 节选与 `content_search` 结果的 inline 守卫只量正文 `excerpt` 字符、不计元数据：正文超过当次模式上限（行模式 16000 / 字符模式 128000）才外置为新 artifact，模型只拿到 summary + ref（超大结果以 content envelope 出现在上下文里属于预期交付）。元数据不计入正文上限，避免“节选 + 元数据”合计撑过闸门被误外部化、触发打开→外部化→指回原 ref 的循环。`content_open` 节选按字符上限行对齐取整：行模式上限 `OPEN_EXCERPT_CHAR_LIMIT`（16000）、字符模式上限 `CHAR_MODE_OPEN_CHAR_LIMIT`（128000），只回显完整行、不在行中途截断；未传寻址参数时从第 1 行起按上限行对齐读取（小文件一次读全）。单行超长内容走行字符兜底：截断到上限并提示改用 `start_char` / `end_char` 分页，MB 级单行文件提示改用 `exec` 定向提取，绝不把整行内联进上下文；显式 `start_line` / `end_line` 区间仍受 `MAX_OPEN_LINES`（200 行）span 约束。 |
| `filesystem_*` | 每个变更工具各是一条独立的治理记录，也只以这些名字被调用与授权：`filesystem_write`（整文件“创建或替换”）、`filesystem_edit`（单文件文本区域替换，契约见下一行）、`filesystem_copy` / `filesystem_move`（`operations=[{source, destination}, ...]`）、`filesystem_delete`（`paths=[...]`）、`filesystem_propose_patch`。`copy` / `move` / `delete` 是路径级批量操作，变更单位本来就是整个文件系统对象路径，不采用文本区域定位契约。每条记录的资源 uri 是 `negi://resource/tool/<tool_id>`（写侧只出这个方案名，已沉淀在转录与工具结果里的 `g3ku://…` 继续按同一规则识别）。 | 授权按被调用的那一条工具判（`filesystem_write` 的 `allowed_roles` 只管写）。所有路径必须绝对路径，并先经过现有 workspace policy（拦截系统临时目录与 legacy `tmp/`、`.g3ku/tmp`；`tools/` 只放行 resource.yaml/main/toolskills 注册内容；`tmp_*` / `temp_*` 前缀命名——含点前缀 `.tmp_*` / `.temp_*`——以及日志、压缩包、下载产物等临时工件必须落在 temp_root（runtime 注入的 `task_temp_dir`，缺省 `<数据根>/temp`）或 `externaltools/` 下）。目录 `copy` / `move` 只允许目标路径不存在；目录 `delete` 必须显式 `recursive=true`。不存在可调用的 legacy 兼容入口（如 `filesystem(action=...)`、`filesystem.search`、`filesystem.open`）。 `filesystem_stat` 是目录清单与度量的归属工具（`paths` 收到目录时返回 `entries` 与整树聚合），它只列与量、不搜文件正文；content path 模式拒绝目录时给出的两条出路里，列目录归它，跨文件正文检索归 `exec`。 |
| `filesystem_edit` | 模型可见契约是**平面必填字段对**：`path + old_text + new_text`，不含任何 object 字段。`old_text` 须逐字节匹配且唯一，用相邻行扩到唯一为止；`new_text: ""` 即删除该区域。定位方式只有这一条车道，因此没有 `by` / `mode` 判别器、也没有“车道互斥”的散文约束。成功文本回显被替换的行区间（`resolved lines N-M`），调用方不必重读文件即可确认改在了哪一段。 | 校验面 `parameters` 有意宽于模型面：`target.by` ∈ `exact_text` / `anchor_pair` / `line_range` 与 `mode + old_text/new_text`、`mode + start_line/end_line/replacement` 仍是可执行车道，历史转录与 adapter 形状继续可跑（`anchor_pair` 车道因此仍然存在，只是不向模型曝光——模型面没有 object，就不存在“把替换文本塞进定位器对象”这个错法）；判定 legacy 类别前先剥离对侧车道的占位值（`start_line=0` / `replacement=""` 等）吸收自动填充噪声，但 `target + legacy 字段` 不是受支持组合。模型面不出现 object 字段的原因：provider schema 管线无法表达互斥与条件必填（组合关键字会被压平），所以定位变体只能拆字段车道或拆工具，不能靠散文约束。重复提交同一编辑安全：`old_text` 已不在文件而 `new_text` 唯一在盘（锚点车道为区域已等于 `new_text`）时返回 `Already applied:` 且不写文件，属成功车道；`main/runtime/recovery_check.py` 判定“结果丢失但磁盘已生效”用同一条判据。`load_tool_context` / `get_tool_toolskill` 的参数摘要与示例只渲染模型面。 |

| `silent` | CEO/frontdoor 专属的常驻内置控制工具：调用它 = 本轮不向用户投递任何回复，同时把正文留在上下文里（参数 `reason` 必填，`subject` / `superseded_by` 可选，三者都进转录行元数据供审计与后续轮次判读）。它是回合终态：同批其余工具照常执行完，随后直接收尾，不回模型。节点侧不暴露。合同与理由见 `runtime-overview.md`「3.3 静默回复（`silent` 工具）」。 | 常驻靠一条独立机制，不靠 `RESERVED_INTERNAL_TOOLS`，也不靠 `CEO_FIXED_BUILTIN_TOOL_NAMES`——那两个集合都只保证"已可见时排在本轮 callable 最前面"，**不具备注入能力**（`stop_tool_execution` 就是反例：它没有 `tools/<name>/resource.yaml`，也不在默认治理表里，因此根本进不了 exposure 名单）。真正让它恒定可用的是**四处**：① 执行侧的工具对象字典——`_frontdoor_execution_bundle` 的 `all_tools` 里与 `submit_next_stage` 一起就地构造，不依赖注册表；② `ALWAYS_CALLABLE_INTERNAL_TOOLS`（不要求已可见）；③④ exposure 与 runtime 可见集两处的恒定点名。顺序上一律追加在真实能力之后、`submit_next_stage` 之前，两条路径同一位置口径。它不参与阶段预算、不受阶段闸门拦截，所以没有活动阶段时也能收尾——但这条豁免**必须同时写在提示词里**（`ceo_frontdoor.md` 的静默条目与契约的 `silent_help` 都点名"无需先开阶段、不要为静默建阶段"），因为阶段协议那句"没活动阶段就先 `submit_next_stage`"字面上覆盖所有工具：实盘出现过模型为静默一轮专门建一个"静默收尾"阶段、白占一次工具轮。纯内置工具不在 `tools/` 目录下，因此不计入资源管理器的工具计数、也不被 RBAC 的 `supported` 集合返回——这四处恒定注入正是为了绕过这一点，**漏掉任何一处都会出现"模型看得见名字却调不动"**：实盘的失败形状就是②③④齐全而①缺失——模型确实发出了 `silent` 调用、被 `_run_single` 的 `visible_tools.get()` 判成 `Error: tool not available: silent`。名字可见 ≠ 对象可解析。名字出现在 callable 名单时，契约另渲染一行 `silent_help:` 告诉模型何时用它，并说明"静默没有文本写法"——措辞里刻意不重提被禁的字面串：在纠正里拼写它等于把它送回模型上下文（`runtime-overview.md`「3.3 静默回复（`silent` 工具）」）。 |

### 3.1.1 为什么"常驻"需要独立机制

`fixed builtin`（不参与候选挑选）与"恒定 callable/visible"（一定进 provider `tools[]`）是两件事，容易混为一谈：

- `CEO_FIXED_BUILTIN_TOOL_NAMES` 与 `RESERVED_INTERNAL_TOOLS` 的作用点都在**已经可见**的名单内部：`_callable_tool_names` 把其中可见的名字排在**本轮 callable 最前面**（先 RESERVED 再 FIXED，然后才是契约在场的名字），所以它们不进取予候选行；同一对集合又把它们从在场读数里剔掉（`CEO_NEVER_CONTRACT_TOOL_NAMES` / `NODE_NEVER_CONTRACT_TOOL_NAMES`），免得常驻名被当成"本轮靠契约块进来的"。两者都会跳过不在 exposure 名单里的名字。
- 让一个不在 `tools/` 注册表里的工具恒定出现，需要：① 在工具注册表注册实例（否则 provider 侧解析不到 schema，会静默丢条目）；② 一条不看 exposure 的追加名单（`ALWAYS_CALLABLE_INTERNAL_TOOLS`）；③ 前门 callable 与 runtime-visible 两条状态路径各自恒定点名。

新增内置控制工具时按这三步核对，少一步的症状是"模型从不使用它"或"调了就报错"，都不会指向注册环节。

### 3.2 candidate tools

候选工具是“本轮推荐给 agent 的具体工具列表”，默认可见但不可直接调用；需要先 `load_tool_context(tool_id="...")`。来源链路：资源注册表 → RBAC 过滤 → 检索/排序 → 节点上下文选择。

当前选择规则：

- `candidate_tool_names` 的上界是 RBAC 治理可见集，**不是**本轮已构建的可执行对象字典：候选 = 治理可见工具 −（本轮 callable ∪ 已提升）。候选生成只做一次集合差。`candidate_skill_ids` 与它同一语义。
- 由此得到一条必须成立的三态划分：任一治理可见的工具在任一时刻恰属于「本轮契约在场、直接执行」/「本轮不在场、调用即交回契约」/「不可见（RBAC 关闭或 repair-required）」之一。第四态（名字看得见、调用既不通也不给契约）才是合同被破坏，排查路径见 `context-and-cache-troubleshooting.md`「节点侧排查要点」与 `tool-hydration-and-callable-chain.md`「契约在场与 callable」。
- 普通候选数量由 RBAC 可见工具决定，节点与 CEO/frontdoor 都不截断、不排名。
- 候选集与候选顺序只由集合差决定：两条车道都按治理可见集的原有顺序给出，不按 query 增减意图权重、不打分。写意图 query 下 `filesystem_write` / `filesystem_edit` / `filesystem_delete` / `filesystem_move` / `filesystem_copy` / `filesystem_propose_patch` 出现在候选里，是因为它们在可见集里且尚未提升，不是因为被排序提前；`exec` 是固定 builtin、常驻 callable，所以任何意图下它都不进取予候选。唯一还按 query 排序的地方是 `load_tool_context(search_query=...)` 的搜索面（`runtime_service._search_visible_tools`），它是搜索结果的排序，不参与候选生成。

加载门控：

- 精确 `tool_id` 加载对两类 concrete tool 开放：当前 canonical `candidate_tool_names` 中的，以及当前 `rbac_visible_tool_names` 中仍然 surfaced 的，都可以读取 toolskill / 参数说明。`load_tool_context(search_query=...)` 维持可见工具搜索路径，不是枚举所有 RBAC 可见工具的 API。两条加载门禁都有同一条实时回退：工具与 skill 的候选/可见快照未命中时，回退查询实时治理可见集（工具 `list_visible_tools`、skill `list_contract_visible_skill_resources`），命中即放行到服务层加载——这条回退让任务运行中途新注册的资源（如 `skill-installer` 在节点运行中装入的、addtools 装上的 MCP 工具）对当前车道立即可判定，RBAC 与 repair-required 语义仍由 `load_tool_context_v2` / `load_skill_context_v2` 强制；两边都未命中才返回门禁错误，该错误因此只表示"未注册/不可见"，不表示快照滞后。
- 工具只有一类：可执行资源，`main/tool.py` 必须存在。第三方能力的本机安装放在 `externaltools/<tool_id>/`，由 `exec` 或某个工具的 handler 驱动；声明 `tool_type: external` 的清单在发现阶段就被拒，错误文案点名两条出路（包成工具、或改成 skill）。把外部 MCP 服务接成工具的合同归 `skills/add-tool/references/mcp-client-tool.md`，本文件不复述。`install_dir` 仍被读取，但不属于任何清单要求：它只用于删除一条陈旧记录时连带收走它的安装目录。
- 门禁与"不可用"类错误文本必须自带可判定信息，不能把模型推回猜名字：`load_tool_context` 的候选/可见门禁拒绝要枚举它自己要求的那份名单（`本轮候选工具：…` / `本轮候选技能：…`，有界上限、超出只报总数）；被拒名字若是该 `actor_role` 的 fixed builtin（`exec` 等常驻内置工具，本就没有 toolskill），先答"无需加载即可直接调用"再说别的——模型常把常驻工具当候选去加载。`tool not available` 要区分「本轮只是候选、尚未水化 → 先 `load_tool_context`」与「既不可调用也不在候选里」，并分别给出动作与两个名单。三条渲染入口共用 `g3ku/runtime/tool_error_guidance` 的 `availability_hint` / `no_load_needed_hint` / `format_name_group`，各调用点不自拼名单。
- `load_tool_context` 只做一件事：把该工具的契约块送进上下文。它不跑入参校验、不落暂存、不"提升"任何状态——对已经 callable、契约已在场、fixed builtin，或当前仅 RBAC 可见但不在 candidate 里的 direct-load lane，它都只是 read-only toolskill 加载。
- 例外：运行时还有一条免模型的复开提醒——`exec` 结果发生输出截断（`stdout_truncated` / `stderr_truncated`）时，运行时在这条 exec 结果里注入 `content_open` 的复开方法，让模型直接从"立即可用但截断的 exec"切换到"能按行/字符精确读本地文件的 content_open"，不必先手动 `load_tool_context`。契约不在场时那一次 `content_open` 调用走投递路径，同一跳把契约拿回来。同一结果只注入一次。
- 节点的可执行对象字典（`_tool_provider` 交付给执行循环的 `tools`）按治理可见集兜底构建，frame 里的候选被写成空时也不塌缩。这一条与上一条是一对：候选决定"能不能被提升"，对象字典决定"提升后能不能真被解析执行"，两者各自按治理可见集取上界，才不会出现"名字可见却调不动"。
- repair-required 资源从普通候选中剥离：工具进 `repair_required_tools`（不进 agent-facing `candidate_tools`，也从在场判据里剔掉，所以永不进 callable），skill 进 `repair_required_skills`（机器状态，留在合同 payload 里供诊断；渲染行已从尾块删除——skill 待修复的信号由 `load_skill_context` 的 repair 载荷承载）。这些列表不影响 provider-facing `tools[]` 变化。

exec 与 memory 工具：

- 部分工具同时是资源支撑与固定内置（CEO 的 `exec` / `memory_update` / `memory_note`，节点的 `exec` / loader tools），通过 fixed-builtin 路径直接可调用。
- `exec` 除 RBAC 外还有一条契约轴：surfaced 工具 `exec` 可携带持久化 `metadata.execution_mode`；`governed` 保留 exec 侧守卫，`full_access` 移除 exec 侧 read-only / 路径 / 安全检查，但不绕过 Tool Admin 启用状态与 RBAC。当前模式的权威暴露位置是运行时工具合同 / `load_tool_context` payload。governed 内部分两层：**路径监禁层**（temp/系统路径策略与工作区边界）永不可豁免；**命令形态层**（只读约束 + 破坏性命令黑名单，黑名单默认开启）命中后有两条放行通道——命令白名单豁免（归一化锚定模板匹配，作用域 all/ceo/tasks）或操作者审批（管理端 `/resources/tools/exec-*` 端点裁决；审批请求持久化于 governance sqlite，执行进程轮询等待、web 管理台裁决，天然跨进程；等待到期自动拒绝，时长可配置，近窗连续未获批的同命令快速拒绝防刷屏）。形态层的命令起始锚点按「语句起始」匹配，换行与 `;` `&` `|` 同等算语句分隔（正则带 MULTILINE，行首缩进不算豁免），所以 `Get-Content a\nremove-item b` 这类分行写法不会绕过守卫；改这些锚点时必须同时保持只读约束与黑名单两条判据。白名单与审批的裁决权只在管理端，模型侧无任何自我豁免参数。**另有不可绕过的宿主进程完整性边界**：`exec` 在进入 execution mode、白名单和审批判定之前，拒绝常见的进程终止表达（PowerShell `Stop-Process`/`Spps`、Windows `taskkill`、POSIX `pkill`/`killall`/`kill`、Python `os.kill`/`signal.kill`，以及 WMI/CIM terminate/delete 等）；该边界在 `full_access`、白名单和审批下仍生效。只读 `Get-Process` / `ps` 等检查允许。它是命令形态防护，不是 OS 沙箱；允许任意 native code、动态解码或外部二进制时，仍必须用独立 worker/container/低权限账户隔离。`exec` 自己启动的子进程应通过 `timeout_seconds`、任务 pause/cancel 或进程树收尾机制清理，不应让任务按 PID/名称清理宿主。
- `exec` 是发现/探测工具（目录结构、文件名搜索、环境检查）；具体本地文件正文证据应来自 `content_open(path=..., start_line, end_line)`。`exec` 的 agent-facing payload 按内联额度交付：捕获到的正文不超过 `_EXEC_INLINE_CHAR_LIMIT`（5000 字符）时**整篇**放进 `head_preview`，`tail_preview` 只承载运行时指引；超限时 exec 不做任何切割——整篇留在载荷里，由工具结果的统一外置闸门（`content.navigation.INLINE_CHAR_LIMIT` = 6000，按序列化长度判）把它换成信封：信封带 6 行 / ≤800 字符预览、成败标注与 `content_open(ref=...)` 指引，正文一字不丢地存在 artifact 里。额度必须落在通用闸门之下，否则 exec 自己凑齐的整篇会被信封缩回 800 字符。`output_truncated` 是"本次没有整篇交付"的读数；它与 `stdout_truncated` / `stderr_truncated`（64 KB 捕获环有没有丢中段）不同源——正文短于捕获环但超出内联额度时只有前者为真。捕获环丢过中段（>64 KB）时，保留的头环与尾环拼成一份正文并在中间插入「…（超出捕获上限，中段未保留）…」标记，这份拼接就是可复开的全部内容。节点反复用 `exec` 提取源码片段时，应把引导转向 `content_open`；运行时代模型完成那次转向的机制见 `tool-hydration-and-callable-chain.md`「交付被切割时的复开提醒」。两个截断标志不同源：`output_truncated` 说的是**这次交付**有没有把内容留给模型之外，`stdout_truncated` / `stderr_truncated` 说的是 64 KB 捕获环有没有丢中段——正文短于捕获环但超出内联额度时只有前者为真。捕获环丢过中段（>64 KB）时，`output_ref` 指向的是保留下来的头尾拼接，正文里会显式标出中段未保留；这种拼接是唯一事实源，不存在第二份"完整 stdout"。节点反复用 `exec` 提取源码片段时，应把引导转向 `content_open`。运行时代模型完成那次转向的机制见 `tool-hydration-and-callable-chain.md`「交付被切割时的复开提醒」。
- `exec` 解码子进程 stdout/stderr 优先 UTF-8，Windows 上先回退宿主首选代码页再替换字节；Windows 子进程 Python 命令注入 `PYTHONIOENCODING=utf-8`（只稳定 Python traceback / `print()` 输出，不改变 RBAC 或 `execution_mode`）；文件系统校验命令等子进程车道共用同一输出解码 helper——某条 Windows 路径仍乱码时，先确认它是否走了共享 subprocess-text helper。
- committed 长期记忆通过注入的 `MEMORY.md` 快照交付（**逐字节原样**：一条记忆一行 `YYMMDD:正文 (note: <句柄>)`，块里没有 id、没有来源标记，前门不替它做任何再派生，合同见 `runtime-overview.md`「Memory Runtime State」）；agent-facing 契约没有记忆检索工具，`memory_note(ref)` 是唯一的按需详细记忆加载器，它同时解析 `memory/notes/` 的普通 note 与 `memory/index/` 的分类 note，参数收的是常驻块里那个句柄——前缀就是目录（`n` 普通、`c` 分类），裸真名同样解析得到。它免活动阶段（见 `tool-hydration-and-callable-chain.md`「阶段门控与 callable 收紧」），用法要求写在工具自己的 `description` 里而不是前门提示词里——provider 边界会剥掉参数级 `description`，只有函数级描述送达模型，所以参数形状与"不要拼造名字"这类要求必须住在 schema，提示词只保留"回答前必须先查 note"这条行为要求。形状那句话要写对：常驻行印的是 ` (note: <句柄>)` 这一整截，描述里说"照块上显示的原样传"就会让模型连标签一起交上来（实盘一个回合里 3/3 次读笔记全失败就是这么造的），所以 schema 明说传**标签后面那一段**、真名不带 `.md`；同时 `load_note` 对 `(note: <句柄>)`、`note:<句柄>`、`<真名>.md` 这三种照抄形状归一——与其赌措辞，不如在读侧容错。管理面 URL 门不做这种宽容，畸形 ref 在那里要响亮失败。节点执行/验收路径不注入额外记忆检索块。
- `memory_update` 是 CEO 唯一的记忆写入口，也是唯一的 queue-submit 记忆工具：`memory_update(content=...)` 收自然语言（要记住、要修改、还是要忘记都写在这里），只请求记忆运行时稍后批处理，不在当前轮同步改写 committed 记忆，也不传记忆 id——常驻块里根本没有 id 可传。"这次调用只是提交，不能说已经记住/改好/删掉"这条要求住在工具自己的 `description` 里：它是每次调用都在场的形状，而前门提示词只写行为要求。实际改写/删除决策委托给带受限工具面的内部记忆 agent（不属于常规 agent-facing 目录，也不按 surfaced Tool Admin family 排查）；它把描述解析成当前常驻块的**行号**，并可能就实质影响该批次的行报告 `inspired_lines`。行号够不到归并区的分类行（那些记忆已离开常驻块），那一节整节摘除走 `category_deletes` 按句柄寻址，合同见 `runtime-overview.md`「Memory Runtime State」。

状态与显示层：

- 对 CEO/frontdoor，`candidate_tool_names` / `candidate_skill_ids` 属于 internal canonical state；暴露给模型的当前轮显示合同是两份运行时块：回合内不变的 `frontdoor_runtime_tool_contract`（待修复条目、附件句柄、临时目录、执行策略）与每跳重写的 `frontdoor_runtime_stage_gate`（已声明但无权限 / 活动阶段），前者排后者的前面、后者在请求体末位。**候选工具整表与 callable 名单都不进尾块**：模型能发出的名字由 `tools[]` 承载，能不能真执行由它自己上下文里的契约块决定，"本轮真可调"因此不是运行时必须重印的一行（尾块若逐跳抄它，等于每跳重写一段身后全是存活正文的字节）；剩下两行都是"拦得住"的面——`denied_tools` 是带着参数表但无权限，`undeclared_candidates` 是能 load 但参数表还没进本次 `tools[]`。两条车道共用 `render_candidate_tool_line`，差集为空时整行不出现；装配路拿不到本次声明名单时退回渲全量并沿用 `candidate_tools` 标签——省略的前提是"可推导"成立，证明不了就宁可重复不可缺失。工具的说明文字由 provider `tools[]` 的 `function.description` 承载（同一请求里不抄第二份），待修复与附件的处置文案住在稳定提示词（前门 `ceo_frontdoor.md`、节点 `shared_repair_required.md`），尾块只留条目行。旧轮 candidate/tool/skill catalog 不进入 durable history，后续轮次只继承真实工具调用轨迹与上下文。
- `candidate_tool_names` 是运行时去重、在场判据排除、恢复和 gate 判断用的 canonical name list；`candidate_tool_items`（`{tool_id, description}`）只是它的显示层缓存，用于 contract rebuild / refresh 后保留描述文本。agent 只应看到结构化 `candidate_tools`；`candidate_tool_items` 不是第二份权威候选集——canonical `candidate_tool_names=[]` 时，agent-facing `candidate_tools` 也必须为空，不从旧 contract、旧 items 或旧动态消息把失效候选补回 prompt。
- 对执行/验收节点，canonical `candidate_skill_ids` 落在 runtime frame，`candidate_skill_items` 随 frame 持久化，供阶段切换、prompt compaction 之后的下一轮 contract 刷新从 frame 恢复。`_enrich_node_messages()` 注入、且已携带 `candidate_skills` / `contract_visible_skill_ids` / `skill_visibility_diagnostics` 的 fresh skill 合同是 first-turn truth source：默认空 bootstrap frame 只负责占位与 phase 跟踪，不能把这些字段覆写成空；同一 turn 内 `_prepare_messages()` 裁掉尾部合同消息后，fresh skill 合同摘要沿 runtime context 继续传给 `react_loop`。
- 字段级排障入口：`contract_visible_skill_ids` 是 `runtime_service._node_context_selection_inputs()` 当轮记下的 contract-visible skill 快照（输入层证据，随 runtime frame 与 `runtime-frame-messages:{node_id}` artifact 落盘）；`skill_visibility_diagnostics.entries` 携带 `registry_skill_ids` 与逐 skill 的 `enabled` / `available` / `allowed_for_actor_role` / `policy_effect` / `included_in_contract_visible`，用于定位是 live `resource_registry`、`allowed_roles` 还是治理策略拦掉了 skill；节点 context selection cache 与 `persisted_frame_router` 都带 live-visibility freshness gate——复用旧 selection 前重新对照当前 `session_key` / `actor_role` / `visible_tool_names` / `contract_visible_skill_ids` / `registry_skill_ids`，一旦漂移就丢弃旧 selection，重新跑 `_node_context_selection_inputs()` 与 `build_node_context_selection(...)`。这挡的是“外部 resource/governance refresh 改了可见性，但节点长期沿用旧 cache / 旧 frame”的回归（尤其“首轮 skill 可见集为空，后续轮次一直空”）。
- 前门候选生成诊断同步在 session snapshot 的 `frontdoor_selection_debug`：`tool_selection` 回答命中项为什么没进最终 `candidate_tool_names`。

### 3.3 candidate skills

候选 skill 与 candidate tools 类似，但机制更轻：

- skill 不进入 tool callable 集合
- 需要显式 `load_skill_context(skill_id="...")`
- skill 加载是“当前轮立即消费正文”
- 不走契约在场判据（skill 没有 callable 面可摘）
- 对 CEO/frontdoor，skill 的可读入口是头部钉住块的全量名单（`id | name | description`），加上重印点之间新变为可读时落进历史的一次性提示（见本文「Pinned Static Declarations」）；`load_skill_context(skill_id="...")` 按 id 直读正文，避免模型把它们误读成需要先加载才可用的候选工具
- repair-required skill 有更强的门控：它仍可作为“待修复资源”出现在 agent-facing `repair_required_skills` 中，但修复完成前 `load_skill_context(...)` / `load_skill_context_v2(...)` 直接返回 repair-required 错误与修复指引（`skill_repair_required` payload 携带 `warnings` / `errors` / `next_actions`），不返回正文；遇到“模型知道这个 skill 存在却无法 load”，先检查 skill 资源本身的 `available` / warnings / errors，而不是先怀疑 selector 没选中
- 节点运行中的 skill 自愈闭环依赖上面两条语义配合：候选快照在节点派发时定格（persisted frame），中途新装 skill 靠加载门禁的实时治理可见性回退获得真实状态——可加载则返回正文，`available=false`（如 `missing required bins`：`requires.bins` 声明了 `shutil.which` 解析不到的命令）则返回修复指引；节点用 `filesystem_*` 修正 manifest 声明或用 `exec` 补依赖（filesystem mutation 自动触发 `refresh_resource_paths` 重探可用性），再次 load 复核。改 `requires` 的判据只有两条——该项在本机实测已有，或该 skill 正文并不使用该项：可用性正是由这些项逐项算出（`shutil.which` / 环境变量 / 已注册工具），删掉一项等于让 `load_skill_context` 直接返回正文，所以"再 load 一次看有没有正文"这道复核对删声明无效；反删规则同时写在前门 §1.3、执行/验收节点共享修复块与 `create_async_task` 的实测可加载验收门槛三处，缺一处就留一条能把真实缺口报成已修复的路径。修复规则文本由 `main/prompts/shared_repair_required.md`（执行/验收节点提示词共享块）、`tools/skill-installer/toolskills/SKILL.md`（安装后三态复核）与 `skills/skill-creator/references/g3ku-resource-spec.md`（创建后三态复核 + `requires` 探测声明规则）承载

### 3.4 契约在场与可调用面

一个工具在这一跳可调用，当且仅当它是常驻内置，或它的 toolskill 契约标记块在这一跳的模型可见请求视图里在场。没有台账、没有提升步骤、没有跨轮状态：在场判据按当次请求视图现算，每跳一次，结果下传给执行准入、契约投递与观测读数三个消费点。块有三条投递道——`load_tool_context(_v2)`、契约不在场时的调用回执、以及派发时由 `seed_tools` 声明的预置——三条产出同一个块，判据不知道是谁送的。契约、投递、入参暂存与 `reuse_args` 批量执行、派发时预置契约、参数错误、外置结果信封、统一 timeout 与阶段门控的合同见 `tool-hydration-and-callable-chain.md`。

模型面**没有**一行"本轮可调用名单"。这是有意的：契约不在场不拦路，所以那份名单不决定模型能不能发出一次调用，逐跳重印它只会让 `tools[]` 之外的尾部每跳重写。模型可发出的名字由 provider `tools[]` 的声明面承载，真不真执行由它自己上下文里的契约块决定——两条都在它眼前，都不需要运行时再抄一份。

推论两条，排查时按这两条走：

- 某工具的 toolskill 被阶段裁撤或压缩移出上下文 ⇒ 它当跳就不在场，直接调用它拿到的是契约投递（那一跳不执行、入参被暂存），下一跳起照旧可调。这条全程无报错、无通知，也不改动 `tools[]` 的参数表——**它不是缺陷的形状**。想知道"为什么这一跳没执行"，看那一跳请求视图里有没有该工具的 `[TOOL_CONTRACT:…]` 块，或读 `tool_contract_presence` 读数。
- 权限收回与资源下架是**唯一会当场调不动**的成因，也是尾块必须显式点名的那一面：两条车道的活状态块都渲 `denied_tools`，因为这类名字带着参数表却永远不放行，不说的话模型只能靠反复撞拒绝去猜。

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

- 治理单位与执行单位不一致，agent 语义与运维读数会各自漂移
- 候选池与真实可调用集边界模糊
- skill 与 tool 的加载模型不一致

因此工具是**唯一单位**，一条链路上只有一个名字：

- `tools/<name>/resource.yaml` 一个目录 = 一个工具 = 一条治理记录 = 一个 `tool_id` = 一个可调用执行器。
- Tool 管理台一行一个工具；RBAC（`allowed_roles`）、启停（`enabled`）、删除、核心保护全部按这一条判。
- 同一个名字同时是治理记录 id、注册表实例名与模型面调用名，三者不换拼写。

模型面没有“分组调用”这条路：`load_tool_context` 与 provider `tools[]` 只认工具名。递一个目录名之外的名字（例如历史文本里的 `filesystem`、`task_runtime`、`skill_access`）取不到治理记录，按“当前运行时未将其暴露为可加载工具”拒绝，并在错误里给出本轮候选与 RBAC 可见两份名单；不要靠再加一层别名把族名接回来，那等于把已经取消的第二名字空间偷偷放回模型面。

`filesystem_*` 是这条边界最典型的例子：

- 写、编辑、复制、移动、删除、补丁、测量各是一条记录、一次授权、一个开关。
- `filesystem_write` 的 `allowed_roles` 只管写这一条；`filesystem_stat` 可以单独放行检验角色而不同同时放开写类工具。
- 不存在可调用或可加载的分组入口（没有 `filesystem(action=...)`、`filesystem.search`，也没有 `load_tool_context("filesystem")`）。

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
- 忘记"契约不在场那一跳不执行"这条语义。
- 把治理记录名当成分组入口去 load（治理记录 id 就是执行器名，没有分组层）。
- 误以为 `ToolRegistry` 决定了 RBAC；实际上它只负责执行层。
- 在 CEO frontdoor 中，只盯可见 skills / candidate tools，而忽略了 `ceo_frontdoor.md` 里的 stage-first 稳定协议。
- 把动态 skill/tool 暴露块里的“加载说明”误读成无条件立即执行指令；实际上它们仍受活动阶段存在与否的约束。
- 把节点 `runtime_environment.path_policy` 里的 content 路径约束误读成“content 工具总要传 `path`”；实际上 `artifact:` 必须走 `ref`，只有本地文件才走绝对 `path`。
- `content_*` 从新一轮 callable 列表消失时，先查候选选择与该工具的契约块是否还在请求视图里，而不是 fixed-builtin 暴露。
- 用 transcript 里 loader 调用次数推断阶段预算；`load_tool_context` / `load_skill_context` 不计入 `tool_rounds_used`。

## 7. 维护高风险区域

- `main/service/runtime_service.py`
  因为 fixed builtin、candidate、governance、契约在场都在这里汇合。

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
- This duplicate-call guard is distinct from the read-only retrieval guard: repeated read-only calls such as `content_describe` / `content_open` / `content_search`, and `task_progress` use their own repair-guidance path with its own repair messaging and escalation semantics.
- The filesystem tools do not participate in the read-only retrieval branch: the execution runtime only hydrates the mutation executors themselves; repeated filesystem mutations follow the ordinary duplicate-call soft-reject path, while retrieval-style guards remain reserved for read-only tools such as `content_*`, `task_progress`, and `task_node_detail`.
- If a node loops on one tool, inspect the transcript/tool messages first: a tool message with `status=reused` and `reason=duplicate_tool_call_in_same_turn` means that call was never executed, and its `same_as` points at the call whose result is authoritative; the absence of a fresh tool result may likewise mean the runtime intentionally rejected a duplicate call rather than that the tool executor failed.

`runtime-overview.md`「Repeated Tool Call Guard」一节是指向本节守卫的摘要引用。

## 9. Tool Admin RBAC For Surfaced Tools

There is an explicit maintenance boundary between:

- surfaced tools that appear in Tool Admin (`/api/resources/tools`, Tool management UI), and
- internal fixed tools that never appear there, such as `submit_next_stage`.

For surfaced tools, RBAC is the highest-priority access contract. Internal fixed tools remain outside this contract: they keep their runtime-only visibility rules, their access is not modeled through Tool Admin `allowed_roles`, and behavior questions about stage protocol tools such as `submit_next_stage` are debugged through the stage/runtime path rather than Tool Admin RBAC.

Current RBAC rules for surfaced tools:

- A tool record's `allowed_roles` is an exact persisted whitelist. An empty list means deny-all for that tool. Refresh, reload, reopen and store readback must preserve an explicit empty list; maintainers should treat `[]` as real state, not as “missing”.
- 启停、删除、核心保护都以工具为单位：`enabled` 只摘这一条工具的能力；`is_core` 由 `memory_runtime` 的 `assembly.core_tools`（写的是工具名）算出。核心工具**允许停用、不提供删除**（`core_tool_delete_forbidden`），并且没有任何自动拉起——运维关掉的核心能力会一直是关掉的，前门/节点会真的缺这项能力，这是放开而不是加护栏。
- 一条工具的权限变化不外溢。`load_skill_context` 与 `load_tool_context` 是两条独立权限记录，角色允许分叉。
- 读取类工具同理各自一条名单：`task_node_detail` 的 `allowed_roles` 对 `ceo` / `execution` / `inspection` 三角色开放，且**按维护者决定不设归属护栏**——任何节点只要报出真实的 `(task_id, node_id)` 就能读到该节点的完整记录，而 task_id 本就明文写在节点自己的引导消息里；这是决定不是待办，测试把它钉成断言，别在别处"顺手修回去"。该工具对 agent 固定返回 `full`（`detail_level` 已从工具 schema 移除，节点不需要为它做一次决策），`summary` 视图只保留给 Web REST 传输层（`main/api/rest.py` 的查询参数不变）。被阶段裁撤移出上下文的原始工具入参/出参**不靠这条道回读**：模型面的唯一出口是阶段块上的 `archive_ref` + `content_open`（合同见 `runtime-overview.md`「stage_compaction」）；这条道读的是同一条阶段的另一种全量轨迹形态——按 `stage.rounds[].tool_call_ids` 关联 `task_node_tool_results` 里逐条留存的 `arguments_text` / `output_ref`，账本与结果表都不因裁撤而减少。提示词不把它教成裁撤回读通道。
- If a tool is surfaced, its model visibility follows Tool Admin RBAC exactly. A surfaced fixed-builtin tool may still be listed in frontdoor or execution fixed-builtin sets, but it only becomes actually visible/callable when that tool's own `allowed_roles` allows it. When debugging “the tool still appears after I removed all roles”, inspect the persisted `tools` row and the derived `role_policy_matrix` first; there is no fallback to `ceo` or `execution`.
- If a fresh workspace shows an unexpected default role set, compare resource discovery governance with the first persisted `tools` row before debugging prompt assembly or frontend rendering.
- First-discovery seeding boundary (separate from persisted RBAC): when a tool is discovered for the first time and there is no persisted row for it yet, runtime seeds `allowed_roles` from that tool's discovery governance — either the resource-local `governance.allowed_roles`, or the implicit default mapping in `main/governance/action_mapper.py`（`DEFAULT_TOOL_GOVERNANCE`，按工具名一条）。Implicit-governance tools with no declared roles seed empty, which is why `[]` and “absent” must stay distinguishable. After that first persistence boundary, `tools.payload_json` becomes authoritative; reload/refresh must preserve operator edits, including an explicit persisted `[]`.
- One-time legacy repair boundary for older workspaces: before `governance_meta.implicit_tool_role_backfill_v1_applied` is set, refresh may backfill an older persisted empty `allowed_roles=[]` from the newly discovered non-empty default. Once that flag is written, later refreshes stop auto-healing empty lists.
- 分组形状的一次性迁移：治理库里还是旧形状（表 `tool_families`，一行一个分组、角色挂在 action 上）时，`GovernanceStore` 打开即按 `actions[].executor_names` 把它展开成一条一工具，运维编辑过的 `enabled` 与逐条角色随迁，`exec` 的 `metadata.execution_mode` 覆盖值也保住；旧分组行与旧的分组策略行删除，`governance_meta.tool_granularity_migration_v1_applied` 记下迁移条数。策略矩阵本身每次启动都按注册表重建，所以它是派生数据，不是迁移对象。
- 鉴权读法是按列窄查询（`actor_role` + `resource_kind='tool'` + `resource_id=工具名`；工具维度没有动作列，`action_id` 存 NULL）。因此有一个会被静默破坏的不变量：**`role_policy_matrix` 的 `actor_role` 列必须与 payload 的公开形态逐字一致**——写路径必须继续过 `to_public_actor_role`，把 `checker` / `acceptance` 这类别名原样写进列会让窄查询查不到策略而判成 `policy_denied`。这条改法的原因是一条实测：全表形态下几百行意味着每次判定都要 `json.loads` + pydantic 校验几百遍；窄查询后是微秒级。`list_role_policies()` 仍然保留，只给管理端列表与对账用。

前端与 API 侧职责详见 `web-and-admin.md`「Tool Admin RBAC Contract」。

### Skill Policy Role Contract

技能的角色名单与工具的角色名单共用写侧判据，但**空集语义是两条**：

- 写侧：`PUT /api/resources/skills/{skill_id}/policy` 的 `allowed_roles` 走与工具面同一个规范化器（`main/governance/roles.py` 的 `normalize_public_allowed_roles`）——旧别名 `checker` / `acceptance` 折叠成 `inspection`，重复值去重，**认不出的取值在门口 400 并点名该值**，不落治理库。记录模型 `allowed_roles` 是 Literal 列表，而 `model_copy` 不做校验，所以规范化必须发生在进入 `model_copy` 之前。
- 读侧：按行降级。`GovernanceStore._parse` 对每条存储行只保留认得的角色，丢掉的部分就该行发一条 WARNING（点名 `skill_id` 或 `tool_id` 与被丢的值），整张列表照出。一行坏值把 `GET /api/resources/skills` 顶成 500 是必须避免的形态——管理台与候选池都走这条读道，列表 500 之后没人能进去改它；刷新车道也经同一个 `_parse`，所以它既不能是纠错点，也不该是故障点。
- 空集回落：技能行解析后为空时读成 `['ceo', 'execution']`（`to_public_allowed_roles`），与工具面「`[]` 是真状态、按 deny-all 保留」相反。这条分叉是现行契约，不是待办；要统一两面判据得先改契约再改代码。

### Exec Runtime Mode Contract

- `exec` 是当前唯一带额外持久化模式字段的 surfaced 工具：`tools` 记录上的 `metadata.execution_mode`。
- The resource manifest may provide a default `settings.execution_mode`, but the persisted tool metadata is the runtime source of truth once an operator saves an override; resource refresh must preserve that override, otherwise Tool Admin would show one mode while runtime execution silently falls back to another.
- `load_tool_context("exec")`、node dynamic contracts 与 frontdoor dynamic contracts 必须给出同一份 `exec_runtime_policy`。不一致时先查那条持久化的工具记录，再查合同注入路径。

### CEO Regulatory Governance Mode

- Tool Admin also owns one global persisted switch for CEO/frontdoor: `ceo_frontdoor_regulatory_mode_enabled`. It is governance metadata used by CEO/frontdoor approval policy, not stored on a specific surfaced tool family.
- Its scope is narrower than generic Tool Admin RBAC: RBAC decides whether a surfaced tool is visible or callable at all; regulatory mode only decides whether already-visible medium/high-risk CEO tool calls must pause for batch human review.
- With the switch enabled: risky CEO tool calls are grouped into one `frontdoor_tool_approval_batch` interrupt; `review_items` enumerate only the risky calls that require operator review; the resume side must submit one complete `submit_batch_review` payload covering every `review_item` exactly once. Pass-through low-risk tool calls in the same original tool batch remain part of the runtime-owned original tool-call ordering — “review items” are not “all tool calls in the round”.
- Rejected risky calls do not disappear: CEO/frontdoor constructs synthetic rejection tool results and merges them back with approved real tool results in original tool-call order before the next model round.
- Changing the switch affects future approval boundaries immediately, even for already-running CEO sessions, but must not silently rewrite an approval batch that is already paused and waiting for review.

### Removal Of `message` / `messaging`

The surfaced `message` executor is absent from the resource/tool contract entirely: resource discovery finds no `tools/message/resource.yaml`, Tool Admin lists no such tool, and CEO/frontdoor fixed builtin exposure and the default `frontdoor_interrupt_tool_names` include no `message`; if it still appears in Tool Admin or in provider-facing web tool schemas, treat that as a contract regression rather than a disabled-by-default state. Browser/web replies travel through the websocket session path (`ceo.reply.final`, inflight snapshots, and related runtime events) and external-channel replies through the External Agent API event stream (`outbound.created`, see `external-agent-api.md`), so channel reply delivery is owned by those paths and independent of this tool-family contract.

### Provider Tool Surface

发给 provider 的 `tools[]` 声明清单（`provider_tool_names`）与"本轮真能调用"的 `tool_names` 是两份集合，两条车道都用这份区分做排查：`tool_names` 是当前轮权威 callable 合同；`provider_tool_names` 只决定 `tools[]` 里出现哪些名字与参数表。契约在场判定与阶段门控改动运行时合同尾块，不该每轮都重铺 `tools[]`。运行时合同是插在最新 user 消息之前的 system-role 摘要，不是 provider schema，也不是 durable history；模型若回显它，前门归一化只做一次私有修复，重复回显永不升级为最终输出。

两条车道发的是同一种形状，区别只在重印边界有几条。边界只有一条判据源：`g3ku/config/context_refresh.py` 的 `idle_boundary_exceeded()`，两条车道共用它，所以"多久没发"在两侧不可能读出不同的值。

- **CEO/frontdoor**：首跳按当轮 RBAC 可见工具全集播种，之后钉住。重印边界三条，全部是**消耗式**信号——内联压缩（preflight 落地时在同一 session 上挂 `PENDING_PROVIDER_BUNDLE_RECOMMIT_ATTR`，与手动压缩同一枚标记）、手动压缩（车道路挂同一枚标记，读一次即清）、**空闲边界**（距上一次真实发送超过 `contextRefresh.idleMinutes`）。**进程重启不在这三条里**：续跑第一跳沿用连续性侧车带回的那份钉住声明，借用它的判据只有"上一跳的 actual-request trace 带得起来"，与当轮 visible 集无关（停机期间增删资源都不改成员），恢复细节与读数见 `context-and-cache-troubleshooting.md`「重启后继续，不能退回 transcript/history fallback」。会话级残留的 `frontdoor_history_shrink_reason` 只作取证读数、不参与判据：一次压缩之后它每一轮都还在，当判据等于把"只有重印边界能改名册"稀释成"每个用户轮都能改"。编辑重发/Fork 截断到会话开头不是这三条之外的第四个判据（`_frontdoor_bundle_recommit_boundary` 对内部轮 flag 与截断都不认），它走的是"没有可沿用的声明"那一支：空基线载荷把那份钉住声明与水合/可见名单一起撤掉，并调用 `drop_pinned_contract_state` 清掉钉住头部的两份载体（会话属性 + 按 `session_key` 分桶的进程表），于是下一跳按当轮 RBAC 全集重新播种。截断契约见 `web-and-admin.md`「Message Edit-Resend And Session Fork」。**换车道不是边界**：心跳/定时内部轮与外部轮共用同一份冻结清单（一次车道切换不该顶掉身后整段正文的缓存），内部轮的工具面收窄由执行准入承担——`_frontdoor_dispatch_tool_names` 的派发面按当轮治理可见集收窄，声明面保持冻结，"看得见但调不动"是内部轮的预期形态。
- **执行/验收节点**：同样首跳按角色 RBAC 全集播种后钉住，重印边界两条——`token_compression` 与空闲边界（判据在 `ReActToolLoop._node_bundle_recommit_reason`，两条都命中时报 `token_compression`，因为那一跳正文已被整段重写、重印不额外破缓存）。空闲那条读数用回合内的局部变量计时，不落盘：进程重启本身就是重新播种的边界，落盘只会多一个每跳读数。帧里带不出钉住声明（`provider_tool_names` 空）时按当轮全集**重新播种并记 `provider_tool_bundle_seeded=true`**——选择器不拿 `model_visible_tool_names`（当轮 callable 视图，通常只有 5–8 条）顶替清单：顶替会把一次字段缺失变成"整段清单收成 callable 大小"的静默换束（实盘形态是同一条链的清单在 19↔20 之间来回摆，且台账上 `seeded` 看不出异常）。
- 两条车道的钉住都是**完全冻结**：重印边界之前一个名字不增删——节点只在 token 压缩那一跳重取清单（react_loop 压缩段重新播种），前门另加手动压缩与空闲窗口（前门多这两档是维护者按实测代价接受的交换：被收回的名字不该等到下次压缩才退场，空闲那一档确实重付头部那一截）；车道切换两侧都不重印。当轮新增能力**不进清单**：靠尾块 `undeclared_candidates` 点名，调用的参数契约由 `load_tool_context` 返回承载（`parameters_schema` / `parameter_contract_markdown` / `example_arguments`），声明在重印点一次性吸收。声明侧渲染脱离派发字典：判据是"这一跳渲不渲得出参数表"，不是"还在不在授权名单里"——派发字典会因为 RBAC 收回、契约不在场的水合撤销、候选塌缩而少一个名字，那三层都是执行准入的事；清单排在正文最前面，跟着删名等于在重印边界之外换束。名字留在清单里而参数表从哪儿取，见下面「参数表的来源」。

#### 参数表的来源

灰度开关 `G3KU_TOOLS_BUNDLE_SINGLE_SOURCE`（`g3ku/utils/helpers.py`，默认关）决定这一跳的参数表有多少个来源，两条车道读同一个函数——开关各读各的就会让两条道跑出不同形状的清单，而这条合同要买的正是"同一份声明在两条道上同形"。

- **开关打开：钉住整表是唯一来源。** 渲染＝按钉住名单的顺序从整表索引取字节，不查活实例、不查删除前快照、不读上一跳原文，**派发字典里的名字也一样**。于是运维在 Tool Admin 里改一颗工具的参数定义，在跑会话与在飞节点都在下一个重印边界才看到新字节；边界那一跳渲染时读实例（`ReActToolLoop._render_provider_tool_schemas(reprint=True)`、前门渲染器的边界/重建分支），编辑在那一刻被吸收并跟着写进新整表。没进派发字典的名字（被收回、本轮不合格）在边界跳仍取整表那份，否则一次无关的增删会顺带把别人的字节换掉。
- **开关关闭：多级回填。** 渲不出的名字按顺序补参数表，一共五档：① 声明面/注册表里现存的实例（RBAC 收回、撤回收窄时实例都还在）；② 实例已消失的（资源被删除）取删除前抄下的进程内快照；③ 帧里的逐名载体 `declaration_schema_backfill`；④ 清单变化那一跳存下的整表引用；⑤ 上一跳真正发出去的那份原文（节点取 `actual_request_ref` 工件的 `actual_tool_schemas`）。④ 排在 ⑤ 前面是因为原文那份会被逐跳剪掉，剪掉的那一刻它就成了空档。
- **整表是什么、落在哪儿。** 存的是**投影后的线上原字节**（渲染结果本身），读回来原样递出去，不重投影、不重排、不补默认值——重投影是"读路径与写回不同源"那一类缺陷的入口。节点侧落 `pinned_tool_schema_bundle` 这份 singleton 工件（同 `(task, node, kind)` 只一份、就地覆盖，且它不在 `task_actual_request` 的按-kind 剪枝集合里，所以逐跳剪枝与进程重启都动不到"当前钉住的那份"；终态清理按 kind 白名单删 artifact，它不在白名单里，会被删掉——那时该节点没有后续跳）。前门侧存会话级 singleton 文件 `.g3ku/web-ceo-tool-bundle/<digest session_key>.json`（与连续性侧车同寿命，会话删除时一起回收），侧车里只带 `pinned_tool_bundle_path` + `pinned_tool_bundle_revision`——整表约 16 KB，而基线载荷每跳都可能重写，所以本体不进侧车（同 `frontdoor_actual_request_path` 的分法）。同一份索引有两处载体：会话对象属性，以及按 `session_key` 分桶的进程表——同一回合内不同装配路拿到的 session 不是同一个实例，只认会话属性的话换一个对象就读不到整表，于是回退去读活实例，单源就在那个装配点破掉。
- **缓存键必须是 `(ref, revision)`。** singleton 就地覆盖复用同一个 ref/artifact_id，只按 ref 做进程内缓存会在重印之后继续供旧字节；revision 取落盘文本的 sha256。revision 为空（存量帧没这个字段）时不写缓存、每次读盘——宁可能一次多读也不供可能过期的内容。恢复只在进程载体为空且载荷真带这两个键时才写回，否则本回合刚重印的那一份会被旧文件抹掉。
- **写点与重建。** 整表只在名单真的变化那一跳重写：节点的写手在 `ReActToolLoop` 的重印分支里（换名单、重渲、写整表、记 revision 同一处完成——写在选择器里永远不触发，因为重印跳帧里带的还是旧名单），前门的写手挂在渲染器的边界分支与重建分支。名单未变而缺名（存量第一份整表还没有后来新增的成员、或整表读不出来）时**就地重建一次**：重建的来源次序按目的分两种——边界/播种先读实例（编辑该被吸收），冻结跳的兼容重建先原样复用整表、只为缺的那个名字读实例。
- **三类结果分开可数**（两条车道同形，节点装在 `model_visible_tool_selection_trace`，前门装在 actual-request 工件）：`bundle_ref` / `bundle_revision` 是这一跳字节源的身份，`bundle_compat_rebuild` 是兼容重建（每个在飞会话/节点最多一次），`bundle_fault` 是整表读不出或写不出，`bundle_placeholder_names` 是这一跳定影成占位的名字。写失败不静默降级、也不退回去读活实例：这一跳照旧按重建出来的字节发，配一条带 ref 与坐标的 WARNING。
- **读不出来的判定。** 满盘写坏过的载体是 0 字节，gzip-minimal 降级同理；这类形态一律判为"读不到"并走上面那次重建，**不得解析成空索引当作"名单为空"**——否则摘名，而摘名从那条的起始字节断一次前缀且每跳重摘直到下个边界。
- **占位。** 重建之后仍拿不到参数表的名字**不离开清单**：渲一份占位参数表（`retired_schemas.placeholder_tool_schema`，两条车道同一个生成器，空 `properties` + 一句"这跳没带出参数表，别猜参数"），并跟着定影进整表，所以下一跳逐字节复用同一份占位。占位与删名的断点位置完全相同，这一档不省缓存，管的是语义：清单成员只在重印边界变。开关打开后占位在发送道上才可达（闸门与渲染器同源之后才有名字落到占位），稳态跳不该有占位样本可找。
- **闸门与渲染器同源。** 前门侧这份名单在渲染之前先过一道可渲染性闸门 `_frontdoor_schema_renderable_tool_names`，判据＝活实例 ∪ 删除前快照 ∪ 整表命中。同源这条是硬的：闸门比渲染器窄一档，那些名字就会在到达渲染器之前被摘掉，整表与占位两档结构上不可达。闸门只放行、不改渲染顺序，所以它的存在不会让名单变宽。
- **前门 `names` 显式含两颗合成控制工具** `submit_next_stage` / `silent`：它们每次装配现场构造，既不在注册表也不在治理集，所以整表的 `names` 尾序必须写入这两颗——漏掉就是功能回归（模型看不见阶段工具/静默工具），不是缓存事件。它们的字节是类内常量，冻进整表的代价是"改这两颗工具的文案要等下一个重印边界"。
- 两份"抄下来的字节"形状不同，读数时别混：删除前快照存的是未归一的 `to_model_schema()`，而前门的参数表要经 `_provider_tool_schemas` 投影才上线，所以快照那一档在前门可能与线上字节有差；整表存的是**投影之后**的渲染结果，回读时原样递出去，因此它天生与上一跳逐字节相同。开关关闭时凡靠 ③④⑤ 才渲出来的名字同时写回逐名载体，下一跳不必再读盘。

漂移规则：

- 普通跳只有在成员集合真的变化时才重算 `tools[]`；同名不同序保持持久化顺序原样不动，不为零收益轮换 schema。
- `stage_compaction` 任何一侧都不得轮转 `tools[]`。重印点=压缩（`token_compression`）+ 空闲边界，前门另加"手动压缩"一条；换车道不是重印点。重印取的是"此刻按当轮 RBAC 重新播种会得到什么"，不是退回上一跳的窄清单。
- 清单滞后不是权限滞后，权限收回也不是删名的理由：两条车道的执行准入都与声明解耦——节点按当轮待派发字典查名，前门 `_frontdoor_dispatch_tool_names` = 钉住清单 ∩ 当轮治理可见（治理可见集取不到时退回当轮 callable pool，宁可窄不可宽），它是**可加载面**；模型可见 `tools[]` 的名字源是 state 里那份钉住清单本身——`_frontdoor_tool_schemas_for_state` 按钉住顺序渲染，参数表的取字节次序见上面「参数表的来源」，所以收回与撤窄都不改名册；准入名单另算（`_frontdoor_executable_tool_names` = 本轮 callable：常驻内置 + 契约在场的水合名；声明名再与当轮治理可见求交，非声明名不受回合初快照误拒，`tool_names` 键缺失的旁路才退回派发名单），声明里没 load 过的名字调用一律被拒并回 `load_tool_context` 指引，mid-turn 注册的工具 load 落账后经执行束放行、参数契约由 load 返回承载，`tools[]` 等重印边界才吸收它的参数表（钉死语义见 `tool-hydration-and-callable-chain.md`「钉住的 `tools[]` 与它的滞后出口」）。RBAC 收回在执行侧当跳即拒，而清单继续带着该名字，直到下一个重印边界才真的退场——空闲边界存在之后，这个滞后有了上界（`contextRefresh.idleMinutes`）；非声明的水合名收回方向走工具态收尾的实时卫生（当跳摘出 callable 与台账）。`denied_tools` 只装**这一跳真在 `tools[]` 里**的名字，名单源＝钉住声明：开关打开后判据直接落在"钉住清单 ∩ 实时授权"（参数表由整表供出，名单里的每个名字这一跳都带着一份字节，供不出的那份是占位，占位名因此也进这一行）；开关关闭时算点还要先过一道 schema 可渲染性（前门 `_frontdoor_schema_renderable_tool_names`；节点侧 `declared_denied_tool_schemas` 本身按实例或删除前快照取 schema）。**前门那一行只有一个算点**：`_frontdoor_declared_denied_tool_names_for_state` = 可渲染的钉住清单 ∩ 实时授权读（`list_effective_tool_names(actor_role="ceo")`，取不到就不印，不拿回合内快照凑）。它必须生效在发送前用 state 重建契约块的那一处——装配路 `message_builder.py` 建的那份在落请求体前还会被 `_refresh_prompt_cache_state` 与发送预检各重算一次，所以尾块加新字段只写进装配层等于没写。拒绝文案三态分开：候选未水化 ⇒ 先 `load_tool_context`；已收回或已下架 ⇒ 重试与改名都不会放行；两者都不在 ⇒ 未知名字。
- **删除与收回享受同一条缓存待遇**：实例随资源快照消失，但删除流程在 `rmtree` 之前会把该工具的 `to_model_schema()` 抄进进程内快照（`g3ku/resources/retired_schemas.py`，LRU 有界、重启即清空），两条车道的渲染侧在名字没有活实例时取回它——`tools[]` 与上一跳逐字节相同，可复用前缀不因一次删除而断裂，`actual_tool_schema_hash` 也不变。快照随进程一起没了（重启、或目录被外部删掉没留过快照）时，字节由钉住整表供出（开关打开时它是唯一来源，开关关闭时还要经过逐名载体与上一跳原文两档），全部取不到也只渲占位，**名字不会离开 `tools[]`**（占位的字节代价、断点位置与三档计数见本文「Provider Tool Surface」的「参数表的来源」）。尾块照旧点名：它确实在 `tools[]` 里带着一份参数表（供不出的那份是占位）、这一跳调不动（`denied_tools` 的文案因此写成"无权限或资源已下架"）。名字真正离开清单只发生在重印点。治理侧的"禁用"不在这条通道上：策略 `enabled=false` 只收窄授权，实例还在，`tools[]` 继续带着参数表。删除有一条运营闸门要知道：`_tool_usage_summary` 按"任一在飞任务或前门会话**看得见**该工具"就抛 `tool_in_use`，所以链进行中删不掉它——想验"删除落在链中间"必须走空闲删除 + 重启（进程内快照清空）再恢复那条仍钉着该名的链。
- 曝光收窄（候选塌缩、LRU 淘汰 callable）不得把名字从节点清单里删掉；LRU 只管 callable 层，与清单无关。被淘汰但清单仍带 ⇒ 模型看得见参数表、本轮调不动，需重新 `load_tool_context`。
- 工具曝光漂移改变当轮实际请求，但不因此轮换 caller-side prompt cache family；family 变化只留给稳定前缀重写、车道或模型切换、显式 cache-family revision bump 及其他刻意重置边界。`tool_signature_hash` / `actual_tool_schema_hash` 是观测字段，不是"该有新 family"的证据；缓存侧排查步骤见 `context-and-cache-troubleshooting.md`「跨普通 fresh turn 的 tool schema churn」。

provider-facing schema 刻意保持最小：

- 富文本说明留在尾部运行时合同里，provider `tools[]` 只保留 function calling 所需的最小 schema。repair-required 资源不靠轮换 provider `tools[]` 来表达：那两份清单只是运行时摘要侧的指引，清单稳定性优先于缓存连续性。
- 说明性文本与不支持的 JSON Schema 组合子（`anyOf` / `oneOf` / `allOf`）在传输前被剥平，provider-facing schema 只保留 function calling 需要的最小形状；参数正确性的权威仍在运行时校验侧，不要假设线上那份 schema 还保留了内部合同的每个分支。若缓存缺失与 `actual_tool_schema_hash` 的大幅变化同时出现，先查 provider schema 是否从这份最小/稳定形态回归成了富文本形态。

### Pinned Static Declarations（头部钉住块：skill 名单 / 执行策略 / 会话临时目录）

三条回合内不变的声明在前门只声明一次：渲染成 `## Runtime Contract (pinned)`，并进请求体首条 system 文本的末尾（与 `## Capability Exposure Snapshot` 同一载体，不是新消息项，因此尾部重铺那条剥运行时块的路碰不到它）。尾块的省略判定**逐段**看头部原文里有没有那一段：有才省，缺的那段照旧整段留在尾块。判据取头部原文而不是"本轮算出来了"——前门一个回合内有四条装配路，任何一条没写进头部却照样让尾块省略，声明就在整份上下文里消失，模型看不到候选技能也不会报错。

- **skill 名单是全量可见集，每行 `id | name | description`**："选哪个 skill"的判断在模型侧按描述做；工具面同样按可见集全量给出，没有按 query 的筛选或名额。名单按 id 排序；名单为空时整块不钉，三段声明全留在尾块。
- **重印判据 = roster epoch + 执行策略签名 + 会话临时目录**。roster epoch 与 `tools[]` 的重印边界同源（`_frontdoor_bundle_recommit_boundary` 返回 True 时递增：token 压缩、手动压缩、空闲边界），可见集本身**不进判据**——两次重印之间可见集怎么变，头部都逐字节不动。节点道的 epoch 源是 `_node_bundle_recommit_reason`（`token_compression` / 空闲边界），名单块钉在节点 system 首条，渲染函数与会话道同一个。
- **漂移的表达是一次性持久提示，不是重印也不是每跳重铺**：重印点之间新变为可读的 skill，由 `g3ku/runtime/skill_roster_notice.py` 渲染成一条 `[RUNTIME_NOTICE]` 消息（每行 `id | 名称 | 一句话描述`，末句给取正文的入口），落在本轮历史末尾、两份运行时块之前。同一条消息还承载长期记忆增量（`### Long-term memory updates` 一节，退场行在 `Deleted memories:` 一节，按 `memory_cursor` 消费记忆修订；头部基座只在压缩边界更新，规则见 `runtime-overview.md`「Memory Runtime State」），两节都空时不投。投递的那条同时写进该车道自己的 durable 载体（前门＝请求体基线，节点＝`message_history`，随逐跳落盘的节点历史一起持久化），因此之后每跳都待在可复用前缀里，不重铺。节点侧只挂发送投影时它靠同回合 append 链带着，历史重建（重启、续跑）后就不在上下文里了，而台账已记"投过" ⇒ 那个名字从此不会出现；头部字节不受影响，因为重印判据里没有可见集。**检测与投递每跳只有一处**：前门在 `_frontdoor_send_preflight_snapshot`，节点在 `ReActToolLoop._apply_node_skill_roster_notice`（同一跳先由 `_apply_node_skill_roster_head` 钉住头部）。其它装配点不读台账——一条回合内多个装配点共用一次消耗式读时，先跑的把差集领走、真正落请求体那处永远拿空。live 取样：前门 `_frontdoor_live_visible_skills`，节点每跳经 `runtime_context['live_skill_roster_supplier']`（supplier 由 `MainRuntimeService._node_skill_roster_supplier` 注入、`NodeRunner._runtime_context` 递进，取数失败才回落帧里的 `visible_skills`）。台账字段 `skill_id / recorded_at / delivered_at / superseded_at` 是"是否已宣布"的唯一判据，不靠扫渲染文本；作用域分桶为前门 `session:<session_key>`、节点 `node:<task_id>:<node_id>`。头部钉住块里那份名单算"已宣布"，所以第一跳不会重播整份名单，被头部吸收的条目同时从台账摘掉（块不无限涨）。台账落盘：前门写连续性侧车 `skill_roster_notice_context`，节点写 `node.metadata`，因此重启与 pause-resume 后不重复投。恢复方向**只在载荷真带这个键时**才覆盖进程表——手动压缩重写出的基线载荷本来就不带它，无条件覆盖等于抹掉台账，那一跳就没有名字可搬（排查"压缩后没带回名单"时先看侧车里这个键在不在）。手动压缩的搬运点在回合起点：待重印标记被消费之前先把名单补回 durable 基线，这样同一跳的种子就带着它。
- **压缩跳搬运，也是退场点**：内联 `token_compression` 采纳处，以及手动压缩（待重印标记被消费的那一跳），把"已宣布且本会话仍可读"的名字重新追加到压缩产物之后；同一次压缩只搬一份，段号记在 `carried_stages`。读不到的名字写 `superseded_at` 退场，不写"已下架"行——它的最终裁决归 `load_skill_context` 三态回执。这一跳的可复用前缀反正已经断在整段正文重写上，所以带这一份不额外付缓存。阶段裁撤碰不到它：提示不是阶段块。编辑重发/Fork 截断按 `recorded_at` 撤回晚于截断点的台账条目，与历史同源，否则那个名字从此再也不会出现。
- 文案是英文（模型可见文案统一英文），抬头固定 `Skills newly readable in this session:`，每行 `id | 名称 | 一句话描述`，末句只给取正文的入口并限定作用域在本会话；不写"我会替你保留"这类运行时承诺。
- 宣布范围是 `available` 为真的那批：`available=False`（repair-required，如 `missing required bins`）既不进头部名单也不进台账，因此不会被宣布成"新增可读"——模型 load 它只会拿到 repair 载荷。这类技能被修好、真的可读时，那一跳照常宣布一次（台账没记过它）。
- 模型整段复读这条提示时按 `strip_skill_roster_notice_echo` 从抬头处截断，与运行时块回显同一处置、同一个 `ECHO_STRIP_ENABLED` 开关。
- 显示面与模型面是两条独立车道：提示本身只进请求体与 durable 基线，**不进用户可见转录**（转录行由会话持久化）。投递时另发一条 `ceo.skill.roster_notice` 事件，前端渲染成与 `ceo.internal.ack` 同族的 system 内部行（`.message.system.ceo-internal-ack.is-skill-notice`，DOM + textContent 装配，不拼 HTML），不是用户气泡。该事件与 ack 一样是实时信号、不落持久转录，刷新后这行消失；技能本身在 Skill 管理页照旧可见，模型侧那份提示不受影响。
- **被删/被收回的名字留在头部名单里直到下一次重印**，模型去 `load_skill_context` 时由三态回执告知原因：`skill_deleted`（注册表无记录）/ `skill_not_permitted`（有记录但角色不可见）/ repair 载荷。skill 没有候选闸门——可见性由 v2 的治理实时可见集强制，回合初 stale 快照不会误拒 mid-turn 授权的名字。
- 载体的份数按车道。**会话道三份**：会话对象属性、按 `session_key` 分桶的进程表（LRU 封顶），以及连续性侧车里那份**原文与重印代数**（`pinned_contract_text` / `pinned_contract_revision` / `pinned_contract_skill_ids` / `skill_roster_epoch`，由 `tool_contract.restore_pinned_contract_state` 在恢复时一次写回前两份）。同回合内不同装配路拿到的 session 不是同一实例，而 `state` 里的键会被归一化白名单静默丢掉、不能当载体；读的时候进程内那两份都读，判据键相同就复用同一串原文。**节点道两份**：进程表，加上节点自己的耐久载体 `model_visible_tool_selection_trace['pinned_head']`——写帧时由 `ReActToolLoop._node_pinned_head_carrier` 导出，跳开始时由 `_restore_node_pinned_head_from_frame` 读回；装在 trace 里是刻意的，那份 dict 已整份持久化，而新增帧顶层字段要跟七个归一化写点（与 `declaration_schema_backfill` 同一处置）。耐久那份**只在进程载体为空时**才读回（会话侧还要"载荷真带这些键"才覆盖）：本回合刚跨过重印边界时耐久载体里存的还是旧原文，覆盖回去等于把刚重印的那一遍抹掉。耐久载体管的就是那一刻：进程载体空了（重启或被 LRU 顶掉）的那一跳若没有可复用的原文，只能按当轮可见集重渲——头部是请求里第一条消息，重渲一行（少一行也一样）就顶掉身后全部存活正文。判据键由 epoch + 执行策略签名 + 会话临时目录算出，epoch 必须按持久值写回而不是从头计，否则键与存下的原文对不上，恢复等于没恢复。
- 重启会让进程表清空、首跳重算一次，但**重算不等于改字节**：边界没动时渲染出的还是同一串，头部逐字节不变。只有边界真动了才付出那一次头部重写。
- 合头部一律**先摘再拼**，摘的位置是抬头行的**首次**出现（不是末次）：续跑回合的头部来自上一跳请求体种子、本身已带一份块，直接追加会把同一份块叠两遍并永久留在携带正文里（实盘抓到过 11,379→12,960 字符的双块头部，之后每跳都带着）。
- 节点道的 `candidate_skills` 仍留在运行时合同 **payload** 里（`extract_node_dynamic_contract_payload` 把它交回派发侧，是机器状态），但不渲染进尾块正文——渲染面由头部名单 + 注入块承载。删渲染行不动派发行为，不是一次缓存搬家。

## 10. 资源目录代检查与语义目录新鲜度

skill / tool 目录可能被编辑器、git 或外部进程直接改动，注册表不会自己收到通知，所以运行时按节拍做一次"代"比对：

- 节拍由 `resources.reload.poll_interval_ms`（JSON 侧 `resources.reload.pollIntervalMs`）节流，默认 1000ms；配成 0 表示每个调用点都允许比对。
- 比对面是 `capture_resource_tree_state()` 产出的 `{"skills": {name: 目录树指纹}, "tools": {...}}`，指纹来自 `ResourceRegistry._tree_fingerprint(资源目录)`。注册表缺失或目录为空时快照为空，比对直接返回"无变化"，这不是错误态。
- 指纹覆盖目录树里每个文件的相对路径、`mtime_ns` 与字节数，`__pycache__` / `node_modules` / `.git` / `.venv` / `venv` / `env` / `.pytest_cache` / `.ruff_cache` 这些目录整体不参与；软链接目录不进入递归。也就是说"改了正文一个字节"能被发现，而"原地替换成一个同大小且 mtime 未变的文件"不能。
- 打不开的子树（权限拒绝、路径过长、被占用）不进指纹：扫描对 `os.scandir` 失败只跳过该目录、不抛错——抛错会哑掉整条自动重载车道。代价是那棵子树里的正文改动判不出来，所以失败集合本身必须可见：集合变化时发一条 WARNING 点名根目录与最多 5 个失败路径（并给出总数），全部可读回来时补一条收口，同一失败集合不随每条 `exec` 命令刷屏。排查"改了 skill/tool 正文而运行时毫无反应"时，先在日志里搜 `resource fingerprint:`，有告警就说明差异集里缺一块。
- 这条扫描跑在 worker 的事件循环线程上，且是每条 `exec` 命令都要付的成本，所以它的单价必须按实盘规模看：skills 60 个目录 1659 个文件 + tools 33 个目录 93 个文件，一次全量扫描实测 45–50 ms。指纹吃 `os.scandir` 随目录枚举一并返回的目录项元数据；改成"先收集路径再逐个 `stat()`"实测回到 132–150 ms。
- 首次调用只落基线：没有上一份快照就没有可比对象，因此第一拍永远不报刷新。
- 三个比对点共用服务侧那一份基线（`_resource_tree_state_cache`）：CEO 前门的节流拍、`exec` 工具每条命令结束（`trigger='tool:exec'`）、管理端显式路径。命令侧不携带自己的快照，所以一条命令只付一次扫描；又因为基线就是"上一次扫描时的树"，命令执行前后之外发生的外部编辑同样会在下一个比对点被抓到，不存在被跳过的变更。
- 指纹变化时才动作：`refresh_changed_resources(trigger='external-resource-generation-check')` 重建受影响资源，随后清空 `_node_context_selection_cache`（节点上下文选择里缓存着旧的候选与在场结论）。差异集里的 skill / tool 名字只随返回值一起报数：`_sync_catalog_targets` 不回写语义目录，`catalog_synced` 恒为 `False`，所以那个字段的意思是"这里没做同步"，不是"按差异同步过了"。
- 管理端保存或编辑资源走的是显式路径（`refresh_resource_paths` / `refresh_paths`，trigger 为 `path-change`），它同步刷新注册表并当场重记基线；各条路径共用同一份基线，所以编辑后的一拍不会因为节流而漏掉变化。
- 维护要点：新增会进入上下文选择的缓存时，必须在这条差异路径上一起失效，否则外部改动的 skill/tool 正文会长期以旧指纹参与候选与描述投影。

## 11. Model Path Anchoring Contract

文件系统族（`filesystem_write|edit|move|copy|delete|propose_patch|stat`）、内容道（`content_open|search|describe`）接受的 `path`，以及 `exec` 的 `working_dir`，有四种形态，落点规则只在本文档说一次：

- 裸文件名（不含目录分隔符）→ 本次调用所属的临时目录，即 `runtime['task_temp_dir']`。节点道是任务临时目录；CEO 前门把会话临时目录注入成同一个字段，所以两条车道共用一条判据。
- 带目录段的相对路径，或 `{workspace}/<相对路径>` → 项目根。
- `{temp}/<相对路径>` → 同第一条。
- 绝对路径 → 逐字节按给定使用，任何一层都不重写、不 re-base。

三个 token 挂三个根：`{workspace}` 是项目根，`{temp}` 是数据根的 `temp/`——任务级 `<数据根>/temp/tasks/<任务>`、会话级 `<数据根>/temp/ceo/<会话>`，`{data}` 是数据根本身。`exec` 给子进程的 `TMP`/`TEMP`/`TMPDIR`/`G3KU_TMP_DIR`/`G3KU_TEMP_DIR` 与写失败正文的 `landing-park` 都跟着 `{temp}` 那一个根，所以模型 spawn 的脚本中间产物也落在数据根一侧。`task_temp_dir` 的准入判据是「在项目根内**或**在 `<数据根>/temp/` 之下」：前者让存量 meta 里仍写着安装根 temp 的任务照旧可用，后者把临时内容放出去；数据根的其余部分（任务库、日志、配置束）不在模型可写面内。该判据与 `restrict_to_workspace` 开关无关，两条车道各自无条件生效，判否的一侧静默回落到规范临时根而不是报错。

判据是「有没有目录段」，不是「像不像临时文件」：裸文件名没有可供推理的锚，模型的意图就是自己的 scratch 目录；而带目录段的形态绝大多数指向仓库内文件，把它默认进临时目录会让一次源码写入在错误位置返回成功。

`{data}` 无条件出现在合同、`path_anchor_tokens` 与 schema 里，包括数据根与项目根重合的安装。两个根重合时它们指向同一个目录，但职责不同：`{workspace}` 是仓库与沙箱锚，`{data}` 是交付与体积数据锚。按两根是否重合增减 token 会让模型在默认安装里根本没有交付锚名，把项目根读成"放哪边都一样"；而操作员第一次设置数据目录时新增那一行会把钉住头部改写一次，每个在跑的会话因此重付一遍前缀。

正式交付物的默认落点是 `{data}/output/`。用户或任务指定了别的位置就按指定的绝对路径写；`output/` 不存在时由写入方创建。这条路径只在本文档与 `PATH_ANCHOR_RULE_TEXT` 里各存一份表述，三条模型可见提示词（`ceo_frontdoor.md`、「执行节点」、「验收节点」）都只指向 `path_anchor_rule`，不抄第二份。

任务临时目录与会话临时目录都不是交付落点，这条判据不依赖任何自动清理动作：任务临时目录随终态回收只在 `G3KU_TERMINAL_TEMP_DIR_CLEANUP_ENABLED` 开启时发生（默认关闭，见 `operations-and-maintenance.md`「关键状态文件与目录」），会话临时目录没有任何清理车道——删会话带走的是连续性快照、请求取证、回合边界与上传目录，不含 `temp/ceo/<会话>`。提示词里印"temp 会被清理"会让模型以为运行时替它收尾，是一句做不到的承诺。

展开只发生在路径参数上：`exec` 的 `command` 正文原样交给 shell，写在里面的 token 不展开。锚点代表一个绝对前缀，整体用作路径开头才有意义——塞进一条绝对路径的中段，展开后得到的仍是无效位置。

四条不变量：

1. 落点闸门判的是解析后的绝对路径，与输入形态无关。legacy `workspace/tmp`、越界使用系统临时目录、`tools/` 注册面、托管产物必须落 temp 这四道闸门对四种形态同样生效；放开相对路径不削弱任何一条守卫，也不依赖「模型给的是绝对路径」这个假设。
2. 工具结果回显的始终是解析后的绝对路径，模型侧、轨道侧和审计面读到的都是落点而不是输入串。
3. 项目根的锚是 `agents.defaults.workspace` 的一次性解析，不随进程当前目录漂移（解析与冻结口径见 `config-and-models.md`「`agents`」；从二级 worktree 起进程时锚即那棵 worktree，见 `operations-and-maintenance.md`「关键状态文件与目录」）。临时根另有自己的锚：`g3ku/deployment/data_root.py` 解析出的数据根，两条车道都调同一个函数取它，不在各自模块里拼路径。
4. 两条车道的合同出自同一个渲染点，不允许各自措辞：`g3ku/utils/helpers.py` 的 `PATH_ANCHOR_RULE_TEXT` 与 `path_anchor_tokens()` 是唯一来源，节点道把它们放进 `runtime_environment.path_policy`（含 `path_anchor_tokens`，三个 token 恒定列出），CEO 前门把它们放进合同块的 `path_anchor` / `path_anchor_rule` 两行（静态句随钉住块进头部，头部没带时照旧落尾块）。`ceo_frontdoor.md` 不复述落点规则，只指向 `path_anchor_rule`。回归用例钉住"两处渲染的规则句逐字节相同"。

例外：`create_async_task.file_targets[].path` 只接受已存在的绝对路径。派发方携带的是跨任务作用度的用户附件，绑到某一个任务的临时目录会指错目标（同 `web-and-admin.md`「Image Upload Gating」）。

抄错前缀的拦截判据：绝对路径与项目根**只在靠前的某一段分叉**（分叉点不得晚于项目根段数减 4），且分叉之后**仍逐段复刻项目根的后续 ≥3 段**时，判为"整条前缀被重抄、其中一段打错"，写入被拒并在错误里给出按项目根重拼后的目标路径。判据不成立就不拦，所以合法出项目根的交付（桌面、输出目录）与项目内新建目录都不受影响——它们不复刻那条项目尾。这条判据排在其余落点闸门之前，因为"该写哪儿"比"别写系统 temp"更能让下一跳直接修对。写族被拦时正文同样进 `landing-park`（见下），编辑/移动/复制/删除没有新正文，只报错并给出目标。

写/改的落地校验（`write_validation_commands_by_ext` / `edit_validation_commands_by_ext`）不通过时，工具**仍然返回 error**，但被拒的正文不会被删：它先被复制到 `<临时根>/landing-park/park-XXXX/<原名>`，再按 `rollback_on_failure` 回滚目标位置，结果行里给出该绝对路径并提示"在那儿修好再 move 过来，别重写正文"。留痕走同一条 `_record_node_file_change` 路径，所以审计面看得见这笔停车。停车目录就在调用方自己的临时根下面，因此它继承该目录的保留与清理策略（见 `operations-and-maintenance.md`「关键状态文件与目录」的 `temp/tasks/`、`temp/ceo/` 两条），不另设回收器。实盘量级：写失败 119 条里 85 条属于这一类。
