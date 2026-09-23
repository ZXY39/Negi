const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

// 用户消息编辑重发/Fork 的前端契约:
// - buildCeoUserMessageActionsMarkup:两个按钮各吃一个服务端 flag,web: 会话才产出行(R1);
// - addMsg 用户分支把按钮行渲染进 message-stack(R2);
// - normalizeCeoSnapshotMessage 保留 can_edit_fork/can_fork/task_dispatched(R3);
// - buildCeoRenderSignature 覆盖两个 flag,权威快照到达必须触发重建(R4);
// - syncCeoFeedTurnActiveClass:回合进行中给 feed 挂 .ceo-turn-active(R5);
// - editForkErrorText 映射服务端错误码(R6);
// - applyCeoEditForkGates:收尾后服务端补发门槛,按钮不等手动刷新(R8/R8b);
// - finalizePausedCeoTurn:暂停收尾把用户行落进快照缓存,门槛帧才有行可 flag(R9)。

const APP_PATH = "g3ku/web/frontend/org_graph_app.js";
const APP_CODE = fs.readFileSync(APP_PATH, "utf8");

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
        this._contentTop = 0;
        this._height = 10;
        this._feed = null;
        this._innerHTML = "";
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
                const has = String(this.className || "").split(/\s+/).includes(token);
                const next = typeof force === "boolean" ? force : !has;
                if (next === has) return next;
                next
                    ? this.classList.add(token)
                    : this.classList.remove(token);
                return next;
            },
        };
    }

    get innerHTML() {
        return this._innerHTML;
    }

    set innerHTML(value) {
        this._innerHTML = String(value);
    }

    get lastElementChild() {
        return this.children.length ? this.children[this.length - 1] : null;
    }

    setAttribute(name, value) {
        this.attributes[name] = String(value);
        if (name === "data-ceo-key") this.dataset.ceoKey = String(value);
    }

    getAttribute(name) {
        return Object.prototype.hasOwnProperty.call(this.attributes, name) ? this.attributes[name] : null;
    }

    removeAttribute(name) {
        delete this.attributes[name];
    }

    querySelector(selector) {
        return this._qs[selector] || null;
    }

    querySelectorAll(selector) {
        return this._qsAll[selector] || [];
    }

    getBoundingClientRect() {
        const feedScrollTop = this._feed && Number(this._feed.scrollTop || 0);
        return { top: this._contentTop - feedScrollTop, height: this._height };
    }

    appendChild(child) {
        this.children.push(child);
        if (child && child._feed !== undefined) child._feed = this;
        return child;
    }

    insertBefore(child, ref) {
        const next = this.children.filter((item) => item !== child);
        const index = next.indexOf(ref);
        if (index < 0) next.push(child);
        else next.splice(index, 0, child);
        this.children = next;
        if (child && child._feed !== undefined) child._feed = this;
        return child;
    }

    remove() {
        const parentChildren = this._feed && this._feed.children ? this._feed.children : null;
        if (parentChildren) {
            const index = parentChildren.indexOf(this);
            if (index >= 0) parentChildren.splice(index, 1);
        }
        this._removed = true;
    }
}

class FeedStub extends StubHTMLElement {
    constructor() {
        super();
        this.resetCount = 0;
    }

    set innerHTML(value) {
        const next = String(value);
        if (next === "") {
            this.resetCount += 1;
            this.children = [];
        }
        this._innerHTML = next;
    }

    get innerHTML() {
        return this._innerHTML;
    }

