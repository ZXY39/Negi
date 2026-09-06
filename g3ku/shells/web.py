"""Web shell runtime bootstrap for the converged runtime architecture."""

from __future__ import annotations

import os
import asyncio
import json
import subprocess
import sys
from typing import Optional
from urllib.parse import urlparse

from loguru import logger

from g3ku.agent.loop import AgentLoop
from g3ku.bus.events import OutboundMessage
from g3ku.bus.queue import MessageBus
from g3ku.china_bridge import CHINA_CHANNELS, ChinaBridgeSupervisor, ChinaBridgeTransport
from g3ku.runtime.external_events import get_session_event_hub
from g3ku.runtime.external_sessions import EXTERNAL_OUTBOUND_CHANNEL, get_external_session_registry
from g3ku.runtime.session_keys import (
    EXTERNAL_SESSION_KEY_PREFIX,
    build_chat_id,
    parse_china_session_key,
    sanitize_channel_outbound_text,
)
from g3ku.config.loader import get_data_dir
from g3ku.config.live_runtime import get_runtime_config
from g3ku.cron.runtime_dispatch import dispatch_cron_job
from g3ku.cron.service import CronService
from g3ku.heartbeat.bootstrap import build_web_session_heartbeat, start_web_session_heartbeat
from g3ku.runtime.bootstrap_factory import make_agent_loop as _make_agent_loop
from g3ku.runtime.bootstrap_factory import make_provider as _make_provider
from g3ku.runtime import SessionRuntimeBridge
from g3ku.runtime import SessionRuntimeManager
from g3ku.runtime.config_refresh import refresh_loop_runtime_config
from g3ku.security import get_bootstrap_security_service
from g3ku.web.launcher import run_web_server_entrypoint
from g3ku.web.worker_control import (
    ensure_managed_task_worker,
    keep_worker_enabled,
    managed_worker_pid,
    shutdown_managed_task_worker,
)
from main.protocol import now_iso
from main.service.task_terminal_callback import TASK_TERMINAL_CALLBACK_URL_ENV

_global_agent: Optional[AgentLoop] = None
_global_bus: Optional[MessageBus] = None
_global_runtime_manager: Optional[SessionRuntimeManager] = None
_global_web_heartbeat = None
_global_china_transport: Optional[ChinaBridgeTransport] = None
_global_china_supervisor: Optional[ChinaBridgeSupervisor] = None
_global_china_outbound_task: Optional[asyncio.Task] = None
_global_china_start_task: Optional[asyncio.Task] = None
_global_runtime_services_lock: Optional[asyncio.Lock] = None

_NO_CEO_MODEL_CONFIGURED_MESSAGE = "No model configured for role 'ceo'."


def is_no_ceo_model_configured_error(exc: BaseException | None) -> bool:
    return str(exc or '').strip() == _NO_CEO_MODEL_CONFIGURED_MESSAGE


def no_ceo_model_configured_payload() -> dict[str, str]:
    return {
        'code': 'no_model_configured',
        'message': '当前项目还没有配置可用模型。请先进入“模型配置”页面，新增并保存至少一个模型，并把它分配给主Agent（CEO）角色。',
    }


def _get_runtime_services_lock() -> asyncio.Lock:
    global _global_runtime_services_lock
    if _global_runtime_services_lock is None:
        _global_runtime_services_lock = asyncio.Lock()
    return _global_runtime_services_lock


def _listen_port_owners(port: int) -> set[int] | None:
    owners: set[int] = set()
    try:
        if os.name == 'nt':
            result = subprocess.run(
                ['netstat', '-ano', '-p', 'tcp'],
                capture_output=True,
                text=True,
                check=False,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
            )
            if result.returncode != 0:
                return None
            needle = f':{int(port)}'
            for line in result.stdout.splitlines():
                parts = line.split()
                if len(parts) < 5:
                    continue
                local_addr = parts[1]
                state = parts[3].upper()
                owning_pid = parts[4]
                if not local_addr.endswith(needle):
                    continue
                if state != 'LISTENING':
                    continue
                try:
                    owners.add(int(owning_pid))
                except Exception:
                    continue
            return owners

        commands = [
            ['ss', '-ltnp'],
            ['lsof', '-nP', f'-iTCP:{int(port)}', '-sTCP:LISTEN'],
        ]
        for command in commands:
            try:
                result = subprocess.run(command, capture_output=True, text=True, check=False)
            except FileNotFoundError:
                continue
            if result.returncode != 0:
                continue
            output = result.stdout
            if os.path.basename(command[0]) == 'ss':
                for line in output.splitlines():
                    if f':{int(port)}' not in line:
                        continue
                    for segment in line.split('pid=')[1:]:
                        pid_part = ''.join(ch for ch in segment if ch.isdigit())
                        if not pid_part:
                            continue
                        owners.add(int(pid_part))
                return owners
            for line in output.splitlines():
                if f':{int(port)}' not in line:
                    continue
                parts = line.split()
                for value in parts:
                    if value.isdigit():
                        owners.add(int(value))
                return owners
    except Exception:
        return None
    return None


def _process_owns_listen_port(port: int, *, pid: int | None = None) -> bool | None:
    owners = _listen_port_owners(port)
    if owners is None:
        return None
    return int(pid or os.getpid()) in owners


