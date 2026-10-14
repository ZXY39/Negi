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
            const modelKeys = Array.isArray(payload.model_keys) ? [...payload.model_keys] : [];
            return {
                ok: true,
                session_id: sessionId,
                mode: payload.mode,
                model_key: payload.model_key || "",
                model_keys: modelKeys,
                is_session_chain: modelKeys.length > 0,
                chain_unavailable_keys: [],
                pinned_available: true,
            };
        },
        // 面板打开会重读模型目录（全局链可能已被别处改过），默认桩与 mountControl 的目录一致。
        getOrgGraphModels: async () => ({
            catalog: CATALOG.map((item) => ({ ...item })),
            roles: { ceo: ["alpha", "beta", "gamma"], execution: [], inspection: [], memory: [] },
        }),
        // 故意不提供 updateModelRoleChain：脑图标面板的链保存只准打会话端点，
        // 谁写回全局角色链就会在这里直接炸掉。
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
            ceoModelBadgeTitle,
            ceoModelChainKeys,
            ceoModelChainBaseline,
            ceoModelChainDraft,
            appendCeoModelChainMember,
            removeCeoModelChainMember,
            enterCeoModelChainEdit,
            stageCeoModelChainGlobalSync,
            beginCeoModelChainDrag,
            modelChainMemberKeys,
            modelChainMixedImageCapability,
            modelChainImageIncapableNames,
            chainsWithImageMix,
            finishCeoModelChainDrag,
            bindCeoModelModeControls,
            ceoModelChainDirty,
            showCeoModelChainPane,
            cancelCeoModelChainSwitch,
            confirmCeoModelChainSwitch,
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
// 开面板会触发一次 GET；断言“没有写入”时只看写操作。
const writesOf = (app) => callsOf(app).filter((call) => call[0] === "patch" || call[0] === "chain");

function mountControl(app, { readonly = false, chain = ["alpha", "beta", "gamma"] } = {}) {
    const { U, S } = app;
    S.activeSessionId = "web:test";
    S.ceoSessions = [{ session_id: "web:test", is_readonly: readonly }];
    U.ceoComposerUsageBrain = new StubHTMLElement();
    U.ceoModelModePanel = new StubHTMLElement();
    U.ceoModelModeBadge = new StubHTMLElement();
    U.ceoModelModeUsageFill = new StubHTMLElement();
    U.ceoModelModeUsageText = new StubHTMLElement();
    U.ceoModelModeChain = new StubHTMLElement();
    U.ceoModelModePinned = new StubHTMLElement();
    U.ceoModelChainPane = new StubHTMLElement();
    U.ceoModelChainHint = new StubHTMLElement();
    U.ceoModelChainList = new StubHTMLElement();
    U.ceoModelChainEmpty = new StubHTMLElement();
    U.ceoModelChainActions = new StubHTMLElement();
    U.ceoModelChainEdit = new StubHTMLElement();
    U.ceoModelChainReset = new StubHTMLElement();
    U.ceoModelChainAdd = new StubHTMLElement();
    U.ceoModelChainApply = new StubHTMLElement();
    U.ceoModelChainConfirm = new StubHTMLElement();
    U.ceoModelChainConfirmText = new StubHTMLElement();
    U.ceoModelChainConfirmAccept = new StubHTMLElement();
    U.ceoModelChainConfirmCancel = new StubHTMLElement();
    U.ceoModelPicker = new StubHTMLElement();
    U.ceoModelPickerSearch = new StubHTMLElement();
    U.ceoModelPickerList = new StubHTMLElement();
    U.ceoModelPickerEmpty = new StubHTMLElement();
    U.ceoModelModeNote = new StubHTMLElement();
    // 「同步全局模型链」会重读模型目录，而 renderModelCatalog 的提示行没有 null 守卫。
    U.modelHint = new StubHTMLElement();
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
    // 胶囊不再当模式指示灯：它只在面板打开时按当前生效模型刷新。
    assert.equal(app.U.ceoModelModeBadge.textContent, "");
    assert.equal(app.U.ceoComposerUsageBrain.getAttribute("aria-expanded"), "false");
    assert.equal(app.U.ceoComposerUsageBrain.classList.contains("is-panel-open"), false);
});