    querySelectorAll(selector) {
        if (selector === ".ceo-turn-message" || selector === ".task-trace-step" || selector === "img" || selector === ".interaction-step") {
            return [];
        }
        return this._qsAll[selector] || [];
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
        URLSearchParams,
        URL,
        AbortController,
        fetch: async () => ({ ok: true, json: async () => ({}) }),
        lucide: { createIcons() {} },
        marked: { parse: (value) => String(value) },
        DOMPurify: { sanitize: (value) => String(value) },
        performance: { now: () => 0 },
        requestAnimationFrame: (callback) => {
            callback();
            return 1;
        },
        cancelAnimationFrame: () => {},
        WebSocket: function WebSocket() {},
        addEventListener() {},
        removeEventListener() {},
        setTraceRoundActiveTool: () => {},
        hydrateTraceOutputBlocks: () => {},
    };
    context.window = context;
    vm.createContext(context);
    vm.runInContext(
        `${APP_CODE}\nthis.__testExports = {
            S,
            U,
            addMsg,
            buildCeoUserMessageActionsMarkup,
            normalizeCeoSnapshotMessage,
            buildCeoRenderSignature,
            syncCeoFeedTurnActiveClass,
            editForkErrorText,
            activeSessionId,
            applyCeoEditForkGates,
            getCeoSessionSnapshotCache,
            finalizePausedCeoTurn,
        };`,
        context
    );
    return context.__testExports;
}

function setup() {
    const api = loadApp();
    api.S.activeSessionId = "web:ceo-s1";
    api.S.ceoSessions = [{ session_id: "web:ceo-s1", is_readonly: false }];
    api.S.activeSessionFamily = "local";
    api.S.ceoTurnActive = false;
    api.U.ceoFeed = new FeedStub();
    return api;
}

test("R1 buildCeoUserMessageActionsMarkup 两个按钮各吃一个 flag + web 会话", () => {
    const api = setup();
    const both = api.buildCeoUserMessageActionsMarkup({
        turnId: "t1",
        canEditFork: true,
        canFork: true,
        sessionId: "web:ceo-s1",
    });
    assert.ok(both.includes('data-ceo-edit-resend="t1"'), both);
    assert.ok(both.includes('data-ceo-fork="t1"'), both);
    assert.ok(both.includes('class="msg-actions"'), both);

    // 只有 Fork 资格：回合在跑/等审批/压缩在途时服务端只发 can_fork。
    const forkOnly = api.buildCeoUserMessageActionsMarkup({
        turnId: "t1",
        canEditFork: false,
        canFork: true,
        sessionId: "web:ceo-s1",
    });
    assert.ok(forkOnly.includes('data-ceo-fork="t1"'), forkOnly);
    assert.ok(!forkOnly.includes("data-ceo-edit-resend"), forkOnly);

    assert.equal(api.buildCeoUserMessageActionsMarkup({ turnId: "t1", sessionId: "web:ceo-s1" }), "");
    assert.equal(api.buildCeoUserMessageActionsMarkup({ turnId: "", canEditFork: true, canFork: true, sessionId: "web:ceo-s1" }), "");
    assert.equal(api.buildCeoUserMessageActionsMarkup({ turnId: "t1", canEditFork: true, canFork: true, sessionId: "ext:qq:1" }), "");
});

test("R2 addMsg 用户分支把按钮行渲染进 message-stack", () => {
    const api = setup();
    api.addMsg("带按钮的消息", "user", { turnId: "t9", canEditFork: true, canFork: true, sessionId: "web:ceo-s1", timestamp: "2026-09-14T10:00:00" });
    const el = api.U.ceoFeed.children[0];
    assert.ok(el.innerHTML.includes('class="message-stack"'), el.innerHTML);
    assert.ok(el.innerHTML.includes('data-ceo-edit-resend="t9"'), el.innerHTML);
    assert.ok(el.innerHTML.includes('data-ceo-fork="t9"'), el.innerHTML);

    api.addMsg("无按钮的消息", "user", { sessionId: "web:ceo-s1" });
    const plain = api.U.ceoFeed.children[1];
    assert.ok(!plain.innerHTML.includes("msg-actions"), plain.innerHTML);

    // live 发送路径(两个 flag 都不带)不渲染按钮。
    api.addMsg("live 消息", "user", { turnId: "t10", canEditFork: false, canFork: false, sessionId: "web:ceo-s1" });
    assert.ok(!api.U.ceoFeed.children[2].innerHTML.includes("msg-actions"));
});

