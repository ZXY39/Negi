from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from g3ku.agent.tools.base import Tool
from g3ku.runtime.frontdoor._ceo_create_agent_impl import CreateAgentCeoFrontDoorRunner
from g3ku.runtime.frontdoor.canonical_context import (
    _completed_stage_overlap_signature,
    _dedupe_canonical_stages,
)
from g3ku.runtime.frontdoor.state_models import initial_persistent_state
from g3ku.runtime.stage_prompt_compaction import (
    completed_stage_blocks,
    retained_completed_stage_ids,
)
from main.runtime.stage_budget import SILENT_TOOL_NAME, STAGE_TOOL_NAME
from main.service.runtime_service import MainRuntimeService


def test_initial_persistent_state_tracks_frontdoor_stage_state() -> None:
    state = initial_persistent_state(user_input={"content": "hello", "metadata": {}})

    assert state["route_kind"] == "direct_reply"
    assert state["frontdoor_stage_state"] == {
        "active_stage_id": "",
        "transition_required": False,
        "stages": [],
    }


def test_initial_persistent_state_tracks_compression_state() -> None:
    state = initial_persistent_state(user_input={"content": "hello", "metadata": {}})

    assert state["compression_state"] == {
        "status": "",
        "text": "",
        "source": "",
        "needs_recheck": False,
    }


class _RecordingTool(Tool):
    def __init__(self, sink: list[str]) -> None:
        self._sink = sink

    @property
    def name(self) -> str:
        return "record_tool"

    @property
    def description(self) -> str:
        return "record a value"

    @property
    def parameters(self) -> dict[str, object]:
        return {
            "type": "object",
            "properties": {
                "value": {"type": "string"},
            },
            "required": ["value"],
        }

    async def execute(self, value: str, **kwargs) -> str:
        _ = kwargs
        self._sink.append(str(value))
        return json.dumps({"ok": True, "value": str(value)}, ensure_ascii=False)


def _active_frontdoor_stage_state(*, budget: int, used: int = 0, transition_required: bool = False) -> dict[str, object]:
    return {
        "active_stage_id": "stage-1",
        "transition_required": bool(transition_required),
        "stages": [
            {
                "stage_id": "stage-1",
                "stage_index": 1,
                "stage_goal": "Inspect the current request",
                "tool_round_budget": int(budget),
                "tool_rounds_used": int(used),
                "status": "active",
                "mode": "自主执行",
                "completed_stage_summary": "",
                "key_refs": [],
                "rounds": [],
            }
        ],
    }


def _completed_frontdoor_stage(index: int) -> dict[str, object]:
    return {
        "stage_id": f"frontdoor-stage-{index}",
        "stage_index": index,
        "stage_goal": f"Stage {index}",
        "tool_round_budget": 6,
        "tool_rounds_used": 1,
        "status": "completed",
        "mode": "鑷富鎵ц",
        "stage_kind": "normal",
        "system_generated": False,
        "completed_stage_summary": f"finished stage {index}",
        "key_refs": [{"ref": f"artifact:artifact:stage-{index}", "note": f"note {index}"}],
        "rounds": [
            {
                "round_id": f"frontdoor-stage-{index}:round-1",
                "round_index": 1,
                "created_at": f"2026-04-08T10:{index:02d}:00",
                "tool_names": ["record_tool"],
                "tool_call_ids": [f"call-tool-{index}"],
                "budget_counted": True,
            }
        ],
        "created_at": f"2026-04-08T09:{index:02d}:00",
        "finished_at": f"2026-04-08T10:{index:02d}:30",
    }


def _active_progress_stage(index: int) -> dict[str, object]:
    return {
        "stage_id": f"frontdoor-stage-{index}",
        "stage_index": index,
        "stage_goal": f"Stage {index}",
        "tool_round_budget": 6,
        "tool_rounds_used": 1,
        "status": "active",
        "mode": "鑷富鎵ц",
        "stage_kind": "normal",
        "system_generated": False,
        "completed_stage_summary": "",
        "key_refs": [],
        "rounds": [
            {
                "round_id": f"frontdoor-stage-{index}:round-1",
                "round_index": 1,
                "created_at": f"2026-04-08T11:{index:02d}:00",
                "tool_names": ["record_tool"],
                "tool_call_ids": [f"call-tool-{index}"],
                "budget_counted": True,
            }
        ],
        "created_at": f"2026-04-08T11:{index:02d}:00",
        "finished_at": "",
    }


def _tool_call_payload(*, call_id: str, tool_name: str, arguments: dict[str, object]) -> dict[str, object]:
    return {
        "id": call_id,
        "name": tool_name,
        "arguments": dict(arguments),
    }


def _assistant_tool_call_record(*, call_id: str, tool_name: str, arguments: dict[str, object]) -> dict[str, object]:
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": call_id,
                "type": "function",
                "function": {
                    "name": tool_name,
                    "arguments": json.dumps(arguments, ensure_ascii=False),
                },
            }
        ],
    }


