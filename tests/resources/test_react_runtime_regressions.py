from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

import main.service.runtime_service as runtime_service_module
from g3ku.agent.tools.base import (
    REUSE_ARGS_MODEL_DESCRIPTION_SENTENCE,
    REUSE_ARGS_PARAMETER_NAME,
    Tool,
)
from g3ku.agent.tools.main_runtime import LoadSkillContextTool, LoadToolContextTool
from g3ku.content import ContentNavigationService, parse_content_envelope
from g3ku.providers.base import LLMResponse, ToolCallRequest
from g3ku.resources.embedded_mcp import EmbeddedMCPTool
from g3ku.resources.loader import ManifestBackedTool, ResourceLoader
from g3ku.resources.models import ResourceKind, ToolResourceDescriptor
from g3ku.resources.registry import ResourceRegistry
from g3ku.runtime.context.node_context_selection import NodeContextSelectionResult
from g3ku.runtime.tool_contract_block import attach_tool_contract_block
from g3ku.runtime.tool_history import analyze_tool_call_history
from g3ku.utils.helpers import tools_bundle_single_source_enabled
from main.errors import TaskPausedError
from main.governance.tool_context import (
    apply_runtime_tool_context_projection,
    build_tool_context_fingerprint,
)
from main.runtime.internal_tools import SpawnChildNodesTool, SubmitFinalResultTool
from main.runtime.node_prompt_contract import (
    NodeRuntimeToolContract,
    extract_node_dynamic_contract_payload,
    inject_node_dynamic_contract_message,
    is_node_dynamic_contract_message,
)
from main.runtime.react_loop import (
    _INVALID_FINAL_SUBMISSION_LIMIT,
    _PLAIN_TEXT_REPLY_STRIKE_LIMIT,
    ReActToolLoop,
)
from main.runtime.tool_call_repair import extract_tool_calls_from_xml_pseudo_content
from main.service.runtime_service import MainRuntimeService
from main.storage.artifact_store import TaskArtifactStore
from main.storage.sqlite_store import SQLiteTaskStore


