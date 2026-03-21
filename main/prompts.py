from __future__ import annotations

from pathlib import Path

PROMPTS_DIR = Path(__file__).resolve().parent / 'prompts'

STRICT_CHILD_SPAWN_POLICY_PROMPT = """
先按第一性原理收敛：接下来的任务符合以下至少一条时，立即拆分任务，派生子节点完成：
1. 任务极度开放，必须在执行中灵活探索旁支，例如需要多方面多角度地推进任务。
2. 任务存在两个以上冲突领域，必须隔离推理上下文，例如需要从正反面等对立点进行任务。
3. 任务可以并行推进，例如有多个相互独立且工作量大的子任务。
4. 在自行完成一部分任务后，发现任务量明显高于预期，继续自行完成会显著降低效率。

协议要求：
- 当 `can_spawn_children=true`，且你本轮准备调用至少一个非控制工具时，`tool_calls[0]` 必须是 `spawn_precheck`。
- `spawn_precheck` 必须使用与普通工具调用完全一致的结构：`id/name/arguments`。
- `spawn_precheck.arguments` 必须严格包含以下字段：
  - `decision`: `spawn_child_nodes` 或 `continue_self_execute`
  - `reason`: 本轮选择原因
  - `rule_ids`: 规则编号数组，只能填 1、2、3、4
  - `rule_semantics`: `matched` 或 `unmatched`
- 若 `decision=spawn_child_nodes`：
  - `rule_semantics` 必须为 `matched`
  - 本轮后续只能调用 `spawn_child_nodes`
- 若 `decision=continue_self_execute`：
  - `rule_semantics` 必须为 `unmatched`
  - 本轮后续不得调用 `spawn_child_nodes`
- 若本轮直接返回最终结果 JSON，则不需要 `spawn_precheck`。
- 若本轮只调用控制工具 `wait_tool_execution` / `stop_tool_execution`，则不需要 `spawn_precheck`。

派生子节点时，需要判断：
- 范围窄、低复杂度、低风险、不易出错的子任务，设置 `requires_acceptance=false`。
- 范围广、跨多来源、需要一致性核对、复杂推理复核的子任务，设置 `requires_acceptance=true`，并提供明确的 `acceptance_prompt`。"""

def load_prompt(name: str) -> str:
    return (PROMPTS_DIR / name).read_text(encoding='utf-8')
