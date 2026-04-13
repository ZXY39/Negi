const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const APP_PATH = "g3ku/web/frontend/org_graph_app.js";
const APP_CODE = fs.readFileSync(APP_PATH, "utf8");
const MODEL_CALL_LABEL = "\u6b63\u5728\u8bf7\u6c42 CEO \u6a21\u578b\u751f\u6210\u4e0b\u4e00\u6b65\u54cd\u5e94...";
const PROCESSING_LABEL = "\u5904\u7406\u4e2d...";

class StubElement {}

class StubHTMLElement extends StubElement {
    constructor() {
        super();
        this.className = "";
        this.hidden = false;
        this.textContent = "";
        this._innerHTML = "";
        this._selectors = {};
        this.attributes = {};
        this.classList = {
            add: (...tokens) => {
                const classes = new Set(String(this.className || "").split(/\s+/).filter(Boolean));
                tokens.forEach((token) => classes.add(token));
                this.className = [...classes].join(" ");
            },
            remove: (...tokens) => {
                const classes = new Set(String(this.className || "").split(/\s+/).filter(Boolean));
                tokens.forEach((token) => classes.delete(token));
                this.className = [...classes].join(" ");
            },
            contains: (token) => String(this.className || "").split(/\s+/).includes(token),
        };
    }

    set innerHTML(value) {
        this._innerHTML = String(value);
        if (!this._innerHTML.includes("assistant-text")) return;
        const assistantText = new StubHTMLElement();
        const assistantMatch = this._innerHTML.match(/<div class="assistant-text pending">([\s\S]*?)<\/div>/);
        assistantText.className = "assistant-text pending";
        assistantText.textContent = assistantMatch ? assistantMatch[1].replace(/<[^>]+>/g, "").trim() : "";
        assistantText._innerHTML = assistantMatch ? assistantMatch[1] : "";
        this._selectors[".assistant-text"] = assistantText;
        this._selectors[".interaction-flow"] = new StubHTMLElement();
        this._selectors[".interaction-flow-meta"] = new StubHTMLElement();
        this._selectors[".interaction-flow-list"] = new StubHTMLElement();
        this._selectors[".interaction-flow-footer"] = new StubHTMLElement();
        this._selectors[".interaction-flow-toggle"] = new StubHTMLButtonElement();
    }

    get innerHTML() {
        return this._innerHTML;
    }

    querySelector(selector) {
        return this._selectors[selector] || null;
    }

    addEventListener() {}

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

    querySelector() {
        return null;
    }

    querySelectorAll() {
        return [];
    }

    addEventListener() {}

    createElement() {
        return new StubHTMLElement();
    }
}

function loadApp() {
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
        `${APP_CODE}\nthis.__testExports = { createPendingCeoTurn, renderCeoAssistantTextIntoTurn, U };`,
        context
    );
    context.__testExports.U.ceoFeed = {
        scrollTop: 0,
        scrollHeight: 0,
        clientHeight: 0,
        appended: [],
        appendChild(element) {
            this.appended.push(element);
            return element;
        },
    };
    return context.__testExports;
}

test("createPendingCeoTurn renders a loading icon instead of fixed processing copy", () => {
    const { createPendingCeoTurn, U } = loadApp();

    const turn = createPendingCeoTurn("user");

    assert.equal(U.ceoFeed.appended.length, 1);
    assert.equal(!!turn, true);
    assert.match(turn.el.innerHTML, /data-lucide="loader-circle"/);
    assert.doesNotMatch(turn.el.innerHTML, new RegExp(PROCESSING_LABEL));
    assert.equal(turn.el.classList.contains("ceo-turn-loading-only"), true);
});

test("renderCeoAssistantTextIntoTurn converts CEO model-call loading copy into a spinner", () => {
    const { renderCeoAssistantTextIntoTurn } = loadApp();
    const textEl = new StubHTMLElement();
    textEl.className = "assistant-text pending";
    const turnEl = new StubHTMLElement();
    const turn = { textEl, el: turnEl };

    renderCeoAssistantTextIntoTurn(turn, MODEL_CALL_LABEL, { status: "running" });

    assert.match(textEl.innerHTML, /data-lucide="loader-circle"/);
    assert.doesNotMatch(textEl.innerHTML, new RegExp(MODEL_CALL_LABEL));
    assert.equal(textEl.textContent, "");
    assert.equal(textEl.classList.contains("assistant-text-loading"), true);
    assert.equal(textEl.classList.contains("markdown-content"), false);
    assert.equal(turnEl.classList.contains("ceo-turn-loading-only"), true);
});

test("renderCeoAssistantTextIntoTurn removes loading-only layout once real content arrives", () => {
    const { renderCeoAssistantTextIntoTurn } = loadApp();
    const textEl = new StubHTMLElement();
    const turnEl = new StubHTMLElement();
    turnEl.classList.add("ceo-turn-loading-only");
    textEl.className = "assistant-text pending assistant-text-loading";
    const turn = { textEl, el: turnEl };

    renderCeoAssistantTextIntoTurn(turn, "ready", { status: "running" });

    assert.match(textEl.innerHTML, /ready/);
    assert.equal(textEl.classList.contains("assistant-text-loading"), false);
    assert.equal(turnEl.classList.contains("ceo-turn-loading-only"), false);
});
