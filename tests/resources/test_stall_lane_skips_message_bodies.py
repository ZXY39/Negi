from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from main.models import NodeRecord, TaskRecord, TokenUsageSummary
from main.monitoring.file_store import TaskFileStore
from main.monitoring.log_service import TaskLogService
from main.service.task_stall_notifier import (
    TaskStallNotifier,
    effective_silence_start,
    running_tool_deadline,
)
from main.storage.sqlite_store import SQLiteTaskStore

TASK_ID = 'task:stalllane'
NODE_ID = 'node:a'
STARTED_AT = (datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat()


class _ContentStore:
    def __init__(self, messages: list[dict]) -> None:
        self._messages = messages
        self.hits = 0

    def _resolve(self, *, ref: str, path: object) -> tuple[str, None]:
        self.hits += 1
        return json.dumps({'messages': self._messages}, ensure_ascii=False), None


def _frames() -> list[dict]:
    """三帧：0 有带截止时间的 running 调用（判定档唯一要读的那帧）、
    1 running 但豁免时限（无 timeout_seconds，不参与静默锚点）、2 已完成。"""
    return [
        {
            'node_id': 'node:0',
            'depth': 0,
            'node_kind': 'execution',
            'phase': 'running',
            'runnable': True,
            'waiting': False,
            'stage_goal': 'goal-0',
            'messages_ref': 'content:messages/node-0',
            'messages_count': 2,
            'tool_calls': [{
                'tool_call_id': 'call-0',
                'name': 'filesystem_read',
                'status': 'running',
                'started_at': STARTED_AT,
                'timeout_seconds': 300,
            }],
        },
        {
            'node_id': 'node:1',
            'depth': 0,
            'node_kind': 'execution',
            'phase': 'running',
            'runnable': True,
            'waiting': False,
            'stage_goal': 'goal-1',
            'messages_ref': 'content:messages/node-1',
            'messages_count': 2,
            'tool_calls': [{
                'tool_call_id': 'call-1',
                'name': 'exec',
                'status': 'running',
                'started_at': STARTED_AT,
            }],
        },
        {
            'node_id': 'node:2',
            'depth': 0,
            'node_kind': 'execution',
            'phase': 'idle',
            'runnable': False,
            'waiting': False,
            'stage_goal': 'goal-2',
            'messages_ref': 'content:messages/node-2',
            'messages_count': 2,
            'tool_calls': [{
                'tool_call_id': 'call-2',
                'name': 'filesystem_read',
                'status': 'success',
                'started_at': STARTED_AT,
                'timeout_seconds': 300,
            }],
        },
    ]


def _service(tmp_path) -> tuple[TaskLogService, _ContentStore]:
    store = SQLiteTaskStore(tmp_path / 'runtime.sqlite3')
    store.upsert_task(TaskRecord(
        task_id=TASK_ID,
        session_id='web:shared',
        title='demo',
        user_request='demo',
        status='in_progress',
        root_node_id=NODE_ID,
        max_depth=1,
        created_at='2026-10-01T10:00:00+08:00',
        updated_at='2026-10-01T10:00:00+08:00',
        token_usage=TokenUsageSummary(tracked=True),
        metadata={},
    ))
    for index in range(3):
        store.upsert_node(NodeRecord(
            node_id=f'node:{index}',
            task_id=TASK_ID,
            parent_node_id=None,
            root_node_id='node:0',
            depth=0,
            node_kind='execution',
            status='in_progress',
            goal='demo',
            prompt='demo',
            input='demo',
            output=[],
            check_result='',
            final_output='',
            can_spawn_children=False,
            created_at='2026-10-01T10:00:00+08:00',
            updated_at='2026-10-01T10:00:00+08:00',
            token_usage=TokenUsageSummary(tracked=True),
        ))
    content_store = _ContentStore([
        {'role': 'assistant', 'content': 'first'},
        {'role': 'tool', 'content': 'second'},
    ])
    service = TaskLogService(
        store=store,
        file_store=TaskFileStore(tmp_path / 'files'),
        registry=None,
        event_history_enabled=False,
        content_store=content_store,
    )
    for frame in _frames():
        service.upsert_frame(TASK_ID, frame)
    return service, content_store


def test_stall_mode_carries_only_deadline_frames_and_never_touches_message_bodies(tmp_path) -> None:
    service, content_store = _service(tmp_path)

    state = service.read_runtime_state(TASK_ID, frame_mode='stall') or {}

    assert content_store.hits == 0
    assert [frame['node_id'] for frame in state['frames']] == ['node:0']
    assert state['frames'][0]['tool_calls'][0]['status'] == 'running'
    # 三张节点表来自列：active 是"这个节点有帧行"，与正文无关
    assert state['active_node_ids'] == ['node:0', 'node:1', 'node:2']
    assert 'node:0' in state['runnable_node_ids']
    assert 'node:2' not in state['runnable_node_ids']


def test_stall_mode_never_reads_frame_payloads_row_by_row(tmp_path) -> None:
    """成本断言：判定档一次帧正文都不该搬回 Python。"""
    service, _content_store = _service(tmp_path)
    seen: list[str] = []
    store = service._store
    original_list = store.list_task_runtime_frames

    def spying(task_id: str):
        seen.append('list_task_runtime_frames')
        return original_list(task_id)

    store.list_task_runtime_frames = spying  # type: ignore[method-assign]

    service.read_runtime_state(TASK_ID, frame_mode='stall')

    assert seen == []


def test_default_state_still_resolves_each_frame(tmp_path) -> None:
    """对照：台账 3 帧 ⇒ 默认口径打 3 次内容存储——这条计数不是桩件假象。"""
    service, content_store = _service(tmp_path)

    state = service.read_runtime_state(TASK_ID) or {}

    assert content_store.hits == 3
    assert len(state['frames'][0]['messages']) == 2
    assert len(state['frames']) == 3


def test_running_tool_deadline_is_identical_on_both_reads(tmp_path) -> None:
    """失速判据依赖的唯一帧内字段：两种读法必须给出同一个截止时间。"""
    service, _content_store = _service(tmp_path)

    stall = service.read_runtime_state(TASK_ID, frame_mode='stall') or {}
    full = service.read_runtime_state(TASK_ID) or {}

    stall_deadline = running_tool_deadline(stall)
    assert stall_deadline is not None
    assert stall_deadline == running_tool_deadline(full)
    assert effective_silence_start(stall, STARTED_AT) == effective_silence_start(full, STARTED_AT)


class _NotifierService:
    """`TaskStallNotifier._schedule` 用到的那几个入口，按实盘形状最小搭一份。"""

    def __init__(self, log_service: TaskLogService, task: TaskRecord) -> None:
        self.log_service = log_service
        self.calls: list[bool] = []
        self._task = task

    def get_task(self, task_id: str) -> TaskRecord | None:
        return self._task if task_id == self._task.task_id else None

    def _task_origin_session_id(self, task) -> str:
        return 'web:shared'

    def _stall_now_iso(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    def is_task_stall_actionable(self, task_id: str, *, runtime_state=None) -> bool:
        return True

    def build_task_stall_payload(self, task_id: str, **kwargs) -> dict:
        return {}


def test_the_stall_lane_asks_for_the_stall_mode(tmp_path) -> None:
    service, content_store = _service(tmp_path)
    original = service.read_runtime_state
    seen: list[str] = []

    def spying(task_id: str, *, frame_mode: str = 'full'):
        seen.append(frame_mode)
        return original(task_id, frame_mode=frame_mode)

    service.read_runtime_state = spying  # type: ignore[method-assign]

    task = service._store.get_task(TASK_ID)
    notifier = TaskStallNotifier(service=_NotifierService(service, task))
    notifier._schedule(TASK_ID)

    assert seen == ['stall']
    assert content_store.hits == 0
    notifier.cancel_task(TASK_ID)
