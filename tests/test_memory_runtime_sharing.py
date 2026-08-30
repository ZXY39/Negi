from __future__ import annotations

import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from g3ku.providers.responses_protocol_helpers import _convert_messages
from g3ku.resources import ResourceManager
from g3ku.runtime.bootstrap_bridge import RuntimeBootstrapBridge
from g3ku.security import get_bootstrap_security_service


class _AsyncCloseSpy:
    def __init__(self):
        self.closed = 0

    async def close(self) -> None:
        self.closed += 1


def test_init_main_runtime_binds_configured_paths(monkeypatch, tmp_path):
    captured: dict[str, object] = {}

    class FakeMainRuntimeService:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    loop = SimpleNamespace(
        resource_manager=None,
        app_config=SimpleNamespace(
            main_runtime=SimpleNamespace(
                store_path=str(tmp_path / 'runtime.sqlite3'),
                files_base_dir=str(tmp_path / 'tasks'),
                artifact_dir=str(tmp_path / 'artifacts'),
                governance_store_path=str(tmp_path / 'governance.sqlite3'),
                default_max_depth=2,
                hard_max_depth=5,
            ),
            get_role_model_keys=lambda role: [f'{role}_model'],
        ),
        main_task_service=None,
    )

    monkeypatch.setattr('g3ku.runtime.bootstrap_bridge.ConfigChatBackend', lambda config: f'backend:{config!r}')
    monkeypatch.setattr('g3ku.runtime.bootstrap_bridge.MainRuntimeService', FakeMainRuntimeService)

    RuntimeBootstrapBridge(loop).init_main_runtime()

    assert captured['store_path'] == str(tmp_path / 'runtime.sqlite3')
    assert captured['files_base_dir'] == str(tmp_path / 'tasks')
    assert captured['artifact_dir'] == str(tmp_path / 'artifacts')
    assert captured['governance_store_path'] == str(tmp_path / 'governance.sqlite3')
    assert captured['execution_model_refs'] == ['execution_model']
    assert captured['acceptance_model_refs'] == ['inspection_model']


@pytest.mark.asyncio
async def test_close_mcp_closes_main_task_service_only():
    from g3ku.runtime.engine import AgentRuntimeEngine

    engine = AgentRuntimeEngine.__new__(AgentRuntimeEngine)
    engine._runtime_closed = False
    engine._consolidation_tasks = set()
    engine._commit_tasks = set()
    engine.background_pool = None
    engine.main_task_service = _AsyncCloseSpy()
    engine.memory_manager = None
    engine._checkpointer = None
    engine._checkpointer_cm = None

    await AgentRuntimeEngine.close_mcp(engine)

    assert engine.main_task_service.closed == 1


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


def test_sync_internal_tool_runtimes_reads_memory_runtime_manifest(tmp_path):
    workspace = tmp_path / 'workspace'
    (workspace / 'skills').mkdir(parents=True, exist_ok=True)
    (workspace / 'tools').mkdir(parents=True, exist_ok=True)
    shutil.copytree(Path(__file__).resolve().parents[1] / 'tools' / 'memory_runtime', workspace / 'tools' / 'memory_runtime')

    manager = ResourceManager(
        workspace,
        app_config=SimpleNamespace(
            resources=SimpleNamespace(
                enabled=True,
                skills_dir='skills',
                tools_dir='tools',
                manifest_name='resource.yaml',
                state_path='.g3ku/resources.state.json',
                reload=SimpleNamespace(enabled=True, poll_interval_ms=200, debounce_ms=100, lazy_reload_on_access=True, keep_last_good_version=True),
                locks=SimpleNamespace(lock_dir='.g3ku/resource-locks', logical_delete_guard=True, windows_fs_lock=True),
            )
        ),
    )
    manager.reload_now(trigger='test-bind')

    class _FakeMemoryManager:
        def __init__(self, workspace_path, cfg):
            self.workspace = workspace_path
            self.cfg = cfg
            self.store = object()
            self.closed = 0

        def close(self):
            self.closed += 1

    loop = SimpleNamespace(
        workspace=workspace,
        resource_manager=manager,
        _internal_tool_settings_fingerprints={},
        _memory_manager_cls=_FakeMemoryManager,
        memory_manager=None,
        commit_service=None,
        _memory_runtime_settings=None,
        _store=None,
        _store_enabled=False,
        _checkpointer_enabled=False,
        _checkpointer_backend='disabled',
        _checkpointer_path=None,
        _checkpointer=None,
        _checkpointer_cm=None,
    )

    try:
        changed = RuntimeBootstrapBridge(loop).sync_internal_tool_runtimes(force=True, reason='test')
        assert changed is True
        assert loop._memory_runtime_settings is not None
        assert loop._memory_runtime_settings.enabled is True
        assert loop._memory_runtime_settings.document.summary_max_chars == 300
        assert loop._memory_runtime_settings.document.document_max_chars == 20000
        assert loop._memory_runtime_settings.queue.batch_max_chars == 50000
        assert loop._memory_runtime_settings.queue.max_wait_seconds == 3
        assert loop.memory_manager is not None
    finally:
        manager.close()


