from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from main.models import normalize_failure_class, normalize_final_acceptance_metadata

TASK_TERMINAL_CALLBACK_PATH = '/api/internal/task-terminal'
TASK_TERMINAL_CALLBACK_URL_ENV = 'G3KU_INTERNAL_CALLBACK_URL'
TASK_TERMINAL_CALLBACK_TOKEN_ENV = 'G3KU_INTERNAL_CALLBACK_TOKEN'
TASK_TERMINAL_CALLBACK_FILE = Path('.g3ku') / 'internal-callback.json'

# Outbound http:// callback URLs are restricted to loopback/compose hosts plus
# any host appended via this env var (comma separated, case-insensitive).
TASK_TERMINAL_CALLBACK_HTTP_ALLOWED_HOSTS_ENV = 'G3KU_CALLBACK_HTTP_ALLOWED_HOSTS'
_CALLBACK_HTTP_LOOPBACK_HOSTS = frozenset({'localhost', '127.0.0.1', '::1', 'web'})


def is_allowed_callback_url(url: str) -> tuple[bool, str]:
    """Validate an outbound task/event callback URL (allow + reason).

    https:// to any host is allowed; http:// is allowed only for loopback and
    compose hosts ({localhost, 127.0.0.1, [::1], web}) plus hosts from env
    G3KU_CALLBACK_HTTP_ALLOWED_HOSTS (comma separated, appended). Any other
    scheme is rejected. Never logs or returns the token.
    """
    text = str(url or '').strip()
    if not text:
        return False, 'empty url'
    try:
        parsed = urlparse(text)
        scheme = str(parsed.scheme or '').strip().lower()
        host = parsed.hostname
    except ValueError:
        return False, 'invalid url'
    if scheme == 'https':
        return True, 'allowed: https any host'
    if scheme != 'http':
        return False, f'unsupported scheme: {scheme or "(none)"}'
    if not host:
        return False, 'missing host'
    allowed_hosts = set(_CALLBACK_HTTP_LOOPBACK_HOSTS)
    for extra in str(os.getenv(TASK_TERMINAL_CALLBACK_HTTP_ALLOWED_HOSTS_ENV, '') or '').split(','):
        normalized = str(extra or '').strip().lower().strip('[]').strip()
        if normalized:
            allowed_hosts.add(normalized)
    if str(host).lower() not in allowed_hosts:
        return False, f'http host not allowed: {host}'
    return True, 'allowed: http loopback or compose host'

# Externalized terminal outputs are re-inlined into the terminal event when
# they fit the same budget a content_open result would inline, so the heartbeat
# turn can deliver the full result to the user without a follow-up tool call.
TASK_TERMINAL_OUTPUT_INLINE_CHAR_LIMIT = 16000
TASK_TERMINAL_OUTPUT_INLINE_LINE_LIMIT = 260
_TERMINAL_OUTPUT_TEXT_MIME_PREFIXES = ('text/',)
_TERMINAL_OUTPUT_TEXT_MIMES = {
    'application/json',
    'application/x-ndjson',
    'application/markdown',
}


def callback_config_path(*, workspace: Path | str | None = None) -> Path:
    root = Path(workspace) if workspace is not None else Path.cwd()
    return root / TASK_TERMINAL_CALLBACK_FILE


