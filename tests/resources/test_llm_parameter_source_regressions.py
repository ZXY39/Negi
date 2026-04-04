from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from langchain_core.messages import HumanMessage

import main.runtime.chat_backend as chat_backend_module
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


def test_normalize_draft_omits_optional_model_parameters_when_left_blank() -> None:
    registry = TemplateRegistry()
    draft = ProviderConfigDraft(
        provider_id="custom",
        api_key="demo-key",
        base_url="https://example.com/v1",
        default_model="custom-model",
        parameters={
            "api_mode": "custom-direct",
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


@pytest.mark.asyncio
async def test_react_loop_no_longer_passes_hardcoded_model_parameter_defaults() -> None:
    captured: list[dict[str, object]] = []

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
