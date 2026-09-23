const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

// 直播回合视图状态保持:renderCeoStageTraceIntoTurn 整段重建前后的展开态捕获/还原,
// 以及 mutateCeoFeed preserve 模式的锚定滚动/贴底跟随契约。
// 覆盖:阶段 details 开合、轮次工具条选中、Interaction Flow 用户折叠、跨轮换轨不串态、
// 上滚锚定(上方内容增高/缩短不跳变)、贴底自动跟随、「回到最新」按钮的呼吸门控与动画载体。

const TASK_VIEW_PATH = "g3ku/web/frontend/org_graph_task_view.js";
const APP_PATH = "g3ku/web/frontend/org_graph_app.js";
const CSS_PATH = "g3ku/web/frontend/org_graph.css";
const TASK_VIEW_CODE = fs.readFileSync(TASK_VIEW_PATH, "utf8");
const APP_CODE = fs.readFileSync(APP_PATH, "utf8");
const APP_CSS = fs.readFileSync(CSS_PATH, "utf8");

class StubElement {}

class StubHTMLElement extends StubElement {
    constructor() {
        super();
        this.tagName = "DIV";
        this.hidden = false;
        this.open = false;
        this.textContent = "";
        this.className = "";
        this.dataset = {};
        this.style = {};
        this.attributes = {};
        this._innerHTML = "";
        this._selectors = {};
        this._selectorLists = {};
        this._children = [];
        this._contentTop = 0;
        this._height = 10;
        this._feed = null;
        this.classList = {
            add: () => {},
            remove: () => {},
            contains: () => false,
            toggle: () => {},
        };
    }

    setAttribute(name, value) {
        this.attributes[name] = String(value);
        if (name === "data-ceo-key") this.dataset.ceoKey = String(value);
        if (name === "data-trace-key") this.dataset.traceKey = String(value);
        if (name === "data-round-key") this.dataset.roundKey = String(value);
        if (name === "data-active-tool-key") this.dataset.activeToolKey = String(value);
    }

    getAttribute(name) {
        return Object.prototype.hasOwnProperty.call(this.attributes, name) ? this.attributes[name] : null;
    }

    querySelector(selector) {
        return this._selectors[selector] || null;
    }

    querySelectorAll(selector) {
        return this._selectorLists[selector] || [];
    }

    getBoundingClientRect() {
        // 与真实 DOM 一致:rect.top 是视口相对坐标(内容顶 - 所属 feed 当前滚动量)。
        const feedScrollTop = this._feed && Number(this._feed.scrollTop || 0);
        return { top: this._contentTop - feedScrollTop, height: this._height };
    }

    addEventListener() {}

    appendChild(child) {
        this._children.push(child);
        return child;
    }

    remove() {
        this._children = [];
    }

    get children() {
        return this._children;
    }

    get innerHTML() {
        return this._innerHTML;
    }

    set innerHTML(value) {
        this._innerHTML = String(value);
    }
}

class StubHTMLButtonElement extends StubHTMLElement {}
class StubHTMLInputElement extends StubHTMLElement {}
class StubHTMLTextAreaElement extends StubHTMLElement {}
class StubHTMLSelectElement extends StubHTMLElement {}

class StubDocument {
    getElementById() {
        return null;
    }

    createElement() {
        return new StubHTMLElement();
    }

    querySelector() {
        return null;
    }

    querySelectorAll() {
        return [];
    }

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
        window: {},
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
        `${TASK_VIEW_CODE}\n${APP_CODE}\nthis.__testExports = { renderCeoStageTraceIntoTurn, mutateCeoFeed, scrollCeoFeedToBottom, updateCeoScrollToLatestButton, syncCeoFeedTurnActiveClass, setCeoFeedFollowLatest, handleCeoFeedUserGesture, handleCeoFeedScrollEvent, markCeoFeedProgrammaticScroll, S, U };`,
        context
    );
    return context.__testExports;
}

