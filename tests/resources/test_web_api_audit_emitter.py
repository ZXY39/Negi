"""4d Web API 5xx 审计发射测试：/api/ 门控、审计前缀防自激、源码契约。"""

import json
from pathlib import Path

import pytest

from g3ku import audit_events
from g3ku.web import main as web_main

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _source(path: str) -> str:
    return (_REPO_ROOT / path).read_text(encoding="utf-8")


def _lines(workspace: Path) -> list[dict]:
    audit_file = workspace / ".g3ku" / "audit.jsonl"
    if not audit_file.exists():
        return []
    raw = audit_file.read_text(encoding="utf-8")
    return [json.loads(line) for line in raw.splitlines() if line.strip()]


@pytest.fixture()
def sink(tmp_path: Path):
    workspace = tmp_path / "workspace"
    audit_events.configure_audit_sink(workspace)
    yield workspace
    audit_events.configure_audit_sink(None)


def test_web_api_5xx_emits_event(sink: Path) -> None:
    web_main._emit_web_api_audit_event("/api/tasks", 503)
    lines = _lines(sink)
    assert len(lines) == 1
    record = lines[0]
    assert record["subsystem"] == "web_api"
    assert record["level"] == "error"
    assert record["event_type"] == "web_api_5xx"
    assert record["summary"].startswith("接口返回 503：/api/tasks")
    assert record["detail"]["status_code"] == 503
    assert record["detail"]["path"] == "/api/tasks"


def test_web_api_emitter_skips_audit_prefix_and_non_api(sink: Path) -> None:
    # 审计端点自身失败不回灌（防自激）；静态资源 5xx 不记录
    web_main._emit_web_api_audit_event("/api/audit/events", 500)
    web_main._emit_web_api_audit_event("/index.html", 500)
    web_main._emit_web_api_audit_event("/api", 500)
    assert _lines(sink) == []


def test_web_api_unhandled_exception_note(sink: Path) -> None:
    web_main._emit_web_api_audit_event("/api/tasks/abc", 500, "unhandled exception")
    record = _lines(sink)[0]
    assert record["detail"]["note"] == "unhandled exception"
    assert record["summary"].startswith("接口返回 500：/api/tasks/abc")


def test_web_api_middleware_source_contract() -> None:
    src = _source("g3ku/web/main.py")
    assert '@app.middleware("http")' in src
    assert "async def audit_error_capture_middleware" in src
    assert "_AUDIT_5XX_SKIP_PREFIXES" in src
    assert "_emit_web_api_audit_event(" in src
    assert "response.status_code >= 500" in src
    assert "unhandled exception" in src
