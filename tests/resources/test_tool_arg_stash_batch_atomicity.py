"""同批多个冲动调用：暂存必须全部存活（存储层原子合并的回归）。

事故形态（task:ca4661223a7a）：模型一条 assistant 消息里发了 3 个未水合的
`filesystem_write`。三条都走契约投递并暂存，但每条拿到的都是同一份批前快照，
各自把自己的条目并入快照后整份写回节点行——后写覆盖先写，最后只剩第 3 条。
下一跳 `reuse_args=true` 于是只执行了 1 份。

这里从两个入口钉住修复：
- 节点道用每调用一份浅拷贝复现 `_call_runtime_context` 的批内形状；
- 消费侧用"执行期间又暂存了一条"的 await 窗口，验证整份写回不会把它删掉。
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from g3ku.agent.tools.base import Tool
from g3ku.runtime import tool_arg_stash
from g3ku.runtime.frontdoor._ceo_runtime_ops import CeoFrontDoorRuntimeOps
from main.models import NodeRecord
from main.runtime.react_loop import ReActToolLoop
from main.service.runtime_service import MainRuntimeService

TOOL_ID = "filesystem_edit"
TASK_ID = "task-batch-stash"
NODE_ID = "node-batch-stash"
SCOPE = tool_arg_stash.node_scope_key(TASK_ID, NODE_ID, TOOL_ID)
FINGERPRINT = "tcf:" + "b" * 16

CONTRACT_PAYLOAD = {
    "tool_id": TOOL_ID,
    "content": "# filesystem_edit\n\nEdit a file in place.\n",
    "parameter_contract_markdown": "## Parameter Contract\n",
    "parameters_schema": {
        "type": "object",
        "properties": {"path": {"type": "string"}, "new_text": {"type": "string"}},
        "required": ["path", "new_text"],
    },
    "required_parameters": ["path", "new_text"],
    "example_arguments": {},
    "warnings": [],
    "errors": [],
    "callable": True,
    "available": True,
    "repair_required": False,
}


class _ExplodingTool(Tool):
    calls = 0

    @property
    def name(self) -> str:
        return TOOL_ID

    @property
    def description(self) -> str:
        return "edit"

    @property
    def parameters(self) -> dict:
        return dict(CONTRACT_PAYLOAD["parameters_schema"])

    async def execute(self, **kwargs):
        type(self).calls += 1
        raise AssertionError("impulsive calls must not execute the tool")


class _RecordingTool(_ExplodingTool):
    calls = 0
    seen: list[str] = []

    async def execute(self, **kwargs):
        type(self).calls += 1
        type(self).seen.append(str(kwargs.get("path") or ""))
        return "written"


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

    class _Instance:
        def coerce_arguments(self, arguments):
            return dict(arguments or {}), []

        def validate_params(self, params):
            _ = params
            return []

    instance = _Instance()
    service._resource_manager = SimpleNamespace(get_tool=lambda name: instance)
    service._visible_tool_map = lambda **kwargs: {TOOL_ID: instance}
    service.get_tool_toolskill = lambda tool_id: dict(CONTRACT_PAYLOAD)
    service.store.upsert_node(
        NodeRecord(
            node_id=NODE_ID,
            task_id=TASK_ID,
            root_node_id=NODE_ID,
            goal="g",
            prompt="p",
            created_at="2026-10-29T19:00:00+08:00",
            updated_at="2026-10-29T19:00:00+08:00",
        )
    )
    return service


def _loop(service: MainRuntimeService) -> ReActToolLoop:
    loop = ReActToolLoop(chat_backend=SimpleNamespace(), log_service=SimpleNamespace(), max_iterations=2)
    loop._tool_impulse_service = service
    return loop


def _context(*, presence: dict | None = None, carrier: dict | None = None, batch_total: int = 1) -> dict:
    return {
        'task_id': TASK_ID,
        'node_id': NODE_ID,
        'actor_role': 'execution',
        'session_key': 'web:shared',
        'tool_contract_presence': {} if presence is None else presence,
        'tool_arg_stash_carrier': carrier or {},
        'impulse_delivered_tools': {},
        'impulse_staged_call_ids': set(),
        'impulse_batch_totals': {TOOL_ID: batch_total},
        'tool_contract_deliveries': {},
        'current_tool_call_id': 'call-0',
        'stage_turn_granted': False,
    }


def _entry(arguments: dict, *, hop: int) -> dict:
    _container, entry, _count = tool_arg_stash.stage_args(
        {},
        scope_key=SCOPE,
        args=dict(arguments),
        fingerprint=FINGERPRINT,
        staged_hop=hop,
    )
    return dict(entry)


def _staged_paths(carrier: dict) -> list[str]:
    container = tool_arg_stash.carrier_container(carrier)
    return [str(dict(entry.get(tool_arg_stash.ARGS_FIELD) or {}).get('path') or '') for entry in container.get(SCOPE) or []]


def _entry_fingerprint(service: MainRuntimeService) -> str:
    carrier = service.node_stash_carrier(NODE_ID)
    container = tool_arg_stash.carrier_container(carrier)
    entries = list(container.get(SCOPE) or [])
    return str(entries[0].get(tool_arg_stash.FINGERPRINT_FIELD) or '') if entries else ''


# ---------------------------------------------------------------------------
# 节点道：同一批三条冲动调用
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_three_impulse_calls_in_one_batch_all_survive(tmp_path: Path) -> None:
    service = _service(tmp_path)
    loop = _loop(service)
    tool = _ExplodingTool()
    parent = _context(batch_total=3)
    contents: list[str] = []
    for index, path in enumerate(["a", "b", "c"]):
        # `_call_runtime_context` 的批内形状：顶层键一份浅拷贝，批前快照各自持有。
        call_context = {**parent, 'current_tool_call_id': f'call-{index}'}
        contents.append(
            await loop._execute_tool_raw(
                tools={TOOL_ID: tool},
                tool_name=TOOL_ID,
                arguments={"path": path, "new_text": "x"},
                runtime_context=call_context,
            )
        )

    assert _ExplodingTool.calls == 0
    # 末条回执必须报整批的真实份数，而不是本调用自己那一份（事故时这里是 1）。
    assert 'Staged sets for "filesystem_edit": 3.' in contents[-1]
    assert _staged_paths(service.node_stash_carrier(NODE_ID)) == ["a", "b", "c"]


@pytest.mark.asyncio
async def test_reuse_after_the_batch_executes_every_staged_set(tmp_path: Path) -> None:
    service = _service(tmp_path)
    loop = _loop(service)
    parent = _context(batch_total=3)
    for index, path in enumerate(["a", "b", "c"]):
        await loop._execute_tool_raw(
            tools={TOOL_ID: _ExplodingTool()},
            tool_name=TOOL_ID,
            arguments={"path": path, "new_text": "x"},
            runtime_context={**parent, 'current_tool_call_id': f'call-{index}'},
        )

    # 下一跳开始时跳序推进（`advance_node_stash_hop`），上一跳的条目才可消费。
    service.advance_node_stash_hop(NODE_ID)
    fingerprint = _entry_fingerprint(service)
    assert fingerprint
    _RecordingTool.seen = []
    content = await loop._execute_tool_raw(
        tools={TOOL_ID: _RecordingTool()},
        tool_name=TOOL_ID,
        arguments={"reuse_args": True},
        runtime_context=_context(
            presence={TOOL_ID: fingerprint},
            carrier=service.node_stash_carrier(NODE_ID),
        ),
    )
    payload = json.loads(content)
    assert payload["executed"] == 3
    assert payload["failed"] == 0
    assert _RecordingTool.seen == ["a", "b", "c"]


# ---------------------------------------------------------------------------
# 消费侧：执行期间新增的暂存不能被整份写回带走
# ---------------------------------------------------------------------------


def test_consume_only_removes_the_executed_digest(tmp_path: Path) -> None:
    service = _service(tmp_path)
    service.persist_node_stash_carrier(NODE_ID, tool_arg_stash.build_carrier({}, hop=3))
    entry_a = _entry({"path": "a"}, hop=3)
    entry_b = _entry({"path": "b"}, hop=3)
    service.stage_node_stash_entry(NODE_ID, scope_key=SCOPE, entry=entry_a, hop=3)
    carrier, count = service.stage_node_stash_entry(NODE_ID, scope_key=SCOPE, entry=entry_b, hop=3)
    assert count == 2

    carrier, removed = service.consume_node_stash_entries(
        NODE_ID,
        scope_key=SCOPE,
        digests=[str(entry_a[tool_arg_stash.DIGEST_FIELD])],
    )

    assert [entry[tool_arg_stash.DIGEST_FIELD] for entry in removed] == [entry_a[tool_arg_stash.DIGEST_FIELD]]
    assert _staged_paths(carrier) == ["b"]


def test_stage_is_an_atomic_merge_not_a_snapshot_write(tmp_path: Path) -> None:
    """同批两条调用各写各的：第二次落盘后第一条必须还在，且同签名不重复入栈。"""
    service = _service(tmp_path)
    service.persist_node_stash_carrier(NODE_ID, tool_arg_stash.build_carrier({}, hop=3))
    first, count = service.stage_node_stash_entry(
        NODE_ID,
        scope_key=SCOPE,
        entry=_entry({"path": "a"}, hop=3),
        hop=3,
    )
    assert count == 1
    second, count = service.stage_node_stash_entry(
        NODE_ID,
        scope_key=SCOPE,
        entry=_entry({"path": "b"}, hop=3),
        hop=3,
    )
    assert count == 2
    assert _staged_paths(second) == ["a", "b"]
    again, count = service.stage_node_stash_entry(
        NODE_ID,
        scope_key=SCOPE,
        entry=_entry({"path": "a"}, hop=3),
        hop=3,
    )
    assert count == 2
    assert _staged_paths(again) == ["a", "b"]
    assert _staged_paths(service.node_stash_carrier(NODE_ID)) == ["a", "b"]
    assert _staged_paths(first) == ["a"]  # 调用方手里那份读视图不被后续合并篡改


@pytest.mark.asyncio
async def test_node_reuse_keeps_entries_staged_during_execution(tmp_path: Path) -> None:
    service = _service(tmp_path)
    loop = _loop(service)
    service.persist_node_stash_carrier(NODE_ID, tool_arg_stash.build_carrier({}, hop=4))
    service.stage_node_stash_entry(
        NODE_ID,
        scope_key=SCOPE,
        entry=_entry({"path": "old"}, hop=4),
        hop=4,
    )
    # 库内 hop=4 的条目要在 hop=5 才可消费；把载体跳到 5，让 old 可消费。
    carrier = tool_arg_stash.build_carrier(
        tool_arg_stash.carrier_container(service.node_stash_carrier(NODE_ID)),
        hop=5,
    )
    service.persist_node_stash_carrier(NODE_ID, carrier)

    staged: list[str] = []

    async def _execute_with_sibling_staging(**kwargs):
        if not staged:
            # 模拟复用批 await 期间同批另一条冲动调用暂存了新入参。
            service.stage_node_stash_entry(
                NODE_ID,
                scope_key=SCOPE,
                entry=_entry({"path": "new"}, hop=5),
                hop=5,
            )
            staged.append("new")
        return "ok", "success"

    loop._execute_tool_with_status = _execute_with_sibling_staging

    content = await loop._execute_tool_raw(
        tools={TOOL_ID: _RecordingTool()},
        tool_name=TOOL_ID,
        arguments={"reuse_args": True},
        runtime_context=_context(
            presence={TOOL_ID: _entry_fingerprint(service)},
            carrier=carrier,
        ),
    )

    payload = json.loads(content)
    assert payload["executed"] == 1
    assert _staged_paths(service.node_stash_carrier(NODE_ID)) == ["new"]


# ---------------------------------------------------------------------------
# 前门道：同一类 await 窗口
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_frontdoor_reuse_keeps_entries_staged_during_execution() -> None:
    session_scope = tool_arg_stash.session_scope_key("web:shared", TOOL_ID)
    container, old_entry, _count = tool_arg_stash.stage_args(
        {},
        scope_key=session_scope,
        args={"path": "old"},
        fingerprint=FINGERPRINT,
        staged_hop=2,
    )
    carrier = tool_arg_stash.build_carrier(container, hop=3)
    session = SimpleNamespace(session_key="web:shared", _frontdoor_tool_arg_stash=carrier)
    runtime_context = {
        "session_key": "web:shared",
        "runtime_session": session,
        "tool_arg_stash_carrier": carrier,
        "tool_contract_presence": {TOOL_ID: FINGERPRINT},
    }

    async def _execute_one(arguments: dict) -> tuple[str, str]:
        value = str((arguments or {}).get("path"))
        if value == "old":
            fresh = tool_arg_stash.carrier_container(getattr(session, "_frontdoor_tool_arg_stash", {}) or {})
            updated, _entry, _staged = tool_arg_stash.stage_args(
                fresh,
                scope_key=session_scope,
                args={"path": "new"},
                fingerprint=FINGERPRINT,
                staged_hop=3,
            )
            session._frontdoor_tool_arg_stash = tool_arg_stash.build_carrier(updated, hop=3)
        return json.dumps({"ok": True}), "success"

    ops = CeoFrontDoorRuntimeOps.__new__(CeoFrontDoorRuntimeOps)
    content = await ops._frontdoor_reuse_args_execution(
        tool_name=TOOL_ID,
        arguments={"reuse_args": True},
        state={"messages": []},
        runtime_context=runtime_context,
        execute_one=_execute_one,
        call_id="call-fd-batch-1",
    )

    payload = json.loads(str(content or "{}"))
    assert payload["executed"] == 1
    assert payload["items"][0]["args_digest"] == old_entry[tool_arg_stash.DIGEST_FIELD]
    stored = tool_arg_stash.carrier_container(session._frontdoor_tool_arg_stash)
    assert [str(entry[tool_arg_stash.ARGS_FIELD]["path"]) for entry in stored.get(session_scope) or []] == ["new"]
