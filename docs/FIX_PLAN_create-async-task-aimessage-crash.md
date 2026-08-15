# 治本修复方案：create_async_task 错误重试导致 `'AIMessage' object has no attribute 'get'` 崩溃

> 适用会话：`web_ceo-e5e605ca53d3`（2026-08-13 17:24:54 / 17:26:09 两次失败）
> 编写日期：2026-08-14
> 性质：后续修复工作的指导文档（治本，非一次性热修）

---

## 0. 结论速览（TL;DR）

一次用户请求“每日简报”被 CEO 前端代理路由到 `create_async_task`，链路中叠加了**三个独立问题**，最终表现为两次相同的会话崩溃：

1. **工具契约类型放宽**（间接原因，最上游）：
   `create_async_task` 的参数 schema 使用宽松的 union 类型，等于向模型暗示“嵌套参数可以传字符串”。
2. **模型/协议遵循度差异**（触发原因）：
   qwen3.8-max 经 `responses:`（DashScope Responses 协议）绑定调用时，把 `execution_policy` 和 `file_targets` 都生成成了 JSON 字符串（`"{"mode":"focus"}"`、`"[]"`），违反工具运行时实际校验，触发 `file_targets should be array`。deepseek（以及同一模型走 `custom:` OpenAI 兼容协议时）生成的是正规对象/数组，从不触发该错误。
3. **框架缺陷**（崩溃根因）：
   工具报错后的“重试处理”路径中，把 LangChain 的 `AIMessage` 消息对象当普通 dict 调用 `.get()`，抛 `AttributeError`，且该异常未持久化到任何日志，只回落到“运行出错”兜底文案。

修复按 **P0 防崩溃 → P1 避免触发 → P2 可观测性 → P3 数据治理** 四层推进。

---

## 1. 问题背景与现象

### 1.1 现象
- 会话 `web_ceo-e5e605ca53d3` 在 17:24:54、17:26:09 两次以同一错误结束：`运行出错：'AIMessage' object has no attribute 'get'`。
- 两次失败发生在 `create_async_task` 返回契约校验错误（`Error: file_targets should be array ...`）之后、下一次模型调用之前（时间差约 130–200ms）。
- 失败后没有创建任何任务（`runtime.sqlite3` 无该会话任务记录）。

### 1.2 完整时序（从请求工件还原）
| 时间 | 事件 |
|---|---|
| 17:24:22 | 模型调用 `submit_next_stage`，stage-2 建立成功 |
| 17:24:48 | 模型调用 `create_async_task`，参数违规：`file_targets="[]"`（字符串）、`execution_policy='{"mode":"focus"}'`（字符串） |
| —— | 工具返回 `Error: file_targets should be array`，轮次已记入 canonical context |
| 17:24:54 | 模型重试请求完成（148 tokens），约 130ms 后崩溃 |
| 17:26:00 | 用户“继续创建”，从 active 的 stage-2（带错误轮次）恢复 |
| 17:26:09 | 模型请求完成（258 tokens），约 130ms 后再次崩溃 |

### 1.3 影响面
- 仅本会话受影响；同款“每日简报”在**新会话** `web:ceo-7b4254a363b9`（deepseek）成功创建任务 `task:a5c2afd7c0e0`。
- 崩溃的非预期副作用：会话卡在 active stage-2 + 错误轮次，继续“继续创建”会反复触发相同崩溃（错误重试死循环）。

---

## 2. 根因分析（已验证证据链）

### 2.1 直接触发：模型生成了类型违规的 tool 参数

| 会话 | 绑定 | `execution_policy` | `file_targets` | 结果 |
|---|---|---|---|---|
| e5e605ca53d3 | `responses:qwen3.8-max` | `{"mode": "focus"}` 的 JSON 字符串 | `"[]"`（字符串） | 工具校验失败，随后崩溃 |
| 7b4254a363b9 | `custom:deepseek-v4-flash-0731` | `{"mode": "coverage"}`（对象） | `[]`（数组） | 成功 |
| 7b4254a363b9（今早） | `custom:qwen3.8-max` | `{"mode": "coverage"}`（对象） | `[]`（数组） | 成功 |

