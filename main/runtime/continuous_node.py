"""连续（待命）节点的共享判据与文案。

单一真值是节点 metadata 的 `continuous` 位：创建时写定，之后不可变。
验收条款是确定性常量——它进验收节点 bootstrap、`acceptance_prompt_template`
与恢复指纹，任何随运行时机变化的内容都会让崩溃重放建出第二颗验收节点。
"""

from __future__ import annotations

from typing import Any

CONTINUOUS_METADATA_KEY = 'continuous'

CONTINUOUS_ACCEPTANCE_CLAUSE = (
    '被检验节点是连续型节点：它的职责是长期待命并按消息/事件响应，不是一次性交付。\n'
    '判定口径：\n'
    '- 只有当它明确声明职责已结束、且给出的收尾交付可核验时，裁 pass。\n'
    '- 若它只是阶段性汇报、或仍在等待外部消息/事件，裁 reject，并在 feedback 里要求它继续待命。\n'
    '- 首轮不要用 reject_no_progress（该档首轮禁用）。'
)


def is_continuous_metadata(metadata: Any) -> bool:
    if not isinstance(metadata, dict):
        return False
    return bool(metadata.get(CONTINUOUS_METADATA_KEY))


def compose_continuous_acceptance_prompt(prompt: str) -> str:
    body = str(prompt or '').strip()
    if not body:
        return CONTINUOUS_ACCEPTANCE_CLAUSE
    return f'{CONTINUOUS_ACCEPTANCE_CLAUSE}\n\n{body}'


def auto_assigns_acceptance(*, continuous: bool, acceptance_prompt: str) -> bool:
    """验收是运行时替连续节点补出来的，而不是模型自己写了判据。"""
    if not bool(continuous):
        return False
    return not str(acceptance_prompt or '').strip()
