from __future__ import annotations

from pathlib import Path
from typing import Any

import json
import yaml
from fastapi import APIRouter, Body, HTTPException, Query

from g3ku.config.loader import load_config
from g3ku.config.model_manager import ModelManager, VALID_SCOPES
from g3ku.resources.tool_settings import MemoryRuntimeSettings, load_tool_settings_from_manifest
from g3ku.shells.web import get_agent, refresh_web_agent_runtime

router = APIRouter()



def _service():
    agent = get_agent()
    service = getattr(agent, 'main_task_service', None)
    if service is None:
        raise HTTPException(status_code=503, detail='main_task_service_unavailable')
    return service


async def _refresh_runtime(reason: str) -> None:
    try:
        await refresh_web_agent_runtime(force=True, reason=reason)
    except Exception:
        return



def _model_roles(manager: ModelManager) -> dict[str, list[str]]:
    return {scope: list(getattr(manager.config.models.roles, scope)) for scope in VALID_SCOPES}


def _memory_manifest_path(manager: ModelManager) -> Path:
    return manager.config.workspace_path / 'tools' / 'memory_runtime' / 'resource.yaml'


def _load_memory_manifest_payload(manager: ModelManager) -> tuple[dict[str, Any], MemoryRuntimeSettings]:
    manifest_path = _memory_manifest_path(manager)
    data = yaml.safe_load(manifest_path.read_text(encoding='utf-8')) if manifest_path.exists() else {}
    settings = load_tool_settings_from_manifest(manager.config.workspace_path, 'memory_runtime', MemoryRuntimeSettings)
    return (data or {}), settings


def _save_memory_manifest_payload(manager: ModelManager, payload: dict[str, Any]) -> None:
    manifest_path = _memory_manifest_path(manager)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding='utf-8')


@router.get('/models')
async def list_models():
    manager = ModelManager.load()
    return {'ok': True, 'items': manager.list_models(), 'roles': _model_roles(manager)}


