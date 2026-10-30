"""模型配置页的 Apikey 读面：默认遮罩、查看/隐藏、口令窗口与草稿脏位。

这些用例把"查看不是配置改动"钉死：查看/隐藏只换 key 的显示值，`llmEditorDirty()` 必须
保持原判，否则操作员点一次「查看」就会被提示有未保存修改。
"""

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


_LLM_HARNESS = r"""
        const fs = require("fs");
        const vm = require("vm");

        function makeEl() {
          return {
            innerHTML: "", value: "", hidden: false, open: false,
            style: {}, dataset: {},
            classList: { add() {}, remove() {}, contains() { return false; } },
            querySelector() { return null; },
            querySelectorAll() { return []; },
            setAttribute() {}, getAttribute() { return null; },
            addEventListener() {},
          };
        }
        const elementsById = {};
        global.document = {
          getElementById(id) {
            if (!elementsById[id]) elementsById[id] = makeEl();
            return elementsById[id];
          },
          querySelector() { return makeEl(); },
          addEventListener() {},
        };
        global.window = global;
        global.window.addEventListener = () => {};
        global.toasts = [];
        global.showToast = (payload) => { global.toasts.push(payload || {}); };
        global.esc = (value) => String(value ?? "");
        global.EMPTY_MODEL_ROLES = () => ({ ceo: [], execution: [], inspection: [] });
        global.DEFAULT_ROLE_ITERATIONS = () => ({ ceo: null, execution: null, inspection: null });
        global.DEFAULT_ROLE_CONCURRENCY = () => ({ ceo: null, execution: null, inspection: null });
        global.DEFAULT_MODEL_DEFAULTS = () => ({ ceo: "", execution: "", inspection: "" });
        global.normalizeAllModelRoles = (value) => ({ ceo: [], execution: [], inspection: [], memory: [], ...(value || {}) });
        global.normalizeRoleIterations = (value) => ({ ceo: null, execution: null, inspection: null, ...(value || {}) });
        global.normalizeRoleConcurrency = (value) => ({ ceo: null, execution: null, inspection: null, ...(value || {}) });
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
        global.enhanceResourceSelects = () => {};
        global.requestInlineConfirm = async () => ({ confirmed: false });
        global.openConfirm = () => {};
        global.ApiClient = {};

        function detailEditor(overrides = {}) {
          const draft = {
            provider_id: "openai",
            capability: "chat",
            auth_mode: "api_key",
            api_key: "********",
            base_url: "https://api.test/v1",
            default_model: "gpt-4o",
            parameters: { context_window_tokens: 200000 },
            extra_headers: {},
            extra_options: {},
          };
          const json = JSON.stringify(draft, null, 2);
          return {
            open: true, mode: "detail", bindingKey: "demo", configId: "cfg-1", modelKey: "demo",
            providerId: "openai", baseUrl: "https://api.test/v1", apiKey: "********",
            apiKeyMasked: true, apiKeyMaskValue: "********", apiKeyCount: 2,
            apiKeyRevealed: false, revealedApiKey: "",
            defaultModel: "gpt-4o", jsonText: json, initialJsonText: json,
            retryOn: ["network", "429"], retryCount: 0, singleApiKeyMaxConcurrency: "",
            initialRetryOn: ["network", "429"], initialRetryCount: 0, initialSingleApiKeyMaxConcurrency: "",
            contextWindowTokens: "200K", initialContextWindowTokens: "200K",
            imageMultimodalEnabled: false, initialImageMultimodalEnabled: false,
            reasoningEffort: "medium", initialReasoningEffort: "medium",
            maxOutputTokens: "65536", initialMaxOutputTokens: "65536",
            requestTimeoutSeconds: "", initialRequestTimeoutSeconds: "",
            validation: null, probe: null, busy: false, modelList: null,
            ...overrides,
          };
        }

        function bootLlm(editor) {
          global.S = {
            modelCatalog: {},
            llmCenter: {
              loading: false, saving: false, error: "",
              templates: [{ provider_id: "openai", display_name: "OpenAI" }],
              templateMap: { openai: { provider_id: "openai", display_name: "OpenAI" } },
              templateDetailMap: {},
              bindings: [{ key: "demo", name: "Demo", llm_config_id: "cfg-1", record_missing: false }],
              bindingMap: {
                demo: {
                  key: "demo", name: "Demo", llm_config_id: "cfg-1", record_missing: false,
                  retry_on: ["network", "429"], retry_count: 0,
                  context_window_tokens: "200K",
                  image_multimodal_enabled: false,
                  single_api_key_max_concurrency: null,
                },
              },
              routes: {}, roleIterations: {}, roleConcurrency: {},
              editor,
              eventsBound: false,
            },
          };
          global.U = {};
          const code = fs.readFileSync("g3ku/web/frontend/org_graph_llm.js", "utf8");
          vm.runInThisContext(code);
        }
"""


