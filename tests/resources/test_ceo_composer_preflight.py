from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
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

    monkeypatch.setattr(runner, "_frontdoor_tool_schemas_for_state", lambda **_: [])
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
        tool_schemas=[],
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

    monkeypatch.setattr(runner, "_frontdoor_tool_schemas_for_state", lambda **_: [])
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
        tool_schemas=[],
    )

    assert preflight["trigger_tokens"] == 20_000
    assert preflight["effective_trigger_tokens"] == int(20_000 * runner._TOKEN_COMPRESSION_ESTIMATE_SAFETY_RATIO)
    assert preflight["estimated_total_tokens"] == 19_950
    assert preflight["would_trigger_token_compression"] is True


def test_frontdoor_send_preflight_snapshot_can_skip_consuming_runtime_notices(
    monkeypatch,
) -> None:
    runner = CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace())

    def _unexpected(*_args, **_kwargs):
        raise AssertionError("dry-run preflight must not consume runtime notices")

    monkeypatch.setattr(runner, "_frontdoor_tool_schemas_for_state", lambda **_: [])
    monkeypatch.setattr(runner, "_apply_frontdoor_skill_roster_notice", _unexpected)
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
        lambda **_: 1000,
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
        tool_schemas=[],
        deliver_runtime_notices=False,
    )

    assert preflight["request_messages"] == [{"role": "user", "content": "hello"}]


def test_frontdoor_send_preflight_snapshot_passes_the_session_adopted_persona_digest(
    monkeypatch,
) -> None:
    runner = CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace())
    captured: dict[str, object] = {}

    def _capture(messages, *, session_key, state=None, persona_adopted_digest=""):
        captured["adopted_digest"] = persona_adopted_digest
        return list(messages)

    monkeypatch.setattr(runner, "_frontdoor_tool_schemas_for_state", lambda **_: [])
    monkeypatch.setattr(runner, "_apply_frontdoor_skill_roster_notice", _capture)
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
        lambda **_: 1000,
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

    session = SimpleNamespace(state=SimpleNamespace(session_key="web:shared"))
    setattr(session, "_frontdoor_persona_digest", "digest-from-session")
    runtime = SimpleNamespace(context=SimpleNamespace(session=session))
    runner._frontdoor_send_preflight_snapshot(
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
        tool_schemas=[],
    )

    assert captured["adopted_digest"] == "digest-from-session"


@pytest.mark.asyncio
async def test_build_session_send_preflight_disables_runtime_notice_delivery(
    monkeypatch,
) -> None:
    runner = CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace())
    captured: dict[str, object] = {}

    async def _prepare(*, user_input, runtime_context):
        return {
            "session_key": "web:draft",
            "messages": [{"role": "user", "content": "hello"}],
        }

    def _snapshot(**kwargs):
        captured.update(kwargs)
        return {"request_messages": []}

    monkeypatch.setattr(runner, "_prepare_turn_state", _prepare)
    monkeypatch.setattr(runner, "_frontdoor_tool_schemas_for_state", lambda **_: [])
    monkeypatch.setattr(runner, "_frontdoor_send_preflight_snapshot", _snapshot)

    session = SimpleNamespace(state=SimpleNamespace(session_key="web:draft"))
    await runner.build_session_send_preflight(session, user_input={"content": "hello"})

    assert captured["deliver_runtime_notices"] is False


@pytest.mark.asyncio
async def test_build_session_send_preflight_does_not_advance_notice_cursors(
    monkeypatch,
) -> None:
    from g3ku.agent.persona import persona_digest
    from g3ku.runtime.skill_roster_notice import (
        clear_skill_roster_notice_stores,
        notice_context_for,
    )

    class _Manager:
        def persona_text(self) -> str:
            return "v1"

    runner = CreateAgentCeoFrontDoorRunner(
        loop=SimpleNamespace(memory_manager=_Manager(), main_task_service=None)
    )

    async def _prepare(*, user_input, runtime_context):
        return {
            "session_key": "web:draft",
            "messages": [{"role": "user", "content": "hello"}],
        }

    monkeypatch.setattr(runner, "_prepare_turn_state", _prepare)
    monkeypatch.setattr(runner, "_frontdoor_tool_schemas_for_state", lambda **_: [])
    monkeypatch.setattr(runner, "_frontdoor_live_visible_skills", lambda *_: [])
    monkeypatch.setattr(runner, "_frontdoor_contract_notice_sections", lambda *_a, **_k: {})
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
        lambda **_: 1000,
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

    clear_skill_roster_notice_stores()
    session = SimpleNamespace(state=SimpleNamespace(session_key="web:draft"))
    setattr(session, "_frontdoor_persona_digest", persona_digest("v1"))
    await runner.build_session_send_preflight(session, user_input={"content": "hello"})

    context = notice_context_for("session:web:draft")
    assert context["persona_cursor"] == {"digest": "", "initialized": False}


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