def _tool_message(*, call_id: str, tool_name: str, result_text: str, status: str = "success") -> dict[str, object]:
    return {
        "role": "tool",
        "tool_call_id": call_id,
        "name": tool_name,
        "content": result_text,
        "status": status,
    }


class _DummyChatBackend:
    async def chat(self, **kwargs):
        raise AssertionError(f"chat backend should not be used in this test: {kwargs!r}")


def _frontdoor_schema_names(schemas: list[dict[str, object]]) -> set[str]:
    return {str((item.get("function") or {}).get("name") or "") for item in list(schemas or [])}


def _tool_row(update: dict[str, object], call_id: str) -> dict[str, object]:
    for item in list(update.get("messages") or []):
        if isinstance(item, dict) and str(item.get("tool_call_id") or "") == call_id:
            return item
    return {}


async def _execute_one_frontdoor_tool(
    runner,
    *,
    state: dict[str, object],
    tool_name: str,
    arguments: dict[str, object],
    call_id: str,
):
    """Run one tool through the production execute_tools node and return its result row."""
    payload = _tool_call_payload(call_id=call_id, tool_name=tool_name, arguments=arguments)
    update = await runner._graph_execute_tools(
        {**state, "tool_call_payloads": [payload]},
        runtime=SimpleNamespace(context=SimpleNamespace()),
    )
    return update, _tool_row(update, call_id)


def _six_tuple_executor():
    executed: list[str] = []

    async def _record(arguments: dict[str, object]) -> None:
        executed.append(str(arguments.get("value") or ""))

    async def _execute_tool_call(*, tool, tool_name, arguments, runtime_context, on_progress, **kwargs):
        _ = tool_name, runtime_context, on_progress, kwargs
        raw = await tool.execute(**arguments)
        result_text = raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False)
        return raw, result_text, "success", "2026-04-08T10:00:00", "2026-04-08T10:00:01", 1.0

    return executed, _execute_tool_call



@pytest.mark.asyncio
async def test_frontdoor_stage_tool_is_visible_and_stage_creation_persists_in_state(monkeypatch) -> None:
    runner = CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace())
    executed, _execute_tool_call = _six_tuple_executor()

    async def _noop_progress(*args, **kwargs) -> None:
        _ = args, kwargs

    monkeypatch.setattr(
        runner,
        "_registered_tools",
        lambda tool_names: {"record_tool": _RecordingTool(executed)} if "record_tool" in list(tool_names or []) else {},
    )
    monkeypatch.setattr(runner, "_build_tool_runtime_context", lambda **kwargs: {"on_progress": _noop_progress})
    monkeypatch.setattr(runner, "_execute_tool_call_with_raw_result", _execute_tool_call)

    base_state = initial_persistent_state(user_input={"content": "hello", "metadata": {}})
    stage_arguments = {"stage_goal": "Inspect the current request", "tool_round_budget": 12}
    tools = runner._frontdoor_tool_schemas_for_state(
        state={**base_state, "tool_names": ["record_tool"]},
        runtime=SimpleNamespace(context=SimpleNamespace()),
    )

    # silent 是常驻内置控制工具，执行侧工具对象字典无条件注入（见 79b0f53a），
    # 所以它出现在每一份精确集合断言里，不是这一轮多放出来的可调用工具。
    assert _frontdoor_schema_names(tools) == {STAGE_TOOL_NAME, SILENT_TOOL_NAME, "record_tool"}

    _update, stage_row = await _execute_one_frontdoor_tool(
        runner,
        state={**base_state, "tool_names": ["record_tool"]},
        tool_name=STAGE_TOOL_NAME,
        arguments=stage_arguments,
        call_id="call-stage-1",
    )
    stage_result_text = str(stage_row.get("content") or "")
    stage_payload = json.loads(stage_result_text)

    result = await runner._postprocess_completed_tool_cycle(
        state={
            **base_state,
            "tool_names": ["record_tool"],
            "tool_call_payloads": [
                _tool_call_payload(
                    call_id="call-stage-1",
                    tool_name=STAGE_TOOL_NAME,
                    arguments=stage_arguments,
                )
            ],
            "messages": [
                {"role": "user", "content": "hello"},
                _assistant_tool_call_record(
                    call_id="call-stage-1",
                    tool_name=STAGE_TOOL_NAME,
                    arguments=stage_arguments,
                ),
                _tool_message(
                    call_id="call-stage-1",
                    tool_name=STAGE_TOOL_NAME,
                    result_text=stage_result_text,
                ),
            ],
        }
    )

    assert stage_payload["stage_goal"] == "Inspect the current request"
    assert stage_payload["tool_round_budget"] == 12
    assert result is not None
    assert result["frontdoor_stage_state"] == {
        "active_stage_id": stage_payload["stage_id"],
        "transition_required": False,
        "stages": [
            {
                **stage_payload,
                "archive_ref": "",
                "archive_stage_index_start": 0,
                "archive_stage_index_end": 0,
            }
        ],
        "pending_orphan_rounds": [],
    }
    assert executed == []

