from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from g3ku.runtime.api import ceo_sessions
from g3ku.runtime.frontdoor._ceo_create_agent_impl import CreateAgentCeoFrontDoorRunner
from g3ku.session.manager import SessionManager


def _build_app() -> FastAPI:
    app = FastAPI()
    app.include_router(ceo_sessions.router, prefix="/api")
    return app


def test_frontdoor_send_preflight_snapshot_reports_ratio_and_threshold_flags(
    monkeypatch,
) -> None:
    runner = CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace())

    monkeypatch.setattr(runner, "_build_langchain_tools_for_state", lambda **_: [])
    monkeypatch.setattr(
        runner,
        "_resolve_frontdoor_send_model_context_window",
        lambda **_: {
            "model_key": "ceo_primary",
            "provider_model": "openai:gpt-5.2",
            "context_window_tokens": 32000,
        },
        raising=False,
    )
    monkeypatch.setattr(
        runner,
        "_estimate_frontdoor_send_total_tokens",
        lambda **_: 26000,
        raising=False,
    )
    monkeypatch.setattr(
        runner,
        "_frontdoor_prompt_contract",
        lambda **kwargs: SimpleNamespace(
            request_messages=list(kwargs.get("state", {}).get("frontdoor_live_request_messages") or []),
            prompt_cache_key="cache-key",
            diagnostics={"family": "ok"},
        ),
        raising=False,
    )

    runtime = SimpleNamespace(context=SimpleNamespace(session=SimpleNamespace(state=SimpleNamespace(session_key="web:shared"))))
    preflight = runner._frontdoor_send_preflight_snapshot(
        state={
            "session_key": "web:shared",
            "model_refs": ["ceo_primary"],
            "messages": [{"role": "user", "content": "hello"}],
            "frontdoor_live_request_messages": [{"role": "user", "content": "hello"}],
            "tool_names": [],
            "provider_tool_names": [],
            "parallel_enabled": False,
            "turn_overlay_text": "",
            "dynamic_appendix_messages": [],
        },
        runtime=runtime,
        langchain_tools=[],
    )

    assert preflight["estimated_total_tokens"] == 26000
    assert preflight["context_window_tokens"] == 32000
    assert preflight["trigger_tokens"] == int(32000 * runner._TOKEN_COMPRESSION_TRIGGER_RATIO)
    assert preflight["effective_trigger_tokens"] == int(
        preflight["trigger_tokens"] * runner._TOKEN_COMPRESSION_ESTIMATE_SAFETY_RATIO
    )
    assert preflight["would_trigger_token_compression"] is True
    assert preflight["would_exceed_context_window"] is False
    assert preflight["missing_context_window"] is False
    assert preflight["provider_model"] == "openai:gpt-5.2"


def test_frontdoor_send_preflight_snapshot_uses_safety_margin_near_trigger(
    monkeypatch,
) -> None:
    runner = CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace())

    monkeypatch.setattr(runner, "_build_langchain_tools_for_state", lambda **_: [])
    monkeypatch.setattr(
        runner,
        "_resolve_frontdoor_send_model_context_window",
        lambda **_: {
            "model_key": "ceo_primary",
            "provider_id": "responses",
            "provider_model": "responses:gpt-5.2",
            "resolved_model": "gpt-5.2",
            "context_window_tokens": 25_001,
        },
        raising=False,
    )
    monkeypatch.setattr(
        runner,
        "_estimate_frontdoor_send_total_tokens",
        lambda **_: 19_950,
        raising=False,
    )
    monkeypatch.setattr(
        runner,
        "_frontdoor_prompt_contract",
        lambda **kwargs: SimpleNamespace(
            request_messages=list(kwargs.get("state", {}).get("frontdoor_live_request_messages") or []),
            prompt_cache_key="cache-key",
            diagnostics={"family": "ok"},
        ),
        raising=False,
    )

    runtime = SimpleNamespace(context=SimpleNamespace(session=SimpleNamespace(state=SimpleNamespace(session_key="web:shared"))))
    preflight = runner._frontdoor_send_preflight_snapshot(
        state={
            "session_key": "web:shared",
            "model_refs": ["ceo_primary"],
            "messages": [{"role": "user", "content": "hello"}],
            "frontdoor_live_request_messages": [{"role": "user", "content": "hello"}],
            "tool_names": [],
            "provider_tool_names": [],
            "parallel_enabled": False,
            "turn_overlay_text": "",
            "dynamic_appendix_messages": [],
        },
        runtime=runtime,
        langchain_tools=[],
    )

    assert preflight["trigger_tokens"] == 20_000
    assert preflight["effective_trigger_tokens"] == int(20_000 * runner._TOKEN_COMPRESSION_ESTIMATE_SAFETY_RATIO)
    assert preflight["estimated_total_tokens"] == 19_950
    assert preflight["would_trigger_token_compression"] is True


def test_ceo_composer_preflight_endpoint_returns_runner_estimate(
    tmp_path: Path,
    monkeypatch,
) -> None:
    session_manager = SessionManager(tmp_path)
    session = session_manager.get_or_create("web:shared")
    session_manager.save(session)

    captured: dict[str, object] = {}

    class _Runner:
        async def estimate_turn_preflight(self, *, user_inputs, session):
            captured["user_inputs"] = list(user_inputs)
            captured["session"] = session
            return {
                "estimated_total_tokens": 4096,
                "context_window_tokens": 32000,
                "ratio": 0.128,
                "provider_model": "openai:gpt-5.2",
                "trigger_tokens": 25600,
                "would_trigger_token_compression": False,
                "would_exceed_context_window": False,
                "missing_context_window": False,
            }

    runtime_session = SimpleNamespace(
        state=SimpleNamespace(session_key="web:shared"),
        _channel="web",
        _chat_id="shared",
    )
    runtime_manager = SimpleNamespace(
        get=lambda session_id: runtime_session if session_id == "web:shared" else None,
        get_or_create=lambda **_: runtime_session,
    )
    agent = SimpleNamespace(
        sessions=session_manager,
        multi_agent_runner=_Runner(),
    )

    monkeypatch.setattr(
        ceo_sessions,
        "_sessions",
        lambda: (agent, session_manager, runtime_manager, SimpleNamespace()),
    )

    client = TestClient(_build_app())
    response = client.post(
        "/api/ceo/sessions/web:shared/composer-preflight",
        json={"messages": [{"text": "请估算现在发送会占用多少上下文"}]},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["session_id"] == "web:shared"
    assert payload["item"]["estimated_total_tokens"] == 4096
    assert payload["item"]["context_window_tokens"] == 32000
    assert payload["item"]["provider_model"] == "openai:gpt-5.2"
    assert payload["item"]["ratio"] == 0.128
    assert len(captured["user_inputs"]) == 1
