from fastapi import APIRouter

from g3ku.runtime.api.websocket_ceo import router as ceo_ws_router

router = APIRouter()
router.include_router(ceo_ws_router)

__all__ = ['router']
