// 任务详情页右上角的后台气泡：合并两类后台（owned processes 与节点后台工具作业）。
// 数据源 GET/POST /tasks/{id}/processes 与 GET/POST /tasks/{id}/tool-jobs，按 kind
// 调正确的停止入口；`/processes` 每行都有 PID 的合同不变，工具作业行不编造 PID。
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

// 两类后台的统一行形状：key 稳定（process:<pid> / tool_job:<job_id>），停止按 kind 分流。
function taskProcessesNormalizeProcess(item) {
    const pid = Number(item?.pid) || 0;
    return {
        kind: "process",
        key: `process:${pid}`,
        pid,
        label: String(item?.purpose || "").trim(),
        detail: String(item?.cmd || "").trim(),
        nodeId: String(item?.node_id || "").trim(),
        nodeGoal: String(item?.node_goal || "").trim(),
        startedAt: String(item?.started_at || ""),
        status: String(item?.status || "running"),
    };
}

function taskProcessesNormalizeToolJob(item) {
    const jobId = String(item?.job_id || "").trim();
    return {
        kind: "tool_job",
        key: `tool_job:${jobId}`,
        jobId,
        label: String(item?.hint || "").trim() || String(item?.tool_name || "tool"),
        detail: String(item?.tool_name || "tool").trim(),
        nodeId: String(item?.node_id || "").trim(),
        nodeGoal: "",
        startedAt: String(item?.started_at || ""),
        status: String(item?.status || "running"),
    };
}

function taskProcessesRow(item) {
    const row = document.createElement("div");
    row.className = "task-processes-row";
    row.setAttribute("role", "listitem");
    row.dataset.taskProcessKey = String(item?.key || "");
    if (item?.kind === "process") {
        row.dataset.taskProcessPid = String(item?.pid || 0);
    }

    const kind = document.createElement("span");
    kind.className = "task-processes-kind";
    kind.textContent = item?.kind === "tool_job" ? "工具" : "进程";

    const main = document.createElement("span");
    main.className = "task-processes-main";
    const label = String(item?.label || "").trim();
    const detail = String(item?.detail || "").trim();
    main.textContent =
        label
        || detail
        || (item?.kind === "process" ? `pid ${Number(item?.pid) || 0}` : detail || "tool");
    main.title = [item?.kind === "tool_job" ? `tool: ${detail}` : "", label, detail]
        .filter(Boolean)
        .join("\n");

    const nodeId = String(item?.nodeId || "").trim();
    const node = document.createElement("button");
    node.type = "button";
    node.className = "task-processes-node";
    node.dataset.taskProcessNode = nodeId;
    node.textContent = nodeId ? taskProcessesTruncate(nodeId, 22) : "未归属";
    const nodeGoal = String(item?.nodeGoal || "").trim();
    node.title = nodeId ? `定位到节点${nodeGoal ? `：${nodeGoal}` : ` ${nodeId}`}` : "该后台项没有节点归属";
    node.disabled = !nodeId;

    const elapsed = document.createElement("span");
    elapsed.className = "task-processes-elapsed";
    elapsed.dataset.taskProcessStarted = String(item?.startedAt || "");
    elapsed.textContent = taskProcessesElapsedText(item?.startedAt);

    const action = document.createElement("button");
    action.type = "button";
    action.className = "task-processes-stop";
    action.dataset.taskProcessStop = String(item?.key || "");
    const stopping = TaskProcessesState.stopping.has(String(item?.key || ""));
    action.textContent = stopping ? "停止中" : "停止";
    action.disabled = stopping;
    if (stopping) action.setAttribute("aria-busy", "true");

    row.appendChild(kind);
    row.appendChild(main);
    row.appendChild(node);
    row.appendChild(elapsed);
    row.appendChild(action);
    return row;
}

