const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const APP_PATH = "g3ku/web/frontend/org_graph_app.js";
// Windows 上经 autocrlf 检出为 CRLF；断言按 LF 匹配，读入后统一归一化行尾。
const APP_CODE = fs.readFileSync(APP_PATH, "utf8").replace(/\r\n/g, "\n");

class StubElement {}
class StubHTMLElement extends StubElement {
    constructor() {
        super();
        this.hidden = false;
        this.disabled = false;
        this.value = "";
        this.textContent = "";
        this.innerHTML = "";
        this.className = "";
        this.dataset = {};
        this.style = {
            setProperty(name, value) {
                this[name] = String(value);
            },
            removeProperty(name) {
                delete this[name];
            },
        };
        this.attributes = {};
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
                const classes = new Set(String(this.className || "").split(/\s+/).filter(Boolean));
                const shouldAdd = force == null ? !classes.has(token) : !!force;
                if (shouldAdd) classes.add(token);
                else classes.delete(token);
                this.className = [...classes].join(" ");
                return shouldAdd;
            },
        };
    }
    addEventListener(type, handler) {
        this.listeners = this.listeners || {};
        (this.listeners[type] = this.listeners[type] || []).push(handler);
    }
    setAttribute(name, value) {
        this.attributes[name] = String(value);
    }
    getAttribute(name) {
        return this.attributes[name];
    }
    removeAttribute(name) {
        delete this.attributes[name];
    }
    querySelector() {
        return null;
    }
    querySelectorAll() {
        return [];
    }
    appendChild() {}
    insertBefore() {}
    remove() {}
    focus() {}
}

class StubDocument {
    getElementById() {
        return null;
    }
    querySelector() {
        return null;
    }
    querySelectorAll() {
        return [];
    }
    createElement() {
        return new StubHTMLElement();
    }
    addEventListener() {}
}

function loadApp(apiClientOverrides = {}) {
    const calls = [];
    const ApiClient = {
        getActiveSessionId: () => "",
        setActiveSessionId() {},
        getCeoSessionModelSelection: async (sessionId) => {
            calls.push(["get", sessionId]);
            return { ok: true, session_id: sessionId, mode: "chain", model_key: "", pinned_available: true };
        },
        updateCeoSessionModelSelection: async (sessionId, payload) => {
            calls.push(["patch", sessionId, payload]);
            return {
                ok: true,
                session_id: sessionId,
                mode: payload.mode,
                model_key: payload.model_key || "",
                pinned_available: true,
            };
        },
        updateModelRoleChain: async (scope, payload) => {
            calls.push(["chain", scope, payload]);
            return {
                catalog: CATALOG.map((item) => ({ ...item })),
                roles: { ceo: [...payload.modelKeys] },
                roleIterations: {},
                roleConcurrency: {},
            };
        },
        estimateCeoComposerPreflight: async () => null,
        ...apiClientOverrides,
    };
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
        HTMLButtonElement: StubHTMLElement,
        HTMLInputElement: StubHTMLElement,
        HTMLTextAreaElement: StubHTMLElement,
        SVGElement: StubHTMLElement,
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
        ApiClient,
    };
    context.window = context;
    vm.createContext(context);
    vm.runInContext(
        `${APP_CODE}
        this.__testExports = {
            S,
            U,
            syncCeoModelModeControl,
            syncCeoModelModePanelUsage,
            resetCeoModelSelection,
            applyCeoModelSelectionPayload,
            refreshCeoModelSelection,
            saveCeoModelSelection,
            saveCeoModelChain,
            openCeoModelModePanel,
            openCeoModelModePicker,
            showCeoModelChainPane,
            closeCeoModelModePanel,
            renderCeoModelPicker,
            renderCeoModelChainPane,
            filterCeoModelPickerModels,
            ceoModelDisplayTitle,
            ceoModelChainKeys,
            finishCeoModelChainDrag,
            bindCeoModelModeControls,
            ceoModelChainDirty,
        };`,
        context
    );
    const exported = context.__testExports;
    exported.calls = calls;
    return exported;
}

const CATALOG = [
    { key: "alpha", name: "Alpha 配置", provider_model: "openai:a", enabled: true },
    { key: "beta", name: "", provider_model: "openai:b", enabled: true },
    { key: "gamma", name: "Gamma", provider_model: "openai:g", enabled: false },
];

// VM 内构造的载荷对象跨 realm，先序列化回宿主对象再做严格比较。
const callsOf = (app) => JSON.parse(JSON.stringify(app.calls));

