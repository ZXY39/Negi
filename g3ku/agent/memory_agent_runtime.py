from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import secrets
import shutil
import string
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool
from loguru import logger

from g3ku.agent.catalog_store import _release_file_lock, _try_acquire_file_lock
from g3ku.agent.markdown_memory import (
    MemoryEntry,
    format_memory_entry,
    note_file_name,
    parse_memory_document,
    validate_memory_document,
)
from g3ku.agent.memory_catalog_bridge import MemoryCatalogBridge
from g3ku.config.live_runtime import get_runtime_config
from g3ku.providers.base import normalize_usage_payload
from g3ku.providers.chatmodels import build_chat_model
from main.runtime.chat_backend import build_actual_request_diagnostics
from main.prompts import load_prompt

_NOTE_REF_RE = re.compile(r"\bref:(?P<ref>[a-z0-9_]+)\b")
_STAGED_DOCUMENT_UNSET = object()
_LEGACY_CLEANUP_RELATIVE_PATHS: tuple[str, ...] = (
    "memory/HISTORY.md",
    "memory/structured_current.jsonl",
    "memory/structured_history.jsonl",
    "memory/structured_state.json",
    "memory/sync_journal.jsonl",
    "memory/sync_state.json",
    "memory/audit.jsonl",
    "memory/pending_facts.jsonl",
    "memory/context_store",
)


@dataclass(slots=True)
class MemoryQueueRequest:
    op: str
    decision_source: str
    payload_text: str
    created_at: str
    request_id: str = ""
    trigger_source: str = ""
    session_key: str = ""
    status: str = "pending"
    processing_started_at: str = ""
    last_error_text: str = ""
    last_error_at: str = ""
    retry_after: str = ""


@dataclass(slots=True)
class MemoryBatch:
    op: str
    items: list[MemoryQueueRequest] = field(default_factory=list)


@dataclass(slots=True)
class MemoryPayloadClassification:
    decision_source: str
    memory_kind: str
    enqueue: bool
    replace_mode: str
    reason: str
    normalized_payload_text: str


@dataclass(slots=True)
class AutonomousReviewDecision:
    enqueue: bool
    reason: str
    decision_source: str
    memory_kind: str
    payload_text: str
    sampled: bool = False


class MemoryStrategyV2:
    _IGNORE_TRIGGER_SOURCES = {
        "pre_compression_flush",
        "session_boundary_flush",
    }
    _USER_IDENTITY_PATTERNS = (
        re.compile(r"(?:我叫|我的名字是|我来自|我住在|我的身份是)"),
        re.compile(
            r"\b(?:my name is|i live in|i'm based in|my role is|my identity is|i work on project|i work on the project)\b",
            re.IGNORECASE,
        ),
    )
    _USER_PREFERENCE_PATTERNS = (
        re.compile(r"(?:记住|请记住).*(?:以后|默认|优先|偏好|请用|不要用|总是|一律)"),
        re.compile(r"(?:以后|今后|默认|优先|偏好|请用中文|用中文回复|别用英文)"),
        re.compile(r"\b(?:prefer|default to|by default|from now on|always use|remember that)\b", re.IGNORECASE),
    )
    _USER_CONSTRAINT_PATTERNS = (
        re.compile(r"(?:记住|请记住).*(?:不要|别|必须|先|禁止)"),
        re.compile(r"(?:以后|今后|默认|始终|一律).*(?:不要|别|必须|先|禁止)"),
        re.compile(r"\b(?:from now on|going forward|always|never|by default|remember that)\b.*\b(?:must|do not|don't|never)\b", re.IGNORECASE),
    )
    _SELF_RULE_PATTERNS = (
        re.compile(r"\b(?:report|summarize|include|mention|record)\b", re.IGNORECASE),
        re.compile(r"(?:说明|汇总|报告|写明|包含|记录)"),
    )
    _SELF_TRANSIENT_PATTERNS = (
        re.compile(r"session boundary flush", re.IGNORECASE),
        re.compile(r"pre[-_ ]compression", re.IGNORECASE),
        re.compile(r"\b(?:runtime state|pause state|currently processing|still processing|in progress)\b", re.IGNORECASE),
        re.compile(r"\b(?:processing_started_at|retry_after|last_error_text|request_id|status=pending|status=processing)\b", re.IGNORECASE),
        re.compile(r"(?:会话边界|压缩前刷新|暂停态|正在处理中|仍在处理中|请求编号|重试时间)"),
    )
    _TASK_LOCAL_PATTERNS = (
        re.compile(r"\b(?:attached diff|this change|for this change|this task|this turn|for now|later|yet)\b", re.IGNORECASE),
        re.compile(r"\b(?:i'll do that later|i will do that later|don't run tests yet|do not run tests yet)\b", re.IGNORECASE),
        re.compile(r"\b(?:use the attached|wait for me to|i'll handle|i will handle)\b", re.IGNORECASE),
        re.compile(r"\b(?:right now|today|this command|this one|working on|debugging|currently debugging|currently working on)\b", re.IGNORECASE),
        re.compile(r"\b(?:repo root|bash for this one|for this command)\b", re.IGNORECASE),
        re.compile(r"(?:这个改动|这次改动|这个任务|这一轮|稍后|待会|先不要|附件 diff|附带 diff)"),
    )

    def __init__(self, config: Any):
        self.config = config
        self._review_interval_turns = max(int(getattr(config.queue, "review_interval_turns", 10) or 10), 1)
        self._document_max_chars = max(int(getattr(config.document, "document_max_chars", 10000) or 10000), 1)

    def classify_memory_payload(
        self,
        *,
        decision_source: str,
        payload_text: str,
        trigger_source: str = "",
        op: str = "write",
    ) -> MemoryPayloadClassification:
        normalized_source = "user" if str(decision_source or "").strip().lower() == "user" else "self"
        normalized_payload = str(payload_text or "").strip()
        normalized_trigger = str(trigger_source or "").strip().lower()
        if not normalized_payload:
            return MemoryPayloadClassification(
                decision_source=normalized_source,
                memory_kind="empty",
                enqueue=False,
                replace_mode="ignore",
                reason="empty_payload",
                normalized_payload_text="",
            )
        if normalized_source == "self" and self._should_ignore_self_memory(
            payload_text=normalized_payload,
            trigger_source=normalized_trigger,
        ):
            return MemoryPayloadClassification(
                decision_source="self",
                memory_kind="self_ignored",
                enqueue=False,
                replace_mode="ignore",
                reason="self_memory_ignored",
                normalized_payload_text=normalized_payload,
            )

        memory_kind = self._classify_kind(normalized_payload)
        if normalized_source == "self" and self._is_task_local_instruction(normalized_payload):
            memory_kind = "self_general"
        promoted_source = normalized_source
        if normalized_source == "self" and memory_kind.startswith("user_"):
            promoted_source = "user"
        elif normalized_source == "user" and memory_kind.startswith("self_"):
            memory_kind = "user_general"

        return MemoryPayloadClassification(
            decision_source=promoted_source,
            memory_kind=memory_kind,
            enqueue=True,
            replace_mode=self._replace_mode(memory_kind=memory_kind, op=op),
            reason="classified",
            normalized_payload_text=normalized_payload,
        )

    def should_enqueue_autonomous_review(
        self,
        *,
        session_key: str,
        turn_id: str,
        user_messages: list[str],
        assistant_text: str,
    ) -> AutonomousReviewDecision:
        normalized_user_messages = [str(item or "").strip() for item in list(user_messages or []) if str(item or "").strip()]
        normalized_assistant = str(assistant_text or "").strip()
        parts = list(normalized_user_messages)
        if normalized_assistant:
            parts.append(normalized_assistant)
        payload_text = "\n".join(parts).strip()
        if not payload_text:
            return AutonomousReviewDecision(
                enqueue=False,
                reason="empty_turn",
                decision_source="self",
                memory_kind="empty",
                payload_text="",
                sampled=False,
            )

        direct_user = self.classify_memory_payload(
            decision_source="self",
            payload_text="\n".join(normalized_user_messages).strip(),
            trigger_source=f"autonomous_review:{str(turn_id or '').strip()}",
        )
        if direct_user.enqueue and direct_user.decision_source == "user":
            return AutonomousReviewDecision(
                enqueue=True,
                reason="direct_user_memory",
                decision_source=direct_user.decision_source,
                memory_kind=direct_user.memory_kind,
                payload_text=payload_text,
                sampled=False,
            )

        if len(payload_text) < 24:
            return AutonomousReviewDecision(
                enqueue=False,
                reason="turn_too_small",
                decision_source="self",
                memory_kind="self_general",
                payload_text=payload_text,
                sampled=False,
            )

        sampled = self._sample_review_window(session_key=session_key, turn_id=turn_id)
        if not sampled:
            return AutonomousReviewDecision(
                enqueue=False,
                reason="outside_review_window",
                decision_source="self",
                memory_kind="self_general",
                payload_text=payload_text,
                sampled=False,
            )

        classification = self.classify_memory_payload(
            decision_source="self",
            payload_text=payload_text,
            trigger_source=f"autonomous_review:{str(turn_id or '').strip()}",
        )
        return AutonomousReviewDecision(
            enqueue=classification.enqueue,
            reason="sampled_review_window",
            decision_source=classification.decision_source,
            memory_kind=classification.memory_kind,
            payload_text=classification.normalized_payload_text,
            sampled=True,
        )

    def prioritize_existing_entries(self, entries: list[MemoryEntry]) -> list[MemoryEntry]:
        ranked: list[tuple[int, int, MemoryEntry]] = []
        for index, entry in enumerate(list(entries or [])):
            classification = self.classify_memory_payload(
                decision_source=str(entry.source or "").strip(),
                payload_text=str(entry.summary or "").strip(),
                trigger_source="memory_snapshot",
            )
            score = self._priority_score(classification.memory_kind)
            if not classification.enqueue:
                score = -100
            ranked.append((score, index, entry))
        ranked.sort(key=lambda item: (-item[0], item[1]))
        return [entry for _score, _index, entry in ranked]

    def prompt_strategy_snapshot(self, *, batch: MemoryBatch, snapshot_text: str) -> dict[str, Any]:
        existing_entries = parse_memory_document(snapshot_text)
        prioritized_existing = self.prioritize_existing_entries(existing_entries)
        incoming = [
            asdict(
                self.classify_memory_payload(
                    decision_source=item.decision_source,
                    payload_text=item.payload_text,
                    trigger_source=item.trigger_source,
                    op=item.op,
                )
            )
            for item in batch.items
        ]
        return {
            "review_sampling_window": f"1/{self._review_interval_turns} hashed turns unless direct user memory is detected",
            "direct_user_upgrade": [
                "stable identity",
                "stable preference/default",
                "durable workflow constraint",
            ],
            "ignored_self_memory": [
                "session boundary flush",
                "pre-compression payloads",
                "transient runtime or pause state",
            ],
            "near_limit_retention_order": [
                "user identity and hard constraints",
                "user preferences/defaults and long-lived project facts",
                "self workflow rules that still generalize",
                "self transient notes are pruned first",
            ],
            "replace_vs_delete_boundary": {
                "replace_existing": "same user slot or durable rule changed in place",
                "delete_existing": "entry is obsolete without a replacement value",
            },
            "document_max_chars": self._document_max_chars,
            "incoming_classifications": incoming,
            "prioritized_existing_summaries": [str(entry.summary or "") for entry in prioritized_existing[:8]],
        }

    def _classify_kind(self, payload_text: str) -> str:
        if self._is_task_local_instruction(payload_text):
            return "self_general"
        if self._matches_any(self._USER_IDENTITY_PATTERNS, payload_text):
            return "user_identity"
        if self._matches_any(self._USER_PREFERENCE_PATTERNS, payload_text):
            return "user_preference"
        if self._matches_any(self._USER_CONSTRAINT_PATTERNS, payload_text):
            return "user_constraint"
        if self._matches_any(self._SELF_TRANSIENT_PATTERNS, payload_text):
            return "self_transient"
        if self._matches_any(self._SELF_RULE_PATTERNS, payload_text):
            return "self_rule"
        return "self_general"

    def _should_ignore_self_memory(self, *, payload_text: str, trigger_source: str) -> bool:
        if str(trigger_source or "").strip().lower() in self._IGNORE_TRIGGER_SOURCES:
            return True
        stripped = str(payload_text or "").strip()
        if not stripped:
            return True
        if stripped.startswith("[") and '"role"' in stripped and '"content"' in stripped:
            return True
        return self._matches_any(self._SELF_TRANSIENT_PATTERNS, stripped)

    def _is_task_local_instruction(self, payload_text: str) -> bool:
        stripped = str(payload_text or "").strip()
        if not stripped:
            return False
        if self._matches_any(self._TASK_LOCAL_PATTERNS, stripped):
            return True
        # Temporary test-deferral or patch-routing instructions are task-local even if they
        # contain "don't" or "please use", so they must not become durable user memory.
        if re.search(r"\b(?:run tests?|use)\b", stripped, re.IGNORECASE) and re.search(
            r"\b(?:later|yet|attached diff|this change|this command|this one)\b",
            stripped,
            re.IGNORECASE,
        ):
            return True
        if re.search(r"\b(?:debugging|working on)\b", stripped, re.IGNORECASE) and re.search(
            r"\b(?:right now|today|currently|this)\b",
            stripped,
            re.IGNORECASE,
        ):
            return True
        return False

    def _replace_mode(self, *, memory_kind: str, op: str) -> str:
        if str(op or "").strip().lower() == "delete":
            return "delete_existing"
        if memory_kind in {"user_identity", "user_preference", "user_constraint", "self_rule"}:
            return "replace_existing"
        if memory_kind == "self_ignored":
            return "ignore"
        return "append_or_merge"

    @staticmethod
    def _priority_score(memory_kind: str) -> int:
        score_map = {
            "user_identity": 500,
            "user_constraint": 450,
            "user_preference": 400,
            "user_general": 320,
            "self_rule": 220,
            "self_general": 120,
            "self_transient": -20,
            "self_ignored": -100,
            "empty": -200,
        }
        return int(score_map.get(memory_kind, 100))

    def _sample_review_window(self, *, session_key: str, turn_id: str) -> bool:
        sample_key = f"{str(session_key or '').strip()}:{str(turn_id or '').strip()}".encode("utf-8")
        digest = hashlib.sha1(sample_key).hexdigest()
        return int(digest[:8], 16) % self._review_interval_turns == 0

    @staticmethod
    def _matches_any(patterns: tuple[Any, ...], payload_text: str) -> bool:
        return any(pattern.search(str(payload_text or "")) for pattern in patterns)


