"""心跳 / cron 内部轮必须自持 turn_id。

frontdoor 的工具账本按 turn_id 切片（`frontdoor_turns_for_turn`），网页再按切片画回合
轨道。内部轮若继承可见轮的 active id，两轮的正文与工具就落在同一个 turn_id 下：静默
回合的轨道会把上一轮的中途输出与工具重画一遍。续跑轮复用被截断回合 id 是另一条显式
车道（`_adopt_interrupted_turn_id_for_resume`），不靠这里的继承。
"""

from __future__ import annotations

from g3ku.core.messages import UserInputMessage
from g3ku.runtime.frontdoor.turns import append_frontdoor_turn, frontdoor_turns_for_turn
from g3ku.runtime.session_agent import RuntimeAgentSession


def _session(active_turn_id: str = "") -> RuntimeAgentSession:
    session = RuntimeAgentSession.__new__(RuntimeAgentSession)
    session._active_turn_id = active_turn_id
    return session


def test_heartbeat_prompt_starts_its_own_turn_id() -> None:
    session = _session("visible-turn")
    heartbeat = UserInputMessage(content="[SESSION EVENTS]", metadata={"heartbeat_internal": True})

    turn_id = session._ensure_user_turn_id(heartbeat)

    assert turn_id and turn_id != "visible-turn"
    assert heartbeat.metadata["_transcript_turn_id"] == turn_id
    # 同一输入重复取值不换号：事件束重投与修复轮认的是同一回合。
    assert session._ensure_user_turn_id(heartbeat) == turn_id
    assert session._current_turn_id(heartbeat) == turn_id


def test_each_heartbeat_turn_gets_a_fresh_turn_id() -> None:
    session = _session("visible-turn")
    first = UserInputMessage(content="a", metadata={"heartbeat_internal": True})
    second = UserInputMessage(content="b", metadata={"heartbeat_internal": True})

    assert session._ensure_user_turn_id(first) != session._ensure_user_turn_id(second)


def test_cron_prompt_starts_its_own_turn_id() -> None:
    session = _session("visible-turn")
    cron = UserInputMessage(content="cron", metadata={"cron_internal": True})

    turn_id = session._ensure_user_turn_id(cron)

    assert turn_id and turn_id != "visible-turn"


def test_visible_prompt_ids_are_untouched_by_the_internal_lane() -> None:
    session = _session("visible-turn")
    prompt = UserInputMessage(content="hi", metadata={})

    assert session._ensure_user_turn_id(prompt, reuse_active=False) not in ("", "visible-turn")


def test_ledger_slices_stay_separate_between_visible_and_internal_turns() -> None:
    # 网页每轮轨道画的就是这一刀切片：上一轮的正文与工具不得落进内部轮那一片。
    session = _session("visible-turn")
    heartbeat = UserInputMessage(content="[SESSION EVENTS]", metadata={"heartbeat_internal": True})
    heartbeat_turn_id = session._ensure_user_turn_id(heartbeat)

    turns: list = []
    turns = append_frontdoor_turn(turns, turn_id="visible-turn", text="用户轮中途输出")
    turns = append_frontdoor_turn(turns, turn_id=heartbeat_turn_id, text="心跳轮中途输出")

    assert [turn["turn_id"] for turn in frontdoor_turns_for_turn(turns, "visible-turn")] == ["visible-turn"]
    assert [turn["turn_id"] for turn in frontdoor_turns_for_turn(turns, heartbeat_turn_id)] == [heartbeat_turn_id]
    assert frontdoor_turns_for_turn(turns, "") == []
