const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const CODE_PATH = "g3ku/web/frontend/org_graph_external.js";
const CODE = fs.readFileSync(CODE_PATH, "utf8");

// QQ 账号面板（外部接入页）的前端契约：一 AppID 一行、整表 PUT、状态摘要，
// 以及 Master 绑定码流程（说明 → 口令 → 码/倒计时/等待 → 注销）。

function makeField(value, checked) {
    return { value, checked: !!checked };
}

function makeClassList() {
    const set = new Set();
    return {
        add: (name) => set.add(name),
        remove: (name) => set.delete(name),
        toggle: (name, on) => (on ? set.add(name) : set.delete(name)),
        contains: (name) => set.has(name),
    };
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

function makeBindEl() {
    return {
        hidden: true,
        textContent: "",
        disabled: false,
        dataset: {},
        classList: makeClassList(),
    };
}

function makeListEl() {
    return {
        innerHTML: "",
        children: [],
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

function loadModule({
    listEl,
    statusEl,
    createReply = {
        ok: true,
        token: "NEGI-MASTER-AAAA-BBBB-CCCC-DDDD",
        expires_in_seconds: 300,
    },
    statusReply = { ok: true, registered: false, pending: true, expires_in_seconds: 120 },
    unregisterReply = { ok: true, enabled: true, accounts: [] },
    settingsReply = { enabled: true, accounts: [] },
} = {}) {
    const created = [];
    const confirms = [];
    const calls = { create: [], status: [], unregister: [] };
    const toasts = [];
    const copied = [];
    const bind = {
        backdrop: makeBindEl(),
        code: makeBindEl(),
        copy: makeBindEl(),
        status: makeBindEl(),
        statusText: makeBindEl(),
        countdown: makeBindEl(),
    };
    const bindIds = {
        "qq-master-bind-backdrop": bind.backdrop,
        "qq-master-bind-code": bind.code,
        "qq-master-bind-copy": bind.copy,
        "qq-master-bind-status": bind.status,
        "qq-master-bind-status-text": bind.statusText,
        "qq-master-bind-countdown": bind.countdown,
    };
    const context = {
        console,
        setTimeout,
        clearTimeout,
        setInterval,
        clearInterval,
        navigator: { clipboard: { writeText: async (text) => { copied.push(text); } } },
        document: {
            getElementById(id) {
                if (id === "qq-bot-accounts") return listEl;
                if (id === "qq-bot-status-text") return statusEl;
                if (Object.prototype.hasOwnProperty.call(bindIds, id)) return bindIds[id];
                return null;
            },
            createElement() {
                const node = makeRowEl();
                created.push(node);
                return node;
            },
            body: { appendChild() {} },
            execCommand() {
                return true;
            },
        },
        window: {},
        esc: (value) => String(value ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;"),
        showToast: (toast) => { toasts.push(toast); },
        openConfirm: (options) => { confirms.push(options); },
        ApiClient: {
            getQqBotSettings: async () => settingsReply,
            updateQqBotSettings: async (body) => body,
            createQqMasterBinding: async (body) => {
                calls.create.push(body);
                if (createReply instanceof Error) throw createReply;
                return createReply;
            },
            getQqMasterBindingStatus: async (appId) => {
                calls.status.push(appId);
                return statusReply;
            },
            unregisterQqMaster: async (body) => {
                calls.unregister.push(body);
                if (unregisterReply instanceof Error) throw unregisterReply;
                return unregisterReply;
            },
        },
    };
    context.window = context;
    vm.createContext(context);
    vm.runInContext(
        `${CODE}
        this.__t = { QQ_BOT_STATE, QQ_MASTER_BIND, renderQqBotAccounts, _collectQqBotRows, _qqBotRowsBody, _qqBotRowStatus, _registerQqMaster, _unregisterQqMaster, openQqMasterBindingModal, _pollQqMasterBinding, _copyQqMasterCode, _closeQqMasterBindingModal };`,
        context,
    );
    return { t: context.__t, created, confirms, calls, toasts, copied, bind };
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

function cardFor(appId = "111") {
    const card = makeRowEl();
    card.dataset.index = "0";
    card.fields = {
        ".qq-bot-appid": makeField(appId),
        ".qq-bot-secret": makeField(""),
        ".qq-bot-label": makeField(""),
    };
    return card;
}

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

    t.QQ_BOT_STATE.rows = [row({ app_id: "111" }), row()];
    const rows = JSON.parse(JSON.stringify(t._collectQqBotRows()));
    assert.equal(rows.length, 1);
    assert.equal(rows[0].app_id, "111");
    assert.equal(rows[0].app_secret, "typed-secret");
    assert.equal(rows[0].sandbox, false);
});

test("QQ 账号面板：重复 AppID 时整表请求体被拒", () => {
    const { t } = loadModule({ listEl: makeListEl(), statusEl: { textContent: "", className: "" } });
    t.QQ_BOT_STATE.enabled = true;
    t.QQ_BOT_STATE.rows = [row({ app_id: "111" }), row({ app_id: "111", app_secret: "b" })];
    assert.equal(t._qqBotRowsBody(), null);

    t.QQ_BOT_STATE.rows = [row({ app_id: "111", app_secret: "a", sandbox: true }), row({ app_id: "222" })];
    const body = JSON.parse(JSON.stringify(t._qqBotRowsBody()));
    assert.deepEqual(body, {
        enabled: true,
        accounts: [
            { app_id: "111", app_secret: "a", label: "", sandbox: false, enabled: true, owner_openids: [] },
            { app_id: "222", app_secret: "", label: "", sandbox: false, enabled: true, owner_openids: [] },
        ],
    });
});

test("QQ 账号面板：未注册显示注册Master，已注册显示注销 Master", () => {
    const listEl = makeListEl();
    const { t } = loadModule({ listEl, statusEl: { textContent: "", className: "" } });
    t.QQ_BOT_STATE.loaded = true;
    t.QQ_BOT_STATE.rows = [row({ app_id: "111", has_secret: true })];
    t.renderQqBotAccounts();
    let html = listEl.children[listEl.children.length - 1].innerHTML;
    assert.match(html, /data-action="register-master"[^>]*>注册Master</);
    assert.doesNotMatch(html, /unregister-master/);

    t.QQ_BOT_STATE.rows = [row({ app_id: "111", has_secret: true, owner_openids: ["EB6C8D4341C1238A627FF73CBE540DAE"] })];
    t.renderQqBotAccounts();
    html = listEl.children[listEl.children.length - 1].innerHTML;
    assert.match(html, /data-action="unregister-master"[^>]*>注销Master</);
    assert.doesNotMatch(html, /data-action="register-master"/);
});

test("QQ 账号面板：注册Master 先说明，再口令生成绑定码并展示等待窗", async () => {
    const listEl = makeListEl();
    const { t, confirms, calls, bind } = loadModule({ listEl, statusEl: { textContent: "", className: "" } });
    t.QQ_BOT_STATE.loaded = true;
    t.QQ_BOT_STATE.rows = [row({ app_id: "111", has_secret: true })];
    listEl.children = [cardFor("111")];
    t.renderQqBotAccounts();

    t._registerQqMaster(0);
    assert.equal(confirms.length, 1);
    assert.match(confirms[0].text, /owner 档/);
    assert.match(confirms[0].text, /5 分钟/);

    await confirms[0].onConfirm({});
    await new Promise((resolve) => setTimeout(resolve, 0));
    assert.equal(confirms.length, 2);
    assert.equal(confirms[1].input.type, "password");

    await confirms[1].onConfirm({ value: "owner-password" });
    await new Promise((resolve) => setTimeout(resolve, 0));
    assert.deepEqual(JSON.parse(JSON.stringify(calls.create)), [{
        app_id: "111",
        unlock_password: "owner-password",
    }]);
    assert.equal(bind.code.textContent, "NEGI-MASTER-AAAA-BBBB-CCCC-DDDD");
    assert.equal(bind.backdrop.hidden, false);
    assert.equal(bind.backdrop.classList.contains("is-open"), true);
    assert.equal(bind.statusText.textContent, "等待识别中…");
    assert.equal(t.QQ_MASTER_BIND.active, true);
    t._closeQqMasterBindingModal();
    assert.equal(bind.backdrop.hidden, true);
    assert.equal(t.QQ_MASTER_BIND.active, false);
});

test("QQ 账号面板：绑定码可复制，状态轮询用 app_id", async () => {
    const listEl = makeListEl();
    const { t, calls, toasts, copied, bind } = loadModule({
        listEl,
        statusEl: { textContent: "", className: "" },
        statusReply: { ok: true, registered: false, pending: true, expires_in_seconds: 120 },
    });
    t.QQ_BOT_STATE.loaded = true;
    t.QQ_BOT_STATE.rows = [row({ app_id: "111", has_secret: true })];
    listEl.children = [cardFor("111")];
    t.renderQqBotAccounts();

    t.openQqMasterBindingModal({
        appId: "111",
        token: "NEGI-MASTER-AAAA-BBBB-CCCC-DDDD",
        expiresInSeconds: 300,
    });
    assert.equal(bind.code.textContent, "NEGI-MASTER-AAAA-BBBB-CCCC-DDDD");

    await t._copyQqMasterCode();
    assert.deepEqual(copied, ["NEGI-MASTER-AAAA-BBBB-CCCC-DDDD"]);
    assert.match(toasts.at(-1).title, /已复制绑定码/);

    await t._pollQqMasterBinding();
    assert.ok(calls.status.length >= 1);
    assert.ok(calls.status.every((item) => item === "111"));
    assert.equal(t.QQ_MASTER_BIND.active, true);

    t._closeQqMasterBindingModal();
    assert.equal(t.QQ_MASTER_BIND.active, false);
});

test("QQ 账号面板：注销 Master 走二次确认并清空名单", async () => {
    const listEl = makeListEl();
    const { t, confirms, calls, toasts } = loadModule({
        listEl,
        statusEl: { textContent: "", className: "" },
        unregisterReply: {
            ok: true,
            enabled: true,
            accounts: [row({ app_id: "111", has_secret: true, owner_openids: [] })],
        },
    });
    t.QQ_BOT_STATE.loaded = true;
    t.QQ_BOT_STATE.rows = [row({ app_id: "111", has_secret: true, owner_openids: ["EB6C8D4341C1238A627FF73CBE540DAE"] })];
    listEl.children = [cardFor("111")];
    t.renderQqBotAccounts();

    t._unregisterQqMaster(0);
    assert.equal(confirms.length, 1);
    assert.equal(confirms[0].confirmKind, "danger");
    assert.match(confirms[0].text, /本地记忆重新只读/);

    await confirms[0].onConfirm({});
    assert.deepEqual(JSON.parse(JSON.stringify(calls.unregister)), [{ app_id: "111" }]);
    assert.match(toasts.at(-1).title, /已注销 Master/);
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
