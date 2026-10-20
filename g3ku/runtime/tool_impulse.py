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

from g3ku.runtime.tool_arg_stash import staged_targets

IMPULSE_STAGED = "staged"
IMPULSE_INVALID = "invalid"
IMPULSE_REJECT = "reject"

# 不可逆/外显类工具：`reuse_args=true` 会把当轮**全部**暂存执行掉，所以首次回执必须把每一
# 份目标都列出来（D16）。列不全＝模型看不见自己要批量做什么。
IRREVERSIBLE_TOOL_NAMES = frozenset(
    {
        "filesystem_delete",
        "process_kill",
        "create_async_task",
        "task_append_notice",
        "memory_update",
        "manage_task_nodes",
        "cron",
        "task_delete",
    }
)


def _render_args_line(args: Any) -> str:
    """一份入参一行，按内容完整渲染、不截断（撑爆的情形属计划 §5-5，要停下回报而不是悄悄裁）。"""
    return json.dumps(args, ensure_ascii=False, sort_keys=True, default=str)


def render_staged_targets(container: dict[str, Any], *, scope_key: str, tool_name: str) -> str:
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
        blocks.append(
            f'Contract for "{tool_name}": see the first result for this tool in this batch. '
            f"These arguments were staged (set {batch_position} of {batch_total})."
        )
        if outcome == IMPULSE_INVALID and parameter_error_text:
            blocks.append(parameter_error_text)
        return {"content": "\n\n".join([contract_block, *blocks]) if contract_block else "\n\n".join(blocks),
                "outcome": outcome,
                "executed": False}

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


def must_enumerate_targets(tool_name: str) -> bool:
    """不可逆/批量外显类必须列全目标值。"""
    return str(tool_name or "").strip() in IRREVERSIBLE_TOOL_NAMES


__all__ = [
    "IMPULSE_INVALID",
    "IMPULSE_REJECT",
    "IMPULSE_STAGED",
    "IRREVERSIBLE_TOOL_NAMES",
    "impulse_receipt",
    "must_enumerate_targets",
    "render_staged_targets",
]
