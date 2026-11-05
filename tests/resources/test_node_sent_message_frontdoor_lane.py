"""根节点发往前门的消息必须出现在节点详情「发出」里。

实盘 `task:b2e5554bfb04` / `node:a70ac9fac293`：`send_message(to="frontdoor")` 成功、
`fdmsg:771a100d674c` 已被会话消费，而这台任务在 `task_node_notifications` 里只有前门→节点
的那一条入站行——根节点→前门走 `deliver_frontdoor_message`，只落 `frontdoor_node_messages`。
发出列表原来只扫通知台账，所以这条发言在界面上没有任何落点。
"""

from main.models import FrontdoorNodeMessage, TaskNodeNotification
from main.monitoring.query_service import TaskQueryService
from main.storage.sqlite_store import SQLiteTaskStore

TASK_ID = 'task:fd-lane'
ROOT_NODE = 'node:root-1'
SESSION_ID = 'ext:qq-official-1903529517:f8a8001865631301'


def _frontdoor(
    message_id: str,
    *,
    source_node_id: str = ROOT_NODE,
    content: str = '进度汇报',
    status: str = 'pending',
    consumed_at: str = '',
    created_at: str = '2026-11-05T23:48:59+08:00',
    task_id: str = TASK_ID,
    session_id: str = SESSION_ID,
) -> FrontdoorNodeMessage:
    return FrontdoorNodeMessage(
        message_id=message_id,
        session_id=session_id,
        task_id=task_id,
        source_node_id=source_node_id,
        content=content,
        status=status,
        created_at=created_at,
        consumed_at=consumed_at,
    )


def _notification(
    notification_id: str,
    *,
    source_node_id: str,
    node_id: str,
    message: str = '给子节点的要求',
    status: str = 'delivered',
    created_at: str = '2026-11-05T23:00:00+08:00',
) -> TaskNodeNotification:
    return TaskNodeNotification(
        notification_id=notification_id,
        task_id=TASK_ID,
        node_id=node_id,
        epoch_id='',
        source_node_id=source_node_id,
        message=message,
        status=status,
        created_at=created_at,
    )


class _NodeRow:
    def __init__(self, node_id: str, title: str = '') -> None:
        self.node_id = node_id
        self.title = title


class _Store:
    def __init__(self, *, notifications, frontdoor) -> None:
        self._notifications = notifications
        self._frontdoor = frontdoor

    def list_task_nodes(self, task_id):
        return [_NodeRow(ROOT_NODE, '根节点'), _NodeRow('node:child-1', '子节点')]

    def list_task_notifications(self, task_id):
        return self._notifications

    def list_frontdoor_node_messages_by_task(self, task_id):
        return self._frontdoor


def _sent_list(*, notifications=None, frontdoor=None) -> list[dict]:
    service = TaskQueryService(
        store=_Store(
            notifications=list(notifications or []),
            frontdoor=list(frontdoor or []),
        ),
        file_store=None,
        log_service=None,
    )
    return service._node_sent_message_list(task_id=TASK_ID, node_id=ROOT_NODE)


def test_store_lists_frontdoor_messages_by_task(tmp_path) -> None:
    store = SQLiteTaskStore(tmp_path / 'runtime.sqlite3')
    store.upsert_frontdoor_node_message(_frontdoor('fdmsg:1'))
    store.upsert_frontdoor_node_message(
        _frontdoor('fdmsg:2', created_at='2026-11-05T23:50:00+08:00')
    )
    store.upsert_frontdoor_node_message(
        _frontdoor('fdmsg:other-task', task_id='task:other', source_node_id='node:other')
    )

    rows = store.list_frontdoor_node_messages_by_task(TASK_ID)

    assert [item.message_id for item in rows] == ['fdmsg:1', 'fdmsg:2']
    assert [item.source_node_id for item in rows] == [ROOT_NODE, ROOT_NODE]


def test_frontdoor_send_lands_in_sent_list_with_session_target() -> None:
    entries = _sent_list(frontdoor=[_frontdoor('fdmsg:1')])

    assert len(entries) == 1
    entry = entries[0]
    assert entry['notification_id'] == 'fdmsg:1'
    assert entry['target_kind'] == 'session'
    assert entry['target_session_id'] == SESSION_ID
    assert entry['target_node_id'] == ''
    assert entry['message'] == '进度汇报'
    assert entry['received_at'] == '2026-11-05T23:48:59+08:00'
    assert entry['status'] == 'pending'


def test_consumed_frontdoor_send_shows_consumed() -> None:
    consumed = _sent_list(
        frontdoor=[_frontdoor('fdmsg:1', status='consumed', consumed_at='2026-11-05T23:49:12+08:00')]
    )
    assert consumed[0]['status'] == 'consumed'
    assert consumed[0]['consumed_at'] == '2026-11-05T23:49:12+08:00'
    # 前门台账没有「并入上下文」这一档，别把会话取走渲染成 merged。
    assert consumed[0]['merged_at'] == ''


def test_sent_list_keeps_node_lane_and_sorts_both_lanes_together() -> None:
    entries = _sent_list(
        notifications=[
            _notification('notif:sent-1', source_node_id=ROOT_NODE, node_id='node:child-1'),
            # 别的节点发出的、以及本节点收到的，都不算「本节点发出」。
            _notification('notif:other-source', source_node_id='node:child-1', node_id=ROOT_NODE),
            _notification('notif:no-source', source_node_id='', node_id=ROOT_NODE),
        ],
        frontdoor=[
            _frontdoor('fdmsg:late', created_at='2026-11-05T23:59:00+08:00'),
            _frontdoor('fdmsg:other-node', source_node_id='node:child-1'),
            _frontdoor('fdmsg:empty', content='   '),
        ],
    )

    assert [item['notification_id'] for item in entries] == ['fdmsg:late', 'notif:sent-1']
    assert entries[0]['target_kind'] == 'session'
    assert entries[1]['target_node_id'] == 'node:child-1'
    assert entries[1]['target_title'] == '子节点'
