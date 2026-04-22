const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const APP_PATH = "g3ku/web/frontend/org_graph_app.js";
const APP_CODE = fs.readFileSync(APP_PATH, "utf8");
const GOVERNANCE_PATH = "g3ku/web/frontend/org_graph_ceo_governance.js";
const GOVERNANCE_CODE = fs.readFileSync(GOVERNANCE_PATH, "utf8");

class StubElement {}
class StubHTMLElement extends StubElement {}
class StubHTMLButtonElement extends StubHTMLElement {}
class StubHTMLTextAreaElement extends StubHTMLElement {}

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
        return new StubElement();
    }
}

test("governance script syncs approval state from active session snapshot on load", () => {
    const context = {
        console,
        setTimeout,
        clearTimeout,
        requestAnimationFrame: (callback) => {
            callback();
            return 1;
        },
        cancelAnimationFrame: () => {},
        document: new StubDocument(),
        window: {},
        Element: StubElement,
        HTMLElement: StubHTMLElement,
        HTMLButtonElement: StubHTMLButtonElement,
        HTMLTextAreaElement: StubHTMLTextAreaElement,
        WebSocket: { OPEN: 1 },
        CSS: { escape: (value) => String(value || "") },
        S: { view: "ceo", ceoQueuedFollowUps: {} },
        U: {},
        ApiClient: {},
        showToast: () => {},
        switchView: () => {},
        canMutateCeoSessions: () => true,
        canCreateCeoSessions: () => true,
        canActivateCeoSessions: () => true,
        syncCeoPrimaryButton: () => {},
        sendCeoMessage: () => {},
        maybeDispatchQueuedCeoFollowUps: () => false,
        sendActiveCeoFollowUpsToRuntime: () => null,
        applyCeoState: () => {},
        resetCeoSessionState: () => {},
        activeSessionIsReadonly: () => false,
        activeSessionId: () => "web:current",
        setDrawerOpen: () => {},
        esc: (value) => String(value || ""),
        displayRiskLabel: (value) => String(value || ""),
        icons: () => {},
        syncCeoApprovalFromSnapshotEntryCalls: [],
        refreshCeoApprovalFromServerCalls: [],
    };
    context.syncCeoApprovalFromSnapshotEntry = (sessionId, entry, options = {}) => {
        context.syncCeoApprovalFromSnapshotEntryCalls.push({
            sessionId: String(sessionId || ""),
            entry,
            authoritative: !!options.authoritative,
            refreshServer: !!options.refreshServer,
        });
    };
    context.refreshCeoApprovalFromServer = (sessionId, options = {}) => {
        context.refreshCeoApprovalFromServerCalls.push({
            sessionId: String(sessionId || ""),
            quiet: !!options.quiet,
        });
        return Promise.resolve([]);
    };
    context.window = context;
    vm.createContext(context);

    vm.runInContext(GOVERNANCE_CODE, context);

    assert.equal(context.syncCeoApprovalFromSnapshotEntryCalls.length, 1);
    assert.deepEqual(context.syncCeoApprovalFromSnapshotEntryCalls[0], {
        sessionId: "web:current",
        entry: null,
        authoritative: true,
        refreshServer: true,
    });
    assert.equal(context.refreshCeoApprovalFromServerCalls.length, 0);
});

test("governance script can load after app script without global wrapper name collisions", () => {
    class StubHTMLInputElement extends StubHTMLElement {}
    class StubHTMLSelectElement extends StubHTMLElement {}

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
        addEventListener() {},
        removeEventListener() {},
        WebSocket: { OPEN: 1 },
        CSS: { escape: (value) => String(value || "") },
        ApiClient: {
            getActiveSessionId: () => "web:shared",
            setActiveSessionId: () => {},
            getCeoWsUrl: () => "ws://localhost/api/ws/ceo?session_id=web%3Ashared",
        },
        syncCeoApprovalFromSnapshotEntryCalls: [],
        refreshCeoApprovalFromServerCalls: [],
    };
    context.window = context;
    vm.createContext(context);

    vm.runInContext(APP_CODE, context);
    vm.runInContext(`
        renderCeoSessions = () => {};
        syncCeoSessionActions = () => {};
        syncCeoPrimaryButton = () => {};
        syncCeoComposerReadonlyState = () => {};
        syncCeoAttachButton = () => {};
        syncCeoCompressionToast = () => {};
    `, context);
    context.syncCeoApprovalFromSnapshotEntry = (sessionId, entry, options = {}) => {
        context.syncCeoApprovalFromSnapshotEntryCalls.push({
            sessionId: String(sessionId || ""),
            entry,
            authoritative: !!options.authoritative,
            refreshServer: !!options.refreshServer,
        });
    };
    context.refreshCeoApprovalFromServer = (sessionId, options = {}) => {
        context.refreshCeoApprovalFromServerCalls.push({
            sessionId: String(sessionId || ""),
            quiet: !!options.quiet,
        });
        return Promise.resolve([]);
    };

    vm.runInContext(GOVERNANCE_CODE, context);

    assert.equal(context.syncCeoApprovalFromSnapshotEntryCalls.length, 1);
    assert.deepEqual(context.syncCeoApprovalFromSnapshotEntryCalls[0], {
        sessionId: "web:shared",
        entry: null,
        authoritative: true,
        refreshServer: true,
    });
    assert.equal(context.refreshCeoApprovalFromServerCalls.length, 0);
});

