from __future__ import annotations

import errno
import json
import os
import re
import shutil
from contextlib import contextmanager
from inspect import isawaitable
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, Body, HTTPException, Query, Request

from g3ku.china_bridge.registry import (
    china_channel_aliases,
    china_channel_attr,
    china_channel_ids,
    china_channel_maintenance_status,
    china_channel_spec,
    china_channel_template,
    list_china_channel_specs,
)
from g3ku.china_bridge.registry import (
    normalize_china_channel_id as normalize_registry_channel_id,
)
from g3ku.config.loader import load_config, save_config
from g3ku.config.model_manager import _UNSET, VALID_SCOPES, ModelManager
from g3ku.config.schema import Config
from g3ku.resources import get_shared_resource_manager
from g3ku.resources.models import ResourceKind
from g3ku.runtime.core_tools import configured_core_tools, resolve_core_tool_targets
from g3ku.runtime.frontdoor.checkpoint_inspection import (
    build_frontdoor_replay_diagnostics,
    get_frontdoor_checkpoint,
    get_frontdoor_checkpoint_history,
)
from g3ku.shells.web import get_agent, is_no_ceo_model_configured_error, refresh_web_agent_runtime
from main.governance import (
    GovernanceStore,
    MainRuntimePolicyEngine,
    MainRuntimeResourceRegistry,
    PermissionSubject,
    list_effective_skill_ids,
    list_effective_tool_names,
)
from main.governance.exec_tool_policy import (
    exec_tool_supports_execution_mode,
    merge_exec_execution_mode_metadata,
)
from main.governance.roles import normalize_public_allowed_roles
from main.governance.tool_context import build_tool_toolskill_payload, resolve_primary_executor_name
from main.protocol import now_iso
from main.storage.sqlite_store import SQLiteTaskStore

router = APIRouter()

CHINA_CHANNEL_SPECS: tuple[dict[str, Any], ...] = tuple(list_china_channel_specs())
CHINA_CHANNEL_INDEX: dict[str, dict[str, Any]] = {item['id']: item for item in CHINA_CHANNEL_SPECS}
CHINA_CHANNEL_ALIASES = china_channel_aliases()

CHINA_PROBE_TIMEOUT = httpx.Timeout(10.0, connect=5.0)
QQBOT_ACCESS_TOKEN_URL = 'https://bots.qq.com/app/getAppAccessToken'
QQBOT_GATEWAY_URL = 'https://api.sgroup.qq.com/gateway'
DINGTALK_ACCESS_TOKEN_URL = 'https://api.dingtalk.com/v1.0/oauth2/accessToken'
WECOM_ACCESS_TOKEN_URL = 'https://qyapi.weixin.qq.com/cgi-bin/gettoken'
FEISHU_APP_ACCESS_TOKEN_URL = 'https://open.feishu.cn/open-apis/auth/v3/app_access_token/internal'
MEMORY_NOTE_REF_RE = re.compile(r"^note_[a-z0-9_]+$")



def _service():
    agent = get_agent()
    service = getattr(agent, 'main_task_service', None)
    if service is None:
        raise HTTPException(status_code=503, detail='main_task_service_unavailable')
    return service


def _llm_facade():
    return ModelManager.load_facade()


def _llm_binding_create_error_detail(exc: Exception) -> dict[str, Any] | str:
    message = str(exc or '').strip()
    prefix = 'Model key already exists:'
    if message.startswith(prefix):
        duplicate_key = message[len(prefix):].strip()
        return {
            'code': 'llm_binding_key_exists',
            'message': '配置名已存在，请使用其他配置名。',
            'data': {'key': duplicate_key},
        }
    return message


def _checkpoint_agent():
    try:
        return get_agent()
    except HTTPException:
        raise
    except Exception as exc:
        if is_no_ceo_model_configured_error(exc):
            raise HTTPException(status_code=503, detail='no_model_configured') from exc
        if str(exc or '').strip() == 'project is locked':
            raise HTTPException(status_code=423, detail='project_locked') from exc
        raise


@router.get("/ceo/checkpoints/{session_id}")
async def get_ceo_checkpoint(
    session_id: str,
    checkpoint_id: str | None = Query(None),
):
    agent = _checkpoint_agent()
    item = get_frontdoor_checkpoint(agent, session_id=session_id, checkpoint_id=checkpoint_id)
    if isawaitable(item):
        item = await item
    if item is None:
        raise HTTPException(status_code=404, detail="checkpoint_not_found")
    return {"ok": True, "item": item}


@router.get("/ceo/checkpoints/{session_id}/history")
async def get_ceo_checkpoint_history(
    session_id: str,
    limit: int = Query(20, ge=1, le=100),
    before_checkpoint_id: str | None = Query(None),
):
    agent = _checkpoint_agent()
    items = get_frontdoor_checkpoint_history(
        agent,
        session_id=session_id,
        limit=limit,
        before_checkpoint_id=before_checkpoint_id,
    )
    if isawaitable(items):
        items = await items
    return {"ok": True, "items": items}


@router.get("/ceo/checkpoints/{session_id}/replay-diagnostics")
async def get_ceo_replay_diagnostics(
    session_id: str,
    checkpoint_id: str = Query(...),
):
    agent = _checkpoint_agent()
    snapshot = get_frontdoor_checkpoint(agent, session_id=session_id, checkpoint_id=checkpoint_id)
    if isawaitable(snapshot):
        snapshot = await snapshot
    if snapshot is None:
        raise HTTPException(status_code=404, detail="checkpoint_not_found")
    return {"ok": True, "item": build_frontdoor_replay_diagnostics(snapshot)}


def _resolve_workspace_relative_path(workspace: Path, raw_path: str | Path | None, *, fallback: str) -> Path:
    candidate = Path(str(raw_path or fallback))
    if not candidate.is_absolute():
        candidate = Path(workspace) / candidate
    return candidate.resolve(strict=False)


