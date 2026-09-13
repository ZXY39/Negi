const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const TASK_VIEW_PATH = "g3ku/web/frontend/org_graph_task_view.js";
const APP_PATH = "g3ku/web/frontend/org_graph_app.js";
const TASK_VIEW_CODE = fs.readFileSync(TASK_VIEW_PATH, "utf8");
const APP_CODE = fs.readFileSync(APP_PATH, "utf8");

class StubElement {}
class StubHTMLElement extends StubElement {
    constructor() {
        super();
        this.hidden = false;
        this.textContent = "";
        this.innerHTML = "";
        this.className = "";
        this.dataset = {};
        this.style = {};
        this.attributes = {};
        this.children = [];
        this.parentElement = null;
        this.classList = {
            add() {},
            remove() {},
            contains() { return false; },
            toggle() { return false; },
        };
    }

    querySelector(selector) {
        const wanted = String(selector || "").replace(/^\./, "");
        for (const child of this.children) {
            if (String(child.className || "").split(/\s+/).includes(wanted)) return child;
            const nested = child.querySelector ? child.querySelector(selector) : null;
            if (nested) return nested;
        }
        return null;
    }

    querySelectorAll() {
        return [];
    }

    closest(selector) {
        const wanted = String(selector || "").replace(/^\./, "");
        let node = this;
        while (node) {
            if (String(node.className || "").split(/\s+/).includes(wanted)) return node;
            node = node.parentElement;
        }
        return null;
    }

    contains(node) {
        if (node === this) return true;
        return this.children.some((child) => child.contains && child.contains(node));
    }

    addEventListener(type, handler) {
        this._listeners = this._listeners || {};
        (this._listeners[type] = this._listeners[type] || []).push(handler);
    }

    appendChild(child) {
        child.parentElement = this;
        this.children.push(child);
        return child;
    }

    setAttribute(name, value) {
        this.attributes[name] = String(value);
    }

    removeAttribute(name) {
        delete this.attributes[name];
    }
}
class StubHTMLButtonElement extends StubHTMLElement {}
class StubHTMLInputElement extends StubHTMLElement {}
class StubHTMLTextAreaElement extends StubHTMLElement {}
class StubHTMLSelectElement extends StubHTMLElement {}

class StubDocument {
    getElementById() { return null; }
    createElement() { return new StubHTMLElement(); }
    querySelector() { return null; }
    querySelectorAll() { return []; }
    addEventListener() {}
}

function loadApp({ apiClient = null } = {}) {
    const copiedTexts = [];
    // 可控定时器队列:flash 恢复图标用的 setTimeout 不自动触发,
    // 测试里显式 fire 来验证"短暂反馈后恢复"的完整时序。
    const pendingTimers = [];
    const context = {
        console,
        setTimeout: (fn) => {
            pendingTimers.push(fn);
            return pendingTimers.length;
        },
        clearTimeout: (id) => {
            const index = Number(id) - 1;
            if (index >= 0) pendingTimers[index] = null;
        },
        setInterval,
        clearInterval,
        queueMicrotask,
        navigator: { clipboard: { writeText: async (text) => { copiedTexts.push(String(text || "")); } } },
        location: { protocol: "http:", host: "localhost", pathname: "/org_graph.html" },
        localStorage: { getItem: () => null, setItem: () => {}, removeItem: () => {} },
        sessionStorage: { getItem: () => null, setItem: () => {}, removeItem: () => {} },
        document: new StubDocument(),
        window: {},
        Element: StubElement,
        HTMLElement: StubHTMLElement,
        HTMLButtonElement: StubHTMLButtonElement,
        HTMLInputElement: StubHTMLInputElement,
        HTMLTextAreaElement: StubHTMLTextAreaElement,
        HTMLSelectElement: StubHTMLSelectElement,
        URLSearchParams,
        URL,
        AbortController,
        fetch: async () => ({ ok: true, json: async () => ({}) }),
        lucide: { createIcons() {} },
        marked: { parse: (value) => String(value) },
        DOMPurify: { sanitize: (value) => String(value) },
        structuredClone: global.structuredClone,
        performance: { now: () => 0 },
        requestAnimationFrame: (callback) => {
            callback();
            return 1;
        },
        cancelAnimationFrame: () => {},
        WebSocket: function WebSocket() {},
        addEventListener() {},
        removeEventListener() {},
    };
    if (apiClient && typeof apiClient === "object") context.ApiClient = apiClient;
    context.window = context;
    vm.createContext(context);
    vm.runInContext(
        `${TASK_VIEW_CODE}\n${APP_CODE}\nthis.__testExports = { renderTraceField, renderTraceOutputField, bindTraceFieldCopyActions };`,
        context
    );
    const fireTimers = () => {
        // 逐个执行排队回调,null 槽位跳过;回调内可能继续排队,循环处理到空。
        let index = 0;
        while (index < pendingTimers.length) {
            const fn = pendingTimers[index];
            pendingTimers[index] = null;
            index += 1;
            if (typeof fn === "function") fn();
        }
    };
    return { ...context.__testExports, copiedTexts, fireTimers };
}

