"""Gateway shell runner for the converged runtime architecture."""

from __future__ import annotations

import asyncio
from typing import Any, Callable

from g3ku.china_bridge import ChinaBridgeSupervisor, ChinaBridgeTransport


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
    china_supervisor = ChinaBridgeSupervisor(
        app_config=config,
        workspace=config.workspace_path,
        transport=china_transport,
    )

    def _pick_heartbeat_target() -> tuple[str, str, dict[str, str]]:
        for item in session_manager.list_sessions():
            key = item.get("key") or ""
            if not key.startswith("china:"):
                continue
            parts = key.split(":")
            if len(parts) < 5:
                continue
            channel = parts[1]
            account_id = parts[2]
            scope = parts[3]
            peer_id = parts[4]
            peer_kind = "group" if scope == "group" else "user"
            return channel, peer_id, {"_china_account_id": account_id, "_china_peer_kind": peer_kind, "_china_peer_id": peer_id}
        return "cli", "direct", {}

    async def on_heartbeat_notify(response: str) -> None:
        channel, chat_id, metadata = _pick_heartbeat_target()
        if channel == "cli":
            return
        await bus.publish_outbound(OutboundMessage(channel=channel, chat_id=chat_id, content=response, metadata=metadata))

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

    enabled_china_channels = [
        name
        for name in ("qqbot", "dingtalk", "wecom", "wecom_app", "feishu_china")
        if getattr(config.china_bridge.channels, name).enabled
    ]
    if enabled_china_channels:
        console.print(f"[green]OK[/green] China channels enabled: {', '.join(enabled_china_channels)}")
    else:
        console.print("[yellow]Warning: No china channels enabled[/yellow]")

    cron_status = cron.status()
    if cron_status["jobs"] > 0:
        console.print(f"[green]OK[/green] Cron: {cron_status['jobs']} scheduled jobs")

    console.print(f"[green]OK[/green] Heartbeat: every {hb_cfg.interval_s}s")

    async def _drain_outbound() -> None:
        while True:
            try:
                msg = await asyncio.wait_for(bus.consume_outbound(), timeout=1.0)
                if msg.channel in {"qqbot", "dingtalk", "wecom", "wecom-app", "feishu-china"}:
                    await china_transport.send_outbound(msg)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break

    async def run() -> None:
        outbound_task: asyncio.Task | None = None
        china_wait_task: asyncio.Task | None = None
        try:
            await cron.start()
            await heartbeat.start()
            await china_supervisor.start()
            outbound_task = asyncio.create_task(_drain_outbound())
            waiters = [outbound_task]
            if config.china_bridge.enabled and config.china_bridge.auto_start:
                china_wait_task = asyncio.create_task(china_supervisor.wait())
                waiters.append(china_wait_task)
            await asyncio.gather(*waiters)
        except KeyboardInterrupt:
            console.print("\nShutting down...")
        finally:
            if outbound_task is not None:
                outbound_task.cancel()
                await asyncio.gather(outbound_task, return_exceptions=True)
            if china_wait_task is not None:
                china_wait_task.cancel()
                await asyncio.gather(china_wait_task, return_exceptions=True)
            await agent.close_mcp()
            heartbeat.stop()
            cron.stop()
            agent.stop()
            await china_supervisor.stop()

    asyncio.run(run())