关键点：**同一个 qwen3.8-max 走 `custom:`（OpenAI 兼容）协议时参数类型正确**。说明差异来自「模型 × 协议适配」组合，而非单单“qwen 不行”。

### 2.2 间接原因 A：工具契约 schema 类型放宽
`main/service/create_async_task_contract.py::build_create_async_task_parameters()`：

```python
"execution_policy": build_execution_policy_schema(...),   # 契约源为 {"type": "object"}
"file_targets": {"type": ["array", "null"], ...},          # union：数组或 null
```

- `file_targets` 的 `["array","null"]` 已确认存在于契约源码。
- `execution_policy` 呈现为 `["object","string"]` 的**注入点已于 2026-08-14 定位**（见 §2.2.1）：不是 `build_execution_policy_schema` → langchain `bind_tools` → provider 序列化链产生，而是 `tools/create_async_task_cn/resource.yaml` 中写死的工具 manifest 类型（web_ceo 前端加载的是资源工具 `EmbeddedMCPTool`，其 `model_parameters` 直接透传 manifest `parameters`）。已修复 manifest + 在 `sanitize_provider_parameters_schema` 增加通用防御。

### 2.2.1 union 注入点定位结论（2026-08-14 实测）

- 契约源（`main/service/runtime_service.py::CreateAsyncTaskTool`）与序列化链全程保持 `execution_policy.type == "object"`；`sanitize_provider_parameters_schema`、`normalize_openai_tool_definition`、`normalize_responses_tool_definition` 对 type 列表忠实透传，不产生也不消除 union。
- 真实下发路径（会话 e5e605ca53d3 的 `responses:qwen3.8-max` 绑定）走的是**资源工具**：`tools/create_async_task_cn/resource.yaml` L31-33 写死 `execution_policy: type: [object, string]`，经 `_model_visible_tool_contract` → `_provider_visible_tool_contract` 原样下发。这就是 union 来源。
- 全仓库源码扫描：`["object","string"]` 字面量仅存在于测试 fixture 与已修复的 manifest。

无论 union 注入点在哪，它都放大了模型“传字符串”的概率。

### 2.3 间接原因 B：错误后重试未修复参数类型
- `_normalize_frontdoor_tool_arguments()`（`g3ku/runtime/frontdoor/_ceo_runtime_ops.py:615`）已经能对 `execution_policy` 做字符串→dict 归一化，但**没有归一化 `file_targets`**。
- `CreateAsyncTaskTool.validate_params()` 先执行 schema 校验（`super().validate_params`），字符串 `"[]"` 与 `["array","null"]` 不匹配，直接报 `file_targets should be array`，根本走不到后面的 `validate_create_async_task_file_targets()`（该函数其实已能容忍字符串）。
- 于是每次重试都是同样的错，形成死循环。

### 2.4 崩溃根因：框架把 LangChain 消息对象当 dict
- 错误 `'AIMessage' object has no attribute 'get'` 一定来自 `(msg or {}).get(...)` 或 `msg.get(...)` 作用于 `AIMessage`。
- 触发窗口：工具轮次记录完成（canonical context 可见）之后、下一次模型调用之前——即 `CeoModelOutputMiddleware.aafter_model` / `CeoTurnLifecycleMiddleware.abefore_model` / `_postprocess_completed_tool_cycle` / `_sync_runtime_session_frontdoor_state` 一带，把图状态里的 LangChain `messages` 混入只接受 dict 的处理路径。
- 已用真实运行时 + 打桩模型做受控复现（工具违规→报错→重试、文本→finalize 两条路径均未崩），说明精确崩点依赖真实模型响应形态（适配层解析/重试后处理）；当前代码多数消息辅助函数已有 `isinstance(dict)` 防护，但存在未防护站点（见 §4.1 清单）。
- **为什么没保存堆栈**：Web 进程 stdout/stderr 未重定向到文件，关键 traceback 只进了失控的控制台日志。

