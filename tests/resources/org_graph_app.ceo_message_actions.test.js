const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

// 用户消息编辑重发/Fork 的前端契约:
// - buildCeoUserMessageEditMarkup:用户气泡那颗是裸按钮(can_edit_fork),web: 会话才产出(R1);
// - buildCeoAssistantForkMarkup/addMsg/renderCeoAssistantForkAction:Fork 挂在回复行的元信息
//   行、排在用量左边,服务端只有这一个切点(R10/R11);
// - 两颗按钮都排在元信息行里:编辑在发送时间右边,不再自成 .msg-actions 一行(R2);
// - 元信息(时间/token/复制)由点击消息显形(.meta-open),按钮不吃这道门(R7 的 CSS 契约);
// - 提问轴的 can_fork / at=question 已从这条车道整体摘掉:flag 不进缓存、不进签名(R3/R4);
// - normalizeCeoSnapshotMessage 保留 can_edit_fork/can_fork_reply/task_dispatched(R3/R12);
// - syncCeoFeedTurnActiveClass:回合进行中给 feed 挂 .ceo-turn-active(R5);
// - editForkErrorText 映射服务端错误码(R6);
// - applyCeoEditForkGates:收尾后服务端补发门槛,按钮不等手动刷新(R8/R8b/R13);
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
            buildCeoUserMessageEditMarkup,
            buildCeoAssistantForkMarkup,
            renderCeoAssistantForkAction,
            renderPersistedCeoAssistantTurn,
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

test("R1 用户气泡只出编辑，而且不再自成一行操作区", () => {
    const api = setup();
    const markup = api.buildCeoUserMessageEditMarkup({
        turnId: "t1",
        canEditFork: true,
        sessionId: "web:ceo-s1",
    });
    assert.ok(markup.includes('data-ceo-edit-resend="t1"'), markup);
    assert.ok(!markup.includes('data-ceo-fork='), `用户气泡不该挂 Fork 按钮: ${markup}`);
    // 裸按钮：它挂进气泡的元信息行，不再包 .msg-actions 容器。
    assert.ok(!markup.includes("msg-actions"), markup);

    // 没有编辑资格（回合在跑/等审批/压缩在途）时用户行不出任何按钮：编辑改的是源
    // 转录；Fork 的入口在回复行，由 can_fork_reply 驱动。
    assert.equal(api.buildCeoUserMessageEditMarkup({
        turnId: "t1", canEditFork: false, sessionId: "web:ceo-s1",
    }), "");
    assert.equal(api.buildCeoUserMessageEditMarkup({ turnId: "t1", sessionId: "web:ceo-s1" }), "");
    assert.equal(api.buildCeoUserMessageEditMarkup({ turnId: "", canEditFork: true, sessionId: "web:ceo-s1" }), "");
    assert.equal(api.buildCeoUserMessageEditMarkup({ turnId: "t1", canEditFork: true, sessionId: "ext:qq:1" }), "");
});

test("R2 addMsg 用户分支把编辑排在发送时间右边的同一行", () => {
    const api = setup();
    const el = api.addMsg("带按钮的消息", "user", {
        turnId: "t9", canEditFork: true, sessionId: "web:ceo-s1", timestamp: "2026-09-14T10:00:00",
    });
    const html = String(el.innerHTML);
    assert.ok(html.includes('class="message-stack"'), html);
    assert.ok(html.includes('data-ceo-edit-resend="t9"'), html);
    assert.ok(!html.includes('data-ceo-fork='), html);
    // 时间与编辑落在同一个 .msg-meta 行里，时间在前、编辑在最右。
    assert.ok(/class="msg-meta">[\s\S]*?class="msg-meta-text"[\s\S]*?data-ceo-edit-resend="t9"/.test(html), html);

    api.addMsg("无按钮的消息", "user", { sessionId: "web:ceo-s1" });
    assert.ok(!api.U.ceoFeed.children[1].innerHTML.includes("data-ceo-edit-resend"));

    // live 发送路径（不带 flag）不渲染按钮。
    api.addMsg("live 消息", "user", { turnId: "t10", canEditFork: false, sessionId: "web:ceo-s1" });
    assert.ok(!api.U.ceoFeed.children[2].innerHTML.includes("data-ceo-edit-resend"));
});

