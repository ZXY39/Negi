const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const APP_PATH = "g3ku/web/frontend/org_graph_app.js";
const APP_CODE = fs.readFileSync(APP_PATH, "utf8");

class StubElement {}

class StubHTMLElement extends StubElement {
    constructor() {
        super();
        this.className = "";
        this.hidden = false;
        this.textContent = "";
        this.innerHTML = "";
        this.children = [];
        this.dataset = {};
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
            toggle: (token, force) => {
                const shouldAdd = force === undefined ? !this.classList.contains(token) : !!force;
                if (shouldAdd) this.classList.add(token);
                else this.classList.remove(token);
            },
        };
    }

    appendChild(child) {
        this.children.push(child);
        return child;
    }

    setAttribute(name, value) {
        this.attributes[name] = String(value);
    }

    querySelector() {
        return null;
    }

    addEventListener() {}
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
        `${APP_CODE}
        this.__testExports = { S, U, createPendingCeoTurn, maybeShowCeoContextLoadNotice, showCeoContextLoadNotice };`,
        context
    );
    context.__testExports.U.ceoContextLoadNotice = new StubHTMLElement();
    return {
        ...context.__testExports,
        __context: context,
    };
}

test("context load notice supports v2 tool loaders", () => {
    const { S, createPendingCeoTurn, maybeShowCeoContextLoadNotice, U } = loadApp();
    const turn = createPendingCeoTurn("user");
    S.ceoPendingTurns = [turn];

    const shown = maybeShowCeoContextLoadNotice(turn, {
        toolName: "load_tool_context_v2",
        status: "success",
        detailTexts: ['{"tool_id":"filesystem_write"}'],
    });

    assert.equal(shown, true);
    assert.equal(U.ceoContextLoadNotice.children.length, 1);
    assert.equal(U.ceoContextLoadNotice.children[0].dataset.noticeKind, "tool");
    assert.equal(U.ceoContextLoadNotice.children[0].children[0].innerHTML, '<i data-lucide="wrench"></i>');
});

test("context load notice renders type icon before text and keeps risk dot metadata", () => {
    const { showCeoContextLoadNotice, U } = loadApp();

    showCeoContextLoadNotice("已加载 skill find-skills", {
        kind: "skill",
        riskLevel: "high",
    });

    assert.equal(U.ceoContextLoadNotice.children.length, 1);
    const notice = U.ceoContextLoadNotice.children[0];
    assert.equal(notice.children[0].className, "ceo-context-load-notice-kind-icon");
    assert.equal(notice.children[1].className, "ceo-context-load-notice-text");
    assert.equal(notice.children[2].className, "ceo-context-load-notice-risk-dot risk-high");
    assert.equal(notice.children[0].innerHTML, '<i data-lucide="sparkles"></i>');
});
