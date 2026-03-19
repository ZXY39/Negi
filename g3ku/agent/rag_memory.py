"""RAG memory manager and LangGraph BaseStore adapter."""

from __future__ import annotations

import asyncio
import json
import os
import re
import sqlite3
import threading
import uuid
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, ClassVar, Iterable, Literal

import requests
from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings
from langchain_core.retrievers import BaseRetriever
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

from g3ku.llm_config.runtime_resolver import (
    resolve_memory_embedding_target,
    resolve_memory_rerank_target,
)
from g3ku.utils.helpers import ensure_dir, resolve_path_in_workspace

try:
    from g3ku.agent.memory import MemoryStore
except Exception:  # pragma: no cover - optional runtime dependency fallback
    class MemoryStore:  # type: ignore[no-redef]
        """Minimal fallback used when legacy memory dependencies are unavailable."""

        def __init__(self, workspace: Path):
            self.workspace = workspace

        def append_history(self, _entry: str) -> None:
            return None

        def read_long_term(self) -> str:
            return ""

        def write_long_term(self, _content: str) -> None:
            return None

_NS_SEP = "\x1f"
CONTEXT_TYPE_ALL: tuple[str, ...] = ("memory", "resource", "skill")
CONTEXT_LAYER_ALL: tuple[str, ...] = ("l0", "l1", "l2")
_SENTENCE_SPLIT_RE = re.compile(
    r"[.!?\u3002\uFF01\uFF1F]+(?=\s|$|[\u3400-\u9FFF\u3040-\u30FF\uAC00-\uD7AF])"
)

ContextType = Literal["memory", "resource", "skill"]
ContextLayer = Literal["l0", "l1", "l2"]


def _now_iso() -> str:
    return datetime.now().isoformat()


def _encode_ns(namespace: tuple[str, ...]) -> str:
    return _NS_SEP.join(namespace)


def _decode_ns(raw: str) -> tuple[str, ...]:
    if not raw:
        return ()
    return tuple(part for part in raw.split(_NS_SEP) if part)


def _read_lock_metadata(handle: Any) -> dict[str, object]:
    try:
        handle.seek(0)
        raw = handle.read().strip()
    except Exception:
        return {}
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


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


def _is_dashscope_vl_embedding_model(model: str) -> bool:
    provider, model_id = _split_provider_model(model, default_provider="dashscope")
    return model_id == "qwen3-vl-embedding" and provider in {None, "dashscope"}


def _is_dashscope_rerank_model(model: str) -> bool:
    provider, model_id = _split_provider_model(model, default_provider="dashscope")
    return model_id == "qwen3-vl-rerank" and provider in {None, "dashscope"}


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

        try:
            vectors[target_idx] = [float(v) for v in vector]
        except Exception as exc:  # pragma: no cover - defensive casting
            raise RuntimeError(f"Invalid embedding vector payload at index {target_idx}") from exc

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

    providers = raw_data.get("providers", {})
    if not isinstance(providers, dict):
        return env_key, env_base

    dashscope = providers.get("dashscope", {})
    if not isinstance(dashscope, dict):
        return env_key, env_base

    cfg_key = str(dashscope.get("apiKey", dashscope.get("api_key", "")) or "").strip()
    cfg_base = str(dashscope.get("apiBase", dashscope.get("api_base", "")) or "").strip() or None
    return cfg_key or env_key, cfg_base or env_base


class DashScopeMultimodalEmbeddings(Embeddings):
    """DashScope multimodal embedding adapter for qwen3-vl-embedding."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str = "qwen3-vl-embedding",
        api_base: str | None = None,
        batch_size: int = 32,
        timeout_s: float = 30.0,
    ):
        self.api_key = api_key.strip()
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
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        response = requests.post(self.endpoint, headers=headers, json=payload, timeout=self.timeout_s)
        try:
            response.raise_for_status()
        except Exception as exc:
            raise RuntimeError(
                f"DashScope embedding API call failed ({response.status_code}) at {self.endpoint}"
            ) from exc
        data = response.json()
        return _extract_embedding_vectors(data, expected_count=len(texts))

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        normalized = [str(t or "") for t in texts]
        out: list[list[float]] = []
        for start in range(0, len(normalized), self.batch_size):
            chunk = normalized[start : start + self.batch_size]
            out.extend(self._embed_batch(chunk))
        return out

    def embed_query(self, text: str) -> list[float]:
        vectors = self.embed_documents([text])
        return vectors[0] if vectors else []


class DashScopeTextReranker:
    """DashScope rerank adapter for qwen3-vl-rerank."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str = "qwen3-vl-rerank",
        api_base: str | None = None,
        timeout_s: float = 20.0,
    ):
        self.api_key = api_key.strip()
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
            "parameters": {
                "return_documents": False,
            },
        }
        if top_n is not None:
            payload["parameters"]["top_n"] = max(1, int(top_n))

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        response = requests.post(self.endpoint, headers=headers, json=payload, timeout=self.timeout_s)
        try:
            response.raise_for_status()
        except Exception as exc:
            raise RuntimeError(
                f"DashScope rerank API call failed ({response.status_code}) at {self.endpoint}"
            ) from exc

        data = response.json()
        scored = _extract_rerank_scores(data)
        return sorted(scored, key=lambda pair: pair[1], reverse=True)


@dataclass(slots=True)
class MemoryRecord:
    """Persistent memory record stored in hybrid index."""

    record_id: str
    text: str
    source: str = "turn"
    confidence: float = 1.0
    pii_level: Literal["none", "low", "high"] = "none"
    created_at: str = field(default_factory=_now_iso)
    updated_at: str = field(default_factory=_now_iso)
    tags: list[str] = field(default_factory=list)
    pinned: bool = False
    session_key: str = ""
    channel: str = ""
    chat_id: str = ""


@dataclass(slots=True)
class MemoryHit:
    """Memory hit returned by retrieval."""

    record_id: str
    score_dense: float = 0.0
    score_sparse: float = 0.0
    score_fused: float = 0.0
    snippet: str = ""
    provenance: str = ""


@dataclass(slots=True)
class AuditEvent:
    """Append-only memory audit event."""

    action: str
    reason: str
    actor: str
    session_key: str
    trace_id: str
    before: dict[str, Any] | None = None
    after: dict[str, Any] | None = None
    timestamp: str = field(default_factory=_now_iso)


@dataclass(slots=True)
class PendingFact:
    """Fact candidate that requires user approval."""

    candidate: str
    reason: str
    confidence: float
    status: Literal["pending", "approved", "rejected"] = "pending"
    created_at: str = field(default_factory=_now_iso)
    pending_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    session_key: str = ""
    channel: str = ""
    chat_id: str = ""


@dataclass(slots=True)
class ContextRecordV2:
    """Unified context record with layered content."""

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
class TypedQuery:
    """Planner output for type-scoped retrieval."""

    query: str
    context_type: ContextType
    intent: str = "lookup"
    priority: int = 1


@dataclass(slots=True)
class RetrievalTrace:
    """Structured retrieval trace for explainability."""

    plan: list[dict[str, Any]]
    candidates: list[dict[str, Any]]
    rerank: list[dict[str, Any]]
    injected_blocks: list[dict[str, Any]]
    token_budget_used: int
    trace_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    timestamp: str = field(default_factory=_now_iso)


@dataclass(slots=True)
class CommitArtifact:
    """Artifacts created by one session commit run."""

    archive_id: str
    summary_uri: str
    extracted_count: int
    merged_count: int
    skipped_count: int
    timestamp: str = field(default_factory=_now_iso)


@dataclass(slots=True)
class _SharedDenseBackend:
    """Process-local shared dense backend to avoid duplicate Qdrant local locks."""

    store: Any
    refs: int = 0
    owner_lock: Any = None


