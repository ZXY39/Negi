from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from g3ku.core.state import AgentState
from g3ku.runtime.api import ceo_sessions, websocket_ceo
from g3ku.runtime.frontdoor._ceo_create_agent_impl import CreateAgentCeoFrontDoorRunner
from g3ku.runtime.frontdoor._ceo_runtime_ops import FrontdoorTokenPreflightResult
from g3ku.runtime.session_agent import (
    CONTEXT_COMPRESSION_MARKER_KIND,
    RuntimeAgentSession,
)
from g3ku.session.manager import SessionManager


def _build_app() -> FastAPI:
    app = FastAPI()
    app.include_router(ceo_sessions.router, prefix="/api")
    return app


class _MarkerHost:
    """只借用 session_agent 上的压缩区分线/状态快照方法，避免拉起完整回合运行时。"""

    append_context_compression_marker = RuntimeAgentSession.append_context_compression_marker
    _compression_snapshot = RuntimeAgentSession._compression_snapshot
    state_dict = RuntimeAgentSession.state_dict

    def __init__(self, *, loop, state, compression_state=None, stop_reason=""):
        self._loop = loop
        self._state = state
        self._compression_state = dict(compression_state or {})
        self._last_stop_reason = stop_reason


def _agent_state(session_key: str = "web:shared") -> AgentState:
    return AgentState(session_key=session_key)


# ---- 区分线落盘 -------------------------------------------------------------


def test_compression_marker_writes_ui_only_system_row(tmp_path: Path) -> None:
    session_manager = SessionManager(tmp_path)
    transcript = session_manager.get_or_create("web:shared")
    host = _MarkerHost(loop=SimpleNamespace(sessions=session_manager), state=_agent_state())

    assert host.append_context_compression_marker(
        state="completed",
        source="manual",
        stats={"pre_tokens": 30000, "post_tokens": 4000},
    ) is True

    row = transcript.messages[-1]
    assert row["role"] == "system"
    assert row["content"] == "会话已压缩"
    metadata = row["metadata"]
    assert metadata["kind"] == CONTEXT_COMPRESSION_MARKER_KIND
    assert metadata["compression_state"] == "completed"
    assert metadata["source"] == "manual"
    # 模型侧不可见、界面可见：区分线只是历史分界，不能回流进请求体。
    assert metadata["prompt_visible"] is False
    assert metadata["ui_visible"] is True
    assert metadata["stats"] == {"pre_tokens": 30000, "post_tokens": 4000}
    assert session_manager.get_or_create("web:shared").messages[-1]["content"] == "会话已压缩"


def test_compression_marker_rejects_unknown_state(tmp_path: Path) -> None:
    session_manager = SessionManager(tmp_path)
    transcript = session_manager.get_or_create("web:shared")
    host = _MarkerHost(loop=SimpleNamespace(sessions=session_manager), state=_agent_state())

    assert host.append_context_compression_marker(state="running", source="manual") is False
    assert transcript.messages == []


def test_paused_marker_uses_paused_label(tmp_path: Path) -> None:
    session_manager = SessionManager(tmp_path)
    transcript = session_manager.get_or_create("web:shared")
    host = _MarkerHost(loop=SimpleNamespace(sessions=session_manager), state=_agent_state())

    assert host.append_context_compression_marker(state="paused", source="auto") is True
    assert transcript.messages[-1]["content"] == "压缩已暂停"


# ---- 快照下发 ---------------------------------------------------------------