function mountControl(app, { readonly = false, chain = ["alpha", "beta", "gamma"] } = {}) {
    const { U, S } = app;
    S.activeSessionId = "web:test";
    S.ceoSessions = [{ session_id: "web:test", is_readonly: readonly }];
    U.ceoComposerUsageBrain = new StubHTMLElement();
    U.ceoModelModePanel = new StubHTMLElement();
    U.ceoModelModeCurrent = new StubHTMLElement();
    U.ceoModelModeBadge = new StubHTMLElement();
    U.ceoModelModeUsageFill = new StubHTMLElement();
    U.ceoModelModeUsageText = new StubHTMLElement();
    U.ceoModelModeChain = new StubHTMLElement();
    U.ceoModelModePinned = new StubHTMLElement();
    U.ceoModelChainPane = new StubHTMLElement();
    U.ceoModelChainList = new StubHTMLElement();
    U.ceoModelChainEmpty = new StubHTMLElement();
    U.ceoModelChainActions = new StubHTMLElement();
    U.ceoModelChainApply = new StubHTMLElement();
    U.ceoModelPicker = new StubHTMLElement();
    U.ceoModelPickerSearch = new StubHTMLElement();
    U.ceoModelPickerList = new StubHTMLElement();
    U.ceoModelPickerEmpty = new StubHTMLElement();
    U.ceoModelModeNote = new StubHTMLElement();
    S.modelCatalog = {
        ...(S.modelCatalog || {}),
        catalog: CATALOG.map((item) => ({ ...item })),
        roles: { ceo: [...chain] },
    };
}

test("默认态是模型链，脑图标未展开面板", () => {
    const app = loadApp();
    mountControl(app);
    app.resetCeoModelSelection("web:test");

    assert.equal(app.U.ceoModelModePanel.hidden, true);
    assert.equal(app.U.ceoModelModeChain.getAttribute("aria-checked"), "true");
    assert.equal(app.U.ceoModelModePinned.getAttribute("aria-checked"), "false");
    assert.equal(app.U.ceoModelModeBadge.textContent, "模型链");
    assert.equal(app.U.ceoComposerUsageBrain.getAttribute("aria-expanded"), "false");
    assert.equal(app.U.ceoComposerUsageBrain.classList.contains("is-panel-open"), false);
});

test("打开面板展示模型链列表并切换脑图标展开态", () => {
    const app = loadApp();
    mountControl(app);
    app.openCeoModelModePanel();

    assert.equal(app.U.ceoModelModePanel.hidden, false);
    assert.equal(app.U.ceoComposerUsageBrain.getAttribute("aria-expanded"), "true");
    assert.equal(app.U.ceoComposerUsageBrain.classList.contains("is-panel-open"), true);
    // 模型链模式显示链面板、隐藏指定模型列表。
    assert.equal(app.U.ceoModelChainPane.hidden, false);
    assert.equal(app.U.ceoModelPicker.hidden, true);
    // 紧凑链行：只显示配置名称，带拖动把手，按服务端顺序渲染。
    const markup = app.U.ceoModelChainList.innerHTML;
    assert.equal((markup.match(/class="ceo-model-chain-grip"/g) || []).length, 3);
    assert.equal((markup.match(/class="ceo-model-chain-title"/g) || []).length, 3);
    assert.equal((markup.match(/resource-list-subtitle|policy-chip/g) || []).length, 0);
    assert.ok(markup.indexOf("Alpha 配置") < markup.indexOf("beta"));
    assert.ok(markup.indexOf("beta") < markup.indexOf("Gamma"));
    assert.equal(app.U.ceoModelChainActions.hidden, true);
});

test("固定模型后链面板换成指定模型列表并显示固定标签", () => {
    const app = loadApp();
    mountControl(app);
    app.applyCeoModelSelectionPayload("web:test", { mode: "model", model_key: "alpha", pinned_available: true });
    app.openCeoModelModePicker();

    assert.equal(app.U.ceoModelChainPane.hidden, true);
    assert.equal(app.U.ceoModelPicker.hidden, false);
    assert.equal(app.U.ceoModelModePinned.getAttribute("aria-checked"), "true");
    assert.equal(app.U.ceoModelModeBadge.textContent, "会话固定 · Alpha 配置");
});

test("固定模型失效时回到模型链并给出回退提示", () => {
    const app = loadApp();
    mountControl(app);
    app.applyCeoModelSelectionPayload("web:test", {
        mode: "model",
        model_key: "removed",
        pinned_available: false,
    });
    app.openCeoModelModePanel();

    assert.equal(app.U.ceoModelModeChain.getAttribute("aria-checked"), "true");
    assert.equal(app.U.ceoModelModeBadge.textContent, "模型链");
    assert.equal(app.U.ceoModelModeNote.hidden, false);
    assert.match(app.U.ceoModelModeNote.textContent, /已自动回退模型链/);
    // 失效后展示的是真实模型链，而不是固定项。
    assert.equal(app.U.ceoModelChainPane.hidden, false);
});

