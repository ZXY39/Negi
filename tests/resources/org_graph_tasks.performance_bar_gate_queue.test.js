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
// 2)「监控新鲜度」量「距离上一次收到新读数过了多久」：读数身份 pressure_sample_at 一变就归零
//    重计，身份不变只在本地累加；不切「刚刚更新」/「监控过期」这类不带时长的文案。

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

function freshness(bar, metrics) {
    return section(bar.render(metrics), "监控新鲜度");
}

function freshnessValue(html) {
    const matched = html.match(/>([ 0-9][0-9](?:秒|分钟|小时)前)</);
    assert.ok(matched, html.slice(0, 240));
    return matched[1];
}

test("刚收到新读数显示 0秒前（前导留空），不再显示「刚刚更新」", () => {
    const html = freshness(createPerfBar({}), beat(0));
    assert.ok(html.includes("> 0秒前<"), html);
    assert.ok(!html.includes("刚刚更新"), html);
});

test("读数身份没变时本地继续走整数秒，并且不切到「监控过期」", () => {
    const bar = createPerfBar(beat(0));
    assert.equal(freshnessValue(freshness(bar)), " 0秒前");
    bar.advanceMs(4_000);
    const later = freshness(bar);
    assert.equal(freshnessValue(later), " 4秒前");
    assert.ok(!later.includes("监控过期"), later);
    assert.ok(!/\d+\.\d+秒前/.test(later), "新鲜度不显示小数");
});

test("读数每秒都在更新时新鲜度不会累加成几十秒（实盘 payload 带 pressure_sample_at）", () => {
    // 回归：旧实现拿"年龄 <1s"当归零条件，而实盘年龄恒 ≥2.4s；又只在年龄变大时重设锚点，
    // 而新读数只会让年龄变小，于是秒表一路累加，读数明明在更新也显示几分钟前。
    const bar = createPerfBar(beat(0), BASE_NOW_MS);
    freshness(bar);
    const seen = [];
    for (let index = 1; index <= 30; index += 1) {
        bar.advanceMs(1_000);
        seen.push(freshnessValue(freshness(bar, beat(index))));
    }
    assert.ok(seen.every((text) => text === " 0秒前"), JSON.stringify(seen));
});

test("同一份读数越走越旧：没有身份串时只有年龄变小才算新读数", () => {
    const stale = { ...baseMetrics, pressure_sample_age_ms: 12_000 };
    const newer = { ...baseMetrics, pressure_sample_age_ms: 2_400 };
    const bar = createPerfBar(stale, BASE_NOW_MS);
    assert.equal(freshnessValue(freshness(bar)), " 0秒前");
    bar.advanceMs(1_000);
    assert.equal(freshnessValue(freshness(bar, { ...baseMetrics, pressure_sample_age_ms: 13_000 })), " 1秒前");
    assert.equal(freshnessValue(freshness(bar, newer)), " 0秒前");
});

test("更久的断供按整数分钟计时；一次读数都没有时才是未采样", () => {
    const bar = createPerfBar(beat(0), BASE_NOW_MS);
    freshness(bar);
    bar.advanceMs(5 * 60_000);
    assert.equal(freshnessValue(freshness(bar)), " 5分钟前");
    assert.ok(freshness(bar, { ...baseMetrics }).includes(">未采样<"));
});

test("9 秒、10 秒与 99 秒渲染出的文本等长：秒表不改变胶囊宽度", () => {
    const texts = [9_400, 10_400, 99_400].map((elapsedMs) => {
        const bar = createPerfBar(beat(0), BASE_NOW_MS);
        freshness(bar);
        bar.advanceMs(elapsedMs);
        return freshnessValue(freshness(bar));
    });
    assert.deepEqual(texts, [" 9秒前", "10秒前", "99秒前"]);
    assert.equal(new Set(texts.map((text) => text.length)).size, 1, JSON.stringify(texts));
});
