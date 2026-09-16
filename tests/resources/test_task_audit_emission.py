"""4b/4c 任务审计发射测试：节点错误与任务失败终态（success 不回灌审计流）。"""

import json
from pathlib import Path

import pytest

from g3ku import audit_events
from main.models import TaskRecord
from main.monitoring.log_service import TaskLogService


class _StubStore:
    def __init__(self):
        self.calls: list[dict] = []

    def get_node(self, node_id: str):
        _ = node_id
        return None

    def append_task_error_log(self, **kwargs):
        self.calls.append(kwargs)
        return kwargs


@pytest.fixture()
def sink(tmp_path: Path):
    workspace = tmp_path / "workspace"
    audit_events.configure_audit_sink(workspace)
    yield workspace
    audit_events.configure_audit_sink(None)


def _lines(workspace: Path) -> list[dict]:
    audit_file = workspace / ".g3ku" / "audit.jsonl"
    if not audit_file.exists():
        return []
    raw = audit_file.read_text(encoding="utf-8")
    return [json.loads(line) for line in raw.splitlines() if line.strip()]


def _bare_service() -> TaskLogService:
    # 绕过重量级 __init__（store/file_store 全部跳过），只测发射点在写库后的行为
    service = TaskLogService.__new__(TaskLogService)
    service._store = _StubStore()
    return service


def test_append_task_error_log_emits_audit_event(sink: Path) -> None:
    service = _bare_service()
    service.append_task_error_log("task-1", "node-1", error_text="上游超时", node_title="执行节点")

    lines = _lines(sink)
    assert len(lines) == 1
    record = lines[0]
    assert record["subsystem"] == "task"
    assert record["level"] == "error"
    assert record["event_type"] == "task_node_error"
    assert record["summary"].startswith("任务节点错误：执行节点")
    assert record["detail"]["task_id"] == "task-1"
    assert record["detail"]["node_id"] == "node-1"
    assert record["detail"]["error_text"] == "上游超时"


def test_failed_task_terminal_emits_audit_event(sink: Path) -> None:
    service = _bare_service()
    service._task_summary_payload = lambda task, **kw: {}
    service._append_task_event = lambda **kw: 0
    service._dispatch_live_event_locked = lambda **kw: None

    task = TaskRecord(
        task_id="task-42",
        title="失败的任务",
        user_request="",
        root_node_id="root-1",
        created_at="2026-09-17T00:00:00+08:00",
        updated_at="2026-09-17T00:05:00+08:00",
        status="failed",
        failure_reason="验收未通过",
        metadata={"failure_class": "business_unpassed"},
    )
    service._publish_task_terminal_locked(task=task)

    lines = _lines(sink)
    assert len(lines) == 1
    record = lines[0]
    assert record["subsystem"] == "task"
    assert record["event_type"] == "task_terminal_failed"
    assert record["summary"].startswith("任务失败：失败的任务")
    assert record["detail"]["task_id"] == "task-42"
    assert record["detail"]["failure_reason"] == "验收未通过"
    assert record["detail"]["failure_class"] == "business_unpassed"
    assert record["detail"]["session_id"]


def test_success_task_terminal_emits_nothing(sink: Path) -> None:
    service = _bare_service()
    service._task_summary_payload = lambda task, **kw: {}
    service._append_task_event = lambda **kw: 0
    service._dispatch_live_event_locked = lambda **kw: None

    task = TaskRecord(
        task_id="task-43",
        title="成功的任务",
        user_request="",
        root_node_id="root-1",
        created_at="2026-09-17T00:00:00+08:00",
        updated_at="2026-09-17T00:05:00+08:00",
        status="success",
    )
    service._publish_task_terminal_locked(task=task)

    assert _lines(sink) == []
