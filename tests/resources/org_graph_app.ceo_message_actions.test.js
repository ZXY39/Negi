const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

// 用户消息编辑重发/Fork 的前端契约:
// - buildCeoUserMessageActionsMarkup:canEditFork + web: 会话才产出按钮行(R1);
// - addMsg 用户分支把按钮行渲染进 message-stack(R2);
// - normalizeCeoSnapshotMessage 保留 can_edit_fork/task_dispatched(R3);
// - buildCeoRenderSignature 覆盖两个 flag,权威快照到达必须触发重建(R4);
// - syncCeoFeedTurnActiveClass:回合进行中给 feed 挂 .ceo-turn-active(R5);
// - editForkErrorText 映射服务端错误码(R6)。

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

test("R1 buildCeoUserMessageActionsMarkup 只在 canEditFork + web 会话时产出按钮", () => {
    const api = setup();
    const markup = api.buildCeoUserMessageActionsMarkup({
        turnId: "t1",
        canEditFork: true,
        sessionId: "web:ceo-s1",
    });
    assert.ok(markup.includes('data-ceo-edit-resend="t1"'), markup);
    assert.ok(markup.includes('data-ceo-fork="t1"'), markup);
    assert.ok(markup.includes('class="msg-actions"'), markup);

    assert.equal(api.buildCeoUserMessageActionsMarkup({ turnId: "t1", canEditFork: false, sessionId: "web:ceo-s1" }), "");
    assert.equal(api.buildCeoUserMessageActionsMarkup({ turnId: "", canEditFork: true, sessionId: "web:ceo-s1" }), "");
    assert.equal(api.buildCeoUserMessageActionsMarkup({ turnId: "t1", canEditFork: true, sessionId: "ext:qq:1" }), "");
});

test("R2 addMsg 用户分支把按钮行渲染进 message-stack", () => {
    const api = setup();
    api.addMsg("带按钮的消息", "user", { turnId: "t9", canEditFork: true, sessionId: "web:ceo-s1", timestamp: "2026-09-14T10:00:00" });
    const el = api.U.ceoFeed.children[0];
    assert.ok(el.innerHTML.includes('class="message-stack"'), el.innerHTML);
    assert.ok(el.innerHTML.includes('data-ceo-edit-resend="t9"'), el.innerHTML);
    assert.ok(el.innerHTML.includes('data-ceo-fork="t9"'), el.innerHTML);

    api.addMsg("无按钮的消息", "user", { sessionId: "web:ceo-s1" });
    const plain = api.U.ceoFeed.children[1];
    assert.ok(!plain.innerHTML.includes("msg-actions"), plain.innerHTML);

    // live 发送路径(不带 flag)不渲染按钮。
    api.addMsg("live 消息", "user", { turnId: "t10", canEditFork: false, sessionId: "web:ceo-s1" });
    assert.ok(!api.U.ceoFeed.children[2].innerHTML.includes("msg-actions"));
});

test("R3 normalizeCeoSnapshotMessage 保留 can_edit_fork/task_dispatched", () => {
    const api = setup();
    const user = api.normalizeCeoSnapshotMessage({ role: "user", content: "u", turn_id: "t1", can_edit_fork: true });
    assert.equal(user.can_edit_fork, true);
    const userNoFlag = api.normalizeCeoSnapshotMessage({ role: "user", content: "u", turn_id: "t2" });
    assert.equal(userNoFlag.can_edit_fork, undefined);
    const assistant = api.normalizeCeoSnapshotMessage({ role: "assistant", content: "a", turn_id: "t1", task_dispatched: true });
    assert.equal(assistant.task_dispatched, true);
    // user 角色不接受 task_dispatched,assistant 角色不接受 can_edit_fork。
    const userCross = api.normalizeCeoSnapshotMessage({ role: "user", content: "u", task_dispatched: true });
    assert.equal(userCross.task_dispatched, undefined);
    const assistantCross = api.normalizeCeoSnapshotMessage({ role: "assistant", content: "a", can_edit_fork: true });
    assert.equal(assistantCross.can_edit_fork, undefined);
});

test("R4 buildCeoRenderSignature 覆盖编辑/Fork 标志", () => {
    const api = setup();
    const base = [{ role: "user", content: "u", turn_id: "t1" }, { role: "assistant", content: "a", turn_id: "t1" }];
    const withFlag = [{ role: "user", content: "u", turn_id: "t1", can_edit_fork: true }, { role: "assistant", content: "a", turn_id: "t1", task_dispatched: true }];
    const signatureBase = api.buildCeoRenderSignature(base, null, null);
    const signatureFlagged = api.buildCeoRenderSignature(withFlag, null, null);
    assert.ok(signatureBase && signatureFlagged);
    assert.notEqual(signatureBase, signatureFlagged);
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
    // CSS:按钮行悬停显隐 + 回合进行中隐藏。
    const css = fs.readFileSync("g3ku/web/frontend/org_graph.css", "utf8");
    assert.ok(css.includes(".msg-actions"), "CSS 缺少 .msg-actions");
    assert.ok(css.includes(".ceo-turn-active .msg-actions"), "CSS 缺少防御型隐藏规则");
});
