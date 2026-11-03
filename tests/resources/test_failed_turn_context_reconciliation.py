from __future__ import annotations

from types import SimpleNamespace

from g3ku.runtime.frontdoor._ceo_runtime_ops import CeoFrontDoorRuntimeOps


class _FakeSession:
    def __init__(self, messages, *, session_key="web:ceo-failed"):
        self.messages = list(messages or [])
        self.state = SimpleNamespace(session_key=session_key)


def _user_record(text, *, turn_id, state="failed", timestamp=""):
    row = {
        "role": "user",
        "content": text,
        "metadata": {"_transcript_turn_id": turn_id, "_transcript_state": state},
    }
    if timestamp:
        row["timestamp"] = timestamp
    return row


def _error_row(text, *, turn_id, code="legacy_session_error", status=None, recoverable=True):
    metadata = {
        "source": "runtime_error",
        "error_code": code,
        "error_message": text,
        "recoverable": recoverable,
    }
    if status is not None:
        metadata["error_status"] = status
    return {"role": "assistant", "content": f"这一轮处理失败：{text}", "turn_id": turn_id, "metadata": metadata}


def _reconcile(seed, session, current=None):
    return CeoFrontDoorRuntimeOps._reconcile_failed_turns_into_seed(
        seed,
        session,
        current_turn_user_content=current,
    )


def _contents(result):
    return [str(item.get("content") or "") for item in result]


def test_failed_turn_appends_notice_when_user_message_already_in_seed():
    session = _FakeSession(
        [
            _user_record("找一下真机自动化的方向", turn_id="turn-1"),
            _error_row("Error code: 400", turn_id="turn-1", status=400, recoverable=False),
        ]
    )
    seed = [{"role": "user", "content": "找一下真机自动化的方向"}]

    result = _reconcile(seed, session)

    assert sum(1 for item in result if item["role"] == "user") == 1
    notices = [text for text in _contents(result) if "[上一回合未完成 turn_id=turn-1]" in text]
    assert len(notices) == 1
    assert "code=legacy_session_error" in notices[0]
    assert "status=400" in notices[0]
    assert "retryable=false" in notices[0]
    assert "这不是一条新的用户消息" in notices[0]


def test_failed_turn_appends_user_message_and_notice_when_seed_lacks_it():
    session = _FakeSession(
        [
            _user_record("Prepare 期就失败的问题", turn_id="turn-2"),
            _error_row("provider unavailable", turn_id="turn-2"),
        ]
    )
    seed = [{"role": "user", "content": "更早的消息"}]

    result = _reconcile(seed, session)

    assert _contents(result)[-2:] == [
        "Prepare 期就失败的问题",
        result[-1]["content"],
    ]
    assert result[-2]["role"] == "user"
    assert result[-1]["role"] == "assistant"
    assert "[上一回合未完成 turn_id=turn-2]" in result[-1]["content"]


def test_reconcile_is_idempotent_across_turns():
    session = _FakeSession(
        [
            _user_record("失败的问题", turn_id="turn-3"),
            _error_row("boom", turn_id="turn-3"),
        ]
    )
    first = _reconcile([{"role": "user", "content": "失败的问题"}], session)

    second = _reconcile(first, session)

    assert _contents(second) == _contents(first)
    assert sum(1 for text in _contents(second) if "[上一回合未完成 turn_id=turn-3]" in text) == 1


def test_seed_that_already_carries_raw_error_row_gets_no_duplicate_notice():
    """冷启动请求体原样带过错误行时，不追加第二份错误信息。"""
    session = _FakeSession(
        [
            _user_record("失败的问题", turn_id="turn-4"),
            _error_row("boom", turn_id="turn-4"),
        ]
    )
    seed = [
        {"role": "user", "content": "失败的问题"},
        {"role": "assistant", "content": "这一轮处理失败：boom"},
    ]

    result = _reconcile(seed, session)

    assert _contents(result) == ["失败的问题", "这一轮处理失败：boom"]


def test_internal_turn_error_without_failed_user_row_is_ignored():
    session = _FakeSession(
        [
            {"role": "assistant", "content": "heartbeat failed", "turn_id": "turn-hb", "metadata": {"source": "runtime_error"}},
        ]
    )

    result = _reconcile([{"role": "user", "content": "正常历史"}], session)

    assert _contents(result) == ["正常历史"]


def test_error_message_is_truncated_in_notice_but_not_in_transcript():
    long_message = "x" * 5000
    session = _FakeSession(
        [
            _user_record("长错误", turn_id="turn-5"),
            _error_row(long_message, turn_id="turn-5"),
        ]
    )
    seed = [{"role": "user", "content": "长错误"}]

    result = _reconcile(seed, session)
    notice = result[-1]["content"]

    assert "..." not in notice or "…" in notice
    assert len(notice) < 4000
    assert session.messages[1]["content"].endswith(long_message)


def test_current_turn_user_text_suppresses_only_the_user_echo():
    session = _FakeSession(
        [
            _user_record("同一句话", turn_id="turn-6"),
            _error_row("boom", turn_id="turn-6"),
        ]
    )

    result = _reconcile(
        [{"role": "user", "content": "同一句话"}],
        session,
        current="同一句话",
    )

    assert _contents(result).count("同一句话") == 1
    assert any("[上一回合未完成 turn_id=turn-6]" in text for text in _contents(result))
