const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const { readFrontendSources } = require("./support/frontend_sources.js");

const TASK_VIEW_PATH = "g3ku/web/frontend/org_graph_task_view.js";
const APP_PATH = "g3ku/web/frontend/org_graph_app.js";
const TASK_VIEW_CODE = readFrontendSources(["task_view"]).join("\n");
const APP_CODE = readFrontendSources(["app"]).join("\n");

class StubElement {}

class StubHTMLElement extends StubElement {
    constructor() {
        super();
        this.className = "";
        this.hidden = false;
        this.textContent = "";
        this.innerHTML = "";
        this.children = [];
        this.dataset = {};
        this.attributes = {};
        this.style = {};
        this.parentElement = null;
        this.classList = {
            add() {},
            remove() {},
            contains() { return false; },
            toggle() { return false; },
        };
    }

    appendChild(child) {
        child.parentElement = this;
        this.children.push(child);
        return child;
    }

    setAttribute(name, value) {
        this.attributes[name] = String(value);
    }

    querySelector() {
        return null;
    }

    querySelectorAll() {
        return [];
    }

    addEventListener() {}
}

class StubHTMLButtonElement extends StubHTMLElement {}
class StubHTMLInputElement extends StubHTMLElement {}
class StubHTMLTextAreaElement extends StubHTMLElement {}
class StubHTMLSelectElement extends StubHTMLElement {}

class StubDocument {
    getElementById() { return null; }
    createElement() { return new StubHTMLElement(); }
    querySelector() { return null; }
    querySelectorAll() { return []; }
    addEventListener() {}
}

function loadApp() {
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
        document: new StubDocument(),
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
    };
    context.window = context;
    vm.createContext(context);
    vm.runInContext(
        `${TASK_VIEW_CODE}
        ${APP_CODE}
        this.__testExports = {
            S,
            U,
            createPendingCeoTurn,
            maybeShowCeoContextLoadNotice,
            isCeoContractDeliveryStep,
            isCeoContextLoadStep,
            filterCeoInteractionFlowSummary,
            contextLoadDisplayFor,
            renderExecutionRoundToolChip,
            renderExecutionRoundToolPanel,
            normalizeExecutionStageTrace,
            normalizeSummaryTraceToolCall,
            buildNodeExecutionTrace,
        };`,
        context
    );
    context.__testExports.U.ceoContextLoadNotice = new StubHTMLElement();
    return { ...context.__testExports, __context: context };
}

function _canonicalContext() {
    return {
        stages: [
            {
                stage_id: "stage-1",
                stage_index: 1,
                stage_goal: "inspect repository",
                status: "active",
                tool_round_budget: 10,
                rounds: [
                    {
                        round_id: "round-1",
                        round_index: 1,
                        budget_counted: true,
                        tools: [
                            {
                                tool_call_id: "call-delivered",
                                tool_name: "filesystem_edit",
                                status: "success",
                                contract_delivery: "staged",
                                output_text: "[TOOL_CONTRACT_V1:filesystem_edit:tcf:abc]\nbody\n[/TOOL_CONTRACT_V1]",
                            },
                            {
                                tool_call_id: "call-delivered-error",
                                tool_name: "filesystem_edit",
                                status: "error",
                                contract_delivery: "staged",
                                output_text: "Error: could not deliver the contract",
                            },
                            {
                                tool_call_id: "call-executed",
                                tool_name: "filesystem_stat",
                                status: "success",
                                output_text: "ok",
                            },
                        ],
                    },
                ],
            },
        ],
    };
}

test("contract delivery is part of the context-load family", () => {
    const { isCeoContractDeliveryStep, isCeoContextLoadStep } = loadApp();

    assert.equal(isCeoContractDeliveryStep({ tool_name: "filesystem_edit", contract_delivery: "staged" }), true);
    assert.equal(isCeoContractDeliveryStep({ tool_name: "filesystem_edit" }), false);
    assert.equal(isCeoContextLoadStep({ tool_name: "filesystem_edit", contract_delivery: "invalid" }), true);
    assert.equal(isCeoContextLoadStep({ tool_name: "load_tool_context" }), true);
    assert.equal(isCeoContextLoadStep({ tool_name: "filesystem_edit" }), false);
});

