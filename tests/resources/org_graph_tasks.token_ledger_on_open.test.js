const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

// 按模型明细不再由任务详情快照带过来（那一格冷页扫全任务明细行实测 4447 ms，会拖住
// "点开任务"），改为 Token 统计窗口一打开就自己取窄接口。两条契约：
// 1) setTaskTokenStatsOpen(true) 必须触发一次取数，且不弹「已刷新」（没人点刷新）；
// 2) 结果还没落到本任务时，模型区说"正在取数"，不能沿用"尚无按模型明细"那句
//    ——那是在宣称服务端没有这份数据，而它只是在飞。

const TASKS_PATH = "g3ku/web/frontend/org_graph_tasks.js";
const TASKS_CODE = fs.readFileSync(TASKS_PATH, "utf8");

function slice(fromMarker, toMarker) {
    const start = TASKS_CODE.indexOf(fromMarker);
    const end = TASKS_CODE.indexOf(toMarker);
    assert.ok(start > 0 && end > start, `slice not found: ${fromMarker}`);
    return TASKS_CODE.slice(start, end);
}

function baseEnv(extra = {}) {
    return {
        console,
        Number,
        String,
        Math,
        Array,
        Object,
        Boolean,
        JSON,
        esc: (v) => String(v ?? ""),
        formatTokenCount: (v) => String(v),
        ...extra,
    };
}

test("打开 Token 窗口就取按模型明细，且不弹刷新 toast", () => {
    const calls = [];
    const context = baseEnv({
        S: {},
        U: { taskTokenBackdrop: null, taskTokenDrawer: null },
        setDrawerOpen: () => {},
        renderTaskTokenStats: () => calls.push('render'),
        ensureTaskTokenModelCatalog: () => Promise.resolve(),
        refreshTaskTokenLedger: (options) => { calls.push(`ledger:${JSON.stringify(options || {})}`); return Promise.resolve(); },
    });
    vm.createContext(context);
    vm.runInContext(slice("function setTaskTokenStatsOpen", "\n// 模型目录正常在 boot 时已加载"), context);

    context.setTaskTokenStatsOpen(true);

    assert.deepEqual(calls, ['render', 'ledger:{"quiet":true}']);
    assert.equal(context.S.taskTokenStatsOpen, true);
});

test("关窗口不取数", () => {
    const calls = [];
    const context = baseEnv({
        S: {},
        U: {},
        setDrawerOpen: () => {},
        renderTaskTokenStats: () => calls.push('render'),
        ensureTaskTokenModelCatalog: () => Promise.resolve(),
        refreshTaskTokenLedger: () => { calls.push('ledger'); return Promise.resolve(); },
    });
    vm.createContext(context);
    vm.runInContext(slice("function setTaskTokenStatsOpen", "\n// 模型目录正常在 boot 时已加载"), context);

    context.setTaskTokenStatsOpen(false);

    assert.deepEqual(calls, []);
});

test("渲染里用的正是那个归属比较：落地前说正在取数", () => {
    const body = slice("function renderTaskTokenStats", "function refreshTaskTokenLedger");
    assert.match(body, /const modelRowsMarkup = String\(S\.taskTokenLedgerTaskId \|\| ""\)\.trim\(\)/);
    assert.match(body, /=== String\(S\.currentTaskId \|\| ""\)\.trim\(\)/);
    assert.match(body, /按模型明细正在取数/);
    assert.match(body, /\$\{modelRowsMarkup\}/, "模型区必须用带归属判定的那份");
    assert.doesNotMatch(body, /<div class="task-token-model-list">\$\{rowsMarkup\}<\/div>/);
});

test("任务详情快照不再带这一格，段名跟着成本搬到窄接口", () => {
    const source = fs.readFileSync("main/monitoring/query_service.py", "utf8");
    const start = source.indexOf("def get_task_snapshot");
    const ledgerAt = source.indexOf("def get_task_token_ledger");
    assert.ok(start > 0 && ledgerAt > start);
    assert.doesNotMatch(
        source.slice(start, ledgerAt),
        /'token_usage_by_model':\s*token_usage_by_model/,
        "快照不该再算并挂上这一格",
    );
    assert.match(
        source.slice(ledgerAt, ledgerAt + 2600),
        /get_task_token_ledger\.token_rollup/,
        "这一格的长块段名必须在真正跑它的车道上",
    );
});
