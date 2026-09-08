const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

// CEO 会话 unread 状态机:切换会话后原会话「一次性豁免」契约。
// 背景:列表通道(ceo.sessions.patch/snapshot、REST)的 message_count 滞后于聊天通道渲染,
// 且切换瞬间 closeCeoWs 丢掉在途 patch;切走后迟到的计数补算曾把已看过的旧消息
// 误判为原会话 unread(假 unread)。修复:用户发起的切换/新建会话时,为被离开的会话武装
// 窗口期内的一条豁免(多槽、互不覆盖),其第一个正增量视为已读。
// 覆盖:基线 diff、豁免消费、一次性语义、多槽互不覆盖、无增量不消费、窗口过期、hydration 守卫、真实切换接线。

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
        return { top: 0, height: 10 };
    }

    appendChild(child) {
        this.children.push(child);
        return child;
    }

    remove() {}
}

function baseContext(clock) {
    return {
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
        performance: { now: () => clock.now },
        requestAnimationFrame: (callback) => {
            callback();
            return 1;
        },
        cancelAnimationFrame: () => {},
        WebSocket: function WebSocket() {},
        addEventListener() {},
        removeEventListener() {},
    };
}

function loadApp() {
    const clock = { now: 1_000_000 };
    const context = baseContext(clock);
    context.window = context;
    vm.createContext(context);
    vm.runInContext(
        `${APP_CODE}\nthis.__testExports = { S, syncCeoSessionUnreadState, markCeoSessionRead, armCeoSessionUnreadExemption, clearCeoSessionUnreadExemption, CEO_SESSION_UNREAD_EXEMPT_WINDOW_MS };`,
        context
    );
    // 可控时钟:豁免窗口过期用例需要推进时间。仅在加载完成后覆盖,顶层加载用真实 Date。
    context.Date = class extends Date {
        static now() {
            return clock.now;
        }
    };
    return { api: context.__testExports, clock };
}

// 加载应用并覆盖切换流程的所有 DOM/副作用函数,保留真实的 applyOptimisticCeoSessionSwitch / arm / sync。
// 用于验证「arm 的接线确实挂在用户发起的切换流程上」(删掉 arm 行,下面接线用例即失败)。
function loadAppWithSwitch() {
    const clock = { now: 1_000_000 };
    const context = baseContext(clock);
    context.window = context;
    vm.createContext(context);
    vm.runInContext(
        `${APP_CODE}\nthis.__testExports = { S, applyOptimisticCeoSessionSwitch, syncCeoSessionUnreadState, armCeoSessionUnreadExemption };`,
        context
    );
    vm.runInContext(
        `
        closeCeoWs = () => {};
        resetCeoComposerForSessionChange = () => {};
        resetCeoSessionState = () => {};
        renderCeoSessionSnapshotFromCache = () => false;
        renderCeoSessionLoadingState = () => {};
        renderCeoSessions = () => {};
        syncCeoComposerReadonlyState = () => {};
        syncCeoSessionActions = () => {};
        syncCeoPrimaryButton = () => {};
        ApiClient = {
            getCeoWsUrl: () => '',
            getErrorCode: () => '',
            friendlyErrorMessage: () => '',
            setActiveSessionId: () => {},
            getActiveSessionId: () => 'web:shared',
        };
        `,
        context
    );
    context.Date = class extends Date {
        static now() {
            return clock.now;
        }
    };
    return { api: context.__testExports, clock };
}

const mk = (sessionId, messageCount) => ({ session_id: sessionId, message_count: messageCount });
const exemptHas = (S, id) => Object.prototype.hasOwnProperty.call(S.ceoSessionUnreadExempt || {}, id);