### 2.5 为什么 deepseek 会话不出问题
崩溃只在 `create_async_task` **参数校验失败后的重试路径** 出现。deepseek 的参数总是合法，工具直接成功，不进入该分支。因此“deepseek 会话无此问题”是**未触发**，不是 deepseek 免疫。

---

## 3. 修复目标与原则

1. **目标**：相同场景下不再崩溃；参数违规时要么被自动归一化、要么安全失败并给模型可行动的反馈，而不是把整个会话拖进死循环。
2. **原则**：
   - 不改变 `create_async_task` 的对外行为语义（任务创建流程不动）。
   - 每个修复必须可独立验证、可回滚。
   - 工具契约只收紧允许类型，不新增必填项（避免影响 deepseek 等已正常的模型）。
   - 本次不追求“一次命中所有 `.get` 站点”，先治理与本崩溃强相关的路径。

---

## 4. 分层修复方案

### P0 —— 防崩溃（框架硬化，最高优先级）✅ 已实施（2026-08-14）

#### P0.1 消息记录辅助函数统一加 dict 防护 ✅ 已实施
对以下位置的 `(x or {}).get(...)` / `x.get(...)` 增加 `isinstance(x, dict)` 前置判断（x 非 dict 时视为无该字段/跳过）：

| 文件 | 行号/函数 | 问题 |
|---|---|---|
| `g3ku/runtime/stage_prompt_compaction.py` | `_message_role` / `repair_split_stage_tool_boundaries` | 直接遍历原始 `messages`（可能为 LangChain 对象）后 `.get("role")`、`.get("tool_calls")` |
| `g3ku/runtime/task_ledger.py` | `build_task_ledger_summary` | 对传入 item 无字典防护 |
| `g3ku/runtime/frontdoor/message_builder.py` | `_render_retrieved_context` 等 | 对 `record` 直接 `.get` |
| `g3ku/runtime/frontdoor/prompt_cache_contract.py` | `_contains_long_context_summary` | 对 `record` 直接 `.get` |
| `g3ku/runtime/context/types.py` | `ContextAssemblyResult.system_prompt/recent_history` | 对 `stable_messages` 元素直接 `.get`（当前无调用方，仍属隐患） |
| `main/runtime/react_loop.py` | `_dedupe_tool_messages`、`_extract_node_context_payload`、`_resume_pending_tool_turn_if_needed` 等 | 任务执行端同类风险 |

统一做法（示例）：
```python
def _safe_role(message) -> str:
    if not isinstance(message, dict):
        return ""
    return str(message.get("role") or "").strip().lower()
```

#### P0.2 “错误轮次重试”路径的消息类型隔离 ✅ 已实施
- 在 `_postprocess_completed_tool_cycle` / `_graph_execute_tools` / `_sync_runtime_session_frontdoor_state` 的入口，统一先经 `self._state_message_records(...)` 把所有 `state["messages"]` 转成 dict 记录后再处理（对现有实现做覆盖性审计，确保没有绕过转换的支路）。
- 审计准则：**凡是消费 `state.get("messages")` 的代码，第一个动作必须是 `_message_record`/`_state_message_records` 归一，或显式 `isinstance(dict)` 过滤**。

#### P0.3 失败重试不携带“半成品”消息 ✅ 已实施（审计结论：境内路径均已归一）
- `create_async_task` 契约错误结果已在 canonical context 入账后，重试请求的 `durable_request_messages` 构造应只包含 **schema 归一化后**的 tool 参数与结果，不把原始 `AIMessage`/`ToolMessage` 对象直接喂给后续轮次。
- 若一时无法定位注入点，最保守方案：在该路径外层包一层 `safe` 转换兜底（配合 P2.1 日志，先拿到真实堆栈再精修）。

