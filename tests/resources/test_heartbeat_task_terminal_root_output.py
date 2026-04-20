from __future__ import annotations

from types import SimpleNamespace

import pytest

from g3ku.core.events import AgentEvent
from g3ku.core.messages import UserInputMessage
from g3ku.heartbeat.prompt_lane import build_heartbeat_prompt_lane
from g3ku.heartbeat.session_service import HEARTBEAT_OK, WebSessionHeartbeatService
from g3ku.session.manager import SessionManager
from main.service.task_terminal_callback import build_task_terminal_payload, enrich_task_terminal_payload


class _Registry:
    def __init__(self) -> None:
        self._seq: dict[str, int] = {}
        self.published: list[tuple[str, dict[str, object]]] = []
        self.global_published: list[dict[str, object]] = []

    async def subscribe_ceo(self, session_id: str):
        _ = session_id
        return None

    async def subscribe_global_ceo(self):
        return None

    async def unsubscribe_ceo(self, session_id: str, queue) -> None:
        _ = session_id, queue

    async def unsubscribe_global_ceo(self, queue) -> None:
        _ = queue

    def next_ceo_seq(self, session_id: str) -> int:
        key = str(session_id or "")
        self._seq[key] = self._seq.get(key, 0) + 1
        return self._seq[key]

    def publish_ceo(self, session_id: str, envelope: dict[str, object]) -> None:
        self.published.append((str(session_id or ""), dict(envelope)))

    def publish_global_ceo(self, envelope: dict[str, object]) -> None:
        self.global_published.append(dict(envelope))


class _TaskService:
    def __init__(self) -> None:
        self.registry = _Registry()
        self.delivered: list[tuple[str, str]] = []
        self.store = SimpleNamespace(mark_task_terminal_outbox_delivered=self._mark_task_terminal_outbox_delivered)
        self.tasks: dict[str, object] = {}
        self.node_details: dict[tuple[str, str], dict[str, object]] = {}

    def _mark_task_terminal_outbox_delivered(self, dedupe_key: str, *, delivered_at: str) -> None:
        self.delivered.append((str(dedupe_key or ""), str(delivered_at or "")))

    def get_task(self, task_id: str):
        return self.tasks.get(str(task_id or "").strip())

    def get_node_detail_payload(self, task_id: str, node_id: str):
        key = (str(task_id or "").strip(), str(node_id or "").strip())
        return self.node_details.get(key)


class _RuntimeManager:
    def __init__(self, session) -> None:
        self._session = session

    def get_or_create(self, **kwargs):
        _ = kwargs
        return self._session


class _FakeHeartbeatSession:
    def __init__(self, *, outputs: list[str] | tuple[str, ...]) -> None:
        self.state = SimpleNamespace(status="idle", is_running=False)
        self.prompts: list[UserInputMessage] = []
        self.persist_transcript_flags: list[bool] = []
        self._listeners = set()
        self._outputs = [str(item or "") for item in list(outputs)]
        self.turn_id = "turn-heartbeat-root-output"

    def subscribe(self, listener):
        self._listeners.add(listener)

        def _unsubscribe() -> None:
            self._listeners.discard(listener)

        return _unsubscribe

    async def _emit(self, event_type: str, **payload) -> None:
        event = AgentEvent(type=event_type, timestamp="2026-03-18T12:00:00", payload=payload)
        for listener in list(self._listeners):
            result = listener(event)
            if hasattr(result, "__await__"):
                await result

    async def prompt(self, user_message, persist_transcript: bool = False) -> SimpleNamespace:
        self.persist_transcript_flags.append(bool(persist_transcript))
        self.prompts.append(user_message)
        output = self._outputs.pop(0) if self._outputs else ""
        heartbeat_reason = str((getattr(user_message, "metadata", None) or {}).get("heartbeat_reason") or "").strip()
        if output:
            await self._emit(
                "message_end",
                role="assistant",
                text=str(output),
                source="heartbeat",
                heartbeat_internal=True,
                heartbeat_reason=heartbeat_reason,
                turn_id=self.turn_id,
            )
        return SimpleNamespace(output=output)


def _task_detail(*, node_id: str, node_kind: str, final_output: str, final_output_ref: str, check_result: str, failure_reason: str) -> dict[str, object]:
    return {
        "item": {
            "node_id": node_id,
            "task_id": "task:demo-acceptance-output",
            "node_kind": node_kind,
            "final_output": final_output,
            "final_output_ref": final_output_ref,
            "check_result": check_result,
            "failure_reason": failure_reason,
        }
    }


