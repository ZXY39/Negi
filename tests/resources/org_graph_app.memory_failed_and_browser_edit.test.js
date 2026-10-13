const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

// 记忆管理:失败停车区卡片(红色 + 重试图标 + 错误历史)与「当前记忆」浏览器编辑模式
// (操作列 / 勾选 / 全选 / mutationsEnabled 门控)的渲染契约。

const APP_PATH = "g3ku/web/frontend/org_graph_app.js";
const APP_CODE = fs.readFileSync(APP_PATH, "utf8");

class StubElement {}
class StubHTMLElement extends StubElement {
    constructor() {
        super();
        this.tagName = "DIV";
        this.className = "";
        this.hidden = false;
        this.disabled = false;
        this.checked = false;
        this.value = "";
        this.open = false;
        this.textContent = "";
        this.innerHTML = "";
        this.title = "";
        this.dataset = {};
        this.attributes = {};
        this.children = [];
        this._qs = {};
        this._qsAll = {};
        this.classList = {
            add: () => {},
            remove: () => {},
            contains: () => false,
            toggle: () => {},
        };
        this.style = {};
    }

    setAttribute(name, value) {
        this.attributes[name] = String(value);
    }

    getAttribute(name) {
        return Object.prototype.hasOwnProperty.call(this.attributes, name) ? this.attributes[name] : null;
    }

    querySelector(selector) {
        return this._qs[selector] || null;
    }

    querySelectorAll(selector) {
        return this._qsAll[selector] || [];
    }

    appendChild(child) {
        this.children.push(child);
        return child;
    }

    // 失败记忆正文的「取全文」是往同一块尾部追加：桩要能记下追加了什么，
    // 否则测不出"整块重绘把滚动位置打回顶部"这类回归。
    closest() {
        return this._closest || null;
    }

    insertAdjacentHTML(_position, html) {
        this.appended = this.appended || [];
        this.appended.push(String(html));
        this.innerHTML += String(html);
    }

    focus() {}
    remove() {}
    addEventListener() {}
}

function baseContext() {
    return {
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
        requestAnimationFrame: (callback) => {
            callback();
            return 1;
        },
        cancelAnimationFrame: () => {},
        WebSocket: function WebSocket() {},
        addEventListener() {},
        removeEventListener() {},
    };
}

function loadApp() {
    const context = baseContext();
    context.window = context;
    vm.createContext(context);
    vm.runInContext(
        `${APP_CODE}\nthis.__testExports = {
            S, U, ApiClient,
            renderMemoryFailedCard,
            memoryFailedCategoryLabel,
            memoryFailedAutoRetryHint,
            memoryFailedErrorHistoryText,
            memoryFailedPayloadBlocksFromRecord,
            memoryFailedPayloadRangeText,
            renderMemoryFailedPayloadBlocks,
            loadMoreMemoryFailedPayload,
            memoryBrowserSelectedIds,
            renderMemoryBrowserList,
            renderMemoryBrowserEditState,
            toggleMemoryBrowserEditMode,
            memoryBrowserToggleRowSelected,
            memoryBrowserToggleSelectAll,
        };`,
        context
    );
    // 屏蔽 toast/DOM 副作用,只测渲染与状态机
    vm.runInContext(`showToast = () => {};`, context);
    return context.__testExports;
}

function stubBrowserUi(api) {
    [
        "memoryBrowserBackdrop",
        "memoryBrowserDrawer",
        "memoryBrowserTbody",
        "memoryBrowserStatus",
        "memoryBrowserSubtitle",
        "memoryBrowserThSelect",
        "memoryBrowserThActions",
        "memoryBrowserBulkBar",
        "memoryBrowserEditToggle",
        "memoryBrowserSelectAll",
        "memoryBrowserSelectedCount",
        "memoryBrowserBulkDelete",
        "memoryBrowserEditHint",
        "memoryBrowserEditDialog",
        "memoryBrowserEditBackdrop",
        "memoryBrowserEditSave",
    ].forEach((key) => {
        api.U[key] = new StubHTMLElement();
    });
}

