const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const { readFrontendSources } = require("./support/frontend_sources.js");

const TASKS_PATH = "g3ku/web/frontend/org_graph_tasks.js";
const TASKS_CODE = readFrontendSources(["tasks"]).join("\n");

function loadTasksModule({ requestTaskAction, selectedTasks = [] } = {}) {
    const context = {
        console,
        Promise,
        setTimeout,
        clearTimeout,
        window: {},
        S: {
            view: "tasks",
            currentTaskId: "",
            taskBusy: false,
            selectedTaskIds: new Set(selectedTasks.map((task) => task.task_id)),
        },
        U: {},
        __toasts: [],
        __deletedTaskIds: [],
        __loadTasksCalls: 0,
        __loadTaskDetailCalls: 0,
        __loadTaskArtifactsCalls: 0,
        __requests: [],
        __confirms: [],
        taskWorkerControlsAvailable: () => true,
        refreshTaskWorkerStatus: () => {},
        renderTasksIfVisible: () => {},
        // 勾选集合与确认框住在 app bundle 里，这里按同一形状桩掉：
        // getSelectedTasks 直接返回用例给的选中任务。
        getSelectedTasks: () => selectedTasks,
        canClearTemp: (task) => ["success", "failed"].includes(String(task?.status || "")),
        closeTaskMenus: () => {},
        openConfirm(options = {}) {
            context.__confirms.push(options);
        },
        formatTaskBytes(value) {
            const bytes = Number(value);
            if (!Number.isFinite(bytes)) return "--";
            if (bytes >= 1024 ** 2) return `${(bytes / 1024 ** 2).toFixed(1)}M`;
            return `${Math.round(bytes)}B`;
        },
        showToast(payload) {
            context.__toasts.push(payload);
        },
        ApiClient: {
            pauseTask: async (taskId, options) => (requestTaskAction || (async () => ({ ok: true })))(taskId, "pause", options),
            resumeTask: async (taskId, options) => (requestTaskAction || (async () => ({ ok: true })))(taskId, "resume", options),
            deleteTask: async (taskId, options) => (requestTaskAction || (async () => ({ ok: true })))(taskId, "delete", options),
            clearTaskTempFiles: async (taskId, options) => {
                context.__requests.push({ taskId, options });
                return (requestTaskAction || (async () => ({ removed_dirs: [], failed_dirs: [], freed_bytes: 0 })))(taskId, "clear_temp", options);
            },
            bulkDeleteTasks: async (taskIds, options) => (requestTaskAction || (async () => ({ items: [] })))(taskIds, "bulk-delete", options),
        },
        loadTasks: async () => {
            context.__loadTasksCalls += 1;
        },
        loadTaskDetail: async () => {
            context.__loadTaskDetailCalls += 1;
        },
        loadTaskArtifacts: async () => {
            context.__loadTaskArtifactsCalls += 1;
        },
        handleDeletedTasks(taskIds = []) {
            context.__deletedTaskIds = [...taskIds];
        },
        __pauseHintTaskIds: [],
        __clearedPauseHintTaskIds: [],
        beginTaskPauseHint(taskId) {
            context.__pauseHintTaskIds.push(taskId);
        },
        clearTaskPauseHint(taskId) {
            context.__clearedPauseHintTaskIds.push(taskId);
        },
    };
    context.window = context;
    vm.createContext(context);
    // performTaskBatchAction 住在 tasks/hall-store.js（bundle 的第一段），单独切它；
    // 任务侧仍从 taskActionText 切到 loadTaskDetail，避免把真实的 loadTasks 带进来盖掉桩。
    const batchStart = TASKS_CODE.indexOf("async function performTaskBatchAction");
    const batchEnd = TASKS_CODE.indexOf("function handleTaskEvent");
    const start = TASKS_CODE.indexOf("function taskActionText");
    const end = TASKS_CODE.indexOf("async function loadTaskDetail");
    vm.runInContext(
        TASKS_CODE.slice(batchStart, batchEnd) + "\n" + TASKS_CODE.slice(start, end),
        context,
    );
    return context;
}

