"""日志审计 REST 端点测试（GET /api/audit/events、GET /api/audit/summary）。"""

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from g3ku import audit_events
from main.api import admin_rest


@pytest.fixture()
def client(tmp_path: Path):
    """配置 tmp 审计池 + 挂载 admin_rest 路由，测试后复位审计池。"""
    workspace = tmp_path / "workspace"
    audit_events.configure_audit_sink(workspace)
    app = FastAPI()
    app.include_router(admin_rest.router, prefix="/api")
    yield workspace, TestClient(app)
    audit_events.configure_audit_sink(None)


@pytest.fixture()
def stamps() -> list[str]:
    """三个相对真实时钟、互不相同的 ISO 时间戳（emit 顺序对应时间顺序）。"""

    def std(since: datetime) -> str:
        return since.isoformat(timespec="seconds")

    now = datetime.now().astimezone()
    return [
        std(now - timedelta(seconds=9)),
        std(now - timedelta(seconds=6)),
        std(now - timedelta(seconds=3)),
    ]


def _make_stamp_sequence(stamps: list[str]):
    """把给定时间戳依次喂给 _now_iso；序列耗尽后回落到真实 _now_iso。"""
    remaining = list(stamps)
    real_now = audit_events._now_iso

    def stamp():
        if remaining:
            return remaining.pop(0)
        return real_now()

    return stamp


def test_events_endpoint_empty_sink(client) -> None:
    _workspace, test_client = client
    response = test_client.get("/api/audit/events")
    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["items"] == []
    assert payload["total"] == 0
    assert payload["has_more"] is False


def test_events_endpoint_lists_newest_first_with_filters(client, stamps, monkeypatch) -> None:
    _workspace, test_client = client
    monkeypatch.setattr(audit_events, "_now_iso", _make_stamp_sequence(stamps))
    audit_events.emit_audit_event("provider", "error", "a", "p-err")
    audit_events.emit_audit_event("task", "warning", "b", "t-warn")
    audit_events.emit_audit_event("provider", "info", "c", "p-info")

    # 新在前
    listing = test_client.get("/api/audit/events").json()
    assert [item["summary"] for item in listing["items"]] == ["p-info", "t-warn", "p-err"]
    assert listing["total"] == 3

    # level 精确过滤
    errors = test_client.get("/api/audit/events", params={"level": "error"}).json()
    assert errors["total"] == 1
    assert errors["items"][0]["summary"] == "p-err"

    # since 严格大于：since=中间事件时间戳 → 只有最新一条（角标未读契约）
    middle_stamp = stamps[1]
    newer = test_client.get("/api/audit/events", params={"since": middle_stamp}).json()
    assert newer["total"] == 1
    assert newer["items"][0]["summary"] == "p-info"

    # 分页
    page = test_client.get("/api/audit/events", params={"limit": 2, "offset": 0}).json()
    assert page["total"] == 3
    assert page["has_more"] is True
    assert len(page["items"]) == 2


def test_events_endpoint_rejects_bad_limits(client) -> None:
    _workspace, test_client = client
    assert test_client.get("/api/audit/events", params={"limit": 0}).status_code == 422
    assert test_client.get("/api/audit/events", params={"limit": 201}).status_code == 422


def test_events_endpoint_503_contract(client, monkeypatch) -> None:
    _workspace, test_client = client
    failing = lambda **kwargs: (_ for _ in ()).throw(RuntimeError("boom"))  # noqa: E731
    monkeypatch.setattr(audit_events, "list_audit_events", failing)
    response = test_client.get("/api/audit/events")
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "audit_events_read_failed"


def test_audit_events_on_disk_are_valid_jsonl(client) -> None:
    workspace, _test_client = client
    audit_events.emit_audit_event(
        "task", "error", "task_node_error", "节点错误", detail={"node_id": "n1"}
    )
    audit_file = workspace / ".g3ku" / "audit.jsonl"
    lines = [
        json.loads(line)
        for line in audit_file.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(lines) == 1
    assert lines[0]["detail"] == {"node_id": "n1"}
