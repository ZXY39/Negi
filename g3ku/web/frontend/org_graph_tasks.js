// Task list, task detail loading, and task event orchestration extracted from org_graph_app.js.
// Loaded after org_graph_app.js and before org_graph_llm.js.

const TASK_SUMMARY_IDLE_RECONCILE_MS = 15_000;


function taskStatusLabel(task) {
    return ({ in_progress: "\u8fd0\u884c", success: "\u6210\u529f", failed: "\u5931\u8d25", blocked: "\u6682\u505c", unpassed: "\u672a\u901a\u8fc7", unknown: "\u672a\u77e5" })[taskStatusKey(task)] || "\u672a\u77e5";
}

function statusBucketMatches(task, bucketKey) {
    const status = taskStatusKey(task);
    if (bucketKey === "all") return true;
    if (bucketKey === "paused") return status === "blocked";
    if (bucketKey === "completed") return status === "success";
    if (bucketKey === "failed") return status === "failed";
    if (bucketKey === "unpassed") return status === "unpassed";
    if (bucketKey === "unread") return !!task?.is_unread;
    if (bucketKey === "running") return status === "in_progress";
    return false;
}

function getSelectedTasks() {
    return S.tasks.filter((task) => S.selectedTaskIds.has(task.task_id));
}

function setTaskMenuVisibility() {
    const filterOpen = !!(S.multiSelectMode && S.taskFilterMenuOpen);
    if (U.taskFilterMenu) U.taskFilterMenu.hidden = !filterOpen;
    U.taskFilterTrigger?.setAttribute("aria-expanded", filterOpen ? "true" : "false");
}

function closeTaskCardMenus({ restoreFocus = false } = {}) {
    const openMenus = [...(U.taskGrid?.querySelectorAll(".pc-card-menu-shell.is-open") || [])];
    let closed = false;
    openMenus.forEach((shell) => {
        shell.classList.remove("is-open");
        shell.querySelector(".pc-card-menu")?.setAttribute("hidden", "hidden");
        const trigger = shell.querySelector("[data-task-menu-toggle]");
        if (trigger) {
            trigger.setAttribute("aria-expanded", "false");
            if (restoreFocus && trigger instanceof HTMLElement) trigger.focus();
        }
        closed = true;
    });
    return closed;
}

function setTaskCardMenuOpen(taskId, open, { restoreFocus = false } = {}) {
    const targetId = String(taskId || "").trim();
    if (!targetId) return false;
    let matched = false;
    [...(U.taskGrid?.querySelectorAll(".pc-card-menu-shell[data-task-menu]") || [])].forEach((shell) => {
        const currentId = String(shell.dataset.taskMenu || "").trim();
        const shouldOpen = !!open && currentId === targetId;
        const trigger = shell.querySelector("[data-task-menu-toggle]");
        const menu = shell.querySelector(".pc-card-menu");
        if (currentId === targetId) matched = true;
        shell.classList.toggle("is-open", shouldOpen);
        if (menu) {
            if (shouldOpen) menu.removeAttribute("hidden");
            else menu.setAttribute("hidden", "hidden");
        }
        if (trigger) {
            trigger.setAttribute("aria-expanded", shouldOpen ? "true" : "false");
            if (!shouldOpen && restoreFocus && currentId === targetId && trigger instanceof HTMLElement) trigger.focus();
        }
    });
    return matched;
}

function setTaskMenuOpen(kind, open) {
    if (open) closeTaskCardMenus();
    S.taskFilterMenuOpen = kind === "filter" ? !!open : false;
    setTaskMenuVisibility();
}

function closeTaskMenus({ restoreFocus = false } = {}) {
    const hadToolbarOpen = !!S.taskFilterMenuOpen;
    S.taskFilterMenuOpen = false;
    setTaskMenuVisibility();
    const cardClosed = closeTaskCardMenus({ restoreFocus });
    return hadToolbarOpen || cardClosed;
}

function setMultiSelectMode(enabled) {
    S.multiSelectMode = !!enabled;
    if (!S.multiSelectMode) S.selectedTaskIds.clear();
    closeTaskMenus();
    renderTasks();
}

function clearTaskSelection() {
    S.multiSelectMode = false;
    S.selectedTaskIds.clear();
    closeTaskMenus();
    renderTasks();
}

function toggleTaskSelection(taskId) {
    if (S.selectedTaskIds.has(taskId)) S.selectedTaskIds.delete(taskId);
    else S.selectedTaskIds.add(taskId);
    renderTasks();
}

function syncTaskSelection() {
    const ids = new Set(S.tasks.map((task) => task.task_id));
    [...S.selectedTaskIds].forEach((id) => !ids.has(id) && S.selectedTaskIds.delete(id));
    if (!S.multiSelectMode) S.selectedTaskIds.clear();
}

function isTaskDetailVisible() {
    return !!U.viewTaskDetails?.classList.contains("active");
}

function handleDeletedTasks(taskIds = []) {
    const ids = new Set(taskIds.map((item) => String(item || "")));
    if (!ids.size) return;
    if (!ids.has(String(S.currentTaskId || ""))) return;
    S.currentTaskId = null;
    resetTaskView();
    if (isTaskDetailVisible()) switchView("tasks");
}

function taskSessionQueryValue() {
    return "all";
}

function formatTaskBytes(value) {
    const bytes = Number(value);
    if (!Number.isFinite(bytes) || bytes < 0) return "--";
    if (bytes >= 1024 ** 3) return `${(bytes / 1024 ** 3).toFixed(1)}G`;
    if (bytes >= 1024 ** 2) return `${(bytes / 1024 ** 2).toFixed(1)}M`;
    if (bytes >= 1024) return `${(bytes / 1024).toFixed(0)}K`;
    return `${Math.round(bytes)}B`;
}

function formatTaskNodeCount(value) {
    const count = Number(value);
    if (!Number.isFinite(count) || count < 0) return "--";
    return String(Math.trunc(count));
}

function taskDiskUsageSortValue(task) {
    const value = Number(task?.disk_usage_bytes);
    return Number.isFinite(value) && value > 0 ? value : 0;
}

function compareTaskListOrder(left, right) {
    if (S.taskSortMode === "size") {
        const sizeDiff = taskDiskUsageSortValue(right) - taskDiskUsageSortValue(left);
        if (sizeDiff !== 0) return sizeDiff;
    }
    const timeDiff = taskCreatedSortValue(right) - taskCreatedSortValue(left);
    if (timeDiff !== 0) return timeDiff;
    const rightCreatedAt = String(right?.created_at || "");
    const leftCreatedAt = String(left?.created_at || "");
    if (rightCreatedAt !== leftCreatedAt) return rightCreatedAt.localeCompare(leftCreatedAt);
    return String(left?.task_id || "").localeCompare(String(right?.task_id || ""));
}

function setTaskSortMode(mode) {
    const normalized = String(mode || "").trim().toLowerCase() === "size" ? "size" : "time";
    if (S.taskSortMode === normalized) return;
    S.taskSortMode = normalized;
    if (U.taskSortSelect && U.taskSortSelect.value !== normalized) U.taskSortSelect.value = normalized;
    resortTaskOrderedIds();
}

function resortTaskOrderedIds() {
    const tasksById = S.tasksById || {};
    S.orderedTaskIds = (Array.isArray(S.orderedTaskIds) ? [...S.orderedTaskIds] : [])
        .sort((leftId, rightId) => compareTaskListOrder(tasksById[leftId], tasksById[rightId]));
    syncTaskArrayFromState();
    S.taskGridSignature = "";
    renderTasksIfVisible();
}

function syncTaskNormalizedState(items = []) {
    const normalized = [...(Array.isArray(items) ? items : [])]
        .filter((item) => item && typeof item === "object" && String(item.task_id || "").trim())
        .sort(compareTaskListOrder);
    const tasksById = {};
    const orderedTaskIds = [];
    const heldById = S.tasksById || {};
    const sources = {};
    normalized.forEach((item) => {
        const taskId = String(item.task_id || "").trim();
        if (!taskId) return;
        sources[taskId] = "list";
        const heldTask = heldById[taskId] || null;
        const carriedUsage = resolveTaskTokenUsage(taskId, heldTask, item, "list-apply");
        tasksById[taskId] = carriedUsage && carriedUsage !== item.token_usage
            ? { ...item, token_usage: carriedUsage }
            : item;
        orderedTaskIds.push(taskId);
    });
    S.tasksById = tasksById;
    S.orderedTaskIds = orderedTaskIds;
    S.tasks = normalized;
    S.taskItemStateSource = sources;
    return normalized;
}

function syncTaskArrayFromState() {
    const items = (Array.isArray(S.orderedTaskIds) ? S.orderedTaskIds : [])
        .map((taskId) => S.tasksById?.[taskId] || null)
        .filter(Boolean);
    S.tasks = items;
    return items;
}

function upsertTaskState(task) {
    const taskId = String(task?.task_id || "").trim();
    if (!taskId) return null;
    const previousTask = S.tasksById?.[taskId] || null;
    const nextTask = previousTask ? { ...previousTask, ...task } : { ...task };
    noteTaskItemStateSource(taskId, "patch");
    if (previousTask) {
        nextTask.token_usage = resolveTaskTokenUsage(taskId, previousTask, nextTask, "patch-merge");
    }
    const tasksById = { ...(S.tasksById || {}), [taskId]: nextTask };
    let orderedTaskIds = Array.isArray(S.orderedTaskIds) ? [...S.orderedTaskIds] : [];
    let added = false;
    let orderChanged = false;
    if (!previousTask) {
        added = true;
        let insertAt = orderedTaskIds.length;
        for (let index = 0; index < orderedTaskIds.length; index += 1) {
            const existing = tasksById[orderedTaskIds[index]];
            if (compareTaskListOrder(nextTask, existing) < 0) {
                insertAt = index;
                break;
            }
        }
        orderedTaskIds.splice(insertAt, 0, taskId);
        orderChanged = true;
    } else if (String(previousTask.created_at || "") !== String(nextTask.created_at || "")) {
        orderedTaskIds = orderedTaskIds
            .filter((item) => item !== taskId)
            .concat(taskId)
            .sort((leftId, rightId) => compareTaskListOrder(tasksById[leftId], tasksById[rightId]));
        orderChanged = true;
    }
    S.tasksById = tasksById;
    S.orderedTaskIds = orderedTaskIds;
    syncTaskArrayFromState();
    return { taskId, previousTask, nextTask, added, orderChanged };
}

function removeTaskState(taskId) {
    const key = String(taskId || "").trim();
    if (!key || !S.tasksById?.[key]) return false;
    const tasksById = { ...(S.tasksById || {}) };
    delete tasksById[key];
    S.tasksById = tasksById;
    S.orderedTaskIds = (Array.isArray(S.orderedTaskIds) ? S.orderedTaskIds : []).filter((item) => item !== key);
    syncTaskArrayFromState();
    return true;
}

function taskListViewVisible() {
    return !!U.viewTasks?.classList.contains("active") && document.visibilityState !== "hidden";
}

function noteTaskHallHiddenDefer() {
    S.taskListDirtyWhileHidden = true;
    const next = { ...(S.taskHallStats || {}) };
    next.task_hall_hidden_defer_count = Number(next.task_hall_hidden_defer_count || 0) + 1;
    S.taskHallStats = next;
}

function trackTaskCardPatchQueueAge(taskId) {
    const queuedAt = Number(S.taskCardPatchQueuedAt?.[taskId] || 0);
    if (!Number.isFinite(queuedAt) || queuedAt <= 0) return;
    const ageMs = Math.max(0, Date.now() - queuedAt);
    const next = { ...(S.taskHallStats || {}) };
    next.task_hall_max_patch_queue_age_ms = Math.max(Number(next.task_hall_max_patch_queue_age_ms || 0), ageMs);
    S.taskHallStats = next;
}

function taskMetricSnapshotValue(task) {
    const tokenUsage = taskTokenDisplayUsage(task);
    return tokenUsage.tracked ? {
        input_tokens: Number(tokenUsage.input_tokens || 0),
        output_tokens: Number(tokenUsage.output_tokens || 0),
        cache_hit_tokens: Number(tokenUsage.cache_hit_tokens || 0),
    } : null;
}