### P1 —— 避免触发（契约与参数归一化）

#### P1.1 收紧工具契约 schema
`main/service/create_async_task_contract.py`：

```python
# 改动前（宽松，暗示可传字符串）
"execution_policy": build_execution_policy_schema(...),     # 实际下发为 ["object","string"]
"file_targets": {"type": ["array", "null"], ...},

# 改动后（严格类型 + 描述强调）   ✅ 已实施（2026-08-14）
"execution_policy": build_execution_policy_schema(...),     # type 恒为 "object"；description 强调"必须传 JSON 对象，不能传 JSON 字符串"
"file_targets": {"type": "array", "items": {...}, ...},     # 不允许 null/string；空任务用 []
```

配套（✅ 已实施）：
- 注入点定位与修复：`tools/create_async_task_cn/resource.yaml` 的 `execution_policy.type` 由 `[object, string]` 改为 `object`、`file_targets.type` 由 `[array, "null"]` 改为 `array`（web_ceo 前端模型可见契约）。
- 通用防御：`g3ku/json_schema_utils.py::sanitize_provider_parameters_schema` 入口新增 `_tighten_object_field_types`——带非空 `properties` 的 object 字段若 type 为含非 object 类型列表则收紧为 `"object"`（保留 null 可选性），开放 union（如 memory_write `value`）不受影响。即使未来任何 manifest/协议层再写死 union，收发边界仍被收紧。
- `build_execution_policy_schema` 的 description 已明确“必须传对象，不能传 JSON 字符串”（`main/models.py` 未改，改于调用方 `create_async_task_contract.py` 的描述常量）。

#### P1.2 工具参数自动归一化（工具侧兜底）✅ 已实施（2026-08-14）
扩展 `_normalize_frontdoor_tool_arguments()`（`_ceo_runtime_ops.py:615`，已实施）：
- `create_async_task.file_targets`：字符串时先尝试 `json.loads` 转数组；失败则按 `normalize_create_async_task_file_targets` 的字符串语义解释（`artifact:` 前缀→`ref`，否则→`path`）；`None` → `[]`。
- `execution_policy` 维持现有字符串→dict 逻辑。

`CreateAsyncTaskTool.validate_params()`（`main/service/runtime_service.py` 约 8941 行起）入口先做同一归一化，再调用 `super().validate_params(...)`（已实施，经新增 `normalize_create_async_task_inbound_params`）：

```python
def validate_params(self, params):
    normalized = normalize_create_async_task_inbound_params(params)  # file_targets / execution_policy 字符串 → 正确类型
    errors = super().validate_params(normalized)
    ...
```

这样即使参数从模型来是字符串，也能被纠正，而不是直接报 `file_targets should be array`。

#### P1.3（可选，防御）错误后自动注入工具契约 ✅ 已实施（2026-08-14）
当工具返回 `Error: ... should be array` 之类契约错误时，不再让模型盲试，而是在下一轮自动附带：
- 工具契约错误提示 + 修正后的示例参数；或
- 直接提示先调用 `load_tool_context(tool_id="create_async_task")`。
注入位置：`g3ku/runtime/frontdoor/_ceo_support.py::_execute_tool_call_with_raw_result` 构造错误文本处（已实施：新增 `_append_contract_error_example`，对 `create_async_task` 的类型错误追加 `execution_policy`/`file_targets` 合法示例，与 `load_tool_context` 指引并存）。

### P2 —— 可观测性（避免再次盲修） ✅ 已实施（2026-08-14）

#### P2.1 持久化运行日志 ✅ 已实施
- `g3ku_bootstrap.py`：`web` 命令子进程 stdout/stderr 追加重定向至 `.g3ku/logs/console.log`（`PYTHONUNBUFFERED=1`）；父进程 loguru 滚动文件 sink（50MB/7 天）作为补充。非 web 命令路径不变。
- 记录内容：session_key、provider_model、tool_name、tool 参数摘要、异常 traceback —— 见 `session_agent._persist_runtime_error_file`（P2.2）。

