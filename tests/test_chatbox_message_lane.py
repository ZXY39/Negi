"""chatbox 车道：操作员输入框来信（kind=chatbox_direct）+ 节点回投（reply_to 锚定）。

操作员消息与 user_direct 同一套投递语义（人工来源：无条件唤醒、暂停目标落箱
不清位、等验收目标排队、验收镜像），只多两件事：账本与展示按来源分档，以及
收件节点被 payload.reply_via 引导回投 `send_message(to="chatbox", ...)`——回投
落 `task_chatbox_messages`（reply_to 指向触发它的最近一条 chatbox 来信），网页
在接收列表那条消息行右侧挂「新回复」图标，查看窗口取走即销账。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from main.models import NodeRecord, TaskRecord
from main.runtime.node_mailbox import delivery_defers_wake
from main.runtime.node_message_kind import (
    ACCEPTANCE_MIRROR_KINDS,
    ALWAYS_WAKING_KINDS,
    KIND_CHATBOX_DIRECT,
    ORIGIN_CHATBOX,
    derive_message_kind,
    derive_priority,
)
from main.runtime.node_message_text import (
    notification_reply_via,
    render_node_message_text,
    strip_node_message_prefix,
)
from main.service.runtime_service import MainRuntimeService

TASK = 'task:chatbox'
ROOT = 'node:root'
CHILD = 'node:child'
VERIFIER = 'node:verifier'
SESSION = 'web:chatbox'
STAMP = '2026-11-07T00:00:00+08:00'


def _service(tmp_path: Path, *, execution_mode: str = 'worker') -> MainRuntimeService:
    return MainRuntimeService(
        chat_backend=SimpleNamespace(),
        workspace_root=tmp_path,
        store_path=tmp_path / 'runtime.sqlite3',
        files_base_dir=tmp_path / 'tasks',
        artifact_dir=tmp_path / 'artifacts',
        governance_store_path=tmp_path / 'governance.sqlite3',
        execution_mode=execution_mode,
    )


def _node(node_id, *, parent=None, depth=0, kind='execution', metadata=None):
    return NodeRecord(
        node_id=node_id,
        task_id=TASK,
        parent_node_id=parent,
        root_node_id=ROOT,
        depth=depth,
        node_kind=kind,
        goal=f'goal-{node_id}',
        prompt='p',
        created_at=STAMP,
        updated_at=STAMP,
        metadata=dict(metadata or {}),
    )


def _seed(service: MainRuntimeService) -> None:
    service.store.upsert_task(
        TaskRecord(
            task_id=TASK,
            title='t',
            user_request='u',
            root_node_id=ROOT,
            session_id=SESSION,
            created_at=STAMP,
            updated_at=STAMP,
        )
    )
    service.store.upsert_node(_node(ROOT))
    service.store.upsert_node(_node(CHILD, parent=ROOT, depth=1))
    service.store.upsert_node(
        _node(
            VERIFIER,
            parent=ROOT,
            depth=1,
            kind='acceptance',
            metadata={'accepted_node_id': ROOT},
        )
    )


def test_chatbox_origin_derives_human_kind():
    assert derive_message_kind(origin=ORIGIN_CHATBOX) == KIND_CHATBOX_DIRECT
    assert derive_priority(kind=KIND_CHATBOX_DIRECT) == 'high'
    assert KIND_CHATBOX_DIRECT in ALWAYS_WAKING_KINDS
    assert KIND_CHATBOX_DIRECT in ACCEPTANCE_MIRROR_KINDS


def test_chatbox_origin_is_a_human_sender_for_sticky_pause():
    """人工来源档：暂停目标上落箱不清位，与 user 同档。"""
    assert delivery_defers_wake(target_park_state='paused', sender_origin=ORIGIN_CHATBOX) is True
    assert delivery_defers_wake(target_park_state='paused', sender_origin='node') is False


def test_chatbox_label_renders_and_strips():
    text = render_node_message_text(
        kind=KIND_CHATBOX_DIRECT,
        message='请先停一下',
        reply_via='chatbox',
    )
    assert text.startswith('来自输入框，内容为：\n请先停一下')
    assert '`send_message(to="chatbox", content=...)`' in text
    assert 'operator chat box' in text
    # strip 合同只剥一行头，回投尾句随正文保留。
    assert strip_node_message_prefix(text).startswith('请先停一下')
    assert 'send_message(to="chatbox"' not in strip_node_message_prefix(
        render_node_message_text(kind=KIND_CHATBOX_DIRECT, message='请先停一下')
    )


def test_reply_via_reader_only_accepts_known_lane():
    assert notification_reply_via({'payload': {'reply_via': 'chatbox'}}) == 'chatbox'
    assert notification_reply_via({'payload': {'reply_via': 'frontdoor'}}) == ''
    assert notification_reply_via({'payload': {}}) == ''
    assert notification_reply_via({'payload': None}) == ''


@pytest.mark.asyncio
async def test_input_box_delivery_lands_as_chatbox_direct_with_reply_tail(tmp_path):
    from main.runtime.node_runner import _notification_rendered_text

    service = _service(tmp_path)
    _seed(service)

    result = await service.deliver_node_message(
        task_id=TASK,
        target_node_id=CHILD,
        message='现在卡在哪一步',
        origin='chatbox',
        reply_via='chatbox',
    )

    assert result['status'] == 'delivered'
    assert result['kind'] == KIND_CHATBOX_DIRECT
    row = service.store.list_task_node_notifications(TASK, CHILD)[-1]
    assert row.kind == KIND_CHATBOX_DIRECT
    assert row.priority == 'high'
    assert row.payload.get('reply_via') == 'chatbox'
    text = _notification_rendered_text(row)
    assert text.startswith('来自输入框，内容为：\n现在卡在哪一步')
    assert 'send_message(to="chatbox", content=...)' in text
    # 台账正文一个字不动：尾句只在注入渲染时补。
    assert row.message == '现在卡在哪一步'


@pytest.mark.asyncio
async def test_unknown_reply_via_value_never_reaches_payload(tmp_path):
    service = _service(tmp_path)
    _seed(service)

    await service.deliver_node_message(
        task_id=TASK,
        target_node_id=CHILD,
        message='x',
        origin='chatbox',
        reply_via='frontdoor',
    )
    row = service.store.list_task_node_notifications(TASK, CHILD)[-1]
    assert 'reply_via' not in row.payload


@pytest.mark.asyncio
async def test_chatbox_mail_to_paused_target_stays_unwoken(tmp_path):
    service = _service(tmp_path)
    _seed(service)
    service.store.update_node(CHILD, lambda record: record.model_copy(update={'is_paused': True}))

    result = await service.deliver_node_message(
        task_id=TASK,
        target_node_id=CHILD,
        message='先别跑',
        origin='chatbox',
        reply_via='chatbox',
    )

    assert result['status'] == 'delivered'
    assert result['wake'] == ''
    stored = service.store.list_task_node_notifications(TASK, CHILD)
    assert len(stored) == 1
    assert stored[0].kind == KIND_CHATBOX_DIRECT


@pytest.mark.asyncio
async def test_waiting_acceptance_target_queues_chatbox_mail(tmp_path):
    service = _service(tmp_path)
    _seed(service)
    child = service.store.get_node(CHILD)
    service.store.upsert_node(
        child.model_copy(
            update={
                'metadata': {
                    **dict(child.metadata or {}),
                    'acceptance_handshake': {'state': 'waiting_acceptance'},
                }
            }
        )
    )

    result = await service.deliver_node_message(
        task_id=TASK,
        target_node_id=CHILD,
        message='补一条要求',
        origin='chatbox',
        reply_via='chatbox',
    )

    assert result['status'] == 'delivered'
    assert result['target_state'] == 'waiting_acceptance'


@pytest.mark.asyncio
async def test_node_reply_anchors_to_latest_chatbox_mail(tmp_path):
    service = _service(tmp_path)
    _seed(service)

    await service.deliver_node_message(
        task_id=TASK,
        target_node_id=CHILD,
        message='第一条要求',
        origin='chatbox',
        reply_via='chatbox',
    )
    await service.deliver_node_message(
        task_id=TASK,
        target_node_id=CHILD,
        message='第二条要求',
        origin='chatbox',
        reply_via='chatbox',
    )

    receipt = await service._deliver_node_message_from_node(
        task_id=TASK,
        sender_node_id=CHILD,
        to='chatbox',
        content='回投正文',
    )

    assert receipt['ok'] is True
    assert receipt['results'][0]['status'] == 'delivered'
    assert receipt['results'][0]['target'] == 'chatbox'
    # 回投锚定最近一条 chatbox 来信：接收列表按 reply_to 把图标挂到那条消息行。
    assert receipt['results'][0]['reply_to'] == latest_chatbox_mail_id(service, CHILD)
    rows = service.list_node_chatbox_messages(TASK, CHILD)
    assert len(rows) == 1
    assert rows[0].content == '回投正文'
    assert rows[0].reply_to == receipt['results'][0]['reply_to']
    assert rows[0].status == 'unread'


def latest_chatbox_mail_id(service, node_id):
    """与投递侧同一读序：store 按 created_at, notification_id 升序，最后一行即最新。"""
    rows = [
        item
        for item in service.store.list_task_node_notifications(TASK, node_id)
        if str(item.kind or '').strip() == 'chatbox_direct'
    ]
    return str(rows[-1].notification_id or '').strip() if rows else ''


@pytest.mark.asyncio
async def test_reply_without_operator_mail_has_no_anchor(tmp_path):
    service = _service(tmp_path)
    _seed(service)

    receipt = await service._deliver_node_message_from_node(
        task_id=TASK,
        sender_node_id=CHILD,
        to='chatbox',
        content='主动上报',
    )

    assert receipt['results'][0]['status'] == 'delivered'
    assert receipt['results'][0]['reply_to'] == ''
    rows = service.list_node_chatbox_messages(TASK, CHILD)
    assert len(rows) == 1
    assert rows[0].reply_to == ''


@pytest.mark.asyncio
async def test_chatbox_replies_reader_filters_and_marks_read(tmp_path):
    service = _service(tmp_path)
    _seed(service)

    mail_receipt = await service.deliver_node_message(
        task_id=TASK,
        target_node_id=CHILD,
        message='来自输入框的要求',
        origin='chatbox',
        reply_via='chatbox',
    )
    reply_to = str(mail_receipt['message_id'] or '').strip()
    await service._deliver_node_message_from_node(
        task_id=TASK,
        sender_node_id=CHILD,
        to='chatbox',
        content='回投一',
    )
    await service._deliver_node_message_from_node(
        task_id=TASK,
        sender_node_id=CHILD,
        to='chatbox',
        content='回投二',
    )

    all_replies = service.node_chatbox_replies(TASK, CHILD)
    assert len(all_replies['messages']) == 2
    assert all(item['reply_to'] == reply_to for item in all_replies['messages'])
    assert all_replies['unread_count'] == 2
    # 操作员来信不进回投读数——它本来就在接收列表里。
    assert all(item['message'] != '来自输入框的要求' for item in all_replies['messages'])

    filtered = service.node_chatbox_replies(TASK, CHILD, reply_to=reply_to)
    assert len(filtered['messages']) == 2

    marked = service.mark_node_chatbox_read(TASK, CHILD, reply_to=reply_to)
    assert marked == 2
    assert service.node_chatbox_replies(TASK, CHILD, reply_to=reply_to)['unread_count'] == 0
    assert service.mark_node_chatbox_read(TASK, CHILD, reply_to=reply_to) == 0


@pytest.mark.asyncio
async def test_acceptance_node_cannot_answer_chatbox(tmp_path):
    service = _service(tmp_path)
    _seed(service)

    receipt = await service._deliver_node_message_from_node(
        task_id=TASK,
        sender_node_id=VERIFIER,
        to='chatbox',
        content='x',
    )

    assert receipt['ok'] is False
    assert receipt['results'][0]['reason'] == 'acceptance_node_not_messageable'
    assert service.list_node_chatbox_messages(TASK, VERIFIER) == []


@pytest.mark.asyncio
async def test_chatbox_is_a_listed_legal_target(tmp_path):
    service = _service(tmp_path)
    _seed(service)

    receipt = await service._deliver_node_message_from_node(
        task_id=TASK,
        sender_node_id=CHILD,
        to='node:missing',
        content='x',
    )

    assert receipt['results'][0]['reason'] == 'target_not_in_task'
    assert 'chatbox' in receipt['legal_targets']
    assert 'frontdoor' in receipt['legal_targets']


@pytest.mark.asyncio
async def test_message_list_projections_expose_chatbox_markers(tmp_path):
    service = _service(tmp_path)
    _seed(service)

    await service.deliver_node_message(
        task_id=TASK,
        target_node_id=CHILD,
        message='来自输入框的要求',
        origin='chatbox',
        reply_via='chatbox',
    )
    await service._deliver_node_message_from_node(
        task_id=TASK,
        sender_node_id=CHILD,
        to='chatbox',
        content='回投正文',
    )

    inbox = service.query_service._node_message_list(
        task_id=TASK,
        node_id=CHILD,
        runtime_node=service.store.get_node(CHILD),
    )
    chatbox_entries = [item for item in inbox if item.get('kind') == KIND_CHATBOX_DIRECT]
    assert len(chatbox_entries) == 1
    assert chatbox_entries[0]['message'] == '来自输入框的要求'

    sent = service.query_service._node_sent_message_list(task_id=TASK, node_id=CHILD)
    chatbox_sent = [item for item in sent if item.get('target_kind') == 'chatbox']
    assert len(chatbox_sent) == 1
    assert chatbox_sent[0]['message'] == '回投正文'
    assert chatbox_sent[0]['status'] == 'pending'
