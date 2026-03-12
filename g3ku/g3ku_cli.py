from __future__ import annotations

import os
import webbrowser
from pathlib import Path

import typer
import uvicorn

from g3ku.config.loader import load_config


app = typer.Typer(
    add_completion=False,
    rich_markup_mode=None,
    help="G3ku project launcher.",
    no_args_is_help=True,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FRONTEND_ENTRY = PROJECT_ROOT / "g3ku" / "web" / "frontend" / "org_graph.html"


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
) -> None:
    """Start the unified G3ku web app.

    The frontend is served by the backend from `g3ku/web/frontend/org_graph.html`.
    """

    root = _resolve_project_root()
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

    uvicorn.run(
        "g3ku.web.main:app",
        host=host,
        port=resolved_port,
        reload=reload,
    )


def main() -> None:
    app()


if __name__ == "__main__":
    main()
