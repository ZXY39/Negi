from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from g3ku.heartbeat.session_service import WebSessionHeartbeatService

HeartbeatReplyNotifier = Callable[[str, str], Awaitable[None] | None]


def _bind_task_terminal_listener(main_task_service: Any, heartbeat: WebSessionHeartbeatService) -> None:
    log_service = getattr(main_task_service, "log_service", None)
    if log_service is None or not hasattr(log_service, "add_task_terminal_listener"):
        return

    current = getattr(log_service, "_web_session_heartbeat_service", None)
    if current is heartbeat:
        return

    listeners = getattr(log_service, "_task_terminal_listeners", None)
    if isinstance(listeners, list) and current is not None:
        log_service._task_terminal_listeners = [
            item
            for item in listeners
            if getattr(item, "__self__", None) is not current
        ]

    log_service.add_task_terminal_listener(heartbeat.enqueue_task_terminal)
    setattr(log_service, "_web_session_heartbeat_service", heartbeat)


def build_web_session_heartbeat(
    agent: Any,
    runtime_manager: Any,
    *,
    reply_notifier: HeartbeatReplyNotifier | None = None,
) -> WebSessionHeartbeatService | None:
    main_task_service = getattr(agent, "main_task_service", None)
    session_manager = getattr(agent, "sessions", None)
    if main_task_service is None or session_manager is None:
        setattr(agent, "web_session_heartbeat", None)
        return None

    existing = getattr(agent, "web_session_heartbeat", None)
    if (
        isinstance(existing, WebSessionHeartbeatService)
        and getattr(existing, "_agent", None) is agent
        and getattr(existing, "_runtime_manager", None) is runtime_manager
        and getattr(existing, "_main_task_service", None) is main_task_service
        and getattr(existing, "_session_manager", None) is session_manager
        and getattr(existing, "_reply_notifier", None) is reply_notifier
    ):
        heartbeat = existing
    else:
        heartbeat = WebSessionHeartbeatService(
            workspace=getattr(agent, "workspace", "."),
            agent=agent,
            runtime_manager=runtime_manager,
            main_task_service=main_task_service,
            session_manager=session_manager,
            reply_notifier=reply_notifier,
        )
        setattr(agent, "web_session_heartbeat", heartbeat)

    _bind_task_terminal_listener(main_task_service, heartbeat)
    return heartbeat


def _replay_pending_outbox(main_task_service: Any, heartbeat: WebSessionHeartbeatService) -> None:
    replay = getattr(heartbeat, "replay_pending_outbox", None)
    if callable(replay):
        replay()
        return

    store = getattr(main_task_service, "store", None)
    if store is None:
        return

    list_task_terminal = getattr(store, "list_pending_task_terminal_outbox", None)
    if callable(list_task_terminal):
        for entry in list_task_terminal(limit=500):
            payload = dict(entry.get("payload") or {})
            heartbeat.enqueue_task_terminal_payload(payload)

    list_task_stall = getattr(store, "list_pending_task_stall_outbox", None)
    if callable(list_task_stall):
        for entry in list_task_stall(limit=500):
            payload = dict(entry.get("payload") or {})
            heartbeat.enqueue_task_stall_payload(payload)


async def start_web_session_heartbeat(
    agent: Any,
    runtime_manager: Any,
    *,
    replay_pending_outbox: bool = False,
    reply_notifier: HeartbeatReplyNotifier | None = None,
) -> WebSessionHeartbeatService | None:
    heartbeat = build_web_session_heartbeat(
        agent,
        runtime_manager,
        reply_notifier=reply_notifier,
    )
    if heartbeat is None:
        return None

    await heartbeat.start()
    if replay_pending_outbox and not bool(getattr(heartbeat, "_outbox_replayed", False)):
        _replay_pending_outbox(getattr(agent, "main_task_service", None), heartbeat)
        setattr(heartbeat, "_outbox_replayed", True)
    return heartbeat
