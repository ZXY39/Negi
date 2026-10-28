const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const { readFrontendSources } = require("./support/frontend_sources.js");

// 在飞工具行与普通工具同一副面孔：只有转圈图标 + 原始工具名，不套中文别名、
// 不挂“已运行 N 秒”；本轮总时长只由流程头部秒数表达。

const APP_CODE = readFrontendSources(["app"]).join("\n");
const CSS = fs.readFileSync("g3ku/web/frontend/org_graph.css", "utf8").replace(/\r\n/g, "\n");

const LIVE_ROW_SELECTORS = [
    ".interaction-step-title",
    ".interaction-step-started",
    ".interaction-step-status",
    ".interaction-step-icon",
    ".interaction-step-preview",
    ".interaction-step-detail",
    ".interaction-step-disclosure",
    ".interaction-step-copy",
];

class StubElement {}

class StubHTMLElement extends StubElement {
    constructor() {
        super();
        this.dataset = {};
        this.style = {};
        this.attributes = {};
        this.children = [];
        this.hidden = false;
        this.textContent = "";
        this.parentElement = null;
        this._innerHTML = "";
        this._selectors = new Map();
        this.classList = {
            add() {},
            remove() {},
            toggle() {},
            contains() { return false; },
        };
    }

    set innerHTML(value) {
        this._innerHTML = String(value);
        LIVE_ROW_SELECTORS.forEach((selector) => {
            const el = new StubHTMLElement();
            el.className = selector;
            this._selectors.set(selector, el);
        });
    }

    get innerHTML() {
        return this._innerHTML;
    }

    querySelector(selector) {
        return this._selectors.get(selector) || null;
    }

    querySelectorAll() {
        return [];
    }

    setAttribute(name, value) {
        this.attributes[name] = String(value);
    }

    appendChild(child) {
        child.parentElement = this;
        this.children.push(child);
        return child;
    }

    addEventListener() {}

    remove() {
        this.removed = true;
        this.parentElement = null;
    }
}

class StubHTMLButtonElement extends StubHTMLElement {}

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
        Element: StubElement,
        HTMLElement: StubHTMLElement,
        HTMLButtonElement: StubHTMLButtonElement,
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
        `${APP_CODE}\nthis.__testExports = { ceoFriendlyToolName, applyCeoToolEventToTurn, HTMLElement };`,
        context
    );
    return context.__testExports;
}

function makeTurn() {
    return {
        listEl: new StubHTMLElement(),
        flowEl: { hidden: true },
        metaEl: new StubHTMLElement(),
        footerEl: null,
        toggleEl: null,
        steps: 0,
        hasError: false,
        liveToolStepsEl: null,
        lastExecutionTraceSummary: null,
        railTurns: [],
    };
}

test("exec keeps its raw tool id, same as ordinary tools", () => {
    const { ceoFriendlyToolName } = loadApp();

    assert.equal(ceoFriendlyToolName("exec"), "exec");
    assert.equal(ceoFriendlyToolName("read_file"), "read_file");
    // 其他中文别名不受影响。
    assert.equal(ceoFriendlyToolName("skill-installer"), "技能安装");
});

test("live tool row renders icon + raw name only, with no elapsed-time badge", () => {
    const { applyCeoToolEventToTurn } = loadApp();
    const turn = makeTurn();

    const item = applyCeoToolEventToTurn(turn, {
        tool_name: "exec",
        status: "running",
        kind: "tool_start",
        tool_call_id: "call_live_1",
        timestamp: new Date().toISOString(),
    });

    assert.ok(item, "live row should be created");
    assert.equal(item.dataset.toolName, "exec");
    assert.equal(item.querySelector(".interaction-step-title").textContent, "exec");
    assert.doesNotMatch(item.innerHTML, /interaction-step-runtime/);
    assert.doesNotMatch(item.innerHTML, /已运行/);
    assert.equal(item.querySelector(".interaction-step-runtime"), null);
});

test("stylesheets carry no live-row elapsed badge rule", () => {
    assert.doesNotMatch(CSS, /\.task-trace-live-steps \.interaction-step-runtime/);
});

test("the usage row sits beside the placeholder arc while the turn is loading", () => {
    const rule = /\.ceo-turn-content:has\(> \.assistant-text\.assistant-text-loading\)\s*\{([\s\S]*?)\n\}/.exec(CSS);
    assert.ok(rule, "loading-state grid rule not found");
    assert.match(rule[1], /display:\s*grid/);
    assert.match(rule[1], /grid-template-columns:\s*max-content minmax\(0, 1fr\)/);
    assert.match(rule[1], /align-items:\s*center/);

    const arc = /\.ceo-turn-content:has\(> \.assistant-text\.assistant-text-loading\)\s*>\s*\.assistant-text-loading\s*\{([\s\S]*?)\n\}/.exec(CSS);
    assert.ok(arc, "arc column rule not found");
    assert.match(arc[1], /grid-column:\s*1;/);

    const meta = /\.ceo-turn-content:has\(> \.assistant-text\.assistant-text-loading\)\s*>\s*\.ceo-turn-meta\s*\{([\s\S]*?)\n\}/.exec(CSS);
    assert.ok(meta, "meta column rule not found");
    assert.match(meta[1], /grid-column:\s*2;/);

    // 流程框与思考行仍通栏，不被挤进第二列。
    const wide = /\.ceo-turn-content:has\(> \.assistant-text\.assistant-text-loading\)\s*>\s*:not\(\.assistant-text-loading\):not\(\.ceo-turn-meta\)\s*\{([\s\S]*?)\n\}/.exec(CSS);
    assert.ok(wide, "full-width rule not found");
    assert.match(wide[1], /grid-column:\s*1 \/ -1;/);
});
