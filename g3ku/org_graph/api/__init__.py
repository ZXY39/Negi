from fastapi import APIRouter

from g3ku.org_graph.api.rest import router as rest_router
from g3ku.org_graph.api.websocket_ceo import router as ceo_ws_router
from g3ku.org_graph.api.websocket_project import router as project_ws_router

router = APIRouter()
router.include_router(rest_router)
router.include_router(ceo_ws_router)
router.include_router(project_ws_router)