test("R3 normalizeCeoSnapshotMessage 保留 can_edit_fork/can_fork/task_dispatched", () => {
    const api = setup();
    const user = api.normalizeCeoSnapshotMessage({ role: "user", content: "u", turn_id: "t1", can_edit_fork: true, can_fork: true });
    assert.equal(user.can_edit_fork, true);
    assert.equal(user.can_fork, true);
    const userNoFlag = api.normalizeCeoSnapshotMessage({ role: "user", content: "u", turn_id: "t2" });
    assert.equal(userNoFlag.can_edit_fork, undefined);
    assert.equal(userNoFlag.can_fork, undefined);
    const assistant = api.normalizeCeoSnapshotMessage({ role: "assistant", content: "a", turn_id: "t1", task_dispatched: true });
    assert.equal(assistant.task_dispatched, true);
    // user 角色不接受 task_dispatched,assistant 角色不接受两个按钮 flag。
    const userCross = api.normalizeCeoSnapshotMessage({ role: "user", content: "u", task_dispatched: true });
    assert.equal(userCross.task_dispatched, undefined);
    const assistantCross = api.normalizeCeoSnapshotMessage({ role: "assistant", content: "a", can_edit_fork: true, can_fork: true });
    assert.equal(assistantCross.can_edit_fork, undefined);
    assert.equal(assistantCross.can_fork, undefined);
});

test("R4 buildCeoRenderSignature 覆盖编辑/Fork 标志", () => {
    const api = setup();
    const base = [{ role: "user", content: "u", turn_id: "t1" }, { role: "assistant", content: "a", turn_id: "t1" }];
    const withFlag = [{ role: "user", content: "u", turn_id: "t1", can_edit_fork: true }, { role: "assistant", content: "a", turn_id: "t1", task_dispatched: true }];
    const signatureBase = api.buildCeoRenderSignature(base, null, null);
    const signatureFlagged = api.buildCeoRenderSignature(withFlag, null, null);
    assert.ok(signatureBase && signatureFlagged);
    assert.notEqual(signatureBase, signatureFlagged);
    // can_fork 单独变化也必须触发重建，否则回合在跑时补发的 Fork 资格落不到像素上。
    const withForkOnly = [{ role: "user", content: "u", turn_id: "t1", can_fork: true }, { role: "assistant", content: "a", turn_id: "t1" }];
    assert.notEqual(signatureBase, api.buildCeoRenderSignature(withForkOnly, null, null));
});

test("R4b buildCeoRenderSignature 覆盖 live 回合的阶段轨道增量", () => {
    // 切回会话时先按缓存渲染并写签名;随后到达的权威快照若只在阶段轨道上更新,
    // 签名相同就会被整份跳过,新阶段/新工具轮要刷新网页才出现。
    const api = setup();
    const inflight = {
        source: "user",
        turn_id: "t1",
        status: "running",
        assistant_text: "正在执行",
        usage: { input_tokens: 10, output_tokens: 2 },
    };
    const oneStage = { stages: [{ stage_id: "s1", status: "running", rounds: [] }] };
    const twoStages = {
        stages: [
            { stage_id: "s1", status: "running", rounds: [] },
            { stage_id: "s2", status: "running", rounds: [] },
        ],
    };
    const newRound = {
        stages: [{
            stage_id: "s1",
            status: "running",
            rounds: [{ tools: [{ tool_name: "exec", status: "running", output_text: "abc" }] }],
        }],
    };

    const base = api.buildCeoRenderSignature([], { ...inflight, canonical_context_delta: oneStage }, null);
    const grownStage = api.buildCeoRenderSignature([], { ...inflight, canonical_context_delta: twoStages }, null);
    const grownRound = api.buildCeoRenderSignature([], { ...inflight, canonical_context_delta: newRound }, null);

    assert.ok(base && grownStage && grownRound);
    assert.notEqual(base, grownStage);
    assert.notEqual(base, grownRound);
});

