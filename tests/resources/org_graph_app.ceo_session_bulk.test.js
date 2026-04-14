const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const APP_PATH = "g3ku/web/frontend/org_graph_app.js";
const APP_CODE = fs.readFileSync(APP_PATH, "utf8");

class StubElement {}
class StubHTMLElement extends StubElement {}
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
        return {};
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
        ApiClient: {
            getActiveSessionId: () => "",
            getCeoSessionDeleteCheck: async () => ({ related_tasks: { total: 0, deletable: 0, in_progress: 0 } }),
            getBootstrapExitCheck: async () => ({ has_running_work: false, summary_text: "" }),
        },
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
        `${APP_CODE}\nthis.__testExports = { S, U, canCreateCeoSessions, syncCeoSessionActions, setCeoSessionTab, renderCeoSessionCard, toggleCeoBulkSelectAll, buildCeoBulkDeleteSummary, requestDeleteSelectedCeoSessions, requestDeleteCeoSession, requestProjectExit };`,
        context
    );
    vm.runInContext(
        `
        renderCeoSessions = () => {};
        syncCeoPrimaryButton = () => {};
        syncCeoComposerReadonlyState = () => {};
        syncCeoAttachButton = () => {};
        syncCeoCompressionToast = () => {};
    `,
        context
    );
    return {
        ...context.__testExports,
        __context: context,
        __makeSet(values = []) {
            context.__setSeed = values;
            return vm.runInContext("new Set(this.__setSeed)", context);
        },
    };
}

test("toggleCeoBulkSelectAll scopes selection to the current tab", () => {
    const { S, toggleCeoBulkSelectAll, __makeSet } = loadApp();

    S.ceoSessionTab = "local";
    S.ceoBulkMode = true;
    S.ceoSelectedSessionIds = __makeSet();
    S.ceoLocalSessions = [
        { session_id: "web:1", title: "Local 1" },
        { session_id: "web:2", title: "Local 2" },
    ];
    S.ceoChannelGroups = [
        { channel_id: "qqbot", label: "QQ Bot", items: [{ session_id: "china:1", title: "Channel 1" }] },
    ];

    toggleCeoBulkSelectAll();

    assert.deepEqual([...S.ceoSelectedSessionIds], ["web:1", "web:2"]);
});

test("canCreateCeoSessions stays enabled during session switch busy", () => {
    const { S, canCreateCeoSessions } = loadApp();

    S.ceoPauseBusy = false;
    S.ceoUploadBusy = false;
    S.ceoSessionCatalogBusy = false;
    S.ceoSessionBusy = true;

    assert.equal(canCreateCeoSessions(), true);
});

test("syncCeoSessionActions keeps bulk selection controls enabled during session switch busy", () => {
    const { S, U, syncCeoSessionActions, __makeSet } = loadApp();

    const bulkToggle = {
        hidden: false,
        disabled: true,
        textContent: "",
        setAttribute() {},
    };
    const bulkSelectAll = {
        disabled: true,
        setAttribute() {},
    };
    const bulkDelete = {
        disabled: false,
    };
    const newSession = {
        disabled: true,
    };
    const bulkCheckboxes = [{ disabled: true }, { disabled: true }];
    const sessionList = {
        querySelectorAll(selector) {
            if (selector === "[data-session-bulk-checkbox]") {
                return bulkCheckboxes;
            }
            return [];
        },
    };

    U.ceoNewSession = newSession;
    U.ceoSessionBulkToggle = bulkToggle;
    U.ceoSessionBulkActions = { hidden: true };
    U.ceoSessionBulkDelete = bulkDelete;
    U.ceoSessionBulkSelectAll = bulkSelectAll;
    U.ceoSessionList = sessionList;
    U.ceoSessionTabLocal = null;
    U.ceoSessionTabChannel = null;
    U.ceoSessionTabs = null;

    S.ceoPauseBusy = false;
    S.ceoUploadBusy = false;
    S.ceoSessionCatalogBusy = false;
    S.ceoSessionBusy = true;
    S.ceoSessionPanelExpanded = true;
    S.ceoBulkMode = false;
    S.ceoSelectedSessionIds = __makeSet();
    S.ceoSessionTab = "local";
    S.ceoLocalSessions = [
        { session_id: "web:1", title: "Local 1" },
        { session_id: "web:2", title: "Local 2" },
    ];
    S.ceoChannelGroups = [];

    syncCeoSessionActions();

    assert.equal(newSession.disabled, false);
    assert.equal(bulkToggle.disabled, false);
    assert.equal(bulkSelectAll.disabled, false);
    assert.equal(bulkDelete.disabled, true);
    assert.deepEqual(bulkCheckboxes.map((item) => item.disabled), [false, false]);
});

