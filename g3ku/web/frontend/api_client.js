const API_BASE_URL = "";
const FALLBACK_SESSION_ID = "web:shared";

class ApiClient {
    static _activeSessionId = "";
    static _requestControllers = new Map();
    static _requestTokens = new Map();
    static _bootstrapUnlockRequestKey = "bootstrap:unlock";

    static _normalizeErrorCode(value) {
        if (typeof value !== "string") return "";
        const normalized = value.trim();
        if (!normalized) return "";
        return /^[a-z0-9_]+$/i.test(normalized) ? normalized.toLowerCase() : "";
    }

    static getErrorCode(value) {
        if (!value) return "";
        if (typeof value === "string") return this._normalizeErrorCode(value);
        if (typeof value !== "object") return "";
        return this._normalizeErrorCode(value.code)
            || this.getErrorCode(value.data)
            || this.getErrorCode(value.detail)
            || this.getErrorCode(value.payload)
            || this.getErrorCode(value.message)
            || "";
    }

    static _friendlyErrorMessageForCode(code) {
        switch (String(code || "").trim().toLowerCase()) {
            case "no_model_configured":
                return "当前项目还没有配置可用模型。请先进入“模型配置”页面，新增并保存至少一个模型，并把它分配给主Agent（CEO）角色。";
            case "project_locked":
                return "项目当前已锁定，请先完成解锁后再继续。";
            case "task_service_unavailable":
                return "任务运行服务暂未就绪，请稍后再试。";
            case "main_task_service_unavailable":
                return "主任务服务暂未就绪，请稍后再试。";
            case "task_worker_starting":
                return "Task worker is starting. Controls will be available shortly.";
            case "task_worker_stale":
                return "Task worker status is temporarily stale. Wait for reconnection and try again.";
            case "task_worker_offline":
                return "Task worker is offline. Controls are unavailable right now.";
            default:
                return "";
        }
    }

    static friendlyErrorMessage(value, fallback = "") {
        const code = this.getErrorCode(value);
        const friendly = this._friendlyErrorMessageForCode(code);
        if (friendly) return friendly;
        if (value && typeof value === "object") {
            const detailMessage = typeof value.detail?.message === "string" ? value.detail.message.trim() : "";
            if (detailMessage) return detailMessage;
            const directMessage = typeof value.message === "string" ? value.message.trim() : "";
            if (directMessage) return directMessage;
        }
        if (typeof value === "string" && value.trim()) return value.trim();
        return String(fallback || "").trim() || "未知错误";
    }

    static getActiveSessionId() {
        return String(this._activeSessionId || FALLBACK_SESSION_ID).trim() || FALLBACK_SESSION_ID;
    }

    static setActiveSessionId(sessionId) {
        this._activeSessionId = String(sessionId || "").trim() || FALLBACK_SESSION_ID;
        return this._activeSessionId;
    }

    static _buildUrl(path, params = {}) {
        const base = API_BASE_URL || window.location.origin;
        const url = new URL(path, base);
        Object.entries(params).forEach(([key, value]) => {
            if (value !== undefined && value !== null && value !== "") {
                url.searchParams.set(key, String(value));
            }
        });
        return url;
    }

    static _delay(delayMs) {
        return new Promise((resolve) => window.setTimeout(resolve, Math.max(0, Number(delayMs) || 0)));
    }

    static _isTimeoutError(error) {
        if (!error || typeof error !== "object") return false;
        if (error.isTimeout === true) return true;
        return String(error?.name || "").trim() === "AbortError" && String(error?.message || "").trim() === "Request timeout";
    }

