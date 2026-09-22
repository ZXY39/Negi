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
        this.children = [];
        this.classList = { add() {}, remove() {}, contains() { return false; }, toggle() { return false; } };
    }
    querySelector() { return null; }
    querySelectorAll() { return []; }
    addEventListener() {}
    appendChild(child) { this.children.push(child); return child; }
    setAttribute(name, value) { this.attributes[name] = String(value); }
    removeAttribute(name) { delete this.attributes[name]; }
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

function loadView() {
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
        requestAnimationFrame: (callback) => { callback(); return 1; },
        cancelAnimationFrame: () => {},
        WebSocket: function WebSocket() {},
        addEventListener() {},
        removeEventListener() {},
    };
    context.window = context;
    vm.createContext(context);
    vm.runInContext(
        `${TASK_VIEW_CODE}\n${APP_CODE}\nthis.__testExports = { S, resetTaskTreeRoundSelections };`,
        context
    );
    vm.runInContext(
        `
        this.__loads = [];
        loadTaskTreeSnapshot = (taskId) => { this.__loads.push(taskId); return Promise.resolve(null); };
        renderTree = () => { this.__renderCount = (this.__renderCount || 0) + 1; };
        scheduleTaskDetailSessionPersist = () => {};
    `,
        context
    );
    return { ...context.__testExports, context };
}

test("回到最新树：清掉轮次选择后补一次整树快照，默认轮次的孩子能回来", () => {
    const view = loadView();
    view.S.currentTaskId = "task:1";
    view.S.treeRootNodeId = "node:root";
    view.S.treeSelectedRoundByNodeId = { "node:root": "round_2" };

    view.resetTaskTreeRoundSelections();

    assert.deepEqual(Array.from(view.context.__loads), ["task:1"]);
    assert.deepEqual(Object.keys(view.S.treeSelectedRoundByNodeId), []);
});

test("回到最新树：本来就没有轮次选择时不整树重拉", () => {
    const view = loadView();
    view.S.currentTaskId = "task:1";
    view.S.treeRootNodeId = "node:root";
    view.S.treeSelectedRoundByNodeId = {};

    view.resetTaskTreeRoundSelections();

    assert.deepEqual(Array.from(view.context.__loads), []);
});

test("回到最新树：只留脏键（空轮次）也不算有选择，不触发重拉", () => {
    const view = loadView();
    view.S.currentTaskId = "task:1";
    view.S.treeSelectedRoundByNodeId = { "node:root": "  ", "": "round_2" };

    view.resetTaskTreeRoundSelections();

    assert.deepEqual(Array.from(view.context.__loads), []);
});
