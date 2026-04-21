const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const TASK_VIEW_PATH = "g3ku/web/frontend/org_graph_task_view.js";
const APP_PATH = "g3ku/web/frontend/org_graph_app.js";
const TASK_VIEW_CODE = fs.readFileSync(TASK_VIEW_PATH, "utf8");
const APP_CODE = fs.readFileSync(APP_PATH, "utf8");
const COMPRESSION_TEXT = "\u4e0a\u4e0b\u6587\u538b\u7f29\u4e2d";

class StubElement {}
function createStubChildrenCollection(owner) {
    return new Proxy({}, {
        get(_target, prop) {
            if (prop === "length") return owner._children.length;
            if (prop === "item") return (index) => owner._children[index] || null;
            if (prop === Symbol.iterator) return owner._children[Symbol.iterator].bind(owner._children);
            if (typeof prop === "string" && /^\d+$/.test(prop)) return owner._children[Number(prop)] || undefined;
            return undefined;
        },
    });
}

class StubHTMLElement extends StubElement {
    constructor() {
        super();
        this.hidden = false;
        this.textContent = "";
        this._innerHTML = "";
        this.className = "";
        this.dataset = {};
        this.style = {};
        this.attributes = {};
        this._selectors = {};
        this._selectorLists = {};
        this._children = [];
        this._childrenCollection = createStubChildrenCollection(this);
        this.parentElement = null;
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
                const hasToken = String(this.className || "").split(/\s+/).includes(token);
                const shouldAdd = force == null ? !hasToken : !!force;
                if (shouldAdd) this.classList.add(token);
                else this.classList.remove(token);
                return shouldAdd;
            },
        };
    }

    querySelector(selector) {
        if (selector === ".interaction-step") {
            return this._children.find((child) => child.classList.contains("interaction-step")) || null;
        }
        return this._selectors[selector] || null;
    }

    querySelectorAll(selector) {
        if (selector === ".interaction-step") {
            return this._children.filter((child) => child.classList.contains("interaction-step"));
        }
        return this._selectorLists[selector] || [];
    }

    addEventListener() {}

    appendChild(child) {
        child.parentElement = this;
        this._children.push(child);
        return child;
    }

    remove() {
        if (!this.parentElement || !Array.isArray(this.parentElement._children)) return;
        this.parentElement._children = this.parentElement._children.filter((child) => child !== this);
        this.parentElement = null;
    }

    get children() {
        return this._childrenCollection;
    }

    setAttribute(name, value) {
        this.attributes[name] = String(value);
    }

    removeAttribute(name) {
        delete this.attributes[name];
    }

    get innerHTML() {
        return this._innerHTML;
    }

    set innerHTML(value) {
        this._innerHTML = String(value);
        if (!this._innerHTML.includes("interaction-step-header")) return;
        this._selectors[".interaction-step-title"] = new StubHTMLElement();
        this._selectors[".interaction-step-started"] = new StubHTMLElement();
        this._selectors[".interaction-step-status"] = new StubHTMLElement();
        this._selectors[".interaction-step-icon"] = new StubHTMLElement();
        this._selectors[".interaction-step-preview"] = new StubHTMLElement();
        this._selectors[".interaction-step-detail"] = new StubHTMLElement();
        this._selectors[".interaction-step-disclosure"] = new StubHTMLButtonElement();
    }
}
class StubHTMLButtonElement extends StubHTMLElement {}
class StubHTMLInputElement extends StubHTMLElement {}
class StubHTMLTextAreaElement extends StubHTMLElement {}
class StubHTMLSelectElement extends StubHTMLElement {}

class StubDocument {
    getElementById() {
        return null;
    }

    createElement() {
        return new StubHTMLElement();
    }

    querySelector() {
        return null;
    }

    querySelectorAll() {
        return [];
    }

    addEventListener() {}
}

function makeTurn({ text = "" } = {}) {
    return {
        textEl: { textContent: text, innerHTML: text, classList: { add() {}, remove() {} } },
        flowEl: { hidden: true, open: false },
        metaEl: { textContent: "" },
        listEl: new StubHTMLElement(),
        footerEl: { hidden: true },
        toggleEl: { textContent: "", setAttribute() {} },
    };
}

