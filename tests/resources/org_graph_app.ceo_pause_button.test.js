const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const { readFrontendSources } = require("./support/frontend_sources.js");

// 主按钮的暂停语义：只要当前会话「不在稳定态」就一直显示暂停；会话真的停下
// （含报错终止）就必须回到发送。判据必须现算，不能读某一帧随手写下的布尔量——
// 心跳帧/内部轮收尾会把那个布尔量抹成 false，按钮于是一闪一闪；而报错停下的
// 会话没有任何东西会把它置回 false，按钮就永久挂在屏幕上。

const APP_PATH = "g3ku/web/frontend/org_graph_app.js";
const APP_CODE = readFrontendSources(["app"]).join("\n");
const SESSION_ID = "web:ceo-btn-s1";
const PAUSED = "\u6682\u505c";
const PAUSING = "\u6682\u505c\u4e2d";
const SEND = "\u53d1\u9001";

class StubElement {}
class StubHTMLElement extends StubElement {
    constructor() {
        super();
        this.className = "";
        this.hidden = false;
        this.textContent = "";
        this.dataset = {};
        this.attributes = {};
        this.children = [];
        this._qs = {};
        this._qsAll = {};
        this._innerHTML = "";
        this.classList = {
            add: () => {},
            remove: () => {},
            contains: () => false,
            toggle: () => false,
        };
    }
    get innerHTML() { return this._innerHTML; }
    set innerHTML(value) { this._innerHTML = String(value); }
    setAttribute(name, value) { this.attributes[name] = String(value); }
    getAttribute(name) { return Object.prototype.hasOwnProperty.call(this.attributes, name) ? this.attributes[name] : null; }
    removeAttribute(name) { delete this.attributes[name]; }
    querySelector(selector) {
        if (!this._qs[selector]) this._qs[selector] = new StubHTMLElement();
        return this._qs[selector];
    }
    querySelectorAll(selector) { return this._qsAll[selector] || []; }
    appendChild(child) { this.children.push(child); return child; }
    insertBefore(child) { this.children.push(child); return child; }
    remove() {}
    addEventListener() {}
    focus() {}
}

function loadApp() {
    const context = {
        console, setTimeout, clearTimeout, setInterval, clearInterval, queueMicrotask,
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
        HTMLButtonElement: StubHTMLElement,
        HTMLInputElement: StubHTMLElement,
        HTMLTextAreaElement: StubHTMLElement,
        HTMLSelectElement: StubHTMLElement,
        URLSearchParams, URL, AbortController,
        fetch: async () => ({ ok: true, json: async () => ({}) }),
        lucide: { createIcons() {} },
        marked: { parse: (value) => String(value) },
        DOMPurify: { sanitize: (value) => String(value) },
        performance: { now: () => 0 },
        requestAnimationFrame: (callback) => { callback(); return 1; },
        cancelAnimationFrame: () => {},
        WebSocket: function WebSocket() {},
        addEventListener() {},
        removeEventListener() {},
        setTraceRoundActiveTool: () => {},
        hydrateTraceOutputBlocks: () => {},
        ApiClient: {
            getCeoWsUrl: () => "ws://localhost/api/ws/ceo",
            setActiveSessionId() {},
            getActiveSessionId: () => SESSION_ID,
        },
    };
    context.window = context;
    vm.createContext(context);
    vm.runInContext(`${APP_CODE}
this.__x = {
    S, U, WebSocket,
    syncCeoPrimaryButton,
    activeCeoSessionIsNonStable,
    applyCeoState,
    handleCeoControlAck,
    handleCeoError,
    getCeoSessionSnapshotCache,
    setCeoSessionSnapshotCache,
};`, context);
    vm.runInContext(`
        renderCeoSessions = () => {};
        syncCeoSessionActions = () => {};
        patchCeoSessionRuntimeState = () => false;
        maybeDispatchQueuedCeoFollowUps = () => false;
        pruneRuntimeSentCeoFollowUps = () => {};
        scheduleSyncCeoComposerUsageOutline = () => {};
        syncCeoCompressionDivider = () => {};
        syncCeoModelRetryToast = () => {};
        syncCeoComposerUsageOutline = () => {};
        scheduleCeoComposerUsageRefresh = () => {};
        syncCeoApprovalFromSnapshotEntry = () => {};
    `, context);
    const api = context.__x;
    api.S.activeSessionId = SESSION_ID;
    api.S.ceoSessions = [{ session_id: SESSION_ID, is_readonly: false }];
    api.S.ceoLocalSessions = [{ session_id: SESSION_ID, is_readonly: false }];
    api.S.activeSessionFamily = "local";
    api.S.ceoUploads = [];
    api.U.ceoSend = new StubHTMLElement();
    api.U.ceoInput = { value: "", style: {}, scrollHeight: 40 };
    // 收尾路径（finalizeCeoTurn → addMsg）要往 feed 里塞气泡，给个能收子节点的桩。
    api.U.ceoFeed = new StubHTMLElement();
    api.WebSocket.OPEN = 1;
    api.S.ceoWs = { readyState: 1, send() {}, close() {} };
    return api;
}

function buttonState(api) {
    return {
        label: String(api.U.ceoSend.innerHTML || ""),
        disabled: !!api.U.ceoSend.disabled,
        aria: api.U.ceoSend.getAttribute("aria-label"),
    };
}

