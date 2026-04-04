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
from g3ku.china_bridge.session_keys import build_chat_id, parse_china_session_key
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
from g3ku.web.worker_control import ensure_managed_task_worker, shutdown_managed_task_worker
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
        return await dispatch_cron_job(
            job,
            runtime_bridge=runtime_bridge,
            session_manager=getattr(runtime_agent, "sessions", None),
            register_task=task_registrar if callable(task_registrar) else None,
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
    global _global_china_supervisor, _global_china_outbound_task, _global_china_start_task
    await _cancel_background_task(_global_china_start_task)
    _global_china_start_task = None
    await _cancel_background_task(_global_china_outbound_task)
    _global_china_outbound_task = None
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


async def refresh_web_agent_runtime(force: bool = False, reason: str = 'runtime') -> bool:
    runtime_agent = get_agent()
    changed = refresh_loop_runtime_config(runtime_agent, force=force, reason=reason)
    forced_sync = False
    if force:
        forced_sync = _force_web_runtime_sync(runtime_agent, reason=reason)
    await _sync_china_bridge_services_after_runtime_refresh(runtime_agent, runtime_agent.app_config)
    return bool(changed or forced_sync)


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


async def _notify_heartbeat_channel_reply(
    session_id: str,
    text: str,
    *,
    agent: AgentLoop | None = None,
    runtime_manager: SessionRuntimeManager | None = None,
) -> None:
    key = str(session_id or '').strip()
    payload = str(text or '').strip()
    if not key.startswith('china:') or not payload:
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


async def _start_china_bridge_services_now(runtime_agent: AgentLoop, config) -> None:
    bus = _global_bus
    if bus is None:
        return
    transport = _get_china_transport(runtime_agent)
    global _global_china_supervisor, _global_china_outbound_task
    if _global_china_supervisor is None:
        _global_china_supervisor = ChinaBridgeSupervisor(
            app_config=config,
            workspace=config.workspace_path,
            transport=transport,
        )
    await _global_china_supervisor.start()
    if _global_china_outbound_task is None or _global_china_outbound_task.done():
        async def _drain_outbound() -> None:
            while True:
                try:
                    msg = await asyncio.wait_for(bus.consume_outbound(), timeout=1.0)
                    if msg.channel in CHINA_CHANNELS:
                        await transport.send_outbound(msg)
                except asyncio.TimeoutError:
                    continue
                except asyncio.CancelledError:
                    break

        _global_china_outbound_task = asyncio.create_task(_drain_outbound())


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


def _force_web_runtime_sync(agent: AgentLoop | None = None, *, reason: str = 'runtime') -> bool:
    runtime_agent = agent or get_agent()
    synced = False
    resource_manager = getattr(runtime_agent, 'resource_manager', None)
    if resource_manager is not None and hasattr(resource_manager, 'reload_now'):
        resource_manager.reload_now(trigger=reason)
        synced = True

    bootstrap = getattr(runtime_agent, '_bootstrap', None)
    if bootstrap is not None and hasattr(bootstrap, 'sync_internal_tool_runtimes'):
        synced = bool(bootstrap.sync_internal_tool_runtimes(force=True, reason=reason)) or synced

    service = getattr(runtime_agent, 'main_task_service', None)
    if service is not None:
        if resource_manager is not None and hasattr(service, 'bind_resource_manager'):
            service.bind_resource_manager(resource_manager)
        registry = getattr(service, 'resource_registry', None)
        if registry is not None and hasattr(registry, 'refresh_from_current_resources'):
            registry.refresh_from_current_resources()
            synced = True
        reconcile = getattr(service, 'reconcile_core_tool_families', None)
        if callable(reconcile):
            synced = bool(reconcile()) or synced
        policy_engine = getattr(service, 'policy_engine', None)
        if policy_engine is not None and hasattr(policy_engine, 'sync_default_role_policies'):
            policy_engine.sync_default_role_policies()
            synced = True

    if hasattr(runtime_agent, '_ceo_model_chain_cache_key'):
        runtime_agent._ceo_model_chain_cache_key = None
    if hasattr(runtime_agent, '_ceo_model_client_cache'):
        runtime_agent._ceo_model_client_cache = None
    return synced


async def ensure_web_runtime_services(agent: AgentLoop | None = None) -> None:
    global _global_web_heartbeat
    if describe_web_runtime_services(agent).get('ready') and _cron_runtime_ready(agent):
        return

    async with _get_runtime_services_lock():
        runtime_agent = agent or get_agent()
        if describe_web_runtime_services(runtime_agent).get('ready') and _cron_runtime_ready(runtime_agent):
            return

        _force_web_runtime_sync(runtime_agent, reason='web_runtime_services_startup')

        main_task_service = getattr(runtime_agent, 'main_task_service', None)
        if main_task_service is not None:
            await main_task_service.startup()
            # Avoid blocking unlock on worker warmup; the UI can surface worker readiness separately.
            await ensure_managed_task_worker(main_task_service, wait_timeout_s=5.0)
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
        await _ensure_china_bridge_services(runtime_agent)


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

    try:
        await shutdown_managed_task_worker()
    except Exception:
        logger.debug('managed task worker stop skipped during shutdown')

    try:
        await agent.close_mcp()
    except Exception:
        logger.debug('Agent runtime close skipped during shutdown')


def run_web_shell(*, host: str | None, port: int | None, reload: bool, debug: bool, set_debug_mode) -> None:
    set_debug_mode(debug)
    run_web_server_entrypoint(
        host=host,
        port=port,
        reload=reload,
        log_level='debug' if debug else 'info',
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
    'refresh_web_agent_runtime',
    'run_web_shell',
    'shutdown_web_runtime',
]
