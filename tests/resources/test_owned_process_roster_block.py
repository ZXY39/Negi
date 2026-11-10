"""头部「在跑的后台进程」清单：用途列、节点归属过滤、冻结与两装配点一致。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from g3ku.runtime import owned_processes
from g3ku.runtime.frontdoor import _ceo_create_agent_impl as create_agent_impl
from g3ku.runtime.frontdoor import _ceo_runtime_ops as runtime_ops_module
from g3ku.runtime.frontdoor import message_builder as message_builder_module
from g3ku.runtime.frontdoor.message_builder import CeoMessageBuilder
from g3ku.runtime.frontdoor.tool_contract import (
    OWNED_PROCESS_ROSTER_HEADING,
    OWNED_PROCESS_ROSTER_MAX_LINES,
    PINNED_CONTRACT_RENDERER_VERSION,
    bump_skill_roster_epoch,
    clear_pinned_contract_stores,
    frontdoor_owned_process_roster,
    frontdoor_pinned_contract_text,
    render_owned_process_roster,
    render_pinned_contract_text,
    skill_roster_epoch_for,
)
from main.runtime import react_loop as react_loop_module


@pytest.fixture(autouse=True)
def _isolate_pinned_contract_stores():
    clear_pinned_contract_stores()
    yield
    clear_pinned_contract_stores()


def _process(
    pid: int,
    purpose: str,
    *,
    node_id: str = "",
    started_at: str = "2026-11-10T19:22:59",
    cmd: str = "node pipeline\\render.mjs",
) -> dict[str, object]:
    return {
        "pid": pid,
        "purpose": purpose,
        "node_id": node_id,
        "started_at": started_at,
        "cmd": cmd,
        "log_path": rf"C:\data\owned-processes\abc\{pid}.log",
    }


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
        owned_processes_provider=provider,
    )


def test_version_bumped_for_process_roster() -> None:
    assert PINNED_CONTRACT_RENDERER_VERSION == 'pinned-contract-v6'


def test_render_roster_uses_purpose_and_falls_back_to_command() -> None:
    text = render_owned_process_roster(
        [
            _process(101, "渲染 ep03 成片并写回日志"),
            _process(102, ""),
        ]
    )
    lines = text.splitlines()
    assert lines[0] == OWNED_PROCESS_ROSTER_HEADING
    # 倒序：102 在上，101 在下；用途为空时才回退到命令。
    assert lines[1].startswith("- 102 | node pipeline")
    assert lines[2] == (
        "- 101 | 渲染 ep03 成片并写回日志 | 启动于 2026-11-10T19:22:59 | "
        r"日志 C:\data\owned-processes\abc\101.log"
    )
    assert "node pipeline" not in lines[2]


def test_render_roster_clips_purpose_and_escapes_pipe() -> None:
    long_purpose = "长期" * 60
    text = render_owned_process_roster(
        [
            _process(1, long_purpose),
            _process(2, "A | B"),
        ]
    )
    lines = text.splitlines()
    purpose = lines[2].split(" | ")[1]
    assert len(purpose) == 80
    assert purpose.endswith("…")
    assert "| A ｜ B |" in lines[1]


def test_render_roster_is_bounded_and_empty_is_absent() -> None:
    items = [
        _process(pid, f"进程 {pid}")
        for pid in range(1, OWNED_PROCESS_ROSTER_MAX_LINES + 3)
    ]
    lines = render_owned_process_roster(items).splitlines()
    assert len(lines) == 1 + OWNED_PROCESS_ROSTER_MAX_LINES + 1
    assert lines[-1] == "另有 2 条更早的未列出"
    assert render_owned_process_roster([]) == ""
    text = render_pinned_contract_text(
        skill_items=[],
        exec_runtime_policy=None,
        session_temp_dir="C:/t",
        owned_processes=[],
    )
    assert OWNED_PROCESS_ROSTER_HEADING not in text


def test_provider_filters_by_node_and_keeps_purpose(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, object]] = []

    def fake_list_owned(scope, *, prune=True, node_id=None, include_unattributed=False):
        calls.append(
            {
                "scope": scope,
                "node_id": node_id,
                "include_unattributed": include_unattributed,
            }
        )
        return [
            {
                "pid": 1,
                "label": "本节点服务",
                "cmd": "node server.mjs",
                "started_at": "2026-11-10T19:22:59",
                "log_path": "C:/log/1.log",
                "node_id": "node:a",
            },
            {
                "pid": 2,
                "label": "",
                "cmd": "node other.mjs",
                "started_at": "2026-11-10T19:23:00",
                "log_path": "C:/log/2.log",
                "node_id": "node:a",
            },
        ]

    monkeypatch.setattr(owned_processes, "list_owned", fake_list_owned)

    items = frontdoor_owned_process_roster(task_id="task:abc", node_id="node:a")

    assert calls == [
        {"scope": "task:task:abc", "node_id": "node:a", "include_unattributed": False}
    ]
    assert [item["pid"] for item in items] == [1, 2]
    assert items[0]["purpose"] == "本节点服务"
    assert items[1]["purpose"] == ""
    assert frontdoor_owned_process_roster() == []


def test_provider_degrades_to_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    def exploding_list_owned(*args, **kwargs):
        raise RuntimeError("registry unavailable")

    monkeypatch.setattr(owned_processes, "list_owned", exploding_list_owned)
    assert frontdoor_owned_process_roster(session_key="web:roster") == []


def test_roster_is_frozen_until_the_epoch_moves() -> None:
    items = [_process(1, "常驻服务 A")]
    provider_calls: list[int] = []

    def provider() -> list[dict[str, object]]:
        provider_calls.append(len(items))
        return list(items)

    first = _pinned(provider=provider)
    assert OWNED_PROCESS_ROSTER_HEADING in first
    assert "常驻服务 A" in first
    assert len(provider_calls) == 1

    items.append(_process(2, "常驻服务 B"))
    again = _pinned(provider=provider)
    assert again == first
    assert len(provider_calls) == 1

    bump_skill_roster_epoch(None, session_key="web:roster")
    items[:] = [_process(2, "常驻服务 B")]
    repinned = _pinned(provider=provider)
    assert repinned != first
    assert "常驻服务 B" in repinned
    assert "常驻服务 A" not in repinned
    assert len(provider_calls) == 2


def _stub_service(roster: list[dict[str, object]]) -> SimpleNamespace:
    return SimpleNamespace(
        long_term_task_roster=lambda session_id: [],
        _current_exec_runtime_policy_payload=lambda: None,
        list_contract_visible_skill_resources=lambda *, actor_role, session_id: [],
    )


def test_frontdoor_assembly_points_share_the_same_process_roster(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    items = [_process(7, "常驻服务 A")]
    for module in (message_builder_module, runtime_ops_module):
        monkeypatch.setattr(module, "frontdoor_owned_process_roster", lambda **kwargs: list(items))

    loop = SimpleNamespace(main_task_service=_stub_service([]))
    session = SimpleNamespace(state=SimpleNamespace(session_key="web:roster"))

    builder = CeoMessageBuilder(loop=loop, prompt_builder=SimpleNamespace())
    builder_text, _ = builder._frontdoor_pinned_contract(
        persisted_session=session,
        context_sources={"selected_skills": []},
    )

    clear_pinned_contract_stores()
    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(loop=loop)
    ops_text, _ = runner._frontdoor_pinned_contract(
        session_key="web:roster",
        skill_ids=[],
        session=session,
    )

    assert builder_text == ops_text
    assert OWNED_PROCESS_ROSTER_HEADING in builder_text
    assert "常驻服务 A" in builder_text


def test_node_head_passes_task_and_node_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, str]] = []

    def fake_provider(**kwargs):
        calls.append({key: str(value) for key, value in kwargs.items()})
        return [_process(9, "节点自己的服务", node_id="node:a")]

    monkeypatch.setattr(react_loop_module, "_frontdoor_owned_process_roster", fake_provider)
    loop = object.__new__(react_loop_module.ReActToolLoop)
    messages = [{"role": "system", "content": "base"}]

    out = loop._apply_node_skill_roster_head(
        messages,
        scope_key="node:task:abc:node:a",
        skill_items=[],
        task_id="task:abc",
        node_id="node:a",
    )

    assert calls == [{"task_id": "task:abc", "node_id": "node:a"}]
    assert OWNED_PROCESS_ROSTER_HEADING in out[0]["content"]
    assert "节点自己的服务" in out[0]["content"]
