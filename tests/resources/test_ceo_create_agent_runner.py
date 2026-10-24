import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from g3ku.agent.tools.base import Tool
from g3ku.agent.tools.memory_update import MemoryUpdateTool
from g3ku.config.schema import MemoryAssemblyConfig
from g3ku.core.messages import UserInputMessage
from g3ku.runtime import web_ceo_sessions
from g3ku.runtime.frontdoor import _ceo_create_agent_impl as create_agent_impl
from g3ku.runtime.frontdoor import _ceo_runtime_ops as ceo_runtime_ops
from g3ku.runtime.frontdoor import ceo_runner, prompt_cache_contract
from g3ku.runtime.frontdoor.state_models import CeoFrontdoorInterrupted, initial_persistent_state
from g3ku.runtime.frontdoor.tool_contract import (
    clear_pinned_contract_stores,
    is_frontdoor_tool_contract_message,
    split_pinned_contract_from_system_text,
)
from g3ku.runtime.session_agent import RuntimeAgentSession
from g3ku.runtime.tool_contract_block import render_tool_contract_block
from main.runtime.chat_backend import (
    build_actual_request_diagnostics,
    build_prompt_cache_diagnostics,
)


@pytest.fixture(autouse=True)
def _isolate_pinned_contract_stores():
    """钉住表按 session_key 分桶且是进程级的：本文件多数用例共用 `web:shared`。

    不隔离的话，前一个用例钉住的名单会被后一个用例当成"已钉住"复用，头部凭空多出别人的名单。
    """
    clear_pinned_contract_stores()
    yield
    clear_pinned_contract_stores()


def _without_pinned_head(records: list[dict[str, object]]) -> list[dict[str, object]]:
    """摘掉头部那条 system 上的钉住块，按"基础正文"比对前缀。

    钉住块只在刷新边界变化时改一次字节，比对逐字相等的前缀用例要先把它摘掉，否则断言的
    是"钉住块的字节"而不是本用例要证的复用形状。
    """
    normalized = [dict(item) for item in list(records or [])]
    if normalized and str(normalized[0].get("role") or "").strip().lower() == "system":
        base, _pinned = split_pinned_contract_from_system_text(normalized[0].get("content"))
        normalized[0]["content"] = base
    return normalized


def _is_frontdoor_runtime_tool_contract_record(record: dict[str, object]) -> bool:
    return is_frontdoor_tool_contract_message(dict(record or {}))


def _message_role_for_contract_filter(message: object) -> str:
    raw_role = str(getattr(message, "role", "") or getattr(message, "type", "") or "").strip().lower()
    if raw_role == "ai":
        return "assistant"
    if raw_role == "human":
        return "user"
    return raw_role


def _web_ceo_uploaded_image_note(*, text: str, image_path: Path) -> str:
    note = "\n".join(
        [
            "Uploaded attachments:",
            f"- image: {image_path.name} (local path: {image_path})",
            "You may inspect the local file paths above when helpful.",
        ]
    )
    text_value = str(text or "").strip()
    return f"{text_value}\n\n{note}" if text_value else note


def _web_ceo_multimodal_image_note(*, text: str) -> str:
    note = (
        "For this turn, the uploaded image is attached directly in this request. "
        "Use direct visual reasoning on the attached image first."
    )
    text_value = str(text or "").strip()
    return f"{text_value}\n\n{note}" if text_value else note


def _web_ceo_upload_metadata(image_path: Path) -> dict[str, object]:
    return {
        "web_ceo_raw_text": "Please inspect this image",
        "web_ceo_uploads": [
            {
                "path": str(image_path),
                "name": image_path.name,
                "mime_type": "image/png",
                "kind": "image",
                "size": image_path.stat().st_size,
            }
        ],
    }


def _web_ceo_mixed_upload_metadata(*, text: str, docx_path: Path, image_path: Path) -> dict[str, object]:
    return {
        "web_ceo_raw_text": text,
        "web_ceo_uploads": [
            {
                "path": str(docx_path),
                "name": docx_path.name,
                "mime_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                "kind": "file",
                "size": docx_path.stat().st_size,
            },
            {
                "path": str(image_path),
                "name": image_path.name,
                "mime_type": "image/png",
                "kind": "image",
                "size": image_path.stat().st_size,
            },
        ],
    }


def _stub_live_runtime_model(
    monkeypatch: pytest.MonkeyPatch,
    *,
    model_key: str = "ceo_primary",
    image_multimodal_enabled: bool,
    context_window_tokens: int = 128000,
) -> None:
    live_config = SimpleNamespace(
        get_managed_model=lambda key: (
            SimpleNamespace(
                image_multimodal_enabled=image_multimodal_enabled,
                context_window_tokens=context_window_tokens,
            )
            if key == model_key
            else None
        )
    )
    monkeypatch.setattr(
        ceo_runtime_ops,
        "get_runtime_config",
        lambda force=False: (live_config, 1, False),
    )


def _contract_block_message(tool_id: str, *, fingerprint: str = "sha256:" + "a" * 64) -> dict[str, object]:
    """上一跳 `load_tool_context` 的结果行：块在请求视图里 ⇒ 这一跳在场 ⇒ 可调用。

    投递形态与真车道同形（JSON 信封的 `content` 字段装着契约块），判据只认这颗块。
    """
    return {
        "role": "tool",
        "name": "load_tool_context",
        "tool_call_id": f"call_load_{tool_id}",
        "content": json.dumps(
            {
                "ok": True,
                "tool_id": tool_id,
                "tool_context_fingerprint": fingerprint,
                "content": render_tool_contract_block(tool_id, fingerprint, f"# {tool_id}\n契约正文"),
            },
            ensure_ascii=False,
        ),
    }


def _normalized_tool_content(messages):
    """工具结果只按去掉尾随空白后的正文比较：artifact 种子允许空白漂移。"""
    normalized = []
    for item in list(messages or []):
        if isinstance(item, dict) and str(item.get("role") or "").strip() == "tool":
            normalized.append({**item, "content": str(item.get("content") or "").strip()})
        else:
            normalized.append(item)
    return normalized


def _canonical_frontdoor_state(**overrides) -> dict[str, object]:
    state: dict[str, object] = {
        "messages": [],
        "tool_names": [],
        "candidate_tool_names": [],
        "candidate_tool_items": [],
        "visible_skill_ids": [],
        "candidate_skill_ids": [],
        "rbac_visible_tool_names": [],
        "rbac_visible_skill_ids": [],
    }
    state.update(dict(overrides))
    return state


class _FakeGraphOutput:
    def __init__(self, *, value, interrupts=()):
        self.value = dict(value or {})
        self.interrupts = tuple(interrupts)


class _NonPrimitive:
    def __str__(self) -> str:
        return "non-primitive"


class _DemoTool(Tool):
    @property
    def name(self) -> str:
        return "demo_tool"

    @property
    def description(self) -> str:
        return "demo tool"

    @property
    def parameters(self) -> dict[str, object]:
        return {
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
        }

    async def execute(self, **kwargs):
        return kwargs


class _CreateAsyncTaskLikeTool(Tool):
    @property
    def name(self) -> str:
        return "create_async_task"

    @property
    def description(self) -> str:
        return "create async task"

    @property
    def parameters(self) -> dict[str, object]:
        return {
            "type": "object",
            "properties": {
                "task": {"type": "string"},
                "core_requirement": {"type": "string"},
                "execution_policy": {"type": ["object", "string"]},
            },
            "required": ["task", "core_requirement", "execution_policy"],
        }

    async def execute(self, **kwargs):
        return kwargs


class _StructuredFactsTool(Tool):
    @property
    def name(self) -> str:
        return "record_facts"

    @property
    def description(self) -> str:
        return "write structured memory facts"

    @property
    def parameters(self) -> dict[str, object]:
        return {
            "type": "object",
            "properties": {
                "facts": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "category": {"type": "string"},
                            "entity": {"type": "string"},
                            "attribute": {"type": "string"},
                            "value": {
                                "type": ["string", "number", "boolean", "object", "array", "null"],
                                "description": "Canonical value for the fact.",
                            },
                            "observed_at": {"type": "string"},
                            "time_semantics": {"type": "string"},
                            "source_excerpt": {"type": "string"},
                        },
                        "required": [
                            "category",
                            "entity",
                            "attribute",
                            "value",
                            "observed_at",
                            "time_semantics",
                            "source_excerpt",
                        ],
                    },
                }
            },
            "required": ["facts"],
        }

    async def execute(self, **kwargs):
        return kwargs


class _ModelVisibleSchemaOverrideTool(Tool):
    @property
    def name(self) -> str:
        return "model_visible_schema_override_tool"

    @property
    def description(self) -> str:
        return "runtime-only description"

    @property
    def model_description(self) -> str:
        return "model-visible description"

    @property
    def parameters(self) -> dict[str, object]:
        return {
            "type": "object",
            "properties": {
                "runtime_only": {"type": "string", "description": "runtime-only field"},
            },
            "required": ["runtime_only"],
        }

    @property
    def model_parameters(self) -> dict[str, object]:
        return {
            "type": "object",
            "properties": {
                "model_only": {"type": "integer", "description": "model-only field"},
            },
            "required": ["model_only"],
        }

    def to_model_schema(self) -> dict[str, object]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.model_description,
                "parameters": self.model_parameters,
            },
        }

    async def execute(self, **kwargs):
        return kwargs


class _NamedSchemaTool(Tool):
    def __init__(
        self,
        *,
        name: str,
        description: str,
        parameters: dict[str, object],
        model_description: str | None = None,
        model_parameters: dict[str, object] | None = None,
    ) -> None:
        self._name = name
        self._description = description
        self._parameters = dict(parameters)
        self._model_description = str(model_description or description)
        self._model_parameters = dict(model_parameters or parameters)

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return self._description

    @property
    def parameters(self) -> dict[str, object]:
        return dict(self._parameters)

    @property
    def model_description(self) -> str:
        return self._model_description

    @property
    def model_parameters(self) -> dict[str, object]:
        return dict(self._model_parameters)

    async def execute(self, **kwargs):
        return kwargs


class _RuntimeToolStack:
    def push_runtime_context(self, _context):
        return "token"

    def pop_runtime_context(self, _token):
        return None


async def _run_frontdoor_tool_once(runner, *, tool_name: str, arguments: dict[str, object], call_id: str):
    """Execute one tool through the production execute_tools node and return its result row."""
    payload = {"id": call_id, "name": tool_name, "arguments": dict(arguments or {})}
    update = await runner._graph_execute_tools(
        {
            "tool_names": [tool_name],
            "tool_call_payloads": [payload],
        },
        runtime=SimpleNamespace(context=SimpleNamespace()),
    )
    rows = [item for item in list(update.get("messages") or []) if str(item.get("role") or "") == "tool"]
    return update, rows[-1] if rows else {}



class _BrokenValidationTool(Tool):
    @property
    def name(self) -> str:
        return "broken_validation_tool"

    @property
    def description(self) -> str:
        return "broken validation tool"

    @property
    def parameters(self) -> dict[str, object]:
        return {
            "type": "object",
            "properties": {
                "value": {"type": "string"},
            },
            "required": ["value"],
        }

    async def execute(self, **kwargs):
        raise AssertionError("execute should not be called when validation crashes")

    def validate_params(self, params: dict[str, object]) -> list[str]:
        _ = params
        raise TypeError("unhashable type: 'list'")


_PARAMETER_GUIDANCE_TEMPLATE = (
    '请先调用 load_tool_context(tool_id="{tool_name}") 查看该工具的详细说明、参数契约和示例后，再重新使用该工具。'
)
# 无资源 descriptor 的工具不再引导 load_tool_context（加载必被合同闸门拒绝）。
# 参数错误改为把该校验面实际使用的契约回贴给模型；渲染不出必填项时才退回泛化核对提醒。
_PARAMETER_RECHECK_GUIDANCE = (
    '该工具没有可加载的扩展说明。请仔细核对该工具的入参（参数名、必填项、类型与取值结构）后重新提交。'
)
_PARAMETER_CONTRACT_GUIDANCE_PREFIX = '该工具没有可加载的扩展说明，参数契约如下（必填项及其类型与取值结构）：'


class _ExecuteErrorTool(Tool):
    def __init__(self, *, name: str, exc: Exception) -> None:
        self._name = name
        self._exc = exc

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return f"{self._name} tool"

    @property
    def parameters(self) -> dict[str, object]:
        return {
            "type": "object",
            "properties": {
                "value": {"type": "string"},
            },
            "required": ["value"],
        }

    async def execute(self, **kwargs):
        _ = kwargs
        raise self._exc


def test_memory_assembly_config_no_longer_exposes_frontdoor_global_summary_settings() -> None:
    cfg = MemoryAssemblyConfig()

    assert not hasattr(cfg, "frontdoor_recent_message_count")
    assert not hasattr(cfg, "frontdoor_summary_trigger_message_count")
    assert not hasattr(cfg, "frontdoor_summarizer_trigger_message_count")
    assert not hasattr(cfg, "frontdoor_summarizer_keep_message_count")
    assert cfg.frontdoor_interrupt_approval_enabled is False
    assert cfg.frontdoor_interrupt_tool_names == ["create_async_task"]
    assert not hasattr(cfg, "frontdoor_global_summary_trigger_ratio")
    assert not hasattr(cfg, "frontdoor_global_summary_target_ratio")
    assert not hasattr(cfg, "frontdoor_global_summary_min_output_tokens")
    assert not hasattr(cfg, "frontdoor_global_summary_max_output_ratio")
    assert not hasattr(cfg, "frontdoor_global_summary_max_output_tokens_ceiling")
    assert not hasattr(cfg, "frontdoor_global_summary_pressure_warn_ratio")
    assert not hasattr(cfg, "frontdoor_global_summary_force_refresh_ratio")
    assert not hasattr(cfg, "frontdoor_global_summary_min_delta_tokens")
    assert not hasattr(cfg, "frontdoor_global_summary_failure_cooldown_seconds")




@pytest.mark.asyncio
async def test_create_agent_runner_prompt_keeps_history_uncompacted_after_legacy_frontdoor_history_compaction_removal() -> None:
    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(
        loop=SimpleNamespace(
            _memory_runtime_settings=SimpleNamespace(
                assembly=SimpleNamespace(
                    core_tools=[],
                )
            ),
            main_task_service=None,
            memory_manager=None,
            provider_name="openai",
            model="gpt-test",
            tools=SimpleNamespace(get=lambda *_: None, tool_names=[]),
        )
    )

    state = {
        "session_key": "web:shared",
        "messages": [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "u1"},
            {"role": "assistant", "content": "a1"},
            {"role": "user", "content": "u2"},
            {"role": "assistant", "content": "a2"},
            {"role": "user", "content": "u3"},
        ],
        "compression_state": {"status": "", "text": "", "source": ""},
    }

    prepared = await runner._graph_prepare_turn(
        state=state,
        runtime=SimpleNamespace(context=SimpleNamespace(session=None)),
    )

    assert prepared["messages"][0] == {"role": "system", "content": "system"}
    assert prepared["messages"][1:] == [
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "u2"},
        {"role": "assistant", "content": "a2"},
        {"role": "user", "content": "u3"},
    ]




def test_create_agent_runner_resolve_ceo_model_refs_keeps_config_chain_order(monkeypatch) -> None:
    """角色链顺序即路由顺序：协议/缓存能力位不参与模型选择。

    命中缓存靠网关侧自动前缀缓存，与请求是否带 prompt_cache_key 无关，因此
    chat 车道不能被当成「无缓存」而从链上抹掉。
    """
    from g3ku.runtime.frontdoor import _ceo_support as ceo_support

    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(
        loop=SimpleNamespace(
            app_config=SimpleNamespace(
                get_role_model_keys=lambda role: [
                    "openai:gpt-4.1",
                    "responses:gpt-5.4",
                ]
            ),
            provider_name="openai",
            model="gpt-test",
        )
    )

    monkeypatch.setattr(ceo_support, "refresh_loop_runtime_config", lambda *args, **kwargs: None)

    assert runner._resolve_ceo_model_refs() == [
        "openai:gpt-4.1",
        "responses:gpt-5.4",
    ]


def test_build_prompt_cache_diagnostics_surfaces_prefix_reason_and_actual_request_fields() -> None:
    diagnostics = build_prompt_cache_diagnostics(
        stable_messages=[{"role": "system", "content": "stable system"}],
        dynamic_appendix_messages=[{"role": "assistant", "content": "dynamic appendix"}],
        tool_schemas=[],
        provider_model="claude-sonnet-4",
        scope="ceo_frontdoor",
        prompt_cache_key="cache-key",
        cache_family_revision="exp:rev-7",
        prompt_lane="ceo_frontdoor",
        prefix_invalidation_reason="cache_family_revision_changed",
        actual_request_messages=[
            {"role": "system", "content": "stable system"},
            {"role": "assistant", "content": "dynamic appendix"},
            {"role": "user", "content": "current user turn"},
        ],
        actual_tool_schemas=[
            {
                "name": "exec",
                "description": "",
                "parameters": {"type": "object"},
            }
        ],
    )

    assert diagnostics["prompt_lane"] == "ceo_frontdoor"
    assert diagnostics["cache_family_revision"] == "exp:rev-7"
    assert diagnostics["stable_prefix_hash"]
    assert diagnostics["dynamic_appendix_hash"]
    assert diagnostics["prefix_invalidation_reason"] == "cache_family_revision_changed"
    assert diagnostics["actual_request_message_count"] == 3
    assert str(diagnostics["actual_request_hash"]).strip()
    assert str(diagnostics["actual_tool_schema_hash"]).strip()


def test_build_prompt_cache_diagnostics_accepts_flat_function_tool_schemas() -> None:
    diagnostics = build_prompt_cache_diagnostics(
        stable_messages=[{"role": "system", "content": "stable system"}],
        dynamic_appendix_messages=[{"role": "assistant", "content": "dynamic appendix"}],
        tool_schemas=[
            {
                "type": "function",
                "name": "exec",
                "description": "Run a command",
                "parameters": {"type": "object", "properties": {"command": {"type": "string"}}},
            }
        ],
        provider_model="gpt-5.2",
        scope="frontdoor_internal",
        prompt_cache_key="cache-key",
        actual_request_messages=[
            {"role": "system", "content": "stable system"},
            {"role": "assistant", "content": "dynamic appendix"},
            {"role": "user", "content": "current user turn"},
        ],
        actual_tool_schemas=[
            {
                "type": "function",
                "name": "exec",
                "description": "Run a command",
                "parameters": {"type": "object", "properties": {"command": {"type": "string"}}},
            }
        ],
    )

    assert str(diagnostics["tool_signature_hash"]).strip()
    assert str(diagnostics["actual_tool_schema_hash"]).strip()
    assert diagnostics["tool_signature_hash"] == diagnostics["actual_tool_schema_hash"]


def test_ceo_runner_always_selects_create_agent_impl(monkeypatch) -> None:
    class _New:
        def __init__(self, *, loop) -> None:
            self.loop = loop

    monkeypatch.setattr(ceo_runner, "CreateAgentCeoFrontDoorRunner", _New)

    loop = SimpleNamespace()
    runner = ceo_runner.CeoFrontDoorRunner(loop=loop)

    assert isinstance(runner._impl, _New)




def test_create_agent_runner_visible_tool_schemas_uses_prepared_state(monkeypatch) -> None:
    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace())
    captured: dict[str, object] = {}

    def _fake_frontdoor_tool_schemas_for_state(*, state, runtime):
        captured["state"] = state
        captured["runtime"] = runtime
        return ["tool-a"]

    monkeypatch.setattr(runner, "_frontdoor_tool_schemas_for_state", _fake_frontdoor_tool_schemas_for_state)
    runtime = SimpleNamespace(context=SimpleNamespace(session_key="web:shared"))
    state = {"tool_names": ["record_tool"]}

    result = runner.visible_tool_schemas(state=state, runtime=runtime)

    assert result == ["tool-a"]
    assert captured == {"state": state, "runtime": runtime}


@pytest.mark.asyncio
async def test_create_agent_runner_node_prepare_turn_replaces_messages_instead_of_appending() -> None:
    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace())
    synced: dict[str, object] = {}

    async def _fake_graph_prepare_turn(state, runtime) -> dict[str, object]:
        _ = state, runtime
        return {
            "messages": [
                {"role": "system", "content": "stable system"},
                {"role": "user", "content": "latest user"},
            ],
            "stable_messages": [
                {"role": "system", "content": "stable system"},
                {"role": "user", "content": "latest user"},
            ],
            "dynamic_appendix_messages": [
                {"role": "assistant", "content": "## Retrieved Context\n- memory"}
            ],
        }

    runner._graph_prepare_turn = _fake_graph_prepare_turn  # type: ignore[method-assign]
    runner._sync_runtime_session_frontdoor_state = lambda *, state, runtime: synced.update(  # type: ignore[method-assign]
        {"state": state, "runtime": runtime}
    )

    update = await runner._node_prepare_turn(
        state={"messages": [{"role": "user", "content": "stale request bundle"}]},
        runtime=SimpleNamespace(),
    )

    assert update["messages"] == [
        {"role": "system", "content": "stable system"},
        {"role": "user", "content": "latest user"},
    ]
    assert dict(synced["state"])["messages"] == [
        {"role": "system", "content": "stable system"},
        {"role": "user", "content": "latest user"},
    ]


@pytest.mark.asyncio
async def test_create_agent_frontdoor_execute_tool_emits_tool_result_progress(monkeypatch) -> None:
    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(
        loop=SimpleNamespace(
            tools=_RuntimeToolStack(),
            resource_manager=None,
            tool_execution_manager=None,
        )
    )
    progress_calls: list[tuple[str, str | None, dict[str, object]]] = []

    async def _on_progress(content: str, *, event_kind=None, event_data=None, **kwargs):
        _ = kwargs
        progress_calls.append((str(content), event_kind, dict(event_data or {})))

    monkeypatch.setattr(runner, "_registered_tools_for_state", lambda state: {"demo_tool": _DemoTool()})
    monkeypatch.setattr(runner, "_registered_tools", lambda names: {"demo_tool": _DemoTool()})
    monkeypatch.setattr(
        runner,
        "_build_tool_runtime_context",
        lambda **kwargs: {"on_progress": _on_progress, "session_key": "web:test"},
    )

    _update, row = await _run_frontdoor_tool_once(
        runner,
        tool_name="demo_tool",
        arguments={"value": "alpha"},
        call_id="call-demo-tool-1",
    )

    assert [item[1] for item in progress_calls] == ["tool_start", "tool_result"]
    assert progress_calls[0][2] == {
        "tool_name": "demo_tool",
        "arguments_text": "demo_tool (value=alpha)",
        "tool_call_id": "call-demo-tool-1",
    }
    assert progress_calls[-1][2] == {"tool_name": "demo_tool", "tool_call_id": "call-demo-tool-1"}
    assert str(row.get("name") or "") == "demo_tool"

@pytest.mark.asyncio
async def test_create_agent_graph_execute_tools_preserves_parallel_same_name_tool_call_ids(monkeypatch) -> None:
    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(
        loop=SimpleNamespace(
            tools=SimpleNamespace(
                push_runtime_context=lambda context: object(),
                pop_runtime_context=lambda token: None,
            )
        )
    )
    progress_calls: list[tuple[str, str | None, dict[str, object]]] = []

    async def _on_progress(content: str, *, event_kind=None, event_data=None, **kwargs):
        _ = kwargs
        progress_calls.append((str(content), event_kind, dict(event_data or {})))

    monkeypatch.setattr(runner, "_registered_tools_for_state", lambda state: {"demo_tool": _DemoTool()})
    monkeypatch.setattr(runner, "_build_tool_runtime_context", lambda **kwargs: {"on_progress": _on_progress})
    async def _fake_execute_tool_call_with_raw_result(*, tool, tool_name, arguments, runtime_context, on_progress, tool_call_id):
        _ = tool, runtime_context
        await on_progress(
            f"{tool_name} started for {arguments['value']}",
            event_kind="tool_start",
            event_data={"tool_name": tool_name, "tool_call_id": tool_call_id},
        )
        return (
            {"value": arguments["value"]},
            json.dumps({"value": arguments["value"]}),
            "success",
            "2026-04-05T11:00:00",
            "2026-04-05T11:00:01",
            1.0,
        )

    monkeypatch.setattr(runner, "_execute_tool_call_with_raw_result", _fake_execute_tool_call_with_raw_result)

    result = await runner._graph_execute_tools(
            {
                "tool_call_payloads": [
                    {"id": "call-demo-tool-1", "name": "demo_tool", "arguments": {"value": "alpha"}},
                    {"id": "call-demo-tool-2", "name": "demo_tool", "arguments": {"value": "beta"}},
                ],
                "messages": [],
                "used_tools": [],
                "route_kind": "direct_reply",
                "parallel_enabled": True,
                "max_parallel_tool_calls": 2,
                "synthetic_tool_calls_used": False,
                "response_payload": {"content": "", "tool_calls": []},
            },
            runtime=SimpleNamespace(context=SimpleNamespace()),
        )

    assert result["next_step"] == "call_model"
    assert [(item[1], item[2]["tool_call_id"]) for item in progress_calls if item[1] == "tool_start"] == [
        ("tool_start", "call-demo-tool-1"),
        ("tool_start", "call-demo-tool-2"),
    ]
    assert sorted(item[2]["tool_call_id"] for item in progress_calls if item[1] == "tool_result") == [
        "call-demo-tool-1",
        "call-demo-tool-2",
    ]


@pytest.mark.asyncio
async def test_graph_execute_tools_grows_authoritative_request_body_baseline() -> None:
    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(
        loop=SimpleNamespace(
            tools=SimpleNamespace(
                push_runtime_context=lambda context: object(),
                pop_runtime_context=lambda token: None,
            )
        )
    )

    async def _on_progress(content: str, *, event_kind=None, event_data=None, **kwargs):
        _ = content, event_kind, event_data, kwargs

    runner._build_tool_runtime_context = lambda **kwargs: {"on_progress": _on_progress}
    runner._registered_tools_for_state = lambda state: {"demo_tool": _DemoTool()}

    async def _fake_execute_tool_call_with_raw_result(*, tool, tool_name, arguments, runtime_context, on_progress, tool_call_id):
        _ = tool, tool_name, runtime_context, on_progress, tool_call_id
        return (
            {"value": arguments["value"]},
            json.dumps({"value": arguments["value"]}),
            "success",
            "2026-04-18T00:04:39+08:00",
            "2026-04-18T00:04:40+08:00",
            1.0,
        )

    runner._execute_tool_call_with_raw_result = _fake_execute_tool_call_with_raw_result

    state = {
        "tool_call_payloads": [
            {"id": "call-demo-tool-1", "name": "demo_tool", "arguments": {"value": "alpha"}},
        ],
        "messages": [
            {"role": "system", "content": "SYSTEM"},
            {"role": "user", "content": "old question"},
            {"role": "assistant", "content": "retrieved context"},
            {"role": "user", "content": "follow-up"},
            {"role": "assistant", "content": "memory hint"},
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "message_type": "frontdoor_runtime_tool_contract",
                        "callable_tool_names": ["demo_tool"],
                    },
                    ensure_ascii=False,
                ),
            },
        ],
        "frontdoor_request_body_messages": [
            {"role": "system", "content": "SYSTEM"},
            {"role": "user", "content": "old question"},
            {"role": "assistant", "content": "retrieved context"},
            {"role": "user", "content": "follow-up"},
            {"role": "assistant", "content": "memory hint"},
        ],
        "frontdoor_history_shrink_reason": "",
        "used_tools": [],
        "route_kind": "direct_reply",
        "parallel_enabled": False,
        "max_parallel_tool_calls": 1,
        "synthetic_tool_calls_used": False,
        "response_payload": {"content": "", "tool_calls": []},
    }

    result = await runner._graph_execute_tools(
        state,
        runtime=SimpleNamespace(context=SimpleNamespace()),
    )

    assert result["frontdoor_request_body_messages"] == [
        *state["frontdoor_request_body_messages"],
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call-demo-tool-1",
                    "type": "function",
                    "function": {
                        "name": "demo_tool",
                        "arguments": json.dumps({"value": "alpha"}, ensure_ascii=False),
                    },
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call-demo-tool-1",
            "name": "demo_tool",
            "content": json.dumps({"value": "alpha"}),
            "started_at": "2026-04-18T00:04:39+08:00",
            "finished_at": "2026-04-18T00:04:40+08:00",
            "elapsed_seconds": 1.0,
        },
    ]








