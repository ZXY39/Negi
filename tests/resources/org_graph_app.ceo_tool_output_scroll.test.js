const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

// 工具输出框滚动位置保持:用户在回翻输出时,新输出/每秒时长刷新/整段重建都不得
// 把他正在读的位置清零。覆盖三处:setCeoToolStepOutput 的原位文本同步、
// hydrate 的填充、以及 renderCeoStageTraceIntoTurn 整段重建的嵌套滚动还原。

const APP_PATH = "g3ku/web/frontend/org_graph_app.js";
const APP_CODE = fs.readFileSync(APP_PATH, "utf8");

class StubElement {}
class StubButtonElement extends StubElement {}

class StubHTMLElement extends StubElement {
    constructor() {
        super();
        this.tagName = "DIV";
        this.className = "";
        this.hidden = false;
        this.open = false;
        this._text = "";
        this.dataset = {};
        this.attributes = {};
        this.children = [];
        this._scrollTop = 0;
        this._scrollResetCount = 0;
        this._maxScrollTop = Number.POSITIVE_INFINITY;
        this._qs = {};
        this._qsAll = {};
        this.classList = {
            add: () => {},
            remove: () => {},
            contains: (name) => String(this.className || "").split(/\s+/).includes(String(name || "")),
            toggle: () => {},
        };
        this.style = {};
    }

    // 真实浏览器语义:重写 textContent 会替换子节点、内容高度瞬间塌陷,滚动位置随之归零。
    get textContent() {
        return this._text === undefined ? "" : this._text;
    }

    set textContent(value) {
        this._text = String(value ?? "");
        this._scrollTop = 0;
    }

    get scrollTop() {
        return Number(this._scrollTop || 0);
    }

    set scrollTop(value) {
        const numeric = Number(value);
        const next = Number.isFinite(numeric) ? Math.max(0, numeric) : 0;
        this._scrollTop = Math.min(next, this._maxScrollTop);
    }

    setAttribute(name, value) {
        this.attributes[name] = String(value);
        if (name === "data-trace-key") this.dataset.traceKey = String(value);
    }

    getAttribute(name) {
        return Object.prototype.hasOwnProperty.call(this.attributes, name) ? this.attributes[name] : null;
    }

    querySelector(selector) {
        return this._qs[selector] || null;
    }

    querySelectorAll(selector) {
        return this._qsAll[selector] || [];
    }

    appendChild(child) {
        this.children.push(child);
        return child;
    }
}

// 输出框自身:封顶 max-height + overflow auto 的滚动容器。
class ScrollBox extends StubHTMLElement {
    constructor(className = "") {
        super();
        this.className = className;
    }

    scrollTo(top) {
        this._scrollTop = Math.min(Math.max(0, Number(top) || 0), this._maxScrollTop);
    }
}

function makeToolStep({ detailText = "", expanded = true, scrollTop = 0 } = {}) {
    const item = new StubHTMLElement();
    const preview = new ScrollBox("interaction-step-preview");
    const detail = new ScrollBox("interaction-step-detail");
    const disclosure = new StubHTMLElement();
    const copy = new StubHTMLElement();
    detail.scrollTo(scrollTop);
    item._qs[".interaction-step-preview"] = preview;
    item._qs[".interaction-step-detail"] = detail;
    item._qs[".interaction-step-disclosure"] = disclosure;
    item._qs[".interaction-step-copy"] = copy;
    item.dataset.detailText = detailText;
    item.dataset.outputExpanded = expanded ? "true" : "false";
    item.__els = { preview, detail, disclosure, copy };
    return item;
}

const LONG_OUTPUT = ["line 1", "line 2", "line 3", "line 4", "line 5"].join("\n");

function makeTraceStep({ traceKey = "", traceScrollTop = 0, detailScrollTop = 0 } = {}) {
    const step = new StubHTMLElement();
    step.dataset.traceKey = traceKey;
    const code = new ScrollBox("code-block task-trace-code");
    code.scrollTo(traceScrollTop);
    const detail = new ScrollBox("interaction-step-detail");
    detail.scrollTo(detailScrollTop);
    // 文档顺序:d 类(interaction-step-detail)与 c 类(task-trace-code)各自按出现次序编号。
    step._qsAll[".interaction-step-detail, .task-trace-code"] = [detail, code];
    return step;
}