class _ResourceDeleteBlockedError(ValueError):
    def __init__(
        self,
        *,
        code: str,
        message: str,
        resource_kind: str,
        resource_id: str,
        usage: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(code)
        self.payload = {
            'code': str(code or '').strip(),
            'message': str(message or '').strip(),
            'resource_kind': str(resource_kind or '').strip(),
            'resource_id': str(resource_id or '').strip(),
            'usage': dict(usage or {}),
        }


class _ResourceMutationBlockedError(ValueError):
    def __init__(
        self,
        *,
        code: str,
        message: str,
        resource_kind: str,
        resource_id: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(code)
        self.payload = {
            'code': str(code or '').strip(),
            'message': str(message or '').strip(),
            'resource_kind': str(resource_kind or '').strip(),
            'resource_id': str(resource_id or '').strip(),
            'details': dict(details or {}),
        }


class _StandaloneResourceService:
    def __init__(self, cfg: Config):
        self._cfg = cfg
        self._workspace = Path(cfg.workspace_path).resolve(strict=False)
        self._resource_manager = get_shared_resource_manager(self._workspace, app_config=cfg)
        self._resource_manager.start()
        self._resource_manager.reload_now(trigger='admin_resource_read')
        runtime_store_path = _resolve_workspace_relative_path(
            self._workspace,
            getattr(cfg.main_runtime, 'store_path', None),
            fallback='.g3ku/main-runtime/runtime.sqlite3',
        )
        governance_path = _resolve_workspace_relative_path(
            self._workspace,
            getattr(cfg.main_runtime, 'governance_store_path', None),
            fallback='.g3ku/main-runtime/governance.sqlite3',
        )
        self._task_store = SQLiteTaskStore(runtime_store_path)
        self._governance_store = GovernanceStore(governance_path)
        self.resource_registry = MainRuntimeResourceRegistry(
            workspace_root=self._workspace,
            store=self._governance_store,
            resource_manager=self._resource_manager,
        )
        self.resource_registry.refresh_from_current_resources()
        self.policy_engine = MainRuntimePolicyEngine(
            store=self._governance_store,
            resource_registry=self.resource_registry,
        )
        self.policy_engine.sync_default_role_policies()

    def close(self) -> None:
        self._task_store.close()
        self._governance_store.close()

    def list_skill_resources(self) -> list[Any]:
        return list(self.resource_registry.list_skill_resources())

    def get_skill_resource(self, skill_id: str):
        return self.resource_registry.get_skill_resource(str(skill_id or '').strip())

    def list_skill_files(self, skill_id: str) -> dict[str, str]:
        return {
            file_key: str(path)
            for file_key, path in self.resource_registry.skill_file_map(str(skill_id or '').strip()).items()
        }

    def read_skill_file(self, skill_id: str, file_key: str) -> str:
        path = self.resource_registry.skill_file_map(str(skill_id or '').strip()).get(str(file_key or '').strip())
        if path is None:
            raise ValueError('editable_file_not_allowed')
        return path.read_text(encoding='utf-8')

    def _configured_core_tool_entries(self) -> list[str]:
        return configured_core_tools(resource_manager=self._resource_manager)

    @staticmethod
    def _bool_env(name: str, *, default: bool) -> bool:
        raw = str(os.getenv(name, '') or '').strip().lower()
        if not raw:
            return bool(default)
        return raw not in {'0', 'false', 'no', 'off'}

    @classmethod
    def _web_disable_message_tool_enabled(cls) -> bool:
        if str(os.getenv('G3KU_TASK_RUNTIME_ROLE', '') or '').strip().lower() != 'web':
            return False
        return cls._bool_env('G3KU_WEB_DISABLE_MESSAGE_TOOL', default=True)

    def _core_tool_resolution(self):
        return resolve_core_tool_targets(
            self._configured_core_tool_entries(),
            list(self.resource_registry.list_tool_families()),
        )

    def _raw_tool_family(self, tool_id: str):
        return self.resource_registry.get_tool_family(str(tool_id or '').strip())

    def _decorate_tool_family(self, family):
        if family is None:
            return None
        resolution = self._core_tool_resolution()
        metadata = dict(getattr(family, 'metadata', {}) or {})
        metadata['repair_required'] = bool(getattr(family, 'callable', True)) and not bool(getattr(family, 'available', True))
        normalized_tool_id = str(getattr(family, 'tool_id', '') or '').strip()
        if normalized_tool_id == 'messaging':
            metadata.setdefault('web_default_disabled', bool(self._web_disable_message_tool_enabled()))
            metadata.setdefault('web_default_disabled_env', 'G3KU_WEB_DISABLE_MESSAGE_TOOL')
        return family.model_copy(update={'is_core': family.tool_id in resolution.family_ids, 'metadata': metadata})

    def list_tool_resources(self) -> list[Any]:
        return [self._decorate_tool_family(item) for item in self.resource_registry.list_tool_families()]

    def get_tool_family(self, tool_id: str):
        return self._decorate_tool_family(self._raw_tool_family(tool_id))

    def _tool_family_executor_name(self, family) -> str:
        return resolve_primary_executor_name(family, resource_manager=self._resource_manager)

    def get_tool_toolskill(self, tool_id: str) -> dict[str, Any] | None:
        return build_tool_toolskill_payload(
            tool_id,
            raw_tool_family_getter=self._raw_tool_family,
            resource_registry=self.resource_registry,
            resource_manager=self._resource_manager,
        )

    def _subject(self, *, actor_role: str, session_id: str, task_id: str | None = None, node_id: str | None = None) -> PermissionSubject:
        return PermissionSubject(
            user_key=session_id,
            session_id=session_id,
            task_id=task_id,
            node_id=node_id,
            actor_role=actor_role,
        )

    def list_effective_tool_names(self, *, actor_role: str, session_id: str) -> list[str]:
        supported = sorted(self._resource_manager.tool_instances().keys())
        return list_effective_tool_names(
            subject=self._subject(actor_role=actor_role, session_id=session_id),
            supported_tool_names=supported,
            resource_registry=self.resource_registry,
            policy_engine=self.policy_engine,
            mutation_allowed=True,
        )

    def list_visible_skill_resources(self, *, actor_role: str, session_id: str):
        visible_ids = set(
            list_effective_skill_ids(
                subject=self._subject(actor_role=actor_role, session_id=session_id),
                available_skill_ids=[item.skill_id for item in self.resource_registry.list_skill_resources()],
                policy_engine=self.policy_engine,
            )
        )
        return [item for item in self.resource_registry.list_skill_resources() if item.skill_id in visible_ids]

    def list_visible_tool_families(self, *, actor_role: str, session_id: str):
        visible_names = set(self.list_effective_tool_names(actor_role=actor_role, session_id=session_id))
        subject = self._subject(actor_role=actor_role, session_id=session_id)
        families = []
        for family in self.resource_registry.list_tool_families():
            actions = []
            for action in family.actions:
                decision = self.policy_engine.evaluate_tool_action(
                    subject=subject,
                    tool_id=family.tool_id,
                    action_id=action.action_id,
                )
                executor_visible = bool(set(action.executor_names) & visible_names)
                if decision.allowed and (not bool(getattr(family, 'callable', True)) or executor_visible):
                    actions.append(action)
            if actions:
                families.append(family.model_copy(update={'actions': actions}))
        return families

    def capture_resource_tree_state(self) -> dict[str, dict[str, str]]:
        return self._resource_manager.capture_resource_tree_state()

    def refresh_resource_paths(
        self,
        paths: list[str | Path],
        *,
        trigger: str = 'path-change',
        session_id: str = 'web:shared',
    ) -> dict[str, Any]:
        self._resource_manager.refresh_paths(list(paths or []), trigger=trigger)
        skills, tools = self.resource_registry.refresh_from_current_resources()
        self.policy_engine.sync_default_role_policies()
        return {'ok': True, 'session_id': session_id, 'skills': len(skills), 'tools': len(tools)}

    def refresh_changed_resources(
        self,
        before_state: dict[str, dict[str, str]] | None,
        *,
        trigger: str = 'path-change',
        session_id: str = 'web:shared',
    ) -> dict[str, Any]:
        self._resource_manager.refresh_changed_tree_state(before_state, trigger=trigger)
        skills, tools = self.resource_registry.refresh_from_current_resources()
        self.policy_engine.sync_default_role_policies()
        return {'ok': True, 'session_id': session_id, 'skills': len(skills), 'tools': len(tools)}

    def write_skill_file(self, skill_id: str, file_key: str, content: str, *, session_id: str = 'web:shared') -> dict[str, Any]:
        path = self.resource_registry.skill_file_map(str(skill_id or '').strip()).get(str(file_key or '').strip())
        if path is None:
            raise ValueError('editable_file_not_allowed')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(content or ''), encoding='utf-8')
        self.refresh_resource_paths([path], trigger='skill-file-write', session_id=session_id)
        return {'skill_id': str(skill_id or '').strip(), 'file_key': str(file_key or '').strip(), 'path': str(path)}

    async def write_skill_file_async(
        self,
        skill_id: str,
        file_key: str,
        content: str,
        *,
        session_id: str = 'web:shared',
    ) -> dict[str, Any]:
        item = self.write_skill_file(skill_id, file_key, content, session_id=session_id)
        item['catalog_synced'] = False
        return item

    def _workspace_root(self) -> Path:
        return self._workspace

    def _resource_base_dir(self, kind: ResourceKind) -> Path:
        registry = getattr(self._resource_manager, '_registry', None)
        if kind is ResourceKind.SKILL:
            candidate = getattr(registry, 'skills_dir', None)
            fallback = self._workspace_root() / 'skills'
        else:
            candidate = getattr(registry, 'tools_dir', None)
            fallback = self._workspace_root() / 'tools'
        return Path(candidate or fallback).resolve(strict=False)

    @staticmethod
    def _is_relative_to(path: Path, base: Path) -> bool:
        try:
            path.relative_to(base)
        except ValueError:
            return False
        return True

    def _resolve_workspace_path(self, raw_path: str | Path | None) -> Path:
        path = Path(raw_path or '').expanduser()
        if not path.is_absolute():
            path = self._workspace_root() / path
        return path.resolve(strict=False)

    def _resolve_resource_root(self, raw_path: str | Path | None, *, kind: ResourceKind) -> Path:
        resolved = self._resolve_workspace_path(raw_path)
        base_dir = self._resource_base_dir(kind)
        if not self._is_relative_to(resolved, base_dir):
            raise ValueError(f'{kind.value}_path_outside_workspace')
        if resolved == base_dir:
            raise ValueError(f'{kind.value}_path_invalid')
        return resolved

    def _resource_is_busy(self, kind: ResourceKind, *names: str) -> bool:
        for raw_name in names:
            name = str(raw_name or '').strip()
            if not name:
                continue
            try:
                state = self._resource_manager.busy_state(kind, name)
            except Exception:
                continue
            if bool(getattr(state, 'busy', False)):
                return True
        return False

    @staticmethod
    def _display_role_label(role: str) -> str:
        return {
            'ceo': '主Agent',
            'execution': '执行',
            'inspection': '检验',
        }.get(str(role or '').strip().lower(), str(role or '').strip())

    def _running_task_records(self) -> list[Any]:
        try:
            tasks = self._task_store.list_tasks()
        except Exception:
            return []
        return [
            task
            for task in tasks
            if str(getattr(task, 'status', '') or '').strip().lower() == 'in_progress' and not bool(getattr(task, 'is_paused', False))
        ]

    def _running_ceo_session_ids(self) -> list[str]:
        return []

    def _skill_visible_roles_for_task(self, task: Any, skill_id: str) -> list[str]:
        roles: list[str] = []
        session_id = str(getattr(task, 'session_id', '') or 'web:shared').strip() or 'web:shared'
        for actor_role in ('execution', 'inspection'):
            visible_ids = {
                str(getattr(item, 'skill_id', '') or '').strip()
                for item in self.list_visible_skill_resources(actor_role=actor_role, session_id=session_id)
            }
            if skill_id in visible_ids:
                roles.append(actor_role)
        return roles

    def _tool_visible_roles_for_task(self, task: Any, tool_id: str) -> list[str]:
        roles: list[str] = []
        session_id = str(getattr(task, 'session_id', '') or 'web:shared').strip() or 'web:shared'
        for actor_role in ('execution', 'inspection'):
            visible_ids = {
                str(getattr(item, 'tool_id', '') or '').strip()
                for item in self.list_visible_tool_families(actor_role=actor_role, session_id=session_id)
            }
            if tool_id in visible_ids:
                roles.append(actor_role)
        return roles

    @classmethod
    def _format_usage_message(
        cls,
        *,
        resource_label: str,
        display_name: str,
        usage: dict[str, list[dict[str, Any]]],
    ) -> str:
        tasks = list(usage.get('tasks') or [])
        blockers: list[str] = []
        if tasks:
            blockers.append(f'{len(tasks)} 个进行中的任务')
        message = f'无法删除{resource_label}“{display_name}”，当前有{"、".join(blockers)}正在使用。'
        previews: list[str] = []
        if tasks:
            task_text = '；'.join(
                (
                    f"{str(item.get('title') or item.get('task_id') or '未命名任务').strip()} ({str(item.get('task_id') or '').strip()})"
                    + (
                        f" / {'、'.join(cls._display_role_label(role) for role in list(item.get('actor_roles') or []))}"
                        if list(item.get('actor_roles') or [])
                        else ''
                    )
                )
                for item in tasks[:3]
            )
            if len(tasks) > 3:
                task_text += f'；等 {len(tasks)} 个'
            previews.append(f'任务：{task_text}')
        return f"{message} {' '.join(previews)}".strip()

    def _skill_usage_summary(self, skill_id: str) -> dict[str, list[dict[str, Any]]]:
        usage: dict[str, list[dict[str, Any]]] = {'tasks': [], 'ceo_sessions': []}
        for task in self._running_task_records():
            actor_roles = self._skill_visible_roles_for_task(task, skill_id)
            if not actor_roles:
                continue
            usage['tasks'].append(
                {
                    'task_id': str(getattr(task, 'task_id', '') or '').strip(),
                    'title': str(getattr(task, 'title', '') or '').strip(),
                    'session_id': str(getattr(task, 'session_id', '') or '').strip(),
                    'actor_roles': actor_roles,
                }
            )
        return usage

    def _tool_usage_summary(self, tool_id: str) -> dict[str, list[dict[str, Any]]]:
        usage: dict[str, list[dict[str, Any]]] = {'tasks': [], 'ceo_sessions': []}
        for task in self._running_task_records():
            actor_roles = self._tool_visible_roles_for_task(task, tool_id)
            if not actor_roles:
                continue
            usage['tasks'].append(
                {
                    'task_id': str(getattr(task, 'task_id', '') or '').strip(),
                    'title': str(getattr(task, 'title', '') or '').strip(),
                    'session_id': str(getattr(task, 'session_id', '') or '').strip(),
                    'actor_roles': actor_roles,
                }
            )
        return usage

    def _raise_if_skill_in_use(self, skill) -> None:
        target_skill_id = str(getattr(skill, 'skill_id', '') or '').strip()
        display_name = str(getattr(skill, 'display_name', '') or target_skill_id).strip() or target_skill_id
        usage = self._skill_usage_summary(target_skill_id)
        if not usage['tasks']:
            return
        raise _ResourceDeleteBlockedError(
            code='skill_in_use',
            message=self._format_usage_message(resource_label='Skill', display_name=display_name, usage=usage),
            resource_kind='skill',
            resource_id=target_skill_id,
            usage=usage,
        )

    def _raise_if_tool_in_use(self, family) -> None:
        target_tool_id = str(getattr(family, 'tool_id', '') or '').strip()
        display_name = str(getattr(family, 'display_name', '') or target_tool_id).strip() or target_tool_id
        usage = self._tool_usage_summary(target_tool_id)
        if not usage['tasks']:
            return
        raise _ResourceDeleteBlockedError(
            code='tool_in_use',
            message=self._format_usage_message(resource_label='工具', display_name=display_name, usage=usage),
            resource_kind='tool',
            resource_id=target_tool_id,
            usage=usage,
        )

    def _delete_path(self, path: Path, *, deleted_paths: list[str]) -> None:
        if not path.exists():
            return
        try:
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
        except FileNotFoundError:
            return
        except Exception as exc:
            raise ValueError(f'resource_delete_failed:{path}:{exc}') from exc
        deleted_paths.append(str(path))

    def _collect_workspace_delete_path(
        self,
        raw_path: str | Path | None,
        *,
        delete_paths: set[Path],
        skipped_paths: list[str],
    ) -> None:
        text = str(raw_path or '').strip()
        if not text:
            return
        resolved = self._resolve_workspace_path(text)
        workspace_root = self._workspace_root()
        if not self._is_relative_to(resolved, workspace_root):
            skipped_paths.append(str(resolved))
            return
        if resolved == workspace_root:
            skipped_paths.append(str(resolved))
            return
        delete_paths.add(resolved)

    def delete_skill_resource(self, skill_id: str, *, session_id: str = 'web:shared') -> dict[str, Any]:
        skill = self.get_skill_resource(skill_id)
        if skill is None:
            raise ValueError('skill_not_found')
        target_skill_id = str(skill.skill_id or '').strip()
        self._raise_if_skill_in_use(skill)
        if self._resource_is_busy(ResourceKind.SKILL, target_skill_id):
            raise ValueError('skill_busy')
        before_state = self.capture_resource_tree_state()
        skill_root = self._resolve_resource_root(skill.source_path, kind=ResourceKind.SKILL)
        deleted_paths: list[str] = []
        self._delete_path(skill_root, deleted_paths=deleted_paths)
        refresh_result = self.refresh_changed_resources(
            before_state,
            trigger='skill-delete',
            session_id=session_id,
        )
        self._governance_store.delete_role_policies_for_resource(
            resource_kind='skill',
            resource_id=target_skill_id,
        )
        return {
            'skill_id': target_skill_id,
            'path': str(skill_root),
            'deleted_paths': deleted_paths,
            'resources': refresh_result,
        }

    async def delete_skill_resource_async(self, skill_id: str, *, session_id: str = 'web:shared') -> dict[str, Any]:
        item = self.delete_skill_resource(skill_id, session_id=session_id)
        item['catalog_synced'] = False
        return item

    def update_skill_policy(self, skill_id: str, *, session_id: str = 'web:shared', enabled: bool | None = None, allowed_roles: list[str] | None = None):
        _ = session_id
        skill = self.get_skill_resource(skill_id)
        if skill is None:
            return None
        updated = skill.model_copy(
            update={
                'enabled': skill.enabled if enabled is None else bool(enabled),
                'allowed_roles': list(skill.allowed_roles if allowed_roles is None else allowed_roles),
            }
        )
        self._governance_store.upsert_skill_resource(updated, updated_at=now_iso())
        self.policy_engine.sync_default_role_policies()
        return updated

    def enable_skill(self, skill_id: str, *, session_id: str = 'web:shared'):
        return self.update_skill_policy(skill_id, session_id=session_id, enabled=True)

    def disable_skill(self, skill_id: str, *, session_id: str = 'web:shared'):
        return self.update_skill_policy(skill_id, session_id=session_id, enabled=False)

    def delete_tool_resource(self, tool_id: str, *, session_id: str = 'web:shared') -> dict[str, Any]:
        family = self._raw_tool_family(tool_id)
        if family is None:
            raise ValueError('tool_not_found')
        target_tool_id = str(family.tool_id or '').strip()
        if target_tool_id in self._core_tool_resolution().family_ids:
            raise _ResourceMutationBlockedError(
                code='core_tool_delete_forbidden',
                message='Core tool families cannot be deleted.',
                resource_kind='tool_family',
                resource_id=target_tool_id,
            )
        self._raise_if_tool_in_use(family)
        descriptor_names: set[str] = {
            str(getattr(family, 'primary_executor_name', '') or '').strip(),
            target_tool_id,
        }
        for action in list(getattr(family, 'actions', []) or []):
            descriptor_names.update(
                str(name or '').strip()
                for name in list(getattr(action, 'executor_names', []) or [])
                if str(name or '').strip()
            )
        descriptor_names.discard('')
        if self._resource_is_busy(ResourceKind.TOOL, *sorted(descriptor_names)):
            raise ValueError('tool_busy')
        before_state = self.capture_resource_tree_state()
        delete_paths: set[Path] = set()
        skipped_paths: list[str] = []
        delete_paths.add(self._resolve_resource_root(family.source_path, kind=ResourceKind.TOOL))
        for descriptor_name in sorted(descriptor_names):
            descriptor = self._resource_manager.get_tool_descriptor(descriptor_name)
            if descriptor is None:
                continue
            delete_paths.add(self._resolve_resource_root(descriptor.root, kind=ResourceKind.TOOL))
            self._collect_workspace_delete_path(
                getattr(descriptor, 'install_dir', None),
                delete_paths=delete_paths,
                skipped_paths=skipped_paths,
            )
        self._collect_workspace_delete_path(
            getattr(family, 'install_dir', None),
            delete_paths=delete_paths,
            skipped_paths=skipped_paths,
        )
        deleted_paths: list[str] = []
        for path in sorted(delete_paths, key=lambda item: (len(str(item)), str(item)), reverse=True):
            self._delete_path(path, deleted_paths=deleted_paths)
        refresh_result = self.refresh_changed_resources(
            before_state,
            trigger='tool-delete',
            session_id=session_id,
        )
        self._governance_store.delete_role_policies_for_resource(
            resource_kind='tool_family',
            resource_id=target_tool_id,
        )
        return {
            'tool_id': target_tool_id,
            'path': str(self._resolve_resource_root(family.source_path, kind=ResourceKind.TOOL)),
            'deleted_paths': deleted_paths,
            'skipped_paths': skipped_paths,
            'resources': refresh_result,
        }

    async def delete_tool_resource_async(self, tool_id: str, *, session_id: str = 'web:shared') -> dict[str, Any]:
        item = self.delete_tool_resource(tool_id, session_id=session_id)
        item['catalog_synced'] = False
        return item

    def update_tool_policy(
        self,
        tool_id: str,
        *,
        session_id: str = 'web:shared',
        enabled: bool | None = None,
        allowed_roles_by_action: dict[str, list[str]] | None = None,
        execution_mode: str | None = None,
    ):
        _ = session_id
        family = self._raw_tool_family(tool_id)
        if family is None:
            return None
        target_tool_id = str(getattr(family, 'tool_id', '') or '').strip()
        is_core = target_tool_id in self._core_tool_resolution().family_ids
        if is_core and enabled is not None and not bool(enabled):
            if not (self._web_disable_message_tool_enabled() and target_tool_id == 'messaging'):
                raise _ResourceMutationBlockedError(
                    code='core_tool_disable_forbidden',
                    message='Core tool families cannot be disabled.',
                    resource_kind='tool_family',
                    resource_id=target_tool_id,
                )
        allowed_roles_by_action = dict(allowed_roles_by_action or {})
        actions = []
        for action in family.actions:
            roles = allowed_roles_by_action.get(action.action_id)
            if str(getattr(action, 'admin_mode', 'editable') or 'editable') == 'readonly_system' and roles is not None:
                normalized_roles = normalize_public_allowed_roles([str(role) for role in list(roles or [])])
                current_roles = normalize_public_allowed_roles(list(getattr(action, 'allowed_roles', []) or []))
                if normalized_roles != current_roles:
                    raise _ResourceMutationBlockedError(
                        code='tool_action_readonly',
                        message='Readonly system actions cannot be edited.',
                        resource_kind='tool_family',
                        resource_id=target_tool_id,
                        details={'action_id': action.action_id},
                    )
            next_roles = (
                list(getattr(action, 'allowed_roles', []) or [])
                if roles is None
                else normalize_public_allowed_roles([str(role) for role in list(roles or [])])
            )
            actions.append(action.model_copy(update={'allowed_roles': next_roles}))
        if execution_mode is not None and not exec_tool_supports_execution_mode(target_tool_id):
            raise _ResourceMutationBlockedError(
                code='tool_execution_mode_unsupported',
                message='execution_mode is only supported for exec_runtime.',
                resource_kind='tool_family',
                resource_id=target_tool_id,
            )
        updated = family.model_copy(
            update={
                'enabled': family.enabled if enabled is None else bool(enabled),
                'actions': actions,
                'metadata': merge_exec_execution_mode_metadata(
                    getattr(family, 'metadata', {}) or {},
                    execution_mode=execution_mode,
                ),
            }
        )
        self._governance_store.upsert_tool_family(updated, updated_at=now_iso())
        self.policy_engine.sync_default_role_policies()
        return self.get_tool_family(target_tool_id)

    def enable_tool(self, tool_id: str, *, session_id: str = 'web:shared'):
        return self.update_tool_policy(tool_id, session_id=session_id, enabled=True)

    def disable_tool(self, tool_id: str, *, session_id: str = 'web:shared'):
        return self.update_tool_policy(tool_id, session_id=session_id, enabled=False)

    def reload_resources(self, *, session_id: str = 'web:shared') -> dict[str, Any]:
        self._resource_manager.reload_now(trigger='manual')
        skills, tools = self.resource_registry.refresh_from_current_resources()
        self.policy_engine.sync_default_role_policies()
        return {'ok': True, 'session_id': session_id, 'skills': len(skills), 'tools': len(tools)}

    async def reload_resources_async(self, *, session_id: str = 'web:shared') -> dict[str, Any]:
        result = self.reload_resources(session_id=session_id)
        result['catalog'] = {'created': 0, 'updated': 0, 'removed': 0}
        return result


@contextmanager
def _resource_service():
    try:
        yield _service()
        return
    except Exception as exc:
        if not is_no_ceo_model_configured_error(exc):
            raise
    service = _StandaloneResourceService(load_config())
    try:
        yield service
    finally:
        service.close()


def _resource_delete_http_error(exc: ValueError) -> HTTPException:
    payload = getattr(exc, 'payload', None)
    if isinstance(payload, dict):
        code = str(payload.get('code') or '').strip()
        if code in {'skill_not_found', 'tool_not_found'}:
            status_code = 404
        elif code in {
            'skill_busy',
            'tool_busy',
            'skill_in_use',
            'tool_in_use',
            'core_tool_disable_forbidden',
            'core_tool_delete_forbidden',
            'core_tool_ceo_visibility_required',
            'tool_action_readonly',
        }:
            status_code = 409
        else:
            status_code = 400
        return HTTPException(status_code=status_code, detail=payload)
    detail = str(exc)
    status_code = 404 if detail in {'skill_not_found', 'tool_not_found'} else 409 if detail in {'skill_busy', 'tool_busy'} else 400
    return HTTPException(status_code=status_code, detail=detail)


async def _refresh_runtime(reason: str) -> None:
    web_refreshed = False
    try:
        await refresh_web_agent_runtime(force=True, reason=reason)
        web_refreshed = True
    except Exception as exc:
        if is_no_ceo_model_configured_error(exc) or str(exc or '').strip() == 'project is locked':
            return
        raise HTTPException(
            status_code=503,
            detail={
                'code': 'web_runtime_refresh_failed',
                'saved': True,
                'web_refreshed': False,
                'worker_refresh_acked': False,
                'reason': reason,
                'error': str(exc or 'web_runtime_refresh_failed').strip() or 'web_runtime_refresh_failed',
            },
        ) from exc
    try:
        service = _service()
    except HTTPException as exc:
        _ = exc
        return
    except Exception:
        return
    if str(getattr(service, 'execution_mode', '') or '').strip().lower() != 'web':
        return
    if not bool(getattr(service, 'is_worker_online', lambda **kwargs: False)()):
        return
    try:
        await service.request_worker_runtime_refresh(reason=reason)
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail={
                'code': 'worker_runtime_refresh_failed',
                'saved': True,
                'web_refreshed': web_refreshed,
                'worker_refresh_acked': False,
                'reason': reason,
                'error': str(exc or 'worker_runtime_refresh_failed').strip() or 'worker_runtime_refresh_failed',
            },
        ) from exc