// 观测器：记 `resolveTaskTokenUsage` 每一次压住的回退（旧数据没被显示出去，但差值留档，
// lane 分 list-apply / patch-merge，source 分 list / patch）。
// 读法：S.taskHallStats.task_hall_metric_rollback_count 与 S.taskMetricRollbacks。
function noteTaskMetricRollback(taskId, metricKey, previousValue, nextValue, lane) {
    if (!Number.isFinite(previousValue) || !Number.isFinite(nextValue)) return false;
    if (nextValue >= previousValue) return false;
    const key = String(taskId || "").trim();
    const nextStats = { ...(S.taskHallStats || {}) };
    nextStats.task_hall_metric_rollback_count = Number(nextStats.task_hall_metric_rollback_count || 0) + 1;
    S.taskHallStats = nextStats;
    const entry = {
        at: new Date().toISOString(),
        task_id: key,
        metric: String(metricKey || ""),
        from: previousValue,
        to: nextValue,
        lane: String(lane || ""),
        source: String(S.taskItemStateSource?.[key] || ""),
    };
    const log = Array.isArray(S.taskMetricRollbacks) ? S.taskMetricRollbacks : [];
    S.taskMetricRollbacks = [entry, ...log].slice(0, 12);
    return true;
}

// 谁最后写了这条任务的卡片数据：列表整表替换记 list，补丁合并记 patch。
// 回退读数要归因到车道，光看值的变化时刻分不出这两条。
function noteTaskItemStateSource(taskId, source) {
    const key = String(taskId || "").trim();
    if (!key) return;
    S.taskItemStateSource = { ...(S.taskItemStateSource || {}), [key]: String(source || "") };
}

// 卡片上的 token 数只有一条服务端来源（任务行的 token_usage），而服务端那一格是加性累加，
// 所以"这次带来的数比手里的小"必然意味着旧数据：整表替换的列表响应赶在补丁之后落地，或某条
// 整包回写把别的进程刚并进去的用量打回去。这里保留手里那份较大的数，并把这次压制记进观测器——
// 被压掉的差值就是那条车道存在的证据，读法 `S.taskMetricRollbacks`（含 lane 与 source）。
// 缺 tracked 的行（全 0）天然按旧值处理，显示不会退化成 "--"。
function resolveTaskTokenUsage(taskId, heldTask, nextTask, lane) {
    const heldUsage = heldTask && heldTask.token_usage ? heldTask.token_usage : null;
    const nextUsage = nextTask && nextTask.token_usage ? nextTask.token_usage : null;
    if (!heldUsage || !nextUsage) return nextUsage || heldUsage;
    if (tokenKnownTotal(nextUsage) >= tokenKnownTotal(heldUsage)) return nextUsage;
    const heldView = taskTokenDisplayUsage({ token_usage: heldUsage });
    const nextView = taskTokenDisplayUsage({ token_usage: nextUsage });
    ["input_tokens", "output_tokens", "cache_hit_tokens"].forEach((metricKey) => {
        noteTaskMetricRollback(taskId, metricKey, heldView[metricKey], nextView[metricKey], lane);
    });
    return heldUsage;
}

// 卡片上的 token 数最多到 10 位（实盘 task:1d9cddf9858e 缓存 1,703,800,704），而一次
// 模型调用只动最低几位（同任务实测 delta：缓存 8,192–143,872 / 输入 1,595–72,670）。
// 整串呼吸等于让没变的位数也跳，所以按位对齐新旧文本，只给换了的那几位挂动画。
function taskMetricValueMarkup(formatted, previousFormatted) {
    const text = String(formatted);
    const previous = previousFormatted ? String(previousFormatted) : "";
    if (!previous || previous === text) return esc(text);
    return text.split("").map((char, index) => (
        previous[index] === char ? esc(char) : `<span class="pc-metric-digit is-increasing">${esc(char)}</span>`
    )).join("");
}

function taskCardPatchEligible(previousTask, nextTask) {
    if (!previousTask || !nextTask) return false;
    return String(previousTask.status || "") === String(nextTask.status || "")
        && !!previousTask.is_paused === !!nextTask.is_paused
        && !!previousTask.is_unread === !!nextTask.is_unread
        && taskFailureClass(previousTask) === taskFailureClass(nextTask)
        && taskFinalAcceptanceStatus(previousTask) === taskFinalAcceptanceStatus(nextTask)
        && taskRetryCount(previousTask) === taskRetryCount(nextTask)
        && taskRecoveryNotice(previousTask) === taskRecoveryNotice(nextTask)
        && String(previousTask.title || "") === String(nextTask.title || "")
        && String(previousTask.brief || "") === String(nextTask.brief || "")
        && Number(previousTask.max_depth || 0) === Number(nextTask.max_depth || 0);
}

function flushQueuedTaskCardPatches() {
    if (S.taskCardPatchFlushId) {
        window.clearTimeout(S.taskCardPatchFlushId);
        S.taskCardPatchFlushId = null;
    }
    if (!taskListViewVisible()) {
        noteTaskHallHiddenDefer();
        return;
    }
    const pendingIds = [...(S.pendingTaskCardPatchIds || new Set())];
    if (!pendingIds.length) return;
    const nextPending = new Set();
    pendingIds.forEach((taskId) => {
        if (!patchTaskCardElement(taskId)) nextPending.add(taskId);
    });
    S.pendingTaskCardPatchIds = nextPending;
    if (nextPending.size) {
        renderTasks();
        return;
    }
    const meta = paginateResources(orderedTasks(S.tasks), S.taskPage, S.taskPageSize);
    S.taskGridSignature = taskGridRenderSignature(meta);
}

function queueTaskCardPatch(taskId) {
    const key = String(taskId || "").trim();
    if (!key) return;
    const nextPending = new Set(S.pendingTaskCardPatchIds || []);
    nextPending.add(key);
    S.pendingTaskCardPatchIds = nextPending;
    S.taskCardPatchQueuedAt = {
        ...(S.taskCardPatchQueuedAt || {}),
        [key]: S.taskCardPatchQueuedAt?.[key] || Date.now(),
    };
    if (!taskListViewVisible()) {
        noteTaskHallHiddenDefer();
        return;
    }
    if (!S.taskCardPatchFlushId) {
        S.taskCardPatchFlushId = window.setTimeout(() => {
            flushQueuedTaskCardPatches();
        }, 200);
    }
}

function ensureTaskListVisibleReconcile() {
    if (!S.taskListDirtyWhileHidden || !taskListViewVisible()) return;
    S.taskListDirtyWhileHidden = false;
    S.taskGridSignature = "";
    renderTasks();
}

function renderTasksIfVisible() {
    if (taskListViewVisible()) renderTasks();
    else noteTaskHallHiddenDefer();
}

function normalizeTaskWorkerState(value) {
    const normalized = String(value || "").trim().toLowerCase();
    if (["starting", "online", "stale", "stopped", "offline"].includes(normalized)) return normalized;
    return normalized === "dead" ? "stopped" : "offline";
}

function taskWorkerControlsAvailable() {
    return normalizeTaskWorkerState(S.tasksWorkerState) === "online";
}

function taskWorkerNoticeText(state = S.tasksWorkerState) {
    const normalized = normalizeTaskWorkerState(state);
    if (normalized === "starting") return "Task worker is starting. You can browse records while controls warm up.";
    if (normalized === "stale") return "Task worker status is temporarily stale. You can browse records while controls wait for reconnection.";
    if (normalized === "stopped" || normalized === "offline") return "Task worker is offline. You can still browse records, but create/resume controls are unavailable.";
    return "";
}

function taskSessionEmptyText() {
    const message = taskWorkerNoticeText();
    if (message) return message;
    return "No tasks yet.";
}

function taskWorkerStatusMetrics() {
    const topLevel = S.tasksWorkerStatusPayload && typeof S.tasksWorkerStatusPayload === "object"
        ? S.tasksWorkerStatusPayload
        : {};
    const workerPayload = S.tasksWorker?.payload && typeof S.tasksWorker.payload === "object"
        ? S.tasksWorker.payload
        : {};
    return { ...workerPayload, ...topLevel };
}

// 读数身份超过 15s 没变才算停更：实盘正常节拍是心跳 1–2s 一拍，WS 断了退到 5s 轮询再叠加
// 秒级截断也才 7s 上下。旧的「服务端年龄 ≤3s」量的其实是这条流水线本身的多长，读数一直在
// 更新也会翻成过期，所以压力状态改与新鲜度共用同一根按身份归零的秒表。
const TASK_PRESSURE_STALE_AFTER_MS = 15_000;

function taskWorkerPressureSnapshotFresh(metrics = taskWorkerStatusMetrics()) {
    const ageMs = taskWorkerSampleAgeMsLive(metrics);
    return ageMs != null && ageMs < TASK_PRESSURE_STALE_AFTER_MS;
}

// 新鲜度量的是「距离上一次收到新读数过了多久」：读数身份（pressure_sample_at）一变就归零重计，
// 身份不变只在本地累加；读数断了数字照旧往上走，也不切成「刚刚更新」这类不带时长的说法——
// 操作员要判断的是断了多久。归零的判据必须是读数的身份，不能拿读数的年龄当阈值：心跳 1–2s 一拍、
// 时间戳又只有整秒精度，实盘 REST 回来的年龄恒定在 2.4s 上下，「年龄 <1s」永不会命中。
function taskWorkerSampleAgeMsLive(metrics = taskWorkerStatusMetrics()) {
    const sampleAt = String(metrics?.pressure_sample_at || metrics?.tool_pressure_sample_at || "").trim();
    const reportedAgeMs = Number(metrics?.pressure_sample_age_ms);
    if (!sampleAt && !Number.isFinite(reportedAgeMs)) {
        S.taskPerfFreshness = null;
        return null;
    }
    const nowMs = Date.now();
    const anchor = S.taskPerfFreshness;
    // 没有身份串时只认「年龄变小」这一件事：同一份读数只会越走越旧，变年轻只可能是来了新读数。
    const isNewReading = !anchor
        || (sampleAt ? sampleAt !== anchor.sampleAt : reportedAgeMs < anchor.reportedAgeMs);
    if (isNewReading) {
        S.taskPerfFreshness = { sampleAt, reportedAgeMs, atMs: nowMs };
        return 0;
    }
    return nowMs - anchor.atMs;
}

// 时长只算一次，两枚胶囊各说各的：新鲜度报「多久之前的读数」，压力状态报「停更多久」。
function taskWorkerAgeParts(ageMs) {
    const seconds = Math.round(ageMs / 1000);
    if (seconds < 100) return { value: seconds, unit: "秒" };
    const minutes = Math.round(seconds / 60);
    if (minutes < 100) return { value: minutes, unit: "分钟" };
    return { value: Math.round(minutes / 60), unit: "小时" };
}

function formatTaskWorkerSampleFreshness(metrics = taskWorkerStatusMetrics()) {
    const ageMs = taskWorkerSampleAgeMsLive(metrics);
    if (ageMs == null) return "未采样";
    // 每档预留两位数字宽度（99 秒内、59 分内、24 时内都不改胶囊宽度），个位数前面
    // 留空不补零；等宽数字与前导空格由 CSS 的 tabular-nums + white-space: pre 保住。
    const parts = taskWorkerAgeParts(ageMs);
    return `${String(parts.value).padStart(2, " ")}${parts.unit}前`;
}

function formatTaskWorkerPercent(value) {
    const numeric = Number(value);
    return Number.isFinite(numeric) ? `${Math.round(numeric)}%` : "--";
}

function queueMetricCount(value) {
    const numeric = Number(value);
    return Number.isFinite(numeric) ? String(Math.max(0, Math.trunc(numeric))) : "--";
}

// 资源读数按各自的占用档位着色：整段磁盘紧急态只决定描边，不再把三个数字一起染绿。
function taskWorkerMetricSpan(text, value) {
    const numeric = Number(value);
    let level = "";
    if (Number.isFinite(numeric)) {
        if (numeric >= 90) level = " is-critical";
        else if (numeric >= 75) level = " is-warning";
    }
    return `<span class="task-performance-metric${level}">${esc(text)}</span>`;
}

