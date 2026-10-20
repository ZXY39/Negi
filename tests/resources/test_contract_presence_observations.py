"""Stage 8：观测与审计读数（D15）。

台账删掉之后排障只剩派生读数，所以这三档必须各自有钉住它的用例：

- `tool_contract_presence` 的三档：在场（带自己那份修订与本片暂存份数）、不在场、以及
  同一名字在视图里撞出两个修订（D10 吞块 / D2 换式的唯一可见形状）；
- `execution_path` 的四档调用级状态：normal / reused / corrected / illegal_reuse，
  由执行侧按 call id 交接，不让下游拿回执文本反推；
- `abandoned`：上一跳暂存过、这一跳没被调用 ⇒ 跳针推进时被清扫，清扫本身是静默的，
  名字必须回给读数。
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from g3ku.agent.tools.base import Tool
from g3ku.runtime import tool_arg_stash
from g3ku.runtime.tool_context_presence import (
    contract_presence_reading,
    loader_contract_revisions,
)
from g3ku.runtime.tool_impulse import (
    EXECUTION_PATH_CORRECTED,
    EXECUTION_PATH_ILLEGAL_REUSE,
    EXECUTION_PATH_REUSED,
)
from main.models import NodeRecord
from main.runtime.react_loop import ReActToolLoop
from main.service.runtime_service import MainRuntimeService

TOOL_ID = "filesystem_write"
TASK_ID = "task-obs"
NODE_ID = "node-obs"
SCOPE = tool_arg_stash.node_scope_key(TASK_ID, NODE_ID, TOOL_ID)
FINGERPRINT = "tcf:" + "c" * 16


class _EchoTool(Tool):
    def __init__(self) -> None:
        self.seen: list[str] = []

    @property
    def name(self) -> str:
        return TOOL_ID

    @property
    def description(self) -> str:
        return "write"

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
        }

    async def execute(self, value: str = "", **kwargs) -> str:
        _ = kwargs
        self.seen.append(str(value))
        return json.dumps({"ok": True, "value": value}, ensure_ascii=False)


def _service(tmp_path: Path) -> MainRuntimeService:
    service = MainRuntimeService(
        chat_backend=SimpleNamespace(),
        workspace_root=tmp_path,
        store_path=tmp_path / "runtime.sqlite3",
        files_base_dir=tmp_path / "tasks",
        artifact_dir=tmp_path / "artifacts",
        governance_store_path=tmp_path / "governance.sqlite3",
        execution_mode="web",
    )
    service.store.upsert_node(
        NodeRecord(
            node_id=NODE_ID,
            task_id=TASK_ID,
            root_node_id=NODE_ID,
            goal="g",
            prompt="p",
            created_at="2026-10-21T00:00:00+08:00",
            updated_at="2026-10-21T00:00:00+08:00",
        )
    )
    return service


def _loop(service: MainRuntimeService) -> ReActToolLoop:
    loop = ReActToolLoop(chat_backend=SimpleNamespace(), log_service=SimpleNamespace(), max_iterations=2)
    loop._tool_impulse_service = service
    return loop


def _block(tool_id: str, fingerprint: str) -> str:
    from g3ku.runtime.tool_contract_block import render_tool_contract_block

    return render_tool_contract_block(tool_id, fingerprint, f"# {tool_id}\n契约正文")


def _load_message(tool_id: str, fingerprint: str) -> dict:
    return {
        "role": "tool",
        "name": "load_tool_context",
        "tool_call_id": f"call-{tool_id}-{fingerprint[-4:]}",
        "content": json.dumps(
            {"ok": True, "tool_id": tool_id, "tool_context_fingerprint": fingerprint, "content": _block(tool_id, fingerprint)},
            ensure_ascii=False,
        ),
    }


def _carrier(*, hop: int, staged: list[dict]) -> dict:
    container: dict = {}
    for entry in staged:
        container, _item, _count = tool_arg_stash.stage_args(
            container,
            scope_key=SCOPE,
            args=dict(entry),
            fingerprint=FINGERPRINT,
            staged_hop=hop - 1,
        )
    return tool_arg_stash.build_carrier(container, hop=hop)


def _context(*, carrier: dict, call_id: str = "call-1") -> dict:
    return {
        "task_id": TASK_ID,
        "node_id": NODE_ID,
        "actor_role": "execution",
        "session_key": "web:shared",
        "tool_contract_presence": {TOOL_ID: FINGERPRINT},
        "tool_arg_stash_carrier": carrier,
        "tool_execution_paths": {},
        "impulse_delivered_tools": {},
        "impulse_staged_call_ids": set(),
        "impulse_batch_totals": {},
        "current_tool_call_id": call_id,
        "stage_turn_granted": True,
    }


# ---------------------------------------------------------------------------
# 在场读数
# ---------------------------------------------------------------------------


def test_presence_reading_classifies_present_and_absent() -> None:
    messages = [_load_message(TOOL_ID, FINGERPRINT)]
    revisions = loader_contract_revisions(messages)
    reading = contract_presence_reading(
        [TOOL_ID, "web_fetch"],
        revisions=revisions,
        staged_count_of=lambda name: 2 if name == TOOL_ID else 0,
    )

    assert reading["present"] == [{"tool": TOOL_ID, "fingerprint": FINGERPRINT, "staged_count": 2}]
    assert reading["absent"] == ["web_fetch"]
    assert reading["stale_rev"] == []


def test_presence_reading_flags_shadowed_revision() -> None:
    """同一名字在视图里留下两个修订 ⇒ `stale_rev`：这是 D2 换式 / D10 吞块唯一的可见形状。"""
    older = _load_message(TOOL_ID, "tcf:" + "a" * 16)
    newer = _load_message(TOOL_ID, FINGERPRINT)
    revisions = loader_contract_revisions([older, newer])

    reading = contract_presence_reading([TOOL_ID], revisions=revisions)

    assert revisions[TOOL_ID] == ["tcf:" + "a" * 16, FINGERPRINT]
    # 在场判据取最近一份，读数也照最近一份报
    assert reading["present"][0]["fingerprint"] == FINGERPRINT
    assert reading["stale_rev"] == [TOOL_ID]


def test_revision_scan_and_index_agree_on_last_block() -> None:
    messages = [_load_message(TOOL_ID, "tcf:" + "a" * 16), _load_message("exec", "tcf:" + "b" * 16)]
    revisions = loader_contract_revisions(messages)

    assert revisions == {TOOL_ID: ["tcf:" + "a" * 16], "exec": ["tcf:" + "b" * 16]}


# ---------------------------------------------------------------------------
# execution_path 四档
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execution_path_records_reused_batch(tmp_path: Path) -> None:
    service = _service(tmp_path)
    loop = _loop(service)
    tool = _EchoTool()
    runtime_context = _context(carrier=_carrier(hop=4, staged=[{"value": "a"}, {"value": "b"}]))

    await loop._execute_tool_raw(
        tools={TOOL_ID: tool},
        tool_name=TOOL_ID,
        arguments={"reuse_args": True},
        runtime_context=runtime_context,
    )

    assert runtime_context["tool_execution_paths"] == {"call-1": EXECUTION_PATH_REUSED}


@pytest.mark.asyncio
async def test_execution_path_records_illegal_reuse(tmp_path: Path) -> None:
    service = _service(tmp_path)
    loop = _loop(service)
    tool = _EchoTool()
    runtime_context = _context(carrier=tool_arg_stash.build_carrier({}, hop=9))

    await loop._execute_tool_raw(
        tools={TOOL_ID: tool},
        tool_name=TOOL_ID,
        arguments={"reuse_args": True},
        runtime_context=runtime_context,
    )

    assert tool.seen == []
    assert runtime_context["tool_execution_paths"] == {"call-1": EXECUTION_PATH_ILLEGAL_REUSE}


@pytest.mark.asyncio
async def test_execution_path_records_correction_after_staging(tmp_path: Path) -> None:
    """上一跳刚暂存过、这一跳带新参数直接调 ⇒ `corrected`（模型绕开了批量通道）。"""
    service = _service(tmp_path)
    loop = _loop(service)
    tool = _EchoTool()
    runtime_context = _context(carrier=_carrier(hop=4, staged=[{"value": "staged"}]))

    await loop._execute_tool_raw(
        tools={TOOL_ID: tool},
        tool_name=TOOL_ID,
        arguments={"value": "fresh"},
        runtime_context=runtime_context,
    )

    assert tool.seen == ["fresh"]
    assert runtime_context["tool_execution_paths"] == {"call-1": EXECUTION_PATH_CORRECTED}


@pytest.mark.asyncio
async def test_plain_call_without_staging_is_not_classified(tmp_path: Path) -> None:
    """没有暂存过的普通调用不写表——审计行按缺省读成 normal，不造第二份"正常"记录。"""
    service = _service(tmp_path)
    loop = _loop(service)
    tool = _EchoTool()
    runtime_context = _context(carrier=tool_arg_stash.build_carrier({}, hop=4))

    await loop._execute_tool_raw(
        tools={TOOL_ID: tool},
        tool_name=TOOL_ID,
        arguments={"value": "plain"},
        runtime_context=runtime_context,
    )

    assert tool.seen == ["plain"]
    assert runtime_context["tool_execution_paths"] == {}


# ---------------------------------------------------------------------------
# abandoned：清扫把"暂存过却没再调用"变成可数读数
# ---------------------------------------------------------------------------


def test_hop_advance_sweeps_expired_stash_and_reports_it(tmp_path: Path) -> None:
    service = _service(tmp_path)
    carrier = service.persist_node_stash_carrier(NODE_ID, _carrier(hop=4, staged=[{"value": "a"}, {"value": "b"}]))
    assert tool_arg_stash.carrier_container(carrier)[SCOPE]

    # hop 4 → 5：条目记的是 hop 3，严格一跳判它已过期（模型这一跳没来消费）。
    next_carrier, swept = service.advance_node_stash_hop(NODE_ID)

    assert tool_arg_stash.read_hop(next_carrier) == 5
    assert not tool_arg_stash.carrier_container(next_carrier).get(SCOPE)
    assert swept == [TOOL_ID]


def test_hop_advance_keeps_fresh_entries_without_reporting_sweep(tmp_path: Path) -> None:
    service = _service(tmp_path)
    # 本轮落的条目按**推进后的**跳序记（时钟先推、条目后落），所以推一格它正是"上一跳"。
    container, _item, _count = tool_arg_stash.stage_args(
        {},
        scope_key=SCOPE,
        args={"value": "a"},
        fingerprint=FINGERPRINT,
        staged_hop=4,
    )
    service.persist_node_stash_carrier(NODE_ID, tool_arg_stash.build_carrier(container, hop=4))

    next_carrier, swept = service.advance_node_stash_hop(NODE_ID)

    assert tool_arg_stash.read_hop(next_carrier) == 5
    assert len(tool_arg_stash.carrier_container(next_carrier).get(SCOPE) or []) == 1
    assert swept == []

    # 再推一格就没有"上一跳"可言了：这批被清扫，并把名字交给 `abandoned` 读数。
    second_carrier, second_swept = service.advance_node_stash_hop(NODE_ID)

    assert tool_arg_stash.read_hop(second_carrier) == 6
    assert not tool_arg_stash.carrier_container(second_carrier).get(SCOPE)
    assert second_swept == [TOOL_ID]


def test_frontdoor_hop_advance_sweeps_and_stashes_the_reading(tmp_path: Path) -> None:
    from g3ku.runtime.frontdoor._ceo_runtime_ops import CeoFrontDoorRuntimeOps

    session = SimpleNamespace(_frontdoor_tool_arg_stash=_frontdoor_carrier(hop=4))
    ops = CeoFrontDoorRuntimeOps.__new__(CeoFrontDoorRuntimeOps)

    next_carrier, swept = ops._advance_frontdoor_stash_hop(session)

    assert tool_arg_stash.read_hop(next_carrier) == 5
    assert swept == [TOOL_ID]
    assert getattr(session, "_frontdoor_stash_swept") == [TOOL_ID]


def _frontdoor_carrier(*, hop: int) -> dict:
    container, _item, _count = tool_arg_stash.stage_args(
        {},
        scope_key=tool_arg_stash.session_scope_key("web:shared", TOOL_ID),
        args={"value": "a"},
        fingerprint=FINGERPRINT,
        staged_hop=hop - 1,
    )
    return tool_arg_stash.build_carrier(container, hop=hop)