    static async _request(method, path, { params = {}, body, headers = {}, requestKey = "", timeoutMs = 10000 } = {}) {
        const url = this._buildUrl(path, params);
        const requestHeaders = { ...headers };
        const isFormData = body instanceof FormData;
        if (body !== undefined && !isFormData && !requestHeaders["Content-Type"]) {
            requestHeaders["Content-Type"] = "application/json";
        }
        const normalizedKey = String(requestKey || "").trim();
        if (normalizedKey && this._requestControllers.has(normalizedKey)) {
            try {
                this._requestControllers.get(normalizedKey)?.abort();
            } catch (error) {
                void error;
            }
        }
        const controller = new AbortController();
        const token = `${Date.now()}:${Math.random().toString(36).slice(2)}`;
        if (normalizedKey) {
            this._requestControllers.set(normalizedKey, controller);
            this._requestTokens.set(normalizedKey, token);
        }
        try {
            let didTimeout = false;
            const timeoutId = window.setTimeout(() => {
                didTimeout = true;
                controller.abort(new DOMException("Request timeout", "AbortError"));
            }, Math.max(1000, Number(timeoutMs) || 10000));
            let response;
            try {
                response = await fetch(url.toString(), {
                    method,
                    headers: requestHeaders,
                    body: body === undefined ? undefined : (isFormData ? body : JSON.stringify(body)),
                    signal: controller.signal,
                });
            } catch (error) {
                if (error?.name === "AbortError") {
                    const abortError = new Error(didTimeout ? "Request timeout" : "Request aborted");
                    abortError.name = "AbortError";
                    abortError.isTimeout = didTimeout;
                    throw abortError;
                }
                throw error;
            } finally {
                window.clearTimeout(timeoutId);
            }
            if (normalizedKey && this._requestTokens.get(normalizedKey) !== token) {
                const staleError = new Error("Stale request");
                staleError.name = "AbortError";
                throw staleError;
            }
            if (!response.ok) {
                const payload = await response.json().catch(() => ({}));
                const detail = payload.detail !== undefined ? payload.detail : payload.message;
                const fallbackMessage = typeof detail === "string"
                    ? detail
                    : (detail && typeof detail === "object" && typeof detail.message === "string" && detail.message.trim())
                        ? detail.message.trim()
                        : payload.message || `HTTP ${response.status}`;
                const code = this.getErrorCode(detail) || this.getErrorCode(payload);
                const message = this.friendlyErrorMessage({ code, detail, payload, message: fallbackMessage }, fallbackMessage);
                const error = new Error(message);
                error.code = code;
                if (detail && typeof detail === "object") error.data = detail;
                else if (code) error.data = { code, message };
                error.status = response.status;
                error.payload = payload;
                throw error;
            }
            const payload = await response.json();
            if (normalizedKey && this._requestTokens.get(normalizedKey) !== token) {
                const staleError = new Error("Stale request");
                staleError.name = "AbortError";
                throw staleError;
            }
            return payload;
        } finally {
            if (normalizedKey && this._requestControllers.get(normalizedKey) === controller) {
                this._requestControllers.delete(normalizedKey);
                this._requestTokens.delete(normalizedKey);
            }
        }
    }

    static get(path, params = {}) {
        return this._request("GET", path, { params });
    }

    static post(path, body = {}, params = {}) {
        return this._request("POST", path, { params, body });
    }

    static put(path, body = {}, params = {}) {
        return this._request("PUT", path, { params, body });
    }

    static delete(path, options = {}) {
        const normalized = options && typeof options === "object" && !Array.isArray(options)
            ? (Object.prototype.hasOwnProperty.call(options, "params")
                || Object.prototype.hasOwnProperty.call(options, "body")
                || Object.prototype.hasOwnProperty.call(options, "headers"))
                ? options
                : { params: options }
            : {};
        return this._request("DELETE", path, normalized);
    }

    static async getBootstrapStatus(options = {}) {
        const normalized = options && typeof options === "object" && !Array.isArray(options) ? options : {};
        const data = await this._request("GET", "/api/bootstrap/status", normalized);
        return data.item || null;
    }

    static async setupBootstrap(payload) {
        const data = await this.post("/api/bootstrap/setup", payload || {});
        return data.item || null;
    }

    static async _waitForBootstrapRuntimeReady({ maxPolls = 6, pollIntervalMs = 1000, onProgress = null } = {}) {
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
    }

    static async unlockBootstrap(payload, { onRetry = null, onProgress = null } = {}) {
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
    }

    static async getBootstrapExitCheck() {
        const data = await this.get("/api/bootstrap/exit-check");
        return data.item || null;
    }

    static async exitBootstrap(payload) {
        const data = await this.post("/api/bootstrap/exit", payload || {});
        return data.item || null;
    }

    static getCeoWsUrl(sessionId = this.getActiveSessionId()) {
        const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
        const host = API_BASE_URL ? new URL(API_BASE_URL).host : window.location.host;
        const normalized = String(sessionId || "").trim();
        return normalized
            ? `${protocol}//${host}/api/ws/ceo?session_id=${encodeURIComponent(normalized)}`
            : `${protocol}//${host}/api/ws/ceo`;
    }

