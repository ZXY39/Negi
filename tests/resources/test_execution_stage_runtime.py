from __future__ import annotations

import asyncio
import importlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from g3ku.agent.tools.base import Tool
from g3ku.providers.base import LLMResponse, ToolCallRequest
from g3ku.runtime.context.node_context_selection import NodeContextSelectionResult
from main.protocol import now_iso
from main.runtime.acceptance_handshake import ACCEPTANCE_HANDSHAKE_KEY
from main.runtime.chat_backend import build_stable_prompt_cache_key
from main.runtime.internal_tools import SpawnChildNodesTool, SubmitFinalResultTool
from main.runtime.node_prompt_contract import (
    NodeRuntimeToolContract,
    extract_node_dynamic_contract_payload,
)
from main.runtime.node_runner import NodeRunner
from main.service.runtime_service import MainRuntimeService

runtime_service_module = importlib.import_module("main.service.runtime_service")

_PARAMETER_GUIDANCE_TEMPLATE = (
    '请先调用 load_tool_context(tool_id="{tool_name}") 查看该工具的详细说明、参数契约和示例后，再重新使用该工具。'
)
# 无资源 descriptor 的工具不再引导 load_tool_context（加载必被合同闸门拒绝）。
# 参数错误改为把该校验面实际使用的契约回贴给模型；渲染不出必填项时才退回泛化核对提醒。
_PARAMETER_RECHECK_GUIDANCE = (
    '该工具没有可加载的扩展说明。请仔细核对该工具的入参（参数名、必填项、类型与取值结构）后重新提交。'
)
_PARAMETER_CONTRACT_GUIDANCE_PREFIX = '该工具没有可加载的扩展说明，参数契约如下（必填项及其类型与取值结构）：'


@pytest.fixture(autouse=True)
def _default_node_send_preflight_context_window(monkeypatch: pytest.MonkeyPatch) -> None:
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


class _DummyChatBackend:
    async def chat(self, **kwargs):
        raise AssertionError(f"chat backend should not be called in this test: {kwargs!r}")


class _StaticTool(Tool):
    def __init__(self, name: str, result: str = 'ok') -> None:
        self._name = name
        self._result = result

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return f'{self._name} tool'

    @property
    def parameters(self) -> dict:
        return {'type': 'object', 'properties': {}}

    async def execute(self, **kwargs):
        _ = kwargs
        return self._result


class _ErrorTool(Tool):
    def __init__(self, *, name: str, exc: Exception) -> None:
        self._name = name
        self._exc = exc

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return f'{self._name} tool'

    @property
    def parameters(self) -> dict:
        return {'type': 'object', 'properties': {'value': {'type': 'string'}}, 'required': ['value']}

    async def execute(self, **kwargs):
        _ = kwargs
        raise self._exc


class _BrokenValidationTool(Tool):
    @property
    def name(self) -> str:
        return 'broken_validation_tool'

    @property
    def description(self) -> str:
        return 'broken validation tool'

    @property
    def parameters(self) -> dict:
        return {'type': 'object', 'properties': {'value': {'type': 'string'}}, 'required': ['value']}

    async def execute(self, **kwargs):
        raise AssertionError('execute should not be reached when validation crashes')

    def validate_params(self, params: dict[str, object]) -> list[str]:
        _ = params
        raise TypeError("unhashable type: 'list'")


def _react_loop_for_content_open_image_overlay() -> object:
    log_service = SimpleNamespace(
        update_frame=lambda *args, **kwargs: None,
        update_node_input=lambda *args, **kwargs: None,
    )
    return runtime_service_module.ReActToolLoop(
        chat_backend=_DummyChatBackend(),
        log_service=log_service,
    )


class _ToolResourceManager:
    def __init__(self, tools: dict[str, Tool]) -> None:
        self._tools = dict(tools)

    def tool_instances(self) -> dict[str, Tool]:
        return dict(self._tools)

    def get_tool_descriptor(self, name: str):
        normalized = str(name or "").strip()
        if normalized in self._tools:
            return SimpleNamespace(name=normalized)
        return None


def _tool_record(
    tool_id: str,
    *,
    display_name: str = "",
    description: str = "",
    available: bool = True,
    enabled: bool = True,
    metadata: dict | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        tool_id=tool_id,
        display_name=display_name or tool_id,
        description=description,
        risk_level="medium",
        destructive=False,
        agent_visible=True,
        admin_mode="editable",
        allowed_roles=["execution", "inspection"],
        is_core=False,
        enabled=enabled,
        available=available,
        tool_type="internal",
        install_dir=None,
        callable=True,
        source_path=f"/tmp/tools/{tool_id}",
        metadata=dict(metadata or {}),
    )


def _build_selector_test_service(
    *,
    task: SimpleNamespace,
    visible_skills: list[SimpleNamespace],
    visible_tool_records: list[SimpleNamespace],
    visible_tool_names: list[str],
    tool_instances: dict[str, Tool],
) -> MainRuntimeService:
    service = object.__new__(MainRuntimeService)
    service.store = SimpleNamespace(get_task=lambda _task_id: task)
    service.memory_manager = None
    service._react_loop = None
    service._resource_manager = _ToolResourceManager(tool_instances)
    service._builtin_tool_cache = None
    service._node_context_selection_cache = {}
    service.list_visible_skill_resources = lambda *, actor_role, session_id: list(visible_skills)
    service.list_visible_tools = lambda *, actor_role, session_id: list(visible_tool_records)
    service.list_effective_tool_names = lambda *, actor_role, session_id: list(visible_tool_names)
    return service


def _final_result_call(
    *,
    call_id: str = "call:final",
    status: str,
    delivery_status: str,
    summary: str,
    answer: str,
    evidence: list[dict[str, object]] | None = None,
    remaining_work: list[str] | None = None,
    blocking_reason: str = "",
) -> ToolCallRequest:
    return ToolCallRequest(
        id=call_id,
        name="submit_final_result",
        arguments={
            "status": status,
            "delivery_status": delivery_status,
            "summary": summary,
            "answer": answer,
            "evidence": list(evidence or []),
            "remaining_work": list(remaining_work or []),
            "blocking_reason": blocking_reason,
        },
    )


def _submit_final_result_tool(*, node_kind: str = "execution") -> SubmitFinalResultTool:
    async def _submit(payload: dict[str, object]) -> dict[str, object]:
        return dict(payload)

    return SubmitFinalResultTool(_submit, node_kind=node_kind)


def _mark_worker_online(service: MainRuntimeService) -> None:
    service.store.upsert_worker_status(
        worker_id='worker:test',
        role='task_worker',
        status='running',
        updated_at=now_iso(),
        payload={'execution_mode': 'worker', 'active_task_count': 0},
    )


async def _create_web_task(service: MainRuntimeService):
    _mark_worker_online(service)
    return await service.create_task('stage test task', session_id='web:shared')


def _task_temp_dir_for(service: MainRuntimeService, task_id: str) -> str:
    runtime_meta = service.log_service.read_task_runtime_meta(task_id) or {}
    return str(runtime_meta.get('task_temp_dir') or '')


def _tool_result_payload(
    *,
    call_id: str,
    tool_name: str,
    content: str,
    status: str = 'success',
) -> dict[str, dict[str, object]]:
    started_at = now_iso()
    finished_at = now_iso()
    return {
        'live_state': {
            'tool_call_id': call_id,
            'tool_name': tool_name,
            'status': status,
            'started_at': started_at,
            'finished_at': finished_at,
            'elapsed_seconds': 0.1,
        },
        'tool_message': {
            'role': 'tool',
            'tool_call_id': call_id,
            'name': tool_name,
            'content': content,
            'started_at': started_at,
            'finished_at': finished_at,
            'elapsed_seconds': 0.1,
            'status': status,
        },
    }










@pytest.mark.asyncio
async def test_selector_precompute_is_shared_by_tool_exposure_and_message_enrichment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selector_calls: list[dict[str, object]] = []
    task = SimpleNamespace(
        task_id="task-1",
        session_id="web:shared",
        metadata={"core_requirement": "inspect content navigation"},
    )
    node = SimpleNamespace(
        task_id="task-1",
        node_id="node-1",
        node_kind="execution",
        prompt="inspect content navigation",
        goal="inspect content navigation",
    )
    visible_skills = [
        SimpleNamespace(skill_id="tmux", display_name="tmux", description="terminal workflow"),
        SimpleNamespace(skill_id="skill-creator", display_name="skill-creator", description="skill creator"),
    ]
    visible_tool_records = [
        # 第二条是普通工具记录，名字与执行器同名；过去它是家族 id，会被从对象字典里摘掉
        _tool_record("content"),
        _tool_record("filesystem_stat"),
    ]
    service = _build_selector_test_service(
        task=task,
        visible_skills=visible_skills,
        visible_tool_records=visible_tool_records,
        visible_tool_names=["content", "filesystem_stat"],
        tool_instances={
            "content": _StaticTool("content"),
            "filesystem_stat": _StaticTool("filesystem_stat"),
        },
    )

    async def _fake_build_node_context_selection(**kwargs):
        selector_calls.append(dict(kwargs))
        return NodeContextSelectionResult(
            selected_skill_ids=["tmux"],
            selected_tool_names=["content"],
        )

    monkeypatch.setattr(
        runtime_service_module,
        "build_node_context_selection",
        _fake_build_node_context_selection,
    )

    selection = await service._prepare_node_context_selection(task=task, node=node)
    provided = service._tool_provider(node)
    enriched = await service._enrich_node_messages(
        task=task,
        node=node,
        messages=[
            {"role": "system", "content": "base prompt"},
            {"role": "user", "content": '{"prompt":"inspect content navigation"}'},
        ],
    )

    assert selection.selected_tool_names == ["content"]
    # 治理可见的两条记录都进对象字典：没有分组层就没有"家族 id 要摘掉"这回事，
    # selection 只决定 callable，不决定字典上界。
    assert list(provided) == ["content", "filesystem_stat"]
    assert len(selector_calls) == 1
    contract_payload = extract_node_dynamic_contract_payload(enriched)
    assert contract_payload is not None
    assert contract_payload["callable_tool_names"] == ["content"]
    assert contract_payload["candidate_skills"] == [
        {
            "skill_id": "tmux",
            "description": "terminal workflow",
        }
    ]
    assert contract_payload["candidate_tools"] == []


