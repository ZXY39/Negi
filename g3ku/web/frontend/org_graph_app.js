const MODEL_SCOPES = [
    { key: "agent", label: "主 Agent" },
    { key: "ceo", label: "CEO" },
    { key: "execution", label: "执行" },
    { key: "inspection", label: "检验" },
];

const EMPTY_MODEL_ROLES = () => ({ agent: [], ceo: [], execution: [], inspection: [] });
const DEFAULT_MODEL_DEFAULTS = () => ({ ceo: "", execution: "", inspection: "" });

const S = {
    view: "ceo",
    ceoWs: null,
    projectWs: null,
    currentProjectId: null,
    projects: [],
    selectedProjects: new Set(),
    projectBusy: false,
    modelCatalog: {
        items: [],
        catalog: [],
        roles: EMPTY_MODEL_ROLES(),
        defaults: DEFAULT_MODEL_DEFAULTS(),
        loading: false,
        saving: false,
        error: "",
        search: "",
        selectedModelKey: "",
        mode: "view",
        rolesDirty: false,
    },
    tree: null,
    selectedUnitId: null,
    events: [],
    skills: [],
    selectedSkill: null,
    skillFiles: [],
    skillContents: {},
    selectedSkillFile: "",
    skillBusy: false,
    tools: [],
    selectedTool: null,
    toolBusy: false,
};

const U = {
    nav: [...document.querySelectorAll(".nav-item")],
    theme: document.getElementById("theme-toggle"),
    noticeList: document.getElementById("global-notice-list"),
    noticeBadge: document.getElementById("notice-badge"),
    ceoFeed: document.getElementById("ceo-chat-feed"),
    ceoInput: document.getElementById("ceo-input"),
    ceoSend: document.getElementById("ceo-send-btn"),
    viewCeo: document.getElementById("view-ceo"),
    viewProjects: document.getElementById("view-projects-list"),
    viewSkills: document.getElementById("view-skills"),
    viewTools: document.getElementById("view-tools"),
    viewModels: document.getElementById("view-models"),
    viewProjectDetails: document.getElementById("view-project-details"),
    modelHint: document.getElementById("sidebar-model-hint"),
    modelRefresh: document.getElementById("model-refresh-btn"),
    modelCreate: document.getElementById("model-create-btn"),
    modelRolesSave: document.getElementById("model-roles-save-btn"),
    modelRoleEditors: document.getElementById("model-role-editors"),
    modelSearch: document.getElementById("model-search-input"),
    modelList: document.getElementById("model-list"),
    modelDetailEmpty: document.getElementById("model-detail-empty"),
    modelDetail: document.getElementById("model-detail-content"),
    projectGrid: document.getElementById("project-card-grid"),
    projectSummary: document.getElementById("project-selection-summary"),
    selectActive: document.getElementById("project-select-all-active"),
    selectBlocked: document.getElementById("project-select-all-blocked"),
    selectClear: document.getElementById("project-select-clear"),
    pauseBatch: document.getElementById("project-batch-pause"),
    resumeBatch: document.getElementById("project-batch-resume"),
    backToProjects: document.getElementById("back-to-projects"),
    pdTitle: document.getElementById("pd-title"),
    pdStatus: document.getElementById("pd-status"),
    pdSummary: document.getElementById("pd-summary"),
    pdActiveCount: document.getElementById("pd-active-count"),
    tree: document.getElementById("org-tree-container"),
    feed: document.getElementById("project-event-feed"),
    feedTitle: document.getElementById("feed-target-name"),
    detail: document.getElementById("agent-detail-view"),
    adRole: document.getElementById("ad-role"),
    adStatus: document.getElementById("ad-status"),
    adObjective: document.getElementById("ad-objective"),
    adPrompt: document.getElementById("ad-prompt"),
    adResult: document.getElementById("ad-result"),
    adLogs: document.getElementById("ad-logs"),
    closeAgent: document.getElementById("close-agent-btn"),
    skillSearch: document.getElementById("skill-search-input"),
    skillRisk: document.getElementById("skill-risk-filter"),
    skillStatus: document.getElementById("skill-status-filter"),
    skillLegacy: document.getElementById("skill-legacy-filter"),
    skillList: document.getElementById("skill-list"),
    skillEmpty: document.getElementById("skill-detail-empty"),
    skillDetail: document.getElementById("skill-detail-content"),
    skillBackdrop: document.getElementById("skill-detail-backdrop"),
    skillDrawer: document.querySelector(".skill-detail-panel"),
    skillRefresh: document.getElementById("skill-refresh-btn"),
    skillSave: document.getElementById("skill-save-btn"),
    toolSearch: document.getElementById("tool-search-input"),
    toolStatus: document.getElementById("tool-status-filter"),
    toolRisk: document.getElementById("tool-risk-filter"),
    toolList: document.getElementById("tool-list"),
    toolEmpty: document.getElementById("tool-detail-empty"),
    toolDetail: document.getElementById("tool-detail-content"),
    toolBackdrop: document.getElementById("tool-detail-backdrop"),
    toolDrawer: document.querySelector(".tool-detail-panel"),
    toolRefresh: document.getElementById("tool-refresh-btn"),
    toolSave: document.getElementById("tool-save-btn"),
};

