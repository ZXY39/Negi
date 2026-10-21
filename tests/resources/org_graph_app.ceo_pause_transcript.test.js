const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const { readFrontendSources } = require("./support/frontend_sources.js");

// 手动暂停之后服务端依次推：ceo.control_ack → ceo.state(completed) → ceo.turn.patch(inflight=null)
// → ceo.edit_fork.gates。这条车道必须让"刚被暂停的那一回合"留在会话面上：用户气泡、
// 模型气泡、以及它已经画出来的阶段卡都不能因为一次缓存重建而消失，用户气泡还要拿到
// 编辑按钮（门槛帧按 turn_id 落 flag，行上没有 turn_id 就永远等不到按钮）。

const APP_PATH = "g3ku/web/frontend/org_graph_app.js";
const APP_CODE = readFrontendSources(["app"]).join("\n");
const SESSION_ID = "web:ceo-pause-s1";

class StubElement {}
class StubHTMLElement extends StubElement {
    constructor() {
        super();
        this.tagName = "DIV";
        this.className = "";
        this.hidden = false;
        this.open = false;
        this.textContent = "";
        this.dataset = {};
        this.attributes = {};
        this.children = [];
        this.scrollTop = 0;
        this.scrollHeight = 0;
        this.clientHeight = 0;
        this._qs = {};
        this._qsAll = {};
        this._innerHTML = "";
        this.classList = {
            add: (...tokens) => {
                const set = new Set(String(this.className || "").split(/\s+/).filter(Boolean));
                tokens.forEach((token) => set.add(token));
                this.className = [...set].join(" ");
            },
            remove: (...tokens) => {
                const set = new Set(String(this.className || "").split(/\s+/).filter(Boolean));
                tokens.forEach((token) => set.delete(token));
                this.className = [...set].join(" ");
            },
            contains: (token) => String(this.className || "").split(/\s+/).includes(token),
            toggle: (token, force) => {
                const has = String(this.className || "").split(/\s+/).includes(token);
                const next = typeof force === "boolean" ? force : !has;
                if (next !== has) (next ? this.classList.add(token) : this.classList.remove(token));
                return next;
            },
        };
    }

    get innerHTML() { return this._innerHTML; }

    set innerHTML(value) { this._innerHTML = String(value); }

    get lastElementChild() {
        return this.children.length ? this.children[this.children.length - 1] : null;
    }

    setAttribute(name, value) {
        this.attributes[name] = String(value);
        if (name === "data-ceo-key") this.dataset.ceoKey = String(value);
    }

    getAttribute(name) {
        return Object.prototype.hasOwnProperty.call(this.attributes, name) ? this.attributes[name] : null;
    }

    removeAttribute(name) { delete this.attributes[name]; }

    // 真实元素是解析 HTML 建出来的，句柄随用随有：按需长出子元素，装配路径照原样跑。
    querySelector(selector) {
        if (!this._qs[selector]) {
            const child = new StubHTMLElement();
            child._feed = this._feed;
            this._qs[selector] = child;
        }
        return this._qs[selector];
    }

    querySelectorAll(selector) { return this._qsAll[selector] || []; }

    getBoundingClientRect() { return { top: 0, height: 10 }; }

    appendChild(child) {
        this.children.push(child);
        if (child) child._feed = this._feed;
        return child;
    }

    insertBefore(child, ref) {
        const next = this.children.filter((item) => item !== child);
        const index = next.indexOf(ref);
        if (index < 0) next.push(child); else next.splice(index, 0, child);
        this.children = next;
        return child;
    }

    remove() {
        const siblings = this._feed && this._feed.children ? this._feed.children : null;
        if (siblings) {
            const index = siblings.indexOf(this);
            if (index >= 0) siblings.splice(index, 1);
        }
        this._removed = true;
    }

    addEventListener() {}

    focus() {}
}

class FeedStub extends StubHTMLElement {
    set innerHTML(value) {
        const next = String(value);
        if (next === "") this.children = [];
        this._innerHTML = next;
    }

    get innerHTML() { return this._innerHTML; }

    querySelectorAll(selector) {
        if ([".ceo-turn-message", ".task-trace-step", "img", ".interaction-step"].includes(selector)) return [];
        return this._qsAll[selector] || [];
    }
}