def debug_trace_enabled() -> bool:
    raw = str(os.getenv('G3KU_DEBUG_TRACE', '')).strip().lower()
    return raw in {'1', 'true', 'yes', 'on', 'debug'}


def _resolve_web_runtime_port(agent: AgentLoop | None = None) -> int:
    callback_url = str(os.getenv(TASK_TERMINAL_CALLBACK_URL_ENV, "") or "").strip()
    if callback_url:
        try:
            parsed = urlparse(callback_url)
            if parsed.port:
                return int(parsed.port)
        except Exception:
            logger.debug("web cron port resolution skipped for callback url {}", callback_url)
    runtime_agent = agent or _global_agent
    config = getattr(runtime_agent, "app_config", None)
    return int(getattr(getattr(config, "web", None), "port", 18790) or 18790)


def _should_start_web_cron(agent: AgentLoop | None = None) -> bool:
    port = _resolve_web_runtime_port(agent)
    ownership = _process_owns_listen_port(port)
    if ownership is False:
        logger.debug(
            "Skipping web cron startup in pid={} because web port {} is owned by another process",
            os.getpid(),
            port,
        )
        return False
    return True


def _cron_runtime_ready(agent: AgentLoop | None = None) -> bool:
    runtime_agent = agent or _global_agent
    cron_service = getattr(runtime_agent, "cron_service", None) if runtime_agent is not None else None
    if cron_service is None:
        return True
    if not _should_start_web_cron(runtime_agent):
        return True
    status = getattr(cron_service, "status", None)
    if not callable(status):
        return False
    try:
        payload = status() or {}
    except Exception:
        return False
    return bool(payload.get("enabled"))


def _build_web_cron_service(agent_holder: dict[str, AgentLoop]) -> CronService:
    async def _on_job(job) -> str | None:
        runtime_agent = agent_holder.get("agent")
        if runtime_agent is None:
            raise RuntimeError("web cron runtime is not initialized")
        runtime_bridge = SessionRuntimeBridge(get_runtime_manager(runtime_agent))
        task_registrar = getattr(runtime_agent, "_register_active_task", None)
        bus = _global_bus
        return await dispatch_cron_job(
            job,
            runtime_bridge=runtime_bridge,
            session_manager=getattr(runtime_agent, "sessions", None),
            register_task=task_registrar if callable(task_registrar) else None,
            publish_outbound=bus.publish_outbound if bus is not None else None,
        )

    return CronService(get_data_dir() / "cron" / "jobs.json", on_job=_on_job)


def get_agent() -> AgentLoop:
    global _global_agent, _global_bus, _global_runtime_manager, _global_web_heartbeat
    if not get_bootstrap_security_service().is_unlocked():
        raise RuntimeError('project is locked')
    if not _global_agent:
        config, revision, _changed = get_runtime_config(force=True)
        provider = _make_provider(config, scope='ceo')

        _global_bus = MessageBus()
        debug_mode = debug_trace_enabled()
        if debug_mode:
            logger.info('Web API debug trace enabled (G3KU_DEBUG_TRACE=1)')
        agent_holder: dict[str, AgentLoop] = {}
        cron_service = _build_web_cron_service(agent_holder)
        _global_agent = _make_agent_loop(
            config,
            _global_bus,
            provider,
            debug_mode=debug_mode,
            cron_service=cron_service,
        )
        agent_holder["agent"] = _global_agent
        _global_agent._runtime_model_revision = revision
        _global_agent._runtime_default_model_key = config.resolve_role_model_key('ceo')
        _global_runtime_manager = SessionRuntimeManager(_global_agent)
        _global_web_heartbeat = build_web_session_heartbeat(
            _global_agent,
            _global_runtime_manager,
            reply_notifier=_make_heartbeat_reply_notifier(_global_agent, _global_runtime_manager),
        )
    elif _global_runtime_manager is None or _global_runtime_manager.loop is not _global_agent:
        _global_runtime_manager = SessionRuntimeManager(_global_agent)
        _global_web_heartbeat = build_web_session_heartbeat(
            _global_agent,
            _global_runtime_manager,
            reply_notifier=_make_heartbeat_reply_notifier(_global_agent, _global_runtime_manager),
        )
    elif _global_web_heartbeat is None:
        _global_web_heartbeat = build_web_session_heartbeat(
            _global_agent,
            _global_runtime_manager,
            reply_notifier=_make_heartbeat_reply_notifier(_global_agent, _global_runtime_manager),
        )
    return _global_agent


def _china_bridge_enabled(config) -> bool:
    bridge = getattr(config, 'china_bridge', None)
    return bool(bridge and getattr(bridge, 'enabled', False) and getattr(bridge, 'auto_start', False))


def _china_bridge_config_signature(config) -> str:
    bridge = getattr(config, 'china_bridge', None)
    if bridge is None:
        return ''
    if hasattr(bridge, 'model_dump'):
        payload = bridge.model_dump(by_alias=True, exclude_none=False)
    elif hasattr(bridge, '__dict__'):
        payload = {
            key: value
            for key, value in vars(bridge).items()
            if not key.startswith('_')
        }
    else:
        payload = {'value': str(bridge)}
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)


