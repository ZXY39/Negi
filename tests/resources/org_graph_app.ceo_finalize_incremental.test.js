const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

// finalize 增量渲染契约:
// - final 带 user_messages 且 DOM 与记录完全对齐时,只补挂新用户气泡 + 就地收尾回合,
//   不整页重建(既有节点对象引用保持不变);
// - 任何 DOM 偏差(无关 key 的子节点、最后子节点不是本回合)回退全量快照重建;
// - 无 user_messages 的收尾保持原位语义(不需要 feed);
// - 同一签名快照重复推送时 renderCeoSnapshot 跳过重建。

const APP_PATH = "g3ku/web/frontend/org_graph_app.js";
const APP_CODE = fs.readFileSync(APP_PATH, "utf8");

class StubElement {}

class StubHTMLElement extends StubElement {
    constructor() {
        super();
        this.tagName = "DIV";
        this.className = "";
        this.hidden = false;
        this.open = false;
        this.textContent = "";
        this.dataset = {};
        this.attributes = {};
        this.children = [];
        this.scrollTop = 0;
        this.scrollHeight = 0;
        this.clientHeight = 0;
        this._qs = {};
        this._qsAll = {};
        this._contentTop = 0;
        this._height = 10;
        this._feed = null;
        this._innerHTML = "";
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
                const has = String(this.className || "").split(/\s+/).includes(token);
                const next = typeof force === "boolean" ? force : !has;
                if (next === has) return next;
                next
                    ? this.classList.add(token)
                    : this.classList.remove(token);
                return next;
            },
        };
    }

    get innerHTML() {
        return this._innerHTML;
    }

    set innerHTML(value) {
        this._innerHTML = String(value);
    }

    get lastElementChild() {
        return this.children.length ? this.children[this.children.length - 1] : null;
    }

    setAttribute(name, value) {
        this.attributes[name] = String(value);
        if (name === "data-ceo-key") this.dataset.ceoKey = String(value);
    }

    getAttribute(name) {
        return Object.prototype.hasOwnProperty.call(this.attributes, name) ? this.attributes[name] : null;
    }

    removeAttribute(name) {
        delete this.attributes[name];
    }

    querySelector(selector) {
        return this._qs[selector] || null;
    }

    querySelectorAll(selector) {
        return this._qsAll[selector] || [];
    }

    getBoundingClientRect() {
        const feedScrollTop = this._feed && Number(this._feed.scrollTop || 0);
        return { top: this._contentTop - feedScrollTop, height: this._height };
    }

    appendChild(child) {
        this.children.push(child);
        if (child && child._feed !== undefined) child._feed = this;
        return child;
    }

    insertBefore(child, ref) {
        const next = this.children.filter((item) => item !== child);
        const index = next.indexOf(ref);
        if (index < 0) next.push(child);
        else next.splice(index, 0, child);
        this.children = next;
        if (child && child._feed !== undefined) child._feed = this;
        return child;
    }

    remove() {
        const parentChildren = this._feed && this._feed.children ? this._feed.children : null;
        if (parentChildren) {
            const index = parentChildren.indexOf(this);
            if (index >= 0) parentChildren.splice(index, 1);
        }
        this._removed = true;
    }
}

class FeedStub extends StubHTMLElement {
    constructor({ children = [], scrollTop = 0, scrollHeight = 0, clientHeight = 0 } = {}) {
        super();
        this.children = [...children];
        children.forEach((child) => {
            if (child) child._feed = this;
        });
        this.scrollTop = scrollTop;
        this.scrollHeight = scrollHeight;
        this.clientHeight = clientHeight;
        this.resetCount = 0;
    }

    set innerHTML(value) {
        const next = String(value);
        if (next === "") {
            this.resetCount += 1;
            this.children = [];
        }
        this._innerHTML = next;
    }

    get innerHTML() {
        return this._innerHTML;
    }

