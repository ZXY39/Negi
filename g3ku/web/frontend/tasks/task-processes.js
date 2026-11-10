// 任务详情页右上角的后台进程气泡：只依赖 GET/POST /tasks/{id}/processes。
// 本文件在 org_graph_app.js 之前加载，顶层只允许声明函数/常量，不得读写 S/U/document。

const TASK_PROCESSES_RECONCILE_MS = 10000;

const TaskProcessesState = {
    taskId: "",
    items: [],
    open: false,
    reconcileTimerId: 0,
    elapsedTimerId: 0,
    requestToken: 0,
    stopping: new Set(),
    lifecycleBound: false,
};

function taskProcessesEl(id) {
    return document.getElementById(String(id || ""));
}

function taskProcessesFormatSeconds(totalSeconds) {
    const seconds = Math.max(0, Math.floor(Number(totalSeconds) || 0));
    const hours = Math.floor(seconds / 3600);
    const minutes = Math.floor((seconds % 3600) / 60);
    const remain = seconds % 60;
    const pad = (value) => String(value).padStart(2, "0");
    return hours > 0 ? `${hours}:${pad(minutes)}:${pad(remain)}` : `${pad(minutes)}:${pad(remain)}`;
}

function taskProcessesElapsedText(startedAt) {
    const startedMs = Date.parse(String(startedAt || ""));
    const seconds = Number.isFinite(startedMs) ? Math.max(0, Math.floor((Date.now() - startedMs) / 1000)) : 0;
    return taskProcessesFormatSeconds(seconds);
}

function taskProcessesCurrentTaskId() {
    return String(typeof S !== "undefined" ? S.currentTaskId || "" : "").trim();
}

function taskProcessesVisible() {
    if (!taskProcessesCurrentTaskId()) return false;
    if (typeof isTaskDetailsViewActive === "function" && !isTaskDetailsViewActive()) return false;
    return true;
}

function taskProcessesTruncate(text, limit = 60) {
    const value = String(text || "").trim();
    return value.length > limit ? `${value.slice(0, limit - 1)}…` : value;
}

function taskProcessesRow(item) {
    const row = document.createElement("div");
    row.className = "task-processes-row";
    row.setAttribute("role", "listitem");
    const pid = Number(item?.pid) || 0;
    row.dataset.taskProcessPid = String(pid);

    const main = document.createElement("span");
    main.className = "task-processes-main";
    const purpose = String(item?.purpose || "").trim();
    const cmd = String(item?.cmd || "").trim();
    main.textContent = taskProcessesTruncate(purpose || cmd || `pid ${pid}`, 60);
    main.title = [purpose, cmd].filter(Boolean).join("\n");

    const nodeId = String(item?.node_id || "").trim();
    const node = document.createElement("button");
    node.type = "button";
    node.className = "task-processes-node";
    node.dataset.taskProcessNode = nodeId;
    node.textContent = nodeId ? taskProcessesTruncate(nodeId, 22) : "未归属";
    const nodeGoal = String(item?.node_goal || "").trim();
    node.title = nodeId ? `定位到节点${nodeGoal ? `：${nodeGoal}` : ` ${nodeId}`}` : "该进程没有节点归属";
    node.disabled = !nodeId;
    if (!nodeId) {
        node.title = "该进程没有节点归属";
    }

    const elapsed = document.createElement("span");
    elapsed.className = "task-processes-elapsed";
    elapsed.dataset.taskProcessStarted = String(item?.started_at || "");
    elapsed.textContent = taskProcessesElapsedText(item?.started_at);

    const action = document.createElement("button");
    action.type = "button";
    action.className = "task-processes-stop";
    action.dataset.taskProcessStop = String(pid);
    const stopping = TaskProcessesState.stopping.has(String(pid));
    action.textContent = stopping ? "停止中" : "停止";
    action.disabled = stopping;
    if (stopping) action.setAttribute("aria-busy", "true");

    row.appendChild(main);
    row.appendChild(node);
    row.appendChild(elapsed);
    row.appendChild(action);
    return row;
}

