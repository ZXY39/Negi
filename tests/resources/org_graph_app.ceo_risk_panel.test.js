const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const GOVERNANCE_PATH = "g3ku/web/frontend/org_graph_ceo_governance.js";
const GOVERNANCE_CODE = fs.readFileSync(GOVERNANCE_PATH, "utf8");

class StubClassList {
    constructor() {
        this.names = new Set();
    }

    add(name) { this.names.add(name); }
    remove(name) { this.names.delete(name); }
    contains(name) { return this.names.has(name); }

    toggle(name, on) {
        const next = on === undefined ? !this.names.has(name) : !!on;
        if (next) this.names.add(name);
        else this.names.delete(name);
        return next;
    }
}

class StubElement {
    constructor(id = "") {
        this.id = id;
        this.dataset = {};
        this.attrs = {};
        this.listeners = {};
        this.classList = new StubClassList();
        this.hidden = false;
        this.disabled = false;
        this.checked = false;
        this.textContent = "";
        this.innerHTML = "";
        this.value = "";
        this.parentElement = null;
        this.style = { setProperty() {} };
        this._children = [];
    }

    setAttribute(name, value) { this.attrs[name] = String(value); }
    getAttribute(name) { return name in this.attrs ? this.attrs[name] : null; }
    focus() {}

    addEventListener(type, handler) {
        if (!this.listeners[type]) this.listeners[type] = [];
        this.listeners[type].push(handler);
    }

    dispatch(type, extra = {}) {
        const event = { type, target: this, stopPropagation() {}, ...extra };
        (this.listeners[type] || []).forEach((handler) => handler(event));
        return event;
    }

    closest(selector) {
        return selector === `#${this.id}` || selector === `.${this.id}` ? this : null;
    }

    querySelectorAll() { return []; }
}

class StubBody {
    constructor() {
        this.children = [];
    }

    appendChild(node) {
        this.children.push(node);
        node.parentElement = this;
    }
}

class StubDocument {
    constructor(ids) {
        this.byId = new Map(ids.map((id) => [id, new StubElement(id)]));
        this.listeners = {};
        this.created = [];
        this.body = new StubBody();
    }

    getElementById(id) { return this.byId.get(id) || null; }
    createElement() { return new StubElement(); }
    querySelector() { return null; }
    querySelectorAll() { return []; }

    addEventListener(type, handler) {
        if (!this.listeners[type]) this.listeners[type] = [];
        this.listeners[type].push(handler);
    }

    dispatch(type, event = {}) {
        (this.listeners[type] || []).forEach((handler) => handler({ type, ...event }));
    }
}

function loadGovernance({ governanceItem, tools = [], extraIds = [] } = {}) {
    const panelIds = [
        "ceo-risk-btn",
        "ceo-risk-panel",
        "ceo-risk-mode-all",
        "ceo-risk-mode-conditional",
        "ceo-risk-whitelist-btn",
        "ceo-risk-whitelist-hint",
        "ceo-risk-exec-settings-btn",
        "ceo-risk-mode-note",
        "ceo-approval-viewport",
    ];
    const document = new StubDocument([...panelIds, ...extraIds]);
    // 工具详情抽屉那两个节点：就地弹出这条车道要把它们搬到 body 上。
    const toolBackdrop = new StubElement("tool-detail-backdrop");
    const toolDrawer = new StubElement("tool-detail-panel");
    document.byId.set("tool-detail-backdrop", toolBackdrop);
    document.querySelector = (selector) => (selector === ".tool-detail-panel" ? toolDrawer : null);
    const calls = [];
    const context = {
        console,
        setTimeout,
        clearTimeout,
        setInterval: () => 1,
        clearInterval: () => {},
        queueMicrotask,
        requestAnimationFrame: (callback) => { callback(); return 1; },
        cancelAnimationFrame: () => {},
        document,
        window: {},
        Element: StubElement,
        HTMLElement: StubElement,
        HTMLButtonElement: StubElement,
        HTMLInputElement: StubElement,
        HTMLTextAreaElement: StubElement,
        HTMLSelectElement: StubElement,
        CSS: { escape: (value) => String(value || "") },
        WebSocket: { OPEN: 1 },
        structuredClone: global.structuredClone,
        performance: { now: () => 0 },
        addEventListener() {},
        removeEventListener() {},
        S: { view: "ceo", ceoQueuedFollowUps: {}, ceoSessionHydrated: false, activeSessionId: "web:shared" },
        U: {},
        ApiClient: {
            getActiveSessionId: () => "web:shared",
            getToolGovernanceMode: async () => governanceItem,
            updateToolGovernanceMode: async (payload) => {
                calls.push({ api: "updateToolGovernanceMode", payload });
                return { ...(governanceItem || {}), ...(payload.enabled === false
                    ? { enabled: false, mode: "all_pass" }
                    : { enabled: true, mode: "conditional" }), whitelist_tools: payload.whitelistTools || governanceItem?.whitelist_tools || [] };
            },
            getTools: async () => { calls.push({ api: "getTools" }); return tools; },
            getExecCommandWhitelist: async () => ({ items: [], approval_wait_seconds: 120 }),
            listExecApprovals: async () => ({ items: [] }),
            getCeoPendingInterrupts: async () => [],
        },
        showToast: (options) => { calls.push({ api: "toast", options }); },
        openTool: (toolId) => { calls.push({ api: "openTool", toolId }); return Promise.resolve(); },
        switchView: (view) => { calls.push({ api: "switchView", view }); },
        canMutateCeoSessions: () => true,
        canCreateCeoSessions: () => true,
        canActivateCeoSessions: () => true,
        syncCeoPrimaryButton: () => {},
        sendCeoMessage: () => {},
        maybeDispatchQueuedCeoFollowUps: () => false,
        sendActiveCeoFollowUpsToRuntime: () => null,
        applyCeoState: () => {},
        resetCeoSessionState: () => {},
        activeSessionIsReadonly: () => false,
        activeSessionId: () => "web:shared",
        setDrawerOpen: () => {},
        esc: (value) => String(value || ""),
        displayRiskLabel: (value) => ({ low: "低风险", medium: "中风险", high: "高风险" })[value] || String(value),
        icons: () => {},
        __calls: calls,
    };
    context.window = context;
    vm.createContext(context);
    vm.runInContext(GOVERNANCE_CODE, context);
    context.elements = document.byId;
    context.calls = calls;
    context.toolBackdrop = toolBackdrop;
    context.toolDrawer = toolDrawer;
    return context;
}

