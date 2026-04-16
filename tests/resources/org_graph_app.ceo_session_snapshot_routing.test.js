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
    class StubWebSocket {
        static instances = [];

        constructor(url) {
            this.url = url;
            this.readyState = 1;
            this.sent = [];
            this.onmessage = null;
            this.onclose = null;
            StubWebSocket.instances.push(this);
        }

        send(payload) {
            this.sent.push(payload);
        }

        close() {
            this.readyState = 3;
            if (typeof this.onclose === "function") this.onclose();
        }
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
        WebSocket: StubWebSocket,
    };
    context.window = context;
    vm.createContext(context);
    vm.runInContext(
        `${APP_CODE}
        this.__testExports = {
            S,
            initCeoWs,
            getCeoSessionSnapshotCache,
            activeSessionId,
        };`,
        context
    );
    vm.runInContext(
        `
        globalThis.__renderCalls = [];
        globalThis.__renderFeedTexts = [];
        globalThis.__renderSessionPayloads = [];
        globalThis.__toolEvents = [];
        globalThis.__reminderEvents = [];
        globalThis.__patchCalls = [];
        renderCeoSnapshot = (messages, inflightTurn, options = {}) => {
            globalThis.__renderCalls.push({
                sessionId: String(options?.sessionId || ""),
                messageCount: Array.isArray(messages) ? messages.length : 0,
            });
            globalThis.__renderSessionPayloads.push({
                sessionId: String(options?.sessionId || ""),
                messages,
                inflightTurn,
                preservedTurn: options?.preservedTurn || null,
            });
        };
        renderCeoSessions = () => {};
        syncCeoSessionActions = () => {};
        syncCeoPrimaryButton = () => {};
        applyCeoState = () => {};
        handleCeoControlAck = () => {};
        patchCeoInflightTurn = (snapshot, options = {}) => {
            globalThis.__patchCalls.push({ snapshot, options });
        };
        appendCeoToolEvent = (event) => {
            globalThis.__toolEvents.push(event);
        };
        handleCeoToolReminder = (event) => {
            globalThis.__reminderEvents.push(event);
        };
        handleCeoError = () => {};
        finalizeCeoTurn = () => {};
        discardActiveCeoTurn = () => {};
        applyCeoSessionsPayload = () => {};
        applyCeoSessionPatch = () => {};
        ApiClient = {
            getCeoWsUrl: (sessionId) => 'ws://localhost/api/ws/ceo?session_id=' + encodeURIComponent(String(sessionId || '')),
            getErrorCode: () => '',
            friendlyErrorMessage: () => '',
            setActiveSessionId: () => {},
            getActiveSessionId: () => 'web:shared',
        };
        `,
        context
    );
    return {
        ...context.__testExports,
        __context: context,
        __socket() {
            return StubWebSocket.instances[StubWebSocket.instances.length - 1] || null;
        },
    };
}

test("snapshot.ceo caches messages under the payload session id", () => {
    const { S, initCeoWs, getCeoSessionSnapshotCache, __socket } = loadApp();

    S.activeSessionId = "";
    initCeoWs();

    const socket = __socket();
    assert.ok(socket);

    socket.onmessage({
        data: JSON.stringify({
            type: "snapshot.ceo",
            session_id: "web:ceo-624dd107a923",
            data: {
                messages: [{ role: "assistant", content: "persisted reply" }],
                inflight_turn: null,
            },
        }),
    });

    const targetEntry = getCeoSessionSnapshotCache("web:ceo-624dd107a923");
    const sharedEntry = getCeoSessionSnapshotCache("web:shared");

    assert.equal(targetEntry?.messages?.[0]?.content, "persisted reply");
    assert.equal(sharedEntry, null);
});

test("snapshot.ceo for a different session does not render into the active feed", () => {
    const { S, initCeoWs, __socket, __context } = loadApp();

    S.activeSessionId = "web:current";
    initCeoWs();

    const socket = __socket();
    assert.ok(socket);

    socket.onmessage({
        data: JSON.stringify({
            type: "snapshot.ceo",
            session_id: "web:other",
            data: {
                messages: [{ role: "assistant", content: "other session" }],
                inflight_turn: null,
            },
        }),
    });

    assert.equal(__context.__renderCalls.length, 0);
});

