const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const APP_PATH = "g3ku/web/frontend/org_graph_app.js";
const APP_CODE = fs.readFileSync(APP_PATH, "utf8");

class StubElement {
    constructor(id = "") {
        this.id = id;
        this.hidden = false;
        this.disabled = false;
        this.value = "";
        this.innerHTML = "";
        this.textContent = "";
        this.attributes = {};
        this.style = {};
        this.scrollHeight = 48;
        this.dataset = {};
        this.classList = {
            add() {},
            remove() {},
            toggle() {},
            contains() { return false; },
        };
    }

    setAttribute(name, value) {
        this.attributes[name] = String(value);
    }

    getAttribute(name) {
        return this.attributes[name];
    }

    removeAttribute(name) {
        delete this.attributes[name];
    }

    addEventListener() {}

    querySelector() {
        return null;
    }

    querySelectorAll() {
        return [];
    }
}

class StubHTMLElement extends StubElement {}
class StubHTMLButtonElement extends StubHTMLElement {}
class StubHTMLInputElement extends StubHTMLElement {}
class StubHTMLTextAreaElement extends StubHTMLElement {}
class StubHTMLSelectElement extends StubHTMLElement {}

class StubDocument {
    constructor(elements) {
        this.elements = elements;
    }

    getElementById(id) {
        return this.elements[id] || null;
    }

    querySelector() {
        return null;
    }

    querySelectorAll() {
        return [];
    }

    addEventListener() {}

    createElement() {
        return new StubHTMLElement();
    }
}

function loadApp() {
    const elements = {
        "ceo-input": new StubHTMLTextAreaElement("ceo-input"),
        "ceo-send-btn": new StubHTMLButtonElement("ceo-send-btn"),
        "ceo-attach-btn": new StubHTMLButtonElement("ceo-attach-btn"),
        "ceo-file-input": new StubHTMLInputElement("ceo-file-input"),
        "ceo-upload-list": new StubHTMLElement("ceo-upload-list"),
        "ceo-follow-up-queue": new StubHTMLElement("ceo-follow-up-queue"),
    };
    const document = new StubDocument(elements);
    const socket = {
        readyState: 1,
        sent: [],
        send(payload) {
            this.sent.push(JSON.parse(payload));
        },
    };
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
        document,
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
        ApiClient: {
            getActiveSessionId: () => "web:test",
            withdrawCeoQueuedFollowUp: async (sessionId, payload) => {
                context.__withdrawCalls = context.__withdrawCalls || [];
                context.__withdrawCalls.push({ sessionId, payload });
                return { ok: true };
            },
        },
        activeSessionIsReadonly: () => false,
        icons: () => {},
        renderPendingCeoUploads: () => {},
        syncCeoAttachButton: () => {},
        syncCeoSessionActions: () => {},
        syncActiveCeoComposerDraft: () => {},
        syncCeoInputHeight: () => {},
        clearCeoComposerDraft: () => {},
        addMsg: () => {},
        showToast: (payload) => {
            context.__showToastCalls = context.__showToastCalls || [];
            context.__showToastCalls.push(payload);
        },
        patchCeoSessionRuntimeState: () => false,
        setCeoSessionSnapshotCache: () => ({}),
        createPendingCeoTurn: () => ({}),
        normalizeUploadList: (items) => Array.isArray(items) ? items : [],
        summarizeUploads: () => "",
        hasRenderableText: (value) => !!String(value || "").trim(),
        requestCeoPause: () => { context.__pauseRequested = (context.__pauseRequested || 0) + 1; },
    };
    context.window = context;
    vm.createContext(context);
    vm.runInContext(
        `${APP_CODE}
        this.__testExports = {
            S,
            U,
            syncCeoPrimaryButton,
            handleCeoPrimaryAction,
            setCeoQueuedFollowUps,
            removeCeoQueuedFollowUp,
            enqueueCeoFollowUp,
            applyCeoState,
            ensureActiveCeoTurn,
            getMergedCeoQueuedFollowUps,
            renderQueuedCeoFollowUps,
            flushCeoQueuedFollowUp,
            withdrawCeoQueuedFollowUp,
            discardCeoQueuedFollowUp,
            toggleCeoQueuedFollowUpExpansion,
            measureCeoFollowUpSingleLine,
            sendCeoMessage,
        };`,
        context
    );
    vm.runInContext(
        `
        addMsg = () => {};
    `,
        context
    );
    context.__testExports.S.ceoWs = socket;
    context.__testExports.S.activeSessionId = "web:test";
    return {
        ...context.__testExports,
        socket,
        __context: context,
    };
}

