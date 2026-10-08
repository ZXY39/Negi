"""会话模型链的独立验证器（本机 .venv 无 pytest，这里跑真实函数与真实 REST 口）。

用后即删，不入版本库。
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from g3ku.runtime import web_ceo_sessions as wcs  # noqa: E402
from g3ku.runtime.api import ceo_sessions  # noqa: E402
from g3ku.runtime.frontdoor._ceo_runtime_ops import CeoFrontDoorRuntimeOps  # noqa: E402
from g3ku.runtime.frontdoor._ceo_support import CeoFrontDoorSupport  # noqa: E402
from g3ku.session.manager import SessionManager  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, got, want) -> None:
    ok = got == want
    RESULTS.append((name, ok, f"got={got!r} want={want!r}"))


def _cfg(models: dict[str, SimpleNamespace]) -> SimpleNamespace:
    return SimpleNamespace(get_managed_model=lambda key: models.get(str(key or "").strip()))


# ---------------------------------------------------------------- 1. 归一化与落盘形状
check(
    "norm.dedup_and_order",
    wcs.normalize_model_selection({"mode": "chain", "model_keys": [" b ", "b", "", " a "]}),
    {"mode": "chain", "model_key": "", "model_keys": ["b", "a"]},
)
check(
    "norm.model_mode_clears_chain",
    wcs.normalize_model_selection({"mode": "model", "model_key": " a ", "model_keys": ["b"]}),
    {"mode": "model", "model_key": "a", "model_keys": []},
)
check("norm.identity", wcs.normalize_model_selection(None), {"mode": "chain", "model_key": "", "model_keys": []})
check(
    "reader.chain_keys",
    wcs.ceo_session_model_chain_keys({"model_selection": {"mode": "chain", "model_keys": ["b", "a"]}}),
    ["b", "a"],
)
check(
    "reader.chain_keys_empty_in_model_mode",
    wcs.ceo_session_model_chain_keys({"model_selection": {"mode": "model", "model_key": "a", "model_keys": ["b"]}}),
    [],
)
check(
    "metadata.identity_not_stored",
    wcs.SESSION_MODEL_SELECTION_KEY
    in wcs.normalize_ceo_metadata({"model_selection": {"mode": "chain", "model_keys": []}}, session_key="web:x"),
    False,
)
check(
    "metadata.session_chain_stored",
    wcs.ceo_session_model_chain_keys(
        wcs.normalize_ceo_metadata({"model_selection": {"mode": "chain", "model_keys": ["b"]}}, session_key="web:x")
    ),
    ["b"],
)

# ---------------------------------------------------------------- 2. REST 读写
tmp = Path(tempfile.mkdtemp())
manager = SessionManager(tmp)
session_key = "web:ceo-model"
seeded = manager.get_or_create(session_key)
seeded.metadata = {"title": "验证会话"}
manager.save(seeded)

models = {
    "alpha": SimpleNamespace(key="alpha", enabled=True),
    "beta": SimpleNamespace(key="beta", enabled=True),
    "retired": SimpleNamespace(key="retired", enabled=False),
}
ceo_sessions._sessions = lambda: (
    SimpleNamespace(main_task_service=None),
    manager,
    SimpleNamespace(get=lambda _k: None, remove=lambda _k: None),
    wcs.WebCeoStateStore(tmp),
)
live_config_state = {"config": _cfg(models)}
ceo_sessions._live_config = lambda: live_config_state["config"]

app = FastAPI()
app.include_router(ceo_sessions.router, prefix="/api")
client = TestClient(app)
route = f"/api/ceo/sessions/{session_key}/model-selection"

default_get = client.get(route)
check("rest.default_mode", default_get.json()["mode"], "chain")
check("rest.default_is_session_chain", default_get.json()["is_session_chain"], False)
check("rest.default_keys", default_get.json()["model_keys"], [])
check(
    "rest.default_metadata_clean",
    wcs.SESSION_MODEL_SELECTION_KEY in (manager.get_or_create(session_key).metadata or {}),
    False,
)

set_chain = client.patch(route, json={"mode": "chain", "model_keys": ["beta", "alpha", "beta", " "]})
check("rest.set_chain_status", set_chain.status_code, 200)
check("rest.set_chain_keys", set_chain.json()["model_keys"], ["beta", "alpha"])
check("rest.set_chain_flag", set_chain.json()["is_session_chain"], True)
check(
    "rest.set_chain_persisted",
    wcs.ceo_session_model_chain_keys(SessionManager(tmp).get_or_create(session_key).metadata),
    ["beta", "alpha"],
)

unknown = client.patch(route, json={"mode": "chain", "model_keys": ["alpha", "missing"]})
check("rest.unknown_status", unknown.status_code, 404)
check("rest.unknown_code", unknown.json()["detail"]["code"], "model_key_not_found")
check("rest.unknown_names_key", unknown.json()["detail"]["model_key"], "missing")
disabled = client.patch(route, json={"mode": "chain", "model_keys": ["retired"]})
check("rest.disabled_status", disabled.status_code, 409)
check("rest.disabled_code", disabled.json()["detail"]["code"], "model_key_disabled")

# 写入被拒时存储必须没被改坏。
check(
    "rest.rejected_write_kept_old_chain",
    wcs.ceo_session_model_chain_keys(manager.get_or_create(session_key).metadata),
    ["beta", "alpha"],
)

live_config_state["config"] = _cfg({"alpha": SimpleNamespace(key="alpha", enabled=True)})
after_removal = client.get(route).json()
check("rest.unavailable_reported", after_removal["chain_unavailable_keys"], ["beta"])
check("rest.unavailable_keeps_chain_flag", after_removal["is_session_chain"], True)

live_config_state["config"] = _cfg(models)
cleared = client.patch(route, json={"mode": "chain"})
check("rest.clear_chain_status", cleared.status_code, 200)
check("rest.clear_chain_keys", cleared.json()["model_keys"], [])
check(
    "rest.clear_chain_metadata_clean",
    wcs.SESSION_MODEL_SELECTION_KEY in (manager.get_or_create(session_key).metadata or {}),
    False,
)

pin = client.patch(route, json={"mode": "model", "model_key": "beta"})
check("rest.pin_still_works", pin.json()["model_key"], "beta")
check("rest.pin_clears_chain", pin.json()["model_keys"], [])

# ---------------------------------------------------------------- 3. 运行时优先级
_original_global = CeoFrontDoorSupport._resolve_ceo_model_refs
CeoFrontDoorSupport._resolve_ceo_model_refs = lambda self: ["chain-a", "chain-b"]
run_models = {
    "alpha": SimpleNamespace(key="alpha", enabled=True),
    "beta": SimpleNamespace(key="beta", enabled=True),
    "retired": SimpleNamespace(key="retired", enabled=False),
}


def _support(chain=None, pinned="", models=None):
    local_manager = SessionManager(Path(tempfile.mkdtemp()))
    key = "web:ceo-pin"
    record = local_manager.get_or_create(key)
    record.metadata = {"title": "t"}
    if pinned:
        record.metadata[wcs.SESSION_MODEL_SELECTION_KEY] = {"mode": "model", "model_key": pinned, "model_keys": []}
    elif chain:
        record.metadata[wcs.SESSION_MODEL_SELECTION_KEY] = {"mode": "chain", "model_key": "", "model_keys": list(chain)}
    local_manager.save(record)
    loop = SimpleNamespace(
        sessions=local_manager,
        app_config=_cfg(models if models is not None else run_models),
        provider_name="openai",
        model="gpt-test",
    )
    return CeoFrontDoorSupport(loop=loop), key


support, key = _support(chain=["beta", "alpha"])
check("runtime.session_chain_wins_over_global", support._resolve_ceo_model_refs_for_session(key), ["beta", "alpha"])
support, key = _support(chain=["retired", "alpha"])
check("runtime.drops_disabled_member", support._resolve_ceo_model_refs_for_session(key), ["alpha"])
support, key = _support(chain=["gone"], models={})
check("runtime.falls_back_when_all_gone", support._resolve_ceo_model_refs_for_session(key), ["chain-a", "chain-b"])
support, key = _support(chain=["alpha"])
check("runtime.no_session_key_uses_global", support._resolve_ceo_model_refs_for_session(None), ["chain-a", "chain-b"])
# 存储被手改成"同时带固定模型和会话链"时，固定模型必须仍然赢（两态互斥由归一化保证，
# 这条钉住读侧优先级，防止后来者把会话链排到固定模型上面）。
dirty_manager = SessionManager(Path(tempfile.mkdtemp()))
dirty_key = "web:ceo-pin"
dirty_record = dirty_manager.get_or_create(dirty_key)
dirty_record.metadata = {
    "title": "t",
    wcs.SESSION_MODEL_SELECTION_KEY: {"mode": "model", "model_key": "beta", "model_keys": ["alpha"]},
}
dirty_manager.save(dirty_record)
dirty_support = CeoFrontDoorSupport(
    loop=SimpleNamespace(
        sessions=dirty_manager,
        app_config=_cfg(run_models),
        provider_name="openai",
        model="gpt-test",
    )
)
check(
    "runtime.pin_outranks_chain_when_both_stored",
    dirty_support._resolve_ceo_model_refs_for_session(dirty_key),
    ["beta"],
)

# ---------------------------------------------------------------- 4. 多模态保守 AND
mm_models = {
    "mm": SimpleNamespace(key="mm", enabled=True, image_multimodal_enabled=True),
    "plain": SimpleNamespace(key="plain", enabled=True, image_multimodal_enabled=False),
}
stub = SimpleNamespace(_frontdoor_runtime_config=lambda: _cfg(mm_models))
call = lambda refs: bool(CeoFrontDoorRuntimeOps._ceo_image_multimodal_enabled_for_model_refs(stub, refs))
check("and.positive_single", call(["mm"]), True)
check("and.positive_two_multimodal", call(["mm", "mm"]), True)
check("and.mixed_head_multimodal", call(["mm", "plain"]), False)
check("and.mixed_head_plain", call(["plain", "mm"]), False)
check("and.unresolved_skipped", call(["gone", "mm"]), True)
check("and.nothing_resolved", call(["gone"]), False)
check("and.empty_chain", call([]), False)

CeoFrontDoorSupport._resolve_ceo_model_refs = _original_global

# ---------------------------------------------------------------- 5. 节点道与 CEO 同一份判据
import main.runtime.react_loop as react_loop_module  # noqa: E402
from main.runtime.model_route import RouteCandidateFilters, RouteMemberView  # noqa: E402

mm_models2 = {
    "mm": SimpleNamespace(key="mm", enabled=True, image_multimodal_enabled=True),
    "plain": SimpleNamespace(key="plain", enabled=True, image_multimodal_enabled=False),
}
react_loop_module.get_runtime_config = lambda force=False: (_cfg(mm_models2), 1, False)
node_gate = lambda refs: bool(react_loop_module.ReActToolLoop._image_multimodal_enabled_for_model_refs(refs))
check("node.and_positive", node_gate(["mm"]), True)
check("node.and_mixed_head_capable", node_gate(["mm", "plain"]), False)
check("node.and_mixed_head_incapable", node_gate(["plain", "mm"]), False)
check("node.and_unresolved_skipped", node_gate(["gone", "mm"]), True)
check("node.and_nothing_resolved", node_gate(["gone"]), False)

# 图片能力不再参与准入：不可达的那条过滤已经删掉。
check("filter.image_field_removed", hasattr(RouteCandidateFilters(), "requires_image_multimodal"), False)
check(
    "filter.incapable_member_still_eligible",
    RouteCandidateFilters(required_context_window_tokens=32000).allows(
        RouteMemberView(model_key="m_small", context_window_tokens=32000, image_multimodal_enabled=False)
    ),
    True,
)
check(
    "filter.window_still_excludes",
    RouteCandidateFilters(required_context_window_tokens=64000).allows(
        RouteMemberView(model_key="m_small", context_window_tokens=32000, image_multimodal_enabled=True)
    ),
    False,
)

# ---------------------------------------------------------------- 报告
failed = [row for row in RESULTS if not row[1]]
for name, ok, note in RESULTS:
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + ("" if ok else f"  [{note}]"))
print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} passed")
sys.exit(1 if failed else 0)
