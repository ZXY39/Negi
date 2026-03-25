import mimetypes
import os
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from loguru import logger

from g3ku.security import get_bootstrap_security_service
from g3ku.shells.web import ensure_web_runtime_services, shutdown_web_runtime
from g3ku.runtime.api import router as runtime_router
from g3ku.web.launcher import run_default_web_entrypoint
from g3ku.web.server_control import request_server_shutdown, set_server_instance
from g3ku.web.windows_asyncio import install_windows_connection_reset_filter
from main.api import router as main_router

mimetypes.add_type('text/css', '.css')
mimetypes.add_type('application/javascript', '.js')
os.environ.setdefault('G3KU_TASK_RUNTIME_ROLE', 'web')


@asynccontextmanager
async def lifespan(_app: FastAPI):
    restore_asyncio_filter = install_windows_connection_reset_filter()
    try:
        security = get_bootstrap_security_service()
        if security.is_unlocked():
            try:
                await ensure_web_runtime_services()
            except Exception as exc:
                logger.warning('web runtime init on startup skipped: {}', exc)
        yield
    finally:
        await shutdown_web_runtime()
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