const esc = (v) => String(v ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;").replaceAll('"', "&quot;").replaceAll("'", "&#39;");
const icons = () => window.lucide && lucide.createIcons();
const roleKey = (v) => (["ceo", "inspection", "checker"].includes(String(v).toLowerCase()) ? (String(v).toLowerCase() === "ceo" ? "ceo" : "inspection") : "execution");
const roleLabel = (v) => ({ ceo: "CEO", execution: "执行", inspection: "检验" }[roleKey(v)]);
const pStatus = (v) => String(v || "").trim().toLowerCase();
const canPause = (v) => ["queued", "planning", "running", "checking"].includes(pStatus(v));
const canResume = (v) => pStatus(v) === "blocked";

function hint(text, err = false) {
    U.modelHint.textContent = text;
    U.modelHint.style.color = err ? "var(--danger, #ff6b6b)" : "";
}

function addMsg(text, role) {
    const el = document.createElement("div");
    el.className = `message ${role}`;
    el.innerHTML = `<div class="avatar"><i data-lucide="${role === "system" ? "cpu" : "user"}"></i></div><div class="msg-content">${esc(text)}</div>`;
    U.ceoFeed.appendChild(el);
    icons();
    U.ceoFeed.scrollTop = U.ceoFeed.scrollHeight;
}

function addNotice(notice, bump = true) {
    const li = document.createElement("li");
    li.className = `notice-item ${String(notice.kind || "").includes("fail") ? "error" : "success"}`;
    li.innerHTML = `<div class="notice-title">${esc(notice.title || "系统通知")}</div><div class="notice-text">${esc(notice.text || "")}</div>`;
    U.noticeList.prepend(li);
    if (bump) U.noticeBadge.textContent = String(Number(U.noticeBadge.textContent || 0) + 1);
}

function modelScopeLabel(scope) {
    return (MODEL_SCOPES.find((item) => item.key === scope) || { label: String(scope || "") }).label;
}

function modelRefItem(ref) {
    const raw = String(ref || "").trim();
    if (!raw) return null;
    return S.modelCatalog.catalog.find((item) => String(item.key || "").trim() === raw || String(item.provider_model || "").trim() === raw) || null;
}

function modelRefEquivalent(left, right) {
    const leftRaw = String(left || "").trim();
    const rightRaw = String(right || "").trim();
    if (!leftRaw || !rightRaw) return false;
    if (leftRaw === rightRaw) return true;
    const leftItem = modelRefItem(leftRaw);
    const rightItem = modelRefItem(rightRaw);
    if (leftItem && rightItem) return String(leftItem.key || "") === String(rightItem.key || "");
    if (leftItem) return rightRaw === String(leftItem.key || "") || rightRaw === String(leftItem.provider_model || "");
    if (rightItem) return leftRaw === String(rightItem.key || "") || leftRaw === String(rightItem.provider_model || "");
    return false;
}

function modelScopeChain(scope) {
    return Array.isArray(S.modelCatalog.roles?.[scope]) ? [...S.modelCatalog.roles[scope]] : [];
}

function modelScopeContains(scope, ref) {
    return modelScopeChain(scope).some((item) => modelRefEquivalent(item, ref));
}

function normalizeModelRoleChain(refs) {
    const normalized = [];
    (refs || []).forEach((ref) => {
        const raw = String(ref || "").trim();
        if (!raw) return;
        const item = modelRefItem(raw);
        const target = String(item?.key || raw).trim();
        if (!target || normalized.some((existing) => modelRefEquivalent(existing, target))) return;
        normalized.push(target);
    });
    return normalized;
}

function applyModelCatalog(data, { preserveRoleDrafts = false } = {}) {
    const payload = data && typeof data === "object" ? data : {};
    const rolesPayload = payload.roles && typeof payload.roles === "object" ? payload.roles : {};
    const nextRoles = EMPTY_MODEL_ROLES();
    MODEL_SCOPES.forEach(({ key }) => {
        nextRoles[key] = Array.isArray(rolesPayload[key])
            ? rolesPayload[key].map((item) => String(item || "").trim()).filter(Boolean)
            : [];
    });
    S.modelCatalog.items = Array.isArray(payload.items) ? payload.items.map((item) => String(item || "").trim()).filter(Boolean) : [];
    S.modelCatalog.catalog = Array.isArray(payload.catalog) ? payload.catalog.map((item) => ({ ...item })) : [];
    if (!preserveRoleDrafts) {
        S.modelCatalog.roles = nextRoles;
        S.modelCatalog.rolesDirty = false;
    }
    S.modelCatalog.defaults = { ...DEFAULT_MODEL_DEFAULTS(), ...(payload.defaults || {}) };
    if (S.modelCatalog.mode !== "create") {
        const selectedKey = String(S.modelCatalog.selectedModelKey || "").trim();
        if (selectedKey && !S.modelCatalog.catalog.some((item) => String(item.key || "").trim() === selectedKey)) {
            S.modelCatalog.selectedModelKey = "";
        }
    }
}

function filterModels() {
    const q = String(S.modelCatalog.search || "").trim().toLowerCase();
    if (!q) return [...S.modelCatalog.catalog];
    return S.modelCatalog.catalog.filter((item) => [item.key, item.provider_model, item.description].join("\n").toLowerCase().includes(q));
}

function syncModelDetailScopeToggles() {
    if (!U.modelDetail || S.modelCatalog.mode === "create") return;
    const selectedKey = String(S.modelCatalog.selectedModelKey || "").trim();
    if (!selectedKey) return;
    MODEL_SCOPES.forEach(({ key }) => {
        const input = U.modelDetail.querySelector(`[name="scope_${key}"]`);
        if (!(input instanceof HTMLInputElement)) return;
        const checked = modelScopeContains(key, selectedKey);
        input.checked = checked;
        input.closest(".role-toggle")?.classList.toggle("checked", checked);
    });
}

function renderModelHint() {
    if (S.modelCatalog.loading) return hint("正在加载模型配置...");
    if (S.modelCatalog.saving) return hint("正在保存模型配置...");
    if (S.modelCatalog.error) return hint(`模型配置错误：${S.modelCatalog.error}`, true);
    if (!S.modelCatalog.catalog.length) return hint("当前还没有模型，请先添加模型。", false);
    if (S.modelCatalog.rolesDirty) return hint("角色模型链有未保存的修改，请点击“保存角色链”。", false);
    return hint("可在这里维护模型目录、角色降级链和模型参数。", false);
}

function renderModelRoleEditors() {
    if (!U.modelRoleEditors) return;
    U.modelRoleEditors.innerHTML = MODEL_SCOPES.map((scope) => {
        const chain = modelScopeChain(scope.key);
        const options = S.modelCatalog.catalog.filter((item) => !chain.some((ref) => modelRefEquivalent(ref, item.key)));
        const defaultText = scope.key === "agent"
            ? "主流程调用链"
            : (S.modelCatalog.defaults[scope.key] ? `当前首选 ${S.modelCatalog.defaults[scope.key]}` : "未配置当前首选");
        const chainMarkup = chain.length
            ? chain.map((ref, index) => {
                const item = modelRefItem(ref);
                const badges = [index === 0 ? '<span class="policy-chip risk-low">首选</span>' : ""];
                if (item?.enabled === false) badges.push('<span class="policy-chip neutral">已禁用</span>');
                if (!item) badges.push('<span class="policy-chip neutral">未托管</span>');
                return `
                    <div class="model-chain-item">
                        <div class="model-chain-item-main">
                            <div class="resource-list-title">${esc(item?.key || ref)}</div>
                            <div class="resource-list-subtitle">${esc(item?.provider_model || ref)}</div>
                            <div class="model-inline-meta">${badges.join("")}</div>
                        </div>
                        <div class="model-chain-item-actions">
                            <button type="button" class="toolbar-btn ghost small" data-model-chain-action="up" data-scope="${scope.key}" data-index="${index}" ${index === 0 ? "disabled" : ""}>上移</button>
                            <button type="button" class="toolbar-btn ghost small" data-model-chain-action="down" data-scope="${scope.key}" data-index="${index}" ${index === chain.length - 1 ? "disabled" : ""}>下移</button>
                            <button type="button" class="toolbar-btn ghost small" data-model-chain-action="remove" data-scope="${scope.key}" data-index="${index}">移除</button>
                        </div>
                    </div>`;
            }).join("")
            : '<div class="empty-state compact">未配置降级链</div>';
        return `
            <section class="model-chain-card">
                <div class="panel-header">
                    <div>
                        <h3>${esc(scope.label)}</h3>
                        <p class="subtitle">${esc(defaultText)}</p>
                    </div>
                    <span class="policy-chip neutral">${chain.length} 个候选</span>
                </div>
                <div class="model-chain-list">${chainMarkup}</div>
                <div class="model-chain-adder">
                    <select class="resource-select" data-model-role-select="${scope.key}" ${!options.length ? "disabled" : ""}>
                        <option value="">添加模型到该角色链</option>
                        ${options.map((item) => `<option value="${esc(item.key)}">${esc(item.key)} · ${esc(item.provider_model)}</option>`).join("")}
                    </select>
                    <button type="button" class="toolbar-btn ghost" data-model-role-add="${scope.key}" ${!options.length ? "disabled" : ""}>添加</button>
                </div>
            </section>`;
    }).join("");
}

function renderModelList() {
    if (!U.modelList) return;
    const items = filterModels();
    if (!items.length) {
        U.modelList.innerHTML = `<div class="empty-state">${S.modelCatalog.search ? "没有匹配的模型。" : "还没有模型，请点击“添加模型”。"}</div>`;
        return;
    }
    U.modelList.innerHTML = items.map((item) => {
        const scopes = MODEL_SCOPES.filter((scope) => modelScopeContains(scope.key, item.key)).map((scope) => scope.label);
        const meta = [item.enabled ? "已启用" : "已禁用", scopes.length ? scopes.join(" · ") : "未加入角色链"];
        if (item.description) meta.push(item.description);
        return `
            <button type="button" class="resource-list-item${S.modelCatalog.mode !== "create" && S.modelCatalog.selectedModelKey === item.key ? " selected" : ""}" data-model-key="${esc(item.key)}">
                <div class="resource-list-item-top">
                    <div>
                        <div class="resource-list-title">${esc(item.key)}</div>
                        <div class="resource-list-subtitle">${esc(item.provider_model)}</div>
                    </div>
                    <span class="policy-chip ${item.enabled ? "risk-low" : "neutral"}">${item.enabled ? "已启用" : "已禁用"}</span>
                </div>
                <div class="resource-list-meta">${esc(meta.join(" · "))}</div>
            </button>`;
    }).join("");
}

function renderModelDetail() {
    if (!U.modelDetail || !U.modelDetailEmpty) return;
    const isCreate = S.modelCatalog.mode === "create";
    const current = isCreate ? null : modelRefItem(S.modelCatalog.selectedModelKey);
    if (!isCreate && !current) {
        U.modelDetailEmpty.style.display = "grid";
        U.modelDetail.innerHTML = "";
        return;
    }

    const enabled = isCreate ? true : !!current?.enabled;
    const selectedScopes = MODEL_SCOPES.filter((scope) => current && modelScopeContains(scope.key, current.key)).map((scope) => scope.label);
    const scopeMarkup = MODEL_SCOPES.map((scope) => {
        const checked = current ? modelScopeContains(scope.key, current.key) : false;
        return `<label class="role-toggle ${checked ? "checked" : ""}"><input type="checkbox" name="scope_${scope.key}" ${checked ? "checked" : ""}><span>${esc(scope.label)}</span></label>`;
    }).join("");

    U.modelDetailEmpty.style.display = "none";
    U.modelDetail.innerHTML = `
        <article class="model-detail-card">
            <div class="panel-header">
                <div>
                    <h2>${isCreate ? "添加模型" : "模型配置"}</h2>
                    <p class="subtitle">${esc(isCreate ? "填写必填项后写入 .g3ku/config.json" : `${current.key} · ${current.provider_model}`)}</p>
                </div>
                <div class="model-inline-meta">
                    <span class="policy-chip ${enabled ? "risk-low" : "neutral"}">${enabled ? "已启用" : "已禁用"}</span>
                    ${!isCreate ? `<span class="policy-chip neutral">${esc(selectedScopes.join(" / ") || "未加入角色链")}</span>` : ""}
                </div>
            </div>
            <form id="model-detail-form" class="model-detail-form" data-mode="${isCreate ? "create" : "edit"}" data-model-key="${esc(current?.key || "")}">
                <section class="resource-section">
                    <h3>基本信息</h3>
                    <div class="model-form-grid">
                        <label class="resource-field">
                            <span class="resource-field-label">模型 Key *</span>
                            <input class="resource-search" name="key" ${isCreate ? `value=""` : `value="${esc(current.key)}" disabled`} placeholder="如 openai_primary">
                        </label>
                        <label class="resource-field">
                            <span class="resource-field-label">Provider / Model *</span>
                            <input class="resource-search" name="providerModel" value="${esc(current?.provider_model || "")}" placeholder="如 openai:gpt-4.1">
                        </label>
                        <label class="resource-field">
                            <span class="resource-field-label">API Key *</span>
                            <input class="resource-search" name="apiKey" value="${esc(current?.api_key || "")}" placeholder="sk-...">
                        </label>
                        <label class="resource-field">
                            <span class="resource-field-label">Base URL ${isCreate ? "*" : ""}</span>
                            <input class="resource-search" name="apiBase" value="${esc(current?.api_base || "")}" placeholder="https://api.example.com/v1">
                        </label>
                    </div>
                    <label class="role-toggle ${enabled ? "checked" : ""}"><input type="checkbox" name="enabled" ${enabled ? "checked" : ""}><span>启用此模型</span></label>
                </section>
                <section class="resource-section">
                    <h3>模型参数</h3>
                    <div class="model-form-grid">
                        <label class="resource-field">
                            <span class="resource-field-label">Max Tokens</span>
                            <input class="resource-search" type="number" min="1" step="1" name="maxTokens" value="${esc(String(current?.max_tokens ?? ""))}" placeholder="留空使用默认值">
                        </label>
                        <label class="resource-field">
                            <span class="resource-field-label">Temperature</span>
                            <input class="resource-search" type="number" min="0" max="2" step="0.1" name="temperature" value="${esc(String(current?.temperature ?? ""))}" placeholder="留空使用默认值">
                        </label>
                        <label class="resource-field">
                            <span class="resource-field-label">Reasoning Effort</span>
                            <input class="resource-search" name="reasoningEffort" value="${esc(current?.reasoning_effort || "")}" placeholder="如 low / medium / high">
                        </label>
                        <label class="resource-field">
                            <span class="resource-field-label">Retry On</span>
                            <input class="resource-search" name="retryOn" value="${esc((current?.retry_on || []).join(", "))}" placeholder="如 network, 429, 5xx">
                        </label>
                    </div>
                </section>
                <section class="resource-section">
                    <h3>作用范围</h3>
                    <div class="model-scopes-grid">${scopeMarkup}</div>
                </section>
                <section class="resource-section">
                    <h3>额外请求头</h3>
                    <textarea class="resource-editor model-textarea" name="extraHeaders" rows="6" placeholder='{"X-Trace-Id": "demo"}'>${esc(current?.extra_headers ? JSON.stringify(current.extra_headers, null, 2) : "")}</textarea>
                </section>
                <section class="resource-section">
                    <h3>说明</h3>
                    <textarea class="resource-editor model-textarea" name="description" rows="5" placeholder="可填写用途、限制、成本等说明">${esc(current?.description || "")}</textarea>
                </section>
                <div class="model-actions">
                    <button type="submit" class="toolbar-btn success">${isCreate ? "添加并保存" : "保存模型"}</button>
                    ${isCreate ? '<button type="button" class="toolbar-btn ghost" data-model-detail-cancel="1">取消</button>' : ""}
                </div>
            </form>
        </article>`;
}

function renderModelCatalog() {
    if (U.modelRefresh) U.modelRefresh.disabled = S.modelCatalog.loading || S.modelCatalog.saving;
    if (U.modelCreate) U.modelCreate.disabled = S.modelCatalog.loading || S.modelCatalog.saving;
    if (U.modelRolesSave) {
        U.modelRolesSave.disabled = S.modelCatalog.loading || S.modelCatalog.saving || !S.modelCatalog.rolesDirty;
        U.modelRolesSave.textContent = S.modelCatalog.saving ? "正在保存角色链..." : "保存角色链";
    }
    renderModelHint();
    renderModelRoleEditors();
    renderModelList();
    renderModelDetail();
}

async function loadModels() {
    S.modelCatalog.loading = true;
    S.modelCatalog.error = "";
    renderModelCatalog();
    try {
        const data = await ApiClient.getOrgGraphModels();
        applyModelCatalog(data);
    } catch (e) {
        S.modelCatalog.error = e.message || "load failed";
    } finally {
        S.modelCatalog.loading = false;
        renderModelCatalog();
    }
}

function openModel(key) {
    S.modelCatalog.mode = "view";
    S.modelCatalog.selectedModelKey = String(key || "").trim();
    renderModelCatalog();
}

function startCreateModel() {
    S.modelCatalog.mode = "create";
    S.modelCatalog.selectedModelKey = "";
    renderModelCatalog();
}

function updateRoleChainDraft(scope, nextChain) {
    S.modelCatalog.roles[scope] = normalizeModelRoleChain(nextChain);
    S.modelCatalog.rolesDirty = true;
    renderModelHint();
    renderModelRoleEditors();
    renderModelList();
    syncModelDetailScopeToggles();
}

function setModelScopesInState(modelKey, selectedScopes) {
    const changed = [];
    MODEL_SCOPES.forEach(({ key }) => {
        const currentChain = modelScopeChain(key);
        const exists = currentChain.some((item) => modelRefEquivalent(item, modelKey));
        const shouldExist = selectedScopes.has(key);
        if (exists === shouldExist) return;
        changed.push(key);
        S.modelCatalog.roles[key] = normalizeModelRoleChain(
            shouldExist
                ? [...currentChain, modelKey]
                : currentChain.filter((item) => !modelRefEquivalent(item, modelKey))
        );
    });
    if (changed.length) S.modelCatalog.rolesDirty = true;
    return changed;
}

async function persistModelRoleChains(scopes = MODEL_SCOPES.map((item) => item.key), successText = "角色模型链已保存。") {
    const targets = [...new Set(scopes.map((item) => String(item || "").trim()).filter(Boolean))];
    if (!targets.length) return;
    S.modelCatalog.saving = true;
    renderModelCatalog();
    try {
        let payload = null;
        for (const scope of targets) {
            payload = await ApiClient.updateModelRoleChain(scope, normalizeModelRoleChain(S.modelCatalog.roles[scope] || []));
        }
        if (payload) applyModelCatalog(payload);
        hint(successText);
    } catch (e) {
        S.modelCatalog.error = e.message || "save failed";
        hint(`模型配置错误：${S.modelCatalog.error}`, true);
        throw e;
    } finally {
        S.modelCatalog.saving = false;
        renderModelCatalog();
    }
}

function parseModelRetryOn(raw) {
    return String(raw || "").split(/[\n,]/).map((item) => item.trim()).filter(Boolean);
}

function parseModelHeaders(raw) {
    const text = String(raw || "").trim();
    if (!text) return null;
    const parsed = JSON.parse(text);
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
        throw new Error("额外请求头必须是 JSON 对象");
    }
    return Object.fromEntries(Object.entries(parsed).map(([key, value]) => [String(key), String(value)]));
}

