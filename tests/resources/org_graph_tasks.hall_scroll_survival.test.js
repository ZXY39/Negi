const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

// 任务大厅滚动存活契约：
// 1) worker 心跳时间戳（workerLastSeenAt）不参与渲染签名——它每秒变化但不改变
//    任何像素，进签名会让每次心跳都整网格重建、滚动位置清零，大厅滚动形同卡死；
// 2) 合法重建（状态/数据变化）后必须还原同页滚动位置；翻页才从顶部开始。

const TASKS_PATH = "g3ku/web/frontend/org_graph_tasks.js";
const TASKS_CODE = fs.readFileSync(TASKS_PATH, "utf8");

function makeStubEl() {
    return {
        className: "",
        style: {},
        dataset: {},
        innerHTML: "",
        textContent: "",
        appendChild: () => {},
        addEventListener: () => {},
        querySelector: () => null,
        querySelectorAll: () => [],
    };
}

function loadRenderModule({ diskMetrics = null } = {}) {
    const task = { task_id: "task:1", title: "T1" };
    const context = {
        console,
        Promise,
        S: {
            taskPageSize: 20,
            taskPage: 1,
            tasks: [task],
            tasksWorkerState: "online",
            tasksWorkerLastSeenAt: "t1",
            taskBusy: false,
            multiSelectMode: false,
            selectedTaskIds: new Set(),
            taskGridSignature: "",
            taskHallStats: {},
            taskMetricSnapshot: {},
            taskMetricAnimationTaskIds: new Set(),
            pendingTaskCardPatchIds: new Set(),
            taskCardPatchQueuedAt: {},
            visibleTaskIds: [],
        },
        U: {
            taskGrid: { innerHTML: "", scrollTop: 0, appendChild: () => {} },
        },
        paginateResources: (items, page) => ({ currentPage: Number(page), items, total: items.length }),
        orderedTasks: (items) => items || [],
        syncTaskPagination: () => {},
        renderTaskPerformanceBar: () => {},
        updateTaskToolbar: () => {},
        icons: () => {},
        taskWorkerNoticeText: () => "",
        taskWorkerStatusMetrics: () => diskMetrics,
        taskWorkerControlsAvailable: () => true,
        normalizeTaskWorkerState: (s) => String(s || "").toLowerCase(),
        taskSessionEmptyText: () => "empty",
        esc: (v) => String(v ?? ""),
        taskStatusKey: () => "running",
        taskStatusLabel: () => "running",
        taskCreatedAtText: () => "2026-01-01",
        taskTokenDisplayUsage: () => ({ tracked: false }),
        taskCardActions: () => [],
        taskPauseHintMarkup: () => "",
        taskPauseHintState: () => null,
        formatTokenCount: (v) => String(v),
        document: { createElement: () => makeStubEl() },
        __task: task,
    };
    context.window = context;
    vm.createContext(context);
    const start = TASKS_CODE.indexOf("function taskGridRenderSignature");
    const end = TASKS_CODE.indexOf("async function loadTasks");
    assert.ok(start > 0 && end > start, "render slice not found");
    vm.runInContext(TASKS_CODE.slice(start, end), context);
    return context;
}

test("心跳时间戳变化不改变渲染签名；可见状态变化必须改变签名", () => {
    const context = loadRenderModule();
    const meta = context.paginateResources(context.S.tasks, 1);
    const sig1 = context.taskGridRenderSignature(meta);
    context.S.tasksWorkerLastSeenAt = "t2-later-heartbeat";
    const sig2 = context.taskGridRenderSignature(meta);
    assert.equal(sig1, sig2, "仅心跳时间戳变化不应触发整网格重建");
    context.S.tasksWorkerState = "offline";
    const sig3 = context.taskGridRenderSignature(meta);
    assert.notEqual(sig2, sig3, "worker 状态变化必须触发重建");
});

test("磁盘紧急态翻转必须能触发重建", () => {
    const context = loadRenderModule();
    const meta = context.paginateResources(context.S.tasks, 1);
    const sig1 = context.taskGridRenderSignature(meta);
    context.taskWorkerStatusMetrics = () => ({ disk_emergency_active: true });
    const sig2 = context.taskGridRenderSignature(meta);
    assert.notEqual(sig1, sig2);
});

test("同页重建还原滚动位置，数据变化导致的重建不把用户拽回顶部", () => {
    const context = loadRenderModule();
    context.U.taskGrid.scrollTop = 42;
    context.renderTasks();
    assert.equal(context.U.taskGrid.scrollTop, 42, "首次全量重建后应还原滚动位置");
    const firstSignature = context.S.taskGridSignature;
    assert.ok(firstSignature, "重建后应写入签名");
    // 任务数据变化 → 签名变化 → 再次全量重建，滚动位置仍需保住。
    context.U.taskGrid.scrollTop = 77;
    context.__task.title = "T1-renamed";
    context.renderTasks();
    assert.notEqual(context.S.taskGridSignature, firstSignature);
    assert.equal(context.U.taskGrid.scrollTop, 77, "数据变化重建后滚动位置不应归零");
});
