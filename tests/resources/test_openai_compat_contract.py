"""OpenAI 兼容网关契约测试（/api/v1/chat/completions）——假 service + 直接 hub 事件。

与 test_openai_compat_api.py（全链路 bridge/relay 形态）互补：本文件把
ExternalTurnService 换成假实现（set_external_turn_service 注入），回合进展
由测试代码直接 publish 到会话 hub，专测网关自身的请求校验、submit 契约、
hub 等待/响应形状。

对照 test_openai_compat_api.py 已覆盖、此处不重复：
- 全链路 happy path / timeout / failed（bridge 驱动）
- 两次真实请求的 duplicate 回读
- data: URL 5MiB → 413、http(s) 引用附件
- 桥驱动的流式前缀差分、段切换分隔符、流式超时
"""

from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from g3ku.core.messages import UserInputMessage
from g3ku.runtime.api import external_turns, external_v1, openai_compat
from g3ku.runtime.api.external_auth import ExternalApiPrincipal, require_external_api
from g3ku.runtime.external_events import (
    get_session_event_hub,
    reset_session_event_hubs,
)
from g3ku.runtime.external_sessions import (
    ExternalSessionRegistry,
    reset_external_session_registry,
)

_BRIDGE_ID = "test-bridge"


class _FakeTurnService:
    """只实现 ExternalTurnService.submit —— 网关依赖的全部面。

    未 enqueue 时 submit 默认返回 started 形态；“只答一次” 但毁约、排队等
    形态由各用例 enqueue 显式提供。
    """

    def __init__(self):
        self._results: list[dict] = []
        self.submitted: list[dict] = []

    def enqueue(self, *results: dict) -> None:
        """追加 submit 返回值队列；最后一条重复返回（多次提交保持同形态）。"""
        self._results.extend(dict(item) for item in results)

    async def submit(self, *, entry, user_message, idempotency_key):
        if not self._results:
            result = {"turn_id": f"turn-{len(self.submitted) + 1}", "status": "started"}
        elif len(self._results) == 1:
            result = dict(self._results[0])
        else:
            result = self._results.pop(0)
        self.submitted.append(
            {
                "entry": entry,
                "user_message": user_message,
                "idempotency_key": idempotency_key,
                "result": result,
            }
        )
        return result


@pytest.fixture
def harness(monkeypatch, tmp_path):
    reset_external_session_registry()
    reset_session_event_hubs()
    registry = ExternalSessionRegistry(tmp_path)
    fake = _FakeTurnService()
    external_turns.set_external_turn_service(fake)
    monkeypatch.setattr(openai_compat, "get_external_session_registry", lambda: registry)
    monkeypatch.setattr(openai_compat, "_publish_ceo_catalog_best_effort", lambda: None)
    monkeypatch.setattr(external_v1, "workspace_path", lambda: tmp_path)
    app = FastAPI()
    app.include_router(openai_compat.router, prefix="/api/v1")
    app.dependency_overrides[require_external_api] = lambda: ExternalApiPrincipal(
        bridge_id=_BRIDGE_ID, label="test"
    )
    yield SimpleNamespace(app=app, registry=registry, service=fake, workspace=tmp_path)
    external_turns.set_external_turn_service(None)
    reset_external_session_registry()
    reset_session_event_hubs()