def _governance_loop(tmp_path):
    return SimpleNamespace(
        workspace=tmp_path,
        resource_manager=None,
        _internal_tool_settings_fingerprints={},
        _memory_manager_cls=None,
        memory_manager=None,
        commit_service=None,
        _memory_runtime_settings=SimpleNamespace(enabled=True),
        _checkpointer_enabled=False,
        _checkpointer_backend='disabled',
        _checkpointer_path=None,
        _checkpointer=None,
        _checkpointer_cm=None,
        main_task_service=None,
    )


def test_init_memory_runtime_wires_checkpointer_governance_fields(tmp_path):
    loop = _governance_loop(tmp_path)
    cp_cfg = SimpleNamespace(
        backend='memory',
        max_checkpoints_per_thread=50,
        trim_interval_seconds=120.0,
        vacuum_min_file_size_bytes=1024,
        vacuum_interval_seconds=600.0,
    )
    RuntimeBootstrapBridge(loop).init_memory_runtime(
        SimpleNamespace(enabled=True, checkpointer=cp_cfg)
    )
    assert loop._checkpointer_max_checkpoints_per_thread == 50
    assert loop._checkpointer_trim_interval_seconds == 120.0
    assert loop._checkpointer_vacuum_min_file_size_bytes == 1024
    assert loop._checkpointer_vacuum_interval_seconds == 600.0


def test_init_memory_runtime_defaults_for_legacy_checkpointer_config(tmp_path):
    loop = _governance_loop(tmp_path)
    # Legacy/test configs may lack the governance attributes; the engine must
    # fall back to defaults instead of raising.
    RuntimeBootstrapBridge(loop).init_memory_runtime(
        SimpleNamespace(enabled=True, checkpointer=SimpleNamespace(backend='memory'))
    )
    assert loop._checkpointer_max_checkpoints_per_thread == 200
    assert loop._checkpointer_trim_interval_seconds == 300.0
    assert loop._checkpointer_vacuum_min_file_size_bytes == 512 * 1024 * 1024
    assert loop._checkpointer_vacuum_interval_seconds == 21600.0


def test_reset_memory_runtime_invalidates_frontdoor_cached_bindings() -> None:
    invalidations: list[str] = []

    class _Runner:
        def invalidate_runtime_bindings(self) -> None:
            invalidations.append("invalidated")

    loop = SimpleNamespace(
        commit_service=None,
        memory_manager=None,
        multi_agent_runner=_Runner(),
        _memory_runtime_settings=object(),
        _store=object(),
        _store_enabled=True,
        _checkpointer_enabled=True,
        _checkpointer_backend='sqlite',
        _checkpointer_path='checkpoints.sqlite3',
        _checkpointer=object(),
        _checkpointer_cm=object(),
    )

    RuntimeBootstrapBridge(loop)._reset_memory_runtime()

    assert invalidations == ["invalidated"]
