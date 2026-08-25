"""轮内模型链热刷新：call_model 迭代边界按配置 revision 重解析 model_refs。

背景：`model_config set_scope_chain` 等切换只会改写配置并刷新 loop 运行时，
进行中的回合仍携带 prepare_turn 时钉死的 model_refs。该文件验证：

- `_rotate_frontdoor_model_refs_if_stale` 的 revision 对比语义；
- `_graph_call_model` 在迭代边界用新链发送下一次模型请求，并把新链写回状态
  （供 execute_tools 的工具运行时上下文 / 多模态闸门读取）。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from g3ku.runtime.frontdoor._ceo_create_agent_impl import CreateAgentCeoFrontDoorRunner
from g3ku.runtime.frontdoor.state_models import CeoRuntimeContext


def test_rotate_model_refs_keeps_state_when_revision_matches(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace())
    monkeypatch.setattr(runner, "_frontdoor_runtime_config_revision", lambda: 7)

    def _explode() -> list[str]:
        raise AssertionError("revision 未变化时不应重新解析模型链")

    monkeypatch.setattr(runner, "_resolve_ceo_model_refs", _explode)

    state = {"model_refs": ["model-a"], "model_refs_revision": 7, "session_key": "web:shared"}
    result = runner._rotate_frontdoor_model_refs_if_stale(state)
    assert result is state


def test_rotate_model_refs_keeps_legacy_state_without_baseline(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace())

    def _explode() -> list[str]:
        raise AssertionError("缺少 revision 基线的旧状态不应触发重解析")

    monkeypatch.setattr(runner, "_resolve_ceo_model_refs", _explode)

    state = {"model_refs": ["model-a"], "session_key": "web:shared"}
    result = runner._rotate_frontdoor_model_refs_if_stale(state)
    assert result is state


def test_rotate_model_refs_rotates_when_revision_advances(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace())
    monkeypatch.setattr(runner, "_frontdoor_runtime_config_revision", lambda: 8)
    monkeypatch.setattr(runner, "_resolve_ceo_model_refs", lambda: ["model-b", "model-a"])

    state = {"model_refs": ["model-a"], "model_refs_revision": 7, "session_key": "web:shared"}
    result = runner._rotate_frontdoor_model_refs_if_stale(state)

    assert result is not state
    assert result["model_refs"] == ["model-b", "model-a"]
    assert result["model_refs_revision"] == 8
    # 原状态不被就地修改
    assert state["model_refs"] == ["model-a"]
    assert state["model_refs_revision"] == 7


def test_rotate_model_refs_keeps_state_when_resolution_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace())
    monkeypatch.setattr(runner, "_frontdoor_runtime_config_revision", lambda: 8)

    def _broken() -> list[str]:
        raise RuntimeError("config broken")

    monkeypatch.setattr(runner, "_resolve_ceo_model_refs", _broken)

    state = {"model_refs": ["model-a"], "model_refs_revision": 7, "session_key": "web:shared"}
    result = runner._rotate_frontdoor_model_refs_if_stale(state)
    assert result is state


def test_rotate_model_refs_keeps_state_when_resolution_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace())
    monkeypatch.setattr(runner, "_frontdoor_runtime_config_revision", lambda: 8)
    monkeypatch.setattr(runner, "_resolve_ceo_model_refs", lambda: [])

    state = {"model_refs": ["model-a"], "model_refs_revision": 7, "session_key": "web:shared"}
    result = runner._rotate_frontdoor_model_refs_if_stale(state)
    assert result is state


@pytest.mark.asyncio
async def test_graph_call_model_rotates_stale_model_refs_before_send(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """模拟轮内 `model_config` 切链：下一次模型调用即改用新链，并写回状态。"""
    runner = CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace())

    # 模拟 model_config 工具已经改写配置并刷新运行时：revision 前进，新链生效。
    monkeypatch.setattr(runner, "_frontdoor_runtime_config_revision", lambda: 9)
    monkeypatch.setattr(runner, "_resolve_ceo_model_refs", lambda: ["qwen-vl", "deepseek"])

    monkeypatch.setattr(runner, "_build_langchain_tools_for_state", lambda **_: [])
    monkeypatch.setattr(
        runner,
        "_resolve_frontdoor_send_model_context_window",
        lambda **_: {
            "model_key": "qwen-vl",
            "provider_model": "custom:qwen-vl",
            "context_window_tokens": 320000,
        },
        raising=False,
    )
    monkeypatch.setattr(runner, "_estimate_frontdoor_send_total_tokens", lambda **_: 1000, raising=False)

    captured_model_refs: list[list[str]] = []

    async def _call_model_with_tools(**kwargs):
        captured_model_refs.append(list(kwargs.get("model_refs") or []))
        return {"content": "ok"}

    monkeypatch.setattr(runner, "_call_model_with_tools", _call_model_with_tools)
    monkeypatch.setattr(
        runner,
        "_model_response_view",
        lambda message: SimpleNamespace(
            content=message.get("content", ""),
            tool_calls=[],
            provider_request_meta={},
            provider_request_body={},
        ),
    )
    monkeypatch.setattr(runner, "_checkpoint_safe_model_response_payload", lambda _message: {"ok": True})
    monkeypatch.setattr(runner, "_persist_frontdoor_actual_request", lambda **_: {})

    session = SimpleNamespace(
        state=SimpleNamespace(session_key="web:shared"),
        _frontdoor_stage_state={"active_stage_id": "", "transition_required": False, "stages": []},
        _frontdoor_canonical_context={"active_stage_id": "", "transition_required": False, "stages": []},
        _compression_state={},
        _semantic_context_state={},
        _frontdoor_hydrated_tool_names=[],
        _emit_state_snapshot=lambda: None,
    )
    runtime = SimpleNamespace(
        context=CeoRuntimeContext(loop=None, session=session, session_key="web:shared", on_progress=None)
    )
    state = {
        "messages": [
            {"role": "system", "content": "SYSTEM"},
            {"role": "user", "content": "切模型看图"},
        ],
        # prepare_turn 钉死的旧链与旧 revision（切换发生在本轮工具执行中）。
        "model_refs": ["deepseek"],
        "model_refs_revision": 3,
        "parallel_enabled": False,
        "prompt_cache_key": "cache-key",
        "iteration": 1,
        "max_iterations": 5,
        "session_key": "web:shared",
        "tool_names": [],
        "provider_tool_names": [],
        "candidate_tool_names": [],
        "candidate_tool_items": [],
        "hydrated_tool_names": [],
        "visible_skill_ids": [],
        "candidate_skill_ids": [],
        "rbac_visible_tool_names": [],
        "rbac_visible_skill_ids": [],
        "turn_overlay_text": "",
        "repair_overlay_text": None,
        "frontdoor_stage_state": {"active_stage_id": "", "transition_required": False, "stages": []},
        "frontdoor_history_shrink_reason": "",
        "frontdoor_token_preflight_diagnostics": {},
    }

    result = await runner._graph_call_model(state, runtime=runtime)

    assert captured_model_refs == [["qwen-vl", "deepseek"]]
    assert result["model_refs"] == ["qwen-vl", "deepseek"]
    assert result["model_refs_revision"] == 9


def test_multimodal_gate_follows_state_model_refs(monkeypatch: pytest.MonkeyPatch) -> None:
    """工具运行时上下文的多模态闸门按 state 内模型链首位判定。"""
    runner = CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace())

    managed_models = {
        "qwen-vl": SimpleNamespace(image_multimodal_enabled=True),
        "deepseek": SimpleNamespace(image_multimodal_enabled=False),
    }

    monkeypatch.setattr(
        runner,
        "_frontdoor_runtime_config",
        lambda: SimpleNamespace(get_managed_model=lambda key: managed_models.get(key)),
    )

    # 切换后的链：首位多模态 → 闸门放行
    assert runner._ceo_image_multimodal_enabled_for_model_refs(["qwen-vl", "deepseek"]) is True
    # 切换前的链：首位非多模态 → 闸门拒绝
    assert runner._ceo_image_multimodal_enabled_for_model_refs(["deepseek"]) is False


def test_build_tool_runtime_context_uses_state_model_refs(monkeypatch: pytest.MonkeyPatch) -> None:
    """execute_tools 读取的工具运行时上下文直接取 state 中（已轮转的）模型链。"""
    session = SimpleNamespace(state=SimpleNamespace(session_key="web:shared"))
    runner = CreateAgentCeoFrontDoorRunner(
        loop=SimpleNamespace(
            sessions=SimpleNamespace(get_or_create=lambda _key: SimpleNamespace(metadata={})),
            workspace=None,
            temp_dir="",
        )
    )
    monkeypatch.setattr(runner, "_session_task_defaults", lambda _record: {})

    def _explode() -> list[str]:
        raise AssertionError("state 已有 model_refs 时不应回退重新解析")

    monkeypatch.setattr(runner, "_resolve_ceo_model_refs", _explode)

    runtime = SimpleNamespace(
        context=SimpleNamespace(session=session, session_key="web:shared", on_progress=None)
    )
    payload = runner._build_tool_runtime_context(
        state={"model_refs": ["qwen-vl", "deepseek"], "user_input": {"content": "hi", "metadata": {}}},
        runtime=runtime,
    )
    assert payload["model_refs"] == ["qwen-vl", "deepseek"]
