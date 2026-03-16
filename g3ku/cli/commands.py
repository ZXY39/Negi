"""CLI commands for g3ku."""

import asyncio
import json
import os
import select
import signal
import shutil
import sys
import warnings
from pathlib import Path

import typer
from prompt_toolkit import PromptSession
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.history import FileHistory
from prompt_toolkit.patch_stdout import patch_stdout
from rich.console import Console
from rich.markdown import Markdown
from rich.table import Table
from rich.text import Text

from g3ku import __logo__, __version__
from g3ku.config.schema import Config
from g3ku.resources.tool_settings import MemoryRuntimeSettings, load_tool_settings_from_manifest
from g3ku.utils.helpers import resolve_path_in_workspace, sync_workspace_templates

app = typer.Typer(
    name="g3ku",
    help="g3ku - Personal AI Assistant",
    no_args_is_help=True,
)

console = Console()
EXIT_COMMANDS = {"exit", "quit", "/exit", "/quit", ":q"}

# ---------------------------------------------------------------------------
# CLI input: prompt_toolkit for editing, paste, history, and display
# ---------------------------------------------------------------------------

_PROMPT_SESSION: PromptSession | None = None
_SAVED_TERM_ATTRS = None  # original termios settings, restored on exit


def _safe_text_for_console(text: str) -> str:
    """Best-effort conversion for legacy Windows consoles (e.g., GBK)."""
    encoding = getattr(getattr(sys, "stdout", None), "encoding", None) or "utf-8"
    try:
        text.encode(encoding)
        return text
    except Exception:
        return text.encode(encoding, errors="replace").decode(encoding, errors="replace")


def _logo() -> str:
    return _safe_text_for_console(__logo__)


def _suppress_noisy_dependency_warnings() -> None:
    """Suppress known third-party dependency noise in CLI output."""
    warnings.filterwarnings(
        "ignore",
        message=r".*doesn't match a supported version.*",
    )
    try:
        from requests import RequestsDependencyWarning
    except Exception:
        return
    warnings.filterwarnings("ignore", category=RequestsDependencyWarning)


_suppress_noisy_dependency_warnings()


def _set_debug_mode(enabled: bool) -> None:
    """Enable runtime debug trace toggles for the current process."""
    if not enabled:
        return
    from loguru import logger

    os.environ["G3KU_DEBUG_TRACE"] = "1"
    logger.enable("g3ku")
    logger.info("Debug mode enabled (G3KU_DEBUG_TRACE=1)")


def _flush_pending_tty_input() -> None:
    """Drop unread keypresses typed while the model was generating output."""
    try:
        fd = sys.stdin.fileno()
        if not os.isatty(fd):
            return
    except Exception:
        return

    try:
        import termios
        termios.tcflush(fd, termios.TCIFLUSH)
        return
    except Exception:
        pass

    try:
        while True:
            ready, _, _ = select.select([fd], [], [], 0)
            if not ready:
                break
            if not os.read(fd, 4096):
                break
    except Exception:
        return


def _restore_terminal() -> None:
    """Restore terminal to its original state (echo, line buffering, etc.)."""
    if _SAVED_TERM_ATTRS is None:
        return
    try:
        import termios
        termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, _SAVED_TERM_ATTRS)
    except Exception:
        pass


def _init_prompt_session(workspace: Path | None = None) -> None:
    """Create the prompt_toolkit session with persistent file history."""
    global _PROMPT_SESSION, _SAVED_TERM_ATTRS

    # Save terminal state so we can restore it on exit
    try:
        import termios
        _SAVED_TERM_ATTRS = termios.tcgetattr(sys.stdin.fileno())
    except Exception:
        pass

    history_root = workspace if workspace is not None else Path.cwd()
    history_file = history_root / ".g3ku" / "history" / "cli_history"
    history_file.parent.mkdir(parents=True, exist_ok=True)

    _PROMPT_SESSION = PromptSession(
        history=FileHistory(str(history_file)),
        enable_open_in_editor=False,
        multiline=False,   # Enter submits (single line mode)
    )


def _print_agent_response(response: str, render_markdown: bool) -> None:
    """Render assistant response with consistent terminal styling."""
    content = response or ""
    body = Markdown(content) if render_markdown else Text(content)
    console.print()
    console.print(f"[cyan]{_logo()} g3ku[/cyan]")
    console.print(body)
    console.print()


