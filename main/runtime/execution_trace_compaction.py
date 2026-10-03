from __future__ import annotations

from typing import Any


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

    return payload


def build_execution_trace_summary(execution_trace: dict[str, Any] | None) -> dict[str, Any]:
    """整份轨迹 → 节点详情用的摘要台账。写侧存的那份与读侧现算的那份必须是这一个函数。

    两条读路（行内存量摘要 / 从外置轨迹现算）过去各用一个构造器，现算那份保留完整
    入参与出参，同一个节点的 `execution_trace_summary` 实测 430 KB 对 2.29 MB；而压实
    存量摘要时又只认 `arguments_text`，把行内的 `arguments_preview` 丢掉，实盘
    `node:8e2382a036e3` 的 425 条工具行 0 条保住参数。
    """
    trace = execution_trace if isinstance(execution_trace, dict) else {}
    stages_payload: list[dict[str, Any]] = []
    for stage in list(trace.get("stages") or []):
        if not isinstance(stage, dict):
            continue
        tool_calls: list[dict[str, Any]] = []
        rounds_payload: list[dict[str, Any]] = []
        for round_item in list(stage.get("rounds") or []):
            if not isinstance(round_item, dict):
                continue
            compact_tools: list[dict[str, Any]] = []
            for step in list(round_item.get("tools") or []):
                compact_step = compact_tool_step_for_summary(step)
                if compact_step is not None:
                    tool_calls.append(compact_step)
                    compact_tools.append(compact_step)
            rounds_payload.append(
                {
                    "round_id": str(round_item.get("round_id") or ""),
                    "round_index": int(round_item.get("round_index") or 0),
                    "created_at": str(round_item.get("created_at") or ""),
                    "text": str(round_item.get("text") or ""),
                    "budget_counted": bool(round_item.get("budget_counted")),
                    "tools": compact_tools,
                }
            )
        stages_payload.append(
            {
                "stage_id": str(stage.get("stage_id") or ""),
                "stage_index": int(stage.get("stage_index") or 0),
                "mode": str(stage.get("mode") or ""),
                "status": str(stage.get("status") or ""),
                "stage_goal": str(stage.get("stage_goal") or ""),
                "completed_stage_summary": str(stage.get("completed_stage_summary") or ""),
                "tool_round_budget": int(stage.get("tool_round_budget") or 0),
                "tool_rounds_used": int(stage.get("tool_rounds_used") or 0),
                "created_at": str(stage.get("created_at") or ""),
                "finished_at": str(stage.get("finished_at") or ""),
                # 摘要侧也要带上裁撤标记：节点详情在完整轨迹缺席时读的就是这份，
                # 漏掉它阶段卡只会把"模型点名移出上下文"画成普通"完成"。
                **({"context_evicted": True} if stage.get("context_evicted") is True else {}),
                "rounds": rounds_payload,
                "tool_calls": tool_calls,
            }
        )
    if stages_payload:
        return {"stages": stages_payload}
    fallback_tool_calls: list[dict[str, Any]] = []
    for step in list(trace.get("tool_steps") or []):
        compact_step = compact_tool_step_for_summary(step)
        if compact_step is not None:
            fallback_tool_calls.append(compact_step)
    if fallback_tool_calls:
        return {
            "stages": [{
                "stage_goal": "",
                "rounds": [{
                    "round_id": "",
                    "round_index": 1,
                    "created_at": "",
                    "budget_counted": False,
                    "tools": fallback_tool_calls,
                }],
                "tool_calls": fallback_tool_calls,
            }]
        }
    return {"stages": []}
