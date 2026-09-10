from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from g3ku.agent.tools.tool_execution_control import StopToolExecutionTool
from g3ku.providers.base import normalize_usage_payload
from g3ku.runtime.frontdoor.message_builder import CeoMessageBuilder
from g3ku.runtime.frontdoor.prompt_builder import CeoPromptBuilder
from g3ku.runtime.tool_watchdog import request_tool_cancellation
from g3ku.runtime.web_ceo_sessions import persist_frontdoor_actual_request
from main.protocol import build_envelope, now_iso
from main.runtime.chat_backend import build_prompt_cache_diagnostics


# 首档巡检窗口（秒）：在此之前侧车道完全静默，不产生任何提醒判定。
DEFAULT_FIRST_CHECK_SECONDS: float = 120.0
# 模型自定的下一次巡检间隔钳制范围（秒）：防 0 值忙循环与超大值变相永不巡检。
MIN_NEXT_CHECK_SECONDS: float = 30.0
MAX_NEXT_CHECK_SECONDS: float = 600.0


def clamp_next_check_seconds(value: Any) -> float | None:
    """把模型给出的下次巡检间隔归一并钳制到合法范围；无效返回 None（沿用上一轮间隔）。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        parsed = float(value)
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            parsed = float(text)
        except ValueError:
            return None
    else:
        return None
    if parsed != parsed or parsed in (float("inf"), float("-inf")):
        return None
    return max(MIN_NEXT_CHECK_SECONDS, min(MAX_NEXT_CHECK_SECONDS, parsed))


@dataclass(slots=True)
class InlineToolStopDecisionMetadata:
    reason_code: str
    decision_source: str
    elapsed_seconds_at_stop: float
    reminder_count: int
    window_seconds: float
    model_decision_excerpt: str = ""

    def as_dict(self) -> dict[str, Any]:
        payload = {
            "reason_code": str(self.reason_code or "").strip(),
            "decision_source": str(self.decision_source or "").strip(),
            "elapsed_seconds_at_stop": round(float(self.elapsed_seconds_at_stop or 0.0), 1),
            "reminder_count": int(self.reminder_count or 0),
            "window_seconds": round(float(self.window_seconds or 0.0), 1),
        }
        excerpt = str(self.model_decision_excerpt or "").strip()
        if excerpt:
            payload["model_decision_excerpt"] = excerpt
        return payload


@dataclass(slots=True)
class CeoReminderDecision:
    decision: str
    label: str
    model_decision_excerpt: str = ""
    # 模型自定的下一次巡检间隔（秒）；None 表示沿用上一轮间隔。
    next_check_seconds: float | None = None
    # 判定来源：model（LLM 判定）/ deterministic_skip（确定性观察跳过）/ unavailable。
    source: str = "model"


@dataclass(slots=True)
class CeoReminderSnapshot:
    session_key: str
    turn_id: str
    source: str
    status: str
    user_message: dict[str, Any]
    assistant_text: str
    visible_canonical_context: dict[str, Any]
    frontdoor_canonical_context: dict[str, Any]
    compression: dict[str, Any]
    semantic_context_state: dict[str, Any]
    hydrated_tool_names: list[str]
    frontdoor_selection_debug: dict[str, Any]
    frontdoor_actual_request_path: str
    active_tool_observation: dict[str, Any]

    @classmethod
    def from_payload(cls, payload: dict[str, Any] | None) -> "CeoReminderSnapshot | None":
        if not isinstance(payload, dict):
            return None
        return cls(
            session_key=str(payload.get("session_key") or "").strip(),
            turn_id=str(payload.get("turn_id") or "").strip(),
            source=str(payload.get("source") or "").strip(),
            status=str(payload.get("status") or "").strip(),
            user_message=dict(payload.get("user_message") or {}),
            assistant_text=str(payload.get("assistant_text") or "").strip(),
            visible_canonical_context=dict(payload.get("visible_canonical_context") or {}),
            frontdoor_canonical_context=dict(payload.get("frontdoor_canonical_context") or {}),
            compression=dict(payload.get("compression") or {}),
            semantic_context_state=dict(payload.get("semantic_context_state") or {}),
            hydrated_tool_names=[
                str(item or "").strip()
                for item in list(payload.get("hydrated_tool_names") or [])
                if str(item or "").strip()
            ],
            frontdoor_selection_debug=dict(payload.get("frontdoor_selection_debug") or {}),
            frontdoor_actual_request_path=str(payload.get("frontdoor_actual_request_path") or "").strip(),
            active_tool_observation=dict(payload.get("active_tool_observation") or {}),
        )


@dataclass(slots=True)
class InlineToolExecutionRecord:
    execution_id: str
    session_key: str
    turn_id: str
    tool_name: str
    tool_call_id: str
    task: asyncio.Task[Any]
    snapshot_supplier: Callable[[], Any] | None
    cancel_token: Any | None
    started_at: float
    arguments: dict[str, Any] | None = None
    runtime_session: Any | None = None
    reminder_count: int = 0
    state: str = "running"
    stop_decision_metadata: InlineToolStopDecisionMetadata | None = None
    reminder_task: asyncio.Task[Any] | None = None
    reminder_visible: bool = False
    # 自定排程巡检：下一次巡检的 monotonic 时刻与上一轮使用的间隔；
    # 确定性跳过轮与判定失败轮沿用上一轮间隔，不产生模型调用。
    next_check_at: float = 0.0
    last_check_interval_seconds: float = DEFAULT_FIRST_CHECK_SECONDS
    # 判定上下文统计：放行次数按来源分列，距上次放行的时长用于进展速率判断。
    llm_continue_count: int = 0
    skip_continue_count: int = 0
    last_continue_at: float = 0.0
    # 本次调用的最大运行时长（统一 timeout 合同）；侧车道只在其内排程。
    timeout_seconds: float | None = None


async def _maybe_await(value: Any) -> Any:
    if hasattr(value, "__await__"):
        return await value
    return value


def _task_terminal_state(task: asyncio.Task[Any]) -> str:
    if not task.done():
        return "running"
    if task.cancelled():
        return "cancelled"
    try:
        error = task.exception()
    except Exception:
        return "failed"
    if error is not None:
        return "failed"
    return "completed"


def _coerce_stop_decision_metadata(
    value: InlineToolStopDecisionMetadata | dict[str, Any] | None,
) -> InlineToolStopDecisionMetadata | None:
    if value is None:
        return None
    if isinstance(value, InlineToolStopDecisionMetadata):
        return value
    return InlineToolStopDecisionMetadata(
        reason_code=str(value.get("reason_code") or "").strip(),
        decision_source=str(value.get("decision_source") or "").strip(),
        elapsed_seconds_at_stop=float(value.get("elapsed_seconds_at_stop") or 0.0),
        reminder_count=int(value.get("reminder_count") or 0),
        window_seconds=float(value.get("window_seconds") or 0.0),
        model_decision_excerpt=str(value.get("model_decision_excerpt") or "").strip(),
    )


def build_timeout_stop_error_text(
    *,
    tool_name: str,
    stop_decision_metadata: InlineToolStopDecisionMetadata | dict[str, Any] | None,
) -> str:
    payload = (
        stop_decision_metadata.as_dict()
        if isinstance(stop_decision_metadata, InlineToolStopDecisionMetadata)
        else dict(stop_decision_metadata or {})
    )
    elapsed_seconds = float(payload.get("elapsed_seconds_at_stop") or 0.0)
    reminder_count = int(payload.get("reminder_count") or 0)
    normalized_tool_name = str(tool_name or "tool").strip() or "tool"
    base = (
        f"Error executing {normalized_tool_name}: stopped by sidecar timeout decision "
        f"after {elapsed_seconds:.1f}s ({reminder_count} reminders)."
    )
    return (
        f"{base} The running tool was actively stopped because the sidecar reminder judged "
        "further waiting was not worthwhile."
    )


class InlineToolExecutionRegistry:
    def __init__(self, *, first_check_seconds: float = DEFAULT_FIRST_CHECK_SECONDS) -> None:
        self._counter = 0
        self._lock = asyncio.Lock()
        self._executions: dict[str, InlineToolExecutionRecord] = {}
        self._reminder_service: CeoToolReminderService | None = None
        self._first_check_seconds = max(1.0, float(first_check_seconds or DEFAULT_FIRST_CHECK_SECONDS))

    @property
    def first_check_seconds(self) -> float:
        return self._first_check_seconds

    def attach_reminder_service(self, service: "CeoToolReminderService") -> None:
        self._reminder_service = service

    async def register_execution(
        self,
        *,
        session_key: str,
        turn_id: str,
        tool_name: str,
        tool_call_id: str,
        arguments: dict[str, Any] | None,
        task: asyncio.Task[Any],
        snapshot_supplier: Callable[[], Any] | None,
        cancel_token: Any | None,
        started_at: float,
        runtime_session: Any | None,
        timeout_seconds: float | None = None,
    ) -> InlineToolExecutionRecord:
        async with self._lock:
            for current in self._executions.values():
                if current.task is task:
                    return current
            self._counter += 1
            normalized_started_at = float(started_at)
            record = InlineToolExecutionRecord(
                execution_id=f"inline-tool-exec:{self._counter}",
                session_key=str(session_key or "").strip(),
                turn_id=str(turn_id or "").strip(),
                tool_name=str(tool_name or "tool").strip() or "tool",
                tool_call_id=str(tool_call_id or "").strip(),
                arguments=dict(arguments or {}),
                task=task,
                snapshot_supplier=snapshot_supplier if callable(snapshot_supplier) else None,
                cancel_token=cancel_token,
                started_at=normalized_started_at,
                runtime_session=runtime_session,
                next_check_at=normalized_started_at + self._first_check_seconds,
                last_check_interval_seconds=self._first_check_seconds,
                last_continue_at=normalized_started_at,
                timeout_seconds=float(timeout_seconds) if timeout_seconds else None,
            )
            self._executions[record.execution_id] = record
        task.add_done_callback(lambda _task, key=record.execution_id: self._schedule_terminal_cleanup(key))
        if self._reminder_service is not None:
            record.reminder_task = self._reminder_service.start_execution(record)
        return record

    async def get(self, execution_id: str) -> InlineToolExecutionRecord | None:
        async with self._lock:
            return self._executions.get(str(execution_id or "").strip())

    def stop_decision_metadata(self, execution_id: str) -> dict[str, Any] | None:
        record = self._executions.get(str(execution_id or "").strip())
        if record is None or record.stop_decision_metadata is None:
            return None
        return record.stop_decision_metadata.as_dict()

    async def stop_execution(
        self,
        execution_id: str,
        *,
        reason: str = "agent_requested_stop",
        stop_grace_seconds: float = 2.0,
        stop_decision_metadata: InlineToolStopDecisionMetadata | dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        normalized_id = str(execution_id or "").strip()
        record = await self.get(normalized_id)
        if record is None:
            return {
                "status": "not_found",
                "execution_id": normalized_id,
                "message": "inline tool execution not found",
            }
        if _task_terminal_state(record.task) == "completed":
            record.stop_decision_metadata = None
            record.state = "completed"
            return {
                "status": "completed",
                "execution_id": normalized_id,
                "tool_name": record.tool_name,
                "target_type": "inline_tool",
                "message": "inline execution already finished before stop could be applied",
            }
        record.stop_decision_metadata = _coerce_stop_decision_metadata(stop_decision_metadata)
        record.state = "stopping"
        await request_tool_cancellation(
            record.task,
            cancel_token=record.cancel_token,
            reason=str(reason or "agent_requested_stop").strip() or "agent_requested_stop",
            grace_seconds=stop_grace_seconds,
        )
        if _task_terminal_state(record.task) == "completed":
            record.stop_decision_metadata = None
            record.state = "completed"
            return {
                "status": "completed",
                "execution_id": normalized_id,
                "tool_name": record.tool_name,
                "target_type": "inline_tool",
                "message": "inline execution completed before the stop decision took effect",
            }
        payload = {
            "status": "stopped",
            "execution_id": normalized_id,
            "tool_name": record.tool_name,
            "target_type": "inline_tool",
            "message": "inline execution stop requested",
        }
        if record.stop_decision_metadata is not None:
            payload["stop_decision_metadata"] = record.stop_decision_metadata.as_dict()
        return payload

    async def discard_execution(self, execution_id: str) -> None:
        normalized_id = str(execution_id or "").strip()
        async with self._lock:
            record = self._executions.pop(normalized_id, None)
        if record is None:
            return
        if self._reminder_service is not None and record.reminder_visible:
            await self._reminder_service.execution_finished(record)
        if record.reminder_task is not None and not record.reminder_task.done():
            record.reminder_task.cancel()

    def _schedule_terminal_cleanup(self, execution_id: str) -> None:
        key = str(execution_id or "").strip()
        if not key:
            return
        try:
            asyncio.get_running_loop().create_task(self._handle_terminal_cleanup(key))
        except RuntimeError:
            return

    async def _handle_terminal_cleanup(self, execution_id: str) -> None:
        record = await self.get(execution_id)
        if record is None:
            return
        task_state = _task_terminal_state(record.task)
        if task_state == "completed":
            record.stop_decision_metadata = None
            record.state = "completed"
        elif record.stop_decision_metadata is not None:
            record.state = "stopped"
        elif task_state == "failed":
            record.state = "failed"
        else:
            record.state = "completed"
        if record.reminder_task is not None and not record.reminder_task.done():
            record.reminder_task.cancel()
        if self._reminder_service is not None:
            await self._reminder_service.execution_finished(record)


class CeoToolReminderService:
    def __init__(
        self,
        *,
        loop: Any,
        registry: InlineToolExecutionRegistry,
    ) -> None:
        self._loop = loop
        self._registry = registry
        self._registry.attach_reminder_service(self)
        self._prompt_builder = CeoPromptBuilder(loop=loop)
        self._builder = CeoMessageBuilder(loop=loop, prompt_builder=self._prompt_builder)
        self._support = None

    def start_execution(self, record: InlineToolExecutionRecord) -> asyncio.Task[Any]:
        return asyncio.create_task(self._run(record.execution_id), name=f"ceo-inline-reminder:{record.execution_id}")

    @staticmethod
    def _actual_request_record_from_path(path_text: str) -> dict[str, Any]:
        path = Path(str(path_text or "").strip())
        if not path.exists():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        return dict(payload) if isinstance(payload, dict) else {}

    @classmethod
    def _actual_request_record_for_reminder(
        cls,
        *,
        snapshot: CeoReminderSnapshot | None,
        runtime_session: Any | None,
    ) -> dict[str, Any]:
        path_text = ""
        if snapshot is not None:
            path_text = str(snapshot.frontdoor_actual_request_path or "").strip()
        if not path_text and runtime_session is not None:
            path_text = str(getattr(runtime_session, "_frontdoor_actual_request_path", "") or "").strip()
        return cls._actual_request_record_from_path(path_text)

    @staticmethod
    def _prompt_message_records(messages: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
        return [dict(item) for item in list(messages or []) if isinstance(item, dict)]

    @staticmethod
    def _tool_schema_records(tool_schemas: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
        return [dict(item) for item in list(tool_schemas or []) if isinstance(item, dict)]

    @staticmethod
    def _parse_text_decision(value: Any) -> tuple[str, float | None]:
        """解析判定回复：返回 (decision, next_check_seconds)。

        支持三种形态：
        - JSON：{"decision": "stop|continue", "next_check_seconds": N}
        - 单行令牌：`STOP` 或 `CONTINUE <seconds>`（如 `CONTINUE 300`）
        - 首词匹配兜底
        无效的秒数归一为 None（由调用方沿用上一轮间隔）。
        """
        raw = str(CeoToolReminderService._content_text(value) or "").strip()
        if not raw:
            return "", None
        compact = raw.strip()
        if compact.startswith("{"):
            try:
                parsed = json.loads(compact)
            except Exception:
                parsed = None
            if isinstance(parsed, dict):
                decision = str(parsed.get("decision") or "").strip().lower()
                if decision in {"stop", "continue"}:
                    next_seconds = clamp_next_check_seconds(parsed.get("next_check_seconds"))
                    return decision, next_seconds
        for line in compact.splitlines():
            stripped_line = str(line or "").strip()
            if not stripped_line:
                continue
            token = re.sub(r"[^A-Z_]+", "", stripped_line.upper())
            if token == "STOP":
                return "stop", None
            upper_line = stripped_line.upper()
            if upper_line.startswith("CONTINUE"):
                number_match = re.search(r"(\d+(?:\.\d+)?)", stripped_line[len("CONTINUE"):])
                next_seconds = clamp_next_check_seconds(number_match.group(1)) if number_match else None
                return "continue", next_seconds
        match = re.match(r"^\s*([A-Za-z_]+)", compact)
        if not match:
            return "", None
        token = re.sub(r"[^A-Z_]+", "", match.group(1).upper())
        if token == "STOP":
            return "stop", None
        if token == "CONTINUE":
            return "continue", None
        return "", None

    async def _decide_from_actual_request_scaffold(
        self,
        *,
        record: InlineToolExecutionRecord,
        snapshot: CeoReminderSnapshot,
        actual_request_record: dict[str, Any],
        reminder_messages: list[dict[str, Any]],
    ) -> CeoReminderDecision | None:
        request_messages = self._prompt_message_records(
            actual_request_record.get("request_messages") or actual_request_record.get("messages")
        )
        if not request_messages:
            return None
        provider_request_body = dict(actual_request_record.get("provider_request_body") or {})
        provider_tool_schemas = self._tool_schema_records(provider_request_body.get("tools"))
        tool_schemas = (
            list(provider_tool_schemas)
            if provider_tool_schemas
            else self._tool_schema_records(actual_request_record.get("tool_schemas"))
        )
        model_refs = [
            str(item or "").strip()
            for item in list(actual_request_record.get("model_refs") or []) 
            if str(item or "").strip()
        ] or self._resolve_ceo_model_refs()
        parallel_tool_calls = actual_request_record.get("parallel_tool_calls")
        normalized_parallel_tool_calls = (
            bool(parallel_tool_calls)
            if isinstance(parallel_tool_calls, bool)
            else None
        )
        prompt_cache_key = str(actual_request_record.get("prompt_cache_key") or "").strip() or None
        chat_backend = self._resolve_chat_backend()
        response = await chat_backend.chat(
            messages=[*request_messages, *reminder_messages],
            tools=(list(tool_schemas) if tool_schemas else None),
            model_refs=model_refs,
            parallel_tool_calls=normalized_parallel_tool_calls,
            prompt_cache_key=prompt_cache_key,
        )
        self._persist_frontdoor_internal_request_artifact(
            session_key=record.session_key,
            turn_id=record.turn_id,
            request_messages=[*request_messages, *reminder_messages],
            tool_schemas=list(tool_schemas or []),
            model_refs=model_refs,
            parallel_tool_calls=normalized_parallel_tool_calls,
            prompt_cache_key=str(prompt_cache_key or ""),
            prompt_cache_diagnostics=build_prompt_cache_diagnostics(
                stable_messages=list(request_messages),
                dynamic_appendix_messages=list(reminder_messages),
                tool_schemas=list(tool_schemas or []),
                provider_model=str((list(model_refs or []) or [""])[0] or "").strip(),
                scope="ceo_inline_tool_reminder",
                prompt_cache_key=str(prompt_cache_key or ""),
                actual_request_messages=[*request_messages, *reminder_messages],
                actual_tool_schemas=list(tool_schemas or []),
            ),
            provider_request_meta=(
                dict(getattr(response, "provider_request_meta", {}) or {})
                if isinstance(getattr(response, "provider_request_meta", None), dict)
                else {}
            ),
            provider_request_body=(
                dict(getattr(response, "provider_request_body", {}) or {})
                if isinstance(getattr(response, "provider_request_body", None), dict)
                else {}
            ),
            usage=normalize_usage_payload(getattr(response, "usage", None)),
            request_lane="inline_tool_reminder",
            parent_request_id=str(actual_request_record.get("request_id") or "").strip(),
        )
        decision_excerpt = self._content_text(getattr(response, "content", "")).strip()
        parsed_decision, parsed_next_seconds = self._parse_text_decision(decision_excerpt)
        elapsed_seconds = max(0.0, time.monotonic() - record.started_at)
        if parsed_decision == "stop":
            return CeoReminderDecision(
                decision="stop",
                label=self._stop_label(
                    tool_name=record.tool_name,
                    elapsed_seconds=elapsed_seconds,
                    reminder_count=record.reminder_count,
                ),
                model_decision_excerpt=decision_excerpt,
            )
        if parsed_decision == "continue":
            return CeoReminderDecision(
                decision="continue",
                label=self._continue_label(
                    tool_name=record.tool_name,
                    elapsed_seconds=elapsed_seconds,
                    reminder_count=record.reminder_count,
                ),
                model_decision_excerpt=decision_excerpt,
                next_check_seconds=parsed_next_seconds,
            )
        return None

    async def execution_finished(self, record: InlineToolExecutionRecord) -> None:
        if not record.reminder_visible:
            return
        record.reminder_visible = False
        self._publish(
            session_id=record.session_key,
            event_type="ceo.tool.reminder",
            data={
                "turn_id": record.turn_id,
                "execution_id": record.execution_id,
                "tool_name": record.tool_name,
                "source": "reminder",
                "terminal": True,
            },
        )

    async def _run(self, execution_id: str) -> None:
        while True:
            record = await self._registry.get(execution_id)
            if record is None or record.state != "running":
                return
            # 自定排程：下一次巡检时刻由上一轮判定给出（首档为注册时的配置值）。
            delay_seconds = max(0.0, record.next_check_at - time.monotonic())
            try:
                await asyncio.sleep(delay_seconds)
            except asyncio.CancelledError:
                return
            record = await self._registry.get(execution_id)
            if record is None or record.state != "running":
                return
            record.reminder_count += 1
            elapsed_seconds = round(max(0.0, time.monotonic() - record.started_at), 1)
            try:
                decision = await self._decide(record=record)
            except Exception:
                decision = CeoReminderDecision(
                    decision="unavailable",
                    label=self._unavailable_label(
                        tool_name=record.tool_name,
                        elapsed_seconds=elapsed_seconds,
                        reminder_count=record.reminder_count,
                    ),
                    source="unavailable",
                )
            if decision.decision == "stop":
                try:
                    stop_payload = await self._registry.stop_execution(
                        record.execution_id,
                        reason="sidecar_timeout_stop",
                        stop_decision_metadata=InlineToolStopDecisionMetadata(
                            reason_code="sidecar_timeout_stop",
                            decision_source="sidecar_reminder",
                            elapsed_seconds_at_stop=elapsed_seconds,
                            reminder_count=record.reminder_count,
                            window_seconds=record.last_check_interval_seconds,
                            model_decision_excerpt=decision.model_decision_excerpt,
                        ),
                    )
                except Exception:
                    stop_payload = {"status": "failed"}
                stop_status = str((stop_payload or {}).get("status") or "").strip().lower()
                if stop_status == "stopped":
                    record.reminder_visible = True
                    self._publish(
                        session_id=record.session_key,
                        event_type="ceo.tool.reminder",
                        data={
                            "turn_id": record.turn_id,
                            "execution_id": record.execution_id,
                            "tool_name": record.tool_name,
                            "elapsed_seconds": elapsed_seconds,
                            "reminder_count": int(record.reminder_count),
                            "decision": "stop",
                            "label": str(decision.label or "").strip(),
                            "source": "reminder",
                        },
                    )
                    return
                decision = CeoReminderDecision(
                    decision="unavailable",
                    label=self._unavailable_label(
                        tool_name=record.tool_name,
                        elapsed_seconds=elapsed_seconds,
                        reminder_count=record.reminder_count,
                    ),
                    source="unavailable",
                )
            # 排下一轮巡检：模型给了间隔就用（钳制后），否则沿用上一轮间隔——
            # 确定性跳过轮与判定失败轮因此零模型调用地延续节奏。
            next_interval = clamp_next_check_seconds(decision.next_check_seconds)
            if next_interval is None:
                next_interval = record.last_check_interval_seconds or self._registry.first_check_seconds
            record.last_check_interval_seconds = next_interval
            record.next_check_at = time.monotonic() + next_interval
            if str(decision.decision or "continue").strip() == "continue":
                if decision.source == "deterministic_skip":
                    record.skip_continue_count += 1
                elif decision.source == "model":
                    record.llm_continue_count += 1
                record.last_continue_at = time.monotonic()
            record.reminder_visible = True
            self._publish(
                session_id=record.session_key,
                event_type="ceo.tool.reminder",
                data={
                    "turn_id": record.turn_id,
                    "execution_id": record.execution_id,
                    "tool_name": record.tool_name,
                    "elapsed_seconds": elapsed_seconds,
                    "reminder_count": int(record.reminder_count),
                    "decision": str(decision.decision or "continue").strip() or "continue",
                    "label": str(decision.label or "").strip(),
                    "source": "reminder",
                    "next_check_in_seconds": round(float(next_interval), 1),
                },
            )

    async def _decide(self, *, record: InlineToolExecutionRecord) -> CeoReminderDecision:
        snapshot_supplier = getattr(record.runtime_session, "reminder_context_snapshot", None)
        snapshot_payload = snapshot_supplier() if callable(snapshot_supplier) else None
        snapshot = CeoReminderSnapshot.from_payload(snapshot_payload)
        elapsed_seconds = max(0.0, time.monotonic() - record.started_at)
        if snapshot is None or record.runtime_session is None:
            return CeoReminderDecision(
                decision="unavailable",
                label=self._unavailable_label(
                    tool_name=record.tool_name,
                    elapsed_seconds=elapsed_seconds,
                    reminder_count=record.reminder_count,
                ),
                source="unavailable",
            )
        deterministic_decision = self._deterministic_observation_decision(
            record=record,
            snapshot=snapshot,
            elapsed_seconds=elapsed_seconds,
        )
        if deterministic_decision is not None:
            return deterministic_decision
        since_last_continue = max(0.0, time.monotonic() - record.last_continue_at)
        timeout_note = (
            f" The call's maximum runtime is {record.timeout_seconds:.0f}s."
            if record.timeout_seconds
            else ""
        )
        reminder_messages: list[dict[str, Any]] = [
            {
                "role": "assistant",
                "content": (
                    f"The running tool `{record.tool_name}` has been executing for {elapsed_seconds:.0f}s.{timeout_note} "
                    f"Reminders so far: {record.reminder_count}; you chose to keep waiting {record.llm_continue_count} time(s), "
                    f"automatic observation skips kept waiting {record.skip_continue_count} time(s); "
                    f"{since_last_continue:.0f}s have passed since the last keep-waiting decision."
                ),
            },
            {
                "role": "user",
                "content": (
                    "This is a live-only reminder. Decide only whether to keep waiting or stop the running tool call. "
                    "Reply with exactly one line: `STOP`, or `CONTINUE <seconds>` where <seconds> is when to check "
                    "the tool again (between 30 and 600). Example: `CONTINUE 300` means re-check in 300 seconds. "
                    "Do not start any new tool chain. Do not call any tools."
                ),
            },
        ]
        tool_context_payload = {
            "tool_name": record.tool_name,
            "tool_call_id": record.tool_call_id,
            "arguments": dict(record.arguments or {}),
            "active_tool_observation": dict(snapshot.active_tool_observation or {}),
        }
        reminder_messages.insert(
            1,
            {
                "role": "assistant",
                "content": "Current tool context:\n" + json.dumps(tool_context_payload, ensure_ascii=False),
            },
        )
        if snapshot.assistant_text:
            reminder_messages.insert(
                2,
                {
                    "role": "assistant",
                    "content": f"Current visible assistant draft before this reminder:\n{snapshot.assistant_text}",
                },
            )
        actual_request_record = self._actual_request_record_for_reminder(
            snapshot=snapshot,
            runtime_session=record.runtime_session,
        )
        if actual_request_record:
            scaffold_decision = await self._decide_from_actual_request_scaffold(
                record=record,
                snapshot=snapshot,
                actual_request_record=actual_request_record,
                reminder_messages=reminder_messages,
            )
            if scaffold_decision is not None:
                return scaffold_decision
        persisted_session = None
        sessions = getattr(self._loop, "sessions", None)
        if sessions is not None and hasattr(sessions, "get_or_create"):
            persisted_session = sessions.get_or_create(record.session_key)
        assembly = await self._builder.build_for_ceo(
            session=record.runtime_session,
            query_text=str(snapshot.user_message.get("content") or record.tool_name).strip() or record.tool_name,
            exposure={
                "skills": [],
                "tool_families": [],
                "tool_names": ["stop_tool_execution"],
            },
            persisted_session=persisted_session,
            user_content=str(snapshot.user_message.get("content") or record.tool_name).strip() or record.tool_name,
            frontdoor_stage_state=dict(snapshot.visible_canonical_context or {}),
            frontdoor_canonical_context=dict(snapshot.frontdoor_canonical_context or {}),
            semantic_context_state=dict(snapshot.semantic_context_state or {}),
            hydrated_tool_names=list(snapshot.hydrated_tool_names or []),
            ephemeral_tail_messages=reminder_messages,
        )
        stop_tool = StopToolExecutionTool(
            lambda: getattr(self._loop, "tool_execution_manager", None),
            task_service_getter=lambda: getattr(self._loop, "main_task_service", None),
            inline_registry_getter=lambda: getattr(self._loop, "inline_tool_execution_registry", None),
        )
        chat_backend = self._resolve_chat_backend()
        response = await chat_backend.chat(
            messages=list(assembly.model_messages),
            tools=[stop_tool.to_schema()],
            model_refs=self._resolve_ceo_model_refs(),
            parallel_tool_calls=False,
        )
        model_refs = self._resolve_ceo_model_refs()
        self._persist_frontdoor_internal_request_artifact(
            session_key=record.session_key,
            turn_id=record.turn_id,
            request_messages=list(assembly.model_messages),
            tool_schemas=[stop_tool.to_schema()],
            model_refs=model_refs,
            parallel_tool_calls=False,
            prompt_cache_key="",
            prompt_cache_diagnostics=build_prompt_cache_diagnostics(
                stable_messages=list(getattr(assembly, "stable_messages", None) or assembly.model_messages),
                dynamic_appendix_messages=list(getattr(assembly, "dynamic_appendix_messages", None) or []),
                tool_schemas=[stop_tool.to_schema()],
                provider_model=str((list(model_refs or []) or [""])[0] or "").strip(),
                scope="ceo_inline_tool_reminder",
                prompt_cache_key="",
                actual_request_messages=list(assembly.model_messages),
                actual_tool_schemas=[stop_tool.to_schema()],
            ),
            provider_request_meta=(
                dict(getattr(response, "provider_request_meta", {}) or {})
                if isinstance(getattr(response, "provider_request_meta", None), dict)
                else {}
            ),
            provider_request_body=(
                dict(getattr(response, "provider_request_body", {}) or {})
                if isinstance(getattr(response, "provider_request_body", None), dict)
                else {}
            ),
            usage=normalize_usage_payload(getattr(response, "usage", None)),
            request_lane="inline_tool_reminder",
        )
        tool_calls = list(getattr(response, "tool_calls", None) or [])
        decision_excerpt = self._content_text(getattr(response, "content", "")).strip()
        for item in tool_calls:
            if self._tool_call_name(item) != "stop_tool_execution":
                continue
            return CeoReminderDecision(
                decision="stop",
                label=self._stop_label(
                    tool_name=record.tool_name,
                    elapsed_seconds=elapsed_seconds,
                    reminder_count=record.reminder_count,
                ),
                model_decision_excerpt=decision_excerpt,
            )
        return CeoReminderDecision(
            decision="continue",
            label=self._continue_label(
                tool_name=record.tool_name,
                elapsed_seconds=elapsed_seconds,
                reminder_count=record.reminder_count,
            ),
            model_decision_excerpt=decision_excerpt,
        )

    def _deterministic_observation_decision(
        self,
        *,
        record: InlineToolExecutionRecord,
        snapshot: CeoReminderSnapshot,
        elapsed_seconds: float,
    ) -> CeoReminderDecision | None:
        observation = dict(snapshot.active_tool_observation or {})
        hard_error_text = str(
            observation.get("hard_error_text")
            or observation.get("stderr_tail")
            or ""
        ).strip()
        if hard_error_text and any(marker in hard_error_text for marker in ("ERR_", "Navigation failed", "net::")):
            return CeoReminderDecision(
                decision="continue",
                label=(
                    f"The running tool `{record.tool_name}` has been active for {elapsed_seconds:.0f}s. "
                    "A concrete tool error is already visible, so the sidecar will keep waiting and preserve the tool's native failure."
                ),
                model_decision_excerpt="deterministic_continue_hard_error",
                source="deterministic_skip",
            )
        if (
            bool(observation.get("positive_progress"))
            or str(observation.get("current_url") or "").strip()
            or str(observation.get("page_title") or "").strip()
        ):
            return CeoReminderDecision(
                decision="continue",
                label=(
                    f"The running tool `{record.tool_name}` has been active for {elapsed_seconds:.0f}s. "
                    "Positive progress is already visible, so the sidecar will continue waiting."
                ),
                model_decision_excerpt="deterministic_continue_positive_progress",
                source="deterministic_skip",
            )
        return None

    @staticmethod
    def _tool_call_name(item: Any) -> str:
        if isinstance(item, dict):
            function = item.get("function")
            if isinstance(function, dict):
                return str(function.get("name") or item.get("name") or "").strip()
            return str(item.get("name") or "").strip()
        return str(getattr(item, "name", None) or getattr(getattr(item, "function", None), "name", "") or "").strip()

    @staticmethod
    def _continue_label(*, tool_name: str, elapsed_seconds: float, reminder_count: int) -> str:
        return (
            f"The running tool `{tool_name}` has been active for {elapsed_seconds:.0f}s. "
            f"This is reminder #{int(reminder_count)}. Decision: continue waiting."
        )

    @staticmethod
    def _stop_label(*, tool_name: str, elapsed_seconds: float, reminder_count: int) -> str:
        return (
            f"The running tool `{tool_name}` has been active for {elapsed_seconds:.0f}s. "
            f"This is reminder #{int(reminder_count)}. Decision: stop the tool call."
        )

    @staticmethod
    def _unavailable_label(*, tool_name: str, elapsed_seconds: float, reminder_count: int) -> str:
        return (
            f"The running tool `{tool_name}` has been active for {elapsed_seconds:.0f}s. "
            f"This is reminder #{int(reminder_count)}. Reminder decision unavailable, so the tool will keep running."
        )

    @staticmethod
    def _content_text(value: Any) -> str:
        if isinstance(value, str):
            return value
        if isinstance(value, list):
            parts: list[str] = []
            for item in value:
                if isinstance(item, str):
                    text = item.strip()
                    if text:
                        parts.append(text)
                    continue
                if isinstance(item, dict):
                    text = item.get("text", item.get("content", ""))
                    if isinstance(text, str) and text.strip():
                        parts.append(text.strip())
            return "\n".join(parts).strip()
        return str(value or "")

    def _resolve_ceo_model_refs(self) -> list[str]:
        if self._support is None:
            from g3ku.runtime.frontdoor._ceo_support import CeoFrontDoorSupport

            self._support = CeoFrontDoorSupport(loop=self._loop)
        return self._support._resolve_ceo_model_refs()

    def _resolve_chat_backend(self):
        if self._support is None:
            from g3ku.runtime.frontdoor._ceo_support import CeoFrontDoorSupport

            self._support = CeoFrontDoorSupport(loop=self._loop)
        return self._support._resolve_chat_backend()

    def _persist_frontdoor_internal_request_artifact(
        self,
        *,
        session_key: str,
        turn_id: str,
        request_messages: list[dict[str, Any]],
        tool_schemas: list[dict[str, Any]] | None,
        model_refs: list[str],
        parallel_tool_calls: bool | None,
        prompt_cache_key: str,
        prompt_cache_diagnostics: dict[str, Any],
        provider_request_meta: dict[str, Any] | None,
        provider_request_body: dict[str, Any] | None,
        usage: dict[str, int] | None,
        request_lane: str,
        parent_request_id: str = "",
    ) -> dict[str, Any]:
        normalized_session_id = str(session_key or "").strip()
        if not normalized_session_id:
            return {}
        return persist_frontdoor_actual_request(
            normalized_session_id,
            payload={
                "type": "frontdoor_internal_request",
                "request_kind": "frontdoor_internal_request",
                "request_lane": str(request_lane or "").strip() or "inline_tool_reminder",
                "session_key": normalized_session_id,
                "turn_id": str(turn_id or "").strip(),
                "parent_request_id": str(parent_request_id or "").strip(),
                "created_at": now_iso(),
                "provider_model": str((list(model_refs or []) or [""])[0] or "").strip(),
                "model_refs": [
                    str(item or "").strip()
                    for item in list(model_refs or [])
                    if str(item or "").strip()
                ],
                "parallel_tool_calls": parallel_tool_calls,
                "prompt_cache_key": str(prompt_cache_key or "").strip(),
                "prompt_cache_key_hash": str(prompt_cache_diagnostics.get("prompt_cache_key_hash") or "").strip(),
                "actual_request_hash": str(prompt_cache_diagnostics.get("actual_request_hash") or "").strip(),
                "actual_request_message_count": int(prompt_cache_diagnostics.get("actual_request_message_count") or 0),
                "actual_tool_schema_hash": str(prompt_cache_diagnostics.get("actual_tool_schema_hash") or "").strip(),
                "tool_signature_hash": str(prompt_cache_diagnostics.get("tool_signature_hash") or "").strip(),
                "stable_prefix_hash": str(prompt_cache_diagnostics.get("stable_prefix_hash") or "").strip(),
                "dynamic_appendix_hash": str(prompt_cache_diagnostics.get("dynamic_appendix_hash") or "").strip(),
                "messages": [dict(item) for item in list(request_messages or []) if isinstance(item, dict)],
                "request_messages": [dict(item) for item in list(request_messages or []) if isinstance(item, dict)],
                "tool_schemas": [dict(item) for item in list(tool_schemas or []) if isinstance(item, dict)],
                "provider_request_meta": (
                    dict(provider_request_meta or {})
                    if isinstance(provider_request_meta, dict)
                    else {}
                ),
                "provider_request_body": (
                    dict(provider_request_body or {})
                    if isinstance(provider_request_body, dict)
                    else {}
                ),
                "usage": normalize_usage_payload(usage),
            },
        )

    def _publish(self, *, session_id: str, event_type: str, data: dict[str, Any]) -> None:
        service = getattr(self._loop, "main_task_service", None)
        registry = getattr(service, "registry", None) if service is not None else None
        if registry is None or not hasattr(registry, "publish_ceo"):
            return
        normalized_session_id = str(session_id or "").strip()
        if not normalized_session_id:
            return
        envelope = build_envelope(
            channel="ceo",
            session_id=normalized_session_id,
            seq=registry.next_ceo_seq(normalized_session_id),
            type=event_type,
            data=dict(data or {}),
        )
        registry.publish_ceo(normalized_session_id, envelope)