test("approval submit clears cached approval interrupts and rebinds the active turn to the user lane", async () => {
    class StubHTMLInputElement extends StubHTMLElement {}
    class StubHTMLSelectElement extends StubHTMLElement {}

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
        addEventListener() {},
        removeEventListener() {},
        WebSocket: { OPEN: 1 },
        CSS: { escape: (value) => String(value || "") },
        ApiClient: {
            getActiveSessionId: () => "web:shared",
            setActiveSessionId: () => {},
            getCeoWsUrl: () => "ws://localhost/api/ws/ceo?session_id=web%3Ashared",
        },
    };
    context.window = context;
    vm.createContext(context);

    vm.runInContext(APP_CODE, context);
    vm.runInContext(`
        renderCeoSessions = () => {};
        syncCeoSessionActions = () => {};
        syncCeoPrimaryButton = () => {};
        syncCeoComposerReadonlyState = () => {};
        syncCeoAttachButton = () => {};
        syncCeoCompressionToast = () => {};
    `, context);
    context.syncCeoApprovalFromSnapshotEntry = () => {};
    context.refreshCeoApprovalFromServer = () => Promise.resolve([]);
    context.__patchCalls = [];
    context.__approvalTurn = { finalized: false, source: "approval" };
    vm.runInContext(`
        patchCeoSessionSnapshotCache = (sessionId, updater) => {
            const entry = {
                inflight_turn: {
                    source: "approval",
                    status: "paused",
                    interrupts: [
                        {
                            id: "interrupt-1",
                            value: {
                                kind: "frontdoor_tool_approval_batch",
                                batch_id: "batch:1",
                                review_items: [{ tool_call_id: "call-1", name: "exec", risk_level: "high", arguments: { command: "dir" } }],
                            },
                        },
                    ],
                },
                preserved_turn: null,
            };
            const next = updater(entry);
            globalThis.__patchCalls.push({ sessionId, next });
            return next;
        };
        getActiveCeoTurn = () => globalThis.__approvalTurn;
    `, context);

    vm.runInContext(GOVERNANCE_CODE, context);
    vm.runInContext(`
        S.activeSessionId = "web:shared";
        S.ceoWs = {
            readyState: 1,
            sent: [],
            send(payload) { this.sent.push(payload); },
        };
        S.ceoApprovalFlow = {
            active: true,
            submitting: false,
            sessionId: "web:shared",
            interruptId: "interrupt-1",
            batchId: "batch:1",
            mode: "regulatory_review",
            submissionMode: "batch_submit_only",
            reviewItems: [
                { tool_call_id: "call-1", name: "exec", risk_level: "high", arguments: { command: "dir" } },
            ],
            decisions: {
                "call-1": { decision: "approve", note: "" },
            },
            currentIndex: 0,
            argsItemId: "",
        };
    `, context);

    await vm.runInContext(`submitCeoApprovalFlow()`, context);

    const sentPayload = JSON.parse(vm.runInContext(`S.ceoWs.sent[0]`, context));
    assert.equal(sentPayload.type, "client.resume_interrupt");
    assert.equal(sentPayload.resume.batch_id, "batch:1");
    assert.equal(vm.runInContext(`S.ceoApprovalFlow.submitting`, context), true);
    assert.equal(context.__approvalTurn.source, "user");
    assert.equal(context.__patchCalls.length, 1);
    assert.equal(Array.isArray(context.__patchCalls[0].next.inflight_turn.interrupts), false);
});