def test_create_agent_frontdoor_tool_arguments_normalize_execution_policy() -> None:
    normalized = ceo_runtime_ops._normalize_frontdoor_tool_arguments(
        "create_async_task",
        {
            "task": "continue task",
            "core_requirement": "finish the analysis",
            "execution_policy": "focus",
        },
    )

    assert normalized["execution_policy"] == {"mode": "focus"}

@pytest.mark.asyncio
async def test_create_agent_frontdoor_tool_degrades_validation_exception_to_tool_error(monkeypatch) -> None:
    progress_calls: list[tuple[str, str | None, dict[str, object]]] = []

    async def _on_progress(content: str, *, event_kind=None, event_data=None, **kwargs):
        _ = kwargs
        progress_calls.append((str(content), event_kind, dict(event_data or {})))

    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(
        loop=SimpleNamespace(
            tools=_RuntimeToolStack(),
            resource_manager=None,
            tool_execution_manager=None,
        )
    )
    monkeypatch.setattr(
        runner,
        "_registered_tools_for_state",
        lambda state: {"broken_validation_tool": _BrokenValidationTool()},
    )
    monkeypatch.setattr(
        runner,
        "_registered_tools",
        lambda names: {"broken_validation_tool": _BrokenValidationTool()},
    )
    monkeypatch.setattr(
        runner,
        "_build_tool_runtime_context",
        lambda **kwargs: {"on_progress": _on_progress, "session_key": "web:test"},
    )

    _update, row = await _run_frontdoor_tool_once(
        runner,
        tool_name="broken_validation_tool",
        arguments={"value": "alpha"},
        call_id="call-broken-1",
    )
    result_text = str(row.get("content") or "")

    assert [item[1] for item in progress_calls] == ["tool_error"]
    assert "Error validating broken_validation_tool" in result_text
    assert "unhashable type: 'list'" in result_text
    assert _PARAMETER_CONTRACT_GUIDANCE_PREFIX in result_text
    assert "value=string" in result_text
    assert _PARAMETER_RECHECK_GUIDANCE not in result_text
    assert _PARAMETER_GUIDANCE_TEMPLATE.format(tool_name="broken_validation_tool") not in result_text

@pytest.mark.asyncio
async def test_create_agent_runner_passes_thread_id_and_context_with_minimal_initial_state(monkeypatch) -> None:
    captured: dict[str, object] = {}
    session = SimpleNamespace(state=SimpleNamespace(session_key="web:shared"), _last_route_kind="task_dispatch")
    progress_calls: list[object] = []

    async def _on_progress(*args, **kwargs):
        progress_calls.append((args, kwargs))

    async def _fake_run_step_loop(*, state, runtime, entry, resume_decision=None):
        captured["state"] = state
        captured["runtime"] = runtime
        captured["entry"] = entry
        captured["resume_decision"] = resume_decision
        return {"messages": [], "route_kind": "direct_reply", "final_output": "ok", "verified_task_ids": []}

    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace())
    monkeypatch.setattr(runner, "_run_step_loop", _fake_run_step_loop)

    output = await runner.run_turn(
        user_input=SimpleNamespace(content="hello", metadata={}),
        session=session,
        on_progress=_on_progress,
    )

    assert output == "ok"
    assert session._last_route_kind == "direct_reply"
    assert captured["entry"] == "prepare_turn"
    runtime_context = getattr(captured["runtime"], "context")
    assert runtime_context.session_key == "web:shared"
    assert runtime_context.loop is runner._loop
    assert runtime_context.session is session
    assert runtime_context.on_progress is _on_progress
    assert captured["state"] == initial_persistent_state(user_input={"content": "hello", "metadata": {}})
    assert captured["resume_decision"] is None
    assert progress_calls == []


@pytest.mark.asyncio
async def test_create_agent_runner_raises_structured_interrupt(monkeypatch) -> None:
    session = SimpleNamespace(state=SimpleNamespace(session_key="web:shared"), _last_route_kind="task_dispatch")

    def _raise_interrupt(*, state, runtime, entry, resume_decision=None):
        _ = runtime
        raise CeoFrontdoorInterrupted(
            interrupts=[SimpleNamespace(interrupt_id="interrupt-1", value={"kind": "frontdoor_tool_approval"})],
            values={"approval_request": {"kind": "frontdoor_tool_approval"}},
            resume_state={"approval_request": {"kind": "frontdoor_tool_approval"}, "next_step": "review_tool_calls"},
        )

    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace())
    monkeypatch.setattr(runner, "_run_step_loop", _raise_interrupt)

    with pytest.raises(CeoFrontdoorInterrupted) as exc_info:
        await runner.run_turn(
            user_input=SimpleNamespace(content="create a task", metadata={}),
            session=session,
            on_progress=None,
        )

    assert session._last_route_kind == "direct_reply"
    assert exc_info.value.values == {"approval_request": {"kind": "frontdoor_tool_approval"}}
    assert [item.interrupt_id for item in exc_info.value.interrupts] == ["interrupt-1"]


@pytest.mark.asyncio
async def test_create_agent_runner_resume_uses_command_resume(monkeypatch) -> None:
    captured: dict[str, object] = {}
    session = SimpleNamespace(
        state=SimpleNamespace(session_key="web:shared"),
        _last_route_kind="task_dispatch",
        paused_execution_context_snapshot=lambda: {
            "graph_state": {"version": 2, "state": {"approval_request": {"kind": "frontdoor_tool_approval"}}}
        },
    )
    progress_calls: list[object] = []

    async def _on_progress(*args, **kwargs):
        progress_calls.append((args, kwargs))

    async def _fake_run_step_loop(*, state, runtime, entry, resume_decision=None):
        captured["state"] = state
        captured["runtime"] = runtime
        captured["entry"] = entry
        captured["resume_decision"] = resume_decision
        return {"messages": [], "route_kind": "self_execute", "final_output": "approved", "verified_task_ids": []}

    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace())
    monkeypatch.setattr(runner, "_run_step_loop", _fake_run_step_loop)

    result = await runner.resume_turn(
        session=session,
        resume_value={"decisions": [{"type": "approve"}]},
        on_progress=_on_progress,
    )

    assert result == "approved"
    assert captured["entry"] == "review_tool_calls"
    assert captured["resume_decision"] == {"decisions": [{"type": "approve"}]}
    assert captured["state"] == {"approval_request": {"kind": "frontdoor_tool_approval"}}
    runtime_context = getattr(captured["runtime"], "context")
    assert runtime_context.session_key == "web:shared"
    assert runtime_context.loop is runner._loop
    assert runtime_context.session is session
    assert runtime_context.on_progress is _on_progress
    assert session._last_route_kind == "self_execute"
    assert progress_calls == []


@pytest.mark.asyncio
async def test_create_agent_runner_resume_raises_structured_interrupt(monkeypatch) -> None:
    session = SimpleNamespace(
        state=SimpleNamespace(session_key="web:shared"),
        _last_route_kind="task_dispatch",
        paused_execution_context_snapshot=lambda: {
            "graph_state": {"version": 2, "state": {"approval_request": {"kind": "frontdoor_tool_approval"}}}
        },
    )

    async def _raise_interrupt(*, state, runtime, entry, resume_decision=None):
        _ = state, runtime, entry
        raise CeoFrontdoorInterrupted(
            interrupts=[SimpleNamespace(interrupt_id="interrupt-2", value={"kind": "frontdoor_tool_approval"})],
            values={"approval_request": {"kind": "frontdoor_tool_approval"}, "final_output": "ignored"},
            resume_state={},
        )

    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace())
    monkeypatch.setattr(runner, "_run_step_loop", _raise_interrupt)

    with pytest.raises(CeoFrontdoorInterrupted) as exc_info:
        await runner.resume_turn(
            session=session,
            resume_value={"decisions": [{"type": "approve"}]},
            on_progress=None,
        )

    assert session._last_route_kind == "task_dispatch"
    assert exc_info.value.values == {
        "approval_request": {"kind": "frontdoor_tool_approval"},
        "final_output": "ignored",
    }
    assert [item.interrupt_id for item in exc_info.value.interrupts] == ["interrupt-2"]






@pytest.mark.asyncio
async def test_create_agent_postprocess_does_not_make_contract_absent_tool_callable() -> None:
    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace(main_task_service=None))

    result = await runner._postprocess_completed_tool_cycle(
        state={
            "tool_call_payloads": [
                {"id": "call-1", "name": "load_tool_context", "arguments": {"tool_id": "filesystem_write"}}
            ],
            "messages": [
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "type": "function",
                            "function": {"name": "load_tool_context", "arguments": '{"tool_id":"filesystem_write"}'},
                        }
                    ],
                },
                {
                    "role": "tool",
                    "tool_call_id": "call-1",
                    "name": "load_tool_context",
                    "content": (
                        '{"ok": true, "tool_id": "filesystem_write", '
                        '"callable_now": true, "will_be_hydrated_next_turn": true, '
                        '"hydration_targets": ["filesystem_write"]}'
                    ),
                },
            ],
            "used_tools": [],
            "route_kind": "direct_reply",
            "tool_names": ["load_tool_context"],
            "candidate_tool_names": ["filesystem_write", "agent_browser"],
            "candidate_skill_ids": [],
            "visible_skill_ids": [],
            "rbac_visible_tool_names": ["load_tool_context", "filesystem_write", "agent_browser"],
            "rbac_visible_skill_ids": [],
        }
    )

    # 结果正文里没有 `[TOOL_CONTRACT_V1:…]` 块 ⇒ 这一跳不在场：既不进 callable，也不从候选里摘走。
    assert result is not None
    assert "hydrated_tool_names" not in result
    assert result["tool_names"] == ["load_tool_context"]
    assert result["candidate_tool_names"] == ["filesystem_write", "agent_browser"]


@pytest.mark.asyncio
async def test_create_agent_graph_execute_tools_makes_loaded_contract_callable() -> None:
    class _InlineLoadTool(Tool):
        @property
        def name(self) -> str:
            return "load_tool_context"

        @property
        def description(self) -> str:
            return "load tool context"

        @property
        def parameters(self) -> dict[str, object]:
            return {
                "type": "object",
                "properties": {"tool_id": {"type": "string"}},
                "required": ["tool_id"],
            }

        async def execute(self, tool_id: str, **kwargs):
            _ = kwargs
            return {
                "ok": True,
                "tool_id": tool_id,
                "callable_now": True,
                "will_be_hydrated_next_turn": True,
                # 真 loader 的 ok 载荷必带 fingerprint（`main/governance/tool_context.py`
                # 的 build_tool_context_fingerprint），且交付时把契约**块**写进 `content`
                # ——在场判据只认那颗块头，fingerprint 键本身不再算载体。
                "tool_context_fingerprint": "sha256:" + "a" * 64,
                "content": render_tool_contract_block(
                    tool_id, "sha256:" + "a" * 64, f"# {tool_id}\n契约正文"
                ),
                "hydration_targets": [tool_id],
            }

    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(
        loop=SimpleNamespace(
            tools=SimpleNamespace(
                push_runtime_context=lambda context: object(),
                pop_runtime_context=lambda token: None,
            )
        )
    )
    runner._registered_tools_for_state = lambda state: {"load_tool_context": _InlineLoadTool()}
    runner._build_tool_runtime_context = lambda **kwargs: {
        "on_progress": None,
        "actor_role": "ceo",
        "session_key": "web:shared",
        "tool_contract_enforced": True,
        "candidate_tool_names": ["filesystem_write", "agent_browser"],
        "candidate_skill_ids": [],
    }

    result = await runner._graph_execute_tools(
        {
            **_canonical_frontdoor_state(
                tool_names=["load_tool_context"],
                candidate_tool_names=["filesystem_write", "agent_browser"],
                candidate_tool_items=[
                    {"tool_id": "filesystem_write", "description": "Write file content to disk."},
                    {"tool_id": "agent_browser", "description": "Browser automation via semantic shortlist."},
                ],
                visible_skill_ids=[],
                candidate_skill_ids=[],
                rbac_visible_tool_names=["load_tool_context", "filesystem_write", "agent_browser"],
                rbac_visible_skill_ids=[],
                dynamic_appendix_messages=[
                    {"role": "assistant", "content": "## Retrieved Context\n- authoritative memory"}
                ],
            ),
            "tool_call_payloads": [
                {"id": "call-1", "name": "load_tool_context", "arguments": {"tool_id": "filesystem_write"}}
            ],
            "messages": [],
            "used_tools": [],
                "route_kind": "direct_reply",
                "parallel_enabled": False,
                "max_parallel_tool_calls": 1,
                "synthetic_tool_calls_used": False,
                "response_payload": {"content": "", "tool_calls": []},
                "compression_state": {"status": "running", "text": "compressing", "source": "user"},
                "semantic_context_state": {"summary_text": "summary", "needs_refresh": False},
            },
            runtime=SimpleNamespace(context=SimpleNamespace()),
        )

    assert result["next_step"] == "call_model"
    assert "hydrated_tool_names" not in result
    assert result["tool_names"] == ["load_tool_context", "filesystem_write", "silent"]
    assert result["candidate_tool_names"] == ["agent_browser"]
    assert result["candidate_tool_items"] == [
        {
            "tool_id": "agent_browser",
            "description": "Browser automation via semantic shortlist.",
        }
    ]
    assert result["candidate_skill_ids"] == []
    assert result["dynamic_appendix_messages"][0] == {
        "role": "assistant",
        "content": "## Retrieved Context\n- authoritative memory",
    }
    contract_messages = [
        item
        for item in list(result["dynamic_appendix_messages"])
        if _is_frontdoor_runtime_tool_contract_record(dict(item))
    ]
    # 尾块退役：动态附录只剩非合同消息，候选/可调用状态由 state 与一次性通知承载。
    assert contract_messages == []




@pytest.mark.asyncio
async def test_create_agent_runner_graph_prepare_turn_keeps_contract_present_tool_callable() -> None:
    session = RuntimeAgentSession(
        SimpleNamespace(model="demo", reasoning_effort=None, multi_agent_runner=None),
        session_key="web:shared",
        channel="web",
        chat_id="shared",
    )

    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(
        loop=SimpleNamespace(
            sessions=SimpleNamespace(get_or_create=lambda session_key: SimpleNamespace(session_key=session_key)),
            main_task_service=None,
            tools=SimpleNamespace(get=lambda *_: None, tool_names=[]),
            provider_name="openai",
            model="gpt-test",
        )
    )
    async def _resolve_for_actor(**kwargs):
        _ = kwargs
        return {
            "skills": [],
            "tool_families": [],
            "tool_names": ["filesystem_stat", "load_tool_context", "filesystem_write"],
        }

    async def _build_for_ceo(**kwargs):
        # 真实装配器会把本回合的活行（上一跳 load_tool_context 的结果）带进请求视图；
        # 读数按装配后的视图算，夹具必须同形，否则测的是"模型看不到契约"那种形状。
        model_messages = [
            *[dict(item) for item in list(kwargs.get("checkpoint_messages") or [])],
            {"role": "system", "content": "system"},
            {"role": "user", "content": "hello"},
        ]
        return SimpleNamespace(
            model_messages=model_messages,
            stable_messages=[
                {"role": "system", "content": "system"},
                {"role": "user", "content": "hello"},
            ],
            dynamic_appendix_messages=[],
            tool_names=["filesystem_stat", "load_tool_context", "filesystem_write"],
            candidate_tool_names=[],
            candidate_tool_items=[],
            trace={
                "selected_skills": [],
                "tool_selection": {
                    "mode": "inventory_only",
                    "candidate_tool_names": [],
                },
                "capability_snapshot": {
                    "visible_tool_ids": ["filesystem_stat", "load_tool_context", "filesystem_write"],
                    "visible_skill_ids": [],
                },
            },
            cache_family_revision="frontdoor:v1",
            turn_overlay_text="",
        )

    runner._resolver = SimpleNamespace(resolve_for_actor=_resolve_for_actor)
    runner._builder = SimpleNamespace(build_for_ceo=_build_for_ceo)
    runner._resolve_ceo_model_refs = lambda: ["openai:gpt-4.1"]
    runner._selected_tool_schemas = lambda tool_names: [{"name": name, "parameters": {"type": "object"}} for name in list(tool_names or [])]

    prepared_state = initial_persistent_state(user_input={"content": "hello", "metadata": {}})
    # 装配路按**当次请求视图**判在场（FIX_PLAN §0 不变量）：这里放上上一跳那条
    # load_tool_context 的结果行，测的是「正文在场 ⇒ 下一回合继续可调」这条真路径。
    prepared_state["messages"] = [
        {
            "role": "tool",
            "name": "load_tool_context",
            "content": json.dumps(
                {
                    "ok": True,
                    "tool_id": "filesystem_write",
                    "tool_context_fingerprint": "sha256:" + "a" * 64,
                    "content": render_tool_contract_block(
                        "filesystem_write", "sha256:" + "a" * 64, "# filesystem_write\n契约正文"
                    ),
                },
                ensure_ascii=False,
            ),
        }
    ]

    prepared = await runner._graph_prepare_turn(
        state=prepared_state,
        runtime=SimpleNamespace(context=SimpleNamespace(session=session)),
    )

    assert "hydrated_tool_names" not in prepared
    assert "hydration_revoked_executor_names" not in prepared
    assert prepared["tool_names"] == ["filesystem_stat", "load_tool_context", "filesystem_write"]
    assert prepared["frontdoor_selection_debug"] == {
        "query_text": "hello",
        "raw_turn_query_text": "hello",
        "tool_selection": {
            "mode": "inventory_only",
            "candidate_tool_names": [],
        },
        "capability_snapshot": {
            "visible_tool_ids": ["filesystem_stat", "load_tool_context", "filesystem_write"],
            "visible_skill_ids": [],
        },
        "selected_skills": [],
        "callable_tool_names": ["load_tool_context", "filesystem_write", "silent"],
        "candidate_tool_names": [],
        "contract_present_tool_names": ["filesystem_write"],
        # D15 的两档读数：在场那份带自己的修订与本跳暂存份数；清扫计数缺省空。
        "tool_contract_presence": {
            "present": [
                {"tool": "filesystem_write", "fingerprint": "sha256:" + "a" * 64, "staged_count": 0}
            ],
            "absent": [],
            "stale_rev": [],
        },
        "staged_swept_tool_ids": [],
    }
    contract_messages = [
        dict(item)
        for item in list(prepared["dynamic_appendix_messages"])
        if _is_frontdoor_runtime_tool_contract_record(dict(item))
    ]
    # 尾块退役：这一跳的 callable 状态仍由 state/selection trace 承载，模型面不再有合同块。
    assert contract_messages == []


@pytest.mark.asyncio
async def test_create_agent_runner_presence_reading_follows_the_assembled_request_view() -> None:
    """读数必须按装配后的请求视图算，而不是按续跑种子。

    实盘形状：上一跳 `load_tool_context` 的结果只在本回合活行的末尾出现，续跑种子
    （上一回合的基线）里没有它。若读数只看种子，就会把模型眼前明明看得见的契约报成
    absent——D15 的三档读数也就失去了排障价值。
    """
    session = RuntimeAgentSession(
        SimpleNamespace(model="demo", reasoning_effort=None, multi_agent_runner=None),
        session_key="web:shared",
        channel="web",
        chat_id="shared",
    )
    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(
        loop=SimpleNamespace(
            sessions=SimpleNamespace(get_or_create=lambda session_key: SimpleNamespace(session_key=session_key)),
            main_task_service=None,
            tools=SimpleNamespace(get=lambda *_: None, tool_names=[]),
            provider_name="openai",
            model="gpt-test",
        )
    )
    live_contract_row = {
        "role": "tool",
        "name": "load_tool_context",
        "content": json.dumps(
            {
                "ok": True,
                "tool_id": "filesystem_write",
                "tool_context_fingerprint": "sha256:" + "b" * 64,
                "content": render_tool_contract_block(
                    "filesystem_write", "sha256:" + "b" * 64, "# filesystem_write\n契约正文"
                ),
            },
            ensure_ascii=False,
        ),
    }

    async def _resolve_for_actor(**kwargs):
        _ = kwargs
        return {
            "skills": [],
            "tool_families": [],
            "tool_names": ["filesystem_stat", "load_tool_context", "filesystem_write"],
        }

    async def _build_for_ceo(**kwargs):
        # 种子只有上一回合的对话；本回合的活行由装配器追加进请求视图。
        return SimpleNamespace(
            model_messages=[
                *[dict(item) for item in list(kwargs.get("request_body_seed_messages") or [])],
                dict(live_contract_row),
                {"role": "user", "content": "hello"},
            ],
            stable_messages=[{"role": "system", "content": "system"}],
            dynamic_appendix_messages=[],
            tool_names=["filesystem_stat", "load_tool_context", "filesystem_write"],
            candidate_tool_names=[],
            candidate_tool_items=[],
            trace={
                "selected_skills": [],
                "tool_selection": {"mode": "inventory_only", "candidate_tool_names": []},
                "capability_snapshot": {
                    "visible_tool_ids": ["filesystem_stat", "load_tool_context", "filesystem_write"],
                    "visible_skill_ids": [],
                },
            },
            cache_family_revision="frontdoor:v1",
            turn_overlay_text="",
        )

    runner._resolver = SimpleNamespace(resolve_for_actor=_resolve_for_actor)
    runner._builder = SimpleNamespace(build_for_ceo=_build_for_ceo)
    runner._resolve_ceo_model_refs = lambda: ["openai:gpt-4.1"]
    runner._selected_tool_schemas = lambda tool_names: [{"name": name, "parameters": {"type": "object"}} for name in list(tool_names or [])]
    # 续跑种子存在，但它不含本回合刚加载的契约。
    session._frontdoor_request_body_messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "上一回合"},
        {"role": "assistant", "content": "好"},
    ]

    prepared = await runner._graph_prepare_turn(
        state=initial_persistent_state(user_input={"content": "hello", "metadata": {}}),
        runtime=SimpleNamespace(context=SimpleNamespace(session=session)),
    )

    presence = prepared["frontdoor_selection_debug"]["tool_contract_presence"]
    assert [item["tool"] for item in presence["present"]] == ["filesystem_write"]
    assert "filesystem_write" not in presence["absent"]


@pytest.mark.asyncio
async def test_create_agent_runner_call_model_node_refreshes_presence_reading_each_hop() -> None:
    """跳内读数必须由 call_model 刷新：prepare_turn 每回合只跑一次。

    第二跳起请求视图里才有本回合的 `load_tool_context` 结果；若读数只留在 prepare
    那份，模型已经能调用的工具会被永久报成 absent。
    """
    session = RuntimeAgentSession(
        SimpleNamespace(model="demo", reasoning_effort=None, multi_agent_runner=None),
        session_key="web:shared",
        channel="web",
        chat_id="shared",
    )
    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace())

    async def _fake_graph_call_model(state, *, runtime):
        _ = state, runtime
        return {"final_output": "ok", "next_step": "finalize"}

    runner._graph_call_model = _fake_graph_call_model  # type: ignore[method-assign]

    state = initial_persistent_state(user_input={"content": "hi", "metadata": {}})
    state["frontdoor_selection_debug"] = {
        "query_text": "hi",
        "candidate_tool_names": ["filesystem_write"],
    }
    state["messages"] = [
        {
            "role": "tool",
            "name": "load_tool_context",
            "content": json.dumps(
                {
                    "ok": True,
                    "tool_id": "filesystem_write",
                    "tool_context_fingerprint": "sha256:" + "c" * 64,
                    "content": render_tool_contract_block(
                        "filesystem_write", "sha256:" + "c" * 64, "# filesystem_write\n契约正文"
                    ),
                },
                ensure_ascii=False,
            ),
        }
    ]

    update = await runner._node_call_model(
        state,
        SimpleNamespace(context=SimpleNamespace(session=session)),
    )

    presence = update["frontdoor_selection_debug"]["tool_contract_presence"]
    assert [item["tool"] for item in presence["present"]] == ["filesystem_write"]
    assert presence["absent"] == []
    assert update["frontdoor_selection_debug"]["candidate_tool_names"] == ["filesystem_write"]


@pytest.mark.asyncio
async def test_create_agent_runner_graph_prepare_turn_keeps_cron_internal_event_out_of_user_messages() -> None:
    session = RuntimeAgentSession(
        SimpleNamespace(model="demo", reasoning_effort=None, multi_agent_runner=None),
        session_key="web:shared",
        channel="web",
        chat_id="shared",
    )
    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(
        loop=SimpleNamespace(
            sessions=SimpleNamespace(get_or_create=lambda session_key: SimpleNamespace(session_key=session_key)),
            main_task_service=None,
            tools=SimpleNamespace(get=lambda *_: None, tool_names=[]),
            provider_name="openai",
            model="gpt-test",
        )
    )
    captured: dict[str, object] = {}

    async def _resolve_for_actor(**kwargs):
        _ = kwargs
        return {
            "skills": [],
            "tool_families": [],
            "tool_names": ["cron"],
        }

    async def _build_for_ceo(**kwargs):
        captured["request_body_seed_messages"] = list(kwargs.get("request_body_seed_messages") or [])
        captured["internal_seed_messages"] = list(kwargs.get("internal_seed_messages") or [])
        captured["user_content"] = kwargs.get("user_content")
        stable_messages = list(kwargs.get("request_body_seed_messages") or [])
        return SimpleNamespace(
            model_messages=list(stable_messages),
            stable_messages=list(stable_messages),
            dynamic_appendix_messages=[],
            tool_names=["cron"],
            candidate_tool_names=[],
            candidate_tool_items=[],
            trace={
                "selected_skills": [],
                "tool_selection": {},
                "capability_snapshot": {
                    "visible_tool_ids": ["cron"],
                    "visible_skill_ids": [],
                },
            },
            cache_family_revision="frontdoor:v1",
            turn_overlay_text="",
        )

    runner._resolver = SimpleNamespace(resolve_for_actor=_resolve_for_actor)
    runner._builder = SimpleNamespace(build_for_ceo=_build_for_ceo)
    runner._resolve_ceo_model_refs = lambda: ["openai:gpt-4.1"]
    runner._selected_tool_schemas = lambda tool_names: [{"name": name, "parameters": {"type": "object"}} for name in list(tool_names or [])]

    prepared = await runner._graph_prepare_turn(
        state=initial_persistent_state(
            user_input={
                "content": "Report the current time to the user.",
                "metadata": {
                    "cron_internal": True,
                    "cron_job_id": "job-77",
                    "cron_max_runs": 3,
                    "cron_delivery_index": 2,
                    "cron_delivered_runs": 1,
                    "cron_reminder_text": "Report the current time to the user.",
                },
            }
        ),
        runtime=SimpleNamespace(context=SimpleNamespace(session=session)),
    )

    assert captured["user_content"] == ""
    # 冷启动（无基线）时续跑种子为空，内部事件改经独立参数下发，不再混入种子。
    seed_messages = [dict(item) for item in list(captured["request_body_seed_messages"] or [])]
    assert seed_messages == []
    internal_messages = [dict(item) for item in list(captured["internal_seed_messages"] or [])]
    assert [message["role"] for message in internal_messages] == ["user", "user"]
    assert str(internal_messages[0]["content"]).startswith("你接收到了之前你定时的任务，如下：")
    assert str(internal_messages[1]["content"]).startswith("[CRON INTERNAL EVENT]")
    assert not any(
        str(item.get("role") or "").strip().lower() == "user"
        and str(item.get("content") or "").strip() == "Report the current time to the user."
        for item in list(prepared["frontdoor_request_body_messages"] or [])
        if isinstance(item, dict)
    )


