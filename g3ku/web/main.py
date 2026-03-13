import mimetypes
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse

from g3ku.shells.web import shutdown_web_runtime
from g3ku.runtime.api import router as runtime_router
from main.api import router as main_router

mimetypes.add_type('text/css', '.css')
mimetypes.add_type('application/javascript', '.js')


@asynccontextmanager
async def lifespan(_app: FastAPI):
    try:
        yield
    finally:
        await shutdown_web_runtime()


app = FastAPI(title='G3ku Web GUI', lifespan=lifespan)
app.include_router(main_router, prefix='/api')
app.include_router(runtime_router, prefix='/api')

WEB_DIR = Path(__file__).resolve().parent
FRONTEND_DIR = WEB_DIR / 'frontend'
FRONTEND_DIST_DIR = FRONTEND_DIR / 'dist'
FRONTEND_ENTRY = FRONTEND_DIST_DIR / 'index.html'
FRONTEND_SOURCE_ENTRY = FRONTEND_DIR / 'src' / 'index.html'
FRONTEND_NO_CACHE_HEADERS = {
    'Cache-Control': 'no-store, no-cache, must-revalidate, max-age=0',
    'Pragma': 'no-cache',
    'Expires': '0',
}
OLD_FRONTEND_PATHS = {'api_client.js', 'org_graph_app.js', 'org_graph.html', 'org_graph.css'}


def _active_assets_dir() -> Path:
    assets_dir = FRONTEND_DIST_DIR / 'assets'
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


def _frontend_index_response() -> FileResponse:
    candidate = FRONTEND_ENTRY if FRONTEND_ENTRY.exists() else FRONTEND_SOURCE_ENTRY
    if not candidate.exists():
        raise HTTPException(status_code=404, detail=f'Frontend not found at {candidate}')
    return FileResponse(str(candidate), headers=FRONTEND_NO_CACHE_HEADERS)


@app.get('/')
async def serve_frontend_root():
    return _frontend_index_response()


@app.get('/assets/{asset_path:path}')
async def serve_frontend_assets(asset_path: str):
    return FileResponse(str(_safe_child_file(_active_assets_dir(), asset_path)), headers=FRONTEND_NO_CACHE_HEADERS)


@app.get('/{full_path:path}')
async def serve_frontend_spa(full_path: str):
    if full_path == 'api' or full_path.startswith('api/'):
        raise HTTPException(status_code=404, detail='Not found')
    if full_path in OLD_FRONTEND_PATHS:
        raise HTTPException(status_code=404, detail='Not found')

    for base_dir in (FRONTEND_DIST_DIR, FRONTEND_DIR):
        candidate = (base_dir / full_path).resolve()
        try:
            candidate.relative_to(base_dir.resolve())
        except ValueError:
            continue
        if candidate.exists() and candidate.is_file():
            return FileResponse(str(candidate), headers=FRONTEND_NO_CACHE_HEADERS)
    return _frontend_index_response()


def run():
    uvicorn.run('g3ku.web.main:app', host='127.0.0.1', port=3000, reload=False)


if __name__ == '__main__':
    run()