from __future__ import annotations

from pathlib import Path

PROMPTS_DIR = Path(__file__).resolve().parent / 'prompts'

EXECUTION_STAGE_POLICY_PROMPT = """
你必须按阶段推进当前执行节点，具体要求：
1. 在开始任何普通工具调用或派生子节点之前，必须先调用 `submit_next_stage` 创建当前阶段。
2. 每个阶段都必须提供 `stage_goal` 和 `tool_round_budget`；`tool_round_budget` 必须是 1 到 10 的整数。
3. `stage_goal` 必须清晰说明当前阶段的完成目标，为了提高完成效率，目标中只要有互不交叉可以并行的任务，就必须派生子节点完成，此类任务不允许自主完成。
4. 除了创建新阶段之外，你必须在任何时候都只能围绕当前阶段目标推进；如果下一步已经不属于当前阶段目标，就先基于已完成工作创建下一阶段。
当前阶段达到 `tool_round_budget` 后:
1. 优先考虑下一阶段是否可以通过增加派生子节点来避免继续超预算。
2. 必须先总结尚未完成的工作，然后创建下一阶段。
3. 必要时适当放大下一阶段的 `tool_round_budget`，但上限仍是 10。
"""

def load_prompt(name: str) -> str:
    return (PROMPTS_DIR / name).read_text(encoding='utf-8')