async function settle() {
    await new Promise((resolve) => setTimeout(resolve, 20));
}

// vm 里造出来的对象与宿主 prototype 不同，node:assert/strict 的 deepEqual 会报
// "same structure but not reference-equal"，先过一遍 JSON 归一。
function plain(value) {
    return JSON.parse(JSON.stringify(value));
}

test("风险面板按服务端档位渲染，条件放行才出现白名单入口", async () => {
    const ctx = loadGovernance({
        governanceItem: {
            enabled: true,
            mode: "conditional",
            whitelist_tools: ["content_search"],
            whitelist_customized: true,
            updated_at: "2026-10-25T00:00:00Z",
        },
    });
    assert.equal(ctx.elements.get("ceo-risk-panel").hidden, true, "面板默认不开");

    ctx.elements.get("ceo-risk-btn").dispatch("click");
    assert.equal(ctx.elements.get("ceo-risk-panel").hidden, false);
    await settle();

    const all = ctx.elements.get("ceo-risk-mode-all");
    const conditional = ctx.elements.get("ceo-risk-mode-conditional");
    assert.equal(all.attrs["aria-checked"], "false");
    assert.equal(conditional.attrs["aria-checked"], "true");
    assert.equal(ctx.elements.get("ceo-risk-whitelist-btn").hidden, false);
    assert.match(ctx.elements.get("ceo-risk-whitelist-hint").textContent, /1 项免审/);
    assert.match(ctx.elements.get("ceo-risk-mode-note").textContent, /渠道会话没有审批界面/);
    // 黄色指的是"这一档不拦任何工具调用"：条件放行下按钮不着色。
    assert.equal(ctx.elements.get("ceo-risk-btn").classList.contains("is-all-pass"), false);

    // 全部放行那档没有免审名单可言，白名单入口收掉、按钮转黄。
    ctx.calls.length = 0;
    ctx.elements.get("ceo-risk-panel").dispatch("click", { target: all });
    await settle();
    assert.deepEqual(plain(ctx.calls.map((call) => call.payload)), [{ enabled: false }]);
    assert.equal(ctx.elements.get("ceo-risk-whitelist-btn").hidden, true);
    assert.equal(ctx.elements.get("ceo-risk-btn").classList.contains("is-all-pass"), true);
});

test("命令行风险拦截设置在会话页就地弹出 exec 工具详情", () => {
    const ctx = loadGovernance({
        governanceItem: { enabled: false, mode: "all_pass", whitelist_tools: [], whitelist_customized: false },
    });
    ctx.elements.get("ceo-risk-btn").dispatch("click");
    assert.equal(ctx.elements.get("ceo-risk-panel").hidden, false);

    ctx.elements.get("ceo-risk-panel").dispatch("click", { target: ctx.elements.get("ceo-risk-exec-settings-btn") });
    assert.deepEqual(plain(ctx.calls.filter((call) => call.api === "openTool")), [{ api: "openTool", toolId: "exec" }]);
    // 不许再把操作员带到 Tool 页：抽屉自己搬到 body 上。
    assert.equal(ctx.calls.some((call) => call.api === "switchView"), false, "点了设置不该切视图");
    assert.equal(ctx.toolBackdrop.parentElement, ctx.document.body);
    assert.equal(ctx.toolDrawer.parentElement, ctx.document.body);
    assert.equal(ctx.elements.get("ceo-risk-panel").hidden, true);
});

