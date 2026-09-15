# 孤儿工具结果（orphan tool result）缺陷族修复计划

日期：2026-09-15
状态：已批准（用户指示：按计划修复已知问题）

## 背景

两条独立取证链（见 `repair_tmp/analysis/workflow_result_full.txt`、`repair_tmp/analysis/audit_result_full.txt`）确认了一个缺陷族：**历史压缩/清理在裁剪消息时不保护 `assistant(tool_calls)` ↔ `role=tool` 配对**，产生"孤儿工具结果"（工具结果存在、声明它的 assistant 消息缺失）。

1. **已遂事故（节点通道）**：`task:25745b5268dc` 根节点 2026-09-15 13:17–13:35 四次熔断失败。
   `react_loop._split_request_messages_for_token_compaction` 的保留尾部是纯位置切片
   `body_messages[-12:]`（react_loop.py:4366-4368），尾部起点落在工具调用对中间时产生孤儿，
   3-strike 熔断（:108/:560-567/:6827）→ error-pause → 管家 resume → 复现 ×4。
   用生产函数对冻结的 628 条历史做前缀扫描，N=599/610/619/628 四个长度逐字节复现全部 4 个孤儿 ID。
2. **已遂缺陷（会话通道 stage 清理）**：`is_stage_context_message`（stage_prompt_compaction.py:52-67）
   缺 `tool_calls` 守卫——assistant 消息只要内容以 `[G3KU_STAGE_*]` 开头即被整块丢弃（含 tool_calls），
   其 tool 结果留在原地 → 孤儿。生产实锤：会话 `ext:qq-official:f8a8001865631301` 连续 5 天、
   301 个请求每轮携带同样 2 个孤儿（`call_f7779e2c…` / `call_9260f1c9…`）发给供应商，
   且固化进连续性基线与轮边界快照，永久重放。节点通道共用同一组 helper
   （react_loop.py:6858-6863,:6884），故此漏洞在节点通道同样可触发同款熔断签名。
3. **潜在同款（会话通道 token 压缩）**：frontdoor 压缩是节点 bug 的机械孪生——
   `_ceo_runtime_ops.py:1801/:1819-1820` 纯位置切片 `min(len(body),4)`，
   唯一的尾部后处理只做 16000 字符截断（:748-778）。留存语料中 0 次发作，
   但前置条件已在生产出现（11 个请求体以单批 4-7 个并行工具结果收尾）。
   且**会话通道没有任何孤儿检测器**（analyze_tool_call_history 仅被节点熔断器与
   Responses 协议清洗器使用），发作即为静默。

## 修复项

### 1.【P0】压缩尾部边界配对对齐（双通道）

新增共享辅助函数（放在 `g3ku/runtime/tool_history.py`，两通道均已/可依赖该模块，避免 main↔g3ku 反向导入）：

```
align_compaction_keep_recent(messages, keep_recent) -> int
```

语义：从 `keep_recent` 起步向前扩展尾部边界，直到尾部首条消息不是
"声明位于尾部之外的 tool 结果"——即**边界不得落在未闭合工具调用对中间**。
规则：当 `messages[-k]` 的 role 为 tool 时 k+=1（其声明必然在更早位置，须一并保留），
直到首条非 tool 消息或 k == len(messages)。扩展上界为整个 body（此时无可压缩历史，
调用方按既有"无可压缩历史"分支处理——节点侧已超窗则按文档失败，不静默）。
扩展量上界 = 一个工具批次的消息数（生产中 1-7 条），不影响文档承诺的收敛保证
（尾部字符截断 `_bound_node_compaction_tail_messages` / `_bound_frontdoor_compaction_tail_messages`
仍在对齐之后照常执行）。

接线点：
- `main/runtime/react_loop.py` `_split_request_messages_for_token_compaction`（:4366-4368 之前）
- `g3ku/runtime/frontdoor/_ceo_runtime_ops.py` frontdoor token 压缩切片（:1801/:1819-1820 之前）

### 2.【P0】`is_stage_context_message` 补 `tool_calls` 守卫

