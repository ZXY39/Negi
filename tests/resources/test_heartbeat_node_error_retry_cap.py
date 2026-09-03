from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from g3ku.heartbeat.session_service import (
    WebSessionHeartbeatService,
    _TASK_NODE_ERROR_MAX_CONSECUTIVE_FAILURES,
    _NODE_ERROR_BACKOFF_BASE_SECONDS,
    _NODE_ERROR_BACKOFF_CAP_SECONDS,
)


class _BoomPromptSession:
    """Runtime session whose prompt() always fails（模拟 provider 配额死掉、模型无响应）。"""

    def __init__(self) -> None:
        self.state = SimpleNamespace(status="idle", is_running=False)
        self.prompt_calls = 0

    def subscribe(self, relay) -> object:
        return lambda: None

    async def prompt(self, user_input, persist_transcript: bool = True):
        self.prompt_calls += 1
        raise RuntimeError("boom")


class _OkPromptSession:
    """Runtime session whose prompt() succeeds（模型成功响应）。"""

    def __init__(self) -> None:
        self.state = SimpleNamespace(status="idle", is_running=False)
        self.prompt_calls = 0

    def subscribe(self, relay):
        return lambda: None

    async def prompt(self, user_input, persist_transcript: bool = True):
        self.prompt_calls += 1
        return SimpleNamespace(output="HEARTBEAT_OK")


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
    def __init__(self, session) -> None:
        self._session = session

    def get_or_create(self, **kwargs):
        return self._session


class _FakeRetryStore:
    """内存模拟 heartbeat_node_retry_state 表（按 node_id 持久计数）。"""

    def __init__(self) -> None:
        self.rows: dict[str, dict] = {}

    def get_heartbeat_node_retry_state(self, node_id: str):
        row = self.rows.get(str(node_id or "").strip())
        return dict(row) if row is not None else None

    def record_heartbeat_node_failure(self, node_id, *, task_id="", session_id="", next_eligible_at="", escalated=False) -> int:
        clean = str(node_id or "").strip()
        if not clean:
            return 0
        existing = self.rows.get(clean) or {}
        count = int(existing.get("consecutive_failures") or 0) + 1
        self.rows[clean] = {
            "node_id": clean,
            "task_id": str(task_id or "").strip() or str(existing.get("task_id") or "").strip(),
            "session_id": str(session_id or "").strip() or str(existing.get("session_id") or "").strip(),
            "consecutive_failures": count,
            "first_failure_at": str(existing.get("first_failure_at") or "2026-09-03T14:51:59+08:00"),
            "last_attempt_at": "2026-09-03T14:52:00+08:00",
            "next_eligible_at": str(next_eligible_at or "").strip(),
            "escalated": 1 if (escalated or int(existing.get("escalated") or 0)) else 0,
            "updated_at": "2026-09-03T14:52:00+08:00",
        }
        return count

    def reset_heartbeat_node_retry_state(self, node_id: str) -> None:
        self.rows.pop(str(node_id or "").strip(), None)


def _node_error_item(node_id="node:85b3119ce0ac", task_id="task:26e64d1dc8b3", pause_row_id=34):
    return {
        "task_id": task_id,
        "node_id": node_id,
        "pause_row_id": pause_row_id,
        "task_title": "日报任务",
        "node_title": "日报节点",
        "pause_reason": "error",
        "error_text": "RuntimeError: Error:",
        "dedupe_key": f"node-error:{task_id}:{node_id}:{pause_row_id}",
    }


def _build_service(tmp_path: Path, *, store=None):
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
        main_task_service=SimpleNamespace(registry=None, store=store),
        session_manager=_FakeSessionManager(persisted, exists_path),
        reply_notifier=_notify,
    )
    service._started = True
    return service, prompts, persisted, notifier_texts, key


def _backoff_sequence(n: int) -> list[float]:
    return [
        min(_NODE_ERROR_BACKOFF_CAP_SECONDS, _NODE_ERROR_BACKOFF_BASE_SECONDS * (2.0 ** (i - 1)))
        for i in range(1, n + 1)
    ]


