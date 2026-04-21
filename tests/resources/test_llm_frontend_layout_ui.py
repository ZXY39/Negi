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


def test_llm_binding_editor_uses_styled_image_multimodal_checkbox_copy_and_layout() -> None:
    llm_js = (REPO_ROOT / "g3ku/web/frontend/org_graph_llm.js").read_text(encoding="utf-8")
    llm_css = (REPO_ROOT / "g3ku/web/frontend/org_graph.css").read_text(encoding="utf-8")

    assert "是否为图像多模态" in llm_js
    assert "llm-image-checkbox-field" in llm_js
    assert "llm-image-checkbox-control" in llm_js
    assert "llm-image-checkbox-indicator" in llm_js
    assert "Image Multimodal" not in llm_js
    assert "communication-toggle llm-image-toggle-control" not in llm_js
    assert ".llm-image-checkbox-control" in llm_css
    assert ".llm-image-checkbox-indicator" in llm_css


def test_llm_binding_editor_uses_header_and_detail_row_image_multimodal_layout_variants() -> None:
    llm_js = (REPO_ROOT / "g3ku/web/frontend/org_graph_llm.js").read_text(encoding="utf-8")
    llm_css = (REPO_ROOT / "g3ku/web/frontend/org_graph.css").read_text(encoding="utf-8")

    assert 'renderImageMultimodalField({ layout: "header" })' in llm_js
    assert 'editor.mode === "create"' in llm_js
    assert "llm-form-grid--binding-detail-policy" in llm_js
    assert "llm-binding-concurrency-actions" in llm_js
    assert 'editor.mode === "create" ? "" : renderImageMultimodalField()' in llm_js
    assert ".llm-image-checkbox-field--header" in llm_css
    assert ".llm-image-checkbox-spacer" in llm_css
    assert ".llm-form-grid.llm-form-grid--binding-detail-policy" in llm_css
    assert "grid-column: 3 / 4;" in llm_css
    assert ".llm-binding-concurrency-actions" in llm_css


def test_llm_detail_editor_renders_image_multimodal_before_concurrency_button_in_detail_row() -> None:
    result = _run_node_script(
        """
        const fs = require("fs");
        const vm = require("vm");

        global.window = global;
        global.window.addEventListener = () => {};
        const elements = {
          "llm-bindings-list": { innerHTML: "", addEventListener: () => {} },
          "llm-editor-shell": { innerHTML: "", addEventListener: () => {} },
          "llm-editor-backdrop": { addEventListener: () => {} },
          "llm-editor-panel": { addEventListener: () => {} },
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
              single_api_key_max_concurrency: 2,
              context_window_tokens: 32000,
              image_multimodal_enabled: false,
            }],
            bindingMap: {
              demo_key: {
                key: "demo_key",
                capability: "chat",
                config_id: "cfg-1",
                llm_config_id: "cfg-1",
                retry_on: ["network", "429", "5xx"],
                retry_count: 0,
                single_api_key_max_concurrency: 2,
                context_window_tokens: 32000,
                image_multimodal_enabled: false,
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
              jsonText: "{\\"provider_id\\":\\"demo-provider\\"}",
              initialJsonText: "{}",
              retryOn: ["network", "429", "5xx"],
              retryCount: 0,
              singleApiKeyMaxConcurrency: "2",
              contextWindowTokens: "32000",
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
        global.ApiClient = {
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
        vm.runInThisContext(code);

        window.renderModelDetail();

        const html = elements["llm-editor-shell"].innerHTML;
        console.log(JSON.stringify({
          html,
          imageIndex: html.indexOf("是否为图像多模态"),
          buttonIndex: html.indexOf("测试最大并发数"),
          detailGridIndex: html.indexOf("llm-form-grid llm-form-grid--binding-detail-policy"),
        }));
        """
    )

    assert result["detailGridIndex"] >= 0
    assert result["imageIndex"] >= 0
    assert result["buttonIndex"] >= 0
    assert result["imageIndex"] < result["buttonIndex"]
    assert 'class="resource-field llm-image-checkbox-field"' in result["html"]


def test_memory_role_fixed_concurrency_reuses_segmented_label_shell_styling() -> None:
    app_js = (REPO_ROOT / "g3ku/web/frontend/org_graph_app.js").read_text(encoding="utf-8")
    app_css = (REPO_ROOT / "g3ku/web/frontend/org_graph.css").read_text(encoding="utf-8")

    assert "model-role-limit-fixed-track" in app_js
    assert "llm-segmented-label model-role-limit-fixed-pill" in app_js
    assert "grid-column: 2 / 3;" in app_css
    assert ".model-role-limit-fixed-pill {" in app_css