async def _refresh_runtime_after_save(reason: str) -> dict[str, Any]:
    status: dict[str, Any] = {
        'saved': True,
        'web_refreshed': False,
        'worker_refresh_requested': False,
        'worker_refresh_acked': False,
        'worker_refresh_command_id': '',
        'worker_refresh_status': 'skipped',
        'reason': str(reason or '').strip() or 'runtime_refresh',
    }
    try:
        await refresh_web_agent_runtime(force=True, reason=status['reason'])
        status['web_refreshed'] = True
    except Exception as exc:
        if is_no_ceo_model_configured_error(exc) or str(exc or '').strip() == 'project is locked':
            return status
        status['error'] = str(exc or 'web_runtime_refresh_failed').strip() or 'web_runtime_refresh_failed'
        status['code'] = 'web_runtime_refresh_failed'
        return status
    try:
        service = _service()
    except HTTPException as exc:
        _ = exc
        return status
    except Exception:
        return status
    if str(getattr(service, 'execution_mode', '') or '').strip().lower() != 'web':
        status['worker_refresh_requested'] = True
        status['worker_refresh_acked'] = True
        status['worker_refresh_status'] = 'completed'
        return status
    if not bool(getattr(service, 'is_worker_online', lambda **kwargs: False)()):
        status['worker_refresh_status'] = 'offline'
        return status
    try:
        refresh_status = dict(getattr(service, 'enqueue_worker_runtime_refresh')(reason=status['reason']) or {})
    except Exception as exc:
        status['worker_refresh_status'] = 'failed'
        status['code'] = 'worker_runtime_refresh_enqueue_failed'
        status['error'] = str(exc or 'worker_runtime_refresh_enqueue_failed').strip() or 'worker_runtime_refresh_enqueue_failed'
        return status
    status.update({
        'worker_refresh_requested': bool(refresh_status.get('worker_refresh_requested', True)),
        'worker_refresh_acked': bool(refresh_status.get('worker_refresh_acked', False)),
        'worker_refresh_command_id': str(refresh_status.get('worker_refresh_command_id') or '').strip(),
        'worker_refresh_status': str(refresh_status.get('worker_refresh_status') or ('completed' if refresh_status.get('worker_refresh_acked') else 'pending')).strip() or 'pending',
    })
    if refresh_status.get('error'):
        status['error'] = str(refresh_status.get('error') or '').strip()
    if refresh_status.get('code'):
        status['code'] = str(refresh_status.get('code') or '').strip()
    return status


