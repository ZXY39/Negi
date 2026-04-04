from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from g3ku.runtime.api import ceo_sessions
from g3ku.runtime.frontdoor.checkpoint_inspection import (
    build_frontdoor_replay_diagnostics,
    get_frontdoor_checkpoint,
    get_frontdoor_checkpoint_history,
    serialize_state_snapshot,
)
from main.api import admin_rest


def _build_checkpoint_app() -> FastAPI:
    app = FastAPI()
    app.include_router(ceo_sessions.router, prefix="/api")
    app.include_router(admin_rest.router, prefix="/api")
    return app


class _FakeCompiledGraph:
    def __init__(self) -> None:
        self.latest_calls: list[dict[str, object]] = []
        self.history_calls: list[dict[str, object]] = []

    async def aget_state(self, config, *, subgraphs: bool = False):
        self.latest_calls.append({"config": config, "subgraphs": subgraphs})
        return SimpleNamespace(
            values={"route_kind": "direct_reply", "messages": [{"role": "assistant", "content": "done"}]},
            next=(),
            config={"configurable": {"thread_id": "web:shared", "checkpoint_ns": "", "checkpoint_id": "cp-2"}},
            metadata={"step": 2, "source": "loop", "writes": {"finalize_turn": {"final_output": "done"}}},
            created_at="2026-04-04T12:00:00+08:00",
            parent_config={"configurable": {"thread_id": "web:shared", "checkpoint_ns": "", "checkpoint_id": "cp-1"}},
            tasks=(),
        )

    async def aget_state_history(self, config, *, filter=None, before=None, limit=None):
        self.history_calls.append(
            {
                "config": config,
                "filter": filter,
                "before": before,
                "limit": limit,
            }
        )
        for step, checkpoint_id in ((2, "cp-2"), (1, "cp-1")):
            yield SimpleNamespace(
                values={"messages": [], "route_kind": "direct_reply"},
                next=(),
                config={"configurable": {"thread_id": "web:shared", "checkpoint_ns": "", "checkpoint_id": checkpoint_id}},
                metadata={"step": step, "source": "loop"},
                created_at=f"2026-04-04T12:0{step}:00+08:00",
                parent_config=None,
                tasks=(),
            )


@pytest.mark.asyncio
async def test_get_frontdoor_checkpoint_serializes_latest_snapshot() -> None:
    graph = _FakeCompiledGraph()

    async def _ready() -> None:
        return None

    loop = SimpleNamespace(_ensure_checkpointer_ready=_ready, multi_agent_runner=SimpleNamespace(_get_compiled_graph=lambda: graph))

    item = await get_frontdoor_checkpoint(loop, session_id="web:shared", checkpoint_id="cp-2")

    assert item["checkpoint_id"] == "cp-2"
    assert item["thread_id"] == "web:shared"
    assert item["metadata"]["step"] == 2
    assert item["parent_checkpoint_id"] == "cp-1"
    assert item["values"]["route_kind"] == "direct_reply"
    assert graph.latest_calls == [
        {
            "config": {"configurable": {"thread_id": "web:shared", "checkpoint_id": "cp-2"}},
            "subgraphs": False,
        }
    ]


@pytest.mark.asyncio
async def test_get_frontdoor_checkpoint_history_serializes_reverse_chronological_history() -> None:
    graph = _FakeCompiledGraph()

    async def _ready() -> None:
        return None

    loop = SimpleNamespace(_ensure_checkpointer_ready=_ready, multi_agent_runner=SimpleNamespace(_get_compiled_graph=lambda: graph))

    items = await get_frontdoor_checkpoint_history(loop, session_id="web:shared", limit=2)

    assert [item["checkpoint_id"] for item in items] == ["cp-2", "cp-1"]
    assert [item["metadata"]["step"] for item in items] == [2, 1]
    assert graph.history_calls == [
        {
            "config": {"configurable": {"thread_id": "web:shared"}},
            "filter": None,
            "before": None,
            "limit": 2,
        }
    ]


@pytest.mark.asyncio
async def test_get_frontdoor_checkpoint_history_clamps_zero_limit_to_one() -> None:
    graph = _FakeCompiledGraph()

    async def _ready() -> None:
        return None

    loop = SimpleNamespace(_ensure_checkpointer_ready=_ready, multi_agent_runner=SimpleNamespace(_get_compiled_graph=lambda: graph))

    items = await get_frontdoor_checkpoint_history(loop, session_id="web:shared", limit=0)

    assert [item["checkpoint_id"] for item in items] == ["cp-2", "cp-1"]
    assert graph.history_calls == [
        {
            "config": {"configurable": {"thread_id": "web:shared"}},
            "filter": None,
            "before": None,
            "limit": 1,
        }
    ]


