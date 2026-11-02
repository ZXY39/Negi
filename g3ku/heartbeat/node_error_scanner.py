from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import Any


class NodeErrorScanner:
    """Poll persisted error-paused nodes and enqueue each error once."""

    def __init__(self, *, main_task_service: Any, heartbeat: Any, interval_seconds: float = 60.0) -> None:
        self._main_task_service = main_task_service
        self._heartbeat = heartbeat
        self._interval_seconds = max(1.0, float(interval_seconds or 60.0))
        self._task: asyncio.Task[Any] | None = None
        self._stopped = False

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stopped = False
        self._task = asyncio.create_task(self.run_forever(), name='heartbeat-node-error-scanner')

    async def stop(self) -> None:
        self._stopped = True
        task = self._task
        self._task = None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def run_forever(self) -> None:
        # Scan immediately so restart recovery does not wait for the first interval.
        await self.scan_once()
        while not self._stopped:
            await asyncio.sleep(self._interval_seconds)
            await self.scan_once()

    def _acceptance_self_fault_payload(self, *, task, node_id: str) -> dict[str, Any] | None:
        """验收节点自身 error pause 的载荷扩展；返回 None 表示这是清扫疤、不该直达。

        过滤的两类疤：被检验的执行节点已终态（T15 收口路径）；握手没有待裁定
        提交（既不在两个裁决等待态，也没有可解析的 submission ref）——那说明
        验收节点本来就不该在跑，这条例外不该惊动会话。
        """
        node_getter = getattr(self._main_task_service, 'get_node', None)
        if not callable(node_getter):
            return {}
        try:
            node = node_getter(node_id)
        except Exception:
            return {}
        if node is None or str(getattr(node, 'node_kind', '') or '').strip().lower() != 'acceptance':
            return {}
        metadata = dict(getattr(node, 'metadata', None) or {}) if isinstance(getattr(node, 'metadata', None), dict) else {}
        inspected_id = str(metadata.get('accepted_node_id') or '').strip()
        root_node_id = str(getattr(task, 'root_node_id', '') or '').strip()
        payload: dict[str, Any] = {
            'node_role': 'acceptance',
            'final_acceptance': bool(metadata.get('final_acceptance')) or bool(inspected_id and inspected_id == root_node_id),
        }
        if not inspected_id:
            return payload
        try:
            inspected = node_getter(inspected_id)
        except Exception:
            return None
        if inspected is None:
            return None
        inspected_status = str(getattr(inspected, 'status', '') or '').strip().lower()
        if inspected_status in {'success', 'failed'}:
            return None
        inspected_metadata = (
            dict(getattr(inspected, 'metadata', None) or {})
            if isinstance(getattr(inspected, 'metadata', None), dict)
            else {}
        )
        handshake = (
            dict(inspected_metadata.get('acceptance_handshake') or {})
            if isinstance(inspected_metadata.get('acceptance_handshake'), dict)
            else {}
        )
        handshake_state = str(handshake.get('state') or '').strip()
        submission_ref = str(handshake.get('latest_execution_result_ref') or '').strip()
        if handshake_state not in {'waiting_acceptance', 'waiting_block_verification'} and not submission_ref:
            return None
        payload['inspected_node_id'] = inspected_id
        payload['inspected_node_status'] = inspected_status
        if handshake_state:
            payload['handshake_state'] = handshake_state
        if submission_ref:
            payload['submission_ref'] = submission_ref
        return payload

    async def scan_once(self) -> int:
        store = getattr(self._main_task_service, 'store', None)
        list_pending = getattr(store, 'list_new_error_pauses', None)
        enqueue = getattr(self._heartbeat, 'enqueue_task_node_error_payload', None)
        origin_getter = getattr(self._main_task_service, '_task_origin_session_id', None)
        if not callable(list_pending) or not callable(enqueue):
            return 0
        groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for raw in list(list_pending() or []):
            item = raw.model_dump(mode='json') if hasattr(raw, 'model_dump') else dict(raw or {})
            task_id = str(item.get('task_id') or '').strip()
            node_id = str(item.get('node_id') or '').strip()
            if not task_id or not node_id:
                continue
            task = self._main_task_service.get_task(task_id) if hasattr(self._main_task_service, 'get_task') else None
            acceptance_payload = self._acceptance_self_fault_payload(task=task, node_id=node_id)
            if acceptance_payload is None:
                continue
            item.update(acceptance_payload)
            session_id = str(origin_getter(task) if callable(origin_getter) else getattr(task, 'session_id', 'web:shared') or 'web:shared').strip() or 'web:shared'
            pause_id = int(item.get('id') or 0)
            groups[session_id].append({
                **item,
                'task_id': task_id,
                'task_title': str(getattr(task, 'title', '') or task_id),
                'node_id': node_id,
                'node_title': str(item.get('node_title') or node_id),
                'pause_reason': str(item.get('pause_reason') or 'error'),
                'remark': str(item.get('remark') or ''),
                'error_text': str(item.get('error_text') or ''),
                'pause_row_id': pause_id,
                'dedupe_key': f'node-error:{task_id}:{node_id}:{pause_id}',
            })
        delivered = 0
        for session_id, items in groups.items():
            try:
                accepted = bool(enqueue(session_id, items))
            except Exception:
                accepted = False
            if not accepted:
                continue
            # 消费点不在 enqueue：heartbeat 在事件束成功注入上下文后才
            # mark_task_node_pause_delivered。这里保持 delivered=0，注入失败下一轮重扫；
            # 队列的 in-flight dedupe 防止同一 pause 在两次扫描间重复入队。
            delivered += len(items)
        return delivered
