from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from g3ku.heartbeat.session_service import (
    WebSessionHeartbeatService,
    _TASK_NODE_ERROR_MAX_CONSECUTIVE_FAILURES,
)


class _BoomPromptSession:
    """Runtime session whose prompt() always fails, modeled after the
    frontdoor_context_window_exceeded loop that broke china:qqbot:default:dm."""

    def __init__(self) -> None:
        self.state = SimpleNamespace(status="idle", is_running=False)
        self.prompt_calls = 0

    def subscribe(self, relay) -> object:
        return lambda: None

    async def prompt(self, user_input, persist_transcript: bool = True):
        self.prompt_calls += 1
        raise RuntimeError("boom")


class _FakePersistedSession:
    def __init__(self) -> None:
        self.metadata: dict = {}
        self.messages: list[dict] = []
        self.updated_at = ""

    def add_message(self, role: str, content: str, **kwargs) -> dict:
        message = {"role": role, "content": content, "timestamp": "2026-09-02T13:00:00+08:00", **kwargs}
        self.messages.append(message)
        return message

    def save(self) -> None:
        return None


class _FakeSessionManager:
    def __init__(self, session: _FakePersistedSession, exists_path: Path) -> None:
        self._session = session
        self._exists_path = exists_path
        self.saves = 0

    def get_path(self, session_id: str) -> Path:
        return self._exists_path

    def get_or_create(self, session_id: str) -> _FakePersistedSession:
        return self._session

    def save(self, session) -> None:
        self.saves += 1


class _FakeRuntimeManager:
    def __init__(self, session: _BoomPromptSession) -> None:
        self._session = session

    def get_or_create(self, **kwargs) -> _BoomPromptSession:
        return self._session


def _build_service(tmp_path: Path) -> tuple[WebSessionHeartbeatService, _BoomPromptSession, _FakePersistedSession, list[str]]:
    key = "china:qqbot:default:dm"
    prompts = _BoomPromptSession()
    persisted = _FakePersistedSession()
    exists_path = tmp_path / "session.jsonl"
    exists_path.write_text("", encoding="utf-8")
    notifier_texts: list[str] = []

    async def _notify(session_id: str, text: str) -> None:
        notifier_texts.append(text)

    service = WebSessionHeartbeatService(
        workspace=tmp_path,
        agent=SimpleNamespace(),
        runtime_manager=_FakeRuntimeManager(prompts),
        main_task_service=SimpleNamespace(registry=None, store=None),
        session_manager=_FakeSessionManager(persisted, exists_path),
        reply_notifier=_notify,
    )
    service._started = True
    service.enqueue_task_node_error_payload(
        key,
        [
            {
                "task_id": "task:26e64d1dc8b3",
                "node_id": "node:85b3119ce0ac",
                "pause_row_id": 34,
                "task_title": "日报任务",
                "node_title": "日报节点",
                "pause_reason": "error",
                "error_text": "RuntimeError: Error:",
                "dedupe_key": "node-error:task:26e64d1dc8b3:node:85b3119ce0ac:34",
            }
        ],
    )
    return service, prompts, persisted, notifier_texts


def test_node_error_heartbeat_stops_after_consecutive_failures(tmp_path: Path) -> None:
    service, prompts, persisted, notifier_texts = _build_service(tmp_path)
    key = "china:qqbot:default:dm"

    returns = [asyncio.run(service._run_session(key)) for _ in range(_TASK_NODE_ERROR_MAX_CONSECUTIVE_FAILURES)]

    # 前 N-1 次失败返回 10 秒继续重试；第 N 次放弃并出队，唤醒循环终止（None）。
    assert returns == [10.0] * (_TASK_NODE_ERROR_MAX_CONSECUTIVE_FAILURES - 1) + [None]
    assert prompts.prompt_calls == _TASK_NODE_ERROR_MAX_CONSECUTIVE_FAILURES
    # 事件已出队，不再重投。
    assert service._events.peek_ready(key) == []
    # 连续失败计数已清零（下次新事件重新获得完整预算）。
    assert service._task_node_error_failure_streaks.get(key) is None
    # 用户收到一条可见的放弃说明，而非静默空转。
    assert len(notifier_texts) == 1
    assert "不再自动重试" in notifier_texts[0]
    assert "task:26e64d1dc8b3" in notifier_texts[0]
    # 说明以 heartbeat 来源持久化进转录。
    assistant_messages = [m for m in persisted.messages if m.get("role") == "assistant"]
    assert len(assistant_messages) == 1
    assert assistant_messages[0]["metadata"]["reason"] == "task_node_error_give_up"


def test_node_error_streak_resets_on_success(tmp_path: Path) -> None:
    service, prompts, persisted, notifier_texts = _build_service(tmp_path)
    key = "china:qqbot:default:dm"

    assert asyncio.run(service._run_session(key)) == 10.0
    assert asyncio.run(service._run_session(key)) == 10.0
    assert service._task_node_error_failure_streaks.get(key) == 2

    # 任意一次成功回合（换一个不抛错的 prompt 会话）都应清零计数。
    class _OkPromptSession:
        def __init__(self) -> None:
            self.state = SimpleNamespace(status="idle", is_running=False)

        def subscribe(self, relay):
            return lambda: None

        async def prompt(self, user_input, persist_transcript: bool = True):
            return SimpleNamespace(output="HEARTBEAT_OK")

    prompts.state = SimpleNamespace(status="idle", is_running=False)
    service._runtime_manager = _FakeRuntimeManager(_OkPromptSession())
    service._started = True
    service.enqueue_task_node_error_payload(
        key,
        [
            {
                "task_id": "task:26e64d1dc8b3",
                "node_id": "node:85b3119ce0ac",
                "pause_row_id": 35,
                "dedupe_key": "node-error:task:26e64d1dc8b3:node:85b3119ce0ac:35",
            }
        ],
    )
    asyncio.run(service._run_session(key))
    assert service._task_node_error_failure_streaks.get(key) is None