"""前门请求的内容审查词替换：历史静默、当前 hop 提示、压缩请求同源。"""

from __future__ import annotations

import copy
import json
from types import SimpleNamespace

import pytest

from g3ku.deployment import data_root as data_root_module
from g3ku.runtime.frontdoor._ceo_create_agent_impl import CreateAgentCeoFrontDoorRunner
from g3ku.runtime.frontdoor.state_models import CeoRuntimeContext
from g3ku.runtime.frontdoor.token_preflight_compaction import FrontdoorTokenPreflightResult
from g3ku.security import content_policy as cp


async def _announce_boundary_noop(**_kwargs) -> None:
    return None


@pytest.fixture(autouse=True)
def _isolated_data_root(tmp_path, monkeypatch):
    monkeypatch.setenv("G3KU_DATA_DIR", str(tmp_path))
    data_root_module.reset_cache()
    cp.load_policy(force=True)
    try:
        yield tmp_path
    finally:
        data_root_module.reset_cache()


def _runtime() -> SimpleNamespace:
    session = SimpleNamespace(
        state=SimpleNamespace(session_key="web:shared"),
        announce_context_compression_boundary=_announce_boundary_noop,
        _compression_state={},
        _semantic_context_state={},
        _emit_state_snapshot=lambda: None,
    )
    return SimpleNamespace(
        context=CeoRuntimeContext(
            loop=None,
            session=session,
            session_key="web:shared",
            on_progress=None,
        )
    )


def _state(messages: list[dict]) -> dict:
    return {
        "messages": messages,
        "model_refs": ["ceo_primary"],
        "parallel_enabled": False,
        "prompt_cache_key": "cache-key",
        "iteration": 0,
        "max_iterations": 3,
        "session_key": "web:shared",
        "tool_names": [],
        "provider_tool_names": [],
        "candidate_tool_names": [],
        "candidate_tool_items": [],
        "visible_skill_ids": [],
        "candidate_skill_ids": [],
        "rbac_visible_tool_names": [],
        "rbac_visible_skill_ids": [],
        "turn_overlay_text": "",
        "repair_overlay_text": None,
        "frontdoor_history_shrink_reason": "",
        "frontdoor_token_preflight_diagnostics": {},
    }


def _install_runner(monkeypatch, captured_calls, persisted):
    runner = CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace())
    monkeypatch.setattr(runner, "_frontdoor_tool_schemas_for_state", lambda **_: [])
    monkeypatch.setattr(
        runner,
        "_resolve_frontdoor_send_model_context_window",
        lambda **_: {
            "model_key": "ceo_primary",
            "provider_model": "openai:gpt-5.2",
            "context_window_tokens": 200000,
        },
        raising=False,
    )

    async def _call_model_with_tools(**kwargs):
        captured_calls.append(list(kwargs.get("messages") or []))
        return {"content": "ok"}

    def _persist(**kwargs):
        persisted.append(list(kwargs.get("request_messages") or []))
        return {}

    monkeypatch.setattr(runner, "_call_model_with_tools", _call_model_with_tools)
    monkeypatch.setattr(runner, "_checkpoint_safe_model_response_payload", lambda _message: {"ok": True})
    monkeypatch.setattr(runner, "_persist_frontdoor_actual_request", _persist)
    return runner


