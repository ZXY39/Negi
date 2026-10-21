// 任务列表 WebSocket 路由：帧表与连接生命周期。状态处理在 tasks/hall-store.js。

const TASK_WS_HANDLERS = {
    "task.worker.status": (data) => {
        applyTaskWorkerStatus(data || {});
    },
    "task.summary.patch": (data) => {
        const patchTask = data?.task || {};
        const patchTaskId = String(patchTask?.task_id || "");
        const previousStatus = taskStatusKey(S.tasksById?.[patchTaskId] || null);
        patchTaskListItem(patchTask);
        // 任务转终态后服务端会做终态清理+大小对账（异步），延迟全量刷新
        // 让卡片拿到清理后的最终占用值。
        const nextStatus = taskStatusKey(patchTask);
        if (previousStatus === "in_progress" && (nextStatus === "success" || nextStatus === "failed")) {
            scheduleTaskListReconcileSoon();
        }
    },
    "task.token.patch": (data, payload) => {
        S.lastTaskTokenPatchAt = new Date().toISOString();
        patchTaskListItem({
            task_id: data?.task_id || payload.task_id || "",
            updated_at: data?.updated_at || "",
            token_usage: data?.token_usage || {},
        });
    },
    "task.deleted": (data) => {
        removeTaskListItem(data?.task_id || "");
    },
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
        const handler = TASK_WS_HANDLERS[payload.type];
        if (handler) handler(payload.data || {}, payload);
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






function closeTasksWs() {
    const socket = S.tasksWs;
    S.tasksWs = null;
    if (!socket) return;
    socket.onclose = null;
    socket.close();
}