    querySelectorAll(selector) {
        if (selector === ".ceo-turn-message") {
            return this.children.filter((child) => child && child._isTurn);
        }
        if (selector === ".task-trace-step" || selector === "img" || selector === ".interaction-step") {
            return [];
        }
        return this._qsAll[selector] || [];
    }
}

function makeMessageEl(key = "") {
    const el = new StubHTMLElement();
    if (key) el.dataset.ceoKey = key;
    return el;
}

function makeFinalizeTurn({ turnId = "", source = "user" } = {}) {
    const el = new StubHTMLElement();
    el._isTurn = true;
    if (turnId) el.dataset.ceoKey = `turn:${turnId}`;
    const textEl = new StubHTMLElement();
    textEl.className = "assistant-text pending";
    const flowEl = new StubHTMLElement();
    flowEl.open = false;
    flowEl.hidden = true;
    const turn = {
        el,
        textEl,
        flowEl,
        listEl: new StubHTMLElement(),
        metaEl: new StubHTMLElement(),
        footerEl: new StubHTMLElement(),
        usageEl: new StubHTMLElement(),
        reminderEl: new StubHTMLElement(),
        steps: 0,
        hasError: false,
        finalized: false,
        historyExpanded: false,
        liveStreamText: "",
        lastExecutionTraceSummary: null,
        turnId: String(turnId || ""),
        source: String(source || "user"),
    };
    return turn;
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
        setTraceRoundActiveTool: (host, toolKey) => {
            if (host && host.dataset) host.dataset.activeToolKey = String(toolKey || "");
        },
        hydrateTraceOutputBlocks: () => {},
    };
    context.window = context;
    vm.createContext(context);
    vm.runInContext(
        `${APP_CODE}\nthis.__testExports = { S, U, finalizeCeoTurn, renderCeoSnapshot, buildCeoRenderSignature, buildCeoMessageKeyList };`,
        context
    );
    return context.__testExports;
}

function setup() {
    const api = loadApp();
    api.S.activeSessionId = "s1";
    api.S.ceoFeedRenderSessionId = "s1";
    api.S.ceoScrollToLatestOnSnapshot = false;
    return api;
}

test("final 带 user_messages 且 DOM 对齐时增量收尾:既有节点引用不变,新用户气泡插入回合前", () => {
    const api = setup();
    const m1 = { role: "user", content: "q1" };
    const m2 = { role: "assistant", content: "a1", turn_id: "t0" };
    api.S.ceoSnapshotCache["s1"] = { session_id: "s1",
        messages: [m1, m2],
        inflight_turn: { source: "user", status: "running", turn_id: "t1" },
    };
    api.S.ceoFeedRenderedMessageKeys = ["m:-:user:0", "m:t0:assistant:0"];
    const turn = makeFinalizeTurn({ turnId: "t1" });
    api.S.ceoPendingTurns = [turn];
    const msgEl1 = makeMessageEl("m:-:user:0");
    const msgEl2 = makeMessageEl("m:t0:assistant:0");
    turn.el._feed = null;
    api.U.ceoFeed = new FeedStub({ children: [msgEl1, msgEl2, turn.el], scrollHeight: 400, clientHeight: 300 });

    api.finalizeCeoTurn("done", { source: "user", turn_id: "t1", user_messages: [{ role: "user", content: "q2" }] });

    assert.equal(turn.finalized, true);
    assert.equal(turn.textEl.innerHTML.includes("done"), true);
    // 既有消息节点与回合节点的对象引用全部保留(没有整页重建)。
    assert.equal(api.U.ceoFeed.children[0], msgEl1);
    assert.equal(api.U.ceoFeed.children[1], msgEl2);
    assert.equal(api.U.ceoFeed.children[3], turn.el);
    // 新用户气泡插在回合元素之前,并带稳定 key。
    const newUserEl = api.U.ceoFeed.children[2];
    assert.equal(newUserEl.getAttribute("data-ceo-key"), "m:-:user:1");
    // 缓存与渲染记录同步更新:4 条消息,keys = 头两条 + 新用户 + 最终答复。
    assert.equal(api.S.ceoSnapshotCache["s1"].messages.length, 4);
    assert.deepEqual(api.S.ceoFeedRenderedMessageKeys.map(String), [
        "m:-:user:0",
        "m:t0:assistant:0",
        "m:-:user:1",
        "m:-:assistant:0",
    ]);
    assert.equal(api.S.ceoFeedRenderSessionId, "s1");
    assert.ok(api.S.ceoFeedRenderSignature.length > 0);
});

