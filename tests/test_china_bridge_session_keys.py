from __future__ import annotations

from g3ku.china_bridge.session_keys import build_chat_id, build_session_key


def test_build_session_key_for_dm_and_group_thread():
    assert build_session_key(
        channel="qqbot",
        account_id="default",
        peer_kind="user",
        peer_id="user-openid-123",
    ) == "china:qqbot:default:dm:user-openid-123"

    assert build_session_key(
        channel="wecom",
        account_id="bot-a",
        peer_kind="group",
        peer_id="wx-chat-1",
        thread_id="thread-9",
    ) == "china:wecom:bot-a:group:wx-chat-1:thread:thread-9"

    assert build_chat_id(
        account_id="bot-a",
        peer_kind="group",
        peer_id="wx-chat-1",
        thread_id="thread-9",
    ) == "bot-a:group:wx-chat-1:thread:thread-9"
