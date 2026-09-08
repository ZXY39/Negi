"""OpenAI 兼容网关测试（/api/v1/chat/completions、/api/v1/models）。

harness 沿用 test_external_api_messages 的 _FakeBridge/service 形态，并按
设计注记 F9 扩展：fake bridge 必须向 ``listeners`` 派发 AgentEvent
（assistant_stream_delta / message_end），否则 relay 不产 hub 事件、等待
必超时；_FakeSession 带 ``_frontdoor_turn_usage`` 供 usage 映射断言。
SSE/流式响应用 ASGITransport 整体缓冲断言（流会自行终结）。
"""

from __future__ import annotations

import asyncio
import base64
import json
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from g3ku.core.events import AgentEvent
from g3ku.core.messages import UserInputMessage
from g3ku.runtime.api import external_turns, external_v1, openai_compat
from g3ku.runtime.api.external_auth import ExternalApiPrincipal, require_external_api
from g3ku.runtime.external_events import (
    get_session_event_hub,
    reset_session_event_hubs,
    wait_for_external_reply,
)
from g3ku.runtime.external_sessions import (
    ExternalSessionRegistry,
    reset_external_session_registry,
)


class _AnyUsageDict(dict):
    """``_frontdoor_turn_usage`` stand-in returning fixed usage for any turn id."""

    def get(self, key, default=None):  # noqa: D102 - intentional override
        return {"input_tokens": 11, "output_tokens": 7, "cache_hit_tokens": 3, "call_count": 2}


class _FakeSession:
    def __init__(self, *, running: bool = False):
        self.state = SimpleNamespace(
            is_running=running,
            status="running" if running else "idle",
            queued_follow_up_messages=[],
            last_error=None,
        )
        self.queued: list = []
        self._frontdoor_turn_usage = _AnyUsageDict()

    async def queue_follow_up_batch(self, messages, *, persist_transcript=True):
        self.queued.extend(messages)
        return list(messages)

    def drain_queued_follow_up_messages(self):
        drained = list(self.queued)
        self.queued.clear()
        return drained

    async def archive_follow_up_chain_transition(self, *, pending_follow_up_turn_ids=None):
        return None


class _FakeBridge:
    def __init__(
        self,
        session=None,
        *,
        reply_text: str = "好的，完成了",
        deltas: tuple[str, ...] = ("好的", "好的，完成了"),
        fail_with: Exception | None = None,
        delay: float = 0.0,
    ):
        self._session = session
        self.reply_text = reply_text
        self.deltas = tuple(deltas)
        self.fail_with = fail_with
        self.delay = delay
        self.prompts: list = []
        self.batches: list = []

    def get_existing_session(self, session_key):
        return self._session

    @staticmethod
    def session_is_running(session):
        return bool(session is not None and session.state.is_running)

    async def _dispatch(self, kwargs):
        for listener in list(kwargs.get("listeners") or []):
            for text in self.deltas:
                await listener(AgentEvent(type="assistant_stream_delta", payload={"text": text, "source": "user"}))
            await listener(AgentEvent(type="message_end", payload={"text": self.reply_text, "source": "user"}))

    async def prompt(self, message, **kwargs):
        self.prompts.append(message)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail_with is not None:
            raise self.fail_with
        await self._dispatch(kwargs)
        return SimpleNamespace(output=self.reply_text)

    async def prompt_batch(self, messages, **kwargs):
        self.batches.append(list(messages))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail_with is not None:
            raise self.fail_with
        await self._dispatch(kwargs)
        return SimpleNamespace(output=self.reply_text)

    async def pause(self, session_key, *, manual=True):
        return 1

    async def cancel(self, session_key, *, reason=""):
        return 0


@pytest.fixture
def workspace(tmp_path):
    return tmp_path


@pytest.fixture
def registry(workspace):
    reset_external_session_registry()
    reset_session_event_hubs()
    reg = ExternalSessionRegistry(workspace)
    yield reg
    reset_external_session_registry()
    reset_session_event_hubs()