@pytest.mark.asyncio
async def test_create_agent_runner_graph_prepare_turn_keeps_normal_ceo_tools_for_cron_internal() -> None:
    session = RuntimeAgentSession(
        SimpleNamespace(model="demo", reasoning_effort=None, multi_agent_runner=None),
        session_key="web:shared",
        channel="web",
        chat_id="shared",
    )
    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(
        loop=SimpleNamespace(
            sessions=SimpleNamespace(get_or_create=lambda session_key: SimpleNamespace(session_key=session_key)),
            main_task_service=None,
            tools=SimpleNamespace(get=lambda *_: None, tool_names=[]),
            provider_name="openai",
            model="gpt-test",
        )
    )

    async def _resolve_for_actor(**kwargs):
        _ = kwargs
        return {
            "skills": [],
            "tool_families": [],
            "tool_names": ["create_async_task", "task_list", "cron"],
        }

    async def _build_for_ceo(**kwargs):
        stable_messages = list(kwargs.get("request_body_seed_messages") or [])
        return SimpleNamespace(
            model_messages=list(stable_messages),
            stable_messages=list(stable_messages),
            dynamic_appendix_messages=[],
            tool_names=["create_async_task", "task_list", "cron"],
            candidate_tool_names=[],
            candidate_tool_items=[],
            repair_required_tool_items=[],
            repair_required_skill_items=[],
            trace={
                "selected_skills": [],
                "tool_selection": {},
                "capability_snapshot": {
                    "visible_tool_ids": ["create_async_task", "task_list", "cron"],
                    "visible_skill_ids": [],
                },
            },
            cache_family_revision="frontdoor:v1",
            turn_overlay_text="",
        )

    runner._resolver = SimpleNamespace(resolve_for_actor=_resolve_for_actor)
    runner._builder = SimpleNamespace(build_for_ceo=_build_for_ceo)
    runner._resolve_ceo_model_refs = lambda: ["openai:gpt-4.1"]
    runner._selected_tool_schemas = (
        lambda tool_names: [{"name": name, "parameters": {"type": "object"}} for name in list(tool_names or [])]
    )

    prepared_state = initial_persistent_state(
        user_input={
            "content": "Create an async task that continues the scheduled work.",
            "metadata": {
                "cron_internal": True,
                "cron_job_id": "job-88",
                "cron_max_runs": 1,
                "cron_delivery_index": 1,
                "cron_delivered_runs": 0,
                "cron_reminder_text": "Create an async task that continues the scheduled work.",
            },
        }
    )
    # 内部轮继承的那份 callable 同样要过在场判据：`cron` 有 toolskill，上一跳 load 过它，
    # 契约块才在这份请求视图里。
    prepared_state["messages"] = [_contract_block_message("cron")]

    prepared = await runner._graph_prepare_turn(
        state=prepared_state,
        runtime=SimpleNamespace(context=SimpleNamespace(session=session)),
    )

    assert prepared["tool_names"] == ["create_async_task", "task_list", "cron"]
    assert "create_async_task" in list(prepared["provider_tool_names"] or [])
    assert "task_list" in list(prepared["provider_tool_names"] or [])

    contract_messages = [
        dict(item)
        for item in list(prepared["dynamic_appendix_messages"] or [])
        if _is_frontdoor_runtime_tool_contract_record(dict(item))
    ]
    # 尾块退役：callable 读点落在 selection debug / state 上，不再有合同消息。
    assert contract_messages == []
    assert list(prepared["frontdoor_selection_debug"]["callable_tool_names"]) == [
        "create_async_task",
        "task_list",
        "cron",
        "silent",
    ]




@pytest.mark.asyncio
async def test_create_agent_runner_graph_prepare_turn_persists_request_body_without_tool_contract() -> None:
    session = RuntimeAgentSession(
        SimpleNamespace(model="demo", reasoning_effort=None, multi_agent_runner=None),
        session_key="web:shared",
        channel="web",
        chat_id="shared",
    )

    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(
        loop=SimpleNamespace(
            sessions=SimpleNamespace(get_or_create=lambda session_key: SimpleNamespace(session_key=session_key)),
            main_task_service=None,
            tools=SimpleNamespace(get=lambda *_: None, tool_names=[]),
            provider_name="openai",
            model="gpt-test",
        )
    )

    async def _resolve_for_actor(**kwargs):
        _ = kwargs
        return {
            "skills": [],
            "tool_families": [],
            "tool_names": ["filesystem_stat", "load_tool_context", "filesystem_write"],
        }

    async def _build_for_ceo(**kwargs):
        _ = kwargs
        return SimpleNamespace(
            model_messages=[
                {"role": "system", "content": "system"},
                {"role": "user", "content": "hello"},
            ],
            stable_messages=[
                {"role": "system", "content": "system"},
                {"role": "user", "content": "hello"},
            ],
            dynamic_appendix_messages=[
                {"role": "assistant", "content": "## Retrieved Context\n- authoritative memory"}
            ],
            tool_names=["filesystem_stat", "load_tool_context", "filesystem_write"],
            candidate_tool_names=[],
            candidate_tool_items=[],
            trace={
                "selected_skills": [],
                "tool_selection": {"mode": "inventory_only", "candidate_tool_names": []},
                "capability_snapshot": {
                    "visible_tool_ids": ["filesystem_stat", "load_tool_context", "filesystem_write"],
                    "visible_skill_ids": [],
                },
            },
            cache_family_revision="frontdoor:v1",
            turn_overlay_text="## Retrieved Context\n- authoritative memory",
        )

    runner._resolver = SimpleNamespace(resolve_for_actor=_resolve_for_actor)
    runner._builder = SimpleNamespace(build_for_ceo=_build_for_ceo)
    runner._resolve_ceo_model_refs = lambda: ["openai:gpt-4.1"]
    runner._selected_tool_schemas = (
        lambda tool_names: [{"name": name, "parameters": {"type": "object"}} for name in list(tool_names or [])]
    )

    prepared = await runner._graph_prepare_turn(
        state=initial_persistent_state(user_input={"content": "hello", "metadata": {}}),
        runtime=SimpleNamespace(context=SimpleNamespace(session=session)),
    )

    messages = list(prepared["messages"])
    assert messages[0]["role"] == "system"
    assert str(messages[0]["content"]).startswith("system")
    assert "## Runtime Contract (pinned)" in str(messages[0]["content"])
    assert messages[1:] == [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "## Retrieved Context\n- authoritative memory"},
    ]
    assert prepared["dynamic_appendix_messages"] == []






def test_runtime_agent_session_persists_paused_request_body_baseline(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from g3ku.runtime import web_ceo_sessions

    monkeypatch.setattr(web_ceo_sessions, "workspace_path", lambda: tmp_path)
    session = RuntimeAgentSession(
        SimpleNamespace(model="demo", reasoning_effort=None, multi_agent_runner=None),
        session_key="web:shared",
        channel="web",
        chat_id="shared",
    )
    session._state.is_running = True
    session._state.status = "running"
    session._frontdoor_request_body_messages = [
        {"role": "system", "content": "SYSTEM"},
        {"role": "user", "content": "old question"},
        {"role": "assistant", "content": "old answer"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "exec", "arguments": "{}"},
                }
            ],
        },
        {
            "role": "tool",
            "name": "exec",
            "tool_call_id": "call-1",
            "content": '{"status":"success"}',
        },
    ]
    session._frontdoor_history_shrink_reason = "stage_compaction"

    paused_snapshot = session._build_execution_context_snapshot(
        allow_manual_pause=True,
        status_override="paused",
    )
    session._set_paused_execution_context(paused_snapshot)

    persisted = web_ceo_sessions.read_paused_execution_context("web:shared")

    assert isinstance(persisted, dict)
    assert persisted["frontdoor_request_body_messages"] == session._frontdoor_request_body_messages
    assert persisted["frontdoor_history_shrink_reason"] == "stage_compaction"


def test_runtime_agent_session_persists_paused_repair_required_lists(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from g3ku.runtime import web_ceo_sessions

    monkeypatch.setattr(web_ceo_sessions, "workspace_path", lambda: tmp_path)
    session = RuntimeAgentSession(
        SimpleNamespace(model="demo", reasoning_effort=None, multi_agent_runner=None),
        session_key="web:shared",
        channel="web",
        chat_id="shared",
    )
    session._state.is_running = True
    session._state.status = "running"
    session._frontdoor_repair_required_tool_items = [
        {
            "tool_id": "agent_browser",
            "description": "Browser automation",
            "reason": "missing required paths",
        }
    ]
    session._frontdoor_repair_required_skill_items = [
        {
            "skill_id": "writing-skills",
            "description": "Skill maintenance workflow",
            "reason": "missing required bins",
        }
    ]

    paused_snapshot = session._build_execution_context_snapshot(
        allow_manual_pause=True,
        status_override="paused",
    )
    session._set_paused_execution_context(paused_snapshot)

    persisted = web_ceo_sessions.read_paused_execution_context("web:shared")

    assert isinstance(persisted, dict)
    assert persisted["repair_required_tool_items"] == session._frontdoor_repair_required_tool_items
    assert persisted["repair_required_skill_items"] == session._frontdoor_repair_required_skill_items

    restored = RuntimeAgentSession(
        SimpleNamespace(model="demo", reasoning_effort=None, multi_agent_runner=None),
        session_key="web:shared",
        channel="web",
        chat_id="shared",
    )
    restored_snapshot = restored.paused_execution_context_snapshot()

    assert isinstance(restored_snapshot, dict)
    assert restored_snapshot["repair_required_tool_items"] == session._frontdoor_repair_required_tool_items
    assert restored_snapshot["repair_required_skill_items"] == session._frontdoor_repair_required_skill_items


def test_runtime_agent_session_restore_prefers_paused_over_inflight_over_completed_continuity(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(web_ceo_sessions, "workspace_path", lambda: tmp_path)
    session_key = "web:shared"
    paused_baseline = [
        {"role": "system", "content": "SYSTEM"},
        {"role": "user", "content": "paused question"},
    ]
    inflight_baseline = [
        {"role": "system", "content": "SYSTEM"},
        {"role": "user", "content": "inflight question"},
    ]
    continuity_baseline = [
        {"role": "system", "content": "SYSTEM"},
        {"role": "user", "content": "continuity question"},
    ]
    continuity_record = web_ceo_sessions.persist_frontdoor_actual_request(
        session_key,
        payload={
            "turn_id": "turn-continuity",
            "request_messages": list(continuity_baseline),
            "prompt_cache_key_hash": "family-continuity",
            "actual_request_hash": "continuity-hash",
            "actual_request_message_count": 2,
            "actual_tool_schema_hash": "tool-continuity",
            "provider_model": "responses:gpt-test",
        },
    )
    inflight_record = web_ceo_sessions.persist_frontdoor_actual_request(
        session_key,
        payload={
            "turn_id": "turn-inflight",
            "request_messages": list(inflight_baseline),
            "prompt_cache_key_hash": "family-inflight",
            "actual_request_hash": "inflight-hash",
            "actual_request_message_count": 2,
            "actual_tool_schema_hash": "tool-inflight",
            "provider_model": "responses:gpt-test",
        },
    )
    paused_record = web_ceo_sessions.persist_frontdoor_actual_request(
        session_key,
        payload={
            "turn_id": "turn-paused",
            "request_messages": list(paused_baseline),
            "prompt_cache_key_hash": "family-paused",
            "actual_request_hash": "paused-hash",
            "actual_request_message_count": 2,
            "actual_tool_schema_hash": "tool-paused",
            "provider_model": "responses:gpt-test",
        },
    )

    web_ceo_sessions.write_completed_continuity_snapshot(
        session_key,
        {
            "frontdoor_request_body_messages": list(continuity_baseline),
            "frontdoor_history_shrink_reason": "stage_compaction",
            "frontdoor_token_preflight_diagnostics": {"final_request_tokens": 42},
            "frontdoor_actual_request_path": continuity_record["path"],
            "frontdoor_actual_request_history": [continuity_record],
            "visible_tool_ids": ["filesystem_write"],
            "visible_skill_ids": ["writing-skills"],
            "provider_tool_schema_names": ["filesystem_write"],
            "capability_snapshot_exposure_revision": "exp:continuity",
            "source_reason": "actual_request_sync",
        },
    )
    web_ceo_sessions.write_inflight_turn_snapshot(
        session_key,
        {
            "status": "running",
            "frontdoor_request_body_messages": list(inflight_baseline),
            "frontdoor_history_shrink_reason": "token_compression",
            "frontdoor_token_preflight_diagnostics": {"final_request_tokens": 64},
            "actual_request_path": inflight_record["path"],
            "actual_request_history": [inflight_record],
            "visible_tool_ids": ["exec"],
            "visible_skill_ids": ["find-skills"],
            "provider_tool_schema_names": ["exec"],
            "capability_snapshot_exposure_revision": "exp:inflight",
        },
    )
    web_ceo_sessions.write_paused_execution_context(
        session_key,
        {
            "status": "paused",
            "frontdoor_request_body_messages": list(paused_baseline),
            "frontdoor_history_shrink_reason": "stage_compaction",
            "frontdoor_token_preflight_diagnostics": {"final_request_tokens": 80},
            "actual_request_path": paused_record["path"],
            "actual_request_history": [paused_record],
            "visible_tool_ids": ["agent_browser"],
            "visible_skill_ids": ["writing-plans"],
            "provider_tool_schema_names": ["agent_browser"],
            "capability_snapshot_exposure_revision": "exp:paused",
        },
    )

    restored = RuntimeAgentSession(
        SimpleNamespace(model="demo", reasoning_effort=None, multi_agent_runner=None, sessions=SimpleNamespace()),
        session_key=session_key,
        channel="web",
        chat_id="shared",
    )

    assert restored._frontdoor_request_body_messages == paused_baseline
    assert restored._frontdoor_history_shrink_reason == "stage_compaction"
    assert restored._frontdoor_token_preflight_diagnostics == {"final_request_tokens": 80}
    assert restored._frontdoor_actual_request_path == paused_record["path"]
    assert restored._frontdoor_actual_request_history == [paused_record]
    assert restored._frontdoor_visible_tool_ids == ["agent_browser"]
    assert restored._frontdoor_visible_skill_ids == ["writing-plans"]
    assert restored._frontdoor_provider_tool_schema_names == ["agent_browser"]
    assert restored._frontdoor_capability_snapshot_exposure_revision == "exp:paused"


def test_runtime_agent_session_restores_frontdoor_baseline_from_latest_actual_request_artifact(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(web_ceo_sessions, "workspace_path", lambda: tmp_path)
    session_key = "web:shared"
    record = web_ceo_sessions.persist_frontdoor_actual_request(
        session_key,
        payload={
            "turn_id": "turn-artifact",
            "request_messages": [
                {"role": "system", "content": "SYSTEM"},
                {"role": "user", "content": "older user"},
                {"role": "assistant", "content": "older answer"},
                {
                    "role": "assistant",
                    "content": "## Runtime Tool Contract\nkind: frontdoor_runtime_tool_contract",
                },
            ],
            "frontdoor_history_shrink_reason": "stage_compaction",
            "frontdoor_token_preflight_diagnostics": {"final_request_tokens": 91},
            "prompt_cache_key_hash": "family-artifact",
            "actual_request_hash": "artifact-hash",
            "actual_request_message_count": 4,
            "actual_tool_schema_hash": "tool-artifact",
            "provider_model": "responses:gpt-test",
        },
    )

    restored = RuntimeAgentSession(
        SimpleNamespace(model="demo", reasoning_effort=None, multi_agent_runner=None, sessions=SimpleNamespace()),
        session_key=session_key,
        channel="web",
        chat_id="shared",
    )

    assert restored._frontdoor_request_body_messages == [
        {"role": "system", "content": "SYSTEM"},
        {"role": "user", "content": "older user"},
        {"role": "assistant", "content": "older answer"},
    ]
    assert restored._frontdoor_actual_request_path == record["path"]
    assert restored._frontdoor_actual_request_history == [record]


def test_runtime_agent_session_enriches_completed_continuity_restore_with_matching_actual_request_trace(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(web_ceo_sessions, "workspace_path", lambda: tmp_path)
    session_key = "web:shared"
    continuity_baseline = [
        {"role": "system", "content": "SYSTEM"},
        {"role": "user", "content": "continuity question"},
        {"role": "assistant", "content": "continuity answer"},
    ]
    record = web_ceo_sessions.persist_frontdoor_actual_request(
        session_key,
        payload={
            "turn_id": "turn-artifact",
            "request_messages": [
                {"role": "system", "content": "SYSTEM"},
                {"role": "user", "content": "continuity question"},
                {"role": "assistant", "content": "continuity answer"},
                {
                    "role": "assistant",
                    "content": "## Runtime Tool Contract\nkind: frontdoor_runtime_tool_contract",
                },
            ],
            "frontdoor_history_shrink_reason": "",
            "frontdoor_token_preflight_diagnostics": {"final_request_tokens": 91},
            "prompt_cache_key_hash": "family-artifact",
            "actual_request_hash": "artifact-hash",
            "actual_request_message_count": 4,
            "actual_tool_schema_hash": "tool-artifact",
            "provider_model": "responses:gpt-test",
        },
    )
    web_ceo_sessions.write_completed_continuity_snapshot(
        session_key,
        {
            "frontdoor_request_body_messages": list(continuity_baseline),
            "frontdoor_history_shrink_reason": "",
            "frontdoor_token_preflight_diagnostics": {"final_request_tokens": 42},
            "frontdoor_actual_request_path": "",
            "frontdoor_actual_request_history": [],
            "visible_tool_ids": ["exec"],
            "visible_skill_ids": ["writing-plans"],
            "provider_tool_schema_names": ["exec"],
            "capability_snapshot_exposure_revision": "exp:continuity",
            "source_reason": "actual_request_sync",
        },
    )

    restored = RuntimeAgentSession(
        SimpleNamespace(model="demo", reasoning_effort=None, multi_agent_runner=None, sessions=SimpleNamespace()),
        session_key=session_key,
        channel="web",
        chat_id="shared",
    )

    assert restored._frontdoor_request_body_messages == continuity_baseline
    assert restored._frontdoor_restore_source == "completed_continuity"
    assert restored._frontdoor_actual_request_path == record["path"]
    assert restored._frontdoor_actual_request_history == [record]
    assert restored._frontdoor_prompt_cache_key_hash == "family-artifact"
    assert restored._frontdoor_actual_request_hash == "artifact-hash"
    assert restored._frontdoor_actual_request_message_count == 4
    assert restored._frontdoor_actual_tool_schema_hash == "tool-artifact"
    assert restored._frontdoor_completed_continuity_bridge_pending is True


def test_runtime_agent_session_replaces_stale_completed_continuity_trace_with_newest_matching_artifact(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(web_ceo_sessions, "workspace_path", lambda: tmp_path)
    session_key = "web:shared"
    continuity_baseline = [
        {"role": "system", "content": "SYSTEM"},
        {"role": "user", "content": "continuity question"},
        {"role": "assistant", "content": "continuity answer"},
    ]
    stale_record = web_ceo_sessions.persist_frontdoor_actual_request(
        session_key,
        payload={
            "turn_id": "turn-stale",
            "request_messages": [
                {"role": "system", "content": "SYSTEM"},
                {"role": "user", "content": "stale question"},
                {"role": "assistant", "content": "stale answer"},
            ],
            "prompt_cache_key_hash": "family-stale",
            "actual_request_hash": "stale-hash",
            "actual_request_message_count": 3,
            "actual_tool_schema_hash": "tool-stale",
            "provider_model": "responses:gpt-test",
        },
    )
    matching_record = web_ceo_sessions.persist_frontdoor_actual_request(
        session_key,
        payload={
            "turn_id": "turn-match",
            "request_messages": [
                {"role": "system", "content": "SYSTEM"},
                {"role": "user", "content": "continuity question"},
                {"role": "assistant", "content": "continuity answer"},
                {
                    "role": "assistant",
                    "content": "## Runtime Tool Contract\nkind: frontdoor_runtime_tool_contract",
                },
            ],
            "prompt_cache_key_hash": "family-match",
            "actual_request_hash": "match-hash",
            "actual_request_message_count": 4,
            "actual_tool_schema_hash": "tool-match",
            "provider_model": "responses:gpt-test",
        },
    )
    web_ceo_sessions.write_completed_continuity_snapshot(
        session_key,
        {
            "frontdoor_request_body_messages": list(continuity_baseline),
            "frontdoor_history_shrink_reason": "",
            "frontdoor_actual_request_path": stale_record["path"],
            "frontdoor_actual_request_history": [stale_record],
            "visible_tool_ids": ["exec"],
            "visible_skill_ids": ["writing-plans"],
            "provider_tool_schema_names": ["exec"],
            "capability_snapshot_exposure_revision": "exp:continuity",
            "source_reason": "actual_request_sync",
        },
    )

    restored = RuntimeAgentSession(
        SimpleNamespace(model="demo", reasoning_effort=None, multi_agent_runner=None, sessions=SimpleNamespace()),
        session_key=session_key,
        channel="web",
        chat_id="shared",
    )

    assert restored._frontdoor_request_body_messages == continuity_baseline
    assert restored._frontdoor_restore_source == "completed_continuity"
    assert restored._frontdoor_actual_request_path == matching_record["path"]
    assert restored._frontdoor_actual_request_history == [matching_record]
    assert restored._frontdoor_prompt_cache_key_hash == "family-match"
    assert restored._frontdoor_actual_request_hash == "match-hash"
    assert restored._frontdoor_actual_request_message_count == 4
    assert restored._frontdoor_actual_tool_schema_hash == "tool-match"
    assert restored._frontdoor_completed_continuity_bridge_pending is True


def test_runtime_agent_session_keeps_completed_continuity_restore_without_trace_when_no_matching_artifact(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(web_ceo_sessions, "workspace_path", lambda: tmp_path)
    session_key = "web:shared"
    continuity_baseline = [
        {"role": "system", "content": "SYSTEM"},
        {"role": "user", "content": "continuity question"},
    ]
    web_ceo_sessions.persist_frontdoor_actual_request(
        session_key,
        payload={
            "turn_id": "turn-mismatch",
            "request_messages": [
                {"role": "system", "content": "SYSTEM"},
                {"role": "user", "content": "different question"},
            ],
            "prompt_cache_key_hash": "family-mismatch",
            "actual_request_hash": "mismatch-hash",
            "actual_request_message_count": 2,
            "actual_tool_schema_hash": "tool-mismatch",
            "provider_model": "responses:gpt-test",
        },
    )
    web_ceo_sessions.write_completed_continuity_snapshot(
        session_key,
        {
            "frontdoor_request_body_messages": list(continuity_baseline),
            "frontdoor_actual_request_path": "",
            "frontdoor_actual_request_history": [],
            "visible_tool_ids": ["exec"],
            "visible_skill_ids": ["writing-plans"],
            "provider_tool_schema_names": ["exec"],
            "capability_snapshot_exposure_revision": "exp:continuity",
            "source_reason": "actual_request_sync",
        },
    )

    restored = RuntimeAgentSession(
        SimpleNamespace(model="demo", reasoning_effort=None, multi_agent_runner=None, sessions=SimpleNamespace()),
        session_key=session_key,
        channel="web",
        chat_id="shared",
    )

    assert restored._frontdoor_request_body_messages == continuity_baseline
    assert restored._frontdoor_actual_request_path == ""
    assert restored._frontdoor_actual_request_history == []
    assert restored._frontdoor_completed_continuity_bridge_pending is False


def _stub_loop() -> SimpleNamespace:
    return SimpleNamespace(model="demo", reasoning_effort=None, multi_agent_runner=None, sessions=SimpleNamespace())




def test_cron_session_continuity_is_persisted_and_restored(
    tmp_path: Path,
    monkeypatch,
) -> None:
    # cron: 兜底会话命名空间同样参与连续性生命周期。
    monkeypatch.setattr(web_ceo_sessions, "workspace_path", lambda: tmp_path)
    session_key = "cron:job-123"
    baseline = [
        {"role": "system", "content": "BASE SYSTEM PROMPT"},
        {"role": "user", "content": "cron run"},
    ]
    writer = RuntimeAgentSession(_stub_loop(), session_key=session_key, channel="cron", chat_id="job-123")
    writer._frontdoor_request_body_messages = list(baseline)
    writer._sync_completed_continuity_snapshot(source_reason="finalize")

    restored = RuntimeAgentSession(_stub_loop(), session_key=session_key, channel="cron", chat_id="job-123")
    assert restored._frontdoor_request_body_messages == baseline
    assert restored._frontdoor_restore_source == "completed_continuity"


def test_non_frontdoor_session_namespace_skips_continuity(
    tmp_path: Path,
    monkeypatch,
) -> None:
    # 非 frontdoor 命名空间（如 task:/memory:）不参与连续性生命周期。
    monkeypatch.setattr(web_ceo_sessions, "workspace_path", lambda: tmp_path)
    session_key = "task:some-task"
    writer = RuntimeAgentSession(_stub_loop(), session_key=session_key, channel="task", chat_id="some-task")
    writer._frontdoor_request_body_messages = [
        {"role": "system", "content": "BASE SYSTEM PROMPT"},
        {"role": "user", "content": "task turn"},
    ]
    writer._sync_completed_continuity_snapshot(source_reason="finalize")
    snapshot_path = web_ceo_sessions.completed_continuity_snapshot_path_for_session(session_key, create=False)
    assert not snapshot_path.exists()


@pytest.mark.asyncio
async def test_graph_finalize_turn_appends_direct_reply_after_runtime_context_assistant() -> None:
    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace(main_task_service=None))

    result = await runner._graph_finalize_turn(
        {
            "messages": [
                {"role": "system", "content": "SYSTEM"},
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "## Retrieved Context\n- memory"},
            ],
            "final_output": "final answer",
            "route_kind": "direct_reply",
            "heartbeat_internal": False,
            "query_text": "hello",
        }
    )

    assert result["messages"] == [
        {"role": "system", "content": "SYSTEM"},
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "## Retrieved Context\n- memory"},
        {"role": "assistant", "content": "final answer"},
    ]


@pytest.mark.asyncio
async def test_graph_finalize_turn_strips_all_frontdoor_tool_contract_snapshots_from_durable_messages() -> None:
    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace(main_task_service=None))
    contract_text = json.dumps(
        {
            "message_type": "frontdoor_runtime_tool_contract",
            "callable_tool_names": ["filesystem_stat"],
            "candidate_tools": [],
            "candidate_skill_ids": [],
            "contract_revision": "frontdoor:v1",
        },
        ensure_ascii=False,
    )

    result = await runner._graph_finalize_turn(
        {
            "messages": [
                {"role": "system", "content": "SYSTEM"},
                {"role": "user", "content": "hello"},
                {"role": "user", "content": contract_text},
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "type": "function",
                            "function": {"name": "filesystem_stat", "arguments": "{}"},
                        }
                    ],
                },
                {
                    "role": "tool",
                    "name": "filesystem_stat",
                    "tool_call_id": "call-1",
                    "content": '{"status":"success"}',
                },
                {"role": "user", "content": contract_text},
            ],
            "final_output": "final answer",
            "route_kind": "direct_reply",
            "heartbeat_internal": False,
            "query_text": "hello",
        }
    )

    assert result["messages"] == [
        {"role": "system", "content": "SYSTEM"},
        {"role": "user", "content": "hello"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "filesystem_stat", "arguments": "{}"},
                }
            ],
        },
        {
            "role": "tool",
            "name": "filesystem_stat",
            "tool_call_id": "call-1",
            "content": '{"status":"success"}',
        },
        {"role": "assistant", "content": "final answer"},
    ]


def test_create_agent_runner_syncs_frontdoor_selection_debug_into_inflight_snapshot() -> None:
    session = RuntimeAgentSession(
        SimpleNamespace(model="demo", reasoning_effort=None, multi_agent_runner=None),
        session_key="web:shared",
        channel="web",
        chat_id="shared",
    )
    session._state.is_running = True
    session._state.status = "running"
    session._state.latest_message = "working"
    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace(main_task_service=None))

    runner._sync_runtime_session_frontdoor_state(
        state={
            "compression_state": {"status": "", "text": "", "source": "", "needs_recheck": False},
            "semantic_context_state": {},
            "frontdoor_selection_debug": {
                "query_text": "create a markdown file",
                "tool_selection": {"candidate_tool_names": ["filesystem_write"]},
            },
        },
        session=session,
    )

    snapshot = session.inflight_turn_snapshot()

    assert isinstance(snapshot, dict)
    assert snapshot["frontdoor_selection_debug"] == {
        "query_text": "create a markdown file",
        "tool_selection": {"candidate_tool_names": ["filesystem_write"]},
    }