@pytest.fixture(autouse=True)
def _default_node_send_preflight_context_window(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Node send preflight requires a resolved context-window, but most regressions in this file
    use placeholder model keys (for example "fake"). Keep the default behavior stable by
    supplying a deterministic context-window resolver for tests, while still allowing
    individual tests to override via their own monkeypatch calls.
    """

    import main.runtime.react_loop as react_loop_module
    from main.runtime.chat_backend import SendModelContextWindowInfo

    def _resolve(**kwargs) -> SendModelContextWindowInfo:
        refs = list(kwargs.get("model_refs") or [])
        model_key = str(refs[0] or "").strip() if refs else ""
        return SendModelContextWindowInfo(
            model_key=model_key,
            provider_id="test",
            provider_model=f"test:{model_key}" if model_key else "test",
            resolved_model=model_key,
            context_window_tokens=32000,
            resolution_error="",
        )

    monkeypatch.setattr(
        react_loop_module,
        "get_runtime_config",
        lambda **_: (SimpleNamespace(), 0, False),
        raising=False,
    )
    monkeypatch.setattr(
        react_loop_module.runtime_chat_backend,
        "resolve_send_model_context_window_info",
        _resolve,
        raising=False,
    )


class _FakeTaskStore:
    def __init__(self) -> None:
        self._task = SimpleNamespace(cancel_requested=False, pause_requested=False)
        self._node = None

    def get_task(self, task_id: str):
        _ = task_id
        return self._task

    def get_node_pause_flags(self, node_id: str):
        # 与真 Store 同形：只回暂停判定要的三键，不建整行模型。
        node = self.get_node(node_id)
        if node is None:
            return None
        return {
            'pause_requested': bool(getattr(node, 'pause_requested', False)),
            'is_paused': bool(getattr(node, 'is_paused', False)),
            'pause_reason': str(getattr(node, 'pause_reason', '') or ''),
        }

    def get_node(self, node_id: str):
        _ = node_id
        return self._node


class _FakeLogService:
    def __init__(self) -> None:
        self._store = _FakeTaskStore()
        self._content_store = None
        self._frames: dict[tuple[str, str], dict[str, object]] = {}

    def set_pause_state(self, task_id: str, pause_requested: bool, is_paused: bool) -> None:
        _ = task_id, pause_requested, is_paused

    def update_node_input(self, *args, **kwargs) -> None:
        _ = args, kwargs

    def upsert_frame(self, task_id: str, payload: dict[str, object], publish_snapshot: bool = True) -> None:
        _ = publish_snapshot
        node_id = str((payload or {}).get("node_id") or "").strip()
        self._frames[(str(task_id), node_id)] = dict(payload or {})

    def append_node_output(self, *args, **kwargs) -> None:
        _ = args, kwargs

    def update_frame(self, task_id: str, node_id: str, mutate, publish_snapshot: bool = True) -> None:
        _ = publish_snapshot
        key = (str(task_id), str(node_id))
        current = dict(self._frames.get(key) or {})
        self._frames[key] = dict(mutate(current) or {})

    def remove_frame(self, task_id: str, node_id: str, publish_snapshot: bool = True) -> None:
        _ = publish_snapshot
        self._frames.pop((str(task_id), str(node_id)), None)

    def read_runtime_frame(self, task_id: str, node_id: str):
        return dict(self._frames.get((str(task_id), str(node_id))) or {})


class _HeartbeatRecorder:
    def __init__(self) -> None:
        self.terminal_calls: list[tuple[str, dict[str, object]]] = []

    def enqueue_tool_terminal(self, *, session_id: str, payload: dict[str, object]) -> None:
        self.terminal_calls.append((str(session_id or ""), dict(payload or {})))


class _SlowCompleteTool(Tool):
    @property
    def name(self) -> str:
        return "slow_complete"

    @property
    def description(self) -> str:
        return "Complete after a short delay."

    @property
    def parameters(self) -> dict[str, object]:
        return {
            "type": "object",
            "properties": {},
            "required": [],
        }

    async def execute(self, **kwargs) -> str:
        _ = kwargs
        await asyncio.sleep(0.08)
        return "done"


# 无资源 descriptor 的控制工具（submit_final_result 等）参数错误必须自带契约，
# 这句前缀是"契约已回贴"而不是"泛化提醒"的判别标记。
_PARAMETER_CONTRACT_GUIDANCE_PREFIX = '该工具没有可加载的扩展说明，参数契约如下（必填项及其类型与取值结构）：'


def _submit_final_result_tool(*, node_kind: str = "execution") -> SubmitFinalResultTool:
    async def _submit(payload: dict[str, object]) -> dict[str, object]:
        return dict(payload)

    return SubmitFinalResultTool(_submit, node_kind=node_kind)


class _RuntimeToolService:
    def __init__(self) -> None:
        self.load_tool_calls: list[dict[str, object]] = []
        self.load_skill_calls: list[dict[str, object]] = []

    async def startup(self) -> None:
        return None

    def load_tool_context_v2(self, **kwargs):
        self.load_tool_calls.append(dict(kwargs))
        return {"ok": True, "tool_id": str(kwargs.get("tool_id") or "")}

    def load_skill_context_v2(self, **kwargs):
        self.load_skill_calls.append(dict(kwargs))
        return {"ok": True, "skill_id": str(kwargs.get("skill_id") or "")}


class _StageRuntimeProbeTool(Tool):
    def __init__(self) -> None:
        self.runtime_payloads: list[dict[str, object]] = []

    @property
    def name(self) -> str:
        return "filesystem_stat"

    @property
    def description(self) -> str:
        return "Capture runtime tool contract details during filesystem_stat execution."

    @property
    def parameters(self) -> dict[str, object]:
        return {
            "type": "object",
            "properties": {
            },
            "required": ["stage_goal", "tool_round_budget"],
        }

    async def execute(
        self,
        stage_goal: str,
        tool_round_budget: int,
        __g3ku_runtime: dict[str, object] | None = None,
        **kwargs,
    ):
        runtime = dict(__g3ku_runtime or {}) if isinstance(__g3ku_runtime, dict) else {}
        self.runtime_payloads.append(runtime)
        _ = kwargs
        return json.dumps(
            {
                "ok": True,
            },
            ensure_ascii=False,
        )




def test_react_tool_message_status_treats_ok_false_payload_as_error() -> None:
    assert ReActToolLoop._tool_message_status('{"ok": false, "error": "bad args"}') == "error"
    assert ReActToolLoop._tool_message_status({"ok": False, "error": "bad args"}) == "error"


class _DirectLoadTool(Tool):
    @property
    def name(self) -> str:
        return "load_tool_context"

    @property
    def description(self) -> str:
        return "Return a large direct-load payload."

    @property
    def parameters(self) -> dict[str, object]:
        return {"type": "object", "properties": {}, "required": []}

    async def execute(self, **kwargs):
        _ = kwargs
        payload = {
            "ok": True,
            "level": "l2",
            "content": "\n".join(f"tool line {index:03d}" for index in range(1, 321)),
            "l0": "tool short summary",
            "l1": "tool structured overview",
            "path": "/virtual/content-tool.md",
            "uri": "negi://resource/tool/content",
        }
        return json.dumps(payload, ensure_ascii=False)


class _LargeInlineManifestTool(Tool):
    @property
    def name(self) -> str:
        return "inline_manifest_tool"

    @property
    def description(self) -> str:
        return "Return a large payload that should stay inline when opted in."

    @property
    def parameters(self) -> dict[str, object]:
        return {"type": "object", "properties": {}, "required": []}

    async def execute(self, **kwargs):
        _ = kwargs
        payload = {
            "ok": True,
            "stdout": "\n".join(f"inline line {index:03d}" for index in range(240)),
        }
        return json.dumps(payload, ensure_ascii=False)


class _RecordingTool(Tool):
    def __init__(self, name: str, sink: list[tuple[str, str]]) -> None:
        self._name = name
        self._sink = sink

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return f"record {self._name}"

    @property
    def parameters(self) -> dict[str, object]:
        return {
            "type": "object",
            "properties": {
                "value": {"type": "string", "description": "value"},
            },
            "required": ["value"],
        }

    async def execute(self, value: str, **kwargs):
        _ = kwargs
        self._sink.append((self._name, value))
        return json.dumps({"ok": True, "tool": self._name, "value": value}, ensure_ascii=False)


class _CountTool(Tool):
    def __init__(self, sink: list[int]) -> None:
        self._sink = sink

    @property
    def name(self) -> str:
        return "count_tool"

    @property
    def description(self) -> str:
        return "record integer counts"

    @property
    def parameters(self) -> dict[str, object]:
        return {
            "type": "object",
            "properties": {
                "count": {"type": "integer", "description": "count"},
            },
            "required": ["count"],
        }

    async def execute(self, count: int, **kwargs):
        _ = kwargs
        self._sink.append(int(count))
        return json.dumps({"ok": True, "count": int(count)}, ensure_ascii=False)


class _TypedSchemaTool(Tool):
    @property
    def name(self) -> str:
        return "typed_schema_tool"

    @property
    def description(self) -> str:
        return "exercise XML schema coercion"

    @property
    def parameters(self) -> dict[str, object]:
        return {
            "type": "object",
            "properties": {
                "count": {"type": "integer"},
                "enabled": {"type": "boolean"},
                "metadata": {
                    "type": "object",
                    "properties": {
                        "source": {"type": "string"},
                    },
                    "required": ["source"],
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "note": {"type": "string"},
            },
            "required": ["count", "enabled", "metadata", "tags", "note"],
        }

    async def execute(self, **kwargs):
        return json.dumps(kwargs, ensure_ascii=False)


def test_tool_model_visible_schema_falls_back_to_runtime_schema_when_unset() -> None:
    class _ModelVisibleSchemaFallbackTool(Tool):
        @property
        def name(self) -> str:
            return "model_visible_schema_tool"

        @property
        def description(self) -> str:
            return "Use the runtime schema when no model-only schema override is defined."

        @property
        def parameters(self) -> dict[str, object]:
            return {
                "type": "object",
                "properties": {
                    "task": {"type": "string", "description": "task"},
                },
                "required": ["task"],
            }

        async def execute(self, **kwargs):
            return json.dumps(kwargs, ensure_ascii=False)

    tool = _ModelVisibleSchemaFallbackTool()

    assert tool.model_description == tool.description
    assert tool.model_parameters == tool.parameters
    assert tool.to_model_schema() == {
        "type": "function",
        "function": {
            "name": "model_visible_schema_tool",
            "description": (
                "Use the runtime schema when no model-only schema override is defined. "
                + REUSE_ARGS_MODEL_DESCRIPTION_SENTENCE
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "task": {"type": "string", "description": "task"},
                    # 统一 timeout 合同：模型可见 schema 统一追加可选超时参数（带单位名）。
                    "timeout_seconds": {
                        "type": "number",
                        "description": (
                            "Optional maximum execution time for this call, in SECONDS (fractions allowed, e.g. 0.5). "
                            "This is seconds, NOT milliseconds — for a 60s limit pass 60, not 60000. "
                            "When omitted, the runtime default (600s) applies and the call is stopped at that limit. "
                            "Pass an explicit larger value for legitimately long-running work; there is no upper cap."
                        ),
                    },
                    # 入参复用字段：派生、可选、不带参数级 description（provider 会逐层剥掉）。
                    REUSE_ARGS_PARAMETER_NAME: {"type": "boolean"},
                },
                "required": ["task"],
            },
        },
    }

class _StageProtocolNoopTool(Tool):
    def __init__(self, name: str) -> None:
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return f"protocol helper {self._name}"

    @property
    def parameters(self) -> dict[str, object]:
        return {"type": "object", "properties": {}, "required": []}

    async def execute(self, **kwargs):
        return json.dumps({"ok": True, "tool": self._name, "args": kwargs}, ensure_ascii=False)


class _ModelSchemaRecordingTool(Tool):
    def __init__(self, *, name: str, authoritative_description: str, model_description: str) -> None:
        self._name = name
        self._authoritative_description = authoritative_description
        self._model_description = model_description

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return self._authoritative_description

    @property
    def parameters(self) -> dict[str, object]:
        return {
            "type": "object",
            "properties": {
                "value": {
                    "type": "string",
                    "description": "authoritative value contract",
                }
            },
            "required": ["value"],
        }

    @property
    def model_description(self) -> str:
        return self._model_description

    @property
    def model_parameters(self) -> dict[str, object]:
        return {
            "type": "object",
            "properties": {
                "summary": {
                    "type": "string",
                    "description": "compact model-visible contract",
                }
            },
            "required": ["summary"],
        }

    async def execute(self, **kwargs):
        return json.dumps(kwargs, ensure_ascii=False)


class _LargeModelSchemaTool(_ModelSchemaRecordingTool):
    def __init__(self, *, name: str) -> None:
        super().__init__(
            name=name,
            authoritative_description="authoritative oversized tool contract",
            model_description="oversized model-visible contract " + ("X" * 12000),
        )


def test_compact_schema_internal_execution_tools_expose_shorter_model_visible_contracts() -> None:
    async def _spawn_children(*args, **kwargs):
        _ = args, kwargs
        return []

    async def _submit_final(payload: dict[str, object]) -> dict[str, object]:
        return dict(payload)

    tools = [
        SpawnChildNodesTool(_spawn_children),
        SubmitFinalResultTool(_submit_final, node_kind="execution"),
    ]

    for tool in tools:
        authoritative = tool.to_schema()["function"]
        compact = tool.to_model_schema()["function"]

        assert compact["name"] == authoritative["name"]
        assert len(compact["description"]) < len(authoritative["description"])
        assert len(json.dumps(compact["parameters"], ensure_ascii=False, sort_keys=True)) < len(
            json.dumps(authoritative["parameters"], ensure_ascii=False, sort_keys=True)
        )

    assert tools[0].validate_params({"children": [{"goal": "a", "prompt": "b"}]}) == [
        "missing required children[0].execution_policy"
    ]
    assert tools[1].validate_params({"status": "success"}) == [
        "missing required summary",
        "missing required answer",
    ]


def test_compact_schema_memory_update_manifest_keeps_authoritative_schema_and_moves_guidance_to_toolskill() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    manifest = yaml.safe_load((repo_root / "tools" / "memory_update" / "resource.yaml").read_text(encoding="utf-8")) or {}
    toolskill = (repo_root / "tools" / "memory_update" / "toolskills" / "SKILL.md").read_text(encoding="utf-8")
    content_schema = (
        manifest.get("parameters", {})
        .get("properties", {})
        .get("content", {})
    )

    assert content_schema["type"] == "string"
    assert "queue for the memory agent" in str(content_schema.get("description") or "")
    assert "raw memory request" in toolskill.lower()
    assert "memory agent will decide the final compact `MEMORY.md` wording" in toolskill


def test_compact_schema_resource_tool_uses_manifest_model_visible_overrides(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    (workspace / "skills").mkdir(parents=True, exist_ok=True)
    (workspace / "tools").mkdir(parents=True, exist_ok=True)
    source_root = Path(__file__).resolve().parents[2] / "tools" / "memory_update"
    target_root = workspace / "tools" / "memory_update"
    import shutil
    shutil.copytree(source_root, target_root)

    registry = ResourceRegistry(workspace, skills_dir=workspace / "skills", tools_dir=workspace / "tools")
    snapshot = registry.discover()
    descriptor = snapshot.tools["memory_update"]
    tool = ResourceLoader(workspace).load_tool(
        descriptor,
        services={"memory_manager": SimpleNamespace()},
    )

    assert tool is not None
    schema = tool.to_model_schema()["function"]
    content_schema = schema["parameters"]["properties"]["content"]

    assert schema["description"].startswith("Queue a durable long-term memory request in natural language.")
    assert content_schema["type"] == "string"
    assert "queue for the memory agent" in str(content_schema.get("description") or "")


def test_compact_schema_manifest_backed_tool_uses_normalized_model_visible_overrides(tmp_path: Path) -> None:
    descriptor = ToolResourceDescriptor(
        kind=ResourceKind.TOOL,
        name="manifest_backed_fake",
        description="Authoritative description.",
        root=tmp_path,
        manifest_path=tmp_path / "resource.yaml",
        fingerprint="fake",
        parameters={
            "type": "object",
            "properties": {
                "bad-name": {"type": "string"},
            },
            "required": ["bad-name"],
        },
        metadata={
            "model_description": "Compact manifest-backed description.",
            "model_parameters": {
                "properties": {
                    "summary": {"type": "string"},
                },
                "required": ["summary"],
            },
        },
    )
    tool = ManifestBackedTool(descriptor, handler=lambda **kwargs: kwargs)
    schema = tool.to_model_schema()["function"]

    assert schema["description"] == f"Compact manifest-backed description. {REUSE_ARGS_MODEL_DESCRIPTION_SENTENCE}"
    assert schema["parameters"]["type"] == "object"
    assert schema["parameters"]["required"] == ["summary"]
    assert schema["parameters"]["properties"]["summary"]["type"] == "string"


def test_tool_model_visible_schema_falls_back_to_authoritative_schema() -> None:
    class _FakeTool(Tool):
        @property
        def name(self) -> str:
            return "fake_tool"

        @property
        def description(self) -> str:
            return "Full schema description."

        @property
        def parameters(self) -> dict[str, Any]:
            return {
                "type": "object",
                "properties": {
                    "value": {
                        "type": "string",
                        "description": "Full contract description.",
                    }
                },
                "required": ["value"],
            }

        async def execute(self, **kwargs: Any) -> Any:
            return kwargs

    tool = _FakeTool()

    assert (
        tool.to_schema()["function"]["parameters"]["properties"]["value"]["description"]
        == "Full contract description."
    )
    # 统一 timeout 合同：模型可见 schema 在验证合同之上统一追加可选 timeout_seconds 参数，
    # `reuse_args` 同源（派生、可选、不进 required），其余部分与权威验证合同保持一致。
    model_schema = tool.to_model_schema()
    model_properties = dict(model_schema["function"]["parameters"]["properties"])
    assert "timeout_seconds" in model_properties
    assert REUSE_ARGS_PARAMETER_NAME in model_properties
    stripped_schema = {
        "type": model_schema["type"],
        "function": {
            **model_schema["function"],
            # 两个派生注入都不属于权威合同：先剥字段，再剥函数级那句。
            "description": "Full schema description.",
            "parameters": {
                **model_schema["function"]["parameters"],
                "properties": {
                    key: value
                    for key, value in model_properties.items()
                    if key not in {"timeout_seconds", REUSE_ARGS_PARAMETER_NAME}
                },
            },
        },
    }
    assert stripped_schema == tool.to_schema()


def test_tool_model_visible_schema_uses_override_without_changing_validation_contract() -> None:
    class _OverrideTool(Tool):
        @property
        def name(self) -> str:
            return "override_tool"

        @property
        def description(self) -> str:
            return "Authoritative execution contract."

        @property
        def parameters(self) -> dict[str, Any]:
            return {
                "type": "object",
                "properties": {
                    "full_value": {
                        "type": "string",
                        "description": "Authoritative parameter used for validation.",
                    }
                },
                "required": ["full_value"],
            }

        @property
        def model_description(self) -> str:
            return "Compact model-facing contract."

        @property
        def model_parameters(self) -> dict[str, Any]:
            return {
                "type": "object",
                "properties": {
                    "summary": {
                        "type": "string",
                        "description": "Compact model-visible field.",
                    }
                },
                "required": ["summary"],
            }

        async def execute(self, **kwargs: Any) -> Any:
            return kwargs

    tool = _OverrideTool()

    assert tool.to_schema()["function"]["description"] == "Authoritative execution contract."
    assert tool.to_schema()["function"]["parameters"]["required"] == ["full_value"]
    assert tool.to_model_schema()["function"]["description"] == (
        f"Compact model-facing contract. {REUSE_ARGS_MODEL_DESCRIPTION_SENTENCE}"
    )
    assert tool.to_model_schema()["function"]["parameters"]["required"] == ["summary"]
    assert tool.validate_params({"summary": "preview"}) == ["missing required full_value"]


def test_embedded_mcp_tool_prefers_handler_model_visible_contract() -> None:
    class _DynamicTool(Tool):
        @property
        def name(self) -> str:
            return "dynamic_tool"

        @property
        def description(self) -> str:
            return "Authoritative execution contract."

        @property
        def model_description(self) -> str:
            return "Runtime model-facing contract."

        @property
        def parameters(self) -> dict[str, Any]:
            return {
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
            }

        @property
        def model_parameters(self) -> dict[str, Any]:
            return {
                "type": "object",
                "properties": {"summary": {"type": "string"}},
                "required": ["summary"],
            }

        async def execute(self, **kwargs: Any) -> Any:
            return kwargs

    descriptor = ToolResourceDescriptor(
        kind=ResourceKind.TOOL,
        name="dynamic_tool",
        description="Manifest description should not win.",
        root=Path.cwd(),
        manifest_path=Path.cwd() / "resource.yaml",
        fingerprint="dynamic",
        parameters={
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
        },
    )

    tool = EmbeddedMCPTool(descriptor, _DynamicTool())

    assert tool.to_model_schema()["function"]["description"] == (
        f"Runtime model-facing contract. {REUSE_ARGS_MODEL_DESCRIPTION_SENTENCE}"
    )
    assert tool.to_model_schema()["function"]["parameters"]["required"] == ["summary"]


def test_tool_model_visible_schema_api_docs_call_out_non_authoritative_contract() -> None:
    assert Tool.parameters.__doc__ is not None
    assert Tool.model_parameters.__doc__ is not None
    assert "authoritative" in Tool.parameters.__doc__.lower()
    assert "not used for validation" in Tool.model_parameters.__doc__.lower()


@pytest.mark.asyncio
async def test_execution_first_turn_does_not_emit_all_visible_tool_schemas(tmp_path) -> None:
    requests: list[dict[str, object]] = []
    react_log_service = _FakeLogService()
    react_log_service.execution_stage_gate_snapshot = lambda task_id, node_id: {
        'has_active_stage': False,
        'transition_required': False,
        'active_stage': None,
    }

    class _Backend:
        async def chat(self, **kwargs):
            requests.append(dict(kwargs))
            frame = react_log_service.read_runtime_frame('task-first-turn-schemas', 'node-first-turn-schemas')
            assert frame.get('phase') == 'before_model'
            assert frame.get('callable_tool_names') == [
                'submit_final_result',
                'spawn_child_nodes',
                'stop_tool_execution',
            ]
            assert frame.get('model_visible_tool_names') == [
                'submit_final_result',
                'spawn_child_nodes',
                'stop_tool_execution',
            ]
            assert frame.get('model_visible_tool_selection_trace', {}).get('full_callable_tool_names') == [
                'submit_final_result',
                'spawn_child_nodes',
                'stop_tool_execution',
            ]
            # 钉头原文与 roster epoch 必须随这份 trace 一起落盘（载体缺失＝重启后那一跳只能重渲头部）。
            assert 'pinned_head' in frame.get('model_visible_tool_selection_trace', {})
            return LLMResponse(
                content='',
                tool_calls=[
                    ToolCallRequest(
                        id='call:final',
                        name='submit_final_result',
                        arguments={
                            'status': 'success',
                            'delivery_status': 'final',
                            'summary': 'done',
                            'answer': 'done',
                            'evidence': [],
                            'remaining_work': [],
                            'blocking_reason': '',
                        },
                    )
                ],
                finish_reason='tool_calls',
                usage={'input_tokens': 8, 'output_tokens': 3},
            )

    service = MainRuntimeService(
        chat_backend=_Backend(),
        workspace_root=tmp_path,
        store_path=tmp_path / "runtime.sqlite3",
        files_base_dir=tmp_path / "tasks",
        artifact_dir=tmp_path / "artifacts",
        governance_store_path=tmp_path / "governance.sqlite3",
        execution_model_refs=['fake'],
        acceptance_model_refs=['fake'],
    )
    service._react_loop._log_service = react_log_service
    service.store = SimpleNamespace(
        get_task=lambda task_id: SimpleNamespace(
            task_id=task_id,
            session_id='web:shared',
            metadata={'core_requirement': 'find terminal workflow skills'},
        ),
        get_node=lambda node_id: SimpleNamespace(
            node_id=node_id,
            prompt='find terminal workflow skills',
            goal='find terminal workflow skills',
            node_kind='execution',
        ),
    )
    service.execution_visible_tool_lightweight_items = lambda *, actor_role, session_id: [
        {
            'tool_id': 'filesystem_stat',
            'display_name': 'Filesystem Stat',
            'description': 'Inspect files',
            'l0': 'Inspect files',
            'l1': 'Inspect files',
        },
        {
            'tool_id': 'memory_update',
            'display_name': 'Memory Write',
            'description': 'Write memory facts',
            'l0': 'Write memory facts',
            'l1': 'Write memory facts',
        },
    ]

    result = await service._react_loop.run(
        task=SimpleNamespace(task_id='task-first-turn-schemas'),
        node=SimpleNamespace(node_id='node-first-turn-schemas', depth=0, node_kind='execution'),
        messages=[
            {'role': 'system', 'content': 'system'},
            {'role': 'user', 'content': '{"task_id":"task-first-turn-schemas","goal":"demo"}'},
        ],
        tools={
            'filesystem_stat': _ModelSchemaRecordingTool(
                name='filesystem_stat',
                authoritative_description='filesystem stat authoritative schema',
                model_description='filesystem stat compact model schema',
            ),
            'memory_update': _LargeModelSchemaTool(name='memory_update'),
            'stop_tool_execution': _StageProtocolNoopTool('stop_tool_execution'),
            'submit_final_result': _submit_final_result_tool(),
            'spawn_child_nodes': _StageProtocolNoopTool('spawn_child_nodes'),
        },
        model_refs=['fake'],
        runtime_context={
            'task_id': 'task-first-turn-schemas',
            'node_id': 'node-first-turn-schemas',
            'session_key': 'web:shared',
            'actor_role': 'execution',
        },
        max_iterations=2,
    )

    assert result.status == 'success'
    assert requests
    emitted_tools = list(requests[0].get('tools') or [])
    emitted_tool_names = [item['function']['name'] for item in emitted_tools]

    assert emitted_tool_names == [
        'filesystem_stat',
        'memory_update',
        'stop_tool_execution',
        'submit_final_result',
        'spawn_child_nodes',
    ]


@pytest.mark.asyncio
async def test_execution_root_replay_semantic_selection_includes_split_tools_without_budget_gate(tmp_path) -> None:
    requests: list[dict[str, object]] = []

    class _Backend:
        async def chat(self, **kwargs):
            requests.append(dict(kwargs))
            return LLMResponse(
                content='',
                tool_calls=[
                    ToolCallRequest(
                        id='call:final',
                        name='submit_final_result',
                        arguments={
                            'status': 'success',
                            'delivery_status': 'final',
                            'summary': 'done',
                            'answer': 'done',
                            'evidence': [],
                            'remaining_work': [],
                            'blocking_reason': '',
                        },
                    )
                ],
                finish_reason='tool_calls',
                usage={'input_tokens': 8, 'output_tokens': 3},
            )

    service = MainRuntimeService(
        chat_backend=_Backend(),
        workspace_root=tmp_path,
        store_path=tmp_path / "runtime.sqlite3",
        files_base_dir=tmp_path / "tasks",
        artifact_dir=tmp_path / "artifacts",
        governance_store_path=tmp_path / "governance.sqlite3",
        execution_model_refs=['fake'],
        acceptance_model_refs=['fake'],
    )
    fake_log_service = _FakeLogService()
    fake_log_service.upsert_frame(
        'task-root-replay-budget',
        {
            'node_id': 'node-root-replay-budget',
            # 上一跳钉住的声明有自己的字段；`model_visible_tool_names` 是当轮 callable 视图，
            # 拿它当清单源会让一次帧字段缺失就把 `tools[]` 收成 callable 大小（实盘 19↔20
            # 来回摆那批的形态），所以夹具两份都带上。
            'provider_tool_names': [
                'stop_tool_execution',
                'submit_final_result',
                'spawn_child_nodes',
                'load_tool_context_v2',
            ],
            'model_visible_tool_names': [
                'stop_tool_execution',
                'submit_final_result',
                'spawn_child_nodes',
                'load_tool_context_v2',
            ],
        },
    )
    fake_log_service.execution_stage_gate_snapshot = lambda task_id, node_id: {
        'has_active_stage': True,
        'transition_required': False,
        'active_stage': {'stage_id': 'stage-root-replay'},
    }
    service.log_service = fake_log_service
    service._react_loop._log_service = fake_log_service
    service.store = SimpleNamespace(
        get_task=lambda task_id: SimpleNamespace(
            task_id=task_id,
            session_id='web:shared',
            metadata={'core_requirement': 'inspect files and open relevant content for a React runtime issue'},
        ),
        get_node=lambda node_id: SimpleNamespace(
            node_id=node_id,
            prompt='inspect files and open relevant content for a React runtime issue',
            goal='inspect files and open relevant content for a React runtime issue',
            node_kind='execution',
        ),
    )
    service.execution_visible_tool_lightweight_items = lambda *, actor_role, session_id: [
        {
            'tool_id': 'filesystem_write',
            'display_name': 'Filesystem Write',
            'description': 'Write files',
            'l0': 'Write files',
            'l1': 'Write files',
        },
        {
            'tool_id': 'filesystem_edit',
            'display_name': 'Filesystem Edit',
            'description': 'Edit files',
            'l0': 'Edit files',
            'l1': 'Edit files',
        },
        {
            'tool_id': 'filesystem_propose_patch',
            'display_name': 'Filesystem Propose Patch',
            'description': 'Propose a patch',
            'l0': 'Propose a patch',
            'l1': 'Propose a patch',
        },
        {
            'tool_id': 'content_describe',
            'display_name': 'Content',
            'description': 'Inspect content',
            'l0': 'Inspect content',
            'l1': 'Inspect content',
        },
        {
            'tool_id': 'content_search',
            'display_name': 'Content',
            'description': 'Inspect content',
            'l0': 'Inspect content',
            'l1': 'Inspect content',
        },
        {
            'tool_id': 'content_open',
            'display_name': 'Content',
            'description': 'Inspect content',
            'l0': 'Inspect content',
            'l1': 'Inspect content',
        },
        {
            'tool_id': 'memory_update',
            'display_name': 'Memory',
            'description': 'Write memory facts',
            'l0': 'Write memory facts',
            'l1': 'Write memory facts',
        },
    ]

    result = await service._react_loop.run(
        task=SimpleNamespace(task_id='task-root-replay-budget'),
        node=SimpleNamespace(node_id='node-root-replay-budget', depth=0, node_kind='execution'),
        messages=[
            {'role': 'system', 'content': 'system'},
            {'role': 'user', 'content': '{"task_id":"task-root-replay-budget","goal":"demo"}'},
        ],
        tools={
            'filesystem_write': _ModelSchemaRecordingTool(
                name='filesystem_write',
                authoritative_description='filesystem write authoritative schema',
                model_description='filesystem write compact model schema',
            ),
            'filesystem_edit': _LargeModelSchemaTool(name='filesystem_edit'),
            'filesystem_propose_patch': _LargeModelSchemaTool(name='filesystem_propose_patch'),
            'content_describe': _ModelSchemaRecordingTool(
                name='content_describe',
                authoritative_description='content describe authoritative schema',
                model_description='content describe compact model schema',
            ),
            'content_search': _LargeModelSchemaTool(name='content_search'),
            'content_open': _LargeModelSchemaTool(name='content_open'),
            'memory_update': _LargeModelSchemaTool(name='memory_update'),
            'load_tool_context_v2': _ModelSchemaRecordingTool(
                name='load_tool_context_v2',
                authoritative_description='load tool context authoritative schema',
                model_description='load tool context compact model schema',
            ),
            'stop_tool_execution': _StageProtocolNoopTool('stop_tool_execution'),
            'submit_final_result': _submit_final_result_tool(),
            'spawn_child_nodes': _StageProtocolNoopTool('spawn_child_nodes'),
        },
        model_refs=['fake'],
        runtime_context={
            'task_id': 'task-root-replay-budget',
            'node_id': 'node-root-replay-budget',
            'session_key': 'web:shared',
            'actor_role': 'execution',
        },
        max_iterations=2,
    )

    assert result.status == 'success'
    assert requests
    emitted_tools = list(requests[0].get('tools') or [])
    emitted_tool_names = [item['function']['name'] for item in emitted_tools]
    assert 'submit_final_result' in emitted_tool_names
    assert 'submit_final_result' in emitted_tool_names
    assert 'spawn_child_nodes' in emitted_tool_names
    assert 'load_tool_context_v2' in emitted_tool_names
    # 族名不再是工具：旧族名既不进 callable，也不进发给模型的清单。
    assert 'filesystem' not in emitted_tool_names
    assert 'content' not in emitted_tool_names
    # 钉死：非压缩跳的 tools[] 与上一跳逐成员相同，候选名不并入——filesystem_write 等待
    # load 或冲动调用把契约带回来才走可调用面，参数契约由 load 返回承载；声明在压缩跳才吸收。
    assert emitted_tool_names == [
        'stop_tool_execution',
        'submit_final_result',
        'spawn_child_nodes',
        'load_tool_context_v2',
    ]
    assert 'filesystem_write' not in emitted_tool_names
    assert 'filesystem_edit' not in emitted_tool_names
    assert 'filesystem_propose_patch' not in emitted_tool_names
    assert 'content_describe' not in emitted_tool_names
    assert 'content_search' not in emitted_tool_names
    assert 'content_open' not in emitted_tool_names
    assert 'memory_update' not in emitted_tool_names


@pytest.mark.asyncio
async def test_react_loop_execution_watchdog_keeps_long_tool_inline() -> None:
    log_service = _FakeLogService()
    loop = ReActToolLoop(chat_backend=SimpleNamespace(), log_service=log_service)
    heartbeat = _HeartbeatRecorder()

    result = await loop._execute_tool_raw(
        tools={'slow_complete': _SlowCompleteTool()},
        tool_name='slow_complete',
        arguments={},
        runtime_context={
            'task_id': 'task-inline-watchdog',
            'node_id': 'node-inline-watchdog',
            'node_kind': 'execution',
            'actor_role': 'execution',
            'session_key': 'web:inline-watchdog',
            'loop': SimpleNamespace(web_session_heartbeat=heartbeat),
            'tool_watchdog': {
                'poll_interval_seconds': 0.01,
                'handoff_after_seconds': 0.03,
            },
            'tool_snapshot_supplier': lambda: {
                'status': 'running',
                'assistant_text': 'execution long tool should stay inline',
            },
        },
    )

    assert result == 'done'
    assert heartbeat.terminal_calls == []


@pytest.mark.asyncio
async def test_react_loop_acceptance_watchdog_keeps_long_tool_inline() -> None:
    log_service = _FakeLogService()
    loop = ReActToolLoop(chat_backend=SimpleNamespace(), log_service=log_service)
    heartbeat = _HeartbeatRecorder()

    result = await loop._execute_tool_raw(
        tools={'slow_complete': _SlowCompleteTool()},
        tool_name='slow_complete',
        arguments={},
        runtime_context={
            'task_id': 'task-inline-watchdog-acceptance',
            'node_id': 'node-inline-watchdog-acceptance',
            'node_kind': 'acceptance',
            'actor_role': 'acceptance',
            'session_key': 'web:inline-watchdog-acceptance',
            'loop': SimpleNamespace(web_session_heartbeat=heartbeat),
            'tool_watchdog': {
                'poll_interval_seconds': 0.01,
                'handoff_after_seconds': 0.03,
            },
            'tool_snapshot_supplier': lambda: {
                'status': 'running',
                'assistant_text': 'acceptance long tool should stay inline',
            },
        },
    )

    assert result == 'done'
    assert heartbeat.terminal_calls == []


def test_execution_selector_uses_stable_visible_tool_order_independent_of_iteration_order(
) -> None:
    visible_tools = {
        'filesystem_stat': _ModelSchemaRecordingTool(
            name='filesystem_stat',
            authoritative_description='filesystem stat authoritative schema',
            model_description='filesystem stat compact model schema ' + ('F' * 1200),
        ),
        'memory_update': _ModelSchemaRecordingTool(
            name='memory_update',
            authoritative_description='memory write authoritative schema',
            model_description='memory write compact model schema ' + ('M' * 800),
        ),
        'submit_final_result': _submit_final_result_tool(),
        'spawn_child_nodes': _StageProtocolNoopTool('spawn_child_nodes'),
    }
    def _service_for(items: list[dict[str, object]]) -> MainRuntimeService:
        service = object.__new__(MainRuntimeService)
        service.store = SimpleNamespace(
            get_task=lambda task_id: SimpleNamespace(
                task_id=task_id,
                session_id='web:shared',
                metadata={'core_requirement': 'find terminal workflow skills'},
            ),
            get_node=lambda node_id: SimpleNamespace(
                node_id=node_id,
                prompt='find terminal workflow skills',
                goal='find terminal workflow skills',
                node_kind='execution',
            ),
        )
        service.execution_visible_tool_lightweight_items = lambda *, actor_role, session_id: items
        return service

    filesystem_first = _service_for(
        [
            {
                'tool_id': 'filesystem_stat',
                'display_name': 'Filesystem Stat',
                'description': 'Inspect files',
                'l0': 'Inspect files',
                'l1': 'Inspect files',
            },
            {
                'tool_id': 'memory_update',
                'display_name': 'Memory Write',
                'description': 'Write memory facts',
                'l0': 'Write memory facts',
                'l1': 'Write memory facts',
            },
        ]
    )
    memory_first = _service_for(
        [
            {
                'tool_id': 'memory_update',
                'display_name': 'Memory Write',
                'description': 'Write memory facts',
                'l0': 'Write memory facts',
                'l1': 'Write memory facts',
            },
            {
                'tool_id': 'filesystem_stat',
                'display_name': 'Filesystem Stat',
                'description': 'Inspect files',
                'l0': 'Inspect files',
                'l1': 'Inspect files',
            },
        ]
    )

    filesystem_first_selection = filesystem_first._select_model_visible_tool_schema_payload(
        task_id='task-order-stable',
        node_id='node-order-stable',
        node_kind='execution',
        visible_tools=visible_tools,
        runtime_context={
            'task_id': 'task-order-stable',
            'node_id': 'node-order-stable',
            'session_key': 'web:shared',
            'actor_role': 'execution',
        },
    )
    memory_first_selection = memory_first._select_model_visible_tool_schema_payload(
        task_id='task-order-stable',
        node_id='node-order-stable',
        node_kind='execution',
        visible_tools=visible_tools,
        runtime_context={
            'task_id': 'task-order-stable',
            'node_id': 'node-order-stable',
            'session_key': 'web:shared',
            'actor_role': 'execution',
        },
    )

    assert filesystem_first_selection['tool_names'] == memory_first_selection['tool_names']
    assert filesystem_first_selection['tool_names'] == [
        'submit_final_result',
        'spawn_child_nodes',
    ]






class _FreePassStubTool(Tool):
    def __init__(self, name: str, result: object) -> None:
        self._name = name
        self._result = result

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return f'{self._name} stub tool'

    @property
    def parameters(self) -> dict[str, object]:
        return {'type': 'object', 'properties': {}, 'required': []}

    async def execute(self, **kwargs):
        _ = kwargs
        return self._result


def _stageless_free_pass_log_service(free_pass_rounds: list[dict[str, object]]) -> _FakeLogService:
    log_service = _FakeLogService()
    log_service.execution_stage_gate_snapshot = lambda task_id, node_id: {
        'has_active_stage': False,
        'transition_required': False,
        'active_stage': None,
        'pending_orphan_rounds': [],
    }

    def _record_free_pass_round(task_id, node_id, **kwargs):
        free_pass_rounds.append({'task_id': str(task_id), 'node_id': str(node_id), **kwargs})

    log_service.record_execution_stage_free_pass_round = _record_free_pass_round
    return log_service






def test_execution_selector_preserves_prior_model_visible_tool_order_across_turns() -> None:
    visible_tools = {
        'filesystem': _ModelSchemaRecordingTool(
            name='filesystem',
            authoritative_description='filesystem authoritative schema',
            model_description='filesystem compact model schema ' + ('F' * 1200),
        ),
        'spawn_child_nodes': _StageProtocolNoopTool('spawn_child_nodes'),
        'submit_final_result': _submit_final_result_tool(),
    }
    log_service = _FakeLogService()
    log_service.upsert_frame(
        'task-prior-order',
        {
            'node_id': 'node-prior-order',
            'model_visible_tool_names': [
                'submit_final_result',
                'spawn_child_nodes',
                'filesystem',
            ],
        },
    )
    log_service.execution_stage_prompt_payload = lambda task_id, node_id: {
        'has_active_stage': False,
        'transition_required': False,
        'active_stage': None,
    }
    service = object.__new__(MainRuntimeService)
    service.log_service = log_service
    service.store = SimpleNamespace(
        get_task=lambda task_id: SimpleNamespace(
            task_id=task_id,
            session_id='web:shared',
            metadata={'core_requirement': 'find terminal workflow skills'},
        ),
        get_node=lambda node_id: SimpleNamespace(
            node_id=node_id,
            prompt='find terminal workflow skills',
            goal='find terminal workflow skills',
            node_kind='execution',
        ),
    )
    service.execution_visible_tool_lightweight_items = lambda *, actor_role, session_id: [
        {
            'tool_id': 'filesystem',
            'display_name': 'Filesystem',
            'description': 'Inspect files',
            'l0': 'Inspect files',
            'l1': 'Inspect files',
        }
    ]

    selection = service._select_model_visible_tool_schema_payload(
        task_id='task-prior-order',
        node_id='node-prior-order',
        node_kind='execution',
        visible_tools=visible_tools,
        runtime_context={
            'task_id': 'task-prior-order',
            'node_id': 'node-prior-order',
            'session_key': 'web:shared',
            'actor_role': 'execution',
        },
    )

    assert selection['tool_names'] == [
        'submit_final_result',
        'spawn_child_nodes',
    ]
    assert selection['trace']['full_callable_tool_names'] == [
        'submit_final_result',
        'spawn_child_nodes',
    ]


def test_execution_selector_appends_missing_tools_after_prior_stable_prefix() -> None:
    visible_tools = {
        'filesystem': _ModelSchemaRecordingTool(
            name='filesystem',
            authoritative_description='filesystem authoritative schema',
            model_description='filesystem compact model schema ' + ('F' * 1200),
        ),
        'spawn_child_nodes': _StageProtocolNoopTool('spawn_child_nodes'),
        'submit_final_result': _submit_final_result_tool(),
    }
    log_service = _FakeLogService()
    log_service.upsert_frame(
        'task-partial-prior-order',
        {
            'node_id': 'node-partial-prior-order',
            'model_visible_tool_names': ['submit_final_result'],
        },
    )
    log_service.execution_stage_prompt_payload = lambda task_id, node_id: {
        'has_active_stage': False,
        'transition_required': False,
        'active_stage': None,
    }
    service = object.__new__(MainRuntimeService)
    service.log_service = log_service
    service.store = SimpleNamespace(
        get_task=lambda task_id: SimpleNamespace(
            task_id=task_id,
            session_id='web:shared',
            metadata={'core_requirement': 'find terminal workflow skills'},
        ),
        get_node=lambda node_id: SimpleNamespace(
            node_id=node_id,
            prompt='find terminal workflow skills',
            goal='find terminal workflow skills',
            node_kind='execution',
        ),
    )
    service.execution_visible_tool_lightweight_items = lambda *, actor_role, session_id: [
        {
            'tool_id': 'filesystem',
            'display_name': 'Filesystem',
            'description': 'Inspect files',
            'l0': 'Inspect files',
            'l1': 'Inspect files',
        }
    ]

    selection = service._select_model_visible_tool_schema_payload(
        task_id='task-partial-prior-order',
        node_id='node-partial-prior-order',
        node_kind='execution',
        visible_tools=visible_tools,
        runtime_context={
            'task_id': 'task-partial-prior-order',
            'node_id': 'node-partial-prior-order',
            'session_key': 'web:shared',
            'actor_role': 'execution',
        },
    )

    assert selection['tool_names'] == [
        'submit_final_result',
        'spawn_child_nodes',
    ]
    assert selection['trace']['full_callable_tool_names'] == [
        'submit_final_result',
        'spawn_child_nodes',
    ]


def test_execution_selector_keeps_full_callable_tools_when_transition_required() -> None:
    visible_tools = {
        'filesystem': _ModelSchemaRecordingTool(
            name='filesystem',
            authoritative_description='filesystem authoritative schema',
            model_description='filesystem compact model schema ' + ('F' * 1200),
        ),
        'spawn_child_nodes': _StageProtocolNoopTool('spawn_child_nodes'),
        'submit_final_result': _submit_final_result_tool(),
    }
    log_service = _FakeLogService()
    log_service.execution_stage_prompt_payload = lambda task_id, node_id: {
        'has_active_stage': True,
        'transition_required': True,
        'active_stage': {'stage_id': 'stage-exhausted'},
    }
    service = object.__new__(MainRuntimeService)
    service.log_service = log_service
    service.store = SimpleNamespace(
        get_task=lambda task_id: SimpleNamespace(
            task_id=task_id,
            session_id='web:shared',
            metadata={'core_requirement': 'find terminal workflow skills'},
        ),
        get_node=lambda node_id: SimpleNamespace(
            node_id=node_id,
            prompt='find terminal workflow skills',
            goal='find terminal workflow skills',
            node_kind='execution',
        ),
    )
    service.execution_visible_tool_lightweight_items = lambda *, actor_role, session_id: [
        {
            'tool_id': 'filesystem',
            'display_name': 'Filesystem',
            'description': 'Inspect files',
            'l0': 'Inspect files',
            'l1': 'Inspect files',
        }
    ]

    selection = service._select_model_visible_tool_schema_payload(
        task_id='task-transition-required',
        node_id='node-transition-required',
        node_kind='execution',
        visible_tools=visible_tools,
        runtime_context={
            'task_id': 'task-transition-required',
            'node_id': 'node-transition-required',
            'session_key': 'web:shared',
            'actor_role': 'execution',
        },
    )

    assert selection['tool_names'] == [
        'submit_final_result',
        'spawn_child_nodes',
    ]
    assert selection['trace']['full_callable_tool_names'] == [
        'submit_final_result',
        'spawn_child_nodes',
    ]


def test_load_tool_context_projection_reports_promotion_and_keeps_fingerprint_stable() -> None:
    """同一份 toolskill 正文，跨轮状态不同也必须算出同一个指纹。

    指纹是重复读守卫跨轮比对的身份。掺进 candidate/callable/will_be_hydrated 这类本轮视角，
    同一正文会算出多个指纹，守卫便永远拦不住重读（实盘：整窗口 0 次触发、同长度正文 2-4 个指纹）。
    另外 ok:true 必须自带"这次到底会不会提升"，不能只回一个治理层含义的可调用标志。
    """
    payload = {
        'ok': True,
        'tool_id': 'content_open',
        'content': '# content_open\nbody',
        'parameter_contract_markdown': 'path, start_line, end_line',
        'required_parameters': ['path'],
        'example_arguments': {'path': 'C:/x.txt'},
        'hydration_targets': ['content_open'],
        'callable': False,
        'available': True,
    }

    promotable = apply_runtime_tool_context_projection(
        dict(payload),
        requested_tool_id='content_open',
        runtime={
            'candidate_tool_names': ['content_open'],
            'callable_tool_names': ['exec'],
        },
    )
    already_callable = apply_runtime_tool_context_projection(
        dict(payload),
        requested_tool_id='content_open',
        runtime={
            'candidate_tool_names': [],
            'callable_tool_names': ['content_open'],
        },
    )
    hidden = apply_runtime_tool_context_projection(
        dict(payload),
        requested_tool_id='content_open',
        runtime={'candidate_tool_names': [], 'callable_tool_names': ['exec']},
    )

    assert promotable['promotion'] == 'promoted_next_turn'
    assert promotable['will_be_hydrated_next_turn'] is True
    assert promotable['callable_this_turn'] is False
    assert promotable['hydration_targets'] == ['content_open']

    assert already_callable['promotion'] == 'already_callable'
    assert already_callable['will_be_hydrated_next_turn'] is False
    assert already_callable['callable_this_turn'] is True
    assert already_callable['hydration_targets'] == []
    assert '无需再加载' in already_callable['promotion_help']

    # 第 4 态：加载被放行、提升被拒，就必须把"不会提升"讲明白，而不是留下一个沉默的 ok:true
    assert hidden['promotion'] == 'not_in_this_turn_candidates'
    assert hidden['hydration_targets'] == []
    assert hidden['callable_this_turn'] is False
    assert '不会提升' in hidden['promotion_help']

    assert promotable['tool_context_fingerprint'] == already_callable['tool_context_fingerprint']
    assert promotable['tool_context_fingerprint'] == hidden['tool_context_fingerprint']


def test_hydration_targets_reject_names_that_are_not_the_visible_tool() -> None:
    """一条记录就是一个工具：只有请求名与它同名才拿到提升承诺，旧族名/单体名一律不给。"""
    service = object.__new__(MainRuntimeService)
    visible_tool = SimpleNamespace(tool_id='content_open')

    assert service._tool_context_hydration_targets(
        requested_tool_id='content',
        visible_tool=visible_tool,
        actor_role='execution',
    ) == []
    assert service._tool_context_hydration_targets(
        requested_tool_id='content_navigation',
        visible_tool=visible_tool,
        actor_role='execution',
    ) == []
    assert service._tool_context_hydration_targets(
        requested_tool_id='content_open',
        visible_tool=visible_tool,
        actor_role='execution',
    ) == ['content_open']


def test_execution_selector_admits_contract_present_concrete_tools_and_ignores_stale_monolith_names() -> None:
    visible_tools = {
        'filesystem_write': _ModelSchemaRecordingTool(
            name='filesystem_write',
            authoritative_description='filesystem write authoritative schema',
            model_description='filesystem write compact model schema',
        ),
        'content_describe': _ModelSchemaRecordingTool(
            name='content_describe',
            authoritative_description='content describe authoritative schema',
            model_description='content describe compact model schema',
        ),
        'submit_final_result': _submit_final_result_tool(),
        'spawn_child_nodes': _StageProtocolNoopTool('spawn_child_nodes'),
    }

    log_service = _FakeLogService()
    service = object.__new__(MainRuntimeService)
    service.log_service = log_service
    service.store = SimpleNamespace(
        get_task=lambda task_id: SimpleNamespace(
            task_id=task_id,
            session_id='web:shared',
            metadata={'core_requirement': 'inspect files and content for a regression'},
        ),
        get_node=lambda node_id: SimpleNamespace(
            node_id=node_id,
            prompt='inspect files and content for a regression',
            goal='inspect files and content for a regression',
            node_kind='execution',
        ),
    )
    service.execution_visible_tool_lightweight_items = lambda *, actor_role, session_id: [
        {
            'tool_id': 'filesystem_write',
            'display_name': 'Filesystem Write',
            'description': 'Inspect files',
            'l0': 'Inspect files',
            'l1': 'Inspect files',
        },
        {
            'tool_id': 'content_describe',
            'display_name': 'Content Describe',
            'description': 'Inspect content',
            'l0': 'Inspect content',
            'l1': 'Inspect content',
        },
    ]

    first_selection = service._select_model_visible_tool_schema_payload(
        task_id='task-split-preference',
        node_id='node-split-preference',
        node_kind='execution',
        visible_tools=visible_tools,
        runtime_context={
            'task_id': 'task-split-preference',
            'node_id': 'node-split-preference',
            'session_key': 'web:shared',
            'actor_role': 'execution',
        },
    )

    assert 'filesystem_write' not in first_selection['tool_names']
    assert 'content_describe' not in first_selection['tool_names']
    assert 'content_describe' in first_selection['candidate_tool_names']

    log_service.upsert_frame(
        'task-split-preference',
        {
            'node_id': 'node-split-preference',
            'model_visible_tool_names': list(first_selection['tool_names']),
        },
    )

    # 下一跳：拆出来的具体工具的契约块进了请求视图（`tool_contract_presence` 是跳起点算好的
    # 在场索引）；视图里同时残留旧单体名/族名的块——它们不在治理可见集里，有块也不放行。
    present_selection = service._select_model_visible_tool_schema_payload(
        task_id='task-split-preference',
        node_id='node-split-preference',
        node_kind='execution',
        visible_tools=visible_tools,
        runtime_context={
            'task_id': 'task-split-preference',
            'node_id': 'node-split-preference',
            'session_key': 'web:shared',
            'actor_role': 'execution',
            'tool_contract_presence': {
                'filesystem_write': 'tcf:aaaaaaaaaaaaaaaa',
                'content_describe': 'tcf:bbbbbbbbbbbbbbbb',
                'filesystem': 'tcf:cccccccccccccccc',
                'content': 'tcf:dddddddddddddddd',
            },
        },
    )

    assert 'filesystem' not in present_selection['tool_names']
    assert 'content' not in present_selection['tool_names']
    assert 'filesystem_write' in present_selection['tool_names']
    assert 'content_describe' in present_selection['tool_names']
    assert present_selection['trace']['contract_present_tool_names'] == ['filesystem_write', 'content_describe']


def test_execution_selector_preserves_concrete_contract_present_tool_names_without_family_rewrite() -> None:
    visible_tools = {
        'task_list': _ModelSchemaRecordingTool(
            name='task_list',
            authoritative_description='task list authoritative schema',
            model_description='task list compact model schema',
        ),
        'submit_final_result': _submit_final_result_tool(),
        'spawn_child_nodes': _StageProtocolNoopTool('spawn_child_nodes'),
    }

    log_service = _FakeLogService()
    service = object.__new__(MainRuntimeService)
    service.log_service = log_service
    service.store = SimpleNamespace(
        get_task=lambda task_id: SimpleNamespace(
            task_id=task_id,
            session_id='web:shared',
            metadata={'core_requirement': 'inspect task tree'},
        ),
        get_node=lambda node_id: SimpleNamespace(
            node_id=node_id,
            prompt='inspect task tree',
            goal='inspect task tree',
            node_kind='execution',
        ),
    )
    service.execution_visible_tool_lightweight_items = lambda *, actor_role, session_id: [
        {
            'tool_id': 'task_list',
            'display_name': 'Task List',
            'description': 'Inspect task tree',
            'l0': 'Inspect task tree',
            'l1': 'Inspect task tree',
        }
    ]

    selection = service._select_model_visible_tool_schema_payload(
        task_id='task-concrete-presence',
        node_id='node-concrete-presence',
        node_kind='execution',
        visible_tools=visible_tools,
        runtime_context={
            'task_id': 'task-concrete-presence',
            'node_id': 'node-concrete-presence',
            'session_key': 'web:shared',
            'actor_role': 'execution',
            # 在场索引的键必须是具体工具 id：族名/单体名不在这个键空间里，选择器也不许把
            # 具体 id 折回族名。
            'tool_contract_presence': {'task_list': 'tcf:aaaaaaaaaaaaaaaa'},
        },
    )

    assert selection['tool_names'] == [
        'submit_final_result',
        'spawn_child_nodes',
        'task_list',
    ]
    assert selection['trace']['contract_present_tool_names'] == ['task_list']
    assert selection['trace']['full_callable_tool_names'] == [
        'submit_final_result',
        'spawn_child_nodes',
        'task_list',
    ]


def test_execution_selector_keeps_governance_visible_executors_promotable_after_bundle_collapse() -> None:
    """候选的上界是治理可见集，不是本轮已构建的对象字典。

    对象字典塌缩到只剩常驻工具时，若候选只按对象字典算，那些执行器会既不在 callable、也不在
    candidate：load 被提升门禁拒（not candidate_hit）、调用又被拒（tool not available），
    且文案给不出候选组——实盘里这条死区最长挂了 42 分钟。
    """
    service, visible_tools = _collapsed_bundle_selector_service()

    selection = service._select_model_visible_tool_schema_payload(
        task_id='task-collapse',
        node_id='node-collapse',
        node_kind='execution',
        visible_tools=visible_tools,
        runtime_context={
            'task_id': 'task-collapse',
            'node_id': 'node-collapse',
            'session_key': 'web:shared',
            'actor_role': 'execution',
            # 请求视图里残留着旧单体名的契约块：它既不是治理记录、也没有实例，三处都不该出现。
            'tool_contract_presence': {'content': 'tcf:cccccccccccccccc'},
        },
    )

    # 治理可见、契约不在场的执行器仍留在候选里，可以 load 回来
    assert 'content_describe' in selection['candidate_tool_names']
    assert 'content_search' in selection['candidate_tool_names']
    # callable 面不因治理可见而变宽；provider 清单是按角色钉住的说明书，本来就含这些名字
    assert 'content_describe' not in selection['tool_names']
    assert 'content_describe' in selection['provider_tool_names']
    # 旧单体名三处都不进：模型面只该看到治理里的具体工具
    assert 'content' not in selection['candidate_tool_names']
    assert 'content' not in selection['tool_names']
    assert 'content' not in selection['provider_tool_names']


def test_execution_provider_bundle_freezes_when_exposure_collapses() -> None:
    """非提交跳的清单只允许"补"，不允许"删"。

    清单排在整段上下文最前面，成员一变就把身后正文全部重铺。当轮曝光字典塌缩（候选重排、
    契约离场）都不构成删名的理由——删名要等到 token 压缩那一跳重印；而当轮真的可用的名字
    必须并进清单，否则模型手里只剩名字、没有参数表。
    """
    service, visible_tools = _collapsed_bundle_selector_service()
    service.log_service.upsert_frame(
        'task-pin',
        {
            'node_id': 'node-pin',
            'provider_tool_names': ['exec', 'content_describe', 'content_search'],
        },
    )

    selection = service._select_model_visible_tool_schema_payload(
        task_id='task-pin',
        node_id='node-pin',
        node_kind='execution',
        visible_tools=visible_tools,
        runtime_context={
            'task_id': 'task-pin',
            'node_id': 'node-pin',
            'session_key': 'web:shared',
            'actor_role': 'execution',
            # 视图里还留着旧单体名的契约块：补名单也不许把它带进清单
            'tool_contract_presence': {'content': 'tcf:cccccccccccccccc'},
        },
    )

    bundle = list(selection['provider_tool_names'] or [])
    assert selection['provider_tool_bundle_mode'] == 'pinned_frozen'
    # 上一跳声明过、本轮曝光里已经没有了 —— 仍然不能删
    assert 'content_describe' in bundle
    assert 'content_search' in bundle
    # 本轮常驻工具照常在场
    assert 'exec' in bundle
    # monolith 名不会因为"补"被带进来
    assert 'content' not in bundle


@pytest.mark.skipif(tools_bundle_single_source_enabled(), reason="单源化开关打开后这一档退役：它钉的是回填链的取数次序")
def test_execution_provider_bundle_keeps_revoked_tool_declared_and_flags_it() -> None:
    """RBAC 收回只该关掉执行准入，不该把名字从钉住的清单里摘走。

    清单排在上下文最前面，删名同样要重铺身后全部正文；而删名的真实成因是派发字典按
    当轮 RBAC 取数、渲不出参数表。所以声明侧必须自带一份不依赖派发字典的 schema，
    并把"已声明但本轮无权限"单独列出来交给尾块提醒模型。
    """
    service, visible_tools = _collapsed_bundle_selector_service()
    service._resource_manager = SimpleNamespace(
        tool_instances=lambda: {'web_fetch': _ModelSchemaRecordingTool(
            name='web_fetch',
            authoritative_description='fetch a page',
            model_description='fetch a page',
        )}
    )
    service.log_service.upsert_frame(
        'task-pin',
        {
            'node_id': 'node-pin',
            'provider_tool_names': ['exec', 'content_describe', 'web_fetch'],
        },
    )

    selection = service._select_model_visible_tool_schema_payload(
        task_id='task-pin',
        node_id='node-pin',
        node_kind='execution',
        visible_tools=visible_tools,
        runtime_context={
            'task_id': 'task-pin',
            'node_id': 'node-pin',
            'session_key': 'web:shared',
            'actor_role': 'execution',
        },
    )

    bundle = list(selection['provider_tool_names'] or [])
    assert selection['provider_tool_bundle_mode'] == 'pinned_frozen'
    # 本轮 RBAC 已不再把 web_fetch 给 execution，但名字必须还留在清单里
    assert 'web_fetch' not in list(selection['trace']['rbac_visible_tool_names'] or [])
    assert 'web_fetch' in bundle
    # 并且要说清它是"无权限"，不是"契约不在场"
    assert selection['declared_denied_tool_names'] == ['web_fetch']
    assert 'web_fetch' in selection['declared_denied_tool_schemas']
    assert selection['declared_denied_tool_schemas']['web_fetch']['function']['name'] == 'web_fetch'


@pytest.mark.skipif(tools_bundle_single_source_enabled(), reason="单源化开关打开后这一档退役：它钉的是回填链的取数次序")
def test_execution_provider_bundle_keeps_dispatch_absent_granted_tool_declared() -> None:
    """仍被授权、只是本轮没进派发字典的名字：参数表要补，尾块文案不写它。

    契约不在场把名字摘出派发字典是执行层的卫生动作；跟着把它从 `tools[]` 删掉等于在重印
    边界之外换束——清单排在正文最前面，删名会把它身后全部存活正文重铺一次。而模型真去调
    它拿到的是"先 load_tool_context"那一档，不是"无权限或已下架"，所以 `denied_tools`
    那份文案必须继续只装真失去授权的名字。
    """
    service, visible_tools = _collapsed_bundle_selector_service()
    service._resource_manager = SimpleNamespace(
        tool_instances=lambda: {'content_describe': _ModelSchemaRecordingTool(
            name='content_describe',
            authoritative_description='describe content',
            model_description='describe content',
        )}
    )
    service.log_service.upsert_frame(
        'task-pin',
        {
            'node_id': 'node-pin',
            'provider_tool_names': ['exec', 'content_describe'],
        },
    )

    selection = service._select_model_visible_tool_schema_payload(
        task_id='task-pin',
        node_id='node-pin',
        node_kind='execution',
        visible_tools=visible_tools,
        runtime_context={
            'task_id': 'task-pin',
            'node_id': 'node-pin',
            'session_key': 'web:shared',
            'actor_role': 'execution',
        },
    )

    bundle = list(selection['provider_tool_names'] or [])
    assert selection['provider_tool_bundle_mode'] == 'pinned_frozen'
    # 派发字典里没有 content_describe，但名字留在清单里，参数表从资源管理器补出来
    assert 'content_describe' not in visible_tools
    assert 'content_describe' in bundle
    assert selection['declared_denied_tool_schemas']['content_describe']['function']['name'] == 'content_describe'
    # 它没有被收回授权，所以不进"无权限/已下架"那份名单
    assert 'content_describe' in list(selection['trace']['rbac_visible_tool_names'] or [])
    assert selection['declared_denied_tool_names'] == []


@pytest.mark.skipif(tools_bundle_single_source_enabled(), reason="单源化开关打开后这一档退役：它钉的是回填链的取数次序")
def test_execution_provider_bundle_falls_back_to_last_sent_schema_bytes() -> None:
    """活实例与删除前快照都没有：用上一跳真正发出的原文兜住，名字照旧留在清单里。

    快照是进程内的、重启即清空，而重启后的第一跳仍带着上一跳钉住的清单；删名等于在重印
    边界之外换束。原文是逐字节的历史事实，复用它不断前缀。这份文案侧仍按"无权限或资源已
    下架"点名（该名字不在本角色治理可见集里），成员不动。
    """
    service, visible_tools = _collapsed_bundle_selector_service()
    service._resource_manager = SimpleNamespace(tool_instances=lambda: {})
    service.log_service.upsert_frame(
        'task-pin',
        {
            'node_id': 'node-pin',
            'provider_tool_names': ['exec', 'lane4-schema-tool'],
            'actual_request_ref': 'artifact:last-sent',
        },
    )
    service.log_service.resolve_content_ref = lambda ref: json.dumps(
        {
            'actual_tool_schemas': [
                {'type': 'function', 'function': {'name': 'lane4-schema-tool', 'parameters': {}}},
            ]
        }
    )

    selection = service._select_model_visible_tool_schema_payload(
        task_id='task-pin',
        node_id='node-pin',
        node_kind='execution',
        visible_tools=visible_tools,
        runtime_context={
            'task_id': 'task-pin',
            'node_id': 'node-pin',
            'session_key': 'web:shared',
            'actor_role': 'execution',
        },
    )

    bundle = list(selection['provider_tool_names'] or [])
    assert 'lane4-schema-tool' in bundle
    assert selection['declared_denied_tool_schemas']['lane4-schema-tool']['function']['name'] == 'lane4-schema-tool'
    assert 'lane4-schema-tool' in list(selection['declared_denied_tool_names'] or [])
    # 原文取到的那份要落进载体，下一跳不必再读盘
    assert selection['trace']['declaration_schema_backfill']['lane4-schema-tool']['function']['name'] == 'lane4-schema-tool'


@pytest.mark.skipif(tools_bundle_single_source_enabled(), reason="单源化开关打开后这一档退役：它钉的是回填链的取数次序")
def test_execution_provider_bundle_reads_the_carrier_before_the_artifact() -> None:
    """载体（帧里的 selection trace）优先于工件：快照空了也不该再付一次读盘。

    载体装在 `model_visible_tool_selection_trace` 里是刻意的——那个 dict 已经整份持久化；
    新开帧字段要跟七个归一化写点，漏一个就是"存了等于没存"。
    """
    carried = {'lane4-schema-tool': {'type': 'function', 'function': {'name': 'lane4-schema-tool', 'parameters': {}}}}
    service, visible_tools = _collapsed_bundle_selector_service()
    service._resource_manager = SimpleNamespace(tool_instances=lambda: {})
    service.log_service.upsert_frame(
        'task-pin',
        {
            'node_id': 'node-pin',
            'provider_tool_names': ['exec', 'lane4-schema-tool'],
            'actual_request_ref': 'artifact:last-sent',
            'model_visible_tool_selection_trace': {'declaration_schema_backfill': carried},
        },
    )

    def _fail_resolve(ref: str) -> str:
        raise AssertionError('carrier hit must not read the artifact')

    service.log_service.resolve_content_ref = _fail_resolve

    selection = service._select_model_visible_tool_schema_payload(
        task_id='task-pin',
        node_id='node-pin',
        node_kind='execution',
        visible_tools=visible_tools,
        runtime_context={
            'task_id': 'task-pin',
            'node_id': 'node-pin',
            'session_key': 'web:shared',
            'actor_role': 'execution',
        },
    )

    assert 'lane4-schema-tool' in list(selection['provider_tool_names'] or [])
    assert selection['declared_denied_tool_schemas'] == carried
    # 用过之后继续带着，否则下一跳又只剩读盘
    assert selection['trace']['declaration_schema_backfill'] == carried


@pytest.mark.skipif(tools_bundle_single_source_enabled(), reason="单源化开关打开后这一档退役：它钉的是回填链的取数次序")
def test_execution_provider_bundle_last_sent_tier_needs_a_prior_artifact_ref() -> None:
    """没有上一跳引用、也没有整表引用时不读盘、不炸；名字留住，参数表明示没带出来。

    这条原来断言"渲不出就退场"。判据改成不许退场：删名与占位的断点位置完全相同（都从这一条
    的起始字节起断），所以占位不省缓存，省的是语义——清单成员只在重印边界变。
    """
    service, visible_tools = _collapsed_bundle_selector_service()
    service._resource_manager = SimpleNamespace(tool_instances=lambda: {})
    service.log_service.upsert_frame(
        'task-pin',
        {'node_id': 'node-pin', 'provider_tool_names': ['exec', 'lane4-schema-tool']},
    )

    def _fail_resolve(ref: str) -> str:
        raise AssertionError('must not read the artifact without a ref')

    service.log_service.resolve_content_ref = _fail_resolve

    selection = service._select_model_visible_tool_schema_payload(
        task_id='task-pin',
        node_id='node-pin',
        node_kind='execution',
        visible_tools=visible_tools,
        runtime_context={
            'task_id': 'task-pin',
            'node_id': 'node-pin',
            'session_key': 'web:shared',
            'actor_role': 'execution',
        },
    )

    assert 'lane4-schema-tool' in list(selection['provider_tool_names'] or [])
    placeholder = selection['declared_denied_tool_schemas']['lane4-schema-tool']
    assert placeholder['function']['name'] == 'lane4-schema-tool'
    assert placeholder['function']['parameters'] == {'type': 'object', 'properties': {}}
    assert selection['trace']['declaration_schema_placeholders'] == ['lane4-schema-tool']


@pytest.mark.skipif(tools_bundle_single_source_enabled(), reason="单源化开关打开后这一档退役：它钉的是回填链的取数次序")
def test_execution_provider_bundle_reads_the_seed_bundle_before_the_last_sent_artifact() -> None:
    """整表引用排在"上一跳原文"之前：那份按 kind 被逐跳剪，剪掉的一刻第五档就空了。

    第三/第四档（进程内快照、逐名载体）都空时，先读清单变化那跳存下的 singleton 整表；
    只有它也没有，才回去读上一跳原文。
    """
    bundled = {'type': 'function', 'function': {'name': 'lane5-bundle-tool', 'parameters': {'type': 'object'}}}
    service, visible_tools = _collapsed_bundle_selector_service()
    service._resource_manager = SimpleNamespace(tool_instances=lambda: {})
    service.log_service.upsert_frame(
        'task-pin',
        {
            'node_id': 'node-pin',
            'provider_tool_names': ['exec', 'lane5-bundle-tool'],
            'actual_request_ref': 'artifact:last-sent',
            'model_visible_tool_selection_trace': {'declaration_schema_ref': 'artifact:bundle-1'},
        },
    )

    def _resolve(ref: str) -> str:
        if str(ref or '') == 'artifact:bundle-1':
            return json.dumps({'tool_schemas': [bundled]})
        raise AssertionError('bundle hit must not read the last-sent artifact')

    service.log_service.resolve_content_ref = _resolve

    selection = service._select_model_visible_tool_schema_payload(
        task_id='task-pin',
        node_id='node-pin',
        node_kind='execution',
        visible_tools=visible_tools,
        runtime_context={
            'task_id': 'task-pin',
            'node_id': 'node-pin',
            'session_key': 'web:shared',
            'actor_role': 'execution',
        },
    )

    assert 'lane5-bundle-tool' in list(selection['provider_tool_names'] or [])
    assert selection['declared_denied_tool_schemas']['lane5-bundle-tool'] == bundled
    # 用过之后继续带着，否则下一跳又只剩读盘
    assert selection['trace']['declaration_schema_backfill'] == {'lane5-bundle-tool': bundled}
    assert selection['trace']['declaration_schema_placeholders'] == []


@pytest.mark.skipif(tools_bundle_single_source_enabled(), reason="单源化开关打开后这一档退役：它钉的是回填链的取数次序")
def test_execution_provider_bundle_rewrites_the_seed_bundle_only_when_the_list_changes() -> None:
    """整表只在名单变化的那一跳重写；冻结跳一次 IO 都不加，且引用原样带下去。"""
    writes: list[dict] = []

    class _FakeArtifactStore:
        def create_or_replace_singleton_text_artifact(self, **kwargs):
            writes.append(kwargs)
            return SimpleNamespace(artifact_id='bundle-new')

    service, visible_tools = _collapsed_bundle_selector_service()
    service._resource_manager = SimpleNamespace(tool_instances=lambda: {})
    service.artifact_store = _FakeArtifactStore()
    service.log_service.upsert_frame(
        'task-pin',
        {
            'node_id': 'node-pin',
            'provider_tool_names': ['exec', 'content_describe'],
            'model_visible_tool_selection_trace': {'declaration_schema_ref': 'artifact:bundle-old'},
        },
    )

    frozen = service._select_model_visible_tool_schema_payload(
        task_id='task-pin',
        node_id='node-pin',
        node_kind='execution',
        visible_tools=visible_tools,
        runtime_context={
            'task_id': 'task-pin',
            'node_id': 'node-pin',
            'session_key': 'web:shared',
            'actor_role': 'execution',
        },
    )

    assert frozen['provider_tool_bundle_mode'] == 'pinned_frozen'
    assert writes == []
    # 没重写也要把上一份引用带着，否则下一跳的第五档凭空消失
    assert frozen['trace']['declaration_schema_ref'] == 'artifact:bundle-old'

    service.log_service.upsert_frame(
        'task-pin',
        {'node_id': 'node-pin'},  # 无钉住声明＝这一跳重播种
    )
    seeded = service._select_model_visible_tool_schema_payload(
        task_id='task-pin',
        node_id='node-pin',
        node_kind='execution',
        visible_tools=visible_tools,
        runtime_context={
            'task_id': 'task-pin',
            'node_id': 'node-pin',
            'session_key': 'web:shared',
            'actor_role': 'execution',
        },
    )

    assert seeded['provider_tool_bundle_mode'] == 'pinned_seeded'
    assert len(writes) == 1
    assert seeded['trace']['declaration_schema_ref'] == 'artifact:bundle-new'
    assert seeded['trace']['declaration_schema_ref_fault'] == 0


def test_execution_provider_bundle_ref_is_not_double_prefixed() -> None:
    """存储侧交回的 id 本身已带 `artifact:`，写引用时不许再拼一层前缀。

    实盘抓到过一次：帧里落的是 `artifact:artifact:0a4aa582fc07`。旧格式仍解析得到，
    所以这一格不会立刻报错——正是这类"看着没事、等兼容层哪天收掉就断"的形状要钉住。
    """
    class _NamespacedIdStore:
        def create_or_replace_singleton_text_artifact(self, **kwargs):
            return SimpleNamespace(artifact_id='artifact:0a4aa582fc07')

    service, visible_tools = _collapsed_bundle_selector_service()
    service._resource_manager = SimpleNamespace(tool_instances=lambda: {})
    service.artifact_store = _NamespacedIdStore()
    service.log_service.upsert_frame('task-pin', {'node_id': 'node-pin'})

    selection = service._select_model_visible_tool_schema_payload(
        task_id='task-pin',
        node_id='node-pin',
        node_kind='execution',
        visible_tools=visible_tools,
        runtime_context={
            'task_id': 'task-pin',
            'node_id': 'node-pin',
            'session_key': 'web:shared',
            'actor_role': 'execution',
        },
    )

    assert selection['trace']['declaration_schema_ref'] == 'artifact:0a4aa582fc07'


def test_ruler_identity_uses_predicted_member_not_chain_head() -> None:
    """usage-first 的身份锚点按"这一发落到的那颗"判；链首在异构组里是错锚点。

    实盘形态：组 v4 链首那颗的回执名是 deepseek-v4-flash，节点被粘在 glm-5.2-2（回执
    sensenova-6.8-flash-lite）上，旧判据 53/53 跳判否，尺子永久停在 preview 全量重估。
    """
    match = ReActToolLoop._ruler_identity_matches_previous

    # 有预测：按预测那颗判，与链首是谁无关
    assert match(
        previous_provider_model='sensenova-6.8-flash-lite',
        predicted_provider_model='openai:sensenova-6.8-flash-lite',
        chain_provider_model='openai:deepseek-v4-flash',
    ) is True
    # 预测到的是另一颗 ⇒ 真换了成员，锚点这次该停用
    assert match(
        previous_provider_model='sensenova-6.8-flash-lite',
        predicted_provider_model='openai:deepseek-v4-flash',
        chain_provider_model='openai:deepseek-v4-flash',
    ) is False
    # 没预测到成员、链首名又对不上：按上一跳回执继续用锚点（重绑那跳本来就是一次合法 miss）
    assert match(
        previous_provider_model='sensenova-6.8-flash-lite',
        predicted_provider_model='',
        chain_provider_model='openai:deepseek-v4-flash',
    ) is True
    # 首跳没有上一跳回执：不启用锚点，走 preview
    assert match(
        previous_provider_model='',
        predicted_provider_model='openai:sensenova-6.8-flash-lite',
        chain_provider_model='openai:sensenova-6.8-flash-lite',
    ) is False


def test_peek_hop_ruler_carries_the_predicted_member_key() -> None:
    """预测口把成员名一起带出来，尺子的身份判据才有得用。"""

    class _Controller:
        def peek_hop_windows(self, *, node_id: str, route_plan):  # noqa: ANN001
            return {'group_key': 'v4', 'model_key': 'glm-5.2-2', 'min_window': 390000}

    loop = ReActToolLoop(chat_backend=object(), log_service=_FakeLogService(), max_iterations=1)
    loop._node_turn_controller = _Controller()

    ruler = loop._peek_hop_ruler(node_id='node-ruler', route_plan=object())

    assert ruler == {'windows': [390000], 'source': 'group:v4', 'model_key': 'glm-5.2-2'}


def test_react_iteration_bundle_filter_keys_on_renderable_schema_not_dispatch_dict() -> None:
    """组装侧的存留判据取"渲不渲得出参数表"，不是"本轮有没有派发资格"。"""
    exec_tool = _ModelSchemaRecordingTool(name='exec', authoritative_description='exec', model_description='exec')
    declared_schemas = {
        'content_open': {'type': 'function', 'function': {'name': 'content_open', 'parameters': {}}},
    }

    def _selection(*, schemas: dict[str, object]) -> dict[str, object]:
        return {
            'tool_names': ['exec'],
            'provider_tool_names': ['exec', 'content_open'],
            'pinned_provider_tool_names': ['exec'],
            'provider_tool_bundle_mode': 'pinned_frozen',
            'declared_denied_tool_names': [],
            'declared_denied_tool_schemas': schemas,
            'trace': {},
        }

    loop = ReActToolLoop(chat_backend=object(), log_service=_FakeLogService(), max_iterations=1)
    loop._model_visible_tool_schema_selector = lambda **kwargs: _selection(schemas=declared_schemas)

    _selected, payload = loop._model_visible_tools_for_iteration(
        task_id='task-render',
        node_id='node-render',
        node_kind='execution',
        visible_tools={'exec': exec_tool},

        runtime_context={},
    )
    assert payload['provider_tool_names'] == ['exec', 'content_open']
    assert payload['provider_tool_bundle_mode'] == 'pinned_frozen'

    # 补不出参数表（实例没了又没有快照）时，名字照旧离开清单
    loop._model_visible_tool_schema_selector = lambda **kwargs: _selection(schemas={})
    _selected_empty, payload_empty = loop._model_visible_tools_for_iteration(
        task_id='task-render',
        node_id='node-render',
        node_kind='execution',
        visible_tools={'exec': exec_tool},

        runtime_context={},
    )
    assert payload_empty['provider_tool_names'] == ['exec']


def _collapsed_bundle_selector_service():
    visible_tools = {
        'submit_final_result': _submit_final_result_tool(),
        'exec': _ModelSchemaRecordingTool(name='exec', authoritative_description='exec', model_description='exec'),
        'load_tool_context': _ModelSchemaRecordingTool(
            name='load_tool_context',
            authoritative_description='load tool context',
            model_description='load tool context',
        ),
        'load_skill_context': _ModelSchemaRecordingTool(
            name='load_skill_context',
            authoritative_description='load skill context',
            model_description='load skill context',
        ),
    }
    service = object.__new__(MainRuntimeService)
    service.log_service = _FakeLogService()
    service.store = SimpleNamespace(
        get_task=lambda task_id: SimpleNamespace(
            task_id=task_id,
            session_id='web:shared',
            metadata={'core_requirement': 'read files and content for a regression'},
        ),
        get_node=lambda node_id: SimpleNamespace(
            node_id=node_id,
            prompt='read files and content for a regression',
            goal='read files and content for a regression',
            node_kind='execution',
        ),
    )
    service.list_effective_tool_names = lambda *, actor_role, session_id: [
        'submit_final_result',
        'exec',
        'load_tool_context',
        'load_skill_context',
        'content_describe',
        'content_search',
    ]
    service.execution_visible_tool_lightweight_items = lambda *, actor_role, session_id: [
        {
            'tool_id': 'content_describe',
            'display_name': 'Content Describe',
            'description': 'Inspect content',
            'l0': 'Inspect content',
            'l1': 'Inspect content',
        },
        {
            'tool_id': 'content_search',
            'display_name': 'Content Search',
            'description': 'Search content',
            'l0': 'Search content',
            'l1': 'Search content',
        },
        {
            'tool_id': 'exec',
            'display_name': 'Exec',
            'description': 'Run commands',
            'l0': 'Run commands',
            'l1': 'Run commands',
        },
    ]
    return service, visible_tools


def test_tool_provider_covers_governance_visible_tools_when_frame_candidates_are_empty() -> None:
    """回灌锁：frame 候选被写成空时，对象字典仍覆盖治理可见执行器。

    只修候选派生不修这里，下一轮 restore 仍会把窄候选读回对象字典，闭环依旧自放大。
    """
    service = object.__new__(MainRuntimeService)
    service.store = SimpleNamespace(
        get_task=lambda task_id: SimpleNamespace(task_id=task_id, session_id='web:shared', metadata={}),
    )
    service._resource_manager = SimpleNamespace(
        tool_instances=lambda: {
            'content_open': _ModelSchemaRecordingTool(
                name='content_open',
                authoritative_description='content open authoritative schema',
                model_description='content open model schema',
            ),
            'content_search': _ModelSchemaRecordingTool(
                name='content_search',
                authoritative_description='content search authoritative schema',
                model_description='content search model schema',
            ),
            'content': _ModelSchemaRecordingTool(
                name='content',
                authoritative_description='content legacy schema',
                model_description='content legacy schema',
            ),
        }
    )
    service._builtin_tool_cache = None
    service._node_context_selection_cache = {
        ('task-empty-cand', 'node-empty-cand'): {
            'selection': NodeContextSelectionResult(
                selected_tool_names=[],
                candidate_tool_names=[],
            )
        }
    }
    service.log_service = _FakeLogService()
    service.log_service.upsert_frame(
        'task-empty-cand',
        {'node_id': 'node-empty-cand', 'candidate_tool_names': []},
    )
    # 资源目录里还留着已删除的旧单体实例，但它不再是治理可见工具。
    service.list_effective_tool_names = lambda *, actor_role, session_id: ['content_open', 'content_search']
    service._actor_role_for_node = lambda node: 'execution'

    provided = service._tool_provider(
        SimpleNamespace(task_id='task-empty-cand', node_id='node-empty-cand', node_kind='execution', can_spawn_children=False)
    )

    assert 'content_open' in provided
    assert 'content_search' in provided
    # 不在治理可见集里的名字（旧单体残留实例）拿不到派发资格
    assert 'content' not in provided


def test_tool_provider_includes_candidate_tools_from_restored_selection() -> None:
    service = object.__new__(MainRuntimeService)
    service.store = SimpleNamespace(
        get_task=lambda task_id: SimpleNamespace(task_id=task_id, session_id='web:shared', metadata={}),
    )
    service._resource_manager = SimpleNamespace(
        tool_instances=lambda: {
            'content_open': _ModelSchemaRecordingTool(
                name='content_open',
                authoritative_description='content open authoritative schema',
                model_description='content open model schema',
            ),
            'content_search': _ModelSchemaRecordingTool(
                name='content_search',
                authoritative_description='content search authoritative schema',
                model_description='content search model schema',
            ),
        }
    )
    service._builtin_tool_cache = None
    service._node_context_selection_cache = {
        ('task-restored-provider', 'node-restored-provider'): {
            'selection': NodeContextSelectionResult(
                selected_tool_names=['load_tool_context'],
                candidate_tool_names=['content_open', 'content_search'],
            )
        }
    }
    service.log_service = _FakeLogService()
    service.list_effective_tool_names = (
        lambda *, actor_role, session_id: ['load_tool_context', 'content_open', 'content_search']
    )
    service.list_visible_tools = lambda *, actor_role, session_id: []
    service._actor_role_for_node = lambda node: 'execution'

    node = SimpleNamespace(
        task_id='task-restored-provider',
        node_id='node-restored-provider',
        node_kind='execution',
        can_spawn_children=False,
    )
    provided = service._tool_provider(node)

    assert 'content_open' in provided
    assert 'content_search' in provided


def test_tool_provider_restores_candidates_from_frame_when_cache_was_cleared() -> None:
    service = object.__new__(MainRuntimeService)
    service.store = SimpleNamespace(
        get_task=lambda task_id: SimpleNamespace(task_id=task_id, session_id='web:shared', metadata={}),
    )
    service._resource_manager = SimpleNamespace(
        tool_instances=lambda: {
            'content_open': _ModelSchemaRecordingTool(
                name='content_open',
                authoritative_description='content open authoritative schema',
                model_description='content open model schema',
            ),
            'content_search': _ModelSchemaRecordingTool(
                name='content_search',
                authoritative_description='content search authoritative schema',
                model_description='content search model schema',
            ),
        }
    )
    service._builtin_tool_cache = None
    service._node_context_selection_cache = {}
    service.log_service = _FakeLogService()
    service.log_service.upsert_frame(
        'task-cache-miss-provider',
        {
            'node_id': 'node-cache-miss-provider',
            'callable_tool_names': ['load_tool_context', 'content_open'],
            'candidate_tool_names': ['content_search'],
            'selected_skill_ids': [],
            'candidate_skill_ids': [],
            'rbac_visible_tool_names': ['load_tool_context', 'content_open', 'content_search'],
            'rbac_visible_skill_ids': [],
            'messages': [
                {'role': 'system', 'content': 'system'},
                {'role': 'user', 'content': '{"prompt":"verify framework"}'},
            ],
        },
    )
    service.list_effective_tool_names = (
        lambda *, actor_role, session_id: ['load_tool_context', 'content_open', 'content_search']
    )
    service.list_contract_visible_skill_resources = lambda *, actor_role, session_id: []
    service.list_visible_tools = lambda *, actor_role, session_id: []
    service._actor_role_for_node = lambda node: 'inspection'

    node = SimpleNamespace(
        task_id='task-cache-miss-provider',
        node_id='node-cache-miss-provider',
        node_kind='acceptance',
        can_spawn_children=False,
    )

    provided = service._tool_provider(node)

    assert 'content_open' in provided
    assert 'content_search' in provided


def test_execution_selector_web_research_query_does_not_directly_promote_candidates() -> None:
    visible_tools = {
        'web_fetch': _ModelSchemaRecordingTool(
            name='web_fetch',
            authoritative_description='web_fetch authoritative schema',
            model_description='web_fetch compact model schema',
        ),
        'agent_browser': _ModelSchemaRecordingTool(
            name='agent_browser',
            authoritative_description='agent_browser authoritative schema',
            model_description='agent_browser compact model schema',
        ),
        'submit_final_result': _submit_final_result_tool(),
        'spawn_child_nodes': _StageProtocolNoopTool('spawn_child_nodes'),
    }

    service = object.__new__(MainRuntimeService)
    service.log_service = _FakeLogService()
    service.store = SimpleNamespace(
        get_task=lambda task_id: SimpleNamespace(
            task_id=task_id,
            session_id='web:shared',
            metadata={'core_requirement': 'collect public web sources and URLs for ranking research'},
        ),
        get_node=lambda node_id: SimpleNamespace(
            node_id=node_id,
            prompt='search the web for official source URLs and ranking pages',
            goal='collect public web sources for character rankings',
            node_kind='execution',
        ),
    )
    service.execution_visible_tool_lightweight_items = lambda *, actor_role, session_id: [
        {
            'tool_id': 'web_fetch',
            'display_name': 'Web Fetch',
            'description': 'Fetch public web pages',
            'l0': 'Fetch public web pages',
            'l1': 'Fetch public web pages',
        },
        {
            'tool_id': 'agent_browser',
            'display_name': 'Agent Browser',
            'description': 'Browse public web pages',
            'l0': 'Browse public web pages',
            'l1': 'Browse public web pages',
        },
    ]

    selection = service._select_model_visible_tool_schema_payload(
        task_id='task-web-research',
        node_id='node-web-research',
        node_kind='execution',
        visible_tools=visible_tools,
        runtime_context={
            'task_id': 'task-web-research',
            'node_id': 'node-web-research',
            'session_key': 'web:shared',
            'actor_role': 'execution',
        },
    )

    assert selection['tool_names'] == [
        'submit_final_result',
        'spawn_child_nodes',
    ]


def test_prepare_messages_rebuilds_prompt_from_completed_stages_and_active_window() -> None:
    loop = ReActToolLoop(chat_backend=SimpleNamespace(), log_service=_FakeLogService(), max_iterations=2)
    loop._log_service._store._node = SimpleNamespace(
        metadata={
            "execution_stages": {
                "stages": [
                    {
                        "mode": "自主执行",
                        "status": "完成",
                        "key_refs": [
                            {
                                "ref": "artifact:artifact:stage-one",
                                "note": "stage one evidence summary",
                            }
                        ],
                    },
                    {
                        "mode": "自主执行",
                        "status": "进行中",
                    },
                ],
            }
        }
    )
    original = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": '{"task_id":"task-1","goal":"demo"}'},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call-stage-1",
                    "type": "function",
                    "function": {"name": "filesystem_stat", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "name": "filesystem_stat", "tool_call_id": "call-stage-1", "content": '{"ok": true}'},
        {"role": "assistant", "content": "stage one raw detail"},
        {
            "role": "tool",
            "name": "content",
            "tool_call_id": "call-stage-archive",
            "content": "archived stage history excerpt",
            "ephemeral": True,
        },
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call-stage-2",
                    "type": "function",
                    "function": {"name": "filesystem_stat", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "name": "filesystem_stat", "tool_call_id": "call-stage-2", "content": '{"ok": true}'},
        {"role": "assistant", "content": "current stage assistant detail"},
        {"role": "tool", "name": "filesystem", "tool_call_id": "call-a", "content": "current stage tool output"},
    ]

    prepared = loop._prepare_messages(original, runtime_context={"task_id": "task-1", "node_id": "node-1"})

    assert prepared == original
    rendered_contents = [str(item.get("content") or "") for item in prepared]
    assert "stage one raw detail" in rendered_contents
    assert "archived stage history excerpt" in rendered_contents
    assert "current stage assistant detail" in rendered_contents
    assert "current stage tool output" in rendered_contents




def test_prepare_messages_is_idempotent_for_compacted_stage_prompt() -> None:
    loop = ReActToolLoop(chat_backend=SimpleNamespace(), log_service=_FakeLogService(), max_iterations=2)
    loop._log_service._store._node = SimpleNamespace(
        metadata={
            "execution_stages": {
                "stages": [
                    {
                        "mode": "自主执行",
                        "status": "完成",
                    },
                    {
                        "mode": "自主执行",
                        "status": "进行中",
                    },
                ],
            }
        }
    )
    original = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": '{"task_id":"task-1","goal":"demo"}'},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call-stage-1",
                    "type": "function",
                    "function": {"name": "filesystem_stat", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "name": "filesystem_stat", "tool_call_id": "call-stage-1", "content": '{"ok": true}'},
        {"role": "assistant", "content": "stage one raw detail"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call-stage-2",
                    "type": "function",
                    "function": {"name": "filesystem_stat", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "name": "filesystem_stat", "tool_call_id": "call-stage-2", "content": '{"ok": true}'},
        {"role": "assistant", "content": "current stage assistant detail"},
        {"role": "tool", "name": "filesystem", "tool_call_id": "call-a", "content": "current stage tool output"},
    ]

    prepared = loop._prepare_messages(original, runtime_context={"task_id": "task-1", "node_id": "node-1"})
    prepared_again = loop._prepare_messages(prepared, runtime_context={"task_id": "task-1", "node_id": "node-1"})

    assert prepared_again == prepared








@pytest.mark.asyncio
async def test_react_loop_orphan_tool_result_circuit_breaker_fails_current_node() -> None:
    calls: list[list[dict[str, object]]] = []

    class _Backend:
        async def chat(self, **kwargs):
            message_batch = [dict(item) for item in list(kwargs.get("messages") or [])]
            calls.append(message_batch)
            return LLMResponse(
                content="not json",
                tool_calls=[],
                finish_reason="stop",
                usage={"input_tokens": 8, "output_tokens": 3},
            )

    loop = ReActToolLoop(chat_backend=_Backend(), log_service=_FakeLogService(), max_iterations=5)
    initial_messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": '{"task_id":"task-1","goal":"demo"}'},
        {"role": "tool", "name": "filesystem", "tool_call_id": "call-orphan|fc_orphan", "content": '{"ok": true}'},
    ]

    result = await loop.run(
        task=SimpleNamespace(task_id='task-1'),
        node=SimpleNamespace(node_id='node-1', depth=0, node_kind='execution', goal='demo'),
        messages=initial_messages,
        tools={},
        model_refs=['fake'],
        runtime_context={'task_id': 'task-1', 'node_id': 'node-1'},
        max_iterations=5,
    )

    assert result.status == "failed"
    assert result.delivery_status == "blocked"
    assert result.failure_disposition == 'pause'
    # The orphan breaker has its own threshold and the precise reason; a single rejected
    # submission must not preempt it with the generic final-submission guard.
    assert result.summary == 'orphan tool result circuit breaker triggered'
    assert 'call-orphan' in result.blocking_reason
    assert 'final result submission guard triggered' not in result.summary
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_react_loop_names_available_tools_when_a_tool_name_is_not_callable() -> None:
    """名称被拒时必须同时给出可调用集与可加载候选：只说 not available 无法判断该改名、该加载还是该换工具。"""
    class _NoCallBackend:
        async def chat(self, **kwargs):
            raise AssertionError('chat must not be called')

    loop = ReActToolLoop(chat_backend=_NoCallBackend(), log_service=_FakeLogService(), max_iterations=3)

    result = await loop._execute_tool_raw(
        tools={'submit_final_result': _submit_final_result_tool()},
        tool_name='submit_final_resultt',
        arguments={},
        runtime_context={'candidate_tool_names': ['agent_browser'], 'node_kind': 'execution'},
    )

    assert 'tool not available: submit_final_resultt' in str(result)
    assert '当前可直接调用的工具：submit_final_result' in str(result)
    assert '本轮其他候选工具（排名选中，可加载）：agent_browser' in str(result)


@pytest.mark.asyncio
async def test_react_loop_recovers_when_a_rejected_final_submission_is_resubmitted_within_budget() -> None:
    turns: list[int] = []
    request_messages: list[list[dict[str, object]]] = []

    class _Backend:
        def __init__(self) -> None:
            self.turn = 0

        async def chat(self, **kwargs):
            self.turn += 1
            turns.append(self.turn)
            request_messages.append([dict(item) for item in list(kwargs.get("messages") or [])])
            if self.turn == 1:
                # The 2026-09-22 incident shape: the long answer made it, the envelope did not.
                return LLMResponse(
                    content='',
                    tool_calls=[
                        ToolCallRequest(
                            id='call:incident',
                            name='submit_final_result',
                            arguments={'answer': 'deliverables are on disk', 'evidence': '[{"kind":"file"}]'},
                        )
                    ],
                    finish_reason='tool_calls',
                    usage={'input_tokens': 8, 'output_tokens': 3},
                )
            return LLMResponse(
                content='',
                tool_calls=[
                    ToolCallRequest(
                        id='call:fixed',
                        name='submit_final_result',
                        arguments={
                            'status': 'success',
                            'delivery_status': 'final',
                            'summary': 'done',
                            'answer': 'deliverables are on disk',
                            'evidence': [],
                            'remaining_work': [],
                            'blocking_reason': '',
                        },
                    )
                ],
                finish_reason='tool_calls',
                usage={'input_tokens': 8, 'output_tokens': 3},
            )

    log_service = _FakeLogService()
    loop = ReActToolLoop(chat_backend=_Backend(), log_service=log_service, max_iterations=5)
    result = await loop.run(
        task=SimpleNamespace(task_id='task-resubmit'),
        node=SimpleNamespace(node_id='node-resubmit', depth=0, node_kind='execution', goal='demo'),
        messages=[
            {'role': 'system', 'content': 'system'},
            {'role': 'user', 'content': '{"task_id":"task-resubmit","goal":"demo"}'},
        ],
        tools={'submit_final_result': _submit_final_result_tool()},
        model_refs=['fake'],
        runtime_context={'task_id': 'task-resubmit', 'node_id': 'node-resubmit'},
        max_iterations=5,
    )

    assert result.status == 'success'
    assert result.summary == 'done'
    assert turns == [1, 2]
    # The rejection reaches the next request, and it carries the contract that was violated.
    second_request = json.dumps(request_messages[1], ensure_ascii=False)
    assert 'Only an empty `summary`/`answer`' in second_request
    assert _PARAMETER_CONTRACT_GUIDANCE_PREFIX in second_request
    assert 'remaining_work' in second_request


@pytest.mark.asyncio
async def test_react_loop_trips_final_submission_guard_only_after_the_full_strike_budget() -> None:
    """逐字相同的拒收另有更早的出口（见 test_final_result_shape_faults.py），这里刻意让
    每一击的 violation 都不同，守住"总预算耗尽才收口"这条主路。"""
    turns: list[int] = []
    distinct_payloads = [
        {'answer': 'still no summary'},
        {'summary': '', 'answer': 'x'},
        {'summary': 'ok', 'answer': 'x', 'remaining_work': ['']},
        {'summary': 'ok', 'answer': 'x', 'evidence': [{'kind': 'file', 'path': 'p', 'start_line': 0}]},
        {'summary': 'ok', 'answer': 'x', 'evidence': [{'kind': 'nope', 'note': 'n'}]},
    ]

    class _Backend:
        def __init__(self) -> None:
            self.turn = 0

        async def chat(self, **kwargs):
            _ = kwargs
            self.turn += 1
            turns.append(self.turn)
            return LLMResponse(
                content='',
                tool_calls=[
                    ToolCallRequest(
                        id=f'call:bad:{self.turn}',
                        name='submit_final_result',
                        arguments=dict(distinct_payloads[min(self.turn, len(distinct_payloads)) - 1]),
                    )
                ],
                finish_reason='tool_calls',
                usage={'input_tokens': 8, 'output_tokens': 3},
            )

    log_service = _FakeLogService()
    loop = ReActToolLoop(chat_backend=_Backend(), log_service=log_service, max_iterations=20)
    result = await loop.run(
        task=SimpleNamespace(task_id='task-budget'),
        node=SimpleNamespace(node_id='node-budget', depth=0, node_kind='execution', goal='demo'),
        messages=[
            {'role': 'system', 'content': 'system'},
            {'role': 'user', 'content': '{"task_id":"task-budget","goal":"demo"}'},
        ],
        tools={'submit_final_result': _submit_final_result_tool()},
        model_refs=['fake'],
        runtime_context={'task_id': 'task-budget', 'node_id': 'node-budget'},
        max_iterations=20,
    )

    assert result.status == 'failed'
    assert result.delivery_status == 'blocked'
    assert result.summary == 'final result submission guard triggered'
    assert len(turns) == _INVALID_FINAL_SUBMISSION_LIMIT
    assert 'evidence[0].kind must be one of' in result.blocking_reason
    frame = log_service.read_runtime_frame('task-budget', 'node-budget')
    assert int(frame.get('invalid_final_submission_count') or 0) == _INVALID_FINAL_SUBMISSION_LIMIT


def _rejected_final_backend(*, ordinary_tool: str | None = None):
    class _Backend:
        def __init__(self) -> None:
            self.turn = 0

        async def chat(self, **kwargs):
            _ = kwargs
            self.turn += 1
            if ordinary_tool and self.turn > 1:
                return LLMResponse(
                    content='',
                    tool_calls=[ToolCallRequest(id=f'call:ordinary:{self.turn}', name=ordinary_tool, arguments={})],
                    finish_reason='tool_calls',
                    usage={'input_tokens': 8, 'output_tokens': 3},
                )
            return LLMResponse(
                content='',
                tool_calls=[
                    ToolCallRequest(
                        id=f'call:bad:{self.turn}',
                        name='submit_final_result',
                        arguments={'answer': 'still no envelope', 'evidence': '[]'},
                    )
                ],
                finish_reason='tool_calls',
                usage={'input_tokens': 8, 'output_tokens': 3},
            )

    return _Backend()


@pytest.mark.asyncio
async def test_react_loop_exhausting_budget_on_rejected_final_returns_pause_instead_of_raising() -> None:
    """迭代预算恰好在「上一击仍被拒收」时用尽时，终态必须是结构化 pause。

    抛 RuntimeError 只会被 NodeRunner 的通用 except 车道接住，而那条车道不做
    取消/节点暂停前置判定、也不回灌节点最后一次有效结果。
    """
    loop = ReActToolLoop(
        chat_backend=_rejected_final_backend(), log_service=_FakeLogService(), max_iterations=1
    )
    result = await loop.run(
        task=SimpleNamespace(task_id='task-budget-exhausted'),
        node=SimpleNamespace(node_id='node-budget-exhausted', depth=0, node_kind='execution', goal='demo'),
        messages=[
            {'role': 'system', 'content': 'system'},
            {'role': 'user', 'content': '{"task_id":"task-budget-exhausted","goal":"demo"}'},
        ],
        tools={'submit_final_result': _submit_final_result_tool()},
        model_refs=['fake'],
        runtime_context={'task_id': 'task-budget-exhausted', 'node_id': 'node-budget-exhausted'},
        max_iterations=1,
    )

    assert result.status == 'failed'
    assert result.delivery_status == 'blocked'
    assert result.failure_disposition == 'pause'
    assert 'detected 1 consecutive times' in result.blocking_reason
    assert 'summary must be at least 1 chars' in result.blocking_reason


@pytest.mark.asyncio
async def test_react_loop_ordinary_tool_turn_clears_stale_contract_violations() -> None:
    """普通工具轮清零 strike 计数时也必须清空违规清单，否则终态归因会引用一轮已被作废的违规。"""
    loop = ReActToolLoop(
        chat_backend=_rejected_final_backend(ordinary_tool='record_tool'),
        log_service=_FakeLogService(),
        max_iterations=3,
    )
    with pytest.raises(RuntimeError, match='exceeded maximum ReAct iterations'):
        await loop.run(
            task=SimpleNamespace(task_id='task-stale-violations'),
            node=SimpleNamespace(node_id='node-stale-violations', depth=0, node_kind='execution', goal='demo'),
            messages=[
                {'role': 'system', 'content': 'system'},
                {'role': 'user', 'content': '{"task_id":"task-stale-violations","goal":"demo"}'},
            ],
            tools={
                'submit_final_result': _submit_final_result_tool(),
                'record_tool': _StageProtocolNoopTool('record_tool'),
            },
            model_refs=['fake'],
            runtime_context={'task_id': 'task-stale-violations', 'node_id': 'node-stale-violations'},
            max_iterations=3,
        )




@pytest.mark.asyncio
async def test_standalone_contract_echo_repairs_once_then_pauses() -> None:
    # 回归 task:e580ebc3dc55：模型把请求末尾注入的 node_runtime_tool_contract
    # 原文复读成整条回复（零工具调用）。修复前该纯文本经 auto-wrap 被包装成
    # success+final，把契约当成任务交付送进验收；修复后首次回显注入修复提示
    # 重试，再次回显按协议违规收口为可恢复的 error pause，绝不升格为成功。
    echo_content = (
        "## Runtime Tool Contract\n"
        "kind: node_runtime_tool_contract\n"
        "callable_tools: `filesystem_stat`, `submit_final_result`, `exec`\n"
        "candidate_tools:\n"
        "- `filesystem_write`\n"
    )

    requests: list[list[dict[str, object]]] = []

    class _Backend:
        async def chat(self, **kwargs):
            requests.append([dict(item) for item in list(kwargs.get("messages") or [])])
            return LLMResponse(
                content=echo_content,
                tool_calls=[],
                finish_reason="stop",
                usage={"input_tokens": 2000, "output_tokens": 200},
            )

    loop = ReActToolLoop(chat_backend=_Backend(), log_service=_FakeLogService(), max_iterations=5)

    result = await loop.run(
        task=SimpleNamespace(task_id="task-contract-echo-standalone"),
        node=SimpleNamespace(node_id="node-contract-echo", depth=0, node_kind="execution", goal="demo"),
        messages=[
            {"role": "system", "content": "system"},
            {"role": "user", "content": '{"task_id":"task-contract-echo-standalone","goal":"demo"}'},
        ],
        tools={
            "submit_final_result": _submit_final_result_tool(),
        },
        model_refs=["fake"],
        runtime_context={"task_id": "task-contract-echo-standalone", "node_id": "node-contract-echo"},
        max_iterations=5,
    )

    assert result.status == "failed"
    assert result.delivery_status == "blocked"
    assert result.failure_disposition == "pause"
    assert "runtime tool contract echo guard triggered" in result.summary

    # 首次回显只注入一次修复提示重试，第二次才收口。
    assert len(requests) == 2
    second_request = requests[1]
    repair_hits = [
        message
        for message in second_request
        if str(message.get("content") or "").startswith("System note for this turn only:")
        and "Runtime Tool Contract" in str(message.get("content") or "")
    ]
    assert len(repair_hits) == 1


@pytest.mark.asyncio
async def test_react_loop_writes_before_model_frame_before_chat_dispatch() -> None:
    log_service = _FakeLogService()
    final_tool = _submit_final_result_tool()

    class _Backend:
        async def chat(self, **kwargs):
            _ = kwargs
            frame = log_service.read_runtime_frame("task-before-model", "node-before-model")
            assert frame.get("phase") == "before_model"
            assert frame.get("callable_tool_names") == ["submit_final_result"]
            assert frame.get("candidate_tool_names") == ["filesystem_write"]
            assert frame.get("rbac_visible_tool_names") == ["submit_final_result", "filesystem_write"]
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
                usage={"input_tokens": 8, "output_tokens": 3},
            )

    loop = ReActToolLoop(chat_backend=_Backend(), log_service=log_service, max_iterations=2)
    loop._visible_tools_for_iteration = lambda **kwargs: dict(kwargs.get("tools") or {})
    loop._model_visible_tools_for_iteration = lambda **kwargs: (
        {"submit_final_result": final_tool},
        {
            "tool_names": ["submit_final_result"],
            "candidate_tool_names": ["filesystem_write"],
            "lightweight_tool_ids": [],
            "trace": {"rbac_visible_tool_names": ["submit_final_result", "filesystem_write"]},
        },
    )

    result = await loop.run(
        task=SimpleNamespace(task_id="task-before-model"),
        node=SimpleNamespace(node_id="node-before-model", depth=0, node_kind="execution"),
        messages=[
            {"role": "system", "content": "system"},
            {"role": "user", "content": '{"task_id":"task-before-model","goal":"demo"}'},
        ],
        tools={"submit_final_result": final_tool},
        model_refs=["fake"],
        runtime_context={"task_id": "task-before-model", "node_id": "node-before-model"},
        max_iterations=2,
    )

    assert result.status == "success"
    assert result.answer == "done"


@pytest.mark.asyncio
async def test_react_loop_writes_candidate_skill_items_into_before_model_frame() -> None:
    log_service = _FakeLogService()
    final_tool = _submit_final_result_tool()
    node_id = "node-before-model-visible-skills"
    contract_message = NodeRuntimeToolContract(
        node_id=node_id,
        node_kind="execution",
        callable_tool_names=["submit_final_result"],
        candidate_tool_names=["filesystem_write"],
        visible_skills=[
            {
                "skill_id": "tmux",
                "display_name": "tmux",
                "description": "terminal workflow",
            }
        ],
        candidate_skill_ids=["tmux"],
        candidate_skill_items=[
            {
                "skill_id": "tmux",
                "description": "terminal workflow",
            }
        ],
        contract_visible_skill_ids=["skill-creator", "tmux"],
        skill_visibility_diagnostics={
            "registry_skill_ids": ["skill-creator", "tmux"],
            "entries": [
                {
                    "skill_id": "skill-creator",
                    "enabled": True,
                    "available": True,
                    "allowed_for_actor_role": True,
                    "policy_effect": "allow",
                    "included_in_contract_visible": True,
                },
                {
                    "skill_id": "tmux",
                    "enabled": True,
                    "available": True,
                    "allowed_for_actor_role": True,
                    "policy_effect": "allow",
                    "included_in_contract_visible": True,
                },
            ],
        },

        lightweight_tool_ids=[],
        selection_trace={},
    ).to_message()

    class _Backend:
        async def chat(self, **kwargs):
            _ = kwargs
            frame = log_service.read_runtime_frame("task-before-model-visible-skills", node_id)
            assert frame.get("candidate_skill_items") == [
                {
                    "skill_id": "tmux",
                    "description": "terminal workflow",
                }
            ]
            assert frame.get("candidate_skill_ids") == ["tmux"]
            assert frame.get("contract_visible_skill_ids") == ["skill-creator", "tmux"]
            assert frame.get("skill_visibility_diagnostics") == {
                "registry_skill_ids": ["skill-creator", "tmux"],
                "entries": [
                    {
                        "skill_id": "skill-creator",
                        "enabled": True,
                        "available": True,
                        "allowed_for_actor_role": True,
                        "policy_effect": "allow",
                        "included_in_contract_visible": True,
                    },
                    {
                        "skill_id": "tmux",
                        "enabled": True,
                        "available": True,
                        "allowed_for_actor_role": True,
                        "policy_effect": "allow",
                        "included_in_contract_visible": True,
                    },
                ],
            }
            return LLMResponse(
                content="",
                tool_calls=[
                    ToolCallRequest(
                        id="call:final-visible-skills",
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
                usage={"input_tokens": 8, "output_tokens": 3},
            )

    loop = ReActToolLoop(chat_backend=_Backend(), log_service=log_service, max_iterations=2)
    loop._visible_tools_for_iteration = lambda **kwargs: dict(kwargs.get("tools") or {})
    loop._model_visible_tools_for_iteration = lambda **kwargs: (
        {"submit_final_result": final_tool},
        {
            "tool_names": ["submit_final_result"],
            "candidate_tool_names": ["filesystem_write"],
            "lightweight_tool_ids": [],
            "trace": {"rbac_visible_tool_names": ["submit_final_result", "filesystem_write"]},
        },
    )

    result = await loop.run(
        task=SimpleNamespace(task_id="task-before-model-visible-skills"),
        node=SimpleNamespace(task_id="task-before-model-visible-skills", node_id=node_id, depth=0, node_kind="execution"),
        messages=[
            {"role": "system", "content": "system"},
            {"role": "user", "content": '{"task_id":"task-before-model-visible-skills","goal":"demo"}'},
            contract_message,
        ],
        tools={"submit_final_result": final_tool},
        model_refs=["fake"],
        runtime_context={"task_id": "task-before-model-visible-skills", "node_id": node_id},
        max_iterations=2,
    )

    assert result.status == "success"
    assert result.answer == "done"


def test_build_node_dynamic_contract_does_not_resurrect_stale_candidate_tools_when_names_are_empty() -> None:
    log_service = _FakeLogService()
    loop = ReActToolLoop(chat_backend=SimpleNamespace(), log_service=log_service, max_iterations=2)
    task_id = "task-no-stale-candidates"
    node = SimpleNamespace(task_id=task_id, node_id="node-no-stale-candidates", depth=0, node_kind="execution")

    stale_contract = NodeRuntimeToolContract(
        node_id=node.node_id,
        node_kind=node.node_kind,
        callable_tool_names=["filesystem_stat"],
        candidate_tool_names=["filesystem_write"],
        visible_skills=[],
        candidate_skill_ids=[],
        candidate_skill_items=[],
        candidate_tool_items=[{"tool_id": "filesystem_write", "description": "write file"}],

        lightweight_tool_ids=[],
        selection_trace={},
    ).to_message()

    log_service.upsert_frame(
        task_id,
        {
            "node_id": node.node_id,
            "depth": node.depth,
            "node_kind": node.node_kind,
            "phase": "before_model",
            "messages": [
                {"role": "system", "content": "system"},
                {"role": "user", "content": '{"prompt":"demo"}'},
                stale_contract,
            ],
            "callable_tool_names": ["filesystem_stat"],
            "candidate_tool_names": [],
            "candidate_tool_items": [{"tool_id": "filesystem_write", "description": "write file"}],
            "selected_skill_ids": [],
            "candidate_skill_ids": [],
        },
        publish_snapshot=False,
    )

    contract = loop._build_node_dynamic_contract(
        node=node,
        message_history=[
            {"role": "system", "content": "system"},
            {"role": "user", "content": '{"prompt":"demo"}'},
            stale_contract,
        ],
        tool_schema_selection={
            "tool_names": ["filesystem_stat"],
            "candidate_tool_names": [],
            "lightweight_tool_ids": [],
            "trace": {},
        },

    )

    assert contract.candidate_tool_names == []
    assert contract.to_message_payload()["candidate_tools"] == []


def test_build_node_dynamic_contract_does_not_resurrect_stale_candidate_skills_when_ids_are_empty() -> None:
    log_service = _FakeLogService()
    loop = ReActToolLoop(chat_backend=SimpleNamespace(), log_service=log_service, max_iterations=2)
    task_id = "task-no-stale-skill-candidates"
    node = SimpleNamespace(task_id=task_id, node_id="node-no-stale-skill-candidates", depth=0, node_kind="execution")

    stale_contract = NodeRuntimeToolContract(
        node_id=node.node_id,
        node_kind=node.node_kind,
        callable_tool_names=["filesystem_stat"],
        candidate_tool_names=[],
        visible_skills=[],
        candidate_skill_ids=["tmux"],
        candidate_skill_items=[{"skill_id": "tmux", "description": "terminal workflow"}],

        lightweight_tool_ids=[],
        selection_trace={},
    ).to_message()

    log_service.upsert_frame(
        task_id,
        {
            "node_id": node.node_id,
            "depth": node.depth,
            "node_kind": node.node_kind,
            "phase": "before_model",
            "messages": [
                {"role": "system", "content": "system"},
                {"role": "user", "content": '{"prompt":"demo"}'},
                stale_contract,
            ],
            "callable_tool_names": ["filesystem_stat"],
            "candidate_tool_names": [],
            "candidate_tool_items": [],
            "selected_skill_ids": [],
            "candidate_skill_ids": [],
            "candidate_skill_items": [{"skill_id": "tmux", "description": "terminal workflow"}],
        },
        publish_snapshot=False,
    )

    contract = loop._build_node_dynamic_contract(
        node=node,
        message_history=[
            {"role": "system", "content": "system"},
            {"role": "user", "content": '{"prompt":"demo"}'},
            stale_contract,
        ],
        tool_schema_selection={
            "tool_names": ["filesystem_stat"],
            "candidate_tool_names": [],
            "lightweight_tool_ids": [],
            "trace": {},
        },

    )

    assert contract.candidate_skill_ids == []
    assert contract.to_message_payload()["candidate_skills"] == []


def test_build_node_dynamic_contract_uses_fresh_message_skill_contract_when_bootstrap_frame_is_empty() -> None:
    log_service = _FakeLogService()
    loop = ReActToolLoop(chat_backend=SimpleNamespace(), log_service=log_service, max_iterations=2)
    task_id = "task-bootstrap-empty-skill-frame"
    node = SimpleNamespace(task_id=task_id, node_id="node-bootstrap-empty-skill-frame", depth=0, node_kind="execution")

    fresh_contract = NodeRuntimeToolContract(
        node_id=node.node_id,
        node_kind=node.node_kind,
        callable_tool_names=["filesystem_stat", "load_skill_context"],
        candidate_tool_names=["content"],
        visible_skills=[],
        candidate_skill_ids=["web-access", "multi-search-engine"],
        candidate_skill_items=[
            {"skill_id": "web-access", "description": "browser workflow"},
            {"skill_id": "multi-search-engine", "description": "search workflow"},
        ],
        contract_visible_skill_ids=["web-access", "multi-search-engine"],
        skill_visibility_diagnostics={
            "registry_skill_ids": ["web-access", "multi-search-engine"],
            "entries": [
                {
                    "skill_id": "web-access",
                    "enabled": True,
                    "available": True,
                    "allowed_for_actor_role": True,
                    "policy_effect": "allow",
                    "included_in_contract_visible": True,
                },
                {
                    "skill_id": "multi-search-engine",
                    "enabled": True,
                    "available": True,
                    "allowed_for_actor_role": True,
                    "policy_effect": "allow",
                    "included_in_contract_visible": True,
                },
            ],
        },

        lightweight_tool_ids=[],
        selection_trace={},
    ).to_message()

    log_service.upsert_frame(
        task_id,
        {
            "node_id": node.node_id,
            "depth": node.depth,
            "node_kind": node.node_kind,
            "phase": "before_model",
            "messages": [],
            "callable_tool_names": [],
            "candidate_tool_names": [],
            "candidate_tool_items": [],
            "selected_skill_ids": [],
            "candidate_skill_ids": [],
            "candidate_skill_items": [],
            "contract_visible_skill_ids": [],
            "skill_visibility_diagnostics": {"registry_skill_ids": [], "entries": []},
        },
        publish_snapshot=False,
    )

    contract = loop._build_node_dynamic_contract(
        node=node,
        message_history=[
            {"role": "system", "content": "system"},
            {"role": "user", "content": '{"prompt":"demo"}'},
            fresh_contract,
        ],
        tool_schema_selection={
            "tool_names": ["filesystem_stat", "load_skill_context"],
            "candidate_tool_names": ["content"],
            "lightweight_tool_ids": [],
            "trace": {},
        },

    )

    assert contract.candidate_skill_ids == ["web-access", "multi-search-engine"]
    assert contract.contract_visible_skill_ids == ["web-access", "multi-search-engine"]
    assert contract.skill_visibility_diagnostics == {
        "registry_skill_ids": ["web-access", "multi-search-engine"],
        "entries": [
            {
                "skill_id": "web-access",
                "enabled": True,
                "available": True,
                "allowed_for_actor_role": True,
                "policy_effect": "allow",
                "included_in_contract_visible": True,
            },
            {
                "skill_id": "multi-search-engine",
                "enabled": True,
                "available": True,
                "allowed_for_actor_role": True,
                "policy_effect": "allow",
                "included_in_contract_visible": True,
            },
        ],
    }


def test_build_node_dynamic_contract_prefers_fresh_runtime_seeded_skill_contract_after_prepare_messages_strip() -> None:
    log_service = _FakeLogService()
    loop = ReActToolLoop(chat_backend=SimpleNamespace(), log_service=log_service, max_iterations=2)
    task_id = "task-runtime-seeded-skill-contract"
    node = SimpleNamespace(task_id=task_id, node_id="node-runtime-seeded-skill-contract", depth=0, node_kind="execution")

    prepared_history = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": '{"prompt":"demo"}'},
    ]

    log_service.upsert_frame(
        task_id,
        {
            "node_id": node.node_id,
            "depth": node.depth,
            "node_kind": node.node_kind,
            "phase": "before_model",
            "messages": [],
            "callable_tool_names": [],
            "candidate_tool_names": [],
            "candidate_tool_items": [],
            "selected_skill_ids": [],
            "candidate_skill_ids": [],
            "candidate_skill_items": [],
            "contract_visible_skill_ids": [],
            "skill_visibility_diagnostics": {"registry_skill_ids": [], "entries": []},
        },
        publish_snapshot=False,
    )

    contract = loop._build_node_dynamic_contract(
        node=node,
        message_history=prepared_history,
        tool_schema_selection={
            "tool_names": ["filesystem_stat", "load_skill_context"],
            "candidate_tool_names": ["content"],
            "lightweight_tool_ids": [],
            "trace": {},
        },

        runtime_context={
            "fresh_node_dynamic_contract_payload": {
                "message_type": "node_runtime_tool_contract",
                "candidate_skills": [
                    {"skill_id": "web-access", "description": "browser workflow"},
                    {"skill_id": "multi-search-engine", "description": "search workflow"},
                ],
                "contract_visible_skill_ids": ["web-access", "multi-search-engine"],
                "skill_visibility_diagnostics": {
                    "registry_skill_ids": ["web-access", "multi-search-engine"],
                    "entries": [
                        {
                            "skill_id": "web-access",
                            "enabled": True,
                            "available": True,
                            "allowed_for_actor_role": True,
                            "policy_effect": "allow",
                            "included_in_contract_visible": True,
                        },
                        {
                            "skill_id": "multi-search-engine",
                            "enabled": True,
                            "available": True,
                            "allowed_for_actor_role": True,
                            "policy_effect": "allow",
                            "included_in_contract_visible": True,
                        },
                    ],
                },
            }
        },
    )

    assert contract.candidate_skill_ids == ["web-access", "multi-search-engine"]
    assert contract.to_message_payload()["candidate_skills"] == [
        {"skill_id": "web-access", "description": "browser workflow"},
        {"skill_id": "multi-search-engine", "description": "search workflow"},
    ]
    assert contract.contract_visible_skill_ids == ["web-access", "multi-search-engine"]


@pytest.mark.asyncio
async def test_react_loop_before_model_frame_keeps_candidate_tool_items_aligned_with_empty_candidate_names() -> None:
    log_service = _FakeLogService()
    final_tool = _submit_final_result_tool()
    node_id = "node-before-model-empty-candidates"
    stale_contract = NodeRuntimeToolContract(
        node_id=node_id,
        node_kind="execution",
        callable_tool_names=["submit_final_result"],
        candidate_tool_names=["filesystem_write"],
        visible_skills=[],
        candidate_skill_ids=[],
        candidate_skill_items=[],
        candidate_tool_items=[{"tool_id": "filesystem_write", "description": "write file"}],

        lightweight_tool_ids=[],
        selection_trace={},
    ).to_message()

    class _Backend:
        async def chat(self, **kwargs):
            _ = kwargs
            frame = log_service.read_runtime_frame("task-before-model-empty-candidates", node_id)
            assert frame.get("candidate_tool_names") == []
            assert frame.get("candidate_tool_items") == []
            return LLMResponse(
                content="",
                tool_calls=[
                    ToolCallRequest(
                        id="call:final-empty-candidates",
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
                usage={"input_tokens": 8, "output_tokens": 3},
            )

    loop = ReActToolLoop(chat_backend=_Backend(), log_service=log_service, max_iterations=2)
    loop._visible_tools_for_iteration = lambda **kwargs: dict(kwargs.get("tools") or {})
    loop._model_visible_tools_for_iteration = lambda **kwargs: (
        {"submit_final_result": final_tool},
        {
            "tool_names": ["submit_final_result"],
            "candidate_tool_names": [],
            "lightweight_tool_ids": [],
            "trace": {"rbac_visible_tool_names": ["submit_final_result", "filesystem_write", "web_fetch"]},
        },
    )

    result = await loop.run(
        task=SimpleNamespace(task_id="task-before-model-empty-candidates"),
        node=SimpleNamespace(
            task_id="task-before-model-empty-candidates",
            node_id=node_id,
            depth=0,
            node_kind="execution",
        ),
        messages=[
            {"role": "system", "content": "system"},
            {"role": "user", "content": '{"task_id":"task-before-model-empty-candidates","goal":"demo"}'},
            stale_contract,
        ],
        tools={"submit_final_result": final_tool},
        model_refs=["fake"],
        runtime_context={"task_id": "task-before-model-empty-candidates", "node_id": node_id},
        max_iterations=2,
    )

    assert result.status == "success"
    assert result.answer == "done"


@pytest.mark.asyncio
async def test_react_loop_before_model_frame_keeps_candidate_skill_items_aligned_with_empty_candidate_ids() -> None:
    log_service = _FakeLogService()
    final_tool = _submit_final_result_tool()
    node_id = "node-before-model-empty-skill-candidates"
    stale_contract = NodeRuntimeToolContract(
        node_id=node_id,
        node_kind="execution",
        callable_tool_names=["submit_final_result"],
        candidate_tool_names=[],
        visible_skills=[],
        candidate_skill_ids=["tmux"],
        candidate_skill_items=[{"skill_id": "tmux", "description": "terminal workflow"}],

        lightweight_tool_ids=[],
        selection_trace={},
    ).to_message()
    log_service.upsert_frame(
        "task-before-model-empty-skill-candidates",
        {
            "node_id": node_id,
            "depth": 0,
            "node_kind": "execution",
            "phase": "before_model",
            "messages": [
                {"role": "system", "content": "system"},
                {"role": "user", "content": '{"task_id":"task-before-model-empty-skill-candidates","goal":"demo"}'},
                stale_contract,
            ],
            "callable_tool_names": ["submit_final_result"],
            "candidate_tool_names": [],
            "candidate_tool_items": [],
            "selected_skill_ids": [],
            "candidate_skill_ids": [],
            "candidate_skill_items": [],
        },
        publish_snapshot=False,
    )

    class _Backend:
        async def chat(self, **kwargs):
            _ = kwargs
            frame = log_service.read_runtime_frame("task-before-model-empty-skill-candidates", node_id)
            assert frame.get("candidate_skill_ids") == []
            assert frame.get("candidate_skill_items") == []
            return LLMResponse(
                content="",
                tool_calls=[
                    ToolCallRequest(
                        id="call:final-empty-skill-candidates",
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
                usage={"input_tokens": 8, "output_tokens": 3},
            )

    loop = ReActToolLoop(chat_backend=_Backend(), log_service=log_service, max_iterations=2)
    loop._visible_tools_for_iteration = lambda **kwargs: dict(kwargs.get("tools") or {})
    loop._model_visible_tools_for_iteration = lambda **kwargs: (
        {"submit_final_result": final_tool},
        {
            "tool_names": ["submit_final_result"],
            "candidate_tool_names": [],
            "lightweight_tool_ids": [],
            "trace": {"rbac_visible_tool_names": ["submit_final_result"]},
        },
    )

    result = await loop.run(
        task=SimpleNamespace(task_id="task-before-model-empty-skill-candidates"),
        node=SimpleNamespace(
            task_id="task-before-model-empty-skill-candidates",
            node_id=node_id,
            depth=0,
            node_kind="execution",
        ),
        messages=[
            {"role": "system", "content": "system"},
            {"role": "user", "content": '{"task_id":"task-before-model-empty-skill-candidates","goal":"demo"}'},
            stale_contract,
        ],
        tools={"submit_final_result": final_tool},
        model_refs=["fake"],
        runtime_context={"task_id": "task-before-model-empty-skill-candidates", "node_id": node_id},
        max_iterations=2,
    )

    assert result.status == "success"
    assert result.answer == "done"


@pytest.mark.asyncio
async def test_react_loop_execute_tool_keeps_direct_load_payload_inline(tmp_path) -> None:
    store = SQLiteTaskStore(tmp_path / "runtime.sqlite3")
    artifact_store = TaskArtifactStore(artifact_dir=tmp_path / "artifacts", store=store)
    log_service = _FakeLogService()
    log_service._content_store = ContentNavigationService(
        workspace=tmp_path,
        artifact_store=artifact_store,
        artifact_lookup=artifact_store,
    )
    loop = ReActToolLoop(chat_backend=SimpleNamespace(), log_service=log_service, max_iterations=2)

    try:
        rendered = await loop._execute_tool(
            tools={"load_tool_context": _DirectLoadTool()},
            tool_name="load_tool_context",
            arguments={},
            runtime_context={"task_id": "task-1", "node_id": "node-1", "actor_role": "execution"},
        )

        assert parse_content_envelope(rendered) is None
        payload = json.loads(rendered)
        assert payload["uri"] == "negi://resource/tool/content"
        assert payload["content"].startswith("tool line 001")
    finally:
        store.close()


@pytest.mark.asyncio
async def test_load_tool_context_tool_allows_rbac_visible_non_candidate_runtime_target() -> None:
    service = _RuntimeToolService()
    tool = LoadToolContextTool(lambda: service)

    rendered = await tool.execute(
        tool_id="filesystem_write",
        _LoadToolContextTool__g3ku_runtime={
            "session_key": "web:shared",
            "actor_role": "execution",
            "tool_contract_enforced": True,
            "candidate_tool_names": ["agent_browser"],
            "rbac_visible_tool_names": ["filesystem_write"],
        },
    )

    payload = json.loads(rendered)
    assert payload["ok"] is True
    assert payload["tool_id"] == "filesystem_write"
    assert service.load_tool_calls == [
        {
            "actor_role": "execution",
            "session_id": "web:shared",
            "tool_id": "filesystem_write",
        }
    ]


@pytest.mark.asyncio
async def test_load_tool_context_tool_rejects_non_visible_runtime_target() -> None:
    service = _RuntimeToolService()
    tool = LoadToolContextTool(lambda: service)

    rendered = await tool.execute(
        tool_id="filesystem_write",
        _LoadToolContextTool__g3ku_runtime={
            "session_key": "web:shared",
            "actor_role": "execution",
            "tool_contract_enforced": True,
            "candidate_tool_names": ["agent_browser"],
            "rbac_visible_tool_names": ["web_fetch"],
        },
    )

    assert rendered.startswith("Error:")
    assert service.load_tool_calls == []


@pytest.mark.asyncio
async def test_load_skill_context_tool_ignores_stale_candidate_snapshot() -> None:
    """skill 候选闸门已拆：回合初的 `candidate_skill_ids` 快照不得拒人。

    可见性由 `load_skill_context_v2` 按治理实时可见集强制（已删除 / 权限不允许 / 待修复
    三态回执）；tool 层若还拿回合初快照拦，mid-turn 新授权的 skill 会被 stale 名单误拒。
    """
    service = _RuntimeToolService()
    tool = LoadSkillContextTool(lambda: service)

    rendered = await tool.execute(
        skill_id="find-skills",
        _LoadSkillContextTool__g3ku_runtime={
            "session_key": "web:shared",
            "actor_role": "execution",
            "tool_contract_enforced": True,
            "candidate_skill_ids": ["repair-tool"],
        },
    )

    payload = json.loads(rendered)
    assert payload["ok"] is True
    assert payload["skill_id"] == "find-skills"
    # 请求照样落到 v2 可见性通道（那里才有治理口径），tool 层不自己裁决
    assert service.load_skill_calls == [
        {"actor_role": "execution", "session_id": "web:shared", "skill_id": "find-skills"}
    ]


@pytest.mark.asyncio
async def test_execute_tool_calls_injects_rbac_visible_tool_names_into_runtime_context() -> None:
    class _RuntimeCaptureTool(Tool):
        @property
        def name(self) -> str:
            return "capture_runtime"

        @property
        def description(self) -> str:
            return "Capture runtime context"

        @property
        def parameters(self) -> dict[str, object]:
            return {
                "type": "object",
                "properties": {
                    "value": {"type": "string", "description": "value"},
                },
                "required": ["value"],
            }

        async def execute(self, value: str, __g3ku_runtime: dict[str, object] | None = None, **kwargs):
            _ = kwargs
            runtime = __g3ku_runtime if isinstance(__g3ku_runtime, dict) else {}
            return json.dumps(
                {
                    "value": value,
                    "rbac_visible_tool_names": runtime.get("rbac_visible_tool_names"),
                    "callable_tool_names": runtime.get("callable_tool_names"),
                    "candidate_tool_names": runtime.get("candidate_tool_names"),
                    "full_callable_tool_names": runtime.get("full_callable_tool_names"),
                    # 循环自己那批记账键（在场索引、暂存时钟、冲动计数）不该出现在工具道里
                    "loop_internal_keys": sorted(
                        key
                        for key in runtime
                        if key
                        in {
                            "tool_contract_presence",
                            "tool_arg_stash_carrier",
                            "impulse_delivered_tools",
                            "impulse_staged_call_ids",
                            "impulse_batch_totals",
                        }
                    ),
                },
                ensure_ascii=False,
            )

    log_service = _FakeLogService()
    log_service.upsert_frame(
        "task-runtime-rbac",
        {
            "node_id": "node-runtime-rbac",
            "rbac_visible_tool_names": ["exec", "load_tool_context"],
            "callable_tool_names": ["load_tool_context"],
            "candidate_tool_names": ["filesystem_write"],
            "model_visible_tool_selection_trace": {"full_callable_tool_names": ["load_tool_context", "exec"]},
        },
    )
    loop = ReActToolLoop(chat_backend=SimpleNamespace(), log_service=log_service, max_iterations=2)

    results = await loop._execute_tool_calls(
        task=SimpleNamespace(task_id="task-runtime-rbac"),
        node=SimpleNamespace(node_id="node-runtime-rbac", depth=0, node_kind="execution"),
        response_tool_calls=[SimpleNamespace(id="call-runtime-rbac", name="capture_runtime", arguments={"value": "demo"})],
        tools={"capture_runtime": _RuntimeCaptureTool()},
        runtime_context={
            "session_key": "web:shared",
            "actor_role": "execution",
            "node_kind": "execution",
            "tool_contract_presence": {"exec": "tcf:aaaaaaaaaaaaaaaa"},
            "tool_arg_stash_carrier": {"filesystem_write": {"hop": 1}},
            "impulse_delivered_tools": {"filesystem_write"},
            "impulse_staged_call_ids": {"call-runtime-rbac"},
            "impulse_batch_totals": {"capture_runtime": 1},
        },
    )

    payload = json.loads(str(results[0]["raw_result"] or ""))
    assert payload["value"] == "demo"
    assert payload["rbac_visible_tool_names"] == ["exec", "load_tool_context"]
    assert payload["callable_tool_names"] == ["load_tool_context"]
    assert payload["candidate_tool_names"] == ["filesystem_write"]
    assert payload["full_callable_tool_names"] == ["load_tool_context", "exec"]
    assert payload["loop_internal_keys"] == []


@pytest.mark.asyncio
async def test_execute_tool_calls_injects_image_multimodal_enabled_from_runtime_model_refs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _RuntimeCaptureTool(Tool):
        @property
        def name(self) -> str:
            return "capture_runtime"

        @property
        def description(self) -> str:
            return "Capture runtime context"

        @property
        def parameters(self) -> dict[str, object]:
            return {
                "type": "object",
                "properties": {
                    "value": {"type": "string", "description": "value"},
                },
                "required": ["value"],
            }

        async def execute(self, value: str, __g3ku_runtime: dict[str, object] | None = None, **kwargs):
            _ = kwargs
            runtime = __g3ku_runtime if isinstance(__g3ku_runtime, dict) else {}
            return json.dumps(
                {
                    "value": value,
                    "model_refs": runtime.get("model_refs"),
                    "image_multimodal_enabled": runtime.get("image_multimodal_enabled"),
                },
                ensure_ascii=False,
            )

    import main.runtime.react_loop as react_loop_module

    monkeypatch.setattr(
        react_loop_module,
        "get_runtime_config",
        lambda force=False: (
            SimpleNamespace(
                get_managed_model=lambda key: SimpleNamespace(
                    image_multimodal_enabled=(key == "execution_model")
                )
            ),
            "rev-test",
            False,
        ),
    )

    log_service = _FakeLogService()
    loop = ReActToolLoop(chat_backend=SimpleNamespace(), log_service=log_service, max_iterations=2)

    results = await loop._execute_tool_calls(
        task=SimpleNamespace(task_id="task-runtime-mm"),
        node=SimpleNamespace(node_id="node-runtime-mm", depth=0, node_kind="execution"),
        response_tool_calls=[SimpleNamespace(id="call-runtime-mm", name="capture_runtime", arguments={"value": "demo"})],
        tools={"capture_runtime": _RuntimeCaptureTool()},
        runtime_context={
            "session_key": "web:shared",
            "actor_role": "execution",
            "node_kind": "execution",
            "model_refs": ["execution_model"],
        },
    )

    payload = json.loads(str(results[0]["raw_result"] or ""))
    assert payload["value"] == "demo"
    assert payload["model_refs"] == ["execution_model"]
    assert payload["image_multimodal_enabled"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("node_kind", ["execution", "acceptance"])
async def test_load_tool_context_direct_read_guard_blocks_duplicate_inline_toolskill_for_callable_lane(
    node_kind: str,
) -> None:
    class _InlineToolContextService:
        async def startup(self) -> None:
            return None

        def load_tool_context_v2(self, **kwargs):
            _ = kwargs
            return {
                "ok": True,
                "tool_id": "exec",
                "content": "# exec",
                "parameter_contract_markdown": "## Parameter Contract",
                "required_parameters": ["command"],
                "example_arguments": {"command": "pwd"},
                "warnings": [],
                "errors": [],
                "callable": True,
                "available": True,
                "repair_required": False,
                "callable_now": True,
                "will_be_hydrated_next_turn": False,
                "hydration_targets": [],
                "exec_runtime_policy": {
                    "mode": "governed",
                    "guardrails_enabled": True,
                    "summary": "exec will execute shell commands with exec-side guardrails.",
                },
                "tool_context_fingerprint": "tcf:current",
            }

    loop = ReActToolLoop(chat_backend=SimpleNamespace(), log_service=_FakeLogService(), max_iterations=2)
    prior_payload = {
        "ok": True,
        "tool_id": "exec",
        "content": "# exec",
        "parameter_contract_markdown": "## Parameter Contract",
        "required_parameters": ["command"],
        "example_arguments": {"command": "pwd"},
        "warnings": [],
        "errors": [],
        "callable": True,
        "available": True,
        "repair_required": False,
        "callable_now": True,
        "will_be_hydrated_next_turn": False,
        "hydration_targets": [],
        "exec_runtime_policy": {
            "mode": "governed",
            "guardrails_enabled": True,
            "summary": "exec will execute shell commands with exec-side guardrails.",
        },
    }
    prior_payload["tool_context_fingerprint"] = build_tool_context_fingerprint(prior_payload)
    violations = await loop._load_tool_context_direct_read_violations(
        response_tool_calls=[SimpleNamespace(id="call-1", name="load_tool_context", arguments={"tool_id": "exec"})],
        task=SimpleNamespace(task_id=f"task-{node_kind}-duplicate"),
        node=SimpleNamespace(node_id=f"node-{node_kind}-duplicate", node_kind=node_kind),
        message_history=[
            {
                "role": "tool",
                "tool_call_id": "call-old",
                "name": "load_tool_context",
                # 交付形态与真车道同形：块写在 `content` 里，判据只认那一颗头。
                "content": json.dumps(attach_tool_contract_block(prior_payload), ensure_ascii=False),
            }
        ],
        runtime_context={
            "session_key": "web:shared",
            "actor_role": "execution",
            "rbac_visible_tool_names": ["exec"],
            "callable_tool_names": ["load_tool_context", "exec"],
            "full_callable_tool_names": ["load_tool_context", "exec"],
        },
        tools={"load_tool_context": LoadToolContextTool(lambda: _InlineToolContextService())},
    )

    assert violations == [
        {
            "signature": f"load_tool_context:exec:{prior_payload['tool_context_fingerprint']}",
            "repair_text": "上下文中已有该工具当前版本的未压缩 toolskill，禁止重复读取！请直接复用已有说明，或在工具状态变化/旧内容被压缩后再重试。",
        }
    ]


@pytest.mark.asyncio
async def test_load_tool_context_direct_read_guard_allows_refresh_when_fingerprint_changes() -> None:
    class _InlineToolContextService:
        async def startup(self) -> None:
            return None

        def load_tool_context_v2(self, **kwargs):
            _ = kwargs
            return {
                "ok": True,
                "tool_id": "exec",
                "content": "# exec",
                "parameter_contract_markdown": "## Parameter Contract",
                "required_parameters": ["command"],
                "example_arguments": {"command": "pwd"},
                "warnings": [],
                "errors": [],
                "callable": True,
                "available": True,
                "repair_required": False,
                "callable_now": True,
                "will_be_hydrated_next_turn": False,
                "hydration_targets": [],
                "exec_runtime_policy": {
                    "mode": "full_access",
                    "guardrails_enabled": False,
                    "summary": "exec will execute shell commands without exec-side guardrails.",
                },
                "tool_context_fingerprint": "tcf:changed",
            }

    loop = ReActToolLoop(chat_backend=SimpleNamespace(), log_service=_FakeLogService(), max_iterations=2)
    prior_payload = {
        "ok": True,
        "tool_id": "exec",
        # 修订身份现在只看源正文 + 参数表（D2）。`exec_runtime_policy` 已退出哈希，
        # "换了修订"必须是正文真的变了，不能再靠治理模式制造号差。
        "content": "# exec\n\nOld toolskill revision.\n",
        "parameter_contract_markdown": "## Parameter Contract",
        "required_parameters": ["command"],
        "example_arguments": {"command": "pwd"},
        "warnings": [],
        "errors": [],
        "callable": True,
        "available": True,
        "repair_required": False,
        "callable_now": True,
        "will_be_hydrated_next_turn": False,
        "hydration_targets": [],
        "exec_runtime_policy": {
            "mode": "governed",
            "guardrails_enabled": True,
            "summary": "exec will execute shell commands with exec-side guardrails.",
        },
    }
    prior_payload["tool_context_fingerprint"] = build_tool_context_fingerprint(prior_payload)
    violations = await loop._load_tool_context_direct_read_violations(
        response_tool_calls=[SimpleNamespace(id="call-1", name="load_tool_context", arguments={"tool_id": "exec"})],
        task=SimpleNamespace(task_id="task-execution-refresh"),
        node=SimpleNamespace(node_id="node-execution-refresh", node_kind="execution"),
        message_history=[
            {
                "role": "tool",
                "tool_call_id": "call-old",
                "name": "load_tool_context",
                # 上一版正文确实留在上下文里（带块），只是修订不同 ⇒ 允许重读。
                "content": json.dumps(attach_tool_contract_block(prior_payload), ensure_ascii=False),
            }
        ],
        runtime_context={
            "session_key": "web:shared",
            "actor_role": "execution",
            "rbac_visible_tool_names": ["exec"],
            "callable_tool_names": ["load_tool_context", "exec"],
            "full_callable_tool_names": ["load_tool_context", "exec"],
        },
        tools={"load_tool_context": LoadToolContextTool(lambda: _InlineToolContextService())},
    )

    assert violations == []


def test_node_dynamic_contract_state_restores_skill_candidates_from_frame_after_stage_compaction() -> None:
    log_service = _FakeLogService()
    loop = ReActToolLoop(chat_backend=SimpleNamespace(), log_service=log_service, max_iterations=2)
    task_id = "task-stage-skill-restore"
    node = SimpleNamespace(task_id=task_id, node_id="node-stage-skill-restore", depth=0, node_kind="execution")
    log_service._store._node = SimpleNamespace(
        metadata={
            "execution_stages": {
                "stages": [
                    {
                    }
                ],
            }
        }
    )
    log_service.upsert_frame(
        task_id,
        {
            "node_id": node.node_id,
            "candidate_skill_items": [
                {
                    "skill_id": "tmux",
                    "description": "terminal workflow",
                }
            ],
            "selected_skill_ids": ["tmux"],
            "candidate_skill_ids": ["tmux"],
        },
    )
    stage_contract = NodeRuntimeToolContract(
        node_id=node.node_id,
        node_kind=node.node_kind,
        callable_tool_names=["filesystem_stat", "load_skill_context"],
        candidate_tool_names=["content"],
        visible_skills=[
            {
                "skill_id": "tmux",
                "display_name": "tmux",
                "description": "terminal workflow",
            }
        ],
        candidate_skill_ids=["tmux"],
        candidate_skill_items=[
            {
                "skill_id": "tmux",
                "description": "terminal workflow",
            }
        ],

        lightweight_tool_ids=[],
        selection_trace={},
    ).to_message()
    message_history = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": '{"task_id":"task-stage-skill-restore","goal":"demo"}'},
        stage_contract,
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call-stage-1",
                    "type": "function",
                    "function": {"name": "filesystem_stat", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "name": "filesystem_stat", "tool_call_id": "call-stage-1", "content": '{"ok": true}'},
    ]

    prepared = loop._prepare_messages(
        message_history,
        runtime_context={"task_id": task_id, "node_id": node.node_id},
    )

    assert extract_node_dynamic_contract_payload(prepared) is None

    refreshed = loop._refresh_node_dynamic_contract_message(
        node=node,
        message_history=prepared,
        tool_schema_selection={
            "tool_names": ["filesystem_stat", "load_skill_context"],
            "candidate_tool_names": ["content"],
            "lightweight_tool_ids": [],
            "trace": {},
        },

    )

    payload = extract_node_dynamic_contract_payload(refreshed)
    # 尾块退役：refresh 只剥离历史合同，状态从 frame 重建在构建层验证。
    assert payload is None
    contract = loop._build_node_dynamic_contract(
        node=node,
        message_history=prepared,
        tool_schema_selection={
            "tool_names": ["filesystem_stat", "load_skill_context"],
            "candidate_tool_names": ["content"],
            "lightweight_tool_ids": [],
            "trace": {},
        },
        runtime_context={"task_id": task_id, "node_id": node.node_id},
    )
    restored_payload = contract.to_message_payload()
    assert restored_payload["candidate_skills"] == [
        {
            "skill_id": "tmux",
            "description": "terminal workflow",
        }
    ]


def test_node_dynamic_contract_injection_only_strips_legacy_messages() -> None:
    contract = NodeRuntimeToolContract(
        node_id="node-1",
        node_kind="execution",
        callable_tool_names=["filesystem_stat"],
        candidate_tool_names=["filesystem_write"],
        visible_skills=[
            {
                "skill_id": "tmux",
                "display_name": "tmux",
                "description": "terminal workflow",
            }
        ],
        candidate_skill_ids=["tmux"],
        candidate_skill_items=[
            {
                "skill_id": "tmux",
                "description": "terminal workflow",
            }
        ],

        lightweight_tool_ids=["filesystem"],
        selection_trace={
            "mode": "execution_tool_selection",
            "full_callable_tool_names": ["filesystem_stat", "filesystem_write"],
            "stage_locked_to_filesystem_stat": True,
            "final_schema_chars": 4096,
        },
    )

    injected = inject_node_dynamic_contract_message(
        [
            {"role": "system", "content": "node system"},
            {"role": "user", "content": '{"prompt":"inspect repo"}'},
            {"role": "assistant", "content": "prior reasoning"},
        ],
        contract,
    )

    assert [item["role"] for item in injected] == ["system", "user", "assistant"]
    assert extract_node_dynamic_contract_payload(injected) is None


def test_node_token_compaction_keeps_append_notice_tail_before_compact_block() -> None:
    from main.runtime.append_notice_context import APPEND_NOTICE_TAIL_PREFIX

    contract_message = NodeRuntimeToolContract(
        node_id="node-token-compact",
        node_kind="execution",
        callable_tool_names=["submit_final_result"],
        candidate_tool_names=["filesystem_write"],
        visible_skills=[],
        candidate_skill_ids=[],
        candidate_skill_items=[],

        lightweight_tool_ids=[],
        selection_trace={},
    ).to_message()

    rewritten, payload = ReActToolLoop._rewrite_request_messages_for_token_compaction(
        node_id="node-token-compact",
        request_messages=[
            {"role": "system", "content": "node system"},
            {"role": "user", "content": '{"task_id":"task-token-compact","goal":"demo"}'},
            {
                "role": "assistant",
                "content": (
                    f"{APPEND_NOTICE_TAIL_PREFIX}\n"
                    '{"kind":"raw_notice_window","notice_count":1,"notices":[{"message":"keep me visible"}]}'
                ),
            },
            contract_message,
            {"role": "assistant", "content": "older history detail"},
            {"role": "assistant", "content": "recent tail detail"},
        ],
        recent_tail_count=1,
    )

    contents = [str(item.get("content") or "") for item in rewritten]
    notice_index = next(index for index, content in enumerate(contents) if content.startswith(APPEND_NOTICE_TAIL_PREFIX))
    compact_index = next(index for index, content in enumerate(contents) if content.startswith("[TOKEN_COMPACT_V2]"))

    assert rewritten[0]["role"] == "system"
    assert rewritten[1]["role"] == "user"
    assert notice_index < compact_index
    assert compact_index < len(rewritten) - 1
    # 旧合同尾块已退役：压缩后不再保留，也不再进元数据。
    assert extract_node_dynamic_contract_payload(rewritten) is None
    assert payload["node_id"] == "node-token-compact"
    assert payload["append_notice_tail_count"] == 1
    assert "contract_message_count" not in payload


def test_node_token_compaction_hoists_deep_raw_notice_windows_out_of_compressible_history() -> None:
    from main.runtime.append_notice_context import APPEND_NOTICE_TAIL_PREFIX

    raw_notice = {
        "role": "assistant",
        "content": (
            f"{APPEND_NOTICE_TAIL_PREFIX}\n"
            '{"kind": "raw_notice_window", "notice_count": 1, "notices": [{"message": "keep raw"}]}'
        ),
    }
    compressed_notice = {
        "role": "assistant",
        "content": (
            f"{APPEND_NOTICE_TAIL_PREFIX}\n"
            '{"kind": "compressed_notice_window", "notice_count": 2, "summary_text": "已消费汇总可压缩"}'
        ),
    }
    request_messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": '{"task_id":"t","goal":"g"}'},
        {"role": "assistant", "content": "history 0"},
        raw_notice,
        {"role": "assistant", "content": "history 1"},
        compressed_notice,
        *[{"role": "assistant", "content": f"tail {index}"} for index in range(12)],
    ]
    parts = ReActToolLoop._split_request_messages_for_token_compaction(
        request_messages=request_messages,
        recent_tail_count=8,
    )
    compressible = parts["compressible_history"]
    # 未消费 raw notice 被捞出；已消费 compressed_notice_window 留在可压缩区。
    assert raw_notice in parts["append_notice_tail"]
    assert raw_notice not in compressible
    assert compressed_notice in compressible

    rewritten, payload = ReActToolLoop._rewrite_request_messages_for_token_compaction(
        node_id="node-hoist",
        request_messages=request_messages,
        compressed_text="压缩摘要正文",
        recent_tail_count=8,
    )
    contents = [str(item.get("content") or "") for item in rewritten]
    raw_index = next(index for index, content in enumerate(contents) if "keep raw" in content)
    compact_index = next(
        index for index, content in enumerate(contents)
        if content.startswith("[TOKEN_COMPACT_V2]")
    )
    # 捞出的 raw notice 在压缩块之前原样保留；已消费汇总随历史一起被压缩吸收。
    assert raw_index < compact_index
    assert all("已消费汇总可压缩" not in content for content in contents)
    assert "压缩摘要正文" in contents[compact_index]
    assert payload["kind"] == "node_token_compaction_llm"


def test_node_token_compaction_bounds_oversized_tail_tool_results() -> None:
    import main.runtime.react_loop as react_loop_module

    char_limit = react_loop_module._NODE_TOKEN_COMPACTION_TAIL_CONTENT_CHAR_LIMIT
    oversized = "x" * (char_limit + 5000)
    request_messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": '{"task_id":"t","goal":"g"}'},
        *[{"role": "assistant", "content": f"filler {index}"} for index in range(10)],
        {
            "role": "tool",
            "content": oversized,
            "tool_call_id": "call:big",
            "name": "content_open",
        },
    ]
    rewritten, _payload = ReActToolLoop._rewrite_request_messages_for_token_compaction(
        node_id="node-bound",
        request_messages=request_messages,
        compressed_text="摘要",
        recent_tail_count=3,
    )
    tail_tool = next(
        item for item in rewritten
        if str(item.get("role") or "").strip().lower() == "tool"
    )
    bounded_text = str(tail_tool.get("content") or "")
    assert len(bounded_text) <= char_limit + 300
    assert "[内容已截断]" in bounded_text
    assert "请按上文其 ref 用 content_open/content_search 检索" in bounded_text


def test_align_compaction_keep_recent_keeps_tool_pairs_intact() -> None:
    # 压缩保留尾部的边界不得落在工具调用组中间：尾部首条是 tool 结果时，
    # 向前扩展边界直到声明它的 assistant 消息（事故 task:25745b5268dc）。
    from g3ku.runtime.tool_history import align_compaction_keep_recent

    body = [
        {"role": "assistant", "content": "older"},
        {
            "role": "assistant",
            "content": "declare pair",
            "tool_calls": [
                {"id": "call_pair_a", "type": "function", "function": {"name": "exec", "arguments": "{}"}},
                {"id": "call_pair_b", "type": "function", "function": {"name": "exec", "arguments": "{}"}},
            ],
        },
        {"role": "tool", "content": "result a", "tool_call_id": "call_pair_a"},
        {"role": "tool", "content": "result b", "tool_call_id": "call_pair_b"},
    ]
    # 边界落在第一/第二个结果上：扩展到包含声明（两结果 -> 声明）。
    assert align_compaction_keep_recent(body, 1) == 3
    assert align_compaction_keep_recent(body, 2) == 3
    # 边界本就在 assistant 声明处：不回退也不过度扩展。
    assert align_compaction_keep_recent(body, 3) == 3
    assert align_compaction_keep_recent(body, 4) == 4
    # 退化：整段都是 tool 结果时扩展至整个 body（交由"无可压缩历史"分支）。
    all_tool = [{"role": "tool", "content": "r", "tool_call_id": f"c{i}"} for i in range(3)]
    assert align_compaction_keep_recent(all_tool, 1) == 3
    # 空输入与零尾部。
    assert align_compaction_keep_recent([], 5) == 0
    assert align_compaction_keep_recent(body, 0) == 0


def test_node_token_compaction_tail_boundary_aligns_tool_call_pairs() -> None:
    # 事故 task:25745b5268dc 的形状：固定条数尾部切片落在并行工具批次中间、
    # 声明 assistant 落入压缩区时，重写请求会出现孤儿工具结果并触发熔断。
    # 修复后边界向前对齐，声明与全部结果一并保留在压缩块之后的尾部。
    request_messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": '{"task_id":"t-align","goal":"g"}'},
        *[{"role": "assistant", "content": f"filler {index}"} for index in range(3)],
        {
            "role": "assistant",
            "content": "declare pair",
            "tool_calls": [
                {"id": "call_pair_a", "type": "function", "function": {"name": "exec", "arguments": "{}"}},
                {"id": "call_pair_b", "type": "function", "function": {"name": "exec", "arguments": "{}"}},
            ],
        },
        {"role": "tool", "content": "result a", "tool_call_id": "call_pair_a"},
        {"role": "tool", "content": "result b", "tool_call_id": "call_pair_b"},
    ]
    # 未对齐的尾部条数 2 会把边界切在两个结果之间（声明落入压缩区）。
    rewritten, payload = ReActToolLoop._rewrite_request_messages_for_token_compaction(
        node_id="node-align",
        request_messages=request_messages,
        compressed_text="摘要",
        recent_tail_count=2,
    )
    analysis = analyze_tool_call_history(rewritten)
    assert analysis.orphan_tool_result_ids == []
    compact_index = next(
        index
        for index, item in enumerate(rewritten)
        if str(item.get("content") or "").startswith("[TOKEN_COMPACT_V2]")
    )
    tail = rewritten[compact_index + 1 :]
    tail_call_ids = [str(tc.get("id")) for item in tail for tc in list(item.get("tool_calls") or [])]
    tail_result_ids = [str(item.get("tool_call_id")) for item in tail if item.get("role") == "tool"]
    assert "call_pair_a" in tail_call_ids and "call_pair_b" in tail_call_ids
    assert tail_result_ids == ["call_pair_a", "call_pair_b"]
    assert payload["kind"] == "node_token_compaction_llm"


@pytest.mark.asyncio
async def test_react_loop_execution_role_keeps_watchdog_inline_without_handoff(tmp_path) -> None:
    store = SQLiteTaskStore(tmp_path / "runtime.sqlite3")
    artifact_store = TaskArtifactStore(artifact_dir=tmp_path / "artifacts", store=store)
    log_service = _FakeLogService()
    log_service._content_store = ContentNavigationService(
        workspace=tmp_path,
        artifact_store=artifact_store,
        artifact_lookup=artifact_store,
    )
    heartbeat = _HeartbeatRecorder()
    loop = ReActToolLoop(chat_backend=SimpleNamespace(), log_service=log_service, max_iterations=2)

    try:
        rendered = await loop._execute_tool(
            tools={"slow_complete": _SlowCompleteTool()},
            tool_name="slow_complete",
            arguments={},
            runtime_context={
                "task_id": "task-1",
                "node_id": "node-1",
                "actor_role": "execution",
                "session_key": "web:test-execution-node",
                "tool_watchdog": {
                    "poll_interval_seconds": 0.01,
                    "handoff_after_seconds": 0.02,
                },
                "tool_snapshot_supplier": lambda: {"status": "running"},
                "loop": SimpleNamespace(web_session_heartbeat=heartbeat),
            },
        )

        assert rendered == "done"
        assert heartbeat.terminal_calls == []
    finally:
        store.close()


@pytest.mark.asyncio
async def test_react_loop_execution_watchdog_poll_can_pause_long_tool_midflight() -> None:
    class _LongRunningTool(Tool):
        @property
        def name(self) -> str:
            return "long_running"

        @property
        def description(self) -> str:
            return "Run long enough for inline watchdog polling to observe pause."

        @property
        def parameters(self) -> dict[str, object]:
            return {"type": "object", "properties": {}, "required": []}

        async def execute(self, **kwargs) -> str:
            _ = kwargs
            await asyncio.sleep(0.5)
            return "done"

    log_service = _FakeLogService()
    loop = ReActToolLoop(chat_backend=SimpleNamespace(), log_service=log_service, max_iterations=2)

    async def _request_pause() -> None:
        await asyncio.sleep(0.03)
        log_service._store._task.pause_requested = True

    pause_task = asyncio.create_task(_request_pause())
    with pytest.raises(TaskPausedError):
        await loop._execute_tool_raw(
            tools={"long_running": _LongRunningTool()},
            tool_name="long_running",
            arguments={},
            runtime_context={
                "task_id": "task-pause-inline-watchdog",
                "node_id": "node-pause-inline-watchdog",
                "node_kind": "execution",
                "actor_role": "execution",
                "session_key": "web:execution-inline-watchdog",
                "tool_watchdog": {
                    "poll_interval_seconds": 0.01,
                    "handoff_after_seconds": 0.05,
                },
                "tool_snapshot_supplier": lambda: {"status": "running"},
            },
        )
    await pause_task


@pytest.mark.asyncio
async def test_react_loop_externalizes_tool_messages_with_canonical_ref(tmp_path) -> None:
    store = SQLiteTaskStore(tmp_path / "runtime.sqlite3")
    artifact_store = TaskArtifactStore(artifact_dir=tmp_path / "artifacts", store=store)
    log_service = _FakeLogService()
    log_service._content_store = ContentNavigationService(
        workspace=tmp_path,
        artifact_store=artifact_store,
        artifact_lookup=artifact_store,
    )
    loop = ReActToolLoop(chat_backend=SimpleNamespace(), log_service=log_service, max_iterations=2)

    try:
        inner = log_service._content_store.maybe_externalize_text(
            "alpha\nneedle\nomega\n",
            runtime={"task_id": "task-1", "node_id": "node-1"},
            display_name="inner",
            source_kind="node_output",
            force=True,
        )
        wrapped = log_service._content_store.maybe_externalize_text(
            json.dumps(inner.to_dict(), ensure_ascii=False),
            runtime={"task_id": "task-1", "node_id": "node-1"},
            display_name="wrapped",
            source_kind="tool_result:content",
            force=True,
        )

        assert inner is not None
        assert wrapped is not None

        tool_payload = {
            "ok": True,
            "ref": wrapped.ref,
            "requested_ref": wrapped.ref,
            "resolved_ref": inner.ref,
            "wrapper_ref": wrapped.ref,
            "wrapper_depth": 1,
            "start_line": 1,
            "end_line": 280,
            "excerpt": "\n".join(f"line {index:03d} needle context" for index in range(280)),
        }

        rendered = loop._render_tool_message_content(
            json.dumps(tool_payload, ensure_ascii=False),
            runtime_context={"task_id": "task-1", "node_id": "node-1", "actor_role": "execution"},
            tool_name="content",
        )

        payload = parse_content_envelope(rendered)

        assert payload is not None
        assert payload.ref.startswith("artifact:")
        assert payload.ref != inner.ref
        assert payload.resolved_ref == inner.ref
    finally:
        store.close()


@pytest.mark.asyncio
async def test_react_loop_externalized_tool_result_envelope_shows_status_and_preview(tmp_path) -> None:
    store = SQLiteTaskStore(tmp_path / "runtime.sqlite3")
    artifact_store = TaskArtifactStore(artifact_dir=tmp_path / "artifacts", store=store)
    log_service = _FakeLogService()
    log_service._content_store = ContentNavigationService(
        workspace=tmp_path,
        artifact_store=artifact_store,
        artifact_lookup=artifact_store,
    )
    loop = ReActToolLoop(chat_backend=SimpleNamespace(), log_service=log_service, max_iterations=2)

    try:
        spawn_result = json.dumps(
            {
                "children": [
                    {"goal": f"child goal {index}", "node_output": "done " * 4000}
                    for index in range(6)
                ]
            },
            ensure_ascii=False,
        )
        rendered = loop._render_tool_message_content(
            spawn_result,
            runtime_context={"task_id": "task-1", "node_id": "node-1", "actor_role": "execution"},
            tool_name="spawn_child_nodes",
            delivery_metadata={"invocation_text": "spawn_child_nodes({\"children\": [ ... huge args ... ]})"},
        )
        payload = parse_content_envelope(rendered)
        assert payload is not None
        # 信封必须显式说明调用成功、给出外置 ref/体量/预览与 content_open 指引，
        # 且不再回显调用入参（避免模型把"结果被外部化"误读成"调用因载荷过大失败"）。
        assert "Tool call succeeded: tool:spawn_child_nodes result externalized" in payload.summary
        assert f'content_open(ref="{payload.ref}")' in payload.summary
        assert "Preview:" in payload.summary
        assert "child goal 0" in payload.summary
        assert "Invocation:" not in payload.summary

        error_rendered = loop._render_tool_message_content(
            "Error: spawn rejected\n" + ("diagnostic line\n" * 900),
            runtime_context={"task_id": "task-1", "node_id": "node-1", "actor_role": "execution"},
            tool_name="spawn_child_nodes",
        )
        error_payload = parse_content_envelope(error_rendered)
        assert error_payload is not None
        assert "Tool call failed: tool:spawn_child_nodes error output externalized" in error_payload.summary
        assert f'content_open(ref="{error_payload.ref}")' in error_payload.summary
    finally:
        store.close()


@pytest.mark.asyncio
async def test_inline_even_when_large_for_manifest_backed_tool_result(tmp_path) -> None:
    store = SQLiteTaskStore(tmp_path / "runtime.sqlite3")
    artifact_store = TaskArtifactStore(artifact_dir=tmp_path / "artifacts", store=store)
    log_service = _FakeLogService()
    log_service._content_store = ContentNavigationService(
        workspace=tmp_path,
        artifact_store=artifact_store,
        artifact_lookup=artifact_store,
    )
    loop = ReActToolLoop(chat_backend=SimpleNamespace(), log_service=log_service, max_iterations=2)
    descriptor = ToolResourceDescriptor(
        kind=ResourceKind.TOOL,
        name="inline_manifest_tool",
        description="Inline large tool results when manifest opt-in is enabled.",
        root=tmp_path,
        manifest_path=tmp_path / "resource.yaml",
        fingerprint="test-fingerprint",
        tool_result_inline_full=True,
        metadata={"tool_result_inline_full": True},
    )
    tool = ManifestBackedTool(descriptor, _LargeInlineManifestTool())

    try:
        rendered = await loop._execute_tool(
            tools={"inline_manifest_tool": tool},
            tool_name="inline_manifest_tool",
            arguments={},
            runtime_context={"task_id": "task-1", "node_id": "node-1", "actor_role": "execution"},
        )

        assert parse_content_envelope(rendered) is None
        payload = json.loads(rendered)
        assert payload["stdout"].startswith("inline line 000")
        assert len(payload["stdout"].splitlines()) == 240
    finally:
        store.close()


@pytest.mark.asyncio
async def test_react_loop_uses_latest_model_refs_from_supplier_between_turns() -> None:
    model_ref_calls: list[list[str]] = []
    current_refs = ['old-model']

    class _Backend:
        def __init__(self) -> None:
            self._calls = 0

        async def chat(self, **kwargs):
            self._calls += 1
            model_ref_calls.append(list(kwargs.get("model_refs") or []))
            if self._calls == 1:
                return LLMResponse(
                    content="",
                    tool_calls=[ToolCallRequest(id="call-1", name="flip_refs", arguments={})],
                    finish_reason="tool_calls",
                    usage={"input_tokens": 8, "output_tokens": 3},
                )
            return LLMResponse(
                content="",
                tool_calls=[
                    ToolCallRequest(
                        id="call:final",
                        name="submit_final_result",
                        arguments={
                            "status": "failed",
                            "delivery_status": "blocked",
                            "summary": "done",
                            "answer": "",
                            "evidence": [],
                            "remaining_work": [],
                            "blocking_reason": "done",
                        },
                    )
                ],
                finish_reason="tool_calls",
                usage={"input_tokens": 8, "output_tokens": 3},
            )

    class _FlipRefsTool(Tool):
        @property
        def name(self) -> str:
            return "flip_refs"

        @property
        def description(self) -> str:
            return "flip refs"

        @property
        def parameters(self) -> dict[str, object]:
            return {"type": "object", "properties": {}}

        async def execute(self, **kwargs):
            _ = kwargs
            current_refs[:] = ["new-model"]
            return "refs flipped"

    loop = ReActToolLoop(chat_backend=_Backend(), log_service=_FakeLogService(), max_iterations=3)

    result = await loop.run(
        task=SimpleNamespace(task_id='task-1'),
        node=SimpleNamespace(node_id='node-1', depth=0, node_kind='execution'),
        messages=[
            {"role": "system", "content": "system"},
            {"role": "user", "content": '{"task_id":"task-1","goal":"demo"}'},
        ],
        tools={
            "flip_refs": _FlipRefsTool(),
            "submit_final_result": _submit_final_result_tool(),
        },
        model_refs=['old-model'],
        model_refs_supplier=lambda: list(current_refs),
        runtime_context={'task_id': 'task-1', 'node_id': 'node-1'},
        max_iterations=3,
    )

    assert result.summary == "done"
    assert model_ref_calls == [["old-model"], ["new-model"]]


@pytest.mark.asyncio
async def test_enrich_node_messages_injects_all_visible_skills_without_memory_overlay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selector_calls: list[dict[str, object]] = []
    retrieve_block_calls: list[dict[str, object]] = []

    class _MemoryManager:
        def _feature_enabled(self, key: str) -> bool:
            return key == "unified_context"

        async def retrieve_block(self, **kwargs):
            retrieve_block_calls.append(dict(kwargs))
            return "memory block"

    async def _fake_build_node_context_selection(**kwargs):
        selector_calls.append(dict(kwargs))
        return NodeContextSelectionResult(
            selected_skill_ids=["skill-creator", "tmux"],
            selected_tool_names=["filesystem_write"],
        )

    monkeypatch.setattr(
        runtime_service_module,
        "build_node_context_selection",
        _fake_build_node_context_selection,
    )

    service = object.__new__(MainRuntimeService)
    service.memory_manager = _MemoryManager()
    service.list_visible_tools = lambda *, actor_role, session_id: [
        SimpleNamespace(tool_id='filesystem_write'),
    ]
    service.list_visible_skill_resources = lambda *, actor_role, session_id: [
        SimpleNamespace(skill_id='skill-creator', display_name='skill-creator', description='skill creator'),
        SimpleNamespace(skill_id='tmux', display_name='tmux', description='terminal workflow'),
    ]
    service.list_effective_tool_names = lambda *, actor_role, session_id: ['filesystem_write']

    task = SimpleNamespace(
        session_id="web:ceo-origin",
        metadata={"memory_scope": {"channel": "web", "chat_id": "shared"}},
    )
    node = SimpleNamespace(prompt="where is the plan", goal="where is the plan", node_kind="execution")

    enriched = await service._enrich_node_messages(
        task=task,
        node=node,
        messages=[
            {"role": "system", "content": "base prompt"},
            {"role": "user", "content": '{"prompt":"where is the plan"}'},
        ],
    )

    assert selector_calls == [
        {
            "visible_skills": service.list_visible_skill_resources(actor_role="execution", session_id="web:ceo-origin"),
            "visible_tool_names": ["filesystem_write"],
        }
    ]
    assert retrieve_block_calls == []
    # 尾块退役：enrichment 仍计算选择结果，但不再把合同消息写进正文。
    assert extract_node_dynamic_contract_payload(enriched) is None
    assert enriched[0]["content"] == "base prompt"


@pytest.mark.asyncio
async def test_enrich_node_messages_uses_selector_narrowed_skills_without_memory_overlay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    retrieve_block_calls: list[dict[str, object]] = []

    class _MemoryManager:
        def _feature_enabled(self, key: str) -> bool:
            return key == "unified_context"

        async def retrieve_block(self, **kwargs):
            retrieve_block_calls.append(dict(kwargs))
            return "semantic block"

    async def _fake_build_node_context_selection(**kwargs):
        _ = kwargs
        return NodeContextSelectionResult(
            selected_skill_ids=["tmux", "skill-creator"],
            selected_tool_names=["content_open"],
            candidate_skill_ids=["tmux", "skill-creator"],
            candidate_tool_names=["content_open"],
        )

    monkeypatch.setattr(
        runtime_service_module,
        "build_node_context_selection",
        _fake_build_node_context_selection,
    )

    service = object.__new__(MainRuntimeService)
    service.memory_manager = _MemoryManager()
    service.list_visible_tools = lambda *, actor_role, session_id: [
        SimpleNamespace(tool_id='content_open'),
    ]
    service.list_visible_skill_resources = lambda *, actor_role, session_id: [
        SimpleNamespace(skill_id='skill-creator', display_name='skill-creator', description='skill creator'),
        SimpleNamespace(skill_id='tmux', display_name='tmux', description='terminal workflow'),
    ]
    service.list_effective_tool_names = lambda *, actor_role, session_id: ['content_open']

    task = SimpleNamespace(
        session_id="web:ceo-origin",
        metadata={"memory_scope": {"channel": "web", "chat_id": "shared"}},
    )
    node = SimpleNamespace(prompt="terminal workflow", goal="terminal workflow", node_kind="execution")

    enriched = await service._enrich_node_messages(
        task=task,
        node=node,
        messages=[
            {"role": "system", "content": "base prompt"},
            {"role": "user", "content": '{"prompt":"terminal workflow"}'},
        ],
    )

    assert retrieve_block_calls == []
    # 尾块退役：选择结果仍由 enrichment 计算，模型面不再注入合同消息。
    assert extract_node_dynamic_contract_payload(enriched) is None
    assert enriched[0]["content"] == "base prompt"


@pytest.mark.asyncio
async def test_enrich_node_messages_keeps_contract_absent_tools_out_of_callable_at_bootstrap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """引导装配手上没有当次请求视图，在场判据按空集走：callable 只剩常驻内置。

    治理可见但契约不在场的工具必须留在候选（load 或冲动调用把契约带回来），不许因为它
    在选中集里就当成"这一跳可直接调用"——派发字典是节点唯一的执行准入。
    """

    async def _fake_build_node_context_selection(**kwargs):
        _ = kwargs
        return NodeContextSelectionResult(
            selected_skill_ids=["tmux"],
            selected_tool_names=["content_open", "exec"],
            candidate_skill_ids=["tmux"],
            candidate_tool_names=["content_open", "exec", "filesystem_write"],
        )

    monkeypatch.setattr(
        runtime_service_module,
        "build_node_context_selection",
        _fake_build_node_context_selection,
    )

    service = object.__new__(MainRuntimeService)
    service.memory_manager = SimpleNamespace(_feature_enabled=lambda _key: False)
    service._node_context_selection_cache = {}
    service.log_service = _FakeLogService()
    service.log_service.execution_stage_prompt_payload = lambda task_id, node_id: {
        'has_active_stage': True,
        'transition_required': False,
        'active_stage': {'stage_id': 'stage-1'},
    }
    service.list_visible_tools = lambda *, actor_role, session_id: [
        SimpleNamespace(tool_id='content_open'),
        SimpleNamespace(tool_id='filesystem_write'),
    ]
    service.list_visible_skill_resources = lambda *, actor_role, session_id: [
        SimpleNamespace(skill_id='tmux', display_name='tmux', description='terminal workflow'),
    ]
    service.list_effective_tool_names = lambda *, actor_role, session_id: ['content_open', 'filesystem_write', 'load_tool_context']
    task = SimpleNamespace(task_id='task-presence-payload', session_id="web:ceo-origin", metadata={})
    node = SimpleNamespace(
        task_id='task-presence-payload',
        node_id='node-presence-payload',
        prompt="terminal workflow",
        goal="terminal workflow",
        node_kind="execution",
        can_spawn_children=False,
    )

    enriched = await service._enrich_node_messages(
        task=task,
        node=node,
        messages=[
            {"role": "system", "content": "base prompt"},
            {"role": "user", "content": '{"prompt":"terminal workflow"}'},
        ],
    )

    # 尾块退役：准入判据仍在 enrichment 内计算，合同消息不再注入正文。
    assert extract_node_dynamic_contract_payload(enriched) is None


@pytest.mark.asyncio
async def test_enrich_node_messages_includes_exec_runtime_policy_in_dynamic_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _fake_build_node_context_selection(**kwargs):
        _ = kwargs
        return NodeContextSelectionResult(
            selected_skill_ids=["tmux"],
            selected_tool_names=["content"],
            candidate_skill_ids=["tmux"],
            candidate_tool_names=["content"],
        )

    monkeypatch.setattr(
        runtime_service_module,
        "build_node_context_selection",
        _fake_build_node_context_selection,
    )

    service = object.__new__(MainRuntimeService)
    service.memory_manager = SimpleNamespace(_feature_enabled=lambda _key: False)
    service._node_context_selection_cache = {}
    service.log_service = _FakeLogService()
    service.log_service.execution_stage_prompt_payload = lambda task_id, node_id: {
        'has_active_stage': True,
        'transition_required': False,
        'active_stage': {'stage_id': 'stage-1'},
    }
    service.list_visible_tools = lambda *, actor_role, session_id: [
        SimpleNamespace(tool_id='content'),
        SimpleNamespace(tool_id='exec_runtime'),
    ]
    service.list_visible_skill_resources = lambda *, actor_role, session_id: [
        SimpleNamespace(skill_id='tmux', display_name='tmux', description='terminal workflow'),
    ]
    service.list_effective_tool_names = lambda *, actor_role, session_id: ['content', 'exec', 'load_tool_context']
    service._current_exec_runtime_policy_payload = lambda: {
        'mode': 'full_access',
        'guardrails_enabled': False,
        'summary': 'exec will execute shell commands without exec-side guardrails.',
    }
    task = SimpleNamespace(task_id='task-exec-policy', session_id="web:ceo-origin", metadata={})
    node = SimpleNamespace(
        task_id='task-exec-policy',
        node_id='node-exec-policy',
        prompt="inspect repo",
        goal="inspect repo",
        node_kind="execution",
        can_spawn_children=False,
    )

    enriched = await service._enrich_node_messages(
        task=task,
        node=node,
        messages=[
            {"role": "system", "content": "base prompt"},
            {"role": "user", "content": '{"prompt":"inspect repo"}'},
        ],
    )

    dynamic_payload = extract_node_dynamic_contract_payload(enriched)
    # 尾块已退役：exec 策略由节点头部快照（react_loop 重印边界）承载，
    # enrichment 不再追加合同消息。
    assert dynamic_payload is None




@pytest.mark.asyncio
async def test_enrich_node_messages_never_injects_memory_retrieval_overlay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    retrieve_block_calls: list[dict[str, object]] = []

    class _MemoryManager:
        def _feature_enabled(self, key: str) -> bool:
            return key == "unified_context"

        async def retrieve_block(self, **kwargs):
            retrieve_block_calls.append(dict(kwargs))
            return "unexpected memory block"

    async def _fake_build_node_context_selection(**kwargs):
        _ = kwargs
        return NodeContextSelectionResult(
            selected_skill_ids=["tmux"],
            selected_tool_names=["filesystem"],
            candidate_skill_ids=["tmux"],
            candidate_tool_names=["filesystem"],
        )

    monkeypatch.setattr(
        runtime_service_module,
        "build_node_context_selection",
        _fake_build_node_context_selection,
    )

    service = object.__new__(MainRuntimeService)
    service.memory_manager = _MemoryManager()
    service.list_visible_tools = lambda *, actor_role, session_id: [
        SimpleNamespace(tool_id='filesystem'),
    ]
    service.list_visible_skill_resources = lambda *, actor_role, session_id: [
        SimpleNamespace(skill_id='skill-creator', display_name='skill-creator', description='skill'),
        SimpleNamespace(skill_id='tmux', display_name='tmux', description='terminal workflow'),
    ]
    service.list_effective_tool_names = lambda *, actor_role, session_id: ['filesystem']

    task = SimpleNamespace(
        session_id="web:ceo-origin",
        metadata={"memory_scope": {"channel": "web", "chat_id": "shared"}},
    )
    node = SimpleNamespace(prompt="where is the plan", goal="where is the plan", node_kind="execution")

    enriched = await service._enrich_node_messages(
        task=task,
        node=node,
        messages=[
            {"role": "system", "content": "base prompt"},
            {"role": "user", "content": '{"prompt":"where is the plan"}'},
        ],
    )

    # 契约尾块现在也落在 user 角色（线体只许一份首位 system），排除运行时注入块之后
    # 剩下的才是真实用户消息——本用例要证的是"记忆检索 overlay 不冒充用户消息"。
    user_messages = [
        message
        for message in enriched
        if message.get("role") == "user" and not is_node_dynamic_contract_message(message)
    ]
    assert len(user_messages) == 1
    # 尾块退役：不再有动态合同消息；真实用户消息仍然只有一条。
    assert extract_node_dynamic_contract_payload(enriched) is None
    assert retrieve_block_calls == []


@pytest.mark.asyncio
async def test_enrich_node_messages_still_applies_selector_when_unified_context_feature_is_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    retrieve_block_calls: list[dict[str, object]] = []

    class _MemoryManager:
        def _feature_enabled(self, key: str) -> bool:
            _ = key
            return False

        async def retrieve_block(self, **kwargs):
            retrieve_block_calls.append(dict(kwargs))
            return "unexpected memory block"

    async def _fake_build_node_context_selection(**kwargs):
        _ = kwargs
        return NodeContextSelectionResult(
            selected_skill_ids=["tmux"],
            selected_tool_names=["content"],
            candidate_skill_ids=["tmux"],
            candidate_tool_names=["content"],
        )

    monkeypatch.setattr(
        runtime_service_module,
        "build_node_context_selection",
        _fake_build_node_context_selection,
    )

    service = object.__new__(MainRuntimeService)
    service.memory_manager = _MemoryManager()
    service.list_visible_tools = lambda *, actor_role, session_id: [
        SimpleNamespace(tool_id='content'),
    ]
    service.list_visible_skill_resources = lambda *, actor_role, session_id: [
        SimpleNamespace(skill_id='skill-creator', display_name='skill-creator', description='skill creator'),
        SimpleNamespace(skill_id='tmux', display_name='tmux', description='terminal workflow'),
    ]
    service.list_effective_tool_names = lambda *, actor_role, session_id: ['content']

    task = SimpleNamespace(
        session_id="web:ceo-origin",
        metadata={"memory_scope": {"channel": "web", "chat_id": "shared"}},
    )
    node = SimpleNamespace(prompt="terminal workflow", goal="terminal workflow", node_kind="execution")

    enriched = await service._enrich_node_messages(
        task=task,
        node=node,
        messages=[
            {"role": "system", "content": "base prompt"},
            {"role": "user", "content": '{"prompt":"terminal workflow"}'},
        ],
    )

    # 尾块退役：选择结果仍在 enrichment 内计算，合同消息不再注入正文。
    assert extract_node_dynamic_contract_payload(enriched) is None
    assert retrieve_block_calls == []

@pytest.mark.asyncio
async def test_execute_tool_blocks_repeated_overflowed_search() -> None:
    class _ContentTool(Tool):
        @property
        def name(self) -> str:
            return 'content'

        @property
        def description(self) -> str:
            return 'Content stub'

        @property
        def parameters(self) -> dict[str, object]:
            return {
                    'type': 'object',
                    'properties': {
                        'action': {'type': 'string', 'description': 'action'},
                        'path': {'type': 'string', 'description': 'path'},
                        'query': {'type': 'string', 'description': 'query'},
                },
                'required': ['action', 'path', 'query'],
            }

        async def execute(self, **kwargs):
            raise AssertionError(f'overflowed search should not execute again: {kwargs!r}')

    loop = ReActToolLoop(chat_backend=SimpleNamespace(), log_service=_FakeLogService(), max_iterations=2)
    result = await loop._execute_tool(
        tools={'content': _ContentTool()},
        tool_name='content',
        arguments={'action': 'search', 'path': '/tmp/demo.py', 'query': 'needle'},
        runtime_context={'prior_overflow_signatures': ['content|/tmp/demo.py|needle']},
    )

    assert result == 'Error: previous search overflowed; refine query before retrying'


@pytest.mark.asyncio
async def test_execute_tool_passes_runtime_context_to_name_mangled_class_tool() -> None:
    class _RuntimeCaptureTool(Tool):
        @property
        def name(self) -> str:
            return 'capture_runtime'

        @property
        def description(self) -> str:
            return 'Capture runtime context'

        @property
        def parameters(self) -> dict[str, object]:
            return {
                'type': 'object',
                'properties': {
                    'value': {'type': 'string', 'description': 'value'},
                },
                'required': ['value'],
            }

        async def execute(self, value: str, __g3ku_runtime: dict[str, object] | None = None, **kwargs):
            runtime = __g3ku_runtime if isinstance(__g3ku_runtime, dict) else {}
            return json.dumps(
                {
                    'value': value,
                    'current_tool_call_id': runtime.get('current_tool_call_id'),
                    'kwargs_runtime': kwargs.get('__g3ku_runtime'),
                },
                ensure_ascii=False,
            )

    loop = ReActToolLoop(chat_backend=SimpleNamespace(), log_service=_FakeLogService(), max_iterations=2)
    result = await loop._execute_tool(
        tools={'capture_runtime': _RuntimeCaptureTool()},
        tool_name='capture_runtime',
        arguments={'value': 'demo'},
        runtime_context={'current_tool_call_id': 'call:test-runtime'},
    )

    payload = json.loads(result)
    assert payload['value'] == 'demo'
    assert payload['current_tool_call_id'] == 'call:test-runtime'
    assert payload['kwargs_runtime'] is None


def test_apply_temporary_system_overlay_keeps_base_messages_untouched() -> None:
    loop = ReActToolLoop(chat_backend=SimpleNamespace(), log_service=_FakeLogService(), max_iterations=2)
    base_messages = [
        {'role': 'system', 'content': 'base system'},
        {'role': 'user', 'content': 'base user'},
    ]
    overlay = 'temporary system overlay'

    request_messages = loop._apply_temporary_system_overlay(base_messages, overlay_text=overlay)

    assert base_messages[0]['content'] == 'base system'
    assert request_messages[0] == base_messages[0]
    assert request_messages[1] == base_messages[1]
    assert len(request_messages) == 3
    assert request_messages[-1]['role'] == 'user'
    assert request_messages[-1]['content'] == 'System note for this turn only:\ntemporary system overlay'


def test_detect_xml_pseudo_tool_call_matches_supported_shape_only() -> None:
    matched = ReActToolLoop._detect_xml_pseudo_tool_call(
        '<minimax:tool_call><invoke name="filesystem"><parameter name="path">docs/a.md</parameter></invoke></minimax:tool_call>',
        allowed_tool_names={'filesystem', 'submit_final_result'},
    )
    rejected_unknown = ReActToolLoop._detect_xml_pseudo_tool_call(
        '<minimax:tool_call><invoke name="unknown_tool"><parameter name="path">docs/a.md</parameter></invoke></minimax:tool_call>',
        allowed_tool_names={'filesystem', 'submit_final_result'},
    )
    rejected_plain_xml = ReActToolLoop._detect_xml_pseudo_tool_call(
        '<note><path>docs/a.md</path></note>',
        allowed_tool_names={'filesystem', 'submit_final_result'},
    )

    assert matched is not None
    assert matched['tool_names'] == ['filesystem']
    assert rejected_unknown is None
    assert rejected_plain_xml is None


def test_extract_tool_calls_from_xml_pseudo_content_coerces_schema_typed_parameters() -> None:
    extracted = extract_tool_calls_from_xml_pseudo_content(
        (
            '<minimax:tool_call>'
            '<invoke name="typed_schema_tool">'
            '<parameter name="count">5</parameter>'
            '<parameter name="enabled">true</parameter>'
            '<parameter name="metadata">{"source":"xml"}</parameter>'
            '<parameter name="tags">["alpha","beta"]</parameter>'
            '<parameter name="note">hello &amp; goodbye</parameter>'
            '</invoke>'
            '</minimax:tool_call>'
        ),
        visible_tools={"typed_schema_tool": _TypedSchemaTool()},
    )

    assert extracted.matched is True
    assert extracted.issue == ""
    assert len(extracted.tool_calls) == 1
    arguments = extracted.tool_calls[0].arguments
    assert arguments["count"] == 5
    assert arguments["enabled"] is True
    assert arguments["metadata"] == {"source": "xml"}
    assert arguments["tags"] == ["alpha", "beta"]
    assert arguments["note"] == "hello & goodbye"


def test_extract_tool_calls_from_xml_pseudo_content_returns_issue_for_invalid_parameter_type() -> None:
    extracted = extract_tool_calls_from_xml_pseudo_content(
        '<minimax:tool_call><invoke name="count_tool"><parameter name="count">oops</parameter></invoke></minimax:tool_call>',
        visible_tools={"count_tool": _CountTool([])},
    )

    assert extracted.matched is True
    assert extracted.tool_calls == []
    assert 'must be an integer' in extracted.issue


def test_extract_tool_calls_from_xml_pseudo_content_is_all_or_nothing_for_multi_invoke() -> None:
    extracted = extract_tool_calls_from_xml_pseudo_content(
        (
            '<minimax:tool_call>'
            '<invoke name="count_tool"><parameter name="count">1</parameter></invoke>'
            '<invoke name="count_tool"><parameter name="count">oops</parameter></invoke>'
            '</minimax:tool_call>'
        ),
        visible_tools={"count_tool": _CountTool([])},
    )

    assert extracted.matched is True
    assert extracted.tool_calls == []
    assert 'must be an integer' in extracted.issue


@pytest.mark.asyncio
async def test_react_loop_directly_executes_xml_reply_without_repair() -> None:
    calls: list[list[dict[str, object]]] = []
    executed: list[tuple[str, str]] = []

    class _Backend:
        def __init__(self) -> None:
            self._responses = [
                LLMResponse(
                    content='<minimax:tool_call><invoke name="record_one"><parameter name="value">hello</parameter></invoke></minimax:tool_call>',
                    tool_calls=[],
                    finish_reason='stop',
                    usage={'input_tokens': 8, 'output_tokens': 3},
                ),
                LLMResponse(
                    content='',
                    tool_calls=[
                        ToolCallRequest(
                            id='call:final',
                            name='submit_final_result',
                            arguments={
                                'status': 'success',
                                'delivery_status': 'final',
                                'summary': 'done',
                                'answer': 'done',
                                'evidence': [{'kind': 'artifact', 'note': 'json repair path'}],
                                'remaining_work': [],
                                'blocking_reason': '',
                            },
                        )
                    ],
                    finish_reason='tool_calls',
                    usage={'input_tokens': 8, 'output_tokens': 3},
                ),
            ]

        async def chat(self, **kwargs):
            calls.append([dict(item) for item in list(kwargs.get('messages') or [])])
            return self._responses.pop(0)

    loop = ReActToolLoop(chat_backend=_Backend(), log_service=_FakeLogService(), max_iterations=3)
    result = await loop.run(
        task=SimpleNamespace(task_id='task-xml-json'),
        node=SimpleNamespace(node_id='node-xml-json', depth=0, node_kind='execution'),
        messages=[
            {'role': 'system', 'content': 'system'},
            {'role': 'user', 'content': '{"task_id":"task-xml-json","goal":"demo"}'},
        ],
        tools={
            'record_one': _RecordingTool('record_one', executed),
            'submit_final_result': _submit_final_result_tool(),
        },
        model_refs=['fake'],
        runtime_context={'task_id': 'task-xml-json', 'node_id': 'node-xml-json'},
        max_iterations=3,
    )

    assert result.status == 'success'
    assert executed == [('record_one', 'hello')]
    assert len(calls) == 2
    second_request = calls[1]
    assert not any('XML-style pseudo tool calling' in str(item.get('content') or '') for item in second_request)
    assistant_turns = [item for item in second_request if item.get('role') == 'assistant']
    assert any(
        str((((tool_call or {}).get('function') or {}).get('name') or '')).strip() == 'record_one'
        for message in assistant_turns
        for tool_call in list(message.get('tool_calls') or [])
    )
    assert all(str(item.get('content') or '') != '<minimax:tool_call><invoke name="record_one"><parameter name="value">hello</parameter></invoke></minimax:tool_call>' for item in assistant_turns)


@pytest.mark.asyncio
async def test_react_loop_repairs_xml_reply_with_json_array_payload() -> None:
    executed: list[int] = []

    class _Backend:
        def __init__(self) -> None:
            self._responses = [
                LLMResponse(
                    content=(
                        '<minimax:tool_call>'
                        '<invoke name="count_tool"><parameter name="count">1</parameter></invoke>'
                        '<invoke name="count_tool"><parameter name="count">oops</parameter></invoke>'
                        '</minimax:tool_call>'
                    ),
                    tool_calls=[],
                    finish_reason='stop',
                    usage={'input_tokens': 8, 'output_tokens': 3},
                ),
                LLMResponse(
                    content='[{"name":"count_tool","arguments":{"count":1}},{"name":"count_tool","arguments":{"count":2}}]',
                    tool_calls=[],
                    finish_reason='stop',
                    usage={'input_tokens': 8, 'output_tokens': 3},
                ),
                LLMResponse(
                    content='',
                    tool_calls=[
                        ToolCallRequest(
                            id='call:final',
                            name='submit_final_result',
                            arguments={
                                'status': 'success',
                                'delivery_status': 'final',
                                'summary': 'done',
                                'answer': 'done',
                                'evidence': [{'kind': 'artifact', 'note': 'json array repair path'}],
                                'remaining_work': [],
                                'blocking_reason': '',
                            },
                        )
                    ],
                    finish_reason='tool_calls',
                    usage={'input_tokens': 8, 'output_tokens': 3},
                ),
            ]

        async def chat(self, **kwargs):
            _ = kwargs
            return self._responses.pop(0)

    loop = ReActToolLoop(chat_backend=_Backend(), log_service=_FakeLogService(), max_iterations=4)
    result = await loop.run(
        task=SimpleNamespace(task_id='task-xml-array'),
        node=SimpleNamespace(node_id='node-xml-array', depth=0, node_kind='execution'),
        messages=[
            {'role': 'system', 'content': 'system'},
            {'role': 'user', 'content': '{"task_id":"task-xml-array","goal":"demo"}'},
        ],
        tools={
            'count_tool': _CountTool(executed),
            'submit_final_result': _submit_final_result_tool(),
        },
        model_refs=['fake'],
        runtime_context={'task_id': 'task-xml-array', 'node_id': 'node-xml-array'},
        max_iterations=4,
    )

    assert result.status == 'success'
    assert executed == [1, 2]


@pytest.mark.asyncio
async def test_react_loop_directly_executes_xml_submit_final_result() -> None:
    calls: list[list[dict[str, object]]] = []

    class _Backend:
        def __init__(self) -> None:
            self._responses = [
                LLMResponse(
                    content='<minimax:tool_call><invoke name="submit_final_result"><parameter name="status">success</parameter><parameter name="delivery_status">final</parameter><parameter name="summary">done</parameter><parameter name="answer">done</parameter><parameter name="evidence">[]</parameter><parameter name="remaining_work">[]</parameter><parameter name="blocking_reason"></parameter></invoke></minimax:tool_call>',
                    tool_calls=[],
                    finish_reason='stop',
                    usage={'input_tokens': 8, 'output_tokens': 3},
                ),
            ]

        async def chat(self, **kwargs):
            calls.append([dict(item) for item in list(kwargs.get('messages') or [])])
            return self._responses.pop(0)

    loop = ReActToolLoop(chat_backend=_Backend(), log_service=_FakeLogService(), max_iterations=2)
    result = await loop.run(
        task=SimpleNamespace(task_id='task-xml-final'),
        node=SimpleNamespace(node_id='node-xml-final', depth=0, node_kind='execution'),
        messages=[
            {'role': 'system', 'content': 'system'},
            {'role': 'user', 'content': '{"task_id":"task-xml-final","goal":"demo"}'},
        ],
        tools={'submit_final_result': _submit_final_result_tool()},
        model_refs=['fake'],
        runtime_context={'task_id': 'task-xml-final', 'node_id': 'node-xml-final'},
        max_iterations=2,
    )

    assert result.status == 'success'
    assert result.answer == 'done'
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_react_loop_repairs_xml_reply_with_json_object_tool_payload_after_local_extraction_fails() -> None:
    calls: list[list[dict[str, object]]] = []
    executed: list[int] = []

    class _Backend:
        def __init__(self) -> None:
            self._responses = [
                LLMResponse(
                    content='<minimax:tool_call><invoke name="count_tool"><parameter name="count">oops</parameter></invoke></minimax:tool_call>',
                    tool_calls=[],
                    finish_reason='stop',
                    usage={'input_tokens': 8, 'output_tokens': 3},
                ),
                LLMResponse(
                    content='{"name":"count_tool","arguments":{"count":2}}',
                    tool_calls=[],
                    finish_reason='stop',
                    usage={'input_tokens': 8, 'output_tokens': 3},
                ),
                LLMResponse(
                    content='',
                    tool_calls=[
                        ToolCallRequest(
                            id='call:final',
                            name='submit_final_result',
                            arguments={
                                'status': 'success',
                                'delivery_status': 'final',
                                'summary': 'done',
                                'answer': 'done',
                                'evidence': [{'kind': 'artifact', 'note': 'json repair path'}],
                                'remaining_work': [],
                                'blocking_reason': '',
                            },
                        )
                    ],
                    finish_reason='tool_calls',
                    usage={'input_tokens': 8, 'output_tokens': 3},
                ),
            ]

        async def chat(self, **kwargs):
            calls.append([dict(item) for item in list(kwargs.get('messages') or [])])
            return self._responses.pop(0)

    loop = ReActToolLoop(chat_backend=_Backend(), log_service=_FakeLogService(), max_iterations=4)
    result = await loop.run(
        task=SimpleNamespace(task_id='task-xml-json'),
        node=SimpleNamespace(node_id='node-xml-json', depth=0, node_kind='execution'),
        messages=[
            {'role': 'system', 'content': 'system'},
            {'role': 'user', 'content': '{"task_id":"task-xml-json","goal":"demo"}'},
        ],
        tools={
            'count_tool': _CountTool(executed),
            'submit_final_result': _submit_final_result_tool(),
        },
        model_refs=['fake'],
        runtime_context={'task_id': 'task-xml-json', 'node_id': 'node-xml-json'},
        max_iterations=4,
    )

    assert result.status == 'success'
    assert executed == [2]
    assert len(calls) == 3
    second_request = calls[1]
    assert any('XML-style pseudo tool calling' in str(item.get('content') or '') for item in second_request)
    assert any('must be an integer' in str(item.get('content') or '') for item in second_request)


@pytest.mark.asyncio
async def test_react_loop_fails_after_three_xml_repair_attempts() -> None:
    class _Backend:
        def __init__(self) -> None:
            self.turn = 0

        async def chat(self, **kwargs):
            _ = kwargs
            self.turn += 1
            if self.turn == 1:
                content = '<minimax:tool_call><invoke name="submit_final_result"><parameter name="status">success</parameter></invoke></minimax:tool_call>'
            elif self.turn == 2:
                content = 'still not valid json or structured tool call'
            else:
                content = '<minimax:tool_call><invoke name="submit_final_result"><parameter name="status">success</parameter></invoke></minimax:tool_call>'
            return LLMResponse(
                content=content,
                tool_calls=[],
                finish_reason='stop',
                usage={'input_tokens': 8, 'output_tokens': 3},
            )

    backend = _Backend()
    loop = ReActToolLoop(chat_backend=backend, log_service=_FakeLogService(), max_iterations=5)
    result = await loop.run(
        task=SimpleNamespace(task_id='task-xml-fail'),
        node=SimpleNamespace(node_id='node-xml-fail', depth=0, node_kind='execution'),
        messages=[
            {'role': 'system', 'content': 'system'},
            {'role': 'user', 'content': '{"task_id":"task-xml-fail","goal":"demo"}'},
        ],
        tools={'submit_final_result': _submit_final_result_tool()},
        model_refs=['fake'],
        runtime_context={'task_id': 'task-xml-fail', 'node_id': 'node-xml-fail'},
        max_iterations=5,
    )

    assert backend.turn == 3
    assert result.status == 'failed'
    assert result.delivery_status == 'blocked'
    assert 'XML pseudo tool-call repair failed 3 consecutive times' in result.blocking_reason


def test_execution_result_protocol_message_avoids_partial_guidance() -> None:
    message = ReActToolLoop._result_protocol_message(node_kind='execution')

    assert 'delivery_status="partial"' not in message
    assert 'submit_final_result' in message
    assert 'remaining_work' in message
    assert 'If you are ending the node now' in message
    assert 'If the task is not complete yet' in message


def test_execution_result_contract_violation_message_keeps_workflow_open() -> None:
    message = ReActToolLoop._result_contract_violation_message(
        ['summary must not be empty'],
        node_kind='execution',
    )

    assert 'submit_final_result' in message
    assert 'If you are ending the node now' in message
    assert 'do not force another premature final submission' in message


def test_execution_repair_prompts_lead_with_continue_and_ban_success_placeholder() -> None:
    """事故 task:61fdc3580cdd：弱模型在修复提示引导下提交 summary="placeholder" 的
    success+final，被握手当作真实交付送进验收。

    契约：修复/违规提示必须把「未完成就继续用工具」放在 schema 模板之前，
    并显式禁止 success 占位提交；node_execution.md 有对称反占位条款与
    success 结束前自检。
    """
    from main.prompts import load_prompt

    protocol = ReActToolLoop._result_protocol_message(node_kind='execution')
    violation = ReActToolLoop._result_contract_violation_message(
        ['summary must not be empty'],
        node_kind='execution',
    )
    guidance = ReActToolLoop._result_repair_guidance(node_kind='execution')

    for message in (protocol, violation):
        # 继续执行的指令必须在「收尾模板」之前，避免把照抄字段的出口先递给弱模型。
        assert message.index('If the task is not complete yet') < message.index('If you are ending the node now')
        assert 'placeholder' in message

    assert 'There is no status field' in guidance
    assert 'keep working with tools' in guidance

    prompt = load_prompt('node_execution.md')
    assert '提交即表示' in prompt
    assert '绝不允许填入' in prompt


def test_acceptance_result_contract_violation_message_uses_three_tier_verdict() -> None:
    message = ReActToolLoop._result_contract_violation_message(
        ['summary must not be empty'],
        node_kind='acceptance',
    )

    assert 'delivery_status="partial"' not in message
    assert 'submit_final_result' in message
    assert 'reject_no_progress' in message
    assert 'feedback' in message


@pytest.mark.asyncio
async def test_react_loop_failed_final_payload_is_accepted_immediately_without_repair_retry() -> None:
    calls: list[list[dict[str, object]]] = []

    class _Backend:
        def __init__(self) -> None:
            self._responses = [
                LLMResponse(
                    content='',
                    tool_calls=[
                        ToolCallRequest(
                            id='call:invalid-final',
                            name='submit_final_result',
                            arguments={
                                'status': 'failed',
                                'delivery_status': 'final',
                                'summary': 'done',
                                'answer': '',
                                'evidence': [],
                                'remaining_work': [],
                                'blocking_reason': '',
                            },
                        )
                    ],
                    finish_reason='tool_calls',
                    usage={'input_tokens': 8, 'output_tokens': 3},
                ),
            ]

        async def chat(self, **kwargs):
            calls.append([dict(item) for item in list(kwargs.get('messages') or [])])
            return self._responses.pop(0)

    loop = ReActToolLoop(chat_backend=_Backend(), log_service=_FakeLogService(), max_iterations=3)
    result = await loop.run(
        task=SimpleNamespace(task_id='task-1'),
        node=SimpleNamespace(node_id='node-1', depth=0, node_kind='execution'),
        messages=[
            {'role': 'system', 'content': 'system'},
            {'role': 'user', 'content': '{"task_id":"task-1","goal":"demo"}'},
        ],
        tools={'submit_final_result': _submit_final_result_tool()},
        model_refs=['fake'],
        runtime_context={'task_id': 'task-1', 'node_id': 'node-1'},
        max_iterations=3,
    )

    # 旧形状的 failed 提交按兼容映射收成一次正常完成，不再触发修复重试。
    assert result.status == 'success'
    assert result.delivery_status == 'final'
    assert len(calls) == 1
    assert result.summary == 'done'


@pytest.mark.asyncio
async def test_react_loop_retries_empty_response_until_valid_result(monkeypatch: pytest.MonkeyPatch) -> None:
    requests: list[dict[str, object]] = []
    sleep_calls: list[float] = []

    async def _fake_sleep(delay: float) -> None:
        sleep_calls.append(float(delay))

    monkeypatch.setattr('main.runtime.react_loop.asyncio.sleep', _fake_sleep)

    class _Backend:
        def __init__(self) -> None:
            self.turn = 0

        async def chat(self, **kwargs):
            requests.append(dict(kwargs))
            self.turn += 1
            if self.turn < 3:
                return LLMResponse(
                    content='',
                    tool_calls=[],
                    finish_reason='stop',
                    usage={},
                )
            return LLMResponse(
                content='',
                tool_calls=[
                    ToolCallRequest(
                        id='call:final',
                        name='submit_final_result',
                        arguments={
                            'status': 'success',
                            'delivery_status': 'final',
                            'summary': 'done',
                            'answer': 'done',
                            'evidence': [],
                            'remaining_work': [],
                            'blocking_reason': '',
                        },
                    )
                ],
                finish_reason='tool_calls',
                usage={'input_tokens': 8, 'output_tokens': 3},
            )

    loop = ReActToolLoop(chat_backend=_Backend(), log_service=_FakeLogService(), max_iterations=2)
    result = await loop.run(
        task=SimpleNamespace(task_id='task-empty-retry'),
        node=SimpleNamespace(node_id='node-empty-retry', depth=0, node_kind='execution'),
        messages=[
            {'role': 'system', 'content': 'system'},
            {'role': 'user', 'content': '{"task_id":"task-empty-retry","goal":"demo"}'},
        ],
        tools={'submit_final_result': _submit_final_result_tool()},
        model_refs=['fake'],
        runtime_context={'task_id': 'task-empty-retry', 'node_id': 'node-empty-retry'},
        max_iterations=2,
    )

    assert result.status == 'success'
    assert result.answer == 'done'
    assert len(requests) == 3
    assert sleep_calls == [1.0, 2.0]


@pytest.mark.asyncio
async def test_react_loop_bounces_plain_text_then_accepts_final_submission() -> None:
    requests: list[dict[str, object]] = []

    class _Backend:
        def __init__(self) -> None:
            self.turn = 0

        async def chat(self, **kwargs):
            requests.append(kwargs)
            self.turn += 1
            if self.turn == 1:
                return LLMResponse(
                    content='Final structured summary line 1\n- detail a\n- detail b',
                    tool_calls=[],
                    finish_reason='stop',
                    usage={'input_tokens': 8, 'output_tokens': 3},
                )
            return LLMResponse(
                content='',
                tool_calls=[
                    ToolCallRequest(
                        id='call:final',
                        name='submit_final_result',
                        arguments={
                            'status': 'success',
                            'delivery_status': 'final',
                            'summary': 'done',
                            'answer': 'done after bounce',
                            'evidence': [],
                            'remaining_work': [],
                            'blocking_reason': '',
                        },
                    )
                ],
                finish_reason='tool_calls',
                usage={'input_tokens': 8, 'output_tokens': 3},
            )

    loop = ReActToolLoop(chat_backend=_Backend(), log_service=_FakeLogService(), max_iterations=3)
    result = await loop.run(
        task=SimpleNamespace(task_id='task-plain-final'),
        node=SimpleNamespace(node_id='node-plain-final', depth=0, node_kind='execution'),
        messages=[
            {'role': 'system', 'content': 'system'},
            {'role': 'user', 'content': '{"task_id":"task-plain-final","goal":"demo"}'},
        ],
        tools={'submit_final_result': _submit_final_result_tool()},
        model_refs=['fake'],
        runtime_context={'task_id': 'task-plain-final', 'node_id': 'node-plain-final'},
        max_iterations=3,
    )

    assert result.status == 'success'
    assert result.delivery_status == 'final'
    assert result.answer == 'done after bounce'
    assert result.summary == 'done'
    assert 'does not treat it as a delivery' in _request_message_text(requests[1])


@pytest.mark.asyncio
async def test_react_loop_bounces_plain_text_after_tool_usage() -> None:
    executed: list[int] = []

    class _Backend:
        def __init__(self) -> None:
            self.turn = 0

        async def chat(self, **kwargs):
            _ = kwargs
            self.turn += 1
            if self.turn == 1:
                return LLMResponse(
                    content='',
                    tool_calls=[
                        ToolCallRequest(
                            id='call:count',
                            name='count_tool',
                            arguments={'count': 2},
                        )
                    ],
                    finish_reason='tool_calls',
                    usage={'input_tokens': 8, 'output_tokens': 3},
                )
            if self.turn == 2:
                return LLMResponse(
                    content='Final answer after tool usage',
                    tool_calls=[],
                    finish_reason='stop',
                    usage={'input_tokens': 8, 'output_tokens': 3},
                )
            return LLMResponse(
                content='',
                tool_calls=[
                    ToolCallRequest(
                        id='call:final',
                        name='submit_final_result',
                        arguments={
                            'status': 'success',
                            'delivery_status': 'final',
                            'summary': 'done',
                            'answer': 'done after tool bounce',
                            'evidence': [],
                            'remaining_work': [],
                            'blocking_reason': '',
                        },
                    )
                ],
                finish_reason='tool_calls',
                usage={'input_tokens': 8, 'output_tokens': 3},
            )

    loop = ReActToolLoop(chat_backend=_Backend(), log_service=_FakeLogService(), max_iterations=4)
    result = await loop.run(
        task=SimpleNamespace(task_id='task-plain-final-with-tools'),
        node=SimpleNamespace(node_id='node-plain-final-with-tools', depth=0, node_kind='execution'),
        messages=[
            {'role': 'system', 'content': 'system'},
            {'role': 'user', 'content': '{"task_id":"task-plain-final-with-tools","goal":"demo"}'},
        ],
        tools={
            'count_tool': _CountTool(executed),
            'submit_final_result': _submit_final_result_tool(),
        },
        model_refs=['fake'],
        runtime_context={'task_id': 'task-plain-final-with-tools', 'node_id': 'node-plain-final-with-tools'},
        max_iterations=4,
    )

    assert executed == [2]
    assert result.status == 'success'
    assert result.answer == 'done after tool bounce'


@pytest.mark.asyncio
async def test_react_loop_allows_progress_text_with_tool_calls() -> None:
    requests: list[dict[str, object]] = []
    executed: list[int] = []

    class _Backend:
        def __init__(self) -> None:
            self.turn = 0

        async def chat(self, **kwargs):
            requests.append(kwargs)
            self.turn += 1
            if self.turn == 1:
                return LLMResponse(
                    content='正在核对第二批文件',
                    tool_calls=[
                        ToolCallRequest(
                            id='call:count',
                            name='count_tool',
                            arguments={'count': 1},
                        )
                    ],
                    finish_reason='tool_calls',
                    usage={'input_tokens': 8, 'output_tokens': 3},
                )
            return LLMResponse(
                content='',
                tool_calls=[
                    ToolCallRequest(
                        id='call:final',
                        name='submit_final_result',
                        arguments={
                            'status': 'success',
                            'delivery_status': 'final',
                            'summary': 'done',
                            'answer': 'done',
                            'evidence': [],
                            'remaining_work': [],
                            'blocking_reason': '',
                        },
                    )
                ],
                finish_reason='tool_calls',
                usage={'input_tokens': 8, 'output_tokens': 3},
            )

    loop = ReActToolLoop(chat_backend=_Backend(), log_service=_FakeLogService(), max_iterations=3)
    result = await loop.run(
        task=SimpleNamespace(task_id='task-progress-text'),
        node=SimpleNamespace(node_id='node-progress-text', depth=0, node_kind='execution'),
        messages=[
            {'role': 'system', 'content': 'system'},
            {'role': 'user', 'content': '{"task_id":"task-progress-text","goal":"demo"}'},
        ],
        tools={
            'count_tool': _CountTool(executed),
            'submit_final_result': _submit_final_result_tool(),
        },
        model_refs=['fake'],
        runtime_context={'task_id': 'task-progress-text', 'node_id': 'node-progress-text'},
        max_iterations=3,
    )

    assert executed == [1]
    assert result.status == 'success'
    assert '正在核对第二批文件' in _request_message_text(requests[1])
    assert 'does not treat it as a delivery' not in _request_message_text(requests[1])


@pytest.mark.asyncio
async def test_react_loop_pauses_after_three_plain_text_replies_without_a_stage() -> None:
    turns: list[int] = []

    class _Backend:
        async def chat(self, **kwargs):
            _ = kwargs
            turns.append(1)
            return LLMResponse(
                content='接下来我要检查列错位。',
                tool_calls=[],
                finish_reason='stop',
                usage={'input_tokens': 8, 'output_tokens': 3},
            )

    loop = ReActToolLoop(chat_backend=_Backend(), log_service=_FakeLogService(), max_iterations=4)
    result = await loop.run(
        task=SimpleNamespace(task_id='task-plain-text-strike-no-stage'),
        node=SimpleNamespace(node_id='node-plain-text-strike-no-stage', depth=0, node_kind='execution'),
        messages=[
            {'role': 'system', 'content': 'system'},
            {'role': 'user', 'content': '{"task_id":"task-plain-text-strike-no-stage","goal":"demo"}'},
        ],
        tools={'submit_final_result': _submit_final_result_tool()},
        model_refs=['fake'],
        runtime_context={'task_id': 'task-plain-text-strike-no-stage', 'node_id': 'node-plain-text-strike-no-stage'},
        max_iterations=4,
    )

    assert len(turns) == _PLAIN_TEXT_REPLY_STRIKE_LIMIT
    assert result.status == 'failed'
    assert result.delivery_status == 'blocked'
    assert result.summary == 'plain-text reply guard triggered'
    assert result.failure_disposition == 'pause'
    assert 'submit_final_result' in str(result.blocking_reason)


def _midstage_stage_gate(*args, **kwargs) -> dict[str, object]:
    _ = args, kwargs
    return {
        'has_active_stage': True,
        'transition_required': False,
        'active_stage': {
            'stage_id': 'stage-mid',
            'stage_index': 3,
            'mode': '自主执行',
            'status': '进行中',
            'stage_goal': '重建 6 列 CSV 并重新打包 ZIP',
            'tool_round_budget': 14,
            'tool_rounds_used': 3,
            'rounds': [{'round_index': 1, 'tool_names': ['exec'], 'budget_counted': True}],
        },
    }


def _request_message_text(request: dict[str, object]) -> str:
    parts: list[str] = []
    for item in list(request.get('messages') or []):
        if isinstance(item, dict):
            parts.append(str(item.get('content') or ''))
    return '\n'.join(parts)


@pytest.mark.asyncio
async def test_react_loop_bounces_midstage_plain_text_instead_of_auto_wrapping() -> None:
    requests: list[dict[str, object]] = []

    class _Backend:
        def __init__(self) -> None:
            self.turn = 0

        async def chat(self, **kwargs):
            requests.append(dict(kwargs))
            self.turn += 1
            if self.turn <= 2:
                return LLMResponse(
                    content='我需要彻底重做。先探明备份文件位置，然后重建 CSV。',
                    tool_calls=[],
                    finish_reason='stop',
                    usage={'input_tokens': 8, 'output_tokens': 3},
                )
            return LLMResponse(
                content='',
                tool_calls=[
                    ToolCallRequest(
                        id='call:real-final',
                        name='submit_final_result',
                        arguments={
                            'status': 'success',
                            'delivery_status': 'final',
                            'summary': '真交付',
                            'answer': 'CSV 与 ZIP 已按备份重建',
                        },
                    )
                ],
                finish_reason='tool_calls',
                usage={'input_tokens': 8, 'output_tokens': 3},
            )

    log_service = _FakeLogService()
    log_service.execution_stage_gate_snapshot = _midstage_stage_gate
    loop = ReActToolLoop(chat_backend=_Backend(), log_service=log_service, max_iterations=5)
    result = await loop.run(
        task=SimpleNamespace(task_id='task-midstage-plain-text'),
        node=SimpleNamespace(node_id='node-midstage-plain-text', depth=0, node_kind='execution'),
        messages=[
            {'role': 'system', 'content': 'system'},
            {'role': 'user', 'content': '{"task_id":"task-midstage-plain-text","goal":"demo"}'},
        ],
        tools={'submit_final_result': _submit_final_result_tool()},
        model_refs=['fake'],
        runtime_context={'task_id': 'task-midstage-plain-text', 'node_id': 'node-midstage-plain-text'},
        max_iterations=5,
    )

    assert result.summary == '真交付'
    assert result.answer == 'CSV 与 ZIP 已按备份重建'
    assert 'auto-wrapped' not in str(result.summary)
    assert 'does not treat it as a delivery' not in _request_message_text(requests[0])
    assert 'does not treat it as a delivery' in _request_message_text(requests[1])
    assert 'does not treat it as a delivery' in _request_message_text(requests[2])
    assert len(requests) == 3


@pytest.mark.asyncio
async def test_react_loop_pauses_after_three_consecutive_plain_text_replies() -> None:
    turns: list[int] = []

    class _Backend:
        async def chat(self, **kwargs):
            _ = kwargs
            turns.append(1)
            return LLMResponse(
                content='接下来我要检查列错位。',
                tool_calls=[],
                finish_reason='stop',
                usage={'input_tokens': 8, 'output_tokens': 3},
            )

    log_service = _FakeLogService()
    log_service.execution_stage_gate_snapshot = _midstage_stage_gate
    loop = ReActToolLoop(chat_backend=_Backend(), log_service=log_service, max_iterations=6)
    result = await loop.run(
        task=SimpleNamespace(task_id='task-plain-text-strike'),
        node=SimpleNamespace(node_id='node-plain-text-strike', depth=0, node_kind='execution'),
        messages=[
            {'role': 'system', 'content': 'system'},
            {'role': 'user', 'content': '{"task_id":"task-plain-text-strike","goal":"demo"}'},
        ],
        tools={'submit_final_result': _submit_final_result_tool()},
        model_refs=['fake'],
        runtime_context={'task_id': 'task-plain-text-strike', 'node_id': 'node-plain-text-strike'},
        max_iterations=6,
    )

    assert len(turns) == _PLAIN_TEXT_REPLY_STRIKE_LIMIT
    assert result.status == 'failed'
    assert result.delivery_status == 'blocked'
    assert result.summary == 'plain-text reply guard triggered'
    assert result.failure_disposition == 'pause'
    assert 'submit_final_result' in str(result.blocking_reason)


@pytest.mark.asyncio
async def test_react_loop_normalizes_sparse_success_final_result_payload_after_tool_usage() -> None:
    executed: list[int] = []

    class _Backend:
        def __init__(self) -> None:
            self.turn = 0

        async def chat(self, **kwargs):
            _ = kwargs
            self.turn += 1
            if self.turn == 1:
                return LLMResponse(
                    content='',
                    tool_calls=[
                        ToolCallRequest(
                            id='call:count',
                            name='count_tool',
                            arguments={'count': 1},
                        )
                    ],
                    finish_reason='tool_calls',
                    usage={'input_tokens': 8, 'output_tokens': 3},
                )
            return LLMResponse(
                content='',
                tool_calls=[
                    ToolCallRequest(
                        id='call:sparse-final',
                        name='submit_final_result',
                        arguments={
                            'status': 'success',
                            'delivery_status': 'final',
                            'summary': 'done',
                            'answer': 'done',
                        },
                    )
                ],
                finish_reason='tool_calls',
                usage={'input_tokens': 8, 'output_tokens': 3},
            )

    loop = ReActToolLoop(chat_backend=_Backend(), log_service=_FakeLogService(), max_iterations=3)
    result = await loop.run(
        task=SimpleNamespace(task_id='task-sparse-final'),
        node=SimpleNamespace(node_id='node-sparse-final', depth=0, node_kind='execution'),
        messages=[
            {'role': 'system', 'content': 'system'},
            {'role': 'user', 'content': '{"task_id":"task-sparse-final","goal":"demo"}'},
        ],
        tools={
            'count_tool': _CountTool(executed),
            'submit_final_result': _submit_final_result_tool(),
        },
        model_refs=['fake'],
        runtime_context={'task_id': 'task-sparse-final', 'node_id': 'node-sparse-final'},
        max_iterations=3,
    )

    assert executed == [1]
    assert result.status == 'success'
    assert result.answer == 'done'
    assert result.remaining_work == []
    assert result.blocking_reason == ''
    assert result.evidence


@pytest.mark.asyncio
async def test_react_loop_infers_evidence_kind_for_submit_final_result_payload() -> None:
    class _Backend:
        async def chat(self, **kwargs):
            _ = kwargs
            return LLMResponse(
                content='',
                tool_calls=[
                    ToolCallRequest(
                        id='call:evidence-kind-final',
                        name='submit_final_result',
                        arguments={
                            'status': 'success',
                            'delivery_status': 'final',
                            'summary': 'done',
                            'answer': 'done',
                            'evidence': [
                                {
                                    'ref': 'artifact:artifact:demo-ref',
                                    'note': 'artifact evidence without explicit kind',
                                }
                            ],
                            'remaining_work': [],
                            'blocking_reason': '',
                        },
                    )
                ],
                finish_reason='tool_calls',
                usage={'input_tokens': 8, 'output_tokens': 3},
            )

    loop = ReActToolLoop(chat_backend=_Backend(), log_service=_FakeLogService(), max_iterations=2)
    result = await loop.run(
        task=SimpleNamespace(task_id='task-evidence-kind-final'),
        node=SimpleNamespace(node_id='node-evidence-kind-final', depth=0, node_kind='execution'),
        messages=[
            {'role': 'system', 'content': 'system'},
            {'role': 'user', 'content': '{"task_id":"task-evidence-kind-final","goal":"demo"}'},
        ],
        tools={'submit_final_result': _submit_final_result_tool()},
        model_refs=['fake'],
        runtime_context={'task_id': 'task-evidence-kind-final', 'node_id': 'node-evidence-kind-final'},
        max_iterations=2,
    )

    assert result.status == 'success'
    assert len(result.evidence) == 1
    assert result.evidence[0].kind == 'artifact'
    assert result.evidence[0].ref == 'artifact:artifact:demo-ref'


@pytest.mark.asyncio
async def test_react_loop_recovers_raw_final_result_json_after_protocol_repair() -> None:
    requests: list[dict[str, object]] = []

    class _Backend:
        def __init__(self) -> None:
            self._responses = [
                LLMResponse(
                    content='{"status":"success","delivery_status":"final","summary":"done","answer":"done","evidence":[],"remaining_work":[],"blocking_reason":""}',
                    tool_calls=[],
                    finish_reason='stop',
                    usage={'input_tokens': 8, 'output_tokens': 3},
                ),
                LLMResponse(
                    content='{"status":"success","delivery_status":"final","summary":"done","answer":"done","evidence":[],"remaining_work":[],"blocking_reason":""}',
                    tool_calls=[],
                    finish_reason='stop',
                    usage={'input_tokens': 8, 'output_tokens': 3},
                ),
            ]

        async def chat(self, **kwargs):
            requests.append(dict(kwargs))
            return self._responses.pop(0)

    loop = ReActToolLoop(chat_backend=_Backend(), log_service=_FakeLogService(), max_iterations=3)
    result = await loop.run(
        task=SimpleNamespace(task_id='task-raw-final-json'),
        node=SimpleNamespace(node_id='node-raw-final-json', depth=0, node_kind='execution', goal='demo'),
        messages=[
            {'role': 'system', 'content': 'system'},
            {'role': 'user', 'content': '{"task_id":"task-raw-final-json","goal":"demo"}'},
        ],
        tools={'submit_final_result': _submit_final_result_tool()},
        model_refs=['fake'],
        runtime_context={'task_id': 'task-raw-final-json', 'node_id': 'node-raw-final-json'},
        max_iterations=3,
    )

    assert result.status == 'success'
    assert result.answer == 'done'
    assert len(requests) == 2
    assert requests[0].get('tool_choice') is None
    assert requests[1].get('tool_choice') == {
        'type': 'function',
        'function': {'name': 'submit_final_result'},
    }


@pytest.mark.asyncio
async def test_react_loop_ignores_persisted_invalid_final_submission_count_and_accepts_current_final_payload() -> None:
    class _Backend:
        def __init__(self) -> None:
            self.turn = 0

        async def chat(self, **kwargs):
            _ = kwargs
            self.turn += 1
            if self.turn == 1:
                return LLMResponse(
                    content='',
                    tool_calls=[
                        ToolCallRequest(
                            id='call:invalid-final',
                            name='submit_final_result',
                            arguments={
                                'status': 'failed',
                                'delivery_status': 'final',
                                'summary': 'still invalid',
                                'answer': '',
                                'evidence': [],
                                'remaining_work': [],
                                'blocking_reason': '',
                            },
                        )
                    ],
                    finish_reason='tool_calls',
                    usage={'input_tokens': 8, 'output_tokens': 3},
                )
            raise AssertionError('backend should not receive a second repair attempt')

    backend = _Backend()
    log_service = _FakeLogService()
    log_service.upsert_frame(
        'task-persisted-invalid-final',
        {
            'node_id': 'node-persisted-invalid-final',
            'phase': 'before_model',
            'invalid_final_submission_count': 4,
            'last_invalid_final_submission_reason': 'persisted invalid final result',
            'last_contract_violations': ['execution failed result requires delivery_status=blocked'],
        },
    )
    loop = ReActToolLoop(chat_backend=backend, log_service=log_service, max_iterations=3)
    result = await loop.run(
        task=SimpleNamespace(task_id='task-persisted-invalid-final'),
        node=SimpleNamespace(node_id='node-persisted-invalid-final', depth=0, node_kind='execution'),
        messages=[
            {'role': 'system', 'content': 'system'},
            {'role': 'user', 'content': '{"task_id":"task-persisted-invalid-final","goal":"demo"}'},
        ],
        tools={'submit_final_result': _submit_final_result_tool()},
        model_refs=['fake'],
        runtime_context={'task_id': 'task-persisted-invalid-final', 'node_id': 'node-persisted-invalid-final'},
        max_iterations=3,
    )

    # 旧形状的 failed 提交按兼容映射收成一次正常完成；持久化的旧拒收计数不再生效。
    assert result.status == 'success'
    assert result.delivery_status == 'final'
    assert result.summary == 'still invalid'
    assert backend.turn == 1


@pytest.mark.asyncio
async def test_react_loop_retries_provider_chain_exhaustion_until_success(monkeypatch: pytest.MonkeyPatch) -> None:
    requests: list[dict[str, object]] = []
    sleep_calls: list[float] = []

    async def _fake_sleep(delay: float) -> None:
        sleep_calls.append(float(delay))

    monkeypatch.setattr('main.runtime.react_loop.asyncio.sleep', _fake_sleep)

    class _Backend:
        def __init__(self) -> None:
            self.turn = 0

        async def chat(self, **kwargs):
            requests.append(dict(kwargs))
            self.turn += 1
            if self.turn < 3:
                raise RuntimeError('Model provider call failed after exhausting the configured fallback chain.')
            return LLMResponse(
                content='',
                tool_calls=[
                    ToolCallRequest(
                        id='call:final',
                        name='submit_final_result',
                        arguments={
                            'status': 'success',
                            'delivery_status': 'final',
                            'summary': 'done',
                            'answer': 'done',
                            'evidence': [],
                            'remaining_work': [],
                            'blocking_reason': '',
                        },
                    )
                ],
                finish_reason='tool_calls',
                usage={'input_tokens': 8, 'output_tokens': 3},
            )

    loop = ReActToolLoop(chat_backend=_Backend(), log_service=_FakeLogService(), max_iterations=3)
    result = await loop.run(
        task=SimpleNamespace(task_id='task-provider-retry'),
        node=SimpleNamespace(node_id='node-provider-retry', depth=0, node_kind='execution'),
        messages=[
            {'role': 'system', 'content': 'system'},
            {'role': 'user', 'content': '{"task_id":"task-provider-retry","goal":"demo"}'},
        ],
        tools={'submit_final_result': _submit_final_result_tool()},
        model_refs=['fake'],
        runtime_context={'task_id': 'task-provider-retry', 'node_id': 'node-provider-retry'},
        max_iterations=3,
    )

    assert result.status == 'success'
    assert result.answer == 'done'
    assert len(requests) == 3
    assert sleep_calls == [1.0, 2.0]


@pytest.mark.asyncio
async def test_react_loop_stops_retrying_provider_chain_exhaustion_after_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[dict[str, object]] = []
    sleep_calls: list[float] = []
    real_sleep = asyncio.sleep

    async def _fake_sleep(delay: float) -> None:
        sleep_calls.append(float(delay))
        await real_sleep(0)

    monkeypatch.setattr('main.runtime.react_loop.asyncio.sleep', _fake_sleep)

    class _Backend:
        async def chat(self, **kwargs):
            requests.append(dict(kwargs))
            raise RuntimeError('Model provider call failed after exhausting the configured fallback chain.')

    loop = ReActToolLoop(chat_backend=_Backend(), log_service=_FakeLogService(), max_iterations=3)
    result = await asyncio.wait_for(
        loop.run(
            task=SimpleNamespace(task_id='task-provider-retry-limit'),
            node=SimpleNamespace(node_id='node-provider-retry-limit', depth=0, node_kind='execution'),
            messages=[
                {'role': 'system', 'content': 'system'},
                {'role': 'user', 'content': '{"task_id":"task-provider-retry-limit","goal":"demo"}'},
            ],
            tools={'submit_final_result': _submit_final_result_tool()},
            model_refs=['fake'],
            runtime_context={'task_id': 'task-provider-retry-limit', 'node_id': 'node-provider-retry-limit'},
            max_iterations=3,
        ),
        timeout=0.2,
    )

    assert result.status == 'failed'
    assert result.delivery_status == 'blocked'
    assert 'provider' in result.summary.lower()
    assert 'exhausting the configured fallback chain' in result.blocking_reason
    assert len(requests) == 3
    assert sleep_calls == [1.0, 2.0]


@pytest.mark.asyncio
async def test_react_loop_restarts_provider_retry_with_refreshed_model_refs_after_runtime_refresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[list[str]] = []
    sleep_calls: list[float] = []
    current_refs = ["old-model"]
    refreshed = {"done": False}

    async def _fake_sleep(delay: float) -> None:
        sleep_calls.append(float(delay))

    monkeypatch.setattr('main.runtime.react_loop.asyncio.sleep', _fake_sleep)

    class _Backend:
        async def chat(self, **kwargs):
            model_refs = list(kwargs.get("model_refs") or [])
            requests.append(model_refs)
            if model_refs == ["old-model"]:
                raise RuntimeError('Model provider call failed after exhausting the configured fallback chain.')
            return LLMResponse(
                content='',
                tool_calls=[
                    ToolCallRequest(
                        id='call:final',
                        name='submit_final_result',
                        arguments={
                            'status': 'success',
                            'delivery_status': 'final',
                            'summary': 'done',
                            'answer': 'done',
                            'evidence': [],
                            'remaining_work': [],
                            'blocking_reason': '',
                        },
                    )
                ],
                finish_reason='tool_calls',
                usage={'input_tokens': 8, 'output_tokens': 3},
            )

    loop = ReActToolLoop(chat_backend=_Backend(), log_service=_FakeLogService(), max_iterations=3)
    loop._runtime_config_refresh_for_retry_invalidation = lambda: (
        False
        if refreshed["done"]
        else refreshed.__setitem__("done", True) or current_refs.__setitem__(slice(None), ["new-model"]) or True
    )
    result = await loop.run(
        task=SimpleNamespace(task_id='task-provider-refresh'),
        node=SimpleNamespace(node_id='node-provider-refresh', depth=0, node_kind='execution'),
        messages=[
            {'role': 'system', 'content': 'system'},
            {'role': 'user', 'content': '{"task_id":"task-provider-refresh","goal":"demo"}'},
        ],
        tools={'submit_final_result': _submit_final_result_tool()},
        model_refs=['old-model'],
        model_refs_supplier=lambda: list(current_refs),
        runtime_context={'task_id': 'task-provider-refresh', 'node_id': 'node-provider-refresh'},
        max_iterations=3,
    )

    assert result.status == 'success'
    assert result.answer == 'done'
    assert requests == [["old-model"], ["new-model"]]
    assert sleep_calls == []


@pytest.mark.asyncio
async def test_node_send_preflight_does_not_compress_below_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import main.runtime.react_loop as react_loop_module
    from main.runtime.chat_backend import SendModelContextWindowInfo

    calls: list[list[dict[str, object]]] = []

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
    monkeypatch.setattr(
        react_loop_module.runtime_send_token_preflight,
        "estimate_runtime_provider_request_preview_tokens",
        lambda **_: 20000,
        raising=False,
    )

    class _Backend:
        async def chat(self, **kwargs):
            calls.append([dict(item) for item in list(kwargs.get("messages") or [])])
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
                usage={"input_tokens": 8, "output_tokens": 3},
            )

    log_service = _FakeLogService()
    observed_frame: dict[str, object] = {}

    class _FramingBackend(_Backend):
        async def chat(self, **kwargs):
            observed_frame.update(
                log_service.read_runtime_frame(
                    "task-preflight-no-compress",
                    "node-preflight-no-compress",
                )
            )
            return await super().chat(**kwargs)

    loop = ReActToolLoop(chat_backend=_FramingBackend(), log_service=log_service, max_iterations=2)
    result = await loop.run(
        task=SimpleNamespace(task_id="task-preflight-no-compress"),
        node=SimpleNamespace(node_id="node-preflight-no-compress", depth=0, node_kind="execution"),
        messages=[
            {"role": "system", "content": "system"},
            {"role": "user", "content": '{"task_id":"task-preflight-no-compress","goal":"demo"}'},
        ],
        tools={"submit_final_result": _submit_final_result_tool()},
        model_refs=["fake"],
        runtime_context={"task_id": "task-preflight-no-compress", "node_id": "node-preflight-no-compress"},
        max_iterations=2,
    )

    assert result.status == "success"
    assert len(calls) == 1
    rendered = "\n".join(str(item.get("content") or "") for item in calls[0])
    assert "[TOKEN_COMPACT_V2]" not in rendered

    assert isinstance(observed_frame.get("token_preflight_diagnostics"), dict)
    assert observed_frame.get("history_shrink_reason") in {"", None}


def _is_node_token_compression_helper_request(messages) -> bool:
    """节点 token 压缩 helper 调用的识别。

    新合同为 append-only：末尾 user 消息携带「【上下文压缩指令】」；分块/归并
    请求携带 `node_token_compression_chunk` / `node_token_compression_merge` kind；
    兼容旧 JSON 巨包形态的 `node_token_compression` kind（存量断言）。"""
    for item in list(messages or []):
        if not isinstance(item, dict):
            continue
        if str(item.get("role") or "").strip().lower() != "user":
            continue
        content = item.get("content")
        if isinstance(content, str) and (
            "node_token_compression" in content or "【上下文压缩指令】" in content
        ):
            return True
    return False


def _node_compression_summary_response() -> LLMResponse:
    return LLMResponse(
        content="针对任务目标整理的压缩摘要。",
        finish_reason="stop",
        usage={"input_tokens": 420, "output_tokens": 30},
    )


@pytest.mark.asyncio
async def test_node_send_preflight_triggers_compression_at_effective_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import main.runtime.react_loop as react_loop_module
    from main.runtime.chat_backend import SendModelContextWindowInfo

    calls: list[list[dict[str, object]]] = []

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

    def _estimate(**kwargs) -> int:
        request_messages = list(kwargs.get("request_messages") or [])
        rendered = "\n".join(
            str(item.get("content") or "") for item in request_messages if isinstance(item, dict)
        )
        return 18000 if "[TOKEN_COMPACT_V2]" in rendered else 24320

    monkeypatch.setattr(
        react_loop_module.runtime_send_token_preflight,
        "estimate_runtime_provider_request_preview_tokens",
        _estimate,
        raising=False,
    )

    helper_calls: list[list[dict[str, object]]] = []

    class _Backend:
        async def chat(self, **kwargs):
            messages = list(kwargs.get("messages") or [])
            if _is_node_token_compression_helper_request(messages):
                helper_calls.append([dict(item) for item in messages])
                return _node_compression_summary_response()
            calls.append([dict(item) for item in messages])
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
                usage={"input_tokens": 8, "output_tokens": 3},
            )

    log_service = _FakeLogService()
    observed_frame: dict[str, object] = {}

    class _FramingBackend(_Backend):
        async def chat(self, **kwargs):
            observed_frame.update(
                log_service.read_runtime_frame(
                    "task-preflight-compress",
                    "node-preflight-compress",
                )
            )
            return await super().chat(**kwargs)

    loop = ReActToolLoop(chat_backend=_FramingBackend(), log_service=log_service, max_iterations=2)
    result = await loop.run(
        task=SimpleNamespace(task_id="task-preflight-compress"),
        node=SimpleNamespace(node_id="node-preflight-compress", depth=0, node_kind="execution"),
        messages=[
            {"role": "system", "content": "system"},
            {"role": "user", "content": '{"task_id":"task-preflight-compress","goal":"demo"}'},
            *[{"role": "assistant", "content": f"filler {index}"} for index in range(15)],
        ],
        tools={"submit_final_result": _submit_final_result_tool()},
        model_refs=["fake"],
        runtime_context={"task_id": "task-preflight-compress", "node_id": "node-preflight-compress"},
        max_iterations=2,
    )

    assert result.status == "success"
    assert len(calls) == 1
    assert len(helper_calls) == 1
    rendered = "\n".join(str(item.get("content") or "") for item in calls[0])
    assert "[TOKEN_COMPACT_V2]" in rendered
    assert "针对任务目标整理的压缩摘要。" in rendered
    helper_rendered = "\n".join(str(item.get("content") or "") for item in helper_calls[0])
    assert "任务目标" in helper_rendered
    # append-only 新合同：任务目标写在末尾指令里，历史消息原位保留、指令殿后。
    assert "当前任务目标：demo" in helper_rendered
    assert "【上下文压缩指令】" in helper_rendered
    helper_messages = list(helper_calls[0])
    assert str(helper_messages[-1].get("role") or "") == "user"
    assert "【上下文压缩指令】" in str(helper_messages[-1].get("content") or "")
    helper_contents = [str(item.get("content") or "") for item in helper_messages]
    assert any("filler 0" in content for content in helper_contents)
    assert not any('"older_history_messages"' in content for content in helper_contents)

    assert observed_frame.get("history_shrink_reason") == "token_compression"
    diagnostics = observed_frame.get("token_preflight_diagnostics")
    assert isinstance(diagnostics, dict)
    assert diagnostics.get("applied") is True
    assert diagnostics.get("mode") == "llm"
    assert diagnostics.get("compaction_payload", {}).get("kind") == "node_token_compaction_llm"


@pytest.mark.asyncio
async def test_node_send_preflight_token_compression_recommits_pinned_provider_bundle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """token 压缩是节点清单的唯一重新提交点。

    这一跳正文已被 `[TOKEN_COMPACT_V2]` 全量重写，可复用前缀反正断在这里，所以按
    角色钉住的清单在此重取一次；旧行为（退回上一跳的窄清单）会让钉住的清单永远没有更新
    机会，已废弃。
    """
    import main.runtime.react_loop as react_loop_module
    from main.runtime.chat_backend import SendModelContextWindowInfo

    calls: list[dict[str, object]] = []
    observed_frame: dict[str, object] = {}

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

    def _estimate(**kwargs) -> int:
        request_messages = list(kwargs.get("request_messages") or [])
        rendered = "\n".join(
            str(item.get("content") or "") for item in request_messages if isinstance(item, dict)
        )
        return 18000 if "[TOKEN_COMPACT_V2]" in rendered else 24320

    monkeypatch.setattr(
        react_loop_module.runtime_send_token_preflight,
        "estimate_runtime_provider_request_preview_tokens",
        _estimate,
        raising=False,
    )

    helper_calls: list[dict[str, object]] = []

    class _Backend:
        async def chat(self, **kwargs):
            observed_frame.update(
                log_service.read_runtime_frame(
                    "task-preflight-provider-sync",
                    "node-preflight-provider-sync",
                )
            )
            messages = list(kwargs.get("messages") or [])
            if _is_node_token_compression_helper_request(messages):
                helper_calls.append(dict(kwargs))
                return _node_compression_summary_response()
            calls.append(dict(kwargs))
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
                usage={"input_tokens": 8, "output_tokens": 3},
            )

    log_service = _FakeLogService()
    log_service.upsert_frame(
        "task-preflight-provider-sync",
        {
            "node_id": "node-preflight-provider-sync",
            "model_visible_tool_names": ["submit_final_result", "web_fetch"],
            "provider_tool_names": ["submit_final_result"],
        },
    )

    loop = ReActToolLoop(chat_backend=_Backend(), log_service=log_service, max_iterations=2)

    def _selector(**kwargs):
        return {
            "tool_names": ["submit_final_result", "web_fetch"],
            "provider_tool_names": ["submit_final_result", "web_fetch"],
            "pinned_provider_tool_names": ["submit_final_result", "web_fetch", "content_open"],
            "provider_tool_bundle_mode": "pinned_frozen",
            "pending_provider_tool_names": [],
            "provider_tool_exposure_pending": False,
            "provider_tool_exposure_commit_reason": "",
            "provider_tool_exposure_revision": "pte:active",
            "trace": {
                "full_callable_tool_names": ["submit_final_result", "web_fetch"],
                "provider_tool_names": ["submit_final_result", "web_fetch"],
                "pending_provider_tool_names": [],
                "provider_tool_exposure_commit_reason": "",
                "prior_provider_tool_names": ["submit_final_result"],
            },
        }

    loop._model_visible_tool_schema_selector = _selector

    class _SchemaTool(_LargeModelSchemaTool):
        def to_model_schema(self):
            return {"type": "function", "function": {"name": self.name, "parameters": {"type": "object"}}}

    result = await loop.run(
        task=SimpleNamespace(task_id="task-preflight-provider-sync"),
        node=SimpleNamespace(node_id="node-preflight-provider-sync", depth=0, node_kind="execution"),
        messages=[
            {"role": "system", "content": "system"},
            {"role": "user", "content": '{"task_id":"task-preflight-provider-sync","goal":"demo"}'},
            *[{"role": "assistant", "content": f"filler {index}"} for index in range(15)],
        ],
        tools={
            "submit_final_result": _submit_final_result_tool(),
            "web_fetch": _SchemaTool(name="web_fetch"),
            "content_open": _SchemaTool(name="content_open"),
        },
        model_refs=["fake"],
        runtime_context={"task_id": "task-preflight-provider-sync", "node_id": "node-preflight-provider-sync"},
        max_iterations=2,
    )

    assert result.status == "success"
    assert len(calls) == 1
    emitted_tools = list(calls[0].get("tools") or [])
    emitted_tool_names = [item["function"]["name"] for item in emitted_tools]
    # 压缩跳按钉住清单重印：当轮生效清单里没有的 `content_open` 在这一跳重新出现在 tools[]。
    assert emitted_tool_names == ["submit_final_result", "web_fetch", "content_open"]
    assert observed_frame.get("provider_tool_names") == ["submit_final_result", "web_fetch", "content_open"]
    assert observed_frame.get("provider_tool_exposure_commit_reason") == ""


@pytest.mark.asyncio
async def test_node_send_preflight_fails_when_post_compression_overflows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import main.runtime.react_loop as react_loop_module
    from main.runtime.chat_backend import SendModelContextWindowInfo

    calls: list[list[dict[str, object]]] = []

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

    def _estimate(**kwargs) -> int:
        request_messages = list(kwargs.get("request_messages") or [])
        rendered = "\n".join(
            str(item.get("content") or "") for item in request_messages if isinstance(item, dict)
        )
        return 33000 if "[TOKEN_COMPACT_V2]" in rendered else 24320

    monkeypatch.setattr(
        react_loop_module.runtime_send_token_preflight,
        "estimate_runtime_provider_request_preview_tokens",
        _estimate,
        raising=False,
    )

    helper_calls: list[list[dict[str, object]]] = []

    class _Backend:
        async def chat(self, **kwargs):
            messages = list(kwargs.get("messages") or [])
            if _is_node_token_compression_helper_request(messages):
                helper_calls.append([dict(item) for item in messages])
                return _node_compression_summary_response()
            calls.append([dict(item) for item in messages])
            raise RuntimeError("chat should not be called when preflight fails")

    log_service = _FakeLogService()
    loop = ReActToolLoop(chat_backend=_Backend(), log_service=log_service, max_iterations=2)
    result = await loop.run(
        task=SimpleNamespace(task_id="task-preflight-overflow"),
        node=SimpleNamespace(node_id="node-preflight-overflow", depth=0, node_kind="execution"),
        messages=[
            {"role": "system", "content": "system"},
            {"role": "user", "content": '{"task_id":"task-preflight-overflow","goal":"demo"}'},
            *[{"role": "assistant", "content": f"filler {index}"} for index in range(15)],
        ],
        tools={"submit_final_result": _submit_final_result_tool()},
        model_refs=["fake"],
        runtime_context={"task_id": "task-preflight-overflow", "node_id": "node-preflight-overflow"},
        max_iterations=2,
    )

    assert result.status == "failed"
    assert calls == []
    assert len(helper_calls) == 1


@pytest.mark.asyncio
async def test_node_preflight_uses_previous_effective_input_tokens_when_preview_underestimates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import main.runtime.react_loop as react_loop_module
    from main.runtime.chat_backend import SendModelContextWindowInfo

    calls: list[list[dict[str, object]]] = []

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
            context_window_tokens=25001,
            resolution_error="",
        ),
        raising=False,
    )

    def _estimate(**kwargs) -> int:
        request_messages = list(kwargs.get("request_messages") or [])
        rendered = "\n".join(
            str(item.get("content") or "") for item in request_messages if isinstance(item, dict)
        )
        return 11000 if "[TOKEN_COMPACT_V2]" in rendered else 12840

    monkeypatch.setattr(
        react_loop_module.runtime_send_token_preflight,
        "estimate_runtime_provider_request_preview_tokens",
        _estimate,
        raising=False,
    )
    monkeypatch.setattr(
        react_loop_module.ReActToolLoop,
        "_resolve_previous_node_observed_input_truth",
        lambda self, **_: {
            "effective_input_tokens": 20313,
            "input_tokens": 20313,
            "cache_hit_tokens": 0,
            "provider_model": "test:fake",
            "actual_request_hash": "prev-request-hash",
            "source": "provider_usage",
        },
        raising=False,
    )
    monkeypatch.setattr(
        react_loop_module.ReActToolLoop,
        "_resolve_previous_node_actual_request_record",
        lambda self, **_: {
            "actual_request_hash": "prev-request-hash",
            "request_messages": [
                {"role": "system", "content": "system"},
                {"role": "user", "content": '{"task_id":"task-preflight-usage-plus-delta","goal":"demo"}'},
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "message_type": "node_runtime_tool_contract",
                            "callable_tool_names": ["submit_final_result"],
                        },
                        ensure_ascii=False,
                    ),
                },
            ],
            "tool_schemas": [
                {
                    "type": "function",
                    "function": {
                        "name": "submit_final_result",
                        "description": "submit final result",
                        "parameters": {"type": "object"},
                    },
                }
            ],
        },
        raising=False,
    )
    monkeypatch.setattr(
        react_loop_module.ReActToolLoop,
        "_append_only_delta_estimate_tokens",
        lambda self, **_: (1800, True),
        raising=False,
    )

    helper_calls: list[list[dict[str, object]]] = []

    class _Backend:
        async def chat(self, **kwargs):
            messages = list(kwargs.get("messages") or [])
            if _is_node_token_compression_helper_request(messages):
                helper_calls.append([dict(item) for item in messages])
                return _node_compression_summary_response()
            calls.append([dict(item) for item in messages])
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
                usage={"input_tokens": 8, "output_tokens": 3},
            )

    log_service = _FakeLogService()
    observed_frame: dict[str, object] = {}

    class _FramingBackend(_Backend):
        async def chat(self, **kwargs):
            observed_frame.update(
                log_service.read_runtime_frame(
                    "task-preflight-usage-plus-delta",
                    "node-preflight-usage-plus-delta",
                )
            )
            return await super().chat(**kwargs)

    loop = ReActToolLoop(chat_backend=_FramingBackend(), log_service=log_service, max_iterations=2)
    result = await loop.run(
        task=SimpleNamespace(task_id="task-preflight-usage-plus-delta"),
        node=SimpleNamespace(node_id="node-preflight-usage-plus-delta", depth=0, node_kind="execution"),
        messages=[
            {"role": "system", "content": "system"},
            {"role": "user", "content": '{"task_id":"task-preflight-usage-plus-delta","goal":"demo"}'},
            *[{"role": "assistant", "content": f"filler {index}"} for index in range(15)],
        ],
        tools={"submit_final_result": _submit_final_result_tool()},
        model_refs=["fake"],
        runtime_context={"task_id": "task-preflight-usage-plus-delta", "node_id": "node-preflight-usage-plus-delta"},
        max_iterations=2,
    )

    assert result.status == "success"
    assert len(calls) == 1
    rendered = "\n".join(str(item.get("content") or "") for item in calls[0])
    assert "[TOKEN_COMPACT_V2]" in rendered

    diagnostics = observed_frame.get("token_preflight_diagnostics")
    assert isinstance(diagnostics, dict)
    assert diagnostics["estimate_source"] == "preview_estimate"
    assert diagnostics["comparable_to_previous_request"] is False
    assert diagnostics["effective_input_tokens"] == 0
    assert diagnostics["final_estimate_tokens"] == 11000
    assert diagnostics["pre_compaction_estimate_source"] == "usage_plus_delta"
    assert diagnostics["pre_compaction_comparable_to_previous_request"] is True
    assert diagnostics["pre_compaction_effective_input_tokens"] == 20313
    assert diagnostics["pre_compaction_final_estimate_tokens"] >= 22113
    assert diagnostics["pre_compaction_estimated_total_tokens"] >= 22113
    assert diagnostics["estimated_total_tokens"] == 11000
    assert diagnostics["would_exceed_context_window"] is False


@pytest.mark.asyncio
async def test_node_preflight_attempts_compression_before_failing_when_pre_compaction_estimate_exceeds_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import main.runtime.react_loop as react_loop_module
    from main.runtime.chat_backend import SendModelContextWindowInfo

    calls: list[list[dict[str, object]]] = []

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
            context_window_tokens=25001,
            resolution_error="",
        ),
        raising=False,
    )

    def _estimate(**kwargs) -> int:
        request_messages = list(kwargs.get("request_messages") or [])
        rendered = "\n".join(
            str(item.get("content") or "") for item in request_messages if isinstance(item, dict)
        )
        if "[TOKEN_COMPACT_V2]" in rendered:
            return 25110
        if len(request_messages) > 6 or "【上下文压缩指令】" in rendered:
            # 整请求与单发压缩指令都按超窗读：分块判据看的正是"单发压缩请求自身是否超窗"。
            return 26000
        if "压缩摘要正文" in rendered:
            return 1_000  # 压缩信封（system + 指令头）：容量 = 窗口 − 信封 − 输出余量
        return 12_000  # 单个原子组：几条组就会切出多块

    monkeypatch.setattr(
        react_loop_module.runtime_send_token_preflight,
        "estimate_runtime_provider_request_preview_tokens",
        _estimate,
        raising=False,
    )

    helper_calls: list[list[dict[str, object]]] = []

    class _Backend:
        async def chat(self, **kwargs):
            messages = list(kwargs.get("messages") or [])
            if _is_node_token_compression_helper_request(messages):
                helper_calls.append([dict(item) for item in messages])
                return _node_compression_summary_response()
            calls.append([dict(item) for item in messages])
            raise RuntimeError("chat should not be called when preflight fails")

    log_service = _FakeLogService()
    loop = ReActToolLoop(chat_backend=_Backend(), log_service=log_service, max_iterations=2)
    result = await loop.run(
        task=SimpleNamespace(task_id="task-preflight-compress-before-fail"),
        node=SimpleNamespace(node_id="node-preflight-compress-before-fail", depth=0, node_kind="execution"),
        messages=[
            {"role": "system", "content": "system"},
            {"role": "user", "content": '{"task_id":"task-preflight-compress-before-fail","goal":"demo"}'},
            *[{"role": "assistant", "content": f"filler {index}"} for index in range(15)],
        ],
        tools={"submit_final_result": _submit_final_result_tool()},
        model_refs=["fake"],
        runtime_context={"task_id": "task-preflight-compress-before-fail", "node_id": "node-preflight-compress-before-fail"},
        max_iterations=2,
    )

    diagnostics = log_service.read_runtime_frame(
        "task-preflight-compress-before-fail",
        "node-preflight-compress-before-fail",
    ).get("token_preflight_diagnostics")

    assert result.status == "failed"
    assert calls == []
    assert isinstance(diagnostics, dict)
    assert diagnostics["applied"] is True
    assert diagnostics["estimate_source"] == "preview_estimate"
    assert diagnostics["comparable_to_previous_request"] is False
    assert diagnostics["effective_input_tokens"] == 0
    assert diagnostics["final_estimate_tokens"] == 25110
    assert diagnostics["pre_compaction_estimated_total_tokens"] == 26000
    assert diagnostics["estimated_total_tokens"] == 25110
    assert "after compression" in str(result.blocking_reason or "")
    assert "after compression" in str(diagnostics.get("error") or "")
    # 超窗新合同：单发压缩请求自身放不下窗口 → 分块压缩（多次 helper 调用：
    # 若干分块 + 可选归并），压缩后重算仍超窗才失败。
    assert len(helper_calls) >= 2
    chunk_requests = [
        str(item.get("content") or "")
        for call in helper_calls
        for item in call
        if isinstance(item, dict) and "node_token_compression_chunk" in str(item.get("content") or "")
    ]
    assert chunk_requests
    assert diagnostics.get("compression_mode") == "llm_chunked"
    assert int(diagnostics.get("chunk_count") or 0) >= 1


@pytest.mark.asyncio
async def test_node_preflight_falls_back_to_preview_when_request_is_not_append_only_continuation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import main.runtime.react_loop as react_loop_module
    from main.runtime.chat_backend import SendModelContextWindowInfo

    calls: list[list[dict[str, object]]] = []

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
            context_window_tokens=25001,
            resolution_error="",
        ),
        raising=False,
    )

    monkeypatch.setattr(
        react_loop_module.runtime_send_token_preflight,
        "estimate_runtime_provider_request_preview_tokens",
        lambda **kwargs: 12840,
        raising=False,
    )
    monkeypatch.setattr(
        react_loop_module.ReActToolLoop,
        "_resolve_previous_node_observed_input_truth",
        lambda self, **_: {
            "effective_input_tokens": 20313,
            "input_tokens": 20313,
            "cache_hit_tokens": 0,
            "provider_model": "test:fake",
            "actual_request_hash": "prev-request-hash",
            "source": "provider_usage",
        },
        raising=False,
    )
    monkeypatch.setattr(
        react_loop_module.ReActToolLoop,
        "_append_only_delta_estimate_tokens",
        lambda self, **_: (1800, False),
        raising=False,
    )

    class _Backend:
        async def chat(self, **kwargs):
            calls.append([dict(item) for item in list(kwargs.get("messages") or [])])
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
                usage={"input_tokens": 8, "output_tokens": 3},
            )

    log_service = _FakeLogService()
    observed_frame: dict[str, object] = {}

    class _FramingBackend(_Backend):
        async def chat(self, **kwargs):
            observed_frame.update(
                log_service.read_runtime_frame(
                    "task-preflight-preview-fallback",
                    "node-preflight-preview-fallback",
                )
            )
            return await super().chat(**kwargs)

    loop = ReActToolLoop(chat_backend=_FramingBackend(), log_service=log_service, max_iterations=2)
    result = await loop.run(
        task=SimpleNamespace(task_id="task-preflight-preview-fallback"),
        node=SimpleNamespace(node_id="node-preflight-preview-fallback", depth=0, node_kind="execution"),
        messages=[
            {"role": "system", "content": "system"},
            {"role": "user", "content": '{"task_id":"task-preflight-preview-fallback","goal":"demo"}'},
        ],
        tools={"submit_final_result": _submit_final_result_tool()},
        model_refs=["fake"],
        runtime_context={"task_id": "task-preflight-preview-fallback", "node_id": "node-preflight-preview-fallback"},
        max_iterations=2,
    )

    assert result.status == "success"
    assert len(calls) == 1
    rendered = "\n".join(str(item.get("content") or "") for item in calls[0])
    assert "[TOKEN_COMPACT_V2]" not in rendered

    diagnostics = observed_frame.get("token_preflight_diagnostics")
    assert isinstance(diagnostics, dict)
    assert diagnostics["estimate_source"] == "preview_estimate"
    assert diagnostics["comparable_to_previous_request"] is False
    assert diagnostics["usage_based_estimate_tokens"] == 0


def test_node_preflight_provider_model_match_accepts_resolved_prefix_form() -> None:
    assert ReActToolLoop._provider_models_match("gpt-5.4", "openai:gpt-5.4") is True
    assert ReActToolLoop._provider_models_match("openai:gpt-5.4", "gpt-5.4") is True
    assert ReActToolLoop._provider_models_match("openai:gpt-5.4", "anthropic:claude-sonnet-4") is False


def test_node_preflight_falls_back_to_preview_when_observed_truth_hash_mismatches_latest_request_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import main.runtime.react_loop as react_loop_module

    monkeypatch.setattr(
        react_loop_module.runtime_send_token_preflight,
        "estimate_runtime_provider_request_preview_tokens",
        lambda **kwargs: 12840,
        raising=False,
    )

    log_service = _FakeLogService()
    log_service._store._node = SimpleNamespace(
        metadata={
            "latest_runtime_observed_input_truth": {
                "effective_input_tokens": 20313,
                "input_tokens": 20313,
                "cache_hit_tokens": 0,
                "provider_model": "test:fake",
                "actual_request_hash": "truth-hash",
                "source": "provider_usage",
            },
            "latest_runtime_actual_request_ref": "artifact:latest-request",
        }
    )
    monkeypatch.setattr(
        log_service,
        "resolve_content_ref",
        lambda ref: json.dumps(
            {
                "actual_request_hash": "record-hash",
                "request_messages": [
                    {"role": "system", "content": "system"},
                    {"role": "user", "content": "prompt"},
                ],
                "tool_schemas": [],
            },
            ensure_ascii=False,
        ),
        raising=False,
    )

    loop = ReActToolLoop(chat_backend=SimpleNamespace(), log_service=log_service, max_iterations=2)
    estimate_payload = loop._estimate_node_send_preflight_tokens(
        task_id="task-hash-mismatch",
        node_id="node-hash-mismatch",
        config=None,
        model_refs=["fake"],
        provider_model="test:fake",
        request_messages=[
            {"role": "system", "content": "system"},
            {"role": "user", "content": "prompt"},
            {"role": "assistant", "content": "next"},
        ],
        tool_schemas=[],
        prompt_cache_key="cache-key",
        tool_choice=None,
        parallel_tool_calls=None,
    )

    assert estimate_payload["estimate_source"] == "preview_estimate"
    assert estimate_payload["comparable_to_previous_request"] is False
    assert estimate_payload["usage_based_estimate_tokens"] == 0


@pytest.mark.asyncio
async def test_node_second_tool_round_request_carries_no_stale_contract_or_note() -> None:
    """End-to-end: two tool rounds in one `run()` must carry zero runtime-contract
    tail messages and at most one turn-only note; the carried prefix must not retain
    the stale note from the previous round."""
    from main.runtime.turn_overlay import is_turn_only_system_note_message

    calls: list[list[dict[str, object]]] = []
    executed: list[tuple[str, str]] = []

    class _Backend:
        def __init__(self) -> None:
            self._responses = [
                LLMResponse(
                    content="",
                    tool_calls=[
                        ToolCallRequest(
                            id="call:first",
                            name="record_one",
                            arguments={"value": "first"},
                        )
                    ],
                    finish_reason="tool_calls",
                    usage={"input_tokens": 8, "output_tokens": 3},
                ),
                LLMResponse(
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
                    usage={"input_tokens": 8, "output_tokens": 3},
                ),
            ]

        async def chat(self, **kwargs):
            calls.append([dict(item) for item in list(kwargs.get("messages") or [])])
            return self._responses.pop(0)

    loop = ReActToolLoop(chat_backend=_Backend(), log_service=_FakeLogService(), max_iterations=3)
    result = await loop.run(
        task=SimpleNamespace(task_id="task-overlay-single"),
        node=SimpleNamespace(node_id="node-overlay-single", depth=0, node_kind="execution"),
        messages=[
            {"role": "system", "content": "system"},
            {"role": "user", "content": '{"task_id":"task-overlay-single","goal":"demo"}'},
        ],
        tools={
            "record_one": _RecordingTool("record_one", executed),
            "submit_final_result": _submit_final_result_tool(),
        },
        model_refs=["fake"],
        runtime_context={"task_id": "task-overlay-single", "node_id": "node-overlay-single"},
        max_iterations=3,
    )

    assert result.status == "success"
    assert executed == [("record_one", "first")]
    assert len(calls) >= 2

    def _contract_count(messages: list[dict[str, object]]) -> int:
        return sum(
            1
            for item in messages
            if str(item.get("content") or "").startswith("## Runtime Tool Contract")
        )

    def _note_count(messages: list[dict[str, object]]) -> int:
        return sum(1 for item in messages if is_turn_only_system_note_message(item))

    for request_messages in calls:
        assert _contract_count(request_messages) == 0
        assert _note_count(request_messages) <= 1

    second_request = calls[-1]
    assistant_turns = [item for item in second_request if item.get("role") == "assistant"]
    tool_turns = [item for item in second_request if item.get("role") == "tool"]
    assert any(
        str((((tool_call or {}).get("function") or {}).get("name") or "")).strip() == "record_one"
        for message in assistant_turns
        for tool_call in list(message.get("tool_calls") or [])
    )
    assert any(str(item.get("name") or "").strip() == "record_one" for item in tool_turns)
    # The carried prefix must not retain the previous round's note.
    non_tail = second_request
    assert _contract_count(non_tail) == 0
    assert _note_count(non_tail) == 0


def test_remind_content_open_reopen_on_exec_truncation() -> None:
    loop = ReActToolLoop.__new__(ReActToolLoop)

    results = [
        {
            "raw_result": json.dumps({"status": "ok", "stdout_truncated": True, "stderr_truncated": False}),
            "tool_message": {"name": "exec", "tool_call_id": "c1", "content": "<EXEC_TRUNC>"},
            "live_state": {"tool_name": "exec"},
        },
        {
            "raw_result": json.dumps({"status": "ok", "stdout_truncated": False}),
            "tool_message": {"name": "exec", "tool_call_id": "c2", "content": "<EXEC_OK>"},
            "live_state": {"tool_name": "exec"},
        },
        {
            "raw_result": json.dumps({"ok": True, "excerpt": "x"}),
            "tool_message": {"name": "content_open", "tool_call_id": "c3", "content": "<OPEN>"},
            "live_state": {"tool_name": "content_open"},
        },
    ]

    reminded = loop._remind_content_open_reopen_on_exec_truncation(results=results)

    assert reminded == 1
    # 只在截断的 exec 结果里注入提醒，且保留原有内容前缀。
    assert results[0]["tool_message"]["content"].startswith("<EXEC_TRUNC>")
    content = str(results[0]["tool_message"]["content"])
    assert "exec 输出未整篇交付到本轮上下文" in content
    # 文案承诺的出路必须真存在：没有任何"自动加载"，模型自己取契约。
    assert "已自动加载" not in content
    assert 'load_tool_context(tool_id="content_open")' in content
    # 非截断 exec 与其它工具不被改动。
    assert results[1]["tool_message"]["content"] == "<EXEC_OK>"
    assert results[2]["tool_message"]["content"] == "<OPEN>"

    # 幂等：同一结果再次处理不再重复注入。
    assert loop._remind_content_open_reopen_on_exec_truncation(results=results) == 0


def test_exec_truncation_reminder_fires_on_inline_budget_cut() -> None:
    """内联额度切割也必须触发复开通道。

    实盘被裁的行号证据正是这一档：捕获环没丢中段（`stdout_truncated=False`），旧判据
    看不见这次切割，模型既没拿到整篇也没被告知能复开。
    """
    loop = ReActToolLoop.__new__(ReActToolLoop)

    results = [
        {
            "raw_result": json.dumps({
                "status": "ok",
                "stdout_truncated": False,
                "stderr_truncated": False,
                "output_truncated": True,
            }),
            "tool_message": {"name": "exec", "tool_call_id": "c1", "content": "<CUT>"},
            "live_state": {"tool_name": "exec"},
        },
        {
            "raw_result": json.dumps({
                "status": "ok",
                "stdout_truncated": False,
                "output_truncated": False,
            }),
            "tool_message": {"name": "exec", "tool_call_id": "c2", "content": "<WHOLE>"},
            "live_state": {"tool_name": "exec"},
        },
    ]

    reminded = loop._remind_content_open_reopen_on_exec_truncation(results=results)

    assert reminded == 1
    content = str(results[0]["tool_message"]["content"])
    assert "exec 输出未整篇交付到本轮上下文" in content
    assert "content_search" in content
    # 整篇交付的那条不被改动。
    assert results[1]["tool_message"]["content"] == "<WHOLE>"


@pytest.mark.asyncio
async def test_empty_model_responses_stop_at_retry_limit(tmp_path, monkeypatch) -> None:
    """连续空响应必须在重试上限处终止并返回 blocked 失败。

    空响应重试发生在内层 while True 中，绕过外层 attempts/max_iterations
    预算：无上限时节点卡死并持续消耗 provider 调用。修复后复用
    _PROVIDER_RETRY_LIMIT 作为上限。
    """
    from main.runtime.react_loop import _PROVIDER_RETRY_LIMIT

    monkeypatch.setattr(
        ReActToolLoop,
        '_empty_response_retry_delay_seconds',
        staticmethod(lambda attempt_count: 0.0),
    )

    chat_calls: list[dict[str, object]] = []

    class _Backend:
        async def chat(self, **kwargs):
            chat_calls.append(dict(kwargs))
            return LLMResponse(
                content='',
                tool_calls=[],
                finish_reason='stop',
                usage={'input_tokens': 1, 'output_tokens': 0},
            )

    react_log_service = _FakeLogService()
    react_log_service.execution_stage_gate_snapshot = lambda task_id, node_id: {
        'has_active_stage': False,
        'transition_required': False,
        'active_stage': None,
    }

    service = MainRuntimeService(
        chat_backend=_Backend(),
        workspace_root=tmp_path,
        store_path=tmp_path / "runtime.sqlite3",
        files_base_dir=tmp_path / "tasks",
        artifact_dir=tmp_path / "artifacts",
        governance_store_path=tmp_path / "governance.sqlite3",
        execution_model_refs=['fake'],
        acceptance_model_refs=['fake'],
    )
    service._react_loop._log_service = react_log_service
    # 隔离配置热刷新重启动路径：该路径依赖真实配置状态，与本用例的空响应
    # 预算主题无关，显式关闭以保证计数确定性。
    service._react_loop._runtime_config_refresh_for_retry_invalidation = None
    service.store = SimpleNamespace(
        get_task=lambda task_id: SimpleNamespace(
            task_id=task_id,
            session_id='web:shared',
            metadata={'core_requirement': 'demo'},
        ),
        get_node=lambda node_id: SimpleNamespace(
            node_id=node_id,
            prompt='demo',
            goal='demo',
            node_kind='execution',
        ),
    )
    service.execution_visible_tool_lightweight_items = lambda *, actor_role, session_id: []

    result = await asyncio.wait_for(
        service._react_loop.run(
            task=SimpleNamespace(task_id='task-empty-cap'),
            node=SimpleNamespace(node_id='node-empty-cap', depth=0, node_kind='execution'),
            messages=[
                {'role': 'system', 'content': 'system'},
                {'role': 'user', 'content': '{"task_id":"task-empty-cap","goal":"demo"}'},
            ],
            tools={
                'stop_tool_execution': _StageProtocolNoopTool('stop_tool_execution'),
                'submit_final_result': _submit_final_result_tool(),
                'spawn_child_nodes': _StageProtocolNoopTool('spawn_child_nodes'),
            },
            model_refs=['fake'],
            runtime_context={
                'task_id': 'task-empty-cap',
                'node_id': 'node-empty-cap',
                'session_key': 'web:shared',
                'actor_role': 'execution',
            },
            max_iterations=8,
        ),
        timeout=30,
    )

    assert result.status == 'failed'
    assert result.delivery_status == 'blocked'
    assert 'empty responses' in str(result.blocking_reason)
    assert len(chat_calls) == _PROVIDER_RETRY_LIMIT, "达到上限后不得继续调用 provider"


def _six_stage_node_history(
    stage_total: int = 6, evict_indexes: list[int] | None = None
) -> tuple[dict[str, object], list[dict[str, object]]]:
    """节点账本 + 同形态历史：每个阶段一次 submit 开阶段、一轮 exec 肉身。

    窗口已移除，想让它里面有几条阶段离开可见层，只能点名：传 evict_indexes。"""
    stages: list[dict[str, object]] = []
    messages: list[dict[str, object]] = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": '{"task_id":"task-1","goal":"demo"}'},
    ]
    for index in range(1, stage_total + 1):
        active = index == stage_total
        stages.append(
            {
                "mode": "自主执行",
                "status": "进行中" if active else "完成",
                **({'context_evicted': True} if index in set(evict_indexes or []) else {}),
                "rounds": [
                    {
                        "round_id": f"stage-{index}:round-1",
                        "round_index": 1,
                        "tool_call_ids": [f"call-work-{index}"],
                    }
                ],
            }
        )
        messages.extend(
            [
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": f"call-stage-{index}",
                            "type": "function",
                            "function": {"name": "filesystem_stat", "arguments": "{}"},
                        }
                    ],
                },
                {
                    "role": "tool",
                    "name": "filesystem_stat",
                    "tool_call_id": f"call-stage-{index}",
                    "content": json.dumps({"stage_id": f"stage-{index}", "stage_index": index}, ensure_ascii=False),
                },
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {"id": f"call-work-{index}", "type": "function", "function": {"name": "exec", "arguments": "{}"}}
                    ],
                },
                {"role": "tool", "name": "exec", "tool_call_id": f"call-work-{index}", "content": f"body-{index}"},
            ]
        )
    state = {"active_stage_id": f"stage-{stage_total}", "transition_required": False, "stages": stages}
    return state, messages






def test_task_node_detail_manifest_is_agent_full_and_unscoped_by_design() -> None:
    # 钉住两条维护者决定，不是断言"应该加护栏"：agent 侧固定返回完整轨迹（detail_level
    # 已从模型可见 schema 摘掉），角色对三个 actor 全开且不做归属校验。
    repo_root = Path(__file__).resolve().parents[2]
    manifest = yaml.safe_load(
        (repo_root / "tools" / "task_node_detail_cn" / "resource.yaml").read_text(encoding="utf-8")
    ) or {}

    assert "detail_level" not in (manifest.get("parameters") or {}).get("properties", {})
    governance = manifest.get("governance") or {}
    assert "actions" not in governance and "family" not in governance
    assert sorted(governance.get("allowed_roles") or []) == ["ceo", "execution", "inspection"]


def test_provider_marker_tail_fingerprint_catches_real_corruption_only() -> None:
    # 四条值逐字取自实盘 node:4367ff7a3325 被拒的 submit_final_result：provider 把参数块
    # 未转义拼进 arguments，json_repair 将闭合标记吸进 status 并吃掉后面两个必填参数，
    # 于是解析故障被读成"模型漏填参数"并按无效提交计次。残缺写法有三种，按形态判定。
    line_feed = chr(10)
    bar = chr(0xFF5C)
    lt = chr(60)
    gt = chr(62)
    corrupted = [
        "success" + lt + "/parameter" + gt + line_feed + lt + bar + "DSML" + bar + ":",
        "failed" + lt + "/" + bar + "DSML" + bar + ":",
        "success" + lt + "/" + bar + "DSML" + bar + ":",
        "failed" + lt + "/parameter" + gt + line_feed + lt + "parameter:",
    ]
    for value in corrupted:
        call = SimpleNamespace(arguments={"status": value, "answer": "", "evidence": []})
        assert ReActToolLoop._tool_call_marker_fault(call), value

    benign = [
        {"status": "success", "delivery_status": "final", "summary": "ok", "answer": "已完成"},
        {"status": "success", "answer": "门槛写作 a < b 时"},
        {"status": "success", "answer": "html 里用 " + lt + "div" + gt + lt + "/div" + gt},
        {"status": "success", "answer": "线上格式是 " + lt + bar + "DSML" + bar + "parameter" + gt + "，后面还有正文"},
        {"status": "success", "key_refs": [{"ref": "path:a/b.py", "note": "口径"}]},
    ]
    for payload in benign:
        assert ReActToolLoop._tool_call_marker_fault(SimpleNamespace(arguments=payload)) == "", payload


@pytest.mark.asyncio
async def test_node_loop_keeps_revoked_tool_declared_but_still_refuses_it() -> None:
    """权限收回后：tools[] 仍带参数表（钉住的声明），但执行准入照旧拒。

    删名会把身后正文整段重铺，所以声明侧要能脱离派发字典渲染；而派发字典是节点唯一的
    RBAC 闸门（`_execute_tool_raw` 只按字典查名，不做二次鉴权），所以"能声明"绝不能
    变成"能执行"。这条同时钉住两端。
    """
    calls: list[dict[str, object]] = []

    class _Backend:
        async def chat(self, **kwargs):
            calls.append(dict(kwargs))
            if len(calls) == 1:
                return LLMResponse(
                    content="",
                    tool_calls=[
                        ToolCallRequest(id="call:wf", name="web_fetch", arguments={"url": "https://example.com"})
                    ],
                    finish_reason="tool_calls",
                    usage={"input_tokens": 8, "output_tokens": 3},
                )
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
                usage={"input_tokens": 8, "output_tokens": 3},
            )

    loop = ReActToolLoop(chat_backend=_Backend(), log_service=_FakeLogService(), max_iterations=2)

    def _selector(**_kwargs):
        return {
            "tool_names": ["submit_final_result"],
            "provider_tool_names": ["submit_final_result", "web_fetch"],
            "declared_denied_tool_names": ["web_fetch"],
            "declared_denied_tool_schemas": {
                "web_fetch": {
                    "type": "function",
                    "function": {
                        "name": "web_fetch",
                        "description": "fetch a page",
                        "parameters": {"type": "object", "properties": {"url": {"type": "string"}}},
                    },
                }
            },
            "candidate_tool_names": [],
            "lightweight_tool_ids": [],
            "pending_provider_tool_names": [],
            "provider_tool_exposure_pending": False,
            "provider_tool_exposure_revision": "",
            "provider_tool_exposure_commit_reason": "",
            "trace": {"full_callable_tool_names": ["submit_final_result"]},
        }

    loop._model_visible_tool_schema_selector = _selector

    result = await loop.run(
        task=SimpleNamespace(task_id="task-declared-denied"),
        node=SimpleNamespace(node_id="node-declared-denied", depth=0, node_kind="execution"),
        messages=[
            {"role": "system", "content": "system"},
            {"role": "user", "content": '{"task_id":"task-declared-denied","goal":"demo"}'},
        ],
        tools={"submit_final_result": _submit_final_result_tool()},
        model_refs=["fake"],
        runtime_context={"task_id": "task-declared-denied", "node_id": "node-declared-denied"},
        max_iterations=2,
    )

    assert result.status == "success"
    assert len(calls) == 2
    emitted_names = [str((item.get("function") or {}).get("name") or "") for item in list(calls[0].get("tools") or [])]
    # 声明侧：派发字典里根本没有 web_fetch 实例，schema 仍必须出现在 tools[] 里
    assert "web_fetch" in emitted_names
    # 执行侧：第二跳的上下文里必须是一次拒绝，而不是真的抓到了页面
    tool_messages = [
        item for item in list(calls[1].get("messages") or []) if isinstance(item, dict) and item.get("role") == "tool"
    ]
    refusal_text = "\n".join(str(item.get("content") or "") for item in tool_messages)
    assert "tool not available: web_fetch" in refusal_text
    assert "无权限" in refusal_text


def test_tool_result_delivery_metadata_tolerates_non_object_arguments() -> None:
    """模型把整份 arguments 给成子节点数组时，元数据构造不得抛 ValueError 掩盖参数错误。"""

    payload = ReActToolLoop._tool_result_delivery_metadata(
        tools={},
        tool_name="spawn_child_nodes",
        arguments=[
            {
                "goal": "g",
                "prompt": "p",
                "execution_policy": {},
                "requires_acceptance": True,
                "acceptance_prompt": "a",
                "seed_tools": [],
                "title": "t",
            }
        ],
    )

    assert payload["invocation_text"] == "spawn_child_nodes"


class _ReasoningChunkChatBackend:
    """按真发流的样子调用节点道的两个思维回调：开段 → 吐若干思维分片 → 再写一会儿工具参数。

    后段那截是这条用例的否决力所在：终点必须停在最后一个思维块，工具参数写多久都不该
    进读数（前门那行读数就栽在把那段算成思考上）。
    """

    def __init__(self, *, thought_chunks: int = 2) -> None:
        self.calls: list[dict[str, object]] = []
        self._thought_chunks = int(thought_chunks)

    async def chat(self, **kwargs):
        self.calls.append(dict(kwargs))
        begin_segment = kwargs.get('on_reasoning_segment')
        note_chunk = kwargs.get('on_reasoning_delta')
        assert callable(begin_segment), '节点道没把思维段起点接到发送层'
        assert callable(note_chunk), '节点道没把思维分片接到发送层'
        begin_segment()
        for _ in range(self._thought_chunks):
            await asyncio.sleep(0.02)
            note_chunk('思' * 30)
        await asyncio.sleep(0.05)
        return LLMResponse(
            content='',
            tool_calls=[
                ToolCallRequest(
                    id='call:final',
                    name='submit_final_result',
                    arguments={
                        'status': 'success',
                        'delivery_status': 'final',
                        'summary': 'done',
                        'answer': 'done',
                        'evidence': [],
                        'remaining_work': [],
                        'blocking_reason': '',
                    },
                )
            ],
            finish_reason='tool_calls',
            usage={'input_tokens': 8, 'output_tokens': 3},
        )


class _AppendingNodeTurnHarness:
    """把一整个节点轮跑到落账那一步，返回 append_node_output 收到的 kwargs。"""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path

    async def run_round(self, backend: _ReasoningChunkChatBackend) -> dict[str, object]:
        log_service = _FakeLogService()
        appended: list[dict[str, object]] = []

        def _append_node_output(*args, **kwargs) -> None:
            appended.append(dict(kwargs))

        log_service.append_node_output = _append_node_output  # type: ignore[method-assign]
        service = MainRuntimeService(
            chat_backend=backend,
            workspace_root=self.tmp_path,
            store_path=self.tmp_path / 'runtime.sqlite3',
            files_base_dir=self.tmp_path / 'tasks',
            artifact_dir=self.tmp_path / 'artifacts',
            governance_store_path=self.tmp_path / 'governance.sqlite3',
            execution_model_refs=['fake'],
            acceptance_model_refs=['fake'],
        )
        service._react_loop._log_service = log_service
        service.store = SimpleNamespace(
            get_task=lambda task_id: SimpleNamespace(
                task_id=task_id,
                session_id='web:shared',
                metadata={},
            ),
            get_node=lambda node_id: SimpleNamespace(
                node_id=node_id,
                prompt='demo goal',
                goal='demo goal',
                node_kind='execution',
            ),
        )
        await service._react_loop.run(
            task=SimpleNamespace(task_id='task-thinking-reading'),
            node=SimpleNamespace(node_id='node-thinking-reading', depth=0, node_kind='execution'),
            messages=[
                {'role': 'system', 'content': 'system'},
                {'role': 'user', 'content': '{"task_id":"task-thinking-reading","goal":"demo"}'},
            ],
            tools={
                'stop_tool_execution': _StageProtocolNoopTool('stop_tool_execution'),
                'submit_final_result': _submit_final_result_tool(),
                'spawn_child_nodes': _StageProtocolNoopTool('spawn_child_nodes'),
            },
            model_refs=['fake'],
            runtime_context={
                'task_id': 'task-thinking-reading',
                'node_id': 'node-thinking-reading',
                'session_key': 'web:shared',
                'actor_role': 'execution',
            },
            max_iterations=2,
        )
        assert appended, '这一轮没走到落账，读数用例白写'
        return appended[0]


@pytest.mark.asyncio
async def test_node_round_stops_thinking_clock_at_the_last_reasoning_chunk(tmp_path: Path) -> None:
    """节点这一轮的思考秒数随轮次落账，且不含思维之后写工具参数的那段。"""
    backend = _ReasoningChunkChatBackend(thought_chunks=2)
    appended = await _AppendingNodeTurnHarness(tmp_path).run_round(backend)

    seconds = float(appended.get('reasoning_seconds') or 0.0)
    assert seconds >= 0.03, seconds
    # 想完之后还有 0.05s 的参数流：读数不能把它一起吃进来。
    assert seconds < 0.09, seconds


@pytest.mark.asyncio
async def test_node_round_without_thought_chunks_reports_no_seconds(tmp_path: Path) -> None:
    """一个思维块都没到的轮次贡献 0：界面据此不画那一行，而不是报一个窗口时长。"""
    backend = _ReasoningChunkChatBackend(thought_chunks=0)
    appended = await _AppendingNodeTurnHarness(tmp_path).run_round(backend)

    assert float(appended.get('reasoning_seconds') or 0.0) == 0.0

