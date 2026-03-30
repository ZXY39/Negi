from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from loguru import logger

from g3ku.agent.rag_memory import CommitArtifact, MemoryManager
from g3ku.session.manager import Session


class SessionCommitService:
    """Drive commit-based long-term memory writes with fixed triggers."""

    def __init__(
        self,
        *,
        memory_manager: MemoryManager,
        turn_trigger: int = 20,
        idle_minutes_trigger: int = 360,
    ):
        self.memory_manager = memory_manager
        self.turn_trigger = max(1, int(turn_trigger or 20))
        self.idle_minutes_trigger = max(1, int(idle_minutes_trigger or 360))

    @staticmethod
    def _parse_iso(value: str | None) -> datetime | None:
        if not value:
            return None
        try:
            return datetime.fromisoformat(value)
        except Exception:
            return None

    def _should_commit(self, session: Session, *, now: datetime, force: bool) -> tuple[bool, str]:
        if force:
            return True, "forced"

        turn_count = int(getattr(session, "commit_turn_counter", 0) or 0)
        if turn_count >= self.turn_trigger:
            return True, "turn_trigger"

        last_user_at = self._parse_iso(getattr(session, "last_user_turn_at", None))
        if last_user_at is not None:
            idle_window = timedelta(minutes=self.idle_minutes_trigger)
            if now - last_user_at >= idle_window:
                return True, "idle_trigger"

        return False, ""

    async def maybe_commit(
        self,
        *,
        session: Session,
        channel: str,
        chat_id: str,
        force: bool = False,
        reason: str | None = None,
    ) -> CommitArtifact | None:
        now = datetime.now()
        should_commit, detected_reason = self._should_commit(session, now=now, force=force)
        if not should_commit:
            return None

        metadata = session.metadata if isinstance(session.metadata, dict) else {}
        if session.metadata is not metadata:
            session.metadata = metadata

        start_idx = int(metadata.get("last_commit_index", 0) or 0)
        start_idx = max(0, min(start_idx, len(session.messages)))
        payload = list(session.messages[start_idx:])
        if not payload:
            metadata["last_commit_index"] = len(session.messages)
            return None

        commit_reason = reason or detected_reason or "turn_trigger"
        latest_archive_overview = ""
        if hasattr(session, "archive_segments") and isinstance(session.archive_segments, list):
            latest_segment = next(
                (
                    item
                    for item in reversed(list(session.archive_segments or []))
                    if isinstance(item, dict) and str(item.get("overview_uri") or "").strip()
                ),
                None,
            )
            if isinstance(latest_segment, dict):
                overview_uri = str(latest_segment.get("overview_uri") or "").strip()
                if overview_uri:
                    try:
                        latest_archive_overview = Path(overview_uri).read_text(encoding="utf-8").strip()
                    except Exception:
                        latest_archive_overview = ""
        try:
            artifact = await self.memory_manager.commit_session(
                session_key=session.key,
                channel=channel,
                chat_id=chat_id,
                messages=payload,
                reason=commit_reason,
                latest_archive_overview=latest_archive_overview,
            )
        except Exception:
            logger.exception("Session commit failed for {}", session.key)
            return None

        metadata["last_commit_index"] = len(session.messages)
        metadata["last_commit_reason"] = commit_reason
        metadata["last_commit_at"] = now.isoformat()
        metadata["last_commit_artifact"] = asdict(artifact)

        keep_recent = max(
            0,
            int(
                getattr(
                    getattr(getattr(self.memory_manager, 'config', None), 'assembly', None),
                    'recent_messages_limit',
                    24,
                )
                or 24
            ),
        )
        if hasattr(session, 'last_consolidated'):
            session.last_consolidated = max(
                int(getattr(session, 'last_consolidated', 0) or 0),
                max(0, len(session.messages) - keep_recent),
            )

        if hasattr(session, "archive_segments") and isinstance(session.archive_segments, list):
            session.archive_segments.append(
                {
                    "archive_id": artifact.archive_id,
                    "archive_uri": artifact.archive_uri,
                    "overview_uri": artifact.overview_uri,
                    "abstract_uri": artifact.abstract_uri,
                    "start_idx": start_idx,
                    "end_idx": len(session.messages),
                    "reason": commit_reason,
                    "created_at": now.isoformat(),
                    "summary_version": artifact.summary_version,
                }
            )

        if hasattr(session, "commit_turn_counter"):
            session.commit_turn_counter = 0

        return artifact

    async def commit_for_new_session(
        self,
        *,
        session: Session,
        channel: str,
        chat_id: str,
    ) -> CommitArtifact | None:
        return await self.maybe_commit(
            session=session,
            channel=channel,
            chat_id=chat_id,
            force=True,
            reason="new_command",
        )

