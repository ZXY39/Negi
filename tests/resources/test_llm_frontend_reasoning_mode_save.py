"""档位/Token 切换与思考预算必须能保存。

这两格没有对应的可见 JSON 输入行，保存判定又只认草稿文本差异与绑定字段：
改动只落在 editor state 里时，详情保存会走「无需保存」出口，操作员看到的
是「切了 Token、填了预算，点保存却说没有修改」。本用例驱动真实前端函数，
钉住三条契约：

- 切到 Token 后确认键进入「有改动」态，且保存真的 PUT 配置；
- 预算输入按解析后的数写进草稿（16K → 16000）；
- 只换写法（8K 与 8000）不算改动，不产生空写入。
"""

from __future__ import annotations

import json
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

BASE_DRAFT = {
    "provider_id": "demo-provider",
    "capability": "chat",
    "auth_mode": "api_key",
    "api_key": "live-key",
    "default_model": "live-model",
    "parameters": {"context_window_tokens": 30001},
    "extra_headers": {},
    "extra_options": {},
}

_COMMON = """
const fs = require("fs");
const vm = require("vm");

const DRAFT_TEXT = __DRAFT__;

global.window = global;
global.window.addEventListener = () => {};
const elements = {
  "llm-model-key-input": { value: "demo_key" },
  "llm-provider-select": { value: "demo-provider" },
  "llm-json-editor": { value: DRAFT_TEXT },
  "llm-binding-retry-on": { value: "network,429" },
  "llm-binding-retry-count": { value: "0" },
  "llm-binding-single-api-key-max-concurrency": { value: "" },
  "llm-binding-context-window-tokens": { value: "30001" },
  "llm-binding-image-multimodal-enabled": { checked: false },
  "llm-binding-reasoning-effort": { value: "medium" },
  "llm-binding-reasoning-budget": { value: "" },
  "llm-binding-max-output-tokens": { value: "65536" },
  "llm-binding-request-timeout-seconds": { value: "" },
  "llm-create-name-input": { value: "demo-config", addEventListener: () => {} },
  "llm-bindings-list": { innerHTML: "", addEventListener: () => {} },
  "llm-editor-shell": { innerHTML: "", addEventListener: () => {}, querySelectorAll: () => [] },
  "llm-editor-backdrop": { addEventListener: () => {} },
  "model-roles-cancel-btn": {},
};
global.document = {
  getElementById: (id) => elements[id] || null,
  querySelector: () => ({ querySelectorAll: () => [] }),
  addEventListener: () => {},
};
const bindingRow = {
  key: "demo_key",
  capability: "chat",
  config_id: "cfg-1",
  llm_config_id: "cfg-1",
  retry_on: ["network", "429"],
  retry_count: 0,
  single_api_key_max_concurrency: null,
  context_window_tokens: 30001,
  image_multimodal_enabled: false,
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
    bindings: [bindingRow],
    bindingMap: { demo_key: bindingRow },
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
      jsonText: DRAFT_TEXT,
      initialJsonText: DRAFT_TEXT,
      retryOn: ["network", "429"],
      initialRetryOn: ["network", "429"],
      retryCount: 0,
      initialRetryCount: 0,
      singleApiKeyMaxConcurrency: "",
      initialSingleApiKeyMaxConcurrency: "",
      contextWindowTokens: "30001",
      initialContextWindowTokens: "30001",
      imageMultimodalEnabled: false,
      initialImageMultimodalEnabled: false,
      reasoningEffort: "medium",
      initialReasoningEffort: "medium",
      reasoningInputMode: "level",
      initialReasoningInputMode: "level",
      reasoningBudgetTokens: __BUDGET__,
      initialReasoningBudgetTokens: __BUDGET__,
      maxOutputTokens: "65536",
      initialMaxOutputTokens: "65536",
      requestTimeoutSeconds: "",
      initialRequestTimeoutSeconds: "",
      validation: null,
      probe: null,
      busy: false,
      modelList: null,
    },
    eventsBound: false,
  },
};
global.U = {};
const calls = { validate: 0, probe: 0, create: 0, updateConfig: 0, updateBinding: 0 };
let savedConfigDraft = null;
global.ApiClient = {
  validateLlmDraft: async () => {
    calls.validate += 1;
    return { valid: true };
  },
  probeLlmDraft: async () => {
    calls.probe += 1;
    return { success: true, message: "ok" };
  },
  createLlmBinding: async () => {
    calls.create += 1;
    return { item: {}, runtimeRefresh: null };
  },
  updateLlmConfig: async (_configId, draft) => {
    calls.updateConfig += 1;
    savedConfigDraft = draft;
    return { item: {}, runtimeRefresh: null };
  },
  updateLlmBinding: async () => {
    calls.updateBinding += 1;
    return { item: {}, runtimeRefresh: null };
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
  "window.__llmTestHooks = {\\n    handleDetailSave,\\n    handleBindingReasoningBudgetInput,\\n    applyReasoningInputMode,"
);
vm.runInThisContext(code);
const hooks = window.__llmTestHooks;
"""


