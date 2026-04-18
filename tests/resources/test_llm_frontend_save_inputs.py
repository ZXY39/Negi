from __future__ import annotations

import json
import subprocess
import textwrap
from pathlib import Path


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


def test_handle_create_save_uses_current_dom_json_and_model_key() -> None:
    result = _run_node_script(
        """
        const fs = require("fs");
        const vm = require("vm");

        global.window = global;
        global.window.addEventListener = () => {};
        const elements = {
          "llm-model-key-input": { value: "demo_key" },
          "llm-provider-select": { value: "demo-provider" },
          "llm-json-editor": {
            value: JSON.stringify({
              provider_id: "demo-provider",
              capability: "chat",
              auth_mode: "api_key",
              api_key: "live-key",
              default_model: "live-model",
              parameters: { temperature: 0.2 },
              extra_headers: {},
              extra_options: {},
            }),
          },
          "llm-binding-retry-on": { value: "network,429,5xx" },
          "llm-binding-retry-count": { value: "0" },
          "llm-binding-single-api-key-max-concurrency": { value: "" },
          "llm-binding-context-window-tokens": { value: "30001" },
          "llm-bindings-list": { innerHTML: "", addEventListener: () => {} },
          "llm-editor-shell": { innerHTML: "", addEventListener: () => {} },
          "llm-editor-backdrop": { addEventListener: () => {} },
          "llm-memory-settings-btn": { addEventListener: () => {} },
          "llm-config-create-btn": { addEventListener: () => {} },
          "model-roles-cancel-btn": {},
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
            bindings: [],
            bindingMap: {},
            routes: { ceo: [], execution: [], inspection: [], memory: [] },
            roleIterations: { ceo: null, execution: null, inspection: null },
            roleConcurrency: { ceo: null, execution: null, inspection: null },
            editor: {
              open: true,
              mode: "create",
              bindingKey: "",
              configId: "",
              modelKey: "",
              providerId: "demo-provider",
              jsonText: JSON.stringify({
                provider_id: "demo-provider",
                capability: "chat",
                auth_mode: "api_key",
                api_key: "stale-key",
                default_model: "stale-model",
                parameters: {},
                extra_headers: {},
                extra_options: {},
              }),
              initialJsonText: "{}",
              retryOn: ["network", "429", "5xx"],
              retryCount: 0,
              singleApiKeyMaxConcurrency: "",
              contextWindowTokens: "",
              validation: null,
              probe: null,
              memory: {
                loading: false,
                error: "",
                embedding: {},
                rerank: {},
              },
            },
            eventsBound: false,
          },
        };
        global.U = {};
        let createPayload = null;
        global.ApiClient = {
          validateLlmDraft: async () => ({ valid: true }),
          probeLlmDraft: async () => ({ success: true, message: "ok" }),
          createLlmBinding: async (payload) => {
            createPayload = payload;
            return { item: payload.binding, runtimeRefresh: null };
          },
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
          "window.__llmTestHooks = {\\n    handleCreateSave,"
        );
        vm.runInThisContext(code);

        (async () => {
          await window.__llmTestHooks.handleCreateSave();
          console.log(JSON.stringify({ createPayload }));
        })().catch((error) => {
          console.log(JSON.stringify({ error: error.message || String(error) }));
          process.exit(1);
        });
        """
    )

    assert result["createPayload"]["binding"]["key"] == "demo_key"
    assert result["createPayload"]["draft"]["api_key"] == "live-key"
    assert result["createPayload"]["draft"]["default_model"] == "live-model"
    assert result["createPayload"]["draft"]["parameters"]["temperature"] == 0.2
    assert result["createPayload"]["draft"]["parameters"]["context_window_tokens"] == 30001


