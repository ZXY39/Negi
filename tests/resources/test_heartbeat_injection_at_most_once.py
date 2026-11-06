from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from g3ku.core.messages import UserInputMessage
from g3ku.heartbeat.session_service import WebSessionHeartbeatService
from g3ku.runtime.session_agent import InternalPromptInjectionError, RuntimeAgentSession
from g3ku.runtime.web_ceo_sessions import injected_heartbeat_event_keys

KEY = "ext:qq-official-1903529517:f8a8001865631301"


class _FakeStore:
    def __init__(self) -> None:
        self.terminal_acks: list[str] = []
        self.stall_acks: list[str] = []
        self.frontdoor_marks: list[str] = []
        self.node_marks: list[int] = []
        self.shutdown_marks: list[tuple[str, str]] = []

    def mark_task_terminal_outbox_delivered(self, dedupe_key: str, *, delivered_at: str) -> None:
        _ = delivered_at
        key = str(dedupe_key)
        if key not in self.terminal_acks:
            self.terminal_acks.append(key)

    def mark_task_stall_outbox_delivered(self, dedupe_key: str, *, delivered_at: str) -> None:
        _ = delivered_at
        key = str(dedupe_key)
        if key not in self.stall_acks:
            self.stall_acks.append(key)

    def mark_frontdoor_node_message_consumed(self, message_id: str, *, consumed_at: str) -> None:
        _ = consumed_at
        self.frontdoor_marks.append(str(message_id))

    def mark_task_node_pause_delivered(self, pause_id: int) -> None:
        self.node_marks.append(int(pause_id))

    def mark_shutdown_pause_entry_consumed(self, *, kind: str, ref_id: str) -> bool:
        self.shutdown_marks.append((str(kind), str(ref_id)))
        return True


class _FakePersistedSession:
    def __init__(self) -> None:
        self.metadata: dict = {}
        self.messages: list[dict] = []
        self.updated_at = ""

    def add_message(self, role: str, content: str, **kwargs) -> dict:
        message = {"role": role, "content": content, **kwargs}
        self.messages.append(message)
        return message

    def save(self) -> None:
        return None


class _FakeSessionManager:
    def __init__(self, session: _FakePersistedSession, path: Path) -> None:
        self._session = session
        self._path = path
        self.saves = 0

    def get_path(self, session_id: str) -> Path:
        _ = session_id
        return self._path

    def get_or_create(self, session_id: str) -> _FakePersistedSession:
        _ = session_id
        return self._session

    def save(self, session) -> None:
        _ = session
        self.saves += 1


class _FakeRuntimeManager:
    def __init__(self, session) -> None:
        self._session = session

    def get_or_create(self, **kwargs):
        _ = kwargs
        return self._session


class _InjectThenBoomSession:
    """模拟真实 prompt_heartbeat：先注入、回调消费，再让模型调用失败。"""

    def __init__(self, persisted: _FakePersistedSession) -> None:
        self.state = SimpleNamespace(status="idle", is_running=False)
        self.prompt_calls = 0
        self._persisted = persisted

    def subscribe(self, relay) -> object:
        _ = relay
        return lambda: None

    async def prompt_heartbeat(self, user_input, *, on_injected=None, already_injected=False):
        assert not already_injected
        self.prompt_calls += 1
        metadata = dict(getattr(user_input, "metadata", None) or {})
        keys = list(metadata.get("heartbeat_event_dedupe_keys") or [])
        self._persisted.add_message(
            "user",
            str(getattr(user_input, "content", "") or ""),
            metadata={
                "internal_prompt_kind": "heartbeat_event_bundle",
                "prompt_visible": True,
                "ui_visible": False,
                "heartbeat_event_dedupe_keys": keys,
            },
        )
        if callable(on_injected):
            await on_injected(
                SimpleNamespace(
                    event_bundle_added=True,
                    event_bundle_already_present=False,
                    persisted=True,
                    event_dedupe_keys=tuple(keys),
                )
            )
        raise RuntimeError("model chain failed after injection")


