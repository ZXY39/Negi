"use strict";

// 域模块：只声明端点、请求体与响应归一；实际发请求统一走 ApiCore。
(function () {
    const domains = (window.ApiDomains = window.ApiDomains || {});
    domains.ceo = {
    async listCeoSessions() {
        return this._request("GET", "/api/ceo/sessions", { requestKey: "ceo:sessions" });
    },

    async createCeoSession(payload = {}) {
        return this.post("/api/ceo/sessions", payload || {});
    },

    async renameCeoSession(sessionId, payload = {}) {
        return this._request("PATCH", `/api/ceo/sessions/${encodeURIComponent(sessionId)}`, { body: payload || {} });
    },

    async truncateCeoSession(sessionId, payload = {}) {
        // 编辑重发第一步：截断转录并重建连续性状态。大会话的全量重写 +
        // sidecar 重建可能超过默认 10s，与 bulk-delete 同档放宽到 30s。
        return this._request("POST", `/api/ceo/sessions/${encodeURIComponent(sessionId)}/truncate`, {
            body: payload || {},
            timeoutMs: 30000,
        });
    },

    async forkCeoSession(sessionId, payload = {}) {
        // Fork 会话：复制前缀 + 附件文件 + 连续性 sidecar 到新会话。
        return this._request("POST", `/api/ceo/sessions/${encodeURIComponent(sessionId)}/fork`, {
            body: payload || {},
            timeoutMs: 30000,
        });
    },

    async withdrawCeoQueuedFollowUp(sessionId, payload = {}) {
        // 撤回一条已被 runtime 受理的待发送补充：内存队列与转录 pending 行一起删。
        return this._request("POST", `/api/ceo/sessions/${encodeURIComponent(sessionId)}/queued-follow-ups/withdraw`, {
            body: payload || {},
        });
    },

    async getCeoToolArguments(sessionId, toolCallId) {
        // 入参被转录投影清空时按 tool_call_id 回取全量：账本里的原文不封顶，进帧的才封顶。
        return this.get(`/api/ceo/sessions/${encodeURIComponent(sessionId)}/tool-arguments`, {
            tool_call_id: toolCallId,
        });
    },

    async getMainRuntimeTaskDefaults() {
        return this.get("/api/main-runtime/settings");
    },

    async updateMainRuntimeTaskDefaults(payload = {}) {
        return this.put("/api/main-runtime/settings", payload || {});
    },

    async getCeoTaskDefaults(sessionId) {
        void sessionId;
        return this.getMainRuntimeTaskDefaults();
    },

    async updateCeoTaskDefaults(sessionId, payload = {}) {
        void sessionId;
        return this.updateMainRuntimeTaskDefaults(payload);
    },

    async activateCeoSession(sessionId) {
        return this.post(`/api/ceo/sessions/${encodeURIComponent(sessionId)}/activate`, {});
    },

    async getCeoPendingInterrupts(sessionId) {
        const data = await this.get(`/api/ceo/sessions/${encodeURIComponent(sessionId)}/pending-interrupts`);
        return Array.isArray(data.items) ? data.items : [];
    },

    async estimateCeoComposerPreflight(sessionId, payload) {
        const data = await this._request("POST", `/api/ceo/sessions/${encodeURIComponent(sessionId)}/composer-preflight`, {
            body: payload || {},
            timeoutMs: 20000,
        });
        return data.item || null;
    },

    async getCeoSessionModelSelection(sessionId) {
        return this.get(`/api/ceo/sessions/${encodeURIComponent(sessionId)}/model-selection`);
    },

    async startCeoContextCompression(sessionId) {
        return this._request("POST", `/api/ceo/sessions/${encodeURIComponent(sessionId)}/compress-context`, {
            body: {},
            timeoutMs: 20000,
        });
    },

    async getCeoContextCompression(sessionId) {
        // 与 start 同档：大会话收尾要整份重写转录，事件循环被占住时 10 秒默认值会误判成失败。
        return this._request("GET", `/api/ceo/sessions/${encodeURIComponent(sessionId)}/compress-context`, {
            timeoutMs: 20000,
        });
    },

    async cancelCeoContextCompression(sessionId) {
        return this._request("POST", `/api/ceo/sessions/${encodeURIComponent(sessionId)}/compress-context/cancel`, {
            body: {},
            timeoutMs: 20000,
        });
    },

    async updateCeoSessionModelSelection(sessionId, payload = {}) {
        return this._request("PATCH", `/api/ceo/sessions/${encodeURIComponent(sessionId)}/model-selection`, {
            body: payload || {},
        });
    },

    async getCeoSessionDeleteCheck(sessionId) {
        return this.get(`/api/ceo/sessions/${encodeURIComponent(sessionId)}/delete-check`);
    },

    async getCeoSessionsBulkDeleteCheck(sessionIds = []) {
        const data = await this._request("POST", "/api/ceo/sessions/delete-check", {
            body: { session_ids: Array.isArray(sessionIds) ? sessionIds : [] },
            timeoutMs: 30000,
        });
        return data || {};
    },

    async deleteCeoSession(sessionId, payload = {}) {
        return this.delete(`/api/ceo/sessions/${encodeURIComponent(sessionId)}`, { body: payload || {} });
    },

    async bulkDeleteCeoSessions(sessionIds = [], payload = {}) {
        const data = await this._request("POST", "/api/ceo/sessions/bulk-delete", {
            body: {
                session_ids: Array.isArray(sessionIds) ? sessionIds : [],
                ...(payload && typeof payload === "object" && !Array.isArray(payload) ? payload : {}),
            },
            timeoutMs: 30000,
        });
        return data || {};
    },

    async uploadCeoFiles(files = [], sessionId = this.getActiveSessionId()) {
        const formData = new FormData();
        [...files].forEach((file) => formData.append("files", file));
        const data = await this._request("POST", "/api/ceo/uploads", {
            params: { session_id: sessionId },
            body: formData,
        });
        return data.items || [];
    },

    // 转写是本机 CPU 推理，实测 2 核机器上 23 秒中文音频要 9.5 秒，默认 10 秒超时
    // 会正好掐在成功前一刻；排队预算另有 60 秒，所以这里放到 150 秒。
    async transcribeCeoVoice(blob, filename = "voice.wav") {
        const formData = new FormData();
        formData.append("file", new File([blob], filename, { type: blob.type || "audio/wav" }));
        return await this._request("POST", "/api/ceo/transcribe", {
            body: formData,
            timeoutMs: 150000,
            requestKey: "ceo-voice-transcribe",
        });
    },

    // 就绪矩阵 + 下载进度都在 status 里，所以轮询只需要这一个口；不带 requestKey
    // （那是"取消上一个同名请求"的语义，会把轮询自己掐掉）。
    async getCeoVoiceStatus() {
        return await this._request("GET", "/api/ceo/voice/status", { timeoutMs: 8000 });
    },

    async prepareCeoVoice() {
        return await this._request("POST", "/api/ceo/voice/prepare", { timeoutMs: 15000 });
    }
    };
})();