function noticeText(item) {
    if (!item || !item.children?.length) return "";
    const textChild = Array.from(item.children).find((child) => (
        String(child?.className || "") === "ceo-context-load-notice-text"
    ));
    return String(textChild?.textContent || item.children[0]?.textContent || "");
}

function noticeRiskClass(item) {
    const classes = String(item?.className || "");
    const match = classes.match(/risk-(low|medium|high)/);
    return match ? match[0] : "";
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
        `${TASK_VIEW_CODE}\n${APP_CODE}\nthis.__testExports = { renderCeoStageTraceIntoTurn, renderCeoToolEventsIntoTurn, applyCeoToolEventToTurn, patchCeoInflightTurn, normalizeCeoSnapshotToolEvents, syncCeoCompressionToast, stageTraceStatus, displayTaskStageStatus, toggleCeoToolStepOutput, S, U };`,
        context
    );
    context.__testExports.U.ceoCompressionToast = new StubHTMLElement();
    context.__testExports.U.ceoCompressionToastText = new StubHTMLElement();
    context.__testExports.__context = context;
    return context.__testExports;
}

test("ceo inflight snapshot renders stage -> round -> tool structure", () => {
    const { renderCeoStageTraceIntoTurn } = loadApp();
    const turn = makeTurn({ text: "" });

    renderCeoStageTraceIntoTurn(turn, {
        stages: [
            {
                stage_id: "stage-1",
                stage_goal: "inspect repository",
                status: "running",
                tool_round_budget: 6,
                rounds: [
                    {
                        round_id: "round-1",
                        round_index: 1,
                        created_at: "2026-04-11T23:13:12+08:00",
                        tools: [
                            {
                                tool_name: "filesystem",
                                arguments_text: "{\"path\": \".\"}",
                                output_text: "ok",
                                status: "success",
                            },
                        ],
                    },
                ],
            },
        ],
    });

    assert.match(turn.listEl.innerHTML, /inspect repository/);
    assert.match(turn.listEl.innerHTML, /filesystem/);
    assert.doesNotMatch(turn.listEl.innerHTML, /第 1 轮/);
    assert.doesNotMatch(turn.listEl.innerHTML, /工具 ·/);
    assert.doesNotMatch(turn.listEl.innerHTML, /自主执行/);
    assert.doesNotMatch(turn.listEl.innerHTML, /点击上方工具卡片查看该调用的参数和输出/);
});

test("ceo stage trace with no rounds still signals caller to keep stage view", () => {
    const { renderCeoStageTraceIntoTurn } = loadApp();
    const turn = makeTurn({ text: "" });

    const rendered = renderCeoStageTraceIntoTurn(turn, {
        stages: [
            {
                stage_id: "stage-no-rounds",
                stage_goal: "inspect repository",
                status: "running",
                tool_round_budget: 7,
                rounds: [],
            },
        ],
    });

    assert.equal(rendered > 0, true);
    assert.match(turn.listEl.innerHTML, /inspect repository/);
});

test("ceo stage trace with no stages keeps a readable waiting meta", () => {
    const { renderCeoStageTraceIntoTurn } = loadApp();
    const turn = makeTurn({ text: "" });

    const rendered = renderCeoStageTraceIntoTurn(turn, { stages: [] });

    assert.equal(rendered, 0);
    assert.equal(turn.metaEl.textContent, "等待工具开始...");
});

