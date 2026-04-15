from __future__ import annotations

import asyncio
from types import SimpleNamespace


def test_frontdoor_query_rewrite_enabled_defaults_true_and_honors_explicit_disable(monkeypatch) -> None:
    from g3ku.runtime.context import frontdoor_catalog_selection as selection

    monkeypatch.delenv("G3KU_ENABLE_FRONTDOOR_QUERY_REWRITE", raising=False)
    assert selection._frontdoor_query_rewrite_enabled() is True

    for disabled_value in ("0", "false", "no", "off", "FALSE", " Off "):
        monkeypatch.setenv("G3KU_ENABLE_FRONTDOOR_QUERY_REWRITE", disabled_value)
        assert selection._frontdoor_query_rewrite_enabled() is False

    for enabled_value in ("1", "true", "yes", "on", "TRUE", " On "):
        monkeypatch.setenv("G3KU_ENABLE_FRONTDOOR_QUERY_REWRITE", enabled_value)
        assert selection._frontdoor_query_rewrite_enabled() is True


def test_build_query_rewrite_cache_key_depends_only_on_raw_query_and_revision_inputs() -> None:
    from g3ku.runtime.context.frontdoor_query_rewriter import build_query_rewrite_cache_key

    first = build_query_rewrite_cache_key(
        raw_query="find the right browser workflow",
        exposure_revision="exp:rev-1",
        rewrite_prompt_revision="rewrite:prompt:v1",
    )
    second = build_query_rewrite_cache_key(
        raw_query="find the right browser workflow",
        exposure_revision="exp:rev-1",
        rewrite_prompt_revision="rewrite:prompt:v1",
    )
    changed_exposure = build_query_rewrite_cache_key(
        raw_query="find the right browser workflow",
        exposure_revision="exp:rev-2",
        rewrite_prompt_revision="rewrite:prompt:v1",
    )
    changed_raw_query = build_query_rewrite_cache_key(
        raw_query="find the right filesystem workflow",
        exposure_revision="exp:rev-1",
        rewrite_prompt_revision="rewrite:prompt:v1",
    )
    changed_rewrite_prompt = build_query_rewrite_cache_key(
        raw_query="find the right browser workflow",
        exposure_revision="exp:rev-1",
        rewrite_prompt_revision="rewrite:prompt:v2",
    )

    assert first == second
    assert first != changed_exposure
    assert first != changed_raw_query
    assert first != changed_rewrite_prompt


def test_rewrite_frontdoor_catalog_queries_uses_sidecar_request_shape_and_cache(monkeypatch) -> None:
    from g3ku.runtime.context import frontdoor_catalog_selection as selection

    selection._FRONTDOOR_QUERY_REWRITE_CACHE.clear()
    observed_requests: list[dict[str, object]] = []
    monkeypatch.setattr(selection, "_frontdoor_query_rewrite_enabled", lambda: True)

    async def fake_invoke_frontdoor_catalog_rewrite_model(**kwargs) -> dict[str, str]:
        observed_requests.append(dict(kwargs))
        request = dict(kwargs.get("request") or {})
        assert kwargs["query_text"] == "find the right browser workflow"
        assert kwargs["visible_skill_ids"] == ["browser.search"]
        assert kwargs["visible_tool_ids"] == ["filesystem"]
        assert set(request) == {
            "raw_query",
            "visible_skill_ids",
            "visible_tool_ids",
            "exposure_revision",
        }
        assert request["raw_query"] == "find the right browser workflow"
        assert request["visible_skill_ids"] == ["browser.search"]
        assert request["visible_tool_ids"] == ["filesystem"]
        assert isinstance(request["exposure_revision"], str)
        return {
            "skill_query": "browser search workflow",
            "tool_query": "filesystem tool",
            "model": "stub-model",
        }

    monkeypatch.setattr(
        selection,
        "_invoke_frontdoor_catalog_rewrite_model",
        fake_invoke_frontdoor_catalog_rewrite_model,
    )

    memory_manager = SimpleNamespace(store=SimpleNamespace(_dense_enabled=True))
    kwargs = {
        "loop": None,
        "memory_manager": memory_manager,
        "query_text": "find the right browser workflow",
        "visible_skills": [{"skill_id": "browser.search"}],
        "visible_families": [{"tool_id": "filesystem"}],
    }

    first = asyncio.run(selection.rewrite_frontdoor_catalog_queries(**kwargs))
    second = asyncio.run(selection.rewrite_frontdoor_catalog_queries(**kwargs))

    assert first["status"] == "rewritten"
    assert first["skill_query"] == "browser search workflow"
    assert first["tool_query"] == "filesystem tool"
    assert second == first
    assert len(observed_requests) == 1