def test_detail_render_shows_mask_and_reveal_toggle_only_when_masked() -> None:
    result = _run_node_script(
        _LLM_HARNESS
        + r"""
        bootLlm(detailEditor());
        window.renderModelDetail();
        const maskedHtml = global.document.getElementById("llm-editor-shell").innerHTML;

        const revealed = detailEditor({
          apiKey: "sk-plain", apiKeyMasked: false, apiKeyRevealed: true, revealedApiKey: "sk-plain",
          jsonText: JSON.stringify({ api_key: "sk-plain", provider_id: "openai" }, null, 2),
          initialJsonText: JSON.stringify({ api_key: "sk-plain", provider_id: "openai" }, null, 2),
        });
        global.S.llmCenter.editor = revealed;
        window.renderModelDetail();
        const revealedHtml = global.document.getElementById("llm-editor-shell").innerHTML;

        global.S.llmCenter.editor = detailEditor({ mode: "create", bindingKey: "", configId: "", apiKey: "" });
        window.renderModelDetail();
        const createHtml = global.document.getElementById("llm-editor-shell").innerHTML;

        console.log(JSON.stringify({ maskedHtml, revealedHtml, createHtml }));
        """
    )

    masked_html = str(result["maskedHtml"])
    assert 'value="********"' in masked_html
    assert 'data-llm-action="reveal-api-key"' in masked_html
    assert ">查看<" in masked_html

    revealed_html = str(result["revealedHtml"])
    assert 'value="sk-plain"' in revealed_html
    assert 'data-llm-action="hide-api-key"' in revealed_html
    assert ">隐藏<" in revealed_html
    assert 'data-llm-action="reveal-api-key"' not in revealed_html

    create_html = str(result["createHtml"])
    assert "llm-api-key-reveal-btn" not in create_html


def test_apply_revealed_key_keeps_the_editor_clean_and_hide_restores_mask() -> None:
    result = _run_node_script(
        _LLM_HARNESS
        + r"""
        bootLlm(detailEditor());
        window.__llmTestHooks.applyRevealedApiKey({
          item: { auth: { type: "api_key", api_key: "sk-plain" } },
          expiresInSeconds: 300,
        });
        const editor = global.S.llmCenter.editor;
        const afterReveal = {
          apiKey: editor.apiKey,
          masked: editor.apiKeyMasked,
          revealed: editor.apiKeyRevealed,
          jsonKey: JSON.parse(editor.jsonText).api_key,
          initialKey: JSON.parse(editor.initialJsonText).api_key,
          dirty: window.__llmTestHooks.llmEditorDirty(),
          toast: global.toasts[global.toasts.length - 1],
        };

        window.__llmTestHooks.hideEditorApiKey();
        const afterHide = {
          apiKey: editor.apiKey,
          masked: editor.apiKeyMasked,
          revealed: editor.apiKeyRevealed,
          jsonKey: JSON.parse(editor.jsonText).api_key,
          initialKey: JSON.parse(editor.initialJsonText).api_key,
          dirty: window.__llmTestHooks.llmEditorDirty(),
        };
        console.log(JSON.stringify({ afterReveal, afterHide }));
        """
    )

    after_reveal = result["afterReveal"]
    assert after_reveal["apiKey"] == "sk-plain"
    assert after_reveal["masked"] is False
    assert after_reveal["revealed"] is True
    assert after_reveal["jsonKey"] == "sk-plain"
    assert after_reveal["initialKey"] == "sk-plain"
    assert after_reveal["dirty"] is False
    assert "5 分钟内可免密码查看 Apikey" in str(after_reveal["toast"])
    assert after_reveal["toast"]["title"] == "已解锁"

    after_hide = result["afterHide"]
    assert after_hide["apiKey"] == "********"
    assert after_hide["masked"] is True
    assert after_hide["revealed"] is False
    assert after_hide["jsonKey"] == "********"
    assert after_hide["initialKey"] == "********"
    assert after_hide["dirty"] is False


