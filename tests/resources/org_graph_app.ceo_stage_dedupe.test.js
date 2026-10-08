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
    constructor(className = "") {
        super();
        this.className = className;
        this.hidden = false;
        this.textContent = "";
        this.innerHTML = "";
        this.dataset = {};
        this.style = {};
        this.attributes = {};
        this._selectors = {};
        this._selectorLists = {};
        this._children = [];
        this.parentElement = null;
        const classes = () => new Set(String(this.className || "").split(/\s+/).filter(Boolean));
        const write = (set) => { this.className = [...set].join(" "); };
        this.classList = {
            add: (...tokens) => { const set = classes(); tokens.forEach((t) => set.add(t)); write(set); },
            remove: (...tokens) => { const set = classes(); tokens.forEach((t) => set.delete(t)); write(set); },
            contains: (token) => classes().has(token),
        };
    }

    querySelector(selector) { return this._selectors[selector] || null; }
    querySelectorAll(selector) { return this._selectorLists[selector] || []; }
    addEventListener() {}
    appendChild(child) {
        child.parentElement = this;
        this._children.push(child);
        return child;
    }

    remove() {
        if (!this.parentElement) return;
        this.parentElement._children = this.parentElement._children.filter((item) => item !== this);
        this.parentElement = null;
    }

    get children() { return this._children; }
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
    context.ApiClient = { getActiveSessionId: () => "web:test" };
    vm.createContext(context);
    vm.runInContext(
        `${TASK_VIEW_CODE}\n${APP_CODE}
        this.__testExports = { U, reconcileCeoFeedStageCards };`,
        context
    );
    return context.__testExports;
}

function stageCard({ stageId, className, rounds = [], summary = "", label = "进行中" }) {
    const card = new StubHTMLElement(`interaction-step task-trace-step ${className}`.trim());
    card.dataset.stageId = stageId;
    const status = new StubHTMLElement("interaction-step-status");
    status.textContent = label;
    // 真 DOM 里这颗徽章带图标 markup，归并搬的是 innerHTML。
    status.innerHTML = label;
    card._selectors[".interaction-step-status"] = status;
    const body = new StubHTMLElement("task-trace-body");
    rounds.forEach((roundKey) => {
        const round = new StubHTMLElement("task-trace-round-group");
        round.dataset.roundKey = roundKey;
        body.appendChild(round);
    });
    if (summary) {
        const block = new StubHTMLElement("task-trace-stage-summary");
        block.textContent = summary;
        body.appendChild(block);
        body._selectors[".task-trace-stage-summary"] = block;
    }
    body._selectorLists[".task-trace-round-group"] = body._children.filter(
        (item) => item.classList.contains("task-trace-round-group"),
    );
    card._selectors[".task-trace-body"] = body;
    return card;
}

function feedWith(cards) {
    const feed = new StubHTMLElement("ceo-feed");
    feed._children = cards;
    cards.forEach((card) => { card.parentElement = feed; });
    feed._selectorLists[".task-trace-step[data-stage-id]"] = cards;
    return feed;
}

test("收尾阶段的后续副本并回最早那张，卡数与轮次都不再重复", () => {
    const { U, reconcileCeoFeedStageCards } = loadApp();
    const host = stageCard({ stageId: "frontdoor-stage-6", className: "running", rounds: ["round:1"] });
    const donor = stageCard({
        stageId: "frontdoor-stage-6",
        className: "success",
        rounds: ["round:1", "round:2"],
        summary: "D4 的结论：权限判定入口在 guard 包，档位三档。",
    });
    const feed = feedWith([host, donor]);
    U.ceoFeed = feed;

    const merged = reconcileCeoFeedStageCards();

    assert.equal(merged, 1);
    assert.equal(feed.children.length, 1);
    const body = host._selectors[".task-trace-body"];
    const keys = body.children.filter((item) => item.classList.contains("task-trace-round-group"))
        .map((item) => item.dataset.roundKey);
    // 重复的 round:1 不搬两次，缺的 round:2 并进来。
    assert.deepEqual(keys, ["round:1", "round:2"]);
    assert.match(body.children.at(-1).textContent, /D4 的结论/);
});

test("未收尾阶段的副本各留一张：归档半截与续跑半截是两件事", () => {
    const { U, reconcileCeoFeedStageCards } = loadApp();
    const archived = stageCard({ stageId: "frontdoor-stage-7", className: "running", rounds: ["round:1"] });
    const continued = stageCard({ stageId: "frontdoor-stage-7", className: "running", rounds: ["round:2"] });
    const feed = feedWith([archived, continued]);
    U.ceoFeed = feed;

    assert.equal(reconcileCeoFeedStageCards(), 0);
    assert.equal(feed.children.length, 2);
});

test("被点名移出上下文的收尾副本并进来时，宿主保留那颗徽章", () => {
    const { U, reconcileCeoFeedStageCards } = loadApp();
    const host = stageCard({ stageId: "frontdoor-stage-8", className: "success", rounds: ["round:1"], label: "完成" });
    const donor = stageCard({
        stageId: "frontdoor-stage-8",
        className: "success stage-evicted",
        rounds: ["round:2"],
        summary: "收口总结。",
        label: "已移出上下文",
    });
    const feed = feedWith([host, donor]);
    U.ceoFeed = feed;

    reconcileCeoFeedStageCards();

    assert.equal(host.classList.contains("stage-evicted"), true);
    assert.equal(host._selectors[".interaction-step-status"].innerHTML, "已移出上下文");
});
