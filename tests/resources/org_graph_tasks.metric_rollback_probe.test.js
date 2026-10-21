const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const { readFrontendSources } = require("./support/frontend_sources.js");

// 任务卡片 token 数只许往前这条契约：
// 服务端那一格（任务行 token_usage）是加性累加，所以任何写者带来更小的数必然是旧数据——
// 整表替换的列表响应赶在补丁之后落地（lane=list-apply），或某条整包回写把在飞的合并打回去
// （lane=patch-merge）。守卫保留手里那份较大的数，并把压掉的差值记进观测器留归因。

const TASKS_PATH = "g3ku/web/frontend/org_graph_tasks.js";
const APP_PATH = "g3ku/web/frontend/org_graph_app.js";
const TASKS_CODE = readFrontendSources(["tasks"]).join("\n");
const APP_CODE = readFrontendSources(["app"]).join("\n");

function sliceBetween(code, startMarker, endMarker, label) {
    const start = code.indexOf(startMarker);
    const end = code.indexOf(endMarker);
    assert.ok(start > 0 && end > start, `${label} slice not found`);
    return code.slice(start, end);
}

function loadProbe() {
    const context = {
        console,
        JSON,
        Number,
        String,
        Math,
        Array,
        Object,
        Date,
        Intl,
        S: { taskHallStats: { task_hall_metric_rollback_count: 0 }, taskItemStateSource: {}, taskMetricRollbacks: [] },
    };
    context.window = context;
    vm.createContext(context);
    // 显示口径用 app 里那三个真函数，不在测试里重算一遍公式。
    vm.runInContext(sliceBetween(APP_CODE, "function normalizeTokenUsage", "function normalizeModelTokenUsage", "normalizeTokenUsage"), context);
    vm.runInContext(sliceBetween(APP_CODE, "function tokenKnownTotal", "function tokenDisplayUsage", "tokenKnownTotal+taskTokenUsage"), context);
    vm.runInContext(sliceBetween(APP_CODE, "function tokenDisplayUsage", "function taskTokenSummaryLine", "tokenDisplayUsage"), context);
    vm.runInContext(sliceBetween(
        TASKS_CODE,
        "function noteTaskMetricRollback",
        "function taskMetricValueMarkup",
        "token rollback guard"
    ), context);
    return context;
}

function usage({ tracked = true, input = 0, cache = 0, output = 0 } = {}) {
    return { tracked, input_tokens: input, cache_hit_tokens: cache, output_tokens: output, call_count: 1 };
}

function run(context, code) {
    return vm.runInContext(code, context);
}

test("列表整表替换带来更小的数：保留手里那份，并记一次压制", () => {
    const context = loadProbe();
    run(context, 'noteTaskItemStateSource("task:a", "list")');
    const held = { token_usage: usage({ input: 391_645_384, cache: 420_943_104, output: 15_056_444 }) };
    const next = { token_usage: usage({ input: 391_192_271, cache: 420_943_104, output: 14_988_992 }) };

    const kept = run(context, `(() => {
        const heldTask = ${JSON.stringify(held)};
        const nextTask = ${JSON.stringify(next)};
        return resolveTaskTokenUsage("task:a", heldTask, nextTask, "list-apply");
    })()`);

    assert.equal(kept.input_tokens, held.token_usage.input_tokens, "旧的输入不许覆盖新的");
    assert.equal(kept.output_tokens, held.token_usage.output_tokens, "旧的输出不许覆盖新的");
    const state = run(context, "S");
    assert.ok(state.taskHallStats.task_hall_metric_rollback_count >= 1, "每一次压制都可数");
    const entry = state.taskMetricRollbacks[0];
    assert.equal(entry.task_id, "task:a");
    assert.equal(entry.lane, "list-apply");
    assert.equal(entry.source, "list");
    assert.ok(entry.from > entry.to, "记的是被压掉的差值方向");
});

test("更大的数照单采纳，什么都不记", () => {
    const context = loadProbe();
    run(context, 'noteTaskItemStateSource("task:a", "patch")');
    const kept = run(context, `(() => {
        const heldTask = { token_usage: ${JSON.stringify(usage({ input: 100, cache: 50, output: 10 }))} };
        const nextTask = { token_usage: ${JSON.stringify(usage({ input: 180, cache: 50, output: 12 }))} };
        return resolveTaskTokenUsage("task:a", heldTask, nextTask, "patch-merge");
    })()`);
    assert.equal(kept.input_tokens, 180);
    assert.equal(run(context, "S.taskMetricRollbacks.length"), 0);
    assert.equal(run(context, "S.taskHallStats.task_hall_metric_rollback_count"), 0);
});

test("没带用量的一行（tracked=false）不会把卡片打成 --", () => {
    const context = loadProbe();
    const kept = run(context, `(() => {
        const heldTask = { token_usage: ${JSON.stringify(usage({ input: 4000, cache: 900, output: 70 }))} };
        const nextTask = { token_usage: ${JSON.stringify(usage({ tracked: false }))} };
        return resolveTaskTokenUsage("task:a", heldTask, nextTask, "list-apply");
    })()`);
    assert.equal(kept.tracked, true, "未统计的行全 0，按旧数据处理");
    assert.equal(kept.input_tokens, 4000);
});

test("首次拿到数据与缺字段都不判回退", () => {
    const context = loadProbe();
    assert.equal(run(context, `(() => {
        const heldTask = { token_usage: null };
        const nextTask = { token_usage: ${JSON.stringify(usage({ input: 10 }))} };
        return resolveTaskTokenUsage("task:a", heldTask, nextTask, "patch-merge").input_tokens;
    })()`), 10);
    assert.equal(run(context, `(() => {
        const heldTask = { token_usage: ${JSON.stringify(usage({ input: 10 }))} };
        return resolveTaskTokenUsage("task:a", heldTask, {}, "patch-merge").input_tokens;
    })()`), 10);
    assert.equal(run(context, "S.taskMetricRollbacks.length"), 0);
});

test("两条写车道各挂守卫，列表道与补丁道都不许回退", () => {
    assert.match(
        TASKS_CODE,
        /resolveTaskTokenUsage\(taskId, heldTask, item, "list-apply"\)/,
        "列表整表替换道"
    );
    assert.match(
        TASKS_CODE,
        /resolveTaskTokenUsage\(taskId, previousTask, nextTask, "patch-merge"\)/,
        "补丁合并道"
    );
    assert.match(TASKS_CODE, /sources\[taskId\] = "list";/, "列表道落来源标记");
    assert.match(TASKS_CODE, /noteTaskItemStateSource\(taskId, "patch"\);/, "补丁道落来源标记");
});