def test_ceo_snapshot_surfaces_compression_marker_and_drops_running() -> None:
    items = websocket_ceo._build_ceo_snapshot(
        [
            {"role": "user", "content": "旧问题"},
            {
                "role": "system",
                "content": "会话已压缩",
                "metadata": {
                    "kind": CONTEXT_COMPRESSION_MARKER_KIND,
                    "compression_state": "completed",
                    "source": "manual",
                    "prompt_visible": False,
                    "ui_visible": True,
                    "stats": {"pre_tokens": 30000, "post_tokens": 4000},
                },
            },
            {
                "role": "system",
                "content": "压缩已暂停",
                "metadata": {
                    "kind": CONTEXT_COMPRESSION_MARKER_KIND,
                    "compression_state": "running",
                    "prompt_visible": False,
                    "ui_visible": True,
                },
            },
            {"role": "assistant", "content": "新回答"},
        ]
    )

    by_content = {item["content"]: item for item in items}
    assert by_content["会话已压缩"]["compression_marker"] == {
        "state": "completed",
        "source": "manual",
        "stats": {"pre_tokens": 30000, "post_tokens": 4000},
    }
    # running 不是终态：实时线由前端状态驱动，持久标记只认 completed / paused。
    assert "compression_marker" not in by_content["压缩已暂停"]
    assert [item["role"] for item in items] == ["user", "system", "system", "assistant"]


def test_state_dict_carries_compression_for_out_of_turn_progress() -> None:
    host = _MarkerHost(
        loop=SimpleNamespace(sessions=None),
        state=_agent_state(),
        compression_state={"status": "running", "text": "上下文压缩中", "source": "manual_context_compression"},
    )

    assert host.state_dict()["compression"]["status"] == "running"

    idle = _MarkerHost(loop=SimpleNamespace(sessions=None), state=_agent_state())
    assert "compression" not in idle.state_dict()


# ---- runner 侧真实压缩 ------------------------------------------------------


def _patch_compression_runner(monkeypatch, runner, *, applied: bool, context_window: int = 200_000):
    calls: dict[str, object] = {}

    async def _fake_prepare(**kwargs):
        calls["user_input"] = kwargs["user_input"]
        return {
            "session_key": "web:shared",
            "model_refs": ["openai:gpt-5.2"],
            "messages": [{"role": "user", "content": "hi"}],
            "parallel_enabled": False,
            "frontdoor_actual_request_history": [],
        }

    monkeypatch.setattr(runner, "_prepare_turn_state", _fake_prepare)
    monkeypatch.setattr(runner, "_build_langchain_tools_for_state", lambda **_: [])
    monkeypatch.setattr(
        runner,
        "_frontdoor_send_preflight_snapshot",
        lambda **_: {
            "request_messages": [{"role": "user", "content": "hi"}],
            "tool_schemas": [],
            "context_window_tokens": context_window,
            "estimated_total_tokens": 120_000,
            "provider_model": "openai:gpt-5.2",
            "prompt_cache_key": "cache-key",
            "prompt_cache_diagnostics": {},
            "model_info": {"context_window_tokens": context_window},
        },
    )

    async def _fake_compress(**kwargs):
        calls["compression_request_messages"] = list(kwargs["request_messages"])
        return FrontdoorTokenPreflightResult(
            request_messages=[{"role": "assistant", "content": "[G3KU_TOKEN_COMPACT_V2]\n摘要"}],
            final_request_tokens=9_000,
            history_shrink_reason="token_compression",
            diagnostics={
                "applied": applied,
                "reason": "" if applied else "no_compressible_history",
                "compression_mode": "llm",
                "compressed_history_message_count": 12,
            },
        )

    monkeypatch.setattr(runner, "_run_frontdoor_llm_token_compression", _fake_compress)

    def _fake_persist(**kwargs):
        calls["persisted_messages"] = list(kwargs["request_messages"])
        calls["persisted_lane"] = kwargs.get("request_lane")
        return {"frontdoor_actual_request_path": "artifact.json"}

    monkeypatch.setattr(runner, "_persist_frontdoor_actual_request", _fake_persist)
    monkeypatch.setattr(
        runner,
        "_build_frontdoor_provider_request_body_preview",
        lambda **kwargs: {"messages": list(kwargs["request_messages"])},
    )
    return calls


