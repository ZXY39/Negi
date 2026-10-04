"""模型调用明细的按页取数口（Token 统计窗口底栏"点了才加载"）。

契约：
1) 一次只回一页，页内是锚点之下最新的 `size` 条；跨页不重不漏。
2) 这条车道**不带按模型 rollup**——`token_usage_by_model` 读的是 `task_node_details`，
   与明细行无关，实盘一跳 80–160 ms；带上它就把每页 ~8 ms 的窄口变成 150 ms。
3) `anchor` 冻结账本尾部：新调用到达时，同一锚点下的页号与页内容不变（实盘活跃窗口
   2.9 行/分 ⇒ 不锚定则一整页 34 分钟往后漂）。
4) `size` 是闸门：夹到 1..200，不给"顺手拉全量账本"留口子（实盘单任务 49,758 行 / 123.9 MB）。
5) 深页偏移便宜是这条车道成立的前提（实盘 offset=49,458 取 100 行 3.5 ms），这里钉住行为。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from main.api import rest
from main.models import NodeRecord, TaskRecord, TokenUsageSummary
from main.monitoring.file_store import TaskFileStore
from main.monitoring.log_service import TaskLogService
from main.monitoring.query_service import TaskQueryService
from main.storage.sqlite_store import SQLiteTaskStore

TASK_ID = 'task:mcpage'
NODE_ID = 'node:0'


def _build(tmp_path: Path, *, calls: int) -> tuple[SQLiteTaskStore, TaskQueryService]:
    store = SQLiteTaskStore(tmp_path / 'runtime.sqlite3')
    store.upsert_task(TaskRecord(
        task_id=TASK_ID, session_id='web:shared', title='demo', user_request='demo',
        status='in_progress', root_node_id=NODE_ID, max_depth=1,
        created_at='2026-10-01T10:00:00+08:00', updated_at='2026-10-01T10:00:00+08:00',
        token_usage=TokenUsageSummary(tracked=True), metadata={},
    ))
    log_service = TaskLogService(
        store=store, file_store=TaskFileStore(tmp_path / 'files'), registry=None,
        event_history_enabled=False,
    )
    log_service.create_node(TASK_ID, NodeRecord(
        node_id=NODE_ID, task_id=TASK_ID, parent_node_id=None, root_node_id=NODE_ID,
        depth=0, node_kind='execution', status='in_progress', goal='demo', prompt='demo',
        input='x' * 64, output=[], check_result='', final_output='', can_spawn_children=False,
        created_at='2026-10-01T10:00:00+08:00', updated_at='2026-10-01T10:00:00+08:00',
        token_usage=TokenUsageSummary(tracked=True),
    ))
    for index in range(calls):
        store.append_task_model_call(
            task_id=TASK_ID,
            node_id=NODE_ID,
            created_at=f'2026-10-01T10:{index % 60:02d}:{index % 60:02d}+08:00',
            payload={
                'call_index': index,
                'node_id': NODE_ID,
                'prepared_message_chars': 10 * index,
                'delta_usage': {'tracked': True, 'input_tokens': index, 'output_tokens': 1},
                'delta_usage_by_model': [],
            },
        )
    return store, TaskQueryService(store=store, file_store=log_service._file_store, log_service=log_service)


def _append(store: SQLiteTaskStore, index: int) -> None:
    store.append_task_model_call(
        task_id=TASK_ID,
        node_id=NODE_ID,
        created_at=f'2026-10-01T11:{index % 60:02d}:00+08:00',
        payload={'call_index': index, 'node_id': NODE_ID, 'delta_usage': {}, 'delta_usage_by_model': []},
    )


def _indexes(page: dict) -> list[int]:
    return [int(item['call_index']) for item in page['model_calls']]


def test_page_one_is_the_newest_rows_and_carries_no_rollup(tmp_path: Path) -> None:
    _store, query_service = _build(tmp_path, calls=250)

    page = query_service.get_task_model_call_page(TASK_ID, page=1, size=100)

    assert page is not None
    assert page['total_calls'] == 250 and page['total_pages'] == 3
    # 页内按 seq 升序回，第 1 页装的是最新的 100 条，不是账本开头
    assert _indexes(page) == list(range(150, 250))
    # 这条车道的理由：明细之外一样都不带，尤其不带按模型 rollup
    assert 'token_usage_by_model' not in page and 'token_usage' not in page
    assert 'task' not in page and 'recent_model_calls' not in page


def test_pages_partition_the_ledger_without_overlap(tmp_path: Path) -> None:
    _store, query_service = _build(tmp_path, calls=250)

    pages = [query_service.get_task_model_call_page(TASK_ID, page=page, size=100) for page in (1, 2, 3)]

    assert [len(item['model_calls']) for item in pages] == [100, 100, 50]
    flat = [index for item in pages for index in _indexes(item)]
    assert sorted(flat) == list(range(250))
    assert len(set(flat)) == 250


def test_anchor_freezes_the_page_numbering_against_new_calls(tmp_path: Path) -> None:
    store, query_service = _build(tmp_path, calls=250)
    anchor = store.get_task_model_call_max_seq(TASK_ID)

    before = query_service.get_task_model_call_page(TASK_ID, page=2, size=100, anchor_seq=anchor)

    for index in range(250, 260):
        _append(store, index)

    after = query_service.get_task_model_call_page(TASK_ID, page=2, size=100, anchor_seq=anchor)
    unanchored = query_service.get_task_model_call_page(TASK_ID, page=2, size=100)

    assert before is not None and after is not None and unanchored is not None
    # 同一锚点下：页号、页数、页内容逐字不变——用户停在第 2 页不会被新调用挪走
    assert after['anchor_seq'] == anchor
    assert after['total_calls'] == before['total_calls'] == 250
    assert _indexes(after) == _indexes(before)
    # 不锚定时读数会往后漂（页数 3→3、总数 250→260，而末页内容已换）
    assert unanchored['total_calls'] == 260
    assert unanchored['anchor_seq'] == store.get_task_model_call_max_seq(TASK_ID)


def test_size_is_a_gate_and_page_clamps_to_the_last_page(tmp_path: Path) -> None:
    _store, query_service = _build(tmp_path, calls=900)

    capped = query_service.get_task_model_call_page(TASK_ID, page=1, size=100000)
    beyond = query_service.get_task_model_call_page(TASK_ID, page=9999, size=100)

    assert capped is not None and beyond is not None
    assert capped['size'] == 200 and len(capped['model_calls']) == 200
    assert capped['total_pages'] == 5
    # 越界的页夹到该 size 下的末页，而不是回空页
    assert beyond['size'] == 100 and beyond['page'] == 9 and beyond['total_pages'] == 9
    assert len(beyond['model_calls']) == 100
    assert _indexes(beyond) == list(range(0, 100))


def test_missing_task_returns_none(tmp_path: Path) -> None:
    _store, query_service = _build(tmp_path, calls=3)
    assert query_service.get_task_model_call_page('task:nope', page=1, size=100) is None


def test_route_serves_the_page_and_404s_an_unknown_task(tmp_path: Path, monkeypatch) -> None:
    _store, query_service = _build(tmp_path, calls=130)

    class _Service:
        async def startup(self) -> None:
            return None

        def normalize_task_id(self, value: str) -> str:
            return value

        def get_task_model_call_page_payload(self, task_id: str, **kwargs):
            return query_service.get_task_model_call_page(task_id, **kwargs)

    service = _Service()
    monkeypatch.setattr('main.api.rest.get_agent', lambda: SimpleNamespace(main_task_service=service))
    client = TestClient(_build_app())

    response = client.get(f'/api/tasks/{TASK_ID}/model-call-page', params={'page': 2, 'size': 30})
    missing = client.get('/api/tasks/task:none/model-call-page')

    assert response.status_code == 200
    body = response.json()
    assert body['ok'] is True
    assert body['page'] == 2 and body['size'] == 30 and body['total_pages'] == 5
    assert _indexes(body) == list(range(70, 100))
    assert body['anchor_seq'] == 130
    assert missing.status_code == 404


def _build_app() -> FastAPI:
    app = FastAPI()
    app.include_router(rest.router, prefix='/api')
    return app


def test_frontend_wires_the_page_lane_without_polluting_the_window() -> None:
    """车道接线的静态面：实时合流必须在快照态早退，页行不能灌进只涨不缩的窗口数组。

    行为侧的断言在 `tests/resources/org_graph_tasks.model_call_page.test.js`（node --test），
    这里只钉住三条容易被后续改动悄悄拆掉的连线。
    """
    root = Path(rest.__file__).resolve().parents[2]
    tasks_js = (root / 'g3ku/web/frontend/org_graph_tasks.js').read_text(encoding='utf-8')
    app_js = (root / 'g3ku/web/frontend/org_graph_app.js').read_text(encoding='utf-8')
    api_js = (root / 'g3ku/web/frontend/api_client.js').read_text(encoding='utf-8')

    live_branch = tasks_js[tasks_js.index('if (payload.type === "task.model.call")'):]
    live_branch = live_branch[:live_branch.index('if (payload.type === "task.live.patch")')]
    assert 'isTaskModelCallHistorical()' in live_branch, '实时合流没在快照态早退'

    assert 'data-task-model-call-goto' in tasks_js and 'data-task-model-call-jump' in tasks_js
    assert 'data-task-model-call-live' in tasks_js, '快照态缺「回到最新」出口'
    assert 'S.taskModelCallPageRows' in tasks_js
    assert 'if (isTaskModelCallHistorical()) void loadTaskModelCallPage(' in app_js, (
        '快照态下的「刷新」必须重算页号而不是弹回最新'
    )
    assert 'setTaskModelCallsPage(value)' in app_js, '页码跳转输入框没接到取数口'
    assert 'exitTaskModelCallHistory()' in app_js

    assert 'getTaskModelCallPage' in api_js
    assert '/model-call-page' in api_js
    assert 'tasks:model-call-page:' in api_js, '翻页请求要有独立 requestKey，别 abort 掉刷新口'
