"""Anthropic Messages protocol lane tests (P0).

Covers the conversion contracts that differ from OpenAI: top-level system,
tool_result adjacency + same-role merge, input_schema tools, SSE consumption,
usage normalization (cache read/write), tool_choice mapping, and the probe shape.
See work/anthropic-protocol-plan.md.
"""

from __future__ import annotations

import json

import pytest

from g3ku.llm_config.enums import ProtocolAdapter
from g3ku.llm_config.models import ProviderConfigDraft
from g3ku.llm_config.normalization import normalize_draft
from g3ku.llm_config.probe_strategies import _build_openai_fallback_payload, _build_openai_headers
from g3ku.llm_config.template_registry import TemplateRegistry
from g3ku.providers.anthropic_protocol_helpers import (
    _consume_sse,
    _convert_messages,
    _convert_tools,
    _map_stop_reason,
    _normalize_tool_call_id,
)
from g3ku.providers.anthropic_provider import AnthropicProvider, _anthropic_tool_choice
from g3ku.providers.base import normalize_usage_payload
from g3ku.providers.fallback import normalize_forced_function_tool_choice
from g3ku.providers.registry import find_by_model, find_by_name


def _tool_call(call_id: str, name: str = "exec", arguments: str = '{"cmd":"ls"}') -> dict:
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": arguments}}


# --------------------------------------------------------------------------- #
# Message conversion
# --------------------------------------------------------------------------- #

def test_system_extracted_to_top_level():
    system, messages = _convert_messages(
        [{"role": "system", "content": "You are Negi."}, {"role": "user", "content": "hi"}]
    )
    assert system == "You are Negi."
    assert messages[0]["role"] == "user"
    assert all(m["role"] != "system" for m in messages)


def test_assistant_text_and_tool_use():
    _, messages = _convert_messages(
        [
            {"role": "user", "content": "go"},
            {"role": "assistant", "content": "checking", "tool_calls": [_tool_call("call_1")]},
            {"role": "tool", "tool_call_id": "call_1", "content": "done"},
        ]
    )
    assistant = messages[1]
    assert assistant["role"] == "assistant"
    assert {"type": "text", "text": "checking"} in assistant["content"]
    tool_use = [b for b in assistant["content"] if b["type"] == "tool_use"][0]
    assert tool_use["id"] == "call_1"
    assert tool_use["name"] == "exec"
    assert tool_use["input"] == {"cmd": "ls"}


def test_tool_results_merge_into_user_and_come_first():
    _, messages = _convert_messages(
        [
            {"role": "user", "content": "go"},
            {"role": "assistant", "content": "", "tool_calls": [_tool_call("call_1"), _tool_call("call_2")]},
            {"role": "tool", "tool_call_id": "call_1", "content": "r1"},
            {"role": "tool", "tool_call_id": "call_2", "content": "r2"},
            {"role": "user", "content": "next"},
        ]
    )
    # assistant then a single user message holding both tool_results (first) + merged user text.
    assert messages[1]["role"] == "assistant"
    user = messages[2]
    assert user["role"] == "user"
    types = [b["type"] for b in user["content"]]
    assert types[:2] == ["tool_result", "tool_result"]
    assert "text" in types
    assert len(messages) == 3  # consecutive user messages merged


def test_consecutive_same_role_merged():
    _, messages = _convert_messages(
        [{"role": "user", "content": "a"}, {"role": "user", "content": "b"}]
    )
    assert len(messages) == 1
    assert [b["text"] for b in messages[0]["content"]] == ["a", "b"]


def test_empty_messages_and_name_dropped():
    _, messages = _convert_messages(
        [{"role": "user", "content": "", "name": "alice"}, {"role": "user", "content": "real"}]
    )
    assert len(messages) == 1
    assert all("name" not in m for m in messages)
    assert messages[0]["content"] == [{"type": "text", "text": "real"}]


def test_dangling_tool_call_dropped():
    _, messages = _convert_messages(
        [
            {"role": "user", "content": "go"},
            {"role": "assistant", "content": "text", "tool_calls": [_tool_call("call_1")]},
            # no tool result for call_1
        ]
    )
    assistant = messages[1]
    assert all(b["type"] != "tool_use" for b in assistant["content"])
    assert {"type": "text", "text": "text"} in assistant["content"]


