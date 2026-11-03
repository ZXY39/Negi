"use strict";

// 提交输出 / 验收结果右上角那颗复制按钮的契约：
// - 框里没有正文（空态占位串）时不画按钮，摘掉的是 hidden 而不是"点了报错"；
// - 有正文时按钮回来，复制的就是框内那份文本，判空与复制同源；
// - 换节点从有正文回到空态要把按钮摘回去，不能只加不减；
// - CSS 侧不画边框也不画底，只留图标。

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const FRONTEND = path.join(__dirname, "..", "..", "g3ku", "web", "frontend");
const DETAIL_CODE = fs.readFileSync(path.join(FRONTEND, "tasks", "node-detail.js"), "utf8")
    .replace(/\r\n/g, "\n");
const APP_CODE = fs.readFileSync(path.join(FRONTEND, "org_graph_app.js"), "utf8")
    .replace(/\r\n/g, "\n");
const CSS_CODE = fs.readFileSync(path.join(FRONTEND, "org_graph.css"), "utf8")
    .replace(/\r\n/g, "\n");

// 取数车道那一支：countMatches → readableText 连续一段，判空规则用生产那份，
// 测试自己抄一份 emptyText 映射就证不了"空态"到底由谁定义。
function sliceReadableTextLane() {
    const start = APP_CODE.indexOf("function countMatches(");
    assert.ok(start >= 0, "app bundle 里找不到 countMatches");
    const readableStart = APP_CODE.indexOf("function readableText(");
    assert.ok(readableStart > start, "app bundle 里找不到 readableText");
    const end = APP_CODE.indexOf("\n}\n", readableStart);
    assert.ok(end > readableStart, "readableText 的结尾无法定位");
    return APP_CODE.slice(start, end + 2);
}

class StubField {
    constructor(id) {
        this.id = id;
        this.textContent = "";
    }
}

class StubButton {
    constructor(id) {
        this.id = id;
        this.hidden = false;
        this.dataset = {};
        this._innerHTML = "";
        this._listeners = {};
        this.classList = {
            add: () => {},
            remove: () => {},
            toggle: () => {},
            contains: () => false,
        };
    }

    get innerHTML() {
        return this._innerHTML;
    }

    set innerHTML(value) {
        this._innerHTML = String(value);
    }

    addEventListener(type, handler) {
        (this._listeners[type] = this._listeners[type] || []).push(handler);
    }

    async click() {
        const event = { preventDefault: () => {}, stopPropagation: () => {} };
        await Promise.all((this._listeners.click || []).map((handler) => handler(event)));
    }
}

function load({ copiedTexts = [], toasts = [] } = {}) {
    const fields = {
        "ad-output": new StubField("ad-output"),
        "ad-check": new StubField("ad-check"),
    };
    const buttons = {
        "ad-output-copy": new StubButton("ad-output-copy"),
        "ad-check-copy": new StubButton("ad-check-copy"),
    };
    const elements = { ...fields, ...buttons };
    const context = {
        console,
        esc: (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({
            "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
        }[char])),
        HTMLButtonElement: StubButton,
        HTMLElement: StubField,
        window: {
            setTimeout: () => 1,
            clearTimeout: () => {},
        },
        document: {
            getElementById: (id) => elements[id] || null,
            querySelector: () => null,
            querySelectorAll: () => [],
        },
        U: { adOutput: fields["ad-output"], adAcceptance: fields["ad-check"] },
        copyTextToClipboard: async (text) => {
            copiedTexts.push(text);
            return true;
        },
        showToast: (toast) => toasts.push(toast),
    };
    vm.createContext(context);
    vm.runInContext(`${sliceReadableTextLane()}\n${DETAIL_CODE}`, context);
    vm.runInContext("bindTaskOutputCopyActions();", context);
    return { context, fields, buttons };
}

test("空态不画复制按钮，有正文才画回来", () => {
    const { context, fields, buttons } = load();

    vm.runInContext('renderFinalOutput(""); renderAcceptanceResult("");', context);
    assert.equal(fields["ad-output"].textContent, "暂无提交输出");
    assert.equal(fields["ad-check"].textContent, "暂无验收结果");
    assert.equal(buttons["ad-output-copy"].hidden, true, "提交输出为空时不该有复制按钮");
    assert.equal(buttons["ad-check-copy"].hidden, true, "验收结果为空时不该有复制按钮");

    vm.runInContext('renderFinalOutput("已交付 3 条链接"); renderAcceptanceResult("通过");', context);
    assert.equal(buttons["ad-output-copy"].hidden, false);
    assert.equal(buttons["ad-check-copy"].hidden, false);

    // 换节点：正文收回空态，按钮必须跟着摘回去。
    vm.runInContext('renderFinalOutput("");', context);
    assert.equal(buttons["ad-output-copy"].hidden, true);
    assert.equal(buttons["ad-check-copy"].hidden, false, "另一格仍有正文，不受影响");
});

test("复制按钮拿的是框内全文：判空与复制同一份读数", async () => {
    const copiedTexts = [];
    const toasts = [];
    const { context, fields, buttons } = load({ copiedTexts, toasts });

    vm.runInContext('renderFinalOutput("提交输出全文");', context);
    assert.equal(fields["ad-output"].textContent, "提交输出全文");
    await buttons["ad-output-copy"].click();
    assert.deepEqual(copiedTexts, ["提交输出全文"]);
    assert.deepEqual(toasts.map((toast) => toast.title), ["已复制"]);
});

test("复制按钮只剩图标：CSS 不画边框也不画底，hidden 才真隐藏", () => {
    const rule = CSS_CODE.match(/\.task-output-copy\s*\{([^}]*)\}/);
    assert.ok(rule, "找不到 .task-output-copy 规则");
    const body = rule[1];
    assert.match(body, /border:\s*0/);
    assert.match(body, /background:\s*none/);
    assert.doesNotMatch(body, /1px solid/);
    assert.doesNotMatch(body, /border-color:/);

    const hover = CSS_CODE.match(/\.task-output-copy:hover\s*\{([^}]*)\}/);
    assert.ok(hover, "找不到 .task-output-copy:hover 规则");
    assert.match(hover[1], /background:\s*none/);
    assert.match(hover[1], /border:\s*0/);

    // .icon-btn 给的是 display:flex，会盖掉 UA 的 [hidden]{display:none}。
    const hidden = CSS_CODE.match(/\.task-output-copy\[hidden\]\s*\{([^}]*)\}/);
    assert.ok(hidden, "找不到 .task-output-copy[hidden] 规则");
    assert.match(hidden[1], /display:\s*none\s*!important/);
});