def test_serialize_state_snapshot_coerces_nested_complex_objects_to_json_safe_values() -> None:
    class _OpaqueValue:
        def __str__(self) -> str:
            return "opaque-value"

    class _OpaqueState:
        def __str__(self) -> str:
            return "opaque-state"

    class _OpaqueInterrupt:
        def __str__(self) -> str:
            return "opaque-interrupt"

    snapshot = SimpleNamespace(
        values={
            "route_kind": "direct_reply",
            "result": _OpaqueValue(),
            "nested": {"items": [1, _OpaqueValue()]},
        },
        next=("finalize_turn", _OpaqueValue()),
        config={"configurable": {"thread_id": "web:shared", "checkpoint_ns": "", "checkpoint_id": "cp-9"}},
        metadata={
            "step": 9,
            "writes": {"finalize_turn": {"result": _OpaqueValue()}},
        },
        created_at="2026-04-04T12:09:00+08:00",
        parent_config={"configurable": {"thread_id": "web:shared", "checkpoint_ns": "", "checkpoint_id": "cp-8"}},
        tasks=(
            SimpleNamespace(
                id="task-1",
                name="await_input",
                error="",
                state=_OpaqueState(),
                interrupts=(SimpleNamespace(id="interrupt-1", value={"payload": _OpaqueInterrupt()}),),
            ),
        ),
    )

    item = serialize_state_snapshot(snapshot)

    assert item["values"] == {
        "route_kind": "direct_reply",
        "result": "opaque-value",
        "nested": {"items": [1, "opaque-value"]},
    }
    assert item["next"] == ["finalize_turn", "opaque-value"]
    assert item["metadata"] == {
        "step": 9,
        "writes": {"finalize_turn": {"result": "opaque-value"}},
    }
    assert item["tasks"] == [
        {
            "id": "task-1",
            "name": "await_input",
            "error": "",
            "interrupts": [{"id": "interrupt-1", "value": {"payload": "opaque-interrupt"}}],
            "state": "opaque-state",
        }
    ]
    json.dumps(item)


def test_serialize_state_snapshot_preserves_structured_task_state_as_json_safe_data() -> None:
    class _OpaqueLeaf:
        def __str__(self) -> str:
            return "opaque-leaf"

    structured_state = SimpleNamespace(
        values={"child": SimpleNamespace(node="planner", payload={"result": _OpaqueLeaf()})},
        next=("finalize_turn",),
        metadata={"step": 3, "source": "subgraph"},
    )
    snapshot = SimpleNamespace(
        values={"route_kind": "direct_reply"},
        next=(),
        config={"configurable": {"thread_id": "web:shared", "checkpoint_ns": "", "checkpoint_id": "cp-10"}},
        metadata={"step": 10},
        created_at="2026-04-04T12:10:00+08:00",
        parent_config=None,
        tasks=(
            SimpleNamespace(
                id="task-subgraph",
                name="subgraph",
                error="",
                state=structured_state,
                interrupts=(),
            ),
        ),
    )

    item = serialize_state_snapshot(snapshot)

    assert item["tasks"] == [
        {
            "id": "task-subgraph",
            "name": "subgraph",
            "error": "",
            "interrupts": [],
            "state": {
                "values": {
                    "child": {
                        "node": "planner",
                        "payload": {"result": "opaque-leaf"},
                    }
                },
                "next": ["finalize_turn"],
                "metadata": {"step": 3, "source": "subgraph"},
            },
        }
    ]
    json.dumps(item)


def test_build_frontdoor_replay_diagnostics_preserves_available_checkpoint_config() -> None:
    snapshot = {
        "thread_id": "web:shared",
        "checkpoint_id": "cp-10",
        "checkpoint_ns": "subgraph:planner",
        "parent_checkpoint_id": "cp-9",
        "metadata": {"step": 10, "source": "loop"},
        "next": ["finalize_turn"],
        "has_interrupts": False,
    }

    item = build_frontdoor_replay_diagnostics(snapshot)

    assert item == {
        "thread_id": "web:shared",
        "checkpoint_id": "cp-10",
        "parent_checkpoint_id": "cp-9",
        "step": 10,
        "source": "loop",
        "next": ["finalize_turn"],
        "has_interrupts": False,
        "replay_config": {
            "configurable": {
                "thread_id": "web:shared",
                "checkpoint_id": "cp-10",
                "checkpoint_ns": "subgraph:planner",
            }
        },
    }