const FAILED_RECORD = {
    failed_id: "failed_abc123",
    op: "write",
    category: "provider_error",
    status: "parked",
    request_ids: ["write_1", "write_2"],
    items: [
        { request_id: "write_1", payload_text: "第一条载荷" },
        { request_id: "write_2", payload_text: "第二条载荷" },
    ],
    parked_at: "2026-09-15T19:31:10+08:00",
    first_parked_at: "2026-09-15T19:31:10+08:00",
    park_count: 2,
    auto_requeue_count: 1,
    manual_retry_count: 0,
    last_error_text: "RateLimitError: Error code: 429 - rpm exhausted",
    error_history: [
        { at: "2026-09-15T19:31:10+08:00", category: "provider_error", error: "RateLimitError: 429 rpm exhausted", trigger: "initial" },
        { at: "2026-09-15T19:40:00+08:00", event: "requeued", trigger: "auto" },
        { at: "2026-09-15T19:41:00+08:00", category: "provider_error", error: "RateLimitError: 429 again", trigger: "initial" },
    ],
    usage_total: { input_tokens: 0, output_tokens: 0, cache_read_tokens: 0 },
};

test("failed card renders red styling, failed badge, error preview and retry icon", () => {
    const api = loadApp();
    api.S.memoryFailedMutationsEnabled = true;
    api.S.memoryFailedActionBusy = "";

    const html = api.renderMemoryFailedCard(FAILED_RECORD);

    assert.match(html, /memory-card-failed/);
    assert.match(html, /data-status="failed"/);
    assert.match(html, /data-memory-detail-open="failed"/);
    assert.match(html, /data-memory-detail-key="failed_abc123"/);
    assert.match(html, /data-memory-failed-retry="failed_abc123"/);
    assert.match(html, /data-lucide="rotate-ccw"/);
    assert.match(html, /provider 瞬时错误/);
    assert.match(html, /RateLimitError/);
    assert.match(html, /rpm exhausted/);
    assert.match(html, /2 条/);
    assert.doesNotMatch(html, /disabled/);
});

test("failed card retry button is disabled without mutations flag or while busy", () => {
    const api = loadApp();
    api.S.memoryFailedMutationsEnabled = false;
    api.S.memoryFailedActionBusy = "";
    assert.match(api.renderMemoryFailedCard(FAILED_RECORD), /data-memory-failed-retry="failed_abc123"[^>]*disabled/);

    api.S.memoryFailedMutationsEnabled = true;
    api.S.memoryFailedActionBusy = "retry:failed_abc123";
    assert.match(api.renderMemoryFailedCard(FAILED_RECORD), /data-memory-failed-retry="failed_abc123"[^>]*disabled/);
});

test("protocol category card shows manual-only hint", () => {
    const api = loadApp();
    api.S.memoryFailedMutationsEnabled = true;
    const html = api.renderMemoryFailedCard({ ...FAILED_RECORD, category: "protocol", request_ids: ["write_1"], items: [FAILED_RECORD.items[0]] });
    assert.match(html, /协议违规/);
    assert.match(html, /仅支持手动重试/);
    assert.doesNotMatch(html, /2 条/);
});

test("window refusal card shows its own category and a retry precondition hint", () => {
    const api = loadApp();
    api.S.memoryFailedMutationsEnabled = true;
    const html = api.renderMemoryFailedCard({
        ...FAILED_RECORD,
        category: "window_refusal",
        request_ids: ["write_1"],
        items: [FAILED_RECORD.items[0]],
        last_error_text: "memory send exceeds model window: 估算=527077 容量=366000 窗口=390000",
    });
    assert.match(html, /超窗拒发/);
    assert.doesNotMatch(html, /provider 瞬时错误/);
    assert.match(html, /载荷变小或模型窗口调大后手动重试/);
});