def test_editing_the_key_after_reveal_stops_treating_it_as_a_stored_secret() -> None:
    result = _run_node_script(
        _LLM_HARNESS
        + r"""
        bootLlm(detailEditor());
        const editor = global.S.llmCenter.editor;
        window.__llmTestHooks.applyRevealedApiKey({
          item: { auth: { api_key: "sk-plain" } },
          expiresInSeconds: 300,
        });
        editor.apiKey = "sk-typed-by-operator";
        window.__llmTestHooks.noteApiKeyValueChanged("sk-typed-by-operator");
        const beforeHide = {
          revealed: editor.apiKeyRevealed,
          masked: editor.apiKeyMasked,
        };
        window.__llmTestHooks.hideEditorApiKey();
        console.log(JSON.stringify({
          beforeHide,
          afterHide: { apiKey: editor.apiKey, masked: editor.apiKeyMasked, revealed: editor.apiKeyRevealed },
        }));
        """
    )

    assert result["beforeHide"] == {"revealed": False, "masked": False}
    assert result["afterHide"] == {
        "apiKey": "sk-typed-by-operator",
        "masked": False,
        "revealed": False,
    }


def test_effective_api_key_count_uses_server_metadata_while_masked() -> None:
    result = _run_node_script(
        _LLM_HARNESS
        + r"""
        bootLlm(detailEditor());
        const editor = global.S.llmCenter.editor;
        const maskedCount = window.__llmTestHooks.effectiveApiKeyCount("********", editor);
        const typedCount = window.__llmTestHooks.effectiveApiKeyCount("sk-one", editor);
        editor.apiKeyMasked = false;
        const unmaskedCount = window.__llmTestHooks.effectiveApiKeyCount("********", editor);
        console.log(JSON.stringify({ maskedCount, typedCount, unmaskedCount }));
        """
    )

    assert result["maskedCount"] == 2
    assert result["typedCount"] == 1
    assert result["unmaskedCount"] == 1


def test_reveal_flow_prompts_for_password_only_when_the_window_is_closed() -> None:
    result = _run_node_script(
        _LLM_HARNESS
        + r"""
        bootLlm(detailEditor());
        const calls = [];
        let confirmOptions = null;
        global.openConfirm = (options) => { confirmOptions = options; };
        global.ApiClient.revealLlmConfigApiKey = async (configId, password) => {
          calls.push({ configId, password });
          if (!password) {
            const error = new Error("查看 Apikey 需要项目解锁口令。");
            error.code = "api_key_password_required";
            throw error;
          }
          return { item: { auth: { type: "api_key", api_key: "sk-plain" } }, expiresInSeconds: 300 };
        };

        async function main() {
          const promise = window.__llmTestHooks.requestEditorApiKeyReveal();
          for (let index = 0; index < 20 && !confirmOptions; index += 1) {
            await new Promise((resolve) => setTimeout(resolve, 0));
          }
          const promptInput = confirmOptions ? confirmOptions.input : null;
          await confirmOptions.onConfirm({ value: "owner-password" });
          const applied = await promise;
          console.log(JSON.stringify({
            calls,
            promptInput,
            applied,
            apiKey: global.S.llmCenter.editor.apiKey,
            toast: global.toasts[global.toasts.length - 1],
          }));
        }
        main();
        """
    )

    assert result["calls"] == [
        {"configId": "cfg-1", "password": ""},
        {"configId": "cfg-1", "password": "owner-password"},
    ]
    assert result["promptInput"]["type"] == "password"
    assert result["applied"] is True
    assert result["apiKey"] == "sk-plain"
    assert "5 分钟内可免密码查看 Apikey" in str(result["toast"])


