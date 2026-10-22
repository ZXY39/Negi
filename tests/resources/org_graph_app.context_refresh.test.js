const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { readFrontendSources } = require("./support/frontend_sources.js");

// 模型页「上下文信息更新时间」的契约：入口在模型页头部动作组、弹窗默认启用 + 120 分钟、
// 草稿与服务端读数分开，只有点「应用」才发 PUT；失败不关窗。

const ROOT = path.resolve(__dirname, "..", "..");
const APP_CODE = readFrontendSources(["app"]).join("\n");
const API_CODE = readFrontendSources(["api_client"]).join("\n");
const HTML = fs.readFileSync(path.join(ROOT, "g3ku/web/frontend/org_graph.html"), "utf8");

function stubElement(extra = {}) {
    return {
        innerHTML: "",
        textContent: "",
        value: "",
        checked: false,
        hidden: true,
        disabled: false,
        attributes: {},
        style: {},
        listeners: {},
        classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
        setAttribute(name, value) { this.attributes[name] = value; },
        getAttribute(name) { return this.attributes[name]; },
        toggleAttribute(name, force) { this.attributes[name] = force ? "" : undefined; },
        focus() {},
        querySelector: () => null,
        querySelectorAll: () => [],
        addEventListener(type, handler) { (this.listeners[type] ||= []).push(handler); },
        dispatch(type, event) { (this.listeners[type] || []).forEach((handler) => handler(event)); },
        appendChild() {},
        insertBefore() {},
        remove() {},
        closest: () => null,
        ...extra,
    };
}

function dialogElements() {
    return {
        "model-context-refresh-btn": stubElement(),
        "context-refresh-backdrop": stubElement(),
        "context-refresh-dialog": stubElement(),
        "context-refresh-enabled": stubElement(),
        "context-refresh-minutes": stubElement(),
        "context-refresh-error": stubElement(),
        "context-refresh-apply-btn": stubElement(),
        "context-refresh-cancel-btn": stubElement(),
        "context-refresh-close-btn": stubElement(),
    };
}

function baseContext(elements = {}) {
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
            getElementById: (id) => elements[String(id)] || null,
            querySelector: () => null,
            querySelectorAll: () => [],
            createElement: () => ({ style: {} }),
            addEventListener: () => {},
            activeElement: null,
            body: {},
        },
        Element: function Element() {},
        HTMLElement: function HTMLElement() {},
        HTMLInputElement: function HTMLInputElement() {},
        HTMLSelectElement: function HTMLSelectElement() {},
        URLSearchParams,
        URL,
        AbortController,
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
    };
}

function loadPage({ settings = { enabled: true, idle_minutes: 120 }, putFails = false } = {}) {
    const elements = dialogElements();
    const context = baseContext(elements);
    context.window = context;
    vm.createContext(context);
    const calls = { get: 0, put: [], toasts: [] };
    context.__calls = calls;
    context.__settings = settings;
    context.__putFails = putFails;
    vm.runInContext(`${APP_CODE}\nthis.__exports = {
        S,
        bindContextRefreshDialog,
        openContextRefreshDialog,
        applyContextRefreshSettings,
        closeContextRefreshDialog,
        renderContextRefreshDialog,
        contextRefreshDraftDirty,
    };`, context);
    const app = context.__exports;
    // ApiClient 与 showToast 是页面级全局：在同一个 realm 里换成探针。
    vm.runInContext(`
        ApiClient = {
            getContextRefreshSettings: async () => { __calls.get += 1; return { ok: true, context_refresh: __settings }; },
            updateContextRefreshSettings: async (payload) => {
                __calls.put.push({ enabled: payload.enabled, idleMinutes: Number(payload.idleMinutes) });
                if (__putFails) { throw new Error("配置被占用"); }
                __settings = { enabled: payload.enabled, idle_minutes: Number(payload.idleMinutes) };
                return { ok: true, context_refresh: __settings };
            },
        };
        showToast = (options) => { __calls.toasts.push(options); };
    `, context);
    app.bindContextRefreshDialog();
    return { app, elements, calls, context };
}

const flush = async () => new Promise((resolve) => setImmediate(resolve));

test("入口在模型页动作组，且不挂进模型链那颗按钮的状态机", () => {
    const actions = HTML.match(/<div class="resource-header-actions model-page-actions">[\s\S]*?<\/div>/);
    assert.ok(actions, "模型页头部动作组必须存在");
    assert.match(actions[0], /id="model-context-refresh-btn"[^>]*>上下文信息更新时间</);
    // 「保存」只在脏状态出现（HTML 里默认 hidden），空闲刷新按钮必须是独立的一颗。
    const saveTag = actions[0].match(/id="model-roles-save-btn"([^>]*)>([^<]*)</);
    assert.equal(saveTag[2], "保存");
    assert.match(saveTag[1], / type="button" hidden$/);
});

test("弹窗按事实描述：默认启用、默认 120 分钟、只有应用才提交", () => {
    const dialog = HTML.match(/<section id="context-refresh-dialog"[\s\S]*?<\/section>/);
    assert.ok(dialog, "弹窗必须存在");
    assert.match(dialog[0], /<h2 id="context-refresh-title">长时间未活跃后刷新上下文信息<\/h2>/);
    assert.doesNotMatch(dialog[0], /缓存命中清零/, "标题不许承诺一个实测不成立的判据");
    assert.match(dialog[0], /距上次请求超过设定时间后/);
    const minutesTag = dialog[0].match(/<input[^>]*id="context-refresh-minutes"[^>]*>/)[0];
    assert.match(minutesTag, /type="number"/);
    assert.match(minutesTag, /min="1"/);
    assert.match(minutesTag, /value="120"/);
    assert.doesNotMatch(minutesTag, /max=/, "不设上限：上限没有可推导的约束");
    assert.match(dialog[0], /id="context-refresh-apply-btn"[^>]*>应用</);
});

