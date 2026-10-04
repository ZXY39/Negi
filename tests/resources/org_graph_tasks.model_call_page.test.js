const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

// 模型调用明细「点了才加载」的车道契约：
// 1) 底栏页号按整本账本算（`total_model_calls`），不是按已加载行数算；
// 2) 窗口内的页仍是本地切片，零请求；越过窗口才发那一跳；
// 3) 服务端页行进 S.taskModelCallPageRows，不进 S.recentModelCalls（那条数组只涨不缩）；
// 4) 进入快照态后每次翻页都回传同一个 anchor，页号不会随新调用漂；
// 5) 快照态搜索只作用已加载那一页，且不动页号；「回到最新」退态并重取窗口。
//
// 行归一化在这里是桩（`normalizeTaskModelCall` 在 app.js，与本车道无关），
// 其余函数按源码顺序整段取真实现。

const TASKS_PATH = "g3ku/web/frontend/org_graph_tasks.js";
const TASKS_CODE = fs.readFileSync(TASKS_PATH, "utf8");

function makeContext(state) {
    const context = {
        console,
        Number,
        String,
        Boolean,
        Array,
        Object,
        Math,
        Date,
        Set,
        Map,
        JSON,
        Promise,
        RegExp,
        Error,
        isNaN,
        parseInt,
        parseFloat,
        setTimeout: (fn) => {
            fn();
            return 0;
        },
        formatTokenCount: (value) => String(Number(value || 0).toLocaleString("en-US")),
        esc: (value) => String(value ?? ""),
        normalizeTaskModelCall: (raw) => ({ ...(raw && typeof raw === "object" ? raw : {}) }),
        showToast: () => {},
        S: state,
        U: { taskTokenContent: { innerHTML: "", querySelector: () => null } },
        requests: [],
        toasts: [],
    };
    context.ApiClient = {
        getTaskModelCallPage: async (taskId, options) => {
            context.requests.push({ taskId, ...options });
            const page = Number(options.page || 1);
            const size = Number(options.size || 100);
            const total = 49758;
            return {
                task_id: taskId,
                page,
                size,
                anchor_seq: Number(options.anchor || 89984),
                total_calls: total,
                total_pages: Math.ceil(total / size),
                model_calls: Array.from({ length: size }, (_, index) => ({
                    call_index: total - ((page - 1) * size) - size + index,
                    node_id: "node:hist",
                    created_at: `2026-09-${String(20 + (index % 9)).padStart(2, "0")}T10:00:${String(index % 60).padStart(2, "0")}+08:00`,
                    delta_usage: { input_tokens: index, cache_hit_tokens: 0 },
                    delta_usage_by_model: [],
                })),
            };
        },
        getTaskTokenLedger: async (taskId) => {
            context.requests.push({ taskId, lane: "ledger" });
            return { token_usage: {}, token_usage_by_model: [], model_calls: [] };
        },
    };
    vm.createContext(context);
    const start = TASKS_CODE.indexOf("function taskModelDisplayName");
    const end = TASKS_CODE.indexOf("async function loadTaskDetail");
    assert.ok(start > 0 && end > start, "org_graph_tasks.js 明细车道函数段未找到");
    vm.runInContext(TASKS_CODE.slice(start, end), context);
    return context;
}

function windowRows(count) {
    return Array.from({ length: count }, (_, index) => ({
        call_index: index,
        node_id: `node:${index % 7}`,
        created_at: `2026-10-04T13:${String(index % 60).padStart(2, "0")}:00+08:00`,
        delta_usage: { input_tokens: index, cache_hit_tokens: 0 },
        delta_usage_by_model: [],
    }));
}

function baseState() {
    return {
        currentTaskId: "task:1d9cddf9858e",
        taskSummary: { total_model_calls: 49758, token_usage_by_model: [] },
        recentModelCalls: windowRows(301),
        taskModelCallsPage: 1,
        taskModelCallsPageSize: 100,
        taskModelCallsQuery: "",
        taskModelCallPaging: null,
        taskModelCallPageRows: [],
        taskModelCallPageLoading: false,
    };
}

async function flush(context) {
    for (let index = 0; index < 12; index += 1) await Promise.resolve();
    void context;
}

