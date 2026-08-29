from __future__ import annotations

import json
import subprocess
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def _run_node_script(script: str) -> dict[str, object]:
    completed = subprocess.run(
        ["node", "-"],
        input=textwrap.dedent(script),
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=True,
        cwd=REPO_ROOT,
    )
    return json.loads(completed.stdout.strip())


class _StubFacade:
    def get_binding(self, config: object, key: str) -> dict[str, object]:
        return {"key": key, "llm_config_id": "cfg"}


def _build_manager(monkeypatch: pytest.MonkeyPatch) -> tuple[object, object]:
    from g3ku.config.model_manager import ModelManager
    from g3ku.config.schema import Config

    cfg = Config.model_validate(
        {
            "agents": {"multi_agent": {"orchestrator_model_key": "old-key"}},
            "models": {
                "catalog": [
                    {"key": "old-key", "llm_config_id": "cfg-old", "enabled": True},
                    {"key": "other-key", "llm_config_id": "cfg-other", "enabled": True},
                ],
                "roles": {
                    "ceo": ["old-key"],
                    "execution": ["old-key", "other-key"],
                    "inspection": ["other-key"],
                    "memory": [],
                },
            },
        }
    )
    manager = object.__new__(ModelManager)
    manager.config = cfg
    manager.facade = _StubFacade()
    monkeypatch.setattr(ModelManager, "save", lambda self: None)
    return manager, cfg


def test_rename_model_updates_catalog_roles_and_orchestrator(monkeypatch: pytest.MonkeyPatch) -> None:
    manager, _cfg = _build_manager(monkeypatch)

    result = manager.rename_model("old-key", "new-key")

    assert result["key"] == "new-key"
    assert manager.config.get_managed_model("old-key") is None
    assert manager.config.get_managed_model("new-key") is not None
    assert manager.config.models.roles.ceo == ["new-key"]
    assert manager.config.models.roles.execution == ["new-key", "other-key"]
    assert manager.config.models.roles.inspection == ["other-key"]
    assert manager.config.agents.multi_agent.orchestrator_model_key == "new-key"


def test_rename_model_rejects_duplicate_key(monkeypatch: pytest.MonkeyPatch) -> None:
    manager, _cfg = _build_manager(monkeypatch)

    with pytest.raises(ValueError, match="already exists"):
        manager.rename_model("old-key", "other-key")


def test_rename_model_rejects_empty_key(monkeypatch: pytest.MonkeyPatch) -> None:
    manager, _cfg = _build_manager(monkeypatch)

    with pytest.raises(ValueError, match="required"):
        manager.rename_model("old-key", "   ")


def test_rename_model_noop_when_same_key(monkeypatch: pytest.MonkeyPatch) -> None:
    manager, _cfg = _build_manager(monkeypatch)

    result = manager.rename_model("old-key", "old-key")

    assert result["key"] == "old-key"
    assert manager.config.get_managed_model("old-key") is not None


def test_rename_llm_binding_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    import importlib

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    admin_rest = importlib.import_module("main.api.admin_rest")

    calls: dict[str, str] = {}

    class _StubManager:
        def rename_model(self, key: str, new_key: str) -> dict[str, str]:
            calls["key"] = key
            calls["new_key"] = new_key
            return {"key": new_key, "config_id": "cfg-1"}

    monkeypatch.setattr(admin_rest.ModelManager, "load", classmethod(lambda cls: _StubManager()))

    async def _no_refresh(reason: str, *, force_memory_sync: bool = False) -> dict[str, object]:
        return {"saved": True, "reason": reason}

    monkeypatch.setattr(admin_rest, "_refresh_runtime_after_save", _no_refresh)

    app = FastAPI()
    app.include_router(admin_rest.router, prefix="/api")
    client = TestClient(app)

    response = client.post("/api/llm/bindings/old-key/rename", json={"key": "new-key"})

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["item"]["key"] == "new-key"
    assert calls == {"key": "old-key", "new_key": "new-key"}