def test_rewrite_frontdoor_catalog_queries_does_not_cache_fallback_or_passthrough(monkeypatch) -> None:
    from g3ku.runtime.context import frontdoor_catalog_selection as selection

    selection._FRONTDOOR_QUERY_REWRITE_CACHE.clear()
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(selection, "_frontdoor_query_rewrite_enabled", lambda: True)

    async def flaky_invoke(**kwargs) -> dict[str, str]:
        calls.append(dict(kwargs))
        if len(calls) == 1:
            raise RuntimeError("transient rewrite failure")
        return {
            "skill_query": "browser skill rewrite",
            "tool_query": "filesystem tool rewrite",
            "model": "stub-model",
        }

    monkeypatch.setattr(selection, "_invoke_frontdoor_catalog_rewrite_model", flaky_invoke)

    dense_off = SimpleNamespace(store=SimpleNamespace(_dense_enabled=False))
    dense_on = SimpleNamespace(store=SimpleNamespace(_dense_enabled=True))
    shared_kwargs = {
        "loop": None,
        "query_text": "find the right browser workflow",
        "visible_skills": [{"skill_id": "browser.search"}],
        "visible_families": [{"tool_id": "filesystem"}],
    }

    passthrough = asyncio.run(
        selection.rewrite_frontdoor_catalog_queries(memory_manager=dense_off, **shared_kwargs)
    )
    fallback = asyncio.run(
        selection.rewrite_frontdoor_catalog_queries(memory_manager=dense_on, **shared_kwargs)
    )
    rewritten = asyncio.run(
        selection.rewrite_frontdoor_catalog_queries(memory_manager=dense_on, **shared_kwargs)
    )

    assert passthrough["status"] == "passthrough"
    assert fallback["status"] == "fallback"
    assert rewritten["status"] == "rewritten"
    assert rewritten["skill_query"] == "browser skill rewrite"
    assert rewritten["tool_query"] == "filesystem tool rewrite"
    assert len(calls) == 2


def test_build_query_rewrite_exposure_revision_is_order_insensitive() -> None:
    from g3ku.runtime.context.frontdoor_query_rewriter import build_query_rewrite_exposure_revision

    first = build_query_rewrite_exposure_revision(
        visible_skill_ids=["browser.search", "filesystem.read", "browser.search"],
        visible_tool_ids=["filesystem", "web_fetch"],
    )
    second = build_query_rewrite_exposure_revision(
        visible_skill_ids=["filesystem.read", "browser.search"],
        visible_tool_ids=["web_fetch", "filesystem", "filesystem"],
    )

    assert first == second


def test_rewrite_sidecar_request_payload_is_canonical_for_same_visible_id_set(monkeypatch) -> None:
    from g3ku.runtime.context import frontdoor_catalog_selection as selection

    selection._FRONTDOOR_QUERY_REWRITE_CACHE.clear()
    observed_requests: list[dict[str, object]] = []
    monkeypatch.setattr(selection, "_frontdoor_query_rewrite_enabled", lambda: True)

    async def fake_invoke(**kwargs) -> dict[str, str]:
        observed_requests.append(dict(kwargs.get("request") or {}))
        return {
            "skill_query": "browser skill rewrite",
            "tool_query": "filesystem tool rewrite",
            "model": "stub-model",
        }

    monkeypatch.setattr(selection, "_invoke_frontdoor_catalog_rewrite_model", fake_invoke)
    monkeypatch.setattr(
        selection,
        "get_runtime_config",
        lambda force=False: (SimpleNamespace(resolve_role_model_key=lambda role: "responses:gpt-5.1-mini"), 11, False),
    )

    first_manager = SimpleNamespace(store=SimpleNamespace(_dense_enabled=True))
    second_manager = SimpleNamespace(store=SimpleNamespace(_dense_enabled=True))
    common = {
        "loop": None,
        "query_text": "find the right browser workflow",
    }

    asyncio.run(
        selection.rewrite_frontdoor_catalog_queries(
            memory_manager=first_manager,
            visible_skills=[{"skill_id": "browser.search"}, {"skill_id": "filesystem.read"}],
            visible_families=[{"tool_id": "filesystem"}, {"tool_id": "web_fetch"}],
            **common,
        )
    )
    asyncio.run(
        selection.rewrite_frontdoor_catalog_queries(
            memory_manager=second_manager,
            visible_skills=[{"skill_id": "filesystem.read"}, {"skill_id": "browser.search"}],
            visible_families=[{"tool_id": "web_fetch"}, {"tool_id": "filesystem"}],
            **common,
        )
    )

    assert len(observed_requests) == 2
    assert observed_requests[0] == observed_requests[1]
    assert observed_requests[0]["visible_skill_ids"] == ["browser.search", "filesystem.read"]
    assert observed_requests[0]["visible_tool_ids"] == ["filesystem", "web_fetch"]


