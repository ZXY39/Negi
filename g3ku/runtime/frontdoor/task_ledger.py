from __future__ import annotations

from typing import Any


def _non_empty_text(value: Any) -> str:
    return str(value or "").strip()


def _task_ids(payload: Any) -> list[str]:
    ids: list[str] = []
    for raw in list((payload or {}).get("task_ids") or []):
        task_id = str(raw or "").strip()
        if not task_id or task_id in ids:
            continue
        ids.append(task_id)
    return ids


def _task_results(payload: Any) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    for raw in list((payload or {}).get("task_results") or []):
        if not isinstance(raw, dict):
            continue
        item = {
            "task_id": _non_empty_text(raw.get("task_id")),
            "node_id": _non_empty_text(raw.get("node_id")),
            "node_kind": _non_empty_text(raw.get("node_kind")),
            "node_reason": _non_empty_text(raw.get("node_reason")),
            "output_excerpt": _non_empty_text(raw.get("output_excerpt")),
            "output_ref": _non_empty_text(raw.get("output_ref")),
            "check_result": _non_empty_text(raw.get("check_result")),
            "failure_reason": _non_empty_text(raw.get("failure_reason")),
        }
        if not item["task_id"]:
            continue
        items.append({key: value for key, value in item.items() if value})
    return items


def build_task_ledger_summary(task_memory: Any) -> str:
    payload = dict(task_memory or {}) if isinstance(task_memory, dict) else {}
    task_ids = _task_ids(payload)
    task_results = _task_results(payload)
    if not task_ids and not task_results:
        return ""

    lines = [
        "## Task Ledger",
        "- Use this ledger for task continuity; hidden internal status turns are intentionally excluded from chat history.",
    ]
    if task_ids:
        lines.append(f"- Recent task ids: {', '.join(f'`{task_id}`' for task_id in task_ids)}")
    source = _non_empty_text(payload.get("source"))
    if source:
        lines.append(f"- Latest task source: {source}")
    reason = _non_empty_text(payload.get("reason"))
    if reason:
        lines.append(f"- Latest task reason: {reason}")
    updated_at = _non_empty_text(payload.get("updated_at"))
    if updated_at:
        lines.append(f"- Ledger updated at: {updated_at}")
    for item in task_results[:3]:
        parts: list[str] = []
        if item.get("node_kind") or item.get("node_id"):
            node_parts = [
                value
                for value in (item.get("node_kind"), item.get("node_id"))
                if str(value or "").strip()
            ]
            parts.append(f"node {' '.join(node_parts)}")
        if item.get("node_reason"):
            parts.append(f"source {item['node_reason']}")
        if item.get("output_excerpt"):
            parts.append(f"output {item['output_excerpt']}")
        if item.get("check_result"):
            parts.append(f"check {item['check_result']}")
        if item.get("failure_reason"):
            parts.append(f"failure {item['failure_reason']}")
        if item.get("output_ref"):
            parts.append(f"ref {item['output_ref']}")
        summary = "; ".join(parts)
        if summary:
            lines.append(f"- `{item['task_id']}`: {summary}")
        else:
            lines.append(f"- `{item['task_id']}`")
    return "\n".join(lines).strip()
