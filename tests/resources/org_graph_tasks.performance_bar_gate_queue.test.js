const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const { readFrontendSources } = require("./support/frontend_sources.js");

// 任务大厅性能条：
// 1)「节点」一项读回合闸，三个数各带标签：运行=占住的闸位（entry_gate_running_total）、
//    等待=闸口排队（entry_gate_queued_total）、容量=闸开多大（entry_gate_limits_total）。
//    `node_queue_waiting_count` 量的是过闸之后等模型 permit 的队列，闸收紧时恒 0（实盘
//    4000 拍里 3986 拍为 0，其中 662 拍闸已贴顶），当不了「等待」的读数；数字也不许叠成
//    `N/M`——分不清哪个是占用、哪个是天花板。
// 2) 文案保持紧凑：压力状态→压力、工具队列→工具、节点队列→节点、当前容量→容量，
//    「监控新鲜度」整项移除（秒表只剩压力一个消费者）。
// 3) 压力用一根按读数身份归零的秒表判停更（≥15s 才翻），标签带时长。

const TASKS_PATH = "g3ku/web/frontend/org_graph_tasks.js";
const TASKS_CODE = readFrontendSources(["tasks"]).join("\n");

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

test("性能条只剩四项，标签压到最短且监控新鲜度整项移除", () => {
    const html = createPerfBar(baseMetrics).render();
    assert.equal((html.match(/class="task-performance-item/g) || []).length, 4);
    assert.ok(html.includes(">压力<"), html);
    assert.ok(!html.includes("压力状态"), html);
    assert.ok(html.includes(">工具<"), html);
    assert.ok(!html.includes("工具队列"), html);
    assert.ok(html.includes(">节点<"), html);
    assert.ok(!html.includes("节点队列"), html);
    assert.ok(html.includes(">容量<"), html);
    assert.ok(!html.includes("当前容量"), html);
    assert.ok(!html.includes("监控新鲜度"), html);
    assert.ok(!html.includes("task-performance-age-value"), html);
});

test("运行/等待/容量各自带标签，读数来自 entry_gate_*", () => {
    const html = section(createPerfBar(baseMetrics).render(), "节点");
    assert.ok(html.includes(">12</span>运行"), html);
    assert.ok(html.includes(">44</span>等待"), html);
    assert.ok(html.includes(">12</span>容量"), html);
    assert.ok(!html.includes("12/12"), "不得把占用与容量叠成分数");
    assert.ok(!html.includes(">0</span>等待"), "闸口排 44 个时不得显示 0 等待");
    assert.ok(!html.includes(">7</span>运行"), "模型侧运行数不得顶替闸位占用");
});

test("闸位读数缺失时三项都显示 --，不回落到模型侧队列", () => {
    const html = section(createPerfBar({
        machine_pressure_available: true,
        node_queue_running_count: 9,
        node_queue_waiting_count: 8,
    }).render(), "节点");
    assert.ok(html.includes(">--</span>运行"), html);
    assert.ok(html.includes(">--</span>等待"), html);
    assert.ok(html.includes(">--</span>容量"), html);
});

const BASE_NOW_MS = 1_700_000_000_000;
// 实盘 REST 的读数形状：身份串只有整秒精度，且浏览器看到它时已经过了 1–2 拍（实测年龄稳态
// 2.4s 上下），所以身份总是落后当前时钟 2.4s。
const isoAt = (offsetMs) => new Date(BASE_NOW_MS + offsetMs).toISOString();
const SAMPLE_LAG_MS = 2_400;
const beat = (index) => ({
    ...baseMetrics,
    pressure_sample_at: isoAt(index * 1_000 - SAMPLE_LAG_MS),
    pressure_sample_age_ms: SAMPLE_LAG_MS,
});

function pressureLabel(bar, metrics) {
    return section(bar.render(metrics), "压力").match(/task-performance-value">([^<]*)</)[1];
}

test("读数持续更新时压力照报状态，不受服务端过期标记影响", () => {
    const bar = createPerfBar({ ...beat(0), pressure_snapshot_fresh: false }, BASE_NOW_MS);
    assert.equal(pressureLabel(bar), "正常");
    for (let index = 1; index <= 6; index += 1) {
        bar.advanceMs(1_000);
        assert.equal(pressureLabel(bar, { ...beat(index), pressure_snapshot_fresh: false }), "正常");
    }
    assert.ok(!bar.render().includes("监控过期"), "整条性能条不再出现「监控过期」");
});

test("读数身份 15 秒没变，压力改报停更时长", () => {
    const bar = createPerfBar(beat(0), BASE_NOW_MS);
    assert.equal(pressureLabel(bar), "正常");
    bar.advanceMs(14_000);
    assert.equal(pressureLabel(bar), "正常");
    bar.advanceMs(2_000);
    assert.equal(pressureLabel(bar), "停更16秒");
});

test("机器资源读数缺失时不谎报停更", () => {
    const bar = createPerfBar({ ...beat(0), machine_pressure_available: false }, BASE_NOW_MS);
    assert.equal(pressureLabel(bar), "无资源读数");
    bar.advanceMs(2_000);
    assert.equal(pressureLabel(bar, { ...beat(2), machine_pressure_available: false }), "无资源读数");
});
