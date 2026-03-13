from __future__ import annotations

from types import SimpleNamespace

import pytest

from g3ku.providers.openai_codex_provider import _convert_messages
from g3ku.org_graph.service.project_service import ProjectService
from g3ku.runtime.bootstrap_bridge import RuntimeBootstrapBridge


class _CloseSpy:
    def __init__(self):
        self.closed = 0

    def close(self) -> None:
        self.closed += 1


class _AsyncCloseSpy:
    def __init__(self):
        self.closed = 0

    async def close(self) -> None:
        self.closed += 1


def test_init_org_graph_runtime_reuses_existing_memory_manager(monkeypatch):
    shared_memory_manager = object()
    resolved_config = object()
    captured: dict[str, object] = {}

    class FakeProjectService:
        def __init__(self, config, resource_manager=None, *, memory_manager=None):
            captured['config'] = config
            captured['resource_manager'] = resource_manager
            captured['memory_manager'] = memory_manager
            self.monitor_service = 'monitor'

    loop = SimpleNamespace(
        org_graph_service=None,
        org_graph_monitor_service=None,
        resource_manager=None,
        memory_manager=shared_memory_manager,
        app_config=SimpleNamespace(org_graph=SimpleNamespace(enabled=True)),
    )

    monkeypatch.setattr('g3ku.runtime.bootstrap_bridge.resolve_org_graph_config', lambda config: resolved_config)
    monkeypatch.setattr('g3ku.runtime.bootstrap_bridge.ProjectService', FakeProjectService)

    RuntimeBootstrapBridge(loop).init_org_graph_runtime()

    assert captured['config'] is resolved_config
    assert captured['resource_manager'] is None
    assert captured['memory_manager'] is shared_memory_manager
    assert loop.org_graph_monitor_service == 'monitor'


@pytest.mark.asyncio
async def test_project_service_close_skips_shared_memory_manager():
    service = ProjectService.__new__(ProjectService)
    service._closed = False
    service._started = True
    service.registry = _AsyncCloseSpy()
    service.memory_manager = _CloseSpy()
    service._owns_memory_manager = False
    service.task_monitor_store = _CloseSpy()
    service.checkpoint_store = _CloseSpy()
    service.governance_store = _CloseSpy()
    service.store = _CloseSpy()

    await service.close()

    assert service.registry.closed == 1
    assert service.memory_manager.closed == 0
    assert service.task_monitor_store.closed == 1
    assert service.checkpoint_store.closed == 1
    assert service.governance_store.closed == 1
    assert service.store.closed == 1


@pytest.mark.asyncio
async def test_project_service_close_closes_owned_memory_manager():
    service = ProjectService.__new__(ProjectService)
    service._closed = False
    service._started = True
    service.registry = _AsyncCloseSpy()
    service.memory_manager = _CloseSpy()
    service._owns_memory_manager = True
    service.task_monitor_store = _CloseSpy()
    service.checkpoint_store = _CloseSpy()
    service.governance_store = _CloseSpy()
    service.store = _CloseSpy()

    await service.close()

    assert service.memory_manager.closed == 1


def test_convert_messages_strips_dangling_assistant_tool_calls():
    system_prompt, input_items = _convert_messages(
        [
            {"role": "system", "content": "sys"},
            {
                "role": "assistant",
                "content": "I will search.",
                "tool_calls": [
                    {
                        "id": "call_dangling|fc_deadbeef",
                        "type": "tool_call",
                        "function": {"name": "web_fetch", "arguments": '{"url":"https://example.com"}'},
                    }
                ],
            },
            {"role": "user", "content": "继续"},
        ]
    )

    assert system_prompt == "sys"
    assert input_items == [
        {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "I will search."}],
            "status": "completed",
            "id": "msg_1",
        },
        {"role": "user", "content": [{"type": "input_text", "text": "继续"}]},
    ]


def test_convert_messages_keeps_completed_tool_calls_and_outputs():
    system_prompt, input_items = _convert_messages(
        [
            {"role": "system", "content": "sys"},
            {
                "role": "assistant",
                "content": "Fetching now.",
                "tool_calls": [
                    {
                        "id": "call_ok|fc_good",
                        "type": "tool_call",
                        "function": {"name": "web_fetch", "arguments": '{"url":"https://example.com"}'},
                    }
                ],
            },
            {
                "role": "tool",
                "name": "web_fetch",
                "tool_call_id": "call_ok|fc_good",
                "content": '{"status":"ok"}',
            },
            {"role": "user", "content": "总结一下"},
        ]
    )

    assert system_prompt == "sys"
    assert input_items[0]["type"] == "message"
    assert input_items[1] == {
        "type": "function_call",
        "id": "fc_good",
        "call_id": "call_ok",
        "name": "web_fetch",
        "arguments": '{"url":"https://example.com"}',
    }
    assert input_items[2] == {
        "type": "function_call_output",
        "call_id": "call_ok",
        "output": [{"type": "input_text", "text": '{"status":"ok"}'}],
    }
    assert input_items[3] == {"role": "user", "content": [{"type": "input_text", "text": "总结一下"}]}
