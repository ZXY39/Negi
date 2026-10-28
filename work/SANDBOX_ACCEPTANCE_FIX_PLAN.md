# 沙箱验收遗留问题修复实施计划

基线：`origin/main = d04a1c2c`；本机 HEAD `c8b59e28`（本地多 16 个提交）。
工作树有他人未提交的沙箱页 UI 改动，本计划不触碰下列文件/目录：

- `docs/architecture/sandbox-execution-plane.md`（他人 UI 文案改动；本计划所需的只读表更新另行协调提交）
- `g3ku/sandbox/installer.py`
- `g3ku/web/frontend/sandbox.css` / `sandbox.html` / `sandbox.js`
- `tests/test_sandbox_installer.py`
- `work/sandbox-page-ui-plan.md` / `work/.sandbox-ui-mock/`

## 范围

修复交接文档 A–J 中已定稿的机械项与契约项：

- A 前端测试 mock 漂移
- B `tool_contract` 循环导入
- C2 验收计数测试桩缺 `_log_service`
- C1/C3/C4 提示词与测试契约
- D3/D4 outbox 投影与最终提交测试
- E `register_stickers` 的 rerun_safe 与第三方只读白名单
- F1/F2 入站消息类型、`chat_type`、`distribution` 摘要
- G1/G2 两处点名行为差异
- G3 按根因分诊，不逐文件重写

## 非目标

- 不打开 `G3KU_SANDBOX_ENABLE_JAIL`，不改档位模型、信任分级与 jail 闸门。
- 不重跑第 11 条端到端：本机 `sandbox status = runtime_missing / wsl_distro_missing`，无 WSL 后端。
- 不整跑 `pytest tests`：`tests/resources/test_task_node_dispatcher.py` 已知挂死（H），继续用逐文件运行。
- 不为了变绿而恢复已退役的 `status` / `delivery_status` / blocked 车道。

## 决策摘要

| 项 | 选择 |
|---|---|
| C1 段号 | 测试改 `### 2.4` |
| C1 `delivery_status` 断言 | 删旧断言，改新裁决契约措辞 |
| C1 空证据分类器 | 删测试，不复活 `_classify_blocked_verdict` |
| C3 执行侧 `task_id` | 改测试，只要求验收侧；执行侧用节点工具规则 |
| C3 工具暂不可用 | 补提示词句（保留"别因暂不可用而放弃"语义），改测试 |
| C4 `completed_stage_summary` | 补三份提示词，保留测试 |
| D3 `outbound.created` | 保留代码，测试改为 3 条并断言 `event` |
| D4 9 条 | 按新契约重写；保留未知参数拒收、缺必填报错、重复提交折叠 |
| E `register_stickers` | `rerun_safe: false`，移出第三方只读白名单 |
| E `show_sticker_category` | 保留 `rerun_safe: true`，加入 rerun 名单，修正注释 |
| E 残留 `task_append_notice_cn` | 删除（仅未跟踪 `.pyc`） |
| F1 `UserInputMessage` / `chat_type` | 接受为有意变更，改测试 |
| F2 `distribution` | `_runtime_summary_from_frame_heads` 补齐，保留测试 |
| G1 `task_append_notice` 测试 | 删除该测试/分支 |
| G2 follow-up | 修代码，保留测试 |
| G3 其余 | 按根因分批，不逐文件重写 |

## 阶段

### Phase 0：基线与计划

- 记录目标用例基线（D/F/G/H/I 计数已核）。
- 创建本计划。
- 运行方式：逐文件 pytest，避免 dispatcher 挂死。

### Phase 1：机械项（A / B / C2）

1. A：`tests/resources/test_llm_frontend_save_inputs.py` 与
   `test_llm_frontend_save_sends_no_request.py` 的 DOM mock 补 `querySelectorAll`。
   验收：12 个前端文件全绿（基线 11 failed / 137 passed）。
