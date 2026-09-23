from __future__ import annotations

import json
import subprocess
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

_STUB_PRELUDE = """
const fs = require("fs");
const vm = require("vm");

const elementsById = {};
function makeEl() {
  return {
    innerHTML: "", value: "", hidden: false, open: false, checked: false,
    style: {}, dataset: {}, listeners: {},
    classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
    querySelector() { return null; },
    querySelectorAll() { return []; },
    setAttribute() {}, getAttribute() { return null; },
    append() {}, replaceWith() {}, remove() {}, focus() {}, select() {},
    getBoundingClientRect() { return { top: 0, bottom: 0, left: 0, right: 0, width: 0, height: 0 }; },
    addEventListener(type, handler) {
      if (!this.listeners[type]) this.listeners[type] = [];
      this.listeners[type].push(handler);
    },
  };
}
global.document = {
  getElementById(id) {
    if (!elementsById[id]) elementsById[id] = makeEl();
    return elementsById[id];
  },
  querySelector() { return makeEl(); },
  querySelectorAll() { return []; },
  createElement() { return makeEl(); },
  addEventListener() {},
  body: makeEl(),
};
global.window = global;
global.window.addEventListener = () => {};
global.window.innerWidth = 1440;

const DRAFT = {
  provider_id: "openai", capability: "chat", auth_mode: "api_key",
  display_name: "OpenAI Chat", api_key: "sk-keep",
  base_url: "https://api.test/v1", default_model: "gpt-4o",
  parameters: { context_window_tokens: 128000 },
  extra_headers: {}, extra_options: {},
};
const BINDING = {
  key: "demo", name: "我的配置", config_id: "cfg-1", capability: "chat",
  retry_on: ["network", "429"], retry_count: 0, image_multimodal_enabled: false,
};

global.S = {
  modelCatalog: {},
  llmCenter: {
    loading: false, saving: false, error: "",
    templates: [
      { provider_id: "openai", display_name: "OpenAI Chat" },
      { provider_id: "responses", display_name: "OpenAI Responses" },
    ],
    templateMap: {}, templateDetailMap: {},
    bindings: [BINDING], bindingMap: { demo: BINDING },
    routes: { ceo: [], execution: [], inspection: [] },
    roleIterations: {}, roleConcurrency: {},
    editor: {
      open: true, mode: "detail", bindingKey: "demo", configId: "cfg-1", modelKey: "demo",
      providerId: "openai",
      baseUrl: DRAFT.base_url, apiKey: DRAFT.api_key, defaultModel: DRAFT.default_model,
      jsonText: JSON.stringify(DRAFT, null, 2),
      initialJsonText: JSON.stringify(DRAFT, null, 2),
      retryOn: ["network", "429"], retryCount: 0,
      singleApiKeyMaxConcurrency: "",
      contextWindowTokens: "128000", initialContextWindowTokens: "128000",
      imageMultimodalEnabled: false, initialImageMultimodalEnabled: false,
      reasoningEffort: "medium", initialReasoningEffort: "medium",
      maxOutputTokens: "65536", initialMaxOutputTokens: "65536",
      requestTimeoutSeconds: "", initialRequestTimeoutSeconds: "",
      validation: null, probe: null, modelList: null,
    },
    eventsBound: true,
  },
};
global.U = {};
global.ApiClient = { getLlmTemplate: async () => null };
global.showToast = () => {};
global.requestInlineConfirm = async () => ({ confirmed: false });
global.esc = (value) => String(value ?? "");
global.EMPTY_MODEL_ROLES = () => ({ ceo: [], execution: [], inspection: [], memory: [] });
global.DEFAULT_ROLE_ITERATIONS = () => ({ ceo: null, execution: null, inspection: null });
global.DEFAULT_ROLE_CONCURRENCY = () => ({ ceo: null, execution: null, inspection: null });
global.DEFAULT_MODEL_DEFAULTS = () => ({ ceo: "", execution: "", inspection: "", memory: "" });
global.normalizeAllModelRoles = (value) => value;
global.normalizeRoleIterations = (value) => value;
global.normalizeRoleConcurrency = (value) => value;
global.cloneModelRoles = (value) => value;
global.cloneRoleIterations = (value) => value;
global.cloneRoleConcurrency = (value) => value;
global.syncModelRoleDraftState = () => {};
global.MODEL_SCOPES = [];
global.modelScopeChain = () => [];
global.modelScopeIterations = () => 0;
global.modelScopeConcurrency = () => 0;
global.renderRoleLimitControl = () => "";
global.modelRefItem = () => null;
global.normalizeModelRoleChain = (value) => value;
global.hint = () => {};
global.setDrawerOpen = () => {};
global.icons = () => {};

function el(id) { return global.document.getElementById(id); }
function shellHtml() { return el("llm-editor-shell").innerHTML; }

let code = fs.readFileSync("g3ku/web/frontend/org_graph_llm.js", "utf8");
code = code.replace(
  "window.__llmTestHooks = {",
  "window.__llmTestHooks = {\\n    handleProviderChange,\\n    enterModelNameEditMode,"
);
vm.runInThisContext(code);
"""


