const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const CODE_PATH = "g3ku/web/frontend/org_graph_external.js";
const CODE = fs.readFileSync(CODE_PATH, "utf8");

// QQ 账号面板（外部接入页）的前端契约：QQ 配置是"一 AppID 一行"的表，整表 PUT、
// 空 AppID 行不进请求体、重复 AppID 挡住保存、状态摘要按号计数。

function makeField(value, checked) {
    return { value, checked: !!checked };
}

function makeRowEl() {
    return {
        className: "",
        dataset: {},
        innerHTML: "",
        fields: {},
        querySelector(sel) {
            return Object.prototype.hasOwnProperty.call(this.fields, sel) ? this.fields[sel] : null;
        },
    };
}

function makeListEl() {
    return {
        innerHTML: "",
        children: [],
        appendedWith: null,
        appendChild(node) {
            this.children.push(node);
            return node;
        },
        querySelectorAll() {
            return this.children;
        },
        addEventListener() {},
    };
}

function loadModule({ listEl, statusEl, previewReply = null, registerReply = null, activeSession = "ext:qq-official-111:abc", activeSessionItemValue = null }) {
    const created = [];
    const confirms = [];
    const calls = { preview: [], register: [] };
    const toasts = [];
    const context = {
        console,
        setTimeout,
        clearTimeout,
        activeSessionId: () => activeSession,
        activeSessionItem: () => activeSessionItemValue,
        document: {
            getElementById(id) {
                if (id === "qq-bot-accounts") return listEl;
                if (id === "qq-bot-status-text") return statusEl;
                return null;
            },
            createElement() {
                const node = makeRowEl();
                created.push(node);
                return node;
            },
        },
        window: {},
        esc: (value) => String(value ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;"),
        showToast: (toast) => { toasts.push(toast); },
        openConfirm: (options) => { confirms.push(options); },
        ApiClient: {
            getQqBotSettings: async () => ({ enabled: true, accounts: [] }),
            updateQqBotSettings: async (body) => body,
            previewQqBotOwnerRegistration: async (body) => {
                calls.preview.push(body);
                if (previewReply instanceof Error) throw previewReply;
                return previewReply || {};
            },
            registerQqBotOwner: async (body) => {
                calls.register.push(body);
                if (registerReply instanceof Error) throw registerReply;
                return registerReply || {};
            },
        },
    };
    context.window = context;
    vm.createContext(context);
    vm.runInContext(
        `${CODE}
        this.__t = { QQ_BOT_STATE, renderQqBotAccounts, _collectQqBotRows, _qqBotRowsBody, _qqBotRowStatus, _editQqBotOwners, _registerQqBotOwner, _confirmQqBotOwnerRegistration };`,
        context,
    );
    return { t: context.__t, created, confirms, calls, toasts };
}

const row = (over = {}) => ({
    app_id: "",
    app_secret: "",
    label: "",
    sandbox: false,
    enabled: true,
    has_secret: false,
    service: null,
    ...over,
});

test("QQ 账号面板：两行账号各自渲染，AppID 出现在行内", () => {
    const listEl = makeListEl();
    const { t } = loadModule({ listEl, statusEl: { textContent: "", className: "" } });
    t.QQ_BOT_STATE.loaded = true;
    t.QQ_BOT_STATE.rows = [row({ app_id: "111", has_secret: true }), row({ app_id: "222", sandbox: true })];

    t.renderQqBotAccounts();

    assert.equal(listEl.children.length, 2);
    assert.match(listEl.children[0].innerHTML, /111/);
    assert.match(listEl.children[1].innerHTML, /222/);
    assert.equal(listEl.children[0].dataset.index, "0");
});

test("QQ 账号面板：collect 跳过空 AppID 行，沙箱不再由界面提供", () => {
    const listEl = makeListEl();
    const first = makeRowEl();
    first.dataset.index = "0";
    first.fields = {
        ".qq-bot-appid": makeField("111"),
        ".qq-bot-secret": makeField("typed-secret"),
        ".qq-bot-label": makeField("主号"),
    };
    const blank = makeRowEl();
    blank.dataset.index = "1";
    blank.fields = { ".qq-bot-appid": makeField("   ") };
    listEl.children = [first, blank];
    const { t } = loadModule({ listEl, statusEl: { textContent: "", className: "" } });
    t.QQ_BOT_STATE.rows = [row({ app_id: "111", has_secret: true })];

    const rows = t._collectQqBotRows();

    assert.equal(rows.length, 1);
    assert.equal(rows[0].app_id, "111");
    assert.equal(rows[0].app_secret, "typed-secret");
    // 界面没有沙箱开关，收集侧一律写 false：存量勾过的号下次保存就归位。
    assert.equal(rows[0].sandbox, false);
});

test("QQ 账号面板：重复 AppID 时整表请求体被拒", () => {
    const { t } = loadModule({ listEl: makeListEl(), statusEl: { textContent: "", className: "" } });
    t.QQ_BOT_STATE.enabled = true;
    t.QQ_BOT_STATE.rows = [row({ app_id: "111" }), row({ app_id: "111", app_secret: "b" })];
    assert.equal(t._qqBotRowsBody(), null);

    t.QQ_BOT_STATE.rows = [row({ app_id: "111", app_secret: "a", sandbox: true }), row({ app_id: "222" })];
    // 请求体对象产自 vm context，原型与宿主不同；深比前先归一。
    const body = JSON.parse(JSON.stringify(t._qqBotRowsBody()));
    // sandbox 即使 state 里还留着 true，请求体也必须写 false——界面已无该开关。
    assert.deepEqual(body, {
        enabled: true,
        accounts: [
            { app_id: "111", app_secret: "a", label: "", sandbox: false, enabled: true, owner_openids: [] },
            { app_id: "222", app_secret: "", label: "", sandbox: false, enabled: true, owner_openids: [] },
        ],
    });
});

test("QQ 账号面板：名单入口按条数渲染，弹窗写 owner_openids 并挡非法形状", async () => {
    const listEl = makeListEl();
    const { t, confirms } = loadModule({ listEl, statusEl: { textContent: "", className: "" } });
    t.QQ_BOT_STATE.loaded = true;
    t.QQ_BOT_STATE.enabled = true;
    t.QQ_BOT_STATE.rows = [row({ app_id: "111", has_secret: true, owner_openids: ["EB6C8D4341C1238A627FF73CBE540DAE"] })];
    // 采集口读 DOM：给一行已落库卡片的输入面。
    const card = makeRowEl();
    card.dataset.index = "0";
    card.fields = {
        ".qq-bot-appid": makeField("111"),
        ".qq-bot-secret": makeField(""),
        ".qq-bot-label": makeField(""),
    };
    listEl.children = [card];

    t.renderQqBotAccounts();
    const rendered = listEl.children[listEl.children.length - 1];
    assert.match(rendered.innerHTML, /data-action="register-owner"[^>]*>注册Master</);
    assert.match(rendered.innerHTML, /data-action="edit-owner"[^>]*>名单 1</);

    t._editQqBotOwners(0);
    assert.equal(confirms.length, 1);
    assert.match(confirms[0].text, /Master/);
    assert.equal(confirms[0].input.value, "EB6C8D4341C1238A627FF73CBE540DAE");

    let rejected = false;
    try {
        await confirms[0].onConfirm({ value: "not-an-openid" });
    } catch (error) {
        rejected = true;
    }
    assert.equal(rejected, true);

    await confirms[0].onConfirm({ value: "AAAA111122223333, BBBB444455556666" });
    const body = JSON.parse(JSON.stringify(t._qqBotRowsBody()));
    assert.deepEqual(body.accounts[0].owner_openids, ["AAAA111122223333", "BBBB444455556666"]);
});

test("QQ 账号面板：注册Master 只收编号+口令，预览后二次确认再写入", async () => {
    const listEl = makeListEl();
    const previewReply = {
        ok: true,
        session: { session_id: "ext:qq-official-111:abc", chat_type: "group", title: "测试群" },
        sender: { number: 2, number_label: "0002", id_masked: "EB6C8D…0DAE" },
    };
    const registerReply = {
        ok: true,
        registered: true,
        session_trust_upgraded: true,
        enabled: true,
        accounts: [row({ app_id: "111", has_secret: true, owner_openids: ["EB6C8D4341C1238A627FF73CBE540DAE"] })],
    };
    const { t, confirms, calls, toasts } = loadModule({
        listEl,
        statusEl: { textContent: "", className: "" },
        previewReply,
        registerReply,
    });
    t.QQ_BOT_STATE.loaded = true;
    t.QQ_BOT_STATE.rows = [row({ app_id: "111", has_secret: true })];
    const card = makeRowEl();
    card.dataset.index = "0";
    card.fields = {
        ".qq-bot-appid": makeField("111"),
        ".qq-bot-secret": makeField(""),
        ".qq-bot-label": makeField(""),
    };
    listEl.children = [card];
    t.renderQqBotAccounts();
    // 没注册过时不出现名单入口。
    assert.doesNotMatch(listEl.children[listEl.children.length - 1].innerHTML, /data-action="edit-owner"/);

    t._registerQqBotOwner(0);
    assert.equal(confirms.length, 1);
    assert.match(confirms[0].text, /0002/);
    assert.equal(confirms[0].input.label, "发送者编号");

    await confirms[0].onConfirm({ value: "0002" });
    await new Promise((resolve) => setTimeout(resolve, 0));
    assert.equal(confirms.length, 2);
    assert.equal(confirms[1].input.type, "password");
    assert.match(confirms[1].text, /测试群/);
    assert.match(confirms[1].text, /EB6C8D…0DAE/);
    assert.deepEqual(JSON.parse(JSON.stringify(calls.preview)), [{
        session_id: "ext:qq-official-111:abc",
        number: "0002",
        app_id: "111",
    }]);

    await confirms[1].onConfirm({ value: "owner-password" });
    assert.deepEqual(JSON.parse(JSON.stringify(calls.register)), [{
        session_id: "ext:qq-official-111:abc",
        number: "0002",
        app_id: "111",
        unlock_password: "owner-password",
    }]);
    assert.equal(toasts.at(-1).kind, "success");
    assert.match(toasts.at(-1).text, /当前私聊会话已可写本地记忆/);
});

test("QQ 账号面板：私聊注册直接定位对方，不再要求编号", async () => {
    const listEl = makeListEl();
    const previewReply = {
        ok: true,
        auto_number: true,
        session: { session_id: "ext:qq-official-111:dm", chat_type: "dm", title: "" },
        sender: { number: 2, number_label: "0002", id_masked: "EB6C8D…0DAE" },
    };
    const registerReply = {
        ok: true,
        registered: true,
        session_trust_upgraded: true,
        enabled: true,
        accounts: [row({ app_id: "111", has_secret: true, owner_openids: ["EB6C8D4341C1238A627FF73CBE540DAE"] })],
    };
    const { t, confirms, calls, toasts } = loadModule({
        listEl,
        statusEl: { textContent: "", className: "" },
        previewReply,
        registerReply,
        activeSession: "ext:qq-official-111:dm",
        activeSessionItemValue: { session_id: "ext:qq-official-111:dm", chat_type: "dm" },
    });
    t.QQ_BOT_STATE.loaded = true;
    t.QQ_BOT_STATE.rows = [row({ app_id: "111", has_secret: true })];
    const card = makeRowEl();
    card.dataset.index = "0";
    card.fields = {
        ".qq-bot-appid": makeField("111"),
        ".qq-bot-secret": makeField(""),
        ".qq-bot-label": makeField(""),
    };
    listEl.children = [card];
    t.renderQqBotAccounts();

    t._registerQqBotOwner(0);
    await new Promise((resolve) => setTimeout(resolve, 0));
    assert.equal(confirms.length, 1);
    assert.equal(confirms[0].input.type, "password");
    assert.match(confirms[0].text, /对方 EB6C8D…0DAE/);
    assert.doesNotMatch(confirms[0].text, /编号 0002/);
    assert.deepEqual(JSON.parse(JSON.stringify(calls.preview)), [{
        session_id: "ext:qq-official-111:dm",
        number: "",
        app_id: "111",
    }]);

    await confirms[0].onConfirm({ value: "owner-password" });
    assert.deepEqual(JSON.parse(JSON.stringify(calls.register)), [{
        session_id: "ext:qq-official-111:dm",
        number: "",
        app_id: "111",
        unlock_password: "owner-password",
    }]);
    assert.match(toasts.at(-1).text, /当前私聊会话已可写本地记忆/);
});

test("QQ 账号面板：编号非法或无当前会话时不发预览请求", async () => {
    const listEl = makeListEl();
    const { t, confirms, calls, toasts } = loadModule({ listEl, statusEl: { textContent: "", className: "" } });
    t.QQ_BOT_STATE.loaded = true;
    t.QQ_BOT_STATE.rows = [row({ app_id: "111", has_secret: true })];
    const card = makeRowEl();
    card.dataset.index = "0";
    card.fields = {
        ".qq-bot-appid": makeField("111"),
        ".qq-bot-secret": makeField(""),
        ".qq-bot-label": makeField(""),
    };
    listEl.children = [card];
    t.renderQqBotAccounts();

    t._registerQqBotOwner(0);
    await assert.rejects(() => confirms[0].onConfirm({ value: "0001" }));
    assert.equal(calls.preview.length, 0);

    const blankList = makeListEl();
    const blankCard = makeRowEl();
    blankCard.dataset.index = "0";
    blankCard.fields = {
        ".qq-bot-appid": makeField("111"),
        ".qq-bot-secret": makeField(""),
        ".qq-bot-label": makeField(""),
    };
    blankList.children = [blankCard];
    const blank = loadModule({ listEl: blankList, statusEl: { textContent: "", className: "" }, activeSession: "" });
    blank.t.QQ_BOT_STATE.loaded = true;
    blank.t.QQ_BOT_STATE.rows = [row({ app_id: "111", has_secret: true })];
    blank.t._registerQqBotOwner(0);
    assert.equal(blank.confirms.length, 0);
    assert.equal(blank.toasts.at(-1).kind, "error");
});

test("QQ 账号面板：状态摘要按号计数，行状态区分未配置", () => {
    const { t } = loadModule({ listEl: makeListEl(), statusEl: { textContent: "", className: "" } });
    t.QQ_BOT_STATE.enabled = true;
    t.QQ_BOT_STATE.rows = [
        row({ app_id: "111", service: { state: "connected" } }),
        row({ app_id: "222", service: { state: "error", detail: "intents 未开通" } }),
        row({ app_id: "333", enabled: false }),
    ];

    t.renderQqBotAccounts();

    const status = t._qqBotRowStatus(t.QQ_BOT_STATE.rows[1]);
    assert.equal(status.state, "error");
    assert.match(status.text, /错误（intents 未开通）/);
});

test("QQ 账号面板：徽标在单行卡片里紧挨着 AppID，取色跟服务态走", () => {
    const listEl = makeListEl();
    const { t } = loadModule({ listEl, statusEl: { textContent: "", className: "" } });
    t.QQ_BOT_STATE.loaded = true;
    t.QQ_BOT_STATE.enabled = true;
    t.QQ_BOT_STATE.rows = [
        row({ app_id: "111", has_secret: true, service: { state: "connected" } }),
        row({ app_id: "222", service: { state: "error", detail: "intents 未开通" } }),
        row({ app_id: "333", service: { state: "connecting" } }),
        row({ app_id: "444", enabled: false }),
    ];

    t.renderQqBotAccounts();

    const html0 = listEl.children[0].innerHTML;
    // 单行卡：AppID 后面紧跟徽标，徽标后面是开关与操作按钮，不再分层。
    assert.match(html0, /qq-bot-account-appid-text[\s\S]*data-status="completed"[\s\S]*qq-bot-row-switch[\s\S]*data-action="edit-secret"[\s\S]*data-action="remove"/);
    assert.doesNotMatch(html0, /qq-bot-account-head|qq-bot-account-meta/);
    assert.doesNotMatch(html0, /external-token-actions[\s\S]*qq-bot-account-status/);
    assert.match(listEl.children[1].innerHTML, /data-status="failed"/);
    assert.match(listEl.children[2].innerHTML, /data-status="running"/);
    assert.equal(t._qqBotRowStatus(t.QQ_BOT_STATE.rows[3]).badge, "pending");
});

test("QQ 账号面板：新行是三个输入框，沙箱已撤掉，临时卡用叉号而不是删除按钮", () => {
    const listEl = makeListEl();
    const { t } = loadModule({ listEl, statusEl: { textContent: "", className: "" } });
    t.QQ_BOT_STATE.loaded = true;
    // 没有 AppID 的行才是可填表单；已落库的行是单行卡片，不铺输入框。
    t.QQ_BOT_STATE.rows = [row({ app_id: "" })];

    t.renderQqBotAccounts();

    const html = listEl.children[0].innerHTML;
    const order = ["qq-bot-appid", "qq-bot-secret", "qq-bot-label", "qq-bot-account-remove-x"]
        .map((name) => html.indexOf(name));
    assert.deepEqual(order, [...order].sort((a, b) => a - b));
    assert.equal(order.every((i) => i >= 0), true);
    assert.doesNotMatch(html, /qq-bot-sandbox/);
    // 临时卡还没有可删的对象，所以不出「删除」按钮。
    assert.doesNotMatch(html, /data-action="remove"[^>]*>删除</);
});

test("QQ 账号面板：已保存的行是卡片，备注即标题且可点改，AppID 掉到下方", () => {
    const listEl = makeListEl();
    const { t } = loadModule({ listEl, statusEl: { textContent: "", className: "" } });
    t.QQ_BOT_STATE.loaded = true;
    t.QQ_BOT_STATE.rows = [row({ app_id: "111", label: "客服小助手" })];

    t.renderQqBotAccounts();

    const html = listEl.children[0].innerHTML;
    assert.match(html, /<button class="qq-bot-account-title"[^>]*data-action="edit-label"/);
    assert.match(html, /qq-bot-account-title[^>]*>客服小助手</);
    // 备注本身就是唯一的改备注入口，标题右侧不再另放一颗编辑键。
    assert.doesNotMatch(html, /qq-bot-account-edit|data-lucide="pencil"/);
    assert.match(html, /qq-bot-account-appid-text">AppID 111</);
    // AppID 在开关左边，改密钥在删除左边。
    const order = ["qq-bot-account-appid-text", "qq-bot-row-switch", "edit-secret", 'data-action="remove"']
        .map((token) => html.indexOf(token));
    assert.deepEqual(order, [...order].sort((a, b) => a - b));
    assert.equal(order.every((i) => i >= 0), true);
    // 卡片态不铺输入框，也不再有沙箱勾选。
    assert.doesNotMatch(html, /qq-bot-label-inline/);
    assert.doesNotMatch(html, /qq-bot-sandbox/);
});

test("QQ 账号面板：编辑态把备注就地换成输入框，且不再留隐藏的 label 副本", () => {
    const listEl = makeListEl();
    const { t } = loadModule({ listEl, statusEl: { textContent: "", className: "" } });
    t.QQ_BOT_STATE.loaded = true;
    t.QQ_BOT_STATE.rows = [row({ app_id: "111", label: "旧备注" })];
    t.QQ_BOT_STATE.editingIndex = 0;

    t.renderQqBotAccounts();

    const html = listEl.children[0].innerHTML;
    assert.match(html, /qq-bot-label-inline[^>]*data-inline-label="0"/);
    assert.match(html, /value="旧备注"/);
    // 同一行只能有一份 label 值来源，否则采集会读到隐藏的那份旧值。
    assert.doesNotMatch(html, /type="hidden" value="旧备注"/);
    assert.doesNotMatch(html, /qq-bot-account-title/);
});

test("QQ 账号面板：备注为空时卡片标题退回 AppID", () => {
    const listEl = makeListEl();
    const { t } = loadModule({ listEl, statusEl: { textContent: "", className: "" } });
    t.QQ_BOT_STATE.loaded = true;
    t.QQ_BOT_STATE.rows = [row({ app_id: "222", label: "" })];

    t.renderQqBotAccounts();

    assert.match(listEl.children[0].innerHTML, /qq-bot-account-title[^>]*>222</);
});

// ---- 桥接 token「当操作员」开关 ----

function loadTokenModule({ items = [] } = {}) {
    const listEl = makeListEl();
    const calls = { patch: [], confirm: [], toast: [] };
    const context = {
        console,
        setTimeout,
        clearTimeout,
        document: {
            getElementById(id) {
                return id === "external-token-list" ? listEl : null;
            },
            createElement() {
                return makeRowEl();
            },
        },
        window: {},
        esc: (value) => String(value ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;"),
        showToast: (toast) => calls.toast.push(toast),
        ApiClient: {
            updateExternalApiToken: async (bridgeId, body) => {
                calls.patch.push({ bridgeId, body });
                const next = items.map((item) => (
                    item.bridge_id === bridgeId ? { ...item, operator: !!body.operator } : item
                ));
                return { enabled: true, items: next };
            },
            friendlyErrorMessage: (error) => String(error?.message || error || ""),
        },
        openConfirm: (options) => calls.confirm.push(options),
    };
    context.window = context;
    vm.createContext(context);
    vm.runInContext(
        `${CODE}
        this.__t = { EXTERNAL_API_VIEW_STATE, renderExternalTokenList, toggleExternalOperator, _applyExternalPayload };`,
        context,
    );
    context.__t.EXTERNAL_API_VIEW_STATE.loaded = true;
    context.__t._applyExternalPayload({ enabled: true, items });
    return { t: context.__t, listEl, calls };
}

const tokenItem = (over = {}) => ({
    bridge_id: "qoder",
    label: "",
    token_masked: "abcd…wxyz",
    enabled: true,
    cross_session: true,
    operator: false,
    ...over,
});

test("桥接 token：当操作员开关按 operator 渲染，关闭态不带 aria-pressed=true", () => {
    const { t, listEl } = loadTokenModule({
        items: [tokenItem(), tokenItem({ bridge_id: "qq", operator: true })],
    });

    t.renderExternalTokenList();

    assert.equal(listEl.children.length, 2);
    assert.match(listEl.children[0].innerHTML, /data-action="operator"[\s\S]*?aria-pressed="false"[\s\S]*?当操作员：已关闭/);
    assert.match(listEl.children[1].innerHTML, /data-action="operator"[\s\S]*?aria-pressed="true"[\s\S]*?当操作员：已开启/);
});

test("桥接 token：关掉当操作员不弹口令，直接提交 operator=false", async () => {
    const { t, calls } = loadTokenModule({ items: [tokenItem({ operator: true })] });

    await t.toggleExternalOperator("qoder", false);

    assert.equal(calls.confirm.length, 0);
    assert.equal(calls.patch.length, 1);
    // 请求体来自 vm 上下文，原型不同，用 JSON 比形状（strict assert 会因对象原型判不等）。
    assert.equal(JSON.stringify(calls.patch[0].body), JSON.stringify({ operator: false }));
    assert.match(calls.toast.at(-1).title, /已收回操作员权限/);
});

test("桥接 token：开启当操作员走口令确认，空口令挡在请求之前", async () => {
    const { t, calls } = loadTokenModule({ items: [tokenItem()] });

    await t.toggleExternalOperator("qoder", true);

    assert.equal(calls.confirm.length, 1);
    const dialog = calls.confirm[0];
    assert.equal(dialog.input.type, "password");
    assert.match(dialog.text, /操作员/);

    let blocked = false;
    try {
        await dialog.onConfirm({ value: "   " });
    } catch (error) {
        blocked = true;
    }
    assert.equal(blocked, true);
    assert.equal(calls.patch.length, 0);

    await dialog.onConfirm({ value: "owner-password" });
    assert.equal(
        JSON.stringify(calls.patch[0].body),
        JSON.stringify({ operator: true, unlock_password: "owner-password" }),
    );
    assert.match(calls.toast.at(-1).title, /已允许当操作员/);
});
