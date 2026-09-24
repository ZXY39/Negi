const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const TASKS_PATH = "g3ku/web/frontend/org_graph_tasks.js";
const TASKS_CODE = fs.readFileSync(TASKS_PATH, "utf8");

// 切片范围与 batch_action_toast 测试一致：taskActionText → loadTaskDetail。
// formatTaskBytes 在切片之前定义，浏览器里同为脚本级函数，这里按同义桩注入。
function loadTasksModule({ response, failure } = {}) {
    const context = {
        console,
        Promise,
        setTimeout,
        clearTimeout,
        window: {},
        S: { view: "tasks", currentTaskId: "", taskBusy: false },
        U: {},
        __toasts: [],
        __confirms: [],
        __requests: [],
        __deletedTaskIds: [],
        taskWorkerControlsAvailable: () => true,
        refreshTaskWorkerStatus: () => {},
        renderTasksIfVisible: () => {},
        beginTaskPauseHint: () => {},
        clearTaskPauseHint: () => {},
        handleDeletedTasks(taskIds = []) {
            context.__deletedTaskIds = [...taskIds];
        },
        showToast(payload) {
            context.__toasts.push(payload);
        },
        openConfirm(options = {}) {
            context.__confirms.push(options);
        },
        loadTasks: async () => {},
        loadTaskDetail: async () => {},
        loadTaskArtifacts: async () => {},
        formatTaskBytes(value) {
            const bytes = Number(value);
            if (!Number.isFinite(bytes)) return "--";
            if (bytes >= 1024 ** 2) return `${(bytes / 1024 ** 2).toFixed(1)}M`;
            return `${Math.round(bytes)}B`;
        },
        ApiClient: {
            pauseTask: async () => ({ ok: true }),
            resumeTask: async () => ({ ok: true }),
            deleteTask: async () => ({ ok: true }),
            clearTaskTempFiles: async (taskId, options) => {
                context.__requests.push({ taskId, options });
                if (failure) throw failure;
                return response;
            },
        },
    };
    context.window = context;
    vm.createContext(context);
    const start = TASKS_CODE.indexOf("function taskActionText");
    const end = TASKS_CODE.indexOf("async function loadTaskDetail");
    vm.runInContext(TASKS_CODE.slice(start, end), context);
    return context;
}

test("clear_temp asks for confirmation before issuing the request", async () => {
    const context = loadTasksModule({ response: { freed_bytes: 0, removed_dirs: [] } });

    await context.runTaskAction("task:1", "clear_temp");

    assert.equal(context.__requests.length, 0, "确认前不得发出请求");
    assert.equal(context.__confirms.length, 1);
    assert.equal(context.__confirms[0].title, "清除临时文件");
    assert.equal(context.__confirms[0].confirmKind, "warn");
    assert.match(context.__confirms[0].text, /任务记录、节点与产出保留/);

    await context.__confirms[0].onConfirm();
    assert.deepEqual(context.__requests.map((item) => item.taskId), ["task:1"]);
    assert.equal(context.__requests[0].options.timeoutMs, 30000);
});

test("clear_temp success toast reports removed dirs and freed bytes", async () => {
    const context = loadTasksModule({
        response: { removed_dirs: ["temp/tasks/task_1", "temp/tasks/elsewhere"], freed_bytes: 3 * 1024 * 1024 },
    });

    await context.performTaskAction("task:1", "clear_temp");

    const toast = context.__toasts.at(-1);
    assert.equal(toast.title, "临时文件已清除");
    assert.equal(toast.kind, "success");
    assert.equal(toast.text, "已清除 2 个目录，释放 3.0M");
    // 卡片不删除：只重载列表以刷新占用大小
    assert.deepEqual(context.__deletedTaskIds, []);
});

test("clear_temp with nothing to remove says so instead of claiming bytes", async () => {
    const context = loadTasksModule({ response: { removed_dirs: [], freed_bytes: 0 } });

    await context.performTaskAction("task:1", "clear_temp");

    assert.equal(context.__toasts.at(-1).text, "该任务没有可清除的临时目录");
});

test("clear_temp surfaces partial failures as a warning", async () => {
    const context = loadTasksModule({
        response: { removed_dirs: ["a"], failed_dirs: ["b"], freed_bytes: 1024 * 1024 },
    });

    await context.performTaskAction("task:1", "clear_temp");

    const toast = context.__toasts.at(-1);
    assert.equal(toast.title, "部分临时目录未删除");
    assert.equal(toast.kind, "warn");
});

test("clear_temp maps the backend gate to readable Chinese", () => {
    const context = loadTasksModule();

    assert.equal(
        context.taskActionErrorText("clear_temp", new Error("task_not_terminal")),
        "仅已完成或失败的任务可清除临时文件",
    );
    assert.equal(
        context.taskActionErrorText("clear_temp", new Error("task_not_found")),
        "任务不存在或已被删除",
    );
});
