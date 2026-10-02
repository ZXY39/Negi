const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

// 「任务开始以来共 N 次调用」这条文案的契约：
// 1) N 必须来自服务端计数（summary.total_model_calls），不能是明细行的长度——
//    快照只带最近一窗（300 条），用长度反推会把 31686 次印成 300 次；
// 2) 只有当真总数多于带回条数时，才追加"明细带最近 X 条"；
// 3) 计数缺失时不声称"任务开始以来"，只说"已收到多少条明细"。

const TASKS_PATH = "g3ku/web/frontend/org_graph_tasks.js";
const TASKS_CODE = fs.readFileSync(TASKS_PATH, "utf8");

function loadNoteHelper() {
    const context = {
        console,
        Number,
        String,
        Math,
        formatTokenCount: (value) => String(value),
    };
    const start = TASKS_CODE.indexOf("function taskModelCallLedgerNote");
    const end = TASKS_CODE.indexOf("function renderTaskTokenStats");
    assert.ok(start > 0 && end > start, "ledger note helper slice not found");
    vm.createContext(context);
    vm.runInContext(TASKS_CODE.slice(start, end), context);
    return context;
}

test("真总数按计数说，带回一窗时明说只带最近多少条", () => {
    const { taskModelCallLedgerNote } = loadNoteHelper();
    assert.equal(
        taskModelCallLedgerNote(300, 31686, 100),
        "任务开始以来共 31686 次调用 · 明细带最近 300 条 · 每页 100 条",
    );
});

test("账本没超出窗口时不多余声明", () => {
    const { taskModelCallLedgerNote } = loadNoteHelper();
    assert.equal(taskModelCallLedgerNote(42, 42, 100), "任务开始以来共 42 次调用 · 每页 100 条");
});

test("缺计数时不声称任务开始以来", () => {
    const { taskModelCallLedgerNote } = loadNoteHelper();
    assert.equal(taskModelCallLedgerNote(42, undefined, 100), "已收到 42 次调用明细 · 每页 100 条");
    assert.equal(taskModelCallLedgerNote(42, 0, 100), "已收到 42 次调用明细 · 每页 100 条");
});

test("没有分页就整段省略分页说明", () => {
    const { taskModelCallLedgerNote } = loadNoteHelper();
    assert.equal(taskModelCallLedgerNote(5, 5, 0), "任务开始以来共 5 次调用");
    assert.equal(taskModelCallLedgerNote(0, 0, 0), "已收到 0 次调用明细");
});
