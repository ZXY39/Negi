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


# 加载 org_graph_llm.js 的最小宿主。两颗 token 输入按实盘形状同时给到 DOM 与编辑器状态，
# 占位 __CONTEXT__ / __MAX__ 由 _harness() 填。
_HARNESS = """
        const fs = require("fs");
        const vm = require("vm");
        global.window = global;
        global.window.addEventListener = () => {};
        const dom = {
          "llm-model-key-input": { value: "demo" },
          "llm-provider-select": { value: "demo" },
          "llm-json-editor": { value: JSON.stringify({ provider_id: "demo", capability: "chat", auth_mode: "api_key", api_key: "k", default_model: "m", parameters: {}, extra_headers: {}, extra_options: {} }) },
          "llm-binding-retry-on": { value: "network" },
          "llm-binding-retry-count": { value: "0" },
          "llm-binding-single-api-key-max-concurrency": { value: "" },
          "llm-binding-context-window-tokens": { value: "__CONTEXT__" },
          "llm-binding-max-output-tokens": { value: "__MAX__" },
        };
        // 存活的 dom 表：保存链路会先按输入框读数覆盖编辑器状态，所以换用例要改这里而不是只改 editor。
        global.document = {
          getElementById: (id) => dom[id] || null,
          querySelector: () => null,
          addEventListener: () => {},
        };
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
              mode: "detail",
              bindingKey: "demo",
              configId: "cfg",
              modelKey: "demo",
              providerId: "demo",
              jsonText: "{}",
              initialJsonText: "{}",
              retryOn: ["network"],
              retryCount: 0,
              singleApiKeyMaxConcurrency: "",
              contextWindowTokens: "__CONTEXT__",
              initialContextWindowTokens: "__CONTEXT__",
              maxOutputTokens: "__MAX__",
              initialMaxOutputTokens: "__MAX__",
              validation: null,
              probe: null,
              memory: { loading: false, error: "", embedding: {}, rerank: {} },
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
        global.normalizeAllModelRoles = (value) => ({ ceo: [], execution: [], inspection: [], memory: [], ...(value || {}) });
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
"""


def _harness(context_window: str, max_output: str = "65536") -> str:
    return _HARNESS.replace("__CONTEXT__", context_window).replace("__MAX__", max_output)


def test_binding_draft_payload_rejects_context_window_tokens_not_greater_than_25000() -> None:
    result = _run_node_script(
        _harness("25000")
        + """
        let message = "";
        try {
          window.__llmTestHooks.bindingDraftPayload({ requireModelKey: true });
        } catch (error) {
          message = error.message || String(error);
        }

        console.log(JSON.stringify({ message }));
        """
    )

    assert "25000" in str(result["message"])
    # 认得出数字但太小，不能说成「无法识别」。
    assert "无法识别" not in str(result["message"])


def test_token_scale_notation_saves_the_converted_integer() -> None:
    result = _run_node_script(
        _harness("200k", "1m")
        + """
        const payload = window.__llmTestHooks.bindingDraftPayload({});
        const editor = window.__llmTestHooks.llmState().editor;
        console.log(JSON.stringify({
          contextWindowTokens: payload.contextWindowTokens,
          draftContextWindowTokens: payload.draft.parameters.context_window_tokens,
          draftMaxTokens: payload.draft.parameters.max_tokens,
          contextWindowText: editor.contextWindowTokens,
          maxOutputText: editor.maxOutputTokens,
        }));
        """
    )

    assert result["contextWindowTokens"] == 200000
    assert result["draftContextWindowTokens"] == 200000
    assert result["draftMaxTokens"] == 1000000
    # 保存后字段保留操作员那一档写法，字母统一大写；落盘的仍是换算后的整数。
    assert result["contextWindowText"] == "200K"
    assert result["maxOutputText"] == "1M"


def test_unrecognized_token_text_blocks_save_with_its_own_message() -> None:
    result = _run_node_script(
        _harness("200KB", "65536")
        + """
        const messages = {};
        for (const [contextWindow, maxOutput] of [["200KB", "65536"], ["32000", "65X"]]) {
          const editor = window.__llmTestHooks.llmState().editor;
          dom["llm-binding-context-window-tokens"].value = contextWindow;
          dom["llm-binding-max-output-tokens"].value = maxOutput;
          editor.contextWindowTokens = contextWindow;
          editor.initialContextWindowTokens = contextWindow;
          editor.maxOutputTokens = maxOutput;
          editor.initialMaxOutputTokens = maxOutput;
          let message = "";
          try {
            window.__llmTestHooks.bindingDraftPayload({});
          } catch (error) {
            message = error.message || String(error);
          }
          messages[`${contextWindow}|${maxOutput}`] = message;
        }
        console.log(JSON.stringify(messages));
        """
    )

    assert str(result["200KB|65536"]).startswith("上下文窗口Token无法识别")
    assert str(result["32000|65X"]).startswith("最大输出TOKEN无法识别")


