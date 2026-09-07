from __future__ import annotations

import errno
import json
import os
import re
import secrets
import shutil
from contextlib import contextmanager
from inspect import isawaitable
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, HTTPException, Query, Request

from g3ku.config.loader import load_config, save_config
from g3ku.config.model_manager import _UNSET, VALID_SCOPES, ModelManager
from g3ku.config.schema import Config, ExternalApiTokenConfig, _normalize_external_token_id
from g3ku.resources import get_shared_resource_manager
from g3ku.resources.models import ResourceKind
from g3ku.runtime.core_tools import configured_core_tools, resolve_core_tool_targets
from g3ku.shells.web import get_agent, is_no_ceo_model_configured_error, refresh_web_agent_runtime
from g3ku.utils.retry_keywords import split_retry_keywords
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

MEMORY_NOTE_REF_RE = re.compile(r"^note_[a-z0-9_]+$")
GOVERNANCE_MODE_META_KEY = 'ceo_frontdoor_regulatory_mode_enabled'
GOVERNANCE_MODE_UPDATED_AT_META_KEY = 'ceo_frontdoor_regulatory_mode_enabled:updated_at'



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
            'message': '模型ID已存在，请使用其他模型ID。',
            'data': {'key': duplicate_key},
        }
    return message




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
        return family.model_copy(update={'is_core': family.tool_id in resolution.family_ids, 'metadata': metadata})

    def list_tool_resources(self) -> list[Any]:
        return [self._decorate_tool_family(item) for item in self.resource_registry.list_tool_families()]

    def get_tool_family(self, tool_id: str):
        return self._decorate_tool_family(self._raw_tool_family(tool_id))

    def get_governance_mode(self) -> dict[str, Any]:
        return {
            'enabled': self._governance_store.get_bool_meta(GOVERNANCE_MODE_META_KEY, default=False),
            'updated_at': str(self._governance_store.get_meta(GOVERNANCE_MODE_UPDATED_AT_META_KEY) or ''),
        }

    def update_governance_mode(self, *, enabled: bool) -> dict[str, Any]:
        self._governance_store.set_bool_meta(GOVERNANCE_MODE_META_KEY, bool(enabled))
        updated_at = now_iso()
        self._governance_store.set_meta(GOVERNANCE_MODE_UPDATED_AT_META_KEY, updated_at)
        return {
            'enabled': bool(enabled),
            'updated_at': updated_at,
        }

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


async def _refresh_runtime(reason: str, *, force_memory_sync: bool = False) -> None:
    web_refreshed = False
    try:
        await refresh_web_agent_runtime(force=True, reason=reason, force_memory_sync=force_memory_sync)
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


async def _refresh_runtime_after_save(reason: str, *, force_memory_sync: bool = False) -> dict[str, Any]:
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
        await refresh_web_agent_runtime(force=True, reason=status['reason'], force_memory_sync=force_memory_sync)
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


def _mask_external_api_token(token: str) -> str:
    raw = str(token or '')
    if not raw:
        return ''
    if len(raw) < 12:
        return '•' * len(raw)
    return f'{raw[:4]}…{raw[-4:]}'


def _external_api_payload(cfg: Config) -> dict[str, Any]:
    external_api = cfg.external_api
    items: list[dict[str, Any]] = []
    for bridge_id, entry in dict(getattr(external_api, 'tokens', None) or {}).items():
        token = str(getattr(entry, 'token', '') or '')
        items.append(
            {
                'bridge_id': str(bridge_id),
                'label': str(getattr(entry, 'label', '') or ''),
                'enabled': bool(getattr(entry, 'enabled', True)),
                'has_token': bool(token),
                'token_masked': _mask_external_api_token(token),
            }
        )
    items.sort(key=lambda item: item['bridge_id'])
    return {
        'enabled': bool(getattr(external_api, 'enabled', False)),
        'event_buffer_size': int(getattr(external_api, 'event_buffer_size', 0) or 0),
        'items': items,
    }


@router.get('/external-api/settings')
async def get_external_api_settings():
    cfg = load_config()
    return {'ok': True, **_external_api_payload(cfg)}