function collectModelFormData(form, current) {
    const isCreate = form.dataset.mode === "create";
    const formData = new FormData(form);
    const key = isCreate ? String(formData.get("key") || "").trim() : String(form.dataset.modelKey || "").trim();
    const providerModel = String(formData.get("providerModel") || "").trim();
    const apiKey = String(formData.get("apiKey") || "").trim();
    const apiBase = String(formData.get("apiBase") || "").trim();
    const maxTokensText = String(formData.get("maxTokens") || "").trim();
    const temperatureText = String(formData.get("temperature") || "").trim();
    const reasoningEffort = String(formData.get("reasoningEffort") || "").trim();
    const retryOnRaw = String(formData.get("retryOn") || "").trim();
    const description = String(formData.get("description") || "").trim();
    const enabled = formData.get("enabled") === "on";
    const selectedScopes = new Set(MODEL_SCOPES.filter((scope) => formData.get(`scope_${scope.key}`) === "on").map((scope) => scope.key));

    if (!key) throw new Error("模型 Key 不能为空");
    if (!providerModel) throw new Error("Provider / Model 不能为空");
    if (!apiKey) throw new Error("API Key 不能为空");
    if (isCreate && !apiBase) throw new Error("Base URL 不能为空");

    const extraHeaders = parseModelHeaders(formData.get("extraHeaders"));
    const retryOn = retryOnRaw ? parseModelRetryOn(retryOnRaw) : null;
    const maxTokens = maxTokensText ? Number(maxTokensText) : null;
    const temperature = temperatureText ? Number(temperatureText) : null;

    if (maxTokensText && (!Number.isInteger(maxTokens) || maxTokens <= 0)) {
        throw new Error("Max Tokens 必须是正整数");
    }
    if (temperatureText && (!Number.isFinite(temperature) || temperature < 0 || temperature > 2)) {
        throw new Error("Temperature 必须在 0 到 2 之间");
    }

    if (isCreate) {
        const payload = {
            key,
            providerModel,
            apiKey,
            apiBase,
            enabled,
            scopes: [...selectedScopes],
            description,
        };
        if (maxTokens !== null) payload.maxTokens = maxTokens;
        if (temperature !== null) payload.temperature = temperature;
        if (reasoningEffort) payload.reasoningEffort = reasoningEffort;
        if (retryOn !== null) payload.retryOn = retryOn;
        if (extraHeaders !== null) payload.extraHeaders = extraHeaders;
        return { isCreate, key, enabled, selectedScopes, payload };
    }

    const patch = {};
    if (providerModel !== String(current?.provider_model || "")) patch.providerModel = providerModel;
    if (apiKey !== String(current?.api_key || "")) patch.apiKey = apiKey;
    if (apiBase !== String(current?.api_base || "")) patch.apiBase = apiBase;
    if (maxTokens !== null && maxTokens !== Number(current?.max_tokens ?? NaN)) patch.maxTokens = maxTokens;
    if (temperature !== null && temperature !== Number(current?.temperature ?? NaN)) patch.temperature = temperature;
    if (reasoningEffort !== String(current?.reasoning_effort || "")) patch.reasoningEffort = reasoningEffort;
    if (retryOn !== null && JSON.stringify(retryOn) !== JSON.stringify(current?.retry_on || [])) patch.retryOn = retryOn;
    if (description !== String(current?.description || "")) patch.description = description;
    if (extraHeaders !== null && JSON.stringify(extraHeaders) !== JSON.stringify(current?.extra_headers || null)) patch.extraHeaders = extraHeaders;
    return { isCreate, key, enabled, selectedScopes, patch };
}

