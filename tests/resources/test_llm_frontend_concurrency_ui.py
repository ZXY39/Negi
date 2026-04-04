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