def test_reveal_flow_cancel_leaves_the_mask_in_place() -> None:
    result = _run_node_script(
        _LLM_HARNESS
        + r"""
        bootLlm(detailEditor());
        let confirmOptions = null;
        global.openConfirm = (options) => { confirmOptions = options; };
        global.ApiClient.revealLlmConfigApiKey = async () => {
          const error = new Error("查看 Apikey 需要项目解锁口令。");
          error.code = "api_key_password_required";
          throw error;
        };

        async function main() {
          const promise = window.__llmTestHooks.requestEditorApiKeyReveal();
          for (let index = 0; index < 20 && !confirmOptions; index += 1) {
            await new Promise((resolve) => setTimeout(resolve, 0));
          }
          confirmOptions.onClose();
          const applied = await promise;
          const editor = global.S.llmCenter.editor;
          console.log(JSON.stringify({
            applied,
            apiKey: editor.apiKey,
            masked: editor.apiKeyMasked,
            revealed: editor.apiKeyRevealed,
            toasts: global.toasts.length,
          }));
        }
        main();
        """
    )

    assert result["applied"] is False
    assert result["apiKey"] == "********"
    assert result["masked"] is True
    assert result["revealed"] is False
    assert result["toasts"] == 0


def test_api_client_reveal_posts_password_and_reads_expiry() -> None:
    result = _run_node_script(
        r"""
        const fs = require("fs");
        const vm = require("vm");
        global.window = global;
        global.window.location = { origin: "http://localhost" };
        const code = ["g3ku/web/frontend/client_api/core.js", "g3ku/web/frontend/client_api/models.js", "g3ku/web/frontend/api_client.js"]
          .map((name) => fs.readFileSync(name, "utf8"))
          .join("\n");
        vm.runInThisContext(code);

        const captured = [];
        ApiClient._request = async (method, path, options = {}) => {
          captured.push({ method, path, body: options.body ?? null });
          return { item: { auth: { api_key: "sk-plain" } }, reveal_expires_in_seconds: 300 };
        };

        Promise.all([
          ApiClient.revealLlmConfigApiKey("cfg/1", "owner-password"),
          ApiClient.revealLlmConfigApiKey("cfg/1", ""),
        ]).then(([withPassword, passwordless]) => {
          console.log(JSON.stringify({ captured, withPassword, passwordless }));
        });
        """
    )

    assert result["captured"] == [
        {"method": "POST", "path": "/api/llm/configs/cfg%2F1/reveal", "body": {"password": "owner-password"}},
        {"method": "POST", "path": "/api/llm/configs/cfg%2F1/reveal", "body": {}},
    ]
    assert result["withPassword"] == {
        "item": {"auth": {"api_key": "sk-plain"}},
        "expiresInSeconds": 300,
    }


def test_api_client_maps_reveal_error_codes_to_operator_text() -> None:
    result = _run_node_script(
        r"""
        const fs = require("fs");
        const vm = require("vm");
        global.window = global;
        global.window.location = { origin: "http://localhost" };
        global.fetch = () => {
          throw new Error("fetch should not be called in this test");
        };
        const code = ["g3ku/web/frontend/client_api/core.js", "g3ku/web/frontend/client_api/models.js", "g3ku/web/frontend/api_client.js"]
          .map((name) => fs.readFileSync(name, "utf8"))
          .join("\n");
        vm.runInThisContext(code);

        console.log(JSON.stringify({
          required: ApiClient.friendlyErrorMessage({ code: "api_key_password_required" }, "fallback"),
          invalid: ApiClient.friendlyErrorMessage({ code: "api_key_password_invalid" }, "fallback"),
        }));
        """
    )

    assert "解锁口令" in str(result["required"])
    assert "不正确" in str(result["invalid"])