def test_orphan_tool_result_dropped():
    _, messages = _convert_messages(
        [
            {"role": "user", "content": "go"},
            {"role": "tool", "tool_call_id": "ghost", "content": "orphan"},
        ]
    )
    assert all(b.get("type") != "tool_result" for m in messages for b in m["content"])


def test_interjection_merges_with_tool_result_still_valid():
    # A user interjection between the tool_use and its result merges into the same user
    # message as the tool_result, which still immediately follows the assistant. This is
    # valid Anthropic: tool_result first, interjection text after, in one user message.
    _, messages = _convert_messages(
        [
            {"role": "user", "content": "go"},
            {"role": "assistant", "content": "", "tool_calls": [_tool_call("call_1")]},
            {"role": "user", "content": "interjection"},
            {"role": "tool", "tool_call_id": "call_1", "content": "r1"},
        ]
    )
    assert messages[1]["role"] == "assistant"
    user = messages[2]
    assert user["role"] == "user"
    assert user["content"][0]["type"] == "tool_result"
    assert user["content"][0]["tool_use_id"] == "call_1"
    assert {"type": "text", "text": "interjection"} in user["content"]


def test_tool_result_after_intervening_assistant_dropped():
    # A new assistant turn between the tool_use and its result breaks adjacency: the
    # result's preceding assistant no longer declares it, so the fail-safe drops it.
    _, messages = _convert_messages(
        [
            {"role": "user", "content": "go"},
            {"role": "assistant", "content": "", "tool_calls": [_tool_call("call_1")]},
            {"role": "user", "content": "interjection"},
            {"role": "assistant", "content": "new turn"},
            {"role": "tool", "tool_call_id": "call_1", "content": "r1"},
        ]
    )
    assert all(b.get("type") != "tool_result" for m in messages for b in m["content"])


# --------------------------------------------------------------------------- #
# Tools
# --------------------------------------------------------------------------- #

def test_tools_use_input_schema_root_object():
    tools = [
        {
            "type": "function",
            "function": {
                "name": "exec",
                "description": "run",
                "parameters": {"type": "object", "properties": {"cmd": {"type": "string"}}, "required": ["cmd"]},
            },
        }
    ]
    converted = _convert_tools(tools)
    assert converted[0]["name"] == "exec"
    schema = converted[0]["input_schema"]
    assert schema["type"] == "object"
    assert schema["properties"] == {"cmd": {"type": "string"}}
    assert schema["required"] == ["cmd"]
    assert "parameters" not in converted[0]


def test_tools_strip_root_combinators():
    tools = [
        {
            "type": "function",
            "function": {
                "name": "t",
                "description": "",
                "parameters": {"anyOf": [{"type": "object", "properties": {"a": {"type": "string"}}}]},
            },
        }
    ]
    schema = _convert_tools(tools)[0]["input_schema"]
    assert "anyOf" not in schema
    assert schema["type"] == "object"


def test_tool_call_id_normalized():
    assert _normalize_tool_call_id("call abc/123") == "call_abc_123"
    assert len(_normalize_tool_call_id("x" * 100)) == 64
    assert _normalize_tool_call_id("toolu_01ABC") == "toolu_01ABC"


# --------------------------------------------------------------------------- #
# tool_choice
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "policy,expected",
    [("auto", {"type": "auto"}), ("required", {"type": "any"}), ("none", {"type": "none"})],
)
def test_tool_choice_string_policies(policy, expected):
    assert _anthropic_tool_choice(policy) == expected


def test_tool_choice_forced_selector():
    forced = {"type": "function", "function": {"name": "exec"}}
    assert _anthropic_tool_choice(forced) == {"type": "tool", "name": "exec"}
    assert normalize_forced_function_tool_choice(forced, protocol="anthropic") == {"type": "tool", "name": "exec"}