function loadApp() {
    const context = {
        console, setTimeout, clearTimeout, setInterval, clearInterval, queueMicrotask,
        structuredClone: global.structuredClone,
        navigator: { clipboard: { writeText: async () => {} } },
        location: { protocol: "http:", host: "localhost", pathname: "/org_graph.html" },
        localStorage: { getItem: () => null, setItem: () => {}, removeItem: () => {} },
        sessionStorage: { getItem: () => null, setItem: () => {}, removeItem: () => {} },
        document: {
            getElementById: () => null,
            querySelector: () => null,
            querySelectorAll: () => [],
            createElement: () => new StubHTMLElement(),
            addEventListener: () => {},
        },
        Element: StubElement,
        HTMLElement: StubHTMLElement,
        HTMLButtonElement: StubHTMLElement,
        HTMLInputElement: StubHTMLElement,
        HTMLTextAreaElement: StubHTMLElement,
        HTMLSelectElement: StubHTMLElement,
        URLSearchParams, URL, AbortController,
        fetch: async () => ({ ok: true, json: async () => ({}) }),
        lucide: { createIcons() {} },
        marked: { parse: (value) => String(value) },
        DOMPurify: { sanitize: (value) => String(value) },
        performance: { now: () => 0 },
        requestAnimationFrame: (callback) => { callback(); return 1; },
        cancelAnimationFrame: () => {},
        WebSocket: function WebSocket() {},
        addEventListener() {},
        removeEventListener() {},
        setTraceRoundActiveTool: () => {},
        hydrateTraceOutputBlocks: () => {},
        ApiClient: {
            getCeoWsUrl: () => "ws://localhost/api/ws/ceo",
            setActiveSessionId() {},
            getActiveSessionId: () => SESSION_ID,
        },
    };
    context.window = context;
    vm.createContext(context);
    vm.runInContext(`${APP_CODE}
this.__x = {
    S, U, WebSocket,
    sendImmediateCeoMessage,
    patchCeoInflightTurn,
    handleCeoControlAck,
    applyCeoState,
    applyCeoEditForkGates,
    getCeoSessionSnapshotCache,
    buildCeoUserMessageEditMarkup,
    syncCeoPrimaryButton,
};`, context);
    vm.runInContext(`
        renderCeoSessions = () => {};
        syncCeoSessionActions = () => {};
        patchCeoSessionRuntimeState = () => false;
        maybeDispatchQueuedCeoFollowUps = () => false;
        pruneRuntimeSentCeoFollowUps = () => {};
        scheduleSyncCeoComposerUsageOutline = () => {};
        syncCeoCompressionDivider = () => {};
        syncCeoModelRetryToast = () => {};
        syncCeoComposerUsageOutline = () => {};
        scheduleCeoComposerUsageRefresh = () => {};
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
    `, context);
    const api = context.__x;
    api.S.activeSessionId = SESSION_ID;
    api.S.ceoSessions = [{ session_id: SESSION_ID, is_readonly: false }];
    api.S.activeSessionFamily = "local";
    api.S.ceoTurnActive = true;
    api.S.ceoUploads = [];
    api.U.ceoFeed = new FeedStub();
    api.U.ceoSend = new StubHTMLElement();
    api.U.ceoInput = { value: "", style: {}, scrollHeight: 40 };
    api.WebSocket.OPEN = 1;
    api.S.ceoWs = { readyState: 1, send() {}, close() {} };
    return api;
}

const STAGE_DELTA = {
    stages: [{
        stage_id: "st1",
        stage_index: 1,
        stage_goal: "看仓库",
        status: "active",
        rounds: [{ round_index: 1, tool_names: ["exec"], tools: [{ tool_call_id: "c1", tool_name: "exec", status: "running" }] }],
    }],
};

function startRunningTurn(api) {
    api.sendImmediateCeoMessage({ text: "帮我看下仓库", uploads: [], scrollMode: "bottom" });
    api.patchCeoInflightTurn({
        source: "user",
        turn_id: "t1",
        status: "running",
        assistant_text: "正在处理",
        user_messages: [{ role: "user", content: "帮我看下仓库", turn_id: "t1", timestamp: "2026-10-20T10:00:00" }],
        canonical_context_delta: STAGE_DELTA,
    }, { sessionId: SESSION_ID, cacheField: "inflight_turn" });
}

function applyPauseFrames(api, ackPayload) {
    // 服务端手动暂停的帧序：ack → state(completed) → patch(inflight=null) → 门槛帧。
    api.handleCeoControlAck(ackPayload);
    api.applyCeoState(
        { status: "completed", is_running: false, paused: false, stop_reason: "user_pause" },
        { source: "user" }
    );
    api.patchCeoInflightTurn(null, { sessionId: SESSION_ID, cacheField: "inflight_turn" });
    api.applyCeoEditForkGates({ turn_ids: ["t1"], fork_reply_turn_ids: ["t1"] });
}