def test_create_agent_runner_syncs_frontdoor_actual_request_trace_into_inflight_snapshot() -> None:
    session = RuntimeAgentSession(
        SimpleNamespace(model="demo", reasoning_effort=None, multi_agent_runner=None),
        session_key="web:shared",
        channel="web",
        chat_id="shared",
    )
    session._state.is_running = True
    session._state.status = "running"
    session._state.latest_message = "working"
    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace(main_task_service=None))

    runner._sync_runtime_session_frontdoor_state(
        state={
            "compression_state": {"status": "", "text": "", "source": "", "needs_recheck": False},
            "semantic_context_state": {},
            "frontdoor_actual_request_path": str(Path("D:/tmp/frontdoor-request.json")),
            "frontdoor_actual_request_history": [
                {
                    "path": str(Path("D:/tmp/frontdoor-request.json")),
                    "turn_id": "turn-frontdoor-1",
                    "actual_request_hash": "request-hash",
                    "actual_request_message_count": 6,
                    "actual_tool_schema_hash": "tool-hash",
                    "prompt_cache_key_hash": "family-hash",
                }
            ],
            "prompt_cache_diagnostics": {
                "prompt_cache_key_hash": "family-hash",
                "actual_request_hash": "request-hash",
                "actual_request_message_count": 6,
                "actual_tool_schema_hash": "tool-hash",
            },
        },
        session=session,
    )

    snapshot = session.inflight_turn_snapshot()

    assert isinstance(snapshot, dict)
    assert snapshot["actual_request_path"] == str(Path("D:/tmp/frontdoor-request.json"))
    assert snapshot["prompt_cache_key_hash"] == "family-hash"
    assert snapshot["actual_request_hash"] == "request-hash"
    assert snapshot["actual_request_message_count"] == 6
    assert snapshot["actual_tool_schema_hash"] == "tool-hash"
    assert snapshot["actual_request_history"] == [
        {
            "path": str(Path("D:/tmp/frontdoor-request.json")),
            "turn_id": "turn-frontdoor-1",
            "actual_request_hash": "request-hash",
            "actual_request_message_count": 6,
            "actual_tool_schema_hash": "tool-hash",
            "prompt_cache_key_hash": "family-hash",
        }
    ]


def test_create_agent_runner_sync_keeps_previous_actual_request_baseline_for_prepare_only_state() -> None:
    session = RuntimeAgentSession(
        SimpleNamespace(model="demo", reasoning_effort=None, multi_agent_runner=None),
        session_key="web:shared",
        channel="web",
        chat_id="shared",
    )
    session._frontdoor_request_body_messages = [
        {"role": "system", "content": "SYSTEM"},
        {"role": "user", "content": "old question"},
        {"role": "assistant", "content": "old answer"},
    ]
    session._frontdoor_actual_request_path = ""
    session._frontdoor_actual_request_history = []
    session._frontdoor_prompt_cache_key_hash = ""
    session._frontdoor_actual_request_hash = ""
    session._frontdoor_actual_request_message_count = 0
    session._frontdoor_actual_tool_schema_hash = ""
    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace(main_task_service=None))

    runner._sync_runtime_session_frontdoor_state(
        state={
            "compression_state": {"status": "", "text": "", "source": "", "needs_recheck": False},
            "semantic_context_state": {},
            "frontdoor_request_body_messages": [
                {"role": "system", "content": "SYSTEM"},
                {"role": "user", "content": "old question"},
                {"role": "assistant", "content": "old answer"},
                {"role": "user", "content": "new paused user"},
            ],
            "prompt_cache_diagnostics": {
                "prompt_cache_key_hash": "family-hash-planned",
                "actual_request_hash": "request-hash-planned",
                "actual_request_message_count": 4,
                "actual_tool_schema_hash": "tool-hash-planned",
            },
        },
        session=session,
    )

    assert session._frontdoor_request_body_messages == [
        {"role": "system", "content": "SYSTEM"},
        {"role": "user", "content": "old question"},
        {"role": "assistant", "content": "old answer"},
    ]
    assert session._frontdoor_actual_request_path == ""
    assert session._frontdoor_actual_request_history == []
    assert session._frontdoor_prompt_cache_key_hash == "family-hash-planned"
    assert session._frontdoor_actual_request_hash == ""
    assert session._frontdoor_actual_request_message_count == 0
    assert session._frontdoor_actual_tool_schema_hash == ""


def test_create_agent_runner_sync_allows_finalize_to_extend_existing_actual_request_baseline() -> None:
    session = RuntimeAgentSession(
        SimpleNamespace(model="demo", reasoning_effort=None, multi_agent_runner=None),
        session_key="web:shared",
        channel="web",
        chat_id="shared",
    )
    session._frontdoor_request_body_messages = [
        {"role": "system", "content": "SYSTEM"},
        {"role": "user", "content": "old question"},
        {"role": "assistant", "content": "tool thinking"},
    ]
    session._frontdoor_actual_request_path = str(Path("D:/tmp/frontdoor-request-current.json"))
    session._frontdoor_actual_request_history = [
        {
            "path": str(Path("D:/tmp/frontdoor-request-current.json")),
            "turn_id": "turn-frontdoor-current",
            "actual_request_hash": "request-hash-current",
            "actual_request_message_count": 3,
            "actual_tool_schema_hash": "tool-hash-current",
            "prompt_cache_key_hash": "family-hash-current",
        }
    ]
    session._frontdoor_prompt_cache_key_hash = "family-hash-current"
    session._frontdoor_actual_request_hash = "request-hash-current"
    session._frontdoor_actual_request_message_count = 3
    session._frontdoor_actual_tool_schema_hash = "tool-hash-current"
    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace(main_task_service=None))

    runner._sync_runtime_session_frontdoor_state(
        state={
            "compression_state": {"status": "", "text": "", "source": "", "needs_recheck": False},
            "semantic_context_state": {},
            "frontdoor_request_body_messages": [
                {"role": "system", "content": "SYSTEM"},
                {"role": "user", "content": "old question"},
                {"role": "assistant", "content": "tool thinking"},
                {"role": "assistant", "content": "final answer"},
            ],
        },
        session=session,
    )

    assert session._frontdoor_request_body_messages == [
        {"role": "system", "content": "SYSTEM"},
        {"role": "user", "content": "old question"},
        {"role": "assistant", "content": "tool thinking"},
        {"role": "assistant", "content": "final answer"},
    ]
    assert session._frontdoor_actual_request_path == str(Path("D:/tmp/frontdoor-request-current.json"))
    assert session._frontdoor_actual_request_history == [
        {
            "path": str(Path("D:/tmp/frontdoor-request-current.json")),
            "turn_id": "turn-frontdoor-current",
            "actual_request_hash": "request-hash-current",
            "actual_request_message_count": 3,
            "actual_tool_schema_hash": "tool-hash-current",
            "prompt_cache_key_hash": "family-hash-current",
        }
    ]
    assert session._frontdoor_actual_request_hash == "request-hash-current"
    assert session._frontdoor_actual_request_message_count == 3
    assert session._frontdoor_actual_tool_schema_hash == "tool-hash-current"


def test_create_agent_runner_sync_blocks_internal_only_heartbeat_baseline_regression() -> None:
    session = SimpleNamespace(
        _frontdoor_request_body_messages=[
            {"role": "system", "content": "SYSTEM"},
            {"role": "user", "content": "original user request"},
            {"role": "assistant", "content": "original assistant answer"},
        ],
        _frontdoor_actual_request_path="D:/tmp/existing-frontdoor-request.json",
        _frontdoor_actual_request_history=[
            {
                "path": "D:/tmp/existing-frontdoor-request.json",
                "turn_id": "turn-existing",
                "actual_request_hash": "existing-hash",
                "actual_request_message_count": 3,
                "actual_tool_schema_hash": "existing-tool-hash",
                "prompt_cache_key_hash": "existing-family-hash",
            }
        ],
        _frontdoor_prompt_cache_key_hash="existing-family-hash",
        _frontdoor_actual_request_hash="existing-hash",
        _frontdoor_actual_request_message_count=3,
        _frontdoor_actual_tool_schema_hash="existing-tool-hash",
        _frontdoor_history_shrink_reason="",
        _sync_completed_continuity_snapshot=lambda *, source_reason, internal_turn=False: continuity_syncs.append(source_reason),
    )
    continuity_syncs: list[str] = []
    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace(main_task_service=None))

    runner._sync_runtime_session_frontdoor_state(
        state={
            "compression_state": {"status": "", "text": "", "source": "", "needs_recheck": False},
            "semantic_context_state": {},
            "heartbeat_internal": True,
            "frontdoor_request_body_messages": [
                {
                    "role": "system",
                    "content": "This is a background heartbeat. Do not explain internal mechanics.",
                },
                {
                    "role": "user",
                    "content": "This is a background heartbeat. Do not explain internal mechanics.\n\n## EVENT BUNDLE",
                },
            ],
            "frontdoor_actual_request_path": "D:/tmp/heartbeat-request.json",
            "frontdoor_actual_request_history": [
                {
                    "path": "D:/tmp/heartbeat-request.json",
                    "turn_id": "turn-heartbeat",
                    "actual_request_hash": "heartbeat-hash",
                    "actual_request_message_count": 2,
                    "actual_tool_schema_hash": "heartbeat-tool-hash",
                    "prompt_cache_key_hash": "heartbeat-family-hash",
                }
            ],
            "prompt_cache_diagnostics": {
                "prompt_cache_key_hash": "heartbeat-family-hash",
                "actual_request_hash": "heartbeat-hash",
                "actual_request_message_count": 2,
                "actual_tool_schema_hash": "heartbeat-tool-hash",
            },
        },
        session=session,
    )

    assert session._frontdoor_request_body_messages == [
        {"role": "system", "content": "SYSTEM"},
        {"role": "user", "content": "original user request"},
        {"role": "assistant", "content": "original assistant answer"},
    ]
    assert continuity_syncs == []


def test_create_agent_runner_sync_allows_appending_legitimate_heartbeat_continuation() -> None:
    session = SimpleNamespace(
        _frontdoor_request_body_messages=[
            {"role": "system", "content": "SYSTEM"},
            {"role": "user", "content": "original user request"},
            {"role": "assistant", "content": "original assistant answer"},
        ],
        _frontdoor_actual_request_path="D:/tmp/existing-frontdoor-request.json",
        _frontdoor_actual_request_history=[
            {
                "path": "D:/tmp/existing-frontdoor-request.json",
                "turn_id": "turn-existing",
                "actual_request_hash": "existing-hash",
                "actual_request_message_count": 3,
                "actual_tool_schema_hash": "existing-tool-hash",
                "prompt_cache_key_hash": "existing-family-hash",
            }
        ],
        _frontdoor_prompt_cache_key_hash="existing-family-hash",
        _frontdoor_actual_request_hash="existing-hash",
        _frontdoor_actual_request_message_count=3,
        _frontdoor_actual_tool_schema_hash="existing-tool-hash",
        _frontdoor_history_shrink_reason="",
        _sync_completed_continuity_snapshot=lambda *, source_reason, internal_turn=False: continuity_syncs.append(source_reason),
    )
    continuity_syncs: list[str] = []
    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace(main_task_service=None))
    updated_baseline = [
        {"role": "system", "content": "SYSTEM"},
        {"role": "user", "content": "original user request"},
        {"role": "assistant", "content": "original assistant answer"},
        {
            "role": "system",
            "content": "This is a background heartbeat. Do not explain internal mechanics.",
        },
        {"role": "user", "content": "## EVENT BUNDLE\n- task completed"},
    ]

    runner._sync_runtime_session_frontdoor_state(
        state={
            "compression_state": {"status": "", "text": "", "source": "", "needs_recheck": False},
            "semantic_context_state": {},
            "heartbeat_internal": True,
            "frontdoor_request_body_messages": list(updated_baseline),
            "frontdoor_actual_request_path": "D:/tmp/heartbeat-request.json",
            "frontdoor_actual_request_history": [
                {
                    "path": "D:/tmp/heartbeat-request.json",
                    "turn_id": "turn-heartbeat",
                    "actual_request_hash": "heartbeat-hash",
                    "actual_request_message_count": 5,
                    "actual_tool_schema_hash": "heartbeat-tool-hash",
                    "prompt_cache_key_hash": "heartbeat-family-hash",
                }
            ],
            "prompt_cache_diagnostics": {
                "prompt_cache_key_hash": "heartbeat-family-hash",
                "actual_request_hash": "heartbeat-hash",
                "actual_request_message_count": 5,
                "actual_tool_schema_hash": "heartbeat-tool-hash",
            },
        },
        session=session,
    )

    assert session._frontdoor_request_body_messages == updated_baseline
    assert continuity_syncs == ["actual_request_sync"]


def test_runtime_agent_session_preserves_previous_actual_request_trace_for_next_visible_turn() -> None:
    session = RuntimeAgentSession(
        SimpleNamespace(model="demo", reasoning_effort=None, multi_agent_runner=None),
        session_key="web:shared",
        channel="web",
        chat_id="shared",
    )
    session._frontdoor_actual_request_path = str(Path("D:/tmp/frontdoor-request-current.json"))
    session._frontdoor_actual_request_history = [
        {
            "path": str(Path("D:/tmp/frontdoor-request-current.json")),
            "turn_id": "turn-frontdoor-current",
            "actual_request_hash": "request-hash-current",
            "actual_request_message_count": 3,
            "actual_tool_schema_hash": "tool-hash-current",
            "prompt_cache_key_hash": "family-hash-current",
        }
    ]

    session._preserve_frontdoor_actual_request_trace_for_next_visible_turn()

    assert session._frontdoor_previous_actual_request_path == str(Path("D:/tmp/frontdoor-request-current.json"))
    assert session._frontdoor_previous_actual_request_history == [
        {
            "path": str(Path("D:/tmp/frontdoor-request-current.json")),
            "turn_id": "turn-frontdoor-current",
            "actual_request_hash": "request-hash-current",
            "actual_request_message_count": 3,
            "actual_tool_schema_hash": "tool-hash-current",
            "prompt_cache_key_hash": "family-hash-current",
        }
    ]


def test_fresh_turn_live_request_messages_reuses_previous_actual_request_prefix(tmp_path: Path) -> None:
    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace(main_task_service=None))
    previous_record_path = tmp_path / "frontdoor-request-previous.json"
    previous_record_path.write_text(
        json.dumps(
            {
                "request_messages": [
                    {"role": "system", "content": "SYSTEM"},
                    {"role": "user", "content": "old question"},
                    {"role": "assistant", "content": "old retrieved"},
                    {
                        "role": "user",
                        "content": '{"message_type":"frontdoor_runtime_tool_contract","callable_tool_names":["filesystem_stat"]}',
                    },
                    {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "call-1",
                                "type": "function",
                                "function": {"name": "filesystem_stat", "arguments": "{}"},
                            }
                        ],
                    },
                    {
                        "role": "tool",
                        "name": "filesystem_stat",
                        "tool_call_id": "call-1",
                        "content": '{"status":"success"}',
                    },
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    session = SimpleNamespace(
        _frontdoor_previous_actual_request_path=str(previous_record_path),
        _frontdoor_previous_actual_request_history=[
            {
                "path": str(previous_record_path),
                "turn_id": "turn-frontdoor-previous",
            }
        ],
    )

    scaffold = runner._fresh_turn_live_request_messages_from_previous_actual_request(
        session=session,
        stable_messages=[
            {"role": "system", "content": "SYSTEM"},
            {"role": "user", "content": "old question"},
            {"role": "assistant", "content": "old retrieved"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {"name": "filesystem_stat", "arguments": "{}"},
                    }
                ],
            },
            {
                "role": "tool",
                "name": "filesystem_stat",
                "tool_call_id": "call-1",
                "content": '{"status":"success"}',
            },
            {"role": "assistant", "content": "final answer"},
        ],
        live_request_messages=[
            {"role": "system", "content": "SYSTEM"},
            {"role": "user", "content": "old question"},
            {"role": "assistant", "content": "old retrieved"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {"name": "filesystem_stat", "arguments": "{}"},
                    }
                ],
            },
            {
                "role": "tool",
                "name": "filesystem_stat",
                "tool_call_id": "call-1",
                "content": '{"status":"success"}',
            },
            {"role": "assistant", "content": "final answer"},
            {"role": "user", "content": "next user"},
        ],
    )

    assert scaffold == [
        {"role": "system", "content": "SYSTEM"},
        {"role": "user", "content": "old question"},
        {"role": "assistant", "content": "old retrieved"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "filesystem_stat", "arguments": "{}"},
                }
            ],
        },
        {
            "role": "tool",
            "name": "filesystem_stat",
            "tool_call_id": "call-1",
            "content": '{"status":"success"}',
        },
        {"role": "assistant", "content": "final answer"},
        {"role": "user", "content": "next user"},
    ]


def test_fresh_turn_live_request_messages_reuses_previous_actual_request_prefix_despite_trailing_whitespace_drift(
    tmp_path: Path,
) -> None:
    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace(main_task_service=None))
    previous_record_path = tmp_path / "frontdoor-request-previous.json"
    previous_request_messages = [
        {"role": "system", "content": "SYSTEM"},
        {"role": "user", "content": "old question"},
        {
            "role": "user",
            "content": '{"message_type":"frontdoor_runtime_tool_contract","callable_tool_names":["filesystem_stat"]}',
        },
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "filesystem_stat", "arguments": "{}"},
                }
            ],
        },
        {
            "role": "tool",
            "name": "filesystem_stat",
            "tool_call_id": "call-1",
            "content": '{"status":"success"} ',
        },
    ]
    previous_record_path.write_text(
        json.dumps({"request_messages": previous_request_messages}, ensure_ascii=False),
        encoding="utf-8",
    )
    session = SimpleNamespace(
        _frontdoor_previous_actual_request_path=str(previous_record_path),
        _frontdoor_previous_actual_request_history=[
            {
                "path": str(previous_record_path),
                "turn_id": "turn-frontdoor-previous",
            }
        ],
    )

    scaffold = runner._fresh_turn_live_request_messages_from_previous_actual_request(
        session=session,
        stable_messages=[
            {"role": "system", "content": "SYSTEM"},
            {"role": "user", "content": "old question"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {"name": "filesystem_stat", "arguments": "{}"},
                    }
                ],
            },
            {
                "role": "tool",
                "name": "filesystem_stat",
                "tool_call_id": "call-1",
                "content": '{"status":"success"}',
            },
            {"role": "assistant", "content": "final answer"},
        ],
        live_request_messages=[
            {"role": "system", "content": "SYSTEM"},
            {"role": "user", "content": "old question"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {"name": "filesystem_stat", "arguments": "{}"},
                    }
                ],
            },
            {
                "role": "tool",
                "name": "filesystem_stat",
                "tool_call_id": "call-1",
                "content": '{"status":"success"}',
            },
            {"role": "assistant", "content": "final answer"},
            {"role": "user", "content": "next user"},
        ],
    )

    # The stale contract inside the previous actual request is dropped from the
    # scaffold; the real prefix (with whitespace-normalized tool result) is reused.
    assert scaffold == [
        {"role": "system", "content": "SYSTEM"},
        {"role": "user", "content": "old question"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "filesystem_stat", "arguments": "{}"},
                }
            ],
        },
        {
            "role": "tool",
            "name": "filesystem_stat",
            "tool_call_id": "call-1",
            "content": '{"status":"success"} ',
        },
        {"role": "assistant", "content": "final answer"},
        {"role": "user", "content": "next user"},
    ]


def test_restart_restored_trace_reuses_previous_actual_request_scaffold_for_next_visible_turn(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(web_ceo_sessions, "workspace_path", lambda: tmp_path)
    session_key = "web:shared"
    continuity_baseline = [
        {"role": "system", "content": "SYSTEM"},
        {"role": "user", "content": "old question"},
        {"role": "assistant", "content": "old retrieved"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "filesystem_stat", "arguments": "{}"},
                }
            ],
        },
        {
            "role": "tool",
            "name": "filesystem_stat",
            "tool_call_id": "call-1",
            "content": '{"status":"success"}',
        },
    ]
    artifact_record = web_ceo_sessions.persist_frontdoor_actual_request(
        session_key,
        payload={
            "turn_id": "turn-artifact",
            "request_messages": [
                {"role": "system", "content": "SYSTEM"},
                {"role": "user", "content": "old question"},
                {"role": "assistant", "content": "old retrieved"},
                {
                    "role": "user",
                    "content": '{"message_type":"frontdoor_runtime_tool_contract","callable_tool_names":["filesystem_stat"]}',
                },
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "type": "function",
                            "function": {"name": "filesystem_stat", "arguments": "{}"},
                        }
                    ],
                },
                {
                    "role": "tool",
                    "name": "filesystem_stat",
                    "tool_call_id": "call-1",
                    "content": '{"status":"success"}',
                },
            ],
            "prompt_cache_key_hash": "family-artifact",
            "actual_request_hash": "artifact-hash",
            "actual_request_message_count": 6,
            "actual_tool_schema_hash": "tool-artifact",
            "provider_model": "responses:gpt-test",
        },
    )
    web_ceo_sessions.write_completed_continuity_snapshot(
        session_key,
        {
            "frontdoor_request_body_messages": list(continuity_baseline),
            "frontdoor_actual_request_path": "",
            "frontdoor_actual_request_history": [],
            "visible_tool_ids": ["filesystem_stat"],
            "visible_skill_ids": ["writing-plans"],
            "provider_tool_schema_names": ["filesystem_stat"],
            "capability_snapshot_exposure_revision": "exp:continuity",
            "source_reason": "actual_request_sync",
        },
    )
    restored = RuntimeAgentSession(
        SimpleNamespace(model="demo", reasoning_effort=None, multi_agent_runner=None, sessions=SimpleNamespace()),
        session_key=session_key,
        channel="web",
        chat_id="shared",
    )
    restored._preserve_frontdoor_actual_request_trace_for_next_visible_turn()

    assert restored._frontdoor_previous_actual_request_path == artifact_record["path"]
    assert restored._frontdoor_previous_actual_request_history == [artifact_record]

    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace(main_task_service=None))
    scaffold = runner._fresh_turn_live_request_messages_from_previous_actual_request(
        session=restored,
        stable_messages=[
            {"role": "system", "content": "SYSTEM"},
            {"role": "user", "content": "old question"},
            {"role": "assistant", "content": "old retrieved"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {"name": "filesystem_stat", "arguments": "{}"},
                    }
                ],
            },
            {
                "role": "tool",
                "name": "filesystem_stat",
                "tool_call_id": "call-1",
                "content": '{"status":"success"}',
            },
            {"role": "assistant", "content": "final answer"},
        ],
        live_request_messages=[
            {"role": "system", "content": "SYSTEM"},
            {"role": "user", "content": "old question"},
            {"role": "assistant", "content": "old retrieved"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {"name": "filesystem_stat", "arguments": "{}"},
                    }
                ],
            },
            {
                "role": "tool",
                "name": "filesystem_stat",
                "tool_call_id": "call-1",
                "content": '{"status":"success"}',
            },
            {"role": "assistant", "content": "final answer"},
            {"role": "user", "content": "next user"},
        ],
    )

    assert scaffold == [
        {"role": "system", "content": "SYSTEM"},
        {"role": "user", "content": "old question"},
        {"role": "assistant", "content": "old retrieved"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "filesystem_stat", "arguments": "{}"},
                }
            ],
        },
        {
            "role": "tool",
            "name": "filesystem_stat",
            "tool_call_id": "call-1",
            "content": '{"status":"success"}',
        },
        {"role": "assistant", "content": "final answer"},
        {"role": "user", "content": "next user"},
    ]


def test_fresh_turn_tool_schema_seed_does_not_expand_previous_actual_request_schemas_when_current_is_superset(
    tmp_path: Path,
) -> None:
    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace(main_task_service=None))
    previous_record_path = tmp_path / "frontdoor-request-previous.json"
    previous_record_path.write_text(
        json.dumps(
            {
                "tool_schemas": [
                    {"type": "function", "function": {"name": "exec", "description": "", "parameters": {"type": "object"}}},
                    {
                        "type": "function",
                        "function": {"name": "filesystem_stat", "description": "", "parameters": {"type": "object"}},
                    },
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    session = SimpleNamespace(
        _frontdoor_previous_actual_request_path=str(previous_record_path),
        _frontdoor_previous_actual_request_history=[
            {
                "path": str(previous_record_path),
                "turn_id": "turn-frontdoor-previous",
            }
        ],
    )

    seeded_schemas, seeded_names = runner._fresh_turn_tool_schema_seed_from_previous_actual_request(
        session=session,
        tool_schemas=[
            {"type": "function", "function": {"name": "exec", "description": "", "parameters": {"type": "object"}}},
            {
                "type": "function",
                "function": {"name": "web_fetch", "description": "", "parameters": {"type": "object"}},
            },
            {
                "type": "function",
                "function": {"name": "filesystem_stat", "description": "", "parameters": {"type": "object"}},
            },
        ],
    )

    assert seeded_names is None
    assert seeded_schemas == [
        {"type": "function", "function": {"name": "exec", "description": "", "parameters": {"type": "object"}}},
        {
            "type": "function",
            "function": {"name": "web_fetch", "description": "", "parameters": {"type": "object"}},
        },
        {
            "type": "function",
            "function": {"name": "filesystem_stat", "description": "", "parameters": {"type": "object"}},
        },
    ]


def test_frontdoor_provider_tool_bundle_pins_and_recommits_only_at_compression() -> None:
    """前门清单完全冻结：增删都只发生在重印边界。

    `tools[]` 排在上下文最前面，成员或顺序一变就把身后整段重铺。新增能力也不再在回合
    起点并入——期间靠尾块 `undeclared_candidates` 点名、load 返回的参数契约承载调用，
    声明在压缩/空闲/换车道重印时一次性吸收。
    """
    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace(main_task_service=None))

    def resolve(active, desired, *, commit_reason=""):
        return runner._resolve_frontdoor_provider_tool_exposure(
            active_provider_tool_names=list(active),
            pending_provider_tool_names=[],
            desired_provider_tool_names=list(desired),
            commit_reason=commit_reason,
        )

    addition = resolve(["exec", "filesystem_stat"], ["exec", "web_fetch", "filesystem_stat"])
    # 冻结分支不并入新增：tools[] 到重印边界才吸收新名字
    assert addition["provider_tool_names"] == ["exec", "filesystem_stat"]
    assert addition["provider_tool_bundle_mode"] == "pinned_frozen"
    assert addition["pending_provider_tool_names"] == []
    assert addition["provider_tool_exposure_pending"] is False
    assert addition["provider_tool_exposure_commit_reason"] == ""

    stage_compaction = resolve(["exec", "filesystem_stat"], ["exec", "web_fetch", "filesystem_stat"],
                               commit_reason="stage_compaction")
    # stage_compaction 不是重印判据（只有 token_compression/显式标记是），照样冻结
    assert stage_compaction["provider_tool_names"] == ["exec", "filesystem_stat"]
    assert stage_compaction["provider_tool_bundle_mode"] == "pinned_frozen"

    removal = resolve(["exec", "web_fetch"], ["exec"])
    # RBAC 收回不摘名：执行准入另有闸门，清单这边等重印
    assert removal["provider_tool_names"] == ["exec", "web_fetch"]
    assert removal["provider_tool_bundle_mode"] == "pinned_frozen"

    token_compression = resolve(["exec", "web_fetch"], ["exec"], commit_reason="token_compression")
    # 压缩跳才真的把僵尸名出清
    assert token_compression["provider_tool_names"] == ["exec"]
    assert token_compression["provider_tool_bundle_mode"] == "pinned_recommitted"

    re_add_after_recommit = resolve(["exec"], ["exec", "web_fetch"], commit_reason="token_compression")
    assert re_add_after_recommit["provider_tool_names"] == ["exec", "web_fetch"]
    assert re_add_after_recommit["provider_tool_bundle_mode"] == "pinned_recommitted"

    seeded = resolve([], ["exec", "web_fetch"])
    # 没有可沿用的钉住声明时按当轮全集播种：新会话首跳、以及编辑重发/Fork 截断到
    # 会话开头之后那一跳（空基线载荷会把声明面与头部一起撤掉）都走这一支。
    assert seeded["provider_tool_names"] == ["exec", "web_fetch"]
    assert seeded["provider_tool_bundle_mode"] == "pinned_seeded"

def test_frontdoor_denied_names_use_live_grant_not_turn_snapshot() -> None:
    """denied_tools 必须对照实时授权，不是回合内缓存的 capability_snapshot。

    实盘翻过一次：收回后 callable 已经不带那个工具，但装配期快照还带着它，
    于是清单里那个"已声明无权限"的名字没人点名，模型只能撞一次拒绝才知道。
    """
    service = SimpleNamespace(
        list_effective_tool_names=lambda *, actor_role, session_id: ["exec", "filesystem_stat"]
    )
    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace(main_task_service=service))

    granted = runner._frontdoor_live_granted_tool_names(session_key="web:shared")
    assert granted == ["exec", "filesystem_stat"]
    assert runner._frontdoor_declared_denied_tool_names(
        declared_tool_names=["exec", "web_fetch", "filesystem_stat"],
        granted_tool_names=granted,
    ) == ["web_fetch"]

    # 实时授权读不到时不产出这一行：宁可不说，也不拿旧快照说错
    broken = create_agent_impl.CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace(main_task_service=None))
    assert broken._frontdoor_live_granted_tool_names(session_key="web:shared") == []


def test_frontdoor_finalize_drops_rbac_revoked_mid_turn_tool_from_callable() -> None:
    """非声明的在场名被 RBAC 收回时，收尾当跳把它摘出 callable。

    它不在 `tools[]` 里，denied_tools 点不到名；留在 callable 只会让模型拿着
    "可调用"的预告去撞一次拒绝。RBAC 重新放行后由发现出口重新送回候选。
    """
    service = SimpleNamespace(
        list_effective_tool_names=lambda *, actor_role, session_id: ["exec"]
    )
    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(
        loop=SimpleNamespace(main_task_service=service, tools={}),
    )
    state = {
        "session_key": "web:shared",
        # 契约块真的在请求视图里：这一跳 `windows_mcp` 本来在场、本来可调，
        # 于是被摘下的原因只能是 RBAC，而不是"从没 load 过"。
        "messages": [_contract_block_message("windows_mcp")],
        "provider_tool_names": ["exec"],
        "rbac_visible_tool_names": ["exec", "windows_mcp"],
        "tool_names": ["exec", "windows_mcp"],
    }
    assert "windows_mcp" in runner._frontdoor_contract_present_names(state)

    result = runner._frontdoor_tool_state_after_tool_results(state=state)

    assert result["tool_names"] == ["exec"]
    # callable 只在场判据这一份账：工具态写回不产出任何 hydration 键。
    assert "hydrated_tool_names" not in result
    assert "hydration_revoked_executor_names" not in result


def test_frontdoor_finalize_surfaces_live_visible_undeclared_tool_in_candidates() -> None:
    """`tools[]` 钉死期间的发现出口：治理实时放行、声明未吸收的名字并回候选，
    尾块渲成 `undeclared_candidates`（能 load 但参数表还没进本次 tools[]）。"""

    service = SimpleNamespace(
        list_effective_tool_names=lambda *, actor_role, session_id: ["exec", "fresh_tool"]
    )
    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(
        loop=SimpleNamespace(main_task_service=service, tools={}),
    )
    state = {
        "session_key": "web:shared",
        "messages": [],
        "provider_tool_names": ["exec"],
        "rbac_visible_tool_names": ["exec"],
        "tool_names": ["exec"],
        "candidate_tool_names": [],
    }

    result = runner._frontdoor_tool_state_after_tool_results(state=state)

    assert "fresh_tool" in result["candidate_tool_names"]
    assert {"tool_id": "fresh_tool", "description": ""} in result["candidate_tool_items"]
    assert "fresh_tool" not in result["tool_names"]


def test_frontdoor_dispatch_stays_live_while_declaration_is_pinned() -> None:
    """钉住只作用于声明，派发仍按当轮治理可见集——声明滞后不能变成权限滞后。

    节点道的执行查表本来就按当轮字典取名，前门过去是"声明≡派发"所以天然一致；
    清单钉住后这条等式不再成立，必须显式把派发收窄，否则被收回的工具仍会留在可执行集合里。
    """
    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace(main_task_service=None))

    state = {
        "provider_tool_names": ["exec", "web_fetch", "filesystem_stat"],
        "rbac_visible_tool_names": ["exec", "filesystem_stat"],
        "tool_names": ["exec"],
    }

    assert runner._frontdoor_dispatch_tool_names(state) == ["exec", "filesystem_stat"]
    assert runner._frontdoor_declared_denied_tool_names(
        declared_tool_names=state["provider_tool_names"],
        granted_tool_names=state["rbac_visible_tool_names"],
    ) == ["web_fetch"]

    # 治理可见集取不到时不猜：派发退回当轮 callable pool，也不拿声明全集兜底
    no_grant = {
        "provider_tool_names": ["exec", "web_fetch"],
        "rbac_visible_tool_names": [],
        "tool_names": ["exec"],
    }
    assert runner._frontdoor_dispatch_tool_names(no_grant) == ["exec"]
    assert runner._frontdoor_declared_denied_tool_names(
        declared_tool_names=no_grant["provider_tool_names"],
        granted_tool_names=no_grant["rbac_visible_tool_names"],
    ) == []