class _RepairSession:
    """首次注入后返回空正文，repair 跳不再注入、直接给出可见回复。"""

    def __init__(self, persisted: _FakePersistedSession) -> None:
        self.state = SimpleNamespace(status="idle", is_running=False)
        self.prompt_calls = 0
        self.already_injected_calls = 0
        self._persisted = persisted

    def subscribe(self, relay) -> object:
        _ = relay
        return lambda: None

    async def prompt_heartbeat(self, user_input, *, on_injected=None, already_injected=False):
        self.prompt_calls += 1
        if already_injected:
            self.already_injected_calls += 1
            return SimpleNamespace(output="最终可见回复")
        metadata = dict(getattr(user_input, "metadata", None) or {})
        keys = list(metadata.get("heartbeat_event_dedupe_keys") or [])
        self._persisted.add_message(
            "user",
            str(getattr(user_input, "content", "") or ""),
            metadata={
                "internal_prompt_kind": "heartbeat_event_bundle",
                "prompt_visible": True,
                "ui_visible": False,
                "heartbeat_event_dedupe_keys": keys,
            },
        )
        if callable(on_injected):
            await on_injected(
                SimpleNamespace(
                    event_bundle_added=True,
                    event_bundle_already_present=False,
                    persisted=True,
                    event_dedupe_keys=tuple(keys),
                )
            )
        return SimpleNamespace(output="")


class _InjectFailsSession:
    """模拟注入失败：事件束没有进入上下文，服务应保留事件重试。"""

    def __init__(self) -> None:
        self.state = SimpleNamespace(status="idle", is_running=False)
        self.prompt_calls = 0

    def subscribe(self, relay) -> object:
        _ = relay
        return lambda: None

    async def prompt_heartbeat(self, user_input, *, on_injected=None, already_injected=False):
        _ = user_input, on_injected, already_injected
        self.prompt_calls += 1
        raise InternalPromptInjectionError("injection failed")


def _build_service(tmp_path: Path, session, persisted: _FakePersistedSession, store: _FakeStore):
    path = tmp_path / "session.jsonl"
    path.write_text("", encoding="utf-8")
    service = WebSessionHeartbeatService(
        workspace=tmp_path,
        agent=SimpleNamespace(),
        runtime_manager=_FakeRuntimeManager(session),
        main_task_service=SimpleNamespace(registry=None, store=store),
        session_manager=_FakeSessionManager(persisted, path),
    )
    service._started = True
    return service


def _enqueue(service: WebSessionHeartbeatService, *, reason: str, dedupe_key: str, payload: dict):
    event = service._events.enqueue(
        session_id=KEY,
        source="test",
        reason=reason,
        dedupe_key=dedupe_key,
        payload=payload,
    )
    assert event is not None
    return event


def _bundle_messages(persisted: _FakePersistedSession) -> list[dict]:
    return [
        message
        for message in persisted.messages
        if (message.get("metadata") or {}).get("internal_prompt_kind") == "heartbeat_event_bundle"
    ]


def test_injection_success_then_model_failure_does_not_redeliver(tmp_path: Path) -> None:
    persisted = _FakePersistedSession()
    store = _FakeStore()
    session = _InjectThenBoomSession(persisted)
    service = _build_service(tmp_path, session, persisted, store)
    event = _enqueue(
        service,
        reason="task_terminal",
        dedupe_key="task-terminal:task:x:success:2026-11-01T17:03:47+08:00",
        payload={"task_id": "task:x", "status": "success", "brief": "done"},
    )

    first_delay = asyncio.run(service._run_session(KEY))

    assert session.prompt_calls == 1
    assert len(_bundle_messages(persisted)) == 1
    assert service._events.peek_ready(KEY) == []
    assert service._events.enqueue(
        session_id=KEY,
        source="test",
        reason="task_terminal",
        dedupe_key=event.dedupe_key,
        payload=event.payload,
    ) is None
    assert store.terminal_acks == [event.dedupe_key]
    assert persisted.metadata.get("handled_terminal_dedupe_keys") == [event.dedupe_key]
    assert service._non_node_error_failure_streaks == {}
    assert first_delay is None

    # 同一事件不会再有第二回合。模型失败不回到注入点，也不叠第二份事件束。
    assert asyncio.run(service._run_session(KEY)) is None
    assert session.prompt_calls == 1
    assert len(_bundle_messages(persisted)) == 1


