"""保存必须复用刚成功的连接探测。

探测现在会向供应商真发一次推理请求（`g3ku/llm_config/probe_strategies.py`），而低
RPM 端点上「测试连接 → 保存修改」的第二次请求会自己撞进同一个限流窗口，表现为
测试全过、保存被挡。同一份 JSON 文本在窗口内刚验过就不再打一次。
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

_PRELUDE = """
const fs = require("fs");
const vm = require("vm");

const DRAFT_TEXT = %s;

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
      jsonText: DRAFT_TEXT,
      initialJsonText: DRAFT_TEXT,
      retryOn: ["network", "429"],
      retryCount: 0,
      singleApiKeyMaxConcurrency: "",
      contextWindowTokens: "30001",
      imageMultimodalEnabled: false,
      initialImageMultimodalEnabled: false,
      validation: null,
      probe: null,
      probedJsonText: "",
      probedAt: 0,
      busy: false,
      modelList: null,
    },
    eventsBound: false,
  },
};
global.U = {};
const calls = { probe: 0, create: 0 };
global.ApiClient = {
  validateLlmDraft: async () => ({ valid: true }),
  probeLlmDraft: async () => {
    calls.probe += 1;
    return { success: true, message: "ok" };
  },
  createLlmBinding: async () => {
    calls.create += 1;
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

function seedProbe(jsonText, ageMs) {
  const editor = global.S.llmCenter.editor;
  editor.probe = { success: true, message: "ok" };
  editor.validation = { valid: true };
  editor.probedJsonText = jsonText;
  editor.probedAt = Date.now() - ageMs;
}

let code = fs.readFileSync("g3ku/web/frontend/org_graph_llm.js", "utf8");
code = code.replace(
  "window.__llmTestHooks = {",
  "window.__llmTestHooks = {\\n    handleCreateSave,\\n    hasFreshProbeFor,\\n    probeDraft,"
);
vm.runInThisContext(code);
""" % json.dumps(DRAFT_TEXT)


def _run_node_script(body: str) -> dict[str, object]:
    completed = subprocess.run(
        ["node", "-"],
        input=_PRELUDE + textwrap.dedent(body),
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=True,
        cwd=REPO_ROOT,
    )
    return json.loads(completed.stdout.strip())


def test_fresh_successful_probe_is_reused_by_save() -> None:
    result = _run_node_script(
        """
        (async () => {
          seedProbe(DRAFT_TEXT, 2000);
          const covered = window.__llmTestHooks.hasFreshProbeFor(DRAFT_TEXT);
          await window.__llmTestHooks.handleCreateSave();
          console.log(JSON.stringify({ covered, calls }));
        })().catch((error) => {
          console.log(JSON.stringify({ error: error.message || String(error) }));
          process.exit(1);
        });
        """
    )

    assert result["covered"] is True
    assert result["calls"]["probe"] == 0
    assert result["calls"]["create"] == 1


def test_stale_probe_is_not_reused() -> None:
    result = _run_node_script(
        """
        (async () => {
          seedProbe(DRAFT_TEXT, 61000);
          const covered = window.__llmTestHooks.hasFreshProbeFor(DRAFT_TEXT);
          await window.__llmTestHooks.handleCreateSave();
          console.log(JSON.stringify({ covered, calls }));
        })().catch((error) => {
          console.log(JSON.stringify({ error: error.message || String(error) }));
          process.exit(1);
        });
        """
    )

    assert result["covered"] is False
    assert result["calls"]["probe"] == 1
    assert result["calls"]["create"] == 1


def test_probe_for_other_text_is_not_reused() -> None:
    result = _run_node_script(
        """
        (async () => {
          seedProbe(DRAFT_TEXT.replace("live-key", "other-key"), 1000);
          const covered = window.__llmTestHooks.hasFreshProbeFor(DRAFT_TEXT);
          await window.__llmTestHooks.handleCreateSave();
          console.log(JSON.stringify({ covered, calls }));
        })().catch((error) => {
          console.log(JSON.stringify({ error: error.message || String(error) }));
          process.exit(1);
        });
        """
    )

    assert result["covered"] is False
    assert result["calls"]["probe"] == 1
    assert result["calls"]["create"] == 1


def test_successful_probe_records_the_text_it_covered() -> None:
    result = _run_node_script(
        """
        (async () => {
          const editor = global.S.llmCenter.editor;
          const draft = JSON.parse(DRAFT_TEXT);
          const ok = await window.__llmTestHooks.probeDraft(draft);
          console.log(JSON.stringify({
            ok,
            probeCalls: calls.probe,
            recorded: editor.probedJsonText === DRAFT_TEXT,
            fresh: window.__llmTestHooks.hasFreshProbeFor(DRAFT_TEXT),
          }));
        })().catch((error) => {
          console.log(JSON.stringify({ error: error.message || String(error) }));
          process.exit(1);
        });
        """
    )

    assert result["ok"] is True
    assert result["probeCalls"] == 1
    assert result["recorded"] is True
    assert result["fresh"] is True
