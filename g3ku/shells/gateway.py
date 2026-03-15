"""Gateway shell runner for the converged runtime architecture."""

from __future__ import annotations

import asyncio
from typing import Any, Callable

from g3ku.channels.outbound_router import OutboundRouter
from g3ku.china_bridge import ChinaBridgeSupervisor, ChinaBridgeTransport
from g3ku.transports.channel_session import ChannelSessionTransport


def run_gateway_shell(
    *,
    port: int,
    verbose: bool,
    debug: bool,
    console: Any,
    logo_text: str,
    make_provider: Callable[[Any], Any],
    make_agent_loop: Callable[..., Any],
    set_debug_mode: Callable[[bool], None],
    sync_workspace_templates: Callable[[str], None],
) -> None:
    """Start the gateway shell using runtime/session transports."""
    from g3ku.bus.events import OutboundMessage
    from g3ku.bus.queue import MessageBus
    from g3ku.channels.manager import ChannelManager
    from g3ku.config.loader import get_data_dir, load_config
    from g3ku.cron.types import CronJob
    from g3ku.runtime import SessionRuntimeBridge, SessionRuntimeManager
    from g3ku.services import CronService, HeartbeatService
    from g3ku.session.manager import SessionManager

    if verbose:
        import logging

        logging.basicConfig(level=logging.DEBUG)
    set_debug_mode(debug)

    console.print(f"{logo_text} Starting g3ku gateway on port {port}...")

    config = load_config()
    sync_workspace_templates(config.workspace_path)
    bus = MessageBus()
    provider = make_provider(config)
    session_manager = SessionManager(config.workspace_path)

    cron_store_path = get_data_dir() / "cron" / "jobs.json"
    cron = CronService(cron_store_path)

    agent = make_agent_loop(
        config,
        bus,
        provider,
        debug_mode=debug,
        cron_service=cron,
        session_manager=session_manager,
    )
    runtime_manager = SessionRuntimeManager(agent)
    runtime_bridge = SessionRuntimeBridge(runtime_manager)
    task_registrar = getattr(agent, "_register_active_task", None)
    channel_transport = ChannelSessionTransport(
        bus=bus,
        runtime_bridge=runtime_bridge,
        register_task=task_registrar if callable(task_registrar) else None,
    )
    china_transport = ChinaBridgeTransport(
        runtime_bridge=runtime_bridge,
        app_config=config,
        register_task=task_registrar if callable(task_registrar) else None,
    )

    async def on_cron_job(job: CronJob) -> str | None:
        reminder_note = (
            "[Scheduled Task] Timer finished.\n\n"
            f"Task '{job.name}' has been triggered.\n"
            f"Scheduled instruction: {job.payload.message}"
        )
        result = await runtime_bridge.prompt(
            reminder_note,
            session_key=f"cron:{job.id}",
            channel=job.payload.channel or "cli",
            chat_id=job.payload.to or "direct",
        )
        response = str(result.output or "")
        if job.payload.deliver and job.payload.to and response:
            await bus.publish_outbound(
                OutboundMessage(
                    channel=job.payload.channel or "cli",
                    chat_id=job.payload.to,
                    content=response,
                )
            )
        return response

    cron.on_job = on_cron_job
    channels = ChannelManager(config, bus, inbound_handler=channel_transport.handle_inbound)
    outbound_router = OutboundRouter(bus=bus, legacy_manager=channels, china_transport=china_transport)
    china_supervisor = ChinaBridgeSupervisor(
        app_config=config,
        workspace=config.workspace_path,
        transport=china_transport,
    )

    def _pick_heartbeat_target() -> tuple[str, str]:
        enabled = set(channels.enabled_channels)
        for item in session_manager.list_sessions():
            key = item.get("key") or ""
            if ":" not in key:
                continue
            channel, chat_id = key.split(":", 1)
            if channel in {"cli", "system"}:
                continue
            if channel in enabled and chat_id:
                return channel, chat_id
        return "cli", "direct"

    async def on_heartbeat_notify(response: str) -> None:
        channel, chat_id = _pick_heartbeat_target()
        if channel == "cli":
            return
        await bus.publish_outbound(OutboundMessage(channel=channel, chat_id=chat_id, content=response))

    hb_cfg = config.gateway.heartbeat
    heartbeat = HeartbeatService(
        workspace=config.workspace_path,
        provider=provider,
        model=agent.model,
        runtime_bridge=runtime_bridge,
        target_resolver=_pick_heartbeat_target,
        session_key="heartbeat",
        task_registrar=task_registrar,
        on_notify=on_heartbeat_notify,
        interval_s=hb_cfg.interval_s,
        enabled=hb_cfg.enabled,
    )

    if channels.enabled_channels:
        console.print(f"[green]OK[/green] Channels enabled: {', '.join(channels.enabled_channels)}")
    else:
        console.print("[yellow]Warning: No channels enabled[/yellow]")

    cron_status = cron.status()
    if cron_status["jobs"] > 0:
        console.print(f"[green]OK[/green] Cron: {cron_status['jobs']} scheduled jobs")

    console.print(f"[green]OK[/green] Heartbeat: every {hb_cfg.interval_s}s")

    async def run() -> None:
        channels_task: asyncio.Task | None = None
        china_wait_task: asyncio.Task | None = None
        try:
            await cron.start()
            await heartbeat.start()
            await outbound_router.start()
            await china_supervisor.start()
            channels_task = asyncio.create_task(channels.start_all(dispatch_outbound=False))
            waiters = [channels_task]
            if config.china_bridge.enabled and config.china_bridge.auto_start:
                china_wait_task = asyncio.create_task(china_supervisor.wait())
                waiters.append(china_wait_task)
            await asyncio.gather(*waiters)
        except KeyboardInterrupt:
            console.print("\nShutting down...")
        finally:
            if channels_task is not None:
                channels_task.cancel()
                await asyncio.gather(channels_task, return_exceptions=True)
            if china_wait_task is not None:
                china_wait_task.cancel()
                await asyncio.gather(china_wait_task, return_exceptions=True)
            await agent.close_mcp()
            heartbeat.stop()
            cron.stop()
            agent.stop()
            await outbound_router.stop()
            await china_supervisor.stop()
            await channels.stop_all()

    asyncio.run(run())
