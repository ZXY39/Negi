from __future__ import annotations

import json
import subprocess
import textwrap
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]

_BASE_EDITOR = {
    "open": True,
    "mode": "detail",
    "bindingKey": "demo",
    "configId": "cfg",
    "modelKey": "demo",
    "providerId": "demo",
    "jsonText": "{}",
    "initialJsonText": "{}",
    "retryOn": ["network"],
    "retryCount": 0,
    "singleApiKeyMaxConcurrency": "",
    "contextWindowTokens": "30000",
    "initialContextWindowTokens": "30000",
    "reasoningEffort": "medium",
    "initialReasoningEffort": "medium",
    "maxOutputTokens": "65536",
    "initialMaxOutputTokens": "65536",
    "validation": None,
    "probe": None,
    "memory": {"loading": False, "error": "", "embedding": {}, "rerank": {}},
}


_DRAFT_JSON_TEXT = json.dumps(
    {
        "provider_id": "demo",
        "capability": "chat",
        "auth_mode": "api_key",
        "api_key": "k",
        "default_model": "m",
        "parameters": {},
        "extra_headers": {},
        "extra_options": {},
    }
)


def _run_node_script(editor_overrides: dict, input_overrides: dict | None = None) -> dict:
    inputs = {
        "llm-model-key-input": {"value": "demo"},
        "llm-provider-select": {"value": "demo"},
        "llm-json-editor": {"value": _DRAFT_JSON_TEXT},
        "llm-binding-retry-on": {"value": "network"},
        "llm-binding-retry-count": {"value": "0"},
        "llm-binding-single-api-key-max-concurrency": {"value": ""},
        "llm-binding-context-window-tokens": {"value": "30000"},
        "llm-binding-reasoning-effort": {"value": "medium"},
        "llm-binding-max-output-tokens": {"value": "65536"},
    }
    inputs.update(input_overrides or {})
    editor = {**_BASE_EDITOR, **editor_overrides}
    script = f"""
        const fs = require("fs");
        const vm = require("vm");
        global.window = global;
        global.window.addEventListener = () => {{}};
        global.document = {{
          getElementById: (id) => ({json.dumps(inputs)}[id] || null),
          querySelector: () => null,
          addEventListener: () => {{}},
        }};
        global.S = {{
          modelCatalog: {{}},
          llmCenter: {{
            loading: false, saving: false, error: "",
            templates: [], templateMap: {{}}, templateDetailMap: {{}},
            bindings: [], bindingMap: {{}}, routes: {{}},
            roleIterations: {{}}, roleConcurrency: {{}},
            editor: {json.dumps(editor)},
            eventsBound: false,
          }},
        }};
        global.U = {{}};
        global.ApiClient = {{}};
        global.showToast = () => {{}};
        global.esc = (value) => String(value ?? "");
        global.EMPTY_MODEL_ROLES = () => ({{ ceo: [], execution: [], inspection: [] }});
        global.DEFAULT_ROLE_ITERATIONS = () => ({{ ceo: null, execution: null, inspection: null }});
        global.DEFAULT_ROLE_CONCURRENCY = () => ({{ ceo: null, execution: null, inspection: null }});
        global.DEFAULT_MODEL_DEFAULTS = () => ({{ ceo: "", execution: "", inspection: "" }});
        global.normalizeAllModelRoles = (value) => value;
        global.normalizeRoleIterations = (value) => value;
        global.normalizeRoleConcurrency = (value) => value;
        global.cloneModelRoles = (value) => value;
        global.cloneRoleIterations = (value) => value;
        global.cloneRoleConcurrency = (value) => value;
        global.syncModelRoleDraftState = () => {{}};
        global.hint = () => {{}};
        global.setDrawerOpen = () => {{}};
        global.icons = () => {{}};
        global.enhanceResourceSelects = () => {{}};
        let code = fs.readFileSync("g3ku/web/frontend/org_graph_llm.js", "utf8");
        code = code.replace(
          "window.__llmTestHooks = {{",
          "window.__llmTestHooks = {{\\n    bindingDraftPayload,\\n    renderThinkingOutputFields,"
        );
        vm.runInThisContext(code);

        let message = "";
        let payload = null;
        try {{
          payload = window.__llmTestHooks.bindingDraftPayload();
        }} catch (error) {{
          message = error.message || String(error);
        }}
        let html = "";
        try {{
          html = window.__llmTestHooks.renderThinkingOutputFields({json.dumps(editor)});
        }} catch (error) {{
          message = message || (error.message || String(error));
        }}

        console.log(JSON.stringify({{ message, payload, html }}));
        """
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


