"""skill 策略的角色值：写侧在门口拒，读侧逐行降级并点名被丢掉的值。

PUT /resources/skills/{id}/policy 把 allowed_roles 原样 `model_copy` 进治理库，绕过了
pydantic 的 Literal；而读侧 `GovernanceStore._parse` 会把单行的 ValueError 顶给整张列表——
一个脏角色能让所有 skill 读取接口 500，且刷新车道走同一个 `_parse`，落库后无法自愈。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from loguru import logger

from main.api import admin_rest
from main.governance.models import SkillResourceRecord
from main.governance.store import GovernanceStore


def _record(skill_id: str = "demo_skill") -> SkillResourceRecord:
    return SkillResourceRecord(
        skill_id=skill_id,
        display_name="Demo",
        description="demo skill",
        source_path=f"/skills/{skill_id}",
        skill_doc_path=f"/skills/{skill_id}/SKILL.md",
        allowed_roles=["ceo"],
    )


class _PolicySpy:
    """只记 PUT 传下来什么，不复刻生产策略逻辑。"""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def update_skill_policy(self, skill_id: str, *, session_id: str, enabled=None, allowed_roles=None):
        self.calls.append(
            {
                "skill_id": skill_id,
                "session_id": session_id,
                "enabled": enabled,
                "allowed_roles": allowed_roles,
            }
        )
        return _record(skill_id)


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch):
    spy = _PolicySpy()
    monkeypatch.setattr(admin_rest, "get_agent", lambda: SimpleNamespace(main_task_service=spy))
    app = FastAPI()
    app.include_router(admin_rest.router, prefix="/api")
    return TestClient(app), spy


def test_policy_put_normalizes_known_aliases(client) -> None:
    http, spy = client

    response = http.put(
        "/api/resources/skills/demo_skill/policy",
        json={"allowed_roles": ["checker", "ceo", "ceo"]},
    )

    assert response.status_code == 200
    # checker 是 inspection 的旧别名；去重按现行判据，不在端点里另算一份
    assert spy.calls[0]["allowed_roles"] == ["inspection", "ceo"]


def test_policy_put_rejects_unsupported_role_and_writes_nothing(client) -> None:
    http, spy = client

    response = http.put(
        "/api/resources/skills/demo_skill/policy",
        json={"allowed_roles": ["ceo", "hacker"]},
    )

    assert response.status_code == 400
    assert "hacker" in response.json()["detail"]
    assert spy.calls == []


def test_policy_put_keeps_absent_roles_untouched(client) -> None:
    http, spy = client

    response = http.put("/api/resources/skills/demo_skill/policy", json={"enabled": False})

    assert response.status_code == 200
    assert spy.calls[0]["allowed_roles"] is None
    assert spy.calls[0]["enabled"] is False


def test_store_reads_a_dirty_row_without_breaking_the_list(tmp_path) -> None:
    store = GovernanceStore(tmp_path / "governance.sqlite3")
    dirty = _record("dirty_skill").model_copy(update={"allowed_roles": ["ceo", "hacker"]})
    store.upsert_skill_resource(dirty, updated_at="2026-10-17T00:00:00+08:00")
    store.upsert_skill_resource(_record("clean_skill"), updated_at="2026-10-17T00:00:00+08:00")

    try:
        rows = {row.skill_id: row for row in store.list_skill_resources()}
    finally:
        store.close()

    assert set(rows) == {"clean_skill", "dirty_skill"}
    assert rows["dirty_skill"].allowed_roles == ["ceo"]
    assert rows["clean_skill"].allowed_roles == ["ceo"]


def test_dropped_roles_are_reported_with_their_row(tmp_path) -> None:
    store = GovernanceStore(tmp_path / "governance.sqlite3")
    dirty = _record("dirty_skill").model_copy(update={"allowed_roles": ["ghost-role"]})
    store.upsert_skill_resource(dirty, updated_at="2026-10-17T00:00:00+08:00")

    seen: list[str] = []
    sink_id = logger.add(lambda message: seen.append(str(message)), level="WARNING")
    try:
        rows = list(store.list_skill_resources())
    finally:
        logger.remove(sink_id)
        store.close()

    # 全部被丢掉 ⇒ 技能面按现行契约回落成 ceo+execution，同时必须留下点名记录
    assert rows[0].allowed_roles == ["ceo", "execution"]
    assert any("dirty_skill" in text and "ghost-role" in text for text in seen)
