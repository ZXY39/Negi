from __future__ import annotations

from typing import Any

from g3ku.runtime.tool_impulse import CONTRACT_DELIVERY_FIELD
from main.monitoring.execution_trace import thinking_reading_for_display

_PREVIEW_CHAR_LIMIT = 160
_RAW_OUTPUT_FALLBACK_PREVIEW_LIMIT = 24


def _has_summary_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    return True


def _first_present(step: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key not in step:
            continue
        value = step.get(key)
        if _has_summary_value(value):
            return value
    return None


def _preview_text(value: Any, *, limit: int = _PREVIEW_CHAR_LIMIT) -> str:
    if value is None:
        return ""
    compact = " ".join(str(value).split()).strip()
    if len(compact) <= limit:
        return compact
    return f"{compact[: limit - 3].rstrip()}..."


def _raw_output_fallback_preview(value: Any, *, has_output_ref: bool) -> str:
    compact = _preview_text(value, limit=_RAW_OUTPUT_FALLBACK_PREVIEW_LIMIT)
    if not compact:
        return ""
    if has_output_ref:
        return "output captured in ref"
    return compact


def compact_tool_step_for_summary(step: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(step, dict):
        return None

    arguments_preview = _preview_text(_first_present(step, "arguments_preview", "arguments_text"))
    output_ref = str(step.get("output_ref") or "").strip()
    output_preview = _preview_text(_first_present(step, "output_preview", "output_preview_text"))
    if not output_preview:
        output_preview = _raw_output_fallback_preview(
            _first_present(step, "output_text", "text"),
            has_output_ref=bool(output_ref),
        )

    payload: dict[str, Any] = {
        "tool_call_id": str(step.get("tool_call_id") or "").strip(),
        "tool_name": str(step.get("tool_name") or "").strip() or "tool",
        "output_ref": output_ref,
        "status": str(step.get("status") or "").strip(),
        "started_at": str(step.get("started_at") or "").strip(),
        "finished_at": str(step.get("finished_at") or "").strip(),
    }
    if arguments_preview:
        payload["arguments_preview"] = arguments_preview
    if output_preview:
        payload["output_preview"] = output_preview

    elapsed_seconds = step.get("elapsed_seconds")
    if elapsed_seconds is not None:
        payload["elapsed_seconds"] = elapsed_seconds

    recovery_decision = str(step.get("recovery_decision") or "").strip()
    if recovery_decision:
        payload["recovery_decision"] = recovery_decision

    related_tool_call_ids = [
        str(item or "").strip()
        for item in list(step.get("related_tool_call_ids") or [])
        if str(item or "").strip()
    ]
    if related_tool_call_ids:
        payload["related_tool_call_ids"] = related_tool_call_ids

    attempted_tools = [
        str(item or "").strip()
        for item in list(step.get("attempted_tools") or [])
        if str(item or "").strip()
    ]
    if attempted_tools:
        payload["attempted_tools"] = attempted_tools

    evidence = [dict(item) for item in list(step.get("evidence") or []) if isinstance(item, dict)]
    if evidence:
        payload["evidence"] = evidence

    lost_result_summary = str(step.get("lost_result_summary") or "").strip()
    if lost_result_summary:
        payload["lost_result_summary"] = lost_result_summary

    timestamp = str(step.get("timestamp") or "").strip()
    if timestamp:
        payload["timestamp"] = timestamp

    kind = str(step.get("kind") or "").strip()
    if kind:
        payload["kind"] = kind

    source = str(step.get("source") or "").strip()
    if source:
        payload["source"] = source

    # 展示标记不是簿记位：摘要档是节点详情在完整轨迹缺席时唯一读的那份，
    # 漏了它"加载 tool"卡片就退回成一次普通工具调用。
    contract_delivery = str(step.get(CONTRACT_DELIVERY_FIELD) or "").strip()
    if contract_delivery:
        payload[CONTRACT_DELIVERY_FIELD] = contract_delivery

    return payload


def build_execution_trace_summary(execution_trace: dict[str, Any] | None) -> dict[str, Any]:
    """整份轨迹 → 节点详情用的摘要台账。写侧存的那份与读侧现算的那份必须是这一个函数。

    两条读路（行内存量摘要 / 从外置轨迹现算）过去各用一个构造器，现算那份保留完整
    入参与出参，同一个节点的 `execution_trace_summary` 实测 430 KB 对 2.29 MB；而压实
    存量摘要时又只认 `arguments_text`，把行内的 `arguments_preview` 丢掉，实盘
    `node:8e2382a036e3` 的 425 条工具行 0 条保住参数。
    """
    trace = execution_trace if isinstance(execution_trace, dict) else {}
    turns_payload: list[dict[str, Any]] = []
    for turn in list(trace.get("turns") or []):
        if not isinstance(turn, dict):
            continue
        compact_tools = [
            compact_step
            for compact_step in (
                compact_tool_step_for_summary(step)
                for step in list(turn.get("tools") or [])
            )
            if compact_step is not None
        ]
        turns_payload.append(
            {
                "seq": int(turn.get("seq") or 0),
                "text": str(turn.get("text") or ""),
                "text_ref": str(turn.get("text_ref") or ""),
                "created_at": str(turn.get("created_at") or ""),
                "status": str(turn.get("status") or ""),
                "thinking": thinking_reading_for_display(turn.get("thinking")),
                "tools": compact_tools,
            }
        )
    tool_steps_payload: list[dict[str, Any]] = []
    for step in list(trace.get("tool_steps") or []):
        compact_step = compact_tool_step_for_summary(step)
        if compact_step is not None:
            tool_steps_payload.append(compact_step)
    return {"turns": turns_payload, "tool_steps": tool_steps_payload}
