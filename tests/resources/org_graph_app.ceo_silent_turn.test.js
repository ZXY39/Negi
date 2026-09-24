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
        this._listeners = {};
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

    addEventListener(type, handler) {
        this._listeners = this._listeners || {};
        (this._listeners[type] = this._listeners[type] || []).push(handler);
    }

    removeEventListener() {}

    click() {
        (this._listeners && this._listeners.click ? this._listeners.click : []).forEach((handler) => handler());
    }

    querySelector(selector) {
        // 回合元素靠 querySelector 从模板里取子节点。桩把它建成**真实子节点**并缓存，
        // 这样 parentElement 与结构遍历跟浏览器一致（静默折叠行要找 textEl 的父容器）。
        if (!this._qs[selector]) {
            const child = new StubHTMLElement();
            child.className = String(selector).replace(/^\./, "");
            this.appendChild(child);
            this._qs[selector] = child;
        }
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
        if (child) child.parentElement = this;
        return child;
    }

    insertBefore(child, ref) {
        const next = this.children.filter((item) => item !== child);
        const index = next.indexOf(ref);
        if (index < 0) next.push(child);
        else next.splice(index, 0, child);
        this.children = next;
        if (child && child._feed !== undefined) child._feed = this;
        if (child) child.parentElement = this;
        return child;
    }

    remove() {
        const parentChildren = (this.parentElement && this.parentElement.children)
            || (this._feed && this._feed.children) || null;
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
    // 真实结构是 .message > .msg-content.ceo-turn-content > [assistant-text, usage,
    // interaction-flow, reminder]，静默折叠行与原因行都挂在 content 上，
    // 所以桩必须把 parentElement 连起来（产品代码靠它定位容器）。
    const el = new StubHTMLElement();
    el._isTurn = true;
    el.className = "message system ceo-turn-message";
    if (turnId) el.dataset.ceoKey = `turn:${turnId}`;
    const contentEl = new StubHTMLElement();
    contentEl.className = "msg-content ceo-turn-content";
    el.appendChild(contentEl);
    const textEl = new StubHTMLElement();
    textEl.className = "assistant-text pending";
    contentEl.appendChild(textEl);
    const flowEl = new StubHTMLElement();
    flowEl.className = "interaction-flow";
    flowEl.open = false;
    flowEl.hidden = true;
    contentEl.appendChild(flowEl);
    return {
        el,
        contentEl,
        textEl,
        flowEl,
        listEl: new StubHTMLElement(),
        metaEl: new StubHTMLElement(),
        footerEl: new StubHTMLElement(),
        usageEl: new StubHTMLElement(),
        reminderEl: new StubHTMLElement(),
        silentLineEl: null,
        silentReasonEl: null,
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
const SILENT_AT = "2026-09-23T23:55:11";

// 静默回合的呈现合同（2026-09-24 改版）：整条响应折成一行「静默消息 HH:MM:SS」+ 下箭头，
// 默认折叠；点开露出正文、阶段轨道与工具步骤，并在气泡底部常驻一行静默原因。
// 旧的 <details> 折叠盒（描边框 + summary 标记）已去掉。
// 折叠行与原因行都用 DOM 节点 + textContent 装配（本文件没有 HTML 转义助手，
// 拼进 innerHTML 等于开注入面），而桩的 appendChild 只记 children、不序列化 innerHTML，
// 所以断言走结构遍历而不是字符串比对。
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

function lineOf(turn) {
    return findByClass(turn.el, "ceo-silent-line");
}

function labelOf(turn) {
    const line = lineOf(turn);
    return line && line.children[0] ? String(line.children[0].textContent || "") : "";
}

function reasonOf(turn) {
    const node = findByClass(turn.el, "ceo-silent-reason");
    return node ? String(node.textContent || "") : "";
}

function findAllByClass(root, className) {
    const found = [];
    const queue = [...(((root && root.children) || []))];
    while (queue.length) {
        const node = queue.shift();
        if (!node) continue;
        if (String(node.className || "").split(/\s+/).includes(className)) found.push(node);
        queue.push(...((node.children || [])));
    }
    return found;
}

function silentLineNodes(turn) {
    const line = lineOf(turn);
    return line ? findAllByClass(line.parentElement || turn.el, "ceo-silent-line") : [];
}

function finalizeSilent(api, turn, overrides = {}) {
    api.S.ceoSnapshotCache["s1"] = { session_id: "s1", messages: [{ role: "user", content: "q1" }] };
    api.S.ceoFeedRenderedMessageKeys = ["m:-:user:0"];
    api.S.ceoPendingTurns = [turn];
    api.U.ceoFeed = null;
    api.finalizeCeoTurn(SILENT_TEXT, {
        source: "user",
        turn_id: "t1",
        silent_reply: true,
        silent_reason: SILENT_REASON,
        canonical_context: STAGE_TRACE,
        timestamp: SILENT_AT,
        ...overrides,
    });
}

test("live 静默收尾折成一行「静默消息 + 时间」，正文与阶段轨道照常但默认收起", () => {
    const api = setup();
    const turn = makeTurn({ turnId: "t1" });
    finalizeSilent(api, turn);

    assert.equal(turn.finalized, true);
    const line = lineOf(turn);
    assert.ok(line, "必须有一条折叠行");
    const container = line.parentElement;
    assert.ok(container, "折叠行必须挂在气泡内容容器上");
    assert.equal(container.children[0], line, "折叠行排在容器子节点最前");
    assert.equal(silentLineNodes(turn).length, 1, "重复收尾不得再插一条");
    assert.match(labelOf(turn), /^静默消息 (\d\d[-/]\d\d )?\d{2}:\d{2}:\d{2}$/);
    assert.equal(String(turn.el.className).includes("ceo-silent-message"), true);
    assert.equal(String(turn.el.className).includes("ceo-silent-expanded"), false, "默认折叠");
    assert.equal(line.getAttribute("aria-expanded"), "false");
    assert.equal(turn.textEl.hidden, false, "靠 CSS 折叠，不靠 hidden 属性");
    assert.equal(String(turn.textEl.innerHTML).includes("汇报过了"), true, "正文照常渲染，展开才可见");
    assert.equal(reasonOf(turn), `静默原因：${SILENT_REASON}`, "原因常驻在气泡底部");
    assert.equal(turn.flowEl.hidden, false, "阶段轨道必须保持存在");
    assert.equal(String(turn.listEl.innerHTML).includes("inspect repository"), true);
    assert.equal(findByClass(turn.el, "ceo-silent-turn"), null, "旧的 details 折叠盒已去掉");
});

test("点击下箭头展开，再点收回", () => {
    const api = setup();
    const turn = makeTurn({ turnId: "t1" });
    finalizeSilent(api, turn);
    const line = lineOf(turn);

    line.click();
    assert.equal(String(turn.el.className).includes("ceo-silent-expanded"), true);
    assert.equal(line.getAttribute("aria-expanded"), "true");

    line.click();
    assert.equal(String(turn.el.className).includes("ceo-silent-expanded"), false);
    assert.equal(line.getAttribute("aria-expanded"), "false");
});

test("原因与正文进缓存行，不落占位串也不回落兜底文案", () => {
    const api = setup();
    const turn = makeTurn({ turnId: "t1" });
    finalizeSilent(api, turn);

    const row = api.S.ceoSnapshotCache["s1"].messages.at(-1);
    assert.equal(row.role, "assistant");
    assert.equal(row.content, SILENT_TEXT, "正文必须留在缓存行里，刷新后才能照样展开");
    assert.equal(row.silent_reply, true);
    assert.equal(row.silent_reason, SILENT_REASON);
});

test("reason 是模型写的自由文本，不得作为 HTML 注入", () => {
    const api = setup();
    const turn = makeTurn({ turnId: "t1" });
    const hostile = "<img src=x onerror=alert(1)>";
    finalizeSilent(api, turn, { silent_reason: hostile, text: "" });

    assert.equal(reasonOf(turn), `静默原因：${hostile}`, "原文照 textContent 呈现");
    assert.equal(findByClass(turn.el, "ceo-silent-turn-body"), null);
    assert.equal(String(turn.textEl.innerHTML).includes("onerror"), false);
});

test("没有理由时只留折叠行，不挂空原因节点", () => {
    const api = setup();
    const turn = makeTurn({ turnId: "t1" });
    finalizeSilent(api, turn, { silent_reason: "" });

    assert.ok(lineOf(turn));
    assert.equal(findByClass(turn.el, "ceo-silent-reason"), null);
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

test("历史静默行渲染为带轨道的回合，折叠行取消息自带时间", () => {
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
        timestamp: SILENT_AT,
        canonical_context: STAGE_TRACE,
    });

    assert.equal(feed.children.length, 1);
    const turn = pushed.find((item) => item && item.textEl) || null;
    assert.ok(turn, "历史静默行必须创建回合元素");
    assert.match(labelOf(turn), /^静默消息 (\d\d[-/]\d\d )?\d{2}:\d{2}:\d{2}$/);
    assert.equal(String(turn.textEl.innerHTML).includes("汇报过了"), true);
    assert.equal(reasonOf(turn), `静默原因：${SILENT_REASON}`);
    assert.equal(turn.flowEl.hidden, false);
    assert.equal(String(turn.listEl.innerHTML).includes("inspect repository"), true);
});

test("历史静默行没有阶段轨道时也要渲染折叠行", () => {
    // 合同：旧实现"无轨道 ⇒ 整行不渲染"，因为那时后端把静默行正文抹成空串。
    // 现在正文与理由都下发，折叠行本身就是可展示内容。
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
    assert.equal(String(labelOf(turn)).startsWith("静默消息"), true, "没有时间时也只写「静默消息」");
});