function taskWorkerPressureStateMeta(metrics = taskWorkerStatusMetrics()) {
    const workerState = normalizeTaskWorkerState(S.tasksWorkerState);
    if (workerState === "offline" || workerState === "stopped") return { key: "offline", label: "离线" };
    if (workerState === "starting") return { key: "starting", label: "启动中" };
    if (workerState === "stale") return { key: "stale", label: "连接过期" };
    if (metrics?.machine_pressure_available === false) return { key: "unfresh", label: "无资源读数" };
    if (!taskWorkerPressureSnapshotFresh(metrics)) {
        const ageMs = taskWorkerSampleAgeMsLive(metrics);
        if (ageMs == null) return { key: "unfresh", label: "未采样" };
        const parts = taskWorkerAgeParts(ageMs);
        return { key: "unfresh", label: `停更${parts.value}${parts.unit}` };
    }
    const state = String(metrics?.budget_state || metrics?.tool_pressure_state || metrics?.worker_execution_state || "normal").trim().toLowerCase();
    if (state === "critical") return { key: "critical", label: "强收紧" };
    if (state === "throttled") return { key: "throttled", label: "收紧中" };
    if (state === "easing") return { key: "easing", label: "放宽中" };
    return { key: "normal", label: "正常" };
}

function formatTaskDiskFreeBytes(metrics) {
    const free = Number(metrics?.machine_disk_free_bytes);
    if (!Number.isFinite(free) || free < 0) return "--";
    if (free >= 1024 ** 3) return `${(free / 1024 ** 3).toFixed(1)}G`;
    if (free >= 1024 ** 2) return `${(free / 1024 ** 2).toFixed(0)}M`;
    return `${(free / 1024).toFixed(0)}K`;
}

function renderTaskPerformanceBar() {
    if (!U.taskPerformanceBar) return;
    // 多选态里这一行要让位给动作组，否则读数会把按钮挤到换行、看着像叠在一起。
    // 内容不清掉：退出多选时先亮出上一次读数，再由轮询刷新。
    if (S.multiSelectMode) {
        U.taskPerformanceBar.hidden = true;
        return;
    }
    const metrics = taskWorkerStatusMetrics();
    const pressureState = taskWorkerPressureStateMeta(metrics);
    const cpuText = formatTaskWorkerPercent(metrics?.machine_pressure_cpu_percent);
    const memoryText = formatTaskWorkerPercent(metrics?.machine_pressure_memory_percent);
    const diskText = metrics?.machine_pressure_disk_busy_available === false
        ? "--"
        : formatTaskWorkerPercent(metrics?.machine_pressure_disk_busy_percent);
    // 磁盘治理：剩余空间并进磁盘那一段（磁盘段显示 `0%(剩余10.1G)`），不再
    // 单列「磁盘剩余」一项；紧急态着色随之落在 CPU/内存/磁盘 这一项上。
    // 清理线已移除——磁盘紧张不再自动删除任务，由任务大小排序引导手动清理。
    const diskEmergency = !!metrics?.disk_emergency_active;
    const diskFreeText = formatTaskDiskFreeBytes(metrics);
    const diskBusyText = diskText === "--" ? "--" : `${diskText}(剩余${diskFreeText})`;
    const diskStateKey = diskEmergency ? "critical" : "normal";
    const diskStateSuffix = diskEmergency ? " · 紧急" : "";
    const toolRunningText = queueMetricCount(metrics?.tool_queue_running_count);
    const toolWaitingText = queueMetricCount(metrics?.tool_queue_waiting_count);
    // 「节点队列」这一段量的是回合闸：运行=占住的闸位数，等待=闸口排队数，当前容量=
    // 闸现在开多大（不是上限）。node_queue_waiting_count 量的是过闸之后等模型 permit 的
    // 队列，闸收紧时节点还卡在闸口、它恒 0，当不了「等请求位」的读数。三个数各带标签，
    // 不叠成 `N/M`——那样读的人分不清哪个是占用、哪个是天花板。
    const gateRunningText = queueMetricCount(metrics?.entry_gate_running_total);
    const gateCapacityText = queueMetricCount(metrics?.entry_gate_limits_total);
    const gateWaitingText = queueMetricCount(metrics?.entry_gate_queued_total);
    U.taskPerformanceBar.hidden = false;
    U.taskPerformanceBar.innerHTML = `
        <div class="task-performance-item task-performance-item--state" data-state="${esc(pressureState.key)}">
            <span class="task-performance-label">压力状态</span>
            <strong class="task-performance-value">${esc(pressureState.label)}</strong>
        </div>
        <div class="task-performance-item task-performance-item--state" data-state="${esc(diskStateKey)}">
            <span class="task-performance-label">CPU/内存/磁盘</span>
            <strong class="task-performance-value">${taskWorkerMetricSpan(cpuText, metrics?.machine_pressure_cpu_percent)} / ${taskWorkerMetricSpan(memoryText, metrics?.machine_pressure_memory_percent)} / ${taskWorkerMetricSpan(diskBusyText, metrics?.machine_pressure_disk_busy_available === false ? Number.NaN : metrics?.machine_pressure_disk_busy_percent)}${esc(diskStateSuffix)}</strong>
        </div>
        <div class="task-performance-item">
            <span class="task-performance-label">工具队列</span>
            <strong class="task-performance-value task-performance-queue-value"><span class="task-performance-count task-performance-count--running">${esc(toolRunningText)}</span>运行 / <span class="task-performance-count task-performance-count--waiting">${esc(toolWaitingText)}</span>等待</strong>
        </div>
        <div class="task-performance-item">
            <span class="task-performance-label">节点队列</span>
            <strong class="task-performance-value task-performance-queue-value"><span class="task-performance-count task-performance-count--running">${esc(gateRunningText)}</span>运行 / <span class="task-performance-count task-performance-count--waiting">${esc(gateWaitingText)}</span>等待 / <span class="task-performance-count">${esc(gateCapacityText)}</span>当前容量</strong>
        </div>
        <div class="task-performance-item">
            <span class="task-performance-label">监控新鲜度</span>
            <strong class="task-performance-value task-performance-age-value">${esc(formatTaskWorkerSampleFreshness(metrics))}</strong>
        </div>
    `;
}

function renderTaskSessionScope() {
    return;
}

async function setTaskSessionScope(scope) {
    return;
}

function taskSessionMeta(task) {
    const sessionId = String(task?.session_id || "").trim();
    if (!sessionId) return "";
    const session = (S.ceoSessions || []).find((item) => String(item?.session_id || "").trim() === sessionId);
    const label = String(session?.title || sessionId).trim();
    return `Session ${label}`;
}

function taskMetaText(task) {
    const parts = [];
    if (task.is_unread) parts.push("Unread");
    const sessionMeta = taskSessionMeta(task);
    if (sessionMeta) parts.push(sessionMeta);
    if (task.created_at) parts.push(`Created ${formatSessionTime(task.created_at)}`);
    return parts.join(" 路 ") || "No timestamp";
}

function taskCreatedAtText(task) {
    return task?.created_at ? formatSessionTime(task.created_at) : "\u6682\u65e0";
}

function parseTaskTimeMs(value) {
    const raw = String(value || "").trim();
    if (!raw) return null;
    const parsed = new Date(raw).getTime();
    return Number.isFinite(parsed) ? parsed : null;
}

// \u7ec8\u6001\uff1a\u4efb\u52a1\u5df2\u8dd1\u5b8c\uff08\u6210\u529f/\u5931\u8d25/\u9a8c\u6536\u672a\u901a\u8fc7\uff09\u3002\u6b64\u65f6\u8017\u65f6\u5b9a\u683c\u5728 finished_at\u3002
function taskIsTerminal(task) {
    const key = taskStatusKey(task);
    return key === "success" || key === "failed" || key === "unpassed";
}

function taskFinishedAtText(task) {
    const raw = String(task?.finished_at || task?.updated_at || "").trim();
    return raw ? formatSessionTime(raw) : "\u6682\u65e0";
}

function formatTaskElapsedMs(ms) {
    const total = Number.isFinite(ms) && ms > 0 ? ms : 0;
    const totalMinutes = Math.floor(total / 60000);
    const hours = Math.floor(totalMinutes / 60);
    const minutes = totalMinutes % 60;
    return hours > 0 ? `${hours}h${minutes}m` : `${minutes}m`;
}

// \u603b\u8017\u65f6\uff1a\u4ece created_at \u5230\u300c\u53c2\u8003\u7ed3\u675f\u65f6\u523b\u300d\u7684\u5899\u949f\u65f6\u957f\uff0c\u7cbe\u786e\u5230\u5206\u949f\u3002
// - \u7ec8\u6001\uff1a\u5b9a\u683c\u5728 finished_at\uff08\u56de\u9000 updated_at\uff09\uff0c\u6052\u5b9a\u4e0d\u53d8\u3002
// - \u6682\u505c\uff08blocked\uff09\uff1a\u5b9a\u683c\u5728 updated_at\u2014\u2014\u6682\u505c\u540e actor \u505c\u6b62\u3001\u65e0\u8282\u70b9\u6d3b\u52a8\uff0c
//   updated_at \u4e0d\u518d\u524d\u8fdb\uff0c\u56e0\u6b64\u6570\u5b57\u5728\u6682\u505c\u671f\u95f4\u4e0d\u589e\u957f\u3002
// - \u8fd0\u884c\u4e2d\uff1a\u7528\u5f53\u524d\u65f6\u523b\uff0c\u968f\u5361\u7247\u91cd\u5efa\uff08\u4ee4\u724c/\u8f93\u51fa\u53d8\u5316\u89e6\u53d1\uff09\u9010\u5206\u949f\u5237\u65b0\u3002
function taskElapsedText(task) {
    const startMs = parseTaskTimeMs(task?.created_at);
    if (startMs === null) return "--";
    let endMs;
    if (taskIsTerminal(task)) {
        endMs = parseTaskTimeMs(task?.finished_at) ?? parseTaskTimeMs(task?.updated_at);
    } else if (taskStatusKey(task) === "blocked") {
        endMs = parseTaskTimeMs(task?.updated_at);
    } else {
        endMs = Date.now();
    }
    if (endMs === null) return "--";
    return formatTaskElapsedMs(endMs - startMs);
}

async function copyTaskId(taskId) {
    const value = String(taskId || "").trim();
    if (!value) return;
    try {
        const copied = await copyTextToClipboard(value);
        if (!copied) throw new Error("Clipboard unavailable");
        showToast({ title: "\u590d\u5236\u6210\u529f", text: value, kind: "success", durationMs: 1800 });
    } catch (error) {
        showToast({
            title: "\u590d\u5236\u5931\u8d25",
            text: String(error?.message || "Clipboard unavailable"),
            kind: "error",
            durationMs: 2200,
        });
    }
}


// 任务卡片同步暂停提示：点击暂停后卡片内显示「暂停中 (x/x)」+ spinner + 恢复运行，
// 轮询 /api/tasks/{id}/pause-state 直到 worker 真实停完（draining=false）才显示
// 「暂停成功」。draining 的判定是 pause_task 命令是否已被 worker 消费完成——命令
// 完成意味着 actor 已经停止，不只是落盘标志先行。
const TASK_PAUSE_HINT_POLL_MS = 400;
const TASK_PAUSE_HINT_POLL_TIMEOUT_MS = 30000;
const TASK_PAUSE_HINT_DONE_HIDE_MS = 2500;
// 任务详情 WS 断线重连：与任务列表 WS 同策略（1s 后重建）。详情 WS 只挂
// onmessage 时，任何一次断线（服务重启/网络抖动/休眠）都会让树视图永久冻结
// 在最后一帧快照上（2026-09-12 分发期间"树卡死"事故的直接成因）。
const TASK_DETAIL_WS_RECONNECT_MS = 1000;

function taskPauseHintState(taskId) {
    const key = String(taskId || "").trim();
    if (!key) return null;
    S.taskPauseHints = S.taskPauseHints || {};
    return S.taskPauseHints[key] || null;
}