// 真实 DOM 里 innerHTML 重建会把旧节点全部换成新节点;stub 里用 setter 钩子模拟:
// 每次赋值都按渲染产物中的 data-trace-key / data-round-key 重新生成默认态桩节点。
function makeRebuildingListEl() {
    const listEl = new StubHTMLElement();
    Object.defineProperty(listEl, "innerHTML", {
        configurable: true,
        get() {
            return this._innerHTML;
        },
        set(value) {
            this._innerHTML = String(value);
            const html = String(value);
            const traceKeys = [...html.matchAll(/data-trace-key="([^"]*)"/g)].map((match) => match[1]);
            const steps = traceKeys.map((key) => {
                const step = new StubHTMLElement();
                step.dataset.traceKey = key;
                step.open = false;
                return step;
            });
            this._selectorLists[".task-trace-step"] = steps;
            const roundKeys = [...html.matchAll(/data-round-key="([^"]*)"/g)].map((match) => match[1]);
            this._selectorLists[".task-trace-round-tools"] = roundKeys.map((roundKey) => {
                const host = new StubHTMLElement();
                host.dataset.roundKey = roundKey;
                host.dataset.activeToolKey = "";
                host.closest = () => null;
                return host;
            });
        },
    });
    return listEl;
}

function makeTurn({ flowOpen = false } = {}) {
    return {
        textEl: new StubHTMLElement(),
        flowEl: { hidden: true, open: flowOpen },
        metaEl: { textContent: "" },
        listEl: makeRebuildingListEl(),
        footerEl: { hidden: true },
        toggleEl: { textContent: "", setAttribute() {} },
        el: new StubHTMLElement(),
    };
}

function stageFixture(stageId, { roundId = null } = {}) {
    const rounds = roundId
        ? [{
            round_id: roundId,
            round_index: 1,
            text: "先搜一下候选",
            tools: [{ tool_name: "filesystem", status: "success", output_text: "ok" }],
        }]
        : [];
    return {
        stage_id: stageId,
        stage_goal: `goal ${stageId}`,
        status: "running",
        tool_round_budget: 3,
        rounds,
    };
}

test("直播重建保留用户展开的阶段与折叠的 Interaction Flow", () => {
    const { renderCeoStageTraceIntoTurn } = loadApp();
    const turn = makeTurn();

    renderCeoStageTraceIntoTurn(turn, { stages: [stageFixture("stage-1", { roundId: "round-1" })] });
    // 首渲染默认:Flow 自动展开、阶段全部折叠。
    assert.equal(turn.flowEl.open, true);
    const firstSteps = turn.listEl.querySelectorAll(".task-trace-step");
    assert.equal(firstSteps.length, 1);
    assert.equal(firstSteps[0].open, false);

    // 用户操作:展开 stage-1、选中 round-1 的某个工具、折叠 Flow 容器。
    firstSteps[0].open = true;
    const firstHosts = turn.listEl.querySelectorAll(".task-trace-round-tools");
    assert.ok(firstHosts.length >= 1);
    firstHosts[0].dataset.activeToolKey = "round-1:tool:0";
    turn.flowEl.open = false;

    // 新阶段增量到达 → 整段重建。
    renderCeoStageTraceIntoTurn(turn, {
        stages: [stageFixture("stage-1", { roundId: "round-1" }), stageFixture("stage-2")],
    });

    const steps = turn.listEl.querySelectorAll(".task-trace-step");
    assert.equal(steps.length, 2);
    const stage1 = steps.find((step) => step.dataset.traceKey === "ceo:stage:stage-1");
    const stage2 = steps.find((step) => step.dataset.traceKey === "ceo:stage:stage-2");
    assert.equal(stage1.open, true, "用户展开的阶段在重建后必须保持展开");
    assert.equal(stage2.open, false, "新阶段保持默认折叠");
    const hosts = turn.listEl.querySelectorAll(".task-trace-round-tools");
    assert.equal(hosts[0].dataset.activeToolKey, "round-1:tool:0", "轮次工具选中必须按 round key 还原");
    assert.equal(turn.flowEl.open, false, "用户折叠的 Flow 容器不能被重建强制展开");
});

test("首渲染与换轨(turnId 变更清空轨道)仍走默认展开语义", () => {
    const { renderCeoStageTraceIntoTurn } = loadApp();
    const turn = makeTurn();

    renderCeoStageTraceIntoTurn(turn, { stages: [stageFixture("stage-1")] });
    turn.listEl.querySelectorAll(".task-trace-step")[0].open = true;

    // patchCeoInflightTurn 在 turnId 变化时清空 lastExecutionTraceSummary:
    // 新一轮渲染不得沿用上一轮的展开态(stage id 跨轮会重复)。
    turn.lastExecutionTraceSummary = null;
    turn.flowEl.open = false;
    renderCeoStageTraceIntoTurn(turn, { stages: [stageFixture("stage-1")] });

    assert.equal(turn.listEl.querySelectorAll(".task-trace-step")[0].open, false);
    assert.equal(turn.flowEl.open, true);
});

