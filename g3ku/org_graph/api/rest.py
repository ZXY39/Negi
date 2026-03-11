from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, HTTPException, Query
from pydantic import BaseModel, Field
from fastapi.responses import JSONResponse

from g3ku.org_graph.integration.web_bridge import get_org_graph_service
from g3ku.org_graph.models import ProjectCreateRequest

router = APIRouter()


class ResourcePolicyBody(BaseModel):
    enabled: bool | None = None
    allowed_roles: list[str] | None = None
    actions: list[dict[str, Any]] | None = None


class SkillFileBody(BaseModel):
    content: str = ''


class ModelCatalogBody(BaseModel):
    key: str
    provider_model: str = Field(alias="providerModel")
    api_key: str = Field(alias="apiKey")
    api_base: str = Field(alias="apiBase")
    extra_headers: dict[str, str] | None = Field(default=None, alias="extraHeaders")
    enabled: bool = True
    max_tokens: int | None = Field(default=None, alias="maxTokens")
    temperature: float | None = None
    reasoning_effort: str | None = Field(default=None, alias="reasoningEffort")
    retry_on: list[str] | None = Field(default=None, alias="retryOn")
    description: str = ""
    scopes: list[str] = Field(default_factory=list)


class ModelCatalogPatchBody(BaseModel):
    provider_model: str | None = Field(default=None, alias="providerModel")
    api_key: str | None = Field(default=None, alias="apiKey")
    api_base: str | None = Field(default=None, alias="apiBase")
    extra_headers: dict[str, str] | None = Field(default=None, alias="extraHeaders")
    max_tokens: int | None = Field(default=None, alias="maxTokens")
    temperature: float | None = None
    reasoning_effort: str | None = Field(default=None, alias="reasoningEffort")
    retry_on: list[str] | None = Field(default=None, alias="retryOn")
    description: str | None = None


class ModelRoleChainBody(BaseModel):
    model_keys: list[str] = Field(default_factory=list, alias="modelKeys")


def _paginate(items, *, offset: int, limit: int) -> tuple[list, int, int, int]:
    safe_offset = max(0, int(offset))
    safe_limit = max(1, min(int(limit), 500))
    total = len(items)
    return items[safe_offset:safe_offset + safe_limit], total, safe_offset, safe_limit


@router.post('/projects')
async def create_project(request: ProjectCreateRequest):
    service = get_org_graph_service()
    try:
        project = await service.create_project(request)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse({'ok': True, 'project': project.model_dump(mode='json')})


@router.get('/projects')
async def list_projects(session_id: str = Query('web:shared'), offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=500)):
    service = get_org_graph_service()
    page, total, offset, limit = _paginate([item.model_dump(mode='json') for item in service.list_projects(session_id)], offset=offset, limit=limit)
    return JSONResponse({'ok': True, 'session_id': session_id, 'items': page, 'total': total, 'offset': offset, 'limit': limit})


@router.get('/models')
async def list_provider_models():
    service = get_org_graph_service()
    return JSONResponse({'ok': True, **service.list_model_catalog()})


@router.post('/models')
async def create_model_catalog_entry(body: ModelCatalogBody):
    service = get_org_graph_service()
    try:
        model = await service.add_model_catalog_entry(body.model_dump(mode='json'))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse({'ok': True, 'model': model, **service.list_model_catalog()})


@router.put('/models/roles/{scope}')
async def update_model_role_chain(scope: str, body: ModelRoleChainBody):
    service = get_org_graph_service()
    try:
        result = await service.update_model_role_chain(scope, body.model_keys)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse({'ok': True, 'result': result, **service.list_model_catalog()})


@router.get('/models/{model_key}')
async def get_model_catalog_entry(model_key: str):
    service = get_org_graph_service()
    item = next((item for item in service.list_model_catalog().get('catalog', []) if item.get('key') == model_key), None)
    if item is None:
        raise HTTPException(status_code=404, detail='model_not_found')
    return JSONResponse({'ok': True, 'model': item})


@router.put('/models/{model_key}')
async def update_model_catalog_entry(model_key: str, body: ModelCatalogPatchBody):
    service = get_org_graph_service()
    try:
        model = await service.update_model_catalog_entry(model_key, body.model_dump(mode='json', exclude_none=True))
    except ValueError as exc:
        detail = str(exc)
        if 'Unknown model key' in detail:
            raise HTTPException(status_code=404, detail='model_not_found') from exc
        raise HTTPException(status_code=400, detail=detail) from exc
    return JSONResponse({'ok': True, 'model': model, **service.list_model_catalog()})


