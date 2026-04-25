const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const TASKS_PATH = "g3ku/web/frontend/org_graph_tasks.js";
const TASKS_CODE = fs.readFileSync(TASKS_PATH, "utf8");

function loadTasksModule({ requestTaskAction } = {}) {
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
        },
        U: {},
        __toasts: [],
        __deletedTaskIds: [],
        __loadTasksCalls: 0,
        __loadTaskDetailCalls: 0,
        __loadTaskArtifactsCalls: 0,
        taskWorkerControlsAvailable: () => true,
        refreshTaskWorkerStatus: () => {},
        renderTasksIfVisible: () => {},
        showToast(payload) {
            context.__toasts.push(payload);
        },
        ApiClient: {
            pauseTask: async (taskId, options) => (requestTaskAction || (async () => ({ ok: true })))(taskId, "pause", options),
            resumeTask: async (taskId, options) => (requestTaskAction || (async () => ({ ok: true })))(taskId, "resume", options),
            deleteTask: async (taskId, options) => (requestTaskAction || (async () => ({ ok: true })))(taskId, "delete", options),
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
    };
    context.window = context;
    vm.createContext(context);
    const start = TASKS_CODE.indexOf("function taskActionText");
    const end = TASKS_CODE.indexOf("async function loadTaskDetail");
    vm.runInContext(TASKS_CODE.slice(start, end), context);
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
    assert.equal(context.__toasts.at(-1)?.title, "\u5220\u9664\u6210\u529f");
    assert.equal(context.__toasts.at(-1)?.text, "\u5df2\u5220\u9664 2 \u4e2a\u4efb\u52a1");
    assert.equal(context.__toasts.at(-1)?.kind, "success");
});

test("performTaskBatchAction shows a readable pause success toast", async () => {
    const context = loadTasksModule();

    await context.performTaskBatchAction("pause", [
        { task_id: "task:1" },
        { task_id: "task:2" },
    ]);

    assert.equal(context.__loadTasksCalls, 1);
    assert.equal(context.__loadTaskDetailCalls, 0);
    assert.equal(context.__loadTaskArtifactsCalls, 0);
    assert.equal(context.__toasts.at(-1)?.title, "\u6682\u505c\u6210\u529f");
    assert.equal(context.__toasts.at(-1)?.text, "2 \u4e2a\u4efb\u52a1\u5df2\u6682\u505c");
    assert.equal(context.__toasts.at(-1)?.kind, "success");
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
