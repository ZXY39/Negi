const test = require("node:test");
const assert = require("node:assert/strict");
const vm = require("node:vm");
const { afterEach } = require("node:test");
const { readFrontendSources } = require("./support/frontend_sources.js");
const {
    StubElement,
    StubHTMLElement,
    StubDocument,
    StubHTMLButtonElement,
    StubHTMLInputElement,
    StubHTMLTextAreaElement,
    StubHTMLSelectElement,
} = require("./support/frontend_stub.js");

// 节点详情消息列表的渲染窗口合同：
// ① 默认只铺前 500 条（列表新→旧，即最新 500 条），标题计数仍是全量条数；
// ② 窗口之外更旧的消息由列表底部「加载更多消息（还剩 N 条）」按 500 条一页放出；
// ③ 窗口读数按 任务/节点/收发方向 各自记账，切 tab 互不传染；
// ④ 任务暂停时抽屉状态胶囊用 data-paused 换灰，运行令牌本身不动。

const TASK_VIEW_CODE = readFrontendSources(["task_view"]).join("\n");
const APP_CODE = readFrontendSources(["app"]).join("\n");
const TASKS_CODE = readFrontendSources(["tasks"]).join("\n");

const PAGE_SIZE = 500;

class MessageHost extends StubHTMLElement {
    querySelector(selector) {
        const needle = String(selector || "").replace(/^\./, "");
        const walk = (node) => {
            for (const child of node.children || []) {
                if (String(child.className || "").split(/\s+/).includes(needle)) return child;
                const nested = walk(child);
                if (nested) return nested;
            }
            return null;
        };
        return walk(this);
    }
}

class ClickTarget extends StubHTMLElement {
    closest(selector) {
        return String(selector || "") === "[data-message-more]" ? this : null;
    }
}

let activeApi = null;
afterEach(() => {
    // renderMessageList 会挂 chatbox 回投轮询（5s 自续一拍），不摘掉进程退不出去。
    activeApi?.stopChatboxReplyPoll();
    activeApi = null;
});

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
        requestAnimationFrame: (callback) => { callback(); return 1; },
        cancelAnimationFrame: () => {},
        WebSocket: function WebSocket() {},
        addEventListener() {},
        removeEventListener() {},
        ApiClient: new Proxy({}, { get: () => async () => null }),
    };
    context.window = context;
    vm.createContext(context);
    vm.runInContext(
        `${TASK_VIEW_CODE}\n${APP_CODE}\n${TASKS_CODE}\nthis.__x = { renderMessageList, loadMoreTaskMessages, setTaskMessageViewMode, renderTaskNodeDetailStatus, stopChatboxReplyPoll, S, U };`,
        context,
    );
    const api = context.__x;
    api.U.adMessages = new MessageHost("DIV");
    api.U.adMessagesHeading = new StubHTMLElement("H4");
    api.U.adStatus = new StubHTMLElement("SPAN");
    api.S.currentTaskId = "task:window";
    api.S.selectedNodeId = "node:msg";
    activeApi = api;
    return api;
}

function buildMessages(count, { prefix = "notif", from = "node:parent" } = {}) {
    return Array.from({ length: count }, (_unused, index) => ({
        notification_id: `${prefix}:${index}`,
        source_node_id: from,
        message: `消息 ${index}`,
        received_at: `2026-11-20T10:00:${String(index % 60).padStart(2, "0")}+08:00`,
        status: "pending",
    }));
}

function buildSentMessages(count) {
    return Array.from({ length: count }, (_unused, index) => ({
        notification_id: `sent:${index}`,
        target_node_id: "node:child",
        message: `发出 ${index}`,
        received_at: `2026-11-20T10:00:${String(index % 60).padStart(2, "0")}+08:00`,
        status: "pending",
    }));
}