function taskPauseHintDrainText(hint) {
    if (!hint || hint.phase !== "draining") return "";
    if (!Number.isFinite(hint.total)) return "暂停中...";
    return `暂停中 (${Number(hint.stopped || 0)}/${Number(hint.total || 0)})`;
}

function taskPauseHintMarkup(taskId) {
    const hint = taskPauseHintState(taskId);
    if (!hint) return "";
    const key = String(taskId || "");
    if (hint.phase === "paused") {
        return `
            <div class="pc-pause-hint is-done" data-task-pause-hint="${esc(key)}" role="status" aria-live="polite">
                <span class="pc-pause-hint-icon" aria-hidden="true"><i data-lucide="circle-check"></i></span>
                <span class="pc-pause-hint-text">暂停成功</span>
            </div>`;
    }
    return `
        <div class="pc-pause-hint" data-task-pause-hint="${esc(key)}" role="status" aria-live="polite">
            <span class="pc-pause-hint-spinner" aria-hidden="true"><i data-lucide="loader-circle"></i></span>
            <span class="pc-pause-hint-text">${esc(taskPauseHintDrainText(hint))}</span>
            <button class="pc-pause-hint-resume" type="button" data-task-pause-resume="${esc(key)}">恢复运行</button>
        </div>`;
}

function stopTaskPauseHintPolling(taskId) {
    const hint = taskPauseHintState(taskId);
    if (!hint) return;
    if (hint.pollTimer) {
        window.clearTimeout(hint.pollTimer);
        hint.pollTimer = null;
    }
}

function clearTaskPauseHint(taskId) {
    const key = String(taskId || "").trim();
    if (!key) return;
    const hint = taskPauseHintState(key);
    if (hint) {
        stopTaskPauseHintPolling(key);
        if (hint.hideTimer) {
            window.clearTimeout(hint.hideTimer);
            hint.hideTimer = null;
        }
    }
    S.taskPauseHints = S.taskPauseHints || {};
    delete S.taskPauseHints[key];
}

function updateTaskPauseHintElement(taskId) {
    // 轮询期间只做定点文本更新，避免每 400ms 全量重建卡片网格。
    const key = String(taskId || "").trim();
    const hint = taskPauseHintState(key);
    if (!hint || hint.phase !== "draining") return;
    const card = U.taskGrid?.querySelector?.(`.project-card[data-task-id="${CSS.escape(key)}"]`);
    const textEl = card?.querySelector?.("[data-task-pause-hint] .pc-pause-hint-text");
    if (textEl) textEl.textContent = taskPauseHintDrainText(hint);
}

function finishTaskPauseHint(taskId) {
    const key = String(taskId || "").trim();
    const hint = taskPauseHintState(key);
    if (!hint) return;
    stopTaskPauseHintPolling(key);
    hint.phase = "paused";
    if (Number.isFinite(hint.total)) hint.stopped = hint.total;
    renderTasksIfVisible();
    hint.hideTimer = window.setTimeout(() => {
        clearTaskPauseHint(key);
        renderTasksIfVisible();
    }, TASK_PAUSE_HINT_DONE_HIDE_MS);
}

async function pollTaskPauseState(taskId) {
    const key = String(taskId || "").trim();
    const hint = taskPauseHintState(key);
    if (!hint || hint.phase !== "draining") return;
    let payload = null;
    try {
        payload = await ApiClient.getTaskPauseState(key);
    } catch (error) {
        payload = null;
    }
    const current = taskPauseHintState(key);
    if (!current || current.phase !== "draining") return;
    if (payload) {
        current.total = Number(payload.internal_total || 0);
        current.stopped = Number(payload.internal_stopped || 0);
        if (!payload.draining && !payload.is_paused && !payload.pause_requested) {
            // 任务已被别处恢复（或暂停未生效）：撤掉提示。
            clearTaskPauseHint(key);
            renderTasksIfVisible();
            return;
        }
        if (!payload.draining) {
            finishTaskPauseHint(key);
            return;
        }
    }
    if (Date.now() - Number(current.startedAt || 0) > TASK_PAUSE_HINT_POLL_TIMEOUT_MS) {
        // 落盘暂停标志早已生效；worker 卡住时不再无限等待。
        finishTaskPauseHint(key);
        return;
    }
    updateTaskPauseHintElement(key);
    current.pollTimer = window.setTimeout(() => { void pollTaskPauseState(key); }, TASK_PAUSE_HINT_POLL_MS);
}

function beginTaskPauseHint(taskId) {
    const key = String(taskId || "").trim();
    if (!key) return;
    clearTaskPauseHint(key);
    S.taskPauseHints = S.taskPauseHints || {};
    S.taskPauseHints[key] = {
        phase: "draining",
        total: null,
        stopped: 0,
        startedAt: Date.now(),
        pollTimer: null,
        hideTimer: null,
    };
    renderTasksIfVisible();
    void pollTaskPauseState(key);
}

function taskGridRenderSignature(meta) {
    const visibleItems = Array.isArray(meta?.items) ? meta.items : [];
    return JSON.stringify({
        total: Number(meta?.total || 0),
        currentPage: Number(meta?.currentPage || 1),
        pageSize: Number(S.taskPageSize || 0),
        workerOnline: taskWorkerControlsAvailable(),
        workerState: normalizeTaskWorkerState(S.tasksWorkerState),
        // workerLastSeenAt 不参与签名：心跳时间戳每秒变化但不改变任何渲染像素，
        // 放进来会让每次心跳都触发整网格重建，滚动位置被清零，大厅滚动形同卡死。
        // 磁盘紧急横幅由快照数据驱动，属于可见状态，需要能触发重建。
        diskEmergencyActive: !!taskWorkerStatusMetrics()?.disk_emergency_active,
        taskBusy: !!S.taskBusy,
        multiSelectMode: !!S.multiSelectMode,
        selectedTaskCount: S.selectedTaskIds.size,
        sortMode: String(S.taskSortMode || "time"),
        emptyText: taskSessionEmptyText(),
        selectedTaskIds: [...S.selectedTaskIds].map((id) => String(id || "")).sort(),
        items: visibleItems.map((task) => {
            const taskId = String(task?.task_id || "");
            const tokenUsage = taskTokenDisplayUsage(task);
            return {
                taskId,
                selected: S.selectedTaskIds.has(taskId),
                title: String(task?.title || ""),
                statusKey: taskStatusKey(task),
                statusLabel: taskStatusLabel(task),
                createdAt: taskCreatedAtText(task),
                diskUsage: taskDiskUsageSortValue(task),
                nodeCount: Number(task?.node_count || 0),
                pauseHintPhase: String(taskPauseHintState(taskId)?.phase || ""),
                tokenUsage: tokenUsage.tracked
                    ? [tokenUsage.input_tokens, tokenUsage.output_tokens, tokenUsage.cache_hit_tokens]
                    : [null, null, null],
                actions: taskCardActions(task).map((action) => `${action.action}:${action.tone}`).join("|"),
            };
        }),
    });
}

async function loadTasks() {
    if (S.taskListReconcileBusy) return;
    S.taskListReconcileBusy = true;
    if (!(S.tasks || []).length) {
        S.taskGridSignature = "";
        U.taskGrid.innerHTML = '<div class="empty-state" style="grid-column: 1/-1;">Loading tasks...</div>';
    }
    try {
        const payload = await ApiClient.getTasks(1, taskSessionQueryValue());
        applyTaskListResponse(payload || {});
        if (S.view === "tasks") initTasksWs();
    } catch (e) {
        if (isAbortLike(e)) return;
        if (!(S.tasks || []).length) {
            U.taskGrid.innerHTML = `<div class="empty-state error" style="grid-column: 1/-1;">任务加载失败：${esc(ApiClient.friendlyErrorMessage(e, e.message || "未知错误"))}</div>`;
        }
        showToast({ title: "加载失败", text: ApiClient.friendlyErrorMessage(e, e.message || "未知错误"), kind: "error" });
    } finally {
        S.taskListReconcileBusy = false;
    }
}

function updateTaskToolbar() {
    syncTaskSelection();
    const selected = getSelectedTasks();
    // 头部只留一颗「多选」；点开才在同一行里出现选区读数、按状态选与三个动作。
    if (U.taskMultiToggle) {
        U.taskMultiToggle.setAttribute("aria-pressed", S.multiSelectMode ? "true" : "false");
        U.taskMultiToggle.classList.toggle("active", S.multiSelectMode);
    }
    if (U.taskSelectionBar) U.taskSelectionBar.hidden = !S.multiSelectMode;
    if (U.taskPerformanceBar) U.taskPerformanceBar.hidden = S.multiSelectMode;
    if (U.taskSelectionCount) U.taskSelectionCount.textContent = `已选 ${selected.length} 个`;
    if (U.taskClearSelection) U.taskClearSelection.disabled = S.taskBusy || !selected.length;
    const filterButtons = [...(U.taskFilterMenu?.querySelectorAll("[data-select-bucket]") || [])];
    filterButtons.forEach((button) => {
        button.disabled = S.taskBusy || !S.tasks.some((task) => statusBucketMatches(task, button.dataset.selectBucket));
    });
    const batchButtons = [...(U.taskSelectionBar?.querySelectorAll("[data-batch-action]") || [])];
    batchButtons.forEach((button) => {
        const action = button.dataset.batchAction;
        const workerReady = !["pause", "resume"].includes(String(action || "")) || taskWorkerControlsAvailable();
        const enabled = workerReady && (action === "pause"
            ? selected.some((task) => canPause(task))
            : action === "resume"
                ? selected.some((task) => canResume(task))
                : selected.some((task) => canDelete(task)));
        button.disabled = S.taskBusy || !enabled;
    });
    setTaskMenuVisibility();
}

function taskActionTone(action) {
    if (action === "pause") return "warn";
    if (action === "clear_temp") return "warn";
    if (action === "delete") return "danger";
    return "success";
}

function taskCardActions(task) {
    const actions = [];
    if (taskWorkerControlsAvailable() && canPause(task)) actions.push("pause");
    if (taskWorkerControlsAvailable() && canResume(task)) actions.push("resume");
    if (canClearTemp(task)) actions.push("clear_temp");
    if (canDelete(task)) actions.push("delete");
    return actions.map((action) => ({ action, label: taskActionText(action, task), tone: taskActionTone(action) }));
}

function primaryTaskAction(task) {
    if (taskWorkerControlsAvailable() && canPause(task)) return { action: "pause", label: "\u6682\u505c", tone: "warn" };
    if (taskWorkerControlsAvailable() && canResume(task)) return { action: "resume", label: "\u5f00\u59cb", tone: "success" };
    return null;
}

function taskActionText(action, task = null) {
    return ({ pause: "暂停", resume: "开始", clear_temp: "清除临时文件", delete: "删除" }[action] || "操作");
}

function taskActionSuccessTitle(action) {
    if (action === "delete") return "删除成功";
    if (action === "clear_temp") return "临时文件已清除";
    return `${taskActionText(action)}成功`;
}

function taskActionFailureTitle(action) {
    if (action === "delete") return "删除失败";
    if (action === "clear_temp") return "清除临时文件失败";
    return `${taskActionText(action)}失败`;
}

function taskActionErrorText(action, error) {
    const message = String(error?.message || error || "").trim();
    if (action === "delete") {
        if (message.includes("task_still_stopping")) return "任务仍在停止中，请稍后再删";
        if (message.includes("task_not_deletable") || message.includes("task_not_paused")) return "仅已暂停或已完成的任务可删除";
        if (message.includes("task_not_found")) return "任务不存在或已被删除";
    }
    if (action === "clear_temp") {
        if (message.includes("task_not_terminal")) return "仅已完成或失败的任务可清除临时文件";
        if (message.includes("task_not_found")) return "任务不存在或已被删除";
    }
    return message || "Unknown error";
}

function taskActionRequiresWorker(action) {
    return ["pause", "resume"].includes(String(action || "").trim().toLowerCase());
}

