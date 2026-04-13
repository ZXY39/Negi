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
        this._innerHTML = "";
        this.children = [];
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

    appendChild(child) {
        this.children.push(child);
        return child;
    }

    querySelector() {
        return null;
    }

    querySelectorAll() {
        return [];
    }

    addEventListener() {}

    set innerHTML(value) {
        this._innerHTML = String(value);
    }

    get innerHTML() {
        return this._innerHTML;
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
        `${APP_CODE}\nthis.__testExports = { handleCeoInternalAck, S, U, getPatchSnapshotCalls: () => globalThis.__patchSnapshotCalls || 0 };`,
        context
    );
    context.__testExports.U.ceoFeed = new StubHTMLElement();
    context.__testExports.U.ceoFeed.appended = [];
    context.__testExports.U.ceoFeed.appendChild = function appendChild(element) {
        this.appended.push(element);
        this.children.push(element);
        return element;
    };
    vm.runInContext(
        `
        globalThis.__patchSnapshotCalls = 0;
        patchCeoSessionSnapshotCache = () => {
            globalThis.__patchSnapshotCalls += 1;
            return {};
        };
        setCeoSessionSnapshotCache = () => ({});
        renderCeoSessions = () => {};
        syncCeoPrimaryButton = () => {};
        syncCeoSessionActions = () => {};
        patchCeoSessionRuntimeState = () => false;
        maybeDispatchQueuedCeoFollowUps = () => false;
        S.activeSessionId = "web:test";
        S.ceoPendingTurns = [];
        `,
        context
    );
    return context.__testExports;
}

test("handleCeoInternalAck renders a special bubble and keeps pending turns untouched", () => {
    const { handleCeoInternalAck, S, U, getPatchSnapshotCalls } = loadApp();

    handleCeoInternalAck({
        source: "heartbeat",
        reason: "task_terminal",
        label: "已接收来自类型：task_terminal的心跳",
        turn_id: "turn-heartbeat-ack",
    });

    assert.equal(S.ceoPendingTurns.length, 0);
    assert.equal(U.ceoFeed.appended.length, 1);
    assert.match(String(U.ceoFeed.appended[0].className || ""), /ceo-internal-ack/);
    assert.match(String(U.ceoFeed.appended[0].innerHTML || ""), /task_terminal/);
    assert.equal(getPatchSnapshotCalls(), 0);
});