def _client(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver", timeout=30.0)


async def _chat(client: AsyncClient, messages, **extra):
    headers = dict(extra.pop("headers", None) or {})
    payload = {"model": "g3ku", "messages": messages, **extra}
    return await client.post("/api/v1/chat/completions", json=payload, headers=headers)


async def _until(check, timeout: float = 5.0) -> object:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        await asyncio.sleep(0.01)
    raise AssertionError("timed out waiting for condition")


async def _publish_after_submit(harness, events: list[dict]) -> None:
    """等请求打到假 service 后，向对应会话 hub 直接发布回合进展事件。

    两个坑（各自都曾让等待方按 want_turn_id 匹配不到事件而饿死）：
    1. 假 service 自造 turn-{n} 的 turn_id（与网关 want_turn_id 同一命名
       空间），用例事件里的 turn_id 只是占位——这里统一覆写成 submit 返回
       的真实 turn_id；
    2. hub.publish 的签名是 publish(event_type, *, turn_id=..., **payload)：
       事件用 dict（含 type 键）描述，必须拆开 dict 再发布，不能整体
       **kwargs（type 对不上 event_type 形参）。
    """
    await _until(lambda: bool(harness.service.submitted))
    record = harness.service.submitted[0]
    hub = get_session_event_hub(record["entry"].session_key)
    real_turn_id = str((record["result"] or {}).get("turn_id") or "")
    for event in events:
        event = dict(event)
        event_type = event.pop("type")
        event.pop("turn_id", None)
        hub.publish(event_type, turn_id=real_turn_id or None, **event)


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


# -- 请求校验 ---------------------------------------------------------------


async def test_error_envelope_shape_and_messages_required(harness):
    """缺失/非列表/空 messages → 400，OpenAI error 信封四项齐全。"""
    cases = [
        {},
        {"model": "g3ku"},
        {"model": "g3ku", "messages": {"role": "user"}},
        {"model": "g3ku", "messages": "hello"},
        {"model": "g3ku", "messages": 42},
        {"model": "g3ku", "messages": []},
    ]
    async with _client(harness.app) as client:
        for payload in cases:
            response = await client.post("/api/v1/chat/completions", json=payload)
            assert response.status_code == 400, payload
            body = response.json()
            assert body["error"]["message"] == "messages_required", payload
            assert body["error"]["type"] == "invalid_request_error"
            assert body["error"]["param"] is None
            assert body["error"]["code"] is None
    assert harness.service.submitted == []


async def test_no_user_message_or_empty_text_400(harness):
    """缺 user 消息、user 消息无内容/纯空白/空 parts → 400。"""
    cases = [
        # 条目全为非 dict → user 缺席
        [{"role": "system", "content": "x"}, "仅字符串", 42],
        [[1, 2]],
        # user 消息内容缺失/空文本
        [{"role": "user"}],
        [{"role": "user", "content": ""}],
        [{"role": "user", "content": "   \n\t"}],
        [{"role": "user", "content": []}],
        [{"role": "user", "content": [{"type": "text", "text": ""}, {"type": "text", "text": "  "}]}],
    ]
    async with _client(harness.app) as client:
        for messages in cases:
            response = await _chat(client, messages)
            assert response.status_code == 400, messages
            body = response.json()
            assert body["error"]["type"] == "invalid_request_error"
            has_user = any(
                isinstance(m, dict) and str(m.get("role") or "").strip().lower() == "user"
                for m in messages
            )
            assert body["error"]["message"] == (
                "user_message_required" if not has_user else "message_required"
            ), messages
    assert harness.service.submitted == []


# -- 非流式 happy path（假 service + 直接 hub 事件）--------------------------


async def test_happy_path_reply_with_hub_events(harness):
    """submit=started 后向 hub 发布 reply.final + turn.completed（同 turn_id）
    → 200 chat.completion 形状、文本正确、usage 映射、g3ku.status=completed。"""
    app, fake = harness.app, harness.service
    async with _client(app) as client:
        task = asyncio.create_task(
            _chat(
                client,
                [
                    {"role": "user", "content": "忽略的历史"},
                    {"role": "assistant", "content": "旧回答"},
                    {"role": "system", "content": "你是助手"},
                    {"role": "user", "content": "你好"},
                ],
                user="alice",
                model="custom-model",
                wait_seconds=2,
            )
        )
        await _publish_after_submit(
            harness,
            [
                {"type": "reply.final", "text": "你好，完成",
                 "usage": {"input_tokens": 4, "output_tokens": 6}},
                {"type": "turn.completed"},
            ],
        )
        response = await task
    assert response.status_code == 200
    body = response.json()
    assert body["object"] == "chat.completion"
    assert body["id"].startswith("chatcmpl-")
    assert body["model"] == "custom-model"  # model 仅回显
    assert body["choices"][0]["message"] == {"role": "assistant", "content": "你好，完成"}
    assert body["choices"][0]["finish_reason"] == "stop"
    assert body["usage"] == {"prompt_tokens": 4, "completion_tokens": 6, "total_tokens": 10}
    g3ku = body["g3ku"]
    assert g3ku["status"] == "completed"
    assert g3ku["submit_status"] == "started"
    assert g3ku["turn_id"] == fake.submitted[0]["result"]["turn_id"]
    assert g3ku["created_session"] is True
    assert g3ku["session_id"].startswith(f"ext:{_BRIDGE_ID}:")

    # 只转发最后一条 user 消息；系统提示词只随首回合前置
    assert len(fake.submitted) == 1
    message = fake.submitted[0]["user_message"]
    assert isinstance(message, UserInputMessage)
    assert message.content == "你是助手\n\n你好"
    assert message.metadata["source"] == "openai_compat"
    assert message.metadata["model"] == "custom-model"
    assert fake.submitted[0]["idempotency_key"] is None
    keys = [entry.external_key for entry in harness.registry.list_entries()]
    assert "openai:alice" in keys


async def test_duplicate_with_pre_seeded_buffer_returns_immediately(harness):
    """幂等重复但 hub 已预置 reply.final：submit=duplicate 直接反扫缓冲返回，
    不再进入等待。"""
    app, fake = harness.app, harness.service
    fake.enqueue({"turn_id": "turn-dup", "status": "duplicate", "original_status": "completed"})
    # 预置：先让注册表/会话就位，把终稿直接放进缓冲（seq 在 after_seq 之前）
    entry, _ = harness.registry.resolve_or_create(
        bridge_id=_BRIDGE_ID, external_key="openai:dup"
    )
    get_session_event_hub(entry.session_key).publish(
        "reply.final", turn_id="turn-dup", text="缓冲回复",
        usage={"input_tokens": 3, "output_tokens": 5},
    )
    async with _client(app) as client:
        response = await _chat(
            client, [{"role": "user", "content": "再问一次"}], user="dup",
            headers={"Idempotency-Key": "key-1"},
        )
    assert response.status_code == 200
    body = response.json()
    assert body["choices"][0]["message"]["content"] == "缓冲回复"
    assert body["g3ku"]["status"] == "completed"
    assert body["g3ku"]["submit_status"] == "duplicate"
    assert body["g3ku"]["turn_id"] == "turn-dup"
    assert body["usage"] == {"prompt_tokens": 3, "completion_tokens": 5, "total_tokens": 8}
    assert len(fake.submitted) == 1
    assert fake.submitted[0]["idempotency_key"] == "key-1"


# -- wait_seconds 边界 -------------------------------------------------------


def test_wait_seconds_clamp_bounds():
    """clamp 5..3600，非法/缺省回退 600（模块常量即契约）。"""
    clamp = openai_compat._clamp_wait
    assert openai_compat.DEFAULT_WAIT_SECONDS == 600.0
    assert openai_compat.MIN_WAIT_SECONDS == 5.0
    assert openai_compat.MAX_WAIT_SECONDS == 3600.0
    assert clamp({}) == 600.0
    assert clamp({"wait_seconds": None}) == 600.0
    assert clamp({"wait_seconds": "abc"}) == 600.0
    assert clamp({"wait_seconds": 0}) == 5.0
    assert clamp({"wait_seconds": -3}) == 5.0
    assert clamp({"wait_seconds": 5}) == 5.0
    assert clamp({"wait_seconds": 100}) == 100.0
    assert clamp({"wait_seconds": "30"}) == 30.0
    assert clamp({"wait_seconds": 3600}) == 3600.0
    assert clamp({"wait_seconds": 10_000}) == 3600.0


async def test_wait_seconds_floor_applies_in_request(harness, monkeypatch):
    """wait_seconds=0 → 实际按 MIN 等待（不报错、不 0 秒瞬返），超时仍是 200 running。"""
    monkeypatch.setattr(openai_compat, "MIN_WAIT_SECONDS", 0.4)
    app = harness.app
    started = time.monotonic()
    async with _client(app) as client:
        response = await _chat(client, [{"role": "user", "content": "慢任务"}], wait_seconds=0)
    elapsed = time.monotonic() - started
    assert response.status_code == 200
    body = response.json()
    assert body["g3ku"]["status"] == "running"
    assert body["g3ku"]["submit_status"] == "started"
    assert "still working" in body["choices"][0]["message"]["content"]
    assert elapsed >= 0.2, f"wait_seconds=0 未按 MIN 抬升，仅耗时 {elapsed:.2f}s"


# -- 图片附件形状 ------------------------------------------------------------


async def test_invalid_image_url_forms_400(harness):
    """损坏 data: URL / 非法 b64 / 非 http(s) scheme → 400 OpenAI 形状，不进入 submit。"""
    app, fake = harness.app, harness.service
    cases = [
        ("data:image/png,没有base64标记", "invalid_image_data_url"),
        ("data:image/png;base64,abc", "invalid_image_data_url"),  # 非法 padding
        ("ftp://server/evil.png", "unsupported_image_url"),
    ]
    async with _client(app) as client:
        for url, expected in cases:
            response = await _chat(
                client,
                [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "看图"},
                            {"type": "image_url", "image_url": {"url": url}},
                        ],
                    }
                ],
            )
            assert response.status_code == 400, url
            body = response.json()
            assert body["error"]["message"] == expected, url
            assert body["error"]["type"] == "invalid_request_error"
    assert fake.submitted == []