def test_rewrite_sidecar_cache_invalidates_when_runtime_identity_changes(monkeypatch) -> None:
    from g3ku.runtime.context import frontdoor_catalog_selection as selection

    selection._FRONTDOOR_QUERY_REWRITE_CACHE.clear()
    runtime_state = {"model": "responses:gpt-5.1-mini", "revision": 11}
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(selection, "_frontdoor_query_rewrite_enabled", lambda: True)

    async def fake_invoke(**kwargs) -> dict[str, str]:
        calls.append(dict(kwargs))
        return {
            "skill_query": f"skill rewrite for {runtime_state['model']}",
            "tool_query": f"tool rewrite for {runtime_state['model']}",
            "model": str(runtime_state["model"]),
        }

    monkeypatch.setattr(selection, "_invoke_frontdoor_catalog_rewrite_model", fake_invoke)
    monkeypatch.setattr(
        selection,
        "get_runtime_config",
        lambda force=False: (
            SimpleNamespace(resolve_role_model_key=lambda role: str(runtime_state["model"])),
            int(runtime_state["revision"]),
            False,
        ),
    )

    memory_manager = SimpleNamespace(store=SimpleNamespace(_dense_enabled=True))
    kwargs = {
        "loop": None,
        "memory_manager": memory_manager,
        "query_text": "find the right browser workflow",
        "visible_skills": [{"skill_id": "browser.search"}],
        "visible_families": [{"tool_id": "filesystem"}],
    }

    first = asyncio.run(selection.rewrite_frontdoor_catalog_queries(**kwargs))
    second = asyncio.run(selection.rewrite_frontdoor_catalog_queries(**kwargs))
    runtime_state["model"] = "responses:gpt-5.1-nano"
    runtime_state["revision"] = 12
    third = asyncio.run(selection.rewrite_frontdoor_catalog_queries(**kwargs))

    assert first == second
    assert first != third
    assert first["model"] == "responses:gpt-5.1-mini"
    assert third["model"] == "responses:gpt-5.1-nano"
    assert len(calls) == 2


def test_rewrite_frontdoor_catalog_queries_defaults_to_passthrough_when_temporarily_disabled(monkeypatch) -> None:
    from g3ku.runtime.context import frontdoor_catalog_selection as selection

    selection._FRONTDOOR_QUERY_REWRITE_CACHE.clear()
    calls: list[dict[str, object]] = []

    async def fake_invoke(**kwargs) -> dict[str, str]:
        calls.append(dict(kwargs))
        return {
            "skill_query": "browser skill rewrite",
            "tool_query": "filesystem tool rewrite",
            "model": "stub-model",
        }

    monkeypatch.setattr(selection, "_invoke_frontdoor_catalog_rewrite_model", fake_invoke)
    monkeypatch.setattr(selection, "_frontdoor_query_rewrite_enabled", lambda: False)

    result = asyncio.run(
        selection.rewrite_frontdoor_catalog_queries(
            loop=None,
            memory_manager=SimpleNamespace(store=SimpleNamespace(_dense_enabled=True)),
            query_text="find the right browser workflow",
            visible_skills=[{"skill_id": "browser.search"}],
            visible_families=[{"tool_id": "filesystem"}],
        )
    )

    assert result["status"] == "passthrough"
    assert result["skill_query"] == "find the right browser workflow"
    assert result["tool_query"] == "find the right browser workflow"
    assert calls == []
