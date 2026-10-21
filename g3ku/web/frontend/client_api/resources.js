"use strict";

// 域模块：只声明端点、请求体与响应归一；实际发请求统一走 ApiCore。
(function () {
    const domains = (window.ApiDomains = window.ApiDomains || {});
    domains.resources = {
    async getSkills(offset = 0, limit = 200) {
        const data = await this._request("GET", "/api/resources/skills", {
            params: { offset, limit },
            requestKey: `resources:skills:${offset}:${limit}`,
        });
        return data.items || [];
    },

    async getSkill(skillId) {
        const data = await this.get(`/api/resources/skills/${skillId}`);
        return data.item || data.skill || null;
    },

    async getSkillFiles(skillId) {
        const data = await this.get(`/api/resources/skills/${skillId}/files`);
        return data.items || [];
    },

    async getSkillFile(skillId, fileKey) {
        return this.get(`/api/resources/skills/${skillId}/files/${fileKey}`);
    },

    async saveSkillFile(skillId, fileKey, content) {
        return this.put(`/api/resources/skills/${skillId}/files/${fileKey}`, { content }, { session_id: this.getActiveSessionId() });
    },

    async updateSkillPolicy(skillId, payload) {
        return this.put(`/api/resources/skills/${skillId}/policy`, payload, { session_id: this.getActiveSessionId() });
    },

    async enableSkill(skillId) {
        return this.post(`/api/resources/skills/${skillId}/enable`, {}, { session_id: this.getActiveSessionId() });
    },

    async disableSkill(skillId) {
        return this.post(`/api/resources/skills/${skillId}/disable`, {}, { session_id: this.getActiveSessionId() });
    },

    // 删除没有客户端超时：服务端要先 rmtree 目录再重建资源树，耗时不可预估。
    // 调用方据此把"删除中"做成常驻提示，直到请求真的返回（成功或失败）。
    async deleteSkill(skillId, { timeoutMs = 0 } = {}) {
        return this._request("DELETE", `/api/resources/skills/${skillId}`, {
            params: { session_id: this.getActiveSessionId() },
            timeoutMs,
        });
    },

    async getTools(offset = 0, limit = 200) {
        const data = await this._request("GET", "/api/resources/tools", {
            params: { offset, limit },
            requestKey: `resources:tools:${offset}:${limit}`,
        });
        return data.items || [];
    },

    async getToolGovernanceMode() {
        const data = await this.get("/api/resources/tools/governance-mode");
        return data.item || null;
    },

    async updateToolGovernanceMode(enabled) {
        const data = await this.put("/api/resources/tools/governance-mode", {
            enabled: !!enabled,
        }, { session_id: this.getActiveSessionId() });
        return data.item || null;
    },

    async getExecCommandWhitelist() {
        const data = await this.get("/api/resources/tools/exec-command-whitelist");
        return data || {};
    },

    async addExecCommandWhitelistEntry(payload) {
        const data = await this.post("/api/resources/tools/exec-command-whitelist", payload || {});
        return data.item || null;
    },

    async removeExecCommandWhitelistEntry(payload) {
        const data = await this.post("/api/resources/tools/exec-command-whitelist/delete", payload || {});
        return data.removed ?? false;
    },

    async updateExecApprovalWait(seconds) {
        const data = await this.put("/api/resources/tools/exec-approval-wait", { seconds });
        return data?.approval_wait_seconds ?? null;
    },

    async listExecApprovals(params = {}) {
        const data = await this.get("/api/resources/tools/exec-approvals", params);
        return data || {};
    },

    async decideExecApproval(approvalId, payload) {
        const data = await this.post(
            `/api/resources/tools/exec-approvals/${encodeURIComponent(String(approvalId || "").trim())}/decision`,
            payload || {},
        );
        return data.item || null;
    },

    async getTool(toolId) {
        const data = await this.get(`/api/resources/tools/${toolId}`);
        return data.item || data.tool || null;
    },

    async getToolSkill(toolId) {
        return this.get(`/api/resources/tools/${toolId}/toolskill`);
    },

    async updateToolPolicy(toolId, payload) {
        return this.put(`/api/resources/tools/${toolId}/policy`, payload, { session_id: this.getActiveSessionId() });
    },

    async enableTool(toolId) {
        return this.post(`/api/resources/tools/${toolId}/enable`, {}, { session_id: this.getActiveSessionId() });
    },

    async disableTool(toolId) {
        return this.post(`/api/resources/tools/${toolId}/disable`, {}, { session_id: this.getActiveSessionId() });
    },

    // 同 deleteSkill：不设客户端超时，删除时长由服务端磁盘决定。
    async deleteTool(toolId, { timeoutMs = 0 } = {}) {
        return this._request("DELETE", `/api/resources/tools/${toolId}`, {
            params: { session_id: this.getActiveSessionId() },
            timeoutMs,
        });
    },

    async reloadResources() {
        return this.post("/api/resources/reload", {}, { session_id: this.getActiveSessionId() });
    }
    };
})();