def test_binding_draft_payload_defaults_write_thinking_and_output_parameters() -> None:
    result = _run_node_script({})
    assert result["message"] == ""
    parameters = result["payload"]["draft"]["parameters"]
    assert parameters["reasoning_effort"] == "medium"
    assert parameters["max_tokens"] == 65536
    assert parameters["context_window_tokens"] == 30000


def test_binding_draft_payload_rejects_invalid_reasoning_effort() -> None:
    result = _run_node_script(
        {"reasoningEffort": "ultra"},
        {"llm-binding-reasoning-effort": {"value": "ultra"}},
    )
    assert "none" in str(result["message"])
    assert "medium" in str(result["message"])


def test_binding_draft_payload_rejects_invalid_max_output_tokens() -> None:
    result = _run_node_script(
        {"maxOutputTokens": "0"},
        {"llm-binding-max-output-tokens": {"value": "0"}},
    )
    assert "65536" in result["html"]
    assert "最大输出TOKEN" in str(result["message"])


def test_binding_draft_payload_keeps_none_reasoning_level() -> None:
    result = _run_node_script(
        {"reasoningEffort": "none"},
        {"llm-binding-reasoning-effort": {"value": "none"}},
    )
    assert result["message"] == ""
    assert result["payload"]["draft"]["parameters"]["reasoning_effort"] == "none"


def test_thinking_output_fields_render_six_levels() -> None:
    result = _run_node_script({})
    html = str(result["html"])
    assert 'id="llm-binding-reasoning-effort"' in html
    assert 'id="llm-binding-max-output-tokens"' in html
    for level in ("none", "low", "medium", "high", "xhigh", "max"):
        assert f'value="{level}"' in html

