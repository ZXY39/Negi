const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const TASK_VIEW_PATH = "g3ku/web/frontend/org_graph_task_view.js";
const APP_PATH = "g3ku/web/frontend/org_graph_app.js";
const TASK_VIEW_CODE = fs.readFileSync(TASK_VIEW_PATH, "utf8");
const APP_CODE = fs.readFileSync(APP_PATH, "utf8");
const COMPRESSION_TEXT = "\u4e0a\u4e0b\u6587\u538b\u7f29\u4e2d";

class StubElement {}
class StubHTMLElement extends StubElement {
    constructor() {
        super();
        this.hidden = false;
        this.textContent = "";
        this.innerHTML = "";
        this.className = "";
        this.dataset = {};
        this.style = {};
        this.attributes = {};
        this._selectors = {};
        this._selectorLists = {};
        this.classList = {
            add() {},
            remove() {},
            contains() { return false; },
            toggle() { return false; },
        };
    }

    querySelector(selector) {
        return this._selectors[selector] || null;
    }

    querySelectorAll(selector) {
        return this._selectorLists[selector] || [];
    }

    addEventListener() {}

    setAttribute(name, value) {
        this.attributes[name] = String(value);
    }

    removeAttribute(name) {
        delete this.attributes[name];
    }
}
class StubHTMLButtonElement extends StubHTMLElement {}
class StubHTMLInputElement extends StubHTMLElement {}
class StubHTMLTextAreaElement extends StubHTMLElement {}
class StubHTMLSelectElement extends StubHTMLElement {}

class StubDocument {
    getElementById() {
        return null;
    }

    createElement() {
        return new StubHTMLElement();
    }

    querySelector() {
        return null;
    }

    querySelectorAll() {
        return [];
    }

    addEventListener() {}
}

function makeTurn({ text = "" } = {}) {
    return {
        textEl: { textContent: text, innerHTML: text, classList: { add() {}, remove() {} } },
        flowEl: { hidden: true, open: false },
        metaEl: { textContent: "" },
        listEl: { innerHTML: "", querySelectorAll: () => [] },
        footerEl: { hidden: true },
        toggleEl: { textContent: "", setAttribute() {} },
    };
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
        `${TASK_VIEW_CODE}\n${APP_CODE}\nthis.__testExports = { renderCeoStageTraceIntoTurn, normalizeCeoSnapshotToolEvents, syncCeoCompressionToast, stageTraceStatus, displayTaskStageStatus, toggleCeoToolStepOutput, S, U };`,
        context
    );
    context.__testExports.U.ceoCompressionToast = new StubHTMLElement();
    context.__testExports.U.ceoCompressionToastText = new StubHTMLElement();
    context.__testExports.__context = context;
    return context.__testExports;
}

test("ceo inflight snapshot renders stage -> round -> tool structure", () => {
    const { renderCeoStageTraceIntoTurn } = loadApp();
    const turn = makeTurn({ text: "" });

    renderCeoStageTraceIntoTurn(turn, {
        stages: [
            {
                stage_id: "stage-1",
                stage_goal: "inspect repository",
                status: "running",
                tool_round_budget: 2,
                rounds: [
                    {
                        round_id: "round-1",
                        round_index: 1,
                        tools: [
                            {
                                tool_name: "filesystem",
                                arguments_text: "{\"path\": \".\"}",
                                output_text: "ok",
                                status: "success",
                            },
                        ],
                    },
                ],
            },
        ],
    });

    assert.match(turn.listEl.innerHTML, /inspect repository/);
    assert.match(turn.listEl.innerHTML, /filesystem/);
});

test("ceo stage trace with no rounds still signals caller to keep stage view", () => {
    const { renderCeoStageTraceIntoTurn } = loadApp();
    const turn = makeTurn({ text: "" });

    const rendered = renderCeoStageTraceIntoTurn(turn, {
        stages: [
            {
                stage_id: "stage-no-rounds",
                stage_goal: "inspect repository",
                status: "running",
                tool_round_budget: 3,
                rounds: [],
            },
        ],
    });

    assert.equal(rendered > 0, true);
    assert.match(turn.listEl.innerHTML, /inspect repository/);
});

