"""CEO websocket 实时车道：单写者与补丁合并。

根因（实盘 2026-09-24 一天 418 次）：三条 sender 任务加握手期的直发并发 send()
踩到 websockets legacy 协议 "send() 不可并发" 的前提，帧被吞掉而 socket 还开着，
工具调用与阶段只能等重连补快照才一口气出现。
"""

from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from g3ku.core.events import AgentEvent
from g3ku.runtime.api import websocket_ceo as wcz
from g3ku.session.manager import SessionManager

SESSION_KEY = "ext:qq-official:liveprobe"


class _Harness:
    def __init__(self) -> None:
        self.frames: list[dict] = []
        self.published: list[dict] = []
        self.active = 0
        self.max_active = 0
        self.stop = threading.Event()
        self.loop: asyncio.AbstractEventLoop | None = None
        self.session_q: asyncio.Queue | None = None
        self.global_q: asyncio.Queue | None = None
        self.relay = None
        self.inflight: dict | None = None

    def frame_types(self) -> list[str]:
        return [str((frame or {}).get("type") or "") for frame in self.frames]

    def wait_until(self, predicate, timeout: float = 8.0) -> bool:
        # websocket_connect 只等 accept，handler 还在往后跑：订阅与握手帧都可能在
        # 断言那一刻还不存在，必须显式等它们到位再注入事件。
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.02)
        return predicate()

    def submit(self, coro):
        assert self.loop is not None
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout=10)


@pytest.fixture()
def harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    manager = SessionManager(tmp_path)
    session = manager.get_or_create(SESSION_KEY)
    session.messages.append({"role": "user", "content": "看下这份简历", "timestamp": "2026-09-24T16:00:00"})
    session.messages.append({
        "role": "assistant",
        "content": "上一轮结论",
        "timestamp": "2026-09-24T16:00:10",
        "canonical_context": {
            "stages": [{
                "stage_id": "frontdoor-stage-1",
                "stage_index": 0,
                "title": "读简历",
                "status": "completed",
                "rounds": [],
            }],
        },
    })
    manager.save(session)

    env = _Harness()

    async def fake_send(_websocket, payload):
        env.active += 1
        env.max_active = max(env.max_active, env.active)
        # 让三个写者在同一批回调里交错：没有写锁时这里必然看到 active>1。
        await asyncio.sleep(0)
        env.frames.append(payload)
        env.active -= 1

    async def fake_receive(_websocket):
        while not env.stop.is_set():
            await asyncio.sleep(0.01)
        raise WebSocketDisconnect()

    class _Registry:
        async def subscribe_ceo(self, _key):
            env.loop = asyncio.get_running_loop()
            env.session_q = asyncio.Queue()
            return env.session_q

        async def subscribe_global_ceo(self):
            env.global_q = asyncio.Queue()
            return env.global_q

        async def unsubscribe_ceo(self, _key, _queue):
            return None

        async def unsubscribe_global_ceo(self, _queue):
            return None

        def next_ceo_seq(self, _key):
            return 1

        def publish_global_ceo(self, envelope):
            env.published.append(envelope)

    class _RuntimeSession:
        def __init__(self) -> None:
            self.state = SimpleNamespace(session_key=SESSION_KEY, status="running", is_running=True)

        def subscribe(self, callback):
            env.relay = callback
            return lambda: None

        def state_dict(self):
            return {"status": "running", "is_running": True}

        def manual_pause_waiting_reason(self):
            return None

        def inflight_turn_snapshot(self):
            return env.inflight

        def preserved_inflight_turn_snapshot(self):
            return None

    runtime_session = _RuntimeSession()
    runtime_manager = SimpleNamespace(get=lambda _key: runtime_session, get_or_create=lambda **_kw: runtime_session)
    agent = SimpleNamespace(
        sessions=manager,
        main_task_service=SimpleNamespace(registry=_Registry(), startup=lambda: asyncio.sleep(0)),
    )

    async def fake_catalog(_store, **_kwargs):
        return {"items": [{"session_id": SESSION_KEY}], "channel_groups": [], "active_session_id": SESSION_KEY}

    monkeypatch.setattr(wcz, "get_bootstrap_security_service", lambda: SimpleNamespace(is_unlocked=lambda: True))
    monkeypatch.setattr(wcz, "get_agent", lambda: agent)
    monkeypatch.setattr(wcz, "get_runtime_manager", lambda _agent: runtime_manager)
    monkeypatch.setattr(wcz, "workspace_path", lambda: tmp_path)
    monkeypatch.setattr(wcz, "ensure_web_runtime_services", lambda _agent: None)
    monkeypatch.setattr(wcz, "build_ceo_session_catalog_async", fake_catalog)
    monkeypatch.setattr(wcz, "websocket_send_json", fake_send)
    monkeypatch.setattr(wcz, "websocket_receive_json", fake_receive)
    monkeypatch.setattr(wcz, "_publish_ceo_session_patch", lambda **_kwargs: None)

    env.inflight = {
        "turn_id": "turn-live",
        "source": "user",
        "status": "running",
        "assistant_text": "正在读",
        "canonical_context": {
            "stages": [
                {"stage_id": "frontdoor-stage-1", "stage_index": 0, "title": "读简历", "status": "completed", "rounds": []},
                {"stage_id": "frontdoor-stage-2", "stage_index": 1, "title": "改技能特长", "status": "running", "rounds": []},
            ],
        },
    }

    app = FastAPI()
    app.include_router(wcz.router, prefix="/api")
    env.client = TestClient(app)
    yield env
    env.stop.set()