def test_build_frontdoor_replay_diagnostics_keeps_replay_pointer_and_step() -> None:
    snapshot = {
        "thread_id": "web:shared",
        "checkpoint_id": "cp-2",
        "parent_checkpoint_id": "cp-1",
        "metadata": {"step": 2, "source": "loop"},
        "next": ["review_tool_calls"],
        "has_interrupts": True,
    }

    item = build_frontdoor_replay_diagnostics(snapshot)

    assert item["step"] == 2
    assert item["has_interrupts"] is True
    assert item["replay_config"] == {
        "configurable": {"thread_id": "web:shared", "checkpoint_id": "cp-2"}
    }


def test_build_frontdoor_replay_diagnostics_surfaces_prompt_cache_diagnostics() -> None:
    snapshot = {
        "thread_id": "web:shared",
        "checkpoint_id": "cp-2",
        "metadata": {"step": 2, "source": "loop"},
        "next": [],
        "has_interrupts": False,
        "values": {
            "prompt_cache_diagnostics": {
                "stable_prompt_signature": "stable-abc",
                "tool_signature_count": 2,
                "tool_signature_hash": "tool-hash",
                "overlay_present": True,
                "overlay_section_count": 3,
                "overlay_text_hash": "overlay-hash",
            }
        },
    }

    item = build_frontdoor_replay_diagnostics(snapshot)

    assert item["prompt_cache_diagnostics"] == {
        "stable_prompt_signature": "stable-abc",
        "tool_signature_count": 2,
        "tool_signature_hash": "tool-hash",
        "overlay_present": True,
        "overlay_section_count": 3,
        "overlay_text_hash": "overlay-hash",
    }


def test_ceo_session_checkpoint_history_endpoint_returns_serialized_history(monkeypatch, tmp_path) -> None:
    app = _build_checkpoint_app()
    client = TestClient(app)

    monkeypatch.setattr(
        ceo_sessions,
        "_sessions",
        lambda: (
            SimpleNamespace(),
            SimpleNamespace(
                get_path=lambda _key: tmp_path / "web.shared.json",
                get_or_create=lambda key: SimpleNamespace(key=key, metadata={}),
            ),
            SimpleNamespace(get=lambda _key: None),
            SimpleNamespace(),
        ),
    )
    monkeypatch.setattr(
        ceo_sessions,
        "_assert_known_session",
        lambda _manager, session_id: SimpleNamespace(key=session_id, metadata={}),
    )
    monkeypatch.setattr(
        ceo_sessions,
        "get_frontdoor_checkpoint_history",
        lambda loop, *, session_id, limit=20, before_checkpoint_id=None, metadata_filter=None: [
            {
                "thread_id": session_id,
                "checkpoint_id": "cp-2",
                "parent_checkpoint_id": "cp-1",
                "metadata": {"step": 2, "source": "loop"},
                "next": [],
                "values": {"route_kind": "direct_reply"},
                "tasks": [],
                "has_interrupts": False,
            }
        ],
    )

    response = client.get("/api/ceo/sessions/web:ceo-demo/checkpoint-history?limit=1")

    assert response.status_code == 200
    body = response.json()
    assert body["session_id"] == "web:ceo-demo"
    assert body["items"][0]["checkpoint_id"] == "cp-2"
    assert body["items"][0]["metadata"]["step"] == 2


def test_ceo_session_checkpoint_endpoint_returns_serialized_snapshot(monkeypatch, tmp_path) -> None:
    app = _build_checkpoint_app()
    client = TestClient(app)

    monkeypatch.setattr(
        ceo_sessions,
        "_sessions",
        lambda: (
            SimpleNamespace(),
            SimpleNamespace(
                get_path=lambda _key: tmp_path / "web.shared.json",
                get_or_create=lambda key: SimpleNamespace(key=key, metadata={}),
            ),
            SimpleNamespace(get=lambda _key: None),
            SimpleNamespace(),
        ),
    )
    monkeypatch.setattr(
        ceo_sessions,
        "_assert_known_session",
        lambda _manager, session_id: SimpleNamespace(key=session_id, metadata={}),
    )
    monkeypatch.setattr(
        ceo_sessions,
        "get_frontdoor_checkpoint",
        lambda loop, *, session_id, checkpoint_id=None, subgraphs=False: {
            "thread_id": session_id,
            "checkpoint_id": checkpoint_id,
            "parent_checkpoint_id": "cp-1",
            "metadata": {"step": 2, "source": "loop"},
            "next": [],
            "values": {"route_kind": "direct_reply"},
            "tasks": [],
            "has_interrupts": False,
        },
    )

    response = client.get("/api/ceo/sessions/web:ceo-demo/checkpoint?checkpoint_id=cp-2")

    assert response.status_code == 200
    body = response.json()
    assert body["session_id"] == "web:ceo-demo"
    assert body["item"]["checkpoint_id"] == "cp-2"
    assert body["item"]["parent_checkpoint_id"] == "cp-1"