function taskActionRequestOptions(action, options = {}) {
    const normalized = options && typeof options === "object" && !Array.isArray(options) ? { ...options } : {};
    if (["delete", "clear_temp"].includes(action) && !Number.isFinite(normalized.timeoutMs)) normalized.timeoutMs = 30000;
    return normalized;
}

function taskBatchActionConcurrency(action) {
    if (action === "delete") return 2;
    return 4;
}

async function settleTaskActionBatch(items, worker, { concurrency = 1 } = {}) {
    const normalizedItems = Array.isArray(items) ? items : [];
    const results = new Array(normalizedItems.length);
    let nextIndex = 0;
    const workerCount = Math.max(1, Math.min(normalizedItems.length || 1, Number(concurrency) || 1));
    async function runNext() {
        while (true) {
            const currentIndex = nextIndex;
            nextIndex += 1;
            if (currentIndex >= normalizedItems.length) return;
            try {
                results[currentIndex] = {
                    status: "fulfilled",
                    value: await worker(normalizedItems[currentIndex], currentIndex),
                };
            } catch (error) {
                results[currentIndex] = {
                    status: "rejected",
                    reason: error,
                };
            }
        }
    }
    await Promise.all(Array.from({ length: workerCount }, () => runNext()));
    return results;
}

async function requestTaskAction(taskId, action, options = {}) {
    const requestOptions = taskActionRequestOptions(action, options);
    if (action === "pause") return ApiClient.pauseTask(taskId, requestOptions);
    if (action === "resume") return ApiClient.resumeTask(taskId, requestOptions);
    if (action === "clear_temp") return ApiClient.clearTaskTempFiles(taskId, requestOptions);
    if (action === "delete") return ApiClient.deleteTask(taskId, requestOptions);
    throw new Error(`Unsupported task action: ${action}`);
}

function taskClearTempConfirmText() {
    return "只删 temp/tasks 下该任务的临时目录，任务记录、节点与产出保留；临时目录里的正式交付物也会一并删除，无法恢复。";
}

function taskClearTempResultText(result) {
    const freed = Number(result?.freed_bytes || 0);
    const removed = Number(result?.removed_dirs?.length || 0);
    if (!removed || freed <= 0) return "该任务没有可清除的临时目录";
    return `已清除 ${removed} 个目录，释放 ${formatTaskBytes(freed)}`;
}

async function runTaskAction(taskId, action, { returnFocus = null } = {}) {
    if (!taskId || !action) return;
    if (action === "delete") {
        openConfirm({
            title: "删除任务",
            text: "删除后将移除任务记录、节点信息与全部过程数据，且无法恢复；报告类产出会自动导出到 deliverables 目录保留。",
            confirmLabel: "删除",
            confirmKind: "danger",
            returnFocus,
            onConfirm: () => performTaskAction(taskId, action),
        });
        return;
    }
    if (action === "clear_temp") {
        openConfirm({
            title: "清除临时文件",
            text: taskClearTempConfirmText(),
            confirmLabel: "清除",
            confirmKind: "warn",
            returnFocus,
            onConfirm: () => performTaskAction(taskId, action),
        });
        return;
    }
    await performTaskAction(taskId, action);
}

async function performTaskAction(taskId, action) {
    if (!taskId || !action) return;
    if (taskActionRequiresWorker(action) && !taskWorkerControlsAvailable()) {
        showToast({ title: taskActionFailureTitle(action), text: taskWorkerNoticeText(), kind: "warn" });
        void refreshTaskWorkerStatus({ render: S.view === "tasks" });
        return;
    }
    S.taskBusy = true;
    renderTasksIfVisible();
    try {
        const result = await requestTaskAction(taskId, action);
        if (action === "pause") {
            // 同步暂停：卡片内提示跟踪 worker 真实排水进度，直到 actor 停完
            // 才显示「暂停成功」；不再用页面顶部 toast 提前宣告成功。
            beginTaskPauseHint(taskId);
        } else {
            if (action === "resume") {
                clearTaskPauseHint(taskId);
            }
            const successText = action === "clear_temp" ? taskClearTempResultText(result) : taskId;
            const partialFailures = action === "clear_temp" && Number(result?.failed_dirs?.length || 0) > 0;
            showToast({
                title: partialFailures ? "部分临时目录未删除" : taskActionSuccessTitle(action),
                text: successText,
                kind: partialFailures ? "warn" : "success",
            });
        }
        await loadTasks();
        if (action === "delete") {
            handleDeletedTasks([taskId]);
        } else if (S.currentTaskId === taskId) {
            await loadTaskDetail(taskId, { preserveView: true, reopenSocket: false });
            await loadTaskArtifacts();
        }
    } catch (e) {
        if (Number(e?.status || 0) === 503) void refreshTaskWorkerStatus({ render: S.view === "tasks" });
        showToast({ title: taskActionFailureTitle(action), text: taskActionErrorText(action, e), kind: "error" });
    } finally {
        S.taskBusy = false;
        renderTasksIfVisible();
    }
}

async function runTaskBatchAction(action, { returnFocus = null } = {}) {
    closeTaskMenus();
    const selected = getSelectedTasks();
    const eligible = selected.filter((task) => {
        if (action === "pause") return canPause(task);
        if (action === "resume") return canResume(task);
        if (action === "delete") return canDelete(task);
        return false;
    });
    if (!eligible.length) {
        showToast({ title: "No eligible tasks", text: "Current selection cannot perform this action.", kind: "warn" });
        return;
    }
    if (action === "delete") {
        openConfirm({
            title: "删除任务",
            text: "删除后将移除任务记录、节点信息与全部过程数据，且无法恢复；报告类产出会自动导出到 deliverables 目录保留。",
            confirmLabel: "删除",
            confirmKind: "danger",
            returnFocus,
            onConfirm: () => performTaskBatchAction(action, eligible),
        });
        return;
    }
    await performTaskBatchAction(action, eligible);
}

function taskDetailViewVisible() {
    // U 或详情视图元素缺失（无真实 DOM 的测试环境）按可见处理。
    if (typeof U === "undefined" || !U || !U.viewTaskDetails) return true;
    return !!U.viewTaskDetails.classList.contains("active");
}

// 主动关闭详情 WS 时必须先摘掉 onclose，否则关闭动作本身会触发重连。
function closeTaskDetailWs() {
    if (S.taskWsReconnectTimer) {
        window.clearTimeout(S.taskWsReconnectTimer);
        S.taskWsReconnectTimer = null;
    }
    const socket = S.taskWs;
    S.taskWs = null;
    if (!socket) return;
    socket.onclose = null;
    socket.onopen = null;
    socket.onmessage = null;
    try {
        socket.close();
    } catch (error) {
        void error;
    }
}

// 打开任务详情 WS；断线后只要仍在查看该任务就按 1s 间隔重连（对齐任务列表
// WS 的 onclose 重连策略）。重连成功后用 preserveView + reopenSocket:false
// 重拉任务详情与整树快照对齐状态：断线窗口内错过的 live 事件无法重放补齐。
function openTaskDetailWs(taskId, { isReconnect = false } = {}) {
    const key = String(taskId || "").trim();
    if (!key) return;
    closeTaskDetailWs();
    const socket = new WebSocket(ApiClient.getTaskWsUrl(key));
    S.taskWs = socket;
    socket.onmessage = (ev) => handleTaskEvent(JSON.parse(ev.data));
    socket.onopen = () => {
        if (S.taskWs !== socket || !isReconnect) return;
        if (String(S.currentTaskId || "").trim() !== key) return;
        void reconcileTaskDetailAfterWsReconnect(key);
    };
    socket.onclose = () => {
        if (S.taskWs !== socket) return;
        S.taskWs = null;
        if (String(S.currentTaskId || "").trim() !== key) return;
        if (!taskDetailViewVisible()) return;
        if (S.taskWsReconnectTimer) return;
        S.taskWsReconnectTimer = window.setTimeout(() => {
            S.taskWsReconnectTimer = null;
            if (S.taskWs) return;
            if (String(S.currentTaskId || "").trim() !== key) return;
            if (!taskDetailViewVisible()) return;
            openTaskDetailWs(key, { isReconnect: true });
        }, TASK_DETAIL_WS_RECONNECT_MS);
    };
}

async function reconcileTaskDetailAfterWsReconnect(taskId) {
    const preservedRoundSelections = { ...(S.treeSelectedRoundByNodeId || {}) };
    await loadTaskDetail(taskId, { preserveView: true, reopenSocket: false });
    if (String(S.currentTaskId || "").trim() !== String(taskId || "").trim() || !taskDetailViewVisible()) return;
    S.treeSelectedRoundByNodeId = normalizeTreeRoundSelections(preservedRoundSelections);
    if (String(S.treeRootNodeId || "").trim()) renderTree();
}

// 离开详情视图后，驻留的大状态（整树快照、节点 full 详情、上下文快照）与
// 对应 DOM 没有读者：就地释放，降低标签页驻留内存。机器内存吃紧时，驻留
// 过大的标签页会被系统裁剪换出，再次交互触发缺页换回，表现为整个浏览器
// 进程冻结数秒、输入延迟回放。重新打开任务本就整包重载（loadTaskDetail
// 全量重拉），这里丢弃的全部会被重建，不损失任何可见状态。
// 注意：必须在 stashTaskDetailViewState / closeTaskDetailWs 之后调用。
function releaseTaskDetailRetainedState() {
    S.taskNodeDetails = {};
    S.taskNodeDetailRequests = {};
    S.taskNodeLatestContexts = {};
    S.taskNodeLatestContextRequests = {};
    S.taskNodePatchSummaries = {};
    if (S.taskNodeErrorHistories) S.taskNodeErrorHistories = {};
    S.currentNodeDetail = null;
    S.recentModelCalls = [];
    S.taskModelCallPaging = null;
    S.taskModelCallPageRows = [];
    S.taskErrorLogs = [];
    S.taskArtifacts = [];
    S.selectedArtifactId = "";
    S.artifactContent = "";
    S.frontier = [];
    S.taskRuntimeSummary = null;
    S.liveFrameMap = {};
    if (typeof resetTaskTreeSnapshotState === "function") resetTaskTreeSnapshotState();
    if (U.tree) U.tree.innerHTML = "";
    if (U.adFlow) U.adFlow.innerHTML = "";
    if (U.adMessages) U.adMessages.innerHTML = "";
    if (U.artifactList) U.artifactList.innerHTML = "";
    if (U.artifactContent) U.artifactContent.textContent = "";
}