test("performTaskBatchAction shows a readable delete success toast", async () => {
    const context = loadTasksModule({
        requestTaskAction: async (taskIds, action) => {
            assert.equal(action, "bulk-delete");
            assert.deepEqual(taskIds, ["task:1", "task:2"]);
            return {
                items: [
                    { task_id: "task:1", result: "deleted" },
                    { task_id: "task:2", result: "deleted" },
                ],
            };
        },
    });

    await context.performTaskBatchAction("delete", [
        { task_id: "task:1" },
        { task_id: "task:2" },
    ]);

    assert.deepEqual(context.__deletedTaskIds, ["task:1", "task:2"]);
    assert.equal(context.__loadTasksCalls, 1);
    // \u5220\u9664\u8fd9\u6761\u662f\u78c1\u76d8\u65f6\u957f\u51b3\u5b9a\u7684\u52a8\u4f5c\uff1a\u5148\u6302\u4e00\u6761\u5e38\u9a7b\u8f6c\u5708\uff0c\u7ed3\u679c\u51fa\u6765\u624d\u6362\u6389\u3002
    assert.equal(context.__toasts[0].spin, true);
    assert.equal(context.__toasts[0].persistent, true);
    assert.equal(context.__toasts.at(-1)?.title, "\u5220\u9664\u6210\u529f");
    assert.equal(context.__toasts.at(-1)?.text, "\u5df2\u5220\u9664 2 \u4e2a\u4efb\u52a1");
    assert.equal(context.__toasts.at(-1)?.kind, "success");
});

test("performTaskBatchAction tracks pause success via per-card hints instead of a toast", async () => {
    const context = loadTasksModule();

    await context.performTaskBatchAction("pause", [
        { task_id: "task:1" },
        { task_id: "task:2" },
    ]);

    assert.equal(context.__loadTasksCalls, 1);
    assert.equal(context.__loadTaskDetailCalls, 0);
    assert.equal(context.__loadTaskArtifactsCalls, 0);
    // \u6279\u91cf\u6682\u505c\u4e0d\u518d\u5f39\u9876\u90e8\u6210\u529f toast\uff1a\u6210\u529f\u53cd\u9988\u7531\u5404\u5361\u7247\u300c\u6682\u505c\u4e2d\u2192\u6682\u505c\u6210\u529f\u300d\u63d0\u793a\u627f\u62c5\u3002
    assert.equal(context.__toasts.length, 0);
    assert.deepEqual(context.__pauseHintTaskIds, ["task:1", "task:2"]);
});

test("performTaskBatchAction sends one bulk delete request for all eligible tasks", async () => {
    const calls = [];
    const context = loadTasksModule({
        requestTaskAction: async (taskIds, action, options) => {
            calls.push({ taskIds, action, options });
            return {
                items: taskIds.map((taskId) => ({ task_id: taskId, result: "deleted" })),
            };
        },
    });

    await context.performTaskBatchAction("delete", [
        { task_id: "task:1" },
        { task_id: "task:2" },
        { task_id: "task:3" },
        { task_id: "task:4" },
    ]);

    assert.equal(calls.length, 1);
    assert.equal(calls[0].action, "bulk-delete");
    assert.deepEqual(calls[0].taskIds, ["task:1", "task:2", "task:3", "task:4"]);
    assert.deepEqual(context.__deletedTaskIds, ["task:1", "task:2", "task:3", "task:4"]);
    assert.equal(context.__loadTasksCalls, 1);
    assert.equal(context.__toasts.at(-1)?.title, "\u5220\u9664\u6210\u529f");
    assert.equal(context.__toasts.at(-1)?.text, "\u5df2\u5220\u9664 4 \u4e2a\u4efb\u52a1");
    assert.equal(context.__toasts.at(-1)?.kind, "success");
});

test("runTaskBatchAction clear_temp confirms first and only clears eligible tasks", async () => {
    const selectedTasks = [
        { task_id: "task:1", status: "success" },
        { task_id: "task:2", status: "in_progress" },
        { task_id: "task:3", status: "failed" },
    ];
    const context = loadTasksModule({ selectedTasks });

    await context.runTaskBatchAction("clear_temp");

    assert.equal(context.__requests.length, 0, "\u786e\u8ba4\u524d\u4e0d\u5f97\u53d1\u51fa\u8bf7\u6c42");
    assert.equal(context.__confirms.length, 1);
    assert.equal(context.__confirms[0].title, "\u6e05\u9664\u4e34\u65f6\u6587\u4ef6");
    assert.equal(context.__confirms[0].confirmKind, "warn");

    await context.__confirms[0].onConfirm();
    // \u8fdb\u884c\u4e2d\u7684\u4efb\u52a1\u88ab\u632a\u5f00\uff1a\u53ea\u5269\u4e24\u6761\u7ec8\u6001\u4efb\u52a1\u53d1\u8bf7\u6c42\u3002
    assert.deepEqual(context.__requests.map((item) => item.taskId).sort(), ["task:1", "task:3"]);
    assert.equal(context.__requests[0].options.timeoutMs, 0);
});

