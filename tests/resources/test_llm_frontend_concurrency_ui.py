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


def test_expand_single_api_key_max_concurrency_for_editor_repeats_scalar_for_multiple_keys() -> None:
    result = _run_node_script(
        """
        const fs = require("fs");
        const vm = require("vm");
        global.window = global;
        global.window.addEventListener = () => {};
        global.document = { getElementById: () => null, querySelector: () => null, addEventListener: () => {} };
        global.S = { modelCatalog: {}, llmCenter: null };
        global.U = {};
        global.ApiClient = {};
        global.showToast = () => {};
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
        global.hint = () => {};
        global.setDrawerOpen = () => {};
        global.icons = () => {};
        global.enhanceResourceSelects = () => {};
        const code = fs.readFileSync("g3ku/web/frontend/org_graph_llm.js", "utf8");
        vm.runInThisContext(code);

        console.log(JSON.stringify({
          expanded: window.__llmTestHooks.expandSingleApiKeyMaxConcurrencyForEditor(3, "key-1,key-2,key-3"),
        }));
        """
    )

    assert result["expanded"] == "3,3,3"


def test_parse_single_api_key_max_concurrency_input_supports_lists_and_zero() -> None:
    result = _run_node_script(
        """
        const fs = require("fs");
        const vm = require("vm");
        global.window = global;
        global.window.addEventListener = () => {};
        global.document = { getElementById: () => null, querySelector: () => null, addEventListener: () => {} };
        global.S = { modelCatalog: {}, llmCenter: null };
        global.U = {};
        global.ApiClient = {};
        global.showToast = () => {};
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
        global.hint = () => {};
        global.setDrawerOpen = () => {};
        global.icons = () => {};
        global.enhanceResourceSelects = () => {};
        const code = fs.readFileSync("g3ku/web/frontend/org_graph_llm.js", "utf8");
        vm.runInThisContext(code);

        console.log(JSON.stringify({
          parsed: window.__llmTestHooks.parseSingleApiKeyMaxConcurrencyInput("3,5,0"),
        }));
        """
    )

    assert result["parsed"] == [3, 5, 0]


def test_validate_single_api_key_max_concurrency_input_rejects_mismatched_key_count_and_all_zero() -> None:
    result = _run_node_script(
        """
        const fs = require("fs");
        const vm = require("vm");
        global.window = global;
        global.window.addEventListener = () => {};
        global.document = { getElementById: () => null, querySelector: () => null, addEventListener: () => {} };
        global.S = { modelCatalog: {}, llmCenter: null };
        global.U = {};
        global.ApiClient = {};
        global.showToast = () => {};
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
        global.hint = () => {};
        global.setDrawerOpen = () => {};
        global.icons = () => {};
        global.enhanceResourceSelects = () => {};
        const code = fs.readFileSync("g3ku/web/frontend/org_graph_llm.js", "utf8");
        vm.runInThisContext(code);

        let mismatchError = "";
        let allZeroError = "";
        try {
          window.__llmTestHooks.validateSingleApiKeyMaxConcurrencyInput("3,5", "key-1,key-2,key-3");
        } catch (error) {
          mismatchError = error.message || String(error);
        }
        try {
          window.__llmTestHooks.validateSingleApiKeyMaxConcurrencyInput("0,0", "key-1,key-2");
        } catch (error) {
          allZeroError = error.message || String(error);
        }
        console.log(JSON.stringify({ mismatchError, allZeroError }));
        """
    )

    assert "数量" in str(result["mismatchError"])
    assert "至少保留" in str(result["allZeroError"])


def test_binding_notes_title_includes_three_required_notes() -> None:
    result = _run_node_script(
        """
        const fs = require("fs");
        const vm = require("vm");
        global.window = global;
        global.window.addEventListener = () => {};
        global.document = { getElementById: () => null, querySelector: () => null, addEventListener: () => {} };
        global.S = { modelCatalog: {}, llmCenter: null };
        global.U = {};
        global.ApiClient = {};
        global.showToast = () => {};
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
        global.hint = () => {};
        global.setDrawerOpen = () => {};
        global.icons = () => {};
        global.enhanceResourceSelects = () => {};
        const code = fs.readFileSync("g3ku/web/frontend/org_graph_llm.js", "utf8");
        vm.runInThisContext(code);

        console.log(JSON.stringify({
          title: window.__llmTestHooks.bindingNotesTitle(),
        }));
        """
    )

    title = str(result["title"])
    assert "填写 0" in title
    assert "重试次数" in title
    assert "缓存命中率下降" in title