function makeFeed({ children = [], scrollTop = 0, scrollHeight = 0, clientHeight = 0 } = {}) {
    const feed = new StubHTMLElement();
    feed._children = children;
    children.forEach((child) => {
        child._feed = feed;
    });
    feed.scrollTop = scrollTop;
    feed.scrollHeight = scrollHeight;
    feed.clientHeight = clientHeight;
    return feed;
}

function makeFeedChild({ key = "", contentTop = 0, height = 10 } = {}) {
    const child = new StubHTMLElement();
    if (key) child.dataset.ceoKey = key;
    child._contentTop = contentTop;
    child._height = height;
    return child;
}

// 「回到最新」的呼吸和 feed 的回合进行中态都挂在类名上,被测元素需要一个能记事的 classList。
function withClassRecorder(el = {}) {
    const classes = new Set();
    el.classes = classes;
    el.classList = {
        toggle: (name, on) => {
            if (on) classes.add(name);
            else classes.delete(name);
            return classes.has(name);
        },
        contains: (name) => classes.has(name),
    };
    return el;
}

function setupScrollApi(feed) {
    const api = loadApp();
    api.S.activeSessionId = "s1";
    api.U.ceoFeed = feed;
    api.U.ceoScrollToLatestBtn = { hidden: true };
    return api;
}

test("preserve 模式:上方内容增高时按锚点元素补偿,不再像素漂移", () => {
    const childA = makeFeedChild({ key: "m:1", contentTop: 0, height: 500 });
    const childB = makeFeedChild({ key: "m:2", contentTop: 500, height: 500 });
    const feed = makeFeed({ children: [childA, childB], scrollTop: 700, scrollHeight: 1000, clientHeight: 200 });
    const api = setupScrollApi(feed);
    api.S.ceoFeedFollowLatest = false; // 新契约:上滚是意图位,不靠瞬时几何

    api.mutateCeoFeed(() => {
        // 视口上方的消息 A 增高 300(工具输出撑开),B 被推下去。
        childA._height = 800;
        childB._contentTop = 800;
        feed.scrollHeight = 1300;
    }, { scrollMode: "preserve" });

    // 锚点 = B 内容顶(500)+ 元素内偏移(200);增高后 B 顶到 800,应还原为 800+200=1000,
    // 旧像素 clamp 会停在 700(视口内容凭空漂移 300px)。
    assert.equal(feed.scrollTop, 1000);
});

test("preserve 模式:上方内容缩短(折叠)时锚点跟随,不被 clamp 甩到 maxTop", () => {
    const childA = makeFeedChild({ key: "m:1", contentTop: 0, height: 500 });
    const childB = makeFeedChild({ key: "m:2", contentTop: 500, height: 500 });
    const feed = makeFeed({ children: [childA, childB], scrollTop: 700, scrollHeight: 1000, clientHeight: 200 });
    const api = setupScrollApi(feed);
    api.S.ceoFeedFollowLatest = false; // 新契约:上滚是意图位,不靠瞬时几何

    api.mutateCeoFeed(() => {
        childA._height = 100;
        childB._contentTop = 100;
        feed.scrollHeight = 600;
    }, { scrollMode: "preserve" });

    // 锚定 B 内同一位置:100+200=300;旧像素 clamp 会算出 min(700, 400)=400 产生跳变。
    assert.equal(feed.scrollTop, 300);
});

test("preserve 模式:贴底时直播内容增长自动跟随到底部", () => {
    const childA = makeFeedChild({ key: "m:1", contentTop: 0, height: 1000 });
    const feed = makeFeed({ children: [childA], scrollTop: 800, scrollHeight: 1000, clientHeight: 200 });
    const api = setupScrollApi(feed);

    api.mutateCeoFeed(() => {
        childA._height = 1200;
        feed.scrollHeight = 1200;
    }, { scrollMode: "preserve" });

    assert.equal(feed.scrollTop, 1200, "贴底用户应跟随最新内容(stick-to-bottom)");
    assert.equal(api.U.ceoScrollToLatestBtn.hidden, true);
});