2. B：`g3ku/runtime/frontdoor/tool_contract.py` 对
   `main.governance.exec_tool_policy` 的模块级 import 改为函数内延迟导入，断开
   `tool_contract -> main` 边。
   验收：`python -c "import g3ku.runtime.frontdoor.tool_contract"` 成功；18 个 B 文件逐文件 collect 无 error；
   单进程整跑不再只有第一个文件报错。
3. C2：`tests/resources/test_acceptance_rejection_count.py` 的
   `object.__new__(NodeRunner)` 桩补 `_log_service`。
   验收：该文件 4 条通过。

### Phase 2：安全与 rerun_safe（E）

1. `tools/register_stickers/resource.yaml`：`recovery_policy.rerun_safe: false`。
2. `g3ku/sandbox/policy.py`：从 `READONLY_THIRD_PARTY_TOOL_NAMES` 移除
   `"register_stickers"`；`show_sticker_category` 保留，注释改为"渲染缓存，不是纯只读"。
3. `tests/resources/test_tool_manifest_contracts.py`：rerun 期望集合加入
   `show_sticker_category`，不加入 `register_stickers`。
4. 删除未跟踪残留 `tools/task_append_notice_cn/`（仅 `__pycache__/tool.cpython-312.pyc`）。
5. 检查并更新 `tests/test_sandbox_trust_p0.py`、`tests/resources/test_sticker_*.py`
   中涉及 `register_stickers` 信任面的断言。
6. 验收：`tests/resources/test_tool_manifest_contracts.py` 0 失败；相关 sandbox/sticker 用例通过。
7. 文档：`docs/architecture/sandbox-execution-plane.md` 的只读档表格需同步
   `show_sticker_category` / 移除 `register_stickers`；该文件当前有他人未提交改动，
   放在 UI 改动提交后单独处理，或在交付说明中列为待办。

### Phase 3：契约一致性代码（F2 / C4 / C3b / G2）

1. F2：`main/monitoring/log_service.py::_runtime_summary_from_frame_heads` 补
   `distribution`（无 live state 时 `{}`），与 `main/monitoring/query_service.py:723` 同形。
   验收：`tests/resources/test_runtime_frame_summary_cache.py` 0 失败。
2. C4：向 `g3ku/runtime/prompts/ceo_frontdoor.md`、`main/prompts/node_execution.md`、
   `main/prompts/acceptance_execution.md` 补
   `` `completed_stage_summary` 必须是对刚结束那条阶段的简要概括 `` 段落。
   验收：`tests/resources/test_execution_prompt_filesystem_guidance.py` 通过。
3. C3b：向 `main/prompts/node_execution.md` 与 `main/prompts/acceptance_execution.md` 补
   "工具暂时不可用"句（保留测试期望的原文，另加执行节点不可提交 failed/blocked 的澄清）。
   验收：`tests/resources/test_runtime_environment_guidance.py` 的该条通过。
4. G2：修 `g3ku/runtime/frontdoor/_ceo_runtime_ops.py` 的 follow-up 合并，
   确保 `[*request_body_messages, *follow_up_messages]` 不丢 assistant 部分回复。
   验收：`test_ceo_frontdoor_regressions.py::test_graph_call_model_injects_running_turn_follow_up_messages_before_provider_send` 通过。

### Phase 4：测试契约更新（C1 / C3a / D3 / F1 / G1）

1. C1：`tests/resources/test_acceptance_evidence_discipline_prompt.py`
   - `### 3.4` → `### 2.4`；
   - 删除 `failed + delivery_status=...` 断言，改新裁决契约措辞；
   - 删除 `_classify_blocked_verdict` 测试。
2. C3a：`tests/resources/test_runtime_environment_guidance.py` 的执行侧断言改为
   "`task_progress` 是会话侧工具，节点侧调不到"及现有节点工具规则；验收侧断保留。
3. D3：`tests/resources/test_mcp_negi_surface.py` 的 wake 投影改为 3 条，
   断言 `event` 区分 `reply.final` / `outbound.created`，`replies_pending=3`。
