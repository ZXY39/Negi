from __future__ import annotations

from types import SimpleNamespace

import pytest

from g3ku.core.events import AgentEvent
from g3ku.core.messages import UserInputMessage
from g3ku.heartbeat.prompt_lane import build_heartbeat_prompt_lane
from g3ku.heartbeat.session_service import HEARTBEAT_OK, WebSessionHeartbeatService
from g3ku.session.manager import SessionManager
from main.service.task_terminal_callback import (
    build_task_terminal_payload,
    enrich_task_terminal_payload,
)


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


def test_silent_reply_token_exact_match_only() -> None:
    from g3ku.runtime.reply_tokens import SILENT_REPLY_TOKEN, is_silent_reply_token

    assert SILENT_REPLY_TOKEN == "[G3KU_SILENT]"
    assert is_silent_reply_token("[G3KU_SILENT]")
    assert is_silent_reply_token("  [G3KU_SILENT]\n")
    assert not is_silent_reply_token("")
    assert not is_silent_reply_token("[G3KU_SILENT] 附言")
    assert not is_silent_reply_token("请保持[G3KU_SILENT]")


async def test_task_terminal_silent_reply_token_acks_without_visible_reply(tmp_path) -> None:
    """模型对 task_terminal 输出 [G3KU_SILENT]（runtime 归一化为 output=''+is_silent_reply）时，
    走静默 ACK：不投递 ceo.reply.final、不触发修复循环、不落兜底文案。"""
    session_id = "web:ceo-heartbeat-task-terminal-silent-reply"
    session_manager = SessionManager(tmp_path)
    persisted = session_manager.get_or_create(session_id)
    session_manager.save(persisted)

    class _SilentReplySession(_FakeHeartbeatSession):
        async def prompt(self, user_message, persist_transcript: bool = False) -> SimpleNamespace:
            self.persist_transcript_flags.append(bool(persist_transcript))
            self.prompts.append(user_message)
            if self._outputs:
                self._outputs.pop(0)
            return SimpleNamespace(output="", is_silent_reply=True)

    live_session = _SilentReplySession(outputs=["[G3KU_SILENT]"])
    task_service = _TaskService()
    task_id = "task:demo-silent-reply"
    task_service.tasks[task_id] = SimpleNamespace(
        task_id=task_id,
        root_node_id="node:root",
        metadata={},
        final_output="silent deliverable",
        final_output_ref="",
        failure_reason="",
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
            "title": "demo silent reply task",
            "status": "success",
            "brief_text": "done silently",
            "failure_reason": "",
            "finished_at": "2026-03-27T01:35:32+08:00",
            "dedupe_key": "task-terminal:task:demo-silent-reply:success:2026-03-27T01:35:32+08:00",
        }
    )
    assert accepted is True
    service._started = True

    next_delay = await service._run_session(session_id)

    assert next_delay is None
    published_types = [env.get("type") for _, env in task_service.registry.published]
    assert "ceo.reply.final" not in published_types
    assert "ceo.internal.ack" in published_types
    # 静默 token 不走修复循环（只发生首次那一次 prompt）
    assert len(live_session.prompts) == 1


def _supplement_store_fixture() -> SimpleNamespace:
    """伪 store：一条 completed epoch（目标 node:child-a / node:child-b）供补充收集。"""

    def _epoch(epoch_id: str, state: str) -> SimpleNamespace:
        return SimpleNamespace(
            epoch_id=epoch_id,
            state=state,
            root_message="全树口径更新",
            created_at="2026-03-27T01:20:00+08:00",
            payload={
                "queued_root_messages": ["全树口径更新", "补充 b 的要求"],
                "target_node_ids": ["node:child-a", "node:child-b"],
            },
        )

    epochs = [_epoch("epoch:done", "completed"), _epoch("epoch:aborted", "failed")]

    def _list_active_task_message_distribution_epochs(task_id: str):
        return epochs if str(task_id) == "task:demo-supplements" else []

    def _get_node(node_id: str):
        titles = {"node:child-a": "抓取西安市 AI 相关岗位", "node:child-b": "branch b"}
        return SimpleNamespace(goal=titles.get(str(node_id), ""))

    return SimpleNamespace(list_active_task_message_distribution_epochs=_list_active_task_message_distribution_epochs, get_node=_get_node)