test("打开面板默认只读：列出正在生效的链，只给一颗编辑", () => {
    const app = loadApp();
    mountControl(app);
    app.openCeoModelModePanel();

    assert.equal(app.U.ceoModelModePanel.hidden, false);
    assert.equal(app.U.ceoComposerUsageBrain.getAttribute("aria-expanded"), "true");
    assert.equal(app.U.ceoComposerUsageBrain.classList.contains("is-panel-open"), true);
    // 模型链模式显示链面板、隐藏指定模型列表。
    assert.equal(app.U.ceoModelChainPane.hidden, false);
    assert.equal(app.U.ceoModelPicker.hidden, true);
    // 只读态的行：只有配置名，按服务端顺序渲染，没有把手、没有叉号、不可拖。
    const markup = app.U.ceoModelChainList.innerHTML;
    assert.equal((markup.match(/class="ceo-model-chain-title"/g) || []).length, 3);
    assert.equal((markup.match(/resource-list-subtitle|policy-chip/g) || []).length, 0);
    assert.ok(markup.indexOf("Alpha 配置") < markup.indexOf("beta"));
    assert.ok(markup.indexOf("beta") < markup.indexOf("Gamma"));
    assert.equal((markup.match(/class="ceo-model-chain-grip"/g) || []).length, 0);
    assert.equal((markup.match(/class="ceo-model-chain-remove"/g) || []).length, 0);
    assert.equal(markup.includes('draggable="true"'), false);
    // 拖动说明只在编辑态讲。
    assert.equal(app.U.ceoModelChainHint.hidden, true);
    // 只读态的动作行只有「编辑」，其余三颗都不露面。
    assert.equal(app.U.ceoModelChainActions.hidden, false);
    assert.equal(app.U.ceoModelChainEdit.hidden, false);
    assert.equal(app.U.ceoModelChainEdit.disabled, false);
    assert.equal(app.U.ceoModelChainReset.hidden, true);
    assert.equal(app.U.ceoModelChainAdd.hidden, true);
    assert.equal(app.U.ceoModelChainApply.hidden, true);
});

test("点编辑进入编辑态：三点动作、行可拖可删、保存先禁用", () => {
    const app = loadApp();
    mountControl(app);
    app.openCeoModelModePanel();
    app.enterCeoModelChainEdit();

    const markup = app.U.ceoModelChainList.innerHTML;
    assert.equal((markup.match(/class="ceo-model-chain-grip"/g) || []).length, 3);
    assert.equal((markup.match(/class="ceo-model-chain-remove"/g) || []).length, 3);
    assert.equal((markup.match(/draggable="true"/g) || []).length, 3);
    assert.equal(app.U.ceoModelChainHint.hidden, false);
    assert.equal(app.U.ceoModelChainEdit.hidden, true);
    assert.equal(app.U.ceoModelChainReset.hidden, false);
    assert.equal(app.U.ceoModelChainAdd.hidden, false);
    assert.equal(app.U.ceoModelChainApply.hidden, false);
    // 编辑态刚进来时草稿＝正在生效的链，所以「保存」禁用而不是整颗消失。
    assert.equal(app.ceoModelChainDirty(), false);
    assert.equal(app.U.ceoModelChainApply.disabled, true);
    // 进编辑不发任何写请求。
    assert.deepEqual(writesOf(app), []);
});

test("只读态不给拖：拖拽起手直接被拒", () => {
    const app = loadApp();
    mountControl(app);
    app.openCeoModelModePanel();
    app.enterCeoModelChainEdit();
    // 先证编辑态能起拖，再把状态放回只读。
    const card = new StubHTMLElement();
    card.dataset = { ceoChainIndex: "0" };
    card.closest = () => card;
    app.beginCeoModelChainDrag({ target: card, dataTransfer: { setData() {} } });
    assert.equal(app.S.ceoModelSelection.dragFrom, 0);

    app.S.ceoModelSelection = { ...app.S.ceoModelSelection, chainEditing: false, dragFrom: -1 };
    app.beginCeoModelChainDrag({ target: card, dataTransfer: { setData() {} } });
    assert.equal(app.S.ceoModelSelection.dragFrom, -1);
});