def _connect(env):
    return env.client.websocket_connect(f"/api/ws/ceo?session_id={quote(SESSION_KEY, safe='')}")


def _tool_event(index: int) -> AgentEvent:
    return AgentEvent(
        type="tool_execution_start",
        timestamp="2026-09-24T16:00:20",
        payload={"tool_name": f"tool_{index}", "data": {}},
    )


def _burst(env, tool_calls: int):
    assert env.wait_until(lambda: "snapshot.ceo" in env.frame_types() and env.loop is not None), \
        f"握手未完成，已收到帧：{env.frame_types()}"

    async def run():
        for index in range(tool_calls):
            await env.session_q.put(wcz.build_envelope(channel="ceo", session_id=SESSION_KEY, type="ceo.state", data={"state": {}}))
            await env.global_q.put(wcz.build_envelope(channel="ceo", session_id=SESSION_KEY, type="ceo.audit", data={}))
            await env.relay(_tool_event(index))
            await asyncio.sleep(0)
    env.submit(run())
    # 留一点时间让拖尾补丁落地
    env.submit(asyncio.sleep(0.6))


def test_all_socket_writes_are_serialized(harness):
    """三条 sender 同时有帧时，写 socket 必须一次只有一个在飞。"""
    with _connect(harness):
        _burst(harness, tool_calls=6)
    assert harness.max_active == 1, f"并发写 socket：同时活跃写者 {harness.max_active}"
    assert "snapshot.ceo" in harness.frame_types()


def test_live_frames_carry_delta_only(harness):
    """live 帧不再挂整份 canonical_context，但 delta 恒在，前端才拿得到阶段。"""
    with _connect(harness):
        _burst(harness, tool_calls=2)
    live = [
        (frame.get("data") or {}).get("inflight_turn")
        for frame in harness.frames
        if isinstance((frame.get("data") or {}).get("inflight_turn"), dict)
    ]
    assert live, "没有带 inflight_turn 的帧"
    for turn in live:
        assert "canonical_context" not in turn
        assert [stage.get("stage_id") for stage in (turn.get("canonical_context_delta") or {}).get("stages") or []] == [
            "frontdoor-stage-2"
        ]


def test_turn_patches_are_coalesced(harness):
    """一回合内连续工具事件只产出一两帧轨道补丁，而不是一事件一帧。"""
    with _connect(harness):
        _burst(harness, tool_calls=6)
    types = harness.frame_types()
    assert types.count("ceo.agent.tool") == 6, "细粒度工具帧不能被合并掉"
    assert 1 <= types.count("ceo.turn.patch") <= 2, types.count("ceo.turn.patch")


def test_final_frame_forces_the_pending_patch_out_first(harness):
    """快回合不能整帧丢掉轨道：收尾帧之前必须先冲一帧补丁，且顺序不能反过来。"""
    with _connect(harness):
        assert harness.wait_until(lambda: "snapshot.ceo" in harness.frame_types() and harness.loop is not None)

        async def run():
            await harness.relay(_tool_event(0))
            await harness.relay(_tool_event(1))
            await harness.relay(AgentEvent(
                type="message_end",
                timestamp="2026-09-24T16:00:21",
                payload={"role": "assistant", "text": "改好了", "source": "user", "turn_id": "turn-live"},
            ))

        harness.submit(run())
        assert harness.wait_until(lambda: "ceo.reply.final" in harness.frame_types())

    types = harness.frame_types()
    assert types.count("ceo.turn.patch") == 2, types
    assert types.index("ceo.turn.patch") < types.index("ceo.reply.final")
    assert types[types.index("ceo.reply.final") - 1] == "ceo.turn.patch"
