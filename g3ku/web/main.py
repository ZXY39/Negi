import atexit
import asyncio
import mimetypes
import os
import signal
import threading
from contextlib import asynccontextmanager, suppress
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from loguru import logger

from g3ku.security import get_bootstrap_security_service
from g3ku.shells.web import ensure_web_runtime_services, shutdown_web_runtime
from g3ku.runtime.api import router as runtime_router
from g3ku.web.launcher import run_default_web_entrypoint
from g3ku.web.frontend_assets import ensure_frontend_vendor_assets, frontend_assets_available
from g3ku.web.server_control import request_server_shutdown, set_server_instance
from g3ku.web.windows_asyncio import install_windows_connection_reset_filter
from main.api import router as main_router

mimetypes.add_type('text/css', '.css')
mimetypes.add_type('application/javascript', '.js')
os.environ.setdefault('G3KU_TASK_RUNTIME_ROLE', 'web')

_SHUTDOWN_HOOKS_LOCK = threading.RLock()
_SHUTDOWN_HOOKS_INSTALLED = False
_RUNTIME_SHUTDOWN_LOOP: asyncio.AbstractEventLoop | None = None


async def _refresh_frontend_assets_in_background() -> None:
    try:
        await asyncio.to_thread(ensure_frontend_vendor_assets)
    except Exception as exc:
        logger.warning("frontend asset sync skipped: {}", exc)


def _set_runtime_shutdown_loop(loop: asyncio.AbstractEventLoop | None) -> None:
    global _RUNTIME_SHUTDOWN_LOOP
    _RUNTIME_SHUTDOWN_LOOP = loop


def _schedule_runtime_shutdown(reason: str = "process_exit") -> bool:
    _ = reason
    request_server_shutdown()
    loop = _RUNTIME_SHUTDOWN_LOOP
    if loop is not None and loop.is_running() and not loop.is_closed():
        try:
            asyncio.run_coroutine_threadsafe(shutdown_web_runtime(), loop)
            return True
        except Exception as exc:
            logger.debug("runtime shutdown scheduling skipped: {}", exc)
    try:
        running_loop = asyncio.get_running_loop()
    except RuntimeError:
        running_loop = None
    if running_loop is not None and running_loop.is_running() and not running_loop.is_closed():
        try:
            running_loop.create_task(shutdown_web_runtime())
            return True
        except Exception as exc:
            logger.debug("runtime shutdown task scheduling skipped: {}", exc)
    return False


def _sync_runtime_shutdown(reason: str = "process_exit") -> None:
    if _schedule_runtime_shutdown(reason):
        return
    try:
        asyncio.run(shutdown_web_runtime())
    except RuntimeError:
        logger.debug("runtime shutdown fallback skipped because no compatible event loop was available")
    except Exception as exc:
        logger.debug("runtime shutdown fallback failed: {}", exc)


def _handle_process_shutdown_signal(signum, _frame) -> None:
    _sync_runtime_shutdown(f"signal:{int(signum)}")


def _install_process_shutdown_hooks() -> None:
    global _SHUTDOWN_HOOKS_INSTALLED
    with _SHUTDOWN_HOOKS_LOCK:
        if _SHUTDOWN_HOOKS_INSTALLED:
            return
        for signal_name in ("SIGINT", "SIGTERM", "SIGBREAK"):
            signal_value = getattr(signal, signal_name, None)
            if signal_value is None:
                continue
            try:
                signal.signal(signal_value, _handle_process_shutdown_signal)
            except Exception:
                continue
        atexit.register(_sync_runtime_shutdown, "atexit")
        _SHUTDOWN_HOOKS_INSTALLED = True


