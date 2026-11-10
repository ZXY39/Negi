const test = require("node:test");
const assert = require("node:assert/strict");
const vm = require("node:vm");
const { readFrontendSources, resolveFrontendSource } = require("./support/frontend_sources.js");

// 任务卡片「开始」后面那一格的契约：
// 1) 终态印「结束」+ finished_at（既有行为不变）；
// 2) 待命印「最近」+ last_run_at，不借用 finished_at（待命不是终态）；
// 3) 运行/暂停两档留空；
// 4) 待命翻面必须走整卡重建——增量补丁车道不重画徽标与这一格，而翻面时
//    status 仍是 in_progress、is_unread 也可能早已为真，两个旧判据都放行。
// 5) 标签与「结束」同字数（共用行宽预算），截断时全值由 title 兜住。

const TASKS_CODE = readFrontendSources(["tasks"]).join("\n");
const APP_CODE = require("node:fs").readFileSync(resolveFrontendSource("org_graph_app.js"), "utf8");
const LAST_RUN = "2026-11-10T09:12:13+08:00";

function makeStubEl() {
    return {
        className: "",
        style: {},
        dataset: {},
        innerHTML: "",
        textContent: "",
        appendChild: () => {},
        addEventListener: () => {},
        setAttribute: () => {},
        querySelector: () => null,
        querySelectorAll: () => [],
    };
}

// 与 org_graph_app.js::taskStatusKey 同口径的最小桩：待命是 in_progress 的派生档。
function statusKey(task) {
    if (!task) return "unknown";
    if (task.is_paused) return "blocked";
    if (task.status === "in_progress" && task.standby) return "standby";
    return String(task.status || "unknown");
}

function loadModule(tasks) {
    const created = [];
    const context = {
        console,
        Promise,
        JSON,
        Number,
        String,
        Math,
        Date,
        Set,
        Map,
        Array,
        Object,
        Boolean,
        CSS: { escape: (v) => String(v) },
        S: {
            taskPageSize: 20,
            taskPage: 1,
            tasks,
            tasksById: Object.fromEntries(tasks.map((task) => [task.task_id, task])),
            orderedTaskIds: tasks.map((task) => task.task_id),
            visibleTaskIds: tasks.map((task) => task.task_id),
            tasksWorkerState: "online",
            taskBusy: false,
            multiSelectMode: false,
            selectedTaskIds: new Set(),
            taskGridSignature: "",
            taskHallStats: {},
            taskMetricSnapshot: {},
            taskMetricAnimationTaskIds: new Set(),
            pendingTaskCardPatchIds: new Set(),
            taskCardPatchQueuedAt: {},
        },
        U: {
            taskGrid: { innerHTML: "", scrollTop: 0, appendChild: () => {} },
            viewTasks: { classList: { contains: () => true } },
        },
        paginateResources: (items, page) => ({ currentPage: Number(page), items, total: items.length }),
        orderedTasks: (items) => items || [],
        syncTaskPagination: () => {},
        renderTaskPerformanceBar: () => {},
        updateTaskToolbar: () => {},
        icons: () => {},
        taskWorkerNoticeText: () => "",
        taskWorkerStatusMetrics: () => null,
        taskWorkerControlsAvailable: () => true,
        normalizeTaskWorkerState: (s) => String(s || "").toLowerCase(),
        taskSessionEmptyText: () => "empty",
        esc: (v) => String(v ?? ""),
        formatSessionTime: (v) => `fmt:${v}`,
        taskStatusKey: statusKey,
        taskStatusLabel: (task) => statusKey(task),
        taskTokenDisplayUsage: () => ({ tracked: false }),
        taskCardActions: () => [],
        taskPauseHintMarkup: () => "",
        taskPauseHintState: () => null,
        formatTokenCount: (v) => String(v),
        setTimeout: () => 1,
        clearTimeout: () => {},
        document: {
            visibilityState: "visible",
            createElement: () => {
                const el = makeStubEl();
                created.push(el);
                return el;
            },
        },
        __created: created,
    };
    context.window = context;
    vm.createContext(context);
    const renderStart = TASKS_CODE.indexOf("function renderTasks");
    const renderEnd = TASKS_CODE.indexOf("function renderTaskTokenStats");
    const patchStart = TASKS_CODE.indexOf("function patchTaskListItem");
    const patchEnd = TASKS_CODE.indexOf("function removeTaskListItem");
    const start = TASKS_CODE.indexOf("function syncTaskSelection");
    const end = TASKS_CODE.indexOf("async function loadTasks");
    // 补丁合格判据引用 app 模块的四个取值谓词，取真源码而不是手写桩，避免判据漂移。
    const predicateStart = APP_CODE.indexOf("const taskFailureClass");
    const predicateEnd = APP_CODE.indexOf("const taskIsUnpassed");
    assert.ok(renderStart > 0 && renderEnd > renderStart, "renderTasks slice not found");
    assert.ok(patchStart > 0 && patchEnd > patchStart, "patchTaskListItem slice not found");
    assert.ok(start > 0 && end > start, "tasks helper slice not found");
    assert.ok(predicateStart > 0 && predicateEnd > predicateStart, "card predicate slice not found");
    vm.runInContext(
        APP_CODE.slice(predicateStart, predicateEnd)
        + "\n" + TASKS_CODE.slice(renderStart, renderEnd)
        + "\n" + TASKS_CODE.slice(patchStart, patchEnd)
        + "\n" + TASKS_CODE.slice(start, end),
        context,
    );
    return context;
}

