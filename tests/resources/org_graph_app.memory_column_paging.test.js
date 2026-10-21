const test = require("node:test");
const assert = require("node:assert/strict");
const vm = require("node:vm");
const { readFrontendSources } = require("./support/frontend_sources.js");

// 记忆页三列的「加载更多」边界：请求的 limit/offset、追加而不是替换、
// total/hasMore 以服务端为准、忙碌与已到底不发请求、失败时列表保持不变。

const APP_CODE = readFrontendSources(["app"]).join("\n");

class StubElement {}
class StubHTMLElement extends StubElement {
    constructor() {
        super();
        this.hidden = false;
        this.disabled = false;
        this.textContent = "";
        this.innerHTML = "";
        this.dataset = {};
        this.classList = { add() {}, remove() {}, contains: () => false, toggle() {} };
        this.style = {};
    }
    setAttribute() {}
    querySelector() { return null; }
    querySelectorAll() { return []; }
    appendChild(child) { return child; }
    addEventListener() {}
}

function baseContext() {
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
            body: new StubHTMLElement(),
        },
        Element: StubElement,
        HTMLElement: StubHTMLElement,
        URLSearchParams,
        URL,
        AbortController,
        fetch: async () => ({ ok: true, json: async () => ({}) }),
        lucide: { createIcons() {} },
        ApiClient: {},
        marked: { parse: (value) => String(value) },
        DOMPurify: { sanitize: (value) => String(value) },
        performance: { now: () => 0 },
        requestAnimationFrame: (callback) => { callback(); return 1; },
        cancelAnimationFrame: () => {},
        WebSocket: function WebSocket() {},
        addEventListener() {},
        removeEventListener() {},
    };
    return context;
}

function loadMemoryPaging(context) {
    context.window = context;
    vm.createContext(context);
    vm.runInContext(
        `${APP_CODE}\nthis.__testExports = {
            S,
            ApiClient,
            loadMoreMemoryQueue,
            loadMoreMemoryProcessed,
            loadMoreMemoryFailed,
            pushedToasts: () => __toasts,
        };`,
        context,
    );
    context.__toasts = [];
    vm.runInContext(`showToast = (payload) => { __toasts.push(payload); };`, context);
    return context.__testExports;
}

const LANES = [
    {
        name: "queue",
        load: "loadMoreMemoryQueue",
        method: "getMemoryQueue",
        items: "memoryQueueItems",
        total: "memoryQueueTotal",
        hasMore: "memoryQueueHasMore",
        pageSize: "memoryQueuePageSize",
        existing: { request_id: "q1" },
        incoming: { request_id: "q2" },
        errorText: "记忆队列加载失败",
    },
    {
        name: "processed",
        load: "loadMoreMemoryProcessed",
        method: "getMemoryProcessed",
        items: "memoryProcessedItems",
        total: "memoryProcessedTotal",
        hasMore: "memoryProcessedHasMore",
        pageSize: "memoryProcessedPageSize",
        existing: { batch_id: "b1" },
        incoming: { batch_id: "b2" },
        errorText: "已处理记忆加载失败",
    },
    {
        name: "failed",
        load: "loadMoreMemoryFailed",
        method: "getMemoryFailed",
        items: "memoryFailedItems",
        total: "memoryFailedTotal",
        hasMore: "memoryFailedHasMore",
        pageSize: "memoryFailedPageSize",
        existing: { failed_id: "f1" },
        incoming: { failed_id: "f2" },
        errorText: "失败记忆加载失败",
    },
];

for (const lane of LANES) {
    test(`${lane.name} load-more appends the next page from the current length`, async () => {
        const context = baseContext();
        const api = loadMemoryPaging(context);
        const calls = [];
        api.ApiClient[lane.method] = async (options) => {
            calls.push(options);
            return { items: [lane.incoming], total: 41, hasMore: false };
        };
        api.S[lane.items] = [lane.existing];
        api.S[lane.total] = 21;
        api.S[lane.hasMore] = true;
        api.S[lane.pageSize] = 20;
        api.S.memoryBusy = false;

        await api[lane.load]();

        assert.equal(calls.length, 1);
        assert.equal(calls[0].limit, 20);
        assert.equal(calls[0].offset, 1);
        assert.equal(api.S[lane.items].length, 2);
        assert.equal(api.S[lane.items][0], lane.existing);
        assert.equal(api.S[lane.items][1], lane.incoming);
        assert.equal(api.S[lane.total], 41);
        assert.equal(api.S[lane.hasMore], false);
        assert.equal(api.S.memoryBusy, false);
    });

    test(`${lane.name} load-more is refused while busy or at the end`, async () => {
        const context = baseContext();
        const api = loadMemoryPaging(context);
        let calls = 0;
        api.ApiClient[lane.method] = async () => {
            calls += 1;
            return { items: [lane.incoming], total: 41, hasMore: false };
        };
        api.S[lane.items] = [lane.existing];
        api.S[lane.hasMore] = true;
        api.S.memoryBusy = true;
        await api[lane.load]();
        assert.equal(calls, 0, "busy must not fetch");

        api.S.memoryBusy = false;
        api.S[lane.hasMore] = false;
        await api[lane.load]();
        assert.equal(calls, 0, "exhausted lane must not fetch");
    });

    test(`${lane.name} load-more keeps the list and reports the error`, async () => {
        const context = baseContext();
        const api = loadMemoryPaging(context);
        api.ApiClient[lane.method] = async () => {
            throw new Error("network down");
        };
        api.S[lane.items] = [lane.existing];
        api.S[lane.total] = 21;
        api.S[lane.hasMore] = true;
        api.S[lane.pageSize] = 20;
        api.S.memoryBusy = false;

        await api[lane.load]();

        assert.equal(api.S[lane.items].length, 1);
        assert.equal(api.S[lane.items][0], lane.existing);
        assert.equal(api.S[lane.total], 21);
        assert.equal(api.S[lane.hasMore], true);
        assert.equal(api.S.memoryBusy, false);
        const toasts = api.pushedToasts();
        assert.equal(toasts.length, 1);
        assert.equal(toasts[0].title, "加载失败");
        assert.equal(toasts[0].text, "network down");
        assert.equal(toasts[0].kind, "error");
    });
}