@pytest.fixture
def harness(monkeypatch, workspace, registry):
    monkeypatch.setattr(openai_compat, "get_external_session_registry", lambda: registry)
    monkeypatch.setattr(openai_compat, "_publish_ceo_catalog_best_effort", lambda: None)
    monkeypatch.setattr(external_v1, "workspace_path", lambda: workspace)

    def build(session=None, **bridge_kwargs):
        bridge = _FakeBridge(session or _FakeSession(), **bridge_kwargs)
        service = external_turns.ExternalTurnService(runtime_bridge=bridge, register_task=None)
        external_turns.set_external_turn_service(service)
        monkeypatch.setattr(openai_compat, "get_external_turn_service", lambda: service)
        app = FastAPI()
        app.include_router(openai_compat.router, prefix="/api/v1")
        app.dependency_overrides[require_external_api] = lambda: ExternalApiPrincipal(
            bridge_id="test-bridge", label="test"
        )
        return app, bridge, service

    yield build
    external_turns.set_external_turn_service(None)


def _client(app) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver", timeout=30.0)


async def _chat(client: AsyncClient, messages, **extra):
    payload = {"model": "g3ku", "messages": messages, **extra}
    return await client.post("/api/v1/chat/completions", json=payload)


def _data_frames(body: str) -> list:
    frames = []
    for line in body.splitlines():
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            frames.append("[DONE]")
            continue
        if data:
            frames.append(json.loads(data))
    return frames


@pytest.mark.asyncio
async def test_models_endpoint_lists_g3ku(harness):
    app, _, _ = harness()
    async with _client(app) as client:
        response = await client.get("/api/v1/models")
    assert response.status_code == 200
    body = response.json()
    assert body["object"] == "list"
    assert body["data"][0]["id"] == "g3ku"
    assert body["data"][0]["object"] == "model"
    assert body["data"][0]["owned_by"] == "g3ku"


@pytest.mark.asyncio
async def test_non_stream_happy_path(harness, registry):
    app, bridge, _ = harness()
    async with _client(app) as client:
        response = await _chat(client, [{"role": "user", "content": "你好"}], user="alice")
    assert response.status_code == 200
    body = response.json()
    assert body["id"].startswith("chatcmpl-")
    assert body["object"] == "chat.completion"
    assert body["model"] == "g3ku"
    choice = body["choices"][0]
    assert choice["message"] == {"role": "assistant", "content": "好的，完成了"}
    assert choice["finish_reason"] == "stop"
    assert body["usage"] == {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}
    assert body["g3ku"]["status"] == "completed"
    assert body["g3ku"]["submit_status"] == "started"
    assert body["g3ku"]["created_session"] is True
    assert body["g3ku"]["session_id"].startswith("ext:test-bridge:")

    forwarded = bridge.prompts[0]
    assert isinstance(forwarded, UserInputMessage)
    assert forwarded.content == "你好"
    assert forwarded.metadata.get("source") == "openai_compat"
    entries = [entry.external_key for entry in registry.list_entries()]
    assert "openai:alice" in entries


@pytest.mark.asyncio
async def test_system_prompt_prepended_only_when_session_created(harness):
    app, bridge, _ = harness()
    async with _client(app) as client:
        first = await _chat(
            client,
            [
                {"role": "system", "content": "你是助手"},
                {"role": "user", "content": "第一问"},
            ],
            user="bob",
        )
        second = await _chat(
            client,
            [
                {"role": "system", "content": "你是助手"},
                {"role": "user", "content": "第二问"},
            ],
            user="bob",
        )
    assert first.status_code == 200 and second.status_code == 200
    assert first.json()["g3ku"]["created_session"] is True
    assert second.json()["g3ku"]["created_session"] is False
    assert bridge.prompts[0].content == "你是助手\n\n第一问"
    assert bridge.prompts[1].content == "第二问"


@pytest.mark.asyncio
async def test_history_ignored_only_last_user_forwarded(harness):
    app, bridge, _ = harness()
    async with _client(app) as client:
        response = await _chat(
            client,
            [
                {"role": "user", "content": "旧问题"},
                {"role": "assistant", "content": "旧回答"},
                {"role": "user", "content": "新问题"},
            ],
        )
    assert response.status_code == 200
    assert len(bridge.prompts) == 1
    assert bridge.prompts[0].content == "新问题"


@pytest.mark.asyncio
async def test_validation_errors_use_openai_error_shape(harness):
    app, _, _ = harness()
    async with _client(app) as client:
        missing = await client.post("/api/v1/chat/completions", json={"model": "g3ku"})
        no_user = await client.post(
            "/api/v1/chat/completions",
            json={"model": "g3ku", "messages": [{"role": "assistant", "content": "hi"}]},
        )
    assert missing.status_code == 400
    body = missing.json()
    assert body["error"]["type"] == "invalid_request_error"
    assert body["error"]["message"] == "messages_required"
    assert body["error"]["param"] is None
    assert no_user.status_code == 400
    assert no_user.json()["error"]["message"] == "user_message_required"


