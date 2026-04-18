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
            pauseTask: async (taskId) => (requestTaskAction || (async () => ({ ok: true })))(taskId, "pause"),
            resumeTask: async (taskId) => (requestTaskAction || (async () => ({ ok: true })))(taskId, "resume"),
            deleteTask: async (taskId) => (requestTaskAction || (async () => ({ ok: true })))(taskId, "delete"),
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
    const context = loadTasksModule();

    await context.performTaskBatchAction("delete", [
        { task_id: "task:1" },
        { task_id: "task:2" },
    ]);

    assert.deepEqual(context.__deletedTaskIds, ["task:1", "task:2"]);
    assert.equal(context.__loadTasksCalls, 1);
    assert.equal(context.__toasts.at(-1)?.title, "删除成功");
    assert.equal(context.__toasts.at(-1)?.text, "已删除 2 个任务");
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
    assert.equal(context.__toasts.at(-1)?.title, "暂停成功");
    assert.equal(context.__toasts.at(-1)?.text, "2 个任务已暂停");
    assert.equal(context.__toasts.at(-1)?.kind, "success");
});