# --------------------------------------------------------------------------- #
# stop reason + usage
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "reason,expected",
    [("end_turn", "stop"), ("tool_use", "tool_calls"), ("max_tokens", "length"), ("refusal", "error"), ("pause_turn", "stop")],
)
def test_stop_reason_mapping(reason, expected):
    assert _map_stop_reason(reason) == expected


def test_usage_anthropic_cache_fields():
    usage = normalize_usage_payload(
        {"input_tokens": 100, "output_tokens": 50, "cache_read_input_tokens": 2000, "cache_creation_input_tokens": 300}
    )
    assert usage["input_tokens"] == 100  # Anthropic input excludes cache
    assert usage["cache_hit_tokens"] == 2000
    assert usage["cache_write_tokens"] == 300


def test_usage_openai_regression():
    usage = normalize_usage_payload(
        {"prompt_tokens": 1000, "completion_tokens": 50, "prompt_tokens_details": {"cached_tokens": 800}}
    )
    assert usage["input_tokens"] == 200  # OpenAI prompt includes cache -> subtract
    assert usage["cache_hit_tokens"] == 800
    assert "cache_write_tokens" not in usage


# --------------------------------------------------------------------------- #
# SSE consumption
# --------------------------------------------------------------------------- #

class _MockResponse:
    status_code = 200

    def __init__(self, events):
        self._lines = []
        for event in events:
            self._lines.append(f"event: {event['type']}")
            self._lines.append("data: " + json.dumps(event))
            self._lines.append("")
        self.terminal_seen = False

    async def aiter_lines(self):
        for line in self._lines:
            yield line

    def note_terminal_event(self):
        self.terminal_seen = True


def _text_events(text: str) -> list[dict]:
    return [
        {"type": "message_start", "message": {"id": "msg_1", "usage": {"input_tokens": 5, "output_tokens": 1}}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text}},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 9}},
        {"type": "message_stop"},
    ]


async def test_sse_text_and_usage():
    response = _MockResponse(_text_events("Hello"))
    deltas = []
    content, calls, finish, usage, thinking = await _consume_sse(response, on_text_delta=lambda t: deltas.append(t))
    assert content == "Hello"
    assert calls == []
    assert finish == "stop"
    assert usage["input_tokens"] == 5
    assert usage["output_tokens"] == 9
    assert deltas == ["Hello"]
    assert response.terminal_seen
    assert thinking == []


async def test_sse_tool_call_arguments_assembled():
    events = [
        {"type": "message_start", "message": {"usage": {"input_tokens": 5}}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "tool_use", "id": "toolu_1", "name": "exec", "input": {}}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": '{"cmd":'}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": '"ls"}'}},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": "tool_use"}},
        {"type": "message_stop"},
    ]
    content, calls, finish, _, _ = await _consume_sse(_MockResponse(events))
    assert content == ""
    assert finish == "tool_calls"
    assert len(calls) == 1
    assert calls[0].id == "toolu_1"
    assert calls[0].name == "exec"
    assert calls[0].arguments == {"cmd": "ls"}


async def test_sse_thinking_block_with_signature():
    events = [
        {"type": "message_start", "message": {"usage": {"input_tokens": 5}}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": "", "signature": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "reasoning"}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "signature_delta", "signature": "sig123"}},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn"}},
        {"type": "message_stop"},
    ]
    reasoning_deltas = []
    _, _, _, _, thinking = await _consume_sse(_MockResponse(events), on_reasoning_delta=lambda t: reasoning_deltas.append(t))
    assert thinking == [{"type": "thinking", "thinking": "reasoning", "signature": "sig123"}]
    assert reasoning_deltas == ["reasoning"]


async def test_sse_no_message_stop_usage_still_collected():
    events = _text_events("partial")[:-1]  # drop message_stop
    response = _MockResponse(events)
    content, _, finish, usage, _ = await _consume_sse(response)
    assert content == "partial"
    assert not response.terminal_seen  # caller marks stream_incomplete from this
    assert usage["input_tokens"] == 5


# --------------------------------------------------------------------------- #
# Template / registry / probe / provider
# --------------------------------------------------------------------------- #