test("R5 syncCeoFeedTurnActiveClass 回合进行中隐藏按钮行", () => {
    const api = setup();
    api.syncCeoFeedTurnActiveClass();
    assert.equal(api.U.ceoFeed.classList.contains("ceo-turn-active"), false);
    api.S.ceoTurnActive = true;
    api.syncCeoFeedTurnActiveClass();
    assert.equal(api.U.ceoFeed.classList.contains("ceo-turn-active"), true);
    api.S.ceoTurnActive = false;
    api.syncCeoFeedTurnActiveClass();
    assert.equal(api.U.ceoFeed.classList.contains("ceo-turn-active"), false);
});

test("R6 editForkErrorText 映射服务端错误码", () => {
    const api = setup();
    assert.match(api.editForkErrorText({ code: "edit_fork_blocked_by_async_task" }), /异步任务/);
    assert.match(api.editForkErrorText({ code: "boundary_unavailable" }), /最近 3 轮/);
    assert.match(api.editForkErrorText({ code: "ceo_turn_in_progress" }), /回合进行中/);
    assert.equal(api.editForkErrorText({ message: "boom" }), "boom");
    assert.equal(api.editForkErrorText(null), "unknown error");
});

test("R7 接线静态契约:委托/时序/横幅/HTML 元素", () => {
    // feed 级点击委托(编辑 + Fork)与横幅取消按钮。
    assert.ok(APP_CODE.includes('[data-ceo-edit-resend]'), "缺少编辑按钮点击委托");
    assert.ok(APP_CODE.includes('[data-ceo-fork]'), "缺少 Fork 按钮点击委托");
    assert.ok(APP_CODE.includes("[data-ceo-edit-resend-cancel]"), "缺少横幅取消委托");
    // 编辑重发时序:关旧 WS → REST 截断 → 重连 → 等 open → 既有 WS 发送链。
    const submitBody = APP_CODE.slice(APP_CODE.indexOf("async function submitCeoEditResend"));
    const submitBlock = submitBody.slice(0, submitBody.indexOf("\nasync function handleCeoForkClick"));
    const order = [
        submitBlock.indexOf("closeCeoWs()"),
        submitBlock.indexOf("ApiClient.truncateCeoSession("),
        submitBlock.indexOf("initCeoWs()"),
        submitBlock.indexOf("whenCeoWsOpen("),
        submitBlock.indexOf("sendImmediateCeoMessage("),
    ];
    order.forEach((position, index) => {
        assert.ok(position >= 0, `submitCeoEditResend 缺少步骤 ${index}`);
        if (index > 0) assert.ok(position > order[index - 1], `submitCeoEditResend 步骤顺序错误: ${index}`);
    });
    // sendCeoMessage 的编辑分支绝不进 follow-up 队列。
    const sendBody = APP_CODE.slice(APP_CODE.indexOf("function sendCeoMessage()"));
    const sendBlock = sendBody.slice(0, sendBody.indexOf("\nconst canPause"));
    const editBranch = sendBlock.indexOf("if (S.ceoEditResend)");
    const queueBranch = sendBlock.indexOf("enqueueCeoFollowUp(");
    assert.ok(editBranch >= 0 && queueBranch >= 0 && editBranch < queueBranch, "编辑分支必须先于 follow-up 队列分支");
    // initCeoWs 挂 onopen 结算 waiter;closeCeoWs 拒绝 waiter。
    assert.ok(APP_CODE.includes("settleCeoWsOpenWaiters(true)"), "initCeoWs 缺少 onopen 结算");
    assert.ok(APP_CODE.includes("settleCeoWsOpenWaiters(false)"), "closeCeoWs 缺少 waiter 拒绝");
    // 防御型显示 hook 挂在 syncCeoPrimaryButton(所有轮状态变化的汇聚点)。
    const primaryBody = APP_CODE.slice(APP_CODE.indexOf("function syncCeoPrimaryButton()"));
    assert.ok(primaryBody.slice(0, 200).includes("syncCeoFeedTurnActiveClass()"), "syncCeoPrimaryButton 缺少防御型显示 hook");
    // HTML 横幅容器 + U 绑定。
    const html = fs.readFileSync("g3ku/web/frontend/org_graph.html", "utf8");
    assert.ok(html.includes('id="ceo-edit-resend-banner"'), "org_graph.html 缺少横幅容器");
    assert.ok(APP_CODE.includes('ceoEditResendBanner: document.getElementById("ceo-edit-resend-banner")'), "U 缺少横幅绑定");
    // CSS:按钮行悬停显隐 + 回合进行中只隐藏编辑按钮。
    const css = fs.readFileSync("g3ku/web/frontend/org_graph.css", "utf8");
    assert.ok(css.includes(".msg-actions"), "CSS 缺少 .msg-actions");
    assert.ok(css.includes(".ceo-turn-active .msg-action-edit"), "CSS 缺少编辑按钮的防御型隐藏规则");
    // 整行隐藏会把 Fork 一起吃掉（暂停/运行中正是唯一还能 Fork 的时刻）。
    assert.ok(!css.includes(".ceo-turn-active .msg-actions"), "CSS 仍在回合进行中整行隐藏按钮");
    // 收尾后服务端补发的门槛帧必须有分发。
    assert.ok(
        APP_CODE.includes('payload.type === "ceo.edit_fork.gates"'),
        "WS 分发缺少 ceo.edit_fork.gates 处理"
    );
});