function loadApp() {
    const context = {
        console,
        setTimeout,
        clearTimeout,
        setInterval,
        clearInterval,
        queueMicrotask,
        structuredClone: global.structuredClone,
        navigator: { clipboard: { writeText: async () => {} } },
        location: { protocol: "http:", host: "localhost", pathname: "/org_graph.html" },
        localStorage: { getItem: () => null, setItem: () => {}, removeItem: () => {} },
        sessionStorage: { getItem: () => null, setItem: () => {}, removeItem: () => {} },
        document: {
            getElementById: () => null,
            querySelector: () => null,
            querySelectorAll: () => [],
            createElement: () => new StubHTMLElement(),
            addEventListener: () => {},
        },
        Element: StubElement,
        HTMLElement: StubHTMLElement,
        HTMLButtonElement: StubButtonElement,
        URLSearchParams,
        URL,
        AbortController,
        fetch: async () => ({ ok: true, json: async () => ({}) }),
        lucide: { createIcons() {} },
        marked: { parse: (value) => String(value) },
        DOMPurify: { sanitize: (value) => String(value) },
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
        `${APP_CODE}\nthis.__testExports = { setCeoToolStepOutput, setTextContentPreservingScroll, captureCeoTurnTraceViewState, applyCeoTurnTraceViewState };`,
        context
    );
    return context.__testExports;
}

test("同一份输出重复同步时不重置滚动位置", () => {
    const api = loadApp();
    const item = makeToolStep({ detailText: LONG_OUTPUT, scrollTop: 40 });
    const { detail } = item.__els;

    api.setCeoToolStepOutput(item, LONG_OUTPUT);

    assert.equal(detail.scrollTop, 40);
});

test("输出增长时保留用户正在读的位置,不被新输出清零", () => {
    const api = loadApp();
    const item = makeToolStep({ detailText: LONG_OUTPUT, scrollTop: 40 });
    const { detail } = item.__els;

    api.setCeoToolStepOutput(item, `${LONG_OUTPUT}\nline 6`);

    assert.equal(detail.scrollTop, 40);
    assert.match(detail.textContent, /line 6/);
});

test("内容缩短到不足原滚动量时按新内容上限收敛,而不是归零", () => {
    const api = loadApp();
    const item = makeToolStep({ detailText: LONG_OUTPUT, scrollTop: 40 });
    const { detail } = item.__els;
    detail._maxScrollTop = 8;

    api.setCeoToolStepOutput(item, "只剩两行\n第二行");

    assert.equal(detail.scrollTop, 8);
});

test("文本未变时整体跳过写入,输出折行状态仍随展开态同步", () => {
    const api = loadApp();
    const item = makeToolStep({ detailText: LONG_OUTPUT, expanded: false });
    const { detail, preview } = item.__els;

    api.setCeoToolStepOutput(item, LONG_OUTPUT);

    assert.equal(detail.hidden, true);
    assert.equal(preview.hidden, false);
});

test("整段重建按阶段键还原输出框与阶段代码块的滚动位置", () => {
    const api = loadApp();
    const oldStep = makeTraceStep({
        traceKey: "ceo:stage:s1",
        traceScrollTop: 30,
        detailScrollTop: 55,
    });
    const oldTurn = {
        listEl: new StubHTMLElement(),
        flowEl: { open: true },
        lastExecutionTraceSummary: { stages: [{ stage_id: "s1" }] },
    };
    oldTurn.listEl._qsAll[".task-trace-step"] = [oldStep];
    oldTurn.listEl._qsAll[".task-trace-round-tools"] = [];

    const state = api.captureCeoTurnTraceViewState(oldTurn);
    assert.equal(state.scrolls["ceo:stage:s1::d::0"], 55);
    assert.equal(state.scrolls["ceo:stage:s1::c::0"], 30);

    const newStep = makeTraceStep({ traceKey: "ceo:stage:s1" });
    const newTurn = { listEl: new StubHTMLElement(), flowEl: { open: false } };
    newTurn.listEl._qsAll[".task-trace-step"] = [newStep];
    newTurn.listEl._qsAll[".task-trace-round-tools"] = [];

    api.applyCeoTurnTraceViewState(newTurn, state);

    const [detail, code] = newStep._qsAll[".interaction-step-detail, .task-trace-code"];
    assert.equal(detail.scrollTop, 55);
    assert.equal(code.scrollTop, 30);
});

test("没有滚动过的输出框不写入状态,避免污染重建快照", () => {
    const api = loadApp();
    const step = makeTraceStep({ traceKey: "ceo:stage:s2" });
    const turn = {
        listEl: new StubHTMLElement(),
        flowEl: { open: true },
        lastExecutionTraceSummary: { stages: [{ stage_id: "s2" }] },
    };
    turn.listEl._qsAll[".task-trace-step"] = [step];
    turn.listEl._qsAll[".task-trace-round-tools"] = [];

    const state = api.captureCeoTurnTraceViewState(turn);

    assert.deepEqual(Object.keys(state.scrolls), []);
});