@pytest.mark.asyncio
async def test_frontdoor_stage_gate_graces_first_stageless_tool_then_blocks_the_next(monkeypatch) -> None:
    runner = CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace())
    executed, _execute_tool_call = _six_tuple_executor()

    async def _noop_progress(*args, **kwargs) -> None:
        _ = args, kwargs

    monkeypatch.setattr(
        runner,
        "_registered_tools",
        lambda tool_names: {"record_tool": _RecordingTool(executed)} if "record_tool" in list(tool_names or []) else {},
    )
    monkeypatch.setattr(runner, "_build_tool_runtime_context", lambda **kwargs: {"on_progress": _noop_progress})
    monkeypatch.setattr(runner, "_execute_tool_call_with_raw_result", _execute_tool_call)

    state = {
        **initial_persistent_state(user_input={"content": "hello", "metadata": {}}),
        "tool_names": ["record_tool"],
    }
    tools = runner._frontdoor_tool_schemas_for_state(
        state=state,
        runtime=SimpleNamespace(context=SimpleNamespace()),
    )
    assert _frontdoor_schema_names(tools) == {STAGE_TOOL_NAME, SILENT_TOOL_NAME, "record_tool"}

    first_update, first_row = await _execute_one_frontdoor_tool(
        runner,
        state=state,
        tool_name="record_tool",
        arguments={"value": "alpha"},
        call_id="call-ordinary-1",
    )
    first_text = str(first_row.get("content") or "")
    assert first_text.startswith('{\"ok\": true, \"value\": \"alpha\"}')
    assert "宽限执行" in first_text
    assert executed == ["alpha"]
    assert list((first_update.get("frontdoor_stage_state") or {}).get("pending_orphan_rounds") or [])

    second_state = {**state, "frontdoor_stage_state": first_update["frontdoor_stage_state"]}
    _second_update, second_row = await _execute_one_frontdoor_tool(
        runner,
        state=second_state,
        tool_name="record_tool",
        arguments={"value": "beta"},
        call_id="call-ordinary-2",
    )
    assert str(second_row.get("content") or "").startswith(
        "Error: no active stage; call submit_next_stage before using other tools"
    )
    assert executed == ["alpha"]

@pytest.mark.asyncio
async def test_frontdoor_stage_budget_exhaustion_updates_gate_and_blocks_next_ordinary_tool(monkeypatch) -> None:
    runner = CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace())
    executed, _execute_tool_call = _six_tuple_executor()

    async def _noop_progress(*args, **kwargs) -> None:
        _ = args, kwargs

    monkeypatch.setattr(
        runner,
        "_registered_tools",
        lambda tool_names: {"record_tool": _RecordingTool(executed)} if "record_tool" in list(tool_names or []) else {},
    )
    monkeypatch.setattr(runner, "_build_tool_runtime_context", lambda **kwargs: {"on_progress": _noop_progress})
    monkeypatch.setattr(runner, "_execute_tool_call_with_raw_result", _execute_tool_call)

    active_state = {
        **initial_persistent_state(user_input={"content": "hello", "metadata": {}}),
        "tool_names": ["record_tool"],
        "frontdoor_stage_state": _active_frontdoor_stage_state(budget=1),
    }
    tools = runner._frontdoor_tool_schemas_for_state(
        state=active_state,
        runtime=SimpleNamespace(context=SimpleNamespace()),
    )
    assert _frontdoor_schema_names(tools) == {STAGE_TOOL_NAME, SILENT_TOOL_NAME, "record_tool"}

    _run_update, ordinary_row = await _execute_one_frontdoor_tool(
        runner,
        state=active_state,
        tool_name="record_tool",
        arguments={"value": "alpha"},
        call_id="call-tool-1",
    )
    ordinary_result_text = str(ordinary_row.get("content") or "")
    assert ordinary_row.get("status") == "success" or ordinary_result_text.startswith('{\"ok\"')

    updated = await runner._postprocess_completed_tool_cycle(
        state={
            **active_state,
            "tool_call_payloads": [
                _tool_call_payload(
                    call_id="call-tool-1",
                    tool_name="record_tool",
                    arguments={"value": "alpha"},
                )
            ],
            "messages": [
                {"role": "user", "content": "hello"},
                _assistant_tool_call_record(
                    call_id="call-tool-1",
                    tool_name="record_tool",
                    arguments={"value": "alpha"},
                ),
                _tool_message(
                    call_id="call-tool-1",
                    tool_name="record_tool",
                    result_text=ordinary_result_text,
                ),
            ],
        }
    )

    assert updated is not None
    assert updated["frontdoor_stage_state"]["transition_required"] is True
    assert updated["frontdoor_stage_state"]["stages"][0]["tool_rounds_used"] == 1
    assert executed == ["alpha"]

    exhausted_state = {**active_state, "frontdoor_stage_state": updated["frontdoor_stage_state"]}
    grace_update, grace_row = await _execute_one_frontdoor_tool(
        runner,
        state=exhausted_state,
        tool_name="record_tool",
        arguments={"value": "beta"},
        call_id="call-tool-2",
    )
    grace_text = str(grace_row.get("content") or "")
    assert "当前阶段预算已耗尽" in grace_text and "宽限执行" in grace_text
    assert executed == ["alpha", "beta"]

    blocked_state = {**exhausted_state, "frontdoor_stage_state": grace_update["frontdoor_stage_state"]}
    _blocked_update, blocked_row = await _execute_one_frontdoor_tool(
        runner,
        state=blocked_state,
        tool_name="record_tool",
        arguments={"value": "gamma"},
        call_id="call-tool-3",
    )
    assert str(blocked_row.get("content") or "").startswith(
        "Error: current stage budget is exhausted; call submit_next_stage before using other tools"
    )
    assert executed == ["alpha", "beta"]

