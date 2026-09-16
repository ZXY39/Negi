"""前端「请求超时时间(秒)」字段回归（Node 子进程驱动真实 org_graph_llm.js）。"""

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
    "requestTimeoutSeconds": "",
    "initialRequestTimeoutSeconds": "",
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
        "llm-binding-request-timeout-seconds": {"value": ""},
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


def test_request_timeout_field_renders_next_to_max_output_tokens() -> None:
    result = _run_node_script({})
    html = str(result["html"])
    assert 'id="llm-binding-request-timeout-seconds"' in html
    assert "请求超时时间(秒)" in html
    assert "默认 600" in html
    # 排在最大输出TOKEN之后（同一表单网格）。
    assert html.index('id="llm-binding-max-output-tokens"') < html.index(
        'id="llm-binding-request-timeout-seconds"'
    )


def test_request_timeout_blank_writes_no_parameter() -> None:
    result = _run_node_script({})
    assert result["message"] == ""
    parameters = result["payload"]["draft"]["parameters"]
    assert "request_timeout_seconds" not in parameters
    assert result["payload"]["requestTimeoutSeconds"] is None


def test_request_timeout_valid_value_written_to_draft_parameters() -> None:
    result = _run_node_script(
        {"requestTimeoutSeconds": "30"},
        {"llm-binding-request-timeout-seconds": {"value": "30"}},
    )
    assert result["message"] == ""
    parameters = result["payload"]["draft"]["parameters"]
    assert parameters["request_timeout_seconds"] == 30
    assert result["payload"]["requestTimeoutSeconds"] == 30


def test_request_timeout_accepts_fractional_seconds() -> None:
    result = _run_node_script(
        {"requestTimeoutSeconds": "90.5"},
        {"llm-binding-request-timeout-seconds": {"value": "90.5"}},
    )
    assert result["message"] == ""
    assert result["payload"]["draft"]["parameters"]["request_timeout_seconds"] == 90.5


def test_request_timeout_rejects_zero_negative_and_non_numeric() -> None:
    for bad in ("0", "-1", "abc"):
        result = _run_node_script(
            {"requestTimeoutSeconds": bad},
            {"llm-binding-request-timeout-seconds": {"value": bad}},
        )
        assert "请求超时时间" in str(result["message"])