test("底栏页号按整本账本算，窗口内的页不发消息", async () => {
    const context = makeContext(baseState());
    const view = context.taskModelCallViewState();

    assert.equal(view.meta.totalPages, 498, "页数按 total_model_calls 算，不是按已加载 301 行算");
    assert.equal(view.meta.currentPage, 1);
    assert.equal(view.meta.items.length, 100);
    assert.equal(view.meta.grandTotal, 49758);
    assert.match(context.taskModelCallPageSummary(view.meta), /第 1\/498 页 · 显示 1-100 \/ 共 49,758 条/);

    context.setTaskModelCallsPage(3);
    await flush(context);

    assert.equal(context.S.taskModelCallsPage, 3);
    assert.deepEqual(context.requests, [], "窗口内翻页不该发任何请求");

    // 301 行窗口 = 4 个本地页，边界就在第 4 页
    context.setTaskModelCallsPage(4);
    await flush(context);
    assert.equal(context.S.taskModelCallsPage, 4);
    assert.equal(context.requests.length, 0);
    assert.equal(context.taskModelCallViewState().meta.items.length, 1);
});

test("越过窗口才取数，历史行进独立桶", async () => {
    const context = makeContext(baseState());
    const windowSize = context.S.recentModelCalls.length;

    context.setTaskModelCallsPage(5);
    await flush(context);

    assert.equal(context.requests.length, 1);
    assert.equal(context.requests[0].page, 5);
    assert.equal(context.requests[0].anchor, null, "首次进翻页态不带锚点，服务端按当前尾部起算");
    assert.equal(context.S.taskModelCallPaging.anchor_seq, 89984);
    assert.equal(context.S.taskModelCallPaging.page, 5);
    assert.equal(context.S.taskModelCallPageRows.length, 100);
    assert.equal(context.S.recentModelCalls.length, windowSize, "历史页不许灌进只涨不缩的窗口数组");
    assert.equal(context.isTaskModelCallHistorical(), true);
});

test("同一锚点贯穿后续翻页，页号不随新调用漂", async () => {
    const context = makeContext(baseState());
    context.setTaskModelCallsPage(5);
    await flush(context);
    context.requests.length = 0;

    context.setTaskModelCallsPage(300);
    await flush(context);

    assert.equal(context.requests.length, 1);
    assert.equal(context.requests[0].anchor, 89984, "翻页态必须回传进入时的锚点");
    assert.equal(context.S.taskModelCallPaging.total_pages, 498);
});

test("快照态搜索只作用本页且不动页号", async () => {
    const context = makeContext(baseState());
    context.setTaskModelCallsPage(5);
    await flush(context);
    context.requests.length = 0;

    context.S.taskModelCallsQuery = "node:hist";
    const view = context.taskModelCallViewState();

    assert.equal(view.meta.currentPage, 5, "搜索不能把用户弹回第 1 页");
    assert.equal(view.historical, true);
    assert.equal(view.filtered.length, 100);
    assert.deepEqual(context.requests, [], "搜索不发请求");
});

test("回到最新：退出快照态并重取窗口", async () => {
    const context = makeContext(baseState());
    context.setTaskModelCallsPage(5);
    await flush(context);
    context.requests.length = 0;

    context.exitTaskModelCallHistory();
    await flush(context);

    assert.equal(context.S.taskModelCallPaging, null);
    assert.equal(context.S.taskModelCallPageRows.length, 0);
    assert.equal(context.S.taskModelCallsPage, 1);
    assert.deepEqual(
        context.requests.map((item) => item.lane || "page"),
        ["ledger"],
        "回到最新要重取最近一窗，而不是留在旧快照",
    );
    assert.equal(context.isTaskModelCallHistorical(), false);
});

test("实时到达的调用只进窗口，不进快照页", async () => {
    const context = makeContext(baseState());
    const before = context.S.taskModelCallPageRows.slice();

    const merged = context.mergeTaskModelCallRows(context.S.recentModelCalls, [{
        call_index: 99,
        node_id: "node:fresh",
        created_at: "2026-10-04T13:59:00+08:00",
        delta_usage: {},
        delta_usage_by_model: [],
    }]);

    assert.equal(merged.length, context.S.recentModelCalls.length + 1);
    assert.deepEqual(context.S.taskModelCallPageRows, before, "合流口与快照页互不相干");
});
