"use strict";

// 域模块：只声明端点、请求体与响应归一；实际发请求统一走 ApiCore。
(function () {
    const domains = (window.ApiDomains = window.ApiDomains || {});
    // 作用域 = ""（本地）/ 群作用域键。请求键带上它，切域不会命中上一域的在途请求缓存。
    const scopeParams = (scope) => {
        const value = String(scope || "").trim();
        return value ? { scope: value } : {};
    };
    const scopeKey = (scope) => String(scope || "").trim() || "local";
    domains.memory = {
    async getMemoryScopes() {
        const data = await this._request("GET", "/api/memory/scopes", {
            requestKey: "memory:scopes",
        });
        return { items: data.items || [] };
    },

    async getMemoryQueue({ limit = 20, offset = 0, scope = "" } = {}) {
        const data = await this._request("GET", "/api/memory/queue", {
            params: { limit, offset, ...scopeParams(scope) },
            requestKey: `memory:queue:${scopeKey(scope)}:${offset}:${limit}`,
        });
        return {
            items: data.items || [],
            total: data.total || 0,
            hasMore: Boolean(data.has_more ?? data.hasMore),
        };
    },

    async getMemoryProcessed({ limit = 20, offset = 0, scope = "" } = {}) {
        const data = await this._request("GET", "/api/memory/processed", {
            params: { limit, offset, ...scopeParams(scope) },
            requestKey: `memory:processed:${scopeKey(scope)}:${offset}:${limit}`,
        });
        return {
            items: data.items || [],
            total: data.total || 0,
            hasMore: Boolean(data.has_more ?? data.hasMore),
        };
    },

    async listMemoryIndexNotes(scope = "") {
        const data = await this._request("GET", "/api/memory/index-notes", {
            params: scopeParams(scope),
            requestKey: `memory:index-notes:${scopeKey(scope)}`,
        });
        return {
            items: data.items || [],
            total: data.total || 0,
        };
    },

    async getMemoryNote(ref, scope = "") {
        const data = await this._request("GET", `/api/memory/notes/${encodeURIComponent(ref)}`, {
            params: scopeParams(scope),
            requestKey: `memory:note:${scopeKey(scope)}:${String(ref || "").trim()}`,
        });
        return data.item || null;
    },

    async getCurrentMemories(scope = "") {
        const data = await this._request("GET", "/api/memory/current", {
            params: scopeParams(scope),
            requestKey: `memory:current:${scopeKey(scope)}`,
        });
        return {
            items: data.items || [],
            total: data.total || 0,
            mutationsEnabled: Boolean(data.mutations_enabled ?? data.mutationsEnabled),
        };
    },

    async updateCurrentMemory(memoryId, { memoryBody, minimalMemory = null, reason = "manual-ui", scope = "" } = {}) {
        return this._request("POST", "/api/memory/current/update", {
            params: scopeParams(scope),
            body: {
                memory_id: memoryId,
                memory_body: memoryBody,
                minimal_memory: minimalMemory,
                reason,
            },
        });
    },

    async deleteCurrentMemories(memoryIds, reason = "manual-ui", scope = "") {
        return this._request("POST", "/api/memory/current/delete", {
            params: scopeParams(scope),
            body: {
                memory_ids: Array.isArray(memoryIds) ? memoryIds : [memoryIds],
                reason,
            },
        });
    },

    async uncompressCurrentMemory(memoryId, reason = "manual-ui", scope = "") {
        return this._request("POST", "/api/memory/current/uncompress", {
            params: scopeParams(scope),
            body: { memory_id: memoryId, reason },
        });
    },

    async updateMemoryNote(ref, body, reason = "manual-ui", scope = "") {
        return this._request("POST", `/api/memory/notes/${encodeURIComponent(ref)}/update`, {
            params: scopeParams(scope),
            body: { body, reason },
        });
    },

    async getMemoryFailed({ limit = 20, offset = 0, scope = "" } = {}) {
        const data = await this._request("GET", "/api/memory/failed", {
            params: { limit, offset, ...scopeParams(scope) },
            requestKey: `memory:failed:${scopeKey(scope)}:${offset}:${limit}`,
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
    async getMemoryPayload({ kind = "", key = "", requestId = "", startChar = 1, scope = "" } = {}) {
        return this.get(`/api/memory/${encodeURIComponent(kind)}/${encodeURIComponent(key)}/payload`, {
            request_id: requestId,
            start_char: startChar,
            ...scopeParams(scope),
        });
    },

    // 重试 / 放弃会把整本 ops.jsonl 台账读一遍再全量重写（当前 13.5 MB），
    // 默认 10s 会在服务端其实已经改完时先放弃，界面报「失败」而记录已经没了。
    async retryMemoryFailed(failedId, reason = "manual", scope = "") {
        return this._request("POST", `/api/memory/failed/${encodeURIComponent(failedId)}/retry`, {
            params: scopeParams(scope),
            body: { reason },
            timeoutMs: 30000,
        });
    },

    async discardMemoryFailed(failedId, reason = "manual", scope = "") {
        return this._request("POST", `/api/memory/failed/${encodeURIComponent(failedId)}/discard`, {
            params: scopeParams(scope),
            body: { reason },
            timeoutMs: 30000,
        });
    }
    };
})();
