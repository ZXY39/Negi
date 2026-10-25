"use strict";

// 域模块：初次引导与只读人设。请求体与归一都在这里，页面模块只读状态。
(function () {
    const domains = (window.ApiDomains = window.ApiDomains || {});
    domains.persona = {
        async getOnboardingStatus() {
            return this.get("/api/onboarding/status");
        },

        async saveOnboardingPersona(description) {
            return this.post("/api/onboarding/persona", { description: String(description || "") });
        },

        async skipOnboardingPersona() {
            return this.post("/api/onboarding/skip-persona", {});
        },

        async completeOnboarding() {
            return this.post("/api/onboarding/complete", {});
        },

        async resetOnboarding() {
            return this.post("/api/onboarding/reset", {});
        },

        async getPersona() {
            return this.get("/api/persona");
        },

        async savePersona(description) {
            return this.post("/api/persona", { description: String(description || "") });
        },

        async uploadPersonaPhotos(files = []) {
            const formData = new FormData();
            [...files].forEach((file) => formData.append("files", file));
            const data = await this._request("POST", "/api/persona/photos", {
                body: formData,
                timeoutMs: 60000,
            });
            return Array.isArray(data?.items) ? data.items : [];
        },
    };
})();
