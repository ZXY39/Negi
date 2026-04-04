from __future__ import annotations

from types import SimpleNamespace

import pytest

from g3ku.runtime.frontdoor.checkpoint_inspection import (
    get_frontdoor_checkpoint,
    get_frontdoor_checkpoint_history,
)


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
