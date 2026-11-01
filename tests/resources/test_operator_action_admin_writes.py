"""控制台写入点必须留一条模型侧可见的操作者归因。"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from g3ku.runtime.api import persona_onboarding, sandbox
from g3ku.stickers import admin as sticker_admin
from main.api import admin_rest
from main.governance.models import SkillResourceRecord

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def __call__(self, **kwargs: object) -> None:
        self.calls.append(dict(kwargs))


def _write_pack(root: Path, name: str = "demo") -> None:
    pack = root / name
    for category in ("happy",):
        target = pack / category
        target.mkdir(parents=True, exist_ok=True)
        (target / f"{category}-a.png").write_bytes(PNG)
    (pack / "manifest.json").write_text(
        json.dumps({"theme": "测试包", "theme_source": "human"}, ensure_ascii=False),
        encoding="utf-8",
    )


@pytest.fixture()
def sticker_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    stickers = tmp_path / "stickers"
    stickers.mkdir()
    _write_pack(stickers)
    monkeypatch.setattr(sticker_admin, "stickers_root", lambda: stickers)
    recorder = _Recorder()
    monkeypatch.setattr(admin_rest, "_record_console_action", recorder)
    app = FastAPI()
    app.include_router(admin_rest.router, prefix="/api")
    return TestClient(app), recorder


def test_sticker_disable_records_operator_action(sticker_client) -> None:
    http, recorder = sticker_client

    response = http.post("/api/resources/stickers/packs/demo/enabled", json={"enabled": False})

    assert response.status_code == 200
    assert recorder.calls[-1] == {
        "category": "sticker",
        "audience": "frontdoor",
        "action": "disabled sticker pack `demo`",
    }


def test_sticker_delete_records_operator_action(sticker_client) -> None:
    http, recorder = sticker_client

    response = http.delete("/api/resources/stickers/packs/demo")

    assert response.status_code == 200
    assert recorder.calls[-1]["action"] == "deleted sticker pack `demo` (1 images)"


class _SkillService:
    def __init__(self) -> None:
        self.enabled = False
        self.calls: list[dict[str, object]] = []
        self.recorded: list[dict[str, object]] = []

    def _record(self, enabled: bool) -> SkillResourceRecord:
        return SkillResourceRecord(
            skill_id="demo",
            display_name="Demo",
            description="demo",
            source_path="/skills/demo",
            skill_doc_path="/skills/demo/SKILL.md",
            allowed_roles=["ceo"],
            enabled=enabled,
        )

    def get_skill_resource(self, skill_id: str):
        return self._record(self.enabled) if skill_id == "demo" else None

    def enable_skill(self, skill_id: str, *, session_id: str = "web:shared"):
        self.enabled = True
        return self._record(True)

    def disable_skill(self, skill_id: str, *, session_id: str = "web:shared"):
        self.enabled = False
        return self._record(False)

    def record_operator_action(self, **kwargs: object) -> None:
        self.recorded.append(dict(kwargs))


@pytest.fixture()
def skill_client(monkeypatch: pytest.MonkeyPatch):
    service = _SkillService()
    monkeypatch.setattr(admin_rest, "get_agent", lambda: SimpleNamespace(main_task_service=service))
    app = FastAPI()
    app.include_router(admin_rest.router, prefix="/api")
    return TestClient(app), service


def test_skill_enable_records_operator_action(skill_client) -> None:
    http, service = skill_client

    response = http.post("/api/resources/skills/demo/enable")

    assert response.status_code == 200
    assert service.recorded[-1]["action"] == "enabled skill `demo`"
    assert service.recorded[-1]["audience"] == "all"


def test_skill_disable_records_operator_action(skill_client) -> None:
    http, service = skill_client
    service.enabled = True

    response = http.post("/api/resources/skills/demo/disable")

    assert response.status_code == 200
    assert service.recorded[-1]["action"] == "disabled skill `demo`"


class _ToolService:
    def __init__(self) -> None:
        self.enabled = True
        self.recorded: list[dict[str, object]] = []

    def _record(self, enabled: bool):
        return SimpleNamespace(
            tool_id="exec",
            enabled=enabled,
            allowed_roles=["ceo"],
            metadata={},
            model_dump=lambda mode="json": {"tool_id": "exec", "enabled": enabled},
        )

    def get_tool(self, tool_id: str):
        return self._record(self.enabled) if tool_id == "exec" else None

    def disable_tool(self, tool_id: str, *, session_id: str = "web:shared"):
        self.enabled = False
        return self._record(False)

    def record_operator_action(self, **kwargs: object) -> None:
        self.recorded.append(dict(kwargs))


def test_tool_disable_records_operator_action(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _ToolService()
    monkeypatch.setattr(admin_rest, "get_agent", lambda: SimpleNamespace(main_task_service=service))
    app = FastAPI()
    app.include_router(admin_rest.router, prefix="/api")
    http = TestClient(app)

    response = http.post("/api/resources/tools/exec/disable")

    assert response.status_code == 200
    assert service.recorded[-1]["action"] == "disabled tool `exec`"
    assert service.recorded[-1]["audience"] == "all"


class _MemoryManager:
    def __init__(self) -> None:
        self.requests: list[dict[str, object]] = []

    def memory_line_for_id(self, memory_id: str) -> int:
        return 3 if memory_id == "m1" else 5

    async def enqueue_operator_request(self, **kwargs: object):
        self.requests.append(dict(kwargs))
        return {"status": "queued", "request_id": "req-1"}


@pytest.fixture()
def memory_client(monkeypatch: pytest.MonkeyPatch):
    manager = _MemoryManager()
    recorder = _Recorder()
    monkeypatch.setattr(admin_rest, "_runtime_memory_manager", lambda scope="": manager)
    monkeypatch.setattr(admin_rest, "_record_console_action", recorder)
    monkeypatch.setattr(admin_rest, "_append_memory_admin_audit_event", lambda *args, **kwargs: None)
    app = FastAPI()
    app.include_router(admin_rest.router, prefix="/api")
    return TestClient(app), manager, recorder


def test_memory_update_records_operator_request(memory_client) -> None:
    http, _manager, recorder = memory_client

    response = http.post(
        "/api/memory/current/update",
        json={"memory_id": "m1", "memory_body": "new body"},
    )

    assert response.status_code == 200
    assert recorder.calls[-1] == {
        "category": "memory",
        "audience": "frontdoor",
        "scope": "memory:local",
        "action": "requested an update to long-term memory (line 3)",
    }


def test_memory_delete_records_operator_request(memory_client) -> None:
    http, _manager, recorder = memory_client

    response = http.post("/api/memory/current/delete", json={"memory_ids": ["m1", "m2"]})

    assert response.status_code == 200
    assert recorder.calls[-1]["action"] == (
        "requested deletion of long-term memory (line 3, line 5)"
    )


def test_persona_helper_records_frontdoor_action(monkeypatch: pytest.MonkeyPatch) -> None:
    recorded: list[dict[str, object]] = []

    class _Service:
        def record_operator_action(self, **kwargs: object) -> None:
            recorded.append(dict(kwargs))

    monkeypatch.setattr(
        "g3ku.shells.web.get_agent",
        lambda: SimpleNamespace(main_task_service=_Service()),
    )

    persona_onboarding._record_persona_operator_action()

    assert recorded == [
        {
            "category": "persona",
            "audience": "frontdoor",
            "scope": "",
            "action": "updated the persona",
        }
    ]


def test_sandbox_helper_records_session_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    recorded: list[dict[str, object]] = []

    class _Service:
        def record_operator_action(self, **kwargs: object) -> None:
            recorded.append(dict(kwargs))

    monkeypatch.setattr(
        "g3ku.shells.web.get_agent",
        lambda: SimpleNamespace(main_task_service=_Service()),
    )

    sandbox._record_session_operator_action(
        session_key="web:abc",
        action="switched this session to `limited` tier",
    )

    assert recorded == [
        {
            "category": "session",
            "audience": "frontdoor",
            "scope": "session:web:abc",
            "action": "switched this session to `limited` tier",
        }
    ]