def test_parse_token_scale_value_and_display_text() -> None:
    result = _run_node_script(
        _harness("32000")
        + """
        const hooks = window.__llmTestHooks;
        console.log(JSON.stringify({
          parsed: [
            ["200K", hooks.parseTokenScaleValue("200K")],
            ["200k", hooks.parseTokenScaleValue("200k")],
            ["1m", hooks.parseTokenScaleValue("1m")],
            ["1.5M", hooks.parseTokenScaleValue("1.5M")],
            ["64 K", hooks.parseTokenScaleValue("64 K")],
            ["200000", hooks.parseTokenScaleValue("200000")],
            ["200KB", hooks.parseTokenScaleValue("200KB")],
            ["K200", hooks.parseTokenScaleValue("K200")],
            ["1e3", hooks.parseTokenScaleValue("1e3")],
            ["-1K", hooks.parseTokenScaleValue("-1K")],
            ["1.5", hooks.parseTokenScaleValue("1.5")],
            ["", hooks.parseTokenScaleValue("")],
          ],
          window: [
            hooks.validContextWindowTokensValue("200K"),
            hooks.validContextWindowTokensValue("25K"),
            hooks.validContextWindowTokensValue("25001"),
          ],
          maxOutput: [
            hooks.validMaxOutputTokensValue("64K"),
            hooks.validMaxOutputTokensValue("0"),
            hooks.validMaxOutputTokensValue("1"),
          ],
          display: [
            hooks.tokenScaleDisplayText("200k", 200000),
            hooks.tokenScaleDisplayText("1m", 1000000),
            hooks.tokenScaleDisplayText("200k", 300000),
            hooks.tokenScaleDisplayText("", 5),
          ],
        }));
        """
    )

    assert [pair[1] for pair in result["parsed"]] == [
        200000,
        200000,
        1000000,
        1500000,
        64000,
        200000,
        None,
        None,
        None,
        None,
        None,
        None,
    ]
    # 25K = 25000 不越过「必须大于 25000」那道界。
    assert result["window"] == [200000, None, 25001]
    assert result["maxOutput"] == [64000, None, 1]
    assert result["display"] == ["200K", "1M", "300000", "5"]


def test_typed_context_window_text_is_never_rescued_by_the_json_draft() -> None:
    result = _run_node_script(
        _harness("200KB", "65536")
        + """
        const draftWithWindow = JSON.stringify({ provider_id: "demo", capability: "chat", auth_mode: "api_key", api_key: "k", default_model: "m", parameters: { context_window_tokens: 200000 }, extra_headers: {}, extra_options: {} });
        dom["llm-json-editor"].value = draftWithWindow;
        const editor = window.__llmTestHooks.llmState().editor;
        editor.jsonText = draftWithWindow;
        editor.initialJsonText = draftWithWindow;
        const cases = {};
        for (const text of ["200KB", "25K"]) {
          dom["llm-binding-context-window-tokens"].value = text;
          editor.contextWindowTokens = text;
          editor.initialContextWindowTokens = text;
          let message = "";
          try {
            window.__llmTestHooks.bindingDraftPayload({});
          } catch (error) {
            message = error.message || String(error);
          }
          cases[text] = { message, fieldAfter: editor.contextWindowTokens };
        }
        console.log(JSON.stringify(cases));
        """
    )

    # 这一格写了字就不许被 JSON 的旧读数悄悄顶掉：认不出与越界都要停在保存之前，文本也要留着让人改。
    assert str(result["200KB"]["message"]).startswith("上下文窗口Token无法识别")
    assert result["200KB"]["fieldAfter"] == "200KB"
    assert "25000" in str(result["25K"]["message"])
    assert result["25K"]["fieldAfter"] == "25K"


def test_new_config_starts_from_the_200k_default_not_the_template_window() -> None:
    result = _run_node_script(
        _harness("32000")
        + """
        (async () => {
          const hooks = window.__llmTestHooks;
          const state = hooks.llmState();
          state.templates = [{ provider_id: "openai", capability: "chat" }];
          state.templateMap.openai = { provider_id: "openai", capability: "chat", auth_mode: "api_key", display_name: "OpenAI Chat" };
          state.templateDetailMap.openai = {
            provider: { default_base_url: "https://api.openai.com/v1", default_model: "gpt-4o" },
            fields: [{ key: "context_window_tokens", path: "parameters.context_window_tokens", default: 32000 }],
          };
          await hooks.openCreateModal();
          const editor = hooks.llmState().editor;
          console.log(JSON.stringify({
            mode: editor.mode,
            fieldText: editor.contextWindowTokens,
            initialFieldText: editor.initialContextWindowTokens,
            jsonWindow: String(editor.jsonText.match(/"context_window_tokens": ([^,\\n]+)/)?.[1] || "").trim(),
          }));
        })();
        """
    )

    assert result["mode"] == "create"
    # 模板建议 32000 不顶新档默认：格子写 200K，JSON 草稿同时带换算后的 200000。
    assert result["fieldText"] == "200K"
    assert result["initialFieldText"] == "200K"
    assert result["jsonWindow"] == "200000"
