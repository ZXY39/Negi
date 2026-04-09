// Task list, task detail loading, and task event orchestration extracted from org_graph_app.js.
// Loaded after org_graph_app.js and before org_graph_llm.js.

const TASK_SUMMARY_IDLE_RECONCILE_MS = 15_000;


function taskStatusLabel(task) {
    return ({ in_progress: "Running", success: "Done", failed: "Failed", blocked: "Paused", continued: "\u5df2\u7eed\u8dd1", unpassed: "\u672a\u901a\u8fc7", unknown: "Unknown" })[taskStatusKey(task)] || "Unknown";
}

function statusBucketMatches(task, bucketKey) {
    const status = taskStatusKey(task);
    if (bucketKey === "paused") return status === "blocked";
    if (bucketKey === "failed") return status === "failed";
    if (bucketKey === "unread") return !!task?.is_unread;
    if (bucketKey === "running") return status === "in_progress";
    return false;
}

function getSelectedTasks() {
    return S.tasks.filter((task) => S.selectedTaskIds.has(task.task_id));
}

function setTaskMenuVisibility() {
    const filterOpen = !!(S.multiSelectMode && S.taskFilterMenuOpen);
    const batchOpen = !!(S.multiSelectMode && S.taskBatchMenuOpen);
    if (U.taskFilterWrap) U.taskFilterWrap.hidden = !S.multiSelectMode;
    if (U.taskBatchWrap) U.taskBatchWrap.hidden = !S.multiSelectMode;
    if (U.taskFilterMenu) U.taskFilterMenu.hidden = !filterOpen;
    if (U.taskBatchMenu) U.taskBatchMenu.hidden = !batchOpen;
    U.taskFilterTrigger?.setAttribute("aria-expanded", filterOpen ? "true" : "false");
    U.taskBatchTrigger?.setAttribute("aria-expanded", batchOpen ? "true" : "false");
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
    if (kind === "filter") {
        S.taskFilterMenuOpen = !!open;
        if (open) S.taskBatchMenuOpen = false;
    } else {
        S.taskBatchMenuOpen = !!open;
        if (open) S.taskFilterMenuOpen = false;
    }
    setTaskMenuVisibility();
}

