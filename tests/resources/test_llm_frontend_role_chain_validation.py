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
        cwd=REPO_ROOT,
        check=True,
    )
    return json.loads(completed.stdout.strip())


def test_org_graph_app_blocks_role_chain_save_until_required_scopes_are_filled() -> None:
    result = _run_node_script(
        """
        const fs = require("fs");
        const vm = require("vm");

        const APP_CODE = fs.readFileSync("g3ku/web/frontend/org_graph_app.js", "utf8");

        class StubElement {
          constructor(id = "") {
            this.id = id;
            this.hidden = false;
            this.disabled = false;
            this.value = "";
            this.innerHTML = "";
            this.textContent = "";
            this.attributes = {};
            this.style = {};
            this.scrollHeight = 48;
            this.dataset = {};
            this.classList = {
              add() {},
              remove() {},
              toggle() {},
              contains() { return false; },
            };
          }
          setAttribute(name, value) { this.attributes[name] = String(value); }
          getAttribute(name) { return this.attributes[name]; }
          removeAttribute(name) { delete this.attributes[name]; }
          addEventListener() {}
          querySelector() { return null; }
          querySelectorAll() { return []; }
          reportValidity() { return true; }
          setCustomValidity() {}
        }

        class StubHTMLElement extends StubElement {}
        class StubHTMLButtonElement extends StubHTMLElement {}
        class StubHTMLInputElement extends StubHTMLElement {}
        class StubHTMLTextAreaElement extends StubHTMLElement {}
        class StubHTMLSelectElement extends StubHTMLElement {}

        class StubDocument {
          constructor(elements) {
            this.elements = elements;
          }
          getElementById(id) { return this.elements[id] || null; }
          querySelector() { return null; }
          querySelectorAll() { return []; }
          addEventListener() {}
          createElement() { return new StubHTMLElement(); }
        }

        const elements = {
          "app-toast": new StubHTMLElement("app-toast"),
          "app-toast-title": new StubHTMLElement("app-toast-title"),
          "app-toast-text": new StubHTMLElement("app-toast-text"),
          "app-toast-progress": new StubHTMLElement("app-toast-progress"),
          "app-toast-progress-bar": new StubHTMLElement("app-toast-progress-bar"),
          "app-toast-close": new StubHTMLButtonElement("app-toast-close"),
          "sidebar-model-hint": new StubHTMLElement("sidebar-model-hint"),
          "model-roles-save-btn": new StubHTMLButtonElement("model-roles-save-btn"),
          "model-roles-cancel-btn": new StubHTMLButtonElement("model-roles-cancel-btn"),
          "model-search-input": new StubHTMLInputElement("model-search-input"),
          "model-list": new StubHTMLElement("model-list"),
          "model-role-editors": new StubHTMLElement("model-role-editors"),
          "model-refresh-btn": new StubHTMLButtonElement("model-refresh-btn"),
          "model-create-btn": new StubHTMLButtonElement("model-create-btn"),
          "llm-bindings-list": new StubHTMLElement("llm-bindings-list"),
          "llm-editor-shell": new StubHTMLElement("llm-editor-shell"),
          "llm-editor-backdrop": new StubHTMLElement("llm-editor-backdrop"),
          "llm-memory-settings-btn": new StubHTMLButtonElement("llm-memory-settings-btn"),
          "llm-config-create-btn": new StubHTMLButtonElement("llm-config-create-btn"),
          "ceo-input": new StubHTMLTextAreaElement("ceo-input"),
          "ceo-send-btn": new StubHTMLButtonElement("ceo-send-btn"),
          "ceo-attach-btn": new StubHTMLButtonElement("ceo-attach-btn"),
          "ceo-file-input": new StubHTMLInputElement("ceo-file-input"),
          "ceo-upload-list": new StubHTMLElement("ceo-upload-list"),
          "ceo-follow-up-queue": new StubHTMLElement("ceo-follow-up-queue"),
        };
        const document = new StubDocument(elements);
        let updateCallCount = 0;
        const toastCalls = [];
        const context = {
          console,
          setTimeout,
          clearTimeout,
          setInterval,
          clearInterval,
          queueMicrotask,
          navigator: { clipboard: { writeText: async () => {} } },
          location: { protocol: "http:", host: "localhost", pathname: "/org_graph.html" },
          localStorage: { getItem: () => null, setItem: () => {}, removeItem: () => {} },
          sessionStorage: { getItem: () => null, setItem: () => {}, removeItem: () => {} },
          document,
          window: {},
          Element: StubElement,
          HTMLElement: StubHTMLElement,
          HTMLButtonElement: StubHTMLButtonElement,
          HTMLInputElement: StubHTMLInputElement,
          HTMLTextAreaElement: StubHTMLTextAreaElement,
          HTMLSelectElement: StubHTMLSelectElement,
          URLSearchParams,
          URL,
          AbortController,
          fetch: async () => ({ ok: true, json: async () => ({}) }),
          lucide: { createIcons() {} },
          marked: { parse: (value) => String(value) },
          DOMPurify: { sanitize: (value) => String(value) },
          structuredClone: global.structuredClone,
          performance: { now: () => 0 },
          requestAnimationFrame: (callback) => {
            callback();
            return 1;
          },
          cancelAnimationFrame: () => {},
          WebSocket: function WebSocket() {},
          addEventListener() {},
          removeEventListener() {},
          ApiClient: {
            getActiveSessionId: () => "web:test",
            updateModelRoleChains: async () => {
              updateCallCount += 1;
              return {};
            },
          },
          icons: () => {},
          renderPendingCeoUploads: () => {},
          syncCeoAttachButton: () => {},
          syncCeoSessionActions: () => {},
          syncActiveCeoComposerDraft: () => {},
          syncCeoInputHeight: () => {},
          clearCeoComposerDraft: () => {},
          addMsg: () => {},
          showToast: (payload) => {
            toastCalls.push(payload);
          },
          patchCeoSessionRuntimeState: () => false,
          setCeoSessionSnapshotCache: () => ({}),
          createPendingCeoTurn: () => ({}),
          normalizeUploadList: (items) => Array.isArray(items) ? items : [],
          summarizeUploads: () => "",
          hasRenderableText: (value) => !!String(value || "").trim(),
          requestCeoPause: () => {},
        };
        context.window = context;
        vm.createContext(context);
        vm.runInContext(
          `${APP_CODE}
          this.__testExports = {
            S,
            handleModelRoleEditorAction,
          };`,
          context
        );

        const { S, handleModelRoleEditorAction } = context.__testExports;
        S.modelCatalog.loading = false;
        S.modelCatalog.saving = false;
        S.modelCatalog.error = "";
        S.modelCatalog.catalog = [{ key: "gpt-5.4", provider_model: "openai:gpt-5.4" }];
        S.modelCatalog.roles = { ceo: [], execution: [], inspection: [], memory: [] };
        S.modelCatalog.roleIterations = { ceo: 8, execution: 8, inspection: 8, memory: 5 };
        S.modelCatalog.roleConcurrency = { ceo: null, execution: null, inspection: null, memory: 1 };
        S.modelCatalog.roleEditing = true;
        S.modelCatalog.rolesDirty = true;
        S.modelCatalog.roleDrafts = {
          ceo: ["gpt-5.4"],
          execution: [],
          inspection: [],
          memory: [],
        };
        S.modelCatalog.roleIterationDrafts = { ceo: 8, execution: 8, inspection: 8, memory: 5 };
        S.modelCatalog.roleConcurrencyDrafts = { ceo: null, execution: null, inspection: null, memory: 1 };

        (async () => {
          await handleModelRoleEditorAction();
            console.log(JSON.stringify({
              updateCallCount,
              error: S.modelCatalog.error,
              toastText: elements["app-toast-text"].textContent,
            }));
        })().catch((error) => {
            console.log(JSON.stringify({ error: error.message || String(error), updateCallCount, toastText: elements["app-toast-text"].textContent }));
            process.exit(1);
          });
        """
    )

    assert result["updateCallCount"] == 0
    assert "执行Agent" in str(result["error"])
    assert "检验Agent" in str(result["error"])
    assert "执行Agent" in str(result["toastText"])
    assert "检验Agent" in str(result["toastText"])


