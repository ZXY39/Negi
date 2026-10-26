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

function loadModule({ listEl, statusEl }) {
    const created = [];
    const context = {
        console,
        setTimeout,
        clearTimeout,
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
        showToast: () => {},
        ApiClient: {
            getQqBotSettings: async () => ({ enabled: true, accounts: [] }),
            updateQqBotSettings: async (body) => body,
        },
    };
    context.window = context;
    vm.createContext(context);
    vm.runInContext(
        `${CODE}
        this.__t = { QQ_BOT_STATE, renderQqBotAccounts, _collectQqBotRows, _qqBotRowsBody, _qqBotRowStatus };`,
        context,
    );
    return { t: context.__t, created };
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
            { app_id: "111", app_secret: "a", label: "", sandbox: false, enabled: true },
            { app_id: "222", app_secret: "", label: "", sandbox: false, enabled: true },
        ],
    });
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
    // 标题右侧是无边框编辑键，点它就地改名（不再开弹窗）。
    assert.match(html, /<button class="qq-bot-account-edit"[^>]*data-action="edit-label"/);
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
