"use strict";

// 域模块：只声明端点、请求体与响应归一；实际发请求统一走 ApiCore。
(function () {
    const domains = (window.ApiDomains = window.ApiDomains || {});
    domains.external = {
    async getExternalApiSettings() {
        return this.get("/api/external-api/settings");
    },

    async updateExternalApiSettings(payload) {
        return this.put("/api/external-api/settings", payload || {});
    },

    async createExternalApiToken(payload) {
        return this.post("/api/external-api/tokens", payload || {});
    },

    async updateExternalApiToken(bridgeId, payload) {
        return this.patch(`/api/external-api/tokens/${encodeURIComponent(bridgeId)}`, payload || {});
    },

    async deleteExternalApiToken(bridgeId) {
        return this.delete(`/api/external-api/tokens/${encodeURIComponent(bridgeId)}`);
    },

    async getQqBotSettings() {
        return this.get("/api/qq-bot/settings");
    },

    async updateQqBotSettings(payload) {
        return this.put("/api/qq-bot/settings", payload || {});
    },

    async getQqGroupSettings() {
        return this.get("/api/qq-bot/group-settings");
    },

    async updateQqGroupSettings(payload) {
        return this.put("/api/qq-bot/group-settings", payload || {});
    },

    async createQqMasterBinding(payload) {
        return this.post("/api/qq-bot/master-binding", payload || {});
    },

    async getQqMasterBindingStatus(appId) {
        return this.get(`/api/qq-bot/master-binding/status?app_id=${encodeURIComponent(String(appId || ""))}`);
    },

    async unregisterQqMaster(payload) {
        return this.post("/api/qq-bot/unregister-master", payload || {});
    },

    async getQqGroupHistoryUsage(sessionId) {
        return this.get(`/api/qq-bot/history/usage?session_id=${encodeURIComponent(String(sessionId || ""))}`);
    },

    async clearQqGroupHistory(sessionId) {
        // 群文件上限按 2GB 配，删媒体目录的时长由磁盘决定。客户端 10s 超时只会把
        // 已经删成功的操作报成 "Request timeout"，所以这里不设超时。
        return this._request("POST", "/api/qq-bot/history/clear", {
            body: { session_id: String(sessionId || ""), confirm: true },
            timeoutMs: 0,
        });
    }
    };
})();
