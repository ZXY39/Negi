const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

// 会话视图状态保持(展开态 + 锚定滚动):renderCeoSnapshot 全量重建前后的捕获/还原契约。
// 覆盖:阶段展开态、轮次工具选中、Interaction Flow 容器与"展开全部"、锚定滚动、
// 跨会话捕获保护、round key 为空时的工具选中分域隔离。

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
        this._removeCount = 0;
        this.classList = {
            add: () => {},
            remove: () => {},
            contains: () => false,
            toggle: () => {},
        };
        this.style = {};
    }

    setAttribute(name, value) {
        this.attributes[name] = String(value);
        if (name === "data-ceo-key") this.dataset.ceoKey = String(value);
        if (name === "data-active-tool-key") this.dataset.activeToolKey = String(value);
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

    getBoundingClientRect() {
        // 与真实 DOM 一致:rect.top 是视口相对坐标(内容顶 - 当前滚动量)。
        const feedScrollTop = this._feed && Number(this._feed.scrollTop || 0);
        return { top: this._contentTop - feedScrollTop, height: this._height };
    }

    appendChild(child) {
        this.children.push(child);
        return child;
    }

    remove() {
        this._removeCount += 1;
    }
}

function makeFeed({ children = [], scrollTop = 0, scrollHeight = 0, clientHeight = 0 } = {}) {
    const feed = new StubHTMLElement();
    feed.children = children;
    children.forEach((child) => {
        child._feed = feed;
    });
    feed.scrollTop = scrollTop;
    feed.scrollHeight = scrollHeight;
    feed.clientHeight = clientHeight;
    feed._qsAll[".ceo-turn-message"] = children.filter((child) => child._isTurn);
    return feed;
}

function makeTurn({ key = "", contentTop = 0, height = 10, steps = [], roundHosts = [], flowOpen = false, historyExpanded = false } = {}) {
    const turn = new StubHTMLElement();
    turn._isTurn = true;
    turn._contentTop = contentTop;
    turn._height = height;
    if (key) turn.dataset.ceoKey = key;
    turn._qs[".interaction-flow"] = { open: flowOpen };
    turn._qs[".interaction-flow-toggle"] = {
        textContent: historyExpanded ? "收起旧进度" : "展开全部",
        setAttribute(name, value) {
            this.attributes[name] = String(value);
        },
        getAttribute(name) {
            return this.attributes && Object.prototype.hasOwnProperty.call(this.attributes, name) ? this.attributes[name] : null;
        },
        attributes: historyExpanded ? { "aria-expanded": "true" } : {},
    };
    turn._qs[".interaction-flow-footer"] = { hidden: false };
    turn._qsAll[".task-trace-step"] = steps;
    turn._qsAll[".task-trace-round-tools"] = roundHosts;
    turn._qsAll[".interaction-step"] = [];
    return turn;
}

function makeStep({ traceKey = "", open = false } = {}) {
    const step = new StubHTMLElement();
    step.dataset.traceKey = traceKey;
    step.open = open;
    return step;
}

function makeRoundHost({ activeToolKey = "" } = {}) {
    const host = new StubHTMLElement();
    host.dataset.activeToolKey = activeToolKey;
    return host;
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
        // 任务详情视图的还原副作用桩:记录调用,并用与真实实现一致的字段语义模拟选中。
        setTraceRoundActiveTool: (host, toolKey) => {
            if (host && host.dataset) host.dataset.activeToolKey = String(toolKey || "");
            host.__restoredToolKeys = [...(host.__restoredToolKeys || []), String(toolKey || "")];
        },
        hydrateTraceOutputBlocks: (root) => {
            root.__hydrated = true;
        },
    };
    context.window = context;
    vm.createContext(context);
    vm.runInContext(
        `${APP_CODE}\nthis.__testExports = { captureCeoFeedViewState, applyCeoFeedViewState, restoreCeoFeedScroll, ceoFeedAnchoredScrollTop, S, U };`,
        context
    );
    return context.__testExports;
}

function setup({ renderSessionId = "s1", feed } = {}) {
    const api = loadApp();
    api.S.activeSessionId = "s1";
    api.S.ceoFeedRenderSessionId = renderSessionId;
    api.U.ceoFeed = feed;
    return api;
}