def _is_exit_command(command: str) -> bool:
    """Return True when input should end interactive chat."""
    return command.lower() in EXIT_COMMANDS


async def _read_interactive_input_async() -> str:
    """Read user input using prompt_toolkit (handles paste, history, display).

    prompt_toolkit natively handles:
    - Multiline paste (bracketed paste mode)
    - History navigation (up/down arrows)
    - Clean display (no ghost characters or artifacts)
    """
    if _PROMPT_SESSION is None:
        raise RuntimeError("Call _init_prompt_session() first")
    try:
        with patch_stdout():
            return await _PROMPT_SESSION.prompt_async(
                HTML("<b fg='ansiblue'>You:</b> "),
            )
    except EOFError as exc:
        raise KeyboardInterrupt from exc



def version_callback(value: bool):
    if value:
        console.print(f"{_logo()} g3ku v{__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: bool = typer.Option(
        None, "--version", "-v", callback=version_callback, is_eager=True
    ),
):
    """g3ku - Personal AI Assistant."""
    _suppress_noisy_dependency_warnings()


# ============================================================================
# Onboard / Setup
# ============================================================================


@app.command()
def onboard(
    project: bool = typer.Option(
        False,
        "--project",
        help="Use project-local ./.g3ku/config.json and workspace at current directory.",
    ),
):
    """Initialize g3ku configuration and workspace."""
    from g3ku.config.loader import build_project_config_from_example, get_config_path, load_config, save_config
    from g3ku.utils.helpers import get_workspace_path

    config_path = (Path.cwd() / ".g3ku" / "config.json") if project else get_config_path()

    if config_path.exists():
        console.print(f"[yellow]Config already exists at {config_path}[/yellow]")
        console.print("  [bold]y[/bold] = overwrite from project example config")
        console.print("  [bold]N[/bold] = re-save current config in compact format")
        if typer.confirm("Overwrite?"):
            config = build_project_config_from_example()
            if project:
                save_config(config, config_path)
            else:
                save_config(config)
            console.print(f"[green]OK[/green] Config rebuilt from example at {config_path}")
        else:
            config = load_config(config_path if project else None)
            if project:
                save_config(config, config_path)
            else:
                save_config(config)
            console.print(f"[green]OK[/green] Config refreshed at {config_path} (existing values preserved)")
    else:
        config = build_project_config_from_example()
        if project:
            save_config(config, config_path)
        else:
            save_config(config)
        console.print(f"[green]OK[/green] Created config from example at {config_path}")

    # Create workspace
    workspace = get_workspace_path(config.agents.defaults.workspace if "config" in locals() else None)

    if not workspace.exists():
        workspace.mkdir(parents=True, exist_ok=True)
        console.print(f"[green]OK[/green] Created workspace at {workspace}")

    sync_workspace_templates(workspace)

    console.print(f"\n{_logo()} g3ku is ready!")
    console.print("\nNext steps:")
    console.print(f"  1. Add your API key to [cyan]{config_path}[/cyan]")
    console.print("     Get one at: https://openrouter.ai/keys")
    console.print("  2. Chat: [cyan]g3ku agent -m \"Hello!\"[/cyan]")
    if project:
        console.print("  3. Commit [cyan].g3ku/config.json[/cyan] and [cyan]memory/[/cyan] for cross-machine sync")
    console.print("\n[dim]Need QQ / 钉钉 / 企微 / 飞书接入？使用 `g3ku china-bridge doctor` 检查子系统状态。[/dim]")

def _make_provider(config: Config, *, scope: str = "ceo"):
    """Create the configured BaseChatModel for a runtime scope."""
    from g3ku.providers.chatmodels import build_chat_model

    try:
        return build_chat_model(config, role=scope)
    except (ValueError, RuntimeError) as exc:
        console.print("[red]Configuration error:[/red]")
        console.print(str(exc))
        raise typer.Exit(1) from exc


def _load_memory_runtime_settings(config: Config) -> MemoryRuntimeSettings | None:
    try:
        return load_tool_settings_from_manifest(config.workspace_path, "memory_runtime", MemoryRuntimeSettings)
    except Exception:
        return None


