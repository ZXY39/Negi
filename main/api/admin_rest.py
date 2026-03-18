from __future__ import annotations

from pathlib import Path
from typing import Any

import json
import shutil
from fastapi import APIRouter, Body, HTTPException, Query

from g3ku.config.loader import load_config, save_config
from g3ku.config.schema import Config
from g3ku.config.model_manager import ModelManager, VALID_SCOPES
from g3ku.llm_config.facade import MEMORY_EMBEDDING_CONFIG_ID, MEMORY_RERANK_CONFIG_ID
from g3ku.shells.web import get_agent, refresh_web_agent_runtime

router = APIRouter()

CHINA_CHANNEL_SPECS: tuple[dict[str, Any], ...] = (
    {
        'id': 'qqbot',
        'config_key': 'qqbot',
        'attr': 'qqbot',
        'label': 'QQ Bot',
        'description': '腾讯 QQ 官方机器人通道',
        'requirements': (
            ('appId', ('appId', 'app_id')),
            ('clientSecret / token', ('clientSecret', 'client_secret', 'token')),
        ),
    },
    {
        'id': 'dingtalk',
        'config_key': 'dingtalk',
        'attr': 'dingtalk',
        'label': '钉钉',
        'description': '钉钉机器人与流式接入通道',
        'requirements': (
            ('clientId', ('clientId', 'client_id')),
            ('clientSecret / token', ('clientSecret', 'client_secret', 'token')),
        ),
    },
    {
        'id': 'wecom',
        'config_key': 'wecom',
        'attr': 'wecom',
        'label': '企业微信机器人',
        'description': '企业微信智能机器人通道',
        'requirements': (
            ('botId / receiveId', ('botId', 'bot_id', 'receiveId', 'receive_id')),
            ('secret / token', ('secret', 'token', 'encodingAesKey', 'encoding_aes_key')),
        ),
    },
    {
        'id': 'wecomApp',
        'config_key': 'wecomApp',
        'attr': 'wecom_app',
        'label': '企业微信应用',
        'description': '企业微信自建应用通道',
        'requirements': (
            ('corpId', ('corpId', 'corp_id')),
            ('corpSecret', ('corpSecret', 'corp_secret')),
            ('agentId', ('agentId', 'agent_id')),
        ),
    },
    {
        'id': 'feishuChina',
        'config_key': 'feishuChina',
        'attr': 'feishu_china',
        'label': '飞书',
        'description': '飞书 / Lark 中国版通道',
        'requirements': (
            ('appId', ('appId', 'app_id')),
            ('appSecret / token', ('appSecret', 'app_secret', 'token')),
        ),
    },
)

CHINA_CHANNEL_INDEX: dict[str, dict[str, Any]] = {
    item['id']: item
    for item in CHINA_CHANNEL_SPECS
}
CHINA_CHANNEL_ALIASES = {
    'qqbot': 'qqbot',
    'dingtalk': 'dingtalk',
    'wecom': 'wecom',
    'wecomapp': 'wecomApp',
    'wecom_app': 'wecomApp',
    'wecom-app': 'wecomApp',
    'feishuchina': 'feishuChina',
    'feishu_china': 'feishuChina',
    'feishu-china': 'feishuChina',
}



def _service():
    agent = get_agent()
    service = getattr(agent, 'main_task_service', None)
    if service is None:
        raise HTTPException(status_code=503, detail='main_task_service_unavailable')
    return service


def _resource_delete_http_error(exc: ValueError) -> HTTPException:
    payload = getattr(exc, 'payload', None)
    if isinstance(payload, dict):
        code = str(payload.get('code') or '').strip()
        if code in {'skill_not_found', 'tool_not_found'}:
            status_code = 404
        elif code in {'skill_busy', 'tool_busy', 'skill_in_use', 'tool_in_use'}:
            status_code = 409
        else:
            status_code = 400
        return HTTPException(status_code=status_code, detail=payload)
    detail = str(exc)
    status_code = 404 if detail in {'skill_not_found', 'tool_not_found'} else 409 if detail in {'skill_busy', 'tool_busy'} else 400
    return HTTPException(status_code=status_code, detail=detail)


async def _refresh_runtime(reason: str) -> None:
    try:
        await refresh_web_agent_runtime(force=True, reason=reason)
    except Exception:
        return



def _model_roles(manager: ModelManager) -> dict[str, list[str]]:
    return {scope: list(getattr(manager.config.models.roles, scope)) for scope in VALID_SCOPES}


def _model_role_iterations(manager: ModelManager) -> dict[str, int]:
    return {scope: manager.config.get_role_max_iterations(scope) for scope in VALID_SCOPES}