test("error history text renders failures and requeue events in order", () => {
    const api = loadApp();
    const text = api.memoryFailedErrorHistoryText(FAILED_RECORD);
    assert.match(text, /#1 \[[^\]]+\] provider 瞬时错误 · 处理失败\nRateLimitError: 429 rpm exhausted/);
    assert.match(text, /#2 \[[^\]]+\] 重新入队（成功信号自动）/);
    assert.match(text, /#3 \[[^\]]+\] provider 瞬时错误 · 处理失败\nRateLimitError: 429 again/);
});

test("browser list renders read-only 6-column rows outside edit mode", () => {
    const api = loadApp();
    stubBrowserUi(api);
    api.S.memoryBrowser.items = [
        { memory_id: "Ab12Z9", memory_body: "第一条记忆", minimal_memory: "a->b", source: "user", created_at: "2026-09-16T10:00:00+08:00", refresh_count: 1, passed_count: 2 },
    ];
    api.S.memoryBrowser.editMode = false;
    api.S.memoryBrowser.mutationsEnabled = false;

    api.renderMemoryBrowserList();
    const html = api.U.memoryBrowserTbody.innerHTML;
    assert.match(html, /colspan="6"|第一条记忆/);
    assert.doesNotMatch(html, /data-memory-row-select/);
    assert.doesNotMatch(html, /data-memory-row-edit/);
});

test("browser list adds select and action columns in edit mode", () => {
    const api = loadApp();
    stubBrowserUi(api);
    api.S.memoryBrowser.items = [
        { memory_id: "Ab12Z9", memory_body: "第一条记忆", minimal_memory: "a->b", source: "user", created_at: "2026-09-16T10:00:00+08:00", refresh_count: 1, passed_count: 2 },
        { memory_id: "Cd34W8", memory_body: "第二条记忆", minimal_memory: "c->d", source: "self", created_at: "2026-09-16T10:01:00+08:00", refresh_count: 0, passed_count: 0 },
    ];
    api.S.memoryBrowser.mutationsEnabled = true;
    api.S.memoryBrowser.editMode = true;
    api.S.memoryBrowser.selected = { Ab12Z9: true };

    api.renderMemoryBrowserList();
    const html = api.U.memoryBrowserTbody.innerHTML;
    assert.match(html, /data-memory-row-select="Ab12Z9"[^>]*checked/);
    assert.match(html, /data-memory-row-select="Cd34W8"/);
    assert.match(html, /data-memory-row-edit="Ab12Z9"/);
    assert.match(html, /data-memory-row-delete="Cd34W8"/);
    assert.match(html, /memory-browser-row-selected/);
    assert.match(html, /修改/);
    assert.match(html, /删除/);
    // 编辑态批量条状态:已选 1 条,删除按钮可用
    assert.equal(api.U.memoryBrowserSelectedCount.textContent, "已选 1 条");
    assert.equal(api.U.memoryBrowserBulkDelete.disabled, false);
    assert.equal(api.U.memoryBrowserThSelect.hidden, false);
    assert.equal(api.U.memoryBrowserThActions.hidden, false);
    assert.equal(api.U.memoryBrowserBulkBar.hidden, false);

    // 全选/取消全选作用于当前筛选结果
    api.memoryBrowserToggleSelectAll(true);
    assert.deepEqual(Object.keys(api.S.memoryBrowser.selected).sort(), ["Ab12Z9", "Cd34W8"]);
    api.memoryBrowserToggleSelectAll(false);
    assert.deepEqual(Object.keys(api.S.memoryBrowser.selected), []);

    // 未知 id 不计入选择集合
    api.memoryBrowserToggleRowSelected("Unknown1", true);
    assert.equal(api.memoryBrowserSelectedIds().length, 0);
});

test("edit mode is refused when mutations are disabled", () => {
    const api = loadApp();
    stubBrowserUi(api);
    api.S.memoryBrowser.mutationsEnabled = false;
    api.S.memoryBrowser.editMode = false;
    api.toggleMemoryBrowserEditMode();
    assert.equal(api.S.memoryBrowser.editMode, false);

    api.S.memoryBrowser.mutationsEnabled = true;
    api.toggleMemoryBrowserEditMode();
    assert.equal(api.S.memoryBrowser.editMode, true);
    api.S.memoryBrowser.selected = { Ab12Z9: true };
    api.toggleMemoryBrowserEditMode();
    assert.equal(api.S.memoryBrowser.editMode, false);
    assert.deepEqual(Object.keys(api.S.memoryBrowser.selected), []);
});

test("failed drawer body carries only the projected first screen", () => {
    const api = loadApp();
    const blocks = api.memoryFailedPayloadBlocksFromRecord({
        items: [
            { request_id: "write_1", payload_text: "x".repeat(4000), payload_char_count: 50000, payload_truncated: true },
            { request_id: "write_2", payload_text: "短载荷", payload_char_count: 3, payload_truncated: false },
        ],
    });
    assert.equal(blocks.length, 2);
    assert.equal(blocks[0].endChar, 4000);
    assert.equal(blocks[0].charCount, 50000);
    assert.equal(blocks[0].truncated, true);
    assert.equal(blocks[1].truncated, false);

    const html = api.renderMemoryFailedPayloadBlocks(blocks);
    assert.match(html, /data-memory-payload-more/);
    assert.match(html, /已显示第 1–4000 字符 \/ 共 50000 字符/);
    assert.match(html, /全文 3 字符已全部显示/);
    // 回归钉子：抽屉曾经把 1.28MB 正文整份拼成一个字符串再注入，主线程被占住 ~2.9 秒。
    assert.ok(html.length < 8000, `首屏渲染带回了整份载荷：${html.length} 字符`);
});

test("payload paging appends by delivered chars and hides the button once exhausted", async () => {
    const api = loadApp();
    api.S.memoryDetailPreview = { open: true, kind: "failed", key: "failed_paged" };
    const calls = [];
    api.ApiClient.getMemoryFailedPayload = async ({ startChar }) => {
        calls.push(startChar);
        const end = Math.min(Number(startChar) + 19999, 50000);
        return { item: { text: "y".repeat(end - Number(startChar) + 1), start_char: Number(startChar), end_char: end, char_count: 50000, truncated: end < 50000 } };
    };
    const textNode = new StubHTMLElement();
    const rangeNode = new StubHTMLElement();
    const item = new StubHTMLElement();
    item.dataset = { requestId: "write_1", startChar: "1", endChar: "4000", charCount: "50000" };
    item._qs = { ".memory-detail-payload-text": textNode, ".memory-detail-payload-range": rangeNode };
    const button = new StubHTMLElement();
    button._closest = item;

    await api.loadMoreMemoryFailedPayload(button);
    assert.deepEqual(calls, [4001], "续读起点必须取自已交付的 end_char+1");
    assert.equal(item.dataset.endChar, "24000");
    assert.equal(textNode.innerHTML.length, 20000);
    assert.match(rangeNode.textContent, /已显示第 1–24000 字符 \/ 共 50000 字符/);
    assert.equal(button.hidden, false);

    await api.loadMoreMemoryFailedPayload(button);
    assert.deepEqual(calls, [4001, 24001]);
    assert.equal(item.dataset.endChar, "44000");
    assert.equal(textNode.innerHTML.length, 40000, "追加不许重绘已有正文");

    await api.loadMoreMemoryFailedPayload(button);
    assert.equal(item.dataset.endChar, "50000");
    assert.equal(textNode.innerHTML.length, 46000);
    assert.match(rangeNode.textContent, /全文 50000 字符已全部显示/);
    assert.equal(button.hidden, true, "取尽后取全文按钮要收口");
});

test("payload paging closes the button when the server delivers no progress", async () => {
    const api = loadApp();
    api.S.memoryDetailPreview = { open: true, kind: "failed", key: "failed_stalled" };
    api.ApiClient.getMemoryFailedPayload = async ({ startChar }) => ({
        item: { text: "same", start_char: Number(startChar), end_char: Number(startChar) - 1, char_count: 50000, truncated: true },
    });
    const textNode = new StubHTMLElement();
    const rangeNode = new StubHTMLElement();
    const item = new StubHTMLElement();
    item.dataset = { requestId: "write_1", startChar: "1", endChar: "4000", charCount: "50000" };
    item._qs = { ".memory-detail-payload-text": textNode, ".memory-detail-payload-range": rangeNode };
    const button = new StubHTMLElement();
    button._closest = item;

    await api.loadMoreMemoryFailedPayload(button);
    assert.equal(item.dataset.endChar, "4000", "没推进就不动已交付水位");
    assert.equal(button.hidden, true, "再点只会拿回同一段，收口而不是留个死按钮");
    assert.equal(button.disabled, false, "按钮收口后不能留在禁用态");
});