@router.post('/models/{model_key}/enable')
async def enable_model_catalog_entry(model_key: str):
    service = get_org_graph_service()
    try:
        model = await service.set_model_catalog_entry_enabled(model_key, True)
    except ValueError as exc:
        detail = str(exc)
        if 'Unknown model key' in detail:
            raise HTTPException(status_code=404, detail='model_not_found') from exc
        raise HTTPException(status_code=400, detail=detail) from exc
    return JSONResponse({'ok': True, 'model': model, **service.list_model_catalog()})


@router.post('/models/{model_key}/disable')
async def disable_model_catalog_entry(model_key: str):
    service = get_org_graph_service()
    try:
        model = await service.set_model_catalog_entry_enabled(model_key, False)
    except ValueError as exc:
        detail = str(exc)
        if 'Unknown model key' in detail:
            raise HTTPException(status_code=404, detail='model_not_found') from exc
        raise HTTPException(status_code=400, detail=detail) from exc
    return JSONResponse({'ok': True, 'model': model, **service.list_model_catalog()})


@router.get('/projects/{project_id}')
async def get_project(project_id: str):
    service = get_org_graph_service()
    project = service.get_project(project_id)
    if project is None:
        raise HTTPException(status_code=404, detail='project_not_found')
    return JSONResponse({'ok': True, 'project': project.model_dump(mode='json')})


@router.post('/projects/{project_id}/pause')
async def pause_project(project_id: str):
    service = get_org_graph_service()
    project = await service.pause_project(project_id)
    if project is None:
        raise HTTPException(status_code=404, detail='project_not_found')
    return JSONResponse({'ok': True, 'project': project.model_dump(mode='json')})


@router.post('/projects/{project_id}/resume')
async def resume_project(project_id: str):
    service = get_org_graph_service()
    project = await service.resume_project(project_id)
    if project is None:
        raise HTTPException(status_code=404, detail='project_not_found')
    return JSONResponse({'ok': True, 'project': project.model_dump(mode='json')})


@router.post('/projects/{project_id}/cancel')
async def cancel_project(project_id: str):
    service = get_org_graph_service()
    project = await service.cancel_project(project_id)
    if project is None:
        raise HTTPException(status_code=404, detail='project_not_found')
    return JSONResponse({'ok': True, 'project': project.model_dump(mode='json')})


@router.post('/projects/{project_id}/archive')
async def archive_project(project_id: str):
    service = get_org_graph_service()
    project = await service.archive_project(project_id)
    if project is None:
        raise HTTPException(status_code=404, detail='project_not_found')
    return JSONResponse({'ok': True, 'project': project.model_dump(mode='json')})


@router.get('/projects/{project_id}/tree')
async def get_tree(project_id: str):
    service = get_org_graph_service()
    tree = service.get_tree(project_id)
    if tree is None:
        raise HTTPException(status_code=404, detail='project_not_found')
    return JSONResponse({'ok': True, 'project_id': project_id, 'root': tree.model_dump(mode='json')})


@router.get('/projects/{project_id}/units')
async def list_units(project_id: str, offset: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=500)):
    service = get_org_graph_service()
    project = service.get_project(project_id)
    if project is None:
        raise HTTPException(status_code=404, detail='project_not_found')
    page, total, offset, limit = _paginate([item.model_dump(mode='json') for item in service.list_units(project_id)], offset=offset, limit=limit)
    return JSONResponse({'ok': True, 'items': page, 'total': total, 'offset': offset, 'limit': limit})


@router.get('/projects/{project_id}/units/{unit_id}')
async def get_unit(project_id: str, unit_id: str):
    service = get_org_graph_service()
    project = service.get_project(project_id)
    unit = service.get_unit(unit_id)
    if project is None:
        raise HTTPException(status_code=404, detail='project_not_found')
    if unit is None or unit.project_id != project_id:
        raise HTTPException(status_code=404, detail='unit_not_found')
    return JSONResponse({'ok': True, 'unit': unit.model_dump(mode='json')})


@router.get('/projects/{project_id}/stages')
async def list_stages(project_id: str, offset: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=500)):
    service = get_org_graph_service()
    if service.get_project(project_id) is None:
        raise HTTPException(status_code=404, detail='project_not_found')
    page, total, offset, limit = _paginate([item.model_dump(mode='json') for item in service.list_stages(project_id)], offset=offset, limit=limit)
    return JSONResponse({'ok': True, 'items': page, 'total': total, 'offset': offset, 'limit': limit})