def _memory_startup_self_check(config: Config) -> None:
    """Print startup diagnostics for memory mode and embedding credentials."""
    mem_cfg = _load_memory_runtime_settings(config)
    if mem_cfg is None:
        console.print("[yellow]Memory self-check:[/yellow] tools/memory_runtime/resource.yaml is missing or invalid.")
        return
    if not mem_cfg.enabled:
        console.print("[yellow]Memory self-check:[/yellow] tools/memory_runtime settings.enabled=false (disabled).")
        return

    mode = str(mem_cfg.mode or "legacy").lower()
    if mode != "rag":
        console.print(
            f"[red]Memory self-check alert:[/red] tools/memory_runtime mode='{mode}', expected 'rag'."
        )
    else:
        console.print("[green]Memory self-check:[/green] mode=rag")

    def _provider_from_model(value: str) -> str | None:
        model = str(value or "").strip()
        if not model:
            return None
        if ":" in model:
            return model.split(":", 1)[0].strip().lower().replace("-", "_")
        if model in {"qwen3-vl-embedding", "qwen3-vl-rerank"}:
            return "dashscope"
        return None

    def _provider_from_model_key(model_key: str | None) -> str | None:
        key = str(model_key or "").strip()
        if not key:
            return None
        try:
            from g3ku.llm_config.facade import LLMConfigFacade

            binding = LLMConfigFacade(config.workspace_path).get_binding(config, key)
            return _provider_from_model(str(binding.get("provider_model") or ""))
        except Exception:
            return None

    def _provider_has_key(provider_id: str | None) -> bool:
        if not provider_id:
            return True
        provider_cfg = getattr(config.providers, provider_id, None)
        has_cfg_key = bool(getattr(provider_cfg, "api_key", "") if provider_cfg else "")
        has_env_key = any(os.environ.get(name, "").strip() for name in env_map.get(provider_id, []))
        return has_cfg_key or has_env_key

    embed_model = str(mem_cfg.embedding.provider_model or "").strip()
    provider_id = _provider_from_model_key(getattr(mem_cfg.embedding, "model_key", None)) or _provider_from_model(embed_model)
    if not provider_id:
        return

    env_map = {
        "openai": ["OPENAI_API_KEY"],
        "anthropic": ["ANTHROPIC_API_KEY"],
        "gemini": ["GEMINI_API_KEY", "GOOGLE_API_KEY"],
        "openrouter": ["OPENROUTER_API_KEY"],
        "dashscope": ["DASHSCOPE_API_KEY"],
        "zhipu": ["ZHIPU_API_KEY"],
    }
    if mode == "rag" and not _provider_has_key(provider_id):
        console.print(
            "[yellow]Memory self-check warning:[/yellow] embedding provider "
            f"'{provider_id}' has no API key configured. Dense retrieval may fallback to sparse-only."
        )
    rerank_model = str(getattr(mem_cfg.retrieval, "rerank_provider_model", "") or "").strip()
    rerank_provider = _provider_from_model_key(getattr(mem_cfg.retrieval, "rerank_model_key", None)) or _provider_from_model(rerank_model)
    if mode == "rag" and rerank_provider and not _provider_has_key(rerank_provider):
        console.print(
            "[yellow]Memory self-check warning:[/yellow] rerank provider "
            f"'{rerank_provider}' has no API key configured. Rerank stage will be skipped."
        )


def _make_agent_loop(
    config: Config,
    bus,
    provider,
    *,
    debug_mode: bool = False,
    cron_service=None,
    session_manager=None,
):
    """Create the configured agent runtime (LangGraph-only)."""
    runtime = (config.agents.defaults.runtime or "langgraph").lower()
    if runtime != "langgraph":
        console.print("[red]Configuration error:[/red]")
        console.print(
            "Original field: agents.defaults.runtime\n"
            f"Current value: {runtime!r}\n"
            "New supported value: 'langgraph' only."
        )
        raise typer.Exit(1)

    from g3ku.agent.loop import AgentLoop

    try:
        from g3ku.agent.middleware import build_middlewares
    except ModuleNotFoundError:
        if config.agents.defaults.middlewares:
            console.print("[red]Configuration error:[/red]")
            console.print(
                "Runtime middleware requires optional langchain dependency. "
                "Install project extras before enabling middlewares."
            )
            raise typer.Exit(1)
        middlewares = []
    else:
        try:
            middlewares = build_middlewares(config.agents.defaults.middlewares)
        except ValueError as exc:
            console.print("[red]Configuration error:[/red]")
            console.print(str(exc))
            raise typer.Exit(1) from exc

    _memory_startup_self_check(config)

    provider_name, model_id = config.get_scope_model_target("ceo")

    return AgentLoop(
        bus=bus,
        provider=provider,
        workspace=config.workspace_path,
        model=model_id,
        provider_name=provider_name,
        temperature=config.agents.defaults.temperature,
        max_tokens=config.agents.defaults.max_tokens,
        max_iterations=config.agents.defaults.max_tool_iterations,
        memory_window=config.agents.defaults.memory_window,
        reasoning_effort=config.agents.defaults.reasoning_effort,
        multi_agent_config=config.agents.multi_agent,
        app_config=config,
        resource_config=config.resources,
        cron_service=cron_service,
        session_manager=session_manager,
        channels_config=config.china_bridge,
        debug_mode=debug_mode,
        middlewares=middlewares,
    )