def _main_runtime_settings_payload(cfg: Config) -> dict[str, Any]:
    default_max_depth = max(0, int(getattr(cfg.main_runtime, 'default_max_depth', 1) or 0))
    hard_max_depth = max(default_max_depth, int(getattr(cfg.main_runtime, 'hard_max_depth', default_max_depth) or default_max_depth))
    return {
        'task_defaults': {'max_depth': default_max_depth},
        'main_runtime': {
            'default_max_depth': default_max_depth,
            'hard_max_depth': hard_max_depth,
        },
    }


def _normalized_main_runtime_default_depth(cfg: Config, payload: dict[str, Any] | None) -> int:
    source = payload if isinstance(payload, dict) else {}
    raw_depth = source.get('max_depth', source.get('maxDepth', getattr(cfg.main_runtime, 'default_max_depth', 1)))
    try:
        requested = int(raw_depth)
    except (TypeError, ValueError):
        requested = int(getattr(cfg.main_runtime, 'default_max_depth', 1) or 1)
    hard_max_depth = max(0, int(getattr(cfg.main_runtime, 'hard_max_depth', requested) or requested))
    return max(0, min(requested, hard_max_depth))


def _normalize_china_channel_id(channel_id: str) -> str:
    raw = str(channel_id or '').strip()
    key = raw.replace('-', '_').lower()
    normalized = CHINA_CHANNEL_ALIASES.get(key)
    if normalized is None:
        raise HTTPException(status_code=404, detail='china_channel_not_found')
    return normalized


def _china_channel_spec(channel_id: str) -> dict[str, Any]:
    normalized = _normalize_china_channel_id(channel_id)
    return CHINA_CHANNEL_INDEX[normalized]


def _china_bridge_status_payload() -> dict[str, Any] | None:
    path = _china_bridge_status_path()
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def _china_bridge_runtime_summary(cfg: Config) -> dict[str, Any]:
    status = _china_bridge_status_payload() or {}
    dist_entry = cfg.workspace_path / 'subsystems' / 'china_channels_host' / 'dist' / 'index.js'
    node_path = shutil.which(cfg.china_bridge.node_bin)
    return {
        'enabled': bool(cfg.china_bridge.enabled),
        'public_port': int(cfg.china_bridge.public_port),
        'control_port': int(cfg.china_bridge.control_port),
        'node_bin': str(cfg.china_bridge.node_bin or 'node'),
        'node_found': bool(node_path),
        'node_path': node_path,
        'dist_entry': str(dist_entry),
        'dist_exists': dist_entry.exists(),
        'running': bool(status.get('running')),
        'connected': bool(status.get('connected')),
        'status_path': str(_china_bridge_status_path()),
        'status_exists': bool(status),
        'last_error': str(status.get('last_error') or '').strip() or None,
    }


def _channel_has_value(payload: dict[str, Any], candidates: tuple[str, ...]) -> bool:
    for key in candidates:
        value = payload.get(key)
        if value not in (None, '', [], {}, False):
            return True
    accounts = payload.get('accounts')
    if isinstance(accounts, dict):
        for account_payload in accounts.values():
            if not isinstance(account_payload, dict):
                continue
            for key in candidates:
                value = account_payload.get(key)
                if value not in (None, '', [], {}, False):
                    return True
    return False


def _channel_missing_requirements(channel_id: str, payload: dict[str, Any]) -> list[str]:
    spec = _china_channel_spec(channel_id)
    missing: list[str] = []
    for label, candidates in spec.get('requirements') or ():
        if not _channel_has_value(payload, tuple(str(item) for item in candidates)):
            missing.append(str(label))
    return missing


def _serialize_china_channel(cfg: Config, channel_id: str) -> dict[str, Any]:
    spec = _china_channel_spec(channel_id)
    channel_model = getattr(cfg.china_bridge.channels, spec['attr'])
    payload = channel_model.model_dump(by_alias=True, exclude_none=True)
    enabled = bool(payload.pop('enabled', False))
    runtime = _china_bridge_runtime_summary(cfg)
    accounts = payload.get('accounts')
    account_count = len(accounts) if isinstance(accounts, dict) else 0
    return {
        'id': spec['id'],
        'label': spec['label'],
        'description': spec['description'],
        'config_path': f"chinaBridge.channels.{spec['config_key']}",
        'enabled': enabled,
        'account_count': account_count,
        'config': payload,
        'json_text': json.dumps(payload, ensure_ascii=False, indent=2),
        'runtime': runtime,
    }


