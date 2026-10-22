"""Web 轨道按需回取全量入参的 REST 端点契约（TestClient + stub 运行时 + sidecar）。"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from g3ku.runtime import web_ceo_sessions as wcs
from g3ku.runtime.api import ceo_sessions
from g3ku.session.manager import SessionManager

KEY = "web:tool-args"
LONG_COMMAND = "Get-ChildItem -Path C:\\ab\\h2\\packages -Recurse -Filter package.json | Select-Object -ExpandProperty FullName"


def _build_app() -> FastAPI:
    app = FastAPI()
    app.include_router(ceo_sessions.router, prefix="/api")
    return app




class _RuntimeStub:
    def __init__(self, ledger: dict):
        _ = ledger


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(wcs, "workspace_path", lambda: tmp_path)
    # 连续性 sidecar 按 data_root() 落盘，不钉住就会写进仓库树里。
    monkeypatch.setattr(wcs, "data_root", lambda **_kwargs: tmp_path)
    manager = SessionManager(tmp_path)
    manager.save(manager.get_or_create(KEY))
    holder: dict[str, object | None] = {"session": None}
    runtime_manager = SimpleNamespace(
        get=lambda _key: holder["session"],
        get_or_create=lambda **_kwargs: holder["session"],
        remove=lambda _key: None,
    )
    monkeypatch.setattr(
        ceo_sessions,
        "_sessions",
        lambda: (SimpleNamespace(main_task_service=None), manager, runtime_manager, wcs.WebCeoStateStore(tmp_path)),
    )
    return SimpleNamespace(client=TestClient(_build_app()), key=KEY, holder=holder)


def _get(env, tool_call_id: str):
    return env.client.get(f"/api/ceo/sessions/{env.key}/tool-arguments", params={"tool_call_id": tool_call_id})




def test_endpoint_serves_full_arguments_from_the_turns_ledger(env):
    runtime = _RuntimeStub({})
    runtime._frontdoor_turns = [
        {
            "seq": 1,
            "turn_id": "turn-1",
            "text": "progress",
            "tools": [
                {
                    "tool_call_id": "call-1",
                    "tool_name": "exec",
                    "arguments_text": json.dumps(
                        {"command": LONG_COMMAND, "timeout_seconds": 60},
                        ensure_ascii=False,
                    ),
                }
            ],
        }
    ]
    env.holder["session"] = runtime

    response = _get(env, "call-1")

    assert response.status_code == 200, response.text
    assert json.loads(response.json()["arguments_text"]) == {
        "command": LONG_COMMAND,
        "timeout_seconds": 60,
    }


def test_endpoint_falls_back_to_turns_in_the_completed_continuity_sidecar(env):
    wcs.write_completed_continuity_snapshot(
        env.key,
        {
            "frontdoor_turns": [
                {
                    "seq": 1,
                    "turn_id": "turn-1",
                    "text": "",
                    "tools": [
                        {
                            "tool_call_id": "call-1",
                            "tool_name": "exec",
                            "arguments_text": json.dumps({"command": LONG_COMMAND}),
                        }
                    ],
                }
            ]
        },
    )
    assert env.holder["session"] is None

    response = _get(env, "call-1")

    assert response.status_code == 200, response.text
    assert json.loads(response.json()["arguments_text"])["command"] == LONG_COMMAND




def test_endpoint_reports_not_found_when_no_ledger_holds_the_call(env):
    runtime = _RuntimeStub({})
    runtime._frontdoor_turns = [
        {
            "seq": 1,
            "turn_id": "turn-1",
            "text": "",
            "tools": [{"tool_call_id": "call-other", "tool_name": "exec", "arguments_text": "{}"}],
        }
    ]
    env.holder["session"] = runtime

    response = _get(env, "call-1")

    assert response.status_code == 404
    assert response.json()["detail"] == "tool_arguments_not_found"


def test_endpoint_requires_tool_call_id(env):
    response = env.client.get(f"/api/ceo/sessions/{env.key}/tool-arguments")

    assert response.status_code == 400
    assert response.json()["detail"] == "tool_call_id_required"
