const MODEL_SCOPES = [
    { key: "ceo", label: "CEO" },
    { key: "execution", label: "执行" },
    { key: "inspection", label: "检验" },
];

const EMPTY_MODEL_ROLES = () => ({ ceo: [], execution: [], inspection: [] });
const DEFAULT_MODEL_DEFAULTS = () => ({ ceo: "", execution: "", inspection: "" });
const cloneModelRoles = (roles = EMPTY_MODEL_ROLES()) => {
    const next = EMPTY_MODEL_ROLES();
    MODEL_SCOPES.forEach(({ key }) => {
        next[key] = Array.isArray(roles?.[key])
            ? roles[key].map((item) => String(item || "").trim()).filter(Boolean)
            : [];
    });
    return next;
};

const S = {
    view: "ceo",
    ceoWs: null,
    projectWs: null,
    currentProjectId: null,
    projects: [],
    selectedProjects: new Set(),
    multiSelectMode: false,
    projectSelectMenuOpen: false,
    projectBatchMenuOpen: false,
    projectBusy: false,
    confirmState: null,
    toastState: { timeoutId: null, intervalId: null, remaining: 0 },
    resourceSaveTimers: { skill: null, tool: null },
    modelCatalog: {
        items: [],
        catalog: [],
        roles: EMPTY_MODEL_ROLES(),
        roleDrafts: EMPTY_MODEL_ROLES(),
        defaults: DEFAULT_MODEL_DEFAULTS(),
        loading: false,
        saving: false,
        error: "",
        search: "",
        selectedModelKey: "",
        mode: "view",
        roleEditing: false,
        rolesDirty: false,
        dragState: null,
    },
    tree: null,
    selectedUnitId: null,
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
    modelRolesCancel: document.getElementById("model-roles-cancel-btn"),
    modelRolesSave: document.getElementById("model-roles-save-btn"),
    modelRoleEditors: document.getElementById("model-role-editors"),
    modelSearch: document.getElementById("model-search-input"),
    modelList: document.getElementById("model-list"),
    modelDetailEmpty: document.getElementById("model-detail-empty"),
    modelDetail: document.getElementById("model-detail-content"),
    modelBackdrop: document.getElementById("model-detail-backdrop"),
    modelDrawer: document.querySelector(".model-detail-dialog"),
    projectGrid: document.getElementById("project-card-grid"),
    projectSummary: document.getElementById("project-selection-summary"),
    projectMultiToggle: document.getElementById("project-multi-toggle"),
    projectSelectWrap: document.getElementById("project-select-wrap"),
    projectSelectTrigger: document.getElementById("project-select-menu-trigger"),
    projectSelectMenu: document.getElementById("project-select-menu"),
    projectBatchWrap: document.getElementById("project-batch-wrap"),
    projectBatchTrigger: document.getElementById("project-batch-menu-trigger"),
    projectBatchMenu: document.getElementById("project-batch-menu"),
    backToProjects: document.getElementById("back-to-projects"),
    pdTitle: document.getElementById("pd-title"),
    pdStatus: document.getElementById("pd-status"),
    pdSummary: document.getElementById("pd-summary"),
    pdActiveCount: document.getElementById("pd-active-count"),
    tree: document.getElementById("org-tree-container"),
    feedTitle: document.getElementById("feed-target-name"),
    detail: document.getElementById("agent-detail-view"),
    adRole: document.getElementById("ad-role"),
    adStatus: document.getElementById("ad-status"),
    adInput: document.getElementById("ad-input"),
    adOutput: document.getElementById("ad-output"),
    adCheck: document.getElementById("ad-check"),
    adLogs: document.getElementById("ad-logs"),
    nodeEmpty: document.getElementById("project-node-empty"),
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
    toast: document.getElementById("app-toast"),
    toastTitle: document.getElementById("app-toast-title"),
    toastText: document.getElementById("app-toast-text"),
    toastProgress: document.getElementById("app-toast-progress"),
    toastProgressBar: document.getElementById("app-toast-progress-bar"),
    toastClose: document.getElementById("app-toast-close"),
    confirmBackdrop: document.getElementById("confirm-backdrop"),
    confirmTitle: document.getElementById("confirm-title"),
    confirmText: document.getElementById("confirm-text"),
    confirmCancel: document.getElementById("confirm-cancel"),
    confirmAccept: document.getElementById("confirm-accept"),
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

async function loadCeoHistory() {
    if (!U.ceoFeed) return;
    try {
        const items = await ApiClient.getCeoHistory(200);
        U.ceoFeed.innerHTML = "";
        items.forEach((item) => addMsg(item.text || "", item.role === "user" ? "user" : "system"));
    } catch (e) {
        console.error("加载 CEO 历史失败:", e);
    }
}

function addNotice(notice, bump = true) {
    const li = document.createElement("li");
    li.className = `notice-item ${String(notice.kind || "").includes("fail") ? "error" : "success"}`;
    li.innerHTML = `<div class="notice-title">${esc(notice.title || "系统通知")}</div><div class="notice-text">${esc(notice.text || "")}</div>`;
    U.noticeList.prepend(li);
    if (bump) U.noticeBadge.textContent = String(Number(U.noticeBadge.textContent || 0) + 1);
}


function clearToastTimers() {
    if (S.toastState.timeoutId) window.clearTimeout(S.toastState.timeoutId);
    if (S.toastState.intervalId) window.clearInterval(S.toastState.intervalId);
    S.toastState.timeoutId = null;
    S.toastState.intervalId = null;
}

function closeToast() {
    clearToastTimers();
    if (!U.toast) return;
    U.toast.hidden = true;
    U.toast.className = "app-toast";
    if (U.toastClose) U.toastClose.hidden = false;
    if (U.toastProgressBar) {
        U.toastProgressBar.className = "app-toast-progress-bar";
        U.toastProgressBar.style.transition = "none";
        U.toastProgressBar.style.transform = "scaleX(1)";
    }
}

function showToast({ title = "操作成功", text = "修改已生效", kind = "success", durationMs = 3000, persistent = false } = {}) {
    if (!U.toast || !U.toastTitle || !U.toastText || !U.toastProgress || !U.toastProgressBar || !U.toastClose) return;
    clearToastTimers();
    const sticky = persistent || durationMs <= 0;
    U.toastTitle.textContent = title;
    U.toastText.textContent = text;
    U.toast.hidden = false;
    U.toast.setAttribute("role", kind === "error" ? "alert" : "status");
    U.toastClose.hidden = false;
    U.toastProgress.hidden = false;
    U.toastProgressBar.className = "app-toast-progress-bar";
    U.toastProgressBar.style.transition = "none";
    U.toastProgressBar.style.transform = "scaleX(1)";
    if (sticky) {
        U.toastProgressBar.classList.add("is-indeterminate");
    } else {
        window.requestAnimationFrame(() => {
            window.requestAnimationFrame(() => {
                U.toastProgressBar.style.transition = `transform ${durationMs}ms linear`;
                U.toastProgressBar.style.transform = "scaleX(0)";
            });
        });
        S.toastState.timeoutId = window.setTimeout(closeToast, durationMs);
    }
    U.toast.className = `app-toast is-open is-${kind}`;
    icons();
}

function queueResourceSave(_kind) {
}

function openConfirm({ title, text, confirmLabel = "确认", confirmKind = "danger", onConfirm, returnFocus = null }) {
    S.confirmState = { onConfirm, returnFocus };
    U.confirmTitle.textContent = title;
    U.confirmText.textContent = text;
    U.confirmAccept.textContent = confirmLabel;
    U.confirmAccept.className = `toolbar-btn ${confirmKind}`;
    U.confirmBackdrop.hidden = false;
    U.confirmBackdrop.classList.add("is-open");
    window.requestAnimationFrame(() => U.confirmCancel?.focus());
}

function closeConfirm({ restoreFocus = true } = {}) {
    const returnFocus = S.confirmState?.returnFocus;
    S.confirmState = null;
    U.confirmBackdrop.hidden = true;
    U.confirmBackdrop.classList.remove("is-open");
    if (restoreFocus) returnFocus?.focus?.();
}

async function acceptConfirm() {
    if (!S.confirmState?.onConfirm) return;
    U.confirmAccept.disabled = true;
    U.confirmCancel.disabled = true;
    try {
        await S.confirmState.onConfirm();
        closeConfirm();
    } finally {
        U.confirmAccept.disabled = false;
        U.confirmCancel.disabled = false;
    }
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

function activeModelRoles() {
    return S.modelCatalog.roleEditing ? S.modelCatalog.roleDrafts : S.modelCatalog.roles;
}

function modelScopeChain(scope, source = "active") {
    const roles = source === "draft"
        ? S.modelCatalog.roleDrafts
        : source === "committed"
            ? S.modelCatalog.roles
            : activeModelRoles();
    return Array.isArray(roles?.[scope]) ? [...roles[scope]] : [];
}

function modelScopeContains(scope, ref, source = "active") {
    return modelScopeChain(scope, source).some((item) => modelRefEquivalent(item, ref));
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

function normalizeAllModelRoles(roles = EMPTY_MODEL_ROLES()) {
    const next = EMPTY_MODEL_ROLES();
    MODEL_SCOPES.forEach(({ key }) => {
        next[key] = normalizeModelRoleChain(Array.isArray(roles?.[key]) ? roles[key] : []);
    });
    return next;
}

function modelRolesEqual(left, right) {
    return MODEL_SCOPES.every(({ key }) => {
        const leftChain = normalizeModelRoleChain(left?.[key] || []);
        const rightChain = normalizeModelRoleChain(right?.[key] || []);
        if (leftChain.length !== rightChain.length) return false;
        return leftChain.every((item, index) => modelRefEquivalent(item, rightChain[index]));
    });
}

function syncModelRoleDraftState() {
    S.modelCatalog.rolesDirty = !!S.modelCatalog.roleEditing && !modelRolesEqual(S.modelCatalog.roleDrafts, S.modelCatalog.roles);
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
    const rawCatalog = Array.isArray(payload.catalog) ? payload.catalog : [];
    const normalizedCatalog = [];
    const seenCatalog = new Set();
    rawCatalog.forEach((item) => {
        if (!item || typeof item !== "object") return;
        const key = String(item.key || "").trim();
        const providerModel = String(item.provider_model || "").trim();
        const dedupeKey = key || providerModel;
        if (!dedupeKey || seenCatalog.has(dedupeKey)) return;
        seenCatalog.add(dedupeKey);
        normalizedCatalog.push({ ...item });
    });
    S.modelCatalog.items = Array.isArray(payload.items) ? payload.items.map((item) => String(item || "").trim()).filter(Boolean) : [];
    S.modelCatalog.catalog = normalizedCatalog;
    S.modelCatalog.roles = normalizeAllModelRoles(nextRoles);
    if (preserveRoleDrafts && S.modelCatalog.roleEditing) {
        S.modelCatalog.roleDrafts = normalizeAllModelRoles(S.modelCatalog.roleDrafts);
        syncModelRoleDraftState();
    } else {
        S.modelCatalog.roleDrafts = cloneModelRoles(S.modelCatalog.roles);
        S.modelCatalog.roleEditing = false;
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
    if (S.modelCatalog.saving) return hint("正在保存...");
    if (S.modelCatalog.error) return hint(`模型配置错误：${S.modelCatalog.error}`, true);
    if (!S.modelCatalog.catalog.length) return hint("当前还没有模型，请先添加模型。", false);
    if (S.modelCatalog.roleEditing && S.modelCatalog.rolesDirty) return hint("正在修改模型链，请点击“保存”应用修改。", false);
    if (S.modelCatalog.roleEditing) return hint("已进入模型链编辑模式，可拖动、移除或加入模型后再点击“保存”。", false);
    return hint("点击“修改模型链”后再调整角色链；点击模型可打开配置弹窗。", false);
}

function renderModelRoleEditors() {
    if (!U.modelRoleEditors) return;
    const editing = !!S.modelCatalog.roleEditing;
    U.modelRoleEditors.innerHTML = MODEL_SCOPES.map((scope) => {
        const chain = modelScopeChain(scope.key);
        const defaultText = S.modelCatalog.defaults[scope.key]
            ? `当前首选 ${S.modelCatalog.defaults[scope.key]}`
            : "未配置当前首选";
        const chainMarkup = chain.length
            ? chain.map((ref, index) => {
                const item = modelRefItem(ref);
                const modelKey = String(item?.key || ref).trim();
                const badges = [index === 0 ? '<span class="policy-chip risk-low">首选</span>' : ""];
                if (item?.enabled === false) badges.push('<span class="policy-chip neutral">已禁用</span>');
                if (!item) badges.push('<span class="policy-chip neutral">未托管</span>');
                return `
                    <article class="model-chain-slide${editing ? " is-editing" : ""}"${editing ? ' draggable="true"' : ''} data-model-chain-ref="${esc(modelKey)}" data-scope="${scope.key}">
                        ${editing ? '<button type="button" class="model-chain-handle" aria-label="拖动调整顺序"><span class="model-chain-grip" aria-hidden="true">&#9776;</span></button>' : ''}
                        <button type="button" class="model-chain-main" data-model-open="${esc(modelKey)}">
                            <span class="resource-list-title">${esc(modelKey)}</span>
                            <span class="resource-list-subtitle">${esc(item?.provider_model || ref)}</span>
                            <span class="model-inline-meta">${badges.join("")}</span>
                        </button>
                        ${editing ? `<button type="button" class="toolbar-btn ghost small" data-model-chain-action="remove" data-scope="${scope.key}" data-index="${index}">移除</button>` : ''}
                    </article>`;
            }).join("")
            : `<div class="empty-state compact">${editing ? '从下方共享模型列表拖入，构建当前角色链。' : '点击“修改模型链”后再调整当前角色链。'}</div>`;

        return `
            <section class="model-chain-card">
                <div class="panel-header">
                    <div>
                        <h3>${esc(scope.label)}</h3>
                        <p class="subtitle">${esc(defaultText)}</p>
                    </div>
                    <span class="policy-chip neutral">${chain.length} 个候选</span>
                </div>
                <div class="model-role-section">
                    <div class="model-role-section-title">当前角色链</div>
                    <div class="model-chain-list" data-model-chain-list="${scope.key}">${chainMarkup}</div>
                </div>
            </section>`;
    }).join("");
}

function renderModelList() {
    if (!U.modelList) return;
    const editing = !!S.modelCatalog.roleEditing;
    const catalog = filterModels().sort((left, right) => String(left.key || "").localeCompare(String(right.key || "")));
    if (!catalog.length) {
        const emptyText = S.modelCatalog.search
            ? "没有匹配的模型，请调整搜索条件。"
            : "暂无可用模型，请先添加模型。";
        U.modelList.innerHTML = `<div class="empty-state compact">${emptyText}</div>`;
        return;
    }
    U.modelList.innerHTML = catalog.map((item) => {
        const usedScopes = MODEL_SCOPES.filter((scope) => modelScopeContains(scope.key, item.key));
        const usageMarkup = usedScopes.length
            ? '<span class="policy-chip neutral">已加入角色链</span>'
            : '<span class="policy-chip neutral">未加入角色链</span>';
        const stateChips = [usageMarkup];
        if (item.enabled === false) stateChips.push('<span class="policy-chip neutral">已禁用</span>');
        return `
            <article class="model-available-item ${usedScopes.length ? "is-in-chain" : ""}"${editing ? ' draggable="true"' : ''} data-model-available-key="${esc(item.key)}">
                <div class="model-shared-item-head">
                    <button type="button" class="model-available-main" data-model-open="${esc(item.key)}">
                        <span class="resource-list-title">${esc(item.key)}</span>
                        <span class="resource-list-subtitle">${esc(item.provider_model)}</span>
                    </button>
                    <button type="button" class="toolbar-btn ghost small" data-model-open="${esc(item.key)}">配置</button>
                </div>
                <div class="model-inline-meta">${stateChips.join("")}</div>
                <div class="resource-empty-copy">${editing ? '拖动当前模型到上方任意角色链即可加入。' : '点击“修改模型链”后，可拖动当前模型到任意角色链。'}</div>
            </article>`;
    }).join("");
}

function renderModelDetail() {
    if (!U.modelDetail || !U.modelDetailEmpty) return;
    const isCreate = S.modelCatalog.mode === "create";
    const current = isCreate ? null : modelRefItem(S.modelCatalog.selectedModelKey);
    if (!isCreate && !current) {
        U.modelDetailEmpty.style.display = "none";
        U.modelDetail.innerHTML = "";
        setDrawerOpen(U.modelBackdrop, U.modelDrawer, false);
        return;
    }

    const enabled = isCreate ? true : !!current?.enabled;

    U.modelDetailEmpty.style.display = "none";
    setDrawerOpen(U.modelBackdrop, U.modelDrawer, true);
    U.modelDetail.innerHTML = `
        <article class="model-detail-card model-config-shell">
            <div class="detail-modal-header model-config-header">
                <div class="detail-modal-title">
                    <h2 id="model-detail-title">${isCreate ? "添加模型" : "模型配置"}</h2>
                    <p class="subtitle">${esc(isCreate ? "填写必填项后写入 .g3ku/config.json" : `${current.key} · ${current.provider_model}`)}</p>
                </div>
                <div class="detail-modal-actions">
                    <span class="policy-chip ${enabled ? "risk-low" : "neutral"}">${enabled ? "已启用" : "已禁用"}</span>
                    <button type="submit" form="model-detail-form" class="toolbar-btn success">保存</button>
                    <button type="button" class="toolbar-btn ghost" data-model-detail-cancel="1" data-modal-close>关闭</button>
                </div>
            </div>
            <div class="detail-modal-body model-config-body">
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
                        <h3>额外请求头</h3>
                        <textarea class="resource-editor model-textarea" name="extraHeaders" rows="6" placeholder='{"X-Trace-Id": "demo"}'>${esc(current?.extra_headers ? JSON.stringify(current.extra_headers, null, 2) : "")}</textarea>
                    </section>
                    <section class="resource-section">
                        <h3>说明</h3>
                        <textarea class="resource-editor model-textarea" name="description" rows="5" placeholder="可填写用途、限制、成本等说明">${esc(current?.description || "")}</textarea>
                    </section>
                    <div class="model-actions">
                        <button type="submit" class="toolbar-btn success">${isCreate ? "添加并保存" : "保存模型"}</button>
                        <button type="button" class="toolbar-btn ghost" data-model-detail-cancel="1">${isCreate ? "取消" : "关闭"}</button>
                    </div>
                </form>
            </div>
        </article>`;
}

function renderModelCatalog() {
    if (U.modelRefresh) U.modelRefresh.disabled = S.modelCatalog.loading || S.modelCatalog.saving;
    if (U.modelCreate) U.modelCreate.disabled = S.modelCatalog.loading || S.modelCatalog.saving;
    if (U.modelRolesCancel) {
        U.modelRolesCancel.hidden = !S.modelCatalog.roleEditing;
        U.modelRolesCancel.disabled = S.modelCatalog.loading || S.modelCatalog.saving;
    }
    if (U.modelRolesSave) {
        U.modelRolesSave.disabled = S.modelCatalog.loading || S.modelCatalog.saving;
        U.modelRolesSave.textContent = S.modelCatalog.saving
            ? "正在保存..."
            : S.modelCatalog.roleEditing
                ? "保存"
                : "修改模型链";
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
        applyModelCatalog(data, { preserveRoleDrafts: !!S.modelCatalog.roleEditing });
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

function clearModelSelection() {
    S.modelCatalog.mode = "view";
    S.modelCatalog.selectedModelKey = "";
    renderModelCatalog();
}


function clearModelDragDecorations() {
    [U.modelRoleEditors, U.modelList].filter(Boolean).forEach((root) => {
        root.querySelectorAll('.is-drop-target').forEach((item) => item.classList.remove('is-drop-target'));
        root.querySelectorAll('.is-drop-zone').forEach((item) => item.classList.remove('is-drop-zone'));
        root.querySelectorAll('[data-model-drop-placeholder]').forEach((item) => item.remove());
    });
}

function beginModelDrag(item, { scope = "", ref = "", source = "available" } = {}, dataTransfer = null) {
    const modelRef = String(ref || "").trim();
    if (!S.modelCatalog.roleEditing || !item || !modelRef) return false;
    S.modelCatalog.dragState = {
        scope: String(scope || ""),
        ref: modelRef,
        source,
        scrollFrameId: null,
        scrollTarget: null,
        scrollStep: 0,
    };
    item.classList.add("is-dragging");
    clearModelDragDecorations();
    if (dataTransfer) {
        dataTransfer.effectAllowed = source === "chain" ? "move" : "copyMove";
        dataTransfer.setData("text/plain", modelRef);
    }
    return true;
}

function finishModelDrag() {
    stopModelAutoScroll();
    [U.modelRoleEditors, U.modelList].filter(Boolean).forEach((root) => {
        root.querySelectorAll(".model-chain-slide.is-dragging, .model-available-item.is-dragging").forEach((item) => item.classList.remove("is-dragging"));
    });
    S.modelCatalog.dragState = null;
    clearModelDragDecorations();
}

function stopModelAutoScroll() {
    const dragState = S.modelCatalog.dragState;
    if (!dragState) return;
    if (dragState.scrollFrameId) window.cancelAnimationFrame(dragState.scrollFrameId);
    dragState.scrollFrameId = null;
    dragState.scrollTarget = null;
    dragState.scrollStep = 0;
}

function startModelAutoScroll(target, clientY) {
    const dragState = S.modelCatalog.dragState;
    if (!dragState || !target) return;
    const rect = target.getBoundingClientRect();
    const threshold = Math.min(48, rect.height / 4);
    let step = 0;
    if (clientY < rect.top + threshold) {
        step = -Math.max(6, Math.round((rect.top + threshold - clientY) / 5));
    } else if (clientY > rect.bottom - threshold) {
        step = Math.max(6, Math.round((clientY - (rect.bottom - threshold)) / 5));
    }
    if (!step) {
        if (dragState.scrollTarget === target) stopModelAutoScroll();
        return;
    }
    dragState.scrollTarget = target;
    dragState.scrollStep = step;
    if (dragState.scrollFrameId) return;
    const tick = () => {
        const state = S.modelCatalog.dragState;
        if (!state?.scrollTarget || !state.scrollStep) {
            stopModelAutoScroll();
            return;
        }
        state.scrollTarget.scrollTop += state.scrollStep;
        state.scrollFrameId = window.requestAnimationFrame(tick);
    };
    dragState.scrollFrameId = window.requestAnimationFrame(tick);
}

function ensureModelDropPlaceholder(list, targetItem, clientY) {
    if (!list) return null;
    const placeholder = document.createElement('div');
    placeholder.className = 'model-chain-drop-placeholder';
    placeholder.dataset.modelDropPlaceholder = '1';
    list.classList.add('is-drop-zone');
    if (targetItem && targetItem.parentElement === list) {
        targetItem.classList.add('is-drop-target');
        const rect = targetItem.getBoundingClientRect();
        const insertBefore = clientY < rect.top + (rect.height / 2);
        placeholder.dataset.dropPosition = insertBefore ? 'before' : 'after';
        targetItem.dataset.dropPosition = insertBefore ? 'before' : 'after';
        list.insertBefore(placeholder, insertBefore ? targetItem : targetItem.nextSibling);
    } else {
        placeholder.dataset.dropPosition = 'append';
        list.appendChild(placeholder);
    }
    return placeholder;
}

function highlightModelAvailableZone(list, targetItem = null) {
    if (!list) return;
    list.classList.add('is-drop-zone');
    if (targetItem && targetItem.parentElement === list) {
        targetItem.classList.add('is-drop-target');
    }
}

function moveRoleChainItem(scope, fromRef, targetIndex = null) {
    const chain = modelScopeChain(scope);
    const sourceIndex = chain.findIndex((item) => modelRefEquivalent(item, fromRef));
    if (sourceIndex < 0) return;
    const nextChain = [...chain];
    const [moving] = nextChain.splice(sourceIndex, 1);
    const boundedIndex = targetIndex === null
        ? nextChain.length
        : Math.max(0, Math.min(Number(targetIndex), nextChain.length));
    nextChain.splice(boundedIndex, 0, moving);
    updateRoleChainDraft(scope, nextChain);
}

function insertRoleChainItem(scope, modelKey, targetIndex = null) {
    const nextChain = modelScopeChain(scope).filter((item) => !modelRefEquivalent(item, modelKey));
    const boundedIndex = targetIndex === null
        ? nextChain.length
        : Math.max(0, Math.min(Number(targetIndex), nextChain.length));
    nextChain.splice(boundedIndex, 0, modelKey);
    updateRoleChainDraft(scope, nextChain);
}

function removeRoleChainItem(scope, modelKey) {
    const nextChain = modelScopeChain(scope).filter((item) => !modelRefEquivalent(item, modelKey));
    updateRoleChainDraft(scope, nextChain);
}

function updateRoleChainDraft(scope, nextChain) {
    if (!S.modelCatalog.roleEditing) return;
    S.modelCatalog.roleDrafts[scope] = normalizeModelRoleChain(nextChain);
    syncModelRoleDraftState();
    renderModelHint();
    renderModelRoleEditors();
    renderModelList();
    syncModelDetailScopeToggles();
}

function startModelRoleEditing() {
    S.modelCatalog.roleEditing = true;
    S.modelCatalog.roleDrafts = cloneModelRoles(S.modelCatalog.roles);
    syncModelRoleDraftState();
    renderModelCatalog();
}

function cancelModelRoleEditing() {
    S.modelCatalog.roleEditing = false;
    S.modelCatalog.roleDrafts = cloneModelRoles(S.modelCatalog.roles);
    S.modelCatalog.rolesDirty = false;
    finishModelDrag();
    renderModelCatalog();
    hint("已取消模型链修改。", false);
}

async function persistModelRoleChains(scopes = MODEL_SCOPES.map((item) => item.key), successText = "模型链已保存。", { useDrafts = false } = {}) {
    const targets = [...new Set(scopes.map((item) => String(item || "").trim()).filter(Boolean))];
    if (!targets.length) return;
    const roleSource = useDrafts ? S.modelCatalog.roleDrafts : S.modelCatalog.roles;
    S.modelCatalog.saving = true;
    renderModelCatalog();
    try {
        let payload = null;
        for (const scope of targets) {
            payload = await ApiClient.updateModelRoleChain(scope, normalizeModelRoleChain(roleSource[scope] || []));
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

async function handleModelRoleEditorAction() {
    if (!S.modelCatalog.roleEditing) {
        startModelRoleEditing();
        hint("已进入模型链编辑模式。");
        return;
    }
    if (!S.modelCatalog.rolesDirty) {
        cancelModelRoleEditing();
        return;
    }
    await persistModelRoleChains(MODEL_SCOPES.map((item) => item.key), "模型链已保存。", { useDrafts: true });
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
    try {
        const draft = collectModelFormData(form, current);
        const preserveRoleDrafts = !!S.modelCatalog.roleEditing;
        const enableChanged = !draft.isCreate && draft.enabled !== !!current?.enabled;
        if (!draft.isCreate && !Object.keys(draft.patch).length && !enableChanged) {
            hint("没有需要保存的更改。");
            return;
        }

        if (draft.isCreate) {
            const payload = await ApiClient.createManagedModel({ ...draft.payload, scopes: [] });
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

        hint(draft.isCreate ? "模型已添加。" : "模型配置已保存。");
        renderModelCatalog();
    } catch (e) {
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

const canDeleteSingle = () => true;
const canDeleteBatch = (v) => ["blocked", "completed", "failed", "canceled", "archived"].includes(pStatus(v));

function statusBucketMatches(project, bucketKey) {
    const status = pStatus(project?.status);
    if (bucketKey === "blocked") return status === "blocked";
    if (bucketKey === "completed") return status === "completed";
    if (bucketKey === "failed") return ["failed", "canceled"].includes(status);
    if (bucketKey === "running") return canPause(status);
    return false;
}

function getSelectedProjects() {
    return S.projects.filter((project) => S.selectedProjects.has(project.project_id));
}

function setProjectMenuVisibility() {
    const selectOpen = !!(S.multiSelectMode && S.projectSelectMenuOpen);
    const batchOpen = !!(S.multiSelectMode && S.projectBatchMenuOpen);
    if (U.projectSelectWrap) U.projectSelectWrap.hidden = !S.multiSelectMode;
    if (U.projectBatchWrap) U.projectBatchWrap.hidden = !S.multiSelectMode;
    if (U.projectSelectMenu) U.projectSelectMenu.hidden = !selectOpen;
    if (U.projectBatchMenu) U.projectBatchMenu.hidden = !batchOpen;
    U.projectSelectTrigger?.setAttribute("aria-expanded", selectOpen ? "true" : "false");
    U.projectBatchTrigger?.setAttribute("aria-expanded", batchOpen ? "true" : "false");
}

function setProjectMenuOpen(menu, open) {
    if (menu === "select") {
        S.projectSelectMenuOpen = !!open;
        if (open) S.projectBatchMenuOpen = false;
    } else {
        S.projectBatchMenuOpen = !!open;
        if (open) S.projectSelectMenuOpen = false;
    }
    setProjectMenuVisibility();
}

function closeProjectMenus() {
    S.projectSelectMenuOpen = false;
    S.projectBatchMenuOpen = false;
    setProjectMenuVisibility();
}

function setMultiSelectMode(enabled) {
    S.multiSelectMode = !!enabled;
    if (!S.multiSelectMode) {
        S.selectedProjects.clear();
        closeProjectMenus();
    }
    renderProjects();
}

function toggleProjectSelection(projectId) {
    if (S.selectedProjects.has(projectId)) S.selectedProjects.delete(projectId);
    else S.selectedProjects.add(projectId);
    renderProjects();
}

function syncProjectSelection() {
    const ids = new Set(S.projects.map((project) => project.project_id));
    [...S.selectedProjects].forEach((id) => !ids.has(id) && S.selectedProjects.delete(id));
    if (!S.multiSelectMode) S.selectedProjects.clear();
}

function updateProjectToolbar() {
    const selected = getSelectedProjects();
    U.projectSummary.textContent = `已选择 ${selected.length} 项`;
    if (U.projectMultiToggle) {
        U.projectMultiToggle.textContent = S.multiSelectMode ? "取消多选" : "多选";
        U.projectMultiToggle.setAttribute("aria-pressed", S.multiSelectMode ? "true" : "false");
        U.projectMultiToggle.disabled = S.projectBusy;
    }
    const selectButtons = [...(U.projectSelectMenu?.querySelectorAll("[data-select-bucket]") || [])];
    selectButtons.forEach((button) => {
        button.disabled = S.projectBusy || !S.projects.some((project) => statusBucketMatches(project, button.dataset.selectBucket));
    });
    const batchButtons = [...(U.projectBatchMenu?.querySelectorAll("[data-batch-action]") || [])];
    batchButtons.forEach((button) => {
        const action = button.dataset.batchAction;
        const enabled = action === "pause"
            ? selected.some((project) => canPause(project.status))
            : action === "resume"
                ? selected.some((project) => canResume(project.status))
                : selected.some((project) => canDeleteBatch(project.status));
        button.disabled = S.projectBusy;
    });
    setProjectMenuVisibility();
}

function setDrawerOpen(backdrop, drawer, open) {
    const wasOpen = !!drawer?.classList.contains("is-open");
    backdrop?.classList.toggle("is-open", open);
    drawer?.classList.toggle("is-open", open);
    backdrop?.setAttribute("aria-hidden", open ? "false" : "true");
    drawer?.setAttribute("aria-hidden", open ? "false" : "true");
    if (open && drawer && !wasOpen) {
        drawer.__returnFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;
        window.requestAnimationFrame(() => {
            const focusTarget = drawer.querySelector("[data-modal-close], button, input, select, textarea, [tabindex]:not([tabindex='-1'])");
            focusTarget?.focus?.();
        });
        return;
    }
    if (!open && drawer && wasOpen) {
        drawer.__returnFocus?.focus?.();
    }
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
    if (S.resourceSaveTimers?.skill) {
        window.clearTimeout(S.resourceSaveTimers.skill);
        S.resourceSaveTimers.skill = null;
    }
    S.selectedSkill = null;
    S.skillFiles = [];
    S.skillContents = {};
    S.selectedSkillFile = "";
    renderSkills();
    renderSkillDetail();
}

function clearToolSelection() {
    if (S.resourceSaveTimers?.tool) {
        window.clearTimeout(S.resourceSaveTimers.tool);
        S.resourceSaveTimers.tool = null;
    }
    S.selectedTool = null;
    renderTools();
    renderToolDetail();
}

function primaryProjectAction(status) {
    if (canPause(status)) return { action: "pause", label: "暂停", tone: "warn" };
    if (canResume(status)) return { action: "resume", label: "恢复", tone: "success" };
    return null;
}

function projectActionText(action) {
    return ({ pause: "暂停", resume: "恢复", delete: "删除" }[action] || "操作");
}

async function requestProjectAction(projectId, action) {
    if (action === "pause") return ApiClient.pauseProject(projectId);
    if (action === "resume") return ApiClient.resumeProject(projectId);
    if (action === "delete") return ApiClient.deleteProject(projectId);
    throw new Error(`Unsupported project action: ${action}`);
}

function confirmDeleteProject(project, trigger) {
    closeProjectMenus();
    const text = canPause(project.status)
        ? "将先终止项目，再彻底删除该项目及关联数据，且不可恢复。"
        : "将彻底删除该项目及关联数据，且不可恢复。";
    openConfirm({
        title: "确认删除项目",
        text,
        confirmLabel: "确认删除",
        confirmKind: "danger",
        returnFocus: trigger,
        onConfirm: () => runProjectAction(project.project_id, "delete"),
    });
}

function confirmBatchDelete(trigger) {
    closeProjectMenus();
    const deletable = getSelectedProjects().filter((project) => canDeleteBatch(project.status));
    openConfirm({
        title: "确认批量删除",
        text: `本次将删除 ${deletable.length} 个项目及其关联数据，操作不可恢复。`,
        confirmLabel: "确认批量删除",
        confirmKind: "danger",
        returnFocus: trigger,
        onConfirm: () => runProjectBatchAction("delete"),
    });
}

function renderProjects() {
    U.projectGrid.innerHTML = "";
    if (!S.projects.length) {
        U.projectGrid.innerHTML = '<div class="empty-state" style="grid-column: 1/-1;">当前没有项目。</div>';
        return updateProjectToolbar();
    }
    S.projects.forEach((p) => {
        const selected = S.selectedProjects.has(p.project_id);
        const primaryAction = primaryProjectAction(p.status);
        const el = document.createElement("div");
        el.className = `project-card${selected ? " is-selected" : ""}${S.multiSelectMode ? " is-multi-mode" : ""}`;
        el.innerHTML = `
            <div class="pc-topbar">
                <label class="project-select-toggle${S.multiSelectMode ? " is-visible" : ""}"><input type="checkbox" class="project-select-checkbox" ${selected ? "checked" : ""} ${S.projectBusy ? "disabled" : ""}><span>勾选</span></label>
                <span class="status-badge" data-status="${esc(p.status)}">${esc(String(p.status || "").toUpperCase())}</span>
            </div>
            <div class="pc-header"><div><h3 class="pc-title">${esc(p.title)}</h3><span class="pc-id">${esc(p.project_id)}</span></div></div>
            <div class="pc-summary">${esc(p.summary || "暂无摘要")}</div>
            <div class="pc-stats">${esc(String(p.active_unit_count || 0))} 个活动单元</div>
            <div class="pc-actions">
                <div class="pc-actions-left">
                    ${primaryAction ? `<button class="project-action-btn ${primaryAction.tone}" type="button" data-action="${primaryAction.action}" ${S.projectBusy ? "disabled" : ""}>${primaryAction.label}</button>` : ""}
                </div>
                <div class="pc-actions-right">
                    <button class="project-action-btn danger" type="button" data-action="delete" ${S.projectBusy || !canDeleteSingle(p.status) ? "disabled" : ""}>删除</button>
                </div>
            </div>
        `;
        const toggle = el.querySelector(".project-select-toggle");
        const checkbox = el.querySelector(".project-select-checkbox");
        toggle?.addEventListener("click", (e) => e.stopPropagation());
        checkbox?.addEventListener("change", (e) => {
            e.stopPropagation();
            if (e.target.checked) S.selectedProjects.add(p.project_id);
            else S.selectedProjects.delete(p.project_id);
            renderProjects();
        });
        el.querySelectorAll(".project-action-btn").forEach((btn) => btn.addEventListener("click", async (e) => {
            e.stopPropagation();
            const action = btn.dataset.action;
            if (action === "delete") {
                confirmDeleteProject(p, btn);
                return;
            }
            await runProjectAction(p.project_id, action);
        }));
        el.addEventListener("click", () => {
            if (S.multiSelectMode) {
                toggleProjectSelection(p.project_id);
                return;
            }
            openProject(p.project_id);
        });
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
        showToast({ title: "加载失败", text: e.message || "Unknown error", kind: "error" });
    }
}

async function runProjectAction(projectId, action) {
    S.projectBusy = true;
    updateProjectToolbar();
    renderProjects();
    try {
        await requestProjectAction(projectId, action);
        if (action === "delete" && S.currentProjectId === projectId) {
            S.currentProjectId = null;
            switchView("projects");
        }
        await loadProjects();
        showToast({ title: `${projectActionText(action)}成功`, text: `项目已${projectActionText(action)}。`, kind: "success" });
    } catch (e) {
        addMsg(`项目操作失败：${projectId} - ${e.message}`, "system");
        showToast({ title: `${projectActionText(action)}失败`, text: e.message || "Unknown error", kind: "error" });
    } finally {
        S.projectBusy = false;
        updateProjectToolbar();
        renderProjects();
    }
}

async function runProjectBatchAction(action) {
    const selected = getSelectedProjects();
    const eligible = selected.filter((project) => {
        if (action === "pause") return canPause(project.status);
        if (action === "resume") return canResume(project.status);
        if (action === "delete") return canDeleteBatch(project.status);
        return false;
    });
    const skipped = selected.length - eligible.length;
    if (!eligible.length) {
        showToast({ title: "没有可操作项目", text: "当前选择中没有符合条件的项目。", kind: "warn" });
        return;
    }
    S.projectBusy = true;
    closeProjectMenus();
    updateProjectToolbar();
    renderProjects();
    try {
        const results = await Promise.allSettled(eligible.map((project) => requestProjectAction(project.project_id, action)));
        const success = results.filter((result) => result.status === "fulfilled").length;
        const failed = results.length - success;
        await loadProjects();
        showToast({
            title: `批量${projectActionText(action)}完成`,
            text: `成功 ${success} 项，跳过 ${skipped} 项，失败 ${failed} 项`,
            kind: failed ? "warn" : "success",
        });
    } catch (e) {
        showToast({ title: `批量${projectActionText(action)}失败`, text: e.message || "Unknown error", kind: "error" });
    } finally {
        S.projectBusy = false;
        updateProjectToolbar();
        renderProjects();
    }
}

function resetProjectView() {
    S.tree = null;
    S.selectedUnitId = null;
    U.tree.innerHTML = '<div class="empty-state">等待获取组织结构...</div>';
    U.feedTitle.textContent = "节点详情";
    if (U.nodeEmpty) U.nodeEmpty.style.display = "block";
    hideAgent();
}

function findTreeNode(node, nodeId) {
    if (!node) return null;
    if (String(node.node_id || "") === String(nodeId || "")) return node;
    for (const child of (node.children || [])) {
        const found = findTreeNode(child, nodeId);
        if (found) return found;
    }
    return null;
}

function renderNodeLogs(rows) {
    U.adLogs.innerHTML = "";
    if (!Array.isArray(rows) || !rows.length) {
        U.adLogs.innerHTML = '<div class="empty-state" style="padding: 10px;">暂无日志...</div>';
        return;
    }
    rows.forEach((row) => {
        const el = document.createElement("div");
        el.className = "feed-card type-info";
        const kind = String(row.kind || "log");
        const ts = row.ts ? new Date(row.ts).toLocaleTimeString() : "";
        el.innerHTML = `<div class="card-header"><span>${esc(kind.toUpperCase())}</span><span>${esc(ts)}</span></div><div class="card-content">${esc(String(row.content || ""))}</div>`;
        U.adLogs.appendChild(el);
    });
    icons();
}

function renderTree() {
    if (!S.tree) return;
    const wrapper = document.createElement("div");
    wrapper.className = "tree-flow-wrapper";

    const walk = (node, parent, depth = 0) => {
        const wrap = document.createElement("div");
        wrap.className = `flow-node-wrapper line-status-${esc(node.state || "")}`;
        wrap.style.marginLeft = `${depth * 18}px`;
        const el = document.createElement("div");
        el.className = `tree-node${S.selectedUnitId === node.node_id ? " selected" : ""}`;
        el.dataset.id = node.node_id;
        el.innerHTML = `<div class="node-header"><span class="node-title">${esc(node.node_id || "")}</span><span class="status-badge" data-status="${esc(node.state || "")}">${esc(String(node.state || "").toUpperCase())}</span></div>`;
        el.addEventListener("click", (e) => {
            e.stopPropagation();
            S.selectedUnitId = node.node_id;
            showAgent(node);
            renderTree();
        });
        wrap.appendChild(el);
        parent.appendChild(wrap);
        (node.children || []).forEach((child) => walk(child, parent, depth + 1));
    };
    walk(S.tree, wrapper, 0);
    U.tree.innerHTML = "";
    U.tree.appendChild(wrapper);
    if (S.selectedUnitId) {
        const selected = findTreeNode(S.tree, S.selectedUnitId);
        if (selected) {
            showAgent(selected);
        } else {
            S.selectedUnitId = null;
            if (U.nodeEmpty) U.nodeEmpty.style.display = "block";
            hideAgent();
            U.feedTitle.textContent = "节点详情";
        }
    }
}

function showAgent(node) {
    U.detail.style.display = "flex";
    if (U.nodeEmpty) U.nodeEmpty.style.display = "none";
    U.adRole.textContent = node.node_id || "节点";
    U.adStatus.textContent = String(node.state || "").toUpperCase();
    U.adStatus.dataset.status = node.state || "";
    U.adInput.textContent = node.input || "-";
    U.adOutput.textContent = node.output || "-";
    U.adCheck.textContent = node.check || "-";
    U.feedTitle.textContent = `节点详情: ${node.node_id || ""}`;
    renderNodeLogs(node.log || []);
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
        const [meta, tree] = await Promise.all([ApiClient.getProjectDetails(projectId), ApiClient.getProjectTree(projectId)]);
        if (meta.project) {
            U.pdTitle.textContent = meta.project.title;
            U.pdStatus.textContent = String(meta.project.status || "").toUpperCase();
            U.pdStatus.dataset.status = meta.project.status || "";
            U.pdSummary.textContent = meta.project.summary || "暂无摘要";
            U.pdActiveCount.textContent = String(meta.project.active_unit_count || 0);
        }
        if (tree.root) { S.tree = tree.root; renderTree(); }
        S.projectWs = new WebSocket(ApiClient.getProjectWsUrl(projectId));
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
        const desc = (skill.description || "").trim();
        const subtitle = desc ? (desc.length > 50 ? desc.slice(0, 47) + "..." : desc) : skill.skill_id;
        el.innerHTML = `<div class="resource-list-title">${esc(skill.display_name)}</div><div class="resource-list-subtitle">${esc(subtitle)}</div><div class="resource-list-meta">${esc(skill.risk_level)} · ${skill.enabled ? "已启用" : "已禁用"}</div>`;
        el.addEventListener("click", () => openSkill(skill.skill_id));
        U.skillList.appendChild(el);
    });
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
        const desc = (tool.description || "").trim();
        const subtitle = desc ? (desc.length > 50 ? desc.slice(0, 47) + "..." : desc) : tool.tool_id;
        el.innerHTML = `<div class="resource-list-title">${esc(tool.display_name)}</div><div class="resource-list-subtitle">${esc(subtitle)}</div><div class="resource-list-meta">${tool.enabled ? "已启用" : "已禁用"} · ${(tool.actions || []).length} 个动作</div>`;
        el.addEventListener("click", () => openTool(tool.tool_id));
        U.toolList.appendChild(el);
    });
}

function toggleTheme() {
    const html = document.documentElement;
    const dark = html.getAttribute("data-theme") === "dark";
    html.setAttribute("data-theme", dark ? "light" : "dark");
    const darkIcon = U.theme.querySelector(".dark-icon");
    const lightIcon = U.theme.querySelector(".light-icon");
    if (darkIcon && lightIcon) {
        darkIcon.style.display = dark ? "none" : "block";
        lightIcon.style.display = dark ? "block" : "none";
    }
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
    const editorValue = esc(S.skillContents[S.selectedSkillFile] || "");
    const fileTabs = S.skillFiles.length
        ? S.skillFiles.map((file) => `<button type="button" class="toolbar-btn ghost skill-file ${S.selectedSkillFile === file.file_key ? "active" : ""}" data-file="${esc(file.file_key)}">${esc(file.file_key)}</button>`).join("")
        : '<span class="resource-empty-copy">暂无可编辑文件</span>';
    U.skillDetail.innerHTML = `
        <article class="resource-detail-card detail-modal-shell">
            <div class="detail-modal-header">
                <div class="detail-modal-title">
                    <h2 id="skill-detail-title">${esc(S.selectedSkill.display_name)}</h2>
                    <p class="subtitle">${esc(S.selectedSkill.skill_id)}</p>
                </div>
                <div class="detail-modal-actions">
                    <button type="button" class="toolbar-btn ghost" id="skill-modal-close" data-modal-close>关闭</button>
                    <button type="button" class="toolbar-btn success" id="skill-modal-save">保存</button>
                </div>
            </div>
            <div class="detail-modal-body">
                <label class="role-toggle ${S.selectedSkill.enabled ? "checked" : ""}">
                    <input id="skill-enabled" type="checkbox" ${S.selectedSkill.enabled ? "checked" : ""}>
                    <span>启用该技能</span>
                </label>
                <div class="resource-section">
                    <h3>允许的角色</h3>
                    <div class="resource-filter-row">
                        ${roles.map((role) => `
                            <label class="role-toggle ${allowedRoles.includes(role) ? "checked" : ""}">
                                <input type="checkbox" class="skill-role" data-role="${role}" ${allowedRoles.includes(role) ? "checked" : ""}>
                                <span>${esc(roleLabel(role))}</span>
                            </label>
                        `).join("")}
                    </div>
                </div>
                <div class="resource-section">
                    <h3>可编辑文件</h3>
                    <div class="resource-filter-row">${fileTabs}</div>
                    <textarea id="skill-editor" rows="18" class="resource-editor">${editorValue}</textarea>
                </div>
            </div>
        </article>`;
    U.skillDetail.querySelector("#skill-modal-close")?.addEventListener("click", clearSkillSelection);
    U.skillDetail.querySelector("#skill-modal-save")?.addEventListener("click", () => void saveSkill());
    U.skillDetail.querySelector("#skill-enabled")?.addEventListener("change", (e) => {
        S.selectedSkill.enabled = !!e.target.checked;
        e.target.closest(".role-toggle")?.classList.toggle("checked", e.target.checked);
        queueResourceSave("skill");
    });
    U.skillDetail.querySelectorAll(".skill-role").forEach((checkbox) => checkbox.addEventListener("change", (e) => {
        const nextRoles = new Set(allowedRoles);
        if (e.target.checked) nextRoles.add(e.target.dataset.role);
        else nextRoles.delete(e.target.dataset.role);
        S.selectedSkill.allowed_roles = [...nextRoles];
        renderSkillDetail();
        queueResourceSave("skill");
    }));
    U.skillDetail.querySelectorAll(".skill-file").forEach((button) => button.addEventListener("click", () => {
        const editor = document.getElementById("skill-editor");
        if (editor && S.selectedSkillFile) S.skillContents[S.selectedSkillFile] = editor.value;
        S.selectedSkillFile = button.dataset.file;
        renderSkillDetail();
    }));
    U.skillDetail.querySelector("#skill-editor")?.addEventListener("input", (e) => {
        if (!S.selectedSkillFile) return;
        S.skillContents[S.selectedSkillFile] = e.target.value;
    });
    renderSkillActions();
}

async function loadSkills({ renderDetail = true } = {}) {
    U.skillList.innerHTML = '<div class="empty-state">Loading skills...</div>';
    const selectedId = S.selectedSkill?.skill_id || "";
    try {
        S.skills = await ApiClient.getSkills(0, 300);
        if (selectedId) {
            const next = S.skills.find((skill) => skill.skill_id === selectedId);
            if (next) S.selectedSkill = next;
            else clearSkillSelection();
        }
        renderSkills();
        if (renderDetail) renderSkillDetail();
    } catch (e) {
        U.skillList.innerHTML = `<div class="empty-state error">Failed to load skills: ${esc(e.message)}</div>`;
        addNotice({ kind: "resource_failed", title: "Skill load failed", text: e.message || "Unknown error" });
    } finally {
        renderSkillActions();
    }
}

async function openSkill(skillId, quiet = false) {
    if (!quiet) {
        setDrawerOpen(U.skillBackdrop, U.skillDrawer, true);
        U.skillEmpty.style.display = "none";
        U.skillDetail.innerHTML = '<div class="empty-state">Loading skill details...</div>';
    }
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
    if (S.skillBusy) return;
    if (S.resourceSaveTimers?.skill) {
        window.clearTimeout(S.resourceSaveTimers.skill);
        S.resourceSaveTimers.skill = null;
    }
    const selectedId = String(S.selectedSkill?.skill_id || "").trim();
    const displayName = String(S.selectedSkill?.display_name || selectedId || "Skill").trim();
    const enabled = !!S.selectedSkill?.enabled;
    const allowedRoles = Array.isArray(S.selectedSkill?.allowed_roles) ? [...S.selectedSkill.allowed_roles] : [];
    if (!selectedId || !S.selectedSkill) {
        addNotice({ kind: "resource_failed", title: "No skill selected", text: "Select a skill before saving." });
        showToast({ title: "保存失败", text: "未选择 Skill", kind: "error" });
        return;
    }
    S.skillBusy = true;
    renderSkillActions();
    U.skillDetail.querySelector("#skill-modal-save")?.setAttribute("disabled", "true");
    showToast({ title: "保存中", text: "正在保存 Skill，请稍候…", kind: "info", persistent: true });
    try {
        const editor = document.getElementById("skill-editor");
        if (editor && S.selectedSkillFile) S.skillContents[S.selectedSkillFile] = editor.value;
        for (const [fileKey, content] of Object.entries(S.skillContents)) {
            await ApiClient.saveSkillFile(selectedId, fileKey, content);
        }
        await ApiClient.updateSkillPolicy(selectedId, {
            enabled,
            allowed_roles: allowedRoles,
        });
        await ApiClient.reloadResources();
        await loadSkills({ renderDetail: false });
        addNotice({ kind: "resource_saved", title: "Skill saved", text: displayName || selectedId });
        showToast({ title: "保存成功", text: "Skill 配置已保存", kind: "success", durationMs: 2200 });
        clearSkillSelection();
    } catch (e) {
        addNotice({ kind: "resource_failed", title: "Skill save failed", text: e.message || "Unknown error" });
        showToast({ title: "保存失败", text: e.message || "Unknown error", kind: "error", durationMs: 2600 });
        clearSkillSelection();
    } finally {
        S.skillBusy = false;
        renderSkillActions();
        U.skillDetail.querySelector("#skill-modal-save")?.removeAttribute("disabled");
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
    const actions = Array.isArray(S.selectedTool.actions) ? S.selectedTool.actions : [];
    U.toolDetail.innerHTML = `
        <article class="resource-detail-card detail-modal-shell">
            <div class="detail-modal-header">
                <div class="detail-modal-title">
                    <h2 id="tool-detail-title">${esc(S.selectedTool.display_name)}</h2>
                    <p class="subtitle">${esc(S.selectedTool.tool_id)}</p>
                </div>
                <div class="detail-modal-actions">
                    <button type="button" class="toolbar-btn ghost" id="tool-modal-close" data-modal-close>关闭</button>
                    <button type="button" class="toolbar-btn success" id="tool-modal-save">保存</button>
                </div>
            </div>
            <div class="detail-modal-body">
                <label class="role-toggle ${S.selectedTool.enabled ? "checked" : ""}">
                    <input id="tool-enabled" type="checkbox" ${S.selectedTool.enabled ? "checked" : ""}>
                    <span>启用工具族</span>
                </label>
                <div class="resource-section">
                    <div class="tool-permission-heading">
                        <h3>分配动作权限</h3>
                        <p class="subtitle">以设置面板的方式逐项配置每个动作对 CEO、执行、检验角色的使用权限。</p>
                    </div>
                    <div class="tool-permission-grid">
                        ${actions.length ? actions.map((action) => {
                            const actionName = esc(action.label || action.action_id);
                            const actionId = esc(action.action_id);
                            const riskLevel = esc(action.risk_level || "medium");
                            const riskClass = `risk-${String(action.risk_level || "medium").toLowerCase()}`;
                            return `
                                <article class="tool-permission-card">
                                    <div class="tool-permission-card-head">
                                        <div class="tool-action-meta">
                                            <div class="tool-action-name">${actionName}</div>
                                            <div class="tool-action-id">${actionId}</div>
                                        </div>
                                        <span class="risk-pill ${riskClass}">${riskLevel}</span>
                                    </div>
                                    <div class="tool-role-toggle-group">
                                        ${roles.map((role) => `
                                            <label class="role-toggle tool-role-toggle ${action.allowed_roles?.includes(role) ? "checked" : ""}">
                                                <input type="checkbox" class="tool-role tool-role-input" data-action="${actionId}" data-role="${role}" aria-label="${actionName} - ${esc(roleLabel(role))}" ${action.allowed_roles?.includes(role) ? "checked" : ""}>
                                                <span>${esc(roleLabel(role))}</span>
                                            </label>
                                        `).join("")}
                                    </div>
                                </article>`;
                        }).join("") : `<div class="tool-empty-card">暂无可配置动作</div>`}
                    </div>
                </div>
            </div>
        </article>`;
    U.toolDetail.querySelector("#tool-modal-close")?.addEventListener("click", clearToolSelection);
    U.toolDetail.querySelector("#tool-modal-save")?.addEventListener("click", () => void saveTool());
    U.toolDetail.querySelector("#tool-enabled")?.addEventListener("change", (e) => {
        S.selectedTool.enabled = !!e.target.checked;
        e.target.closest(".role-toggle")?.classList.toggle("checked", e.target.checked);
        queueResourceSave("tool");
    });
    U.toolDetail.querySelectorAll(".tool-role").forEach((checkbox) => checkbox.addEventListener("change", (e) => {
        const action = S.selectedTool.actions.find((item) => item.action_id === e.target.dataset.action);
        if (!action) return;
        const set = new Set(action.allowed_roles || []);
        if (e.target.checked) set.add(e.target.dataset.role);
        else set.delete(e.target.dataset.role);
        action.allowed_roles = [...set];
        e.target.closest(".role-toggle")?.classList.toggle("checked", e.target.checked);
        queueResourceSave("tool");
    }));
    renderToolActions();
}

async function loadTools({ renderDetail = true } = {}) {
    U.toolList.innerHTML = '<div class="empty-state">Loading tools...</div>';
    const selectedId = S.selectedTool?.tool_id || "";
    try {
        S.tools = await ApiClient.getTools(0, 300);
        if (selectedId) {
            const next = S.tools.find((tool) => tool.tool_id === selectedId);
            if (next) S.selectedTool = next;
            else clearToolSelection();
        }
        renderTools();
        if (renderDetail) renderToolDetail();
    } catch (e) {
        U.toolList.innerHTML = `<div class="empty-state error">Failed to load tools: ${esc(e.message)}</div>`;
        addNotice({ kind: "resource_failed", title: "Tool load failed", text: e.message || "Unknown error" });
    } finally {
        renderToolActions();
    }
}

async function openTool(toolId, quiet = false) {
    if (!quiet) {
        setDrawerOpen(U.toolBackdrop, U.toolDrawer, true);
        U.toolEmpty.style.display = "none";
        U.toolDetail.innerHTML = '<div class="empty-state">Loading tool details...</div>';
    }
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
    if (S.toolBusy) return;
    if (S.resourceSaveTimers?.tool) {
        window.clearTimeout(S.resourceSaveTimers.tool);
        S.resourceSaveTimers.tool = null;
    }
    const selectedId = String(S.selectedTool?.tool_id || "").trim();
    const displayName = String(S.selectedTool?.display_name || selectedId || "Tool").trim();
    const enabled = !!S.selectedTool?.enabled;
    const actions = Array.isArray(S.selectedTool?.actions)
        ? S.selectedTool.actions.map((action) => ({
            action_id: action.action_id,
            allowed_roles: Array.isArray(action.allowed_roles) ? [...action.allowed_roles] : [],
        }))
        : [];
    if (!selectedId || !S.selectedTool) {
        addNotice({ kind: "resource_failed", title: "No tool selected", text: "Select a tool before saving." });
        showToast({ title: "保存失败", text: "未选择工具族", kind: "error" });
        return;
    }
    S.toolBusy = true;
    renderToolActions();
    U.toolDetail.querySelector("#tool-modal-save")?.setAttribute("disabled", "true");
    showToast({ title: "保存中", text: "正在保存工具权限，请稍候…", kind: "info", persistent: true });
    try {
        await ApiClient.updateToolPolicy(selectedId, {
            enabled,
            actions,
        });
        await ApiClient.reloadResources();
        await loadTools({ renderDetail: false });
        addNotice({ kind: "resource_saved", title: "Tool saved", text: displayName || selectedId });
        showToast({ title: "保存成功", text: "工具权限已保存", kind: "success", durationMs: 2200 });
        clearToolSelection();
    } catch (e) {
        addNotice({ kind: "resource_failed", title: "Tool save failed", text: e.message || "Unknown error" });
        showToast({ title: "保存失败", text: e.message || "Unknown error", kind: "error", durationMs: 2600 });
        clearToolSelection();
    } finally {
        S.toolBusy = false;
        renderToolActions();
        U.toolDetail.querySelector("#tool-modal-save")?.removeAttribute("disabled");
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
    if (view !== "projects") setMultiSelectMode(false);
    if (view !== "skills") setDrawerOpen(U.skillBackdrop, U.skillDrawer, false);
    if (view !== "tools") setDrawerOpen(U.toolBackdrop, U.toolDrawer, false);
    if (view !== "models") {
        setDrawerOpen(U.modelBackdrop, U.modelDrawer, false);
        S.modelCatalog.mode = "view";
        S.modelCatalog.selectedModelKey = "";
        S.modelCatalog.roleEditing = false;
        S.modelCatalog.roleDrafts = cloneModelRoles(S.modelCatalog.roles);
        S.modelCatalog.rolesDirty = false;
    }
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
    U.modelRolesCancel?.addEventListener("click", cancelModelRoleEditing);
    U.modelRolesSave?.addEventListener("click", () => void handleModelRoleEditorAction());
    U.modelSearch?.addEventListener("input", (e) => {
        S.modelCatalog.search = String(e.target.value || "");
        renderModelList();
    });
    U.modelList?.addEventListener("click", (e) => {
        const open = e.target.closest("[data-model-open]");
        if (open) {
            openModel(open.dataset.modelOpen);
            return;
        }
    });
    U.modelRoleEditors?.addEventListener("click", (e) => {
        const open = e.target.closest("[data-model-open]");
        if (open) {
            openModel(open.dataset.modelOpen);
            return;
        }
        if (!S.modelCatalog.roleEditing) return;
        const action = e.target.closest("[data-model-chain-action]");
        if (action) {
            const scope = String(action.dataset.scope || "");
            const index = Number(action.dataset.index || -1);
            const chain = modelScopeChain(scope);
            if (!scope || index < 0 || index >= chain.length) return;
            if (action.dataset.modelChainAction === "remove") {
                chain.splice(index, 1);
                updateRoleChainDraft(scope, chain);
            }
            return;
        }
    });
    U.modelRoleEditors?.addEventListener("dragstart", (e) => {
        if (!S.modelCatalog.roleEditing) return;
        const chainItem = e.target.closest("[data-model-chain-ref]");
        if (!chainItem) return;
        beginModelDrag(chainItem, {
            scope: String(chainItem.dataset.scope || ""),
            ref: String(chainItem.dataset.modelChainRef || ""),
            source: "chain",
        }, e.dataTransfer);
    });
    U.modelRoleEditors?.addEventListener("dragover", (e) => {
        if (!S.modelCatalog.roleEditing) return;
        const dragState = S.modelCatalog.dragState;
        if (!dragState?.ref) return;
        const chainList = e.target.closest("[data-model-chain-list]");
        if (!chainList) return;
        const scope = String(chainList.dataset.modelChainList || "");
        const allowDrop = dragState.source === "available" || scope === dragState.scope;
        if (!scope || !allowDrop) return;
        e.preventDefault();
        e.dataTransfer.dropEffect = dragState.source === "chain" ? "move" : "copy";
        clearModelDragDecorations();
        let targetItem = e.target.closest("[data-model-chain-ref]");
        if (targetItem && dragState.source === "chain" && scope === dragState.scope && String(targetItem.dataset.modelChainRef || "") === dragState.ref) {
            targetItem = null;
        }
        ensureModelDropPlaceholder(chainList, targetItem, e.clientY);
        startModelAutoScroll(chainList, e.clientY);
    });
    U.modelRoleEditors?.addEventListener("drop", (e) => {
        if (!S.modelCatalog.roleEditing) return;
        const dragState = S.modelCatalog.dragState;
        if (!dragState?.ref) return;
        const chainList = e.target.closest("[data-model-chain-list]");
        if (!chainList) return;
        const scope = String(chainList.dataset.modelChainList || "");
        const allowDrop = dragState.source === "available" || scope === dragState.scope;
        if (!scope || !allowDrop) return;
        e.preventDefault();
        const placeholder = chainList.querySelector('[data-model-drop-placeholder]');
        const children = [...chainList.children];
        const placeholderIndex = children.indexOf(placeholder);
        const targetIndex = placeholderIndex < 0
            ? children.filter((child) => child.matches?.('[data-model-chain-ref]') && String(child.dataset.modelChainRef || '') !== dragState.ref).length
            : children.slice(0, placeholderIndex).filter((child) => child.matches?.('[data-model-chain-ref]') && String(child.dataset.modelChainRef || '') !== dragState.ref).length;
        clearModelDragDecorations();
        stopModelAutoScroll();
        if (dragState.source === "chain") moveRoleChainItem(scope, dragState.ref, targetIndex);
        else insertRoleChainItem(scope, dragState.ref, targetIndex);
    });
    U.modelRoleEditors?.addEventListener("dragleave", (e) => {
        if (!S.modelCatalog.roleEditing) return;
        const dragState = S.modelCatalog.dragState;
        if (!dragState?.ref) return;
        const zone = e.target.closest("[data-model-chain-list]");
        if (!zone) return;
        const related = e.relatedTarget;
        if (related && zone.contains(related)) return;
        clearModelDragDecorations();
        stopModelAutoScroll();
    });
    U.modelRoleEditors?.addEventListener("dragend", finishModelDrag);
    U.modelList?.addEventListener("dragstart", (e) => {
        if (!S.modelCatalog.roleEditing) return;
        const availableItem = e.target.closest("[data-model-available-key]");
        if (!availableItem) return;
        beginModelDrag(availableItem, {
            ref: String(availableItem.dataset.modelAvailableKey || ""),
            source: "available",
        }, e.dataTransfer);
    });
    U.modelList?.addEventListener("dragover", (e) => {
        if (!S.modelCatalog.roleEditing) return;
        const dragState = S.modelCatalog.dragState;
        if (!dragState?.ref || dragState.source !== "chain") return;
        const availableList = e.target.closest("[data-model-available-list]");
        if (!availableList) return;
        e.preventDefault();
        e.dataTransfer.dropEffect = "move";
        clearModelDragDecorations();
        const targetItem = e.target.closest("[data-model-available-key]");
        highlightModelAvailableZone(availableList, targetItem);
        startModelAutoScroll(availableList, e.clientY);
    });
    U.modelList?.addEventListener("drop", (e) => {
        if (!S.modelCatalog.roleEditing) return;
        const dragState = S.modelCatalog.dragState;
        if (!dragState?.ref || dragState.source !== "chain") return;
        const availableList = e.target.closest("[data-model-available-list]");
        if (!availableList) return;
        e.preventDefault();
        clearModelDragDecorations();
        stopModelAutoScroll();
        removeRoleChainItem(dragState.scope, dragState.ref);
    });
    U.modelList?.addEventListener("dragleave", (e) => {
        if (!S.modelCatalog.roleEditing) return;
        const dragState = S.modelCatalog.dragState;
        if (!dragState?.ref) return;
        const zone = e.target.closest("[data-model-available-list]");
        if (!zone) return;
        const related = e.relatedTarget;
        if (related && zone.contains(related)) return;
        clearModelDragDecorations();
        stopModelAutoScroll();
    });
    U.modelList?.addEventListener("dragend", finishModelDrag);
    U.modelDetail?.addEventListener("submit", (e) => {
        if (e.target?.id !== "model-detail-form") return;
        e.preventDefault();
        void saveModelDetail();
    });
    U.modelDetail?.addEventListener("click", (e) => {
        const cancel = e.target.closest("[data-model-detail-cancel]");
        if (!cancel) return;
        clearModelSelection();
    });
    U.modelDetail?.addEventListener("change", (e) => {
        const toggle = e.target.closest(".role-toggle");
        if (toggle && e.target instanceof HTMLInputElement && e.target.type === "checkbox") {
            toggle.classList.toggle("checked", e.target.checked);
        }
    });
    U.projectMultiToggle?.addEventListener("click", () => setMultiSelectMode(!S.multiSelectMode));
    U.projectSelectTrigger?.addEventListener("click", (e) => {
        e.stopPropagation();
        setProjectMenuOpen("select", !S.projectSelectMenuOpen);
    });
    U.projectBatchTrigger?.addEventListener("click", (e) => {
        e.stopPropagation();
        setProjectMenuOpen("batch", !S.projectBatchMenuOpen);
    });
    U.projectSelectMenu?.querySelectorAll("[data-select-bucket]")?.forEach((button) => button.addEventListener("click", () => {
        S.selectedProjects = new Set(S.projects.filter((project) => statusBucketMatches(project, button.dataset.selectBucket)).map((project) => project.project_id));
        closeProjectMenus();
        renderProjects();
    }));
    U.projectBatchMenu?.querySelectorAll("[data-batch-action]")?.forEach((button) => button.addEventListener("click", async () => {
        if (button.dataset.batchAction === "delete") {
            confirmBatchDelete(button);
            return;
        }
        await runProjectBatchAction(button.dataset.batchAction);
    }));
    U.closeAgent?.addEventListener("click", () => {
        S.selectedUnitId = null;
        U.feedTitle.textContent = "节点详情";
        if (U.nodeEmpty) U.nodeEmpty.style.display = "block";
        hideAgent();
        renderTree();
    });
    [U.skillSearch, U.skillRisk, U.skillStatus, U.skillLegacy].forEach((el) => el?.addEventListener(el.tagName === "INPUT" ? "input" : "change", renderSkills));
    U.skillRefresh?.addEventListener("click", () => void refreshSkills());
    U.skillSave?.addEventListener("click", () => void saveSkill());
    [U.toolSearch, U.toolStatus, U.toolRisk].forEach((el) => el?.addEventListener(el.tagName === "INPUT" ? "input" : "change", renderTools));
    U.toolRefresh?.addEventListener("click", () => void refreshTools());
    U.toolSave?.addEventListener("click", () => void saveTool());
    U.modelBackdrop?.addEventListener("click", clearModelSelection);
    U.skillBackdrop?.addEventListener("click", clearSkillSelection);
    U.toolBackdrop?.addEventListener("click", clearToolSelection);
    U.toastClose?.addEventListener("click", closeToast);
    U.confirmBackdrop?.addEventListener("click", (e) => {
        if (e.target === U.confirmBackdrop) closeConfirm();
    });
    U.confirmCancel?.addEventListener("click", () => closeConfirm());
    U.confirmAccept?.addEventListener("click", () => void acceptConfirm());
    document.addEventListener("click", (e) => {
        if (!(e.target instanceof Element)) return;
        if (!e.target.closest(".toolbar-dropdown")) closeProjectMenus();
    });
    document.addEventListener("keydown", (e) => {
        if (e.key !== "Escape") return;
        if (S.confirmState) {
            closeConfirm();
            return;
        }
        if (S.projectSelectMenuOpen || S.projectBatchMenuOpen) {
            closeProjectMenus();
            return;
        }
        if (S.modelCatalog.mode === "create" || S.modelCatalog.selectedModelKey) {
            clearModelSelection();
            return;
        }
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
    void loadCeoHistory();
    initCeoWs();
}

document.addEventListener("DOMContentLoaded", init);
