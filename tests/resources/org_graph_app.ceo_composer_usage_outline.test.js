const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const APP_PATH = "g3ku/web/frontend/org_graph_app.js";
const APP_CODE = fs.readFileSync(APP_PATH, "utf8");
const APP_CSS = fs.readFileSync("g3ku/web/frontend/org_graph.css", "utf8");
const APP_HTML = fs.readFileSync("g3ku/web/frontend/org_graph.html", "utf8");

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
        this.style = {
            setProperty(name, value) {
                this[name] = String(value);
            },
            removeProperty(name) {
                delete this[name];
            },
        };
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
            activeCeoRuntimeUsageEstimate,
            buildCeoComposerPreflightEntries,
            syncCeoComposerUsageOutline,
            applyCeoState,
            setCeoSessionSnapshotCache,
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

test("composer usage brain maps ratio into progressive icon fill", () => {
    const { syncCeoComposerUsageOutline, S, U } = loadApp();
    S.activeSessionId = "web:test";
    S.ceoComposerUsageEstimate = {
        session_id: "web:test",
        ratio: 0.25,
        estimated_total_tokens: 8000,
        context_window_tokens: 32000,
        provider_model: "openai:gpt-5.2",
    };
    U.ceoComposerUsageBrain = new StubHTMLElement();
    U.ceoComposerUsageBrainBase = new StubHTMLElement();
    U.ceoComposerUsageBrainFill = new StubHTMLElement();

    syncCeoComposerUsageOutline();

    assert.equal(U.ceoComposerUsageBrain.classList.contains("is-active"), true);
    assert.equal(U.ceoComposerUsageBrain.classList.contains("is-pending"), false);
    assert.equal(U.ceoComposerUsageBrain.dataset.usageState, "active");
    assert.equal(U.ceoComposerUsageBrain.title, "openai:gpt-5.2 · 8000/32000 TOKEN");
    assert.equal(U.ceoComposerUsageBrain.attributes["aria-label"], "openai:gpt-5.2 · 8000/32000 TOKEN");
    assert.match(String(U.ceoComposerUsageBrain.style["--ceo-context-usage-color"] || ""), /^hsl\(/);
    assert.equal(U.ceoComposerUsageBrainBase.style.height, "75%");
    assert.equal(U.ceoComposerUsageBrainFill.style.height, "25%");
});

test("composer usage brain clamps overflow state to a full fill", () => {
    const { syncCeoComposerUsageOutline, S, U } = loadApp();
    S.activeSessionId = "web:test";
    S.ceoComposerUsageEstimate = {
        session_id: "web:test",
        ratio: 1.42,
        estimated_total_tokens: 45500,
        context_window_tokens: 32000,
        provider_model: "openai:gpt-5.2",
        would_exceed_context_window: true,
    };
    U.ceoComposerUsageBrain = new StubHTMLElement();
    U.ceoComposerUsageBrainBase = new StubHTMLElement();
    U.ceoComposerUsageBrainFill = new StubHTMLElement();

    syncCeoComposerUsageOutline();

    assert.equal(U.ceoComposerUsageBrain.dataset.usageState, "overflow");
    assert.equal(U.ceoComposerUsageBrainBase.style.height, "0%");
    assert.equal(U.ceoComposerUsageBrainFill.style.height, "100%");
});

test("tiny ratio still renders a visible minimum token fill", () => {
    const { syncCeoComposerUsageOutline, S, U } = loadApp();
    S.activeSessionId = "web:test";
    S.ceoComposerUsageEstimate = {
        session_id: "web:test",
        ratio: 0.002,
        estimated_total_tokens: 64,
        context_window_tokens: 32000,
        provider_model: "openai:gpt-5.2",
    };
    U.ceoComposerUsageBrain = new StubHTMLElement();
    U.ceoComposerUsageBrainBase = new StubHTMLElement();
    U.ceoComposerUsageBrainFill = new StubHTMLElement();

    syncCeoComposerUsageOutline();

    const visibleHeight = Number.parseFloat(String(U.ceoComposerUsageBrainFill.style.height || "0"));
    const baseHeight = Number.parseFloat(String(U.ceoComposerUsageBrainBase.style.height || "0"));
    assert.equal(U.ceoComposerUsageBrain.classList.contains("is-pending"), false);
    assert.ok(visibleHeight >= 6);
    assert.ok(visibleHeight < 100);
    assert.ok(baseHeight > visibleHeight);
});

test("brain stays empty when no exact next-request estimate is available", () => {
    const { syncCeoComposerUsageOutline, S, U } = loadApp();
    S.activeSessionId = "web:test";
    S.ceoTurnActive = false;
    S.ceoComposerUsageEstimate = null;
    U.ceoComposerUsageBrain = new StubHTMLElement();
    U.ceoComposerUsageBrainBase = new StubHTMLElement();
    U.ceoComposerUsageBrainFill = new StubHTMLElement();

    syncCeoComposerUsageOutline();

    assert.equal(U.ceoComposerUsageBrain.classList.contains("is-active"), false);
    assert.equal(U.ceoComposerUsageBrain.classList.contains("is-pending"), false);
    assert.equal(U.ceoComposerUsageBrain.title, "");
    assert.equal(U.ceoComposerUsageBrain.attributes["aria-label"], "");
});

test("active turn stays empty before runtime next-request snapshot arrives", () => {
    const { syncCeoComposerUsageOutline, S, U } = loadApp();
    S.activeSessionId = "web:test";
    S.ceoTurnActive = true;
    S.ceoComposerUsageEstimate = null;
    U.ceoComposerUsageBrain = new StubHTMLElement();
    U.ceoComposerUsageBrainBase = new StubHTMLElement();
    U.ceoComposerUsageBrainFill = new StubHTMLElement();

    syncCeoComposerUsageOutline();

    assert.equal(U.ceoComposerUsageBrain.classList.contains("is-active"), false);
    assert.equal(U.ceoComposerUsageBrain.classList.contains("is-pending"), false);
    assert.equal(U.ceoComposerUsageBrain.title, "");
    assert.equal(U.ceoComposerUsageBrain.attributes["aria-label"], "");
    assert.equal(U.ceoComposerUsageBrainBase.style.height, "100%");
    assert.equal(U.ceoComposerUsageBrainFill.style.height, "0%");
});

test("running state transition does not show fallback outline before runtime estimate arrives", () => {
    const { applyCeoState, S, U } = loadApp();
    S.activeSessionId = "web:test";
    S.ceoTurnActive = false;
    S.ceoComposerUsageEstimate = null;
    U.ceoComposerUsageBrain = new StubHTMLElement();
    U.ceoComposerUsageBrainBase = new StubHTMLElement();
    U.ceoComposerUsageBrainFill = new StubHTMLElement();

    applyCeoState({ status: "running", is_running: true }, { source: "user", turn_id: "turn-1" });

    assert.equal(U.ceoComposerUsageBrain.classList.contains("is-active"), false);
    assert.equal(U.ceoComposerUsageBrain.classList.contains("is-pending"), false);
    assert.equal(U.ceoComposerUsageBrain.title, "");
});

test("estimate mode renders a dedicated brain control instead of textarea outline css", () => {
    assert.equal(
        APP_CSS.includes(".ceo-composer-outline-shell"),
        false
    );
    assert.equal(
        APP_CSS.includes(".ceo-context-usage-brain"),
        true
    );
});

test("brain icon renders before attach button in composer html", () => {
    const brainIndex = APP_HTML.indexOf('id="ceo-context-usage-brain"');
    const attachIndex = APP_HTML.indexOf('id="ceo-attach-btn"');
    assert.ok(brainIndex >= 0);
    assert.ok(attachIndex >= 0);
    assert.ok(brainIndex < attachIndex);
});

test("brain icon uses actual lucide brain markup in composer html", () => {
    const brainStart = APP_HTML.indexOf('id="ceo-context-usage-brain"');
    const attachStart = APP_HTML.indexOf('id="ceo-attach-btn"');
    const section = brainStart >= 0 && attachStart > brainStart
        ? APP_HTML.slice(brainStart, attachStart)
        : "";
    assert.ok(section.includes('data-lucide="brain"'));
    assert.equal(section.includes("ceo-context-usage-brain-svg"), false);
    assert.ok(section.includes('id="ceo-context-usage-brain-base"'));
    assert.ok(section.includes('ceo-context-usage-brain-iconbox'));
});

test("brain icon no longer uses framed button shell styling", () => {
    assert.equal(
        APP_CSS.includes(".ceo-attach-btn,\n.ceo-context-usage-brain {"),
        false
    );
    assert.equal(
        APP_CSS.includes("background: color-mix(in srgb, var(--bg-app) 82%, var(--bg-panel) 18%);"),
        true
    );
});

test("leading group vertically centers brain with attach button", () => {
    assert.equal(
        APP_CSS.includes(".ceo-input-leading-group {\n    display: inline-flex;\n    align-items: center;"),
        true
    );
});

test("brain icon uses same alignment slot size as attach button", () => {
    const brainBlock = /\.ceo-context-usage-brain \{([\s\S]*?)\n\}/.exec(APP_CSS)?.[1] || "";
    assert.equal(
        brainBlock.includes("\n    width: var(--ceo-input-leading-size);"),
        true
    );
    assert.equal(
        brainBlock.includes("\n    height: var(--ceo-input-leading-size);"),
        true
    );
    assert.equal(
        brainBlock.includes("\n    top: 2px;"),
        false
    );
});

test("brain icon uses crisp native lucide sizing", () => {
    assert.equal(
        APP_CSS.includes(".ceo-context-usage-brain-iconbox {\n    position: relative;\n    width: 24px;\n    height: 24px;"),
        true
    );
    assert.equal(
        APP_CSS.includes(".ceo-context-usage-brain-layer > svg,\n.ceo-context-usage-brain-fill-inner > svg {\n    width: 24px;\n    height: 24px;"),
        true
    );
});

test("brain icon avoids blur-inducing svg drop shadows", () => {
    assert.equal(
        APP_CSS.includes(".ceo-context-usage-brain-base > svg {\n    filter: drop-shadow"),
        false
    );
    assert.equal(
        APP_CSS.includes("filter: drop-shadow(0 0 10px"),
        false
    );
});

test("brain icon splits base and fill into non-overlapping clips", () => {
    const layerBlock = /\.ceo-context-usage-brain-layer \{([\s\S]*?)\n\}/.exec(APP_CSS)?.[1] || "";
    assert.equal(
        APP_CSS.includes(".ceo-context-usage-brain-base {\n    position: absolute;\n    top: 0;\n    align-items: flex-start;"),
        true
    );
    assert.equal(
        APP_CSS.includes(".ceo-context-usage-brain-fill {\n    position: absolute;\n    left: 0;\n    bottom: 0;\n    display: flex;\n    align-items: flex-end;"),
        true
    );
    assert.equal(
        APP_CSS.includes(".ceo-context-usage-brain-layer {\n    position: absolute;\n    left: 0;\n    width: 100%;"),
        true
    );
    assert.equal(
        layerBlock.includes("inset: 0;"),
        false
    );
});

test("brain icon color transitions continuously from green to red", () => {
    const { syncCeoComposerUsageOutline, S, U } = loadApp();
    S.activeSessionId = "web:test";
    U.ceoComposerUsageBrain = new StubHTMLElement();
    U.ceoComposerUsageBrainBase = new StubHTMLElement();
    U.ceoComposerUsageBrainFill = new StubHTMLElement();

    S.ceoComposerUsageEstimate = {
        session_id: "web:test",
        ratio: 0,
        estimated_total_tokens: 0,
        context_window_tokens: 32000,
        provider_model: "openai:gpt-5.2",
    };
    syncCeoComposerUsageOutline();
    const lowColor = String(U.ceoComposerUsageBrain.style["--ceo-context-usage-color"] || "");

    S.ceoComposerUsageEstimate = {
        session_id: "web:test",
        ratio: 0.55,
        estimated_total_tokens: 17600,
        context_window_tokens: 32000,
        provider_model: "openai:gpt-5.2",
    };
    syncCeoComposerUsageOutline();
    const midColor = String(U.ceoComposerUsageBrain.style["--ceo-context-usage-color"] || "");

    S.ceoComposerUsageEstimate = {
        session_id: "web:test",
        ratio: 1,
        estimated_total_tokens: 32000,
        context_window_tokens: 32000,
        provider_model: "openai:gpt-5.2",
    };
    syncCeoComposerUsageOutline();
    const highColor = String(U.ceoComposerUsageBrain.style["--ceo-context-usage-color"] || "");

    assert.notEqual(lowColor, midColor);
    assert.notEqual(midColor, highColor);
    assert.match(lowColor, /^hsl\(/);
    assert.match(midColor, /^hsl\(/);
    assert.match(highColor, /^hsl\(/);
});

test("brain icon no longer hardcodes warning and overflow colors in css", () => {
    assert.equal(
        APP_CSS.includes('.ceo-context-usage-brain[data-usage-state="warning"]'),
        false
    );
    assert.equal(
        APP_CSS.includes('.ceo-context-usage-brain[data-usage-state="overflow"]'),
        false
    );
});

test("active turn clears stale composer estimate after draft is cleared when runtime snapshot is unavailable", async () => {
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

    assert.equal(result, null);
    assert.equal(S.ceoComposerUsageEstimate, null);
});

test("active turn never falls back to pinned sent entries for context usage", async () => {
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
    let callCount = 0;
    __context.ApiClient = {
        estimateCeoComposerPreflight: async () => {
            callCount += 1;
            return {
            estimated_total_tokens: 12000,
            context_window_tokens: 32000,
            ratio: 0.375,
            provider_model: "openai:gpt-5.2",
            trigger_tokens: 25600,
            would_trigger_token_compression: false,
            would_exceed_context_window: false,
            missing_context_window: false,
            };
        },
    };

    const result = await refreshCeoComposerUsageEstimate();

    assert.equal(result, null);
    assert.equal(callCount, 0);
    assert.equal(S.ceoComposerUsageEstimate, null);
});

test("active turn never guesses from inflight user message without runtime request diagnostics", async () => {
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
    let callCount = 0;
    __context.ApiClient = {
        estimateCeoComposerPreflight: async () => {
            callCount += 1;
            return {
            estimated_total_tokens: 15000,
            context_window_tokens: 32000,
            ratio: 0.46875,
            provider_model: "openai:gpt-5.2",
            trigger_tokens: 25600,
            would_trigger_token_compression: false,
            would_exceed_context_window: false,
            missing_context_window: false,
            };
        },
    };

    const result = await refreshCeoComposerUsageEstimate();

    assert.equal(result, null);
    assert.equal(callCount, 0);
    assert.equal(S.ceoComposerUsageEstimate, null);
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

test("active turn does not enqueue composer-preflight refreshes while runtime request size is unavailable", async () => {
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

    assert.equal(callCount, 0);
    assert.equal(S.ceoComposerUsageEstimate, null);
});

test("active turn prefers runtime next-request snapshot over stale composer estimate and refreshes immediately", () => {
    const { S, U, __context, activeCeoRuntimeUsageEstimate } = loadApp();
    S.activeSessionId = "web:test";
    S.ceoTurnActive = true;
    S.ceoComposerUsageEstimate = {
        session_id: "web:test",
        ratio: 0.25,
        estimated_total_tokens: 8000,
        context_window_tokens: 32000,
        provider_model: "openai:gpt-5.2",
    };
    U.ceoComposerUsageBrain = new StubHTMLElement();
    U.ceoComposerUsageBrainBase = new StubHTMLElement();
    U.ceoComposerUsageBrainFill = new StubHTMLElement();
    U.ceoCompressionToast = new StubHTMLElement();
    U.ceoCompressionToastText = new StubHTMLElement();
    U.ceoCompressionActions = new StubHTMLElement();
    U.ceoCompressionPause = new StubHTMLButtonElement();

    __context.setCeoSessionSnapshotCache("web:test", {
        inflight_turn: {
            status: "running",
            user_message: { content: "hello" },
            frontdoor_token_preflight_diagnostics: {
                final_request_tokens: 21000,
                max_context_tokens: 64000,
                trigger_tokens: 48640,
                effective_trigger_tokens: 48640,
                provider_model: "openai:gpt-5.4",
                estimate_source: "usage_plus_delta",
                effective_input_tokens: 20313,
            },
            actual_request_message_count: 12,
        },
    });

    assert.equal(U.ceoComposerUsageBrain.classList.contains("is-active"), true);
    assert.equal(U.ceoComposerUsageBrain.classList.contains("is-pending"), false);
    assert.match(U.ceoComposerUsageBrain.title, /openai:gpt-5\.4/);
    assert.match(U.ceoComposerUsageBrain.title, /21000\/64000 TOKEN/);
    assert.equal(U.ceoComposerUsageBrain.attributes["aria-label"], U.ceoComposerUsageBrain.title);
    assert.equal(U.ceoComposerUsageBrain.dataset.usageState, "active");
    const runtimeEstimate = activeCeoRuntimeUsageEstimate("web:test");
    assert.equal(runtimeEstimate.estimate_source, "usage_plus_delta");
    assert.equal(runtimeEstimate.effective_input_tokens, 20313);
});
