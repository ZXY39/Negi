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

const SILENT_TEXT = "这份 CSV 的链接已在 17:49 那轮汇报过了。";
const SILENT_REASON = "已被 task:9771d6c5469d 覆盖";

// 折叠行是用 DOM 节点 + textContent 装配的（本文件没有 HTML 转义助手，拼字符串等于
// 开注入面），而 stub 的 appendChild 只记进 children、不序列化 innerHTML。
// 所以这里按结构断言而不是比字符串。
function findByClass(root, className) {
    const queue = [...(((root && root.children) || []))];
    while (queue.length) {
        const node = queue.shift();
        if (!node) continue;
        if (String(node.className || "").split(/\s+/).includes(className)) return node;
        queue.push(...((node.children || [])));
    }
    return null;
}

function summaryTextOf(textEl) {
    const details = findByClass(textEl, "ceo-silent-turn");
    const summary = details ? findByClass(details, "summary") || (details.children || [])[0] : null;
    return summary ? String(summary.textContent || "") : "";
}

function bodyHtmlOf(textEl) {
    const details = findByClass(textEl, "ceo-silent-turn");
    const body = details ? findByClass(details, "ceo-silent-turn-body") : null;
    return body ? String(body.innerHTML || "") : "";
}

test("live 静默收尾折成一行「已静默」，展开可见原文，阶段轨道照常", () => {
    const api = setup();
    api.S.ceoSnapshotCache["s1"] = { session_id: "s1", messages: [{ role: "user", content: "q1" }] };
    api.S.ceoFeedRenderedMessageKeys = ["m:-:user:0"];
    const turn = makeTurn({ turnId: "t1" });
    api.S.ceoPendingTurns = [turn];
    api.U.ceoFeed = null;

    api.finalizeCeoTurn(SILENT_TEXT, {
        source: "user",
        turn_id: "t1",
        silent_reply: true,
        silent_reason: SILENT_REASON,
        canonical_context: STAGE_TRACE,
    });

    assert.equal(turn.finalized, true);
    assert.equal(turn.textEl.hidden, false, "不再靠隐藏气泡表达静默");
    assert.ok(findByClass(turn.textEl, "ceo-silent-turn"), "折叠行必须是 details");
    assert.equal(summaryTextOf(turn.textEl), `已静默 · ${SILENT_REASON}`, "摘要给出模型填的理由");
    assert.equal(bodyHtmlOf(turn.textEl).includes("汇报过了"), true, "展开区里是原文，不是占位串");
    assert.equal(turn.flowEl.hidden, false, "阶段轨道必须保持可见");
    assert.equal(String(turn.listEl.innerHTML).includes("inspect repository"), true);
});

test("静默回合的正文进缓存行，不落「信息已静默」占位也不回落兜底文案", () => {
    const api = setup();
    api.S.ceoSnapshotCache["s1"] = { session_id: "s1", messages: [{ role: "user", content: "q1" }] };
    api.S.ceoFeedRenderedMessageKeys = ["m:-:user:0"];
    const turn = makeTurn({ turnId: "t1" });
    api.S.ceoPendingTurns = [turn];
    api.U.ceoFeed = null;

    api.finalizeCeoTurn(SILENT_TEXT, {
        source: "user",
        turn_id: "t1",
        silent_reply: true,
        silent_reason: SILENT_REASON,
        canonical_context: STAGE_TRACE,
    });

    const row = api.S.ceoSnapshotCache["s1"].messages.at(-1);
    assert.equal(row.role, "assistant");
    assert.equal(row.content, SILENT_TEXT, "正文必须留在缓存行里，刷新后才能照样展开");
    assert.equal(row.silent_reply, true);
    assert.equal(row.silent_reason, SILENT_REASON);
});

test("reason 是模型写的自由文本，不得作为 HTML 注入", () => {
    const api = setup();
    api.S.ceoSnapshotCache["s1"] = { session_id: "s1", messages: [{ role: "user", content: "q1" }] };
    api.S.ceoFeedRenderedMessageKeys = ["m:-:user:0"];
    const turn = makeTurn({ turnId: "t1" });
    api.S.ceoPendingTurns = [turn];
    api.U.ceoFeed = null;
    const hostile = '<img src=x onerror=alert(1)>';

    api.finalizeCeoTurn("", {
        source: "user",
        turn_id: "t1",
        silent_reply: true,
        silent_reason: hostile,
        canonical_context: STAGE_TRACE,
    });

    assert.equal(summaryTextOf(turn.textEl), `已静默 · ${hostile}`, "原文照 textContent 呈现");
    // 关键：没有任何节点把这段文本当成标签结构
    assert.equal(bodyHtmlOf(turn.textEl), "", "无正文时不渲染展开体");
    assert.equal(findByClass(turn.textEl, "ceo-silent-turn-body"), null);
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

test("历史静默行渲染为带阶段轨道的回合，并展开得出原文", () => {
    const api = setup();
    const feed = new FeedStub({ children: [], scrollHeight: 400, clientHeight: 300 });
    api.U.ceoFeed = feed;
    const pushed = trackPushedTurns(api);

    api.renderPersistedCeoAssistantTurn({
        role: "assistant",
        content: SILENT_TEXT,
        silent_reply: true,
        silent_reason: SILENT_REASON,
        turn_id: "t1",
        canonical_context: STAGE_TRACE,
    });

    assert.equal(feed.children.length, 1);
    assert.equal(String(feed.children[0].className).includes("ceo-turn-message"), true);
    const turn = pushed.find((item) => item && item.textEl) || null;
    assert.ok(turn, "历史静默行必须创建回合元素");
    assert.equal(turn.textEl.hidden, false);
    assert.equal(bodyHtmlOf(turn.textEl).includes("汇报过了"), true);
    assert.equal(turn.flowEl.hidden, false);
    assert.equal(String(turn.listEl.innerHTML).includes("inspect repository"), true);
});

test("历史静默行没有阶段轨道时也要渲染折叠行", () => {
    // 合同变更：旧实现"无轨道 ⇒ 整行不渲染"，因为那时静默行正文被后端抹成空串，
    // 渲染出来必是空气泡。现在正文与理由都下发，折叠行本身就是可展示内容。
    const api = setup();
    const feed = new FeedStub({ children: [], scrollHeight: 400, clientHeight: 300 });
    api.U.ceoFeed = feed;
    const pushed = trackPushedTurns(api);

    api.renderPersistedCeoAssistantTurn({
        role: "assistant",
        content: SILENT_TEXT,
        silent_reply: true,
        turn_id: "t1",
    });

    assert.equal(feed.children.length, 1, "没有轨道也不再吞掉整行");
    const turn = pushed.find((item) => item && item.textEl) || null;
    assert.ok(turn);
    assert.equal(summaryTextOf(turn.textEl), "已静默", "没有理由时只写「已静默」");
});