def test_frontdoor_send_contract_reports_denied_tools_in_notice() -> None:
    """尾块退役后 denied_tools 由发送前的单生产者投进一次性通知。"""
    service = SimpleNamespace(
        list_effective_tool_names=lambda *, actor_role, session_id: ["exec", "filesystem_stat"]
    )
    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace(main_task_service=service))
    runner._ceo_session_temp_dir = lambda _session_key: ""
    state = _canonical_frontdoor_state(
        messages=[{"role": "user", "content": "hello"}],
        provider_tool_names=["exec", "web_fetch", "filesystem_stat"],
        rbac_visible_tool_names=["exec", "filesystem_stat"],
        tool_names=["exec"],
        session_key="web:shared",
    )

    messages = runner._apply_frontdoor_skill_roster_notice(
        [{"role": "user", "content": "hello"}],
        session_key="web:shared",
        state=state,
    )
    notice_texts = [
        str(item.get("content") or "")
        for item in messages
        if str(item.get("content") or "").startswith("[RUNTIME_NOTICE]")
    ]
    assert len(notice_texts) == 1
    assert "denied_tools" in notice_texts[0]
    assert "web_fetch" in notice_texts[0]


def test_build_frontdoor_request_artifact_payload_includes_provider_tool_exposure_fields() -> None:
    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace(main_task_service=None))

    payload = runner._build_frontdoor_request_artifact_payload(
        state={
            "model_refs": ["gpt-5.2"],
            "frontdoor_history_shrink_reason": "token_compression",
            "frontdoor_token_preflight_diagnostics": {"provider_model": "gpt-5.2"},
            "provider_tool_exposure_revision": "pte:frontdoor-revision",
            "provider_tool_exposure_commit_reason": "token_compression",
        },
        session_key="web:shared",
        turn_id="turn-1",
        request_messages=[
            {"role": "system", "content": "system"},
            {"role": "user", "content": "user"},
        ],
        tool_schemas=[
            {"type": "function", "function": {"name": "exec", "parameters": {"type": "object"}}},
        ],
        prompt_cache_key="cache-key",
        prompt_cache_diagnostics=build_actual_request_diagnostics(
            request_messages=[
                {"role": "system", "content": "system"},
                {"role": "user", "content": "user"},
            ],
            tool_schemas=[
                {"type": "function", "function": {"name": "exec", "parameters": {"type": "object"}}},
            ],
        ),
        parallel_tool_calls=False,
        provider_request_meta={"provider": "responses"},
        provider_request_body={"model": "gpt-5.2"},
        usage={"input_tokens": 10, "output_tokens": 5},
        request_kind="frontdoor_actual_request",
        request_lane="visible_frontdoor",
    )

    assert payload["provider_tool_exposure_revision"] == "pte:frontdoor-revision"
    assert payload["provider_tool_exposure_commit_reason"] == "token_compression"


def test_runtime_agent_session_completed_continuity_bridge_survives_capability_change() -> None:
    """续跑首跳的可见能力集变了，也不许摘掉钉住的那份名单与曝光锚。

    摘名等于在这一跳重铺 `tools[]`（清单排在正文最前面，成员一变身后全部重付），而重启不是
    重印边界。能力收回的承载点是执行准入与尾块 `denied_tools`，不是清单。
    """
    session = RuntimeAgentSession(
        SimpleNamespace(model="demo", reasoning_effort=None, multi_agent_runner=None),
        session_key="web:shared",
        channel="web",
        chat_id="shared",
    )
    session._frontdoor_capability_snapshot_exposure_revision = "exp:demo"
    session._frontdoor_visible_tool_ids = ["exec", "web_fetch"]
    session._frontdoor_visible_skill_ids = ["web-access"]
    session._frontdoor_provider_tool_schema_names = ["exec", "web_fetch"]

    expected = {
        "carried": True,
        "exposure_revision": "exp:demo",
        "provider_tool_schema_names": ["exec", "web_fetch"],
    }

    for label, current_tool_ids, current_skill_ids in (
        ("tool added", ["exec", "web_fetch", "filesystem_write"], ["web-access"]),
        ("tool revoked", ["exec"], ["web-access"]),
        ("skill added", ["exec", "web_fetch"], ["web-access", "writing-plans"]),
        ("skill revoked", ["exec", "web_fetch"], []),
        ("reordered", ["web_fetch", "exec"], ["web-access"]),
    ):
        session._frontdoor_completed_continuity_bridge_pending = True
        assert session._consume_completed_continuity_bridge() == expected, label

    session._frontdoor_completed_continuity_bridge_pending = True
    consumed_once = session._consume_completed_continuity_bridge()
    # 消耗式：同一份基线只在续跑第一跳带一次，之后按当轮名单走。
    assert consumed_once == expected
    assert session._consume_completed_continuity_bridge() == {"carried": False}

    session._frontdoor_completed_continuity_bridge_pending = False
    assert session._consume_completed_continuity_bridge() == {"carried": False}


def test_completed_continuity_snapshot_carries_pinned_head_into_restored_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """重启后第一跳的头部原文必须还是上一跳发出去的那份，即使停机期间可见 skill 集变过。

    钉住块与 roster epoch 都是进程内的，不落盘就只能按当轮可见集重渲；头部是第一条消息，
    少一行就让身后全部存活正文重付（实盘 10-19 22:35:35 那一跳删掉一个探针 skill，头部少
    一行、清单少一名，该跳 10,452 uncached，同会话下一跳回到 2,281）。
    """
    from g3ku.runtime.frontdoor import tool_contract as tc

    monkeypatch.setattr(web_ceo_sessions, "workspace_path", lambda: tmp_path)
    tc.clear_pinned_contract_stores()
    session_key = "web:shared"
    temp_dir = "C:/temp/ceo/web_shared"
    baseline = [
        {"role": "system", "content": "SYSTEM"},
        {"role": "user", "content": "continuity question"},
        {"role": "assistant", "content": "continuity answer"},
    ]
    loop = SimpleNamespace(model="demo", reasoning_effort=None, multi_agent_runner=None, sessions=SimpleNamespace())
    pinned_text = tc.frontdoor_pinned_contract_text(
        None,
        skill_items=[
            {"skill_id": "alpha", "display_name": "Alpha", "description": "first"},
            {"skill_id": "beta", "display_name": "Beta", "description": "second"},
        ],
        exec_runtime_policy=None,
        session_temp_dir=temp_dir,
        skill_roster_epoch=3,
        session_key=session_key,
    )
    assert "beta" in pinned_text

    writer = RuntimeAgentSession(loop, session_key=session_key, channel="web", chat_id="shared")
    writer._frontdoor_request_body_messages = list(baseline)
    writer._frontdoor_provider_tool_schema_names = ["exec"]
    writer._frontdoor_capability_snapshot_exposure_revision = "exp:continuity"
    writer._frontdoor_visible_tool_ids = ["exec"]
    writer._frontdoor_visible_skill_ids = ["alpha", "beta"]
    # 会话属性是钉住块的第二载体（`_write_pinned_contract_entry` 与 epoch 都写这两份）。
    writer._frontdoor_pinned_contract_text = pinned_text
    writer._frontdoor_pinned_contract_revision = tc.pinned_contract_revision_key(
        exec_runtime_policy=None, session_temp_dir=temp_dir, skill_roster_epoch=3
    )
    writer._frontdoor_pinned_contract_skill_ids = ["alpha", "beta"]
    writer._frontdoor_skill_roster_epoch = 3
    writer._sync_completed_continuity_snapshot(source_reason="actual_request_sync")

    # 重启：进程内两份载体全空，只有磁盘快照活下来。
    tc.clear_pinned_contract_stores()
    restored = RuntimeAgentSession(loop, session_key=session_key, channel="web", chat_id="shared")
    assert restored._frontdoor_request_body_messages == baseline
    assert tc.skill_roster_epoch_for(restored, session_key=session_key) == 3

    # 停机期间删掉 beta：可见集只剩 alpha，头部仍必须逐字节复用钉住的那份。
    replayed = tc.frontdoor_pinned_contract_text(
        restored,
        skill_items=[{"skill_id": "alpha", "display_name": "Alpha", "description": "first"}],
        exec_runtime_policy=None,
        session_temp_dir=temp_dir,
        skill_roster_epoch=tc.skill_roster_epoch_for(restored, session_key=session_key),
        session_key=session_key,
    )
    assert replayed == pinned_text

    # 真的到了重印边界才重渲，钉住不是永久冻结。
    tc.bump_skill_roster_epoch(restored, session_key=session_key)
    reprint = tc.frontdoor_pinned_contract_text(
        restored,
        skill_items=[{"skill_id": "alpha", "display_name": "Alpha", "description": "first"}],
        exec_runtime_policy=None,
        session_temp_dir=temp_dir,
        skill_roster_epoch=tc.skill_roster_epoch_for(restored, session_key=session_key),
        session_key=session_key,
    )
    assert "beta" not in reprint