test("面板头部展示当前模型展示名与上下文数字", () => {
    const app = loadApp();
    mountControl(app);
    app.openCeoModelModePanel();
    app.S.ceoComposerUsageEstimate = {
        session_id: "web:test",
        provider_model: "alpha",
        estimated_total_tokens: 12000,
        context_window_tokens: 390000,
        ratio: 0.0308,
    };

    app.syncCeoModelModePanelUsage();

    assert.equal(app.U.ceoModelModeCurrent.textContent, "Alpha 配置");
    assert.match(app.U.ceoModelModeUsageText.textContent, /alpha · 12000\/390000 TOKEN/);
});

test("选择模型提交 PATCH 并收起面板", async () => {
    const app = loadApp();
    mountControl(app);
    app.openCeoModelModePicker();

    const applied = await app.saveCeoModelSelection("model", "alpha");

    assert.deepEqual(callsOf(app), [["patch", "web:test", { mode: "model", model_key: "alpha" }]]);
    assert.equal(applied.modelKey, "alpha");
    assert.equal(app.U.ceoModelModePanel.hidden, true);
});

test("切回模型链提交 chain 载荷并留在面板继续看链", async () => {
    const app = loadApp();
    mountControl(app);
    app.applyCeoModelSelectionPayload("web:test", { mode: "model", model_key: "alpha", pinned_available: true });

    await app.saveCeoModelSelection("chain");

    assert.deepEqual(callsOf(app), [["patch", "web:test", { mode: "chain" }]]);
    assert.equal(app.U.ceoModelModeChain.getAttribute("aria-checked"), "true");
    assert.equal(app.U.ceoModelModeBadge.textContent, "模型链");
    assert.equal(app.U.ceoModelModePanel.hidden, true);
});

test("已是当前选择时不重复发请求", async () => {
    const app = loadApp();
    mountControl(app);
    app.applyCeoModelSelectionPayload("web:test", { mode: "model", model_key: "alpha", pinned_available: true });

    await app.saveCeoModelSelection("model", "alpha");

    assert.deepEqual(callsOf(app), []);
});

test("保存失败时保留错误状态并收起保存中标记", async () => {
    const app = loadApp({
        updateCeoSessionModelSelection: async () => {
            throw new Error("model_key_disabled");
        },
    });
    mountControl(app);

    const applied = await app.saveCeoModelSelection("model", "gamma");

    assert.equal(applied, null);
    assert.equal(app.S.ceoModelSelection.saving, false);
    assert.equal(app.S.ceoModelSelection.error, "model_key_disabled");
});

test("渠道/只读会话不打开面板也不发起读写", async () => {
    const app = loadApp();
    mountControl(app, { readonly: true });

    app.openCeoModelModePanel();
    const loaded = await app.refreshCeoModelSelection("web:test");

    assert.equal(app.U.ceoModelModePanel.hidden, true);
    assert.equal(app.U.ceoComposerUsageBrain.getAttribute("aria-expanded"), "false");
    assert.equal(loaded, null);
    assert.deepEqual(callsOf(app), []);
});

test("搜索按展示名与 key 过滤列表", () => {
    const app = loadApp();
    mountControl(app);
    app.openCeoModelModePicker();
    // VM 内数组跨 realm，先转成宿主数组再做严格比较。
    const keysOf = () => Array.from(app.filterCeoModelPickerModels(), (item) => item.key);

    app.S.ceoModelSelection = { ...app.S.ceoModelSelection, search: "gamma" };
    assert.deepEqual(keysOf(), ["gamma"]);

    app.S.ceoModelSelection = { ...app.S.ceoModelSelection, search: "openai:b" };
    assert.deepEqual(keysOf(), ["beta"]);

    app.S.ceoModelSelection = { ...app.S.ceoModelSelection, search: "" };
    assert.deepEqual(keysOf().sort(), ["alpha", "beta", "gamma"]);
});

test("列表把禁用绑定渲染成不可选项并标记当前选中项", () => {
    const app = loadApp();
    mountControl(app);
    app.applyCeoModelSelectionPayload("web:test", { mode: "model", model_key: "alpha", pinned_available: true });
    app.openCeoModelModePicker();

    const markup = app.U.ceoModelPickerList.innerHTML;
    assert.match(markup, /data-ceo-model-pick="gamma" disabled/);
    assert.match(markup, /已禁用/);
    assert.match(markup, /class="ceo-model-picker-item is-selected"[^>]*data-ceo-model-pick="alpha"/);
    assert.equal(app.U.ceoModelPickerEmpty.hidden, true);
});

test("无匹配项时显示空态文案", () => {
    const app = loadApp();
    mountControl(app);
    app.openCeoModelModePicker();
    app.S.ceoModelSelection = { ...app.S.ceoModelSelection, search: "不存在" };

    app.renderCeoModelPicker();

    assert.equal(app.U.ceoModelPickerEmpty.hidden, false);
    assert.equal(app.U.ceoModelPickerList.innerHTML, "");
});