`g3ku/runtime/stage_prompt_compaction.py:52-67`：消息携带非空 `tool_calls` 时返回 `False`
（语义对齐 `main/runtime/node_prompt_contract.py:394-395` 的 `_message_declares_tool_calls` 守卫；
因 g3ku 不得导入 main，在本模块内联等价判断）。
一处谓词修复同时关闭三个消费点：`stage_prompt_prefix`（:121-125）、compact step-0（:409-418）、
`keep_stage_blocks_off_continuation_tail`（:135-161），并覆盖节点/会话双通道。

### 3.【P1】熔断路径加日志

`react_loop._orphan_tool_result_failure`（:6827）返回前输出一条 warning：
task_id / node_id / orphan call ids / strike 计数。用 `try/except Exception` 包裹
（磁盘满时不得把干净的熔断暂停污染成通用异常暂停；node_runner 对 DB 写入已有同类守卫模式）。
`react_loop.py` 目前无 logger，引入 `from loguru import logger`（node_runner 同款）。

### 4.【P1】回归测试

- `tests/resources/test_react_runtime_regressions.py`（压缩测试群 :4745-4879 旁）：
  - 尾部起点落在单批并行工具结果中间（声明在压缩区）→ 重写请求零孤儿（analyze_tool_call_history 断言）
  - 尾部起点为 assistant 声明（健康边界不回退）
  - 尾部起点落在两结果批次的第二个结果 → 声明+第一结果被拉回尾部
  - 全部为 tool 消息的极端历史 → 边界退化到整体，不产生孤儿、不抛异常
  - frontdoor 切片同款用例（tail=4）
- `tests/resources/test_stage_prompt_compaction.py`：assistant 回声 `[G3KU_STAGE_COMPACT_V1]` 且携带
  tool_calls → `is_stage_context_message` 返回 False；step-0 不丢弃、不产生孤儿
  （形状参照 `tests/resources/test_node_prompt_contract.py:230-268` 的既有守卫断言）。

### 5.【P2，不自动执行】存量固化孤儿治愈（数据手术）

`ext:qq-official:f8a8001865631301` 的连续性 sidecar（`.g3ku/web-ceo-continuity/…json`
的 `frontdoor_request_body_messages[93]/[94]`）与两份轮边界快照内嵌同一孤儿对。
代码修复不能治愈已固化数据。方案：准备一次性脚本（先备份再改写，或按
`repair_split_stage_tool_boundaries`(:251-301) 的先例补桩声明，或连同其结果一并移除），
**仅在该会话空闲且获得明确批准后执行**——运行时可能正在使用该文件。

## 不在本次范围（记录为 follow-up）

- chat/completions wire 级孤儿清洗器（推广 responses_protocol_helpers.py:124-187；需先定策略：丢弃+告警 vs 补桩）
- 会话通道检测对齐（发送前 analyze_tool_call_history 诊断/断路器）
- `repair_split_stage_tool_boundaries` 扩展到任意工具名（当前仅 submit_next_stage）
- `has_dangling_assistant_calls` 无消费者（反向分裂盲区）
- seed adoption 配对探针（react_loop.py:6981-7049 与 _ceo_runtime_ops.py:2332-2366）
- `task_events` 停写与 `managed-worker.log` 断流（独立排查会话进行中，结论回来后再定）

## 验证

1. `python -m ruff check .`（或 `scripts/lint.ps1`）
2. pytest：`tests/resources/test_react_runtime_regressions.py`、`tests/resources/test_stage_prompt_compaction.py`、
   `tests/resources/test_node_prompt_contract.py`、`tests/resources/test_ceo_context_window_overflow.py`、
   冒烟 `tests/resources/test_resource_runtime_smoke.py`
3. 事故复算：用修复后的生产函数对 `repair_tmp/analysis` 冻结的 628 条历史重跑前缀扫描，
   断言 N=599/610/619/628（及 585..628 全区间）重写请求零孤儿。

## 文档维护（AGENTS.md 要求）

改动触及运行时行为与契约：完成后用 `g3ku-architecture-maintenance` skill 维护
`docs/architecture/runtime-overview.md`（token_compression 契约补"尾部边界与工具调用配对不变量"；
stage_compaction 契约补"带 tool_calls 的阶段回声不识别为阶段块"）及
`docs/architecture/context-and-cache-troubleshooting.md`（如适用）。

## 提交

验证通过后按仓库风格提交（Conventional Commits + 中文描述），不 push。
