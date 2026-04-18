from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import sqlite3
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, ClassVar, Iterable, Literal

import requests
from langchain_core.embeddings import Embeddings
from langgraph.store.base import (
    BaseStore,
    GetOp,
    Item,
    ListNamespacesOp,
    MatchCondition,
    Op,
    PutOp,
    Result,
    SearchItem,
    SearchOp,
)
from loguru import logger

from g3ku.llm_config.enums import ProtocolAdapter
from g3ku.llm_config.runtime_resolver import resolve_memory_embedding_target
from g3ku.security.bootstrap import apply_config_secret_entries, get_bootstrap_security_service
from g3ku.utils.api_keys import parse_api_keys, should_switch_api_key_for_http_status
from g3ku.utils.helpers import ensure_dir, resolve_path_in_workspace

_NS_SEP = "\x1f"
ContextType = Literal["memory", "resource", "skill"]


def _now_iso() -> str:
    return datetime.now().isoformat()


def _encode_ns(namespace: tuple[str, ...]) -> str:
    return _NS_SEP.join(namespace)


def _decode_ns(raw: str) -> tuple[str, ...]:
    if not raw:
        return ()
    return tuple(part for part in raw.split(_NS_SEP) if part)


def _try_acquire_file_lock(path: Path, *, metadata: dict[str, object] | None = None) -> Any | None:
    ensure_dir(path.parent)
    handle = path.open("a+", encoding="utf-8")
    try:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None

    if metadata:
        handle.seek(0)
        handle.truncate(0)
        handle.write(json.dumps(metadata, ensure_ascii=False))
        handle.flush()
        try:
            os.fsync(handle.fileno())
        except OSError:
            pass
    return handle


def _release_file_lock(handle: Any) -> None:
    if handle is None:
        return
    try:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except Exception:
        pass
    try:
        handle.close()
    except Exception:
        pass


def _dense_owner_lock_path(qdrant_path: Path) -> Path:
    resolved = qdrant_path.expanduser().resolve()
    return resolved.parent / f".{resolved.name}.g3ku.dense.lock"


def _task_runtime_role() -> str:
    return str(os.getenv("G3KU_TASK_RUNTIME_ROLE", "embedded") or "embedded").strip().lower()


def _ns_prefix_match(namespace: tuple[str, ...], prefix: tuple[str, ...]) -> bool:
    if len(namespace) < len(prefix):
        return False
    return namespace[: len(prefix)] == prefix


def _passes_filter(value: dict[str, Any], flt: dict[str, Any] | None) -> bool:
    if not flt:
        return True
    for key, expected in flt.items():
        current = value.get(key)
        if isinstance(expected, dict):
            if "$eq" in expected and current != expected["$eq"]:
                return False
            if "$ne" in expected and current == expected["$ne"]:
                return False
            if "$gt" in expected and not (isinstance(current, (int, float)) and current > expected["$gt"]):
                return False
            if "$gte" in expected and not (isinstance(current, (int, float)) and current >= expected["$gte"]):
                return False
            if "$lt" in expected and not (isinstance(current, (int, float)) and current < expected["$lt"]):
                return False
            if "$lte" in expected and not (isinstance(current, (int, float)) and current <= expected["$lte"]):
                return False
            continue
        if current != expected:
            return False
    return True


def _rrf_fuse(dense_ids: list[str], sparse_ids: list[str], c: int = 60) -> list[str]:
    score: dict[str, float] = {}
    for rank, key in enumerate(dense_ids, start=1):
        score[key] = score.get(key, 0.0) + 1.0 / (c + rank)
    for rank, key in enumerate(sparse_ids, start=1):
        score[key] = score.get(key, 0.0) + 1.0 / (c + rank)
    return [k for k, _ in sorted(score.items(), key=lambda kv: kv[1], reverse=True)]


def _vector_point_id(namespace_raw: str, key: str) -> str:
    raw = f"g3ku:{namespace_raw}::{key}"
    return str(uuid.uuid5(uuid.NAMESPACE_URL, raw))


def _safe_fts_query(raw: str) -> str:
    text = str(raw or "").strip()
    if not text:
        return '""'
    cleaned = text.replace('"', " ").replace("'", " ").replace("`", " ")
    tokens = re.findall(r"[\w\u4e00-\u9fff]+", cleaned, flags=re.UNICODE)
    if not tokens:
        return '""'
    return " OR ".join(f'"{token}"' for token in tokens[:12])


def _normalize_provider_id(raw: str | None) -> str | None:
    if raw is None:
        return None
    text = str(raw).strip().lower().replace("-", "_")
    return text or None


def _split_provider_model(raw: str, *, default_provider: str | None = None) -> tuple[str | None, str]:
    text = str(raw or "").strip()
    if ":" in text:
        provider, model = text.split(":", 1)
        return _normalize_provider_id(provider), model.strip()
    return _normalize_provider_id(default_provider), text


def _dashscope_root_url(api_base: str | None) -> str:
    base = str(api_base or "https://dashscope.aliyuncs.com").strip()
    if not base:
        base = "https://dashscope.aliyuncs.com"
    base = base.rstrip("/")
    for suffix in ("/compatible-mode/v1", "/compatible-mode", "/v1"):
        if base.endswith(suffix):
            base = base[: -len(suffix)].rstrip("/")
            break
    return base


def _protocol_adapter_value(value: Any) -> str:
    raw = getattr(value, "value", value)
    return str(raw or "").strip().lower()


def _uses_dashscope_embedding_adapter(
    *,
    provider_id: str | None = None,
    protocol_adapter: Any = None,
    model: str | None = None,
) -> bool:
    normalized_provider = _normalize_provider_id(provider_id)
    normalized_protocol = _protocol_adapter_value(protocol_adapter)
    model_provider, _model_id = _split_provider_model(str(model or "").strip(), default_provider=None)
    return (
        normalized_protocol == ProtocolAdapter.DASHSCOPE_EMBEDDING.value
        or normalized_provider in {"dashscope", "dashscope_embedding"}
        or model_provider == "dashscope"
    )


def _extract_embedding_vectors(payload: dict[str, Any], expected_count: int) -> list[list[float]]:
    output = payload.get("output", {}) if isinstance(payload, dict) else {}
    rows = output.get("embeddings")
    if not isinstance(rows, list):
        raise RuntimeError("DashScope embedding response missing output.embeddings")

    vectors: list[list[float] | None] = [None] * expected_count
    for idx, row in enumerate(rows):
        vector: Any = None
        target_idx = idx
        if isinstance(row, dict):
            vector = row.get("embedding")
            for key in ("text_index", "index"):
                if isinstance(row.get(key), int):
                    target_idx = int(row[key])
                    break
        elif isinstance(row, list):
            vector = row
        if not isinstance(vector, list):
            continue
        if target_idx < 0 or target_idx >= expected_count:
            continue
        vectors[target_idx] = [float(v) for v in vector]

    if any(v is None for v in vectors):
        raise RuntimeError("DashScope embedding response is incomplete")
    return [v for v in vectors if v is not None]