def _test_china_channel(cfg: Config, channel_id: str) -> dict[str, Any]:
    item = _serialize_china_channel(cfg, channel_id)
    runtime = item['runtime']
    if not item['enabled']:
        return {
            'status': 'disabled',
            'title': '当前通信已禁用',
            'message': '配置已保存，当前渠道保持禁用状态。',
            'details': [],
        }
    missing = _channel_missing_requirements(channel_id, item['config'])
    if missing:
        return {
            'status': 'error',
            'title': '测试失败',
            'message': f"配置缺少必要字段：{', '.join(missing)}",
            'details': missing,
        }
    if runtime['running'] and runtime['connected']:
        return {
            'status': 'success',
            'title': '连接成功',
            'message': '桥接宿主正在运行，内部控制连接已建立。',
            'details': [],
        }
    warnings: list[str] = []
    if not runtime['node_found']:
        warnings.append('未找到 Node 可执行文件')
    if not runtime['dist_exists']:
        warnings.append('中国通信子系统尚未构建')
    if not runtime['running']:
        warnings.append('桥接宿主当前未运行')
    if warnings:
        return {
            'status': 'warning',
            'title': '测试通过',
            'message': '配置校验已通过，但本地桥接环境尚未完全就绪。',
            'details': warnings,
        }
    return {
        'status': 'success',
        'title': '测试通过',
        'message': '配置校验已通过，等待宿主完成平台侧连接。',
        'details': [],
    }


def _update_china_channel_config(cfg: Config, channel_id: str, *, enabled: bool, payload: dict[str, Any]) -> Config:
    spec = _china_channel_spec(channel_id)
    config_payload = dict(payload or {})
    config_payload.pop('enabled', None)

    full_payload = cfg.model_dump(by_alias=True, exclude_none=True)
    bridge_payload = full_payload.setdefault('chinaBridge', {})
    channels_payload = bridge_payload.setdefault('channels', {})
    channels_payload[spec['config_key']] = {
        **config_payload,
        'enabled': bool(enabled),
    }
    bridge_payload['enabled'] = any(
        bool((channels_payload.get(item['config_key']) or {}).get('enabled'))
        for item in CHINA_CHANNEL_SPECS
    )
    next_cfg = Config.model_validate(full_payload)
    save_config(next_cfg)
    return next_cfg


@router.get('/main-runtime/settings')
async def get_main_runtime_settings():
    cfg = load_config()
    return {'ok': True, **_main_runtime_settings_payload(cfg)}


@router.put('/main-runtime/settings')
async def update_main_runtime_settings(payload: dict | None = Body(default=None)):
    cfg = load_config()
    next_depth = _normalized_main_runtime_default_depth(cfg, payload)
    if int(getattr(cfg.main_runtime, 'default_max_depth', 1) or 0) != next_depth:
        cfg.main_runtime.default_max_depth = next_depth
        save_config(cfg)
        await _refresh_runtime('admin_main_runtime_update')
    return {'ok': True, **_main_runtime_settings_payload(cfg)}


@router.get('/models')
async def list_models():
    manager = ModelManager.load()
    return {
        'ok': True,
        'items': manager.list_models(),
        'roles': _model_roles(manager),
        'role_iterations': _model_role_iterations(manager),
    }


@router.post('/models')
async def create_model(payload: dict = Body(...)):
    manager = ModelManager.load()
    raw_retry_count = payload.get('retry_count')
    if raw_retry_count is None and 'retryCount' in payload:
        raw_retry_count = payload.get('retryCount')
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
            retry_count=raw_retry_count,
            description=str(payload.get('description') or ''),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await _refresh_runtime('admin_model_create')
    return {'ok': True, 'item': item}