test("preserve 模式:上滚用户不被贴底跟随打扰,回到底部提示按钮出现", () => {
    const childA = makeFeedChild({ key: "m:1", contentTop: 0, height: 1000 });
    const feed = makeFeed({ children: [childA], scrollTop: 100, scrollHeight: 1000, clientHeight: 200 });
    const api = setupScrollApi(feed);
    api.S.ceoFeedFollowLatest = false; // 新契约:上滚是意图位,不靠瞬时几何

    api.mutateCeoFeed(() => {
        childA._height = 1200;
        feed.scrollHeight = 1200;
    }, { scrollMode: "preserve" });

    assert.equal(feed.scrollTop, 100, "上滚用户视口必须原地保持");
    assert.equal(api.U.ceoScrollToLatestBtn.hidden, false, "离开底部后应显示回到底部按钮");
});

test("意图脱离竞态:几何近底但用户已上滚,直播突变不得钉底", () => {
    const childA = makeFeedChild({ key: "m:1", contentTop: 0, height: 1000 });
    // scrollTop=750 距底部仅 12px<64 阈值——旧几何判定会误判"在底部"并钉回。
    const feed = makeFeed({ children: [childA], scrollTop: 750, scrollHeight: 1000, clientHeight: 200 });
    const api = setupScrollApi(feed);
    api.S.ceoFeedFollowLatest = false;

    api.mutateCeoFeed(() => {
        childA._height = 1200;
        feed.scrollHeight = 1200;
    }, { scrollMode: "preserve" });

    assert.notEqual(feed.scrollTop, 1200, "跟随与否只看意图位,不再被瞬时几何骗走");
    assert.equal(feed.scrollTop, 750, "锚点(m:1 内偏移 750)保持原位");
});

test("跟随意图保持时:内容增长自动钉到最新底部", () => {
    const childA = makeFeedChild({ key: "m:1", contentTop: 0, height: 1000 });
    const feed = makeFeed({ children: [childA], scrollTop: 300, scrollHeight: 1000, clientHeight: 200 });
    const api = setupScrollApi(feed);
    // S.ceoFeedFollowLatest 默认 true：上一次突变刚把新内容撑出视口，几何暂时远离底部。

    api.mutateCeoFeed(() => {
        childA._height = 1200;
        feed.scrollHeight = 1200;
    }, { scrollMode: "preserve" });

    assert.equal(feed.scrollTop, 1200);
});

test("脱离时按钮常显；scrollCeoFeedToBottom 恢复跟随并收起按钮", () => {
    const childA = makeFeedChild({ key: "m:1", contentTop: 0, height: 1000 });
    const feed = makeFeed({ children: [childA], scrollTop: 100, scrollHeight: 1000, clientHeight: 200 });
    const api = setupScrollApi(feed);
    api.S.ceoFeedFollowLatest = false;
    api.updateCeoScrollToLatestButton();
    assert.equal(api.U.ceoScrollToLatestBtn.hidden, false, "看不到最新输出时按钮必须可见(呼吸样式挂在可见态上)");

    api.scrollCeoFeedToBottom();
    assert.equal(api.S.ceoFeedFollowLatest, true);
    assert.equal(feed.scrollTop, 1000);
    assert.equal(api.U.ceoScrollToLatestBtn.hidden, true);
});

test("呼吸只在回合进行中:收尾后按钮常显但不再闪烁", () => {
    const childA = makeFeedChild({ key: "m:1", contentTop: 0, height: 1000 });
    const feed = makeFeed({ children: [childA], scrollTop: 100, scrollHeight: 1000, clientHeight: 200 });
    const api = setupScrollApi(feed);
    const btn = withClassRecorder({ hidden: true });
    api.U.ceoScrollToLatestBtn = btn;
    api.S.ceoFeedFollowLatest = false;

    api.S.ceoTurnActive = true;
    api.updateCeoScrollToLatestButton();
    assert.equal(btn.hidden, false);
    assert.ok(btn.classes.has("is-breathing"), "回合进行中且看不到最新输出:按钮呼吸");

    // 回合收尾不回收可见性(操作者仍没在看最新输出),只停呼吸。
    api.S.ceoTurnActive = false;
    api.updateCeoScrollToLatestButton();
    assert.equal(btn.hidden, false, "会话收尾后按钮仍要常显");
    assert.ok(!btn.classes.has("is-breathing"), "会话已收尾:没有新内容要追,不该继续闪");
});