function standbyTask(overrides = {}) {
    return {
        task_id: "task:standby",
        title: "待命任务",
        status: "in_progress",
        standby: true,
        is_unread: true,
        created_at: "2026-11-01T08:00:00+08:00",
        updated_at: "2026-11-10T12:00:00+08:00",
        last_run_at: LAST_RUN,
        finished_at: "",
        ...overrides,
    };
}

function cardHtml(context, taskId) {
    const card = context.__created.find((el) => el.dataset?.taskId === taskId);
    assert.ok(card, `卡片 ${taskId} 应被创建`);
    return String(card.innerHTML || "");
}

test("待命档在结束时间那一格印「最近」，取值来自 last_run_at", () => {
    const context = loadModule([standbyTask()]);
    const markup = context.taskCardTimeSlotMarkup(standbyTask());
    assert.match(markup, /最近/);
    assert.match(markup, /data-task-last-run-at/);
    assert.ok(markup.includes(`fmt:${LAST_RUN}`), "必须印 last_run_at 而不是 updated_at");
    assert.ok(!markup.includes("结束"), "待命不是终态，不印结束");
    assert.ok(!markup.includes("data-task-finished-at"));
    assert.ok(!markup.includes("fmt:2026-11-10T12:00:00+08:00"), "不得退到 updated_at");
});

test("终态档仍印「结束」+ finished_at，运行与暂停两档留空", () => {
    const context = loadModule([standbyTask()]);
    const terminal = context.taskCardTimeSlotMarkup(standbyTask({
        status: "success",
        standby: false,
        finished_at: "2026-11-09T18:00:00+08:00",
    }));
    assert.match(terminal, /结束/);
    assert.match(terminal, /data-task-finished-at/);
    assert.ok(terminal.includes("fmt:2026-11-09T18:00:00+08:00"));
    assert.ok(!terminal.includes("最近"));
    assert.equal(context.taskCardTimeSlotMarkup(standbyTask({ standby: false })), "");
    assert.equal(context.taskCardTimeSlotMarkup(standbyTask({ standby: false, is_paused: true })), "");
});

test("待命但缺 last_run_at 时占位为暂无，不编造时间", () => {
    const context = loadModule([standbyTask()]);
    const markup = context.taskCardTimeSlotMarkup(standbyTask({ last_run_at: "" }));
    assert.match(markup, /最近/);
    assert.match(markup, /暂无/);
    assert.ok(!markup.includes("fmt:"), "缺读数时不得拿别的时间字段顶上");
});