4. F1：`tests/resources/test_external_v1_contract.py` 的 POST /sessions 键集加入
   `chat_type`；`test_external_api_messages.py` 断言 `UserInputMessage.content`。
5. G1：删除 `test_parse_create_async_task_result_recognizes_task_append_notice_guidance`
   及生产侧 `append_notice` 分支（若仍存在）。
6. 验收：每个文件 0 失败。

### Phase 5：D4 重写（9 条）

- 按新契约重写 `tests/resources/test_final_result_shape_faults.py` 的 9 条失败用例：
  - 未知参数（含 `status` / `delivery_status`）被拒收；
  - 缺必填的报错文本；
  - 重复相同提交的折叠（`main/runtime/react_loop.py:8879` 仍存在，不要删除）。
- 逐条运行，保留仍适用的 shape fault / marker 字节用例。
- 验收：该文件 0 失败；`pytest -k "final_result or react_loop"` 不回归。

### Phase 6：G3 分诊

- 运行 14 个文件，按根因分组（append-notice 退役、distribution、goal 字段、
  tool_impulse staging、其余单条）。
- 只改确定属于同一退役契约的批次，不逐文件重写；其余在计划末尾列出分类清单。
- 验收：既有通过文件不回归；剩余失败有根因标签。

### Phase 7：全量验证与文档

- `pytest tests -q -k sandbox`：保持 109 passed / 3 skipped。
- 逐文件运行所有本轮触碰的文件。
- `python -m ruff check .`。
- 文档：
  - `docs/architecture/sandbox-execution-plane.md` 只读表（与 UI 改动协调）；
  - `docs/architecture/main-task-runtime.md` follow-up 注入行为（若确认行为修正）；
  - `docs/architecture/web-and-admin.md` 的 runtime summary `distribution`（若该文档描述摘要形状）。
- 每阶段一个 commit，只 stage 本计划触碰的文件；不 stage 他人 UI 改动。

## 风险

- B 的循环导入修复可能暴露新的导入顺序问题；先逐文件、再单进程验证。
- D4 重写容易误删折叠语义；折叠在 `main/runtime/react_loop.py:8879`，保留。
- 共享工作树：他人 UI 改动与 `docs/architecture/sandbox-execution-plane.md` 重叠，
  文档更新单独协调，避免把他人改动带进 commit。
- 全量套件仍会在 dispatcher 挂死（H）；不整跑。
- 本机无 WSL 后端，J 的第 11 条端到端仍不可复跑。

## 交接

- 完成后在本文件末尾补：已关闭项、剩余 G3 分类、验证命令与结果、
  文档更新状态、J 的前置条件。

## 执行结果（2026-10-28）

### 已提交

| 批次 | commit |
|---|---|
| B 循环导入 | `cdd66148` |
| A 前端 mock | `31eecf6e` |
| C2 测试桩 | `d23cabf9` |
| 计划文件 | `c07cc814` |
| E 安全 / rerun_safe | `24974e5d` |
| F2 distribution | `19da46c1` |
| G2 follow-up 测试 | `b6acfc64` |
| C1 证据纪律测试 | `593e629e` |
| C3/C4 两份提示词 | `0a979bfb` |
| F1 / D3 / G1 | `3785e9fd` / `3dfbec6e` / `b91e9327` |
| D4 契约 + repair 骨架 | `31fb8350` |
| G3 节点 goal / sandbox_tier / impulse 替身 | `8cf39505` |
| G3 剩余测试批次 | `eee6f366` |

### 验证结果

- A：12 个前端文件 `148 passed`。
- B：18 个文件单进程 `168 passed, 2 skipped`；`import g3ku.runtime.frontdoor.tool_contract` 成功。
- C：`test_acceptance_evidence_discipline_prompt.py`、`test_acceptance_rejection_count.py`、
  `test_runtime_environment_guidance.py` 全绿；`test_execution_prompt_filesystem_guidance.py`
  仅剩 frontdoor 的 `completed_stage_summary` 一条。
