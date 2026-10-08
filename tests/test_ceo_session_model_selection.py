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


def test_session_chain_read_write_round_trip(env):
    response = env.client.patch(
        f"/api/ceo/sessions/{env.key}/model-selection",
        json={"mode": "chain", "model_keys": ["beta", "alpha", "beta", " "]},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["mode"] == "chain"
    assert payload["model_keys"] == ["beta", "alpha"]
    assert payload["is_session_chain"] is True
    assert payload["chain_unavailable_keys"] == []
    stored = env.manager.get_or_create(env.key).metadata
    assert wcs.ceo_session_model_chain_keys(stored) == ["beta", "alpha"]
    reloaded = SessionManager(env.workspace).get_or_create(env.key)
    assert wcs.ceo_session_model_chain_keys(reloaded.metadata) == ["beta", "alpha"]


def test_clearing_session_chain_returns_to_global(env):
    env.client.patch(
        f"/api/ceo/sessions/{env.key}/model-selection",
        json={"mode": "chain", "model_keys": ["beta"]},
    )
    response = env.client.patch(f"/api/ceo/sessions/{env.key}/model-selection", json={"mode": "chain"})
    assert response.status_code == 200, response.text
    assert response.json()["model_keys"] == []
    assert response.json()["is_session_chain"] is False
    # 恒等态不落 metadata 键，与默认态同一口径。
    assert wcs.SESSION_MODEL_SELECTION_KEY not in (env.manager.get_or_create(env.key).metadata or {})


def test_session_chain_names_the_invalid_key(env):
    response = env.client.patch(
        f"/api/ceo/sessions/{env.key}/model-selection",
        json={"mode": "chain", "model_keys": ["alpha", "missing"]},
    )
    assert response.status_code == 404
    detail = response.json()["detail"]
    assert detail["code"] == "model_key_not_found"
    assert detail["model_key"] == "missing"
    response = env.client.patch(
        f"/api/ceo/sessions/{env.key}/model-selection",
        json={"mode": "chain", "model_keys": ["retired"]},
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "model_key_disabled"


def test_get_reports_session_chain_unavailable_members(env, monkeypatch):
    env.client.patch(
        f"/api/ceo/sessions/{env.key}/model-selection",
        json={"mode": "chain", "model_keys": ["alpha", "beta"]},
    )
    # beta 从 catalog 消失：存储不改写，读侧点名它，运行时按剩余成员继续。
    monkeypatch.setattr(ceo_sessions, "_live_config", lambda: _config({"alpha": SimpleNamespace(key="alpha", enabled=True)}))
    payload = env.client.get(f"/api/ceo/sessions/{env.key}/model-selection").json()
    assert payload["model_keys"] == ["alpha", "beta"]
    assert payload["is_session_chain"] is True
    assert payload["chain_unavailable_keys"] == ["beta"]


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


def _support(
    tmp_path,
    *,
    pinned: str = "",
    chain: list[str] | None = None,
    models: dict[str, SimpleNamespace] | None = None,
) -> CeoFrontDoorSupport:
    manager = SessionManager(tmp_path)
    key = "web:ceo-pin"
    session = manager.get_or_create(key)
    session.metadata = {"title": "t"}
    if pinned:
        session.metadata[wcs.SESSION_MODEL_SELECTION_KEY] = {"mode": "model", "model_key": pinned, "model_keys": []}
    elif chain:
        session.metadata[wcs.SESSION_MODEL_SELECTION_KEY] = {"mode": "chain", "model_key": "", "model_keys": list(chain)}
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


def test_runtime_session_chain_ignored_without_session_key(tmp_path, monkeypatch):
    monkeypatch.setattr(CeoFrontDoorSupport, "_resolve_ceo_model_refs", lambda self: ["chain-a"])
    models = {"alpha": SimpleNamespace(key="alpha", enabled=True)}
    support, _key = _support(tmp_path, chain=["alpha"], models=models)
    assert support._resolve_ceo_model_refs_for_session(None) == ["chain-a"]


def test_runtime_prefers_session_chain_over_global_chain(tmp_path, monkeypatch):
    monkeypatch.setattr(CeoFrontDoorSupport, "_resolve_ceo_model_refs", lambda self: ["chain-a"])
    models = {
        "alpha": SimpleNamespace(key="alpha", enabled=True),
        "beta": SimpleNamespace(key="beta", enabled=True),
    }
    support, key = _support(tmp_path, chain=["beta", "alpha"], models=models)
    assert support._resolve_ceo_model_refs_for_session(key) == ["beta", "alpha"]


def test_runtime_pinned_model_outranks_session_chain(tmp_path, monkeypatch):
    # 两态互斥由归一化保证；这条钉住优先级本身，防止后来者把会话链排到固定模型上面。
    monkeypatch.setattr(CeoFrontDoorSupport, "_resolve_ceo_model_refs", lambda self: ["chain-a"])
    manager = SessionManager(tmp_path)
    key = "web:ceo-pin"
    session = manager.get_or_create(key)
    session.metadata = {
        "title": "t",
        wcs.SESSION_MODEL_SELECTION_KEY: {"mode": "model", "model_key": "alpha", "model_keys": ["beta"]},
    }
    manager.save(session)
    models = {
        "alpha": SimpleNamespace(key="alpha", enabled=True),
        "beta": SimpleNamespace(key="beta", enabled=True),
    }
    loop = SimpleNamespace(sessions=manager, app_config=_config(models), provider_name="openai", model="gpt-test")
    assert CeoFrontDoorSupport(loop=loop)._resolve_ceo_model_refs_for_session(key) == ["alpha"]


def test_runtime_session_chain_drops_disabled_member(tmp_path, monkeypatch):
    monkeypatch.setattr(CeoFrontDoorSupport, "_resolve_ceo_model_refs", lambda self: ["chain-a"])
    models = {
        "alpha": SimpleNamespace(key="alpha", enabled=True),
        "retired": SimpleNamespace(key="retired", enabled=False),
    }
    support, key = _support(tmp_path, chain=["retired", "alpha"], models=models)
    assert support._resolve_ceo_model_refs_for_session(key) == ["alpha"]


def test_runtime_session_chain_falls_back_when_every_member_gone(tmp_path, monkeypatch):
    monkeypatch.setattr(CeoFrontDoorSupport, "_resolve_ceo_model_refs", lambda self: ["chain-a"])
    support, key = _support(tmp_path, chain=["gone"], models={})
    assert support._resolve_ceo_model_refs_for_session(key) == ["chain-a"]


def _mm_model(key: str, *, enabled: bool = True, multimodal: bool) -> SimpleNamespace:
    return SimpleNamespace(key=key, enabled=enabled, image_multimodal_enabled=multimodal)


def _image_multimodal_for(models: dict[str, SimpleNamespace], refs: list[str]) -> bool:
    from g3ku.runtime.frontdoor._ceo_runtime_ops import CeoFrontDoorRuntimeOps

    stub = SimpleNamespace(_frontdoor_runtime_config=lambda: _config(models))
    return bool(CeoFrontDoorRuntimeOps._ceo_image_multimodal_enabled_for_model_refs(stub, refs))


def test_image_multimodal_requires_every_chain_member():
    mm = _mm_model("mm", multimodal=True)
    plain = _mm_model("plain", multimodal=False)
    models = {"mm": mm, "plain": plain}
    # 正样本：全多模态必须判 True，否则判据退化成恒 False 也测不出来。
    assert _image_multimodal_for(models, ["mm"]) is True
    assert _image_multimodal_for(models, ["mm", "mm"]) is True
    # 混链整条按不支持处理，与成员顺序无关。
    assert _image_multimodal_for(models, ["mm", "plain"]) is False
    assert _image_multimodal_for(models, ["plain", "mm"]) is False
    # 解析不到的成员跳过：运行时不会把它发出去，不该替它否决整条链。
    assert _image_multimodal_for(models, ["gone", "mm"]) is True
    assert _image_multimodal_for(models, ["gone"]) is False
    assert _image_multimodal_for(models, []) is False


def test_model_selection_normalization():
    assert wcs.normalize_model_selection({"mode": "model", "model_key": " a "}) == {
        "mode": "model",
        "model_key": "a",
        "model_keys": [],
    }
    # 指定模式缺 key 视为模型链，不产生半配置状态。
    assert wcs.normalize_model_selection({"mode": "model"}) == {"mode": "chain", "model_key": "", "model_keys": []}
    assert wcs.normalize_model_selection(None) == {"mode": "chain", "model_key": "", "model_keys": []}
    # 会话链：去重保序，空白项丢弃。
    assert wcs.normalize_model_selection({"mode": "chain", "model_keys": [" b ", "b", "", " a "]}) == {
        "mode": "chain",
        "model_key": "",
        "model_keys": ["b", "a"],
    }
    assert wcs.ceo_session_pinned_model_key({"model_selection": {"mode": "model", "model_key": "a"}}) == "a"
    assert wcs.ceo_session_pinned_model_key({"model_selection": {"mode": "chain"}}) == ""
    assert wcs.ceo_session_pinned_model_key(None) == ""
    assert wcs.ceo_session_model_chain_keys({"model_selection": {"mode": "chain", "model_keys": ["b", "a"]}}) == ["b", "a"]
    assert wcs.ceo_session_model_chain_keys({"model_selection": {"mode": "model", "model_key": "a", "model_keys": ["b"]}}) == []
    assert wcs.ceo_session_model_chain_keys(None) == []