def test_api_client_maps_duplicate_binding_name_error_code_to_clear_message() -> None:
    result = _run_node_script(
        """
        const fs = require("fs");
        const vm = require("vm");
        global.window = global;
        global.window.location = { origin: "http://localhost" };
        global.fetch = () => {
          throw new Error("fetch should not be called in this test");
        };
        const code = fs.readFileSync("g3ku/web/frontend/api_client.js", "utf8");
        vm.runInThisContext(code);

        console.log(JSON.stringify({
          message: ApiClient.friendlyErrorMessage({ code: "llm_binding_key_exists" }, "fallback"),
        }));
        """
    )

    assert result["message"] == "配置名已存在，请使用其他配置名。"


def test_api_client_update_llm_binding_returns_item_and_runtime_refresh() -> None:
    result = _run_node_script(
        """
        const fs = require("fs");
        const vm = require("vm");
        global.window = global;
        global.window.location = { origin: "http://localhost" };
        global.fetch = async () => ({
          ok: true,
          json: async () => ({
            item: { key: "m", retry_count: 4 },
            runtime_refresh: {
              worker_refresh_command_id: "command:refresh-1",
              worker_refresh_status: "pending",
            },
          }),
        });
        const code = fs.readFileSync("g3ku/web/frontend/api_client.js", "utf8");
        vm.runInThisContext(code);

        ApiClient.updateLlmBinding("m", { retry_count: 4 }).then((value) => {
          console.log(JSON.stringify(value));
        });
        """
    )

    assert result["item"] == {"key": "m", "retry_count": 4}
    assert result["runtimeRefresh"] == {
        "worker_refresh_command_id": "command:refresh-1",
        "worker_refresh_status": "pending",
    }


def test_api_client_update_managed_model_uses_extended_timeout_for_save_requests() -> None:
    result = _run_node_script(
        """
        const fs = require("fs");
        const vm = require("vm");
        global.window = global;
        global.window.location = { origin: "http://localhost" };
        global.fetch = () => {
          throw new Error("fetch should not be called in this test");
        };
        const code = fs.readFileSync("g3ku/web/frontend/api_client.js", "utf8");
        vm.runInThisContext(code);

        const captured = [];
        ApiClient._request = async (method, path, options = {}) => {
          captured.push({ method, path, timeoutMs: options.timeoutMs ?? null });
          return { items: [{ key: "demo" }], roles: {}, roleIterations: {}, roleConcurrency: {} };
        };

        ApiClient.updateManagedModel("demo", { providerModel: "openai:gpt-5.2" }).then(() => {
          console.log(JSON.stringify(captured));
        });
        """
    )

    assert result[0] == {
        "method": "PUT",
        "path": "/api/models/demo",
        "timeoutMs": 30000,
    }


def test_api_client_update_llm_config_uses_extended_timeout_for_save_requests() -> None:
    result = _run_node_script(
        """
        const fs = require("fs");
        const vm = require("vm");
        global.window = global;
        global.window.location = { origin: "http://localhost" };
        global.fetch = () => {
          throw new Error("fetch should not be called in this test");
        };
        const code = fs.readFileSync("g3ku/web/frontend/api_client.js", "utf8");
        vm.runInThisContext(code);

        const captured = [];
        ApiClient._request = async (method, path, options = {}) => {
          captured.push({ method, path, timeoutMs: options.timeoutMs ?? null });
          return { item: { config_id: "cfg-1" }, runtime_refresh: null };
        };

        ApiClient.updateLlmConfig("cfg-1", { default_model: "gpt-5.2" }).then(() => {
          console.log(JSON.stringify(captured));
        });
        """
    )

    assert result == [
        {
            "method": "PUT",
            "path": "/api/llm/configs/cfg-1",
            "timeoutMs": 30000,
        }
    ]


