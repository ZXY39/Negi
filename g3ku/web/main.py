import mimetypes
import os
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse

from g3ku.shells.web import ensure_web_runtime_services, get_agent, shutdown_web_runtime
from g3ku.runtime.api import router as runtime_router
from g3ku.web.windows_asyncio import install_windows_connection_reset_filter
from main.api import router as main_router

mimetypes.add_type('text/css', '.css')
mimetypes.add_type('application/javascript', '.js')
os.environ.setdefault('G3KU_TASK_RUNTIME_ROLE', 'web')


@asynccontextmanager
async def lifespan(_app: FastAPI):
    restore_asyncio_filter = install_windows_connection_reset_filter()
    try:
        agent = get_agent()
        await ensure_web_runtime_services(agent)
        yield
    finally:
        await shutdown_web_runtime()
        restore_asyncio_filter()


app = FastAPI(title='G3ku Web GUI', lifespan=lifespan)
app.include_router(main_router, prefix='/api')
app.include_router(runtime_router, prefix='/api')

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
    uvicorn.run('g3ku.web.main:app', host='127.0.0.1', port=3000, reload=False)


if __name__ == '__main__':
    run()