# ============================================================================
# Gateway / Server
# ============================================================================


@app.command()
def web(
    host: str = typer.Option("127.0.0.1", "--host", help="Web UI host"),
    port: int = typer.Option(3000, "--port", "-p", help="Web UI port"),
    reload: bool = typer.Option(False, "--reload/--no-reload", help="Enable auto-reload"),
    debug: bool = typer.Option(False, "--debug/--no-debug", help="Enable full backend debug trace logs."),
):
    """Start g3ku Web UI (compatible alias of `g3ku-web`)."""
    from g3ku.shells.web import run_web_shell

    run_web_shell(
        host=host,
        port=port,
        reload=reload,
        debug=debug,
        set_debug_mode=_set_debug_mode,
    )


@app.command()
def gateway(
    port: int = typer.Option(18790, "--port", "-p", help="Gateway port"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Verbose output"),
    debug: bool = typer.Option(False, "--debug/--no-debug", help="Enable full backend debug trace logs."),
):
    """Start the g3ku gateway."""
    from g3ku.shells.gateway import run_gateway_shell

    run_gateway_shell(
        port=port,
        verbose=verbose,
        debug=debug,
        console=console,
        logo_text=_logo(),
        make_provider=_make_provider,
        make_agent_loop=_make_agent_loop,
        set_debug_mode=_set_debug_mode,
        sync_workspace_templates=sync_workspace_templates,
    )




# ============================================================================
# Agent Commands
# ============================================================================


@app.command()
def agent(
    message: str = typer.Option(None, "--message", "-m", help="Message to send to the agent"),
    session_id: str = typer.Option("cli:direct", "--session", "-s", help="Session ID"),
    markdown: bool = typer.Option(True, "--markdown/--no-markdown", help="Render assistant output as Markdown"),
    logs: bool = typer.Option(False, "--logs/--no-logs", help="Show g3ku runtime logs during chat"),
    debug: bool = typer.Option(False, "--debug/--no-debug", help="Enable full backend debug trace logs."),
):
    """Interact with the agent directly."""
    from g3ku.config.loader import get_data_dir, load_config
    from g3ku.shells.cli import run_agent_shell

    run_agent_shell(
        message=message,
        session_id=session_id,
        markdown=markdown,
        logs=logs,
        debug=debug,
        console=console,
        logo_text=_logo(),
        load_config=load_config,
        get_data_dir=get_data_dir,
        make_provider=_make_provider,
        make_agent_loop=_make_agent_loop,
        set_debug_mode=_set_debug_mode,
        sync_workspace_templates=sync_workspace_templates,
        init_prompt_session=_init_prompt_session,
        flush_pending_tty_input=_flush_pending_tty_input,
        restore_terminal=_restore_terminal,
        read_interactive_input_async=_read_interactive_input_async,
        is_exit_command=_is_exit_command,
        print_agent_response=_print_agent_response,
    )


china_bridge_app = typer.Typer(help="Manage the China communication subsystem")
app.add_typer(china_bridge_app, name="china-bridge")


def _china_bridge_status_path(config: Config) -> Path:
    return config.workspace_path / str(config.china_bridge.state_dir or ".g3ku/china-bridge") / "status.json"


@china_bridge_app.command("status")
def china_bridge_status():
    """Show china bridge status from the runtime status file."""
    from g3ku.config.loader import load_config

    config = load_config()
    path = _china_bridge_status_path(config)
    if not path.exists():
        console.print(f"[yellow]No china bridge status file at {path}[/yellow]")
        raise typer.Exit(1)
    payload = json.loads(path.read_text(encoding="utf-8"))
    console.print_json(json.dumps(payload, ensure_ascii=False, indent=2))


@china_bridge_app.command("doctor")
def china_bridge_doctor():
    """Run basic local diagnostics for the china bridge subsystem."""
    from g3ku.config.loader import load_config

    config = load_config()
    dist_entry = config.workspace_path / "subsystems" / "china_channels_host" / "dist" / "index.js"
    status_path = _china_bridge_status_path(config)
    table = Table(title="China Bridge Doctor")
    table.add_column("Check")
    table.add_column("Status")
    table.add_column("Value")
    node_path = shutil.which(config.china_bridge.node_bin)
    table.add_row("enabled", "ok" if config.china_bridge.enabled else "warn", str(config.china_bridge.enabled))
    table.add_row("node", "ok" if node_path else "error", node_path or "not found")
    table.add_row("dist", "ok" if dist_entry.exists() else "error", str(dist_entry))
    table.add_row("status", "ok" if status_path.exists() else "warn", str(status_path))
    table.add_row("public_port", "ok", str(config.china_bridge.public_port))
    table.add_row("control_port", "ok", str(config.china_bridge.control_port))
    for channel_name in ("qqbot", "dingtalk", "wecom", "wecom_app", "feishu_china"):
        payload = getattr(config.china_bridge.channels, channel_name)
        table.add_row(f"channel:{channel_name}", "ok" if payload.enabled else "warn", str(payload.enabled))
    console.print(table)


@china_bridge_app.command("restart")
def china_bridge_restart():
    """Terminate the running node host by PID so the gateway supervisor can restart it."""
    from g3ku.config.loader import load_config

    config = load_config()
    path = _china_bridge_status_path(config)
    if not path.exists():
        console.print(f"[red]No china bridge status file at {path}[/red]")
        raise typer.Exit(1)
    payload = json.loads(path.read_text(encoding="utf-8"))
    pid = int(payload.get("pid") or 0)
    if pid <= 0:
        console.print("[red]china bridge host PID not available[/red]")
        raise typer.Exit(1)
    try:
        os.kill(pid, signal.SIGTERM)
    except Exception as exc:
        console.print(f"[red]failed to signal china bridge pid {pid}: {exc}[/red]")
        raise typer.Exit(1) from exc
    console.print(f"[green]OK[/green] sent SIGTERM to china bridge pid {pid}")

def _middleware_ref(cfg, idx: int) -> str:
    """Human-readable middleware reference for CLI output."""
    if cfg.name and cfg.class_path:
        return f"[{idx}] {cfg.name} ({cfg.class_path})"
    if cfg.name:
        return f"[{idx}] {cfg.name}"
    if cfg.class_path:
        return f"[{idx}] {cfg.class_path}"
    return f"[{idx}] <unnamed>"


def _resolve_middleware_matches(entries, key: str) -> list[int]:
    """Resolve middleware key to candidate indices."""
    target = key.strip()
    if not target:
        return []

    idx_text = target[1:] if target.startswith("#") else target
    if idx_text.isdigit():
        idx = int(idx_text)
        return [idx] if 0 <= idx < len(entries) else []

    lowered = target.lower()
    matches: list[int] = []
    for i, cfg in enumerate(entries):
        name = (cfg.name or "").strip()
        class_path = (cfg.class_path or "").strip()
        if name and name.lower() == lowered:
            matches.append(i)
            continue
        if class_path and class_path == target:
            matches.append(i)
            continue
    return matches


def _set_middleware_enabled(key: str, enabled: bool) -> None:
    """Enable/disable a configured middleware by key."""
    from g3ku.config.loader import load_config, save_config

    config = load_config()
    entries = config.agents.defaults.middlewares
    if not entries:
        console.print("[yellow]No runtime middlewares configured.[/yellow]")
        raise typer.Exit(1)

    matches = _resolve_middleware_matches(entries, key)
    if not matches:
        console.print(f"[red]Middleware '{key}' not found.[/red]")
        console.print("Use `g3ku middleware list` to inspect available entries.")
        raise typer.Exit(1)

    if len(matches) > 1:
        console.print(f"[red]Middleware key '{key}' is ambiguous.[/red]")
        for idx in matches:
            console.print(f"  - {_middleware_ref(entries[idx], idx)}")
        console.print("Use index (for example: `g3ku middleware enable 0`).")
        raise typer.Exit(1)

    idx = matches[0]
    entry = entries[idx]
    if entry.enabled == enabled:
        status = "enabled" if enabled else "disabled"
        console.print(f"[yellow]{_middleware_ref(entry, idx)} already {status}.[/yellow]")
        return

    entry.enabled = enabled
    save_config(config)

    status = "enabled" if enabled else "disabled"
    console.print(f"[green]OK[/green] {_middleware_ref(entry, idx)} {status}.")


middleware_app = typer.Typer(help="Manage runtime middlewares")
app.add_typer(middleware_app, name="middleware")


@middleware_app.command("list")
def middleware_list(
    all: bool = typer.Option(True, "--all/--enabled-only", help="Show all or only enabled middlewares"),
):
    """List configured runtime middlewares."""
    from g3ku.config.loader import load_config

    config = load_config()
    entries = config.agents.defaults.middlewares

    if not entries:
        console.print("No runtime middlewares configured.")
        return

    table = Table(title="Runtime Middlewares")
    table.add_column("Index", style="cyan", justify="right")
    table.add_column("Enabled", style="green")
    table.add_column("Name")
    table.add_column("Class Path")
    table.add_column("Options")

    shown_entries = []
    for idx, cfg in enumerate(entries):
        if not all and not cfg.enabled:
            continue
        table.add_row(
            str(idx),
            "yes" if cfg.enabled else "no",
            cfg.name or "-",
            cfg.class_path or "-",
            str(cfg.options or {}),
        )
        shown_entries.append((idx, cfg))

    if not shown_entries:
        console.print("No enabled middlewares configured.")
        return

    console.print(table)
    for idx, cfg in shown_entries:
        console.print(
            f"[dim]{idx}: enabled={str(cfg.enabled).lower()} name={cfg.name or '-'} classPath={cfg.class_path or '-'}[/dim]"
        )
    console.print("[dim]Use index/name/classPath with `middleware enable|disable`.[/dim]")


@middleware_app.command("enable")
def middleware_enable(
    key: str = typer.Argument(..., help="Middleware index, name, or classPath"),
):
    """Enable a runtime middleware entry."""
    _set_middleware_enabled(key, True)


@middleware_app.command("disable")
def middleware_disable(
    key: str = typer.Argument(..., help="Middleware index, name, or classPath"),
):
    """Disable a runtime middleware entry."""
    _set_middleware_enabled(key, False)



# ============================================================================
# Cron Commands
# ============================================================================

cron_app = typer.Typer(help="Manage scheduled tasks")
app.add_typer(cron_app, name="cron")


@cron_app.command("list")
def cron_list(
    all: bool = typer.Option(False, "--all", "-a", help="Include disabled jobs"),
):
    """List scheduled jobs."""
    from g3ku.config.loader import get_data_dir
    from g3ku.cron.service import CronService

    store_path = get_data_dir() / "cron" / "jobs.json"
    service = CronService(store_path)

    jobs = service.list_jobs(include_disabled=all)

    if not jobs:
        console.print("No scheduled jobs.")
        return

    table = Table(title="Scheduled Jobs")
    table.add_column("ID", style="cyan")
    table.add_column("Name")
    table.add_column("Schedule")
    table.add_column("Status")
    table.add_column("Next Run")

    import time
    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo
    for job in jobs:
        # Format schedule
        if job.schedule.kind == "every":
            sched = f"every {(job.schedule.every_ms or 0) // 1000}s"
        elif job.schedule.kind == "cron":
            sched = f"{job.schedule.expr or ''} ({job.schedule.tz})" if job.schedule.tz else (job.schedule.expr or "")
        else:
            sched = "one-time"

        # Format next run
        next_run = ""
        if job.state.next_run_at_ms:
            ts = job.state.next_run_at_ms / 1000
            try:
                tz = ZoneInfo(job.schedule.tz) if job.schedule.tz else None
                next_run = _dt.fromtimestamp(ts, tz).strftime("%Y-%m-%d %H:%M")
            except Exception:
                next_run = time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))

        status = "[green]enabled[/green]" if job.enabled else "[dim]disabled[/dim]"

        table.add_row(job.id, job.name, sched, status, next_run)

    console.print(table)