test("primary button is disabled when idle and composer is empty", () => {
    const { S, U, syncCeoPrimaryButton } = loadApp();
    S.ceoTurnActive = false;
    U.ceoInput.value = "";

    syncCeoPrimaryButton();

    assert.equal(U.ceoSend.disabled, true);
    assert.match(U.ceoSend.innerHTML, /发送/);
});

test("removing a queued follow-up re-renders the visible queue", () => {
    const { U, setCeoQueuedFollowUps, removeCeoQueuedFollowUp } = loadApp();

    setCeoQueuedFollowUps("web:test", [
        { id: "first", text: "first follow-up" },
        { id: "second", text: "second follow-up" },
    ]);
    assert.equal(U.ceoFollowUpQueue.hidden, false);
    assert.match(U.ceoFollowUpQueue.innerHTML, /first follow-up/);
    assert.match(U.ceoFollowUpQueue.innerHTML, /second follow-up/);

    removeCeoQueuedFollowUp("web:test", "first");

    assert.equal(U.ceoFollowUpQueue.hidden, false);
    assert.doesNotMatch(U.ceoFollowUpQueue.innerHTML, /first follow-up/);
    assert.match(U.ceoFollowUpQueue.innerHTML, /second follow-up/);
});

test("queued follow-up list renders chips without a queue title block", () => {
    const { U, setCeoQueuedFollowUps } = loadApp();

    setCeoQueuedFollowUps("web:test", [
        { id: "only", text: "only follow-up" },
    ]);

    assert.equal(U.ceoFollowUpQueue.hidden, false);
    assert.match(U.ceoFollowUpQueue.innerHTML, /only follow-up/);
    assert.doesNotMatch(U.ceoFollowUpQueue.innerHTML, /ceo-follow-up-queue-title/);
});

test("primary button shows send when there is input during an active turn", () => {
    const { S, U, syncCeoPrimaryButton } = loadApp();
    S.ceoTurnActive = true;
    U.ceoInput.value = "前10个";

    syncCeoPrimaryButton();

    assert.equal(U.ceoSend.disabled, false);
    assert.match(U.ceoSend.innerHTML, /发送/);
});

test("sending while a turn is active holds the follow-up in the browser, not the runtime", () => {
    const { S, U, handleCeoPrimaryAction, socket, __context } = loadApp();
    S.ceoTurnActive = true;
    U.ceoInput.value = "前10个";

    handleCeoPrimaryAction();

    assert.equal(__context.__pauseRequested || 0, 0);
    assert.equal(Array.isArray(S.ceoQueuedFollowUps?.["web:test"]), true);
    assert.equal(S.ceoQueuedFollowUps["web:test"].length, 1);
    // 默认不并入正在跑的这一轮：一条 WS 帧都不发，等本轮最终输出后按顺序起新回合。
    assert.equal(socket.sent.length, 0);
    assert.equal(S.ceoQueuedFollowUps["web:test"][0].text, "前10个");
    assert.equal(String(S.ceoQueuedFollowUps["web:test"][0].runtime_sent_at || ""), "");
    assert.equal(U.ceoFollowUpQueue.hidden, false);
    assert.match(U.ceoFollowUpQueue.innerHTML, /前10个/);
    assert.equal((__context.__showToastCalls || []).length, 0);
});