def _config_summary_probe_status(facade, config_id: str) -> str | None:
    for summary in list(getattr(facade.repository, 'list_summaries', lambda: [])() or []):
        if str(getattr(summary, 'config_id', '') or '').strip() == str(config_id or '').strip():
            return getattr(summary, 'last_probe_status', None)
    return None


def _snapshot_config_record(facade, config_id: str | None) -> dict[str, Any] | None:
    normalized = str(config_id or '').strip()
    if not normalized:
        return None
    record = facade._hydrate_record_secrets(facade.repository.get(normalized))
    return {
        'config_id': normalized,
        'record': record,
        'last_probe_status': _config_summary_probe_status(facade, normalized),
    }


def _restore_config_record_snapshot(facade, snapshot: dict[str, Any] | None) -> None:
    if not snapshot:
        return
    record = snapshot.get('record')
    if record is None:
        return
    facade.repository.save(
        facade._sanitize_record_for_storage(record),
        last_probe_status=snapshot.get('last_probe_status'),
    )
    facade._store_record_secrets(record)


async def _save_memory_embedding_atomically(
    *,
    facade,
    embedding_payload: dict[str, Any],
    rerank_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    current_binding = facade.get_memory_binding()
    original_binding = {
        'embedding_config_id': current_binding.embedding_config_id,
        'rerank_config_id': current_binding.rerank_config_id,
    }
    original_embedding_snapshot = _snapshot_config_record(facade, current_binding.embedding_config_id)
    original_rerank_snapshot = _snapshot_config_record(facade, current_binding.rerank_config_id)

    created_config_ids: list[str] = []
    target_embedding_config_id = str(embedding_payload.get('config_id') or current_binding.embedding_config_id or '').strip() or None
    target_rerank_config_id = (
        str((rerank_payload or {}).get('config_id') or current_binding.rerank_config_id or '').strip() or None
    )

    try:
        embedding_draft = embedding_payload.get('draft') if isinstance(embedding_payload.get('draft'), dict) else None
        if embedding_draft is None:
            raise ValueError('embedding draft is required')
        if target_embedding_config_id:
            embedding_item = facade.update_config_record(target_embedding_config_id, embedding_draft)
        else:
            embedding_item = facade.create_config_record(embedding_draft)
            created_config_ids.append(str(embedding_item.get('config_id') or '').strip())
        next_embedding_config_id = str(embedding_item.get('config_id') or '').strip() or None

        next_rerank_config_id = original_binding['rerank_config_id']
        if rerank_payload is not None:
            rerank_draft = rerank_payload.get('draft') if isinstance(rerank_payload.get('draft'), dict) else None
            if rerank_draft is None:
                raise ValueError('rerank draft is required')
            if target_rerank_config_id:
                rerank_item = facade.update_config_record(target_rerank_config_id, rerank_draft)
            else:
                rerank_item = facade.create_config_record(rerank_draft)
                created_config_ids.append(str(rerank_item.get('config_id') or '').strip())
            next_rerank_config_id = str(rerank_item.get('config_id') or '').strip() or None

        binding = facade.set_memory_binding(
            embedding_config_id=next_embedding_config_id,
            rerank_config_id=next_rerank_config_id,
        )
        await refresh_web_agent_runtime(force=True, reason='admin_llm_memory_embedding_atomic_save')
        manager = _runtime_memory_manager()
        reset_result = await manager.reset_dense_index(reason='embedding_model_changed')
        rebuild_result = await manager.rebuild_dense_index(reason='embedding_model_changed')
        return {
            'binding': binding.model_dump(mode='json'),
            'reset': reset_result,
            'rebuild': rebuild_result,
        }
    except Exception as exc:
        rollback_error: Exception | None = None
        try:
            for config_id in created_config_ids:
                try:
                    facade.delete_config_record(config_id)
                except Exception:
                    pass
            _restore_config_record_snapshot(facade, original_embedding_snapshot)
            _restore_config_record_snapshot(facade, original_rerank_snapshot)
            facade.set_memory_binding(
                embedding_config_id=original_binding['embedding_config_id'],
                rerank_config_id=original_binding['rerank_config_id'],
            )
            await refresh_web_agent_runtime(force=True, reason='admin_llm_memory_embedding_atomic_rollback')
        except Exception as rollback_exc:
            rollback_error = rollback_exc

        status_code = 503 if original_binding['embedding_config_id'] or created_config_ids else 400
        detail = {
            'code': 'memory_embedding_atomic_save_failed',
            'saved': False,
            'rolled_back': rollback_error is None,
            'error': str(exc or 'memory_embedding_atomic_save_failed').strip() or 'memory_embedding_atomic_save_failed',
        }
        if rollback_error is not None:
            detail['rollback_error'] = str(rollback_error or 'rollback_failed').strip() or 'rollback_failed'
        raise HTTPException(status_code=status_code, detail=detail) from exc


def _model_roles(manager: ModelManager) -> dict[str, list[str]]:
    return {scope: list(getattr(manager.config.models.roles, scope)) for scope in VALID_SCOPES}


def _model_role_iterations(manager: ModelManager) -> dict[str, int]:
    return {scope: manager.config.get_role_max_iterations(scope) for scope in VALID_SCOPES}


def _model_role_concurrency(manager: ModelManager) -> dict[str, int | None]:
    return {scope: manager.config.get_role_max_concurrency(scope) for scope in VALID_SCOPES}


def _model_roles_payload(manager: ModelManager) -> dict[str, Any]:
    return {
        'roles': _model_roles(manager),
        'role_iterations': _model_role_iterations(manager),
        'role_concurrency': _model_role_concurrency(manager),
    }


def _llm_routes_payload(manager: ModelManager) -> dict[str, Any]:
    return {
        'routes': manager.facade.get_routes(manager.config),
        'role_iterations': _model_role_iterations(manager),
        'role_concurrency': _model_role_concurrency(manager),
    }


def _scope_route_update_kwargs(payload: dict[str, Any] | None) -> dict[str, Any]:
    body = payload if isinstance(payload, dict) else {}
    raw_model_keys = body.get('model_keys')
    if raw_model_keys is None and 'modelKeys' in body:
        raw_model_keys = body.get('modelKeys')
    raw_max_iterations = body.get('max_iterations')
    if raw_max_iterations is None and 'maxIterations' in body:
        raw_max_iterations = body.get('maxIterations')
    raw_max_concurrency = body.get('max_concurrency')
    if raw_max_concurrency is None and 'maxConcurrency' in body and 'max_concurrency' not in body:
        raw_max_concurrency = body.get('maxConcurrency')

    update_kwargs: dict[str, Any] = {}
    if raw_model_keys is not None or 'model_keys' in body or 'modelKeys' in body:
        update_kwargs['model_keys'] = [str(item) for item in raw_model_keys] if raw_model_keys is not None else None
    if 'max_iterations' in body or 'maxIterations' in body:
        update_kwargs['max_iterations'] = raw_max_iterations
    if 'max_concurrency' in body or 'maxConcurrency' in body:
        update_kwargs['max_concurrency'] = raw_max_concurrency
    return update_kwargs


def _bulk_scope_route_updates(payload: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    body = payload if isinstance(payload, dict) else {}
    raw_updates = body.get('updates')
    if not isinstance(raw_updates, dict) or not raw_updates:
        raise ValueError('updates must be a non-empty object')
    updates: dict[str, dict[str, Any]] = {}
    for raw_scope, raw_item in raw_updates.items():
        scope = str(raw_scope or '').strip()
        if not scope:
            raise ValueError('scope key must not be empty')
        if not isinstance(raw_item, dict):
            raise ValueError(f'updates.{scope} must be an object')
        updates[scope] = _scope_route_update_kwargs(raw_item)
    return updates


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
    return max(0, requested)


def _normalize_china_channel_id(channel_id: str) -> str:
    try:
        return normalize_registry_channel_id(channel_id)
    except KeyError:
        raise HTTPException(status_code=404, detail='china_channel_not_found')


def _china_channel_spec(channel_id: str) -> dict[str, Any]:
    try:
        return china_channel_spec(channel_id)
    except KeyError:
        raise HTTPException(status_code=404, detail='china_channel_not_found')


def _china_bridge_status_payload() -> dict[str, Any] | None:
    path = _china_bridge_status_path()
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def _china_bridge_status_pid(status: dict[str, Any]) -> int | None:
    try:
        pid = int(status.get('pid') or 0)
    except (TypeError, ValueError):
        return None
    return pid if pid > 0 else None


def _process_exists(pid: int | None) -> bool:
    if pid is None or pid <= 0:
        return False
    if os.name == 'nt':
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32
            process = kernel32.OpenProcess(0x1000 | 0x00100000, False, int(pid))
            if not process:
                return False
            try:
                return kernel32.WaitForSingleObject(process, 0) == 0x00000102
            finally:
                kernel32.CloseHandle(process)
        except Exception:
            return False
    try:
        os.kill(int(pid), 0)
    except OSError as exc:
        return exc.errno == errno.EPERM
    except Exception:
        return False
    return True


def _china_bridge_runtime_deferred_payload(cfg: Config) -> dict[str, str] | None:
    try:
        has_ceo_model = bool(cfg.get_role_model_keys('ceo'))
    except Exception:
        has_ceo_model = True
    if has_ceo_model:
        return None
    return {
        'reason': 'no_model_configured',
        'message': '???? CEO ??????????????',
    }


def _china_bridge_runtime_summary(cfg: Config) -> dict[str, Any]:
    status = _china_bridge_status_payload() or {}
    dist_entry = cfg.workspace_path / 'subsystems' / 'china_channels_host' / 'dist' / 'index.js'
    node_path = shutil.which(cfg.china_bridge.node_bin)
    raw_running = bool(status.get('running'))
    pid = _china_bridge_status_pid(status)
    pid_alive = _process_exists(pid) if pid is not None else False
    running = raw_running if pid is None else (raw_running and pid_alive)
    connected = bool(status.get('connected')) and running
    stale_state = raw_running and pid is not None and not pid_alive
    deferred = _china_bridge_runtime_deferred_payload(cfg)
    last_error = str(status.get('last_error') or '').strip()
    if stale_state and not last_error:
        last_error = 'china bridge host process is not running'
    if deferred is not None and not running and not connected:
        last_error = ''
    return {
        'enabled': bool(cfg.china_bridge.enabled),
        'public_port': int(cfg.china_bridge.public_port),
        'control_port': int(cfg.china_bridge.control_port),
        'node_bin': str(cfg.china_bridge.node_bin or 'node'),
        'node_found': bool(node_path),
        'node_path': node_path,
        'dist_entry': str(dist_entry),
        'dist_exists': dist_entry.exists(),
        'running': running,
        'connected': connected,
        'pid': pid,
        'pid_alive': pid_alive,
        'state_stale': stale_state,
        'status_path': str(_china_bridge_status_path()),
        'status_exists': bool(status),
        'startup_deferred': deferred is not None,
        'startup_deferred_reason': deferred['reason'] if deferred is not None else None,
        'startup_deferred_message': deferred['message'] if deferred is not None else None,
        'last_error': last_error or None,
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


def _channel_candidate_sections(payload: dict[str, Any]) -> list[dict[str, Any]]:
    sections: list[dict[str, Any]] = [payload]
    accounts = payload.get('accounts')
    if isinstance(accounts, dict):
        for account_payload in accounts.values():
            if isinstance(account_payload, dict):
                sections.append(account_payload)
    return sections


def _channel_section_value(section: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = section.get(key)
        if value not in (None, '', [], {}, False):
            return value
    return None


def _channel_section_has_all(section: dict[str, Any], *keys: tuple[str, ...]) -> bool:
    return all(_channel_section_value(section, *key_group) is not None for key_group in keys)


def _channel_mode_value(section: dict[str, Any], default: str) -> str:
    raw = _channel_section_value(section, 'mode', 'connectionMode')
    return str(raw or default).strip().lower()


def _channel_missing_requirements(channel_id: str, payload: dict[str, Any]) -> list[str]:
    sections = _channel_candidate_sections(payload)
    if channel_id == 'qqbot':
        return [] if any(
            _channel_section_has_all(section, ('appId', 'app_id'), ('clientSecret', 'client_secret'))
            for section in sections
        ) else ['appId', 'clientSecret']
    if channel_id == 'dingtalk':
        return [] if any(
            _channel_section_has_all(section, ('clientId', 'client_id'), ('clientSecret', 'client_secret'))
            for section in sections
        ) else ['clientId', 'clientSecret']
    if channel_id == 'wecom':
        return [] if any(
            (
                _channel_mode_value(section, 'ws') == 'webhook'
                and _channel_section_has_all(section, ('token',), ('encodingAESKey', 'encoding_aes_key'))
            )
            or (
                _channel_mode_value(section, 'ws') != 'webhook'
                and _channel_section_has_all(section, ('botId', 'bot_id'), ('secret',))
            )
            for section in sections
        ) else ['mode=ws 需 botId + secret；mode=webhook 需 token + encodingAESKey']
    if channel_id == 'wecom-app':
        return [] if any(
            _channel_section_has_all(section, ('token',), ('encodingAESKey', 'encoding_aes_key'))
            for section in sections
        ) else ['token', 'encodingAESKey']
    if channel_id == 'wecom-kf':
        return [] if any(
            _channel_section_has_all(section, ('corpId', 'corp_id'), ('token',), ('encodingAESKey', 'encoding_aes_key'))
            for section in sections
        ) else ['corpId', 'token', 'encodingAESKey']
    if channel_id == 'wechat-mp':
        return [] if any(
            _channel_section_has_all(section, ('appId', 'app_id'), ('token',))
            and (
                _channel_mode_value(section, 'safe') == 'plain'
                or _channel_section_has_all(section, ('encodingAESKey', 'encoding_aes_key'))
            )
            for section in sections
        ) else ['appId', 'token', 'safe/compat 模式还需 encodingAESKey']
    if channel_id == 'feishu-china':
        return [] if any(
            _channel_section_has_all(section, ('appId', 'app_id'), ('appSecret', 'app_secret'))
            for section in sections
        ) else ['appId', 'appSecret']
    return []


def _channel_top_level_section(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in dict(payload or {}).items()
        if key not in {'accounts', 'defaultAccount', 'default_account'}
    }


def _channel_effective_sections(payload: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    base = _channel_top_level_section(payload)
    accounts = payload.get('accounts')
    if isinstance(accounts, dict) and accounts:
        sections: list[tuple[str, dict[str, Any]]] = []
        for raw_account_id, raw_account_payload in accounts.items():
            account_id = str(raw_account_id or '').strip()
            if not account_id or not isinstance(raw_account_payload, dict):
                continue
            sections.append((account_id, {**base, **raw_account_payload}))
        if sections:
            return sections
    return [('default', dict(payload or {}))]


def _channel_account_count(channel_id: str, payload: dict[str, Any]) -> int:
    accounts = payload.get('accounts')
    if isinstance(accounts, dict) and accounts:
        return len([key for key in accounts.keys() if str(key or '').strip()])
    top_level = _channel_top_level_section(payload)
    has_top_level_values = any(value not in (None, '', [], {}, False) for value in top_level.values())
    return 1 if has_top_level_values else 0


def _string_value(value: Any) -> str:
    return str(value or '').strip()


def _channel_template_placeholder_values(channel_id: str, *keys: str) -> set[str]:
    template = china_channel_template(channel_id)
    if not template:
        return set()
    candidates: list[dict[str, Any]] = [template]
    accounts = template.get('accounts')
    if isinstance(accounts, dict):
        candidates.extend(account for account in accounts.values() if isinstance(account, dict))
    values: set[str] = set()
    for section in candidates:
        for key in keys:
            value = _string_value(section.get(key))
            if value:
                values.add(value)
    return values


def _describe_http_error(exc: httpx.HTTPError, url: str) -> str:
    detail = str(exc).strip()
    if detail:
        return detail
    cause = str(getattr(exc, '__cause__', '') or '').strip()
    request = getattr(exc, 'request', None)
    request_url = str(getattr(request, 'url', '') or '').strip() or str(url or '').strip()
    parts = [exc.__class__.__name__ or 'HTTPError']
    if cause:
        parts.append(cause)
    if request_url:
        parts.append(request_url)
    return '（'.join([parts[0], '；'.join(parts[1:])]) + ')' if len(parts) > 1 else parts[0]


def _qqbot_placeholder_fields(section: dict[str, Any]) -> list[str]:
    app_id = _string_value(_channel_section_value(section, 'appId', 'app_id'))
    client_secret = _string_value(_channel_section_value(section, 'clientSecret', 'client_secret'))
    placeholder_fields: list[str] = []
    if app_id and app_id in _channel_template_placeholder_values('qqbot', 'appId', 'app_id'):
        placeholder_fields.append('appId')
    if client_secret and client_secret in _channel_template_placeholder_values('qqbot', 'clientSecret', 'client_secret'):
        placeholder_fields.append('clientSecret')
    return placeholder_fields


def _trim_probe_response_text(text: str, limit: int = 240) -> str:
    body = str(text or '').strip()
    if not body:
        return ''
    if len(body) <= limit:
        return body
    return body[: limit - 1] + '…'


async def _probe_http_json(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    json_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    try:
        response = await client.request(
            method,
            url,
            headers=headers,
            json=json_payload,
        )
    except httpx.HTTPError as exc:
        raise RuntimeError(f'请求失败：{_describe_http_error(exc, url)}') from exc
    if response.status_code >= 400:
        body = _trim_probe_response_text(response.text)
        suffix = f'：{body}' if body else ''
        raise RuntimeError(f'HTTP {response.status_code}{suffix}')
    try:
        payload = response.json()
    except Exception as exc:
        body = _trim_probe_response_text(response.text)
        suffix = f'：{body}' if body else ''
        raise RuntimeError(f'响应不是合法 JSON{suffix}') from exc
    if not isinstance(payload, dict):
        raise RuntimeError('响应格式异常，预期应返回 JSON 对象。')
    return payload


async def _probe_qqbot_connectivity(payload: dict[str, Any]) -> dict[str, Any]:
    sections = _channel_effective_sections(payload)
    async with httpx.AsyncClient(timeout=CHINA_PROBE_TIMEOUT) as client:
        for account_id, section in sections:
            placeholder_fields = _qqbot_placeholder_fields(section)
            if placeholder_fields:
                fields_text = ' / '.join(placeholder_fields)
                raise RuntimeError(
                    f'QQ Bot 账号 {account_id} 仍在使用模板占位值（{fields_text}）。'
                    '如果只配置单账号，请删除 defaultAccount / accounts；'
                    f'如果配置多账号，请将 accounts.{account_id}.{fields_text} 替换为真实值。'
                )
            app_id = _string_value(_channel_section_value(section, 'appId', 'app_id'))
            client_secret = _string_value(_channel_section_value(section, 'clientSecret', 'client_secret'))
            token_payload = await _probe_http_json(
                client,
                'POST',
                QQBOT_ACCESS_TOKEN_URL,
                json_payload={'appId': app_id, 'clientSecret': client_secret},
            )
            access_token = _string_value(token_payload.get('access_token'))
            if not access_token:
                raise RuntimeError(f'QQ Bot 账号 {account_id} 未返回 access_token，请检查 appId / clientSecret 是否正确。')
            gateway_payload = await _probe_http_json(
                client,
                'GET',
                QQBOT_GATEWAY_URL,
                headers={'Authorization': f'QQBot {access_token}'},
            )
            gateway_url = _string_value(gateway_payload.get('url'))
            if not gateway_url:
                raise RuntimeError(f'QQ Bot 账号 {account_id} 未返回 gateway 地址，请稍后重试。')
    return {
        'status': 'success',
        'checked': True,
        'message': f'已完成 {len(sections)} 个 QQ Bot 账号的 access_token 与 gateway 连通性校验。',
        'details': [],
    }


async def _probe_dingtalk_connectivity(payload: dict[str, Any]) -> dict[str, Any]:
    sections = _channel_effective_sections(payload)
    async with httpx.AsyncClient(timeout=CHINA_PROBE_TIMEOUT) as client:
        for account_id, section in sections:
            client_id = _string_value(_channel_section_value(section, 'clientId', 'client_id'))
            client_secret = _string_value(_channel_section_value(section, 'clientSecret', 'client_secret'))
            token_payload = await _probe_http_json(
                client,
                'POST',
                DINGTALK_ACCESS_TOKEN_URL,
                json_payload={'appKey': client_id, 'appSecret': client_secret},
            )
            access_token = _string_value(token_payload.get('accessToken'))
            if not access_token:
                raise RuntimeError(f'钉钉账号 {account_id} 未返回 accessToken，请检查 clientId / clientSecret 是否正确。')
    return {
        'status': 'success',
        'checked': True,
        'message': f'已完成 {len(sections)} 个钉钉账号的 accessToken 连通性校验。',
        'details': [],
    }


async def _probe_wecom_app_connectivity(payload: dict[str, Any]) -> dict[str, Any]:
    sections = [
        (account_id, section)
        for account_id, section in _channel_effective_sections(payload)
        if _channel_section_has_all(section, ('corpId', 'corp_id'), ('corpSecret', 'corp_secret'))
    ]
    if not sections:
        return {
            'status': 'warning',
            'checked': False,
            'message': '当前企业微信应用仅检测到 webhook 入站配置；未提供 corpId / corpSecret，无法在保存前校验企业微信 API 连通性。',
            'details': [],
        }
    async with httpx.AsyncClient(timeout=CHINA_PROBE_TIMEOUT) as client:
        for account_id, section in sections:
            corp_id = _string_value(_channel_section_value(section, 'corpId', 'corp_id'))
            corp_secret = _string_value(_channel_section_value(section, 'corpSecret', 'corp_secret'))
            query = httpx.QueryParams({'corpid': corp_id, 'corpsecret': corp_secret})
            token_payload = await _probe_http_json(
                client,
                'GET',
                f'{WECOM_ACCESS_TOKEN_URL}?{query}',
            )
            errcode = token_payload.get('errcode')
            if errcode not in (None, 0):
                errmsg = _string_value(token_payload.get('errmsg')) or 'unknown error'
                raise RuntimeError(f'企业微信应用账号 {account_id} 获取 access_token 失败：{errmsg} (errcode={errcode})')
            access_token = _string_value(token_payload.get('access_token'))
            if not access_token:
                raise RuntimeError(f'企业微信应用账号 {account_id} 未返回 access_token，请检查 corpId / corpSecret 是否正确。')
    return {
        'status': 'success',
        'checked': True,
        'message': f'已完成 {len(sections)} 个企业微信应用账号的 access_token 连通性校验。',
        'details': [],
    }


async def _probe_wecom_kf_connectivity(payload: dict[str, Any]) -> dict[str, Any]:
    sections = _channel_effective_sections(payload)
    checkable = [
        (account_id, section)
        for account_id, section in sections
        if _channel_section_has_all(section, ('corpId', 'corp_id'), ('corpSecret', 'corp_secret'))
    ]
    if not checkable:
        return {
            'status': 'warning',
            'checked': False,
            'message': '当前企业微信客服仅检测到 webhook 入站配置；未提供 corpSecret，无法在保存前校验企业微信 API 连通性。',
            'details': [],
        }
    async with httpx.AsyncClient(timeout=CHINA_PROBE_TIMEOUT) as client:
        for account_id, section in checkable:
            corp_id = _string_value(_channel_section_value(section, 'corpId', 'corp_id'))
            corp_secret = _string_value(_channel_section_value(section, 'corpSecret', 'corp_secret'))
            query = httpx.QueryParams({'corpid': corp_id, 'corpsecret': corp_secret})
            token_payload = await _probe_http_json(client, 'GET', f'{WECOM_ACCESS_TOKEN_URL}?{query}')
            errcode = token_payload.get('errcode')
            if errcode not in (None, 0):
                errmsg = _string_value(token_payload.get('errmsg')) or 'unknown error'
                raise RuntimeError(f'企业微信客服账号 {account_id} 获取 access_token 失败：{errmsg} (errcode={errcode})')
            if not _string_value(token_payload.get('access_token')):
                raise RuntimeError(f'企业微信客服账号 {account_id} 未返回 access_token，请检查 corpId / corpSecret 是否正确。')
    return {
        'status': 'success',
        'checked': True,
        'message': f'已完成 {len(checkable)} 个企业微信客服账号的 access_token 连通性校验。',
        'details': [],
    }


async def _probe_wechat_mp_connectivity(payload: dict[str, Any]) -> dict[str, Any]:
    sections = _channel_effective_sections(payload)
    checkable = [
        (account_id, section)
        for account_id, section in sections
        if _channel_section_has_all(section, ('appId', 'app_id'), ('appSecret', 'app_secret'))
    ]
    if not checkable:
        return {
            'status': 'warning',
            'checked': False,
            'message': '当前微信公众号仅检测到被动回复配置；未提供 appSecret，无法在保存前校验主动发送 access_token。',
            'details': [],
        }
    async with httpx.AsyncClient(timeout=CHINA_PROBE_TIMEOUT) as client:
        for account_id, section in checkable:
            app_id = _string_value(_channel_section_value(section, 'appId', 'app_id'))
            app_secret = _string_value(_channel_section_value(section, 'appSecret', 'app_secret'))
            query = httpx.QueryParams(
                {'grant_type': 'client_credential', 'appid': app_id, 'secret': app_secret}
            )
            token_payload = await _probe_http_json(client, 'GET', f'https://api.weixin.qq.com/cgi-bin/token?{query}')
            errcode = token_payload.get('errcode')
            if errcode not in (None, 0):
                errmsg = _string_value(token_payload.get('errmsg')) or 'unknown error'
                raise RuntimeError(f'微信公众号账号 {account_id} 获取 access_token 失败：{errmsg} (errcode={errcode})')
            if not _string_value(token_payload.get('access_token')):
                raise RuntimeError(f'微信公众号账号 {account_id} 未返回 access_token，请检查 appId / appSecret 是否正确。')
    return {
        'status': 'success',
        'checked': True,
        'message': f'已完成 {len(checkable)} 个微信公众号账号的 access_token 连通性校验。',
        'details': [],
    }


async def _probe_feishu_connectivity(payload: dict[str, Any]) -> dict[str, Any]:
    sections = _channel_effective_sections(payload)
    async with httpx.AsyncClient(timeout=CHINA_PROBE_TIMEOUT) as client:
        for account_id, section in sections:
            app_id = _string_value(_channel_section_value(section, 'appId', 'app_id'))
            app_secret = _string_value(_channel_section_value(section, 'appSecret', 'app_secret'))
            token_payload = await _probe_http_json(
                client,
                'POST',
                FEISHU_APP_ACCESS_TOKEN_URL,
                json_payload={'app_id': app_id, 'app_secret': app_secret},
            )
            code = token_payload.get('code')
            if code not in (None, 0):
                msg = _string_value(token_payload.get('msg')) or 'unknown error'
                raise RuntimeError(f'飞书账号 {account_id} 获取 app_access_token 失败：{msg} (code={code})')
            access_token = _string_value(token_payload.get('app_access_token') or token_payload.get('tenant_access_token'))
            if not access_token:
                raise RuntimeError(f'飞书账号 {account_id} 未返回 app_access_token，请检查 appId / appSecret 是否正确。')
    return {
        'status': 'success',
        'checked': True,
        'message': f'已完成 {len(sections)} 个飞书账号的 app_access_token 连通性校验。',
        'details': [],
    }


async def _probe_china_channel_platform_connectivity(channel_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    if channel_id == 'qqbot':
        return await _probe_qqbot_connectivity(payload)
    if channel_id == 'dingtalk':
        return await _probe_dingtalk_connectivity(payload)
    if channel_id == 'wecom-app':
        return await _probe_wecom_app_connectivity(payload)
    if channel_id == 'wecom-kf':
        return await _probe_wecom_kf_connectivity(payload)
    if channel_id == 'wechat-mp':
        return await _probe_wechat_mp_connectivity(payload)
    if channel_id == 'feishu-china':
        return await _probe_feishu_connectivity(payload)
    if channel_id == 'wecom':
        mode = _channel_mode_value(_channel_top_level_section(payload), 'ws')
        if mode == 'webhook':
            return {
                'status': 'warning',
                'checked': False,
                'message': '企业微信机器人 webhook 模式需要依赖平台回调验签，保存前只能完成本地字段校验。',
                'details': [],
            }
        return {
            'status': 'warning',
            'checked': False,
            'message': '企业微信机器人 ws 模式暂不支持无副作用的后台预检，保存前只能完成本地字段校验。',
            'details': [],
        }
    return {
        'status': 'warning',
        'checked': False,
        'message': '当前渠道暂未实现平台侧预检，已完成本地字段校验。',
        'details': [],
    }


def _build_china_channel_item(
    cfg: Config,
    channel_id: str,
    *,
    enabled: bool | None = None,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    spec = _china_channel_spec(channel_id)
    if payload is None:
        channel_model = getattr(cfg.china_bridge.channels, china_channel_attr(channel_id))
        source_payload = channel_model.model_dump(by_alias=True, exclude_none=True)
        enabled_value = bool(source_payload.pop('enabled', False))
    else:
        source_payload = dict(payload or {})
        source_payload.pop('enabled', None)
        enabled_value = bool(enabled)
    runtime = _china_bridge_runtime_summary(cfg)
    return {
        'id': spec['id'],
        'label': spec['label'],
        'description': spec['description'],
        'maintenance_status': china_channel_maintenance_status(channel_id),
        'config_path': f"chinaBridge.channels.{spec['id']}",
        'enabled': enabled_value,
        'account_count': _channel_account_count(channel_id, source_payload),
        'config': source_payload,
        'json_text': json.dumps(source_payload, ensure_ascii=False, indent=2),
        'template_json': china_channel_template(channel_id),
        'runtime': runtime,
    }


def _serialize_china_channel(cfg: Config, channel_id: str) -> dict[str, Any]:
    return _build_china_channel_item(cfg, channel_id)


async def _test_china_channel(
    cfg: Config,
    channel_id: str,
    *,
    enabled: bool | None = None,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    item = _build_china_channel_item(cfg, channel_id, enabled=enabled, payload=payload)
    runtime = item['runtime']
    if not item['enabled']:
        return {
            'status': 'disabled',
            'title': '当前通信已禁用',
            'message': '配置已校验，当前渠道保持禁用状态。',
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
    try:
        platform_probe = await _probe_china_channel_platform_connectivity(channel_id, item['config'])
    except Exception as exc:
        return {
            'status': 'error',
            'title': '测试失败',
            'message': f'平台连通性校验失败：{exc}',
            'details': [],
        }
    if str(platform_probe.get('status') or '').strip().lower() == 'error':
        return {
            'status': 'error',
            'title': '测试失败',
            'message': _string_value(platform_probe.get('message')) or '平台连通性校验失败。',
            'details': list(platform_probe.get('details') or []),
        }
    if runtime['running'] and runtime['connected']:
        return {
            'status': 'success',
            'title': '连接成功',
            'message': _string_value(platform_probe.get('message')) or '平台连通性校验通过，桥接宿主正在运行，内部控制连接已建立。',
            'details': [],
        }
    warnings: list[str] = []
    probe_message = _string_value(platform_probe.get('message'))
    if str(platform_probe.get('status') or '').strip().lower() == 'warning' and probe_message:
        warnings.append(probe_message)
    for detail in platform_probe.get('details') or []:
        text = _string_value(detail)
        if text:
            warnings.append(text)
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
            'message': probe_message or '配置校验已通过，但本地桥接环境尚未完全就绪。',
            'details': warnings,
        }
    return {
        'status': 'success',
        'title': '测试通过',
        'message': probe_message or '配置校验已通过，等待宿主完成平台侧连接。',
        'details': [],
    }


def _update_china_channel_config(cfg: Config, channel_id: str, *, enabled: bool, payload: dict[str, Any]) -> Config:
    spec = _china_channel_spec(channel_id)
    config_payload = dict(payload or {})
    config_payload.pop('enabled', None)

    full_payload = cfg.model_dump(by_alias=True, exclude_none=True)
    bridge_payload = full_payload.setdefault('chinaBridge', {})
    channels_payload = bridge_payload.setdefault('channels', {})
    channels_payload[spec['id']] = {
        **config_payload,
        'enabled': bool(enabled),
    }
    bridge_payload['enabled'] = any(
        bool((channels_payload.get(item['id']) or {}).get('enabled'))
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
        **_model_roles_payload(manager),
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


@router.put('/models/routes/batch')
async def update_model_roles_bulk(payload: dict = Body(...)):
    manager = ModelManager.load()
    try:
        result = manager.update_scope_routes_bulk(_bulk_scope_route_updates(payload))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await _refresh_runtime('admin_model_roles')
    return {
        'ok': True,
        **result,
    }


@router.put('/models/{model_key:path}')
async def update_model(model_key: str, payload: dict = Body(...)):
    manager = ModelManager.load()
    body = payload if isinstance(payload, dict) else {}
    raw_retry_count = payload.get('retry_count')
    if raw_retry_count is None and 'retryCount' in payload:
        raw_retry_count = payload.get('retryCount')

    def _pick(snake_key: str, camel_key: str | None = None):
        if snake_key in body:
            return body.get(snake_key)
        if camel_key and camel_key in body:
            return body.get(camel_key)
        return _UNSET

    try:
        item = manager.update_model(
            key=model_key,
            provider_model=_pick('provider_model', 'providerModel'),
            api_key=_pick('api_key', 'apiKey'),
            api_base=_pick('api_base', 'apiBase'),
            extra_headers=(
                body.get('extra_headers')
                if 'extra_headers' in body and isinstance(body.get('extra_headers'), dict)
                else body.get('extraHeaders')
                if 'extraHeaders' in body and isinstance(body.get('extraHeaders'), dict)
                else _UNSET
            ),
            max_tokens=_pick('max_tokens', 'maxTokens'),
            temperature=_pick('temperature'),
            reasoning_effort=_pick('reasoning_effort', 'reasoningEffort'),
            retry_on=(
                [str(item) for item in (body.get('retry_on') or [])]
                if 'retry_on' in body
                else [str(item) for item in (body.get('retryOn') or [])]
                if 'retryOn' in body
                else _UNSET
            ),
            retry_count=raw_retry_count if ('retry_count' in body or 'retryCount' in body) else _UNSET,
            description=_pick('description'),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await _refresh_runtime('admin_model_update')
    return {'ok': True, 'item': item}


@router.post('/models/{model_key:path}/enable')
async def enable_model(model_key: str):
    manager = ModelManager.load()
    try:
        item = manager.set_model_enabled(model_key, True)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await _refresh_runtime('admin_model_enable')
    return {'ok': True, 'item': item}


@router.post('/models/{model_key:path}/disable')
async def disable_model(model_key: str):
    manager = ModelManager.load()
    try:
        item = manager.set_model_enabled(model_key, False)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await _refresh_runtime('admin_model_disable')
    return {'ok': True, 'item': item}


@router.delete('/models/{model_key:path}')
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
        update_kwargs = _scope_route_update_kwargs(payload)
        roles = manager.update_scope_route(
            scope,
            **update_kwargs,
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
        'role_concurrency': _model_role_concurrency(manager),
    }


@router.get('/llm/templates')
async def list_llm_templates():
    return {'ok': True, 'items': _llm_facade().list_templates()}


@router.get('/llm/templates/{provider_id}')
async def get_llm_template(provider_id: str):
    try:
        item = _llm_facade().get_template(provider_id)
    except Exception as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {'ok': True, 'item': item}


@router.post('/llm/drafts/validate')
async def validate_llm_draft(payload: dict = Body(...)):
    try:
        result = _llm_facade().validate_draft(payload)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {'ok': True, 'result': result}


@router.post('/llm/drafts/probe')
async def probe_llm_draft(payload: dict = Body(...)):
    try:
        result = _llm_facade().probe_draft(payload)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {'ok': True, 'result': result}


@router.post('/llm/drafts/probe-max-concurrency')
async def probe_llm_draft_max_concurrency(payload: dict = Body(...)):
    try:
        result = await _llm_facade().probe_max_concurrency_draft(payload)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {'ok': True, 'result': result}


@router.get('/llm/configs')
async def list_llm_configs():
    return {'ok': True, 'items': _llm_facade().list_config_records()}


@router.get('/llm/configs/{config_id}')
async def get_llm_config(config_id: str, include_secrets: bool = Query(False)):
    try:
        item = _llm_facade().get_config_record(config_id, include_secrets=include_secrets)
    except Exception as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {'ok': True, 'item': item}


@router.post('/llm/configs')
async def create_llm_config(payload: dict = Body(...)):
    try:
        item = _llm_facade().create_config_record(payload)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {'ok': True, 'item': item}


@router.put('/llm/configs/{config_id}')
async def update_llm_config(config_id: str, payload: dict = Body(...)):
    try:
        item = _llm_facade().update_config_record(config_id, payload)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    runtime_refresh = await _refresh_runtime_after_save('admin_llm_config_update')
    return {'ok': True, 'item': item, 'runtime_refresh': runtime_refresh}


@router.delete('/llm/configs/{config_id}')
async def delete_llm_config(config_id: str):
    try:
        _llm_facade().delete_config_record(config_id)
    except Exception as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {'ok': True}


@router.get('/llm/bindings')
async def list_llm_bindings():
    manager = ModelManager.load()
    return {
        'ok': True,
        'items': manager.list_models(),
        **_llm_routes_payload(manager),
    }


@router.post('/llm/bindings')
async def create_llm_binding(payload: dict = Body(...)):
    manager = ModelManager.load()
    draft = payload.get('draft') if isinstance(payload.get('draft'), dict) else {}
    binding = payload.get('binding') if isinstance(payload.get('binding'), dict) else {}
    try:
        item = manager.facade.create_binding(manager.config, draft_payload=draft, binding_payload=binding)
        manager._revalidate()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=_llm_binding_create_error_detail(exc)) from exc
    manager.save()
    runtime_refresh = await _refresh_runtime_after_save('admin_llm_binding_create')
    return {'ok': True, 'item': item, 'runtime_refresh': runtime_refresh}


@router.put('/llm/bindings/{model_key:path}')
async def update_llm_binding(model_key: str, payload: dict = Body(...)):
    manager = ModelManager.load()
    try:
        item = manager.facade.update_binding(manager.config, model_key=model_key, draft_payload=payload)
        manager._revalidate()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    manager.save()
    runtime_refresh = await _refresh_runtime_after_save('admin_llm_binding_update')
    return {'ok': True, 'item': item, 'runtime_refresh': runtime_refresh}


@router.get('/runtime-refresh/{command_id:path}')
async def get_runtime_refresh_status(command_id: str):
    try:
        service = _service()
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    item = getattr(service, 'get_task_command_status', lambda _command_id: None)(command_id)
    if item is None:
        raise HTTPException(status_code=404, detail='runtime_refresh_command_not_found')
    return {'ok': True, 'item': item}


@router.post('/llm/bindings/{model_key:path}/enable')
async def enable_llm_binding(model_key: str):
    manager = ModelManager.load()
    try:
        item = manager.set_model_enabled(model_key, True)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    runtime_refresh = await _refresh_runtime_after_save('admin_llm_binding_enable')
    return {'ok': True, 'item': item, 'runtime_refresh': runtime_refresh}


@router.post('/llm/bindings/{model_key:path}/disable')
async def disable_llm_binding(model_key: str):
    manager = ModelManager.load()
    try:
        item = manager.set_model_enabled(model_key, False)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    runtime_refresh = await _refresh_runtime_after_save('admin_llm_binding_disable')
    return {'ok': True, 'item': item, 'runtime_refresh': runtime_refresh}


@router.delete('/llm/bindings/{model_key:path}')
async def delete_llm_binding(model_key: str):
    manager = ModelManager.load()
    try:
        manager.delete_model(model_key)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    runtime_refresh = await _refresh_runtime_after_save('admin_llm_binding_delete')
    return {'ok': True, 'runtime_refresh': runtime_refresh}


@router.get('/llm/routes')
async def get_llm_routes():
    manager = ModelManager.load()
    return {
        'ok': True,
        **_llm_routes_payload(manager),
    }


@router.put('/llm/routes/{scope}')
async def update_llm_route(scope: str, payload: dict = Body(...)):
    manager = ModelManager.load()
    try:
        update_kwargs = _scope_route_update_kwargs(payload)
        route = manager.update_scope_route(
            scope,
            **update_kwargs,
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await _refresh_runtime('admin_llm_route_update')
    return {
        'ok': True,
        'route': route,
        **_llm_routes_payload(manager),
    }


@router.put('/llm/routes')
async def update_llm_routes_bulk(payload: dict = Body(...)):
    manager = ModelManager.load()
    try:
        result = manager.update_scope_routes_bulk(_bulk_scope_route_updates(payload))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await _refresh_runtime('admin_llm_route_update')
    return {
        'ok': True,
        'updated_scopes': result.get('updated_scopes', []),
        'routes': result.get('roles', {}),
        'role_iterations': result.get('role_iterations', {}),
        'role_concurrency': result.get('role_concurrency', {}),
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
    body = payload if isinstance(payload, dict) else {}
    try:
        current = manager.facade.get_memory_binding()

        def _pick(snake_key: str, camel_key: str, current_value: str | None) -> str | None:
            if snake_key in body:
                return str(body.get(snake_key) or '').strip() or None
            if camel_key in body:
                return str(body.get(camel_key) or '').strip() or None
            return current_value

        result = manager.facade.set_memory_binding(
            embedding_config_id=_pick(
                'embedding_config_id',
                'embeddingConfigId',
                current.embedding_config_id,
            ),
            rerank_config_id=_pick(
                'rerank_config_id',
                'rerankConfigId',
                current.rerank_config_id,
            ),
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await _refresh_runtime('admin_llm_memory_update')
    return {'ok': True, 'item': result.model_dump(mode='json')}


@router.post('/llm/memory/embedding-atomic-save')
async def atomic_save_llm_memory_embedding(payload: dict | None = Body(default=None)):
    manager = ModelManager.load()
    body = payload if isinstance(payload, dict) else {}
    embedding_payload = body.get('embedding') if isinstance(body.get('embedding'), dict) else None
    rerank_payload = body.get('rerank') if isinstance(body.get('rerank'), dict) else None
    if embedding_payload is None:
        raise HTTPException(status_code=400, detail='embedding payload is required')
    item = await _save_memory_embedding_atomically(
        facade=manager.facade,
        embedding_payload=embedding_payload,
        rerank_payload=rerank_payload,
    )
    return {'ok': True, 'item': item}


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
    with _resource_service() as service:
        return {'ok': True, 'items': [item.model_dump(mode='json') for item in service.list_skill_resources()]}


@router.get('/resources/skills/{skill_id}')
async def get_skill(skill_id: str):
    with _resource_service() as service:
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
    with _resource_service() as service:
        item = service.get_skill_resource(skill_id)
        if item is None:
            raise HTTPException(status_code=404, detail='skill_not_found')
        return {'ok': True, 'items': [{'file_key': file_key, 'path': path} for file_key, path in service.list_skill_files(skill_id).items()]}


@router.get('/resources/skills/{skill_id}/files/{file_key}')
async def get_skill_file(skill_id: str, file_key: str):
    with _resource_service() as service:
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
    with _resource_service() as service:
        if service.get_skill_resource(skill_id) is None:
            raise HTTPException(status_code=404, detail='skill_not_found')
        try:
            item = await service.write_skill_file_async(skill_id, file_key, str(payload.get('content') or ''), session_id=session_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {'ok': True, 'item': item}


@router.put('/resources/skills/{skill_id}/policy')
async def update_skill_policy(skill_id: str, payload: dict = Body(...), session_id: str = Query('web:shared')):
    with _resource_service() as service:
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
    with _resource_service() as service:
        item = service.enable_skill(skill_id, session_id=session_id)
        if item is None:
            raise HTTPException(status_code=404, detail='skill_not_found')
        return {'ok': True, 'item': item.model_dump(mode='json')}


@router.post('/resources/skills/{skill_id}/disable')
async def disable_skill(skill_id: str, session_id: str = Query('web:shared')):
    with _resource_service() as service:
        item = service.disable_skill(skill_id, session_id=session_id)
        if item is None:
            raise HTTPException(status_code=404, detail='skill_not_found')
        return {'ok': True, 'item': item.model_dump(mode='json')}


@router.delete('/resources/skills/{skill_id}')
async def delete_skill(skill_id: str, session_id: str = Query('web:shared')):
    with _resource_service() as service:
        try:
            item = await service.delete_skill_resource_async(skill_id, session_id=session_id)
        except ValueError as exc:
            raise _resource_delete_http_error(exc) from exc
        return {'ok': True, 'item': item}


@router.get('/resources/tools')
async def list_tools():
    with _resource_service() as service:
        return {'ok': True, 'items': [item.model_dump(mode='json') for item in service.list_tool_resources()]}


@router.get('/resources/tools/{tool_id}')
async def get_tool(tool_id: str):
    with _resource_service() as service:
        item = service.get_tool_family(tool_id)
        if item is None:
            raise HTTPException(status_code=404, detail='tool_not_found')
        return {'ok': True, 'item': item.model_dump(mode='json')}


@router.get('/resources/tools/{tool_id}/toolskill')
async def get_tool_toolskill(tool_id: str):
    with _resource_service() as service:
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
    enabled = bool(payload.get('enabled'))
    probe_result = await _test_china_channel(
        cfg,
        channel_id,
        enabled=enabled,
        payload=config_payload,
    )
    if enabled and str(probe_result.get('status') or '').strip().lower() == 'error':
        raise HTTPException(
            status_code=400,
            detail={
                'code': 'china_channel_probe_failed',
                'message': str(probe_result.get('message') or '平台连通性校验失败'),
                'probe': probe_result,
            },
        )
    next_cfg = _update_china_channel_config(
        cfg,
        channel_id,
        enabled=enabled,
        payload=config_payload,
    )
    await _refresh_runtime('admin_china_bridge_channel_update')
    return {
        'ok': True,
        'item': _serialize_china_channel(next_cfg, channel_id),
        'probe_result': probe_result,
    }


@router.post('/china-bridge/channels/{channel_id}/test')
async def test_china_bridge_channel(channel_id: str, payload: dict | None = Body(default=None)):
    cfg = load_config()
    body = payload if isinstance(payload, dict) else {}
    saved_item = _serialize_china_channel(cfg, channel_id)
    config_payload = body.get('config') if isinstance(body.get('config'), dict) else None
    enabled = body.get('enabled') if 'enabled' in body else saved_item['enabled']
    return {
        'ok': True,
        'item': _build_china_channel_item(cfg, channel_id, enabled=enabled, payload=config_payload) if config_payload is not None else saved_item,
        'result': await _test_china_channel(cfg, channel_id, enabled=enabled, payload=config_payload),
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
            channel_id: bool(getattr(cfg.china_bridge.channels, china_channel_attr(channel_id)).enabled)
            for channel_id in china_channel_ids()
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
    with _resource_service() as service:
        actions_payload = payload.get('actions') if isinstance(payload.get('actions'), dict) else None
        normalized_actions: dict[str, list[str]] | None = None
        if actions_payload is not None:
            normalized_actions = {
                str(action_id): [str(role) for role in (roles or [])]
                for action_id, roles in actions_payload.items()
            }
        try:
            item = service.update_tool_policy(
                tool_id,
                session_id=session_id,
                enabled=payload.get('enabled'),
                allowed_roles_by_action=normalized_actions,
                execution_mode=payload.get('execution_mode'),
            )
        except ValueError as exc:
            raise _resource_delete_http_error(exc) from exc
        if item is None:
            raise HTTPException(status_code=404, detail='tool_not_found')
        return {'ok': True, 'item': item.model_dump(mode='json')}


@router.post('/resources/tools/{tool_id}/enable')
async def enable_tool(tool_id: str, session_id: str = Query('web:shared')):
    with _resource_service() as service:
        try:
            item = service.enable_tool(tool_id, session_id=session_id)
        except ValueError as exc:
            raise _resource_delete_http_error(exc) from exc
        if item is None:
            raise HTTPException(status_code=404, detail='tool_not_found')
        return {'ok': True, 'item': item.model_dump(mode='json')}


@router.post('/resources/tools/{tool_id}/disable')
async def disable_tool(tool_id: str, session_id: str = Query('web:shared')):
    with _resource_service() as service:
        try:
            item = service.disable_tool(tool_id, session_id=session_id)
        except ValueError as exc:
            raise _resource_delete_http_error(exc) from exc
        if item is None:
            raise HTTPException(status_code=404, detail='tool_not_found')
        return {'ok': True, 'item': item.model_dump(mode='json')}


@router.delete('/resources/tools/{tool_id}')
async def delete_tool(tool_id: str, session_id: str = Query('web:shared')):
    with _resource_service() as service:
        try:
            item = await service.delete_tool_resource_async(tool_id, session_id=session_id)
        except ValueError as exc:
            raise _resource_delete_http_error(exc) from exc
        return {'ok': True, 'item': item}


@router.post('/resources/reload')
async def reload_resources(payload: dict[str, Any] | None = Body(default=None), session_id: str = Query('web:shared')):
    with _resource_service() as service:
        startup = getattr(service, 'startup', None)
        if callable(startup):
            await startup()
        effective_session_id = str((payload or {}).get('session_id') or session_id or 'web:shared')
        result = await service.reload_resources_async(session_id=effective_session_id)
        return {'ok': True, **result}


@router.get('/memory/retrieval-traces')
async def get_retrieval_traces(limit: int = Query(20, ge=1, le=200)):
    service = _service()
    await service.startup()
    return await service.get_context_traces(trace_kind='retrieval', limit=limit)


def _runtime_memory_manager():
    agent = get_agent()
    manager = getattr(agent, 'memory_manager', None)
    if manager is None:
        service = getattr(agent, 'main_task_service', None)
        manager = getattr(service, 'memory_manager', None) if service is not None else None
    if manager is None:
        raise HTTPException(status_code=503, detail='memory_manager_unavailable')
    return manager


def _memory_admin_mutations_enabled() -> bool:
    return _StandaloneResourceService._bool_env('G3KU_ENABLE_MEMORY_ADMIN_MUTATIONS', default=False)


def _memory_admin_mutation_disabled() -> HTTPException:
    return HTTPException(
        status_code=403,
        detail={
            'code': 'memory_admin_mutation_disabled',
            'message': 'memory admin mutations are disabled',
        },
    )


def _memory_admin_audit_path(manager: Any) -> Path:
    workspace = Path(getattr(manager, 'workspace', Path.cwd()))
    return workspace / 'memory' / 'admin_audit.jsonl'


def _append_memory_admin_audit_event(manager: Any, payload: dict[str, Any]) -> None:
    path = _memory_admin_audit_path(manager)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(dict(payload or {}), ensure_ascii=False)
    with path.open('a', encoding='utf-8') as fh:
        fh.write(line + '\n')


def _queue_row_to_dict(row: Any) -> dict[str, Any]:
    if isinstance(row, dict):
        return dict(row)
    field_names = getattr(type(row), '__dataclass_fields__', None)
    if isinstance(field_names, dict):
        return {name: getattr(row, name) for name in field_names}
    keys = (
        'request_id',
        'op',
        'decision_source',
        'payload_text',
        'created_at',
        'status',
        'processing_started_at',
        'last_error_text',
        'last_error_at',
        'retry_after',
        'trigger_source',
        'session_key',
    )
    return {name: getattr(row, name) for name in keys if hasattr(row, name)}


def _queue_row_with_update(row: Any, **updates: Any) -> Any:
    payload = {**_queue_row_to_dict(row), **updates}
    field_names = getattr(type(row), '__dataclass_fields__', None)
    if isinstance(field_names, dict):
        return row.__class__(**payload)
    return payload


async def _retry_memory_queue_head_with_fallback(
    manager: Any,
    *,
    reason: str,
    request_id: str,
) -> dict[str, Any]:
    retry_fn = getattr(manager, 'retry_queue_head', None)
    if callable(retry_fn):
        item = retry_fn(reason=reason)
        if isawaitable(item):
            item = await item
        if isinstance(item, dict):
            item_dict = dict(item)
        else:
            item_dict = _queue_row_to_dict(item)
        try:
            _append_memory_admin_audit_event(
                manager,
                {
                    'action': 'retry_head',
                    'reason': reason,
                    'request_id': request_id,
                    'queue_head_request_id': str(item_dict.get('request_id') or '').strip(),
                    'result': 'ok',
                    'timestamp': now_iso(),
                },
            )
            item_dict['audit_logged'] = True
        except Exception as exc:
            item_dict['audit_logged'] = False
            item_dict['audit_error'] = str(exc or 'memory admin audit write failed').strip()
        return item_dict

    reader = getattr(manager, '_read_queue_requests', None)
    writer = getattr(manager, '_write_queue_requests', None)
    if not callable(reader) or not callable(writer):
        raise HTTPException(
            status_code=503,
            detail={
                'code': 'memory_admin_retry_unavailable',
                'message': 'memory queue retry is unavailable',
            },
        )

    async def _mutate_and_audit() -> dict[str, Any]:
        rows = reader()
        if isawaitable(rows):
            rows = await rows
        rows = list(rows or [])
        if not rows:
            raise HTTPException(
                status_code=409,
                detail={
                    'code': 'memory_admin_queue_empty',
                    'message': 'memory queue is empty',
                },
            )

        original_rows = list(rows)
        head = rows[0]
        head_data = _queue_row_to_dict(head)
        if str(head_data.get('status') or '').strip().lower() != 'processing':
            raise HTTPException(
                status_code=409,
                detail={
                    'code': 'memory_admin_retry_not_applicable',
                    'message': 'queue head is not in processing state',
                },
            )
        retry_after_cleared = bool(str(head_data.get('retry_after') or '').strip())
        rows[0] = _queue_row_with_update(head, retry_after='')
        write_result = writer(rows)
        if isawaitable(write_result):
            await write_result

        try:
            _append_memory_admin_audit_event(
                manager,
                {
                    'action': 'retry_head',
                    'reason': reason,
                    'request_id': request_id,
                    'queue_head_request_id': str(head_data.get('request_id') or '').strip(),
                    'result': 'ok',
                    'timestamp': now_iso(),
                },
            )
        except Exception as exc:
            rollback_result = writer(original_rows)
            if isawaitable(rollback_result):
                await rollback_result
            raise HTTPException(
                status_code=503,
                detail={
                    'code': 'memory_admin_audit_failed',
                    'message': 'memory admin audit write failed',
                },
            ) from exc

        return {
            'request_id': str(head_data.get('request_id') or '').strip(),
            'status': str(head_data.get('status') or '').strip(),
            'retry_after_cleared': retry_after_cleared,
            'last_error_text': str(head_data.get('last_error_text') or '').strip(),
            'reason': reason,
            'audit_logged': True,
        }

    lock = getattr(manager, '_io_lock', None)
    if hasattr(lock, '__enter__') and hasattr(lock, '__exit__'):
        with lock:
            return await _mutate_and_audit()
    return await _mutate_and_audit()


def _memory_read_error(*, code: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=503,
        detail={
            'code': str(code or '').strip(),
            'message': str(message or '').strip(),
        },
    )


@router.get('/memory/queue')
async def get_memory_queue(
    limit: int = Query(20, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    manager = _runtime_memory_manager()
    reader = getattr(manager, 'list_queue_page', None)
    if not callable(reader):
        raise HTTPException(status_code=503, detail='memory_queue_unavailable')
    try:
        payload = await reader(limit=limit, offset=offset)
    except Exception as exc:
        raise _memory_read_error(
            code='memory_queue_read_failed',
            message='记忆队列暂时不可读取，请稍后刷新。',
        ) from exc
    return {
        'ok': True,
        'items': list(payload.get('items') or []),
        'total': int(payload.get('total', 0) or 0),
        'has_more': bool(payload.get('has_more', False)),
    }


@router.get('/memory/processed')
async def get_memory_processed(
    limit: int = Query(20, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    manager = _runtime_memory_manager()
    reader = getattr(manager, 'list_processed_page', None)
    if not callable(reader):
        raise HTTPException(status_code=503, detail='memory_processed_unavailable')
    try:
        payload = await reader(limit=limit, offset=offset)
    except Exception as exc:
        raise _memory_read_error(
            code='memory_processed_read_failed',
            message='已处理记忆暂时不可读取，请稍后刷新。',
        ) from exc
    return {
        'ok': True,
        'items': list(payload.get('items') or []),
        'total': int(payload.get('total', 0) or 0),
        'has_more': bool(payload.get('has_more', False)),
    }


@router.get('/memory/notes/{ref}')
async def get_memory_note(ref: str):
    manager = _runtime_memory_manager()
    reader = getattr(manager, 'load_note', None)
    if not callable(reader):
        reader = getattr(manager, 'read_note', None)
    if not callable(reader):
        raise HTTPException(
            status_code=503,
            detail={
                'code': 'memory_note_unavailable',
                'message': '记忆 note 预览暂不可用，请稍后刷新。',
            },
        )
    normalized_ref = str(ref or '').strip()
    if not normalized_ref:
        raise HTTPException(
            status_code=400,
            detail={
                'code': 'memory_note_invalid_ref',
                'message': 'note ref is required',
            },
        )
    if not MEMORY_NOTE_REF_RE.fullmatch(normalized_ref):
        raise HTTPException(
            status_code=400,
            detail={
                'code': 'memory_note_invalid_ref',
                'message': 'note ref must match note_[a-z0-9_]+',
            },
        )
    try:
        body = reader(normalized_ref)
        if isawaitable(body):
            body = await body
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=404,
            detail={
                'code': 'memory_note_not_found',
                'message': '未找到对应的记忆 note。',
                'ref': normalized_ref,
            },
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail={
                'code': 'memory_note_invalid_ref',
                'message': str(exc) or 'note ref is required',
            },
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail={
                'code': 'memory_note_read_failed',
                'message': '读取记忆 note 失败，请稍后重试。',
            },
        ) from exc
    return {
        'ok': True,
        'item': {
            'ref': normalized_ref,
            'body': str(body or ''),
        },
    }


@router.post('/memory/admin/retry-head')
async def retry_memory_queue_head(request: Request, payload: dict | None = Body(default=None)):
    if not _memory_admin_mutations_enabled():
        raise _memory_admin_mutation_disabled()

    manager = _runtime_memory_manager()
    reason = str((payload or {}).get('reason') or 'manual').strip() or 'manual'
    request_id = str(request.headers.get('x-request-id') or '').strip()
    item = await _retry_memory_queue_head_with_fallback(
        manager,
        reason=reason,
        request_id=request_id,
    )
    return {'ok': True, 'item': item}


@router.post('/memory/dense-index/reset')
async def reset_memory_dense_index(payload: dict | None = Body(default=None)):
    manager = _runtime_memory_manager()
    reason = str((payload or {}).get('reason') or 'manual').strip() or 'manual'
    try:
        item = await manager.reset_dense_index(reason=reason)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {'ok': True, 'item': item}


@router.post('/memory/dense-index/rebuild')
async def rebuild_memory_dense_index(payload: dict | None = Body(default=None)):
    manager = _runtime_memory_manager()
    reason = str((payload or {}).get('reason') or 'manual').strip() or 'manual'
    try:
        item = await manager.rebuild_dense_index(reason=reason)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {'ok': True, 'item': item}


@router.get('/memory/runtime-stats')
async def get_memory_runtime_stats():
    agent = get_agent()
    service = getattr(agent, 'main_task_service', None)
    if service is None:
        raise HTTPException(status_code=503, detail='main_task_service_unavailable')
    await service.startup()

    async def _stats_for(manager: Any | None) -> dict[str, Any] | None:
        if manager is None:
            return None
        stats_fn = getattr(manager, 'stats', None)
        if not callable(stats_fn):
            return {'available': False}
        try:
            stats = await stats_fn()
        except Exception as exc:
            return {
                'available': True,
                'error': str(exc),
            }
        return {
            'available': True,
            'manager_type': type(manager).__name__,
            'stats': stats,
        }

    loop_manager = getattr(agent, 'memory_manager', None)
    service_manager = getattr(service, 'memory_manager', None)
    return {
        'ok': True,
        'item': {
            'same_object': loop_manager is service_manager,
            'loop_manager': await _stats_for(loop_manager),
            'service_manager': await _stats_for(service_manager),
        },
    }
