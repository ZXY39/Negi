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
from typing import Any, Callable

from g3ku.agent.tools.base import REUSE_ARGS_PARAMETER_NAME
from g3ku.runtime.tool_arg_stash import DIGEST_FIELD

IMPULSE_STAGED = "staged"
IMPULSE_INVALID = "invalid"
IMPULSE_REJECT = "reject"

# 调用行上的投递标记：这一跳没有执行工具，只是把契约交回。Web 面据此把该行画成
# "加载 tool" 卡片（会话侧另弹 notice），而不是一次普通工具调用。值取上面的 outcome。
CONTRACT_DELIVERY_FIELD = "contract_delivery"

# 循环/前门自己的跳级状态：在场索引、暂存载体、同批计数。它们只服务准入与记账，**不进**
# 工具可见的 `__g3ku_runtime`——在场索引随契约数量增长，逐次调用都带一份既没用又压不动的
# 字典，等于把上下文预算的账算到每个工具头上。两条车道共用这一份名单。
LOOP_INTERNAL_RUNTIME_KEYS = frozenset(
    {
        "tool_contract_presence",
        "tool_arg_stash_carrier",
        "tool_arg_stash_swept",
        "tool_execution_paths",
        "reuse_batch_in_progress",
        "impulse_delivered_tools",
        "impulse_staged_call_ids",
        "impulse_batch_totals",
        "tool_contract_deliveries",
    }
)


def tool_facing_runtime_context(runtime_context: Any) -> dict[str, Any]:
    return {
        key: value
        for key, value in dict(runtime_context or {}).items()
        if str(key or "").strip() not in LOOP_INTERNAL_RUNTIME_KEYS
    }

def _staged_sets_line(*, tool_name: str, staged_count: int, batch_total: int) -> str:
    """这一批最终会被 `reuse_args` 执行多少份，以及它是从几条调用来的。

    不列目标清单：回执紧贴在发起它们的那条 assistant 行之后，模型自己那几条
    `tool_calls[].function.arguments` 就在同一请求视图里（实盘 202,788 条这样的行），再抄一遍
    是纯重复且长留历史；裁撤把工具肉身移走时，那行入参与这条回执成对离场，所以清单也兜不住
    任何"入参已不在场"的窗口。模型真正推不出来的是**份数**：同批同签名会折叠成一条，校验失败
    的那条也不入库，两种都会让份数少于调用数——所以这里只说两个事实（几份、几条调用），
    不替运行时猜是哪一种。
    """
    if staged_count and batch_total > staged_count:
        return f'Staged sets for "{tool_name}": {staged_count} (from {batch_total} calls).'
    return f'Staged sets for "{tool_name}": {staged_count}.'


def impulse_receipt(
    *,
    tool_name: str,
    contract_block: str,
    outcome: str,
    staged_count: int = 0,
    batch_position: int = 1,
    batch_total: int = 1,
    parameter_error_text: str = "",
) -> dict[str, Any]:
    """产出这一跳交给模型的那一条工具结果文本。

    返回 `{"content": ..., "outcome": ..., "executed": False}`：`executed` 恒为假——这条
    路径不跑工具，调用方的"未执行不进熔断/去重"判据就认这个字段。
    """
    is_last_of_batch = int(batch_position or 0) == int(batch_total or 0)
    blocks: list[str] = []
    if batch_position > 1:
        # D4：同轮同工具只发一份完整块。第 2..N 份带块等于把同一篇正文按调用次数
        # 放大进历史，而在场判据只需要那一次出现。
        blocks.append(
            f'Contract for "{tool_name}": see the first result for this tool in this batch.'
        )
    if outcome == IMPULSE_INVALID:
        if batch_position > 1:
            blocks.append(
                f"These arguments failed validation and were not staged "
                f"(set {batch_position} of {batch_total})."
            )
        else:
            blocks.append(
                f'Tool contract for "{tool_name}" loaded - the tool is now callable.\n'
                "This call's arguments failed validation. Nothing was staged."
            )
        if parameter_error_text:
            blocks.append(parameter_error_text)
    elif batch_position > 1:
        blocks.append(f"These arguments were staged (set {batch_position} of {batch_total}).")
    else:
        blocks.append(
            f'Tool contract for "{tool_name}" loaded - the tool is now callable.\n'
            "This call's arguments passed validation and were staged."
        )
    if is_last_of_batch and (outcome != IMPULSE_INVALID or batch_total > 1):
        # 份数只在最后一条报：投递逐条发生，前面的条只看得见半批。末条恰好失败时也要报，
        # 否则模型只握着若干 `set i of n`，拼不出下一跳会跑多少。
        blocks.append(
            _staged_sets_line(
                tool_name=tool_name,
                staged_count=staged_count,
                batch_total=batch_total,
            )
        )
    # D4：同批第 2..N 条**不带块**——块只有这一跳那一份，重复带等于按调用次数放大正文。
    head = contract_block if contract_block and batch_position <= 1 else ""
    content = "\n\n".join([head, *blocks]) if head else "\n\n".join(blocks)
    return {"content": content, "outcome": outcome, "executed": False}