- D4：`test_final_result_shape_faults.py` `37 passed`。
- F：external contract/messages + frame summary `84 passed`（与 D4 合并运行）。
- E：manifest + sandbox trust `44 passed, 1 skipped`；贴纸相关 `59 passed`。
- sandbox 子集：`109 passed, 3 skipped`。
- G3 已关闭：react regressions 134、context assembly 79、tool_impulse 28、
  tool resource admin 125、runtime progress 119、reasoning replay / file targets 全绿。

### 后续续跑（2026-10-28 下午）

| 批次 | commit |
|---|---|
| C4 frontdoor `completed_stage_summary` | `f64e5e85` |
| 压缩后重贴契约尾块 | `72694462` |
| 验收握手 submission_id 默认字段 | `ee35a6d1` |
| 压缩后重投一次性合同 notice | `1bbcc31c` |

- C4 已关闭：`test_execution_prompt_filesystem_guidance.py` 6 passed。
- 压缩契约尾块已修：`repair_required/candidate` 段不再随摘要丢失；
  `test_frontdoor_append_only_compression.py` + context overflow 24 passed。
- task_web_worker 握手默认字段已修，40 → 39。

### 剩余（明确阻塞/独立批次）

1. **task_web_worker_runtime（已关闭）**：`f11fd682` 把 39 条全部对齐到
   非阻塞派生、钉住契约头与节点信箱契约，整文件 `221 passed`。分族处置：
   - spawn children 结果/failure_info：改为创建回执断言；失败/取消/异常用例改测
     `_spawn_abort_result`、`_runtime_spawn_failure_info` 与记账故障原样上抛。
   - waiting_children / round 恢复：恢复只重放派生轮，测试显式驱动子节点后
     用 `reconcile_spawn_round_completion` 断言轮次完成度。
   - 契约尾块与 seed/prefix：动态尾块退役，改为断言钉住头 + 一次性 notice；
     seed 夹具补上节点头部快照，`provider`/内部前缀断言恢复为精确前缀。
   - distribution / barrier：分发屏障与 `_deliver_distribution_message` 已删，
     改测“旧 distribution 状态不再拦恢复”“待并入消息只记节点层”。
   - acceptance blocked：改为 `reject_no_progress` 三档裁决契约。
   - node detail / metadata-only：轮次投影停写，详情读节点行、指纹只看子节点集。
2. **J 第 11 条**：本机仍无 WSL 后端、token 为空，证据包不在本机；不可复跑。

`docs/architecture/sandbox-execution-plane.md` 只读表已同步（`ceefd5ed`）；
仓库 ruff 基线恢复（`6b4fbc4c`）。

### 最终验证（2026-10-28 15:40）

- `tests/resources/test_task_web_worker_runtime.py`：221 passed。
- 关联回归（spawn / wait_all / spawn-round-fault / contract-overlay / acceptance scheduler / append-only）：97 passed。
- `test_resource_runtime_smoke.py`：106 passed。
- `pytest tests -k sandbox`：109 passed / 3 skipped。
- `python -m ruff check .`：全绿。

context_window 已关闭：`1bbcc31c` 在压缩重置后、同一次主请求前重投一次性
`[RUNTIME_NOTICE]`，repair 段以 `- repair_required:` 合同节到达模型；压缩套件
`25 passed`。钉住头保持只吃 roster epoch 的缓存契约，未把 repair 项塞进头部。

### 并发改动警告

工作时另一会话在同一工作树修改了：
`docs/architecture/*`、`g3ku/runtime/prompts/ceo_frontdoor.md`、
`g3ku/runtime/frontdoor/tool_contract.py`（repair 段注释）、
`g3ku/web/frontend/*`、`skills/meme|repair-tool`、以及多个 prompt/前端测试。
本轮只 stage 自己的文件；上述文件不要整文件提交，避免把他人改动带进 commit。
如需继续 C4 / G3，建议等另一会话提交后在干净树上续跑。