async function saveModelDetail() {
    const form = U.modelDetail?.querySelector("#model-detail-form");
    if (!(form instanceof HTMLFormElement)) return;
    const current = form.dataset.mode === "create" ? null : modelRefItem(form.dataset.modelKey);
    const previousRoles = structuredClone(S.modelCatalog.roles);
    const previousDirty = S.modelCatalog.rolesDirty;
    try {
        const draft = collectModelFormData(form, current);
        const targetKey = draft.isCreate ? draft.key : String(current?.key || draft.key);
        const changedScopes = setModelScopesInState(targetKey, draft.selectedScopes);
        const preserveRoleDrafts = S.modelCatalog.rolesDirty;
        const enableChanged = !draft.isCreate && draft.enabled !== !!current?.enabled;
        if (!draft.isCreate && !Object.keys(draft.patch).length && !enableChanged && !S.modelCatalog.rolesDirty) {
            hint("没有需要保存的更改。");
            return;
        }

        if (draft.isCreate) {
            const payload = await ApiClient.createManagedModel(draft.payload);
            applyModelCatalog(payload, { preserveRoleDrafts });
            S.modelCatalog.mode = "view";
            S.modelCatalog.selectedModelKey = payload.model?.key || draft.key;
        } else {
            if (Object.keys(draft.patch).length) {
                const payload = await ApiClient.updateManagedModel(current.key, draft.patch);
                applyModelCatalog(payload, { preserveRoleDrafts });
            }
            if (enableChanged) {
                const payload = draft.enabled ? await ApiClient.enableManagedModel(current.key) : await ApiClient.disableManagedModel(current.key);
                applyModelCatalog(payload, { preserveRoleDrafts });
            }
            S.modelCatalog.mode = "view";
            S.modelCatalog.selectedModelKey = current.key;
        }

        if (S.modelCatalog.rolesDirty || changedScopes.length) {
            await persistModelRoleChains(MODEL_SCOPES.map((item) => item.key), draft.isCreate ? "模型已添加并同步角色链。" : "模型配置已保存。");
            return;
        }
        hint(draft.isCreate ? "模型已添加。" : "模型配置已保存。");
        renderModelCatalog();
    } catch (e) {
        S.modelCatalog.roles = previousRoles;
        S.modelCatalog.rolesDirty = previousDirty;
        S.modelCatalog.error = e.message || "save failed";
        hint(`模型配置错误：${S.modelCatalog.error}`, true);
        renderModelRoleEditors();
        renderModelList();
        syncModelDetailScopeToggles();
    }
}

async function loadNotices() {
    try {
        const notices = await ApiClient.getNotices(0, 50);
        U.noticeList.innerHTML = "";
        let count = 0;
        notices.forEach((n) => { addNotice(n, false); if (!n.acknowledged) count += 1; });
        U.noticeBadge.textContent = String(count);
    } catch (e) {
        console.error("加载通知失败:", e);
    }
}

function initCeoWs() {
    if (S.ceoWs && S.ceoWs.readyState <= 1) return;
    S.ceoWs = new WebSocket(ApiClient.getCeoWsUrl());
    S.ceoWs.onmessage = (ev) => {
        const payload = JSON.parse(ev.data);
        if (payload.type === "ceo.reply.final") addMsg(payload.data?.text || "", "system");
        if (payload.type === "project.notice") addNotice(payload.data || {});
    };
    S.ceoWs.onclose = () => window.setTimeout(() => S.view === "ceo" && initCeoWs(), 1000);
}

function sendCeoMessage() {
    const text = String(U.ceoInput.value || "").trim();
    if (!text) return;
    addMsg(text, "user");
    U.ceoInput.value = "";
    if (!S.ceoWs || S.ceoWs.readyState !== WebSocket.OPEN) {
        addMsg("连接尚未建立，请稍后重试。", "system");
        initCeoWs();
        return;
    }
    S.ceoWs.send(JSON.stringify({ type: "client.user_message", session_id: "web:shared", text }));
}

function syncProjectSelection() {
    const ids = new Set(S.projects.map((p) => p.project_id));
    [...S.selectedProjects].forEach((id) => !ids.has(id) && S.selectedProjects.delete(id));
}

function updateProjectToolbar() {
    const selected = S.projects.filter((p) => S.selectedProjects.has(p.project_id));
    U.projectSummary.textContent = `已选择 ${selected.length} 项`;
    U.pauseBatch.disabled = S.projectBusy || !selected.some((p) => canPause(p.status));
    U.resumeBatch.disabled = S.projectBusy || !selected.some((p) => canResume(p.status));
}

function setDrawerOpen(backdrop, drawer, open) {
    backdrop?.classList.toggle("is-open", open);
    drawer?.classList.toggle("is-open", open);
}

function syncActionButton(button, { idleLabel, busyLabel, busy, disabled }) {
    if (!button) return;
    button.disabled = disabled;
    button.textContent = busy ? busyLabel : idleLabel;
}

function renderSkillActions() {
    syncActionButton(U.skillRefresh, {
        idleLabel: "刷新",
        busyLabel: "处理中...",
        busy: S.skillBusy,
        disabled: S.skillBusy,
    });
    syncActionButton(U.skillSave, {
        idleLabel: "保存",
        busyLabel: "保存中...",
        busy: S.skillBusy,
        disabled: S.skillBusy || !S.selectedSkill,
    });
}

function renderToolActions() {
    syncActionButton(U.toolRefresh, {
        idleLabel: "刷新",
        busyLabel: "处理中...",
        busy: S.toolBusy,
        disabled: S.toolBusy,
    });
    syncActionButton(U.toolSave, {
        idleLabel: "保存",
        busyLabel: "保存中...",
        busy: S.toolBusy,
        disabled: S.toolBusy || !S.selectedTool,
    });
}

function clearSkillSelection() {
    S.selectedSkill = null;
    S.skillFiles = [];
    S.skillContents = {};
    S.selectedSkillFile = "";
    renderSkills();
    renderSkillDetail();
}

function clearToolSelection() {
    S.selectedTool = null;
    renderTools();
    renderToolDetail();
}

function renderProjects() {
    U.projectGrid.innerHTML = "";
    if (!S.projects.length) {
        U.projectGrid.innerHTML = '<div class="empty-state" style="grid-column: 1/-1;">当前没有项目。</div>';
        return updateProjectToolbar();
    }
    S.projects.forEach((p) => {
        const selected = S.selectedProjects.has(p.project_id);
        const el = document.createElement("div");
        el.className = `project-card${selected ? " is-selected" : ""}`;
        el.innerHTML = `
            <div class="pc-topbar">
                <label class="project-select-toggle"><input type="checkbox" class="project-select-checkbox" ${selected ? "checked" : ""}><span>选择</span></label>
                <span class="status-badge" data-status="${esc(p.status)}">${esc(String(p.status || "").toUpperCase())}</span>
            </div>
            <div class="pc-header"><div><h3 class="pc-title">${esc(p.title)}</h3><span class="pc-id">${esc(p.project_id)}</span></div></div>
            <div class="pc-summary">${esc(p.summary || "暂无摘要")}</div>
            <div class="pc-stats">${esc(String(p.active_unit_count || 0))} 个活动单元</div>
            <div class="pc-actions">
                <button class="project-action-btn warn" type="button" data-action="pause" ${canPause(p.status) ? "" : "disabled"}>暂停</button>
                <button class="project-action-btn success" type="button" data-action="resume" ${canResume(p.status) ? "" : "disabled"}>恢复</button>
            </div>
        `;
        el.querySelector(".project-select-checkbox")?.addEventListener("change", (e) => {
            if (e.target.checked) S.selectedProjects.add(p.project_id);
            else S.selectedProjects.delete(p.project_id);
            renderProjects();
        });
        el.querySelectorAll(".project-action-btn").forEach((btn) => btn.addEventListener("click", async (e) => {
            e.stopPropagation();
            await runProjectAction(p.project_id, btn.dataset.action);
        }));
        el.addEventListener("click", () => openProject(p.project_id));
        U.projectGrid.appendChild(el);
    });
    updateProjectToolbar();
    icons();
}