async def test_image_only_message_is_valid_and_forwarded(harness):
    """无文本、仅 image_url → 不触发 400，以附件消息正常提交并完成。"""
    app, fake = harness.app, harness.service
    async with _client(app) as client:
        task = asyncio.create_task(
            _chat(
                client,
                [
                    {
                        "role": "user",
                        "content": [
                            {"type": "image_url", "image_url": {"url": "https://cdn.example/p.png"}},
                        ],
                    }
                ],
                wait_seconds=2,
            )
        )
        await _publish_after_submit(
            harness,
            [
                {"type": "reply.final", "text": "收到图片"},
                {"type": "turn.completed"},
            ],
        )
        response = await task
    assert response.status_code == 200
    assert response.json()["g3ku"]["status"] == "completed"
    message = fake.submitted[0]["user_message"]
    assert isinstance(message, UserInputMessage)
    attachments = message.metadata["external_attachments"]
    assert attachments[0]["url"] == "https://cdn.example/p.png"
    assert "path" not in attachments[0]


# -- 流式（假 service + 直接 hub 事件）---------------------------------------


async def test_stream_shape_and_final_tail_correction(harness):
    """stream=true：role 块开头 → 前缀差分内容块 → reply.final 扩展最后一段时
    补尾差 → finish_reason=stop → [DONE]。"""
    app = harness.app
    async with _client(app) as client:
        task = asyncio.create_task(
            _chat(client, [{"role": "user", "content": "流式"}], stream=True, wait_seconds=2)
        )
        await _publish_after_submit(
            harness,
            [
                {"type": "reply.delta", "text": "AB"},
                {"type": "reply.delta", "text": "ABC"},
                {"type": "reply.final", "text": "ABCD"},
                {"type": "turn.completed"},
            ],
        )
        response = await task
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    frames = _data_frames(response.text)
    assert frames[-1] == "[DONE]"
    chunks = frames[:-1]
    assert all(chunk["object"] == "chat.completion.chunk" for chunk in chunks)
    assert chunks[0]["choices"][0]["delta"].get("role") == "assistant"
    contents = [
        chunk["choices"][0]["delta"].get("content")
        for chunk in chunks
        if chunk["choices"][0]["delta"].get("content")
    ]
    assert contents == ["AB", "C", "D"]  # 前缀差分 + final 尾差
    assert chunks[-1]["choices"][0]["finish_reason"] == "stop"
    assert chunks[-1]["choices"][0]["delta"] == {}


