"""Session management for conversation history."""

import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger

from g3ku.utils.helpers import ensure_dir, safe_filename

# Tail window read by `list_sessions` to pick up an incrementally appended
# metadata row without parsing the whole transcript file.
_METADATA_TAIL_WINDOW_BYTES = 512 * 1024


class _TrackingList(list):
    """List that records edits requiring a full-file rewrite.

    Plain appends and extends are append-compatible with the JSONL store.
    Any structural edit (insert, pop, replace, delete, clear, reorder) makes
    the next save rewrite the whole file so the on-disk history stays exact.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.structural_edit = False

    def _mark_edit(self) -> None:
        self.structural_edit = True

    def append(self, item: Any) -> None:
        super().append(item)

    def extend(self, items: Any) -> None:
        super().extend(items)

    def insert(self, index: int, item: Any) -> None:
        self._mark_edit()
        return super().insert(index, item)

    def pop(self, index: int = -1) -> Any:
        self._mark_edit()
        return super().pop(index)

    def remove(self, value: Any) -> None:
        self._mark_edit()
        return super().remove(value)

    def __setitem__(self, key: Any, value: Any) -> None:
        self._mark_edit()
        return super().__setitem__(key, value)

    def __delitem__(self, key: Any) -> None:
        self._mark_edit()
        return super().__delitem__(key)

    def clear(self) -> None:
        self._mark_edit()
        return super().clear()

    def sort(self, *args: Any, **kwargs: Any) -> None:
        self._mark_edit()
        return super().sort(*args, **kwargs)

    def reverse(self) -> None:
        self._mark_edit()
        return super().reverse()

    def __iadd__(self, other: Any) -> "_TrackingList":
        self._mark_edit()
        result = super().__iadd__(other)
        self.structural_edit = True
        return result


@dataclass
class Session:
    """
    A conversation session.

    Stores messages in JSONL format for easy reading and persistence.

    Messages are append-only and persisted directly as transcript history.
    """

    key: str  # channel:chat_id
    messages: _TrackingList = field(default_factory=_TrackingList)
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)
    metadata: dict[str, Any] = field(default_factory=dict)
    last_user_turn_at: str | None = None
    commit_turn_counter: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.messages, _TrackingList):
            self.messages = _TrackingList(self.messages)

    def add_message(self, role: str, content: str, **kwargs: Any) -> None:
        """Add a message to the session."""
        msg = {
            "role": role,
            "content": content,
            "timestamp": datetime.now().isoformat(),
            **kwargs
        }
        self.messages.append(msg)
        if role == "user":
            self.last_user_turn_at = msg["timestamp"]
            self.commit_turn_counter += 1
        self.updated_at = datetime.now()

    def get_history(self, max_messages: int = 500) -> list[dict[str, Any]]:
        """Return recent transcript messages for LLM input, aligned to a user turn."""
        sliced = self.messages[-max_messages:]

        # Drop leading non-user messages to avoid orphaned tool_result blocks
        for i, m in enumerate(sliced):
            if m.get("role") == "user":
                sliced = sliced[i:]
                break

        out: list[dict[str, Any]] = []
        for m in sliced:
            entry: dict[str, Any] = {"role": m["role"], "content": m.get("content", "")}
            for k in ("tool_calls", "tool_call_id", "name"):
                if k in m:
                    entry[k] = m[k]
            out.append(entry)
        return out

    def get_history_messages(self, max_messages: int = 500):
        """Return history normalized into AgentMessage objects."""
        from g3ku.runtime.message_adapter import dicts_to_agent_messages

        return dicts_to_agent_messages(self.get_history(max_messages=max_messages))

    def clear(self) -> None:
        """Clear all messages and reset session to initial state."""
        # Replacing the whole list is a structural edit even though the new
        # list itself only ever receives appends afterwards.
        messages = _TrackingList()
        messages.structural_edit = True
        self.messages = messages
        self.last_user_turn_at = None
        self.commit_turn_counter = 0
        self.updated_at = datetime.now()


class SessionManager:
    """
    Manages conversation sessions.

    Sessions are stored as JSONL files in the sessions directory.
    """

    def __init__(self, workspace: Path):
        self.workspace = workspace
        self.sessions_dir = ensure_dir(self.workspace / "sessions")
        self._cache: dict[str, Session] = {}
        self._file_states: dict[str, dict[str, Any]] = {}

    def _get_session_path(self, key: str) -> Path:
        """Get the file path for a session."""
        safe_key = safe_filename(key.replace(":", "_"))
        return self.sessions_dir / f"{safe_key}.jsonl"

    def get_path(self, key: str) -> Path:
        """Get the persistent path for a session."""
        return self._get_session_path(key)

    def get_or_create(self, key: str) -> Session:
        """
        Get an existing session or create a new one.

        Args:
            key: Session key (usually channel:chat_id).

        Returns:
            The session.
        """
        if key in self._cache:
            return self._cache[key]

        session = self._load(key)
        if session is None:
            session = Session(key=key)

        self._cache[key] = session
        return session

    def _load(self, key: str) -> Session | None:
        """Load a session from disk."""
        path = self._get_session_path(key)
        if not path.exists():
            return None

        try:
            messages = _TrackingList()
            metadata = {}
            created_at = None
            last_user_turn_at: str | None = None
            commit_turn_counter = 0

            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue

                    data = json.loads(line)

                    if data.get("_type") == "metadata":
                        metadata = data.get("metadata", {})
                        created_at = datetime.fromisoformat(data["created_at"]) if data.get("created_at") else None
                        last_user_turn_at = data.get("last_user_turn_at")
                        commit_turn_counter = int(data.get("commit_turn_counter", 0) or 0)
                    else:
                        messages.append(data)

            session = Session(
                key=key,
                messages=messages,
                created_at=created_at or datetime.now(),
                metadata=metadata,
                last_user_turn_at=last_user_turn_at,
                commit_turn_counter=commit_turn_counter,
            )
            self._migrate_oversized_records(session)
            path_size = path.stat().st_size if path.exists() else 0
            self._file_states[key] = {
                "record_count": len(session.messages),
                "size": path_size,
            }
            return session
        except Exception as e:
            logger.warning("Failed to load session {}: {}", key, e)
            return None

    def _migrate_oversized_records(self, session: Session) -> None:
        """Shrink legacy transcript records once so future saves are append-only."""
        try:
            from g3ku.runtime.frontdoor.canonical_context import (
                project_canonical_context_for_transcript,
                TRANSCRIPT_PROJECTION_MODE,
            )

            messages = session.messages
            migrated = 0
            for index, message in enumerate(list(messages)):
                if not isinstance(message, dict):
                    continue
                if str(message.get("canonical_context_projection") or "").strip():
                    # Already a transcript projection: re-projecting cannot shrink
                    # it further and would force a full rewrite on every load.
                    continue
                canonical_context = message.get("canonical_context")
                if not isinstance(canonical_context, dict) or not canonical_context:
                    continue
                try:
                    serialized = json.dumps(canonical_context, ensure_ascii=False)
                except Exception:
                    serialized = ""
                if len(serialized) <= 256 * 1024:
                    continue
                projected = project_canonical_context_for_transcript(canonical_context)
                if not list(projected.get("stages") or []):
                    continue
                updated = dict(message)
                updated["canonical_context"] = projected
                updated["canonical_context_projection"] = TRANSCRIPT_PROJECTION_MODE
                messages[index] = updated
                migrated += 1
            if migrated:
                logger.info(
                    "Projected {} oversized transcript record(s) for session {}; next save rewrites",
                    migrated,
                    session.key,
                )
        except Exception as exc:
            logger.warning(
                "Skipped transcript projection migration for session {}: {}",
                session.key,
                exc,
            )

    def _metadata_line(self, session: Session) -> dict[str, Any]:
        return {
            "_type": "metadata",
            "key": session.key,
            "created_at": session.created_at.isoformat(),
            "updated_at": session.updated_at.isoformat(),
            "metadata": session.metadata,
            "last_user_turn_at": session.last_user_turn_at,
            "commit_turn_counter": session.commit_turn_counter,
        }

    def _append_save(
        self,
        path: Path,
        session: Session,
        *,
        start_index: int,
    ) -> None:
        with open(path, "a", encoding="utf-8") as f:
            for msg in session.messages[start_index:]:
                f.write(json.dumps(msg, ensure_ascii=False) + "\n")
            f.write(json.dumps(self._metadata_line(session), ensure_ascii=False) + "\n")

    def save(self, session: Session) -> None:
        """Save a session to disk."""
        path = self._get_session_path(session.key)
        tracked = self._file_states.get(session.key)
        messages = session.messages
        appendable = (
            tracked is not None
            and path.exists()
            and isinstance(messages, _TrackingList)
            and not messages.structural_edit
            and len(messages) >= int(tracked.get("record_count") or 0)
            and path.stat().st_size == int(tracked.get("size") or -1)
        )
        if appendable:
            self._append_save(path, session, start_index=int(tracked.get("record_count") or 0))
            self._file_states[session.key] = {
                "record_count": len(messages),
                "size": path.stat().st_size,
            }
            self._cache[session.key] = session
            return

        with open(path, "w", encoding="utf-8") as f:
            f.write(json.dumps(self._metadata_line(session), ensure_ascii=False) + "\n")
            for msg in session.messages:
                f.write(json.dumps(msg, ensure_ascii=False) + "\n")

        if isinstance(messages, _TrackingList):
            messages.structural_edit = False
        self._file_states[session.key] = {
            "record_count": len(messages),
            "size": path.stat().st_size,
        }
        self._cache[session.key] = session

    def invalidate(self, key: str) -> None:
        """Remove a session from the in-memory cache."""
        self._cache.pop(key, None)
        self._file_states.pop(key, None)

    def delete(self, key: str) -> bool:
        """Delete a session from disk and cache."""
        path = self._get_session_path(key)
        self.invalidate(key)
        self._file_states.pop(key, None)
        if not path.exists():
            return False
        path.unlink()
        return True

    @staticmethod
    def _metadata_entry(line: str, path: Path) -> dict[str, Any] | None:
        """Parse one JSONL line into a listing entry when it is a metadata row."""
        text = (line or "").strip()
        if not text:
            return None
        try:
            data = json.loads(text)
        except Exception:
            return None
        if not isinstance(data, dict) or data.get("_type") != "metadata":
            return None
        return {
            "key": data.get("key") or path.stem.replace("_", ":", 1),
            "created_at": data.get("created_at"),
            "updated_at": data.get("updated_at"),
            "path": str(path),
        }

    def _session_list_entry(self, path: Path) -> dict[str, Any] | None:
        """Build the session listing entry without reading the whole transcript.

        Incremental saves append a fresh metadata row, so the last metadata row
        is authoritative for `updated_at` / `commit_turn_counter`, while the
        first row still provides the original `created_at`. Only the head and a
        bounded tail window are read.
        """
        with open(path, "rb") as f:
            head = f.readline(_METADATA_TAIL_WINDOW_BYTES).decode("utf-8", errors="ignore")
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - _METADATA_TAIL_WINDOW_BYTES))
            tail = f.read().decode("utf-8", errors="ignore")
        head_entry = self._metadata_entry(head, path)
        tail_lines = [item for item in tail.split("\n") if item.strip()]
        tail_entry = self._metadata_entry(tail_lines[-1] if tail_lines else "", path)
        if tail_entry and head_entry:
            tail_entry["created_at"] = tail_entry.get("created_at") or head_entry.get("created_at")
        return tail_entry or head_entry

    def list_sessions(self) -> list[dict[str, Any]]:
        """
        List all sessions.

        Returns:
            List of session info dicts.
        """
        entries: list[dict[str, Any]] = []

        for path in self.sessions_dir.glob("*.jsonl"):
            try:
                entry = self._session_list_entry(path)
            except Exception:
                entry = None
            if entry:
                entries.append(entry)

        return sorted(entries, key=lambda x: x.get("updated_at", ""), reverse=True)

