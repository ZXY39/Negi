from __future__ import annotations

from datetime import UTC, datetime
import importlib
from types import SimpleNamespace

import pytest
from langchain_core.messages import HumanMessage
from langchain_core.messages.utils import convert_to_messages

import main.runtime.chat_backend as chat_backend_module
import g3ku.providers.fallback as fallback_module
from g3ku.providers.fallback import DEFAULT_PROVIDER_ATTEMPT_TIMEOUT_SECONDS
from g3ku.agent.tools.base import Tool
from g3ku.agent.tools.registry import ToolRegistry
from g3ku.llm_config.enums import AuthMode, Capability, ProtocolAdapter
from g3ku.llm_config.migration import migrate_raw_config_if_needed
from g3ku.llm_config.models import NormalizedProviderConfig, ProviderConfigDraft
from g3ku.llm_config.normalization import normalize_draft
from g3ku.llm_config.template_registry import TemplateRegistry
from g3ku.providers.base import LLMResponse, ToolCallRequest
from g3ku.providers.base_chat_model_adapter import G3kuChatModelAdapter
from g3ku.providers.provider_factory import ProviderTarget
from g3ku.runtime.frontdoor._ceo_support import _DirectProviderChatBackend
from main.runtime.internal_tools import SubmitFinalResultTool
from main.runtime.react_loop import ReActToolLoop


class _FakeTaskStore:
    def __init__(self) -> None:
        self._task = SimpleNamespace(cancel_requested=False, pause_requested=False)
        self._node = None

    def get_task(self, task_id: str):
        _ = task_id
        return self._task

    def get_node(self, node_id: str):
        _ = node_id
        return self._node


class _FakeLogService:
    def __init__(self) -> None:
        self._store = _FakeTaskStore()
        self._content_store = None

    def set_pause_state(self, task_id: str, pause_requested: bool, is_paused: bool) -> None:
        _ = task_id, pause_requested, is_paused

    def update_node_input(self, *args, **kwargs) -> None:
        _ = args, kwargs

    def upsert_frame(self, *args, **kwargs) -> None:
        _ = args, kwargs

    def append_node_output(self, *args, **kwargs) -> None:
        _ = args, kwargs

    def update_frame(self, *args, **kwargs) -> None:
        _ = args, kwargs

    def remove_frame(self, *args, **kwargs) -> None:
        _ = args, kwargs

    def read_runtime_frame(self, *args, **kwargs):
        _ = args, kwargs
        return None


def _submit_final_result_tool(*, node_kind: str = "execution") -> SubmitFinalResultTool:
    async def _submit(payload: dict[str, object]) -> dict[str, object]:
        return dict(payload)

    return SubmitFinalResultTool(_submit, node_kind=node_kind)


class _NestedSchemaTool(Tool):
    @property
    def name(self) -> str:
        return "nested_schema_tool"

    @property
    def description(self) -> str:
        return "preserve nested schema"

    @property
    def parameters(self) -> dict[str, object]:
        return {
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "description": "Nested items to preserve.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "kind": {
                                "type": "string",
                                "enum": ["profile", "preference"],
                                "description": "Nested enum field.",
                            },
                            "value": {
                                "type": "string",
                                "description": "Nested value field.",
                            },
                        },
                        "required": ["kind", "value"],
                    },
                }
            },
            "required": ["items"],
        }

    async def execute(self, items: list[dict[str, object]], **kwargs) -> str:
        _ = kwargs
        return str(items)


def test_template_marks_model_parameters_optional_and_removes_timeout() -> None:
    template = TemplateRegistry().get_template("custom")
    fields = {field.key: field for field in template.fields}

    assert "timeout_s" not in fields
    assert fields["max_tokens"].required is False
    assert fields["max_tokens"].default is None
    assert fields["temperature"].required is False
    assert fields["temperature"].default is None
    assert fields["reasoning_effort"].required is False
    assert fields["reasoning_effort"].default is None