test("snapshot.ceo forwards preserved_turn separately from current inflight turn", () => {
    const { S, initCeoWs, __socket, __context, getCeoSessionSnapshotCache } = loadApp();

    S.activeSessionId = "web:current";
    initCeoWs();

    const socket = __socket();
    assert.ok(socket);

    socket.onmessage({
        data: JSON.stringify({
            type: "snapshot.ceo",
            session_id: "web:current",
            data: {
                messages: [],
                inflight_turn: {
                    source: "heartbeat",
                    turn_id: "turn-heartbeat-current",
                    status: "running",
                    assistant_text: "heartbeat processing",
                },
                preserved_turn: {
                    source: "user",
                    turn_id: "turn-user-preserved",
                    status: "running",
                    user_message: { content: "Install skill" },
                },
            },
        }),
    });

    assert.equal(__context.__renderSessionPayloads.length, 1);
    assert.equal(__context.__renderSessionPayloads[0].inflightTurn.turn_id, "turn-heartbeat-current");
    assert.equal(__context.__renderSessionPayloads[0].preservedTurn.turn_id, "turn-user-preserved");
    const entry = getCeoSessionSnapshotCache("web:current");
    assert.equal(entry?.inflight_turn?.turn_id, "turn-heartbeat-current");
    assert.equal(entry?.preserved_turn?.turn_id, "turn-user-preserved");
});

test("ceo.turn.patch forwards preserved_turn and current inflight turn as separate patch calls", () => {
    const { S, initCeoWs, __socket, __context } = loadApp();

    S.activeSessionId = "web:current";
    initCeoWs();

    const socket = __socket();
    assert.ok(socket);

    socket.onmessage({
        data: JSON.stringify({
            type: "ceo.turn.patch",
            session_id: "web:current",
            data: {
                inflight_turn: {
                    source: "heartbeat",
                    turn_id: "turn-heartbeat-current",
                    status: "running",
                    assistant_text: "heartbeat processing",
                },
                preserved_turn: {
                    source: "user",
                    turn_id: "turn-user-preserved",
                    status: "running",
                    user_message: { content: "Install skill" },
                },
            },
        }),
    });

    assert.equal(__context.__patchCalls.length, 2);
    assert.equal(__context.__patchCalls[0].snapshot.turn_id, "turn-user-preserved");
    assert.equal(__context.__patchCalls[0].options.cacheField, "preserved_turn");
    assert.equal(__context.__patchCalls[1].snapshot.turn_id, "turn-heartbeat-current");
    assert.equal(__context.__patchCalls[1].options.cacheField, "inflight_turn");
});

test("ceo.agent.tool forwards live tool events into the active session feed", () => {
    const { S, initCeoWs, __socket, __context } = loadApp();

    S.activeSessionId = "web:current";
    initCeoWs();

    const socket = __socket();
    assert.ok(socket);

    socket.onmessage({
        data: JSON.stringify({
            type: "ceo.agent.tool",
            session_id: "web:current",
            data: {
                tool_name: "load_tool_context",
                status: "success",
                text: '{"tool_id":"filesystem_write"}',
                source: "user",
            },
        }),
    });

    assert.equal(__context.__toolEvents.length, 1);
    assert.equal(__context.__toolEvents[0].tool_name, "load_tool_context");
    assert.equal(__context.__toolEvents[0].status, "success");
});

test("ceo.tool.reminder forwards live reminder events into the active session feed", () => {
    const { S, initCeoWs, __socket, __context } = loadApp();

    S.activeSessionId = "web:current";
    initCeoWs();

    const socket = __socket();
    assert.ok(socket);

    socket.onmessage({
        data: JSON.stringify({
            type: "ceo.tool.reminder",
            session_id: "web:current",
            data: {
                turn_id: "turn-user-1",
                execution_id: "inline-tool-exec:1",
                tool_name: "exec",
                elapsed_seconds: 120,
                reminder_count: 2,
                decision: "stop",
                label: "stopped by reminder",
            },
        }),
    });

    assert.equal(__context.__reminderEvents.length, 1);
    assert.equal(__context.__reminderEvents[0].execution_id, "inline-tool-exec:1");
    assert.equal(__context.__reminderEvents[0].decision, "stop");
});
