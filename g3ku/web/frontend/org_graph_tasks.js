// Task list, task detail loading, and task event orchestration extracted from org_graph_app.js.
// Loaded after org_graph_app.js and before org_graph_llm.js.


function taskStatusLabel(task) {
    return ({ in_progress: "Running", success: "Done", failed: "Failed", blocked: "Paused", unknown: "Unknown" })[taskStatusKey(task)] || "Unknown";
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
    syncTaskPagination(meta);
    const signature = taskGridRenderSignature(meta);
    if (signature === S.taskGridSignature) {
        S.taskMetricAnimationTaskIds?.clear?.();
        updateTaskToolbar();
        return;
    }
    S.taskGridSignature = signature;
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
            { key: "cache_hit_tokens", label: "缓存命中Token", value: tokenUsage.tracked ? tokenUsage.cache_hit_tokens : null },
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
            return `<div class="pc-metric${isIncreasing ? " is-increasing" : ""}"><span class="pc-metric-label">${item.label}</span><strong class="pc-metric-value${isIncreasing ? " is-increasing" : ""}">${esc(hasValue ? formatTokenCount(item.value) : "--")}</strong></div>`;
        }).join("");
        const cardActions = taskCardActions(task);
        const el = document.createElement("div");
        el.className = `project-card${selected ? " is-selected" : ""}${S.multiSelectMode ? " is-multi-mode" : ""}`;
        el.innerHTML = `
            <div class="pc-topbar">
                <div class="pc-topbar-left">
                    <label class="project-select-toggle${S.multiSelectMode ? " is-visible" : ""}"><input type="checkbox" class="project-select-checkbox" ${selected ? "checked" : ""} ${S.taskBusy ? "disabled" : ""}><span>Select</span></label>
                    <div class="pc-topbar-meta">
                        <span class="status-badge" data-status="${esc(statusKey)}">${esc(taskStatusLabel(task))}</span>
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
            <div class="pc-header"><div class="pc-header-left"><h3 class="pc-title" title="${esc(task.title || taskId)}">${esc(task.title || taskId)}</h3></div></div>
            <div class="pc-created-at"><span class="pc-field-label">创建时间</span><span class="pc-field-value">${esc(taskCreatedAtText(task))}</span></div>
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
    S.taskMetricAnimationTaskIds?.clear?.();
    updateTaskToolbar();
    icons();
}

async function loadTasks() {
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
    if (canDelete(task)) actions.push("delete");
    return actions.map((action) => ({ action, label: taskActionText(action), tone: taskActionTone(action) }));
}

function primaryTaskAction(task) {
    if (taskWorkerControlsAvailable() && canPause(task)) return { action: "pause", label: "暂停", tone: "warn" };
    if (taskWorkerControlsAvailable() && canResume(task)) return { action: "resume", label: "开始", tone: "success" };
    if (taskWorkerControlsAvailable() && canRetry(task)) return { action: "retry", label: "重试", tone: "success" };
    return null;
}

function taskActionText(action) {
    return ({ pause: "暂停", resume: "开始", retry: "重试", delete: "删除" }[action] || "操作");
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
    if (action === "delete") return ApiClient.deleteTask(taskId);
    throw new Error(`Unsupported task action: ${action}`);
}

async function runTaskAction(taskId, action, { returnFocus = null } = {}) {
    if (!taskId || !action) return;
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
    renderTasks();
    try {
        const result = await requestTaskAction(taskId, action);
        const successText = action === "retry" ? (result?.task_id || taskId) : taskId;
        showToast({ title: taskActionSuccessTitle(action), text: successText, kind: "success" });
        await loadTasks();
        if (action === "delete") {
            handleDeletedTasks([taskId]);
        } else if (action !== "retry" && S.currentTaskId === taskId) {
            await loadTaskDetail(taskId, { preserveView: true, reopenSocket: false });
            await loadTaskArtifacts();
        }
    } catch (e) {
        if (Number(e?.status || 0) === 503) void refreshTaskWorkerStatus({ render: S.view === "tasks" });
        showToast({ title: taskActionFailureTitle(action), text: taskActionErrorText(action, e), kind: "error" });
    } finally {
        S.taskBusy = false;
        renderTasks();
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
    renderTasks();
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
        } else if (action !== "retry" && S.currentTaskId && succeeded.includes(S.currentTaskId)) {
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
                ? `已创建 ${succeeded.length} 个重试任务`
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
        renderTasks();
    }
}

function resetTaskView() {
    if (S.taskWs) {
        S.taskWs.close();
        S.taskWs = null;
    }
    clearTaskDetailSession();
    S.currentTask = null;
    S.currentTaskProgress = null;
    S.currentTaskTreeRoot = null;
    S.currentTaskRuntimeSummary = null;
    S.currentNodeDetail = null;
    S.taskNodeDetails = {};
    S.taskNodeDetailRequests = {};
    S.taskNodeBusy = false;
    S.taskArtifacts = [];
    S.selectedArtifactId = "";
    S.artifactContent = "";
    S.tree = null;
    S.treeView = null;
    S.treeRoundSelectionsByNodeId = {};
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
    if (U.artifactList) U.artifactList.innerHTML = '<div class="empty-state" style="padding: 10px;">No artifacts yet.</div>';
    if (U.artifactContent) U.artifactContent.textContent = "Select an artifact to view details.";
    if (U.artifactApply) U.artifactApply.hidden = true;
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
    const summary = taskTokenUsage(S.currentTask, S.currentTaskProgress);
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
    const modelRows = Array.isArray(S.currentTaskProgress?.token_usage_by_model)
        ? S.currentTaskProgress.token_usage_by_model.map(normalizeModelTokenUsage).sort((a, b) => {
            const delta = tokenKnownTotal(b) - tokenKnownTotal(a);
            if (delta !== 0) return delta;
            return String(a.model_key || "").localeCompare(String(b.model_key || ""));
        })
        : [];
    const recentModelCalls = Array.isArray(S.currentTaskProgress?.model_calls)
        ? S.currentTaskProgress.model_calls.map(normalizeTaskModelCall).sort((a, b) => Number(b.call_index || 0) - Number(a.call_index || 0))
        : [];
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
                    <h3>Recent model calls</h3>
                    <p>Showing the latest ${esc(formatTokenCount(recentModelCalls.length))} calls</p>
                </div>
                <div class="task-token-call-table-wrap">
                    <table class="task-token-call-table">
                        <thead>
                            <tr>
                                <th>#</th>
                                <th>prepared chars</th>
                                <th>msgs</th>
                                <th>delta input</th>
                                <th>delta cache</th>
                                <th>hit %</th>
                                <th>tool calls</th>
                                <th>models</th>
                            </tr>
                        </thead>
                        <tbody>
                            ${recentModelCalls.map((item) => {
                                const modelNames = item.delta_usage_by_model.length
                                    ? item.delta_usage_by_model.map((row) => row.model_key || row.provider_model || row.provider_id || "").filter(Boolean).join(", ")
                                    : "n/a";
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
            </div>
        `
        : '<div class="empty-state task-token-empty">No per-call telemetry yet.</div>';
    U.taskTokenContent.innerHTML = `
        ${topline}
        <div class="task-token-model-list">${rowsMarkup}</div>
        ${recentCallMarkup}
    `;
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
    return payload;
}