@pytest.mark.asyncio
async def test_enrich_node_messages_moves_repair_required_tools_and_skills_into_dedicated_lists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = SimpleNamespace(
        task_id="task-repair-contract",
        session_id="web:shared",
        metadata={"core_requirement": "repair broken resources"},
    )
    node = SimpleNamespace(
        task_id="task-repair-contract",
        node_id="node-repair-contract",
        node_kind="execution",
        prompt="repair broken resources",
        goal="repair broken resources",
    )
    service = _build_selector_test_service(
        task=task,
        visible_skills=[
            SimpleNamespace(
                skill_id='broken_skill',
                display_name='broken_skill',
                description='Broken workflow',
                available=False,
                enabled=True,
                allowed_roles=['execution', 'inspection'],
                metadata={'warnings': ['missing required bins']},
            ),
        ],
        visible_tool_records=[
            SimpleNamespace(
                **{
                    **_tool_record('agent_browser').__dict__,
                    'available': False,
                    'metadata': {'warnings': ['missing required paths']},
                }
            ),
        ],
        visible_tool_names=[],
        tool_instances={},
    )

    async def _fake_build_node_context_selection(**kwargs):
        _ = kwargs
        return NodeContextSelectionResult(
            selected_skill_ids=["broken_skill"],
            selected_tool_names=[],
            candidate_skill_ids=["broken_skill"],
            candidate_tool_names=["agent_browser"],
        )

    monkeypatch.setattr(
        runtime_service_module,
        "build_node_context_selection",
        _fake_build_node_context_selection,
    )

    enriched = await service._enrich_node_messages(
        task=task,
        node=node,
        messages=[
            {"role": "system", "content": "base prompt"},
            {"role": "user", "content": '{"prompt":"repair broken resources"}'},
        ],
    )

    contract_payload = extract_node_dynamic_contract_payload(enriched)
    assert contract_payload is not None
    assert contract_payload["candidate_tools"] == []
    assert contract_payload["candidate_skills"] == []
    assert contract_payload["repair_required_tools"] == [
        {
            "tool_id": "agent_browser",
            "description": "",
            "reason": "missing required paths",
        }
    ]
    assert contract_payload["repair_required_skills"] == [
        {
            "skill_id": "broken_skill",
            "description": "Broken workflow",
            "reason": "missing required bins",
        }
    ]


@pytest.mark.asyncio
async def test_tool_provider_uses_full_visible_tool_fallback_when_selector_returns_visible_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = SimpleNamespace(
        task_id="task-1",
        session_id="web:shared",
        metadata={"core_requirement": "inspect filesystem fallback"},
    )
    node = SimpleNamespace(
        task_id="task-1",
        node_id="node-1",
        node_kind="execution",
        prompt="inspect filesystem fallback",
        goal="inspect filesystem fallback",
    )
    service = _build_selector_test_service(
        task=task,
        visible_skills=[],
        visible_tool_records=[
            _tool_record("content"),
            _tool_record("filesystem_stat"),
        ],
        visible_tool_names=["content", "filesystem_stat"],
        tool_instances={
            "content": _StaticTool("content"),
            "filesystem_stat": _StaticTool("filesystem_stat"),
        },
    )

    async def _fake_build_node_context_selection(**kwargs):
        _ = kwargs
        return NodeContextSelectionResult(
            selected_skill_ids=[],
            selected_tool_names=["content", "filesystem_stat"],
        )

    monkeypatch.setattr(
        runtime_service_module,
        "build_node_context_selection",
        _fake_build_node_context_selection,
    )

    await service._prepare_node_context_selection(task=task, node=node)
    provided = service._tool_provider(node)

    assert set(provided) == {"content", "filesystem_stat"}


@pytest.mark.asyncio
async def test_node_build_tools_preserves_protocol_tools_when_callable_tools_are_selector_narrowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = SimpleNamespace(
        task_id="task-1",
        session_id="web:shared",
        metadata={"core_requirement": "inspect content navigation"},
    )
    node = SimpleNamespace(
        task_id="task-1",
        node_id="node-1",
        node_kind="execution",
        prompt="inspect content navigation",
        goal="inspect content navigation",
        can_spawn_children=True,
    )
    service = _build_selector_test_service(
        task=task,
        visible_skills=[],
        visible_tool_records=[
            _tool_record("content"),
            _tool_record("filesystem_stat"),
        ],
        visible_tool_names=["content", "filesystem_stat"],
        tool_instances={
            "content": _StaticTool("content"),
            "filesystem_stat": _StaticTool("filesystem_stat"),
        },
    )

    async def _fake_build_node_context_selection(**kwargs):
        _ = kwargs
        return NodeContextSelectionResult(
            selected_skill_ids=[],
            selected_tool_names=["content"],
        )

    monkeypatch.setattr(
        runtime_service_module,
        "build_node_context_selection",
        _fake_build_node_context_selection,
    )

    await service._prepare_node_context_selection(task=task, node=node)

    runner = NodeRunner(
        store=SimpleNamespace(),
        log_service=SimpleNamespace(),
        react_loop=SimpleNamespace(),
        tool_provider=service._tool_provider,
        execution_model_refs=["execution"],
        acceptance_model_refs=["inspection"],
    )

    async def _spawn_children(*args, **kwargs):
        _ = args, kwargs
        return []

    runner._submit_final_result = service.node_runner._submit_final_result if hasattr(service, "node_runner") else NodeRunner._submit_final_result
    runner._spawn_children = _spawn_children

    tools = runner._build_tools(task=task, node=node)

    assert "content" in tools
    assert "filesystem_stat" in tools
    assert isinstance(tools["submit_final_result"], SubmitFinalResultTool)
    assert isinstance(tools["spawn_child_nodes"], SpawnChildNodesTool)


@pytest.mark.asyncio
async def test_prepare_node_context_selection_restores_callable_tools_from_persisted_frame_messages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = SimpleNamespace(
        task_id="task-1",
        session_id="web:shared",
        metadata={"core_requirement": "inspect content navigation"},
    )
    node = SimpleNamespace(
        task_id="task-1",
        node_id="node-1",
        node_kind="execution",
        prompt="inspect content navigation",
        goal="inspect content navigation",
    )
    service = _build_selector_test_service(
        task=task,
        visible_skills=[],
        visible_tool_records=[
            _tool_record("content"),
            _tool_record("filesystem_stat"),
        ],
        visible_tool_names=["content", "filesystem_stat"],
        tool_instances={
            "content": _StaticTool("content"),
            "filesystem_stat": _StaticTool("filesystem_stat"),
        },
    )
    service.log_service = SimpleNamespace(
        read_runtime_frame=lambda task_id, node_id: {
            "callable_tool_names": ["content"],
            "candidate_tool_names": [],
            "selected_skill_ids": [],
            "candidate_skill_ids": [],
            "rbac_visible_tool_names": ["content", "filesystem_stat"],
            "rbac_visible_skill_ids": [],
            "messages": [
                {"role": "system", "content": "base prompt"},
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "prompt": "inspect content navigation",
                            "goal": "inspect content navigation",
                            "core_requirement": "inspect content navigation",
                            "visible_skills": [
                                {
                                    "skill_id": "tmux",
                                    "display_name": "tmux",
                                    "description": "terminal workflow",
                                }
                            ],
                        },
                        ensure_ascii=False,
                    ),
                },
                NodeRuntimeToolContract(
                    node_id="node-1",
                    node_kind="execution",
                    callable_tool_names=["content"],
                    candidate_tool_names=[],
                    visible_skills=[],
                    candidate_skill_ids=[],

                    lightweight_tool_ids=[],
                    selection_trace={"mode": "persisted_frame_restore"},
                ).to_message(),
            ]
        }
    )

    async def _unexpected_build_node_context_selection(**kwargs):
        raise AssertionError(f"selector should not be recomputed when frame messages already carry callable tools: {kwargs!r}")

    monkeypatch.setattr(
        runtime_service_module,
        "build_node_context_selection",
        _unexpected_build_node_context_selection,
    )

    selection = await service._prepare_node_context_selection(task=task, node=node)
    provided = service._tool_provider(node)

    assert selection.selected_tool_names == ["content"]
    # 治理可见的两条记录都进对象字典：没有分组层就没有"家族 id 要摘掉"这回事，
    # selection 只决定 callable，不决定字典上界。
    assert list(provided) == ["content", "filesystem_stat"]