class G3kuHybridStore(BaseStore):
    """Hybrid store backed by SQLite (metadata+FTS) and optional Qdrant dense index."""

    _dense_backend_lock: ClassVar[threading.RLock] = threading.RLock()
    _dense_backend_registry: ClassVar[dict[tuple[str, str, str], _SharedDenseBackend]] = {}

    def __init__(
        self,
        *,
        sqlite_path: Path,
        qdrant_path: Path,
        qdrant_collection: str,
        embedding_model: str,
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
                            holder_pid = _read_lock_metadata(handle).get("pid", "unknown")
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

                if _is_dashscope_vl_embedding_model(self.embedding_model):
                    _, model_id = _split_provider_model(self.embedding_model, default_provider="dashscope")
                    api_key = self.dashscope_api_key or os.environ.get("DASHSCOPE_API_KEY", "").strip()
                    if not api_key:
                        raise RuntimeError(
                            "DashScope API key is not configured for qwen3-vl-embedding "
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

                try:
                    qdrant_store = QdrantVectorStore.from_existing_collection(
                        collection_name=self.qdrant_collection,
                        embedding=self._embeddings,
                        path=str(self.qdrant_path),
                    )
                except Exception:
                    qdrant_store = QdrantVectorStore.from_texts(
                        texts=["g3ku memory bootstrap"],
                        metadatas=[{"namespace": "__bootstrap__", "key": "__bootstrap__"}],
                        ids=[_vector_point_id("__bootstrap__", "__bootstrap__")],
                        collection_name=self.qdrant_collection,
                        embedding=self._embeddings,
                        path=str(self.qdrant_path),
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
        text_content = str(op.value.get("text", "") or op.value.get("content", "") or json.dumps(op.value, ensure_ascii=False))

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

    def add_context_relation_v2(
        self,
        *,
        from_uri: str,
        to_uri: str,
        relation_type: str,
        source: str,
        weight: float = 1.0,
    ) -> None:
        relation_id = uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"{from_uri}|{to_uri}|{relation_type}|{source}",
        ).hex
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT OR REPLACE INTO context_relations_v2(
                    relation_id, from_uri, to_uri, relation_type, source, weight, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    relation_id,
                    from_uri,
                    to_uri,
                    relation_type,
                    source,
                    float(weight),
                    _now_iso(),
                ),
            )

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
        sql += " ORDER BY rank"
        sql += " LIMIT ? OFFSET ?"
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
            # Non-query mode: list by updated_at desc.
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


class G3kuHybridRetriever(BaseRetriever):
    """Retriever backed by G3kuHybridStore and runtime-aware namespace resolution."""

    def __init__(
        self,
        *,
        store: G3kuHybridStore,
        namespace_resolver,
        top_k: int = 8,
    ):
        super().__init__()
        self._store = store
        self._namespace_resolver = namespace_resolver
        self._top_k = top_k

    def _get_relevant_documents(self, query: str, *, run_manager=None) -> list[Document]:
        namespace = tuple(self._namespace_resolver() or ())
        items = self._store.search(namespace, query=query, limit=self._top_k)
        docs: list[Document] = []
        for item in items:
            text = str(item.value.get("text", "") or item.value.get("content", ""))
            docs.append(
                Document(
                    page_content=text,
                    metadata={
                        "record_id": item.key,
                        "namespace": list(item.namespace),
                        **{k: v for k, v in item.value.items() if k != "text"},
                    },
                )
            )
        return docs


class MemoryManager:
    """High-level RAG memory service: retrieval, write guard, pending queue, legacy dual-write."""

    def __init__(self, workspace: Path, config: Any):
        self.workspace = Path(workspace).expanduser().resolve()
        self.config = config
        self.arch_version = str(getattr(config, "arch_version", "v1") or "v1").lower()
        self.features = getattr(config, "features", None)
        self._feature_defaults = {
            "unified_context": False,
            "layered_loading": False,
            "query_planner": False,
            "commit_pipeline": False,
            "split_store": False,
            "observability": False,
        }
        self._resolved_embedding_model = ""
        self._dashscope_api_key, self._dashscope_api_base = _load_workspace_dashscope_settings(
            self.workspace
        )

        try:
            embedding_target = resolve_memory_embedding_target(workspace=self.workspace)
            provider_id = _normalize_provider_id(embedding_target.provider_id)
            if provider_id in {"dashscope_embedding", "dashscope_rerank"}:
                provider_id = "dashscope"
            self._resolved_embedding_model = (
                f"{provider_id}:{embedding_target.resolved_model}" if provider_id else embedding_target.resolved_model
            )
            self._dashscope_api_key = str(
                embedding_target.secret_payload.get("api_key", "") or self._dashscope_api_key
            ).strip()
            self._dashscope_api_base = str(embedding_target.base_url or self._dashscope_api_base or "").strip() or None
        except Exception as exc:
            logger.warning("Memory embedding target resolution failed: {}", exc)
        self._reranker = self._init_reranker()

        mem_dir = ensure_dir(self.workspace / "memory")
        self.audit_file = mem_dir / "audit.jsonl"
        self.pending_file = mem_dir / "pending_facts.jsonl"
        self.trace_file = mem_dir / "retrieval_trace.jsonl"
        self.context_assembly_trace_file = mem_dir / "context_assembly.jsonl"
        self.cost_file = mem_dir / "cost_metrics.json"
        self.archive_dir = ensure_dir(mem_dir / "archives")
        self.context_store_dir = ensure_dir(mem_dir / "context_store")
        self.commit_summary_dir = ensure_dir(mem_dir / "commit_summaries")
        self._cost_metrics = self._load_cost_metrics()

        sqlite_path = resolve_path_in_workspace(config.store.sqlite_path, self.workspace)
        qdrant_path = resolve_path_in_workspace(config.store.qdrant_path, self.workspace)
        self.store = G3kuHybridStore(
            sqlite_path=sqlite_path,
            qdrant_path=qdrant_path,
            qdrant_collection=config.store.qdrant_collection,
            embedding_model=self._resolved_embedding_model,
            embedding_batch_size=config.embedding.batch_size,
            dashscope_api_key=self._dashscope_api_key,
            dashscope_api_base=self._dashscope_api_base,
            dense_top_k=config.retrieval.dense_top_k,
            sparse_top_k=config.retrieval.sparse_top_k,
        )
        self._legacy = MemoryStore(self.workspace)
        self._io_lock = asyncio.Lock()
        self._trace_lock = asyncio.Lock()

    def close(self) -> None:
        self.store.close()

    def namespace_for(self, *, channel: str | None, chat_id: str | None) -> tuple[str, ...]:
        template = list(self.config.isolation.namespace_template or ["memory", "{channel}", "{chat_id}"])
        channel_val = str(channel or "unknown")
        chat_val = str(chat_id or "unknown")
        session_val = f"{channel_val}:{chat_val}"
        out = []
        for token in template:
            out.append(
                token.replace("{channel}", channel_val)
                .replace("{chat_id}", chat_val)
                .replace("{session_key}", session_val)
            )
        return tuple(out)

    @staticmethod
    def catalog_namespace() -> tuple[str, ...]:
        return ('catalog', 'global')

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

    async def fetch_context_record(self, *, namespace: tuple[str, ...], record_id: str) -> ContextRecordV2 | None:
        return await asyncio.to_thread(self.store._fetch_context_v2, namespace, record_id)

    async def sync_catalog(
        self,
        service: Any,
        *,
        skill_ids: set[str] | None = None,
        tool_ids: set[str] | None = None,
    ) -> dict[str, int]:
        if not self._feature_enabled('unified_context') or self.arch_version != 'v2':
            return {'created': 0, 'updated': 0, 'removed': 0}
        from g3ku.runtime.context.catalog import ContextCatalogIndexer

        indexer = ContextCatalogIndexer(memory_manager=self, service=service)
        return await indexer.sync(skill_ids=skill_ids, tool_ids=tool_ids)

    async def write_context_assembly_trace(
        self,
        *,
        session_key: str | None,
        channel: str | None,
        chat_id: str | None,
        payload: dict[str, Any],
    ) -> None:
        event = {
            'timestamp': _now_iso(),
            'session_key': str(session_key or ''),
            'channel': str(channel or ''),
            'chat_id': str(chat_id or ''),
            'payload': payload,
        }
        async with self._trace_lock:
            await asyncio.to_thread(self._append_jsonl, self.context_assembly_trace_file, event)

    async def read_trace_file(self, *, trace_kind: str, limit: int = 20) -> list[dict[str, Any]]:
        if trace_kind == 'context_assembly':
            path = self.context_assembly_trace_file
        else:
            path = self.trace_file
        if not path.exists():
            return []

        def _run() -> list[dict[str, Any]]:
            try:
                lines = path.read_text(encoding='utf-8').splitlines()
            except Exception:
                return []
            items: list[dict[str, Any]] = []
            for line in lines[-max(1, int(limit)) :]:
                line = str(line or '').strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                except Exception:
                    continue
                if isinstance(payload, dict):
                    items.append(payload)
            return items

        return await asyncio.to_thread(_run)

    def _feature_enabled(self, key: str) -> bool:
        default = self._feature_defaults.get(key, False)
        if self.features is None:
            return default
        return bool(getattr(self.features, key, default))

    def _default_load_level(self) -> ContextLayer:
        level = str(getattr(self.config.retrieval, "default_load_level", "l1") or "l1").lower()
        if level not in CONTEXT_LAYER_ALL:
            return "l1"
        return level  # type: ignore[return-value]

    @staticmethod
    def _estimate_tokens(text: str) -> int:
        if not text:
            return 0
        compact = " ".join(str(text).split())
        if not compact:
            return 0
        by_chars = max(1, len(compact) // 4)
        by_words = max(1, int(len(compact.split()) * 1.3))
        return max(by_chars, by_words)

    @staticmethod
    def _stable_text_hash(text: str) -> str:
        return uuid.uuid5(uuid.NAMESPACE_URL, str(text or "").strip().lower()).hex

    def _context_uri(
        self,
        *,
        context_type: ContextType,
        channel: str,
        chat_id: str,
        record_id: str,
    ) -> str:
        safe_channel = str(channel or "unknown")
        safe_chat = str(chat_id or "unknown")
        return f"g3ku://{context_type}/{safe_channel}/{safe_chat}/{record_id}"

    def _safe_channel_chat(self, session_key: str, channel: str | None, chat_id: str | None) -> tuple[str, str]:
        ch = str(channel or "").strip()
        cid = str(chat_id or "").strip()
        if ch and cid:
            return ch, cid
        if ":" in session_key:
            c1, c2 = session_key.split(":", 1)
            return c1 or "unknown", c2 or "unknown"
        return ch or "unknown", cid or "unknown"

    def _l0_summary(self, text: str, *, limit: int = 160) -> str:
        compact = " ".join(str(text or "").split())
        if not compact:
            return ""
        if len(compact) <= limit:
            return compact
        return compact[: limit - 3] + "..."

    def _l1_summary(self, text: str, *, limit: int = 640) -> str:
        compact = " ".join(str(text or "").split())
        if not compact:
            return ""
        if len(compact) <= limit:
            return compact
        return compact[: limit - 3] + "..."

    async def _write_l2_payload(self, *, record_id: str, content: str) -> str:
        path = self.context_store_dir / f"{record_id}.txt"
        await asyncio.to_thread(path.write_text, str(content or ""), "utf-8")
        return str(path)

    async def _read_l2_payload(self, l2_ref: str | None) -> str:
        if not l2_ref:
            return ""
        try:
            path = Path(l2_ref)
            if not path.exists():
                return ""
            return await asyncio.to_thread(path.read_text, "utf-8")
        except Exception:
            return ""

    def _load_cost_metrics(self) -> dict[str, float]:
        defaults = {
            "retrieval_calls": 0.0,
            "planner_calls": 0.0,
            "commit_calls": 0.0,
            "rerank_calls": 0.0,
            "token_in": 0.0,
            "token_out": 0.0,
        }
        if not self.cost_file.exists():
            return defaults
        try:
            raw = json.loads(self.cost_file.read_text(encoding="utf-8"))
        except Exception:
            return defaults
        if not isinstance(raw, dict):
            return defaults
        out = dict(defaults)
        for key in out:
            val = raw.get(key)
            if isinstance(val, (int, float)):
                out[key] = float(val)
        return out

    async def _save_cost_metrics(self) -> None:
        payload = {k: float(v) for k, v in self._cost_metrics.items()}
        async with self._io_lock:
            await asyncio.to_thread(
                self.cost_file.write_text,
                json.dumps(payload, ensure_ascii=False, indent=2),
                "utf-8",
            )

    async def _bump_cost_metrics(
        self,
        *,
        retrieval_calls: float = 0,
        planner_calls: float = 0,
        commit_calls: float = 0,
        rerank_calls: float = 0,
        token_in: float = 0,
        token_out: float = 0,
    ) -> None:
        self._cost_metrics["retrieval_calls"] += float(retrieval_calls)
        self._cost_metrics["planner_calls"] += float(planner_calls)
        self._cost_metrics["commit_calls"] += float(commit_calls)
        self._cost_metrics["rerank_calls"] += float(rerank_calls)
        self._cost_metrics["token_in"] += float(token_in)
        self._cost_metrics["token_out"] += float(token_out)
        await self._save_cost_metrics()

    def _cost_delta_pct(self) -> float:
        base = max(1.0, float(self._cost_metrics.get("retrieval_calls", 0.0)))
        extra = (
            float(self._cost_metrics.get("planner_calls", 0.0))
            + float(self._cost_metrics.get("commit_calls", 0.0))
            + float(self._cost_metrics.get("rerank_calls", 0.0))
        )
        return round((extra / base) * 100.0, 2)

    def _append_jsonl(self, path: Path, payload: dict[str, Any]) -> None:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")

    async def _audit(self, event: AuditEvent) -> None:
        async with self._io_lock:
            await asyncio.to_thread(self._append_jsonl, self.audit_file, asdict(event))

    def _init_reranker(self) -> DashScopeTextReranker | None:
        try:
            target = resolve_memory_rerank_target(workspace=self.workspace)
        except Exception as exc:
            logger.warning("Memory rerank target resolution failed: {}", exc)
            return None
        model = str(target.resolved_model or "").strip()
        if not _is_dashscope_rerank_model(model):
            logger.warning("Unsupported rerank model configured for memory retrieval: {}", model)
            return None
        _, model_id = _split_provider_model(model, default_provider="dashscope")
        api_key = str(target.secret_payload.get("api_key", "") or self._dashscope_api_key or os.environ.get("DASHSCOPE_API_KEY", "")).strip()
        if not api_key:
            logger.warning(
                "DashScope rerank requested but API key is missing; rerank disabled "
                "(model={})",
                model,
            )
            return None
        return DashScopeTextReranker(
            api_key=api_key,
            model=model_id,
            api_base=str(target.base_url or self._dashscope_api_base or "").strip() or None,
        )

    def _rerank_items(self, *, query: str, items: list[SearchItem], top_n: int) -> list[SearchItem]:
        if self._reranker is None or not items:
            return items
        docs = [str(item.value.get("text", "") or item.value.get("content", ""))[:2000] for item in items]
        if not any(docs):
            return items
        try:
            ranked = self._reranker.rerank(query=query, documents=docs, top_n=top_n)
        except Exception as exc:
            logger.warning("Memory rerank failed; using fused retrieval order: {}", exc)
            return items

        ordered: list[SearchItem] = []
        seen: set[int] = set()
        for index, _score in ranked:
            if index < 0 or index >= len(items) or index in seen:
                continue
            ordered.append(items[index])
            seen.add(index)
            if len(ordered) >= top_n:
                break
        if len(ordered) < min(top_n, len(items)):
            for idx, item in enumerate(items):
                if idx in seen:
                    continue
                ordered.append(item)
                if len(ordered) >= top_n:
                    break
        return ordered or items

    def _window_extract(self, query: str, text: str, window: int) -> str:
        if window <= 0 or not text:
            return text
        normalized = " ".join(text.split())
        sentences = [s.strip() for s in re.split(_SENTENCE_SPLIT_RE, normalized) if s.strip()]
        if len(sentences) <= 1:
            return text
        q = query.lower()
        idx = 0
        for i, sent in enumerate(sentences):
            if q and q in sent.lower():
                idx = i
                break
        lo = max(0, idx - window)
        hi = min(len(sentences), idx + window + 1)
        return ". ".join(sentences[lo:hi])

    @staticmethod
    def _is_complex_query(text: str) -> bool:
        raw = str(text or "").strip()
        if len(raw) > 30:
            return True
        lowered = raw.lower()
        complex_terms = (
            "compare",
            "tradeoff",
            "architecture",
            "migration",
            "rollback",
            "root cause",
            "step by step",
            "多步骤",
            "复杂",
            "对比",
            "迁移",
            "回滚",
            "分阶段",
            "编排",
            "方案",
        )
        return any(term in lowered for term in complex_terms)

    def _plan_queries(self, query: str) -> list[TypedQuery]:
        raw = str(query or "").strip()
        if not raw:
            return []
        lowered = raw.lower()
        out: list[TypedQuery] = [
            TypedQuery(query=raw, context_type="memory", intent="memory_lookup", priority=1),
        ]
        resource_terms = ("doc", "file", "repo", "url", "资源", "文档", "链接", "路径")
        skill_terms = ("tool", "skill", "workflow", "runbook", "技能", "工具", "流程")
        if any(term in lowered for term in resource_terms):
            out.append(
                TypedQuery(
                    query=raw,
                    context_type="resource",
                    intent="resource_lookup",
                    priority=2,
                )
            )
        if any(term in lowered for term in skill_terms):
            out.append(
                TypedQuery(
                    query=raw,
                    context_type="skill",
                    intent="skill_lookup",
                    priority=3,
                )
            )
        return out[:3]

    async def _search_v2_candidates(
        self,
        *,
        namespace: tuple[str, ...],
        typed_query: TypedQuery,
        limit: int,
    ) -> list[tuple[ContextRecordV2, float]]:
        target_namespace = namespace if typed_query.context_type == 'memory' else self.catalog_namespace()

        def _run() -> list[tuple[ContextRecordV2, float]]:
            return self.store.search_context_v2(
                target_namespace,
                query=typed_query.query,
                limit=limit,
                context_type=typed_query.context_type,
            )

        return await asyncio.to_thread(_run)

    async def _search_v1_candidates(
        self,
        *,
        namespace: tuple[str, ...],
        typed_query: TypedQuery,
        limit: int,
    ) -> list[tuple[ContextRecordV2, float]]:
        if typed_query.context_type != "memory":
            return []

        def _run() -> list[SearchItem]:
            return self.store.search(namespace, query=typed_query.query, limit=limit)

        items = await asyncio.to_thread(_run)
        out: list[tuple[ContextRecordV2, float]] = []
        for idx, item in enumerate(items):
            text = str(item.value.get("text", "") or item.value.get("content", "")).strip()
            if not text:
                continue
            record_id = str(item.key or uuid.uuid4().hex[:16])
            out.append(
                (
                    ContextRecordV2(
                        record_id=record_id,
                        context_type="memory",
                        uri=self._context_uri(
                            context_type="memory",
                            channel=str(item.value.get("channel", "unknown")),
                            chat_id=str(item.value.get("chat_id", "unknown")),
                            record_id=record_id,
                        ),
                        parent_uri=None,
                        l0=self._l0_summary(text),
                        l1=self._l1_summary(text),
                        l2_ref=None,
                        tags=list(item.value.get("tags", []) or []),
                        source=str(item.value.get("source", "legacy")),
                        confidence=float(item.value.get("confidence", 0.5) or 0.5),
                        session_key=str(item.value.get("session_key", "")),
                        channel=str(item.value.get("channel", "")),
                        chat_id=str(item.value.get("chat_id", "")),
                    ),
                    float(1.0 / (idx + 1)),
                )
            )
        return out

    @staticmethod
    def _fuse_ranked_records(ranked_lists: list[list[ContextRecordV2]]) -> list[ContextRecordV2]:
        score: dict[str, float] = {}
        by_id: dict[str, ContextRecordV2] = {}
        for ranked in ranked_lists:
            for rank, record in enumerate(ranked, start=1):
                score[record.record_id] = score.get(record.record_id, 0.0) + 1.0 / (60 + rank)
                by_id[record.record_id] = record
        ordered_ids = [rid for rid, _ in sorted(score.items(), key=lambda kv: kv[1], reverse=True)]
        return [by_id[rid] for rid in ordered_ids if rid in by_id]

    def _rerank_context_records(
        self,
        *,
        query: str,
        records: list[ContextRecordV2],
        top_n: int,
    ) -> tuple[list[ContextRecordV2], list[dict[str, Any]]]:
        if self._reranker is None or not records:
            return records, []
        docs = [str(record.l1 or record.l0 or "")[:2000] for record in records]
        if not any(docs):
            return records, []
        try:
            ranked = self._reranker.rerank(query=query, documents=docs, top_n=top_n)
        except Exception as exc:
            logger.warning("Memory rerank failed; using fused retrieval order: {}", exc)
            return records, []

        out: list[ContextRecordV2] = []
        trace: list[dict[str, Any]] = []
        seen: set[int] = set()
        for index, score in ranked:
            if index < 0 or index >= len(records) or index in seen:
                continue
            out.append(records[index])
            trace.append({"record_id": records[index].record_id, "score": score})
            seen.add(index)
            if len(out) >= top_n:
                break
        if len(out) < min(top_n, len(records)):
            for idx, record in enumerate(records):
                if idx in seen:
                    continue
                out.append(record)
                if len(out) >= top_n:
                    break
        return out, trace

    async def _write_retrieval_trace(
        self,
        *,
        session_key: str | None,
        channel: str | None,
        chat_id: str | None,
        query: str,
        trace: RetrievalTrace,
    ) -> None:
        if not self._feature_enabled("observability"):
            return
        payload = asdict(trace)
        payload.update(
            {
                "session_key": str(session_key or ""),
                "channel": str(channel or ""),
                "chat_id": str(chat_id or ""),
                "query": str(query or ""),
            }
        )
        async with self._trace_lock:
            await asyncio.to_thread(self._append_jsonl, self.trace_file, payload)

    async def get_traces(self, *, session_key: str, limit: int = 20) -> list[dict[str, Any]]:
        if not self.trace_file.exists():
            return []
        raw = await asyncio.to_thread(self.trace_file.read_text, "utf-8")
        out: list[dict[str, Any]] = []
        for line in raw.splitlines():
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            if str(obj.get("session_key", "")) != str(session_key):
                continue
            out.append(obj)
        return out[-max(1, int(limit)) :]

    async def explain_query(
        self,
        *,
        query: str,
        session_key: str,
        channel: str | None = None,
        chat_id: str | None = None,
    ) -> dict[str, Any]:
        block = await self.retrieve_block(
            query=query,
            channel=channel,
            chat_id=chat_id,
            session_key=session_key,
        )
        traces = await self.get_traces(session_key=session_key, limit=1)
        return {
            "query": query,
            "session_key": session_key,
            "trace": traces[0] if traces else None,
            "preview_block": block,
        }

    async def search_tool_view(
        self,
        *,
        query: str,
        channel: str | None,
        chat_id: str | None,
        session_key: str | None = None,
        limit: int = 8,
        context_type: ContextType | None = None,
        include_l2: bool = False,
    ) -> dict[str, Any]:
        raw_query = str(query or "").strip()
        if not raw_query:
            return {
                "query": "",
                "grouped": {"memory": [], "resource": [], "skill": []},
                "view": [],
                "plan": [],
                "meta": {"total": 0, "limit": max(1, int(limit))},
            }

        ctype = None
        if context_type in CONTEXT_TYPE_ALL:
            ctype = context_type

        namespace = self.namespace_for(channel=channel, chat_id=chat_id)
        top_k = max(1, int(limit))
        candidate_limit = max(top_k, int(getattr(self.config.retrieval, "fused_top_k", top_k)), top_k * 3)
        planner_enabled = self._feature_enabled("query_planner")
        use_planner = planner_enabled and ctype is None and self._is_complex_query(raw_query)

        if ctype is not None:
            typed_queries = [TypedQuery(query=raw_query, context_type=ctype, intent="typed_filter", priority=1)]
        elif use_planner:
            typed_queries = self._plan_queries(raw_query)[:3]
            if not typed_queries:
                typed_queries = [TypedQuery(query=raw_query, context_type="memory", intent="fallback", priority=1)]
        else:
            typed_queries = [TypedQuery(query=raw_query, context_type="memory", intent="fast_path", priority=1)]

        ranked_lists: list[list[ContextRecordV2]] = []
        plan_trace: list[dict[str, Any]] = []
        for typed in typed_queries:
            records_with_score: list[tuple[ContextRecordV2, float]] = []
            if self.arch_version == "v2" and self._feature_enabled("unified_context"):
                records_with_score = await self._search_v2_candidates(
                    namespace=namespace,
                    typed_query=typed,
                    limit=candidate_limit,
                )
            if not records_with_score:
                records_with_score = await self._search_v1_candidates(
                    namespace=namespace,
                    typed_query=typed,
                    limit=candidate_limit,
                )
            ranked = [record for record, _score in records_with_score]
            ranked_lists.append(ranked)
            plan_trace.append(
                {
                    "query": typed.query,
                    "context_type": typed.context_type,
                    "intent": typed.intent,
                    "priority": typed.priority,
                    "candidates": len(ranked),
                }
            )

        fused = self._fuse_ranked_records(ranked_lists)
        rerank_trace: list[dict[str, Any]] = []
        rerank_triggered = self._reranker is not None and len(fused) > 1
        if rerank_triggered:
            fused, rerank_trace = self._rerank_context_records(query=raw_query, records=fused, top_n=top_k)
        else:
            fused = fused[:top_k]

        grouped: dict[str, list[dict[str, Any]]] = {"memory": [], "resource": [], "skill": []}
        unified: list[dict[str, Any]] = []
        for rank, record in enumerate(fused[:top_k], start=1):
            l2_preview = ""
            if include_l2 and record.l2_ref:
                l2_text = await self._read_l2_payload(record.l2_ref)
                l2_preview = self._window_extract(raw_query, l2_text, max(1, int(self.config.retrieval.sentence_window)))[:500]
            entry = {
                "rank": rank,
                "record_id": record.record_id,
                "context_type": record.context_type,
                "uri": record.uri,
                "source": record.source,
                "confidence": round(float(record.confidence), 4),
                "l0": record.l0,
                "l1": record.l1,
                "l2_preview": l2_preview,
                "tags": list(record.tags or []),
            }
            grouped.setdefault(record.context_type, []).append(entry)
            unified.append(entry)

        trace = RetrievalTrace(
            plan=plan_trace,
            candidates=[
                {
                    "record_id": record.record_id,
                    "context_type": record.context_type,
                    "source": record.source,
                    "confidence": record.confidence,
                }
                for record in fused[: max(top_k * 2, 8)]
            ],
            rerank=rerank_trace,
            injected_blocks=[
                {"record_id": row["record_id"], "context_type": row["context_type"], "reason": "tool_response"}
                for row in unified
            ],
            token_budget_used=self._estimate_tokens(json.dumps(unified, ensure_ascii=False)),
        )
        await self._write_retrieval_trace(
            session_key=session_key,
            channel=channel,
            chat_id=chat_id,
            query=raw_query,
            trace=trace,
        )
        await self._bump_cost_metrics(
            retrieval_calls=1,
            planner_calls=1 if use_planner else 0,
            rerank_calls=1 if rerank_triggered else 0,
            token_in=self._estimate_tokens(raw_query),
            token_out=self._estimate_tokens(json.dumps(unified, ensure_ascii=False)),
        )

        return {
            "query": raw_query,
            "grouped": grouped,
            "view": unified,
            "plan": plan_trace,
            "meta": {
                "total": len(unified),
                "limit": top_k,
                "trace_id": trace.trace_id,
                "session_key": str(session_key or ""),
                "channel": str(channel or ""),
                "chat_id": str(chat_id or ""),
            },
        }

    async def _build_layered_context_block(
        self,
        *,
        query: str,
        records: list[ContextRecordV2],
        budget_tokens: int,
    ) -> tuple[str, list[dict[str, Any]], int]:
        if not records:
            return "", [], 0
        default_level = self._default_load_level()
        lines = ["# Retrieved Context (Layered)"]
        injected_blocks: list[dict[str, Any]] = []
        used = self._estimate_tokens(lines[0])

        for idx, record in enumerate(records, start=1):
            label = record.record_id if record.context_type == "memory" else f"{record.context_type}:{record.record_id}"
            header = f"- [{label}] {record.l0 or self._l0_summary(record.l1)}"
            candidate = [header]
            if default_level in {"l1", "l2"} and record.l1:
                candidate.append(f"  L1: {record.l1}")
            include_l2 = default_level == "l2" or idx <= 2
            if include_l2 and record.l2_ref:
                l2_text = await self._read_l2_payload(record.l2_ref)
                snippet = self._window_extract(query, l2_text, max(1, int(self.config.retrieval.sentence_window)))[:500]
                if snippet:
                    candidate.append(f"  L2: {snippet}")

            block_text = "\n".join(candidate)
            block_tokens = self._estimate_tokens(block_text)
            if used + block_tokens > budget_tokens:
                header_tokens = self._estimate_tokens(header)
                if used + header_tokens <= budget_tokens:
                    lines.append(header)
                    used += header_tokens
                    injected_blocks.append(
                        {
                            "record_id": record.record_id,
                            "context_type": record.context_type,
                            "layer": "l0",
                            "tokens": header_tokens,
                            "reason": "fallback_l0_due_budget",
                        }
                    )
                    continue
                injected_blocks.append(
                    {
                        "record_id": record.record_id,
                        "context_type": record.context_type,
                        "reason": "token_budget_exceeded",
                    }
                )
                continue
            lines.extend(candidate)
            used += block_tokens
            injected_blocks.append(
                {
                    "record_id": record.record_id,
                    "context_type": record.context_type,
                    "layer": "l2" if include_l2 and record.l2_ref else default_level,
                    "tokens": block_tokens,
                }
            )
        return ("\n".join(lines) if len(lines) > 1 else ""), injected_blocks, used

    async def _dual_write_legacy(self, text: str) -> None:
        if not self.config.compat.dual_write_legacy_files:
            return
        entry = f"[{datetime.now().strftime('%Y-%m-%d %H:%M')}] MEMORY: {text[:1000]}"
        await asyncio.to_thread(self._legacy.append_history, entry)
        long_term = self._legacy.read_long_term()
        marker = "## RAG Extracted Facts"
        snippet = f"- {text[:300]}"
        if marker not in long_term:
            long_term = (long_term.rstrip() + "\n\n" + marker + "\n\n" + snippet + "\n").strip()
        else:
            long_term = long_term.rstrip() + "\n" + snippet + "\n"
        await asyncio.to_thread(self._legacy.write_long_term, long_term)

    async def retrieve_block(
        self,
        *,
        query: str,
        channel: str | None,
        chat_id: str | None,
        session_key: str | None = None,
    ) -> str:
        raw_query = str(query or "").strip()
        if not raw_query:
            return ""
        namespace = self.namespace_for(channel=channel, chat_id=chat_id)
        limit = max(1, int(self.config.retrieval.context_top_k))
        candidate_limit = max(
            limit,
            int(getattr(self.config.retrieval, "fused_top_k", limit)),
            limit * 3,
        )
        budget_tokens = max(120, int(self.config.retrieval.max_context_tokens))

        planner_enabled = self._feature_enabled("query_planner")
        use_planner = planner_enabled and self._is_complex_query(raw_query)
        if use_planner:
            typed_queries = self._plan_queries(raw_query)
            if not typed_queries:
                typed_queries = [TypedQuery(query=raw_query, context_type="memory", intent="fallback", priority=1)]
        else:
            typed_queries = [TypedQuery(query=raw_query, context_type="memory", intent="fast_path", priority=1)]

        ranked_lists: list[list[ContextRecordV2]] = []
        plan_trace: list[dict[str, Any]] = []
        for typed in typed_queries[:3]:
            records_with_score: list[tuple[ContextRecordV2, float]] = []
            if self.arch_version == "v2" and self._feature_enabled("unified_context"):
                records_with_score = await self._search_v2_candidates(
                    namespace=namespace,
                    typed_query=typed,
                    limit=candidate_limit,
                )
            if not records_with_score:
                records_with_score = await self._search_v1_candidates(
                    namespace=namespace,
                    typed_query=typed,
                    limit=candidate_limit,
                )
            ranked = [record for record, _score in records_with_score]
            ranked_lists.append(ranked)
            plan_trace.append(
                {
                    "query": typed.query,
                    "context_type": typed.context_type,
                    "intent": typed.intent,
                    "priority": typed.priority,
                    "candidates": len(ranked),
                }
            )

        fused = self._fuse_ranked_records(ranked_lists)
        if not fused:
            empty_trace = RetrievalTrace(
                plan=plan_trace,
                candidates=[],
                rerank=[],
                injected_blocks=[],
                token_budget_used=0,
            )
            await self._write_retrieval_trace(
                session_key=session_key,
                channel=channel,
                chat_id=chat_id,
                query=raw_query,
                trace=empty_trace,
            )
            await self._bump_cost_metrics(
                retrieval_calls=1,
                planner_calls=1 if use_planner else 0,
                token_in=self._estimate_tokens(raw_query),
                token_out=0,
            )
            return ""

        rerank_trace: list[dict[str, Any]] = []
        rerank_triggered = self._reranker is not None and len(fused) > 1
        if rerank_triggered:
            fused, rerank_trace = self._rerank_context_records(query=raw_query, records=fused, top_n=limit)
        else:
            fused = fused[:limit]

        layered_enabled = self._feature_enabled("layered_loading")
        if layered_enabled:
            block, injected_blocks, used_tokens = await self._build_layered_context_block(
                query=raw_query,
                records=fused[:limit],
                budget_tokens=budget_tokens,
            )
        else:
            lines = ["## Retrieved Memory"]
            injected_blocks = []
            used_tokens = self._estimate_tokens(lines[0])
            for record in fused[:limit]:
                line = f"- [{record.record_id}] {(record.l1 or record.l0)[:500]}"
                line_tokens = self._estimate_tokens(line)
                if used_tokens + line_tokens > budget_tokens:
                    continue
                lines.append(line)
                used_tokens += line_tokens
                injected_blocks.append({"record_id": record.record_id, "context_type": record.context_type})
            block = "\n".join(lines) if len(lines) > 1 else ""

        trace = RetrievalTrace(
            plan=plan_trace,
            candidates=[
                {
                    "record_id": record.record_id,
                    "context_type": record.context_type,
                    "source": record.source,
                    "confidence": record.confidence,
                }
                for record in fused[: max(limit * 2, 8)]
            ],
            rerank=rerank_trace,
            injected_blocks=injected_blocks,
            token_budget_used=used_tokens,
        )
        await self._write_retrieval_trace(
            session_key=session_key,
            channel=channel,
            chat_id=chat_id,
            query=raw_query,
            trace=trace,
        )

        await self._bump_cost_metrics(
            retrieval_calls=1,
            planner_calls=1 if use_planner else 0,
            rerank_calls=1 if rerank_triggered else 0,
            token_in=self._estimate_tokens(raw_query),
            token_out=used_tokens,
        )

        return block

    def _score_fact_confidence(self, text: str) -> float:
        normalized = " ".join(str(text or "").split())
        if not normalized:
            return 0.0
        lower = normalized.lower()
        score = 0.25

        english_markers = (
            "prefer",
            "always",
            "never",
            "my name is",
            "i am",
            "deadline",
            "project",
            "remember",
            "habit",
            "i like",
            "i don't like",
            "my team",
            "my role",
        )
        chinese_markers = (
            "我叫",
            "我是",
            "我喜欢",
            "我不喜欢",
            "偏好",
            "习惯",
            "项目",
            "截止",
            "记住",
            "请记住",
            "我的团队",
            "我的角色",
        )
        for kw in english_markers:
            if kw in lower:
                score += 0.08
        for kw in chinese_markers:
            if kw in normalized:
                score += 0.08

        if ":" in normalized:
            score += 0.05
        if len(normalized) > 80:
            score += 0.05
        if any(ch.isdigit() for ch in normalized):
            score += 0.04
        if "?" in normalized:
            score -= 0.08
        if normalized.count("\n") > 8:
            score -= 0.05
        return max(0.0, min(score, 0.98))

    async def ingest_turn(
        self,
        *,
        session_key: str,
        channel: str,
        chat_id: str,
        messages: list[dict[str, Any]],
    ) -> None:
        if not messages:
            return

        # Keep concise user+assistant textual content for memory storage.
        selected: list[str] = []
        for msg in messages[-8:]:
            role = str(msg.get("role", ""))
            if role not in {"user", "assistant"}:
                continue
            content = msg.get("content")
            if isinstance(content, str) and content.strip():
                if content.strip().startswith("[Runtime Context"):
                    continue
                selected.append(f"{role.upper()}: {content.strip()}")
        if not selected:
            return

        text = "\n".join(selected)
        conf = self._score_fact_confidence(text)
        threshold = float(self.config.guard.auto_fact_confidence)
        trace_id = uuid.uuid4().hex[:12]

        if self.config.guard.mode == "tiered" and conf < threshold:
            pending = PendingFact(
                candidate=text,
                reason=f"confidence<{threshold}",
                confidence=conf,
                session_key=session_key,
                channel=channel,
                chat_id=chat_id,
            )
            async with self._io_lock:
                await asyncio.to_thread(self._append_jsonl, self.pending_file, asdict(pending))
            await self._audit(
                AuditEvent(
                    action="pending",
                    reason=pending.reason,
                    actor="memory_manager",
                    session_key=session_key,
                    trace_id=trace_id,
                    after=asdict(pending),
                )
            )
            return

        namespace = self.namespace_for(channel=channel, chat_id=chat_id)
        record_id = uuid.uuid4().hex[:16]

        if self.arch_version == "v2" and self._feature_enabled("unified_context"):
            l2_ref = None
            if self._feature_enabled("split_store"):
                l2_ref = await self._write_l2_payload(record_id=record_id, content=text)
            uri = self._context_uri(
                context_type="memory",
                channel=channel,
                chat_id=chat_id,
                record_id=record_id,
            )
            context_record = ContextRecordV2(
                record_id=record_id,
                context_type="memory",
                uri=uri,
                l0=self._l0_summary(text),
                l1=self._l1_summary(text),
                l2_ref=l2_ref,
                tags=["turn_ingest"],
                source="turn",
                confidence=conf,
                session_key=session_key,
                channel=channel,
                chat_id=chat_id,
            )
            try:
                await asyncio.to_thread(self.store.put_context_v2, namespace, context_record)
                await self._audit(
                    AuditEvent(
                        action="upsert",
                        reason="turn_ingest_v2",
                        actor="memory_manager",
                        session_key=session_key,
                        trace_id=trace_id,
                        after=asdict(context_record),
                    )
                )
            except Exception:
                logger.exception("Failed to ingest v2 context record, fallback to v1")
                record = MemoryRecord(
                    record_id=record_id,
                    text=text,
                    source="turn",
                    confidence=conf,
                    session_key=session_key,
                    channel=channel,
                    chat_id=chat_id,
                )
                value = asdict(record)
                await asyncio.to_thread(self.store.put, namespace, record.record_id, value)
                await self._audit(
                    AuditEvent(
                        action="upsert",
                        reason="turn_ingest_v1_fallback",
                        actor="memory_manager",
                        session_key=session_key,
                        trace_id=trace_id,
                        after=value,
                    )
                )
        else:
            record = MemoryRecord(
                record_id=record_id,
                text=text,
                source="turn",
                confidence=conf,
                session_key=session_key,
                channel=channel,
                chat_id=chat_id,
            )
            value = asdict(record)
            await asyncio.to_thread(self.store.put, namespace, record.record_id, value)
            await self._audit(
                AuditEvent(
                    action="upsert",
                    reason="turn_ingest",
                    actor="memory_manager",
                    session_key=session_key,
                    trace_id=trace_id,
                    after=value,
                )
            )

        await self._dual_write_legacy(text)

    async def list_pending(self, limit: int = 50) -> list[PendingFact]:
        if not self.pending_file.exists():
            return []
        lines = await asyncio.to_thread(self.pending_file.read_text, "utf-8")
        out: list[PendingFact] = []
        for raw in lines.splitlines():
            if not raw.strip():
                continue
            try:
                data = json.loads(raw)
                if data.get("status") == "pending":
                    out.append(PendingFact(**data))
            except Exception:
                continue
        return out[-limit:]

    async def update_pending(self, pending_id: str, status: Literal["approved", "rejected"]) -> bool:
        if not self.pending_file.exists():
            return False
        raw = await asyncio.to_thread(self.pending_file.read_text, "utf-8")
        rows: list[dict[str, Any]] = []
        target_pending: PendingFact | None = None
        for line in raw.splitlines():
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            if obj.get("pending_id") == pending_id and obj.get("status") == "pending":
                try:
                    target_pending = PendingFact(**obj)
                except Exception:
                    target_pending = None
                obj["status"] = status
            rows.append(obj)
        if target_pending is None:
            return False

        trace_id = uuid.uuid4().hex[:12]
        if status == "approved":
            try:
                namespace = self.namespace_for(channel=target_pending.channel, chat_id=target_pending.chat_id)
                record_id = uuid.uuid4().hex[:16]
                value: dict[str, Any]
                legacy_record = MemoryRecord(
                    record_id=record_id,
                    text=target_pending.candidate,
                    source="pending_approved",
                    confidence=target_pending.confidence,
                    session_key=target_pending.session_key,
                    channel=target_pending.channel,
                    chat_id=target_pending.chat_id,
                )
                legacy_value = asdict(legacy_record)
                if self.arch_version == "v2" and self._feature_enabled("unified_context"):
                    l2_ref = None
                    if self._feature_enabled("split_store"):
                        l2_ref = await self._write_l2_payload(record_id=record_id, content=target_pending.candidate)
                    record_v2 = ContextRecordV2(
                        record_id=record_id,
                        context_type="memory",
                        uri=self._context_uri(
                            context_type="memory",
                            channel=target_pending.channel,
                            chat_id=target_pending.chat_id,
                            record_id=record_id,
                        ),
                        l0=self._l0_summary(target_pending.candidate),
                        l1=self._l1_summary(target_pending.candidate),
                        l2_ref=l2_ref,
                        tags=["pending_approved"],
                        source="pending_approved",
                        confidence=target_pending.confidence,
                        session_key=target_pending.session_key,
                        channel=target_pending.channel,
                        chat_id=target_pending.chat_id,
                    )
                    value = asdict(record_v2)
                    await asyncio.to_thread(self.store.put_context_v2, namespace, record_v2)
                    if str(getattr(self.config, "mode", "") or "").lower() == "dual":
                        await asyncio.to_thread(self.store.put, namespace, legacy_record.record_id, legacy_value)
                else:
                    value = legacy_value
                    await asyncio.to_thread(self.store.put, namespace, legacy_record.record_id, value)
                await self._dual_write_legacy(target_pending.candidate)
                await self._audit(
                    AuditEvent(
                        action="approve",
                        reason="pending_fact_approved",
                        actor="memory_manager",
                        session_key=target_pending.session_key,
                        trace_id=trace_id,
                        before={"pending_id": pending_id},
                        after=value,
                    )
                )
            except Exception:
                logger.exception("Failed to approve pending fact {}", pending_id)
                return False
        else:
            await self._audit(
                AuditEvent(
                    action="reject",
                    reason="pending_fact_rejected",
                    actor="memory_manager",
                    session_key=target_pending.session_key,
                    trace_id=trace_id,
                    before={"pending_id": pending_id},
                    after={"status": status},
                )
            )

        content = "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + ("\n" if rows else "")
        async with self._io_lock:
            await asyncio.to_thread(self.pending_file.write_text, content, "utf-8")
        return True

    async def commit_session(
        self,
        *,
        session_key: str,
        channel: str,
        chat_id: str,
        messages: list[dict[str, Any]],
        reason: str = "turn_trigger",
    ) -> CommitArtifact:
        archive_id = uuid.uuid4().hex[:12]
        if not messages:
            return CommitArtifact(
                archive_id=archive_id,
                summary_uri="",
                extracted_count=0,
                merged_count=0,
                skipped_count=0,
            )

        commit_time = _now_iso()
        channel_safe = str(channel or "unknown")
        chat_safe = str(chat_id or "unknown")
        session_safe = (
            str(session_key or f"{channel_safe}:{chat_safe}")
            .replace("/", "_")
            .replace("\\", "_")
            .replace(":", "_")
        )
        archive_path = ensure_dir(self.archive_dir / session_safe) / f"{archive_id}.jsonl"
        lines = [json.dumps(m, ensure_ascii=False) for m in messages]
        await asyncio.to_thread(archive_path.write_text, ("\n".join(lines) + "\n"), "utf-8")

        user_lines: list[str] = []
        assistant_lines: list[str] = []
        for msg in messages:
            role = str(msg.get("role", ""))
            content = msg.get("content")
            if not isinstance(content, str):
                continue
            text = " ".join(content.split())
            if not text or text.startswith("[Runtime Context"):
                continue
            if role == "user":
                user_lines.append(text)
            elif role == "assistant":
                assistant_lines.append(text)

        summary_parts: list[str] = []
        if user_lines:
            summary_parts.append("User focus: " + " | ".join(user_lines[-3:]))
        if assistant_lines:
            summary_parts.append("Assistant outputs: " + " | ".join(assistant_lines[-2:]))
        summary_text = "\n".join(summary_parts)[:1800]
        summary_path = self.commit_summary_dir / f"{archive_id}.md"
        await asyncio.to_thread(summary_path.write_text, summary_text, "utf-8")

        extracted: list[tuple[str, list[str], float]] = []
        category_rules: tuple[tuple[str, tuple[str, ...]], ...] = (
            ("profile", ("my name is", "我是", "我叫", "my role", "我的角色")),
            ("preferences", ("prefer", "喜欢", "偏好", "不喜欢", "i like")),
            ("entities", ("team", "公司", "project", "项目", "repo", "仓库")),
            ("events", ("deadline", "due", "截至", "截止", "schedule", "日程")),
            ("cases", ("issue", "bug", "问题", "案例", "故障")),
            ("patterns", ("always", "never", "通常", "经常", "习惯")),
        )
        seen_text_hash: set[str] = set()
        for text in user_lines[-20:]:
            lower = text.lower()
            tags = [name for name, markers in category_rules if any(m in lower or m in text for m in markers)]
            if not tags:
                continue
            text_hash = self._stable_text_hash(text)
            if text_hash in seen_text_hash:
                continue
            seen_text_hash.add(text_hash)
            extracted.append((text, tags, self._score_fact_confidence(text)))

        namespace = self.namespace_for(channel=channel, chat_id=chat_id)
        created = 0
        merged = 0
        skipped = 0
        for text, tags, conf in extracted:
            existing = await asyncio.to_thread(
                self.store.search_context_v2,
                namespace,
                query=text,
                limit=3,
                context_type="memory",
            )
            duplicate = False
            for record, _score in existing:
                if self._stable_text_hash(record.l1) == self._stable_text_hash(text):
                    duplicate = True
                    break
            if duplicate:
                skipped += 1
                continue

            record_id = uuid.uuid4().hex[:16]
            l2_ref = None
            if self._feature_enabled("split_store"):
                l2_ref = await self._write_l2_payload(record_id=record_id, content=text)
            record = ContextRecordV2(
                record_id=record_id,
                context_type="memory",
                uri=self._context_uri(
                    context_type="memory",
                    channel=channel_safe,
                    chat_id=chat_safe,
                    record_id=record_id,
                ),
                l0=self._l0_summary(text),
                l1=self._l1_summary(text),
                l2_ref=l2_ref,
                tags=tags + [f"commit:{reason}"],
                source="commit",
                confidence=conf,
                session_key=session_key,
                channel=channel_safe,
                chat_id=chat_safe,
            )
            await asyncio.to_thread(self.store.put_context_v2, namespace, record)
            created += 1

            if tags:
                for tag in tags[:2]:
                    try:
                        await asyncio.to_thread(
                            self.store.add_context_relation_v2,
                            from_uri=record.uri,
                            to_uri=f"g3ku://taxonomy/{tag}",
                            relation_type="tagged_as",
                            source="commit",
                            weight=1.0,
                        )
                        merged += 1
                    except Exception:
                        logger.debug("Skip relation write for commit record {}", record.record_id)

        await self._audit(
            AuditEvent(
                action="commit",
                reason=reason,
                actor="memory_manager",
                session_key=session_key,
                trace_id=archive_id,
                after={
                    "archive_id": archive_id,
                    "summary_uri": str(summary_path),
                    "extracted_count": len(extracted),
                    "created_count": created,
                    "merged_count": merged,
                    "skipped_count": skipped,
                    "commit_time": commit_time,
                },
            )
        )
        await self._bump_cost_metrics(
            commit_calls=1,
            token_in=self._estimate_tokens("\n".join(user_lines[-20:])),
            token_out=self._estimate_tokens(summary_text),
        )
        return CommitArtifact(
            archive_id=archive_id,
            summary_uri=str(summary_path),
            extracted_count=created,
            merged_count=merged,
            skipped_count=skipped,
        )

    async def migrate_v2(self, *, dry_run: bool = False, limit: int = 100000) -> dict[str, Any]:
        all_items = await asyncio.to_thread(self.store.search, (), query=None, limit=limit, offset=0)
        migrated = 0
        skipped = 0
        for item in all_items:
            value = dict(item.value or {})
            text = str(value.get("text", "") or value.get("content", "")).strip()
            if not text:
                skipped += 1
                continue
            channel = str(value.get("channel", "unknown"))
            chat_id = str(value.get("chat_id", "unknown"))
            session_key = str(value.get("session_key", f"{channel}:{chat_id}"))
            record_id = str(value.get("record_id", item.key or uuid.uuid4().hex[:16]))
            namespace = self.namespace_for(channel=channel, chat_id=chat_id)
            l2_ref = None
            if self._feature_enabled("split_store"):
                l2_ref = str(self.context_store_dir / f"{record_id}.txt")
                if not dry_run and not Path(l2_ref).exists():
                    await asyncio.to_thread(Path(l2_ref).write_text, text, "utf-8")
            record = ContextRecordV2(
                record_id=record_id,
                context_type="memory",
                uri=self._context_uri(
                    context_type="memory",
                    channel=channel,
                    chat_id=chat_id,
                    record_id=record_id,
                ),
                l0=self._l0_summary(text),
                l1=self._l1_summary(text),
                l2_ref=l2_ref,
                tags=list(value.get("tags", []) or []),
                source=str(value.get("source", "migrate_v1")),
                confidence=float(value.get("confidence", 0.5) or 0.5),
                session_key=session_key,
                channel=channel,
                chat_id=chat_id,
                created_at=str(value.get("created_at", _now_iso())),
                updated_at=str(value.get("updated_at", _now_iso())),
            )
            if not dry_run:
                await asyncio.to_thread(self.store.put_context_v2, namespace, record)
            migrated += 1
        return {
            "dry_run": dry_run,
            "source_records": len(all_items),
            "migrated": migrated,
            "skipped": skipped,
        }

    async def run_decay(self, *, dry_run: bool = False) -> dict[str, Any]:
        retention_days = getattr(self.config, "retention_days", None)
        if retention_days is None:
            return {"retention_days": None, "scanned": 0, "deleted": 0, "dry_run": dry_run}

        cutoff = datetime.now() - timedelta(days=int(retention_days))
        scanned = 0
        deleted = 0
        namespace_prefix = ()
        all_records = await asyncio.to_thread(self.store.list_context_v2, namespace_prefix, limit=200000, offset=0)
        for record in all_records:
            scanned += 1
            if "pinned" in record.tags:
                continue
            try:
                updated = datetime.fromisoformat(record.updated_at)
            except Exception:
                continue
            if updated >= cutoff:
                continue
            if dry_run:
                deleted += 1
                continue
            namespace = self.namespace_for(channel=record.channel, chat_id=record.chat_id)
            await asyncio.to_thread(self.store.delete_context_v2, namespace, record.record_id)
            if record.l2_ref:
                try:
                    path = Path(record.l2_ref)
                    if path.exists():
                        await asyncio.to_thread(path.unlink)
                except Exception:
                    logger.debug("Failed to delete L2 payload {}", record.l2_ref)
            deleted += 1
        return {
            "retention_days": retention_days,
            "scanned": scanned,
            "deleted": deleted,
            "dry_run": dry_run,
        }

    async def stats(self) -> dict[str, Any]:
        all_items = await asyncio.to_thread(self.store.search, (), query=None, limit=100000, offset=0)
        v2_items = await asyncio.to_thread(self.store.list_context_v2, (), limit=200000, offset=0)
        pending = await self.list_pending(limit=100000)
        by_type: dict[str, int] = defaultdict(int)
        layers = {"l0": 0, "l1": 0, "l2": 0}
        for record in v2_items:
            by_type[record.context_type] += 1
            if record.l0:
                layers["l0"] += 1
            if record.l1:
                layers["l1"] += 1
            if record.l2_ref:
                layers["l2"] += 1
        return {
            "records": len(all_items),
            "records_v2": len(v2_items),
            "pending": len([p for p in pending if p.status == "pending"]),
            "records_by_type": dict(by_type),
            "layer_distribution": layers,
            "dense_enabled": self.store._dense_enabled,
            "sqlite_path": str(self.store.sqlite_path),
            "qdrant_path": str(self.store.qdrant_path),
            "planner_calls": int(self._cost_metrics.get("planner_calls", 0)),
            "commit_calls": int(self._cost_metrics.get("commit_calls", 0)),
            "rerank_calls": int(self._cost_metrics.get("rerank_calls", 0)),
            "token_in": int(self._cost_metrics.get("token_in", 0)),
            "token_out": int(self._cost_metrics.get("token_out", 0)),
            "cost_delta_pct": self._cost_delta_pct(),
        }