test("chip offers 插话 only for unsent items while a turn runs", () => {
    const { S, U, setCeoQueuedFollowUps } = loadApp();
    S.ceoTurnActive = true;
    setCeoQueuedFollowUps("web:test", [{ id: "draft", text: "还没发出去的补充" }]);

    // 插话是唯一带文字的那颗，图标在左、文字在右。
    assert.match(U.ceoFollowUpQueue.innerHTML, /data-follow-up-flush="draft"/);
    assert.match(U.ceoFollowUpQueue.innerHTML, /<i data-lucide="send"><\/i><span>插话<\/span>/);
    assert.match(U.ceoFollowUpQueue.innerHTML, /data-follow-up-withdraw="draft"/);
    assert.match(U.ceoFollowUpQueue.innerHTML, /data-follow-up-remove="draft"/);
    // 编辑是"取回正文继续改"，图标用铅笔；左下转弯箭头读起来像"撤回/回车"。
    assert.match(U.ceoFollowUpQueue.innerHTML, /data-lucide="pencil"/);
    assert.match(U.ceoFollowUpQueue.innerHTML, /data-lucide="trash-2"/);
    // 三颗都带悬停文字（图标按钮没有可见文案，功能只能在这里说）。
    assert.match(U.ceoFollowUpQueue.innerHTML, /title="编辑：把正文取回输入框继续改"/);
    assert.match(U.ceoFollowUpQueue.innerHTML, /title="删除这条待发送补充"/);
    assert.match(U.ceoFollowUpQueue.innerHTML, /title="插话：现在就并进正在跑的这一轮"/);
    assert.match(U.ceoFollowUpQueue.innerHTML, /title="展开正文"/);
    // 没量过之前三角一律在（放得下与否要等排版，缺证据就不能藏入口）。
    assert.doesNotMatch(U.ceoFollowUpQueue.innerHTML, /ceo-follow-up-expand[\s\S]{0,160}?hidden/);
    // 「已受理」不再画：条目还挂在输入区上面就代表没发出去。
    assert.doesNotMatch(U.ceoFollowUpQueue.innerHTML, /已受理/);

    // 回合结束后没有"下一轮"可并，插话按钮不再出现，编辑与删除留着。
    S.ceoTurnActive = false;
    setCeoQueuedFollowUps("web:test", [{ id: "draft", text: "还没发出去的补充" }]);
    assert.doesNotMatch(U.ceoFollowUpQueue.innerHTML, /data-follow-up-flush/);
    assert.match(U.ceoFollowUpQueue.innerHTML, /data-follow-up-withdraw="draft"/);
});

test("正文默认一行，点三角切到滚动块并活过重绘", () => {
    const { U, setCeoQueuedFollowUps, toggleCeoQueuedFollowUpExpansion } = loadApp();
    setCeoQueuedFollowUps("web:test", [{ id: "draft", text: "一条很长的补充正文" }]);

    assert.doesNotMatch(U.ceoFollowUpQueue.innerHTML, /is-expanded/);
    assert.match(U.ceoFollowUpQueue.innerHTML, /data-lucide="chevron-down"/);
    assert.match(U.ceoFollowUpQueue.innerHTML, /aria-expanded="false"/);

    toggleCeoQueuedFollowUpExpansion("draft");
    assert.match(U.ceoFollowUpQueue.innerHTML, /ceo-follow-up-chip is-expanded/);
    assert.match(U.ceoFollowUpQueue.innerHTML, /data-lucide="chevron-up"/);
    assert.match(U.ceoFollowUpQueue.innerHTML, /aria-expanded="true"/);

    // 队列每帧重画，展开态必须跟着条目 id 活下来；再点一次收回。
    U.ceoFollowUpQueue.innerHTML = "";
    toggleCeoQueuedFollowUpExpansion("draft");
    assert.doesNotMatch(U.ceoFollowUpQueue.innerHTML, /is-expanded/);
});

test("in-flight item paints no affordance and no state text until it is represented", () => {
    const { S, U, setCeoQueuedFollowUps } = loadApp();
    S.ceoTurnActive = true;
    // 已转出去、服务端状态帧还没跟上的那一小段：runtime 已经持有它，
    // 既不能再"插话"，也没有可撤的对象。
    setCeoQueuedFollowUps("web:test", [
        { id: "sent", text: "已经转出去的补充", runtime_sent_at: "2026-09-24T18:00:00.000Z" },
    ]);

    assert.match(U.ceoFollowUpQueue.innerHTML, /已经转出去的补充/);
    assert.doesNotMatch(U.ceoFollowUpQueue.innerHTML, /已受理/);
    assert.doesNotMatch(U.ceoFollowUpQueue.innerHTML, /data-follow-up-flush/);
    assert.doesNotMatch(U.ceoFollowUpQueue.innerHTML, /data-follow-up-withdraw/);
    assert.doesNotMatch(U.ceoFollowUpQueue.innerHTML, /data-follow-up-remove/);
});