@pytest.mark.asyncio
async def test_prepare_node_context_selection_recomputes_when_cached_skill_visibility_drift_is_detected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = SimpleNamespace(
        task_id="task-cache-skill-drift",
        session_id="web:shared",
        metadata={"core_requirement": "load web skills"},
    )
    node = SimpleNamespace(
        task_id="task-cache-skill-drift",
        node_id="node-cache-skill-drift",
        node_kind="execution",
        prompt="load web skills",
        goal="load web skills",
    )
    visible_skills = [
        SimpleNamespace(skill_id="web-access", display_name="web-access", description="browser workflow"),
        SimpleNamespace(
            skill_id="multi-search-engine",
            display_name="multi-search-engine",
            description="search workflow",
        ),
    ]
    service = object.__new__(MainRuntimeService)
    service._node_context_selection_cache = {
        ("task-cache-skill-drift", "node-cache-skill-drift"): {
            "session_key": "web:shared",
            "actor_role": "execution",
            "visible_skills": [],
            "contract_visible_skill_ids": [],
            "skill_visibility_diagnostics": {"registry_skill_ids": [], "entries": []},
            "visible_tool_records": [],
            "visible_tool_names": ["web_fetch"],
            "prompt": "load web skills",
            "goal": "load web skills",
            "core_requirement": "load web skills",
            "selection": NodeContextSelectionResult(
                selected_skill_ids=[],
                selected_tool_names=["web_fetch"],
                candidate_skill_ids=[],
                candidate_tool_names=["web_fetch"],
            ),
        }
    }
    service.log_service = SimpleNamespace(read_runtime_frame=lambda task_id, node_id: None)
    service.list_contract_visible_skill_resources = (
        lambda *, actor_role, session_id: list(visible_skills)
    )
    service.list_visible_tools = (
        lambda *, actor_role, session_id: [_tool_record("web_fetch")]
    )
    service.list_effective_tool_names = (
        lambda *, actor_role, session_id: ["web_fetch", "load_skill_context", "load_tool_context"]
    )

    async def _no_refresh(*, session_id: str):
        _ = session_id
        return {"refreshed": False, "catalog_synced": False, "skill_ids": [], "tool_ids": []}

    service.maybe_refresh_external_resource_changes = _no_refresh

    selector_calls: list[dict[str, object]] = []

    async def _fake_build_node_context_selection(**kwargs):
        selector_calls.append(dict(kwargs))
        return NodeContextSelectionResult(
            selected_skill_ids=["web-access"],
            selected_tool_names=["web_fetch"],
            candidate_skill_ids=["web-access", "multi-search-engine"],
            candidate_tool_names=["web_fetch"],
        )

    monkeypatch.setattr(
        runtime_service_module,
        "build_node_context_selection",
        _fake_build_node_context_selection,
    )

    selection = await service._prepare_node_context_selection(task=task, node=node)

    assert selector_calls, "expected stale cached selection to be recomputed"
    assert selection.candidate_skill_ids == ["web-access", "multi-search-engine"]
    cached = service._node_context_selection_cache[("task-cache-skill-drift", "node-cache-skill-drift")]
    assert cached["contract_visible_skill_ids"] == ["web-access", "multi-search-engine"]


@pytest.mark.asyncio
async def test_prepare_node_context_selection_ignores_persisted_frame_when_live_skill_visibility_drift_is_detected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = SimpleNamespace(
        task_id="task-frame-skill-drift",
        session_id="web:shared",
        metadata={"core_requirement": "load web skills"},
    )
    node = SimpleNamespace(
        task_id="task-frame-skill-drift",
        node_id="node-frame-skill-drift",
        node_kind="execution",
        prompt="load web skills",
        goal="load web skills",
    )
    visible_skills = [
        SimpleNamespace(skill_id="web-access", display_name="web-access", description="browser workflow"),
    ]
    service = object.__new__(MainRuntimeService)
    service._node_context_selection_cache = {}
    service.log_service = SimpleNamespace(
        read_runtime_frame=lambda task_id, node_id: {
            "callable_tool_names": ["web_fetch"],
            "candidate_tool_names": ["web_fetch"],
            "selected_skill_ids": [],
            "candidate_skill_ids": [],
            "contract_visible_skill_ids": [],
            "skill_visibility_diagnostics": {"registry_skill_ids": [], "entries": []},
            "rbac_visible_tool_names": ["web_fetch"],
            "rbac_visible_skill_ids": [],
            "messages": [
                {"role": "system", "content": "base prompt"},
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "prompt": "load web skills",
                            "goal": "load web skills",
                            "core_requirement": "load web skills",
                        },
                        ensure_ascii=False,
                    ),
                },
            ],
        }
    )
    service.list_contract_visible_skill_resources = (
        lambda *, actor_role, session_id: list(visible_skills)
    )
    service.list_visible_tools = (
        lambda *, actor_role, session_id: [_tool_record("web_fetch")]
    )
    service.list_effective_tool_names = (
        lambda *, actor_role, session_id: ["web_fetch", "load_skill_context", "load_tool_context"]
    )

    async def _no_refresh(*, session_id: str):
        _ = session_id
        return {"refreshed": False, "catalog_synced": False, "skill_ids": [], "tool_ids": []}

    service.maybe_refresh_external_resource_changes = _no_refresh

    selector_calls: list[dict[str, object]] = []

    async def _fake_build_node_context_selection(**kwargs):
        selector_calls.append(dict(kwargs))
        return NodeContextSelectionResult(
            selected_skill_ids=["web-access"],
            selected_tool_names=["web_fetch"],
            candidate_skill_ids=["web-access"],
            candidate_tool_names=["web_fetch"],
        )

    monkeypatch.setattr(
        runtime_service_module,
        "build_node_context_selection",
        _fake_build_node_context_selection,
    )

    selection = await service._prepare_node_context_selection(task=task, node=node)

    assert selector_calls, "expected stale persisted frame selection to be recomputed"
    assert selection.candidate_skill_ids == ["web-access"]


@pytest.mark.asyncio
async def test_create_task_assigns_distinct_task_temp_dirs_and_injects_runtime_environment(tmp_path: Path):
    service = MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        workspace_root=tmp_path,
        store_path=tmp_path / 'runtime.sqlite3',
        files_base_dir=tmp_path / 'tasks',
        artifact_dir=tmp_path / 'artifacts',
        governance_store_path=tmp_path / 'governance.sqlite3',
        execution_mode='web',
    )
    service._workspace_root = lambda: tmp_path
    try:
        original = await _create_web_task(service)
        continuation = await service.create_task(
            'second task',
            session_id='web:shared',
            metadata={},
        )
        original_temp_dir = _task_temp_dir_for(service, original.task_id)
        continuation_temp_dir = _task_temp_dir_for(service, continuation.task_id)

        assert original_temp_dir == str(tmp_path / 'temp' / 'tasks' / original.task_id.replace(':', '_'))
        assert continuation_temp_dir == str(tmp_path / 'temp' / 'tasks' / continuation.task_id.replace(':', '_'))
        assert original_temp_dir != continuation_temp_dir
        assert Path(original_temp_dir).is_dir()
        assert Path(continuation_temp_dir).is_dir()

        root = service.store.get_node(original.root_node_id)
        assert root is not None
        messages = await service.node_runner._build_messages(task=original, node=root)
        payload = json.loads(messages[1]['content'])

        assert payload['runtime_environment']['task_temp_dir'] == original_temp_dir
        assert payload['runtime_environment']['path_policy']['exec_default_working_dir'] == 'task_temp_dir'
        path_policy = payload['runtime_environment']['path_policy']
        assert path_policy['relative_paths_bind_to_workspace'] is True
        assert path_policy['bare_filename_binds_to_task_temp_dir'] is True
        assert '{workspace}' in path_policy['path_anchor_tokens']
        assert '{temp}' in path_policy['path_anchor_tokens']
    finally:
        await service.close()






def test_spawn_child_nodes_tool_requires_execution_policy_for_each_child() -> None:
    async def _spawn_callback(specs, call_id):
        _ = specs, call_id
        return []

    tool = SpawnChildNodesTool(_spawn_callback)

    errors = tool.validate_params(
        {
            'children': [
                {
                    'goal': 'child goal',
                    'prompt': 'child prompt',
                }
            ]
        }
    )

    assert 'missing required children[0].execution_policy' in errors






