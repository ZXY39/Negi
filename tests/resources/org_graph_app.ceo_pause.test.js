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
        return {
            className: "",
            innerHTML: "",
            hidden: false,
            querySelector(selector) {
                if (selector === ".assistant-text") {
                    return {
                        textContent: "",
                        innerHTML: "",
                        classList: makeClassList("pending"),
                        setAttribute() {},
                        removeAttribute() {},
                    };
                }
                if (selector === ".interaction-flow") return { hidden: true, open: false };
                if (selector === ".interaction-flow-meta") return { textContent: "" };
                if (selector === ".interaction-flow-list") return { innerHTML: "", querySelectorAll: () => [] };
                if (selector === ".interaction-flow-footer") return { hidden: true };
                if (selector === ".interaction-flow-toggle") return { textContent: "", setAttribute() {}, addEventListener() {} };
                return null;
            },
            remove() {},
        };
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
        turnId: "",
        steps,
        renderMode: "",
        lastExecutionTraceSummary: null,
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
        `${APP_CODE}\nthis.__testExports = { handleCeoControlAck, patchCeoInflightTurn, finalizeCeoTurn, finalizePausedCeoTurn, renderPersistedCeoAssistantTurn, renderCeoSnapshot, dedupeInflightUserMessageAgainstMessages, applyCeoState, maybeDispatchQueuedCeoFollowUps, setCeoQueuedFollowUps, getCeoQueuedFollowUps, S, U, WebSocket, setAddMsg(fn) { addMsg = fn; }, setCreatePendingCeoTurn(fn) { createPendingCeoTurn = fn; }, getPatchSnapshotCalls: () => globalThis.__patchSnapshotCalls || 0 };`,
        context
    );
    vm.runInContext(
        `
        globalThis.__patchSnapshotCalls = 0;
        renderCeoSessions = () => {};
        syncCeoPrimaryButton = () => {};
        syncCeoSessionActions = () => {};
        patchCeoSessionRuntimeState = () => false;
        maybeDispatchQueuedCeoFollowUps = () => false;
        patchCeoSessionSnapshotCache = () => {
            globalThis.__patchSnapshotCalls += 1;
            return {};
        };
        setCeoSessionSnapshotCache = () => ({});
        normalizeExecutionStageTrace = (stage, index) => ({
            stage_id: String(stage?.stage_id || ""),
            stage_index: index + 1,
            stage_goal: String(stage?.stage_goal || ""),
            status: String(stage?.status || "running"),
            rounds: Array.isArray(stage?.rounds) ? stage.rounds : [],
        });
        renderCeoStageTraceIntoTurn = (turn, summary) => {
            const hasStages = Array.isArray(summary?.stages) && summary.stages.length > 0;
            if (!hasStages) return 0;
            turn.renderMode = "stage";
            turn.lastExecutionTraceSummary = summary;
            turn.steps = summary.stages.length;
            turn.flowEl.hidden = false;
            turn.flowEl.open = true;
            turn.listEl.innerHTML = "stage-trace";
            return turn.steps;
        };
        renderCeoToolEventsIntoTurn = (turn, toolEvents) => {
            const count = Array.isArray(toolEvents) ? toolEvents.length : 0;
            turn.renderMode = count > 0 ? "tool" : turn.renderMode;
            turn.steps = count;
            turn.flowEl.hidden = count === 0;
            turn.flowEl.open = true;
            turn.listEl.innerHTML = count > 0 ? "tool-events" : turn.listEl.innerHTML;
            return count;
        };
    `,
        context
    );
    context.__testExports.U.ceoFeed = {
        scrollTop: 0,
        scrollHeight: 0,
        clientHeight: 0,
        appended: [],
        appendChild(element) {
            this.appended.push(element);
            return element;
        },
    };
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
    });

    assert.equal(turn.textEl.textContent, PAUSED_LABEL);
    assert.equal(turn.textEl.classList.contains("pending"), false);
    assert.equal(turn.flowEl.hidden, false);
    assert.equal(turn.flowEl.open, false);
    assert.equal(turn.finalized, true);
    assert.equal(S.ceoPendingTurns.length, 0);
});

