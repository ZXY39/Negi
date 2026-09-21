const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

// inflight/preserved 回合的 user_messages 气泡落位契约：
// - 批次里带原始时间戳的消息按时间插入到已渲染气泡之间，不得一律追加到流末尾
//   （渠道会话实测 9 条跨 10 天的旧提问冒到最新回复下面，读起来像用户重发）；
// - 没有可解析时间戳的实时输入保持追加；
// - 带时间戳的气泡在 DOM 上留 data-ceo-timestamp 供上面的比较使用。

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
            add: () => {},
            remove: () => {},
            contains: () => false,
            toggle: () => false,
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

    addEventListener() {}

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
    }
}

class StubHTMLButtonElement extends StubHTMLElement {}
class StubHTMLInputElement extends StubHTMLElement {}
class StubHTMLTextAreaElement extends StubHTMLElement {}
class StubHTMLSelectElement extends StubHTMLElement {}

function makeFeed() {
    const feed = new StubHTMLElement();
    feed.id = "ceo-feed";
    return feed;
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
        `${APP_CODE}\nthis.__testExports = { S, U, addMsg, addCeoUserMessage, renderCeoSnapshot };`,
        context
    );
    return context.__testExports;
}

function setup() {
    const api = loadApp();
    const feed = makeFeed();
    api.S.activeSessionId = "s1";
    api.S.ceoFeedRenderSessionId = "s1";
    api.S.ceoScrollToLatestOnSnapshot = false;
    api.U.ceoFeed = feed;
    return { api, feed };
}

function bubbleLabels(feed) {
    return feed.children
        .filter((child) => String(child.className || "").split(/\s+/).includes("user"))
        .map((child) => {
            const match = String(child.innerHTML || "").match(/<div class="msg-content[^"]*">([^<]*)</);
            return match ? match[1] : "";
        });
}

function makeChronology(count) {
    const messages = [];
    for (let index = 0; index < count; index += 1) {
        messages.push({
            role: "user",
            content: `m${index}`,
            turn_id: `t${index}`,
            timestamp: `2026-09-20T10:${String(index).padStart(2, "0")}:00`,
        });
    }
    return messages;
}

test("inflight 批次里的旧排队消息按原始时间落位，不追加到流末尾", () => {
    const { api, feed } = setup();
    const messages = makeChronology(30);

    api.renderCeoSnapshot(messages, {
        turn_id: "live",
        status: "running",
        source: "user",
        user_messages: [
            { role: "user", content: "old-a", timestamp: "2026-09-20T10:03:30" },
            { role: "user", content: "old-b", timestamp: "2026-09-21T20:00:00" },
            { role: "user", content: "newest", timestamp: "2026-09-21T20:30:00" },
        ],
    }, { sessionId: "s1" });

    const labels = bubbleLabels(feed);
    // 去重只看最后 24 行，所以这三条都会作为 inflight 气泡被渲染出来（复现前提）。
    assert.ok(labels.includes("old-a") && labels.includes("old-b") && labels.includes("newest"), labels.join("|"));
    // 10:03:30 落在 m3(10:03:00) 与 m4(10:04:00) 之间，而不是整串气泡的最后。
    assert.equal(labels[3], "m3", labels.join("|"));
    assert.equal(labels[4], "old-a", labels.join("|"));
    assert.equal(labels[5], "m4", labels.join("|"));
    assert.deepEqual(labels.slice(-3), ["m29", "old-b", "newest"], labels.join("|"));
});

test("无可解析时间戳的实时输入保持追加到流末尾", () => {
    const { api, feed } = setup();

    api.renderCeoSnapshot(makeChronology(30), {
        turn_id: "live",
        status: "running",
        source: "user",
        user_messages: [{ role: "user", content: "typed-now" }],
    }, { sessionId: "s1" });

    const labels = bubbleLabels(feed);
    assert.equal(labels[labels.length - 1], "typed-now", labels.join("|"));
});

test("addMsg 把 timestamp 写进气泡的 data-ceo-timestamp", () => {
    const { api, feed } = setup();

    api.addMsg("带时间", "user", { timestamp: "2026-09-20T10:00:00" });
    api.addMsg("无时间", "user", {});

    assert.equal(feed.children[0].dataset.ceoTimestamp, "2026-09-20T10:00:00");
    assert.equal(feed.children[1].dataset.ceoTimestamp, undefined);
});