def load_task_terminal_callback_config(*, workspace: Path | str | None = None) -> dict[str, Any]:
    path = callback_config_path(workspace=workspace)
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def save_task_terminal_callback_config(
    *,
    workspace: Path | str | None = None,
    url: str,
    token: str,
) -> dict[str, str]:
    path = callback_config_path(workspace=workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        'url': str(url or '').strip(),
        'token': str(token or '').strip(),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    return payload


def resolve_task_terminal_callback_url(*, workspace: Path | str | None = None) -> str:
    env_value = str(os.getenv(TASK_TERMINAL_CALLBACK_URL_ENV, '') or '').strip()
    if env_value:
        return env_value
    return str(load_task_terminal_callback_config(workspace=workspace).get('url') or '').strip()


def resolve_task_terminal_callback_token(*, workspace: Path | str | None = None) -> str:
    env_value = str(os.getenv(TASK_TERMINAL_CALLBACK_TOKEN_ENV, '') or '').strip()
    if env_value:
        return env_value
    return str(load_task_terminal_callback_config(workspace=workspace).get('token') or '').strip()


def build_task_terminal_dedupe_key(*, task_id: str, status: str, finished_at: str) -> str:
    return f'task-terminal:{str(task_id or '').strip()}:{str(status or '').strip().lower()}:{str(finished_at or '').strip()}'


def _normalize_task_terminal_text(value: Any) -> str:
    return str(value or '').strip()


# 用户定向补充的一律从分发 epoch 重推：epoch 的 root_message / queued_root_messages
# 是用户追加通知的权威记录，target_node_ids 点名了补充目标；失败/取消的分发跳过。
_SUPPLEMENT_TERMINAL_EPOCH_STATES = frozenset({'failed', 'cancelled', 'cancelled_by_task_delete'})


def collect_task_user_node_supplements(task_id: str, *, store: Any) -> list[dict[str, str]]:
    """收集任务期间用户对各节点追加的定向通知（终态事件附带给会话模型）。

    每行 = 一个 (epoch, 目标节点, 消息) 三元组；消息与目标取自分发 epoch 的
    权威字段，节点标题用于会话模型对照（截断 60 字，兜底 node id）。
    """
    normalized_task_id = str(task_id or '').strip()
    if not normalized_task_id or store is None:
        return []
    list_epochs = getattr(store, 'list_active_task_message_distribution_epochs', None)
    get_node = getattr(store, 'get_node', None)
    if not callable(list_epochs) or not callable(get_node):
        return []
    try:
        epochs = list_epochs(normalized_task_id)
    except Exception:
        return []
    items: list[dict[str, str]] = []
    for epoch in list(epochs or []):
        state = str(getattr(epoch, 'state', '') or '').strip().lower()
        if state in _SUPPLEMENT_TERMINAL_EPOCH_STATES:
            continue
        payload = getattr(epoch, 'payload', None)
        payload = payload if isinstance(payload, dict) else {}
        messages = [
            str(item or '').strip()
            for item in list(payload.get('queued_root_messages') or [])
            if str(item or '').strip()
        ]
        if not messages:
            message = str(getattr(epoch, 'root_message', '') or '').strip()
            messages = [message] if message else []
        if not messages:
            continue
        targets = [
            str(item or '').strip()
            for item in list(payload.get('target_node_ids') or [])
            if str(item or '').strip()
        ]
        epoch_id = str(getattr(epoch, 'epoch_id', '') or '').strip()
        created_at = str(getattr(epoch, 'created_at', '') or '').strip()
        for node_id in targets:
            try:
                node = get_node(node_id)
            except Exception:
                node = None
            title = ' '.join(str(getattr(node, 'goal', '') or '').split())[:60].strip() if node is not None else ''
            for raw_message in messages:
                items.append(
                    {
                        'node_id': node_id,
                        'node_title': title or node_id,
                        'message': raw_message,
                        'epoch_id': epoch_id,
                        'created_at': created_at,
                        'epoch_state': state or 'unknown',
                    }
                )
    return items


def normalize_user_node_supplements(value: Any) -> list[dict[str, str]]:
    """清洗 user_node_supplements：仅保留有 node_id 与 message 的条目。"""
    items: list[dict[str, str]] = []
    for raw in list(value or []):
        if not isinstance(raw, dict):
            continue
        node_id = str(raw.get('node_id') or raw.get('nodeId') or '').strip()
        message = str(raw.get('message') or '').strip()
        if not node_id or not message:
            continue
        items.append(
            {
                'node_id': node_id,
                'node_title': str(raw.get('node_title') or raw.get('nodeTitle') or '').strip() or node_id,
                'message': message,
                'epoch_id': str(raw.get('epoch_id') or raw.get('epochId') or '').strip(),
                'created_at': str(raw.get('created_at') or raw.get('createdAt') or '').strip(),
                'epoch_state': str(raw.get('epoch_state') or raw.get('epochState') or '').strip(),
            }
        )
    return items


def build_terminal_output_resolver(content_store: Any | None) -> Callable[[str], str] | None:
    """Build a resolver that re-inlines small externalized outputs.

    Returns ``""`` when the referenced content is missing, non-textual, or too
    large to inline; callers then keep the externalized summary + ref.
    """
    if content_store is None or not hasattr(content_store, 'read'):
        return None

    def resolve(ref: Any) -> str:
        normalized_ref = _normalize_task_terminal_text(ref)
        if not normalized_ref.startswith('artifact:'):
            return ''
        try:
            payload = content_store.read(ref=normalized_ref, view='canonical')
        except Exception:
            return ''
        if not isinstance(payload, dict) or not payload.get('ok'):
            return ''
        handle = payload.get('handle') if isinstance(payload.get('handle'), dict) else {}
        mime_type = str(handle.get('mime_type') or '').strip().lower()
        if mime_type and not (
            mime_type.startswith(_TERMINAL_OUTPUT_TEXT_MIME_PREFIXES)
            or mime_type in _TERMINAL_OUTPUT_TEXT_MIMES
        ):
            return ''
        text = str(payload.get('content') or '')
        if not text:
            return ''
        if len(text) > TASK_TERMINAL_OUTPUT_INLINE_CHAR_LIMIT:
            return ''
        if len(text.splitlines()) > TASK_TERMINAL_OUTPUT_INLINE_LINE_LIMIT:
            return ''
        return text

    return resolve


def _maybe_inline_externalized_output(output_text: str, output_ref: str, resolver: Callable[[str], str] | None) -> str:
    if not output_ref or not callable(resolver):
        return output_text
    try:
        inline_text = resolver(output_ref)
    except Exception:
        inline_text = ''
    return inline_text or output_text


def _task_terminal_delivery_payload(
    task: Any,
    *,
    node_detail_getter: Callable[[str, str], dict[str, Any] | None] | None = None,
    output_resolver: Callable[[str], str] | None = None,
    supplement_getter: Callable[[str], list[dict[str, Any]]] | None = None,
    fallback_supplements: Any = None,
) -> dict[str, Any]:
    task_id = _normalize_task_terminal_text(getattr(task, 'task_id', ''))
    # 用户定向补充：getter 可用且能重推到非空结果时以重推为准（同一任务、同一
    # 数据源），否则保留上游传入值——worker 模式预计算、web 侧 getter 读不到
    # 任务存储时兜底，保证 --no-worker 容器拓扑下该泳道不丢。
    collected_supplements: list[dict[str, Any]] = []
    if callable(supplement_getter):
        try:
            collected_supplements = normalize_user_node_supplements(supplement_getter(_normalize_task_terminal_text(getattr(task, 'task_id', ''))))
        except Exception:
            collected_supplements = []
    user_node_supplements = collected_supplements if collected_supplements else normalize_user_node_supplements(fallback_supplements)
    root_node_id = _normalize_task_terminal_text(getattr(task, 'root_node_id', ''))
    metadata = getattr(task, 'metadata', None) if isinstance(getattr(task, 'metadata', None), dict) else {}
    final_acceptance = normalize_final_acceptance_metadata((metadata or {}).get('final_acceptance'))
    acceptance_node_id = _normalize_task_terminal_text(getattr(final_acceptance, 'node_id', ''))
    acceptance_failed = bool(
        getattr(final_acceptance, 'required', False)
        and _normalize_task_terminal_text(getattr(final_acceptance, 'status', '')).lower() == 'failed'
        and acceptance_node_id
    )
    terminal_node_id = acceptance_node_id if acceptance_failed else root_node_id
    terminal_node_kind = 'acceptance' if acceptance_failed else 'execution'
    terminal_node_reason = 'acceptance_failed' if acceptance_failed else 'root_terminal'
    terminal_output = ''
    terminal_output_ref = ''
    terminal_check_result = ''
    terminal_failure_reason = ''
    root_output = ''
    root_output_ref = ''

    detail_item = None
    if task_id and terminal_node_id and callable(node_detail_getter):
        try:
            detail_payload = node_detail_getter(task_id, terminal_node_id)
        except Exception:
            detail_payload = None
        if isinstance(detail_payload, dict) and isinstance(detail_payload.get('item'), dict):
            detail_item = dict(detail_payload.get('item') or {})

    root_detail_item = None
    if task_id and root_node_id and callable(node_detail_getter):
        try:
            root_detail_payload = node_detail_getter(task_id, root_node_id)
        except Exception:
            root_detail_payload = None
        if isinstance(root_detail_payload, dict) and isinstance(root_detail_payload.get('item'), dict):
            root_detail_item = dict(root_detail_payload.get('item') or {})

    if isinstance(detail_item, dict):
        terminal_output = _normalize_task_terminal_text(
            detail_item.get('final_output')
            or detail_item.get('output')
            or detail_item.get('check_result')
            or detail_item.get('failure_reason')
        )
        terminal_output_ref = _normalize_task_terminal_text(
            detail_item.get('final_output_ref')
            or detail_item.get('output_ref')
            or detail_item.get('check_result_ref')
        )
        terminal_check_result = _normalize_task_terminal_text(detail_item.get('check_result'))
        terminal_failure_reason = _normalize_task_terminal_text(detail_item.get('failure_reason'))

    if isinstance(root_detail_item, dict):
        root_output = _normalize_task_terminal_text(
            root_detail_item.get('final_output')
            or root_detail_item.get('output')
            or root_detail_item.get('check_result')
            or root_detail_item.get('failure_reason')
        )
        root_output_ref = _normalize_task_terminal_text(
            root_detail_item.get('final_output_ref')
            or root_detail_item.get('output_ref')
            or root_detail_item.get('check_result_ref')
        )

    if not terminal_output:
        if acceptance_failed:
            terminal_output = _normalize_task_terminal_text(getattr(task, 'failure_reason', ''))
        else:
            terminal_output = _normalize_task_terminal_text(getattr(task, 'final_output', ''))
    if not terminal_output_ref and not acceptance_failed:
        terminal_output_ref = _normalize_task_terminal_text(getattr(task, 'final_output_ref', ''))
    if not terminal_failure_reason:
        terminal_failure_reason = _normalize_task_terminal_text(getattr(task, 'failure_reason', ''))
    if not root_output:
        root_output = _normalize_task_terminal_text(
            (metadata or {}).get('final_execution_output')
            or getattr(task, 'final_output', '')
        )
    if not root_output_ref:
        root_output_ref = _normalize_task_terminal_text(getattr(task, 'final_output_ref', ''))

    terminal_output = _maybe_inline_externalized_output(terminal_output, terminal_output_ref, output_resolver)
    root_output = _maybe_inline_externalized_output(root_output, root_output_ref, output_resolver)

    return {
        'root_node_id': root_node_id,
        'acceptance_node_id': acceptance_node_id,
        'terminal_node_id': terminal_node_id,
        'terminal_node_kind': terminal_node_kind,
        'terminal_node_reason': terminal_node_reason,
        'terminal_output': terminal_output,
        'terminal_output_ref': terminal_output_ref,
        'terminal_check_result': terminal_check_result,
        'terminal_failure_reason': terminal_failure_reason,
        'root_output': root_output,
        'root_output_ref': root_output_ref,
        # 任务期间用户对各节点追加的定向通知（会话模型对齐最终需求用）。
        'user_node_supplements': user_node_supplements,
    }


def build_task_terminal_payload(task: Any, *, supplement_getter: Callable[[str], list[dict[str, Any]]] | None = None) -> dict[str, Any]:
    task_id = str(getattr(task, 'task_id', '') or '').strip()
    session_id = str(getattr(task, 'session_id', '') or '').strip()
    status = str(getattr(task, 'status', '') or '').strip().lower()
    finished_at = str(getattr(task, 'finished_at', '') or '').strip()
    payload = {
        'dedupe_key': build_task_terminal_dedupe_key(task_id=task_id, status=status, finished_at=finished_at),
        'task_id': task_id,
        'session_id': session_id,
        'title': str(getattr(task, 'title', '') or task_id).strip() or task_id,
        'status': status,
        'failure_class': normalize_failure_class((getattr(task, 'metadata', None) or {}).get('failure_class')),
        'final_acceptance_status': str(normalize_final_acceptance_metadata((getattr(task, 'metadata', None) or {}).get('final_acceptance')).status or '').strip().lower(),
        'brief_text': str(getattr(task, 'brief_text', '') or '').strip(),
        'failure_reason': str(getattr(task, 'failure_reason', '') or '').strip(),
        'finished_at': finished_at,
    }
    payload.update(
        _task_terminal_delivery_payload(
            task,
            supplement_getter=supplement_getter,
        )
    )
    return payload


def enrich_task_terminal_payload(
    payload: dict[str, Any] | None,
    *,
    task: Any | None = None,
    task_getter: Callable[[str], Any | None] | None = None,
    node_detail_getter: Callable[[str, str], dict[str, Any] | None] | None = None,
    output_resolver: Callable[[str], str] | None = None,
    supplement_getter: Callable[[str], list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    normalized = normalize_task_terminal_payload(payload)
    if not normalized:
        return {}
    task_record = task
    if task_record is None and callable(task_getter):
        try:
            task_record = task_getter(str(normalized.get('task_id') or '').strip())
        except Exception:
            task_record = None
    if task_record is None:
        return normalized
    normalized.update(
        _task_terminal_delivery_payload(
            task_record,
            node_detail_getter=node_detail_getter,
            output_resolver=output_resolver,
            supplement_getter=supplement_getter,
            fallback_supplements=normalized.get('user_node_supplements'),
        )
    )
    return normalize_task_terminal_payload(normalized)


def normalize_task_terminal_payload(payload: dict[str, Any] | None) -> dict[str, Any]:
    source = payload if isinstance(payload, dict) else {}
    task_id = str(source.get('task_id') or source.get('taskId') or '').strip()
    if task_id and not task_id.startswith('task:') and ':' not in task_id:
        task_id = f'task:{task_id}'
    session_id = str(source.get('session_id') or source.get('sessionId') or '').strip() or 'web:shared'
    status = str(source.get('status') or '').strip().lower()
    finished_at = str(source.get('finished_at') or source.get('finishedAt') or '').strip()
    if not task_id or status not in {'success', 'failed'}:
        return {}
    # The dedupe key is always recomputed server-side as the canonical key.
    # Callers that supply their own dedupe_key/dedupeKey are ignored: probe or
    # retry variants of the key used to bypass exact-key dedupe and re-deliver
    # the same task result to the user multiple times.
    dedupe_key = build_task_terminal_dedupe_key(task_id=task_id, status=status, finished_at=finished_at)
    return {
        'dedupe_key': dedupe_key,
        'task_id': task_id,
        'session_id': session_id,
        'title': str(source.get('title') or task_id).strip() or task_id,
        'status': status,
        'failure_class': normalize_failure_class(source.get('failure_class') or source.get('failureClass')),
        'final_acceptance_status': _normalize_task_terminal_text(source.get('final_acceptance_status') or source.get('finalAcceptanceStatus')).lower(),
        'brief_text': str(source.get('brief_text') or source.get('briefText') or '').strip(),
        'failure_reason': str(source.get('failure_reason') or source.get('failureReason') or '').strip(),
        'finished_at': finished_at,
        'root_node_id': _normalize_task_terminal_text(source.get('root_node_id') or source.get('rootNodeId')),
        'acceptance_node_id': _normalize_task_terminal_text(source.get('acceptance_node_id') or source.get('acceptanceNodeId')),
        'terminal_node_id': _normalize_task_terminal_text(source.get('terminal_node_id') or source.get('terminalNodeId')),
        'terminal_node_kind': _normalize_task_terminal_text(source.get('terminal_node_kind') or source.get('terminalNodeKind')),
        'terminal_node_reason': _normalize_task_terminal_text(source.get('terminal_node_reason') or source.get('terminalNodeReason')),
        'terminal_output': _normalize_task_terminal_text(source.get('terminal_output') or source.get('terminalOutput')),
        'terminal_output_ref': _normalize_task_terminal_text(source.get('terminal_output_ref') or source.get('terminalOutputRef')),
        'terminal_check_result': _normalize_task_terminal_text(source.get('terminal_check_result') or source.get('terminalCheckResult')),
        'terminal_failure_reason': _normalize_task_terminal_text(source.get('terminal_failure_reason') or source.get('terminalFailureReason')),
        'root_output': _normalize_task_terminal_text(source.get('root_output') or source.get('rootOutput')),
        'root_output_ref': _normalize_task_terminal_text(source.get('root_output_ref') or source.get('rootOutputRef')),
        'user_node_supplements': normalize_user_node_supplements(
            source.get('user_node_supplements') or source.get('userNodeSupplements')
        ),
    }
