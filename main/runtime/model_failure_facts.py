"""Provider-agnostic failure facts for a node error report.

The runtime does not interpret a provider 4xx: the same status covers size,
content policy, parameter faults and transient gateway states. This module only
records what was sent and what came back — the provider's raw error verbatim
plus the request facts a parent node or the front door needs — and lets the
reader decide. No cause line, no remedy line, no configuration change.
"""

from __future__ import annotations

from typing import Any, Mapping

from g3ku.providers.fallback import is_request_shape_error
from main.errors import describe_exception_chain

# Parks the fact card on the exception that is about to reach the node fault
# lane; the lane writes this text as the node's error row and uses the same
# text for the parent mailbox and the root heartbeat.
EXCEPTION_FACTS_ATTR = 'model_failure_facts'


def _http_status(error: BaseException | None) -> int:
    if error is None:
        return 0
    for attr in ('status_code', 'status', 'error_status'):
        raw = getattr(error, attr, None)
        if raw is None:
            continue
        try:
            value = int(raw)
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return 0


def build_model_failure_facts(
    *,
    error: BaseException | None,
    diagnostics: Mapping[str, Any] | None = None,
    model_ref: str = '',
    provider_model: str = '',
    route_group: str = '',
    route_index: int = -1,
    route_escalation_count: int = 0,
    route_escalation_reason: str = '',
    request_message_count: int = 0,
    actual_request_hash: str = '',
) -> str:
    payload = dict(diagnostics or {})
    lines = [
        'provider_error (verbatim):',
        describe_exception_chain(error),
        '',
        'request_facts:',
    ]
    status = _http_status(error)
    status_parts = []
    if status:
        status_parts.append(f'http_status={status}')
    status_parts.append(f'request_shape={"yes" if is_request_shape_error(error) else "no"}')
    lines.append(' '.join(status_parts))

    member_parts = [f'member={str(model_ref or "").strip() or "unknown"}']
    wire_model = str(provider_model or payload.get('provider_model') or '').strip()
    if wire_model:
        member_parts.append(f'wire_model={wire_model}')
    lines.append(' '.join(member_parts))

    route_parts = []
    if str(route_group or '').strip():
        route_parts.append(f'route_group={str(route_group).strip()}')
    if int(route_index) >= 0:
        route_parts.append(f'route_index={int(route_index)}')
    route_parts.append(f'escalation_count={int(route_escalation_count or 0)}')
    if str(route_escalation_reason or '').strip():
        route_parts.append(f'escalation_reason={str(route_escalation_reason).strip()}')
    lines.append(' '.join(route_parts))

    token_parts = [f'send_tokens={int(payload.get("final_request_tokens") or 0)}']
    if str(payload.get('estimate_source') or '').strip():
        token_parts.append(f'ruler={str(payload.get("estimate_source")).strip()}')
    declared_window = int(payload.get('context_window_tokens') or 0)
    if declared_window:
        token_parts.append(f'declared_window={declared_window}')
    trigger_line = int(payload.get('effective_trigger_tokens') or 0)
    if trigger_line:
        token_parts.append(f'trigger_line={trigger_line}')
    lines.append(' '.join(token_parts))

    request_parts = [f'request_messages={int(request_message_count or payload.get("actual_request_message_count") or 0)}']
    if str(actual_request_hash or '').strip():
        request_parts.append(f'actual_request_hash={str(actual_request_hash).strip()}')
    if bool(payload.get('compaction_applied')):
        request_parts.append(f'compaction={str(payload.get("compaction_mode") or "applied").strip()}')
    lines.append(' '.join(request_parts))

    lines.append('runtime_action: none (no configuration changed)')
    return '\n'.join(lines)


def attach(exc: BaseException | None, text: str) -> None:
    if exc is None or not str(text or '').strip():
        return
    try:
        setattr(exc, EXCEPTION_FACTS_ATTR, str(text).strip())
    except Exception:
        pass


def facts_for(exc: BaseException | None) -> str:
    if exc is None:
        return ''
    return str(getattr(exc, EXCEPTION_FACTS_ATTR, '') or '').strip()