    static getTaskWsUrl(taskId, sessionId = this.getActiveSessionId()) {
        const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
        const host = API_BASE_URL ? new URL(API_BASE_URL).host : window.location.host;
        return `${protocol}//${host}/api/ws/tasks/${encodeURIComponent(taskId)}?session_id=${encodeURIComponent(sessionId)}`;
    }

    static getTasksWsUrl(sessionId = "all") {
        const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
        const host = API_BASE_URL ? new URL(API_BASE_URL).host : window.location.host;
        return `${protocol}//${host}/api/ws/tasks?session_id=${encodeURIComponent(sessionId)}`;
    }

    static async listCeoSessions() {
        return this._request("GET", "/api/ceo/sessions", { requestKey: "ceo:sessions" });
    }

    static async createCeoSession(payload = {}) {
        return this.post("/api/ceo/sessions", payload || {});
    }

    static async renameCeoSession(sessionId, payload = {}) {
        return this._request("PATCH", `/api/ceo/sessions/${encodeURIComponent(sessionId)}`, { body: payload || {} });
    }

    static async getMainRuntimeTaskDefaults() {
        return this.get("/api/main-runtime/settings");
    }

    static async updateMainRuntimeTaskDefaults(payload = {}) {
        return this.put("/api/main-runtime/settings", payload || {});
    }

    static async getCeoTaskDefaults(sessionId) {
        void sessionId;
        return this.getMainRuntimeTaskDefaults();
    }

    static async updateCeoTaskDefaults(sessionId, payload = {}) {
        void sessionId;
        return this.updateMainRuntimeTaskDefaults(payload);
    }

    static async activateCeoSession(sessionId) {
        return this.post(`/api/ceo/sessions/${encodeURIComponent(sessionId)}/activate`, {});
    }

    static async getCeoSessionDeleteCheck(sessionId) {
        return this.get(`/api/ceo/sessions/${encodeURIComponent(sessionId)}/delete-check`);
    }

    static async deleteCeoSession(sessionId, payload = {}) {
        return this.delete(`/api/ceo/sessions/${encodeURIComponent(sessionId)}`, { body: payload || {} });
    }

    static async uploadCeoFiles(files = [], sessionId = this.getActiveSessionId()) {
        const formData = new FormData();
        [...files].forEach((file) => formData.append("files", file));
        const data = await this._request("POST", "/api/ceo/uploads", {
            params: { session_id: sessionId },
            body: formData,
        });
        return data.items || [];
    }

    static async getTasks(scope = 1, sessionId = this.getActiveSessionId()) {
        const data = await this._request("GET", "/api/tasks", {
            params: { session_id: sessionId, scope },
            requestKey: `tasks:list:${sessionId}:${scope}`,
        });
        return data || { items: [] };
    }

    static async getTaskWorkerStatus() {
        const data = await this._request("GET", "/api/tasks/worker-status", {
            requestKey: "tasks:worker-status",
        });
        return data || {};
    }

    static async getTask(taskId, markRead = false) {
        return this._request("GET", `/api/tasks/${taskId}`, {
            params: { mark_read: markRead },
            requestKey: `tasks:detail:${taskId}`,
        });
    }

    static async getTaskNodeDetail(taskId, nodeId) {
        const data = await this._request("GET", `/api/tasks/${taskId}/nodes/${nodeId}`, {
            requestKey: `tasks:node:${taskId}:${nodeId}`,
        });
        return data.item || null;
    }

    static async getTaskNodeLatestContext(taskId, nodeId) {
        return this._request("GET", `/api/tasks/${taskId}/nodes/${nodeId}/latest-context`, {
            requestKey: `tasks:node-context:${taskId}:${nodeId}`,
        });
    }

    static async getTaskTreeSnapshot(taskId) {
        return this._request("GET", `/api/tasks/${taskId}/tree-snapshot`, {
            requestKey: `tasks:tree-snapshot:${taskId}`,
        });
    }

    static async getTaskNodeTreeSubtree(taskId, nodeId, { roundId = "" } = {}) {
        return this._request("GET", `/api/tasks/${taskId}/nodes/${nodeId}/tree-subtree`, {
            params: {
                round_id: roundId || undefined,
            },
            requestKey: `tasks:tree-subtree:${taskId}:${nodeId}:${roundId || "default"}`,
        });
    }

    static async pauseTask(taskId) {
        const data = await this.post(`/api/tasks/${taskId}/pause`);
        return data.task || null;
    }

    static async resumeTask(taskId) {
        const data = await this.post(`/api/tasks/${taskId}/resume`);
        return data.task || null;
    }