def test_anthropic_template_registered():
    template = TemplateRegistry().get_template("anthropic")
    assert template.protocol_adapter == ProtocolAdapter.ANTHROPIC_MESSAGES
    assert template.default_headers.get("anthropic-version") == "2023-06-01"
    auth_header = [f.default for f in template.fields if f.key == "auth_header"][0]
    assert auth_header is False


def test_registry_matches_claude():
    assert find_by_name("anthropic").name == "anthropic"
    assert find_by_model("claude-sonnet-4-5").name == "anthropic"


def _normalized_anthropic():
    draft = ProviderConfigDraft(
        provider_id="anthropic",
        api_key="sk-ant-test",
        base_url="https://api.anthropic.com/v1",
        default_model="claude-sonnet-4-5",
        parameters={"context_window_tokens": 200000},
    )
    normalized, errors = normalize_draft(draft, TemplateRegistry())
    assert not errors
    return normalized


def test_probe_shape_and_headers():
    normalized = _normalized_anthropic()
    endpoint, payload = _build_openai_fallback_payload(normalized)
    assert endpoint == "/messages"
    assert payload["messages"] == [{"role": "user", "content": "ping"}]
    assert payload["thinking"] == {"type": "disabled"}
    assert payload["tools"][0]["input_schema"]["type"] == "object"
    headers = _build_openai_headers(normalized)
    assert headers["x-api-key"] == "sk-ant-test"
    assert headers["anthropic-version"] == "2023-06-01"
    assert "Authorization" not in headers


def test_probe_bearer_when_auth_header_on():
    draft = ProviderConfigDraft(
        provider_id="anthropic",
        api_key="sk-ant-test",
        base_url="https://api.anthropic.com/v1",
        default_model="claude-sonnet-4-5",
        parameters={"context_window_tokens": 200000, "auth_header": True},
    )
    normalized, errors = normalize_draft(draft, TemplateRegistry())
    assert not errors
    headers = _build_openai_headers(normalized)
    assert headers["Authorization"] == "Bearer sk-ant-test"
    assert "x-api-key" not in headers


def test_base_url_messages_suffix_stripped():
    draft = ProviderConfigDraft(
        provider_id="anthropic",
        api_key="sk-ant-test",
        base_url="https://api.anthropic.com/v1/messages",
        default_model="claude-sonnet-4-5",
        parameters={"context_window_tokens": 200000},
    )
    normalized, errors = normalize_draft(draft, TemplateRegistry())
    assert not errors
    assert normalized.base_url == "https://api.anthropic.com/v1"


def test_provider_capabilities():
    provider = AnthropicProvider(api_key="sk-test", api_base="https://api.anthropic.com/v1")
    assert provider.supports_streaming
    assert provider.supports_reasoning_delta_streaming
    assert provider.manages_request_timeout_internally
    assert provider.get_default_model() == "claude-sonnet-4-5"


def test_provider_registration_consistency():
    """每个注册的 provider 模板必须被所有配置守卫接受。

    回归：anthropic 接入时 migration（provider 白名单）/ loader（protocol 白名单）/
    schema（ProvidersConfig 字段）三处硬编码漏改，存量 anthropic 记录让启动直接失败。
    此测试把"模板注册面"与三道守卫钉成一致，未来加协议漏改任一处即红。
    """
    from g3ku.config.schema import ProvidersConfig
    from g3ku.llm_config.enums import ProtocolAdapter
    from g3ku.llm_config.migration import _SUPPORTED_PROVIDER_IDS
    from g3ku.llm_config.provider_snapshots import PROVIDER_TEMPLATES
    from g3ku.providers.registry import PROVIDERS

    template_ids = {t.provider_id for t in PROVIDER_TEMPLATES}
    assert template_ids <= _SUPPORTED_PROVIDER_IDS, "migration provider 白名单漏注册"
    assert template_ids <= set(ProvidersConfig.model_fields.keys()), "ProvidersConfig 缺字段"
    assert template_ids <= {p.name for p in PROVIDERS}, "registry 漏注册"
    enum_adapters = {a.value for a in ProtocolAdapter}
    assert {t.protocol_adapter.value for t in PROVIDER_TEMPLATES} <= enum_adapters
