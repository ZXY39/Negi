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

function loadApp({ getSessionLinks, putSessionLinks } = {}) {
    const document = new StubDocument();
    for (const id of [
        "ceo-session-link-backdrop",
        "ceo-session-link-dialog",
        "ceo-session-link-list",
        "ceo-session-link-error",
        "ceo-session-link-save-btn",
        "ceo-session-link-close-btn",
    ]) {
        document._byId[id] = new StubHTMLElement("DIV");
    }
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
        `${APP_CODE}\nthis.__testExports = { S, U, renderCeoSessionCard, openCeoSessionLinkDialog, closeCeoSessionLinkDialog, saveCeoSessionLinks, renderCeoSessionLinkList, readCheckedSessionLinkIds, ceoSessionLinkState };`,
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
    return { ...context.__testExports, __context: context };
}

const SESSIONS = [
    { session_id: "web:ceo-a", title: "甲", session_family: "local" },
    { session_id: "web:ceo-b", title: "乙", session_family: "local" },
    { session_id: "ext:qq-official:abc123", title: "QQ 群", session_family: "channel" },
    { session_id: "internal:persona_photo", title: "隐藏", session_family: "local" },
];

test("session card menu carries the link action", () => {
    const { renderCeoSessionCard } = loadApp();
    const html = renderCeoSessionCard(SESSIONS[0], { allowActions: true });
    assert.ok(html.includes('data-session-link="web:ceo-a"'));
    assert.ok(html.includes("关联其他会话"));
});

test("link dialog lists candidates excluding self and internal, prechecks linked", async () => {
    const { S, U, openCeoSessionLinkDialog, ceoSessionLinkState } = loadApp({
        getSessionLinks: async () => ({ ok: true, linked: ["web:ceo-b"] }),
    });
    S.ceoSessions = SESSIONS;
    await openCeoSessionLinkDialog("web:ceo-a");
    assert.deepEqual(ceoSessionLinkState.linked, ["web:ceo-b"]);
    assert.equal(U.ceoSessionLinkBackdrop.hidden, false);
    const html = String(U.ceoSessionLinkList.innerHTML);
    assert.ok(html.includes('data-link-session-id="web:ceo-b"'));
    assert.ok(html.includes('data-link-session-id="ext:qq-official:abc123"'));
    assert.ok(!html.includes('data-link-session-id="web:ceo-a"'));
    assert.ok(!html.includes("internal:persona_photo"));
    const checkedB = html.match(/data-link-session-id="web:ceo-b"[^>]*checked/);
    assert.ok(checkedB, "linked peer must be prechecked");
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