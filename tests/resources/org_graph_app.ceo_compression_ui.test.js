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
        this.hidden = false;
        this.disabled = false;
        this.textContent = "";
        this.className = "";
        this.dataset = {};
        this.attributes = {};
        this.classList = {
            add: () => {},
            remove: () => {},
            contains: () => false,
            toggle: () => {},
        };
    }
    addEventListener() {}
    setAttribute(name, value) {
        this.attributes[name] = String(value);
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
        this.__testExports = { S, U, syncCeoCompressionToast, handleCeoError };`,
        context
    );
    context.__testExports.U.ceoCompressionToast = new StubHTMLElement();
    context.__testExports.U.ceoCompressionToastText = new StubHTMLElement();
    context.__testExports.U.ceoCompressionActions = new StubHTMLElement();
    context.__testExports.U.ceoCompressionPause = new StubHTMLButtonElement();
    context.__testExports.__toasts = [];
    context.showToast = (payload) => context.__testExports.__toasts.push(payload);
    context.patchCeoSessionRuntimeState = () => false;
    context.renderCeoSessions = () => {};
    context.syncCeoSessionActions = () => {};
    context.syncCeoPrimaryButton = () => {};
    context.finalizeCeoTurn = () => {};
    context.__testExports.S.activeSessionId = "web:test";
    return context.__testExports;
}

test("compression toast also shows dedicated pause controls while compression is running", () => {
    const { syncCeoCompressionToast, S, U } = loadApp();
    S.ceoSnapshotCache = {
        "web:test": {
            session_id: "web:test",
            inflight_turn: {
                status: "running",
                compression: { status: "running", text: "上下文压缩中", source: "token_compression" },
            },
        },
    };

    syncCeoCompressionToast();

    assert.equal(U.ceoCompressionToast.hidden, false);
    assert.equal(U.ceoCompressionActions.hidden, false);
    assert.equal(U.ceoCompressionPause.disabled, false);
});

test("context window overflow errors show a toast", () => {
    const { handleCeoError, __toasts } = loadApp();

    handleCeoError({
        code: "frontdoor_context_window_exceeded",
        message: "上下文大小超出当前模型openai:gpt-5.2，请更改模型链配置后继续",
    });

    assert.equal(__toasts.length, 1);
    assert.equal(__toasts[0].title, "上下文超限");
    assert.match(String(__toasts[0].text || ""), /上下文大小超出当前模型openai:gpt-5\.2/);
});