async def test_stream_turn_failed_ends_with_honest_text(harness):
    """流式中收到 turn.failed → failed 文案成内容块，随后 stop + [DONE]。"""
    app = harness.app
    async with _client(app) as client:
        task = asyncio.create_task(
            _chat(client, [{"role": "user", "content": "会失败"}], stream=True, wait_seconds=2)
        )
        await _publish_after_submit(
            harness,
            [{"type": "turn.failed", "error": "boom", "detail": "boom"}],
        )
        response = await task
    assert response.status_code == 200
    frames = _data_frames(response.text)
    assert frames[-1] == "[DONE]"
    chunks = frames[:-1]
    contents = "".join(
        str(chunk["choices"][0]["delta"].get("content") or "")
        for chunk in chunks
        if chunk["choices"][0]["delta"]
    )
    assert "turn failed" in contents
    assert "boom" in contents
    assert chunks[-1]["choices"][0]["finish_reason"] == "stop"


# -- 会话键映射 --------------------------------------------------------------


def test_resolve_external_key_trim_truncate_default():
    """openai: 前缀 + 剥空白 + 128 截断 + 空回退 default。"""
    resolve = openai_compat._resolve_external_key
    assert resolve({"user": "alice"}) == "openai:alice"
    assert resolve({"user": "  alice  "}) == "openai:alice"
    assert resolve({"user": "x" * 200}) == "openai:" + "x" * 128
    assert resolve({}) == "openai:default"
    assert resolve({"user": "   "}) == "openai:default"