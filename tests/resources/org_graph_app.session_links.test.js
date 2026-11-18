const test = require("node:test");
const assert = require("node:assert/strict");
const vm = require("node:vm");
const { readFrontendSources } = require("./support/frontend_sources.js");

const APP_CODE = readFrontendSources(["app"]).join("\n");

const {
    StubElement,
    StubHTMLElement,
    StubDocument,
    StubHTMLButtonElement,
    StubHTMLInputElement,
    StubHTMLTextAreaElement,
    StubHTMLSelectElement,
} = require("./support/frontend_stub.js");

function loadApp({ getSessionLinks, putSessionLinks, storedSort = null } = {}) {
    const document = new StubDocument();
    for (const id of [
        "ceo-session-link-backdrop",
        "ceo-session-link-dialog",
        "ceo-session-link-tabs",
        "ceo-session-link-sort",
        "ceo-session-link-list",
        "ceo-session-link-error",
        "ceo-session-link-save-btn",
        "ceo-session-link-close-btn",
    ]) {
        document._byId[id] = new StubHTMLElement("DIV");
    }
    const storage = new Map();
    if (storedSort) storage.set("g3ku.ui.ceo.session-link-sort.v1", storedSort);
    const context = {
        console,
        setTimeout,
        clearTimeout,
        setInterval,
        clearInterval,
        queueMicrotask,
        navigator: { clipboard: { writeText: async () => {} } },
        location: { protocol: "http:", host: "localhost", pathname: "/org_graph.html" },
        localStorage: {
            getItem: (key) => (storage.has(key) ? storage.get(key) : null),
            setItem: (key, value) => storage.set(key, String(value)),
            removeItem: (key) => storage.delete(key),
        },
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
        ApiClient: {
            getActiveSessionId: () => "",
            getSessionLinks: getSessionLinks || (async () => ({ ok: true, linked: [] })),
            putSessionLinks: putSessionLinks || (async () => ({ ok: true, linked: [] })),
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
        `${APP_CODE}\nthis.__testExports = { S, U, renderCeoSessionCard, openCeoSessionLinkDialog, closeCeoSessionLinkDialog, saveCeoSessionLinks, renderCeoSessionLinkList, readCheckedSessionLinkIds, setCeoSessionLinkTab, setCeoSessionLinkSort, syncCeoSessionLinkSortButtons, ceoSessionLinkState };`,
        context
    );
    vm.runInContext(
        `
        renderCeoSessions = () => {};
        syncCeoPrimaryButton = () => {};
        syncCeoComposerReadonlyState = () => {};
        syncCeoAttachButton = () => {};
        syncCeoCompressionDivider = () => {};
        showToast = () => {};
    `,
        context
    );
    return { ...context.__testExports, __context: context, __storage: storage };
}

const SESSIONS = [
    { session_id: "web:ceo-a", title: "甲", session_family: "local", created_at: "2026-11-01T00:00:00", updated_at: "2026-11-10T00:00:00" },
    { session_id: "web:ceo-b", title: "乙", session_family: "local", created_at: "2026-11-05T00:00:00", updated_at: "2026-11-08T00:00:00" },
    { session_id: "ext:qq-official:abc123", title: "QQ 群", session_family: "channel", created_at: "2026-11-02T00:00:00", updated_at: "2026-11-12T00:00:00" },
    { session_id: "internal:persona_photo", title: "隐藏", session_family: "local" },
];

test("session card menu carries the link action", () => {
    const { renderCeoSessionCard } = loadApp();
    const html = renderCeoSessionCard(SESSIONS[0], { allowActions: true });
    assert.ok(html.includes('data-session-link="web:ceo-a"'));
    assert.ok(html.includes("关联其他会话"));
});

test("link dialog defaults to local tab, prechecks linked, excludes self/internal", async () => {
    const { S, U, openCeoSessionLinkDialog, ceoSessionLinkState } = loadApp({
        getSessionLinks: async () => ({ ok: true, linked: ["web:ceo-b"] }),
    });
    S.ceoSessions = SESSIONS;
    await openCeoSessionLinkDialog("web:ceo-a");
    assert.deepEqual(ceoSessionLinkState.linked, ["web:ceo-b"]);
    assert.equal(ceoSessionLinkState.tab, "local");
    assert.equal(U.ceoSessionLinkBackdrop.hidden, false);
    const html = String(U.ceoSessionLinkList.innerHTML);
    assert.ok(html.includes('data-link-session-id="web:ceo-b"'));
    assert.ok(!html.includes('data-link-session-id="web:ceo-a"'));
    assert.ok(!html.includes("internal:persona_photo"));
    // Channel rows live behind the channel tab, not in the local projection.
    assert.ok(!html.includes("ext:qq-official"));
    const checkedB = html.match(/data-link-session-id="web:ceo-b"[^>]*checked/);
    assert.ok(checkedB, "linked peer must be prechecked");
});

test("channel tab shows channel rows with bridge label; draft survives tab switches", async () => {
    const calls = [];
    const { S, U, openCeoSessionLinkDialog, setCeoSessionLinkTab, saveCeoSessionLinks, ceoSessionLinkState } = loadApp({
        getSessionLinks: async () => ({ ok: true, linked: [] }),
        putSessionLinks: async (id, links) => {
            calls.push([id, [...links].sort()]);
            return { ok: true, linked: links };
        },
    });
    S.ceoSessions = SESSIONS;
    await openCeoSessionLinkDialog("web:ceo-a");
    const tabPressed = {};
    U.ceoSessionLinkTabs.querySelectorAll = () => [
        { getAttribute: () => "local", setAttribute: (k, v) => { tabPressed.local = v; } },
        { getAttribute: () => "channel", setAttribute: (k, v) => { tabPressed.channel = v; } },
    ];
    // Check a local row, switch to channel, check a channel row, switch back.
    U.ceoSessionLinkList.querySelectorAll = () => [
        { checked: true, getAttribute: () => "web:ceo-b" },
    ];
    setCeoSessionLinkTab("channel");
    assert.deepEqual(tabPressed, { local: "false", channel: "true" });
    let html = String(U.ceoSessionLinkList.innerHTML);
    assert.ok(html.includes('data-link-session-id="ext:qq-official:abc123"'));
    assert.ok(html.includes(">QQ</span>"), "channel row carries the bridge label");
    assert.ok(!html.includes('data-link-session-id="web:ceo-b"'), "local rows hidden on channel tab");
    U.ceoSessionLinkList.querySelectorAll = () => [
        { checked: true, getAttribute: () => "ext:qq-official:abc123" },
    ];
    setCeoSessionLinkTab("local");
    html = String(U.ceoSessionLinkList.innerHTML);
    assert.ok(html.match(/data-link-session-id="web:ceo-b"[^>]*checked/), "local draft preserved");
    assert.deepEqual([...ceoSessionLinkState.draft].sort(), ["ext:qq-official:abc123", "web:ceo-b"]);
    // Save sends the union of both tabs' drafts.
    U.ceoSessionLinkList.querySelectorAll = () => [
        { checked: true, getAttribute: () => "web:ceo-b" },
    ];
    await saveCeoSessionLinks();
    assert.deepEqual(calls, [["web:ceo-a", ["ext:qq-official:abc123", "web:ceo-b"]]]);
});

test("save sends checked ids and closes the dialog", async () => {
    const calls = [];
    const { S, U, openCeoSessionLinkDialog, saveCeoSessionLinks, ceoSessionLinkState } = loadApp({
        getSessionLinks: async () => ({ ok: true, linked: [] }),
        putSessionLinks: async (id, links) => {
            calls.push([id, links]);
            return { ok: true, linked: links };
        },
    });
    S.ceoSessions = SESSIONS;
    await openCeoSessionLinkDialog("web:ceo-a");
    U.ceoSessionLinkList.querySelectorAll = () => [
        { checked: true, getAttribute: () => "web:ceo-b" },
        { checked: false, getAttribute: () => "ext:qq-official:abc123" },
    ];
    await saveCeoSessionLinks();
    assert.equal(JSON.stringify(calls), JSON.stringify([["web:ceo-a", ["web:ceo-b"]]]));
    assert.equal(U.ceoSessionLinkBackdrop.hidden, true);
    assert.equal(JSON.stringify(ceoSessionLinkState.linked), JSON.stringify(["web:ceo-b"]));
});

test("save failure stays in the dialog with the rejection reason", async () => {
    const { S, U, openCeoSessionLinkDialog, saveCeoSessionLinks } = loadApp({
        getSessionLinks: async () => ({ ok: true, linked: [] }),
        putSessionLinks: async () => {
            const error = new Error("HTTP 400");
            error.data = { reason: "session_not_linkable", target: "web:ceo-b" };
            throw error;
        },
    });
    S.ceoSessions = SESSIONS;
    await openCeoSessionLinkDialog("web:ceo-a");
    U.ceoSessionLinkList.querySelectorAll = () => [
        { checked: true, getAttribute: () => "web:ceo-b" },
    ];
    await saveCeoSessionLinks();
    assert.equal(U.ceoSessionLinkBackdrop.hidden, false);
    assert.equal(U.ceoSessionLinkError.hidden, false);
    assert.ok(String(U.ceoSessionLinkError.textContent).includes("session_not_linkable"));
    assert.ok(String(U.ceoSessionLinkError.textContent).includes("web:ceo-b"));
});

test("default sort is latest-active within each group", async () => {
    const { S, U, openCeoSessionLinkDialog } = loadApp();
    S.ceoSessions = SESSIONS;
    await openCeoSessionLinkDialog("web:ceo-owner");
    const html = String(U.ceoSessionLinkList.innerHTML);
    // A updated 11-10 > B updated 11-08, while created order is the opposite.
    assert.ok(html.indexOf('data-link-session-id="web:ceo-a"') < html.indexOf('data-link-session-id="web:ceo-b"'));
});

test("sort toggle reorders by created, keeps draft, persists preference", async () => {
    const { S, U, openCeoSessionLinkDialog, setCeoSessionLinkSort, ceoSessionLinkState, __storage } = loadApp();
    S.ceoSessions = SESSIONS;
    await openCeoSessionLinkDialog("web:ceo-owner");
    // Unsaved draft: A checked, B not.
    U.ceoSessionLinkList.querySelectorAll = () => [
        { checked: true, getAttribute: () => "web:ceo-a" },
        { checked: false, getAttribute: () => "web:ceo-b" },
    ];
    const pressed = {};
    U.ceoSessionLinkSort.querySelectorAll = () => [
        { getAttribute: () => "active", setAttribute: (k, v) => { pressed.active = v; } },
        { getAttribute: () => "created", setAttribute: (k, v) => { pressed.created = v; } },
    ];
    setCeoSessionLinkSort("created");
    assert.equal(ceoSessionLinkState.sort, "created");
    assert.equal(__storage.get("g3ku.ui.ceo.session-link-sort.v1"), "created");
    assert.deepEqual(pressed, { active: "false", created: "true" });
    const html = String(U.ceoSessionLinkList.innerHTML);
    // Created: B 11-05 > A 11-01.
    assert.ok(html.indexOf('data-link-session-id="web:ceo-b"') < html.indexOf('data-link-session-id="web:ceo-a"'));
    // Draft survived the re-render.
    const rowA = html.slice(html.indexOf('data-link-session-id="web:ceo-a"'));
    assert.ok(rowA.slice(0, 120).includes("checked"), "checked draft preserved across sort");
});

test("stored sort preference is restored on load", () => {
    const { ceoSessionLinkState } = loadApp({ storedSort: "created" });
    assert.equal(ceoSessionLinkState.sort, "created");
    const fresh = loadApp();
    assert.equal(fresh.ceoSessionLinkState.sort, "active");
});