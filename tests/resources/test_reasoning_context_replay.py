"""思考内容进上下车的四条咽喉点：链级闸门、发送白名单、两份投影、LangChain 回环。

契约本体见 docs/architecture/runtime-overview.md「思考内容进上下文」。
"""

from __future__ import annotations

from types import SimpleNamespace

from langchain_core.messages import AIMessage, HumanMessage

from g3ku.providers.base_chat_model_adapter import _as_message_dicts
from g3ku.runtime.frontdoor.ceo_runner import CeoFrontDoorRunner
from g3ku.runtime.frontdoor.message_builder import CeoMessageBuilder
from main.runtime.chat_backend import (
    model_chain_replays_reasoning,
    sanitize_provider_messages,
)
from main.runtime.react_loop import ReActToolLoop


class _StubConfig:
    """只提供 get_model_runtime_profile，判据不该依赖真实 Config。"""

    def __init__(self, profiles: dict[str, object | None]) -> None:
        self._profiles = profiles

    def get_model_runtime_profile(self, model_key: str | None = None):
        return self._profiles.get(str(model_key or "").strip())


def _profile(enabled: bool) -> SimpleNamespace:
    return SimpleNamespace(reasoning_context_enabled=enabled)


def test_chain_predicate_requires_every_ref_to_allow_replay() -> None:
    both_on = _StubConfig({"a": _profile(True), "b": _profile(True)})
    assert model_chain_replays_reasoning(both_on, ["a", "b"]) is True

    one_off = _StubConfig({"a": _profile(True), "b": _profile(False)})
    assert model_chain_replays_reasoning(one_off, ["a", "b"]) is False

    assert model_chain_replays_reasoning(_StubConfig({}), ["a"]) is False
    assert model_chain_replays_reasoning(both_on, []) is False


def test_sanitize_carries_reasoning_only_on_assistant_rows() -> None:
    sanitized = sanitize_provider_messages(
        [
            {"role": "user", "content": "继续", "reasoning_content": "不该出现"},
            {
                "role": "assistant",
                "content": "先看索引",
                "reasoning_content": "我要再核一遍来源",
                "tool_calls": [
                    {"id": "call_1", "type": "function", "function": {"name": "exec", "arguments": "{}"}}
                ],
            },
            {"role": "assistant", "content": "空思考", "reasoning_content": "   "},
        ]
    )

    assert sanitized[0] == {"role": "user", "content": "继续"}
    assert sanitized[1]["reasoning_content"] == "我要再核一遍来源"
    assert "reasoning_content" not in sanitized[2]
    # 咽喉点必须幂等：诊断与线上体走同一个投影，第二次过不能把字段洗掉。
    assert sanitize_provider_messages(sanitized) == sanitized


def test_durable_projections_preserve_reasoning_in_place() -> None:
    rows = [
        {
            "role": "assistant",
            "content": "先看索引",
            "reasoning_content": "我要再核一遍来源",
            "tool_calls": [
                {"id": "call_1", "type": "function", "function": {"name": "exec", "arguments": "{}"}}
            ],
        },
        {"role": "assistant", "content": "正文", "reasoning_content": "只想了一段"},
        {"role": "user", "content": "继续"},
    ]

    seed = CeoMessageBuilder._request_body_seed_records(rows)
    assert [item.get("reasoning_content") for item in seed] == ["我要再核一遍来源", "只想了一段", None]

    prompt = CeoMessageBuilder._prompt_message_records(rows)
    assert prompt[0]["reasoning_content"] == "我要再核一遍来源"
    # 只有思考、没有正文也没有工具调用的行仍按现状丢弃（计划 §2.2 第 1 条）。
    assert (
        CeoMessageBuilder._prompt_message_records(
            [{"role": "assistant", "content": "", "reasoning_content": "半截思考"}]
        )
        == []
    )


def test_adapter_round_trip_reattaches_reasoning() -> None:
    dicts = _as_message_dicts(
        [
            HumanMessage(content="继续"),
            AIMessage(
                content="先看索引",
                additional_kwargs={"reasoning_content": "我要再核一遍来源"},
            ),
        ]
    )

    assert "reasoning_content" not in dicts[0]
    assert dicts[1]["reasoning_content"] == "我要再核一遍来源"


def _runner() -> CeoFrontDoorRunner:
    return CeoFrontDoorRunner(loop=SimpleNamespace())


def test_gate_verdict_travels_from_response_to_history_row() -> None:
    runner = _runner()
    message = AIMessage(
        content="先看索引",
        additional_kwargs={"reasoning_content": "我要再核一遍来源", "reasoning_context_allowed": True},
    )

    payload = runner._checkpoint_safe_model_response_payload(message)
    assert payload["reasoning_context_allowed"] is True

    view = runner._model_response_view(payload)
    assert view.reasoning_context_allowed is True

    assert runner._frontdoor_assistant_reasoning_field(payload) == {
        "reasoning_content": "我要再核一遍来源"
    }

    blocked = dict(payload, reasoning_context_allowed=False)
    assert runner._frontdoor_assistant_reasoning_field(blocked) == {}
    blank = dict(payload, reasoning_content="   ")
    assert runner._frontdoor_assistant_reasoning_field(blank) == {}


def test_node_row_helper_skips_disallowed_and_externalized_rows() -> None:
    helper = ReActToolLoop._node_assistant_reasoning_field
    allowed = SimpleNamespace(reasoning_content="我要再核一遍来源", reasoning_context_allowed=True)
    blocked = SimpleNamespace(reasoning_content="我要再核一遍来源", reasoning_context_allowed=False)
    body = "先看索引"

    # 外置返回的是信封，不是原对象 ⇒ 指针行不挂思考。
    assert helper(None, allowed, body, body) == {"reasoning_content": "我要再核一遍来源"}
    assert helper(None, blocked, body, body) == {}
    assert helper(None, allowed, body, "[内容已外置] ref=/artifact.json") == {}
    assert (
        helper(
            None,
            SimpleNamespace(reasoning_content="", reasoning_context_allowed=True),
            body,
            body,
        )
        == {}
    )