test("requestDeleteCeoSession hides task checkbox when there are no related task records", async () => {
    const { S, requestDeleteCeoSession, __context } = loadApp();

    S.ceoSessions = [
        { session_id: "web:1", title: "Local Alpha", session_family: "local" },
    ];
    __context.ApiClient.getCeoSessionDeleteCheck = async () => ({
        related_tasks: { total: 0, deletable: 0, in_progress: 0 },
    });
    vm.runInContext(
        "openConfirm = (payload) => { this.__capturedConfirm = payload; };",
        __context
    );

    await requestDeleteCeoSession("web:1");

    assert.ok(__context.__capturedConfirm);
    assert.equal(__context.__capturedConfirm.checkbox, null);
});

test("requestProjectExit hides pause checkbox when there is no running work", async () => {
    const { requestProjectExit, __context } = loadApp();

    __context.ApiClient.getBootstrapExitCheck = async () => ({
        has_running_work: false,
        summary_text: "",
    });
    vm.runInContext(
        "openConfirm = (payload) => { this.__capturedConfirm = payload; };",
        __context
    );

    await requestProjectExit();

    assert.ok(__context.__capturedConfirm);
    assert.equal(__context.__capturedConfirm.checkbox, null);
});

test("setCeoSessionTab clears bulk selection when changing tabs", () => {
    const { S, setCeoSessionTab, __makeSet } = loadApp();

    S.ceoBulkMode = true;
    S.ceoSelectedSessionIds = __makeSet(["web:1"]);

    setCeoSessionTab("channel");

    assert.equal(S.ceoSelectedSessionIds.size, 0);
});

test("renderCeoSessionCard shows checkbox markup in bulk mode", () => {
    const { S, renderCeoSessionCard, __makeSet } = loadApp();

    S.ceoBulkMode = true;
    S.ceoSelectedSessionIds = __makeSet(["web:1"]);

    const html = renderCeoSessionCard(
        { session_id: "web:1", title: "Alpha", preview_text: "", created_at: "2026-04-09T10:00:00+08:00" },
        { allowActions: true }
    );

    assert.match(html, /data-session-bulk-checkbox="web:1"/);
    assert.match(html, /ceo-session-checkbox/);
});

test("buildCeoBulkDeleteSummary deduplicates related task ids across sessions", () => {
    const { buildCeoBulkDeleteSummary } = loadApp();

    const summary = buildCeoBulkDeleteSummary([
        {
            session_id: "web:1",
            deleteCheck: {
                related_tasks: { total: 1, deletable: 1, in_progress: 0 },
                usage: {
                    completed_tasks: ["task:1"],
                    paused_tasks: [],
                    in_progress_tasks: [],
                },
            },
        },
        {
            session_id: "china:1",
            deleteCheck: {
                related_tasks: { total: 2, deletable: 1, in_progress: 1 },
                usage: {
                    completed_tasks: ["task:1"],
                    paused_tasks: ["task:2"],
                    in_progress_tasks: [],
                },
            },
        },
    ]);

    assert.match(summary.checkboxDetails, /task:1/);
    assert.match(summary.checkboxDetails, /task:2/);
    assert.equal((summary.checkboxDetails.match(/task:1/g) || []).length, 1);
});

test("requestDeleteSelectedCeoSessions opens one aggregated confirm dialog", async () => {
    const { S, requestDeleteSelectedCeoSessions, __context, __makeSet } = loadApp();
    let confirmPayload = null;

    S.ceoSelectedSessionIds = __makeSet(["web:1", "china:1"]);
    S.ceoSessions = [
        { session_id: "web:1", title: "Local Alpha", session_family: "local" },
        { session_id: "china:1", title: "Channel Beta", session_family: "channel", session_origin: "china" },
    ];
    __context.ApiClient.getCeoSessionDeleteCheck = async (sessionId) => (
        sessionId === "web:1"
            ? {
                related_tasks: { total: 1, deletable: 1, in_progress: 0 },
                usage: {
                    completed_tasks: ["task:1"],
                    paused_tasks: [],
                    in_progress_tasks: [],
                },
            }
            : {
                related_tasks: { total: 2, deletable: 1, in_progress: 1 },
                usage: {
                    completed_tasks: ["task:1"],
                    paused_tasks: ["task:2"],
                    in_progress_tasks: [],
                },
            }
    );
    vm.runInContext(
        "openConfirm = (payload) => { this.__capturedConfirm = payload; };",
        __context
    );

    await requestDeleteSelectedCeoSessions();

    confirmPayload = __context.__capturedConfirm;

    assert.ok(confirmPayload);
    assert.equal(confirmPayload.title, "批量清理会话");
    assert.match(confirmPayload.text, /将删除所选 1 个本地会话的聊天记录与附件。/);
    assert.match(confirmPayload.text, /将清空所选 1 个渠道会话的上下文与附件。/);
    assert.doesNotMatch(confirmPayload.text, /会话列表：/);
    assert.doesNotMatch(confirmPayload.text, /Local Alpha/);
    assert.doesNotMatch(confirmPayload.text, /Channel Beta/);
    assert.equal(confirmPayload.checkbox.label, "清除关联任务");
    assert.match(confirmPayload.checkbox.details, /task:1/);
    assert.match(confirmPayload.checkbox.details, /task:2/);
});
