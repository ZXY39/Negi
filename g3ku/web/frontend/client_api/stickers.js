"use strict";

// 域模块：只声明端点、请求体与响应归一；实际发请求统一走 ApiCore。
// 表情包管理页的写操作都由服务端返回刷新后的整包快照（data.item），
// 调用方直接拿它替换本地状态，不需要写后再 GET 一次。
(function () {
    const domains = (window.ApiDomains = window.ApiDomains || {});
    const packPath = (pack) => `/api/resources/stickers/packs/${encodeURIComponent(pack)}`;
    const categoryPath = (pack, category) =>
        `${packPath(pack)}/categories/${encodeURIComponent(category)}`;

    domains.stickers = {
        async getStickerPacks() {
            const data = await this._request("GET", "/api/resources/stickers/packs", {
                requestKey: "stickers:packs",
            });
            return data.items || [];
        },

        async getStickerPack(pack, perCategory = 12) {
            const data = await this._request("GET", packPath(pack), {
                params: { per_category: perCategory },
                requestKey: `stickers:pack:${pack}`,
            });
            return data.item || null;
        },

        async getStickerCategoryImages(pack, category, offset = 0, limit = 500) {
            const data = await this._request("GET", `${categoryPath(pack, category)}/images`, {
                params: { offset, limit },
                requestKey: `stickers:images:${pack}:${category}`,
            });
            return { items: data.items || [], total: Number(data.total || 0) };
        },

        async updateStickerTheme(pack, theme) {
            return this.put(`${packPath(pack)}/theme`, { theme });
        },

        async updateStickerCategoryDescription(pack, category, description) {
            return this.put(`${categoryPath(pack, category)}/description`, { description });
        },

        async setStickerPackEnabled(pack, enabled) {
            return this.post(`${packPath(pack)}/enabled`, { enabled });
        },

        async setStickerCategoryEnabled(pack, category, enabled) {
            return this.post(`${categoryPath(pack, category)}/enabled`, { enabled });
        },

        async createStickerCategory(pack, key, description = "") {
            return this.post(`${packPath(pack)}/categories`, { key, description });
        },

        async uploadStickerImages(pack, category, files) {
            const form = new FormData();
            for (const file of files || []) form.append("files", file);
            // 不设客户端超时：一次 20 张 × 20 MiB 的上传耗时由磁盘决定。
            return this._request("POST", "/api/resources/stickers/upload", {
                params: { pack, category },
                body: form,
                timeoutMs: 0,
            });
        },

        async deleteStickerImage(pack, category, name) {
            return this.delete(`${categoryPath(pack, category)}/images`, {
                params: { name },
                timeoutMs: 0,
            });
        },

        async deleteStickerCategory(pack, category) {
            return this.delete(categoryPath(pack, category), { timeoutMs: 0 });
        },

        async deleteStickerPack(pack) {
            return this.delete(packPath(pack), { timeoutMs: 0 });
        },

        async getStickerSettings() {
            return this._request("GET", "/api/resources/stickers/settings", {
                requestKey: "stickers:settings",
            });
        },

        async updateStickerSettings(payload) {
            return this.put("/api/resources/stickers/settings", payload);
        },
    };
})();
