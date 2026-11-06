"""连续（待命）节点：标记、验收强制与自动验收条款。"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from main.models import (
    ExecutionPolicyState,
    NodeRecord,
    SpawnChildResult,
    SpawnChildSpec,
    TaskNodeNotification,
    TaskRecord,
)
from main.runtime.continuous_node import (
    CONTINUOUS_ACCEPTANCE_CLAUSE,
    CONTINUOUS_METADATA_KEY,
    compose_continuous_acceptance_prompt,
    is_continuous_metadata,
)
from main.runtime.internal_tools import SpawnChildNodesTool
from main.runtime.node_runner import _spec_requires_acceptance
from main.runtime.plain_text_reply import build_node_plain_text_reply_block_message
from main.service.runtime_service import MainRuntimeService
from main.service.task_stall_callback import (
    TASK_STALL_REASON_DESIGNED_IDLE,
    TASK_STALL_REASON_SUSPECTED_STALL,
)
from tools.create_async_task_cn.main.tool import _CreateAsyncTaskHandler

STAMP = '2026-11-06T00:00:00+08:00'


class _DummyChatBackend:
    async def chat(self, **kwargs):
        raise AssertionError(f'chat backend must not run in these tests: {kwargs!r}')


async def _noop_enqueue_task(_task_id: str) -> None:
    return None


def _service(tmp_path: Path) -> MainRuntimeService:
    service = MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        workspace_root=tmp_path,
        store_path=tmp_path / 'runtime.sqlite3',
        files_base_dir=tmp_path / 'tasks',
        artifact_dir=tmp_path / 'artifacts',
        governance_store_path=tmp_path / 'governance.sqlite3',
        execution_mode='embedded',
    )
    service.global_scheduler.enqueue_task = _noop_enqueue_task
    return service


def _spec(**overrides) -> SpawnChildSpec:
    payload = {
        'goal': 'g',
        'prompt': 'p',
        'execution_policy': ExecutionPolicyState(mode='focus'),
    }
    payload.update(overrides)
    return SpawnChildSpec(**payload)


def _seed_parent(service: MainRuntimeService) -> tuple[TaskRecord, NodeRecord]:
    task = TaskRecord(
        task_id='task:continuous',
        title='t',
        user_request='u',
        root_node_id='node:root',
        created_at=STAMP,
        updated_at=STAMP,
        metadata={'execution_policy': {'mode': 'focus'}},
    )
    parent = NodeRecord(
        node_id='node:root',
        task_id=task.task_id,
        parent_node_id=None,
        root_node_id='node:root',
        depth=0,
        node_kind='execution',
        goal='root',
        prompt='p',
        created_at=STAMP,
        updated_at=STAMP,
        metadata={'execution_policy': {'mode': 'focus'}},
    )
    service.store.upsert_task(task)
    service.store.upsert_node(parent)
    return task, parent


def test_continuous_forces_acceptance_against_explicit_false() -> None:
    assert _spec_requires_acceptance(_spec(continuous=True, requires_acceptance=False)) is True
    assert _spec_requires_acceptance(_spec(continuous=True, acceptance_prompt='')) is True
    assert _spec_requires_acceptance(_spec(continuous=False, requires_acceptance=False)) is False
    assert _spec_requires_acceptance(_spec(acceptance_prompt='criteria')) is True


def test_compose_clause_is_deterministic_and_prepends() -> None:
    assert compose_continuous_acceptance_prompt('') == CONTINUOUS_ACCEPTANCE_CLAUSE
    composed = compose_continuous_acceptance_prompt('只有 A 成立才通过')
    assert composed.startswith(CONTINUOUS_ACCEPTANCE_CLAUSE)
    assert '只有 A 成立才通过' in composed
    assert composed == compose_continuous_acceptance_prompt('只有 A 成立才通过')


def test_metadata_predicate() -> None:
    assert is_continuous_metadata({CONTINUOUS_METADATA_KEY: True}) is True
    assert is_continuous_metadata({}) is False
    assert is_continuous_metadata(None) is False


@pytest.mark.asyncio
async def test_continuous_root_forces_final_acceptance(tmp_path: Path) -> None:
    service = _service(tmp_path)
    task = await service.create_task(
        '长期订阅任务',
        session_id='web:test',
        continuous=True,
        metadata={'execution_policy': {'mode': 'focus'}},
    )

    final_acceptance = task.metadata['final_acceptance']
    assert final_acceptance['required'] is True
    assert CONTINUOUS_ACCEPTANCE_CLAUSE in final_acceptance['prompt']
    assert str(final_acceptance['node_id']).strip()

    root = service.store.get_node(task.root_node_id)
    assert root is not None
    assert is_continuous_metadata(root.metadata)

    acceptance = service.store.get_node(final_acceptance['node_id'])
    assert acceptance is not None
    assert acceptance.node_kind == 'acceptance'
    assert CONTINUOUS_ACCEPTANCE_CLAUSE in acceptance.metadata['acceptance_prompt_template']
    assert not is_continuous_metadata(acceptance.metadata)


@pytest.mark.asyncio
async def test_non_continuous_root_stays_unchanged(tmp_path: Path) -> None:
    service = _service(tmp_path)
    task = await service.create_task(
        '一次性任务',
        session_id='web:test',
        metadata={'execution_policy': {'mode': 'focus'}},
    )

    final_acceptance = task.metadata['final_acceptance']
    assert final_acceptance['required'] is False
    assert final_acceptance['prompt'] == ''
    assert final_acceptance['node_id'] == ''

    root = service.store.get_node(task.root_node_id)
    assert root is not None
    assert CONTINUOUS_METADATA_KEY not in (root.metadata or {})


def test_execution_child_metadata_carries_continuous(tmp_path: Path) -> None:
    service = _service(tmp_path)
    task, parent = _seed_parent(service)

    continuous_child = service.node_runner._create_execution_child(
        task=task, parent=parent, spec=_spec(continuous=True)
    )
    normal_child = service.node_runner._create_execution_child(
        task=task, parent=parent, spec=_spec()
    )

    assert continuous_child.metadata[CONTINUOUS_METADATA_KEY] is True
    assert CONTINUOUS_METADATA_KEY not in normal_child.metadata


def test_spawn_acceptance_injects_clause_when_prompt_missing(tmp_path: Path) -> None:
    service = _service(tmp_path)
    task, parent = _seed_parent(service)
    child = service.node_runner._create_execution_child(
        task=task, parent=parent, spec=_spec(continuous=True)
    )
    cached_payload: dict = {'entries': [{'goal': 'g'}]}

    acceptance = service.node_runner._ensure_spawn_acceptance_node(
        task=task,
        parent=parent,
        child=child,
        spec=_spec(continuous=True),
        cache_key='round-1',
        cached_payload=cached_payload,
        index=0,
    )

    assert acceptance.goal == 'accept:g'
    assert acceptance.metadata['acceptance_prompt_template'] == CONTINUOUS_ACCEPTANCE_CLAUSE
    assert cached_payload['entries'][0]['acceptance_node_id'] == acceptance.node_id


def test_spawn_acceptance_keeps_model_prompt_after_clause(tmp_path: Path) -> None:
    service = _service(tmp_path)
    task, parent = _seed_parent(service)
    spec = _spec(continuous=True, acceptance_prompt='只有 A 成立才通过')
    child = service.node_runner._create_execution_child(task=task, parent=parent, spec=spec)

    acceptance = service.node_runner._ensure_spawn_acceptance_node(
        task=task,
        parent=parent,
        child=child,
        spec=spec,
        cache_key='round-1',
        cached_payload={'entries': [{'goal': 'g'}]},
        index=0,
    )

    template = acceptance.metadata['acceptance_prompt_template']
    assert template.startswith(CONTINUOUS_ACCEPTANCE_CLAUSE)
    assert '只有 A 成立才通过' in template


def test_spawn_spec_payload_keeps_legacy_fingerprint(tmp_path: Path) -> None:
    service = _service(tmp_path)

    normal_payload = service.node_runner._spawn_spec_payload(_spec())
    continuous_payload = service.node_runner._spawn_spec_payload(_spec(continuous=True))

    assert 'continuous' not in normal_payload
    assert continuous_payload['continuous'] is True
    assert normal_payload['requires_acceptance'] is False
    assert continuous_payload['requires_acceptance'] is True


def _mark_waiting(service: MainRuntimeService, task_id: str, node_id: str, *, external: bool, phase: str = 'waiting_explicit') -> None:
    service.store.update_node(
        node_id,
        lambda record: record.model_copy(
            update={
                'metadata': {
                    **dict(record.metadata or {}),
                    'node_wait_external': bool(external),
                    'node_wait_targets': [],
                }
            }
        ),
    )
    service.log_service.upsert_frame(
        task_id,
        {'node_id': node_id, 'depth': 0, 'node_kind': 'execution', 'phase': phase},
    )


def _stall_reason(service: MainRuntimeService, task_id: str) -> str:
    state = service.log_service.read_runtime_state(task_id, frame_mode='stall') or {}
    return service.classify_task_stall_reason(task_id, runtime_state=state)


def test_designed_idle_when_every_execution_node_waits_external(tmp_path: Path) -> None:
    service = _service(tmp_path)
    task, parent = _seed_parent(service)
    _mark_waiting(service, task.task_id, parent.node_id, external=True)

    assert _stall_reason(service, task.task_id) == TASK_STALL_REASON_DESIGNED_IDLE
    assert service.is_task_stall_designed_idle(task.task_id) is True
    assert service.is_task_stall_actionable(task.task_id) is False


def test_internal_only_wait_is_not_exempt(tmp_path: Path) -> None:
    service = _service(tmp_path)
    task, parent = _seed_parent(service)
    _mark_waiting(service, task.task_id, parent.node_id, external=False)

    assert _stall_reason(service, task.task_id) == TASK_STALL_REASON_SUSPECTED_STALL


def test_unread_mail_breaks_designed_idle(tmp_path: Path) -> None:
    service = _service(tmp_path)
    task, parent = _seed_parent(service)
    _mark_waiting(service, task.task_id, parent.node_id, external=True)
    service.store.upsert_task_node_notification(
        TaskNodeNotification(
            notification_id='notif:1',
            task_id=task.task_id,
            node_id=parent.node_id,
            epoch_id='',
            status='delivered',
            created_at=STAMP,
            delivered_at=STAMP,
            message='wake up',
        )
    )

    assert _stall_reason(service, task.task_id) == TASK_STALL_REASON_SUSPECTED_STALL


def test_inflight_phase_breaks_designed_idle(tmp_path: Path) -> None:
    service = _service(tmp_path)
    task, parent = _seed_parent(service)
    _mark_waiting(service, task.task_id, parent.node_id, external=True, phase='before_model')

    assert _stall_reason(service, task.task_id) == TASK_STALL_REASON_SUSPECTED_STALL


def test_spawn_receipt_marks_auto_acceptance(tmp_path: Path) -> None:
    service = _service(tmp_path)
    task, parent = _seed_parent(service)
    spec = _spec(continuous=True)
    child = service.node_runner._create_execution_child(task=task, parent=parent, spec=spec)
    cached_payload: dict = {'entries': [{'goal': 'g', 'child_node_id': child.node_id}]}

    acceptance = service.node_runner._ensure_spawn_acceptance_node(
        task=task,
        parent=parent,
        child=child,
        spec=spec,
        cache_key='round-1',
        cached_payload=cached_payload,
        index=0,
    )
    receipts = service.node_runner._spawn_creation_receipts(
        specs=[spec], entries=cached_payload['entries']
    )

    assert receipts[0].acceptance_auto_created is True
    assert acceptance.node_id in receipts[0].acceptance_note

    replay = service.node_runner._spawn_creation_receipts(
        specs=[spec], entries=cached_payload['entries']
    )
    assert replay[0].model_dump() == receipts[0].model_dump()


def test_spawn_receipt_does_not_mark_when_prompt_given(tmp_path: Path) -> None:
    service = _service(tmp_path)
    task, parent = _seed_parent(service)
    spec = _spec(continuous=True, acceptance_prompt='只有 A 成立才通过')
    child = service.node_runner._create_execution_child(task=task, parent=parent, spec=spec)
    cached_payload: dict = {'entries': [{'goal': 'g', 'child_node_id': child.node_id}]}

    service.node_runner._ensure_spawn_acceptance_node(
        task=task,
        parent=parent,
        child=child,
        spec=spec,
        cache_key='round-1',
        cached_payload=cached_payload,
        index=0,
    )
    receipts = service.node_runner._spawn_creation_receipts(
        specs=[spec], entries=cached_payload['entries']
    )

    assert receipts[0].acceptance_auto_created is False
    assert receipts[0].acceptance_note == ''


@pytest.mark.asyncio
async def test_spawn_tool_forwards_auto_acceptance_note() -> None:
    async def _spawn(specs, round_id, wait_all):
        return [
            SpawnChildResult(
                goal='g',
                state='created',
                child_node_id='node:child',
                acceptance_node_id='node:acceptance',
                acceptance_auto_created=True,
                acceptance_note='该子节点是连续节点，运行时已自动为它创建验收节点 node:acceptance。',
            )
        ]

    tool = SpawnChildNodesTool(_spawn)
    payload = json.loads(
        await tool.execute(
            children=[
                {
                    'goal': 'g',
                    'prompt': 'p',
                    'execution_policy': {'mode': 'focus'},
                    'continuous': True,
                }
            ]
        )
    )

    assert payload['created'][0]['acceptance_auto_created'] is True
    assert 'node:acceptance' in payload['created'][0]['acceptance_note']


@pytest.mark.asyncio
async def test_continuous_bootstrap_marks_run_mode_and_prompt(tmp_path: Path) -> None:
    service = _service(tmp_path)
    task, parent = _seed_parent(service)
    continuous_child = service.node_runner._create_execution_child(
        task=task, parent=parent, spec=_spec(continuous=True)
    )
    normal_child = service.node_runner._create_execution_child(
        task=task, parent=parent, spec=_spec()
    )

    continuous_messages = await service.node_runner._build_messages(task=task, node=continuous_child)
    normal_messages = await service.node_runner._build_messages(task=task, node=normal_child)

    continuous_payload = json.loads(continuous_messages[1]['content'])
    assert continuous_payload['run_mode'] == 'continuous'
    assert '连续节点收尾契约' in continuous_messages[0]['content']

    normal_payload = json.loads(normal_messages[1]['content'])
    assert 'run_mode' not in normal_payload
    assert '连续节点收尾契约' not in normal_messages[0]['content']


def test_plain_text_lane_offers_enter_wait_only_to_continuous() -> None:
    continuous = build_node_plain_text_reply_block_message(node_kind='execution', continuous=True)
    assert 'enter_wait(waiting_for=["frontdoor"])' in continuous
    assert 'submit_final_result' in continuous

    legacy = build_node_plain_text_reply_block_message(node_kind='execution')
    assert 'enter_wait' not in legacy
    assert 'spawn child nodes if needed' in legacy

    acceptance = build_node_plain_text_reply_block_message(node_kind='acceptance', continuous=True)
    assert 'enter_wait' not in acceptance
    assert 'keep verifying the acceptance criteria' in acceptance


@pytest.mark.asyncio
async def test_create_async_task_tool_passes_continuous() -> None:
    captured: dict[str, object] = {}

    class _CapturingService:
        async def precheck_async_task_creation(self, **kwargs):
            return {'decision': 'allow'}

        async def create_task(self, task, **kwargs):
            captured['task'] = task
            captured.update(kwargs)
            return SimpleNamespace(task_id='task:created')

    tool = _CreateAsyncTaskHandler(_CapturingService())
    result = await tool.execute(
        't',
        core_requirement='c',
        execution_policy={'mode': 'focus'},
        continuous=True,
        __g3ku_runtime={'session_key': 'web:test'},
    )

    assert captured['continuous'] is True
    assert result == '创建任务成功task:created'
