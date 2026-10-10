"""MCP 门面这轮改动的新用例（每条都在未改动的树上先红过）。

覆盖四件事：

1. 会话列表 own 车道与 all 同形，且**读列表不再伪造 created_at**；
2. 回合终态带机器读的 `task_ids`，并且派发句柄不被存在性闸门吃掉；
3. `negi_chat(wait_seconds=0)` 提交即返回，不开 SSE 流；
4. `negi_delete_conversation` 只收本命名空间短名；节点账本白名单留住分块读数。
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pytest

from g3ku.mcp_gateway.server import build_mcp_server
from g3ku.runtime.frontdoor import _ceo_create_agent_impl as create_agent_impl
from g3ku.runtime.web_ceo_sessions import _external_channel_row


async def _call(server, name: str, args: dict) -> dict:
    from g3ku.mcp_gateway import server as server_module  # noqa: F401  (keep import order stable)

    tools = server._tool_manager.list_tools() if hasattr(server, "_tool_manager") else None
    if tools is None:  # pragma: no cover - FastMCP 版本兜底
        raise AssertionError("FastMCP 没有 _tool_manager，测试需改用其它取法")
    handler = {item.name: item for item in tools}[name].fn
    return await handler(**args)


class _ListingManager:
    """只够 `_external_channel_row` 用的会话管理器桩。

    `get_or_create` 一旦被动到就抛：列表读数是只读的，不该顺手造出一个
    `created_at=now` 的空会话（那是原来伪造时间的入口）。
    """

    def __init__(self, *, has_transcript: bool) -> None:
        self._has = has_transcript

    def has_transcript(self, key: str) -> bool:
        return self._has

    def get_or_create(self, key: str):  # pragma: no cover - 有转录时才走
        raise AssertionError("无转录会话不得被 get_or_create 实例化")


def test_external_row_without_transcript_keeps_registry_created_at():
    entry = SimpleNamespace(
        external_key="probe", title="", created_at="2026-10-09T06:36:00.260596", session_key="ext:qoder:abc"
    )
    row = _external_channel_row(
        _ListingManager(has_transcript=False),
        "ext:qoder:abc",
        entry=entry,
        active_session_id="",
        is_running=False,
    )
    assert row is not None
    assert row["created_at"] == "2026-10-09T06:36:00.260596"
    assert row["message_count"] == 0
    assert row["preview_text"] == ""
    assert row["updated_at"] == ""


def test_external_row_with_transcript_still_reads_activity():
    session = SimpleNamespace(created_at=datetime(2026, 10, 1, 8, 0, 0))
    manager = SimpleNamespace(
        has_transcript=lambda key: True,
        get_or_create=lambda key: session,
    )
    entry = SimpleNamespace(external_key="probe", title="", created_at="2026-01-01T00:00:00", session_key="ext:qoder:abc")
    row = _external_channel_row(manager, "ext:qoder:abc", entry=entry, active_session_id="", is_running=False)
    assert row["created_at"] == "2026-10-01T08:00:00"


@pytest.mark.asyncio
async def test_dispatched_task_ids_survive_the_existence_gate():
    """闸门只该管"能不能对模型说这号存在"，不该把派发句柄本身吃掉。"""
    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(
        loop=SimpleNamespace(main_task_service=SimpleNamespace(get_task=lambda task_id: None))
    )
    result = await runner._postprocess_completed_tool_cycle(
        state={
            "tool_call_payloads": [{"id": "call-1", "name": "create_async_task", "arguments": {"task": "demo"}}],
            "messages": [
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {"id": "call-1", "type": "function", "function": {"name": "create_async_task", "arguments": "{}"}}
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
    assert result["dispatched_task_ids"] == ["task:demo-123"]
    assert result["verified_task_ids"] == []


def test_node_ledger_keeps_chunk_readings():
    from main.runtime.react_loop import ReActToolLoop

    fields = ReActToolLoop._node_send_compaction_diagnostics(
        {
            "applied": True,
            "mode": "llm",
            "compression_mode": "llm_chunked",
            "chunk_count": 2,
            "merge_pass_applied": True,
            "chunk_failed_count": 1,
            "chunk_failed_indexes": [1],
            "estimate_source": "usage_plus_delta",
        }
    )
    assert fields["compression_mode"] == "llm_chunked"
    assert fields["chunk_count"] == 2
    assert fields["chunk_failed_count"] == 1
    assert fields["chunk_failed_indexes"] == [1]
    assert fields["merge_pass_applied"] is True

    import importlib

    log_service = importlib.import_module("main.monitoring.log_service")
    carrier = next(
        obj
        for obj in vars(log_service).values()
        if isinstance(obj, type) and hasattr(obj, "_send_diagnostics_fields")
    )
    row = carrier._send_diagnostics_fields(fields)
    assert row["chunk_count"] == 2
    assert row["chunk_failed_indexes"] == [1]
    assert row["compression_mode"] == "llm_chunked"


class _NoWaitClient:
    """够 `negi_chat` / `negi_delete_conversation` 用的网关桩：SSE 一旦开就记一笔。"""

    def __init__(self) -> None:
        self.stream_opens = 0
        self.deleted: list[tuple[str, bool]] = []

    async def resolve_session(self, conversation: str) -> str:
        return f"ext:probe:{conversation}"

    async def ensure_session(self, conversation: str, *, title: str | None = None) -> str:
        return f"ext:probe:{conversation}"

    def last_seq(self, session_id: str) -> int:
        return 0

    async def send_message(self, session_id: str, text: str, **kwargs) -> dict:
        return {"status": "started", "turn_id": "turn-1"}

    def event_stream(self, session_id: str, *, last_seq: int = 0):
        self.stream_opens += 1

        class _Ctx:
            async def __aenter__(self):
                async def _events():
                    if False:
                        yield {}

                return _events()

            async def __aexit__(self, *exc) -> bool:
                return False

        return _Ctx()

    async def delete_session(self, session_id: str, *, purge: bool = False) -> dict:
        self.deleted.append((session_id, purge))
        return {"ok": True, "deleted": True, "registry_forgotten": purge, "transcript_removed": True}

    def forget_session(self, conversation: str) -> None:
        return None


@pytest.mark.asyncio
async def test_chat_with_zero_wait_submits_without_opening_stream():
    client = _NoWaitClient()
    result = await _call(build_mcp_server(client), "negi_chat", {"conversation": "c1", "message": "派活", "wait_seconds": 0})
    assert result["status"] == "submitted"
    assert result["turn_id"] == "turn-1"
    assert result["task_ids"] == []
    assert client.stream_opens == 0


@pytest.mark.asyncio
async def test_delete_conversation_refuses_full_keys_and_purges_own_names():
    client = _NoWaitClient()
    server = build_mcp_server(client)
    refused = await _call(server, "negi_delete_conversation", {"conversation": "web:ceo-1"})
    assert refused == {"ok": False, "error": "own_namespace_only", "conversation": "web:ceo-1"}
    deleted = await _call(server, "negi_delete_conversation", {"conversation": "c1"})
    assert deleted["deleted"] is True
    assert deleted["transcript_removed"] is True
    assert client.deleted == [("ext:probe:c1", True)]