@pytest.mark.asyncio
async def test_persisted_tool_result_output_ref_stays_canonical_while_execution_trace_preserves_wrapper_ref(tmp_path: Path):
    service = MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        workspace_root=tmp_path,
        store_path=tmp_path / 'runtime.sqlite3',
        files_base_dir=tmp_path / 'tasks',
        artifact_dir=tmp_path / 'artifacts',
        governance_store_path=tmp_path / 'governance.sqlite3',
        execution_mode='web',
    )
    try:
        record = await _create_web_task(service)
        navigator = service.content_store
        inner = navigator.maybe_externalize_text(
            'canonical body',
            runtime={'task_id': record.task_id, 'node_id': record.root_node_id},
            display_name='inner',
            source_kind='node_output',
            force=True,
        )
        assert inner is not None
        wrapped = navigator.maybe_externalize_text(
            json.dumps(inner.to_dict(), ensure_ascii=False),
            runtime={'task_id': record.task_id, 'node_id': record.root_node_id},
            display_name='wrapped',
            source_kind='tool_result:content',
            force=True,
        )
        assert wrapped is not None

        tool_calls = [
            ToolCallRequest(
                id='call:content',
                name='content',
                arguments={'action': 'open', 'ref': wrapped.ref},
            )
        ]
        service.log_service.append_node_output(
            record.task_id,
            record.root_node_id,
            content='',
            tool_calls=[{'id': 'call:content', 'name': 'content', 'arguments': {'action': 'open', 'ref': wrapped.ref}}],
        )
        service.log_service.record_tool_result_batch(
            task_id=record.task_id,
            node_id=record.root_node_id,
            response_tool_calls=tool_calls,
            results=[
                _tool_result_payload(
                    call_id='call:content',
                    tool_name='content',
                    content=json.dumps(wrapped.to_dict(), ensure_ascii=False),
                )
            ],
        )

        tool_results = service.store.list_task_node_tool_results(record.task_id, record.root_node_id)
        assert len(tool_results) == 1
        assert tool_results[0].output_ref == inner.ref

        detail = service.get_node_detail_payload(record.task_id, record.root_node_id, detail_level='full')
        assert detail is not None
        tool_step = detail['item']['execution_trace']['turns'][0]['tools'][0]
        assert tool_step['output_ref'] == wrapped.ref
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_inline_content_json_tool_results_preserve_structured_refs(tmp_path: Path):
    service = MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        workspace_root=tmp_path,
        store_path=tmp_path / 'runtime.sqlite3',
        files_base_dir=tmp_path / 'tasks',
        artifact_dir=tmp_path / 'artifacts',
        governance_store_path=tmp_path / 'governance.sqlite3',
        execution_mode='web',
    )
    try:
        record = await _create_web_task(service)
        navigator = service.content_store
        inner = navigator.maybe_externalize_text(
            'alpha\nneedle\nomega\n',
            runtime={'task_id': record.task_id, 'node_id': record.root_node_id},
            display_name='inner',
            source_kind='node_output',
            force=True,
        )
        assert inner is not None
        wrapped = navigator.maybe_externalize_text(
            json.dumps(inner.to_dict(), ensure_ascii=False),
            runtime={'task_id': record.task_id, 'node_id': record.root_node_id},
            display_name='wrapped',
            source_kind='tool_result:content',
            force=True,
        )
        assert wrapped is not None
        inline_open_payload = service.open_content(ref=wrapped.ref)

        tool_calls = [
            ToolCallRequest(
                id='call:content-open',
                name='content',
                arguments={'action': 'open', 'ref': wrapped.ref},
            )
        ]
        service.log_service.append_node_output(
            record.task_id,
            record.root_node_id,
            content='',
            tool_calls=[{'id': 'call:content-open', 'name': 'content', 'arguments': {'action': 'open', 'ref': wrapped.ref}}],
        )
        service.log_service.record_tool_result_batch(
            task_id=record.task_id,
            node_id=record.root_node_id,
            response_tool_calls=tool_calls,
            results=[
                _tool_result_payload(
                    call_id='call:content-open',
                    tool_name='content',
                    content=json.dumps(inline_open_payload, ensure_ascii=False),
                )
            ],
        )

        tool_results = service.store.list_task_node_tool_results(record.task_id, record.root_node_id)
        assert len(tool_results) == 1
        assert tool_results[0].output_ref == inner.ref

        detail = service.get_node_detail_payload(record.task_id, record.root_node_id, detail_level='full')
        assert detail is not None
        tool_step = detail['item']['execution_trace']['turns'][0]['tools'][0]
        assert tool_step['output_ref'] == wrapped.ref
    finally:
        await service.close()














def test_prompt_cache_key_ignores_tool_schema_changes_when_messages_match() -> None:
    messages = [
        {'role': 'system', 'content': 'system'},
        {'role': 'user', 'content': '{"task_id":"task-1","goal":"demo"}'},
    ]
    tools_a = [
        {
            'name': 'web_fetch',
            'description': '',
            'parameters': {'type': 'object'},
        }
    ]
    tools_b = [
        {
            'name': 'filesystem_edit',
            'description': '',
            'parameters': {'type': 'object'},
        }
    ]

    first = build_stable_prompt_cache_key(messages, tools_a, 'model-a')
    second = build_stable_prompt_cache_key(messages, tools_b, 'model-a')

    assert first == second


def test_submit_final_result_tool_schema_is_hard_switched_to_final_or_blocked() -> None:
    tool = _submit_final_result_tool()

    assert tool.parameters['properties']['delivery_status']['enum'] == ['final', 'blocked']
    assert tool.parameters['required'] == [
        'status',
        'delivery_status',
        'summary',
        'answer',
        'evidence',
        'remaining_work',
        'blocking_reason',
    ]










@pytest.mark.asyncio
async def test_execution_node_can_finish_via_submit_final_result_tool(tmp_path: Path):
    class _Backend:
        async def chat(self, **kwargs):
            _ = kwargs
            return LLMResponse(
                content='',
                tool_calls=[
                    _final_result_call(
                        status='success',
                        delivery_status='final',
                        summary='done',
                        answer='done',
                        evidence=[{'kind': 'artifact', 'note': 'final result tool path'}],
                        remaining_work=[],
                        blocking_reason='',
                    )
                ],
                finish_reason='tool_calls',
                usage={'input_tokens': 10, 'output_tokens': 5, 'cache_hit_tokens': 0},
            )

    service = MainRuntimeService(
        chat_backend=_Backend(),
        workspace_root=tmp_path,
        store_path=tmp_path / 'runtime.sqlite3',
        files_base_dir=tmp_path / 'tasks',
        artifact_dir=tmp_path / 'artifacts',
        governance_store_path=tmp_path / 'governance.sqlite3',
        execution_mode='embedded',
    )
    try:
        record = await service.create_task('submit-final-result success', session_id='web:shared')
        await service.wait_for_task(record.task_id)
        task = service.store.get_task(record.task_id)
        root = service.store.get_node(record.root_node_id)
        assert task is not None
        assert root is not None
        assert task.status == 'success'
        assert root.status == 'success'
        assert root.final_output == 'done'
        assert str((root.metadata or {}).get('result_payload_ref') or '').strip()
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_execution_node_can_finish_via_xml_direct_final_result(tmp_path: Path):
    class _Backend:
        def __init__(self) -> None:
            self.turn = 0

        async def chat(self, **kwargs):
            _ = kwargs
            self.turn += 1
            return LLMResponse(
                content='<minimax:tool_call><invoke name="submit_final_result"><parameter name="status">success</parameter><parameter name="delivery_status">final</parameter><parameter name="summary">done</parameter><parameter name="answer">done</parameter><parameter name="evidence">[]</parameter><parameter name="remaining_work">[]</parameter><parameter name="blocking_reason"></parameter></invoke></minimax:tool_call>',
                tool_calls=[],
                finish_reason='stop',
                usage={'input_tokens': 10, 'output_tokens': 5, 'cache_hit_tokens': 0},
            )

    backend = _Backend()
    service = MainRuntimeService(
        chat_backend=backend,
        workspace_root=tmp_path,
        store_path=tmp_path / 'runtime.sqlite3',
        files_base_dir=tmp_path / 'tasks',
        artifact_dir=tmp_path / 'artifacts',
        governance_store_path=tmp_path / 'governance.sqlite3',
        execution_mode='embedded',
    )
    try:
        record = await service.create_task('submit-final-result xml direct success', session_id='web:shared')
        await service.wait_for_task(record.task_id)
        task = service.store.get_task(record.task_id)
        root = service.store.get_node(record.root_node_id)
        assert task is not None
        assert root is not None
        assert backend.turn == 1
        assert task.status == 'success'
        assert root.final_output == 'done'
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_execution_node_rejects_failed_final_then_accepts_blocked(tmp_path: Path):
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
                        _final_result_call(
                            status='failed',
                            delivery_status='final',
                            summary='invalid execution failure',
                            answer='',
                            evidence=[],
                            remaining_work=[],
                            blocking_reason='',
                        )
                    ],
                    finish_reason='tool_calls',
                    usage={'input_tokens': 10, 'output_tokens': 5, 'cache_hit_tokens': 0},
                )
            return LLMResponse(
                content='',
                tool_calls=[
                    _final_result_call(
                        status='failed',
                        delivery_status='blocked',
                        summary='blocked',
                        answer='',
                        evidence=[],
                        remaining_work=[],
                        blocking_reason='blocked correctly',
                    )
                ],
                finish_reason='tool_calls',
                usage={'input_tokens': 10, 'output_tokens': 5, 'cache_hit_tokens': 0},
            )

    backend = _Backend()
    service = MainRuntimeService(
        chat_backend=backend,
        workspace_root=tmp_path,
        store_path=tmp_path / 'runtime.sqlite3',
        files_base_dir=tmp_path / 'tasks',
        artifact_dir=tmp_path / 'artifacts',
        governance_store_path=tmp_path / 'governance.sqlite3',
        execution_mode='embedded',
    )
    try:
        record = await service.create_task('invalid failed final', session_id='web:shared')
        await service.wait_for_task(record.task_id)
        task = service.store.get_task(record.task_id)
        assert task is not None
        assert backend.turn == 1
        assert task.status == 'failed'
        assert 'invalid execution failure' in str(task.failure_reason or '')
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_old_text_json_no_longer_finishes_node(tmp_path: Path):
    class _Backend:
        def __init__(self) -> None:
            self.turn = 0

        async def chat(self, **kwargs):
            _ = kwargs
            self.turn += 1
            if self.turn == 1:
                return LLMResponse(
                    content='{"status":"success","delivery_status":"final","summary":"old path","answer":"old path","evidence":[{"kind":"artifact","note":"legacy"}],"remaining_work":[],"blocking_reason":""}',
                    tool_calls=[],
                    finish_reason='stop',
                    usage={'input_tokens': 10, 'output_tokens': 5, 'cache_hit_tokens': 0},
                )
            return LLMResponse(
                content='',
                tool_calls=[
                    _final_result_call(
                        status='success',
                        delivery_status='final',
                        summary='new path',
                        answer='new path',
                        evidence=[{'kind': 'artifact', 'note': 'new tool path'}],
                        remaining_work=[],
                        blocking_reason='',
                    )
                ],
                finish_reason='tool_calls',
                usage={'input_tokens': 10, 'output_tokens': 5, 'cache_hit_tokens': 0},
            )

    backend = _Backend()
    service = MainRuntimeService(
        chat_backend=backend,
        workspace_root=tmp_path,
        store_path=tmp_path / 'runtime.sqlite3',
        files_base_dir=tmp_path / 'tasks',
        artifact_dir=tmp_path / 'artifacts',
        governance_store_path=tmp_path / 'governance.sqlite3',
        execution_mode='embedded',
    )
    try:
        record = await service.create_task('legacy text result path', session_id='web:shared')
        await service.wait_for_task(record.task_id)
        task = service.store.get_task(record.task_id)
        root = service.store.get_node(record.root_node_id)
        assert task is not None
        assert root is not None
        assert backend.turn == 2
        assert task.status == 'success'
        assert root.final_output == 'new path'
        assert len(list(root.output or [])) >= 2
    finally:
        await service.close()