@router.put('/external-api/settings')
async def update_external_api_settings(payload: dict | None = Body(default=None)):
    body = payload if isinstance(payload, dict) else {}
    cfg = load_config()
    if 'enabled' in body:
        cfg.external_api.enabled = bool(body.get('enabled'))
    if 'eventBufferSize' in body or 'event_buffer_size' in body:
        raw = body.get('eventBufferSize', body.get('event_buffer_size'))
        try:
            cfg.external_api.event_buffer_size = max(1, int(raw))
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail={'code': 'invalid_event_buffer_size'})
    save_config(cfg)
    await _refresh_runtime_after_save('admin_external_api_settings_update')
    return {'ok': True, **_external_api_payload(cfg)}


@router.post('/external-api/tokens')
async def create_external_api_token(payload: dict | None = Body(default=None)):
    body = payload if isinstance(payload, dict) else {}
    raw_bridge_id = str(body.get('bridge_id') or body.get('bridgeId') or '').strip()
    if not raw_bridge_id:
        raise HTTPException(status_code=400, detail={'code': 'bridge_id_required', 'message': '需要桥接标识（bridge_id）'})
    bridge_id = _normalize_external_token_id(raw_bridge_id)
    cfg = load_config()
    tokens = cfg.external_api.tokens or {}
    if bridge_id in tokens:
        raise HTTPException(status_code=409, detail={'code': 'bridge_id_exists', 'message': f'桥接标识 {bridge_id} 已存在'})
    custom_token = str(body.get('token') or '').strip()
    if custom_token:
        if any(str(entry.token or '') == custom_token for entry in tokens.values()):
            raise HTTPException(status_code=409, detail={'code': 'token_exists', 'message': '该 token 已被其他桥接使用，请更换'})
        token = custom_token
    else:
        existing_tokens = {str(entry.token or '') for entry in tokens.values()}
        token = secrets.token_urlsafe(32)
        while token in existing_tokens:
            token = secrets.token_urlsafe(32)
    cfg.external_api.tokens[bridge_id] = ExternalApiTokenConfig(
        token=token,
        label=str(body.get('label') or '').strip(),
        enabled=True,
    )
    save_config(cfg)
    await _refresh_runtime_after_save('admin_external_api_token_create')
    # The plaintext token is returned exactly once; later reads only expose a mask.
    return {'ok': True, 'bridge_id': bridge_id, 'token': token, **_external_api_payload(cfg)}


@router.patch('/external-api/tokens/{bridge_id}')
async def update_external_api_token(bridge_id: str, payload: dict | None = Body(default=None)):
    body = payload if isinstance(payload, dict) else {}
    cfg = load_config()
    key = _normalize_external_token_id(bridge_id)
    entry = (cfg.external_api.tokens or {}).get(key)
    if entry is None:
        raise HTTPException(status_code=404, detail={'code': 'external_token_not_found'})
    if 'label' in body:
        entry.label = str(body.get('label') or '').strip()
    if 'enabled' in body:
        entry.enabled = bool(body.get('enabled'))
    revealed = None
    if bool(body.get('regenerate')):
        revealed = secrets.token_urlsafe(32)
        entry.token = revealed
    save_config(cfg)
    await _refresh_runtime_after_save('admin_external_api_token_update')
    response = {'ok': True, 'bridge_id': key, **_external_api_payload(cfg)}
    if revealed is not None:
        response['token'] = revealed
    return response


@router.delete('/external-api/tokens/{bridge_id}')
async def delete_external_api_token(bridge_id: str):
    cfg = load_config()
    key = _normalize_external_token_id(bridge_id)
    if key not in (cfg.external_api.tokens or {}):
        raise HTTPException(status_code=404, detail={'code': 'external_token_not_found'})
    del cfg.external_api.tokens[key]
    save_config(cfg)
    await _refresh_runtime_after_save('admin_external_api_token_delete')
    return {'ok': True, **_external_api_payload(cfg)}


def _qq_bot_payload(cfg: Config) -> dict[str, Any]:
    q = cfg.qq_bot
    secret = str(getattr(q, 'app_secret', '') or '')
    return {
        'enabled': bool(getattr(q, 'enabled', False)),
        'app_id': str(getattr(q, 'app_id', '') or ''),
        'app_secret_masked': _mask_external_api_token(secret),
        'has_secret': bool(secret),
        'sandbox': bool(getattr(q, 'sandbox', False)),
    }


