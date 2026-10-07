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


def _ledger(tool_call_id: str = "call-1") -> dict:
    return {
        "stages": [
            {
                "stage_id": "frontdoor-stage-1",
                "stage_index": 1,
                "rounds": [
                    {
                        "round_index": 1,
                        "tools": [
                            {
                                "tool_call_id": tool_call_id,
                                "tool_name": "exec",
                                # 账本这一份没裁：只有投影进转录/帧时才会被 _cap_tool_payload 清空。
                                "arguments": {"command": LONG_COMMAND, "timeout_seconds": 60},
                                "arguments_text": "exec (command=Get-ChildItem -Path C:\\ab\\h2\\packa..., timeout_seconds=60)",
                            }
                        ],
                    }
                ],
            }
        ]
    }


class _RuntimeStub:
    def __init__(self, ledger: dict):
        # 端点按引用读这张账本，不调快照方法（后者是 deepcopy + normalize）。
        self._frontdoor_stage_state = ledger
        self.snapshot_calls = 0

    def _frontdoor_visible_canonical_context_snapshot(self) -> dict:
        self.snapshot_calls += 1
        return self._frontdoor_stage_state


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


def test_endpoint_serves_full_arguments_from_the_resident_ledger(env):
    runtime = _RuntimeStub(_ledger())
    env.holder["session"] = runtime

    response = _get(env, "call-1")

    assert response.status_code == 200, response.text
    body = response.json()
    # 回的是账本里那份原文（不是 48 字提示），带缩进与中文原样输出。
    assert json.loads(body["arguments_text"]) == {"command": LONG_COMMAND, "timeout_seconds": 60}
    assert '"timeout_seconds": 60' in body["arguments_text"]
    assert body["tool_call_id"] == "call-1"
    # 收法本身：账本按引用读，不付 deepcopy + normalize 那一份。
    # （"驻留时一次磁盘都不碰"这条断言不成立也不是这里能收的：`_assert_known_session`
    # 为判断会话可否恢复，每个 web: 请求都会读一次完成态 sidecar。）
    assert runtime.snapshot_calls == 0


def test_endpoint_falls_back_to_the_completed_continuity_sidecar(env):
    # 会话没驻留（重启后没人打开过）：sidecar 里那份同样是未裁的账本。
    wcs.write_completed_continuity_snapshot(env.key, {"frontdoor_stage_state": _ledger()})
    assert env.holder["session"] is None

    response = _get(env, "call-1")

    assert response.status_code == 200, response.text
    assert json.loads(response.json()["arguments_text"])["command"] == LONG_COMMAND


def test_endpoint_reports_not_found_when_no_ledger_holds_the_call(env):
    env.holder["session"] = _RuntimeStub(_ledger("call-other"))

    response = _get(env, "call-1")

    assert response.status_code == 404
    assert response.json()["detail"] == "tool_arguments_not_found"


def test_endpoint_requires_tool_call_id(env):
    response = env.client.get(f"/api/ceo/sessions/{env.key}/tool-arguments")

    assert response.status_code == 400
    assert response.json()["detail"] == "tool_call_id_required"