function taskProcessesRenderRows() {
    const list = taskProcessesEl("task-processes-list");
    if (!list) return;
    while (list.firstChild) list.removeChild(list.firstChild);
    TaskProcessesState.items.forEach((item) => list.appendChild(taskProcessesRow(item)));
}

function taskProcessesRender() {
    const root = taskProcessesEl("task-processes");
    const button = taskProcessesEl("task-processes-btn");
    const count = taskProcessesEl("task-processes-count");
    const items = Array.isArray(TaskProcessesState.items) ? TaskProcessesState.items : [];
    if (count) count.textContent = String(items.length);
    if (!items.length) {
        taskProcessesSetOpen(false);
        taskProcessesStopElapsedTimer();
        if (root) root.hidden = true;
        if (button) button.hidden = true;
        return;
    }
    if (root) root.hidden = false;
    if (button) button.hidden = false;
    taskProcessesRenderRows();
    if (TaskProcessesState.open) taskProcessesStartElapsedTimer();
}

function taskProcessesSetOpen(open) {
    const popover = taskProcessesEl("task-processes-popover");
    const toggle = taskProcessesEl("task-processes-btn");
    const next = !!open && TaskProcessesState.items.length > 0;
    TaskProcessesState.open = next;
    if (popover) popover.hidden = !next;
    if (toggle) toggle.setAttribute("aria-expanded", next ? "true" : "false");
    if (next) {
        taskProcessesStartElapsedTimer();
    } else {
        taskProcessesStopElapsedTimer();
    }
}

function taskProcessesToggle() {
    taskProcessesSetOpen(!TaskProcessesState.open);
}

function taskProcessesStartElapsedTimer() {
    if (TaskProcessesState.elapsedTimerId) return;
    TaskProcessesState.elapsedTimerId = window.setInterval(() => {
        if (!TaskProcessesState.open) return;
        const list = taskProcessesEl("task-processes-list");
        if (!list) return;
        Array.from(list.querySelectorAll("[data-task-process-started]")).forEach((el) => {
            el.textContent = taskProcessesElapsedText(el.dataset?.taskProcessStarted);
        });
    }, 1000);
}

function taskProcessesStopElapsedTimer() {
    if (!TaskProcessesState.elapsedTimerId) return;
    window.clearInterval(TaskProcessesState.elapsedTimerId);
    TaskProcessesState.elapsedTimerId = 0;
}

async function taskProcessesStop(pid) {
    const taskId = TaskProcessesState.taskId || taskProcessesCurrentTaskId();
    const normalizedPid = Number(pid) || 0;
    if (!taskId || !normalizedPid) return;
    TaskProcessesState.stopping.add(String(normalizedPid));
    taskProcessesRenderRows();
    try {
        await ApiClient.stopTaskProcess(taskId, normalizedPid);
        if (typeof showToast === "function") {
            showToast({
                title: "已停止进程",
                text: `pid ${normalizedPid} 已终止。`,
                kind: "info",
            });
        }
    } catch (error) {
        const code = typeof ApiClient.getErrorCode === "function"
            ? String(ApiClient.getErrorCode(error) || "")
            : String(error?.code || "");
        const status = Number(error?.status);
        if (code === "process_not_found" || status === 404 || status === 409) {
            if (typeof showToast === "function") {
                showToast({ title: "进程已结束", text: "该进程已经不在运行。", kind: "info" });
            }
        } else if (typeof showToast === "function") {
            showToast({ title: "停止失败", text: "未能停止该后台进程，请重试。", kind: "error" });
        }
    } finally {
        TaskProcessesState.stopping.delete(String(normalizedPid));
        await taskProcessesRefresh({ quiet: true });
    }
}

function taskProcessesClose() {
    taskProcessesSetOpen(false);
}

