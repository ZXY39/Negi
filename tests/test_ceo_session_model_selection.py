"""会话模型模式（模型链 / 指定模型）的契约测试：REST 端点 + 运行时固定模型解析。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from g3ku.runtime import web_ceo_sessions as wcs
from g3ku.runtime.api import ceo_sessions
from g3ku.runtime.frontdoor._ceo_support import CeoFrontDoorSupport
from g3ku.session.manager import SessionManager


def _build_app() -> FastAPI:
    app = FastAPI()
    app.include_router(ceo_sessions.router, prefix="/api")
    return app


def _config(models: dict[str, SimpleNamespace]) -> SimpleNamespace:
    return SimpleNamespace(get_managed_model=lambda key: models.get(str(key or "").strip()))


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(wcs, "workspace_path", lambda: tmp_path)
    manager = SessionManager(tmp_path)
    key = "web:ceo-model"
    session = manager.get_or_create(key)
    session.metadata = {"title": "模型模式测试会话"}
    manager.save(session)
    models = {
        "alpha": SimpleNamespace(key="alpha", enabled=True),
        "beta": SimpleNamespace(key="beta", enabled=True),
        "retired": SimpleNamespace(key="retired", enabled=False),
    }
    agent = SimpleNamespace(main_task_service=None)
    monkeypatch.setattr(
        ceo_sessions,
        "_sessions",
        lambda: (agent, manager, SimpleNamespace(get=lambda _k: None, remove=lambda _k: None), wcs.WebCeoStateStore(tmp_path)),
    )
    monkeypatch.setattr(ceo_sessions, "_live_config", lambda: _config(models))
    client = TestClient(_build_app())
    return SimpleNamespace(client=client, manager=manager, key=key, workspace=tmp_path)


def test_default_mode_is_chain(env):
    response = env.client.get(f"/api/ceo/sessions/{env.key}/model-selection")
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["mode"] == "chain"
    assert payload["model_key"] == ""
    assert payload["pinned_available"] is True
    # 默认态不落 metadata 键，避免给每个会话写恒等记录。
    assert wcs.SESSION_MODEL_SELECTION_KEY not in (env.manager.get_or_create(env.key).metadata or {})


def test_patch_pins_model_and_persists(env):
    response = env.client.patch(
        f"/api/ceo/sessions/{env.key}/model-selection",
        json={"mode": "model", "model_key": "beta"},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["mode"] == "model"
    assert payload["model_key"] == "beta"
    assert payload["pinned_available"] is True
    stored = env.manager.get_or_create(env.key).metadata
    assert wcs.ceo_session_pinned_model_key(stored) == "beta"
    # 重新读取（新进程视角）仍拿到固定模型。
    reloaded = SessionManager(env.workspace).get_or_create(env.key)
    assert wcs.ceo_session_pinned_model_key(reloaded.metadata) == "beta"


def test_patch_back_to_chain_clears_pin(env):
    env.client.patch(f"/api/ceo/sessions/{env.key}/model-selection", json={"mode": "model", "model_key": "alpha"})
    response = env.client.patch(f"/api/ceo/sessions/{env.key}/model-selection", json={"mode": "chain"})
    assert response.status_code == 200, response.text
    assert response.json()["mode"] == "chain"
    metadata = env.manager.get_or_create(env.key).metadata
    assert wcs.SESSION_MODEL_SELECTION_KEY not in metadata


def test_patch_unknown_model_404(env):
    response = env.client.patch(
        f"/api/ceo/sessions/{env.key}/model-selection",
        json={"mode": "model", "model_key": "missing"},
    )
    assert response.status_code == 404
    assert response.json()["detail"] == "model_key_not_found"


def test_patch_disabled_model_409(env):
    response = env.client.patch(
        f"/api/ceo/sessions/{env.key}/model-selection",
        json={"mode": "model", "model_key": "retired"},
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "model_key_disabled"


def test_patch_requires_key_in_model_mode(env):
    response = env.client.patch(f"/api/ceo/sessions/{env.key}/model-selection", json={"mode": "model"})
    assert response.status_code == 400
    assert response.json()["detail"] == "model_key_required"


def test_patch_rejects_unknown_mode(env):
    response = env.client.patch(f"/api/ceo/sessions/{env.key}/model-selection", json={"mode": "auto"})
    assert response.status_code == 400
    assert response.json()["detail"] == "invalid_model_selection_mode"


def test_patch_rejects_key_without_model_mode(env):
    response = env.client.patch(
        f"/api/ceo/sessions/{env.key}/model-selection",
        json={"mode": "chain", "model_key": "alpha"},
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "model_key_requires_model_mode"


def _seed_channel_session(manager, key: str = "ext:qq-demo") -> None:
    session = manager.get_or_create(key)
    session.metadata = {"title": "渠道会话", "handled_terminal_dedupe_keys": ["task-terminal:seed"]}
    manager.save(session)


def test_channel_session_model_selection_read_write(env):
    _seed_channel_session(env.manager)
    response = env.client.patch(
        "/api/ceo/sessions/ext:qq-demo/model-selection",
        json={"mode": "model", "model_key": "alpha"},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["session_id"] == "ext:qq-demo"
    assert payload["mode"] == "model"
    assert payload["model_key"] == "alpha"
    reloaded = SessionManager(env.workspace).get_or_create("ext:qq-demo")
    assert wcs.ceo_session_pinned_model_key(reloaded.metadata) == "alpha"
    # 渠道侧自有元数据不被模型模式写入破坏。
    assert reloaded.metadata["handled_terminal_dedupe_keys"] == ["task-terminal:seed"]
    assert reloaded.metadata["title"] == "渠道会话"
    assert env.client.get("/api/ceo/sessions/ext:qq-demo/model-selection").json()["mode"] == "model"


def test_channel_session_switch_back_to_chain(env):
    _seed_channel_session(env.manager)
    env.client.patch("/api/ceo/sessions/ext:qq-demo/model-selection", json={"mode": "model", "model_key": "alpha"})
    response = env.client.patch("/api/ceo/sessions/ext:qq-demo/model-selection", json={"mode": "chain"})
    assert response.status_code == 200, response.text
    assert response.json()["mode"] == "chain"
    reloaded = SessionManager(env.workspace).get_or_create("ext:qq-demo")
    assert wcs.SESSION_MODEL_SELECTION_KEY not in reloaded.metadata
    assert reloaded.metadata["handled_terminal_dedupe_keys"] == ["task-terminal:seed"]


def test_unknown_channel_session_404(env):
    response = env.client.get("/api/ceo/sessions/ext:qq-missing/model-selection")
    assert response.status_code == 404


def test_pinned_availability_false_after_model_disappears(env, monkeypatch):
    env.client.patch(f"/api/ceo/sessions/{env.key}/model-selection", json={"mode": "model", "model_key": "beta"})
    monkeypatch.setattr(ceo_sessions, "_live_config", lambda: _config({}))
    payload = env.client.get(f"/api/ceo/sessions/{env.key}/model-selection").json()
    # 固定模型被删除：存储保留，但可用性报 false，运行时回退模型链。
    assert payload["mode"] == "model"
    assert payload["model_key"] == "beta"
    assert payload["pinned_available"] is False


def _support(tmp_path, *, pinned: str = "", models: dict[str, SimpleNamespace] | None = None) -> CeoFrontDoorSupport:
    manager = SessionManager(tmp_path)
    key = "web:ceo-pin"
    session = manager.get_or_create(key)
    session.metadata = {"title": "t"}
    if pinned:
        session.metadata[wcs.SESSION_MODEL_SELECTION_KEY] = {"mode": "model", "model_key": pinned}
    manager.save(session)
    loop = SimpleNamespace(
        sessions=manager,
        app_config=_config(models if models is not None else {"alpha": SimpleNamespace(key="alpha", enabled=True)}),
        provider_name="openai",
        model="gpt-test",
    )
    return CeoFrontDoorSupport(loop=loop), key


def test_runtime_pins_session_model(tmp_path, monkeypatch):
    monkeypatch.setattr(CeoFrontDoorSupport, "_resolve_ceo_model_refs", lambda self: ["chain-a", "chain-b"])
    support, key = _support(tmp_path, pinned="alpha")
    assert support._resolve_ceo_model_refs_for_session(key) == ["alpha"]


def test_runtime_falls_back_when_pinned_model_deleted(tmp_path, monkeypatch):
    monkeypatch.setattr(CeoFrontDoorSupport, "_resolve_ceo_model_refs", lambda self: ["chain-a", "chain-b"])
    support, key = _support(tmp_path, pinned="alpha", models={})
    assert support._resolve_ceo_model_refs_for_session(key) == ["chain-a", "chain-b"]


def test_runtime_falls_back_when_pinned_model_disabled(tmp_path, monkeypatch):
    monkeypatch.setattr(CeoFrontDoorSupport, "_resolve_ceo_model_refs", lambda self: ["chain-a"])
    models = {"alpha": SimpleNamespace(key="alpha", enabled=False)}
    support, key = _support(tmp_path, pinned="alpha", models=models)
    assert support._resolve_ceo_model_refs_for_session(key) == ["chain-a"]


def test_runtime_without_session_key_uses_chain(tmp_path, monkeypatch):
    monkeypatch.setattr(CeoFrontDoorSupport, "_resolve_ceo_model_refs", lambda self: ["chain-a"])
    support, _key = _support(tmp_path, pinned="alpha")
    assert support._resolve_ceo_model_refs_for_session(None) == ["chain-a"]
    assert support._resolve_ceo_model_refs_for_session("web:ceo-unknown") == ["chain-a"]


def test_model_selection_normalization():
    assert wcs.normalize_model_selection({"mode": "model", "model_key": " a "}) == {"mode": "model", "model_key": "a"}
    # 指定模式缺 key 视为模型链，不产生半配置状态。
    assert wcs.normalize_model_selection({"mode": "model"}) == {"mode": "chain", "model_key": ""}
    assert wcs.normalize_model_selection(None) == {"mode": "chain", "model_key": ""}
    assert wcs.ceo_session_pinned_model_key({"model_selection": {"mode": "model", "model_key": "a"}}) == "a"
    assert wcs.ceo_session_pinned_model_key({"model_selection": {"mode": "chain"}}) == ""
    assert wcs.ceo_session_pinned_model_key(None) == ""
