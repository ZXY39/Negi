"""CLI shell runtime entrypoints for the converged architecture."""

from __future__ import annotations

import asyncio
import os
import signal
from pathlib import Path
from typing import Any, Callable

from g3ku.utils.sdk_logging import configure_openai_sdk_logging


def run_agent_shell(
    *,
    message: str | None,
    session_id: str,
    markdown: bool,
    logs: bool,
    debug: bool,
    console: Any,
    logo_text: str,
    load_config: Callable[[], Any],
    get_data_dir: Callable[[], Path],
    make_provider: Callable[[Any], Any],
    make_agent_loop: Callable[..., Any],
    set_debug_mode: Callable[[bool], None],
    sync_workspace_templates: Callable[[str], None],
    init_prompt_session: Callable[[Path | None], None],
    flush_pending_tty_input: Callable[[], None],
    restore_terminal: Callable[[], None],
    read_interactive_input_async: Callable[[], Any],
    is_exit_command: Callable[[str], bool],
    print_agent_response: Callable[[str, bool], None],
) -> None:
    """Run the interactive or one-shot CLI agent shell."""
    from loguru import logger

    from g3ku.bus.queue import MessageBus
    from g3ku.cron.service import CronService
    from g3ku.runtime import SessionRuntimeBridge, SessionRuntimeManager, cli_event_text

    config = load_config()
    sync_workspace_templates(config.workspace_path)

    bus = MessageBus()
    provider = make_provider(config)

    cron_store_path = get_data_dir() / "cron" / "jobs.json"
    cron = CronService(cron_store_path)

    set_debug_mode(debug)
    configure_openai_sdk_logging()
    if logs or debug:
        logger.enable("g3ku")
    else:
        logger.disable("g3ku")

    agent_loop = make_agent_loop(config, bus, provider, debug_mode=debug, cron_service=cron)
    runtime_manager = SessionRuntimeManager(agent_loop)
    runtime_bridge = SessionRuntimeBridge(runtime_manager)
    task_registrar = getattr(agent_loop, "_register_active_task", None)

    def _thinking_ctx():
        if logs:
            from contextlib import nullcontext

            return nullcontext()
        return console.status("[dim]g3ku is thinking...[/dim]", spinner="dots")

    async def _cli_session_event(event) -> None:
        kind, text = cli_event_text(event)
        if not text:
            return
        ch = agent_loop.channels_config
        is_tool_hint = kind == "tool_plan"
        if ch and is_tool_hint and not ch.send_tool_hints:
            return
        if ch and not is_tool_hint and kind not in {"control", "tool_error"} and not ch.send_progress:
            return
        prefix = (
            "deep"
            if kind == "deep_progress"
            else "tool"
            if kind in {"tool", "tool_plan", "tool_result"}
            else "error"
            if kind == "tool_error"
            else kind or "progress"
        )
        style = "red" if kind == "tool_error" else "dim"
        console.print(f"  [{style}]-> [{prefix}] {text}[/{style}]")

    if ":" in session_id:
        cli_channel, cli_chat_id = session_id.split(":", 1)
    else:
        cli_channel, cli_chat_id = "cli", session_id

    async def _consume_outbound() -> None:
        while True:
            try:
                msg = await asyncio.wait_for(bus.consume_outbound(), timeout=1.0)
                if msg.content:
                    console.print()
                    print_agent_response(msg.content, markdown)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break

    if message:
        async def run_once() -> None:
            outbound_task = asyncio.create_task(_consume_outbound())
            try:
                with _thinking_ctx():
                    result = await runtime_bridge.prompt(
                        message,
                        session_key=session_id,
                        channel=cli_channel,
                        chat_id=cli_chat_id,
                        listeners=[_cli_session_event],
                        register_task=task_registrar if callable(task_registrar) else None,
                    )
                print_agent_response(str(result.output or ""), markdown)
            finally:
                outbound_task.cancel()
                await asyncio.gather(outbound_task, return_exceptions=True)
                await agent_loop.close_mcp()

        asyncio.run(run_once())
        return

    init_prompt_session(Path(config.workspace_path))
    console.print(f"{logo_text} Interactive mode (type [bold]exit[/bold] or [bold]Ctrl+C[/bold] to quit)\n")

    def _exit_on_sigint(signum, frame):
        _ = signum, frame
        restore_terminal()
        console.print("\nGoodbye!")
        os._exit(0)

    signal.signal(signal.SIGINT, _exit_on_sigint)

    async def run_interactive() -> None:
        outbound_task = asyncio.create_task(_consume_outbound())
        try:
            while True:
                try:
                    flush_pending_tty_input()
                    user_input = await read_interactive_input_async()
                    command = user_input.strip()
                    if not command:
                        continue

                    if is_exit_command(command):
                        restore_terminal()
                        console.print("\nGoodbye!")
                        break

                    with _thinking_ctx():
                        result = await runtime_bridge.prompt(
                            user_input,
                            session_key=session_id,
                            channel=cli_channel,
                            chat_id=cli_chat_id,
                            listeners=[_cli_session_event],
                            register_task=task_registrar if callable(task_registrar) else None,
                        )
                    if result.output:
                        print_agent_response(str(result.output), markdown)
                except KeyboardInterrupt:
                    restore_terminal()
                    console.print("\nGoodbye!")
                    break
                except EOFError:
                    restore_terminal()
                    console.print("\nGoodbye!")
                    break
        finally:
            outbound_task.cancel()
            await asyncio.gather(outbound_task, return_exceptions=True)
            await agent_loop.close_mcp()

    asyncio.run(run_interactive())
