from __future__ import annotations

import asyncio
from types import SimpleNamespace

from g3ku.core.messages import UserInputMessage
from g3ku.runtime import web_ceo_sessions
from g3ku.runtime.session_agent import (
    _TRANSCRIPT_STATE_COMPLETED,
    _TRANSCRIPT_STATE_FAILED,
    _TRANSCRIPT_STATE_PENDING,
    RuntimeAgentSession,
)


class _FakeSessionStore:
    def __init__(self, session):
        self._session = session
        self.save_calls = 0

    def get_or_create(self, session_key):
        return self._session

    def save(self, session):
        self.save_calls += 1


class _FakePersistedSession:
    def __init__(self, messages=None):
        self.messages = list(messages or [])

    def add_message(self, role, content, **kwargs):
        record = {"role": role, "content": content}
        record.update(kwargs)
        self.messages.append(record)
        return record


def _user_record(text, *, turn_id, state):
    return {
        "role": "user",
        "content": text,
        "timestamp": "2026-09-24T13:13:39.008689+00:00",
        "metadata": {"_transcript_turn_id": turn_id, "_transcript_state": state},
    }


def _assistant_record(text, *, turn_id, source=""):
    metadata = {}
    if source:
        metadata["source"] = source
    return {"role": "assistant", "content": text, "turn_id": turn_id, "metadata": metadata}


def _build_agent(persisted_session, *, session_key="china:qqbot:default:dm"):
    loop = SimpleNamespace(
        sessions=_FakeSessionStore(persisted_session),
        model="test-model",
        reasoning_effort=None,
        multi_agent_runner=None,
        prompt_trace=False,
    )
    return RuntimeAgentSession(loop, session_key=session_key, channel="qqbot", chat_id="dm")


def _input(text, *, turn_id):
    return UserInputMessage(content=text, metadata={"_transcript_turn_id": turn_id})


def _persist(agent, *, user_input, text, promote=True, retire=False, assistant_text="回答"):
    agent._active_user_batch_inputs = [user_input]
    agent._active_batch_id = None
    agent._active_turn_id = "turn-f"
    agent._last_verified_task_ids = []
    return asyncio.run(
        agent._persist_turn_transcript(
            user_input=user_input,
            user_text=text,
            assistant_text=assistant_text,
            interaction_flow=[],
            internal_source=None,
            route_kind="dm",
            retire_lingering_transcript_rows=retire,
            promote_user_transcript_rows=promote,
        )
    )


def test_failed_turn_settles_batch_user_rows_and_leaves_no_queue():
    """失败收尾把本批用户行落 failed 终态，不再放回队列。"""
    session = _FakePersistedSession([])
    agent = _build_agent(session)
    user_input = _input("对项目机制进行检查", turn_id="turn-f")
    session.messages.append(_user_record("对项目机制进行检查", turn_id="turn-f", state=_TRANSCRIPT_STATE_PENDING))

    _persist(agent, user_input=user_input, text="对项目机制进行检查", promote=False, assistant_text="这一轮处理失败")

    assert agent._settle_unanswered_user_inputs(user_input, persisted_session=session) == 1
    assert session.messages[0]["metadata"]["_transcript_state"] == _TRANSCRIPT_STATE_FAILED
    assert agent._state.queued_follow_up_messages == []
    # 同一条再次收尾幂等：已经终态，不重复计数、不降级
    assert agent._settle_unanswered_user_inputs(user_input, persisted_session=session) == 0
    assert session.messages[0]["metadata"]["_transcript_state"] == _TRANSCRIPT_STATE_FAILED


def test_settle_skips_blank_inputs_and_non_pending_rows():
    session = _FakePersistedSession([])
    agent = _build_agent(session)
    blank_input = UserInputMessage(content="   ", metadata={"_transcript_turn_id": "turn-e"})
    agent._active_user_batch_inputs = [blank_input]
    assert agent._settle_unanswered_user_inputs(blank_input) == 0

    answered = _input("已经答过", turn_id="turn-a")
    session.messages.append(_user_record("已经答过", turn_id="turn-a", state=_TRANSCRIPT_STATE_COMPLETED))
    agent._active_user_batch_inputs = [answered]
    assert agent._settle_unanswered_user_inputs(answered) == 0
    assert session.messages[0]["metadata"]["_transcript_state"] == _TRANSCRIPT_STATE_COMPLETED