#### P2.2 异常落盘 ✅ 已实施
- `g3ku/runtime/session_agent.py::_prompt_locked` 兜底捕获分支：新增 `_persist_runtime_error_file`，把 `exc` 完整 traceback 与关联上下文（session_key、route_kind、internal_source、user_text、最后一个工具交互摘要）写入 `.g3ku/errors/<timestamp>-<session>.log`。真实端到端已验证（`test_ceo_runtime_progress.py` 运行中自动落盘成功）。

#### P2.3 关键快照随请求工件落盘
- 请求工件已含大部分信息。附加项：合约 schema 下发差异已由 §2.2.1 定位并根治（manifest + sanitize 防御），无需额外监控槽；模型 tool 参数原始 JSON 可经 P2.1 web 日志捕获。本项按需推进，暂无阻塞。

### P3 —— 会话与数据治理

- **已完成的现场恢复**（2026-08-14）：删除复现临时产物；按会话记录重建 `web-ceo-continuity/web_ceo-e5e605ca53d3.json` 至崩溃前状态（stage-1 完成 / stage-2 active + 1 条 create_async_task 错误轮次）；未改动会话记录（9 行）与任务库，未创建任务。
- **后续**：建议提供管理入口，允许用户“清空卡死的 active stage / 重置本会话前端状态”，避免反复“继续创建”死循环；崩溃会话可引导用户开新会话继续（已证实新会话可正常完成任务）。

---

## 5. 实施步骤（建议分 PR）

| PR | 内容 | 涉及文件 | 验收 |
|---|---|---|---|
| PR-0 | 日志与异常落盘（P2 全部） | `g3ku_bootstrap.py` / `session_agent.py` | 制造一次同类错误能拿到完整堆栈 |
| PR-1 | P0 防崩溃（P0.1+P0.2） | `stage_prompt_compaction.py`、`_ceo_runtime_ops.py`、`_ceo_support.py`、`message_builder.py` 等 | 复现脚本不再崩；回归测试通过 |
| PR-2 | P1 契约与归一化（P1.1+P1.2，含 union 注入点定位修复） | `create_async_task_contract.py`、`runtime_service.py`、`_ceo_runtime_ops.py` | qwen3.8-max（responses 绑定）违规参数被自动纠正或安全失败 |
| PR-3 | P1.3 防御性契约注入 + P3 治理入口 | `_ceo_support.py`、web/manager 层 | 错误后模型能拿到可行动反馈 |
| PR-4 | 全量回归 + 文档更新 | 测试、本文档 | 新旧会话均正常 |

---

## 6. 验证方案

### 6.1 单元测试
- `create_async_task_contract`：`file_targets`/`execution_policy` 为字符串、`"[]"`、JSON 字符串、null、正确数组/对象 六种输入的 normalize + validate 结果。
- `_normalize_frontdoor_tool_arguments`：字符串 policy、字符串 file_targets 的归一化输出与边界（非法 JSON、空串）。
- 消息辅助函数：输入 `AIMessage`/`ToolMessage`/`dict` 混合列表时无 AttributeError。
- schema 下发链：`build_create_async_task_parameters` → langchain 绑定 → provider 序列化，断言 `execution_policy.type == "object"`（不接受 `["object","string"]`）。

### 6.2 集成复现脚本（沿用已建立的受控复现方式）
- 真实 `AgentLoop` + 打桩 `_call_model_with_tools`，覆盖：
  1. 工具调用（违规参数）→ 工具报错 → 重试工具调用 → 最终文本回复；
  2. 工具调用 → 报错 → 直接文本回复（finalize）；
  3. 文本回复直达 finalize（stage-2 active 恢复场景）。
