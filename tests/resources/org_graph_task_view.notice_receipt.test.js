const test = require("node:test");
const assert = require("node:assert/strict");
const vm = require("node:vm");
const { readFrontendSources } = require("./support/frontend_sources.js");

const TASK_VIEW_CODE = readFrontendSources(["task_view"]).join("\n");
const APP_CODE = readFrontendSources(["app"]).join("\n");

class StubElement {}
class StubHTMLElement extends StubElement {
    constructor() {
        super();
        this.hidden = false;
        this.textContent = "";
        this.innerHTML = "";
        this.className = "";
        this.dataset = {};
        this.style = {};
        this.attributes = {};
        this.children = [];
        this.parentElement = null;
        this.disabled = false;
        this.classList = {
            add() {},
            remove() {},
            contains() { return false; },
            toggle() { return false; },
        };
    }

    querySelector() { return null; }
    querySelectorAll() { return []; }
    closest() { return null; }
    contains() { return false; }
    addEventListener() {}
    appendChild(child) { return child; }
    setAttribute() {}
    removeAttribute() {}
}
class StubHTMLButtonElement extends StubHTMLElement {}
class StubHTMLInputElement extends StubHTMLElement {}
class StubHTMLTextAreaElement extends StubHTMLElement {}
class StubHTMLSelectElement extends StubHTMLElement {}

class StubDocument {
    getElementById() { return null; }
    createElement() { return new StubHTMLElement(); }
    querySelector() { return null; }
    querySelectorAll() { return []; }
    addEventListener() {}
}

function loadTaskView({ receipt }) {
    const toasts = [];
    const settles = [];
    const chatboxRenders = [];
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
        __toasts: toasts,
        __settles: settles,
        __chatboxRenders: chatboxRenders,
        ApiClient: {
            appendTaskNodeNotice: async () => receipt,
            markNodeChatboxRead: async () => ({ marked: 1 }),
        },
    };
    context.window = context;
    vm.createContext(context);
    vm.runInContext(
        `${TASK_VIEW_CODE}\n${APP_CODE}\nthis.__testExports = { submitNodeNoticeComposer, S, U, renderChatboxReplyChip, chatboxReplyChipForEntry, chatboxReplyState, openNodeChatboxReplies };`,
        context
    );
    vm.runInContext(
        `
        showToast = (payload) => { __toasts.push(payload); };
        loadTaskTreeSnapshot = async () => {};
        renderTree = () => {};
        findTreeNode = () => null;
        showAgent = async () => {};
        renderMessageList = () => { __chatboxRenders.push(1); };
        openMediaViewer = () => {};
        settleTaskNoticeAfterSend = (taskId, nodeId, message) => {
            __settles.push({ taskId, nodeId, message });
        };
        `,
        context
    );
    const { submitNodeNoticeComposer, S, U, renderChatboxReplyChip, chatboxReplyChipForEntry, chatboxReplyState, openNodeChatboxReplies } = context.__testExports;
    U.adNoticeInput = new StubHTMLInputElement();
    U.adNoticeSend = new StubHTMLButtonElement();
    S.currentTaskId = "task:notice";
    S.selectedNodeId = "node:exec";
    U.adNoticeInput.value = "再加一节实测";
    return { submitNodeNoticeComposer, S, U, toasts, settles, chatboxRenders, renderChatboxReplyChip, chatboxReplyChipForEntry, chatboxReplyState, openNodeChatboxReplies };
}

test("waiting_acceptance receipt explains the message is held until a rejection", async () => {
    const app = loadTaskView({
        receipt: {
            status: "delivered",
            target_state: "waiting_acceptance",
            notice: "The target is awaiting acceptance; the message will not be consumed until it is rejected.",
        },
    });

    await app.submitNodeNoticeComposer();

    assert.equal(app.toasts.length, 1);
    assert.equal(app.toasts[0].title, "已进入待处理");
    assert.match(app.toasts[0].text, /等验收/);
    assert.equal(app.toasts[0].kind, "success");
    assert.deepEqual(app.settles, []);
});

test("ordinary delivery still reports success and starts the settle poll", async () => {
    const app = loadTaskView({ receipt: { status: "delivered" } });

    await app.submitNodeNoticeComposer();

    assert.equal(app.toasts.length, 1);
    assert.equal(app.toasts[0].title, "消息已发送");
    assert.equal(app.toasts[0].kind, "success");
    assert.equal(app.settles.length, 1);
    assert.equal(app.settles[0].nodeId, "node:exec");
});

test("deduplicated delivery counts as success", async () => {
    const app = loadTaskView({ receipt: { status: "deduplicated" } });

    await app.submitNodeNoticeComposer();

    assert.equal(app.toasts.length, 1);
    assert.equal(app.toasts[0].kind, "success");
    assert.equal(app.settles.length, 1);
});

test("回投按钮销账后转灰、仍可点开重看", () => {
    const app = loadTaskView({ receipt: { status: "delivered" } });
    app.S.currentNodeDetail = { node_id: "node:exec" };
    app.chatboxReplyState()["task:notice:node:exec"] = {
        "ntf-1": { unread: 2, total: 3 },
    };

    const unreadChip = app.chatboxReplyChipForEntry({ notification_id: "ntf-1" });
    assert.match(unreadChip, /新回复·2/);
    assert.match(unreadChip, /2 条未读/);
    assert.doesNotMatch(unreadChip, /is-read/);

    app.openNodeChatboxReplies("ntf-1");

    const group = app.chatboxReplyState()["task:notice:node:exec"]["ntf-1"];
    assert.equal(group.unread, 0, "点击后本地读数应当场转成已读");
    assert.equal(app.chatboxRenders.length, 1, "转灰要立刻重绘消息列表");

    const readChip = app.chatboxReplyChipForEntry({ notification_id: "ntf-1" });
    assert.match(readChip, /is-read/);
    assert.match(readChip, /回复·3/);
    assert.match(readChip, /共 3 条/);
    assert.match(readChip, /data-chatbox-open="ntf-1"/, "已读按钮仍要能再点开");
});

test("没有回投的消息行不挂按钮", () => {
    const app = loadTaskView({ receipt: { status: "delivered" } });
    app.chatboxReplyState()["task:notice:node:exec"] = {
        "ntf-1": { unread: 0, total: 1 },
    };

    assert.equal(app.chatboxReplyChipForEntry({ notification_id: "ntf-404" }), "");
    assert.match(app.chatboxReplyChipForEntry({ notification_id: "ntf-1" }), /is-read/);
});
