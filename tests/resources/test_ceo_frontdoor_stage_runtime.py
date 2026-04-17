from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from g3ku.agent.tools.base import Tool
from g3ku.runtime.frontdoor._ceo_create_agent_impl import CreateAgentCeoFrontDoorRunner
from g3ku.runtime.frontdoor.state_models import initial_persistent_state
from main.service.runtime_service import MainRuntimeService
from main.runtime.stage_budget import STAGE_TOOL_NAME


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


def _frontdoor_stage_archive_task_id(session_id: str) -> str:
    return f"frontdoor-stage-archive:{str(session_id or '').strip()}"


@pytest.mark.asyncio
async def test_frontdoor_stage_tool_is_visible_and_stage_creation_persists_in_state(monkeypatch) -> None:
    runner = CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace())
    executed: list[str] = []

    async def _noop_progress(*args, **kwargs) -> None:
        _ = args, kwargs

    async def _execute_tool_call(*, tool, tool_name, arguments, runtime_context, on_progress):
        _ = tool_name, runtime_context, on_progress
        return await tool.execute(**arguments), "success", "2026-04-08T10:00:00", "2026-04-08T10:00:01", 1.0

    monkeypatch.setattr(
        runner,
        "_registered_tools",
        lambda tool_names: {"record_tool": _RecordingTool(executed)} if "record_tool" in list(tool_names or []) else {},
    )
    monkeypatch.setattr(runner, "_build_tool_runtime_context", lambda **kwargs: {"on_progress": _noop_progress})
    monkeypatch.setattr(runner, "_execute_tool_call", _execute_tool_call)

    base_state = initial_persistent_state(user_input={"content": "hello", "metadata": {}})
    tools = runner._build_langchain_tools_for_state(
        state={**base_state, "tool_names": ["record_tool"]},
        runtime=SimpleNamespace(context=SimpleNamespace()),
    )
    tools_by_name = {str(getattr(tool, "name", "") or ""): tool for tool in tools}

    assert set(tools_by_name) == {STAGE_TOOL_NAME, "record_tool"}

    stage_result = await tools_by_name[STAGE_TOOL_NAME].ainvoke(
        {
            "stage_goal": "Inspect the current request",
            "tool_round_budget": 5,
        }
    )
    stage_payload = json.loads(str(stage_result["result_text"]))

    result = await runner._postprocess_completed_tool_cycle(
        state={
            **base_state,
            "tool_names": ["record_tool"],
            "tool_call_payloads": [
                _tool_call_payload(
                    call_id="call-stage-1",
                    tool_name=STAGE_TOOL_NAME,
                    arguments={
                        "stage_goal": "Inspect the current request",
                        "tool_round_budget": 5,
                    },
                )
            ],
            "messages": [
                {"role": "user", "content": "hello"},
                _assistant_tool_call_record(
                    call_id="call-stage-1",
                    tool_name=STAGE_TOOL_NAME,
                    arguments={
                        "stage_goal": "Inspect the current request",
                        "tool_round_budget": 5,
                    },
                ),
                _tool_message(
                    call_id="call-stage-1",
                    tool_name=STAGE_TOOL_NAME,
                    result_text=str(stage_result["result_text"]),
                ),
            ],
        }
    )

    assert stage_payload["stage_goal"] == "Inspect the current request"
    assert stage_payload["tool_round_budget"] == 5
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
    }
    assert executed == []


@pytest.mark.asyncio
async def test_frontdoor_stage_gate_keeps_ordinary_tools_visible_but_blocks_them_before_first_stage(monkeypatch) -> None:
    runner = CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace())
    executed: list[str] = []

    async def _noop_progress(*args, **kwargs) -> None:
        _ = args, kwargs

    async def _execute_tool_call(*, tool, tool_name, arguments, runtime_context, on_progress):
        _ = tool_name, runtime_context, on_progress
        return await tool.execute(**arguments), "success", "2026-04-08T10:00:00", "2026-04-08T10:00:01", 1.0

    monkeypatch.setattr(
        runner,
        "_registered_tools",
        lambda tool_names: {"record_tool": _RecordingTool(executed)} if "record_tool" in list(tool_names or []) else {},
    )
    monkeypatch.setattr(runner, "_build_tool_runtime_context", lambda **kwargs: {"on_progress": _noop_progress})
    monkeypatch.setattr(runner, "_execute_tool_call", _execute_tool_call)

    state = {
        **initial_persistent_state(user_input={"content": "hello", "metadata": {}}),
        "tool_names": ["record_tool"],
    }
    tools = runner._build_langchain_tools_for_state(
        state=state,
        runtime=SimpleNamespace(context=SimpleNamespace()),
    )
    tools_by_name = {str(getattr(tool, "name", "") or ""): tool for tool in tools}
    blocked_result = await tools_by_name["record_tool"].ainvoke({"value": "alpha"})

    assert set(tools_by_name) == {STAGE_TOOL_NAME, "record_tool"}
    assert blocked_result["status"] == "error"
    assert str(blocked_result["result_text"]).startswith(
        "Error: no active stage; call submit_next_stage before using other tools"
    )
    assert executed == []


