const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const APP_PATH = "g3ku/web/frontend/org_graph_app.js";
const APP_CODE = fs.readFileSync(APP_PATH, "utf8");

class StubElement {
    constructor(id = "") {
        this.id = id;
        this.hidden = false;
        this.disabled = false;
        this.value = "";
        this.innerHTML = "";
        this.textContent = "";
        this.attributes = {};
        this.style = {};
        this.scrollHeight = 48;
        this.dataset = {};
        this.classList = {
            add() {},
            remove() {},
            toggle() {},
            contains() { return false; },
        };
    }

    setAttribute(name, value) {
        this.attributes[name] = String(value);
    }

    getAttribute(name) {
        return this.attributes[name];
    }

    removeAttribute(name) {
        delete this.attributes[name];
    }

    addEventListener() {}

    querySelector() {
        return null;
    }

    querySelectorAll() {
        return [];
    }
}

class StubHTMLElement extends StubElement {}
class StubHTMLButtonElement extends StubHTMLElement {}
class StubHTMLInputElement extends StubHTMLElement {}
class StubHTMLTextAreaElement extends StubHTMLElement {}
class StubHTMLSelectElement extends StubHTMLElement {}

class StubDocument {
    constructor(elements) {
        this.elements = elements;
    }

    getElementById(id) {
        return this.elements[id] || null;
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
    const elements = {
        "ceo-input": new StubHTMLTextAreaElement("ceo-input"),
        "ceo-send-btn": new StubHTMLButtonElement("ceo-send-btn"),
        "ceo-attach-btn": new StubHTMLButtonElement("ceo-attach-btn"),
        "ceo-file-input": new StubHTMLInputElement("ceo-file-input"),
        "ceo-upload-list": new StubHTMLElement("ceo-upload-list"),
        "ceo-follow-up-queue": new StubHTMLElement("ceo-follow-up-queue"),
    };
    const document = new StubDocument(elements);
    const socket = {
        readyState: 1,
        sent: [],
        send(payload) {
            this.sent.push(JSON.parse(payload));
        },
    };
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
        document,
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
        ApiClient: {
            getActiveSessionId: () => "web:test",
        },
        activeSessionIsReadonly: () => false,
        icons: () => {},
        renderPendingCeoUploads: () => {},
        syncCeoAttachButton: () => {},
        syncCeoSessionActions: () => {},
        syncActiveCeoComposerDraft: () => {},
        syncCeoInputHeight: () => {},
        clearCeoComposerDraft: () => {},
        addMsg: () => {},
        showToast: (payload) => {
            context.__showToastCalls = context.__showToastCalls || [];
            context.__showToastCalls.push(payload);
        },
        patchCeoSessionRuntimeState: () => false,
        setCeoSessionSnapshotCache: () => ({}),
        createPendingCeoTurn: () => ({}),
        normalizeUploadList: (items) => Array.isArray(items) ? items : [],
        summarizeUploads: () => "",
        hasRenderableText: (value) => !!String(value || "").trim(),
        requestCeoPause: () => { context.__pauseRequested = (context.__pauseRequested || 0) + 1; },
    };
    context.window = context;
    vm.createContext(context);
    vm.runInContext(
        `${APP_CODE}
        this.__testExports = {
            S,
            U,
            syncCeoPrimaryButton,
            handleCeoPrimaryAction,
            setCeoQueuedFollowUps,
            removeCeoQueuedFollowUp,
        };`,
        context
    );
    context.__testExports.S.ceoWs = socket;
    context.__testExports.S.activeSessionId = "web:test";
    return {
        ...context.__testExports,
        socket,
        __context: context,
    };
}

test("primary button is disabled when idle and composer is empty", () => {
    const { S, U, syncCeoPrimaryButton } = loadApp();
    S.ceoTurnActive = false;
    U.ceoInput.value = "";

    syncCeoPrimaryButton();

    assert.equal(U.ceoSend.disabled, true);
    assert.match(U.ceoSend.innerHTML, /发送/);
});

test("removing a queued follow-up re-renders the visible queue", () => {
    const { U, setCeoQueuedFollowUps, removeCeoQueuedFollowUp } = loadApp();

    setCeoQueuedFollowUps("web:test", [
        { id: "first", text: "first follow-up" },
        { id: "second", text: "second follow-up" },
    ]);
    assert.equal(U.ceoFollowUpQueue.hidden, false);
    assert.match(U.ceoFollowUpQueue.innerHTML, /first follow-up/);
    assert.match(U.ceoFollowUpQueue.innerHTML, /second follow-up/);

    removeCeoQueuedFollowUp("web:test", "first");

    assert.equal(U.ceoFollowUpQueue.hidden, false);
    assert.doesNotMatch(U.ceoFollowUpQueue.innerHTML, /first follow-up/);
    assert.match(U.ceoFollowUpQueue.innerHTML, /second follow-up/);
});

test("queued follow-up list renders chips without a queue title block", () => {
    const { U, setCeoQueuedFollowUps } = loadApp();

    setCeoQueuedFollowUps("web:test", [
        { id: "only", text: "only follow-up" },
    ]);

    assert.equal(U.ceoFollowUpQueue.hidden, false);
    assert.match(U.ceoFollowUpQueue.innerHTML, /only follow-up/);
    assert.doesNotMatch(U.ceoFollowUpQueue.innerHTML, /ceo-follow-up-queue-title/);
});

test("primary button shows send when there is input during an active turn", () => {
    const { S, U, syncCeoPrimaryButton } = loadApp();
    S.ceoTurnActive = true;
    U.ceoInput.value = "前10个";

    syncCeoPrimaryButton();

    assert.equal(U.ceoSend.disabled, false);
    assert.match(U.ceoSend.innerHTML, /发送/);
});

test("sending while a turn is active queues follow-up instead of pausing immediately", () => {
    const { S, U, handleCeoPrimaryAction, __context } = loadApp();
    S.ceoTurnActive = true;
    U.ceoInput.value = "前10个";

    handleCeoPrimaryAction();

    assert.equal(__context.__pauseRequested || 0, 0);
    assert.equal(Array.isArray(S.ceoQueuedFollowUps?.["web:test"]), true);
    assert.equal(S.ceoQueuedFollowUps["web:test"].length, 1);
    assert.equal(S.ceoQueuedFollowUps["web:test"][0].text, "前10个");
    assert.equal((__context.__showToastCalls || []).length, 0);
});
