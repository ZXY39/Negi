"use strict";

// 域模块：只声明端点、请求体与响应归一；实际发请求统一走 ApiCore。
(function () {
    const domains = (window.ApiDomains = window.ApiDomains || {});
    domains.tasks = {
    async getTasks(scope = 1, sessionId = this.getActiveSessionId()) {
        const data = await this._request("GET", "/api/tasks", {
            params: { session_id: sessionId, scope },
            requestKey: `tasks:list:${sessionId}:${scope}`,
        });
        return data || { items: [] };
    },

    async getTaskWorkerStatus() {
        const data = await this._request("GET", "/api/tasks/worker-status", {
            requestKey: "tasks:worker-status",
        });
        return data || {};
    },

    async getTask(taskId, markRead = false) {
        // 大任务详情负载与节点数正相关（token 明细、调用记录、根节点详情等），
        // 10s 默认超时在大树上会误报 "Failed to open task: Request timeout"。
        return this._request("GET", `/api/tasks/${taskId}`, {
            params: { mark_read: markRead },
            requestKey: `tasks:detail:${taskId}`,
            timeoutMs: 120000,
        });
    },

    async getTaskTokenLedger(taskId, { limit = 300 } = {}) {
        // 独立的 requestKey：同 key 的后发请求会 abort 前一条，蹭 tasks:detail:* 会让
        // 点「刷新」打断一次正在加载的大任务详情。
        return this._request("GET", `/api/tasks/${taskId}/token-ledger`, {
            params: { limit },
            requestKey: `tasks:token-ledger:${taskId}`,
            timeoutMs: 30000,
        });
    },

    async getTaskModelCallPage(taskId, { page = 1, size = 100, anchor = null } = {}) {
        // 明细按页取数：只回一页，不带按模型 rollup。anchor 为 null 时服务端按当前账本
        // 尾部起算，之后每次翻页都回传同一个 anchor，页号才不会随新调用漂移。
        const params = { page, size };
        if (anchor !== null && anchor !== undefined && Number(anchor) > 0) params.anchor = Number(anchor);
        return this._request("GET", `/api/tasks/${taskId}/model-call-page`, {
            params,
            requestKey: `tasks:model-call-page:${taskId}`,
            timeoutMs: 30000,
        });
    },

    async getTaskNodeDetail(taskId, nodeId, { detailLevel = "summary" } = {}) {
        const normalizedDetailLevel = String(detailLevel || "summary").trim().toLowerCase() === "full"
            ? "full"
            : "summary";
        const data = await this._request("GET", `/api/tasks/${taskId}/nodes/${nodeId}`, {
            params: {
                detail_level: normalizedDetailLevel,
            },
            requestKey: `tasks:node:${taskId}:${nodeId}:${normalizedDetailLevel}`,
        });
        return data.item || null;
    },

    async pauseTaskNode(taskId, nodeId, { cascade = false, remark = "" } = {}) {
        const data = await this._request("POST", `/api/tasks/${taskId}/nodes/${nodeId}/pause`, {
            body: { cascade: !!cascade, remark: String(remark || "") },
        });
        return data.node || null;
    },

    async resumeTaskNode(taskId, nodeId) {
        const data = await this._request("POST", `/api/tasks/${taskId}/nodes/${nodeId}/resume`);
        return data.node || null;
    },

    async getTaskErrorLog(taskId) {
        return this._request("GET", `/api/tasks/${taskId}/error-log`, {
            requestKey: `tasks:error-log:${taskId}`,
        });
    },

    async getTaskNodeErrorLog(taskId, nodeId) {
        return this._request("GET", `/api/tasks/${taskId}/nodes/${nodeId}/error-log`, {
            requestKey: `tasks:node-error-log:${taskId}:${nodeId}`,
        });
    },

    async getTaskNodeLatestContext(taskId, nodeId) {
        return this._request("GET", `/api/tasks/${taskId}/nodes/${nodeId}/latest-context`, {
            requestKey: `tasks:node-context:${taskId}:${nodeId}`,
        });
    },

    async getTaskTreeSnapshot(taskId, { maxNodes = null, afterNodeId = "" } = {}) {
        return this._request("GET", `/api/tasks/${taskId}/tree-snapshot`, {
            params: {
                max_nodes: maxNodes !== null && maxNodes !== undefined ? Math.max(1, Number(maxNodes) || 1) : undefined,
                after_node_id: afterNodeId || undefined,
            },
            // 游标进入 requestKey：不同分块的请求互不打断，重试相同分块时仍可去重。
            requestKey: `tasks:tree-snapshot:${taskId}:${String(afterNodeId || "")}`,
            timeoutMs: 30000,
        });
    },

    async getTaskNodeTreeSubtree(taskId, nodeId, { roundId = "" } = {}) {
        return this._request("GET", `/api/tasks/${taskId}/nodes/${nodeId}/tree-subtree`, {
            params: {
                round_id: roundId || undefined,
            },
            requestKey: `tasks:tree-subtree:${taskId}:${nodeId}:${roundId || "default"}`,
        });
    },

    // 定向通知：对以该节点为根的子树追加用户通知（子树屏障分发）。
    async appendTaskNodeNotice(taskId, nodeId, message) {
        return this._request("POST", `/api/tasks/${taskId}/nodes/${nodeId}/notice`, {
            body: { message: String(message || "") },
            timeoutMs: 30000,
        });
    },

    async pauseTask(taskId) {
        const data = await this.post(`/api/tasks/${taskId}/pause`);
        return data.task || null;
    },

    async resumeTask(taskId) {
        const data = await this.post(`/api/tasks/${taskId}/resume`);
        return data.task || null;
    },

    async getTaskPauseState(taskId) {
        const data = await this._request("GET", `/api/tasks/${taskId}/pause-state`, {
            requestKey: `tasks:pause-state:${taskId}`,
            timeoutMs: 5000,
        });
        return data.item || null;
    },

    async cancelTask(taskId) {
        const data = await this.post(`/api/tasks/${taskId}/cancel`);
        return data.task || null;
    },

    async deleteTask(taskId, options = {}) {
        const normalized = options && typeof options === "object" && !Array.isArray(options) ? options : {};
        return this._request("DELETE", `/api/tasks/${taskId}`, normalized);
    },

    async clearTaskTempFiles(taskId, options = {}) {
        const normalized = options && typeof options === "object" && !Array.isArray(options) ? options : {};
        return this._request("POST", `/api/tasks/${taskId}/clear-temp`, normalized);
    },

    async bulkDeleteTasks(taskIds = [], options = {}) {
        const normalized = options && typeof options === "object" && !Array.isArray(options) ? options : {};
        const body = {
            task_ids: Array.isArray(taskIds) ? taskIds : [],
        };
        return this._request("POST", "/api/tasks/bulk-delete", {
            ...normalized,
            body,
        });
    },

    async getTaskArtifacts(taskId) {
        const data = await this.get(`/api/tasks/${taskId}/artifacts`);
        return data.items || [];
    },

    async getTaskArtifact(taskId, artifactId) {
        return this.get(`/api/tasks/${taskId}/artifacts/${artifactId}`);
    },

    async describeContent({ ref = "", path = "", view = "canonical" } = {}) {
        return this.get("/api/content/describe", { ref, path, view });
    },

    async searchContent({ query, ref = "", path = "", view = "canonical", limit = 10, before = 2, after = 2 } = {}) {
        return this.get("/api/content/search", { query, ref, path, view, limit, before, after });
    },

    async openContent({ ref = "", path = "", view = "canonical", startLine = null, endLine = null, aroundLine = null, window = null } = {}) {
        return this.get("/api/content/open", {
            ref,
            path,
            view,
            start_line: startLine,
            end_line: endLine,
            around_line: aroundLine,
            window,
        });
    },

    async readContent({ ref = "", path = "", view = "canonical" } = {}) {
        return this.get("/api/content/read", { ref, path, view });
    },

    async applyTaskArtifact(taskId, artifactId) {
        return this.post(`/api/tasks/${taskId}/artifacts/${artifactId}/apply`);
    }
    };
})();