async function loadProjects() {
    U.projectGrid.innerHTML = '<div class="empty-state" style="grid-column: 1/-1;">正在加载项目列表...</div>';
    try {
        S.projects = await ApiClient.getProjects(0, 50);
        syncProjectSelection();
        renderProjects();
    } catch (e) {
        U.projectGrid.innerHTML = `<div class="empty-state error" style="grid-column: 1/-1;">加载项目失败：${esc(e.message)}</div>`;
    }
}

async function runProjectAction(projectId, action) {
    S.projectBusy = true;
    updateProjectToolbar();
    try {
        if (action === "pause") await ApiClient.pauseProject(projectId);
        if (action === "resume") await ApiClient.resumeProject(projectId);
        await loadProjects();
    } catch (e) {
        addMsg(`项目操作失败：${projectId} - ${e.message}`, "system");
    } finally {
        S.projectBusy = false;
        updateProjectToolbar();
    }
}

function resetProjectView() {
    S.tree = null;
    S.selectedUnitId = null;
    S.events = [];
    U.tree.innerHTML = '<div class="empty-state">等待获取组织结构...</div>';
    U.feed.innerHTML = "";
    U.feedTitle.textContent = "项目全局动态 / 详情";
    hideAgent();
}

function eventType(evt) {
    const name = String(evt.event_name || "");
    if (name.startsWith("checker.")) return "checker";
    if (name.startsWith("tool.")) return "tool";
    if (name.startsWith("stage.")) return "stage";
    if (name.startsWith("unit.")) return "unit";
    return "info";
}

function eventCard(evt) {
    const type = eventType(evt);
    const icon = { unit: "user", tool: "wrench", stage: "git-commit", checker: "shield-check", info: "hash" }[type];
    const el = document.createElement("div");
    el.className = `feed-card type-${type}`;
    el.innerHTML = `<div class="card-header"><span style="display:flex; gap:4px; align-items:center;"><i data-lucide="${icon}" style="width:14px;height:14px;"></i>${esc(type.toUpperCase())}</span><span>${esc(new Date(evt.created_at || Date.now()).toLocaleTimeString())}</span></div><div class="card-title">${esc(evt.event_name || "project.event")}</div><div class="card-content">${esc(evt.text || "")}</div>`;
    return el;
}

function renderFeed() {
    U.feed.innerHTML = "";
    const rows = S.selectedUnitId ? S.events.filter((evt) => String(evt.unit_id || "") === S.selectedUnitId || !evt.unit_id) : S.events;
    rows.forEach((evt) => U.feed.appendChild(eventCard(evt)));
    icons();
}

function patchTreeUnit(unit) {
    const visit = (node) => {
        if (!node) return false;
        if (node.unit_id === unit.unit_id) { Object.assign(node, unit); return true; }
        return Array.isArray(node.children) && node.children.some(visit);
    };
    visit(S.tree);
}

function renderTree() {
    if (!S.tree) return;
    const levels = [];
    const walk = (node, depth) => {
        if (!levels[depth]) levels[depth] = [];
        levels[depth].push(node);
        (node.children || []).forEach((child) => walk(child, depth + 1));
    };
    walk(S.tree, 0);
    const wrapper = document.createElement("div");
    wrapper.className = "tree-flow-wrapper";
    levels.forEach((level) => {
        const col = document.createElement("div");
        col.className = "tree-level";
        level.forEach((node) => {
            const wrap = document.createElement("div");
            wrap.className = `flow-node-wrapper${node.children?.length ? " has-children" : ""} line-status-${esc(node.status)}`;
            const el = document.createElement("div");
            el.className = `tree-node${S.selectedUnitId === node.unit_id ? " selected" : ""}`;
            el.dataset.id = node.unit_id;
            el.dataset.role = node.role_title || roleLabel(node.role_kind);
            el.dataset.status = node.status;
            el.dataset.objective = node.objective_summary || "";
            el.dataset.prompt = node.prompt_preview || "";
            el.dataset.result = node.result_summary || node.error_summary || "暂无结果";
            el.innerHTML = `<div class="node-header"><span class="node-title">${esc(node.role_title || roleLabel(node.role_kind))}</span><span class="status-badge" data-status="${esc(node.status)}">${esc(String(node.status || "").toUpperCase())}</span></div><div class="node-desc">${esc(node.objective_summary || roleLabel(node.role_kind))}</div>`;
            el.addEventListener("click", (e) => { e.stopPropagation(); S.selectedUnitId = node.unit_id; showAgent(el.dataset); renderTree(); renderFeed(); });
            wrap.appendChild(el);
            col.appendChild(wrap);
        });
        wrapper.appendChild(col);
    });
    U.tree.innerHTML = "";
    U.tree.appendChild(wrapper);
}

function showAgent(data) {
    U.detail.style.display = "flex";
    U.adRole.textContent = data.role || "执行";
    U.adStatus.textContent = String(data.status || "").toUpperCase();
    U.adStatus.dataset.status = data.status || "";
    U.adObjective.textContent = data.objective || "-";
    U.adPrompt.textContent = data.prompt || "-";
    U.adResult.textContent = data.result || "-";
    U.feedTitle.textContent = `限定视角: ${data.role || "执行"}`;
    const logs = S.events.filter((evt) => String(evt.unit_id || "") === String(data.id || ""));
    U.adLogs.innerHTML = logs.length ? "" : '<div class="empty-state" style="padding: 10px;">暂无动作日志...</div>';
    logs.forEach((evt) => U.adLogs.appendChild(eventCard(evt)));
    icons();
}

function hideAgent() {
    if (U.detail) U.detail.style.display = "none";
}

async function openProject(projectId) {
    S.currentProjectId = projectId;
    switchView("project-details");
    resetProjectView();
    if (S.projectWs) { S.projectWs.close(); S.projectWs = null; }
    try {
        const [meta, tree, events] = await Promise.all([ApiClient.getProjectDetails(projectId), ApiClient.getProjectTree(projectId), ApiClient.getProjectEvents(projectId, 0, 200)]);
        if (meta.project) {
            U.pdTitle.textContent = meta.project.title;
            U.pdStatus.textContent = String(meta.project.status || "").toUpperCase();
            U.pdStatus.dataset.status = meta.project.status || "";
            U.pdSummary.textContent = meta.project.summary || "暂无摘要";
            U.pdActiveCount.textContent = String(meta.project.active_unit_count || 0);
        }
        if (tree.root) { S.tree = tree.root; renderTree(); }
        S.events = Array.isArray(events) ? events : [];
        renderFeed();
        const afterSeq = S.events.reduce((max, evt) => Math.max(max, Number(evt.seq || 0)), 0);
        S.projectWs = new WebSocket(ApiClient.getProjectWsUrl(projectId, afterSeq));
        S.projectWs.onmessage = (ev) => handleProjectEvent(JSON.parse(ev.data));
    } catch (e) {
        U.tree.innerHTML = `<div class="empty-state error">初始化项目失败：${esc(e.message)}</div>`;
    }
}

function handleProjectEvent(payload) {
    if (payload.type === "snapshot.project") {
        U.pdTitle.textContent = payload.data.title;
        U.pdStatus.textContent = String(payload.data.status || "").toUpperCase();
        U.pdStatus.dataset.status = payload.data.status || "";
        U.pdSummary.textContent = payload.data.summary || "暂无摘要";
        U.pdActiveCount.textContent = String(payload.data.active_unit_count || 0);
        return;
    }
    if (payload.type === "snapshot.tree") {
        S.tree = payload.data;
        return renderTree();
    }
    if (payload.type === "project.event") {
        S.events.push(payload.data);
        if (["unit.created", "unit.updated", "unit.completed", "unit.failed"].includes(payload.data?.event_name)) {
            const unit = payload.data?.data?.unit;
            if (unit && S.tree) patchTreeUnit(unit);
            renderTree();
        }
        return renderFeed();
    }
    if (payload.type === "artifact.created") {
        S.events.push({ event_name: "artifact.created", text: payload.data?.title || "新产物", created_at: new Date().toISOString(), seq: Date.now() });
        return renderFeed();
    }
    if (payload.type === "project.finished") {
        S.events.push({ event_name: "project.finished", text: payload.data?.final_result || "项目执行完成", created_at: new Date().toISOString(), seq: Date.now() });
        return renderFeed();
    }
}

function filterSkills() {
    const q = String(U.skillSearch.value || "").trim().toLowerCase();
    return S.skills.filter((skill) => {
        if (q && !`${skill.skill_id} ${skill.display_name} ${skill.source_path}`.toLowerCase().includes(q)) return false;
        if (U.skillRisk.value !== "all" && skill.risk_level !== U.skillRisk.value) return false;
        if (U.skillStatus.value === "enabled" && !skill.enabled) return false;
        if (U.skillStatus.value === "disabled" && skill.enabled) return false;
        if (U.skillStatus.value === "unavailable" && skill.available) return false;
        if (U.skillLegacy.value === "legacy" && !skill.legacy) return false;
        if (U.skillLegacy.value === "standard" && skill.legacy) return false;
        return true;
    });
}

