const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const TASK_VIEW_PATH = "g3ku/web/frontend/org_graph_task_view.js";
const APP_PATH = "g3ku/web/frontend/org_graph_app.js";
const TASK_VIEW_CODE = fs.readFileSync(TASK_VIEW_PATH, "utf8");
const APP_CODE = fs.readFileSync(APP_PATH, "utf8");

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
        this.classList = {
            add() {},
            remove() {},
            contains() { return false; },
            toggle() { return false; },
        };
    }

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
        `${TASK_VIEW_CODE}\n${APP_CODE}\nthis.__testExports = { renderCeoStageTraceIntoTurn, syncCeoCompressionToast, S, U };`,
        context
    );
    context.__testExports.U.ceoCompressionToast = new StubHTMLElement();
    context.__testExports.U.ceoCompressionToastText = new StubHTMLElement();
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
                status: "进行中",
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
                status: "进行中",
                tool_round_budget: 3,
                rounds: [],
            },
        ],
    });

    assert.equal(rendered > 0, true);
    assert.match(turn.listEl.innerHTML, /inspect repository/);
});

test("ceo composer shows session-local compression toast only for active compressing session", () => {
    const { syncCeoCompressionToast, S, U } = loadApp();

    S.activeSessionId = "web:ceo-a";
    S.ceoSnapshotCache = {
        "web:ceo-a": {
            session_id: "web:ceo-a",
            inflight_turn: {
                compression: { status: "running", text: "上下文压缩中", source: "user" },
            },
        },
        "web:ceo-b": {
            session_id: "web:ceo-b",
            inflight_turn: {
                compression: { status: "running", text: "上下文压缩中", source: "user" },
            },
        },
    };

    syncCeoCompressionToast();
    assert.equal(U.ceoCompressionToast.hidden, false);
    assert.equal(U.ceoCompressionToastText.textContent, "上下文压缩中");
});