function seedRenderedCeoCache(api, messages) {
    // 模拟"刚按无 flag 的缓存渲染完"：签名与渲染会话都对齐当前消息列表。
    api.S.ceoFeedRenderSessionId = "web:ceo-s1";
    api.S.ceoScrollToLatestOnSnapshot = false;
    api.S.ceoSnapshotCache["web:ceo-s1"] = {
        session_id: "web:ceo-s1",
        messages,
        inflight_turn: null,
        preserved_turn: null,
    };
    api.S.ceoFeedRenderSignature = api.buildCeoRenderSignature(messages, null, null);
}

test("R8 applyCeoEditForkGates 收尾后补发门槛:按钮不等手动刷新", () => {
    const api = setup();
    seedRenderedCeoCache(api, [
        { role: "user", content: "同批第一条", turn_id: "t2" },
        { role: "user", content: "同批第二条", turn_id: "t2" },
        { role: "assistant", content: "a2", turn_id: "t2" },
    ]);
    const feed = new FeedStub();
    api.U.ceoFeed = feed;

    api.applyCeoEditForkGates({ turn_ids: ["t2"] });

    const cached = api.getCeoSessionSnapshotCache("web:ceo-s1").messages;
    assert.equal(cached[0].can_edit_fork, true, "同批首条必须拿到 flag");
    assert.equal(cached[1].can_edit_fork, undefined, "同批共享 turn_id 的后续行不得拿到 flag");
    const renderedHtml = feed.children.map((child) => child.innerHTML).join("\n");
    assert.ok(renderedHtml.includes('data-ceo-edit-resend="t2"'), renderedHtml);
    assert.equal((renderedHtml.match(/msg-actions/g) || []).length, 1);

    // 同一集合重复推送：签名未变，不得再重建一次。
    const rebuilds = feed.resetCount;
    api.applyCeoEditForkGates({ turn_ids: ["t2"] });
    assert.equal(feed.resetCount, rebuilds);

    // 空集合 = 整份收回（本轮派发了任务，或旧行掉出"最近 3 轮"窗口）。
    api.applyCeoEditForkGates({ turn_ids: [] });
    assert.equal(api.getCeoSessionSnapshotCache("web:ceo-s1").messages[0].can_edit_fork, undefined);
    assert.ok(!feed.children.map((child) => child.innerHTML).join("\n").includes("msg-actions"));
});

