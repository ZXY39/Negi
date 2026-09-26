const test = require("node:test");
const assert = require("node:assert/strict");

// vm 里造出来的对象带着另一个 realm 的 Object 原型，deepStrictEqual 会比原型；
// 跨 realm 的结构断言统一走 JSON 文本比较。
const sameJson = (actual, expected, message) => assert.equal(JSON.stringify(actual), JSON.stringify(expected), message);
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

// 模型页对「负载均衡组」的契约：route_entries 的读取与回写、左侧组配置列的渲染与增删改、
// 拖进链的 group 记号，以及提交时带上整份组集合（而不是只有被链引用的那份）。

const ROOT = path.resolve(__dirname, "..", "..");
const APP_CODE = fs.readFileSync(path.join(ROOT, "g3ku/web/frontend/org_graph_app.js"), "utf8");
const API_CODE = fs.readFileSync(path.join(ROOT, "g3ku/web/frontend/api_client.js"), "utf8");

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

function loadApp(elements = {}) {
    const context = baseContext(elements);
    context.window = context;
    vm.createContext(context);
    vm.runInContext(
        `${APP_CODE}\nthis.__exports = {
            S,
            U,
            applyModelCatalog,
            buildModelRoleChainUpdates,
            chainToRouteEntries,
            chainUsesGroup,
            createLoadBalanceGroupDraft,
            deleteLoadBalanceGroupDraft,
            draftGroupForWrite,
            groupRefToken,
            isGroupRef,
            normalizeModelRoleChain,
            renderModelGroupColumn,
            renderModelGroupEntryCard,
            renameLoadBalanceGroupKey,
            setLoadBalanceGroupRounds,
            startModelRoleEditing,
            toggleLoadBalanceGroupMember,
            cloneLoadBalanceGroups,
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

function catalogItems() {
    // provider_model 各不相同：目录按签名去重，同 provider+同模型会被并成一条别名。
    return [
        { key: "m_a", enabled: true, provider_model: "openai:model-a" },
        { key: "m_b", enabled: true, provider_model: "openai:model-b" },
        { key: "m_emergency", enabled: true, provider_model: "openai:model-c" },
    ];
}

function catalogPayload() {
    const catalog = catalogItems();
    return {
        catalog,
        items: catalog.map((item) => item.key),
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

// 没有任何链用到组的那份配置：新建的组要靠「承载 scope」才能落盘。
function plainChainPayload() {
    const payload = catalogPayload();
    payload.route_entries.execution = [{ type: "model", model_key: "m_a" }, { type: "model", model_key: "m_b" }];
    return payload;
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
    // 提交整份组集合：配置侧按 scope 整份替换 loadBalanceGroups，交子集会删掉没被这条链
    // 用上的组（包括刚建好还没拖进链的）。
    sameJson(Object.keys(updates.execution.loadBalanceGroups ?? updates.execution.load_balance_groups), ["g_shared", "g_unused"]);
    // 只有 execution 用到组，纯模型链的 inspection 不带组载荷。
    assert.equal(updates.inspection.loadBalanceGroups, undefined);
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

function stubElement(extra = {}) {
    return {
        innerHTML: "",
        textContent: "",
        value: "",
        hidden: true,
        disabled: false,
        style: {},
        classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
        querySelector: () => null,
        querySelectorAll: () => [],
        addEventListener() {},
        appendChild() {},
        insertBefore() {},
        remove() {},
        closest: () => null,
        getBoundingClientRect: () => ({ top: 0, bottom: 0, left: 0, right: 0, width: 0, height: 0 }),
        ...extra,
    };
}

// 组列的渲染与增删改都要走整页渲染，所以按模型页真实存在的容器铺一份桩。
function modelPageElements() {
    return {
        "sidebar-model-hint": stubElement(),
        "model-group-list": stubElement(),
        "model-group-create-btn": stubElement(),
        "model-role-editors": stubElement(),
        "model-role-limits-bar": stubElement(),
        "model-roles-save-btn": stubElement(),
        "model-roles-cancel-btn": stubElement(),
        "model-search-input": stubElement(),
        "model-list": stubElement(),
        "model-detail-empty": stubElement(),
        "model-detail-content": stubElement(),
    };
}

function loadModelPage(payload = catalogPayload()) {
    const elements = modelPageElements();
    const app = loadApp(elements);
    app.applyModelCatalog(payload, { preserveRoleDrafts: false });
    return { app, elements };
}

test("组列列出所有组并标出未加入链的那份", () => {
    const { app, elements } = loadModelPage();

    app.renderModelGroupColumn();
    const rows = elements["model-group-list"].innerHTML.split("<article").filter(Boolean);
    const sharedRow = rows.find((row) => row.includes('data-model-group-key="g_shared"'));
    const unusedRow = rows.find((row) => row.includes('data-model-group-key="g_unused"'));

    assert.ok(sharedRow && unusedRow, "两份组都要出现在列里");
    // 引用状态逐行判：g_shared 在 execution 链上，g_unused 没有。
    assert.match(sharedRow, /已在 1 条链/);
    assert.match(unusedRow, /未加入链/);
    // 非编辑态：不可拖、复选框禁用，避免「看起来能点其实改了不生效」。
    assert.doesNotMatch(sharedRow, /draggable="true"/);
    assert.match(sharedRow, /<input type="checkbox"[^>]*disabled/);
    // 成员清单来自模型目录，逐条一个复选框。
    assert.equal(sharedRow.match(/<input type="checkbox"/g).length, 3);
});

test("新建组进入编辑会话，填上成员后才进载荷", () => {
    const { app } = loadModelPage(plainChainPayload());

    const groupKey = app.createLoadBalanceGroupDraft();

    assert.equal(app.S.modelCatalog.roleEditing, true);
    assert.equal(groupKey, "group_1");
    sameJson(app.S.modelCatalog.loadBalanceGroupDrafts.group_1.model_keys, []);

    // 空成员组不提交：后端会拒，而且它也没被任何链引用。
    let updates = app.buildModelRoleChainUpdates(["execution", "inspection"], { useDrafts: true });
    assert.doesNotMatch(JSON.stringify(updates), /group_1/);

    assert.equal(app.toggleLoadBalanceGroupMember(groupKey, "m_a", true), true);
    updates = app.buildModelRoleChainUpdates(["execution", "inspection"], { useDrafts: true });
    // 没有任何链用到组时，整份组集合挂在第一个能承载组的 scope（execution）上提交。
    sameJson(Object.keys(updates.execution.loadBalanceGroups), ["g_shared", "g_unused", "group_1"]);
    sameJson(updates.execution.modelKeys, ["m_a", "m_b"]);
    assert.equal(updates.execution.routeEntries, undefined);
    assert.equal(updates.inspection.loadBalanceGroups, undefined);
});

test("改组名会把链上的 group 记号一起改掉", () => {
    const { app } = loadModelPage();
    app.S.modelCatalog.roleEditing = true;
    app.S.modelCatalog.roleDrafts = { ceo: ["m_emergency"], execution: ["group:g_shared", "m_emergency"], inspection: ["m_b"], memory: [] };
    app.S.modelCatalog.loadBalanceGroupDrafts = app.cloneLoadBalanceGroups(app.S.modelCatalog.loadBalanceGroups);

    assert.equal(app.renameLoadBalanceGroupKey("g_shared", "g_fast"), true);

    sameJson(app.S.modelCatalog.roleDrafts.execution, ["group:g_fast", "m_emergency"]);
    assert.equal(app.S.modelCatalog.loadBalanceGroupDrafts.g_shared, undefined);
    sameJson(app.S.modelCatalog.loadBalanceGroupDrafts.g_fast.model_keys, ["m_a", "m_b"]);
    // 轮预算跟着组走，不因为改名被重置。
    assert.equal(app.S.modelCatalog.loadBalanceGroupDrafts.g_fast.max_retry_rounds, 2);
});

test("改组名撞名或撞模型 key 时拒绝，链保持原样", () => {
    const { app } = loadModelPage();
    app.S.modelCatalog.roleEditing = true;
    app.S.modelCatalog.roleDrafts = { ceo: [], execution: ["group:g_shared"], inspection: [], memory: [] };
    app.S.modelCatalog.loadBalanceGroupDrafts = app.cloneLoadBalanceGroups(app.S.modelCatalog.loadBalanceGroups);

    assert.equal(app.renameLoadBalanceGroupKey("g_shared", "g_unused"), false);
    assert.equal(app.renameLoadBalanceGroupKey("g_shared", "m_a"), false);
    assert.equal(app.renameLoadBalanceGroupKey("g_shared", "  "), false);

    sameJson(app.S.modelCatalog.roleDrafts.execution, ["group:g_shared"]);
    assert.equal(app.S.modelCatalog.loadBalanceGroupDrafts.g_shared.max_retry_rounds, 2);
});

test("删除组会把引用它的链位一起摘掉", () => {
    const { app } = loadModelPage();
    app.S.modelCatalog.roleEditing = true;
    app.S.modelCatalog.roleDrafts = { ceo: [], execution: ["group:g_shared", "m_emergency"], inspection: ["m_b"], memory: [] };
    app.S.modelCatalog.loadBalanceGroupDrafts = app.cloneLoadBalanceGroups(app.S.modelCatalog.loadBalanceGroups);

    assert.equal(app.deleteLoadBalanceGroupDraft("g_shared"), true);

    sameJson(app.S.modelCatalog.roleDrafts.execution, ["m_emergency"]);
    assert.equal(app.S.modelCatalog.loadBalanceGroupDrafts.g_shared, undefined);
    // 链里再也没有组：这条 scope 回到扁平 modelKeys 载荷。
    const updates = app.buildModelRoleChainUpdates(["execution"], { useDrafts: true });
    assert.equal(updates.execution.routeEntries, undefined);
    sameJson(updates.execution.modelKeys, ["m_emergency"]);
});

test("组预算下拉只接受 1..3", () => {
    const { app } = loadModelPage();
    app.S.modelCatalog.roleEditing = true;
    app.S.modelCatalog.loadBalanceGroupDrafts = app.cloneLoadBalanceGroups(app.S.modelCatalog.loadBalanceGroups);

    app.setLoadBalanceGroupRounds("g_shared", "9");
    assert.equal(app.S.modelCatalog.loadBalanceGroupDrafts.g_shared.max_retry_rounds, 3);

    app.setLoadBalanceGroupRounds("g_shared", "0");
    assert.equal(app.S.modelCatalog.loadBalanceGroupDrafts.g_shared.max_retry_rounds, 1);
});
