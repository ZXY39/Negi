const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

// 性能条「节点队列」那一段必须读回合闸：等待 = entry_gate_queued_total，运行 =
// entry_gate_running_total / entry_gate_limits_total。node_queue_waiting_count 量的是
// 过闸之后等模型 permit 的队列，闸收紧时它恒 0（实盘 4000 拍里 3986 拍为 0，而其中
// 662 拍闸已贴顶），拿它当「等待」会让闸口排队 41 个节点看起来像没有排队。

const TASKS_PATH = "g3ku/web/frontend/org_graph_tasks.js";
const TASKS_CODE = fs.readFileSync(TASKS_PATH, "utf8");

function loadPerfBar(metrics) {
    const context = {
        console,
        Promise,
        Number,
        Math,
        Date,
        String,
        JSON,
        esc: (value) => String(value ?? ""),
        normalizeTaskWorkerState: (state) => String(state || "").toLowerCase(),
        S: { tasksWorker: { payload: metrics }, tasksWorkerStatusPayload: null },
        U: { taskPerformanceBar: { hidden: true, innerHTML: "" } },
    };
    context.window = context;
    vm.createContext(context);
    const start = TASKS_CODE.indexOf("function taskWorkerStatusMetrics()");
    const end = TASKS_CODE.indexOf("function renderTaskSessionScope");
    assert.ok(start > 0 && end > start, "perf bar slice not found");
    vm.runInContext(TASKS_CODE.slice(start, end), context);
    context.renderTaskPerformanceBar();
    return context.U.taskPerformanceBar.innerHTML;
}

function gateQueueSection(html) {
    const start = html.indexOf("节点队列");
    assert.ok(start > 0, "性能条必须渲染「节点队列」一项");
    return html.slice(start, start + 420);
}

test("闸口排队与闸位容量来自 entry_gate_*，不再来自 node_queue_*", () => {
    const html = gateQueueSection(loadPerfBar({
        machine_pressure_available: true,
        machine_pressure_cpu_percent: 30,
        machine_pressure_memory_percent: 40,
        entry_gate_running_total: 12,
        entry_gate_limits_total: 12,
        entry_gate_queued_total: 44,
        node_queue_running_count: 7,
        node_queue_waiting_count: 0,
    }));
    assert.ok(html.includes(">12/12</span>运行"), html);
    assert.ok(html.includes(">44</span>等待"), html);
    assert.ok(!html.includes(">0</span>等待"), "闸口排 44 个时不得显示 0 等待");
    assert.ok(!html.includes(">7</span>运行"), "模型侧运行数不得顶替闸位占用");
});

test("闸位读数缺失时显示 --，不回落到模型侧队列", () => {
    const html = gateQueueSection(loadPerfBar({
        machine_pressure_available: true,
        node_queue_running_count: 9,
        node_queue_waiting_count: 8,
    }));
    assert.ok(html.includes(">--/--</span>运行"), html);
    assert.ok(html.includes(">--</span>等待"), html);
});
