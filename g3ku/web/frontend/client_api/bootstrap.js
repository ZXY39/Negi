"use strict";

// 域模块：只声明端点、请求体与响应归一；实际发请求统一走 ApiCore。
(function () {
    const domains = (window.ApiDomains = window.ApiDomains || {});
    domains.bootstrap = {
    async getBootstrapStatus(options = {}) {
        const normalized = options && typeof options === "object" && !Array.isArray(options) ? options : {};
        const data = await this._request("GET", "/api/bootstrap/status", normalized);
        return data.item || null;
    },

    async setupBootstrap(payload) {
        const data = await this.post("/api/bootstrap/setup", payload || {});
        return data.item || null;
    },

    // 原生目录框由服务端弹出并等到用户操作完成，超时按分钟级给。
    async pickBootstrapDataDir() {
        const data = await this._request("POST", "/api/bootstrap/pick-data-dir", { timeoutMs: 600000 });
        return data.item || null;
    },

    async _waitForBootstrapRuntimeReady({ maxPolls = 6, pollIntervalMs = 1000, onProgress = null } = {}) {
        const totalPolls = Math.max(1, Number(maxPolls) || 1);
        for (let pollIndex = 0; pollIndex < totalPolls; pollIndex += 1) {
            if (pollIndex > 0) {
                await this._delay(pollIntervalMs);
            }
            try {
                const status = await this.getBootstrapStatus({ timeoutMs: 4000 });
                const runtime = status?.runtime && typeof status.runtime === "object" ? status.runtime : {};
                const mode = String(status?.mode || "").trim().toLowerCase();
                const runtimeReady = Boolean(status?.runtime_ready ?? runtime.ready);
                const runtimeBootstrapping = Boolean(status?.runtime_bootstrapping ?? runtime.bootstrapping);
                if (typeof onProgress === "function") {
                    onProgress({
                        mode,
                        status,
                        runtimeReady,
                        runtimeBootstrapping,
                        pollIndex: pollIndex + 1,
                        totalPolls,
                    });
                }
                if (mode === "unlocked" && runtimeReady) {
                    return status;
                }
                if (mode !== "unlocked" && !runtimeBootstrapping) {
                    return null;
                }
            } catch (error) {
                if (typeof onProgress === "function") {
                    onProgress({
                        mode: "",
                        status: null,
                        runtimeReady: false,
                        runtimeBootstrapping: false,
                        pollIndex: pollIndex + 1,
                        totalPolls,
                        error,
                    });
                }
            }
        }
        return null;
    },

    async unlockBootstrap(payload, { onRetry = null, onProgress = null } = {}) {
        const maxRetries = 3;
        let lastTimeoutError = null;
        for (let retryIndex = 0; retryIndex <= maxRetries; retryIndex += 1) {
            try {
                const data = await this._request("POST", "/api/bootstrap/unlock", {
                    body: payload || {},
                    requestKey: this._bootstrapUnlockRequestKey,
                    timeoutMs: 15000,
                });
                return data.item || null;
            } catch (error) {
                if (!this._isTimeoutError(error)) {
                    throw error;
                }
                lastTimeoutError = error;
                const recoveredStatus = await this._waitForBootstrapRuntimeReady({ onProgress });
                if (recoveredStatus) {
                    return recoveredStatus;
                }
                if (retryIndex >= maxRetries) {
                    break;
                }
                if (typeof onRetry === "function") {
                    onRetry({
                        retryIndex: retryIndex + 1,
                        maxRetries,
                    });
                }
                await this._delay(Math.min(1600, 450 * (retryIndex + 1)));
            }
        }
        const timeoutError = new Error("\u8d85\u65f6\u8bf7\u91cd\u8bd5");
        timeoutError.name = "AbortError";
        timeoutError.isTimeout = true;
        timeoutError.cause = lastTimeoutError;
        throw timeoutError;
    },

    async getBootstrapExitCheck() {
        const data = await this.get("/api/bootstrap/exit-check");
        return data.item || null;
    },

    async exitBootstrap(payload) {
        const data = await this.post("/api/bootstrap/exit", payload || {});
        return data.item || null;
    },

    async getUpdateStatus() {
        const data = await this.get("/api/update/status");
        return data.item || null;
    },

    async checkUpdateNow() {
        const data = await this.post("/api/update/check", {});
        return data.item || null;
    },

    async applyUpdate(payload) {
        const data = await this.post("/api/update/apply", payload || {});
        return data.item || null;
    },

    async changeBootstrapPassword(payload) {
        const data = await this.post("/api/bootstrap/change-password", payload || {});
        return data.item || null;
    },

    async setBootstrapAutoUnlock(enabled) {
        const data = await this.post("/api/bootstrap/auto-unlock", { enabled: !!enabled });
        return data.item || null;
    },

    async lockBootstrap() {
        const data = await this.post("/api/bootstrap/lock", {});
        return data.item || null;
    },

    async exportConfigBundle({ password = "", useProjectPassword = true, includeConfig = true, includeMemory = false } = {}) {
        const data = await this._request("POST", "/api/bootstrap/config-bundle/export", {
            body: {
                password,
                use_project_password: !!useProjectPassword,
                include_config: !!includeConfig,
                include_memory: !!includeMemory,
            },
            timeoutMs: 60000,
        });
        return data.item || null;
    },

    getConfigBundleDownloadUrl(filename) {
        return this._buildUrl("/api/bootstrap/config-bundle/download", { filename }).toString();
    },

    async importConfigBundle(
        file,
        password,
        { confirmRunningWork = false, currentPassword = "", includeConfig = true, includeMemory = false } = {},
    ) {
        const formData = new FormData();
        formData.append("file", file);
        formData.append("password", password);
        formData.append("confirm_running_work", confirmRunningWork ? "true" : "false");
        formData.append("current_password", currentPassword || "");
        formData.append("include_config", includeConfig ? "true" : "false");
        formData.append("include_memory", includeMemory ? "true" : "false");
        const data = await this._request("POST", "/api/bootstrap/config-bundle/import", {
            body: formData,
            timeoutMs: 180000,
        });
        return data.item || null;
    },

    getCeoWsUrl(sessionId = this.getActiveSessionId()) {
        const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
        const host = API_BASE_URL ? new URL(API_BASE_URL).host : window.location.host;
        const normalized = String(sessionId || "").trim();
        return normalized
            ? `${protocol}//${host}/api/ws/ceo?session_id=${encodeURIComponent(normalized)}`
            : `${protocol}//${host}/api/ws/ceo`;
    },

    getTaskWsUrl(taskId, sessionId = this.getActiveSessionId()) {
        const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
        const host = API_BASE_URL ? new URL(API_BASE_URL).host : window.location.host;
        return `${protocol}//${host}/api/ws/tasks/${encodeURIComponent(taskId)}?session_id=${encodeURIComponent(sessionId)}`;
    },

    getTasksWsUrl(sessionId = "all") {
        const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
        const host = API_BASE_URL ? new URL(API_BASE_URL).host : window.location.host;
        return `${protocol}//${host}/api/ws/tasks?session_id=${encodeURIComponent(sessionId)}`;
    }
    };
})();
