"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const { FRONTEND_DIR } = require("./support/frontend_sources.js");
const { StubDocument, StubHTMLElement } = require("./support/frontend_stub.js");

const SANDBOX_CODE = fs.readFileSync(path.join(FRONTEND_DIR, "sandbox.js"), "utf8");

// 沙箱页的"进/出沙箱"按钮契约：装了不等于接管；点了才切；jail 不可用时显示"沙箱失效"，
// 且必须在受限模式下才出现。

const IDS = [
    "sandbox-jail-btn",
    "session-panel",
    "session-trust",
    "session-tier",
    "mode-switch",
    "roots-field",
    "roots-input",
    "save-mode-btn",
    "save-result",
    "files-card",
    "files-body",
    "files-unjailed",
    "files-unjailed-hint",
    "files-hint",
    "copy-workspace-btn",
    "file-list",
    "files-empty",
    "area-tabs",
    "manual-session",
    "toast",
    "mode-confirm-backdrop",
    "mode-confirm-title",
    "mode-confirm-impact",
    "mode-confirm-password",
    "mode-confirm-error",
    "mode-confirm-accept",
];

function loadSandbox() {
    const document = new StubDocument();
    for (const id of IDS) document._byId[id] = new StubHTMLElement("DIV");
    const calls = [];
    const context = {
        console,
        document,
        navigator: { clipboard: { writeText: async () => {} } },
        Element: StubHTMLElement,
        HTMLElement: StubHTMLElement,
        URLSearchParams,
        location: { search: "" },
        isSecureContext: false,
        addEventListener: () => {},
        setTimeout: (fn) => setTimeout(fn, 0),
        clearTimeout: (handle) => clearTimeout(handle),
        requestAnimationFrame: (fn) => fn(),
        fetch: async (url, options = {}) => {
            calls.push({ url: String(url), body: options.body ? JSON.parse(options.body) : null });
            return {
                ok: true,
                status: 200,
                json: async () => ({
                    ok: true,
                    item: {
                        session_id: "ext:qq-official-1:abc",
                        mode: "restricted",
                        sandbox_enabled: true,
                        sandbox_failed: false,
                        runs_in_jail: true,
                        install_state: "ready",
                        resolved_tier: "jailed",
                    },
                }),
            };
        },
    };
    context.window = context;
    vm.createContext(context);
    vm.runInContext(SANDBOX_CODE, context);
    return { context, document, hooks: context.__sandboxTestHooks, calls };
}

function mount(hooks, item, status = { install_state: "ready", file_management_available: true }) {
    hooks.state.current = item;
    hooks.state.status = status;
    hooks.state.sessionId = item.session_id;
}

function item(overrides = {}) {
    return {
        session_id: "ext:qq-official-1:abc",
        trust: "third_party_group",
        mode: "restricted",
        sandbox_enabled: false,
        sandbox_failed: false,
        runs_in_jail: false,
        resolved_tier: "limited",
        session_workspace: "E:\\Negi Data\\session-workspaces\\x",
        ...overrides,
    };
}

test("受限档 + 已安装：显示「切换沙箱」", () => {
    const { document, hooks } = loadSandbox();
    mount(hooks, item());
    hooks.renderSandboxJailButton(hooks.state.current);
    const button = document._byId["sandbox-jail-btn"];
    assert.equal(button.hidden, false);
    assert.equal(button.textContent, "切换沙箱");
    assert.equal(button.disabled, false);
});

test("未安装：沙箱按钮不显示", () => {
    const { document, hooks } = loadSandbox();
    mount(hooks, item(), { install_state: "not_installed" });
    hooks.renderSandboxJailButton(hooks.state.current);
    assert.equal(document._byId["sandbox-jail-btn"].hidden, true);
});

test("沙箱意图 + 真在 jail：显示「退出沙箱」", () => {
    const { document, hooks } = loadSandbox();
    mount(hooks, item({ sandbox_enabled: true, runs_in_jail: true, resolved_tier: "jailed" }));
    hooks.renderSandboxJailButton(hooks.state.current);
    const button = document._byId["sandbox-jail-btn"];
    assert.equal(button.textContent, "退出沙箱");
    assert.equal(button.disabled, false);
});

test("沙箱意图 + jail 不可用：显示「沙箱失效」且不可点", () => {
    const { document, hooks } = loadSandbox();
    mount(hooks, item({ sandbox_enabled: true, sandbox_failed: true }));
    hooks.renderSandboxJailButton(hooks.state.current);
    const button = document._byId["sandbox-jail-btn"];
    assert.equal(button.textContent, "沙箱失效");
    assert.equal(button.disabled, true);
});

test("全权限档不出现沙箱按钮", () => {
    const { document, hooks } = loadSandbox();
    mount(hooks, item({ mode: "unrestricted", resolved_tier: "host_full" }));
    hooks.renderSandboxJailButton(hooks.state.current);
    assert.equal(document._byId["sandbox-jail-btn"].hidden, true);
});

test("进入沙箱的确认弹窗写清后果，确认后只发 sandbox 字段", async () => {
    const { document, hooks, calls } = loadSandbox();
    mount(hooks, item());
    hooks.openSandboxConfirm(true);
    assert.equal(hooks.state.confirmKind, "sandbox");
    assert.equal(document._byId["mode-confirm-title"].textContent, "进入沙箱模式？");
    assert.equal(document._byId["mode-confirm-accept"].textContent, "进入沙箱模式");
    assert.equal(document._byId["mode-confirm-backdrop"].hidden, false);
    assert.match(document._byId["mode-confirm-impact"].children.map((row) => row.children[1].textContent).join(""),
        /沙箱内执行/);

    document._byId["mode-confirm-password"].value = "right";
    await hooks.applyModeChange();
    const patchCalls = calls.filter((call) => call.body && "sandbox" in call.body);
    assert.equal(patchCalls.length, 1);
    assert.match(patchCalls[0].url, /\/sandbox$/);
    assert.deepEqual(patchCalls[0].body, { sandbox: true, unlock_password: "right" });
    assert.equal(document._byId["mode-confirm-backdrop"].hidden, true);
});

test("退出沙箱的确认文案说明回到受限模式", async () => {
    const { document, hooks, calls } = loadSandbox();
    mount(hooks, item({ sandbox_enabled: true, runs_in_jail: true, resolved_tier: "jailed" }));
    hooks.openSandboxConfirm(false);
    assert.equal(document._byId["mode-confirm-title"].textContent, "退出沙箱？");
    assert.match(
        document._byId["mode-confirm-impact"].children.map((row) => row.children[1].textContent).join(""),
        /宿主/,
    );

    document._byId["mode-confirm-password"].value = "right";
    await hooks.applyModeChange();
    const patchCalls = calls.filter((call) => call.body && "sandbox" in call.body);
    assert.deepEqual(patchCalls[0].body, { sandbox: false, unlock_password: "right" });
});

test("徽标行：沙箱按钮与信任/档位药丸同一基准线（居中、同高）", () => {
    const css = fs.readFileSync(path.join(FRONTEND_DIR, "sandbox.css"), "utf8");
    const meta = (css.match(/\.session-meta \{([^}]*)\}/) || [])[1] || "";
    assert.match(meta, /align-items:\s*center/);
    const button = (css.match(/\.session-meta \.toolbar-btn \{([^}]*)\}/) || [])[1] || "";
    assert.match(button, /height:\s*28px/);
    assert.match(button, /min-height:\s*28px/);
});
