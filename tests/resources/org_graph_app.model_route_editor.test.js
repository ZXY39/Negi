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
            createElement: () => ({ style: {} }),
            addEventListener: () => {},
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
            activeLoadBalanceGroups,
            closeLoadBalanceGroupDialog,
            confirmLoadBalanceGroupDialog,
            deleteLoadBalanceGroupDraft,
            groupRefToken,
            isGroupRef,
            normalizeModelRoleChain,
            openLoadBalanceGroupDialog,
            renderModelGroupChainTile,
            renderModelGroupColumn,
            renderModelGroupDialog,
            setLoadBalanceGroupDialogField,
            startModelRoleEditing,
            toggleLoadBalanceGroupDialogMember,
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
        { key: "m_a", name: "深度V4 快档", enabled: true, provider_model: "openai:model-a" },
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
    // 组预算不再夹取：大数是用户意图，只有负数会被后端拒。
    assert.equal(app.S.modelCatalog.loadBalanceGroups.g_unused.max_retry_rounds, 9);
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

test("链上的组卡说明「平级 + 按负载 + 粘滞」，并暴露空成员风险", () => {
    const app = loadApp();
    app.applyModelCatalog(catalogPayload(), { preserveRoleDrafts: false });

    const markup = app.renderModelGroupChainTile("execution", "g_shared", 0, false);
    assert.match(markup, /组内平级/);
    assert.match(markup, /按综合负载选成员/);
    assert.match(markup, /节点绑定后粘滞/);
    assert.match(markup, /data-model-chain-ref="group:g_shared"/);
    assert.doesNotMatch(markup, /成员为空/);
    // 结构必须和模型卡一致：live 的模型卡没有 handle 元素，多塞一个 40px 虚线把手会把
    // 主区挤成 0 宽，标题就会掉到卡片外面（他截图里那个又高又空的虚线框）。
    assert.doesNotMatch(markup, /model-chain-handle|model-chain-grip/);
    // 计数类文字（N 个成员 / 每成员 N 轮）按裁定不显示。
    assert.doesNotMatch(markup, /个成员|每成员 \d+ 轮/);
    // 整张卡都是打开配置的点击区。
    assert.match(markup, /<article[^>]*data-group-edit="g_shared"/);

    const empty = app.renderModelGroupChainTile("execution", "g_missing", 1, false);
    assert.match(empty, /成员为空，保存会被拒绝/);
});