test("风险按钮与语音按钮同尺寸，命令行设置入口排在两个切换按钮之前", () => {
    const css = fs.readFileSync("g3ku/web/frontend/org_graph.css", "utf8");
    const iconRule = css.match(/#ceo-attach-btn svg,([^}]*)}/);
    assert.ok(iconRule, "composer 图标尺寸规则不见了");
    assert.match(iconRule[1], /#ceo-risk-btn svg/, "风险按钮的图标必须与附件/语音同一档尺寸");
    // 40px 控件档那条按 id 压过基础类，风险按钮不登记进去就会长成基础类的 58px。
    const controlTier = css.match(/#ceo-context-usage-brain,\s*#ceo-attach-btn,\s*#ceo-voice-btn,\s*#ceo-risk-btn \{([^}]*)}/);
    assert.ok(controlTier, "风险按钮没进输入区 40px 控件档");
    assert.match(controlTier[1], /width: 40px/);

    const html = fs.readFileSync("g3ku/web/frontend/org_graph.html", "utf8");
    const voiceAt = html.indexOf('id="ceo-voice-btn"');
    const riskAt = html.indexOf('id="ceo-risk-btn"');
    assert.ok(riskAt > voiceAt && riskAt - voiceAt < 400, "风险按钮要紧跟在语音按钮右边");
    const settingsAt = html.indexOf('id="ceo-risk-exec-settings-btn"');
    const modeAt = html.indexOf('id="ceo-risk-mode-all"');
    assert.ok(settingsAt > 0 && settingsAt < modeAt, "命令行风险拦截设置排在切换按钮上方");

    // 深色主题那条 .meta-tag blanket 会把风险色压成中性，两页与白名单弹窗都靠这几条恢复。
    assert.match(css, /\[data-theme="dark"\] \.meta-tag\.risk-low \{ color: #34d399; \}/);
    assert.match(css, /\[data-theme="dark"\] \.meta-tag\.risk-medium \{ color: #fbbf24; \}/);
    assert.match(css, /\[data-theme="dark"\] \.meta-tag\.risk-high \{ color: #f87171; \}/);
});

test("白名单弹窗按服务端名单画勾选，保存只提交勾上的那几项", async () => {
    const ctx = loadGovernance({
        governanceItem: {
            enabled: true,
            mode: "conditional",
            whitelist_tools: ["content_search"],
            whitelist_customized: false,
        },
        tools: [
            { tool_id: "content_search", display_name: "Content Search", risk_level: "low" },
            { tool_id: "exec", display_name: "Exec", risk_level: "high" },
        ],
        extraIds: [
            "ceo-risk-whitelist-backdrop",
            "ceo-risk-whitelist-dialog",
            "ceo-risk-whitelist-list",
            "ceo-risk-whitelist-save-btn",
        ],
    });
    ctx.elements.get("ceo-risk-btn").dispatch("click");
    await settle();
    ctx.elements.get("ceo-risk-panel").dispatch("click", { target: ctx.elements.get("ceo-risk-whitelist-btn") });
    await settle();

    const list = ctx.elements.get("ceo-risk-whitelist-list");
    assert.equal(ctx.elements.get("ceo-risk-whitelist-backdrop").hidden, false);
    assert.match(list.innerHTML, /data-tool-id="content_search"[^>]*checked/);
    assert.doesNotMatch(list.innerHTML, /data-tool-id="exec"[^>]*checked/);
    assert.match(list.innerHTML, /高风险/);

    // 用户只勾回 exec：勾上的集合按 DOM 现状读，不是按打开时的名单。
    const checked = new StubElement("exec-input");
    checked.checked = true;
    checked.setAttribute("data-tool-id", "exec");
    const unchecked = new StubElement("search-input");
    unchecked.checked = false;
    unchecked.setAttribute("data-tool-id", "content_search");
    list.querySelectorAll = () => [checked, unchecked];

    ctx.calls.length = 0;
    ctx.elements.get("ceo-risk-whitelist-dialog").dispatch("click", {
        target: ctx.elements.get("ceo-risk-whitelist-save-btn"),
    });
    await settle();

    const saved = ctx.calls.find((call) => call.api === "updateToolGovernanceMode");
    assert.deepEqual(plain(saved.payload), { whitelistTools: ["exec"] });
    assert.equal(ctx.elements.get("ceo-risk-whitelist-backdrop").hidden, true);
});

test("点面板外面收起风险面板，点面板内部不收起", async () => {
    const ctx = loadGovernance({
        governanceItem: { enabled: false, mode: "all_pass", whitelist_tools: [], whitelist_customized: false },
    });
    ctx.elements.get("ceo-risk-btn").dispatch("click");
    await settle();
    assert.equal(ctx.elements.get("ceo-risk-panel").hidden, false);

    ctx.elements.get("ceo-risk-panel").dispatch("click", { target: ctx.elements.get("ceo-risk-mode-conditional") });
    assert.equal(ctx.elements.get("ceo-risk-panel").hidden, false, "面板内的点击不该被收掉");

    ctx.document.dispatch("click", { target: new StubElement("ceo-feed") });
    assert.equal(ctx.elements.get("ceo-risk-panel").hidden, true);
});

function stubSandboxFetch(ctx, { mode, unlockRequired }) {
    const state = { mode };
    const patched = [];
    ctx.fetch = async (_url, options = {}) => {
        if (!options.method || options.method === "GET") {
            return {
                ok: true,
                json: async () => ({
                    ok: true,
                    item: {
                        session_id: "web:shared",
                        trust: unlockRequired ? "third_party_dm" : "owner",
                        mode: state.mode,
                        resolved_tier: state.mode === "restricted" ? "readonly" : "host_full",
                        unlock_required: unlockRequired,
                    },
                }),
            };
        }
        const body = JSON.parse(options.body);
        patched.push(body);
        state.mode = body.mode || state.mode;
        return {
            ok: true,
            json: async () => ({
                ok: true,
                item: {
                    session_id: "web:shared",
                    trust: unlockRequired ? "third_party_dm" : "owner",
                    mode: state.mode,
                    resolved_tier: state.mode === "restricted" ? "readonly" : "host_full",
                    unlock_required: unlockRequired,
                },
            }),
        };
    };
    return patched;
}

test("沙箱模式一行：按意图画开关，整行一点就地切档并通知沙箱页", async () => {
    const ctx = loadGovernance({
        governanceItem: { enabled: false, mode: "all_pass", whitelist_tools: [], whitelist_customized: false },
        extraIds: ["ceo-risk-sandbox-btn", "ceo-risk-sandbox-hint"],
    });
    const patched = stubSandboxFetch(ctx, { mode: "restricted", unlockRequired: false });
    const notified = [];
    ctx.notifySandboxSessionModeChanged = (sessionId) => notified.push(sessionId);

    ctx.elements.get("ceo-risk-btn").dispatch("click");
    await settle();
    const row = ctx.elements.get("ceo-risk-sandbox-btn");
    const hint = ctx.elements.get("ceo-risk-sandbox-hint");
    assert.equal(row.attrs["aria-pressed"], "true");
    assert.equal(hint.textContent, "只读模式（沙箱模式）");

    ctx.elements.get("ceo-risk-panel").dispatch("click", { target: row });
    await settle();
    assert.deepEqual(plain(patched), [{ mode: "unrestricted" }], "owner 切无限制不带口令");
    assert.equal(row.attrs["aria-pressed"], "false");
    assert.equal(hint.textContent, "无限制");
    assert.deepEqual(notified, ["web:shared"], "切完要通知沙箱 iframe 重读");
});

test("第三方会话关闭沙箱：先弹口令框，口令随 PATCH 一起提交", async () => {
    const ctx = loadGovernance({
        governanceItem: { enabled: false, mode: "all_pass", whitelist_tools: [], whitelist_customized: false },
        extraIds: ["ceo-risk-sandbox-btn", "ceo-risk-sandbox-hint"],
    });
    const patched = stubSandboxFetch(ctx, { mode: "restricted", unlockRequired: true });
    let dialog = null;
    ctx.openConfirm = (options) => { dialog = options; };

    ctx.elements.get("ceo-risk-btn").dispatch("click");
    await settle();
    const row = ctx.elements.get("ceo-risk-sandbox-btn");
    assert.equal(row.attrs["aria-pressed"], "true");

    ctx.elements.get("ceo-risk-panel").dispatch("click", { target: row });
    await settle();
    assert.equal(dialog?.title, "关闭沙箱模式？");
    assert.equal(dialog?.input?.type, "password");
    assert.deepEqual(plain(patched), [], "没验口令之前不发 PATCH");

    await dialog.onConfirm({ value: "live-test-pass" });
    assert.deepEqual(plain(patched), [{ mode: "unrestricted", unlock_password: "live-test-pass" }]);
    assert.equal(row.attrs["aria-pressed"], "false");
});
