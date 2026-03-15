const API_BASE_URL = "";
const DEFAULT_SESSION_ID = "web:shared";

class ApiClient {
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

    static async _request(method, path, { params = {}, body } = {}) {
        const url = this._buildUrl(path, params);
        const response = await fetch(url.toString(), {
            method,
            headers: { "Content-Type": "application/json" },
            body: body === undefined ? undefined : JSON.stringify(body),
        });
        if (!response.ok) {
            const payload = await response.json().catch(() => ({}));
            throw new Error(payload.detail || payload.message || `HTTP ${response.status}`);
        }
        return response.json();
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

    static delete(path, params = {}) {
        return this._request("DELETE", path, { params });
    }

    static getCeoWsUrl() {
        const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
        const host = API_BASE_URL ? new URL(API_BASE_URL).host : window.location.host;
        return `${protocol}//${host}/api/ws/ceo?session_id=${DEFAULT_SESSION_ID}`;
    }

    static getTaskWsUrl(taskId) {
        const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
        const host = API_BASE_URL ? new URL(API_BASE_URL).host : window.location.host;
        return `${protocol}//${host}/api/ws/tasks/${encodeURIComponent(taskId)}?session_id=${DEFAULT_SESSION_ID}`;
    }

    static async getTasks(scope = 1) {
        const data = await this.get("/api/tasks", { session_id: DEFAULT_SESSION_ID, scope });
        return data.items || [];
    }

    static async getTask(taskId, markRead = false) {
        return this.get(`/api/tasks/${taskId}`, { mark_read: markRead });
    }

    static async pauseTask(taskId) {
        const data = await this.post(`/api/tasks/${taskId}/pause`);
        return data.task || null;
    }

    static async resumeTask(taskId) {
        const data = await this.post(`/api/tasks/${taskId}/resume`);
        return data.task || null;
    }

    static async cancelTask(taskId) {
        const data = await this.post(`/api/tasks/${taskId}/cancel`);
        return data.task || null;
    }

    static async getTaskArtifacts(taskId) {
        const data = await this.get(`/api/tasks/${taskId}/artifacts`);
        return data.items || [];
    }

    static async getTaskArtifact(taskId, artifactId) {
        return this.get(`/api/tasks/${taskId}/artifacts/${artifactId}`);
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

    static async updateModelRoleChain(scope, modelKeys) {
        return this._refreshModelsAfter(this.put(`/api/models/roles/${scope}`, { model_keys: modelKeys, modelKeys }));
    }

    static async getSkills(offset = 0, limit = 200) {
        const data = await this.get("/api/resources/skills", { offset, limit });
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
        return this.put(`/api/resources/skills/${skillId}/files/${fileKey}`, { content }, { session_id: DEFAULT_SESSION_ID });
    }

    static async updateSkillPolicy(skillId, payload) {
        return this.put(`/api/resources/skills/${skillId}/policy`, payload, { session_id: DEFAULT_SESSION_ID });
    }

    static async enableSkill(skillId) {
        return this.post(`/api/resources/skills/${skillId}/enable`, {}, { session_id: DEFAULT_SESSION_ID });
    }

    static async disableSkill(skillId) {
        return this.post(`/api/resources/skills/${skillId}/disable`, {}, { session_id: DEFAULT_SESSION_ID });
    }

    static async getTools(offset = 0, limit = 200) {
        const data = await this.get("/api/resources/tools", { offset, limit });
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
        return this.put(`/api/resources/tools/${toolId}/policy`, payload, { session_id: DEFAULT_SESSION_ID });
    }

    static async enableTool(toolId) {
        return this.post(`/api/resources/tools/${toolId}/enable`, {}, { session_id: DEFAULT_SESSION_ID });
    }

    static async disableTool(toolId) {
        return this.post(`/api/resources/tools/${toolId}/disable`, {}, { session_id: DEFAULT_SESSION_ID });
    }

    static async reloadResources() {
        return this.post("/api/resources/reload", {}, { session_id: DEFAULT_SESSION_ID });
    }
}

window.ApiClient = ApiClient;