@pytest.mark.asyncio
async def test_invalid_submit_final_result_fails_after_five_attempts(tmp_path: Path):
    class _Backend:
        def __init__(self) -> None:
            self.turn = 0

        async def chat(self, **kwargs):
            _ = kwargs
            self.turn += 1
            return LLMResponse(
                content='',
                tool_calls=[
                    _final_result_call(
                        call_id=f'call:final:{self.turn}',
                        status='failed',
                        delivery_status='final',
                        summary='still invalid',
                        answer='',
                        evidence=[],
                        remaining_work=[],
                        blocking_reason='',
                    )
                ],
                finish_reason='tool_calls',
                usage={'input_tokens': 10, 'output_tokens': 5, 'cache_hit_tokens': 0},
            )

    backend = _Backend()
    service = MainRuntimeService(
        chat_backend=backend,
        workspace_root=tmp_path,
        store_path=tmp_path / 'runtime.sqlite3',
        files_base_dir=tmp_path / 'tasks',
        artifact_dir=tmp_path / 'artifacts',
        governance_store_path=tmp_path / 'governance.sqlite3',
        execution_mode='embedded',
    )
    try:
        record = await service.create_task('invalid final result limit', session_id='web:shared')
        await service.wait_for_task(record.task_id)
        task = service.store.get_task(record.task_id)
        assert task is not None
        assert backend.turn == 1
        assert task.status == 'failed'
        assert 'still invalid' in str(task.failure_reason or '')
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_model_request_attempt_timeout_falls_back_within_chain(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import main.runtime.chat_backend as chat_backend_module
    from g3ku.providers.provider_factory import ProviderTarget

    class _HangingProvider:
        # Does not manage timeouts internally, so the backend applies the
        # per-single-request attempt timeout around this call.
        async def chat(self, **kwargs):
            _ = kwargs
            await asyncio.Event().wait()

    class _FinalResultProvider:
        async def chat(self, **kwargs):
            _ = kwargs
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
                usage={'input_tokens': 8, 'output_tokens': 4},
            )

    targets = {
        'primary': ProviderTarget(
            provider_ref='primary',
            provider_id='openai',
            model_id='primary-model',
            provider=_HangingProvider(),
            retry_on=['network', '429'],
            retry_count=0,
            api_key_count=1,
            api_key_indexes=[0],
        ),
        'secondary': ProviderTarget(
            provider_ref='secondary',
            provider_id='openai',
            model_id='secondary-model',
            provider=_FinalResultProvider(),
            retry_on=['network', '429'],
            retry_count=0,
            api_key_count=1,
            api_key_indexes=[0],
        ),
    }
    monkeypatch.setattr(
        chat_backend_module,
        'build_provider_from_model_key',
        lambda config, ref, api_key_index=None: targets[str(ref)],
    )

    backend = chat_backend_module.ConfigChatBackend(config=SimpleNamespace())
    # Per-single-request response cap for one provider attempt; the hanging
    # primary attempt hits it and the chain falls back to the secondary model
    # instead of the task failing on a total-budget timeout.
    backend._model_attempt_timeout_seconds = 0.05

    service = MainRuntimeService(
        chat_backend=backend,
        workspace_root=tmp_path,
        store_path=tmp_path / "runtime.sqlite3",
        files_base_dir=tmp_path / "tasks",
        artifact_dir=tmp_path / "artifacts",
        governance_store_path=tmp_path / "governance.sqlite3",
        execution_model_refs=['primary', 'secondary'],
        execution_mode="embedded",
    )
    try:
        record = await service.create_task("timeout model request", session_id="web:shared")
        await service.wait_for_task(record.task_id)
        task = service.store.get_task(record.task_id)
        root = service.store.get_node(record.root_node_id)
        assert task is not None
        assert root is not None
        assert root.status == "success"
        assert task.status == "success"
    finally:
        await service.close()




@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('status', 'delivery_status', 'answer', 'blocking_reason'),
    [
        ('success', 'final', 'accepted', ''),
        ('failed', 'final', 'rejected', ''),
        ('failed', 'blocked', '', 'missing evidence'),
    ],
)
async def test_acceptance_node_supports_allowed_final_result_combinations(
    tmp_path: Path,
    status: str,
    delivery_status: str,
    answer: str,
    blocking_reason: str,
) -> None:
    class _Backend:
        async def chat(self, **kwargs):
            _ = kwargs
            return LLMResponse(
                content='',
                tool_calls=[
                    _final_result_call(
                        status=status,
                        delivery_status=delivery_status,
                        summary='acceptance result',
                        answer=answer,
                        evidence=[{'kind': 'artifact', 'note': 'acceptance evidence'}] if status == 'success' else [],
                        remaining_work=[],
                        blocking_reason=blocking_reason,
                    )
                ],
                finish_reason='tool_calls',
                usage={'input_tokens': 10, 'output_tokens': 5, 'cache_hit_tokens': 0},
            )

    service = MainRuntimeService(
        chat_backend=_Backend(),
        workspace_root=tmp_path,
        store_path=tmp_path / f'{status}-{delivery_status}.sqlite3',
        files_base_dir=tmp_path / 'tasks',
        artifact_dir=tmp_path / 'artifacts',
        governance_store_path=tmp_path / f'{status}-{delivery_status}-governance.sqlite3',
        execution_mode='web',
    )
    try:
        record = await _create_web_task(service)
        task = service.get_task(record.task_id)
        root = service.get_node(record.root_node_id)
        assert task is not None
        assert root is not None
        acceptance = service.node_runner.create_acceptance_node(
            task=task,
            accepted_node=root,
            goal='accept root output',
            acceptance_prompt='verify the root output',
            parent_node_id=root.node_id,
        )
        # 验收抢跑闸门要求对方有一笔待裁定的闭合提交，否则 run_node 直接落
        # success+partial 冻结、三条裁定都到不了模型。三条派验车道在派发前都
        # 会登记这份握手，这里补的是同一个事实。
        service.store.update_node(
            root.node_id,
            lambda n: n.model_copy(update={'metadata': {
                **(n.metadata or {}),
                ACCEPTANCE_HANDSHAKE_KEY: {
                    'state': 'waiting_acceptance',
                    'acceptance_node_id': acceptance.node_id,
                    'latest_execution_result_ref': 'artifact:fixture-result',
                    'latest_execution_result_summary': 'fixture submission',
                },
            }}),
        )
        result = await service.node_runner.run_node(record.task_id, acceptance.node_id)
        latest = service.store.get_node(acceptance.node_id)
        assert latest is not None
        assert result.status == status
        assert result.delivery_status == delivery_status
        assert latest.status == status
    finally:
        await service.close()






