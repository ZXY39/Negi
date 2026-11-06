"""Durable intake and dispatch for agent-created external event subscriptions."""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime
from typing import Any, Callable

from loguru import logger

from g3ku.runtime.event_subscriptions import EventSubscriptionManager

_MAX_INFLIGHT_DISPATCHES = 8
_RECONCILE_INTERVAL_SECONDS = 60.0
_PENDING_SCAN_LIMIT = 200


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


class ExternalEventService:
    def __init__(
        self,
        *,
        store: Any,
        task_service: Any | None,
        heartbeat_getter: Callable[[], Any | None],
    ):
        self._store = store
        self._task_service = task_service
        self._heartbeat_getter = heartbeat_getter
        self._manager = EventSubscriptionManager(store, task_service)
        self._started = False
        self._closed = False
        self._reconcile_task: asyncio.Task[None] | None = None
        self._dispatch_tasks: dict[str, asyncio.Task[None]] = {}
        self._inflight_event_ids: set[str] = set()

    @property
    def manager(self) -> EventSubscriptionManager:
        return self._manager

    @property
    def store(self) -> Any:
        return self._store

    async def start(self) -> None:
        if self._started or self._closed:
            return
        self._started = True
        self._schedule_pending()
        self._reconcile_task = asyncio.create_task(
            self._reconcile_loop(),
            name="external-event-reconcile",
        )
        logger.info("external event service started")

    async def stop(self) -> None:
        self._closed = True
        self._started = False
        reconcile = self._reconcile_task
        self._reconcile_task = None
        if reconcile is not None:
            reconcile.cancel()
        tasks = list(self._dispatch_tasks.values())
        self._dispatch_tasks.clear()
        self._inflight_event_ids.clear()
        for task in tasks:
            task.cancel()
        pending = [task for task in [reconcile, *tasks] if task is not None]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        logger.info("external event service stopped")

    def accept(
        self,
        *,
        subscription: dict[str, Any],
        idempotency_key: str,
        event_type: str,
        occurred_at: str,
        text: str,
        data: dict[str, Any] | None,
    ) -> tuple[dict[str, Any], bool]:
        subscription_id = str(subscription.get("subscription_id") or "").strip()
        if not subscription_id:
            raise ValueError("subscription_identity_required")
        event_id = f"evt-{uuid.uuid4().hex[:20]}"
        created_at = _now_iso()
        record, inserted = self._store.insert_external_event(
            {
                "event_id": event_id,
                "subscription_id": subscription_id,
                "idempotency_key": str(idempotency_key or "").strip(),
                "event_type": str(event_type or "").strip(),
                "occurred_at": str(occurred_at or "").strip(),
                "payload": {
                    "text": str(text or ""),
                    "data": dict(data or {}),
                },
                "status": "pending",
                "delivery_detail": {"stage": "accepted", "target_kind": subscription.get("target_kind")},
                "created_at": created_at,
                "updated_at": created_at,
            }
        )
        if inserted:
            logger.info(
                "external event accepted: subscription={} event={} target={}",
                subscription_id,
                event_id,
                str(subscription.get("target_kind") or ""),
            )
            self._schedule_dispatch(event_id)
        else:
            logger.info(
                "external event duplicate: subscription={} key={} original_event={}",
                subscription_id,
                idempotency_key,
                record.get("event_id"),
            )
        return record, inserted

    def _schedule_pending(self) -> None:
        try:
            events = self._store.list_pending_external_events(limit=_PENDING_SCAN_LIMIT)
        except Exception:
            logger.exception("external event pending scan failed")
            return
        for event in events:
            if len(self._dispatch_tasks) >= _MAX_INFLIGHT_DISPATCHES:
                return
            event_id = str(event.get("event_id") or "").strip()
            if event_id:
                self._schedule_dispatch(event_id)

    def _schedule_dispatch(self, event_id: str) -> None:
        key = str(event_id or "").strip()
        if not key or self._closed:
            return
        if key in self._inflight_event_ids:
            return
        if len(self._dispatch_tasks) >= _MAX_INFLIGHT_DISPATCHES:
            return
        try:
            task = asyncio.create_task(
                self._dispatch_event(key),
                name=f"external-event-dispatch:{key}",
            )
        except RuntimeError:
            return
        self._dispatch_tasks[key] = task
        self._inflight_event_ids.add(key)

        def _done(done_task: asyncio.Task[None]) -> None:
            self._dispatch_tasks.pop(key, None)
            self._inflight_event_ids.discard(key)
            if done_task.cancelled():
                return
            try:
                error = done_task.exception()
            except asyncio.CancelledError:
                return
            if error is not None:
                logger.opt(exception=error).error(
                    "external event dispatch failed: event={} error={!r}",
                    key,
                    error,
                )

        task.add_done_callback(_done)

    async def _reconcile_loop(self) -> None:
        try:
            while not self._closed:
                await asyncio.sleep(_RECONCILE_INTERVAL_SECONDS)
                self._schedule_pending()
        except asyncio.CancelledError:
            raise

    async def _dispatch_event(self, event_id: str) -> None:
        event = self._store.get_external_event(event_id)
        if not event or str(event.get("status") or "") != "pending":
            return
        subscription_id = str(event.get("subscription_id") or "").strip()
        subscription = self._store.get_event_subscription(subscription_id)
        updated_at = _now_iso()
        if not subscription or bool(subscription.get("removed")):
            self._mark_undeliverable(event_id, "subscription_removed")
            return
        if not bool(subscription.get("enabled")):
            self._note_pending(event_id, {"stage": "subscription_disabled"})
            return

        payload = self._build_event_payload(event, subscription)
        target_kind = str(subscription.get("target_kind") or "").strip().lower()
        if target_kind == "session":
            heartbeat = self._heartbeat_getter()
            enqueue = getattr(heartbeat, "enqueue_external_event", None) if heartbeat is not None else None
            if not callable(enqueue):
                self._note_pending(event_id, {"stage": "heartbeat_unavailable"})
                self._record_subscription_error(subscription_id, "heartbeat_unavailable")
                return
            try:
                queued = bool(enqueue(payload))
            except Exception as exc:  # noqa: BLE001 - durable event stays pending
                logger.exception(
                    "external event heartbeat enqueue failed: session={} event={}",
                    subscription.get("session_key"),
                    event_id,
                )
                self._note_pending(event_id, {"stage": "heartbeat_enqueue_error", "error": str(exc)[:500]})
                self._record_subscription_error(subscription_id, str(exc)[:500])
                return
            detail = {
                "stage": "heartbeat_enqueued",
                "target_kind": "session",
                "target_session_key": str(subscription.get("session_key") or ""),
                "queued": queued,
                "at": updated_at,
            }
            self._note_pending(event_id, detail)
            logger.info(
                "external event heartbeat enqueued: session={} event={} queued={}",
                subscription.get("session_key"),
                event_id,
                queued,
            )
            return

        # Node delivery is wired by the node lane; keeping the event pending is
        # the safe outcome if this build cannot dispatch it yet.
        self._note_pending(event_id, {"stage": "target_unsupported", "target_kind": target_kind})
        self._record_subscription_error(subscription_id, "target_unsupported")

    def _build_event_payload(
        self,
        event: dict[str, Any],
        subscription: dict[str, Any],
    ) -> dict[str, Any]:
        body = dict(event.get("payload") or {})
        return {
            "event_id": str(event.get("event_id") or ""),
            "subscription_id": str(subscription.get("subscription_id") or ""),
            "subscription_name": str(subscription.get("name") or ""),
            "event_type": str(event.get("event_type") or ""),
            "occurred_at": str(event.get("occurred_at") or ""),
            "instruction": str(subscription.get("instruction") or ""),
            "condition_note": str(subscription.get("condition_note") or ""),
            "text": str(body.get("text") or ""),
            "data": dict(body.get("data") or {}),
            "target_kind": str(subscription.get("target_kind") or ""),
            "target_session_key": str(subscription.get("session_key") or ""),
            "target_task_id": str(subscription.get("task_id") or ""),
            "target_node_id": str(subscription.get("node_id") or ""),
            "session_id": str(subscription.get("session_key") or ""),
            "task_id": str(subscription.get("task_id") or ""),
            "node_id": str(subscription.get("node_id") or ""),
        }

    def _note_pending(self, event_id: str, detail: dict[str, Any]) -> None:
        try:
            self._store.mark_external_event_status(
                event_id,
                status="pending",
                detail=detail,
                updated_at=_now_iso(),
            )
        except Exception:
            logger.exception("external event pending detail update failed: event={}", event_id)

    def _mark_undeliverable(self, event_id: str, reason: str) -> None:
        try:
            self._store.mark_external_event_status(
                event_id,
                status="undeliverable",
                detail={"stage": "undeliverable", "reason": reason, "at": _now_iso()},
                updated_at=_now_iso(),
            )
        except Exception:
            logger.exception("external event terminal update failed: event={}", event_id)
            return
        logger.warning("external event undeliverable: event={} reason={}", event_id, reason)

    def _record_subscription_error(self, subscription_id: str, error_text: str) -> None:
        try:
            self._store.touch_event_subscription(
                subscription_id,
                updated_at=_now_iso(),
                last_delivery_error=str(error_text or "")[:500],
            )
        except Exception:
            logger.exception(
                "external event subscription error update failed: subscription={}",
                subscription_id,
            )


_SERVICE: ExternalEventService | None = None


def set_external_event_service(service: ExternalEventService | None) -> None:
    global _SERVICE
    _SERVICE = service


def get_external_event_service() -> ExternalEventService | None:
    return _SERVICE


__all__ = [
    "ExternalEventService",
    "get_external_event_service",
    "set_external_event_service",
]