def _qq_bot_service_state() -> dict[str, Any]:
    from g3ku.shells.web import qq_official_service_status

    try:
        return qq_official_service_status()
    except Exception:
        return {'state': 'stopped', 'detail': ''}


@router.get('/qq-bot/settings')
async def get_qq_bot_settings():
    cfg = load_config()
    return {'ok': True, **_qq_bot_payload(cfg), 'service': _qq_bot_service_state()}


@router.put('/qq-bot/settings')
async def update_qq_bot_settings(payload: dict | None = Body(default=None)):
    body = payload if isinstance(payload, dict) else {}
    cfg = load_config()
    q = cfg.qq_bot
    if 'enabled' in body:
        q.enabled = bool(body.get('enabled'))
    if 'appId' in body or 'app_id' in body:
        q.app_id = str(body.get('appId', body.get('app_id')) or '').strip()
    if 'sandbox' in body:
        q.sandbox = bool(body.get('sandbox'))
    if 'appSecret' in body or 'app_secret' in body:
        raw_secret = str(body.get('appSecret', body.get('app_secret')) or '').strip()
        if raw_secret:
            q.app_secret = raw_secret
    save_config(cfg)
    await _refresh_runtime_after_save('admin_qq_bot_settings_update')
    return {'ok': True, **_qq_bot_payload(cfg), 'service': _qq_bot_service_state()}


@router.get('/qq-bot/status')
async def get_qq_bot_status():
    return {'ok': True, 'service': _qq_bot_service_state()}


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
            retry_on=split_retry_keywords(payload.get('retry_on')) or None,
            retry_count=raw_retry_count,
            description=str(payload.get('description') or ''),
            name=str(payload.get('name') or '').strip(),
            context_window_tokens=(
                payload.get('context_window_tokens')
                if 'context_window_tokens' in payload
                else payload.get('contextWindowTokens')
            ),
            image_multimodal_enabled=(
                payload.get('image_multimodal_enabled')
                if 'image_multimodal_enabled' in payload
                else payload.get('imageMultimodalEnabled', False)
            ),
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
                split_retry_keywords(body.get('retry_on'))
                if 'retry_on' in body
                else split_retry_keywords(body.get('retryOn'))
                if 'retryOn' in body
                else _UNSET
            ),
            retry_count=raw_retry_count if ('retry_count' in body or 'retryCount' in body) else _UNSET,
            description=_pick('description'),
            name=_pick('name'),
            context_window_tokens=_pick('context_window_tokens', 'contextWindowTokens'),
            image_multimodal_enabled=_pick('image_multimodal_enabled', 'imageMultimodalEnabled'),
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


@router.post('/llm/drafts/models')
async def list_llm_draft_models(payload: dict = Body(...)):
    try:
        result = await _llm_facade().list_draft_models(payload)
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


@router.post('/llm/bindings/{model_key:path}/rename')
async def rename_llm_binding(model_key: str, payload: dict = Body(...)):
    manager = ModelManager.load()
    try:
        new_key = str((payload or {}).get('key') or '').strip()
        item = manager.rename_model(model_key, new_key)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    runtime_refresh = await _refresh_runtime_after_save('admin_llm_binding_rename')
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


@router.get('/resources/tools/governance-mode')
async def get_governance_mode():
    with _resource_service() as service:
        return {'ok': True, 'item': dict(service.get_governance_mode() or {})}


@router.put('/resources/tools/governance-mode')
async def update_governance_mode(payload: dict = Body(...)):
    with _resource_service() as service:
        return {'ok': True, 'item': dict(service.update_governance_mode(enabled=bool(payload.get('enabled'))) or {})}


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


@router.get('/memory/current')
async def get_current_memories():
    manager = _runtime_memory_manager()
    reader = getattr(manager, 'list_current_memories', None)
    if not callable(reader):
        raise HTTPException(status_code=503, detail='memory_current_unavailable')
    try:
        items = reader()
        if isawaitable(items):
            items = await items
    except Exception as exc:
        raise _memory_read_error(
            code='memory_current_read_failed',
            message='当前记忆暂时不可读取，请稍后刷新。',
        ) from exc
    return {
        'ok': True,
        'items': list(items or []),
        'total': len(list(items or [])),
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