@pytest.mark.asyncio
async def test_repeated_exec_call_is_soft_rejected_without_engine_failure(tmp_path: Path):
    class _Backend:
        def __init__(self) -> None:
            self._turn = 0

        @staticmethod
        def _messages_text(kwargs: dict[str, object]) -> str:
            messages = list(kwargs.get('messages') or [])
            parts: list[str] = []
            for item in messages:
                if not isinstance(item, dict):
                    continue
                content = item.get('content')
                if isinstance(content, str) and content.strip():
                    parts.append(content)
            return '\n'.join(parts)

        async def chat(self, **kwargs):
            self._turn += 1
            if self._turn in {1, 2, 3}:
                return LLMResponse(
                    content='',
                    tool_calls=[
                        ToolCallRequest(
                            id=f'call:exec:{self._turn}',
                            name='exec',
                            arguments={
                                'command': "Write-Output 'inspect multi-search-engine'",
                                'working_dir': r'D:\NewProjects\G3KU',
                            },
                        )
                    ],
                    finish_reason='tool_calls',
                    usage={'input_tokens': 10, 'output_tokens': 5, 'cache_hit_tokens': 0},
                )
            if self._turn == 4:
                text = self._messages_text(kwargs)
                assert 'duplicate tool call' in text.lower()
                assert 'exec' in text
                return LLMResponse(
                    content='',
                    tool_calls=[
                        _final_result_call(
                            status='failed',
                            delivery_status='blocked',
                            summary='intentional stop after duplicate tool warning',
                            answer='',
                            evidence=[],
                            remaining_work=['stop after duplicate-call soft reject check'],
                            blocking_reason='intentional stop after duplicate tool warning',
                        )
                    ],
                    finish_reason='tool_calls',
                    usage={'input_tokens': 10, 'output_tokens': 5, 'cache_hit_tokens': 0},
                )
            if self._turn == 5:
                # blocked 核验节点：裁决阻塞成立，放行执行节点的主动失败
                text = self._messages_text(kwargs)
                assert 'failed+blocked' in text
                return LLMResponse(
                    content='',
                    tool_calls=[
                        _final_result_call(
                            status='success',
                            delivery_status='final',
                            summary='阻塞成立：重复调用软拒后主动停止属于合理终止',
                            answer='',
                            evidence=[{'kind': 'artifact', 'note': 'verified duplicate-call soft reject'}],
                            remaining_work=[],
                            blocking_reason='',
                        )
                    ],
                    finish_reason='tool_calls',
                    usage={'input_tokens': 10, 'output_tokens': 5, 'cache_hit_tokens': 0},
                )
            raise AssertionError(f'unexpected extra turn: {self._turn}')

    service = MainRuntimeService(
        chat_backend=_Backend(),
        workspace_root=tmp_path,
        store_path=tmp_path / 'runtime.sqlite3',
        files_base_dir=tmp_path / 'tasks',
        artifact_dir=tmp_path / 'artifacts',
        governance_store_path=tmp_path / 'governance.sqlite3',
        execution_mode='embedded',
    )

    def _build_tools(task, node):
        return {
            'submit_final_result': SubmitFinalResultTool(service.node_runner._submit_final_result, node_kind=node.node_kind),
            'exec': _StaticTool(
                'exec',
                json.dumps(
                    {
                        'status': 'success',
                        'exit_code': 0,
                        'head_preview': 'inspect result already available',
                    },
                    ensure_ascii=False,
                ),
            ),
        }

    service.node_runner._build_tools = _build_tools
    try:
        record = await service.create_task('duplicate exec soft reject regression', session_id='web:shared')
        await service.wait_for_task(record.task_id)
        task = service.store.get_task(record.task_id)
        assert task is not None
        assert task.status == 'failed'
        assert 'repeated tool call detected: exec' not in str(task.failure_reason or '')
        assert 'intentional stop after duplicate tool warning' in str(task.failure_reason or '')
        # failed+blocked 经过阻塞核验放行，失败记录带核验标记
        assert '[blocked核验]' in str(task.failure_reason or '')
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_current_task_progress_after_spawn_fails_after_three_ignored_repair_guidances(tmp_path: Path):
    class _Backend:
        def __init__(self) -> None:
            self._turn = 0

        @staticmethod
        def _messages_text(kwargs: dict[str, object]) -> str:
            messages = list(kwargs.get('messages') or [])
            parts: list[str] = []
            for item in messages:
                if not isinstance(item, dict):
                    continue
                content = item.get('content')
                if isinstance(content, str) and content.strip():
                    parts.append(content)
            return '\n'.join(parts)

        async def chat(self, **kwargs):
            self._turn += 1
            if self._turn == 1:
                return LLMResponse(
                    content='',
                    tool_calls=[
                        ToolCallRequest(
                            id='call:spawn',
                            name='spawn_child_nodes',
                            arguments={
                                'children': [
                                    {
                                        'goal': 'dummy child',
                                        'prompt': 'dummy prompt',
                                        'execution_policy': {'mode': 'focus'},
                                    }
                                ]
                            },
                        )
                    ],
                    finish_reason='tool_calls',
                    usage={'input_tokens': 10, 'output_tokens': 5, 'cache_hit_tokens': 0},
                )

            if self._turn in {2, 3, 4}:
                if self._turn > 2:
                    text = self._messages_text(kwargs)
                    assert '不得对当前正在执行的 `task_id` 调用 `task_progress`' in text
                    assert 'artifact:artifact:test-spawn' in text
                task_id = ''
                for item in list(kwargs.get('messages') or []):
                    if not isinstance(item, dict) or item.get('role') != 'user':
                        continue
                    content = item.get('content')
                    if not isinstance(content, str) or '"task_id"' not in content:
                        continue
                    try:
                        task_id = str(json.loads(content).get('task_id') or '').strip()
                    except Exception:
                        continue
                assert task_id
                return LLMResponse(
                    content='',
                    tool_calls=[
                        ToolCallRequest(
                            id=f'call:progress:{self._turn}',
                            name='task_progress',
                            arguments={'任务id': task_id},
                        )
                    ],
                    finish_reason='tool_calls',
                    usage={'input_tokens': 10, 'output_tokens': 5, 'cache_hit_tokens': 0},
                )

            raise AssertionError(f'unexpected extra turn: {self._turn}')

    service = MainRuntimeService(
        chat_backend=_Backend(),
        workspace_root=tmp_path,
        store_path=tmp_path / 'runtime.sqlite3',
        files_base_dir=tmp_path / 'tasks',
        artifact_dir=tmp_path / 'artifacts',
        governance_store_path=tmp_path / 'governance.sqlite3',
        execution_mode='embedded',
    )

    def _build_tools(task, node):
        return {
            'submit_final_result': SubmitFinalResultTool(service.node_runner._submit_final_result, node_kind=node.node_kind),
            'spawn_child_nodes': _StaticTool(
                'spawn_child_nodes',
                json.dumps(
                    {
                        'ref': 'artifact:artifact:test-spawn',
                        'summary': 'Use content_search/content_open with ref=artifact:artifact:test-spawn',
                    },
                    ensure_ascii=False,
                ),
            ),
            'task_progress': _StaticTool(
                'task_progress',
                json.dumps({'task_status': 'in_progress'}, ensure_ascii=False),
            ),
        }

    service.node_runner._build_tools = _build_tools
    try:
        record = await service.create_task('self task_progress repair regression', session_id='web:shared')
        await service.wait_for_task(record.task_id)
        task = service.store.get_task(record.task_id)
        assert task is not None
        # 只读重复护栏按新语义进入可恢复的错误暂停：任务不终态，根节点暂停并落错误日志
        assert task.status == 'in_progress'
        paused = service.get_node(record.root_node_id)
        assert paused is not None
        assert paused.is_paused is True
        assert paused.pause_reason == 'error'
        logs = service.log_service.list_task_error_logs(record.task_id)
        assert logs
        error_text = str(logs[0].error_text or '')
        assert 'repeated tool call detected: task_progress' not in error_text
        assert 'read-only repair guidance' in error_text
        assert 'task_progress' in error_text
        assert 'artifact:artifact:test-spawn' in error_text

        detail = service.get_node_detail_payload(record.task_id, record.root_node_id, detail_level='full')
        assert detail is not None
        turns = detail['item']['execution_trace']['turns']
        if turns:
            round_tools = [item['tool_name'] for item in turns[0]['tools']]
            assert round_tools == ['spawn_child_nodes']
    finally:
        await service.close()


