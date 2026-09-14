const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

// 气泡悬停元信息契约(token 用量 + 时间):
// - setCeoTurnUsage sticky:后续不带 usage 的调用不得清空历史回合的 usage 行(R1);
// - finalize 写缓存的 assistant 消息始终携带 usage/timestamp(R3);
// - 渲染签名覆盖 per-message usage/timestamp,服务端权威快照必须能触发重建(R2);
// - 用户气泡带 timestamp 时渲染 .msg-meta 发送时间行(R6);
// - 无轨道历史助手消息兜底气泡也带完成时间 + usage 元信息(R5)。

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

function makeMetaTurn({ turnId = "", source = "user" } = {}) {
    const el = new StubHTMLElement();
    el._isTurn = true;
    if (turnId) el.dataset.ceoKey = `turn:${turnId}`;
    const textEl = new StubHTMLElement();
    textEl.className = "assistant-text pending";
    const flowEl = new StubHTMLElement();
    flowEl.open = false;
    flowEl.hidden = true;
    return {
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
        usage: null,
        completedAt: "",
        turnId: String(turnId || ""),
        source: String(source || "user"),
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
        `${APP_CODE}\nthis.__testExports = { S, U, addMsg, finalizeCeoTurn, renderCeoSnapshot, renderPersistedCeoAssistantTurn, buildCeoRenderSignature, buildFinalizedCeoTurnPayload, setCeoTurnUsage, normalizeCeoTurnUsage };`,
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

const USAGE = { input_tokens: 1200, output_tokens: 340, cache_hit_tokens: 800, call_count: 2 };

test("setCeoTurnUsage 渲染 token + 完成时间,且后续空调用不清空(sticky)", () => {
    const api = setup();
    const turn = makeMetaTurn({ turnId: "t1" });

    api.setCeoTurnUsage(turn, USAGE, { completedAt: "2026-09-14T10:00:00" });
    assert.equal(turn.usageEl.hidden, false);
    assert.ok(turn.usageEl.textContent.includes("输入 1.2k"), turn.usageEl.textContent);
    assert.ok(turn.usageEl.textContent.includes("缓存命中 800"), turn.usageEl.textContent);
    assert.ok(turn.usageEl.textContent.includes("输出 340"), turn.usageEl.textContent);
    assert.ok(turn.usageEl.textContent.includes("完成于"), turn.usageEl.textContent);
    assert.ok(!turn.usageEl.textContent.includes("NaN"), turn.usageEl.textContent);

    // R1 回归:不带 usage/completedAt 的后续调用(历史回合收尾)不得清空元信息。
    api.setCeoTurnUsage(turn, null);
    assert.equal(turn.usageEl.hidden, false);
    assert.ok(turn.usageEl.textContent.includes("输入 1.2k"), turn.usageEl.textContent);
    assert.ok(turn.usageEl.textContent.includes("完成于"), turn.usageEl.textContent);
});

test("setCeoTurnUsage 只有完成时间也渲染(usage 缺失时悬停仍有内容)", () => {
    const api = setup();
    const turn = makeMetaTurn({ turnId: "t1" });

    api.setCeoTurnUsage(turn, null, { completedAt: "2026-09-14T10:00:00" });
    assert.equal(turn.usageEl.hidden, false);
    assert.ok(turn.usageEl.textContent.includes("完成于"), turn.usageEl.textContent);
    assert.ok(!turn.usageEl.textContent.includes("输入"), turn.usageEl.textContent);
});

test("历史回合 finalize(meta 不带 usage)不清空 usage 行", () => {
    const api = setup();
    api.S.ceoSnapshotCache["s1"] = { session_id: "s1", messages: [{ role: "user", content: "q1" }] };
    api.S.ceoFeedRenderedMessageKeys = ["m:-:user:0"];
    const turn = makeMetaTurn({ turnId: "t1", source: "history" });
    api.S.ceoPendingTurns = [turn];
    api.U.ceoFeed = null;
    api.setCeoTurnUsage(turn, USAGE, { completedAt: "2026-09-14T10:00:00" });

    api.finalizeCeoTurn("done", { source: "history", timestamp: "2026-09-14T10:00:00" });

    assert.equal(turn.finalized, true);
    assert.equal(turn.usageEl.hidden, false);
    assert.ok(turn.usageEl.textContent.includes("输入 1.2k"), turn.usageEl.textContent);
    assert.ok(turn.usageEl.textContent.includes("完成于"), turn.usageEl.textContent);
});

test("finalize 写缓存的 assistant 消息始终携带 usage 与 timestamp(R3)", () => {
    const api = setup();
    api.S.ceoSnapshotCache["s1"] = { session_id: "s1", messages: [{ role: "user", content: "q1" }] };
    api.S.ceoFeedRenderedMessageKeys = ["m:-:user:0"];
    const turn = makeMetaTurn({ turnId: "t1" });
    api.S.ceoPendingTurns = [turn];
    api.U.ceoFeed = null;

    // 无 user_messages 的就地收尾分支(旧行为此分支缓存不带 usage)。
    api.finalizeCeoTurn("done", { source: "user", turn_id: "t1", usage: USAGE });

    const cached = api.S.ceoSnapshotCache["s1"].messages;
    const assistant = cached[cached.length - 1];
    assert.equal(assistant.role, "assistant");
    // 缓存对象来自 vm 上下文(跨 realm),deepStrictEqual 会比原型,故用 JSON 对比。
    assert.equal(JSON.stringify(assistant.usage), JSON.stringify({
        input_tokens: 1200,
        output_tokens: 340,
        cache_hit_tokens: 800,
        call_count: 2,
    }));
    assert.ok(String(assistant.timestamp || "").trim(), "缓存 assistant 消息必须带完成时间");
});

test("渲染签名覆盖 per-message usage/timestamp(R2)", () => {
    const api = setup();
    const base = [{ role: "assistant", content: "a1", turn_id: "t0" }];
    const withUsage = [{ role: "assistant", content: "a1", turn_id: "t0", usage: USAGE }];
    const withTimestamp = [{ role: "assistant", content: "a1", turn_id: "t0", timestamp: "2026-09-14T10:00:00" }];

    const baseSignature = api.buildCeoRenderSignature(base, null, null);
    assert.notEqual(baseSignature, api.buildCeoRenderSignature(withUsage, null, null));
    assert.notEqual(baseSignature, api.buildCeoRenderSignature(withTimestamp, null, null));
    assert.equal(baseSignature, api.buildCeoRenderSignature(base, null, null));
});

test("用户气泡带 timestamp 渲染 .msg-meta 发送时间行(R6)", () => {
    const api = setup();
    const feed = new FeedStub({ scrollHeight: 400, clientHeight: 300 });
    api.U.ceoFeed = feed;

    api.addMsg("hello", "user", { timestamp: "2026-09-14T09:30:00" });
    const withMeta = feed.children[feed.children.length - 1];
    assert.ok(withMeta.innerHTML.includes("msg-meta"), withMeta.innerHTML);
    assert.ok(withMeta.innerHTML.includes("发送于"), withMeta.innerHTML);
    assert.ok(!withMeta.innerHTML.includes("NaN"), withMeta.innerHTML);

    api.addMsg("no meta", "user", {});
    const withoutMeta = feed.children[feed.children.length - 1];
    assert.ok(!withoutMeta.innerHTML.includes("msg-meta"), withoutMeta.innerHTML);
});

test("无轨道历史助手消息兜底气泡带完成时间与 usage 元信息(R5)", () => {
    const api = setup();
    const feed = new FeedStub({ scrollHeight: 400, clientHeight: 300 });
    api.U.ceoFeed = feed;

    api.renderPersistedCeoAssistantTurn({
        role: "assistant",
        content: "plain answer",
        timestamp: "2026-09-14T10:00:00",
        usage: USAGE,
    });

    const el = feed.children[feed.children.length - 1];
    assert.ok(el.innerHTML.includes("msg-meta"), el.innerHTML);
    assert.ok(el.innerHTML.includes("完成于"), el.innerHTML);
    assert.ok(el.innerHTML.includes("输入 1.2k"), el.innerHTML);
});
