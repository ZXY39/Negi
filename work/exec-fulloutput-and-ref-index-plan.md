# exec 全量进 artifact（单落点）+ 压缩块钉住 ref 索引

状态：待批（未动代码）。本版取代上一版"独立日志 + 双落点"设计。
靶子来源：DSH（deepseek-harness 2e39e704）/ pi（pi-mono edfe6b2）找回闭环对照，Negi 467aa2a2。

## 1. 两个结构缺口

**A：前台 exec 中段永久丢失。** `_BoundedStreamCapture.append` 的 `del self._tail[:-byte_limit]`（`g3ku/agent/tools/shell.py:118-120`）把中段字节直接从内存删掉，不落任何盘。下游外置闸门拿到的是 `head + …（超出捕获上限，中段未保留）… + tail`（`:1342`），`_persist_text` 忠实存了这个残缺字符串。**外置保真的是载荷，不是命令输出**；丢失在它上游。后台车道反而有全量 `log_path`（`:936,1007,1019`）。

**B：压缩后 artifact ref 不被机器钉住。** 规则同时写"保留重要引用与文件路径"和"丢弃纯工具调用流水"（`main/runtime/react_loop.py:317` 起），ref 存活取决于摘要器。已有"机器重算稳定块"成熟形状（未终态子节点快照，`:341,347,7067`）没用在 ref 上。

## 2. 设计决定：为什么是单落点

上一版让全量落独立明文日志、artifact 仍装头尾，模型要理解"ref 是摘录、path 才是全量"。这是永久认知负担，且"信封写明关系"只是补救不是优点。本版改为：**全量 tee 到文件 → 登记为 artifact → ref 直接指向全量**。模型只面对一个落点。

撤回的论据：曾说"artifact 装全量就不可 grep"。不成立——gzip 阈值默认 1 MiB 且可配，`<=0` 纯明文（`g3ku/config/schema.py:921,932`、`main/storage/disk_guard.py:106,152`），`read_artifact_text` 对 gzip 透明解压（`main/storage/artifact_store.py:50`）。

硬约束：**内存必须有界**。`create_text_artifact(content: str)` 要全文 sha256 + 全文去重比对 + `content[:400]` preview（`artifact_store.py:117-152`），直接灌全量会 OOM。所以必须给 store 加从文件登记的入口，而不是把环放大。

## 3. 非目标

- 不做"模型按次声明全量返回 + 预算准入"档（已搁置）。
- 不改 `INLINE_CHAR_LIMIT = 6000`，不改 `content_open` 的 16000/128000 正文上限。
- 不改 Negi 整篇外置范式。
- 不做日志脱敏。
- 不做导航读端的流式搜索重写（本版只加守卫 + 指引，见 P0-c）。

## 4. P0：exec 全量进 artifact

### P0-a 捕获阶段 tee（内存有界）

- `shell.py:467-479` `_capture_stream` 与 `:457-465` `_collect_terminated_process_output`：每个 chunk 既进内存环也 append 到临时文件。**从第一个 chunk 就写**（等超阈值再写会丢头部）。
- 临时落点 `data_g3ku_path("exec-tmp", scope)`，名 `<stamp>-<uuid8>.log`。这只是中转，寿命由 P0-b 决定。
- **未超阈值就删临时文件、不登记**：`total_bytes <= byte_limit` 时环里已是全量，无需 artifact。小命令只付一次临时写+删的 I/O，不留文件。
- 单文件上限 **64 MiB**（照 DSH `DEFAULT_MAX_SPILL_BYTES`）。超限停止写入并标注已写字节数。

### P0-b store 新增从文件登记入口

`main/storage/artifact_store.py` 新增 `create_text_artifact_from_file(...)`：

- 流式 sha256（不整读）、`size_bytes` 取文件字节、`preview_text` 取头部 400 字符。
- **去重按 hash，不做全文比对**（`_find_existing_text_artifact` 的全文比对路径不适用于大文件）。
- 同盘 move/原子 replace 进 artifact 目录（复用 `_replace_atomically`），零拷贝。
- 走 `_note_disk_written` 计量，纳入磁盘守卫口径。
- **这类 artifact 强制 plain**（不 gzip），保住 exec 里 rg / Select-String 直接可搜。默认值可否决：沿用 1 MiB 阈值则只能走 ref 读端。
- 登记成功后删临时文件；登记失败则保留临时文件并如实退回"中段未保留"文案。
- session 车道无需特殊处理：`_runtime_task_id` 已有 `adhoc:<session_key>` 兜底（`navigation.py`）。

