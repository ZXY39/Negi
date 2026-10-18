"""`load_skill_context` 三态回执：已删除 / 权限不允许 / 正常。

头部名单在两次重印之间会过期：被删的名字仍留在名单里，模型按 stale 名单来 load
是常态，回执必须说清"为什么读不了"——已删除与权限不允许是两种不同的处置。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from main.governance.models import SkillResourceRecord
from main.service.runtime_service import MainRuntimeService


def _skill_record(skill_id: str) -> SkillResourceRecord:
    return SkillResourceRecord(
        skill_id=skill_id,
        display_name=skill_id.title(),
        description=f"{skill_id} 的描述",
        enabled=True,
        source_path="skills/demo",
        skill_doc_path="skills/demo/SKILL.md",
    )


def _service_with(*, registry_skills: list[str], visible_skills: list[str]):
    service = object.__new__(MainRuntimeService)
    registry_records = {skill_id: _skill_record(skill_id) for skill_id in registry_skills}
    visible_records = [registry_records[skill_id] for skill_id in visible_skills if skill_id in registry_records]
    service.resource_registry = SimpleNamespace(
        get_skill_resource=lambda skill_id: registry_records.get(str(skill_id or '').strip()),
    )
    service.list_contract_visible_skill_resources = lambda *, actor_role, session_id: visible_records
    return service


def test_load_skill_context_reports_deleted_when_registry_has_no_record() -> None:
    service = _service_with(registry_skills=[], visible_skills=[])

    payload = service.load_skill_context_v2(actor_role="ceo", session_id="web:x", skill_id="ghost-skill")

    assert payload["ok"] is False
    assert payload["error"] == "skill_deleted"
    assert "已删除" in payload["message"]
    assert "ghost-skill" in payload["message"]


def test_load_skill_context_reports_permission_when_record_is_not_visible() -> None:
    service = _service_with(registry_skills=["locked-skill"], visible_skills=[])

    payload = service.load_skill_context_v2(actor_role="execution", session_id="web:x", skill_id="locked-skill")

    assert payload["ok"] is False
    assert payload["error"] == "skill_not_permitted"
    assert "权限不允许" in payload["message"]
    assert "execution" in payload["message"]


def test_load_skill_context_still_loads_visible_skill(tmp_path: Path) -> None:
    service = _service_with(registry_skills=["good-skill"], visible_skills=["good-skill"])
    # 正文路径不存在时内容为空但 load 成功——回执三态只管"读不了"的分支
    payload = service.load_skill_context_v2(actor_role="ceo", session_id="web:x", skill_id="good-skill")
    assert payload["ok"] is True
    assert payload["skill_id"] == "good-skill"