def test_fresh_turn_tool_schema_seed_skips_expected_bridge_when_previous_names_do_not_match(
    tmp_path: Path,
) -> None:
    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace(main_task_service=None))
    previous_record_path = tmp_path / "frontdoor-request-previous.json"
    previous_record_path.write_text(
        json.dumps(
            {
                "tool_schemas": [
                    {"type": "function", "function": {"name": "exec", "description": "", "parameters": {"type": "object"}}},
                    {
                        "type": "function",
                        "function": {"name": "filesystem_stat", "description": "", "parameters": {"type": "object"}},
                    },
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    session = SimpleNamespace(
        _frontdoor_previous_actual_request_path=str(previous_record_path),
        _frontdoor_previous_actual_request_history=[
            {
                "path": str(previous_record_path),
                "turn_id": "turn-frontdoor-previous",
            }
        ],
    )
    current_schemas = [
        {"type": "function", "function": {"name": "exec", "description": "", "parameters": {"type": "object"}}},
        {
            "type": "function",
            "function": {"name": "web_fetch", "description": "", "parameters": {"type": "object"}},
        },
        {
            "type": "function",
            "function": {"name": "filesystem_stat", "description": "", "parameters": {"type": "object"}},
        },
    ]

    seeded_schemas, seeded_names = runner._fresh_turn_tool_schema_seed_from_previous_actual_request(
        session=session,
        tool_schemas=current_schemas,
        expected_schema_names=["exec", "web_fetch"],
    )

    assert seeded_names is None
    assert seeded_schemas == current_schemas


def test_live_request_message_records_for_state_prefers_explicit_frontdoor_live_request_messages() -> None:
    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace(main_task_service=None))

    records = runner._live_request_message_records_for_state(
        state={
            "messages": [
                {"role": "system", "content": "SYSTEM"},
                {"role": "user", "content": "stable baseline"},
            ],
            "frontdoor_live_request_messages": [
                {"role": "system", "content": "SYSTEM"},
                {"role": "user", "content": "previous actual request"},
                {"role": "assistant", "content": "final answer"},
                {"role": "user", "content": "next user"},
            ],
        }
    )

    assert records == [
        {"role": "system", "content": "SYSTEM"},
        {"role": "user", "content": "previous actual request"},
        {"role": "assistant", "content": "final answer"},
        {"role": "user", "content": "next user"},
    ]


@pytest.mark.asyncio
async def test_graph_call_model_fresh_turn_reuses_previous_message_artifact_prefix_after_message_tool_finalize(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_key = "web:shared"
    runtime_session = SimpleNamespace(session_key=session_key, messages=[])
    loop = SimpleNamespace(
        sessions=SimpleNamespace(get_or_create=lambda key: runtime_session),
        main_task_service=None,
        tools={},
        max_iterations=8,
        workspace=None,
        temp_dir="",
    )
    runner = ceo_runner.CeoFrontDoorRunner(loop=loop)
    captured_model_messages: list[dict[str, object]] = []

    monkeypatch.setattr(ceo_runtime_ops, "current_project_environment", lambda workspace_root=None: {})

    previous_request_path = tmp_path / "frontdoor-request-previous.json"
    previous_request_messages = [
        {"role": "system", "content": "SYSTEM"},
        {"role": "user", "content": "old question"},
        {"role": "assistant", "content": "older answer"},
        {
            "role": "user",
            "content": '{"message_type":"frontdoor_runtime_tool_contract","callable_tool_names":["exec","filesystem_stat"]}',
        },
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call-exec-1",
                    "type": "function",
                    "function": {"name": "exec", "arguments": "{}"},
                }
            ],
        },
        {
            "role": "tool",
            "name": "exec",
            "tool_call_id": "call-exec-1",
            "content": "Command finished for final:ceo-demo",
        },
    ]
    previous_request_path.write_text(
        json.dumps(
            {
                "request_messages": previous_request_messages,
                "tool_schemas": [
                    {
                        "type": "function",
                        "function": {"name": "exec", "description": "", "parameters": {"type": "object"}},
                    },
                    {
                        "type": "function",
                        "function": {"name": "filesystem_stat", "description": "", "parameters": {"type": "object"}},
                    },
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    async def _resolve_for_actor(*, actor_role: str, session_id: str):
        _ = actor_role, session_id
        return {"skills": [], "tool_families": [], "tool_names": ["exec", "filesystem_stat"]}

    async def _build_for_ceo(**kwargs):
        stable_messages = list(kwargs.get("request_body_seed_messages") or [])
        user_content = str(kwargs.get("user_content") or "").strip()
        model_messages = [*stable_messages, {"role": "user", "content": user_content}]
        return SimpleNamespace(
            tool_names=["exec", "filesystem_stat"],
            model_messages=model_messages,
            stable_messages=list(stable_messages),
            dynamic_appendix_messages=[],
            candidate_tool_names=[],
            candidate_tool_items=[],
            trace={
                "selected_skills": [],
                "tool_selection": {},
                "capability_snapshot": {
                    "visible_tool_ids": ["exec", "filesystem_stat"],
                    "visible_skill_ids": [],
                },
            },
            cache_family_revision="frontdoor:v1",
            turn_overlay_text="",
        )

    async def _call_model_with_tools(**kwargs):
        captured_model_messages[:] = list(kwargs.get("messages") or [])
        return {"content": "ok"}

    monkeypatch.setattr(runner._resolver, "resolve_for_actor", _resolve_for_actor)
    monkeypatch.setattr(runner._builder, "build_for_ceo", _build_for_ceo)
    monkeypatch.setattr(runner, "_resolve_ceo_model_refs", lambda: ["openai_codex:gpt-test"])
    monkeypatch.setattr(
        runner,
        "_frontdoor_tool_schemas_for_state",
        lambda *, state, runtime: [
            {
                "type": "function",
                "function": {"name": str(name), "description": "", "parameters": {"type": "object"}},
            }
            for name in list(state.get("provider_tool_names") or state.get("tool_names") or [])
        ],
    )
    monkeypatch.setattr(
        runner,
        "_resolve_frontdoor_send_model_context_window",
        lambda **_: {
            "model_key": "openai_codex:gpt-test",
            "provider_model": "openai_codex:gpt-test",
            "context_window_tokens": 128000,
        },
    )
    monkeypatch.setattr(runner, "_call_model_with_tools", _call_model_with_tools)
    monkeypatch.setattr(runner, "_checkpoint_safe_model_response_payload", lambda _message: {"ok": True})
    monkeypatch.setattr(runner, "_persist_frontdoor_actual_request", lambda **_: {})

    session = SimpleNamespace(
        state=SimpleNamespace(session_key=session_key),
        _memory_channel="web",
        _memory_chat_id="shared",
        _channel="web",
        _chat_id="shared",
        _active_cancel_token=None,
        inflight_turn_snapshot=lambda: None,
        _frontdoor_request_body_messages=[
            {"role": "system", "content": "SYSTEM"},
            {"role": "user", "content": "old question"},
            {"role": "assistant", "content": "older answer"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call-exec-1",
                        "type": "function",
                        "function": {"name": "exec", "arguments": "{}"},
                    }
                ],
            },
            {
                "role": "tool",
                "name": "exec",
                "tool_call_id": "call-exec-1",
                "content": "Command finished for final:ceo-demo",
            },
            {"role": "assistant", "content": "final answer"},
        ],
        _frontdoor_history_shrink_reason="",
        _compression_state={"status": "", "text": "", "source": "", "needs_recheck": False},
        _semantic_context_state={"summary_text": "", "needs_refresh": False},
        _frontdoor_selection_debug={},
        _frontdoor_previous_actual_request_path=str(previous_request_path),
        _frontdoor_previous_actual_request_history=[{"path": str(previous_request_path), "turn_id": "turn-old"}],
        _frontdoor_actual_request_path="",
        _frontdoor_actual_request_history=[],
    )
    runtime = SimpleNamespace(
        context=SimpleNamespace(loop=loop, session=session, session_key=session_key, on_progress=None)
    )

    prepared = await runner._graph_prepare_turn(
        initial_persistent_state(user_input={"content": "next question", "metadata": {}}),
        runtime=runtime,
    )
    await runner._graph_call_model(
        prepared,
        runtime=runtime,
    )

    # The stale contract inside the previous actual request is never reused as a
    # scaffold prefix; the real prefix (system/user/assistant/tool) survives.
    expected_scaffold_prefix = [
        item
        for item in previous_request_messages
        if not (
            isinstance(item, dict)
            and str(item.get("role") or "").strip().lower() == "user"
            and "message_type" in str(item.get("content") or "")
        )
    ]
    assert captured_model_messages[0]["role"] == "system"
    assert "## Runtime Contract (pinned)" in str(captured_model_messages[0]["content"])
    expected_tail = expected_scaffold_prefix[1:]
    assert _normalized_tool_content(captured_model_messages[1 : 1 + len(expected_tail)]) == _normalized_tool_content(expected_tail)
    assert {"role": "assistant", "content": "final answer"} in captured_model_messages
    final_answer_index = captured_model_messages.index({"role": "assistant", "content": "final answer"})
    next_user_index = captured_model_messages.index({"role": "user", "content": "next question"})
    assert final_answer_index < next_user_index


@pytest.mark.asyncio
async def test_graph_call_model_runs_token_preflight_after_fresh_turn_seed_and_before_provider_send(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_key = "web:shared"
    runtime_session = SimpleNamespace(session_key=session_key, messages=[])
    loop = SimpleNamespace(
        sessions=SimpleNamespace(get_or_create=lambda key: runtime_session),
        main_task_service=None,
        tools={},
        max_iterations=8,
        workspace=None,
        temp_dir="",
    )
    runner = ceo_runner.CeoFrontDoorRunner(loop=loop)
    captured_model_messages: list[dict[str, object]] = []
    observed: dict[str, object] = {}

    monkeypatch.setattr(ceo_runtime_ops, "current_project_environment", lambda workspace_root=None: {})

    previous_request_path = tmp_path / "frontdoor-request-previous.json"
    previous_request_messages = [
        {"role": "system", "content": "SYSTEM"},
        {"role": "user", "content": "old question"},
        {"role": "assistant", "content": "older answer"},
        {
            "role": "user",
            "content": '{"message_type":"frontdoor_runtime_tool_contract","callable_tool_names":["exec","filesystem_stat"]}',
        },
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call-exec-1",
                    "type": "function",
                    "function": {"name": "exec", "arguments": "{}"},
                }
            ],
        },
        {
            "role": "tool",
            "name": "exec",
            "tool_call_id": "call-exec-1",
            "content": "Command finished for final:ceo-demo",
        },
    ]
    previous_request_path.write_text(
        json.dumps(
            {
                "request_messages": previous_request_messages,
                "tool_schemas": [
                    {
                        "type": "function",
                        "function": {"name": "exec", "description": "", "parameters": {"type": "object"}},
                    },
                    {
                        "type": "function",
                        "function": {"name": "filesystem_stat", "description": "", "parameters": {"type": "object"}},
                    },
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    async def _resolve_for_actor(*, actor_role: str, session_id: str):
        _ = actor_role, session_id
        return {"skills": [], "tool_families": [], "tool_names": ["exec", "filesystem_stat"]}

    async def _build_for_ceo(**kwargs):
        stable_messages = list(kwargs.get("request_body_seed_messages") or [])
        user_content = str(kwargs.get("user_content") or "").strip()
        model_messages = [*stable_messages, {"role": "user", "content": user_content}]
        return SimpleNamespace(
            tool_names=["exec", "filesystem_stat"],
            model_messages=model_messages,
            stable_messages=list(stable_messages),
            dynamic_appendix_messages=[],
            candidate_tool_names=[],
            candidate_tool_items=[],
            trace={
                "selected_skills": [],
                "tool_selection": {},
                "capability_snapshot": {
                    "visible_tool_ids": ["exec", "filesystem_stat"],
                    "visible_skill_ids": [],
                },
            },
            cache_family_revision="frontdoor:v1",
            turn_overlay_text="",
        )

    async def _call_model_with_tools(**kwargs):
        captured_model_messages[:] = list(kwargs.get("messages") or [])
        return {"content": "ok"}

    monkeypatch.setattr(runner._resolver, "resolve_for_actor", _resolve_for_actor)
    monkeypatch.setattr(runner._builder, "build_for_ceo", _build_for_ceo)
    monkeypatch.setattr(runner, "_resolve_ceo_model_refs", lambda: ["openai_codex:gpt-test"])
    monkeypatch.setattr(
        runner,
        "_resolve_frontdoor_send_model_context_window",
        lambda **_: {
            "model_key": "openai_codex:gpt-test",
            "provider_model": "openai_codex:gpt-test",
            "context_window_tokens": 128000,
        },
        raising=False,
    )
    monkeypatch.setattr(
        runner,
        "_selected_tool_schemas",
        lambda tool_names: [
            {
                "type": "function",
                "function": {"name": str(name), "description": "", "parameters": {"type": "object"}},
            }
            for name in list(tool_names or [])
        ],
    )
    monkeypatch.setattr(
        runner,
        "_frontdoor_tool_schemas_for_state",
        lambda *, state, runtime: [
            {
                "type": "function",
                "function": {"name": str(name), "description": "", "parameters": {"type": "object"}},
            }
            for name in list(state.get("provider_tool_names") or state.get("tool_names") or [])
        ],
    )
    monkeypatch.setattr(runner, "_call_model_with_tools", _call_model_with_tools)
    monkeypatch.setattr(runner, "_checkpoint_safe_model_response_payload", lambda _message: {"ok": True})
    monkeypatch.setattr(runner, "_persist_frontdoor_actual_request", lambda **_: {})
    monkeypatch.setattr(
        runner,
        "_estimate_frontdoor_send_total_tokens",
        lambda **kwargs: observed.update(
            {
                "request_messages": list(kwargs["request_messages"]),
                "tool_schemas": list(kwargs["tool_schemas"]),
            }
        ) or 12345,
        raising=False,
    )

    session = SimpleNamespace(
        state=SimpleNamespace(session_key=session_key),
        _memory_channel="web",
        _memory_chat_id="shared",
        _channel="web",
        _chat_id="shared",
        _active_cancel_token=None,
        inflight_turn_snapshot=lambda: None,
        _frontdoor_request_body_messages=[
            {"role": "system", "content": "SYSTEM"},
            {"role": "user", "content": "old question"},
            {"role": "assistant", "content": "older answer"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call-exec-1",
                        "type": "function",
                        "function": {"name": "exec", "arguments": "{}"},
                    }
                ],
            },
            {
                "role": "tool",
                "name": "exec",
                "tool_call_id": "call-exec-1",
                "content": "Command finished for final:ceo-demo",
            },
            {"role": "assistant", "content": "final answer"},
        ],
        _frontdoor_history_shrink_reason="",
        _compression_state={"status": "", "text": "", "source": "", "needs_recheck": False},
        _semantic_context_state={"summary_text": "", "needs_refresh": False},
        _frontdoor_selection_debug={},
        _frontdoor_previous_actual_request_path=str(previous_request_path),
        _frontdoor_previous_actual_request_history=[{"path": str(previous_request_path), "turn_id": "turn-old"}],
        _frontdoor_actual_request_path="",
        _frontdoor_actual_request_history=[],
    )
    runtime = SimpleNamespace(
        context=SimpleNamespace(loop=loop, session=session, session_key=session_key, on_progress=None)
    )

    prepared = await runner._graph_prepare_turn(
        initial_persistent_state(user_input={"content": "next question", "metadata": {}}),
        runtime=runtime,
    )
    await runner._graph_call_model(
        prepared,
        runtime=runtime,
    )

    # The stale contract inside the previous actual request is never reused as a
    # scaffold prefix; both the preflight observation and the provider send carry
    # the real prefix and re-inject a fresh contract only at the tail.
    expected_scaffold_prefix = [
        item
        for item in previous_request_messages
        if not (
            isinstance(item, dict)
            and str(item.get("role") or "").strip().lower() == "user"
            and "message_type" in str(item.get("content") or "")
        )
    ]
    assert observed["request_messages"][0]["role"] == "system"
    assert "## Runtime Contract (pinned)" in str(observed["request_messages"][0]["content"])
    expected_tail = expected_scaffold_prefix[1:]
    assert observed["request_messages"][1 : 1 + len(expected_tail)] == expected_tail
    assert observed["tool_schemas"]
    assert captured_model_messages[0]["role"] == "system"
    assert "## Runtime Contract (pinned)" in str(captured_model_messages[0]["content"])
    assert _normalized_tool_content(captured_model_messages[1 : 1 + len(expected_tail)]) == _normalized_tool_content(expected_tail)


@pytest.mark.asyncio
async def test_prepare_turn_promotes_uploaded_image_only_into_live_request_when_binding_enabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_live_runtime_model(monkeypatch, image_multimodal_enabled=True)
    session_key = "web:shared"
    runtime_session = SimpleNamespace(session_key=session_key, messages=[])
    loop = SimpleNamespace(
        sessions=SimpleNamespace(get_or_create=lambda key: runtime_session),
        main_task_service=None,
        tools={},
        max_iterations=8,
        workspace=None,
        temp_dir="",
        app_config=SimpleNamespace(
            get_managed_model=lambda key: SimpleNamespace(image_multimodal_enabled=(key == "ceo_primary"))
        ),
    )
    runner = ceo_runner.CeoFrontDoorRunner(loop=loop)

    monkeypatch.setattr(ceo_runtime_ops, "current_project_environment", lambda workspace_root=None: {})
    monkeypatch.setattr(prompt_cache_contract, "build_session_prompt_cache_key", lambda **kwargs: "cache-key")

    image_path = tmp_path / "demo.png"
    image_path.write_bytes(b"\x89PNG\r\n\x1a\nsmall")
    merged_text = _web_ceo_uploaded_image_note(text="Please inspect this image", image_path=image_path)
    multimodal_text = _web_ceo_multimodal_image_note(text="Please inspect this image")

    async def _resolve_for_actor(*, actor_role: str, session_id: str):
        _ = actor_role, session_id
        return {"skills": [], "tool_families": [], "tool_names": ["filesystem_stat"]}

    async def _build_for_ceo(**kwargs):
        _ = kwargs
        return SimpleNamespace(
            tool_names=["filesystem_stat"],
            model_messages=[
                {"role": "system", "content": "SYSTEM"},
                {"role": "user", "content": merged_text},
            ],
            stable_messages=[
                {"role": "system", "content": "SYSTEM"},
                {"role": "user", "content": merged_text},
            ],
            dynamic_appendix_messages=[],
            candidate_tool_names=[],
            candidate_tool_items=[],
            trace={
                "selected_skills": [],
                "tool_selection": {},
                "capability_snapshot": {
                    "visible_tool_ids": ["filesystem_stat"],
                    "visible_skill_ids": [],
                },
            },
            cache_family_revision="frontdoor:v1",
            turn_overlay_text="",
        )

    monkeypatch.setattr(runner._resolver, "resolve_for_actor", _resolve_for_actor)
    monkeypatch.setattr(runner._builder, "build_for_ceo", _build_for_ceo)
    monkeypatch.setattr(runner, "_resolve_ceo_model_refs", lambda: ["ceo_primary"])
    monkeypatch.setattr(
        runner,
        "_resolve_frontdoor_send_model_context_window",
        lambda **_: {
            "model_key": "ceo_primary",
            "provider_id": "responses",
            "provider_model": "responses:gpt-test",
            "resolved_model": "gpt-test",
            "context_window_tokens": 128000,
        },
        raising=False,
    )

    session = SimpleNamespace(
        state=SimpleNamespace(session_key=session_key),
        _memory_channel="web",
        _memory_chat_id="shared",
        _channel="web",
        _chat_id="shared",
        _active_cancel_token=None,
        inflight_turn_snapshot=lambda: None,
        _frontdoor_request_body_messages=[],
        _frontdoor_history_shrink_reason="",
        _compression_state={"status": "", "text": "", "source": "", "needs_recheck": False},
        _semantic_context_state={"summary_text": "", "needs_refresh": False},
        _frontdoor_selection_debug={},
    )
    runtime = SimpleNamespace(
        context=SimpleNamespace(loop=loop, session=session, session_key=session_key, on_progress=None)
    )

    prepared = await runner._graph_prepare_turn(
        initial_persistent_state(
            user_input={
                "content": merged_text,
                "metadata": _web_ceo_upload_metadata(image_path),
            }
        ),
        runtime=runtime,
    )
    preflight = runner._frontdoor_send_preflight_snapshot(
        state=prepared,
        runtime=runtime,
        tool_schemas=[],
    )

    assert prepared["frontdoor_request_body_messages"][-1]["content"] == multimodal_text
    assert isinstance(prepared["frontdoor_live_request_messages"][-1]["content"], list)
    live_blocks = [
        block
        for block in list(prepared["frontdoor_live_request_messages"][-1]["content"] or [])
        if isinstance(block, dict)
    ]
    assert live_blocks[0] == {"type": "text", "text": multimodal_text}
    assert any(block.get("type") == "image_url" for block in live_blocks)
    assert "local path" not in json.dumps(prepared["frontdoor_live_request_messages"], ensure_ascii=False)
    assert "inspect the local file paths" not in json.dumps(
        prepared["frontdoor_live_request_messages"], ensure_ascii=False
    )
    assert "input_image" in json.dumps(preflight["provider_request_body"], ensure_ascii=False)
    assert "local path" not in json.dumps(preflight["provider_request_body"], ensure_ascii=False)
    assert prepared["query_text"] == "Please inspect this image"


@pytest.mark.asyncio
async def test_prepare_turn_keeps_uploaded_image_as_text_only_when_binding_disabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_live_runtime_model(monkeypatch, image_multimodal_enabled=False)
    session_key = "web:shared"
    runtime_session = SimpleNamespace(session_key=session_key, messages=[])
    loop = SimpleNamespace(
        sessions=SimpleNamespace(get_or_create=lambda key: runtime_session),
        main_task_service=None,
        tools={},
        max_iterations=8,
        workspace=None,
        temp_dir="",
        app_config=SimpleNamespace(get_managed_model=lambda key: SimpleNamespace(image_multimodal_enabled=False)),
    )
    runner = ceo_runner.CeoFrontDoorRunner(loop=loop)

    monkeypatch.setattr(ceo_runtime_ops, "current_project_environment", lambda workspace_root=None: {})
    monkeypatch.setattr(prompt_cache_contract, "build_session_prompt_cache_key", lambda **kwargs: "cache-key")

    image_path = tmp_path / "demo.png"
    image_path.write_bytes(b"\x89PNG\r\n\x1a\nsmall")
    merged_text = _web_ceo_uploaded_image_note(text="Please inspect this image", image_path=image_path)

    async def _resolve_for_actor(*, actor_role: str, session_id: str):
        _ = actor_role, session_id
        return {"skills": [], "tool_families": [], "tool_names": ["filesystem_stat"]}

    async def _build_for_ceo(**kwargs):
        _ = kwargs
        return SimpleNamespace(
            tool_names=["filesystem_stat"],
            model_messages=[
                {"role": "system", "content": "SYSTEM"},
                {"role": "user", "content": merged_text},
            ],
            stable_messages=[
                {"role": "system", "content": "SYSTEM"},
                {"role": "user", "content": merged_text},
            ],
            dynamic_appendix_messages=[],
            candidate_tool_names=[],
            candidate_tool_items=[],
            trace={
                "selected_skills": [],
                "tool_selection": {},
                "capability_snapshot": {
                    "visible_tool_ids": ["filesystem_stat"],
                    "visible_skill_ids": [],
                },
            },
            cache_family_revision="frontdoor:v1",
            turn_overlay_text="",
        )

    monkeypatch.setattr(runner._resolver, "resolve_for_actor", _resolve_for_actor)
    monkeypatch.setattr(runner._builder, "build_for_ceo", _build_for_ceo)
    monkeypatch.setattr(runner, "_resolve_ceo_model_refs", lambda: ["ceo_primary"])
    monkeypatch.setattr(
        runner,
        "_resolve_frontdoor_send_model_context_window",
        lambda **_: {
            "model_key": "ceo_primary",
            "provider_id": "responses",
            "provider_model": "responses:gpt-test",
            "resolved_model": "gpt-test",
            "context_window_tokens": 128000,
        },
        raising=False,
    )

    session = SimpleNamespace(
        state=SimpleNamespace(session_key=session_key),
        _memory_channel="web",
        _memory_chat_id="shared",
        _channel="web",
        _chat_id="shared",
        _active_cancel_token=None,
        inflight_turn_snapshot=lambda: None,
        _frontdoor_request_body_messages=[],
        _frontdoor_history_shrink_reason="",
        _compression_state={"status": "", "text": "", "source": "", "needs_recheck": False},
        _semantic_context_state={"summary_text": "", "needs_refresh": False},
        _frontdoor_selection_debug={},
    )
    runtime = SimpleNamespace(
        context=SimpleNamespace(loop=loop, session=session, session_key=session_key, on_progress=None)
    )

    prepared = await runner._graph_prepare_turn(
        initial_persistent_state(
            user_input={
                "content": merged_text,
                "metadata": _web_ceo_upload_metadata(image_path),
            }
        ),
        runtime=runtime,
    )
    preflight = runner._frontdoor_send_preflight_snapshot(
        state=prepared,
        runtime=runtime,
        tool_schemas=[],
    )

    assert prepared["frontdoor_request_body_messages"][-1]["content"] == merged_text
    assert prepared["frontdoor_live_request_messages"][-1]["content"] == merged_text
    assert "input_image" not in json.dumps(preflight["provider_request_body"], ensure_ascii=False)


def test_prompt_contract_keeps_attachment_reopen_targets_in_request_messages(
    tmp_path: Path,
) -> None:
    docx_path = tmp_path / "resume.docx"
    docx_path.write_bytes(b"PK\x03\x04docx")
    image_path = tmp_path / "jd.png"
    image_path.write_bytes(b"\x89PNG\r\n\x1a\nsmall")

    state = initial_persistent_state(
        user_input={
            "content": "参考JD优化简历",
            "metadata": _web_ceo_mixed_upload_metadata(
                text="参考JD优化简历",
                docx_path=docx_path,
                image_path=image_path,
            ),
        }
    )
    state.update(
        {
            "messages": [
                {"role": "system", "content": "SYSTEM"},
                {"role": "user", "content": "参考JD优化简历"},
            ],
            "stable_messages": [
                {"role": "system", "content": "SYSTEM"},
                {"role": "user", "content": "参考JD优化简历"},
            ],
            "dynamic_appendix_messages": [],
            "frontdoor_live_request_messages": [
                {"role": "system", "content": "SYSTEM"},
                {"role": "user", "content": "参考JD优化简历"},
            ],
            "frontdoor_request_body_messages": [
                {"role": "system", "content": "SYSTEM"},
                {"role": "user", "content": "参考JD优化简历"},
            ],
            "tool_names": ["filesystem_stat"],
            "provider_tool_names": [],
            "pending_provider_tool_names": [],
            "provider_tool_exposure_pending": False,
            "provider_tool_exposure_revision": "",
            "provider_tool_exposure_commit_reason": "",
            "candidate_tool_names": [],
            "candidate_tool_items": [],
            "visible_skill_ids": [],
            "candidate_skill_ids": [],
            "rbac_visible_tool_names": ["filesystem_stat"],
            "rbac_visible_skill_ids": [],
            "cache_family_revision": "frontdoor:v1",
            "attachment_reopen_targets": [
                {
                    "name": docx_path.name,
                    "kind": "file",
                    "mime_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    "path": str(docx_path),
                },
                {
                    "name": image_path.name,
                    "kind": "image",
                    "mime_type": "image/png",
                    "path": str(image_path),
                },
            ],
        }
    )

    round_tripped = dict(state)

    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace(main_task_service=None))
    contract = runner._frontdoor_prompt_contract(
        state=round_tripped,
        provider_model="ceo_primary",
        tool_schemas=[],
        overlay_text="",
    )
    request_messages = list(contract.request_messages or [])
    assert request_messages
    rendered = "\n\n".join(
        str(item.get("content", "") or "")
        for item in request_messages
        if isinstance(item, dict)
    )
    assert "attachment_reopen_targets:" not in rendered
    # 尾块退役：附件句柄由发送前的单生产者投进一次性通知。
    runner._ceo_session_temp_dir = lambda _session_key: ""
    notified = runner._apply_frontdoor_skill_roster_notice(
        request_messages,
        session_key="web:shared",
        state=round_tripped,
    )
    notice_texts = [
        str(item.get("content") or "")
        for item in notified
        if str(item.get("content") or "").startswith("[RUNTIME_NOTICE]")
    ]
    assert any(
        "attachment_targets" in text
        and str(docx_path) in text
        and str(image_path) in text
        for text in notice_texts
    )


@pytest.mark.asyncio
async def test_follow_up_uploaded_image_stays_multimodal_without_local_path_when_binding_enabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_live_runtime_model(monkeypatch, image_multimodal_enabled=True)
    session_key = "web:shared"
    runtime_session = SimpleNamespace(session_key=session_key, messages=[])
    loop = SimpleNamespace(
        sessions=SimpleNamespace(get_or_create=lambda key: runtime_session),
        main_task_service=None,
        tools={},
        max_iterations=8,
        workspace=None,
        temp_dir="",
        app_config=SimpleNamespace(
            get_managed_model=lambda key: SimpleNamespace(image_multimodal_enabled=(key == "ceo_primary"))
        ),
    )
    runner = ceo_runner.CeoFrontDoorRunner(loop=loop)

    image_path = tmp_path / "follow-up.png"
    image_path.write_bytes(b"\x89PNG\r\n\x1a\nsmall")
    merged_text = _web_ceo_uploaded_image_note(text="Please inspect this image", image_path=image_path)
    multimodal_text = _web_ceo_multimodal_image_note(text="Please inspect this image")

    queued_input = UserInputMessage(
        content=merged_text,
        metadata=_web_ceo_upload_metadata(image_path),
    )
    session = SimpleNamespace(
        state=SimpleNamespace(session_key=session_key),
        take_follow_up_batch_for_call_model=lambda: [queued_input],
    )
    runtime = SimpleNamespace(
        context=SimpleNamespace(loop=loop, session=session, session_key=session_key, on_progress=None)
    )
    monkeypatch.setattr(runner, "_sync_runtime_session_frontdoor_state", lambda **_: None)

    update = await runner._consume_session_follow_up_messages_before_call_model(
        state={
            "messages": [
                {"role": "system", "content": "SYSTEM"},
                {"role": "assistant", "content": "Still working"},
            ],
            "frontdoor_request_body_messages": [
                {"role": "system", "content": "SYSTEM"},
                {"role": "assistant", "content": "Still working"},
            ],
            "model_refs": ["ceo_primary"],
            "query_text": "Original request",
        },
        runtime=runtime,
    )

    assert update["frontdoor_request_body_messages"][-1]["content"] == multimodal_text
    live_blocks = [
        block
        for block in list(update["frontdoor_live_request_messages"][-1]["content"] or [])
        if isinstance(block, dict)
    ]
    assert live_blocks[0] == {"type": "text", "text": multimodal_text}
    assert any(block.get("type") == "image_url" for block in live_blocks)
    assert "local path" not in json.dumps(update["frontdoor_live_request_messages"], ensure_ascii=False)
    assert "inspect the local file paths" not in json.dumps(update, ensure_ascii=False)
    assert update["query_text"] == "Original request\n\nPlease inspect this image"


def test_frontdoor_send_preflight_snapshot_adds_content_open_image_overlay_only_to_live_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_live_runtime_model(monkeypatch, image_multimodal_enabled=True)
    loop = SimpleNamespace(
        app_config=SimpleNamespace(
            get_managed_model=lambda key: SimpleNamespace(image_multimodal_enabled=(key == "ceo_primary"))
        ),
    )
    runner = ceo_runner.CeoFrontDoorRunner(loop=loop)

    monkeypatch.setattr(
        runner,
        "_frontdoor_prompt_contract",
        lambda **kwargs: SimpleNamespace(
            request_messages=list(kwargs["state"].get("messages") or []),
            prompt_cache_key="cache-key",
            diagnostics={},
        ),
    )
    monkeypatch.setattr(
        runner,
        "_resolve_frontdoor_send_model_context_window",
        lambda **_: {
            "model_key": "ceo_primary",
            "provider_id": "responses",
            "provider_model": "responses:gpt-test",
            "resolved_model": "gpt-test",
            "context_window_tokens": 128000,
        },
        raising=False,
    )

    image_path = tmp_path / "opened.png"
    image_path.write_bytes(b"\x89PNG\r\n\x1a\nsmall")
    state = {
        "messages": [
            {"role": "system", "content": "SYSTEM"},
            {"role": "user", "content": "Please inspect the reopened image"},
        ],
        "model_refs": ["ceo_primary"],
        "pending_content_open_image_payloads": [
            {
                "ok": True,
                "operation": "open",
                "content_kind": "image",
                "mime_type": "image/png",
                "summary": "图片已通过 content_open 打开，视觉内容将在下一轮请求中附带。",
                "multimodal_open_pending": True,
                "runtime_image_target": {"path": str(image_path), "mime_type": "image/png", "source_ref": ""},
            }
        ],
    }

    snapshot = runner._frontdoor_send_preflight_snapshot(
        state=state,
        runtime=SimpleNamespace(context=SimpleNamespace(session=None, session_key="web:shared")),
        tool_schemas=[],
    )

    live_blocks = [
        block
        for block in list(snapshot["request_messages"][-1]["content"] or [])
        if isinstance(block, dict)
    ]
    # 末位那条 user 记录就是当轮契约/阶段位置：图片载体必须**折进**它的原文字，
    # 直接替换会让这一跳收不到 callable_tools（节点道一直是折叠口径，两条道同形）。
    folded_text = str(live_blocks[0].get("text") or "")
    assert "Please inspect the reopened image" in folded_text
    assert "图片已通过 content_open 打开，视觉内容已附带在本轮上下文中" in folded_text
    assert any(block.get("type") == "image_url" for block in live_blocks)
    assert snapshot["durable_request_messages"][-1]["content"] == folded_text




def test_frontdoor_send_preflight_snapshot_rejects_content_open_image_overlay_without_multimodal_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_live_runtime_model(monkeypatch, image_multimodal_enabled=False)
    loop = SimpleNamespace(
        app_config=SimpleNamespace(
            get_managed_model=lambda key: SimpleNamespace(image_multimodal_enabled=False)
        ),
    )
    runner = ceo_runner.CeoFrontDoorRunner(loop=loop)

    monkeypatch.setattr(
        runner,
        "_frontdoor_prompt_contract",
        lambda **kwargs: SimpleNamespace(
            request_messages=list(kwargs["state"].get("messages") or []),
            prompt_cache_key="cache-key",
            diagnostics={},
        ),
    )

    image_path = tmp_path / "opened.png"
    image_path.write_bytes(b"\x89PNG\r\n\x1a\nsmall")
    state = {
        "messages": [
            {"role": "system", "content": "SYSTEM"},
            {"role": "user", "content": "Please inspect the reopened image"},
        ],
        "model_refs": ["ceo_primary"],
        "pending_content_open_image_payloads": [
            {
                "ok": True,
                "operation": "open",
                "content_kind": "image",
                "mime_type": "image/png",
                "summary": "图片已通过 content_open 打开，视觉内容将在下一轮请求中附带。",
                "multimodal_open_pending": True,
                "runtime_image_target": {"path": str(image_path), "mime_type": "image/png", "source_ref": ""},
            }
        ],
    }

    with pytest.raises(ceo_runtime_ops.FrontdoorCompressionRuntimeError, match="没有勾选多模态能力因此无法打开图片"):
        runner._frontdoor_send_preflight_snapshot(
            state=state,
            runtime=SimpleNamespace(context=SimpleNamespace(session=None, session_key="web:shared")),
            tool_schemas=[],
        )


def test_ceo_image_multimodal_enabled_for_model_refs_prefers_live_runtime_config_over_loop_app_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loop = SimpleNamespace(
        app_config=SimpleNamespace(
            get_managed_model=lambda key: SimpleNamespace(image_multimodal_enabled=False)
        ),
    )
    runner = ceo_runner.CeoFrontDoorRunner(loop=loop)
    live_config = SimpleNamespace(
        get_managed_model=lambda key: SimpleNamespace(image_multimodal_enabled=(key == "ceo_primary"))
    )

    monkeypatch.setattr(
        ceo_runtime_ops,
        "get_runtime_config",
        lambda force=False: (live_config, 1, False),
    )

    assert runner._ceo_image_multimodal_enabled_for_model_refs(["ceo_primary"]) is True


def test_frontdoor_build_tool_runtime_context_includes_image_multimodal_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_session = SimpleNamespace(session_key="web:shared")
    session = SimpleNamespace(
        state=SimpleNamespace(session_key="web:shared"),
        _channel="web",
        _chat_id="shared",
        _memory_channel="web",
        _memory_chat_id="shared",
        inflight_turn_snapshot=lambda: {},
        _current_turn_id=lambda: "turn-1",
        _active_cancel_token=None,
    )
    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(
        loop=SimpleNamespace(
            sessions=SimpleNamespace(get_or_create=lambda key: runtime_session),
            workspace=None,
            temp_dir="",
        )
    )

    monkeypatch.setattr(ceo_runtime_ops, "current_project_environment", lambda workspace_root=None: {})
    monkeypatch.setattr(runner, "_session_task_defaults", lambda runtime_session: {})
    monkeypatch.setattr(
        runner,
        "_ceo_image_multimodal_for_request",
        lambda refs: (list(refs or []) == ["ceo_primary"], "ceo_primary"),
    )

    context = runner._build_tool_runtime_context(
        state={
            "user_input": {"metadata": {}},
            "candidate_tool_names": [],
            "candidate_skill_ids": [],
            "rbac_visible_tool_names": [],
            "rbac_visible_skill_ids": [],
            "model_refs": ["ceo_primary"],
        },
        runtime=SimpleNamespace(
            context=SimpleNamespace(
                session=session,
                session_key="web:shared",
                on_progress=None,
            )
        ),
    )

    assert context["model_refs"] == ["ceo_primary"]
    assert context["image_multimodal_enabled"] is True


@pytest.mark.asyncio
async def test_prepare_turn_rejects_oversized_uploaded_image_when_binding_enabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_live_runtime_model(monkeypatch, image_multimodal_enabled=True)
    session_key = "web:shared"
    runtime_session = SimpleNamespace(session_key=session_key, messages=[])
    loop = SimpleNamespace(
        sessions=SimpleNamespace(get_or_create=lambda key: runtime_session),
        main_task_service=None,
        tools={},
        max_iterations=8,
        workspace=None,
        temp_dir="",
        app_config=SimpleNamespace(
            get_managed_model=lambda key: SimpleNamespace(image_multimodal_enabled=(key == "ceo_primary"))
        ),
    )
    runner = ceo_runner.CeoFrontDoorRunner(loop=loop)

    monkeypatch.setattr(ceo_runtime_ops, "current_project_environment", lambda workspace_root=None: {})
    monkeypatch.setattr(prompt_cache_contract, "build_session_prompt_cache_key", lambda **kwargs: "cache-key")

    image_path = tmp_path / "huge.png"
    image_path.write_bytes(b"0" * (web_ceo_sessions.WEB_CEO_IMAGE_UPLOAD_MAX_BYTES + 1))
    merged_text = _web_ceo_uploaded_image_note(text="Please inspect this image", image_path=image_path)

    async def _resolve_for_actor(*, actor_role: str, session_id: str):
        _ = actor_role, session_id
        return {"skills": [], "tool_families": [], "tool_names": ["filesystem_stat"]}

    async def _build_for_ceo(**kwargs):
        _ = kwargs
        return SimpleNamespace(
            tool_names=["filesystem_stat"],
            model_messages=[
                {"role": "system", "content": "SYSTEM"},
                {"role": "user", "content": merged_text},
            ],
            stable_messages=[
                {"role": "system", "content": "SYSTEM"},
                {"role": "user", "content": merged_text},
            ],
            dynamic_appendix_messages=[],
            candidate_tool_names=[],
            candidate_tool_items=[],
            trace={
                "selected_skills": [],
                "tool_selection": {},
                "capability_snapshot": {
                    "visible_tool_ids": ["filesystem_stat"],
                    "visible_skill_ids": [],
                },
            },
            cache_family_revision="frontdoor:v1",
            turn_overlay_text="",
        )

    monkeypatch.setattr(runner._resolver, "resolve_for_actor", _resolve_for_actor)
    monkeypatch.setattr(runner._builder, "build_for_ceo", _build_for_ceo)
    monkeypatch.setattr(runner, "_resolve_ceo_model_refs", lambda: ["ceo_primary"])

    session = SimpleNamespace(
        state=SimpleNamespace(session_key=session_key),
        _memory_channel="web",
        _memory_chat_id="shared",
        _channel="web",
        _chat_id="shared",
        _active_cancel_token=None,
        inflight_turn_snapshot=lambda: None,
        _frontdoor_request_body_messages=[],
        _frontdoor_history_shrink_reason="",
        _compression_state={"status": "", "text": "", "source": "", "needs_recheck": False},
        _semantic_context_state={"summary_text": "", "needs_refresh": False},
        _frontdoor_selection_debug={},
    )
    runtime = SimpleNamespace(
        context=SimpleNamespace(loop=loop, session=session, session_key=session_key, on_progress=None)
    )

    with pytest.raises(ceo_runtime_ops.FrontdoorCompressionRuntimeError, match="5 MiB"):
        await runner._graph_prepare_turn(
            initial_persistent_state(
                user_input={
                    "content": merged_text,
                    "metadata": _web_ceo_upload_metadata(image_path),
                }
            ),
            runtime=runtime,
        )


def test_persist_frontdoor_actual_request_keeps_multimodal_artifact_but_strips_durable_baseline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = ceo_runner.CeoFrontDoorRunner(loop=SimpleNamespace())
    captured: dict[str, object] = {}

    def _fake_persist(session_id: str, *, payload: dict[str, object]):
        captured["session_id"] = session_id
        captured["payload"] = payload
        return {
            "path": "D:/tmp/frontdoor-request.json",
            "actual_request_hash": "request-hash",
            "actual_request_message_count": 2,
            "actual_tool_schema_hash": "tool-hash",
        }

    monkeypatch.setattr(ceo_runtime_ops, "persist_frontdoor_actual_request", _fake_persist)

    session = SimpleNamespace(
        state=SimpleNamespace(session_key="web:shared"),
        _frontdoor_actual_request_history=[],
    )
    runtime = SimpleNamespace(context=SimpleNamespace(session=session, session_key="web:shared"))
    request_messages = [
        {"role": "system", "content": "SYSTEM"},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "Please inspect this image"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
            ],
        },
    ]

    result = runner._persist_frontdoor_actual_request(
        state={"session_key": "web:shared", "frontdoor_actual_request_history": []},
        runtime=runtime,
        request_messages=request_messages,
        tool_schemas=[],
        prompt_cache_key="cache-key",
        prompt_cache_diagnostics={},
        parallel_tool_calls=None,
    )

    payload = dict(captured["payload"] or {})
    assert payload["request_messages"][1]["content"][1]["type"] == "image_url"
    assert result["frontdoor_request_body_messages"][1]["content"] == "Please inspect this image"
    assert session._frontdoor_request_body_messages[1]["content"] == "Please inspect this image"