### P0-c 导航读端大目标守卫（必做，不是可选）

`_resolve` 对 path 与 ref 两条路都**全量读进内存**（`_read_text_for_content_display` / `read_artifact_text`），且**无大小守卫**（全文件只有 `:947` 的 `st_size` 用于元数据）。今天 exec artifact 最大约 128 KB 所以没暴露；全量进 artifact 后上限抬到 64 MiB，一次 `content_search(ref)` 可能吃掉数百 MB RAM（整读 + `splitlines()`）。

- 默认阈值 **8 MiB**：口径是高于已知最大正常用例（docs 实测 `task_node_detail(detail_level="full")` 达 7,024,971 字符，见 `tool-hydration-and-callable-chain.md:95`），不误伤既有行为。
- 超阈值不整读，返回可行动错误 + 指引：用 exec 定向提取（rg / jq / python 流式），或缩小范围。**不允许静默截断后假装读全**。
- 流式逐行搜索另立阶段，不在本版。

### P0-d 载荷与文案

- 超阈值时 exec 结果走登记后的 ref，信封仍是 6 行/800 字符头部预览（`navigation.py:252-287` 契约不变）。
- 中段标记三态如实：完整全量 → 指向 ref；只写到 64 MiB 上限 → 明写"仅前 64 MiB"；写盘/登记失败或档位不允许 → 退回 `中段未保留`。
- **后台车道仍给 `log_path`**：活文件在持续写入，不能登记成静态 artifact。双落点问题在后台车道依然存在，文案必须区分"前台结果 ref"与"后台进程日志 path"，避免模型把前者当活日志 tail。
- `react_loop.py:4399-4410` 复开提醒同步：ref 背后是全量，`content_search(ref=…)` 可直接搜中段。

### 机制级保证

`中段未保留` 只允许在真的没有全量时出现。不允许"给了 ref 但 artifact 只有头尾"。内存占用不随命令输出线性增长（tee 流式 + 读端守卫）。

### 验收

- 单测（扩 `tests/resources/test_exec_tool_shell.py`）：>64 KB 输出头/中/尾埋哨兵 → ref 背后 artifact 含三个哨兵；<64 KB → 不登记、临时文件已删；超时路径同样落盘；超 64 MiB → 标注截断；登记失败 → 文案如实退回。
- store 入口单测：流式 hash 正确、preview 取头部、按 hash 去重、plain  encoding、计量入 `_note_disk_written`。
- 读端守卫单测：8 MiB 以上目标返回可行动错误而非整读；7,024,971 字符用例仍正常。
- 真对话验收（重启 web，端口 18790、数据根 D:\NEGI）：>200 KB 输出命令，让模型找中段哨兵，确认走 `content_search(ref=…)` → `content_open(ref=…, around_line=N)`，**不重跑命令**。
- `python -m pytest tests/resources/test_exec_tool_shell.py tests/resources/test_resource_runtime_smoke.py -q`；`python -m ruff check .`。

## 5. P1：压缩块机器钉住 ref 索引

- 照 `_render_unsettled_children_section`（`react_loop.py:347`）新增 `_NODE_EXTERNALIZED_REFS_HEADING` + 渲染函数。
- 数据源 `TaskArtifactStore.list_artifacts_for_node`（`artifact_store.py:281`），**现成台账不新建 ledger**；渲染前用 `_artifact_is_readable`（`:387`）剔掉不可读条目。
- 注入 `:7067` unsettled_section 旁，两处调用 `:7457`、`:8539` 都传。
- 默认最近 **N=20** 条，每条一行 `ref · display_name · line_count/char_count`；超 N 写明"另有 M 条更早结果，用 content_search 找"。只列本节点产出的；无外置结果不出空块。
- 生命周期随压缩基线驻留稳定区（`:8067`），下次压缩整段重写不叠加。

