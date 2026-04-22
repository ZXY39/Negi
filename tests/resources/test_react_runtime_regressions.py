from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from g3ku.agent.catalog_store import ContextRecordV2
from g3ku.agent.tools.base import Tool
from g3ku.agent.tools.main_runtime import LoadSkillContextTool, LoadToolContextTool
from g3ku.content import ContentNavigationService, parse_content_envelope
from g3ku.providers.base import LLMResponse, ToolCallRequest
from g3ku.resources.embedded_mcp import EmbeddedMCPTool
from g3ku.resources.loader import ResourceLoader
from g3ku.resources.loader import ManifestBackedTool
from g3ku.resources.models import ResourceKind, ToolResourceDescriptor
from g3ku.resources.registry import ResourceRegistry
from g3ku.runtime.context.node_context_selection import NodeContextSelectionResult
from g3ku.runtime.tool_history import analyze_tool_call_history
from g3ku.runtime.tool_watchdog import ToolExecutionManager
from main.errors import TaskPausedError
from main.monitoring.log_service import (
    _EXECUTION_STAGE_MODE_SELF,
    _EXECUTION_STAGE_STATUS_ACTIVE,
    _EXECUTION_STAGE_STATUS_COMPLETED,
)
from main.runtime.node_prompt_contract import (
    NodeRuntimeToolContract,
    extract_node_dynamic_contract_payload,
    inject_node_dynamic_contract_message,
)
import main.service.runtime_service as runtime_service_module
from main.runtime.internal_tools import SubmitFinalResultTool, SubmitNextStageTool, SpawnChildNodesTool
from main.runtime.react_loop import ReActToolLoop
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
        return "submit_next_stage"

    @property
    def description(self) -> str:
        return "Capture runtime tool contract details during submit_next_stage execution."

    @property
    def parameters(self) -> dict[str, object]:
        return {
            "type": "object",
            "properties": {
                "stage_goal": {"type": "string"},
                "tool_round_budget": {"type": "integer"},
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
                "stage_goal": str(stage_goal or ""),
                "tool_round_budget": int(tool_round_budget or 0),
            },
            ensure_ascii=False,
        )