    static async retryTask(taskId) {
        const data = await this.post(`/api/tasks/${taskId}/retry`);
        return data.task || null;
    }

    static async cancelTask(taskId) {
        const data = await this.post(`/api/tasks/${taskId}/cancel`);
        return data.task || null;
    }

    static async deleteTask(taskId) {
        return this.delete(`/api/tasks/${taskId}`);
    }

    static async getTaskArtifacts(taskId) {
        const data = await this.get(`/api/tasks/${taskId}/artifacts`);
        return data.items || [];
    }

    static async getTaskArtifact(taskId, artifactId) {
        return this.get(`/api/tasks/${taskId}/artifacts/${artifactId}`);
    }

    static async describeContent({ ref = "", path = "" } = {}) {
        return this.get("/api/content/describe", { ref, path });
    }

    static async searchContent({ query, ref = "", path = "", limit = 10, before = 2, after = 2 } = {}) {
        return this.get("/api/content/search", { query, ref, path, limit, before, after });
    }

    static async openContent({ ref = "", path = "", startLine = null, endLine = null, aroundLine = null, window = null } = {}) {
        return this.get("/api/content/open", {
            ref,
            path,
            start_line: startLine,
            end_line: endLine,
            around_line: aroundLine,
            window,
        });
    }

    static async applyTaskArtifact(taskId, artifactId) {
        return this.post(`/api/tasks/${taskId}/artifacts/${artifactId}/apply`);
    }

    static async getOrgGraphModels() {
        const data = await this.get("/api/models");
        return {
            catalog: Array.isArray(data.items) ? data.items : [],
            items: Array.isArray(data.items) ? data.items.map((item) => item.key) : [],
            roles: data.roles || {},
            roleIterations: data.roleIterations || data.role_iterations || {},
            roleConcurrency: data.roleConcurrency || data.role_concurrency || {},
            defaults: {},
        };
    }

    static _toManagedModelPayload(payload = {}) {
        const source = payload && typeof payload === "object" ? payload : {};
        const body = {};
        const has = (key) => Object.prototype.hasOwnProperty.call(source, key);
        const pick = (snakeKey, camelKey = null) => {
            if (has(snakeKey)) return source[snakeKey];
            if (camelKey && has(camelKey)) return source[camelKey];
            return undefined;
        };

        if (has("key")) body.key = source.key;
        if (has("enabled")) body.enabled = source.enabled;
        if (has("scopes")) body.scopes = source.scopes;
        if (has("description")) body.description = source.description;

        const mappedFields = [
            ["provider_model", "providerModel"],
            ["api_key", "apiKey"],
            ["api_base", "apiBase"],
            ["extra_headers", "extraHeaders"],
            ["max_tokens", "maxTokens"],
            ["temperature", null],
            ["reasoning_effort", "reasoningEffort"],
            ["retry_on", "retryOn"],
            ["retry_count", "retryCount"],
        ];
        mappedFields.forEach(([snakeKey, camelKey]) => {
            const value = pick(snakeKey, camelKey);
            if (value !== undefined) body[snakeKey] = value;
        });
        return body;
    }

    static async _refreshModelsAfter(requestPromise) {
        await requestPromise;
        return this.getOrgGraphModels();
    }

    static async createManagedModel(payload) {
        return this._refreshModelsAfter(this.post("/api/models", this._toManagedModelPayload(payload)));
    }

    static async updateManagedModel(modelKey, payload) {
        return this._refreshModelsAfter(this.put(`/api/models/${modelKey}`, this._toManagedModelPayload(payload)));
    }

    static async enableManagedModel(modelKey) {
        return this._refreshModelsAfter(this.post(`/api/models/${modelKey}/enable`));
    }

    static async disableManagedModel(modelKey) {
        return this._refreshModelsAfter(this.post(`/api/models/${modelKey}/disable`));
    }

    static async deleteManagedModel(modelKey) {
        return this._refreshModelsAfter(this.delete(`/api/models/${modelKey}`));
    }

