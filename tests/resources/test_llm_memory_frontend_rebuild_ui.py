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


def test_memory_save_requires_rebuild_confirmation_only_for_embedding_model_changes() -> None:
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
          sameEmbedding: window.__llmTestHooks.memorySaveRequiresRebuildConfirmation({
            currentEmbeddingProviderModel: "dashscope:qwen3-vl-embedding",
            nextEmbeddingProviderModel: "dashscope:qwen3-vl-embedding",
            embeddingModified: true,
            rerankModified: false,
          }),
          changedEmbedding: window.__llmTestHooks.memorySaveRequiresRebuildConfirmation({
            currentEmbeddingProviderModel: "dashscope:qwen3-vl-embedding",
            nextEmbeddingProviderModel: "dashscope:multimodal-embedding-v1",
            embeddingModified: true,
            rerankModified: false,
          }),
          rerankOnly: window.__llmTestHooks.memorySaveRequiresRebuildConfirmation({
            currentEmbeddingProviderModel: "dashscope:qwen3-vl-embedding",
            nextEmbeddingProviderModel: "dashscope:qwen3-vl-embedding",
            embeddingModified: false,
            rerankModified: true,
          }),
        }));
        """
    )

    assert result["sameEmbedding"] is False
    assert result["changedEmbedding"] is True
    assert result["rerankOnly"] is False


def test_frontend_uses_inline_confirm_instead_of_browser_confirm() -> None:
    app_js = (REPO_ROOT / "g3ku" / "web" / "frontend" / "org_graph_app.js").read_text(encoding="utf-8")
    llm_js = (REPO_ROOT / "g3ku" / "web" / "frontend" / "org_graph_llm.js").read_text(encoding="utf-8")

    assert "window.confirm" not in app_js
    assert "window.confirm" not in llm_js


def test_untouched_unconfigured_rerank_is_not_marked_pending_when_only_embedding_changes() -> None:
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

        S.llmCenter = {
          loading: false,
          saving: false,
          error: "",
          templates: [],
          templateMap: {},
          templateDetailMap: {},
          bindings: [],
          bindingMap: {},
          routes: EMPTY_MODEL_ROLES(),
          roleIterations: DEFAULT_ROLE_ITERATIONS(),
          roleConcurrency: DEFAULT_ROLE_CONCURRENCY(),
          eventsBound: false,
          editor: {
            open: true,
            mode: "memory",
            memory: {
              loading: false,
              error: "",
              embedding: {
                label: "Embedding",
                capability: "embedding",
                configId: "",
                providerId: "dashscope_embedding",
                providerModel: "dashscope:qwen3-vl-embedding",
                templateProviderId: "dashscope_embedding",
                jsonText: '{"provider_id":"dashscope_embedding","capability":"embedding","default_model":"multimodal-embedding-v1","auth_mode":"api_key","api_key":"demo"}',
                initialJsonText: '{"provider_id":"dashscope_embedding","capability":"embedding","default_model":"qwen3-vl-embedding","auth_mode":"api_key","api_key":"demo"}',
                validation: null,
                probe: null,
                error: "",
              },
              rerank: {
                label: "Rerank",
                capability: "rerank",
                configId: "",
                providerId: "dashscope_rerank",
                providerModel: "dashscope:qwen3-vl-rerank",
                templateProviderId: "dashscope_rerank",
                jsonText: '{"provider_id":"dashscope_rerank","capability":"rerank","default_model":"qwen3-vl-rerank","auth_mode":"api_key","api_key":""}',
                initialJsonText: '{"provider_id":"dashscope_rerank","capability":"rerank","default_model":"qwen3-vl-rerank","auth_mode":"api_key","api_key":""}',
                validation: null,
                probe: null,
                error: "",
              },
            },
          },
        };

        console.log(JSON.stringify({
          pending: window.__llmTestHooks.modifiedMemorySectionKeys(),
        }));
        """
    )

    assert result["pending"] == ["embedding"]