test("successful contract deliveries stay out of the visible stage rail", () => {
    const { filterCeoInteractionFlowSummary } = loadApp();

    const filtered = filterCeoInteractionFlowSummary(_canonicalContext());
    const tools = filtered.stages[0].rounds[0].tools;

    assert.deepEqual(
        JSON.parse(JSON.stringify(tools.map((step) => step.tool_call_id))),
        ["call-delivered-error", "call-executed"]
    );
});

test("a direct-load step renders as the load-tool card with its own tool id", () => {
    const { S, contextLoadDisplayFor } = loadApp();
    S.tools = [{ tool_id: "filesystem_edit", risk_level: "high" }];

    const display = contextLoadDisplayFor({
        tool_call_id: "call-1",
        tool_name: "filesystem_edit",
        status: "success",
        contract_delivery: "staged",
        output_text: "[TOOL_CONTRACT_V1:filesystem_edit:tcf:abc]\nbody\n[/TOOL_CONTRACT_V1]",
    });

    assert.equal(display.kind, "tool");
    assert.equal(display.targetId, "filesystem_edit");
    assert.equal(display.title, "加载 tool");
    assert.equal(display.riskLevel, "high");
    // 契约块不是 loader 的 JSON 载荷：正文退回输出车道，但卡片族保持加载 tool。
    assert.equal(display.body, "");
});

test("the node card uses the load-tool chip and skips the arguments panel", () => {
    const { S, normalizeExecutionStageTrace, renderExecutionRoundToolChip, renderExecutionRoundToolPanel } = loadApp();
    S.tools = [{ tool_id: "filesystem_edit", risk_level: "high" }];
    const stage = normalizeExecutionStageTrace(_canonicalContext().stages[0], 0);
    const round = stage.rounds[0];
    const step = round.tools[0];

    const chip = renderExecutionRoundToolChip(round, step, 0);
    assert.match(chip, /is-context-load is-tool/);
    assert.match(chip, /加载 tool/);
    assert.match(chip, /title="filesystem_edit"/);

    const panel = renderExecutionRoundToolPanel(round, step, 0);
    assert.match(panel, /task-trace-round-panel is-context-load/);
    assert.match(panel, /内容/);
    assert.doesNotMatch(panel, /参数/);
});

test("the delivery marker survives stage and summary normalizers", () => {
    const { normalizeExecutionStageTrace, normalizeSummaryTraceToolCall } = loadApp();

    const stage = normalizeExecutionStageTrace(_canonicalContext().stages[0], 0);
    assert.equal(stage.rounds[0].tools[0].contract_delivery, "staged");

    const summaryStep = normalizeSummaryTraceToolCall({
        tool_call_id: "call-1",
        tool_name: "filesystem_edit",
        status: "success",
        contract_delivery: "staged",
    }, 0);
    assert.equal(summaryStep.contract_delivery, "staged");
});

test("node detail keeps the marker on flat tool steps", () => {
    const { buildNodeExecutionTrace } = loadApp();

    const trace = buildNodeExecutionTrace({}, {
        execution_trace: {
            tool_steps: [
                {
                    tool_call_id: "call-1",
                    tool_name: "filesystem_edit",
                    status: "success",
                    contract_delivery: "staged",
                    output_text: "contract",
                },
            ],
            stages: [],
        },
    });

    assert.equal(trace.tool_steps[0].contract_delivery, "staged");
});

test("a direct delivery raises the same tool notice as load_tool_context", () => {
    const { S, U, createPendingCeoTurn, maybeShowCeoContextLoadNotice } = loadApp();
    S.tools = [{ tool_id: "filesystem_edit", risk_level: "high" }];
    const turn = createPendingCeoTurn("user");
    S.ceoPendingTurns = [turn];

    const shown = maybeShowCeoContextLoadNotice(turn, {
        toolName: "load_tool_context",
        status: "success",
        targetId: "filesystem_edit",
        detailTexts: ["[TOOL_CONTRACT_V1:filesystem_edit:tcf:abc]\nbody\n[/TOOL_CONTRACT_V1]"],
    });

    assert.equal(shown, true);
    const notice = U.ceoContextLoadNotice.children[0];
    assert.equal(notice.dataset.noticeKind, "tool");
    assert.equal(notice.children[0].innerHTML, '<i data-lucide="wrench"></i>');
    assert.equal(notice.children[1].textContent, "已加载工具 filesystem_edit");
    assert.equal(notice.dataset.riskLevel, "high");
});