def test_settle_marks_every_turn_in_the_batch_once():
    session = _FakePersistedSession([])
    agent = _build_agent(session)
    primary = _input("主输入", turn_id="turn-1")
    follow_up = _input("补充", turn_id="turn-2")
    agent._active_user_batch_inputs = [primary, follow_up]
    session.messages.append(_user_record("主输入", turn_id="turn-1", state=_TRANSCRIPT_STATE_PENDING))
    session.messages.append(_user_record("补充", turn_id="turn-2", state=_TRANSCRIPT_STATE_PENDING))

    assert agent._settle_unanswered_user_inputs(primary) == 2
    assert [row["metadata"]["_transcript_state"] for row in session.messages] == [
        _TRANSCRIPT_STATE_FAILED,
        _TRANSCRIPT_STATE_FAILED,
    ]


def test_rehydrate_settles_legacy_pending_row_behind_error_reply():
    """升级前的失败回合：pending + runtime_error 行按新契约收口，不再接回队列。"""
    failed_pair = [
        _user_record("对项目机制进行检查", turn_id="turn-f", state=_TRANSCRIPT_STATE_PENDING),
        _assistant_record("这一轮处理失败", turn_id="turn-f", source="runtime_error"),
    ]
    session = _FakePersistedSession(failed_pair)
    agent = _build_agent(session)

    assert agent._state.queued_follow_up_messages == []
    assert session.messages[0]["metadata"]["_transcript_state"] == _TRANSCRIPT_STATE_FAILED
    # 再次接回也不会复活
    assert agent._rehydrate_queued_follow_ups() == 0
    assert agent._state.queued_follow_up_messages == []


def test_rehydrate_skips_answered_pending_row():
    answered_pair = [
        _user_record("已经答过的排队行", turn_id="turn-g", state=_TRANSCRIPT_STATE_PENDING),
        _assistant_record("真正的回答", turn_id="turn-g"),
    ]
    agent = _build_agent(_FakePersistedSession(answered_pair))

    assert agent._state.queued_follow_up_messages == []


def test_rehydrate_still_restores_a_real_queued_follow_up():
    pending = _user_record("还没派发的补充", turn_id="turn-q", state=_TRANSCRIPT_STATE_PENDING)
    agent = _build_agent(_FakePersistedSession([pending]))

    assert [item.content for item in agent._state.queued_follow_up_messages] == ["还没派发的补充"]
    assert agent._state.queued_follow_up_messages[0].metadata["_transcript_state"] == _TRANSCRIPT_STATE_PENDING


def test_unrelated_success_turn_does_not_touch_failed_row():
    failed = _user_record("对项目机制进行检查", turn_id="turn-f", state=_TRANSCRIPT_STATE_FAILED)
    session = _FakePersistedSession(
        [failed, _assistant_record("这一轮处理失败", turn_id="turn-f", source="runtime_error")]
    )
    agent = _build_agent(session)

    _persist(
        agent,
        user_input=_input("继续", turn_id="turn-n"),
        text="继续",
        retire=True,
        assistant_text="好的，接着看那条检查请求",
    )

    assert session.messages[0]["metadata"]["_transcript_state"] == _TRANSCRIPT_STATE_FAILED


def test_failed_turn_boundary_snapshot_mirrors_baseline_and_skips_empty(monkeypatch):
    written: list[tuple[str, str]] = []
    monkeypatch.setattr(
        web_ceo_sessions,
        "read_completed_continuity_snapshot",
        lambda session_id: {"frontdoor_request_body_messages": [{"role": "user", "content": "基线"}]},
    )
    monkeypatch.setattr(
        web_ceo_sessions,
        "write_turn_boundary_snapshot",
        lambda session_id, turn_id, payload: written.append((session_id, turn_id)),
    )
    agent = _build_agent(_FakePersistedSession([]))

    agent._mirror_completed_continuity_as_turn_boundary(session_key="web:ceo-x", turn_id="turn-f")
    assert written == [("web:ceo-x", "turn-f")]

    monkeypatch.setattr(web_ceo_sessions, "read_completed_continuity_snapshot", lambda session_id: {})
    agent._mirror_completed_continuity_as_turn_boundary(session_key="web:ceo-x", turn_id="turn-g")
    # 空基线不写：截断到它会把会话清成零上下文
    assert written == [("web:ceo-x", "turn-f")]


def test_visible_turn_promotes_user_row_on_success():
    """对照：成功路径仍然升格，本改动没有把 pending 变成永久态。"""
    pending = _user_record("新项目问题", turn_id="turn-s", state=_TRANSCRIPT_STATE_PENDING)
    session = _FakePersistedSession([pending])
    agent = _build_agent(session)

    _persist(agent, user_input=_input("新项目问题", turn_id="turn-s"), text="新项目问题", promote=True)

    assert session.messages[0]["metadata"]["_transcript_state"] == _TRANSCRIPT_STATE_COMPLETED