async def _cancel_background_task(task: asyncio.Task | None) -> None:
    if task is None:
        return
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def _stop_china_bridge_runtime() -> None:
    global _global_china_supervisor, _global_china_start_task
    await _cancel_background_task(_global_china_start_task)
    _global_china_start_task = None
    # The outbound drain intentionally survives bridge stop/restart: it also
    # serves external bridge sessions, and China messages hitting a missing
    # transport are dropped with a throttled warning instead of retry storms.
    # Only shutdown_web_runtime cancels it.
    if _global_china_supervisor is not None:
        await _global_china_supervisor.stop()
        _global_china_supervisor = None


async def _sync_china_bridge_services_after_runtime_refresh(runtime_agent: AgentLoop, config) -> None:
    current_signature = _china_bridge_config_signature(getattr(_global_china_supervisor, '_app_config', None))
    next_signature = _china_bridge_config_signature(config)

    global _global_china_transport
    if _global_china_transport is not None:
        _global_china_transport._app_config = config

    if not _china_bridge_enabled(config):
        await _stop_china_bridge_runtime()
        return

    if _global_china_supervisor is None:
        await _ensure_china_bridge_services(runtime_agent)
        return

    if current_signature == next_signature:
        return

    await _stop_china_bridge_runtime()
    await _start_china_bridge_services_now(runtime_agent, config)


async def refresh_web_agent_runtime(
    force: bool = False,
    reason: str = 'runtime',
    *,
    force_memory_sync: bool = False,
) -> bool:
    runtime_agent = get_agent()
    changed = refresh_loop_runtime_config(
        runtime_agent,
        force=force,
        reason=reason,
        force_memory_sync=force_memory_sync,
    )
    await _sync_china_bridge_services_after_runtime_refresh(runtime_agent, runtime_agent.app_config)
    return changed


def get_runtime_manager(agent: AgentLoop | None = None) -> SessionRuntimeManager:
    runtime_agent = agent or get_agent()
    global _global_runtime_manager
    if _global_runtime_manager is None or _global_runtime_manager.loop is not runtime_agent:
        _global_runtime_manager = SessionRuntimeManager(runtime_agent)
    return _global_runtime_manager


def _parse_runtime_chat_target(chat_id: str) -> dict[str, str] | None:
    raw = str(chat_id or '').strip()
    if not raw:
        return None
    parts = raw.split(':')
    if len(parts) < 3:
        return None
    account_id = str(parts[0] or '').strip() or 'default'
    scope = str(parts[1] or '').strip().lower()
    peer_id = str(parts[2] or '').strip()
    if not peer_id:
        return None
    thread_id = ''
    if len(parts) >= 5 and parts[3] == 'thread':
        thread_id = ':'.join(parts[4:]).strip()
    return {
        'account_id': account_id,
        'peer_kind': 'group' if scope == 'group' else 'user',
        'peer_id': peer_id,
        'thread_id': thread_id,
    }


def _route_from_session_message(message: object) -> dict[str, str] | None:
    if not isinstance(message, dict):
        return None
    metadata = message.get('metadata') if isinstance(message.get('metadata'), dict) else {}
    peer_id = str(metadata.get('_china_peer_id') or '').strip()
    if not peer_id:
        return None
    return {
        'account_id': str(metadata.get('_china_account_id') or 'default').strip() or 'default',
        'peer_kind': str(metadata.get('_china_peer_kind') or 'user').strip() or 'user',
        'peer_id': peer_id,
        'thread_id': str(metadata.get('_china_thread_id') or '').strip(),
        'event_id': str(metadata.get('_china_event_id') or '').strip(),
        'message_id': str(metadata.get('message_id') or '').strip(),
    }


def _resolve_china_heartbeat_route(
    session_id: str,
    *,
    agent: AgentLoop | None = None,
    runtime_manager: SessionRuntimeManager | None = None,
) -> dict[str, str] | None:
    parsed = parse_china_session_key(session_id)
    if parsed is None:
        return None

    current_runtime_manager = runtime_manager or (_global_runtime_manager if _global_runtime_manager is not None else None)
    current_agent = agent or _global_agent
    resolved: dict[str, str] | None = None

    if current_runtime_manager is not None:
        meta = current_runtime_manager.session_meta(session_id)
        if isinstance(meta, tuple) and len(meta) == 2:
            runtime_channel = str(meta[0] or '').strip() or parsed.channel
            target = _parse_runtime_chat_target(str(meta[1] or ''))
            if target is not None:
                resolved = {
                    'channel': runtime_channel,
                    'chat_id': str(meta[1] or '').strip(),
                    **target,
                }

    if current_agent is not None:
        session_manager = getattr(current_agent, 'sessions', None)
        if session_manager is not None and hasattr(session_manager, 'get_or_create'):
            try:
                session = session_manager.get_or_create(session_id)
            except Exception:
                session = None
            if session is not None:
                for message in reversed(list(getattr(session, 'messages', []) or [])):
                    route = _route_from_session_message(message)
                    if route is None:
                        continue
                    if resolved is None:
                        resolved = {
                            'channel': parsed.channel,
                            'chat_id': build_chat_id(
                                account_id=route['account_id'],
                                peer_kind=route['peer_kind'],
                                peer_id=route['peer_id'],
                                thread_id=route['thread_id'] or None,
                            ),
                            **route,
                        }
                    else:
                        for key_name in ('account_id', 'peer_kind', 'peer_id', 'thread_id', 'event_id', 'message_id'):
                            if not str(resolved.get(key_name) or '').strip():
                                resolved[key_name] = str(route.get(key_name) or '').strip()
                    break

    if resolved is not None:
        return resolved

    if parsed.peer_id:
        peer_kind = 'group' if parsed.chat_type == 'group' else 'user'
        return {
            'channel': parsed.channel,
            'chat_id': build_chat_id(
                account_id=parsed.account_id,
                peer_kind=peer_kind,
                peer_id=parsed.peer_id,
                thread_id=parsed.thread_id,
            ),
            'account_id': parsed.account_id,
            'peer_kind': peer_kind,
            'peer_id': parsed.peer_id,
            'thread_id': str(parsed.thread_id or '').strip(),
            'event_id': '',
            'message_id': '',
        }

    return None