def test_repair_turn_reuses_injected_bundle_without_second_injection(tmp_path: Path) -> None:
    persisted = _FakePersistedSession()
    store = _FakeStore()
    session = _RepairSession(persisted)
    service = _build_service(tmp_path, session, persisted, store)
    _enqueue(
        service,
        reason="task_terminal",
        dedupe_key="task-terminal:task:x:success:2026-11-01T17:03:47+08:00",
        payload={"task_id": "task:x", "status": "success", "brief": "done"},
    )

    assert asyncio.run(service._run_session(KEY)) is None

    assert session.prompt_calls == 2
    assert session.already_injected_calls == 1
    assert len(_bundle_messages(persisted)) == 1
    assert store.terminal_acks == ["task-terminal:task:x:success:2026-11-01T17:03:47+08:00"]
    assistant_messages = [m for m in persisted.messages if m.get("role") == "assistant"]
    assert assistant_messages
    assert assistant_messages[-1]["content"] == "最终可见回复"


def test_injection_failure_keeps_event_and_does_not_mark_source(tmp_path: Path) -> None:
    persisted = _FakePersistedSession()
    store = _FakeStore()
    session = _InjectFailsSession()
    service = _build_service(tmp_path, session, persisted, store)
    _enqueue(
        service,
        reason="task_terminal",
        dedupe_key="task-terminal:task:x:success:2026-11-01T17:03:47+08:00",
        payload={"task_id": "task:x", "status": "success", "brief": "done"},
    )

    delay = asyncio.run(service._run_session(KEY))

    assert session.prompt_calls == 1
    assert _bundle_messages(persisted) == []
    assert store.terminal_acks == []
    assert service._events.peek_ready(KEY) != []
    assert delay == 60.0
    assert service._non_node_error_failure_streaks[
        (KEY, ("task_terminal",))
    ] == 1


@pytest.mark.parametrize(
    ("reason", "dedupe_key", "payload", "expected"),
    [
        (
            "task_terminal",
            "task-terminal:task:x:success:2026-11-01T17:03:47+08:00",
            {"task_id": "task:x", "status": "success"},
            "terminal",
        ),
        (
            "task_stall",
            "task-stall:task:x:20:2026-11-01T16:00:00+08:00",
            {"task_id": "task:x", "bucket_minutes": 20},
            "stall",
        ),
        (
            "frontdoor_node_message",
            "frontdoor-node-message:m1",
            {"message_id": "m1"},
            "frontdoor",
        ),
        (
            "task_node_error",
            "node-error:task:x:node:n:34",
            {"task_id": "task:x", "node_id": "node:n", "pause_row_id": 34},
            "node",
        ),
        (
            "shutdown_resume",
            "shutdown-resume:ext:test",
            {"session_id": KEY},
            "shutdown",
        ),
        (
            "task_distribution_error",
            "distribution-error:task:x",
            {"task_id": "task:x"},
            "queue_only",
        ),
        (
            "task_distribution_skipped",
            "distribution-skipped:task:x",
            {"task_id": "task:x"},
            "queue_only",
        ),
        (
            "delivery_failure",
            "delivery-failure:obx-1",
            {"outbox_id": "obx-1"},
            "queue_only",
        ),
        (
            "attachment_failure",
            "attachment-failure:obx-2",
            {"outbox_id": "obx-2", "attachment_failures": [{"name": "a.docx"}]},
            "queue_only",
        ),
    ],
)
def test_consume_at_injection_marks_every_source_once(
    tmp_path: Path,
    reason: str,
    dedupe_key: str,
    payload: dict,
    expected: str,
) -> None:
    persisted = _FakePersistedSession()
    store = _FakeStore()
    service = _build_service(tmp_path, _InjectFailsSession(), persisted, store)
    event = _enqueue(service, reason=reason, dedupe_key=dedupe_key, payload=payload)

    service._consume_heartbeat_events_at_injection(KEY, [event], persisted)

    assert service._events.peek_ready(KEY) == []
    assert service._events.enqueue(
        session_id=KEY,
        source="test",
        reason=reason,
        dedupe_key=dedupe_key,
        payload=payload,
    ) is None
    if expected == "terminal":
        assert store.terminal_acks == [dedupe_key]
    elif expected == "stall":
        assert store.stall_acks == [dedupe_key]
    elif expected == "frontdoor":
        assert store.frontdoor_marks == ["m1"]
    elif expected == "node":
        assert store.node_marks == [34]
    elif expected == "shutdown":
        assert store.shutdown_marks == [("session", KEY)]
    else:
        assert store.terminal_acks == []
        assert store.stall_acks == []


