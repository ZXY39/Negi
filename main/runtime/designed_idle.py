"""设计内待命：所有非终态执行节点都停在带外部唤醒源的停靠相位。

判据与等待图同源：有人声明了 `frontdoor`（`node_wait_external`）才是合法的无限
等待；只等任务内节点的停靠仍按失速处理，否则会掩盖真死锁。读法走
`frame_mode='stall'` 的列（`node_phases`），不搬帧正文。
"""

from __future__ import annotations

from typing import Any

from main.runtime.node_mailbox import notification_is_held
from main.runtime.wait_graph import NODE_WAIT_EXTERNAL_KEY

_PARKED_PHASES = frozenset({'waiting_explicit', 'waiting_children'})


def task_is_designed_idle(
    *,
    store: Any,
    log_service: Any,
    task_id: str,
    runtime_state: dict[str, Any] | None = None,
    check_unread: bool = True,
) -> bool:
    state = runtime_state if isinstance(runtime_state, dict) else (
        log_service.read_runtime_state(task_id, frame_mode='stall') or {}
    )
    phases = {
        str(key or '').strip(): str(value or '').strip()
        for key, value in dict(state.get('node_phases') or {}).items()
    }
    try:
        records = list(store.list_nodes(task_id) or [])
    except Exception:
        return False
    execution_count = 0
    external = False
    for node in records:
        node_id = str(getattr(node, 'node_id', '') or '').strip()
        kind = str(getattr(node, 'node_kind', '') or '').strip().lower()
        status = str(getattr(node, 'status', '') or '').strip().lower()
        if not node_id or status in {'success', 'failed'}:
            continue
        phase = str(phases.get(node_id) or '').strip()
        if kind == 'acceptance':
            # 未派发的验收节点没有帧；在飞或卡住的验收相位不豁免。
            if phase and phase not in _PARKED_PHASES and phase != 'waiting_acceptance':
                return False
            continue
        if kind != 'execution':
            continue
        execution_count += 1
        if bool(getattr(node, 'is_paused', False)) or bool(getattr(node, 'pause_requested', False)):
            return False
        if phase not in _PARKED_PHASES:
            return False
        metadata = dict(getattr(node, 'metadata', None) or {})
        if bool(metadata.get(NODE_WAIT_EXTERNAL_KEY)):
            external = True
        if check_unread and node_has_unread_notifications(store=store, task_id=task_id, node_id=node_id):
            # 有可注入未读意味着派发车道马上要开回合；把"该醒没醒"藏进待命会漏报。
            return False
    return bool(execution_count) and external


def node_has_unread_notifications(*, store: Any, task_id: str, node_id: str) -> bool:
    try:
        rows = list(store.list_task_node_notifications(task_id, node_id) or [])
    except Exception:
        # 读不到按"有未读"处理：宁可告警，不把未知读成待命。
        return True
    for item in rows:
        status = str(getattr(item, 'status', '') or '').strip()
        if status == 'delivered' and not notification_is_held(item):
            return True
        if status == 'consumed' and not str(getattr(item, 'merged_at', '') or '').strip():
            return True
    return False
