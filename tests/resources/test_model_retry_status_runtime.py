from __future__ import annotations

import pytest

from g3ku.core.messages import UserInputMessage
from g3ku.runtime.session_agent import RuntimeAgentSession
from main.monitoring.log_service import TaskLogService
from main.runtime.chat_backend import _model_retry_status_error_text
from main.runtime.react_loop import ReActToolLoop


class _FrameLogService:
    def __init__(self) -> None:
        self.frames: dict[tuple[str, str], dict[str, object]] = {}
        self.publish_counts: list[bool] = []

    def update_frame(self, task_id, node_id, mutator, *, publish_snapshot=False) -> None:
        key = (str(task_id), str(node_id))
        frame = dict(self.frames.get(key, {"node_id": node_id}))
        self.frames[key] = mutator(frame)
        if publish_snapshot:
            self.publish_counts.append(True)


def _model_retry_payload(*, retry_count: int = 3) -> dict[str, object]:
    return {
        "state": "retrying",
        "retry_count": retry_count,
        "chain_round": 7,
        "error_message": "Error code: 429 - Server is busy",
        "model_refs": ["primary"],
        "delay_seconds": 1.2,
        "last_retry_at": "2026-09-03T14:53:07+08:00",
        "next_retry_at": "2026-09-03T14:55:07+08:00",
    }


@pytest.mark.asyncio
async def test_runtime_session_exposes_live_model_retry_status() -> None:
    session = RuntimeAgentSession(
        None,
        session_key="test:model-retry",
        channel="test",
        chat_id="model-retry",
    )
    session._state.is_running = True
    session._state.status = "running"
    session._last_prompt = UserInputMessage(content="retry demo")
    session._frontdoor_model_retry_status = _model_retry_payload()

    snapshot = session._build_execution_context_snapshot()

    assert snapshot is not None
    assert snapshot["model_retry_status"]["state"] == "retrying"
    assert snapshot["model_retry_status"]["retry_count"] == 3
    assert snapshot["model_retry_status"]["error_message"].startswith("Error code: 429")
    # CEO 快照深拷贝整体透传，绝对重试时刻不被裁剪
    assert snapshot["model_retry_status"]["last_retry_at"] == "2026-09-03T14:53:07+08:00"
    assert snapshot["model_retry_status"]["next_retry_at"] == "2026-09-03T14:55:07+08:00"


def test_task_runtime_frame_sanitizes_model_retry_status_for_websocket() -> None:
    public_frame = TaskLogService._public_runtime_frame(
        {
            "node_id": "node:1",
            "model_retry_status": _model_retry_payload(),
            "error_message": "ignored",
        }
    )

    assert public_frame["model_retry_status"]["state"] == "retrying"
    assert public_frame["model_retry_status"]["retry_count"] == 3
    assert public_frame["model_retry_status"]["error_message"].startswith(
        "Error code: 429 - Server is busy"
    )
    # task-node frame 净化器是白名单：绝对重试时刻必须被显式放行，否则到不了前端 toast
    assert public_frame["model_retry_status"]["last_retry_at"] == "2026-09-03T14:53:07+08:00"
    assert public_frame["model_retry_status"]["next_retry_at"] == "2026-09-03T14:55:07+08:00"
    assert TaskLogService._public_runtime_frame(
        {"node_id": "node:1", "model_retry_status": {"state": "cleared"}}
    )["model_retry_status"] is None


def test_model_retry_error_text_keeps_full_diagnostics_for_click_to_expand() -> None:
    long_error = "Error code: 429 - " + "rpm exhausted body " * 60

    text = _model_retry_status_error_text(long_error)

    assert text == " ".join(long_error.split())
    assert not text.endswith("...")


def test_task_frame_sanitizer_keeps_full_error_for_click_to_expand() -> None:
    payload = _model_retry_payload()
    payload["error_message"] = "Error code: 429 - " + "rpm exhausted body " * 60

    public_frame = TaskLogService._public_runtime_frame(
        {"node_id": "node:1", "model_retry_status": payload}
    )

    assert public_frame["model_retry_status"]["error_message"] == " ".join(
        str(payload["error_message"]).split()
    )


