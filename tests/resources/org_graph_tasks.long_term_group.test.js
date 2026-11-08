const test = require("node:test");
const assert = require("node:assert/strict");
const vm = require("node:vm");
const { readFrontendSources } = require("./support/frontend_sources.js");

// 任务大厅长期任务分组契约：
// 1) 长期（根节点 continuous）任务与一次性任务互斥展示：折叠时只渲染一次性任务，
//    展开时只渲染长期任务；
// 2) 折叠条常驻并报「共 N 个长期任务，运行中 N，待命 N」；
// 3) 展开态长期任务收进带底色的面板，折叠条成为面板头。

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

function walk(element, predicate, out = []) {
    (element.children || []).forEach((child) => {
        if (predicate(child)) out.push(child);
        walk(child, predicate, out);
    });
    return out;
}

function cardIds(context) {
    return walk(context.U.taskGrid, (child) => String(child.dataset?.taskId || ""))
        .map((child) => child.dataset.taskId);
}

function groupBar(context) {
    return walk(context.U.taskGrid, (child) => child.dataset?.taskLongTermToggle)[0] || null;
}

function byClass(context, name) {
    return walk(context.U.taskGrid, (child) => String(child.className || "").split(/\s+/).includes(name))[0] || null;
}

const tasks = [
    { task_id: "off1", title: "一次性 1", status: "success" },
    { task_id: "lt1", title: "长期 1", status: "in_progress", continuous: true },
    { task_id: "off2", title: "一次性 2", status: "in_progress" },
    { task_id: "lt2", title: "长期 2", status: "in_progress", standby: true, continuous: true },
];

test("长期任务统计与分组只认根节点 continuous 位", () => {
    const context = loadRenderModule(tasks);
    const stats = context.taskLongTermStats(tasks);
    assert.equal(stats.total, 2);
    assert.equal(stats.running, 1);
    assert.equal(stats.standby, 1);
    assert.equal(context.taskLongTermGroupText(stats), "共 2 个长期任务，运行中 1，待命 1");
    const grouped = context.taskLongTermGroups(tasks);
    assert.equal(grouped.longTerm.map((task) => task.task_id).join(","), "lt1,lt2");
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

test("展开后只渲染长期任务，并收进浅灰面板", () => {
    const context = loadRenderModule(tasks);
    context.S.taskLongTermExpanded = true;
    context.renderTasks();
    const bar = groupBar(context);
    const section = byClass(context, "task-long-term-section");
    const inner = byClass(context, "task-long-term-grid");
    assert.ok(section, "展开态必须有长期任务面板");
    assert.ok(!String(section.className).includes("is-hint"), "默认不常亮提示色");
    assert.ok(inner, "面板内必须有独立的卡片网格");
    assert.ok(section.children.includes(bar), "折叠条是面板头");
    assert.ok(bar.innerHTML.includes("chevron-up"), bar.innerHTML);
    assert.equal(bar.getAttribute("aria-expanded"), "true");
    assert.deepEqual(cardIds(context), ["lt1", "lt2"], "展开时不得出现一次性任务");
    assert.equal(context.lastMeta.total, 2, "展开时分页总数只算长期任务");
});

test("展开瞬间才挂提示色，1 秒窗口结束后回到浅灰", () => {
    const context = loadRenderModule(tasks);
    context.S.taskLongTermExpanded = true;
    context.S.taskLongTermHintUntil = Date.now() + 1000;
    context.renderTasks();
    assert.ok(byClass(context, "task-long-term-section").className.includes("is-hint"));
    context.S.taskLongTermHintUntil = 0;
    context.S.taskGridSignature = "";
    context.renderTasks();
    const section = byClass(context, "task-long-term-section");
    assert.ok(section);
    assert.ok(!String(section.className).includes("is-hint"));
});

test("折叠收起后回到一次性任务，长期任务卡片重新隐藏", () => {
    const context = loadRenderModule(tasks);
    context.S.taskLongTermExpanded = true;
    context.renderTasks();
    assert.deepEqual(cardIds(context), ["lt1", "lt2"]);
    context.S.taskLongTermExpanded = false;
    context.renderTasks();
    assert.deepEqual(cardIds(context), ["off1", "off2"]);
    assert.equal(byClass(context, "task-long-term-section"), null);
    assert.ok(groupBar(context).innerHTML.includes("chevron-down"));
});
