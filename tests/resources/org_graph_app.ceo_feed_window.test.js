const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

// 首屏窗口化渲染契约：尾部窗口、向上分页（滚动补偿）、锚点扩展、跨会话守卫。
// 渲染器与深 DOM 依赖全部替换为轻量 stub，专测窗口数学与 key 对齐。

const APP_PATH = "g3ku/web/frontend/org_graph_app.js";
const APP_CODE = fs.readFileSync(APP_PATH, "utf8");
const TASK_VIEW_PATH = "g3ku/web/frontend/org_graph_task_view.js";
const TASK_VIEW_CODE = fs.readFileSync(TASK_VIEW_PATH, "utf8");

class StubNode {
    constructor() {
        this.tagName = "DIV";
        this.className = "";
        this.children = [];
        this.dataset = {};
        this.attributes = {};
        this._height = 100;
        this._parent = null;
        this._innerHTML = "";
        this.style = {};
        this.hidden = false;
        this.classList = { add: () => {}, remove: () => {}, contains: () => false, toggle: () => {} };
    }
    appendChild(child) {
        if (child._parent) child._parent._detach(child);
        child._parent = this;
        this.children.push(child);
        return child;
    }
    insertBefore(child, ref) {
        if (!ref) return this.appendChild(child);
        if (child._parent) child._parent._detach(child);
        const index = this.children.indexOf(ref);
        this.children.splice(index < 0 ? this.children.length : index, 0, child);
        child._parent = this;
        return child;
    }
    _detach(child) {
        const index = this.children.indexOf(child);
        if (index >= 0) this.children.splice(index, 1);
        child._parent = null;
    }
    get firstChild() { return this.children[0] || null; }
    get lastChild() { return this.children[this.children.length - 1] || null; }
    get lastElementChild() { return this.children[this.children.length - 1] || null; }
    get firstElementChild() { return this.children[0] || null; }
    set innerHTML(value) {
        this._innerHTML = String(value);
        if (value === "") this.children.forEach((child) => { child._parent = null; });
        this.children = [];
    }
    get innerHTML() { return this._innerHTML; }
    setAttribute(name, value) {
        this.attributes[name] = String(value);
        if (name === "data-ceo-key") this.dataset.ceoKey = String(value);
    }
    getAttribute(name) {
        return Object.prototype.hasOwnProperty.call(this.attributes, name) ? this.attributes[name] : null;
    }
    querySelector() { return null; }
    querySelectorAll() { return []; }
    getBoundingClientRect() { return { top: 0, height: this._height }; }
    remove() { if (this._parent) this._parent._detach(this); }
}

class StubFeed extends StubNode {
    constructor() {
        super();
        this.scrollTop = 0;
        this.clientHeight = 600;
    }
    get scrollHeight() {
        return this.children.reduce((sum, child) => sum + (child._height || 0), 0);
    }
}

function makeMessage(index, role = "user") {
    return { role, content: `msg ${index}`, turn_id: `t${index}`, timestamp: "" };
}

function messageKey(turnId, role, occ) {
    return `m:${turnId}:${role}:${occ}`;
}

function loadApp() {
    const feed = new StubFeed();
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
            createElement: () => new StubNode(),
            addEventListener: () => {},
        },
        window: {},
        Element: StubNode,
        HTMLElement: StubNode,
        URLSearchParams,
        URL,
        AbortController,
        fetch: async () => ({ ok: true, json: async () => ({}) }),
        lucide: { createIcons() {} },
        marked: { parse: (value) => String(value) },
        DOMPurify: { sanitize: (value) => String(value) },
        performance: { now: () => Date.now() },
        requestAnimationFrame: (callback) => { callback(); return 1; },
        cancelAnimationFrame: () => {},
        WebSocket: function WebSocket() {},
        addEventListener() {},
        removeEventListener() {},
    };
    context.window = context;
    vm.createContext(context);
    vm.runInContext(
        `${TASK_VIEW_CODE}\n${APP_CODE}\nthis.__testExports = { renderCeoSnapshot, loadOlderCeoFeedMessages, ceoFeedAnchorRendered, S, U };`,
        context
    );
    const api = context.__testExports;
    api.U.ceoFeed = feed;
    api.U.ceoScrollToLatestBtn = { hidden: true };
    // 渲染器与深依赖替换为轻量 stub（APP_CODE 顶层函数声明即全局属性，可覆写）：
    // 追加一个高 100 的节点并带上 turn_id，key 打标仍走真实的
    // renderCeoSnapshotMessageRange 路径。
    context.StubNodeClass = StubNode;
    vm.runInContext(`
        addCeoUserMessage = (text, opts = {}) => {
            const el = new StubNodeClass();
            const match = String(opts.turnId || text).match(/t?(\\d+)/);
            el._turnId = match ? "t" + match[1] : "";
            ceoFeedAppendHost().appendChild(el);
            return el;
        };
        addMsg = (text, role, opts = {}) => {
            const el = new StubNodeClass();
            const match = String(text).match(/msg (\\d+)/);
            el._turnId = match ? "t" + match[1] : "";
            ceoFeedAppendHost().appendChild(el);
            return el;
        };
        renderPersistedCeoAssistantTurn = (item = {}) => {
            const el = new StubNodeClass();
            el._turnId = String(item.turn_id || "");
            ceoFeedAppendHost().appendChild(el);
            return el;
        };
        appendCeoCompressionDivider = () => { const el = new StubNodeClass(); ceoFeedAppendHost().appendChild(el); return el; };
        restoreCeoInflightTurn = () => {};
        consumeRepresentedRuntimeSentCeoFollowUps = () => {};
        setCeoSessionSnapshotCache = () => {};
        applyCeoFeedViewState = () => {};
        hideCeoContextLoadNotice = () => {};
    `, context, { filename: "window-stubs.js" });
    return { api, feed, context };
}