@pytest.mark.asyncio
async def test_frontdoor_without_valid_stage_keeps_runtime_visible_tools_stable(monkeypatch) -> None:
    runner = CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace())

    monkeypatch.setattr(
        runner,
        "_registered_tools",
        lambda tool_names: {"record_tool": _RecordingTool([])} if "record_tool" in list(tool_names or []) else {},
    )
    monkeypatch.setattr(runner, "_build_tool_runtime_context", lambda **kwargs: {"on_progress": None})

    no_stage_tools = runner._frontdoor_tool_schemas_for_state(
        state={
            **initial_persistent_state(user_input={"content": "hello", "metadata": {}}),
            "tool_names": ["record_tool", "load_tool_context", "filesystem_write"],
        },
        runtime=SimpleNamespace(context=SimpleNamespace()),
    )
    exhausted_tools = runner._frontdoor_tool_schemas_for_state(
        state={
            **initial_persistent_state(user_input={"content": "hello", "metadata": {}}),
            "tool_names": ["record_tool", "load_tool_context", "filesystem_write"],
            "frontdoor_stage_state": _active_frontdoor_stage_state(budget=1, used=1, transition_required=True),
        },
        runtime=SimpleNamespace(context=SimpleNamespace()),
    )

    assert _frontdoor_schema_names(no_stage_tools) == {STAGE_TOOL_NAME, SILENT_TOOL_NAME, "record_tool"}
    assert _frontdoor_schema_names(exhausted_tools) == {STAGE_TOOL_NAME, SILENT_TOOL_NAME, "record_tool"}

def test_frontdoor_stage_state_snapshot_preserves_archive_refs() -> None:
    snapshot = CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace())._frontdoor_stage_state_snapshot(
        {
            "frontdoor_stage_state": {
                "active_stage_id": "",
                "transition_required": False,
                "stages": [
                    {
                        "stage_id": "frontdoor-compression-1-10",
                        "stage_index": 10,
                        "stage_goal": "Archive completed stage history 1-10",
                        "tool_round_budget": 0,
                        "tool_rounds_used": 0,
                        "status": "completed",
                        "mode": "鑷富鎵ц",
                        "stage_kind": "compression",
                        "system_generated": True,
                        "completed_stage_summary": "Archived completed stages 1-10.",
                        "key_refs": [],
                        "archive_ref": "artifact:artifact:frontdoor-stage-archive",
                        "archive_stage_index_start": 1,
                        "archive_stage_index_end": 10,
                        "rounds": [],
                        "created_at": "2026-04-08T12:00:00",
                        "finished_at": "2026-04-08T12:00:01",
                    }
                ],
            }
        }
    )

    stage = snapshot["stages"][0]
    assert stage["stage_kind"] == "compression"
    assert stage["archive_ref"] == "artifact:artifact:frontdoor-stage-archive"
    assert stage["archive_stage_index_start"] == 1
    assert stage["archive_stage_index_end"] == 10


def test_frontdoor_submit_next_stage_marks_final_stage() -> None:
    runner = CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace())
    next_state, stage = runner._submit_frontdoor_next_stage_state(
        {"active_stage_id": "", "transition_required": False, "stages": []},
        stage_goal="final synthesis only",
        tool_round_budget=5,
        completed_stage_summary="",
        key_refs=[],
        final=True,
    )
    assert stage["final_stage"] is True
    assert next_state["stages"][0]["final_stage"] is True


def test_frontdoor_final_stage_does_not_require_transition_when_budget_is_exhausted() -> None:
    runner = CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace())
    stage_state, _stage = runner._submit_frontdoor_next_stage_state(
        {"active_stage_id": "", "transition_required": False, "stages": []},
        stage_goal="final synthesis only",
        tool_round_budget=5,
        completed_stage_summary="",
        key_refs=[],
        final=True,
    )
    updated = runner._record_frontdoor_stage_round(
        stage_state,
        tool_call_payloads=[{"id": "call:record", "name": "record_tool", "arguments": {"value": "alpha"}}],
    )
    assert updated["transition_required"] is False
    assert updated["stages"][0]["tool_rounds_used"] == 1
    assert updated["stages"][0]["final_stage"] is True


