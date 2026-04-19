const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const APP_PATH = "g3ku/web/frontend/org_graph_app.js";
const APP_CODE = fs.readFileSync(APP_PATH, "utf8");
const APP_CSS = fs.readFileSync("g3ku/web/frontend/org_graph.css", "utf8");

class StubElement {}
class StubHTMLElement extends StubElement {
    constructor() {
        super();
        this.hidden = false;
        this.disabled = false;
        this.value = "";
        this.textContent = "";
        this.className = "";
        this.dataset = {};
        this.style = {};
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
                const classes = new Set(String(this.className || "").split(/\s+/).filter(Boolean));
                const shouldAdd = force == null ? !classes.has(token) : !!force;
                if (shouldAdd) classes.add(token);
                else classes.delete(token);
                this.className = [...classes].join(" ");
                return shouldAdd;
            },
        };
    }
    addEventListener() {}
    setAttribute(name, value) {
        this.attributes[name] = String(value);
    }
    getBoundingClientRect() {
        return { width: 420, height: 84 };
    }
}
class StubHTMLButtonElement extends StubHTMLElement {}
class StubHTMLInputElement extends StubHTMLElement {}
class StubHTMLTextAreaElement extends StubHTMLElement {}
class StubSVGElement extends StubHTMLElement {}
class StubSVGPathElement extends StubHTMLElement {
    constructor() {
        super();
        this.totalLength = 400;
    }
    getTotalLength() {
        return this.totalLength;
    }
}

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
        SVGElement: StubSVGElement,
        SVGPathElement: StubSVGPathElement,
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
        this.__testExports = {
            S,
            U,
            refreshCeoComposerUsageEstimate,
            buildCeoComposerPreflightEntries,
            syncCeoComposerUsageOutline,
        };`,
        context
    );
    context.__testExports.__context = context;
    return context.__testExports;
}

test("composer preflight entries include queued follow-ups plus current draft", () => {
    const { buildCeoComposerPreflightEntries, S, U } = loadApp();
    S.activeSessionId = "web:test";
    S.ceoQueuedFollowUps = {
        "web:test": [
            { id: "q1", text: "queued-1", uploads: [] },
            { id: "q2", text: "queued-2", uploads: [{ name: "a.txt", path: "/tmp/a.txt", kind: "file", size: 12 }] },
        ],
    };
    S.ceoUploads = [{ name: "draft.txt", path: "/tmp/draft.txt", kind: "file", size: 18 }];
    U.ceoInput = new StubHTMLTextAreaElement();
    U.ceoInput.value = "draft-now";

    const entries = buildCeoComposerPreflightEntries("web:test");

    assert.deepEqual(
        entries.map((item) => [item.text, (item.uploads || []).length]),
        [["queued-1", 0], ["queued-2", 1], ["draft-now", 1]]
    );
});

test("composer usage outline maps ratio into clockwise stroke progress", () => {
    const { syncCeoComposerUsageOutline, S, U } = loadApp();
    S.activeSessionId = "web:test";
    S.ceoComposerUsageEstimate = {
        session_id: "web:test",
        ratio: 0.25,
        estimated_total_tokens: 8000,
        context_window_tokens: 32000,
        provider_model: "openai:gpt-5.2",
    };
    U.ceoComposerOutlineShell = new StubHTMLElement();
    U.ceoComposerOutlineSvg = new StubSVGElement();
    U.ceoComposerOutlineTrack = new StubSVGPathElement();
    U.ceoComposerOutlineProgress = new StubSVGPathElement();

    syncCeoComposerUsageOutline();

    assert.equal(U.ceoComposerOutlineShell.classList.contains("is-active"), true);
    assert.equal(U.ceoComposerOutlineShell.classList.contains("is-force-visible"), false);
    assert.equal(U.ceoComposerOutlineProgress.style.strokeDasharray, "100 400");
});

test("tiny ratio still renders a visible minimum token segment", () => {
    const { syncCeoComposerUsageOutline, S, U } = loadApp();
    S.activeSessionId = "web:test";
    S.ceoComposerUsageEstimate = {
        session_id: "web:test",
        ratio: 0.002,
        estimated_total_tokens: 64,
        context_window_tokens: 32000,
        provider_model: "openai:gpt-5.2",
    };
    U.ceoComposerOutlineShell = new StubHTMLElement();
    U.ceoComposerOutlineSvg = new StubSVGElement();
    U.ceoComposerOutlineTrack = new StubSVGPathElement();
    U.ceoComposerOutlineProgress = new StubSVGPathElement();

    syncCeoComposerUsageOutline();

    const [visibleLength] = String(U.ceoComposerOutlineProgress.style.strokeDasharray || "0 0")
        .split(" ")
        .map((item) => Number(item || 0));
    assert.equal(U.ceoComposerOutlineShell.classList.contains("is-force-visible"), false);
    assert.ok(visibleLength >= 20);
    assert.ok(visibleLength < 400);
});

test("active turn forces visible outline before estimate arrives", () => {
    const { syncCeoComposerUsageOutline, S, U } = loadApp();
    S.activeSessionId = "web:test";
    S.ceoTurnActive = true;
    S.ceoComposerUsageEstimate = null;
    U.ceoComposerOutlineShell = new StubHTMLElement();
    U.ceoComposerOutlineSvg = new StubSVGElement();
    U.ceoComposerOutlineTrack = new StubSVGPathElement();
    U.ceoComposerOutlineProgress = new StubSVGPathElement();

    syncCeoComposerUsageOutline();

    assert.equal(U.ceoComposerOutlineShell.classList.contains("is-active"), true);
    assert.equal(U.ceoComposerOutlineShell.classList.contains("is-force-visible"), true);
    assert.equal(U.ceoComposerOutlineProgress.style.strokeDasharray, "400 400");
});

test("estimate mode no longer paints a full textarea border fallback", () => {
    assert.equal(
        APP_CSS.includes(".ceo-composer-outline-shell.is-active textarea:not(:focus)"),
        false
    );
    assert.equal(
        APP_CSS.includes(".ceo-composer-outline-shell.is-force-visible textarea:not(:focus)"),
        true
    );
});

test("active turn keeps last composer usage outline after draft is cleared", async () => {
    const { refreshCeoComposerUsageEstimate, S, U } = loadApp();
    S.activeSessionId = "web:test";
    S.ceoTurnActive = true;
    S.ceoComposerUsageEstimate = {
        session_id: "web:test",
        ratio: 0.52,
        estimated_total_tokens: 16640,
        context_window_tokens: 32000,
        provider_model: "openai:gpt-5.2",
    };
    S.ceoUploads = [];
    U.ceoInput = new StubHTMLTextAreaElement();
    U.ceoInput.value = "";

    const result = await refreshCeoComposerUsageEstimate();

    assert.equal(result.estimated_total_tokens, 16640);
    assert.equal(S.ceoComposerUsageEstimate.estimated_total_tokens, 16640);
});

test("active turn can estimate from pinned sent entries after composer clears", async () => {
    const { refreshCeoComposerUsageEstimate, S, U, __context } = loadApp();
    S.activeSessionId = "web:test";
    S.ceoTurnActive = true;
    S.ceoComposerUsageEstimate = null;
    S.ceoComposerUsagePinnedEntries = {
        session_id: "web:test",
        entries: [{ text: "already-sent", uploads: [] }],
    };
    S.ceoUploads = [];
    U.ceoInput = new StubHTMLTextAreaElement();
    U.ceoInput.value = "";
    __context.ApiClient = {
        estimateCeoComposerPreflight: async () => ({
            estimated_total_tokens: 12000,
            context_window_tokens: 32000,
            ratio: 0.375,
            provider_model: "openai:gpt-5.2",
            trigger_tokens: 25600,
            would_trigger_token_compression: false,
            would_exceed_context_window: false,
            missing_context_window: false,
        }),
    };

    const result = await refreshCeoComposerUsageEstimate();

    assert.equal(result.estimated_total_tokens, 12000);
    assert.equal(S.ceoComposerUsageEstimate.estimated_total_tokens, 12000);
});

test("active turn can estimate from inflight snapshot after refresh clears pinned state", async () => {
    const { refreshCeoComposerUsageEstimate, S, U, __context } = loadApp();
    S.activeSessionId = "web:test";
    S.ceoTurnActive = true;
    S.ceoComposerUsageEstimate = null;
    S.ceoComposerUsagePinnedEntries = null;
    S.ceoSnapshotCache = {
        "web:test": {
            session_id: "web:test",
            inflight_turn: {
                status: "running",
                user_message: {
                    content: "snapshot-user",
                    attachments: [{ name: "note.txt", path: "/tmp/note.txt", kind: "file", size: 22 }],
                },
            },
        },
    };
    S.ceoUploads = [];
    U.ceoInput = new StubHTMLTextAreaElement();
    U.ceoInput.value = "";
    __context.ApiClient = {
        estimateCeoComposerPreflight: async () => ({
            estimated_total_tokens: 15000,
            context_window_tokens: 32000,
            ratio: 0.46875,
            provider_model: "openai:gpt-5.2",
            trigger_tokens: 25600,
            would_trigger_token_compression: false,
            would_exceed_context_window: false,
            missing_context_window: false,
        }),
    };

    const result = await refreshCeoComposerUsageEstimate();

    assert.equal(result.estimated_total_tokens, 15000);
    assert.equal(S.ceoComposerUsageEstimate.estimated_total_tokens, 15000);
});

test("snapshot refresh does not force another composer estimate once token mode is active", () => {
    const { S, U, __context } = loadApp();
    S.activeSessionId = "web:test";
    S.ceoTurnActive = true;
    S.ceoComposerUsageEstimate = {
        session_id: "web:test",
        ratio: 0.4,
        estimated_total_tokens: 12800,
        context_window_tokens: 32000,
        provider_model: "openai:gpt-5.2",
    };
    U.ceoCompressionToast = new StubHTMLElement();
    U.ceoCompressionToastText = new StubHTMLElement();
    U.ceoCompressionActions = new StubHTMLElement();
    U.ceoCompressionPause = new StubHTMLButtonElement();
    let scheduled = 0;
    __context.scheduleCeoComposerUsageRefresh = () => {
        scheduled += 1;
    };

    __context.setCeoSessionSnapshotCache("web:test", {
        inflight_turn: {
            status: "running",
            user_message: { content: "hello" },
        },
    });

    assert.equal(scheduled, 0);
});

test("composer estimate refreshes serialize while active turn is streaming", async () => {
    const { S, U, __context } = loadApp();
    S.activeSessionId = "web:test";
    S.ceoTurnActive = true;
    S.ceoComposerUsageEstimate = null;
    S.ceoComposerUsagePinnedEntries = {
        session_id: "web:test",
        entries: [{ text: "already-sent", uploads: [] }],
    };
    U.ceoInput = new StubHTMLTextAreaElement();
    U.ceoInput.value = "";
    let callCount = 0;
    __context.ApiClient = {
        estimateCeoComposerPreflight: async () => {
            callCount += 1;
            await new Promise((resolve) => setTimeout(resolve, 20));
            return {
                estimated_total_tokens: 11000,
                context_window_tokens: 32000,
                ratio: 0.34375,
                provider_model: "openai:gpt-5.2",
                trigger_tokens: 25600,
                would_trigger_token_compression: false,
                would_exceed_context_window: false,
                missing_context_window: false,
            };
        },
    };

    __context.scheduleCeoComposerUsageRefresh({ immediate: true });
    await new Promise((resolve) => setTimeout(resolve, 5));
    __context.scheduleCeoComposerUsageRefresh({ immediate: true });
    await new Promise((resolve) => setTimeout(resolve, 5));
    __context.scheduleCeoComposerUsageRefresh({ immediate: true });
    await new Promise((resolve) => setTimeout(resolve, 80));

    assert.ok(callCount <= 2);
    assert.equal(S.ceoComposerUsageEstimate.estimated_total_tokens, 11000);
});
