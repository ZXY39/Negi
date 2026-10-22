from __future__ import annotations

import json
from datetime import datetime
from typing import Any

FRONTDOOR_TURNS_MAX = 200


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="microseconds")


def _normalized_tool(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    tool_call_id = str(raw.get("tool_call_id") or "").strip()
    tool_name = str(raw.get("tool_name") or "").strip()
    if not tool_call_id and not tool_name:
        return None
    return {
        "tool_call_id": tool_call_id,
        "tool_name": tool_name or "tool",
        "status": str(raw.get("status") or "").strip(),
        "arguments_text": str(raw.get("arguments_text") or ""),
        "arguments_ref": str(raw.get("arguments_ref") or ""),
        "output_text": str(raw.get("output_text") or ""),
        "output_ref": str(raw.get("output_ref") or ""),
        "started_at": str(raw.get("started_at") or ""),
        "finished_at": str(raw.get("finished_at") or ""),
        "elapsed_seconds": (
            float(raw.get("elapsed_seconds"))
            if isinstance(raw.get("elapsed_seconds"), (int, float))
            else None
        ),
    }


def normalize_frontdoor_turns(raw: Any) -> list[dict[str, Any]]:
    turns: list[dict[str, Any]] = []
    for item in list(raw or []):
        if not isinstance(item, dict):
            continue
        seq = int(item.get("seq") or 0)
        if seq <= 0:
            continue
        tools = [
            tool
            for tool in (_normalized_tool(candidate) for candidate in list(item.get("tools") or []))
            if tool is not None
        ]
        turns.append(
            {
                "seq": seq,
                "turn_id": str(item.get("turn_id") or "").strip(),
                "text": str(item.get("text") or ""),
                "text_ref": str(item.get("text_ref") or ""),
                "created_at": str(item.get("created_at") or ""),
                "status": str(item.get("status") or ""),
                "tools": tools,
            }
        )
    turns.sort(key=lambda turn: int(turn.get("seq") or 0))
    return turns[-FRONTDOOR_TURNS_MAX:]


def append_frontdoor_turn(
    turns: Any,
    *,
    turn_id: str,
    text: str,
    text_ref: str = "",
    tools: list[dict[str, Any]] | None = None,
    created_at: str = "",
) -> list[dict[str, Any]]:
    normalized = normalize_frontdoor_turns(turns)
    tool_payload = [
        tool for tool in (_normalized_tool(candidate) for candidate in list(tools or [])) if tool is not None
    ]
    tool_ids = {str(tool.get("tool_call_id") or "").strip() for tool in tool_payload if tool.get("tool_call_id")}
    for index, existing in enumerate(normalized):
        if (
            tool_ids
            and str(existing.get("turn_id") or "").strip() == str(turn_id or "").strip()
            and tool_ids
            == {str(tool.get("tool_call_id") or "").strip() for tool in list(existing.get("tools") or []) if tool.get("tool_call_id")}
        ):
            normalized[index] = {
                **existing,
                "text": str(text or existing.get("text") or ""),
                "text_ref": str(text_ref or existing.get("text_ref") or ""),
                "tools": tool_payload,
            }
            return normalize_frontdoor_turns(normalized)
    seq = max((int(turn.get("seq") or 0) for turn in normalized), default=0) + 1
    normalized.append(
        {
            "seq": seq,
            "turn_id": str(turn_id or "").strip(),
            "text": str(text or ""),
            "text_ref": str(text_ref or ""),
            "created_at": str(created_at or _now_iso()),
            "status": "completed",
            "tools": tool_payload,
        }
    )
    return normalize_frontdoor_turns(normalized)


def build_frontdoor_turn_tools(
    tool_call_payloads: Any,
    tool_results: Any,
) -> list[dict[str, Any]]:
    results = list(tool_results or [])
    tools: list[dict[str, Any]] = []
    for index, payload in enumerate(list(tool_call_payloads or [])):
        if not isinstance(payload, dict):
            continue
        result = dict(results[index]) if index < len(results) and isinstance(results[index], dict) else {}
        arguments = payload.get("arguments")
        if isinstance(arguments, (dict, list)):
            arguments_text = json.dumps(arguments, ensure_ascii=False, indent=2)
        else:
            arguments_text = str(arguments or "")
        tools.append(
            {
                "tool_call_id": str(payload.get("id") or payload.get("tool_call_id") or "").strip(),
                "tool_name": str(payload.get("name") or payload.get("tool_name") or "tool").strip() or "tool",
                "status": str(result.get("status") or "success").strip() or "success",
                "arguments_text": arguments_text,
                "arguments_ref": "",
                "output_text": str(result.get("result_text") or ""),
                "output_ref": str(result.get("output_ref") or ""),
                "started_at": str(result.get("started_at") or ""),
                "finished_at": str(result.get("finished_at") or ""),
                "elapsed_seconds": None,
            }
        )
    return tools


def frontdoor_turns_for_turn(turns: Any, turn_id: str) -> list[dict[str, Any]]:
    normalized_turn_id = str(turn_id or "").strip()
    if not normalized_turn_id:
        return []
    return [
        turn
        for turn in normalize_frontdoor_turns(turns)
        if str(turn.get("turn_id") or "").strip() == normalized_turn_id
    ]


def find_frontdoor_turn_tool(turns: Any, tool_call_id: str) -> dict[str, Any] | None:
    normalized_call_id = str(tool_call_id or "").strip()
    if not normalized_call_id:
        return None
    for turn in normalize_frontdoor_turns(turns):
        for tool in list(turn.get("tools") or []):
            if str(tool.get("tool_call_id") or "").strip() == normalized_call_id:
                return dict(tool)
    return None


__all__ = [
    "FRONTDOOR_TURNS_MAX",
    "append_frontdoor_turn",
    "build_frontdoor_turn_tools",
    "find_frontdoor_turn_tool",
    "frontdoor_turns_for_turn",
    "normalize_frontdoor_turns",
]
