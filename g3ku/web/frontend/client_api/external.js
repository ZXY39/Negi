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

    async previewQqBotOwnerRegistration(payload) {
        return this.post("/api/qq-bot/register-owner/preview", payload || {});
    },

    async registerQqBotOwner(payload) {
        return this.post("/api/qq-bot/register-owner", payload || {});
    }
    };
})();