@pytest.mark.asyncio
async def test_react_loop_forwards_and_updates_task_frame_retry_status() -> None:
    captured: list[dict[str, object]] = []

    class _ChatBackend:
        async def chat(self, **kwargs):
            captured.append(dict(kwargs))
            return object()

    log_service = _FrameLogService()
    loop = ReActToolLoop(
        chat_backend=_ChatBackend(),
        log_service=log_service,
        max_iterations=2,
    )
    callback = loop._model_retry_status_callback(task_id="task:1", node_id="node:1")

    await callback(_model_retry_payload(retry_count=2))
    retrying_frame = dict(log_service.frames[("task:1", "node:1")]["model_retry_status"] or {})
    assert retrying_frame["retry_count"] == 2
    await loop._chat_with_optional_extensions(
        messages=[],
        tools=None,
        model_refs=["primary"],
        on_model_retry_status=callback,
    )
    await callback({"state": "cleared"})

    key = ("task:1", "node:1")
    assert captured[0]["on_model_retry_status"] is callback
    assert log_service.frames[key]["model_retry_status"] is None
    assert len(log_service.publish_counts) == 2


def _waiting_upstream_payload(*, waiting_seconds: int = 61, chunk_count: int = 1284) -> dict[str, object]:
    return {
        "state": "waiting_upstream",
        "retry_count": 1,
        "chain_round": 0,
        "waiting_seconds": waiting_seconds,
        "chunk_count": chunk_count,
        "chunk_kinds": "line:1200,event:response.keepalive:84",
        "model_refs": ["primary"],
        "route_entries": [{"type": "model", "model_key": "primary"}],
        "attempted_model_keys": ["primary"],
        "selected_group_key": "",
        "delay_seconds": 0.0,
    }


@pytest.mark.asyncio
async def test_runtime_session_exposes_live_upstream_wait_status() -> None:
    session = RuntimeAgentSession(
        None,
        session_key="test:upstream-wait",
        channel="test",
        chat_id="upstream-wait",
    )
    session._state.is_running = True
    session._state.status = "running"
    session._last_prompt = UserInputMessage(content="wait demo")
    session._frontdoor_model_retry_status = _waiting_upstream_payload()

    snapshot = session._build_execution_context_snapshot()

    assert snapshot is not None
    status = snapshot["model_retry_status"]
    assert status["state"] == "waiting_upstream"
    # 这条道只插入不改写：等待秒数与分片数必须原样穿过快照，否则界面上只剩一个转圈。
    assert status["waiting_seconds"] == 61
    assert status["chunk_count"] == 1284
    session._frontdoor_model_retry_status = {"state": "cleared"}
    assert "model_retry_status" not in (session._build_execution_context_snapshot() or {})


def test_task_runtime_frame_sanitizes_upstream_wait_status_for_websocket() -> None:
    public_frame = TaskLogService._public_runtime_frame(
        {"node_id": "node:1", "model_retry_status": _waiting_upstream_payload()}
    )

    status = public_frame["model_retry_status"]
    assert status["state"] == "waiting_upstream"
    # 净化器是逐字段重建的白名单：新字段不在名单里就等于没发生。
    assert status["waiting_seconds"] == 61
    assert status["chunk_count"] == 1284
    assert status["chunk_kinds"] == "line:1200,event:response.keepalive:84"
    assert "error_message" not in status


def test_retry_status_sanitizer_still_rejects_unknown_states() -> None:
    for payload in ({"state": "waiting-upstream"}, {"state": "stalled"}, {"state": "cleared"}, {}):
        assert TaskLogService._sanitize_model_retry_status(payload) is None, payload


@pytest.mark.asyncio
async def test_react_loop_stores_upstream_wait_on_task_frame() -> None:
    log_service = _FrameLogService()
    loop = ReActToolLoop(chat_backend=None, log_service=log_service, max_iterations=2)
    callback = loop._model_retry_status_callback(task_id="task:1", node_id="node:1")

    await callback(_waiting_upstream_payload(waiting_seconds=95, chunk_count=42))

    status = log_service.frames[("task:1", "node:1")]["model_retry_status"]
    assert status["state"] == "waiting_upstream"
    assert status["waiting_seconds"] == 95
    assert status["chunk_count"] == 42

    await callback({"state": "cleared"})
    assert log_service.frames[("task:1", "node:1")]["model_retry_status"] is None