function renderSkills() {
    U.skillList.innerHTML = "";
    const items = filterSkills();
    if (!items.length) return void (U.skillList.innerHTML = '<div class="empty-state">没有匹配的 Skill。</div>');
    items.forEach((skill) => {
        const el = document.createElement("button");
        el.type = "button";
        el.className = `resource-list-item${S.selectedSkill?.skill_id === skill.skill_id ? " selected" : ""}`;
        el.innerHTML = `<div class="resource-list-title">${esc(skill.display_name)}</div><div class="resource-list-subtitle">${esc(skill.skill_id)}</div><div class="resource-list-meta">${esc(skill.risk_level)} · ${skill.enabled ? "已启用" : "已禁用"}</div>`;
        el.addEventListener("click", () => openSkill(skill.skill_id));
        U.skillList.appendChild(el);
    });
}

function renderSkillDetail() {
    if (!S.selectedSkill) { U.skillEmpty.style.display = "block"; U.skillDetail.innerHTML = ""; return; }
    U.skillEmpty.style.display = "none";
    const roles = ["ceo", "execution", "inspection"];
    U.skillDetail.innerHTML = `<article class="resource-detail-card"><div class="panel-header"><div><h2>${esc(S.selectedSkill.display_name)}</h2><p class="subtitle">${esc(S.selectedSkill.skill_id)}</p></div></div><label class="role-toggle checked"><input id="skill-enabled" type="checkbox" ${S.selectedSkill.enabled ? "checked" : ""}><span>已启用</span></label><div class="resource-section"><h3>允许角色</h3><div class="resource-filter-row">${roles.map((r) => `<label class="role-toggle ${S.selectedSkill.allowed_roles.includes(r) ? "checked" : ""}"><input type="checkbox" class="skill-role" data-role="${r}" ${S.selectedSkill.allowed_roles.includes(r) ? "checked" : ""}><span>${esc(roleLabel(r))}</span></label>`).join("")}</div></div><div class="resource-section"><h3>可编辑文件</h3><div class="resource-filter-row">${S.skillFiles.map((f) => `<button type="button" class="toolbar-btn ghost skill-file ${S.selectedSkillFile === f.file_key ? "active" : ""}" data-file="${esc(f.file_key)}">${esc(f.file_key)}</button>`).join("")}</div><textarea id="skill-editor" rows="18" class="resource-editor">${esc(S.skillContents[S.selectedSkillFile] || "")}</textarea></div></article>`;
    U.skillDetail.querySelector("#skill-enabled")?.addEventListener("change", (e) => { S.selectedSkill.enabled = !!e.target.checked; });
    U.skillDetail.querySelectorAll(".skill-role").forEach((cb) => cb.addEventListener("change", (e) => {
        const set = new Set(S.selectedSkill.allowed_roles || []);
        if (e.target.checked) set.add(e.target.dataset.role); else set.delete(e.target.dataset.role);
        S.selectedSkill.allowed_roles = [...set];
        renderSkillDetail();
    }));
    U.skillDetail.querySelectorAll(".skill-file").forEach((btn) => btn.addEventListener("click", () => {
        const editor = document.getElementById("skill-editor");
        if (editor && S.selectedSkillFile) S.skillContents[S.selectedSkillFile] = editor.value;
        S.selectedSkillFile = btn.dataset.file;
        renderSkillDetail();
    }));
}

async function loadSkills() { S.skills = await ApiClient.getSkills(0, 300); renderSkills(); }

async function openSkill(skillId) {
    const [skill, files] = await Promise.all([ApiClient.getSkill(skillId), ApiClient.getSkillFiles(skillId)]);
    S.selectedSkill = skill;
    S.skillFiles = files;
    S.selectedSkillFile = files[0]?.file_key || "";
    S.skillContents = {};
    await Promise.all(files.map(async (file) => {
        const data = await ApiClient.getSkillFile(skillId, file.file_key);
        S.skillContents[file.file_key] = data.content || "";
    }));
    renderSkills();
    renderSkillDetail();
}

async function saveSkill() {
    if (!S.selectedSkill) return;
    const editor = document.getElementById("skill-editor");
    if (editor && S.selectedSkillFile) S.skillContents[S.selectedSkillFile] = editor.value;
    for (const [key, content] of Object.entries(S.skillContents)) await ApiClient.saveSkillFile(S.selectedSkill.skill_id, key, content);
    await ApiClient.updateSkillPolicy(S.selectedSkill.skill_id, { enabled: !!S.selectedSkill.enabled, allowed_roles: S.selectedSkill.allowed_roles || [] });
    await loadSkills();
    await openSkill(S.selectedSkill.skill_id);
}

function filterTools() {
    const q = String(U.toolSearch.value || "").trim().toLowerCase();
    return S.tools.filter((tool) => {
        if (q && !`${tool.tool_id} ${tool.display_name} ${tool.source_path}`.toLowerCase().includes(q)) return false;
        if (U.toolStatus.value === "enabled" && !tool.enabled) return false;
        if (U.toolStatus.value === "disabled" && tool.enabled) return false;
        if (U.toolStatus.value === "unavailable" && tool.available) return false;
        if (U.toolRisk.value !== "all" && !(tool.actions || []).some((a) => a.risk_level === U.toolRisk.value)) return false;
        return true;
    });
}

function renderTools() {
    U.toolList.innerHTML = "";
    const items = filterTools();
    if (!items.length) return void (U.toolList.innerHTML = '<div class="empty-state">没有匹配的工具族。</div>');
    items.forEach((tool) => {
        const el = document.createElement("button");
        el.type = "button";
        el.className = `resource-list-item${S.selectedTool?.tool_id === tool.tool_id ? " selected" : ""}`;
        el.innerHTML = `<div class="resource-list-title">${esc(tool.display_name)}</div><div class="resource-list-subtitle">${esc(tool.tool_id)}</div><div class="resource-list-meta">${tool.enabled ? "已启用" : "已禁用"} · ${(tool.actions || []).length} 个动作</div>`;
        el.addEventListener("click", () => openTool(tool.tool_id));
        U.toolList.appendChild(el);
    });
}

function renderToolDetail() {
    if (!S.selectedTool) { U.toolEmpty.style.display = "block"; U.toolDetail.innerHTML = ""; return; }
    U.toolEmpty.style.display = "none";
    const roles = ["ceo", "execution", "inspection"];
    U.toolDetail.innerHTML = `<article class="resource-detail-card"><div class="panel-header"><div><h2>${esc(S.selectedTool.display_name)}</h2><p class="subtitle">${esc(S.selectedTool.tool_id)}</p></div></div><label class="role-toggle checked"><input id="tool-enabled" type="checkbox" ${S.selectedTool.enabled ? "checked" : ""}><span>已启用</span></label><div class="resource-section"><h3>动作矩阵</h3><div class="matrix-table"><table><thead><tr><th>动作</th><th>风险</th>${roles.map((r) => `<th>${esc(roleLabel(r))}</th>`).join("")}</tr></thead><tbody>${(S.selectedTool.actions || []).map((a) => `<tr><td>${esc(a.label || a.action_id)}</td><td>${esc(a.risk_level || "medium")}</td>${roles.map((r) => `<td><input type="checkbox" class="tool-role" data-action="${esc(a.action_id)}" data-role="${r}" ${a.allowed_roles?.includes(r) ? "checked" : ""}></td>`).join("")}</tr>`).join("")}</tbody></table></div></div></article>`;
    U.toolDetail.querySelector("#tool-enabled")?.addEventListener("change", (e) => { S.selectedTool.enabled = !!e.target.checked; });
    U.toolDetail.querySelectorAll(".tool-role").forEach((cb) => cb.addEventListener("change", (e) => {
        const action = S.selectedTool.actions.find((item) => item.action_id === e.target.dataset.action);
        if (!action) return;
        const set = new Set(action.allowed_roles || []);
        if (e.target.checked) set.add(e.target.dataset.role); else set.delete(e.target.dataset.role);
        action.allowed_roles = [...set];
    }));
}

async function loadTools() { S.tools = await ApiClient.getTools(0, 300); renderTools(); }
async function openTool(toolId) { S.selectedTool = await ApiClient.getTool(toolId); renderTools(); renderToolDetail(); }
async function saveTool() {
    if (!S.selectedTool) return;
    await ApiClient.updateToolPolicy(S.selectedTool.tool_id, { enabled: !!S.selectedTool.enabled, actions: (S.selectedTool.actions || []).map((a) => ({ action_id: a.action_id, allowed_roles: a.allowed_roles || [] })) });
    await loadTools();
    await openTool(S.selectedTool.tool_id);
}

