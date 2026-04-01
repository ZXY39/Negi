from __future__ import annotations

import html
import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import json_repair

from g3ku.providers.base import ToolCallRequest

XML_REPAIR_ATTEMPT_LIMIT = 3
XML_REPAIR_EXCERPT_LIMIT = 1200

_XML_INVOKE_PATTERN = re.compile(r'<invoke\b[^>]*\bname\s*=\s*["\']([^"\']+)["\']', re.IGNORECASE)
_XML_PARAMETER_PATTERN = re.compile(r'<parameter\b[^>]*\bname\s*=\s*["\']([^"\']+)["\']', re.IGNORECASE)
_XML_INVOKE_BLOCK_PATTERN = re.compile(
    r'<invoke\b(?P<attrs>[^>]*)>(?P<body>.*?)</invoke\s*>',
    re.IGNORECASE | re.DOTALL,
)
_XML_PARAMETER_BLOCK_PATTERN = re.compile(
    r'<parameter\b(?P<attrs>[^>]*)>(?P<body>.*?)</parameter\s*>',
    re.IGNORECASE | re.DOTALL,
)
_XML_NAME_ATTR_PATTERN = re.compile(r'\bname\s*=\s*["\']([^"\']+)["\']', re.IGNORECASE)
_INTEGER_PATTERN = re.compile(r'^[+-]?\d+$')
_NUMBER_PATTERN = re.compile(r'^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$')


@dataclass(slots=True)
class XmlPseudoToolCallExtraction:
    matched: bool = False
    tool_calls: list[ToolCallRequest] = field(default_factory=list)
    tool_names: list[str] = field(default_factory=list)
    excerpt: str = ''
    issue: str = ''


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


def extract_tool_calls_from_xml_pseudo_content(
    content: Any,
    *,
    visible_tools: Mapping[str, Any],
    id_prefix: str = 'call:xml-direct',
) -> XmlPseudoToolCallExtraction:
    normalized_visible_tools = {
        str(name or '').strip(): tool
        for name, tool in dict(visible_tools or {}).items()
        if str(name or '').strip()
    }
    detected = detect_xml_pseudo_tool_call(
        content,
        allowed_tool_names=set(normalized_visible_tools.keys()),
    )
    if detected is None:
        return XmlPseudoToolCallExtraction()
    detected_tool_names = list(detected.get('tool_names') or [])
    excerpt = str(detected.get('excerpt') or '').strip()
    text = str(content or '').strip()
    invoke_blocks = list(_XML_INVOKE_BLOCK_PATTERN.finditer(text))
    if not invoke_blocks:
        return XmlPseudoToolCallExtraction(
            matched=True,
            tool_names=detected_tool_names,
            excerpt=excerpt,
            issue='could not parse XML invoke blocks',
        )
    payload_items: list[dict[str, Any]] = []
    parsed_tool_names: list[str] = []
    for invoke_match in invoke_blocks:
        tool_name = _extract_xml_name(invoke_match.group('attrs') or '')
        if not tool_name:
            return XmlPseudoToolCallExtraction(
                matched=True,
                tool_names=detected_tool_names,
                excerpt=excerpt,
                issue='invoke block is missing a valid name attribute',
            )
        tool = normalized_visible_tools.get(tool_name)
        if tool is None:
            return XmlPseudoToolCallExtraction(
                matched=True,
                tool_names=detected_tool_names,
                excerpt=excerpt,
                issue=f'tool "{tool_name}" is not visible this turn',
            )
        arguments, issue = _extract_invoke_arguments(
            invoke_match.group('body') or '',
            tool=tool,
        )
        if issue:
            return XmlPseudoToolCallExtraction(
                matched=True,
                tool_names=detected_tool_names,
                excerpt=excerpt,
                issue=f'{tool_name}: {issue}',
            )
        parsed_tool_names.append(tool_name)
        payload_items.append({'name': tool_name, 'arguments': arguments})
    if parsed_tool_names != detected_tool_names:
        return XmlPseudoToolCallExtraction(
            matched=True,
            tool_names=detected_tool_names,
            excerpt=excerpt,
            issue='could not parse every XML invoke block consistently',
        )
    tool_calls = tool_calls_from_json_payload(
        payload_items,
        allowed_tool_names=set(normalized_visible_tools.keys()),
        id_prefix=id_prefix,
    )
    if not tool_calls:
        return XmlPseudoToolCallExtraction(
            matched=True,
            tool_names=detected_tool_names,
            excerpt=excerpt,
            issue='failed to convert XML payload into synthetic tool calls',
        )
    return XmlPseudoToolCallExtraction(
        matched=True,
        tool_calls=tool_calls,
        tool_names=parsed_tool_names,
        excerpt=excerpt,
    )


