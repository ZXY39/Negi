from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from g3ku.agent.tools.base import Tool
from g3ku.providers.base import LLMResponse, ToolCallRequest
from main.runtime.react_loop import ReActToolLoop


class _FakeTaskStore:
    def __init__(self) -> None:
        self._task = SimpleNamespace(cancel_requested=False, pause_requested=False)
        self._node = None

    def get_task(self, task_id: str):
        _ = task_id
        return self._task

    def get_node(self, node_id: str):
        _ = node_id
        return self._node


class _FakeLogService:
    def __init__(self) -> None:
        self._store = _FakeTaskStore()
        self._content_store = None

    def set_pause_state(self, task_id: str, pause_requested: bool, is_paused: bool) -> None:
        _ = task_id, pause_requested, is_paused

    def update_node_input(self, *args, **kwargs) -> None:
        _ = args, kwargs

    def upsert_frame(self, *args, **kwargs) -> None:
        _ = args, kwargs

    def append_node_output(self, *args, **kwargs) -> None:
        _ = args, kwargs

    def update_frame(self, *args, **kwargs) -> None:
        _ = args, kwargs

    def remove_frame(self, *args, **kwargs) -> None:
        _ = args, kwargs

    def read_runtime_frame(self, *args, **kwargs):
        _ = args, kwargs
        return None


class _SubmitFinalResultTool(Tool):
    @property
    def name(self) -> str:
        return "submit_final_result"

    @property
    def description(self) -> str:
        return "submit final result payload"

    @property
    def parameters(self) -> dict[str, object]:
        return {
            "type": "object",
            "properties": {
                "status": {"type": "string"},
                "delivery_status": {"type": "string"},
                "summary": {"type": "string"},
                "answer": {"type": "string"},
                "evidence": {"type": "array"},
                "remaining_work": {"type": "array"},
                "blocking_reason": {"type": "string"},
            },
            "required": [
                "status",
                "delivery_status",
                "summary",
                "answer",
                "evidence",
                "remaining_work",
                "blocking_reason",
            ],
        }

    async def execute(self, **kwargs: Any) -> Any:
        return dict(kwargs)


def test_normalize_tool_call_arguments_handles_non_mapping_inputs() -> None:
    assert ReActToolLoop._normalize_tool_call_arguments(["status"]) == {}
    assert ReActToolLoop._normalize_tool_call_arguments([("status", "success")]) == {"status": "success"}
    assert ReActToolLoop._normalize_tool_call_arguments('{"status":"success"}') == {"status": "success"}


@pytest.mark.asyncio
async def test_react_loop_survives_non_mapping_submit_final_result_arguments() -> None:
    requests: list[dict[str, object]] = []

    class _Backend:
        def __init__(self) -> None:
            self._responses = [
                LLMResponse(
                    content="",
                    tool_calls=[
                        ToolCallRequest(
                            id="call:final-invalid",
                            name="submit_final_result",
                            arguments=["status"],  # type: ignore[arg-type]
                        )
                    ],
                    finish_reason="tool_calls",
                    usage={"input_tokens": 8, "output_tokens": 3},
                ),
            ]

        async def chat(self, **kwargs):
            requests.append(dict(kwargs))
            return self._responses.pop(0)

    loop = ReActToolLoop(chat_backend=_Backend(), log_service=_FakeLogService(), max_iterations=3)
    result = await loop.run(
        task=SimpleNamespace(task_id="task-non-mapping-final-args"),
        node=SimpleNamespace(node_id="node-non-mapping-final-args", depth=0, node_kind="execution"),
        messages=[
            {"role": "system", "content": "system"},
            {"role": "user", "content": '{"task_id":"task-non-mapping-final-args","goal":"demo"}'},
        ],
        tools={"submit_final_result": _SubmitFinalResultTool()},
        model_refs=["fake"],
        runtime_context={"task_id": "task-non-mapping-final-args", "node_id": "node-non-mapping-final-args"},
        max_iterations=3,
    )

    assert result.status == "failed"
    assert result.delivery_status == "blocked"
    assert len(requests) == 1