def _run_persistent_inputs_script(json_parameters: dict, extra_hooks: str = "") -> dict:
    """Drive handleBindingJsonEditorInput against mutable input stubs."""
    script = f"""
        const fs = require("fs");
        const vm = require("vm");
        global.window = global;
        global.window.addEventListener = () => {{}};
        const inputs = {{}};
        const makeInput = (value) => ({{ value: String(value ?? "") }});
        [
          "llm-model-key-input", "llm-provider-select", "llm-json-editor",
          "llm-binding-retry-on", "llm-binding-retry-count",
          "llm-binding-single-api-key-max-concurrency",
          "llm-binding-context-window-tokens",
          "llm-binding-reasoning-effort", "llm-binding-max-output-tokens",
          "llm-binding-base-url", "llm-binding-api-key", "llm-binding-default-model",
          "llm-binding-image-multimodal-enabled",
        ].forEach((id) => {{ inputs[id] = makeInput(""); }});
        inputs["llm-json-editor"].value = JSON.stringify({{
          provider_id: "demo", capability: "chat", auth_mode: "api_key",
          api_key: "k", default_model: "m",
          parameters: {json.dumps(json_parameters)},
          extra_headers: {{}}, extra_options: {{}},
        }});
        inputs["llm-provider-select"].value = "demo";
        global.document = {{
          getElementById: (id) => inputs[id] || null,
          querySelector: () => null,
          addEventListener: () => {{}},
        }};
        global.S = {{
          modelCatalog: {{}},
          llmCenter: {{
            loading: false, saving: false, error: "",
            templates: [], templateMap: {{}}, templateDetailMap: {{}},
            bindings: [], bindingMap: {{}}, routes: {{}},
            roleIterations: {{}}, roleConcurrency: {{}},
            editor: {json.dumps(_BASE_EDITOR)},
            eventsBound: false,
          }},
        }};
        global.U = {{}};
        global.ApiClient = {{}};
        global.showToast = () => {{}};
        global.esc = (value) => String(value ?? "");
        global.EMPTY_MODEL_ROLES = () => ({{ ceo: [], execution: [], inspection: [] }});
        global.DEFAULT_ROLE_ITERATIONS = () => ({{ ceo: null, execution: null, inspection: null }});
        global.DEFAULT_ROLE_CONCURRENCY = () => ({{ ceo: null, execution: null, inspection: null }});
        global.DEFAULT_MODEL_DEFAULTS = () => ({{ ceo: "", execution: "", inspection: "" }});
        global.normalizeAllModelRoles = (value) => value;
        global.normalizeRoleIterations = (value) => value;
        global.normalizeRoleConcurrency = (value) => value;
        global.cloneModelRoles = (value) => value;
        global.cloneRoleIterations = (value) => value;
        global.cloneRoleConcurrency = (value) => value;
        global.syncModelRoleDraftState = () => {{}};
        global.hint = () => {{}};
        global.setDrawerOpen = () => {{}};
        global.icons = () => {{}};
        global.enhanceResourceSelects = () => {{}};
        global.syncResourceSelectUI = () => {{}};
        let code = fs.readFileSync("g3ku/web/frontend/org_graph_llm.js", "utf8");
        vm.runInThisContext(code);

        const hooks = window.__llmTestHooks;
        let message = "";
        try {{
          hooks.handleBindingJsonEditorInput();
        }} catch (error) {{
          message = error.message || String(error);
        }}
        console.log(JSON.stringify({{
          message,
          reasoning: inputs["llm-binding-reasoning-effort"].value,
          maxTokens: inputs["llm-binding-max-output-tokens"].value,
          normalized: hooks.withThinkingOutputParameterDefaults({{
            provider_id: "demo",
            parameters: {json.dumps(json_parameters)},
          }}),
        }}));
        """
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


def test_json_editor_edits_sync_form_reasoning_and_max_tokens() -> None:
    result = _run_persistent_inputs_script(
        {"context_window_tokens": 100000, "reasoning_effort": "high", "max_tokens": 20000}
    )
    assert result["message"] == ""
    assert result["reasoning"] == "high"
    assert result["maxTokens"] == "20000"


def test_json_editor_missing_new_params_falls_back_to_defaults() -> None:
    result = _run_persistent_inputs_script({"context_window_tokens": 100000})
    assert result["message"] == ""
    assert result["reasoning"] == "medium"
    assert result["maxTokens"] == "65536"


def test_opening_draft_normalization_injects_new_parameters() -> None:
    result = _run_persistent_inputs_script({"context_window_tokens": 100000})
    normalized = result["normalized"]
    assert normalized["parameters"]["reasoning_effort"] == "medium"
    assert normalized["parameters"]["max_tokens"] == 65536


def test_opening_draft_normalization_preserves_valid_values() -> None:
    result = _run_persistent_inputs_script(
        {"context_window_tokens": 100000, "reasoning_effort": "max", "max_tokens": 65536}
    )
    normalized = result["normalized"]
    assert normalized["parameters"]["reasoning_effort"] == "max"
    assert normalized["parameters"]["max_tokens"] == 65536


def test_opening_draft_normalization_repairs_invalid_values() -> None:
    result = _run_persistent_inputs_script(
        {"context_window_tokens": 100000, "reasoning_effort": "yolo", "max_tokens": 0}
    )
    normalized = result["normalized"]
    assert normalized["parameters"]["reasoning_effort"] == "medium"
    assert normalized["parameters"]["max_tokens"] == 65536
