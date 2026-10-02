from __future__ import annotations

import json
from pathlib import Path

from main.models import NodeOutputEntry, NodeRecord, TaskRecord, TokenUsageSummary
from main.monitoring.file_store import TaskFileStore
from main.monitoring.log_service import TaskLogService
from main.storage.sqlite_store import SQLiteTaskStore

TASK_ID = 'task:hydrskipped'
NODE_ID = 'node:a'


def _task_record() -> TaskRecord:
    return TaskRecord(
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
    )


def _node_record() -> NodeRecord:
    return NodeRecord(
        node_id=NODE_ID,
        task_id=TASK_ID,
        parent_node_id=None,
        root_node_id=NODE_ID,
        depth=0,
        node_kind='execution',
        status='in_progress',
        goal='demo',
        prompt='demo',
        input='demo',
        output=[
            NodeOutputEntry(
                seq=1,
                content='',
                tool_calls=[{'id': 'call-1', 'name': 'filesystem_read', 'arguments': {'path': 'a.md'}}],
                created_at='2026-10-01T10:00:01+08:00',
            )
        ],
        check_result='',
        final_output='',
        can_spawn_children=False,
        created_at='2026-10-01T10:00:00+08:00',
        updated_at='2026-10-01T10:00:00+08:00',
        token_usage=TokenUsageSummary(tracked=True),
    )


class _ContentStore:
    """只装 `_resolve_content_ref` 要用的那一个方法，并数它被打了多少次。

    计数是这条测试的重点：水合一帧=把 `messages_ref` 指向的整份会话历史读盘解析一遍，
    而投影车道一次都不该碰它。
    """

    def __init__(self, messages: list[dict]) -> None:
        self._messages = messages
        self.hits = 0

    def _resolve(self, *, ref: str, path: object) -> tuple[str, None]:
        self.hits += 1
        return json.dumps({'messages': self._messages}, ensure_ascii=False), None


def _frame() -> dict:
    return {
        'node_id': NODE_ID,
        'depth': 0,
        'node_kind': 'execution',
        'phase': 'running',
        'active': False,
        'runnable': True,
        'waiting': False,
        'stage_goal': 'demo goal',
        'stage_status': '进行中',
        'messages_ref': 'content:messages/node-a',
        'messages_count': 2,
        'actual_request_ref': 'content:actual-request/node-a',
        'prompt_cache_key_hash': 'pck-1',
        'actual_request_hash': 'arh-1',
        'actual_request_message_count': 7,
        'actual_tool_schema_hash': 'tsh-1',
        'tool_calls': [
            {'tool_call_id': 'call-1', 'name': 'filesystem_read', 'status': 'running', 'arguments': {'path': 'a.md'}},
            {'tool_call_id': 'call-2', 'name': 'filesystem_edit', 'status': 'pending', 'arguments': {'path': 'b.md'}},
        ],
    }


def _service(tmp_path: Path) -> tuple[TaskLogService, _ContentStore]:
    store = SQLiteTaskStore(tmp_path / 'runtime.sqlite3')
    store.upsert_task(_task_record())
    store.upsert_node(_node_record())
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
    service.upsert_frame(TASK_ID, _frame())
    return service, content_store


def _detail_payload(service: TaskLogService) -> dict:
    node = service._store.get_node(NODE_ID)
    record = service._task_projection_node_detail_record(node, externalize_execution_trace=False)
    return dict(record.payload or {})

def test_the_lane_being_cut_really_reads_the_message_blob(tmp_path) -> None:
    """对照：先证明计数不是桩件的假象——整帧水合每读一帧就打一次内容存储，并拿到历史正文。"""
    service, content_store = _service(tmp_path)

    frame = service.read_runtime_frame(TASK_ID, NODE_ID) or {}

    assert content_store.hits == 1
    assert len(frame.get('messages') or []) == 2


def test_projection_trace_takes_tool_calls_without_touching_history(tmp_path) -> None:
    service, content_store = _service(tmp_path)
    node = service._store.get_node(NODE_ID)

    trace = service._projection_execution_trace(node)

    assert content_store.hits == 0
    assert [item['tool_call_id'] for item in trace['live_tool_calls']] == ['call-1', 'call-2']
    step = (trace['tool_steps'] or [{}])[0]
    assert step.get('tool_call_id') == 'call-1' and step.get('status') == 'running'


def test_detail_record_is_identical_to_the_hydrated_one(tmp_path) -> None:
    """字段等价：换成只读正文之后 detail 的 payload 必须逐字不变。

    顺带量出旧口径一次 detail 重建把同一帧水合了**两遍**——`_task_projection_node_detail_record`
    自己读一次（只为几个 ref 标量），`_projection_execution_trace` 再读一次（只为
    tool_calls），两遍都要把 `messages_ref` 指向的会话历史读盘、解码、解析。
    """
    service, content_store = _service(tmp_path)

    trimmed = _detail_payload(service)
    assert content_store.hits == 0

    service.read_runtime_frame_payload = service.read_runtime_frame  # type: ignore[method-assign]
    hydrated = _detail_payload(service)

    # 旧口径里同一帧被水合两遍：detail 自己读一次（只为几个 ref 标量），
    # `_projection_execution_trace` 再读一次（只为 tool_calls）。
    assert content_store.hits == 2
    assert hydrated == trimmed
    assert trimmed.get('actual_request_ref') == 'content:actual-request/node-a'
    assert trimmed.get('actual_request_message_count') == 7
    # 明细行的 payload 不再内联整份轨迹摘要——它的家是外置 artifact，读侧按 ref 现算
    # （构造函数与存进去的逐字节相同，见 test_execution_trace_summary_compaction.py）。
    assert 'execution_trace_summary' not in trimmed


def test_payload_reader_still_reports_a_missing_frame_as_none(tmp_path) -> None:
    service, _content_store = _service(tmp_path)

    assert service.read_runtime_frame_payload(TASK_ID, 'node:missing') is None


def test_public_frame_is_identical_without_the_message_bodies(tmp_path) -> None:
    """live.patch 的单帧投影是字段白名单，不含 `messages`：解析正文是白活。

    这条断言把"公开投影读不到正文"钉在形状上——将来谁往 `_public_runtime_frame`
    里加 `messages`，这里就会红，那时才需要在发布车道把正文读回来。
    """
    service, content_store = _service(tmp_path)
    record = service._store.get_task_runtime_frame(TASK_ID, NODE_ID)

    light = service._public_runtime_frame(
        service._hydrate_runtime_frame_record(record, include_messages=False)
    )
    light_hits = content_store.hits
    heavy = service._public_runtime_frame(service._hydrate_runtime_frame_record(record))

    assert light_hits == 0
    assert content_store.hits == 1
    assert light == heavy
    assert 'messages' not in light