def test_default_provider_attempt_timeout_seconds_is_sixty_seconds() -> None:
    fallback_runtime = importlib.reload(fallback_module)
    chat_backend_runtime = importlib.reload(chat_backend_module)
    backend = chat_backend_runtime.ConfigChatBackend(config=SimpleNamespace())

    assert fallback_runtime.DEFAULT_PROVIDER_ATTEMPT_TIMEOUT_SECONDS == 60.0
    assert backend._model_attempt_timeout_seconds == 60.0


def test_normalize_draft_omits_optional_model_parameters_when_left_blank() -> None:
    registry = TemplateRegistry()
    draft = ProviderConfigDraft(
        provider_id="custom",
        api_key="demo-key",
        base_url="https://example.com/v1",
        default_model="custom-model",
        parameters={
            "api_mode": "custom-direct",
            "context_window_tokens": 200_000,
            "max_tokens": None,
            "temperature": "",
            "reasoning_effort": "",
        },
    )

    normalized, errors = normalize_draft(draft, registry)

    assert errors == []
    assert normalized is not None
    assert "max_tokens" not in normalized.parameters
    assert "temperature" not in normalized.parameters
    assert "reasoning_effort" not in normalized.parameters
    assert "timeout_s" not in normalized.parameters
    assert normalized.parameters["api_mode"] == "custom-direct"
    assert normalized.parameters["context_window_tokens"] == 200_000


def test_migration_cleans_legacy_default_model_parameters_from_saved_records(tmp_path) -> None:
    from g3ku.llm_config.facade import LLMConfigFacade

    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    facade = LLMConfigFacade(workspace)
    now = datetime.now(UTC)
    facade.repository.save(
        NormalizedProviderConfig(
            config_id="cfg-legacy",
            provider_id="custom",
            display_name="Custom",
            protocol_adapter=ProtocolAdapter.CUSTOM_DIRECT,
            capability=Capability.CHAT,
            auth_mode=AuthMode.API_KEY,
            base_url="https://example.com/v1",
            default_model="custom-model",
            auth={"type": "api_key", "api_key": "demo-key"},
            parameters={
                "api_mode": "custom-direct",
                "max_tokens": 4096,
                "temperature": 0.2,
                "timeout_s": 8,
            },
            headers={},
            extra_options={},
            template_version="test",
            created_at=now,
            updated_at=now,
        ),
        last_probe_status="success",
    )

    _, changed = migrate_raw_config_if_needed({"models": {"catalog": []}}, workspace=workspace)
    record = facade.repository.get("cfg-legacy")

    assert changed is True
    assert record.parameters == {"api_mode": "custom-direct"}


@pytest.mark.asyncio
async def test_config_chat_backend_uses_config_center_model_parameters_when_not_overridden(monkeypatch) -> None:
    captured: list[dict[str, object]] = []

    class _RecorderProvider:
        async def chat(self, **kwargs):
            captured.append(dict(kwargs))
            return LLMResponse(content="ok", finish_reason="stop")

    monkeypatch.setattr(
        chat_backend_module,
        "build_provider_from_model_key",
        lambda config, ref, api_key_index=None: ProviderTarget(
            provider_ref=str(ref),
            provider_id="custom",
            model_id="custom-model",
            provider=_RecorderProvider(),
            model_parameters={
                "max_tokens": 4096,
                "temperature": 0.6,
                "reasoning_effort": "high",
            },
            retry_on=[],
            retry_count=0,
            api_key_count=0,
        ),
    )

    backend = chat_backend_module.ConfigChatBackend(config=SimpleNamespace())
    await backend.chat(
        messages=[{"role": "user", "content": "demo"}],
        tools=None,
        model_refs=["primary"],
    )

    assert captured == [
        {
            "messages": [{"role": "user", "content": "demo"}],
            "tools": None,
            "model": "custom-model",
            "tool_choice": "auto",
            "parallel_tool_calls": None,
            "prompt_cache_key": captured[0]["prompt_cache_key"],
            "request_timeout_seconds": DEFAULT_PROVIDER_ATTEMPT_TIMEOUT_SECONDS,
            "max_tokens": 4096,
            "temperature": 0.6,
            "reasoning_effort": "high",
        }
    ]