function closeTaskMenus({ restoreFocus = false } = {}) {
    const hadToolbarOpen = !!(S.taskFilterMenuOpen || S.taskBatchMenuOpen);
    S.taskFilterMenuOpen = false;
    S.taskBatchMenuOpen = false;
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

function compareTaskListOrder(left, right) {
    const timeDiff = taskCreatedSortValue(right) - taskCreatedSortValue(left);
    if (timeDiff !== 0) return timeDiff;
    const rightCreatedAt = String(right?.created_at || "");
    const leftCreatedAt = String(left?.created_at || "");
    if (rightCreatedAt !== leftCreatedAt) return rightCreatedAt.localeCompare(leftCreatedAt);
    return String(left?.task_id || "").localeCompare(String(right?.task_id || ""));
}

function syncTaskNormalizedState(items = []) {
    const normalized = [...(Array.isArray(items) ? items : [])]
        .filter((item) => item && typeof item === "object" && String(item.task_id || "").trim())
        .sort(compareTaskListOrder);
    const tasksById = {};
    const orderedTaskIds = [];
    normalized.forEach((item) => {
        const taskId = String(item.task_id || "").trim();
        if (!taskId) return;
        tasksById[taskId] = item;
        orderedTaskIds.push(taskId);
    });
    S.tasksById = tasksById;
    S.orderedTaskIds = orderedTaskIds;
    S.tasks = normalized;
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
    const tokenUsage = taskTokenUsage(task);
    return tokenUsage.tracked ? {
        input_tokens: Number(tokenUsage.input_tokens || 0),
        output_tokens: Number(tokenUsage.output_tokens || 0),
        cache_hit_tokens: Number(tokenUsage.cache_hit_tokens || 0),
    } : null;
}

function taskCardPatchEligible(previousTask, nextTask) {
    if (!previousTask || !nextTask) return false;
    return String(previousTask.status || "") === String(nextTask.status || "")
        && !!previousTask.is_paused === !!nextTask.is_paused
        && !!previousTask.is_unread === !!nextTask.is_unread
        && taskFailureClass(previousTask) === taskFailureClass(nextTask)
        && taskFinalAcceptanceStatus(previousTask) === taskFinalAcceptanceStatus(nextTask)
        && taskContinuationState(previousTask) === taskContinuationState(nextTask)
        && taskContinuedByTaskId(previousTask) === taskContinuedByTaskId(nextTask)
        && taskRetryCount(previousTask) === taskRetryCount(nextTask)
        && taskRecoveryNotice(previousTask) === taskRecoveryNotice(nextTask)
        && String(previousTask.title || "") === String(nextTask.title || "")
        && String(previousTask.brief || "") === String(nextTask.brief || "")
        && Number(previousTask.max_depth || 0) === Number(nextTask.max_depth || 0);
}

function patchTaskCardElement(taskId) {
    const key = String(taskId || "").trim();
    if (!key) return false;
    const task = S.tasksById?.[key];
    if (!task) return false;
    const card = U.taskGrid?.querySelector?.(`.project-card[data-task-id="${CSS.escape(key)}"]`);
    if (!(card instanceof HTMLElement)) return false;
    const titleEl = card.querySelector("[data-task-title]");
    if (titleEl) {
        const title = String(task.title || key);
        titleEl.textContent = title;
        titleEl.setAttribute("title", title);
    }
    const tokenUsage = taskTokenUsage(task);
    const previousMetrics = S.taskMetricSnapshot?.[key] || null;
    const nextMetrics = taskMetricSnapshotValue(task);
    ["input_tokens", "output_tokens", "cache_hit_tokens"].forEach((metricKey) => {
        const valueEl = card.querySelector(`[data-task-metric-value="${metricKey}"]`);
        const wrapEl = card.querySelector(`[data-task-metric="${metricKey}"]`);
        if (!valueEl || !wrapEl) return;
        const metricValue = tokenUsage.tracked ? Number(tokenUsage[metricKey] || 0) : null;
        const previousValue = previousMetrics && Number.isFinite(previousMetrics[metricKey]) ? previousMetrics[metricKey] : null;
        const isIncreasing = previousValue !== null && Number.isFinite(metricValue) && metricValue > previousValue;
        valueEl.textContent = Number.isFinite(metricValue) ? formatTokenCount(metricValue) : "--";
        wrapEl.classList.toggle("is-increasing", !!isIncreasing);
        valueEl.classList.toggle("is-increasing", !!isIncreasing);
    });
    S.taskMetricSnapshot = {
        ...(S.taskMetricSnapshot || {}),
        [key]: nextMetrics,
    };
    const nextStats = { ...(S.taskHallStats || {}) };
    nextStats.task_hall_card_patch_count = Number(nextStats.task_hall_card_patch_count || 0) + 1;
    S.taskHallStats = nextStats;
    trackTaskCardPatchQueueAge(key);
    const nextQueued = { ...(S.taskCardPatchQueuedAt || {}) };
    delete nextQueued[key];
    S.taskCardPatchQueuedAt = nextQueued;
    return true;
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

function taskWorkerPressureSampleAgeMs(metrics = taskWorkerStatusMetrics()) {
    const sampleAt = String(metrics?.pressure_sample_at || metrics?.tool_pressure_sample_at || "").trim();
    const parsedMs = sampleAt ? Date.parse(sampleAt) : Number.NaN;
    if (Number.isFinite(parsedMs)) return Math.max(0, Date.now() - parsedMs);
    const rawAge = Number(metrics?.pressure_sample_age_ms);
    return Number.isFinite(rawAge) && rawAge >= 0 ? rawAge : null;
}

function taskWorkerPressureSnapshotFresh(metrics = taskWorkerStatusMetrics()) {
    if (metrics?.pressure_snapshot_fresh != null) return !!metrics.pressure_snapshot_fresh;
    const ageMs = taskWorkerPressureSampleAgeMs(metrics);
    if (ageMs == null) return false;
    return ageMs <= 3000 && metrics?.machine_pressure_available !== false;
}

function formatTaskWorkerSampleFreshness(metrics = taskWorkerStatusMetrics()) {
    const ageMs = taskWorkerPressureSampleAgeMs(metrics);
    if (ageMs == null) return "未采样";
    if (!taskWorkerPressureSnapshotFresh(metrics)) return "监控过期";
    if (ageMs < 1000) return "刚刚更新";
    if (ageMs < 60_000) {
        const seconds = ageMs / 1000;
        return seconds < 10 ? `${seconds.toFixed(1)}s 前` : `${Math.round(seconds)}s 前`;
    }
    const minutes = ageMs / 60_000;
    return `${minutes < 10 ? minutes.toFixed(1) : Math.round(minutes)}m 前`;
}

function formatTaskWorkerPercent(value) {
    const numeric = Number(value);
    return Number.isFinite(numeric) ? `${Math.round(numeric)}%` : "--";
}

function queueMetricCount(value) {
    const numeric = Number(value);
    return Number.isFinite(numeric) ? String(Math.max(0, Math.trunc(numeric))) : "--";
}

function taskWorkerPressureStateMeta(metrics = taskWorkerStatusMetrics()) {
    const workerState = normalizeTaskWorkerState(S.tasksWorkerState);
    if (workerState === "offline" || workerState === "stopped") return { key: "offline", label: "离线" };
    if (workerState === "starting") return { key: "starting", label: "启动中" };
    if (workerState === "stale") return { key: "stale", label: "连接过期" };
    if (!taskWorkerPressureSnapshotFresh(metrics)) return { key: "unfresh", label: "监控过期" };
    const state = String(metrics?.budget_state || metrics?.tool_pressure_state || metrics?.worker_execution_state || "normal").trim().toLowerCase();
    if (state === "critical") return { key: "critical", label: "强收紧" };
    if (state === "throttled") return { key: "throttled", label: "收紧中" };
    if (state === "easing") return { key: "easing", label: "放宽中" };
    return { key: "normal", label: "正常" };
}

function renderTaskPerformanceBar() {
    if (!U.taskPerformanceBar) return;
    const metrics = taskWorkerStatusMetrics();
    const pressureState = taskWorkerPressureStateMeta(metrics);
    const cpuText = formatTaskWorkerPercent(metrics?.machine_pressure_cpu_percent);
    const memoryText = formatTaskWorkerPercent(metrics?.machine_pressure_memory_percent);
    const diskText = metrics?.machine_pressure_disk_busy_available === false
        ? "--"
        : formatTaskWorkerPercent(metrics?.machine_pressure_disk_busy_percent);
    const toolRunningText = queueMetricCount(metrics?.tool_queue_running_count);
    const toolWaitingText = queueMetricCount(metrics?.tool_queue_waiting_count);
    const nodeRunningText = queueMetricCount(metrics?.node_queue_running_count);
    const nodeWaitingText = queueMetricCount(metrics?.node_queue_waiting_count);
    U.taskPerformanceBar.hidden = false;
    U.taskPerformanceBar.innerHTML = `
        <div class="task-performance-item task-performance-item--state" data-state="${esc(pressureState.key)}">
            <span class="task-performance-label">压力状态</span>
            <strong class="task-performance-value">${esc(pressureState.label)}</strong>
        </div>
        <div class="task-performance-item">
            <span class="task-performance-label">CPU/内存/磁盘</span>
            <strong class="task-performance-value">${esc(`${cpuText} / ${memoryText} / ${diskText}`)}</strong>
        </div>
        <div class="task-performance-item">
            <span class="task-performance-label">工具队列</span>
            <strong class="task-performance-value task-performance-queue-value"><span class="task-performance-count task-performance-count--running">${esc(toolRunningText)}</span>运行 / <span class="task-performance-count task-performance-count--waiting">${esc(toolWaitingText)}</span>等待</strong>
        </div>
        <div class="task-performance-item">
            <span class="task-performance-label">节点队列</span>
            <strong class="task-performance-value task-performance-queue-value"><span class="task-performance-count task-performance-count--running">${esc(nodeRunningText)}</span>运行 / <span class="task-performance-count task-performance-count--waiting">${esc(nodeWaitingText)}</span>等待</strong>
        </div>
        <div class="task-performance-item">
            <span class="task-performance-label">监控新鲜度</span>
            <strong class="task-performance-value">${esc(formatTaskWorkerSampleFreshness(metrics))}</strong>
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


function taskGridRenderSignature(meta) {
    const visibleItems = Array.isArray(meta?.items) ? meta.items : [];
    return JSON.stringify({
        total: Number(meta?.total || 0),
        currentPage: Number(meta?.currentPage || 1),
        pageSize: Number(S.taskPageSize || 0),
        workerOnline: taskWorkerControlsAvailable(),
        workerState: normalizeTaskWorkerState(S.tasksWorkerState),
        workerLastSeenAt: String(S.tasksWorkerLastSeenAt || ""),
        taskBusy: !!S.taskBusy,
        multiSelectMode: !!S.multiSelectMode,
        emptyText: taskSessionEmptyText(),
        selectedTaskIds: [...S.selectedTaskIds].map((id) => String(id || "")).sort(),
        items: visibleItems.map((task) => {
            const taskId = String(task?.task_id || "");
            const tokenUsage = taskTokenUsage(task);
            return {
                taskId,
                selected: S.selectedTaskIds.has(taskId),
                title: String(task?.title || ""),
                statusKey: taskStatusKey(task),
                statusLabel: taskStatusLabel(task),
                createdAt: taskCreatedAtText(task),
                tokenUsage: tokenUsage.tracked
                    ? [tokenUsage.input_tokens, tokenUsage.output_tokens, tokenUsage.cache_hit_tokens]
                    : [null, null, null],
                actions: taskCardActions(task).map((action) => `${action.action}:${action.tone}`).join("|"),
            };
        }),
    });
}

function renderTasks() {
    const meta = paginateResources(orderedTasks(S.tasks), S.taskPage, S.taskPageSize);
    S.taskPage = meta.currentPage;
    S.visibleTaskIds = meta.items.map((task) => String(task?.task_id || "").trim()).filter(Boolean);
    syncTaskPagination(meta);
    renderTaskPerformanceBar();
    const signature = taskGridRenderSignature(meta);
    if (signature === S.taskGridSignature) {
        S.taskMetricAnimationTaskIds?.clear?.();
        updateTaskToolbar();
        return;
    }
    S.taskGridSignature = signature;
    S.taskHallStats = {
        ...(S.taskHallStats || {}),
        task_hall_full_render_count: Number(S.taskHallStats?.task_hall_full_render_count || 0) + 1,
    };
    U.taskGrid.innerHTML = "";
    const metricAnimationTaskIds = S.taskMetricAnimationTaskIds || new Set();
    const workerState = normalizeTaskWorkerState(S.tasksWorkerState);
    const workerNotice = taskWorkerNoticeText(workerState);
    if (workerNotice) {
        const warning = document.createElement("div");
        warning.className = `empty-state${workerState === "stale" || workerState === "starting" ? "" : " error"}`;
        warning.style.gridColumn = "1/-1";
        warning.textContent = workerNotice;
        U.taskGrid.appendChild(warning);
    }
    if (!meta.total) {
        if (workerNotice) {
            const empty = document.createElement("div");
            empty.className = "empty-state";
            empty.style.gridColumn = "1/-1";
            empty.textContent = taskSessionEmptyText();
            U.taskGrid.appendChild(empty);
        } else {
            U.taskGrid.innerHTML = `<div class="empty-state" style="grid-column: 1/-1;">${esc(taskSessionEmptyText())}</div>`;
        }
        return updateTaskToolbar();
    }
    const nextTaskMetricSnapshot = {};
    meta.items.forEach((task) => {
        const taskId = String(task?.task_id || "");
        const selected = S.selectedTaskIds.has(taskId);
        const statusKey = taskStatusKey(task);
        const tokenUsage = taskTokenUsage(task);
        const previousMetrics = S.taskMetricSnapshot?.[taskId] || null;
        const metricItems = [
            { key: "input_tokens", label: "输入Token", value: tokenUsage.tracked ? tokenUsage.input_tokens : null },
            { key: "output_tokens", label: "输出Token", value: tokenUsage.tracked ? tokenUsage.output_tokens : null },
            { key: "cache_hit_tokens", label: "缓存命中", value: tokenUsage.tracked ? tokenUsage.cache_hit_tokens : null },
        ];
        nextTaskMetricSnapshot[taskId] = tokenUsage.tracked ? {
            input_tokens: Number(tokenUsage.input_tokens || 0),
            output_tokens: Number(tokenUsage.output_tokens || 0),
            cache_hit_tokens: Number(tokenUsage.cache_hit_tokens || 0),
        } : null;
        const metricsMarkup = metricItems.map((item) => {
            const hasValue = Number.isFinite(item.value);
            const previousValue = previousMetrics && Number.isFinite(previousMetrics[item.key]) ? previousMetrics[item.key] : null;
            const isIncreasing = metricAnimationTaskIds.has(taskId) && previousValue !== null && hasValue && item.value > previousValue;
            return `<div class="pc-metric${isIncreasing ? " is-increasing" : ""}" data-task-metric="${item.key}"><span class="pc-metric-label">${item.label}</span><strong class="pc-metric-value${isIncreasing ? " is-increasing" : ""}" data-task-metric-value="${item.key}">${esc(hasValue ? formatTokenCount(item.value) : "--")}</strong></div>`;
        }).join("");
        const cardActions = taskCardActions(task);
        const continuationSummary = taskContinuationSummary(task);
        const el = document.createElement("div");
        el.className = `project-card${selected ? " is-selected" : ""}${S.multiSelectMode ? " is-multi-mode" : ""}`;
        el.dataset.taskId = taskId;
        el.innerHTML = `
            <div class="pc-topbar">
                <div class="pc-topbar-left">
                    <label class="project-select-toggle${S.multiSelectMode ? " is-visible" : ""}"><input type="checkbox" class="project-select-checkbox" ${selected ? "checked" : ""} ${S.taskBusy ? "disabled" : ""}><span>Select</span></label>
                    <div class="pc-topbar-meta">
                        <span class="status-badge" data-status="${esc(statusKey)}" data-task-status>${esc(taskStatusLabel(task))}</span>
                        <span class="pc-task-id-chip">
                            <span class="pc-task-id-label">Task</span>
                            <span class="pc-task-id-value">${esc(taskId)}</span>
                        </span>
                        <button class="icon-btn pc-copy-btn" type="button" title="复制任务 ID" aria-label="复制任务 ID">
                            <i data-lucide="copy"></i>
                        </button>
                    </div>
                </div>
                ${cardActions.length ? `
                    <div class="pc-card-menu-shell toolbar-dropdown" data-task-menu="${esc(taskId)}">
                        <button class="icon-btn pc-card-menu-trigger" type="button" data-task-menu-toggle="${esc(taskId)}" title="更多操作" aria-label="更多操作" aria-haspopup="menu" aria-expanded="false" ${S.taskBusy ? "disabled" : ""}>
                            <i data-lucide="more-horizontal"></i>
                        </button>
                        <div class="toolbar-menu pc-card-menu" role="menu" hidden>
                            ${cardActions.map((action) => `<button class="toolbar-menu-item ${action.tone}" type="button" data-task-card-action="${action.action}" role="menuitem" ${S.taskBusy ? "disabled" : ""}>${esc(action.label)}</button>`).join("")}
                        </div>
                    </div>
                ` : ""}
            </div>
            <div class="pc-header"><div class="pc-header-left"><h3 class="pc-title" data-task-title title="${esc(task.title || taskId)}">${esc(task.title || taskId)}</h3></div></div>
            <div class="pc-created-at"><span class="pc-field-label">创建时间</span><span class="pc-field-value">${esc(taskCreatedAtText(task))}</span></div>
            ${continuationSummary ? `<div class="pc-continuation-summary">${esc(continuationSummary)}</div>` : ""}
            <div class="pc-metrics">${metricsMarkup}</div>
        `;
        const toggle = el.querySelector(".project-select-toggle");
        const checkbox = el.querySelector(".project-select-checkbox");
        toggle?.addEventListener("click", (e) => e.stopPropagation());
        checkbox?.addEventListener("change", (e) => {
            e.stopPropagation();
            if (e.target.checked) S.selectedTaskIds.add(taskId);
            else S.selectedTaskIds.delete(taskId);
            renderTasks();
        });
        el.querySelector(".pc-copy-btn")?.addEventListener("click", async (e) => {
            e.stopPropagation();
            await copyTaskId(taskId);
        });
        const menuTrigger = el.querySelector("[data-task-menu-toggle]");
        menuTrigger?.addEventListener("click", (e) => {
            e.stopPropagation();
            const shell = menuTrigger.closest(".pc-card-menu-shell");
            const isOpen = !!shell?.classList.contains("is-open");
            setTaskCardMenuOpen(taskId, !isOpen, { restoreFocus: isOpen });
        });
        el.querySelectorAll("[data-task-card-action]").forEach((btn) => btn.addEventListener("click", async (e) => {
            e.stopPropagation();
            closeTaskCardMenus();
            await runTaskAction(taskId, btn.dataset.taskCardAction, { returnFocus: menuTrigger || btn });
        }));
        el.addEventListener("click", () => {
            if (S.multiSelectMode) {
                toggleTaskSelection(taskId);
                return;
            }
            void openTask(taskId);
        });
        U.taskGrid.appendChild(el);
    });
    S.taskMetricSnapshot = nextTaskMetricSnapshot;
    S.pendingTaskCardPatchIds = new Set([...(S.pendingTaskCardPatchIds || new Set())].filter((taskId) => !S.visibleTaskIds.includes(taskId)));
    const nextQueuedAt = { ...(S.taskCardPatchQueuedAt || {}) };
    S.visibleTaskIds.forEach((taskId) => { delete nextQueuedAt[taskId]; });
    S.taskCardPatchQueuedAt = nextQueuedAt;
    S.taskMetricAnimationTaskIds?.clear?.();
    updateTaskToolbar();
    icons();
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
    if (U.taskToolbar) U.taskToolbar.hidden = !S.multiSelectMode;
    if (U.taskMultiToggle) {
        U.taskMultiToggle.setAttribute("aria-pressed", S.multiSelectMode ? "true" : "false");
        U.taskMultiToggle.classList.toggle("active", S.multiSelectMode);
    }
    const filterButtons = [...(U.taskFilterMenu?.querySelectorAll("[data-select-bucket]") || [])];
    filterButtons.forEach((button) => {
        button.disabled = S.taskBusy || !S.tasks.some((task) => statusBucketMatches(task, button.dataset.selectBucket));
    });
    const batchButtons = [...(U.taskBatchMenu?.querySelectorAll("[data-batch-action]") || [])];
    batchButtons.forEach((button) => {
        const action = button.dataset.batchAction;
        const workerReady = !["pause", "resume", "retry"].includes(String(action || "")) || taskWorkerControlsAvailable();
        const enabled = workerReady && (action === "pause"
            ? selected.some((task) => canPause(task))
            : action === "resume"
                ? selected.some((task) => canResume(task))
                : action === "retry"
                    ? selected.some((task) => canRetry(task))
                    : selected.some((task) => canDelete(task)));
        button.disabled = S.taskBusy || !enabled;
    });
    setTaskMenuVisibility();
}

function taskActionTone(action) {
    if (action === "pause") return "warn";
    if (action === "delete") return "danger";
    return "success";
}

function taskCardActions(task) {
    const actions = [];
    if (taskWorkerControlsAvailable() && canPause(task)) actions.push("pause");
    if (taskWorkerControlsAvailable() && canResume(task)) actions.push("resume");
    if (taskWorkerControlsAvailable() && canRetry(task)) actions.push("retry");
    if (taskIsSuperseded(task)) actions.push("open-continuation");
    if (canContinueEvaluate(task)) actions.push("continue-evaluate");
    if (canDelete(task)) actions.push("delete");
    return actions.map((action) => ({ action, label: taskActionText(action), tone: taskActionTone(action) }));
}

function primaryTaskAction(task) {
    if (taskWorkerControlsAvailable() && canPause(task)) return { action: "pause", label: "暂停", tone: "warn" };
    if (taskWorkerControlsAvailable() && canResume(task)) return { action: "resume", label: "开始", tone: "success" };
    if (taskWorkerControlsAvailable() && canRetry(task)) return { action: "retry", label: "重试", tone: "success" };
    if (taskIsSuperseded(task)) return { action: "open-continuation", label: "查看续跑任务", tone: "success" };
    if (canContinueEvaluate(task)) return { action: "continue-evaluate", label: "\u8bc4\u4f30\u7eed\u8dd1", tone: "success" };
    return null;
}

function taskActionText(action) {
    return ({ pause: "暂停", resume: "开始", retry: "重试", "open-continuation": "查看续跑任务", "continue-evaluate": "\u8bc4\u4f30\u7eed\u8dd1", delete: "删除" }[action] || "操作");
}

function taskActionSuccessTitle(action) {
    return action === "delete" ? "删除成功" : `${taskActionText(action)}成功`;
}

function taskActionFailureTitle(action) {
    return action === "delete" ? "删除失败" : `${taskActionText(action)}失败`;
}

function taskActionErrorText(action, error) {
    const message = String(error?.message || error || "").trim();
    if (action === "retry") {
        if (message.includes("task_not_failed")) return "仅失败任务可重试";
        if (message.includes("task_not_retryable")) return "\u8be5\u4efb\u52a1\u4e0d\u5141\u8bb8\u91cd\u8bd5";
        if (message.includes("task_not_found")) return "任务不存在或已被删除";
    }
    if (action === "open-continuation") {
        if (message.includes("task_not_found")) return "续跑任务不存在或已被删除";
        if (message.includes("continuation_task_missing")) return "该任务暂无已确认的续跑任务";
    }
    if (action === "continue-evaluate") {
        if (message.includes("task_not_unpassed")) return "\u4ec5\u672a\u901a\u8fc7\u9a8c\u6536\u7684\u4efb\u52a1\u53ef\u8bc4\u4f30\u7eed\u8dd1";
        if (message.includes("task_not_found")) return "任务不存在或已被删除";
    }
    if (action === "delete") {
        if (message.includes("task_still_stopping")) return "任务仍在停止中，请稍后再删";
        if (message.includes("task_not_deletable") || message.includes("task_not_paused")) return "仅已暂停或已完成的任务可删除";
        if (message.includes("task_not_found")) return "任务不存在或已被删除";
    }
    return message || "Unknown error";
}

function taskActionRequiresWorker(action) {
    return ["pause", "resume", "retry"].includes(String(action || "").trim().toLowerCase());
}

async function requestTaskAction(taskId, action) {
    if (action === "pause") return ApiClient.pauseTask(taskId);
    if (action === "resume") return ApiClient.resumeTask(taskId);
    if (action === "retry") return ApiClient.retryTask(taskId);
    if (action === "continue-evaluate") return ApiClient.continueEvaluateTask(taskId);
    if (action === "delete") return ApiClient.deleteTask(taskId);
    throw new Error(`Unsupported task action: ${action}`);
}

async function runTaskAction(taskId, action, { returnFocus = null } = {}) {
    if (!taskId || !action) return;
    if (action === "open-continuation") {
        const normalizedTaskId = String(taskId || "").trim();
        const task = S.tasksById?.[normalizedTaskId]
            || (Array.isArray(S.tasks) ? S.tasks.find((item) => String(item?.task_id || "").trim() === normalizedTaskId) : null);
        const continuationTaskId = taskContinuedByTaskId(task);
        if (!continuationTaskId) {
            showToast({ title: taskActionFailureTitle(action), text: taskActionErrorText(action, "continuation_task_missing"), kind: "warn" });
            return;
        }
        try {
            await openTask(continuationTaskId);
            showToast({ title: taskActionSuccessTitle(action), text: continuationTaskId, kind: "success" });
        } catch (e) {
            showToast({ title: taskActionFailureTitle(action), text: taskActionErrorText(action, e), kind: "error" });
        }
        return;
    }
    if (action === "delete") {
        openConfirm({
            title: "删除任务",
            text: "删除后将移除任务记录、树快照和工件文件，且无法恢复。",
            confirmLabel: "删除",
            confirmKind: "danger",
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
        const successText = action === "retry"
            ? (result?.task_id || taskId)
            : action === "continue-evaluate"
                ? (result?.continuation_task?.task_id || result?.task?.task_id || taskId)
                : taskId;
        showToast({ title: taskActionSuccessTitle(action), text: successText, kind: "success" });
        await loadTasks();
        if (action === "delete") {
            handleDeletedTasks([taskId]);
        } else if (action === "continue-evaluate" && result?.continuation_task?.task_id) {
            await loadTaskDetail(result.continuation_task.task_id, { preserveView: true, reopenSocket: false });
            await loadTaskArtifacts();
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
        if (action === "retry") return canRetry(task);
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
            text: "删除后将移除任务记录、树快照和工件文件，且无法恢复。",
            confirmLabel: "删除",
            confirmKind: "danger",
            returnFocus,
            onConfirm: () => performTaskBatchAction(action, eligible),
        });
        return;
    }
    await performTaskBatchAction(action, eligible);
}

async function performTaskBatchAction(action, eligible) {
    if (taskActionRequiresWorker(action) && !taskWorkerControlsAvailable()) {
        showToast({ title: taskActionFailureTitle(action), text: taskWorkerNoticeText(), kind: "warn" });
        void refreshTaskWorkerStatus({ render: S.view === "tasks" });
        return;
    }
    S.taskBusy = true;
    renderTasksIfVisible();
    try {
        const results = await Promise.allSettled(eligible.map((task) => requestTaskAction(task.task_id, action)));
        const succeeded = results
            .map((result, index) => (result.status === "fulfilled" ? eligible[index].task_id : ""))
            .filter(Boolean);
        const failed = results
            .map((result, index) => (result.status === "rejected" ? { taskId: eligible[index].task_id, error: result.reason } : null))
            .filter(Boolean);
        await loadTasks();
        if (action === "delete") {
            handleDeletedTasks(succeeded);
        } else if (S.currentTaskId && succeeded.includes(S.currentTaskId)) {
            await loadTaskDetail(S.currentTaskId, { preserveView: true, reopenSocket: false });
            await loadTaskArtifacts();
        }
        if (failed.length && !succeeded.length) {
            if (failed.some((item) => Number(item?.error?.status || 0) === 503)) {
                void refreshTaskWorkerStatus({ render: S.view === "tasks" });
            }
            showToast({
                title: taskActionFailureTitle(action),
                text: taskActionErrorText(action, failed[0].error),
                kind: "error",
            });
            return;
        }
        if (failed.length) {
            showToast({
                title: action === "delete" ? "删除完成" : `${taskActionText(action)}完成`,
                text: `${succeeded.length} 个任务成功，${failed.length} 个失败`,
                kind: "warn",
            });
            return;
        }
        const successText = action === "delete"
            ? `已删除 ${succeeded.length} 个任务`
            : action === "retry"
                ? `已重试 ${succeeded.length} 个任务`
                : `${succeeded.length} 个任务已更新`;
        showToast({
            title: taskActionSuccessTitle(action),
            text: successText,
            kind: "success",
        });
        if (failed.some((item) => Number(item?.error?.status || 0) === 503)) {
            void refreshTaskWorkerStatus({ render: S.view === "tasks" });
        }
    } finally {
        S.taskBusy = false;
        renderTasksIfVisible();
    }
}

function resetTaskView() {
    if (S.taskWs) {
        S.taskWs.close();
        S.taskWs = null;
    }
    clearTaskDetailSession();
    S.currentTask = null;
    S.taskSummary = null;
    S.rootNode = null;
    S.frontier = [];
    S.recentModelCalls = [];
    S.taskModelCallsPage = 1;
    S.taskModelCallsPageSize = typeof TASK_MODEL_CALLS_PAGE_SIZE === "number" && TASK_MODEL_CALLS_PAGE_SIZE > 0
        ? TASK_MODEL_CALLS_PAGE_SIZE
        : 100;
    S.liveFrameMap = {};
    S.currentNodeDetail = null;
    S.taskNodeDetails = {};
    S.taskNodeDetailRequests = {};
    S.taskNodeLatestContexts = {};
    S.taskNodeLatestContextRequests = {};
    if (typeof resetTaskTreeSnapshotState === "function") resetTaskTreeSnapshotState();
    S.taskNodeBusy = false;
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
    if (U.adOutput) U.adOutput.textContent = "暂无最终输出";
    if (U.adFlow) U.adFlow.innerHTML = '<div class="empty-state task-trace-empty">选择任务树中的节点后，这里会显示执行流程。</div>';
    if (U.adAcceptance) U.adAcceptance.textContent = "暂无验收结果";
    if (U.adRoundSummary) U.adRoundSummary.textContent = "默认显示：最新树";
    if (U.nodeEmpty) U.nodeEmpty.style.display = "block";
    if (U.artifactList) U.artifactList.innerHTML = '<div class="empty-state" style="padding: 10px;">No file changes yet.</div>';
    if (U.artifactContent) U.artifactContent.textContent = "展开后查看节点完整上下文。";
    if (U.nodeContextDisclosure) U.nodeContextDisclosure.open = false;
    renderFlowHeading(0);
    renderArtifactHeading(0);
    syncTaskTreeHeaderState(null);
    refreshTaskDetailScrollRegions();
    if (U.taskTokenButton) U.taskTokenButton.disabled = true;
    if (U.taskTokenSummaryText) U.taskTokenSummaryText.textContent = "任务级 token 消耗会在这里实时刷新。";
    if (U.taskTokenContent) U.taskTokenContent.innerHTML = '<div class="empty-state">请选择一个任务后查看 token 统计。</div>';
    setTaskTokenStatsOpen(false);
    setTaskSelectionEmptyVisible(false);
    hideAgent();
}

function setTaskDetailOpen(open) {
    setDrawerOpen(U.taskDetailBackdrop, U.taskDetailDrawer, open);
}

function setTaskTokenStatsOpen(open) {
    S.taskTokenStatsOpen = !!open;
    setDrawerOpen(U.taskTokenBackdrop, U.taskTokenDrawer, !!open);
    if (open) renderTaskTokenStats();
}

function renderTaskTokenStats() {
    if (!U.taskTokenContent || !U.taskTokenSummaryText) return;
    const summary = taskTokenUsage(S.currentTask, null);
    U.taskTokenSummaryText.textContent = taskTokenSummaryLine(summary);
    if (U.taskTokenButton) U.taskTokenButton.title = taskTokenSummaryLine(summary);
    if (!summary.tracked) {
        U.taskTokenContent.innerHTML = `
            <div class="task-token-topline">
                <div class="task-token-stat"><strong>未统计</strong><span>该任务创建于统计上线前，暂无精确数据。</span></div>
            </div>
        `;
        return;
    }
    const modelRows = Array.isArray(S.taskSummary?.token_usage_by_model)
        ? S.taskSummary.token_usage_by_model.map(normalizeModelTokenUsage).sort((a, b) => {
            const delta = tokenKnownTotal(b) - tokenKnownTotal(a);
            if (delta !== 0) return delta;
            return String(a.model_key || "").localeCompare(String(b.model_key || ""));
        })
        : [];
    const recentModelCalls = Array.isArray(S.recentModelCalls)
        ? S.recentModelCalls.map(normalizeTaskModelCall).sort((a, b) => Number(b.call_index || 0) - Number(a.call_index || 0))
        : [];
    const modelCallPageMeta = paginateTaskModelCalls(recentModelCalls);
    const partialNote = summary.is_partial
        ? '<span class="task-token-badge warn">部分模型未返回 usage</span>'
        : '<span class="task-token-badge success">统计完整</span>';
    const topline = `
        <div class="task-token-topline">
            <div class="task-token-stat"><strong>${esc(formatTokenCount(summary.input_tokens))}</strong><span>总输入</span></div>
            <div class="task-token-stat"><strong>${esc(formatTokenCount(summary.output_tokens))}</strong><span>总输出</span></div>
            <div class="task-token-stat"><strong>${esc(formatTokenCount(summary.cache_hit_tokens))}</strong><span>缓存命中</span></div>
            <div class="task-token-stat"><strong>${esc(formatTokenCount(summary.call_count))}</strong><span>模型调用</span></div>
        </div>
        <div class="task-token-meta">
            ${partialNote}
            <span class="task-token-subtle">有 usage ${esc(formatTokenCount(summary.calls_with_usage))} · 缺失 ${esc(formatTokenCount(summary.calls_without_usage))}</span>
        </div>
    `;
    if (!summary.call_count) {
        U.taskTokenContent.innerHTML = `${topline}<div class="empty-state task-token-empty">尚未发生模型调用。</div>`;
        return;
    }
    const rowsMarkup = modelRows.length
        ? modelRows.map((item) => {
            const subtitleParts = [item.provider_id, item.provider_model].filter(Boolean);
            const badges = [];
            if (item.is_partial) badges.push('<span class="task-token-badge warn">部分缺失</span>');
            if (!item.calls_without_usage) badges.push('<span class="task-token-badge success">完整</span>');
            return `
                <div class="task-token-model-item">
                    <div class="task-token-model-head">
                        <div>
                            <h3>${esc(item.model_key || "未命名模型")}</h3>
                            <p>${esc(subtitleParts.join(" · ") || "模型标识未提供")}</p>
                        </div>
                        <div class="task-token-model-badges">${badges.join("")}</div>
                    </div>
                    <div class="task-token-model-stats">
                        <span>输入 ${esc(formatTokenCount(item.input_tokens))}</span>
                        <span>输出 ${esc(formatTokenCount(item.output_tokens))}</span>
                        <span>缓存命中 ${esc(formatTokenCount(item.cache_hit_tokens))}</span>
                    </div>
                    <div class="task-token-model-meta">
                        调用 ${esc(formatTokenCount(item.call_count))} · 有 usage ${esc(formatTokenCount(item.calls_with_usage))} · 缺失 ${esc(formatTokenCount(item.calls_without_usage))}
                    </div>
                </div>
            `;
        }).join("")
        : '<div class="empty-state task-token-empty">当前只有任务级统计，尚无按模型明细。</div>';
    const recentCallMarkup = recentModelCalls.length
        ? `
            <div class="task-token-call-card">
                <div class="task-token-call-head">
                    <div>
                        <h3>模型调用明细</h3>
                        <p>任务开始以来共 ${esc(formatTokenCount(modelCallPageMeta.total))} 次调用 · 每页 ${esc(formatTokenCount(modelCallPageMeta.pageSize))} 条</p>
                    </div>
                    <div class="task-token-call-page-info">${esc(taskModelCallPageSummary(modelCallPageMeta))}</div>
                </div>
                <div class="task-token-call-table-wrap">
                    <table class="task-token-call-table">
                        <thead>
                            <tr>
                                <th>调用序号</th>
                                <th>预处理字符数</th>
                                <th>消息数</th>
                                <th>新增输入 Token</th>
                                <th>新增缓存命中</th>
                                <th>命中率</th>
                                <th>工具调用数</th>
                                <th>模型</th>
                            </tr>
                        </thead>
                        <tbody>
                            ${modelCallPageMeta.items.map((item) => {
                                const modelNames = item.delta_usage_by_model.length
                                    ? item.delta_usage_by_model.map((row) => row.model_key || row.provider_model || row.provider_id || "").filter(Boolean).join(", ")
                                    : "未提供";
                                return `
                                    <tr>
                                        <td>${esc(formatTokenCount(item.call_index))}</td>
                                        <td>${esc(formatTokenCount(item.prepared_message_chars))}</td>
                                        <td>${esc(formatTokenCount(item.prepared_message_count))}</td>
                                        <td>${esc(formatTokenCount(item.delta_usage.input_tokens))}</td>
                                        <td>${esc(formatTokenCount(item.delta_usage.cache_hit_tokens))}</td>
                                        <td>${esc((modelCallHitRate(item) * 100).toFixed(1))}%</td>
                                        <td>${esc(formatTokenCount(item.response_tool_call_count))}</td>
                                        <td>${esc(modelNames)}</td>
                                    </tr>
                                `;
                            }).join("")}
                        </tbody>
                    </table>
                </div>
                <div class="task-token-call-footer">
                    <div class="task-token-call-page-info">${esc(taskModelCallPageSummary(modelCallPageMeta))}</div>
                    <div class="task-token-call-actions">
                        <button class="toolbar-btn ghost" type="button" data-task-model-call-page="prev" ${modelCallPageMeta.currentPage <= 1 ? "disabled" : ""}>上一页</button>
                        <button class="toolbar-btn ghost" type="button" data-task-model-call-page="next" ${modelCallPageMeta.currentPage >= modelCallPageMeta.totalPages ? "disabled" : ""}>下一页</button>
                    </div>
                </div>
            </div>
        `
        : '<div class="empty-state task-token-empty">暂无逐次调用明细。</div>';
    U.taskTokenContent.innerHTML = `
        ${topline}
        <div class="task-token-model-list">${rowsMarkup}</div>
        ${recentCallMarkup}
    `;
}

function taskModelCallsPageSize() {
    const fallback = typeof TASK_MODEL_CALLS_PAGE_SIZE === "number" && TASK_MODEL_CALLS_PAGE_SIZE > 0
        ? TASK_MODEL_CALLS_PAGE_SIZE
        : 100;
    const next = Number(S.taskModelCallsPageSize || 0);
    return Number.isInteger(next) && next > 0 ? next : fallback;
}

function paginateTaskModelCalls(items) {
    const total = Array.isArray(items) ? items.length : 0;
    const pageSize = taskModelCallsPageSize();
    const totalPages = Math.max(1, Math.ceil(total / pageSize));
    const requestedPage = Number(S.taskModelCallsPage || 1);
    const currentPage = Number.isFinite(requestedPage)
        ? Math.min(Math.max(1, Math.floor(requestedPage)), totalPages)
        : 1;
    const startIndex = total ? ((currentPage - 1) * pageSize) + 1 : 0;
    const endIndex = total ? Math.min(currentPage * pageSize, total) : 0;
    const startOffset = total ? startIndex - 1 : 0;
    S.taskModelCallsPage = currentPage;
    S.taskModelCallsPageSize = pageSize;
    return {
        total,
        pageSize,
        totalPages,
        currentPage,
        startIndex,
        endIndex,
        items: total ? items.slice(startOffset, startOffset + pageSize) : [],
    };
}

function taskModelCallPageSummary(meta) {
    if (!meta.total) return "第 1/1 页 · 共 0 条";
    return `第 ${formatTokenCount(meta.currentPage)}/${formatTokenCount(meta.totalPages)} 页 · 显示 ${formatTokenCount(meta.startIndex)}-${formatTokenCount(meta.endIndex)} / 共 ${formatTokenCount(meta.total)} 条`;
}

function setTaskModelCallsPage(page) {
    const next = Number(page);
    S.taskModelCallsPage = Number.isFinite(next) ? Math.max(1, Math.floor(next)) : 1;
    renderTaskTokenStats();
}

async function loadTaskDetail(taskId, { preserveView = false, reopenSocket = true } = {}) {
    if (!taskId) return;
    if (!preserveView) {
        S.currentTaskId = taskId;
        switchView("task-details");
        resetTaskView();
    }
    const payload = await ApiClient.getTask(taskId, true);
    applyTaskPayload(payload);
    if (reopenSocket) {
        if (S.taskWs) {
            S.taskWs.close();
            S.taskWs = null;
        }
        S.taskWs = new WebSocket(ApiClient.getTaskWsUrl(taskId));
        S.taskWs.onmessage = (ev) => handleTaskEvent(JSON.parse(ev.data));
    }
    await loadTaskTreeSnapshot(taskId);
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
        U.tree.innerHTML = `<div class="empty-state error">Failed to open task: ${esc(e.message)}</div>`;
        showToast({ title: "Task open failed", text: e.message || "Unknown error", kind: "error" });
    }
}

function handleTaskEvent(payload) {
    if (payload.type === "task.summary.patch" && payload.data?.task) {
        S.currentTask = { ...(S.currentTask || {}), ...payload.data.task };
        S.taskSummary = { ...(S.taskSummary || {}), ...payload.data.task };
        patchTaskListItem(payload.data.task);
        renderTaskDetailHeader();
        renderTaskTokenStats();
        return;
    }
    if (payload.type === "task.model.call") {
        const nextCall = normalizeTaskModelCall(payload.data || {});
        const existing = Array.isArray(S.recentModelCalls) ? S.recentModelCalls : [];
        const withoutSame = existing.filter((item) => Number(item?.call_index || 0) !== Number(nextCall.call_index || 0));
        const merged = [...withoutSame, nextCall]
            .sort((a, b) => Number(a?.call_index || 0) - Number(b?.call_index || 0));
        S.recentModelCalls = merged;
        renderTaskTokenStats();
        return;
    }
    if (payload.type === "task.live.patch") {
        const runtimeSummary = payload.data?.runtime_summary || {};
        const frames = Array.isArray(runtimeSummary?.frames) ? runtimeSummary.frames : [];
        const activeNodeIds = Array.isArray(runtimeSummary?.active_node_ids) ? runtimeSummary.active_node_ids : [];
        const runnableNodeIds = Array.isArray(runtimeSummary?.runnable_node_ids) ? runtimeSummary.runnable_node_ids : [];
        const waitingNodeIds = Array.isArray(runtimeSummary?.waiting_node_ids) ? runtimeSummary.waiting_node_ids : [];
        const hasTreeContext = !!String(S.treeRootNodeId || "").trim();
        S.frontier = frames;
        S.liveFrameMap = indexTaskLiveFrames(frames);
        S.taskSummary = {
            ...(S.taskSummary || {}),
            active_node_count: hasTreeContext
                ? normalizeInt(S.taskSummary?.active_node_count, 0)
                : activeNodeIds.length,
            runnable_node_count: runnableNodeIds.length,
            waiting_node_count: waitingNodeIds.length,
        };
        if (String(S.treeRootNodeId || "").trim()) {
            const candidateNodeIds = [...new Set([...activeNodeIds, ...runnableNodeIds, ...waitingNodeIds]
                .map((item) => String(item || "").trim())
                .filter(Boolean))];
            candidateNodeIds
                .filter((item) => !treeSnapshotNode(item))
                .slice(0, 3)
                .forEach((item) => { void reconcileTaskTreeForNode(item); });
            if (typeof scheduleRenderedTreeNodeStatusRefresh === "function") scheduleRenderedTreeNodeStatusRefresh();
        }
        return;
    }
    if (payload.type === "task.node.patch") {
        const nextNode = payload.data?.node && typeof payload.data.node === "object" ? payload.data.node : {};
        const nodeId = String(nextNode?.node_id || "").trim();
        if (nodeId) {
            const parentNodeId = String(nextNode?.parent_node_id || "").trim();
            const rootNodeId = String(S.rootNode?.node_id || "").trim();
            if (rootNodeId && rootNodeId === nodeId) {
                S.rootNode = { ...(S.rootNode || {}), ...nextNode };
            }
            if (S.taskNodeDetails[nodeId]) {
                S.taskNodeDetails = {
                    ...(S.taskNodeDetails || {}),
                    [nodeId]: { ...(S.taskNodeDetails[nodeId] || {}), ...nextNode },
                };
            }
            if (String(S.treeRootNodeId || "").trim()) {
                const existingTreeNode = treeSnapshotNode(nodeId);
                if (existingTreeNode) {
                    const previousFingerprint = String(existingTreeNode?.children_fingerprint || "").trim();
                    S.treeNodesById = {
                        ...(S.treeNodesById || {}),
                        [nodeId]: normalizeTaskTreeSnapshotNode({
                            ...existingTreeNode,
                            ...nextNode,
                        }, existingTreeNode),
                    };
                    if (typeof refreshRenderedTreeNodeStatus === "function") refreshRenderedTreeNodeStatus(nodeId);
                    if (previousFingerprint !== String(nextNode?.children_fingerprint || "").trim()) {
                        scheduleTaskTreeBranchSync(nodeId);
                    }
                } else if (parentNodeId) {
                    scheduleTaskTreeBranchSync(parentNodeId);
                }
            }
            if (String(S.selectedNodeId || "") === nodeId) {
                const currentViewState = captureTaskDetailViewState();
                stashTaskDetailViewState({ nodeId, viewState: currentViewState });
                S.pendingTaskDetailRestore = { nodeId, viewState: currentViewState };
                const selected = findTreeNode(S.treeView, nodeId) || { node_id: nodeId, title: nodeId, state: "in_progress" };
                void showAgent(selected, { preserveViewState: true, forceRefresh: true });
            } else {
                const nextDetails = { ...(S.taskNodeDetails || {}) };
                delete nextDetails[nodeId];
                S.taskNodeDetails = nextDetails;
            }
        }
        return;
    }
    if (payload.type === "task.artifact.added" || payload.type === "task.artifact.applied" || payload.type === "artifact.applied") {
        void loadTaskArtifacts();
        return;
    }
    if (payload.type === "task.deleted") {
        removeTaskListItem(payload.data?.task_id || payload.task_id || "");
        return;
    }
    if (payload.type === "task.terminal" && payload.data?.task) {
        S.currentTask = { ...(S.currentTask || {}), ...payload.data.task };
        S.taskSummary = { ...(S.taskSummary || {}), ...payload.data.task };
        renderTaskTokenStats();
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

function patchTaskListItem(task) {
    const change = upsertTaskState(task);
    const taskId = String(change?.taskId || "").trim();
    if (!taskId) return;
    S.lastTaskSummaryPatchAt = new Date().toISOString();
    S.taskMetricAnimationTaskIds = new Set([taskId]);
    syncTaskSelection();
    if (!taskListViewVisible()) {
        noteTaskHallHiddenDefer();
        return;
    }
    const visibleIds = new Set(Array.isArray(S.visibleTaskIds) ? S.visibleTaskIds : []);
    if (change.added || change.orderChanged || !visibleIds.has(taskId) || !taskCardPatchEligible(change.previousTask, change.nextTask)) {
        renderTasks();
        return;
    }
    queueTaskCardPatch(taskId);
}

function removeTaskListItem(taskId) {
    const key = String(taskId || "").trim();
    if (!key) return;
    if (!removeTaskState(key)) return;
    handleDeletedTasks([key]);
    syncTaskSelection();
    if (!taskListViewVisible()) {
        noteTaskHallHiddenDefer();
        return;
    }
    renderTasks();
}

function closeTasksWs() {
    const socket = S.tasksWs;
    S.tasksWs = null;
    if (!socket) return;
    socket.onclose = null;
    socket.close();
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

function initTasksWs() {
    if (S.tasksWs && S.tasksWs.readyState <= 1) return;
    closeTasksWs();
    const socket = new WebSocket(ApiClient.getTasksWsUrl(taskSessionQueryValue()));
    S.tasksWs = socket;
    socket.onopen = () => {
        void refreshTaskWorkerStatus({ render: !!U.viewTasks?.classList.contains("active") });
        if (S.taskListReconnectNeedsReconcile) {
            S.taskListReconnectNeedsReconcile = false;
            void loadTasks();
        }
    };
    socket.onmessage = (event) => {
        const payload = JSON.parse(event.data || "{}");
        if (payload.type === "task.worker.status") {
            applyTaskWorkerStatus(payload.data || {});
            return;
        }
        if (payload.type === "task.summary.patch") {
            patchTaskListItem(payload.data?.task || {});
            return;
        }
        if (payload.type === "task.token.patch") {
            S.lastTaskTokenPatchAt = new Date().toISOString();
            patchTaskListItem({
                task_id: payload.data?.task_id || payload.task_id || "",
                updated_at: payload.data?.updated_at || "",
                token_usage: payload.data?.token_usage || {},
            });
            return;
        }
        if (payload.type === "task.deleted") {
            removeTaskListItem(payload.data?.task_id || "");
        }
    };
    socket.onclose = () => {
        if (!U.viewTasks?.classList.contains("active")) return;
        S.taskListReconnectNeedsReconcile = true;
        void refreshTaskWorkerStatus({ render: !!U.viewTasks?.classList.contains("active") });
        window.setTimeout(() => {
            if (U.viewTasks?.classList.contains("active")) initTasksWs();
        }, 1000);
    };
}