def test_frontdoor_eviction_archives_the_stage_once_and_survives_write_failure(tmp_path: Path) -> None:
    # 前门的读回不做新工具：账本是每会话的、工具 schema 从全局注册表解析，注册可执行
    # 实例会把 A 会话账本暴露给 B 会话。改为裁撤时导档 + 块带 archive_ref，回读走
    # content_open。这里钉住"一条阶段只导一次"和"写不成就谎称不可读"。
    runner = CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace())
    runner._ceo_session_temp_dir = lambda session_key: str(tmp_path)  # type: ignore[assignment]
    stage = {
        "stage_id": "frontdoor-stage-1",
        "stage_index": 1,
        "status": "completed",
        "stage_goal": "collect candidates",
        "completed_stage_summary": "确认了候选池口径",
        "context_evicted": True,
        "created_at": "2026-09-26T01:00:00+08:00",
        "key_refs": [],
        "rounds": [
            {
                "round_id": "frontdoor-stage-1:round-1",
                "round_index": 1,
                "tool_call_ids": ["c1"],
                "tool_names": ["exec"],
                "tools": [
                    {
                        "tool_call_id": "c1",
                        "tool_name": "exec",
                        "status": "success",
                        "arguments_text": "ls -1",
                        "output_text": "file.txt",
                    }
                ],
            }
        ],
    }
    state = {
        "active_stage_id": "frontdoor-stage-2",
        "transition_required": False,
        "stages": [dict(stage), {"stage_id": "frontdoor-stage-2", "stage_index": 2, "status": "active"}],
    }

    ref = runner._frontdoor_archive_evicted_stage(session_key="ext:x", stage_state=state, stage_id="frontdoor-stage-1")

    assert ref and Path(ref).exists()
    assert state["stages"][0]["archive_ref"] == ref
    assert state["stages"][0]["archive_stage_index_end"] == 1
    document = json.loads(Path(ref).read_text(encoding="utf-8"))
    assert document["kind"] == "frontdoor_stage_eviction"
    archived_tool = document["stages"][0]["rounds"][0]["tools"][0]
    assert archived_tool["arguments_text"] == "ls -1"
    assert archived_tool["output_text"] == "file.txt"

    # 再点一次不重写文件，只复用 ref。
    files_after_first = sorted(p.name for p in tmp_path.glob("*.json"))
    assert runner._frontdoor_archive_evicted_stage(session_key="ext:x", stage_state=state, stage_id="frontdoor-stage-1") == ref
    assert sorted(p.name for p in tmp_path.glob("*.json")) == files_after_first

    # 未被点名裁撤的阶段不导档。
    kept = {"active_stage_id": "", "transition_required": False, "stages": [{**stage, "context_evicted": False}]}
    assert runner._frontdoor_archive_evicted_stage(session_key="ext:x", stage_state=kept, stage_id="frontdoor-stage-1") == ""
    assert "archive_ref" not in kept["stages"][0]

    # 导不了盘就不留指针，也不谎称可读回。
    runner._frontdoor_write_stage_archive_file = lambda **kwargs: ("", 0, 0)  # type: ignore[assignment]
    blocked = {"active_stage_id": "", "transition_required": False, "stages": [dict(stage)]}
    assert runner._frontdoor_archive_evicted_stage(session_key="ext:x", stage_state=blocked, stage_id="frontdoor-stage-1") == ""
    assert not blocked["stages"][0].get("archive_ref")


def test_frontdoor_durable_tool_cycle_persists_eviction_pointer(tmp_path: Path) -> None:
    # 裁撤的三样东西（标记、导出的文件、块里的指针）必须由 finalize 写 durable 账本那条
    # 路径产出。图闭包里的 mutable_stage_state 只是本轮工作副本，写在它上面的 archive_ref
    # 会被这里的返回值整个覆盖——上一版就栽在这里：文件照写、标记照落，块里却永远没有
    # archive_ref，提示词承诺的 content_open 回读成了空头支票。
    runner = CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace())
    runner._ceo_session_temp_dir = lambda session_key: str(tmp_path)  # type: ignore[assignment]
    durable_state = {
        "session_key": "web:evict",
        "frontdoor_stage_state": {
            "active_stage_id": "frontdoor-stage-1",
            "transition_required": False,
            "stages": [
                {
                    "stage_id": "frontdoor-stage-1",
                    "stage_index": 1,
                    "stage_kind": "normal",
                    "mode": "自主执行",
                    "status": "active",
                    "stage_goal": "collect candidates",
                    "completed_stage_summary": "",
                    "tool_round_budget": 5,
                    "tool_rounds_used": 1,
                    "created_at": "2026-09-26T01:00:00+08:00",
                    "finished_at": "",
                    "rounds": [
                        {
                            "round_id": "frontdoor-stage-1:round-1",
                            "round_index": 1,
                            "budget_counted": True,
                            "tool_names": ["exec"],
                            "tool_call_ids": ["call-exec-1"],
                            "tools": [
                                {
                                    "tool_call_id": "call-exec-1",
                                    "tool_name": "exec",
                                    "status": "success",
                                    "arguments_text": "dir",
                                    "output_text": "a.txt",
                                }
                            ],
                        }
                    ],
                }
            ],
        },
    }
    payload = {
        "id": "call-submit-1",
        "name": STAGE_TOOL_NAME,
        "arguments": {
            "stage_goal": "score candidates",
            "tool_round_budget": 5,
            "completed_stage_summary": "阶段1确认了候选池口径",
            "drop_completed_stage_tool_detail": True,
        },
    }

    updated = runner._frontdoor_stage_state_after_tool_cycle(
        durable_state,
        tool_call_payloads=[payload],
        tool_results=[{"tool_name": STAGE_TOOL_NAME, "status": "success", "result_text": "ok"}],
    )

    closed = updated["stages"][0]
    assert closed["context_evicted"] is True
    archive_ref = str(closed.get("archive_ref") or "")
    assert archive_ref and Path(archive_ref).exists()
    document = json.loads(Path(archive_ref).read_text(encoding="utf-8"))
    assert document["stages"][0]["rounds"][0]["tools"][0]["output_text"] == "a.txt"

    # 指针要活过下一轮的快照白名单，并真的进块——模型看到的读回承诺只有这一处载体。
    carried = runner._frontdoor_stage_state_snapshot({"frontdoor_stage_state": updated})
    blocks = completed_stage_blocks(carried, skip_stage_ids=set())
    assert len(blocks) == 1
    rendered = json.loads(str(blocks[0]["content"]).split("\n", 1)[1])
    assert rendered["evicted"] is True
    assert rendered["archive_ref"] == archive_ref


