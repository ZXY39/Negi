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
            getMergedCeoQueuedFollowUps,
            renderQueuedCeoFollowUps,
            flushCeoQueuedFollowUp,
            withdrawCeoQueuedFollowUp,
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

test("chip offers 立即发送 only for unsent items while a turn runs", () => {
    const { S, U, setCeoQueuedFollowUps } = loadApp();
    S.ceoTurnActive = true;
    setCeoQueuedFollowUps("web:test", [{ id: "draft", text: "还没发出去的补充" }]);

    assert.match(U.ceoFollowUpQueue.innerHTML, /data-follow-up-flush="draft"/);
    assert.match(U.ceoFollowUpQueue.innerHTML, /data-follow-up-withdraw="draft"/);
    assert.match(U.ceoFollowUpQueue.innerHTML, /data-follow-up-remove="draft"/);

    // 回合结束后没有"下一轮"可并，立即发送按钮不再出现，撤回与丢弃留着。
    S.ceoTurnActive = false;
    setCeoQueuedFollowUps("web:test", [{ id: "draft", text: "还没发出去的补充" }]);
    assert.doesNotMatch(U.ceoFollowUpQueue.innerHTML, /data-follow-up-flush/);
    assert.match(U.ceoFollowUpQueue.innerHTML, /data-follow-up-withdraw="draft"/);
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
    assert.match(U.ceoFollowUpQueue.innerHTML, /已受理/);
    assert.doesNotMatch(U.ceoFollowUpQueue.innerHTML, /data-follow-up-remove/);
    const merged = getMergedCeoQueuedFollowUps("web:test");
    assert.equal(merged.length, 1);
    assert.equal(merged[0].accepted_by_runtime, true);
    assert.equal(merged[0].id, "server:t-1");
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
    assert.doesNotMatch(U.ceoFollowUpQueue.innerHTML, /data-follow-up-remove="server:t-3"/);
});