async def _notify_external_channel_reply(session_id: str, text: str) -> None:
    """Publish a heartbeat/cron/task-terminal reply for an external bridge
    session onto the outbound bus; the shared drain resolves the session via
    the external registry and routes it to the session event hub as
    ``outbound.created``."""
    bus = _global_bus
    if bus is None:
        return
    await bus.publish_outbound(
        OutboundMessage(
            channel=EXTERNAL_OUTBOUND_CHANNEL,
            chat_id=session_id,
            content=text,
            metadata={'source': 'heartbeat', 'session_key': session_id},
        )
    )


async def _notify_heartbeat_channel_reply(
    session_id: str,
    text: str,
    *,
    agent: AgentLoop | None = None,
    runtime_manager: SessionRuntimeManager | None = None,
) -> None:
    key = str(session_id or '').strip()
    payload = str(text or '').strip()
    if not payload:
        return
    if key.startswith(EXTERNAL_SESSION_KEY_PREFIX):
        await _notify_external_channel_reply(key, payload)
        return
    if not key.startswith('china:'):
        return
    bus = _global_bus
    if bus is None:
        return
    route = _resolve_china_heartbeat_route(key, agent=agent, runtime_manager=runtime_manager)
    if route is None:
        logger.debug('heartbeat china reply skipped: route unavailable for {}', key)
        return
    metadata = {
        'source': 'heartbeat',
        'session_key': key,
        '_china_account_id': route['account_id'],
        '_china_peer_kind': route['peer_kind'],
        '_china_peer_id': route['peer_id'],
    }
    event_id = str(route.get('event_id') or '').strip()
    if event_id:
        metadata['_china_event_id'] = event_id
    message_id = str(route.get('message_id') or '').strip()
    if message_id:
        metadata['message_id'] = message_id
    thread_id = str(route.get('thread_id') or '').strip()
    if thread_id:
        metadata['_china_thread_id'] = thread_id
    await bus.publish_outbound(
        OutboundMessage(
            channel=str(route.get('channel') or '').strip() or 'qqbot',
            chat_id=str(route.get('chat_id') or '').strip(),
            content=payload,
            reply_to=message_id or None,
            metadata=metadata,
        )
    )


def _make_heartbeat_reply_notifier(
    runtime_agent: AgentLoop,
    runtime_manager: SessionRuntimeManager,
):
    async def _notify(session_id: str, text: str) -> None:
        await _notify_heartbeat_channel_reply(
            session_id,
            text,
            agent=runtime_agent,
            runtime_manager=runtime_manager,
        )

    return _notify


def _get_china_transport(agent: AgentLoop | None = None) -> ChinaBridgeTransport:
    runtime_agent = agent or get_agent()
    runtime_manager = get_runtime_manager(runtime_agent)
    global _global_china_transport
    if _global_china_transport is None:
        task_registrar = getattr(runtime_agent, '_register_active_task', None)
        _global_china_transport = ChinaBridgeTransport(
            runtime_bridge=SessionRuntimeBridge(runtime_manager),
            app_config=get_runtime_config(force=False)[0],
            register_task=task_registrar if callable(task_registrar) else None,
        )
    else:
        _global_china_transport._app_config = get_runtime_config(force=False)[0]
    return _global_china_transport


_CHINA_OUTBOUND_RETRY_LOG_INTERVAL_S = 30.0