@pytest.mark.asyncio
async def test_config_chat_backend_omits_optional_model_parameters_when_unset(monkeypatch) -> None:
    captured: list[dict[str, object]] = []

    class _RecorderProvider:
        async def chat(self, **kwargs):
            captured.append(dict(kwargs))
            return LLMResponse(content="ok", finish_reason="stop")

    monkeypatch.setattr(
        chat_backend_module,
        "build_provider_from_model_key",
        lambda config, ref, api_key_index=None: ProviderTarget(
            provider_ref=str(ref),
            provider_id="custom",
            model_id="custom-model",
            provider=_RecorderProvider(),
            retry_on=[],
            retry_count=0,
            api_key_count=0,
        ),
    )

    backend = chat_backend_module.ConfigChatBackend(config=SimpleNamespace())
    await backend.chat(
        messages=[{"role": "user", "content": "demo"}],
        tools=None,
        model_refs=["primary"],
    )

    assert "max_tokens" not in captured[0]
    assert "temperature" not in captured[0]
    assert "reasoning_effort" not in captured[0]
    assert captured[0]["request_timeout_seconds"] == DEFAULT_PROVIDER_ATTEMPT_TIMEOUT_SECONDS


@pytest.mark.asyncio
async def test_config_chat_backend_sanitizes_internal_runtime_message_fields_before_provider_call(monkeypatch) -> None:
    captured: list[dict[str, object]] = []

    class _RecorderProvider:
        async def chat(self, **kwargs):
            captured.append(dict(kwargs))
            return LLMResponse(content="ok", finish_reason="stop")

    monkeypatch.setattr(
        chat_backend_module,
        "build_provider_from_model_key",
        lambda config, ref, api_key_index=None: ProviderTarget(
            provider_ref=str(ref),
            provider_id="custom",
            model_id="custom-model",
            provider=_RecorderProvider(),
            retry_on=[],
            retry_count=0,
            api_key_count=0,
        ),
    )

    original_messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "demo"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "content", "arguments": '{"action":"search"}'},
                    "status": "running",
                }
            ],
            "started_at": "2026-04-07T18:00:00+08:00",
        },
        {
            "role": "tool",
            "tool_call_id": "call-1",
            "name": "content",
            "content": "Error: duplicate read-only request",
            "started_at": "2026-04-07T18:00:01+08:00",
            "finished_at": "2026-04-07T18:00:02+08:00",
            "elapsed_seconds": 1.0,
            "status": "error",
            "ephemeral": True,
        },
    ]

    backend = chat_backend_module.ConfigChatBackend(config=SimpleNamespace())
    await backend.chat(
        messages=original_messages,
        tools=None,
        model_refs=["primary"],
    )

    assert original_messages[2]["started_at"] == "2026-04-07T18:00:00+08:00"
    assert original_messages[3]["status"] == "error"
    assert captured[0]["messages"] == [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "demo"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "content", "arguments": '{"action":"search"}'},
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call-1",
            "name": "content",
            "content": "Error: duplicate read-only request",
        },
    ]


def test_sanitize_provider_messages_preserves_langchain_style_tool_call_args_for_round_trip() -> None:
    sanitized = chat_backend_module.sanitize_provider_messages(
        [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call-1",
                        "name": "submit_next_stage",
                        "args": {
                            "stage_goal": "create markdown file",
                            "tool_round_budget": 5,
                        },
                        "type": "tool_call",
                    }
                ],
            }
        ]
    )

    assert sanitized == [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {
                        "name": "submit_next_stage",
                        "arguments": {
                            "stage_goal": "create markdown file",
                            "tool_round_budget": 5,
                        },
                    },
                }
            ],
        }
    ]
    assert convert_to_messages(sanitized)[0].tool_calls == [
        {
            "name": "submit_next_stage",
            "args": {
                "stage_goal": "create markdown file",
                "tool_round_budget": 5,
            },
            "id": "call-1",
            "type": "tool_call",
        }
    ]