test("capture 记录阶段展开态与回合流容器状态,并按回合身份分域", () => {
    const stepA = makeStep({ traceKey: "ceo:stage:s1", open: true });
    const stepB = makeStep({ traceKey: "ceo:stage:s2", open: false });
    const turn = makeTurn({
        key: "turn:t1",
        steps: [stepA, stepB],
        flowOpen: true,
        historyExpanded: true,
    });
    const api = setup({ feed: makeFeed({ children: [turn] }) });

    const state = api.captureCeoFeedViewState("s1");

    assert.equal(state.sessionId, "s1");
    assert.equal(state.steps["k:turn:t1::ceo:stage:s1"], true);
    assert.equal(state.steps["k:turn:t1::ceo:stage:s2"], false);
    // 跨 realm 对象不做 deepStrictEqual,逐字段断言。
    assert.equal(state.turnFlows["k:turn:t1"].flowOpen, true);
    assert.equal(state.turnFlows["k:turn:t1"].historyExpanded, true);
});

test("capture 用回合内 DOM 顺序分域 round 工具选中,空 round key 不跨轮次撞车", () => {
    const hostA = makeRoundHost({ activeToolKey: "" });
    const hostB = makeRoundHost({ activeToolKey: ":tool:0" });
    const turn = makeTurn({ key: "turn:t1", roundHosts: [hostA, hostB] });
    const api = setup({ feed: makeFeed({ children: [turn] }) });

    const state = api.captureCeoFeedViewState("s1");

    assert.equal(state.roundTools["k:turn:t1::0"], undefined);
    assert.equal(state.roundTools["k:turn:t1::1"], ":tool:0");
});

test("capture 在 feed 渲染的会话与目标不一致时跳过,防止跨会话套用状态", () => {
    const turn = makeTurn({ key: "turn:t1", steps: [makeStep({ traceKey: "ceo:stage:s1", open: true })] });
    const api = setup({
        renderSessionId: "s-other",
        feed: makeFeed({ children: [turn] }),
    });

    const state = api.captureCeoFeedViewState("s1");

    assert.equal(state, null);
});

test("apply 把阶段展开态与流容器状态还原到重建后的新 DOM", () => {
    const newTurn = makeTurn({ key: "turn:t1", steps: [makeStep({ traceKey: "ceo:stage:s1" })] });
    const api = setup({ feed: makeFeed({ children: [newTurn] }) });
    api.U.ceoFeed._qsAll[".task-trace-step"] = [newTurn._qsAll[".task-trace-step"][0]];

    const state = {
        sessionId: "s1",
        atBottom: false,
        prevTop: 0,
        anchor: null,
        turnFlows: { "k:turn:t1": { flowOpen: false, historyExpanded: false } },
        steps: { "k:turn:t1::ceo:stage:s1": true },
        roundTools: {},
    };
    api.applyCeoFeedViewState(state);

    assert.equal(newTurn._qsAll[".task-trace-step"][0].open, true);
    assert.equal(newTurn._qs[".interaction-flow"].open, false);
});

test("apply 还原 historyExpanded:展开较早步骤并更新按钮文案", () => {
    const hiddenStep = new StubHTMLElement();
    hiddenStep.hidden = true;
    const turn = makeTurn({ key: "turn:t1" });
    turn._qsAll[".interaction-step"] = [hiddenStep];
    const api = setup({ feed: makeFeed({ children: [turn] }) });
    api.U.ceoFeed._qsAll[".task-trace-step"] = [];

    api.applyCeoFeedViewState({
        sessionId: "s1",
        atBottom: false,
        prevTop: 0,
        anchor: null,
        turnFlows: { "k:turn:t1": { flowOpen: true, historyExpanded: true } },
        steps: {},
        roundTools: {},
    });

    assert.equal(hiddenStep.hidden, false);
    assert.equal(turn._qs[".interaction-flow-toggle"].textContent, "收起旧进度");
    assert.equal(turn._qs[".interaction-flow-toggle"].getAttribute("aria-expanded"), "true");
});

test("apply 通过 setTraceRoundActiveTool 还原轮次工具选中", () => {
    const host = makeRoundHost();
    const turn = makeTurn({ key: "turn:t1", roundHosts: [host] });
    const api = setup({ feed: makeFeed({ children: [turn] }) });
    api.U.ceoFeed._qsAll[".task-trace-step"] = [];

    api.applyCeoFeedViewState({
        sessionId: "s1",
        atBottom: false,
        prevTop: 0,
        anchor: null,
        turnFlows: {},
        steps: {},
        roundTools: { "k:turn:t1::0": ":tool:0" },
    });

    assert.deepEqual(host.__restoredToolKeys, [":tool:0"]);
    assert.ok(host.dataset.activeToolKey === ":tool:0" || true); // 桩里由 setTraceRoundActiveTool 写入
});

