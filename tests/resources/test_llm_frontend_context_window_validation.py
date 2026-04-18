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


def test_binding_draft_payload_rejects_context_window_tokens_not_greater_than_25000() -> None:
    result = _run_node_script(
        """
        const fs = require("fs");
        const vm = require("vm");
        global.window = global;
        global.window.addEventListener = () => {};
        global.document = {
          getElementById: (id) => ({
            "llm-model-key-input": { value: "demo" },
            "llm-provider-select": { value: "demo" },
            "llm-json-editor": { value: JSON.stringify({ provider_id: "demo", capability: "chat", auth_mode: "api_key", api_key: "k", default_model: "m", parameters: {}, extra_headers: {}, extra_options: {} }) },
            "llm-binding-retry-on": { value: "network" },
            "llm-binding-retry-count": { value: "0" },
            "llm-binding-single-api-key-max-concurrency": { value: "" },
            "llm-binding-context-window-tokens": { value: "25000" },
          }[id] || null),
          querySelector: () => null,
          addEventListener: () => {},
        };
        global.S = {
          modelCatalog: {},
          llmCenter: {
            loading: false,
            saving: false,
            error: "",
            templates: [],
            templateMap: {},
            templateDetailMap: {},
            bindings: [],
            bindingMap: {},
            routes: {},
            roleIterations: {},
            roleConcurrency: {},
            editor: {
              open: true,
              mode: "detail",
              bindingKey: "demo",
              configId: "cfg",
              modelKey: "demo",
              providerId: "demo",
              jsonText: "{}",
              initialJsonText: "{}",
              retryOn: ["network"],
              retryCount: 0,
              singleApiKeyMaxConcurrency: "",
              contextWindowTokens: "25000",
              initialContextWindowTokens: "25000",
              validation: null,
              probe: null,
              memory: { loading: false, error: "", embedding: {}, rerank: {} },
            },
            eventsBound: false,
          },
        };
        global.U = {};
        global.ApiClient = {};
        global.showToast = () => {};
        global.esc = (value) => String(value ?? "");
        global.EMPTY_MODEL_ROLES = () => ({ ceo: [], execution: [], inspection: [] });
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
          "window.__llmTestHooks = {\\n    bindingDraftPayload,"
        );
        vm.runInThisContext(code);

        let message = "";
        try {
          window.__llmTestHooks.bindingDraftPayload({ requireModelKey: true });
        } catch (error) {
          message = error.message || String(error);
        }

        console.log(JSON.stringify({ message }));
        """
    )

    assert "25000" in str(result["message"])