def test_node_runtime_image_gate_takes_conservative_chain_intersection(monkeypatch: pytest.MonkeyPatch) -> None:
    """节点道的图片闸门按整条链取保守交集，与 CEO 前门同一份判据。

    曾有两处读数分叉：拼图那步取交集、工具运行时上下文看链首。混链时工具会答「可以开图」
    而拼图那步判「不可以」，模型手里留下一条 ok:true 却等不到像素。
    """
    loop = _react_loop_for_content_open_image_overlay()
    models = {
        'mm': SimpleNamespace(image_multimodal_enabled=True),
        'plain': SimpleNamespace(image_multimodal_enabled=False),
    }
    config = SimpleNamespace(get_managed_model=lambda key: models.get(str(key or '').strip()))
    monkeypatch.setattr('main.runtime.react_loop.get_runtime_config', lambda force=False: (config, 1, False))

    assert loop._image_multimodal_enabled_for_model_refs(['mm']) is True
    assert loop._image_multimodal_enabled_for_model_refs(['mm', 'plain']) is False
    assert loop._image_multimodal_enabled_for_model_refs(['plain', 'mm']) is False
    # 解析不到的成员跳过：运行时不会把它发出去，不该替它否决整条链。
    assert loop._image_multimodal_enabled_for_model_refs(['gone', 'mm']) is True
    assert loop._image_multimodal_enabled_for_model_refs(['gone']) is False


def test_node_runtime_builds_content_open_image_overlay_message_blocks(tmp_path: Path):
    loop = _react_loop_for_content_open_image_overlay()
    image_path = tmp_path / 'demo.png'
    image_path.write_bytes(b'\x89PNG\r\n\x1a\nsmall')

    payload = {
        'ok': True,
        'operation': 'open',
        'content_kind': 'image',
        'mime_type': 'image/png',
        'summary': '图片已通过 content_open 打开，视觉内容将在下一轮请求中附带。',
        'multimodal_open_pending': True,
        'runtime_image_target': {
            'path': str(image_path),
            'mime_type': 'image/png',
            'source_ref': '',
        },
    }

    blocks = loop._content_open_image_overlay_message_blocks([payload], runtime_context={'image_multimodal_enabled': True})

    assert blocks[0] == {'type': 'text', 'text': '图片已通过 content_open 打开，视觉内容已附带在本轮上下文中'}
    assert blocks[1]['type'] == 'image_url'
    assert str(blocks[1]['image_url']['url']).startswith('data:image/png;base64,')


def test_node_runtime_content_open_image_overlay_degrades_missing_image(tmp_path: Path):
    """缺图不得抛出：该构造点在工具错误道之外，异常会把整个节点打成 error-pause。"""
    loop = _react_loop_for_content_open_image_overlay()
    good_path = tmp_path / 'good.png'
    good_path.write_bytes(b'\x89PNG\r\n\x1a\nsmall')
    missing_path = tmp_path / 'hallucinated.png'

    def _payload(path: Path) -> dict:
        return {
            'ok': True,
            'content_kind': 'image',
            'mime_type': 'image/png',
            'multimodal_open_pending': True,
            'runtime_image_target': {
                'path': str(path),
                'mime_type': 'image/png',
                'display_name': path.name,
            },
        }

    blocks = loop._content_open_image_overlay_message_blocks(
        [_payload(missing_path), _payload(good_path)],
        runtime_context={'image_multimodal_enabled': True},
    )

    notices = [block for block in blocks if block.get('type') == 'text' and 'hallucinated.png' in str(block.get('text'))]
    attached = [block for block in blocks if block.get('type') == 'image_url']
    assert len(notices) == 1
    assert '文件不存在' in str(notices[0]['text'])
    assert len(attached) == 1
    assert str(attached[0]['image_url']['url']).startswith('data:image/png;base64,')


def test_node_runtime_content_open_image_overlay_degrades_non_file_image_target(tmp_path: Path):
    """目标是目录时 is_file() 先挡下，绝不让 IsADirectoryError 走到 read_bytes 之外。"""
    loop = _react_loop_for_content_open_image_overlay()
    directory_target = tmp_path / 'not-a-file.png'
    directory_target.mkdir()

    blocks = loop._content_open_image_overlay_message_blocks(
        [
            {
                'ok': True,
                'content_kind': 'image',
                'mime_type': 'image/png',
                'multimodal_open_pending': True,
                'runtime_image_target': {'path': str(directory_target), 'mime_type': 'image/png'},
            }
        ],
        runtime_context={'image_multimodal_enabled': True},
    )

    assert not any(block.get('type') == 'image_url' for block in blocks)
    assert any('未能附带' in str(block.get('text')) for block in blocks if block.get('type') == 'text')


