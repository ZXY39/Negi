from __future__ import annotations

from pathlib import Path

from g3ku.resources import ResourceManager
from main.governance.models import SkillResourceRecord, ToolRecord
from main.governance.resource_bridge import build_skill_resources, build_tool_records
from main.protocol import now_iso

IMPLICIT_TOOL_ROLE_BACKFILL_META_KEY = 'implicit_tool_role_backfill_v1_applied'


class MainRuntimeResourceRegistry:
    def __init__(self, *, workspace_root: Path, store, resource_manager: ResourceManager | None = None):
        self._workspace_root = Path(workspace_root)
        self._store = store
        self._resource_manager = resource_manager

    def bind_resource_manager(self, resource_manager: ResourceManager) -> None:
        self._resource_manager = resource_manager

    def refresh(self) -> tuple[list[SkillResourceRecord], list[ToolRecord]]:
        if self._resource_manager is None:
            return [], []
        self._resource_manager.reload_now(trigger='main_runtime')
        return self.refresh_from_current_resources()

    def refresh_from_current_resources(self) -> tuple[list[SkillResourceRecord], list[ToolRecord]]:
        if self._resource_manager is None:
            return [], []
        existing_skills = {item.skill_id: item for item in self._store.list_skill_resources()}
        existing_tools = {item.tool_id: item for item in self._store.list_tools()}
        apply_legacy_empty_role_backfill = self._should_backfill_legacy_empty_tool_roles()
        discovered_skills = build_skill_resources(self._resource_manager.list_skills(), default_risk_level='medium', exclude_names=set())
        merged_skills = [self._merge_skill(existing_skills.get(item.skill_id), item) for item in discovered_skills]
        merged_tools = self._merge_tools(
            existing_tools=existing_tools,
            discovered=build_tool_records(self._resource_manager.list_tools()),
            apply_legacy_empty_role_backfill=apply_legacy_empty_role_backfill,
        )
        refreshed_at = now_iso()
        self._store.replace_skill_resources(merged_skills, updated_at=refreshed_at)
        self._store.replace_tools(merged_tools, updated_at=refreshed_at)
        if apply_legacy_empty_role_backfill:
            self._mark_legacy_empty_tool_roles_backfilled()
        return merged_skills, merged_tools

    def list_skill_resources(self) -> list[SkillResourceRecord]:
        return self._store.list_skill_resources()

    def get_skill_resource(self, skill_id: str) -> SkillResourceRecord | None:
        return self._store.get_skill_resource(skill_id)

    def list_tools(self) -> list[ToolRecord]:
        return self._store.list_tools()

    def get_tool(self, tool_id: str) -> ToolRecord | None:
        return self._store.get_tool(tool_id)

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

    def _merge_tools(
        self,
        *,
        existing_tools: dict[str, ToolRecord],
        discovered: list[ToolRecord],
        apply_legacy_empty_role_backfill: bool = False,
    ) -> list[ToolRecord]:
        merged: list[ToolRecord] = []
        for record in sorted(discovered, key=lambda item: item.tool_id):
            existing = existing_tools.get(record.tool_id)
            if existing is None:
                merged.append(record)
                continue
            persisted_roles = list(existing.allowed_roles)
            if apply_legacy_empty_role_backfill and not persisted_roles and list(record.allowed_roles):
                persisted_roles = list(record.allowed_roles)
            merged.append(
                record.model_copy(
                    update={
                        'enabled': existing.enabled,
                        'allowed_roles': persisted_roles,
                        'metadata': {
                            **dict(getattr(existing, 'metadata', {}) or {}),
                            **dict(getattr(record, 'metadata', {}) or {}),
                        },
                    }
                )
            )
        return merged

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
