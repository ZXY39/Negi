from __future__ import annotations

from types import SimpleNamespace

from g3ku.core.messages import UserInputMessage
from g3ku.runtime.frontdoor._ceo_runtime_ops import CeoFrontDoorRuntimeOps
from g3ku.runtime.session_agent import (
    RuntimeAgentSession,
    _TRANSCRIPT_STATE_COMPLETED,
    _TRANSCRIPT_STATE_PAUSED,
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


def _user_record(text, *, turn_id, state, **metadata_extra):
    metadata = {
        "_transcript_turn_id": turn_id,
        "_transcript_state": state,
    }
    metadata.update(metadata_extra)
    return {"role": "user", "content": text, "metadata": metadata}


def _build_agent(persisted_session):
    loop = SimpleNamespace(
        sessions=_FakeSessionStore(persisted_session),
        model="test-model",
        reasoning_effort=None,
        multi_agent_runner=None,
        prompt_trace=False,
    )
    agent = RuntimeAgentSession(
        loop,
        session_key="china:qqbot:default:dm",
        channel="qqbot",
        chat_id="dm",
    )
    return agent


def test_complete_lingering_paused_user_messages_flips_only_paused_user_entries():
    paused = _user_record("告诉我图片内容", turn_id="turn-a", state=_TRANSCRIPT_STATE_PAUSED)
    completed = _user_record("新问题", turn_id="turn-b", state=_TRANSCRIPT_STATE_COMPLETED)
    assistant = {
        "role": "assistant",
        "content": "回答",
        "metadata": {"_transcript_state": _TRANSCRIPT_STATE_PAUSED},
    }
    session = _FakePersistedSession([paused, assistant, completed])
    agent = _build_agent(session)

    flipped = agent._complete_lingering_paused_user_messages(session)

    assert flipped == 1
    assert session.messages[0]["metadata"]["_transcript_state"] == _TRANSCRIPT_STATE_COMPLETED
    # 助手条目与非 paused 用户条目不受影响
    assert session.messages[1]["metadata"]["_transcript_state"] == _TRANSCRIPT_STATE_PAUSED
    assert session.messages[2]["metadata"]["_transcript_state"] == _TRANSCRIPT_STATE_COMPLETED


def test_persist_turn_transcript_success_path_completes_lingering_paused():
    paused = _user_record("告诉我图片内容", turn_id="turn-a", state=_TRANSCRIPT_STATE_PAUSED)
    session = _FakePersistedSession([paused])
    agent = _build_agent(session)
    user_input = UserInputMessage(content="glm-5.3-flash性能怎么样", metadata={})
    agent._active_user_batch_inputs = [user_input]
    agent._active_batch_id = None
    agent._active_turn_id = None
    agent._last_verified_task_ids = []

    import asyncio

    asyncio.run(
        agent._persist_turn_transcript(
            user_input=user_input,
            user_text="glm-5.3-flash性能怎么样",
            assistant_text="回答内容",
            interaction_flow=[],
            internal_source=None,
            route_kind="dm",
            complete_lingering_paused_turns=True,
        )
    )

    assert session.messages[0]["metadata"]["_transcript_state"] == _TRANSCRIPT_STATE_COMPLETED


def test_persist_turn_transcript_error_path_keeps_paused_entries():
    paused = _user_record("告诉我图片内容", turn_id="turn-a", state=_TRANSCRIPT_STATE_PAUSED)
    session = _FakePersistedSession([paused])
    agent = _build_agent(session)
    user_input = UserInputMessage(content="glm-5.3-flash性能怎么样", metadata={})
    agent._active_user_batch_inputs = [user_input]
    agent._active_batch_id = None
    agent._active_turn_id = None
    agent._last_verified_task_ids = []

    import asyncio

    asyncio.run(
        agent._persist_turn_transcript(
            user_input=user_input,
            user_text="glm-5.3-flash性能怎么样",
            assistant_text="这一轮处理没有完成",
            interaction_flow=[],
            internal_source=None,
            route_kind="dm",
        )
    )

    # 错误路径不得提前退役 paused 条目，否则基线未回写时用户消息会永久消失
    assert session.messages[0]["metadata"]["_transcript_state"] == _TRANSCRIPT_STATE_PAUSED


def test_reconcile_stops_reinjecting_paused_message_after_completion():
    """回归：卡在 paused 的历史用户消息会被 _reconcile_paused_user_turns_into_seed
    反复补到每轮请求种子尾部（紧邻当前用户消息、无助手回复），造成模型重复处理
    已回答的问题；正常完成一轮后该条目退役，不再被补发。"""
    phantom_text = "告诉我图片内容"
    seed_template = [
        {"role": "system", "content": "# 定位"},
        {"role": "assistant", "content": "[G3KU_TOKEN_COMPACT_V2] ..."},
        {"role": "assistant", "content": "好的，立刻停下。"},
    ]

    paused = _user_record(phantom_text, turn_id="turn-a", state=_TRANSCRIPT_STATE_PAUSED)
    session = _FakePersistedSession([paused])

    injected = CeoFrontDoorRuntimeOps._reconcile_paused_user_turns_into_seed(
        list(seed_template),
        session,
        current_turn_user_content="glm-5.3-flash性能怎么样",
    )
    assert [m for m in injected if m.get("content") == phantom_text], (
        "复现前提：卡死的 paused 条目会被补进种子尾部"
    )
    assert injected[-1]["content"] == phantom_text

    agent = _build_agent(session)
    flipped = agent._complete_lingering_paused_user_messages(session)
    assert flipped == 1

    healed = CeoFrontDoorRuntimeOps._reconcile_paused_user_turns_into_seed(
        list(seed_template),
        session,
        current_turn_user_content="glm-5.3-flash性能怎么样",
    )
    assert healed == seed_template, "修复后 paused 条目退役，种子不再被追加幻影消息"


def test_persist_turn_transcript_silent_reply_persists_empty_carrier_row():
    import asyncio

    session = _FakePersistedSession([])
    agent = _build_agent(session)
    user_input = UserInputMessage(content="静默测试", metadata={})
    agent._active_user_batch_inputs = [user_input]
    agent._active_batch_id = None
    agent._active_turn_id = None
    agent._last_verified_task_ids = []

    asyncio.run(
        agent._persist_turn_transcript(
            user_input=user_input,
            user_text="静默测试",
            assistant_text="",
            interaction_flow=[],
            internal_source=None,
            route_kind="dm",
            assistant_metadata={"silent_reply": True, "prompt_visible": False, "ui_visible": True},
            complete_lingering_paused_turns=True,
        )
    )

    assistant_records = [m for m in session.messages if m.get("role") == "assistant"]
    assert assistant_records, "静默回合仍要落一条 assistant 行承载阶段轨道"
    last = assistant_records[-1]
    assert last["content"] == "", "静默回合不得再写可见占位文案"
    assert last["metadata"]["silent_reply"] is True
    assert last["metadata"]["prompt_visible"] is False, "被吞掉的回复不得经转录重放回到模型上下文"


def test_persist_turn_transcript_attaches_frontdoor_turn_usage():
    """轮次 token 用量随 transcript 持久化：请求工件被修剪/重启后，
    历史气泡悬停的 usage 仍有稳定数据源。"""
    session = _FakePersistedSession([])
    agent = _build_agent(session)
    user_input = UserInputMessage(content="问题", metadata={"_transcript_turn_id": "turn-usage"})
    agent._active_user_batch_inputs = [user_input]
    agent._active_batch_id = None
    agent._active_turn_id = "turn-usage"
    agent._last_verified_task_ids = []
    agent._frontdoor_turn_usage = {
        "turn-usage": {
            "input_tokens": 1200,
            "output_tokens": 340,
            "cache_hit_tokens": 800,
            "call_count": 2,
        },
        "other-turn": {
            "input_tokens": 999,
            "output_tokens": 999,
            "cache_hit_tokens": 999,
            "call_count": 9,
        },
    }

    import asyncio

    asyncio.run(
        agent._persist_turn_transcript(
            user_input=user_input,
            user_text="问题",
            assistant_text="回答",
            interaction_flow=[],
            internal_source=None,
            route_kind="dm",
        )
    )

    assistant = [m for m in session.messages if m["role"] == "assistant"][-1]
    assert assistant["turn_id"] == "turn-usage"
    assert assistant["usage"] == {
        "input_tokens": 1200,
        "output_tokens": 340,
        "cache_hit_tokens": 800,
        "call_count": 2,
    }


def test_persist_turn_transcript_skips_empty_turn_usage():
    session = _FakePersistedSession([])
    agent = _build_agent(session)
    user_input = UserInputMessage(content="问题", metadata={"_transcript_turn_id": "turn-zero"})
    agent._active_user_batch_inputs = [user_input]
    agent._active_batch_id = None
    agent._active_turn_id = "turn-zero"
    agent._last_verified_task_ids = []
    agent._frontdoor_turn_usage = {
        "turn-zero": {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_hit_tokens": 0,
            "call_count": 0,
        },
    }

    import asyncio

    asyncio.run(
        agent._persist_turn_transcript(
            user_input=user_input,
            user_text="问题",
            assistant_text="回答",
            interaction_flow=[],
            internal_source=None,
            route_kind="dm",
        )
    )

    assistant = [m for m in session.messages if m["role"] == "assistant"][-1]
    assert "usage" not in assistant
