const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const TASK_VIEW_PATH = "g3ku/web/frontend/org_graph_task_view.js";
const APP_PATH = "g3ku/web/frontend/org_graph_app.js";
const TASK_VIEW_CODE = fs.readFileSync(TASK_VIEW_PATH, "utf8");
const APP_CODE = fs.readFileSync(APP_PATH, "utf8");

const STEP_SELECTOR = ".task-trace-step[data-stage-id]";

class StubElement {}

class StubHTMLElement extends StubElement {
    constructor() {
        super();
        this.hidden = false;
        this.textContent = "";
        this._innerHTML = "";
        this.className = "";
        this.dataset = {};
        this.style = {};
        this.attributes = {};
        this._selectors = {};
        this._selectorLists = {};
        this._children = [];
        this.parentElement = null;
        this.classList = {
            add: (...tokens) => {
                const classes = new Set(String(this.className || "").split(/\s+/).filter(Boolean));
                tokens.forEach((token) => classes.add(token));
                this.className = [...classes].join(" ");
            },
            remove: (...tokens) => {
                const classes = new Set(String(this.className || "").split(/\s+/).filter(Boolean));
                tokens.forEach((token) => classes.delete(token));
                this.className = [...classes].join(" ");
            },
            contains: (token) => String(this.className || "").split(/\s+/).includes(token),
            toggle: (token, force) => {
                const hasToken = String(this.className || "").split(/\s+/).includes(token);
                const shouldAdd = force == null ? !hasToken : !!force;
                if (shouldAdd) this.classList.add(token);
                else this.classList.remove(token);
                return shouldAdd;
            },
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

    get innerHTML() {
        return this._innerHTML;
    }

    set innerHTML(value) {
        this._innerHTML = String(value);
    }
}

class StubDocument {
    getElementById() { return null; }
    createElement() { return new StubHTMLElement(); }
    querySelector() { return null; }
    querySelectorAll() { return []; }
    addEventListener() {}
}

function makeTurn() {
    return {
        textEl: { textContent: "", innerHTML: "", classList: { add() {}, remove() {} } },
        flowEl: { hidden: true, open: false },
        metaEl: { textContent: "" },
        listEl: new StubHTMLElement(),
        footerEl: { hidden: true },
        toggleEl: { textContent: "", setAttribute() {} },
        el: new StubHTMLElement(),
    };
}

// 对账只读 class + data-stage-id + 标签节点，所以步骤用最小桩即可，
// 不需要真的解析 innerHTML。
function makeStep(stageId, status, { label = "" } = {}) {
    const step = new StubHTMLElement();
    step.className = `interaction-step task-trace-step ${status}`;
    step.dataset.stageId = stageId;
    const statusEl = new StubHTMLElement();
    statusEl.className = "interaction-step-status";
    statusEl.textContent = label;
    step._selectors[".interaction-step-status"] = statusEl;
    return step;
}

function makeFeed(steps) {
    const feed = new StubHTMLElement();
    feed._selectorLists[STEP_SELECTOR] = steps;
    return feed;
}

function labelOf(step) {
    return step._selectors[".interaction-step-status"].textContent;
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
        HTMLButtonElement: StubHTMLElement,
        HTMLInputElement: StubHTMLElement,
        HTMLTextAreaElement: StubHTMLElement,
        HTMLSelectElement: StubHTMLElement,
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
        `${TASK_VIEW_CODE}\n${APP_CODE}\nthis.__testExports = { renderCeoStageTraceIntoTurn, reconcileCeoFeedStageStatuses };`,
        context
    );
    return context.__testExports;
}

test("ceo stage cards carry an explicit data-stage-id next to the trace key", () => {
    const { renderCeoStageTraceIntoTurn } = loadApp();
    const turn = makeTurn();

    renderCeoStageTraceIntoTurn(turn, {
        stages: [
            { stage_id: "frontdoor-stage-500", stage_goal: "派发审查", status: "running", rounds: [] },
            { stage_index: 7, stage_goal: "无 id 的存量阶段", status: "running", rounds: [] },
        ],
    });

    const html = turn.listEl.innerHTML;
    assert.match(html, /data-stage-id="frontdoor-stage-500"/);
    // traceKey 允许退化成 stage_index / 序号，那种 key 跨回合会撞车，
    // 所以退化时不得伪造出 data-stage-id。
    assert.equal(/data-stage-id="7"/.test(html), false);
    assert.equal((html.match(/data-stage-id=/g) || []).length, 1);
});

test("reconcile raises earlier copies of a stage to its terminal status", () => {
    const { reconcileCeoFeedStageStatuses } = loadApp();
    const first = makeStep("frontdoor-stage-500", "running", { label: "进行中" });
    const second = makeStep("frontdoor-stage-500", "running", { label: "进行中" });
    const last = makeStep("frontdoor-stage-500", "success", { label: "完成" });

    const applied = reconcileCeoFeedStageStatuses(makeFeed([first, second, last]));

    assert.equal(applied, 2);
    assert.match(first.className, /\bsuccess\b/);
    assert.equal(first.className.includes("running"), false);
    assert.equal(labelOf(first), "完成");
    assert.match(second.className, /\bsuccess\b/);
    assert.equal(labelOf(second), "完成");
    // 较新的那份是判定依据，本身不动
    assert.match(last.className, /\bsuccess\b/);
    assert.equal(labelOf(last), "完成");
});

test("reconcile never walks a settled stage backwards", () => {
    const { reconcileCeoFeedStageStatuses } = loadApp();
    const failed = makeStep("frontdoor-stage-501", "error", { label: "失败" });
    const running = makeStep("frontdoor-stage-501", "running", { label: "进行中" });

    // 文档顺序里最后一条是 error：较早的 running 副本升到 error，而不是被 success 覆盖。
    const applied = reconcileCeoFeedStageStatuses(makeFeed([running, failed]));

    assert.equal(applied, 1);
    assert.match(running.className, /\berror\b/);
    assert.equal(labelOf(running), "失败");
    assert.match(failed.className, /\berror\b/);
});

test("reconcile leaves a stage alone while its newest copy is still running", () => {
    const { reconcileCeoFeedStageStatuses } = loadApp();
    const earlier = makeStep("frontdoor-stage-502", "running", { label: "进行中" });
    const latest = makeStep("frontdoor-stage-502", "info", { label: "进行中" });

    const applied = reconcileCeoFeedStageStatuses(makeFeed([earlier, latest]));

    assert.equal(applied, 0);
    assert.match(earlier.className, /\brunning\b/);
    assert.equal(labelOf(earlier), "进行中");
});

test("reconcile skips steps without a stage id and single-occurrence stages", () => {
    const { reconcileCeoFeedStageStatuses } = loadApp();
    const anonymous = new StubHTMLElement();
    anonymous.className = "interaction-step task-trace-step running";
    anonymous.dataset = {};
    const anonymousLabel = new StubHTMLElement();
    anonymousLabel.textContent = "进行中";
    anonymous._selectors[".interaction-step-status"] = anonymousLabel;

    const solo = makeStep("frontdoor-stage-600", "running", { label: "进行中" });
    const applied = reconcileCeoFeedStageStatuses(makeFeed([anonymous, solo]));

    assert.equal(applied, 0);
    assert.match(anonymous.className, /\brunning\b/);
    assert.match(solo.className, /\brunning\b/);
});