test("立即发送 arms a single queued item on the runtime lane", () => {
    const { S, U, socket, setCeoQueuedFollowUps, flushCeoQueuedFollowUp } = loadApp();
    S.ceoTurnActive = true;
    setCeoQueuedFollowUps("web:test", [
        { id: "a", text: "第一条" },
        { id: "b", text: "第二条" },
    ]);

    assert.equal(flushCeoQueuedFollowUp("a"), true);

    assert.equal(socket.sent.length, 1);
    assert.equal(socket.sent[0].type, "client.user_message");
    // 只武装点的那一条：另一条继续留在浏览器队列里等收尾。
    assert.equal(socket.sent[0].messages.length, 1);
    assert.equal(socket.sent[0].messages[0].text, "第一条");
    const local = S.ceoQueuedFollowUps["web:test"];
    assert.ok(String(local.find((item) => item.id === "a").runtime_sent_at || ""));
    assert.equal(String(local.find((item) => item.id === "b").runtime_sent_at || ""), "");
    assert.match(U.ceoFollowUpQueue.innerHTML, /第二条/);
});

test("撤回已受理条目走服务端删除并回填输入框", async () => {
    const api = loadApp();
    const { S, U, applyCeoState, withdrawCeoQueuedFollowUp, __context } = api;
    applyCeoState({
        status: "running",
        queued_follow_up_messages: [
            { content: "压缩途中发的那条", attachments: [], metadata: { _transcript_turn_id: "t-9" } },
        ],
    });
    U.ceoInput.value = "";

    assert.equal(await withdrawCeoQueuedFollowUp("server:t-9"), true);

    const calls = __context.__withdrawCalls || [];
    assert.equal(calls.length, 1);
    assert.equal(calls[0].sessionId, "web:test");
    assert.equal(calls[0].payload.turn_id, "t-9");
    // 撤回的落点是输入框，不是转录：回填后可继续编辑，不自动发送。
    assert.equal(U.ceoInput.value, "压缩途中发的那条");
    assert.equal(U.ceoFollowUpQueue.hidden, true);
});

test("撤回未受理条目不碰服务端，只把内容还给输入框", async () => {
    const api = loadApp();
    const { S, U, setCeoQueuedFollowUps, withdrawCeoQueuedFollowUp, __context } = api;
    S.ceoTurnActive = true;
    setCeoQueuedFollowUps("web:test", [{ id: "draft", text: "写错了要改", uploads: [] }]);
    U.ceoInput.value = "";

    assert.equal(await withdrawCeoQueuedFollowUp("draft"), true);

    assert.equal((__context.__withdrawCalls || []).length, 0);
    assert.equal(U.ceoInput.value, "写错了要改");
    assert.equal(U.ceoFollowUpQueue.hidden, true);
});

test("state snapshot with a runtime-held queue paints the accepted chip", () => {
    const { U, applyCeoState, getMergedCeoQueuedFollowUps } = loadApp();

    applyCeoState({
        status: "idle",
        queued_follow_up_messages: [
            { content: "压缩途中发的那条", attachments: [], metadata: { _transcript_turn_id: "t-1" } },
        ],
    });

    // 换标签页/重启后 sessionStorage 是空的，这条候选只能由服务端状态快照画出来。
    assert.equal(U.ceoFollowUpQueue.hidden, false);
    assert.match(U.ceoFollowUpQueue.innerHTML, /压缩途中发的那条/);
    // 已受理不再写成文字；条目还在队列里就是"还没被那一轮接走"。
    assert.doesNotMatch(U.ceoFollowUpQueue.innerHTML, /已受理/);
    // 服务端排队里的条目一样可以编辑与删除（两颗都对应服务端那一行）。
    assert.match(U.ceoFollowUpQueue.innerHTML, /data-follow-up-withdraw="server:t-1"/);
    assert.match(U.ceoFollowUpQueue.innerHTML, /data-follow-up-remove="server:t-1"/);
    // 已经交给 runtime 排队了，插话那颗不再出现。
    assert.doesNotMatch(U.ceoFollowUpQueue.innerHTML, /data-follow-up-flush/);
    const merged = getMergedCeoQueuedFollowUps("web:test");
    assert.equal(merged.length, 1);
    assert.equal(merged[0].accepted_by_runtime, true);
    assert.equal(merged[0].id, "server:t-1");
});

