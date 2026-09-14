"""任务大厅列表窄读路径等价性单测。

覆盖：SQLiteTaskStore.list_task_summaries 以 json_extract 提取摘要字段；
TaskQueryService.get_tasks 窄读路径与整包 TaskRecord 解析路径产出一致的
TaskListItem（含 metadata 派生字段、token_usage、磁盘占用与查询范围过滤）；
窄读不可用时回退整包解析；摘要字段缺失时对齐 TaskRecord 默认值。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from main.models import TaskRecord, TokenUsageSummary
from main.service.runtime_service import MainRuntimeService

REPO_ROOT = Path(__file__).resolve().parents[2]


class _DummyChatBackend:
    async def chat(self, **kwargs):
        return SimpleNamespace(content='', tool_calls=[], finish_reason='stop', usage={})


def _make_web_service(tmp_path) -> MainRuntimeService:
    return MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        workspace_root=tmp_path,
        store_path=tmp_path / "runtime.sqlite3",
        files_base_dir=tmp_path / "tasks",
        artifact_dir=tmp_path / "artifacts",
        governance_store_path=tmp_path / "governance.sqlite3",
        execution_mode="web",
    )


def _rich_task(task_id: str, session_id: str, status: str, **overrides) -> TaskRecord:
    base = dict(
        task_id=task_id,
        session_id=session_id,
        title=f'title-{task_id}',
        brief_text=f'brief-{task_id}',
        user_request='request ' * 200,  # 模拟大字段，窄读路径必须绕开它
        status=status,
        root_node_id='node-root',
        max_depth=3,
        created_at='2026-09-13T10:00:00+08:00',
        updated_at=f'2026-09-14T10:00:0{len(task_id) % 10}+08:00',
        final_output='output ' * 200,
        token_usage=TokenUsageSummary(tracked=True, input_tokens=111, output_tokens=222, cache_hit_tokens=33, call_count=4),
        metadata={
            'failure_class': 'engine_failure',
            'final_acceptance': {'required': True, 'status': 'passed', 'node_id': 'node-acc'},
            'retry_history': [{'attempt': 1}, {'attempt': 2}],
            'recovery_notice': '已回退到稳定步骤',
        },
        is_paused=True,
        is_unread=True,
    )
    base.update(overrides)
    return TaskRecord(**base)


def _seed(service: MainRuntimeService) -> None:
    service.store.upsert_task(_rich_task('task:a1', 'web:one', 'in_progress'))
    service.store.upsert_task(_rich_task('task:a2', 'web:one', 'failed', is_unread=False, is_paused=False))
    service.store.upsert_task(_rich_task('task:b1', 'web:two', 'success', is_paused=False))
    service.store.upsert_task(_rich_task('task:b2', 'web:two', 'in_progress', metadata={}, token_usage=TokenUsageSummary()))
    service.store.upsert_task_disk_usage('task:a1', 12345)


def _full_path_items(service: MainRuntimeService, session_id) -> list:
    """旧口径基准：整包 TaskRecord 解析 + 同样的磁盘占用附加。"""
    items = [service.query_service._task_list_item_from_record(task) for task in service.store.list_tasks(session_id)]
    usages = service.store.get_task_disk_usages([item.task_id for item in items])
    for item in items:
        item.disk_usage_bytes = int(usages.get(item.task_id, 0) or 0)
    return items


def _assert_same_items(actual: list, expected: list) -> None:
    assert [item.model_dump(mode='json') for item in actual] == [item.model_dump(mode='json') for item in expected]


def test_narrow_read_matches_full_parse_global(tmp_path) -> None:
    service = _make_web_service(tmp_path)
    _seed(service)

    narrow = service.query_service.get_tasks(None, 1)
    assert _assert_same_items(narrow, _full_path_items(service, None)) is None
    by_id = {item.task_id: item for item in narrow}
    assert by_id['task:a1'].disk_usage_bytes == 12345
    assert by_id['task:a1'].retry_count == 2
    assert by_id['task:a1'].failure_class == 'engine_failure'
    assert by_id['task:a1'].recovery_notice == '已回退到稳定步骤'
    assert by_id['task:a1'].final_acceptance.get('status') == 'passed'
    assert by_id['task:a1'].token_usage.input_tokens == 111
    assert by_id['task:a1'].max_depth == 3


def test_narrow_read_matches_full_parse_session_and_scopes(tmp_path) -> None:
    service = _make_web_service(tmp_path)
    _seed(service)

    assert _assert_same_items(
        service.query_service.get_tasks('web:one', 1),
        _full_path_items(service, 'web:one'),
    ) is None
    # 查询范围过滤口径与整包路径一致。
    assert _assert_same_items(
        service.query_service.get_tasks(None, 2),
        [item for item in _full_path_items(service, None) if item.status == 'in_progress'],
    ) is None
    assert _assert_same_items(
        service.query_service.get_tasks(None, 3),
        [item for item in _full_path_items(service, None) if item.status == 'failed'],
    ) is None
    assert _assert_same_items(
        service.query_service.get_tasks(None, 4),
        [item for item in _full_path_items(service, None) if item.is_unread],
    ) is None


def test_narrow_read_falls_back_when_unavailable(tmp_path) -> None:
    service = _make_web_service(tmp_path)
    _seed(service)

    def _broken(*args, **kwargs):
        raise RuntimeError('json1 unavailable')

    service.store.list_task_summaries = _broken  # type: ignore[method-assign]
    items = service.query_service.get_tasks(None, 1)
    assert {item.task_id for item in items} == {'task:a1', 'task:a2', 'task:b1', 'task:b2'}
    assert any(item.disk_usage_bytes == 12345 for item in items)


def test_summary_defaults_align_with_task_record(tmp_path) -> None:
    service = _make_web_service(tmp_path)
    # 摘要字段缺失（NULL）时对齐 TaskRecord 默认：is_unread=True、其余空值兜底。
    item = service.query_service._task_list_item_from_summary({'task_id': 'task:x'})
    assert item.is_unread is True
    assert item.is_paused is False
    assert item.status == 'in_progress'
    assert item.title == 'task:x'
    assert item.token_usage.tracked is False


def test_task_read_endpoints_serve_via_rest_api(tmp_path, monkeypatch) -> None:
    """rest 读接口经 asyncio.to_thread 卸载后仍保持响应契约。

    覆盖 `GET /api/tasks`（窄读列表 + worker 状态合并）、
    `GET /api/tasks/{task_id}?mark_read=true`（mark_read 留在事件循环、
    快照构建卸载——关键字参数回归防线）与 `GET /api/tasks/worker-status`。
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from main.api.rest import router as rest_router

    service = _make_web_service(tmp_path)
    _seed(service)

    app = FastAPI()
    app.include_router(rest_router, prefix="/api")
    monkeypatch.setattr("main.api.rest.get_agent", lambda: SimpleNamespace(main_task_service=service))
    client = TestClient(app)

    list_resp = client.get("/api/tasks", params={"session_id": "all", "scope": 1})
    assert list_resp.status_code == 200
    list_payload = list_resp.json()
    assert {item["task_id"] for item in list_payload["items"]} == {"task:a1", "task:a2", "task:b1", "task:b2"}
    assert list_payload["worker_state"]  # worker_status_payload 合并在响应里
    a1 = next(item for item in list_payload["items"] if item["task_id"] == "task:a1")
    assert a1["disk_usage_bytes"] == 12345

    detail_resp = client.get("/api/tasks/task:a1", params={"mark_read": "true"})
    assert detail_resp.status_code == 200
    assert detail_resp.json()["task"]["task_id"] == "task:a1"
    assert service.store.get_task("task:a1").is_unread is False

    status_resp = client.get("/api/tasks/worker-status")
    assert status_resp.status_code == 200
    assert "worker_state" in status_resp.json()

    snapshot_resp = client.get("/api/tasks/task:a1/tree-snapshot")
    assert snapshot_resp.status_code == 200
    assert snapshot_resp.json()["task_id"] == "task:a1"
