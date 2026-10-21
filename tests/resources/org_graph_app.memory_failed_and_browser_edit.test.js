const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const { readFrontendSources } = require("./support/frontend_sources.js");

// 记忆管理:失败停车区卡片(红色 + 重试图标 + 错误历史)与「当前记忆」浏览器编辑模式
// (操作列 / 勾选 / 全选 / mutationsEnabled 门控)的渲染契约。

const APP_PATH = "g3ku/web/frontend/org_graph_app.js";
const APP_CODE = readFrontendSources(["app"]).join("\n");

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
            memoryProcessedOpKinds,
            memoryProcessedStatusLabel,
            memoryProcessedIsNoChange,
            memoryProcessedNoopReason,
            renderMemoryChangeBlock,
            memoryFailedErrorHistoryText,
            memoryPayloadBlocksFromRecord,
            memoryPayloadRangeText,
            renderMemoryPayloadBlocks,
            loadMoreMemoryPayloadBlock,
            memoryBrowserSelectedIds,
            renderMemoryBrowserList,
            renderMemoryBrowserEditState,
            toggleMemoryBrowserEditMode,
            memoryBrowserToggleRowSelected,
            memoryBrowserToggleSelectAll,
            runMemoryBrowserUncompress,
            runMemoryBrowserDelete,
            runMemoryBrowserEditSave,
            pushedToasts: () => __toasts,
            resetToasts: () => { __toasts.length = 0; },
            globalRef: () => globalThis,
        };`,
        context
    );
    // 屏蔽 toast/DOM 副作用,只测渲染与状态机
    context.__toasts = [];
    vm.runInContext(`showToast = (payload) => { __toasts.push(payload); };`, context);
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

test("browser row shows note ownership from the column, not from projected text", () => {
    const api = loadApp();
    stubBrowserUi(api);
    api.S.memoryBrowser.items = [
        { memory_id: "Ab12Z9", memory_body: "第一条记忆 ref:note_legacy", minimal_memory: "a->b", source: "user", created_at: "2026-09-16T10:00:00+08:00", refresh_count: 1, passed_count: 2, note_ref: "note_ports" },
        { memory_id: "Ef56Gh", memory_body: "没有 note 的记忆", minimal_memory: "e->f", source: "self", created_at: "2026-09-16T10:00:00+08:00", refresh_count: 0, passed_count: 0 },
    ];
    api.S.memoryBrowser.editMode = false;
    api.S.memoryBrowser.mutationsEnabled = true;

    api.renderMemoryBrowserList();
    const html = api.U.memoryBrowserTbody.innerHTML;
    // 归属 chip 只来自 `note_ref` 列；退场的行根本不进这张表（列表端已过滤），
    // 所以这里不再拿 consolidated_into 兜底，否则等于把已迁走的记忆又画回常驻面。
    assert.match(html, /data-memory-note-ref="note_ports"[^>]*>note:note_ports</);
    assert.equal((html.match(/memory-note-ownership-chip/g) || []).length, 1);
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

test("memory drawer body carries only the projected first screen, per request block", () => {
    const api = loadApp();
    const blocks = api.memoryPayloadBlocksFromRecord({
        items: [
            { request_id: "write_1", payload_text: "x".repeat(4000), payload_char_count: 50000 },
            { request_id: "write_2", payload_text: "短载荷", payload_char_count: 3 },
        ],
    }, "failed");
    assert.equal(blocks.length, 2);
    assert.equal(blocks[0].endChar, 4000);
    assert.equal(blocks[0].charCount, 50000);
    assert.equal(blocks[0].truncated, true);
    assert.equal(blocks[1].truncated, false);

    const html = api.renderMemoryPayloadBlocks(blocks, "failed", "failed_abc123");
    assert.match(html, /data-payload-kind="failed"/);
    assert.match(html, /data-payload-key="failed_abc123"/);
    assert.match(html, /data-memory-payload-more/);
    assert.match(html, /已显示第 1–4000 字符 \/ 共 50000 字符/);
    assert.match(html, /全文 3 字符已全部显示/);
    // 回归钉子：抽屉曾经把整份正文拼成一个字符串再注入，1.28MB 时主线程被占住 ~2.9 秒。
    assert.ok(html.length < 8000, `首屏渲染带回了整份载荷：${html.length} 字符`);
});

test("processed and queue records project from their own payload shapes", () => {
    const api = loadApp();
    // 已处理批次：payload_texts 与 payload_char_counts 同下标对齐，request_ids 提供寻址。
    const processed = api.memoryPayloadBlocksFromRecord({
        batch_id: "write_batch_1",
        request_ids: ["write_a", "write_b"],
        payload_texts: ["a".repeat(4000), "第二条正文"],
        payload_char_counts: [40000, 5],
    }, "processed");
    assert.deepEqual(processed.map((b) => b.requestId), ["write_a", "write_b"]);
    assert.equal(processed[0].truncated, true);
    assert.equal(processed[1].truncated, false);
    assert.equal(processed[1].charCount, 5);

    // 队列行只有一份正文。
    const queued = api.memoryPayloadBlocksFromRecord({
        request_id: "write_q",
        payload_text: "z".repeat(4000),
        payload_char_count: 9000,
    }, "queue");
    assert.equal(queued.length, 1);
    assert.equal(queued[0].requestId, "write_q");
    assert.equal(queued[0].truncated, true);

    // 旧服务端没给 counts 时按交付长度判：不截断、不出按钮，也不报错。
    const legacy = api.memoryPayloadBlocksFromRecord({ payload_texts: ["短"], request_ids: ["w"] }, "processed");
    assert.equal(legacy[0].truncated, false);
});

test("payload paging appends by delivered chars across surfaces and hides the button once exhausted", async () => {
    const api = loadApp();
    const calls = [];
    api.ApiClient.getMemoryPayload = async ({ kind, key, requestId, startChar }) => {
        calls.push([kind, key, requestId, startChar]);
        const end = Math.min(Number(startChar) + 19999, 50000);
        return { item: { kind, key, request_id: requestId, text: "y".repeat(end - Number(startChar) + 1), start_char: Number(startChar), end_char: end, char_count: 50000, truncated: end < 50000 } };
    };
    const textNode = new StubHTMLElement();
    const rangeNode = new StubHTMLElement();
    const item = new StubHTMLElement();
    item.dataset = { payloadKind: "processed", payloadKey: "write_batch_1", requestId: "write_a", startChar: "1", endChar: "4000", charCount: "50000" };
    item._qs = { ".memory-detail-payload-text": textNode, ".memory-detail-payload-range": rangeNode };
    const button = new StubHTMLElement();
    button._closest = item;

    await api.loadMoreMemoryPayloadBlock(button);
    assert.deepEqual(calls[0], ["processed", "write_batch_1", "write_a", 4001], "续读起点取自已交付的 end_char+1");
    assert.equal(item.dataset.endChar, "24000");
    assert.equal(textNode.innerHTML.length, 20000);
    assert.match(rangeNode.textContent, /已显示第 1–24000 字符 \/ 共 50000 字符/);
    assert.equal(button.hidden, false);

    await api.loadMoreMemoryPayloadBlock(button);
    assert.equal(item.dataset.endChar, "44000");
    assert.equal(textNode.innerHTML.length, 40000, "追加不许重绘已有正文");

    await api.loadMoreMemoryPayloadBlock(button);
    assert.equal(item.dataset.endChar, "50000");
    assert.match(rangeNode.textContent, /全文 50000 字符已全部显示/);
    assert.equal(button.hidden, true, "取尽后取全文按钮要收口");
});

test("payload paging closes the button when the server delivers no progress", async () => {
    const api = loadApp();
    api.ApiClient.getMemoryPayload = async ({ startChar }) => ({
        item: { text: "same", start_char: Number(startChar), end_char: Number(startChar) - 1, char_count: 50000, truncated: true },
    });
    const textNode = new StubHTMLElement();
    const rangeNode = new StubHTMLElement();
    const item = new StubHTMLElement();
    item.dataset = { payloadKind: "failed", payloadKey: "failed_stalled", requestId: "write_1", startChar: "1", endChar: "4000", charCount: "50000" };
    item._qs = { ".memory-detail-payload-text": textNode, ".memory-detail-payload-range": rangeNode };
    const button = new StubHTMLElement();
    button._closest = item;

    await api.loadMoreMemoryPayloadBlock(button);
    assert.equal(item.dataset.endChar, "4000", "没推进就不动已交付水位");
    assert.equal(button.hidden, true, "再点只会拿回同一段，收口而不是留个死按钮");
    assert.equal(button.disabled, false, "按钮收口后不能留在禁用态");
});

test("a downgraded row says so on the face of the table and offers warming", () => {
    const api = loadApp();
    stubBrowserUi(api);
    api.S.memoryBrowser.items = [
        { memory_id: "Ab12Z9", memory_body: "全文还是这条", minimal_memory: "a->b", source: "user", created_at: "2026-09-16T10:00:00+08:00", is_compressed: true },
        { memory_id: "Cd34W8", memory_body: "这条没被压过", minimal_memory: "c->d", source: "self", created_at: "2026-09-16T10:01:00+08:00", is_compressed: false },
    ];
    api.S.memoryBrowser.editMode = true;
    api.S.memoryBrowser.mutationsEnabled = true;

    api.renderMemoryBrowserList();
    const html = api.U.memoryBrowserTbody.innerHTML;
    // 表体显示的永远是 `memory_body` 全文；这颗标记才是"上下文里现在只剩摘要"。
    assert.match(html, /memory-downgraded-chip/);
    assert.match(html, /上下文里只显示摘要/);
    assert.equal((html.match(/memory-downgraded-chip/g) || []).length, 1, "没被降级的行不许画这颗");
    assert.match(html, /data-memory-row-uncompress="Ab12Z9"/);
    assert.doesNotMatch(html, /data-memory-row-uncompress="Cd34W8"/, "回暖只对降级的行有意义");
});

test("warming calls the operator route, reports the bytes it put back, then reloads", async () => {
    const api = loadApp();
    stubBrowserUi(api);
    api.S.memoryBrowser.items = [];
    api.S.memoryBrowser.editMode = true;
    const calls = [];
    api.ApiClient.uncompressCurrentMemory = async (memoryId) => {
        calls.push(memoryId);
        return { ok: true, item: { memory_id: memoryId, changed: true, resident_chars_after: 14210, compress_trigger_chars: 16000, document_max_chars: 20000 } };
    };
    let reloaded = 0;
    api.globalRef().loadMemoryBrowser = async () => { reloaded += 1; };

    await api.runMemoryBrowserUncompress("Ab12Z9");

    assert.deepEqual(calls, ["Ab12Z9"]);
    assert.equal(reloaded, 1, "成功后必须把表重画，否则那颗标记还留在行上");
    const toast = api.pushedToasts().at(-1);
    assert.equal(toast.title, "已回暖");
    assert.match(toast.text, /14210/);
    assert.match(toast.text, /16000/);
    assert.equal(api.S.memoryBrowser.actionBusy, "", "按钮要回到可用态");

    // 顶到硬顶被拒：失败侧同样要有可读落点，不能只把错误吞掉。
    api.ApiClient.uncompressCurrentMemory = async () => { throw new Error("memory document exceeds 20000 chars"); };
    await api.runMemoryBrowserUncompress("Ef56Gh");
    const failed = api.pushedToasts().at(-1);
    assert.equal(failed.kind, "error");
    assert.match(failed.text, /exceeds 20000/);
});

test("the memory browser never promises a synchronous save", () => {
    // 编辑与删除都只入队：文案再说"已保存/镜像已同步"就是假承诺。
    assert.doesNotMatch(APP_CODE, /记忆修改已保存/);
    assert.doesNotMatch(APP_CODE, /已删除 \$\{deletedCount\} 条记忆，镜像已同步/);
    assert.match(APP_CODE, /已提交修改/);
    assert.match(APP_CODE, /已提交删除/);
    assert.match(APP_CODE, /交给记忆 agent/);
});

test("the capacity rungs read as their own outcomes, not as generic failures", () => {
    const api = loadApp();
    assert.equal(api.memoryFailedCategoryLabel({ category: "capacity" }), "库容不下");
    assert.equal(api.memoryFailedCategoryLabel({ category: "capacity_noop" }), "归并未受理");
    // 两条都必须带"下一步做什么"：停车记录的解释就是操作员的出口。
    assert.match(api.memoryFailedAutoRetryHint({ category: "capacity" }), /原文完整保留/);
    assert.match(api.memoryFailedAutoRetryHint({ category: "capacity_noop" }), /放弃.*重新允许/);
    assert.equal(api.memoryFailedCategoryLabel({ category: "provider_error" }), "provider 瞬时错误");
    // 确定性写故障不许借 provider 那一档：那一档会被下一次成功重排回同一个坏载荷。
    assert.equal(api.memoryFailedCategoryLabel({ category: "agent_fault" }), "写入形状故障");
    assert.match(api.memoryFailedAutoRetryHint({ category: "agent_fault" }), /不随队列成功自动重排/);
});

test("a machine downgrade is its own change chip, not a rewrite", () => {
    const api = loadApp();
    const item = {
        status: "applied",
        changes: [{ type: "compress_for_capacity", memory_id: "Ab12Z9", freed_chars: 61 }],
    };
    const html = api.renderMemoryChangeBlock({ type: "compress_for_capacity", memory_id: "Ab12Z9", freed_chars: 61 });
    assert.match(html, /容量降级/);
    const kinds = api.memoryProcessedOpKinds(item);
    // vm 里产出的数组带着那个 realm 的 Array 原型，deepStrictEqual 会因原型不同而判不等。
    assert.equal(kinds.join("|"), "compress_for_capacity", "机器动作不能被并进「修改」那颗");
    assert.match(APP_CODE, /data-op="compress_for_capacity"|compress_for_capacity: \{/);
});

test("a capacity park reads as a park, not as a batch that wrote nothing", () => {
    const api = loadApp();
    const parked = { status: "parked", op: "write", discard_reason: "capacity_exhausted", error: "memory document exceeds 900 chars" };
    assert.equal(api.memoryProcessedStatusLabel(parked), "已停车");
    assert.equal(api.memoryProcessedIsNoChange(parked), true, "停下来的批次一行都没写，不许画变更牌");
    assert.equal(api.memoryProcessedOpKinds(parked).join("|"), "");
    assert.match(api.memoryProcessedNoopReason(parked), /capacity_exhausted/);

    const applied = { status: "applied", op: "write", changes: [{ type: "add", memory_id: "Ab12Z9", content: "x" }] };
    assert.equal(api.memoryProcessedStatusLabel(applied), "已应用");
    assert.equal(api.memoryProcessedOpKinds(applied).join("|"), "add");
});