### 缓存两轴

- 写价：进稳定前缀，每压缩周期一次，上界 20 × ~120 字符 ≈ 2.4 KB ≈ **1.5k token**（中文 0.5–0.66 token/字符口径）。
- 读价：每请求付。一天 100 发 ≈ 15 万缓存读 token/天；缓存读价通常原价 1/10 量级，折原价约 1.5 万 token/天。
- 只在压缩时换字节，**不断前缀缓存**。放尾部或每跳重算会重演 `:8067` 注释里的"每跳判超窗⇒每跳重摘要"病。
- 验收实测压缩前后 `cached_tokens` 比例，不靠推理。

### 机制级保证

索引由运行时机器重算，不依赖摘要器保真。

### 验收

照 `tests/resources/test_node_compaction_unsettled_children.py` 新建 `test_node_compaction_externalized_refs.py`：块在场、条目上界、重写不叠加、无结果不出空块、不可读条目剔除。真任务验收：长任务触发压缩后让模型引用早先结果，确认直接用 ref。

## 6. 代价与风险（如实）

**常开**
- 磁盘：每条超阈值 exec 产一份全量 artifact，单文件 ≤64 MiB，计入磁盘守卫预算（`_note_disk_written`）。**会挤占守卫预算**，可能触发降级写/清理口径，需实测。寿命随任务清理（`delete_artifacts_for_task` / terminal cleanup），比上一版独立 7 天日志短——敏感暴露面反而更小。
- 敏感输出持久化：凭据/token 落盘，不脱敏。随任务清理消失是本版相对上一版的改善。
- 内存：读端守卫把单次导航读的内存上界钉在 8 MiB 量级；tee 本身流式有界。
- P1 稳定块约 1.5k token（读价每请求）。

**启用（按行为触发）**
- 只有输出超 64 KB 才 tee 留存并登记；小命令临时写+删，不留文件、不暴露 ref。
- store 流式入口只在这条路径触发，其他工具照旧走 string-in。
- 反效果：索引在场可能诱导模型开不需要的旧结果。缓解：只给 ref+label+size、不给预览，写明按需才开。

**一次性**
- 比上一版多两项工程：store 从文件登记入口、导航读端守卫。其余为 exec tee、两个测试文件、三篇 docs、P0/P1 各一轮真机验收。

**残留风险**
1. 只读档/沙箱是否允许写数据根需核 `main/governance/exec_tool_policy.py`；不允许则该档 fail-closed 退回现状文案。
2. 磁盘守卫与全量 artifact 的相互作用未实测，可能出现"命令输出大→守卫清理→ref 失效"。失效是可行动错误（not-found），不静默。
3. 前台 ref 与后台 log_path 语义仍不同，文案区分不彻底会让模型误 tail。
4. 8 MiB 守卫会让"模型想整读一份 20 MiB 日志"被拒，只能走 exec 定向提取。这是有意的能力边界，不是缺陷，但要写进工具指引。
5. 全量随任务清理消失：跨任务复用同一份输出会失效。若需跨任务留存要另定口径。

## 7. 文档维护（AGENTS.md）

完成前走 `skills/negi-architecture-maintenance/SKILL.md`，预计更新：
- `tool-hydration-and-callable-chain.md`：复开提醒（:72-74）、外置信封（:91-98）补"ref 背后是全量"与三态文案。
- `tool-and-skill-system.md`：exec 截断契约（:166 起）补全量登记。
- `main-task-runtime.md`：压缩稳定块清单补 ref 索引段。
- 新增读端守卫与 store 文件登记入口的归属文档（按 skill 判定，可能落 `web-and-admin.md` 或 `operations-and-maintenance.md`）。
- `README.md` Topic Guide 若需增条目则同步。

## 8. 交接

- 行号基于 Negi `467aa2a2`，动手前重核（共享工作树）。
- 可复用：`_replace_atomically`、`_note_disk_written`、`_artifact_is_readable`、`list_artifacts_for_node`、`_render_unsettled_children_section`、`_runtime_task_id` 的 adhoc 兜底。
- 未做：全量返回档、脱敏、导航流式搜索、DSH 式跨工具 token 预算（6000 字符闸门已覆盖该职责）。
- 顺序：P0-a/b/c/d 一起做完并真对话验收（c 不能省，否则新上限直接变成 OOM 面），再做 P1。
- 提交纪律：只 add 本计划涉及文件；每阶段完成即提交；英文提交信息，作者临时 `-c` 用 ZXY39 <1045273950@qq.com>。