    static _toRoleRouteBody(payload = {}) {
        const source = Array.isArray(payload) ? { modelKeys: payload } : (payload && typeof payload === "object" ? payload : {});
        const has = (key) => Object.prototype.hasOwnProperty.call(source, key);
        const modelKeys = Array.isArray(source.modelKeys)
            ? source.modelKeys
            : Array.isArray(source.model_keys)
                ? source.model_keys
                : undefined;
        const body = {};
        if (modelKeys !== undefined) {
            body.model_keys = modelKeys;
            body.modelKeys = modelKeys;
        }
        if (has("maxIterations") || has("max_iterations")) {
            const maxIterations = has("maxIterations") ? source.maxIterations : source.max_iterations;
            body.max_iterations = maxIterations;
            body.maxIterations = maxIterations;
        }
        if (has("maxConcurrency") || has("max_concurrency")) {
            const maxConcurrency = has("maxConcurrency") ? source.maxConcurrency : source.max_concurrency;
            body.max_concurrency = maxConcurrency;
            body.maxConcurrency = maxConcurrency;
        }
        return body;
    }

    static _toRoleRouteUpdatesBody(updates = {}) {
        const entries = Object.entries(updates && typeof updates === "object" ? updates : {});
        return {
            updates: Object.fromEntries(
                entries.map(([scope, payload]) => [String(scope || "").trim(), this._toRoleRouteBody(payload)])
            ),
        };
    }

    static async updateModelRoleChain(scope, payload) {
        return this._refreshModelsAfter(this._request("PUT", `/api/models/roles/${scope}`, {
            body: this._toRoleRouteBody(payload),
            timeoutMs: 20000,
        }));
    }

    static async updateModelRoleChains(updates) {
        return this._refreshModelsAfter(this._request("PUT", "/api/models/routes/batch", {
            body: this._toRoleRouteUpdatesBody(updates),
            timeoutMs: 20000,
        }));
    }

    static async getLlmTemplates() {
        const data = await this.get("/api/llm/templates");
        return data.items || [];
    }

    static async getLlmTemplate(providerId) {
        const data = await this.get(`/api/llm/templates/${encodeURIComponent(providerId)}`);
        return data.item || null;
    }

    static async validateLlmDraft(payload) {
        const data = await this.post("/api/llm/drafts/validate", payload || {});
        return data.result || null;
    }

    static async probeLlmDraft(payload) {
        const data = await this.post("/api/llm/drafts/probe", payload || {});
        return data.result || null;
    }

    static async listLlmConfigs() {
        const data = await this.get("/api/llm/configs");
        return data.items || [];
    }

    static async getLlmConfig(configId, { includeSecrets = false } = {}) {
        const data = await this.get(`/api/llm/configs/${encodeURIComponent(configId)}`, { include_secrets: includeSecrets });
        return data.item || null;
    }

    static async createLlmConfig(payload) {
        const data = await this.post("/api/llm/configs", payload || {});
        return data.item || null;
    }

    static async updateLlmConfig(configId, payload) {
        const data = await this.put(`/api/llm/configs/${encodeURIComponent(configId)}`, payload || {});
        return data.item || null;
    }

    static async deleteLlmConfig(configId) {
        return this.delete(`/api/llm/configs/${encodeURIComponent(configId)}`);
    }

    static async listLlmBindings() {
        const data = await this.get("/api/llm/bindings");
        return {
            items: data.items || [],
            routes: data.routes || {},
            roleIterations: data.roleIterations || data.role_iterations || {},
            roleConcurrency: data.roleConcurrency || data.role_concurrency || {},
        };
    }

    static async createLlmBinding(payload) {
        const data = await this.post("/api/llm/bindings", payload || {});
        return data.item || null;
    }

    static async updateLlmBinding(modelKey, payload) {
        const data = await this.put(`/api/llm/bindings/${encodeURIComponent(modelKey)}`, payload || {});
        return data.item || null;
    }

    static async enableLlmBinding(modelKey) {
        const data = await this.post(`/api/llm/bindings/${encodeURIComponent(modelKey)}/enable`);
        return data.item || null;
    }

    static async disableLlmBinding(modelKey) {
        const data = await this.post(`/api/llm/bindings/${encodeURIComponent(modelKey)}/disable`);
        return data.item || null;
    }

    static async deleteLlmBinding(modelKey) {
        return this.delete(`/api/llm/bindings/${encodeURIComponent(modelKey)}`);
    }

    static async getLlmRoutes() {
        const data = await this.get("/api/llm/routes");
        return {
            routes: data.routes || {},
            roleIterations: data.roleIterations || data.role_iterations || {},
            roleConcurrency: data.roleConcurrency || data.role_concurrency || {},
        };
    }