- 断言：全程无 `'AIMessage' object has no attribute 'get'`；`file_targets="[]"` 时工具正常接收数组而非报错。
- union 注入点定位：✅ 2026-08-14 已完成（结论见 §2.2.1，注入点 = `tools/create_async_task_cn/resource.yaml` manifest；探针脚本 `temp/union_probe_execution_policy.py` 可复跑验证）。原来拟在 `_PydanticSchemaBuilder._annotation_for` / `sanitize_provider_parameters_schema` / `normalize_responses_tool_definition` 加断点——实测证明该链对 type 列表忠实透传（PR-2 内完成）。

### 6.3 回归
- deepseek 会话全流程（创建每日简报任务）不受影响；
- 新会话 qwen3.8-max（custom）不受影响；
- 旧的失败会话继续“继续创建”时行为变成：工具安全失败/被纠正，不再崩溃。

---

## 7. 风险与回滚

| 风险 | 缓解 | 回滚 |
|---|---|---|
| schema 收紧导致个别模型（如 qwen3.8-max responses 绑定）不产出合法参数 | P1.2 归一化兜底 + P1.3 示例提示 | 单独回滚 PR-2，P0 仍防崩溃 |
| `.get` 防护改变返回语义（如空串 vs None） | 只影响“非 dict”分支，正常 dict 路径不变；跑现有单测 | 单独回滚 PR-1 |
| 日志量增长 | 只落 traceback/摘要，不入全量对话 | 关闭 sink 即可 |
| 重建的 continuity 与运行中服务内存状态不一致 | 已与崩溃前状态对齐；运行中服务下次交互会以内存态为准重写 | 删除/替换 continuity 文件后重启服务即可 |

---

## 8. 附录：关键代码位置索引

| 文件 | 位置 | 说明 |
|---|---|---|
| `main/service/create_async_task_contract.py` | `build_create_async_task_parameters()`（L118）/ `normalize_create_async_task_file_targets()`（L99）/ `validate_create_async_task_file_targets()` | 契约 schema 与参数校验 |
| `main/models.py` | `build_execution_policy_schema()`（L338） | execution_policy schema（源为 object） |
| `tools/create_async_task_cn/resource.yaml` | `execution_policy.type`（原 `[object, string]`） | 注入点已定位（2026-08-14）：web_ceo 前端资源工具的模型可见 manifest；已改为 `object`、`file_targets` 改为 `array` |
| `main/service/runtime_service.py` | `class CreateAsyncTaskTool`（约 L8941） | 工具 validate/execute 入口，P1.2 归一化落点 |
| `g3ku/runtime/frontdoor/_ceo_runtime_ops.py` | `_normalize_frontdoor_tool_arguments`（L615）、`_postprocess_completed_tool_cycle`、`_sync_runtime_session_frontdoor_state`、`_build_frontdoor_provider_request_body_preview`（L1176）、`_call_model_with_tools`（L4846） | 前端参数归一化、重试后处理、请求体预览（union 可能注入点） |
| `g3ku/runtime/frontdoor/ceo_agent_middleware.py` | `CeoModelOutputMiddleware.aafter_model` / `CeoTurnLifecycleMiddleware.abefore_model` | 崩溃高发窗口 |
| `g3ku/runtime/frontdoor/_ceo_support.py` | `_execute_tool_call_with_raw_result` | 工具错误文本构造 / P1.3 落点 |
| `g3ku/runtime/session_agent.py` | `_prompt_locked` 兜底捕获 | 异常落盘（P2.2）落点 |
| `g3ku/json_schema_utils.py` | `_PydanticSchemaBuilder` / `sanitize_provider_parameters_schema`（L143）/ `normalize_openai_tool_definition`（L354）/ `normalize_responses_tool_definition`（L389）/ `_tool_definition_from_runtime_tool` | schema 序列化链，union 注入点排查范围 |
| `g3ku/runtime/stage_prompt_compaction.py` | `_message_role` / `repair_split_stage_tool_boundaries` | P0.1 首要硬化点（react_loop 侧）✅ |
| `g3ku/runtime/frontdoor/message_builder.py` | `_render_retrieved_context` 等消费记录站点 | P0.1 硬化点 ✅ |
| `g3ku/runtime/context/types.py` | `ContextAssemblyResult.system_prompt` / `recent_history` | P0.1 硬化点 ✅ |
| `main/runtime/react_loop.py` | `_dedupe_tool_messages`、`_extract_node_context_payload`、`_resume_pending_tool_turn_if_needed` 等消息站点 | P0.1 任务执行端硬化点 ✅ |
| `g3ku/runtime/session_agent.py` | `_prompt_locked` 兜底捕获（约 L2702） | P2.2 异常落盘落点 ✅ |
| `g3ku_bootstrap.py` | `main()` web 分支 | P2.1 web 启动日志落点 ✅ |
| `g3ku/runtime/tool_error_guidance.py` | `append_parameter_error_guidance` | 与 P1.3 示例注入并存 ✅ |