test("api_client 走 context-refresh 这一对路径", () => {
    const context = baseContext();
    context.window = context;
    vm.createContext(context);
    vm.runInContext(`${API_CODE}\nthis.__ApiClient = ApiClient;`, context);
    const ApiClient = context.__ApiClient;
    assert.equal(typeof ApiClient.getContextRefreshSettings, "function");
    assert.equal(typeof ApiClient.updateContextRefreshSettings, "function");
});

test("弹窗显隐形状照抄同页可用的那一族（backdrop 载显隐，section 不带 hidden）", () => {
    // `.confirm-backdrop` 这一族靠外层显示：section 上再挂 hidden，外层打开后内层仍是
    // display:none，表现就是"点了没窗口"。按 model-group 弹窗的实际形状钉住。
    const groupBackdrop = HTML.match(/<div id="model-group-backdrop" class="confirm-backdrop"[^>]*>/)[0];
    const groupSection = HTML.match(/<section id="model-group-dialog"[^>]*>/)[0];
    const mineBackdrop = HTML.match(/<div id="context-refresh-backdrop" class="confirm-backdrop"[^>]*>/)[0];
    const mineSection = HTML.match(/<section id="context-refresh-dialog"[^>]*>/)[0];
    assert.match(groupBackdrop, /hidden/);
    assert.match(mineBackdrop, /hidden/);
    assert.doesNotMatch(groupSection, /hidden/, "样板 section 不带 hidden");
    assert.doesNotMatch(mineSection, /hidden/, "新弹窗 section 也不许带 hidden");
});

test("点按钮真的把外层打开（从点击入口喂，不直接调渲染函数）", async () => {
    const { elements, calls } = loadPage();

    elements["model-context-refresh-btn"].dispatch("click", {});
    await flush();

    assert.equal(elements["context-refresh-backdrop"].hidden, false);
    assert.equal(elements["context-refresh-dialog"].attributes["aria-hidden"], "false");
    assert.equal(calls.get, 1);
});

test("打开弹窗先取服务端读数，草稿没变时「应用」不可点", async () => {
    const { elements, calls } = loadPage({ settings: { enabled: true, idle_minutes: 45 } });

    elements["model-context-refresh-btn"].dispatch("click", {});
    await flush();

    assert.equal(calls.get, 1);
    assert.equal(elements["context-refresh-minutes"].value, "45");
    assert.equal(elements["context-refresh-enabled"].checked, true);
    assert.equal(elements["context-refresh-apply-btn"].disabled, true);
    assert.equal(calls.put.length, 0, "只读一次不许写盘");
});

test("改分钟数只动草稿，点应用才提交", async () => {
    const { app, elements, calls } = loadPage();

    elements["model-context-refresh-btn"].dispatch("click", {});
    await flush();
    elements["context-refresh-minutes"].dispatch("input", { target: { value: "30" } });
    elements["context-refresh-minutes"].value = "30";
    elements["context-refresh-enabled"].dispatch("change", { target: { checked: false } });
    await flush();

    assert.equal(calls.put.length, 0, "改动草稿不许当场写盘");
    assert.equal(elements["context-refresh-apply-btn"].disabled, false);

    elements["context-refresh-apply-btn"].dispatch("click", {});
    await flush();
    await flush();

    // vm 里造出来的对象带着另一个 realm 的原型，结构断言统一走 JSON 文本比较。
    assert.equal(JSON.stringify(calls.put), JSON.stringify([{ enabled: false, idleMinutes: 30 }]));
    assert.equal(S_open(app), false, "应用成功后必须关窗");
    assert.equal(app.S.modelCatalog.contextRefresh.saved.idleMinutes, 30);
    assert.equal(app.S.modelCatalog.contextRefresh.saved.enabled, false);
    const toast = calls.toasts.at(-1);
    assert.equal(toast.kind, "success", "成功侧要有自己的反馈");
    assert.match(toast.text, /已禁用/);
});

test("写盘失败时不关窗，错误行与失败提示各一份", async () => {
    const { app, elements, calls } = loadPage({ putFails: true });

    elements["model-context-refresh-btn"].dispatch("click", {});
    await flush();
    elements["context-refresh-minutes"].dispatch("input", { target: { value: "15" } });
    elements["context-refresh-apply-btn"].dispatch("click", {});
    await flush();
    await flush();

    assert.equal(S_open(app), true, "失败必须留在窗口里");
    assert.equal(elements["context-refresh-error"].hidden, false);
    assert.match(elements["context-refresh-error"].textContent, /配置被占用/);
    assert.equal(calls.toasts.at(-1).kind, "error");
    assert.equal(app.S.modelCatalog.contextRefresh.saved.idleMinutes, 120, "失败不许把服务端读数当已改");
});

test("分钟数低于一分钟时本地拦下，不发请求", async () => {
    const { app, elements, calls } = loadPage();

    elements["model-context-refresh-btn"].dispatch("click", {});
    await flush();
    elements["context-refresh-minutes"].dispatch("input", { target: { value: "0" } });
    elements["context-refresh-apply-btn"].dispatch("click", {});
    await flush();
    await flush();

    assert.equal(calls.put.length, 0);
    assert.match(elements["context-refresh-error"].textContent, /至少为 1 分钟/);
});

function S_open(app) {
    return app.S.modelCatalog.contextRefresh.open;
}