# ---------------------------------------------------------------------------
# `reuse_args=true` 的执行计划（D7/D8）
# ---------------------------------------------------------------------------

REUSE_PLAN_EXECUTE = "execute"
REUSE_PLAN_EXPLICIT_ARGS = "explicit_args"
REUSE_PLAN_NO_STASH = "no_stash"
REUSE_PLAN_CONTRACT_ABSENT = "contract_absent"

# 审计用的"这次调用实际走的道"（D15）。五档里 `abandoned` 不挂在任何一次调用上——它是
# "暂存过却再没被调用"，由跳针推进时的清扫统计记（见 `staged_swept_tool_ids`）。
EXECUTION_PATH_NORMAL = "normal"
EXECUTION_PATH_REUSED = "reused"
EXECUTION_PATH_CORRECTED = "corrected"
EXECUTION_PATH_ILLEGAL_REUSE = "illegal_reuse"
EXECUTION_PATH_ABANDONED = "abandoned"
EXECUTION_PATHS = (
    EXECUTION_PATH_NORMAL,
    EXECUTION_PATH_REUSED,
    EXECUTION_PATH_CORRECTED,
    EXECUTION_PATH_ILLEGAL_REUSE,
    EXECUTION_PATH_ABANDONED,
)

REUSE_NO_STASH_NOTE = (
    'No staged arguments for "{tool}" to reuse (the tool was not called in your previous turn, '
    "or the staged set expired). Call it with arguments."
)
REUSE_CONTRACT_ABSENT_NOTE = (
    'The contract for "{tool}" left the context (compaction), and its staged arguments are '
    "invalid. Call it with arguments - the contract comes back with the result."
)
REUSE_EXPLICIT_ARGS_NOTE = "reuse_args ignored: executed with the arguments you provided."

# `execution_path` 的补格（D15）：`corrected` 一个词分不开两种完全不同的东西——模型把
# **同一份**已暂存的入参又重发一遍（该用 reuse 没用），和它改了参数重新调（本来就该重发，
# 因为 reuse 会跑全部份数）。基线要把 reuse 缺口数出来，靠的就是这一格。判据放在这里，
# 两条车道共用一份，避免两边各写一个摘要口径。
STAGED_REPLAY_FIELD = "staged_replay"
STAGED_REPLAY_IDENTICAL = "identical"
STAGED_REPLAY_NEW_ARGS = "new_args"
# 第三格：模型确实打了 `reuse_args`，但同时给了显式入参 ⇒ 标记被忽略（C3）。
# 它和"根本没打标记"在 `corrected` 里是同一个词，可修法的方向完全相反：一个是形状
# 不允许（schema 里业务参数照旧必填），一个是主动选了重发。没有这一格就只能猜。
REUSE_FLAG_IGNORED_FIELD = "reuse_flag_ignored"