@router.put('/models/{model_key}')
async def update_model(model_key: str, payload: dict = Body(...)):
    manager = ModelManager.load()
    raw_retry_count = payload.get('retry_count')
    if raw_retry_count is None and 'retryCount' in payload:
        raw_retry_count = payload.get('retryCount')
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
            retry_count=raw_retry_count,
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
    raw_model_keys = payload.get('model_keys')
    if raw_model_keys is None and 'modelKeys' in payload:
        raw_model_keys = payload.get('modelKeys')
    raw_max_iterations = payload.get('max_iterations')
    if raw_max_iterations is None and 'maxIterations' in payload:
        raw_max_iterations = payload.get('maxIterations')
    try:
        roles = manager.update_scope_route(
            scope,
            model_keys=[str(item) for item in raw_model_keys] if raw_model_keys is not None else None,
            max_iterations=raw_max_iterations,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await _refresh_runtime('admin_model_roles')
    return {
        'ok': True,
        'scope': scope,
        'roles': roles,
        'all_roles': _model_roles(manager),
        'role_iterations': _model_role_iterations(manager),
    }


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
    return {
        'ok': True,
        'items': manager.list_models(),
        'routes': manager.facade.get_routes(manager.config),
        'role_iterations': _model_role_iterations(manager),
    }


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
    return {
        'ok': True,
        'routes': manager.facade.get_routes(manager.config),
        'role_iterations': _model_role_iterations(manager),
    }


@router.put('/llm/routes/{scope}')
async def update_llm_route(scope: str, payload: dict = Body(...)):
    manager = ModelManager.load()
    raw_model_keys = payload.get('model_keys')
    if raw_model_keys is None and 'modelKeys' in payload:
        raw_model_keys = payload.get('modelKeys')
    raw_max_iterations = payload.get('max_iterations')
    if raw_max_iterations is None and 'maxIterations' in payload:
        raw_max_iterations = payload.get('maxIterations')
    try:
        route = manager.update_scope_route(
            scope,
            model_keys=[str(item) for item in raw_model_keys] if raw_model_keys is not None else None,
            max_iterations=raw_max_iterations,
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await _refresh_runtime('admin_llm_route_update')
    return {
        'ok': True,
        'route': route,
        'routes': manager.facade.get_routes(manager.config),
        'role_iterations': _model_role_iterations(manager),
    }


@router.get('/llm/memory')
async def get_llm_memory_binding():
    manager = ModelManager.load()
    try:
        result = manager.facade.get_memory_binding()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {'ok': True, 'item': result.model_dump(mode='json')}


@router.put('/llm/memory')
async def update_llm_memory_binding(payload: dict | None = Body(default=None)):
    manager = ModelManager.load()
    try:
        result = manager.facade.get_memory_binding()
        if not result.embedding_config_id or result.embedding_config_id != MEMORY_EMBEDDING_CONFIG_ID:
            raise ValueError('memory embedding config is missing')
        if not result.rerank_config_id or result.rerank_config_id != MEMORY_RERANK_CONFIG_ID:
            raise ValueError('memory rerank config is missing')
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


@router.delete('/resources/skills/{skill_id}')
async def delete_skill(skill_id: str, session_id: str = Query('web:shared')):
    service = _service()
    await service.startup()
    try:
        item = await service.delete_skill_resource_async(skill_id, session_id=session_id)
    except ValueError as exc:
        raise _resource_delete_http_error(exc) from exc
    return {'ok': True, 'item': item}


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


@router.get('/china-bridge/channels')
async def list_china_bridge_channels():
    cfg = load_config()
    return {
        'ok': True,
        'bridge': _china_bridge_runtime_summary(cfg),
        'items': [_serialize_china_channel(cfg, item['id']) for item in CHINA_CHANNEL_SPECS],
    }


@router.get('/china-bridge/channels/{channel_id}')
async def get_china_bridge_channel(channel_id: str):
    cfg = load_config()
    return {'ok': True, 'item': _serialize_china_channel(cfg, channel_id)}


@router.put('/china-bridge/channels/{channel_id}')
async def update_china_bridge_channel(channel_id: str, payload: dict = Body(...)):
    config_payload = payload.get('config') if isinstance(payload.get('config'), dict) else None
    if config_payload is None:
        raise HTTPException(status_code=400, detail='config must be a JSON object')
    cfg = load_config()
    next_cfg = _update_china_channel_config(
        cfg,
        channel_id,
        enabled=bool(payload.get('enabled')),
        payload=config_payload,
    )
    await _refresh_runtime('admin_china_bridge_channel_update')
    return {'ok': True, 'item': _serialize_china_channel(next_cfg, channel_id)}


@router.post('/china-bridge/channels/{channel_id}/test')
async def test_china_bridge_channel(channel_id: str):
    cfg = load_config()
    return {
        'ok': True,
        'item': _serialize_china_channel(cfg, channel_id),
        'result': _test_china_channel(cfg, channel_id),
    }


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
            'qqbot': cfg.china_bridge.channels.qqbot.enabled,
            'dingtalk': cfg.china_bridge.channels.dingtalk.enabled,
            'wecom': cfg.china_bridge.channels.wecom.enabled,
            'wecom_app': cfg.china_bridge.channels.wecom_app.enabled,
            'feishu_china': cfg.china_bridge.channels.feishu_china.enabled,
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


@router.delete('/resources/tools/{tool_id}')
async def delete_tool(tool_id: str, session_id: str = Query('web:shared')):
    service = _service()
    await service.startup()
    try:
        item = await service.delete_tool_resource_async(tool_id, session_id=session_id)
    except ValueError as exc:
        raise _resource_delete_http_error(exc) from exc
    return {'ok': True, 'item': item}


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