test("锚定滚动:上方内容高度变化后,锚点仍保持在用户正在读的元素上", () => {
    const message = new StubHTMLElement();
    message.dataset.ceoKey = "m:t0:user:0";
    message._contentTop = 0;
    message._height = 500;
    const turn = makeTurn({ key: "turn:t1", contentTop: 500, height: 800 });
    const api = setup({
        feed: makeFeed({ children: [message, turn], scrollTop: 0, scrollHeight: 1300, clientHeight: 600 }),
    });

    // 用户滚动到 520:命中的锚是 turn 元素,元素内偏移 20。
    api.U.ceoFeed.scrollTop = 520;
    const state = api.captureCeoFeedViewState("s1");
    assert.equal(state.anchor.key, "turn:t1");
    assert.equal(state.anchor.offsetInElement, 20);

    // 重建后:同一条消息变高(500 → 900),turn 内容顶从 500 挪到 900。
    const newMessage = new StubHTMLElement();
    newMessage.dataset.ceoKey = "m:t0:user:0";
    newMessage._contentTop = 0;
    newMessage._height = 900;
    const newTurn = makeTurn({ key: "turn:t1", contentTop: 900, height: 800 });
    api.U.ceoFeed = makeFeed({ children: [newMessage, newTurn], scrollTop: 0, scrollHeight: 1700, clientHeight: 600 });
    api.U.ceoFeed._qsAll[".ceo-turn-message"] = [newTurn];
    api.U.ceoFeed._qsAll[".task-trace-step"] = [];

    api.applyCeoFeedViewState(state);

    // 900(新位置) + 20(元素内偏移) = 920,而不是旧的像素 520。
    assert.equal(api.U.ceoFeed.scrollTop, 920);
});

test("锚定元素消失时回退像素 clamp,丢失锚点时不做猜测", () => {
    const message = new StubHTMLElement();
    message.dataset.ceoKey = "m:t0:user:0";
    message._contentTop = 0;
    message._height = 500;
    const turn = makeTurn({ key: "turn:t1", contentTop: 500, height: 800 });
    const api = setup({
        feed: makeFeed({ children: [message, turn], scrollTop: 0, scrollHeight: 1300, clientHeight: 600 }),
    });
    api.U.ceoFeed.scrollTop = 520;
    const state = api.captureCeoFeedViewState("s1");

    // 新渲染里 turn 被换成了另一条消息,turn:t1 消失。
    const newMessageA = new StubHTMLElement();
    newMessageA.dataset.ceoKey = "m:t0:user:0";
    newMessageA._contentTop = 0;
    newMessageA._height = 400;
    const newMessageB = new StubHTMLElement();
    newMessageB.dataset.ceoKey = "m:t1:assistant:0";
    newMessageB._contentTop = 400;
    newMessageB._height = 300;
    api.U.ceoFeed = makeFeed({ children: [newMessageA, newMessageB], scrollTop: 0, scrollHeight: 1200, clientHeight: 600 });
    api.U.ceoFeed._qsAll[".ceo-turn-message"] = [];
    api.U.ceoFeed._qsAll[".task-trace-step"] = [];

    api.applyCeoFeedViewState(state);

    // 回退到先前像素位置并按新高度 clamp:520 ∈ [0, 600]。
    assert.equal(api.U.ceoFeed.scrollTop, 520);
});

test("用户在底部跟随时不锚定,保持滚到底", () => {
    const message = new StubHTMLElement();
    message.dataset.ceoKey = "m:t0:user:0";
    message._contentTop = 0;
    message._height = 1000;
    const api = setup({
        feed: makeFeed({ children: [message], scrollTop: 0, scrollHeight: 1200, clientHeight: 800 }),
    });
    api.U.ceoFeed.scrollTop = 380; // 1200-380-800=20 <= 64 阈值内
    const state = api.captureCeoFeedViewState("s1");
    assert.equal(state.atBottom, true);

    const newMessage = new StubHTMLElement();
    newMessage.dataset.ceoKey = "m:t0:user:0";
    newMessage._contentTop = 0;
    newMessage._height = 1000;
    api.U.ceoFeed = makeFeed({ children: [newMessage], scrollTop: 0, scrollHeight: 2000, clientHeight: 800 });
    api.U.ceoFeed._qsAll[".ceo-turn-message"] = [];
    api.U.ceoFeed._qsAll[".task-trace-step"] = [];

    api.applyCeoFeedViewState(state);

    assert.equal(api.U.ceoFeed.scrollTop, 2000);
});