def _run_node_script(script: str) -> dict[str, object]:
    completed = subprocess.run(
        ["node", "-"],
        input=textwrap.dedent(_STUB_PRELUDE) + textwrap.dedent(script),
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=True,
        cwd=REPO_ROOT,
    )
    return json.loads(completed.stdout.strip())


def test_detail_editor_header_renders_protocol_select() -> None:
    result = _run_node_script(
        """
        window.renderModelDetail();
        console.log(JSON.stringify({ detailHtml: shellHtml() }));
        """
    )

    detail_html = str(result["detailHtml"])
    assert "llm-edit-protocol-field" in detail_html
    assert "llm-provider-select" in detail_html
    assert 'value="openai" selected' in detail_html
    assert "OpenAI Responses" in detail_html


def test_detail_provider_switch_rewrites_draft_and_marks_config_changed() -> None:
    result = _run_node_script(
        """
        window.renderModelDetail();
        el("llm-json-editor").value = global.S.llmCenter.editor.jsonText;
        el("llm-provider-select").value = "responses";

        window.__llmTestHooks.handleProviderChange().then(() => {
          const editor = global.S.llmCenter.editor;
          const parsed = JSON.parse(editor.jsonText);
          console.log(JSON.stringify({
            provider: parsed.provider_id,
            api_key: parsed.api_key,
            base_url: parsed.base_url,
            model: parsed.default_model,
            ctx: parsed.parameters.context_window_tokens,
            configChanged: editor.jsonText !== editor.initialJsonText,
          }));
        });
        """
    )

    assert result["provider"] == "responses"
    assert result["api_key"] == "sk-keep"
    assert result["base_url"] == "https://api.test/v1"
    assert result["model"] == "gpt-4o"
    assert result["ctx"] == 128000
    assert result["configChanged"] is True


def test_in_progress_rename_survives_provider_switch_rerender() -> None:
    result = _run_node_script(
        """
        window.renderModelDetail();
        window.__llmTestHooks.enterModelNameEditMode();

        const nameInput = el("llm-edit-name-input");
        const typedHtml = shellHtml();
        nameInput.listeners.input[0]({ target: { value: "改名中" } });
        el("llm-json-editor").value = global.S.llmCenter.editor.jsonText;
        el("llm-provider-select").value = "responses";

        window.__llmTestHooks.handleProviderChange().then(() => {
          console.log(JSON.stringify({
            typedHtml,
            afterHtml: shellHtml(),
            editName: global.S.llmCenter.editor.editName,
          }));
        });
        """
    )

    typed_html = str(result["typedHtml"])
    assert 'id="llm-edit-name-input"' in typed_html
    assert 'data-original-value="我的配置"' in typed_html
    assert 'id="llm-edit-name-display"' not in typed_html

    after_html = str(result["afterHtml"])
    assert str(result["editName"]) == "改名中"
    assert 'id="llm-edit-name-input" class="resource-search llm-edit-name-input" type="text" maxlength="40" autocomplete="off" value="改名中"' in after_html
    assert 'data-original-value="我的配置"' in after_html
    assert "llm-provider-select" in after_html


def test_cancel_rename_restores_name_display() -> None:
    result = _run_node_script(
        """
        window.renderModelDetail();
        window.__llmTestHooks.enterModelNameEditMode();
        el("llm-edit-name-input").listeners.keydown[0]({ key: "Escape", preventDefault() {} });
        console.log(JSON.stringify({ afterHtml: shellHtml() }));
        """
    )

    after_html = str(result["afterHtml"])
    assert 'id="llm-edit-name-display"' in after_html
    assert 'id="llm-edit-name-input"' not in after_html
    assert "我的配置" in after_html