test("删除已受理条目撤下服务端那一行但不回填输入框", async () => {
    const api = loadApp();
    const { U, applyCeoState, discardCeoQueuedFollowUp, __context } = api;
    applyCeoState({
        status: "running",
        queued_follow_up_messages: [
            { content: "压缩途中发的那条", attachments: [], metadata: { _transcript_turn_id: "t-8" } },
        ],
    });
    U.ceoInput.value = "";

    assert.equal(await discardCeoQueuedFollowUp("server:t-8"), true);

    const calls = __context.__withdrawCalls || [];
    assert.equal(calls.length, 1);
    assert.equal(calls[0].payload.turn_id, "t-8");
    // 与编辑的分工：删除只把条目弄走，正文不进输入框。
    assert.equal(U.ceoInput.value, "");
    assert.equal(U.ceoFollowUpQueue.hidden, true);
});

test("a follow-up the runtime already holds is not painted twice", () => {
    const { U, applyCeoState, setCeoQueuedFollowUps, getMergedCeoQueuedFollowUps } = loadApp();
    setCeoQueuedFollowUps("web:test", [
        { id: "local-sent", text: "同一条", runtime_sent_at: "2026-09-21T13:06:01" },
    ]);

    applyCeoState({
        status: "idle",
        queued_follow_up_messages: [
            { content: "同一条", attachments: [], metadata: { _transcript_turn_id: "t-2" } },
        ],
    });

    const merged = getMergedCeoQueuedFollowUps("web:test");
    assert.equal(merged.length, 1);
    assert.equal(merged[0].id, "server:t-2");
    assert.equal((U.ceoFollowUpQueue.innerHTML.match(/同一条/g) || []).length, 1);
});

test("an unsent local draft stays removable beside the server queue", () => {
    const { S, U, applyCeoState, setCeoQueuedFollowUps, getMergedCeoQueuedFollowUps } = loadApp();
    setCeoQueuedFollowUps("web:test", [{ id: "local-unsent", text: "还没发出去的" }]);
    // 会话忙（压缩在途就是这个形状）：空闲快照会触发浏览器自己把未发送的草稿发出去。
    S.ceoSessionBusy = true;

    applyCeoState({
        status: "idle",
        queued_follow_up_messages: [
            { content: "服务端已在排队", attachments: [], metadata: { _transcript_turn_id: "t-3" } },
        ],
    });

    const merged = getMergedCeoQueuedFollowUps("web:test");
    // 不用 deepEqual：app 跑在 vm 沙箱里，沙箱内新建的数组与宿主 Array.prototype 不同域。
    assert.equal(merged.length, 2);
    assert.equal(merged[0].text, "服务端已在排队");
    assert.equal(merged[1].text, "还没发出去的");
    assert.match(U.ceoFollowUpQueue.innerHTML, /data-follow-up-remove="local-unsent"/);
    assert.match(U.ceoFollowUpQueue.innerHTML, /data-follow-up-remove="server:t-3"/);
});

function fakeChip({ id, clientWidth, scrollWidth, expanded = false, hidden = false }) {
    const name = new StubHTMLElement("name");
    name.clientWidth = clientWidth;
    name.scrollWidth = scrollWidth;
    const button = new StubHTMLElement("expand");
    button.hidden = hidden;
    const classes = new Set(expanded ? ["is-expanded"] : []);
    const chip = new StubHTMLElement("chip");
    chip.dataset.followUpId = id;
    // 桩 classList 是空实现，这里换成集合，断言才看得见摘类名的动作。
    chip.classList = {
        contains: (token) => classes.has(token),
        add: (token) => classes.add(token),
        remove: (token) => classes.delete(token),
    };
    chip.querySelector = (selector) => {
        if (selector === ".ceo-follow-up-name") return name;
        if (selector === ".ceo-follow-up-expand") return button;
        return null;
    };
    return { chip, button, classes };
}

test("一行放得下的条目收掉三角，放不下的留着", () => {
    const { U, measureCeoFollowUpSingleLine } = loadApp();
    const fits = fakeChip({ id: "short", clientWidth: 320, scrollWidth: 300 });
    const overflows = fakeChip({ id: "long", clientWidth: 320, scrollWidth: 900 });
    U.ceoFollowUpQueue.querySelectorAll = () => [fits.chip, overflows.chip];

    measureCeoFollowUpSingleLine();

    assert.equal(fits.button.hidden, true);
    assert.equal(overflows.button.hidden, false);
});