@pytest.mark.asyncio
async def test_graph_call_model_fresh_turn_reuses_previous_message_artifact_prefix_despite_trailing_whitespace_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_key = "web:shared"
    runtime_session = SimpleNamespace(session_key=session_key, messages=[])
    loop = SimpleNamespace(
        sessions=SimpleNamespace(get_or_create=lambda key: runtime_session),
        main_task_service=None,
        tools={},
        max_iterations=8,
        workspace=None,
        temp_dir="",
    )
    runner = ceo_runner.CeoFrontDoorRunner(loop=loop)
    captured_model_messages: list[dict[str, object]] = []

    monkeypatch.setattr(ceo_runtime_ops, "current_project_environment", lambda workspace_root=None: {})

    previous_request_path = tmp_path / "frontdoor-request-previous.json"
    previous_request_messages = [
        {"role": "system", "content": "SYSTEM"},
        {"role": "user", "content": "old question"},
        {
            "role": "user",
            "content": '{"message_type":"frontdoor_runtime_tool_contract","callable_tool_names":["exec","filesystem_stat"]}',
        },
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call-exec-1",
                    "type": "function",
                    "function": {"name": "exec", "arguments": "{}"},
                }
            ],
        },
        {
            "role": "tool",
            "name": "exec",
            "tool_call_id": "call-exec-1",
            "content": "Command finished for final:ceo-demo ",
        },
    ]
    previous_request_path.write_text(
        json.dumps(
            {
                "request_messages": previous_request_messages,
                "tool_schemas": [
                    {
                        "type": "function",
                        "function": {"name": "exec", "description": "", "parameters": {"type": "object"}},
                    },
                    {
                        "type": "function",
                        "function": {"name": "filesystem_stat", "description": "", "parameters": {"type": "object"}},
                    },
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    async def _resolve_for_actor(*, actor_role: str, session_id: str):
        _ = actor_role, session_id
        return {"skills": [], "tool_families": [], "tool_names": ["exec", "filesystem_stat"]}

    async def _build_for_ceo(**kwargs):
        stable_messages = list(kwargs.get("request_body_seed_messages") or [])
        user_content = str(kwargs.get("user_content") or "").strip()
        model_messages = [*stable_messages, {"role": "user", "content": user_content}]
        return SimpleNamespace(
            tool_names=["exec", "filesystem_stat"],
            model_messages=model_messages,
            stable_messages=list(stable_messages),
            dynamic_appendix_messages=[],
            candidate_tool_names=[],
            candidate_tool_items=[],
            trace={
                "selected_skills": [],
                "tool_selection": {},
                "capability_snapshot": {
                    "visible_tool_ids": ["exec", "filesystem_stat"],
                    "visible_skill_ids": [],
                },
            },
            cache_family_revision="frontdoor:v1",
            turn_overlay_text="",
        )

    async def _call_model_with_tools(**kwargs):
        captured_model_messages[:] = list(kwargs.get("messages") or [])
        return {"content": "ok"}

    monkeypatch.setattr(runner._resolver, "resolve_for_actor", _resolve_for_actor)
    monkeypatch.setattr(runner._builder, "build_for_ceo", _build_for_ceo)
    monkeypatch.setattr(runner, "_resolve_ceo_model_refs", lambda: ["openai_codex:gpt-test"])
    monkeypatch.setattr(
        runner,
        "_resolve_frontdoor_send_model_context_window",
        lambda **_: {
            "model_key": "openai_codex:gpt-test",
            "provider_model": "openai_codex:gpt-test",
            "context_window_tokens": 128000,
        },
        raising=False,
    )
    monkeypatch.setattr(
        runner,
        "_frontdoor_tool_schemas_for_state",
        lambda *, state, runtime: [
            {
                "type": "function",
                "function": {"name": str(name), "description": "", "parameters": {"type": "object"}},
            }
            for name in list(state.get("provider_tool_names") or state.get("tool_names") or [])
        ],
    )
    monkeypatch.setattr(runner, "_call_model_with_tools", _call_model_with_tools)
    monkeypatch.setattr(runner, "_checkpoint_safe_model_response_payload", lambda _message: {"ok": True})
    monkeypatch.setattr(runner, "_persist_frontdoor_actual_request", lambda **_: {})

    session = SimpleNamespace(
        state=SimpleNamespace(session_key=session_key),
        _memory_channel="web",
        _memory_chat_id="shared",
        _channel="web",
        _chat_id="shared",
        _active_cancel_token=None,
        inflight_turn_snapshot=lambda: None,
        _frontdoor_request_body_messages=[
            {"role": "system", "content": "SYSTEM"},
            {"role": "user", "content": "old question"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call-exec-1",
                        "type": "function",
                        "function": {"name": "exec", "arguments": "{}"},
                    }
                ],
            },
            {
                "role": "tool",
                "name": "exec",
                "tool_call_id": "call-exec-1",
                "content": "Command finished for final:ceo-demo",
            },
            {"role": "assistant", "content": "final answer"},
        ],
        _frontdoor_history_shrink_reason="",
        _compression_state={"status": "", "text": "", "source": "", "needs_recheck": False},
        _semantic_context_state={"summary_text": "", "needs_refresh": False},
        _frontdoor_selection_debug={},
        _frontdoor_previous_actual_request_path=str(previous_request_path),
        _frontdoor_previous_actual_request_history=[{"path": str(previous_request_path), "turn_id": "turn-old"}],
        _frontdoor_actual_request_path="",
        _frontdoor_actual_request_history=[],
    )
    runtime = SimpleNamespace(
        context=SimpleNamespace(loop=loop, session=session, session_key=session_key, on_progress=None)
    )

    prepared = await runner._graph_prepare_turn(
        initial_persistent_state(user_input={"content": "next question", "metadata": {}}),
        runtime=runtime,
    )
    await runner._graph_call_model(
        prepared,
        runtime=runtime,
    )

    # The stale contract inside the previous actual request is never reused as a
    # scaffold prefix (the whitespace-normalized tool result is still preserved).
    expected_scaffold_prefix = [
        item
        for item in previous_request_messages
        if not (
            isinstance(item, dict)
            and str(item.get("role") or "").strip().lower() == "user"
            and "message_type" in str(item.get("content") or "")
        )
    ]
    assert captured_model_messages[0]["role"] == "system"
    assert "## Runtime Contract (pinned)" in str(captured_model_messages[0]["content"])
    expected_tail = expected_scaffold_prefix[1:]
    assert _normalized_tool_content(captured_model_messages[1 : 1 + len(expected_tail)]) == _normalized_tool_content(expected_tail)
    assert {"role": "assistant", "content": "final answer"} in captured_model_messages
    final_answer_index = captured_model_messages.index({"role": "assistant", "content": "final answer"})
    next_user_index = captured_model_messages.index({"role": "user", "content": "next question"})
    assert final_answer_index < next_user_index






@pytest.mark.asyncio
async def test_prepare_turn_keeps_provider_schema_hash_stable_when_web_fetch_is_promoted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_key = "web:frontdoor-cache"
    runtime_session = SimpleNamespace(session_key=session_key, messages=[])
    tools = {
        "exec": _NamedSchemaTool(
            name="exec",
            description="Run a command in the current workspace.",
            parameters={
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Command to execute."},
                },
                "required": ["command"],
            },
        ),
        "filesystem_stat": _NamedSchemaTool(
            name="filesystem_stat",
            description="List files and measure paths in the workspace.",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Absolute path to stat."},
                },
                "required": ["path"],
            },
        ),
        "web_fetch": _NamedSchemaTool(
            name="web_fetch",
            description="Lightweight HTTP fetch for reading public web pages.",
            parameters={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "Public http(s) URL to fetch."},
                    "timeout_ms": {
                        "type": "integer",
                        "description": "Network timeout in milliseconds.",
                    },
                },
                "required": ["url"],
            },
        ),
    }
    loop = SimpleNamespace(
        sessions=SimpleNamespace(get_or_create=lambda key: runtime_session),
        main_task_service=None,
        tools=tools,
        max_iterations=8,
        workspace=None,
        temp_dir="",
    )
    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(loop=loop)

    monkeypatch.setattr(ceo_runtime_ops, "current_project_environment", lambda workspace_root=None: {})
    monkeypatch.setattr(prompt_cache_contract, "build_session_prompt_cache_key", lambda **kwargs: "cache-key")
    monkeypatch.setattr(runner, "_resolve_ceo_model_refs", lambda: ["openai:gpt-test"])

    async def _resolve_for_actor(*, actor_role: str, session_id: str):
        _ = actor_role, session_id
        return {
            "skills": [],
            "tool_families": [],
            "tool_names": ["exec", "filesystem_stat", "web_fetch"],
        }

    captured_builds: list[dict[str, object]] = []
    assembly_tool_name_sets = [
        ["exec", "filesystem_stat"],
        ["exec", "filesystem_stat", "web_fetch"],
    ]
    assembly_candidate_sets = [
        ["web_fetch"],
        [],
    ]

    async def _build_for_ceo(**kwargs):
        build_index = len(captured_builds)
        captured_builds.append(dict(kwargs))
        user_content = str(kwargs.get("user_content") or "")
        return SimpleNamespace(
            model_messages=[
                {"role": "system", "content": "SYSTEM"},
                {"role": "user", "content": user_content},
            ],
            stable_messages=[
                {"role": "system", "content": "SYSTEM"},
                {"role": "user", "content": "bootstrap"},
            ],
            dynamic_appendix_messages=[],
            tool_names=list(assembly_tool_name_sets[build_index]),
            candidate_tool_names=list(assembly_candidate_sets[build_index]),
            candidate_tool_items=[
                {"tool_id": tool_name, "description": f"{tool_name} description"}
                for tool_name in assembly_candidate_sets[build_index]
            ],
            trace={
                "selected_skills": [],
                "tool_selection": {},
                "capability_snapshot": {
                    "visible_tool_ids": ["exec", "filesystem_stat", "web_fetch"],
                    "visible_skill_ids": [],
                },
            },
            cache_family_revision="frontdoor:v1",
            turn_overlay_text="",
        )

    runner._resolver = SimpleNamespace(resolve_for_actor=_resolve_for_actor)
    runner._builder = SimpleNamespace(build_for_ceo=_build_for_ceo)

    def _assembly_session() -> SimpleNamespace:
        return SimpleNamespace(
            state=SimpleNamespace(session_key=session_key),
            _memory_channel="web",
            _memory_chat_id="shared",
            _channel="web",
            _chat_id="shared",
            _active_cancel_token=None,
            inflight_turn_snapshot=lambda: None,
            _frontdoor_request_body_messages=[],
            _frontdoor_history_shrink_reason="",
            _compression_state={"status": "", "text": "", "source": "", "needs_recheck": False},
            _semantic_context_state={"summary_text": "", "needs_refresh": False},
            _frontdoor_selection_debug={},
        )

    first_prepared = await runner._graph_prepare_turn(
        initial_persistent_state(user_input={"content": "collect sources", "metadata": {}}),
        runtime=SimpleNamespace(context=SimpleNamespace(session=_assembly_session())),
    )
    second_prepared = await runner._graph_prepare_turn(
        initial_persistent_state(user_input={"content": "collect sources", "metadata": {}}),
        runtime=SimpleNamespace(context=SimpleNamespace(session=_assembly_session())),
    )

    assert first_prepared["provider_tool_names"] == ["exec", "filesystem_stat", "web_fetch"]
    assert second_prepared["provider_tool_names"] == ["exec", "filesystem_stat", "web_fetch"]
    assert (
        first_prepared["prompt_cache_diagnostics"]["actual_tool_schema_hash"]
        == second_prepared["prompt_cache_diagnostics"]["actual_tool_schema_hash"]
    )






@pytest.mark.asyncio
async def test_create_agent_runner_preserves_unverified_dispatch_text_from_model() -> None:
    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(
        loop=SimpleNamespace(
            main_task_service=SimpleNamespace(get_task=lambda task_id: None),
            tools=SimpleNamespace(get=lambda *_: None),
        )
    )

    result = await runner._graph_normalize_model_output(
        {
            "response_payload": {
                "content": "Claude Code Haha 项目分析任务已在后台成功续跑。新任务 ID: `task:fake-123`。",
                "tool_calls": [],
                "finish_reason": "stop",
                "error_text": "",
                "reasoning_content": None,
                "thinking_blocks": None,
            },
            "used_tools": [],
            "route_kind": "direct_reply",
        },
        runtime=SimpleNamespace(context=SimpleNamespace()),
    )

    assert result["next_step"] == "finalize"
    assert result["final_output"] == "Claude Code Haha 项目分析任务已在后台成功续跑。新任务 ID: `task:fake-123`。"
    assert "summary_text" not in result
    assert "summary_payload" not in result
    assert "summary_model_key" not in result


@pytest.mark.asyncio
async def test_create_agent_runner_preserves_verified_dispatch_text_from_model() -> None:
    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(
        loop=SimpleNamespace(
            main_task_service=SimpleNamespace(get_task=lambda task_id: SimpleNamespace(task_id=task_id)),
            tools=SimpleNamespace(get=lambda *_: None),
        )
    )

    text = "后台修复任务已经建立，任务号 `task:demo-123`。我先继续排查，完成后直接把结果同步给你。"
    result = await runner._graph_normalize_model_output(
        {
            "response_payload": {
                "content": text,
                "tool_calls": [],
                "finish_reason": "stop",
                "error_text": "",
                "reasoning_content": None,
                "thinking_blocks": None,
            },
            "used_tools": ["create_async_task"],
            "route_kind": "task_dispatch",
            "verified_task_ids": ["task:demo-123"],
        },
        runtime=SimpleNamespace(context=SimpleNamespace()),
    )

    assert result["next_step"] == "finalize"
    assert result["final_output"] == text
    assert result["route_kind"] == "task_dispatch"




@pytest.mark.asyncio
async def test_create_agent_runner_allows_finalize_after_substantive_text_progress() -> None:
    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(
        loop=SimpleNamespace(
            main_task_service=SimpleNamespace(get_task=lambda task_id: None),
            tools=SimpleNamespace(get=lambda *_: None),
        )
    )

    result = await runner._graph_normalize_model_output(
        {
            "response_payload": {
                "content": "文件已经写好并核对完成。",
                "tool_calls": [],
                "finish_reason": "stop",
                "error_text": "",
                "reasoning_content": None,
                "thinking_blocks": None,
            },
            "used_tools": ["filesystem_write"],
            "route_kind": "self_execute",
        },
        runtime=SimpleNamespace(context=SimpleNamespace()),
    )

    assert result["next_step"] == "finalize"
    assert result["final_output"] == "文件已经写好并核对完成。"
    assert result["route_kind"] == "self_execute"


@pytest.mark.asyncio
async def test_create_agent_runner_keeps_heartbeat_finalize_even_with_empty_progress_text() -> None:
    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(
        loop=SimpleNamespace(
            main_task_service=SimpleNamespace(get_task=lambda task_id: None),
            tools=SimpleNamespace(get=lambda *_: None),
        )
    )

    result = await runner._graph_normalize_model_output(
        {
            "response_payload": {
                "content": "后台任务已完成。",
                "tool_calls": [],
                "finish_reason": "stop",
                "error_text": "",
                "reasoning_content": None,
                "thinking_blocks": None,
            },
            "used_tools": [],
            "route_kind": "direct_reply",
            "heartbeat_internal": True,
            "user_input": {
                "content": "[SESSION EVENTS]",
                "metadata": {
                    "heartbeat_internal": True,
                    "heartbeat_reason": "task_terminal",
                    "heartbeat_task_ids": ["task:demo-terminal"],
                },
            },
        },
        runtime=SimpleNamespace(context=SimpleNamespace()),
    )

    assert result["next_step"] == "finalize"
    assert result["final_output"] == "后台任务已完成。"


@pytest.mark.asyncio
async def test_create_agent_runner_preserves_dispatch_text_for_unverified_task_id_even_when_verified_task_exists() -> None:
    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(
        loop=SimpleNamespace(
            main_task_service=SimpleNamespace(get_task=lambda task_id: None),
            tools=SimpleNamespace(get=lambda *_: None),
        )
    )

    result = await runner._graph_normalize_model_output(
        {
            "response_payload": {
                "content": "后台修复任务已经建立，任务号 `task:fake-123`。我先继续排查。",
                "tool_calls": [],
                "finish_reason": "stop",
                "error_text": "",
                "reasoning_content": None,
                "thinking_blocks": None,
            },
            "used_tools": ["create_async_task"],
            "route_kind": "task_dispatch",
            "verified_task_ids": ["task:demo-123"],
        },
        runtime=SimpleNamespace(context=SimpleNamespace()),
    )

    assert result["next_step"] == "finalize"
    assert result["final_output"] == "后台修复任务已经建立，任务号 `task:fake-123`。我先继续排查。"


@pytest.mark.asyncio
async def test_create_agent_runner_preserves_heartbeat_success_reply_for_current_task_id() -> None:
    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(
        loop=SimpleNamespace(
            main_task_service=SimpleNamespace(get_task=lambda task_id: SimpleNamespace(task_id=task_id)),
            tools=SimpleNamespace(get=lambda *_: None),
        )
    )

    text = "已通过异步任务 `task:demo-terminal` 完成修复，结果：skill 已恢复可见。"
    result = await runner._graph_normalize_model_output(
        {
            "response_payload": {
                "content": text,
                "tool_calls": [],
                "finish_reason": "stop",
                "error_text": "",
                "reasoning_content": None,
                "thinking_blocks": None,
            },
            "used_tools": [],
            "route_kind": "direct_reply",
            "heartbeat_internal": True,
            "user_input": {
                "content": "[SESSION EVENTS]",
                "metadata": {
                    "heartbeat_internal": True,
                    "heartbeat_reason": "task_terminal",
                    "heartbeat_task_ids": ["task:demo-terminal"],
                },
            },
        },
        runtime=SimpleNamespace(context=SimpleNamespace()),
    )

    assert result["next_step"] == "finalize"
    assert result["final_output"] == text
    assert "未确认成功创建后台任务" not in result["final_output"]


@pytest.mark.asyncio
async def test_create_agent_runner_preserves_heartbeat_dispatch_claim_for_unknown_task_id() -> None:
    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(
        loop=SimpleNamespace(
            main_task_service=SimpleNamespace(get_task=lambda task_id: None),
            tools=SimpleNamespace(get=lambda *_: None),
        )
    )

    result = await runner._graph_normalize_model_output(
        {
            "response_payload": {
                "content": "任务 `demo-old` 已完成但未通过验收，已续跑为 `task:new-123`。",
                "tool_calls": [],
                "finish_reason": "stop",
                "error_text": "",
                "reasoning_content": None,
                "thinking_blocks": None,
            },
            "used_tools": [],
            "route_kind": "direct_reply",
            "heartbeat_internal": True,
            "user_input": {
                "content": "[SESSION EVENTS]",
                "metadata": {
                    "heartbeat_internal": True,
                    "heartbeat_reason": "task_terminal",
                    "heartbeat_task_ids": ["task:demo-old"],
                },
            },
        },
        runtime=SimpleNamespace(context=SimpleNamespace()),
    )

    assert result["next_step"] == "finalize"
    assert result["final_output"] == "任务 `demo-old` 已完成但未通过验收，已续跑为 `task:new-123`。"


@pytest.mark.asyncio
async def test_create_agent_postprocess_allows_unverified_task_dispatch_reply(monkeypatch) -> None:
    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(
        loop=SimpleNamespace(main_task_service=SimpleNamespace(get_task=lambda task_id: None))
    )

    result = await runner._postprocess_completed_tool_cycle(
        state={
            "tool_call_payloads": [
                {"id": "call-1", "name": "create_async_task", "arguments": {"task": "demo"}}
            ],
            "messages": [
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "type": "function",
                            "function": {"name": "create_async_task", "arguments": "{}"},
                        }
                    ],
                },
                {
                    "role": "tool",
                    "tool_call_id": "call-1",
                    "name": "create_async_task",
                    "content": "创建任务成功task:fake-123",
                },
            ],
            "used_tools": [],
            "route_kind": "direct_reply",
        }
    )

    assert result is not None
    assert "jump_to" not in result
    assert result["route_kind"] == "direct_reply"
    assert result["verified_task_ids"] == []
    assert "repair_overlay_text" not in result


@pytest.mark.asyncio
async def test_create_agent_postprocess_continues_after_verified_async_dispatch(monkeypatch) -> None:
    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(
        loop=SimpleNamespace(
            main_task_service=SimpleNamespace(get_task=lambda task_id: SimpleNamespace(task_id=task_id))
        )
    )

    result = await runner._postprocess_completed_tool_cycle(
        state={
            "tool_call_payloads": [
                {"id": "call-1", "name": "create_async_task", "arguments": {"task": "demo"}}
            ],
            "messages": [
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "type": "function",
                            "function": {"name": "create_async_task", "arguments": "{}"},
                        }
                    ],
                },
                {
                    "role": "tool",
                    "tool_call_id": "call-1",
                    "name": "create_async_task",
                    "content": "创建任务成功task:demo-123",
                },
            ],
            "used_tools": [],
            "route_kind": "direct_reply",
            "tool_names": ["create_async_task"],
        }
    )

    assert result is not None
    assert "jump_to" not in result
    assert "summary_text" not in result
    assert "summary_payload" not in result
    assert "summary_model_key" not in result
    assert result["verified_task_ids"] == ["task:demo-123"]
    assert result["route_kind"] == "task_dispatch"
    assert result["tool_call_payloads"] == []


@pytest.mark.asyncio
async def test_create_agent_postprocess_duplicate_rejection_with_old_task_id_does_not_count_as_dispatch() -> None:
    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(
        loop=SimpleNamespace(
            main_task_service=SimpleNamespace(get_task=lambda task_id: SimpleNamespace(task_id=task_id))
        )
    )

    result = await runner._postprocess_completed_tool_cycle(
        state={
            "tool_call_payloads": [
                {"id": "call-1", "name": "create_async_task", "arguments": {"task": "demo"}}
            ],
            "messages": [
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "type": "function",
                            "function": {"name": "create_async_task", "arguments": "{}"},
                        }
                    ],
                },
                {
                    "role": "tool",
                    "tool_call_id": "call-1",
                    "name": "create_async_task",
                    "content": "任务未创建：正在运行的任务 task:demo-123 与新任务完全相同（命中：core_requirement exact match）。\n- 重复拦截仅针对正在运行的任务：暂停旧任务或等待其进入终态（如失败）后，即可重新创建；\n- 如只是想补充约束、验收细节或更新要求 → 请改用 task_append_notice；\n- 如确需重做该工作 → 先与用户确认意图；确需删除旧任务时，先征求用户同意再操作。",
                },
            ],
            "used_tools": [],
            "route_kind": "direct_reply",
            "tool_names": ["create_async_task"],
        }
    )

    assert result["verified_task_ids"] == []
    assert result["route_kind"] == "direct_reply"
    assert "repair_overlay_text" not in result


def test_parse_create_async_task_result_recognizes_task_append_notice_guidance() -> None:
    parsed = ceo_runtime_ops.CeoFrontDoorRuntimeOps._parse_create_async_task_result(
        "任务未创建：现有任务 task:demo-123 请改用 task_append_notice 更新该任务。原因：new constraints"
    )

    assert parsed["created"] is False
    assert parsed["created_task_ids"] == []
    assert parsed["rejection_kind"] == "append_notice"


def test_parse_create_async_task_result_classifies_duplicate_message_with_notice_guidance() -> None:
    parsed = ceo_runtime_ops.CeoFrontDoorRuntimeOps._parse_create_async_task_result(
        "任务未创建：正在运行的任务 task:demo-123 与新任务完全相同（命中：core_requirement exact match）。\n"
        "- 重复拦截仅针对正在运行的任务：暂停旧任务或等待其进入终态（如失败）后，即可重新创建；\n"
        "- 如只是想补充约束、验收细节或更新要求 → 请改用 task_append_notice；\n"
        "- 如确需重做该工作 → 先与用户确认意图；确需删除旧任务时，先征求用户同意再操作。"
    )

    assert parsed["created"] is False
    assert parsed["created_task_ids"] == []
    assert parsed["rejection_kind"] == "duplicate"


@pytest.mark.asyncio
async def test_create_agent_postprocess_accumulates_multiple_verified_task_ids() -> None:
    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(
        loop=SimpleNamespace(
            main_task_service=SimpleNamespace(get_task=lambda task_id: SimpleNamespace(task_id=task_id))
        )
    )

    result = await runner._postprocess_completed_tool_cycle(
        state={
            "tool_call_payloads": [
                {"id": "call-1", "name": "create_async_task", "arguments": {"task": "demo-1"}},
                {"id": "call-2", "name": "create_async_task", "arguments": {"task": "demo-2"}},
            ],
            "messages": [
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "type": "function",
                            "function": {"name": "create_async_task", "arguments": "{}"},
                        },
                        {
                            "id": "call-2",
                            "type": "function",
                            "function": {"name": "create_async_task", "arguments": "{}"},
                        },
                    ],
                },
                {
                    "role": "tool",
                    "tool_call_id": "call-1",
                    "name": "create_async_task",
                    "content": "创建任务成功task:demo-123",
                },
                {
                    "role": "tool",
                    "tool_call_id": "call-2",
                    "name": "create_async_task",
                    "content": "创建任务成功task:demo-456",
                },
            ],
            "used_tools": [],
            "route_kind": "direct_reply",
            "tool_names": ["create_async_task"],
        }
    )

    assert result["verified_task_ids"] == ["task:demo-123", "task:demo-456"]
    assert result["route_kind"] == "task_dispatch"


@pytest.mark.asyncio
async def test_create_agent_graph_execute_tools_does_not_mark_duplicate_rejection_as_dispatch(monkeypatch) -> None:
    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(
        loop=SimpleNamespace(
            tools=SimpleNamespace(
                push_runtime_context=lambda context: object(),
                pop_runtime_context=lambda token: None,
            )
        )
    )
    monkeypatch.setattr(runner, "_registered_tools_for_state", lambda state: {"create_async_task": _CreateAsyncTaskLikeTool()})
    monkeypatch.setattr(runner, "_build_tool_runtime_context", lambda **kwargs: {"on_progress": None})

    async def _fake_execute_tool_call_with_raw_result(*, tool, tool_name, arguments, runtime_context, on_progress, tool_call_id):
        _ = tool, tool_name, arguments, runtime_context, on_progress, tool_call_id
        return (
            None,
            "任务未创建：正在运行的任务 task:demo-123 与新任务完全相同（命中：core_requirement exact match）。\n- 重复拦截仅针对正在运行的任务：暂停旧任务或等待其进入终态（如失败）后，即可重新创建；\n- 如只是想补充约束、验收细节或更新要求 → 请改用 task_append_notice；\n- 如确需重做该工作 → 先与用户确认意图；确需删除旧任务时，先征求用户同意再操作。",
            "success",
            "2026-04-18T23:00:00",
            "2026-04-18T23:00:01",
            1.0,
        )

    monkeypatch.setattr(runner, "_execute_tool_call_with_raw_result", _fake_execute_tool_call_with_raw_result)
    monkeypatch.setattr(runner, "_task_id_exists", lambda task_id: True)

    result = await runner._graph_execute_tools(
        {
            **_canonical_frontdoor_state(
                tool_names=["create_async_task"],
                rbac_visible_tool_names=["create_async_task"],

            ),
            "tool_call_payloads": [
                {"id": "call-1", "name": "create_async_task", "arguments": {"task": "demo"}}
            ],
            "messages": [],
            "used_tools": [],
            "route_kind": "direct_reply",
            "parallel_enabled": False,
            "max_parallel_tool_calls": 1,
            "synthetic_tool_calls_used": False,
            "response_payload": {"content": "", "tool_calls": []},
        },
        runtime=SimpleNamespace(context=SimpleNamespace()),
    )

    assert result["verified_task_ids"] == []
    assert result["route_kind"] == "direct_reply"


@pytest.mark.asyncio
async def test_create_agent_graph_execute_tools_accumulates_multiple_verified_task_ids(monkeypatch) -> None:
    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(
        loop=SimpleNamespace(
            tools=SimpleNamespace(
                push_runtime_context=lambda context: object(),
                pop_runtime_context=lambda token: None,
            )
        )
    )
    monkeypatch.setattr(runner, "_registered_tools_for_state", lambda state: {"create_async_task": _CreateAsyncTaskLikeTool()})
    monkeypatch.setattr(runner, "_build_tool_runtime_context", lambda **kwargs: {"on_progress": None})

    async def _fake_execute_tool_call_with_raw_result(*, tool, tool_name, arguments, runtime_context, on_progress, tool_call_id):
        _ = tool, tool_name, arguments, runtime_context, on_progress
        result_text = "创建任务成功task:demo-123" if tool_call_id == "call-1" else "创建任务成功task:demo-456"
        return (
            None,
            result_text,
            "success",
            "2026-04-18T23:00:00",
            "2026-04-18T23:00:01",
            1.0,
        )

    monkeypatch.setattr(runner, "_execute_tool_call_with_raw_result", _fake_execute_tool_call_with_raw_result)
    monkeypatch.setattr(runner, "_task_id_exists", lambda task_id: True)

    result = await runner._graph_execute_tools(
        {
            **_canonical_frontdoor_state(
                tool_names=["create_async_task"],
                rbac_visible_tool_names=["create_async_task"],

            ),
            "tool_call_payloads": [
                {"id": "call-1", "name": "create_async_task", "arguments": {"task": "demo-1"}},
                {"id": "call-2", "name": "create_async_task", "arguments": {"task": "demo-2"}},
            ],
            "messages": [],
            "used_tools": [],
            "route_kind": "direct_reply",
            "parallel_enabled": False,
            "max_parallel_tool_calls": 1,
            "synthetic_tool_calls_used": False,
            "response_payload": {"content": "", "tool_calls": []},
        },
        runtime=SimpleNamespace(context=SimpleNamespace()),
    )

    assert result["verified_task_ids"] == ["task:demo-123", "task:demo-456"]
    assert result["route_kind"] == "task_dispatch"






