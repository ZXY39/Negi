"""停机续跑轮复用被截断回合的 turn_id：一个请求在转录里只留一行回复。

回归的实盘症状：会话 `web:ceo-86b77d402872` 在 2026-10-04 23:26 被「重启」截断后，
同一个阶段在网页上画成两个气泡（6/10 已闭合 + 10/10 转圈），因为续跑轮拿到了新的
turn_id 并另起了一行助手回复。
"""

from __future__ import annotations

import asyncio
import copy
from types import SimpleNamespace

import g3ku.runtime.web_ceo_sessions as web_ceo_sessions
from g3ku.core.messages import UserInputMessage
from g3ku.runtime.frontdoor.canonical_context import (
    TRANSCRIPT_CC_UPSERT_FIELD,
    TRANSCRIPT_PROJECTION_MODE,
    encode_cc_upsert,
    materialize_transcript_view,
)
from g3ku.runtime.session_agent import (
    _TRANSCRIPT_STATE_COMPLETED,
    _TRANSCRIPT_STATE_PAUSED,
    _TRANSCRIPT_TURN_ID_KEY,
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
        self.metadata = {}
        self.updated_at = None

    def add_message(self, role, content, **kwargs):
        record = {"role": role, "content": content}
        record.update(kwargs)
        self.messages.append(record)
        return record


def _build_agent(persisted_session):
    loop = SimpleNamespace(
        sessions=_FakeSessionStore(persisted_session),
        model="test-model",
        reasoning_effort=None,
        multi_agent_runner=None,
        prompt_trace=False,
    )
    return RuntimeAgentSession(
        loop,
        session_key="china:qqbot:default:dm",
        channel="qqbot",
        chat_id="dm",
    )


def _paused_user_row(text, *, turn_id):
    return {
        "role": "user",
        "content": text,
        "metadata": {
            _TRANSCRIPT_TURN_ID_KEY: turn_id,
            "_transcript_state": _TRANSCRIPT_STATE_PAUSED,
        },
    }


def _archive_row(*, turn_id, view=None):
    row = {
        "role": "assistant",
        "content": "继续定位：读取探测结果的尾部关键段落。",
        "turn_id": turn_id,
        "status": _TRANSCRIPT_STATE_PAUSED,
        "timestamp": "2026-10-04T23:26:16+08:00",
        "metadata": {
            "history_visible": False,
            "source": "manual_pause_archive",
            "archived_paused_turn": True,
        },
    }
    if view is not None:
        row["canonical_context"] = copy.deepcopy(view)
        row["canonical_context_projection"] = TRANSCRIPT_PROJECTION_MODE
    return row


def _heartbeat_input(reason, *, turn_id=""):
    metadata = {
        "heartbeat_internal": True,
        "heartbeat_reason": reason,
        "history_visibility": "internal_event",
    }
    if turn_id:
        metadata[_TRANSCRIPT_TURN_ID_KEY] = turn_id
    return UserInputMessage(content="[SESSION EVENTS]", metadata=metadata)


def test_resumable_turn_id_comes_from_the_tail_archive_row():
    session = _FakePersistedSession([_paused_user_row("重做一个视频", turn_id="T1"), _archive_row(turn_id="T1")])
    agent = _build_agent(session)

    assert agent._find_resumable_paused_archive_turn_id(session) == "T1"


def test_resumable_turn_id_is_dropped_once_anything_answered_after_it():
    session = _FakePersistedSession(
        [
            _paused_user_row("重做一个视频", turn_id="T1"),
            _archive_row(turn_id="T1"),
            {"role": "assistant", "content": "另一轮已经回复过了", "turn_id": "T2", "metadata": {}},
        ]
    )
    agent = _build_agent(session)

    assert agent._find_resumable_paused_archive_turn_id(session) == ""


def test_resume_adopts_the_interrupted_turn_only_for_a_pure_shutdown_resume_bundle():
    session = _FakePersistedSession([_paused_user_row("重做一个视频", turn_id="T1"), _archive_row(turn_id="T1")])
    agent = _build_agent(session)

    assert agent._adopt_interrupted_turn_id_for_resume(_heartbeat_input("task_terminal")) == ""
    assert agent._active_turn_id is None
    # 与终态/失速事件混成一束时答的不是用户那条提问，不许认领。
    assert agent._adopt_interrupted_turn_id_for_resume(_heartbeat_input("mixed")) == ""
    assert agent._active_turn_id is None

    assert agent._adopt_interrupted_turn_id_for_resume(_heartbeat_input("shutdown_resume")) == "T1"
    assert agent._active_turn_id == "T1"
    # 已有在飞回合时不抢：认领只发生在停机后重建的第一个续跑轮。
    agent._active_turn_id = "T9"
    assert agent._adopt_interrupted_turn_id_for_resume(_heartbeat_input("shutdown_resume")) == ""


def test_resume_seeds_usage_from_the_interrupted_turn_artifacts(monkeypatch):
    session = _FakePersistedSession([_paused_user_row("重做一个视频", turn_id="T1"), _archive_row(turn_id="T1")])
    agent = _build_agent(session)
    monkeypatch.setattr(
        web_ceo_sessions,
        "read_session_turn_token_usage",
        lambda session_id: {"T1": {"input_tokens": 63290, "output_tokens": 6819, "cache_hit_tokens": 262912, "call_count": 6}},
    )

    agent._adopt_interrupted_turn_id_for_resume(_heartbeat_input("shutdown_resume"))

    # 累加器重启即空；不接回截断前那几跳，续跑气泡的数字会比暂停气泡那行还小。
    assert agent._frontdoor_turn_usage["T1"]["input_tokens"] == 63290


def test_overwrite_keeps_the_row_a_full_checkpoint_and_repairs_the_downstream_chain():
    old_view = {"stages": [{"stage_id": "s39", "rounds": [1, 2, 3]}]}
    newer_view = {"stages": [{"stage_id": "s39", "rounds": [1, 2, 3, 4]}]}
    session = _FakePersistedSession(
        [
            _paused_user_row("重做一个视频", turn_id="T1"),
            _archive_row(turn_id="T1", view=old_view),
        ]
    )
    downstream = {"role": "assistant", "content": "后续行", "turn_id": "T0", "metadata": {}}
    downstream.update(
        {
            TRANSCRIPT_CC_UPSERT_FIELD: encode_cc_upsert(old_view, newer_view),
            "canonical_context_projection": "delta_window",
        }
    )
    session.messages.append(downstream)
    agent = _build_agent(session)
    before = materialize_transcript_view(session.messages, 2)

    agent._overwrite_archived_paused_assistant_row(
        session,
        index=1,
        assistant_text="新视频任务已派发",
        assistant_payload={"turn_id": "T1", "metadata": {"source": "heartbeat"}},
        projected={"stages": [{"stage_id": "s39", "rounds": [1, 2, 3, 4, 5]}]},
    )

    replaced = session.messages[1]
    assert replaced["content"] == "新视频任务已派发"
    assert "status" not in replaced
    # history_visible 留着会让这条回复进不了模型种子。
    assert "history_visible" not in replaced["metadata"]
    assert replaced["canonical_context_projection"] == TRANSCRIPT_PROJECTION_MODE
    # delta 是相对被替换行自己编码的，就地写会自指。
    assert TRANSCRIPT_CC_UPSERT_FIELD not in replaced
    # 下游行按新链重编码后必须仍然物化出同一份视图，否则替换就在丢历史。
    assert materialize_transcript_view(session.messages, 2) == before


def test_resume_turn_replaces_the_archive_row_instead_of_appending_a_second_one():
    session = _FakePersistedSession([_paused_user_row("重做一个视频", turn_id="T1"), _archive_row(turn_id="T1")])
    agent = _build_agent(session)
    agent._frontdoor_turn_usage = {
        "T1": {"input_tokens": 99786, "output_tokens": 26377, "cache_hit_tokens": 988928, "call_count": 17}
    }
    user_input = _heartbeat_input("shutdown_resume", turn_id="T1")

    asyncio.run(
        agent._persist_turn_transcript(
            user_input=user_input,
            user_text="",
            assistant_text="新视频任务已派发，正在后台执行",
            interaction_flow=[],
            internal_source="heartbeat",
            route_kind="direct_reply",
        )
    )

    assistant_rows = [row for row in session.messages if row.get("role") == "assistant"]
    assert len(assistant_rows) == 1
    assert assistant_rows[0]["content"] == "新视频任务已派发，正在后台执行"
    assert assistant_rows[0]["usage"]["input_tokens"] == 99786
    # 用户行仍留在 paused 会让种子对账在之后每一轮把这条已回答的提问补回请求体尾部。
    assert session.messages[0]["metadata"]["_transcript_state"] == _TRANSCRIPT_STATE_COMPLETED
    assert agent._loop.sessions.save_calls == 1


def test_a_normal_turn_still_appends_when_no_archive_row_is_waiting():
    session = _FakePersistedSession([_paused_user_row("重做一个视频", turn_id="T1")])
    agent = _build_agent(session)
    user_input = _heartbeat_input("task_terminal", turn_id="T2")

    asyncio.run(
        agent._persist_turn_transcript(
            user_input=user_input,
            user_text="",
            assistant_text="任务已完成",
            interaction_flow=[],
            internal_source="heartbeat",
            route_kind="direct_reply",
        )
    )

    assert [row.get("role") for row in session.messages] == ["user", "assistant"]
    assert session.messages[1]["turn_id"] == "T2"