test("展开着的行不参与测量，宽度未知的行不下结论", () => {
    const { U, measureCeoFollowUpSingleLine } = loadApp();
    const expanded = fakeChip({ id: "open", clientWidth: 320, scrollWidth: 120, expanded: true, hidden: false });
    const unmeasured = fakeChip({ id: "hidden-panel", clientWidth: 0, scrollWidth: 0, hidden: false });
    U.ceoFollowUpQueue.querySelectorAll = () => [expanded.chip, unmeasured.chip];

    measureCeoFollowUpSingleLine();

    // 展开态的正文盒是换行排版的，量出来永远"放得下"，按这个结论收就会点开即塌。
    assert.equal(expanded.button.hidden, false);
    assert.equal(expanded.classes.has("is-expanded"), true);
    assert.equal(unmeasured.button.hidden, false);
});

test("量出放得下之后，重画不再画那颗三角", () => {
    const { U, setCeoQueuedFollowUps, measureCeoFollowUpSingleLine } = loadApp();
    U.ceoFollowUpQueue.querySelectorAll = () => [
        fakeChip({ id: "draft", clientWidth: 320, scrollWidth: 300 }).chip,
    ];
    setCeoQueuedFollowUps("web:test", [{ id: "draft", text: "一句话的补充" }]);
    assert.equal(/data-follow-up-expand="draft"/.test(U.ceoFollowUpQueue.innerHTML), true);

    measureCeoFollowUpSingleLine();
    U.ceoFollowUpQueue.querySelectorAll = () => [];
    setCeoQueuedFollowUps("web:test", [{ id: "draft", text: "一句话的补充" }]);

    const expandTag = U.ceoFollowUpQueue.innerHTML.match(/<button[^>]*ceo-follow-up-expand[^>]*>/)?.[0] || "";
    assert.match(expandTag, /hidden/);
});

test("409 撤下失败被当成退场信号：条目摘掉、提示是 info", async () => {
    const api = loadApp();
    const { U, applyCeoState, withdrawCeoQueuedFollowUp, __context } = api;
    applyCeoState({
        status: "idle",
        queued_follow_up_messages: [
            { content: "已被本轮接走的补充", attachments: [], metadata: { _transcript_turn_id: "t-7" } },
        ],
    });
    // 先证条目真的画出来了：回合运行中服务端会隐藏 pending 行，用 idle 帧才有候选条。
    assert.match(U.ceoFollowUpQueue.innerHTML, /已被本轮接走的补充/);
    // app.js 顶层的 function showToast 会盖掉沙箱里的桩，所以观察点要装在 vm 的全局上。
    const toasts = [];
    __context.showToast = (payload) => { toasts.push(payload); };
    __context.ApiClient = {
        ...__context.ApiClient,
        withdrawCeoQueuedFollowUp: async () => {
            const error = new Error("HTTP 409");
            error.status = 409;
            error.code = "follow_up_not_queued";
            throw error;
        },
    };

    assert.equal(await withdrawCeoQueuedFollowUp("server:t-7"), false);

    // 服务端说"这条已不在队列"，条目就不该继续占位等人再点一次同样的错。
    assert.equal(U.ceoFollowUpQueue.hidden, true);
    assert.equal(toasts.length, 1);
    assert.equal(toasts[0].kind, "info");
    assert.match(toasts[0].title, /已被本轮接走/);
});

test("一帧说没在跑，但本地回合未收尾且帧里还有未了结的活 ⇒ 不许放行派发", () => {
    const { S, applyCeoState } = loadApp();
    // 直接摆一个未收尾的本地回合（reply.final 还没到），不依赖建泡桩的形状。
    S.ceoPendingTurns = [{ source: "user", turnId: "t-live" }];
    S.ceoTurnActive = true;

    applyCeoState({ status: "idle", is_running: false, source: "user", turn_id: "t-live", pending_tool_calls: ["call-1"] });
    assert.equal(S.ceoTurnActive, true);

    // 帧里什么都没有了：这就是服务端自己说"没事了"，照常放行。
    applyCeoState({ status: "idle", is_running: false, source: "user", turn_id: "t-live" });
    assert.equal(S.ceoTurnActive, false);
});
