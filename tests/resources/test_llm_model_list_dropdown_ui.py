"""模型ID右侧「获取模型列表」的悬浮下拉：挂在 body 上、贴着模型ID框、首行是筛选 + 收起。"""

from __future__ import annotations

import json
import subprocess
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


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


_STUB_PRELUDE = """
const fs = require("fs");
const vm = require("vm");

const elements = {};
function makeEl(id, rect) {
  return {
    id: id || "",
    innerHTML: "",
    value: "",
    hidden: false,
    style: {},
    dataset: {},
    offsetHeight: 200,
    children: [],
    _queries: {},
    classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
    addEventListener() {},
    setAttribute() {},
    getAttribute() { return null; },
    querySelector(selector) {
      if (!this._queries[selector]) this._queries[selector] = makeEl();
      return this._queries[selector];
    },
    querySelectorAll() { return []; },
    getBoundingClientRect() { return rect || { top: 0, bottom: 0, left: 0, right: 0, width: 0, height: 0 }; },
    appendChild(child) {
      this.children.push(child);
      if (child.id) elements[child.id] = child;
      return child;
    },
  };
}

const body = makeEl("body");
global.document = {
  getElementById(id) { return elements[id] || null; },
  querySelector() { return null; },
  createElement() { return makeEl(""); },
  addEventListener() {},
  body,
};
global.window = global;
global.window.addEventListener = () => {};
global.window.innerWidth = 1440;
global.window.innerHeight = 900;

elements["llm-binding-default-model"] = makeEl("llm-binding-default-model", {
  top: 100, bottom: 138, left: 40, right: 340, width: 300, height: 38,
});
elements["llm-binding-default-model"].value = "gpt-4o";
elements["llm-provider-select"] = makeEl("llm-provider-select");
elements["llm-provider-select"].value = "demo";
elements["llm-json-editor"] = makeEl("llm-json-editor");
elements["llm-json-editor"].value = JSON.stringify({
  provider_id: "demo", capability: "chat", auth_mode: "api_key",
  api_key: "sk-demo", base_url: "https://api.demo/v1", default_model: "gpt-4o",
  parameters: { context_window_tokens: 200000, reasoning_effort: "medium", max_tokens: 65536 },
  extra_headers: {}, extra_options: {},
});

global.S = {
  modelCatalog: {},
  llmCenter: {
    loading: false, saving: false, error: "",
    templates: [{ provider_id: "demo", display_name: "Demo" }],
    templateMap: {}, templateDetailMap: {},
    bindings: [], bindingMap: {},
    routes: { ceo: [], execution: [], inspection: [] },
    roleIterations: {}, roleConcurrency: {},
    editor: {
      open: true, mode: "detail", bindingKey: "demo", configId: "cfg-1",
      providerId: "demo", baseUrl: "https://api.demo/v1", apiKey: "sk-demo",
      defaultModel: "gpt-4o", jsonText: elements["llm-json-editor"].value,
      initialJsonText: elements["llm-json-editor"].value,
      retryOn: ["network", "429"], retryCount: 0,
      singleApiKeyMaxConcurrency: "", contextWindowTokens: "200K",
      imageMultimodalEnabled: false,
      reasoningEffort: "medium", maxOutputTokens: "65536", requestTimeoutSeconds: "",
      validation: null, probe: null, modelList: null,
    },
    eventsBound: true,
  },
};
global.U = {};
global.ApiClient = {
  listLlmDraftModels: async () => ({ success: true, models: ["gpt-4o", "gpt-4.1"] }),
};
global.showToast = () => {};
global.requestInlineConfirm = async () => ({ confirmed: false });
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

const code = fs.readFileSync("g3ku/web/frontend/org_graph_llm.js", "utf8");
vm.runInThisContext(code);
"""


def test_fetch_model_list_renders_floating_dropdown_under_model_id() -> None:
    result = _run_node_script(
        """
        (async () => {
          await window.__llmTestHooks.handleFetchModelList();
          const panel = document.getElementById("llm-model-list-panel");
          console.log(JSON.stringify({
            appendedToBody: document.body.children.includes(panel),
            classes: panel.className,
            hidden: panel.hidden,
            top: panel.style.top,
            left: panel.style.left,
            html: panel.innerHTML,
            itemsHost: panel.querySelector(".llm-model-list-items").innerHTML,
          }));
        })();
        """
    )

    assert result["appendedToBody"] is True
    assert "llm-model-list-panel--floating" in str(result["classes"])
    assert result["hidden"] is False
    # 贴着模型ID框下沿（rect.bottom 138 + 6），宽度对齐输入框。
    assert result["top"] == "144px"
    assert result["left"] == "40px"
    html = str(result["html"])
    assert 'id="llm-model-list-filter"' in html
    # 首行只有 筛选框 + 收起按钮，且收起在右侧。
    assert html.index("llm-model-list-filter") < html.index("model-list-close")
    assert "llm-model-list-toolbar" in html
    assert "gpt-4.1" in str(result["itemsHost"])


def test_model_list_selection_writes_default_model_and_closes_dropdown() -> None:
    result = _run_node_script(
        """
        (async () => {
          await window.__llmTestHooks.handleFetchModelList();
          window.__llmTestHooks.applyModelListItem("gpt-4.1");
          const editor = global.S.llmCenter.editor;
          const panel = document.getElementById("llm-model-list-panel");
          console.log(JSON.stringify({
            modelList: editor.modelList,
            hidden: panel.hidden,
            inputValue: document.getElementById("llm-binding-default-model").value,
            draftModel: JSON.parse(document.getElementById("llm-json-editor").value).default_model,
          }));
        })();
        """
    )

    assert result["modelList"] is None
    assert result["hidden"] is True
    assert result["inputValue"] == "gpt-4.1"
    assert result["draftModel"] == "gpt-4.1"


def test_model_list_panel_flips_above_when_it_would_overflow_the_viewport() -> None:
    result = _run_node_script(
        """
        (async () => {
          document.getElementById("llm-binding-default-model").getBoundingClientRect = () => ({
            top: 820, bottom: 858, left: 200, right: 500, width: 300, height: 38,
          });
          await window.__llmTestHooks.handleFetchModelList();
          const panel = document.getElementById("llm-model-list-panel");
          console.log(JSON.stringify({ top: panel.style.top }));
        })();
        """
    )

    # 下方放不下（858 + 6 + 200 > 900 - 8），翻到输入框上方：820 - 200 - 6 = 614。
    assert result["top"] == "614px"