def test_compress_session_context_rewrites_durable_baseline(monkeypatch) -> None:
    runner = CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace())
    calls = _patch_compression_runner(monkeypatch, runner, applied=True)
    synced: dict[str, str] = {}
    session = SimpleNamespace(
        state=_agent_state(),
        _frontdoor_history_shrink_reason="",
        _sync_completed_continuity_snapshot=lambda **kwargs: synced.update(kwargs),
    )

    result = asyncio.run(runner.compress_session_context(session=session))

    assert result["applied"] is True
    assert result["pre_tokens"] == 120_000
    assert result["post_tokens"] == 9_000
    assert result["compressed_history_message_count"] == 12
    # 手动压缩没有新消息：预检按空输入重建基线。
    assert calls["user_input"] == {"content": "", "metadata": {}}
    # 摘要结果写回基线，并且工件带上手写压缩的泳道标记。
    assert calls["persisted_messages"] == [{"role": "assistant", "content": "[G3KU_TOKEN_COMPACT_V2]\n摘要"}]
    assert calls["persisted_lane"] == "manual_context_compression"
    assert session._frontdoor_history_shrink_reason == "token_compression"
    assert synced == {"source_reason": "finalize"}


def test_compress_session_context_skips_baseline_when_nothing_to_compress(monkeypatch) -> None:
    runner = CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace())
    calls = _patch_compression_runner(monkeypatch, runner, applied=False)
    synced: dict[str, str] = {}
    session = SimpleNamespace(
        state=_agent_state(),
        _sync_completed_continuity_snapshot=lambda **kwargs: synced.update(kwargs),
    )

    result = asyncio.run(runner.compress_session_context(session=session))

    assert result["applied"] is False
    assert result["reason"] == "no_compressible_history"
    assert "persisted_messages" not in calls
    assert synced == {}


def test_compress_session_context_refuses_model_without_context_window(monkeypatch) -> None:
    runner = CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace())
    calls = _patch_compression_runner(monkeypatch, runner, applied=True, context_window=20_000)
    session = SimpleNamespace(
        state=_agent_state(),
        _sync_completed_continuity_snapshot=lambda **kwargs: None,
    )

    result = asyncio.run(runner.compress_session_context(session=session))

    assert result["applied"] is False
    assert result["reason"] == "missing_context_window"
    assert "compression_request_messages" not in calls


def test_estimate_turn_preflight_reports_baseline_for_empty_draft(monkeypatch) -> None:
    runner = CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace())
    calls = _patch_compression_runner(monkeypatch, runner, applied=False)

    item = asyncio.run(runner.estimate_turn_preflight(user_inputs=[], session=SimpleNamespace(
        state=_agent_state(),
        _history_text=lambda value: str(value),
    )))

    # 空草稿不再回全零：输入框为空时用量表要常驻显示基线占用值。
    assert item["estimated_total_tokens"] == 120_000
    assert item["context_window_tokens"] == 200_000
    assert item["provider_model"] == "openai:gpt-5.2"
    assert calls["user_input"] == {"content": "", "metadata": {}}


# ---- 端点 ------------------------------------------------------------------


class _FakeCompressionRunner:
    def __init__(self, *, block: bool = False):
        self.calls = 0
        self._block = block
        self._started = asyncio.Event()

    async def compress_session_context(self, *, session):
        self.calls += 1
        if self._block:
            self._started.set()
            await asyncio.Event()  # 挂住，由取消端点结束
        return {
            "applied": True,
            "reason": "",
            "pre_tokens": 120_000,
            "post_tokens": 9_000,
            "provider_model": "openai:gpt-5.2",
            "context_window_tokens": 200_000,
            "compression_mode": "llm",
        }


