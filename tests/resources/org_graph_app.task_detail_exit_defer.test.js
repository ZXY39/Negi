const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

// 任务详情返回大厅的时序契约：详情退出的重步骤（视图状态捕获、大 DOM 拆除、
// 持久化调度）推迟到下一个宏任务，时序对齐「侧栏绕行两跳回大厅」——点击帧
// 只做视图切换、掐 live 流与发起大厅请求（/api/tasks、/api/ws/tasks、
// worker-status），大片冷内存的集中触碰不与大厅重绘同帧叠加。
// 快速来回切换（返回后立刻再开任务）时推迟的拆除整体作废。见 switchView
// 的 leavingTaskDetails 分支。

const APP_PATH = "g3ku/web/frontend/org_graph_app.js";
const APP_CODE = fs.readFileSync(APP_PATH, "utf8");

function makeEl() {
    const classes = new Set();
    return {
        style: {},
        classList: {
            toggle: (name, on) => {
                if (on) classes.add(name);
                else classes.delete(name);
                return !!on;
            },
            contains: (name) => classes.has(name),
            add: (name) => classes.add(name),
            remove: (name) => classes.delete(name),
        },
    };
}

function loadSwitchView({ detailActive = true } = {}) {
    const calls = { order: [] };
    const record = (name) => (...args) => {
        calls.order.push(name);
        calls[name] = (calls[name] || 0) + 1;
    };
    const context = {
        console,
        Promise,
        setTimeout,
        clearTimeout,
        S: {
            view: "tasks",
            memoryNotePreview: { open: false },
            memoryDetailPreview: { open: false },
            memoryBrowser: { open: false },
        },
        U: {
            nav: [],
            viewCeo: makeEl(),
            viewTasks: makeEl(),
            viewSkills: makeEl(),
            viewTools: makeEl(),
            viewMemory: makeEl(),
            viewModels: makeEl(),
            viewExternal: makeEl(),
            viewTaskDetails: makeEl(),
        },
        cancelTaskTreeLoading: record("cancelTaskTreeLoading"),
        stashTaskDetailViewState: record("stashTaskDetailViewState"),
        setTaskTokenStatsOpen: record("setTaskTokenStatsOpen"),
        clearAgentSelection: record("clearAgentSelection"),
        closeTaskDetailWs: record("closeTaskDetailWs"),
        scheduleTaskDetailSessionPersist: record("scheduleTaskDetailSessionPersist"),
        releaseTaskDetailRetainedState: record("releaseTaskDetailRetainedState"),
        loadTasks: record("loadTasks"),
        initTasksWs: record("initTasksWs"),
        ensureTaskListVisibleReconcile: record("ensureTaskListVisibleReconcile"),
        closeTasksWs: record("closeTasksWs"),
        startTaskWorkerStatusPolling: record("startTaskWorkerStatusPolling"),
        stopTaskWorkerStatusPolling: record("stopTaskWorkerStatusPolling"),
        closeMemoryNotePreview: record("closeMemoryNotePreview"),
        closeMemoryDetailPreview: record("closeMemoryDetailPreview"),
        closeMemoryBrowser: record("closeMemoryBrowser"),
        startMemoryViewAutoRefresh: record("startMemoryViewAutoRefresh"),
        stopMemoryViewAutoRefresh: record("stopMemoryViewAutoRefresh"),
        startAuditViewAutoRefresh: record("startAuditViewAutoRefresh"),
        stopAuditViewAutoRefresh: record("stopAuditViewAutoRefresh"),
        loadSkills: record("loadSkills"),
        loadTools: record("loadTools"),
        loadToolGovernanceMode: record("loadToolGovernanceMode"),
        loadMemoryView: record("loadMemoryView"),
        loadModels: record("loadModels"),
        loadExternalApiView: record("loadExternalApiView"),
        __calls: calls,
    };
    if (detailActive) context.U.viewTaskDetails.classList.add("active");
    context.window = context;
    vm.createContext(context);
    const start = APP_CODE.indexOf("function switchView(view)");
    const end = APP_CODE.indexOf("function toggleTheme()");
    assert.ok(start > 0 && end > start, "switchView slice not found");
    vm.runInContext(APP_CODE.slice(start, end), context);
    return context;
}

const nextTick = () => new Promise((resolve) => setTimeout(resolve, 5));

test("箭头返回大厅：点击帧内发起大厅请求并掐掉 live 流，详情拆除推迟到下一宏任务", async () => {
    const context = loadSwitchView({ detailActive: true });
    context.switchView("tasks");
    const calls = context.__calls;
    // 点击帧：live 流已掐断、大厅请求已发起，重拆除尚未执行。
    assert.equal(calls.closeTaskDetailWs, 1);
    assert.equal(calls.loadTasks, 1);
    assert.equal(calls.initTasksWs, 1);
    assert.equal(calls.startTaskWorkerStatusPolling, 1);
    assert.equal(calls.stashTaskDetailViewState, undefined);
    assert.equal(calls.scheduleTaskDetailSessionPersist, undefined);
    assert.equal(calls.releaseTaskDetailRetainedState, undefined);
    await nextTick();
    // 下一宏任务：状态捕获、抽屉关闭、持久化调度、大状态拆除依次落位。
    assert.equal(calls.stashTaskDetailViewState, 1);
    assert.equal(calls.setTaskTokenStatsOpen, 1);
    assert.equal(calls.clearAgentSelection, 1);
    assert.equal(calls.scheduleTaskDetailSessionPersist, 1);
    assert.equal(calls.releaseTaskDetailRetainedState, 1);
    // 大厅请求先于拆除：与绕行两跳路径的时序一致。
    assert.ok(calls.order.indexOf("loadTasks") < calls.order.indexOf("releaseTaskDetailRetainedState"));
});

test("快速来回切换：推迟的拆除在重新进入详情后整体作废", async () => {
    const context = loadSwitchView({ detailActive: true });
    context.switchView("tasks");
    // 同一 tick 内用户又点开任务：详情视图重新激活。
    context.switchView("task-details");
    await nextTick();
    const calls = context.__calls;
    assert.equal(calls.releaseTaskDetailRetainedState, undefined);
    assert.equal(calls.stashTaskDetailViewState, undefined);
    assert.equal(calls.scheduleTaskDetailSessionPersist, undefined);
});

test("从非详情视图离开：同步拆除路径保持不变", async () => {
    const context = loadSwitchView({ detailActive: false });
    context.switchView("tasks");
    const calls = context.__calls;
    assert.equal(calls.stashTaskDetailViewState, 1);
    assert.equal(calls.closeTaskDetailWs, 1);
    assert.equal(calls.scheduleTaskDetailSessionPersist, 1);
    assert.equal(calls.releaseTaskDetailRetainedState, 1);
});