def test_persisted_count_backs_off_and_escalates_at_cap(tmp_path: Path) -> None:
    store = _FakeRetryStore()
    service, prompts, persisted, notifier_texts, key = _build_service(tmp_path, store=store)
    service.enqueue_task_node_error_payload(key, [_node_error_item()])

    cap = _TASK_NODE_ERROR_MAX_CONSECUTIVE_FAILURES
    returns = [asyncio.run(service._run_session(key)) for _ in range(cap)]

    # 退避 1→2→4→5→5 分钟，事件不出队（持续按退避重投，不再"放弃即停"）。
    assert returns == _backoff_sequence(cap)
    assert prompts.prompt_calls == cap
    assert service._events.peek_ready(key) != []
    # 持久计数累积到上限、未在 give-up 时清零。
    assert store.rows["node:85b3119ce0ac"]["consecutive_failures"] == cap
    assert store.rows["node:85b3119ce0ac"]["escalated"] == 1
    # 刚跨入上限时落一条请用户裁决的可见说明（升级文案，非旧"避免反复打扰"）。
    assert len(notifier_texts) == 1
    assert f"连续 {cap} 次无法启动" in notifier_texts[0]
    assert "是否判为失败让任务继续进行" in notifier_texts[0]
    assert "task:26e64d1dc8b3" in notifier_texts[0]
    assistant_messages = [m for m in persisted.messages if m.get("role") == "assistant"]
    assert len(assistant_messages) == 1
    assert assistant_messages[0]["metadata"]["reason"] == "task_node_error_escalation"


def test_escalation_emitted_once_not_every_failure(tmp_path: Path) -> None:
    store = _FakeRetryStore()
    service, prompts, persisted, notifier_texts, key = _build_service(tmp_path, store=store)
    service.enqueue_task_node_error_payload(key, [_node_error_item()])

    cap = _TASK_NODE_ERROR_MAX_CONSECUTIVE_FAILURES
    for _ in range(cap + 2):  # 跨过上限后再失败两次
        asyncio.run(service._run_session(key))

    # 计数继续累积（6、7），但升级说明只在跨入上限那次落一次，不每轮刷屏。
    assert store.rows["node:85b3119ce0ac"]["consecutive_failures"] == cap + 2
    assert len(notifier_texts) == 1
    # 退避封顶 5 分钟。
    assert service._events.peek_ready(key) != []


def test_repause_same_node_does_not_reopen_budget(tmp_path: Path) -> None:
    """节点重新暂停（新 pause_row_id、同 node_id）不重开预算——计数按 node 持久累积。"""
    store = _FakeRetryStore()
    service, prompts, persisted, notifier_texts, key = _build_service(tmp_path, store=store)
    service.enqueue_task_node_error_payload(key, [_node_error_item(pause_row_id=34)])

    cap = _TASK_NODE_ERROR_MAX_CONSECUTIVE_FAILURES
    for _ in range(cap):
        asyncio.run(service._run_session(key))
    assert store.rows["node:85b3119ce0ac"]["consecutive_failures"] == cap

    # 模拟节点被 resume 后再次失败：新的 pause 行（不同 pause_row_id），同 node_id。
    service.enqueue_task_node_error_payload(key, [_node_error_item(pause_row_id=99)])
    asyncio.run(service._run_session(key))
    # 计数从持久值继续（cap+1），而非重开为 1。
    assert store.rows["node:85b3119ce0ac"]["consecutive_failures"] == cap + 1


def test_successful_turn_resets_persisted_count(tmp_path: Path) -> None:
    store = _FakeRetryStore()
    service, prompts, persisted, notifier_texts, key = _build_service(tmp_path, store=store)
    service.enqueue_task_node_error_payload(key, [_node_error_item()])

    asyncio.run(service._run_session(key))  # 失败 1
    asyncio.run(service._run_session(key))  # 失败 2
    assert store.rows["node:85b3119ce0ac"]["consecutive_failures"] == 2

    # 模型成功响应（换一个不抛错的 prompt 会话）→ 清零该 node 的持久计数。
    service._runtime_manager = _FakeRuntimeManager(_OkPromptSession())
    asyncio.run(service._run_session(key))
    assert store.get_heartbeat_node_retry_state("node:85b3119ce0ac") is None


