const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const APP_PATH = "g3ku/web/frontend/org_graph_app.js";
const APP_CODE = fs.readFileSync(APP_PATH, "utf8");
const PAUSED_LABEL = "\u5df2\u6682\u505c";
const PROCESSING_LABEL = "\u5904\u7406\u4e2d...";

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

function makeClassList(...initialTokens) {
    const tokens = new Set(initialTokens);
    return {
        add: (...nextTokens) => nextTokens.forEach((token) => tokens.add(token)),
        remove: (...nextTokens) => nextTokens.forEach((token) => tokens.delete(token)),
        contains: (token) => tokens.has(token),
    };
}

function makeTurn({ text = PROCESSING_LABEL, source = "user", steps = 1 } = {}) {
    return {
        finalized: false,
        source,
        steps,
        textEl: {
            textContent: text,
            innerHTML: text,
            classList: makeClassList("pending"),
        },
        flowEl: { hidden: false, open: true },
        metaEl: { textContent: "" },
        listEl: { innerHTML: "", querySelectorAll: () => [] },
        footerEl: { hidden: false },
        toggleEl: { textContent: "", setAttribute() {} },
        el: { remove() {} },
    };
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
        `${APP_CODE}\nthis.__testExports = { handleCeoControlAck, patchCeoInflightTurn, S };`,
        context
    );
    vm.runInContext(
        `
        renderCeoSessions = () => {};
        syncCeoPrimaryButton = () => {};
        syncCeoSessionActions = () => {};
        patchCeoSessionRuntimeState = () => false;
        patchCeoSessionSnapshotCache = () => ({});
        setCeoSessionSnapshotCache = () => ({});
        renderCeoToolEventsIntoTurn = (turn, toolEvents) => {
            const count = Array.isArray(toolEvents) ? toolEvents.length : 0;
            turn.steps = count;
            turn.flowEl.hidden = count === 0;
            turn.flowEl.open = true;
            return count;
        };
    `,
        context
    );
    context.__testExports.S.activeSessionId = "web:test";
    return context.__testExports;
}

test("manual pause ack replaces pending label with paused label and keeps tool flow", () => {
    const { handleCeoControlAck, S } = loadApp();
    const turn = makeTurn({ steps: 2 });

    S.ceoPendingTurns = [turn];
    S.ceoTurnActive = true;

    handleCeoControlAck({
        action: "pause",
        accepted: true,
        source: "user",
        manual_pause_waiting_reason: true,
    });

    assert.equal(turn.textEl.textContent, PAUSED_LABEL);
    assert.equal(turn.textEl.classList.contains("pending"), false);
    assert.equal(turn.flowEl.hidden, false);
    assert.equal(turn.flowEl.open, false);
    assert.equal(turn.finalized, true);
    assert.equal(S.ceoPendingTurns.length, 0);
});

test("paused inflight snapshot does not fall back to processing placeholder", () => {
    const { patchCeoInflightTurn, S } = loadApp();
    const turn = makeTurn({ text: "", steps: 0 });

    S.ceoPendingTurns = [turn];

    const patched = patchCeoInflightTurn({
        source: "user",
        status: "paused",
        assistant_text: "",
        tool_events: [{ tool_name: "skill-installer", source: "user" }],
    });

    assert.equal(patched, true);
    assert.equal(turn.textEl.textContent, PAUSED_LABEL);
    assert.notEqual(turn.textEl.textContent, PROCESSING_LABEL);
    assert.equal(turn.textEl.classList.contains("pending"), false);
    assert.equal(turn.flowEl.hidden, false);
});