test("approval pending no longer blocks view switching or session activation", () => {
    class StubHTMLInputElement extends StubHTMLElement {}
    class StubHTMLSelectElement extends StubHTMLElement {}

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
        addEventListener() {},
        removeEventListener() {},
        WebSocket: { OPEN: 1 },
        CSS: { escape: (value) => String(value || "") },
        ApiClient: {
            getActiveSessionId: () => "web:shared",
            setActiveSessionId: () => {},
            getCeoWsUrl: () => "ws://localhost/api/ws/ceo?session_id=web%3Ashared",
        },
    };
    context.window = context;
    vm.createContext(context);

    vm.runInContext(APP_CODE, context);
    vm.runInContext(`
        renderCeoSessions = () => {};
        syncCeoSessionActions = () => {};
        syncCeoPrimaryButton = () => {};
        syncCeoComposerReadonlyState = () => {};
        syncCeoAttachButton = () => {};
        syncCeoCompressionToast = () => {};
        globalThis.__switchCalls = [];
        canMutateCeoSessions = () => true;
        canCreateCeoSessions = () => true;
        canActivateCeoSessions = () => true;
        switchView = (view) => { globalThis.__switchCalls.push(view); return view; };
    `, context);
    context.syncCeoApprovalFromSnapshotEntry = () => {};
    context.refreshCeoApprovalFromServer = () => Promise.resolve([]);

    vm.runInContext(GOVERNANCE_CODE, context);
    vm.runInContext(`
        S.activeSessionId = "web:shared";
        S.ceoApprovalFlow = {
            active: true,
            submitting: false,
            sessionId: "web:shared",
            interruptId: "interrupt-1",
            batchId: "batch:1",
            mode: "regulatory_review",
            submissionMode: "batch_submit_only",
            reviewItems: [{ tool_call_id: "call-1", name: "exec", risk_level: "high", arguments: { command: "dir" } }],
            decisions: { "call-1": { decision: "approve", note: "" } },
            currentIndex: 0,
            argsItemId: "",
        };
    `, context);

    assert.equal(vm.runInContext(`canMutateCeoSessions()`, context), true);
    assert.equal(vm.runInContext(`canCreateCeoSessions()`, context), true);
    assert.equal(vm.runInContext(`canActivateCeoSessions()`, context), true);
    assert.equal(vm.runInContext(`switchView("tasks")`, context), "tasks");
    assert.deepEqual(Array.from(context.__switchCalls), ["tasks"]);
});

test("approval sync no longer forces the shell back to the ceo view", () => {
    const context = {
        console,
        setTimeout,
        clearTimeout,
        requestAnimationFrame: (callback) => {
            callback();
            return 1;
        },
        cancelAnimationFrame: () => {},
        document: new StubDocument(),
        window: {},
        Element: StubElement,
        HTMLElement: StubHTMLElement,
        HTMLButtonElement: StubHTMLButtonElement,
        HTMLTextAreaElement: StubHTMLTextAreaElement,
        WebSocket: { OPEN: 1 },
        CSS: { escape: (value) => String(value || "") },
        S: { view: "tasks", ceoQueuedFollowUps: {} },
        U: {},
        ApiClient: {},
        showToast: () => {},
        canMutateCeoSessions: () => true,
        canCreateCeoSessions: () => true,
        canActivateCeoSessions: () => true,
        syncCeoPrimaryButton: () => {},
        sendCeoMessage: () => {},
        maybeDispatchQueuedCeoFollowUps: () => false,
        sendActiveCeoFollowUpsToRuntime: () => null,
        applyCeoState: () => {},
        resetCeoSessionState: () => {},
        activeSessionIsReadonly: () => false,
        activeSessionId: () => "web:shared",
        setDrawerOpen: () => {},
        esc: (value) => String(value || ""),
        displayRiskLabel: (value) => String(value || ""),
        icons: () => {},
        switchCalls: [],
    };
    context.switchView = (view) => {
        context.switchCalls.push(view);
        return view;
    };
    context.window = context;
    vm.createContext(context);

    vm.runInContext(GOVERNANCE_CODE, context);
    vm.runInContext(`
        syncCeoApprovalFromInterrupts([
            {
                id: "interrupt-1",
                value: {
                    kind: "frontdoor_tool_approval_batch",
                    batch_id: "batch:1",
                    review_items: [{ tool_call_id: "call-1", name: "exec", risk_level: "high", arguments: { command: "dir" } }],
                },
            },
        ], "web:shared", { authoritative: true });
    `, context);

    assert.deepEqual(context.switchCalls, []);
    assert.equal(vm.runInContext(`S.view`, context), "tasks");
});