@pytest.mark.asyncio
async def test_timeout_returns_200_pending_without_retry_storm(harness, monkeypatch):
    monkeypatch.setattr(openai_compat, "MIN_WAIT_SECONDS", 1.0)
    app, bridge, _ = harness(delay=2.0)
    async with _client(app) as client:
        response = await _chat(client, [{"role": "user", "content": "慢任务"}], wait_seconds=1)
    assert response.status_code == 200  # F8: 5xx 会触发 SDK 自动重试造成重复提交
    body = response.json()
    assert body["g3ku"]["status"] == "running"
    assert body["g3ku"]["submit_status"] == "started"
    assert body["g3ku"]["turn_id"]
    assert "still working" in body["choices"][0]["message"]["content"]
    assert len(bridge.prompts) == 1
    await asyncio.sleep(2.5)  # drain the still-running fake turn


@pytest.mark.asyncio
async def test_queued_waits_for_chain_final_not_predecessor(harness, registry):
    """F1：queued 提交后在跑回合自己的 final（回答前一条消息）先到；
    必须收集 finals 直到终态、取 max-seq 的那条。"""
    session = _FakeSession(running=True)
    app, _, _ = harness(session=session)
    entry, _ = registry.resolve_or_create(bridge_id="test-bridge", external_key="openai:default")
    hub = get_session_event_hub(entry.session_key)
    async with _client(app) as client:
        task = asyncio.create_task(_chat(client, [{"role": "user", "content": "加一句"}]))
        await asyncio.sleep(0.1)
        hub.publish("reply.final", turn_id="t-pre", text="predecessor answer", source="user")
        hub.publish("reply.final", turn_id="t-pre", text="batch answer", source="user")
        hub.publish("turn.completed", turn_id="t-pre")
        response = await task
    assert response.status_code == 200
    body = response.json()
    assert body["choices"][0]["message"]["content"] == "batch answer"
    assert body["g3ku"]["submit_status"] == "queued"
    assert body["g3ku"]["status"] == "completed"
    assert len(session.queued) == 1


@pytest.mark.asyncio
async def test_queued_timeout_returns_receipt(harness, monkeypatch):
    monkeypatch.setattr(openai_compat, "MIN_WAIT_SECONDS", 1.0)
    session = _FakeSession(running=True)
    app, _, _ = harness(session=session)
    async with _client(app) as client:
        response = await _chat(client, [{"role": "user", "content": "加一句"}], wait_seconds=1)
    assert response.status_code == 200
    body = response.json()
    assert body["g3ku"]["status"] == "queued_receipt"
    assert body["choices"][0]["message"]["content"] == "收到，将在当前任务中一并处理。"


@pytest.mark.asyncio
async def test_duplicate_returns_buffered_reply(harness):
    """F4：重复提交的 final 已在缓冲（seq <= after_seq），预扫 replay(0) 即返。"""
    app, bridge, _ = harness(reply_text="唯一答案", deltas=("唯一答案",))
    headers = {"Idempotency-Key": "openai-evt-1"}
    async with _client(app) as client:
        first = await client.post(
            "/api/v1/chat/completions",
            json={"model": "g3ku", "messages": [{"role": "user", "content": "只答一次"}]},
            headers=headers,
        )
        second = await client.post(
            "/api/v1/chat/completions",
            json={"model": "g3ku", "messages": [{"role": "user", "content": "只答一次"}]},
            headers=headers,
        )
    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["choices"][0]["message"]["content"] == "唯一答案"
    assert second.json()["choices"][0]["message"]["content"] == "唯一答案"
    assert second.json()["g3ku"]["submit_status"] == "duplicate"
    assert second.json()["g3ku"]["status"] == "completed"
    assert len(bridge.prompts) == 1


@pytest.mark.asyncio
async def test_failed_turn_returns_200_honest_text(harness):
    app, _, _ = harness(fail_with=ValueError("模型链不可用"))
    async with _client(app) as client:
        response = await _chat(client, [{"role": "user", "content": "会失败"}])
    assert response.status_code == 200
    body = response.json()
    assert body["g3ku"]["status"] == "failed"
    content = body["choices"][0]["message"]["content"]
    assert "turn failed" in content
    assert "模型链不可用" in content