test("「最近」标签与「结束」同字数，截断时全值由 title 兜住", () => {
    const context = loadModule([standbyTask()]);
    const labelOf = (markup) => (markup.match(/<span class="pc-field-label pc-field-label--end">([^<]*)<\/span>/) || [])[1] || "";
    const standbyLabel = labelOf(context.taskCardTimeSlotMarkup(standbyTask()));
    const terminalLabel = labelOf(context.taskCardTimeSlotMarkup(standbyTask({
        status: "success",
        standby: false,
        finished_at: LAST_RUN,
    })));
    assert.equal(standbyLabel, "最近");
    // 实测：四字标签会把 370–410px 卡宽这一带的时间挤成省略号，而终态行同宽度不裁。
    assert.equal(standbyLabel.length, terminalLabel.length, "两档标签共用同一份行宽预算，字数必须相等");
    const markup = context.taskCardTimeSlotMarkup(standbyTask());
    assert.ok(markup.includes(`title="fmt:${LAST_RUN}"`), "窄屏截断时 title 要能读出完整时间");
});

test("整卡重建入口：待命卡片的时间行真的带上最近读数", () => {
    const context = loadModule([standbyTask()]);
    context.renderTasks();
    const html = cardHtml(context, "task:standby");
    const row = html.split('<div class="pc-created-at">')[1]?.split("</div>")[0] || "";
    assert.ok(row, "时间行应存在");
    assert.match(row, /开始/);
    assert.match(row, /最近/);
    assert.ok(row.includes(`fmt:${LAST_RUN}`), "读数要活过卡片模板这一层");
});

test("运行中卡片的时间行只有开始，没有结束也没有最近", () => {
    const context = loadModule([standbyTask({ task_id: "task:running", standby: false })]);
    context.renderTasks();
    const html = cardHtml(context, "task:running");
    const row = html.split('<div class="pc-created-at">')[1]?.split("</div>")[0] || "";
    assert.match(row, /开始/);
    assert.ok(!row.includes("最近"));
    assert.ok(!row.includes("结束"));
});

test("待命翻面不许走增量补丁车道（is_unread 早已为真时两个旧判据都放行）", () => {
    const context = loadModule([standbyTask()]);
    const running = standbyTask({ standby: false });
    const parked = standbyTask();
    assert.equal(
        context.taskCardPatchEligible(running, parked),
        false,
        "进入待命换了徽标与时间格，必须整卡重建",
    );
    assert.equal(context.taskCardPatchEligible(parked, running), false, "离开待命同样要重建");
    assert.equal(
        context.taskCardPatchEligible(parked, standbyTask({ token_usage: { input_tokens: 99 } })),
        true,
        "待命期间的纯 token 变化仍走补丁车道，不该被这条判据拖成整网格重建",
    );
});

test("WS 摘要补丁入口：待命翻面触发整卡重建且读数落进状态层", () => {
    const context = loadModule([standbyTask({ standby: false })]);
    let rebuilds = 0;
    context.renderTasks = () => { rebuilds += 1; };
    context.patchTaskListItem({
        task_id: "task:standby",
        status: "in_progress",
        standby: true,
        is_unread: true,
        last_run_at: LAST_RUN,
    });
    assert.equal(rebuilds, 1, "待命翻面必须重建，否则徽标与最近格都不刷新");
    assert.equal(context.S.tasksById["task:standby"].last_run_at, LAST_RUN);
    assert.equal(context.S.tasksById["task:standby"].standby, true);
    // 对照臂：待命期间的纯 token 补丁仍走增量车道，不重建。
    context.patchTaskListItem({
        task_id: "task:standby",
        token_usage: { input_tokens: 123 },
    });
    assert.equal(rebuilds, 1, "纯 token 补丁不该触发整网格重建");
});