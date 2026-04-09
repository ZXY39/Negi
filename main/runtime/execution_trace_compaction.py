from __future__ import annotations

from typing import Any


_PREVIEW_CHAR_LIMIT = 160
_RAW_OUTPUT_FALLBACK_PREVIEW_LIMIT = 24


def _first_present(step: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in step and step.get(key) is not None:
            return step.get(key)
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
