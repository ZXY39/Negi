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

    static getCeoWsUrl() {
        const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
        const host = API_BASE_URL ? new URL(API_BASE_URL).host : window.location.host;
        return `${protocol}//${host}/api/ws/ceo?session_id=${DEFAULT_SESSION_ID}`;
    }

    static getProjectWsUrl(projectId, afterSeq = 0) {
        const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
        const host = API_BASE_URL ? new URL(API_BASE_URL).host : window.location.host;
        return `${protocol}//${host}/api/ws/projects/${projectId}?session_id=${DEFAULT_SESSION_ID}&after_seq=${afterSeq}`;
    }

    static async getProjects(offset = 0, limit = 50) {
        const data = await this.get("/api/projects", { session_id: DEFAULT_SESSION_ID, offset, limit });
        return data.items || [];
    }

    static async getOrgGraphModels() {
        return this.get("/api/models");
    }

    static async createManagedModel(payload) {
        return this.post("/api/models", payload);
    }

    static async updateManagedModel(modelKey, payload) {
        return this.put(`/api/models/${modelKey}`, payload);
    }

    static async enableManagedModel(modelKey) {
        return this.post(`/api/models/${modelKey}/enable`);
    }

    static async disableManagedModel(modelKey) {
        return this.post(`/api/models/${modelKey}/disable`);
    }

    static async updateModelRoleChain(scope, modelKeys) {
        return this.put(`/api/models/roles/${scope}`, { modelKeys });
    }

    static async getNotices(offset = 0, limit = 50) {
        const data = await this.get("/api/notices", { session_id: DEFAULT_SESSION_ID, offset, limit });
        return data.items || [];
    }

    static async pauseProject(projectId) {
        const data = await this.post(`/api/projects/${projectId}/pause`);
        return data.project || null;
    }

    static async resumeProject(projectId) {
        const data = await this.post(`/api/projects/${projectId}/resume`);
        return data.project || null;
    }

    static async getProjectDetails(projectId) {
        return this.get(`/api/projects/${projectId}`);
    }

    static async getProjectTree(projectId) {
        return this.get(`/api/projects/${projectId}/tree`);
    }

    static async getProjectEvents(projectId, afterSeq = 0, limit = 200) {
        const data = await this.get(`/api/projects/${projectId}/events`, { after_seq: afterSeq, limit });
        return data.items || [];
    }

    static async getSkills(offset = 0, limit = 200) {
        const data = await this.get("/api/resources/skills", { offset, limit });
        return data.items || [];
    }

    static async getSkill(skillId) {
        const data = await this.get(`/api/resources/skills/${skillId}`);
        return data.skill || null;
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
        return data.tool || null;
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