test("ceo stage trace renders real stage goal and budget from true frontdoor stage data", () => {
    const { renderCeoStageTraceIntoTurn } = loadApp();
    const turn = makeTurn({ text: "" });

    const renderedSteps = renderCeoStageTraceIntoTurn(turn, {
        stages: [
            {
                stage_id: "inflight-stage-1",
                stage_goal: "synthetic carryover",
                status: "running",
                tool_round_budget: 5,
                system_generated: true,
                stage_kind: "normal",
                rounds: [
                    {
                        round_id: "round-synthetic-1",
                        round_index: 1,
                        tools: [
                            { tool_name: "load_tool_context", status: "success", output_text: "ok" },
                            { tool_name: "memory_note", status: "success", output_text: "ok" },
                            { tool_name: "submit_next_stage", status: "success", output_text: "stage advanced" },
                        ],
                    },
                ],
            },
            {
                stage_id: "frontdoor-stage-1",
                stage_goal: "\u67e5\u770b\u5f53\u524d\u53ef\u68c0\u7d22\u7684\u957f\u671f\u8bb0\u5fc6\uff0c\u5e76\u5411\u7528\u6237\u6309\u7c7b\u522b\u6e05\u6670\u6c47\u603b\u6211\u5df2\u8bb0\u4f4f\u7684\u5185\u5bb9\u3002",
                status: "running",
                tool_round_budget: 7,
                tool_rounds_used: 1,
                rounds: [
                    {
                        round_id: "round-1",
                        round_index: 1,
                        budget_counted: false,
                        tools: [{ tool_name: "load_tool_context", status: "success", output_text: "loaded context" }],
                    },
                    {
                        round_id: "round-2",
                        round_index: 2,
                        budget_counted: true,
                        tools: [{ tool_name: "memory_note", status: "running", output_text: "" }],
                    },
                ],
            },
        ],
    });

    assert.match(turn.listEl.innerHTML, /\u67e5\u770b\u5f53\u524d\u53ef\u68c0\u7d22\u7684\u957f\u671f\u8bb0\u5fc6/);
    assert.match(turn.listEl.innerHTML, /1\/7/);
    assert.doesNotMatch(turn.listEl.innerHTML, /\u6700\u5927\u8f6e\u6570/);
    assert.doesNotMatch(turn.listEl.innerHTML, /loaded context/);
    assert.match(turn.listEl.innerHTML, /memory_note/);
    assert.equal(renderedSteps, 1);
    assert.match(turn.metaEl.textContent, /1/);
    assert.doesNotMatch(turn.listEl.innerHTML, /ceo:stage:inflight-stage-1/);
    assert.doesNotMatch(turn.listEl.innerHTML, /synthetic carryover/);
    assert.doesNotMatch(turn.listEl.innerHTML, /submit_next_stage/);
});

test("ceo stage trace does not count successful loader-only rounds toward displayed budget progress", () => {
    const { renderCeoStageTraceIntoTurn, U, S } = loadApp();
    const turn = makeTurn({ text: "" });

    U.ceoContextLoadNotice = new StubHTMLElement();
    U.ceoContextLoadNotice.hidden = true;
    S.skills = [{ skill_id: "skill-creator", risk_level: "high" }];

    const renderedSteps = renderCeoStageTraceIntoTurn(turn, {
        stages: [
            {
                stage_id: "frontdoor-stage-loader-only",
                stage_goal: "read skill context",
                status: "running",
                tool_round_budget: 5,
                tool_rounds_used: 0,
                rounds: [
                    {
                        round_id: "round-loader-only",
                        round_index: 1,
                        budget_counted: false,
                        tools: [
                            {
                                tool_name: "load_skill_context",
                                status: "success",
                                arguments_text: 'load_skill_context (skill_id=skill-creator)',
                                output_text: '{"skill_id":"skill-creator"}',
                            },
                        ],
                    },
                ],
            },
        ],
    });

    assert.equal(renderedSteps, 1);
    assert.match(turn.listEl.innerHTML, /0\/5/);
    assert.doesNotMatch(turn.listEl.innerHTML, /load_skill_context/);
    assert.equal(U.ceoContextLoadNotice.children.length, 0);
    assert.equal(U.ceoContextLoadNotice.hidden, true);
});

test("ceo legacy tool flow hides submit_next_stage events until stage trace arrives", () => {
    const { normalizeCeoSnapshotToolEvents } = loadApp();

    const events = normalizeCeoSnapshotToolEvents([
        {
            tool_name: "submit_next_stage",
            status: "success",
            text: "stage advanced",
            tool_call_id: "submit_next_stage:1",
            source: "user",
        },
    ]);

    assert.deepEqual(events, []);
});

