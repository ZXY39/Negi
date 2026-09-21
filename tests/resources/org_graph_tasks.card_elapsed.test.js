const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

// 任务卡片总耗时契约：
// 1) 只显示分钟精度的数字（如 15h30m），小于 1 小时只到分钟；
// 2) 终态（success/failed/unpassed）以 finished_at 定格，运行中用当前时刻；
// 3) 暂停（blocked）以 updated_at 定格——暂停期间数字不增长。

const TASKS_PATH = "g3ku/web/frontend/org_graph_tasks.js";
const TASKS_CODE = fs.readFileSync(TASKS_PATH, "utf8");

function loadHelpers(statusKeyFn) {
    const context = {
        console,
        Promise,
        JSON,
        Number,
        String,
        Math,
        Date,
        esc: (v) => String(v ?? ""),
        formatSessionTime: (v) => `fmt:${v}`,
        taskStatusKey: statusKeyFn,
    };
    context.window = context;
    vm.createContext(context);
    const start = TASKS_CODE.indexOf("function parseTaskTimeMs");
    const end = TASKS_CODE.indexOf("async function copyTaskId");
    assert.ok(start > 0 && end > start, "elapsed helper slice not found");
    vm.runInContext(TASKS_CODE.slice(start, end), context);
    return context;
}

test("分钟精度：跨小时显示 NhMm，不足一小时只显示分钟", () => {
    const context = loadHelpers(() => "success");
    assert.equal(context.formatTaskElapsedMs(15 * 3600e3 + 30 * 60e3), "15h30m");
    assert.equal(context.formatTaskElapsedMs(30 * 60e3), "30m");
    assert.equal(context.formatTaskElapsedMs(59 * 60e3 + 59e3), "59m");
    assert.equal(context.formatTaskElapsedMs(60 * 60e3), "1h0m");
    assert.equal(context.formatTaskElapsedMs(-1000), "0m");
});

test("终态以 finished_at 定格总耗时并显示结束行", () => {
    const context = loadHelpers(() => "success");
    const task = {
        created_at: "2026-09-14T10:00:00+08:00",
        updated_at: "2026-09-14T11:20:00+08:00",
        finished_at: "2026-09-14T10:30:00+08:00",
    };
    assert.equal(context.taskElapsedText(task), "30m", "耗时按 finished_at 而非 updated_at");
    assert.equal(context.taskIsTerminal(task), true);
    assert.equal(context.taskFinishedAtText(task), "fmt:2026-09-14T10:30:00+08:00");
});

test("暂停任务以 updated_at 定格，数字不随当前时刻增长", () => {
    const context = loadHelpers(() => "blocked");
    const task = {
        created_at: "2026-09-14T10:00:00+08:00",
        updated_at: "2026-09-14T10:45:00+08:00",
        finished_at: "",
    };
    const first = context.taskElapsedText(task);
    const second = context.taskElapsedText(task);
    assert.equal(first, "45m");
    assert.equal(second, first, "同一暂停任务重复取值应恒定");
    assert.equal(context.taskIsTerminal(task), false, "暂停不是终态，不应显示结束行");
});

test("运行中任务用当前时刻计算", () => {
    const context = loadHelpers(() => "in_progress");
    const twoMinutesAgo = new Date(Date.now() - 2 * 60e3).toISOString();
    assert.equal(context.taskElapsedText({ created_at: twoMinutesAgo }), "2m");
    assert.equal(context.taskElapsedText({}), "--", "缺少 created_at 时占位为 --");
});