function stubElement(extra = {}) {
    return {
        innerHTML: "",
        textContent: "",
        value: "",
        hidden: true,
        disabled: false,
        attributes: {},
        style: {},
        classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
        setAttribute(name, value) { this.attributes[name] = value; },
        getAttribute(name) { return this.attributes[name]; },
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

// 组列、组弹窗与角色链都要走整页渲染，所以按模型页真实存在的容器铺一份桩。
function modelPageElements() {
    return {
        "sidebar-model-hint": stubElement(),
        "model-group-list": stubElement(),
        "model-group-create-btn": stubElement(),
        "model-group-backdrop": stubElement(),
        "model-group-dialog": stubElement(),
        "model-group-dialog-body": stubElement(),
        "model-group-close-btn": stubElement(),
        "model-group-title": stubElement(),
        "model-group-cancel-btn": stubElement(),
        "model-group-confirm-btn": stubElement(),
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
    // 成员构成要能直接看见，不用先点开弹窗；显示的是备注，key 挂在 title 上。
    assert.match(sharedRow, /title="m_a">深度V4 快档</);
    assert.match(sharedRow, /title="m_b">m_b</);
    assert.doesNotMatch(sharedRow, /个成员|每成员 \d+ 轮/);
    // 整张卡是点击区（rows 是按 <article 切开的，这里只匹配属性）。
    assert.match(sharedRow, /data-group-edit="g_shared"/);
    // 非编辑态：不可拖、没有删除按钮。
    assert.doesNotMatch(sharedRow, /draggable="true"/);
    assert.doesNotMatch(sharedRow, /data-group-delete/);
});

test("新建组先弹窗，点确定才落草稿并自动进入链编辑会话", () => {
    const { app, elements } = loadModelPage(plainChainPayload());

    app.openLoadBalanceGroupDialog();
    const body = elements["model-group-dialog-body"].innerHTML;
    assert.match(body, /data-group-dialog-name/);
    // 重试次数是自填数字，默认 3，且不带 max（不设上限）。
    const roundsTag = (body.match(/<input[^>]*data-group-dialog-rounds[^>]*>/) || [null])[0];
    assert.ok(roundsTag, '重试次数必须是自填输入框');
    assert.match(roundsTag, /type="number"/);
    assert.match(roundsTag, /value="3"/);
    assert.doesNotMatch(roundsTag, /max=/);
    assert.doesNotMatch(body, /<select[^>]*data-group-dialog-rounds/);
    // 成员清单显示用户写的备注而不是 key，key 挂在 title 上供 hover 辨认。
    assert.match(body, /<span title="m_a">深度V4 快档<\/span>/);
    assert.match(body, /<span title="m_b">m_b<\/span>/);
    // 光打开弹窗不碰数据：既没进编辑会话，也没有新组草稿。
    assert.equal(app.S.modelCatalog.roleEditing, false);
    assert.equal(app.S.modelCatalog.loadBalanceGroupDrafts.g_stage, undefined);

    assert.equal(app.setLoadBalanceGroupDialogField("name", "g_stage"), true);
    assert.equal(app.toggleLoadBalanceGroupDialogMember("m_a", true), true);
    assert.equal(app.toggleLoadBalanceGroupDialogMember("m_b", true), true);
    assert.equal(app.setLoadBalanceGroupDialogField("rounds", "2"), true);
    assert.equal(app.confirmLoadBalanceGroupDialog(), true);

    assert.equal(app.S.modelCatalog.roleEditing, true);
    sameJson(app.S.modelCatalog.loadBalanceGroupDrafts.g_stage.model_keys, ["m_a", "m_b"]);
    assert.equal(app.S.modelCatalog.loadBalanceGroupDrafts.g_stage.max_retry_rounds, 2);
    assert.equal(app.S.modelCatalog.groupDialog.open, false);

    // 没有任何链用到组时，整份组集合挂在第一个能承载组的 scope（execution）上提交。
    const updates = app.buildModelRoleChainUpdates(["execution", "inspection"], { useDrafts: true });
    sameJson(Object.keys(updates.execution.loadBalanceGroups), ["g_shared", "g_unused", "g_stage"]);
    sameJson(updates.execution.modelKeys, ["m_a", "m_b"]);
    assert.equal(updates.execution.routeEntries, undefined);
    assert.equal(updates.inspection.loadBalanceGroups, undefined);
});

test("弹窗里空成员与空组名都被挡下，不落草稿", () => {
    const { app, elements } = loadModelPage(plainChainPayload());

    app.openLoadBalanceGroupDialog();
    assert.equal(app.confirmLoadBalanceGroupDialog(), false);
    assert.match(app.S.modelCatalog.groupDialog.error, /至少勾选 1 个模型/);

    app.toggleLoadBalanceGroupDialogMember("m_a", true);
    app.setLoadBalanceGroupDialogField("name", "   ");
    assert.equal(app.confirmLoadBalanceGroupDialog(), false);
    assert.match(app.S.modelCatalog.groupDialog.error, /组名不能为空/);

    assert.equal(app.S.modelCatalog.roleEditing, false);
    // 草稿仍是已保存那两份组，没有多出被拒的空白组。
    sameJson(Object.keys(app.S.modelCatalog.loadBalanceGroupDrafts).sort(), ["g_shared", "g_unused"]);
    assert.match(elements["model-group-dialog-body"].innerHTML, /组名不能为空/);
});

test("重试次数只挡负数，大数按用户意图保留", () => {
    const { app } = loadModelPage(plainChainPayload());

    app.openLoadBalanceGroupDialog();
    app.setLoadBalanceGroupDialogField("name", "g_rounds");
    app.toggleLoadBalanceGroupDialogMember("m_a", true);
    app.setLoadBalanceGroupDialogField("rounds", "-1");
    assert.equal(app.confirmLoadBalanceGroupDialog(), false);
    assert.match(app.S.modelCatalog.groupDialog.error, /不小于 0 的整数/);

    app.setLoadBalanceGroupDialogField("rounds", "9999");
    assert.equal(app.confirmLoadBalanceGroupDialog(), true);
    assert.equal(app.S.modelCatalog.loadBalanceGroupDrafts.g_rounds.max_retry_rounds, 9999);
});

test("弹窗改组名会把链上的 group 记号一起改掉", () => {
    const { app } = loadModelPage();
    app.S.modelCatalog.roleEditing = true;
    app.S.modelCatalog.roleDrafts = { ceo: ["m_emergency"], execution: ["group:g_shared", "m_emergency"], inspection: ["m_b"], memory: [] };
    app.S.modelCatalog.loadBalanceGroupDrafts = app.cloneLoadBalanceGroups(app.S.modelCatalog.loadBalanceGroups);

    app.openLoadBalanceGroupDialog("g_shared");
    app.setLoadBalanceGroupDialogField("name", "g_fast");
    assert.equal(app.confirmLoadBalanceGroupDialog(), true);

    sameJson(app.S.modelCatalog.roleDrafts.execution, ["group:g_fast", "m_emergency"]);
    assert.equal(app.S.modelCatalog.loadBalanceGroupDrafts.g_shared, undefined);
    sameJson(app.S.modelCatalog.loadBalanceGroupDrafts.g_fast.model_keys, ["m_a", "m_b"]);
    // 轮预算跟着组走，不因为改名被重置。
    assert.equal(app.S.modelCatalog.loadBalanceGroupDrafts.g_fast.max_retry_rounds, 2);
});

test("弹窗改组名撞名或撞模型 key 时拒绝，链保持原样", () => {
    const { app } = loadModelPage();
    app.S.modelCatalog.roleEditing = true;
    app.S.modelCatalog.roleDrafts = { ceo: [], execution: ["group:g_shared"], inspection: [], memory: [] };
    app.S.modelCatalog.loadBalanceGroupDrafts = app.cloneLoadBalanceGroups(app.S.modelCatalog.loadBalanceGroups);

    app.openLoadBalanceGroupDialog("g_shared");
    app.setLoadBalanceGroupDialogField("name", "g_unused");
    assert.equal(app.confirmLoadBalanceGroupDialog(), false);
    assert.match(app.S.modelCatalog.groupDialog.error, /组名已存在：g_unused/);

    app.setLoadBalanceGroupDialogField("name", "m_a");
    assert.equal(app.confirmLoadBalanceGroupDialog(), false);
    assert.match(app.S.modelCatalog.groupDialog.error, /不能与模型配置同名/);

    sameJson(app.S.modelCatalog.roleDrafts.execution, ["group:g_shared"]);
    assert.equal(app.S.modelCatalog.loadBalanceGroupDrafts.g_shared.max_retry_rounds, 2);
    assert.equal(app.S.modelCatalog.groupDialog.open, true);
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

const LLM_CODE = fs.readFileSync(path.join(ROOT, "g3ku/web/frontend/org_graph_llm.js"), "utf8");

// /api/llm/bindings 的形状：routes 是候选展开视图，顺序与组在另外两个字段里。
function llmBindingsPayload() {
    return {
        items: catalogItems().map((item) => ({ ...item, capability: "chat" })),
        routes: { ceo: [], execution: ["m_a", "m_b"], inspection: ["m_b"], memory: [] },
        route_entries: catalogPayload().route_entries,
        load_balance_groups: catalogPayload().load_balance_groups,
        role_iterations: {},
        role_concurrency: {},
    };
}

function loadModelPageWithLlm() {
    const elements = modelPageElements();
    elements["llm-bindings-list"] = stubElement();
    const context = baseContext(elements);
    context.window = context;
    vm.createContext(context);
    vm.runInContext(APP_CODE, context);
    vm.runInContext(LLM_CODE, context);
    const payload = llmBindingsPayload();
    context.__llmPayload = payload;
    vm.runInContext(`
        const state = __llmTestHooks.llmState();
        state.bindings = __llmPayload.items;
        state.routes = normalizeAllModelRoles(__llmTestHooks.llmRouteChains(__llmPayload));
        state.loadBalanceGroups = cloneLoadBalanceGroups(__llmPayload.load_balance_groups);
    `, context);
    return { context, elements };
}

test("llmRouteChains 用 route_entries 覆盖候选展开视图", () => {
    const { context } = loadModelPageWithLlm();

    const chains = context.__llmTestHooks.llmRouteChains(llmBindingsPayload());

    // execution 的第一跳是一个组，不是 m_a。
    sameJson(chains.execution, ["group:g_shared", "m_emergency"]);
    sameJson(chains.inspection, ["m_b"]);
});

test("模型页由 org_graph_llm.js 渲染：renderAll 画得出组列，链里的组也不是假模型卡", () => {
    const { context, elements } = loadModelPageWithLlm();

    // 这一条钉的是「点了新建组却什么都没出现」那个缺陷：llm 模块覆盖了
    // window.renderModelCatalog/renderModelRoleEditors，app 侧的渲染函数不会被调用。
    context.window.renderModelCatalog();

    assert.ok(elements["model-group-list"].innerHTML.length > 0, "renderAll 必须把组列画出来");
    assert.match(elements["model-group-list"].innerHTML, /data-model-group-key="g_shared"/);

    const chains = elements["model-role-editors"].innerHTML;
    assert.match(chains, /is-group-tile/);
    assert.match(chains, /组内平级/);
    // 组卡不能挂到「打开模型详情」的入口上：它没有对应的 binding。
    assert.doesNotMatch(chains, /data-model-open="group:g_shared"/);
    // 组内的 m_a 不能被摊平成一块独立模型卡——那正是「链看起来对、保存下去变成逐个成员」的来路。
    assert.doesNotMatch(chains, /data-model-chain-ref="m_a"/);
});