@cron_app.command("add")
def cron_add(
    name: str = typer.Option(..., "--name", "-n", help="Job name"),
    message: str = typer.Option(..., "--message", "-m", help="Message for agent"),
    every: int = typer.Option(None, "--every", "-e", help="Run every N seconds"),
    cron_expr: str = typer.Option(None, "--cron", "-c", help="Cron expression (e.g. '0 9 * * *')"),
    tz: str | None = typer.Option(None, "--tz", help="IANA timezone for cron (e.g. 'America/Vancouver')"),
    at: str = typer.Option(None, "--at", help="Run once at time (ISO format)"),
    deliver: bool = typer.Option(False, "--deliver", "-d", help="Deliver response to channel"),
    to: str = typer.Option(None, "--to", help="Recipient for delivery"),
    channel: str = typer.Option(None, "--channel", help="Channel for delivery (e.g. 'telegram', 'whatsapp')"),
):
    """Add a scheduled job."""
    from g3ku.config.loader import get_data_dir
    from g3ku.cron.service import CronService
    from g3ku.cron.types import CronSchedule

    if tz and not cron_expr:
        console.print("[red]Error: --tz can only be used with --cron[/red]")
        raise typer.Exit(1)

    # Determine schedule type
    if every:
        schedule = CronSchedule(kind="every", every_ms=every * 1000)
    elif cron_expr:
        schedule = CronSchedule(kind="cron", expr=cron_expr, tz=tz)
    elif at:
        import datetime
        dt = datetime.datetime.fromisoformat(at)
        schedule = CronSchedule(kind="at", at_ms=int(dt.timestamp() * 1000))
    else:
        console.print("[red]Error: Must specify --every, --cron, or --at[/red]")
        raise typer.Exit(1)

    store_path = get_data_dir() / "cron" / "jobs.json"
    service = CronService(store_path)

    try:
        job = service.add_job(
            name=name,
            schedule=schedule,
            message=message,
            deliver=deliver,
            to=to,
            channel=channel,
        )
    except ValueError as e:
        console.print(f"[red]Error: {e}[/red]")
        raise typer.Exit(1) from e

    console.print(f"[green]闁翠焦褰?green] Added job '{job.name}' ({job.id})")


