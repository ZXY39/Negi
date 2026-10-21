"use strict";

// 域模块：只声明端点、请求体与响应归一；实际发请求统一走 ApiCore。
(function () {
    const domains = (window.ApiDomains = window.ApiDomains || {});
    domains.memory = {
    async getMemoryQueue({ limit = 20, offset = 0 } = {}) {
        const data = await this._request("GET", "/api/memory/queue", {
            params: { limit, offset },
            requestKey: `memory:queue:${offset}:${limit}`,
        });
        return {
            items: data.items || [],
            total: data.total || 0,
            hasMore: Boolean(data.has_more ?? data.hasMore),
        };
    },

    async getMemoryProcessed({ limit = 20, offset = 0 } = {}) {
        const data = await this._request("GET", "/api/memory/processed", {
            params: { limit, offset },
            requestKey: `memory:processed:${offset}:${limit}`,
        });
        return {
            items: data.items || [],
            total: data.total || 0,
            hasMore: Boolean(data.has_more ?? data.hasMore),
        };
    },

    async listMemoryIndexNotes() {
        const data = await this._request("GET", "/api/memory/index-notes", {
            requestKey: "memory:index-notes",
        });
        return {
            items: data.items || [],
            total: data.total || 0,
        };
    },

    async getMemoryNote(ref) {
        const data = await this._request("GET", `/api/memory/notes/${encodeURIComponent(ref)}`, {
            requestKey: `memory:note:${String(ref || "").trim()}`,
        });
        return data.item || null;
    },

    async getCurrentMemories() {
        const data = await this._request("GET", "/api/memory/current", {
            requestKey: "memory:current",
        });
        return {
            items: data.items || [],
            total: data.total || 0,
            mutationsEnabled: Boolean(data.mutations_enabled ?? data.mutationsEnabled),
        };
    },

    async updateCurrentMemory(memoryId, { memoryBody, minimalMemory = null, reason = "manual-ui" } = {}) {
        return this._request("POST", "/api/memory/current/update", {
            body: {
                memory_id: memoryId,
                memory_body: memoryBody,
                minimal_memory: minimalMemory,
                reason,
            },
        });
    },

    async deleteCurrentMemories(memoryIds, reason = "manual-ui") {
        return this._request("POST", "/api/memory/current/delete", {
            body: {
                memory_ids: Array.isArray(memoryIds) ? memoryIds : [memoryIds],
                reason,
            },
        });
    },

    async uncompressCurrentMemory(memoryId, reason = "manual-ui") {
        return this._request("POST", "/api/memory/current/uncompress", {
            body: { memory_id: memoryId, reason },
        });
    },

    async updateMemoryNote(ref, body, reason = "manual-ui") {
        return this._request("POST", `/api/memory/notes/${encodeURIComponent(ref)}/update`, {
            body: { body, reason },
        });
    },

    async getMemoryFailed({ limit = 20, offset = 0 } = {}) {
        const data = await this._request("GET", "/api/memory/failed", {
            params: { limit, offset },
            requestKey: `memory:failed:${offset}:${limit}`,
        });
        return {
            items: data.items || [],
            total: data.total || 0,
            hasMore: Boolean(data.has_more ?? data.hasMore),
            mutationsEnabled: Boolean(data.mutations_enabled ?? data.mutationsEnabled),
        };
    },

    // 记忆正文的按需一屏：三面（failed / processed / queue）同一路径形状，服务端共用一个分页实现。
    // 列表口只投首屏，整份载荷不进浏览器。
    async getMemoryPayload({ kind = "", key = "", requestId = "", startChar = 1 } = {}) {
        return this.get(`/api/memory/${encodeURIComponent(kind)}/${encodeURIComponent(key)}/payload`, {
            request_id: requestId,
            start_char: startChar,
        });
    },

    // 重试 / 放弃会把整本 ops.jsonl 台账读一遍再全量重写（当前 13.5 MB），
    // 默认 10s 会在服务端其实已经改完时先放弃，界面报「失败」而记录已经没了。
    async retryMemoryFailed(failedId, reason = "manual") {
        return this._request("POST", `/api/memory/failed/${encodeURIComponent(failedId)}/retry`, {
            body: { reason },
            timeoutMs: 30000,
        });
    },

    async discardMemoryFailed(failedId, reason = "manual") {
        return this._request("POST", `/api/memory/failed/${encodeURIComponent(failedId)}/discard`, {
            body: { reason },
            timeoutMs: 30000,
        });
    }
    };
})();