@pytest.mark.asyncio
async def test_graph_call_model_masks_history_and_notices_current_hop(monkeypatch) -> None:
    cp.save_policy(True, ["六四"])
    messages = [
        {"role": "system", "content": "SYSTEM"},
        {"role": "user", "content": "older 六四"},
        {"role": "assistant", "content": "ok"},
        {"role": "tool", "name": "search", "content": '{"summary": "old 六四", "ref": "a"}'},
        {"role": "assistant", "content": "call"},
        {"role": "tool", "name": "search", "content": '{"summary": "new 六四", "ref": "b"}'},
        {"role": "tool", "name": "read", "content": "plain 六四 tail"},
    ]
    original = copy.deepcopy(messages)
    state = _state(messages)
    captured_calls: list[list[dict]] = []
    persisted: list[list[dict]] = []
    runner = _install_runner(monkeypatch, captured_calls, persisted)

    result = await runner._graph_call_model(state, runtime=_runtime())

    assert state["messages"] == original
    sent = captured_calls[0]
    tool_messages = [item for item in sent if item.get("role") == "tool"]
    assert tool_messages[0]["content"].startswith('{"summary": "old ')
    assert cp.REPLACEMENT_MARKER in tool_messages[0]["content"]
    assert cp.NOTICE_PREFIX not in tool_messages[0]["content"]
    assert tool_messages[1]["content"].startswith("{")
    assert cp.REPLACEMENT_MARKER in tool_messages[1]["content"]
    assert cp.NOTICE_PREFIX in tool_messages[1]["content"]
    assert tool_messages[2]["content"].startswith(cp.NOTICE_PREFIX)
    # 发起方正文也被换、且不带提示：用户自己打来的词是 400 的主来源；
    # state 里那一行仍是原文（上面 `state["messages"] == original` 钉的就是这件事）。
    assert sent[1]["content"] == f"older {cp.REPLACEMENT_MARKER}"
    assert cp.NOTICE_PREFIX not in sent[1]["content"]
    assert persisted[0] == sent

    durable = result["messages"]
    durable_tools = [item for item in durable if item.get("role") == "tool"]
    assert cp.REPLACEMENT_MARKER in durable_tools[0]["content"]
    assert cp.REPLACEMENT_MARKER in durable_tools[1]["content"]


@pytest.mark.asyncio
async def test_graph_call_model_masks_messages_before_token_compression_helper(monkeypatch) -> None:
    cp.save_policy(True, ["六四"])
    messages = [
        {"role": "system", "content": "SYSTEM"},
        {"role": "user", "content": "older"},
        {"role": "tool", "name": "search", "content": '{"summary": "六四", "ref": "a"}'},
    ]
    state = _state(copy.deepcopy(messages))
    captured_calls: list[list[dict]] = []
    persisted: list[list[dict]] = []
    runner = _install_runner(monkeypatch, captured_calls, persisted)

    snapshot = {
        "request_messages": copy.deepcopy(messages),
        "durable_request_messages": copy.deepcopy(messages),
        "model_info": {"model_key": "ceo_primary", "provider_model": "openai:gpt-5.2"},
        "context_window_tokens": 200000,
        "estimated_total_tokens": 190000,
        "would_trigger_token_compression": True,
    }
    monkeypatch.setattr(runner, "_frontdoor_send_preflight_snapshot", lambda **_: snapshot)
    compression_inputs: list[list[dict]] = []

    async def _compress(**kwargs):
        compression_inputs.append(list(kwargs.get("request_messages") or []))
        return FrontdoorTokenPreflightResult(
            request_messages=list(kwargs["request_messages"]),
            final_request_tokens=1000,
            history_shrink_reason="",
            diagnostics={"applied": False, "reason": "test"},
        )

    monkeypatch.setattr(runner, "_run_frontdoor_llm_token_compression", _compress)

    await runner._graph_call_model(state, runtime=_runtime())

    assert compression_inputs
    compressed_tool = [item for item in compression_inputs[0] if item.get("role") == "tool"][0]
    parsed = json.loads(compressed_tool["content"])
    assert parsed["summary"] == cp.REPLACEMENT_MARKER
    assert cp.NOTICE_PREFIX in parsed[cp.NOTICE_FIELD]
    sent_tool = [item for item in captured_calls[0] if item.get("role") == "tool"][0]
    assert cp.REPLACEMENT_MARKER in sent_tool["content"]
    assert cp.NOTICE_PREFIX in sent_tool["content"]
