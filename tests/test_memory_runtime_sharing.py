from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from g3ku.agent.catalog_store import (
    ContextRecordV2,
    DashScopeMultimodalEmbeddings,
    DashScopeTextReranker,
    G3kuHybridStore,
    _load_workspace_dashscope_settings,
)
from g3ku.llm_config.enums import Capability
from g3ku.llm_config.facade import LLMConfigFacade, MEMORY_EMBEDDING_CONFIG_ID
from g3ku.llm_config.migration import _build_record
from g3ku.llm_config.models import NormalizedProviderConfig
from g3ku.providers.openai_codex_provider import _convert_messages
from g3ku.resources import ResourceManager
from g3ku.runtime.bootstrap_bridge import RuntimeBootstrapBridge
from g3ku.security import get_bootstrap_security_service


class _FakeRequestsResponse:
    def __init__(self, status_code: int, payload: dict):
        self.status_code = status_code
        self._payload = payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self) -> dict:
        return self._payload


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
        def __init__(self, **kwargs):
            _ = kwargs

        def close(self) -> None:
            calls["closed"] += 1

        def add_texts(self, *args, **kwargs) -> None:
            _ = args, kwargs

    class FakeQdrantVectorStore:
        def __init__(self, **kwargs):
            _ = kwargs
            calls["existing"] += 1
            self._store = FakeDenseStore()

        def __getattr__(self, name):
            return getattr(self._store, name)

    class FakeQdrantClient:
        def __init__(self, **kwargs):
            _ = kwargs

        def collection_exists(self, _collection_name):
            return True

        def close(self) -> None:
            return None

    monkeypatch.setitem(
        sys.modules,
        "langchain_qdrant",
        SimpleNamespace(QdrantVectorStore=FakeQdrantVectorStore),
    )
    monkeypatch.setitem(
        sys.modules,
        "qdrant_client",
        SimpleNamespace(
            QdrantClient=FakeQdrantClient,
            models=SimpleNamespace(
                VectorParams=lambda **kwargs: kwargs,
                Distance=SimpleNamespace(COSINE="cosine"),
            ),
        ),
    )
    monkeypatch.setattr(
        "g3ku.agent.catalog_store.DashScopeMultimodalEmbeddings",
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


def test_g3ku_hybrid_store_uses_dashscope_embedding_adapter_for_configured_protocol(tmp_path, monkeypatch):
    calls = {"existing": 0, "dashscope_model": "", "bootstrap_adds": 0}

    class FakeDenseStore:
        def __init__(self, **kwargs):
            _ = kwargs

        def close(self) -> None:
            return None

        def add_texts(self, *args, **kwargs) -> None:
            _ = args, kwargs
            calls["bootstrap_adds"] += 1

    class FakeQdrantVectorStore:
        def __init__(self, **kwargs):
            _ = kwargs
            calls["existing"] += 1
            self._store = FakeDenseStore()

        def __getattr__(self, name):
            return getattr(self._store, name)

    class FakeQdrantClient:
        def __init__(self, **kwargs):
            _ = kwargs

        def collection_exists(self, _collection_name):
            return False

        def create_collection(self, **kwargs):
            _ = kwargs
            return True

        def close(self) -> None:
            return None

    def _fake_dashscope_embeddings(**kwargs):
        calls["dashscope_model"] = str(kwargs.get("model") or "")

        class _Embeddings:
            def embed_documents(self, texts):
                _ = texts
                return [[0.1, 0.2, 0.3]]

        return _Embeddings()

    monkeypatch.setitem(
        sys.modules,
        "langchain_qdrant",
        SimpleNamespace(QdrantVectorStore=FakeQdrantVectorStore),
    )
    monkeypatch.setitem(
        sys.modules,
        "qdrant_client",
        SimpleNamespace(
            QdrantClient=FakeQdrantClient,
            models=SimpleNamespace(
                VectorParams=lambda **kwargs: kwargs,
                Distance=SimpleNamespace(COSINE="cosine"),
            ),
        ),
    )
    monkeypatch.setattr(
        "g3ku.agent.catalog_store.DashScopeMultimodalEmbeddings",
        _fake_dashscope_embeddings,
    )

    G3kuHybridStore._dense_backend_registry.clear()
    store = G3kuHybridStore(
        sqlite_path=tmp_path / "memory.db",
        qdrant_path=tmp_path / "qdrant",
        qdrant_collection="test_collection",
        embedding_model="dashscope:multimodal-embedding-v1",
        embedding_protocol_adapter="dashscope-embedding",
        dashscope_api_key="test-key",
    )

    try:
        assert calls["existing"] == 1
        assert calls["dashscope_model"] == "multimodal-embedding-v1"
        assert calls["bootstrap_adds"] == 1
        assert store._dense_enabled is True
    finally:
        store.close()
        G3kuHybridStore._dense_backend_registry.clear()


def test_g3ku_hybrid_store_skips_dense_backend_when_owner_lock_is_busy(tmp_path, monkeypatch):
    calls = {"existing": 0}

    class FakeQdrantVectorStore:
        def __init__(self, **kwargs):
            _ = kwargs
            calls["existing"] += 1

    class FakeQdrantClient:
        def __init__(self, **kwargs):
            _ = kwargs

        def collection_exists(self, _collection_name):
            return True

    monkeypatch.setitem(
        sys.modules,
        "langchain_qdrant",
        SimpleNamespace(QdrantVectorStore=FakeQdrantVectorStore),
    )
    monkeypatch.setitem(
        sys.modules,
        "qdrant_client",
        SimpleNamespace(
            QdrantClient=FakeQdrantClient,
            models=SimpleNamespace(
                VectorParams=lambda **kwargs: kwargs,
                Distance=SimpleNamespace(COSINE="cosine"),
            ),
        ),
    )
    monkeypatch.setattr(
        "g3ku.agent.catalog_store.DashScopeMultimodalEmbeddings",
        lambda **kwargs: object(),
    )
    monkeypatch.setattr("g3ku.agent.catalog_store._try_acquire_file_lock", lambda *args, **kwargs: None)

    G3kuHybridStore._dense_backend_registry.clear()
    store = G3kuHybridStore(
        sqlite_path=tmp_path / "memory.db",
        qdrant_path=tmp_path / "qdrant",
        qdrant_collection="test_collection",
        embedding_model="dashscope:qwen3-vl-embedding",
        dashscope_api_key="test-key",
    )

    try:
        assert calls["existing"] == 0
        assert store._qdrant is None
        assert store._dense_enabled is False
    finally:
        store.close()
        G3kuHybridStore._dense_backend_registry.clear()


def test_g3ku_hybrid_store_disables_dense_backend_for_worker_runtime(tmp_path, monkeypatch):
    calls = {"existing": 0}

    class FakeQdrantVectorStore:
        @classmethod
        def from_existing_collection(cls, **kwargs):
            _ = kwargs
            calls["existing"] += 1
            return object()

    monkeypatch.setenv("G3KU_TASK_RUNTIME_ROLE", "worker")
    monkeypatch.setitem(
        sys.modules,
        "langchain_qdrant",
        SimpleNamespace(QdrantVectorStore=FakeQdrantVectorStore),
    )

    G3kuHybridStore._dense_backend_registry.clear()
    store = G3kuHybridStore(
        sqlite_path=tmp_path / "memory.db",
        qdrant_path=tmp_path / "qdrant",
        qdrant_collection="test_collection",
        embedding_model="dashscope:qwen3-vl-embedding",
        dashscope_api_key="test-key",
    )

    try:
        assert calls["existing"] == 0
        assert store._qdrant is None
        assert store._dense_enabled is False
    finally:
        store.close()
        G3kuHybridStore._dense_backend_registry.clear()


def test_g3ku_hybrid_store_releases_owner_lock_after_last_reference(tmp_path, monkeypatch):
    calls = {"existing": 0, "closed": 0, "released": 0}
    owner_lock = object()

    class FakeDenseStore:
        def __init__(self, **kwargs):
            _ = kwargs

        def close(self) -> None:
            calls["closed"] += 1

        def add_texts(self, *args, **kwargs) -> None:
            _ = args, kwargs

    class FakeQdrantVectorStore:
        def __init__(self, **kwargs):
            _ = kwargs
            calls["existing"] += 1
            self._store = FakeDenseStore()

        def __getattr__(self, name):
            return getattr(self._store, name)

    class FakeQdrantClient:
        def __init__(self, **kwargs):
            _ = kwargs

        def collection_exists(self, _collection_name):
            return True

        def close(self) -> None:
            return None

    monkeypatch.setitem(
        sys.modules,
        "langchain_qdrant",
        SimpleNamespace(QdrantVectorStore=FakeQdrantVectorStore),
    )
    monkeypatch.setitem(
        sys.modules,
        "qdrant_client",
        SimpleNamespace(
            QdrantClient=FakeQdrantClient,
            models=SimpleNamespace(
                VectorParams=lambda **kwargs: kwargs,
                Distance=SimpleNamespace(COSINE="cosine"),
            ),
        ),
    )
    monkeypatch.setattr(
        "g3ku.agent.catalog_store.DashScopeMultimodalEmbeddings",
        lambda **kwargs: object(),
    )
    monkeypatch.setattr("g3ku.agent.catalog_store._try_acquire_file_lock", lambda *args, **kwargs: owner_lock)
    monkeypatch.setattr(
        "g3ku.agent.catalog_store._release_file_lock",
        lambda handle: calls.__setitem__("released", calls["released"] + int(handle is owner_lock)),
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
        store1.close()
        assert calls["released"] == 0
        store2.close()
        assert calls["closed"] == 1
        assert calls["released"] == 1
    finally:
        G3kuHybridStore._dense_backend_registry.clear()


def test_reset_memory_runtime_purges_stale_dense_backends_for_same_qdrant_group(tmp_path, monkeypatch):
    calls = {"closed": 0, "released": 0}
    owner_lock = object()
    qdrant_path = tmp_path / "qdrant"

    class FakeDenseStore:
        def close(self) -> None:
            calls["closed"] += 1

    class FakeMemoryManager:
        def __init__(self) -> None:
            self.closed = 0

        def close(self) -> None:
            self.closed += 1

    memory_manager = FakeMemoryManager()
    loop = SimpleNamespace(
        workspace=tmp_path,
        memory_manager=memory_manager,
        commit_service=None,
        _memory_runtime_settings=SimpleNamespace(
            store=SimpleNamespace(
                qdrant_path=str(qdrant_path),
                qdrant_collection="test_collection",
            )
        ),
        _store=object(),
        _store_enabled=True,
        _checkpointer_enabled=False,
        _checkpointer_backend="sqlite",
        _checkpointer_path=None,
        _checkpointer=None,
        _checkpointer_cm=None,
        multi_agent_runner=None,
    )

    monkeypatch.setattr(
        "g3ku.agent.catalog_store._release_file_lock",
        lambda handle: calls.__setitem__("released", calls["released"] + int(handle is owner_lock)),
    )

    G3kuHybridStore._dense_backend_registry.clear()
    G3kuHybridStore._dense_backend_registry[
        (str(qdrant_path.resolve()).lower(), "test_collection", "dashscope:qwen3-vl-embedding")
    ] = SimpleNamespace(
        store=FakeDenseStore(),
        refs=1,
        owner_lock=owner_lock,
    )

    try:
        RuntimeBootstrapBridge(loop)._reset_memory_runtime()

        assert memory_manager.closed == 1
        assert calls["closed"] == 1
        assert calls["released"] == 1
        assert G3kuHybridStore._dense_backend_registry == {}
    finally:
        G3kuHybridStore._dense_backend_registry.clear()


def test_context_v2_dense_backfill_reports_post_rebuild_stats(tmp_path, monkeypatch):
    class FakeQdrantClient:
        def __init__(self, **kwargs):
            _ = kwargs
            self.ids: set[str] = set()

        def collection_exists(self, _collection_name):
            return True

        def count(self, *, collection_name, exact=True):
            _ = collection_name, exact
            return SimpleNamespace(count=len(self.ids))

        def retrieve(self, *, collection_name, ids, with_payload=False, with_vectors=False):
            _ = collection_name, with_payload, with_vectors
            return [SimpleNamespace(id=point_id) for point_id in ids if point_id in self.ids]

        def close(self) -> None:
            return None

    class FakeQdrantVectorStore:
        def __init__(self, **kwargs):
            self.client = kwargs["client"]

        def add_texts(self, *, texts, metadatas, ids):
            _ = texts, metadatas
            self.client.ids.update(str(point_id) for point_id in ids)

        def close(self) -> None:
            return None

    monkeypatch.setitem(
        sys.modules,
        "langchain_qdrant",
        SimpleNamespace(QdrantVectorStore=FakeQdrantVectorStore),
    )
    monkeypatch.setitem(
        sys.modules,
        "qdrant_client",
        SimpleNamespace(
            QdrantClient=FakeQdrantClient,
            models=SimpleNamespace(
                VectorParams=lambda **kwargs: kwargs,
                Distance=SimpleNamespace(COSINE="cosine"),
            ),
        ),
    )
    monkeypatch.setattr(
        "g3ku.agent.catalog_store.DashScopeMultimodalEmbeddings",
        lambda **kwargs: object(),
    )

    G3kuHybridStore._dense_backend_registry.clear()
    store = G3kuHybridStore(
        sqlite_path=tmp_path / "memory.db",
        qdrant_path=tmp_path / "qdrant",
        qdrant_collection="test_collection",
        embedding_model="dashscope:qwen3-vl-embedding",
        dashscope_api_key="test-key",
    )

    try:
        store._dense_enabled = False
        store.put_context_v2(
            ("resource",),
            ContextRecordV2(
                record_id="tool:alpha",
                context_type="resource",
                uri="g3ku://resource/tool/alpha",
                l0="alpha l0",
                l1="alpha l1",
            ),
        )
        store.put_context_v2(
            ("skill",),
            ContextRecordV2(
                record_id="skill:beta",
                context_type="skill",
                uri="g3ku://skill/beta",
                l0="beta l0",
                l1="beta l1",
            ),
        )
        store._dense_enabled = True

        result = store.ensure_context_v2_dense_backfill(batch_size=8)

        assert result["needed"] is True
        assert result["eligible"] == 2
        assert result["indexed"] == 2
        assert result["dense_points"] == 2
        assert result["sample_missing"] is False
    finally:
        store.close()
        G3kuHybridStore._dense_backend_registry.clear()


def test_load_workspace_dashscope_settings_reads_api_key_from_security_overlay(tmp_path):
    workspace = tmp_path / "workspace"
    config_dir = workspace / ".g3ku"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "config.json").write_text(
        json.dumps(
            {
                "providers": {
                    "dashscope": {
                        "apiKey": "",
                        "apiBase": "https://dashscope.aliyuncs.com",
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    security = get_bootstrap_security_service(workspace)
    security.setup_initial_realm(password="test-password")
    security.set_overlay_values({"config.providers.dashscope.apiKey": "overlay-key"})

    api_key, api_base = _load_workspace_dashscope_settings(workspace)

    assert api_key == "overlay-key"
    assert api_base == "https://dashscope.aliyuncs.com"


def test_resolve_memory_target_hydrates_secret_overlay(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    security = get_bootstrap_security_service(workspace)
    security.setup_initial_realm(password="test-password")

    facade = LLMConfigFacade(workspace)
    config_id = _build_record(
        facade,
        config_id=MEMORY_EMBEDDING_CONFIG_ID,
        provider_id="dashscope",
        model_id="qwen3-vl-embedding",
        api_key="memory-overlay-key",
        api_base="https://dashscope.aliyuncs.com/compatible-mode/v1",
        extra_headers=None,
        capability=Capability.EMBEDDING,
    )
    record = facade.repository.get(config_id)
    assert isinstance(record, NormalizedProviderConfig)

    facade.repository.save(
        facade._sanitize_record_for_storage(record),
        last_probe_status=None,
    )
    facade._store_record_secrets(record)
    facade.set_memory_binding(
        embedding_config_id=MEMORY_EMBEDDING_CONFIG_ID,
        rerank_config_id=None,
    )

    target = facade.resolve_memory_target("embedding")

    assert target.config_id == MEMORY_EMBEDDING_CONFIG_ID
    assert target.secret_payload["api_key"] == "memory-overlay-key"


def test_dashscope_embeddings_rotate_api_keys_on_retryable_status(monkeypatch) -> None:
    calls: list[str] = []

    def _post(url, *, headers, json, timeout):
        _ = url, json, timeout
        calls.append(str(headers.get("Authorization", "")))
        if len(calls) == 1:
            return _FakeRequestsResponse(429, {"error": "rate limited"})
        return _FakeRequestsResponse(
            200,
            {"output": {"embeddings": [{"embedding": [0.1, 0.2], "text_index": 0}]}},
        )

    monkeypatch.setattr("g3ku.agent.catalog_store.requests.post", _post)

    embeddings = DashScopeMultimodalEmbeddings(api_key="key-1,key-2")
    vectors = embeddings.embed_documents(["hello"])

    assert vectors == [[0.1, 0.2]]
    assert calls == ["Bearer key-1", "Bearer key-2"]


def test_dashscope_reranker_rotates_api_keys_on_auth_failure(monkeypatch) -> None:
    calls: list[str] = []

    def _post(url, *, headers, json, timeout):
        _ = url, json, timeout
        calls.append(str(headers.get("Authorization", "")))
        if len(calls) == 1:
            return _FakeRequestsResponse(401, {"error": "unauthorized"})
        return _FakeRequestsResponse(
            200,
            {"output": {"results": [{"index": 0, "score": 0.9}]}},
        )

    monkeypatch.setattr("g3ku.agent.catalog_store.requests.post", _post)

    reranker = DashScopeTextReranker(api_key="key-1,key-2")
    ranked = reranker.rerank(query="hello", documents=["hello"])

    assert ranked == [(0, 0.9)]
    assert calls == ["Bearer key-1", "Bearer key-2"]



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
        assert loop._memory_runtime_settings.document.summary_max_chars == 100
        assert loop._memory_runtime_settings.document.document_max_chars == 10000
        assert loop._memory_runtime_settings.queue.batch_max_chars == 50000
        assert loop._memory_runtime_settings.queue.max_wait_seconds == 3
        assert loop.memory_manager is not None
        assert loop._store_enabled is True
    finally:
        manager.close()


def test_sync_internal_tool_runtimes_keeps_memory_manager_when_runtime_has_no_rag_store(tmp_path):
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

    class _FallbackOnlyMemoryManager:
        def __init__(self, workspace_path, cfg):
            self.workspace = workspace_path
            self.cfg = cfg
            self.store = None

        def close(self):
            return None

    loop = SimpleNamespace(
        workspace=workspace,
        resource_manager=manager,
        _internal_tool_settings_fingerprints={},
        _memory_manager_cls=_FallbackOnlyMemoryManager,
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
        assert loop.memory_manager is not None
        assert loop._store is None
        assert loop._store_enabled is False
    finally:
        manager.close()


def test_sync_internal_tool_runtimes_bootstraps_catalog_when_memory_store_is_available(tmp_path):
    workspace = tmp_path / 'workspace'
    (workspace / 'skills').mkdir(parents=True, exist_ok=True)
    (workspace / 'tools').mkdir(parents=True, exist_ok=True)

    class _FakeMemoryManager:
        def __init__(self, workspace_path, cfg):
            self.workspace = workspace_path
            self.cfg = cfg
            self.store = object()
            self.catalog_bootstrap_calls: list[object] = []

        async def ensure_catalog_bootstrap(self, service):
            self.catalog_bootstrap_calls.append(service)
            return {"ok": True, "synced": True, "created": 2, "updated": 0, "removed": 0}

        def close(self):
            return None

    loop = SimpleNamespace(
        workspace=workspace,
        resource_manager=None,
        _internal_tool_settings_fingerprints={},
        _memory_manager_cls=_FakeMemoryManager,
        memory_manager=None,
        commit_service=None,
        _memory_runtime_settings=SimpleNamespace(enabled=True),
        _store=None,
        _store_enabled=False,
        _checkpointer_enabled=False,
        _checkpointer_backend='disabled',
        _checkpointer_path=None,
        _checkpointer=None,
        _checkpointer_cm=None,
        main_task_service=SimpleNamespace(),
    )

    RuntimeBootstrapBridge(loop).init_memory_runtime(SimpleNamespace(enabled=True, checkpointer=SimpleNamespace(backend='memory')))

    assert loop.memory_manager is not None
    assert loop.memory_manager.catalog_bootstrap_calls == [loop.main_task_service]


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