@pytest.mark.asyncio
async def test_config_chat_backend_recommends_dynamic_hard_timeout_from_model_chain_budget(monkeypatch) -> None:
    chat_backend_runtime = importlib.reload(chat_backend_module)

    class _Provider:
        async def chat(self, **kwargs):
            _ = kwargs
            return LLMResponse(content="ok", finish_reason="stop")

    targets = {
        "primary": ProviderTarget(
            provider_ref="primary",
            provider_id="custom",
            model_id="primary-model",
            provider=_Provider(),
            retry_on=["network", "429", "5xx"],
            retry_count=0,
            api_key_count=3,
            api_key_indexes=[0, 1, 2],
        ),
        "secondary": ProviderTarget(
            provider_ref="secondary",
            provider_id="responses",
            model_id="secondary-model",
            provider=_Provider(),
            retry_on=["network", "429", "5xx"],
            retry_count=10,
            api_key_count=1,
            api_key_indexes=[0],
        ),
    }

    monkeypatch.setattr(
        chat_backend_runtime,
        "build_provider_from_model_key",
        lambda config, ref, api_key_index=None: targets[str(ref)],
    )

    backend = chat_backend_runtime.ConfigChatBackend(config=SimpleNamespace())

    assert backend.recommended_model_response_timeout_seconds(model_refs=["primary", "secondary"]) == 8415.0


@pytest.mark.asyncio
async def test_react_loop_no_longer_passes_hardcoded_model_parameter_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import main.runtime.react_loop as react_loop_module
    from main.runtime.chat_backend import SendModelContextWindowInfo

    captured: list[dict[str, object]] = []

    monkeypatch.setattr(
        react_loop_module,
        "get_runtime_config",
        lambda **_: (SimpleNamespace(), 0, False),
        raising=False,
    )
    monkeypatch.setattr(
        react_loop_module.runtime_chat_backend,
        "resolve_send_model_context_window_info",
        lambda **_: SendModelContextWindowInfo(
            model_key="fake",
            provider_id="test",
            provider_model="test:fake",
            resolved_model="fake",
            context_window_tokens=32000,
            resolution_error="",
        ),
        raising=False,
    )

    class _Backend:
        async def chat(self, **kwargs):
            captured.append(dict(kwargs))
            return LLMResponse(
                content="",
                tool_calls=[
                    ToolCallRequest(
                        id="call:final",
                        name="submit_final_result",
                        arguments={
                            "status": "success",
                            "delivery_status": "final",
                            "summary": "done",
                            "answer": "done",
                            "evidence": [],
                            "remaining_work": [],
                            "blocking_reason": "",
                        },
                    )
                ],
                finish_reason="tool_calls",
            )

    loop = ReActToolLoop(chat_backend=_Backend(), log_service=_FakeLogService(), max_iterations=2)
    result = await loop.run(
        task=SimpleNamespace(task_id="task-1"),
        node=SimpleNamespace(node_id="node-1", depth=0, node_kind="execution"),
        messages=[
            {"role": "system", "content": "system"},
            {"role": "user", "content": '{"task_id":"task-1","goal":"demo"}'},
        ],
        tools={"submit_final_result": _submit_final_result_tool()},
        model_refs=["fake"],
        runtime_context={"task_id": "task-1", "node_id": "node-1"},
        max_iterations=2,
    )

    assert result.status == "success"
    assert "max_tokens" not in captured[0]
    assert "temperature" not in captured[0]


def test_react_loop_uses_backend_recommended_hard_timeout_when_unset() -> None:
    loop = ReActToolLoop(
        chat_backend=SimpleNamespace(
            recommended_model_response_timeout_seconds=lambda *, model_refs: 33.0
        ),
        log_service=_FakeLogService(),
        max_iterations=2,
    )

    assert loop._resolved_model_response_timeout_seconds(model_refs=["primary"]) == 33.0