_DETAIL_SAVE_SCRIPT_TEMPLATE = """
const fs = require("fs");
const vm = require("vm");

global.window = global;
global.window.addEventListener = () => {};
const initialJson = JSON.stringify({
  provider_id: "demo-provider",
  capability: "chat",
  auth_mode: "api_key",
  api_key: "detail-key",
  default_model: "@@INITIAL_MODEL@@",
  parameters: { context_window_tokens: 32000 },
  extra_headers: {},
  extra_options: {},
});
const changedJson = JSON.stringify({
  provider_id: "demo-provider",
  capability: "chat",
  auth_mode: "api_key",
  api_key: "detail-key",
  default_model: "@@NEW_MODEL@@",
  parameters: { context_window_tokens: 32000 },
  extra_headers: {},
  extra_options: {},
});
const elements = {
  "llm-json-editor": { value: changedJson },
  "llm-binding-retry-on": { value: "network,429,5xx" },
  "llm-binding-retry-count": { value: "0" },
  "llm-binding-single-api-key-max-concurrency": { value: "" },
  "llm-binding-context-window-tokens": { value: "32000" },
  "llm-binding-image-multimodal-enabled": { checked: false },
  "llm-bindings-list": { innerHTML: "", addEventListener: () => {} },
  "llm-editor-shell": { innerHTML: "", addEventListener: () => {} },
  "llm-editor-backdrop": { addEventListener: () => {} },
  "llm-memory-settings-btn": { addEventListener: () => {} },
  "llm-config-create-btn": { addEventListener: () => {} },
  "model-roles-cancel-btn": {},
};
const binding = {
  key: "@@BINDING_KEY@@",
  capability: "chat",
  config_id: "cfg-1",
  llm_config_id: "cfg-1",
  retry_on: @@BINDING_RETRY_ON@@,
  retry_count: 0,
  single_api_key_max_concurrency: null,
  context_window_tokens: 32000,
  image_multimodal_enabled: false,
};
global.document = {
  getElementById: (id) => elements[id] || null,
  querySelector: () => ({}),
  addEventListener: () => {},
};
global.S = {
  modelCatalog: { roleEditing: false },
  llmCenter: {
    loading: false,
    saving: false,
    error: "",
    templates: [{ provider_id: "demo-provider", display_name: "Demo", capability: "chat" }],
    templateMap: { "demo-provider": { provider_id: "demo-provider", display_name: "Demo", capability: "chat" } },
    templateDetailMap: {},
    bindings: [binding],
    bindingMap: { "@@BINDING_KEY@@": binding },
    routes: { ceo: [], execution: [], inspection: [], memory: [] },
    roleIterations: { ceo: null, execution: null, inspection: null },
    roleConcurrency: { ceo: null, execution: null, inspection: null },
    editor: {
      open: true,
      mode: "detail",
      bindingKey: "@@BINDING_KEY@@",
      configId: "cfg-1",
      modelKey: "@@BINDING_KEY@@",
      providerId: "demo-provider",
      jsonText: initialJson,
      initialJsonText: initialJson,
      retryOn: ["network", "429", "5xx"],
      retryCount: 0,
      singleApiKeyMaxConcurrency: "",
      contextWindowTokens: "32000",
      imageMultimodalEnabled: false,
      initialImageMultimodalEnabled: false,
      validation: null,
      probe: null,
      memory: { loading: false, error: "", embedding: {}, rerank: {} },
    },
    eventsBound: false,
  },
};
global.U = {};
let updateConfigDefaultModel = null;
let updateBindingKey = null;
const renameCalls = [];
global.ApiClient = {
  validateLlmDraft: async () => ({ valid: true }),
  probeLlmDraft: async () => ({ success: true, message: "ok" }),
  updateLlmConfig: async (_id, payload) => { updateConfigDefaultModel = payload.default_model; return { item: payload, runtimeRefresh: null }; },
  updateLlmBinding: async (modelKey, payload) => { updateBindingKey = modelKey; return { item: payload, runtimeRefresh: null }; },
  renameLlmBinding: async (modelKey, newKey) => { renameCalls.push([modelKey, newKey]); return { item: { key: newKey }, runtimeRefresh: null }; },
  getLlmTemplates: async () => [],
  listLlmBindings: async () => ({
    items: [],
    routes: { ceo: [], execution: [], inspection: [], memory: [] },
    roleIterations: { ceo: null, execution: null, inspection: null },
    roleConcurrency: { ceo: null, execution: null, inspection: null },
  }),
};
global.showToast = () => {};
global.esc = (value) => String(value ?? "");
global.MODEL_SCOPES = [];
global.EMPTY_MODEL_ROLES = () => ({ ceo: [], execution: [], inspection: [], memory: [] });
global.DEFAULT_ROLE_ITERATIONS = () => ({ ceo: null, execution: null, inspection: null });
global.DEFAULT_ROLE_CONCURRENCY = () => ({ ceo: null, execution: null, inspection: null });
global.DEFAULT_MODEL_DEFAULTS = () => ({ ceo: "", execution: "", inspection: "" });
global.normalizeAllModelRoles = (value) => value;
global.normalizeRoleIterations = (value) => value;
global.normalizeRoleConcurrency = (value) => value;
global.cloneModelRoles = (value) => value;
global.cloneRoleIterations = (value) => value;
global.cloneRoleConcurrency = (value) => value;
global.syncModelRoleDraftState = () => {};
global.hint = () => {};
global.setDrawerOpen = () => {};
global.icons = () => {};
global.enhanceResourceSelects = () => {};
let code = fs.readFileSync("g3ku/web/frontend/org_graph_llm.js", "utf8");
code = code.replace(
  "window.__llmTestHooks = {",
  "window.__llmTestHooks = {\\n    handleDetailSave,"
);
vm.runInThisContext(code);

(async () => {
  await window.__llmTestHooks.handleDetailSave();
  console.log(JSON.stringify({ updateConfigDefaultModel, updateBindingKey, renameCalls }));
})().catch((error) => {
  console.log(JSON.stringify({ error: error.message || String(error) }));
  process.exit(1);
});
"""


