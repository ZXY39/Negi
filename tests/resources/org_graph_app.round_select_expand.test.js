const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

// 轮次下拉框的展开状态必须活过任务树重绘：renderTree 会整块替换 U.tree，
// 下拉框身份（select.id → shell.dataset.selectId）一旦随机化，刷新就等于把它
// 关掉并把焦点甩掉。restoreOpenResourceSelect 按 S.openResourceSelectId 把它重新
// 打开，且只在它真的开着时动手。见 org_graph_task_view.js 的 treeRoundSelectId。

const APP_PATH = "g3ku/web/frontend/org_graph_app.js";
const APP_CODE = fs.readFileSync(APP_PATH, "utf8");

class HTMLElementStub {
    constructor() {
        this._classes = new Set();
        this.classList = {
            add: (name) => this._classes.add(name),
            remove: (name) => this._classes.delete(name),
            contains: (name) => this._classes.has(name),
            toggle: (name, on) => {
                if (on) this._classes.add(name);
                else this._classes.delete(name);
                return this._classes.has(name);
            },
        };
        this.dataset = {};
        this.attributes = {};
        this.style = {};
    }

    setAttribute(name, value) {
        this.attributes[name] = String(value);
    }

    getAttribute(name) {
        return this.attributes[name] ?? null;
    }

    focus() {
        this.focused = true;
    }
}

class HTMLSelectElementStub extends HTMLElementStub {
    constructor(id) {
        super();
        this.id = id;
        this.disabled = false;
        this.value = "round-2";
        this.options = [];
        this.selectedOptions = [];
    }
}

function makeOption(value) {
    const option = new HTMLElementStub();
    option.dataset.value = value;
    option.classList.add("is-selected");
    return option;
}

function makeShell(selectId, select) {
    const shell = new HTMLElementStub();
    shell.dataset.selectId = selectId;
    shell.trigger = new HTMLElementStub();
    shell.menu = new HTMLElementStub();
    shell.menu.hidden = true;
    shell.options = [makeOption(selectId)];
    select.shell = shell;
    shell.querySelector = (selector) => {
        if (selector === "select.resource-select") return select;
        if (selector === ".resource-select-trigger") return shell.trigger;
        if (selector === ".resource-select-menu") return shell.menu;
        return null;
    };
    shell.querySelectorAll = (selector) => (selector === ".resource-select-option" ? shell.options : []);
    shell.menu.querySelectorAll = (selector) => (selector === ".resource-select-option" ? shell.options : []);
    shell.closest = (selector) => (selector === ".resource-select-shell" ? shell : null);
    return shell;
}

function makeContext() {
    const shells = new Map();
    const container = {
        querySelector: (selector) => {
            const match = /^\.resource-select-shell\[data-select-id="([^"]+)"\]$/.exec(selector);
            if (!match) return null;
            return shells.get(match[1]) || null;
        },
    };
    const document = {
        activeElement: null,
        querySelectorAll: (selector) => {
            if (selector === ".resource-select-shell.is-open") {
                return [...shells.values()].filter((shell) => shell.classList.contains("is-open"));
            }
            return [];
        },
    };
    const state = { openResourceSelectId: "" };
    const context = {
        console,
        document,
        container,
        S: state,
        HTMLElement: HTMLElementStub,
        HTMLSelectElement: HTMLSelectElementStub,
        resourceSelectLabel: (select) => `label:${select.id}`,
        addShell: (selectId) => {
            const select = new HTMLSelectElementStub(selectId);
            select.classList.add("resource-select");
            select.closest = (selector) => (selector === ".resource-select-shell" ? shell : null);
            const shell = makeShell(selectId, select);
            shells.set(selectId, shell);
            return { shell, select };
        },
    };
    context.window = context;
    vm.createContext(context);
    const start = APP_CODE.indexOf("function closeResourceSelects(");
    const end = APP_CODE.indexOf("function enhanceResourceSelects()");
    assert.ok(start > 0 && end > start, "resource-select slice not found");
    vm.runInContext(APP_CODE.slice(start, end), context);
    return context;
}

test("整树重绘后，正开着的轮次下拉框被重新打开并把焦点还给选中项", () => {
    const context = makeContext();
    const { shell, select } = context.addShell("tree-round-node-a");
    context.openResourceSelect(select);
    assert.equal(shell.classList.contains("is-open"), true);
    assert.equal(shell.menu.hidden, false);

    // 模拟 renderTree：节点重建出一个同 id 的新 shell，旧的连同 is-open 一起消失。
    const rebuilt = context.addShell("tree-round-node-a");
    assert.equal(rebuilt.shell.classList.contains("is-open"), false);
    assert.equal(context.restoreOpenResourceSelect(context.container), true);
    assert.equal(rebuilt.shell.classList.contains("is-open"), true);
    assert.equal(rebuilt.shell.menu.hidden, false);
    assert.equal(rebuilt.shell.trigger.getAttribute("aria-expanded"), "true");
    assert.equal(rebuilt.shell.options[0].focused, true);
    assert.equal(context.S.openResourceSelectId, "tree-round-node-a");
});

test("没有展开态时 restore 不动任何下拉框", () => {
    const context = makeContext();
    const { shell } = context.addShell("tree-round-node-a");
    assert.equal(context.restoreOpenResourceSelect(context.container), false);
    assert.equal(shell.classList.contains("is-open"), false);
});

test("选完值即收起：关闭后的重绘不会把它重新弹开", () => {
    const context = makeContext();
    const { shell, select } = context.addShell("tree-round-node-a");
    context.openResourceSelect(select);
    context.closeResourceSelects({ restoreFocus: true });
    assert.equal(context.S.openResourceSelectId, "");
    assert.equal(shell.trigger.focused, true);
    const rebuilt = context.addShell("tree-round-node-a");
    assert.equal(context.restoreOpenResourceSelect(context.container), false);
    assert.equal(rebuilt.shell.classList.contains("is-open"), false);
});

test("节点从树上消失：静默跳过，不误开其他下拉框", () => {
    const context = makeContext();
    context.addShell("tree-round-node-a");
    context.S.openResourceSelectId = "tree-round-gone";
    assert.equal(context.restoreOpenResourceSelect(context.container), false);
    assert.equal(context.S.openResourceSelectId, "tree-round-gone");
});

test("真实 id 让同树互斥生效：打开第二个会关掉第一个", () => {
    const context = makeContext();
    const first = context.addShell("tree-round-node-a");
    const second = context.addShell("tree-round-node-b");
    context.openResourceSelect(first.select);
    context.openResourceSelect(second.select);
    assert.equal(first.shell.classList.contains("is-open"), false);
    assert.equal(first.shell.menu.hidden, true);
    assert.equal(second.shell.classList.contains("is-open"), true);
    assert.equal(context.S.openResourceSelectId, "tree-round-node-b");
});