function feedMarkup(api) {
    return api.U.ceoFeed.children.map((child) => String(child.innerHTML || "")).join("\n");
}

// 回合气泡的正文/轨道挂在子节点上（真实页面也是），所以读句柄而不是父节点的 innerHTML。
function renderedTurnBubbles(api) {
    return api.U.ceoFeed.children
        .filter((child) => String(child.className || "").includes("ceo-turn-message"))
        .map((child) => ({
            text: String(child.querySelector(".assistant-text")?.textContent || ""),
            trace: String(child.querySelector(".interaction-flow-list")?.innerHTML || ""),
            flowHidden: !!child.querySelector(".interaction-flow")?.hidden,
        }));
}

test("暂停后：用户气泡、模型气泡与阶段卡都留在会话面上", () => {
    const api = loadApp();
    startRunningTurn(api);
    assert.equal(api.U.ceoFeed.children.length, 2, "运行中：用户气泡 + 回合气泡");

    applyPauseFrames(api, { action: "pause", accepted: true, source: "user", turn_id: "t1" });

    const html = feedMarkup(api);
    assert.ok(html.includes("帮我看下仓库"), `暂停后用户气泡不该消失: ${html}`);
    const bubbles = renderedTurnBubbles(api);
    assert.equal(bubbles.length, 1, `暂停后必须仍有且只有一条模型气泡: ${JSON.stringify(bubbles)}`);
    assert.equal(bubbles[0].text, "已暂停", "暂停后模型气泡必须留下「已暂停」");
    assert.equal(bubbles[0].trace, "stage-trace", "暂停后阶段卡必须还在");
    assert.equal(bubbles[0].flowHidden, false, "有阶段卡的回合不能把轨道折起来");

    const entry = api.getCeoSessionSnapshotCache(SESSION_ID);
    const rows = entry?.messages || [];
    const userRow = rows.find((row) => row.role === "user" && row.turn_id === "t1");
    assert.ok(userRow, `缓存里必须有这一轮的用户行: ${JSON.stringify(rows)}`);
    const pausedReply = rows.find((row) => row.role === "assistant");
    assert.ok(pausedReply, `缓存里必须有暂停后的模型行: ${JSON.stringify(rows)}`);
    assert.equal(pausedReply.status, "paused", "暂停收尾写进缓存的模型行必须带 status=paused");
    assert.equal(pausedReply.turn_id, "t1", "暂停收尾的模型行必须带同一个 turn_id，门槛帧才落得上");
    assert.ok(
        pausedReply.canonical_context_delta || pausedReply.canonical_context,
        `暂停收尾的模型行必须带住阶段轨道: ${JSON.stringify(pausedReply)}`
    );
});

test("暂停后：用户气泡拿到编辑按钮", () => {
    const api = loadApp();
    startRunningTurn(api);
    applyPauseFrames(api, { action: "pause", accepted: true, source: "user", turn_id: "t1" });

    const entry = api.getCeoSessionSnapshotCache(SESSION_ID);
    const userRow = (entry?.messages || []).find((row) => row.role === "user" && row.turn_id === "t1");
    assert.ok(userRow, "暂停后缓存里必须有这一轮的用户行");
    assert.equal(userRow.can_edit_fork, true, "暂停后用户气泡必须拿到编辑资格");
    assert.ok(
        api.buildCeoUserMessageEditMarkup({ turnId: "t1", canEditFork: true, sessionId: SESSION_ID })
            .includes("data-ceo-edit-resend"),
        "编辑按钮标记必须产出"
    );
    assert.ok(feedMarkup(api).includes("data-ceo-edit-resend"), `重建后的 DOM 里必须画出编辑按钮: ${feedMarkup(api)}`);
});

test("暂停后：ack 不带 turn_id 时从在飞回合认领", () => {
    // 实盘 ack 的载荷只有 action/accepted/source（旧进程）。turn_id 只能从在飞回合自己认出来，
    // 否则缓存里的用户行没有 turn_id，门槛帧永远 flag 不到这一行。
    const api = loadApp();
    startRunningTurn(api);
    applyPauseFrames(api, { action: "pause", accepted: true, source: "user" });

    const entry = api.getCeoSessionSnapshotCache(SESSION_ID);
    const userRow = (entry?.messages || []).find((row) => row.role === "user");
    assert.ok(userRow, "暂停后缓存里必须有这一轮的用户行");
    assert.equal(userRow.turn_id, "t1", "turn_id 必须从在飞回合认领");
    assert.equal(userRow.can_edit_fork, true, "认领到 turn_id 后编辑资格才盖得上");
});