def test_api_client_delete_llm_binding_returns_runtime_refresh() -> None:
    result = _run_node_script(
        """
        const fs = require("fs");
        const vm = require("vm");
        global.window = global;
        global.window.location = { origin: "http://localhost" };
        global.fetch = async () => ({
          ok: true,
          json: async () => ({
            ok: true,
            runtime_refresh: {
              worker_refresh_command_id: "command:delete-1",
              worker_refresh_status: "pending",
            },
          }),
        });
        const code = fs.readFileSync("g3ku/web/frontend/api_client.js", "utf8");
        vm.runInThisContext(code);

        ApiClient.deleteLlmBinding("m").then((value) => {
          console.log(JSON.stringify(value));
        });
        """
    )

    assert result["ok"] is True
    assert result["runtime_refresh"] == {
        "worker_refresh_command_id": "command:delete-1",
        "worker_refresh_status": "pending",
    }


def test_llm_frontend_uses_binding_name_wording_in_create_form() -> None:
    html = (REPO_ROOT / "g3ku" / "web" / "frontend" / "org_graph.html").read_text(encoding="utf-8")
    result = _run_node_script(
        """
        const fs = require("fs");
        const vm = require("vm");
        global.window = global;
        global.window.addEventListener = () => {};
        global.document = { getElementById: () => null, querySelector: () => null, addEventListener: () => {} };
        global.S = { modelCatalog: {}, llmCenter: null };
        global.U = {};
        global.ApiClient = {};
        global.showToast = () => {};
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
        global.hint = () => {};
        global.setDrawerOpen = () => {};
        global.icons = () => {};
        global.enhanceResourceSelects = () => {};
        const code = fs.readFileSync("g3ku/web/frontend/org_graph_llm.js", "utf8");
        vm.runInThisContext(code);

        console.log(JSON.stringify({
          label: window.__llmTestHooks.bindingNameLabel(),
          normalized: window.__llmTestHooks.normalizeBindingNameText("模型 Key * / 妯″瀷 Key * / ДЈРН Key *"),
        }));
        """
    )

    assert result["label"] == "配置名 / 绑定名"
    assert result["normalized"] == "配置名 / 绑定名 * / 配置名 / 绑定名 * / 配置名 / 绑定名 *"
    assert "配置名 / 绑定名 / Provider / 模型" in html


def test_binding_draft_payload_requires_model_key_with_readable_message() -> None:
    result = _run_node_script(
        """
        const fs = require("fs");
        const vm = require("vm");
        global.window = global;
        global.window.addEventListener = () => {};
        global.document = { getElementById: () => null, querySelector: () => null, addEventListener: () => {} };
        global.S = {
          modelCatalog: {},
          llmCenter: {
            loading: false,
            saving: false,
            error: "",
            templates: [],
            templateMap: {},
            templateDetailMap: {},
            bindings: [],
            bindingMap: {},
            routes: {},
            roleIterations: {},
            roleConcurrency: {},
            editor: {
              open: true,
              mode: "create",
              bindingKey: "",
              configId: "",
              modelKey: "",
              providerId: "demo",
              jsonText: "{}",
              initialJsonText: "{}",
              retryOn: ["network"],
              retryCount: 0,
              singleApiKeyMaxConcurrency: "",
              contextWindowTokens: "30001",
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
        global.ApiClient = {};
        global.showToast = () => {};
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
        global.hint = () => {};
        global.setDrawerOpen = () => {};
        global.icons = () => {};
        global.enhanceResourceSelects = () => {};
        let code = fs.readFileSync("g3ku/web/frontend/org_graph_llm.js", "utf8");
        code = code.replace(
          "window.__llmTestHooks = {",
          "window.__llmTestHooks = {\\n    bindingDraftPayload,"
        );
        vm.runInThisContext(code);

        let message = "";
        try {
          window.__llmTestHooks.bindingDraftPayload({ requireModelKey: true });
        } catch (error) {
          message = error.message || String(error);
        }

        console.log(JSON.stringify({ message }));
        """
    )

    assert result["message"] == "模型 Key 不能为空"