@pytest.mark.asyncio
async def test_image_data_url_part_stores_attachment(harness, workspace):
    app, bridge, _ = harness()
    png_bytes = b"\x89PNG\r\n\x1a\nfake-image-bytes"
    data_url = "data:image/png;base64," + base64.b64encode(png_bytes).decode("ascii")
    async with _client(app) as client:
        response = await _chat(
            client,
            [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "看这张图"},
                        {"type": "image_url", "image_url": {"url": data_url}},
                    ],
                }
            ],
        )
    assert response.status_code == 200
    message = bridge.prompts[0]
    assert isinstance(message, UserInputMessage)
    blocks = message.content
    image_blocks = [b for b in blocks if b.get("type") == "image_url"]
    assert len(image_blocks) == 1
    assert image_blocks[0]["image_url"]["url"].startswith("data:image/png;base64,")
    stored_paths = list((workspace / ".g3ku" / "external-uploads").rglob("*"))
    stored_files = [p for p in stored_paths if p.is_file()]
    assert len(stored_files) == 1
    assert stored_files[0].read_bytes() == png_bytes
    assert message.metadata["external_attachments"][0]["kind"] == "image"


@pytest.mark.asyncio
async def test_image_http_url_part_reference_only(harness, workspace):
    app, bridge, _ = harness()
    async with _client(app) as client:
        response = await _chat(
            client,
            [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "看图"},
                        {"type": "image_url", "image_url": {"url": "https://cdn.example/img.png"}},
                    ],
                }
            ],
        )
    assert response.status_code == 200
    message = bridge.prompts[0]
    attachments = message.metadata["external_attachments"]
    assert attachments[0]["url"] == "https://cdn.example/img.png"
    assert "path" not in attachments[0]
    upload_root = workspace / ".g3ku" / "external-uploads"
    assert not upload_root.exists() or not [p for p in upload_root.rglob("*") if p.is_file()]


@pytest.mark.asyncio
async def test_oversize_image_413_openai_shape(harness):
    app, _, _ = harness()
    too_big = base64.b64encode(b"x" * (5 * 1024 * 1024 + 1)).decode("ascii")
    async with _client(app) as client:
        response = await _chat(
            client,
            [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "大图"},
                        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{too_big}"}},
                    ],
                }
            ],
        )
    assert response.status_code == 413
    assert response.json()["error"]["type"] == "invalid_request_error"


@pytest.mark.asyncio
async def test_auth_401_and_403(harness, monkeypatch):
    app, _, _ = harness()
    bare = FastAPI()
    bare.include_router(openai_compat.router, prefix="/api/v1")

    monkeypatch.setattr(
        "g3ku.runtime.api.external_auth.get_runtime_config",
        lambda force=False: (
            SimpleNamespace(external_api=SimpleNamespace(enabled=False, tokens={})),
            0,
            False,
        ),
    )
    async with _client(bare) as client:
        disabled = await client.post(
            "/api/v1/chat/completions", json={"messages": [{"role": "user", "content": "hi"}]}
        )
    assert disabled.status_code == 403
    assert disabled.json()["detail"] == "external_api_disabled"

    monkeypatch.setattr(
        "g3ku.runtime.api.external_auth.get_runtime_config",
        lambda force=False: (
            SimpleNamespace(
                external_api=SimpleNamespace(
                    enabled=True,
                    tokens={"b": SimpleNamespace(token="right-token", enabled=True, label="")},
                )
            ),
            0,
            False,
        ),
    )
    async with _client(bare) as client:
        wrong = await client.post(
            "/api/v1/chat/completions",
            json={"messages": [{"role": "user", "content": "hi"}]},
            headers={"Authorization": "Bearer wrong-token"},
        )
    assert wrong.status_code == 401
    assert wrong.json()["detail"] == "invalid_api_token"
    _ = app


