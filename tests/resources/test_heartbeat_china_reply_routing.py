from __future__ import annotations

from types import SimpleNamespace

from g3ku.heartbeat.bootstrap import build_web_session_heartbeat
from g3ku.heartbeat.session_service import WebSessionHeartbeatService, _derive_session_channel_chat
from g3ku.runtime.manager import SessionRuntimeManager


def test_derive_session_channel_chat_china_dm_key() -> None:
    # Regression: a naive first-colon split used to yield ("china",
    # "qqbot:default:dm"), poisoning the runtime session meta and making
    # heartbeat reply routing publish channel="china" messages that the
    # china drain silently skips.
    channel, chat_id = _derive_session_channel_chat("china:qqbot:default:dm")
    assert channel == "qqbot"
    assert chat_id == "default:dm"


def test_derive_session_channel_chat_china_group_key() -> None:
    channel, chat_id = _derive_session_channel_chat("china:qqbot:default:group:peer-1")
    assert channel == "qqbot"
    assert chat_id == "default:group:peer-1"


def test_derive_session_channel_chat_china_group_thread_key() -> None:
    channel, chat_id = _derive_session_channel_chat("china:qqbot:default:group:peer-1:thread:t-9")
    assert channel == "qqbot"
    assert chat_id == "default:group:peer-1:thread:t-9"


def test_derive_session_channel_chat_web_key_unchanged() -> None:
    channel, chat_id = _derive_session_channel_chat("web:ceo-abc")
    assert channel == "web"
    assert chat_id == "ceo-abc"


def test_derive_session_channel_chat_plain_key_unchanged() -> None:
    channel, chat_id = _derive_session_channel_chat("shared")
    assert channel == "web"
    assert chat_id == "shared"


def test_derive_session_channel_chat_malformed_china_key_falls_back() -> None:
    channel, chat_id = _derive_session_channel_chat("china:broken")
    assert channel == "china"
    assert chat_id == "broken"


class _FakeSession:
    def __init__(self, loop, *, session_key, channel, chat_id, memory_channel=None, memory_chat_id=None):
        self.session_key = session_key
        self.channel = channel
        self.chat_id = chat_id


def test_manager_meta_first_registration_wins() -> None:
    manager = SessionRuntimeManager(loop=SimpleNamespace())
    manager._session_cls = _FakeSession

    # Authoritative registration by the owning transport.
    manager.get_or_create(
        session_key="china:qqbot:default:dm",
        channel="qqbot",
        chat_id="default:dm:EB6C8D",
    )
    assert manager.session_meta("china:qqbot:default:dm") == ("qqbot", "default:dm:EB6C8D")

    # A later heartbeat wake must not clobber the authoritative meta.
    manager.get_or_create(
        session_key="china:qqbot:default:dm",
        channel="china",
        chat_id="qqbot:default:dm",
    )
    assert manager.session_meta("china:qqbot:default:dm") == ("qqbot", "default:dm:EB6C8D")


def _agent_stub() -> SimpleNamespace:
    return SimpleNamespace(
        workspace=".",
        main_task_service=SimpleNamespace(store=None),
        sessions=SimpleNamespace(),
    )


def test_build_heartbeat_reuses_started_instance_across_fresh_notifiers() -> None:
    # Regression: comparing reply_notifier by identity always failed because
    # callers pass a fresh closure each time, rebuilding a not-started
    # heartbeat and orphaning the started one (stranding enqueued events).
    agent = _agent_stub()
    runtime_manager = SimpleNamespace()

    first = build_web_session_heartbeat(
        agent, runtime_manager, reply_notifier=lambda session_id, text: None
    )
    assert isinstance(first, WebSessionHeartbeatService)
    assert agent.web_session_heartbeat is first

    second = build_web_session_heartbeat(
        agent, runtime_manager, reply_notifier=lambda session_id, text: None
    )
    assert second is first


def test_build_heartbeat_refreshes_missing_notifier_on_reuse() -> None:
    agent = _agent_stub()
    runtime_manager = SimpleNamespace()

    first = build_web_session_heartbeat(agent, runtime_manager, reply_notifier=None)
    assert isinstance(first, WebSessionHeartbeatService)
    assert first._reply_notifier is None

    def _notifier(session_id, text):
        return None

    second = build_web_session_heartbeat(agent, runtime_manager, reply_notifier=_notifier)
    assert second is first
    assert second._reply_notifier is _notifier