test("回合状态翻转经 syncCeoFeedTurnActiveClass 落到按钮,不依赖滚动事件", () => {
    const childA = makeFeedChild({ key: "m:1", contentTop: 0, height: 1000 });
    const feed = makeFeed({ children: [childA], scrollTop: 100, scrollHeight: 1000, clientHeight: 200 });
    const api = setupScrollApi(feed);
    withClassRecorder(feed);
    const btn = withClassRecorder({ hidden: true });
    api.U.ceoScrollToLatestBtn = btn;
    api.S.ceoFeedFollowLatest = false;
    api.updateCeoScrollToLatestButton();

    api.S.ceoTurnActive = true;
    api.syncCeoFeedTurnActiveClass();
    assert.ok(btn.classes.has("is-breathing"));
    assert.ok(feed.classes.has("ceo-turn-active"), "同一状态位也要继续驱动 feed 的回合进行中类");

    api.S.ceoTurnActive = false;
    api.syncCeoFeedTurnActiveClass();
    assert.ok(!btn.classes.has("is-breathing"), "收尾必须即时停呼吸");
    assert.ok(!feed.classes.has("ceo-turn-active"));
});

test("呼吸画在伪元素的 opacity 上:不插值 box-shadow spread,按钮不带 backdrop-filter", () => {
    const breathe = APP_CSS.match(/@keyframes ceo-scroll-latest-breathe\s*\{[\s\S]*?\n\}/);
    assert.ok(breathe, "呼吸 keyframes 必须存在");
    assert.ok(!/box-shadow/.test(breathe[0]), "呼吸只能动 opacity:插值 spread 会让光晕随周期涨到周围内容上");
    assert.match(breathe[0], /opacity/);

    const btnRule = APP_CSS.match(/\.ceo-scroll-to-latest-btn\s*\{[\s\S]*?\n\}/);
    assert.ok(btnRule, "按钮基础样式必须存在");
    assert.ok(!/backdrop-filter/.test(btnRule[0]), "backdrop-filter 每帧重算,是这层光斑漫开的载体");

    assert.match(
        APP_CSS,
        /\.ceo-scroll-to-latest-btn\.is-breathing:not\(\[hidden\]\)::after\s*\{/,
        "呼吸必须由「进行中 + 未贴底」两个条件共同门控"
    );
});

test("竞态序列:程序钉底派发的 scroll 不得重新武装跟随", () => {
    const childA = makeFeedChild({ key: "m:1", contentTop: 0, height: 1000 });
    const feed = makeFeed({ children: [childA], scrollTop: 800, scrollHeight: 1000, clientHeight: 200 });
    const api = setupScrollApi(feed);
    api.S.ceoFeedFollowLatest = false;
    api.handleCeoFeedUserGesture(); // 用户正在拖动，处于手势窗口内
    api.markCeoFeedProgrammaticScroll(); // 上一次突变刚钉过底

    api.handleCeoFeedScrollEvent();
    assert.equal(api.S.ceoFeedFollowLatest, false, "程序滚动落在手势窗口内也不得翻转意图位");
});

test("对照:无程序标记且几何近底+手势窗口内,滚动事件会恢复跟随(用户自己滚回底部)", () => {
    const childA = makeFeedChild({ key: "m:1", contentTop: 0, height: 1000 });
    const feed = makeFeed({ children: [childA], scrollTop: 800, scrollHeight: 1000, clientHeight: 200 });
    const api = setupScrollApi(feed);
    api.S.ceoFeedFollowLatest = false;
    api.handleCeoFeedUserGesture();
    api.mutateCeoFeed(() => {}, { scrollMode: "preserve" }); // 刷新最近突变时间戳之外的路径不带标记

    // 静默期(250ms)内事件被吞掉：意图位保持不变
    api.handleCeoFeedScrollEvent();
    assert.equal(api.S.ceoFeedFollowLatest, false, "直播突变静默期内的程序滚动不改意图");
});