def test_terminal_payload_collects_user_node_supplements() -> None:
    from main.service.task_terminal_callback import collect_task_user_node_supplements

    task = SimpleNamespace(task_id="task:demo-supplements")
    supplements = collect_task_user_node_supplements(task.task_id, store=_supplement_store_fixture())
    # 失败 epoch 跳过；completed epoch 的 2 个目标 × 2 条消息 = 4 行。
    assert len(supplements) == 4
    assert {str(item["node_id"]) for item in supplements} == {"node:child-a", "node:child-b"}
    child_a_rows = [item for item in supplements if item["node_id"] == "node:child-a"]
    assert {str(item["message"]) for item in child_a_rows} == {"全树口径更新", "补充 b 的要求"}
    assert child_a_rows[0]["node_title"] == "抓取西安市 AI 相关岗位"
    assert child_a_rows[0]["epoch_state"] == "completed"


def test_enrich_task_terminal_payload_renders_user_node_supplements_in_lane() -> None:
    from main.service.task_terminal_callback import (
        collect_task_user_node_supplements,
        enrich_task_terminal_payload,
    )

    task_id = "task:demo-supplements"
    task = SimpleNamespace(
        task_id=task_id,
        session_id="web:shared",
        title="demo supplements task",
        status="success",
        root_node_id="node:root",
        metadata={},
        final_output="root final output",
        final_output_ref="",
        failure_reason="",
        finished_at="2026-03-27T01:35:32+08:00",
        brief_text="done",
    )
    payload = enrich_task_terminal_payload(
        build_task_terminal_payload(task),
        task=task,
        supplement_getter=lambda current_task_id: collect_task_user_node_supplements(
            current_task_id, store=_supplement_store_fixture()
        ),
    )

    supplements = list(payload.get("user_node_supplements") or [])
    assert len(supplements) == 4
    assert {str(item["node_id"]) for item in supplements} == {"node:child-a", "node:child-b"}

    lane = build_heartbeat_prompt_lane(
        provider_model="openai:gpt-4.1",
        stable_rules_text="Keep the user informed without exposing internal mechanics.",
        events=[payload],
    )
    event_message = next(
        message
        for message in list(lane.request_messages)
        if str(message.get("role") or "").strip().lower() == "user"
    )
    event_text = str(event_message.get("content") or "")
    assert "User node supplements" in event_text
    assert "Node 抓取西安市 AI 相关岗位 (node:child-a): 全树口径更新" in event_text
    assert "Node branch b (node:child-b): 补充 b 的要求" in event_text
    # 内容排在任务结果之后（事件块末尾提醒）。
    assert event_text.index("User node supplements") > event_text.index("Result output")


def test_normalize_task_terminal_payload_preserves_worker_supplements() -> None:
    from main.service.task_terminal_callback import normalize_task_terminal_payload

    worker_payload = {
        "task_id": "task:demo-supplements",
        "session_id": "web:shared",
        "title": "demo supplements task",
        "status": "success",
        "finished_at": "2026-03-27T01:35:32+08:00",
        "user_node_supplements": [
            {"node_id": "node:child-a", "node_title": "child a", "message": "补充要求"},
            {"node_id": "", "message": "drop me"},
            {"nodeTitle": "no id either", "message": "drop me too"},
        ],
    }
    normalized = normalize_task_terminal_payload(worker_payload)
    assert normalized["user_node_supplements"] == [
        {"node_id": "node:child-a", "node_title": "child a", "message": "补充要求", "epoch_id": "", "created_at": "", "epoch_state": ""},
    ]

    # 无 getter 可用（--no-worker 拓扑：web 读不到 worker 库）：enrich 保留上游预计算值。
    enriched = enrich_task_terminal_payload(normalized, task=None, task_getter=lambda task_id: None)
    assert enriched["user_node_supplements"] == normalized["user_node_supplements"]
