"""A verifier's own error pause reaches the source session directly.

The parent of an acceptance node sits in `waiting_acceptance` and is not woken
by non-verdict messages, so the verifier's self-fault is only actionable through
the `task_node_error` lane. These tests pin the selection predicate, the payload
extension, the prompt branch and the fail exit.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from g3ku.heartbeat.node_error_scanner import NodeErrorScanner
from g3ku.heartbeat.prompt_lane import _task_node_error_lines
from main.models import NodeRecord, TaskRecord
from main.runtime.acceptance_handshake import ACCEPTANCE_HANDSHAKE_KEY, verifier_recovery_exits
from main.service.runtime_service import MainRuntimeService

TASK = 'task:acceptance-self-fault'
ROOT = 'node:root'
CHILD = 'node:child'
ACCEPTANCE = 'node:acceptance'
STAMP = '2026-11-02T00:00:00+08:00'
REF = 'artifact:submission-1'


def _service(tmp_path: Path) -> MainRuntimeService:
    return MainRuntimeService(
        chat_backend=SimpleNamespace(),
        workspace_root=tmp_path,
        store_path=tmp_path / 'runtime.sqlite3',
        files_base_dir=tmp_path / 'tasks',
        artifact_dir=tmp_path / 'artifacts',
        governance_store_path=tmp_path / 'governance.sqlite3',
        execution_mode='web',
    )


def _node(
    node_id: str,
    *,
    parent: str | None,
    kind: str = 'execution',
    depth: int = 0,
    metadata: dict | None = None,
    is_paused: bool = False,
    status: str = 'in_progress',
) -> NodeRecord:
    return NodeRecord(
        node_id=node_id,
        task_id=TASK,
        parent_node_id=parent,
        root_node_id=ROOT,
        depth=depth,
        node_kind=kind,
        status=status,
        goal=node_id,
        prompt='p',
        created_at=STAMP,
        updated_at=STAMP,
        is_paused=is_paused,
        pause_reason='error' if is_paused else '',
        metadata=dict(metadata or {}),
    )


def _seed(service: MainRuntimeService, *, handshake_state: str = 'waiting_acceptance', submission_ref: str = REF) -> None:
    service.store.upsert_task(
        TaskRecord(
            task_id=TASK,
            title='t',
            user_request='u',
            root_node_id=ROOT,
            created_at=STAMP,
            updated_at=STAMP,
        )
    )
    service.store.upsert_node(_node(ROOT, parent=None))
    service.store.upsert_node(
        _node(
            CHILD,
            parent=ROOT,
            depth=1,
            metadata={
                ACCEPTANCE_HANDSHAKE_KEY: {
                    'state': handshake_state,
                    'latest_execution_result_ref': submission_ref,
                }
            },
            status='in_progress',
        )
    )
    service.store.upsert_node(
        _node(
            ACCEPTANCE,
            parent=CHILD,
            kind='acceptance',
            depth=2,
            metadata={'accepted_node_id': CHILD, 'final_acceptance': False},
            is_paused=True,
        )
    )
    service.store.upsert_task_node_pause(
        task_id=TASK,
        node_id=ACCEPTANCE,
        pause_reason='error',
        remark='provider unavailable while verifying',
        delivered=False,
        created_at=STAMP,
        updated_at=STAMP,
    )


@pytest.mark.asyncio
async def test_error_pause_selection_includes_acceptance_but_not_non_root_execution(tmp_path: Path) -> None:
    service = _service(tmp_path)
    try:
        _seed(service)
        selected = {row['node_id'] for row in service.store.list_new_error_pauses()}
        assert ACCEPTANCE in selected
        assert CHILD not in selected

        # 非根执行节点的 error pause 仍不直达（走父节点信箱）。
        service.store.update_node(
            CHILD,
            lambda record: record.model_copy(
                update={'is_paused': True, 'pause_reason': 'error'}
            ),
        )
        service.store.upsert_task_node_pause(
            task_id=TASK,
            node_id=CHILD,
            pause_reason='error',
            remark='child failed',
            delivered=False,
            created_at=STAMP,
            updated_at=STAMP,
        )
        selected = {row['node_id'] for row in service.store.list_new_error_pauses()}
        assert ACCEPTANCE in selected
        assert CHILD not in selected
    finally:
        await service.close()


class _FakeHeartbeat:
    def __init__(self) -> None:
        self.calls: list[tuple[str, list[dict]]] = []

    def enqueue_task_node_error_payload(self, session_id: str, items: list[dict]) -> bool:
        self.calls.append((session_id, list(items)))
        return True


def _scanner(service: MainRuntimeService, heartbeat: _FakeHeartbeat) -> NodeErrorScanner:
    return NodeErrorScanner(main_task_service=service, heartbeat=heartbeat, interval_seconds=60)


@pytest.mark.asyncio
async def test_scanner_extends_acceptance_payload_and_skips_scars(tmp_path: Path) -> None:
    service = _service(tmp_path)
    heartbeat = _FakeHeartbeat()
    try:
        _seed(service)
        await _scanner(service, heartbeat).scan_once()
        assert len(heartbeat.calls) == 1
        item = heartbeat.calls[0][1][0]
        assert item['node_role'] == 'acceptance'
        assert item['inspected_node_id'] == CHILD
        assert item['inspected_node_status'] == 'in_progress'
        assert item['handshake_state'] == 'waiting_acceptance'
        assert item['submission_ref'] == REF
        assert item['final_acceptance'] is False
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_scanner_skips_terminal_inspected_node_and_handshake_without_submission(tmp_path: Path) -> None:
    service = _service(tmp_path)
    heartbeat = _FakeHeartbeat()
    try:
        _seed(service)
        service.store.update_node(
            CHILD,
            lambda record: record.model_copy(update={'status': 'success'}),
        )
        await _scanner(service, heartbeat).scan_once()
        assert heartbeat.calls == []

        service.store.update_node(
            CHILD,
            lambda record: record.model_copy(
                update={
                    'status': 'in_progress',
                    'metadata': {
                        **dict(record.metadata or {}),
                        ACCEPTANCE_HANDSHAKE_KEY: {'state': 'idle', 'latest_execution_result_ref': ''},
                    },
                }
            ),
        )
        await _scanner(service, heartbeat).scan_once()
        assert heartbeat.calls == []
    finally:
        await service.close()


def test_prompt_lane_renders_acceptance_role_and_inspected_node() -> None:
    lines = _task_node_error_lines(
        {
            'task_id': TASK,
            'task_title': 'task title',
            'node_id': ACCEPTANCE,
            'node_title': 'acceptance',
            'pause_reason': 'error',
            'error_text': 'provider chain exhausted',
            'node_role': 'acceptance',
            'inspected_node_id': CHILD,
            'inspected_node_status': 'in_progress',
            'handshake_state': 'waiting_acceptance',
            'final_acceptance': False,
        },
        [],
        output_inline_limit=4000,
        externalizer=None,
    )
    text = '\n'.join(lines)
    assert 'Role: acceptance node' in text
    assert 'not a verdict on the inspected node' in text
    assert f'Inspected node: {CHILD} [in_progress], handshake=waiting_acceptance' in text
    assert 'Final acceptance: no' in text
    assert 'Do not fail the verifier node itself' in text


@pytest.mark.asyncio
async def test_fail_on_acceptance_node_is_rejected_with_a_real_exit_hint(tmp_path: Path) -> None:
    service = _service(tmp_path)
    try:
        _seed(service)
        result = await service._apply_fail_node_command(
            TASK, node_ids=[ACCEPTANCE], reason='give up'
        )
        item = result['items'][0]
        assert item['result'] == 'conflict'
        assert item['reason'] == 'acceptance_fail_does_not_settle_task'
        assert item['inspected_node_id'] == CHILD
        assert 'fail the inspected execution node' in item['hint']
        latest = service.get_node(ACCEPTANCE)
        assert latest is not None and latest.status == 'in_progress'
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_batch_fail_on_unpaused_acceptance_node_answers_the_acceptance_guard(
    tmp_path: Path,
) -> None:
    """批次形态的入口判据：未暂停的验收节点 `fail` 得先撞上验收守卫。

    车道入口（`control_nodes` + targets）的前置校验原先按 `node_not_paused` 整批打回，
    模型读到的是"先把它暂停"，于是真会去暂停一颗永远不该被 fail 的节点，再撞第二次拒绝。
    原子语义不变（整批不生效、items 为空），换的是错误码与 hint 的可行动性。
    """
    service = _service(tmp_path)
    service._assert_worker_available = lambda: None
    try:
        _seed(service)
        # 验收节点回到未暂停（模型还没处置过它），另备一颗已暂停的执行节点作同批对照。
        service.store.upsert_node(
            _node(
                ACCEPTANCE,
                parent=CHILD,
                kind='acceptance',
                depth=2,
                metadata={'accepted_node_id': CHILD, 'final_acceptance': False},
            )
        )
        service.store.upsert_node(
            _node('node:paused-exec', parent=ROOT, depth=1, is_paused=True)
        )

        result = await service.control_nodes(
            TASK,
            [],
            '',
            targets=[
                {'node_id': ACCEPTANCE, 'action': 'fail'},
                {'node_id': 'node:paused-exec', 'action': 'fail'},
            ],
            remark='batch give up',
        )

        assert result['ok'] is False
        assert result['error'] == 'acceptance_fail_does_not_settle_task'
        assert result['node_id'] == ACCEPTANCE
        assert result['inspected_node_id'] == CHILD
        assert 'fail the inspected execution node' in str(result['hint'])
        # 原子性：整批没有生效，同批那颗已暂停的执行节点照旧 in_progress
        assert result['items'] == []
        for node_id in (ACCEPTANCE, 'node:paused-exec'):
            node = service.get_node(node_id)
            assert node is not None and node.status == 'in_progress'
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_node_ids_fail_on_unpaused_acceptance_node_answers_the_acceptance_guard(
    tmp_path: Path,
) -> None:
    """单动作批次形态（`node_ids`）同样先撞验收守卫，而不是先被告知"没暂停"。

    实盘字节是 `conflict / node_not_paused`：这条回包不带任何出口指引，模型读到就
    会去暂停一颗永远不该被 fail 的验收器；targets 形态已经改成先答验收守卫，两条形态
    对同一个动作必须给同一个理由（形状仍按各自语义：targets 整批原子打回，node_ids 逐节点 conflict）。
    """
    service = _service(tmp_path)
    service._assert_worker_available = lambda: None
    try:
        _seed(service)
        # 现场 = 实盘 C2：验收器被恢复后正在核验（暂停位已清、暂停行已退休）。
        service.store.update_node(
            ACCEPTANCE,
            lambda record: record.model_copy(
                update={'is_paused': False, 'pause_requested': False, 'pause_reason': ''}
            ),
        )
        service.store.delete_task_node_pause(ACCEPTANCE)
        acceptance = service.get_node(ACCEPTANCE)
        assert acceptance is not None and acceptance.is_paused is False
        result = await service.control_nodes(TASK, [ACCEPTANCE], 'fail', remark='give up')
        item = result['items'][0]
        assert item['result'] == 'conflict'
        assert item['reason'] == 'acceptance_fail_does_not_settle_task'
        assert item['inspected_node_id'] == CHILD
        assert 'fail the inspected execution node' in item['hint']
        latest = service.get_node(ACCEPTANCE)
        assert latest is not None and latest.status == 'in_progress'
    finally:
        await service.close()
def _flat(text: str) -> str:
    return " ".join(text.split())


def _acceptance_event_text(*, final_acceptance: bool) -> str:
    lines = _task_node_error_lines(
        {
            "task_id": TASK,
            "task_title": "task title",
            "node_id": ACCEPTANCE,
            "node_title": "acceptance",
            "pause_reason": "error",
            "error_text": "provider chain exhausted",
            "node_role": "acceptance",
            "inspected_node_id": CHILD,
            "inspected_node_status": "in_progress",
            "handshake_state": "waiting_acceptance",
            "final_acceptance": final_acceptance,
        },
        [],
        output_inline_limit=4000,
        externalizer=None,
    )
    return '\n'.join(lines)


@pytest.mark.asyncio
async def test_verifier_exits_are_stated_once_per_tier(tmp_path: Path) -> None:
    """出口清单只有一份事实源：回执 hint、事件束那行、工具契约文案按同一措辞分档。

    这条守卫的措辞以前抄在四处，改一处就留下三份真相（实盘见过 hint 只剩
    "判被验节点失败"、工具契约还写着"或取消任务"）。现在两处生成串都调
    `verifier_recovery_exits`，工具契约与心跳规则由本用例钉住同一份清单：
    子验收节点没有"取消任务"这一档，根最终验收节点必须有。
    """
    child_clause = verifier_recovery_exits(final_acceptance=False)
    final_clause = verifier_recovery_exits(final_acceptance=True)
    assert "cancel" not in child_clause
    assert "cancel the task" in final_clause

    service = _service(tmp_path)
    service._assert_worker_available = lambda: None
    try:
        _seed(service)
        child_result = await service.control_nodes(TASK, [ACCEPTANCE], "fail", remark="give up")
        child_hint = child_result["items"][0]["hint"]
        assert child_clause in child_hint
        assert "cancel the task" not in child_hint

        service.store.update_node(
            ACCEPTANCE,
            lambda record: record.model_copy(
                update={"metadata": {"accepted_node_id": ROOT, "final_acceptance": True}}
            ),
        )
        final_result = await service.control_nodes(TASK, [ACCEPTANCE], "fail", remark="give up")
        final_hint = final_result["items"][0]["hint"]
        assert final_clause in final_hint
        assert "cancel the task" in final_hint
    finally:
        await service.close()

    assert child_clause in _acceptance_event_text(final_acceptance=False)
    assert final_clause in _acceptance_event_text(final_acceptance=True)

    skill = _flat((Path(__file__).resolve().parents[1] / "tools" / "manage_task_nodes_cn" / "toolskills" / "SKILL.md").read_text(encoding="utf-8"))
    assert child_clause in skill
    assert final_clause in skill

    rules = (Path(__file__).resolve().parents[1] / "g3ku" / "runtime" / "prompts" / "heartbeat_rules.md").read_text(encoding="utf-8")
    assert "子验收节点" not in rules
    assert "这条事件自带的出口那一行" in rules