def test_attachment_failure_does_not_suppress_the_channel_reply() -> None:
    """附件失败与投递失败是两条 reason：前者正文已送达，模型必须还能回话/重发。"""
    assert WebSessionHeartbeatService._events_are_delivery_failure_only(
        [SimpleNamespace(reason="delivery_failure")]
    )
    assert not WebSessionHeartbeatService._events_are_delivery_failure_only(
        [SimpleNamespace(reason="attachment_failure")]
    )
    assert not WebSessionHeartbeatService._events_are_delivery_failure_only(
        [
            SimpleNamespace(reason="attachment_failure"),
            SimpleNamespace(reason="delivery_failure"),
        ]
    )


def test_already_injected_event_is_consumed_without_a_model_turn(tmp_path: Path) -> None:
    persisted = _FakePersistedSession()
    persisted.add_message(
        "user",
        "old bundle",
        metadata={
            "internal_prompt_kind": "heartbeat_event_bundle",
            "prompt_visible": True,
            "ui_visible": False,
            "heartbeat_event_dedupe_keys": ["frontdoor-node-message:m1"],
        },
    )
    store = _FakeStore()
    session = _InjectThenBoomSession(persisted)
    service = _build_service(tmp_path, session, persisted, store)
    _enqueue(
        service,
        reason="frontdoor_node_message",
        dedupe_key="frontdoor-node-message:m1",
        payload={"message_id": "m1"},
    )

    assert asyncio.run(service._run_session(KEY)) is None

    assert session.prompt_calls == 0
    assert len(_bundle_messages(persisted)) == 1
    assert service._events.peek_ready(KEY) == []
    assert store.frontdoor_marks == ["m1"]


def test_node_error_injection_success_does_not_retry_or_escalate(tmp_path: Path) -> None:
    persisted = _FakePersistedSession()
    store = _FakeStore()
    session = _InjectThenBoomSession(persisted)
    service = _build_service(tmp_path, session, persisted, store)
    event = _enqueue(
        service,
        reason="task_node_error",
        dedupe_key="node-error:task:x:node:n:34",
        payload={
            "task_id": "task:x",
            "node_id": "node:n",
            "node_title": "node n",
            "pause_row_id": 34,
            "error_text": "boom",
        },
    )

    asyncio.run(service._run_session(KEY))

    assert session.prompt_calls == 1
    assert store.node_marks == [34]
    assert service._events.peek_ready(KEY) == []
    assert service._task_node_error_failure_streaks == {}
    assert service._non_node_error_failure_streaks == {}
    assert len(_bundle_messages(persisted)) == 1
    assert event.dedupe_key not in (persisted.metadata.get("handled_terminal_dedupe_keys") or [])