def test_org_graph_llm_blocks_role_chain_save_until_required_scopes_are_filled() -> None:
    result = _run_node_script(
        """
        const fs = require("fs");
        const vm = require("vm");

        global.window = global;
        global.window.addEventListener = () => {};
        global.document = {
          getElementById: () => null,
          querySelector: () => null,
          addEventListener: () => {},
        };
        global.S = {
          modelCatalog: {
            roleEditing: true,
            rolesDirty: true,
            roleDrafts: {
              ceo: ["gpt-5.4"],
              execution: [],
              inspection: [],
              memory: [],
            },
            roleIterationDrafts: { ceo: 8, execution: 8, inspection: 8, memory: 5 },
            roleConcurrencyDrafts: { ceo: null, execution: null, inspection: null, memory: 1 },
          },
          llmCenter: {
            loading: false,
            saving: false,
            error: "",
            templates: [],
            templateMap: {},
            templateDetailMap: {},
            bindings: [],
            bindingMap: {},
            routes: { ceo: [], execution: [], inspection: [], memory: [] },
            roleIterations: { ceo: 8, execution: 8, inspection: 8, memory: 5 },
            roleConcurrency: { ceo: null, execution: null, inspection: null, memory: 1 },
            editor: {
              open: false,
              mode: "",
              bindingKey: "",
              configId: "",
              modelKey: "",
              providerId: "",
              jsonText: "",
              initialJsonText: "",
              retryOn: ["network", "429", "5xx"],
              retryCount: 0,
              singleApiKeyMaxConcurrency: "",
              contextWindowTokens: "",
              initialContextWindowTokens: "",
              imageMultimodalEnabled: false,
              initialImageMultimodalEnabled: false,
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
        let updateCallCount = 0;
        const toastCalls = [];
        global.ApiClient = {
          updateLlmRoutes: async () => {
            updateCallCount += 1;
            return {};
          },
          getLlmTemplates: async () => [],
          listLlmBindings: async () => ({
            items: [],
            routes: { ceo: [], execution: [], inspection: [], memory: [] },
            roleIterations: { ceo: 8, execution: 8, inspection: 8, memory: 5 },
            roleConcurrency: { ceo: null, execution: null, inspection: null, memory: 1 },
          }),
        };
        global.showToast = (payload) => { toastCalls.push(payload); };
        global.esc = (value) => String(value ?? "");
        global.MODEL_SCOPES = [
          { key: "ceo", label: "主Agent" },
          { key: "execution", label: "执行Agent" },
          { key: "inspection", label: "检验Agent" },
          { key: "memory", label: "记忆Agent" },
        ];
        global.EMPTY_MODEL_ROLES = () => ({ ceo: [], execution: [], inspection: [], memory: [] });
        global.DEFAULT_ROLE_ITERATIONS = () => ({ ceo: 8, execution: 8, inspection: 8, memory: 5 });
        global.DEFAULT_ROLE_CONCURRENCY = () => ({ ceo: null, execution: null, inspection: null, memory: 1 });
        global.DEFAULT_MODEL_DEFAULTS = () => ({ ceo: "", execution: "", inspection: "", memory: "" });
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
        global.startModelRoleEditing = () => {};
        global.cancelModelRoleEditing = () => {};
        global.syncRoleIterationDraftsFromInputs = () => {};
        global.normalizeModelRoleChain = (value) => Array.isArray(value) ? value : [];
        global.modelScopeIterations = (scope, source) => global.S.modelCatalog.roleIterationDrafts[scope];
        global.modelScopeConcurrency = (scope, source) => global.S.modelCatalog.roleConcurrencyDrafts[scope];
        global.renderAll = () => {};
        const code = fs.readFileSync("g3ku/web/frontend/org_graph_llm.js", "utf8");
        vm.runInThisContext(code);

        (async () => {
          await window.handleModelRoleEditorAction();
          console.log(JSON.stringify({
            updateCallCount,
            error: global.S.llmCenter.error,
            toasts: toastCalls,
          }));
        })().catch((error) => {
          console.log(JSON.stringify({ error: error.message || String(error), updateCallCount, toasts: toastCalls }));
          process.exit(1);
        });
        """
    )

    assert result["updateCallCount"] == 0
    assert "执行Agent" in str(result["error"])
    assert "检验Agent" in str(result["error"])
    assert any("执行Agent" in str(item.get("text") or "") for item in result["toasts"])
    assert any("检验Agent" in str(item.get("text") or "") for item in result["toasts"])