def test_create_agent_prompt_cache_key_contract_preserves_fallback_system_prompt_with_nonempty_state_messages() -> None:
    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace())

    contract = runner._frontdoor_prompt_contract(
        state=_canonical_frontdoor_state(
            messages=[
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "working"},
            ],
        ),
        provider_model="openai:gpt-4.1",
        tool_schemas=[],
        fallback_system_message={"role": "system", "content": "You are the CEO frontdoor agent."},
        fallback_messages=[{"role": "user", "content": "ignored fallback"}],
        session_key="web:shared",
    )

    assert contract.stable_messages[0] == {
        "role": "system",
        "content": "You are the CEO frontdoor agent.",
    }
    assert contract.request_messages[0] == {
        "role": "system",
        "content": "You are the CEO frontdoor agent.",
    }



def test_sanitize_provider_messages_repairs_null_tool_call_arguments() -> None:
    from main.runtime import chat_backend as chat_backend_module

    sanitized = chat_backend_module.sanitize_provider_messages(
        [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {
                            "name": "filesystem_stat",
                            "arguments": None,
                        },
                    }
                ],
            }
        ]
    )

    assert len(sanitized) == 1
    assert sanitized[0]["tool_calls"] == [
        {
            "type": "function",
            "function": {"name": "filesystem_stat", "arguments": {}},
            "id": "call-1",
        }
    ]








def test_create_agent_prompt_contract_avoids_duplicate_history_when_live_messages_are_compacted() -> None:
    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace())

    contract = runner._frontdoor_prompt_contract(
        state=_canonical_frontdoor_state(
            messages=[
                {"role": "system", "content": "stable system"},
                {
                    "role": "assistant",
                    "content": "[TOKEN_COMPACT_V2]\nsummary body",
                },
                {"role": "assistant", "content": "latest assistant"},
                {"role": "user", "content": "latest user"},
            ],
            stable_messages=[
                {"role": "system", "content": "stable system"},
                {"role": "assistant", "content": "[TOKEN_COMPACT_V2]\nsummary body"},
                {"role": "assistant", "content": "latest assistant"},
                {"role": "user", "content": "latest user"},
            ],
            dynamic_appendix_messages=[
                {"role": "assistant", "content": "## Retrieved Context\n- authoritative memory"}
            ],
        ),
        provider_model="openai:gpt-4.1",
        tool_schemas=[],
        session_key="web:shared",
    )

    non_contract_request_messages = [
        item
        for item in list(contract.request_messages or [])
        if not _is_frontdoor_runtime_tool_contract_record(dict(item))
    ]
    assert non_contract_request_messages == [
        {"role": "system", "content": "stable system"},
        {"role": "assistant", "content": "[TOKEN_COMPACT_V2]\nsummary body"},
        {"role": "assistant", "content": "latest assistant"},
        {"role": "user", "content": "latest user"},
        {"role": "assistant", "content": "## Retrieved Context\n- authoritative memory"},
    ]
    assert contract.stable_messages == [
        {"role": "system", "content": "stable system"},
        {"role": "assistant", "content": "[TOKEN_COMPACT_V2]\nsummary body"},
        {"role": "assistant", "content": "latest assistant"},
        {"role": "user", "content": "latest user"},
    ]


def test_create_agent_prompt_contract_does_not_treat_plain_short_recap_text_as_compaction() -> None:
    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace())

    contract = runner._frontdoor_prompt_contract(
        state=_canonical_frontdoor_state(
            messages=[
                {"role": "system", "content": "stable system"},
                {"role": "assistant", "content": "Short recap: here's the answer you asked for."},
                {"role": "user", "content": "latest user"},
            ],
            stable_messages=[
                {"role": "system", "content": "stable system"},
                {"role": "user", "content": "q1"},
                {"role": "assistant", "content": "a1"},
                {"role": "user", "content": "latest user"},
            ],
            dynamic_appendix_messages=[
                {"role": "assistant", "content": "## Retrieved Context\n- authoritative memory"}
            ],
        ),
        provider_model="openai:gpt-4.1",
        tool_schemas=[],
        session_key="web:shared",
    )

    assert contract.stable_messages == [
        {"role": "system", "content": "stable system"},
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "latest user"},
    ]
    non_contract_request_messages = [
        item
        for item in list(contract.request_messages or [])
        if not _is_frontdoor_runtime_tool_contract_record(dict(item))
    ]
    assert non_contract_request_messages == [
        {"role": "system", "content": "stable system"},
        {"role": "assistant", "content": "Short recap: here's the answer you asked for."},
        {"role": "user", "content": "latest user"},
        {"role": "assistant", "content": "## Retrieved Context\n- authoritative memory"},
    ]


def test_create_agent_prompt_contract_keeps_request_body_prefix_before_same_turn_tool_growth() -> None:
    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace())

    first = runner._frontdoor_prompt_contract(
        state=_canonical_frontdoor_state(
            messages=[
                {"role": "system", "content": "stable system"},
                {"role": "user", "content": "start"},
                {"role": "assistant", "content": "## Retrieved Context\n- authoritative memory"},
            ],
            stable_messages=[
                {"role": "system", "content": "stable system"},
                {"role": "user", "content": "start"},
                {"role": "assistant", "content": "## Retrieved Context\n- authoritative memory"},
            ],
            dynamic_appendix_messages=[
                {"role": "user", "content": '{"message_type":"frontdoor_runtime_tool_contract"}'},
            ],
        ),
        provider_model="openai:gpt-4.1",
        tool_schemas=[],
        session_key="web:shared",
    )
    second = runner._frontdoor_prompt_contract(
        state=_canonical_frontdoor_state(
            messages=[
                {"role": "system", "content": "stable system"},
                {"role": "user", "content": "start"},
                {"role": "assistant", "content": "## Retrieved Context\n- authoritative memory"},
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "type": "function",
                            "function": {"name": "filesystem_stat", "arguments": "{}"},
                        }
                    ],
                },
                {
                    "role": "tool",
                    "name": "filesystem_stat",
                    "tool_call_id": "call-1",
                    "content": '{"status":"success"}',
                },
            ],
            stable_messages=[
                {"role": "system", "content": "stable system"},
                {"role": "user", "content": "start"},
                {"role": "assistant", "content": "## Retrieved Context\n- authoritative memory"},
            ],
            dynamic_appendix_messages=[
                {"role": "user", "content": '{"message_type":"frontdoor_runtime_tool_contract"}'},
            ],
        ),
        provider_model="openai:gpt-4.1",
        tool_schemas=[],
        session_key="web:shared",
    )

    first_non_contract_messages = [
        dict(item)
        for item in list(first.request_messages or [])
        if not _is_frontdoor_runtime_tool_contract_record(dict(item))
    ]
    second_non_contract_messages = [
        dict(item)
        for item in list(second.request_messages or [])
        if not _is_frontdoor_runtime_tool_contract_record(dict(item))
    ]

    assert first_non_contract_messages == [
        {"role": "system", "content": "stable system"},
        {"role": "user", "content": "start"},
        {"role": "assistant", "content": "## Retrieved Context\n- authoritative memory"},
    ]
    assert second_non_contract_messages == [
        {"role": "system", "content": "stable system"},
        {"role": "user", "content": "start"},
        {"role": "assistant", "content": "## Retrieved Context\n- authoritative memory"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "filesystem_stat", "arguments": "{}"},
                }
            ],
        },
        {
            "role": "tool",
            "name": "filesystem_stat",
            "tool_call_id": "call-1",
            "content": '{"status":"success"}',
        },
    ]
    assert second_non_contract_messages[: len(first_non_contract_messages)] == first_non_contract_messages








async def test_create_agent_frontdoor_execute_tool_call_normalizes_nested_array_object_arguments() -> None:
    tool = _StructuredFactsTool()

    normalized_arguments = ceo_runtime_ops._normalize_frontdoor_tool_arguments(
        "record_facts",
        {
            "facts": [
                {
                    "category": "preference",
                    "entity": "user",
                    "attribute": "default_document_save_location",
                    "value": "desktop",
                    "observed_at": "2026-04-09T13:05:00+08:00",
                    "time_semantics": "durable_until_replaced",
                    "source_excerpt": "remember this preference",
                }
            ]
        },
    )

    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(
        loop=SimpleNamespace(
            tools=_RuntimeToolStack(),
            resource_manager=None,
            tool_execution_manager=None,
        )
    )

    result_text, status, _started_at, _finished_at, _elapsed_seconds = await runner._execute_tool_call(
        tool=tool,
        tool_name="record_facts",
        arguments=dict(normalized_arguments or {}),
        runtime_context={},
        on_progress=None,
    )

    payload = json.loads(result_text)
    assert status == "success"
    assert payload["facts"][0]["attribute"] == "default_document_save_location"

async def test_create_agent_frontdoor_execute_tool_call_omits_unset_optional_nested_fields() -> None:
    class _FakeMemoryManager:
        async def enqueue_write_request(self, **kwargs):
            return {
                "ok": True,
                "session_key": kwargs.get("session_key"),
                "decision_source": kwargs.get("decision_source"),
                "payload_text": kwargs.get("payload_text"),
                "trigger_source": kwargs.get("trigger_source"),
            }

    tool = MemoryUpdateTool(manager=_FakeMemoryManager())
    normalized_arguments = ceo_runtime_ops._normalize_frontdoor_tool_arguments(
        "memory_update",
        {"content": "remember this preference"},
    )

    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(
        loop=SimpleNamespace(
            tools=_RuntimeToolStack(),
            resource_manager=None,
            tool_execution_manager=None,
        )
    )

    result_text, status, _started_at, _finished_at, _elapsed_seconds = await runner._execute_tool_call(
        tool=tool,
        tool_name="memory_update",
        arguments=dict(normalized_arguments or {}),
        runtime_context={"session_key": "web:shared"},
        on_progress=None,
    )

    payload = json.loads(result_text)
    assert status == "success"
    assert payload["ok"] is True
    assert payload["session_key"] == "web:shared"
    assert payload["decision_source"] == "user"
    assert payload["payload_text"] == "remember this preference"
    assert payload["trigger_source"] == "memory_update_tool"

@pytest.mark.asyncio
async def test_create_agent_frontdoor_execute_tool_call_appends_loader_guidance_for_parameter_like_execute_errors() -> None:
    class _RuntimeToolStack:
        def push_runtime_context(self, _context):
            return "token"

        def pop_runtime_context(self, _token):
            return None

    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(
        loop=SimpleNamespace(
            tools=_RuntimeToolStack(),
            resource_manager=None,
        )
    )

    value_error_text, value_error_status, _started_at, _finished_at, _elapsed_seconds = await runner._execute_tool_call(
        tool=_ExecuteErrorTool(name="value_error_tool", exc=ValueError("value must be an absolute path")),
        tool_name="value_error_tool",
        arguments={"value": "demo"},
        runtime_context={},
        on_progress=None,
    )
    type_error_text, type_error_status, _started_at, _finished_at, _elapsed_seconds = await runner._execute_tool_call(
        tool=_ExecuteErrorTool(name="type_error_tool", exc=TypeError("value must be a string scalar")),
        tool_name="type_error_tool",
        arguments={"value": "demo"},
        runtime_context={},
        on_progress=None,
    )
    runtime_error_text, runtime_error_status, _started_at, _finished_at, _elapsed_seconds = await runner._execute_tool_call(
        tool=_ExecuteErrorTool(name="runtime_error_tool", exc=RuntimeError("runtime execution failed")),
        tool_name="runtime_error_tool",
        arguments={"value": "demo"},
        runtime_context={},
        on_progress=None,
    )

    assert value_error_status == "error"
    assert "Error executing value_error_tool: value must be an absolute path" in value_error_text
    assert _PARAMETER_CONTRACT_GUIDANCE_PREFIX in value_error_text
    assert "value=string" in value_error_text
    assert _PARAMETER_RECHECK_GUIDANCE not in value_error_text
    assert _PARAMETER_GUIDANCE_TEMPLATE.format(tool_name="value_error_tool") not in value_error_text
    assert type_error_status == "error"
    assert "Error executing type_error_tool: value must be a string scalar" in type_error_text
    assert _PARAMETER_CONTRACT_GUIDANCE_PREFIX in type_error_text
    assert "value=string" in type_error_text
    assert _PARAMETER_RECHECK_GUIDANCE not in type_error_text
    assert _PARAMETER_GUIDANCE_TEMPLATE.format(tool_name="type_error_tool") not in type_error_text
    assert runtime_error_status == "error"
    assert "Error executing runtime_error_tool: runtime execution failed" in runtime_error_text
    assert _PARAMETER_GUIDANCE_TEMPLATE.format(tool_name="runtime_error_tool") not in runtime_error_text
    assert _PARAMETER_RECHECK_GUIDANCE not in runtime_error_text
    assert _PARAMETER_CONTRACT_GUIDANCE_PREFIX not in runtime_error_text

    descriptor_tool = _ExecuteErrorTool(name="descriptor_value_error_tool", exc=ValueError("value must be an absolute path"))
    descriptor_tool._descriptor = object()
    descriptor_text, descriptor_status, _d_started, _d_finished, _d_elapsed = await runner._execute_tool_call(
        tool=descriptor_tool,
        tool_name="descriptor_value_error_tool",
        arguments={"value": "demo"},
        runtime_context={},
        on_progress=None,
    )
    assert descriptor_status == "error"
    assert "Error executing descriptor_value_error_tool: value must be an absolute path" in descriptor_text
    assert _PARAMETER_GUIDANCE_TEMPLATE.format(tool_name="descriptor_value_error_tool") in descriptor_text
    assert _PARAMETER_RECHECK_GUIDANCE not in descriptor_text
    assert _PARAMETER_CONTRACT_GUIDANCE_PREFIX not in descriptor_text






@pytest.mark.asyncio
async def test_graph_call_model_restarts_with_refreshed_model_refs_after_runtime_refresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loop = SimpleNamespace(main_task_service=None, _runtime_model_revision=1)
    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(loop=loop)
    seen_model_refs: list[list[str]] = []

    monkeypatch.setattr(
        runner,
        "_frontdoor_tool_schemas_for_state",
        lambda *, state, runtime: [
            {
                "type": "function",
                "function": {"name": str(name), "description": "", "parameters": {"type": "object"}},
            }
            for name in list(state.get("provider_tool_names") or state.get("tool_names") or [])
        ],
    )
    monkeypatch.setattr(
        runner,
        "_frontdoor_prompt_contract",
        lambda **kwargs: SimpleNamespace(
            request_messages=list(kwargs["state"].get("messages") or []),
            prompt_cache_key=f"pk:{kwargs['provider_model']}",
            diagnostics={},
        ),
    )
    monkeypatch.setattr(
        runner,
        "_resolve_ceo_model_refs",
        lambda: ["new-model"] if int(getattr(loop, "_runtime_model_revision", 0) or 0) >= 2 else ["old-model"],
    )
    monkeypatch.setattr(
        runner,
        "_resolve_frontdoor_send_model_context_window",
        lambda **kwargs: {
            "model_key": str((kwargs.get("model_refs") or [""])[0] or ""),
            "provider_model": str((kwargs.get("model_refs") or [""])[0] or ""),
            "context_window_tokens": 128000,
        },
    )

    def _refresh_runtime_config(_loop, *, force: bool = False, reason: str = "runtime") -> bool:
        _ = force, reason
        if int(getattr(_loop, "_runtime_model_revision", 0) or 0) < 2:
            _loop._runtime_model_revision = 2
            return True
        return False

    monkeypatch.setattr(ceo_runtime_ops, "refresh_loop_runtime_config", _refresh_runtime_config)

    async def _call_model_with_tools(**kwargs):
        model_refs = list(kwargs.get("model_refs") or [])
        seen_model_refs.append(model_refs)
        if model_refs == ["old-model"]:
            raise RuntimeError(ceo_runtime_ops.PUBLIC_PROVIDER_FAILURE_MESSAGE)
        return {"content": "ok"}

    monkeypatch.setattr(runner, "_call_model_with_tools", _call_model_with_tools)
    monkeypatch.setattr(runner, "_checkpoint_safe_model_response_payload", lambda _message: {"ok": True})
    monkeypatch.setattr(runner, "_persist_frontdoor_actual_request", lambda **_: {})

    state = _canonical_frontdoor_state(
        messages=[
            {"role": "system", "content": "SYSTEM"},
            {"role": "user", "content": "hello"},
        ],
        model_refs=["old-model"],
        prompt_cache_key="",
        prompt_cache_diagnostics={},
        session_key="web:shared",
        parallel_enabled=False,
    )

    result = await runner._graph_call_model(
        state,
        runtime=SimpleNamespace(context=SimpleNamespace(session_key="web:shared", session=SimpleNamespace())),
    )

    assert seen_model_refs == [["old-model"], ["new-model"]]
    assert result["model_refs"] == ["new-model"]
    assert result["prompt_cache_key"] == "pk:new-model"


@pytest.mark.asyncio
async def test_graph_call_model_stops_retrying_provider_chain_exhaustion_after_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loop = SimpleNamespace(main_task_service=None, _runtime_model_revision=1)
    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(loop=loop)
    seen_model_refs: list[list[str]] = []
    sleep_calls: list[float] = []
    real_sleep = asyncio.sleep

    async def _fake_sleep(delay: float) -> None:
        sleep_calls.append(float(delay))
        await real_sleep(0)

    monkeypatch.setattr(ceo_runtime_ops.asyncio, "sleep", _fake_sleep)
    monkeypatch.setattr(
        runner,
        "_frontdoor_tool_schemas_for_state",
        lambda *, state, runtime: [
            {
                "type": "function",
                "function": {"name": str(name), "description": "", "parameters": {"type": "object"}},
            }
            for name in list(state.get("provider_tool_names") or state.get("tool_names") or [])
        ],
    )
    monkeypatch.setattr(
        runner,
        "_frontdoor_prompt_contract",
        lambda **kwargs: SimpleNamespace(
            request_messages=list(kwargs["state"].get("messages") or []),
            prompt_cache_key="pk:old-model",
            diagnostics={},
        ),
    )
    monkeypatch.setattr(runner, "_resolve_ceo_model_refs", lambda: ["old-model"])
    monkeypatch.setattr(
        runner,
        "_resolve_frontdoor_send_model_context_window",
        lambda **kwargs: {
            "model_key": str((kwargs.get("model_refs") or [""])[0] or ""),
            "provider_model": str((kwargs.get("model_refs") or [""])[0] or ""),
            "context_window_tokens": 128000,
        },
    )
    monkeypatch.setattr(ceo_runtime_ops, "refresh_loop_runtime_config", lambda *_args, **_kwargs: False)

    async def _call_model_with_tools(**kwargs):
        model_refs = list(kwargs.get("model_refs") or [])
        seen_model_refs.append(model_refs)
        raise RuntimeError(ceo_runtime_ops.PUBLIC_PROVIDER_FAILURE_MESSAGE)

    monkeypatch.setattr(runner, "_call_model_with_tools", _call_model_with_tools)

    state = _canonical_frontdoor_state(
        messages=[
            {"role": "system", "content": "SYSTEM"},
            {"role": "user", "content": "hello"},
        ],
        model_refs=["old-model"],
        prompt_cache_key="",
        prompt_cache_diagnostics={},
        session_key="web:shared",
        parallel_enabled=False,
    )

    with pytest.raises(RuntimeError, match="configured fallback chain"):
        await asyncio.wait_for(
            runner._graph_call_model(
                state,
                runtime=SimpleNamespace(context=SimpleNamespace(session_key="web:shared", session=SimpleNamespace())),
            ),
            timeout=0.2,
        )

    assert seen_model_refs == [["old-model"], ["old-model"], ["old-model"]]
    assert sleep_calls == [1.0, 2.0]




def test_create_agent_runner_preserves_full_tool_call_payloads_when_approval_request_is_subset() -> None:
    payload = {
        "kind": "frontdoor_tool_approval",
        "tool_calls": [{"name": "create_async_task", "arguments": {"task": "demo"}}],
        "tool_call_payloads": [
            {"name": "create_async_task", "arguments": {"task": "demo"}},
            {"name": "memory_update", "arguments": {"content": "persist this"}},
        ],
    }

    with pytest.raises(CeoFrontdoorInterrupted) as exc_info:
        ceo_runtime_ops.raise_frontdoor_approval_interrupt(
            state={
                "approval_request": {
                    "kind": "frontdoor_tool_approval",
                    "tool_calls": [{"name": "create_async_task", "arguments": {"task": "demo"}}],
                },
            },
            payload=payload,
        )

    assert exc_info.value.values["tool_call_payloads"] == [
        {"name": "create_async_task", "arguments": {"task": "demo"}},
        {"name": "memory_update", "arguments": {"content": "persist this"}},
    ]


def test_create_agent_runner_keeps_pending_interrupt_contract_coherent() -> None:
    with pytest.raises(CeoFrontdoorInterrupted) as exc_info:
        ceo_runtime_ops.raise_frontdoor_approval_interrupt(
            state={
                "prompt_cache_key": "cache-key",
                "prompt_cache_diagnostics": {
                    "stable_prompt_signature": "sig-1",
                    "tool_signature_count": 1,
                },
            },
            payload={
                "kind": "frontdoor_tool_approval",
                "tool_calls": [{"name": "create_async_task", "arguments": {"task": "demo"}}],
            },
        )

    assert exc_info.value.values == {
        "approval_request": {
            "kind": "frontdoor_tool_approval",
            "tool_calls": [{"name": "create_async_task", "arguments": {"task": "demo"}}],
        },
        "tool_call_payloads": [{"name": "create_async_task", "arguments": {"task": "demo"}}],
        "prompt_cache_key": "cache-key",
        "prompt_cache_diagnostics": {
            "stable_prompt_signature": "sig-1",
            "tool_signature_count": 1,
        },
    }


def test_create_agent_runner_preserves_full_tool_call_payload_batch_from_interrupt_state() -> None:
    subset_payloads = [{"id": "call-1", "name": "exec", "arguments": {"command": "safe subset"}}]
    full_payloads = subset_payloads + [
        {"id": "call-2", "name": "create_async_task", "arguments": {"task": "risky full batch"}}
    ]

    with pytest.raises(CeoFrontdoorInterrupted) as exc_info:
        ceo_runtime_ops.raise_frontdoor_approval_interrupt(
            state={"prompt_cache_key": "cache-key"},
            payload={
                "approval_request": {
                    "kind": "frontdoor_tool_approval",
                    "tool_calls": subset_payloads,
                },
                "tool_call_payloads": full_payloads,
            },
        )

    assert exc_info.value.values == {
        "approval_request": {
            "kind": "frontdoor_tool_approval",
            "tool_calls": subset_payloads,
        },
        "tool_call_payloads": full_payloads,
        "prompt_cache_key": "cache-key",
    }


def test_create_agent_runner_sanitizes_interrupt_payloads_before_raising() -> None:
    with pytest.raises(CeoFrontdoorInterrupted) as exc_info:
        ceo_runtime_ops.raise_frontdoor_approval_interrupt(
            state={"approval_request": {"details": _NonPrimitive()}, "items": {_NonPrimitive()}},
            payload={
                "kind": "frontdoor_tool_approval",
                "payload": _NonPrimitive(),
                "extra": {_NonPrimitive()},
            },
        )

    assert exc_info.value.values == {
        "approval_request": {"details": "non-primitive"},
        "items": ["non-primitive"],
    }
    assert exc_info.value.interrupts[0].value == {
        "kind": "frontdoor_tool_approval",
        "payload": "non-primitive",
        "extra": ["non-primitive"],
    }


def _node_error_heartbeat_state() -> dict:
    return {
        "session_key": "qq:default",
        "heartbeat_internal": True,
        "user_input": {
            "content": "## EVENT BUNDLE\n- Task ... node paused after an error",
            "metadata": {
                "heartbeat_internal": True,
                "heartbeat_reason": "task_node_error",
                "heartbeat_task_ids": ["task:26e64d1dc8b3"],
                "heartbeat_node_ids": ["node:cbbde9623fb4"],
            },
        },
    }








@pytest.mark.asyncio
async def test_postprocess_appends_a_frontdoor_turn_with_tool_results() -> None:
    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace(main_task_service=None))
    state = {
        "messages": [
            {"role": "user", "content": "start"},
            {
                "role": "assistant",
                "content": "checking files",
                "tool_calls": [
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {"name": "exec", "arguments": "{\"command\": \"dir\"}"},
                    }
                ],
            },
            {
                "role": "tool",
                "name": "exec",
                "tool_call_id": "call-1",
                "content": "{\"result_text\": \"ok\", \"status\": \"success\"}",
            },
        ],
        "tool_call_payloads": [
            {"id": "call-1", "name": "exec", "arguments": {"command": "dir"}},
        ],
        "frontdoor_turn_id": "turn-1",
        "frontdoor_turns": [],
    }

    result = await runner._postprocess_completed_tool_cycle(state=state)

    assert result is not None
    turns = result["frontdoor_turns"]
    assert len(turns) == 1
    assert turns[0]["turn_id"] == "turn-1"
    assert turns[0]["text"] == "checking files"
    assert turns[0]["tools"][0]["tool_call_id"] == "call-1"
    assert turns[0]["tools"][0]["output_text"] == "ok"


def test_build_frontdoor_request_artifact_payload_records_the_served_model() -> None:
    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace(main_task_service=None))

    payload = runner._build_frontdoor_request_artifact_payload(
        state={
            "model_refs": ["deepseek-flash", "sensenova-6.8-flash-lite"],
            "frontdoor_token_preflight_diagnostics": {"provider_model": "openai:deepseek-flash"},
        },
        session_key="web:shared",
        turn_id="turn-escalated",
        request_messages=[{"role": "user", "content": "hi"}],
        tool_schemas=[],
        prompt_cache_key="cache-key",
        prompt_cache_diagnostics=build_actual_request_diagnostics(
            request_messages=[{"role": "user", "content": "hi"}],
            tool_schemas=[],
        ),
        parallel_tool_calls=False,
        provider_request_meta={"provider": "openai_chat"},
        provider_request_body={"model": "sensenova-6.8-flash-lite"},
        usage={"input_tokens": 70744, "cache_hit_tokens": 0},
        request_kind="frontdoor_actual_request",
        request_lane="visible_frontdoor",
    )

    assert payload["provider_model"] == "sensenova-6.8-flash-lite"
    assert payload["requested_provider_model"] == "openai:deepseek-flash"
    assert payload["observed_input_truth"]["provider_model"] == "sensenova-6.8-flash-lite"


def test_build_frontdoor_request_artifact_payload_falls_back_to_the_requested_model() -> None:
    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace(main_task_service=None))

    payload = runner._build_frontdoor_request_artifact_payload(
        state={
            "model_refs": ["deepseek-flash"],
            "frontdoor_token_preflight_diagnostics": {"provider_model": "openai:deepseek-flash"},
        },
        session_key="web:shared",
        turn_id="turn-plain",
        request_messages=[{"role": "user", "content": "hi"}],
        tool_schemas=[],
        prompt_cache_key="cache-key",
        prompt_cache_diagnostics=build_actual_request_diagnostics(
            request_messages=[{"role": "user", "content": "hi"}],
            tool_schemas=[],
        ),
        parallel_tool_calls=False,
        provider_request_meta={"provider": "responses"},
        provider_request_body={},
        usage={"input_tokens": 10, "cache_hit_tokens": 2},
        request_kind="frontdoor_actual_request",
        request_lane="visible_frontdoor",
    )

    assert payload["provider_model"] == "openai:deepseek-flash"
    assert payload["requested_provider_model"] == "openai:deepseek-flash"