test("ceo loader tool events stack tool and skill notices in separate columns for ten seconds", () => {
    const { applyCeoToolEventToTurn, U, __context, S } = loadApp();
    const turn = makeTurn({ text: "" });
    const scheduled = [];

    S.tools = [{ tool_id: "filesystem_write", actions: [{ risk_level: "high" }, { risk_level: "low" }] }];
    S.skills = [{ skill_id: "find-skills", risk_level: "low" }];

    __context.setTimeout = (callback, delay) => {
        scheduled.push({ callback, delay });
        return scheduled.length;
    };
    __context.clearTimeout = () => {};
    U.ceoContextLoadNotice = new StubHTMLElement();
    U.ceoContextLoadNotice.hidden = true;

    const firstRendered = applyCeoToolEventToTurn(turn, {
        tool_name: "load_tool_context",
        status: "success",
        text: '{"tool_id":"filesystem_write"}',
        tool_call_id: "load-tool:1",
        source: "user",
    });
    const secondRendered = applyCeoToolEventToTurn(turn, {
        tool_name: "load_skill_context",
        status: "success",
        text: '{"skill_id":"find-skills"}',
        tool_call_id: "load-skill:2",
        source: "user",
    });

    assert.equal(firstRendered, null);
    assert.equal(secondRendered, null);
    assert.equal(turn.listEl.children.length, 0);
    assert.equal(U.ceoContextLoadNotice.hidden, false);
    assert.equal(U.ceoContextLoadNotice.children.length, 2);
    assert.match(String(U.ceoContextLoadNotice.children[0].className || ""), /is-tool/);
    assert.match(String(U.ceoContextLoadNotice.children[1].className || ""), /is-skill/);
    assert.equal(noticeRiskClass(U.ceoContextLoadNotice.children[0]), "risk-high");
    assert.equal(noticeRiskClass(U.ceoContextLoadNotice.children[1]), "risk-low");
    assert.match(noticeText(U.ceoContextLoadNotice.children[0]), /filesystem_write/);
    assert.match(noticeText(U.ceoContextLoadNotice.children[1]), /find-skills/);
    assert.doesNotMatch(noticeText(U.ceoContextLoadNotice.children[0]), /\[/);
    assert.doesNotMatch(noticeText(U.ceoContextLoadNotice.children[1]), /\[/);
    assert.deepEqual(scheduled.map((item) => item.delay), [10000, 10000]);

    scheduled[0].callback();

    assert.equal(U.ceoContextLoadNotice.children.length, 1);
    assert.equal(U.ceoContextLoadNotice.hidden, false);

    scheduled[1].callback();

    assert.equal(U.ceoContextLoadNotice.children.length, 0);
    assert.equal(U.ceoContextLoadNotice.hidden, true);
});

test("ceo stage trace hides successful loader tools from interaction flow rendering", () => {
    const { renderCeoStageTraceIntoTurn, U, S } = loadApp();
    const turn = makeTurn({ text: "" });

    U.ceoContextLoadNotice = new StubHTMLElement();
    U.ceoContextLoadNotice.hidden = true;
    S.tools = [{ tool_id: "filesystem_write", actions: [{ risk_level: "medium" }] }];

    const renderedSteps = renderCeoStageTraceIntoTurn(turn, {
        stages: [
            {
                stage_id: "frontdoor-stage-1",
                stage_goal: "inspect repository",
                status: "running",
                tool_round_budget: 3,
                rounds: [
                    {
                        round_id: "round-1",
                        round_index: 1,
                        tools: [
                            {
                                tool_name: "load_tool_context",
                                status: "success",
                                arguments_text: 'load_tool_context (tool_id=filesystem_write)',
                                output_text: '{"tool_id":"filesystem_write"}',
                            },
                            {
                                tool_name: "memory_note",
                                status: "success",
                                output_text: "ok",
                            },
                        ],
                    },
                ],
            },
        ],
    });

    assert.equal(renderedSteps, 1);
    assert.doesNotMatch(turn.listEl.innerHTML, /load_tool_context/);
    assert.match(turn.listEl.innerHTML, /memory_note/);
    assert.equal(U.ceoContextLoadNotice.children.length, 0);
    assert.equal(U.ceoContextLoadNotice.hidden, true);
});

test("ceo loader tool live events still show notices when serialized output_text carries the loader payload", () => {
    const { applyCeoToolEventToTurn, U, __context, S } = loadApp();
    const turn = makeTurn({ text: "" });
    const scheduled = [];

    S.tools = [{ tool_id: "filesystem_write", actions: [{ risk_level: "high" }] }];

    __context.setTimeout = (callback, delay) => {
        scheduled.push({ callback, delay });
        return scheduled.length;
    };
    __context.clearTimeout = () => {};
    U.ceoContextLoadNotice = new StubHTMLElement();
    U.ceoContextLoadNotice.hidden = true;

    const rendered = applyCeoToolEventToTurn(turn, {
        tool_name: "load_tool_context",
        status: "success",
        text: "",
        output_text: '{"tool_id":"filesystem_write"}',
        tool_call_id: "load-tool:output-text",
        source: "user",
    });

    assert.equal(rendered, null);
    assert.equal(turn.listEl.children.length, 0);
    assert.equal(U.ceoContextLoadNotice.hidden, false);
    assert.equal(U.ceoContextLoadNotice.children.length, 1);
    assert.match(noticeText(U.ceoContextLoadNotice.children[0]), /filesystem_write/);
    assert.deepEqual(scheduled.map((item) => item.delay), [10000]);
});

test("ceo stage trace keeps interaction flow collapsed by default", () => {
    const { renderCeoStageTraceIntoTurn } = loadApp();
    const turn = makeTurn({ text: "" });

    const renderedSteps = renderCeoStageTraceIntoTurn(turn, {
        stages: [
            {
                stage_id: "frontdoor-stage-1",
                stage_goal: "inspect repository",
                status: "running",
                tool_round_budget: 3,
                rounds: [
                    {
                        round_id: "round-1",
                        round_index: 1,
                        tools: [
                            {
                                tool_name: "memory_note",
                                status: "success",
                                output_text: "ok",
                            },
                        ],
                    },
                ],
            },
        ],
    });

    assert.equal(renderedSteps, 1);
    assert.equal(turn.flowEl.hidden, false);
    assert.equal(turn.flowEl.open, false);
});

test("ceo live tool events do not auto-expand interaction flow", () => {
    const { applyCeoToolEventToTurn } = loadApp();
    const turn = makeTurn({ text: "" });

    const item = applyCeoToolEventToTurn(turn, {
        tool_name: "memory_note",
        status: "running",
        kind: "tool_start",
        text: '{"query": "repo"}',
        tool_call_id: "memory-note:1",
        source: "user",
    });

    assert.ok(item);
    assert.equal(turn.flowEl.hidden, false);
    assert.equal(turn.flowEl.open, false);
});

test("ceo snapshot patch preserves a manually expanded interaction flow", () => {
    const { patchCeoInflightTurn, S } = loadApp();
    const turn = makeTurn({ text: "" });
    turn.source = "user";
    turn.turnId = "turn-stage-1";
    turn.flowEl.hidden = false;
    turn.flowEl.open = true;
    S.activeSessionId = "web:test";
    S.ceoPendingTurns = [turn];

    const patched = patchCeoInflightTurn({
        turn_id: "turn-stage-1",
        source: "user",
        status: "running",
        canonical_context: {
            stages: [
                {
                    stage_id: "frontdoor-stage-1",
                    stage_goal: "inspect repository",
                    status: "running",
                    tool_round_budget: 3,
                    rounds: [
                        {
                            round_id: "round-1",
                            round_index: 1,
                            tools: [
                                {
                                    tool_name: "memory_note",
                                    status: "success",
                                    output_text: "ok",
                                },
                            ],
                        },
                    ],
                },
            ],
        },
    });

    assert.equal(patched, true);
    assert.equal(turn.flowEl.hidden, false);
    assert.equal(turn.flowEl.open, true);
});

test("ceo snapshot tool normalization preserves distinct tool_call_id values for same-name events", () => {
    const { normalizeCeoSnapshotToolEvents } = loadApp();

    const events = normalizeCeoSnapshotToolEvents([
        {
            tool_name: "filesystem",
            status: "running",
            text: "alpha",
            tool_call_id: "filesystem:1",
            source: "user",
        },
        {
            tool_name: "filesystem",
            status: "running",
            text: "beta",
            tool_call_id: "filesystem:2",
            source: "user",
        },
    ]);

    assert.deepEqual(events.map((item) => item.tool_call_id), ["filesystem:1", "filesystem:2"]);
});

test("ceo tool rows stay distinct for same-name events with different tool_call_id values", () => {
    const { renderCeoToolEventsIntoTurn } = loadApp();
    const turn = makeTurn({ text: "" });

    const rendered = renderCeoToolEventsIntoTurn(turn, [
        {
            tool_name: "filesystem",
            status: "running",
            text: "{\"path\": \"alpha\"}",
            tool_call_id: "filesystem:1",
            source: "user",
        },
        {
            tool_name: "filesystem",
            status: "running",
            text: "{\"path\": \"beta\"}",
            tool_call_id: "filesystem:2",
            source: "user",
        },
    ], { source: "user" });

    assert.equal(rendered, 2);
    assert.equal(turn.listEl.children.length, 2);
    assert.deepEqual(
        Array.from(turn.listEl.children).map((item) => item.dataset.toolCallId),
        ["filesystem:1", "filesystem:2"]
    );
    assert.deepEqual(
        Array.from(turn.listEl.children).map((item) => item.dataset.detailText),
        ['{"path": "alpha"}', '{"path": "beta"}']
    );
});

test("ceo tool_start running rows stay empty until a result arrives", () => {
    const { applyCeoToolEventToTurn } = loadApp();
    const turn = makeTurn({ text: "" });

    const item = applyCeoToolEventToTurn(turn, {
        tool_name: "filesystem",
        status: "running",
        kind: "tool_start",
        text: "{\"path\": \"alpha\"}",
        tool_call_id: "filesystem:start-1",
        source: "user",
    });

    assert.equal(turn.listEl.children.length, 1);
    assert.equal(item.dataset.toolCallId, "filesystem:start-1");
    assert.equal(item.dataset.detailText, "");
    assert.equal(item.querySelector(".interaction-step-preview").textContent, "");
    assert.equal(item.querySelector(".interaction-step-detail").textContent, "");
    assert.equal(item.querySelector(".interaction-step-status").textContent.length > 0, true);
});

test("ceo composer shows session-local compression toast only for active compressing session", () => {
    const { syncCeoCompressionToast, S, U } = loadApp();

    S.activeSessionId = "web:ceo-a";
    S.ceoSnapshotCache = {
        "web:ceo-a": {
            session_id: "web:ceo-a",
            inflight_turn: {
                status: "running",
                compression: { status: "running", text: COMPRESSION_TEXT, source: "user" },
            },
        },
        "web:ceo-b": {
            session_id: "web:ceo-b",
            inflight_turn: {
                status: "running",
                compression: { status: "running", text: COMPRESSION_TEXT, source: "user" },
            },
        },
    };

    syncCeoCompressionToast();
    assert.equal(U.ceoCompressionToast.hidden, false);
    assert.equal(U.ceoCompressionToastText.textContent, COMPRESSION_TEXT);
});

test("ceo composer hides compression toast when inflight turn is paused", () => {
    const { syncCeoCompressionToast, S, U } = loadApp();

    S.activeSessionId = "web:ceo-a";
    S.ceoSnapshotCache = {
        "web:ceo-a": {
            session_id: "web:ceo-a",
            inflight_turn: {
                status: "paused",
                compression: { status: "running", text: COMPRESSION_TEXT, source: "user" },
            },
        },
    };

    syncCeoCompressionToast();
    assert.equal(U.ceoCompressionToast.hidden, true);
    assert.equal(U.ceoCompressionToastText.textContent, "");
});

test("shared stage status maps active to running semantics", () => {
    const { stageTraceStatus, displayTaskStageStatus } = loadApp();

    assert.equal(stageTraceStatus({ status: "active" }), "running");
    assert.equal(displayTaskStageStatus("active"), displayTaskStageStatus("running"));
});

test("ceo tool output expansion prefetches full content when output_ref is present", async () => {
    const { toggleCeoToolStepOutput, __context } = loadApp();
    let callCount = 0;
    __context.ensureCeoToolStepFullOutput = async (item) => {
        callCount += 1;
        item.dataset.detailText = "FULL OUTPUT";
    };

    const previewEl = new StubHTMLElement();
    const detailEl = new StubHTMLElement();
    const disclosureEl = new StubHTMLButtonElement();
    const item = new StubHTMLElement();
    item.dataset.detailText = "line 1\nline 2\nline 3";
    item.dataset.outputRef = "artifact:artifact:tool-output";
    item.dataset.outputExpanded = "false";
    item._selectors[".interaction-step-preview"] = previewEl;
    item._selectors[".interaction-step-detail"] = detailEl;
    item._selectors[".interaction-step-disclosure"] = disclosureEl;

    toggleCeoToolStepOutput(item);
    await new Promise((resolve) => setTimeout(resolve, 0));

    assert.equal(item.dataset.outputExpanded, "true");
    assert.equal(callCount, 1);
});
