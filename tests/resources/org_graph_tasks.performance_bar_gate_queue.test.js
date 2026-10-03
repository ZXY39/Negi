const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

// 任务大厅性能条两件事：
// 1)「节点队列」读回合闸，三个数各带标签：运行=占住的闸位（entry_gate_running_total）、
//    等待=闸口排队（entry_gate_queued_total）、当前容量=闸开多大（entry_gate_limits_total）。
//    `node_queue_waiting_count` 量的是过闸之后等模型 permit 的队列，闸收紧时恒 0（实盘
//    4000 拍里 3986 拍为 0，其中 662 拍闸已贴顶），当不了「等待」的读数；数字也不许叠成
//    `N/M`——分不清哪个是占用、哪个是天花板。
// 2)「监控新鲜度」是一根只走整数的秒表：收到新读数归零重计，读数断了继续往上走，
//    不切「刚刚更新」/「监控过期」这类不带时长的文案。

const TASKS_PATH = "g3ku/web/frontend/org_graph_tasks.js";
const TASKS_CODE = fs.readFileSync(TASKS_PATH, "utf8");

function createPerfBar(initialMetrics, initialNowMs = 1_700_000_000_000) {
    let clockMs = initialNowMs;
    const bar = { hidden: false, innerHTML: "" };
    const context = {
        console,
        Promise,
        Number,
        Math,
        String,
        JSON,
        Date: { now: () => clockMs, parse: (value) => Date.parse(value) },
        esc: (value) => String(value ?? ""),
        normalizeTaskWorkerState: (state) => String(state || "").toLowerCase(),
        S: { tasksWorker: { payload: initialMetrics }, tasksWorkerStatusPayload: null },
        U: { taskPerformanceBar: bar },
    };
    context.window = context;
    vm.createContext(context);
    const start = TASKS_CODE.indexOf("function taskWorkerStatusMetrics()");
    const end = TASKS_CODE.indexOf("function renderTaskSessionScope");
    assert.ok(start > 0 && end > start, "perf bar slice not found");
    vm.runInContext(TASKS_CODE.slice(start, end), context);
    return {
        context,
        advanceMs: (delta) => { clockMs += delta; },
        render(metrics) {
            if (metrics !== undefined) context.S.tasksWorker = { payload: metrics };
            context.renderTaskPerformanceBar();
            return bar.innerHTML;
        },
    };
}

function section(html, label) {
    const start = html.indexOf(label);
    assert.ok(start > 0, `性能条必须渲染「${label}」一项`);
    return html.slice(start, start + 320);
}

const baseMetrics = {
    machine_pressure_available: true,
    machine_pressure_cpu_percent: 30,
    machine_pressure_memory_percent: 40,
    entry_gate_running_total: 12,
    entry_gate_limits_total: 12,
    entry_gate_queued_total: 44,
    node_queue_running_count: 7,
    node_queue_waiting_count: 0,
};

test("运行/等待/当前容量各自带标签，读数来自 entry_gate_*", () => {
    const html = section(createPerfBar(baseMetrics).render(), "节点队列");
    assert.ok(html.includes(">12</span>运行"), html);
    assert.ok(html.includes(">44</span>等待"), html);
    assert.ok(html.includes(">12</span>当前容量"), html);
    assert.ok(!html.includes("12/12"), "不得把占用与容量叠成分数");
    assert.ok(!html.includes(">0</span>等待"), "闸口排 44 个时不得显示 0 等待");
    assert.ok(!html.includes(">7</span>运行"), "模型侧运行数不得顶替闸位占用");
});

test("闸位读数缺失时三项都显示 --，不回落到模型侧队列", () => {
    const html = section(createPerfBar({
        machine_pressure_available: true,
        node_queue_running_count: 9,
        node_queue_waiting_count: 8,
    }).render(), "节点队列");
    assert.ok(html.includes(">--</span>运行"), html);
    assert.ok(html.includes(">--</span>等待"), html);
    assert.ok(html.includes(">--</span>当前容量"), html);
});

test("刚收到的读数显示 0秒前，不再显示「刚刚更新」", () => {
    const html = section(createPerfBar({}).render({ ...baseMetrics, pressure_sample_age_ms: 20 }), "监控新鲜度");
    assert.ok(html.includes(">0秒前<"), html);
    assert.ok(!html.includes("刚刚更新"), html);
});

test("读数停更后本地继续走整数秒，并且不切到「监控过期」", () => {
    const metrics = { ...baseMetrics, pressure_sample_age_ms: 1200 };
    const bar = createPerfBar(metrics);
    assert.ok(section(bar.render(), "监控新鲜度").includes(">1秒前<"));
    bar.advanceMs(4000);
    const later = section(bar.render(), "监控新鲜度");
    assert.ok(later.includes(">5秒前<"), later);
    assert.ok(!later.includes("监控过期"), later);
    assert.ok(!/\d+\.\d+秒前/.test(later), "新鲜度不显示小数");
});

test("收到新的更旧读数会重新归零起计", () => {
    const bar = createPerfBar({ ...baseMetrics, pressure_sample_age_ms: 9_400 });
    assert.ok(section(bar.render(), "监控新鲜度").includes(">9秒前<"));
    bar.advanceMs(1000);
    assert.ok(section(bar.render({ ...baseMetrics, pressure_sample_age_ms: 300 }), "监控新鲜度").includes(">0秒前<"));
});

test("更久的断供按整数分钟计时；一次读数都没有时才是未采样", () => {
    const bar = createPerfBar({ ...baseMetrics, pressure_sample_age_ms: 1200 });
    bar.render();
    bar.advanceMs(5 * 60_000);
    assert.ok(section(bar.render(), "监控新鲜度").includes(">5分钟前<"));
    assert.ok(section(bar.render({ ...baseMetrics }), "监控新鲜度").includes(">未采样<"));
});
