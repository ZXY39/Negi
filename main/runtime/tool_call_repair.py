from __future__ import annotations

import hashlib
import json
import re
from typing import Any

import json_repair

from g3ku.providers.base import ToolCallRequest

XML_REPAIR_ATTEMPT_LIMIT = 3
XML_REPAIR_EXCERPT_LIMIT = 1200

_XML_INVOKE_PATTERN = re.compile(r'<invoke\b[^>]*\bname\s*=\s*["\']([^"\']+)["\']', re.IGNORECASE)
_XML_PARAMETER_PATTERN = re.compile(r'<parameter\b[^>]*\bname\s*=\s*["\']([^"\']+)["\']', re.IGNORECASE)


def detect_xml_pseudo_tool_call(content: Any, *, allowed_tool_names: set[str]) -> dict[str, Any] | None:
    text = str(content or '').strip()
    if not text or '<' not in text or '>' not in text:
        return None
    invoke_names = [str(item or '').strip() for item in _XML_INVOKE_PATTERN.findall(text)]
    if not invoke_names:
        return None
    if not _XML_PARAMETER_PATTERN.search(text):
        return None
    normalized_allowed = {
        str(item or '').strip()
        for item in set(allowed_tool_names or set())
        if str(item or '').strip()
    }
    if not normalized_allowed:
        return None
    normalized_invokes: list[str] = []
    for name in invoke_names:
        if not name or name not in normalized_allowed:
            return None
        normalized_invokes.append(name)
    return {
        'tool_names': normalized_invokes,
        'excerpt': text[:XML_REPAIR_EXCERPT_LIMIT],
    }


def recover_tool_calls_from_json_payload(
    content: Any,
    *,
    allowed_tool_names: set[str],
    id_prefix: str = 'call:xml-repair',
) -> list[ToolCallRequest]:
    for candidate in extract_json_payload_candidates(str(content or '')):
        parsed = None
        try:
            parsed = json.loads(candidate)
        except Exception:
            try:
                parsed = json_repair.loads(candidate)
            except Exception:
                parsed = None
        if parsed is None:
            continue
        calls = tool_calls_from_json_payload(
            parsed,
            allowed_tool_names=allowed_tool_names,
            id_prefix=id_prefix,
        )
        if calls:
            return calls
    return []


def tool_calls_from_json_payload(
    payload: Any,
    *,
    allowed_tool_names: set[str],
    id_prefix: str = 'call:xml-repair',
) -> list[ToolCallRequest]:
    items = payload if isinstance(payload, list) else [payload]
    if not items or any(not isinstance(item, dict) for item in items):
        return []
    normalized_allowed = {
        str(item or '').strip()
        for item in set(allowed_tool_names or set())
        if str(item or '').strip()
    }
    if not normalized_allowed:
        return []
    signature = hashlib.sha256(
        json.dumps(items, ensure_ascii=False, sort_keys=True, default=str).encode('utf-8')
    ).hexdigest()[:12]
    calls: list[ToolCallRequest] = []
    for index, item in enumerate(items):
        tool_name = str((item or {}).get('name') or '').strip()
        arguments = (item or {}).get('arguments')
        if not tool_name or tool_name not in normalized_allowed or not isinstance(arguments, dict):
            return []
        calls.append(
            ToolCallRequest(
                id=f'{id_prefix}:{signature}:{index + 1}',
                name=tool_name,
                arguments=dict(arguments),
            )
        )
    return calls


def extract_json_payload_candidates(content: str) -> list[str]:
    text = str(content or '')
    stripped = text.strip()
    candidates: list[str] = []
    if stripped.startswith('{') or stripped.startswith('['):
        candidates.append(stripped)
    start_index: int | None = None
    stack: list[str] = []
    in_string = False
    escape = False
    for index, char in enumerate(text):
        if in_string:
            if escape:
                escape = False
            elif char == '\\':
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
            continue
        if char == '{':
            if not stack:
                start_index = index
            stack.append('}')
            continue
        if char == '[':
            if not stack:
                start_index = index
            stack.append(']')
            continue
        if char in {'}', ']'}:
            if not stack or char != stack[-1]:
                stack.clear()
                start_index = None
                continue
            stack.pop()
            if not stack and start_index is not None:
                candidates.append(text[start_index : index + 1].strip())
                start_index = None
    ordered: list[str] = []
    seen: set[str] = set()
    for candidate in reversed(candidates):
        normalized = str(candidate or '').strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        ordered.append(normalized)
    return ordered


def build_xml_tool_repair_message(
    *,
    xml_excerpt: str,
    tool_names: list[str],
    attempt_count: int,
    attempt_limit: int = XML_REPAIR_ATTEMPT_LIMIT,
    latest_issue: str = '',
) -> str:
    allowed = ', '.join(str(item or '').strip() for item in list(tool_names or []) if str(item or '').strip()) or '<unknown>'
    issue_text = str(latest_issue or '').strip()
    issue_prefix = f' Latest issue: {issue_text}.' if issue_text else ''
    excerpt = str(xml_excerpt or '').strip()[:XML_REPAIR_EXCERPT_LIMIT]
    return (
        f'Your previous reply looked like XML-style pseudo tool calling instead of valid tool calling.{issue_prefix} '
        f'Repair attempt {int(attempt_count or 0)} of {int(attempt_limit or 0)}. '
        'Re-emit the same tool invocation now in one valid format only: '
        'either a real structured tool call, or JSON repair payload in content only. '
        'Allowed JSON forms are: '
        '{"name":"tool_name","arguments":{...}} for a single call, or '
        '[{"name":"tool_a","arguments":{...}}, {"name":"tool_b","arguments":{...}}] for multiple calls. '
        f'Only use tool names visible this turn: {allowed}. '
        'Do not output XML, Markdown, code fences, or explanatory prose. '
        f'Previous invalid XML-like reply excerpt:\n{excerpt}'
    )


def format_xml_repair_failure_reason(*, count: int, tool_names: list[str], content_excerpt: str) -> str:
    visible_tools = ', '.join(str(item or '').strip() for item in list(tool_names or []) if str(item or '').strip()) or '<unknown>'
    excerpt = truncate_text(content_excerpt)
    excerpt_suffix = f' Latest content excerpt: {excerpt}' if excerpt else ''
    return (
        f'XML pseudo tool-call repair failed {int(count or 0)} consecutive times. '
        f'Latest candidate tool names: {visible_tools}.'
        f'{excerpt_suffix}'
    )


def truncate_text(value: Any, limit: int = 240) -> str:
    text = ' '.join(str(value or '').split()).strip()
    if len(text) <= max(1, int(limit)):
        return text
    return text[: max(1, int(limit)) - 3].rstrip() + '...'