@pytest.mark.asyncio
async def test_frontdoor_stage_budget_exhaustion_updates_gate_and_blocks_next_ordinary_tool(monkeypatch) -> None:
    runner = CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace())
    executed: list[str] = []

    async def _noop_progress(*args, **kwargs) -> None:
        _ = args, kwargs

    async def _execute_tool_call(*, tool, tool_name, arguments, runtime_context, on_progress):
        _ = tool_name, runtime_context, on_progress
        return await tool.execute(**arguments), "success", "2026-04-08T10:00:00", "2026-04-08T10:00:01", 1.0

    monkeypatch.setattr(
        runner,
        "_registered_tools",
        lambda tool_names: {"record_tool": _RecordingTool(executed)} if "record_tool" in list(tool_names or []) else {},
    )
    monkeypatch.setattr(runner, "_build_tool_runtime_context", lambda **kwargs: {"on_progress": _noop_progress})
    monkeypatch.setattr(runner, "_execute_tool_call", _execute_tool_call)

    active_state = {
        **initial_persistent_state(user_input={"content": "hello", "metadata": {}}),
        "tool_names": ["record_tool"],
        "frontdoor_stage_state": _active_frontdoor_stage_state(budget=1),
    }
    tools = runner._build_langchain_tools_for_state(
        state=active_state,
        runtime=SimpleNamespace(context=SimpleNamespace()),
    )
    tools_by_name = {str(getattr(tool, "name", "") or ""): tool for tool in tools}

    ordinary_result = await tools_by_name["record_tool"].ainvoke({"value": "alpha"})

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
                    result_text=str(ordinary_result["result_text"]),
                ),
            ],
        }
    )

    assert ordinary_result["status"] == "success"
    assert updated is not None
    assert updated["frontdoor_stage_state"]["transition_required"] is True
    assert updated["frontdoor_stage_state"]["stages"][0]["tool_rounds_used"] == 1
    assert executed == ["alpha"]

    exhausted_tools = runner._build_langchain_tools_for_state(
        state={
            **active_state,
            "frontdoor_stage_state": updated["frontdoor_stage_state"],
        },
        runtime=SimpleNamespace(context=SimpleNamespace()),
    )
    exhausted_tools_by_name = {str(getattr(tool, "name", "") or ""): tool for tool in exhausted_tools}
    blocked_after_exhaustion = await exhausted_tools_by_name["record_tool"].ainvoke({"value": "beta"})
    assert set(exhausted_tools_by_name) == {STAGE_TOOL_NAME, "record_tool"}
    assert blocked_after_exhaustion["status"] == "error"
    assert str(blocked_after_exhaustion["result_text"]).startswith(
        "Error: current stage budget is exhausted; call submit_next_stage before using other tools"
    )


@pytest.mark.asyncio
async def test_frontdoor_without_valid_stage_keeps_runtime_visible_tools_stable(monkeypatch) -> None:
    runner = CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace())

    monkeypatch.setattr(
        runner,
        "_registered_tools",
        lambda tool_names: {"record_tool": _RecordingTool([])} if "record_tool" in list(tool_names or []) else {},
    )
    monkeypatch.setattr(runner, "_build_tool_runtime_context", lambda **kwargs: {"on_progress": None})

    no_stage_tools = runner._build_langchain_tools_for_state(
        state={
            **initial_persistent_state(user_input={"content": "hello", "metadata": {}}),
            "tool_names": ["record_tool", "load_tool_context", "filesystem_write"],
        },
        runtime=SimpleNamespace(context=SimpleNamespace()),
    )
    no_stage_tool_names = {str(getattr(tool, "name", "") or "") for tool in no_stage_tools}

    exhausted_tools = runner._build_langchain_tools_for_state(
        state={
            **initial_persistent_state(user_input={"content": "hello", "metadata": {}}),
            "tool_names": ["record_tool", "load_tool_context", "filesystem_write"],
            "frontdoor_stage_state": _active_frontdoor_stage_state(budget=1, used=1, transition_required=True),
        },
        runtime=SimpleNamespace(context=SimpleNamespace()),
    )
    exhausted_tool_names = {str(getattr(tool, "name", "") or "") for tool in exhausted_tools}

    assert no_stage_tool_names == {STAGE_TOOL_NAME, "record_tool"}
    assert exhausted_tool_names == {STAGE_TOOL_NAME, "record_tool"}


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


@pytest.mark.asyncio
async def test_completed_frontdoor_stage_archives_oldest_ten_and_inserts_compression_stage(tmp_path: Path, monkeypatch) -> None:
    service = MainRuntimeService(
        chat_backend=_DummyChatBackend(),
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
        compression_stages = [stage for stage in stages if stage["stage_kind"] == "compression"]
        assert len(compression_stages) == 1
        compression = compression_stages[0]
        assert compression["archive_stage_index_start"] == 1
        assert compression["archive_stage_index_end"] == 10
        assert str(compression["archive_ref"]).startswith("artifact:")

        completed_normal = [
            stage["stage_index"]
            for stage in stages
            if stage["stage_kind"] == "normal" and stage["status"] != "active"
        ]
        assert completed_normal == list(range(11, 23))
        active_stage = next(stage for stage in stages if stage["status"] == "active")
        assert active_stage["stage_index"] == 23

        archive_artifact_id = str(compression["archive_ref"]).split(":", 1)[1]
        archive_artifact = service.get_artifact(archive_artifact_id)
        assert archive_artifact is not None
        assert archive_artifact.task_id == _frontdoor_stage_archive_task_id("web:frontdoor-archive-demo")
        archive_payload = json.loads(Path(archive_artifact.path).read_text(encoding="utf-8"))
        assert archive_payload["session_id"] == "web:frontdoor-archive-demo"
        assert archive_payload["stage_index_start"] == 1
        assert archive_payload["stage_index_end"] == 10
        assert len(archive_payload["stages"]) == 10
        assert archive_payload["stages"][0]["key_refs"] == [
            {"ref": "artifact:artifact:stage-1", "note": "note 1"}
        ]
    finally:
        await service.close()
