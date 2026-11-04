"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const { FRONTEND_DIR } = require("./support/frontend_sources.js");
const { StubDocument, StubHTMLElement } = require("./support/frontend_stub.js");

const SANDBOX_CODE = fs.readFileSync(path.join(FRONTEND_DIR, "sandbox.js"), "utf8");

// 沙箱文件卡契约：卷只属于真在 jail 里跑的会话。宿主受限档（tier=limited）的文件写在
// session-workspaces，列卷只会呈现一个空壳——卡片必须改给宿主落点，而不是列表。

const FILE_PANEL_IDS = [
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
];

function loadSandbox() {
    const document = new StubDocument();
    for (const id of FILE_PANEL_IDS) document._byId[id] = new StubHTMLElement("DIV");
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
        fetch: async (url) => {
            calls.push(String(url));
            return {
                ok: true,
                status: 200,
                json: async () => ({ ok: true, item: { entries: [{ kind: "file", name: "a.txt", size: 3 }] } }),
            };
        },
    };
    context.window = context;
    vm.createContext(context);
    vm.runInContext(SANDBOX_CODE, context);
    return { context, document, hooks: context.__sandboxTestHooks, calls };
}

function mount(hooks, { runsInJail, fileManagement = true, workspace = "E:\\Negi Data\\session-workspaces\\ext-1" }) {
    hooks.state.current = {
        session_id: "ext:qq-official-1:abc",
        trust: "third_party_group",
        mode: "restricted",
        runs_in_jail: runsInJail,
        session_workspace: workspace,
    };
    hooks.state.status = { install_state: "ready", file_management_available: fileManagement };
    hooks.state.sessionId = "ext:qq-official-1:abc";
}

test("宿主受限档：不列卷，给出宿主工作目录与复制入口", async () => {
    const { document, hooks, calls } = loadSandbox();
    mount(hooks, { runsInJail: false });
    hooks.renderFileSection();

    assert.equal(document._byId["files-card"].hidden, false);
    assert.equal(document._byId["files-body"].hidden, true);
    assert.equal(document._byId["files-unjailed"].hidden, false);
    assert.match(document._byId["files-unjailed-hint"].textContent, /宿主受限档/);
    assert.match(document._byId["files-unjailed-hint"].textContent, /session-workspaces/);
    assert.equal(document._byId["copy-workspace-btn"].disabled, false);
    assert.deepEqual(calls, []);
});

test("jailed 会话：列卷并保持原有三区入口", async () => {
    const { document, hooks, calls } = loadSandbox();
    mount(hooks, { runsInJail: true });
    hooks.renderFileSection();
    await new Promise((resolve) => setTimeout(resolve, 5));

    assert.equal(document._byId["files-body"].hidden, false);
    assert.equal(document._byId["files-unjailed"].hidden, true);
    assert.equal(calls.length, 1);
    assert.match(calls[0], /\/sandbox\/files\?area=workspace$/);
    assert.equal(document._byId["file-list"].children.length, 1);
});

test("装了但 jail 后端没接进执行道：不冒充可用，也不列空卷", async () => {
    const { document, hooks, calls } = loadSandbox();
    mount(hooks, { runsInJail: true, fileManagement: false });
    hooks.renderFileSection();

    assert.equal(document._byId["files-body"].hidden, true);
    assert.equal(document._byId["files-unjailed"].hidden, false);
    assert.match(document._byId["files-unjailed-hint"].textContent, /还没接进执行道/);
    assert.deepEqual(calls, []);
});

test("没有宿主落点时复制按钮不可点", async () => {
    const { document, hooks } = loadSandbox();
    mount(hooks, { runsInJail: false, workspace: "" });
    hooks.renderFileSection();

    assert.equal(document._byId["copy-workspace-btn"].disabled, true);
    assert.match(document._byId["files-unjailed-hint"].textContent, /路径暂不可用/);
});