def test_enrich_task_terminal_payload_keeps_root_output_when_acceptance_failed() -> None:
    task_id = "task:demo-acceptance-output"
    task = SimpleNamespace(
        task_id=task_id,
        session_id="web:shared",
        title="demo acceptance output task",
        status="failed",
        root_node_id="node:root",
        metadata={
            "final_acceptance": {
                "required": True,
                "prompt": "check the final result",
                "node_id": "node:acceptance",
                "status": "failed",
            }
        },
        final_output="Fallback root output",
        final_output_ref="artifact:artifact:root-fallback",
        failure_reason="Acceptance Failure: evidence mismatch",
        finished_at="2026-03-27T01:35:32+08:00",
        brief_text="acceptance failed",
    )

    def _get_node_detail_payload(current_task_id: str, node_id: str):
        key = (str(current_task_id), str(node_id))
        details = {
            (task_id, "node:root"): _task_detail(
                node_id="node:root",
                node_kind="execution",
                final_output="Root node full output",
                final_output_ref="artifact:artifact:root-output",
                check_result="final acceptance failed",
                failure_reason="",
            ),
            (task_id, "node:acceptance"): _task_detail(
                node_id="node:acceptance",
                node_kind="acceptance",
                final_output="Acceptance node full output",
                final_output_ref="artifact:artifact:accept-output",
                check_result="acceptance failed",
                failure_reason="Acceptance Failure: evidence mismatch",
            ),
        }
        return details.get(key)

    payload = enrich_task_terminal_payload(
        build_task_terminal_payload(task),
        task=task,
        node_detail_getter=_get_node_detail_payload,
    )

    assert payload["terminal_output"] == "Acceptance node full output"
    assert payload["terminal_output_ref"] == "artifact:artifact:accept-output"
    assert payload["root_output"] == "Root node full output"
    assert payload["root_output_ref"] == "artifact:artifact:root-output"


def test_build_heartbeat_prompt_lane_includes_root_output_when_acceptance_failed() -> None:
    lane = build_heartbeat_prompt_lane(
        provider_model="openai:gpt-4.1",
        stable_rules_text="Keep the user informed without exposing internal mechanics.",
        task_ledger_summary="task:demo-acceptance-output failed after final acceptance.",
        events=[
            {
                "reason": "task_terminal",
                "task_id": "task:demo-acceptance-output",
                "title": "demo acceptance output task",
                "status": "failed",
                "brief_text": "acceptance failed",
                "terminal_node_id": "node:acceptance",
                "terminal_node_kind": "acceptance",
                "terminal_node_reason": "acceptance_failed",
                "terminal_output": "Acceptance node full output",
                "terminal_output_ref": "artifact:artifact:accept-output",
                "terminal_check_result": "acceptance failed",
                "terminal_failure_reason": "Acceptance Failure: evidence mismatch",
                "root_output": "Root node full output",
                "root_output_ref": "artifact:artifact:root-output",
            }
        ],
    )

    event_message = next(
        message
        for message in list(lane.request_messages)
        if str(message.get("role") or "").strip().lower() == "user"
    )
    event_text = str(event_message.get("content") or "")

    assert "Result output: Acceptance node full output" in event_text
    assert "Execution output: Root node full output" in event_text
    assert "Execution output ref: artifact:artifact:root-output" in event_text


@pytest.mark.asyncio
async def test_web_session_heartbeat_includes_root_output_when_acceptance_failed(tmp_path) -> None:
    session_id = "web:ceo-heartbeat-task-terminal-root-output"
    session_manager = SessionManager(tmp_path)
    persisted = session_manager.get_or_create(session_id)
    session_manager.save(persisted)
    live_session = _FakeHeartbeatSession(outputs=[HEARTBEAT_OK, "I have read both the acceptance failure and the execution deliverable."])
    task_service = _TaskService()
    task_id = "task:demo-acceptance-output"
    task_service.tasks[task_id] = SimpleNamespace(
        task_id=task_id,
        root_node_id="node:root",
        metadata={
            "final_acceptance": {
                "required": True,
                "prompt": "check the final result",
                "node_id": "node:acceptance",
                "status": "failed",
            }
        },
        final_output="Execution Deliverable: root answer",
        final_output_ref="artifact:artifact:root-output",
        failure_reason="Acceptance Failure: evidence mismatch",
    )
    task_service.node_details[(task_id, "node:root")] = _task_detail(
        node_id="node:root",
        node_kind="execution",
        final_output="Root node full output",
        final_output_ref="artifact:artifact:root-output",
        check_result="final acceptance failed",
        failure_reason="",
    )
    task_service.node_details[(task_id, "node:acceptance")] = _task_detail(
        node_id="node:acceptance",
        node_kind="acceptance",
        final_output="Acceptance node full output",
        final_output_ref="artifact:artifact:accept-output",
        check_result="acceptance failed",
        failure_reason="Acceptance Failure: evidence mismatch",
    )
    service = WebSessionHeartbeatService(
        workspace=tmp_path,
        agent=SimpleNamespace(tool_execution_manager=None),
        runtime_manager=_RuntimeManager(live_session),
        main_task_service=task_service,
        session_manager=session_manager,
    )

    accepted = service.enqueue_task_terminal_payload(
        {
            "task_id": task_id,
            "session_id": session_id,
            "title": "demo acceptance output task",
            "status": "failed",
            "brief_text": "acceptance failed",
            "failure_reason": "Acceptance Failure: evidence mismatch",
            "finished_at": "2026-03-27T01:35:32+08:00",
            "dedupe_key": "task-terminal:task:demo-acceptance-output:failed:2026-03-27T01:35:32+08:00",
        }
    )
    assert accepted is True
    service._started = True

    next_delay = await service._run_session(session_id)

    assert next_delay is None
    prompt_text = str(live_session.prompts[0].content)
    assert "Result output: Acceptance node full output" in prompt_text
    assert "Execution output: Root node full output" in prompt_text
    assert "Execution output ref: artifact:artifact:root-output" in prompt_text
