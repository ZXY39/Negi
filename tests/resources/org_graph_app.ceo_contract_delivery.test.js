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
            ResourcesState,
            U,
            createPendingCeoTurn,
            maybeShowCeoContextLoadNotice,
            isCeoContractDeliveryStep,
            isCeoContextLoadStep,
            ceoCommittedToolCallKeys,
            contextLoadDisplayFor,
            renderExecutionRoundToolChip,
            renderExecutionRoundToolPanel,
            normalizeTraceTurns,
            normalizeSummaryTraceToolCall,
            buildNodeExecutionTrace,
        };`,
        context
    );
    context.__testExports.U.ceoContextLoadNotice = new StubHTMLElement();
    return { ...context.__testExports, __context: context };
}

function _turnFixture() {
    return [
        {
            seq: 1,
            turn_id: "turn-1",
            text: "inspect repository",
            status: "completed",
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
    ];
}

test("contract delivery is part of the context-load family", () => {
    const { isCeoContractDeliveryStep, isCeoContextLoadStep } = loadApp();

    assert.equal(isCeoContractDeliveryStep({ tool_name: "filesystem_edit", contract_delivery: "staged" }), true);
    assert.equal(isCeoContractDeliveryStep({ tool_name: "filesystem_edit" }), false);
    assert.equal(isCeoContextLoadStep({ tool_name: "filesystem_edit", contract_delivery: "invalid" }), true);
    assert.equal(isCeoContextLoadStep({ tool_name: "load_tool_context" }), true);
    assert.equal(isCeoContextLoadStep({ tool_name: "filesystem_edit" }), false);
});

test("a ledger turn retires the live rows it already contains", () => {
    const { ceoCommittedToolCallKeys, normalizeTraceTurns } = loadApp();
    const turnRow = normalizeTraceTurns(_turnFixture())[0];

    const committed = ceoCommittedToolCallKeys({ turns: [{ tools: turnRow.tools }] });

    assert.deepEqual(
        [...committed].sort(),
        ["call-delivered", "call-delivered-error", "call-executed"],
        "账本里每条工具都要能让对应实时行退场，漏一条就是一行永远挂着的“加载中”"
    );
});

test("a direct-load step renders as the load-tool card with its own tool id", () => {
    const { ResourcesState, S, contextLoadDisplayFor } = loadApp();
    ResourcesState.tools = [{ tool_id: "filesystem_edit", risk_level: "high" }];

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
    const { ResourcesState, S, normalizeTraceTurns, renderExecutionRoundToolChip, renderExecutionRoundToolPanel } = loadApp();
    ResourcesState.tools = [{ tool_id: "filesystem_edit", risk_level: "high" }];
    const turnRow = normalizeTraceTurns(_turnFixture())[0];
    const round = { round_id: `turn:${turnRow.seq}` };
    const step = turnRow.tools[0];

    const chip = renderExecutionRoundToolChip(round, step, 0);
    assert.match(chip, /is-context-load is-tool/);
    assert.match(chip, /加载 tool/);
    assert.match(chip, /title="filesystem_edit"/);

    const panel = renderExecutionRoundToolPanel(round, step, 0);
    assert.match(panel, /task-trace-round-panel is-context-load/);
    assert.match(panel, /内容/);
    assert.doesNotMatch(panel, /参数/);
});

test("the delivery marker survives the turn and summary normalizers", () => {
    const { normalizeTraceTurns, normalizeSummaryTraceToolCall } = loadApp();

    const turnRow = normalizeTraceTurns(_turnFixture())[0];
    assert.equal(turnRow.tools[0].contract_delivery, "staged");

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
        },
    });

    assert.equal(trace.tool_steps[0].contract_delivery, "staged");
});

test("a direct delivery raises the same tool notice as load_tool_context", () => {
    const { ResourcesState, S, U, createPendingCeoTurn, maybeShowCeoContextLoadNotice } = loadApp();
    ResourcesState.tools = [{ tool_id: "filesystem_edit", risk_level: "high" }];
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