def test_frontdoor_eviction_mark_is_written_and_carried_by_every_rewriter() -> None:
    # 前门有两份账本和四处重写者。收口标记曾在"白名单漏字段"上栽过一次，裁撤标记
    # 走的是同一批落点，所以逐处钉住：写出、穿过快照白名单、不占窗口名额、
    # 不进重叠签名、去重时随逻辑阶段继承到存活副本。
    runner = CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace())
    opened, _ = runner._submit_frontdoor_next_stage_state(
        {"active_stage_id": "", "transition_required": False, "stages": []},
        stage_goal="collect candidates",
        tool_round_budget=5,
        completed_stage_summary="",
        key_refs=[],
    )
    opened = runner._record_frontdoor_stage_round(
        opened,
        tool_call_payloads=[{"id": "call:collect", "name": "exec", "arguments": {"command": "dir"}}],
    )
    closed, _ = runner._submit_frontdoor_next_stage_state(
        opened,
        stage_goal="score candidates",
        tool_round_budget=5,
        completed_stage_summary="阶段1确认了候选池口径",
        key_refs=[],
        drop_completed_stage_tool_detail=True,
    )

    first = closed["stages"][0]
    assert first["context_evicted"] is True
    # 默认态不落字段：逐条带布尔键会让存量大会话白涨体积。
    assert "context_evicted" not in closed["stages"][1]

    snapshot = runner._frontdoor_stage_state_snapshot({"frontdoor_stage_state": closed})
    assert snapshot["stages"][0]["context_evicted"] is True
    # 已裁撤的阶段离开保留集；活动阶段本来就不在 completed 集合里。
    assert retained_completed_stage_ids(snapshot) == set()

    unmarked = {key: value for key, value in first.items() if key != "context_evicted"}
    assert _completed_stage_overlap_signature(first) == _completed_stage_overlap_signature(unmarked)

    deduped = _dedupe_canonical_stages([dict(first), dict(unmarked)])
    assert len(deduped) == 1
    assert deduped[0].get("context_evicted") is True


def _settled_frontdoor_stage_ledger(*, stages: list[dict]) -> dict:
    """回合尾结清后的账本形态：没有活动阶段，最后一条是终态普通阶段。"""
    return {"active_stage_id": "", "transition_required": False, "stages": stages}


def _settled_frontdoor_stage(*, index: int, summary: str = "") -> dict:
    return {
        "stage_id": f"frontdoor-stage-{index}",
        "stage_index": index,
        "stage_kind": "normal",
        "mode": "自主执行",
        "status": "completed",
        "stage_goal": f"stage {index} goal",
        "completed_stage_summary": summary,
        "key_refs": [],
        "tool_round_budget": 5,
        "tool_rounds_used": 1,
        "created_at": f"2026-09-26T01:0{index}:00+08:00",
        "finished_at": f"2026-09-26T01:0{index}:30+08:00",
        "rounds": [
            {
                "round_id": f"frontdoor-stage-{index}:round-1",
                "round_index": 1,
                "budget_counted": True,
                "tool_names": ["exec"],
                "tool_call_ids": [f"call-exec-{index}"],
                "tools": [
                    {
                        "tool_call_id": f"call-exec-{index}",
                        "tool_name": "exec",
                        "status": "success",
                        "arguments_text": "dir",
                        "output_text": f"a{index}.txt",
                    }
                ],
            }
        ],
    }


