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


def _structured_fact(
    fact_id: str,
    *,
    category: str,
    scope: str,
    entity: str,
    attribute: str,
    value: Any,
    observed_at: str,
    time_semantics: str,
    source_excerpt: str = "source",
) -> dict[str, Any]:
    return {
        "fact_id": fact_id,
        "category": category,
        "scope": scope,
        "entity": entity,
        "attribute": attribute,
        "value": value,
        "observed_at": observed_at,
        "time_semantics": time_semantics,
        "source_excerpt": source_excerpt,
    }


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
            slot_id="slot-remove",
        )
        remaining = _stateful_fact(
            "state-keep",
            statement="State is active.",
            observed_at="2026-04-08T12:15:00+00:00",
            slot_id="slot-keep",
        )
        await manager.upsert_structured_memory_facts(
            session_key=SESSION_KEY,
            channel=CHANNEL,
            chat_id=CHAT_ID,
            facts=[removable, remaining],
        )
        assert {fact["fact_id"] for fact in await manager.list_active_structured_facts(
            session_key=SESSION_KEY,
            channel=CHANNEL,
            chat_id=CHAT_ID,
        )} == {removable["fact_id"], remaining["fact_id"]}
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
        assert {fact["fact_id"] for fact in active} == {remaining["fact_id"]}
    finally:
        manager.close()


@pytest.mark.asyncio
async def test_current_state_rendered_statement_includes_observed_at(tmp_path: Path) -> None:
    manager = MemoryManager(tmp_path, _memory_cfg())
    try:
        timestamp = "2026-04-08T12:20:00+00:00"
        statement = "State is warm."
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
        assert "State is warm." in rendered
        assert timestamp in rendered
    finally:
        manager.close()


@pytest.mark.asyncio
async def test_retrieve_context_bundle_hides_superseded_structured_fact(
    tmp_path: Path,
) -> None:
    manager = MemoryManager(tmp_path, _memory_cfg())
    try:
        first_fact = _structured_fact(
            "sales-v1",
            category="stateful_fact",
            scope="project",
            entity="amazon_store",
            attribute="sales",
            value="1100",
            observed_at="2026-04-08T11:00:00+08:00",
            time_semantics="current_state",
            source_excerpt="sales 1100",
        )
        second_fact = _structured_fact(
            "sales-v2",
            category="stateful_fact",
            scope="project",
            entity="amazon_store",
            attribute="sales",
            value="1300",
            observed_at="2026-04-09T11:00:00+08:00",
            time_semantics="current_state",
            source_excerpt="sales 1300",
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

        bundle = await manager.retrieve_context_bundle(
            query="sales",
            session_key=SESSION_KEY,
            channel=CHANNEL,
            chat_id=CHAT_ID,
            search_context_types=["memory"],
            allowed_context_types=["memory"],
        )

        records = list(bundle.records or [])
        assert len(records) == 1
        assert records[0]["record_id"] == "sales-v2"
        assert "1300" in str(records[0]["l1"] or "")
    finally:
        manager.close()


@pytest.mark.asyncio
async def test_durable_default_setting_replaces_old_value(tmp_path: Path) -> None:
    manager = MemoryManager(tmp_path, _memory_cfg())
    try:
        first = _structured_fact(
            "lang-v1",
            category="default_setting",
            scope="user",
            entity="response_style",
            attribute="language",
            value="zh-CN",
            observed_at="2026-04-08T11:00:00+08:00",
            time_semantics="durable_until_replaced",
            source_excerpt="use Chinese",
        )
        second = _structured_fact(
            "lang-v2",
            category="default_setting",
            scope="user",
            entity="response_style",
            attribute="language",
            value="en-US",
            observed_at="2026-04-09T11:00:00+08:00",
            time_semantics="durable_until_replaced",
            source_excerpt="use English",
        )
        await manager.upsert_structured_memory_facts(
            session_key=SESSION_KEY,
            channel=CHANNEL,
            chat_id=CHAT_ID,
            facts=[first],
        )
        await manager.upsert_structured_memory_facts(
            session_key=SESSION_KEY,
            channel=CHANNEL,
            chat_id=CHAT_ID,
            facts=[second],
        )
        active = await manager.list_active_structured_facts(
            session_key=SESSION_KEY,
            channel=CHANNEL,
            chat_id=CHAT_ID,
        )

        assert len(active) == 1
        assert active[0]["fact_id"] == "lang-v2"
        assert active[0]["value"] == "en-US"
    finally:
        manager.close()


@pytest.mark.asyncio
async def test_historical_facts_keep_multiple_observations(tmp_path: Path) -> None:
    manager = MemoryManager(tmp_path, _memory_cfg())
    try:
        first = _structured_fact(
            "hist-v1",
            category="historical_fact",
            scope="project",
            entity="amazon_store",
            attribute="sales",
            value="1100",
            observed_at="2026-04-08T11:00:00+08:00",
            time_semantics="historical_observation",
            source_excerpt="sales was 1100",
        )
        second = _structured_fact(
            "hist-v2",
            category="historical_fact",
            scope="project",
            entity="amazon_store",
            attribute="sales",
            value="1300",
            observed_at="2026-04-09T11:00:00+08:00",
            time_semantics="historical_observation",
            source_excerpt="sales was 1300",
        )
        await manager.upsert_structured_memory_facts(
            session_key=SESSION_KEY,
            channel=CHANNEL,
            chat_id=CHAT_ID,
            facts=[first],
        )
        await manager.upsert_structured_memory_facts(
            session_key=SESSION_KEY,
            channel=CHANNEL,
            chat_id=CHAT_ID,
            facts=[second],
        )
        active = await manager.list_active_structured_facts(
            session_key=SESSION_KEY,
            channel=CHANNEL,
            chat_id=CHAT_ID,
        )

        assert len(active) == 2
        assert {item["fact_id"] for item in active} == {"hist-v1", "hist-v2"}
    finally:
        manager.close()


@pytest.mark.asyncio
async def test_delete_structured_memory_reports_only_actual_deletions(tmp_path: Path) -> None:
    manager = MemoryManager(tmp_path, _memory_cfg())
    try:
        result = await manager.delete_structured_memory_facts(
            session_key=SESSION_KEY,
            channel=CHANNEL,
            chat_id=CHAT_ID,
            fact_ids=[],
            canonical_keys=["missing|canonical|key"],
        )

        assert result["deleted"] == 0
    finally:
        manager.close()
