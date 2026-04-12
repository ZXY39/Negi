const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const APP_PATH = "g3ku/web/frontend/org_graph_app.js";
const HTML_PATH = "g3ku/web/frontend/org_graph.html";
const APP_CODE = fs.readFileSync(APP_PATH, "utf8");
const HTML_CODE = fs.readFileSync(HTML_PATH, "utf8");

class StubElement {}
class StubHTMLElement extends StubElement {
    constructor() {
        super();
        this.className = "";
        this._innerHTML = "";
    }

    set innerHTML(value) {
        this._innerHTML = String(value);
    }

    get innerHTML() {
        return this._innerHTML;
    }

    querySelector() {
        return null;
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
        `${APP_CODE}\nthis.__testExports = { addMsg, createPendingCeoTurn, U };`,
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

test("chat message renderers omit avatar markup for user, agent, and pending messages", () => {
    const { addMsg, createPendingCeoTurn, U } = loadApp();

    addMsg("hello", "user");
    addMsg("world", "system");
    createPendingCeoTurn("user");

    assert.equal(U.ceoFeed.appended.length, 3);
    for (const element of U.ceoFeed.appended) {
        assert.equal(element.innerHTML.includes('class="avatar"'), false);
    }
});

test("ceo welcome message in static html omits avatar markup", () => {
    assert.equal(HTML_CODE.includes('<div class="avatar"><i data-lucide="cpu"></i></div>'), false);
});