function taskProcessesRenderRows() {
    const list = taskProcessesEl("task-processes-list");
    if (!list) return;
    const scrollTop = list.scrollTop;
    while (list.firstChild) list.removeChild(list.firstChild);
    TaskProcessesState.items.forEach((item) => list.appendChild(taskProcessesRow(item)));
    taskProcessesPosition();
    list.scrollTop = scrollTop;
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

function taskProcessesPosition() {
    if (!TaskProcessesState.open) return;
    positionListPopover(
        taskProcessesEl("task-processes-btn"),
        taskProcessesEl("task-processes-popover"),
        taskProcessesEl("task-processes-list"),
    );
}

function taskProcessesSetOpen(open) {
    const popover = taskProcessesEl("task-processes-popover");
    const toggle = taskProcessesEl("task-processes-btn");
    const next = !!open && TaskProcessesState.items.length > 0;
    TaskProcessesState.open = next;
    if (popover) popover.hidden = !next;
    if (toggle) toggle.setAttribute("aria-expanded", next ? "true" : "false");
    if (next) {
        taskProcessesPosition();
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

async function taskProcessesStop(rawKey) {
    let key = String(rawKey || "");
    if (!key.includes(":")) {
        // 兼容旧的纯 pid 调用形态。
        const pid = Number(key) || 0;
        key = pid > 0 ? `process:${pid}` : "";
    }
    const taskId = TaskProcessesState.taskId || taskProcessesCurrentTaskId();
    const item = TaskProcessesState.items.find((entry) => entry?.key === key);
    if (!taskId || !key) return;
    const isToolJob = item?.kind === "tool_job" || key.startsWith("tool_job:");
    TaskProcessesState.stopping.add(key);
    taskProcessesRenderRows();
    try {
        if (isToolJob) {
            const jobId = String(item?.jobId || key.slice("tool_job:".length));
            if (typeof ApiClient.stopTaskToolJob !== "function") {
                throw new Error("tool job stop lane unavailable");
            }
            await ApiClient.stopTaskToolJob(taskId, jobId);
            if (typeof showToast === "function") {
                showToast({
                    title: "已请求停止工具作业",
                    text: "停止结果以后台状态为准。",
                    kind: "info",
                });
            }
        } else {
            const normalizedPid = Number(key.slice("process:".length)) || 0;
            if (!normalizedPid) return;
            await ApiClient.stopTaskProcess(taskId, normalizedPid);
            if (typeof showToast === "function") {
                showToast({
                    title: "已停止进程",
                    text: `pid ${normalizedPid} 已终止。`,
                    kind: "info",
                });
            }
        }
    } catch (error) {
        const code = typeof ApiClient.getErrorCode === "function"
            ? String(ApiClient.getErrorCode(error) || "")
            : String(error?.code || "");
        const status = Number(error?.status);
        if (isToolJob) {
            if (code === "tool_job_finished" || status === 409) {
                if (typeof showToast === "function") {
                    showToast({ title: "工具已结束", text: "该后台工具作业已经完成或被停止。", kind: "info" });
                }
            } else if (status === 404 || code === "tool_job_not_found") {
                if (typeof showToast === "function") {
                    showToast({ title: "工具作业不存在", text: "作业记录不在当前任务上。", kind: "warn" });
                }
            } else if (typeof showToast === "function") {
                showToast({ title: "停止失败", text: "未能停止该后台工具作业，请重试。", kind: "error" });
            }
        } else if (code === "process_not_found" || status === 404 || status === 409) {
            if (typeof showToast === "function") {
                showToast({ title: "进程已结束", text: "该进程已经不在运行。", kind: "info" });
            }
        } else if (typeof showToast === "function") {
            showToast({ title: "停止失败", text: "未能停止该后台进程，请重试。", kind: "error" });
        }
    } finally {
        TaskProcessesState.stopping.delete(key);
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
        // 两类后台合并：processes（每行有 PID）与 tool-jobs（无 PID，kind 区分）。
        // 一边失败不吞掉另一边：进程列表照常渲染，工具作业按空处理。
        const [processResult, toolJobResult] = await Promise.allSettled([
            ApiClient.getTaskProcesses(taskId),
            ApiClient.getTaskToolJobs
                ? ApiClient.getTaskToolJobs(taskId)
                : Promise.resolve({ ok: true, items: [] }),
        ]);
        if (token !== TaskProcessesState.requestToken) return;
        if (taskProcessesCurrentTaskId() !== taskId) return;
        const processItems = processResult.status === "fulfilled" && Array.isArray(processResult.value?.items)
            ? processResult.value.items.map(taskProcessesNormalizeProcess)
            : [];
        const toolJobItems = toolJobResult.status === "fulfilled" && Array.isArray(toolJobResult.value?.items)
            ? toolJobResult.value.items.map(taskProcessesNormalizeToolJob)
            : [];
        TaskProcessesState.items = [...processItems, ...toolJobItems].sort((a, b) => {
            const aTime = Date.parse(String(a?.startedAt || "")) || 0;
            const bTime = Date.parse(String(b?.startedAt || "")) || 0;
            return bTime - aTime;
        });
        taskProcessesRender();
        if (processResult.status === "rejected" && !quiet && typeof showToast === "function") {
            showToast({ title: "进程列表加载失败", text: "请稍后重试。", kind: "warn" });
        }
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
            if (taskProcessesEl("task-processes-popover")?.contains(event.target)) return;
            taskProcessesClose();
        });
        document.addEventListener("keydown", (event) => {
            if (String(event?.key || "") === "Escape") taskProcessesClose();
        });
        window.addEventListener("resize", taskProcessesPosition);
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