test("manual pause ack ignores legacy waiting-reason flag", () => {
    const baseline = loadApp();
    const baselineTurn = makeTurn({ steps: 1 });
    baseline.S.ceoPendingTurns = [baselineTurn];
    baseline.S.ceoTurnActive = true;
    baseline.handleCeoControlAck({
        action: "pause",
        accepted: true,
        source: "user",
    });

    const legacy = loadApp();
    const legacyTurn = makeTurn({ steps: 1 });
    legacy.S.ceoPendingTurns = [legacyTurn];
    legacy.S.ceoTurnActive = true;
    legacy.handleCeoControlAck({
        action: "pause",
        accepted: true,
        source: "user",
        manual_pause_waiting_reason: true,
    });

    assert.equal(legacyTurn.textEl.textContent, PAUSED_LABEL);
    assert.equal(legacyTurn.finalized, true);
    assert.equal(legacy.S.ceoPendingTurns.length, 0);
    assert.equal(legacy.getPatchSnapshotCalls(), baseline.getPatchSnapshotCalls());
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

test("persisted paused assistant history renders as a paused bubble", () => {
    const context = loadApp();
    const { renderPersistedCeoAssistantTurn, setCreatePendingCeoTurn, S } = context;
    const turn = makeTurn({ text: "", source: "history", steps: 0 });

    setCreatePendingCeoTurn(() => turn);
    S.ceoPendingTurns = [];

    renderPersistedCeoAssistantTurn({
        role: "assistant",
        content: "",
        status: "paused",
        execution_trace_summary: {
            stages: [{ stage_id: "frontdoor-stage-1", stage_goal: "inspect repo", rounds: [] }],
        },
    });

    assert.equal(turn.textEl.textContent, PAUSED_LABEL);
    assert.equal(turn.finalized, true);
    assert.equal(turn.renderMode, "stage");
    assert.equal(turn.flowEl.hidden, false);
    assert.equal(turn.flowEl.open, false);
    assert.equal(S.ceoPendingTurns.length, 0);
});

test("deduped running inflight snapshot is preserved so the assistant placeholder can render", () => {
    const { dedupeInflightUserMessageAgainstMessages } = loadApp();

    const deduped = dedupeInflightUserMessageAgainstMessages(
        [{ role: "user", content: "继续完成Claude Code Haha那个任务" }],
        {
            status: "running",
            user_message: { content: "继续完成Claude Code Haha那个任务" },
        }
    );

    assert.equal(deduped?.status, "running");
    assert.equal("user_message" in (deduped || {}), false);
});

test("discard and final match the target pending turn by turn_id before source", () => {
    const context = loadApp();
    const { S, patchCeoInflightTurn, handleCeoControlAck, finalizeCeoTurn } = context;
    const older = makeTurn({ text: PROCESSING_LABEL, source: "user", steps: 1 });
    older.turnId = "turn-old";
    const newer = makeTurn({ text: PROCESSING_LABEL, source: "user", steps: 1 });
    newer.turnId = "turn-new";

    S.ceoPendingTurns = [older, newer];
    S.ceoTurnActive = true;

    patchCeoInflightTurn({
        turn_id: "turn-old",
        source: "user",
        status: "running",
        assistant_text: "Older turn is still active",
        tool_events: [],
    });

    assert.match(older.textEl.innerHTML, /Older turn is still active/);
    assert.equal(newer.textEl.innerHTML, PROCESSING_LABEL);

    finalizeCeoTurn("done", { source: "user", turn_id: "turn-old" });

    assert.equal(older.finalized, true);
    assert.equal(newer.finalized, false);
    assert.equal(S.ceoPendingTurns.length, 1);

    handleCeoControlAck({
        action: "pause",
        accepted: true,
        source: "user",
        turn_id: "turn-new",
    });

    assert.equal(newer.textEl.textContent, PAUSED_LABEL);
    assert.equal(newer.finalized, true);
    assert.equal(S.ceoPendingTurns.length, 0);
});

test("finalize uses final execution trace summary instead of stale inflight trace", () => {
    const context = loadApp();
    const { S, patchCeoInflightTurn, finalizeCeoTurn } = context;
    const turn = makeTurn({ text: PROCESSING_LABEL, source: "user", steps: 1 });
    turn.turnId = "turn-final";

    S.ceoPendingTurns = [turn];
    S.ceoTurnActive = true;

    patchCeoInflightTurn({
        turn_id: "turn-final",
        source: "user",
        status: "running",
        execution_trace_summary: {
            stages: [
                {
                    stage_id: "frontdoor-stage-4",
                    stage_goal: "write file",
                    rounds: [],
                },
            ],
        },
    });

    finalizeCeoTurn("done", {
        source: "user",
        turn_id: "turn-final",
        execution_trace_summary: {
            stages: [
                {
                    stage_id: "frontdoor-stage-4",
                    stage_goal: "write file",
                    rounds: [
                        {
                            round_id: "frontdoor-stage-4:round-1",
                            tools: [{ tool_name: "filesystem_write", status: "success" }],
                        },
                    ],
                },
            ],
        },
    });

    assert.equal(turn.finalized, true);
    assert.equal(turn.renderMode, "stage");
    assert.equal(turn.lastExecutionTraceSummary.stages[0].rounds.length, 1);
    assert.equal(turn.lastExecutionTraceSummary.stages[0].rounds[0].tools[0].tool_name, "filesystem_write");
});

test("running state without source does not create a phantom pending turn", () => {
    const { applyCeoState, S } = loadApp();

    S.ceoPendingTurns = [];
    S.ceoTurnActive = true;

    applyCeoState({ status: "running", is_running: true }, {});

    assert.equal(S.ceoPendingTurns.length, 0);
});

test("stage trace stays visible when a later patch only carries fallback tool events", () => {
    const { patchCeoInflightTurn, S } = loadApp();
    const turn = makeTurn({ text: "", steps: 0 });

    S.ceoPendingTurns = [turn];

    patchCeoInflightTurn({
        source: "user",
        status: "running",
        execution_trace_summary: {
            stages: [{ stage_id: "frontdoor-stage-1", stage_goal: "inspect repo" }],
        },
    });

    assert.equal(turn.renderMode, "stage");
    assert.equal(turn.listEl.innerHTML, "stage-trace");

    patchCeoInflightTurn({
        source: "user",
        status: "running",
        assistant_text: "still working",
        tool_events: [{ tool_name: "command_execution", source: "user" }],
    });

    assert.equal(turn.renderMode, "stage");
    assert.equal(turn.listEl.innerHTML, "stage-trace");
});

test("render snapshot keeps preserved user flow separate from current heartbeat bubble", () => {
    const { renderCeoSnapshot, S } = loadApp();

    renderCeoSnapshot(
        [],
        {
            source: "heartbeat",
            turn_id: "turn-heartbeat-current",
            status: "running",
            assistant_text: "heartbeat processing",
        },
        {
            sessionId: "web:test",
            preservedTurn: {
                source: "user",
                turn_id: "turn-user-preserved",
                status: "running",
                user_message: { content: "Install the skill" },
                assistant_text: "Still working on it...",
                execution_trace_summary: {
                    stages: [{ stage_id: "frontdoor-stage-user", stage_goal: "install skill" }],
                },
            },
        },
    );

    assert.equal(S.ceoPendingTurns.length, 2);
    const userTurn = S.ceoPendingTurns.find((turn) => turn.turnId === "turn-user-preserved");
    const heartbeatTurn = S.ceoPendingTurns.find((turn) => turn.turnId === "turn-heartbeat-current");

    assert.equal(userTurn?.renderMode, "stage");
    assert.equal(userTurn?.listEl?.innerHTML, "stage-trace");
    assert.equal(heartbeatTurn?.renderMode || "", "");
    assert.equal(heartbeatTurn?.lastExecutionTraceSummary || null, null);
    assert.equal(heartbeatTurn?.flowEl?.hidden, true);
});

test("queued follow-ups drain as one batch request and render multiple user bubbles", () => {
    const context = loadApp();
    const { maybeDispatchQueuedCeoFollowUps, setCeoQueuedFollowUps, getCeoQueuedFollowUps, S } = context;
    const sent = [];
    const userBubbles = [];
    let pendingTurnCount = 0;

    context.WebSocket.OPEN = 1;
    S.ceoWs = {
        readyState: 1,
        send(payload) {
            sent.push(JSON.parse(payload));
        },
    };
    context.activeSessionId = () => "web:test";
    context.setAddMsg((text, role) => {
        userBubbles.push({ text, role });
    });
    context.setCreatePendingCeoTurn(() => {
        pendingTurnCount += 1;
        return makeTurn({ source: "user", steps: 0 });
    });

    setCeoQueuedFollowUps("web:test", [
        { id: "follow-1", text: "先补齐 skill 元数据", uploads: [] },
        { id: "follow-2", text: "再检查 filesystem_write 是否可用", uploads: [] },
    ]);

    const dispatched = maybeDispatchQueuedCeoFollowUps();

    assert.equal(dispatched, true);
    assert.equal(sent.length, 1);
    assert.deepEqual(sent[0], {
        type: "client.user_message",
        session_id: "web:test",
        messages: [
            { text: "先补齐 skill 元数据", uploads: [] },
            { text: "再检查 filesystem_write 是否可用", uploads: [] },
        ],
    });
    assert.deepEqual(userBubbles, [
        { text: "先补齐 skill 元数据", role: "user" },
        { text: "再检查 filesystem_write 是否可用", role: "user" },
    ]);
    assert.equal(pendingTurnCount, 1);
    assert.equal(getCeoQueuedFollowUps("web:test").length, 0);
});