def _start_outbound_drain(bus: MessageBus) -> asyncio.Task:
    """Create the outbound drain task and return it.

    The drain is the only bridge between the in-process outbound bus and the
    channel consumers (China host transport and external bridge event hubs),
    so it must never die silently: a crashed drain strands every later
    message (e.g. cron reminders) in the queue forever with no log. Its
    lifecycle is independent of the China bridge enable switch so external
    outbound keeps flowing while the legacy subsystem is disabled.

    Routing:
    - ``channel == "ext"``: resolve ``chat_id`` (a session key or a
      registered external_key) through the external session registry and
      publish ``outbound.created`` on that session's event hub. Unknown
      targets are dropped with a warning; internal-only (post-sanitize
      empty) text is acked silently, mirroring the transport contract.
    - ``channel in CHINA_CHANNELS``: hand to the China transport. When the
      transport is absent (bridge disabled) the message is dropped with a
      throttled warning instead of retrying forever.

    Failure handling (China send path):
    - ``RuntimeError`` from the send path (control WebSocket not connected
      yet, or dropped mid-reconnect) is transient: keep the message and retry
      with a backoff instead of losing it.
    - Any other exception is treated as a poison message: log and drop only
      that message, then keep draining.
    - ``CancelledError`` still terminates the task (shutdown/restart path).
    """

    async def _route_external_outbound(pending: OutboundMessage) -> None:
        sanitized = sanitize_channel_outbound_text(str(getattr(pending, "content", "") or ""))
        if not sanitized:
            return
        entry = get_external_session_registry().find_by_any_key(getattr(pending, "chat_id", None))
        if entry is None:
            logger.warning(
                "external outbound drain dropped message: unknown target chat_id={}",
                getattr(pending, "chat_id", "?"),
            )
            return
        payload: dict[str, Any] = {
            "text": sanitized,
            "external_key": entry.external_key,
            "session_key": entry.session_key,
        }
        reply_to = str(getattr(pending, "reply_to", "") or "").strip()
        if reply_to:
            payload["reply_to"] = reply_to
        dedupe_key = str((getattr(pending, "metadata", None) or {}).get("dedupe_key") or "").strip()
        if dedupe_key:
            payload["dedupe_key"] = dedupe_key
        get_session_event_hub(entry.session_key).publish("outbound.created", **payload)
        logger.debug("external outbound drained: session={}", entry.session_key)

    async def _drain_outbound() -> None:
        pending: OutboundMessage | None = None
        last_retry_log = float("-inf")
        while True:
            try:
                if pending is None:
                    try:
                        pending = await asyncio.wait_for(bus.consume_outbound(), timeout=1.0)
                    except asyncio.TimeoutError:
                        continue
                if pending.channel == EXTERNAL_OUTBOUND_CHANNEL:
                    await _route_external_outbound(pending)
                    pending = None
                    continue
                if pending.channel not in CHINA_CHANNELS:
                    # Non-China/non-ext outbound must never reach this drain;
                    # if it does the publisher almost certainly resolved the
                    # wrong channel (e.g. poisoned session meta). Surface it
                    # instead of dropping silently.
                    logger.warning(
                        "china outbound drain skipped non-china message channel={} chat_id={}",
                        pending.channel,
                        pending.chat_id,
                    )
                    pending = None
                    continue
                transport = _global_china_transport
                if transport is None:
                    now = asyncio.get_running_loop().time()
                    if now - last_retry_log >= _CHINA_OUTBOUND_RETRY_LOG_INTERVAL_S:
                        logger.warning(
                            "china outbound drain dropped message (china bridge transport unavailable) channel={} chat_id={}",
                            pending.channel,
                            pending.chat_id,
                        )
                        last_retry_log = now
                    pending = None
                    continue
                await transport.send_outbound(pending)
                logger.debug(
                    "china outbound drained: channel={} chat_id={}",
                    pending.channel,
                    pending.chat_id,
                )
                pending = None
            except asyncio.CancelledError:
                raise
            except RuntimeError as exc:
                now = asyncio.get_running_loop().time()
                if now - last_retry_log >= _CHINA_OUTBOUND_RETRY_LOG_INTERVAL_S:
                    logger.warning(
                        "china outbound drain waiting for bridge connection; will retry: {}",
                        exc,
                    )
                    last_retry_log = now
                await asyncio.sleep(1.0)
            except Exception as exc:
                logger.error(
                    "china outbound drain dropped message channel={} chat_id={}: {}",
                    getattr(pending, "channel", "?"),
                    getattr(pending, "chat_id", "?"),
                    exc,
                )
                pending = None

    return asyncio.create_task(_drain_outbound())


async def _start_china_bridge_services_now(runtime_agent: AgentLoop, config) -> None:
    bus = _global_bus
    if bus is None:
        return
    transport = _get_china_transport(runtime_agent)
    global _global_china_supervisor
    if _global_china_supervisor is None:
        _global_china_supervisor = ChinaBridgeSupervisor(
            app_config=config,
            workspace=config.workspace_path,
            transport=transport,
        )
    await _global_china_supervisor.start()


def _ensure_outbound_drain_running() -> None:
    """Start the shared outbound drain once the bus exists. Independent of
    the China bridge enable switch (external outbound must keep flowing)."""
    global _global_china_outbound_task
    if _global_bus is None:
        return
    if _global_china_outbound_task is None or _global_china_outbound_task.done():
        _global_china_outbound_task = _start_outbound_drain(_global_bus)


async def _await_current_web_process_then_start_china_bridge(runtime_agent: AgentLoop, config, web_port: int) -> None:
    current_pid = os.getpid()
    while True:
        owners = _listen_port_owners(web_port)
        if owners is None or current_pid in owners:
            await _start_china_bridge_services_now(runtime_agent, config)
            return
        if owners and current_pid not in owners:
            logger.debug(
                'Skipping china bridge startup in pid={} because web port {} is owned by pid(s) {}',
                current_pid,
                web_port,
                ','.join(str(pid) for pid in sorted(owners)),
            )
            return
        await asyncio.sleep(0.25)


