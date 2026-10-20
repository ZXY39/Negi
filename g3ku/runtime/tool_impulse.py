"""冲动调用（未水合工具被直接调用）那一跳的投递语义。

两条车道共用这一份，回执才可能逐字节同形：前门在 `_frontdoor_executable_tool_names` 拒
名字，节点在 `_execute_tool_raw` 里判契约在场与否，两边分支结构一样、文案一样、暂存规则
一样。

三个结果（D3）：

- `staged`：入参校验通过 ⇒ 落 stash、交付契约块、**不执行**、本轮不占预算；
- `invalid`：校验失败 ⇒ 交付契约块 + 参数错误、不落 stash、**占一轮**（全免会让模型零成本
  换个错法再试，而现有熔断按相同签名判，挡不住这种漂移）；
- `reject`：不是"没水合"这一类（名字无权限、资源下架、压根没这个工具）⇒ 沿用既有拒绝回执。

同轮同工具只发一份完整块（D4）：第 2..N 份只带一行指向首条。
"""

from __future__ import annotations

import json
from typing import Any

from g3ku.agent.tools.base import REUSE_ARGS_PARAMETER_NAME
from g3ku.runtime.tool_arg_stash import staged_targets

IMPULSE_STAGED = "staged"
IMPULSE_INVALID = "invalid"
IMPULSE_REJECT = "reject"

# 循环/前门自己的跳级状态：在场索引、暂存载体、同批计数。它们只服务准入与记账，**不进**
# 工具可见的 `__g3ku_runtime`——在场索引随契约数量增长，逐次调用都带一份既没用又压不动的
# 字典，等于把上下文预算的账算到每个工具头上。两条车道共用这一份名单。
LOOP_INTERNAL_RUNTIME_KEYS = frozenset(
    {
        "tool_contract_presence",
        "tool_arg_stash_carrier",
        "impulse_delivered_tools",
        "impulse_staged_call_ids",
        "impulse_batch_totals",
    }
)


def tool_facing_runtime_context(runtime_context: Any) -> dict[str, Any]:
    return {
        key: value
        for key, value in dict(runtime_context or {}).items()
        if str(key or "").strip() not in LOOP_INTERNAL_RUNTIME_KEYS
    }

def _render_args_line(args: Any) -> str:
    """一份入参一行，按内容完整渲染、不截断（撑爆的情形属计划 §5-5，要停下回报而不是悄悄裁）。

    枚举对**所有**工具都做，不只计划点名的那批不可逆工具：`exec`、`filesystem_write` 这类
    同样有副作用，而"这一批会跑掉什么"只在列全目标时才可核对。
    """
    return json.dumps(args, ensure_ascii=False, sort_keys=True, default=str)


def render_staged_targets(container: dict[str, Any], *, scope_key: str) -> str:
    """枚举该 scope 下全部已暂存入参。"""
    entries = staged_targets(container, scope_key=scope_key)
    if not entries:
        return ""
    lines = [f"  - {_render_args_line(entry.get('args'))}" for entry in entries]
    return "\n".join(lines)


def impulse_receipt(
    *,
    tool_name: str,
    contract_block: str,
    outcome: str,
    staged_count: int = 0,
    targets_text: str = "",
    batch_position: int = 1,
    batch_total: int = 1,
    parameter_error_text: str = "",
) -> dict[str, Any]:
    """产出这一跳交给模型的那一条工具结果文本。

    返回 `{"content": ..., "outcome": ..., "executed": False}`：`executed` 恒为假——这条
    路径不跑工具，调用方的"未执行不进熔断/去重"判据就认这个字段。
    """
    blocks: list[str] = []
    if batch_position > 1:
        # D4：同轮同工具只发一份完整块。第 2..N 份带块等于把同一篇正文按调用次数
        # 放大进历史，而在场判据只需要那一次出现。
        blocks.append(
            f'Contract for "{tool_name}": see the first result for this tool in this batch. '
            f"These arguments were staged (set {batch_position} of {batch_total})."
        )
        if outcome == IMPULSE_INVALID and parameter_error_text:
            blocks.append(parameter_error_text)
        return {
            "content": "\n\n".join(blocks),
            "outcome": outcome,
            "executed": False,
        }

    if outcome == IMPULSE_INVALID:
        blocks.append(
            f'Tool contract for "{tool_name}" loaded - the tool is now callable.\n'
            "This call's arguments failed validation. Nothing was staged."
        )
        if parameter_error_text:
            blocks.append(parameter_error_text)
    else:
        blocks.append(
            f'Tool contract for "{tool_name}" loaded - the tool is now callable.\n'
            f"This call's arguments passed validation and were staged (sets: {staged_count}).\n"
            f"Next turn: set reuse_args=true to execute all staged sets, or call with new arguments "
            "(staged sets are overwritten)."
        )
        if targets_text:
            blocks.append(f"Staged sets that reuse_args=true would execute:\n{targets_text}")
            blocks.append(
                "To run only some of them, do not use reuse_args - call the tool with the arguments you want."
            )
    content = "\n\n".join([contract_block, *blocks]) if contract_block else "\n\n".join(blocks)
    return {"content": content, "outcome": outcome, "executed": False}


