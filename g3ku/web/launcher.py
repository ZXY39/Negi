from __future__ import annotations

import atexit
import asyncio
import json
import os
import secrets
from pathlib import Path

import typer

from g3ku.config.loader import ensure_startup_config_ready, load_config
from g3ku.runtime.bootstrap_factory import make_agent_loop as _make_agent_loop
from g3ku.runtime.bootstrap_factory import make_provider as _make_provider
from g3ku.security import BOOTSTRAP_MASTER_KEY_ENV, get_bootstrap_security_service
from g3ku.utils.sdk_logging import configure_openai_sdk_logging
from g3ku.utils.helpers import sync_workspace_templates
from g3ku.web.worker_control import WEB_AUTO_WORKER_ENV
from main.service.task_terminal_callback import (
    TASK_TERMINAL_CALLBACK_PATH,
    TASK_TERMINAL_CALLBACK_TOKEN_ENV,
    TASK_TERMINAL_CALLBACK_URL_ENV,
    save_task_terminal_callback_config,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
FRONTEND_ENTRY = PROJECT_ROOT / "g3ku" / "web" / "frontend" / "org_graph.html"
_START_LOCK_HANDLE = None


def _resolve_project_root() -> Path:
    os.chdir(PROJECT_ROOT)
    return PROJECT_ROOT


def _ensure_frontend_ready() -> None:
    if not FRONTEND_ENTRY.exists():
        raise typer.BadParameter(f"Missing frontend entry file: {FRONTEND_ENTRY}")


def _resolve_web_bind(host: str | None, port: int | None) -> tuple[str, int]:
    cfg = load_config()
    resolved_host = str(host or cfg.web.host or "127.0.0.1").strip() or "127.0.0.1"
    resolved_port = int(port if port is not None else getattr(cfg.web, "port", 18790) or 18790)
    return resolved_host, resolved_port


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


def release_web_start_lock() -> None:
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


def _acquire_web_start_lock(root: Path, *, port: int) -> None:
    global _START_LOCK_HANDLE
    release_web_start_lock()

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
            "Another `g3ku web` process is already running for this workspace "
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
    atexit.register(release_web_start_lock)


def prepare_web_server_start(
    *,
    host: str | None,
    port: int | None,
    reload: bool,
    with_worker: bool,
) -> tuple[Path, str, int]:
    root = _resolve_project_root()
    configure_openai_sdk_logging()
    ensure_startup_config_ready()
    _ensure_frontend_ready()
    resolved_host, resolved_port = _resolve_web_bind(host, port)
    _acquire_web_start_lock(root, port=resolved_port)

    callback_token = secrets.token_urlsafe(24)
    callback_url = f"http://127.0.0.1:{resolved_port}{TASK_TERMINAL_CALLBACK_PATH}"
    os.environ[TASK_TERMINAL_CALLBACK_URL_ENV] = callback_url
    os.environ[TASK_TERMINAL_CALLBACK_TOKEN_ENV] = callback_token
    save_task_terminal_callback_config(workspace=root, url=callback_url, token=callback_token)
    if with_worker and not reload:
        os.environ[WEB_AUTO_WORKER_ENV] = "1"
    else:
        os.environ.pop(WEB_AUTO_WORKER_ENV, None)
    return root, resolved_host, resolved_port


def run_web_server_entrypoint(
    *,
    host: str | None = None,
    port: int | None = None,
    reload: bool = False,
    log_level: str = "info",
    with_worker: bool = True,
) -> None:
    _root, resolved_host, resolved_port = prepare_web_server_start(
        host=host,
        port=port,
        reload=reload,
        with_worker=with_worker,
    )
    try:
        if with_worker and reload:
            typer.echo(
                "[g3ku] worker auto-start is disabled when --reload is enabled; "
                "run `python -m g3ku worker` separately."
            )
        from g3ku.web.main import run_server

        run_server(
            host=resolved_host,
            port=resolved_port,
            reload=reload,
            log_level=log_level,
        )
    finally:
        release_web_start_lock()


def run_default_web_entrypoint() -> None:
    run_web_server_entrypoint()


async def run_worker_runtime() -> None:
    from g3ku.bus.queue import MessageBus

    _resolve_project_root()
    configure_openai_sdk_logging()
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