test("在飞回合存在时一直显示暂停，不受某一帧抹掉布尔量影响", () => {
    const api = loadApp();
    // 服务端在飞快照说 running：这就是权威读数。
    api.setCeoSessionSnapshotCache(SESSION_ID, {
        inflight_turn: { source: "user", turn_id: "t1", status: "running", assistant_text: "working" },
    });
    api.S.ceoTurnActive = false; // 模拟被心跳帧/收尾抹掉的那个布尔量
    api.syncCeoPrimaryButton();

    const state = buttonState(api);
    assert.ok(state.label.includes(PAUSED), `非稳定态必须显示暂停: ${state.label}`);
    assert.equal(state.disabled, false);
    assert.equal(state.aria, "暂停当前 Leader 会话");
    assert.equal(api.activeCeoSessionIsNonStable(), true, "权威读数说会话没停稳");
});

test("心跳帧把状态抹成空闲也不会让暂停消失（在飞仍在）", () => {
    const api = loadApp();
    api.setCeoSessionSnapshotCache(SESSION_ID, {
        inflight_turn: { source: "heartbeat", turn_id: "t-hb", status: "running", assistant_text: "heartbeat working" },
    });
    // 内部轮的心跳帧：status 不是 running、没有任何未了结的活。旧实现只看
    // S.ceoTurnActive，于是暂停按钮在这里消失——而这一轮其实还在跑。
    api.applyCeoState({ status: "completed", is_running: false, paused: false }, { source: "heartbeat" });

    assert.equal(api.activeCeoSessionIsNonStable(), true, "在飞快照仍说 running");
    assert.ok(buttonState(api).label.includes(PAUSED), "暂停按钮不能因为心跳帧消失");
});

test("会话报错停下后按钮回到发送，不会永久挂着暂停", () => {
    const api = loadApp();
    api.setCeoSessionSnapshotCache(SESSION_ID, {
        inflight_turn: { source: "user", turn_id: "t1", status: "running", assistant_text: "working" },
    });
    api.syncCeoPrimaryButton();
    assert.ok(buttonState(api).label.includes(PAUSED));

    // 报错终止：服务端清掉在飞快照，状态落到 error。
    api.setCeoSessionSnapshotCache(SESSION_ID, { inflight_turn: null });
    api.handleCeoError({ message: "boom", code: "legacy_session_error" });
    api.U.ceoInput.value = "";
    api.syncCeoPrimaryButton();

    const state = buttonState(api);
    assert.equal(state.disabled, true, "空输入框 + 已停下：主按钮没有可做的事");
    assert.ok(!state.label.includes(PAUSED), `会话已停下，不能再显示暂停: ${state.label}`);
    assert.equal(api.activeCeoSessionIsNonStable(), false);
});

test("手动暂停收尾后按钮回到发送", () => {
    const api = loadApp();
    api.setCeoSessionSnapshotCache(SESSION_ID, {
        inflight_turn: { source: "user", turn_id: "t1", status: "running", assistant_text: "working" },
    });
    api.syncCeoPrimaryButton();
    assert.ok(buttonState(api).label.includes(PAUSED));

    // 暂停确认 → state(completed) → 在飞快照清空。
    api.handleCeoControlAck({ action: "pause", accepted: true, source: "user", turn_id: "t1" });
    api.applyCeoState(
        { status: "completed", is_running: false, paused: false, stop_reason: "user_pause" },
        { source: "user" }
    );
    api.setCeoSessionSnapshotCache(SESSION_ID, { inflight_turn: null });
    api.syncCeoPrimaryButton();

    const state = buttonState(api);
    assert.ok(!state.label.includes(PAUSED), `暂停收尾后不该继续显示暂停: ${state.label}`);
    assert.equal(api.activeCeoSessionIsNonStable(), false);
});

test("输入框有草稿时主按钮是发送而不是暂停", () => {
    const api = loadApp();
    api.setCeoSessionSnapshotCache(SESSION_ID, {
        inflight_turn: { source: "user", turn_id: "t1", status: "running", assistant_text: "working" },
    });
    api.U.ceoInput.value = "下一件事";
    api.syncCeoPrimaryButton();

    const state = buttonState(api);
    assert.ok(state.label.includes(SEND), `有草稿时主按钮必须是发送: ${state.label}`);
    assert.equal(state.disabled, false);
    assert.equal(state.aria, "发送消息");
});

test("暂停请求进行中显示暂停中并置灰", () => {
    const api = loadApp();
    api.setCeoSessionSnapshotCache(SESSION_ID, {
        inflight_turn: { source: "user", turn_id: "t1", status: "running", assistant_text: "working" },
    });
    api.S.ceoPauseBusy = true;
    api.syncCeoPrimaryButton();

    const state = buttonState(api);
    assert.ok(state.label.includes(PAUSING), `暂停请求进行中必须显示暂停中: ${state.label}`);
    assert.equal(state.disabled, true);
});

test("等审批期间仍留着暂停入口", () => {
    const api = loadApp();
    api.setCeoSessionSnapshotCache(SESSION_ID, {
        inflight_turn: {
            source: "approval",
            turn_id: "t-approval",
            status: "paused",
            interrupts: [{
                id: "interrupt-1",
                value: {
                    kind: "frontdoor_tool_approval_batch",
                    batch_id: "batch:1",
                    review_items: [{ tool_call_id: "call-1", name: "exec", risk_level: "high", arguments: { command: "dir" } }],
                },
            }],
        },
    });
    api.syncCeoPrimaryButton();

    assert.equal(api.activeCeoSessionIsNonStable(), true, "等审批不算稳定态");
    assert.ok(buttonState(api).label.includes(PAUSED));
});