def _run_node_script(draft: dict, editor_budget: str, body: str) -> dict[str, object]:
    if not shutil.which("node"):
        pytest.skip("node is required to run this frontend harness")
    prelude = (
        _COMMON.replace("__DRAFT__", json.dumps(json.dumps(draft)))
        .replace("__BUDGET__", json.dumps(editor_budget))
    )
    completed = subprocess.run(
        ["node", "-"],
        input=prelude + textwrap.dedent(body),
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=True,
        cwd=REPO_ROOT,
    )
    return json.loads(completed.stdout.strip())


def test_switching_to_token_marks_dirty_and_saves() -> None:
    result = _run_node_script(
        BASE_DRAFT,
        "8192",
        """
        (async () => {
          const dirtyBefore = hooks.llmEditorDirty();
          hooks.applyReasoningInputMode("token");
          const dirtyAfter = hooks.llmEditorDirty();
          await hooks.handleDetailSave();
          console.log(JSON.stringify({
            dirtyBefore,
            dirtyAfter,
            calls,
            mode: savedConfigDraft?.parameters?.reasoning_input_mode,
          }));
        })().catch((error) => {
          console.log(JSON.stringify({ error: error.message || String(error) }));
          process.exit(1);
        });
        """,
    )

    assert result["dirtyBefore"] is False
    assert result["dirtyAfter"] is True
    assert result["calls"]["updateConfig"] == 1
    assert result["mode"] == "token"


def test_editing_budget_tokens_saves_parsed_value() -> None:
    result = _run_node_script(
        BASE_DRAFT,
        "8192",
        """
        (async () => {
          hooks.applyReasoningInputMode("token");
          elements["llm-binding-reasoning-budget"].value = "16K";
          hooks.handleBindingReasoningBudgetInput();
          await hooks.handleDetailSave();
          console.log(JSON.stringify({
            calls,
            mode: savedConfigDraft?.parameters?.reasoning_input_mode,
            budget: savedConfigDraft?.parameters?.reasoning_budget_tokens,
          }));
        })().catch((error) => {
          console.log(JSON.stringify({ error: error.message || String(error) }));
          process.exit(1);
        });
        """,
    )

    assert result["calls"]["updateConfig"] == 1
    assert result["mode"] == "token"
    assert result["budget"] == 16000


def test_budget_notation_change_alone_is_not_dirty() -> None:
    draft = {
        **BASE_DRAFT,
        "parameters": {**BASE_DRAFT["parameters"], "reasoning_budget_tokens": 8000},
    }
    result = _run_node_script(
        draft,
        "8000",
        """
        (async () => {
          elements["llm-binding-reasoning-budget"].value = "8K";
          hooks.handleBindingReasoningBudgetInput();
          const dirty = hooks.llmEditorDirty();
          await hooks.handleDetailSave();
          console.log(JSON.stringify({ dirty, calls }));
        })().catch((error) => {
          console.log(JSON.stringify({ error: error.message || String(error) }));
          process.exit(1);
        });
        """,
    )

    assert result["dirty"] is False
    assert result["calls"]["updateConfig"] == 0


def test_switching_back_to_level_restores_clean_state() -> None:
    # 抽屉打开时草稿已经被 withThinkingOutputParameterDefaults 归一化（两格默认值显式入草稿），
    # 所以切到 Token 再切回档位，文本应该原样回来，确认键不亮、保存不写。
    draft = {
        **BASE_DRAFT,
        "parameters": {
            **BASE_DRAFT["parameters"],
            "reasoning_input_mode": "level",
            "reasoning_budget_tokens": 8192,
        },
    }
    result = _run_node_script(
        draft,
        "8192",
        """
        (async () => {
          const pretty = JSON.stringify(JSON.parse(DRAFT_TEXT), null, 2);
          S.llmCenter.editor.jsonText = pretty;
          S.llmCenter.editor.initialJsonText = pretty;
          elements["llm-json-editor"].value = pretty;
          hooks.applyReasoningInputMode("token");
          const dirtyMidway = hooks.llmEditorDirty();
          hooks.applyReasoningInputMode("level");
          const dirtyAfter = hooks.llmEditorDirty();
          await hooks.handleDetailSave();
          console.log(JSON.stringify({ dirtyMidway, dirtyAfter, calls }));
        })().catch((error) => {
          console.log(JSON.stringify({ error: error.message || String(error) }));
          process.exit(1);
        });
        """,
    )

    assert result["dirtyMidway"] is True
    assert result["dirtyAfter"] is False
    assert result["calls"]["updateConfig"] == 0