async function restoreTaskDetailSession() {
    const snapshot = readTaskDetailSessionSnapshot();
    if (!snapshot?.currentTaskId) return false;
    try {
        await loadTaskDetail(snapshot.currentTaskId);
        S.taskDetailViewStates = snapshot.nodeViewStates || {};
        S.treeRoundSelectionsByNodeId = pruneTreeRoundSelections(S.tree, snapshot.treeRoundSelectionsByNodeId);
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
    if (payload.type === "snapshot.task" || payload.type === "task.snapshot") {
        applyTaskPayload(payload.data || {});
        return;
    }
    if (payload.type === "task.summary.patch" && payload.data?.task) {
        S.currentTask = { ...(S.currentTask || {}), ...payload.data.task };
        patchTaskListItem(payload.data.task);
        renderTaskDetailHeader();
        renderTaskTokenStats();
        return;
    }
    if (payload.type === "task.model.call") {
        const nextCall = normalizeTaskModelCall(payload.data || {});
        const existing = Array.isArray(S.currentTaskProgress?.model_calls) ? S.currentTaskProgress.model_calls : [];
        const withoutSame = existing.filter((item) => Number(item?.call_index || 0) !== Number(nextCall.call_index || 0));
        const merged = [...withoutSame, nextCall]
            .sort((a, b) => Number(a?.call_index || 0) - Number(b?.call_index || 0))
            .slice(-50);
        S.currentTaskProgress = { ...(S.currentTaskProgress || {}), model_calls: merged };
        renderTaskTokenStats();
        return;
    }
    if (payload.type === "task.live.patch") {
        S.currentTaskRuntimeSummary = payload.data?.runtime_summary || null;
        S.currentTaskProgress = { ...(S.currentTaskProgress || {}), live_state: S.currentTaskRuntimeSummary };
        if (S.tree) renderTree();
        return;
    }
    if (payload.type === "task.node.patch") {
        const nodeId = String(payload.data?.node?.node_id || "").trim();
        if (nodeId) {
            if (String(S.selectedNodeId || "") === nodeId) {
                const currentViewState = captureTaskDetailViewState();
                stashTaskDetailViewState({ nodeId, viewState: currentViewState });
                S.pendingTaskDetailRestore = { nodeId, viewState: currentViewState };
                const selected = findTreeNode(S.treeView || S.tree, nodeId) || { node_id: nodeId, title: nodeId, state: "in_progress" };
                void showAgent(selected, { preserveViewState: true, forceRefresh: true });
            } else {
                delete S.taskNodeDetails[nodeId];
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
        renderTaskTokenStats();
    }
}

function isAbortLike(error) {
    return String(error?.name || "").trim() === "AbortError";
}

function applyTaskListResponse(payload = {}) {
    const items = Array.isArray(payload?.items) ? payload.items : [];
    S.tasks = items;
    applyTaskWorkerStatus(payload || {}, { render: false });
    syncTaskSelection();
    renderTaskSessionScope();
    renderTasks();
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
    const taskId = String(task?.task_id || "").trim();
    if (!taskId) return;
    const next = [...(S.tasks || [])];
    const index = next.findIndex((item) => String(item?.task_id || "").trim() === taskId);
    if (index >= 0) next[index] = { ...next[index], ...task };
    else next.unshift(task);
    S.tasks = next;
    S.taskMetricAnimationTaskIds = new Set([taskId]);
    syncTaskSelection();
    renderTasks();
}

function removeTaskListItem(taskId) {
    const key = String(taskId || "").trim();
    if (!key) return;
    S.tasks = (S.tasks || []).filter((item) => String(item?.task_id || "").trim() !== key);
    handleDeletedTasks([key]);
    syncTaskSelection();
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
    if (S.taskWorkerStatusPollId) return;
    void refreshTaskWorkerStatus({ render: S.view === "tasks" });
    S.taskWorkerStatusPollId = window.setInterval(() => {
        void refreshTaskWorkerStatus({ render: S.view === "tasks" });
    }, 10000);
}

function stopTaskWorkerStatusPolling() {
    if (!S.taskWorkerStatusPollId) return;
    window.clearInterval(S.taskWorkerStatusPollId);
    S.taskWorkerStatusPollId = null;
}

function initTasksWs() {
    if (S.tasksWs && S.tasksWs.readyState <= 1) return;
    closeTasksWs();
    const socket = new WebSocket(ApiClient.getTasksWsUrl(taskSessionQueryValue()));
    S.tasksWs = socket;
    socket.onopen = () => {
        void refreshTaskWorkerStatus({ render: S.view === "tasks" });
    };
    socket.onmessage = (event) => {
        const payload = JSON.parse(event.data || "{}");
        if (payload.type === "task.list.snapshot") {
            applyTaskListResponse(payload.data || {});
            return;
        }
        if (payload.type === "task.worker.status") {
            applyTaskWorkerStatus(payload.data || {});
            return;
        }
        if (payload.type === "task.summary.patch") {
            patchTaskListItem(payload.data?.task || {});
            return;
        }
        if (payload.type === "task.deleted") {
            removeTaskListItem(payload.data?.task_id || "");
        }
    };
    socket.onclose = () => {
        if (S.view !== "tasks") return;
        void refreshTaskWorkerStatus({ render: S.view === "tasks" });
        window.setTimeout(() => {
            if (S.view === "tasks") initTasksWs();
        }, 1000);
    };
}