def _install_endpoint_runtime(monkeypatch, tmp_path: Path, *, runner, runtime_session):
    session_manager = SessionManager(tmp_path)
    session = session_manager.get_or_create("web:shared")
    session_manager.save(session)
    runtime_manager = SimpleNamespace(
        get=lambda session_id: runtime_session if session_id == "web:shared" else None,
        get_or_create=lambda **_: runtime_session,
    )
    agent = SimpleNamespace(sessions=session_manager, multi_agent_runner=runner)
    monkeypatch.setattr(
        ceo_sessions,
        "_sessions",
        lambda: (agent, session_manager, runtime_manager, SimpleNamespace()),
    )
    return session_manager


def _idle_runtime_session():
    return SimpleNamespace(
        state=_agent_state(),
        _compression_state={},
        _emit_state_snapshot=lambda: asyncio.sleep(0),
        _sync_completed_continuity_snapshot=lambda **kwargs: None,
        _active_frontdoor_compression_generation=None,
        _cancel_active_frontdoor_compression_generation=lambda: None,
        append_context_compression_marker=lambda **kwargs: True,
    )


def test_compress_context_endpoint_rejects_channel_session(tmp_path: Path, monkeypatch) -> None:
    _install_endpoint_runtime(
        monkeypatch,
        tmp_path,
        runner=_FakeCompressionRunner(),
        runtime_session=_idle_runtime_session(),
    )

    client = TestClient(_build_app())
    response = client.post("/api/ceo/sessions/ext:qq/compress-context")

    assert response.status_code == 409
    assert response.json()["detail"] == "channel_session_readonly"


def test_compress_context_endpoint_runs_and_writes_completed_marker(tmp_path: Path, monkeypatch) -> None:
    runner = _FakeCompressionRunner()
    runtime_session = _idle_runtime_session()
    markers: list[dict] = []
    runtime_session.append_context_compression_marker = lambda **kwargs: markers.append(kwargs) or True
    _install_endpoint_runtime(monkeypatch, tmp_path, runner=runner, runtime_session=runtime_session)

    client = TestClient(_build_app())
    response = client.post("/api/ceo/sessions/web:shared/compress-context")

    assert response.status_code == 200
    payload = response.json()
    assert payload["session_id"] == "web:shared"
    assert runner.calls == 1
    assert payload["status"] in {"running", "completed"}
    assert markers and markers[-1]["state"] == "completed"
    assert markers[-1]["source"] == "manual"
    assert markers[-1]["stats"]["post_tokens"] == 9_000
    # 收尾必须把进度复位，否则区分线一直挂在「进行中」。
    assert runtime_session._compression_state["status"] == ""


def test_compress_context_endpoint_pauses_running_turn_first(tmp_path: Path, monkeypatch) -> None:
    runner = _FakeCompressionRunner()
    runtime_session = _idle_runtime_session()
    runtime_session.state.is_running = True
    paused: list[bool] = []
    runtime_session.pause = lambda **kwargs: paused.append(True) or asyncio.sleep(0)
    _install_endpoint_runtime(monkeypatch, tmp_path, runner=runner, runtime_session=runtime_session)

    client = TestClient(_build_app())
    response = client.post("/api/ceo/sessions/web:shared/compress-context")

    assert response.status_code == 200
    assert paused == [True]


def test_cancel_endpoint_rejects_when_nothing_is_running(tmp_path: Path, monkeypatch) -> None:
    _install_endpoint_runtime(
        monkeypatch,
        tmp_path,
        runner=_FakeCompressionRunner(),
        runtime_session=_idle_runtime_session(),
    )

    client = TestClient(_build_app())
    response = client.post("/api/ceo/sessions/web:shared/compress-context/cancel")

    assert response.status_code == 409
    assert response.json()["detail"] == "compression_not_running"


def test_get_endpoint_reports_idle_when_session_runtime_missing(tmp_path: Path, monkeypatch) -> None:
    _install_endpoint_runtime(
        monkeypatch,
        tmp_path,
        runner=_FakeCompressionRunner(),
        runtime_session=None,
    )

    client = TestClient(_build_app())
    response = client.get("/api/ceo/sessions/web:shared/compress-context")

    assert response.status_code == 200
    assert response.json()["status"] == "idle"
