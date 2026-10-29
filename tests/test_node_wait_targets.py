"""`enter_wait.waiting_for`: declaration resolution, wait edges and waiver notices."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from main.runtime.node_relationships import resolve_wait_targets
from main.runtime.wait_graph import (
    NODE_WAIT_TARGETS_KEY,
    build_wait_nodes,
    find_wait_cycles,
)
from main.service.runtime_service import MainRuntimeService


def _node(
    node_id: str,
    *,
    parent: str = '',
    status: str = 'in_progress',
    kind: str = 'execution',
    accepted: str = '',
) -> SimpleNamespace:
    metadata = {'accepted_node_id': accepted} if accepted else {}
    return SimpleNamespace(
        node_id=node_id,
        parent_node_id=parent,
        node_kind=kind,
        status=status,
        depth=1,
        metadata=metadata,
    )


# --- declaration resolution -------------------------------------------------


def test_no_declaration_keeps_the_old_behaviour():
    nodes = {'node:1': _node('node:1', parent='root')}
    assert resolve_wait_targets(waiter=nodes['node:1'], waiting_for=None, node_map=nodes) == (
        [],
        False,
        [],
    )
    assert resolve_wait_targets(waiter=nodes['node:1'], waiting_for=[], node_map=nodes) == (
        [],
        False,
        [],
    )


def test_frontdoor_is_external_and_never_an_edge():
    nodes = {'node:1': _node('node:1', parent='root')}
    targets, external, problems = resolve_wait_targets(
        waiter=nodes['node:1'], waiting_for='frontdoor', node_map=nodes
    )
    assert (targets, external, problems) == ([], True, [])


def test_parent_resolves_to_the_parent_id():
    nodes = {
        'root': _node('root'),
        'node:1': _node('node:1', parent='root'),
    }
    targets, external, problems = resolve_wait_targets(
        waiter=nodes['node:1'], waiting_for='parent', node_map=nodes
    )
    assert (targets, external, problems) == (['root'], False, [])


def test_parent_on_a_root_means_waiting_on_a_human():
    nodes = {'root': _node('root')}
    targets, external, problems = resolve_wait_targets(
        waiter=nodes['root'], waiting_for='parent', node_map=nodes
    )
    assert (targets, external, problems) == ([], True, [])


def test_sibling_is_a_legal_target():
    nodes = {
        'root': _node('root'),
        'node:a': _node('node:a', parent='root'),
        'node:b': _node('node:b', parent='root'),
    }
    targets, external, problems = resolve_wait_targets(
        waiter=nodes['node:a'], waiting_for='node:b', node_map=nodes
    )
    assert (targets, external, problems) == (['node:b'], False, [])


def test_missing_target_is_a_problem():
    nodes = {'node:a': _node('node:a', parent='root')}
    targets, _, problems = resolve_wait_targets(
        waiter=nodes['node:a'], waiting_for='node:ghost', node_map=nodes
    )
    assert targets == []
    assert problems == [{'target': 'node:ghost', 'reason': 'wait_target_not_in_task'}]


def test_terminal_target_is_refused_instead_of_parked():
    nodes = {
        'root': _node('root'),
        'node:a': _node('node:a', parent='root'),
        'node:b': _node('node:b', parent='root', status='success'),
    }
    targets, _, problems = resolve_wait_targets(
        waiter=nodes['node:a'], waiting_for='node:b', node_map=nodes
    )
    assert targets == []
    assert problems == [{'target': 'node:b', 'reason': 'wait_target_terminal'}]


def test_a_target_that_cannot_address_the_waiter_is_refused():
    nodes = {
        'root': _node('root'),
        'node:mid': _node('node:mid', parent='root'),
        'node:a': _node('node:a', parent='root'),
        'node:cousin': _node('node:cousin', parent='node:mid'),
    }
    targets, _, problems = resolve_wait_targets(
        waiter=nodes['node:a'], waiting_for='node:cousin', node_map=nodes
    )
    assert targets == []
    assert problems == [
        {'target': 'node:cousin', 'reason': 'wait_target_not_messageable:not_messageable'}
    ]


def test_duplicate_targets_are_collapsed():
    nodes = {
        'root': _node('root'),
        'node:a': _node('node:a', parent='root'),
        'node:b': _node('node:b', parent='root'),
    }
    targets, _, problems = resolve_wait_targets(
        waiter=nodes['node:a'], waiting_for=['node:b', 'node:b'], node_map=nodes
    )
    assert (targets, problems) == (['node:b'], [])


# --- wait edges -------------------------------------------------------------


def _frames(*node_ids: str, phase: str = 'waiting_explicit') -> dict[str, dict]:
    return {node_id: {'phase': phase} for node_id in node_ids}


def _with_targets(node: SimpleNamespace, *targets: str) -> SimpleNamespace:
    node.metadata[NODE_WAIT_TARGETS_KEY] = list(targets)
    return node


def test_mutual_declaration_is_a_cycle():
    a = _with_targets(_node('node:a', parent='root'), 'node:b')
    b = _with_targets(_node('node:b', parent='root'), 'node:a')
    nodes = build_wait_nodes(records=[a, b], frames=_frames('node:a', 'node:b'))
    cycles = find_wait_cycles(nodes)
    assert len(cycles) == 1
    assert set(cycles[0]) == {'node:a', 'node:b'}


def test_one_sided_wait_is_not_a_cycle():
    a = _with_targets(_node('node:a', parent='root'), 'node:b')
    b = _node('node:b', parent='root')
    nodes = build_wait_nodes(records=[a, b], frames=_frames('node:a', 'node:b'))
    assert find_wait_cycles(nodes) == []


def test_a_running_target_does_not_form_an_edge():
    a = _with_targets(_node('node:a', parent='root'), 'node:b')
    b = _with_targets(_node('node:b', parent='root'), 'node:a')
    frames = {'node:a': {'phase': 'waiting_explicit'}, 'node:b': {'phase': 'waiting_tool_results'}}
    nodes = build_wait_nodes(records=[a, b], frames=frames)
    assert find_wait_cycles(nodes) == []


def test_a_terminal_target_does_not_form_an_edge():
    a = _with_targets(_node('node:a', parent='root'), 'node:b')
    b = _with_targets(_node('node:b', parent='root', status='failed'), 'node:a')
    nodes = build_wait_nodes(records=[a, b], frames=_frames('node:a', 'node:b'))
    assert find_wait_cycles(nodes) == []


def test_waiting_on_a_human_never_forms_an_edge():
    a = _node('node:a', parent='root')
    b = _with_targets(_node('node:b', parent='root'), 'node:a')
    nodes = build_wait_nodes(records=[a, b], frames=_frames('node:a', 'node:b'))
    assert find_wait_cycles(nodes) == []


# --- wake notice ------------------------------------------------------------


def test_gone_message_carries_the_note_and_the_reason():
    body = MainRuntimeService._waiter_gone_message(
        note='等兄弟汇总',
        goal='汇总分支',
        gone_node_id='node:b',
        event='terminal',
        detail='已产出三条结论',
    )
    assert '你上次停靠时在等：等兄弟汇总' in body
    assert 'node:b' in body
    assert '已结束' in body
    assert '已产出三条结论' in body


def test_paused_message_reads_as_paused():
    body = MainRuntimeService._waiter_gone_message(
        note='',
        goal='汇总分支',
        gone_node_id='node:b',
        event='paused',
        detail='看门狗判停',
    )
    assert '已暂停' in body
    assert '看门狗判停' in body


# --- targeted wake ----------------------------------------------------------


def _service(records: list[SimpleNamespace], delivered: list[dict]) -> MainRuntimeService:
    service = object.__new__(MainRuntimeService)
    service.store = SimpleNamespace(list_nodes=lambda task_id: records)
    service.node_runner = SimpleNamespace(NODE_WAIT_NOTE_KEY='node_wait_note')
    service.normalize_task_id = lambda value: str(value)

    async def _deliver(**payload):
        delivered.append(payload)
        return {'status': 'delivered'}

    service.deliver_node_message = _deliver
    return service


@pytest.mark.asyncio
async def test_only_nodes_that_declared_the_target_are_woken():
    waiter = _with_targets(_node('node:a', parent='root'), 'node:b')
    waiter.metadata['node_wait_note'] = '等 b 的结论'
    bystander = _with_targets(_node('node:c', parent='root'), 'node:d')
    gone = _node('node:b', parent='root', status='success')
    delivered: list[dict] = []
    service = _service([waiter, bystander, gone], delivered)

    await service._notify_node_waiters(
        task_id='task:1',
        gone_node_id='node:b',
        event='terminal',
        detail='三条结论',
        goal='汇总分支',
    )

    assert len(delivered) == 1
    assert delivered[0]['target_node_id'] == 'node:a'
    assert delivered[0]['origin'] == 'frontdoor'
    assert '等 b 的结论' in delivered[0]['message']
    assert '三条结论' in delivered[0]['message']


@pytest.mark.asyncio
async def test_terminal_nodes_are_not_woken():
    waiter = _with_targets(_node('node:a', parent='root', status='failed'), 'node:b')
    delivered: list[dict] = []
    service = _service([waiter], delivered)

    await service._notify_node_waiters(
        task_id='task:1', gone_node_id='node:b', event='paused', detail='x'
    )

    assert delivered == []


@pytest.mark.asyncio
async def test_held_batch_parent_is_skipped():
    parent = _with_targets(_node('node:p', parent='root'), 'node:b')
    sibling = _with_targets(_node('node:a', parent='root'), 'node:b')
    delivered: list[dict] = []
    service = _service([parent, sibling], delivered)

    await service._notify_node_waiters(
        task_id='task:1',
        gone_node_id='node:b',
        event='terminal',
        skip_node_ids={'node:p'},
    )

    assert [item['target_node_id'] for item in delivered] == ['node:a']


# --- the park itself --------------------------------------------------------


class _Record:
    def __init__(self, node_id: str, *, parent: str = '', status: str = 'in_progress') -> None:
        self.node_id = node_id
        self.parent_node_id = parent
        self.status = status
        self.node_kind = 'execution'
        self.depth = 1
        self.metadata: dict = {}

    def model_copy(self, *, update: dict | None = None) -> _Record:
        clone = _Record(self.node_id, parent=self.parent_node_id, status=self.status)
        clone.metadata = dict((update or {}).get('metadata') or self.metadata)
        return clone


class _ParkStore:
    def __init__(self, records: list[_Record]) -> None:
        self._records = {record.node_id: record for record in records}

    def list_nodes(self, task_id: str) -> list[_Record]:
        _ = task_id
        return list(self._records.values())

    def update_node(self, node_id: str, mutate) -> None:
        self._records[node_id] = mutate(self._records[node_id])


class _ParkLog:
    def __init__(self) -> None:
        self.frames: dict[str, dict] = {}

    def update_frame(self, task_id: str, node_id: str, mutate) -> None:
        _ = task_id
        self.frames[node_id] = mutate(self.frames.get(node_id) or {})


def _park_runner(records: list[_Record]):
    from main.runtime.node_runner import NodeRunner

    runner = object.__new__(NodeRunner)
    runner._store = _ParkStore(records)
    runner._log_service = _ParkLog()
    runner._pending_node_notifications = lambda task_id, node_id: []
    return runner


@pytest.mark.asyncio
async def test_undeclared_park_keeps_the_old_shape():
    root = _Record('root')
    node = _Record('node:a', parent='root')
    runner = _park_runner([root, node])

    result = await runner._enter_wait(task_id='task:1', node=node, note='等消息')

    assert result['status'] == 'parked'
    assert result['waiting_for'] == {'targets': [], 'external': False}
    assert runner._store.list_nodes('task:1')[1].metadata[NODE_WAIT_TARGETS_KEY] == []
    assert runner._log_service.frames['node:a']['phase'] == 'waiting_explicit'


@pytest.mark.asyncio
async def test_declared_sibling_is_persisted_for_the_reverse_lookup():
    root = _Record('root')
    node = _Record('node:a', parent='root')
    sibling = _Record('node:b', parent='root')
    runner = _park_runner([root, node, sibling])

    result = await runner._enter_wait(
        task_id='task:1', node=node, note='等兄弟', waiting_for='node:b'
    )

    assert result['status'] == 'parked'
    assert result['waiting_for'] == {'targets': ['node:b'], 'external': False}
    assert runner._store.list_nodes('task:1')[1].metadata[NODE_WAIT_TARGETS_KEY] == ['node:b']


@pytest.mark.asyncio
async def test_terminal_target_refuses_the_park():
    root = _Record('root')
    node = _Record('node:a', parent='root')
    done = _Record('node:b', parent='root', status='success')
    runner = _park_runner([root, node, done])

    result = await runner._enter_wait(
        task_id='task:1', node=node, note='等兄弟', waiting_for='node:b'
    )

    assert result['status'] == 'not_parked_invalid_targets'
    assert result['problems'] == [{'target': 'node:b', 'reason': 'wait_target_terminal'}]
    assert NODE_WAIT_TARGETS_KEY not in runner._store.list_nodes('task:1')[1].metadata
    assert runner._log_service.frames == {}