test("batch clear_temp pushes progress onto one spinning toast and settles with the freed bytes", async () => {
    const selectedTasks = [{ task_id: "task:1" }, { task_id: "task:2" }];
    const context = loadTasksModule({
        selectedTasks,
        requestTaskAction: async (taskId) => (taskId === "task:1"
            ? { removed_dirs: ["temp/tasks/a"], failed_dirs: [], freed_bytes: 2 * 1024 * 1024 }
            : { removed_dirs: ["temp/tasks/b", "temp/tasks/c"], failed_dirs: ["temp/tasks/d"], freed_bytes: 1 * 1024 * 1024 }),
    });

    await context.performTaskBatchAction("clear_temp", selectedTasks);

    assert.equal(context.__toasts[0].title, "\u6b63\u5728\u6e05\u9664\u4e34\u65f6\u6587\u4ef6");
    assert.equal(context.__toasts[0].spin, true);
    assert.equal(context.__toasts[0].text, "\u5df2\u5904\u7406 0 / 2 \u4e2a");
    // \u9010\u4efb\u52a1\u8bf7\u6c42\u6bcf\u6536\u4e00\u6761\u5c31\u628a\u8ba1\u6570\u63a8\u5230\u540c\u4e00\u6761\u5e38\u9a7b toast \u4e0a\uff0c\u4e0d\u662f\u53e6\u8d77\u4e00\u6837\u3002
    assert.equal(context.__toasts[1].text, "\u5df2\u5904\u7406 1 / 2 \u4e2a");
    assert.equal(context.__toasts[2].text, "\u5df2\u5904\u7406 2 / 2 \u4e2a");
    const last = context.__toasts.at(-1);
    assert.equal(last.title, "\u90e8\u5206\u4e34\u65f6\u76ee\u5f55\u672a\u5220\u9664");
    assert.equal(last.kind, "warn");
    assert.equal(last.text, "\u5df2\u6e05\u9664 3 \u4e2a\u76ee\u5f55\uff0c\u91ca\u653e 3.0M\uff0c1 \u4e2a\u76ee\u5f55\u672a\u5220\u9664");
});

test("batch clear_temp says nothing was clearable instead of claiming bytes", async () => {
    const selectedTasks = [{ task_id: "task:1" }];
    const context = loadTasksModule({
        selectedTasks,
        requestTaskAction: async () => ({ removed_dirs: [], failed_dirs: [], freed_bytes: 0 }),
    });

    await context.performTaskBatchAction("clear_temp", selectedTasks);

    const last = context.__toasts.at(-1);
    assert.equal(last.title, "\u4e34\u65f6\u6587\u4ef6\u5df2\u6e05\u9664");
    assert.equal(last.kind, "success");
    assert.equal(last.text, "1 \u4e2a\u4efb\u52a1\u6ca1\u6709\u53ef\u6e05\u9664\u7684\u4e34\u65f6\u76ee\u5f55");
});

test("a rejected bulk delete replaces the spinning toast with an error toast", async () => {
    const context = loadTasksModule({
        requestTaskAction: async () => {
            throw new Error("HTTP 500");
        },
    });

    await context.performTaskBatchAction("delete", [{ task_id: "task:1" }]);

    assert.equal(context.__toasts[0].spin, true);
    const last = context.__toasts.at(-1);
    assert.equal(last.kind, "error");
    assert.equal(last.title, "\u5220\u9664\u5931\u8d25");
    assert.equal(last.text, "HTTP 500");
    assert.equal(context.S.taskBusy, false, "\u5fd9\u6001\u8981\u968f\u5931\u8d25\u4e00\u8d77\u6536\u6389");
});

test("batch clear_temp whose every request failed ends the spinning toast with the first error", async () => {
    const selectedTasks = [{ task_id: "task:1" }, { task_id: "task:2" }];
    const context = loadTasksModule({
        selectedTasks,
        requestTaskAction: async () => {
            throw new Error("task_not_terminal");
        },
    });

    await context.performTaskBatchAction("clear_temp", selectedTasks);

    const last = context.__toasts.at(-1);
    assert.equal(last.kind, "error");
    assert.equal(last.title, "\u6e05\u9664\u4e34\u65f6\u6587\u4ef6\u5931\u8d25");
    // \u540e\u7aef\u95e8\u7684\u4e2d\u6587\u5224\u636e\u8981\u7167\u65e7\u7ffb\u8bd1\uff0c\u4e0d\u80fd\u56e0\u4e3a\u8d70\u4e86\u6279\u91cf\u8f66\u9053\u5c31\u6f0f\u539f\u6587\u3002
    assert.equal(last.text, "\u4ec5\u5df2\u5b8c\u6210\u6216\u5931\u8d25\u7684\u4efb\u52a1\u53ef\u6e05\u9664\u4e34\u65f6\u6587\u4ef6");
});
