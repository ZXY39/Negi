from __future__ import annotations

import atexit
import asyncio
import json
import os
import secrets
import webbrowser
from pathlib import Path

import typer
from g3ku.config.loader import ensure_startup_config_ready, load_config
from g3ku.security import BOOTSTRAP_MASTER_KEY_ENV, get_bootstrap_security_service
from g3ku.web.worker_control import WEB_AUTO_WORKER_ENV
from main.service.task_terminal_callback import (
    TASK_TERMINAL_CALLBACK_PATH,
    TASK_TERMINAL_CALLBACK_TOKEN_ENV,
    TASK_TERMINAL_CALLBACK_URL_ENV,
    save_task_terminal_callback_config,
)


app = typer.Typer(
    add_completion=False,
    rich_markup_mode=None,
    help="G3ku project launcher.",
    no_args_is_help=True,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FRONTEND_ENTRY = PROJECT_ROOT / "g3ku" / "web" / "frontend" / "org_graph.html"
_START_LOCK_HANDLE = None


def _resolve_project_root() -> Path:
    os.chdir(PROJECT_ROOT)
    return PROJECT_ROOT


def _ensure_frontend_ready() -> str:
    if not FRONTEND_ENTRY.exists():
        raise typer.BadParameter(f"Missing frontend entry file: {FRONTEND_ENTRY}")
    return "static"


def _set_prompt_log_mode(enabled: bool) -> None:
    if not enabled:
        return
    os.environ["G3KU_PROMPT_TRACE"] = "1"


def _resolve_backend_port(port: int | None) -> int:
    if port is not None:
        return port
    try:
        return int(load_config().gateway.port)
    except Exception:
        return 18790


def _start_lock_path(root: Path) -> Path:
    return root / ".g3ku" / "start.lock"


def _read_lock_metadata(handle) -> dict[str, object]:
    try:
        handle.seek(0)
        raw = handle.read().strip()
    except Exception:
        return {}
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _release_start_lock() -> None:
    global _START_LOCK_HANDLE
    handle = _START_LOCK_HANDLE
    _START_LOCK_HANDLE = None
    if handle is None:
        return
    try:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except Exception:
        pass
    try:
        handle.close()
    except Exception:
        pass


def _acquire_start_lock(root: Path, *, port: int) -> None:
    global _START_LOCK_HANDLE
    _release_start_lock()

    lock_path = _start_lock_path(root)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+", encoding="utf-8")
    try:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        metadata = _read_lock_metadata(handle)
        holder = metadata.get("pid", "unknown")
        holder_port = metadata.get("port", "unknown")
        handle.close()
        raise typer.BadParameter(
            "Another `g3ku start` process is already running for this workspace "
            f"(pid={holder}, port={holder_port}, lock={lock_path})."
        )

    metadata = {"pid": os.getpid(), "port": port, "root": str(root)}
    handle.seek(0)
    handle.truncate(0)
    handle.write(json.dumps(metadata, ensure_ascii=False))
    handle.flush()
    try:
        os.fsync(handle.fileno())
    except OSError:
        pass

    _START_LOCK_HANDLE = handle
    atexit.register(_release_start_lock)


async def _run_worker_runtime() -> None:
    from g3ku.bus.queue import MessageBus
    from g3ku.cli.commands import _make_agent_loop, _make_provider, sync_workspace_templates
    from g3ku.config.loader import load_config

    os.environ["G3KU_TASK_RUNTIME_ROLE"] = "worker"
    master_key = str(os.environ.pop(BOOTSTRAP_MASTER_KEY_ENV, "")).strip()
    if master_key:
        get_bootstrap_security_service(Path.cwd()).activate_with_master_key(master_key=master_key)
    ensure_startup_config_ready()
    config = load_config()
    sync_workspace_templates(config.workspace_path)
    bus = MessageBus()
    provider = _make_provider(config)
    agent = _make_agent_loop(config, bus, provider, debug_mode=False)
    service = getattr(agent, "main_task_service", None)
    if service is None:
        raise RuntimeError("main_task_service_unavailable")
    try:
        await service.startup()
        while True:
            await asyncio.sleep(1.0)
    finally:
        await agent.close_mcp()


@app.callback()
def _main() -> None:
    """G3ku command group."""


@app.command()
def start(
    host: str = typer.Option("127.0.0.1", "--host", help="Backend bind host."),
    port: int | None = typer.Option(None, "--port", min=1, max=65535, help="Backend bind port. Defaults to gateway.port from project config."),
    reload: bool = typer.Option(False, "--reload", help="Enable uvicorn reload mode."),
    log_enabled: bool = typer.Option(False, "--log", "-log", help="Render main-agent user/prompt/answer logs."),
    open_browser: bool = typer.Option(False, "--open", help="Open the app URL in the default browser."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print the resolved actions without starting the server."),
    with_worker: bool = typer.Option(True, "--worker/--no-worker", help="Auto-start the task worker after unlock."),
) -> None:
    """Start the unified G3ku web app.

    The frontend is served by the backend from `g3ku/web/frontend/org_graph.html`.
    """

    root = _resolve_project_root()
    ensure_startup_config_ready()
    resolved_port = _resolve_backend_port(port)
    frontend_mode = _ensure_frontend_ready()
    _set_prompt_log_mode(log_enabled)
    url = f"http://{host}:{resolved_port}"

    typer.echo(f"[g3ku] project root: {root}")
    typer.echo(f"[g3ku] frontend mode: {frontend_mode}")
    typer.echo(f"[g3ku] backend url: {url}")
    if log_enabled:
        typer.echo("[g3ku] main-agent prompt logging: enabled")

    if open_browser:
        typer.echo(f"[g3ku] opening browser: {url}")
        if not dry_run:
            webbrowser.open(url)

    if dry_run:
        return

    _acquire_start_lock(root, port=resolved_port)
    callback_token = secrets.token_urlsafe(24)
    callback_url = f"http://127.0.0.1:{resolved_port}{TASK_TERMINAL_CALLBACK_PATH}"
    os.environ[TASK_TERMINAL_CALLBACK_URL_ENV] = callback_url
    os.environ[TASK_TERMINAL_CALLBACK_TOKEN_ENV] = callback_token
    save_task_terminal_callback_config(workspace=root, url=callback_url, token=callback_token)
    if with_worker and not reload:
        os.environ[WEB_AUTO_WORKER_ENV] = "1"
        typer.echo("[g3ku] task worker will auto-start after project unlock")
    else:
        os.environ.pop(WEB_AUTO_WORKER_ENV, None)
    try:
        if with_worker and reload:
            typer.echo("[g3ku] worker auto-start is disabled when --reload is enabled; run `python -m g3ku worker` separately.")
        from g3ku.web.main import run_server

        run_server(
            host=host,
            port=resolved_port,
            reload=reload,
            log_level='info',
        )
    finally:
        _release_start_lock()


@app.command()
def worker() -> None:
    """Start the background task worker."""
    root = _resolve_project_root()
    ensure_startup_config_ready()
    typer.echo(f"[g3ku] project root: {root}")
    typer.echo("[g3ku] task runtime role: worker")
    try:
        asyncio.run(_run_worker_runtime())
    except KeyboardInterrupt:
        raise typer.Exit(0)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
