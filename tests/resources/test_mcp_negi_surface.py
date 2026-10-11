"""MCP 门面这轮改动的新用例（每条都在未改动的树上先红过）。

覆盖四件事：

1. 会话列表 own 车道与 all 同形，且**读列表不再伪造 created_at**；
2. 回合终态带机器读的 `task_ids`，并且派发句柄不被存在性闸门吃掉；
3. `negi_chat(wait_seconds=0)` 提交即返回，不开 SSE 流；
4. `negi_delete_conversation` 只收本命名空间短名；节点账本白名单留住分块读数。
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime
from types import SimpleNamespace

import httpx
import pytest

from g3ku.mcp_gateway.client import G3kuMcpClient
from g3ku.mcp_gateway.server import _progress_hook, build_mcp_server
from g3ku.runtime.external_events import SessionEventHub
from g3ku.runtime.frontdoor import _ceo_create_agent_impl as create_agent_impl
from g3ku.runtime.web_ceo_sessions import _external_channel_row

# 任务跳次 `created_at` 的真实形状：`main/monitoring/log_service.py::_now_iso()` 用
# `datetime.now().astimezone().isoformat(timespec='microseconds')`，带本机偏移。
HOP_AT_WIRE_SHAPE = "2026-10-11T00:12:34.567890+08:00"


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


class _StreamClient(G3kuMcpClient):
    """只替掉 HTTP 面，让真实 `wait_for_reply` 参与测试（按回合定址的判据在它里面）。"""

    def __init__(self, events: list[dict]) -> None:
        super().__init__("http://127.0.0.1:1/api/v1", "stub-token")
        self._events = list(events)

    async def resolve_session(self, conversation: str) -> str:
        return "ext:probe:abc"

    def last_seq(self, session_id: str) -> int:
        return 0

    @asynccontextmanager
    async def event_stream(self, session_id: str, *, last_seq: int = 0):
        events = list(self._events)

        async def _iter():
            for item in events:
                yield item

        yield _iter()


@pytest.mark.asyncio
async def test_get_reply_can_target_a_specific_turn_id():
    """旧形状只能"取下一条未消费的终稿"，两条终稿并存时会答错题。"""
    events = [
        {"seq": 1, "ts": "2026-10-11T00:00:01", "type": "reply.final", "turn_id": "other", "text": "上一问的答复"},
        {"seq": 2, "ts": "2026-10-11T00:00:02", "type": "reply.final", "turn_id": "mine", "text": "我要的那一份"},
    ]
    result = await _call(
        build_mcp_server(_StreamClient(events)),
        "negi_get_reply",
        {"conversation": "c1", "wait_seconds": 5, "turn_id": "mine"},
    )
    assert result["reply"] == "我要的那一份", result
    assert result["turn_id"] == "mine"
    assert result["answered_seq"] == 2


@pytest.mark.asyncio
async def test_progress_events_reach_the_caller_without_changing_the_reply():
    events = [
        {"seq": 1, "ts": "2026-10-11T00:00:01", "type": "progress", "kind": "tool", "text": "🔧 sql_scan"},
        {"seq": 2, "ts": "2026-10-11T00:00:02", "type": "reply.final", "turn_id": "mine", "text": "答复"},
    ]
    seen: list = []

    class _Ctx:
        async def report_progress(self, *, progress=None, message=None, total=None):
            seen.append((progress, message))

    client = _StreamClient(events)
    async with client.event_stream("ext:probe:abc") as stream:
        outcome = await client.wait_for_reply(
            stream, turn_id="mine", queued=False, timeout=5, on_event=_progress_hook(_Ctx())
        )
    assert outcome["text"] == "答复"
    assert seen and seen[0][1].startswith("tool:"), seen


@pytest.mark.asyncio
async def test_a_client_that_cannot_receive_progress_still_gets_its_reply():
    class _Boom:
        async def report_progress(self, **kwargs):
            raise RuntimeError("client does not support notifications/progress")

    events = [
        {"seq": 1, "ts": "x", "type": "progress", "kind": "tool", "text": "x"},
        {"seq": 2, "ts": "x", "type": "reply.final", "turn_id": "mine", "text": "still arrives"},
    ]
    client = _StreamClient(events)
    async with client.event_stream("ext:probe:abc") as stream:
        outcome = await client.wait_for_reply(
            stream, turn_id="mine", queued=False, timeout=5, on_event=_progress_hook(_Boom())
        )
    assert outcome["kind"] == "reply" and outcome["text"] == "still arrives"


@pytest.mark.asyncio
async def test_get_turn_reads_the_stored_reply_and_reports_a_missing_turn():
    class _TurnClient(G3kuMcpClient):
        def __init__(self, payload=None, status=None):
            super().__init__("http://127.0.0.1:1/api/v1", "stub-token")
            self._payload = payload
            self._status = status

        async def resolve_session(self, conversation: str) -> str:
            return "ext:probe:abc"

        async def get_turn(self, session_id: str, turn_id: str) -> dict:
            if self._status:
                response = httpx.Response(self._status, request=httpx.Request("GET", "http://127.0.0.1:1/x"))
                raise httpx.HTTPStatusError("nope", request=response.request, response=response)
            return dict(self._payload)

    found = await _call(
        build_mcp_server(
            _TurnClient(
                payload={
                    "text": "原文 73,328",
                    "created_at": "2026-10-10T22:38:05",
                    "usage": {"input_tokens": 20406},
                    "task_ids": ["task:f031c42b53e8"],
                }
            )
        ),
        "negi_get_turn",
        {"conversation": "c1", "turn_id": "a10e784b"},
    )
    assert found["reply"] == "原文 73,328"
    assert found["task_ids"] == ["task:f031c42b53e8"]

    missing = await _call(
        build_mcp_server(_TurnClient(status=404)), "negi_get_turn", {"conversation": "c1", "turn_id": "nope"}
    )
    assert missing["ok"] is False and missing["error"] == "turn_not_found"


@pytest.mark.asyncio
async def test_session_status_exposes_time_and_activity_not_only_a_cursor():
    class _StateClient(G3kuMcpClient):
        async def resolve_session(self, conversation: str) -> str:
            return "ext:probe:abc"

        async def session_state(self, session_id: str) -> dict:
            return {
                "running": True,
                "queued_follow_ups": 0,
                "inflight_turn_id": "t1",
                "started_at": "2026-10-11T00:02:09",
                "elapsed_seconds": 900,
                "last_activity_at": "2026-10-11T00:17:00",
                "silence_seconds": 40,
                "last_event_seq": 856,
                "last_activity_kind": "progress",
                "last_activity_text": "🔧 sql_scan",
                "last_error": None,
            }

        def last_seq(self, session_id: str) -> int:
            return 856

    result = await _call(build_mcp_server(_StateClient()), "negi_session_status", {"conversation": "c1"})
    assert result["silence_seconds"] == 40
    assert result["elapsed_seconds"] == 900
    assert result["last_activity_text"] == "🔧 sql_scan"
    assert result["last_seq"] == 856


def test_hub_tail_event_exposes_the_time_of_last_activity():
    hub = SessionEventHub("ext:probe:abc", 64)
    hub.publish("progress", turn_id="t1", kind="tool", text="🔧 exec")
    final = hub.publish("reply.final", turn_id="t1", text="答复")
    tail = hub.tail_event()
    assert tail["seq"] == final["seq"] == 2
    assert tail["ts"]
    # 读活动不该消耗或推进任何东西：同一份缓冲区仍能整段回放
    assert len(hub.replay(0)) == 2
    assert hub.tail_event() == tail


@pytest.mark.asyncio
async def test_reply_carries_the_transcript_turn_id_for_re_read():
    """两套 id 各管各的：`turn_id` 定址外部回合，`transcript_turn_id` 才是转录行的键。"""
    events = [
        {
            "seq": 3,
            "ts": "x",
            "type": "reply.final",
            "turn_id": "rec-1",
            "transcript_turn_id": "7eefb1004a5b4fdf",
            "text": "task=a9862e6309a6",
            "task_ids": ["task:a9862e6309a6"],
        }
    ]
    result = await _call(
        build_mcp_server(_StreamClient(events)),
        "negi_get_reply",
        {"conversation": "c1", "wait_seconds": 5, "turn_id": "rec-1"},
    )
    assert result["turn_id"] == "rec-1"
    assert result["transcript_turn_id"] == "7eefb1004a5b4fdf"
    assert result["task_ids"] == ["task:a9862e6309a6"]


def test_task_activity_reports_unknown_separately_from_none():
    from g3ku.runtime.api.external_v1 import _session_task_activity, _task_hop_ceiling

    assert _session_task_activity(None, "ext:qoder:abc") is None
    assert _session_task_activity(SimpleNamespace(), "ext:qoder:abc") is None

    service = SimpleNamespace(
        list_unfinished_tasks_for_session=lambda key: [SimpleNamespace(task_id="task:a", status="in_progress")],
        store=SimpleNamespace(
            list_task_model_calls=lambda task_id, limit=None: [{"created_at": HOP_AT_WIRE_SHAPE}],
            count_task_model_calls=lambda task_id: 245,
        ),
    )
    activity = _session_task_activity(SimpleNamespace(main_task_service=service), "ext:qoder:abc")
    assert activity["tasks_running"] == 1
    assert activity["tasks"][0]["model_calls"] == 245
    assert _task_hop_ceiling(activity) == HOP_AT_WIRE_SHAPE


def test_task_hop_age_handles_the_timezone_aware_shape_the_store_actually_returns():
    """/state 里三种时间源形状不一样：任务跳次带 `+08:00`，会话事件与回合是 naive 串。

    桩里原来喂的是 naive 跳次串，所以这条 500 只有实盘能抓到——会话有未完成任务、
    且那任务已经跳过一次时 `GET /state` 连回两次
    `TypeError: can't subtract offset-naive and offset-aware datetimes`，
    MCP 侧整包 `{ok:false}`：越是"节点在跑、想看它活着没"的时候越读不到。
    """
    from g3ku.runtime.api.external_v1 import _age_from_iso

    now = datetime.fromisoformat("2026-10-11T10:12:34.567890+08:00")
    assert _age_from_iso("2026-10-11T10:02:34.567890+08:00", now=now) == 600
    assert _age_from_iso("2026-10-11T10:02:34.567890", now=now) == 600
    assert _age_from_iso("", now=now) is None
    assert _age_from_iso("not-a-time", now=now) is None


def test_latest_model_text_is_truncated_and_reports_the_real_length():
    from g3ku.runtime.api.external_v1 import summarize_external_session_activity

    long_text = "结" * 900
    events = [
        {"seq": 1, "type": "reply.delta", "text": "较早的一段输出"},
        {"seq": 2, "type": "reply.delta", "text": long_text},
    ]
    summary = summarize_external_session_activity(events)
    assert summary["latest_model_text"] == "结" * 300
    assert summary["latest_model_text_total_chars"] == 900


def test_recent_tool_calls_come_from_structured_names_newest_first():
    from g3ku.runtime.api.external_v1 import summarize_external_session_activity

    events = [
        {"seq": 1, "type": "progress", "kind": "tool", "text": "🔧 exec started", "tool_name": "exec"},
        {"seq": 2, "type": "progress", "kind": "milestone", "text": "阶段推进", "tool_name": "not_a_tool"},
        {"seq": 3, "type": "progress", "kind": "tool", "text": "🔧 content_search started", "tool_name": "content_search"},
        {"seq": 4, "type": "progress", "kind": "tool", "text": "🔧 exec started", "tool_name": "exec"},
    ]
    summary = summarize_external_session_activity(events)
    assert summary["recent_tool_calls"] == ["exec", "content_search"]


def test_stage_projection_gives_status_counts_and_goal_prefix_only():
    from g3ku.runtime.api.external_v1 import summarize_external_session_stages

    ledger = {
        "stages": [
            {
                "stage_index": index,
                "status": "completed",
                "stage_kind": "work",
                "stage_goal": "核查" + ("细" * 400),
                "tool_rounds_used": index,
                "tool_round_budget": 12,
                "rounds": [{}, {}],
                "completed_stage_summary": "不该出现的摘要",
                "key_refs": [{"secret": "不该出现"}],
            }
            for index in range(1, 6)
        ]
    }
    stages = summarize_external_session_stages(ledger)
    assert [item["stage_index"] for item in stages] == [3, 4, 5]
    assert stages[0]["goal"].startswith("核查") and len(stages[0]["goal"]) == 120
    assert stages[0]["round_count"] == 2 and stages[0]["tool_round_budget"] == 12
    joined = __import__("json").dumps(stages, ensure_ascii=False)
    assert "不该出现" not in joined
    # 账本读不到时是 None（"不知道"），不是空列表——空列表会被读成"这个会话没有阶段"
    assert summarize_external_session_stages(None) is None
    assert summarize_external_session_stages({"stages": []}) == []