test("R8b 两份门槛列表各自独立:回合在跑时只发 Fork 资格", () => {
    const api = setup();
    seedRenderedCeoCache(api, [
        { role: "user", content: "第一条", turn_id: "t1" },
        { role: "assistant", content: "a1", turn_id: "t1" },
    ]);
    const feed = new FeedStub();
    api.U.ceoFeed = feed;

    // 服务端在非稳定态返回 (edit=None, fork=gates)：编辑资格收回、Fork 资格保留。
    api.applyCeoEditForkGates({ turn_ids: [], fork_turn_ids: ["t1"] });
    const cached = api.getCeoSessionSnapshotCache("web:ceo-s1").messages;
    assert.equal(cached[0].can_edit_fork, undefined);
    assert.equal(cached[0].can_fork, true);
    const renderedHtml = feed.children.map((child) => child.innerHTML).join("\n");
    assert.ok(renderedHtml.includes('data-ceo-fork="t1"'), renderedHtml);
    assert.ok(!renderedHtml.includes("data-ceo-edit-resend"), renderedHtml);

    api.applyCeoEditForkGates({ turn_ids: [], fork_turn_ids: [] });
    assert.equal(api.getCeoSessionSnapshotCache("web:ceo-s1").messages[0].can_fork, undefined);
    assert.ok(!feed.children.map((child) => child.innerHTML).join("\n").includes("msg-actions"));
});

function seedPausedTurn(api, turnId) {
    // 手写 live 回合：finalizePausedCeoTurn 只吃 textEl/flowEl 两个元素句柄。
    const turn = {
        source: "user",
        turnId,
        textEl: new StubHTMLElement(),
        flowEl: new StubHTMLElement(),
        steps: 0,
        finalized: false,
        liveStreamText: "",
    };
    api.S.ceoPendingTurns = [turn];
    api.S.ceoSnapshotCache["web:ceo-s1"] = {
        session_id: "web:ceo-s1",
        messages: [],
        inflight_turn: {
            source: "user",
            turn_id: turnId,
            status: "running",
            user_message: { content: "刚发出去就被暂停", timestamp: "2026-09-14T10:00:00" },
        },
        preserved_turn: null,
    };
    return turn;
}

test("R9 暂停收尾把用户行落进快照缓存，补发门槛帧才有行可 flag", () => {
    const api = setup();
    api.U.ceoFeed = new FeedStub();
    seedPausedTurn(api, "t2");

    assert.equal(
        api.finalizePausedCeoTurn("已暂停", { source: "user", turnId: "t2", landTranscriptRows: true }),
        true
    );
    const messages = api.getCeoSessionSnapshotCache("web:ceo-s1").messages;
    const pausedRow = messages.find((item) => item.role === "user" && item.turn_id === "t2");
    assert.ok(pausedRow, "暂停没有 ceo.reply.final，收尾必须自己把用户行按 turn_id 落进缓存");

    // 落进行里之后，同一轮补发的门槛帧才盖得上章——此前正是"刷新才有按钮"的根因。
    api.applyCeoEditForkGates({ turn_ids: ["t2"], fork_turn_ids: ["t2"] });
    const flagged = api.getCeoSessionSnapshotCache("web:ceo-s1").messages
        .find((item) => item.role === "user" && item.turn_id === "t2");
    assert.equal(flagged.can_fork, true);
    assert.equal(flagged.can_edit_fork, true);
});

test("R9b 审批等待造成的暂停不落地转录行（回合还没结束）", () => {
    const api = setup();
    api.U.ceoFeed = new FeedStub();
    seedPausedTurn(api, "t3");

    assert.equal(
        api.finalizePausedCeoTurn("已暂停", { source: "user", turnId: "t3" }),
        true
    );
    const entry = api.getCeoSessionSnapshotCache("web:ceo-s1");
    assert.equal((entry.messages || []).length, 0, "未结束的回合不得伪装成转录行");
    assert.equal(entry.inflight_turn.status, "paused");
});
