"""验收镜像行：发给等验收执行节点的人工/前门消息同步进验收节点信箱。

第五物化点（镜像行落箱）钉住的合同：

1. 目标握手为 `waiting_acceptance` 且 kind 属于 ACCEPTANCE_MIRROR_KINDS 时，投递
   在验收节点信箱里留一行镜像（`payload.mirrored_from` 指回原行、正文/指纹与原行
   一字不差、不挂回投尾句），运行中的验收回合经在途刷新在下一跳看到它。
2. 节点互发的消息与外部订阅事件不镜像（与尾块、I2 复核同口径）；deduplicated
   的重发不再补镜像。
3. 尾块未读段对已镜像的正文退位（同一段上下文不重复渲染）；I2 pass 复核放行
   已被验收方消费过的镜像内容，仍 delivered 的镜像行照旧扣住 pass。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from main.models import NodeRecord, TaskRecord
from main.runtime.acceptance_handshake import (
    ACCEPTANCE_HANDSHAKE_KEY,
    ACCEPTANCE_STATE_WAITING_ACCEPTANCE,
    set_acceptance_handshake_state,
)
from main.runtime.node_runner import NodeRunner
from main.service.runtime_service import MainRuntimeService

TASK = 'task:mirror'
ROOT = 'node:root'
CHILD = 'node:child'
VERIFIER = 'node:verifier'
STRAY = 'node:stray-verifier'
STAMP = '2026-11-07T15:00:00+08:00'
ANCHOR = '2026-11-07T15:53:00+08:00'
AFTER = '2026-11-07T16:50:59+08:00'
RESULT_REF = 'artifact:payload-1'


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


def _node(node_id: str, *, parent: str | None, kind: str = 'execution', depth: int = 0, metadata: dict | None = None) -> NodeRecord:
    return NodeRecord(
        node_id=node_id,
        task_id=TASK,
        parent_node_id=parent,
        root_node_id=ROOT,
        depth=depth,
        node_kind=kind,
        goal=node_id,
        prompt='p',
        created_at=STAMP,
        updated_at=STAMP,
        metadata=dict(metadata or {}),
    )


def _seed(
    service: MainRuntimeService,
    *,
    handshake_acceptance_id: str = VERIFIER,
) -> None:
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
    service.store.upsert_node(_node(CHILD, parent=ROOT, depth=1))
    service.store.upsert_node(
        _node(VERIFIER, parent=CHILD, kind='acceptance', depth=2, metadata={'accepted_node_id': CHILD})
    )
    # 一颗不受绑定的验收节点：陈旧握手指到它身上时，解析必须放弃它而走反查。
    service.store.upsert_node(
        _node(STRAY, parent=ROOT, kind='acceptance', depth=1, metadata={'accepted_node_id': ROOT})
    )
    handshake = set_acceptance_handshake_state(
        {},
        state=ACCEPTANCE_STATE_WAITING_ACCEPTANCE,
        acceptance_node_id=handshake_acceptance_id,
        rejection_count=0,
        latest_execution_result_ref=RESULT_REF,
        latest_execution_result_summary='child done',
        latest_rejection_feedback_ref='',
        latest_rejection_feedback_summary='',
        updated_at=STAMP,
    )
    service.store.update_node(
        CHILD,
        lambda record: record.model_copy(
            update={'metadata': {**dict(record.metadata or {}), ACCEPTANCE_HANDSHAKE_KEY: handshake}}
        ),
    )
    service.store.update_node(
        VERIFIER,
        lambda record: record.model_copy(
            update={'metadata': {**dict(record.metadata or {}), 'acceptance_tail_built_at': ANCHOR}}
        ),
    )


def _rows(service: MainRuntimeService, node_id: str) -> list[dict]:
    return list(
        service.store.list_task_node_notifications(TASK, node_id) or []
    )


def _upsert_notification(
    service: MainRuntimeService,
    *,
    notification_id: str,
    node_id: str,
    message: str,
    kind: str,
    status: str = 'delivered',
    delivered_at: str = AFTER,
    payload: dict | None = None,
) -> None:
    service.store.upsert_task_node_notification(
        {
            'notification_id': notification_id,
            'task_id': TASK,
            'node_id': node_id,
            'epoch_id': '',
            'source_node_id': ROOT,
            'message': message,
            'status': status,
            'created_at': delivered_at,
            'delivered_at': delivered_at,
            'kind': kind,
            'priority': 'normal',
            'fingerprint': f'fp:{notification_id}',
            'ref': '',
            'submission_id': '',
            'verdict': '',
            'batch_id': '',
            'held_at': '',
            'released_at': '',
            'payload': dict(payload or {}),
        }
    )


class TestMirrorDelivery:
    @pytest.mark.asyncio
    async def test_waiting_acceptance_delivery_mirrors_into_acceptance_mailbox(self, tmp_path):
        service = _service(tmp_path)
        _seed(service)
        receipt = await service.deliver_node_message(
            task_id=TASK,
            target_node_id=CHILD,
            message='Master 要求改掉 20 秒处的文字重叠',
            origin='frontdoor',
            expects_reply=True,
        )
        assert receipt['status'] == 'delivered'
        assert receipt['acceptance_mirror']['target'] == VERIFIER
        assert receipt['acceptance_mirror']['status'] == 'delivered'

        executor_rows = _rows(service, CHILD)
        verifier_rows = _rows(service, VERIFIER)
        assert [str(row.status) for row in executor_rows] == ['delivered']
        assert len(verifier_rows) == 1
        mirror = verifier_rows[0]
        assert str(mirror.status) == 'delivered'
        assert str(mirror.kind) == 'frontdoor_notice'
        assert str(mirror.message) == str(executor_rows[0].message)
        assert str(mirror.fingerprint) == str(executor_rows[0].fingerprint)
        assert mirror.payload.get('mirrored_from') == str(executor_rows[0].notification_id)
        # 回投尾句是给被验节点的车道；镜像行不得携带，否则验收方被误导去回投。
        assert not mirror.payload.get('expects_reply')
        assert not mirror.payload.get('reply_via')

    @pytest.mark.asyncio
    async def test_node_origin_delivery_does_not_mirror(self, tmp_path):
        service = _service(tmp_path)
        _seed(service)
        receipt = await service.deliver_node_message(
            task_id=TASK,
            target_node_id=CHILD,
            message='进展同步',
            origin='node',
            relation='parent',
            source_node_id=ROOT,
        )
        assert receipt['status'] == 'delivered'
        assert 'acceptance_mirror' not in receipt
        assert _rows(service, VERIFIER) == []

    @pytest.mark.asyncio
    async def test_external_event_delivery_does_not_mirror(self, tmp_path):
        service = _service(tmp_path)
        _seed(service)
        receipt = await service.deliver_node_message(
            task_id=TASK,
            target_node_id=CHILD,
            message='上游事件',
            origin='frontdoor',
            external_event_id='evt-1',
        )
        assert receipt['status'] == 'delivered'
        assert 'acceptance_mirror' not in receipt
        assert _rows(service, VERIFIER) == []

    @pytest.mark.asyncio
    async def test_deduplicated_resend_does_not_mirror_twice(self, tmp_path):
        service = _service(tmp_path)
        _seed(service)
        body = 'Master 要求改掉 20 秒处的文字重叠'
        first = await service.deliver_node_message(
            task_id=TASK, target_node_id=CHILD, message=body, origin='frontdoor',
        )
        second = await service.deliver_node_message(
            task_id=TASK, target_node_id=CHILD, message=body, origin='frontdoor',
        )
        assert first['status'] == 'delivered'
        assert second['status'] == 'deduplicated'
        assert len(_rows(service, VERIFIER)) == 1

    @pytest.mark.asyncio
    async def test_stale_handshake_falls_back_to_binding_scan(self, tmp_path):
        service = _service(tmp_path)
        _seed(service, handshake_acceptance_id=STRAY)
        receipt = await service.deliver_node_message(
            task_id=TASK,
            target_node_id=CHILD,
            message='Master 的追加要求',
            origin='user',
        )
        assert receipt['status'] == 'delivered'
        # 陈旧握手点名的 STRAY 没绑定 CHILD；镜像必须落在真正绑定的 VERIFIER 上。
        assert receipt['acceptance_mirror']['target'] == VERIFIER
        assert [str(row.status) for row in _rows(service, STRAY)] == []

    @pytest.mark.asyncio
    async def test_mirror_survives_without_handshake_pointer(self, tmp_path):
        service = _service(tmp_path)
        _seed(service, handshake_acceptance_id='')
        receipt = await service.deliver_node_message(
            task_id=TASK,
            target_node_id=CHILD,
            message='Master 的追加要求',
            origin='frontdoor',
        )
        assert receipt['acceptance_mirror']['target'] == VERIFIER
        assert len(_rows(service, VERIFIER)) == 1


class TestMirrorRenderingAndSettlement:
    def _runner(self, service: MainRuntimeService) -> NodeRunner:
        return NodeRunner(
            store=service.store,
            log_service=SimpleNamespace(update_frame=lambda *a, **k: None),
            react_loop=None,
            tool_provider=lambda node: {},
            execution_model_refs=['execution_model'],
            acceptance_model_refs=['acceptance_model'],
        )

    def _seed_unseen_fixture(self, service: MainRuntimeService) -> str:
        _seed(service)
        _upsert_notification(
            service,
            notification_id='notif:orig-1',
            node_id=CHILD,
            message='修改文字重叠',
            kind='frontdoor_notice',
            delivered_at=AFTER,
        )
        _upsert_notification(
            service,
            notification_id='notif:orig-2',
            node_id=CHILD,
            message='补齐交付清单',
            kind='frontdoor_notice',
            delivered_at=AFTER,
        )
        return 'notif:orig-1'

    def _acceptance_and_execution(self, service: MainRuntimeService):
        return (
            service.store.get_node(VERIFIER),
            service.store.get_node(CHILD),
        )

    def test_tail_unread_section_stands_down_for_mirrored_rows(self, tmp_path):
        service = _service(tmp_path)
        source_id = self._seed_unseen_fixture(service)
        _upsert_notification(
            service,
            notification_id='notif:mirror-1',
            node_id=VERIFIER,
            message='修改文字重叠',
            kind='frontdoor_notice',
            status='delivered',
            payload={'mirrored_from': source_id},
        )
        runner = self._runner(service)
        acceptance, execution = self._acceptance_and_execution(service)
        block = runner._acceptance_notice_block(accepted_node=execution, acceptance=acceptance)
        # 镜像行在场：同一段上下文里不再按被验节点未读行重复渲染这条正文。
        assert '修改文字重叠' not in block
        # 未镜像的另一条仍由未读段兜底。
        assert '补齐交付清单' in block
        # 不传 acceptance（兜底视角）：未读段恢复渲染，行为与既有合同一致。
        block_without = runner._acceptance_notice_block(accepted_node=execution)
        assert '修改文字重叠' in block_without

    def test_pass_recheck_releases_seen_mirror_and_holds_unseen(self, tmp_path):
        service = _service(tmp_path)
        source_id = self._seed_unseen_fixture(service)
        _upsert_notification(
            service,
            notification_id='notif:mirror-1',
            node_id=VERIFIER,
            message='修改文字重叠',
            kind='frontdoor_notice',
            status='consumed',
            payload={'mirrored_from': source_id},
        )
        runner = self._runner(service)
        acceptance, execution = self._acceptance_and_execution(service)
        unseen = runner._unseen_acceptance_notices(
            task_id=TASK, acceptance=acceptance, execution=execution,
        )
        # 已消费的镜像 = 裁决看过：正文放行；没有镜像的那条照旧扣住。
        assert [str(item.notification_id) for item in unseen] == ['notif:orig-2']

    def test_pass_recheck_still_holds_when_mirror_not_yet_consumed(self, tmp_path):
        service = _service(tmp_path)
        source_id = self._seed_unseen_fixture(service)
        _upsert_notification(
            service,
            notification_id='notif:mirror-1',
            node_id=VERIFIER,
            message='修改文字重叠',
            kind='frontdoor_notice',
            status='delivered',
            payload={'mirrored_from': source_id},
        )
        runner = self._runner(service)
        acceptance, execution = self._acceptance_and_execution(service)
        unseen = runner._unseen_acceptance_notices(
            task_id=TASK, acceptance=acceptance, execution=execution,
        )
        # 镜像行还没注入进回合 = 裁决没看过：两条都扣。
        assert sorted(str(item.notification_id) for item in unseen) == ['notif:orig-1', 'notif:orig-2']

    def test_rendered_mirror_notice_carries_positioning_tail(self, tmp_path):
        service = _service(tmp_path)
        self._seed_unseen_fixture(service)
        _upsert_notification(
            service,
            notification_id='notif:mirror-1',
            node_id=VERIFIER,
            message='修改文字重叠',
            kind='frontdoor_notice',
            payload={'mirrored_from': 'notif:orig-1'},
        )
        mirror = _rows(service, VERIFIER)[0]
        from main.runtime.node_runner import _notification_rendered_text

        rendered = _notification_rendered_text(mirror)
        assert '修改文字重叠' in rendered
        assert 'inspected execution node' in rendered
        # 台账正文保持原样：尾句只活在渲染层。
        assert str(mirror.message) == '修改文字重叠'
