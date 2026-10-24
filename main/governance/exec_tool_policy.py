from __future__ import annotations

from typing import Any

EXEC_TOOL_ID = 'exec'
EXECUTION_MODE_METADATA_KEY = 'execution_mode'
EXECUTION_MODE_GOVERNED = 'governed'
EXECUTION_MODE_FULL_ACCESS = 'full_access'
EXECUTION_MODE_VALUES = frozenset({EXECUTION_MODE_GOVERNED, EXECUTION_MODE_FULL_ACCESS})


def normalize_exec_execution_mode(value: Any) -> str:
    normalized = str(value or '').strip().lower()
    if normalized in EXECUTION_MODE_VALUES:
        return normalized
    return EXECUTION_MODE_GOVERNED


def exec_tool_supports_execution_mode(tool_id: Any) -> bool:
    return str(tool_id or '').strip() == EXEC_TOOL_ID


def exec_tool_mode_from_settings_payload(settings_payload: dict[str, Any] | None) -> str:
    payload = dict(settings_payload or {})
    return normalize_exec_execution_mode(payload.get(EXECUTION_MODE_METADATA_KEY))


def exec_tool_mode_from_metadata(metadata: dict[str, Any] | None) -> str:
    payload = dict(metadata or {})
    return normalize_exec_execution_mode(payload.get(EXECUTION_MODE_METADATA_KEY))


def resolve_exec_execution_mode(
    *,
    record: Any | None = None,
    descriptor: Any | None = None,
    settings_payload: dict[str, Any] | None = None,
) -> str:
    record_tool_id = str(getattr(record, 'tool_id', '') or '').strip()
    descriptor_name = str(getattr(descriptor, 'name', '') or '').strip()
    if record is not None and exec_tool_supports_execution_mode(record_tool_id):
        metadata = getattr(record, 'metadata', None)
        if isinstance(metadata, dict) and EXECUTION_MODE_METADATA_KEY in metadata:
            return exec_tool_mode_from_metadata(metadata)
    if descriptor is not None and descriptor_name == EXEC_TOOL_ID:
        metadata = getattr(descriptor, 'metadata', None)
        if isinstance(metadata, dict):
            settings = metadata.get('settings')
            if isinstance(settings, dict) and EXECUTION_MODE_METADATA_KEY in settings:
                return exec_tool_mode_from_settings_payload(settings)
    if isinstance(settings_payload, dict) and EXECUTION_MODE_METADATA_KEY in settings_payload:
        return exec_tool_mode_from_settings_payload(settings_payload)
    return EXECUTION_MODE_GOVERNED


def merge_exec_execution_mode_metadata(
    metadata: dict[str, Any] | None,
    *,
    execution_mode: Any | None,
) -> dict[str, Any]:
    next_metadata = dict(metadata or {})
    if execution_mode is not None:
        next_metadata[EXECUTION_MODE_METADATA_KEY] = normalize_exec_execution_mode(execution_mode)
    return next_metadata


def exec_runtime_policy_payload(
    *,
    mode: Any,
) -> dict[str, Any]:
    normalized_mode = normalize_exec_execution_mode(mode)
    if normalized_mode == EXECUTION_MODE_FULL_ACCESS:
        return {
            'mode': normalized_mode,
            'guardrails_enabled': False,
            'summary': 'exec will execute shell commands without exec-side guardrails.',
        }
    return {
        'mode': EXECUTION_MODE_GOVERNED,
        'guardrails_enabled': True,
        'summary': 'exec will enforce exec-side guardrails before running shell commands.',
    }


def render_exec_runtime_policy_body(payload: dict[str, Any] | None) -> str:
    """单行正文（不含 `exec_runtime_policy:` 前缀）：头部合同与一次性通知共用。

    空载荷返回空串——调用方据此把"exec 不在本轮暴露面"当作没有这一节，而不是
    渲染成 `none` 去污染每次通知。
    """
    data = dict(payload or {})
    if not data:
        return ''
    parts: list[str] = []
    mode = str(data.get('mode') or '').strip()
    if mode:
        parts.append(f'mode={mode}')
    if 'guardrails_enabled' in data:
        parts.append(f'guardrails_enabled={bool(data.get("guardrails_enabled"))}')
    summary = str(data.get('summary') or '').strip()
    if summary:
        parts.append(f'summary={summary}')
    return '; '.join(parts)


def resolve_exec_runtime_policy_payload(
    *,
    record: Any | None = None,
    descriptor: Any | None = None,
    settings_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return exec_runtime_policy_payload(
        mode=resolve_exec_execution_mode(
            record=record,
            descriptor=descriptor,
            settings_payload=settings_payload,
        )
    )