async def _ensure_china_bridge_services(agent: AgentLoop | None = None) -> None:
    runtime_agent = agent or get_agent()
    config = get_runtime_config(force=False)[0]
    if not bool(getattr(config, 'china_bridge', None) and config.china_bridge.enabled and config.china_bridge.auto_start):
        return
    web_port = int(getattr(getattr(config, 'web', None), 'port', 18790) or 18790)
    global _global_china_start_task
    if _global_china_start_task is None or _global_china_start_task.done():
        _global_china_start_task = asyncio.create_task(
            _await_current_web_process_then_start_china_bridge(runtime_agent, config, web_port)
        )


_SHUTDOWN_PAUSE_DRAIN_TIMEOUT_S = 10.0
_SHUTDOWN_PAUSE_DRAIN_POLL_S = 0.1


async def wait_shutdown_pause_commands_drained(
    service: Any,
    *,
    task_ids: set[str],
    timeout_s: float = _SHUTDOWN_PAUSE_DRAIN_TIMEOUT_S,
) -> bool:
    """Wait until the worker has really applied the shutdown pause commands.

    Durable pause flags are written by the web process synchronously, but the
    worker's actor keeps running until it processes the `pause_task` command
    (which awaits the actor unwinding). Poll the shared command table so
    shutdown does not declare victory — or kill the worker — while a model
    call or long tool is still executing in the background.
    """
    normalized_ids = {str(item or '').strip() for item in list(task_ids or []) if str(item or '').strip()}
    store = getattr(service, 'store', None)
    list_unfinished = getattr(store, 'list_unfinished_task_commands', None)
    if not normalized_ids or not callable(list_unfinished):
        return True
    worker_state = getattr(service, 'worker_state', None)
    deadline = asyncio.get_running_loop().time() + max(0.0, float(timeout_s or 0.0))
    while asyncio.get_running_loop().time() < deadline:
        try:
            unfinished = [
                item
                for item in list_unfinished()
                if str(item.get('command_type') or '').strip() == 'pause_task'
                and str(item.get('task_id') or '').strip() in normalized_ids
            ]
        except Exception:
            return True
        if not unfinished:
            return True
        if callable(worker_state):
            try:
                state = str(worker_state() or '').strip().lower()
            except Exception:
                state = ''
            if state in {'stopped', 'offline', 'dead'}:
                # No live worker will drain these commands; the durable flags
                # plus the shutdown ledger already cover the restart.
                return True
            # 'online' / 'starting' / 'stale' keep waiting: a stale row may
            # still belong to a live but busy worker.
        await asyncio.sleep(_SHUTDOWN_PAUSE_DRAIN_POLL_S)
    logger.warning(
        "shutdown pause drain timed out after {:.1f}s; {} pause commander(s) still unfinished",
        float(timeout_s or 0.0),
        len(normalized_ids),
    )
    return False


async def pause_running_work_for_shutdown(
    agent: AgentLoop | None = None,
    runtime_manager: SessionRuntimeManager | None = None,
) -> dict[str, int]:
    """Pause every running session and in-progress task before a process exit.

    Manual pause archives each in-flight turn durably; task pause takes effect
    at the next safe boundary and is persisted immediately. Both kinds are
    recorded in the shutdown-pause ledger so the next startup can resume them
    silently instead of treating them as abnormal stops. User/agent-initiated
    pauses are deliberately not recorded and therefore never auto-resumed.
    """
    runtime_agent = agent if agent is not None else _global_agent
    current_manager = runtime_manager if runtime_manager is not None else _global_runtime_manager
    service = getattr(runtime_agent, "main_task_service", None) if runtime_agent is not None else None
    if service is not None:
        try:
            startup = getattr(service, "startup", None)
            if callable(startup):
                await startup()
        except Exception:
            logger.debug("main task service startup skipped during shutdown pause")
    store = getattr(service, "store", None) if service is not None else None

    paused_sessions = 0
    if current_manager is not None:
        for session_key in list(current_manager.list_sessions()):
            session = current_manager.get(session_key)
            if not SessionRuntimeBridge.session_is_running(session):
                continue
            try:
                await session.pause(manual=True)
            except Exception:
                logger.debug("session pause skipped during shutdown for {}", session_key)
                continue
            if store is not None and callable(getattr(store, "record_shutdown_pause_entry", None)):
                channel, chat_id = current_manager.session_meta(session_key) or ("", "")
                try:
                    store.record_shutdown_pause_entry(
                        kind="session",
                        ref_id=session_key,
                        channel=channel,
                        chat_id=chat_id,
                    )
                except Exception:
                    logger.debug("shutdown pause ledger write skipped for session {}", session_key)
            paused_sessions += 1

    paused_tasks = 0
    paused_task_ids: set[str] = set()
    if service is not None:
        for task in list(getattr(service.store, "list_tasks", lambda: [])() or []):
            status = str(getattr(task, "status", "") or "").strip().lower()
            if status != "in_progress" or bool(getattr(task, "is_paused", False)):
                continue
            task_id = str(getattr(task, "task_id", "") or "").strip()
            try:
                pause_impl = getattr(service, "force_pause_task_durably", None)
                if callable(pause_impl):
                    await pause_impl(task_id)
                else:
                    await service.pause_task(task_id)
            except Exception:
                logger.debug("task pause skipped during shutdown for {}", task_id)
                continue
            paused_task_ids.add(task_id)
            if store is not None and callable(getattr(store, "record_shutdown_pause_entry", None)):
                try:
                    store.record_shutdown_pause_entry(kind="task", ref_id=task_id)
                except Exception:
                    logger.debug("shutdown pause ledger write skipped for task {}", task_id)
            paused_tasks += 1
        if paused_task_ids:
            # The durable flags are set; wait for the worker to truly stop the
            # running actors (pause commands finished) before the process exits.
            await wait_shutdown_pause_commands_drained(service, task_ids=paused_task_ids)

    return {"paused_sessions": paused_sessions, "paused_tasks": paused_tasks}