def _extract_rerank_scores(payload: dict[str, Any]) -> list[tuple[int, float]]:
    output = payload.get("output", {}) if isinstance(payload, dict) else {}
    rows = output.get("results")
    if not isinstance(rows, list):
        return []

    out: list[tuple[int, float]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        index = row.get("index")
        score = row.get("relevance_score", row.get("score"))
        if not isinstance(index, int):
            continue
        if not isinstance(score, (int, float)):
            continue
        out.append((int(index), float(score)))
    return out


def _load_workspace_dashscope_settings(workspace: Path) -> tuple[str, str | None]:
    env_key = os.environ.get("DASHSCOPE_API_KEY", "").strip()
    env_base = os.environ.get("DASHSCOPE_API_BASE", "").strip() or None

    config_path = workspace / ".g3ku" / "config.json"
    if not config_path.exists():
        return env_key, env_base

    try:
        raw_data = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception:
        return env_key, env_base

    try:
        security = get_bootstrap_security_service(workspace)
        raw_data = apply_config_secret_entries(raw_data, security.current_overlay())
    except Exception:
        pass

    providers = raw_data.get("providers", {})
    if not isinstance(providers, dict):
        return env_key, env_base
    dashscope = providers.get("dashscope", {})
    if not isinstance(dashscope, dict):
        return env_key, env_base

    cfg_key = str(dashscope.get("apiKey", dashscope.get("api_key", "")) or "").strip()
    cfg_base = str(dashscope.get("apiBase", dashscope.get("api_base", "")) or "").strip() or None
    return cfg_key or env_key, cfg_base or env_base


def _dashscope_post_with_api_key_pool(
    *,
    endpoint: str,
    api_key_value: str,
    payload: dict[str, Any],
    timeout_s: float,
    label: str,
) -> requests.Response:
    api_keys = parse_api_keys(api_key_value)
    if not api_keys:
        raise RuntimeError(f"{label} API key is not configured")

    last_exc: Exception | None = None
    for key_index, api_key in enumerate(api_keys):
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        try:
            response = requests.post(endpoint, headers=headers, json=payload, timeout=timeout_s)
        except requests.RequestException as exc:
            last_exc = exc
            if key_index < len(api_keys) - 1:
                continue
            raise RuntimeError(f"{label} API call failed at {endpoint}") from exc

        if 200 <= response.status_code < 300:
            return response
        if should_switch_api_key_for_http_status(response.status_code) and key_index < len(api_keys) - 1:
            last_exc = RuntimeError(f"{label} API call failed ({response.status_code}) at {endpoint}")
            continue
        try:
            response.raise_for_status()
        except Exception as exc:
            raise RuntimeError(f"{label} API call failed ({response.status_code}) at {endpoint}") from exc
        raise RuntimeError(f"{label} API call failed ({response.status_code}) at {endpoint}")

    if last_exc is not None:
        raise RuntimeError(f"{label} API call failed at {endpoint}") from last_exc
    raise RuntimeError(f"{label} API call failed at {endpoint}")


class DashScopeMultimodalEmbeddings(Embeddings):
    def __init__(
        self,
        *,
        api_key: str,
        model: str = "qwen3-vl-embedding",
        api_base: str | None = None,
        batch_size: int = 32,
        timeout_s: float = 30.0,
    ):
        self.api_key = str(api_key or "").strip()
        self.model = model
        self.api_base = _dashscope_root_url(api_base)
        self.batch_size = max(1, int(batch_size or 1))
        self.timeout_s = float(timeout_s)
        self.endpoint = (
            f"{self.api_base}/api/v1/services/embeddings/multimodal-embedding/multimodal-embedding"
        )

    def _embed_batch(self, texts: list[str]) -> list[list[float]]:
        payload = {
            "model": self.model,
            "input": {"contents": [{"text": text} for text in texts]},
            "parameters": {"output_type": "dense"},
        }
        response = _dashscope_post_with_api_key_pool(
            endpoint=self.endpoint,
            api_key_value=self.api_key,
            payload=payload,
            timeout_s=self.timeout_s,
            label="DashScope embedding",
        )
        return _extract_embedding_vectors(response.json(), expected_count=len(texts))

    def _embed_batch_resilient(self, texts: list[str]) -> list[list[float]]:
        try:
            return self._embed_batch(texts)
        except Exception as exc:
            if len(texts) <= 1:
                raise
            message = str(exc)
            if "(400)" not in message and "failed (400)" not in message:
                raise
            mid = max(1, len(texts) // 2)
            logger.debug(
                "DashScope embedding batch rejected with 400; splitting batch of {} into {} + {}",
                len(texts),
                mid,
                len(texts) - mid,
            )
            return self._embed_batch_resilient(texts[:mid]) + self._embed_batch_resilient(texts[mid:])

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        normalized = [str(t or "") for t in texts]
        out: list[list[float]] = []
        for start in range(0, len(normalized), self.batch_size):
            chunk = normalized[start : start + self.batch_size]
            out.extend(self._embed_batch_resilient(chunk))
        return out

    def embed_query(self, text: str) -> list[float]:
        vectors = self.embed_documents([text])
        return vectors[0] if vectors else []


class DashScopeTextReranker:
    def __init__(
        self,
        *,
        api_key: str,
        model: str = "qwen3-vl-rerank",
        api_base: str | None = None,
        timeout_s: float = 20.0,
    ):
        self.api_key = str(api_key or "").strip()
        self.model = model
        self.api_base = _dashscope_root_url(api_base)
        self.timeout_s = float(timeout_s)
        self.endpoint = f"{self.api_base}/api/v1/services/rerank/text-rerank/text-rerank"

    def rerank(
        self,
        *,
        query: str,
        documents: list[str],
        top_n: int | None = None,
    ) -> list[tuple[int, float]]:
        if not documents:
            return []
        payload = {
            "model": self.model,
            "input": {
                "query": query,
                "documents": [{"text": str(doc or "")} for doc in documents],
            },
            "parameters": {"return_documents": False},
        }
        if top_n is not None:
            payload["parameters"]["top_n"] = max(1, int(top_n))

        response = _dashscope_post_with_api_key_pool(
            endpoint=self.endpoint,
            api_key_value=self.api_key,
            payload=payload,
            timeout_s=self.timeout_s,
            label="DashScope rerank",
        )
        scored = _extract_rerank_scores(response.json())
        return sorted(scored, key=lambda pair: pair[1], reverse=True)


@dataclass(slots=True)
class ContextRecordV2:
    record_id: str
    context_type: ContextType
    uri: str
    parent_uri: str | None = None
    l0: str = ""
    l1: str = ""
    l2_ref: str | None = None
    tags: list[str] = field(default_factory=list)
    source: str = "turn"
    confidence: float = 1.0
    session_key: str = ""
    channel: str = ""
    chat_id: str = ""
    created_at: str = field(default_factory=_now_iso)
    updated_at: str = field(default_factory=_now_iso)


@dataclass(slots=True)
class _SharedDenseBackend:
    store: Any
    refs: int = 0
    owner_lock: Any = None


class G3kuHybridStore(BaseStore):
    _dense_backend_lock: ClassVar[threading.RLock] = threading.RLock()
    _dense_backend_registry: ClassVar[dict[tuple[str, str, str], _SharedDenseBackend]] = {}

    def __init__(
        self,
        *,
        sqlite_path: Path,
        qdrant_path: Path,
        qdrant_collection: str,
        embedding_model: str,
        embedding_provider_id: str = "",
        embedding_protocol_adapter: str = "",
        embedding_batch_size: int = 32,
        dashscope_api_key: str = "",
        dashscope_api_base: str | None = None,
        dense_top_k: int = 24,
        sparse_top_k: int = 24,
    ):
        self.sqlite_path = sqlite_path
        self.qdrant_path = qdrant_path
        self.qdrant_collection = qdrant_collection
        self.embedding_model = embedding_model
        self.embedding_provider_id = str(embedding_provider_id or "").strip()
        self.embedding_protocol_adapter = str(embedding_protocol_adapter or "").strip()
        self.embedding_batch_size = max(1, int(embedding_batch_size or 1))
        self.dashscope_api_key = str(dashscope_api_key or "").strip()
        self.dashscope_api_base = dashscope_api_base
        self.dense_top_k = dense_top_k
        self.sparse_top_k = sparse_top_k

        ensure_dir(self.sqlite_path.parent)
        ensure_dir(self.qdrant_path)
        self._conn = sqlite3.connect(self.sqlite_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._init_schema()

        self._qdrant = None
        self._dense_enabled = False
        self._shared_dense_key: tuple[str, str, str] | None = None
        self._init_dense_backend()

    def _init_schema(self) -> None:
        with self._conn:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS memory_items (
                    namespace TEXT NOT NULL,
                    key TEXT NOT NULL,
                    value_json TEXT NOT NULL,
                    text_content TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (namespace, key)
                )
                """
            )
            self._conn.execute(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts
                USING fts5(namespace, key, text_content)
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS context_items_v2 (
                    namespace TEXT NOT NULL,
                    record_id TEXT NOT NULL,
                    context_type TEXT NOT NULL,
                    uri TEXT NOT NULL,
                    parent_uri TEXT,
                    l0 TEXT NOT NULL,
                    l1 TEXT NOT NULL,
                    l2_ref TEXT,
                    tags_json TEXT NOT NULL,
                    source TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    session_key TEXT NOT NULL,
                    channel TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (namespace, record_id)
                )
                """
            )
            self._conn.execute(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS context_fts_v2
                USING fts5(namespace, record_id, context_type, l0, l1, tags)
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS context_relations_v2 (
                    relation_id TEXT PRIMARY KEY,
                    from_uri TEXT NOT NULL,
                    to_uri TEXT NOT NULL,
                    relation_type TEXT NOT NULL,
                    source TEXT NOT NULL,
                    weight REAL NOT NULL DEFAULT 1.0,
                    created_at TEXT NOT NULL
                )
                """
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_context_items_v2_ns_type ON context_items_v2(namespace, context_type)"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_context_items_v2_updated ON context_items_v2(updated_at DESC)"
            )

    def _init_dense_backend(self) -> None:
        if _task_runtime_role() == "worker":
            logger.info("Hybrid store dense backend disabled for worker runtime; using sparse-only")
            self._dense_enabled = False
            self._qdrant = None
            self._shared_dense_key = None
            return

        dense_owner_lock = None
        try:
            from langchain_qdrant import QdrantVectorStore
            from qdrant_client import QdrantClient, models as qdrant_models

            dense_key = (
                str(self.qdrant_path.expanduser().resolve()).lower(),
                str(self.qdrant_collection or "").strip(),
                str(self.embedding_model or "").strip(),
            )
            with self._dense_backend_lock:
                shared = self._dense_backend_registry.get(dense_key)
                if shared is not None:
                    shared.refs += 1
                    self._shared_dense_key = dense_key
                    self._qdrant = shared.store
                    self._dense_enabled = True
                    return

                dense_lock_path = _dense_owner_lock_path(self.qdrant_path)
                dense_owner_lock = _try_acquire_file_lock(
                    dense_lock_path,
                    metadata={
                        "pid": os.getpid(),
                        "qdrant_path": str(self.qdrant_path),
                        "collection": self.qdrant_collection,
                    },
                )
                if dense_owner_lock is None:
                    try:
                        with dense_lock_path.open("r", encoding="utf-8") as handle:
                            holder_payload = json.loads(handle.read().strip() or "{}")
                            holder_pid = holder_payload.get("pid", "unknown")
                    except Exception:
                        holder_pid = "unknown"
                    logger.info(
                        "Hybrid store dense backend busy at {}; owned by pid={}, using sparse-only",
                        self.qdrant_path,
                        holder_pid,
                    )
                    self._dense_enabled = False
                    self._qdrant = None
                    self._shared_dense_key = None
                    return

                if _uses_dashscope_embedding_adapter(
                    provider_id=self.embedding_provider_id,
                    protocol_adapter=self.embedding_protocol_adapter,
                    model=self.embedding_model,
                ):
                    _, model_id = _split_provider_model(self.embedding_model, default_provider="dashscope")
                    api_key = self.dashscope_api_key or os.environ.get("DASHSCOPE_API_KEY", "").strip()
                    if not api_key:
                        raise RuntimeError(
                            "DashScope API key is not configured for the selected embedding model "
                            "(set providers.dashscope.apiKey or DASHSCOPE_API_KEY)"
                        )
                    self._embeddings = DashScopeMultimodalEmbeddings(
                        api_key=api_key,
                        model=model_id,
                        api_base=self.dashscope_api_base,
                        batch_size=self.embedding_batch_size,
                    )
                else:
                    from langchain.embeddings import init_embeddings

                    self._embeddings = init_embeddings(self.embedding_model)

                qdrant_client = QdrantClient(path=str(self.qdrant_path))
                collection_exists = False
                try:
                    collection_exists = bool(qdrant_client.collection_exists(self.qdrant_collection))
                except Exception:
                    collection_exists = False

                if not collection_exists:
                    bootstrap_vectors = self._embeddings.embed_documents(["g3ku memory bootstrap"])
                    vector_size = len(bootstrap_vectors[0]) if bootstrap_vectors and bootstrap_vectors[0] else 0
                    if vector_size <= 0:
                        raise RuntimeError("Failed to infer embedding vector size for Qdrant collection bootstrap")
                    qdrant_client.create_collection(
                        collection_name=self.qdrant_collection,
                        vectors_config=qdrant_models.VectorParams(
                            size=vector_size,
                            distance=qdrant_models.Distance.COSINE,
                        ),
                    )

                qdrant_store = QdrantVectorStore(
                    client=qdrant_client,
                    collection_name=self.qdrant_collection,
                    embedding=self._embeddings,
                    distance=qdrant_models.Distance.COSINE,
                    validate_collection_config=collection_exists,
                )
                if not collection_exists:
                    qdrant_store.add_texts(
                        texts=["g3ku memory bootstrap"],
                        metadatas=[{"namespace": "__bootstrap__", "key": "__bootstrap__"}],
                        ids=[_vector_point_id("__bootstrap__", "__bootstrap__")],
                    )

                self._qdrant = qdrant_store
                self._dense_enabled = True
                self._shared_dense_key = dense_key
                self._dense_backend_registry[dense_key] = _SharedDenseBackend(
                    store=qdrant_store,
                    refs=1,
                    owner_lock=dense_owner_lock,
                )
        except Exception as exc:
            if dense_owner_lock is not None:
                _release_file_lock(dense_owner_lock)
            if "already accessed by another instance of Qdrant client" in str(exc):
                logger.info(
                    "Hybrid store dense backend busy at {}; local Qdrant is in use, using sparse-only",
                    self.qdrant_path,
                )
            else:
                logger.warning("Hybrid store dense backend unavailable; fallback to sparse-only: {}", exc)
            self._dense_enabled = False
            self._qdrant = None
            self._shared_dense_key = None

    @staticmethod
    def _close_qdrant_store(store: Any) -> None:
        if store is None:
            return
        try:
            close_fn = getattr(store, "close", None)
            if callable(close_fn):
                close_fn()
            for attr_name in ("_client", "client", "_async_client", "async_client"):
                client = getattr(store, attr_name, None)
                if client is not None and hasattr(client, "close"):
                    try:
                        client.close()
                    except Exception:
                        pass
        except Exception:
            pass

    @classmethod
    def purge_process_local_dense_backends(
        cls,
        *,
        qdrant_path: Path,
        qdrant_collection: str,
    ) -> int:
        normalized_path = str(Path(qdrant_path).expanduser().resolve()).lower()
        normalized_collection = str(qdrant_collection or "").strip()
        stale_entries: list[tuple[Any, Any]] = []
        seen_store_ids: set[int] = set()

        with cls._dense_backend_lock:
            for dense_key, shared in list(cls._dense_backend_registry.items()):
                key_path, key_collection, _embedding_model = dense_key
                if key_path != normalized_path:
                    continue
                if normalized_collection and key_collection != normalized_collection:
                    continue
                cls._dense_backend_registry.pop(dense_key, None)
                store = getattr(shared, "store", None)
                owner_lock = getattr(shared, "owner_lock", None)
                store_id = id(store)
                if store is not None and store_id in seen_store_ids:
                    continue
                if store is not None:
                    seen_store_ids.add(store_id)
                stale_entries.append((store, owner_lock))

        for store, owner_lock in stale_entries:
            cls._close_qdrant_store(store)
            _release_file_lock(owner_lock)
        return len(stale_entries)

    def reset_dense_index(self) -> dict[str, Any]:
        self.purge_process_local_dense_backends(
            qdrant_path=self.qdrant_path,
            qdrant_collection=self.qdrant_collection,
        )
        self._qdrant = None
        self._shared_dense_key = None
        self._dense_enabled = False
        try:
            shutil.rmtree(self.qdrant_path, ignore_errors=True)
        except Exception:
            pass
        try:
            dense_lock_path = _dense_owner_lock_path(self.qdrant_path)
            if dense_lock_path.exists():
                dense_lock_path.unlink()
        except Exception:
            pass
        ensure_dir(self.qdrant_path)
        self._init_dense_backend()
        return {
            "qdrant_path": str(self.qdrant_path),
            "qdrant_collection": str(self.qdrant_collection or ""),
            "embedding_model": str(self.embedding_model or ""),
            "dense_enabled": bool(self._dense_enabled and self._qdrant is not None),
        }

    def close(self) -> None:
        qdrant_store = self._qdrant
        dense_key = self._shared_dense_key
        dense_owner_lock = None
        self._qdrant = None
        self._shared_dense_key = None
        self._dense_enabled = False

        if qdrant_store is not None:
            should_close_dense = True
            with self._dense_backend_lock:
                shared = self._dense_backend_registry.get(dense_key) if dense_key is not None else None
                if shared is not None and shared.store is qdrant_store:
                    shared.refs -= 1
                    if shared.refs > 0:
                        should_close_dense = False
                    else:
                        dense_owner_lock = shared.owner_lock
                        self._dense_backend_registry.pop(dense_key, None)
            if should_close_dense:
                self._close_qdrant_store(qdrant_store)
                _release_file_lock(dense_owner_lock)
        try:
            self._conn.close()
        except Exception:
            pass

    def _fetch_one(self, namespace: tuple[str, ...], key: str) -> Item | None:
        ns_raw = _encode_ns(namespace)
        row = self._conn.execute(
            "SELECT * FROM memory_items WHERE namespace=? AND key=?",
            (ns_raw, key),
        ).fetchone()
        if row is None:
            return None
        value = json.loads(row["value_json"])
        return Item(
            value=value,
            key=row["key"],
            namespace=_decode_ns(row["namespace"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    def _delete_item(self, namespace: tuple[str, ...], key: str) -> None:
        ns_raw = _encode_ns(namespace)
        self._conn.execute("DELETE FROM memory_items WHERE namespace=? AND key=?", (ns_raw, key))
        self._conn.execute("DELETE FROM memory_fts WHERE namespace=? AND key=?", (ns_raw, key))
        if self._dense_enabled and self._qdrant is not None:
            try:
                self._qdrant.delete(ids=[_vector_point_id(ns_raw, key)])
            except Exception:
                logger.debug("Dense delete ignored for {}::{}", ns_raw, key)

    def _upsert_item(self, op: PutOp) -> None:
        if op.value is None:
            self._delete_item(op.namespace, op.key)
            return

        ns_raw = _encode_ns(op.namespace)
        now = _now_iso()
        existing = self._conn.execute(
            "SELECT created_at FROM memory_items WHERE namespace=? AND key=?",
            (ns_raw, op.key),
        ).fetchone()
        created = existing["created_at"] if existing else now
        value_json = json.dumps(op.value, ensure_ascii=False)
        text_content = str(
            op.value.get("text", "") or op.value.get("content", "") or json.dumps(op.value, ensure_ascii=False)
        )

        self._conn.execute(
            """
            INSERT INTO memory_items(namespace, key, value_json, text_content, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(namespace, key)
            DO UPDATE SET value_json=excluded.value_json, text_content=excluded.text_content, updated_at=excluded.updated_at
            """,
            (ns_raw, op.key, value_json, text_content, created, now),
        )
        self._conn.execute("DELETE FROM memory_fts WHERE namespace=? AND key=?", (ns_raw, op.key))
        self._conn.execute(
            "INSERT INTO memory_fts(namespace, key, text_content) VALUES (?, ?, ?)",
            (ns_raw, op.key, text_content),
        )

        if self._dense_enabled and self._qdrant is not None and op.index is not False:
            try:
                self._qdrant.add_texts(
                    texts=[text_content],
                    metadatas=[{"namespace": ns_raw, "key": op.key}],
                    ids=[_vector_point_id(ns_raw, op.key)],
                )
            except Exception:
                logger.debug("Dense upsert ignored for {}::{}", ns_raw, op.key)

    @staticmethod
    def _row_to_context_record_v2(row: sqlite3.Row) -> ContextRecordV2:
        tags_raw = str(row["tags_json"] or "[]")
        try:
            tags = json.loads(tags_raw)
        except Exception:
            tags = []
        if not isinstance(tags, list):
            tags = []
        return ContextRecordV2(
            record_id=str(row["record_id"]),
            context_type=str(row["context_type"]),
            uri=str(row["uri"]),
            parent_uri=row["parent_uri"],
            l0=str(row["l0"] or ""),
            l1=str(row["l1"] or ""),
            l2_ref=row["l2_ref"],
            tags=[str(t) for t in tags],
            source=str(row["source"] or "turn"),
            confidence=float(row["confidence"] or 0.0),
            session_key=str(row["session_key"] or ""),
            channel=str(row["channel"] or ""),
            chat_id=str(row["chat_id"] or ""),
            created_at=str(row["created_at"] or _now_iso()),
            updated_at=str(row["updated_at"] or _now_iso()),
        )

    def _fetch_context_v2(self, namespace: tuple[str, ...], record_id: str) -> ContextRecordV2 | None:
        ns_raw = _encode_ns(namespace)
        row = self._conn.execute(
            "SELECT * FROM context_items_v2 WHERE namespace=? AND record_id=?",
            (ns_raw, record_id),
        ).fetchone()
        if row is None:
            return None
        return self._row_to_context_record_v2(row)

    def put_context_v2(self, namespace: tuple[str, ...], record: ContextRecordV2) -> None:
        ns_raw = _encode_ns(namespace)
        now = _now_iso()
        existing = self._conn.execute(
            "SELECT created_at FROM context_items_v2 WHERE namespace=? AND record_id=?",
            (ns_raw, record.record_id),
        ).fetchone()
        created = existing["created_at"] if existing else (record.created_at or now)
        updated = record.updated_at or now
        tags_json = json.dumps(record.tags, ensure_ascii=False)
        tags_text = " ".join(str(t) for t in record.tags)

        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO context_items_v2(
                    namespace, record_id, context_type, uri, parent_uri, l0, l1, l2_ref,
                    tags_json, source, confidence, session_key, channel, chat_id, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(namespace, record_id) DO UPDATE SET
                    context_type=excluded.context_type,
                    uri=excluded.uri,
                    parent_uri=excluded.parent_uri,
                    l0=excluded.l0,
                    l1=excluded.l1,
                    l2_ref=excluded.l2_ref,
                    tags_json=excluded.tags_json,
                    source=excluded.source,
                    confidence=excluded.confidence,
                    session_key=excluded.session_key,
                    channel=excluded.channel,
                    chat_id=excluded.chat_id,
                    updated_at=excluded.updated_at
                """,
                (
                    ns_raw,
                    record.record_id,
                    record.context_type,
                    record.uri,
                    record.parent_uri,
                    record.l0,
                    record.l1,
                    record.l2_ref,
                    tags_json,
                    record.source,
                    float(record.confidence),
                    record.session_key,
                    record.channel,
                    record.chat_id,
                    created,
                    updated,
                ),
            )
            self._conn.execute(
                "DELETE FROM context_fts_v2 WHERE namespace=? AND record_id=?",
                (ns_raw, record.record_id),
            )
            self._conn.execute(
                """
                INSERT INTO context_fts_v2(namespace, record_id, context_type, l0, l1, tags)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (ns_raw, record.record_id, record.context_type, record.l0, record.l1, tags_text),
            )

        if self._dense_enabled and self._qdrant is not None:
            try:
                dense_text = (record.l1 or record.l0 or "").strip()
                if dense_text:
                    dense_id = _vector_point_id(f"v2::{ns_raw}", record.record_id)
                    self._qdrant.add_texts(
                        texts=[dense_text],
                        metadatas=[
                            {
                                "version": "v2",
                                "namespace": ns_raw,
                                "key": record.record_id,
                                "context_type": record.context_type,
                                "uri": record.uri,
                                "layer": "l1",
                            }
                        ],
                        ids=[dense_id],
                    )
            except Exception:
                logger.debug("Dense upsert ignored for v2 {}::{}", ns_raw, record.record_id)

    def delete_context_v2(self, namespace: tuple[str, ...], record_id: str) -> None:
        ns_raw = _encode_ns(namespace)
        with self._lock, self._conn:
            self._conn.execute(
                "DELETE FROM context_items_v2 WHERE namespace=? AND record_id=?",
                (ns_raw, record_id),
            )
            self._conn.execute(
                "DELETE FROM context_fts_v2 WHERE namespace=? AND record_id=?",
                (ns_raw, record_id),
            )
        if self._dense_enabled and self._qdrant is not None:
            try:
                self._qdrant.delete(ids=[_vector_point_id(f"v2::{ns_raw}", record_id)])
            except Exception:
                logger.debug("Dense delete ignored for v2 {}::{}", ns_raw, record_id)

    def _search_context_v2_sparse(
        self,
        *,
        namespace_prefix: tuple[str, ...] | None,
        query: str,
        context_type: ContextType | None,
        limit: int,
        offset: int,
    ) -> list[tuple[ContextRecordV2, float]]:
        params: list[Any] = []
        sql = (
            "SELECT ci.* FROM context_items_v2 ci "
            "JOIN context_fts_v2 fts ON ci.namespace=fts.namespace AND ci.record_id=fts.record_id "
        )
        where: list[str] = []
        where.append("context_fts_v2 MATCH ?")
        params.append(_safe_fts_query(query))

        if namespace_prefix:
            ns_raw = _encode_ns(namespace_prefix)
            where.append("(ci.namespace=? OR ci.namespace LIKE ?)")
            params.extend([ns_raw, f"{ns_raw}{_NS_SEP}%"])
        if context_type:
            where.append("ci.context_type=?")
            params.append(context_type)

        sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY rank LIMIT ? OFFSET ?"
        params.extend([max(limit, 1), max(offset, 0)])

        rows = self._conn.execute(sql, tuple(params)).fetchall()
        return [(self._row_to_context_record_v2(row), 1.0) for row in rows]

    def _search_context_v2_dense(
        self,
        *,
        namespace_prefix: tuple[str, ...] | None,
        query: str,
        context_type: ContextType | None,
        limit: int,
    ) -> list[tuple[ContextRecordV2, float]]:
        if not self._dense_enabled or self._qdrant is None:
            return []
        try:
            docs = self._qdrant.similarity_search_with_score(query, k=max(limit, self.dense_top_k))
        except Exception:
            return []

        out: list[tuple[ContextRecordV2, float]] = []
        for doc, score in docs:
            md = doc.metadata or {}
            if str(md.get("version", "")).strip() != "v2":
                continue
            ns_raw = str(md.get("namespace", ""))
            record_id = str(md.get("key", ""))
            if not ns_raw or not record_id:
                continue
            namespace = _decode_ns(ns_raw)
            if namespace_prefix and not _ns_prefix_match(namespace, namespace_prefix):
                continue
            record = self._fetch_context_v2(namespace, record_id)
            if record is None:
                continue
            if context_type and record.context_type != context_type:
                continue
            out.append((record, float(score)))
        return out

    def _count_dense_points(self) -> int:
        if not self._dense_enabled or self._qdrant is None:
            return 0
        client = getattr(self._qdrant, "client", None)
        if client is None or not hasattr(client, "count"):
            return 0
        result = client.count(collection_name=self.qdrant_collection, exact=True)
        return int(getattr(result, "count", 0) or 0)

    def _count_context_v2_dense_eligible(self) -> int:
        row = self._conn.execute(
            """
            SELECT COUNT(1) AS total
            FROM context_items_v2
            WHERE TRIM(COALESCE(l1, '')) <> '' OR TRIM(COALESCE(l0, '')) <> ''
            """
        ).fetchone()
        return int((row["total"] if row is not None else 0) or 0)

    def _sample_context_v2_dense_point_ids(self, *, limit: int = 8) -> list[str]:
        rows = self._conn.execute(
            """
            SELECT namespace, record_id
            FROM context_items_v2
            WHERE TRIM(COALESCE(l1, '')) <> '' OR TRIM(COALESCE(l0, '')) <> ''
            ORDER BY updated_at DESC, record_id DESC
            LIMIT ?
            """,
            (max(limit, 1),),
        ).fetchall()
        return [
            _vector_point_id(f"v2::{str(row['namespace'] or '')}", str(row["record_id"] or ""))
            for row in rows
            if str(row["namespace"] or "").strip() and str(row["record_id"] or "").strip()
        ]

    def _missing_context_v2_dense_sample(self, *, sample_limit: int = 8) -> bool:
        if not self._dense_enabled or self._qdrant is None:
            return False
        client = getattr(self._qdrant, "client", None)
        if client is None or not hasattr(client, "retrieve"):
            return False
        point_ids = self._sample_context_v2_dense_point_ids(limit=sample_limit)
        if not point_ids:
            return False
        records = client.retrieve(
            collection_name=self.qdrant_collection,
            ids=point_ids,
            with_payload=False,
            with_vectors=False,
        )
        existing_ids = {
            str(getattr(record, "id", "") or "").strip()
            for record in list(records or [])
            if str(getattr(record, "id", "") or "").strip()
        }
        return any(str(point_id) not in existing_ids for point_id in point_ids)

    def ensure_context_v2_dense_backfill(self, *, batch_size: int | None = None) -> dict[str, Any]:
        if not self._dense_enabled or self._qdrant is None:
            return {"needed": False, "eligible": 0, "indexed": 0, "dense_points": 0}

        eligible = self._count_context_v2_dense_eligible()
        if eligible <= 0:
            return {"needed": False, "eligible": 0, "indexed": 0, "dense_points": self._count_dense_points()}

        dense_points = self._count_dense_points()
        sample_missing = self._missing_context_v2_dense_sample()
        if dense_points >= eligible and not sample_missing:
            return {
                "needed": False,
                "eligible": eligible,
                "indexed": 0,
                "dense_points": dense_points,
                "sample_missing": False,
            }

        rows = self._conn.execute(
            """
            SELECT namespace, record_id, context_type, uri, l0, l1
            FROM context_items_v2
            WHERE TRIM(COALESCE(l1, '')) <> '' OR TRIM(COALESCE(l0, '')) <> ''
            ORDER BY updated_at ASC, record_id ASC
            """
        ).fetchall()

        resolved_batch_size = max(1, int(batch_size or self.embedding_batch_size or 1))
        texts: list[str] = []
        metadatas: list[dict[str, Any]] = []
        ids: list[str] = []
        indexed = 0

        def _flush() -> None:
            nonlocal indexed, texts, metadatas, ids
            if not texts:
                return
            self._qdrant.add_texts(texts=texts, metadatas=metadatas, ids=ids)
            indexed += len(texts)
            texts = []
            metadatas = []
            ids = []

        for row in rows:
            namespace_raw = str(row["namespace"] or "").strip()
            record_id = str(row["record_id"] or "").strip()
            dense_text = str(row["l1"] or row["l0"] or "").strip()
            if not namespace_raw or not record_id or not dense_text:
                continue
            texts.append(dense_text)
            metadatas.append(
                {
                    "version": "v2",
                    "namespace": namespace_raw,
                    "key": record_id,
                    "context_type": str(row["context_type"] or "").strip(),
                    "uri": str(row["uri"] or "").strip(),
                    "layer": "l1",
                }
            )
            ids.append(_vector_point_id(f"v2::{namespace_raw}", record_id))
            if len(texts) >= resolved_batch_size:
                _flush()

        _flush()
        dense_points_after = self._count_dense_points()
        sample_missing_after = self._missing_context_v2_dense_sample()
        return {
            "needed": True,
            "eligible": eligible,
            "indexed": indexed,
            "dense_points": dense_points_after,
            "sample_missing": sample_missing_after,
        }

    def search_context_v2(
        self,
        namespace_prefix: tuple[str, ...] | None,
        *,
        query: str | None,
        limit: int = 8,
        offset: int = 0,
        context_type: ContextType | None = None,
    ) -> list[tuple[ContextRecordV2, float]]:
        with self._lock:
            if not query:
                params: list[Any] = []
                sql = "SELECT * FROM context_items_v2"
                where: list[str] = []
                if namespace_prefix:
                    ns_raw = _encode_ns(namespace_prefix)
                    where.append("(namespace=? OR namespace LIKE ?)")
                    params.extend([ns_raw, f"{ns_raw}{_NS_SEP}%"])
                if context_type:
                    where.append("context_type=?")
                    params.append(context_type)
                if where:
                    sql += " WHERE " + " AND ".join(where)
                sql += " ORDER BY updated_at DESC LIMIT ? OFFSET ?"
                params.extend([max(limit, 1), max(offset, 0)])
                rows = self._conn.execute(sql, tuple(params)).fetchall()
                return [(self._row_to_context_record_v2(row), 0.0) for row in rows]

            sparse = self._search_context_v2_sparse(
                namespace_prefix=namespace_prefix,
                query=query,
                context_type=context_type,
                limit=max(limit, self.sparse_top_k),
                offset=0,
            )
            dense = self._search_context_v2_dense(
                namespace_prefix=namespace_prefix,
                query=query,
                context_type=context_type,
                limit=max(limit, self.dense_top_k),
            )
            sparse_ids = [rec.record_id for rec, _ in sparse]
            dense_ids = [rec.record_id for rec, _ in dense]
            fused_ids = _rrf_fuse(dense_ids, sparse_ids)

            item_by_id: dict[str, tuple[ContextRecordV2, float]] = {}
            for rec, score in sparse:
                item_by_id[rec.record_id] = (rec, score)
            for rec, score in dense:
                if rec.record_id not in item_by_id:
                    item_by_id[rec.record_id] = (rec, score)

            required = max(limit, 1) + max(offset, 0)
            out: list[tuple[ContextRecordV2, float]] = []
            for record_id in fused_ids:
                item = item_by_id.get(record_id)
                if item is None:
                    continue
                out.append(item)
                if len(out) >= required:
                    break
            start = max(offset, 0)
            return out[start : start + max(limit, 1)]

    def search_context_v2_dense(
        self,
        namespace_prefix: tuple[str, ...] | None,
        *,
        query: str | None,
        limit: int = 8,
        context_type: ContextType | None = None,
    ) -> list[tuple[ContextRecordV2, float]]:
        with self._lock:
            if not query:
                return []
            return self._search_context_v2_dense(
                namespace_prefix=namespace_prefix,
                query=str(query or ""),
                context_type=context_type,
                limit=max(limit, 1),
            )[: max(limit, 1)]

    def list_context_v2(
        self,
        namespace_prefix: tuple[str, ...] | None = None,
        *,
        limit: int = 1000,
        offset: int = 0,
        context_type: ContextType | None = None,
    ) -> list[ContextRecordV2]:
        rows = self.search_context_v2(
            namespace_prefix,
            query=None,
            limit=limit,
            offset=offset,
            context_type=context_type,
        )
        return [record for record, _score in rows]

    def _search_sparse(self, op: SearchOp) -> list[SearchItem]:
        params: list[Any] = []
        sql = (
            "SELECT mi.* FROM memory_items mi "
            "JOIN memory_fts fts ON mi.namespace=fts.namespace AND mi.key=fts.key "
        )
        where = []
        if op.query:
            where.append("fts.text_content MATCH ?")
            params.append(_safe_fts_query(op.query))

        ns_prefix = op.namespace_prefix
        if ns_prefix:
            ns_raw = _encode_ns(ns_prefix)
            where.append("(mi.namespace=? OR mi.namespace LIKE ?)")
            params.extend([ns_raw, f"{ns_raw}{_NS_SEP}%"])

        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY rank LIMIT ? OFFSET ?"
        params.extend([max(op.limit, 1), max(op.offset, 0)])

        rows = self._conn.execute(sql, tuple(params)).fetchall()
        out: list[SearchItem] = []
        for row in rows:
            value = json.loads(row["value_json"])
            if not _passes_filter(value, op.filter):
                continue
            out.append(
                SearchItem(
                    namespace=_decode_ns(row["namespace"]),
                    key=row["key"],
                    value=value,
                    created_at=datetime.fromisoformat(row["created_at"]),
                    updated_at=datetime.fromisoformat(row["updated_at"]),
                    score=1.0,
                )
            )
        return out

    def _search_dense(self, op: SearchOp) -> list[SearchItem]:
        if not self._dense_enabled or self._qdrant is None or not op.query:
            return []
        try:
            docs = self._qdrant.similarity_search_with_score(op.query, k=max(self.dense_top_k, op.limit))
        except Exception:
            return []

        out: list[SearchItem] = []
        for doc, score in docs:
            md = doc.metadata or {}
            ns_raw = str(md.get("namespace", ""))
            key = str(md.get("key", ""))
            if not ns_raw or not key:
                continue
            namespace = _decode_ns(ns_raw)
            if not _ns_prefix_match(namespace, op.namespace_prefix):
                continue
            item = self._fetch_one(namespace, key)
            if item is None:
                continue
            if not _passes_filter(item.value, op.filter):
                continue
            out.append(
                SearchItem(
                    namespace=item.namespace,
                    key=item.key,
                    value=item.value,
                    created_at=item.created_at,
                    updated_at=item.updated_at,
                    score=float(score),
                )
            )
        return out

    def _search_fused(self, op: SearchOp) -> list[SearchItem]:
        if not op.query:
            params: list[Any] = []
            sql = "SELECT * FROM memory_items"
            if op.namespace_prefix:
                ns_raw = _encode_ns(op.namespace_prefix)
                sql += " WHERE (namespace=? OR namespace LIKE ?)"
                params.extend([ns_raw, f"{ns_raw}{_NS_SEP}%"])
            sql += " ORDER BY updated_at DESC LIMIT ? OFFSET ?"
            params.extend([max(op.limit, 1), max(op.offset, 0)])
            rows = self._conn.execute(sql, tuple(params)).fetchall()
            out: list[SearchItem] = []
            for row in rows:
                value = json.loads(row["value_json"])
                if not _passes_filter(value, op.filter):
                    continue
                out.append(
                    SearchItem(
                        namespace=_decode_ns(row["namespace"]),
                        key=row["key"],
                        value=value,
                        created_at=datetime.fromisoformat(row["created_at"]),
                        updated_at=datetime.fromisoformat(row["updated_at"]),
                        score=None,
                    )
                )
            return out

        sparse = self._search_sparse(op)
        dense = self._search_dense(op)
        sparse_ids = [f"{_encode_ns(i.namespace)}::{i.key}" for i in sparse]
        dense_ids = [f"{_encode_ns(i.namespace)}::{i.key}" for i in dense]
        fused_ids = _rrf_fuse(dense_ids, sparse_ids)

        item_by_id: dict[str, SearchItem] = {}
        for item in sparse + dense:
            item_by_id[f"{_encode_ns(item.namespace)}::{item.key}"] = item

        out: list[SearchItem] = []
        required = max(op.limit, 1) + max(op.offset, 0)
        for fid in fused_ids:
            item = item_by_id.get(fid)
            if item is None:
                continue
            out.append(item)
            if len(out) >= required:
                break
        start = max(op.offset, 0)
        return out[start : start + max(op.limit, 1)]

    def _list_namespaces(self, op: ListNamespacesOp) -> list[tuple[str, ...]]:
        rows = self._conn.execute("SELECT DISTINCT namespace FROM memory_items").fetchall()
        all_namespaces = [_decode_ns(r["namespace"]) for r in rows]

        def _match(ns: tuple[str, ...], cond: MatchCondition) -> bool:
            path = tuple(cond.path)
            if cond.match_type == "prefix":
                return _ns_prefix_match(ns, path)
            if cond.match_type == "suffix":
                if len(ns) < len(path):
                    return False
                return ns[-len(path) :] == path
            return True

        matched: list[tuple[str, ...]] = []
        for ns in all_namespaces:
            keep = True
            for cond in op.match_conditions or ():
                if not _match(ns, cond):
                    keep = False
                    break
            if not keep:
                continue
            if op.max_depth is not None:
                ns = ns[: op.max_depth]
            matched.append(ns)

        dedup = sorted(set(matched))
        return dedup[op.offset : op.offset + op.limit]

    def batch(self, ops: Iterable[Op]) -> list[Result]:
        results: list[Result] = []
        with self._lock, self._conn:
            for op in ops:
                if isinstance(op, GetOp):
                    results.append(self._fetch_one(op.namespace, op.key))
                    continue
                if isinstance(op, SearchOp):
                    results.append(self._search_fused(op))
                    continue
                if isinstance(op, PutOp):
                    self._upsert_item(op)
                    results.append(None)
                    continue
                if isinstance(op, ListNamespacesOp):
                    results.append(self._list_namespaces(op))
                    continue
                raise TypeError(f"Unsupported store op: {type(op).__name__}")
        return results

    async def abatch(self, ops: Iterable[Op]) -> list[Result]:
        return await asyncio.to_thread(self.batch, list(ops))


class CatalogStoreManager:
    def __init__(self, workspace: Path, config: Any):
        self.workspace = Path(workspace).expanduser().resolve()
        self.config = config
        self._resolved_embedding_model = ""
        self._resolved_embedding_provider_id = ""
        self._resolved_embedding_protocol_adapter = ""
        self._dashscope_api_key, self._dashscope_api_base = _load_workspace_dashscope_settings(self.workspace)

        try:
            embedding_target = resolve_memory_embedding_target(workspace=self.workspace)
            provider_id = _normalize_provider_id(embedding_target.provider_id)
            if provider_id in {"dashscope_embedding", "dashscope_rerank"}:
                provider_id = "dashscope"
            self._resolved_embedding_provider_id = str(provider_id or "")
            self._resolved_embedding_protocol_adapter = _protocol_adapter_value(embedding_target.protocol_adapter)
            self._resolved_embedding_model = (
                f"{provider_id}:{embedding_target.resolved_model}" if provider_id else embedding_target.resolved_model
            )
            self._dashscope_api_key = str(
                embedding_target.secret_payload.get("api_key", "") or self._dashscope_api_key
            ).strip()
            self._dashscope_api_base = str(embedding_target.base_url or self._dashscope_api_base or "").strip() or None
        except Exception as exc:
            logger.warning("Memory embedding target resolution failed: {}", exc)

        sqlite_path = resolve_path_in_workspace(config.store.sqlite_path, self.workspace)
        qdrant_path = resolve_path_in_workspace(config.store.qdrant_path, self.workspace)
        self.store = G3kuHybridStore(
            sqlite_path=sqlite_path,
            qdrant_path=qdrant_path,
            qdrant_collection=config.store.qdrant_collection,
            embedding_model=self._resolved_embedding_model,
            embedding_provider_id=self._resolved_embedding_provider_id,
            embedding_protocol_adapter=self._resolved_embedding_protocol_adapter,
            embedding_batch_size=config.embedding.batch_size,
            dashscope_api_key=self._dashscope_api_key,
            dashscope_api_base=self._dashscope_api_base,
            dense_top_k=config.retrieval.dense_top_k,
            sparse_top_k=config.retrieval.sparse_top_k,
        )

    @staticmethod
    def _stable_text_hash(text: str) -> str:
        return hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()

    async def list_context_records(
        self,
        *,
        namespace_prefix: tuple[str, ...] | None = None,
        limit: int = 1000,
        offset: int = 0,
    ) -> list[ContextRecordV2]:
        return await asyncio.to_thread(
            self.store.list_context_v2,
            namespace_prefix,
            limit=max(limit, 1),
            offset=max(offset, 0),
        )

    async def put_context_record(self, *, namespace: tuple[str, ...], record: ContextRecordV2) -> None:
        await asyncio.to_thread(self.store.put_context_v2, namespace, record)

    async def delete_context_record(self, *, namespace: tuple[str, ...], record_id: str) -> None:
        await asyncio.to_thread(self.store.delete_context_v2, namespace, record_id)

    async def semantic_search_context_records(
        self,
        *,
        namespace_prefix: tuple[str, ...] | None = None,
        query: str,
        limit: int = 8,
        context_type: ContextType | None = None,
    ) -> list[ContextRecordV2]:
        rows = await asyncio.to_thread(
            self.store.search_context_v2_dense,
            namespace_prefix,
            query=str(query or ""),
            limit=max(limit, 1),
            context_type=context_type,
        )
        return [record for record, _score in rows]

    async def sync_catalog(
        self,
        service: Any,
        *,
        skill_ids: set[str] | None = None,
        tool_ids: set[str] | None = None,
    ) -> dict[str, int]:
        from g3ku.runtime.context.catalog import ContextCatalogIndexer

        indexer = ContextCatalogIndexer(memory_manager=self, service=service)
        return await indexer.sync(skill_ids=skill_ids, tool_ids=tool_ids)

    async def ensure_catalog_bootstrap(self, service: Any) -> dict[str, Any]:
        from g3ku.runtime.context.catalog import ContextCatalogIndexer

        existing = await self.list_context_records(namespace_prefix=ContextCatalogIndexer.NAMESPACE, limit=1)
        if existing:
            return {"ok": True, "synced": False, "reason": "already_present"}
        result = await self.sync_catalog(service)
        return {"ok": True, "synced": True, "reason": "bootstrap", **dict(result or {})}

    def close(self) -> None:
        self.store.close()


__all__ = [
    "CatalogStoreManager",
    "ContextRecordV2",
    "DashScopeMultimodalEmbeddings",
    "DashScopeTextReranker",
    "G3kuHybridStore",
    "_load_workspace_dashscope_settings",
    "_release_file_lock",
    "_try_acquire_file_lock",
]
