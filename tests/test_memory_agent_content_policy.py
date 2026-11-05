"""记忆 Agent 的请求面也过内容审查词替换：工具结果被替换，原文留在工件外。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from g3ku.agent import memory_agent_runtime as memory_module
from g3ku.deployment import data_root as data_root_module
from g3ku.providers.base import LLMResponse, ToolCallRequest
from g3ku.security import content_policy as cp


@pytest.fixture(autouse=True)
def _isolated_data_root(tmp_path, monkeypatch):
    monkeypatch.setenv("G3KU_DATA_DIR", str(tmp_path))
    data_root_module.reset_cache()
    cp.load_policy(force=True)
    try:
        yield tmp_path
    finally:
        data_root_module.reset_cache()


class _FakeModel:
    def __init__(self) -> None:
        self.calls: list[list[dict]] = []

    async def chat(self, messages, tools=None):
        self.calls.append([dict(item) for item in list(messages or [])])
        if len(self.calls) == 1:
            return LLMResponse(
                content="",
                tool_calls=[
                    ToolCallRequest(
                        id="call-1",
                        name="memory_read_note",
                        arguments={"ref": "note-1"},
                    )
                ],
                finish_reason="tool_calls",
                usage={"input_tokens": 8, "output_tokens": 3},
            )
        return LLMResponse(
            content="done",
            tool_calls=[],
            finish_reason="stop",
            usage={"input_tokens": 8, "output_tokens": 3},
        )


@pytest.mark.asyncio
async def test_memory_agent_request_masks_tool_result(tmp_path: Path, monkeypatch) -> None:
    cp.save_policy(True, ["六四"])
    model = _FakeModel()
    monkeypatch.setattr(memory_module, "build_chat_model", lambda *args, **kwargs: model)

    manager = memory_module.MemoryManager.__new__(memory_module.MemoryManager)
    manager.notes_dir = tmp_path / "notes"
    manager.index_dir = tmp_path / "index"
    manager.persona_enabled = False
    manager.scope_label = ""
    manager.notes_dir.mkdir(parents=True, exist_ok=True)
    manager.index_dir.mkdir(parents=True, exist_ok=True)
    # 离屏重放用 `__new__` 起实例，真实 `__init__` 一定会设 `self.config`；这条尝试路径现在
    # 读 `config.document` 的预算（与提交闸同源），不补就直接 AttributeError。
    from g3ku.config.schema import MemoryDocumentConfig

    manager.config = SimpleNamespace(document=MemoryDocumentConfig())
    manager._memory_window_guard = lambda **kwargs: None
    async def _fake_execute(tools, call):
        return {
            "ok": True,
            "tool": str(call.get("name") or ""),
            "result": "六四",
        }

    manager._execute_memory_tool = _fake_execute
    persisted: list[dict] = []
    manager._persist_memory_request_artifact = lambda **kwargs: persisted.append(kwargs) or None

    runtime_config = SimpleNamespace(get_role_max_iterations=lambda role: 3)
    await manager._run_memory_agent_attempt(
        batch=memory_module.MemoryBatch(op="write"),
        runtime_config=runtime_config,
        before_text="before",
        repair_reason="",
        queue_request_ids=["req-1"],
        model_chain=["fake"],
        lane="memory",
    )

    assert len(model.calls) == 2
    tool_messages = [item for item in model.calls[1] if item.get("role") == "tool"]
    assert tool_messages
    content = str(tool_messages[0].get("content"))
    assert cp.REPLACEMENT_MARKER in content
    assert "六四" not in content
    assert cp.NOTICE_PREFIX in content