def test_admin_checkpoint_history_endpoint_returns_serialized_history(monkeypatch) -> None:
    app = _build_checkpoint_app()
    client = TestClient(app)

    monkeypatch.setattr(admin_rest, "get_agent", lambda: SimpleNamespace(multi_agent_runner=SimpleNamespace()))
    monkeypatch.setattr(
        admin_rest,
        "get_frontdoor_checkpoint_history",
        lambda loop, *, session_id, limit=20, before_checkpoint_id=None, metadata_filter=None: [
            {
                "thread_id": session_id,
                "checkpoint_id": "cp-2",
                "parent_checkpoint_id": "cp-1",
                "metadata": {"step": 2, "source": "loop"},
                "next": [],
                "values": {"route_kind": "direct_reply"},
                "tasks": [],
                "has_interrupts": False,
            }
        ],
    )

    response = client.get("/api/ceo/checkpoints/web:ceo-demo/history?limit=1")

    assert response.status_code == 200
    body = response.json()
    assert body["items"][0]["checkpoint_id"] == "cp-2"
    assert body["items"][0]["metadata"]["source"] == "loop"


def test_admin_checkpoint_endpoint_returns_404_for_missing_checkpoint(monkeypatch) -> None:
    app = _build_checkpoint_app()
    client = TestClient(app)

    monkeypatch.setattr(admin_rest, "get_agent", lambda: SimpleNamespace(multi_agent_runner=SimpleNamespace()))
    monkeypatch.setattr(
        admin_rest,
        "get_frontdoor_checkpoint",
        lambda loop, *, session_id, checkpoint_id=None, subgraphs=False: None,
    )

    response = client.get("/api/ceo/checkpoints/web:ceo-demo?checkpoint_id=cp-missing")

    assert response.status_code == 404
    assert response.json()["detail"] == "checkpoint_not_found"


def test_admin_replay_diagnostics_endpoint_returns_replay_ready_payload(monkeypatch) -> None:
    app = _build_checkpoint_app()
    client = TestClient(app)

    monkeypatch.setattr(admin_rest, "get_agent", lambda: SimpleNamespace(multi_agent_runner=SimpleNamespace()))
    monkeypatch.setattr(
        admin_rest,
        "get_frontdoor_checkpoint",
        lambda loop, *, session_id, checkpoint_id=None, subgraphs=False: {
            "thread_id": session_id,
            "checkpoint_id": checkpoint_id,
            "parent_checkpoint_id": "cp-1",
            "metadata": {"step": 2, "source": "loop"},
            "next": ["finalize_turn"],
            "values": {"route_kind": "direct_reply"},
            "tasks": [],
            "has_interrupts": False,
        },
    )

    response = client.get("/api/ceo/checkpoints/web:ceo-demo/replay-diagnostics?checkpoint_id=cp-2")

    assert response.status_code == 200
    body = response.json()
    assert body["item"]["checkpoint_id"] == "cp-2"
    assert body["item"]["replay_config"] == {
        "configurable": {"thread_id": "web:ceo-demo", "checkpoint_id": "cp-2"}
    }


@pytest.mark.parametrize(
    ("path", "expected_status", "error"),
    [
        ("/api/ceo/checkpoints/web:ceo-demo?checkpoint_id=cp-2", 503, RuntimeError("No model configured for role 'ceo'.")),
        ("/api/ceo/checkpoints/web:ceo-demo/history?limit=1", 503, RuntimeError("No model configured for role 'ceo'.")),
        ("/api/ceo/checkpoints/web:ceo-demo/replay-diagnostics?checkpoint_id=cp-2", 503, RuntimeError("No model configured for role 'ceo'.")),
        ("/api/ceo/checkpoints/web:ceo-demo?checkpoint_id=cp-2", 423, RuntimeError("project is locked")),
        ("/api/ceo/checkpoints/web:ceo-demo/history?limit=1", 423, RuntimeError("project is locked")),
        ("/api/ceo/checkpoints/web:ceo-demo/replay-diagnostics?checkpoint_id=cp-2", 423, RuntimeError("project is locked")),
    ],
)
def test_admin_checkpoint_endpoints_translate_unavailable_runtime_errors(monkeypatch, path: str, expected_status: int, error: RuntimeError) -> None:
    app = _build_checkpoint_app()
    client = TestClient(app, raise_server_exceptions=False)

    def _raise():
        raise error

    monkeypatch.setattr(admin_rest, "get_agent", _raise)

    response = client.get(path)

    assert response.status_code == expected_status
    expected_detail = "no_model_configured" if expected_status == 503 else "project_locked"
    assert response.json()["detail"] == expected_detail
