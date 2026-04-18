from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from g3ku.runtime.frontdoor.inline_tool_reminder import (
    CeoToolReminderService,
    InlineToolExecutionRecord,
    InlineToolExecutionRegistry,
)


class _CapturingChatBackend:
    def __init__(self, *, content: str = "STOP") -> None:
        self.content = content
        self.calls: list[dict[str, object]] = []

    async def chat(self, **kwargs):
        self.calls.append(dict(kwargs))
        return SimpleNamespace(content=self.content, tool_calls=[])


@pytest.mark.asyncio
async def test_reminder_sidecar_reuses_latest_actual_request_scaffold_for_cache_prefix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request_messages = [
        {"role": "system", "content": "stable system"},
        {"role": "user", "content": "Please finish the task."},
        {"role": "assistant", "content": "Working on it."},
    ]
    tool_schemas = [
        {
            "type": "function",
            "name": "exec",
            "description": "Run a command",
            "parameters": {"type": "object", "properties": {}},
        },
        {
            "type": "function",
            "name": "submit_next_stage",
            "description": "Advance the current stage",
            "parameters": {"type": "object", "properties": {}},
        },
    ]
    request_path = tmp_path / "frontdoor-request.json"
    request_path.write_text(
        json.dumps(
            {
                "request_messages": request_messages,
                "tool_schemas": tool_schemas,
                "prompt_cache_key": "cache-key-main-turn",
                "parallel_tool_calls": True,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    registry = InlineToolExecutionRegistry()
    loop = SimpleNamespace(
        sessions=None,
        main_task_service=None,
        inline_tool_execution_registry=registry,
    )
    service = CeoToolReminderService(loop=loop, registry=registry)
    chat_backend = _CapturingChatBackend(content="STOP")
    monkeypatch.setattr(service, "_resolve_chat_backend", lambda: chat_backend)
    monkeypatch.setattr(service, "_resolve_ceo_model_refs", lambda: ["openai_codex:gpt-test"])

    async def _unexpected_build_for_ceo(**kwargs):
        _ = kwargs
        raise AssertionError("reminder sidecar should reuse the persisted actual request scaffold")

    monkeypatch.setattr(service._builder, "build_for_ceo", _unexpected_build_for_ceo)

    runtime_session = SimpleNamespace(
        reminder_context_snapshot=lambda: {
            "session_key": "web:test",
            "turn_id": "turn-main-1",
            "source": "user",
            "status": "running",
            "user_message": {"content": "Please finish the task."},
            "assistant_text": "Working on it.",
            "visible_canonical_context": {},
            "frontdoor_canonical_context": {},
            "compression": {},
            "semantic_context_state": {},
            "hydrated_tool_names": [],
            "frontdoor_selection_debug": {},
            "frontdoor_actual_request_path": str(request_path),
        }
    )
    task = asyncio.create_task(asyncio.sleep(0))
    try:
        record = InlineToolExecutionRecord(
            execution_id="inline-tool-exec:1",
            session_key="web:test",
            turn_id="turn-main-1",
            tool_name="exec",
            tool_call_id="call-exec-1",
            task=task,
            snapshot_supplier=None,
            cancel_token=None,
            started_at=time.monotonic() - 35.0,
            runtime_session=runtime_session,
            reminder_count=1,
        )

        decision = await service._decide(record=record)
    finally:
        await task

    assert decision.decision == "stop"
    assert len(chat_backend.calls) == 1
    call = chat_backend.calls[0]
    assert call["messages"][: len(request_messages)] == request_messages
    assert call["tools"] == tool_schemas
    assert call["parallel_tool_calls"] is True
    assert call["prompt_cache_key"] == "cache-key-main-turn"
    appended_messages = call["messages"][len(request_messages) :]
    assert appended_messages
    assert appended_messages[-1]["role"] == "user"