// 初始 hydration:A 为 active,基线 A=5、B=2,unread 全零。
function setup() {
    const loaded = loadApp();
    const { api } = loaded;
    api.syncCeoSessionUnreadState([mk("A", 5), mk("B", 2)], "A");
    assert.equal(api.S.ceoSessionHydrated, true);
    // 跨 realm 对象不做 deepStrictEqual,逐字段断言。
    assert.equal(api.S.ceoSessionUnread.A, 0);
    assert.equal(api.S.ceoSessionUnread.B, 0);
    return loaded;
}

test("基线机制:非 active 会话的正增量照常累计 unread(豁免未武装时)", () => {
    const { api } = setup();
    const { S } = api;

    api.syncCeoSessionUnreadState([mk("A", 8), mk("B", 2)], "B");

    assert.equal(S.ceoSessionUnread.A, 3);
    assert.equal(S.ceoSessionMessageCounts.A, 8);
});

test("切换豁免:原会话的第一个正增量被豁免,不产生假 unread,基线直接抬平", () => {
    const { api } = setup();
    const { S } = api;

    api.armCeoSessionUnreadExemption("A");
    assert.equal(exemptHas(S, "A"), true);

    api.syncCeoSessionUnreadState([mk("A", 8), mk("B", 2)], "B");

    assert.equal(S.ceoSessionUnread.A, 0);
    assert.equal(S.ceoSessionMessageCounts.A, 8);
    assert.equal(exemptHas(S, "A"), false, "豁免消费后应清除");
});

test("豁免是一次性的:消费后原会话的真新消息照常计数(防过度修复)", () => {
    const { api } = setup();
    const { S } = api;

    api.armCeoSessionUnreadExemption("A");
    api.syncCeoSessionUnreadState([mk("A", 8), mk("B", 2)], "B");
    assert.equal(S.ceoSessionUnread.A, 0);

    api.syncCeoSessionUnreadState([mk("A", 9), mk("B", 2)], "B");
    assert.equal(S.ceoSessionUnread.A, 1);
});

test("多槽豁免互不覆盖:快速 A→B→C 连续切换时,先离开会话的迟到补算仍被各自豁免", () => {
    const { api } = setup();
    const { S } = api;

    // A→B 武装 A,紧接着 B→C 武装 B:两槽并存,不互相覆盖
    api.armCeoSessionUnreadExemption("A");
    api.armCeoSessionUnreadExemption("B");
    assert.equal(exemptHas(S, "A"), true);
    assert.equal(exemptHas(S, "B"), true);

    // 迟到的全量 snapshot 只补算了 A(B 计数未变):A 的 +3 被 A 的豁免吸收,B 的豁免保持待用
    api.syncCeoSessionUnreadState([mk("A", 8), mk("B", 2), mk("C", 1)], "C");
    assert.equal(S.ceoSessionUnread.A, 0);
    assert.equal(exemptHas(S, "A"), false);
    assert.equal(exemptHas(S, "B"), true, "B 无增量时豁免不被消费");

    // B 的第一条正增量随后到达,仍被 B 的豁免吸收
    api.syncCeoSessionUnreadState([mk("A", 8), mk("B", 5), mk("C", 1)], "C");
    assert.equal(S.ceoSessionUnread.B, 0);
    assert.equal(exemptHas(S, "B"), false);

    // A 真正再来一条新消息 → 正常累计
    api.syncCeoSessionUnreadState([mk("A", 9), mk("B", 5), mk("C", 1)], "C");
    assert.equal(S.ceoSessionUnread.A, 1);
});

test("无正增量时豁免不被消费:迟到的补算仍能被豁免", () => {
    const { api } = setup();
    const { S } = api;

    api.armCeoSessionUnreadExemption("A");

    api.syncCeoSessionUnreadState([mk("A", 5), mk("B", 2)], "B");
    assert.equal(exemptHas(S, "A"), true);
    assert.equal(S.ceoSessionUnread.A, 0);

    api.syncCeoSessionUnreadState([mk("A", 8), mk("B", 2)], "B");
    assert.equal(S.ceoSessionUnread.A, 0);
    assert.equal(exemptHas(S, "A"), false);
});