async def resume_shutdown_paused_sessions(
    agent: AgentLoop | None = None,
    runtime_manager: SessionRuntimeManager | None = None,
    heartbeat: Any | None = None,
) -> int:
    """Auto-resume sessions that were paused by the previous graceful shutdown.

    Runs once after the heartbeat service is up: each ledger row wakes its
    session through the heartbeat internal-turn lane so the paused user
    request is reconciled back into the request seed and the model continues
    the interrupted work. Rows whose session no longer exists are retired.
    """
    runtime_agent = agent if agent is not None else _global_agent
    current_manager = runtime_manager if runtime_manager is not None else _global_runtime_manager
    heartbeat_service = heartbeat if heartbeat is not None else _global_web_heartbeat
    if runtime_agent is None or current_manager is None or heartbeat_service is None:
        return 0
    service = getattr(runtime_agent, "main_task_service", None)
    store = getattr(service, "store", None) if service is not None else None
    if store is None:
        return 0
    list_entries = getattr(store, "list_shutdown_pause_entries", None)
    consume = getattr(store, "mark_shutdown_pause_entry_consumed", None)
    enqueue = getattr(heartbeat_service, "enqueue_shutdown_resume", None)
    if not callable(list_entries) or not callable(consume) or not callable(enqueue):
        return 0
    session_manager = getattr(runtime_agent, "sessions", None)
    resumed = 0
    for entry in list_entries(kind="session"):
        session_key = str(entry.get("ref_id") or "").strip()
        if not session_key:
            consume(kind="session", ref_id=session_key)
            continue
        exists = False
        if session_manager is not None and callable(getattr(session_manager, "get_path", None)):
            try:
                exists = bool(getattr(session_manager, "get_path")(session_key).exists())
            except Exception:
                exists = False
        if not exists:
            consume(kind="session", ref_id=session_key)
            continue
        # Recreate the runtime session so its frontdoor continuity baseline is
        # restored before the wake turn runs.
        channel = str(entry.get("channel") or "").strip() or "web"
        chat_id = str(entry.get("chat_id") or "").strip() or session_key
        current_manager.get_or_create(session_key=session_key, channel=channel, chat_id=chat_id)
        if enqueue(session_key):
            resumed += 1
        consume(kind="session", ref_id=session_key)
    if resumed:
        logger.info("auto-resumed {} session(s) paused by shutdown", resumed)
    return resumed


def get_web_heartbeat_service(agent: AgentLoop | None = None):
    runtime_agent = agent or get_agent()
    runtime_manager = get_runtime_manager(runtime_agent)
    global _global_web_heartbeat
    _global_web_heartbeat = build_web_session_heartbeat(
        runtime_agent,
        runtime_manager,
        reply_notifier=_make_heartbeat_reply_notifier(runtime_agent, runtime_manager),
    )
    return _global_web_heartbeat


def describe_web_runtime_services(agent: AgentLoop | None = None) -> dict[str, bool]:
    runtime_agent = agent or _global_agent
    main_task_service = getattr(runtime_agent, 'main_task_service', None) if runtime_agent is not None else None
    heartbeat = _global_web_heartbeat
    main_runtime_ready = bool(main_task_service is not None and getattr(main_task_service, '_started', False))
    heartbeat_ready = bool(heartbeat is not None and getattr(heartbeat, '_started', False))
    bootstrapping = _get_runtime_services_lock().locked()
    ready = bool(runtime_agent is not None and main_runtime_ready and heartbeat_ready and not bootstrapping)
    return {
        'agent_ready': runtime_agent is not None,
        'main_runtime_ready': main_runtime_ready,
        'heartbeat_ready': heartbeat_ready,
        'bootstrapping': bootstrapping,
        'ready': ready,
    }


async def ensure_web_runtime_services(agent: AgentLoop | None = None) -> None:
    global _global_web_heartbeat
    if describe_web_runtime_services(agent).get('ready') and _cron_runtime_ready(agent):
        return

    async with _get_runtime_services_lock():
        runtime_agent = agent or get_agent()
        if describe_web_runtime_services(runtime_agent).get('ready') and _cron_runtime_ready(runtime_agent):
            return

        main_task_service = getattr(runtime_agent, 'main_task_service', None)
        if main_task_service is not None:
            await main_task_service.startup()
            # Avoid blocking unlock on worker warmup; the UI can surface worker readiness separately.
            await ensure_managed_task_worker(main_task_service, wait_timeout_s=1.0)
        heartbeat = await start_web_session_heartbeat(
            runtime_agent,
            get_runtime_manager(runtime_agent),
            replay_pending_outbox=True,
            reply_notifier=_make_heartbeat_reply_notifier(runtime_agent, get_runtime_manager(runtime_agent)),
        )
        if heartbeat is not None:
            _global_web_heartbeat = heartbeat
        cron_service = getattr(runtime_agent, "cron_service", None)
        if cron_service is not None and _should_start_web_cron(runtime_agent) and not _cron_runtime_ready(runtime_agent):
            await cron_service.start()
        _ensure_outbound_drain_running()
        await _ensure_china_bridge_services(runtime_agent)
        try:
            await resume_shutdown_paused_sessions(runtime_agent, get_runtime_manager(runtime_agent), _global_web_heartbeat)
        except Exception:
            logger.debug("shutdown-paused session resume skipped during startup")


