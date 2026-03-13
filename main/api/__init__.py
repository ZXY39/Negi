from fastapi import APIRouter

from main.api.admin_rest import router as admin_router
from main.api.rest import router as rest_router
from main.api.websocket_task import router as task_ws_router

router = APIRouter()
router.include_router(rest_router)
router.include_router(admin_router)
router.include_router(task_ws_router)

__all__ = ['router']