test("固定模型后链面板换成指定模型列表并显示固定标签", () => {
    const app = loadApp();
    mountControl(app);
    app.applyCeoModelSelectionPayload("web:test", { mode: "model", model_key: "alpha", pinned_available: true });
    app.openCeoModelModePicker();

    assert.equal(app.U.ceoModelChainPane.hidden, true);
    assert.equal(app.U.ceoModelPicker.hidden, false);
    assert.equal(app.U.ceoModelModePinned.getAttribute("aria-checked"), "true");
    // 胶囊直接显示正在使用的模型配置名，不再带「会话固定 ·」前缀。
    assert.equal(app.U.ceoModelModeBadge.textContent, "Alpha 配置");
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
    // 既无预估也无固定项时胶囊回落到占位态。
    assert.equal(app.U.ceoModelModeBadge.textContent, "等待 Leader 上下文预估");
    assert.equal(app.U.ceoModelModeBadge.classList.contains("is-pending"), true);
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

    assert.equal(app.U.ceoModelModeBadge.textContent, "Alpha 配置");
    // 进度条下方只留 token 占用值，模型名不再重复一遍。
    assert.equal(app.U.ceoModelModeUsageText.hidden, false);
    assert.equal(app.U.ceoModelModeUsageText.textContent, "12000/390000 TOKEN");
});