def _run_detail_save(binding_key: str, initial_model: str, new_model: str, binding_retry_on: str) -> dict[str, object]:
    script = (
        _DETAIL_SAVE_SCRIPT_TEMPLATE
        .replace("@@BINDING_KEY@@", binding_key)
        .replace("@@INITIAL_MODEL@@", initial_model)
        .replace("@@NEW_MODEL@@", new_model)
        .replace("@@BINDING_RETRY_ON@@", binding_retry_on)
    )
    return _run_node_script(script)


def test_handle_detail_save_renames_binding_key_when_key_matches_old_model() -> None:
    result = _run_detail_save(
        binding_key="minimax/minimax-m3:free",
        initial_model="minimax/minimax-m3:free",
        new_model="z-ai/glm-5.2:free",
        binding_retry_on='["network", "429"]',
    )

    assert result["updateConfigDefaultModel"] == "z-ai/glm-5.2:free"
    assert result["renameCalls"] == [["minimax/minimax-m3:free", "z-ai/glm-5.2:free"]]
    assert result["updateBindingKey"] == "z-ai/glm-5.2:free"


def test_handle_detail_save_keeps_custom_key_when_it_differs_from_model() -> None:
    result = _run_detail_save(
        binding_key="ceo_primary",
        initial_model="minimax/minimax-m3:free",
        new_model="z-ai/glm-5.2:free",
        binding_retry_on='["network", "429", "5xx"]',
    )

    assert result["updateConfigDefaultModel"] == "z-ai/glm-5.2:free"
    assert result["renameCalls"] == []
    assert result["updateBindingKey"] is None