@dataclass(slots=True)
class _MemoryToolSession:
    snapshot_text: str
    notes_dir: Path
    staged_document: object = _STAGED_DOCUMENT_UNSET
    staged_notes: dict[str, str] = field(default_factory=dict)
    applied_batch: dict[str, Any] | None = None
    apply_batch_count: int = 0

    def read_document(self) -> str:
        if self.staged_document is _STAGED_DOCUMENT_UNSET:
            return str(self.snapshot_text or "")
        return str(self.staged_document or "")

    def read_note(self, ref: str) -> str:
        normalized = str(ref or "").strip()
        if not normalized:
            raise ValueError("note ref is required")
        if normalized in self.staged_notes:
            return self.staged_notes[normalized]
        path = self.notes_dir / note_file_name(normalized)
        if not path.exists():
            raise FileNotFoundError(f"memory note not found: {normalized}")
        return path.read_text(encoding="utf-8")

    def write_document(self, content: str) -> str:
        self.staged_document = str(content or "")
        return "document staged"

    def write_note(self, ref: str, content: str) -> str:
        normalized = str(ref or "").strip()
        note_file_name(normalized)
        self.staged_notes[normalized] = str(content or "")
        return f"note staged: {normalized}"

    def apply_batch(
        self,
        *,
        adds: list[dict[str, Any]] | None = None,
        rewrites: list[dict[str, Any]] | None = None,
        deletes: list[str] | None = None,
        note_upserts: dict[str, str] | None = None,
        noop_reason: str | None = None,
    ) -> dict[str, Any]:
        errors: dict[str, str] = {}
        normalized_adds: list[dict[str, str]] = []
        normalized_rewrites: list[dict[str, str]] = []
        normalized_deletes: list[str] = []
        normalized_note_upserts: dict[str, str] = {}
        normalized_noop_reason = str(noop_reason or "").strip()

        raw_adds = list(adds or [])
        if not isinstance(raw_adds, list):
            errors["adds"] = "adds must be a list"
        else:
            for index, item in enumerate(raw_adds):
                if not isinstance(item, dict):
                    errors[f"adds[{index}]"] = "add item must be an object"
                    continue
                content = str(item.get("content") or "").strip()
                decision_source = str(item.get("decision_source") or "").strip().lower()
                if not content:
                    errors[f"adds[{index}].content"] = "content must not be empty"
                if decision_source not in {"user", "self"}:
                    errors[f"adds[{index}].decision_source"] = "decision_source must be user or self"
                if content and decision_source in {"user", "self"}:
                    normalized_adds.append(
                        {
                            "content": content,
                            "decision_source": decision_source,
                        }
                    )

        raw_rewrites = list(rewrites or [])
        if not isinstance(raw_rewrites, list):
            errors["rewrites"] = "rewrites must be a list"
        else:
            for index, item in enumerate(raw_rewrites):
                if not isinstance(item, dict):
                    errors[f"rewrites[{index}]"] = "rewrite item must be an object"
                    continue
                memory_id = str(item.get("id") or "").strip()
                content = str(item.get("content") or "").strip()
                if not memory_id:
                    errors[f"rewrites[{index}].id"] = "id must not be empty"
                if not content:
                    errors[f"rewrites[{index}].content"] = "content must not be empty"
                if memory_id and content:
                    normalized_rewrites.append(
                        {
                            "id": memory_id,
                            "content": content,
                        }
                    )

        raw_deletes = list(deletes or [])
        if not isinstance(raw_deletes, list):
            errors["deletes"] = "deletes must be a list"
        else:
            for index, item in enumerate(raw_deletes):
                memory_id = str(item or "").strip()
                if not memory_id:
                    errors[f"deletes[{index}]"] = "delete id must not be empty"
                    continue
                normalized_deletes.append(memory_id)

        raw_note_upserts = dict(note_upserts or {})
        if not isinstance(raw_note_upserts, dict):
            errors["note_upserts"] = "note_upserts must be an object"
        else:
            for raw_ref, raw_body in raw_note_upserts.items():
                ref = str(raw_ref or "").strip()
                body = str(raw_body or "")
                try:
                    note_file_name(ref)
                except Exception as exc:
                    errors[f"note_upserts.{ref or '<empty>'}"] = str(exc or "invalid note ref").strip()
                    continue
                normalized_note_upserts[ref] = body

        has_mutation = bool(normalized_adds or normalized_rewrites or normalized_deletes or normalized_note_upserts)
        if normalized_noop_reason and has_mutation:
            errors["noop_reason"] = "noop_reason may not be combined with add, rewrite, delete, or note_upsert"
        if not normalized_noop_reason and not has_mutation:
            errors["batch"] = "at least one add, rewrite, delete, note_upsert, or noop_reason is required"

        if errors:
            return {"ok": False, "errors": errors}

        self.apply_batch_count += 1
        self.applied_batch = {
            "adds": normalized_adds,
            "rewrites": normalized_rewrites,
            "deletes": normalized_deletes,
            "note_upserts": normalized_note_upserts,
            "noop_reason": normalized_noop_reason,
        }
        return {"ok": True, "status": "batch_staged"}


@dataclass(slots=True)
class _MemoryAttemptResult:
    session: _MemoryToolSession
    usage: dict[str, int]
    final_text: str
    request_artifacts: list[dict[str, Any]] = field(default_factory=list)


@dataclass(slots=True)
class _MemoryValidatedWrite:
    document_text: str
    note_writes: dict[str, str]
    note_refs_written: list[str]
    memory_chars_after: int
    document_preview: str
    write_mode: str = ""
    noop_reason: str = ""


class _MemoryAgentValidationError(ValueError):
    pass


class _MemoryAgentRuntimeError(RuntimeError):
    pass


