const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

// 静默回合（模型输出 [G3KU_SILENT]）的会话框契约：
// - 阶段轨道与工具步骤照常展示，只隐藏回复气泡本身；
// - 不再出现「信息已静默」占位，也不得退化成「已完成。」/「Done.」兜底文案。

const TASK_VIEW_PATH = "g3ku/web/frontend/org_graph_task_view.js";
const APP_PATH = "g3ku/web/frontend/org_graph_app.js";
const TASK_VIEW_CODE = fs.readFileSync(TASK_VIEW_PATH, "utf8");
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

    addEventListener() {}

    removeEventListener() {}

    querySelector(selector) {
        // 回合元素靠 querySelector 从模板里取子节点，为每个选择器缓存一个稳定桩元素。
        if (!this._qs[selector]) this._qs[selector] = new StubHTMLElement();
        return this._qs[selector];
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

class StubHTMLButtonElement extends StubHTMLElement {}
class StubHTMLDetailsElement extends StubHTMLElement {}
class StubHTMLFormElement extends StubHTMLElement {}

const STAGE_TRACE = {
    active_stage_id: "frontdoor-stage-1",
    transition_required: false,
    stages: [
        {
            stage_id: "frontdoor-stage-1",
            stage_goal: "inspect repository",
            status: "completed",
            tool_round_budget: 3,
            rounds: [
                {
                    round_id: "round-1",
                    round_index: 1,
                    tools: [{ tool_name: "filesystem", status: "success", output_text: "ok" }],
                },
            ],
        },
    ],
};

function makeTurn({ turnId = "", source = "user" } = {}) {
    const el = new StubHTMLElement();
    el._isTurn = true;
    if (turnId) el.dataset.ceoKey = `turn:${turnId}`;
    const textEl = new StubHTMLElement();
    textEl.className = "assistant-text pending";
    const flowEl = new StubHTMLElement();
    flowEl.className = "interaction-flow";
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
        turnId: String(turnId || ""),
        source: String(source || "user"),
    };
}

// createPendingCeoTurn 会把回合 push 进 S.ceoPendingTurns，而 finalize 又立刻 splice 掉；
// 记录 push 顺序才能在收尾之后继续断言回合元素本身。
function trackPushedTurns(api) {
    const pushed = [];
    const turns = [];
    turns.push = function tracked(...items) {
        pushed.push(...items);
        return Array.prototype.push.apply(this, items);
    };
    api.S.ceoPendingTurns = turns;
    return pushed;
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
        HTMLDetailsElement: StubHTMLDetailsElement,
        HTMLFormElement: StubHTMLFormElement,
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
        setTraceRoundActiveTool: () => {},
        hydrateTraceOutputBlocks: () => {},
    };
    context.window = context;
    vm.createContext(context);
    vm.runInContext(
        `${TASK_VIEW_CODE}\n${APP_CODE}\nthis.__testExports = { S, U, finalizeCeoTurn, renderPersistedCeoAssistantTurn };`,
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

test("live 静默收尾隐藏回复气泡，但阶段轨道照常展示", () => {
    const api = setup();
    api.S.ceoSnapshotCache["s1"] = { session_id: "s1", messages: [{ role: "user", content: "q1" }] };
    api.S.ceoFeedRenderedMessageKeys = ["m:-:user:0"];
    const turn = makeTurn({ turnId: "t1" });
    api.S.ceoPendingTurns = [turn];
    api.U.ceoFeed = null;

    api.finalizeCeoTurn("", { source: "user", turn_id: "t1", silent_reply: true, canonical_context: STAGE_TRACE });

    assert.equal(turn.finalized, true);
    assert.equal(turn.textEl.hidden, true, "静默回合只隐藏回复气泡本身");
    assert.equal(turn.textEl.innerHTML, "");
    assert.equal(turn.flowEl.hidden, false, "阶段轨道必须保持可见");
    assert.equal(String(turn.listEl.innerHTML).includes("inspect repository"), true);
});

test("静默收尾既不落「信息已静默」占位，也不回落成兜底文案", () => {
    const api = setup();
    api.S.ceoSnapshotCache["s1"] = { session_id: "s1", messages: [{ role: "user", content: "q1" }] };
    api.S.ceoFeedRenderedMessageKeys = ["m:-:user:0"];
    const turn = makeTurn({ turnId: "t1" });
    api.S.ceoPendingTurns = [turn];
    api.U.ceoFeed = null;

    api.finalizeCeoTurn("", { source: "user", turn_id: "t1", silent_reply: true, canonical_context: STAGE_TRACE });

    const row = api.S.ceoSnapshotCache["s1"].messages.at(-1);
    assert.equal(row.role, "assistant");
    assert.equal(row.content, "");
    assert.equal(row.silent_reply, true);
});

test("静默 final 找不到回合元素时不得补一个空 system 气泡", () => {
    const api = setup();
    api.S.ceoSnapshotCache["s1"] = { session_id: "s1", messages: [{ role: "user", content: "q1" }] };
    api.S.ceoFeedRenderedMessageKeys = ["m:-:user:0"];
    api.S.ceoPendingTurns = [];
    const feed = new FeedStub({ children: [], scrollHeight: 200, clientHeight: 200 });
    api.U.ceoFeed = feed;

    api.finalizeCeoTurn("", { source: "user", turn_id: "missing", silent_reply: true });

    assert.equal(feed.children.length, 0);
});

test("历史静默行渲染为带阶段轨道的回合，而不是文本气泡", () => {
    const api = setup();
    const feed = new FeedStub({ children: [], scrollHeight: 400, clientHeight: 300 });
    api.U.ceoFeed = feed;
    const pushed = trackPushedTurns(api);

    api.renderPersistedCeoAssistantTurn({
        role: "assistant",
        content: "",
        silent_reply: true,
        turn_id: "t1",
        canonical_context: STAGE_TRACE,
    });

    assert.equal(feed.children.length, 1);
    assert.equal(String(feed.children[0].className).includes("ceo-turn-message"), true);
    const turn = pushed.find((item) => item && item.textEl) || null;
    assert.ok(turn, "历史静默行必须创建回合元素");
    assert.equal(turn.textEl.hidden, true);
    assert.equal(turn.flowEl.hidden, false);
    assert.equal(String(turn.listEl.innerHTML).includes("inspect repository"), true);
});

test("历史静默行没有阶段轨道时整行不渲染", () => {
    const api = setup();
    const feed = new FeedStub({ children: [], scrollHeight: 400, clientHeight: 300 });
    api.U.ceoFeed = feed;
    trackPushedTurns(api);

    api.renderPersistedCeoAssistantTurn({ role: "assistant", content: "", silent_reply: true, turn_id: "t1" });

    assert.equal(feed.children.length, 0, "没有可展示内容时不得产出空气泡");
});
