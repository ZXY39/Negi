from __future__ import annotations

from pathlib import Path

from g3ku.resources import ResourceManager
from main.governance.action_mapper import DEFAULT_TOOL_FAMILIES
from main.governance.models import SkillResourceRecord, ToolActionRecord, ToolFamilyRecord
from main.governance.resource_bridge import build_skill_resources, build_tool_families
from main.governance.roles import normalize_public_allowed_roles
from main.protocol import now_iso

MANUAL_ACTION_TOOL_NAMES = {'patch_apply'}
IMPLICIT_TOOL_ROLE_BACKFILL_META_KEY = 'implicit_tool_role_backfill_v1_applied'


def _primary_executor_name(actions: list[ToolActionRecord]) -> str:
    for action in actions:
        for executor_name in action.executor_names or []:
            name = str(executor_name or '').strip()
            if name:
                return name
    return ''


class MainRuntimeResourceRegistry:
    def __init__(self, *, workspace_root: Path, store, resource_manager: ResourceManager | None = None):
        self._workspace_root = Path(workspace_root)
        self._store = store
        self._resource_manager = resource_manager

    def bind_resource_manager(self, resource_manager: ResourceManager) -> None:
        self._resource_manager = resource_manager

    def refresh(self) -> tuple[list[SkillResourceRecord], list[ToolFamilyRecord]]:
        if self._resource_manager is None:
            return [], []
        self._resource_manager.reload_now(trigger='main_runtime')
        return self.refresh_from_current_resources()

    def refresh_from_current_resources(self) -> tuple[list[SkillResourceRecord], list[ToolFamilyRecord]]:
        if self._resource_manager is None:
            return [], []
        existing_skills = {item.skill_id: item for item in self._store.list_skill_resources()}
        existing_tools = {item.tool_id: item for item in self._store.list_tool_families()}
        apply_legacy_empty_role_backfill = self._should_backfill_legacy_empty_tool_roles()
        discovered_skills = build_skill_resources(self._resource_manager.list_skills(), default_risk_level='medium', exclude_names=set())
        merged_skills = [self._merge_skill(existing_skills.get(item.skill_id), item) for item in discovered_skills]
        merged_tools = self._merge_tool_families(
            existing_tools=existing_tools,
            discovered=build_tool_families(self._resource_manager.list_tools()),
            apply_legacy_empty_role_backfill=apply_legacy_empty_role_backfill,
        )
        refreshed_at = now_iso()
        self._store.replace_skill_resources(merged_skills, updated_at=refreshed_at)
        self._store.replace_tool_families(merged_tools, updated_at=refreshed_at)
        if apply_legacy_empty_role_backfill:
            self._mark_legacy_empty_tool_roles_backfilled()
        return merged_skills, merged_tools

    def list_skill_resources(self) -> list[SkillResourceRecord]:
        return self._store.list_skill_resources()

    def get_skill_resource(self, skill_id: str) -> SkillResourceRecord | None:
        return self._store.get_skill_resource(skill_id)

    def list_tool_families(self) -> list[ToolFamilyRecord]:
        return self._store.list_tool_families()

    def get_tool_family(self, tool_id: str) -> ToolFamilyRecord | None:
        return self._store.get_tool_family(tool_id)

    def skill_file_map(self, skill_id: str) -> dict[str, Path]:
        record = self.get_skill_resource(skill_id)
        if record is None:
            return {}
        mapping = {'skill_doc': Path(record.skill_doc_path)}
        if record.manifest_path:
            mapping['manifest'] = Path(record.manifest_path)
        if record.openai_yaml_path:
            mapping['openai_yaml'] = Path(record.openai_yaml_path)
        return mapping

    def _merge_skill(self, existing: SkillResourceRecord | None, record: SkillResourceRecord) -> SkillResourceRecord:
        if existing is None:
            return record
        return record.model_copy(
            update={
                'enabled': existing.enabled,
                'allowed_roles': list(existing.allowed_roles) if existing.allowed_roles is not None else list(record.allowed_roles),
                'editable_files': list(existing.editable_files or record.editable_files),
                'risk_level': existing.risk_level or record.risk_level,
            }
        )

    def _merge_tool_families(
        self,
        *,
        existing_tools: dict[str, ToolFamilyRecord],
        discovered: list[ToolFamilyRecord],
        apply_legacy_empty_role_backfill: bool = False,
    ) -> list[ToolFamilyRecord]:
        families = {item.tool_id: item for item in discovered}
        discovered_tool_names = {
            executor_name
            for family in discovered
            for action in family.actions
            for executor_name in action.executor_names
            if executor_name
        }
        self._inject_manual_actions(families, discovered_tool_names=discovered_tool_names)
        merged: list[ToolFamilyRecord] = []
        for tool_id, record in sorted(families.items(), key=lambda item: item[0]):
            existing = existing_tools.get(tool_id)
            if existing is None:
                merged.append(record)
                continue
            merged_actions: list[ToolActionRecord] = []
            for action in record.actions:
                old = next((candidate for candidate in existing.actions if candidate.action_id == action.action_id), None)
                if old is None:
                    merged_actions.append(action)
                else:
                    persisted_roles = list(old.allowed_roles)
                    if (
                        apply_legacy_empty_role_backfill
                        and not persisted_roles
                        and list(action.allowed_roles)
                    ):
                        persisted_roles = list(action.allowed_roles)
                    merged_actions.append(action.model_copy(update={'allowed_roles': persisted_roles}))
            merged.append(
                record.model_copy(
                    update={
                        'enabled': existing.enabled,
                        'actions': merged_actions,
                        'metadata': {
                            **dict(getattr(existing, 'metadata', {}) or {}),
                            **dict(getattr(record, 'metadata', {}) or {}),
                        },
                        'primary_executor_name': _primary_executor_name(merged_actions),
                    }
                )
            )
        return merged

    def _inject_manual_actions(self, families: dict[str, ToolFamilyRecord], *, discovered_tool_names: set[str]) -> None:
        for tool_name, governance in DEFAULT_TOOL_FAMILIES.items():
            if tool_name not in discovered_tool_names and tool_name not in MANUAL_ACTION_TOOL_NAMES:
                continue
            tool_id = str(governance.get('tool_id') or tool_name)
            family = families.get(tool_id)
            if family is None:
                continue
            action_map = {action.action_id: action for action in family.actions}
            for action in governance.get('actions') or []:
                action_id = str(action.get('id') or '')
                if not action_id:
                    continue
                existing = action_map.get(action_id)
                if existing is None:
                    action_map[action_id] = ToolActionRecord(
                        action_id=action_id,
                        label=str(action.get('label') or action_id),
                        risk_level=str(action.get('risk_level') or 'medium'),
                        destructive=bool(action.get('destructive', False)),
                        allowed_roles=normalize_public_allowed_roles(
                            [str(role) for role in list(action.get('allowed_roles') or [])]
                        ),
                        executor_names=[tool_name],
                    )
                else:
                    executors = list(existing.executor_names)
                    if tool_name not in executors:
                        executors.append(tool_name)
                    action_map[action_id] = existing.model_copy(update={'executor_names': executors})
            actions = list(action_map.values())
            families[tool_id] = family.model_copy(
                update={
                    'actions': actions,
                    'primary_executor_name': _primary_executor_name(actions),
                }
            )

    def _should_backfill_legacy_empty_tool_roles(self) -> bool:
        getter = getattr(self._store, 'get_bool_meta', None)
        if not callable(getter):
            return False
        try:
            return not bool(getter(IMPLICIT_TOOL_ROLE_BACKFILL_META_KEY, default=False))
        except Exception:
            return False

    def _mark_legacy_empty_tool_roles_backfilled(self) -> None:
        setter = getattr(self._store, 'set_bool_meta', None)
        if not callable(setter):
            return
        try:
            setter(IMPLICIT_TOOL_ROLE_BACKFILL_META_KEY, True)
        except Exception:
            return
