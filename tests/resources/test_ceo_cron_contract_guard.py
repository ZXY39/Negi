from __future__ import annotations

from types import SimpleNamespace

from g3ku.runtime.frontdoor import _ceo_runtime_ops as ceo_runtime_ops
from g3ku.runtime.frontdoor.ceo_runner import CeoFrontDoorRunner
from g3ku.runtime.frontdoor.cron_hidden_prompt import (
    CRON_HIDDEN_PROMPT,
    split_cron_hidden_prompt,
    strip_cron_hidden_prompt,
)
from g3ku.runtime.frontdoor.message_builder import CeoMessageBuilder


def _runner():
    loop = SimpleNamespace(
        sessions=SimpleNamespace(get_or_create=lambda key: SimpleNamespace(session_key=key, messages=[])),
        main_task_service=None,
        tools={},
        workspace=None,
        temp_dir="",
    )
    return CeoFrontDoorRunner(loop=loop)


def _contract_record():
    return {
        "role": "assistant",
        "content": "## Runtime Tool Contract\nkind: frontdoor_runtime_tool_contract\ncallable_tools: `cron`",
    }


def test_durable_request_body_strips_tool_contract() -> None:
    runner = _runner()
    messages = [
        {"role": "user", "content": "把定时任务改到下午3点"},
        _contract_record(),
        {"role": "assistant", "content": "好的，已改。"},
    ]
    durable = runner._durable_frontdoor_request_body_messages(messages)
    assert all(not runner._is_frontdoor_tool_contract_record(item) for item in durable)
    assert [m["content"] for m in durable] == ["把定时任务改到下午3点", "好的，已改。"]


def test_shrink_guard_quarantines_without_raising() -> None:
    runner = _runner()
    session = SimpleNamespace(session_key="china:qqbot:default:dm", messages=[])
    new_seed = [{"role": "user", "content": "短一点的新种子"}]
    # 旧基线（大）vs 新种子（小）且无允许原因：不应抛错，应写回受控基线。
    runner._quarantine_frontdoor_shrink(
        session, new_seed=new_seed, previous_tokens=10_000, next_tokens=1_000
    )
    assert getattr(session, "_frontdoor_history_shrink_reason", "") == "context_shrink_quarantine"
    assert session._frontdoor_request_body_messages == runner._durable_frontdoor_request_body_messages(new_seed)


def test_strip_cron_hidden_prompt_removes_block() -> None:
    base = "后面两天每天把当天的天气告诉我"
    polluted = f"{base}\n\n{CRON_HIDDEN_PROMPT}"
    assert strip_cron_hidden_prompt(polluted) == base
    assert split_cron_hidden_prompt(polluted)[1] != ""
    assert strip_cron_hidden_prompt(base) == base


def test_history_has_current_user_ignores_cron_prompt() -> None:
    builder = CeoMessageBuilder(loop=SimpleNamespace(), prompt_builder=SimpleNamespace())
    base = "把那个focus研究的定时任务改到下午3点进行"
    polluted_last = {"role": "user", "content": f"{base}\n\n{CRON_HIDDEN_PROMPT}"}
    assert builder._history_has_current_user(
        history_messages=[polluted_last], query_text=base, user_metadata=None
    )
    # 双向都干净也匹配
    assert builder._history_has_current_user(
        history_messages=[{"role": "user", "content": base}], query_text=base, user_metadata=None
    )
    # 真正不同的消息不匹配
    assert not builder._history_has_current_user(
        history_messages=[{"role": "user", "content": base}], query_text="另一条完全不同的消息", user_metadata=None
    )