@router.get('/projects/{project_id}/events')
async def list_events(project_id: str, after_seq: int = Query(0), limit: int = Query(200)):
    service = get_org_graph_service()
    if service.get_project(project_id) is None:
        raise HTTPException(status_code=404, detail='project_not_found')
    items = [item.model_dump(mode='json') for item in service.list_events(project_id, after_seq=after_seq, limit=limit)]
    return JSONResponse({'ok': True, 'items': items})


@router.get('/projects/{project_id}/artifacts')
async def list_artifacts(project_id: str, offset: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=500)):
    service = get_org_graph_service()
    if service.get_project(project_id) is None:
        raise HTTPException(status_code=404, detail='project_not_found')
    page, total, offset, limit = _paginate([item.model_dump(mode='json') for item in service.list_artifacts(project_id)], offset=offset, limit=limit)
    return JSONResponse({'ok': True, 'items': page, 'total': total, 'offset': offset, 'limit': limit})


@router.get('/projects/{project_id}/artifacts/{artifact_id}')
async def get_artifact(project_id: str, artifact_id: str):
    service = get_org_graph_service()
    if service.get_project(project_id) is None:
        raise HTTPException(status_code=404, detail='project_not_found')
    artifact = service.get_artifact(artifact_id)
    if artifact is None or artifact.project_id != project_id:
        raise HTTPException(status_code=404, detail='artifact_not_found')
    return JSONResponse({'ok': True, 'artifact': artifact.model_dump(mode='json')})


@router.post('/projects/{project_id}/artifacts/{artifact_id}/apply')
async def apply_patch_artifact(project_id: str, artifact_id: str):
    service = get_org_graph_service()
    if service.get_project(project_id) is None:
        raise HTTPException(status_code=404, detail='project_not_found')
    artifact = service.get_artifact(artifact_id)
    if artifact is None or artifact.project_id != project_id:
        raise HTTPException(status_code=404, detail='artifact_not_found')
    try:
        result = await service.apply_patch_artifact(project_id, artifact_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return JSONResponse({'ok': True, 'result': result})


@router.get('/resources/skills')
async def list_skill_resources(offset: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=500)):
    service = get_org_graph_service()
    page, total, offset, limit = _paginate([item.model_dump(mode='json') for item in service.list_skill_resources()], offset=offset, limit=limit)
    return JSONResponse({'ok': True, 'items': page, 'total': total, 'offset': offset, 'limit': limit})


@router.get('/resources/skills/{skill_id}')
async def get_skill_resource(skill_id: str):
    service = get_org_graph_service()
    item = service.get_skill_resource(skill_id)
    if item is None:
        raise HTTPException(status_code=404, detail='skill_not_found')
    return JSONResponse({'ok': True, 'skill': item.model_dump(mode='json')})


@router.get('/resources/skills/{skill_id}/files')
async def list_skill_files(skill_id: str):
    service = get_org_graph_service()
    item = service.get_skill_resource(skill_id)
    if item is None:
        raise HTTPException(status_code=404, detail='skill_not_found')
    return JSONResponse({'ok': True, 'items': [{'file_key': key, 'path': value} for key, value in service.list_skill_files(skill_id).items()]})


@router.get('/resources/skills/{skill_id}/files/{file_key}')
async def get_skill_file(skill_id: str, file_key: str):
    service = get_org_graph_service()
    if service.get_skill_resource(skill_id) is None:
        raise HTTPException(status_code=404, detail='skill_not_found')
    try:
        content = service.read_skill_file(skill_id, file_key)
    except ValueError:
        raise HTTPException(status_code=409, detail='editable_file_not_allowed')
    return JSONResponse({'ok': True, 'file_key': file_key, 'content': content})


@router.put('/resources/skills/{skill_id}/files/{file_key}')
async def put_skill_file(skill_id: str, file_key: str, body: SkillFileBody, session_id: str = Query('web:shared')):
    service = get_org_graph_service()
    if service.get_skill_resource(skill_id) is None:
        raise HTTPException(status_code=404, detail='skill_not_found')
    try:
        result = await service.write_skill_file(skill_id, file_key, body.content, session_id=session_id)
    except ValueError:
        raise HTTPException(status_code=409, detail='editable_file_not_allowed')
    return JSONResponse({'ok': True, 'result': result})