@asynccontextmanager
async def lifespan(_app: FastAPI):
    restore_asyncio_filter = install_windows_connection_reset_filter()
    _install_process_shutdown_hooks()
    _set_runtime_shutdown_loop(asyncio.get_running_loop())
    asset_refresh_task: asyncio.Task | None = None
    try:
        try:
            if frontend_assets_available():
                asset_refresh_task = asyncio.create_task(_refresh_frontend_assets_in_background())
            else:
                await asyncio.to_thread(ensure_frontend_vendor_assets)
        except Exception as exc:
            logger.warning("frontend asset sync skipped: {}", exc)

        security = get_bootstrap_security_service()
        if security.is_unlocked():
            try:
                await ensure_web_runtime_services()
            except Exception as exc:
                logger.warning('web runtime init on startup skipped: {}', exc)
        yield
    finally:
        if asset_refresh_task is not None and not asset_refresh_task.done():
            asset_refresh_task.cancel()
            with suppress(asyncio.CancelledError):
                await asset_refresh_task
        await shutdown_web_runtime()
        _set_runtime_shutdown_loop(None)
        restore_asyncio_filter()


app = FastAPI(title='G3ku Web GUI', lifespan=lifespan)
app.include_router(main_router, prefix='/api')
app.include_router(runtime_router, prefix='/api')


@app.middleware("http")
async def bootstrap_lock_middleware(request: Request, call_next):
    path = str(request.url.path or "")
    is_api_path = path == "/api" or path.startswith("/api/")
    if is_api_path and not path.startswith("/api/bootstrap"):
        security = get_bootstrap_security_service()
        if not security.is_unlocked():
            return JSONResponse(
                status_code=423,
                content={
                    "detail": "project_locked",
                    "mode": security.status().get("mode"),
                },
            )
    return await call_next(request)

WEB_DIR = Path(__file__).resolve().parent
FRONTEND_DIR = WEB_DIR / 'frontend'
FRONTEND_ENTRY = FRONTEND_DIR / 'org_graph.html'
FRONTEND_NO_CACHE_HEADERS = {
    'Cache-Control': 'no-store, no-cache, must-revalidate, max-age=0',
    'Pragma': 'no-cache',
    'Expires': '0',
}


def _safe_child_file(base_dir: Path, relative_path: str) -> Path:
    base_dir = base_dir.resolve()
    candidate = (base_dir / relative_path).resolve()
    try:
        candidate.relative_to(base_dir)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail='Not found') from exc
    if not candidate.exists() or not candidate.is_file():
        raise HTTPException(status_code=404, detail=f'Frontend asset not found at {candidate}')
    return candidate


def _frontend_index_response() -> FileResponse:
    if not FRONTEND_ENTRY.exists():
        raise HTTPException(status_code=404, detail=f'Frontend not found at {FRONTEND_ENTRY}')
    return FileResponse(str(FRONTEND_ENTRY), headers=FRONTEND_NO_CACHE_HEADERS)


@app.get('/')
async def serve_frontend_root():
    return _frontend_index_response()


@app.get('/{full_path:path}')
async def serve_frontend_file(full_path: str):
    if full_path == 'api' or full_path.startswith('api/'):
        raise HTTPException(status_code=404, detail='Not found')
    candidate = (FRONTEND_DIR / full_path).resolve()
    try:
        candidate.relative_to(FRONTEND_DIR.resolve())
    except ValueError as exc:
        raise HTTPException(status_code=404, detail='Not found') from exc
    if candidate.exists() and candidate.is_file():
        return FileResponse(str(candidate), headers=FRONTEND_NO_CACHE_HEADERS)
    return _frontend_index_response()


def run():
    run_default_web_entrypoint()


def run_server(*, host: str, port: int, reload: bool, log_level: str = 'info') -> None:
    _install_process_shutdown_hooks()
    if reload:
        uvicorn.run('g3ku.web.main:app', host=host, port=port, reload=True, log_level=log_level)
        return
    config = uvicorn.Config('g3ku.web.main:app', host=host, port=port, reload=False, log_level=log_level)
    server = uvicorn.Server(config)
    set_server_instance(server)
    try:
        server.run()
    finally:
        set_server_instance(None)


if __name__ == '__main__':
    run()