function toggleTheme() {
    const html = document.documentElement;
    const dark = html.getAttribute("data-theme") === "dark";
    html.setAttribute("data-theme", dark ? "light" : "dark");
    const darkIcon = U.theme.querySelector(".dark-icon");
    const lightIcon = U.theme.querySelector(".light-icon");
    if (darkIcon && lightIcon) { darkIcon.style.display = dark ? "none" : "block"; lightIcon.style.display = dark ? "block" : "none"; }
}

function renderSkillDetail() {
    if (!S.selectedSkill) {
        U.skillEmpty.style.display = "block";
        U.skillDetail.innerHTML = "";
        setDrawerOpen(U.skillBackdrop, U.skillDrawer, false);
        renderSkillActions();
        return;
    }
    U.skillEmpty.style.display = "none";
    setDrawerOpen(U.skillBackdrop, U.skillDrawer, true);
    const roles = ["ceo", "execution", "inspection"];
    const allowedRoles = Array.isArray(S.selectedSkill.allowed_roles) ? S.selectedSkill.allowed_roles : [];
    U.skillDetail.innerHTML = `<article class="resource-detail-card"><div class="panel-header"><div><h2>${esc(S.selectedSkill.display_name)}</h2><p class="subtitle">${esc(S.selectedSkill.skill_id)}</p></div></div><label class="role-toggle checked"><input id="skill-enabled" type="checkbox" ${S.selectedSkill.enabled ? "checked" : ""}><span>已启用</span></label><div class="resource-section"><h3>允许角色</h3><div class="resource-filter-row">${roles.map((r) => `<label class="role-toggle ${allowedRoles.includes(r) ? "checked" : ""}"><input type="checkbox" class="skill-role" data-role="${r}" ${allowedRoles.includes(r) ? "checked" : ""}><span>${esc(roleLabel(r))}</span></label>`).join("")}</div></div><div class="resource-section"><h3>可编辑文件</h3><div class="resource-filter-row">${S.skillFiles.map((f) => `<button type="button" class="toolbar-btn ghost skill-file ${S.selectedSkillFile === f.file_key ? "active" : ""}" data-file="${esc(f.file_key)}">${esc(f.file_key)}</button>`).join("")}</div><textarea id="skill-editor" rows="18" class="resource-editor">${esc(S.skillContents[S.selectedSkillFile] || "")}</textarea></div></article>`;
    U.skillDetail.querySelector("#skill-enabled")?.addEventListener("change", (e) => { S.selectedSkill.enabled = !!e.target.checked; });
    U.skillDetail.querySelectorAll(".skill-role").forEach((cb) => cb.addEventListener("change", (e) => {
        const set = new Set(allowedRoles);
        if (e.target.checked) set.add(e.target.dataset.role);
        else set.delete(e.target.dataset.role);
        S.selectedSkill.allowed_roles = [...set];
        renderSkillDetail();
    }));
    U.skillDetail.querySelectorAll(".skill-file").forEach((btn) => btn.addEventListener("click", () => {
        const editor = document.getElementById("skill-editor");
        if (editor && S.selectedSkillFile) S.skillContents[S.selectedSkillFile] = editor.value;
        S.selectedSkillFile = btn.dataset.file;
        renderSkillDetail();
    }));
    renderSkillActions();
}

async function loadSkills() {
    U.skillList.innerHTML = '<div class="empty-state">Loading skills...</div>';
    try {
        S.skills = await ApiClient.getSkills(0, 300);
        if (S.selectedSkill) {
            const next = S.skills.find((skill) => skill.skill_id === S.selectedSkill.skill_id);
            if (next) S.selectedSkill = next;
            else clearSkillSelection();
        }
        renderSkills();
        renderSkillDetail();
    } catch (e) {
        U.skillList.innerHTML = `<div class="empty-state error">Failed to load skills: ${esc(e.message)}</div>`;
        addNotice({ kind: "resource_failed", title: "Skill load failed", text: e.message || "Unknown error" });
    } finally {
        renderSkillActions();
    }
}

async function openSkill(skillId) {
    setDrawerOpen(U.skillBackdrop, U.skillDrawer, true);
    U.skillEmpty.style.display = "none";
    U.skillDetail.innerHTML = '<div class="empty-state">Loading skill details...</div>';
    try {
        const [skill, files] = await Promise.all([ApiClient.getSkill(skillId), ApiClient.getSkillFiles(skillId)]);
        S.selectedSkill = skill;
        S.skillFiles = files;
        S.selectedSkillFile = files[0]?.file_key || "";
        S.skillContents = {};
        await Promise.all(files.map(async (file) => {
            const data = await ApiClient.getSkillFile(skillId, file.file_key);
            S.skillContents[file.file_key] = data.content || "";
        }));
        renderSkills();
        renderSkillDetail();
    } catch (e) {
        U.skillDetail.innerHTML = `<div class="empty-state error">Failed to load skill details: ${esc(e.message)}</div>`;
        addNotice({ kind: "resource_failed", title: "Skill detail failed", text: e.message || "Unknown error" });
    } finally {
        renderSkillActions();
    }
}

async function saveSkill() {
    if (!S.selectedSkill) {
        addNotice({ kind: "resource_failed", title: "No skill selected", text: "Select a skill before saving." });
        return;
    }
    S.skillBusy = true;
    renderSkillActions();
    try {
        const editor = document.getElementById("skill-editor");
        if (editor && S.selectedSkillFile) S.skillContents[S.selectedSkillFile] = editor.value;
        for (const [key, content] of Object.entries(S.skillContents)) {
            await ApiClient.saveSkillFile(S.selectedSkill.skill_id, key, content);
        }
        await ApiClient.updateSkillPolicy(S.selectedSkill.skill_id, {
            enabled: !!S.selectedSkill.enabled,
            allowed_roles: S.selectedSkill.allowed_roles || [],
        });
        const selectedId = S.selectedSkill.skill_id;
        await loadSkills();
        await openSkill(selectedId);
        addNotice({ kind: "resource_saved", title: "Skill saved", text: S.selectedSkill.display_name || selectedId });
    } catch (e) {
        addNotice({ kind: "resource_failed", title: "Skill save failed", text: e.message || "Unknown error" });
    } finally {
        S.skillBusy = false;
        renderSkillActions();
    }
}

function renderToolDetail() {
    if (!S.selectedTool) {
        U.toolEmpty.style.display = "block";
        U.toolDetail.innerHTML = "";
        setDrawerOpen(U.toolBackdrop, U.toolDrawer, false);
        renderToolActions();
        return;
    }
    U.toolEmpty.style.display = "none";
    setDrawerOpen(U.toolBackdrop, U.toolDrawer, true);
    const roles = ["ceo", "execution", "inspection"];
    U.toolDetail.innerHTML = `<article class="resource-detail-card"><div class="panel-header"><div><h2>${esc(S.selectedTool.display_name)}</h2><p class="subtitle">${esc(S.selectedTool.tool_id)}</p></div></div><label class="role-toggle checked"><input id="tool-enabled" type="checkbox" ${S.selectedTool.enabled ? "checked" : ""}><span>已启用</span></label><div class="resource-section"><h3>动作矩阵</h3><div class="matrix-table"><table><thead><tr><th>动作</th><th>风险</th>${roles.map((r) => `<th>${esc(roleLabel(r))}</th>`).join("")}</tr></thead><tbody>${(S.selectedTool.actions || []).map((a) => `<tr><td>${esc(a.label || a.action_id)}</td><td>${esc(a.risk_level || "medium")}</td>${roles.map((r) => `<td><input type="checkbox" class="tool-role" data-action="${esc(a.action_id)}" data-role="${r}" ${a.allowed_roles?.includes(r) ? "checked" : ""}></td>`).join("")}</tr>`).join("")}</tbody></table></div></div></article>`;
    U.toolDetail.querySelector("#tool-enabled")?.addEventListener("change", (e) => { S.selectedTool.enabled = !!e.target.checked; });
    U.toolDetail.querySelectorAll(".tool-role").forEach((cb) => cb.addEventListener("change", (e) => {
        const action = S.selectedTool.actions.find((item) => item.action_id === e.target.dataset.action);
        if (!action) return;
        const set = new Set(action.allowed_roles || []);
        if (e.target.checked) set.add(e.target.dataset.role);
        else set.delete(e.target.dataset.role);
        action.allowed_roles = [...set];
    }));
    renderToolActions();
}

async function loadTools() {
    U.toolList.innerHTML = '<div class="empty-state">Loading tools...</div>';
    try {
        S.tools = await ApiClient.getTools(0, 300);
        if (S.selectedTool) {
            const next = S.tools.find((tool) => tool.tool_id === S.selectedTool.tool_id);
            if (next) S.selectedTool = next;
            else clearToolSelection();
        }
        renderTools();
        renderToolDetail();
    } catch (e) {
        U.toolList.innerHTML = `<div class="empty-state error">Failed to load tools: ${esc(e.message)}</div>`;
        addNotice({ kind: "resource_failed", title: "Tool load failed", text: e.message || "Unknown error" });
    } finally {
        renderToolActions();
    }
}