@cron_app.command("remove")
def cron_remove(
    job_id: str = typer.Argument(..., help="Job ID to remove"),
):
    """Remove a scheduled job."""
    from g3ku.config.loader import get_data_dir
    from g3ku.cron.service import CronService

    store_path = get_data_dir() / "cron" / "jobs.json"
    service = CronService(store_path)

    if service.remove_job(job_id):
        console.print(f"[green]闁翠焦褰?green] Removed job {job_id}")
    else:
        console.print(f"[red]Job {job_id} not found[/red]")


@cron_app.command("enable")
def cron_enable(
    job_id: str = typer.Argument(..., help="Job ID"),
    disable: bool = typer.Option(False, "--disable", help="Disable instead of enable"),
):
    """Enable or disable a job."""
    from g3ku.config.loader import get_data_dir
    from g3ku.cron.service import CronService

    store_path = get_data_dir() / "cron" / "jobs.json"
    service = CronService(store_path)

    job = service.enable_job(job_id, enabled=not disable)
    if job:
        status = "disabled" if disable else "enabled"
        console.print(f"[green]闁翠焦褰?green] Job '{job.name}' {status}")
    else:
        console.print(f"[red]Job {job_id} not found[/red]")


@cron_app.command("run")
def cron_run(
    job_id: str = typer.Argument(..., help="Job ID to run"),
    force: bool = typer.Option(False, "--force", "-f", help="Run even if disabled"),
):
    """Manually run a job."""
    from loguru import logger

    from g3ku.bus.queue import MessageBus
    from g3ku.config.loader import get_data_dir, load_config
    from g3ku.cron.service import CronService
    from g3ku.cron.types import CronJob
    logger.disable("g3ku")

    config = load_config()
    provider = _make_provider(config)
    bus = MessageBus()
    agent_loop = _make_agent_loop(config, bus, provider)

    store_path = get_data_dir() / "cron" / "jobs.json"
    service = CronService(store_path)

    result_holder = []
    runtime_manager = SessionRuntimeManager(agent_loop)
    runtime_bridge = SessionRuntimeBridge(runtime_manager)

    async def on_job(job: CronJob) -> str | None:
        result = await runtime_bridge.prompt(
            job.payload.message,
            session_key=f"cron:{job.id}",
            channel=job.payload.channel or "cli",
            chat_id=job.payload.to or "direct",
        )
        response = str(result.output or "")
        result_holder.append(response)
        return response

    service.on_job = on_job

    async def run():
        return await service.run_job(job_id, force=force)

    if asyncio.run(run()):
        console.print("[green]闁翠焦褰?green] Job executed")
        if result_holder:
            _print_agent_response(result_holder[0], render_markdown=True)
    else:
        console.print(f"[red]Failed to run job {job_id}[/red]")


