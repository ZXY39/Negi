const test = require("node:test");
const assert = require("node:assert/strict");
const vm = require("node:vm");
const { readFrontendSources } = require("./support/frontend_sources.js");

// 模型页角色链状态：目录应用（签名去重 + 旧 key 改名映射）、route_entries 顺序优先、
// 编辑中刷新目录保留草稿、非编辑刷新回填、选中模型被删后清空。

const APP_CODE = readFrontendSources(["app"]).join("\n");

class StubElement {}
class StubHTMLElement extends StubElement {
    constructor() {
        super();
        this.hidden = false;
        this.disabled = false;
        this.textContent = "";
        this.innerHTML = "";
        this.dataset = {};
        this.classList = { add() {}, remove() {}, contains: () => false, toggle() {} };
        this.style = {};
    }
    setAttribute() {}
    querySelector() { return null; }
    querySelectorAll() { return []; }
    appendChild(child) { return child; }
    addEventListener() {}
}

function baseContext() {
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
            body: new StubHTMLElement(),
        },
        Element: StubElement,
        HTMLElement: StubHTMLElement,
        URLSearchParams,
        URL,
        AbortController,
        fetch: async () => ({ ok: true, json: async () => ({}) }),
        lucide: { createIcons() {} },
        ApiClient: {},
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

function loadRoleStore() {
    const context = baseContext();
    context.window = context;
    vm.createContext(context);
    vm.runInContext(
        `${APP_CODE}\nthis.__testExports = {
            S,
            applyModelCatalog,
            routeTokenChainFromEntries,
            normalizeModelRoleChain,
            modelRolesEqual,
            syncModelRoleDraftState,
        };`,
        context,
    );
    return context.__testExports;
}

function resetCatalog(state) {
    state.roles = { ceo: [], execution: [], inspection: [], memory: [] };
    state.roleDrafts = { ceo: [], execution: [], inspection: [], memory: [] };
    state.roleIterations = { ceo: null, execution: null, inspection: null, memory: null };
    state.roleIterationDrafts = { ceo: null, execution: null, inspection: null, memory: null };
    state.roleConcurrency = { ceo: null, execution: null, inspection: null, memory: 1 };
    state.roleConcurrencyDrafts = { ceo: null, execution: null, inspection: null, memory: 1 };
    state.loadBalanceGroups = {};
    state.loadBalanceGroupDrafts = {};
    state.roleEditing = false;
    state.rolesDirty = false;
    state.selectedModelKey = "";
    state.mode = "";
}

test("route_entries 决定顺序，组记号与改名映射各归各的", () => {
    const api = loadRoleStore();
    resetCatalog(api.S.modelCatalog);
    api.applyModelCatalog({
        catalog: [
            { key: "new-key", provider_model: "p1", api_key: "k1", api_base: "u1" },
            { key: "old-key", provider_model: "p1", api_key: "k1", api_base: "u1" },
        ],
        route_entries: {
            ceo: [
                { type: "load_balance", group_key: "grp" },
                { type: "model", model_key: "old-key" },
            ],
        },
        roles: { ceo: ["old-key"] },
        load_balance_groups: { grp: { model_keys: ["new-key"], max_retry_rounds: 2 } },
    });
    const state = api.S.modelCatalog;
    assert.equal(state.catalog.length, 1, "same signature must dedupe");
    assert.equal(state.catalog[0].key, "new-key");
    // route_entries 是服务端给出的最终顺序：模型 key 原样保留，只有旧的 `roles`
    // 回退路径才走 alias 改名映射。
    assert.equal(state.roles.ceo.length, 2);
    assert.equal(state.roles.ceo[0], "group:grp");
    assert.equal(state.roles.ceo[1], "old-key");

    api.applyModelCatalog({
        catalog: [
            { key: "new-key", provider_model: "p1", api_key: "k1", api_base: "u1" },
            { key: "old-key", provider_model: "p1", api_key: "k1", api_base: "u1" },
        ],
        roles: { ceo: ["old-key"] },
    });
    assert.equal(api.S.modelCatalog.roles.ceo[0], "new-key", "roles fallback must remap aliases");
    assert.equal(api.routeTokenChainFromEntries([
        { type: "load_balance", groupKey: "g2" },
        { type: "model", modelKey: "m2" },
    ]).join(","), "group:g2,m2");
});

test("编辑中刷新目录保留角色草稿，只重算 dirty", () => {
    const api = loadRoleStore();
    const state = api.S.modelCatalog;
    resetCatalog(state);
    state.roleEditing = true;
    state.roles = { ceo: ["orig"], execution: [], inspection: [], memory: [] };
    state.roleDrafts = { ceo: ["draft"], execution: [], inspection: [], memory: [] };
    api.applyModelCatalog({ roles: { ceo: ["server"] } }, { preserveRoleDrafts: true });
    assert.equal(state.roles.ceo[0], "server");
    assert.equal(state.roleDrafts.ceo[0], "draft");
    assert.equal(state.roleEditing, true);
    assert.equal(state.rolesDirty, true);
});

test("非编辑刷新把服务端角色回填成草稿并清 dirty", () => {
    const api = loadRoleStore();
    const state = api.S.modelCatalog;
    resetCatalog(state);
    state.roleEditing = true;
    state.rolesDirty = true;
    state.roleDrafts = { ceo: ["draft"], execution: [], inspection: [], memory: [] };
    api.applyModelCatalog({ roles: { ceo: ["server"] } }, { preserveRoleDrafts: false });
    assert.equal(state.roleEditing, false);
    assert.equal(state.rolesDirty, false);
    assert.equal(state.roleDrafts.ceo[0], "server");
});

test("选中的模型从目录消失后清空选中", () => {
    const api = loadRoleStore();
    const state = api.S.modelCatalog;
    resetCatalog(state);
    state.selectedModelKey = "gone";
    api.applyModelCatalog({ catalog: [], roles: {} });
    assert.equal(state.selectedModelKey, "");
});

test("角色归一化去重且等价比较只看内容", () => {
    const api = loadRoleStore();
    const normalized = api.normalizeModelRoleChain([" a ", "a", "", "b"]);
    assert.equal(normalized.length, 2);
    assert.equal(normalized[0], "a");
    assert.equal(normalized[1], "b");
    assert.equal(api.modelRolesEqual(
        { ceo: ["a"], execution: [], inspection: [], memory: [] },
        { ceo: ["a"], execution: [], inspection: [], memory: [] },
    ), true);
    assert.equal(api.modelRolesEqual(
        { ceo: ["a"], execution: [], inspection: [], memory: [] },
        { ceo: ["b"], execution: [], inspection: [], memory: [] },
    ), false);
});