## 9. 实施记录（已实现，含与计划的偏差）

提交：P0 `945e4770`、P1 `1387e826`，其后一批为指引修正 + 架构 docs + 验收脚手架。

**与计划的偏差（都是实施中发现的事实，不是缩水）**

1. **exec 不自产信封**。原设想让 exec 直接返回 `content_ref` 信封，实施时发现那会绕过执行道的 `is_error_like_tool_result` 判定（信封 JSON 没有 `status` 字段），把失败命令画成成功。改为：exec 只在载荷里声明 `full_output_ref` 与全量规模读数，信封仍由投递道产出；navigation 的 `maybe_externalize_text` 从载荷读该 hint，ref 指向全量 artifact 而不另存载荷。两条投递道（前门 / 节点）因此都不用改。
2. **索引段规模读数用 `size_bytes`，不是行数/字符数**。`TaskArtifactRecord` 不带行数/字符数，为印这两个数去整读文件会把轻量索引变成一次大读。
3. **复开指引改用 `start_line`/`end_line`，不用 `around_line`/`window`**。服务层支持后者，但 `tools/content_open/resource.yaml` 的模型面 schema 未声明；指向未声明参数会被当未知参数忽略并退回默认区间。真机验收实测到这个形态（按行号复开却拿到文件开头），已修指引并把这条陷阱写进架构 doc。
4. **tee 目录名用 scope 的 sha1**。`owned_processes.scope_key` 返回 `task:…` / `session:…`，冒号在 Windows 路径分量非法，直接当目录名会 `WinError 123`。
5. 新增 `sniff_subprocess_encoding`（与 `decode_subprocess_output` 共用候选编码表）：整缓冲嗅探不可流式，登记时按头部采样定编码再增量解码转 UTF-8。

**验收结论**

- 单测：`test_exec_full_output_artifact.py` 8 条、`test_node_compaction_externalized_refs.py` 9 条；连同 exec / 压缩 / react 回归 / smoke 共 **301 passed, 7 skipped**。`ruff check` 全树干净，`check_architecture_docs.py` 0 error（剩 1 条 warning 在未触及的 `config-and-models.md`）。
- 真机（假 provider 跑真实车道，`work/live_full_output_acceptance.py`）：**前门道 P0 全通**——exec 信封报 12003 行（＝全量文件行数，不是头尾拼接）、`content_search(ref)` 命中只存在于中段的哨兵并给出绝对行号 6002、`content_open(ref, start_line/end_line)` 取回该哨兵且带行号栏、9 MiB 路径目标被守卫拒绝并给 exec 定向提取出路。节点道 P0 在另一次运行中通过（任务 success + `content_search(ref)` 命中中段）。数据根 `exec-tmp/` 无残留 `.log`，全量 artifact 以明文 `.txt` 落在 `main-runtime/artifacts/` 下（前门 adhoc 与任务两侧都有）。
- **P1 的真机压缩未验到**：节点压缩助手不走 execution 路由绑定，假 provider 收不到压缩请求，因此无法在脚手架里逼出压缩块。P1 目前只有单测证据（含块位置、元数据、上界、去重、不可读剔除、不叠加、取数失败不阻断）。这条缺口如实留着，不声称已真机验证。
- 脚手架不能证明的事：真实模型会不会**选择**不重跑命令。provider 是脚本化的，那个选择没有被实际行使；指引文案只是把出路写清楚。

**残留与后续**

- 敏感输出落盘不脱敏（计划已列为新暴露面），寿命随任务清理。
- 磁盘守卫与全量 artifact 的相互作用未做长跑实测。
- 前台 ref 与后台 `log_path` 仍是两种落点语义（后台是活文件，不能登记成静态 artifact）。
- 导航读端仍是整读，8 MiB 以上只能走 exec 定向提取；流式搜索另立阶段。