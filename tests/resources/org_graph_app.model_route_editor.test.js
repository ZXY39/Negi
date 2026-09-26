const test = require("node:test");
const assert = require("node:assert/strict");

// vm 里造出来的对象带着另一个 realm 的 Object 原型，deepStrictEqual 会比原型；
// 跨 realm 的结构断言统一走 JSON 文本比较。
const sameJson = (actual, expected, message) => assert.equal(JSON.stringify(actual), JSON.stringify(expected), message);
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

// 模型链编辑器对「负载均衡组」的契约：route_entries 的读取与回写、组卡片文案、
// 以及只有被链引用的组才会被提交。

const ROOT = path.resolve(__dirname, "..", "..");
const APP_CODE = fs.readFileSync(path.join(ROOT, "g3ku/web/frontend/org_graph_app.js"), "utf8");
const API_CODE = fs.readFileSync(path.join(ROOT, "g3ku/web/frontend/api_client.js"), "utf8");

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
            createElement: () => ({}),
            addEventListener: () => {},
            body: {},
        },
        Element: function Element() {},
        HTMLElement: function HTMLElement() {},
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

function loadApp() {
    const context = baseContext();
    context.window = context;
    vm.createContext(context);
    vm.runInContext(
        `${APP_CODE}\nthis.__exports = {
            S,
            applyModelCatalog,
            buildModelRoleChainUpdates,
            chainToRouteEntries,
            chainUsesGroup,
            groupRefToken,
            isGroupRef,
            normalizeModelRoleChain,
            renderModelGroupEntryCard,
            cloneLoadBalanceGroups,
            startModelRoleEditing,
            draftGroupForWrite,
        };`,
        context,
    );
    return context.__exports;
}

function loadApiClient() {
    const context = baseContext();
    context.window = context;
    vm.createContext(context);
    vm.runInContext(`${API_CODE}\nthis.__ApiClient = ApiClient;`, context);
    return context.__ApiClient;
}

function catalogPayload() {
    return {
        items: [
            { key: "m_a", enabled: true, provider_model: "openai:x" },
            { key: "m_b", enabled: true, provider_model: "openai:x" },
            { key: "m_emergency", enabled: true, provider_model: "openai:y" },
        ],
        roles: { ceo: ["m_emergency"], execution: ["m_a", "m_b"], inspection: ["m_b"], memory: [] },
        route_entries: {
            ceo: [{ type: "model", model_key: "m_emergency" }],
            execution: [
                { type: "load_balance", group_key: "g_shared" },
                { type: "model", model_key: "m_emergency" },
            ],
            inspection: [{ type: "model", model_key: "m_b" }],
            memory: [],
        },
        load_balance_groups: {
            g_shared: { enabled: true, max_retry_rounds: 2, model_keys: ["m_a", "m_b"] },
            g_unused: { enabled: true, max_retry_rounds: 9, model_keys: ["m_a"] },
        },
        roleIterations: { ceo: 40, execution: 16, inspection: 16, memory: 8 },
        roleConcurrency: { ceo: null, execution: null, inspection: null, memory: 1 },
    };
}

test("route_entries 被读成链，组以 group: 记号占位", () => {
    const app = loadApp();
    app.applyModelCatalog(catalogPayload(), { preserveRoleDrafts: false });

    sameJson(app.S.modelCatalog.roles.execution, ["group:g_shared", "m_emergency"]);
    assert.equal(app.chainUsesGroup(app.S.modelCatalog.roles.execution), true);
    assert.equal(app.S.modelCatalog.loadBalanceGroups.g_shared.max_retry_rounds, 2);
    // 越界的组预算在读入时就按上限归一，避免保存时被后端拒绝却看不出来。
    assert.equal(app.S.modelCatalog.loadBalanceGroups.g_unused.max_retry_rounds, 3);
    // 没有组的链仍是纯模型 key 列表，旧行为不变。
    sameJson(app.S.modelCatalog.roles.inspection, ["m_b"]);
});

test("含组的链按 route_entries 回写，纯模型链继续用 modelKeys", () => {
    const app = loadApp();
    app.applyModelCatalog(catalogPayload(), { preserveRoleDrafts: false });

    const updates = app.buildModelRoleChainUpdates(["execution", "inspection"], { useDrafts: false });

    sameJson(updates.execution.route_entries ?? updates.execution.routeEntries, [
        { type: "load_balance", group_key: "g_shared" },
        { type: "model", model_key: "m_emergency" },
    ]);
    assert.equal(updates.execution.model_keys, undefined);
    // 只提交被链引用的组，界面上未使用的草稿组不能顺手写盘。
    sameJson(Object.keys(updates.execution.loadBalanceGroups ?? updates.execution.load_balance_groups), ["g_shared"]);
    sameJson(updates.inspection.modelKeys, ["m_b"]);
    assert.equal(updates.inspection.routeEntries, undefined);
});

test("api_client 只发一套链，且组定义随链一起原子提交", () => {
    const ApiClient = loadApiClient();
    const body = ApiClient._toRoleRouteBody({
        modelKeys: ["m_a"],
        routeEntries: [{ type: "load_balance", group_key: "g_shared" }],
        loadBalanceGroups: { g_shared: { model_keys: ["m_a"] } },
    });

    sameJson(body.route_entries, [{ type: "load_balance", group_key: "g_shared" }]);
    assert.deepEqual(body.routeEntries, body.route_entries);
    assert.equal(body.model_keys, undefined);
    sameJson(body.load_balance_groups, { g_shared: { model_keys: ["m_a"] } });
    assert.deepEqual(body.loadBalanceGroups, body.load_balance_groups);

    const legacy = ApiClient._toRoleRouteBody(["m_a", "m_b"]);
    sameJson(legacy.model_keys, ["m_a", "m_b"]);
    assert.equal(legacy.route_entries, undefined);
});

test("组卡片说明「平级 + 按负载 + 粘滞」，并暴露空成员风险", () => {
    const app = loadApp();
    app.applyModelCatalog(catalogPayload(), { preserveRoleDrafts: false });

    const markup = app.renderModelGroupEntryCard("execution", "g_shared", 0, false);
    assert.match(markup, /组内平级/);
    assert.match(markup, /按综合负载选成员/);
    assert.match(markup, /节点绑定后粘滞/);
    assert.match(markup, /每成员 2 轮/);
    assert.doesNotMatch(markup, /成员为空/);

    const empty = app.renderModelGroupEntryCard("execution", "g_missing", 1, false);
    assert.match(empty, /成员为空，保存会被拒绝/);
});

test("编辑模式下的组成员增删只改草稿，不动已保存状态", () => {
    const app = loadApp();
    app.applyModelCatalog(catalogPayload(), { preserveRoleDrafts: false });
    // 直接摆出编辑态：startModelRoleEditing 会触发整页渲染，需要真实 DOM。
    app.S.modelCatalog.roleEditing = true;
    app.S.modelCatalog.loadBalanceGroupDrafts = app.cloneLoadBalanceGroups(app.S.modelCatalog.loadBalanceGroups);

    const group = app.draftGroupForWrite("g_shared");
    group.model_keys = ["m_a"];

    sameJson(app.S.modelCatalog.loadBalanceGroups.g_shared.model_keys, ["m_a", "m_b"]);
    const updates = app.buildModelRoleChainUpdates(["execution"], { useDrafts: true });
    const submitted = updates.execution.loadBalanceGroups ?? updates.execution.load_balance_groups;
    sameJson(submitted.g_shared.model_keys, ["m_a"]);
});