# ---------------------------------------------------------------------------
# `reuse_args=true` 的执行计划（D7/D8）
# ---------------------------------------------------------------------------

REUSE_PLAN_EXECUTE = "execute"
REUSE_PLAN_EXPLICIT_ARGS = "explicit_args"
REUSE_PLAN_NO_STASH = "no_stash"
REUSE_PLAN_CONTRACT_ABSENT = "contract_absent"

REUSE_NO_STASH_NOTE = (
    'No staged arguments for "{tool}" to reuse (the tool was not called in your previous turn, '
    "or the staged set expired). Call it with arguments."
)
REUSE_CONTRACT_ABSENT_NOTE = (
    'The contract for "{tool}" left the context (compaction), and its staged arguments are '
    "invalid. Call it with arguments - the contract comes back with the result."
)
REUSE_EXPLICIT_ARGS_NOTE = "reuse_args ignored: executed with the arguments you provided."


def split_reuse_arguments(arguments: Any) -> tuple[bool, dict[str, Any]]:
    """拆出 `reuse_args` 标记与剩余显式入参；标记本身永远不当业务入参传下去。"""
    payload = dict(arguments or {}) if isinstance(arguments, dict) else {}
    raw_flag = payload.pop(REUSE_ARGS_PARAMETER_NAME, None)
    if isinstance(raw_flag, str):
        reuse = raw_flag.strip().lower() in {"true", "1", "yes"}
    else:
        reuse = bool(raw_flag)
    return reuse, payload


def plan_reuse_execution(
    *,
    reuse_requested: bool,
    explicit_arguments: dict[str, Any],
    contract_present: bool,
    staged_entries: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    """`reuse_args=true` 这一跳该怎么走，四条答案互斥。

    显式入参优先（C3）：模型两个都给时按它填的跑一次，被忽略的那件事必须在结果里说清，
    否则它会以为批量已经跑过。契约不在场时**不**执行任何一份（C2）——执行必须发生在契约
    对模型可见之后，这条不变量不因走的是复用路径而放宽。
    """
    if not reuse_requested:
        return {"kind": "", "arguments": dict(explicit_arguments or {}), "entries": []}
    if explicit_arguments:
        return {"kind": REUSE_PLAN_EXPLICIT_ARGS, "arguments": dict(explicit_arguments), "entries": []}
    entries = [dict(item) for item in list(staged_entries or []) if isinstance(item, dict)]
    if not contract_present:
        return {"kind": REUSE_PLAN_CONTRACT_ABSENT, "arguments": {}, "entries": entries}
    if not entries:
        return {"kind": REUSE_PLAN_NO_STASH, "arguments": {}, "entries": []}
    return {"kind": REUSE_PLAN_EXECUTE, "arguments": {}, "entries": entries}


def reuse_refusal_text(kind: str, tool_name: str) -> str:
    """非法复用（C1/C2）与显式入参优先（C3）的回执句。"""
    normalized = str(kind or "").strip()
    template = {
        REUSE_PLAN_NO_STASH: REUSE_NO_STASH_NOTE,
        REUSE_PLAN_CONTRACT_ABSENT: REUSE_CONTRACT_ABSENT_NOTE,
        REUSE_PLAN_EXPLICIT_ARGS: REUSE_EXPLICIT_ARGS_NOTE,
    }.get(normalized)
    if not template:
        return ""
    return template.format(tool=str(tool_name or "").strip())


def aggregate_reuse_results(*, tool_name: str, items: list[dict[str, Any]]) -> str:
    """N 份结果聚成**一个** tool_result（一个 call id 恰好一个结果块）。

    顶层 `ok` 只回答"这批跑了"，逐项成败在内层：把任何一份的失败抬到顶层，模型就会以为
    整批没执行，于是重发 reuse_args 再跑一遍已成功的那些。
    """
    normalized = [dict(item) for item in list(items or []) if isinstance(item, dict)]
    return json.dumps(
        {
            "ok": True,
            "tool": str(tool_name or "").strip(),
            "executed": len(normalized),
            "items": normalized,
        },
        ensure_ascii=False,
        sort_keys=True,
    )


__all__ = [
    "IMPULSE_INVALID",
    "IMPULSE_REJECT",
    "IMPULSE_STAGED",
    "LOOP_INTERNAL_RUNTIME_KEYS",
    "REUSE_CONTRACT_ABSENT_NOTE",
    "REUSE_EXPLICIT_ARGS_NOTE",
    "REUSE_NO_STASH_NOTE",
    "REUSE_PLAN_CONTRACT_ABSENT",
    "REUSE_PLAN_EXECUTE",
    "REUSE_PLAN_EXPLICIT_ARGS",
    "REUSE_PLAN_NO_STASH",
    "aggregate_reuse_results",
    "impulse_receipt",
    "plan_reuse_execution",
    "render_staged_targets",
    "reuse_refusal_text",
    "split_reuse_arguments",
    "tool_facing_runtime_context",
]