def test_handle_detail_save_uses_current_dom_json_text() -> None:
    result = _run_node_script(
        """
        const fs = require("fs");
        const vm = require("vm");

        global.window = global;
        global.window.addEventListener = () => {};
        const elements = {
          "llm-json-editor": {
            value: JSON.stringify({
              provider_id: "demo-provider",
              capability: "chat",
              auth_mode: "api_key",
              api_key: "detail-live-key",
              default_model: "detail-live-model",
              parameters: { temperature: 0.4 },
              extra_headers: {},
              extra_options: {},
            }),
          },
          "llm-binding-retry-on": { value: "network,429,5xx" },
          "llm-binding-retry-count": { value: "0" },
          "llm-binding-single-api-key-max-concurrency": { value: "" },
          "llm-binding-context-window-tokens": { value: "35001" },
          "llm-bindings-list": { innerHTML: "", addEventListener: () => {} },
          "llm-editor-shell": { innerHTML: "", addEventListener: () => {} },
          "llm-editor-backdrop": { addEventListener: () => {} },
          "llm-memory-settings-btn": { addEventListener: () => {} },
          "llm-config-create-btn": { addEventListener: () => {} },
          "model-roles-cancel-btn": {},
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
            bindings: [{
              key: "demo_key",
              capability: "chat",
              config_id: "cfg-1",
              llm_config_id: "cfg-1",
              retry_on: ["network", "429", "5xx"],
              retry_count: 0,
              single_api_key_max_concurrency: null,
              context_window_tokens: 32000,
            }],
            bindingMap: {
              demo_key: {
                key: "demo_key",
                capability: "chat",
                config_id: "cfg-1",
                llm_config_id: "cfg-1",
                retry_on: ["network", "429", "5xx"],
                retry_count: 0,
                single_api_key_max_concurrency: null,
                context_window_tokens: 32000,
              },
            },
            routes: { ceo: [], execution: [], inspection: [], memory: [] },
            roleIterations: { ceo: null, execution: null, inspection: null },
            roleConcurrency: { ceo: null, execution: null, inspection: null },
            editor: {
              open: true,
              mode: "detail",
              bindingKey: "demo_key",
              configId: "cfg-1",
              modelKey: "demo_key",
              providerId: "demo-provider",
              jsonText: JSON.stringify({
                provider_id: "demo-provider",
                capability: "chat",
                auth_mode: "api_key",
                api_key: "detail-stale-key",
                default_model: "detail-stale-model",
                parameters: {},
                extra_headers: {},
                extra_options: {},
              }),
              initialJsonText: JSON.stringify({
                provider_id: "demo-provider",
                capability: "chat",
                auth_mode: "api_key",
                api_key: "detail-stale-key",
                default_model: "detail-stale-model",
                parameters: {},
                extra_headers: {},
                extra_options: {},
              }),
              retryOn: ["network", "429", "5xx"],
              retryCount: 0,
              singleApiKeyMaxConcurrency: "",
              contextWindowTokens: "32000",
              validation: null,
              probe: null,
              memory: {
                loading: false,
                error: "",
                embedding: {},
                rerank: {},
              },
            },
            eventsBound: false,
          },
        };
        global.U = {};
        let updateConfigPayload = null;
        let updateBindingPayload = null;
        global.ApiClient = {
          validateLlmDraft: async () => ({ valid: true }),
          probeLlmDraft: async () => ({ success: true, message: "ok" }),
          updateLlmConfig: async (_configId, payload) => {
            updateConfigPayload = payload;
            return { item: payload, runtimeRefresh: null };
          },
          updateLlmBinding: async (_modelKey, payload) => {
            updateBindingPayload = payload;
            return { item: payload, runtimeRefresh: null };
          },
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
          console.log(JSON.stringify({ updateConfigPayload, updateBindingPayload }));
        })().catch((error) => {
          console.log(JSON.stringify({ error: error.message || String(error) }));
          process.exit(1);
        });
        """
    )

    assert result["updateConfigPayload"]["api_key"] == "detail-live-key"
    assert result["updateConfigPayload"]["default_model"] == "detail-live-model"
    assert result["updateConfigPayload"]["parameters"]["temperature"] == 0.4
    assert result["updateConfigPayload"]["parameters"]["context_window_tokens"] == 35001
    assert result["updateBindingPayload"] is None