def _build_session_agent(persisted: _FakePersistedSession):
    class _Sessions:
        def __init__(self) -> None:
            self.saves = 0

        def get_or_create(self, session_key: str) -> _FakePersistedSession:
            _ = session_key
            return persisted

        def save(self, session) -> None:
            _ = session
            self.saves += 1

    class _Agent(RuntimeAgentSession):
        async def _prompt_locked(self, user_input, **kwargs):
            _ = user_input
            self.locked_calls += 1
            self.locked_kwargs = dict(kwargs)
            return SimpleNamespace(output="ok")

    agent = object.__new__(_Agent)
    agent._loop = SimpleNamespace(sessions=_Sessions())
    agent._state = SimpleNamespace(session_key=KEY)
    agent._active_turn_id = None
    agent._turn_lock = asyncio.Lock()
    agent.locked_calls = 0
    agent.locked_kwargs = {}
    return agent


def _heartbeat_input(*, text: str = "bundle", keys: list[str] | None = None) -> UserInputMessage:
    return UserInputMessage(
        content=text,
        metadata={
            "heartbeat_internal": True,
            "heartbeat_stable_rules_text": "rules",
            "heartbeat_event_bundle_text": text,
            "heartbeat_event_dedupe_keys": list(keys or ["k1"]),
        },
    )


def test_session_agent_injection_result_is_idempotent_and_keeps_one_bundle() -> None:
    persisted = _FakePersistedSession()
    agent = _build_session_agent(persisted)
    injected = []

    result = asyncio.run(
        agent.prompt_heartbeat(
            _heartbeat_input(),
            on_injected=lambda value: _collect(injected, value),
        )
    )

    assert result.output == "ok"
    assert len(injected) == 1
    assert injected[0].event_bundle_added is True
    assert agent.locked_kwargs.get("internal_prompt_already_persisted") is True
    assert agent.locked_kwargs.get("preserve_internal_event_bundle") is True
    assert len(_bundle_messages(persisted)) == 1
    assert len([m for m in persisted.messages if (m.get("metadata") or {}).get("internal_prompt_kind") == "heartbeat_rule"]) == 1

    second = asyncio.run(
        agent._persist_internal_prompt_messages(
            user_input=_heartbeat_input(),
            internal_source="heartbeat",
        )
    )
    assert second is not None
    assert second.event_bundle_already_present is True
    assert second.event_bundle_added is False
    assert len(_bundle_messages(persisted)) == 1

    # 同一 key 再注入：只认已存在，不加第二份；repair 路径不再注入、不再回调。
    agent.locked_calls = 0
    asyncio.run(agent.prompt_heartbeat(_heartbeat_input(), already_injected=True))
    assert agent.locked_calls == 1
    assert len(_bundle_messages(persisted)) == 1


def test_session_agent_prompt_heartbeat_raises_before_model_when_not_injected() -> None:
    persisted = _FakePersistedSession()
    agent = _build_session_agent(persisted)
    injected = []

    with pytest.raises(InternalPromptInjectionError):
        asyncio.run(
            agent.prompt_heartbeat(
                _heartbeat_input(text=""),
                on_injected=lambda value: _collect(injected, value),
            )
        )

    assert injected == []
    assert agent.locked_calls == 0
    assert _bundle_messages(persisted) == []


async def _collect(target: list, value) -> None:
    target.append(value)


def test_failed_heartbeat_turn_preserves_event_bundle_but_discards_rules() -> None:
    persisted = _FakePersistedSession()
    agent = _build_session_agent(persisted)
    persisted.add_message(
        "user",
        "rules",
        metadata={
            "internal_prompt_kind": "heartbeat_rule",
            "_transcript_turn_id": "t1",
            "prompt_visible": True,
        },
    )
    persisted.add_message(
        "user",
        "bundle",
        metadata={
            "internal_prompt_kind": "heartbeat_event_bundle",
            "_transcript_turn_id": "t1",
            "prompt_visible": True,
            "heartbeat_event_dedupe_keys": ["k1"],
        },
    )

    flipped = agent._discard_internal_prompt_messages(
        persisted,
        "t1",
        preserve_kinds={"heartbeat_event_bundle"},
    )

    assert flipped == 1
    assert (persisted.messages[0].get("metadata") or {}).get("_transcript_state") == "discarded"
    assert (persisted.messages[1].get("metadata") or {}).get("_transcript_state") != "discarded"
    assert injected_heartbeat_event_keys(persisted) == {"k1"}
