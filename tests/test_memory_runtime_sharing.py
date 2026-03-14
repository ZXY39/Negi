from __future__ import annotations

import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from g3ku.agent.rag_memory import G3kuHybridStore
from g3ku.providers.openai_codex_provider import _convert_messages
from g3ku.resources import ResourceManager
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


def test_g3ku_hybrid_store_reuses_qdrant_backend_per_process(tmp_path, monkeypatch):
    calls = {"existing": 0, "closed": 0}

    class FakeDenseStore:
        def close(self) -> None:
            calls["closed"] += 1

    class FakeQdrantVectorStore:
        @classmethod
        def from_existing_collection(cls, **kwargs):
            _ = kwargs
            calls["existing"] += 1
            return FakeDenseStore()

    monkeypatch.setitem(
        sys.modules,
        "langchain_qdrant",
        SimpleNamespace(QdrantVectorStore=FakeQdrantVectorStore),
    )
    monkeypatch.setattr(
        "g3ku.agent.rag_memory.DashScopeMultimodalEmbeddings",
        lambda **kwargs: object(),
    )

    G3kuHybridStore._dense_backend_registry.clear()
    store1 = G3kuHybridStore(
        sqlite_path=tmp_path / "memory.db",
        qdrant_path=tmp_path / "qdrant",
        qdrant_collection="test_collection",
        embedding_model="dashscope:qwen3-vl-embedding",
        dashscope_api_key="test-key",
    )
    store2 = G3kuHybridStore(
        sqlite_path=tmp_path / "memory.db",
        qdrant_path=tmp_path / "qdrant",
        qdrant_collection="test_collection",
        embedding_model="dashscope:qwen3-vl-embedding",
        dashscope_api_key="test-key",
    )

    try:
        assert calls["existing"] == 1
        assert store1._qdrant is store2._qdrant
        store1.close()
        assert calls["closed"] == 0
        store2.close()
        assert calls["closed"] == 1
    finally:
        G3kuHybridStore._dense_backend_registry.clear()



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

    class _Loop(SimpleNamespace):
        def _use_rag_memory(self) -> bool:
            cfg = getattr(self, '_memory_runtime_settings', None)
            return bool(cfg and cfg.enabled)

    loop = _Loop(
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
        assert loop.memory_manager is not None
        assert loop._store_enabled is True
    finally:
        manager.close()