test("没有上下文预估时进度条下方整行隐藏", () => {
    const app = loadApp();
    mountControl(app);
    app.openCeoModelModePanel();
    app.S.ceoComposerUsageEstimate = null;

    app.syncCeoModelModePanelUsage();

    assert.equal(app.U.ceoModelModeUsageText.hidden, true);
    assert.equal(app.U.ceoModelModeUsageText.textContent, "");
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

test("会话切换后真正拉取服务端模式而不是复用默认态", async () => {
    const app = loadApp();
    mountControl(app);
    app.resetCeoModelSelection("web:other");

    const loaded = await app.refreshCeoModelSelection("web:other");

    assert.deepEqual(callsOf(app), [["get", "web:other"]]);
    assert.equal(loaded.sessionId, "web:other");
    assert.equal(app.S.ceoModelSelection.loaded, true);
});

test("渠道会话同样可以打开面板并读取模式", async () => {
    const app = loadApp();
    mountControl(app, { readonly: true });

    app.openCeoModelModePanel();
    const loaded = await app.refreshCeoModelSelection("web:test");

    assert.equal(app.U.ceoModelModePanel.hidden, false);
    assert.equal(app.U.ceoComposerUsageBrain.getAttribute("aria-expanded"), "true");
    assert.deepEqual(callsOf(app), [["get", "web:test"]]);
    assert.equal(loaded.mode, "chain");
});

test("无激活会话时不打开面板", () => {
    const app = loadApp();
    mountControl(app);
    app.S.activeSessionId = "";

    app.openCeoModelModePanel();

    assert.equal(app.S.ceoModelSelection.panelOpen, false);
    assert.deepEqual(writesOf(app), []);
});

test("打开面板停在正在使用的板块：固定生效直接进指定模型列表", () => {
    const app = loadApp();
    mountControl(app);
    app.applyCeoModelSelectionPayload("web:test", { mode: "model", model_key: "alpha", pinned_available: true });

    app.openCeoModelModePanel();

    assert.equal(app.U.ceoModelPicker.hidden, false);
    assert.equal(app.U.ceoModelChainPane.hidden, true);
});

test("面板打开后到达的固定态把板块自动切到指定模型", () => {
    const app = loadApp();
    mountControl(app);
    app.openCeoModelModePanel();
    // 打开瞬间还没有固定信息，先显示模型链板块。
    assert.equal(app.U.ceoModelChainPane.hidden, false);

    app.applyCeoModelSelectionPayload("web:test", { mode: "model", model_key: "alpha", pinned_available: true });

    assert.equal(app.U.ceoModelPicker.hidden, false);
    assert.equal(app.U.ceoModelChainPane.hidden, true);
});

test("点模型链先出确认条，勾了才切换", async () => {
    const app = loadApp();
    mountControl(app);
    app.bindCeoModelModeControls();
    app.applyCeoModelSelectionPayload("web:test", { mode: "model", model_key: "alpha", pinned_available: true });
    app.openCeoModelModePanel();

    app.U.ceoModelModeChain.listeners.click[0]();
    assert.equal(app.U.ceoModelChainPane.hidden, false);
    assert.equal(app.U.ceoModelChainConfirm.hidden, false);
    assert.match(app.U.ceoModelChainConfirmText.textContent, /切换到模型链/);
    // 还没确认，不发写请求。
    assert.deepEqual(writesOf(app), []);

    app.U.ceoModelChainConfirmAccept.listeners.click[0]();
    await new Promise((resolve) => setTimeout(resolve, 0));

    assert.deepEqual(writesOf(app), [["patch", "web:test", { mode: "chain" }]]);
    assert.equal(app.U.ceoModelChainConfirm.hidden, true);
    assert.equal(app.U.ceoModelModeChain.getAttribute("aria-checked"), "true");
});

test("确认条点叉取消，回到正在使用的固定板块", () => {
    const app = loadApp();
    mountControl(app);
    app.bindCeoModelModeControls();
    app.applyCeoModelSelectionPayload("web:test", { mode: "model", model_key: "alpha", pinned_available: true });
    app.openCeoModelModePanel();

    app.U.ceoModelModeChain.listeners.click[0]();
    app.U.ceoModelChainConfirmCancel.listeners.click[0]();

    assert.deepEqual(writesOf(app), []);
    assert.equal(app.U.ceoModelChainConfirm.hidden, true);
    assert.equal(app.U.ceoModelModePinned.getAttribute("aria-checked"), "true");
    assert.equal(app.U.ceoModelPicker.hidden, false);
    assert.equal(app.U.ceoModelChainPane.hidden, true);
});

test("本来就是模型链时点模型链不出确认条", () => {
    const app = loadApp();
    mountControl(app);
    app.openCeoModelModePanel();

    app.showCeoModelChainPane();

    assert.equal(app.U.ceoModelChainConfirm.hidden, true);
    assert.equal(app.U.ceoModelChainPane.hidden, false);
    assert.deepEqual(writesOf(app), []);
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

test("拖拽换位只改草稿并亮出保存按钮", () => {
    const app = loadApp();
    mountControl(app);
    app.openCeoModelModePanel();
    app.enterCeoModelChainEdit();

    // 把第一项拖到第三项之前。
    app.S.ceoModelSelection = { ...app.S.ceoModelSelection, dragFrom: 0, dropIndex: 2 };
    app.finishCeoModelChainDrag({ preventDefault() {} });

    assert.deepEqual(Array.from(app.S.ceoModelSelection.chainKeys), ["beta", "alpha", "gamma"]);
    assert.equal(app.S.ceoModelSelection.dragFrom, -1);
    assert.equal(app.ceoModelChainDirty(), true);
    assert.equal(app.U.ceoModelChainActions.hidden, false);
    assert.equal(app.U.ceoModelChainApply.disabled, false);
    // 未点保存前不发写请求。
    assert.deepEqual(writesOf(app), []);
});

test("拖到原位置不产生草稿差异", () => {
    const app = loadApp();
    mountControl(app);
    app.openCeoModelModePanel();
    app.enterCeoModelChainEdit();

    app.S.ceoModelSelection = { ...app.S.ceoModelSelection, dragFrom: 0, dropIndex: 1 };
    app.finishCeoModelChainDrag({ preventDefault() {} });

    assert.equal(app.ceoModelChainDirty(), false);
    assert.equal(app.U.ceoModelChainApply.disabled, true);
    assert.deepEqual(writesOf(app), []);
});

test("点保存把会话链提交到本会话，不写全局角色链，并回到只读态", async () => {
    const app = loadApp();
    mountControl(app);
    app.bindCeoModelModeControls();
    app.openCeoModelModePanel();
    app.U.ceoModelChainEdit.listeners.click[0]({ stopPropagation() {} });

    app.S.ceoModelSelection = { ...app.S.ceoModelSelection, dragFrom: 0, dropIndex: 2 };
    app.finishCeoModelChainDrag({ preventDefault() {} });
    const handlers = app.U.ceoModelChainApply.listeners.click || [];
    assert.equal(handlers.length, 1);
    handlers[0]();
    for (let i = 0; i < 6; i += 1) await new Promise((resolve) => setTimeout(resolve, 0));

    // 只打会话端点：全局角色链接口一次都没被调用，writesOf 里不该出现 "chain"。
    assert.deepEqual(writesOf(app).map((call) => [call[0], call[1], call[2].mode, call[2].model_keys]), [
        ["patch", "web:test", "chain", ["beta", "alpha", "gamma"]],
    ]);
    assert.deepEqual(Array.from(app.ceoModelChainBaseline()), ["beta", "alpha", "gamma"]);
    assert.deepEqual(Array.from(app.ceoModelChainKeys()), ["alpha", "beta", "gamma"]);
    assert.equal(app.ceoModelChainDirty(), false);
    // 落盘成功即退出编辑态：只读的那一眼必须是没有未保存改动的链。
    assert.equal(app.S.ceoModelSelection.chainEditing, false);
    assert.equal(app.U.ceoModelChainEdit.hidden, false);
    assert.equal(app.U.ceoModelChainApply.hidden, true);
    assert.equal(app.U.ceoModelModeChain.textContent, "模型链（此会话）");
});

test("会话链保存失败回到生效中的那条链", async () => {
    const app = loadApp({
        updateCeoSessionModelSelection: async (sessionId, payload) => {
            app.calls.push(["patch", sessionId, payload]);
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

test("会话链非空时基线用会话链，全局链只当种子", () => {
    const app = loadApp();
    mountControl(app, { chain: ["alpha", "beta"] });

    app.applyCeoModelSelectionPayload("web:test", { mode: "chain", model_keys: ["beta", "alpha"] });
    assert.equal(app.S.ceoModelSelection.isSessionChain, true);
    assert.deepEqual(Array.from(app.ceoModelChainBaseline()), ["beta", "alpha"]);
    assert.deepEqual(Array.from(app.S.ceoModelSelection.chainKeys), ["beta", "alpha"]);
    assert.equal(app.ceoModelChainDirty(), false);

    // 恢复全局链：会话链清空后基线回到 roles.ceo。
    app.applyCeoModelSelectionPayload("web:test", { mode: "chain", model_keys: [] });
    assert.equal(app.S.ceoModelSelection.isSessionChain, false);
    assert.deepEqual(Array.from(app.ceoModelChainBaseline()), ["alpha", "beta"]);
});

test("混链判据按整条链展开成员，并点名不支持图片的配置", () => {
    const app = loadApp();
    mountControl(app);
    app.S.modelCatalog.catalog = [
        { key: "mm1", name: "收图一", provider_model: "openai:m1", enabled: true, image_multimodal_enabled: true },
        { key: "mm2", name: "", provider_model: "openai:m2", enabled: true, imageMultimodalEnabled: true },
        { key: "plain", name: "不收图一", provider_model: "openai:p", enabled: true, image_multimodal_enabled: false },
        { key: "plain2", name: "", provider_model: "openai:p2", enabled: true, image_multimodal_enabled: false },
    ];
    app.S.modelCatalog.loadBalanceGroups = {
        g_mix: { enabled: true, max_retry_rounds: 1, model_keys: ["mm1", "plain"] },
    };

    assert.equal(app.modelChainMixedImageCapability(["mm1", "plain"]), true);
    assert.equal(app.modelChainMixedImageCapability(["plain", "mm1"]), true);
    assert.equal(app.modelChainMixedImageCapability(["mm1", "mm2"]), false);
    assert.equal(app.modelChainMixedImageCapability(["mm1"]), false);
    // 全链都不收图不算"混"：那条链本来也看不了图，这次保存没让它失去什么。
    assert.equal(app.modelChainMixedImageCapability(["plain", "plain2"]), false);
    // 目录里查不到的成员不参与判定：运行时不会把它发出去。
    assert.equal(app.modelChainMixedImageCapability(["mm1", "gone"]), false);

    // 组槽位展开成成员：混在组里与混在链上后果相同。
    assert.deepEqual(Array.from(app.modelChainMemberKeys(["group:g_mix"])), ["mm1", "plain"]);
    assert.equal(app.modelChainMixedImageCapability(["group:g_mix"]), true);
    assert.deepEqual(Array.from(app.modelChainImageIncapableNames(["group:g_mix", "plain2"])), ["不收图一", "plain2"]);
    // 两条链都混 ⇒ 成员名并成一个列表，只弹一次。
    assert.deepEqual(Array.from(app.chainsWithImageMix([["mm1", "plain"], ["mm1", "plain2"]])), ["不收图一", "plain2"]);
    assert.deepEqual(Array.from(app.chainsWithImageMix([["plain", "plain2"]])), []);
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

test("徽标按绑定 key 解析实跑模型，provider 模型名撞名不串到别的绑定", () => {
    const app = loadApp();
    mountControl(app, { chain: ["alpha", "beta"] });
    // 两条绑定共用同一个 provider 模型名：只按 provider_model 解析会命中 key 相同的那条。
    app.S.modelCatalog.catalog = [
        { key: "beta", name: "实跑绑定", provider_model: "shared-model", enabled: true },
        { key: "alpha", name: "撞名绑定", provider_model: "shared-model", enabled: true },
    ];
    assert.equal(
        app.ceoModelBadgeTitle({
            provider_model: "shared-model",
            resolved_model_key: "beta",
        }),
        "实跑绑定",
    );
    // 读数里没有绑定 key 时仍按 provider 模型名解析。
    assert.equal(app.ceoModelBadgeTitle({ provider_model: "alpha" }), "撞名绑定");
});
function clickChainRowControl(app, attributeName, value) {
    const control = new StubHTMLElement();
    control.attributes = { [attributeName]: value };
    control.getAttribute = (name) => (Object.prototype.hasOwnProperty.call(control.attributes, name) ? control.attributes[name] : null);
    control.closest = (selector) => (selector.includes(attributeName) ? control : null);
    const handlers = app.U.ceoModelChainList.listeners?.click || [];
    assert.ok(handlers.length >= 1, "链列表应绑定点击处理");
    let stopped = false;
    handlers[handlers.length - 1]({ target: control, stopPropagation: () => { stopped = true; } });
    return stopped;
}

test("点行上的移除只改草稿，不提前发写请求", () => {
    const app = loadApp();
    mountControl(app);
    app.bindCeoModelModeControls();
    app.openCeoModelModePanel();
    app.enterCeoModelChainEdit();

    // 移除同样会重画列表，必须截断冒泡，否则整个面板会被关掉。
    assert.equal(clickChainRowControl(app, "data-ceo-chain-remove", "beta"), true);

    assert.deepEqual(Array.from(app.ceoModelChainDraft()), ["alpha", "gamma"]);
    assert.equal(app.ceoModelChainDirty(), true);
    assert.equal(app.U.ceoModelChainApply.disabled, false);
    assert.deepEqual(writesOf(app), []);
});

test("加入模型走真实点击：截断冒泡、回到链栏、只改草稿", () => {
    const app = loadApp();
    mountControl(app, { chain: ["alpha"] });
    app.bindCeoModelModeControls();
    app.openCeoModelModePanel();
    app.U.ceoModelChainEdit.listeners.click[0]({ stopPropagation() {} });
    app.S.ceoModelSelection = { ...app.S.ceoModelSelection, chainKeys: ["alpha"] };
    app.openCeoModelModePicker("append");

    const markup = app.U.ceoModelPickerList.innerHTML;
    // 已在链里的不再列出，否则同一成员能被加两遍。
    assert.equal(markup.includes('data-ceo-model-pick="alpha"'), false);
    assert.equal(markup.includes('data-ceo-model-pick="beta"'), true);
    assert.equal((markup.match(/data-lucide="plus"/g) || []).length, 2);

    const item = new StubHTMLElement();
    item.attributes = { "data-ceo-model-pick": "beta" };
    item.getAttribute = (name) => (Object.prototype.hasOwnProperty.call(item.attributes, name) ? item.attributes[name] : null);
    item.closest = (selector) => (selector.includes("data-ceo-model-pick") ? item : null);
    let stopped = false;
    const handlers = app.U.ceoModelPickerList.listeners.click || [];
    handlers[handlers.length - 1]({ target: item, stopPropagation: () => { stopped = true; } });

    // 处理会重画列表，事件目标随即脱离文档；不截断冒泡就会被「点了面板外」那条全局判定
    // 关掉整个面板，加进去的成员也跟着被草稿重置掉。
    assert.equal(stopped, true);
    assert.equal(app.S.ceoModelSelection.pickerOpen, false);
    assert.deepEqual(Array.from(app.ceoModelChainDraft()), ["alpha", "beta"]);
    assert.equal(app.ceoModelChainDirty(), true);
    assert.equal(app.S.ceoModelSelection.mode, "chain");
    assert.deepEqual(writesOf(app), []);
});

test("删空会话链后保存恢复全局模型链", async () => {
    const app = loadApp();
    mountControl(app);
    app.S.ceoModelSelection = {
        ...app.S.ceoModelSelection,
        sessionChainKeys: ["alpha", "beta", "gamma"],
        isSessionChain: true,
        chainKeys: [],
    };

    await app.saveCeoModelChain([]);

    // 空草稿与「同步全局模型链」走同一个写：不带 model_keys，后端摘掉会话链键。
    assert.deepEqual(writesOf(app).map((call) => [call[0], call[2]]), [["patch", { mode: "chain" }]]);
    assert.equal(app.S.ceoModelSelection.isSessionChain, false);
});

test("点同步全局模型链把服务端最新那条全局链拉成底稿，一个写请求都不发", async () => {
    const app = loadApp({
        // 目录里此刻的全局链与面板缓存的那份不同：同步必须重读，不能拿旧缓存铺列表。
        getOrgGraphModels: async () => ({
            catalog: CATALOG.map((item) => ({ ...item })),
            roles: { ceo: ["gamma", "beta"], execution: [], inspection: [], memory: [] },
        }),
    });
    mountControl(app);
    app.bindCeoModelModeControls();
    app.applyCeoModelSelectionPayload("web:test", { mode: "chain", model_keys: ["beta", "alpha"] });
    app.openCeoModelModePanel();
    app.U.ceoModelChainEdit.listeners.click[0]({ stopPropagation() {} });
    assert.deepEqual(Array.from(app.ceoModelChainDraft()), ["beta", "alpha"]);

    app.U.ceoModelChainReset.listeners.click[0]({ stopPropagation() {} });
    for (let i = 0; i < 6; i += 1) await new Promise((resolve) => setTimeout(resolve, 0));

    // 列表铺的是刚拉到的全局链，不是空列表、也不是那份旧缓存。
    assert.deepEqual(Array.from(app.ceoModelChainDraft()), ["gamma", "beta"]);
    assert.equal(app.U.ceoModelChainList.innerHTML.includes("Gamma"), true);
    assert.equal(app.U.ceoModelChainEmpty.hidden, true);
    assert.equal(app.ceoModelChainDirty(), true);
    assert.equal(app.U.ceoModelChainApply.disabled, false);
    // 同步只改草稿：服务端一条写都没收到。
    assert.deepEqual(writesOf(app), []);
});

test("同步全局模型链后直接保存＝摘掉会话链继续跟随全局", async () => {
    const app = loadApp({
        getOrgGraphModels: async () => ({
            catalog: CATALOG.map((item) => ({ ...item })),
            roles: { ceo: ["gamma", "beta"], execution: [], inspection: [], memory: [] },
        }),
    });
    mountControl(app);
    app.bindCeoModelModeControls();
    app.applyCeoModelSelectionPayload("web:test", { mode: "chain", model_keys: ["beta", "alpha"] });
    app.openCeoModelModePanel();
    app.U.ceoModelChainEdit.listeners.click[0]({ stopPropagation() {} });
    app.U.ceoModelChainReset.listeners.click[0]({ stopPropagation() {} });
    for (let i = 0; i < 6; i += 1) await new Promise((resolve) => setTimeout(resolve, 0));
    assert.equal(app.U.ceoModelModeChain.textContent, "模型链（此会话）");

    app.U.ceoModelChainApply.listeners.click[0]();
    for (let i = 0; i < 6; i += 1) await new Promise((resolve) => setTimeout(resolve, 0));

    // 草稿与全局链逐位相同 ⇒ 不再存一份副本：发不带 model_keys 的 chain 载荷，后端摘键。
    assert.deepEqual(writesOf(app).map((call) => [call[1], call[2]]), [["web:test", { mode: "chain" }]]);
    assert.equal(app.S.ceoModelSelection.isSessionChain, false);
    assert.equal(app.S.ceoModelSelection.chainEditing, false);
    assert.equal(app.U.ceoModelModeChain.textContent, "模型链（全局）");
    assert.deepEqual(Array.from(app.ceoModelChainDraft()), ["gamma", "beta"]);
});

test("同步成全局底稿后再动一手，保存才落这条会话链", async () => {
    const app = loadApp({
        getOrgGraphModels: async () => ({
            catalog: CATALOG.map((item) => ({ ...item })),
            roles: { ceo: ["gamma", "beta"], execution: [], inspection: [], memory: [] },
        }),
    });
    mountControl(app);
    app.bindCeoModelModeControls();
    app.openCeoModelModePanel();
    app.U.ceoModelChainEdit.listeners.click[0]({ stopPropagation() {} });
    app.U.ceoModelChainReset.listeners.click[0]({ stopPropagation() {} });
    for (let i = 0; i < 6; i += 1) await new Promise((resolve) => setTimeout(resolve, 0));
    assert.deepEqual(Array.from(app.ceoModelChainDraft()), ["gamma", "beta"]);
    // 这个会话本来就跟随全局：同步回来的底稿就是正在生效的那条，没有改动可保存。
    assert.equal(app.ceoModelChainDirty(), false);
    assert.equal(app.U.ceoModelChainApply.disabled, true);

    // 在底稿上删掉一位：草稿不再等于全局链，保存必须落显式 keys。
    clickChainRowControl(app, "data-ceo-chain-remove", "beta");
    app.U.ceoModelChainApply.listeners.click[0]();
    for (let i = 0; i < 6; i += 1) await new Promise((resolve) => setTimeout(resolve, 0));

    assert.deepEqual(writesOf(app).map((call) => [call[2].mode, call[2].model_keys]), [["chain", ["gamma"]]]);
    assert.equal(app.S.ceoModelSelection.isSessionChain, true);
    assert.equal(app.U.ceoModelModeChain.textContent, "模型链（此会话）");
});

test("打开面板重读模型目录：只读态显示此刻的全局链而不是上次加载的快照", async () => {
    const app = loadApp({
        // 服务端这条链已经换了头：面板必须显示新链，而不是本页加载时那份缓存。
        getOrgGraphModels: async () => ({
            catalog: CATALOG.map((item) => ({ ...item })),
            roles: { ceo: ["beta", "alpha"], execution: [], inspection: [], memory: [] },
        }),
    });
    mountControl(app);
    assert.deepEqual(Array.from(app.ceoModelChainKeys()), ["alpha", "beta", "gamma"]);

    app.openCeoModelModePanel();
    for (let i = 0; i < 6; i += 1) await new Promise((resolve) => setTimeout(resolve, 0));

    assert.deepEqual(Array.from(app.ceoModelChainDraft()), ["beta", "alpha"]);
    assert.equal(app.S.ceoModelSelection.chainEditing, false);
    assert.equal(app.ceoModelChainDirty(), false);
    assert.deepEqual(writesOf(app), []);
});

test("目录重读落地时不盖掉编辑中的草稿", async () => {
    let release;
    const gate = new Promise((resolve) => { release = resolve; });
    const app = loadApp({
        getOrgGraphModels: async () => {
            await gate;
            return {
                catalog: CATALOG.map((item) => ({ ...item })),
                roles: { ceo: ["gamma", "beta"], execution: [], inspection: [], memory: [] },
            };
        },
    });
    mountControl(app);
    app.openCeoModelModePanel();
    app.enterCeoModelChainEdit();
    app.removeCeoModelChainMember("gamma");

    release();
    for (let i = 0; i < 6; i += 1) await new Promise((resolve) => setTimeout(resolve, 0));

    // 目录已经换成新链，但编辑中的草稿一个字都不动。
    assert.deepEqual(Array.from(app.ceoModelChainKeys()), ["gamma", "beta"]);
    assert.deepEqual(Array.from(app.ceoModelChainDraft()), ["alpha", "beta"]);
    assert.equal(app.S.ceoModelSelection.chainEditing, true);
    assert.equal(app.ceoModelChainDirty(), true);
    assert.deepEqual(writesOf(app), []);
});

test("页签标出这条链是谁的：没会话链时跟随全局，存了会话链才标此会话", () => {
    const app = loadApp();
    mountControl(app);
    app.openCeoModelModePanel();
    assert.equal(app.U.ceoModelModeChain.textContent, "模型链（全局）");

    app.applyCeoModelSelectionPayload("web:test", { mode: "chain", model_keys: ["gamma"] });
    assert.equal(app.U.ceoModelModeChain.textContent, "模型链（此会话）");

    app.applyCeoModelSelectionPayload("web:test", { mode: "chain", model_keys: [] });
    assert.equal(app.U.ceoModelModeChain.textContent, "模型链（全局）");
});

test("点编辑不关面板：面板内控件都截断冒泡", () => {
    const app = loadApp();
    mountControl(app);
    app.bindCeoModelModeControls();
    app.openCeoModelModePanel();

    for (const ref of ["ceoModelChainEdit", "ceoModelChainReset", "ceoModelChainAdd"]) {
        const handlers = app.U[ref].listeners.click || [];
        assert.equal(handlers.length, 1, `${ref} 应绑定一次点击`);
        let stopped = false;
        handlers[0]({ stopPropagation: () => { stopped = true; } });
        assert.equal(stopped, true, `${ref} 必须截断冒泡，否则面板会被全局判定关掉`);
    }
});

test("切回模型链板块不丢掉刚加入的草稿", () => {
    const app = loadApp();
    mountControl(app, { chain: ["alpha", "gamma"] });
    app.bindCeoModelModeControls();
    app.openCeoModelModePanel();
    app.enterCeoModelChainEdit();
    app.openCeoModelModePicker("append");

    app.appendCeoModelChainMember("beta");
    assert.equal(app.S.ceoModelSelection.pickerOpen, false, "加完应回到链那一栏");
    assert.equal(app.S.ceoModelSelection.chainEditing, true, "加成员是编辑态的动作，不该被踢回只读");

    // 用户再手动点一次板块，也不能把刚加的成员冲掉。
    app.showCeoModelChainPane();
    assert.ok(app.ceoModelChainDraft().includes("beta"));
    assert.equal(app.ceoModelChainDirty(), true);
    assert.equal(app.U.ceoModelChainApply.disabled, false);
});
