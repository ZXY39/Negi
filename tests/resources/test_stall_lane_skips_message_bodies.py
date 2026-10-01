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


def _frames(count: int = 3) -> list[dict]:
    out = []
    for index in range(count):
        out.append({
            'node_id': f'node:{index}',
            'depth': 0,
            'node_kind': 'execution',
            'phase': 'running',
            'active': index == 0,
            'runnable': True,
            'waiting': False,
            'stage_goal': f'goal-{index}',
            'messages_ref': f'content:messages/node-{index}',
            'messages_count': 2,
            'tool_calls': [{
                'tool_call_id': f'call-{index}',
                'name': 'filesystem_read',
                'status': 'running',
                'started_at': STARTED_AT,
                'timeout_seconds': 300,
            }],
        })
    return out


def _service(tmp_path, *, frame_count: int = 3) -> tuple[TaskLogService, _ContentStore]:
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
    for index in range(frame_count):
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
    for frame in _frames(frame_count):
        service.upsert_frame(TASK_ID, frame)
    return service, content_store


def test_light_state_skips_every_message_body_but_keeps_frame_state(tmp_path) -> None:
    service, content_store = _service(tmp_path)

    state = service.read_runtime_state(TASK_ID, include_frame_messages=False) or {}

    assert content_store.hits == 0
    assert len(state['frames']) == 3
    frame = state['frames'][0]
    assert frame['messages'] == []
    # 指针与计数是 payload 里的字段，不解析正文也必须在
    assert frame['messages_ref'] == 'content:messages/node-0'
    assert frame['messages_count'] == 2
    assert frame['tool_calls'][0]['status'] == 'running'
    assert 'node:0' in state['active_node_ids']


def test_default_state_still_resolves_each_frame(tmp_path) -> None:
    """对照：台账 3 帧 ⇒ 默认口径打 3 次内容存储——这条计数不是桩件假象。"""
    service, content_store = _service(tmp_path)

    state = service.read_runtime_state(TASK_ID) or {}

    assert content_store.hits == 3
    assert len(state['frames'][0]['messages']) == 2


def test_running_tool_deadline_is_identical_on_both_reads(tmp_path) -> None:
    """失速判据依赖的唯一帧内字段：两种读法必须给出同一个截止时间。"""
    service, _content_store = _service(tmp_path)

    light = service.read_runtime_state(TASK_ID, include_frame_messages=False) or {}
    heavy = service.read_runtime_state(TASK_ID) or {}

    light_deadline = running_tool_deadline(light)
    assert light_deadline is not None
    assert light_deadline == running_tool_deadline(heavy)
    assert effective_silence_start(light, STARTED_AT) == effective_silence_start(heavy, STARTED_AT)


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


def test_the_stall_lane_asks_for_the_light_state(tmp_path) -> None:
    service, content_store = _service(tmp_path, frame_count=3)
    original = service.read_runtime_state
    seen: list[bool] = []

    def spying(task_id: str, *, include_frame_messages: bool = True):
        seen.append(include_frame_messages)
        return original(task_id, include_frame_messages=include_frame_messages)

    service.read_runtime_state = spying  # type: ignore[method-assign]

    task = service._store.get_task(TASK_ID)
    notifier = TaskStallNotifier(service=_NotifierService(service, task))
    notifier._schedule(TASK_ID)

    assert seen == [False]
    assert content_store.hits == 0
    notifier.cancel_task(TASK_ID)