def test_node_runtime_strip_content_open_image_overlay_from_durable_messages() -> None:
    loop = _react_loop_for_content_open_image_overlay()

    stripped = loop._strip_content_open_image_overlay_from_message_records(
        [
            {
                'role': 'user',
                'content': [
                    {'type': 'text', 'text': '图片已通过 content_open 打开，视觉内容已附带在本轮上下文中'},
                    {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,AAAA'}},
                ],
            }
        ]
    )

    assert stripped == [{'role': 'user', 'content': '图片已通过 content_open 打开，视觉内容已附带在本轮上下文中'}]


@pytest.mark.asyncio
async def test_repeated_content_open_fails_after_three_ignored_repair_guidances(tmp_path: Path):
    class _Backend:
        def __init__(self) -> None:
            self._turn = 0

        @staticmethod
        def _messages_text(kwargs: dict[str, object]) -> str:
            messages = list(kwargs.get('messages') or [])
            return '\n'.join(
                content
                for item in messages
                if isinstance(item, dict)
                for content in [item.get('content')]
                if isinstance(content, str) and content.strip()
            )

        async def chat(self, **kwargs):
            self._turn += 1
            if self._turn == 1:
                return LLMResponse(
                    content='',
                    tool_calls=[
                        ToolCallRequest(
                            id='call:content:first',
                            name='content',
                            arguments={
                                'action': 'open',
                                'path': r'D:\repo\cli.tsx',
                                'start_line': 240,
                                'end_line': 303,
                            },
                        )
                    ],
                    finish_reason='tool_calls',
                    usage={'input_tokens': 10, 'output_tokens': 5, 'cache_hit_tokens': 0},
                )
            if self._turn in {2, 3, 4}:
                if self._turn > 2:
                    text = self._messages_text(kwargs)
                    assert '不要重复调用完全相同的只读/检索工具' in text
                return LLMResponse(
                    content='',
                    tool_calls=[
                        ToolCallRequest(
                            id=f'call:content:repeat:{self._turn}',
                            name='content',
                            arguments={
                                'action': 'open',
                                'path': r'D:\repo\cli.tsx',
                                'start_line': 240,
                                'end_line': 303,
                            },
                        )
                    ],
                    finish_reason='tool_calls',
                    usage={'input_tokens': 10, 'output_tokens': 5, 'cache_hit_tokens': 0},
                )
            raise AssertionError(f'unexpected extra turn: {self._turn}')

    service = MainRuntimeService(
        chat_backend=_Backend(),
        workspace_root=tmp_path,
        store_path=tmp_path / 'runtime.sqlite3',
        files_base_dir=tmp_path / 'tasks',
        artifact_dir=tmp_path / 'artifacts',
        governance_store_path=tmp_path / 'governance.sqlite3',
        execution_mode='embedded',
    )

    def _build_tools(task, node):
        return {
            'submit_final_result': SubmitFinalResultTool(service.node_runner._submit_final_result, node_kind=node.node_kind),
            'content': _StaticTool(
                'content',
                json.dumps(
                    {
                        'ref': 'artifact:artifact:test-content',
                        'resolved_ref': r'path:D:\repo\cli.tsx',
                        'summary': 'Use content_search/content_open with ref=artifact:artifact:test-content',
                    },
                    ensure_ascii=False,
                ),
            ),
        }

    service.node_runner._build_tools = _build_tools
    try:
        record = await service.create_task('repeated content repair regression', session_id='web:shared')
        await service.wait_for_task(record.task_id)
        task = service.store.get_task(record.task_id)
        assert task is not None
        assert task.status == 'in_progress'
        paused = service.get_node(record.root_node_id)
        assert paused is not None
        assert paused.is_paused is True
        assert paused.pause_reason == 'error'
        logs = service.log_service.list_task_error_logs(record.task_id)
        assert logs
        error_text = str(logs[0].error_text or '')
        assert 'repeated tool call detected: content' not in error_text
        assert 'read-only repair guidance' in error_text
        assert 'content' in error_text
        assert r'D:\repo\cli.tsx' in error_text
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_read_only_repeat_counts_are_tracked_per_signature(tmp_path: Path):
    class _Backend:
        def __init__(self) -> None:
            self._turn = 0

        @staticmethod
        def _messages_text(kwargs: dict[str, object]) -> str:
            messages = list(kwargs.get('messages') or [])
            return '\n'.join(
                content
                for item in messages
                if isinstance(item, dict)
                for content in [item.get('content')]
                if isinstance(content, str) and content.strip()
            )

        async def chat(self, **kwargs):
            self._turn += 1
            if self._turn == 1:
                return LLMResponse(
                    content='',
                    tool_calls=[
                        ToolCallRequest(
                            id='call:a:first',
                            name='content',
                            arguments={'action': 'open', 'path': r'D:\repo\cli.tsx', 'start_line': 1, 'end_line': 100},
                        )
                    ],
                    finish_reason='tool_calls',
                    usage={'input_tokens': 10, 'output_tokens': 5, 'cache_hit_tokens': 0},
                )
            if self._turn == 2:
                return LLMResponse(
                    content='',
                    tool_calls=[
                        ToolCallRequest(
                            id='call:a:repeat',
                            name='content',
                            arguments={'action': 'open', 'path': r'D:\repo\cli.tsx', 'start_line': 1, 'end_line': 100},
                        )
                    ],
                    finish_reason='tool_calls',
                    usage={'input_tokens': 10, 'output_tokens': 5, 'cache_hit_tokens': 0},
                )
            if self._turn == 3:
                text = self._messages_text(kwargs)
                assert '不要重复调用完全相同的只读/检索工具' in text
                return LLMResponse(
                    content='',
                    tool_calls=[
                        ToolCallRequest(
                            id='call:b:first',
                            name='content',
                            arguments={'action': 'open', 'path': r'D:\repo\cli.tsx', 'start_line': 101, 'end_line': 200},
                        )
                    ],
                    finish_reason='tool_calls',
                    usage={'input_tokens': 10, 'output_tokens': 5, 'cache_hit_tokens': 0},
                )
            if self._turn == 4:
                return LLMResponse(
                    content='',
                    tool_calls=[
                        ToolCallRequest(
                            id='call:b:repeat',
                            name='content',
                            arguments={'action': 'open', 'path': r'D:\repo\cli.tsx', 'start_line': 101, 'end_line': 200},
                        )
                    ],
                    finish_reason='tool_calls',
                    usage={'input_tokens': 10, 'output_tokens': 5, 'cache_hit_tokens': 0},
                )
            if self._turn == 5:
                text = self._messages_text(kwargs)
                assert '不要重复调用完全相同的只读/检索工具' in text
                return LLMResponse(
                    content='',
                    tool_calls=[
                        _final_result_call(
                            status='failed',
                            delivery_status='blocked',
                            summary='intentional stop',
                            answer='',
                            evidence=[],
                            remaining_work=['stop after verifying separate counts'],
                            blocking_reason='intentional stop',
                        )
                    ],
                    finish_reason='tool_calls',
                    usage={'input_tokens': 10, 'output_tokens': 5, 'cache_hit_tokens': 0},
                )
            if self._turn == 6:
                # blocked 核验节点：裁决阻塞成立，放行执行节点的主动失败
                text = self._messages_text(kwargs)
                assert 'failed+blocked' in text
                return LLMResponse(
                    content='',
                    tool_calls=[
                        _final_result_call(
                            status='success',
                            delivery_status='final',
                            summary='阻塞成立：签名计数验证完成后的主动停止属于合理终止',
                            answer='',
                            evidence=[{'kind': 'artifact', 'note': 'verified per-signature counts'}],
                            remaining_work=[],
                            blocking_reason='',
                        )
                    ],
                    finish_reason='tool_calls',
                    usage={'input_tokens': 10, 'output_tokens': 5, 'cache_hit_tokens': 0},
                )
            raise AssertionError(f'unexpected extra turn: {self._turn}')

    service = MainRuntimeService(
        chat_backend=_Backend(),
        workspace_root=tmp_path,
        store_path=tmp_path / 'runtime.sqlite3',
        files_base_dir=tmp_path / 'tasks',
        artifact_dir=tmp_path / 'artifacts',
        governance_store_path=tmp_path / 'governance.sqlite3',
        execution_mode='embedded',
    )

    def _build_tools(task, node):
        return {
            'submit_final_result': SubmitFinalResultTool(service.node_runner._submit_final_result, node_kind=node.node_kind),
            'content': _StaticTool(
                'content',
                json.dumps(
                    {
                        'ref': 'artifact:artifact:test-content',
                        'resolved_ref': r'path:D:\repo\cli.tsx',
                        'summary': 'Use content_search/content_open with ref=artifact:artifact:test-content',
                    },
                    ensure_ascii=False,
                ),
            ),
        }

    service.node_runner._build_tools = _build_tools
    try:
        record = await service.create_task('read-only signature counting regression', session_id='web:shared')
        await service.wait_for_task(record.task_id)
        task = service.store.get_task(record.task_id)
        assert task is not None
        assert task.status == 'failed'
        assert 'intentional stop' in str(task.failure_reason or '')
        assert 'read-only repair guidance' not in str(task.failure_reason or '')
        # failed+blocked 经过阻塞核验放行，失败记录带核验标记
        assert '[blocked核验]' in str(task.failure_reason or '')
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_same_turn_duplicate_tool_calls_execute_once_and_reuse_first_result(tmp_path: Path):
    class _CountingTool(Tool):
        def __init__(self, name: str) -> None:
            self._name = name
            self.execute_count = 0

        @property
        def name(self) -> str:
            return self._name

        @property
        def description(self) -> str:
            return f'{self._name} tool'

        @property
        def parameters(self) -> dict:
            return {'type': 'object', 'properties': {}}

        async def execute(self, **kwargs):
            self.execute_count += 1
            command = str(kwargs.get('command') or '')
            return json.dumps(
                {'status': 'success', 'exit_code': 0, 'head_preview': f'probe output for: {command}'},
                ensure_ascii=False,
            )

    class _Backend:
        def __init__(self) -> None:
            self._turn = 0

        @staticmethod
        def _messages_text(kwargs: dict[str, object]) -> str:
            messages = list(kwargs.get('messages') or [])
            parts: list[str] = []
            for item in messages:
                if not isinstance(item, dict):
                    continue
                content = item.get('content')
                if isinstance(content, str) and content.strip():
                    parts.append(content)
            return '\n'.join(parts)

        async def chat(self, **kwargs):
            self._turn += 1
            if self._turn == 1:
                # 同轮两个完全相同的 exec（不足连续 3 个，不触发跨轮 breaker 软拒绝），
                # 外加一个参数不同的 exec 验证非重复调用照常执行。
                return LLMResponse(
                    content='',
                    tool_calls=[
                        ToolCallRequest(
                            id='call:exec:dup:1',
                            name='exec',
                            arguments={
                                'command': "Write-Output 'same-turn dedup probe A'",
                                'working_dir': r'D:\NewProjects\G3KU',
                            },
                        ),
                        ToolCallRequest(
                            id='call:exec:dup:2',
                            name='exec',
                            arguments={
                                'command': "Write-Output 'same-turn dedup probe A'",
                                'working_dir': r'D:\NewProjects\G3KU',
                            },
                        ),
                        ToolCallRequest(
                            id='call:exec:other',
                            name='exec',
                            arguments={
                                'command': "Write-Output 'same-turn dedup probe B'",
                                'working_dir': r'D:\NewProjects\G3KU',
                            },
                        ),
                    ],
                    finish_reason='tool_calls',
                    usage={'input_tokens': 10, 'output_tokens': 5, 'cache_hit_tokens': 0},
                )
            if self._turn == 2:
                text = self._messages_text(kwargs)
                assert '"status": "reused"' in text
                assert 'duplicate_tool_call_in_same_turn' in text
                assert 'call:exec:dup:1' in text
                return LLMResponse(
                    content='',
                    tool_calls=[
                        _final_result_call(
                            status='success',
                            delivery_status='final',
                            summary='同轮相同调用已去重：首个执行一次，其余标记 reused',
                            answer='',
                            evidence=[{'kind': 'artifact', 'note': 'verified same-turn duplicate dedup'}],
                            remaining_work=[],
                            blocking_reason='',
                        )
                    ],
                    finish_reason='tool_calls',
                    usage={'input_tokens': 10, 'output_tokens': 5, 'cache_hit_tokens': 0},
                )
            raise AssertionError(f'unexpected extra turn: {self._turn}')

    exec_tool = _CountingTool('exec')

    service = MainRuntimeService(
        chat_backend=_Backend(),
        workspace_root=tmp_path,
        store_path=tmp_path / 'runtime.sqlite3',
        files_base_dir=tmp_path / 'tasks',
        artifact_dir=tmp_path / 'artifacts',
        governance_store_path=tmp_path / 'governance.sqlite3',
        execution_mode='embedded',
    )

    def _build_tools(task, node):
        return {
            'submit_final_result': SubmitFinalResultTool(service.node_runner._submit_final_result, node_kind=node.node_kind),
            'exec': exec_tool,
        }

    service.node_runner._build_tools = _build_tools
    try:
        record = await service.create_task('same-turn duplicate tool call dedup', session_id='web:shared')
        await service.wait_for_task(record.task_id)
        task = service.store.get_task(record.task_id)
        assert task is not None
        assert task.status == 'success'
        # 两个相同 exec 只执行一次 + 不同参数的 exec 照常执行 = 共 2 次
        assert exec_tool.execute_count == 2
    finally:
        await service.close()