test("豁免窗口过期后失效:切换很久后的新消息不被误吞", () => {
    const { api, clock } = setup();
    const { S } = api;

    api.armCeoSessionUnreadExemption("A");
    clock.now += api.CEO_SESSION_UNREAD_EXEMPT_WINDOW_MS + 1;

    api.syncCeoSessionUnreadState([mk("A", 8), mk("B", 2)], "B");

    assert.equal(S.ceoSessionUnread.A, 3);
    assert.equal(exemptHas(S, "A"), false);
});

test("active 会话恒 unread=0 且基线随 payload 刷新,豁免不影响 active 分支", () => {
    const { api } = setup();
    const { S } = api;

    api.armCeoSessionUnreadExemption("A");
    api.syncCeoSessionUnreadState([mk("A", 5), mk("B", 4)], "B");

    assert.equal(S.ceoSessionUnread.B, 0);
    assert.equal(S.ceoSessionMessageCounts.B, 4);
    assert.equal(exemptHas(S, "A"), true, "active 分支不消费他会话的豁免");
});

test("hydration 守卫:水合前 arm 是 no-op(防冷启动为回退 id 误武装)", () => {
    const { api } = loadApp();
    const { S } = api;

    assert.equal(S.ceoSessionHydrated, false);
    api.armCeoSessionUnreadExemption("web:shared");
    assert.equal(exemptHas(S, "web:shared"), false);
    assert.deepEqual(Object.keys(S.ceoSessionUnreadExempt), []);

    // 水合后再 arm 才生效
    api.syncCeoSessionUnreadState([mk("A", 5)], "A");
    api.armCeoSessionUnreadExemption("A");
    assert.equal(exemptHas(S, "A"), true);
});

test("markCeoSessionRead 清零 unread 并可校准基线(既有契约回归)", () => {
    const { api } = setup();
    const { S } = api;

    api.syncCeoSessionUnreadState([mk("A", 8), mk("B", 2)], "B");
    assert.equal(S.ceoSessionUnread.A, 3);

    api.markCeoSessionRead("A", { messageCount: 8 });
    assert.equal(S.ceoSessionUnread.A, 0);
    assert.equal(S.ceoSessionMessageCounts.A, 8);
});

test("armCeoSessionUnreadExemption 对空 id 是 no-op", () => {
    const { api } = setup();
    const { S } = api;

    api.armCeoSessionUnreadExemption("");
    assert.equal(Object.keys(S.ceoSessionUnreadExempt).length, 0);

    api.armCeoSessionUnreadExemption("  ");
    assert.equal(Object.keys(S.ceoSessionUnreadExempt).length, 0);
});

test("接线:applyOptimisticCeoSessionSwitch 会为被离开的会话武装豁免,并吸收其后迟到的补算", () => {
    const { api } = loadAppWithSwitch();
    const { S, applyOptimisticCeoSessionSwitch, syncCeoSessionUnreadState } = api;

    // 先水合并建立基线:A active,count 5;B inactive,count 2
    S.activeSessionId = "web:A";
    syncCeoSessionUnreadState([mk("web:A", 5), mk("web:B", 2)], "web:A");
    assert.equal(S.ceoSessionHydrated, true);

    // 用户点击切换到 B:真实 applyOptimisticCeoSessionSwitch 路径
    const result = applyOptimisticCeoSessionSwitch("web:B", { session_family: "local" });
    assert.equal(result.switched, true);
    assert.equal(exemptHas(S, "web:A"), true, "切换流程必须武装被离开的会话");

    // 切换后迟到的全量 snapshot 补算 A:5→8,被豁免吸收
    syncCeoSessionUnreadState([mk("web:A", 8), mk("web:B", 2)], "web:B");
    assert.equal(S.ceoSessionUnread["web:A"], 0);
    assert.equal(exemptHas(S, "web:A"), false);
});