# ============================================================================
# Status Commands
# ============================================================================


@app.command()
def status():
    """Show g3ku status."""
    from g3ku.config.loader import get_config_path, load_config

    config_path = get_config_path()
    config = load_config()
    workspace = config.workspace_path

    def _status_mark(ok: bool) -> str:
        return "[green]OK[/green]" if ok else "[red]X[/red]"

    console.print(f"{_logo()} g3ku Status\n")

    console.print(f"Config: {config_path} {_status_mark(config_path.exists())}")
    console.print(f"Workspace: {workspace} {_status_mark(workspace.exists())}")

    if config_path.exists():
        from g3ku.providers.registry import PROVIDERS

        console.print(f"主Agent Model: {config.resolve_role_model_key('ceo')}")
        console.print(f"Execution Model: {config.resolve_role_model_key('execution')}")
        console.print(f"Inspection Model: {config.resolve_role_model_key('inspection')}")
        console.print(f"Runtime: {config.agents.defaults.runtime}")

        mem_cfg = _load_memory_runtime_settings(config)
        if mem_cfg is None:
            console.print("Memory Runtime: [yellow]missing tools/memory_runtime/resource.yaml[/yellow]")
        else:
            console.print(f"Memory Mode: {mem_cfg.mode} ({'enabled' if mem_cfg.enabled else 'disabled'})")
            cp_path = resolve_path_in_workspace(mem_cfg.checkpointer.path, config.workspace_path)
            store_db = resolve_path_in_workspace(mem_cfg.store.sqlite_path, config.workspace_path)
            qdrant_dir = resolve_path_in_workspace(mem_cfg.store.qdrant_path, config.workspace_path)
            console.print(f"Memory Checkpointer: {mem_cfg.checkpointer.backend} {cp_path}")
            console.print(f"Memory Store(SQLite): {store_db} {_status_mark(store_db.exists())}")
            console.print(f"Memory Store(Qdrant): {qdrant_dir} {_status_mark(qdrant_dir.exists())}")
            pending_file = config.workspace_path / "memory" / "pending_facts.jsonl"
            audit_file = config.workspace_path / "memory" / "audit.jsonl"
            console.print(f"Memory Pending File: {pending_file} {_status_mark(pending_file.exists())}")
            console.print(f"Memory Audit File: {audit_file} {_status_mark(audit_file.exists())}")

        # Check API keys from registry
        for spec in PROVIDERS:
            p = getattr(config.providers, spec.name, None)
            if p is None:
                continue
            if spec.is_oauth:
                console.print(f"{spec.label}: [green]OK[/green] (OAuth)")
            elif spec.is_local:
                # Local deployments show api_base instead of api_key
                if p.api_base:
                    console.print(f"{spec.label}: [green]OK[/green] {p.api_base}")
                else:
                    console.print(f"{spec.label}: [dim]not set[/dim]")
            else:
                has_key = bool(p.api_key)
                console.print(f"{spec.label}: {'[green]OK[/green]' if has_key else '[dim]not set[/dim]'}")


from g3ku.shells.memory_cli import build_memory_app

memory_app = build_memory_app(console)
app.add_typer(memory_app, name="memory")

# ============================================================================
# OAuth Login
# ============================================================================

from g3ku.shells.provider_cli import build_provider_app

provider_app = build_provider_app(console, _logo())
app.add_typer(provider_app, name="provider")

from g3ku.shells.resource_cli import build_resource_app

resource_app = build_resource_app(console)
app.add_typer(resource_app, name="resource")

if __name__ == "__main__":
    app()











