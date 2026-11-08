const test = require("node:test");
const assert = require("node:assert/strict");
const vm = require("node:vm");
const { readFrontendSources } = require("./support/frontend_sources.js");

// 任务大厅长期任务分组契约：
// 1) 长期（根节点 continuous）任务整组排在一次性任务前面，组内保持当前排序；
// 2) 默认为折叠态，折叠时只渲染一次性任务，折叠条报「共 N 个长期任务，运行中 N，待命 N」；
// 3) 折叠条可展开/收起，展开后长期任务卡片先于一次性任务出现。

const TASKS_CODE = readFrontendSources(["tasks"]).join("\n");

function makeElement() {
    return {
        className: "",
        style: {},
        dataset: {},
        textContent: "",
        children: [],
        attrs: {},
        _html: "",
        get innerHTML() { return this._html; },
        set innerHTML(value) { this._html = String(value); this.children = []; },
        appendChild(child) { this.children.push(child); return child; },
        addEventListener() {},
        setAttribute(name, value) { this.attrs[String(name)] = String(value); },
        getAttribute(name) { return this.attrs[String(name)]; },
        querySelector() { return null; },
        querySelectorAll() { return []; },
        closest() { return null; },
    };
}

function loadRenderModule(tasks) {
    const context = {
        console,
        Promise,
        Number,
        Math,
        String,
        JSON,
        S: {
            taskPageSize: 20,
            taskPage: 1,
            tasks,
            tasksById: {},
            orderedTaskIds: [],
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
            taskLongTermExpanded: false,
            taskSortMode: "time",
        },
        U: { taskGrid: makeElement() },
        paginateResources: (items, page, pageSize) => {
            const size = Number(pageSize) || 20;
            const total = items.length;
            const totalPages = Math.max(1, Math.ceil(total / size));
            const currentPage = Math.min(Math.max(1, Number(page) || 1), totalPages);
            const start = (currentPage - 1) * size;
            return {
                total,
                pageSize: size,
                totalPages,
                currentPage,
                startIndex: total ? start + 1 : 0,
                endIndex: Math.min(currentPage * size, total),
                items: items.slice(start, start + size),
            };
        },
        orderedTasks: (items) => [...(items || [])],
        syncTaskPagination: (meta) => { context.lastMeta = meta; },
        renderTaskPerformanceBar: () => {},
        updateTaskToolbar: () => {},
        icons: () => {},
        taskWorkerNoticeText: () => "",
        taskWorkerStatusMetrics: () => null,
        taskWorkerControlsAvailable: () => true,
        normalizeTaskWorkerState: (state) => String(state || "").toLowerCase(),
        taskSessionEmptyText: () => "empty",
        esc: (value) => String(value ?? ""),
        taskStatusKey: (task) => (
            task?.status === "in_progress" && task?.standby
                ? "standby"
                : (task?.status || "running")
        ),
        taskStatusLabel: () => "运行",
        taskCreatedAtText: () => "2026-01-01",
        taskTokenDisplayUsage: () => ({ tracked: false }),
        taskCardActions: () => [],
        taskPauseHintMarkup: () => "",
        taskPauseHintState: () => null,
        formatTokenCount: (value) => String(value),
        taskMetricValueMarkup: (text) => String(text),
        document: { createElement: () => makeElement() },
    };
    context.window = context;
    vm.createContext(context);
    const renderStart = TASKS_CODE.indexOf("function renderTasks");
    const renderEnd = TASKS_CODE.indexOf("function renderTaskTokenStats");
    const helperStart = TASKS_CODE.indexOf("function formatTaskBytes");
    const helperEnd = TASKS_CODE.indexOf("async function loadTasks");
    assert.ok(renderStart > 0 && renderEnd > renderStart, "renderTasks slice not found");
    assert.ok(helperStart > 0 && helperEnd > helperStart, "hall helper slice not found");
    vm.runInContext(TASKS_CODE.slice(renderStart, renderEnd), context);
    vm.runInContext(TASKS_CODE.slice(helperStart, helperEnd), context);
    return context;
}

function cardIds(context) {
    return context.U.taskGrid.children
        .filter((child) => String(child.dataset?.taskId || ""))
        .map((child) => child.dataset.taskId);
}

function groupBar(context) {
    return context.U.taskGrid.children.find((child) => child.dataset?.taskLongTermToggle);
}

const tasks = [
    { task_id: "off1", title: "一次性 1", status: "success" },
    { task_id: "lt1", title: "长期 1", status: "in_progress", continuous: true },
    { task_id: "off2", title: "一次性 2", status: "in_progress" },
    { task_id: "lt2", title: "长期 2", status: "in_progress", standby: true, continuous: true },
];

test("长期任务统计与分组顺序只认根节点 continuous 位", () => {
    const context = loadRenderModule(tasks);
    const stats = context.taskLongTermStats(tasks);
    assert.equal(stats.total, 2);
    assert.equal(stats.running, 1);
    assert.equal(stats.standby, 1);
    assert.equal(context.taskLongTermGroupText(stats), "共 2 个长期任务，运行中 1，待命 1");
    const grouped = context.taskLongTermOrderedItems(tasks);
    assert.equal(grouped.ordered.map((task) => task.task_id).join(","), "lt1,lt2,off1,off2");
    assert.equal(grouped.oneOff.map((task) => task.task_id).join(","), "off1,off2");
});

test("默认折叠：只渲染一次性任务，折叠条显示长期任务统计与向下箭头", () => {
    const context = loadRenderModule(tasks);
    context.renderTasks();
    const bar = groupBar(context);
    assert.ok(bar, "有长期任务时必须渲染折叠条");
    assert.ok(bar.innerHTML.includes("共 2 个长期任务，运行中 1，待命 1"), bar.innerHTML);
    assert.ok(bar.innerHTML.includes("chevron-down"), bar.innerHTML);
    assert.equal(bar.getAttribute("aria-expanded"), "false");
    assert.deepEqual(cardIds(context), ["off1", "off2"]);
    assert.equal(context.lastMeta.total, 2, "折叠时分页总数只算一次性任务");
});

test("展开后长期任务整组排在一次性任务前面，折叠条换成向上箭头", () => {
    const context = loadRenderModule(tasks);
    context.S.taskLongTermExpanded = true;
    context.renderTasks();
    const bar = groupBar(context);
    assert.ok(bar.innerHTML.includes("chevron-up"), bar.innerHTML);
    assert.equal(bar.getAttribute("aria-expanded"), "true");
    assert.deepEqual(cardIds(context), ["lt1", "lt2", "off1", "off2"]);
    assert.equal(context.lastMeta.total, 4, "展开时分页总数包含长期任务");
});

test("折叠收起后长期任务卡片重新隐藏", () => {
    const context = loadRenderModule(tasks);
    context.S.taskLongTermExpanded = true;
    context.renderTasks();
    assert.deepEqual(cardIds(context), ["lt1", "lt2", "off1", "off2"]);
    context.S.taskLongTermExpanded = false;
    context.renderTasks();
    assert.deepEqual(cardIds(context), ["off1", "off2"]);
    assert.ok(groupBar(context).innerHTML.includes("chevron-down"));
});
