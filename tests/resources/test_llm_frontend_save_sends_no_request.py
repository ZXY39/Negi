"""保存不发真实请求，真实请求只由「测试连接」发起。

探测会向供应商真发一次推理请求（`g3ku/llm_config/probe_strategies.py`）。低 RPM 端点上
"保存前先自动测一次"会把健康的配置判成失败：探测自己那一发撞进限流或被上游拒掉，用户看
到的是"保存不了"，而模型与密钥都没问题。界面上保存只做字段校验与落盘。
"""

from __future__ import annotations

import json
import subprocess
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

DRAFT_TEXT = json.dumps(
    {
        "provider_id": "demo-provider",
        "capability": "chat",
        "auth_mode": "api_key",
        "api_key": "live-key",
        "default_model": "live-model",
        "parameters": {"context_window_tokens": 30001},
        "extra_headers": {},
        "extra_options": {},
    }
)

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
  "llm-create-name-input": { value: "demo-config", addEventListener: () => {} },
  "llm-bindings-list": { innerHTML: "", addEventListener: () => {} },
  "llm-editor-shell": { innerHTML: "", addEventListener: () => {} },
  "llm-editor-backdrop": { addEventListener: () => {} },
  "model-roles-cancel-btn": {},
};
global.document = {
  getElementById: (id) => elements[id] || null,
  querySelector: () => ({}),
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
    bindings: __MODE__ === "detail" ? [bindingRow] : [],
    bindingMap: __MODE__ === "detail" ? { demo_key: bindingRow } : {},
    routes: { ceo: [], execution: [], inspection: [], memory: [] },
    roleIterations: { ceo: null, execution: null, inspection: null },
    roleConcurrency: { ceo: null, execution: null, inspection: null },
    editor: {
      open: true,
      mode: __MODE__,
      bindingKey: __MODE__ === "detail" ? "demo_key" : "",
      configId: __MODE__ === "detail" ? "cfg-1" : "",
      modelKey: "demo_key",
      providerId: "demo-provider",
      jsonText: DRAFT_TEXT,
      // 详情态要真做出"配置被改过"的差异，否则保存会走"无需保存"那条出口。
      initialJsonText: __MODE__ === "detail" ? DRAFT_TEXT.replace("live-model", "old-model") : DRAFT_TEXT,
      retryOn: ["network", "429"],
      retryCount: 0,
      singleApiKeyMaxConcurrency: "",
      contextWindowTokens: "30001",
      reasoningEffort: "",
      maxOutputTokens: "",
      requestTimeoutSeconds: "",
      initialRequestTimeoutSeconds: "",
      imageMultimodalEnabled: false,
      initialImageMultimodalEnabled: false,
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
  updateLlmConfig: async () => {
    calls.updateConfig += 1;
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
  "window.__llmTestHooks = {\\n    handleCreateSave,\\n    handleDetailSave,\\n    probeDraft,"
);
vm.runInThisContext(code);
"""


def _run_node_script(mode: str, body: str) -> dict[str, object]:
    prelude = _COMMON.replace("__MODE__", json.dumps(mode)).replace("__DRAFT__", json.dumps(DRAFT_TEXT))
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


def test_create_save_sends_no_inference_request() -> None:
    result = _run_node_script(
        "create",
        """
        (async () => {
          await window.__llmTestHooks.handleCreateSave();
          console.log(JSON.stringify(calls));
        })().catch((error) => {
          console.log(JSON.stringify({ error: error.message || String(error) }));
          process.exit(1);
        });
        """
    )

    assert result["probe"] == 0
    assert result["create"] == 1


def test_detail_save_of_changed_config_sends_no_inference_request() -> None:
    result = _run_node_script(
        "detail",
        """
        (async () => {
          await window.__llmTestHooks.handleDetailSave();
          console.log(JSON.stringify(calls));
        })().catch((error) => {
          console.log(JSON.stringify({ error: error.message || String(error) }));
          process.exit(1);
        });
        """
    )

    assert result["probe"] == 0
    assert result["updateConfig"] == 1


def test_connection_test_button_still_sends_one_request() -> None:
    result = _run_node_script(
        "detail",
        """
        (async () => {
          const ok = await window.__llmTestHooks.probeDraft(JSON.parse(DRAFT_TEXT));
          console.log(JSON.stringify({ ok, probeCalls: calls.probe, validateCalls: calls.validate }));
        })().catch((error) => {
          console.log(JSON.stringify({ error: error.message || String(error) }));
          process.exit(1);
        });
        """
    )

    assert result["ok"] is True
    assert result["validateCalls"] == 1
    assert result["probeCalls"] == 1