def staged_replay_kind(
    *,
    staged_entries: list[dict[str, Any]] | None,
    arguments: Any,
    digest_of: Callable[[Any], str],
) -> str:
    """这一跳的显式入参是不是就是某一份已经暂存的入参。

    摘要必须由调用方给（`tool_arg_stash.args_digest`），因为暂存条目里存的那个摘要才是
    权威——用另一个函数算，折叠规则一变就悄悄错配。
    """
    entries = [dict(item) for item in list(staged_entries or []) if isinstance(item, dict)]
    if not entries:
        return ""
    digest = str(digest_of(arguments) or "").strip()
    for entry in entries:
        if str(entry.get(DIGEST_FIELD) or "").strip() == digest:
            return STAGED_REPLAY_IDENTICAL
    return STAGED_REPLAY_NEW_ARGS


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


class PreRenderedToolResult(str):
    """模型面文本已经逐份渲染并过完外置闸门的工具结果。

    交付层对它只做透传：复用批的聚合体必须整包进入上下文，不能再按单条结果判一次
    外置闸门。
    """

    status: str

    def __new__(cls, text: str, *, status: str = "success") -> "PreRenderedToolResult":
        instance = super().__new__(cls, text)
        instance.status = str(status or "success")
        return instance


def aggregate_reuse_results(*, tool_name: str, items: list[dict[str, Any]]) -> PreRenderedToolResult:
    """N 份结果聚成**一个** tool_result（一个 call id 恰好一个结果块）。

    顶层 `ok` 只回答"这批跑了"，逐项成败在内层：把任何一份的失败抬到顶层，模型就会以为
    整批没执行，于是重发 reuse_args 再跑一遍已成功的那些。`failed` 是失败份数，让模型
    不必逐项扫读也能看到失败。每份 item 已各自限界，聚合体以 `PreRenderedToolResult`
    返回，交付层整包透传、不再外置。
    """
    normalized = [dict(item) for item in list(items or []) if isinstance(item, dict)]
    failed = sum(1 for item in normalized if item.get("ok") is False)
    return PreRenderedToolResult(
        json.dumps(
            {
                "ok": True,
                "tool": str(tool_name or "").strip(),
                "executed": len(normalized),
                "failed": failed,
                "items": normalized,
            },
            ensure_ascii=False,
            sort_keys=True,
        ),
        status="success",
    )


__all__ = [
    "CONTRACT_DELIVERY_FIELD",
    "IMPULSE_INVALID",
    "IMPULSE_REJECT",
    "IMPULSE_STAGED",
    "LOOP_INTERNAL_RUNTIME_KEYS",
    "PreRenderedToolResult",
    "REUSE_CONTRACT_ABSENT_NOTE",
    "REUSE_EXPLICIT_ARGS_NOTE",
    "REUSE_NO_STASH_NOTE",
    "REUSE_PLAN_CONTRACT_ABSENT",
    "REUSE_PLAN_EXECUTE",
    "REUSE_PLAN_EXPLICIT_ARGS",
    "REUSE_PLAN_NO_STASH",
    "REUSE_FLAG_IGNORED_FIELD",
    "STAGED_REPLAY_FIELD",
    "STAGED_REPLAY_IDENTICAL",
    "STAGED_REPLAY_NEW_ARGS",
    "aggregate_reuse_results",
    "EXECUTION_PATHS",
    "EXECUTION_PATH_ABANDONED",
    "EXECUTION_PATH_CORRECTED",
    "EXECUTION_PATH_ILLEGAL_REUSE",
    "EXECUTION_PATH_NORMAL",
    "EXECUTION_PATH_REUSED",
    "impulse_receipt",
    "plan_reuse_execution",
    "reuse_refusal_text",
    "split_reuse_arguments",
    "staged_replay_kind",
    "tool_facing_runtime_context",
]
