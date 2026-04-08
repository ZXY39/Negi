from __future__ import annotations

from typing import Any


_PREVIEW_CHAR_LIMIT = 160
_RAW_OUTPUT_FALLBACK_PREVIEW_LIMIT = 24


def _preview_text(value: Any, *, limit: int = _PREVIEW_CHAR_LIMIT) -> str:
    compact = " ".join(str(value or "").split()).strip()
    if len(compact) <= limit:
        return compact
    return f"{compact[: limit - 3].rstrip()}..."


def _raw_output_fallback_preview(value: Any) -> str:
    compact = " ".join(str(value or "").split()).strip()
    if not compact:
        return ""
    if len(compact) <= 8:
        return "output available"
    preview_limit = min(_RAW_OUTPUT_FALLBACK_PREVIEW_LIMIT, max(len(compact) - 1, 8))
    snippet = compact[:preview_limit].rstrip()
    if len(snippet) >= len(compact):
        snippet = compact[: max(min(len(compact) - 1, 12), 1)].rstrip()
    return f"{snippet}..." if snippet else "output available"


def compact_tool_step_for_summary(step: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(step, dict):
        return None

    arguments_preview = _preview_text(step.get("arguments_preview") or step.get("arguments_text"))
    output_preview = _preview_text(step.get("output_preview") or step.get("output_preview_text"))
    if not output_preview:
        output_preview = _raw_output_fallback_preview(step.get("output_text") or step.get("text"))

    payload: dict[str, Any] = {
        "tool_call_id": str(step.get("tool_call_id") or "").strip(),
        "tool_name": str(step.get("tool_name") or "").strip() or "tool",
        "output_ref": str(step.get("output_ref") or "").strip(),
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