@pytest.mark.asyncio
async def test_stream_chunks_prefix_diff_and_done(harness):
    app, _, _ = harness(reply_text="ABC", deltas=("AB", "ABC"))
    async with _client(app) as client:
        response = await _chat(client, [{"role": "user", "content": "流式"}], stream=True)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    frames = _data_frames(response.text)
    assert frames[-1] == "[DONE]"
    chunks = frames[:-1]
    assert all(chunk["object"] == "chat.completion.chunk" for chunk in chunks)
    assert chunks[0]["choices"][0]["delta"].get("role") == "assistant"
    contents = [c["choices"][0]["delta"].get("content") for c in chunks if c["choices"][0]["delta"].get("content")]
    assert contents == ["AB", "C"]  # 前缀差分；final 与已发一致 → 无纠正块
    assert chunks[-1]["choices"][0]["finish_reason"] == "stop"


@pytest.mark.asyncio
async def test_stream_segment_reset_emits_separator(harness):
    app, _, _ = harness(reply_text="XY", deltas=("AB", "XY"))
    async with _client(app) as client:
        response = await _chat(client, [{"role": "user", "content": "换段"}], stream=True)
    frames = _data_frames(response.text)
    chunks = frames[:-1]
    contents = [c["choices"][0]["delta"].get("content") for c in chunks if c["choices"][0]["delta"].get("content")]
    assert contents == ["AB", "\n\nXY"]  # F5：段切换发分隔符+新段全文
    assert frames[-1] == "[DONE]"


@pytest.mark.asyncio
async def test_stream_timeout_emits_honest_text_then_done(harness, monkeypatch):
    monkeypatch.setattr(openai_compat, "MIN_WAIT_SECONDS", 1.0)
    app, _, _ = harness(delay=2.0)
    async with _client(app) as client:
        response = await _chat(client, [{"role": "user", "content": "慢任务"}], stream=True, wait_seconds=1)
    frames = _data_frames(response.text)
    assert frames[-1] == "[DONE]"
    chunks = frames[:-1]
    assert chunks[-1]["choices"][0]["finish_reason"] == "stop"
    contents = "".join(
        str(c["choices"][0]["delta"].get("content") or "") for c in chunks if c["choices"][0]["delta"]
    )
    assert "still working" in contents
    await asyncio.sleep(2.5)  # drain the still-running fake turn


@pytest.mark.asyncio
async def test_wait_helper_replay_covers_pre_subscribe_publish(registry):
    hub = get_session_event_hub("ext:t:waiter")
    hub.publish("reply.final", turn_id="t1", text="早前回复", source="user")
    outcome = await wait_for_external_reply("ext:t:waiter", after_seq=0, timeout=0.5)
    assert outcome.kind == "reply"
    assert outcome.text == "早前回复"
    assert outcome.turn_id == "t1"


@pytest.mark.asyncio
async def test_wait_helper_queued_picks_max_seq_final_before_terminal(registry):
    """F1/F3 在 helper 层钉死：收集 finals 至终态、按 max-seq 选择。"""
    hub = get_session_event_hub("ext:t:queued")
    hub.publish("reply.final", turn_id="t0", text="first", source="user")
    hub.publish("reply.final", turn_id="t0", text="second", source="user")
    hub.publish("turn.completed", turn_id="t0")
    outcome = await wait_for_external_reply("ext:t:queued", after_seq=0, timeout=0.5, queued=True)
    assert outcome.kind == "reply"
    assert outcome.text == "second"


@pytest.mark.asyncio
async def test_wait_helper_failed_and_cancelled(registry):
    hub = get_session_event_hub("ext:t:failed")
    hub.publish("turn.failed", turn_id="t9", error="炸了", detail="boom")
    failed = await wait_for_external_reply("ext:t:failed", after_seq=0, timeout=0.5)
    assert failed.kind == "failed"
    assert failed.error == "炸了"

    hub2 = get_session_event_hub("ext:t:cancelled")
    hub2.publish("turn.completed", turn_id="t8", cancelled=True)
    cancelled = await wait_for_external_reply("ext:t:cancelled", after_seq=0, timeout=0.5)
    assert cancelled.kind == "cancelled"


@pytest.mark.asyncio
async def test_wait_helper_timeout_and_should_stop(registry):
    outcome = await wait_for_external_reply("ext:t:silent", after_seq=0, timeout=0.2)
    assert outcome.kind == "timeout"

    async def _stop() -> bool:
        return True

    stopped = await wait_for_external_reply("ext:t:silent2", after_seq=0, timeout=30.0, should_stop=_stop)
    assert stopped.kind == "timeout"
    assert stopped.error == "client_disconnected"
