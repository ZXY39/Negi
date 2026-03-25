"""Web shell runtime bootstrap for the converged runtime architecture."""

from __future__ import annotations

import os
import asyncio
import subprocess
import sys
from typing import Optional

from loguru import logger

from g3ku.agent.loop import AgentLoop
from g3ku.bus.queue import MessageBus
from g3ku.china_bridge import ChinaBridgeSupervisor, ChinaBridgeTransport
from g3ku.cli.commands import _make_provider
from g3ku.config.live_runtime import get_runtime_config
from g3ku.heartbeat.bootstrap import build_web_session_heartbeat, start_web_session_heartbeat
from g3ku.runtime import SessionRuntimeBridge
from g3ku.runtime import SessionRuntimeManager
from g3ku.runtime.config_refresh import refresh_loop_runtime_config
from g3ku.security import get_bootstrap_security_service
from g3ku.web.worker_control import ensure_managed_task_worker, shutdown_managed_task_worker
from main.protocol import now_iso

_global_agent: Optional[AgentLoop] = None
_global_bus: Optional[MessageBus] = None
_global_runtime_manager: Optional[SessionRuntimeManager] = None
_global_web_heartbeat = None
_global_china_transport: Optional[ChinaBridgeTransport] = None
_global_china_supervisor: Optional[ChinaBridgeSupervisor] = None
_global_china_outbound_task: Optional[asyncio.Task] = None
_global_china_start_task: Optional[asyncio.Task] = None
_global_runtime_services_lock: Optional[asyncio.Lock] = None


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


def get_agent() -> AgentLoop:
    global _global_agent, _global_bus, _global_runtime_manager, _global_web_heartbeat
    if not get_bootstrap_security_service().is_unlocked():
        raise RuntimeError('project is locked')
    if not _global_agent:
        config, revision, _changed = get_runtime_config(force=True)
        provider_name, model_name = config.get_role_model_target('ceo')
        provider = _make_provider(config, scope='ceo')
        middlewares = []
        try:
            from g3ku.agent.middleware import build_middlewares
        except ModuleNotFoundError:
            if config.agents.defaults.middlewares:
                logger.warning(
                    'Runtime middleware unavailable in web mode because optional langchain middleware package is missing; '
                    'starting without custom middleware.'
                )
        else:
            try:
                middlewares = build_middlewares(config.agents.defaults.middlewares)
            except ValueError as exc:
                logger.error('Invalid middleware config in web mode: {}', exc)
                middlewares = []

        _global_bus = MessageBus()
        debug_mode = debug_trace_enabled()
        if debug_mode:
            logger.info('Web API debug trace enabled (G3KU_DEBUG_TRACE=1)')
        _global_agent = AgentLoop(
            bus=_global_bus,
            provider=provider,
            workspace=config.workspace_path,
            model=model_name,
            provider_name=provider_name,
            temperature=config.agents.defaults.temperature,
            max_tokens=config.agents.defaults.max_tokens,
            max_iterations=config.get_role_max_iterations('ceo'),
            memory_window=config.agents.defaults.memory_window,
            reasoning_effort=config.agents.defaults.reasoning_effort,
            multi_agent_config=config.agents.multi_agent,
            app_config=config,
            resource_config=config.resources,
            channels_config=config.china_bridge,
            debug_mode=debug_mode,
            middlewares=middlewares,
        )
        _global_agent._runtime_model_revision = revision
        _global_agent._runtime_default_model_key = config.resolve_role_model_key('ceo')
        _global_runtime_manager = SessionRuntimeManager(_global_agent)
        _global_web_heartbeat = build_web_session_heartbeat(_global_agent, _global_runtime_manager)
    elif _global_runtime_manager is None or _global_runtime_manager.loop is not _global_agent:
        _global_runtime_manager = SessionRuntimeManager(_global_agent)
        _global_web_heartbeat = build_web_session_heartbeat(_global_agent, _global_runtime_manager)
    elif _global_web_heartbeat is None:
        _global_web_heartbeat = build_web_session_heartbeat(_global_agent, _global_runtime_manager)
    return _global_agent


async def refresh_web_agent_runtime(force: bool = False, reason: str = 'runtime') -> bool:
    return refresh_loop_runtime_config(get_agent(), force=force, reason=reason)


def get_runtime_manager(agent: AgentLoop | None = None) -> SessionRuntimeManager:
    runtime_agent = agent or get_agent()
    global _global_runtime_manager
    if _global_runtime_manager is None or _global_runtime_manager.loop is not runtime_agent:
        _global_runtime_manager = SessionRuntimeManager(runtime_agent)
    return _global_runtime_manager


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
                    if msg.channel in {"qqbot", "dingtalk", "wecom", "wecom-app", "feishu-china"}:
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
    web_port = int(getattr(getattr(config, 'gateway', None), 'port', 18790) or 18790)
    global _global_china_start_task
    if _global_china_start_task is None or _global_china_start_task.done():
        _global_china_start_task = asyncio.create_task(
            _await_current_web_process_then_start_china_bridge(runtime_agent, config, web_port)
        )


def get_web_heartbeat_service(agent: AgentLoop | None = None):
    runtime_agent = agent or get_agent()
    runtime_manager = get_runtime_manager(runtime_agent)
    global _global_web_heartbeat
    if _global_web_heartbeat is None:
        _global_web_heartbeat = build_web_session_heartbeat(runtime_agent, runtime_manager)
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
    if describe_web_runtime_services(agent).get('ready'):
        return

    async with _get_runtime_services_lock():
        runtime_agent = agent or get_agent()
        if describe_web_runtime_services(runtime_agent).get('ready'):
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
        )
        if heartbeat is not None:
            _global_web_heartbeat = heartbeat
        await _ensure_china_bridge_services(runtime_agent)


async def shutdown_web_runtime() -> None:
    global _global_agent, _global_bus, _global_runtime_manager, _global_web_heartbeat
    global _global_china_transport, _global_china_supervisor, _global_china_outbound_task, _global_china_start_task

    agent = _global_agent
    runtime_manager = _global_runtime_manager
    heartbeat = _global_web_heartbeat
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


def run_web_shell(*, host: str, port: int, reload: bool, debug: bool, set_debug_mode) -> None:
    from g3ku.web.main import run_server

    set_debug_mode(debug)
    run_server(
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
    'get_runtime_manager',
    'get_web_heartbeat_service',
    'refresh_web_agent_runtime',
    'run_web_shell',
    'shutdown_web_runtime',
]
