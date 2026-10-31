const test = require("node:test");
const assert = require("node:assert/strict");

// vm 里造出来的对象带着另一个 realm 的 Object 原型，deepStrictEqual 会比原型；
// 跨 realm 的结构断言统一走 JSON 文本比较。
const sameJson = (actual, expected, message) => assert.equal(JSON.stringify(actual), JSON.stringify(expected), message);
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { readFrontendSources } = require("./support/frontend_sources.js");

// 模型页对「负载均衡组」的契约：route_entries 的读取与回写、左侧组配置列的渲染与增删改、
// 拖进链的 group 记号，以及提交时带上整份组集合（而不是只有被链引用的那份）。

const ROOT = path.resolve(__dirname, "..", "..");
const APP_CODE = readFrontendSources(["app"]).join("\n");
const API_CODE = readFrontendSources(["api_client"]).join("\n");

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

function loadApp(elements = {}, globals = {}) {
    const context = baseContext(elements);
    context.window = context;
    vm.createContext(context);
    // bind() 的尾巴上会调到别的模块提供的渲染函数；这里只按名字补空实现，
    // 让事件绑定能跑完，不改变被测处理器的行为。
    Object.entries(globals).forEach(([name, value]) => { context[name] = value; });
    vm.runInContext(
        `${APP_CODE}\nthis.__exports = {
            S,
            U,
            applyModelCatalog,
            bind,
            buildModelRoleChainUpdates,
            chainToRouteEntries,
            chainUsesGroup,
            activeLoadBalanceGroups,
            closeLoadBalanceGroupDialog,
            confirmLoadBalanceGroupDialog,
            deleteLoadBalanceGroupDraft,
            groupRefToken,
            isGroupRef,
            loadBalanceGroupDialogDirty,
            modelScopeChain,
            normalizeModelRoleChain,
            openLoadBalanceGroupDialog,
            rewriteGroupTokensInChain,
            renderModelGroupChainTile,
            renderModelGroupColumn,
            renderModelGroupDialog,
            loadBalanceGroupWindowSummary,
            renderLoadBalanceGroupWindowAlignment,
            syncLoadBalanceGroupDialogSummary,
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
    // 窗口值故意不齐（m_a 69 万 / m_b 39 万）：组卡的"对齐最小成员"读数要有可判形状。
    return [
        { key: "m_a", name: "深度V4 快档", enabled: true, provider_model: "openai:model-a", context_window_tokens: 690000 },
        { key: "m_b", enabled: true, provider_model: "openai:model-b", context_window_tokens: 390000 },
        { key: "m_emergency", enabled: true, provider_model: "openai:model-c", context_window_tokens: 390000 },
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
    const classes = new Set();
    return {
        innerHTML: "",
        textContent: "",
        value: "",
        hidden: true,
        disabled: false,
        attributes: {},
        style: {},
        listeners: {},
        // 颜色这类显示判据要靠类名读数，空实现的 classList 会把「加了没有」读成永远没加。
        classList: {
            add: (name) => classes.add(name),
            remove: (name) => classes.delete(name),
            toggle: (name, force) => {
                const on = force === undefined ? !classes.has(name) : Boolean(force);
                if (on) classes.add(name); else classes.delete(name);
                return on;
            },
            contains: (name) => classes.has(name),
        },
        classes,
        setAttribute(name, value) { this.attributes[name] = value; },
        getAttribute(name) { return this.attributes[name]; },
        querySelector: () => null,
        querySelectorAll: () => [],
        addEventListener(type, handler) { (this.listeners[type] ||= []).push(handler); },
        // 事件委托类缺陷只能真投递：把同一个 event 交给这个容器上挂过的处理器。
        dispatch(type, event) { (this.listeners[type] || []).forEach((handler) => handler(event)); },
        appendChild() {},
        insertBefore() {},
        remove() {},
        closest: () => null,
        getBoundingClientRect: () => ({ top: 0, bottom: 0, left: 0, right: 0, width: 0, height: 0 }),
        ...extra,
    };
}

// 最小 DOM 桩：只实现处理器用到的 closest(属性选择器) 与 dataset。
function fakeNode(dataset, parent = null) {
    return {
        dataset,
        parent,
        closest(selector) {
            const attr = String(selector || "").replace(/^\[|\]$/g, "").replace(/^data-/, "");
            const camel = attr.replace(/-([a-z])/g, (_m, c) => c.toUpperCase());
            let node = this;
            while (node) {
                if (node.dataset && camel in node.dataset) return node;
                node = node.parent;
            }
            return null;
        },
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

function loadModelPage(payload = catalogPayload(), options = {}) {
    const elements = modelPageElements();
    const calls = [];
    const failure = options.failWith ? String(options.failWith) : "";
    const app = loadApp(elements, {
        renderTaskSessionScope: () => {},
        ApiClient: {
            // mini 服务端：记下提交体，再按提交体回读一份配置（链与整份组集合都被替换）。
            updateModelRoleChains: async (updates) => {
                calls.push(JSON.parse(JSON.stringify(updates)));
                if (failure) throw new Error(failure);
                return committedPayloadAfterSave(payload, updates);
            },
        },
    });
    app.applyModelCatalog(payload, { preserveRoleDrafts: false });
    return { app, elements, calls };
}

function committedPayloadAfterSave(payload, updates) {
    const next = JSON.parse(JSON.stringify(payload));
    next.route_entries = { ...(next.route_entries || {}) };
    Object.entries(updates).forEach(([scope, item]) => {
        const entries = (item.routeEntries || [])
            .length
            ? item.routeEntries
            : (item.modelKeys || []).map((key) => ({ type: "model", model_key: key }));
        next.route_entries[scope] = entries;
        if (item.maxIterations !== undefined) next.roleIterations[scope] = item.maxIterations;
    });
    const carried = Object.values(updates)
        .map((item) => item.loadBalanceGroups)
        .find((groups) => groups && Object.keys(groups).length);
    if (carried) next.load_balance_groups = JSON.parse(JSON.stringify(carried));
    return next;
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
    // 卡随时可拖、随时可删：没有「进入编辑」这一步，草稿会话在第一次交互时开启。
    assert.match(sharedRow, /draggable="true"/);
    assert.match(sharedRow, /data-group-delete="g_shared"/);
});

test("新建组先弹窗，点确定才落草稿并自动进入链编辑会话", async () => {
    const { app, elements, calls } = loadModelPage(plainChainPayload());

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
    assert.equal(await app.confirmLoadBalanceGroupDialog(), true);

    assert.equal(app.S.modelCatalog.roleEditing, true);
    sameJson(app.S.modelCatalog.loadBalanceGroupDrafts.g_stage.model_keys, ["m_a", "m_b"]);
    assert.equal(app.S.modelCatalog.loadBalanceGroupDrafts.g_stage.max_retry_rounds, 2);
    assert.equal(app.S.modelCatalog.groupDialog.open, false);

    // 点确定就是落盘点：不再需要页面右上角那一次「保存」。
    assert.equal(calls.length, 1, "组弹窗的确定必须自己提交");
    sameJson(calls[0].execution.modelKeys, ["m_a", "m_b"]);
    sameJson(Object.keys(calls[0].execution.loadBalanceGroups).sort(), ["g_shared", "g_stage", "g_unused"]);
    assert.equal(calls[0].inspection.loadBalanceGroups, undefined);
    // 落盘后草稿与已存值一致：脏位清空，头部那颗「保存」不该还亮着。
    assert.equal(app.S.modelCatalog.rolesDirty, false);
    sameJson(Object.keys(app.S.modelCatalog.loadBalanceGroups).sort(), ["g_shared", "g_stage", "g_unused"]);
});

test("组确认交的是服务端现有链：页面上没保存的拖动草稿不许被顺手提交", async () => {
    const { app, calls } = loadModelPage();

    // 链草稿被拖成「只剩 m_b」，而服务端 execution 链仍是 group:g_shared + m_emergency。
    app.startModelRoleEditing();
    app.S.modelCatalog.roleDrafts.execution = ["m_b"];

    app.openLoadBalanceGroupDialog("g_shared");
    app.setLoadBalanceGroupDialogField("rounds", "5");
    assert.equal(await app.confirmLoadBalanceGroupDialog(), true);

    // 提交体按服务端的链回写：组那一跳还在，草稿的 m_b 没被带上去。
    sameJson(calls[0].execution.routeEntries, [
        { type: "load_balance", group_key: "g_shared" },
        { type: "model", model_key: "m_emergency" },
    ]);
    assert.equal(calls[0].execution.modelKeys, undefined);
    assert.equal(calls[0].execution.loadBalanceGroups.g_shared.max_retry_rounds, 5);
    // 草稿仍是用户拖的那份，脏位还在，右上角「保存」仍可点。
    sameJson(app.S.modelCatalog.roleDrafts.execution, ["m_b"]);
    assert.equal(app.S.modelCatalog.rolesDirty, true);
});

test("组弹窗确定键的重点色：开了不亮，改过才亮", () => {
    const { app, elements } = loadModelPage();
    const confirm = elements["model-group-confirm-btn"];

    app.openLoadBalanceGroupDialog("g_shared");
    assert.equal(confirm.classes.has("is-dirty"), false, "刚打开时内容与已存值一致，不该提示有改动");
    assert.equal(app.loadBalanceGroupDialogDirty(), false);

    app.setLoadBalanceGroupDialogField("rounds", "3");
    assert.equal(confirm.classes.has("is-dirty"), true, "改了重试次数就要亮");

    app.setLoadBalanceGroupDialogField("rounds", "2");
    assert.equal(confirm.classes.has("is-dirty"), false, "改回原值等于没改");

    app.toggleLoadBalanceGroupDialogMember("m_emergency", true);
    assert.equal(confirm.classes.has("is-dirty"), true, "勾了成员要亮");
});

test("新建组的重点色按打开那一刻的默认值判，不是按空值判", () => {
    const { app, elements } = loadModelPage(plainChainPayload());
    const confirm = elements["model-group-confirm-btn"];

    app.openLoadBalanceGroupDialog();
    assert.equal(confirm.classes.has("is-dirty"), false);
    app.toggleLoadBalanceGroupDialogMember("m_a", true);
    assert.equal(confirm.classes.has("is-dirty"), true);
});

test("弹窗里空成员与空组名都被挡下，不落草稿", async () => {
    const { app, elements, calls } = loadModelPage(plainChainPayload());

    app.openLoadBalanceGroupDialog();
    assert.equal(await app.confirmLoadBalanceGroupDialog(), false);
    assert.match(app.S.modelCatalog.groupDialog.error, /至少勾选 1 个模型/);

    app.toggleLoadBalanceGroupDialogMember("m_a", true);
    app.setLoadBalanceGroupDialogField("name", "   ");
    assert.equal(await app.confirmLoadBalanceGroupDialog(), false);
    assert.match(app.S.modelCatalog.groupDialog.error, /组名不能为空/);

    assert.equal(app.S.modelCatalog.roleEditing, false);
    // 草稿仍是已保存那两份组，没有多出被拒的空白组。
    sameJson(Object.keys(app.S.modelCatalog.loadBalanceGroupDrafts).sort(), ["g_shared", "g_unused"]);
    assert.match(elements["model-group-dialog-body"].innerHTML, /组名不能为空/);
    // 被拒的改动一次都不该发出去。
    sameJson(calls, []);
});

test("重试次数只挡负数，大数按用户意图保留", async () => {
    const { app, calls } = loadModelPage(plainChainPayload());

    app.openLoadBalanceGroupDialog();
    app.setLoadBalanceGroupDialogField("name", "g_rounds");
    app.toggleLoadBalanceGroupDialogMember("m_a", true);
    app.setLoadBalanceGroupDialogField("rounds", "-1");
    assert.equal(await app.confirmLoadBalanceGroupDialog(), false);
    assert.match(app.S.modelCatalog.groupDialog.error, /不小于 0 的整数/);
    sameJson(calls, []);

    app.setLoadBalanceGroupDialogField("rounds", "9999");
    assert.equal(await app.confirmLoadBalanceGroupDialog(), true);
    assert.equal(app.S.modelCatalog.loadBalanceGroupDrafts.g_rounds.max_retry_rounds, 9999);
    assert.equal(calls[0].execution.loadBalanceGroups.g_rounds.max_retry_rounds, 9999);
    // 服务端读数跟着落：不必再点右上角的「保存」。
    assert.equal(app.S.modelCatalog.loadBalanceGroups.g_rounds.max_retry_rounds, 9999);
});

test("弹窗改组名会把链上的 group 记号一起改掉", async () => {
    const { app, calls } = loadModelPage();
    app.S.modelCatalog.roleEditing = true;
    app.S.modelCatalog.roleDrafts = { ceo: ["m_emergency"], execution: ["group:g_shared", "m_emergency"], inspection: ["m_b"], memory: [] };
    app.S.modelCatalog.loadBalanceGroupDrafts = app.cloneLoadBalanceGroups(app.S.modelCatalog.loadBalanceGroups);

    app.openLoadBalanceGroupDialog("g_shared");
    app.setLoadBalanceGroupDialogField("name", "g_fast");
    assert.equal(await app.confirmLoadBalanceGroupDialog(), true);

    sameJson(app.S.modelCatalog.roleDrafts.execution, ["group:g_fast", "m_emergency"]);
    assert.equal(app.S.modelCatalog.loadBalanceGroupDrafts.g_shared, undefined);
    sameJson(app.S.modelCatalog.loadBalanceGroupDrafts.g_fast.model_keys, ["m_a", "m_b"]);
    // 轮预算跟着组走，不因为改名被重置。
    assert.equal(app.S.modelCatalog.loadBalanceGroupDrafts.g_fast.max_retry_rounds, 2);

    // 改名要连提交体里的链一起换：留着旧记号等于让链指向一个已经不存在的组，
    // 运行时 route plan 对查不到的组是静默 continue，那一跳就没了。
    sameJson(calls[0].execution.routeEntries, [
        { type: "load_balance", group_key: "g_fast" },
        { type: "model", model_key: "m_emergency" },
    ]);
    sameJson(Object.keys(app.S.modelCatalog.loadBalanceGroups).sort(), ["g_fast", "g_unused"]);
});

test("弹窗改组名撞名或撞模型 key 时拒绝，链保持原样", async () => {
    const { app, calls } = loadModelPage();
    app.S.modelCatalog.roleEditing = true;
    app.S.modelCatalog.roleDrafts = { ceo: [], execution: ["group:g_shared"], inspection: [], memory: [] };
    app.S.modelCatalog.loadBalanceGroupDrafts = app.cloneLoadBalanceGroups(app.S.modelCatalog.loadBalanceGroups);

    app.openLoadBalanceGroupDialog("g_shared");
    app.setLoadBalanceGroupDialogField("name", "g_unused");
    assert.equal(await app.confirmLoadBalanceGroupDialog(), false);
    assert.match(app.S.modelCatalog.groupDialog.error, /组名已存在：g_unused/);

    app.setLoadBalanceGroupDialogField("name", "m_a");
    assert.equal(await app.confirmLoadBalanceGroupDialog(), false);
    assert.match(app.S.modelCatalog.groupDialog.error, /不能与模型配置同名/);

    sameJson(app.S.modelCatalog.roleDrafts.execution, ["group:g_shared"]);
    assert.equal(app.S.modelCatalog.loadBalanceGroupDrafts.g_shared.max_retry_rounds, 2);
    assert.equal(app.S.modelCatalog.groupDialog.open, true);
    sameJson(calls, []);
});

test("保存失败停在弹窗里点名原因，改动留在草稿可重试", async () => {
    const { app, elements } = loadModelPage(plainChainPayload(), { failWith: "组名不能与模型配置同名：x2" });

    app.openLoadBalanceGroupDialog("g_unused");
    app.setLoadBalanceGroupDialogField("rounds", "4");
    assert.equal(await app.confirmLoadBalanceGroupDialog(), true);

    // 弹窗不许自己关掉：改动还在草稿里，原地改完可以再点一次。
    assert.equal(app.S.modelCatalog.groupDialog.open, true);
    assert.match(app.S.modelCatalog.groupDialog.error, /保存失败：组名不能与模型配置同名/);
    assert.match(elements["model-group-dialog-body"].innerHTML, /保存失败：/);
    // 已存读数没变（服务端拒了），草稿带着新值，脏位仍在——右上角「保存」作为兜底还能点。
    assert.equal(app.S.modelCatalog.loadBalanceGroups.g_unused.max_retry_rounds, 9);
    assert.equal(app.S.modelCatalog.loadBalanceGroupDrafts.g_unused.max_retry_rounds, 4);
    assert.equal(app.S.modelCatalog.rolesDirty, true);
    assert.equal(elements["model-group-confirm-btn"].disabled, false);
    assert.equal(elements["model-group-confirm-btn"].textContent, "确定");
});

test("组确定不许被「链中混着不收图成员」那道确认挡住：交回的是没变过的链", async () => {
    // 服务端的 execution 链本来就混着收图与不收图的成员——那是上一次链保存时接受过的形状。
    const payload = catalogPayload();
    // 三颗成员都要写出显式的识图勾选：没写过这格的记录读成 null，混勾判据会把它们整个跳过。
    payload.catalog = payload.catalog.map((item) => ({
        ...item,
        image_multimodal_enabled: item.key === "m_a",
    }));
    const { app, calls } = loadModelPage(payload);

    app.openLoadBalanceGroupDialog("g_shared");
    app.setLoadBalanceGroupDialogField("rounds", "6");
    assert.equal(await app.confirmLoadBalanceGroupDialog(), true);

    // 若这道确认仍被调用，提交会停在弹窗后面（桩里没有点下去的人），calls 就是空的。
    assert.equal(calls.length, 1, "组的确定必须直接落盘，不追加与链无关的确认");
    assert.equal(app.S.modelCatalog.loadBalanceGroups.g_shared.max_retry_rounds, 6);
    assert.equal(app.S.modelCatalog.groupDialog.open, false);
});

test("删除组会把引用它的链位一起摘掉，并当场落盘", async () => {
    const { app, calls } = loadModelPage();
    app.S.modelCatalog.roleEditing = true;
    app.S.modelCatalog.roleDrafts = { ceo: [], execution: ["group:g_shared", "m_emergency"], inspection: ["m_b"], memory: [] };
    app.S.modelCatalog.loadBalanceGroupDrafts = app.cloneLoadBalanceGroups(app.S.modelCatalog.loadBalanceGroups);

    assert.equal(await app.deleteLoadBalanceGroupDraft("g_shared"), true);

    sameJson(app.S.modelCatalog.roleDrafts.execution, ["m_emergency"]);
    assert.equal(app.S.modelCatalog.loadBalanceGroupDrafts.g_shared, undefined);
    // 链里再也没有组：这条 scope 回到扁平 modelKeys 载荷。
    const updates = app.buildModelRoleChainUpdates(["execution"], { useDrafts: true });
    assert.equal(updates.execution.routeEntries, undefined);
    sameJson(updates.execution.modelKeys, ["m_emergency"]);
    // 提交体按服务端的链把那一跳摘掉（不是留着指向已删的组），组字典整份替换。
    sameJson(calls[0].execution.modelKeys, ["m_emergency"]);
    sameJson(Object.keys(calls[0].execution.loadBalanceGroups).sort(), ["g_unused"]);
    sameJson(Object.keys(app.S.modelCatalog.loadBalanceGroups).sort(), ["g_unused"]);
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

function loadModelPageWithLlm(extraElements = {}, api = {}) {
    const elements = modelPageElements();
    elements["llm-bindings-list"] = stubElement();
    elements["llm-editor-shell"] = stubElement();
    Object.assign(elements, extraElements);
    const context = baseContext(elements);
    context.window = context;
    vm.createContext(context);
    vm.runInContext(APP_CODE, context);
    context.ApiClient = api;
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

test("旧配置（记录里没写过 retry_on）打开详情时不许一上来就报有改动", async () => {
    const saveButton = stubElement({ dataset: { llmAction: "save-detail" } });
    const shell = stubElement({ querySelectorAll: () => [saveButton] });
    const { context } = loadModelPageWithLlm(
        { "llm-editor-shell": shell },
        {
            getLlmConfig: async () => ({
                provider_id: "openai",
                base_url: "https://api.example.com/v1",
                api_key: "sk-1",
                default_model: "openai:model-a",
                parameters: { context_window_tokens: 200000 },
            }),
        },
    );
    const hooks = context.__llmTestHooks;
    // bindingMap 正常由加载流程建；这里直接铺一份，让 openModel 走得通。
    vm.runInContext("const s = __llmTestHooks.llmState(); s.bindingMap = { m_a: s.bindings.find((i) => i.key === 'm_a') };", context);

    context.window.openModel("m_a");
    await new Promise((resolve) => setTimeout(resolve, 20));

    const editor = hooks.llmState().editor;
    assert.equal(editor.mode, "detail");
    // 面板把缺席的 retry_on 填成默认关键词：颜色必须按「打开那一刻」判，而不是按服务端字段有无判。
    sameJson(editor.retryOn, ["network", "429", "tpm", "rpm"]);
    assert.equal(hooks.llmEditorDirty(), false, "刚打开就是基线，没有任何改动可言");
    assert.equal(saveButton.classes.has("is-dirty"), false);

    hooks.llmState().editor.retryOn = ["network", "429", "502"];
    hooks.syncLlmEditorDirtyAffordance();
    assert.equal(hooks.llmEditorDirty(), true, "改了重试关键词要亮");
    assert.equal(saveButton.classes.has("is-dirty"), true);
});

test("模型配置面板的确认键：内容改了才填重点色，关闭键不许一起亮", async () => {
    const saveButton = stubElement({ dataset: { llmAction: "save-create" } });
    const closeButton = stubElement({ dataset: { llmAction: "close" } });
    const shell = stubElement({ querySelectorAll: () => [saveButton, closeButton] });
    const { context } = loadModelPageWithLlm({ "llm-editor-shell": shell });
    const hooks = context.__llmTestHooks;

    await hooks.openCreateModal();
    assert.equal(hooks.llmEditorDirty(), false, "刚打开时与打开那一刻一致，不该报有改动");
    assert.equal(saveButton.classes.has("is-dirty"), false);

    hooks.llmState().editor.createName = "demo";
    assert.equal(hooks.llmEditorDirty(), true);
    hooks.syncLlmEditorDirtyAffordance();
    assert.equal(saveButton.classes.has("is-dirty"), true, "改了内容确认键要当场变重心");
    assert.equal(closeButton.classes.has("is-dirty"), false, "只有确认键参与这套颜色");

    hooks.llmState().editor.createName = "";
    hooks.syncLlmEditorDirtyAffordance();
    assert.equal(saveButton.classes.has("is-dirty"), false, "改回原值等于没改");
});

test("新建配置预填 10 轮重试，默认关键词含 tpm/rpm", async () => {
    const { context } = loadModelPageWithLlm();
    const hooks = context.__llmTestHooks;

    await hooks.openCreateModal();
    const editor = hooks.llmState().editor;

    assert.equal(editor.mode, "create");
    assert.equal(editor.retryCount, 10);
    assert.equal(editor.initialRetryCount, 10, "预填值就是打开那一刻的基线");
    assert.equal(hooks.llmEditorDirty(), false, "预填默认值不等于未保存改动");
    sameJson(editor.retryOn, ["network", "429", "tpm", "rpm"]);
    sameJson(editor.initialRetryOn, ["network", "429", "tpm", "rpm"]);
});

test("配置面板的重试次数：负数原样交给保存校验，不许静默夹成 0", () => {
    // 0 现在就是「不重试」，把 -1 夹成 0 等于替操作员关掉重试并让他看不见。
    const retryInput = stubElement({ value: "-1" });
    const { context } = loadModelPageWithLlm({ "llm-binding-retry-count": retryInput });
    const hooks = context.__llmTestHooks;
    hooks.llmState().editor = { retryCount: 5, initialRetryCount: 5 };

    hooks.syncBindingInputs();
    assert.equal(hooks.llmState().editor.retryCount, -1, "负数要留在 editor 上，由保存那一步报错");
    assert.equal(hooks.llmEditorDirty(), true);

    retryInput.value = "0";
    hooks.syncBindingInputs();
    assert.equal(hooks.llmState().editor.retryCount, 0, "填 0 就是不重烧，按字面值留");

    retryInput.value = "";
    hooks.syncBindingInputs();
    assert.equal(hooks.llmState().editor.retryCount, 0, "留空同样是 0，没有内置默认可退");
});

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

test("组卡的叉号先于整卡点击区判定：点它是移出链，不是弹配置", () => {
    const elements = modelPageElements();
    const app = loadApp(elements, { renderTaskSessionScope: () => {} });
    app.bind();
    app.applyModelCatalog(catalogPayload(), { preserveRoleDrafts: false });
    app.startModelRoleEditing();
    sameJson(app.modelScopeChain("execution"), ["group:g_shared", "m_emergency"]);

    // 叉号在带 data-group-edit 的那张 article 里面：判定顺序错位就会弹配置。
    const tile = fakeNode({ groupEdit: "g_shared", modelChainRef: "group:g_shared", scope: "execution" });
    const removeButton = fakeNode({ modelChainAction: "remove", scope: "execution", index: "0" }, tile);

    elements["model-role-editors"].dispatch("click", { target: removeButton });

    sameJson(app.modelScopeChain("execution"), ["m_emergency"]);
    assert.equal(app.S.modelCatalog.groupDialog.open, false, "移出链不该顺手把组配置弹窗弹出来");
});

test("组卡本体仍然整张可点：查看态也打开配置", () => {
    const elements = modelPageElements();
    const app = loadApp(elements, { renderTaskSessionScope: () => {} });
    app.bind();
    app.applyModelCatalog(catalogPayload(), { preserveRoleDrafts: false });

    const tile = fakeNode({ groupEdit: "g_shared", modelChainRef: "group:g_shared" });
    const body = fakeNode({ modelChainRef: "group:g_shared" }, tile);

    elements["model-role-editors"].dispatch("click", { target: body });

    assert.equal(app.S.modelCatalog.groupDialog.open, true);
    assert.equal(app.S.modelCatalog.groupDialog.editingKey, "g_shared");
    sameJson(app.S.modelCatalog.groupDialog.modelKeys, ["m_a", "m_b"]);
});

test("组卡不显示窗口对齐读数：链上卡与组列卡都只留成员与链状态", () => {
    const { app, elements } = loadModelPage();

    const tile = app.renderModelGroupChainTile("execution", "g_shared", 0, false);
    assert.doesNotMatch(tile, /上下文对齐|窗口对齐|本组最大/);
    // 不是整行没渲染：组卡自己的标记与成员仍在，少的是窗口那一档读数。
    assert.match(tile, /policy-chip risk-low">组</);

    app.renderModelGroupColumn();
    const rows = elements["model-group-list"].innerHTML.split("<article").filter(Boolean);
    const sharedRow = rows.find((row) => row.includes('data-model-group-key="g_shared"'));
    assert.doesNotMatch(sharedRow, /上下文对齐|窗口对齐|本组最大/);
    assert.match(sharedRow, /title="m_b">m_b</);
});

test("组弹窗把这行放在成员模型上方，值是对齐后的那颗上限，不齐时给警示档", () => {
    const { app, elements } = loadModelPage();

    app.openLoadBalanceGroupDialog("g_shared");
    const body = elements["model-group-dialog-body"].innerHTML;
    assert.match(body, /组内最大上下文对齐: 390000/);
    assert.match(body, /title="对齐到最小那颗 m_b=390000；组内最大 690000"/);
    assert.match(body, /policy-chip risk-high"[^>]*>组内最大上下文对齐/);
    const alignAt = body.indexOf("组内最大上下文对齐");
    const membersAt = body.indexOf("成员模型");
    assert.ok(alignAt >= 0 && membersAt >= 0 && alignAt < membersAt, "这行要在成员列表之前，勾选之后才看得见连带");
});

test("同组窗口一致时不带警示档，读不到窗口的成员不参与对齐", () => {
    const app = loadApp();
    const payload = catalogPayload();
    payload.catalog = [...payload.catalog, { key: "m_nowindow", enabled: true, provider_model: "openai:model-w" }];
    payload.items = payload.catalog.map((item) => item.key);
    app.applyModelCatalog(payload, { preserveRoleDrafts: false });

    const equal = app.renderLoadBalanceGroupWindowAlignment(["m_b", "m_emergency"]);
    assert.match(equal, /组内最大上下文对齐: 390000/);
    assert.doesNotMatch(equal, /risk-high/);
    assert.equal(app.renderLoadBalanceGroupWindowAlignment(["m_nowindow"]), "");
});

test("组弹窗点名「不收图」的成员数：混勾的代价要在勾成员时看得见", () => {
    const app = loadApp();
    const payload = catalogPayload();
    payload.catalog = payload.catalog.map((item) =>
        item.key === "m_b" ? { ...item, imageMultimodalEnabled: false } : item
    );
    payload.catalog = [...payload.catalog, {
        key: "m_mm",
        enabled: true,
        provider_model: "openai:model-mm",
        context_window_tokens: 390000,
        imageMultimodalEnabled: true,
    }];
    payload.items = payload.catalog.map((item) => item.key);
    app.applyModelCatalog(payload, { preserveRoleDrafts: false });

    const mixed = app.renderLoadBalanceGroupWindowAlignment(["m_mm", "m_b"]);
    assert.match(mixed, /组内 1 颗不收图/);
    assert.match(mixed, /title="m_b"/);
    // 全收图的组不给这条；读不到开关（旧记录没这个字段）也不算不收图。
    assert.doesNotMatch(app.renderLoadBalanceGroupWindowAlignment(["m_mm"]), /不收图/);
    assert.doesNotMatch(app.renderLoadBalanceGroupWindowAlignment(["m_emergency"]), /不收图/);
});

test("组弹窗的这行随勾选更新：压低整组上限的那一刻要当场看见", () => {
    const { app, elements } = loadModelPage();
    const body = elements["model-group-dialog-body"];
    const windowsRow = { innerHTML: "" };
    const countRow = { textContent: "" };
    body.querySelector = (selector) => {
        if (selector === "[data-group-dialog-count]") return countRow;
        if (selector === "[data-group-dialog-windows]") return windowsRow;
        return null;
    };
    app.openLoadBalanceGroupDialog("");
    app.setLoadBalanceGroupDialogField("name", "g_probe");

    app.toggleLoadBalanceGroupDialogMember("m_a", true);
    app.syncLoadBalanceGroupDialogSummary();
    assert.match(windowsRow.innerHTML, /组内最大上下文对齐: 690000/);
    assert.doesNotMatch(windowsRow.innerHTML, /risk-high/);

    app.toggleLoadBalanceGroupDialogMember("m_b", true);
    app.syncLoadBalanceGroupDialogSummary();
    assert.match(windowsRow.innerHTML, /组内最大上下文对齐: 390000/);
    assert.match(windowsRow.innerHTML, /组内最大 690000/);
});

test("重开组弹窗要把正文滚回顶部：常驻容器会把上一组的滚动位置带过来", () => {
    const { app, elements } = loadModelPage();
    const body = elements["model-group-dialog-body"];

    assert.equal(app.openLoadBalanceGroupDialog("g_shared"), true);
    // 用户在上一组的成员列表里滚到了底部，组名那颗输入框已经滚出可视区。
    body.scrollTop = 91;
    assert.equal(app.openLoadBalanceGroupDialog("g_unused"), true);
    assert.equal(body.scrollTop, 0, "换组重开必须从组名那一行开始");
});

test("确认框自己不许当滚动口：点列表底部的勾选框会把整窗顶上去", () => {
    // overflow:hidden 仍是滚动容器——成员列表的溢出内容会让弹窗 scrollHeight 高于
    // clientHeight，复选框一拿焦点，浏览器就把整窗滚下去（标题被顶出、底部露空白）。
    // 只有 clip 不是滚动容器，所以这条钉在 CSS 上而不是 DOM 上。
    const css = fs.readFileSync(path.join(ROOT, "g3ku/web/frontend/org_graph.css"), "utf8");
    const open = css.search(/^\.confirm-dialog\s*\{/m);
    assert.ok(open > 0, "找不到 .confirm-dialog 规则块");
    const block = css.slice(open, css.indexOf("}", open));
    assert.match(block, /overflow:\s*clip/);
    assert.doesNotMatch(block, /overflow:\s*hidden/);
});
