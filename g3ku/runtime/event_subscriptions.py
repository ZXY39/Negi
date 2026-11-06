"""External event subscriptions created by the agent for its own wake-ups.

The manager owns the control plane only: target ownership, credentials and
subscription lifecycle. Trigger detection stays in the external program; event
delivery is owned by ``external_event_service``.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import tempfile
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger

EVENT_SUBSCRIPTION_DIRNAME = "external-events"
CREDENTIAL_TOKEN_PREFIX = "nev_"
_SUBSCRIPTION_NAME_MAX = 120
_INSTRUCTION_MAX = 8000
_CONDITION_NOTE_MAX = 2000
_PUBLIC_FIELDS = (
    "subscription_id",
    "name",
    "enabled",
    "target_kind",
    "session_key",
    "task_id",
    "node_id",
    "instruction",
    "condition_note",
    "credential_path",
    "created_at",
    "updated_at",
    "last_event_at",
    "last_delivery_error",
)


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _hash_secret(token: str) -> str:
    return hashlib.sha256(str(token or "").encode("utf-8")).hexdigest()


def _public_subscription(record: dict[str, Any] | None) -> dict[str, Any] | None:
    if not record:
        return None
    return {key: record.get(key) for key in _PUBLIC_FIELDS}


class EventSubscriptionManager:
    def __init__(self, store: Any, task_service: Any | None = None):
        self._store = store
        self._task_service = task_service

    @property
    def store(self) -> Any:
        return self._store

    def create(
        self,
        *,
        runtime_context: dict[str, Any] | None,
        name: str,
        instruction: str,
        condition_note: str = "",
        target_kind: str = "session",
        task_id: str = "",
        node_id: str = "",
        credential_file: str,
    ) -> dict[str, Any]:
        owner = self._owner_context(runtime_context)
        normalized_name = str(name or "").strip()
        normalized_instruction = str(instruction or "").strip()
        normalized_target = str(target_kind or "session").strip().lower()
        if not normalized_name:
            raise ValueError("name_required")
        if len(normalized_name) > _SUBSCRIPTION_NAME_MAX:
            raise ValueError(f"name_too_long (max {_SUBSCRIPTION_NAME_MAX} chars)")
        if not normalized_instruction:
            raise ValueError("instruction_required")
        if len(normalized_instruction) > _INSTRUCTION_MAX:
            raise ValueError(f"instruction_too_long (max {_INSTRUCTION_MAX} chars)")
        normalized_note = str(condition_note or "").strip()
        if len(normalized_note) > _CONDITION_NOTE_MAX:
            raise ValueError(f"condition_note_too_long (max {_CONDITION_NOTE_MAX} chars)")
        if normalized_target not in {"session", "node"}:
            raise ValueError("target_kind_invalid")

        resolved_target = self._resolve_target(
            owner,
            target_kind=normalized_target,
            task_id=task_id,
            node_id=node_id,
        )
        credential_path = self._resolve_credential_path(
            owner,
            runtime_context=runtime_context,
            credential_file=credential_file,
            must_not_exist=True,
        )

        subscription_id = f"sub-{uuid.uuid4().hex[:20]}"
        token = CREDENTIAL_TOKEN_PREFIX + secrets.token_urlsafe(32)
        credential = {
            "subscription_id": subscription_id,
            "token": token,
            "endpoint_path": f"/api/v1/event-subscriptions/{subscription_id}/events",
            "authorization_header": "Authorization",
            "idempotency_header": "Idempotency-Key",
        }
        self._write_credential_file(credential_path, credential)
        created_at = _now_iso()
        try:
            record = self._store.create_event_subscription(
                {
                    "subscription_id": subscription_id,
                    "name": normalized_name,
                    "enabled": True,
                    "target_kind": normalized_target,
                    "session_key": resolved_target["session_key"],
                    "task_id": resolved_target["task_id"],
                    "node_id": resolved_target["node_id"],
                    "instruction": normalized_instruction,
                    "condition_note": normalized_note,
                    "secret_hash": _hash_secret(token),
                    "credential_path": str(credential_path),
                    "owner_session_key": owner["session_key"],
                    "owner_task_id": owner["task_id"],
                    "owner_node_id": owner["node_id"],
                    "created_at": created_at,
                    "updated_at": created_at,
                    "removed_at": "",
                    "last_event_at": "",
                    "last_delivery_error": "",
                }
            )
        except Exception:
            try:
                credential_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise
        logger.info(
            "external event subscription created: subscription={} target={} session={} task={} node={}",
            subscription_id,
            normalized_target,
            resolved_target["session_key"],
            resolved_target["task_id"] or "-",
            resolved_target["node_id"] or "-",
        )
        result = _public_subscription(record) or {}
        result["endpoint_path"] = credential["endpoint_path"]
        result["credential_file"] = str(credential_path)
        result["credential_hint"] = token[:8] + "..."
        return result

    def list(self, *, runtime_context: dict[str, Any] | None) -> list[dict[str, Any]]:
        owner = self._owner_context(runtime_context)
        records = self._store.list_event_subscriptions(
            owner_session_key=owner["session_key"],
            owner_task_id=owner["task_id"],
        )
        return [item for item in (_public_subscription(record) for record in records) if item]

    def events(
        self,
        *,
        runtime_context: dict[str, Any] | None,
        subscription_id: str,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        record = self._owned_subscription(runtime_context, subscription_id)
        events = self._store.list_external_events_for_subscription(
            record["subscription_id"],
            limit=max(1, min(int(limit or 20), 50)),
        )
        public: list[dict[str, Any]] = []
        for event in events:
            public.append(
                {
                    "event_id": str(event.get("event_id") or ""),
                    "type": str(event.get("event_type") or ""),
                    "status": str(event.get("status") or ""),
                    "occurred_at": str(event.get("occurred_at") or ""),
                    "created_at": str(event.get("created_at") or ""),
                    "updated_at": str(event.get("updated_at") or ""),
                    "detail": dict(event.get("delivery_detail") or {}),
                }
            )
        return public

    def set_enabled(
        self,
        *,
        runtime_context: dict[str, Any] | None,
        subscription_id: str,
        enabled: bool,
    ) -> dict[str, Any] | None:
        record = self._owned_subscription(runtime_context, subscription_id)
        updated = self._store.set_event_subscription_enabled(
            record["subscription_id"],
            enabled=bool(enabled),
            updated_at=_now_iso(),
        )
        return _public_subscription(updated)

    def rotate_credential(
        self,
        *,
        runtime_context: dict[str, Any] | None,
        subscription_id: str,
    ) -> dict[str, Any]:
        record = self._owned_subscription(runtime_context, subscription_id)
        credential_path = self._resolve_credential_path(
            self._owner_context(runtime_context),
            runtime_context=runtime_context,
            credential_file=str(record.get("credential_path") or ""),
            must_not_exist=False,
        )
        token = CREDENTIAL_TOKEN_PREFIX + secrets.token_urlsafe(32)
        credential = {
            "subscription_id": record["subscription_id"],
            "token": token,
            "endpoint_path": f"/api/v1/event-subscriptions/{record['subscription_id']}/events",
            "authorization_header": "Authorization",
            "idempotency_header": "Idempotency-Key",
        }
        previous_hash = str(record.get("secret_hash") or "")
        previous_path = str(record.get("credential_path") or "")
        staged = self._stage_credential_file(credential_path, credential)
        try:
            updated = self._store.update_event_subscription_secret(
                record["subscription_id"],
                secret_hash=_hash_secret(token),
                credential_path=str(credential_path),
                updated_at=_now_iso(),
            )
        except Exception:
            staged.unlink(missing_ok=True)
            raise
        try:
            os.replace(staged, credential_path)
        except OSError:
            # The old file is still in place. Restore the old hash so the
            # connector's existing credential remains valid.
            try:
                self._store.update_event_subscription_secret(
                    record["subscription_id"],
                    secret_hash=previous_hash,
                    credential_path=previous_path,
                    updated_at=_now_iso(),
                )
            except Exception:
                logger.exception(
                    "external event credential rollback failed: subscription={}",
                    record["subscription_id"],
                )
            staged.unlink(missing_ok=True)
            raise
        logger.info(
            "external event subscription credential rotated: subscription={}",
            record["subscription_id"],
        )
        result = _public_subscription(updated) or {}
        result["credential_file"] = str(credential_path)
        result["credential_hint"] = token[:8] + "..."
        return result

    def remove(
        self,
        *,
        runtime_context: dict[str, Any] | None,
        subscription_id: str,
    ) -> dict[str, Any] | None:
        record = self._owned_subscription(runtime_context, subscription_id)
        removed = self._store.remove_event_subscription(
            record["subscription_id"],
            updated_at=_now_iso(),
        )
        logger.info(
            "external event subscription removed: subscription={}",
            record["subscription_id"],
        )
        return _public_subscription(removed)

    def authenticate(self, subscription_id: str, token: str) -> tuple[dict[str, Any] | None, str]:
        record = self._store.get_event_subscription(subscription_id)
        if not record:
            return None, "not_found"
        if bool(record.get("removed")):
            return None, "not_found"
        if not bool(record.get("enabled")):
            return None, "disabled"
        presented = str(token or "")
        expected = str(record.get("secret_hash") or "")
        if not presented or not expected:
            return None, "invalid_token"
        if not secrets.compare_digest(_hash_secret(presented), expected):
            return None, "invalid_token"
        return record, "ok"

    def _owned_subscription(
        self,
        runtime_context: dict[str, Any] | None,
        subscription_id: str,
    ) -> dict[str, Any]:
        owner = self._owner_context(runtime_context)
        key = str(subscription_id or "").strip()
        record = self._store.get_event_subscription(key)
        if not record or bool(record.get("removed")):
            raise ValueError("subscription_not_found")
        if str(record.get("owner_session_key") or "") != owner["session_key"]:
            raise ValueError("subscription_not_found")
        if owner["task_id"] and str(record.get("owner_task_id") or "") != owner["task_id"]:
            raise ValueError("subscription_not_found")
        return record

    @staticmethod
    def _owner_context(runtime_context: dict[str, Any] | None) -> dict[str, str]:
        context = dict(runtime_context or {})
        role = str(context.get("actor_role") or "ceo").strip().lower() or "ceo"
        session_key = str(context.get("session_key") or "").strip()
        task_id = str(context.get("task_id") or "").strip()
        node_id = str(context.get("node_id") or "").strip()
        if role not in {"ceo", "execution"}:
            raise ValueError("event_subscription_role_not_allowed")
        if not session_key:
            raise ValueError("no_session_context")
        if role == "execution" and (not task_id or not node_id):
            raise ValueError("no_task_node_context")
        return {
            "actor_role": role,
            "session_key": session_key,
            "task_id": task_id if role == "execution" else "",
            "node_id": node_id if role == "execution" else "",
        }

    def _resolve_target(
        self,
        owner: dict[str, str],
        *,
        target_kind: str,
        task_id: str,
        node_id: str,
    ) -> dict[str, str]:
        service = self._task_service
        role = owner["actor_role"]
        if target_kind == "session":
            if str(task_id or "").strip() or str(node_id or "").strip():
                raise ValueError("session_target_accepts_no_task_or_node")
            return {
                "session_key": owner["session_key"],
                "task_id": "",
                "node_id": "",
            }

        if service is None:
            raise ValueError("task_service_unavailable")
        normalized_task_id = ""
        normalized_node_id = str(node_id or "").strip()
        if normalized_node_id:
            normalized_node_id = service.normalize_node_id(normalized_node_id)
            node = service.get_node(normalized_node_id)
            if node is None:
                raise ValueError("target_node_not_found")
            normalized_node_id = str(getattr(node, "node_id", "") or normalized_node_id)
            normalized_task_id = str(getattr(node, "task_id", "") or "")
        normalized_task_id = service.normalize_task_id(
            str(task_id or "").strip() or normalized_task_id
        )
        if not normalized_task_id:
            normalized_task_id = owner["task_id"]
        task = service.get_task(normalized_task_id)
        if task is None:
            raise ValueError("target_task_not_found")
        if role == "execution" and normalized_task_id != owner["task_id"]:
            raise ValueError("target_outside_task")
        if str(getattr(task, "session_id", "") or "") != owner["session_key"]:
            raise ValueError("target_outside_session")
        if not normalized_node_id:
            raise ValueError("node_id_required")
        normalized_node_id = service.normalize_node_id(normalized_node_id)
        node = service.get_node(normalized_node_id)
        if node is None or str(getattr(node, "task_id", "") or "") != normalized_task_id:
            raise ValueError("target_node_not_found")
        if str(getattr(node, "node_kind", "execution") or "execution").strip().lower() == "acceptance":
            raise ValueError("acceptance_node_target_not_allowed")
        if str(getattr(node, "status", "") or "").strip().lower() not in {"", "in_progress"}:
            raise ValueError("target_node_terminal")
        return {
            "session_key": str(getattr(task, "session_id", "") or owner["session_key"]),
            "task_id": normalized_task_id,
            "node_id": normalized_node_id,
        }

    @staticmethod
    def _credential_roots(runtime_context: dict[str, Any] | None) -> list[Path]:
        context = dict(runtime_context or {})
        roots: list[Path] = []
        for raw in (
            context.get("session_workspace_dir"),
            context.get("task_temp_dir"),
            context.get("temp_dir"),
        ):
            text = str(raw or "").strip()
            if not text:
                continue
            try:
                root = Path(text).expanduser().resolve()
            except OSError:
                continue
            if root not in roots:
                roots.append(root)
        return roots

    def _resolve_credential_path(
        self,
        owner: dict[str, str],
        *,
        runtime_context: dict[str, Any] | None,
        credential_file: str,
        must_not_exist: bool,
    ) -> Path:
        raw = str(credential_file or "").strip()
        if not raw:
            raise ValueError("credential_file_required")
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            raise ValueError("credential_file_must_be_absolute")
        roots = self._credential_roots(runtime_context)
        if not roots:
            raise ValueError("credential_root_unavailable")
        resolved = candidate.resolve()
        if not any(resolved.is_relative_to(root) for root in roots):
            raise ValueError("credential_file_outside_allowed_root")
        if resolved in roots:
            raise ValueError("credential_file_must_be_file")
        if must_not_exist and resolved.exists():
            raise ValueError("credential_file_exists")
        try:
            resolved.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ValueError(f"credential_directory_unavailable: {exc}") from exc
        return resolved

    @staticmethod
    def _stage_credential_file(path: Path, credential: dict[str, Any]) -> Path:
        data = json.dumps(credential, ensure_ascii=False, indent=2) + "\n"
        fd, temp_name = tempfile.mkstemp(
            prefix=".event-credential-",
            dir=str(path.parent),
        )
        temp_path = Path(temp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.chmod(temp_path, 0o600)
            except OSError:
                pass
            return temp_path
        except Exception:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise

    @classmethod
    def _write_credential_file(cls, path: Path, credential: dict[str, Any]) -> None:
        staged = cls._stage_credential_file(path, credential)
        try:
            os.replace(staged, path)
        except Exception:
            staged.unlink(missing_ok=True)
            raise


__all__ = [
    "EVENT_SUBSCRIPTION_DIRNAME",
    "EventSubscriptionManager",
]
