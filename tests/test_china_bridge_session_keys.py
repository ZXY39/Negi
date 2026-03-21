from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from g3ku.china_bridge.session_keys import (
    build_memory_chat_id,
    build_runtime_chat_id,
    build_session_key,
    parse_china_session_key,
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


def test_build_ceo_session_catalog_includes_local_and_channel_groups(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        "g3ku.runtime.web_ceo_sessions.load_config",
        lambda: SimpleNamespace(
            workspace_path=str(tmp_path),
            china_bridge=SimpleNamespace(
                channels=SimpleNamespace(
                    qqbot={"enabled": True, "accounts": {}, "appId": "qq-app", "clientSecret": "qq-secret"},
                    dingtalk={"enabled": False, "accounts": {}},
                    wecom={"enabled": False, "accounts": {}},
                    wecom_app={"enabled": False, "accounts": {}},
                    feishu_china={"enabled": False, "accounts": {}},
                )
            ),
        ),
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

        def _use_rag_memory(self) -> bool:
            return False

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
