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

    querySelector() {
        return null;
    }

    querySelectorAll() {
        return [];
    }

    addEventListener() {}

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
    getElementById() {
        return null;
    }

    createElement() {
        return new StubHTMLElement();
    }

    querySelector() {
        return null;
    }

    querySelectorAll() {
        return [];
    }

    addEventListener() {}
}

function loadApp() {
    const artifactContent = new StubHTMLElement();
    const context = {
        console,
        setTimeout,
        clearTimeout,
        setInterval,
        clearInterval,
        queueMicrotask,
        navigator: { clipboard: { writeText: async () => {} } },
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
    context.window = context;
    vm.createContext(context);
    vm.runInContext(
        `${TASK_VIEW_CODE}\n${APP_CODE}\nthis.__testExports = { renderNodeContextPlaceholder, renderTaskNodeModelRetryToast, U };`,
        context
    );
    context.__testExports.U.artifactContent = artifactContent;
    context.__testExports.U.taskNodeModelRetryToast = new StubHTMLElement();
    context.__testExports.U.taskNodeModelRetryToastText = new StubHTMLElement();
    return { ...context.__testExports, artifactContent };
}

test("renderNodeContextPlaceholder decodes escaped display text for node context", () => {
    const { renderNodeContextPlaceholder, artifactContent } = loadApp();

    renderNodeContextPlaceholder('line1\\nline2\\t\\"quoted\\"\\\\path');

    assert.equal(artifactContent.textContent, 'line1\nline2\t"quoted"\\path');
});

test("task node retry toast renders live frame retry count and error", () => {
    const { renderTaskNodeModelRetryToast, U } = loadApp();

    renderTaskNodeModelRetryToast({
        model_retry_status: {
            state: "retrying",
            retry_count: 8,
            error_message: "Error code: 502 - upstream request failed",
        },
    });

    assert.equal(U.taskNodeModelRetryToast.hidden, false);
    assert.match(U.taskNodeModelRetryToastText.textContent, /第 8 次重试/);
    assert.match(U.taskNodeModelRetryToastText.textContent, /Error code: 502/);
});

test("task node retry toast shows last and next retry clock times", () => {
    const { renderTaskNodeModelRetryToast, U } = loadApp();

    renderTaskNodeModelRetryToast({
        model_retry_status: {
            state: "retrying",
            retry_count: 3,
            error_message: "Error code: 502 - upstream request failed",
            last_retry_at: "2026-09-03T14:53:07+08:00",
            next_retry_at: "2026-09-03T14:56:07+08:00",
        },
    });

    const text = U.taskNodeModelRetryToastText.textContent;
    assert.match(text, /第 3 次重试/);
    assert.match(text, /最新 14:53:07/);
    assert.match(text, /下次 14:56:07/);
});

test("task node retry toast hides when live frame status is cleared", () => {
    const { renderTaskNodeModelRetryToast, U } = loadApp();

    renderTaskNodeModelRetryToast({ model_retry_status: null });

    assert.equal(U.taskNodeModelRetryToast.hidden, true);
    assert.equal(U.taskNodeModelRetryToastText.textContent, "");
});