test("R3 normalizeCeoSnapshotMessage 保留按钮 flag，提问轴那条已摘干净", () => {
    const api = setup();
    const user = api.normalizeCeoSnapshotMessage({ role: "user", content: "u", turn_id: "t1", can_edit_fork: true, can_fork: true });
    assert.equal(user.can_edit_fork, true);
    // can_fork 没有任何渲染者，服务端也不再下发；万一读到不得进缓存行（否则会白占签名）。
    assert.equal(user.can_fork, undefined);
    const userNoFlag = api.normalizeCeoSnapshotMessage({ role: "user", content: "u", turn_id: "t2" });
    assert.equal(userNoFlag.can_edit_fork, undefined);
    const assistant = api.normalizeCeoSnapshotMessage({ role: "assistant", content: "a", turn_id: "t1", task_dispatched: true, can_fork_reply: true });
    assert.equal(assistant.task_dispatched, true);
    assert.equal(assistant.can_fork_reply, true);
    // user 角色不接受 task_dispatched，assistant 角色不接受编辑 flag。
    const userCross = api.normalizeCeoSnapshotMessage({ role: "user", content: "u", task_dispatched: true });
    assert.equal(userCross.task_dispatched, undefined);
    const assistantCross = api.normalizeCeoSnapshotMessage({ role: "assistant", content: "a", can_edit_fork: true });
    assert.equal(assistantCross.can_edit_fork, undefined);
});

test("R4 buildCeoRenderSignature 覆盖两颗按钮的 flag", () => {
    const api = setup();
    const base = [{ role: "user", content: "u", turn_id: "t1" }, { role: "assistant", content: "a", turn_id: "t1" }];
    const signatureBase = api.buildCeoRenderSignature(base, null, null);
    const withFlag = [{ role: "user", content: "u", turn_id: "t1", can_edit_fork: true }, { role: "assistant", content: "a", turn_id: "t1", task_dispatched: true }];
    assert.ok(signatureBase && api.buildCeoRenderSignature(withFlag, null, null));
    assert.notEqual(signatureBase, api.buildCeoRenderSignature(withFlag, null, null));
    // 回复轴 flag 单独变化也必须触发重建，否则补发的门槛帧落不到像素上。
    const withReplyFork = [{ role: "user", content: "u", turn_id: "t1" }, { role: "assistant", content: "a", turn_id: "t1", can_fork_reply: true }];
    assert.notEqual(signatureBase, api.buildCeoRenderSignature(withReplyFork, null, null));
    // 提问轴不进签名：残留的 can_fork 不该白触发一次整列重建。
    const withStaleCanFork = [{ role: "user", content: "u", turn_id: "t1", can_fork: true }, { role: "assistant", content: "a", turn_id: "t1" }];
    assert.equal(signatureBase, api.buildCeoRenderSignature(withStaleCanFork, null, null));
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
    assert.match(api.editForkErrorText({ code: "edit_fork_blocked_by_async_task" }), /任务还没跑完/);
    assert.match(api.editForkErrorText({ code: "boundary_unavailable" }), /最近 12 轮/);
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
    // CSS：两颗按钮常驻挂在自己的元信息行里，.msg-actions 那层容器整块删掉了。
    const css = fs.readFileSync("g3ku/web/frontend/org_graph.css", "utf8");
    assert.ok(!css.includes(".msg-actions"), "CSS 里还留着 .msg-actions 容器");
    assert.ok(!APP_CODE.includes("msg-actions"), "前端还在校自成一行的操作区");
    assert.ok(css.includes(".ceo-turn-active .msg-action-edit"), "CSS 缺少编辑按钮的防御型隐藏规则");
    // 按整行隐藏会把同一行里的 Fork 一起吃掉（暂停/运行中正是唯一还能 Fork 的时刻）。
    assert.ok(!css.includes(".ceo-turn-active .msg-meta"), "CSS 仍按整行隐藏元信息里的按钮");
    assert.ok(!css.includes(".message:hover .msg-action"), "动作按钮仍靠悬停显形");
    // 元信息（时间/用量/复制）改成点击这条消息才显形，两车道各一条门；两颗按钮不吃这道门。
    assert.ok(
        css.includes(".ceo-turn-message.usage-collapsed:not(.meta-open) .ceo-turn-usage"),
        "CSS 缺轨道回合用量的点击门"
    );
    assert.ok(
        css.includes(".message:not(.meta-open) .msg-meta-text") &&
            css.includes(".message:not(.meta-open) .msg-copy-btn"),
        "CSS 缺普通气泡元信息的点击门"
    );
    assert.ok(!css.includes(".message:not(.meta-open) .msg-action-fork"), "Fork 按钮被点击门管住了");
    assert.ok(!css.includes(".message:not(.meta-open) .msg-action-edit"), "编辑按钮被点击门管住了");
    assert.ok(!css.includes(".message:hover .msg-copy-btn"), "复制按钮仍靠悬停显形");
    assert.ok(!css.includes(".message:hover .msg-meta"), "元信息行仍靠悬停显形");
    // 点击门的行为面：委托挂在 feed 的 click 上，且控件与选字都让路。
    assert.ok(APP_CODE.includes("toggleCeoMessageMetaReveal(e.target)"), "feed click 没挂元信息点开钩子");
    const revealBody = APP_CODE.slice(
        APP_CODE.indexOf("function toggleCeoMessageMetaReveal"),
        APP_CODE.indexOf("function toggleCeoMessageMetaReveal") + 900
    );
    assert.ok(revealBody.includes('closest("button, a, summary'), "点击门没给可交互控件让路");
    assert.ok(revealBody.includes("getSelection"), "点击门没给选中正文让路");
    assert.ok(revealBody.includes('closest(".message")'), "点击门没有落在消息根元素上");
    assert.ok(revealBody.includes('toggle("meta-open")'), "点击门没有切 meta-open");
    // 提问轴整条摘干净：服务端只剩一个切点，客户端不许再发 at，也不许留切点属性。
    assert.ok(!APP_CODE.includes("ceoForkAt"), "前端还在读 data-ceo-fork-at");
    assert.ok(!APP_CODE.includes("at: mode"), "Fork 请求体还带 at");
    assert.ok(!APP_CODE.includes("fork_turn_ids"), "前端还在消费提问轴的 fork_turn_ids");
    // Fork 成功侧不弹 toast：切会话 + 输入框回填就是回执；失败侧必须说话。
    const forkBody = APP_CODE.slice(
        APP_CODE.indexOf("async function handleCeoForkClick"),
        APP_CODE.indexOf("async function handleCeoForkClick") + 2400
    );
    assert.ok(!forkBody.includes('kind: "success"'), "Fork 成功又弹提示层了");
    assert.ok(forkBody.includes('title: "Fork 失败"'), "Fork 失败侧没有提示");
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
    assert.equal((renderedHtml.match(/data-ceo-edit-resend=/g) || []).length, 1);

    // 同一集合重复推送：签名未变，不得再重建一次。
    const rebuilds = feed.resetCount;
    api.applyCeoEditForkGates({ turn_ids: ["t2"] });
    assert.equal(feed.resetCount, rebuilds);

    // 空集合 = 整份收回（本轮派发了还没跑完的任务，或旧行掉出"最近 12 轮"窗口）。
    api.applyCeoEditForkGates({ turn_ids: [] });
    assert.equal(api.getCeoSessionSnapshotCache("web:ceo-s1").messages[0].can_edit_fork, undefined);
    assert.ok(!feed.children.map((child) => child.innerHTML).join("\n").includes("data-ceo-edit-resend"));
});