async function openTool(toolId) {
    setDrawerOpen(U.toolBackdrop, U.toolDrawer, true);
    U.toolEmpty.style.display = "none";
    U.toolDetail.innerHTML = '<div class="empty-state">Loading tool details...</div>';
    try {
        S.selectedTool = await ApiClient.getTool(toolId);
        renderTools();
        renderToolDetail();
    } catch (e) {
        U.toolDetail.innerHTML = `<div class="empty-state error">Failed to load tool details: ${esc(e.message)}</div>`;
        addNotice({ kind: "resource_failed", title: "Tool detail failed", text: e.message || "Unknown error" });
    } finally {
        renderToolActions();
    }
}

async function saveTool() {
    if (!S.selectedTool) {
        addNotice({ kind: "resource_failed", title: "No tool selected", text: "Select a tool before saving." });
        return;
    }
    S.toolBusy = true;
    renderToolActions();
    try {
        await ApiClient.updateToolPolicy(S.selectedTool.tool_id, {
            enabled: !!S.selectedTool.enabled,
            actions: (S.selectedTool.actions || []).map((a) => ({
                action_id: a.action_id,
                allowed_roles: a.allowed_roles || [],
            })),
        });
        const selectedId = S.selectedTool.tool_id;
        await loadTools();
        await openTool(selectedId);
        addNotice({ kind: "resource_saved", title: "Tool saved", text: S.selectedTool.display_name || selectedId });
    } catch (e) {
        addNotice({ kind: "resource_failed", title: "Tool save failed", text: e.message || "Unknown error" });
    } finally {
        S.toolBusy = false;
        renderToolActions();
    }
}

async function refreshSkills() {
    const selectedId = S.selectedSkill?.skill_id || "";
    S.skillBusy = true;
    renderSkillActions();
    try {
        await ApiClient.reloadResources();
        await loadSkills();
        if (selectedId && S.skills.some((skill) => skill.skill_id === selectedId)) {
            await openSkill(selectedId);
        }
        addNotice({ kind: "resource_refreshed", title: "Skills refreshed", text: "Resource registry reloaded." });
    } catch (e) {
        addNotice({ kind: "resource_failed", title: "Skill refresh failed", text: e.message || "Unknown error" });
    } finally {
        S.skillBusy = false;
        renderSkillActions();
    }
}

async function refreshTools() {
    const selectedId = S.selectedTool?.tool_id || "";
    S.toolBusy = true;
    renderToolActions();
    try {
        await ApiClient.reloadResources();
        await loadTools();
        if (selectedId && S.tools.some((tool) => tool.tool_id === selectedId)) {
            await openTool(selectedId);
        }
        addNotice({ kind: "resource_refreshed", title: "Tools refreshed", text: "Resource registry reloaded." });
    } catch (e) {
        addNotice({ kind: "resource_failed", title: "Tool refresh failed", text: e.message || "Unknown error" });
    } finally {
        S.toolBusy = false;
        renderToolActions();
    }
}

function switchView(view) {
    const map = { ceo: U.viewCeo, projects: U.viewProjects, skills: U.viewSkills, tools: U.viewTools, models: U.viewModels, "project-details": U.viewProjectDetails };
    const navView = view === "project-details" ? "projects" : view;
    U.nav.forEach((btn) => btn.classList.toggle("active", btn.dataset.view === navView));
    Object.entries(map).forEach(([key, el]) => {
        if (el) el.style.display = key === view ? (key === "ceo" || key === "project-details" || key === "models" ? "flex" : "block") : "none";
    });
    if (view !== "project-details" && S.projectWs) {
        S.projectWs.close();
        S.projectWs = null;
    }
    if (view !== "skills") setDrawerOpen(U.skillBackdrop, U.skillDrawer, false);
    if (view !== "tools") setDrawerOpen(U.toolBackdrop, U.toolDrawer, false);
    S.view = view;
    if (view === "projects") void loadProjects();
    if (view === "skills") void loadSkills();
    if (view === "tools") void loadTools();
    if (view === "models") void loadModels();
}

function bind() {
    U.theme?.addEventListener("click", toggleTheme);
    U.nav.forEach((btn) => btn.addEventListener("click", () => switchView(btn.dataset.view)));
    U.backToProjects?.addEventListener("click", () => switchView("projects"));
    U.ceoSend?.addEventListener("click", sendCeoMessage);
    U.ceoInput?.addEventListener("keydown", (e) => {
        if (e.key === "Enter" && !e.shiftKey) {
            e.preventDefault();
            sendCeoMessage();
        }
    });
    U.modelRefresh?.addEventListener("click", () => void loadModels());
    U.modelCreate?.addEventListener("click", startCreateModel);
    U.modelRolesSave?.addEventListener("click", () => void persistModelRoleChains(MODEL_SCOPES.map((item) => item.key)));
    U.modelSearch?.addEventListener("input", (e) => {
        S.modelCatalog.search = String(e.target.value || "");
        renderModelList();
    });
    U.modelList?.addEventListener("click", (e) => {
        const button = e.target.closest("[data-model-key]");
        if (!button) return;
        openModel(button.dataset.modelKey);
    });
    U.modelRoleEditors?.addEventListener("click", (e) => {
        const action = e.target.closest("[data-model-chain-action]");
        if (action) {
            const scope = String(action.dataset.scope || "");
            const index = Number(action.dataset.index || -1);
            const chain = modelScopeChain(scope);
            if (!scope || index < 0 || index >= chain.length) return;
            if (action.dataset.modelChainAction === "remove") {
                chain.splice(index, 1);
            } else if (action.dataset.modelChainAction === "up" && index > 0) {
                [chain[index - 1], chain[index]] = [chain[index], chain[index - 1]];
            } else if (action.dataset.modelChainAction === "down" && index < chain.length - 1) {
                [chain[index], chain[index + 1]] = [chain[index + 1], chain[index]];
            }
            updateRoleChainDraft(scope, chain);
            return;
        }
        const add = e.target.closest("[data-model-role-add]");
        if (!add) return;
        const scope = String(add.dataset.modelRoleAdd || "");
        const select = U.modelRoleEditors.querySelector(`[data-model-role-select="${scope}"]`);
        const value = String(select?.value || "").trim();
        if (!scope || !value) return;
        updateRoleChainDraft(scope, [...modelScopeChain(scope), value]);
        select.value = "";
    });
    U.modelDetail?.addEventListener("submit", (e) => {
        if (e.target?.id !== "model-detail-form") return;
        e.preventDefault();
        void saveModelDetail();
    });
    U.modelDetail?.addEventListener("click", (e) => {
        const cancel = e.target.closest("[data-model-detail-cancel]");
        if (!cancel) return;
        S.modelCatalog.mode = "view";
        renderModelCatalog();
    });
    U.modelDetail?.addEventListener("change", (e) => {
        const toggle = e.target.closest(".role-toggle");
        if (toggle && e.target instanceof HTMLInputElement && e.target.type === "checkbox") {
            toggle.classList.toggle("checked", e.target.checked);
        }
    });
    U.selectActive?.addEventListener("click", () => {
        S.selectedProjects = new Set(S.projects.filter((p) => canPause(p.status)).map((p) => p.project_id));
        renderProjects();
    });
    U.selectBlocked?.addEventListener("click", () => {
        S.selectedProjects = new Set(S.projects.filter((p) => canResume(p.status)).map((p) => p.project_id));
        renderProjects();
    });
    U.selectClear?.addEventListener("click", () => {
        S.selectedProjects.clear();
        renderProjects();
    });
    U.pauseBatch?.addEventListener("click", async () => {
        for (const id of [...S.selectedProjects]) await runProjectAction(id, "pause");
    });
    U.resumeBatch?.addEventListener("click", async () => {
        for (const id of [...S.selectedProjects]) await runProjectAction(id, "resume");
    });
    U.closeAgent?.addEventListener("click", () => {
        S.selectedUnitId = null;
        U.feedTitle.textContent = "项目全局动态 / 详情";
        hideAgent();
        renderTree();
        renderFeed();
    });
    [U.skillSearch, U.skillRisk, U.skillStatus, U.skillLegacy].forEach((el) => el?.addEventListener(el.tagName === "INPUT" ? "input" : "change", renderSkills));
    U.skillRefresh?.addEventListener("click", () => void refreshSkills());
    U.skillSave?.addEventListener("click", () => void saveSkill());
    [U.toolSearch, U.toolStatus, U.toolRisk].forEach((el) => el?.addEventListener(el.tagName === "INPUT" ? "input" : "change", renderTools));
    U.toolRefresh?.addEventListener("click", () => void refreshTools());
    U.toolSave?.addEventListener("click", () => void saveTool());
    U.skillBackdrop?.addEventListener("click", clearSkillSelection);
    U.toolBackdrop?.addEventListener("click", clearToolSelection);
    document.addEventListener("keydown", (e) => {
        if (e.key !== "Escape") return;
        if (S.selectedSkill) clearSkillSelection();
        if (S.selectedTool) clearToolSelection();
    });
}

function init() {
    bind();
    icons();
    renderSkillActions();
    renderToolActions();
    void loadModels();
    void loadNotices();
    initCeoWs();
}

document.addEventListener("DOMContentLoaded", init);