def test_per_node_count_isolation(tmp_path: Path) -> None:
    """计数按 node 独立：递增/清零只影响对应 node，不串到别的 node（取代旧的按 session 单计数）。"""
    store = _FakeRetryStore()
    service, prompts, persisted, notifier_texts, key = _build_service(tmp_path, store=store)

    def ev(node_id, pause_row_id=1):
        return SimpleNamespace(
            reason="task_node_error",
            payload={"node_id": node_id, "task_id": "task:x"},
            event_id=f"{node_id}:{pause_row_id}",
            dedupe_key=f"node-error:task:x:{node_id}:{pause_row_id}",
        )

    node_a, node_b = "node:aaaaaaaa", "node:bbbbbbbb"
    service._record_node_error_failures([ev(node_a)], key)
    service._record_node_error_failures([ev(node_a)], key)
    assert store.rows[node_a]["consecutive_failures"] == 2
    service._record_node_error_failures([ev(node_b)], key)
    assert store.rows[node_b]["consecutive_failures"] == 1
    assert store.rows[node_a]["consecutive_failures"] == 2  # A 不受 B 影响

    # 同一批里同一 node 的多个事件只递增一次。
    service._record_node_error_failures([ev(node_a, 1), ev(node_a, 2)], key)
    assert store.rows[node_a]["consecutive_failures"] == 3

    # 清零只作用于本批事件涉及的 node。
    service._reset_node_error_retry_state([ev(node_b)], key)
    assert store.get_heartbeat_node_retry_state(node_b) is None
    assert store.rows[node_a]["consecutive_failures"] == 3  # A 保留


def test_in_memory_fallback_when_store_unavailable(tmp_path: Path) -> None:
    """store 不可用时退回内存计数保底，仍按退避重投、达上限升级。"""
    service, prompts, persisted, notifier_texts, key = _build_service(tmp_path, store=None)
    service.enqueue_task_node_error_payload(key, [_node_error_item()])

    cap = _TASK_NODE_ERROR_MAX_CONSECUTIVE_FAILURES
    returns = [asyncio.run(service._run_session(key)) for _ in range(cap)]
    assert returns == _backoff_sequence(cap)
    assert service._task_node_error_failure_streaks.get(key) == cap
    assert len(notifier_texts) == 1
    assert "是否判为失败让任务继续进行" in notifier_texts[0]


def test_enrich_event_payload_adds_retry_state_and_previous_errors(tmp_path: Path) -> None:
    """2.4：事件束注入按 node 的重试状态 + 历史错误（去重、newest first、≤3 条）。"""
    store = _FakeRetryStore()
    store.list_task_node_error_logs = lambda task_id, node_id: [
        SimpleNamespace(error_text="old error A", created_at="2026-09-03T14:50:00+08:00"),
        SimpleNamespace(error_text="old error A", created_at="2026-09-03T14:50:30+08:00"),  # 重复文本
        SimpleNamespace(error_text="old error B", created_at="2026-09-03T14:51:00+08:00"),
    ]
    service, prompts, persisted, notifier_texts, key = _build_service(tmp_path, store=store)
    cap = _TASK_NODE_ERROR_MAX_CONSECUTIVE_FAILURES
    for _ in range(cap):
        store.record_heartbeat_node_failure("node:n", task_id="task:t", session_id=key)

    ev = SimpleNamespace(
        reason="task_node_error",
        payload={"task_id": "task:t", "node_id": "node:n", "node_title": "N", "error_text": "boom"},
        event_id="e1",
        dedupe_key="node-error:task:t:node:n:1",
    )
    payload = service._enrich_event_payload_for_lane(ev)

    assert payload["event_reason"] == "task_node_error"
    assert payload["retry_attempt"] == cap
    assert payload["retry_cap"] == cap
    assert payload["retry_escalated"] is True
    # 历史错误去重 + newest first（list_task_node_error_logs 返回 seq ASC，注入时 reversed）
    assert [p["text"] for p in payload["previous_errors"]] == ["old error B", "old error A"]


def test_enrich_event_payload_passthrough_for_non_node_error(tmp_path: Path) -> None:
    """非 task_node_error 事件不富化，原样透传 payload + event_reason。"""
    store = _FakeRetryStore()
    service, prompts, persisted, notifier_texts, key = _build_service(tmp_path, store=store)
    ev = SimpleNamespace(
        reason="task_stall",
        payload={"task_id": "task:t", "stalled_minutes": 20},
        event_id="e2",
        dedupe_key="task-stall:t",
    )
    payload = service._enrich_event_payload_for_lane(ev)
    assert payload["event_reason"] == "task_stall"
    assert "retry_attempt" not in payload
    assert payload["stalled_minutes"] == 20