@router.put('/resources/skills/{skill_id}/policy')
async def put_skill_policy(skill_id: str, body: ResourcePolicyBody, session_id: str = Query('web:shared')):
    service = get_org_graph_service()
    item = await service.update_skill_policy(skill_id, session_id=session_id, enabled=body.enabled, allowed_roles=body.allowed_roles)
    if item is None:
        raise HTTPException(status_code=404, detail='skill_not_found')
    return JSONResponse({'ok': True, 'skill': item.model_dump(mode='json')})


@router.post('/resources/skills/{skill_id}/enable')
async def enable_skill(skill_id: str, session_id: str = Query('web:shared')):
    service = get_org_graph_service()
    item = await service.enable_skill(skill_id, session_id=session_id)
    if item is None:
        raise HTTPException(status_code=404, detail='skill_not_found')
    return JSONResponse({'ok': True, 'skill': item.model_dump(mode='json')})


@router.post('/resources/skills/{skill_id}/disable')
async def disable_skill(skill_id: str, session_id: str = Query('web:shared')):
    service = get_org_graph_service()
    item = await service.disable_skill(skill_id, session_id=session_id)
    if item is None:
        raise HTTPException(status_code=404, detail='skill_not_found')
    return JSONResponse({'ok': True, 'skill': item.model_dump(mode='json')})


@router.get('/resources/tools')
async def list_tool_resources(offset: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=500)):
    service = get_org_graph_service()
    page, total, offset, limit = _paginate([item.model_dump(mode='json') for item in service.list_tool_resources()], offset=offset, limit=limit)
    return JSONResponse({'ok': True, 'items': page, 'total': total, 'offset': offset, 'limit': limit})


@router.get('/resources/tools/{tool_id}')
async def get_tool_resource(tool_id: str):
    service = get_org_graph_service()
    item = service.get_tool_resource(tool_id)
    if item is None:
        raise HTTPException(status_code=404, detail='tool_not_found')
    return JSONResponse({'ok': True, 'tool': item.model_dump(mode='json')})


@router.put('/resources/tools/{tool_id}/policy')
async def put_tool_policy(tool_id: str, body: ResourcePolicyBody, session_id: str = Query('web:shared')):
    service = get_org_graph_service()
    item = await service.update_tool_policy(tool_id, session_id=session_id, enabled=body.enabled, actions=body.actions)
    if item is None:
        raise HTTPException(status_code=404, detail='tool_not_found')
    return JSONResponse({'ok': True, 'tool': item.model_dump(mode='json')})


@router.post('/resources/tools/{tool_id}/enable')
async def enable_tool(tool_id: str, session_id: str = Query('web:shared')):
    service = get_org_graph_service()
    item = await service.enable_tool(tool_id, session_id=session_id)
    if item is None:
        raise HTTPException(status_code=404, detail='tool_not_found')
    return JSONResponse({'ok': True, 'tool': item.model_dump(mode='json')})


@router.post('/resources/tools/{tool_id}/disable')
async def disable_tool(tool_id: str, session_id: str = Query('web:shared')):
    service = get_org_graph_service()
    item = await service.disable_tool(tool_id, session_id=session_id)
    if item is None:
        raise HTTPException(status_code=404, detail='tool_not_found')
    return JSONResponse({'ok': True, 'tool': item.model_dump(mode='json')})


@router.post('/resources/reload')
async def reload_resources(session_id: str = Query('web:shared')):
    service = get_org_graph_service()
    result = await service.reload_resources(session_id=session_id)
    return JSONResponse({'ok': True, 'result': result})


@router.get('/notices')
async def list_notices(session_id: str = Query('web:shared'), include_acknowledged: bool = Query(False), offset: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=500)):
    service = get_org_graph_service()
    page, total, offset, limit = _paginate([item.model_dump(mode='json') for item in service.list_notices(session_id, include_acknowledged=include_acknowledged)], offset=offset, limit=limit)
    return JSONResponse({'ok': True, 'session_id': session_id, 'items': page, 'total': total, 'offset': offset, 'limit': limit})


@router.post('/notices/{notice_id}/ack')
async def ack_notice(notice_id: str):
    service = get_org_graph_service()
    notice = service.ack_notice(notice_id)
    if notice is None:
        raise HTTPException(status_code=404, detail='notice_not_found')
    return JSONResponse({'ok': True, 'notice': notice.model_dump(mode='json')})