function resetTaskView() {
    closeTaskDetailWs();
    clearTaskDetailSession();
    S.currentTask = null;
    S.taskSummary = null;
    S.taskRuntimeSummary = null;
    S.rootNode = null;
    S.frontier = [];
    S.recentModelCalls = [];
    S.taskModelCallsPage = 1;
    S.taskModelCallPaging = null;
    S.taskModelCallPageRows = [];
    S.taskModelCallPageLoading = false;
    S.taskModelCallsQuery = "";
    S.taskModelCallsPageSize = typeof TASK_MODEL_CALLS_PAGE_SIZE === "number" && TASK_MODEL_CALLS_PAGE_SIZE > 0
        ? TASK_MODEL_CALLS_PAGE_SIZE
        : 100;
    S.liveFrameMap = {};
    S.currentNodeDetail = null;
    S.taskNodeDetails = {};
    S.taskNodePatchSummaries = {};
    S.taskNodeDetailRequests = {};
    S.taskNodeLatestContexts = {};
    S.taskNodeLatestContextRequests = {};
    if (typeof resetTaskTreeSnapshotState === "function") resetTaskTreeSnapshotState();
    if (typeof stopTaskNoticeSettle === "function") stopTaskNoticeSettle();
    S.taskNodeBusy = false;
    S.taskErrorLogs = [];
    S.taskErrorLogOpen = false;
    S.taskArtifacts = [];
    S.selectedArtifactId = "";
    S.artifactContent = "";
    S.treeView = null;
    S.treeSelectedRoundByNodeId = {};
    S.selectedNodeId = null;
    S.treePan.active = false;
    S.treePan.originNodeId = null;
    S.treePan.offsetX = 0;
    S.treePan.offsetY = 0;
    S.treePan.baseOffsetX = 0;
    S.treePan.baseOffsetY = 0;
    S.treePan.scale = 1;
    S.treePan.baseScale = 1;
    S.treePan.moved = false;
    S.treePan.suppressClickNodeId = null;
    S.treeLocateHighlight = null;
    S.treeFitOnNextRender = false;
    U.tree.innerHTML = '<div class="empty-state">Waiting for task tree...</div>';
    if (U.taskTreeResetRounds) {
        U.taskTreeResetRounds.hidden = true;
        U.taskTreeResetRounds.disabled = true;
        U.taskTreeResetRounds.classList.remove("active");
        U.taskTreeResetRounds.title = "轮次信息加载中";
    }
    if (U.tdPromptDisclosure) U.tdPromptDisclosure.open = false;
    if (U.tdTitle) {
        U.tdTitle.textContent = "正在加载...";
        U.tdTitle.title = "";
    }
    if (U.tdStatus) U.tdStatus.textContent = "未知";
    if (U.tdStatusPill) U.tdStatusPill.dataset.status = "unknown";
    U.feedTitle.textContent = "Node Details";
    if (U.adOutput) U.adOutput.textContent = "暂无提交输出";
    if (U.adFlow) U.adFlow.innerHTML = '<div class="empty-state task-trace-empty">选择任务树中的节点后，这里会显示执行流程。</div>';
    if (U.adAcceptance) U.adAcceptance.textContent = "暂无验收结果";
    if (U.adTokenSummary) U.adTokenSummary.textContent = "";
    if (U.nodeEmpty) U.nodeEmpty.style.display = "block";
    if (U.artifactList) U.artifactList.innerHTML = '<div class="empty-state" style="padding: 10px;">No file changes yet.</div>';
    if (U.artifactContent) U.artifactContent.textContent = "展开后查看节点完整上下文。";
    if (U.nodeContextDisclosure) U.nodeContextDisclosure.open = false;
    renderFlowHeading(0);
    renderArtifactHeading(0);
    syncTaskTreeHeaderState(null);
    refreshTaskDetailScrollRegions();
    if (U.taskTokenButton) U.taskTokenButton.disabled = true;
    if (U.taskTokenContent) U.taskTokenContent.innerHTML = '<div class="empty-state">请选择一个任务后查看 token 统计。</div>';
    setTaskTokenStatsOpen(false);
    setTaskSelectionEmptyVisible(false);
    hideAgent();
}

function setTaskDetailOpen(open) {
    setDrawerOpen(U.taskDetailBackdrop, U.taskDetailDrawer, open);
}

function setTaskErrorLogOpen(open) {
    S.taskErrorLogOpen = !!open;
    if (typeof setDrawerOpen === "function") {
        setDrawerOpen(U.taskErrorLogBackdrop, U.taskErrorLogDrawer, S.taskErrorLogOpen);
    } else {
        U.taskErrorLogBackdrop?.classList.toggle("is-open", S.taskErrorLogOpen);
        U.taskErrorLogDrawer?.classList.toggle("is-open", S.taskErrorLogOpen);
    }
}

function renderTaskErrorLog() {
    if (!U.taskErrorLogContent) return;
    const items = Array.isArray(S.taskErrorLogs) ? S.taskErrorLogs : [];
    if (U.taskErrorLogSummary) U.taskErrorLogSummary.textContent = items.length ? `共 ${items.length} 条错误记录` : "暂无错误日志";
    if (!items.length) {
        U.taskErrorLogContent.innerHTML = '<div class="empty-state">暂无错误日志</div>';
        return;
    }
    U.taskErrorLogContent.innerHTML = items.map((item) => {
        const nodeId = String(item?.node_id || "").trim();
        const title = String(item?.node_title || nodeId || "未知节点").trim();
        const timestamp = String(item?.created_at || "").trim();
        const errorText = String(item?.error_text || "").trim();
        return `<article class="task-error-log-item">
            <div class="task-error-log-meta"><time>${esc(timestamp)}</time><span>${esc(title)}</span></div>
            <button type="button" class="task-error-log-node-link" data-error-node-id="${esc(nodeId)}">${esc(nodeId)}</button>
            <pre class="task-error-log-text">${esc(errorText)}</pre>
        </article>`;
    }).join("");
    U.taskErrorLogContent.querySelectorAll("[data-error-node-id]").forEach((button) => {
        button.addEventListener("click", () => {
            const nodeId = String(button.dataset.errorNodeId || "").trim();
            setTaskErrorLogOpen(false);
            void focusErrorLogNode(nodeId);
        });
    });
}

async function openTaskErrorLog() {
    const taskId = String(S.currentTaskId || "").trim();
    if (!taskId) return;
    if (U.taskErrorLogContent) U.taskErrorLogContent.innerHTML = '<div class="empty-state">正在加载错误日志...</div>';
    setTaskErrorLogOpen(true);
    try {
        const payload = await ApiClient.getTaskErrorLog(taskId);
        if (String(S.currentTaskId || "").trim() !== taskId) return;
        S.taskErrorLogs = Array.isArray(payload?.items) ? payload.items : [];
        renderTaskErrorLog();
    } catch (error) {
        if (U.taskErrorLogContent) U.taskErrorLogContent.innerHTML = `<div class="empty-state error">${esc(error?.message || "错误日志加载失败")}</div>`;
    }
}

async function focusErrorLogNode(nodeId) {
    // 复用任务树搜索的定位管线：展开祖先节点、居中并一次性高亮；
    // 不传 scale 表示保持当前缩放，与错误日志跳转的历史行为一致。
    await locateTaskNode(nodeId);
}

function setTaskTokenStatsOpen(open) {
    S.taskTokenStatsOpen = !!open;
    setDrawerOpen(U.taskTokenBackdrop, U.taskTokenDrawer, !!open);
    if (!open) return;
    renderTaskTokenStats({ force: true });
    void ensureTaskTokenModelCatalog();
    // 按模型明细不再由任务详情快照带过来（那一格的冷页扫描会拖住"点开任务"），
    // 所以窗口一打开就自己取窄接口。先渲染再取数：总量与调用明细手里已经有。
    // 开窗口带来的这次取数不弹「已刷新」——没人点了刷新，成功反馈是表格填上。
    void refreshTaskTokenLedger({ quiet: true });
}

// 模型目录正常在 boot 时已加载；那一次请求失败会让整片 token 统计退化成裸配置 key，
// 所以打开面板时补一次加载，拿到目录后强制重绘。
async function ensureTaskTokenModelCatalog() {
    if (S.modelCatalog?.loading || (S.modelCatalog?.catalog || []).length) return;
    try {
        await loadModels();
    } catch (error) {
        void error;
        return;
    }
    if (S.taskTokenStatsOpen) renderTaskTokenStats({ force: true });
}

// 账本只记录配置 key（不存显示名），这里经模型目录换成用户写的配置名，与 CEO 会话
// 用量标题同一口径；目录查不到（配置已删）才退回 key 本身。
function taskModelDisplayName(row) {
    const raw = String(row?.model_key || row?.provider_model || row?.provider_id || "").trim();
    if (!raw) return "";
    return ceoModelUsageHeadlineTitle(raw) || raw;
}

// 「任务开始以来共 N 次调用」按服务端计数说，不按明细行的长度说：快照只带最近一窗
// （`_TASK_SNAPSHOT_MODEL_CALL_ROWS`，300 条），实盘在飞单任务的账本是 31686 行——
// 拿行数反推会把三万次调用印成三百次。计数缺失时（旧快照）只声称"已收到多少条明细"。
function taskModelCallLedgerNote(carriedCount, totalCount, pageSize) {
    const carried = Number(carriedCount || 0);
    const total = Number(totalCount || 0);
    const page = Number(pageSize || 0);
    const pageNote = page > 0 ? ` · 每页 ${formatTokenCount(page)} 条` : "";
    if (total > 0 && total > carried) {
        return `任务开始以来共 ${formatTokenCount(total)} 次调用 · 明细带最近 ${formatTokenCount(carried)} 条${pageNote}`;
    }
    if (total > 0) {
        return `任务开始以来共 ${formatTokenCount(total)} 次调用${pageNote}`;
    }
    return `已收到 ${formatTokenCount(carried)} 次调用明细${pageNote}`;
}

// 账本行的身份是「节点 + 该节点自己的轮次序号」：call_index 每节点从 1 重新计
// （实盘单任务 8071 行只覆盖 259 个不同序号），只按 call_index 去重会让一条新调用
// 顶掉其他节点的同序号行。实时合流与手动刷新共用这个键。
function taskModelCallRowKey(call) {
    return `${String(call?.node_id || "").trim()}:${Number(call?.call_index || 0)}`;
}

function mergeTaskModelCallRows(existing, incoming) {
    const rows = Array.isArray(existing) ? existing : [];
    const added = (Array.isArray(incoming) ? incoming : []).filter(Boolean);
    if (!added.length) return rows.slice();
    const addedKeys = new Set(added.map(taskModelCallRowKey));
    return [...rows.filter((item) => item && !addedKeys.has(taskModelCallRowKey(item))), ...added];
}

function setTaskTokenRefreshBusy(busy) {
    const button = U.taskTokenContent?.querySelector?.("[data-task-model-call-refresh]") || null;
    if (!button) return;
    button.disabled = !!busy;
    button.textContent = busy ? "刷新中" : "刷新";
}

// 「刷新」按契约要重新取数，而不是重绘内存：窗口冻结期间 WS 漏掉的行只有取数才补得回。
// 走窄接口（总量 + 按模型明细 + 最近若干条），整份任务详情带的是全量账本。
async function refreshTaskTokenLedger({ quiet = false } = {}) {
    const taskId = String(S.currentTaskId || "").trim();
    if (!taskId || S.taskTokenLedgerRefreshing) return;
    S.taskTokenLedgerRefreshing = true;
    setTaskTokenRefreshBusy(true);
    let payload = null;
    let failure = null;
    try {
        payload = await ApiClient.getTaskTokenLedger(taskId);
    } catch (error) {
        failure = error;
    } finally {
        // 无论取回、报错还是在飞期间切走任务，按钮都不能停在「刷新中」。
        S.taskTokenLedgerRefreshing = false;
        setTaskTokenRefreshBusy(false);
    }
    if (!payload) {
        showToast({ title: "刷新失败", text: failure?.message || "未知错误", kind: "error" });
        return;
    }
    if (String(S.currentTaskId || "").trim() !== taskId) return;
    const rows = (Array.isArray(payload.model_calls) ? payload.model_calls : []).map(normalizeTaskModelCall);
    S.currentTask = {
        ...(S.currentTask || {}),
        token_usage: payload.token_usage || S.currentTask?.token_usage || null,
    };
    S.taskSummary = {
        ...(S.taskSummary || {}),
        token_usage_by_model: Array.isArray(payload.token_usage_by_model)
            ? payload.token_usage_by_model
            : (S.taskSummary?.token_usage_by_model || []),
    };
    // 这份按模型明细属于哪个任务：窗口靠它区分"还在取数"与"真的只有任务级统计"。
    S.taskTokenLedgerTaskId = taskId;
    S.recentModelCalls = mergeTaskModelCallRows(S.recentModelCalls, rows);
    renderTaskTokenStats({ force: true });
    if (quiet) return;
    showToast({
        title: "Token 统计已刷新",
        text: `明细已对齐到最近 ${formatTokenCount(rows.length)} 条调用`,
        kind: "success",
        durationMs: 2200,
    });
}

// 模型调用明细视图状态：默认按时间倒序（同一秒内按调用序号倒序），
// 搜索在进入分页前作用于全部记录（而非当前页），匹配序号、节点 ID 或模型名称。
function taskModelCallTimeValue(call) {
    const parsed = Date.parse(String(call?.created_at || "").trim());
    return Number.isFinite(parsed) ? parsed : 0;
}

// 请求类型：账本里的空串（含历史行）都读成常规节点回合，只有分发控制回合带
// message_distribution。标签同时用于「类型」列展示和搜索框筛选。
const TASK_MODEL_CALL_KIND_LABELS = {
    message_distribution: "分发消息",
};

