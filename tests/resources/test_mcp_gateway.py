"""MCP 网关测试：工具编排（FakeClient）、传输层（httpx.MockTransport SSE）、
协议层（mcp.shared.memory in-memory client/server）。

工具级断言用公开的 ``FastMCP.call_tool``；``_call`` 助手兼容 structured
dict / content-block 两种返回形态（SDK 1.27 对 dict[str, Any] 注解的
graceful fallback，见设计注记 R8）。
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest

from g3ku.mcp_gateway.client import G3kuMcpClient
from g3ku.mcp_gateway.server import build_mcp_server

TOOL_NAMES = {
    "g3ku_chat",
    "g3ku_get_reply",
    "g3ku_session_status",
    "g3ku_pause",
    "g3ku_cancel",
    "g3ku_list_conversations",
}


class FakeGatewayClient:
    """Scripted stand-in for G3kuMcpClient (tool-level orchestration tests)."""

    def __init__(
        self,
        *,
        send_result: dict[str, Any] | None = None,
        outcome: dict[str, Any] | None = None,
        state: dict[str, Any] | None = None,
        sessions_payload: dict[str, Any] | None = None,
        send_error: Exception | None = None,
    ):
        self.conversation_prefix = "mcp"
        self.sent: list[tuple[str, str]] = []
        self.streamed: list[tuple[str, int]] = []
        self._send_result = send_result or {"status": "started", "turn_id": "t1"}
        self._outcome = outcome or {"kind": "reply", "text": "网关回复", "turn_id": "t1", "usage": None}
        self._state = state or {"running": False, "queued_follow_ups": 0, "inflight_turn_id": None, "last_error": None}
        self._sessions_payload = sessions_payload or {"ok": True, "bridge_id": "test", "items": []}
        self._send_error = send_error
        self._seq = 3

    def external_key(self, conversation: str) -> str:
        return f"mcp:{conversation}"

    def conversation_of(self, external_key: str) -> str:
        raw = str(external_key or "")
        return raw[4:] if raw.startswith("mcp:") else raw

    def last_seq(self, session_id: str) -> int:
        return self._seq

    async def ensure_session(self, conversation: str, *, title: str | None = None) -> str:
        return f"ext:test:{conversation}"

    @asynccontextmanager
    async def event_stream(self, session_id: str, *, last_seq: int = 0):
        self.streamed.append((session_id, last_seq))

        async def _events():
            yield {"type": "progress", "seq": last_seq + 1}

        yield _events()

    async def send_message(self, session_id: str, text: str, *, idempotency_key: str | None = None) -> dict:
        if self._send_error is not None:
            raise self._send_error
        self.sent.append((session_id, text))
        return dict(self._send_result)

    async def wait_for_reply(self, events, **kwargs) -> dict:
        async for _ in events:
            break
        return dict(self._outcome)

    async def session_state(self, session_id: str) -> dict:
        return dict(self._state)

    async def pause_turn(self, turn_id: str) -> dict:
        return {"ok": True, "paused": True, "turn_id": turn_id}

    async def cancel_session(self, session_id: str) -> dict:
        return {"ok": True, "cancelled": 2, "session_id": session_id}

    async def list_sessions(self) -> dict:
        return dict(self._sessions_payload)


async def _call(server, name: str, args: dict[str, Any]) -> dict[str, Any]:
    result = await server.call_tool(name, args)
    if isinstance(result, dict):
        return result
    if isinstance(result, tuple):
        for item in result:
            if isinstance(item, dict):
                return item
        result = result[0]
    for block in result:
        text = getattr(block, "text", None)
        if text:
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                return parsed
    raise AssertionError(f"no dict result from tool {name}: {result!r}")


@pytest.mark.asyncio
async def test_chat_completed_maps_reply_and_usage():
    fake = FakeGatewayClient(
        outcome={"kind": "reply", "text": "完成回复", "turn_id": "t7", "usage": {"input_tokens": 3}}
    )
    server = build_mcp_server(fake)
    result = await _call(server, "g3ku_chat", {"conversation": "c1", "message": "你好"})
    assert result["ok"] is True
    assert result["status"] == "completed"
    assert result["reply"] == "完成回复"
    assert result["turn_id"] == "t7"
    assert result["usage"] == {"input_tokens": 3}
    assert result["session_id"] == "ext:test:c1"
    assert fake.sent == [("ext:test:c1", "你好")]
    # 流先开、再发消息（零间隙次序）
    assert fake.streamed == [("ext:test:c1", 3)]


@pytest.mark.asyncio
async def test_chat_pending_returns_hint_and_turn_id():
    fake = FakeGatewayClient(outcome={"kind": "timeout"})
    server = build_mcp_server(fake)
    result = await _call(server, "g3ku_chat", {"conversation": "c1", "message": "慢任务", "wait_seconds": 30})
    assert result["ok"] is True
    assert result["status"] == "pending"
    assert result["turn_id"] == "t1"
    assert "g3ku_get_reply" in result["hint"]


@pytest.mark.asyncio
async def test_chat_queued_timeout_returns_receipt():
    fake = FakeGatewayClient(
        send_result={"status": "queued", "turn_id": None, "receipt": "收到，将在当前任务中一并处理。"},
        outcome={"kind": "timeout"},
    )
    server = build_mcp_server(fake)
    result = await _call(server, "g3ku_chat", {"conversation": "c1", "message": "加一句"})
    assert result["status"] == "queued_receipt"
    assert result["receipt"] == "收到，将在当前任务中一并处理。"
    assert result["submit_status"] == "queued"


@pytest.mark.asyncio
async def test_chat_failed_and_cancelled_and_no_reply():
    for outcome, expected in (
        ({"kind": "failed", "error": "模型链不可用"}, "failed"),
        ({"kind": "cancelled"}, "cancelled"),
        ({"kind": "no_reply"}, "no_reply"),
    ):
        server = build_mcp_server(FakeGatewayClient(outcome=outcome))
        result = await _call(server, "g3ku_chat", {"conversation": "c1", "message": "x"})
        assert result["status"] == expected
        if expected == "failed":
            assert result["error"] == "模型链不可用"


@pytest.mark.asyncio
async def test_tools_never_raise_http_errors_become_payloads():
    request = httpx.Request("POST", "http://x/api/v1/sessions/s/messages")
    response = httpx.Response(401, json={"detail": "invalid_api_token"}, request=request)
    fake = FakeGatewayClient(send_error=httpx.HTTPStatusError("401", request=request, response=response))
    server = build_mcp_server(fake)
    result = await _call(server, "g3ku_chat", {"conversation": "c1", "message": "x"})
    assert result == {"ok": False, "error": "invalid_api_token", "status_code": 401}

    fake2 = FakeGatewayClient(send_error=httpx.ConnectError("boom"))
    server2 = build_mcp_server(fake2)
    result2 = await _call(server2, "g3ku_chat", {"conversation": "c1", "message": "x"})
    assert result2 == {"ok": False, "error": "connection_failed"}


@pytest.mark.asyncio
async def test_get_reply_found_and_not_found():
    server = build_mcp_server(
        FakeGatewayClient(outcome={"kind": "reply", "text": "续取回复", "turn_id": "t2", "usage": None})
    )
    found = await _call(server, "g3ku_get_reply", {"conversation": "c1"})
    assert found == {
        "ok": True,
        "conversation": "c1",
        "found": True,
        "reply": "续取回复",
        "turn_id": "t2",
        "usage": None,
    }

    server2 = build_mcp_server(FakeGatewayClient(outcome={"kind": "timeout"}))
    missing = await _call(server2, "g3ku_get_reply", {"conversation": "c1"})
    assert missing["found"] is False
    assert missing["status"] == "timeout"


@pytest.mark.asyncio
async def test_session_status_pause_cancel_list():
    fake = FakeGatewayClient(
        state={"running": True, "queued_follow_ups": 2, "inflight_turn_id": "t9", "last_error": None},
        sessions_payload={
            "ok": True,
            "bridge_id": "test",
            "items": [
                {"session_id": "ext:test:h1", "external_key": "mcp:alpha", "title": "Alpha", "created_at": "2026"},
                {"session_id": "ext:test:h2", "external_key": "other:x", "title": "X", "created_at": "2026"},
            ],
        },
    )
    server = build_mcp_server(fake)

    status = await _call(server, "g3ku_session_status", {"conversation": "c1"})
    assert status["running"] is True
    assert status["queued_follow_ups"] == 2
    assert status["inflight_turn_id"] == "t9"
    assert status["last_seq"] == 3

    paused = await _call(server, "g3ku_pause", {"conversation": "c1"})
    assert paused == {"ok": True, "conversation": "c1", "paused": True, "turn_id": "t9"}

    idle = FakeGatewayClient(state={"running": False, "queued_follow_ups": 0, "inflight_turn_id": None, "last_error": None})
    no_turn = await _call(build_mcp_server(idle), "g3ku_pause", {"conversation": "c1"})
    assert no_turn == {"ok": False, "error": "no_inflight_turn", "conversation": "c1"}

    cancelled = await _call(server, "g3ku_cancel", {"conversation": "c1"})
    assert cancelled["cancelled"] == 2

    listed = await _call(server, "g3ku_list_conversations", {})
    assert listed["ok"] is True
    assert [item["conversation"] for item in listed["items"]] == ["alpha", "other:x"]


@pytest.mark.asyncio
async def test_mcp_protocol_in_memory_list_and_call_tools():
    """协议层冒烟：真 MCP client/session 走 in-memory 流对接 FastMCP server。"""
    from mcp.shared.memory import create_connected_server_and_client_session

    fake = FakeGatewayClient()
    server = build_mcp_server(fake)
    async with create_connected_server_and_client_session(server) as session:
        await session.initialize()
        tools = await session.list_tools()
        names = {tool.name for tool in tools.tools}
        assert TOOL_NAMES <= names
        chat_schema = next(t for t in tools.tools if t.name == "g3ku_chat").inputSchema
        assert {"conversation", "message"} <= set(chat_schema.get("required") or [])

        result = await session.call_tool("g3ku_chat", {"conversation": "c1", "message": "协议层你好"})
        payload = None
        structured = getattr(result, "structuredContent", None)
        if isinstance(structured, dict):
            payload = structured.get("result") if isinstance(structured.get("result"), dict) else structured
        if payload is None:
            for block in result.content:
                text = getattr(block, "text", None)
                if text:
                    try:
                        payload = json.loads(text)
                        break
                    except json.JSONDecodeError:
                        continue
        assert isinstance(payload, dict)
        assert payload.get("status") == "completed"
        assert payload.get("reply") == "网关回复"


# -- client transport layer (real G3kuMcpClient over MockTransport) ----------


def _sse_body(events: list[dict[str, Any]]) -> bytes:
    frames = []
    for event in events:
        frames.append(f"id: {event['seq']}\nevent: {event['type']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n")
    return "".join(frames).encode("utf-8")


def _transport(
    *,
    events: list[dict[str, Any]] | None = None,
    message_response: dict[str, Any] | None = None,
):
    seen: dict[str, list] = {"requests": []}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["requests"].append(request)
        path = request.url.path
        if path.endswith("/events"):
            return httpx.Response(
                200,
                content=_sse_body(events or []),
                headers={"content-type": "text/event-stream"},
            )
        if path.endswith("/messages"):
            return httpx.Response(200, json=message_response or {"ok": True, "status": "started", "turn_id": "t1"})
        if path.endswith("/state"):
            return httpx.Response(
                200,
                json={"ok": True, "running": False, "queued_follow_ups": 0, "inflight_turn_id": None, "last_error": None},
            )
        if path.endswith("/sessions") and request.method == "POST":
            return httpx.Response(200, json={"ok": True, "session_id": "ext:test:s1"})
        if path.endswith("/sessions") and request.method == "GET":
            return httpx.Response(200, json={"ok": True, "bridge_id": "test", "items": []})
        return httpx.Response(404, json={"detail": "not_found"})

    return httpx.MockTransport(handler), seen


def _client(transport) -> G3kuMcpClient:
    return G3kuMcpClient("http://testserver/api/v1", "tok", transport=transport)


@pytest.mark.asyncio
async def test_client_wait_for_reply_started_match():
    transport, _ = _transport(
        events=[
            {"type": "progress", "seq": 2, "kind": "milestone", "text": "working"},
            {"type": "reply.final", "seq": 3, "turn_id": "t1", "text": "答案", "usage": {"input_tokens": 1}},
            {"type": "turn.completed", "seq": 4, "turn_id": "t1"},
        ]
    )
    client = _client(transport)
    try:
        async with client.event_stream("ext:test:s1", last_seq=1) as events:
            outcome = await client.wait_for_reply(events, turn_id="t1", after_seq=1, timeout=5)
        assert outcome["kind"] == "reply"
        assert outcome["text"] == "答案"
        assert outcome["usage"] == {"input_tokens": 1}
        assert client.last_seq("ext:test:s1") == 3  # final 后立刻返回，未消费 completed
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_client_wait_for_reply_queued_chain_rule():
    """F1 客户端镜像：在跑回合的 final 先到，queued 模式取终态前 max-seq。"""
    transport, _ = _transport(
        events=[
            {"type": "reply.final", "seq": 2, "turn_id": "t0", "text": "predecessor"},
            {"type": "reply.final", "seq": 3, "turn_id": "t0", "text": "batch answer"},
            {"type": "turn.completed", "seq": 4, "turn_id": "t0"},
        ]
    )
    client = _client(transport)
    try:
        async with client.event_stream("ext:test:s1", last_seq=1) as events:
            outcome = await client.wait_for_reply(events, queued=True, after_seq=1, timeout=5)
        assert outcome["kind"] == "reply"
        assert outcome["text"] == "batch answer"
        assert client.last_seq("ext:test:s1") == 4
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_client_wait_for_reply_failed_and_stream_closed():
    transport, _ = _transport(events=[{"type": "turn.failed", "seq": 2, "turn_id": "t1", "error": "炸了"}])
    client = _client(transport)
    try:
        async with client.event_stream("ext:test:s1", last_seq=1) as events:
            outcome = await client.wait_for_reply(events, turn_id="t1", after_seq=1, timeout=5)
        assert outcome == {"kind": "failed", "error": "炸了"}
    finally:
        await client.aclose()

    transport2, _ = _transport(events=[{"type": "progress", "seq": 2, "kind": "milestone", "text": "x"}])
    client2 = _client(transport2)
    try:
        async with client2.event_stream("ext:test:s1", last_seq=1) as events:
            outcome2 = await client2.wait_for_reply(events, turn_id="t1", after_seq=1, timeout=5)
        assert outcome2["kind"] == "timeout"
        assert outcome2["error"] == "stream_closed"
    finally:
        await client2.aclose()


@pytest.mark.asyncio
async def test_client_wait_for_reply_deadline():
    transport, _ = _transport()
    client = _client(transport)

    async def _slow_events():
        yield {"type": "progress", "seq": 1}
        await asyncio.sleep(10)
        yield {"type": "reply.final", "seq": 2, "turn_id": "t1", "text": "太晚了"}

    try:
        outcome = await client.wait_for_reply(_slow_events(), turn_id="t1", after_seq=0, timeout=0.3)
        assert outcome["kind"] == "timeout"
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_client_send_message_idempotency_header_and_session_cache():
    transport, seen = _transport()
    client = _client(transport)
    try:
        session_id = await client.ensure_session("alpha")
        assert session_id == "ext:test:s1"
        await client.ensure_session("alpha")  # cached, no second POST
        posts = [r for r in seen["requests"] if r.method == "POST" and r.url.path.endswith("/sessions")]
        assert len(posts) == 1

        await client.send_message(session_id, "无键")
        await client.send_message(session_id, "带键", idempotency_key="k1")
        messages = [r for r in seen["requests"] if r.url.path.endswith("/messages")]
        assert "idempotency-key" not in messages[0].headers
        assert messages[1].headers["idempotency-key"] == "k1"
        assert json.loads(messages[0].content)["text"] == "无键"
    finally:
        await client.aclose()
