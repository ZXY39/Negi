"""连续（待命）节点：标记、验收强制与自动验收条款。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from main.models import ExecutionPolicyState, NodeRecord, SpawnChildSpec, TaskRecord
from main.runtime.continuous_node import (
    CONTINUOUS_ACCEPTANCE_CLAUSE,
    CONTINUOUS_METADATA_KEY,
    compose_continuous_acceptance_prompt,
    is_continuous_metadata,
)
from main.runtime.node_runner import _spec_requires_acceptance
from main.service.runtime_service import MainRuntimeService
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