function renderInbox(api, { inbox = 0, outbox = 0 } = {}) {
    const node = {
        node_id: "node:msg",
        state: "in_progress",
        message_list: buildMessages(inbox),
        sent_message_list: buildSentMessages(outbox),
    };
    api.S.currentNodeDetail = node;
    api.renderMessageList(node, { viewState: { messageScrollTop: 0, messageItems: [] } });
    return node;
}

function listHTML(api) {
    const list = api.U.adMessages.querySelector(".task-trace-list");
    assert.ok(list, "消息列表容器必须已渲染");
    return list.innerHTML;
}

function countRows(html, keyPrefix) {
    return (html.match(new RegExp(`data-trace-key="${keyPrefix}`, "g")) || []).length;
}

function clickLoadMore(api) {
    const list = api.U.adMessages.querySelector(".task-trace-list");
    const target = new ClickTarget("BUTTON");
    target.dataset.messageMore = "inbox";
    list.dispatchEvent({
        type: "click",
        target,
        preventDefault() {},
        stopPropagation() {},
    });
}

test("超过 500 条时默认只铺最新 500 条，底部给出加载更多入口", () => {
    const api = loadApp();
    renderInbox(api, { inbox: 600 });

    const html = listHTML(api);
    assert.equal(countRows(html, "message:"), PAGE_SIZE);
    assert.match(html, /data-message-more="inbox"/);
    assert.match(html, /还剩 100 条/);
    // 入口在已铺开的窗口之后（列表新→旧，更旧的在底部）。
    assert.ok(html.indexOf("data-message-more") > html.lastIndexOf("data-trace-key=\"message:"));
    // 标题计数是这条方向的全量条数，不是窗口条数。
    assert.match(api.U.adMessagesHeading.innerHTML, />600</);
});

test("点「加载更多消息」放出一整页，翻到头后入口消失", () => {
    const api = loadApp();
    renderInbox(api, { inbox: 600 });

    clickLoadMore(api);

    const html = listHTML(api);
    assert.equal(countRows(html, "message:"), 600);
    assert.ok(!html.includes("data-message-more"));
    // 窗口账本在 vm realm 里造：按键取值再比，别跨 realm 比原型。
    assert.equal(api.S.taskMessageWindows["task:window:node:msg:inbox"], 1);
    assert.match(api.U.adMessagesHeading.innerHTML, />600</);
});

test("不足一页时不画入口", () => {
    const api = loadApp();
    renderInbox(api, { inbox: 12 });

    const html = listHTML(api);
    assert.equal(countRows(html, "message:"), 12);
    assert.ok(!html.includes("data-message-more"));
    assert.match(api.U.adMessagesHeading.innerHTML, />12</);
});

test("收发两个方向的窗口各自记账，切 tab 互不传染", () => {
    const api = loadApp();
    renderInbox(api, { inbox: 600, outbox: 510 });

    clickLoadMore(api);
    assert.equal(countRows(listHTML(api), "message:"), 600);

    api.setTaskMessageViewMode("outbox");
    const outboxHtml = listHTML(api);
    assert.equal(countRows(outboxHtml, "sent-message:"), PAGE_SIZE);
    assert.match(outboxHtml, /data-message-more="outbox"/);
    assert.match(outboxHtml, /还剩 10 条/);

    api.setTaskMessageViewMode("inbox");
    const inboxHtml = listHTML(api);
    assert.equal(countRows(inboxHtml, "message:"), 600);
    assert.ok(!inboxHtml.includes("data-message-more"));
});

test("任务暂停时抽屉胶囊靠 data-paused 换灰，运行令牌本身不动", () => {
    const api = loadApp();
    api.S.currentTask = { task_id: "task:window", status: "in_progress", is_paused: true };
    const node = { node_id: "node:msg", state: "in_progress" };

    api.renderTaskNodeDetailStatus(node);

    assert.equal(api.U.adStatus.textContent, "任务暂停");
    assert.equal(api.U.adStatus.dataset.paused, "true");
    // 真实状态令牌保留：树卡片与其它读取方仍按 in_progress 走生命周期色。
    assert.equal(api.U.adStatus.dataset.status, "in_progress");
});
