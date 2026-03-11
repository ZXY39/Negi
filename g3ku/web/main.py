import mimetypes
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse

from g3ku.org_graph.api import router as org_graph_router
from g3ku.org_graph.integration.web_bridge import shutdown_org_graph_runtime, startup_org_graph_runtime

mimetypes.add_type('text/css', '.css')
mimetypes.add_type('application/javascript', '.js')


@asynccontextmanager
async def lifespan(_app: FastAPI):
    try:
        await startup_org_graph_runtime()
        yield
    finally:
        await shutdown_org_graph_runtime()


app = FastAPI(title='G3ku Web GUI', lifespan=lifespan)
app.include_router(org_graph_router, prefix='/api')

WEB_DIR = Path(__file__).resolve().parent
FRONTEND_DIR = WEB_DIR / 'frontend'
FRONTEND_ENTRY = FRONTEND_DIR / 'org_graph.html'
FRONTEND_NO_CACHE_HEADERS = {
    'Cache-Control': 'no-store, no-cache, must-revalidate, max-age=0',
    'Pragma': 'no-cache',
    'Expires': '0',
}


def _active_assets_dir() -> Path:
    assets_dir = FRONTEND_DIR / 'assets'
    if assets_dir.exists():
        return assets_dir
    return FRONTEND_DIR


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


def _frontend_index_response(directory: Path) -> FileResponse:
    if not FRONTEND_ENTRY.exists():
        raise HTTPException(status_code=404, detail=f'Frontend not found at {FRONTEND_ENTRY}')
    return FileResponse(str(FRONTEND_ENTRY), headers=FRONTEND_NO_CACHE_HEADERS)


@app.get('/')
async def serve_frontend_root():
    return _frontend_index_response(FRONTEND_DIR)


@app.get('/assets/{asset_path:path}')
async def serve_frontend_assets(asset_path: str):
    return FileResponse(str(_safe_child_file(_active_assets_dir(), asset_path)), headers=FRONTEND_NO_CACHE_HEADERS)


@app.get('/{full_path:path}')
async def serve_frontend_spa(full_path: str):
    if full_path == 'api' or full_path.startswith('api/'):
        raise HTTPException(status_code=404, detail='Not found')

    candidate = (FRONTEND_DIR / full_path).resolve()
    try:
        candidate.relative_to(FRONTEND_DIR.resolve())
    except ValueError as exc:
        raise HTTPException(status_code=404, detail='Not found') from exc

    if candidate.exists() and candidate.is_file():
        return FileResponse(str(candidate), headers=FRONTEND_NO_CACHE_HEADERS)
    return _frontend_index_response(FRONTEND_DIR)


def run():
    uvicorn.run('g3ku.web.main:app', host='127.0.0.1', port=3000, reload=True)


if __name__ == '__main__':
    run()