test("首屏只渲染尾部窗口，keys 与全局计数对齐", () => {
    const { api, feed } = loadApp();
    api.S.activeSessionId = "s1";
    const messages = Array.from({ length: 250 }, (_, i) => makeMessage(i));
    api.renderCeoSnapshot(messages, null, { sessionId: "s1" });

    assert.equal(feed.children.length, 80);
    assert.equal(api.S.ceoFeedWindowSource.start, 170);
    assert.equal(api.S.ceoFeedRenderedMessageKeys.length, 80);
    // 窗口首条的 key 必须是全局 occ（第 170 条 user 消息 occ=170），不是窗口内 0。
    assert.equal(feed.children[0].dataset.ceoKey, messageKey("t170", "user", 0), "每条消息 turn_id 唯一 → occ=0");
});

test("向上分页补渲染一片并做滚动补偿", () => {
    const { api, feed } = loadApp();
    api.S.activeSessionId = "s1";
    const messages = Array.from({ length: 250 }, (_, i) => makeMessage(i));
    api.renderCeoSnapshot(messages, null, { sessionId: "s1" });

    feed.scrollTop = 300;
    const before = feed.children.length;
    const loaded = api.loadOlderCeoFeedMessages();
    assert.equal(loaded, true);
    assert.equal(feed.children.length, before + 80);
    assert.equal(api.S.ceoFeedWindowSource.start, 90);
    // 每个 stub 节点高 100，补 80 条 → 高度增加 8000，视口补偿保持阅读位置。
    assert.equal(feed.scrollTop, 300 + 8000);
    assert.equal(api.S.ceoFeedRenderedMessageKeys.length, 160);
});

test("锚点扩展按 upToKey 连续补页直至命中", () => {
    const { api, feed } = loadApp();
    api.S.activeSessionId = "s1";
    const messages = Array.from({ length: 500 }, (_, i) => makeMessage(i));
    api.renderCeoSnapshot(messages, null, { sessionId: "s1" });

    const targetKey = messageKey("t300", "user", 0);
    const found = api.ceoFeedAnchorRendered(targetKey);
    assert.equal(found, false);
    api.loadOlderCeoFeedMessages({ upToKey: targetKey, maxPages: 4 });
    assert.equal(api.ceoFeedAnchorRendered(targetKey), true);
    // 尾窗 80 + 第 1 页 [340,420) 未命中 + 第 2 页 [260,340) 命中即停 → 240 条。
    assert.equal(feed.children.length, 240);
    assert.equal(api.S.ceoFeedWindowSource.start, 260);
});

test("跨会话 source 守卫：不匹配时不补渲染", () => {
    const { api, feed } = loadApp();
    api.S.activeSessionId = "s1";
    const messages = Array.from({ length: 250 }, (_, i) => makeMessage(i));
    api.renderCeoSnapshot(messages, null, { sessionId: "s1" });
    api.S.ceoFeedRenderSessionId = "other";
    const before = feed.children.length;
    assert.equal(api.loadOlderCeoFeedMessages(), false);
    assert.equal(feed.children.length, before);
});

test("已到顶：start=0 时分页为 no-op", () => {
    const { api, feed } = loadApp();
    api.S.activeSessionId = "s1";
    const messages = Array.from({ length: 60 }, (_, i) => makeMessage(i));
    api.renderCeoSnapshot(messages, null, { sessionId: "s1" });
    assert.equal(feed.children.length, 60);
    assert.equal(api.S.ceoFeedWindowSource.start, 0);
    assert.equal(api.loadOlderCeoFeedMessages(), false);
});
