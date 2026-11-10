"""头部「在挂的长期任务」清单：取数口径、渲染形状、冻结与两装配点一致。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from g3ku.runtime.frontdoor import _ceo_create_agent_impl as create_agent_impl
from g3ku.runtime.frontdoor.message_builder import CeoMessageBuilder
from g3ku.runtime.frontdoor.tool_contract import (
    LONG_TERM_TASK_ROSTER_HEADING,
    LONG_TERM_TASK_ROSTER_MAX_LINES,
    build_frontdoor_tool_contract,
    bump_skill_roster_epoch,
    clear_pinned_contract_stores,
    frontdoor_long_term_task_roster,
    frontdoor_pinned_contract_text,
    render_long_term_task_roster,
    render_pinned_contract_text,
    skill_roster_epoch_for,
)
from main.service.runtime_service import MainRuntimeService


@pytest.fixture(autouse=True)
def _isolate_pinned_contract_stores():
    clear_pinned_contract_stores()
    yield
    clear_pinned_contract_stores()


def _task(task_id: str, title: str, *, paused: bool = False) -> dict[str, object]:
    return {"task_id": task_id, "title": title, "paused": paused}


def _pinned(*, session=None, session_key="web:roster", epoch=None, provider=None) -> str:
    if epoch is None:
        epoch = skill_roster_epoch_for(session, session_key=session_key)
    return frontdoor_pinned_contract_text(
        session,
        skill_items=[],
        exec_runtime_policy=None,
        session_temp_dir="C:/temp/ceo",
        skill_roster_epoch=epoch,
        session_key=session_key,
        sticker_items=[],
        long_term_tasks_provider=provider,
    )


def test_render_roster_uses_title_not_core_requirement() -> None:
    text = render_long_term_task_roster(
        [
            {
                "task_id": "task:aaa",
                "title": "接手 ep01 及之后全部集数，负责脚本、配音与成片",
                "core_requirement": "本轮完成 ep01 v3 round 4 返修",
                "goal": "本轮返修",
            },
            {"task_id": "task:bbb", "title": "收集表情包", "paused": True},
        ]
    )
    lines = text.splitlines()
    assert lines[0] == LONG_TERM_TASK_ROSTER_HEADING
    assert lines[1] == "- task:aaa | 接手 ep01 及之后全部集数，负责脚本、配音与成片 | 在挂"
    assert lines[2] == "- task:bbb | 收集表情包 | 暂停"
    assert "本轮" not in text


def test_render_roster_clips_title_and_keeps_columns() -> None:
    long_title = "长期" * 40
    text = render_long_term_task_roster(
        [{"task_id": "task:long", "title": long_title}, {"task_id": "task:pipe", "title": "A | B"}]
    )
    long_line, pipe_line = text.splitlines()[1:3]
    goal = long_line.split(" | ")[1]
    assert len(goal) == 60
    assert goal.endswith("…")
    assert pipe_line == "- task:pipe | A ｜ B | 在挂"


def test_render_roster_is_bounded_and_empty_roster_is_absent() -> None:
    items = [
        {"task_id": f"task:{index:02d}", "title": f"长期任务 {index}"}
        for index in range(LONG_TERM_TASK_ROSTER_MAX_LINES + 2)
    ]
    lines = render_long_term_task_roster(items).splitlines()
    assert len(lines) == 1 + LONG_TERM_TASK_ROSTER_MAX_LINES + 1
    assert lines[-1] == "另有 2 条，用 task_list(long_term=true) 查看"
    assert render_long_term_task_roster([]) == ""
    text = render_pinned_contract_text(
        skill_items=[],
        exec_runtime_policy=None,
        session_temp_dir="C:/t",
        long_term_tasks=[],
    )
    assert LONG_TERM_TASK_ROSTER_HEADING not in text


def test_roster_is_frozen_until_the_epoch_moves() -> None:
    tasks = [_task("task:aaa", "常驻产线 A")]
    provider_calls: list[int] = []

    def provider() -> list[dict[str, object]]:
        provider_calls.append(len(tasks))
        return list(tasks)

    first = _pinned(provider=provider)
    assert LONG_TERM_TASK_ROSTER_HEADING in first
    assert "task:aaa" in first
    assert len(provider_calls) == 1

    # 无压缩的新任务不进头部，也不触发取数：头部逐字节冻结。
    tasks.append(_task("task:bbb", "常驻产线 B"))
    again = _pinned(provider=provider)
    assert again == first
    assert len(provider_calls) == 1

    # 压缩边界（epoch 跳变）重印：用当刻清单，旧任务消失、新任务出现。
    bump_skill_roster_epoch(None, session_key="web:roster")
    tasks[:] = [_task("task:bbb", "常驻产线 B")]
    repinned = _pinned(provider=provider)
    assert repinned != first
    assert "task:bbb" in repinned
    assert "task:aaa" not in repinned
    assert len(provider_calls) == 2


def test_fetch_helper_degrades_to_empty() -> None:
    class _ExplodingService:
        def long_term_task_roster(self, session_id: str):
            raise RuntimeError("store unavailable")

    assert frontdoor_long_term_task_roster(
        SimpleNamespace(main_task_service=_ExplodingService()), session_key="web:roster"
    ) == []
    assert frontdoor_long_term_task_roster(SimpleNamespace(), session_key="web:roster") == []
    assert frontdoor_long_term_task_roster(SimpleNamespace(), session_key="") == []


class _DummyChatBackend:
    async def chat(self, **kwargs):
        raise AssertionError(f"chat backend must not run in these tests: {kwargs!r}")


async def _noop_enqueue_task(_task_id: str) -> None:
    return None


def _service(tmp_path: Path) -> MainRuntimeService:
    service = MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        workspace_root=tmp_path,
        store_path=tmp_path / "runtime.sqlite3",
        files_base_dir=tmp_path / "tasks",
        artifact_dir=tmp_path / "artifacts",
        governance_store_path=tmp_path / "governance.sqlite3",
        execution_mode="embedded",
    )
    service.global_scheduler.enqueue_task = _noop_enqueue_task
    return service


@pytest.mark.asyncio
async def test_long_term_task_roster_reads_active_continuous_of_this_session(tmp_path: Path) -> None:
    service = _service(tmp_path)
    active = await service.create_task(
        "长期产线 A",
        session_id="web:roster",
        continuous=True,
        title="接手 ep01 及之后全部集数",
        metadata={"core_requirement": "本轮完成 ep01 v3 round 4 返修"},
    )
    paused = await service.create_task("长期产线 B", session_id="web:roster", continuous=True)
    finished = await service.create_task("长期产线 C", session_id="web:roster", continuous=True)
    one_shot = await service.create_task("一次性任务", session_id="web:roster")
    other_session = await service.create_task(
        "别的会话的长期任务", session_id="web:other", continuous=True
    )
    service.store.upsert_task(paused.model_copy(update={"is_paused": True}))
    service.store.upsert_task(finished.model_copy(update={"status": "success"}))

    items = service.long_term_task_roster("web:roster")
    by_id = {str(item["task_id"]): item for item in items}

    # success 不列、一次性不列、别的会话不列；暂停的留下但状态分档。
    assert set(by_id) == {active.task_id, paused.task_id}
    assert by_id[active.task_id]["title"] == "接手 ep01 及之后全部集数"
    assert by_id[active.task_id]["paused"] is False
    assert by_id[paused.task_id]["paused"] is True
    assert one_shot.task_id not in by_id
    assert other_session.task_id not in by_id
    # 清单里没有任何 core_requirement 的影子。
    assert all("本轮" not in str(item.get("title") or "") for item in items)
    assert service.long_term_task_roster("") == []


def _stub_service(roster: list[dict[str, object]]) -> SimpleNamespace:
    return SimpleNamespace(
        long_term_task_roster=lambda session_id: list(roster),
        _current_exec_runtime_policy_payload=lambda: None,
        list_contract_visible_skill_resources=lambda *, actor_role, session_id: [],
    )


def test_frontdoor_assembly_points_share_the_same_pinned_text() -> None:
    roster = [
        {"task_id": "task:aaa", "title": "常驻产线 A", "paused": False},
        {"task_id": "task:bbb", "title": "常驻产线 B", "paused": True},
    ]
    loop = SimpleNamespace(main_task_service=_stub_service(roster))
    session = SimpleNamespace(state=SimpleNamespace(session_key="web:roster"))

    builder = CeoMessageBuilder(loop=loop, prompt_builder=SimpleNamespace())
    builder_text, _builder_skill_ids = builder._frontdoor_pinned_contract(
        persisted_session=session,
        context_sources={"selected_skills": []},
    )

    clear_pinned_contract_stores()
    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(loop=loop)
    ops_text, _ops_skill_ids = runner._frontdoor_pinned_contract(
        session_key="web:roster",
        skill_ids=[],
        session=session,
    )

    assert builder_text == ops_text
    assert LONG_TERM_TASK_ROSTER_HEADING in builder_text
    assert "- task:aaa | 常驻产线 A | 在挂" in builder_text
    assert "- task:bbb | 常驻产线 B | 暂停" in builder_text

    payloads = [
        build_frontdoor_tool_contract(
            callable_tool_names=[],
            candidate_tool_names=[],
            pinned_contract_text=text,
        ).to_message_payload()
        for text in (builder_text, ops_text)
    ]
    assert {payload["pinned_contract_text"] for payload in payloads} == {builder_text}
