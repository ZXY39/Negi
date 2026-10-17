const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

// 任务卡片 token 数回退观测器的契约：
// 卡片上的数只有一条服务端来源（任务行的 token_usage），所以"比上一次小"本身就是证据。
// 这一族只记读数、不改任何显示，归因靠 source（list=整表替换 / patch=补丁合并）与
// lane（card-patch=增量补丁道 / full-render=整网格重建道）。

const TASKS_PATH = "g3ku/web/frontend/org_graph_tasks.js";
const TASKS_CODE = fs.readFileSync(TASKS_PATH, "utf8");

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
        S: { taskHallStats: { task_hall_metric_rollback_count: 0 }, taskItemStateSource: {}, taskMetricRollbacks: [] },
    };
    context.window = context;
    vm.createContext(context);
    const start = TASKS_CODE.indexOf("function noteTaskMetricRollback");
    const end = TASKS_CODE.indexOf("function taskMetricValueMarkup");
    assert.ok(start > 0 && end > start, "rollback probe slice not found");
    vm.runInContext(TASKS_CODE.slice(start, end), context);
    return context;
}

test("回退记一条：带 from/to、观察道与写数据的来源车道", () => {
    const context = loadProbe();
    vm.runInContext('noteTaskItemStateSource("task:a", "list")', context);

    const hit = vm.runInContext(
        'noteTaskMetricRollback("task:a", "input_tokens", 391645384, 391192271, "full-render")',
        context
    );
    assert.equal(hit, true);
    const state = vm.runInContext("S", context);
    assert.equal(state.taskHallStats.task_hall_metric_rollback_count, 1);
    // vm 里造的对象与宿主原型不同，deepEqual 会因引用/原型判不等，逐字段断言。
    const entry = state.taskMetricRollbacks[0];
    assert.equal(entry.task_id, "task:a");
    assert.equal(entry.metric, "input_tokens");
    assert.equal(entry.from, 391645384);
    assert.equal(entry.to, 391192271);
    assert.equal(entry.lane, "full-render");
    assert.equal(entry.source, "list");
    assert.ok(Number.isFinite(Date.parse(entry.at)), "at 是可解析的时刻");
});

test("数字不减时什么都不记", () => {
    const context = loadProbe();
    vm.runInContext('noteTaskItemStateSource("task:a", "patch")', context);

    assert.equal(vm.runInContext('noteTaskMetricRollback("task:a", "input_tokens", 100, 100, "card-patch")', context), false);
    assert.equal(vm.runInContext('noteTaskMetricRollback("task:a", "output_tokens", 100, 140, "card-patch")', context), false);
    const state = vm.runInContext("S", context);
    assert.equal(state.taskHallStats.task_hall_metric_rollback_count, 0);
    assert.equal(state.taskMetricRollbacks.length, 0);
});

test("首次取值与终态缺数都不算回退", () => {
    const context = loadProbe();
    // previousValue 为 null（这张卡第一次渲染）或 metricValue 为 null（tracked=false 画 "--"）
    assert.equal(vm.runInContext('noteTaskMetricRollback("task:a", "cache_hit_tokens", null, 420943104, "full-render")', context), false);
    assert.equal(vm.runInContext('noteTaskMetricRollback("task:a", "cache_hit_tokens", 420943104, null, "full-render")', context), false);
    assert.equal(vm.runInContext("S.taskMetricRollbacks.length", context), 0);
});

test("日志只留最近 12 条，新的排在最前", () => {
    const context = loadProbe();
    vm.runInContext(`
        for (let i = 0; i < 15; i += 1) {
            noteTaskMetricRollback("task:" + i, "input_tokens", 1000, 900, "full-render");
        }
    `, context);
    const state = vm.runInContext("S", context);
    assert.equal(state.taskMetricRollbacks.length, 12);
    assert.equal(state.taskMetricRollbacks[0].task_id, "task:14");
    assert.equal(state.taskHallStats.task_hall_metric_rollback_count, 15, "计数记总命中，日志只留窗口");
});

test("两条渲染道都挂观测，否则复发时读不到数", () => {
    assert.match(
        TASKS_CODE,
        /noteTaskMetricRollback\(key, metricKey, previousValue, metricValue, "card-patch"\)/,
        "增量补丁道"
    );
    assert.match(
        TASKS_CODE,
        /noteTaskMetricRollback\(taskId, item\.key, previousValue, hasValue \? item\.value : null, "full-render"\)/,
        "整网格重建道"
    );
    // 归因判据必须由两条写车道各自盖章：列表整表替换记 list，补丁合并记 patch。
    assert.match(TASKS_CODE, /sources\[taskId\] = "list";/, "列表道落来源");
    assert.match(TASKS_CODE, /noteTaskItemStateSource\(taskId, "patch"\);/, "补丁道落来源");
});