function taskModelCallKindLabel(call) {
    const kind = String(call?.call_kind || "").trim();
    return TASK_MODEL_CALL_KIND_LABELS[kind] || "普通回合";
}

function taskModelCallMatchesQuery(call, query) {
    const needle = String(query || "").trim().toLowerCase();
    if (!needle) return true;
    if (String(call?.call_index ?? "").includes(needle)) return true;
    if (String(call?.call_kind || "").toLowerCase().includes(needle)) return true;
    if (taskModelCallKindLabel(call).toLowerCase().includes(needle)) return true;
    if (String(call?.node_id || "").toLowerCase().includes(needle)) return true;
    return (Array.isArray(call?.delta_usage_by_model) ? call.delta_usage_by_model : []).some((row) =>
        [taskModelDisplayName(row), row?.model_key, row?.provider_model, row?.provider_id]
            .some((value) => String(value || "").toLowerCase().includes(needle)));
}

function formatModelCallTime(value) {
    const raw = String(value || "").trim();
    if (!raw) return "--";
    const parsed = new Date(raw);
    if (Number.isNaN(parsed.getTime())) return raw;
    const pad = (num) => String(num).padStart(2, "0");
    const time = `${pad(parsed.getHours())}:${pad(parsed.getMinutes())}:${pad(parsed.getSeconds())}`;
    const now = new Date();
    const sameDay = parsed.getFullYear() === now.getFullYear()
        && parsed.getMonth() === now.getMonth()
        && parsed.getDate() === now.getDate();
    return sameDay ? time : `${pad(parsed.getMonth() + 1)}-${pad(parsed.getDate())} ${time}`;
}

// 历史快照态：明细页来自服务端按页取数口，行不进 S.recentModelCalls（那条数组只涨
// 不缩），实时事件也不再插入——页号是按锚点算的，插进来会让整页往后漂。
function isTaskModelCallHistorical() {
    const paging = S.taskModelCallPaging;
    return !!(paging && Number(paging.anchor_seq || 0) > 0);
}

function compareTaskModelCalls(a, b) {
    const timeDiff = taskModelCallTimeValue(b) - taskModelCallTimeValue(a);
    if (timeDiff !== 0) return timeDiff;
    return Number(b.call_index || 0) - Number(a.call_index || 0);
}

function taskModelCallViewState() {
    const query = String(S.taskModelCallsQuery || "").trim();
    if (isTaskModelCallHistorical()) {
        const paging = S.taskModelCallPaging;
        const rows = (Array.isArray(S.taskModelCallPageRows) ? S.taskModelCallPageRows : [])
            .map(normalizeTaskModelCall)
            .sort(compareTaskModelCalls);
        const shown = query ? rows.filter((call) => taskModelCallMatchesQuery(call, query)) : rows.slice();
        const size = Number(paging.size || taskModelCallsPageSize());
        const page = Number(paging.page || 1);
        const grandTotal = Number(paging.total_calls || 0);
        return {
            calls: rows,
            filtered: shown,
            query,
            historical: true,
            meta: {
                total: rows.length,
                pageSize: size,
                totalPages: Math.max(1, Number(paging.total_pages || 1)),
                currentPage: page,
                startIndex: rows.length ? ((page - 1) * size) + 1 : 0,
                endIndex: rows.length ? Math.min(page * size, grandTotal) : 0,
                grandTotal,
                historical: true,
                items: shown,
            },
        };
    }
    const calls = Array.isArray(S.recentModelCalls)
        ? S.recentModelCalls.map(normalizeTaskModelCall)
        : [];
    calls.sort(compareTaskModelCalls);
    const filtered = query ? calls.filter((call) => taskModelCallMatchesQuery(call, query)) : calls.slice();
    const meta = paginateTaskModelCalls(filtered, Number(S.taskSummary?.total_model_calls || 0));
    return { calls, filtered, meta, query, historical: false };
}

function renderTaskTokenCallTableMarkup(meta, filteredCalls, query) {
    if (!filteredCalls.length) {
        const hasQuery = String(query || "").trim();
        return `<div class="empty-state task-token-empty">${hasQuery ? "未找到匹配的记录。" : "暂无逐次调用明细。"}</div>`;
    }
    const rows = meta.items.map((item) => {
        const usageRows = Array.isArray(item.delta_usage_by_model) ? item.delta_usage_by_model : [];
        const modelNames = usageRows.map((row) => taskModelDisplayName(row)).filter(Boolean).join(", ") || "未提供";
        // 悬停保留账本原始 key：配置改名或删除后仍可据此定位是哪一条配置。
        const modelKeys = usageRows.map((row) => String(row?.model_key || "").trim()).filter(Boolean).join(", ") || modelNames;
        // 思考 token 为 null 表示 provider 未上报（旧记录 / 非流式 / 不回传
        // reasoning_tokens），显示 "--" 而不是 0；耗时两列的 null 由 formatDurationMs 兜底。
        const thinkingText = item.thinking_tokens === null ? "--" : formatTokenCount(item.thinking_tokens);
        // 上游中途关掉 SSE 的那一跳根本不会回 usage 块：那三个 0 是"没拿到数"，不是
        // "这次没花钱"（请求体照发，实测单跳 ~5.3 万 token）。按同一套 null 约定显示 "--"。
        const abortedStream = !!item.stream_incomplete;
        const abortedTitle = "上游未回终止分片，这一跳没有 usage 回执";
        const inputText = abortedStream ? "--" : formatTokenCount(item.delta_usage.input_tokens);
        const cacheText = abortedStream ? "--" : formatTokenCount(item.delta_usage.cache_hit_tokens);
        return `
            <tr>
                <td data-task-call-index>${esc(formatTokenCount(item.call_index))}</td>
                <td>${esc(formatModelCallTime(item.created_at))}</td>
                <td class="task-token-call-node-id" title="${esc(item.node_id)}">${esc(item.node_id || "--")}</td>
                <td class="task-token-call-kind" title="${esc(String(item.call_kind || "").trim() || "普通回合")}">${esc(taskModelCallKindLabel(item))}</td>
                <td>${esc(formatTokenCount(item.prepared_message_chars))}</td>
                <td class="task-token-call-token" title="${esc(abortedStream ? abortedTitle : `${formatTokenCount(item.delta_usage.input_tokens)} 新增输入 token`)}">${esc(inputText)}</td>
                <td class="task-token-call-token" title="${esc(abortedStream ? abortedTitle : `${formatTokenCount(item.delta_usage.cache_hit_tokens)} 缓存命中 token`)}">${esc(cacheText)}</td>
                <td>${esc((modelCallHitRate(item) * 100).toFixed(1))}%</td>
                <td class="task-token-call-thinking" title="${esc(item.thinking_tokens === null ? "provider 未上报思考 token" : `${formatTokenCount(item.thinking_tokens)} 思考 token`)}">${esc(thinkingText)}</td>
                <td>${esc(formatTokenCount(item.response_tool_call_count))}</td>
                <td class="task-token-call-duration" title="${esc(item.first_token_ms === null ? "未测到首 token（非流式请求）" : `首 token ${formatDurationMs(item.first_token_ms)}`)}">${esc(formatDurationMs(item.first_token_ms))}</td>
                <td class="task-token-call-duration" title="${esc(item.duration_ms === null ? "未记录耗时" : `本次调用累计 ${formatDurationMs(item.duration_ms)}`)}">${esc(formatDurationMs(item.duration_ms))}</td>
                <td class="task-token-call-model" title="${esc(modelKeys)}">${esc(modelNames)}</td>
            </tr>
        `;
    }).join("");
    return `
        <div class="task-token-call-table-wrap">
            <table class="task-token-call-table">
                <thead>
                    <tr>
                        <th>序号</th>
                        <th>时间</th>
                        <th>节点ID</th>
                        <th>类型</th>
                        <th>预处理字符数</th>
                        <th>新增输入 Token</th>
                        <th>缓存命中</th>
                        <th>命中率</th>
                        <th>思考 Token</th>
                        <th>工具调用数</th>
                        <th>首 Token 耗时</th>
                        <th>总耗时</th>
                        <th>模型</th>
                    </tr>
                </thead>
                <tbody>
                    ${rows}
                </tbody>
            </table>
        </div>
        <div class="task-token-call-footer">
            <div class="task-token-call-page-info">${esc(taskModelCallPageSummary(meta))}${meta.historical ? " · 历史快照" : ""}</div>
            <div class="task-token-call-actions">
                ${meta.historical ? '<button class="toolbar-btn ghost" type="button" data-task-model-call-live>回到最新</button>' : ""}
                <button class="toolbar-btn ghost" type="button" data-task-model-call-page="prev" ${meta.currentPage <= 1 ? "disabled" : ""}>上一页</button>
                <button class="toolbar-btn ghost" type="button" data-task-model-call-page="next" ${meta.currentPage >= meta.totalPages ? "disabled" : ""}>下一页</button>
                <input class="task-token-call-search task-token-call-jump" type="number" min="1" max="${esc(String(meta.totalPages))}"
                    data-task-model-call-jump aria-label="跳转到页" placeholder="页">
                <button class="toolbar-btn ghost" type="button" data-task-model-call-goto ${S.taskModelCallPageLoading ? "disabled" : ""}>跳转</button>
            </div>
        </div>
    `;
}

// 只重建表格区域（保留搜索框焦点/内容与卡片头部），搜索与翻页共用。
function refreshTaskTokenCallTable() {
    if (!U.taskTokenContent) return;
    const region = U.taskTokenContent.querySelector?.("[data-task-model-call-region]") || null;
    if (!region) {
        renderTaskTokenStats({ force: true });
        return;
    }
    const callViewState = taskModelCallViewState();
    region.innerHTML = renderTaskTokenCallTableMarkup(callViewState.meta, callViewState.filtered, callViewState.query);
}

function taskModelCallsPageSize() {
    const fallback = typeof TASK_MODEL_CALLS_PAGE_SIZE === "number" && TASK_MODEL_CALLS_PAGE_SIZE > 0
        ? TASK_MODEL_CALLS_PAGE_SIZE
        : 100;
    const next = Number(S.taskModelCallsPageSize || 0);
    return Number.isInteger(next) && next > 0 ? next : fallback;
}

function paginateTaskModelCalls(items, grandTotal = 0) {
    const total = Array.isArray(items) ? items.length : 0;
    const pageSize = taskModelCallsPageSize();
    const loadedPages = Math.max(1, Math.ceil(total / pageSize));
    // 页号按整本账本算，不按已加载的行数算：底栏要显示"共 498 页"，而明细只带最近
    // 一窗（实盘单任务 49,758 行 / 123.9 MB，300 条窗口只覆盖 1 小时 42 分）。
    const full = Math.max(Number(grandTotal || 0), total);
    const totalPages = Math.max(loadedPages, Math.ceil(full / pageSize));
    const requestedPage = Number(S.taskModelCallsPage || 1);
    const currentPage = Number.isFinite(requestedPage)
        ? Math.min(Math.max(1, Math.floor(requestedPage)), loadedPages)
        : 1;
    const startIndex = total ? ((currentPage - 1) * pageSize) + 1 : 0;
    const endIndex = total ? Math.min(currentPage * pageSize, total) : 0;
    const startOffset = total ? startIndex - 1 : 0;
    S.taskModelCallsPage = currentPage;
    S.taskModelCallsPageSize = pageSize;
    return {
        total,
        grandTotal: full,
        pageSize,
        totalPages,
        currentPage,
        startIndex,
        endIndex,
        historical: false,
        items: total ? items.slice(startOffset, startOffset + pageSize) : [],
    };
}

function taskModelCallPageSummary(meta) {
    const grand = Number(meta.grandTotal || meta.total || 0);
    const pages = Math.max(1, Number(meta.totalPages || 1));
    if (!meta.total) return `第 1/${formatTokenCount(pages)} 页 · 共 ${formatTokenCount(grand)} 条`;
    return `第 ${formatTokenCount(meta.currentPage)}/${formatTokenCount(pages)} 页 · 显示 ${formatTokenCount(meta.startIndex)}-${formatTokenCount(meta.endIndex)} / 共 ${formatTokenCount(grand)} 条`;
}