def test_react_loop_prefers_explicit_hard_timeout_override_over_backend_budget() -> None:
    loop = ReActToolLoop(
        chat_backend=SimpleNamespace(
            recommended_model_response_timeout_seconds=lambda *, model_refs: 99.0
        ),
        log_service=_FakeLogService(),
        max_iterations=2,
    )
    loop._model_response_timeout_seconds = 0.25

    assert loop._resolved_model_response_timeout_seconds(model_refs=["primary"]) == 0.25


@pytest.mark.asyncio
async def test_direct_provider_chat_backend_omits_optional_model_parameters_when_unset() -> None:
    captured: list[dict[str, object]] = []

    class _Provider:
        async def chat(self, **kwargs):
            captured.append(dict(kwargs))
            return LLMResponse(content="ok", finish_reason="stop")

    backend = _DirectProviderChatBackend(provider=_Provider())
    await backend.chat(
        messages=[{"role": "user", "content": "demo"}],
        tools=None,
        model_refs=["custom:model"],
    )

    assert "max_tokens" not in captured[0]
    assert "temperature" not in captured[0]
    assert "reasoning_effort" not in captured[0]


@pytest.mark.asyncio
async def test_direct_provider_chat_backend_sanitizes_internal_runtime_message_fields_before_provider_call() -> None:
    captured: list[dict[str, object]] = []

    class _Provider:
        async def chat(self, **kwargs):
            captured.append(dict(kwargs))
            return LLMResponse(content="ok", finish_reason="stop")

    backend = _DirectProviderChatBackend(provider=_Provider())
    original_messages = [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "filesystem", "arguments": '{"action":"list"}'},
                    "status": "queued",
                }
            ],
            "finished_at": "2026-04-07T18:00:05+08:00",
        },
        {
            "role": "tool",
            "tool_call_id": "call-1",
            "name": "filesystem",
            "content": "FILE a.txt",
            "started_at": "2026-04-07T18:00:01+08:00",
            "finished_at": "2026-04-07T18:00:02+08:00",
            "elapsed_seconds": 1.0,
            "status": "success",
        },
    ]

    await backend.chat(
        messages=original_messages,
        tools=None,
        model_refs=["custom:model"],
    )

    assert original_messages[0]["finished_at"] == "2026-04-07T18:00:05+08:00"
    assert captured[0]["messages"] == [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "filesystem", "arguments": '{"action":"list"}'},
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call-1",
            "name": "filesystem",
            "content": "FILE a.txt",
        },
    ]


@pytest.mark.asyncio
async def test_g3ku_chat_model_adapter_preserves_nested_tool_schema_when_sending_tools() -> None:
    captured: list[dict[str, object]] = []

    class _Backend:
        async def chat(self, **kwargs):
            captured.append(dict(kwargs))
            return LLMResponse(content="ok", finish_reason="stop")

    registry = ToolRegistry()
    registry.register(_NestedSchemaTool())
    tool = registry.to_langchain_tools_filtered(["nested_schema_tool"])[0]
    adapter = G3kuChatModelAdapter(chat_backend=_Backend(), default_model="demo:model")

    await adapter._agenerate(
        [HumanMessage(content="hello")],
        tools=[tool],
    )

    tool_payload = dict(captured[0]["tools"][0]["function"])
    parameters = dict(tool_payload["parameters"])
    items_schema = dict((parameters.get("properties") or {}).get("items") or {})
    nested_item = dict(items_schema.get("items") or {})
    nested_properties = dict(nested_item.get("properties") or {})

    assert parameters.get("required") == ["items"]
    assert nested_item.get("required") == ["kind", "value"]
    assert dict(nested_properties.get("kind") or {}).get("enum") == ["profile", "preference"]


