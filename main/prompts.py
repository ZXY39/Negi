from __future__ import annotations

from pathlib import Path

PROMPTS_DIR = Path(__file__).resolve().parent / 'prompts'

STRICT_CHILD_SPAWN_POLICY_PROMPT = """
注意，接下来的任务符合以下至少一条时，立即拆分任务，派生子节点完成：
1. 任务开放，必须在执行中灵活探索旁支，例如需要多方面多角度的进行任务；
2. 任务存在冲突领域，必须隔离推理上下文，例如需要从正反面等对立点进行任务；
3. 任务可以并行推进，例如有多个相互独立，工作量大的任务；
4. 在自行完成一部分任务后，发现任务量明显高于预期，继续自行完成会显著降低效率。
确认以上都不满足时，才自主完成任务：

派生子节点时，需要判断：
- 范围窄、低复杂度、低风险、不易出错的子任务，设置 `requires_acceptance=false`。
- 范围广、跨多来源、需要一致性核对、复杂推理复核的子任务，设置 `requires_acceptance=true`，并提供明确的 `acceptance_prompt`。"""

def load_prompt(name: str) -> str:
    return (PROMPTS_DIR / name).read_text(encoding='utf-8')