test("面板关闭函数返回是否真的关闭（供 Escape 处理器判定）", () => {
    const app = loadApp();
    mountControl(app);

    assert.equal(app.closeCeoModelModePanel(), false);
    app.openCeoModelModePanel();
    assert.equal(app.closeCeoModelModePanel(), true);
    assert.equal(app.U.ceoModelModePanel.hidden, true);
    assert.equal(app.U.ceoComposerUsageBrain.getAttribute("aria-expanded"), "false");
});

test("模型链为空时渲染空态提示", () => {
    const app = loadApp();
    mountControl(app, { chain: [] });
    app.openCeoModelModePanel();

    assert.equal(app.U.ceoModelChainEmpty.hidden, false);
    assert.equal(app.U.ceoModelChainList.innerHTML, "");
});

test("拖拽换位只改草稿并亮出应用按钮", () => {
    const app = loadApp();
    mountControl(app);
    app.openCeoModelModePanel();

    // 把第一项拖到第三项之前。
    app.S.ceoModelSelection = { ...app.S.ceoModelSelection, dragFrom: 0, dropIndex: 2 };
    app.finishCeoModelChainDrag({ preventDefault() {} });

    assert.deepEqual(Array.from(app.S.ceoModelSelection.chainKeys), ["beta", "alpha", "gamma"]);
    assert.equal(app.S.ceoModelSelection.dragFrom, -1);
    assert.equal(app.ceoModelChainDirty(), true);
    assert.equal(app.U.ceoModelChainActions.hidden, false);
    // 未点应用前不发请求。
    assert.deepEqual(callsOf(app), []);
});

test("拖到原位置不产生草稿差异", () => {
    const app = loadApp();
    mountControl(app);
    app.openCeoModelModePanel();

    app.S.ceoModelSelection = { ...app.S.ceoModelSelection, dragFrom: 0, dropIndex: 1 };
    app.finishCeoModelChainDrag({ preventDefault() {} });

    assert.equal(app.ceoModelChainDirty(), false);
    assert.equal(app.U.ceoModelChainActions.hidden, true);
    assert.deepEqual(callsOf(app), []);
});

test("点击应用按草稿顺序提交模型链并回到干净状态", async () => {
    const app = loadApp();
    mountControl(app);
    app.bindCeoModelModeControls();
    app.openCeoModelModePanel();

    app.S.ceoModelSelection = { ...app.S.ceoModelSelection, dragFrom: 0, dropIndex: 2 };
    app.finishCeoModelChainDrag({ preventDefault() {} });
    const handlers = app.U.ceoModelChainApply.listeners.click || [];
    assert.equal(handlers.length, 1);
    handlers[0]();
    await new Promise((resolve) => setTimeout(resolve, 0));

    assert.deepEqual(callsOf(app).map((call) => [call[0], call[1], call[2].modelKeys]), [
        ["chain", "ceo", ["beta", "alpha", "gamma"]],
    ]);
    assert.deepEqual(Array.from(app.ceoModelChainKeys()), ["beta", "alpha", "gamma"]);
    assert.equal(app.ceoModelChainDirty(), false);
    assert.equal(app.U.ceoModelChainActions.hidden, true);
});

test("模型链保存失败回到服务端顺序", async () => {
    const app = loadApp({
        updateModelRoleChain: async (scope, payload) => {
            app.calls.push(["chain", scope, payload]);
            throw new Error("chain_save_failed");
        },
    });
    mountControl(app);
    app.openCeoModelModePanel();
    app.S.ceoModelSelection = { ...app.S.ceoModelSelection, dragFrom: 2, dropIndex: 0 };

    app.finishCeoModelChainDrag({ preventDefault() {} });
    await app.saveCeoModelChain(app.S.ceoModelSelection.chainKeys);

    assert.deepEqual(Array.from(app.S.ceoModelSelection.chainKeys), ["alpha", "beta", "gamma"]);
    assert.equal(app.S.ceoModelSelection.error, "chain_save_failed");
    assert.equal(app.ceoModelChainDirty(), false);
});

test("模型链草稿来自目录 roles.ceo", () => {
    const app = loadApp();
    mountControl(app, { chain: ["gamma", "alpha"] });

    assert.deepEqual(Array.from(app.ceoModelChainKeys()), ["gamma", "alpha"]);
});

test("展示名优先级为 name > key > provider_model", () => {
    const app = loadApp();
    assert.equal(app.ceoModelDisplayTitle({ name: "  ", key: "k", provider_model: "p" }), "k");
    assert.equal(app.ceoModelDisplayTitle({ key: "", provider_model: "p" }), "p");
    assert.equal(app.ceoModelDisplayTitle(null), "");
});