## 9. 后续待办（明确未完成项）

- [x] PR-0：日志/异常落盘 ✅ 2026-08-14（`g3ku_bootstrap.py` + `session_agent.py::_persist_runtime_error_file`；`.g3ku/errors/` 端到端验证有效）
- [x] PR-1：消息 `.get` 防护 + 重试路径类型隔离 ✅ 2026-08-14（P0.1 全部站点 + P0.2/P0.3 审计；包含 `_ceo_runtime_ops`/`_ceo_create_agent_impl`/`react_loop`/`stage_prompt_compaction`/`message_builder`/`prompt_cache_contract`/`task_ledger`/`context.types`）
- [x] PR-2：schema 收紧 + union 注入点定位修复 + 参数归一化 ✅ 2026-08-14（契约 `file_targets.type`/`execution_policy` 收紧；注入点 = `tools/create_async_task_cn/resource.yaml` manifest，已修；`sanitize_provider_parameters_schema` 防御；`CreateAsyncTaskTool.validate_params` + `_normalize_frontdoor_tool_arguments` 归一化）
- [x] PR-3：错误后契约/示例注入 ✅ 2026-08-14（`_ceo_support._append_contract_error_example`）；会话重置入口 ⬜ 未实施（建议后续单独 PR：web/manager 层提供"清空卡死 active stage / 重置前端状态"入口）
- [x] PR-4：回归与文档 ✅ 2026-08-14（新增 `tests/resources/test_aimessage_crash_fixes.py` 16 用例 + 更新 `test_tool_resource_admin_api.py` 类型断言；全量 1861 passed，5 个失败经基线对照确认为既有环境性失败，与本改动无关；本文档 §2/§4/§8/§9 更新）
- [ ] 复测会话 e5e605ca53d3：“继续创建”不再崩溃 ⬜ 需要真实 qwen3.8-max（responses 绑定）会话环境复现；代码层面已由 P0.1/P0.2 防护 + P1.2 归一化覆盖，建议在具备该模型链的环境复测

### 已知既有失败（与本次改动无关，基线 HEAD 复现）

以下 5 个测试在无任何改动（HEAD）下同样失败，非本修复引入：
- `test_export_node_prompt_rounds.py::test_export_node_prompt_rounds_script_exports_two_consecutive_requests`
- `test_memory_manager_recovery.py::test_run_due_batch_once_drops_request_already_recorded_in_processed_log`
- `test_resource_runtime_smoke.py::test_tool_managed_builtin_family_defaults_to_deny_all_roles_on_first_discovery`
- `test_subprocess_text_tool_integration.py::test_agent_browser_run_command_decodes_legacy_codepage_output`
- `test_tool_resource_admin_api.py::test_recreate_runtime_session_enriches_completed_continuity_only_restore_with_matching_actual_request_trace`
