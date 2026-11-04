from __future__ import annotations

import json

from g3ku.qq_official.group_history import GroupHistoryStore


def _store(tmp_path, *, max_bytes: int = 2 * 1024**3) -> GroupHistoryStore:
    return GroupHistoryStore(
        root=tmp_path / "group-inbox",
        app_id="app-1",
        external_key="qq:group:g1",
        session_key="ext:qq-official-app-1:qq:group:g1",
        max_bytes=max_bytes,
    )


def test_append_digest_consume_and_clear_keeps_sequence(tmp_path) -> None:
    store = _store(tmp_path)
    first = store.append(
        text="第一句",
        sender_id="AAAA",
        sender_name="小明",
        role="admin",
        is_master=False,
        files=[{"kind": "image", "name": "a.png", "mime": "image/png", "data": b"png"}],
    )
    second = store.append(
        text="第二句",
        sender_id="OWNER",
        sender_name="Master",
        role="owner",
        is_master=True,
    )
    assert first == 1
    assert second == 2
    rows, stats = store.build_digest()
    assert [row["n"] for row in rows] == [1, 2]
    assert stats.message_count == 2
    assert stats.file_count == 1
    assert stats.participant_count == 2
    assert stats.has_master is True
    assert stats.n_start == 1 and stats.n_end == 2
    media = rows[0]["files"][0]
    assert media["name"] == "a.png"
    assert media["path"].endswith("1_a.png")

    store.mark_consumed(2)
    rows, _stats = store.build_digest()
    assert rows == []

    removed = store.clear()
    assert removed > 0
    assert store.read_messages() == []
    third = store.append(
        text="清空后的新消息",
        sender_id="AAAA",
        sender_name="小明",
        role="admin",
        is_master=False,
    )
    assert third == 3


def test_size_limit_evicts_oldest_messages(tmp_path) -> None:
    store = _store(tmp_path, max_bytes=600)
    store.append(text="a" * 200, sender_id="AAAA", sender_name="小明", role="member", is_master=False)
    store.append(text="b" * 200, sender_id="BBBB", sender_name="小红", role="member", is_master=False)
    rows = store.read_messages()
    assert len(rows) <= 1
    assert rows[-1]["text"].startswith("b")
    assert store.usage_bytes() <= 600 or len(rows) == 1


def test_state_is_json_and_contains_no_message_text(tmp_path) -> None:
    store = _store(tmp_path)
    store.append(text="secret text", sender_id="AAAA", sender_name="小明", role="member", is_master=False)
    state = json.loads((tmp_path / "group-inbox" / "state.json").read_text(encoding="utf-8"))
    assert "secret text" not in json.dumps(state, ensure_ascii=False)
    assert state["next_n"] == 2