test("ceo stage trace renders real stage goal and budget from true frontdoor stage data", () => {
    const { renderCeoStageTraceIntoTurn } = loadApp();
    const turn = makeTurn({ text: "" });

    const renderedSteps = renderCeoStageTraceIntoTurn(turn, {
        stages: [
            {
                stage_id: "inflight-stage-1",
                stage_goal: "synthetic carryover",
                status: "running",
                tool_round_budget: 1,
                system_generated: true,
                stage_kind: "normal",
                rounds: [
                    {
                        round_id: "round-synthetic-1",
                        round_index: 1,
                        tools: [
                            { tool_name: "load_tool_context", status: "success", output_text: "ok" },
                            { tool_name: "memory_search", status: "success", output_text: "ok" },
                            { tool_name: "submit_next_stage", status: "success", output_text: "stage advanced" },
                        ],
                    },
                ],
            },
            {
                stage_id: "frontdoor-stage-1",
                stage_goal: "查看当前可检索的长期记忆，并向用户按类别清晰汇总我已记住的内容。",
                status: "running",
                tool_round_budget: 3,
                rounds: [
                    {
                        round_id: "round-1",
                        round_index: 1,
                        tools: [{ tool_name: "load_tool_context", status: "success", output_text: "loaded context" }],
                    },
                    {
                        round_id: "round-2",
                        round_index: 2,
                        tools: [{ tool_name: "memory_search", status: "running", output_text: "" }],
                    },
                ],
            },
        ],
    });

    assert.match(turn.listEl.innerHTML, /查看当前可检索的长期记忆/);
    assert.match(turn.listEl.innerHTML, /本阶段最大轮数为3/);
    assert.doesNotMatch(turn.listEl.innerHTML, /本阶段最大轮数为0/);
    assert.match(turn.listEl.innerHTML, /loaded context/);
    assert.match(turn.listEl.innerHTML, /memory_search/);
    assert.equal(renderedSteps, 2);
    assert.match(turn.metaEl.textContent, /2/);
    assert.doesNotMatch(turn.listEl.innerHTML, /ceo:stage:inflight-stage-1/);
    assert.doesNotMatch(turn.listEl.innerHTML, /synthetic carryover/);
    assert.doesNotMatch(turn.listEl.innerHTML, /submit_next_stage/);
});

test("ceo legacy tool flow hides submit_next_stage events until stage trace arrives", () => {
    const { normalizeCeoSnapshotToolEvents } = loadApp();

    const events = normalizeCeoSnapshotToolEvents([
        {
            tool_name: "submit_next_stage",
            status: "success",
            text: "stage advanced",
            tool_call_id: "submit_next_stage:1",
            source: "user",
        },
    ]);

    assert.deepEqual(events, []);
});

test("ceo composer shows session-local compression toast only for active compressing session", () => {
    const { syncCeoCompressionToast, S, U } = loadApp();

    S.activeSessionId = "web:ceo-a";
    S.ceoSnapshotCache = {
        "web:ceo-a": {
            session_id: "web:ceo-a",
            inflight_turn: {
                status: "running",
                compression: { status: "running", text: COMPRESSION_TEXT, source: "user" },
            },
        },
        "web:ceo-b": {
            session_id: "web:ceo-b",
            inflight_turn: {
                status: "running",
                compression: { status: "running", text: COMPRESSION_TEXT, source: "user" },
            },
        },
    };

    syncCeoCompressionToast();
    assert.equal(U.ceoCompressionToast.hidden, false);
    assert.equal(U.ceoCompressionToastText.textContent, COMPRESSION_TEXT);
});

test("ceo composer hides compression toast when inflight turn is paused", () => {
    const { syncCeoCompressionToast, S, U } = loadApp();

    S.activeSessionId = "web:ceo-a";
    S.ceoSnapshotCache = {
        "web:ceo-a": {
            session_id: "web:ceo-a",
            inflight_turn: {
                status: "paused",
                compression: { status: "running", text: COMPRESSION_TEXT, source: "user" },
            },
        },
    };

    syncCeoCompressionToast();
    assert.equal(U.ceoCompressionToast.hidden, true);
    assert.equal(U.ceoCompressionToastText.textContent, "");
});

test("shared stage status maps active to running semantics", () => {
    const { stageTraceStatus, displayTaskStageStatus } = loadApp();

    assert.equal(stageTraceStatus({ status: "active" }), "running");
    assert.equal(displayTaskStageStatus("active"), displayTaskStageStatus("running"));
});

test("ceo tool output expansion prefetches full content when output_ref is present", async () => {
    const { toggleCeoToolStepOutput, __context } = loadApp();
    let callCount = 0;
    __context.ensureCeoToolStepFullOutput = async (item) => {
        callCount += 1;
        item.dataset.detailText = "FULL OUTPUT";
    };

    const previewEl = new StubHTMLElement();
    const detailEl = new StubHTMLElement();
    const disclosureEl = new StubHTMLButtonElement();
    const item = new StubHTMLElement();
    item.dataset.detailText = "line 1\nline 2\nline 3";
    item.dataset.outputRef = "artifact:artifact:tool-output";
    item.dataset.outputExpanded = "false";
    item._selectors[".interaction-step-preview"] = previewEl;
    item._selectors[".interaction-step-detail"] = detailEl;
    item._selectors[".interaction-step-disclosure"] = disclosureEl;

    toggleCeoToolStepOutput(item);
    await new Promise((resolve) => setTimeout(resolve, 0));

    assert.equal(item.dataset.outputExpanded, "true");
    assert.equal(callCount, 1);
});
