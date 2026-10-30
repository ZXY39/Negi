"use strict";

// 域模块：只声明端点、请求体与响应归一；实际发请求统一走 ApiCore。
(function () {
    const domains = (window.ApiDomains = window.ApiDomains || {});
    domains.models = {
    async getOrgGraphModels() {
        const data = await this.get("/api/models");
        return {
            catalog: Array.isArray(data.items) ? data.items : [],
            items: Array.isArray(data.items) ? data.items.map((item) => item.key) : [],
            // roles 是候选展开视图（含组内成员），routeEntries 才是 fallback 顺序。
            roles: data.roles || {},
            routeEntries: data.route_entries || {},
            loadBalanceGroups: data.load_balance_groups || data.loadBalanceGroups || {},
            roleIterations: data.roleIterations || data.role_iterations || {},
            roleConcurrency: data.roleConcurrency || data.role_concurrency || {},
            defaults: {},
        };
    },

    _toManagedModelPayload(payload = {}) {
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
            ["context_window_tokens", "contextWindowTokens"],
        ];
        mappedFields.forEach(([snakeKey, camelKey]) => {
            const value = pick(snakeKey, camelKey);
            if (value !== undefined) body[snakeKey] = value;
        });
        return body;
    },

    async _refreshModelsAfter(requestPromise) {
        await requestPromise;
        return this.getOrgGraphModels();
    },

    async createManagedModel(payload) {
        return this._refreshModelsAfter(this._request("POST", "/api/models", {
            body: this._toManagedModelPayload(payload),
            timeoutMs: MODEL_SAVE_TIMEOUT_MS,
        }));
    },

    async updateManagedModel(modelKey, payload) {
        return this._refreshModelsAfter(this._request("PUT", `/api/models/${modelKey}`, {
            body: this._toManagedModelPayload(payload),
            timeoutMs: MODEL_SAVE_TIMEOUT_MS,
        }));
    },

    async enableManagedModel(modelKey) {
        return this._refreshModelsAfter(this.post(`/api/models/${modelKey}/enable`));
    },

    async disableManagedModel(modelKey) {
        return this._refreshModelsAfter(this.post(`/api/models/${modelKey}/disable`));
    },

    async deleteManagedModel(modelKey) {
        return this._refreshModelsAfter(this.delete(`/api/models/${modelKey}`));
    },

    _toRoleRouteBody(payload = {}) {
        const source = Array.isArray(payload) ? { modelKeys: payload } : (payload && typeof payload === "object" ? payload : {});
        const has = (key) => Object.prototype.hasOwnProperty.call(source, key);
        const modelKeys = Array.isArray(source.modelKeys)
            ? source.modelKeys
            : Array.isArray(source.model_keys)
                ? source.model_keys
                : undefined;
        const body = {};
        // 显式 route 与 legacy 扁平链不能同时给：后端以 route_entries 为准并走严格校验，
        // 这里也不两套都发，避免"保存了什么"与"运行的是什么"分叉。
        const routeEntries = Array.isArray(source.routeEntries)
            ? source.routeEntries
            : Array.isArray(source.route_entries)
                ? source.route_entries
                : undefined;
        const groups = source.loadBalanceGroups && typeof source.loadBalanceGroups === "object"
            ? source.loadBalanceGroups
            : source.load_balance_groups && typeof source.load_balance_groups === "object"
                ? source.load_balance_groups
                : undefined;
        if (routeEntries !== undefined) {
            body.route_entries = routeEntries;
            body.routeEntries = routeEntries;
        } else if (modelKeys !== undefined) {
            body.model_keys = modelKeys;
            body.modelKeys = modelKeys;
        }
        if (groups !== undefined) {
            body.load_balance_groups = groups;
            body.loadBalanceGroups = groups;
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
    },

    _toRoleRouteUpdatesBody(updates = {}) {
        const entries = Object.entries(updates && typeof updates === "object" ? updates : {});
        return {
            updates: Object.fromEntries(
                entries.map(([scope, payload]) => [String(scope || "").trim(), this._toRoleRouteBody(payload)])
            ),
        };
    },

    async updateModelRoleChains(updates) {
        return this._refreshModelsAfter(this._request("PUT", "/api/models/routes/batch", {
            body: this._toRoleRouteUpdatesBody(updates),
            timeoutMs: 20000,
        }));
    },

    async getLlmTemplates() {
        const data = await this.get("/api/llm/templates");
        return data.items || [];
    },

    async getLlmTemplate(providerId) {
        const data = await this.get(`/api/llm/templates/${encodeURIComponent(providerId)}`);
        return data.item || null;
    },

    async validateLlmDraft(payload) {
        const data = await this.post("/api/llm/drafts/validate", payload || {});
        return data.result || null;
    },

    async probeLlmDraft(payload) {
        const data = await this.post("/api/llm/drafts/probe", payload || {});
        return data.result || null;
    },

    async probeLlmDraftMaxConcurrency(payload) {
        const data = await this._request("POST", "/api/llm/drafts/probe-max-concurrency", {
            body: payload || {},
            timeoutMs: 120000,
        });
        return data.result || null;
    },

    async listLlmDraftModels(payload) {
        const data = await this._request("POST", "/api/llm/drafts/models", {
            body: payload || {},
            timeoutMs: 30000,
        });
        return data.result || null;
    },

    async listLlmConfigs() {
        const data = await this.get("/api/llm/configs");
        return data.items || [];
    },

    async getLlmConfig(configId, { includeSecrets = false } = {}) {
        const data = await this.get(`/api/llm/configs/${encodeURIComponent(configId)}`, { include_secrets: includeSecrets });
        return data.item || null;
    },

    async revealLlmConfigApiKey(configId, password = "") {
        const body = password ? { password } : {};
        const data = await this._request("POST", `/api/llm/configs/${encodeURIComponent(configId)}/reveal`, {
            body,
            timeoutMs: 15000,
        });
        return {
            item: data.item || null,
            expiresInSeconds: Number(data.reveal_expires_in_seconds ?? data.revealExpiresInSeconds ?? 0) || 0,
        };
    },

    async createLlmConfig(payload) {
        const data = await this._request("POST", "/api/llm/configs", {
            body: payload || {},
            timeoutMs: MODEL_SAVE_TIMEOUT_MS,
        });
        return data.item || null;
    },

    async updateLlmConfig(configId, payload) {
        const data = await this._request("PUT", `/api/llm/configs/${encodeURIComponent(configId)}`, {
            body: payload || {},
            timeoutMs: MODEL_SAVE_TIMEOUT_MS,
        });
        return {
            item: data.item || null,
            runtimeRefresh: data.runtime_refresh || data.runtimeRefresh || null,
        };
    },

    async deleteLlmConfig(configId) {
        return this.delete(`/api/llm/configs/${encodeURIComponent(configId)}`);
    },

    async listLlmBindings() {
        const data = await this.get("/api/llm/bindings");
        return {
            items: data.items || [],
            routes: data.routes || {},
            // routes 是候选展开视图；链的真正顺序与组定义只在这两个字段里。
            routeEntries: data.route_entries || data.routeEntries || {},
            loadBalanceGroups: data.load_balance_groups || data.loadBalanceGroups || {},
            roleIterations: data.roleIterations || data.role_iterations || {},
            roleConcurrency: data.roleConcurrency || data.role_concurrency || {},
        };
    },

    async createLlmBinding(payload) {
        const data = await this._request("POST", "/api/llm/bindings", {
            body: payload || {},
            timeoutMs: MODEL_SAVE_TIMEOUT_MS,
        });
        return {
            item: data.item || null,
            runtimeRefresh: data.runtime_refresh || data.runtimeRefresh || null,
        };
    },

    async updateLlmBinding(modelKey, payload) {
        const data = await this._request("PUT", `/api/llm/bindings/${encodeURIComponent(modelKey)}`, {
            body: payload || {},
            timeoutMs: MODEL_SAVE_TIMEOUT_MS,
        });
        return {
            item: data.item || null,
            runtimeRefresh: data.runtime_refresh || data.runtimeRefresh || null,
        };
    },

    async renameLlmBinding(modelKey, newKey) {
        const data = await this._request("POST", `/api/llm/bindings/${encodeURIComponent(modelKey)}/rename`, {
            body: { key: newKey },
            timeoutMs: MODEL_SAVE_TIMEOUT_MS,
        });
        return {
            item: data.item || null,
            runtimeRefresh: data.runtime_refresh || data.runtimeRefresh || null,
        };
    },

    async getRuntimeRefreshStatus(commandId) {
        const data = await this.get(`/api/runtime-refresh/${encodeURIComponent(commandId)}`);
        return data.item || null;
    },

    async enableLlmBinding(modelKey) {
        const data = await this.post(`/api/llm/bindings/${encodeURIComponent(modelKey)}/enable`);
        return {
            item: data.item || null,
            runtimeRefresh: data.runtime_refresh || data.runtimeRefresh || null,
        };
    },

    async disableLlmBinding(modelKey) {
        const data = await this.post(`/api/llm/bindings/${encodeURIComponent(modelKey)}/disable`);
        return {
            item: data.item || null,
            runtimeRefresh: data.runtime_refresh || data.runtimeRefresh || null,
        };
    },

    async deleteLlmBinding(modelKey) {
        const data = await this.delete(`/api/llm/bindings/${encodeURIComponent(modelKey)}`);
        return {
            ...data,
            runtimeRefresh: data.runtime_refresh || data.runtimeRefresh || null,
        };
    },

    async getLlmRoutes() {
        const data = await this.get("/api/llm/routes");
        return {
            routes: data.routes || {},
            roleIterations: data.roleIterations || data.role_iterations || {},
            roleConcurrency: data.roleConcurrency || data.role_concurrency || {},
        };
    },

    async updateLlmRoute(scope, payload) {
        const data = await this._request("PUT", `/api/llm/routes/${encodeURIComponent(scope)}`, {
            body: this._toRoleRouteBody(payload),
            timeoutMs: 20000,
        });
        return {
            routes: data.routes || {},
            roleIterations: data.roleIterations || data.role_iterations || {},
            roleConcurrency: data.roleConcurrency || data.role_concurrency || {},
        };
    },

    async updateLlmRoutes(updates) {
        const data = await this._request("PUT", "/api/llm/routes", {
            body: this._toRoleRouteUpdatesBody(updates),
            timeoutMs: 20000,
        });
        return {
            routes: data.routes || {},
            // 保存后仍要认顺序：routes 是候选展开视图，链与组的真相在这两个字段里。
            routeEntries: data.route_entries || data.routeEntries || {},
            loadBalanceGroups: data.load_balance_groups || data.loadBalanceGroups || {},
            roleIterations: data.roleIterations || data.role_iterations || {},
            roleConcurrency: data.roleConcurrency || data.role_concurrency || {},
        };
    },

    async runLlmMigration() {
        return this.post("/api/llm/migrate", {});
    }
    };
})();