test("R8b 门槛帧只剩两份列表:回合在跑时用户行不出按钮、回复行出 Fork", () => {
    const api = setup();
    seedRenderedCeoCache(api, [
        { role: "user", content: "第一条", turn_id: "t1" },
        { role: "assistant", content: "a1", turn_id: "t1" },
    ]);
    const feed = new FeedStub();
    api.U.ceoFeed = feed;

    // 非稳定态收回编辑资格。提问轴的 fork_turn_ids 已经不再生效——旧进程真发来这个键，
    // 前端也不认：没有渲染者的资格不该画成按钮。
    api.applyCeoEditForkGates({ turn_ids: [], fork_turn_ids: ["t1"] });
    const cached = api.getCeoSessionSnapshotCache("web:ceo-s1").messages;
    assert.equal(cached[0].can_edit_fork, undefined);
    assert.equal(cached[0].can_fork, undefined);
    const renderedHtml = feed.children.map((child) => child.innerHTML).join("\n");
    assert.ok(!renderedHtml.includes("data-ceo-fork"), "提问行不再贡献 Fork 按钮");
    assert.ok(!renderedHtml.includes("data-ceo-edit-resend"), renderedHtml);

    // 带上回复轴资格：按钮挂在回复行的元信息行里，与用量同行。
    api.applyCeoEditForkGates({ turn_ids: [], fork_reply_turn_ids: ["t1"] });
    const withFork = feed.children.map((child) => child.innerHTML).join("\n");
    assert.equal((withFork.match(/data-ceo-fork="/g) || []).length, 1, withFork);
    assert.ok(withFork.includes('data-ceo-fork="t1"'), withFork);

    // 收回：整份 feed 一个 Fork 都不剩。
    api.applyCeoEditForkGates({ turn_ids: [], fork_reply_turn_ids: [] });
    assert.equal(api.getCeoSessionSnapshotCache("web:ceo-s1").messages[1].can_fork_reply, undefined);
    assert.ok(!feed.children.map((child) => child.innerHTML).join("\n").includes("data-ceo-fork"));
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
    api.applyCeoEditForkGates({ turn_ids: ["t2"], fork_reply_turn_ids: ["t2"] });
    const flagged = api.getCeoSessionSnapshotCache("web:ceo-s1").messages
        .find((item) => item.role === "user" && item.turn_id === "t2");
    assert.equal(flagged.can_edit_fork, true);
    // 回复轴的列表不许盖到用户行上（收尾时写进缓存的 assistant 行不带 turn_id，
    // 那份资格由权威快照补，见 R13）。
    assert.equal(flagged.can_fork_reply, undefined);
    assert.equal(flagged.can_fork, undefined);
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

test("R10 buildCeoAssistantForkMarkup 只出一颗裸 Fork、不吃编辑资格", () => {
    const api = setup();
    const markup = api.buildCeoAssistantForkMarkup({
        turnId: "t7",
        canForkReply: true,
        sessionId: "web:ceo-s1",
    });
    assert.ok(markup.includes('data-ceo-fork="t7"'), markup);
    assert.ok(!markup.includes("msg-actions"), markup);
    // 服务端只剩一个切点（这条回复之后），所以不再需要 at 属性区分入口。
    assert.ok(!markup.includes("data-ceo-fork-at"), markup);
    assert.ok(!markup.includes("data-ceo-edit-resend"), "编辑重发不该出现在回复下方");

    assert.equal(api.buildCeoAssistantForkMarkup({ turnId: "t7", sessionId: "web:ceo-s1" }), "");
    assert.equal(api.buildCeoAssistantForkMarkup({ turnId: "t7", canForkReply: true, sessionId: "ext:qq:1" }), "");
});

test("R11 addMsg 系统分支把 Fork 排在用量左边的同一行", () => {
    const api = setup();
    const el = api.addMsg("模型回复正文", "system", {
        markdown: true,
        sessionId: "web:ceo-s1",
        timestamp: "2026-09-14T10:00:00",
        usage: { input_tokens: 1200, output_tokens: 80, cache_hit_tokens: 900 },
        turnId: "t8",
        canForkReply: true,
    });
    const html = String(el.innerHTML);
    assert.ok(html.includes("message-stack"), html);
    // 同一个 .msg-meta 行内：Fork 在最左，用量文字排在它右边。
    assert.ok(/class="msg-meta"><button[^>]*msg-action-fork/.test(html), html);
    assert.ok(/msg-action-fork[\s\S]*?class="msg-meta-text"/.test(html), html);

    const bare = api.addMsg("没有资格的回复", "system", { markdown: true, sessionId: "web:ceo-s1", turnId: "t9" });
    assert.ok(!String(bare.innerHTML).includes("msg-action-fork"), bare.innerHTML);
});

test("R12 normalizeCeoSnapshotMessage 保留 assistant 行的 can_fork_reply", () => {
    const api = setup();
    const row = api.normalizeCeoSnapshotMessage({
        role: "assistant",
        content: "答",
        turn_id: "t10",
        can_fork_reply: true,
    });
    assert.equal(row.can_fork_reply, true);
    const plain = api.normalizeCeoSnapshotMessage({ role: "assistant", content: "答", turn_id: "t10" });
    assert.equal(plain.can_fork_reply, undefined);
});

test("R13 门槛帧的回复行列表只打 assistant，不冒名到用户行", () => {
    const api = setup();
    seedRenderedCeoCache(api, [
        { role: "user", content: "问", turn_id: "t11" },
        { role: "assistant", content: "答", turn_id: "t11" },
    ]);
    const feed = new FeedStub();
    api.U.ceoFeed = feed;

    api.applyCeoEditForkGates({ fork_reply_turn_ids: ["t11"] });

    const cached = api.getCeoSessionSnapshotCache("web:ceo-s1").messages;
    assert.equal(cached[1].can_fork_reply, true, "回复行必须拿到 Fork 资格");
    assert.equal(cached[0].can_fork_reply, undefined, "提问行不得冒名领回复行的资格");
    assert.equal(cached[0].can_fork, undefined, "回复轴列表不会给用户行盖任何 Fork 章");
    // 重建后的 DOM 里只有一颗 Fork（挂在回复那一条上）
    const renderedHtml = feed.children.map((child) => child.innerHTML).join("\n");
    assert.equal((renderedHtml.match(/data-ceo-fork="/g) || []).length, 1, renderedHtml);

    // 整份收回：新一轮把这条挤出"最近 12 轮"窗口时按钮跟着消失
    api.applyCeoEditForkGates({ fork_reply_turn_ids: [] });
    assert.equal(api.getCeoSessionSnapshotCache("web:ceo-s1").messages[1].can_fork_reply, undefined);
});
