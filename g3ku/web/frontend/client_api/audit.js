"use strict";

// 域模块：只声明端点、请求体与响应归一；实际发请求统一走 ApiCore。
(function () {
    const domains = (window.ApiDomains = window.ApiDomains || {});
    domains.audit = {
    async getAuditEvents({ limit = 50, offset = 0, level = "", subsystem = "", since = "" } = {}) {
        const params = { limit, offset };
        if (level) params.level = level;
        if (subsystem) params.subsystem = subsystem;
        if (since) params.since = since;
        const data = await this._request("GET", "/api/audit/events", {
            params,
            requestKey: `audit:events:${offset}:${limit}:${level || "-"}:${subsystem || "-"}:${since || "-"}`,
        });
        return {
            items: data.items || [],
            total: data.total || 0,
            hasMore: Boolean(data.has_more ?? data.hasMore),
        };
    }
    };
})();