def test_frontdoor_cross_turn_submit_attaches_summary_to_the_settled_stage() -> None:
    # 渠道会话一轮一阶段：回合尾把活动阶段结清并清空 active_stage_id，模型在**下一个回合**
    # 才发 submit_next_stage。此时它写的 completed_stage_summary / key_refs 语义上归属刚被
    # 结清的那条阶段，落点必须是它，而不是整块静默跳过（实测会话 ext:qq-official-*：
    # 12/12 条阶段 summary 为空、drop 从未兑现）。
    runner = CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace())
    state = _settled_frontdoor_stage_ledger(stages=[_settled_frontdoor_stage(index=1)])

    closed, _ = runner._submit_frontdoor_next_stage_state(
        state,
        stage_goal="score candidates",
        tool_round_budget=5,
        completed_stage_summary="阶段1确认了候选池口径",
        key_refs=[{"ref": "task:t1", "note": "口径依据"}],
        drop_completed_stage_tool_detail=True,
    )

    settled = closed["stages"][0]
    assert settled["completed_stage_summary"] == "阶段1确认了候选池口径"
    assert settled["key_refs"][0]["ref"] == "task:t1"
    assert settled["context_evicted"] is True
    assert closed["stages"][1]["stage_index"] == 2


def test_frontdoor_cross_turn_eviction_writes_archive_pointer(tmp_path: Path) -> None:
    # 跨回合裁撤要一路走到 durable 账本上的 archive_ref，否则提示词承诺的 content_open
    # 回读又是空头支票（实测该会话 temp/ceo 下归档文件数为 0）。
    runner = CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace())
    runner._ceo_session_temp_dir = lambda session_key: str(tmp_path)  # type: ignore[assignment]
    durable_state = {
        "session_key": "ext:qq-official-1:abc",
        "frontdoor_stage_state": _settled_frontdoor_stage_ledger(
            stages=[_settled_frontdoor_stage(index=1)]
        ),
    }
    payload = {
        "id": "call-submit-1",
        "name": STAGE_TOOL_NAME,
        "arguments": {
            "stage_goal": "score candidates",
            "tool_round_budget": 5,
            "completed_stage_summary": "阶段1确认了候选池口径",
            "drop_completed_stage_tool_detail": True,
        },
    }

    updated = runner._frontdoor_stage_state_after_tool_cycle(
        durable_state,
        tool_call_payloads=[payload],
        tool_results=[{"tool_name": STAGE_TOOL_NAME, "status": "success", "result_text": "ok"}],
    )

    settled = updated["stages"][0]
    assert settled["context_evicted"] is True
    archive_ref = str(settled.get("archive_ref") or "")
    assert archive_ref and Path(archive_ref).exists()
    rendered = json.loads(
        str(completed_stage_blocks(updated, skip_stage_ids=set())[0]["content"]).split("\n", 1)[1]
    )
    assert rendered["evicted"] is True
    assert rendered["archive_ref"] == archive_ref
    assert retained_completed_stage_ids(updated) == set()


def test_frontdoor_cross_turn_submit_without_summary_never_evicts() -> None:
    # 承接只允许在模型自带非空总结时发生。回合尾结清的阶段留空是既有合同（最终回复紧邻
    # 块之后），指针不得变成"顺手把上一轮回复抄成摘要"或无总结裁撤的入口。
    runner = CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace())
    state = _settled_frontdoor_stage_ledger(stages=[_settled_frontdoor_stage(index=1)])

    closed, _ = runner._submit_frontdoor_next_stage_state(
        state,
        stage_goal="score candidates",
        tool_round_budget=5,
        completed_stage_summary="",
        key_refs=[],
        drop_completed_stage_tool_detail=True,
    )

    settled = closed["stages"][0]
    assert settled["completed_stage_summary"] == ""
    assert "context_evicted" not in settled


def test_frontdoor_cross_turn_submit_does_not_rewrite_older_stages() -> None:
    # 承接窗口只有"最后一条"这么大：最新那条已经带总结时，说明它已被收尾过，
    # 后来的 submit 不得回头改写更早的阶段。
    runner = CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace())
    state = _settled_frontdoor_stage_ledger(
        stages=[
            _settled_frontdoor_stage(index=1, summary="阶段1结论"),
            _settled_frontdoor_stage(index=2, summary="阶段2结论"),
        ]
    )

    closed, _ = runner._submit_frontdoor_next_stage_state(
        state,
        stage_goal="score candidates",
        tool_round_budget=5,
        completed_stage_summary="阶段3结论",
        key_refs=[],
        drop_completed_stage_tool_detail=True,
    )

    assert [stage["completed_stage_summary"] for stage in closed["stages"][:2]] == [
        "阶段1结论",
        "阶段2结论",
    ]
    assert all("context_evicted" not in stage for stage in closed["stages"])


