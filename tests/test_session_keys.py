"""Tests for the canonical session-key module (g3ku/runtime/session_keys.py).

Migrated from the removed ``g3ku/china_bridge`` shim tests when the China
channel subsystem was deleted. The ``china:`` key namespace stays canonical:
pre-existing channel transcripts remain readable archives, and external
bridges use the derived ``ext:`` namespace.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from g3ku.runtime.session_keys import (
    build_memory_chat_id,
    build_runtime_chat_id,
    build_session_key,
    parse_china_session_key,
    sanitize_channel_outbound_text,
)
from g3ku.runtime.session_agent import RuntimeAgentSession
from g3ku.runtime.web_ceo_sessions import build_ceo_session_catalog


def test_build_session_key_merges_dm_by_channel_and_account() -> None:
    first = build_session_key(
        channel="qqbot",
        account_id="default",
        peer_kind="user",
        peer_id="user-openid-123",
    )
    second = build_session_key(
        channel="qqbot",
        account_id="default",
        peer_kind="user",
        peer_id="user-openid-456",
    )

    assert first == "china:qqbot:default:dm"
    assert second == first


def test_build_session_key_keeps_group_and_thread_isolated() -> None:
    assert build_session_key(
        channel="wecom",
        account_id="bot-a",
        peer_kind="group",
        peer_id="wx-chat-1",
        thread_id="thread-9",
    ) == "china:wecom:bot-a:group:wx-chat-1:thread:thread-9"

    assert build_session_key(
        channel="wecom",
        account_id="bot-a",
        peer_kind="group",
        peer_id="wx-chat-2",
    ) == "china:wecom:bot-a:group:wx-chat-2"


def test_runtime_and_memory_chat_ids_split_dm_target_from_memory_scope() -> None:
    assert build_runtime_chat_id(
        account_id="bot-a",
        peer_kind="user",
        peer_id="user-1",
    ) == "bot-a:dm:user-1"

    assert build_memory_chat_id(
        account_id="bot-a",
        peer_kind="user",
        peer_id="user-1",
    ) == "bot-a:dm"

    assert build_runtime_chat_id(
        account_id="bot-a",
        peer_kind="group",
        peer_id="wx-chat-1",
        thread_id="thread-9",
    ) == "bot-a:group:wx-chat-1:thread:thread-9"

    assert build_memory_chat_id(
        account_id="bot-a",
        peer_kind="group",
        peer_id="wx-chat-1",
        thread_id="thread-9",
    ) == "bot-a:group:wx-chat-1:thread:thread-9"


def test_parse_china_session_key_supports_new_and_legacy_dm_shapes() -> None:
    merged = parse_china_session_key("china:qqbot:default:dm")
    assert merged is not None
    assert merged.chat_type == "dm"
    assert merged.peer_id is None
    assert merged.thread_id is None
    assert merged.merged_dm is True

    merged_thread = parse_china_session_key("china:qqbot:default:dm:thread:thread-1")
    assert merged_thread is not None
    assert merged_thread.chat_type == "dm"
    assert merged_thread.peer_id is None
    assert merged_thread.thread_id == "thread-1"
    assert merged_thread.merged_dm is True

    legacy = parse_china_session_key("china:qqbot:default:dm:user-openid-123")
    assert legacy is not None
    assert legacy.chat_type == "dm"
    assert legacy.peer_id == "user-openid-123"
    assert legacy.thread_id is None
    assert legacy.merged_dm is False

    group = parse_china_session_key("china:wecom:bot-a:group:wx-chat-1:thread:thread-9")
    assert group is not None
    assert group.chat_type == "group"
    assert group.peer_id == "wx-chat-1"
    assert group.thread_id == "thread-9"
    assert group.merged_dm is False


def test_sanitize_channel_outbound_text_truncates_session_events_marker():
    text = "Visible reply.\n[SESSION EVENTS]\n## EVENT BUNDLE\ninternal stuff"
    assert sanitize_channel_outbound_text(text) == "Visible reply."


def test_sanitize_channel_outbound_text_internal_only_returns_empty():
    assert sanitize_channel_outbound_text("[SESSION EVENTS]\ninternal") == ""


def test_sanitize_channel_outbound_text_removes_runtime_tool_contract_echo():
    contract = (
        "## Runtime Tool Contract\n"
        "kind: frontdoor_runtime_tool_contract\n"
        "callable_tools: `exec`"
    )
    assert sanitize_channel_outbound_text(contract) == ""
    assert sanitize_channel_outbound_text("Visible answer\n\n" + contract) == "Visible answer"


def test_build_ceo_session_catalog_lists_legacy_channel_sessions_readonly(monkeypatch, tmp_path: Path) -> None:
    """After the subsystem removal the catalog groups pre-existing china:*
    transcripts purely from session storage (no config-driven placeholders)."""
    monkeypatch.setattr(
        "g3ku.runtime.web_ceo_sessions.load_config",
        lambda: SimpleNamespace(workspace_path=str(tmp_path)),
    )

    class _Session:
        def __init__(self, key: str, content: str) -> None:
            self.key = key
            self.messages = [{"role": "assistant", "content": content}]
            self.metadata = {}
            self.created_at = datetime(2026, 3, 21, 10, 0, 0)
            self.updated_at = datetime(2026, 3, 21, 10, 5, 0)

    class _Store:
        def __init__(self) -> None:
            self._sessions = {
                "web:shared": _Session("web:shared", "local reply"),
                "china:qqbot:default:group:group-1": _Session("china:qqbot:default:group:group-1", "group reply"),
                "china:qqbot:default:dm:user-a": _Session("china:qqbot:default:dm:user-a", "legacy dm reply"),
            }

        def list_sessions(self):
            return [{"key": key} for key in self._sessions]

        def get_or_create(self, key: str):
            return self._sessions[key]

        def save(self, _session) -> None:
            return None

    catalog = build_ceo_session_catalog(_Store(), active_session_id="china:qqbot:default:dm")
    assert any(item["session_id"] == "web:shared" for item in catalog["items"])
    assert catalog["active_session_family"] == "channel"
    channel_items = catalog["channel_groups"][0]["items"]
    assert any(item["session_id"] == "china:qqbot:default:dm" for item in channel_items)
    assert any(item["session_id"] == "china:qqbot:default:group:group-1" for item in channel_items)
    for item in channel_items:
        assert item["is_readonly"] is True
        assert item["can_delete"] is False


def test_build_ceo_session_catalog_lists_external_bridge_sessions_readonly(monkeypatch, tmp_path: Path) -> None:
    """Live ``ext:*`` bridge sessions surface in the channel catalog as
    read-only groups keyed by bridge, titled from the registry mapping."""
    from g3ku.runtime.external_sessions import ExternalSessionRegistry, reset_external_session_registry

    monkeypatch.setattr(
        "g3ku.runtime.web_ceo_sessions.load_config",
        lambda: SimpleNamespace(workspace_path=str(tmp_path)),
    )
    reset_external_session_registry()
    registry = ExternalSessionRegistry(tmp_path)
    entry, _ = registry.resolve_or_create(bridge_id="qq", external_key="qq:dm:user-1")

    class _Session:
        def __init__(self, key: str, content: str) -> None:
            self.key = key
            self.messages = [{"role": "assistant", "content": content}]
            self.metadata = {}
            self.created_at = datetime(2026, 3, 21, 10, 0, 0)
            self.updated_at = datetime(2026, 3, 21, 10, 5, 0)

    class _Store:
        def __init__(self) -> None:
            # The catalog resolves the external registry from the session
            # manager's own workspace (mirrors the real SessionManager).
            self.workspace = tmp_path
            self._sessions = {
                "web:shared": _Session("web:shared", "local reply"),
                entry.session_key: _Session(entry.session_key, "bridge reply"),
            }

        def list_sessions(self):
            return [{"key": key} for key in self._sessions]

        def get_or_create(self, key: str):
            return self._sessions[key]

        def save(self, _session) -> None:
            return None

    catalog = build_ceo_session_catalog(_Store(), active_session_id=entry.session_key)
    assert catalog["active_session_family"] == "channel"
    ext_groups = [group for group in catalog["channel_groups"] if group["channel_id"] == "ext:qq"]
    assert len(ext_groups) == 1
    assert ext_groups[0]["label"].startswith("外部桥接")
    item = ext_groups[0]["items"][0]
    assert item["session_id"] == entry.session_key
    assert item["is_readonly"] is True
    assert item["can_delete"] is False
    assert item["session_origin"] == "external"
    assert "qq:dm:user-1" in item["title"]


def test_runtime_agent_session_serializes_prompt_and_keeps_live_targets(monkeypatch) -> None:
    async def _noop_refresh(**_kwargs):
        return None

    monkeypatch.setattr("g3ku.shells.web.refresh_web_agent_runtime", _noop_refresh)

    class _Persisted:
        def __init__(self) -> None:
            self.messages = []
            self.metadata = {}

        def add_message(self, role: str, content: str, **kwargs) -> None:
            self.messages.append({"role": role, "content": content, **kwargs})

    class _LoopStub:
        def __init__(self) -> None:
            self.prompt_trace = False
            self.memory_manager = None
            self.commit_service = None
            self.sessions = SimpleNamespace(get_or_create=lambda _key: _Persisted(), save=lambda _session: None)

        def create_session_cancellation_token(self, _session_key: str):
            return SimpleNamespace(cancel=lambda **_kwargs: None)

        def release_session_cancellation_token(self, _session_key: str, _token) -> None:
            return None

    async def _run() -> None:
        loop = _LoopStub()
        session = RuntimeAgentSession(loop, session_key="china:qqbot:default:dm", channel="qqbot", chat_id="default:dm:user-a")
        observed: list[tuple[str, str, str]] = []

        async def _fake_run_message(user_input):
            observed.append((session._chat_id, session._memory_chat_id, str(user_input.content)))
            await asyncio.sleep(0.02)
            return f"reply:{user_input.content}"

        monkeypatch.setattr(session, "_run_message", _fake_run_message)
        await asyncio.gather(
            session.prompt(
                "first",
                live_context={
                    "channel": "qqbot",
                    "chat_id": "default:dm:user-a",
                    "memory_channel": "qqbot",
                    "memory_chat_id": "default:dm",
                },
            ),
            session.prompt(
                "second",
                live_context={
                    "channel": "qqbot",
                    "chat_id": "default:dm:user-b",
                    "memory_channel": "qqbot",
                    "memory_chat_id": "default:dm",
                },
            ),
        )
        assert observed == [
            ("default:dm:user-a", "default:dm", "first"),
            ("default:dm:user-b", "default:dm", "second"),
        ]

    asyncio.run(_run())


class _RefreshSession:
    def __init__(self, key: str, content: str = "reply") -> None:
        self.key = key
        self.messages = [{"role": "assistant", "content": content}]
        self.metadata = {}
        self.created_at = datetime(2026, 3, 21, 10, 0, 0)
        self.updated_at = datetime(2026, 3, 21, 10, 5, 0)


class _RefreshStore:
    """Session-manager double for resolver/patch tests: registry-backed ext
    lookup needs ``workspace``; listing needs ``list_sessions``."""

    def __init__(self, workspace: Path, keys: list[str]) -> None:
        self.workspace = workspace
        self._sessions = {key: _RefreshSession(key) for key in keys}

    def list_sessions(self):
        return [{"key": key} for key in self._sessions]

    def get_or_create(self, key: str):
        return self._sessions[key]

    def save(self, _session) -> None:
        return None


def test_resolve_active_ceo_session_id_keeps_external_channel_session(tmp_path: Path) -> None:
    """刷新网页时激活的 ``ext:`` 渠道会话必须保住：解析器不能因为它不是
    ``web:`` 键就回退到最近的本地会话并改写状态存储（P3 回归形态）。"""
    from g3ku.runtime.external_sessions import ExternalSessionRegistry, reset_external_session_registry
    from g3ku.runtime.web_ceo_sessions import WebCeoStateStore, resolve_active_ceo_session_id

    reset_external_session_registry()
    try:
        registry = ExternalSessionRegistry(tmp_path)
        entry, _ = registry.resolve_or_create(bridge_id="qq-official", external_key="qq:c2c:user-1")

        # 本地存在更新的 web 会话：若 ext 键被误判无效，解析器会切到它。
        store = _RefreshStore(tmp_path, ["web:newer-local"])
        state_store = WebCeoStateStore(workspace=tmp_path)
        state_store.set_active_session_id(entry.session_key)

        resolved = resolve_active_ceo_session_id(store, state_store)

        assert resolved == entry.session_key
        assert state_store.get_active_session_id() == entry.session_key
    finally:
        reset_external_session_registry()


def test_resolve_active_ceo_session_id_keeps_ext_session_with_transcript_only(tmp_path: Path) -> None:
    """孤儿 ``ext:`` 转录（注册表无条目）同样是有效会话，刷新不得切走。"""
    from g3ku.runtime.external_sessions import reset_external_session_registry
    from g3ku.runtime.web_ceo_sessions import WebCeoStateStore, resolve_active_ceo_session_id

    reset_external_session_registry()
    try:
        orphan_key = "ext:qq-official:orphan12"
        store = _RefreshStore(tmp_path, ["web:newer-local", orphan_key])
        state_store = WebCeoStateStore(workspace=tmp_path)
        state_store.set_active_session_id(orphan_key)

        resolved = resolve_active_ceo_session_id(store, state_store)

        assert resolved == orphan_key
        assert state_store.get_active_session_id() == orphan_key
    finally:
        reset_external_session_registry()


def test_resolve_active_ceo_session_id_falls_back_for_unknown_ext_key(tmp_path: Path) -> None:
    """不存在的 ``ext:`` 键（既无注册表条目也无转录）仍走原有回退。"""
    from g3ku.runtime.external_sessions import reset_external_session_registry
    from g3ku.runtime.web_ceo_sessions import WebCeoStateStore, resolve_active_ceo_session_id

    reset_external_session_registry()
    try:
        store = _RefreshStore(tmp_path, ["web:only-local"])
        state_store = WebCeoStateStore(workspace=tmp_path)
        state_store.set_active_session_id("ext:ghost:deadbeef")

        resolved = resolve_active_ceo_session_id(store, state_store)

        assert resolved == "web:only-local"
        assert state_store.get_active_session_id() == "web:only-local"
    finally:
        reset_external_session_registry()


def test_build_channel_ceo_session_item_keeps_channel_shape(tmp_path: Path) -> None:
    """补丁/快照构建器必须给渠道键返回渠道形状（P4 根因的契约侧）：
    本地构建器对渠道键返回 None，通用兜底会把它标成普通 web 会话。"""
    from g3ku.runtime.external_sessions import ExternalSessionRegistry, reset_external_session_registry
    from g3ku.runtime.web_ceo_sessions import build_channel_ceo_session_item

    reset_external_session_registry()
    try:
        registry = ExternalSessionRegistry(tmp_path)
        entry, _ = registry.resolve_or_create(bridge_id="qq-official", external_key="qq:c2c:user-1")
        china_key = "china:qqbot:default:dm:user-a"
        store = _RefreshStore(tmp_path, [entry.session_key, china_key, "web:shared"])

        ext_item = build_channel_ceo_session_item(
            store, entry.session_key, active_session_id=entry.session_key, is_running=True
        )
        assert ext_item is not None
        assert ext_item["session_family"] == "channel"
        assert ext_item["session_origin"] == "external"
        assert ext_item["channel_id"] == "ext:qq-official"
        assert ext_item["is_readonly"] is True
        assert ext_item["can_rename"] is False
        assert ext_item["can_delete"] is False
        assert ext_item["is_active"] is True
        assert ext_item["is_running"] is True
        assert "qq:c2c:user-1" in ext_item["title"]

        china_item = build_channel_ceo_session_item(store, china_key, active_session_id="web:shared")
        assert china_item is not None
        assert china_item["session_family"] == "channel"
        assert china_item["session_origin"] == "china"
        assert china_item["channel_id"] == "qqbot"
        assert china_item["is_readonly"] is True
        assert china_item["is_active"] is False

        assert build_channel_ceo_session_item(store, "web:shared", active_session_id="") is None
    finally:
        reset_external_session_registry()


def test_publish_ceo_session_patch_emits_channel_shape_for_channel_keys(tmp_path: Path) -> None:
    """``ceo.sessions.patch`` 对渠道键必须发渠道形状条目：本地形状条目会
    被前端短暂插进本地 web 会话列表（P4）。"""
    from g3ku.runtime.api.websocket_ceo import _publish_ceo_session_patch
    from g3ku.runtime.external_sessions import ExternalSessionRegistry, reset_external_session_registry
    from g3ku.runtime.web_ceo_sessions import WebCeoStateStore

    reset_external_session_registry()
    try:
        registry = ExternalSessionRegistry(tmp_path)
        entry, _ = registry.resolve_or_create(bridge_id="qq-official", external_key="qq:c2c:user-1")
        china_key = "china:qqbot:default:dm:user-a"
        store = _RefreshStore(tmp_path, [entry.session_key, china_key])
        state_store = WebCeoStateStore(workspace=tmp_path)
        state_store.set_active_session_id(entry.session_key)

        envelopes: list[dict] = []

        class _Registry:
            def next_ceo_seq(self, session_id: str) -> int:
                return 1

            def publish_global_ceo(self, envelope: dict) -> None:
                envelopes.append(envelope)

        agent = SimpleNamespace(main_task_service=SimpleNamespace(registry=_Registry()))
        runtime_manager = SimpleNamespace(get=lambda session_id: None)

        _publish_ceo_session_patch(
            agent=agent,
            transcript_store=store,
            runtime_manager=runtime_manager,
            state_store=state_store,
            session_id=entry.session_key,
        )
        _publish_ceo_session_patch(
            agent=agent,
            transcript_store=store,
            runtime_manager=runtime_manager,
            state_store=state_store,
            session_id=china_key,
        )

        assert [envelope["type"] for envelope in envelopes] == ["ceo.sessions.patch", "ceo.sessions.patch"]
        ext_envelope, china_envelope = envelopes
        ext_item = ext_envelope["data"]["item"]
        assert ext_item["session_id"] == entry.session_key
        assert ext_item["session_family"] == "channel"
        assert ext_item["session_origin"] == "external"
        assert ext_item["channel_id"] == "ext:qq-official"
        assert ext_item["is_readonly"] is True
        assert ext_envelope["data"]["active_session_family"] == "channel"

        china_item = china_envelope["data"]["item"]
        assert china_item["session_id"] == china_key
        assert china_item["session_family"] == "channel"
        assert china_item["session_origin"] == "china"
        assert china_item["channel_id"] == "qqbot"
    finally:
        reset_external_session_registry()
