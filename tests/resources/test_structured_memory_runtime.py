from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from g3ku.agent.rag_memory import MemoryManager
from g3ku.config.schema import MemoryToolsConfig

SESSION_KEY = "web:shared"
CHANNEL = "web"
CHAT_ID = "shared"


def _memory_cfg(**overrides: Any) -> MemoryToolsConfig:
    payload = MemoryToolsConfig().model_dump(mode="python")
    payload["guard"]["mode"] = "manual"
    payload["guard"]["auto_fact_confidence"] = 0.2
    payload.update(overrides)
    return MemoryToolsConfig.model_validate(payload)


def _stateful_fact(
    fact_id: str,
    *,
    slot_id: str = "current_state",
    statement: str,
    observed_at: str,
) -> dict[str, Any]:
    return {
        "fact_id": fact_id,
        "slot_id": slot_id,
        "stateful_fact": True,
        "rendered_statement": statement,
        "state": {"value": statement},
        "observed_at": observed_at,
    }


def _timestamp(offset_minutes: int = 0) -> str:
    return (datetime.now() - timedelta(minutes=offset_minutes)).isoformat()


@pytest.mark.asyncio
async def test_stateful_fact_replaces_current_state_slot(tmp_path: Path) -> None:
    manager = MemoryManager(tmp_path, _memory_cfg())
    try:
        first_fact = _stateful_fact(
            "state-current-v1",
            statement="State is idle.",
            observed_at="2026-04-08T12:00:00+00:00",
        )
        second_fact = _stateful_fact(
            "state-current-v2",
            statement="State is ready.",
            observed_at="2026-04-08T12:05:00+00:00",
        )
        await manager.upsert_structured_memory_facts(
            session_key=SESSION_KEY,
            channel=CHANNEL,
            chat_id=CHAT_ID,
            facts=[first_fact],
        )
        await manager.upsert_structured_memory_facts(
            session_key=SESSION_KEY,
            channel=CHANNEL,
            chat_id=CHAT_ID,
            facts=[second_fact],
        )
        active = await manager.list_active_structured_facts(
            session_key=SESSION_KEY,
            channel=CHANNEL,
            chat_id=CHAT_ID,
        )
        assert len(active) == 1
        assert active[0]["fact_id"] == second_fact["fact_id"]
        assert active[0]["slot_id"] == "current_state"
    finally:
        manager.close()


@pytest.mark.asyncio
async def test_duplicate_structured_write_is_noop(tmp_path: Path) -> None:
    manager = MemoryManager(tmp_path, _memory_cfg())
    try:
        fact = _stateful_fact(
            "state-dup",
            statement="State is syncing.",
            observed_at=_timestamp(),
        )
        await manager.upsert_structured_memory_facts(
            session_key=SESSION_KEY,
            channel=CHANNEL,
            chat_id=CHAT_ID,
            facts=[fact],
        )
        await manager.upsert_structured_memory_facts(
            session_key=SESSION_KEY,
            channel=CHANNEL,
            chat_id=CHAT_ID,
            facts=[dict(fact)],
        )
        active = await manager.list_active_structured_facts(
            session_key=SESSION_KEY,
            channel=CHANNEL,
            chat_id=CHAT_ID,
        )
        assert len(active) == 1
        assert active[0]["fact_id"] == fact["fact_id"]
        assert active[0]["observed_at"] == fact["observed_at"]
    finally:
        manager.close()


@pytest.mark.asyncio
async def test_delete_structured_fact_removes_only_target(tmp_path: Path) -> None:
    manager = MemoryManager(tmp_path, _memory_cfg())
    try:
        removable = _stateful_fact(
            "state-remove",
            statement="State is archived.",
            observed_at="2026-04-08T12:10:00+00:00",
        )
        remaining = _stateful_fact(
            "state-keep",
            statement="State is active.",
            observed_at="2026-04-08T12:15:00+00:00",
        )
        await manager.upsert_structured_memory_facts(
            session_key=SESSION_KEY,
            channel=CHANNEL,
            chat_id=CHAT_ID,
            facts=[removable, remaining],
        )
        await manager.delete_structured_memory_facts(
            session_key=SESSION_KEY,
            channel=CHANNEL,
            chat_id=CHAT_ID,
            fact_ids=[removable["fact_id"]],
        )
        active = await manager.list_active_structured_facts(
            session_key=SESSION_KEY,
            channel=CHANNEL,
            chat_id=CHAT_ID,
        )
        assert len(active) == 1
        assert active[0]["fact_id"] == remaining["fact_id"]
    finally:
        manager.close()


@pytest.mark.asyncio
async def test_current_state_rendered_statement_includes_observed_at(tmp_path: Path) -> None:
    manager = MemoryManager(tmp_path, _memory_cfg())
    try:
        timestamp = "2026-04-08T12:20:00+00:00"
        statement = f"State is warm. observed_at={timestamp}"
        fact = _stateful_fact(
            "state-rendered",
            statement=statement,
            observed_at=timestamp,
        )
        await manager.upsert_structured_memory_facts(
            session_key=SESSION_KEY,
            channel=CHANNEL,
            chat_id=CHAT_ID,
            facts=[fact],
        )
        active = await manager.list_active_structured_facts(
            session_key=SESSION_KEY,
            channel=CHANNEL,
            chat_id=CHAT_ID,
        )
        rendered = str(active[0].get("rendered_statement") or "")
        assert timestamp in rendered
    finally:
        manager.close()