    static async updateLlmRoute(scope, payload) {
        const data = await this._request("PUT", `/api/llm/routes/${encodeURIComponent(scope)}`, {
            body: this._toRoleRouteBody(payload),
            timeoutMs: 20000,
        });
        return {
            routes: data.routes || {},
            roleIterations: data.roleIterations || data.role_iterations || {},
            roleConcurrency: data.roleConcurrency || data.role_concurrency || {},
        };
    }

    static async updateLlmRoutes(updates) {
        const data = await this._request("PUT", "/api/llm/routes", {
            body: this._toRoleRouteUpdatesBody(updates),
            timeoutMs: 20000,
        });
        return {
            routes: data.routes || {},
            roleIterations: data.roleIterations || data.role_iterations || {},
            roleConcurrency: data.roleConcurrency || data.role_concurrency || {},
        };
    }

    static async getLlmMemoryModels() {
        const data = await this.get("/api/llm/memory");
        return data.item || null;
    }

    static async updateLlmMemoryModels(payload) {
        const data = await this.put("/api/llm/memory", payload || {});
        return data.item || null;
    }

    static async runLlmMigration() {
        return this.post("/api/llm/migrate", {});
    }

    static async getSkills(offset = 0, limit = 200) {
        const data = await this._request("GET", "/api/resources/skills", {
            params: { offset, limit },
            requestKey: `resources:skills:${offset}:${limit}`,
        });
        return data.items || [];
    }

    static async getSkill(skillId) {
        const data = await this.get(`/api/resources/skills/${skillId}`);
        return data.item || data.skill || null;
    }

    static async getSkillFiles(skillId) {
        const data = await this.get(`/api/resources/skills/${skillId}/files`);
        return data.items || [];
    }

    static async getSkillFile(skillId, fileKey) {
        return this.get(`/api/resources/skills/${skillId}/files/${fileKey}`);
    }

    static async saveSkillFile(skillId, fileKey, content) {
        return this.put(`/api/resources/skills/${skillId}/files/${fileKey}`, { content }, { session_id: this.getActiveSessionId() });
    }

    static async updateSkillPolicy(skillId, payload) {
        return this.put(`/api/resources/skills/${skillId}/policy`, payload, { session_id: this.getActiveSessionId() });
    }

    static async enableSkill(skillId) {
        return this.post(`/api/resources/skills/${skillId}/enable`, {}, { session_id: this.getActiveSessionId() });
    }

    static async disableSkill(skillId) {
        return this.post(`/api/resources/skills/${skillId}/disable`, {}, { session_id: this.getActiveSessionId() });
    }

    static async deleteSkill(skillId) {
        return this.delete(`/api/resources/skills/${skillId}`, { session_id: this.getActiveSessionId() });
    }

    static async getTools(offset = 0, limit = 200) {
        const data = await this._request("GET", "/api/resources/tools", {
            params: { offset, limit },
            requestKey: `resources:tools:${offset}:${limit}`,
        });
        return data.items || [];
    }

    static async getTool(toolId) {
        const data = await this.get(`/api/resources/tools/${toolId}`);
        return data.item || data.tool || null;
    }

    static async getToolSkill(toolId) {
        return this.get(`/api/resources/tools/${toolId}/toolskill`);
    }

    static async updateToolPolicy(toolId, payload) {
        return this.put(`/api/resources/tools/${toolId}/policy`, payload, { session_id: this.getActiveSessionId() });
    }

    static async enableTool(toolId) {
        return this.post(`/api/resources/tools/${toolId}/enable`, {}, { session_id: this.getActiveSessionId() });
    }

    static async disableTool(toolId) {
        return this.post(`/api/resources/tools/${toolId}/disable`, {}, { session_id: this.getActiveSessionId() });
    }

    static async deleteTool(toolId) {
        return this.delete(`/api/resources/tools/${toolId}`, { session_id: this.getActiveSessionId() });
    }

    static async reloadResources() {
        return this.post("/api/resources/reload", {}, { session_id: this.getActiveSessionId() });
    }

    static async getChinaChannels() {
        return this.get("/api/china-bridge/channels");
    }

    static async getChinaChannel(channelId) {
        const data = await this.get(`/api/china-bridge/channels/${encodeURIComponent(channelId)}`);
        return data.item || null;
    }

    static async updateChinaChannel(channelId, payload) {
        return this.put(`/api/china-bridge/channels/${encodeURIComponent(channelId)}`, payload || {});
    }

    static async testChinaChannel(channelId, payload = {}) {
        return this.post(`/api/china-bridge/channels/${encodeURIComponent(channelId)}/test`, payload || {});
    }
}

window.ApiClient = ApiClient;