@router.post('/models')
async def create_model(payload: dict = Body(...)):
    manager = ModelManager.load()
    try:
        item = manager.add_model(
            key=str(payload.get('key') or '').strip(),
            provider_model=str(payload.get('provider_model') or '').strip(),
            api_key=str(payload.get('api_key') or '').strip(),
            api_base=str(payload.get('api_base') or '').strip(),
            scopes=[str(item) for item in (payload.get('scopes') or [])],
            extra_headers=payload.get('extra_headers') if isinstance(payload.get('extra_headers'), dict) else None,
            enabled=bool(payload.get('enabled', True)),
            max_tokens=payload.get('max_tokens'),
            temperature=payload.get('temperature'),
            reasoning_effort=payload.get('reasoning_effort'),
            retry_on=[str(item) for item in (payload.get('retry_on') or [])] if payload.get('retry_on') is not None else None,
            description=str(payload.get('description') or ''),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await _refresh_runtime('admin_model_create')
    return {'ok': True, 'item': item}


@router.put('/models/{model_key}')
async def update_model(model_key: str, payload: dict = Body(...)):
    manager = ModelManager.load()
    try:
        item = manager.update_model(
            key=model_key,
            provider_model=payload.get('provider_model'),
            api_key=payload.get('api_key'),
            api_base=payload.get('api_base'),
            extra_headers=payload.get('extra_headers') if isinstance(payload.get('extra_headers'), dict) else None,
            max_tokens=payload.get('max_tokens'),
            temperature=payload.get('temperature'),
            reasoning_effort=payload.get('reasoning_effort'),
            retry_on=[str(item) for item in (payload.get('retry_on') or [])] if payload.get('retry_on') is not None else None,
            description=payload.get('description'),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await _refresh_runtime('admin_model_update')
    return {'ok': True, 'item': item}


@router.post('/models/{model_key}/enable')
async def enable_model(model_key: str):
    manager = ModelManager.load()
    try:
        item = manager.set_model_enabled(model_key, True)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await _refresh_runtime('admin_model_enable')
    return {'ok': True, 'item': item}


@router.post('/models/{model_key}/disable')
async def disable_model(model_key: str):
    manager = ModelManager.load()
    try:
        item = manager.set_model_enabled(model_key, False)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await _refresh_runtime('admin_model_disable')
    return {'ok': True, 'item': item}


@router.delete('/models/{model_key}')
async def delete_model(model_key: str):
    manager = ModelManager.load()
    try:
        item = manager.delete_model(model_key)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await _refresh_runtime('admin_model_delete')
    return {'ok': True, 'item': item}


@router.put('/models/roles/{scope}')
async def update_model_roles(scope: str, payload: dict = Body(...)):
    manager = ModelManager.load()
    try:
        roles = manager.set_scope_chain(scope, [str(item) for item in (payload.get('model_keys') or [])])
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await _refresh_runtime('admin_model_roles')
    return {'ok': True, 'scope': scope, 'roles': roles, 'all_roles': _model_roles(manager)}


@router.get('/llm/templates')
async def list_llm_templates():
    manager = ModelManager.load()
    return {'ok': True, 'items': manager.list_templates()}


@router.get('/llm/templates/{provider_id}')
async def get_llm_template(provider_id: str):
    manager = ModelManager.load()
    try:
        item = manager.get_template(provider_id)
    except Exception as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {'ok': True, 'item': item}


@router.post('/llm/drafts/validate')
async def validate_llm_draft(payload: dict = Body(...)):
    manager = ModelManager.load()
    try:
        result = manager.validate_draft(payload)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {'ok': True, 'result': result}


@router.post('/llm/drafts/probe')
async def probe_llm_draft(payload: dict = Body(...)):
    manager = ModelManager.load()
    try:
        result = manager.probe_draft(payload)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {'ok': True, 'result': result}


@router.get('/llm/configs')
async def list_llm_configs():
    manager = ModelManager.load()
    return {'ok': True, 'items': manager.facade.list_config_records()}


@router.get('/llm/configs/{config_id}')
async def get_llm_config(config_id: str, include_secrets: bool = Query(False)):
    manager = ModelManager.load()
    try:
        item = manager.facade.get_config_record(config_id, include_secrets=include_secrets)
    except Exception as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {'ok': True, 'item': item}


@router.post('/llm/configs')
async def create_llm_config(payload: dict = Body(...)):
    manager = ModelManager.load()
    try:
        item = manager.facade.create_config_record(payload)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {'ok': True, 'item': item}


@router.put('/llm/configs/{config_id}')
async def update_llm_config(config_id: str, payload: dict = Body(...)):
    manager = ModelManager.load()
    try:
        item = manager.facade.update_config_record(config_id, payload)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {'ok': True, 'item': item}


@router.delete('/llm/configs/{config_id}')
async def delete_llm_config(config_id: str):
    manager = ModelManager.load()
    try:
        manager.facade.delete_config_record(config_id)
    except Exception as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {'ok': True}


@router.get('/llm/bindings')
async def list_llm_bindings():
    manager = ModelManager.load()
    return {'ok': True, 'items': manager.list_models(), 'routes': manager.facade.get_routes(manager.config)}


@router.post('/llm/bindings')
async def create_llm_binding(payload: dict = Body(...)):
    manager = ModelManager.load()
    draft = payload.get('draft') if isinstance(payload.get('draft'), dict) else {}
    binding = payload.get('binding') if isinstance(payload.get('binding'), dict) else {}
    try:
        item = manager.facade.create_binding(manager.config, draft_payload=draft, binding_payload=binding)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    manager.save()
    await _refresh_runtime('admin_llm_binding_create')
    return {'ok': True, 'item': item}


@router.put('/llm/bindings/{model_key}')
async def update_llm_binding(model_key: str, payload: dict = Body(...)):
    manager = ModelManager.load()
    try:
        item = manager.facade.update_binding(manager.config, model_key=model_key, draft_payload=payload)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    manager.save()
    await _refresh_runtime('admin_llm_binding_update')
    return {'ok': True, 'item': item}


@router.post('/llm/bindings/{model_key}/enable')
async def enable_llm_binding(model_key: str):
    manager = ModelManager.load()
    try:
        item = manager.set_model_enabled(model_key, True)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await _refresh_runtime('admin_llm_binding_enable')
    return {'ok': True, 'item': item}


@router.post('/llm/bindings/{model_key}/disable')
async def disable_llm_binding(model_key: str):
    manager = ModelManager.load()
    try:
        item = manager.set_model_enabled(model_key, False)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await _refresh_runtime('admin_llm_binding_disable')
    return {'ok': True, 'item': item}


@router.delete('/llm/bindings/{model_key}')
async def delete_llm_binding(model_key: str):
    manager = ModelManager.load()
    try:
        manager.delete_model(model_key)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await _refresh_runtime('admin_llm_binding_delete')
    return {'ok': True}


@router.get('/llm/routes')
async def get_llm_routes():
    manager = ModelManager.load()
    return {'ok': True, 'routes': manager.facade.get_routes(manager.config)}


@router.put('/llm/routes/{scope}')
async def update_llm_route(scope: str, payload: dict = Body(...)):
    manager = ModelManager.load()
    try:
        routes = manager.facade.set_route(manager.config, scope, [str(item) for item in (payload.get('model_keys') or [])])
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    manager.save()
    await _refresh_runtime('admin_llm_route_update')
    return {'ok': True, 'routes': routes}


@router.get('/llm/memory')
async def get_llm_memory_binding():
    manager = ModelManager.load()
    try:
        _payload, settings = _load_memory_manifest_payload(manager)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {'ok': True, 'item': manager.facade.get_memory_binding(settings).model_dump(mode='json')}


@router.put('/llm/memory')
async def update_llm_memory_binding(payload: dict = Body(...)):
    manager = ModelManager.load()
    try:
        manifest_payload, settings = _load_memory_manifest_payload(manager)
        result = manager.facade.set_memory_binding(
            settings,
            embedding_model_key=payload.get('embedding_model_key'),
            rerank_model_key=payload.get('rerank_model_key'),
        )
        settings_payload = manifest_payload.setdefault('settings', {})
        embedding_payload = settings_payload.setdefault('embedding', {})
        retrieval_payload = settings_payload.setdefault('retrieval', {})
        embedding_payload['model_key'] = result.embedding_model_key
        retrieval_payload['rerank_model_key'] = result.rerank_model_key
        _save_memory_manifest_payload(manager, manifest_payload)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await _refresh_runtime('admin_llm_memory_update')
    return {'ok': True, 'item': result.model_dump(mode='json')}


@router.post('/llm/migrate')
async def run_llm_migration():
    from g3ku.config.loader import load_config

    try:
        load_config()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await _refresh_runtime('admin_llm_migrate')
    return {'ok': True}


@router.get('/resources/skills')
async def list_skills():
    service = _service()
    await service.startup()
    return {'ok': True, 'items': [item.model_dump(mode='json') for item in service.list_skill_resources()]}


@router.get('/resources/skills/{skill_id}')
async def get_skill(skill_id: str):
    service = _service()
    await service.startup()
    item = service.get_skill_resource(skill_id)
    if item is None:
        raise HTTPException(status_code=404, detail='skill_not_found')
    return {
        'ok': True,
        'item': item.model_dump(mode='json'),
        'files': [{'file_key': file_key, 'path': path} for file_key, path in service.list_skill_files(skill_id).items()],
    }


@router.get('/resources/skills/{skill_id}/files')
async def list_skill_files(skill_id: str):
    service = _service()
    await service.startup()
    item = service.get_skill_resource(skill_id)
    if item is None:
        raise HTTPException(status_code=404, detail='skill_not_found')
    return {'ok': True, 'items': [{'file_key': file_key, 'path': path} for file_key, path in service.list_skill_files(skill_id).items()]}


@router.get('/resources/skills/{skill_id}/files/{file_key}')
async def get_skill_file(skill_id: str, file_key: str):
    service = _service()
    await service.startup()
    if service.get_skill_resource(skill_id) is None:
        raise HTTPException(status_code=404, detail='skill_not_found')
    try:
        content = service.read_skill_file(skill_id, file_key)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {
        'ok': True,
        'file_key': file_key,
        'path': service.list_skill_files(skill_id).get(file_key, ''),
        'content': content,
    }


@router.put('/resources/skills/{skill_id}/files/{file_key}')
async def update_skill_file(skill_id: str, file_key: str, payload: dict = Body(...), session_id: str = Query('web:shared')):
    service = _service()
    await service.startup()
    if service.get_skill_resource(skill_id) is None:
        raise HTTPException(status_code=404, detail='skill_not_found')
    try:
        item = await service.write_skill_file_async(skill_id, file_key, str(payload.get('content') or ''), session_id=session_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {'ok': True, 'item': item}


@router.put('/resources/skills/{skill_id}/policy')
async def update_skill_policy(skill_id: str, payload: dict = Body(...), session_id: str = Query('web:shared')):
    service = _service()
    await service.startup()
    item = service.update_skill_policy(
        skill_id,
        session_id=session_id,
        enabled=payload.get('enabled'),
        allowed_roles=[str(item) for item in (payload.get('allowed_roles') or [])] if payload.get('allowed_roles') is not None else None,
    )
    if item is None:
        raise HTTPException(status_code=404, detail='skill_not_found')
    return {'ok': True, 'item': item.model_dump(mode='json')}


@router.post('/resources/skills/{skill_id}/enable')
async def enable_skill(skill_id: str, session_id: str = Query('web:shared')):
    service = _service()
    await service.startup()
    item = service.enable_skill(skill_id, session_id=session_id)
    if item is None:
        raise HTTPException(status_code=404, detail='skill_not_found')
    return {'ok': True, 'item': item.model_dump(mode='json')}


@router.post('/resources/skills/{skill_id}/disable')
async def disable_skill(skill_id: str, session_id: str = Query('web:shared')):
    service = _service()
    await service.startup()
    item = service.disable_skill(skill_id, session_id=session_id)
    if item is None:
        raise HTTPException(status_code=404, detail='skill_not_found')
    return {'ok': True, 'item': item.model_dump(mode='json')}


@router.get('/resources/tools')
async def list_tools():
    service = _service()
    await service.startup()
    return {'ok': True, 'items': [item.model_dump(mode='json') for item in service.list_tool_resources()]}


@router.get('/resources/tools/{tool_id}')
async def get_tool(tool_id: str):
    service = _service()
    await service.startup()
    item = service.get_tool_family(tool_id)
    if item is None:
        raise HTTPException(status_code=404, detail='tool_not_found')
    return {'ok': True, 'item': item.model_dump(mode='json')}


@router.get('/resources/tools/{tool_id}/toolskill')
async def get_tool_toolskill(tool_id: str):
    service = _service()
    await service.startup()
    payload = service.get_tool_toolskill(tool_id)
    if payload is None:
        raise HTTPException(status_code=404, detail='tool_not_found')
    return {'ok': True, **payload}


def _china_bridge_status_path() -> Path:
    cfg = load_config()
    return cfg.workspace_path / str(cfg.china_bridge.state_dir or '.g3ku/china-bridge') / 'status.json'


@router.get('/china-bridge/status')
async def get_china_bridge_status():
    path = _china_bridge_status_path()
    if not path.exists():
        return {'ok': False, 'available': False, 'path': str(path), 'error': 'status_not_found'}
    return {'ok': True, 'available': True, 'path': str(path), 'item': json.loads(path.read_text(encoding='utf-8'))}


@router.get('/china-bridge/doctor')
async def get_china_bridge_doctor():
    cfg = load_config()
    path = _china_bridge_status_path()
    dist_entry = cfg.workspace_path / 'subsystems' / 'china_channels_host' / 'dist' / 'index.js'
    payload = {
        'enabled': cfg.china_bridge.enabled,
        'public_port': cfg.china_bridge.public_port,
        'control_port': cfg.china_bridge.control_port,
        'node_bin': cfg.china_bridge.node_bin,
        'dist_entry': str(dist_entry),
        'dist_exists': dist_entry.exists(),
        'status_path': str(path),
        'status_exists': path.exists(),
        'channels': {
            'qqbot': cfg.channels.qqbot.enabled,
            'dingtalk': cfg.channels.dingtalk.enabled,
            'wecom': cfg.channels.wecom.enabled,
            'wecom_app': cfg.channels.wecom_app.enabled,
            'feishu_china': cfg.channels.feishu_china.enabled,
        },
    }
    if path.exists():
        payload['status'] = json.loads(path.read_text(encoding='utf-8'))
    return {'ok': True, 'item': payload}


@router.post('/china-bridge/restart')
async def restart_china_bridge():
    path = _china_bridge_status_path()
    if not path.exists():
        raise HTTPException(status_code=404, detail='china_bridge_status_not_found')
    payload = json.loads(path.read_text(encoding='utf-8'))
    pid = int(payload.get('pid') or 0)
    if pid <= 0:
        raise HTTPException(status_code=400, detail='china_bridge_pid_unavailable')
    try:
        import os
        import signal

        os.kill(pid, signal.SIGTERM)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {'ok': True, 'pid': pid}


@router.put('/resources/tools/{tool_id}/policy')
async def update_tool_policy(tool_id: str, payload: dict = Body(...), session_id: str = Query('web:shared')):
    service = _service()
    await service.startup()
    actions_payload = payload.get('actions') if isinstance(payload.get('actions'), dict) else None
    normalized_actions: dict[str, list[str]] | None = None
    if actions_payload is not None:
        normalized_actions = {
            str(action_id): [str(role) for role in (roles or [])]
            for action_id, roles in actions_payload.items()
        }
    item = service.update_tool_policy(tool_id, session_id=session_id, enabled=payload.get('enabled'), allowed_roles_by_action=normalized_actions)
    if item is None:
        raise HTTPException(status_code=404, detail='tool_not_found')
    return {'ok': True, 'item': item.model_dump(mode='json')}


@router.post('/resources/tools/{tool_id}/enable')
async def enable_tool(tool_id: str, session_id: str = Query('web:shared')):
    service = _service()
    await service.startup()
    item = service.enable_tool(tool_id, session_id=session_id)
    if item is None:
        raise HTTPException(status_code=404, detail='tool_not_found')
    return {'ok': True, 'item': item.model_dump(mode='json')}


@router.post('/resources/tools/{tool_id}/disable')
async def disable_tool(tool_id: str, session_id: str = Query('web:shared')):
    service = _service()
    await service.startup()
    item = service.disable_tool(tool_id, session_id=session_id)
    if item is None:
        raise HTTPException(status_code=404, detail='tool_not_found')
    return {'ok': True, 'item': item.model_dump(mode='json')}


@router.post('/resources/reload')
async def reload_resources(payload: dict[str, Any] | None = Body(default=None), session_id: str = Query('web:shared')):
    service = _service()
    await service.startup()
    effective_session_id = str((payload or {}).get('session_id') or session_id or 'web:shared')
    result = await service.reload_resources_async(session_id=effective_session_id)
    return {'ok': True, **result}


@router.get('/memory/retrieval-traces')
async def get_retrieval_traces(limit: int = Query(20, ge=1, le=200)):
    service = _service()
    await service.startup()
    return await service.get_context_traces(trace_kind='retrieval', limit=limit)


@router.get('/memory/context-assembly-traces')
async def get_context_assembly_traces(limit: int = Query(20, ge=1, le=200)):
    service = _service()
    await service.startup()
    return await service.get_context_traces(trace_kind='context_assembly', limit=limit)