@pytest.mark.asyncio
async def test_g3ku_chat_model_adapter_preserves_provider_request_payload_metadata() -> None:
    class _Backend:
        async def chat(self, **kwargs):
            _ = kwargs
            return LLMResponse(
                content="ok",
                finish_reason="stop",
                provider_request_meta={
                    "provider": "responses",
                    "endpoint": "https://example.test/v1/responses",
                },
                provider_request_body={
                    "model": "gpt-5.4-mini",
                    "input": [{"role": "user", "content": [{"type": "input_text", "text": "hello"}]}],
                    "tool_choice": "auto",
                },
            )

    adapter = G3kuChatModelAdapter(chat_backend=_Backend(), default_model="demo:model")

    result = await adapter._agenerate([HumanMessage(content="hello")])
    message = result.generations[0].message

    assert message.response_metadata["provider_request_meta"] == {
        "provider": "responses",
        "endpoint": "https://example.test/v1/responses",
    }
    assert message.response_metadata["provider_request_body"] == {
        "model": "gpt-5.4-mini",
        "input": [{"role": "user", "content": [{"type": "input_text", "text": "hello"}]}],
        "tool_choice": "auto",
    }

@pytest.mark.asyncio
async def test_g3ku_chat_model_adapter_forwards_text_delta_callback() -> None:
    captured: list[dict[str, object]] = []
    deltas: list[str] = []

    class _Backend:
        async def chat(self, **kwargs):
            captured.append(dict(kwargs))
            callback = kwargs.get("on_text_delta")
            if callable(callback):
                callback("O")
                callback("K")
            return LLMResponse(content="OK", finish_reason="stop")

    adapter = G3kuChatModelAdapter(chat_backend=_Backend(), default_model="demo:model")

    result = await adapter._agenerate(
        [HumanMessage(content="hello")],
        on_text_delta=deltas.append,
    )

    assert result.generations[0].message.content == "OK"
    assert callable(captured[0]["on_text_delta"])
    assert deltas == ["O", "K"]
def test_runtime_send_token_preflight_threshold_math_is_ceo_aligned() -> None:
    """
    Regression guard: node/runtime send-side token preflight must share the CEO threshold semantics:
    - trigger_tokens = int(context_window_tokens * 0.80)
    - effective_trigger_tokens = int(trigger_tokens * 0.95)
    - triggers when estimated_total_tokens >= effective_trigger_tokens
    """

    from main.runtime.send_token_preflight import (
        compute_runtime_send_token_preflight_thresholds,
        should_trigger_runtime_token_compression,
    )

    # Use a non-round context window to ensure the behavior is int() truncation, not rounding.
    context_window_tokens = 100_001
    thresholds = compute_runtime_send_token_preflight_thresholds(
        context_window_tokens=context_window_tokens,
    )

    assert thresholds.context_window_tokens == context_window_tokens
    assert thresholds.trigger_tokens == int(context_window_tokens * 0.80)
    assert thresholds.effective_trigger_tokens == int(thresholds.trigger_tokens * 0.95)

    assert should_trigger_runtime_token_compression(
        estimated_total_tokens=thresholds.effective_trigger_tokens - 1,
        thresholds=thresholds,
    ) is False
    assert should_trigger_runtime_token_compression(
        estimated_total_tokens=thresholds.effective_trigger_tokens,
        thresholds=thresholds,
    ) is True


def test_runtime_send_token_preflight_snapshot_keeps_compression_trigger_true_when_over_window() -> None:
    from main.runtime.send_token_preflight import build_runtime_send_token_preflight_snapshot

    snapshot = build_runtime_send_token_preflight_snapshot(
        context_window_tokens=25_001,
        estimated_total_tokens=25_512,
    )

    assert snapshot.would_exceed_context_window is True
    assert snapshot.would_trigger_token_compression is True
    assert snapshot.effective_trigger_tokens == int(int(25_001 * 0.80) * 0.95)


