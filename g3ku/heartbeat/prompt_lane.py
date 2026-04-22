from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class HeartbeatPromptLane:
    scope: str
    stable_messages: list[dict[str, Any]]
    dynamic_appendix_messages: list[dict[str, Any]]
    request_messages: list[dict[str, Any]]
    retrieval_query: str


def _non_empty_text(value: Any) -> str:
    return str(value or "").strip()


def _append_retrieval_parts(parts: list[str], *values: Any) -> None:
    for value in values:
        text = _non_empty_text(value)
        if not text or text.startswith("artifact:"):
            continue
        parts.append(text)


def _float_value(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _int_value(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)


def _tool_background_lines(event: dict[str, Any], retrieval_parts: list[str]) -> list[str]:
    tool_name = _non_empty_text(event.get("tool_name")) or "tool"
    execution_id = _non_empty_text(event.get("execution_id"))
    status = _non_empty_text(event.get("status")) or "background_running"
    snapshot = event.get("runtime_snapshot") if isinstance(event.get("runtime_snapshot"), dict) else {}
    summary = _non_empty_text(snapshot.get("summary_text") or event.get("message")) or "No snapshot summary."
    elapsed_seconds = _float_value(event.get("elapsed_seconds"), 0.0)
    wait_seconds = _float_value(event.get("recommended_wait_seconds"), 0.0)
    _append_retrieval_parts(
        retrieval_parts,
        "tool_background",
        tool_name,
        execution_id,
        status,
        summary,
        f"{elapsed_seconds:.1f}s",
    )
    return [
        f"- Background tool {tool_name} ({execution_id}) is still running",
        f"  Status: {status}",
        f"  Elapsed: {elapsed_seconds:.1f}s",
        f"  Next scheduled heartbeat: {wait_seconds:.1f}s",
        f"  Snapshot: {summary}",
        "  Current rule: keep waiting in the same turn unless you decide to stop it.",
        "  Allowed tool: stop_tool_execution",
    ]


def _tool_terminal_lines(event: dict[str, Any], retrieval_parts: list[str]) -> list[str]:
    tool_name = _non_empty_text(event.get("tool_name")) or "tool"
    execution_id = _non_empty_text(event.get("execution_id"))
    status = _non_empty_text(event.get("status")) or "completed"
    summary = _non_empty_text(event.get("message") or event.get("final_result") or event.get("error")) or "No terminal summary."
    _append_retrieval_parts(retrieval_parts, "tool_terminal", tool_name, execution_id, status, summary)
    return [
        f"- Background tool {tool_name} ({execution_id}) reached a terminal state",
        f"  Status: {status}",
        f"  Summary: {summary}",
    ]


def _task_stall_lines(event: dict[str, Any], retrieval_parts: list[str]) -> list[str]:
    task_id = _non_empty_text(event.get("task_id"))
    title = _non_empty_text(event.get("title")) or task_id or "task"
    stall_reason = (
        _non_empty_text(event.get("stall_reason"))
        or _non_empty_text(event.get("task_stall_reason"))
        or _non_empty_text(event.get("reason"))
        or "suspected_stall"
    )
    stalled_minutes = _int_value(event.get("stalled_minutes"), 0)
    bucket_minutes = _int_value(event.get("bucket_minutes"), 0)
    brief_text = _non_empty_text(event.get("brief_text")) or "No task summary."
    latest_node_summary = _non_empty_text(event.get("latest_node_summary")) or "No latest node summary."
    runtime_excerpt = _non_empty_text(event.get("runtime_summary_excerpt")) or "No runtime summary."
    last_visible_output_at = _non_empty_text(event.get("last_visible_output_at")) or "unknown"
    _append_retrieval_parts(
        retrieval_parts,
        "task_stall",
        task_id,
        title,
        stall_reason,
        brief_text,
        latest_node_summary,
        runtime_excerpt,
    )
    if stall_reason == "user_paused":
        return [
            f"- Task {title} ({task_id}) was paused by the user",
            "  Reason: user_paused",
            "  Do not investigate the task yet. Wait for the user to resume or redirect it.",
        ]
    if stall_reason == "worker_unavailable":
        return [
            f"- Task {title} ({task_id}) is waiting for the worker to come back",
            "  Reason: worker_unavailable",
            "  Do not treat this as a task logic stall yet. Wait for worker recovery or restart.",
        ]
    return [
        f"- Task {title} ({task_id}) may be stalled",
        f"  Reason: {stall_reason}",
        f"  Silent for: {stalled_minutes} min",
        f"  Trigger bucket: {bucket_minutes} min",
        f"  Last visible output at: {last_visible_output_at}",
        f"  Brief: {brief_text}",
        f"  Latest node: {latest_node_summary}",
        f"  Runtime: {runtime_excerpt}",
        "  Suggested first step: task_progress(task_id)",
        "  If needed: stop_tool_execution(task_id)",
    ]


def _task_terminal_lines(event: dict[str, Any], retrieval_parts: list[str], *, output_inline_limit: int) -> list[str]:
    title = _non_empty_text(event.get("title") or event.get("task_id")) or "task"
    task_id = _non_empty_text(event.get("task_id"))
    status = _non_empty_text(event.get("status")) or "unknown"
    summary = _non_empty_text(event.get("brief_text") or event.get("failure_reason")) or "No summary."
    terminal_node_id = _non_empty_text(event.get("terminal_node_id"))
    terminal_node_kind = _non_empty_text(event.get("terminal_node_kind")) or "execution"
    terminal_reason = _non_empty_text(event.get("terminal_node_reason"))
    terminal_output = _non_empty_text(event.get("terminal_output"))
    terminal_output_ref = _non_empty_text(event.get("terminal_output_ref"))
    terminal_check_result = _non_empty_text(event.get("terminal_check_result"))
    terminal_failure_reason = _non_empty_text(event.get("terminal_failure_reason"))
    root_output = _non_empty_text(event.get("root_output"))
    root_output_ref = _non_empty_text(event.get("root_output_ref"))
    _append_retrieval_parts(
        retrieval_parts,
        "task_terminal",
        title,
        task_id,
        status,
        summary,
        terminal_node_kind,
        terminal_node_id,
        terminal_reason,
        terminal_output[:output_inline_limit] if terminal_output else "",
        terminal_check_result,
        terminal_failure_reason,
        root_output[:output_inline_limit] if root_output and root_output != terminal_output else "",
    )
    lines = [
        f"- Task {title} ({task_id}) completed",
        f"  Status: {status}",
        f"  Summary: {summary}",
    ]
    if terminal_node_id:
        lines.append(f"  Result node: {terminal_node_kind} {terminal_node_id}")
    if terminal_reason:
        lines.append(f"  Result source: {terminal_reason}")
    if terminal_output:
        if len(terminal_output) > output_inline_limit:
            excerpt = terminal_output[:output_inline_limit].rstrip()
            lines.append(f"  Result output excerpt: {excerpt}...")
        else:
            lines.append(f"  Result output: {terminal_output}")
    if terminal_output_ref:
        lines.append(f"  Result output ref: {terminal_output_ref}")
    if root_output and root_output != terminal_output:
        if len(root_output) > output_inline_limit:
            excerpt = root_output[:output_inline_limit].rstrip()
            lines.append(f"  Execution output excerpt: {excerpt}...")
        else:
            lines.append(f"  Execution output: {root_output}")
    if root_output_ref and root_output_ref != terminal_output_ref:
        lines.append(f"  Execution output ref: {root_output_ref}")
    if terminal_check_result:
        lines.append(f"  Result check: {terminal_check_result}")
    if terminal_failure_reason and terminal_failure_reason != summary:
        lines.append(f"  Result failure reason: {terminal_failure_reason}")
    return lines


def _event_bundle_content(events: list[dict[str, Any]], *, output_inline_limit: int) -> tuple[str, str]:
    lines = ["[SESSION EVENTS]", "## EVENT BUNDLE"]
    retrieval_parts: list[str] = []
    for raw_event in list(events or []):
        event = dict(raw_event or {})
        reason = _non_empty_text(event.get("event_reason") or event.get("kind") or event.get("reason")).lower()
        if reason == "tool_background":
            lines.extend(_tool_background_lines(event, retrieval_parts))
            continue
        if reason == "tool_terminal":
            lines.extend(_tool_terminal_lines(event, retrieval_parts))
            continue
        if reason == "task_stall":
            lines.extend(_task_stall_lines(event, retrieval_parts))
            continue
        lines.extend(
            _task_terminal_lines(
                event,
                retrieval_parts,
                output_inline_limit=output_inline_limit,
            )
        )
    return "\n".join(lines).strip(), " ".join(part for part in retrieval_parts if part).strip()


def build_heartbeat_prompt_lane(
    *,
    provider_model: str,
    stable_rules_text: str,
    task_ledger_summary: str,
    events: list[dict[str, Any]],
    output_inline_limit: int = 4000,
) -> HeartbeatPromptLane:
    _ = provider_model
    stable_messages: list[dict[str, Any]] = []
    stable_rules = _non_empty_text(stable_rules_text)
    if stable_rules:
        stable_messages.append({"role": "system", "content": stable_rules})
    ledger = _non_empty_text(task_ledger_summary)
    if ledger:
        stable_messages.append({"role": "assistant", "content": ledger})
    event_bundle_text, retrieval_query = _event_bundle_content(
        list(events or []),
        output_inline_limit=output_inline_limit,
    )
    dynamic_appendix_messages = (
        [{"role": "user", "content": event_bundle_text}]
        if event_bundle_text
        else []
    )
    request_messages = [*stable_messages, *dynamic_appendix_messages]
    combined_user_sections = [
        str(message.get("content") or "").strip()
        for message in [*stable_messages, *dynamic_appendix_messages]
        if str(message.get("content") or "").strip()
    ]
    if combined_user_sections:
        request_messages.append(
            {
                "role": "user",
                "content": "\n\n".join(combined_user_sections).strip(),
            }
        )
    return HeartbeatPromptLane(
        scope="ceo_heartbeat",
        stable_messages=stable_messages,
        dynamic_appendix_messages=dynamic_appendix_messages,
        request_messages=request_messages,
        retrieval_query=retrieval_query,
    )


__all__ = ["HeartbeatPromptLane", "build_heartbeat_prompt_lane"]