@pytest.mark.parametrize(
    "loader_tool_name",
    ["load_tool_context", "load_tool_context_v2", "load_skill_context", "load_skill_context_v2"],
)
def test_react_loop_treats_loader_tools_as_budget_bypass(loader_tool_name: str) -> None:
    assert ReActToolLoop._should_bypass_execution_budget(call=SimpleNamespace(name=loader_tool_name)) is True
    assert ReActToolLoop.model_visible_always_callable_tool_names(
        visible_tool_names=[loader_tool_name]
    ) == [loader_tool_name]


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
            "uri": "g3ku://resource/tool/content",
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
            "description": "Use the runtime schema when no model-only schema override is defined.",
            "parameters": {
                "type": "object",
                "properties": {
                    "task": {"type": "string", "description": "task"},
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
    async def _submit_stage(*args, **kwargs):
        _ = args, kwargs
        return {"ok": True}

    async def _spawn_children(*args, **kwargs):
        _ = args, kwargs
        return []

    async def _submit_final(payload: dict[str, object]) -> dict[str, object]:
        return dict(payload)

    tools = [
        SubmitNextStageTool(_submit_stage),
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

    assert tools[0].validate_params({"stage_goal": "investigate"}) == ["missing required tool_round_budget"]
    assert tools[1].validate_params({"children": [{"goal": "a", "prompt": "b"}]}) == [
        "missing required children[0].execution_policy"
    ]
    assert tools[2].validate_params({"status": "success"}) == [
        "missing required delivery_status",
        "missing required summary",
        "missing required answer",
        "missing required evidence",
        "missing required remaining_work",
        "missing required blocking_reason",
    ]


def test_compact_schema_memory_write_manifest_keeps_authoritative_schema_and_moves_guidance_to_toolskill() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    manifest = yaml.safe_load((repo_root / "tools" / "memory_write" / "resource.yaml").read_text(encoding="utf-8")) or {}
    toolskill = (repo_root / "tools" / "memory_write" / "toolskills" / "SKILL.md").read_text(encoding="utf-8")
    content_schema = (
        manifest.get("parameters", {})
        .get("properties", {})
        .get("content", {})
    )

    assert content_schema["type"] == "string"
    assert "queue for the memory agent" in str(content_schema.get("description") or "")
    assert "raw memory candidate" in toolskill.lower()
    assert "memory agent will decide the final compact `MEMORY.md` wording" in toolskill


def test_compact_schema_resource_tool_uses_manifest_model_visible_overrides(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    (workspace / "skills").mkdir(parents=True, exist_ok=True)
    (workspace / "tools").mkdir(parents=True, exist_ok=True)
    source_root = Path(__file__).resolve().parents[2] / "tools" / "memory_write"
    target_root = workspace / "tools" / "memory_write"
    import shutil
    shutil.copytree(source_root, target_root)

    registry = ResourceRegistry(workspace, skills_dir=workspace / "skills", tools_dir=workspace / "tools")
    snapshot = registry.discover()
    descriptor = snapshot.tools["memory_write"]
    tool = ResourceLoader(workspace).load_tool(
        descriptor,
        services={"memory_manager": SimpleNamespace()},
    )

    assert tool is not None
    schema = tool.to_model_schema()["function"]
    content_schema = schema["parameters"]["properties"]["content"]

    assert schema["description"].startswith("Queue a durable long-term memory write request.")
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

    assert schema["description"] == "Compact manifest-backed description."
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
    assert tool.to_model_schema() == tool.to_schema()


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
    assert tool.to_model_schema()["function"]["description"] == "Compact model-facing contract."
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

    assert tool.to_model_schema()["function"]["description"] == "Runtime model-facing contract."
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
            assert frame.get('callable_tool_names') == ['submit_next_stage']
            assert frame.get('model_visible_tool_names') == ['submit_next_stage']
            assert frame.get('model_visible_tool_selection_trace', {}).get('full_callable_tool_names') == [
                'submit_next_stage',
                'submit_final_result',
                'spawn_child_nodes',
                'stop_tool_execution',
            ]
            assert frame.get('model_visible_tool_selection_trace', {}).get('stage_locked_to_submit_next_stage') is True
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
            'tool_id': 'filesystem',
            'display_name': 'Filesystem',
            'description': 'Inspect files',
            'l0': 'Inspect files',
            'l1': 'Inspect files',
            'actions': [{'action_id': 'inspect', 'executor_names': ['filesystem']}],
        },
        {
            'tool_id': 'memory',
            'display_name': 'Memory',
            'description': 'Write memory facts',
            'l0': 'Write memory facts',
            'l1': 'Write memory facts',
            'actions': [{'action_id': 'write', 'executor_names': ['memory_write']}],
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
            'filesystem': _ModelSchemaRecordingTool(
                name='filesystem',
                authoritative_description='filesystem authoritative schema',
                model_description='filesystem compact model schema',
            ),
            'memory_write': _LargeModelSchemaTool(name='memory_write'),
            'stop_tool_execution': _StageProtocolNoopTool('stop_tool_execution'),
            'submit_next_stage': _StageProtocolNoopTool('submit_next_stage'),
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
        'submit_next_stage',
        'submit_final_result',
        'spawn_child_nodes',
        'stop_tool_execution',
    ]
    assert 'filesystem' not in emitted_tool_names
    assert 'memory_write' not in emitted_tool_names


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
            'model_visible_tool_names': [
                'stop_tool_execution',
                'submit_next_stage',
                'submit_final_result',
                'spawn_child_nodes',
                'load_tool_context_v2',
            ],
            'hydrated_executor_names': ['filesystem_write', 'content_describe'],
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
            'tool_id': 'filesystem',
            'display_name': 'Filesystem',
            'description': 'Inspect files',
            'primary_executor_name': 'filesystem_write',
            'l0': 'Inspect files',
            'l1': 'Inspect files',
            'actions': [
                {'action_id': 'legacy', 'executor_names': ['filesystem']},
                {
                    'action_id': 'describe',
                    'executor_names': ['filesystem_write', 'filesystem_edit', 'filesystem_propose_patch'],
                },
            ],
        },
        {
            'tool_id': 'content_navigation',
            'display_name': 'Content',
            'description': 'Inspect content',
            'primary_executor_name': 'content_describe',
            'l0': 'Inspect content',
            'l1': 'Inspect content',
            'actions': [
                {'action_id': 'legacy', 'executor_names': ['content']},
                {
                    'action_id': 'describe',
                    'executor_names': ['content_describe', 'content_search', 'content_open'],
                },
            ],
        },
        {
            'tool_id': 'memory',
            'display_name': 'Memory',
            'description': 'Write memory facts',
            'primary_executor_name': 'memory_write',
            'l0': 'Write memory facts',
            'l1': 'Write memory facts',
            'actions': [{'action_id': 'write', 'executor_names': ['memory_write']}],
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
            'filesystem': _LargeModelSchemaTool(name='filesystem'),
            'filesystem_write': _ModelSchemaRecordingTool(
                name='filesystem_write',
                authoritative_description='filesystem write authoritative schema',
                model_description='filesystem write compact model schema',
            ),
            'filesystem_edit': _LargeModelSchemaTool(name='filesystem_edit'),
            'filesystem_propose_patch': _LargeModelSchemaTool(name='filesystem_propose_patch'),
            'content': _LargeModelSchemaTool(name='content'),
            'content_describe': _ModelSchemaRecordingTool(
                name='content_describe',
                authoritative_description='content describe authoritative schema',
                model_description='content describe compact model schema',
            ),
            'content_search': _LargeModelSchemaTool(name='content_search'),
            'content_open': _LargeModelSchemaTool(name='content_open'),
            'memory_write': _LargeModelSchemaTool(name='memory_write'),
            'load_tool_context_v2': _ModelSchemaRecordingTool(
                name='load_tool_context_v2',
                authoritative_description='load tool context authoritative schema',
                model_description='load tool context compact model schema',
            ),
            'stop_tool_execution': _StageProtocolNoopTool('stop_tool_execution'),
            'submit_next_stage': _StageProtocolNoopTool('submit_next_stage'),
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
    assert 'submit_next_stage' in emitted_tool_names
    assert 'submit_final_result' in emitted_tool_names
    assert 'spawn_child_nodes' in emitted_tool_names
    assert 'load_tool_context_v2' in emitted_tool_names
    assert 'filesystem' not in emitted_tool_names
    assert 'content' not in emitted_tool_names
    assert 'filesystem_write' not in emitted_tool_names
    assert 'content_describe' not in emitted_tool_names
    assert 'memory_write' not in emitted_tool_names


@pytest.mark.asyncio
async def test_react_loop_execution_watchdog_keeps_long_tool_inline_with_manager_present() -> None:
    log_service = _FakeLogService()
    loop = ReActToolLoop(chat_backend=SimpleNamespace(), log_service=log_service)
    loop._tool_execution_manager = ToolExecutionManager()
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
async def test_react_loop_acceptance_watchdog_keeps_long_tool_inline_with_manager_present() -> None:
    log_service = _FakeLogService()
    loop = ReActToolLoop(chat_backend=SimpleNamespace(), log_service=log_service)
    loop._tool_execution_manager = ToolExecutionManager()
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


def test_execution_selector_uses_stable_visible_tool_order_independent_of_family_iteration_order(
) -> None:
    visible_tools = {
        'filesystem': _ModelSchemaRecordingTool(
            name='filesystem',
            authoritative_description='filesystem authoritative schema',
            model_description='filesystem compact model schema ' + ('F' * 1200),
        ),
        'memory_write': _ModelSchemaRecordingTool(
            name='memory_write',
            authoritative_description='memory authoritative schema',
            model_description='memory compact model schema ' + ('M' * 800),
        ),
        'submit_next_stage': _StageProtocolNoopTool('submit_next_stage'),
        'submit_final_result': _submit_final_result_tool(),
        'spawn_child_nodes': _StageProtocolNoopTool('spawn_child_nodes'),
    }
    def _service_for(families: list[dict[str, object]]) -> MainRuntimeService:
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
        service.execution_visible_tool_lightweight_items = lambda *, actor_role, session_id: families
        return service

    filesystem_first = _service_for(
        [
            {
                'tool_id': 'filesystem',
                'display_name': 'Filesystem',
                'description': 'Inspect files',
                'l0': 'Inspect files',
                'l1': 'Inspect files',
                'actions': [{'action_id': 'inspect', 'executor_names': ['filesystem']}],
            },
            {
                'tool_id': 'memory',
                'display_name': 'Memory',
                'description': 'Write memory facts',
                'l0': 'Write memory facts',
                'l1': 'Write memory facts',
                'actions': [{'action_id': 'write', 'executor_names': ['memory_write']}],
            },
        ]
    )
    memory_first = _service_for(
        [
            {
                'tool_id': 'memory',
                'display_name': 'Memory',
                'description': 'Write memory facts',
                'l0': 'Write memory facts',
                'l1': 'Write memory facts',
                'actions': [{'action_id': 'write', 'executor_names': ['memory_write']}],
            },
            {
                'tool_id': 'filesystem',
                'display_name': 'Filesystem',
                'description': 'Inspect files',
                'l0': 'Inspect files',
                'l1': 'Inspect files',
                'actions': [{'action_id': 'inspect', 'executor_names': ['filesystem']}],
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
        'submit_next_stage',
        'submit_final_result',
        'spawn_child_nodes',
    ]


def test_execution_stage_gate_allows_spawn_child_nodes_without_active_stage() -> None:
    log_service = _FakeLogService()
    log_service.execution_stage_gate_snapshot = lambda task_id, node_id: {
        'has_active_stage': False,
        'transition_required': False,
        'active_stage': None,
    }
    loop = ReActToolLoop(chat_backend=SimpleNamespace(), log_service=log_service, max_iterations=2)

    error = loop._execution_tool_gate_error(
        tool_name='spawn_child_nodes',
        runtime_context={
            'task_id': 'task-spawn-gate',
            'node_id': 'node-spawn-gate',
            'node_kind': 'execution',
        },
    )

    assert error == ''


def test_execution_selector_preserves_prior_model_visible_tool_order_across_turns() -> None:
    visible_tools = {
        'filesystem': _ModelSchemaRecordingTool(
            name='filesystem',
            authoritative_description='filesystem authoritative schema',
            model_description='filesystem compact model schema ' + ('F' * 1200),
        ),
        'spawn_child_nodes': _StageProtocolNoopTool('spawn_child_nodes'),
        'submit_final_result': _submit_final_result_tool(),
        'submit_next_stage': _StageProtocolNoopTool('submit_next_stage'),
    }
    log_service = _FakeLogService()
    log_service.upsert_frame(
        'task-prior-order',
        {
            'node_id': 'node-prior-order',
            'model_visible_tool_names': [
                'submit_next_stage',
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
            'actions': [{'action_id': 'inspect', 'executor_names': ['filesystem']}],
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

    assert selection['tool_names'] == ['submit_next_stage']
    assert selection['trace']['full_callable_tool_names'] == [
        'submit_next_stage',
        'submit_final_result',
        'spawn_child_nodes',
    ]
    assert selection['trace']['stage_locked_to_submit_next_stage'] is True


def test_execution_selector_appends_missing_tools_after_prior_stable_prefix() -> None:
    visible_tools = {
        'filesystem': _ModelSchemaRecordingTool(
            name='filesystem',
            authoritative_description='filesystem authoritative schema',
            model_description='filesystem compact model schema ' + ('F' * 1200),
        ),
        'spawn_child_nodes': _StageProtocolNoopTool('spawn_child_nodes'),
        'submit_final_result': _submit_final_result_tool(),
        'submit_next_stage': _StageProtocolNoopTool('submit_next_stage'),
    }
    log_service = _FakeLogService()
    log_service.upsert_frame(
        'task-partial-prior-order',
        {
            'node_id': 'node-partial-prior-order',
            'model_visible_tool_names': ['submit_next_stage'],
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
            'actions': [{'action_id': 'inspect', 'executor_names': ['filesystem']}],
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

    assert selection['tool_names'] == ['submit_next_stage']
    assert selection['trace']['full_callable_tool_names'] == [
        'submit_next_stage',
        'submit_final_result',
        'spawn_child_nodes',
    ]
    assert selection['trace']['stage_locked_to_submit_next_stage'] is True


def test_execution_selector_locks_callable_tools_to_submit_next_stage_when_transition_required() -> None:
    visible_tools = {
        'filesystem': _ModelSchemaRecordingTool(
            name='filesystem',
            authoritative_description='filesystem authoritative schema',
            model_description='filesystem compact model schema ' + ('F' * 1200),
        ),
        'spawn_child_nodes': _StageProtocolNoopTool('spawn_child_nodes'),
        'submit_final_result': _submit_final_result_tool(),
        'submit_next_stage': _StageProtocolNoopTool('submit_next_stage'),
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
            'actions': [{'action_id': 'inspect', 'executor_names': ['filesystem']}],
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

    assert selection['tool_names'] == ['submit_next_stage']
    assert selection['trace']['full_callable_tool_names'] == [
        'submit_next_stage',
        'submit_final_result',
        'spawn_child_nodes',
    ]
    assert selection['trace']['stage_locked_to_submit_next_stage'] is True


@pytest.mark.parametrize('loader_tool_name', ['load_tool_context', 'load_tool_context_v2'])
def test_promotes_selected_tool_next_turn_after_load_tool_context_variants(
    monkeypatch: pytest.MonkeyPatch,
    loader_tool_name: str,
) -> None:
    visible_tools = {
        'load_tool_context': _ModelSchemaRecordingTool(
            name='load_tool_context',
            authoritative_description='load tool context authoritative schema',
            model_description='load tool context compact schema',
        ),
        'load_tool_context_v2': _ModelSchemaRecordingTool(
            name='load_tool_context_v2',
            authoritative_description='load tool context v2 authoritative schema',
            model_description='load tool context v2 compact schema',
        ),
        'filesystem': _ModelSchemaRecordingTool(
            name='filesystem',
            authoritative_description='filesystem authoritative schema',
            model_description='filesystem compact model schema ' + ('F' * 1200),
        ),
        'submit_next_stage': _StageProtocolNoopTool('submit_next_stage'),
        'submit_final_result': _submit_final_result_tool(),
        'spawn_child_nodes': _StageProtocolNoopTool('spawn_child_nodes'),
    }
    monkeypatch.setattr(
        runtime_service_module,
        'build_execution_tool_selection',
        lambda **kwargs: SimpleNamespace(
            hydrated_tool_names=[
                'submit_next_stage',
                'submit_final_result',
                'spawn_child_nodes',
                loader_tool_name,
            ],
            lightweight_tool_ids=['filesystem'],
            schema_chars=321,
            trace={'mode': 'test'},
        ),
    )

    log_service = _FakeLogService()
    log_service.execution_stage_prompt_payload = lambda task_id, node_id: {
        'has_active_stage': True,
        'transition_required': False,
        'active_stage': {'stage_id': 'stage-1'},
    }
    service = object.__new__(MainRuntimeService)
    service.log_service = log_service
    service.store = SimpleNamespace(
        get_task=lambda task_id: SimpleNamespace(
            task_id=task_id,
            session_id='web:shared',
            metadata={'core_requirement': 'inspect project files'},
        ),
        get_node=lambda node_id: SimpleNamespace(
            node_id=node_id,
            prompt='inspect project files',
            goal='inspect project files',
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
            'actions': [{'action_id': 'inspect', 'executor_names': ['filesystem']}],
        }
    ]
    service.list_visible_tool_families = lambda *, actor_role, session_id: [
        SimpleNamespace(
            tool_id='filesystem',
            actions=[SimpleNamespace(executor_names=['filesystem'])],
        )
    ]

    loop = ReActToolLoop(chat_backend=SimpleNamespace(), log_service=log_service, max_iterations=2)
    loop._tool_context_hydration_promoter = service._promote_tool_context_hydration

    first_selection = service._select_model_visible_tool_schema_payload(
        task_id='task-tool-hydration',
        node_id='node-tool-hydration',
        node_kind='execution',
        visible_tools=visible_tools,
        runtime_context={
            'task_id': 'task-tool-hydration',
            'node_id': 'node-tool-hydration',
            'session_key': 'web:shared',
            'actor_role': 'execution',
        },
    )

    assert first_selection['lightweight_tool_ids'] == ['filesystem']
    assert loader_tool_name in first_selection['tool_names']
    assert 'filesystem' not in first_selection['tool_names']
    assert first_selection['trace']['base_schema_chars'] == 321
    assert first_selection['trace']['final_schema_chars'] == first_selection['schema_chars']

    log_service.upsert_frame(
        'task-tool-hydration',
        {
            'node_id': 'node-tool-hydration',
            'model_visible_tool_names': list(first_selection['tool_names']),
            'hydrated_executor_names': [],
        },
    )

    loop._promote_tool_context_hydration_after_results(
        task_id='task-tool-hydration',
        node_id='node-tool-hydration',
        response_tool_calls=[
            ToolCallRequest(
                id='call-load-tool-context',
                name=loader_tool_name,
                arguments={'tool_id': 'filesystem'},
            )
        ],
        results=[
            {
                'live_state': {
                    'tool_call_id': 'call-load-tool-context',
                    'tool_name': loader_tool_name,
                    'status': 'success',
                },
                'tool_message': {
                    'role': 'tool',
                    'tool_call_id': 'call-load-tool-context',
                    'name': loader_tool_name,
                    'content': json.dumps({'ok': True, 'tool_id': 'filesystem'}, ensure_ascii=False),
                },
                'raw_result': {
                    'ok': True,
                    'tool_id': 'filesystem',
                },
            }
        ],
        runtime_context={
            'task_id': 'task-tool-hydration',
            'node_id': 'node-tool-hydration',
            'session_key': 'web:shared',
            'actor_role': 'execution',
        },
    )

    promoted_frame = log_service.read_runtime_frame('task-tool-hydration', 'node-tool-hydration')
    assert promoted_frame.get('hydrated_executor_names') in (None, [])

    next_selection = service._select_model_visible_tool_schema_payload(
        task_id='task-tool-hydration',
        node_id='node-tool-hydration',
        node_kind='execution',
        visible_tools=visible_tools,
        runtime_context={
            'task_id': 'task-tool-hydration',
            'node_id': 'node-tool-hydration',
            'session_key': 'web:shared',
            'actor_role': 'execution',
        },
    )

    assert next_selection['tool_names'] == [
        'submit_next_stage',
        'submit_final_result',
        'spawn_child_nodes',
        loader_tool_name,
    ]
    assert next_selection['schema_chars'] == first_selection['schema_chars']
    assert next_selection['trace']['base_schema_chars'] == 321
    assert next_selection['trace']['final_schema_chars'] == next_selection['schema_chars']
    assert next_selection['trace']['promoted_hydrated_executor_names'] == []


def test_promote_tool_context_hydration_keeps_executor_requests_precise() -> None:
    log_service = _FakeLogService()
    service = object.__new__(MainRuntimeService)
    service.log_service = log_service
    service.store = SimpleNamespace(
        get_task=lambda task_id: SimpleNamespace(task_id=task_id, session_id='web:shared', metadata={}),
        get_node=lambda node_id: SimpleNamespace(node_id=node_id, node_kind='execution'),
    )
    service.list_visible_tool_families = lambda *, actor_role, session_id: [
        SimpleNamespace(
            tool_id='filesystem',
            actions=[
                SimpleNamespace(
                    executor_names=['filesystem_write', 'filesystem_edit', 'filesystem_propose_patch']
                )
            ],
        )
    ]
    log_service.upsert_frame('task-hydration-precise', {'node_id': 'node-hydration-precise'})

    service._promote_tool_context_hydration(
        task_id='task-hydration-precise',
        node_id='node-hydration-precise',
        tool_call=SimpleNamespace(name='load_tool_context', arguments={'tool_id': 'filesystem_write'}),
        raw_result={'ok': True, 'tool_id': 'filesystem_write'},
        runtime_context={'session_key': 'web:shared', 'actor_role': 'execution'},
    )

    promoted_frame = log_service.read_runtime_frame('task-hydration-precise', 'node-hydration-precise')
    assert promoted_frame['hydrated_executor_names'] == ['filesystem_write']
    assert promoted_frame['hydrated_executor_state'] == ['filesystem_write']


def test_promote_tool_context_hydration_rejects_family_names() -> None:
    log_service = _FakeLogService()
    service = object.__new__(MainRuntimeService)
    service.log_service = log_service
    service.store = SimpleNamespace(
        get_task=lambda task_id: SimpleNamespace(task_id=task_id, session_id='web:shared', metadata={}),
        get_node=lambda node_id: SimpleNamespace(node_id=node_id, node_kind='execution'),
    )
    service.list_visible_tool_families = lambda *, actor_role, session_id: [
        SimpleNamespace(
            tool_id='filesystem',
            actions=[
                SimpleNamespace(
                    executor_names=[
                        'filesystem_write',
                        'filesystem_edit',
                        'filesystem_propose_patch',
                        'filesystem_write',
                    ]
                )
            ],
        )
    ]
    log_service.upsert_frame('task-hydration-family', {'node_id': 'node-hydration-family'})

    service._promote_tool_context_hydration(
        task_id='task-hydration-family',
        node_id='node-hydration-family',
        tool_call=SimpleNamespace(name='load_tool_context', arguments={'tool_id': 'filesystem'}),
        raw_result={'ok': True, 'tool_id': 'filesystem'},
        runtime_context={'session_key': 'web:shared', 'actor_role': 'execution'},
    )

    promoted_frame = log_service.read_runtime_frame('task-hydration-family', 'node-hydration-family')
    assert promoted_frame.get('hydrated_executor_names') in (None, [])


def test_promote_tool_context_hydration_applies_lru_limit() -> None:
    log_service = _FakeLogService()
    service = object.__new__(MainRuntimeService)
    service.log_service = log_service
    service._hydrated_tool_limit = 3
    service.store = SimpleNamespace(
        get_task=lambda task_id: SimpleNamespace(task_id=task_id, session_id='web:shared', metadata={}),
        get_node=lambda node_id: SimpleNamespace(node_id=node_id, node_kind='execution'),
    )
    service.list_visible_tool_families = lambda *, actor_role, session_id: [
        SimpleNamespace(tool_id='filesystem', actions=[SimpleNamespace(executor_names=['filesystem_write'])]),
        SimpleNamespace(tool_id='content_navigation', actions=[SimpleNamespace(executor_names=['content_open'])]),
        SimpleNamespace(tool_id='agent_browser', actions=[SimpleNamespace(executor_names=['agent_browser'])]),
        SimpleNamespace(tool_id='web_fetch', actions=[SimpleNamespace(executor_names=['web_fetch'])]),
    ]
    log_service.upsert_frame('task-hydration-lru', {'node_id': 'node-hydration-lru'})

    for tool_id in ['filesystem_write', 'content_open', 'agent_browser', 'web_fetch']:
        service._promote_tool_context_hydration(
            task_id='task-hydration-lru',
            node_id='node-hydration-lru',
            tool_call=SimpleNamespace(name='load_tool_context', arguments={'tool_id': tool_id}),
            raw_result={'ok': True, 'tool_id': tool_id},
            runtime_context={'session_key': 'web:shared', 'actor_role': 'execution'},
        )

    promoted_frame = log_service.read_runtime_frame('task-hydration-lru', 'node-hydration-lru')
    assert promoted_frame['hydrated_executor_names'] == [
        'content_open',
        'agent_browser',
        'web_fetch',
    ]
    assert promoted_frame['hydrated_executor_state'] == [
        'content_open',
        'agent_browser',
        'web_fetch',
    ]

    service._promote_tool_context_hydration(
        task_id='task-hydration-lru',
        node_id='node-hydration-lru',
        tool_call=SimpleNamespace(name='load_tool_context', arguments={'tool_id': 'content_open'}),
        raw_result={'ok': True, 'tool_id': 'content_open'},
        runtime_context={'session_key': 'web:shared', 'actor_role': 'execution'},
    )

    promoted_frame = log_service.read_runtime_frame('task-hydration-lru', 'node-hydration-lru')
    assert promoted_frame['hydrated_executor_names'] == [
        'agent_browser',
        'web_fetch',
        'content_open',
    ]
    assert promoted_frame['hydrated_executor_state'] == [
        'agent_browser',
        'web_fetch',
        'content_open',
    ]


def test_promote_tool_context_hydration_skips_fixed_builtin_executors() -> None:
    log_service = _FakeLogService()
    service = object.__new__(MainRuntimeService)
    service.log_service = log_service
    service.store = SimpleNamespace(
        get_task=lambda task_id: SimpleNamespace(task_id=task_id, session_id='web:shared', metadata={}),
        get_node=lambda node_id: SimpleNamespace(node_id=node_id, node_kind='execution'),
    )
    service.list_visible_tool_families = lambda *, actor_role, session_id: [
        SimpleNamespace(tool_id='exec', actions=[SimpleNamespace(executor_names=['exec'])]),
        SimpleNamespace(tool_id='agent_browser', actions=[SimpleNamespace(executor_names=['agent_browser'])]),
    ]
    log_service.upsert_frame('task-hydration-fixed-builtin', {'node_id': 'node-hydration-fixed-builtin'})

    service._promote_tool_context_hydration(
        task_id='task-hydration-fixed-builtin',
        node_id='node-hydration-fixed-builtin',
        tool_call=SimpleNamespace(name='load_tool_context', arguments={'tool_id': 'exec'}),
        raw_result={'ok': True, 'tool_id': 'exec'},
        runtime_context={'session_key': 'web:shared', 'actor_role': 'execution'},
    )

    promoted_frame = log_service.read_runtime_frame('task-hydration-fixed-builtin', 'node-hydration-fixed-builtin')
    assert promoted_frame.get('hydrated_executor_names') in (None, [])
    assert promoted_frame.get('hydrated_executor_state') in (None, [])


def test_main_runtime_service_default_hydrated_tool_limit_is_16() -> None:
    service = object.__new__(MainRuntimeService)

    assert service._hydrated_tool_limit_value() == 16


def test_promote_tool_context_hydration_uses_successful_result_without_matching_call_id() -> None:
    promoted_calls: list[dict[str, object]] = []
    loop = ReActToolLoop(chat_backend=SimpleNamespace(), log_service=_FakeLogService(), max_iterations=2)
    loop._tool_context_hydration_promoter = lambda **kwargs: promoted_calls.append(dict(kwargs))

    loop._promote_tool_context_hydration_after_results(
        task_id='task-result-driven-hydration',
        node_id='node-result-driven-hydration',
        response_tool_calls=[
            ToolCallRequest(
                id='call-original',
                name='load_tool_context',
                arguments={'tool_id': 'filesystem_write'},
            )
        ],
        results=[
            {
                'live_state': {
                    'tool_call_id': 'call-original|fc_extra_suffix',
                    'tool_name': 'load_tool_context',
                    'status': 'success',
                },
                'tool_message': {
                    'role': 'tool',
                    'tool_call_id': 'call-original|fc_extra_suffix',
                    'name': 'load_tool_context',
                    'content': json.dumps({'ok': True, 'tool_id': 'filesystem_write'}, ensure_ascii=False),
                },
                'raw_result': {
                    'ok': True,
                    'tool_id': 'filesystem_write',
                },
            }
        ],
        runtime_context={
            'task_id': 'task-result-driven-hydration',
            'node_id': 'node-result-driven-hydration',
            'session_key': 'web:shared',
            'actor_role': 'execution',
        },
    )

    assert len(promoted_calls) == 1
    promoted = promoted_calls[0]
    assert getattr(promoted['tool_call'], 'name') == 'load_tool_context'
    assert getattr(promoted['tool_call'], 'arguments') == {'tool_id': 'filesystem_write'}
    assert promoted['raw_result'] == {'ok': True, 'tool_id': 'filesystem_write'}


def test_promote_tool_context_hydration_accepts_json_string_result_payload() -> None:
    promoted_calls: list[dict[str, object]] = []
    loop = ReActToolLoop(chat_backend=SimpleNamespace(), log_service=_FakeLogService(), max_iterations=2)
    loop._tool_context_hydration_promoter = lambda **kwargs: promoted_calls.append(dict(kwargs))

    loop._promote_tool_context_hydration_after_results(
        task_id='task-result-driven-hydration-json',
        node_id='node-result-driven-hydration-json',
        response_tool_calls=[
            ToolCallRequest(
                id='call-original-json',
                name='load_tool_context',
                arguments={'tool_id': 'filesystem_write'},
            )
        ],
        results=[
            {
                'live_state': {
                    'tool_call_id': 'call-original-json|fc_extra_suffix',
                    'tool_name': 'load_tool_context',
                    'status': 'success',
                },
                'tool_message': {
                    'role': 'tool',
                    'tool_call_id': 'call-original-json|fc_extra_suffix',
                    'name': 'load_tool_context',
                    'content': json.dumps({'ok': True, 'tool_id': 'filesystem_write'}, ensure_ascii=False),
                },
                'raw_result': json.dumps({'ok': True, 'tool_id': 'filesystem_write'}, ensure_ascii=False),
            }
        ],
        runtime_context={
            'task_id': 'task-result-driven-hydration-json',
            'node_id': 'node-result-driven-hydration-json',
            'session_key': 'web:shared',
            'actor_role': 'execution',
        },
    )

    assert len(promoted_calls) == 1
    promoted = promoted_calls[0]
    assert getattr(promoted['tool_call'], 'arguments') == {'tool_id': 'filesystem_write'}
    assert promoted['raw_result'] == {'ok': True, 'tool_id': 'filesystem_write'}


@pytest.mark.asyncio
async def test_execute_tool_calls_promotes_tool_context_hydration_immediately() -> None:
    class _InlineLoadTool(Tool):
        @property
        def name(self) -> str:
            return 'load_tool_context'

        @property
        def description(self) -> str:
            return 'inline load tool context'

        @property
        def parameters(self) -> dict[str, object]:
            return {'type': 'object', 'properties': {}, 'required': []}

        async def execute(self, **kwargs):
            _ = kwargs
            return {'ok': True, 'tool_id': 'filesystem_write'}

    promoted_calls: list[dict[str, object]] = []
    loop = ReActToolLoop(chat_backend=SimpleNamespace(), log_service=_FakeLogService(), max_iterations=2)
    loop._tool_context_hydration_promoter = lambda **kwargs: promoted_calls.append(dict(kwargs))

    task = SimpleNamespace(task_id='task-inline-hydration')
    node = SimpleNamespace(node_id='node-inline-hydration', depth=0, node_kind='execution')
    response_tool_calls = [
        ToolCallRequest(
            id='call-inline-load-tool-context',
            name='load_tool_context',
            arguments={'tool_id': 'filesystem_write'},
        )
    ]
    tools = {'load_tool_context': _InlineLoadTool()}

    await loop._execute_tool_calls(
        task=task,
        node=node,
        response_tool_calls=response_tool_calls,
        tools=tools,
        allowed_content_refs=[],
        runtime_context={'task_id': task.task_id, 'node_id': node.node_id, 'actor_role': 'execution'},
    )

    assert len(promoted_calls) == 1
    promoted = promoted_calls[0]
    assert getattr(promoted['tool_call'], 'name') == 'load_tool_context'
    assert getattr(promoted['tool_call'], 'arguments') == {'tool_id': 'filesystem_write'}
    assert promoted['raw_result']['tool_id'] == 'filesystem_write'


@pytest.mark.asyncio
async def test_execute_tool_calls_promotes_tool_context_hydration_immediately_from_json_string() -> None:
    class _InlineLoadTool(Tool):
        @property
        def name(self) -> str:
            return 'load_tool_context'

        @property
        def description(self) -> str:
            return 'inline load tool context'

        @property
        def parameters(self) -> dict[str, object]:
            return {'type': 'object', 'properties': {}, 'required': []}

        async def execute(self, **kwargs):
            _ = kwargs
            return json.dumps({'ok': True, 'tool_id': 'filesystem_write'}, ensure_ascii=False)

    promoted_calls: list[dict[str, object]] = []
    loop = ReActToolLoop(chat_backend=SimpleNamespace(), log_service=_FakeLogService(), max_iterations=2)
    loop._tool_context_hydration_promoter = lambda **kwargs: promoted_calls.append(dict(kwargs))

    task = SimpleNamespace(task_id='task-inline-hydration-json')
    node = SimpleNamespace(node_id='node-inline-hydration-json', depth=0, node_kind='execution')
    response_tool_calls = [
        ToolCallRequest(
            id='call-inline-load-tool-context-json',
            name='load_tool_context',
            arguments={'tool_id': 'filesystem_write'},
        )
    ]
    tools = {'load_tool_context': _InlineLoadTool()}

    await loop._execute_tool_calls(
        task=task,
        node=node,
        response_tool_calls=response_tool_calls,
        tools=tools,
        allowed_content_refs=[],
        runtime_context={'task_id': task.task_id, 'node_id': node.node_id, 'actor_role': 'execution'},
    )

    assert len(promoted_calls) == 1
    promoted = promoted_calls[0]
    assert getattr(promoted['tool_call'], 'arguments') == {'tool_id': 'filesystem_write'}
    assert promoted['raw_result']['tool_id'] == 'filesystem_write'


def test_execution_selector_prefers_split_executors_over_legacy_monoliths() -> None:
    visible_tools = {
        'filesystem': _ModelSchemaRecordingTool(
            name='filesystem',
            authoritative_description='filesystem authoritative schema',
            model_description='filesystem compact model schema',
        ),
        'filesystem_write': _ModelSchemaRecordingTool(
            name='filesystem_write',
            authoritative_description='filesystem write authoritative schema',
            model_description='filesystem write compact model schema',
        ),
        'content': _ModelSchemaRecordingTool(
            name='content',
            authoritative_description='content authoritative schema',
            model_description='content compact model schema',
        ),
        'content_describe': _ModelSchemaRecordingTool(
            name='content_describe',
            authoritative_description='content describe authoritative schema',
            model_description='content describe compact model schema',
        ),
        'submit_next_stage': _StageProtocolNoopTool('submit_next_stage'),
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
            'tool_id': 'filesystem',
            'display_name': 'Filesystem',
            'description': 'Inspect files',
            'primary_executor_name': 'filesystem_write',
            'l0': 'Inspect files',
            'l1': 'Inspect files',
            'actions': [
                {'action_id': 'legacy', 'executor_names': ['filesystem']},
                {'action_id': 'write', 'executor_names': ['filesystem_write']},
            ],
        },
        {
            'tool_id': 'content_navigation',
            'display_name': 'Content',
            'description': 'Inspect content',
            'primary_executor_name': 'content_describe',
            'l0': 'Inspect content',
            'l1': 'Inspect content',
            'actions': [
                {'action_id': 'legacy', 'executor_names': ['content']},
                {'action_id': 'describe', 'executor_names': ['content_describe']},
            ],
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

    assert 'filesystem' not in first_selection['tool_names']
    assert 'content' not in first_selection['tool_names']
    assert 'filesystem_write' not in first_selection['tool_names']
    assert 'content_describe' not in first_selection['tool_names']
    assert 'content_describe' in first_selection['candidate_tool_names']

    log_service.upsert_frame(
        'task-split-preference',
        {
            'node_id': 'node-split-preference',
            'model_visible_tool_names': list(first_selection['tool_names']),
            'hydrated_executor_names': ['filesystem', 'content'],
        },
    )

    hydrated_selection = service._select_model_visible_tool_schema_payload(
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

    assert 'filesystem' not in hydrated_selection['tool_names']
    assert 'content' not in hydrated_selection['tool_names']
    assert 'filesystem_write' in hydrated_selection['tool_names']
    assert 'content_describe' in hydrated_selection['tool_names']
    assert hydrated_selection['hydrated_executor_names'] == ['filesystem_write', 'content_describe']
    assert hydrated_selection['trace']['promoted_hydrated_executor_names'] == ['filesystem_write', 'content_describe']


def test_execution_selector_preserves_concrete_hydrated_tool_names_without_family_rewrite() -> None:
    visible_tools = {
        'task_list': _ModelSchemaRecordingTool(
            name='task_list',
            authoritative_description='task list authoritative schema',
            model_description='task list compact model schema',
        ),
        'submit_next_stage': _StageProtocolNoopTool('submit_next_stage'),
        'submit_final_result': _submit_final_result_tool(),
        'spawn_child_nodes': _StageProtocolNoopTool('spawn_child_nodes'),
    }

    log_service = _FakeLogService()
    log_service.upsert_frame(
        'task-concrete-hydration',
        {
            'node_id': 'node-concrete-hydration',
            'hydrated_executor_names': ['task_list'],
        },
    )
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
            'tool_id': 'task_runtime',
            'display_name': 'Task Runtime',
            'description': 'Inspect task tree',
            'l0': 'Inspect task tree',
            'l1': 'Inspect task tree',
            'actions': [
                {'action_id': 'list', 'executor_names': ['task_list']},
            ],
        }
    ]

    selection = service._select_model_visible_tool_schema_payload(
        task_id='task-concrete-hydration',
        node_id='node-concrete-hydration',
        node_kind='execution',
        visible_tools=visible_tools,
        runtime_context={
            'task_id': 'task-concrete-hydration',
            'node_id': 'node-concrete-hydration',
            'session_key': 'web:shared',
            'actor_role': 'execution',
        },
    )

    assert selection['tool_names'] == [
        'submit_next_stage',
        'submit_final_result',
        'spawn_child_nodes',
        'task_list',
    ]
    assert selection['hydrated_executor_names'] == ['task_list']
    assert selection['trace']['promoted_hydrated_executor_names'] == ['task_list']


def test_tool_provider_includes_hydrated_tools_even_when_selection_cache_excludes_them() -> None:
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
            'filesystem_write': _ModelSchemaRecordingTool(
                name='filesystem_write',
                authoritative_description='filesystem write authoritative schema',
                model_description='filesystem write model schema',
            ),
        }
    )
    service._external_tool_provider = lambda _node: {}
    service._builtin_tool_cache = None
    service._node_context_selection_cache = {
        ('task-hydrated-provider', 'node-hydrated-provider'): {
            'selection': NodeContextSelectionResult(
                mode='dense_rerank',
                selected_tool_names=['content_open'],
                candidate_tool_names=['content_open', 'filesystem_write'],
            )
        }
    }
    service.log_service = _FakeLogService()
    service.log_service.upsert_frame(
        'task-hydrated-provider',
        {
            'node_id': 'node-hydrated-provider',
            'hydrated_executor_names': ['filesystem_write'],
        },
    )
    service.list_effective_tool_names = lambda *, actor_role, session_id: ['content_open', 'filesystem_write']
    service._actor_role_for_node = lambda node: 'execution'

    node = SimpleNamespace(
        task_id='task-hydrated-provider',
        node_id='node-hydrated-provider',
        node_kind='execution',
        can_spawn_children=False,
    )
    provided = service._tool_provider(node)

    assert 'content_open' in provided
    assert 'filesystem_write' in provided


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
    service._external_tool_provider = lambda _node: {}
    service._builtin_tool_cache = None
    service._node_context_selection_cache = {
        ('task-restored-provider', 'node-restored-provider'): {
            'selection': NodeContextSelectionResult(
                mode='persisted_frame_restore',
                selected_tool_names=['load_tool_context'],
                candidate_tool_names=['content_open', 'content_search'],
            )
        }
    }
    service.log_service = _FakeLogService()
    service.list_effective_tool_names = (
        lambda *, actor_role, session_id: ['load_tool_context', 'content_open', 'content_search']
    )
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
        'submit_next_stage': _StageProtocolNoopTool('submit_next_stage'),
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
            'actions': [{'action_id': 'fetch', 'executor_names': ['web_fetch']}],
        },
        {
            'tool_id': 'agent_browser',
            'display_name': 'Agent Browser',
            'description': 'Browse public web pages',
            'l0': 'Browse public web pages',
            'l1': 'Browse public web pages',
            'actions': [{'action_id': 'browse', 'executor_names': ['agent_browser']}],
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
        'submit_next_stage',
        'submit_final_result',
        'spawn_child_nodes',
    ]


def test_prepare_messages_rebuilds_prompt_from_completed_stages_and_active_window() -> None:
    loop = ReActToolLoop(chat_backend=SimpleNamespace(), log_service=_FakeLogService(), max_iterations=2)
    loop._log_service._store._node = SimpleNamespace(
        metadata={
            "execution_stages": {
                "active_stage_id": "stage-2",
                "transition_required": False,
                "stages": [
                    {
                        "stage_id": "stage-1",
                        "stage_index": 1,
                        "stage_kind": "normal",
                        "system_generated": False,
                        "mode": "自主执行",
                        "status": "完成",
                        "stage_goal": "inspect the first stage",
                        "completed_stage_summary": "finished stage one",
                        "key_refs": [
                            {
                                "ref": "artifact:artifact:stage-one",
                                "note": "stage one evidence summary",
                            }
                        ],
                        "tool_round_budget": 2,
                        "tool_rounds_used": 1,
                    },
                    {
                        "stage_id": "stage-2",
                        "stage_index": 2,
                        "stage_kind": "normal",
                        "system_generated": False,
                        "mode": "自主执行",
                        "status": "进行中",
                        "stage_goal": "inspect the second stage",
                        "tool_round_budget": 3,
                        "tool_rounds_used": 0,
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
                    "function": {"name": "submit_next_stage", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "name": "submit_next_stage", "tool_call_id": "call-stage-1", "content": '{"ok": true}'},
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
                    "function": {"name": "submit_next_stage", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "name": "submit_next_stage", "tool_call_id": "call-stage-2", "content": '{"ok": true}'},
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


def test_prepare_messages_keeps_active_stage_and_latest_three_completed_stage_windows() -> None:
    loop = ReActToolLoop(chat_backend=SimpleNamespace(), log_service=_FakeLogService(), max_iterations=2)
    loop._log_service._store._node = SimpleNamespace(
        metadata={
            "execution_stages": {
                "active_stage_id": "stage-5",
                "transition_required": False,
                "stages": [
                    {
                        "stage_id": "stage-1",
                        "stage_index": 1,
                        "stage_kind": "normal",
                        "system_generated": False,
                        "mode": _EXECUTION_STAGE_MODE_SELF,
                        "status": _EXECUTION_STAGE_STATUS_COMPLETED,
                        "stage_goal": "inspect stage one",
                        "completed_stage_summary": "finished stage one",
                        "key_refs": [],
                        "tool_round_budget": 2,
                        "tool_rounds_used": 1,
                    },
                    {
                        "stage_id": "stage-2",
                        "stage_index": 2,
                        "stage_kind": "normal",
                        "system_generated": False,
                        "mode": _EXECUTION_STAGE_MODE_SELF,
                        "status": _EXECUTION_STAGE_STATUS_COMPLETED,
                        "stage_goal": "inspect stage two",
                        "completed_stage_summary": "finished stage two",
                        "key_refs": [],
                        "tool_round_budget": 2,
                        "tool_rounds_used": 1,
                    },
                    {
                        "stage_id": "stage-3",
                        "stage_index": 3,
                        "stage_kind": "normal",
                        "system_generated": False,
                        "mode": _EXECUTION_STAGE_MODE_SELF,
                        "status": _EXECUTION_STAGE_STATUS_COMPLETED,
                        "stage_goal": "inspect stage three",
                        "completed_stage_summary": "finished stage three",
                        "key_refs": [],
                        "tool_round_budget": 2,
                        "tool_rounds_used": 1,
                    },
                    {
                        "stage_id": "stage-4",
                        "stage_index": 4,
                        "stage_kind": "normal",
                        "system_generated": False,
                        "mode": _EXECUTION_STAGE_MODE_SELF,
                        "status": _EXECUTION_STAGE_STATUS_COMPLETED,
                        "stage_goal": "inspect stage four",
                        "completed_stage_summary": "finished stage four",
                        "key_refs": [],
                        "tool_round_budget": 2,
                        "tool_rounds_used": 1,
                    },
                    {
                        "stage_id": "stage-5",
                        "stage_index": 5,
                        "stage_kind": "normal",
                        "system_generated": False,
                        "mode": _EXECUTION_STAGE_MODE_SELF,
                        "status": _EXECUTION_STAGE_STATUS_ACTIVE,
                        "stage_goal": "inspect stage five",
                        "tool_round_budget": 3,
                        "tool_rounds_used": 0,
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
                    "function": {"name": "submit_next_stage", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "name": "submit_next_stage", "tool_call_id": "call-stage-1", "content": '{"ok": true}'},
        {"role": "assistant", "content": "stage one raw detail"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call-stage-2",
                    "type": "function",
                    "function": {"name": "submit_next_stage", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "name": "submit_next_stage", "tool_call_id": "call-stage-2", "content": '{"ok": true}'},
        {"role": "assistant", "content": "stage two raw detail"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call-stage-3",
                    "type": "function",
                    "function": {"name": "submit_next_stage", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "name": "submit_next_stage", "tool_call_id": "call-stage-3", "content": '{"ok": true}'},
        {"role": "assistant", "content": "stage three raw detail"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call-stage-4",
                    "type": "function",
                    "function": {"name": "submit_next_stage", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "name": "submit_next_stage", "tool_call_id": "call-stage-4", "content": '{"ok": true}'},
        {"role": "assistant", "content": "stage four raw detail"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call-stage-5",
                    "type": "function",
                    "function": {"name": "submit_next_stage", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "name": "submit_next_stage", "tool_call_id": "call-stage-5", "content": '{"ok": true}'},
        {"role": "assistant", "content": "current stage assistant detail"},
        {"role": "tool", "name": "filesystem", "tool_call_id": "call-a", "content": "current stage tool output"},
    ]

    prepared = loop._prepare_messages(original, runtime_context={"task_id": "task-1", "node_id": "node-1"})

    rendered_contents = [str(item.get("content") or "") for item in prepared]
    assert "stage two raw detail" in rendered_contents
    assert "stage three raw detail" in rendered_contents
    assert "stage four raw detail" in rendered_contents
    assert "current stage assistant detail" in rendered_contents
    assert "current stage tool output" in rendered_contents
    assert "stage one raw detail" not in rendered_contents

    compact_blocks = [
        content
        for content in rendered_contents
        if content.startswith("[G3KU_STAGE_COMPACT_V1]")
    ]
    assert len(compact_blocks) == 1
    compact_payload = json.loads(compact_blocks[0].split("\n", 1)[1])
    assert compact_payload["stage_index"] == 1
    assert compact_payload["completed_stage_summary"] == "finished stage one"


def test_prepare_messages_is_idempotent_for_compacted_stage_prompt() -> None:
    loop = ReActToolLoop(chat_backend=SimpleNamespace(), log_service=_FakeLogService(), max_iterations=2)
    loop._log_service._store._node = SimpleNamespace(
        metadata={
            "execution_stages": {
                "active_stage_id": "stage-2",
                "transition_required": False,
                "stages": [
                    {
                        "stage_id": "stage-1",
                        "stage_index": 1,
                        "stage_kind": "normal",
                        "system_generated": False,
                        "mode": "自主执行",
                        "status": "完成",
                        "stage_goal": "inspect the first stage",
                        "completed_stage_summary": "finished stage one",
                        "key_refs": [],
                        "tool_round_budget": 2,
                        "tool_rounds_used": 1,
                    },
                    {
                        "stage_id": "stage-2",
                        "stage_index": 2,
                        "stage_kind": "normal",
                        "system_generated": False,
                        "mode": "自主执行",
                        "status": "进行中",
                        "stage_goal": "inspect the second stage",
                        "tool_round_budget": 3,
                        "tool_rounds_used": 0,
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
                    "function": {"name": "submit_next_stage", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "name": "submit_next_stage", "tool_call_id": "call-stage-1", "content": '{"ok": true}'},
        {"role": "assistant", "content": "stage one raw detail"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call-stage-2",
                    "type": "function",
                    "function": {"name": "submit_next_stage", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "name": "submit_next_stage", "tool_call_id": "call-stage-2", "content": '{"ok": true}'},
        {"role": "assistant", "content": "current stage assistant detail"},
        {"role": "tool", "name": "filesystem", "tool_call_id": "call-a", "content": "current stage tool output"},
    ]

    prepared = loop._prepare_messages(original, runtime_context={"task_id": "task-1", "node_id": "node-1"})
    prepared_again = loop._prepare_messages(prepared, runtime_context={"task_id": "task-1", "node_id": "node-1"})

    assert prepared_again == prepared


def test_prepare_messages_repairs_split_submit_next_stage_boundary_in_retained_window() -> None:
    loop = ReActToolLoop(chat_backend=SimpleNamespace(), log_service=_FakeLogService(), max_iterations=2)
    loop._log_service._store._node = SimpleNamespace(
        metadata={
            "execution_stages": {
                "active_stage_id": "stage-7",
                "transition_required": False,
                "stages": [
                    {
                        "stage_id": "stage-1",
                        "stage_index": 1,
                        "stage_kind": "normal",
                        "system_generated": False,
                        "mode": _EXECUTION_STAGE_MODE_SELF,
                        "status": _EXECUTION_STAGE_STATUS_COMPLETED,
                        "stage_goal": "inspect stage one",
                        "completed_stage_summary": "finished stage one",
                        "key_refs": [],
                        "tool_round_budget": 2,
                        "tool_rounds_used": 1,
                    },
                    {
                        "stage_id": "stage-2",
                        "stage_index": 2,
                        "stage_kind": "normal",
                        "system_generated": False,
                        "mode": _EXECUTION_STAGE_MODE_SELF,
                        "status": _EXECUTION_STAGE_STATUS_COMPLETED,
                        "stage_goal": "inspect stage two",
                        "completed_stage_summary": "finished stage two",
                        "key_refs": [],
                        "tool_round_budget": 2,
                        "tool_rounds_used": 1,
                    },
                    {
                        "stage_id": "stage-3",
                        "stage_index": 3,
                        "stage_kind": "normal",
                        "system_generated": False,
                        "mode": _EXECUTION_STAGE_MODE_SELF,
                        "status": _EXECUTION_STAGE_STATUS_COMPLETED,
                        "stage_goal": "inspect stage three",
                        "completed_stage_summary": "finished stage three",
                        "key_refs": [],
                        "tool_round_budget": 2,
                        "tool_rounds_used": 1,
                    },
                    {
                        "stage_id": "stage-4",
                        "stage_index": 4,
                        "stage_kind": "normal",
                        "system_generated": False,
                        "mode": _EXECUTION_STAGE_MODE_SELF,
                        "status": _EXECUTION_STAGE_STATUS_COMPLETED,
                        "stage_goal": "inspect stage four",
                        "completed_stage_summary": "finished stage four",
                        "key_refs": [],
                        "tool_round_budget": 2,
                        "tool_rounds_used": 1,
                    },
                    {
                        "stage_id": "stage-5",
                        "stage_index": 5,
                        "stage_kind": "normal",
                        "system_generated": False,
                        "mode": _EXECUTION_STAGE_MODE_SELF,
                        "status": _EXECUTION_STAGE_STATUS_COMPLETED,
                        "stage_goal": "inspect stage five",
                        "completed_stage_summary": "finished stage five",
                        "key_refs": [],
                        "tool_round_budget": 2,
                        "tool_rounds_used": 1,
                    },
                    {
                        "stage_id": "stage-6",
                        "stage_index": 6,
                        "stage_kind": "normal",
                        "system_generated": False,
                        "mode": _EXECUTION_STAGE_MODE_SELF,
                        "status": _EXECUTION_STAGE_STATUS_COMPLETED,
                        "stage_goal": "inspect stage six",
                        "completed_stage_summary": "finished stage six",
                        "key_refs": [],
                        "tool_round_budget": 2,
                        "tool_rounds_used": 1,
                    },
                    {
                        "stage_id": "stage-7",
                        "stage_index": 7,
                        "stage_kind": "normal",
                        "system_generated": False,
                        "mode": _EXECUTION_STAGE_MODE_SELF,
                        "status": _EXECUTION_STAGE_STATUS_ACTIVE,
                        "stage_goal": "inspect stage seven",
                        "tool_round_budget": 2,
                        "tool_rounds_used": 0,
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
                    "id": "call-stage-4",
                    "type": "function",
                    "function": {"name": "submit_next_stage", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "name": "submit_next_stage", "tool_call_id": "call-stage-4", "content": '{"ok": true}'},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call-stage-5",
                    "type": "function",
                    "function": {"name": "submit_next_stage", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "name": "submit_next_stage", "tool_call_id": "call-stage-5", "content": '{"ok": true}'},
        # Reproduce the broken history shape seen in task:9001280b6cbe:
        # the retained stage-6 window only keeps the tool half.
        {"role": "tool", "name": "submit_next_stage", "tool_call_id": "call-stage-6", "content": '{"ok": true}'},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call-stage-7",
                    "type": "function",
                    "function": {"name": "submit_next_stage", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "name": "submit_next_stage", "tool_call_id": "call-stage-7", "content": '{"ok": true}'},
    ]

    prepared = loop._prepare_messages(original, runtime_context={"task_id": "task-1", "node_id": "node-1"})
    history = analyze_tool_call_history(prepared)

    assert history.orphan_tool_result_ids == []
    assert any(
        str(item.get("role") or "") == "assistant"
        and any(str((tool_call or {}).get("id") or "").startswith("call-stage-6") for tool_call in list(item.get("tool_calls") or []))
        for item in prepared
    )


@pytest.mark.asyncio
async def test_execute_tool_calls_marks_stage_history_archive_reads_ephemeral() -> None:
    class _ArchiveTool(Tool):
        @property
        def name(self) -> str:
            return "content"

        @property
        def description(self) -> str:
            return "open archived stage history"

        @property
        def parameters(self) -> dict[str, object]:
            return {"type": "object", "properties": {}, "required": []}

        async def execute(self, **kwargs):
            _ = kwargs
            return {
                "ok": True,
                "ref": "artifact:artifact:archive",
                "handle": {
                    "ref": "artifact:artifact:archive",
                    "source_kind": "stage_history_archive",
                },
                "excerpt": "archived stage body",
            }

    loop = ReActToolLoop(chat_backend=SimpleNamespace(), log_service=_FakeLogService(), max_iterations=2)
    results = await loop._execute_tool_calls(
        task=SimpleNamespace(task_id="task-1"),
        node=SimpleNamespace(node_id="node-1", depth=0, node_kind="execution"),
        response_tool_calls=[ToolCallRequest(id="call-archive", name="content", arguments={})],
        tools={"content": _ArchiveTool()},
        allowed_content_refs=[],
        runtime_context={"task_id": "task-1", "node_id": "node-1", "actor_role": "execution"},
    )

    assert len(results) == 1
    assert results[0]["tool_message"]["ephemeral"] is True
    assert results[0]["live_state"]["ephemeral"] is True


@pytest.mark.asyncio
async def test_execute_tool_calls_allows_submit_next_stage_and_passes_runtime_contract_candidates() -> None:
    loop = ReActToolLoop(chat_backend=SimpleNamespace(), log_service=_FakeLogService(), max_iterations=2)
    tool = _StageRuntimeProbeTool()

    results = await loop._execute_tool_calls(
        task=SimpleNamespace(task_id="task-stage-probe"),
        node=SimpleNamespace(node_id="node-stage-probe", depth=0, node_kind="execution"),
        response_tool_calls=[
            ToolCallRequest(
                id="call-stage-probe",
                name="submit_next_stage",
                arguments={"stage_goal": "probe stage", "tool_round_budget": 1},
            )
        ],
        tools={"submit_next_stage": tool},
        allowed_content_refs=[],
        runtime_context={
            "task_id": "task-stage-probe",
            "node_id": "node-stage-probe",
            "actor_role": "execution",
            "stage_turn_granted": True,
            "candidate_tool_names": ["filesystem_write"],
            "candidate_skill_ids": ["skill-creator"],
        },
    )

    assert len(results) == 1
    assert results[0]["live_state"]["status"] == "success"
    assert '"stage_goal": "probe stage"' in str(results[0]["tool_message"]["content"])
    assert tool.runtime_payloads == [
        {
            "task_id": "task-stage-probe",
            "node_id": "node-stage-probe",
            "actor_role": "execution",
            "stage_turn_granted": False,
            "candidate_tool_names": ["filesystem_write"],
            "candidate_skill_ids": ["skill-creator"],
            "current_tool_call_id": "call-stage-probe",
            "tool_contract_enforced": True,
            "allowed_content_refs": [],
            "enforce_content_ref_allowlist": False,
            "prior_overflow_signatures": [],
            "image_multimodal_enabled": False,
        }
    ]


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
        node=SimpleNamespace(node_id='node-1', depth=0, node_kind='execution'),
        messages=initial_messages,
        tools={},
        model_refs=['fake'],
        runtime_context={'task_id': 'task-1', 'node_id': 'node-1'},
        max_iterations=5,
    )

    assert result.status == "failed"
    assert result.delivery_status == "blocked"
    assert "final result submission guard triggered" in result.summary
    assert "submit_final_result" in result.blocking_reason
    assert "Detected orphan tool results" not in result.blocking_reason
    assert "call-orphan|fc_orphan" not in result.blocking_reason
    assert len(calls) == 1


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
            "hydrated_executor_names": [],
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
        stage_payload={},
        hydrated_executor_names=[],
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
            "hydrated_executor_names": [],
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
        callable_tool_names=["submit_next_stage"],
        candidate_tool_names=["filesystem_write"],
        visible_skills=[],
        candidate_skill_ids=[],
        candidate_skill_items=[],
        candidate_tool_items=[{"tool_id": "filesystem_write", "description": "write file"}],
        stage_payload={},
        hydrated_executor_names=[],
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
            "callable_tool_names": ["submit_next_stage"],
            "candidate_tool_names": [],
            "candidate_tool_items": [{"tool_id": "filesystem_write", "description": "write file"}],
            "selected_skill_ids": [],
            "candidate_skill_ids": [],
            "hydrated_executor_state": ["web_fetch"],
            "hydrated_executor_names": ["web_fetch"],
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
            "tool_names": ["submit_next_stage"],
            "candidate_tool_names": [],
            "hydrated_executor_names": ["web_fetch"],
            "lightweight_tool_ids": [],
            "trace": {},
        },
        stage_gate={"has_active_stage": True, "transition_required": False, "active_stage": {"stage_id": "stage-1"}},
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
        callable_tool_names=["submit_next_stage"],
        candidate_tool_names=[],
        visible_skills=[],
        candidate_skill_ids=["tmux"],
        candidate_skill_items=[{"skill_id": "tmux", "description": "terminal workflow"}],
        stage_payload={},
        hydrated_executor_names=[],
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
            "callable_tool_names": ["submit_next_stage"],
            "candidate_tool_names": [],
            "candidate_tool_items": [],
            "selected_skill_ids": [],
            "candidate_skill_ids": [],
            "candidate_skill_items": [{"skill_id": "tmux", "description": "terminal workflow"}],
            "hydrated_executor_state": [],
            "hydrated_executor_names": [],
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
            "tool_names": ["submit_next_stage"],
            "candidate_tool_names": [],
            "hydrated_executor_names": [],
            "lightweight_tool_ids": [],
            "trace": {},
        },
        stage_gate={"has_active_stage": True, "transition_required": False, "active_stage": {"stage_id": "stage-1"}},
    )

    assert contract.candidate_skill_ids == []
    assert contract.to_message_payload()["candidate_skills"] == []


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
        stage_payload={},
        hydrated_executor_names=[],
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
            "hydrated_executor_names": ["web_fetch"],
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
        stage_payload={},
        hydrated_executor_names=[],
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
            "hydrated_executor_names": [],
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
        assert payload["uri"] == "g3ku://resource/tool/content"
        assert payload["content"].startswith("tool line 001")
    finally:
        store.close()


@pytest.mark.asyncio
async def test_load_tool_context_tool_rejects_non_candidate_runtime_target() -> None:
    service = _RuntimeToolService()
    tool = LoadToolContextTool(lambda: service)

    rendered = await tool.execute(
        tool_id="filesystem_write",
        _LoadToolContextTool__g3ku_runtime={
            "session_key": "web:shared",
            "actor_role": "execution",
            "tool_contract_enforced": True,
            "candidate_tool_names": ["agent_browser"],
        },
    )

    assert rendered.startswith("Error:")
    assert service.load_tool_calls == []


@pytest.mark.asyncio
async def test_load_skill_context_tool_rejects_non_candidate_runtime_target() -> None:
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

    assert rendered.startswith("Error:")
    assert service.load_skill_calls == []


def test_refresh_node_dynamic_contract_restores_skill_candidates_from_frame_after_stage_compaction() -> None:
    log_service = _FakeLogService()
    loop = ReActToolLoop(chat_backend=SimpleNamespace(), log_service=log_service, max_iterations=2)
    task_id = "task-stage-skill-restore"
    node = SimpleNamespace(task_id=task_id, node_id="node-stage-skill-restore", depth=0, node_kind="execution")
    log_service._store._node = SimpleNamespace(
        metadata={
            "execution_stages": {
                "active_stage_id": "stage-1",
                "stages": [
                    {
                        "stage_id": "stage-1",
                        "stage_index": 1,
                        "stage_kind": "normal",
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
        callable_tool_names=["submit_next_stage", "load_skill_context"],
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
        stage_payload={},
        hydrated_executor_names=[],
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
                    "function": {"name": "submit_next_stage", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "name": "submit_next_stage", "tool_call_id": "call-stage-1", "content": '{"ok": true}'},
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
            "tool_names": ["submit_next_stage", "load_skill_context"],
            "candidate_tool_names": ["content"],
            "hydrated_executor_names": [],
            "lightweight_tool_ids": [],
            "trace": {},
        },
        stage_gate={"has_active_stage": True, "transition_required": False, "active_stage": {"stage_id": "stage-1"}},
    )

    payload = extract_node_dynamic_contract_payload(refreshed)
    assert payload is not None
    assert payload["candidate_skills"] == [
        {
            "skill_id": "tmux",
            "description": "terminal workflow",
        }
    ]


def test_node_dynamic_contract_injection_keeps_request_only_message_after_bootstrap_prefix() -> None:
    contract = NodeRuntimeToolContract(
        node_id="node-1",
        node_kind="execution",
        callable_tool_names=["submit_next_stage"],
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
        stage_payload={
            "has_active_stage": True,
            "transition_required": False,
            "active_stage": {
                "stage_id": "stage-1",
                "stage_goal": "inspect repo",
                "tool_round_budget": 6,
                "tool_rounds_used": 3,
                "rounds": [{"round_id": "round-1"}],
            },
        },
        hydrated_executor_names=["filesystem_write"],
        lightweight_tool_ids=["filesystem"],
        selection_trace={
            "mode": "execution_tool_selection",
            "full_callable_tool_names": ["submit_next_stage", "filesystem_write"],
            "stage_locked_to_submit_next_stage": True,
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

    assert [item["role"] for item in injected[:4]] == ["system", "user", "assistant", "assistant"]
    payload = extract_node_dynamic_contract_payload(injected)
    assert payload is not None
    assert payload["execution_stage"] == {
        "has_active_stage": True,
        "transition_required": False,
        "active_stage": {
            "stage_id": "stage-1",
            "stage_goal": "inspect repo",
            "tool_round_budget": 6,
            "stage_kind": "normal",
            "final_stage": False,
        },
    }
    assert payload["candidate_skills"] == [
        {
            "skill_id": "tmux",
            "description": "terminal workflow",
        }
    ]
    assert payload["hydrated_executor_names"] == ["filesystem_write"]
    assert "model_visible_tool_selection_trace" not in payload


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
        stage_payload={},
        hydrated_executor_names=[],
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
            {"role": "assistant", "content": "older history detail"},
            {"role": "assistant", "content": "recent tail detail"},
            contract_message,
        ],
        recent_tail_count=1,
    )

    contents = [str(item.get("content") or "") for item in rewritten]
    notice_index = next(index for index, content in enumerate(contents) if content.startswith(APPEND_NOTICE_TAIL_PREFIX))
    compact_index = next(index for index, content in enumerate(contents) if content.startswith("[G3KU_TOKEN_COMPACT_V2]"))

    assert rewritten[0]["role"] == "system"
    assert rewritten[1]["role"] == "user"
    assert notice_index < compact_index
    assert compact_index < len(rewritten) - 1
    assert extract_node_dynamic_contract_payload([rewritten[-1]]) is not None
    assert payload["node_id"] == "node-token-compact"
    assert payload["append_notice_tail_count"] == 1
    assert payload["contract_message_count"] == 1


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
    loop._tool_execution_manager = ToolExecutionManager()

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

        wait_payload = await loop._tool_execution_manager.wait_execution(
            "tool-exec:1",
            wait_seconds=0.05,
            poll_interval_seconds=0.01,
        )

        assert rendered == "done"
        assert wait_payload["status"] == "not_found"
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
    loop._tool_execution_manager = ToolExecutionManager()

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
async def test_enrich_node_messages_visible_only_fallback_injects_all_visible_skills_without_memory_overlay(
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
            mode="visible_only",
            selected_skill_ids=["skill-creator", "tmux"],
            selected_tool_names=["filesystem"],
            trace={"mode": "visible_only"},
        )

    monkeypatch.setattr(
        runtime_service_module,
        "build_node_context_selection",
        _fake_build_node_context_selection,
    )

    service = object.__new__(MainRuntimeService)
    service.memory_manager = _MemoryManager()
    service.list_visible_tool_families = lambda *, actor_role, session_id: [
        SimpleNamespace(tool_id='filesystem'),
    ]
    service.list_visible_skill_resources = lambda *, actor_role, session_id: [
        SimpleNamespace(skill_id='skill-creator', display_name='skill-creator', description='skill creator'),
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

    assert selector_calls == [
        {
            "loop": None,
            "memory_manager": service.memory_manager,
            "prompt": "where is the plan",
            "goal": "where is the plan",
            "core_requirement": "where is the plan",
            "visible_skills": service.list_visible_skill_resources(actor_role="execution", session_id="web:ceo-origin"),
            "visible_tool_families": service.list_visible_tool_families(actor_role="execution", session_id="web:ceo-origin"),
            "visible_tool_names": ["filesystem"],
        }
    ]
    assert retrieve_block_calls == []
    dynamic_payload = extract_node_dynamic_contract_payload(enriched)
    assert dynamic_payload is not None
    assert dynamic_payload["message_type"] == "node_runtime_tool_contract"
    assert dynamic_payload["candidate_skills"] == [
        {
            "skill_id": "skill-creator",
            "description": "skill creator",
        },
        {
            "skill_id": "tmux",
            "description": "terminal workflow",
        },
    ]
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
            mode="dense_rerank",
            selected_skill_ids=["tmux", "skill-creator"],
            selected_tool_names=["content"],
            candidate_skill_ids=["tmux", "skill-creator"],
            candidate_tool_names=["content"],
            trace={"mode": "dense_rerank"},
        )

    monkeypatch.setattr(
        runtime_service_module,
        "build_node_context_selection",
        _fake_build_node_context_selection,
    )

    service = object.__new__(MainRuntimeService)
    service.memory_manager = _MemoryManager()
    service.list_visible_tool_families = lambda *, actor_role, session_id: [
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

    assert retrieve_block_calls == []
    dynamic_payload = extract_node_dynamic_contract_payload(enriched)
    assert dynamic_payload is not None
    assert dynamic_payload["message_type"] == "node_runtime_tool_contract"
    assert dynamic_payload["candidate_skills"] == [
        {
            "skill_id": "tmux",
            "description": "terminal workflow",
        },
        {
            "skill_id": "skill-creator",
            "description": "skill creator",
        }
    ]
    assert dynamic_payload["candidate_tools"] == [
        {
            "tool_id": "content",
            "description": "",
        }
    ]
    assert enriched[0]["content"] == "base prompt"


@pytest.mark.asyncio
async def test_enrich_node_messages_reports_hydrated_callable_tools_separately_from_candidates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _fake_build_node_context_selection(**kwargs):
        _ = kwargs
        return NodeContextSelectionResult(
            mode="dense_rerank",
            selected_skill_ids=["tmux"],
            selected_tool_names=["content", "exec"],
            candidate_skill_ids=["tmux"],
            candidate_tool_names=["content", "exec"],
            trace={"mode": "dense_rerank"},
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
    service.log_service.upsert_frame(
        'task-hydrated-payload',
        {
            'node_id': 'node-hydrated-payload',
            'hydrated_executor_names': ['filesystem_write'],
        },
    )
    service.list_visible_tool_families = lambda *, actor_role, session_id: [
        SimpleNamespace(tool_id='content'),
        SimpleNamespace(tool_id='filesystem'),
    ]
    service.list_visible_skill_resources = lambda *, actor_role, session_id: [
        SimpleNamespace(skill_id='tmux', display_name='tmux', description='terminal workflow'),
    ]
    service.list_effective_tool_names = lambda *, actor_role, session_id: ['content', 'filesystem_write', 'load_tool_context']
    task = SimpleNamespace(task_id='task-hydrated-payload', session_id="web:ceo-origin", metadata={})
    node = SimpleNamespace(
        task_id='task-hydrated-payload',
        node_id='node-hydrated-payload',
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

    dynamic_payload = extract_node_dynamic_contract_payload(enriched)
    assert dynamic_payload is not None
    assert dynamic_payload["message_type"] == "node_runtime_tool_contract"
    assert dynamic_payload["callable_tool_names"] == ["load_tool_context", "filesystem_write"]
    assert dynamic_payload["candidate_tools"] == [
        {
            "tool_id": "content",
            "description": "",
        }
    ]


@pytest.mark.asyncio
async def test_enrich_node_messages_includes_exec_runtime_policy_in_dynamic_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _fake_build_node_context_selection(**kwargs):
        _ = kwargs
        return NodeContextSelectionResult(
            mode="dense_rerank",
            selected_skill_ids=["tmux"],
            selected_tool_names=["content"],
            candidate_skill_ids=["tmux"],
            candidate_tool_names=["content"],
            trace={"mode": "dense_rerank"},
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
    service.list_visible_tool_families = lambda *, actor_role, session_id: [
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
    assert dynamic_payload is not None
    assert dynamic_payload["exec_runtime_policy"] == {
        'mode': 'full_access',
        'guardrails_enabled': False,
        'summary': 'exec will execute shell commands without exec-side guardrails.',
    }


@pytest.mark.asyncio
async def test_enrich_node_messages_locks_callable_tools_to_submit_next_stage_without_valid_stage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _fake_build_node_context_selection(**kwargs):
        _ = kwargs
        return NodeContextSelectionResult(
            mode="dense_rerank",
            selected_skill_ids=["tmux"],
            selected_tool_names=["content"],
            candidate_skill_ids=["tmux"],
            candidate_tool_names=["content"],
            trace={"mode": "dense_rerank"},
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
        'has_active_stage': False,
        'transition_required': False,
        'active_stage': None,
    }
    service.log_service.upsert_frame(
        'task-stage-locked-payload',
        {
            'node_id': 'node-stage-locked-payload',
            'hydrated_executor_names': ['filesystem_write'],
        },
    )
    service.list_visible_tool_families = lambda *, actor_role, session_id: [
        SimpleNamespace(tool_id='content'),
        SimpleNamespace(tool_id='filesystem'),
    ]
    service.list_visible_skill_resources = lambda *, actor_role, session_id: [
        SimpleNamespace(skill_id='tmux', display_name='tmux', description='terminal workflow'),
    ]
    service.list_effective_tool_names = lambda *, actor_role, session_id: ['content', 'filesystem_write', 'load_tool_context']
    task = SimpleNamespace(task_id='task-stage-locked-payload', session_id="web:ceo-origin", metadata={})
    node = SimpleNamespace(
        task_id='task-stage-locked-payload',
        node_id='node-stage-locked-payload',
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

    dynamic_payload = extract_node_dynamic_contract_payload(enriched)
    assert dynamic_payload is not None
    assert dynamic_payload["message_type"] == "node_runtime_tool_contract"
    assert dynamic_payload["callable_tool_names"] == ["submit_next_stage"]
    assert dynamic_payload["candidate_tools"] == [
        {
            "tool_id": "content",
            "description": "",
        }
    ]
    assert "model_visible_tool_selection_trace" not in dynamic_payload


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
            mode="dense_rerank",
            selected_skill_ids=["tmux"],
            selected_tool_names=["filesystem"],
            candidate_skill_ids=["tmux"],
            candidate_tool_names=["filesystem"],
            trace={"mode": "dense_rerank"},
        )

    monkeypatch.setattr(
        runtime_service_module,
        "build_node_context_selection",
        _fake_build_node_context_selection,
    )

    service = object.__new__(MainRuntimeService)
    service.memory_manager = _MemoryManager()
    service.list_visible_tool_families = lambda *, actor_role, session_id: [
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

    user_messages = [message for message in enriched if message.get("role") == "user"]
    assert len(user_messages) == 1
    payload = extract_node_dynamic_contract_payload(enriched)
    assert payload is not None
    assert payload["message_type"] == "node_runtime_tool_contract"
    assert payload["candidate_skills"] == [
        {
            "skill_id": "tmux",
            "description": "terminal workflow",
        }
    ]
    assert payload["candidate_tools"] == [
        {
            "tool_id": "filesystem",
            "description": "",
        }
    ]
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
            mode="dense_rerank",
            selected_skill_ids=["tmux"],
            selected_tool_names=["content"],
            candidate_skill_ids=["tmux"],
            candidate_tool_names=["content"],
            trace={"mode": "dense_rerank"},
        )

    monkeypatch.setattr(
        runtime_service_module,
        "build_node_context_selection",
        _fake_build_node_context_selection,
    )

    service = object.__new__(MainRuntimeService)
    service.memory_manager = _MemoryManager()
    service.list_visible_tool_families = lambda *, actor_role, session_id: [
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

    dynamic_payload = extract_node_dynamic_contract_payload(enriched)
    assert dynamic_payload is not None
    assert dynamic_payload["message_type"] == "node_runtime_tool_contract"
    assert dynamic_payload["candidate_skills"] == [
        {
            "skill_id": "tmux",
            "description": "terminal workflow",
        }
    ]
    assert dynamic_payload["candidate_tools"] == [
        {
            "tool_id": "content",
            "description": "",
        }
    ]
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
    assert 'final|blocked' in message
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


def test_acceptance_result_contract_violation_message_uses_final_or_blocked_only() -> None:
    message = ReActToolLoop._result_contract_violation_message(
        ['summary must not be empty'],
        node_kind='acceptance',
    )

    assert 'delivery_status="partial"' not in message
    assert 'submit_final_result' in message
    assert 'failed+final' in message
    assert 'failed+blocked' in message


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

    assert result.status == 'failed'
    assert result.delivery_status == 'final'
    assert len(calls) == 1
    assert result.summary == 'done'
    assert result.blocking_reason == ''


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
async def test_react_loop_auto_wraps_plain_text_final_result() -> None:
    class _Backend:
        async def chat(self, **kwargs):
            _ = kwargs
            return LLMResponse(
                content='Final structured summary line 1\n- detail a\n- detail b',
                tool_calls=[],
                finish_reason='stop',
                usage={'input_tokens': 8, 'output_tokens': 3},
            )

    loop = ReActToolLoop(chat_backend=_Backend(), log_service=_FakeLogService(), max_iterations=2)
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
        max_iterations=2,
    )

    assert result.status == 'success'
    assert result.delivery_status == 'final'
    assert result.answer == 'Final structured summary line 1\n- detail a\n- detail b'
    assert result.summary == 'auto-wrapped plain-text final result'
    assert result.evidence == []


@pytest.mark.asyncio
async def test_react_loop_auto_wraps_plain_text_final_result_with_tool_evidence() -> None:
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
            return LLMResponse(
                content='Final answer after tool usage',
                tool_calls=[],
                finish_reason='stop',
                usage={'input_tokens': 8, 'output_tokens': 3},
            )

    loop = ReActToolLoop(chat_backend=_Backend(), log_service=_FakeLogService(), max_iterations=3)
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
        max_iterations=3,
    )

    assert executed == [2]
    assert result.status == 'success'
    assert result.answer == 'Final answer after tool usage'
    assert result.evidence
    assert any('count_tool' in str(item.note or '') for item in result.evidence)


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
        node=SimpleNamespace(node_id='node-raw-final-json', depth=0, node_kind='execution'),
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

    assert result.status == 'failed'
    assert result.delivery_status == 'final'
    assert result.summary == 'still invalid'
    assert result.blocking_reason == ''
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
    assert "[G3KU_TOKEN_COMPACT_V2]" not in rendered

    assert isinstance(observed_frame.get("token_preflight_diagnostics"), dict)
    assert observed_frame.get("history_shrink_reason") in {"", None}


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
        return 18000 if "[G3KU_TOKEN_COMPACT_V2]" in rendered else 24320

    monkeypatch.setattr(
        react_loop_module.runtime_send_token_preflight,
        "estimate_runtime_provider_request_preview_tokens",
        _estimate,
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
        ],
        tools={"submit_final_result": _submit_final_result_tool()},
        model_refs=["fake"],
        runtime_context={"task_id": "task-preflight-compress", "node_id": "node-preflight-compress"},
        max_iterations=2,
    )

    assert result.status == "success"
    assert len(calls) == 1
    rendered = "\n".join(str(item.get("content") or "") for item in calls[0])
    assert "[G3KU_TOKEN_COMPACT_V2]" in rendered

    assert observed_frame.get("history_shrink_reason") == "token_compression"
    diagnostics = observed_frame.get("token_preflight_diagnostics")
    assert isinstance(diagnostics, dict)
    assert diagnostics.get("applied") is True


@pytest.mark.asyncio
async def test_node_send_preflight_token_compression_commits_pending_provider_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
        return 18000 if "[G3KU_TOKEN_COMPACT_V2]" in rendered else 24320

    monkeypatch.setattr(
        react_loop_module.runtime_send_token_preflight,
        "estimate_runtime_provider_request_preview_tokens",
        _estimate,
        raising=False,
    )

    class _Backend:
        async def chat(self, **kwargs):
            observed_frame.update(
                log_service.read_runtime_frame(
                    "task-preflight-provider-sync",
                    "node-preflight-provider-sync",
                )
            )
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
        commit_reason = str(dict(kwargs.get("runtime_context") or {}).get("provider_tool_exposure_commit_reason") or "")
        provider_tool_names = ["submit_final_result", "web_fetch"] if commit_reason == "token_compression" else ["submit_final_result"]
        pending_names = [] if commit_reason == "token_compression" else ["submit_final_result", "web_fetch"]
        return {
            "tool_names": ["submit_final_result", "web_fetch"],
            "provider_tool_names": provider_tool_names,
            "pending_provider_tool_names": pending_names,
            "provider_tool_exposure_pending": bool(pending_names),
            "provider_tool_exposure_commit_reason": "token_compression" if commit_reason == "token_compression" else "",
            "provider_tool_exposure_revision": "pte:committed" if commit_reason == "token_compression" else "pte:active",
            "trace": {
                "full_callable_tool_names": ["submit_final_result", "web_fetch"],
                "provider_tool_names": provider_tool_names,
                "pending_provider_tool_names": pending_names,
                "provider_tool_exposure_commit_reason": "token_compression" if commit_reason == "token_compression" else "",
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
        ],
        tools={
            "submit_final_result": _submit_final_result_tool(),
            "web_fetch": _SchemaTool(name="web_fetch"),
        },
        model_refs=["fake"],
        runtime_context={"task_id": "task-preflight-provider-sync", "node_id": "node-preflight-provider-sync"},
        max_iterations=2,
    )

    assert result.status == "success"
    assert len(calls) == 1
    emitted_tools = list(calls[0].get("tools") or [])
    emitted_tool_names = [item["function"]["name"] for item in emitted_tools]
    assert "web_fetch" in emitted_tool_names
    assert observed_frame.get("provider_tool_names") == ["submit_final_result", "web_fetch"]
    assert observed_frame.get("provider_tool_exposure_commit_reason") == "token_compression"


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
        return 33000 if "[G3KU_TOKEN_COMPACT_V2]" in rendered else 24320

    monkeypatch.setattr(
        react_loop_module.runtime_send_token_preflight,
        "estimate_runtime_provider_request_preview_tokens",
        _estimate,
        raising=False,
    )

    class _Backend:
        async def chat(self, **kwargs):
            calls.append([dict(item) for item in list(kwargs.get("messages") or [])])
            raise RuntimeError("chat should not be called when preflight fails")

    log_service = _FakeLogService()
    loop = ReActToolLoop(chat_backend=_Backend(), log_service=log_service, max_iterations=2)
    result = await loop.run(
        task=SimpleNamespace(task_id="task-preflight-overflow"),
        node=SimpleNamespace(node_id="node-preflight-overflow", depth=0, node_kind="execution"),
        messages=[
            {"role": "system", "content": "system"},
            {"role": "user", "content": '{"task_id":"task-preflight-overflow","goal":"demo"}'},
        ],
        tools={"submit_final_result": _submit_final_result_tool()},
        model_refs=["fake"],
        runtime_context={"task_id": "task-preflight-overflow", "node_id": "node-preflight-overflow"},
        max_iterations=2,
    )

    assert result.status == "failed"
    assert calls == []


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
        return 11000 if "[G3KU_TOKEN_COMPACT_V2]" in rendered else 12840

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
        ],
        tools={"submit_final_result": _submit_final_result_tool()},
        model_refs=["fake"],
        runtime_context={"task_id": "task-preflight-usage-plus-delta", "node_id": "node-preflight-usage-plus-delta"},
        max_iterations=2,
    )

    assert result.status == "success"
    assert len(calls) == 1
    rendered = "\n".join(str(item.get("content") or "") for item in calls[0])
    assert "[G3KU_TOKEN_COMPACT_V2]" in rendered

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
        return 25110 if "[G3KU_TOKEN_COMPACT_V2]" in rendered else 26000

    monkeypatch.setattr(
        react_loop_module.runtime_send_token_preflight,
        "estimate_runtime_provider_request_preview_tokens",
        _estimate,
        raising=False,
    )

    class _Backend:
        async def chat(self, **kwargs):
            calls.append([dict(item) for item in list(kwargs.get("messages") or [])])
            raise RuntimeError("chat should not be called when preflight fails")

    log_service = _FakeLogService()
    loop = ReActToolLoop(chat_backend=_Backend(), log_service=log_service, max_iterations=2)
    result = await loop.run(
        task=SimpleNamespace(task_id="task-preflight-compress-before-fail"),
        node=SimpleNamespace(node_id="node-preflight-compress-before-fail", depth=0, node_kind="execution"),
        messages=[
            {"role": "system", "content": "system"},
            {"role": "user", "content": '{"task_id":"task-preflight-compress-before-fail","goal":"demo"}'},
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
    assert "[G3KU_TOKEN_COMPACT_V2]" not in rendered

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