def test_runtime_observed_input_truth_uses_input_plus_cache_hit_tokens() -> None:
    from main.runtime.send_token_preflight import build_runtime_observed_input_truth

    truth = build_runtime_observed_input_truth(
        usage={"input_tokens": 12000, "cache_hit_tokens": 3400},
        provider_model="responses:gpt-5.4",
        actual_request_hash="req-hash",
        source="provider_usage",
    )

    assert truth.input_tokens == 12000
    assert truth.cache_hit_tokens == 3400
    assert truth.effective_input_tokens == 15400
    assert truth.provider_model == "responses:gpt-5.4"
    assert truth.actual_request_hash == "req-hash"
    assert truth.source == "provider_usage"


def test_runtime_observed_input_truth_accepts_cache_read_tokens_alias() -> None:
    from main.runtime.send_token_preflight import build_runtime_observed_input_truth

    truth = build_runtime_observed_input_truth(
        usage={"input_tokens": 8000, "cache_read_tokens": 2200},
        provider_model="custom:model",
        actual_request_hash="req-hash",
        source="provider_usage",
    )

    assert truth.input_tokens == 8000
    assert truth.cache_hit_tokens == 2200
    assert truth.effective_input_tokens == 10200


def test_normalize_usage_payload_treats_nested_input_cached_tokens_as_breakdown() -> None:
    from g3ku.providers.base import normalize_usage_payload

    normalized = normalize_usage_payload(
        {
            "input_tokens": 57986,
            "output_tokens": 125,
            "input_tokens_details": {
                "cached_tokens": 56064,
            },
        }
    )

    assert normalized == {
        "input_tokens": 1922,
        "output_tokens": 125,
        "cache_hit_tokens": 56064,
    }


def test_normalize_usage_payload_treats_nested_prompt_cached_tokens_as_breakdown() -> None:
    from g3ku.providers.base import normalize_usage_payload

    normalized = normalize_usage_payload(
        {
            "prompt_tokens": 2006,
            "completion_tokens": 300,
            "prompt_tokens_details": {
                "cached_tokens": 1920,
            },
        }
    )

    assert normalized == {
        "input_tokens": 86,
        "output_tokens": 300,
        "cache_hit_tokens": 1920,
    }


def test_normalize_usage_payload_keeps_cache_read_alias_as_separate_input_lane() -> None:
    from g3ku.providers.base import normalize_usage_payload

    normalized = normalize_usage_payload(
        {
            "input_tokens": 8000,
            "output_tokens": 250,
            "cache_read_tokens": 2200,
        }
    )

    assert normalized == {
        "input_tokens": 8000,
        "output_tokens": 250,
        "cache_hit_tokens": 2200,
    }


def test_runtime_hybrid_estimate_prefers_conservative_upper_bound() -> None:
    from main.runtime.send_token_preflight import build_runtime_hybrid_send_token_estimate

    estimate = build_runtime_hybrid_send_token_estimate(
        preview_estimate_tokens=12840,
        previous_effective_input_tokens=20313,
        delta_estimate_tokens=1800,
        comparable_to_previous_request=True,
    )

    assert estimate.preview_estimate_tokens == 12840
    assert estimate.usage_based_estimate_tokens == 22113
    assert estimate.delta_estimate_tokens == 1800
    assert estimate.final_estimate_tokens == 22113
    assert estimate.estimate_source == "usage_plus_delta"
    assert estimate.comparable_to_previous_request is True


def test_runtime_request_preview_breakdown_omits_inline_image_data_urls_from_text_estimate() -> None:
    from main.runtime.send_token_preflight import estimate_runtime_provider_request_token_breakdown

    png_data_url = (
        "data:image/png;base64,"
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a6bQAAAAASUVORK5CYII="
    )
    provider_request_body = {
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "describe the attached image"},
                    {"type": "image_url", "image_url": {"url": png_data_url}},
                ],
            }
        ],
        "tools": [{"type": "function", "function": {"name": "demo", "parameters": {"type": "object"}}}],
    }

    breakdown = estimate_runtime_provider_request_token_breakdown(
        provider_request_body=provider_request_body,
        request_messages=[],
        tool_schemas=[],
    )

    assert breakdown["estimated_image_tokens"] > 0
    assert breakdown["image_count"] == 1
    assert breakdown["image_estimation_method"] == "openai_vision_heuristic"
    # Regression guard: the text lane should not scale with the raw base64 payload size.
    assert breakdown["estimated_text_tokens"] < 500
    assert breakdown["estimated_total_tokens"] < 1000


