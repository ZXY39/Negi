const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const APP_PATH = "g3ku/web/frontend/org_graph_app.js";
const API_CLIENT_PATH = "g3ku/web/frontend/api_client.js";
const HTML_PATH = "g3ku/web/frontend/org_graph.html";
const CSS_PATH = "g3ku/web/frontend/org_graph.css";

const APP_CODE = fs.readFileSync(process.env.ORG_GRAPH_APP || APP_PATH, "utf8");
const API_CLIENT_CODE = fs.readFileSync(API_CLIENT_PATH, "utf8");
const HTML_CODE = fs.readFileSync(HTML_PATH, "utf8");
const CSS_CODE = fs.readFileSync(CSS_PATH, "utf8");

class StubElement {}

function makeClassList(owner) {
    const tokens = new Set();
    return {
        add: (name) => tokens.add(name),
        remove: (name) => tokens.delete(name),
        contains: (name) => tokens.has(name),
        toggle: (name, force) => {
            const shouldAdd = force === undefined ? !tokens.has(name) : !!force;
            if (shouldAdd) tokens.add(name);
            else tokens.delete(name);
            return shouldAdd;
        },
        _tokens: tokens,
    };
}

class StubHTMLElement extends StubElement {
    constructor() {
        super();
        this.hidden = false;
        this.disabled = false;
        this.textContent = "";
        this.className = "";
        this.dataset = {};
        this.attributes = {};
        this.style = {
            values: {},
            setProperty(name, value) {
                this.values[name] = String(value);
            },
            removeProperty(name) {
                delete this.values[name];
            },
        };
        this.children = [];
        this.scrollTop = 0;
        this.scrollHeight = 400;
        this.clientHeight = 300;
        this._innerHTML = "";
        this.classList = makeClassList(this);
    }

    addEventListener() {}
    setAttribute(name, value) {
        this.attributes[name] = String(value);
    }
    removeAttribute(name) {
        delete this.attributes[name];
    }
    getAttribute(name) {
        return Object.prototype.hasOwnProperty.call(this.attributes, name) ? this.attributes[name] : null;
    }
    appendChild(child) {
        child.parentElement = this;
        this.children.push(child);
        return child;
    }
    remove() {
        if (!this.parentElement) return;
        this.parentElement.children = this.parentElement.children.filter((child) => child !== this);
        this.parentElement = null;
    }
    querySelector(selector) {
        // 只按第一个类名做包含匹配，够用于 ".ceo-compression-divider.is-running" 这类选择器。
        const needle = String(selector || "").replace(/^\./, "").split(".")[0];
        if (!needle) return null;
        return this.children.find((child) => String(child.className || "").includes(needle)) || null;
    }
    querySelectorAll() {
        return [];
    }
    get lastElementChild() {
        return this.children[this.children.length - 1] || null;
    }

    set innerHTML(value) {
        const next = String(value);
        if (next === "") this.children = [];
        this._innerHTML = next;
    }

    get innerHTML() {
        return this._innerHTML;
    }
}

class StubHTMLButtonElement extends StubHTMLElement {}
class StubHTMLInputElement extends StubHTMLElement {}
class StubHTMLTextAreaElement extends StubHTMLElement {}
class StubHTMLSelectElement extends StubHTMLElement {}

