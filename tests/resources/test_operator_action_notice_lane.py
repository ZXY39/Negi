"""控制台动作的车道：持久化、作用域过滤、一次性投递与归因文案。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from g3ku.runtime import skill_roster_notice as notice
from g3ku.runtime.frontdoor._ceo_create_agent_impl import CreateAgentCeoFrontDoorRunner
from g3ku.runtime.frontdoor.tool_contract import clear_pinned_contract_stores
from main.governance import operator_actions as oa
from main.governance.store import GovernanceStore
from main.runtime.react_loop import ReActToolLoop


@pytest.fixture()
def store(tmp_path: Path):
    instance = GovernanceStore(tmp_path / "governance.sqlite3")
    yield instance
    instance.close()


def _append(
    store: GovernanceStore,
    action: str,
    *,
    audience: str = oa.AUDIENCE_FRONTDOOR,
    scope: str = oa.SCOPE_GLOBAL,
    category: str = "test",
    created_at: str = "2026-11-01T00:00:00+08:00",
) -> dict[str, object]:
    return store.append_operator_action(
        created_at=created_at,
        category=category,
        audience=audience,
        scope=scope,
        text=oa.format_operator_action(action),
    )


def _provider(store: GovernanceStore, *, session_key: str = "web:operator-actions", group_key: str = ""):
    def _fetch(*, cursor_id: int, initialized: bool):
        _ = initialized
        return {
            "actions": store.list_operator_actions(
                after_id=cursor_id,
                audiences=(oa.AUDIENCE_FRONTDOOR, oa.AUDIENCE_ALL),
                scopes=oa.scope_keys_for_frontdoor(session_key, group_key=group_key),
                limit=oa.MAX_ACTIONS_PER_NOTICE,
            ),
            "latest_id": store.latest_operator_action_id(),
        }

    return _fetch


def test_action_store_filters_audience_and_scope(store: GovernanceStore) -> None:
    global_frontdoor = _append(store, "disabled sticker pack `demo`")
    grouped_memory = _append(
        store,
        "requested deletion of long-term memory (line 3)",
        scope=oa.memory_scope("group-1"),
        category="memory",
    )
    node_tool = _append(
        store,
        "disabled tool `exec`",
        audience=oa.AUDIENCE_NODE,
        category="tool",
    )
    session_only = _append(
        store,
        "switched this session to `limited` tier",
        scope=oa.session_scope("web:other"),
        category="session",
    )

    frontdoor_rows = store.list_operator_actions(
        after_id=0,
        audiences=(oa.AUDIENCE_FRONTDOOR, oa.AUDIENCE_ALL),
        scopes=oa.scope_keys_for_frontdoor("group-session", group_key="group-1"),
        limit=20,
    )
    assert [row["id"] for row in frontdoor_rows] == [
        global_frontdoor["id"],
        grouped_memory["id"],
    ]

    other_group_rows = store.list_operator_actions(
        after_id=0,
        audiences=(oa.AUDIENCE_FRONTDOOR, oa.AUDIENCE_ALL),
        scopes=oa.scope_keys_for_frontdoor("group-session", group_key="group-2"),
        limit=20,
    )
    assert [row["id"] for row in other_group_rows] == [global_frontdoor["id"]]

    node_rows = store.list_operator_actions(
        after_id=0,
        audiences=(oa.AUDIENCE_NODE, oa.AUDIENCE_ALL),
        scopes=oa.scope_keys_for_node(),
        limit=20,
    )
    assert [row["id"] for row in node_rows] == [node_tool["id"]]

    assert session_only["id"] == store.latest_operator_action_id()


def test_action_text_is_english_and_actor_explicit() -> None:
    text = oa.format_operator_action("disabled sticker pack `demo`")
    assert text == "The user (operator) disabled sticker pack `demo` from the console."


def test_notice_aligns_then_delivers_once_per_scope(store: GovernanceStore) -> None:
    notice.clear_skill_roster_notice_stores()
    scope = "session:web:operator-actions"
    provider = _provider(store)

    # 新会话先对齐：操作发生在它存在之前，不重播。
    first, _ = notice.deliver_for_scope(scope, [], now="2026-11-01T00:00:00", operator_actions_provider=provider)
    assert first is None

    _append(store, "disabled sticker pack `demo`")
    second, _ = notice.deliver_for_scope(scope, [], now="2026-11-01T00:00:01", operator_actions_provider=provider)
    assert second is not None
    content = str(second["content"])
    assert content.startswith(notice.RUNTIME_NOTICE_MARKER)
    assert notice.OPERATOR_ACTIONS_HEADING in content
    assert "The user (operator) disabled sticker pack `demo` from the console." in content
    assert content.count(notice.RUNTIME_NOTICE_MARKER) == 1

    third, _ = notice.deliver_for_scope(scope, [], now="2026-11-01T00:00:02", operator_actions_provider=provider)
    assert third is None
    assert notice.notice_context_for(scope)["operator_action_cursor"]["initialized"] is True


def test_notice_cursor_survives_normalization() -> None:
    normalized = notice.normalize_skill_roster_notice_context(
        {"operator_action_cursor": {"id": 17, "initialized": True}}
    )
    assert normalized["operator_action_cursor"] == {"id": 17, "initialized": True}
    assert notice.context_has_state(normalized) is True


def test_operator_actions_render_before_state_sections() -> None:
    message = notice.build_notice_message(
        [{"skill_id": "alpha", "display_name": "Alpha", "description": "a"}],
        memory_text="261101:remembered",
        operator_actions=[{"id": 1, "text": "The user (operator) disabled tool `exec` from the console."}],
        contract_sections=[("denied_tools", "`exec`")],
    )
    assert message is not None
    content = str(message["content"])
    assert content.count(notice.RUNTIME_NOTICE_MARKER) == 1
    assert content.startswith(
        f"{notice.RUNTIME_NOTICE_MARKER}\n\n{notice.OPERATOR_ACTIONS_HEADING}"
    )
    assert content.index(notice.OPERATOR_ACTIONS_HEADING) < content.index("Skills newly readable")


def test_node_scope_only_carries_global_actions() -> None:
    assert oa.scope_keys_for_node() == (oa.SCOPE_GLOBAL,)
    keys = oa.scope_keys_for_frontdoor("web:abc", group_key="g1")
    assert oa.SCOPE_GLOBAL in keys
    assert oa.session_scope("web:abc") in keys
    assert oa.memory_scope("") in keys
    assert oa.memory_scope("g1") in keys


def test_frontdoor_provider_aligns_then_queries_session_scopes() -> None:
    calls: list[dict[str, object]] = []

    class _Service:
        def latest_operator_action_id(self) -> int:
            return 9

        def list_operator_actions(self, **kwargs: object):
            calls.append(dict(kwargs))
            return {"actions": [{"id": 10, "text": "The operator disabled tool `exec`."}], "latest_id": 10}

    runner = CreateAgentCeoFrontDoorRunner(
        loop=SimpleNamespace(main_task_service=_Service())
    )

    aligned = runner._frontdoor_operator_action_provider(
        initialized=False, session_key="web:abc"
    )
    assert aligned == {"actions": [], "latest_id": 9}
    assert calls == []

    result = runner._frontdoor_operator_action_provider(
        cursor_id=9, initialized=True, session_key="web:abc"
    )
    assert result == {
        "actions": [{"id": 10, "text": "The operator disabled tool `exec`."}],
        "latest_id": 10,
    }
    assert calls == [
        {
            "after_id": 9,
            "audiences": (oa.AUDIENCE_FRONTDOOR, oa.AUDIENCE_ALL),
            "scopes": oa.scope_keys_for_frontdoor("web:abc", group_key=""),
            "limit": oa.MAX_ACTIONS_PER_NOTICE,
        }
    ]


def test_frontdoor_notice_carries_operator_action_once(store: GovernanceStore) -> None:
    notice.clear_skill_roster_notice_stores()
    clear_pinned_contract_stores()
    session_key = "web:operator-actions-frontdoor"

    class _Service:
        def list_contract_visible_skill_resources(self, *, actor_role: str, session_id: str):
            return []

        def list_effective_tool_names(self, *, actor_role: str, session_id: str):
            return ["other_tool"]

        def _current_exec_runtime_policy_payload(self):
            return {"mode": "governed", "guardrails_enabled": True, "summary": "governed"}

        def latest_operator_action_id(self) -> int:
            return store.latest_operator_action_id()

        def list_operator_actions(self, *, after_id: int, audiences, scopes, limit: int):
            return {
                "actions": store.list_operator_actions(
                    after_id=after_id,
                    audiences=audiences,
                    scopes=scopes,
                    limit=limit,
                ),
                "latest_id": store.latest_operator_action_id(),
            }

    runner = CreateAgentCeoFrontDoorRunner(
        loop=SimpleNamespace(main_task_service=_Service())
    )
    runner._ceo_session_temp_dir = lambda _session_key: "C:/temp/ceo"
    base = [
        {"role": "system", "content": "SYSTEM"},
        {"role": "user", "content": "hi"},
    ]

    first = runner._apply_frontdoor_skill_roster_notice(base, session_key=session_key)
    _append(store, "disabled sticker pack `demo`")
    second = runner._apply_frontdoor_skill_roster_notice(first, session_key=session_key)
    operator_texts = [
        str(item.get("content") or "")
        for item in second
        if str(item.get("content") or "").startswith(notice.RUNTIME_NOTICE_MARKER)
        and notice.OPERATOR_ACTIONS_HEADING in str(item.get("content") or "")
    ]
    assert len(operator_texts) == 1
    assert "The user (operator) disabled sticker pack `demo` from the console." in operator_texts[0]

    third = runner._apply_frontdoor_skill_roster_notice(second, session_key=session_key)
    assert sum(
        notice.OPERATOR_ACTIONS_HEADING in str(item.get("content") or "")
        for item in third
    ) == sum(
        notice.OPERATOR_ACTIONS_HEADING in str(item.get("content") or "")
        for item in second
    )


class _NodeLogService:
    def __init__(self) -> None:
        self.metadata: dict[str, object] = {}

    def update_node_metadata(self, node_id: str, mutator) -> None:
        self.metadata = mutator(self.metadata)


def test_node_notice_carries_tool_action_once(store: GovernanceStore) -> None:
    notice.clear_skill_roster_notice_stores()
    clear_pinned_contract_stores()
    node = SimpleNamespace(
        task_id="task-operator-actions",
        node_id="node-operator-actions",
        metadata={},
        node_kind="execution",
    )
    loop = ReActToolLoop(
        chat_backend=SimpleNamespace(),
        log_service=_NodeLogService(),
        max_iterations=2,
    )

    def _provider(*, cursor_id: int, initialized: bool):
        _ = initialized
        return {
            "actions": store.list_operator_actions(
                after_id=cursor_id,
                audiences=(oa.AUDIENCE_NODE, oa.AUDIENCE_ALL),
                scopes=oa.scope_keys_for_node(),
                limit=oa.MAX_ACTIONS_PER_NOTICE,
            ),
            "latest_id": store.latest_operator_action_id(),
        }

    messages = [
        {"role": "system", "content": "node system"},
        {"role": "user", "content": "go"},
    ]
    first = loop._apply_node_skill_roster_notice(
        list(messages),
        node=node,
        skill_roster_items=[],
        operator_actions_provider=_provider,
    )
    _append(store, "disabled tool `exec`", audience=oa.AUDIENCE_NODE, category="tool")
    second = loop._apply_node_skill_roster_notice(
        first,
        node=node,
        skill_roster_items=[],
        operator_actions_provider=_provider,
    )
    texts = [
        str(item.get("content") or "")
        for item in second
        if str(item.get("content") or "").startswith(notice.RUNTIME_NOTICE_MARKER)
    ]
    assert len(texts) == 1
    assert notice.OPERATOR_ACTIONS_HEADING in texts[0]
    assert "The user (operator) disabled tool `exec` from the console." in texts[0]

    third = loop._apply_node_skill_roster_notice(
        second,
        node=node,
        skill_roster_items=[],
        operator_actions_provider=_provider,
    )
    assert sum(
        notice.OPERATOR_ACTIONS_HEADING in str(item.get("content") or "")
        for item in third
    ) == 1