test("DOM 与记录不一致时回退全量快照重建,渲染记录对齐新消息列表", () => {
    const api = setup();
    const m1 = { role: "user", content: "q1" };
    const m2 = { role: "assistant", content: "a1" };
    api.S.ceoSnapshotCache["s1"] = { session_id: "s1",
        messages: [m1, m2],
        inflight_turn: { source: "user", status: "running", turn_id: "t1" },
    };
    api.S.ceoFeedRenderedMessageKeys = ["m:-:user:0", "m:-:assistant:0"];
    const turn = makeFinalizeTurn({ turnId: "t1" });
    api.S.ceoPendingTurns = [turn];
    const msgEl1 = makeMessageEl("m:-:user:0");
    const unkeyedEl = makeMessageEl(""); // 未打 key 的子节点 = 渲染记录与 DOM 脱节
    const feed = new FeedStub({ children: [msgEl1, unkeyedEl, turn.el], scrollHeight: 400, clientHeight: 300 });
    api.U.ceoFeed = feed;

    api.finalizeCeoTurn("done", { source: "user", turn_id: "t1", user_messages: [{ role: "user", content: "q2" }] });

    assert.equal(feed.resetCount, 1);
    assert.deepEqual(api.S.ceoFeedRenderedMessageKeys.map(String), [
        "m:-:user:0",
        "m:-:assistant:0",
        "m:-:user:1",
        "m:-:assistant:1",
    ]);
});

test("无 user_messages 的收尾保持原位语义,不需要 feed 也不整页重建", () => {
    const api = setup();
    const m1 = { role: "user", content: "q1" };
    api.S.ceoSnapshotCache["s1"] = { session_id: "s1", messages: [m1] };
    api.S.ceoFeedRenderedMessageKeys = ["m:-:user:0"];
    const turn = makeFinalizeTurn({ turnId: "t1" });
    api.S.ceoPendingTurns = [turn];
    api.U.ceoFeed = null;

    api.finalizeCeoTurn("done", { source: "user", turn_id: "t1" });

    assert.equal(turn.finalized, true);
    assert.equal(turn.textEl.innerHTML.includes("done"), true);
    assert.equal(api.S.ceoSnapshotCache["s1"].messages.length, 2);
});

test("内容签名相同的快照重复推送跳过重建,签名不同才重建", () => {
    const api = setup();
    const messages = [
        { role: "user", content: "q1" },
        { role: "assistant", content: "a1", turn_id: "t0" },
    ];
    const signature = api.buildCeoRenderSignature(messages, null, null);
    api.S.ceoFeedRenderSignature = signature;
    const feed = new FeedStub({ children: [makeMessageEl("m:-:user:0")], scrollHeight: 200, clientHeight: 200 });
    api.U.ceoFeed = feed;

    api.renderCeoSnapshot(messages, null, { sessionId: "s1" });
    assert.equal(feed.resetCount, 0, "同签名快照不应重建");

    const changed = [
        { role: "user", content: "q1" },
        { role: "assistant", content: "a1-changed", turn_id: "t0" },
    ];
    api.renderCeoSnapshot(changed, null, { sessionId: "s1" });
    assert.equal(feed.resetCount, 1, "签名变化必须重建");
    assert.equal(api.S.ceoFeedRenderSignature, api.buildCeoRenderSignature(changed, null, null));
    assert.deepEqual(api.S.ceoFeedRenderedMessageKeys.map(String), ["m:-:user:0", "m:t0:assistant:0"]);
});