def test_frontdoor_stage_closure_report_tells_the_model_whether_it_landed(tmp_path: Path) -> None:
    # 提示词让模型"在同一次提交里带上 drop"，它就只能按契约文案倒推自己成功了没有。
    # 实测那次它先答"已移出"，下一轮发现没有 ref 又改口"可能被压缩折叠了或者导出失败"。
    # 所以给了收尾材料（summary 或 drop）就必须回报归属与是否落地。
    runner = CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace())
    runner._ceo_session_temp_dir = lambda session_key: str(tmp_path)  # type: ignore[assignment]
    state = _settled_frontdoor_stage_ledger(stages=[_settled_frontdoor_stage(index=1)])

    updated, payload = runner._frontdoor_submit_next_stage(
        state,
        session_key="ext:qq-official-1:abc",
        arguments={
            "stage_goal": "score candidates",
            "tool_round_budget": 5,
            "completed_stage_summary": "阶段1确认了候选池口径",
            "drop_completed_stage_tool_detail": True,
        },
    )

    closure = payload["stage_closure"]
    assert closure["target_stage_id"] == "frontdoor-stage-1"
    assert closure["summary_attached"] is True
    assert closure["evicted"] is True
    assert closure["reason"] == "applied"
    # 回报只挂在给模型看的副本上，账本里的新阶段记录保持原形状。
    assert "stage_closure" not in updated["stages"][1]

    # 没有收尾对象时（最新一条已带总结）不谎称生效。
    settled_twice = _settled_frontdoor_stage_ledger(
        stages=[
            _settled_frontdoor_stage(index=1, summary="阶段1结论"),
            _settled_frontdoor_stage(index=2, summary="阶段2结论"),
        ]
    )
    _, blocked = runner._frontdoor_submit_next_stage(
        settled_twice,
        session_key="ext:qq-official-1:abc",
        arguments={
            "stage_goal": "score candidates",
            "tool_round_budget": 5,
            "completed_stage_summary": "阶段3结论",
            "drop_completed_stage_tool_detail": True,
        },
    )
    assert blocked["stage_closure"]["evicted"] is False
    assert blocked["stage_closure"]["reason"] == "no_closing_target"
    # 落空时结果自带可读说明：模型不必再去猜"是不是被压缩折叠了"，也不会向用户谎报。
    assert "未生效" in blocked["stage_closure"]["note"]

    # 没给任何收尾材料的普通开阶段不添字段，存量会话的块与逐条布尔键都不因此涨体积。
    _, plain = runner._frontdoor_submit_next_stage(
        _settled_frontdoor_stage_ledger(stages=[]),
        session_key="ext:qq-official-1:abc",
        arguments={"stage_goal": "collect candidates", "tool_round_budget": 5},
    )
    assert "stage_closure" not in plain


@pytest.mark.asyncio
async def test_completed_frontdoor_stages_are_not_externalized_into_archives(tmp_path: Path, monkeypatch) -> None:
    service = MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        workspace_root=tmp_path,
        store_path=tmp_path / "runtime.sqlite3",
        files_base_dir=tmp_path / "tasks",
        artifact_dir=tmp_path / "artifacts",
        governance_store_path=tmp_path / "governance.sqlite3",
        execution_mode="web",
    )
    runner = CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace(main_task_service=service))

    stage_state = {
        "active_stage_id": "frontdoor-stage-22",
        "transition_required": True,
        "stages": [
            *[_completed_frontdoor_stage(index) for index in range(1, 22)],
            _active_progress_stage(22),
        ],
    }
    base_state = initial_persistent_state(user_input={"content": "hello", "metadata": {}})

    try:
        await service.startup()
        result = await runner._postprocess_completed_tool_cycle(
            state={
                **base_state,
                "session_key": "web:frontdoor-archive-demo",
                "frontdoor_stage_state": stage_state,
                "tool_call_payloads": [
                    _tool_call_payload(
                        call_id="call-stage-22",
                        tool_name=STAGE_TOOL_NAME,
                        arguments={
                            "stage_goal": "Stage 23",
                            "tool_round_budget": 6,
                            "completed_stage_summary": "finished stage 22",
                            "key_refs": [{"ref": "artifact:artifact:stage-22", "note": "note 22"}],
                        },
                    )
                ],
                "messages": [
                    {"role": "user", "content": "hello"},
                    _assistant_tool_call_record(
                        call_id="call-stage-22",
                        tool_name=STAGE_TOOL_NAME,
                        arguments={
                            "stage_goal": "Stage 23",
                            "tool_round_budget": 6,
                            "completed_stage_summary": "finished stage 22",
                            "key_refs": [{"ref": "artifact:artifact:stage-22", "note": "note 22"}],
                        },
                    ),
                    _tool_message(
                        call_id="call-stage-22",
                        tool_name=STAGE_TOOL_NAME,
                        result_text=json.dumps({"ok": True}, ensure_ascii=False),
                    ),
                ],
            }
        )

        assert result is not None
        stages = result["frontdoor_stage_state"]["stages"]
        # 外置归档已删除：完成阶段再多也原位保留，不再改写为归档压缩阶段。
        assert all(stage["stage_kind"] == "normal" for stage in stages)
        completed_normal = [
            stage["stage_index"]
            for stage in stages
            if stage["stage_kind"] == "normal" and stage["status"] != "active"
        ]
        assert completed_normal == list(range(1, 23))
        active_stage = next(stage for stage in stages if stage["status"] == "active")
        assert active_stage["stage_index"] == 23
        assert service.list_artifacts("frontdoor-stage-archive:web:frontdoor-archive-demo") == []
    finally:
        await service.close()