def recover_tool_calls_from_json_payload(
    content: Any,
    *,
    allowed_tool_names: set[str],
    id_prefix: str = 'call:xml-repair',
) -> list[ToolCallRequest]:
    for candidate in extract_json_payload_candidates(str(content or '')):
        parsed, ok = _load_json_value(candidate)
        if not ok:
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


def _extract_xml_name(attrs_text: str) -> str:
    match = _XML_NAME_ATTR_PATTERN.search(str(attrs_text or ''))
    return str(match.group(1) or '').strip() if match else ''


def _extract_invoke_arguments(invoke_body: str, *, tool: Any) -> tuple[dict[str, Any], str]:
    parameter_matches = list(_XML_PARAMETER_BLOCK_PATTERN.finditer(str(invoke_body or '')))
    if not parameter_matches:
        return {}, 'invoke block did not contain any <parameter name="..."> entries'
    parameters_schema = dict(getattr(tool, 'parameters', {}) or {})
    properties = dict(parameters_schema.get('properties') or {}) if isinstance(parameters_schema, dict) else {}
    arguments: dict[str, Any] = {}
    for parameter_match in parameter_matches:
        parameter_name = _extract_xml_name(parameter_match.group('attrs') or '')
        if not parameter_name:
            return {}, 'parameter block is missing a valid name attribute'
        if parameter_name in arguments:
            return {}, f'duplicate parameter "{parameter_name}"'
        raw_value = html.unescape(str(parameter_match.group('body') or ''))
        coerced_value, issue = _coerce_xml_parameter_value(
            raw_value,
            schema=properties.get(parameter_name),
        )
        if issue:
            return {}, f'parameter "{parameter_name}" {issue}'
        arguments[parameter_name] = coerced_value
    validation_errors = list(tool.validate_params(arguments))
    if validation_errors:
        return {}, truncate_text(
            '; '.join(
                str(item or '').strip()
                for item in validation_errors
                if str(item or '').strip()
            )
        )
    return arguments, ''


def _coerce_xml_parameter_value(raw_value: str, *, schema: dict[str, Any] | None) -> tuple[Any, str]:
    if not isinstance(schema, dict):
        return raw_value, ''
    schema_type = str(schema.get('type') or '').strip().lower()
    if schema_type == 'string' or not schema_type:
        return raw_value, ''
    stripped = str(raw_value or '').strip()
    if schema_type == 'integer':
        if not stripped or not _INTEGER_PATTERN.fullmatch(stripped):
            return None, 'must be an integer'
        return int(stripped), ''
    if schema_type == 'number':
        if not stripped or not _NUMBER_PATTERN.fullmatch(stripped):
            return None, 'must be a number'
        return (int(stripped) if _INTEGER_PATTERN.fullmatch(stripped) else float(stripped)), ''
    if schema_type == 'boolean':
        lowered = stripped.lower()
        if lowered == 'true':
            return True, ''
        if lowered == 'false':
            return False, ''
        return None, 'must be true or false'
    if schema_type in {'array', 'object'}:
        parsed, ok = _load_json_value(stripped)
        if not ok:
            expected = 'JSON array' if schema_type == 'array' else 'JSON object'
            return None, f'must be a valid {expected}'
        if schema_type == 'array' and not isinstance(parsed, list):
            return None, 'must be a JSON array'
        if schema_type == 'object' and not isinstance(parsed, dict):
            return None, 'must be a JSON object'
        return parsed, ''
    return raw_value, ''


def _load_json_value(content: str) -> tuple[Any, bool]:
    text = str(content or '').strip()
    if not text:
        return None, False
    try:
        return json.loads(text), True
    except Exception:
        try:
            return json_repair.loads(text), True
        except Exception:
            return None, False


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