function loadApp() {
    const clock = { now: 0 };
    // rAF 排成队列由测试按帧推进，才能分别验证「未满 5 秒松手」和「满 5 秒」两条分支。
    const frames = [];

    class FakeDate {
        constructor() {}
        static now() {
            return clock.now;
        }
        toISOString() {
            return "2026-09-19T00:00:00.000Z";
        }
    }

    const context = {
        console,
        Date: FakeDate,
        JSON,
        Math,
        Number,
        String,
        Boolean,
        Array,
        Object,
        RegExp,
        Error,
        TypeError,
        Promise,
        setTimeout,
        clearTimeout,
        setInterval: () => 1,
        clearInterval: () => {},
        queueMicrotask,
        navigator: { clipboard: { writeText: async () => {} } },
        location: { protocol: "http:", host: "localhost", origin: "http://localhost", pathname: "/org_graph.html" },
        localStorage: { getItem: () => null, setItem: () => {}, removeItem: () => {} },
        sessionStorage: { getItem: () => null, setItem: () => {}, removeItem: () => {} },
        document: {
            getElementById: () => null,
            createElement: () => new StubHTMLElement(),
            querySelector: () => null,
            querySelectorAll: () => [],
            addEventListener: () => {},
            body: new StubHTMLElement(),
            documentElement: new StubHTMLElement(),
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
        structuredClone: global.structuredClone,
        FormData,
        Blob,
        Headers,
        Request,
        Response,
        performance: { now: () => clock.now },
        requestAnimationFrame: (callback) => {
            frames.push(callback);
            return frames.length;
        },
        cancelAnimationFrame: (id) => {
            if (id > 0) frames[id - 1] = null;
        },
        WebSocket: function WebSocket() {},
        addEventListener: () => {},
        removeEventListener: () => {},
    };
    context.window = context;
    context.self = context;
    vm.createContext(context);
    vm.runInContext(
        `${API_CLIENT_CODE}\n${APP_CODE}
        this.__testExports = {
            S, U, ApiClient,
            beginCeoBrainHold, finishCeoBrainHold, ceoBrainHoldBlockedReason,
            activeCeoManualCompressionRunning, activeCeoSessionCompressionState,
            appendCeoCompressionDivider, syncCeoCompressionDivider,
            normalizeCeoSnapshotMessage, activeCeoSessionHasHistory,
            refreshCeoComposerUsageEstimate, syncCeoModelModePanelUsage,
            CEO_BRAIN_LONG_PRESS_MS,
        };`,
        context
    );
    const api = context.__testExports;
    api.clock = clock;
    api.pumpFrames = (count, stepMs = 1000) => {
        for (let index = 0; index < count; index += 1) {
            const callback = frames.shift();
            if (!callback) return index;
            clock.now += stepMs;
            callback();
        }
        return count;
    };
    api.pendingFetches = [];
    api.toasts = [];
    api.compressCalls = [];
    api.context = context;
    api.StubHTMLElement = StubHTMLElement;
    api.U.ceoFeed = new StubHTMLElement();
    api.U.ceoComposerUsageBrain = new StubHTMLElement();
    api.U.ceoComposerUsageBrainBase = new StubHTMLElement();
    api.U.ceoComposerUsageBrainFill = new StubHTMLElement();
    api.U.ceoComposerUsageBrainRing = new StubHTMLElement();
    api.U.ceoModelModeBadge = new StubHTMLElement();
    api.U.ceoModelModePanel = new StubHTMLElement();
    api.U.ceoModelModeUsageFill = new StubHTMLElement();
    api.U.ceoModelModeUsageText = new StubHTMLElement();
    api.U.ceoInput = new StubHTMLTextAreaElement();
    api.S.activeSessionId = "web:test";
    api.S.ceoSessions = [];
    api.S.ceoUploads = [];
    context.showToast = (payload) => api.toasts.push(payload);
    context.openConfirm = (payload) => api.toasts.push({ confirm: payload });
    return api;
}

function dividers(feed) {
    return feed.children.filter((child) => String(child.className || "").includes("ceo-compression-divider"));
}

test("长按未满 5 秒松手不发起压缩，进度环归零", () => {
    const api = loadApp();
    api.context.beginCeoContextCompression = (sessionId) => {
        api.compressCalls.push(sessionId);
    };

    api.beginCeoBrainHold({ button: 0, pointerType: "mouse" });
    api.pumpFrames(4);
    api.finishCeoBrainHold();

    assert.deepEqual(api.compressCalls, []);
    assert.equal(api.clock.now, 4000);
    assert.equal(api.U.ceoComposerUsageBrain.style.values["--ceo-brain-hold"], "0");
    assert.equal(api.U.ceoComposerUsageBrain.classList.contains("is-holding"), false);
    assert.equal(api.S.ceoBrainHold.active, false);
});

test("长按满 5 秒发起压缩并吞掉随后到达的 click", () => {
    const api = loadApp();
    api.context.beginCeoContextCompression = (sessionId) => {
        api.compressCalls.push(sessionId);
    };

    api.beginCeoBrainHold({ button: 0, pointerType: "mouse" });
    api.pumpFrames(5);

    assert.deepEqual(api.compressCalls, ["web:test"]);
    assert.equal(api.S.ceoBrainHoldConsumedClick, true);
    assert.equal(api.S.ceoBrainHold.active, false);
});

test("触摸与右键不启动长按", () => {
    const api = loadApp();
    api.context.beginCeoContextCompression = (sessionId) => {
        api.compressCalls.push(sessionId);
    };

    api.beginCeoBrainHold({ button: 0, pointerType: "touch" });
    api.clock.now += 10000;
    api.beginCeoBrainHold({ button: 2, pointerType: "mouse" });
    api.pumpFrames(6);

    assert.deepEqual(api.compressCalls, []);
    assert.equal(api.S.ceoBrainHold.active, false);
});

test("渠道会话与上传中拒绝压缩并给出原因", () => {
    const api = loadApp();

    api.S.activeSessionId = "ext:qq";
    assert.equal(api.ceoBrainHoldBlockedReason(), "只有本地会话可以压缩上下文");

    api.S.activeSessionId = "web:test";
    api.S.ceoUploadBusy = true;
    assert.equal(api.ceoBrainHoldBlockedReason(), "附件上传中，请稍后再试");

    api.S.ceoUploadBusy = false;
    api.S.ceoContextCompressionStatus = "running";
    api.S.ceoContextCompressionSessionId = "web:test";
    assert.equal(api.ceoBrainHoldBlockedReason(), "正在压缩上下文");
});

test("手动压缩进行中即视为该会话正在压缩", () => {
    const api = loadApp();

    api.S.ceoContextCompressionStatus = "running";
    api.S.ceoContextCompressionSessionId = "web:other";
    assert.equal(api.activeCeoManualCompressionRunning(), false);
    assert.equal(api.activeCeoSessionCompressionState(), null);

    api.S.ceoContextCompressionSessionId = "web:test";
    assert.equal(api.activeCeoManualCompressionRunning(), true);
    assert.equal(api.activeCeoSessionCompressionState().status, "running");
});

test("实时区分线只挂一条，进行中的图标即暂停按钮", () => {
    const api = loadApp();
    api.S.ceoContextCompressionStatus = "running";
    api.S.ceoContextCompressionSessionId = "web:test";

    api.syncCeoCompressionDivider();
    api.syncCeoCompressionDivider();

    const live = dividers(api.U.ceoFeed);
    assert.equal(live.length, 1);
    assert.equal(live[0].dataset.ceoCompressionState, "running");
    assert.match(live[0].innerHTML, /上下文压缩中/);
    assert.match(live[0].innerHTML, /data-ceo-compress-pause/);

    api.S.ceoContextCompressionStatus = "idle";
    api.syncCeoCompressionDivider();
    assert.equal(dividers(api.U.ceoFeed).length, 0);
});

test("终态区分线不带暂停按钮，完成态只有文案", () => {
    const api = loadApp();

    const completed = api.appendCeoCompressionDivider("completed", { interactive: false });
    assert.equal(completed.dataset.ceoCompressionState, "completed");
    assert.match(completed.innerHTML, /会话已压缩/);
    assert.doesNotMatch(completed.innerHTML, /data-ceo-compress-pause/);
    assert.doesNotMatch(completed.innerHTML, /data-lucide/);

    const paused = api.appendCeoCompressionDivider("paused", { interactive: false });
    assert.match(paused.innerHTML, /压缩已暂停/);
    assert.match(paused.innerHTML, /data-lucide="pause"/);
    assert.doesNotMatch(paused.innerHTML, /data-ceo-compress-pause/);
});

test("转录标记只认 completed 与 paused 两种终态", () => {
    const api = loadApp();

    const kept = api.normalizeCeoSnapshotMessage({
        role: "system",
        content: "会话已压缩",
        compression_marker: { state: "COMPLETED", source: "manual" },
    });
    assert.equal(kept.compression_marker.state, "completed");
    assert.equal(kept.compression_marker.source, "manual");

    const running = api.normalizeCeoSnapshotMessage({
        role: "system",
        content: "上下文压缩中",
        compression_marker: { state: "running" },
    });
    assert.equal(running.compression_marker, undefined);
});

test("输入框为空时已有历史的会话仍常驻显示占用值", async () => {
    const api = loadApp();
    const requested = [];
    api.context.fetch = async (url, init = {}) => {
        requested.push({ url: String(url), method: String(init.method || "GET") });
        return {
            ok: true,
            status: 200,
            json: async () => ({
                item: {
                    estimated_total_tokens: 12000,
                    context_window_tokens: 390000,
                    provider_model: "alpha",
                },
            }),
            text: async () => "{}",
        };
    };
    api.U.ceoInput.value = "";
    api.S.ceoTurnActive = false;
    api.S.ceoSessions = [{ session_id: "web:test", message_count: 4 }];

    await api.refreshCeoComposerUsageEstimate();

    assert.equal(requested.filter((entry) => entry.url.includes("/composer-preflight")).length, 1);
    assert.equal(api.S.ceoComposerUsageEstimate.estimated_total_tokens, 12000);
});

test("新会话空输入没有占用值可读", () => {
    const api = loadApp();

    api.S.ceoSessions = [{ session_id: "web:test", message_count: 0 }];
    assert.equal(api.activeCeoSessionHasHistory("web:test"), false);

    api.S.ceoSessions = [{ session_id: "web:test", message_count: 2 }];
    assert.equal(api.activeCeoSessionHasHistory("web:test"), true);
});

test("面板头部只显示模型配置名，进度条下方只显示 token 占用值", () => {
    const api = loadApp();
    api.S.ceoComposerUsageEstimate = {
        session_id: "web:test",
        provider_model: "alpha",
        estimated_total_tokens: 12000,
        context_window_tokens: 390000,
        ratio: 0.0308,
    };

    api.syncCeoModelModePanelUsage();

    assert.equal(api.U.ceoModelModeBadge.textContent, "alpha");
    assert.equal(api.U.ceoModelModeUsageText.hidden, false);
    assert.equal(api.U.ceoModelModeUsageText.textContent, "12000/390000 TOKEN");

    api.S.ceoComposerUsageEstimate = null;
    api.syncCeoModelModePanelUsage();
    assert.equal(api.U.ceoModelModeUsageText.hidden, true);
    assert.equal(api.U.ceoModelModeBadge.textContent, "等待 Leader 上下文预估");
});

test("压缩端点走 compress-context 三个路由", async () => {
    const calls = [];
    const clientContext = {
        console,
        window: {},
        document: { addEventListener: () => {}, querySelector: () => null, querySelectorAll: () => [] },
        location: { protocol: "http:", host: "localhost", origin: "http://localhost", pathname: "/org_graph.html" },
        localStorage: { getItem: () => null, setItem: () => {}, removeItem: () => {} },
        sessionStorage: { getItem: () => null, setItem: () => {}, removeItem: () => {} },
        setTimeout,
        clearTimeout,
        AbortController,
        URLSearchParams,
        URL,
        FormData,
        Blob,
        Headers,
        Request,
        Response,
        fetch: async (url, init = {}) => {
            calls.push({ url, method: String(init.method || "GET").toUpperCase() });
            return { ok: true, status: 200, json: async () => ({}), text: async () => "{}" };
        },
    };
    clientContext.window = clientContext;
    vm.createContext(clientContext);
    vm.runInContext(
        `${API_CLIENT_CODE}\nthis.__exports = { ApiClient };`,
        clientContext
    );
    const { ApiClient } = clientContext.__exports;

    await ApiClient.startCeoContextCompression("web:a b");
    await ApiClient.getCeoContextCompression("web:a b");
    await ApiClient.cancelCeoContextCompression("web:a b");

    const encoded = encodeURIComponent("web:a b");
    const base = "http://localhost";
    assert.deepEqual(calls.map((call) => [call.method, call.url]), [
        ["POST", `${base}/api/ceo/sessions/${encoded}/compress-context`],
        ["GET", `${base}/api/ceo/sessions/${encoded}/compress-context`],
        ["POST", `${base}/api/ceo/sessions/${encoded}/compress-context/cancel`],
    ]);
});

test("压缩 toast 让位给会话流区分线，长按环与提示进入 DOM", () => {
    assert.equal(HTML_CODE.includes("ceo-compression-toast"), false);
    assert.equal(HTML_CODE.includes('id="ceo-model-mode-current"'), false);
    assert.match(HTML_CODE, /class="ceo-context-usage-brain-ring"/);
    assert.match(HTML_CODE, /<span id="ceo-context-usage-brain-hint"[^>]*>长按压缩上下文<\/span>/);
    assert.match(CSS_CODE, /\.ceo-context-usage-brain-ring\s*\{[^}]*conic-gradient/);
    assert.match(CSS_CODE, /\.message\.ceo-compression-divider\s*\{[^}]*\}/);
    assert.match(CSS_CODE, /--ceo-context-compress-color:\s*#39c5bb/);
    assert.match(CSS_CODE, /\.ceo-compression-divider\.is-running[^}]*animation/);
});