function buildCopySetup({ text = "hello", emptyText = "", outputRef = "", hydrated, apiClient = null } = {}) {
    const { renderTraceOutputField, renderTraceField, bindTraceFieldCopyActions, copiedTexts, fireTimers } = loadApp({ apiClient });
    const listEl = new StubHTMLElement();
    listEl.className = "task-trace-list";
    const field = new StubHTMLElement();
    field.className = "task-trace-field";
    const label = new StubHTMLElement();
    label.className = "task-trace-label";
    label.textContent = "工具输出";
    const code = new StubHTMLElement();
    code.className = "code-block task-trace-code";
    code.textContent = text;
    code.dataset.emptyText = emptyText;
    if (outputRef) code.dataset.outputRef = outputRef;
    if (typeof hydrated === "string") code.dataset.outputHydrated = hydrated;
    const button = new StubHTMLButtonElement();
    button.className = "task-trace-copy";
    field.appendChild(label);
    field.appendChild(code);
    field.appendChild(button);
    listEl.appendChild(field);
    bindTraceFieldCopyActions(listEl);
    const dispatchClick = () => {
        const handler = (listEl._listeners && listEl._listeners.click) ? listEl._listeners.click[0] : null;
        assert.ok(typeof handler === "function", "copy delegation listener should be bound");
        handler({ target: button, preventDefault() {}, stopPropagation() {} });
    };
    return { listEl, field, label, code, button, copiedTexts, fireTimers, dispatchClick, renderTraceField, renderTraceOutputField };
}

async function flush() {
    await new Promise((resolve) => setTimeout(resolve, 10));
}

test("renderTraceField adds a copy button next to the label only when copyable", () => {
    const { renderTraceField } = loadApp();

    const withCopy = renderTraceField("参数", "git status", "无参数", { copyable: true });
    assert.match(withCopy, /class="task-trace-label-row"/);
    assert.match(withCopy, /class="task-trace-copy"/);
    assert.match(withCopy, /aria-label="复制 参数"/);
    assert.match(withCopy, /data-empty-text="无参数"/);

    const withoutCopy = renderTraceField("参数", "git status", "无参数");
    assert.doesNotMatch(withoutCopy, /task-trace-copy/);
    assert.match(withoutCopy, /task-trace-label-row/);
});

test("renderTraceOutputField keeps data-empty-text with and without an output ref", () => {
    const { renderTraceOutputField } = loadApp();

    const withRef = renderTraceOutputField("工具输出", "preview", "note:42", "暂无工具输出", { copyable: true });
    assert.match(withRef, /data-output-ref="note:42"/);
    assert.match(withRef, /data-empty-text="暂无工具输出"/);
    assert.match(withRef, /task-trace-copy/);

    const withoutRef = renderTraceOutputField("工具输出", "plain text", "", "暂无工具输出", { copyable: true });
    assert.doesNotMatch(withoutRef, /data-output-ref/);
    assert.match(withoutRef, /data-empty-text="暂无工具输出"/);
});

test("copy delegation copies the displayed output text", async () => {
    const setup = buildCopySetup({ text: "line 1 of output\nline 2 of output", emptyText: "暂无工具输出" });
    setup.dispatchClick();
    await flush();

    assert.deepEqual(setup.copiedTexts, ["line 1 of output\nline 2 of output"]);
    assert.equal(setup.button.innerHTML, '<i data-lucide="check"></i>');
    setup.fireTimers();
    assert.equal(setup.button.innerHTML, '<i data-lucide="copy"></i>');
});

test("copy delegation skips placeholder empty text without writing", async () => {
    const setup = buildCopySetup({ text: "暂无工具输出", emptyText: "暂无工具输出" });
    setup.dispatchClick();
    await flush();

    assert.equal(setup.copiedTexts.length, 0);
    assert.equal(setup.button.innerHTML, '<i data-lucide="x"></i>');
    setup.fireTimers();
    assert.equal(setup.button.innerHTML, '<i data-lucide="copy"></i>');
});

test("copy on a lazy output block hydrates the full output before copying", async () => {
    const hydratedText = "hydrated full output\nsecond line";
    const setup = buildCopySetup({
        text: "preview text",
        emptyText: "暂无工具输出",
        outputRef: "ref:demo/out.log",
        hydrated: "false",
        apiClient: { readContent: async () => ({ content: hydratedText }) },
    });
    setup.dispatchClick();
    await flush();
    await flush();

    assert.deepEqual(setup.copiedTexts, [hydratedText]);
    assert.equal(setup.code.textContent, hydratedText);
    assert.equal(setup.code.dataset.outputHydrated, "true");
});