def test_frontdoor_token_preflight_re_exports_ground_truth_helpers() -> None:
    from g3ku.runtime.frontdoor.token_preflight_compaction import (
        RuntimeHybridSendTokenEstimate,
        RuntimeObservedInputTruth,
        build_runtime_hybrid_send_token_estimate,
        build_runtime_observed_input_truth,
    )

    truth = build_runtime_observed_input_truth(
        usage={"input_tokens": 10, "cache_hit_tokens": 4},
        provider_model="demo:model",
        actual_request_hash="req",
        source="provider_usage",
    )
    estimate = build_runtime_hybrid_send_token_estimate(
        preview_estimate_tokens=8,
        previous_effective_input_tokens=14,
        delta_estimate_tokens=3,
        comparable_to_previous_request=True,
    )

    assert isinstance(truth, RuntimeObservedInputTruth)
    assert truth.effective_input_tokens == 14
    assert isinstance(estimate, RuntimeHybridSendTokenEstimate)
    assert estimate.final_estimate_tokens == 17


def test_frontdoor_token_preflight_policy_preserves_invalid_context_window_as_zero() -> None:
    from g3ku.runtime.frontdoor._ceo_runtime_ops import (
        _FrontdoorTokenPreflightPolicy,
        _build_frontdoor_token_preflight_policy,
    )

    policy = _build_frontdoor_token_preflight_policy(
        max_context_tokens=0,
        trigger_ratio=0.8,
    )

    assert isinstance(policy, _FrontdoorTokenPreflightPolicy)
    assert policy.max_context_tokens == 0
    assert policy.trigger_tokens == 0


def test_build_send_provider_request_preview_sanitizes_messages_and_synthesizes_prompt_cache_key(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        chat_backend_module,
        "build_provider_from_model_key",
        lambda config, ref, api_key_index=None: ProviderTarget(
            provider_ref=str(ref),
            provider_id="custom",
            model_id="custom-model",
            provider=SimpleNamespace(),
            model_parameters={"context_window_tokens": 32000},
            retry_on=[],
            retry_count=0,
            api_key_count=0,
        ),
    )

    raw_messages = [
        {"role": "user", "content": "hello"},
        {"role": "invalid", "content": "drop-me"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "filesystem_write", "arguments": {"path": "/tmp/demo"}},
                }
            ],
        },
    ]
    raw_tools = [
        {
            "type": "function",
            "function": {
                "name": "filesystem_write",
                "description": "write file",
                "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
            },
        }
    ]

    preview = chat_backend_module.build_send_provider_request_preview(
        config=SimpleNamespace(),
        messages=raw_messages,
        tools=raw_tools,
        model_refs=["primary"],
        prompt_cache_key=None,
    )

    assert preview["messages"] == chat_backend_module.sanitize_provider_messages(raw_messages)
    assert preview["tools"] == chat_backend_module.normalize_openai_tool_definitions(raw_tools)
    assert preview["prompt_cache_key"] == chat_backend_module.build_stable_prompt_cache_key(
        preview["messages"],
        preview["tools"],
        "custom-model",
    )


def test_resolve_send_model_context_window_info_preserves_resolution_error_details(monkeypatch) -> None:
    monkeypatch.setattr(
        chat_backend_module,
        "build_provider_from_model_key",
        lambda config, ref, api_key_index=None: (_ for _ in ()).throw(RuntimeError("bad binding")),
    )

    info = chat_backend_module.resolve_send_model_context_window_info(
        config=SimpleNamespace(),
        model_refs=["primary"],
    )

    assert info.model_key == "primary"
    assert info.context_window_tokens == 0
    assert "bad binding" in info.resolution_error