async def shutdown_web_runtime() -> None:
    global _global_agent, _global_bus, _global_runtime_manager, _global_web_heartbeat
    global _global_china_transport, _global_china_supervisor, _global_china_outbound_task, _global_china_start_task

    agent = _global_agent
    runtime_manager = _global_runtime_manager
    heartbeat = _global_web_heartbeat
    cron_service = getattr(agent, "cron_service", None) if agent is not None else None
    china_supervisor = _global_china_supervisor
    china_outbound_task = _global_china_outbound_task
    china_start_task = _global_china_start_task

    _global_agent = None
    _global_bus = None
    _global_runtime_manager = None
    _global_web_heartbeat = None
    _global_china_transport = None
    _global_china_supervisor = None
    _global_china_outbound_task = None
    _global_china_start_task = None

    if agent is None:
        return

    # Freeze all running work first (durably, with a startup-resume ledger row)
    # so no matter how this process exit was triggered, tasks and sessions
    # restart cleanly instead of being reported as abnormal stops.
    try:
        paused = await pause_running_work_for_shutdown(agent, runtime_manager)
        if paused.get("paused_sessions") or paused.get("paused_tasks"):
            logger.info(
                "graceful shutdown paused {} session(s) and {} task(s)",
                paused.get("paused_sessions", 0),
                paused.get("paused_tasks", 0),
            )
    except Exception:
        logger.debug("shutdown pause-all skipped during shutdown")

    if cron_service is not None:
        try:
            cron_service.stop()
        except Exception:
            logger.debug('web cron stop skipped during shutdown')

    if heartbeat is not None:
        try:
            await heartbeat.stop()
        except Exception:
            logger.debug('web heartbeat stop skipped during shutdown')

    if china_outbound_task is not None:
        china_outbound_task.cancel()
        await asyncio.gather(china_outbound_task, return_exceptions=True)

    if china_start_task is not None:
        china_start_task.cancel()
        await asyncio.gather(china_start_task, return_exceptions=True)

    if china_supervisor is not None:
        try:
            await china_supervisor.stop()
        except Exception:
            logger.debug('china bridge supervisor stop skipped during shutdown')

    session_keys: set[str] = set()
    if runtime_manager is not None:
        try:
            session_keys.update(key for key in runtime_manager.list_sessions() if str(key or '').strip())
        except Exception:
            logger.debug('Runtime manager session enumeration skipped during shutdown')
    try:
        active_tasks = getattr(agent, '_active_tasks', None)
        if isinstance(active_tasks, dict):
            session_keys.update(key for key in active_tasks.keys() if str(key or '').strip())
    except Exception:
        logger.debug('Active session enumeration skipped during shutdown')

    for session_key in sorted(session_keys):
        try:
            await agent.cancel_session_tasks(session_key)
        except Exception:
            logger.debug('Session cancel skipped during shutdown for {}', session_key)

    pool = getattr(agent, 'background_pool', None)
    if pool is not None and hasattr(pool, 'close'):
        try:
            await pool.close()
        except Exception:
            logger.debug('Background pool close skipped during shutdown')

    main_task_service = getattr(agent, 'main_task_service', None)
    if main_task_service is not None:
        try:
            await main_task_service.close()
        except Exception:
            logger.debug('main task service close skipped during shutdown')

    if main_task_service is not None and not keep_worker_enabled() and managed_worker_pid() is not None:
        # The managed worker is about to be killed without running its own
        # close(); drop its lease now so an immediate restart does not collide
        # with the stale-lease window (lease rows outlive the worker by TTL).
        try:
            main_task_service.release_task_worker_lease_durably()
        except Exception:
            logger.debug('task worker lease release skipped during shutdown')

    try:
        await shutdown_managed_task_worker()
    except Exception:
        logger.debug('managed task worker stop skipped during shutdown')

    try:
        await agent.close_mcp()
    except Exception:
        logger.debug('Agent runtime close skipped during shutdown')


def run_web_shell(*, host: str | None, port: int | None, reload: bool, debug: bool, set_debug_mode, with_worker: bool = True) -> None:
    set_debug_mode(debug)
    run_web_server_entrypoint(
        host=host,
        port=port,
        reload=reload,
        log_level='debug' if debug else 'info',
        with_worker=with_worker,
    )


__all__ = [
    'describe_web_runtime_services',
    'debug_trace_enabled',
    'ensure_web_runtime_services',
    'get_agent',
    'is_no_ceo_model_configured_error',
    'get_runtime_manager',
    'get_web_heartbeat_service',
    'no_ceo_model_configured_payload',
    'pause_running_work_for_shutdown',
    'refresh_web_agent_runtime',
    'resume_shutdown_paused_sessions',
    'run_web_shell',
    'shutdown_web_runtime',
    'wait_shutdown_pause_commands_drained',
]