function taskProcessesJumpToNode(nodeId) {
    const key = String(nodeId || "").trim();
    if (!key) return;
    taskProcessesClose();
    if (typeof jumpTaskTreeSearchToNode === "function") {
        try {
            void Promise.resolve(jumpTaskTreeSearchToNode(key)).catch(() => null);
        } catch {
            // 定位函数的同步异常不影响列表本身。
        }
        return;
    }
    if (typeof showToast === "function") {
        showToast({ title: "无法定位节点", text: "任务树定位入口不可用。", kind: "warn" });
    }
}

async function taskProcessesRefresh({ quiet = false } = {}) {
    const taskId = taskProcessesCurrentTaskId();
    if (!taskId || !taskProcessesVisible()) {
        TaskProcessesState.items = [];
        TaskProcessesState.taskId = "";
        taskProcessesRender();
        return;
    }
    if (TaskProcessesState.taskId && TaskProcessesState.taskId !== taskId) {
        TaskProcessesState.items = [];
        TaskProcessesState.stopping.clear();
        taskProcessesSetOpen(false);
    }
    TaskProcessesState.taskId = taskId;
    const token = ++TaskProcessesState.requestToken;
    try {
        const data = await ApiClient.getTaskProcesses(taskId);
        if (token !== TaskProcessesState.requestToken) return;
        if (taskProcessesCurrentTaskId() !== taskId) return;
        TaskProcessesState.items = Array.isArray(data?.items) ? data.items : [];
        taskProcessesRender();
    } catch (error) {
        if (token !== TaskProcessesState.requestToken) return;
        TaskProcessesState.items = [];
        taskProcessesRender();
        if (!quiet && typeof showToast === "function") {
            showToast({ title: "进程列表加载失败", text: "请稍后重试。", kind: "warn" });
        }
    }
}

function bindTaskProcesses() {
    const root = taskProcessesEl("task-processes");
    const toggle = taskProcessesEl("task-processes-btn");
    const list = taskProcessesEl("task-processes-list");
    if (toggle && toggle.dataset.taskProcessesBound !== "true") {
        toggle.dataset.taskProcessesBound = "true";
        toggle.addEventListener("click", (event) => {
            event.stopPropagation();
            taskProcessesToggle();
        });
    }
    if (list && list.dataset.taskProcessesBound !== "true") {
        list.dataset.taskProcessesBound = "true";
        list.addEventListener("click", (event) => {
            const target = event.target;
            const stop = target?.closest?.("[data-task-process-stop]");
            if (stop) {
                event.stopPropagation();
                void taskProcessesStop(stop.dataset.taskProcessStop);
                return;
            }
            const node = target?.closest?.("[data-task-process-node]");
            if (node) {
                event.stopPropagation();
                taskProcessesJumpToNode(node.dataset.taskProcessNode);
            }
        });
    }
    if (!TaskProcessesState.lifecycleBound) {
        TaskProcessesState.lifecycleBound = true;
        document.addEventListener("click", (event) => {
            if (!TaskProcessesState.open) return;
            if (root && event.target && root.contains?.(event.target)) return;
            taskProcessesClose();
        });
        document.addEventListener("keydown", (event) => {
            if (String(event?.key || "") === "Escape") taskProcessesClose();
        });
        window.addEventListener("pagehide", () => {
            taskProcessesStopElapsedTimer();
            if (TaskProcessesState.reconcileTimerId) {
                window.clearInterval(TaskProcessesState.reconcileTimerId);
                TaskProcessesState.reconcileTimerId = 0;
            }
        });
    }
    if (!TaskProcessesState.reconcileTimerId) {
        TaskProcessesState.reconcileTimerId = window.setInterval(() => {
            if (!taskProcessesVisible()) return;
            void taskProcessesRefresh({ quiet: true });
        }, TASK_PROCESSES_RECONCILE_MS);
    }
}
