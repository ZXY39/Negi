"""轮级用量热缓存的封顶：长会话不许按回合数线性涨常驻内存。

累加侧（frontdoor 落 usage 的那一步）与认领侧（`_seed_turn_usage_from_artifacts`）都要
过同一个判据；更早的轮由请求工件给出合计，所以掉旧条目与重启后内存为空是同一种形状。
"""

from __future__ import annotations

from types import MethodType, SimpleNamespace

from g3ku.runtime import session_agent
from g3ku.runtime.session_agent import RuntimeAgentSession, trim_turn_usage_cache


def test_trim_keeps_the_newest_entries_and_leaves_small_maps_alone() -> None:
    usage = {f"t{index}": {"input_tokens": index} for index in range(session_agent._TURN_USAGE_MEMORY_ENTRIES + 25)}

    trim_turn_usage_cache(usage)

    assert len(usage) == session_agent._TURN_USAGE_MEMORY_ENTRIES
    newest = f"t{session_agent._TURN_USAGE_MEMORY_ENTRIES + 24}"
    assert newest in usage
    assert "t0" not in usage

    small = {"only": {"input_tokens": 1}}
    trim_turn_usage_cache(small)
    assert small == {"only": {"input_tokens": 1}}
    trim_turn_usage_cache(None)


def test_seeding_a_resumed_turn_bounds_the_cache_through_the_real_lane(monkeypatch) -> None:
    from g3ku.runtime import web_ceo_sessions

    monkeypatch.setattr(
        web_ceo_sessions,
        "read_session_turn_token_usage",
        lambda _key: {"resumed": {"input_tokens": 7, "output_tokens": 3, "call_count": 2}},
    )
    filler = {f"t{index}": {"input_tokens": index} for index in range(session_agent._TURN_USAGE_MEMORY_ENTRIES + 10)}
    agent = SimpleNamespace(
        _frontdoor_turn_usage=filler,
        _state=SimpleNamespace(session_key="web:shared"),
    )
    agent._bound_turn_usage_cache = MethodType(RuntimeAgentSession._bound_turn_usage_cache, agent)

    RuntimeAgentSession._seed_turn_usage_from_artifacts(agent, "resumed")

    assert agent._frontdoor_turn_usage["resumed"]["input_tokens"] == 7
    assert len(agent._frontdoor_turn_usage) == session_agent._TURN_USAGE_MEMORY_ENTRIES