class MemoryManager:
    def __init__(self, workspace: Path, config: Any, *, read_only_init: bool = False):
        self.workspace = Path(workspace)
        self.config = config
        self._read_only_init = bool(read_only_init)
        self.runtime_dir = self.workspace / ".g3ku"
        self.request_artifacts_dir = self.runtime_dir / "memory-requests"
        self.mem_dir = self.workspace / "memory"
        self.memory_file = self.workspace / str(config.document.memory_file)
        self.notes_dir = self.workspace / str(config.document.notes_dir)
        self.queue_file = self.workspace / str(config.queue.queue_file)
        self.ops_file = self.workspace / str(config.queue.ops_file)
        self.review_state_file = self.mem_dir / "review_state.json"
        self._io_lock = threading.RLock()
        self._worker_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._strategy = MemoryStrategyV2(config)
        self._catalog_bridge: MemoryCatalogBridge | None = None
        self.store = None
        if self._read_only_init:
            return
        preserved_document = self.memory_file.read_text(encoding="utf-8") if self.memory_file.exists() else ""
        preserved_notes = self._read_note_bodies()
        self._catalog_bridge = MemoryCatalogBridge(self.workspace, config)
        self.store = getattr(self._catalog_bridge, "store", None)
        self._ensure_layout()
        self._restore_preserved_memory_state(
            document_text=preserved_document,
            note_bodies=preserved_notes,
        )

    def _ensure_layout(self) -> None:
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        self.request_artifacts_dir.mkdir(parents=True, exist_ok=True)
        self.mem_dir.mkdir(parents=True, exist_ok=True)
        self.notes_dir.mkdir(parents=True, exist_ok=True)
        if not self.memory_file.exists():
            self.memory_file.write_text("", encoding="utf-8")
        else:
            current = self.memory_file.read_text(encoding="utf-8").strip()
            if current == "# Managed Memory Mirror":
                self.memory_file.write_text("", encoding="utf-8")
        if not self.queue_file.exists():
            self.queue_file.write_text("", encoding="utf-8")
        if not self.ops_file.exists():
            self.ops_file.write_text("", encoding="utf-8")
        if not self.review_state_file.exists():
            self.review_state_file.write_text('{"sessions": {}}\n', encoding="utf-8")

    def _read_note_bodies(self) -> dict[str, str]:
        if not self.notes_dir.exists():
            return {}
        note_bodies: dict[str, str] = {}
        for path in self.notes_dir.glob("*.md"):
            try:
                note_bodies[path.stem] = path.read_text(encoding="utf-8")
            except OSError:
                continue
        return note_bodies

    def _restore_preserved_memory_state(self, *, document_text: str, note_bodies: dict[str, str]) -> None:
        preserved_document = str(document_text or "")
        if preserved_document.strip():
            current_document = self.memory_file.read_text(encoding="utf-8") if self.memory_file.exists() else ""
            if not current_document.strip():
                self.memory_file.write_text(preserved_document, encoding="utf-8")
        for ref, body in dict(note_bodies or {}).items():
            note_path = self.notes_dir / note_file_name(ref)
            current_body = note_path.read_text(encoding="utf-8") if note_path.exists() else ""
            if note_path.exists() and current_body.strip():
                continue
            note_path.parent.mkdir(parents=True, exist_ok=True)
            note_path.write_text(str(body or ""), encoding="utf-8")

    def start(self) -> None:
        if self._worker_thread is not None and self._worker_thread.is_alive():
            return
        self._stop_event.clear()
        self._worker_thread = threading.Thread(
            target=self._worker_loop,
            name="g3ku-memory-worker",
            daemon=True,
        )
        self._worker_thread.start()

    def snapshot_text(self, **_: Any) -> str:
        if not self.memory_file.exists():
            return ""
        return self.memory_file.read_text(encoding="utf-8").strip()

    async def _append_queue_request(self, request: MemoryQueueRequest) -> None:
        rows = self._read_queue_requests()
        rows.append(self._normalize_queue_request(request))
        self._write_queue_requests(rows)

    async def list_queue(self, *, limit: int = 20, offset: int = 0) -> list[dict[str, Any]]:
        rows = self._read_queue_requests()
        start = max(int(offset or 0), 0)
        end = start + max(int(limit or 0), 0)
        return [asdict(item) for item in rows[start:end]]

    async def list_queue_page(self, *, limit: int = 20, offset: int = 0) -> dict[str, Any]:
        rows = self._read_queue_requests()
        start = max(int(offset or 0), 0)
        page_size = max(int(limit or 0), 0)
        end = start + page_size
        return {
            "items": [asdict(item) for item in rows[start:end]],
            "total": len(rows),
            "has_more": end < len(rows),
        }

    async def list_processed_page(self, *, limit: int = 20, offset: int = 0) -> dict[str, Any]:
        rows = self._read_processed_batches()
        ordered = list(reversed(rows))
        start = max(int(offset or 0), 0)
        page_size = max(int(limit or 0), 0)
        end = start + page_size
        return {
            "items": ordered[start:end],
            "total": len(ordered),
            "has_more": end < len(ordered),
        }

    def doctor_report(
        self,
        *,
        now_iso: str | None = None,
        stuck_after_seconds: int = 300,
    ) -> dict[str, Any]:
        snapshot_text = self.snapshot_text()
        summary_limit = int(getattr(self.config.document, "summary_max_chars", 250) or 250)
        document_limit = int(getattr(self.config.document, "document_max_chars", 10000) or 10000)
        document_error = ""
        document_valid = True
        try:
            validate_memory_document(
                snapshot_text,
                summary_max_chars=summary_limit,
                document_max_chars=document_limit,
            )
        except Exception as exc:
            document_valid = False
            document_error = str(exc or "invalid memory document").strip()

        note_refs = self._collect_note_refs(snapshot_text)
        note_files = self._note_file_refs()
        missing_note_refs = sorted(ref for ref in note_refs if ref not in note_files)
        orphan_notes = sorted(ref for ref in note_files if ref not in note_refs)
        queue_rows, queue_parse_errors = self._read_queue_requests_with_diagnostics()
        stuck_processing_head = self._stuck_processing_head(
            queue_rows=queue_rows,
            now_iso=now_iso,
            stuck_after_seconds=stuck_after_seconds,
        )
        checks = [
            {
                "name": "memory_document",
                "ok": document_valid,
                "detail": "valid"
                if document_valid
                else (document_error or "memory document contains invalid blocks"),
            },
            {
                "name": "missing_note_refs",
                "ok": not missing_note_refs,
                "detail": "none" if not missing_note_refs else ", ".join(missing_note_refs),
            },
            {
                "name": "orphan_notes",
                "ok": not orphan_notes,
                "detail": "none" if not orphan_notes else ", ".join(orphan_notes),
            },
            {
                "name": "queue_parse_errors",
                "ok": not queue_parse_errors,
                "detail": "none"
                if not queue_parse_errors
                else ", ".join(
                    f"line {int(item.get('line', 0) or 0)}: {str(item.get('error') or 'unknown parse error')}"
                    for item in queue_parse_errors
                ),
            },
            {
                "name": "stuck_processing_head",
                "ok": stuck_processing_head is None,
                "detail": "none"
                if stuck_processing_head is None
                else f"{stuck_processing_head['request_id']} age={stuck_processing_head['age_seconds']}s",
            },
        ]
        return {
            "ok": all(bool(item["ok"]) for item in checks),
            "status": "ok" if all(bool(item["ok"]) for item in checks) else "issues_found",
            "checks": checks,
            "memory_document_valid": document_valid,
            "memory_document_error": document_error,
            "memory_note_refs": note_refs,
            "missing_note_refs": missing_note_refs,
            "orphan_notes": orphan_notes,
            "queue_depth": len(queue_rows),
            "queue_head": asdict(queue_rows[0]) if queue_rows else None,
            "queue_parse_errors": queue_parse_errors,
            "stuck_processing_head": stuck_processing_head,
        }

    def reconcile_notes(self, *, delete_orphans: bool = False) -> dict[str, Any]:
        snapshot_text = self.snapshot_text()
        note_refs = self._collect_note_refs(snapshot_text)
        note_files = self._note_file_refs()
        missing_note_refs = sorted(ref for ref in note_refs if ref not in note_files)
        orphan_notes = sorted(ref for ref in note_files if ref not in note_refs)
        created_notes: list[str] = []
        deleted_notes: list[str] = []

        for ref in missing_note_refs:
            note_path = self.notes_dir / note_file_name(ref)
            note_path.parent.mkdir(parents=True, exist_ok=True)
            note_path.write_text(
                self._normalize_note_text(
                    f"Placeholder note created by reconcile-notes for ref:{ref}."
                ),
                encoding="utf-8",
            )
            created_notes.append(ref)

        if delete_orphans:
            for ref in orphan_notes:
                note_path = self.notes_dir / note_file_name(ref)
                if not note_path.exists():
                    continue
                try:
                    note_path.unlink()
                except OSError:
                    continue
                deleted_notes.append(ref)

        return {
            "ok": True,
            "status": "reconciled",
            "memory_note_refs": note_refs,
            "created_missing_notes": created_notes,
            "created_missing_count": len(created_notes),
            "orphan_notes_detected": orphan_notes,
            "deleted_orphan_notes": deleted_notes,
            "deleted_orphan_count": len(deleted_notes),
            "delete_orphans": bool(delete_orphans),
        }

    def import_legacy(
        self,
        legacy_path: Path | str,
        *,
        apply: bool = False,
    ) -> dict[str, Any]:
        path = Path(legacy_path)
        rows = self._load_legacy_rows(path)
        document_text, note_writes = self._build_legacy_import_payload(rows)
        note_refs = self._collect_note_refs(document_text)
        report = {
            "ok": True,
            "status": "dry_run" if not apply else "applied",
            "legacy_path": str(path),
            "entry_count": len(parse_memory_document(document_text)),
            "note_ref_count": len(note_refs),
            "note_refs": note_refs,
            "document_preview": self._document_preview(document_text),
            "apply": bool(apply),
        }
        if not apply:
            return report

        if self.snapshot_text().strip():
            raise ValueError("memory/MEMORY.md is non-empty; import-legacy only applies to an empty target")
        if self._read_queue_requests():
            raise ValueError("memory queue is not empty; flush or reconcile the queue before import-legacy --apply")

        validated = _MemoryValidatedWrite(
            document_text=document_text,
            note_writes={ref: self._normalize_note_text(body) for ref, body in note_writes.items()},
            note_refs_written=sorted(note_writes.keys()),
            memory_chars_after=len(document_text),
            document_preview=self._document_preview(document_text),
        )
        self._commit_validated_write(validated)
        report["note_refs_written"] = list(validated.note_refs_written)
        return report

    def legacy_cleanup_report(self, *, apply: bool = False) -> dict[str, Any]:
        existing_paths: list[str] = []
        data_bearing_paths: list[str] = []
        resolved_paths: list[Path] = []

        for relative_path in _LEGACY_CLEANUP_RELATIVE_PATHS:
            path = self.workspace / relative_path
            if not path.exists():
                continue
            existing_paths.append(relative_path)
            resolved_paths.append(path)
            if path.is_dir():
                try:
                    has_children = any(path.iterdir())
                except OSError:
                    has_children = True
                if has_children:
                    data_bearing_paths.append(relative_path)
                continue
            try:
                if path.read_text(encoding="utf-8").strip():
                    data_bearing_paths.append(relative_path)
            except OSError:
                data_bearing_paths.append(relative_path)

        report = {
            "ok": True,
            "status": "dry_run" if not apply else "applied",
            "apply": bool(apply),
            "existing_paths": existing_paths,
            "data_bearing_paths": data_bearing_paths,
            "deleted_paths": [],
            "queue_depth": len(self._read_queue_requests()),
            "memory_document_empty": not bool(self.snapshot_text().strip()),
        }
        if not apply:
            return report

        if self._read_queue_requests():
            raise ValueError("memory queue is not empty; flush or reconcile the queue before cleanup-legacy --apply")
        if not self.snapshot_text().strip() and data_bearing_paths:
            raise ValueError(
                "legacy memory artifacts still contain data while memory/MEMORY.md is empty; "
                "run import-legacy first or delete them only after manual review"
            )

        deleted_paths: list[str] = []
        for relative_path, path in zip(existing_paths, resolved_paths):
            try:
                if path.is_dir():
                    shutil.rmtree(path)
                else:
                    path.unlink()
            except FileNotFoundError:
                continue
            deleted_paths.append(relative_path)
        report["deleted_paths"] = deleted_paths
        return report

    async def collect_due_batch(self, *, now_iso: str) -> MemoryBatch | None:
        rows = self._read_queue_requests()
        if not rows:
            return None
        head = rows[0]
        if str(head.status or "pending").strip() == "processing":
            retry_after = self._parse_optional_iso(head.retry_after)
            if retry_after is not None and retry_after > datetime.fromisoformat(str(now_iso).strip()):
                return None
            items: list[MemoryQueueRequest] = []
            for row in rows:
                if str(row.status or "").strip() != "processing" or row.op != head.op:
                    break
                items.append(row)
            return MemoryBatch(op=head.op, items=items) if items else None

        max_chars = int(getattr(self.config.queue, "batch_max_chars", 50000) or 50000)
        current_chars = 0
        hit_char_boundary = False
        items: list[MemoryQueueRequest] = []
        for row in rows:
            if str(row.status or "pending").strip() != "pending":
                break
            if row.op != head.op:
                break
            next_chars = current_chars + len(str(row.payload_text or ""))
            if items and next_chars > max_chars:
                hit_char_boundary = True
                break
            current_chars = next_chars
            items.append(row)

        if not items:
            return None
        waited_seconds = self._seconds_since(head.created_at, now_iso)
        if (
            not hit_char_boundary
            and current_chars < max_chars
            and waited_seconds < int(getattr(self.config.queue, "max_wait_seconds", 3) or 3)
        ):
            return None
        return MemoryBatch(op=head.op, items=items)

    async def enqueue_write_request(
        self,
        *,
        session_key: str,
        decision_source: str,
        payload_text: str,
        trigger_source: str,
    ) -> dict[str, Any]:
        classification = self.classify_memory_payload(
            decision_source=decision_source,
            payload_text=payload_text,
            trigger_source=trigger_source,
            op="write",
        )
        if not classification.enqueue:
            return {
                "ok": True,
                "status": "ignored",
                "reason": classification.reason,
                "decision_source": classification.decision_source,
            }
        request = MemoryQueueRequest(
            op="write",
            decision_source=self._normalize_decision_source(classification.decision_source),
            payload_text=classification.normalized_payload_text,
            created_at=self._now_iso(),
            session_key=str(session_key or "").strip(),
            trigger_source=str(trigger_source or "").strip(),
            request_id=self._request_id("write"),
        )
        await self._append_queue_request(request)
        return {"ok": True, "request_id": request.request_id, "status": "queued"}

    async def enqueue_delete_request(
        self,
        *,
        session_key: str,
        decision_source: str,
        payload_text: str,
        trigger_source: str,
    ) -> dict[str, Any]:
        classification = self.classify_memory_payload(
            decision_source=decision_source,
            payload_text=payload_text,
            trigger_source=trigger_source,
            op="delete",
        )
        if not classification.enqueue:
            return {
                "ok": True,
                "status": "ignored",
                "reason": classification.reason,
                "decision_source": classification.decision_source,
            }
        request = MemoryQueueRequest(
            op="delete",
            decision_source=self._normalize_decision_source(classification.decision_source),
            payload_text=classification.normalized_payload_text,
            created_at=self._now_iso(),
            session_key=str(session_key or "").strip(),
            trigger_source=str(trigger_source or "").strip(),
            request_id=self._request_id("delete"),
        )
        await self._append_queue_request(request)
        return {"ok": True, "request_id": request.request_id, "status": "queued"}

    async def enqueue_autonomous_review(
        self,
        *,
        session_key: str,
        channel: str,
        chat_id: str,
        user_messages: list[str],
        assistant_text: str,
        turn_id: str,
    ) -> dict[str, Any]:
        decision = self.should_enqueue_autonomous_review(
            session_key=session_key,
            turn_id=turn_id,
            user_messages=user_messages,
            assistant_text=assistant_text,
        )
        if not decision.enqueue:
            return {
                "ok": True,
                "status": "ignored",
                "reason": decision.reason,
                "decision_source": decision.decision_source,
            }
        return await self.enqueue_write_request(
            session_key=session_key,
            decision_source=decision.decision_source,
            payload_text=decision.payload_text,
            trigger_source=f"autonomous_review:{str(turn_id or '').strip()}",
        )

    async def enqueue_pre_compression_flush(
        self,
        *,
        session_key: str,
        channel: str,
        chat_id: str,
        context_messages: list[dict[str, Any]],
    ) -> dict[str, Any]:
        payload_text = json.dumps(list(context_messages or []), ensure_ascii=False)
        return await self.enqueue_write_request(
            session_key=session_key,
            decision_source="self",
            payload_text=payload_text,
            trigger_source="pre_compression_flush",
        )

    async def enqueue_session_boundary_flush(
        self,
        *,
        session_key: str,
        channel: str,
        chat_id: str,
        trigger_source: str,
    ) -> dict[str, Any]:
        return await self.enqueue_write_request(
            session_key=session_key,
            decision_source="self",
            payload_text="session boundary flush",
            trigger_source=str(trigger_source or "").strip() or "session_boundary_flush",
        )

    async def run_due_batch_once(self, *, now_iso: str | None = None) -> dict[str, Any]:
        effective_now = str(now_iso or self._now_iso()).strip()
        batch = await self.collect_due_batch(now_iso=effective_now)
        if batch is None:
            return {"ok": True, "status": "idle", "processed": 0}
        if not batch.items:
            return {"ok": True, "status": "idle", "processed": 0}
        if str(batch.items[0].status or "pending").strip() != "processing":
            self._mark_batch_processing(batch.items, effective_now)

        try:
            runtime_config, _revision, _changed = get_runtime_config()
        except Exception as exc:
            return self._mark_batch_error(batch, effective_now, str(exc or "memory runtime config unavailable"))

        model_chain = self._memory_model_chain(runtime_config)
        if not model_chain:
            return self._mark_batch_blocked(batch, effective_now, "memory role not configured")

        try:
            result = await self._run_memory_agent_batch(
                batch=batch,
                runtime_config=runtime_config,
                model_chain=model_chain,
            )
        except Exception as exc:
            logger.warning("memory batch failed: {}", exc)
            return self._mark_batch_error(batch, effective_now, str(exc or "memory batch failed"))

        self._commit_validated_write(result["validated"])
        self._drop_request_ids({item.request_id for item in batch.items})
        processed_at = self._now_iso()
        processed_payload = {
            "batch_id": self._request_id(batch.op),
            "op": batch.op,
            "processed_at": processed_at,
            "request_ids": [item.request_id for item in batch.items],
            "request_count": len(batch.items),
            "decision_sources": [item.decision_source for item in batch.items],
            "payload_texts": [item.payload_text for item in batch.items],
            "usage": {
                "input_tokens": int(result["usage"]["input_tokens"]),
                "output_tokens": int(result["usage"]["output_tokens"]),
                "cache_read_tokens": int(result["usage"]["cache_read_tokens"]),
            },
            "model_chain": list(model_chain),
            "attempt_count": int(result["attempt_count"]),
            "memory_chars_after": int(result["validated"].memory_chars_after),
            "note_refs_written": list(result["validated"].note_refs_written),
            "document_preview": result["validated"].document_preview,
        }
        self._append_ops_payload(processed_payload)
        return {
            "ok": True,
            "status": "applied",
            "op": batch.op,
            "processed": len(batch.items),
            "request_ids": [item.request_id for item in batch.items],
            "attempt_count": int(result["attempt_count"]),
            "usage": dict(processed_payload["usage"]),
            "model_chain": list(model_chain),
            "processed_at": processed_at,
        }

    def load_note(self, ref: str) -> str:
        path = self.notes_dir / note_file_name(ref)
        if not path.exists():
            raise FileNotFoundError(f"memory note not found: {ref}")
        return path.read_text(encoding="utf-8")

    async def sync_catalog(self, service: Any) -> Any:
        return await self._require_catalog_bridge().sync_catalog(service)

    async def ensure_catalog_bootstrap(self, service: Any) -> Any:
        return await self._require_catalog_bridge().ensure_catalog_bootstrap(service)

    async def semantic_search_context_records(self, **kwargs: Any) -> Any:
        return await self._require_catalog_bridge().semantic_search_context_records(**kwargs)

    async def list_context_records(self, **kwargs: Any) -> Any:
        return await self._require_catalog_bridge().list_context_records(**kwargs)

    async def put_context_record(self, **kwargs: Any) -> Any:
        return await self._require_catalog_bridge().put_context_record(**kwargs)

    async def delete_context_record(self, **kwargs: Any) -> Any:
        return await self._require_catalog_bridge().delete_context_record(**kwargs)

    def close(self) -> None:
        self._stop_event.set()
        worker = self._worker_thread
        if worker is not None and worker.is_alive():
            worker.join(timeout=2.0)
        self._worker_thread = None
        if self._catalog_bridge is not None:
            self._catalog_bridge.close()

    def _require_catalog_bridge(self) -> MemoryCatalogBridge:
        bridge = self._catalog_bridge
        if bridge is None:
            raise RuntimeError("memory catalog bridge is unavailable in read-only initialization mode")
        return bridge

    def _memory_worker_lock_path(self) -> Path:
        return self.mem_dir / ".worker.lock"

    def _memory_worker_lock_metadata(self) -> dict[str, object]:
        return {
            "pid": int(os.getpid()),
            "thread": str(threading.current_thread().name or "").strip() or "unknown",
            "workspace": str(self.workspace),
            "kind": "memory_worker",
        }

    def _try_acquire_memory_worker_lease(self) -> Any | None:
        return _try_acquire_file_lock(
            self._memory_worker_lock_path(),
            metadata=self._memory_worker_lock_metadata(),
        )

    @staticmethod
    def _request_id(prefix: str) -> str:
        return f"{prefix}_{uuid.uuid4().hex[:12]}"

    @staticmethod
    def _now_iso() -> str:
        return datetime.now().astimezone().isoformat()

    @staticmethod
    def _parse_optional_iso(value: str | None) -> datetime | None:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            return datetime.fromisoformat(text)
        except Exception:
            return None

    @classmethod
    def _retry_after_iso(cls, now_iso: str, seconds: int) -> str:
        now = datetime.fromisoformat(str(now_iso or "").strip())
        return (now + timedelta(seconds=max(int(seconds or 0), 0))).isoformat()

    @staticmethod
    def _seconds_since(start_iso: str, end_iso: str) -> int:
        start = datetime.fromisoformat(str(start_iso or "").strip())
        end = datetime.fromisoformat(str(end_iso or "").strip())
        return max(0, int((end - start).total_seconds()))

    @staticmethod
    def _normalize_decision_source(value: str) -> str:
        normalized = str(value or "").strip().lower()
        return "user" if normalized == "user" else "self"

    def classify_memory_payload(
        self,
        *,
        decision_source: str,
        payload_text: str,
        trigger_source: str = "",
        op: str = "write",
    ) -> MemoryPayloadClassification:
        return self._strategy.classify_memory_payload(
            decision_source=decision_source,
            payload_text=payload_text,
            trigger_source=trigger_source,
            op=op,
        )

    def should_enqueue_autonomous_review(
        self,
        *,
        session_key: str,
        turn_id: str,
        user_messages: list[str],
        assistant_text: str,
    ) -> AutonomousReviewDecision:
        return self._strategy.should_enqueue_autonomous_review(
            session_key=session_key,
            turn_id=turn_id,
            user_messages=user_messages,
            assistant_text=assistant_text,
        )

    def prioritize_existing_entries(self, entries: list[MemoryEntry]) -> list[MemoryEntry]:
        return self._strategy.prioritize_existing_entries(entries)

    def _worker_loop(self) -> None:
        while not self._stop_event.wait(1.0):
            try:
                asyncio.run(self.run_due_batch_once())
            except Exception:
                continue

    def _normalize_queue_request(self, request: MemoryQueueRequest) -> MemoryQueueRequest:
        return MemoryQueueRequest(
            op=str(request.op or "").strip(),
            decision_source=self._normalize_decision_source(request.decision_source),
            payload_text=str(request.payload_text or "").strip(),
            created_at=str(request.created_at or "").strip() or self._now_iso(),
            request_id=str(request.request_id or "").strip() or self._request_id(str(request.op or "memory")),
            trigger_source=str(request.trigger_source or "").strip(),
            session_key=str(request.session_key or "").strip(),
            status=str(request.status or "pending").strip() or "pending",
            processing_started_at=str(request.processing_started_at or "").strip(),
            last_error_text=str(request.last_error_text or "").strip(),
            last_error_at=str(request.last_error_at or "").strip(),
            retry_after=str(request.retry_after or "").strip(),
        )

    def _read_queue_requests(self) -> list[MemoryQueueRequest]:
        if not self.queue_file.exists():
            return []
        with self._io_lock:
            return [
                self._normalize_queue_request(MemoryQueueRequest(**json.loads(line)))
                for line in self.queue_file.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]

    def _read_queue_requests_with_diagnostics(self) -> tuple[list[MemoryQueueRequest], list[dict[str, Any]]]:
        if not self.queue_file.exists():
            return [], []
        with self._io_lock:
            lines = self.queue_file.read_text(encoding="utf-8").splitlines()
        rows: list[MemoryQueueRequest] = []
        parse_errors: list[dict[str, Any]] = []
        for line_number, line in enumerate(lines, start=1):
            if not str(line or "").strip():
                continue
            try:
                payload = json.loads(line)
                if not isinstance(payload, dict):
                    raise ValueError("queue row must be a JSON object")
                rows.append(self._normalize_queue_request(MemoryQueueRequest(**payload)))
            except Exception as exc:
                error_text = str(exc or exc.__class__.__name__).strip() or exc.__class__.__name__
                parse_errors.append(
                    {
                        "line": line_number,
                        "error": error_text,
                    }
                )
        return rows, parse_errors

    def _write_queue_requests(self, rows: list[MemoryQueueRequest]) -> None:
        with self._io_lock:
            text = "\n".join(json.dumps(asdict(item), ensure_ascii=False) for item in rows)
            self.queue_file.write_text((f"{text}\n" if text else ""), encoding="utf-8")

    def _read_processed_batches(self) -> list[dict[str, Any]]:
        if not self.ops_file.exists():
            return []
        with self._io_lock:
            return [
                json.loads(line)
                for line in self.ops_file.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]

    def _processed_request_ids(self) -> set[str]:
        request_ids: set[str] = set()
        for payload in self._read_processed_batches():
            if not isinstance(payload, dict):
                continue
            for raw_request_id in list(payload.get("request_ids") or []):
                normalized = str(raw_request_id or "").strip()
                if normalized:
                    request_ids.add(normalized)
        return request_ids

    def _drop_processed_queue_requests(self, *, processed_request_ids: set[str] | None = None) -> list[str]:
        known_processed = set(processed_request_ids or set())
        if not known_processed:
            known_processed = self._processed_request_ids()
        if not known_processed:
            return []

        rows = self._read_queue_requests()
        remaining: list[MemoryQueueRequest] = []
        dropped: list[str] = []
        seen: set[str] = set()
        for row in rows:
            if row.request_id in known_processed:
                if row.request_id not in seen:
                    dropped.append(row.request_id)
                    seen.add(row.request_id)
                continue
            remaining.append(row)
        if dropped:
            self._write_queue_requests(remaining)
        return dropped

    def _append_ops_payload(self, payload: dict[str, Any]) -> None:
        line = json.dumps(dict(payload or {}), ensure_ascii=False)
        with self._io_lock:
            with self.ops_file.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")

    def _note_file_refs(self) -> set[str]:
        return {path.stem for path in self.notes_dir.glob("*.md")}

    def _stuck_processing_head(
        self,
        *,
        queue_rows: list[MemoryQueueRequest],
        now_iso: str | None,
        stuck_after_seconds: int,
    ) -> dict[str, Any] | None:
        if not queue_rows:
            return None
        head = queue_rows[0]
        if str(head.status or "").strip() != "processing":
            return None
        effective_now = self._parse_optional_iso(now_iso) or datetime.now().astimezone()
        retry_after = self._parse_optional_iso(head.retry_after)
        if retry_after is not None and retry_after > effective_now:
            return None
        started_at = self._parse_optional_iso(head.processing_started_at) or self._parse_optional_iso(head.created_at)
        if started_at is None:
            return {
                "request_id": head.request_id,
                "op": head.op,
                "age_seconds": None,
                "processing_started_at": head.processing_started_at,
                "retry_after": head.retry_after,
                "last_error_text": head.last_error_text,
            }
        age_seconds = max(0, int((effective_now - started_at).total_seconds()))
        if age_seconds < max(int(stuck_after_seconds or 0), 0):
            return None
        return {
            "request_id": head.request_id,
            "op": head.op,
            "age_seconds": age_seconds,
            "processing_started_at": head.processing_started_at,
            "retry_after": head.retry_after,
            "last_error_text": head.last_error_text,
        }

    def _load_legacy_rows(self, path: Path) -> list[dict[str, Any]]:
        if not path.exists():
            raise FileNotFoundError(f"legacy import file not found: {path}")
        text = path.read_text(encoding="utf-8").strip()
        if not text:
            raise ValueError("legacy import file is empty")
        if path.suffix.lower() == ".jsonl":
            rows = [json.loads(line) for line in text.splitlines() if line.strip()]
        else:
            payload = json.loads(text)
            if isinstance(payload, list):
                rows = payload
            elif isinstance(payload, dict) and isinstance(payload.get("entries"), list):
                rows = payload["entries"]
            else:
                raise ValueError("legacy import payload must be a JSON array, JSONL file, or an object with an entries list")
        normalized = [dict(item) for item in rows if isinstance(item, dict)]
        if not normalized:
            raise ValueError("legacy import payload does not contain any object entries")
        return normalized

    def _build_legacy_import_payload(self, rows: list[dict[str, Any]]) -> tuple[str, dict[str, str]]:
        entries: list[MemoryEntry] = []
        note_writes: dict[str, str] = {}
        allocated_ids: set[str] = set()
        for index, row in enumerate(rows, start=1):
            date_text = str(row.get("date_text") or "").strip()
            source = str(row.get("source") or "").strip().lower()
            summary = str(row.get("summary") or "").strip()
            note_ref = str(row.get("note_ref") or "").strip()
            note_text = str(row.get("note_text") or "").strip()
            if not date_text or not source or not summary:
                raise ValueError(f"legacy import row {index} is missing date_text, source, or summary")
            if source not in {"user", "self"}:
                raise ValueError(f"legacy import row {index} has unsupported source: {source}")
            if note_ref and f"ref:{note_ref}" not in summary:
                summary = f"{summary} ref:{note_ref}"
            memory_id = self._generate_memory_id(allocated_ids)
            allocated_ids.add(memory_id)
            entries.append(
                MemoryEntry(
                    memory_id=memory_id,
                    date_text=date_text,
                    source=source,
                    summary=summary,
                    note_ref=note_ref,
                )
            )
            if note_ref:
                if not note_text and not (self.notes_dir / note_file_name(note_ref)).exists():
                    raise ValueError(f"legacy import row {index} references {note_ref} without note_text")
                if note_text:
                    note_writes[note_ref] = note_text

        document_text = "".join(format_memory_entry(entry) for entry in entries)
        validate_memory_document(
            document_text,
            summary_max_chars=int(getattr(self.config.document, "summary_max_chars", 250) or 250),
            document_max_chars=int(getattr(self.config.document, "document_max_chars", 10000) or 10000),
        )
        note_refs = self._collect_note_refs(document_text)
        missing_refs = sorted(ref for ref in note_refs if ref not in note_writes and ref not in self._note_file_refs())
        if missing_refs:
            raise ValueError(f"legacy import payload is missing note bodies for refs: {', '.join(missing_refs)}")
        return document_text, note_writes

    def _mark_batch_processing(self, items: list[MemoryQueueRequest], now_iso: str) -> None:
        request_ids = {item.request_id for item in items}
        rows = self._read_queue_requests()
        updated: list[MemoryQueueRequest] = []
        for row in rows:
            if row.request_id in request_ids:
                updated.append(
                    row.__class__(
                        **{
                            **asdict(row),
                            "status": "processing",
                            "processing_started_at": row.processing_started_at or now_iso,
                            "last_error_text": "",
                            "last_error_at": "",
                            "retry_after": "",
                        }
                    )
                )
            else:
                updated.append(row)
        self._write_queue_requests(updated)

    def _mark_batch_error(self, batch: MemoryBatch, now_iso: str, error_text: str) -> dict[str, Any]:
        request_ids = {item.request_id for item in batch.items}
        rows = self._read_queue_requests()
        updated: list[MemoryQueueRequest] = []
        retry_after = self._retry_after_iso(now_iso, 30)
        for row in rows:
            if row.request_id in request_ids:
                updated.append(
                    row.__class__(
                        **{
                            **asdict(row),
                            "status": "processing",
                            "processing_started_at": row.processing_started_at or now_iso,
                            "last_error_text": str(error_text or "").strip(),
                            "last_error_at": now_iso,
                            "retry_after": retry_after,
                        }
                    )
                )
            else:
                updated.append(row)
        self._write_queue_requests(updated)
        return {
            "ok": False,
            "status": "error",
            "op": batch.op,
            "processed": 0,
            "request_ids": [item.request_id for item in batch.items],
            "error": str(error_text or "").strip(),
            "retry_after": retry_after,
        }

    def _mark_batch_blocked(self, batch: MemoryBatch, now_iso: str, error_text: str) -> dict[str, Any]:
        payload = self._mark_batch_error(batch, now_iso, error_text)
        payload["status"] = "blocked"
        return payload

    def _drop_request_ids(self, request_ids: set[str]) -> None:
        remaining = [row for row in self._read_queue_requests() if row.request_id not in request_ids]
        self._write_queue_requests(remaining)

    def _memory_model_chain(self, runtime_config: Any) -> list[str]:
        try:
            return [str(item or "").strip() for item in runtime_config.get_role_model_keys("memory") if str(item or "").strip()]
        except Exception:
            return []

    async def _run_memory_agent_batch(
        self,
        *,
        batch: MemoryBatch,
        runtime_config: Any,
        model_chain: list[str],
    ) -> dict[str, Any]:
        before_text = self.snapshot_text()
        total_usage = {"input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0}
        total_attempts = 1 + max(int(getattr(self.config.agent, "repair_attempt_limit", 1) or 0), 0)
        last_error = "memory agent did not stage a document"
        for attempt_index in range(total_attempts):
            repair_reason = last_error if attempt_index > 0 else ""
            attempt = await self._run_memory_agent_attempt(
                batch=batch,
                runtime_config=runtime_config,
                before_text=before_text,
                repair_reason=repair_reason,
            )
            self._merge_usage(total_usage, attempt.usage)
            try:
                validated = self._validate_candidate_state(
                    batch=batch,
                    before_text=before_text,
                    session=attempt.session,
                )
                return {
                    "validated": validated,
                    "usage": total_usage,
                    "attempt_count": attempt_index + 1,
                }
            except _MemoryAgentValidationError as exc:
                last_error = str(exc or "memory agent output invalid").strip()
                continue
        raise _MemoryAgentRuntimeError(last_error)

    async def _run_memory_agent_attempt(
        self,
        *,
        batch: MemoryBatch,
        runtime_config: Any,
        before_text: str,
        repair_reason: str,
        queue_request_ids: list[str],
        model_chain: list[str],
    ) -> _MemoryAttemptResult:
        session = _MemoryToolSession(snapshot_text=before_text, notes_dir=self.notes_dir)
        tools = self._memory_agent_tools(session)
        model = build_chat_model(runtime_config, role="memory").bind_tools(tools)
        messages: list[Any] = [
            SystemMessage(content=self._memory_agent_system_prompt()),
            HumanMessage(
                content=self._memory_agent_user_prompt(
                    batch=batch,
                    snapshot_text=before_text,
                    repair_reason=repair_reason,
                )
            ),
        ]
        usage_total = {"input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0}
        request_artifacts: list[dict[str, Any]] = []
        round_limit = int(runtime_config.get_role_max_iterations("memory") or 8)
        round_limit = max(round_limit, 1)
        final_text = ""
        for _ in range(round_limit):
            request_messages = self._memory_request_messages(messages)
            response = await model.ainvoke(messages)
            self._merge_usage(usage_total, self._extract_usage(response))
            artifact = self._persist_memory_request_artifact(
                phase="agent",
                batch_op=batch.op,
                queue_request_ids=queue_request_ids,
                model_chain=model_chain,
                request_messages=request_messages,
                response=response,
            )
            if artifact is not None:
                request_artifacts.append(artifact)
            final_text = self._response_text(response)
            tool_calls = self._normalize_tool_calls(response)
            if not tool_calls:
                break
            messages.append(AIMessage(content=final_text, tool_calls=tool_calls))
            for tool_call in tool_calls:
                result = await self._execute_memory_tool(tools, tool_call)
                messages.append(
                    ToolMessage(
                        content=json.dumps(result, ensure_ascii=False),
                        tool_call_id=str(tool_call.get("id") or ""),
                        name=str(tool_call.get("name") or ""),
                    )
                )
        return _MemoryAttemptResult(
            session=session,
            usage=usage_total,
            final_text=final_text,
            request_artifacts=request_artifacts,
        )

    def _memory_agent_tools(self, session: _MemoryToolSession):
        @tool("memory_read_note")
        def memory_read_note(ref: str) -> str:
            """Read one existing note by ref without the .md suffix."""

            return session.read_note(ref)

        @tool("memory_apply_batch")
        def memory_apply_batch(
            adds: list[dict[str, Any]] | None = None,
            rewrites: list[dict[str, Any]] | None = None,
            deletes: list[str] | None = None,
            note_upserts: dict[str, str] | None = None,
            noop_reason: str | None = None,
        ) -> dict[str, Any]:
            """Stage one complete memory mutation batch."""

            return session.apply_batch(
                adds=adds,
                rewrites=rewrites,
                deletes=deletes,
                note_upserts=note_upserts,
                noop_reason=noop_reason,
            )

        return [memory_read_note, memory_apply_batch]

    def _memory_agent_system_prompt(self) -> str:
        return load_prompt("memory_agent.md").strip()

    def _memory_agent_user_prompt(
        self,
        *,
        batch: MemoryBatch,
        snapshot_text: str,
        repair_reason: str,
    ) -> str:
        note_index = sorted(path.stem for path in self.notes_dir.glob("*.md"))
        strategy_snapshot = self._strategy.prompt_strategy_snapshot(batch=batch, snapshot_text=snapshot_text)
        requests = [
            {
                "request_id": item.request_id,
                "op": item.op,
                "decision_source": item.decision_source,
                "payload_text": item.payload_text,
                "created_at": item.created_at,
                "trigger_source": item.trigger_source,
                "session_key": item.session_key,
            }
            for item in batch.items
        ]
        repair_block = f"\nPrevious attempt was invalid: {repair_reason}\nRepair it.\n" if repair_reason else ""
        return (
            f"Target MEMORY.md path: {self.memory_file}\n"
            f"Target notes directory: {self.notes_dir}\n"
            f"Existing note refs: {json.dumps(note_index, ensure_ascii=False)}\n"
            f"Current frozen MEMORY.md snapshot:\n{snapshot_text or '(empty)'}\n\n"
            f"Current batch op: {batch.op}\n"
            f"Batch requests (keep FIFO intent):\n{json.dumps(requests, ensure_ascii=False, indent=2)}\n"
            f"Memory strategy v2 snapshot:\n{json.dumps(strategy_snapshot, ensure_ascii=False, indent=2)}\n"
            f"{repair_block}"
        )

    def _memory_agent_system_prompt(self) -> str:
        return load_prompt("memory_agent.md").strip()

    def _memory_assessor_system_prompt(self) -> str:
        return load_prompt("memory_assessor.md").strip()

    @staticmethod
    def _response_text(response: Any) -> str:
        content = getattr(response, "content", "")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "\n".join(str(item) for item in content)
        return str(content or "")

    @staticmethod
    def _normalize_tool_calls(response: Any) -> list[dict[str, Any]]:
        raw_calls = list(getattr(response, "tool_calls", None) or [])
        normalized: list[dict[str, Any]] = []
        for index, raw_call in enumerate(raw_calls):
            if not isinstance(raw_call, dict):
                continue
            name = str(raw_call.get("name") or "").strip()
            if not name and isinstance(raw_call.get("function"), dict):
                name = str(raw_call["function"].get("name") or "").strip()
            raw_args = raw_call.get("args")
            if raw_args is None and "arguments" in raw_call:
                raw_args = raw_call.get("arguments")
            if raw_args is None and isinstance(raw_call.get("function"), dict):
                raw_args = raw_call["function"].get("arguments")
            if isinstance(raw_args, str):
                try:
                    raw_args = json.loads(raw_args)
                except Exception:
                    raw_args = {"value": raw_args}
            if not isinstance(raw_args, dict):
                raw_args = {}
            normalized.append(
                {
                    "id": str(raw_call.get("id") or f"memory-call-{index + 1}"),
                    "name": name,
                    "args": raw_args,
                }
            )
        return normalized

    @staticmethod
    async def _execute_memory_tool(tools: list[Any], tool_call: dict[str, Any]) -> dict[str, Any]:
        name = str(tool_call.get("name") or "").strip()
        args = dict(tool_call.get("args") or {})
        tool_map = {str(getattr(item, "name", "") or ""): item for item in list(tools or [])}
        target = tool_map.get(name)
        if target is None:
            raise _MemoryAgentRuntimeError(f"unknown memory tool: {name}")
        result = target.invoke(args)
        if asyncio.iscoroutine(result):
            result = await result
        return {"ok": True, "tool": name, "result": result}

    @staticmethod
    def _extract_usage(response: Any) -> dict[str, int]:
        total = {"input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0}
        candidates = [
            getattr(response, "usage_metadata", None),
            getattr(response, "usage", None),
            (getattr(response, "response_metadata", None) or {}).get("token_usage")
            if isinstance(getattr(response, "response_metadata", None), dict)
            else None,
            (getattr(response, "response_metadata", None) or {}).get("usage")
            if isinstance(getattr(response, "response_metadata", None), dict)
            else None,
        ]
        for candidate in candidates:
            normalized = normalize_usage_payload(candidate)
            total["input_tokens"] = max(total["input_tokens"], int(normalized.get("input_tokens", 0) or 0))
            total["output_tokens"] = max(total["output_tokens"], int(normalized.get("output_tokens", 0) or 0))
            total["cache_read_tokens"] = max(total["cache_read_tokens"], int(normalized.get("cache_hit_tokens", 0) or 0))
        return total

    @staticmethod
    def _merge_usage(target: dict[str, int], delta: dict[str, int]) -> None:
        for key in ("input_tokens", "output_tokens", "cache_read_tokens"):
            target[key] = int(target.get(key, 0) or 0) + int(delta.get(key, 0) or 0)

    def _validate_candidate_state(
        self,
        *,
        batch: MemoryBatch,
        before_text: str,
        session: _MemoryToolSession,
    ) -> _MemoryValidatedWrite:
        return self._build_validated_write_from_apply_batch(
            before_text=before_text,
            session=session,
            batch=batch,
        )

    @staticmethod
    def _normalize_document_text(text: str) -> str:
        stripped = str(text or "").strip()
        return f"{stripped}\n" if stripped else ""

    @staticmethod
    def _normalize_note_text(text: str) -> str:
        stripped = str(text or "").strip()
        return f"{stripped}\n" if stripped else ""

    @staticmethod
    def _document_preview(document_text: str) -> str:
        text = str(document_text or "").strip()
        if len(text) <= 240:
            return text
        return text[:237].rstrip() + "..."

    def _commit_validated_write(self, validated: _MemoryValidatedWrite) -> None:
        with self._io_lock:
            for ref, body in validated.note_writes.items():
                note_path = self.notes_dir / note_file_name(ref)
                note_path.parent.mkdir(parents=True, exist_ok=True)
                note_path.write_text(body, encoding="utf-8")
            self.memory_file.write_text(validated.document_text, encoding="utf-8")
            self._cleanup_orphan_notes_locked(validated.document_text)

    def _cleanup_orphan_notes_locked(self, document_text: str) -> None:
        active_refs = set(self._collect_note_refs(document_text))
        for path in self.notes_dir.glob("*.md"):
            if path.stem not in active_refs:
                try:
                    path.unlink()
                except OSError:
                    continue

    @staticmethod
    def _collect_note_refs(document_text: str) -> list[str]:
        return sorted({match.group("ref") for match in _NOTE_REF_RE.finditer(str(document_text or ""))})

    def _read_review_state(self) -> dict[str, Any]:
        default_state = {"sessions": {}}
        if not self.review_state_file.exists():
            return default_state
        with self._io_lock:
            try:
                payload = json.loads(self.review_state_file.read_text(encoding="utf-8") or "{}")
            except Exception:
                return default_state
        if not isinstance(payload, dict):
            return default_state
        sessions = payload.get("sessions")
        if not isinstance(sessions, dict):
            return default_state
        normalized_sessions: dict[str, Any] = {}
        for raw_session_key, raw_state in sessions.items():
            session_key = str(raw_session_key or "").strip()
            if not session_key or not isinstance(raw_state, dict):
                continue
            pending_turns = [
                dict(item)
                for item in list(raw_state.get("pending_turns") or [])
                if isinstance(item, dict)
            ]
            stage_versions = {
                str(key or "").strip(): str(value or "").strip()
                for key, value in dict(raw_state.get("stage_versions") or {}).items()
                if str(key or "").strip() and str(value or "").strip()
            }
            normalized_sessions[session_key] = {
                "pending_turns": pending_turns,
                "stage_versions": stage_versions,
            }
        return {"sessions": normalized_sessions}

    def _write_review_state(self, payload: dict[str, Any]) -> None:
        normalized = dict(payload or {})
        sessions = normalized.get("sessions")
        if not isinstance(sessions, dict):
            normalized["sessions"] = {}
        with self._io_lock:
            self.review_state_file.write_text(
                json.dumps(normalized, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )

    def _review_window_turns(self) -> int:
        return max(int(getattr(self.config.queue, "review_interval_turns", 5) or 5), 1)

    @staticmethod
    def _memory_date_text(now_iso: str | None = None) -> str:
        value = datetime.fromisoformat(str(now_iso or MemoryManager._now_iso()).strip())
        return f"{value.year}/{value.month}/{value.day}"

    def _generate_memory_id(self, existing_ids: set[str]) -> str:
        alphabet = string.ascii_letters + string.digits
        for _ in range(2048):
            candidate = "".join(secrets.choice(alphabet) for _ in range(6))
            if candidate not in existing_ids:
                return candidate
        raise RuntimeError("unable to allocate unique memory id")

    @staticmethod
    def _normalize_memory_content_text(text: str) -> str:
        normalized = str(text or "").strip()
        if not normalized:
            raise _MemoryAgentValidationError("memory content must not be empty")
        if "\n" in normalized or "\r" in normalized:
            raise _MemoryAgentValidationError("memory content must be one line")
        return normalized

    @staticmethod
    def _allowed_review_flush_sources() -> set[str]:
        return {"token_compression", "stage_compaction"}

    @staticmethod
    def _compact_review_stage_summary(canonical_summary: dict[str, Any] | None) -> dict[str, Any] | None:
        if not isinstance(canonical_summary, dict):
            return None
        compact: dict[str, Any] = {}
        active_stage_id = str(canonical_summary.get("active_stage_id") or "").strip()
        if active_stage_id:
            compact["active_stage_id"] = active_stage_id
        stages: list[dict[str, str]] = []
        for raw_stage in list(canonical_summary.get("stages") or []):
            if not isinstance(raw_stage, dict):
                continue
            entry: dict[str, str] = {}
            for key in ("stage_id", "stage_goal", "completed_stage_summary"):
                value = str(raw_stage.get(key) or "").strip()
                if value:
                    entry[key] = value
            if entry:
                stages.append(entry)
        if stages:
            compact["stages"] = stages
        return compact or None

    @staticmethod
    def _review_stage_identity(stage: dict[str, Any]) -> str:
        stage_id = str(stage.get("stage_id") or "").strip()
        if stage_id:
            return stage_id
        encoded = json.dumps(stage, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return f"anonymous:{hashlib.sha256(encoded.encode('utf-8')).hexdigest()[:16]}"

    @staticmethod
    def _review_stage_version(stage: dict[str, Any]) -> str:
        payload = {
            "stage_goal": str(stage.get("stage_goal") or "").strip(),
            "completed_stage_summary": str(stage.get("completed_stage_summary") or "").strip(),
        }
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    @staticmethod
    def _compact_review_stage_summary_delta(
        *,
        canonical_summary: dict[str, Any] | None,
        stage_versions: dict[str, str] | None,
    ) -> tuple[dict[str, Any] | None, dict[str, str]]:
        compact = MemoryManager._compact_review_stage_summary(canonical_summary)
        normalized_versions = {
            str(key or "").strip(): str(value or "").strip()
            for key, value in dict(stage_versions or {}).items()
            if str(key or "").strip() and str(value or "").strip()
        }
        if not isinstance(compact, dict):
            return None, normalized_versions

        next_versions = dict(normalized_versions)
        delta_stages: list[dict[str, str]] = []
        for stage in list(compact.get("stages") or []):
            if not isinstance(stage, dict):
                continue
            identity = MemoryManager._review_stage_identity(stage)
            version = MemoryManager._review_stage_version(stage)
            if normalized_versions.get(identity) == version:
                continue
            next_versions[identity] = version
            delta_stages.append(stage)

        if not delta_stages:
            return None, next_versions

        delta: dict[str, Any] = {"stages": delta_stages}
        active_stage_id = str(compact.get("active_stage_id") or "").strip()
        if active_stage_id and any(str(stage.get("stage_id") or "").strip() == active_stage_id for stage in delta_stages):
            delta["active_stage_id"] = active_stage_id
        return delta, next_versions

    @staticmethod
    def _message_to_request_dict(message: Any) -> dict[str, Any]:
        if isinstance(message, dict):
            return dict(message)
        if isinstance(message, SystemMessage):
            role = "system"
        elif isinstance(message, HumanMessage):
            role = "user"
        elif isinstance(message, ToolMessage):
            role = "tool"
        else:
            role = "assistant"
        payload = {
            "role": role,
            "content": MemoryManager._response_text(message),
        }
        if role == "tool":
            name = str(getattr(message, "name", "") or "").strip()
            tool_call_id = str(getattr(message, "tool_call_id", "") or "").strip()
            if name:
                payload["name"] = name
            if tool_call_id:
                payload["tool_call_id"] = tool_call_id
        elif role == "assistant":
            tool_calls = MemoryManager._normalize_tool_calls(message)
            if tool_calls:
                payload["tool_calls"] = tool_calls
        return payload

    def _memory_request_messages(self, messages: list[Any]) -> list[dict[str, Any]]:
        return [self._message_to_request_dict(message) for message in list(messages or [])]

    @staticmethod
    def _extract_provider_request_metadata(response: Any) -> dict[str, Any]:
        metadata = getattr(response, "response_metadata", None)
        if not isinstance(metadata, dict):
            metadata = {}
        return {
            "provider_request_id": str(
                metadata.get("provider_request_id") or metadata.get("request_id") or ""
            ).strip(),
            "provider_request_meta": metadata.get("provider_request_meta"),
            "provider_request_body": metadata.get("provider_request_body"),
        }

    def _persist_memory_request_artifact(
        self,
        *,
        phase: str,
        batch_op: str,
        queue_request_ids: list[str],
        model_chain: list[str],
        request_messages: list[dict[str, Any]],
        response: Any,
    ) -> dict[str, Any] | None:
        provider_metadata = self._extract_provider_request_metadata(response)
        if not (
            provider_metadata["provider_request_id"]
            or provider_metadata["provider_request_meta"] is not None
            or provider_metadata["provider_request_body"] is not None
        ):
            return None
        artifact_id = self._request_id("memory_request")
        artifact_path = self.request_artifacts_dir / f"{artifact_id}.json"
        payload = {
            "artifact_id": artifact_id,
            "created_at": self._now_iso(),
            "phase": str(phase or "").strip() or "memory",
            "batch_op": str(batch_op or "").strip() or "write",
            "queue_request_ids": [str(item or "").strip() for item in list(queue_request_ids or []) if str(item or "").strip()],
            "model_chain": [str(item or "").strip() for item in list(model_chain or []) if str(item or "").strip()],
            "request_messages": list(request_messages or []),
            "actual_request_diagnostics": build_actual_request_diagnostics(
                request_messages=request_messages,
                tool_schemas=None,
            ),
            "provider_request_id": provider_metadata["provider_request_id"],
            "provider_request_meta": provider_metadata["provider_request_meta"],
            "provider_request_body": provider_metadata["provider_request_body"],
        }
        artifact_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return {
            "artifact_id": artifact_id,
            "path": str(artifact_path.resolve()),
            "provider_request_id": provider_metadata["provider_request_id"],
        }

    @staticmethod
    def _provider_request_ids_from_artifacts(request_artifacts: list[dict[str, Any]]) -> list[str]:
        provider_request_ids: list[str] = []
        seen: set[str] = set()
        for artifact in list(request_artifacts or []):
            provider_request_id = str((artifact or {}).get("provider_request_id") or "").strip()
            if not provider_request_id or provider_request_id in seen:
                continue
            seen.add(provider_request_id)
            provider_request_ids.append(provider_request_id)
        return provider_request_ids

    @staticmethod
    def _request_artifact_paths(request_artifacts: list[dict[str, Any]]) -> list[str]:
        return [
            str((artifact or {}).get("path") or "").strip()
            for artifact in list(request_artifacts or [])
            if str((artifact or {}).get("path") or "").strip()
        ]

    @staticmethod
    def _precheck_batch_for_processing(batch: MemoryBatch) -> str:
        if not isinstance(batch, MemoryBatch) or not list(batch.items or []):
            return "empty_batch"
        for item in batch.items:
            if not str(item.payload_text or "").strip():
                return "empty_payload_text"
        return ""

    def _append_terminal_history(
        self,
        *,
        batch: MemoryBatch,
        status: str,
        op: str,
        processed_at: str,
        discard_reason: str = "",
        usage: dict[str, int] | None = None,
        model_chain: list[str] | None = None,
        attempt_count: int = 0,
        validated: _MemoryValidatedWrite | None = None,
        provider_request_ids: list[str] | None = None,
        request_artifact_paths: list[str] | None = None,
        error: str = "",
    ) -> dict[str, Any]:
        payload = {
            "batch_id": self._request_id(batch.op),
            "op": str(op or "").strip() or batch.op,
            "source_op": batch.op,
            "status": str(status or "").strip() or "applied",
            "processed_at": str(processed_at or "").strip() or self._now_iso(),
            "request_ids": [item.request_id for item in batch.items],
            "request_count": len(batch.items),
            "decision_sources": [item.decision_source for item in batch.items],
            "payload_texts": [item.payload_text for item in batch.items],
            "usage": {
                "input_tokens": int((usage or {}).get("input_tokens", 0) or 0),
                "output_tokens": int((usage or {}).get("output_tokens", 0) or 0),
                "cache_read_tokens": int((usage or {}).get("cache_read_tokens", 0) or 0),
            },
            "model_chain": [str(item or "").strip() for item in list(model_chain or []) if str(item or "").strip()],
            "attempt_count": int(attempt_count or 0),
            "provider_request_ids": [str(item or "").strip() for item in list(provider_request_ids or []) if str(item or "").strip()],
            "request_artifact_paths": [str(item or "").strip() for item in list(request_artifact_paths or []) if str(item or "").strip()],
        }
        normalized_discard_reason = str(discard_reason or "").strip()
        if normalized_discard_reason:
            payload["discard_reason"] = normalized_discard_reason
        normalized_error = str(error or "").strip()
        if normalized_error:
            payload["error"] = normalized_error
        if validated is not None:
            payload["memory_chars_after"] = int(validated.memory_chars_after)
            payload["note_refs_written"] = list(validated.note_refs_written)
            payload["document_preview"] = validated.document_preview
            normalized_write_mode = str(validated.write_mode or "").strip().lower()
            if normalized_write_mode:
                payload["write_mode"] = normalized_write_mode
            normalized_noop_reason = str(validated.noop_reason or "").strip()
            if normalized_noop_reason:
                payload["noop_reason"] = normalized_noop_reason
        self._append_ops_payload(payload)
        return payload

    @staticmethod
    def _review_turn_payload(
        *,
        turn_id: str,
        user_messages: list[str],
        assistant_text: str,
        compression_summary: dict[str, Any] | None,
        canonical_summary: dict[str, Any] | None,
    ) -> str:
        parts = [f"[turn_id] {str(turn_id or '').strip() or '(unknown)'}"]
        normalized_user_messages = [str(item or "").strip() for item in list(user_messages or []) if str(item or "").strip()]
        if normalized_user_messages:
            parts.append("用户消息：")
            parts.extend(f"- {item}" for item in normalized_user_messages)
        assistant = str(assistant_text or "").strip()
        if assistant:
            parts.append("助手回复：")
            parts.append(assistant)
        if isinstance(compression_summary, dict) and compression_summary:
            parts.append("压缩摘要：")
            parts.append(json.dumps(compression_summary, ensure_ascii=False, sort_keys=True))
        if isinstance(canonical_summary, dict) and canonical_summary:
            parts.append("阶段摘要：")
            parts.append(json.dumps(canonical_summary, ensure_ascii=False, sort_keys=True))
        return "\n".join(str(item) for item in parts if str(item).strip()).strip()

    @staticmethod
    def _review_window_payload(*, session_key: str, pending_turns: list[dict[str, Any]]) -> str:
        sections = [f"[session_key] {str(session_key or '').strip()}"]
        for index, item in enumerate(list(pending_turns or []), start=1):
            payload_text = str(item.get("payload_text") or "").strip()
            if not payload_text:
                continue
            sections.append(f"## 窗口回合 {index}")
            sections.append(payload_text)
        return "\n\n".join(sections).strip()

    @staticmethod
    def _review_turn_payload(
        *,
        turn_id: str,
        user_messages: list[str],
        assistant_text: str,
        compression_summary: dict[str, Any] | None,
        canonical_summary: dict[str, Any] | None,
    ) -> str:
        _ = compression_summary
        parts = [f"[turn_id] {str(turn_id or '').strip() or '(unknown)'}"]
        normalized_user_messages = [
            str(item or "").strip()
            for item in list(user_messages or [])
            if str(item or "").strip()
        ]
        if normalized_user_messages:
            parts.append("user_messages:")
            parts.extend(f"- {item}" for item in normalized_user_messages)
        assistant = str(assistant_text or "").strip()
        if assistant:
            parts.append("assistant_text:")
            parts.append(assistant)
        compact_stage_summary = MemoryManager._compact_review_stage_summary(canonical_summary)
        if isinstance(compact_stage_summary, dict) and compact_stage_summary:
            parts.append("stage_summary:")
            parts.append(json.dumps(compact_stage_summary, ensure_ascii=False, sort_keys=True))
        return "\n".join(str(item) for item in parts if str(item).strip()).strip()

    async def record_turn_for_review(
        self,
        *,
        session_key: str,
        turn_id: str,
        user_messages: list[str],
        assistant_text: str,
        compression_summary: dict[str, Any] | None,
        canonical_summary: dict[str, Any] | None,
    ) -> dict[str, Any]:
        normalized_session = str(session_key or "").strip()
        if not normalized_session:
            return {"ok": True, "status": "ignored", "reason": "missing_session_key"}
        state = self._read_review_state()
        sessions = dict(state.get("sessions") or {})
        session_state = dict(sessions.get(normalized_session) or {})
        stage_versions = {
            str(key or "").strip(): str(value or "").strip()
            for key, value in dict(session_state.get("stage_versions") or {}).items()
            if str(key or "").strip() and str(value or "").strip()
        }
        stage_summary_delta, next_stage_versions = self._compact_review_stage_summary_delta(
            canonical_summary=canonical_summary,
            stage_versions=stage_versions,
        )
        pending_turns = [
            dict(item)
            for item in list(session_state.get("pending_turns") or [])
            if isinstance(item, dict)
        ]
        payload_text = self._review_turn_payload(
            turn_id=turn_id,
            user_messages=user_messages,
            assistant_text=assistant_text,
            compression_summary=compression_summary,
            canonical_summary=stage_summary_delta,
        )
        if not payload_text:
            return {"ok": True, "status": "ignored", "reason": "empty_turn"}
        pending_turns.append(
            {
                "turn_id": str(turn_id or "").strip(),
                "payload_text": payload_text,
                "created_at": self._now_iso(),
            }
        )
        if len(pending_turns) < self._review_window_turns():
            sessions[normalized_session] = {
                "pending_turns": pending_turns,
                "stage_versions": next_stage_versions,
            }
            self._write_review_state({"sessions": sessions})
            return {
                "ok": True,
                "status": "buffered",
                "pending_turn_count": len(pending_turns),
            }
        payload = self._review_window_payload(session_key=normalized_session, pending_turns=pending_turns)
        request = MemoryQueueRequest(
            op="assess",
            decision_source="self",
            payload_text=payload,
            created_at=self._now_iso(),
            request_id=self._request_id("assess"),
            session_key=normalized_session,
            trigger_source="ordinary_turn_window",
        )
        sessions[normalized_session] = {
            "pending_turns": [],
            "stage_versions": next_stage_versions,
        }
        self._write_review_state({"sessions": sessions})
        await self._append_queue_request(request)
        return {
            "ok": True,
            "status": "queued",
            "request_id": request.request_id,
            "pending_turn_count": 0,
        }

    async def flush_review_window(
        self,
        *,
        session_key: str,
        trigger_source: str,
    ) -> dict[str, Any]:
        normalized_session = str(session_key or "").strip()
        if not normalized_session:
            return {"ok": True, "status": "ignored", "reason": "missing_session_key"}
        normalized_trigger = str(trigger_source or "").strip() or "token_compression"
        if normalized_trigger not in self._allowed_review_flush_sources():
            return {"ok": True, "status": "ignored", "reason": "unsupported_trigger_source"}
        state = self._read_review_state()
        sessions = dict(state.get("sessions") or {})
        session_state = dict(sessions.get(normalized_session) or {})
        pending_turns = [
            dict(item)
            for item in list(session_state.get("pending_turns") or [])
            if isinstance(item, dict)
        ]
        if not pending_turns:
            return {"ok": True, "status": "idle", "reason": "no_pending_turns"}
        payload = self._review_window_payload(session_key=normalized_session, pending_turns=pending_turns)
        request = MemoryQueueRequest(
            op="assess",
            decision_source="self",
            payload_text=payload,
            created_at=self._now_iso(),
            request_id=self._request_id("assess"),
            session_key=normalized_session,
            trigger_source=normalized_trigger,
        )
        sessions[normalized_session] = {
            "pending_turns": [],
            "stage_versions": {
                str(key or "").strip(): str(value or "").strip()
                for key, value in dict(session_state.get("stage_versions") or {}).items()
                if str(key or "").strip() and str(value or "").strip()
            },
        }
        self._write_review_state({"sessions": sessions})
        await self._append_queue_request(request)
        return {
            "ok": True,
            "status": "queued",
            "request_id": request.request_id,
            "pending_turn_count": 0,
        }

    def clear_review_window(self, *, session_key: str) -> None:
        normalized_session = str(session_key or "").strip()
        if not normalized_session:
            return
        state = self._read_review_state()
        sessions = dict(state.get("sessions") or {})
        if normalized_session not in sessions:
            return
        sessions.pop(normalized_session, None)
        self._write_review_state({"sessions": sessions})

    async def enqueue_write_request(
        self,
        *,
        session_key: str,
        decision_source: str,
        payload_text: str,
        trigger_source: str,
    ) -> dict[str, Any]:
        normalized_payload = str(payload_text or "").strip()
        if not normalized_payload:
            return {"ok": True, "status": "ignored", "reason": "empty_payload"}
        request = MemoryQueueRequest(
            op="write",
            decision_source=self._normalize_decision_source(decision_source),
            payload_text=normalized_payload,
            created_at=self._now_iso(),
            session_key=str(session_key or "").strip(),
            trigger_source=str(trigger_source or "").strip(),
            request_id=self._request_id("write"),
        )
        await self._append_queue_request(request)
        return {"ok": True, "request_id": request.request_id, "status": "queued"}

    async def enqueue_delete_request(
        self,
        *,
        session_key: str,
        decision_source: str,
        payload_text: str,
        trigger_source: str,
    ) -> dict[str, Any]:
        normalized_payload = str(payload_text or "").strip()
        if not normalized_payload:
            return {"ok": True, "status": "ignored", "reason": "empty_payload"}
        request = MemoryQueueRequest(
            op="delete",
            decision_source=self._normalize_decision_source(decision_source),
            payload_text=normalized_payload,
            created_at=self._now_iso(),
            session_key=str(session_key or "").strip(),
            trigger_source=str(trigger_source or "").strip(),
            request_id=self._request_id("delete"),
        )
        await self._append_queue_request(request)
        return {"ok": True, "request_id": request.request_id, "status": "queued"}

    async def enqueue_autonomous_review(
        self,
        *,
        session_key: str,
        channel: str,
        chat_id: str,
        user_messages: list[str],
        assistant_text: str,
        turn_id: str,
        compression_summary: dict[str, Any] | None = None,
        canonical_summary: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        _ = channel, chat_id
        return await self.record_turn_for_review(
            session_key=session_key,
            turn_id=turn_id,
            user_messages=user_messages,
            assistant_text=assistant_text,
            compression_summary=compression_summary,
            canonical_summary=canonical_summary,
        )

    async def enqueue_pre_compression_flush(
        self,
        *,
        session_key: str,
        channel: str,
        chat_id: str,
        context_messages: list[dict[str, Any]],
    ) -> dict[str, Any]:
        _ = channel, chat_id, context_messages
        return await self.flush_review_window(
            session_key=session_key,
            trigger_source="pre_compression_flush",
        )

    async def enqueue_session_boundary_flush(
        self,
        *,
        session_key: str,
        channel: str,
        chat_id: str,
        trigger_source: str,
    ) -> dict[str, Any]:
        _ = channel, chat_id, trigger_source
        self.clear_review_window(session_key=session_key)
        return {"ok": True, "status": "ignored", "reason": "session_boundary_flush_disabled"}

    def _memory_agent_tools(self, session: _MemoryToolSession):
        @tool("memory_read_note")
        def memory_read_note(ref: str) -> str:
            """Read one existing note by ref without the .md suffix."""

            return session.read_note(ref)

        @tool("memory_apply_batch")
        def memory_apply_batch(
            adds: list[dict[str, Any]] | None = None,
            rewrites: list[dict[str, Any]] | None = None,
            deletes: list[str] | None = None,
            note_upserts: dict[str, str] | None = None,
            noop_reason: str | None = None,
        ) -> dict[str, Any]:
            """Stage one complete memory mutation batch."""

            return session.apply_batch(
                adds=adds,
                rewrites=rewrites,
                deletes=deletes,
                note_upserts=note_upserts,
                noop_reason=noop_reason,
            )

        return [memory_read_note, memory_apply_batch]

    def _memory_agent_user_prompt(
        self,
        *,
        batch: MemoryBatch,
        snapshot_text: str,
        repair_reason: str,
    ) -> str:
        note_index = sorted(path.stem for path in self.notes_dir.glob("*.md"))
        requests = [
            {
                "request_id": item.request_id,
                "op": item.op,
                "decision_source": item.decision_source,
                "payload_text": item.payload_text,
                "created_at": item.created_at,
                "trigger_source": item.trigger_source,
                "session_key": item.session_key,
            }
            for item in batch.items
        ]
        repair_block = f"\n上一轮输出无效：{repair_reason}\n请修复后重试。\n" if repair_reason else ""
        return (
            f"当前记忆正文：\n{snapshot_text or '(empty)'}\n\n"
            f"现有 note refs：{json.dumps(note_index, ensure_ascii=False)}\n"
            f"当前批次类型：{batch.op}\n"
            f"待处理请求：\n{json.dumps(requests, ensure_ascii=False, indent=2)}\n"
            "提交要求：\n"
            "- 只允许使用一次 memory_apply_batch 完成所有修改。\n"
            "- 新增记忆必须通过 adds 提交，并显式给出 decision_source。\n"
            "- rewrite 只传 id 和 content；系统会保留原 source 并刷新日期。\n"
            "- delete 传记忆 id 列表。\n"
            "- note_upserts 只写需要新增或改写的 note。\n"
            "- 当本轮无需任何记忆或 note 变更时，可单独提交 noop_reason。\n"
            f"{repair_block}"
        )

    def _memory_assessor_user_prompt(self, *, batch: MemoryBatch) -> str:
        windows = [str(item.payload_text or "").strip() for item in batch.items if str(item.payload_text or "").strip()]
        return (
            "以下是待评估的对话窗口内容。\n"
            "请只判断是否存在值得进入长期记忆的高价值内容。\n\n"
            f"{chr(10).join(windows) or '(empty)'}\n"
        )

    @staticmethod
    async def _execute_memory_tool(tools: list[Any], tool_call: dict[str, Any]) -> dict[str, Any]:
        name = str(tool_call.get("name") or "").strip()
        args = dict(tool_call.get("args") or {})
        tool_map = {str(getattr(item, "name", "") or ""): item for item in list(tools or [])}
        target = tool_map.get(name)
        if target is None:
            return {"ok": False, "tool": name, "error": f"unknown memory tool: {name}"}
        try:
            result = target.invoke(args)
            if asyncio.iscoroutine(result):
                result = await result
            return {"ok": True, "tool": name, "result": result}
        except Exception as exc:
            return {"ok": False, "tool": name, "error": str(exc or "memory tool failed").strip()}

    async def _run_memory_assessor_batch(
        self,
        *,
        batch: MemoryBatch,
        runtime_config: Any,
        model_chain: list[str],
        queue_request_ids: list[str],
    ) -> dict[str, Any]:
        result_holder = {"content": None}

        @tool("memory_assessment_result")
        def memory_assessment_result(content: str) -> str:
            """Record the assessor decision as either null or one refined memory text block."""

            normalized = str(content or "").strip()
            if not normalized:
                raise ValueError("content must not be empty")
            result_holder["content"] = normalized
            return "assessment recorded"

        model = build_chat_model(runtime_config, role="memory").bind_tools([memory_assessment_result])
        messages: list[Any] = [
            SystemMessage(content=self._memory_assessor_system_prompt()),
            HumanMessage(content=self._memory_assessor_user_prompt(batch=batch)),
        ]
        usage_total = {"input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0}
        request_artifacts: list[dict[str, Any]] = []
        round_limit = int(runtime_config.get_role_max_iterations("memory") or 6)
        round_limit = max(round_limit, 1)
        for _ in range(round_limit):
            request_messages = self._memory_request_messages(messages)
            response = await model.ainvoke(messages)
            self._merge_usage(usage_total, self._extract_usage(response))
            artifact = self._persist_memory_request_artifact(
                phase="assessor",
                batch_op=batch.op,
                queue_request_ids=queue_request_ids,
                model_chain=model_chain,
                request_messages=request_messages,
                response=response,
            )
            if artifact is not None:
                request_artifacts.append(artifact)
            final_text = self._response_text(response)
            tool_calls = self._normalize_tool_calls(response)
            if not tool_calls:
                break
            messages.append(AIMessage(content=final_text, tool_calls=tool_calls))
            for tool_call in tool_calls:
                result = await self._execute_memory_tool([memory_assessment_result], tool_call)
                messages.append(
                    ToolMessage(
                        content=json.dumps(result, ensure_ascii=False),
                        tool_call_id=str(tool_call.get("id") or ""),
                        name=str(tool_call.get("name") or ""),
                    )
                )
            if result_holder["content"] is not None:
                break
        if result_holder["content"] is None:
            return {
                "assessed_text": None,
                "usage": usage_total,
                "request_artifacts": request_artifacts,
                "discard_reason": "rejected",
                "error": "memory assessor must call memory_assessment_result",
            }
        assessed_text = str(result_holder["content"] or "").strip()
        return {
            "assessed_text": None if assessed_text.lower() == "null" else assessed_text,
            "usage": usage_total,
            "request_artifacts": request_artifacts,
        }

    def _build_validated_write_from_apply_batch(
        self,
        *,
        before_text: str,
        session: _MemoryToolSession,
        batch: MemoryBatch,
    ) -> _MemoryValidatedWrite:
        if session.apply_batch_count != 1 or not isinstance(session.applied_batch, dict):
            raise _MemoryAgentValidationError("memory agent must call memory_apply_batch exactly once")
        payload = dict(session.applied_batch or {})
        adds = list(payload.get("adds") or [])
        rewrites = list(payload.get("rewrites") or [])
        deletes = list(payload.get("deletes") or [])
        note_upserts = dict(payload.get("note_upserts") or {})
        noop_reason = str(payload.get("noop_reason") or "").strip()

        if batch.op == "delete" and (adds or rewrites):
            raise _MemoryAgentValidationError("delete batch may not add or rewrite memories")
        if batch.op == "delete" and noop_reason:
            raise _MemoryAgentValidationError("delete batch may not use noop_reason")
        if noop_reason and (adds or rewrites or deletes or note_upserts):
            raise _MemoryAgentValidationError("noop_reason may not be combined with add, rewrite, delete, or note_upsert")

        before_entries = parse_memory_document(before_text)
        existing_by_id = {entry.memory_id: entry for entry in before_entries}
        if len(existing_by_id) != len(before_entries):
            raise _MemoryAgentValidationError("current memory snapshot contains duplicate ids")

        delete_ids = {str(item or "").strip() for item in deletes if str(item or "").strip()}
        missing_delete_ids = sorted(memory_id for memory_id in delete_ids if memory_id not in existing_by_id)
        if missing_delete_ids:
            raise _MemoryAgentValidationError(f"delete ids not found: {', '.join(missing_delete_ids)}")

        rewrite_by_id: dict[str, str] = {}
        for item in rewrites:
            if not isinstance(item, dict):
                raise _MemoryAgentValidationError("rewrite item must be an object")
            memory_id = str(item.get("id") or "").strip()
            content = self._normalize_memory_content_text(str(item.get("content") or ""))
            if memory_id not in existing_by_id:
                raise _MemoryAgentValidationError(f"rewrite id not found: {memory_id}")
            rewrite_by_id[memory_id] = content

        existing_ids = set(existing_by_id)
        date_text = self._memory_date_text()
        after_entries: list[MemoryEntry] = []
        seen_summary_keys: set[tuple[str, str]] = set()
        for entry in before_entries:
            if entry.memory_id in delete_ids:
                continue
            if entry.memory_id in rewrite_by_id:
                updated_summary = rewrite_by_id[entry.memory_id]
                updated = MemoryEntry(
                    memory_id=entry.memory_id,
                    date_text=date_text,
                    source=entry.source,
                    summary=updated_summary,
                    note_ref=(self._extract_note_ref(updated_summary)),
                )
                summary_key = (updated.source, updated.summary)
                if summary_key in seen_summary_keys:
                    continue
                seen_summary_keys.add(summary_key)
                after_entries.append(updated)
                continue
            summary_key = (entry.source, entry.summary)
            if summary_key in seen_summary_keys:
                continue
            seen_summary_keys.add(summary_key)
            after_entries.append(entry)

        for item in adds:
            if not isinstance(item, dict):
                raise _MemoryAgentValidationError("add item must be an object")
            content = self._normalize_memory_content_text(str(item.get("content") or ""))
            decision_source = str(item.get("decision_source") or "").strip().lower()
            if decision_source not in {"user", "self"}:
                raise _MemoryAgentValidationError("add decision_source must be user or self")
            summary_key = (decision_source, content)
            if summary_key in seen_summary_keys:
                continue
            seen_summary_keys.add(summary_key)
            memory_id = self._generate_memory_id(existing_ids)
            existing_ids.add(memory_id)
            after_entries.append(
                MemoryEntry(
                    memory_id=memory_id,
                    date_text=date_text,
                    source=decision_source,
                    summary=content,
                    note_ref=(self._extract_note_ref(content)),
                )
            )

        document_text = "".join(format_memory_entry(entry) for entry in after_entries)
        validate_memory_document(
            document_text,
            summary_max_chars=int(getattr(self.config.document, "summary_max_chars", 250) or 250),
            document_max_chars=int(getattr(self.config.document, "document_max_chars", 10000) or 10000),
        )
        existing_refs = {path.stem for path in self.notes_dir.glob("*.md")}
        note_writes = {
            str(ref): self._normalize_note_text(content)
            for ref, content in note_upserts.items()
            if str(ref or "").strip()
        }
        note_refs = {
            str(entry.note_ref or "").strip()
            for entry in after_entries
            if str(entry.note_ref or "").strip()
        }
        missing_refs = sorted(ref for ref in note_refs if ref not in existing_refs and ref not in note_writes)
        if missing_refs:
            raise _MemoryAgentValidationError(f"missing note refs: {', '.join(missing_refs)}")
        return _MemoryValidatedWrite(
            document_text=self._normalize_document_text(document_text),
            note_writes=note_writes,
            note_refs_written=sorted(note_writes.keys()),
            memory_chars_after=len(self._normalize_document_text(document_text)),
            document_preview=self._document_preview(document_text),
            write_mode=self._validated_write_mode(
                adds=adds,
                rewrites=rewrites,
                deletes=deletes,
                note_upserts=note_upserts,
                noop_reason=noop_reason,
            ),
            noop_reason=noop_reason,
        )

    @staticmethod
    def _validated_write_mode(
        *,
        adds: list[Any],
        rewrites: list[Any],
        deletes: list[Any],
        note_upserts: dict[str, Any],
        noop_reason: str,
    ) -> str:
        if str(noop_reason or "").strip():
            return ""
        has_adds = bool(list(adds or []))
        has_rewrites_or_deletes = bool(list(rewrites or []) or list(deletes or []))
        has_note_only_mutation = bool(dict(note_upserts or {})) and not has_adds and not has_rewrites_or_deletes
        has_existing_mutations = has_rewrites_or_deletes or has_note_only_mutation
        if has_adds and has_existing_mutations:
            return "mixed"
        if has_adds:
            return "add"
        if has_existing_mutations:
            return "rewrite"
        return ""

    @staticmethod
    def _extract_note_ref(text: str) -> str:
        match = _NOTE_REF_RE.search(str(text or "").strip())
        return str(match.group("ref") or "").strip() if match else ""

    def _validate_candidate_state(
        self,
        *,
        batch: MemoryBatch,
        before_text: str,
        session: _MemoryToolSession,
    ) -> _MemoryValidatedWrite:
        return self._build_validated_write_from_apply_batch(
            before_text=before_text,
            session=session,
            batch=batch,
        )

    async def _run_memory_agent_batch(
        self,
        *,
        batch: MemoryBatch,
        runtime_config: Any,
        runtime_revision: int,
        model_chain: list[str],
    ) -> dict[str, Any]:
        before_text = self.snapshot_text()
        total_usage = {"input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0}
        request_artifacts: list[dict[str, Any]] = []
        queue_request_ids = [item.request_id for item in batch.items if str(item.request_id or "").strip()]

        current_runtime_config = runtime_config
        current_runtime_revision = int(runtime_revision or 0)
        current_model_chain = list(model_chain or [])
        processing_batch = batch
        if batch.op == "assess":
            assess_result = await self._run_memory_assessor_batch(
                batch=batch,
                runtime_config=current_runtime_config,
                model_chain=current_model_chain,
                queue_request_ids=queue_request_ids,
            )
            self._merge_usage(total_usage, assess_result["usage"])
            request_artifacts.extend(list(assess_result.get("request_artifacts") or []))
            if str(assess_result.get("discard_reason") or "").strip():
                return {
                    "validated": None,
                    "usage": total_usage,
                    "attempt_count": 1,
                    "assessed_text": None,
                    "discard_reason": str(assess_result.get("discard_reason") or "").strip(),
                    "error": str(assess_result.get("error") or "").strip(),
                    "model_chain": list(current_model_chain),
                    "provider_request_ids": self._provider_request_ids_from_artifacts(request_artifacts),
                    "request_artifact_paths": self._request_artifact_paths(request_artifacts),
                }
            assessed_text = assess_result.get("assessed_text")
            if assessed_text is None:
                return {
                    "validated": None,
                    "usage": total_usage,
                    "attempt_count": 1,
                    "assessed_text": None,
                    "discard_reason": "assessed_null",
                    "model_chain": list(current_model_chain),
                    "provider_request_ids": self._provider_request_ids_from_artifacts(request_artifacts),
                    "request_artifact_paths": self._request_artifact_paths(request_artifacts),
                }
            processing_batch = MemoryBatch(
                op="write",
                items=[
                    MemoryQueueRequest(
                        op="write",
                        decision_source="self",
                        payload_text=str(assessed_text or "").strip(),
                        created_at=self._now_iso(),
                        request_id=self._request_id("assessed"),
                        trigger_source=f"assess:{batch.items[0].request_id if batch.items else 'window'}",
                        session_key=str(batch.items[0].session_key or "").strip() if batch.items else "",
                    )
                ],
            )
            current_runtime_config, current_runtime_revision, current_model_chain = self._reload_runtime_config_for_memory_retry(
                runtime_config=current_runtime_config,
                runtime_revision=current_runtime_revision,
                model_chain=current_model_chain,
            )

        total_attempts = 3
        last_error = "memory agent did not stage a batch"
        for attempt_index in range(total_attempts):
            if attempt_index > 0:
                current_runtime_config, current_runtime_revision, current_model_chain = self._reload_runtime_config_for_memory_retry(
                    runtime_config=current_runtime_config,
                    runtime_revision=current_runtime_revision,
                    model_chain=current_model_chain,
                )
            repair_reason = last_error if attempt_index > 0 else ""
            attempt = await self._run_memory_agent_attempt(
                batch=processing_batch,
                runtime_config=current_runtime_config,
                before_text=before_text,
                repair_reason=repair_reason,
                queue_request_ids=queue_request_ids,
                model_chain=current_model_chain,
            )
            self._merge_usage(total_usage, attempt.usage)
            request_artifacts.extend(list(attempt.request_artifacts or []))
            try:
                validated = self._validate_candidate_state(
                    batch=processing_batch,
                    before_text=before_text,
                    session=attempt.session,
                )
                return {
                    "validated": validated,
                    "usage": total_usage,
                    "attempt_count": attempt_index + 1,
                    "assessed_text": None if batch.op != "assess" else processing_batch.items[0].payload_text,
                    "model_chain": list(current_model_chain),
                    "provider_request_ids": self._provider_request_ids_from_artifacts(request_artifacts),
                    "request_artifact_paths": self._request_artifact_paths(request_artifacts),
                }
            except _MemoryAgentValidationError as exc:
                last_error = str(exc or "memory agent output invalid").strip()
                continue
        return {
            "validated": None,
            "usage": total_usage,
            "attempt_count": total_attempts,
            "assessed_text": None if batch.op != "assess" else processing_batch.items[0].payload_text,
            "discard_reason": "rejected",
            "error": last_error,
            "model_chain": list(current_model_chain),
            "provider_request_ids": self._provider_request_ids_from_artifacts(request_artifacts),
            "request_artifact_paths": self._request_artifact_paths(request_artifacts),
        }

    async def run_due_batch_once(self, *, now_iso: str | None = None) -> dict[str, Any]:
        worker_lease = self._try_acquire_memory_worker_lease()
        if worker_lease is None:
            return {"ok": True, "status": "worker_lease_unavailable", "processed": 0}

        try:
            processed_request_ids = self._processed_request_ids()
            dropped_request_ids = self._drop_processed_queue_requests(
                processed_request_ids=processed_request_ids,
            )
            effective_now = str(now_iso or self._now_iso()).strip()
            batch = await self.collect_due_batch(now_iso=effective_now)
            if batch is None or not batch.items:
                if dropped_request_ids:
                    return {
                        "ok": True,
                        "status": "already_processed",
                        "processed": 0,
                        "request_ids": list(dropped_request_ids),
                    }
                return {"ok": True, "status": "idle", "processed": 0}
            duplicate_request_ids = [
                item.request_id
                for item in batch.items
                if str(item.request_id or "").strip() in processed_request_ids
            ]
            if duplicate_request_ids:
                self._drop_request_ids(set(duplicate_request_ids))
                return {
                    "ok": True,
                    "status": "already_processed",
                    "processed": 0,
                    "request_ids": list(duplicate_request_ids),
                }
            precheck_error = self._precheck_batch_for_processing(batch)
            if precheck_error:
                self._drop_request_ids({item.request_id for item in batch.items})
                processed_at = self._now_iso()
                self._append_terminal_history(
                    batch=batch,
                    status="discarded",
                    op=batch.op,
                    processed_at=processed_at,
                    discard_reason="precheck_failed",
                    error=precheck_error,
                )
                return {
                    "ok": True,
                    "status": "discarded",
                    "discard_reason": "precheck_failed",
                    "op": batch.op,
                    "processed": len(batch.items),
                    "request_ids": [item.request_id for item in batch.items],
                    "processed_at": processed_at,
                }
            if str(batch.items[0].status or "pending").strip() != "processing":
                self._mark_batch_processing(batch.items, effective_now)

            try:
                runtime_config, _revision, _changed = get_runtime_config()
            except Exception as exc:
                return self._mark_batch_error(batch, effective_now, str(exc or "memory runtime config unavailable"))

            model_chain = self._memory_model_chain(runtime_config)
            if not model_chain:
                return self._mark_batch_blocked(batch, effective_now, "memory role not configured")

            try:
                result = await self._run_memory_agent_batch(
                    batch=batch,
                    runtime_config=runtime_config,
                    runtime_revision=int(_revision or 0),
                    model_chain=model_chain,
                )
            except Exception as exc:
                logger.warning("memory batch failed: {}", exc)
                return self._mark_batch_error(batch, effective_now, str(exc or "memory batch failed"))

            self._drop_request_ids({item.request_id for item in batch.items})
            discard_reason = str(result.get("discard_reason") or "").strip()
            effective_model_chain = list(result.get("model_chain") or model_chain)
            if discard_reason:
                processed_at = self._now_iso()
                self._append_terminal_history(
                    batch=batch,
                    status="discarded",
                    op=batch.op,
                    processed_at=processed_at,
                    discard_reason=discard_reason,
                    usage=result.get("usage"),
                    model_chain=effective_model_chain,
                    attempt_count=int(result.get("attempt_count", 0) or 0),
                    provider_request_ids=list(result.get("provider_request_ids") or []),
                    request_artifact_paths=list(result.get("request_artifact_paths") or []),
                    error=str(result.get("error") or "").strip(),
                )
                return {
                    "ok": True,
                    "status": "discarded",
                    "discard_reason": discard_reason,
                    "op": batch.op,
                    "processed": len(batch.items),
                    "request_ids": [item.request_id for item in batch.items],
                    "attempt_count": int(result.get("attempt_count", 0) or 0),
                    "usage": {
                        "input_tokens": int((result.get("usage") or {}).get("input_tokens", 0) or 0),
                        "output_tokens": int((result.get("usage") or {}).get("output_tokens", 0) or 0),
                        "cache_read_tokens": int((result.get("usage") or {}).get("cache_read_tokens", 0) or 0),
                    },
                    "model_chain": list(effective_model_chain),
                    "provider_request_ids": list(result.get("provider_request_ids") or []),
                    "request_artifact_paths": list(result.get("request_artifact_paths") or []),
                    "processed_at": processed_at,
                }

            self._commit_validated_write(result["validated"])
            processed_at = self._now_iso()
            processed_payload = self._append_terminal_history(
                batch=batch,
                status="applied",
                op="write" if batch.op == "assess" else batch.op,
                processed_at=processed_at,
                usage=result.get("usage"),
                model_chain=effective_model_chain,
                attempt_count=int(result["attempt_count"]),
                validated=result["validated"],
                provider_request_ids=list(result.get("provider_request_ids") or []),
                request_artifact_paths=list(result.get("request_artifact_paths") or []),
            )
            return {
                "ok": True,
                "status": "applied",
                "op": batch.op,
                "processed": len(batch.items),
                "request_ids": [item.request_id for item in batch.items],
                "attempt_count": int(result["attempt_count"]),
                "usage": dict(processed_payload["usage"]),
                "model_chain": list(effective_model_chain),
                "provider_request_ids": list(processed_payload["provider_request_ids"]),
                "request_artifact_paths": list(processed_payload["request_artifact_paths"]),
                "processed_at": processed_at,
                **({"noop_reason": str(processed_payload.get("noop_reason") or "").strip()} if str(processed_payload.get("noop_reason") or "").strip() else {}),
            }
        finally:
            _release_file_lock(worker_lease)

    def _reload_runtime_config_for_memory_retry(
        self,
        *,
        runtime_config: Any,
        runtime_revision: int,
        model_chain: list[str],
    ) -> tuple[Any, int, list[str]]:
        try:
            refreshed_config, refreshed_revision, _changed = get_runtime_config(force=False)
        except Exception:
            return runtime_config, int(runtime_revision or 0), list(model_chain or [])
        normalized_revision = int(refreshed_revision or 0)
        if normalized_revision == int(runtime_revision or 0):
            return runtime_config, int(runtime_revision or 0), list(model_chain or [])
        refreshed_chain = self._memory_model_chain(refreshed_config)
        return refreshed_config, normalized_revision, list(refreshed_chain)
