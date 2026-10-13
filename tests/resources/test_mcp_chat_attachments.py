"""MCP 网关的附件上行车道：`negi_chat(attachments=[…])`。

进口是工具入参（模型只能给字符串），出口是 `/api/v1` 的请求体——所以每条用例
都从 handler 喂进去、在客户端那一层断言收到的描述符，中间任何一层把附件悄悄
丢掉都会红。另外钉住两件事：被拒的附件**一个都不提交**，以及网关那几个上限
就是运行时已有的那几个数字（漂移只会红，不会静默）。
"""

from __future__ import annotations

import base64
import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest

from g3ku.mcp_gateway import server as server_module
from g3ku.mcp_gateway.client import G3kuMcpClient
from g3ku.mcp_gateway.server import build_mcp_server

PNG_BYTES = b"\x89PNG\r\n\x1a\n-fake-image-body"


class _RecordingClient:
    """够 negi_chat 走完整车道用；只记录，不联网。"""

    def __init__(self, *, send_status: str = "started") -> None:
        self.conversation_prefix = "mcp"
        self.sent: list[tuple[str, str]] = []
        self.attachments: list[list[dict[str, Any]]] = []
        self._send_status = send_status

    async def resolve_session(self, conversation: str) -> str:
        return f"ext:mcp:{conversation}"

    def last_seq(self, session_id: str) -> int:
        return 0

    @asynccontextmanager
    async def event_stream(self, session_id: str, *, last_seq: int = 0):
        async def _events():
            yield {"type": "progress", "seq": last_seq + 1}

        yield _events()

    async def send_message(
        self,
        session_id: str,
        text: str,
        *,
        attachments: list[dict[str, Any]] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        self.sent.append((session_id, text))
        self.attachments.append(list(attachments or []))
        return {"status": self._send_status, "turn_id": "t1"}

    async def wait_for_reply(self, events, **kwargs) -> dict[str, Any]:
        async for _ in events:
            break
        if self._send_status == "queued":
            return {"kind": "", "turn_id": "t1", "transcript_turn_id": "tt1"}
        return {
            "kind": "reply",
            "text": "看到图了",
            "turn_id": "t1",
            "usage": None,
            "task_ids": [],
            "transcript_turn_id": "tt1",
        }


async def _chat(client: _RecordingClient, **args: Any) -> dict[str, Any]:
    handler = {t.name: t for t in build_mcp_server(client)._tool_manager.list_tools()}[
        "negi_chat"
    ].fn
    return await handler(conversation="c1", **args)


def _sent_file(client: _RecordingClient) -> dict[str, Any]:
    assert client.attachments and len(client.attachments[0]) == 1, client.attachments
    return client.attachments[0][0]


@pytest.mark.asyncio
async def test_local_path_reaches_the_gateway_client_as_base64(tmp_path: Path):
    image = tmp_path / "shot.png"
    image.write_bytes(PNG_BYTES)
    client = _RecordingClient()

    result = await _chat(client, message="看一下这张图", attachments=[str(image)])

    assert result["ok"] is True, result
    descriptor = _sent_file(client)
    assert base64.b64decode(descriptor["data_base64"]) == PNG_BYTES
    assert descriptor["name"] == "shot.png"
    assert descriptor["mime_type"] == "image/png"
    assert "path" not in descriptor and "url" not in descriptor
    # 送出去的东西要回报给调用方，不能只落在会话里。
    assert result["attachments"] == [f"shot.png ({len(PNG_BYTES)} bytes)"], result


@pytest.mark.asyncio
async def test_url_attachment_is_a_reference_and_reads_no_bytes(tmp_path: Path):
    client = _RecordingClient()

    result = await _chat(
        client, message="参考这张", attachments=["https://example.com/a/logo.png"]
    )

    assert result["ok"] is True, result
    assert _sent_file(client) == {"url": "https://example.com/a/logo.png"}
    assert result["attachments"] == ["https://example.com/a/logo.png"], result


@pytest.mark.asyncio
async def test_a_rejected_attachment_submits_nothing_at_all(tmp_path: Path):
    client = _RecordingClient()

    result = await _chat(client, message="图呢", attachments=[str(tmp_path / "gone.png")])

    assert result["ok"] is False, result
    assert result["error"] == "attachment_not_found", result
    assert result["attachment_index"] == 0
    assert "negi mcp serve" in result["hint"], result["hint"]
    assert client.sent == [], "被拒的附件不许先把正文提交出去"
    assert client.attachments == []


@pytest.mark.asyncio
async def test_oversize_is_refused_before_the_file_is_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    big = tmp_path / "big.png"
    big.write_bytes(PNG_BYTES)
    reads: list[str] = []

    def _spy(self):
        reads.append(str(self))
        return PNG_BYTES

    monkeypatch.setattr(Path, "read_bytes", _spy)
    monkeypatch.setattr(server_module, "ATTACHMENT_IMAGE_MAX_BYTES", len(PNG_BYTES) - 1)
    client = _RecordingClient()

    result = await _chat(client, message="发这张", attachments=[str(big)])

    assert result["ok"] is False, result
    assert result["error"] == "attachment_too_large", result
    assert result["max_bytes"] == len(PNG_BYTES) - 1, result
    assert reads == [], "超限文件不该被读进内存"
    assert client.sent == []


@pytest.mark.asyncio
async def test_more_than_four_attachments_rejects_the_whole_message(tmp_path: Path):
    paths = []
    for index in range(5):
        item = tmp_path / f"p{index}.txt"
        item.write_text("x")
        paths.append(str(item))
    client = _RecordingClient()

    result = await _chat(client, message="五个文件", attachments=paths)

    assert result["ok"] is False, result
    assert result["error"] == "too_many_attachments", result
    assert result["attachments_requested"] == 5 and result["max_attachments"] == 4, result
    assert client.sent == []


@pytest.mark.parametrize("source", ["file:///tmp/x.png", "data:image/png;base64,AAAA"])
@pytest.mark.asyncio
async def test_non_http_scheme_is_named_back_to_the_caller(source: str):
    client = _RecordingClient()

    result = await _chat(client, message="换个写法", attachments=[source])

    assert result["ok"] is False, result
    assert result["error"] == "unsupported_attachment_scheme", result
    scheme = source.split("://", 1)[0]
    assert scheme in result["hint"] or scheme in result["attachment"], result
    assert client.sent == []


@pytest.mark.asyncio
async def test_a_directory_is_reported_as_a_directory(tmp_path: Path):
    client = _RecordingClient()

    result = await _chat(client, message="整个目录", attachments=[str(tmp_path)])

    assert result["ok"] is False, result
    assert result["error"] == "attachment_is_a_directory", result
    assert client.sent == []


@pytest.mark.asyncio
async def test_the_attachment_echo_survives_a_queued_turn(tmp_path: Path):
    payload = "纪要".encode("utf-8")
    note = tmp_path / "n.txt"
    note.write_bytes(payload)
    client = _RecordingClient(send_status="queued")

    result = await _chat(client, message="附上纪要", attachments=[str(note)])

    assert result["status"] == "queued_receipt", result
    assert result["attachments"] == [f"n.txt ({len(payload)} bytes)"], result


def test_the_body_stays_text_only_when_no_attachment_is_given():
    bodies: list[dict[str, Any]] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(200, json={"status": "started", "turn_id": "t1"})

    async def _run():
        client = G3kuMcpClient(
            base_url="http://gate/api/v1", token="t", transport=httpx.MockTransport(_handler)
        )
        try:
            await client.send_message("s1", "只有正文")
            await client.send_message("s1", "带一张", attachments=[{"url": "https://e/x.png"}])
        finally:
            await client.aclose()

    import asyncio

    asyncio.run(_run())

    assert bodies[0] == {"text": "只有正文"}, "没附件时请求体形状不许变"
    assert bodies[1] == {
        "text": "带一张",
        "attachments": [{"url": "https://e/x.png"}],
    }, bodies[1]


def test_gateway_limits_are_the_runtimes_own_numbers():
    from g3ku.runtime.api.ceo_media import MAX_CHANNEL_ATTACHMENTS
    from g3ku.runtime.api.external_v1 import EXTERNAL_FILE_UPLOAD_MAX_BYTES
    from g3ku.runtime.web_ceo_sessions import WEB_CEO_IMAGE_UPLOAD_MAX_BYTES

    assert server_module.ATTACHMENT_IMAGE_MAX_BYTES == WEB_CEO_IMAGE_UPLOAD_MAX_BYTES
    assert server_module.ATTACHMENT_FILE_MAX_BYTES == EXTERNAL_FILE_UPLOAD_MAX_BYTES
    assert server_module.ATTACHMENTS_PER_MESSAGE_MAX == MAX_CHANNEL_ATTACHMENTS


@pytest.mark.asyncio
async def test_attachments_are_an_optional_string_array_in_the_schema():
    tools = await build_mcp_server(None).list_tools()
    schema = next(tool for tool in tools if tool.name == "negi_chat").inputSchema

    attachments = schema["properties"]["attachments"]
    assert attachments["items"]["type"] == "string", attachments
    assert "attachments" not in schema.get("required") or "attachments" not in schema["required"]