function setTaskModelCallsPage(page) {
    const next = Number(page);
    const target = Number.isFinite(next) ? Math.max(1, Math.floor(next)) : 1;
    if (isTaskModelCallHistorical()) {
        void loadTaskModelCallPage(target);
        return;
    }
    const loadedPages = Math.ceil((Array.isArray(S.recentModelCalls) ? S.recentModelCalls.length : 0) / taskModelCallsPageSize());
    // 窗口内仍走本地切片（零请求）；越过窗口才发那一跳——点了才加载。
    if (target <= loadedPages) {
        S.taskModelCallsPage = target;
        refreshTaskTokenCallTable();
        return;
    }
    void loadTaskModelCallPage(target);
}

function setTaskModelCallPagerBusy(busy) {
    const region = U.taskTokenContent?.querySelector?.("[data-task-model-call-region]") || null;
    if (!region || typeof region.querySelectorAll !== "function") return;
    region.querySelectorAll("[data-task-model-call-page],[data-task-model-call-goto]").forEach((node) => {
        node.disabled = !!busy;
    });
}

async function loadTaskModelCallPage(page, { rebase = false } = {}) {
    const taskId = String(S.currentTaskId || "").trim();
    if (!taskId || S.taskModelCallPageLoading) return;
    S.taskModelCallPageLoading = true;
    setTaskModelCallPagerBusy(true);
    let payload = null;
    let failure = null;
    try {
        payload = await ApiClient.getTaskModelCallPage(taskId, {
            page,
            size: taskModelCallsPageSize(),
            anchor: rebase ? null : (S.taskModelCallPaging?.anchor_seq || null),
        });
    } catch (error) {
        failure = error;
    } finally {
        S.taskModelCallPageLoading = false;
        setTaskModelCallPagerBusy(false);
    }
    if (!payload) {
        if (failure) showToast({ title: "加载失败", text: failure?.message || "未知错误", kind: "error" });
        return;
    }
    if (String(S.currentTaskId || "").trim() !== taskId) return;
    S.taskModelCallPaging = {
        anchor_seq: Number(payload.anchor_seq || 0),
        page: Number(payload.page || page),
        size: Number(payload.size || taskModelCallsPageSize()),
        total_calls: Number(payload.total_calls || 0),
        total_pages: Number(payload.total_pages || 1),
    };
    S.taskModelCallsPage = S.taskModelCallPaging.page;
    S.taskModelCallPageRows = (Array.isArray(payload.model_calls) ? payload.model_calls : []).map(normalizeTaskModelCall);
    refreshTaskTokenCallTable();
}

// 「回到最新」：退出快照态、把实时合流打开，并重取最近一窗（刷新口自带尾部 300 条）。
function exitTaskModelCallHistory() {
    if (!isTaskModelCallHistorical()) return;
    S.taskModelCallPaging = null;
    S.taskModelCallPageRows = [];
    S.taskModelCallsPage = 1;
    void refreshTaskTokenLedger();
}

async function loadTaskDetail(taskId, { preserveView = false, reopenSocket = true } = {}) {
    if (!taskId) return;
    if (!preserveView) {
        S.currentTaskId = taskId;
        switchView("task-details");
        resetTaskView();
        S.treeFitOnNextRender = true;
    }
    // 大任务详情请求可能较慢：超过阈值仍未进入树加载阶段时显示进度提示条。
    // 只有真正的"打开任务"（preserveView 为假）才配这条提示：暂停/恢复/批量操作
    // 后的对齐刷新、断线重连的整树重拉都发生在已经打开的任务里，报"正在打开任务"
    // 会让用户以为任务被重新打开了一次。
    const announceLoad = !preserveView;
    if (announceLoad) scheduleTaskTreeLoadNotice(taskId);
    let payload;
    try {
        payload = await ApiClient.getTask(taskId, true);
    } catch (error) {
        // 详情失败后进度提示没有继续存在的意义：清掉延迟提示条（含已显示的），
        // 由外层 openTask / restoreTaskDetailSession 展示错误信息。
        cancelTaskTreeLoadNotice(taskId);
        throw error;
    }
    // 已离开详情视图（加载途中点了返回）：不再应用详情、不重开详情 WS、
    // 也不再启动树快照分块加载——否则尾巴会在大厅视图里继续重建整树。
    if (String(S.currentTaskId || "").trim() !== String(taskId || "").trim() || !taskDetailViewVisible()) return null;
    applyTaskPayload(payload);
    if (reopenSocket) {
        openTaskDetailWs(taskId);
    }
    // 打开任务：整树分块加载期间显示"正在打开任务"进度提示条（原地刷新不显示）。
    await loadTaskTreeSnapshot(taskId, { announceLoad });
    return payload;
}

async function restoreTaskDetailSession() {
    const snapshot = readTaskDetailSessionSnapshot();
    if (!snapshot?.currentTaskId) return false;
    try {
        await loadTaskDetail(snapshot.currentTaskId);
        S.taskDetailViewStates = snapshot.nodeViewStates || {};
        S.treeSelectedRoundByNodeId = normalizeTreeRoundSelections(snapshot.treeSelectedRoundByNodeId);
        if (String(S.treeRootNodeId || "").trim()) renderTree();
        S.selectedArtifactId = snapshot.selectedArtifactId || "";
        if (snapshot.selectedNodeId) {
            const pendingViewState = getStoredTaskDetailViewState(snapshot.currentTaskId, snapshot.selectedNodeId);
            S.pendingTaskDetailRestore = { nodeId: snapshot.selectedNodeId, viewState: pendingViewState };
            S.selectedNodeId = snapshot.selectedNodeId;
            renderTree();
        }
        await loadTaskArtifacts();
        scheduleTaskDetailSessionPersist();
        return true;
    } catch {
        clearTaskDetailSession();
        return false;
    }
}

async function openTask(taskId) {
    try {
        await loadTaskDetail(taskId);
        await loadTaskArtifacts();
        scheduleTaskDetailSessionPersist();
    } catch (e) {
        cancelTaskTreeLoadNotice(String(taskId || ""));
        U.tree.innerHTML = `<div class="empty-state error">Failed to open task: ${esc(e.message)}</div>`;
        showToast({ title: "Task open failed", text: e.message || "Unknown error", kind: "error" });
    }
}

function isAbortLike(error) {
    return String(error?.name || "").trim() === "AbortError";
}

function applyTaskListResponse(payload = {}) {
    const items = Array.isArray(payload?.items) ? payload.items : [];
    syncTaskNormalizedState(items);
    S.lastTaskSummaryPatchAt = new Date().toISOString();
    S.taskListDirtyWhileHidden = false;
    S.taskListReconnectNeedsReconcile = false;
    applyTaskWorkerStatus(payload || {}, { render: false });
    syncTaskSelection();
    renderTaskSessionScope();
    if (taskListViewVisible()) renderTasks();
    else noteTaskHallHiddenDefer();
}

function resolveTaskWorkerState({
    worker = S.tasksWorker,
    reportedState = S.tasksWorkerReportedState,
    staleAfterSeconds = S.tasksWorkerStaleAfterSeconds,
    lastSeenAt = S.tasksWorkerLastSeenAt,
} = {}) {
    const record = worker && typeof worker === "object" ? worker : null;
    const normalizedReportedState = normalizeTaskWorkerState(
        reportedState
        || record?.worker_state
        || record?.state
        || (S.tasksWorkerReportedOnline === false ? "offline" : "online")
    );
    if (["starting", "stopped", "offline"].includes(normalizedReportedState)) return normalizedReportedState;
    const updatedAt = String(lastSeenAt || record?.updated_at || "").trim();
    const staleWindowSeconds = Number(staleAfterSeconds);
    if (updatedAt && Number.isFinite(staleWindowSeconds) && staleWindowSeconds > 0) {
        const updatedMs = Date.parse(updatedAt);
        if (Number.isFinite(updatedMs) && Math.max(0, Date.now() - updatedMs) > staleWindowSeconds * 1000) {
            return "stale";
        }
    }
    return normalizedReportedState === "stale" ? "stale" : "online";
}

function refreshTaskWorkerState({ render = true, force = false } = {}) {
    const next = resolveTaskWorkerState();
    const changed = next !== normalizeTaskWorkerState(S.tasksWorkerState);
    S.tasksWorkerState = next;
    S.tasksWorkerOnline = next === "online";
    S.tasksWorkerControlAvailable = next === "online";
    if (render && (force || changed)) renderTasks();
    return changed;
}

function refreshTaskWorkerOnlineState(options = {}) {
    return refreshTaskWorkerState(options);
}

function applyTaskWorkerStatus(payload = {}, { render = true } = {}) {
    S.tasksWorkerReportedOnline = payload?.worker_online !== false;
    S.tasksWorker = payload?.worker || null;
    S.tasksWorkerStatusPayload = payload && typeof payload === "object" ? payload : null;
    S.tasksWorkerReportedState = normalizeTaskWorkerState(
        payload?.worker_state
        || S.tasksWorker?.worker_state
        || S.tasksWorker?.state
        || (payload?.worker_online === false ? "offline" : "online")
    );
    S.tasksWorkerLastSeenAt = String(payload?.worker_last_seen_at || S.tasksWorker?.updated_at || "").trim();
    const staleAfterSeconds = Number(payload?.worker_stale_after_seconds);
    if (Number.isFinite(staleAfterSeconds) && staleAfterSeconds > 0) {
        S.tasksWorkerStaleAfterSeconds = staleAfterSeconds;
    }
    renderTaskPerformanceBar();
    refreshTaskWorkerState({ render, force: true });
}

async function refreshTaskWorkerStatus({ render = true } = {}) {
    try {
        const payload = await ApiClient.getTaskWorkerStatus();
        applyTaskWorkerStatus(payload || {}, { render });
    } catch (error) {
        if (isAbortLike(error)) return;
    }
}

function startTaskWorkerStatusPolling() {
    if (!S.taskWorkerStatusPollId) {
        void refreshTaskWorkerStatus({ render: !!U.viewTasks?.classList.contains("active") });
        S.taskWorkerStatusPollId = window.setInterval(() => {
            void refreshTaskWorkerStatus({ render: !!U.viewTasks?.classList.contains("active") });
            const lastSummaryPatchMs = S.lastTaskSummaryPatchAt ? Date.parse(S.lastTaskSummaryPatchAt) : Number.NaN;
            const lastTokenPatchMs = S.lastTaskTokenPatchAt ? Date.parse(S.lastTaskTokenPatchAt) : Number.NaN;
            const lastPatchMs = Math.max(
                Number.isFinite(lastSummaryPatchMs) ? lastSummaryPatchMs : -Infinity,
                Number.isFinite(lastTokenPatchMs) ? lastTokenPatchMs : -Infinity,
            );
            const hasRunningTasks = (S.tasks || []).some((task) => taskStatusKey(task) === "in_progress");
            if (
                taskListViewVisible()
                && hasRunningTasks
                && (!Number.isFinite(lastPatchMs) || (Date.now() - lastPatchMs) > TASK_SUMMARY_IDLE_RECONCILE_MS)
            ) {
                void loadTasks();
            }
        }, 5000);
    }
    if (!S.taskPerformanceRefreshId) {
        renderTaskPerformanceBar();
        S.taskPerformanceRefreshId = window.setInterval(() => {
            renderTaskPerformanceBar();
            refreshTaskWorkerState({ render: !!U.viewTasks?.classList.contains("active") });
            ensureTaskListVisibleReconcile();
        }, 1000);
    }
}

function stopTaskWorkerStatusPolling() {
    if (S.taskWorkerStatusPollId) {
        window.clearInterval(S.taskWorkerStatusPollId);
        S.taskWorkerStatusPollId = null;
    }
    if (S.taskPerformanceRefreshId) {
        window.clearInterval(S.taskPerformanceRefreshId);
        S.taskPerformanceRefreshId = null;
    }
}

// 任务列表 WebSocket 的帧表：每个 entry 只处理自己那一种 type，未知帧直接丢弃。
;

