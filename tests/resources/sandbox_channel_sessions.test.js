"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const { FRONTEND_DIR } = require("./support/frontend_sources.js");
const { StubDocument, StubHTMLElement } = require("./support/frontend_stub.js");

const SANDBOX_CODE = fs.readFileSync(path.join(FRONTEND_DIR, "sandbox.js"), "utf8");

// 沙箱页会话下拉契约：`/api/ceo/sessions` 把会话拆成 `items`（只有 `web:` 本地会话）和
// `channel_groups[].items`（渠道会话，含官方 QQ 的 `ext:`）。只读前者会让第三方会话在
// 档位面板里彻底消失——而 `GET/PATCH /api/ceo/sessions/{id}/sandbox` 对 `ext:` 是有效的。

function catalogPayload() {
    return {
        ok: true,
        items: [
            { session_id: "web:ceo-c2e165e9762a", title: "实盘探针A", session_family: "local" },
        ],
        channel_groups: [
            {
                channel_id: "ext:qq-official-1903529517",
                label: "QQ",
                items: [
                    {
                        session_id: "ext:qq-official-1903529517:83765909cf75d3a9",
                        title: "QQ · qq:group:48A6F16E277B61B6",
                        session_family: "channel",
                        trust: "third_party_group",
                        chat_type: "group",
                    },
                ],
            },
            {
                channel_id: "ext:qq-official-1905678192",
                label: "私聊号",
                items: [
                    {
                        session_id: "ext:qq-official-1905678192:971aec8f39139d18",
                        title: "私聊号 · qq:c2c:A73DE6610A4C2793",
                        session_family: "channel",
                        trust: "third_party_dm",
                        chat_type: "dm",
                    },
                    { title: "没有会话键的行", session_id: "  " },
                ],
            },
        ],
        active_session_id: "web:ceo-c2e165e9762a",
        active_session_family: "local",
    };
}

function loadSandbox(extraIds = {}) {
    const document = new StubDocument();
    for (const id of [
        "session-select",
        "session-select-trigger",
        "session-select-menu",
        "session-select-label",
        "manual-session",
        "manual-session-field",
        "toast",
        ...Object.keys(extraIds),
    ]) {
        document._byId[id] = new StubHTMLElement(id.includes("manual-session-") ? "INPUT" : "DIV");
    }
    for (const [id, node] of Object.entries(extraIds)) document._byId[id] = node;

    const context = {
        console,
        document,
        navigator: { clipboard: {} },
        Element: StubHTMLElement,
        HTMLElement: StubHTMLElement,
        URLSearchParams,
        fetch: async () => ({ ok: true, status: 200, json: async () => catalogPayload() }),
        location: { search: "" },
        isSecureContext: false,
        addEventListener: () => {},
        setTimeout: (fn) => setTimeout(fn, 0),
        clearTimeout: (handle) => clearTimeout(handle),
        requestAnimationFrame: (fn) => fn(),
    };
    // 页面脚本把测试钩子挂在 window 上，因此 window 就是脚本的全局对象本身。
    context.window = context;
    vm.createContext(context);
    vm.runInContext(SANDBOX_CODE, context);
    return { context, document, hooks: context.__sandboxTestHooks };
}

// vm 里造出的数组带着沙箱执行域的 Array 原型，deepStrictEqual 会因原型判不同；
// 断言前先跨域取回普通值。
function ids(options) {
    return Array.from(options || []).map((item) => item.id);
}

test("会话目录的两个容器都要进下拉：渠道会话（ext:）不许被 items 挡在外面", () => {
    const { hooks } = loadSandbox();
    const options = hooks.collectSessionOptions(catalogPayload());
    assert.deepEqual(ids(options), [
        "web:ceo-c2e165e9762a",
        "ext:qq-official-1903529517:83765909cf75d3a9",
        "ext:qq-official-1905678192:971aec8f39139d18",
    ]);
    assert.equal(options[1].title, "QQ · qq:group:48A6F16E277B61B6");
});

test("载荷缺容器、容器缺数组或键为空时不许抛错，也不许造出空选项", () => {
    const { hooks } = loadSandbox();
    assert.deepEqual(ids(hooks.collectSessionOptions({})), []);
    assert.deepEqual(ids(hooks.collectSessionOptions(null)), []);
    assert.deepEqual(ids(hooks.collectSessionOptions({ items: {}, channel_groups: {} })), []);
    assert.deepEqual(ids(hooks.collectSessionOptions({ items: [{ title: "x" }], channel_groups: [{}] })), []);
    assert.deepEqual(
        ids(hooks.collectSessionOptions({
            items: [{ session_id: "web:a" }],
            channel_groups: [{ items: [{ session_id: "web:a" }, { session_id: "ext:b" }] }],
        })),
        ["web:a", "ext:b"],
    );
});

test("loadSessions 走真实取数路径后，QQ 会话可选中且不再退化成手填", async () => {
    const { document, hooks } = loadSandbox();
    hooks.state.preset = "ext:qq-official-1905678192:971aec8f39139d18";
    await hooks.loadSessions();

    assert.equal(hooks.state.sessions.length, 3);
    assert.equal(hooks.state.sessionId, "ext:qq-official-1905678192:971aec8f39139d18");
    assert.equal(document._byId["manual-session-field"].hidden, true);
    const labels = document._byId["session-select-menu"].children.map((node) => node.children[0].textContent);
    assert.equal(labels.length, 3);
    assert.match(labels[1], /^QQ · qq:group:.+ · ext:qq-official-1903529517:/);
    assert.equal(document._byId["session-select-label"].textContent,
        "私聊号 · qq:c2c:A73DE6610A4C2793 · ext:qq-official-1905678192:971aec8f39139d18");
});
