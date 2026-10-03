const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

// 任务大厅性能条两件事：
// 1)「节点队列」读回合闸（`entry_gate_queued_total` 才是「在等请求位」的数）。
//    `node_queue_waiting_count` 量的是过闸之后等模型 permit 的队列，闸收紧时恒 0
//    （实盘 4000 拍里 3986 拍为 0，其中 662 拍闸已贴顶）。
// 2)「监控新鲜度」是一位会自己往前走的秒表：收到新读数归零重计，读数断了也只报
//    「多久之前」，不切「刚刚更新」/「监控过期」这类不带时间的文案。

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
    return html.slice(start, start + 260);
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

test("闸口排队与闸位容量来自 entry_gate_*，不再来自 node_queue_*", () => {
    const html = section(createPerfBar(baseMetrics).render(), "节点队列");
    assert.ok(html.includes(">12/12</span>运行"), html);
    assert.ok(html.includes(">44</span>等待"), html);
    assert.ok(!html.includes(">0</span>等待"), "闸口排 44 个时不得显示 0 等待");
    assert.ok(!html.includes(">7</span>运行"), "模型侧运行数不得顶替闸位占用");
});

test("闸位读数缺失时显示 --，不回落到模型侧队列", () => {
    const html = section(createPerfBar({
        machine_pressure_available: true,
        node_queue_running_count: 9,
        node_queue_waiting_count: 8,
    }).render(), "节点队列");
    assert.ok(html.includes(">--/--</span>运行"), html);
    assert.ok(html.includes(">--</span>等待"), html);
});

test("刚收到的读数显示 0.0秒前，不再显示「刚刚更新」", () => {
    const bar = createPerfBar({});
    const html = section(bar.render({ ...baseMetrics, pressure_sample_age_ms: 20 }), "监控新鲜度");
    assert.ok(html.includes(">0.0秒前<"), html);
    assert.ok(!html.includes("刚刚更新"), html);
});

test("读数停更后本地继续走字，并且不切到「监控过期」", () => {
    const metrics = { ...baseMetrics, pressure_sample_age_ms: 1200 };
    const bar = createPerfBar(metrics);
    assert.ok(section(bar.render(), "监控新鲜度").includes(">1.2秒前<"));
    bar.advanceMs(4000);
    const later = section(bar.render(), "监控新鲜度");
    assert.ok(later.includes(">5.2秒前<"), later);
    assert.ok(!later.includes("监控过期"), later);
});

test("收到新的更旧读数会重新归零起计", () => {
    const bar = createPerfBar({ ...baseMetrics, pressure_sample_age_ms: 9_000 });
    assert.ok(section(bar.render(), "监控新鲜度").includes(">9.0秒前<"));
    bar.advanceMs(1000);
    assert.ok(section(bar.render({ ...baseMetrics, pressure_sample_age_ms: 300 }), "监控新鲜度").includes(">0.3秒前<"));
});

test("更久的断供按分钟计时；一次读数都没有时才是未采样", () => {
    const bar = createPerfBar({ ...baseMetrics, pressure_sample_age_ms: 1200 });
    bar.render();
    bar.advanceMs(5 * 60_000);
    assert.ok(section(bar.render(), "监控新鲜度").includes(">5.0分钟前<"));
    assert.ok(section(bar.render({ ...baseMetrics }), "监控新鲜度").includes(">未采样<"));
});
