const MODEL_SCOPES = [
    { key: "ceo", label: "主Agent" },
    { key: "execution", label: "执行" },
    { key: "inspection", label: "检验" },
];

const EMPTY_MODEL_ROLES = () => ({ ceo: [], execution: [], inspection: [] });
const DEFAULT_MODEL_DEFAULTS = () => ({ ceo: "", execution: "", inspection: "" });
const DEFAULT_ROLE_ITERATIONS = () => ({ ceo: 40, execution: 16, inspection: 16 });
const TREE_SCALE_MIN = 0.6;
const TREE_SCALE_MAX = 1.8;
const TREE_SCALE_FACTOR = 1.12;
const RESOURCE_PAGE_SIZES = [20, 50, 100];
const CEO_TOOL_PROGRESS_MAX_LINES = 4;
const CEO_TOOL_STEP_MAX = 5;
const CEO_SESSION_POLL_MS = 4000;
const cloneModelRoles = (roles = EMPTY_MODEL_ROLES()) => {
    const next = EMPTY_MODEL_ROLES();
    MODEL_SCOPES.forEach(({ key }) => {
        next[key] = Array.isArray(roles?.[key])
            ? roles[key].map((item) => String(item || "").trim()).filter(Boolean)
            : [];
    });
    return next;
};
const cloneRoleIterations = (iterations = DEFAULT_ROLE_ITERATIONS()) => {
    const defaults = DEFAULT_ROLE_ITERATIONS();
    const next = DEFAULT_ROLE_ITERATIONS();
    MODEL_SCOPES.forEach(({ key }) => {
        const value = Number(iterations?.[key]);
        next[key] = Number.isInteger(value) && value >= 2 ? value : defaults[key];
    });
    return next;
};

const S = {
    view: "ceo",
    ceoWs: null,
    ceoWsToken: 0,
    ceoPendingTurns: [],
    ceoTurnActive: false,
    ceoPauseBusy: false,
    ceoUploads: [],
    ceoUploadBusy: false,
    ceoSessions: [],
    ceoSessionUnread: {},
    ceoSessionMessageCounts: {},
    ceoSessionHydrated: false,
    ceoSessionPollIntervalId: null,
    ceoSessionPollInFlight: false,
    liveDurationIntervalId: null,
    activeSessionId: "",
    ceoSessionBusy: false,
    taskDefaults: {
        scope: "global",
        maxDepth: 1,
        defaultMaxDepth: 1,
        hardMaxDepth: 4,
        loading: false,
        saving: false,
        requestToken: 0,
    },
    taskWs: null,
    currentTaskId: null,
    tasks: [],
    currentTask: null,
    currentTaskProgress: null,
    taskTokenStatsOpen: false,
    taskArtifacts: [],
    selectedArtifactId: "",
    artifactContent: "",
    selectedTaskIds: new Set(),
    multiSelectMode: false,
    taskFilterMenuOpen: false,
    taskBatchMenuOpen: false,
    taskBusy: false,
    taskPage: 1,
    taskPageSize: RESOURCE_PAGE_SIZES[0],
    confirmState: null,
    toastState: { timeoutId: null, intervalId: null, remaining: 0 },
    openResourceSelectId: "",
    modelCatalog: {
        items: [],
        catalog: [],
        roles: EMPTY_MODEL_ROLES(),
        roleDrafts: EMPTY_MODEL_ROLES(),
        roleIterations: DEFAULT_ROLE_ITERATIONS(),
        roleIterationDrafts: DEFAULT_ROLE_ITERATIONS(),
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
    treeView: null,
    treePan: {
        active: false,
        originNodeId: null,
        startX: 0,
        startY: 0,
        offsetX: 0,
        offsetY: 0,
        baseOffsetX: 0,
        baseOffsetY: 0,
        scale: 1,
        baseScale: 1,
        moved: false,
        suppressClickNodeId: null,
    },
    selectedNodeId: null,
    skills: [],
    selectedSkill: null,
    skillFiles: [],
    skillContents: {},
    selectedSkillFile: "",
    skillBusy: false,
    skillDirty: false,
    skillPage: 1,
    skillPageSize: RESOURCE_PAGE_SIZES[0],
    tools: [],
    selectedTool: null,
    toolBusy: false,
    toolDirty: false,
    toolPage: 1,
    toolPageSize: RESOURCE_PAGE_SIZES[0],
    communications: [],
    communicationBridge: null,
    selectedCommunication: null,
    communicationBusy: false,
    communicationDirty: false,
    communicationDraftEnabled: false,
    communicationDraftText: "",
    communicationBaselineEnabled: false,
    communicationBaselineText: "",
};

const U = {
    nav: [...document.querySelectorAll(".nav-item")],
    theme: document.getElementById("theme-toggle"),
    ceoSessionList: document.getElementById("ceo-session-list"),
    ceoSessionCurrent: document.getElementById("ceo-session-current"),
    ceoNewSession: document.getElementById("ceo-new-session-btn"),
    renameSessionBackdrop: document.getElementById("rename-session-backdrop"),
    renameSessionInput: document.getElementById("rename-session-input"),
    renameSessionCancel: document.getElementById("rename-session-cancel"),
    renameSessionAccept: document.getElementById("rename-session-accept"),
    ceoFeed: document.getElementById("ceo-chat-feed"),
    ceoInput: document.getElementById("ceo-input"),
    ceoAttach: document.getElementById("ceo-attach-btn"),
    ceoFileInput: document.getElementById("ceo-file-input"),
    ceoUploadList: document.getElementById("ceo-upload-list"),
    ceoSend: document.getElementById("ceo-send-btn"),
    viewCeo: document.getElementById("view-ceo"),
    viewTasks: document.getElementById("view-tasks-list"),
    viewSkills: document.getElementById("view-skills"),
    viewTools: document.getElementById("view-tools"),
    viewModels: document.getElementById("view-models"),
    viewCommunications: document.getElementById("view-communications"),
    viewTaskDetails: document.getElementById("view-task-details"),
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
    taskGrid: document.getElementById("task-card-grid"),
    taskToolbar: document.getElementById("task-toolbar"),
    taskDepthSelect: document.getElementById("task-depth-select"),
    taskDepthHint: document.getElementById("task-depth-hint"),
    taskPageSize: document.getElementById("task-page-size"),
    taskPageInfo: document.getElementById("task-page-info"),
    taskPagePrev: document.getElementById("task-page-prev"),
    taskPageNext: document.getElementById("task-page-next"),
    taskMultiToggle: document.getElementById("task-multi-toggle"),
    taskFilterWrap: document.getElementById("task-filter-wrap"),
    taskFilterTrigger: document.getElementById("task-filter-menu-trigger"),
    taskFilterMenu: document.getElementById("task-filter-menu"),
    taskBatchWrap: document.getElementById("task-batch-wrap"),
    taskBatchTrigger: document.getElementById("task-batch-menu-trigger"),
    taskBatchMenu: document.getElementById("task-batch-menu"),
    backToTasks: document.getElementById("back-to-tasks"),
    tdTitle: document.getElementById("td-title"),
    tdStatus: document.getElementById("td-status"),
    tdSummary: document.getElementById("td-summary"),
    tdActiveCount: document.getElementById("td-active-count"),
    tree: document.getElementById("org-tree-container"),
    taskSelectionEmpty: document.getElementById("task-selection-empty-inline"),
    taskDetailBackdrop: document.getElementById("task-detail-backdrop"),
    taskDetailDrawer: document.getElementById("task-detail-drawer"),
    taskTokenButton: null,
    taskTokenBackdrop: null,
    taskTokenDrawer: null,
    taskTokenSummaryText: null,
    taskTokenContent: null,
    taskTokenClose: null,
    artifactList: document.getElementById("artifact-list"),
    artifactContent: document.getElementById("artifact-content"),
    artifactApply: document.getElementById("artifact-apply-btn"),
    feedTitle: document.getElementById("feed-target-name"),
    detail: document.getElementById("agent-detail-view"),
    adRole: document.getElementById("ad-role"),
    adStatus: document.getElementById("ad-status"),
    adFlow: document.getElementById("ad-input"),
    adAcceptance: document.getElementById("ad-check"),
    adFlowHeading: document.getElementById("ad-input")?.closest(".agent-detail-section")?.querySelector("h4"),
    adAcceptanceHeading: document.getElementById("ad-check")?.closest(".agent-detail-section")?.querySelector("h4"),
    adOutputSection: document.getElementById("ad-output")?.closest(".agent-detail-section"),
    adLogsSection: document.getElementById("ad-logs")?.closest(".agent-detail-section"),
    nodeEmpty: document.getElementById("task-node-empty"),
    closeAgent: document.getElementById("close-agent-btn"),
    skillSearch: document.getElementById("skill-search-input"),
    skillRisk: document.getElementById("skill-risk-filter"),
    skillStatus: document.getElementById("skill-status-filter"),
    skillList: document.getElementById("skill-list"),
    skillPageSize: document.getElementById("skill-page-size"),
    skillPageInfo: document.getElementById("skill-page-info"),
    skillPagePrev: document.getElementById("skill-page-prev"),
    skillPageNext: document.getElementById("skill-page-next"),
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
    toolPageSize: document.getElementById("tool-page-size"),
    toolPageInfo: document.getElementById("tool-page-info"),
    toolPagePrev: document.getElementById("tool-page-prev"),
    toolPageNext: document.getElementById("tool-page-next"),
    toolEmpty: document.getElementById("tool-detail-empty"),
    toolDetail: document.getElementById("tool-detail-content"),
    toolBackdrop: document.getElementById("tool-detail-backdrop"),
    toolDrawer: document.querySelector(".tool-detail-panel"),
    toolRefresh: document.getElementById("tool-refresh-btn"),
    toolSave: document.getElementById("tool-save-btn"),
    communicationList: document.getElementById("communication-list"),
    communicationBridgeSummary: document.getElementById("communication-bridge-summary"),
    communicationEmpty: document.getElementById("communication-detail-empty"),
    communicationDetail: document.getElementById("communication-detail-content"),
    communicationBackdrop: document.getElementById("communication-detail-backdrop"),
    communicationDrawer: document.querySelector(".communication-detail-panel"),
    communicationRefresh: document.getElementById("communication-refresh-btn"),
    toast: document.getElementById("app-toast"),
    toastTitle: document.getElementById("app-toast-title"),
    toastText: document.getElementById("app-toast-text"),
    toastProgress: document.getElementById("app-toast-progress"),
    toastProgressBar: document.getElementById("app-toast-progress-bar"),
    toastClose: document.getElementById("app-toast-close"),
    confirmBackdrop: document.getElementById("confirm-backdrop"),
    confirmTitle: document.getElementById("confirm-title"),
    confirmText: document.getElementById("confirm-text"),
    confirmOptions: document.getElementById("confirm-options"),
    confirmCheckbox: document.getElementById("confirm-checkbox"),
    confirmCheckboxLabel: document.getElementById("confirm-checkbox-label"),
    confirmCheckboxHint: document.getElementById("confirm-checkbox-hint"),
    confirmCancel: document.getElementById("confirm-cancel"),
    confirmAccept: document.getElementById("confirm-accept"),
};

const esc = (v) => String(v ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;").replaceAll('"', "&quot;").replaceAll("'", "&#39;");
const icons = () => window.lucide && lucide.createIcons();
const roleKey = (v) => (["ceo", "inspection", "checker"].includes(String(v).toLowerCase()) ? (String(v).toLowerCase() === "ceo" ? "ceo" : "inspection") : "execution");
const roleLabel = (v) => ({ ceo: "主Agent", execution: "执行", inspection: "检验" }[roleKey(v)]);
const pStatus = (v) => String(v || "").trim().toLowerCase();
const MD_TOKEN_MARKER = "\uE000";
const clamp = (value, min, max) => Math.max(min, Math.min(max, value));
const activeSessionId = () => String(S.activeSessionId || ApiClient.getActiveSessionId()).trim() || ApiClient.getActiveSessionId();

function formatSessionTime(value) {
    const raw = String(value || "").trim();
    if (!raw) return "No activity yet";
    const parsed = new Date(raw);
    if (Number.isNaN(parsed.getTime())) return raw;
    return parsed.toLocaleString();
}

function normalizeInt(value, fallback = 0) {
    const next = Number(value);
    return Number.isFinite(next) ? Math.trunc(next) : Math.trunc(fallback);
}

function sessionMessageCount(session) {
    return Math.max(0, normalizeInt(session?.message_count, 0));
}

function sessionUnreadCount(sessionId) {
    const key = String(sessionId || "").trim();
    if (!key) return 0;
    return Math.max(0, normalizeInt(S.ceoSessionUnread?.[key], 0));
}

function markCeoSessionRead(sessionId, { messageCount = null } = {}) {
    const key = String(sessionId || "").trim();
    if (!key) return;
    S.ceoSessionUnread = { ...S.ceoSessionUnread, [key]: 0 };
    if (messageCount !== null && messageCount !== undefined) {
        S.ceoSessionMessageCounts = {
            ...S.ceoSessionMessageCounts,
            [key]: Math.max(0, normalizeInt(messageCount, 0)),
        };
    }
}

function syncCeoSessionUnreadState(sessions = [], activeId = activeSessionId()) {
    const previousCounts = S.ceoSessionMessageCounts && typeof S.ceoSessionMessageCounts === "object"
        ? S.ceoSessionMessageCounts
        : {};
    const previousUnread = S.ceoSessionUnread && typeof S.ceoSessionUnread === "object"
        ? S.ceoSessionUnread
        : {};
    const nextCounts = {};
    const nextUnread = {};
    const hydrated = !!S.ceoSessionHydrated;

    (Array.isArray(sessions) ? sessions : []).forEach((item) => {
        const sessionId = String(item?.session_id || "").trim();
        if (!sessionId) return;
        const messageCount = sessionMessageCount(item);
        nextCounts[sessionId] = messageCount;

        if (sessionId === activeId) {
            nextUnread[sessionId] = 0;
            return;
        }

        const previousCount = Math.max(0, normalizeInt(previousCounts[sessionId], messageCount));
        const existingUnread = Math.max(0, normalizeInt(previousUnread[sessionId], 0));

        if (!hydrated || !Object.prototype.hasOwnProperty.call(previousCounts, sessionId)) {
            nextUnread[sessionId] = existingUnread;
            return;
        }

        if (messageCount > previousCount) {
            nextUnread[sessionId] = existingUnread + (messageCount - previousCount);
            return;
        }

        if (messageCount < previousCount) {
            nextUnread[sessionId] = 0;
            return;
        }

        nextUnread[sessionId] = existingUnread;
    });

    S.ceoSessionMessageCounts = nextCounts;
    S.ceoSessionUnread = nextUnread;
    S.ceoSessionHydrated = true;
}

function normalizeResourcePageSize(value, fallback = RESOURCE_PAGE_SIZES[0]) {
    const next = normalizeInt(value, fallback);
    return RESOURCE_PAGE_SIZES.includes(next) ? next : fallback;
}

function paginateResources(items, page, pageSize) {
    const total = Array.isArray(items) ? items.length : 0;
    const size = normalizeResourcePageSize(pageSize, RESOURCE_PAGE_SIZES[0]);
    const totalPages = Math.max(1, Math.ceil(total / size));
    const currentPage = clamp(normalizeInt(page, 1), 1, totalPages);
    const startIndex = total ? ((currentPage - 1) * size) + 1 : 0;
    const endIndex = total ? Math.min(currentPage * size, total) : 0;
    const startOffset = total ? startIndex - 1 : 0;
    return {
        total,
        pageSize: size,
        totalPages,
        currentPage,
        startIndex,
        endIndex,
        items: total ? items.slice(startOffset, startOffset + size) : [],
    };
}

function syncResourcePagination(kind, meta) {
    const isSkill = kind === "skill";
    const pageInfo = isSkill ? U.skillPageInfo : U.toolPageInfo;
    const prevBtn = isSkill ? U.skillPagePrev : U.toolPagePrev;
    const nextBtn = isSkill ? U.skillPageNext : U.toolPageNext;
    const pageSizeSelect = isSkill ? U.skillPageSize : U.toolPageSize;
    const pageSize = isSkill ? S.skillPageSize : S.toolPageSize;

    if (pageInfo) {
        pageInfo.textContent = meta.total
            ? `第 ${meta.currentPage}/${meta.totalPages} 页 · 显示 ${meta.startIndex}-${meta.endIndex} / 共 ${meta.total} 项`
            : "共 0 项";
    }
    if (prevBtn) prevBtn.disabled = meta.currentPage <= 1 || meta.total === 0;
    if (nextBtn) nextBtn.disabled = meta.currentPage >= meta.totalPages || meta.total === 0;
    if (pageSizeSelect instanceof HTMLSelectElement) {
        const nextValue = String(pageSize);
        if (pageSizeSelect.value !== nextValue) pageSizeSelect.value = nextValue;
        syncResourceSelectUI(pageSizeSelect);
    }
}

function resetSkillPagination() {
    S.skillPage = 1;
    renderSkills();
}

function resetToolPagination() {
    S.toolPage = 1;
    renderTools();
}

function syncTaskPagination(meta) {
    if (U.taskPageInfo) {
        U.taskPageInfo.textContent = meta.total
            ? `第 ${meta.currentPage}/${meta.totalPages} 页 · 显示 ${meta.startIndex}-${meta.endIndex} / 共 ${meta.total} 项`
            : "共 0 项";
    }
    if (U.taskPagePrev) U.taskPagePrev.disabled = meta.currentPage <= 1 || meta.total === 0;
    if (U.taskPageNext) U.taskPageNext.disabled = meta.currentPage >= meta.totalPages || meta.total === 0;
    if (U.taskPageSize instanceof HTMLSelectElement) {
        const nextValue = String(S.taskPageSize);
        if (U.taskPageSize.value !== nextValue) U.taskPageSize.value = nextValue;
        syncResourceSelectUI(U.taskPageSize);
    }
}

function scrollTaskListToTop() {
    U.taskGrid?.scrollTo?.({ top: 0, behavior: "auto" });
    U.taskGrid?.closest(".project-list-container")?.scrollTo?.({ top: 0, behavior: "auto" });
}

function setTaskPage(page) {
    const meta = paginateResources(orderedTasks(S.tasks), page, S.taskPageSize);
    S.taskPage = meta.currentPage;
    renderTasks();
    scrollTaskListToTop();
}

function setTaskPageSize(value) {
    S.taskPageSize = normalizeResourcePageSize(value, S.taskPageSize);
    S.taskPage = 1;
    renderTasks();
    scrollTaskListToTop();
}

function setSkillPage(page) {
    const meta = paginateResources(filterSkills(), page, S.skillPageSize);
    S.skillPage = meta.currentPage;
    renderSkills();
    U.skillList?.scrollTo?.({ top: 0, behavior: "auto" });
}

function setToolPage(page) {
    const meta = paginateResources(filterTools(), page, S.toolPageSize);
    S.toolPage = meta.currentPage;
    renderTools();
    U.toolList?.scrollTo?.({ top: 0, behavior: "auto" });
}

function setSkillPageSize(value) {
    S.skillPageSize = normalizeResourcePageSize(value, S.skillPageSize);
    S.skillPage = 1;
    renderSkills();
    U.skillList?.scrollTo?.({ top: 0, behavior: "auto" });
}

function setToolPageSize(value) {
    S.toolPageSize = normalizeResourcePageSize(value, S.toolPageSize);
    S.toolPage = 1;
    renderTools();
    U.toolList?.scrollTo?.({ top: 0, behavior: "auto" });
}

function ensureSkillPageForItem(skillId) {
    const targetId = String(skillId || "").trim();
    if (!targetId) return;
    const items = filterSkills();
    const index = items.findIndex((item) => item.skill_id === targetId);
    if (index < 0) return;
    S.skillPage = Math.floor(index / S.skillPageSize) + 1;
}

function ensureToolPageForItem(toolId) {
    const targetId = String(toolId || "").trim();
    if (!targetId) return;
    const items = filterTools();
    const index = items.findIndex((item) => item.tool_id === targetId);
    if (index < 0) return;
    S.toolPage = Math.floor(index / S.toolPageSize) + 1;
}

function applyTaskDefaultsPayload(payload = {}) {
    const runtime = payload?.main_runtime && typeof payload.main_runtime === "object"
        ? payload.main_runtime
        : payload?.mainRuntime && typeof payload.mainRuntime === "object"
            ? payload.mainRuntime
            : {};
    const taskDefaults = payload?.task_defaults && typeof payload.task_defaults === "object"
        ? payload.task_defaults
        : payload?.taskDefaults && typeof payload.taskDefaults === "object"
            ? payload.taskDefaults
            : {};
    const defaultMaxDepth = Math.max(0, normalizeInt(runtime.default_max_depth ?? runtime.defaultMaxDepth, S.taskDefaults.defaultMaxDepth));
    const hardMaxDepth = Math.max(defaultMaxDepth, normalizeInt(runtime.hard_max_depth ?? runtime.hardMaxDepth, S.taskDefaults.hardMaxDepth));
    const maxDepth = clamp(
        normalizeInt(taskDefaults.max_depth ?? taskDefaults.maxDepth, defaultMaxDepth),
        0,
        hardMaxDepth,
    );
    S.taskDefaults.scope = String(payload?.scope || S.taskDefaults.scope || "global").trim() || "global";
    S.taskDefaults.defaultMaxDepth = defaultMaxDepth;
    S.taskDefaults.hardMaxDepth = hardMaxDepth;
    S.taskDefaults.maxDepth = maxDepth;
    S.taskDefaults.loading = false;
    S.taskDefaults.saving = false;
    renderTaskDepthControl();
    return S.taskDefaults;
}

function renderTaskDepthControl() {
    if (!U.taskDepthSelect || !U.taskDepthHint) return;
    const defaultMaxDepth = Math.max(0, normalizeInt(S.taskDefaults.defaultMaxDepth, 1));
    const hardMaxDepth = Math.max(defaultMaxDepth, normalizeInt(S.taskDefaults.hardMaxDepth, 4));
    const currentMaxDepth = clamp(normalizeInt(S.taskDefaults.maxDepth, defaultMaxDepth), 0, hardMaxDepth);
    const disabled = S.taskDefaults.loading || S.taskDefaults.saving;
    const select = U.taskDepthSelect;

    select.innerHTML = "";
    for (let depth = 0; depth <= hardMaxDepth; depth += 1) {
        const option = document.createElement("option");
        option.value = String(depth);
        option.textContent = `${depth} 层`;
        if (depth === currentMaxDepth) option.selected = true;
        select.appendChild(option);
    }
    select.disabled = disabled;
    select.value = String(currentMaxDepth);
    select.dataset.scope = "global";
    buildResourceSelect(select);
    syncResourceSelectUI(select);

    if (S.taskDefaults.loading) {
        U.taskDepthHint.textContent = "正在加载全局任务树深度设置...";
        return;
    }
    if (S.taskDefaults.saving) {
        U.taskDepthHint.textContent = `正在保存，全局后续新任务将使用 ${currentMaxDepth} 层深度。`;
        return;
    }
    U.taskDepthHint.textContent = `全局后续新任务会自动使用该深度，当前范围 0-${hardMaxDepth}。`;
}

async function loadTaskDefaults() {
    S.taskDefaults.requestToken += 1;
    const token = S.taskDefaults.requestToken;
    S.taskDefaults.loading = true;
    renderTaskDepthControl();
    try {
        const payload = await ApiClient.getMainRuntimeTaskDefaults();
        if (token !== S.taskDefaults.requestToken) return payload;
        return applyTaskDefaultsPayload(payload);
    } catch (e) {
        if (token !== S.taskDefaults.requestToken) return null;
        S.taskDefaults.loading = false;
        S.taskDefaults.saving = false;
        renderTaskDepthControl();
        showToast({ title: "深度设置加载失败", text: e.message || "Unknown error", kind: "error" });
        return null;
    }
}

async function saveTaskDefaultMaxDepth(value) {
    if (!U.taskDepthSelect) return;
    const nextDepth = clamp(normalizeInt(value, S.taskDefaults.defaultMaxDepth), 0, Math.max(S.taskDefaults.defaultMaxDepth, S.taskDefaults.hardMaxDepth));
    if (!S.taskDefaults.loading && !S.taskDefaults.saving && nextDepth === normalizeInt(S.taskDefaults.maxDepth, nextDepth)) {
        renderTaskDepthControl();
        return;
    }
    const previousDepth = S.taskDefaults.maxDepth;
    S.taskDefaults.maxDepth = nextDepth;
    S.taskDefaults.saving = true;
    renderTaskDepthControl();
    try {
        const payload = await ApiClient.updateMainRuntimeTaskDefaults({ max_depth: nextDepth });
        applyTaskDefaultsPayload(payload);
        showToast({ title: "深度已更新", text: `全局后续新任务将使用 ${S.taskDefaults.maxDepth} 层深度。`, kind: "success" });
    } catch (e) {
        S.taskDefaults.maxDepth = previousDepth;
        S.taskDefaults.saving = false;
        renderTaskDepthControl();
        showToast({ title: "深度更新失败", text: e.message || "Unknown error", kind: "error" });
    }
}

function taskCreatedSortValue(task) {
    const parsed = Date.parse(String(task?.created_at || ""));
    return Number.isFinite(parsed) ? parsed : Number.NEGATIVE_INFINITY;
}

function orderedTasks(tasks = S.tasks) {
    return [...(Array.isArray(tasks) ? tasks : [])].sort((left, right) => {
        const timeDiff = taskCreatedSortValue(right) - taskCreatedSortValue(left);
        if (timeDiff !== 0) return timeDiff;
        const rightCreatedAt = String(right?.created_at || "");
        const leftCreatedAt = String(left?.created_at || "");
        if (rightCreatedAt !== leftCreatedAt) return rightCreatedAt.localeCompare(leftCreatedAt);
        return String(left?.task_id || "").localeCompare(String(right?.task_id || ""));
    });
}

function canMutateCeoSessions() {
    return !(S.ceoTurnActive || S.ceoPauseBusy || S.ceoUploadBusy || S.ceoSessionBusy);
}

function canActivateCeoSessions() {
    return !(S.ceoPauseBusy || S.ceoUploadBusy || S.ceoSessionBusy);
}

function syncCeoSessionActions() {
    const mutationDisabled = !canMutateCeoSessions();
    const activationDisabled = !canActivateCeoSessions();
    if (U.ceoNewSession) U.ceoNewSession.disabled = mutationDisabled;
    U.ceoSessionList?.querySelectorAll("[data-session-activate]")?.forEach((button) => {
        const targetId = String(button?.dataset?.sessionActivate || "").trim();
        button.disabled = activationDisabled || targetId === activeSessionId();
    });
    U.ceoSessionList?.querySelectorAll("[data-session-rename], [data-session-delete]")?.forEach((button) => {
        button.disabled = mutationDisabled;
    });
}

function safeHref(value) {
    const href = String(value || "").trim();
    if (!href) return "#";
    if (/^(https?:|mailto:|tel:)/i.test(href) || href.startsWith("/") || href.startsWith("#")) return esc(href);
    return "#";
}

function createMarkdownToken(tokens, html) {
    const token = `${MD_TOKEN_MARKER}${tokens.length}${MD_TOKEN_MARKER}`;
    tokens.push(html);
    return token;
}

function renderInlineMarkdown(value, { allowLinks = true } = {}) {
    let text = String(value ?? "");
    if (!text) return "";
    const tokens = [];

    text = text.replace(/`([^`\n]+)`/g, (_match, code) => createMarkdownToken(tokens, `<code>${esc(code)}</code>`));
    if (allowLinks) {
        text = text.replace(/\[([^\]]+)\]\(([^)\s]+(?:\s+"[^"]*")?)\)/g, (_match, label, target) => {
            const href = String(target || "").replace(/\s+"[^"]*"$/, "");
            return createMarkdownToken(
                tokens,
                `<a href="${safeHref(href)}" target="_blank" rel="noreferrer noopener">${renderInlineMarkdown(label, { allowLinks: false })}</a>`
            );
        });
    }

    text = esc(text);
    text = text.replace(/\*\*([^*][\s\S]*?)\*\*/g, "<strong>$1</strong>");
    text = text.replace(/__([^_][\s\S]*?)__/g, "<strong>$1</strong>");
    text = text.replace(/~~([^~][\s\S]*?)~~/g, "<del>$1</del>");
    text = text.replace(/(^|[\s(])\*([^*\n][^*\n]*?)\*(?=($|[\s).,!?:;]))/g, "$1<em>$2</em>");
    text = text.replace(/(^|[\s(])_([^_\n][^_\n]*?)_(?=($|[\s).,!?:;]))/g, "$1<em>$2</em>");

    return text.replace(new RegExp(`${MD_TOKEN_MARKER}(\\d+)${MD_TOKEN_MARKER}`, "g"), (_match, index) => tokens[Number(index)] || "");
}

function isMarkdownTableSeparator(line) {
    return /^\s*\|?(?:\s*:?-{3,}:?\s*\|)+(?:\s*:?-{3,}:?\s*)?\|?\s*$/.test(line);
}

function splitMarkdownTableCells(line) {
    let row = String(line || "").trim();
    if (row.startsWith("|")) row = row.slice(1);
    if (row.endsWith("|")) row = row.slice(0, -1);
    return row.split("|").map((cell) => cell.trim());
}

function isMarkdownBlockStart(lines, index) {
    const line = String(lines[index] || "");
    if (!line.trim()) return false;
    if (/^```/.test(line)) return true;
    if (/^ {0,3}(#{1,6})\s+/.test(line)) return true;
    if (/^ {0,3}([-*_]\s*){3,}$/.test(line)) return true;
    if (/^ {0,3}> ?/.test(line)) return true;
    if (/^\s*[-*+]\s+/.test(line)) return true;
    if (/^\s*\d+\.\s+/.test(line)) return true;
    if (line.includes("|") && lines[index + 1] && isMarkdownTableSeparator(lines[index + 1])) return true;
    return false;
}

function renderMarkdownBlocks(value) {
    const text = String(value ?? "").replace(/\r\n?/g, "\n");
    const lines = text.split("\n");
    const blocks = [];

    for (let index = 0; index < lines.length;) {
        const line = String(lines[index] || "");
        if (!line.trim()) {
            index += 1;
            continue;
        }

        const fenceMatch = line.match(/^```([\w-]+)?\s*$/);
        if (fenceMatch) {
            const codeLines = [];
            const lang = String(fenceMatch[1] || "").trim();
            index += 1;
            while (index < lines.length && !/^```/.test(lines[index])) {
                codeLines.push(lines[index]);
                index += 1;
            }
            if (index < lines.length && /^```/.test(lines[index])) index += 1;
            const langClass = lang ? ` class="language-${esc(lang)}"` : "";
            blocks.push(`<pre><code${langClass}>${esc(codeLines.join("\n"))}</code></pre>`);
            continue;
        }

        const headingMatch = line.match(/^ {0,3}(#{1,6})\s+(.*)$/);
        if (headingMatch) {
            const level = headingMatch[1].length;
            blocks.push(`<h${level}>${renderInlineMarkdown(headingMatch[2])}</h${level}>`);
            index += 1;
            continue;
        }

        if (/^ {0,3}([-*_]\s*){3,}$/.test(line)) {
            blocks.push("<hr>");
            index += 1;
            continue;
        }

        if (line.includes("|") && lines[index + 1] && isMarkdownTableSeparator(lines[index + 1])) {
            const headerCells = splitMarkdownTableCells(line);
            const bodyRows = [];
            index += 2;
            while (index < lines.length && String(lines[index] || "").trim().includes("|")) {
                bodyRows.push(splitMarkdownTableCells(lines[index]));
                index += 1;
            }
            const headerHtml = headerCells.map((cell) => `<th>${renderInlineMarkdown(cell)}</th>`).join("");
            const bodyHtml = bodyRows.map((cells) => `<tr>${cells.map((cell) => `<td>${renderInlineMarkdown(cell)}</td>`).join("")}</tr>`).join("");
            blocks.push(`<table><thead><tr>${headerHtml}</tr></thead>${bodyHtml ? `<tbody>${bodyHtml}</tbody>` : ""}</table>`);
            continue;
        }

        if (/^ {0,3}> ?/.test(line)) {
            const quoteLines = [];
            while (index < lines.length) {
                const current = String(lines[index] || "");
                if (!current.trim()) {
                    quoteLines.push("");
                    index += 1;
                    continue;
                }
                if (!/^ {0,3}> ?/.test(current)) break;
                quoteLines.push(current.replace(/^ {0,3}> ?/, ""));
                index += 1;
            }
            blocks.push(`<blockquote>${renderMarkdownBlocks(quoteLines.join("\n")).join("")}</blockquote>`);
            continue;
        }

        if (/^\s*\d+\.\s+/.test(line)) {
            const items = [];
            while (index < lines.length) {
                const current = String(lines[index] || "");
                const match = current.match(/^\s*\d+\.\s+(.*)$/);
                if (!match) break;
                items.push(`<li>${renderInlineMarkdown(match[1])}</li>`);
                index += 1;
            }
            blocks.push(`<ol>${items.join("")}</ol>`);
            continue;
        }

        if (/^\s*[-*+]\s+/.test(line)) {
            const items = [];
            while (index < lines.length) {
                const current = String(lines[index] || "");
                const match = current.match(/^\s*[-*+]\s+(.*)$/);
                if (!match) break;
                items.push(`<li>${renderInlineMarkdown(match[1])}</li>`);
                index += 1;
            }
            blocks.push(`<ul>${items.join("")}</ul>`);
            continue;
        }

        const paragraphLines = [];
        while (index < lines.length) {
            const current = String(lines[index] || "");
            if (!current.trim()) break;
            if (paragraphLines.length && isMarkdownBlockStart(lines, index)) break;
            paragraphLines.push(current.trimEnd());
            index += 1;
        }
        blocks.push(`<p>${renderInlineMarkdown(paragraphLines.join("\n")).replace(/\n/g, "<br>")}</p>`);
    }

    return blocks;
}

function renderMarkdown(value) {
    const blocks = renderMarkdownBlocks(value);
    return blocks.length ? blocks.join("") : "<p>Done.</p>";
}

function hint(text, err = false) {
    U.modelHint.textContent = text;
    U.modelHint.style.color = err ? "var(--danger, #ff6b6b)" : "";
}

function formatFileSize(value) {
    const size = Number(value || 0);
    if (!Number.isFinite(size) || size <= 0) return "";
    if (size < 1024) return `${size} B`;
    if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
    return `${(size / (1024 * 1024)).toFixed(1)} MB`;
}

function hasRenderableText(value) {
    return String(value || "").trim().length > 0;
}

function normalizeUploadList(items = []) {
    return Array.isArray(items) ? items.filter((item) => item && typeof item === "object" && item.path) : [];
}

function summarizeUploads(items = []) {
    const uploads = normalizeUploadList(items);
    if (!uploads.length) return "已附加附件";
    const imageCount = uploads.filter((item) => String(item.kind || "") === "image").length;
    const fileCount = uploads.length - imageCount;
    const parts = [];
    if (imageCount) parts.push(`${imageCount} 张图片`);
    if (fileCount) parts.push(`${fileCount} 个文件`);
    return parts.length ? `已附加 ${parts.join("，")}` : "已附加附件";
}

function renderChatAttachments(items = []) {
    const uploads = normalizeUploadList(items);
    if (!uploads.length) return "";
    return `
        <div class="chat-attachment-list" role="list">
            ${uploads.map((item) => `
                <div class="chat-attachment-pill" role="listitem">
                    <span class="chat-attachment-kind">${String(item.kind || "") === "image" ? "图片" : "文件"}</span>
                    <span class="chat-attachment-name">${esc(String(item.name || item.path || "附件"))}</span>
                    <span class="chat-attachment-size">${esc(formatFileSize(item.size))}</span>
                </div>
            `).join("")}
        </div>
    `;
}

function syncCeoInputHeight() {
    if (!U.ceoInput) return;
    U.ceoInput.style.height = "auto";
    U.ceoInput.style.height = `${Math.min(U.ceoInput.scrollHeight, 200)}px`;
}

function renderPendingCeoUploads() {
    if (!U.ceoUploadList) return;
    const uploads = normalizeUploadList(S.ceoUploads);
    U.ceoUploadList.hidden = !uploads.length && !S.ceoUploadBusy;
    U.ceoUploadList.setAttribute("aria-busy", S.ceoUploadBusy ? "true" : "false");
    if (!uploads.length && !S.ceoUploadBusy) {
        U.ceoUploadList.innerHTML = "";
    } else {
        const status = S.ceoUploadBusy ? '<div class="ceo-upload-status">附件上传中...</div>' : "";
        U.ceoUploadList.innerHTML = `
            ${status}
            <div class="ceo-upload-chip-list" role="list">
                ${uploads.map((item, index) => `
                    <div class="ceo-upload-chip" role="listitem">
                        <span class="ceo-upload-kind">${String(item.kind || "") === "image" ? "图片" : "文件"}</span>
                        <span class="ceo-upload-name">${esc(String(item.name || item.path || "附件"))}</span>
                        <span class="ceo-upload-size">${esc(formatFileSize(item.size))}</span>
                        <button type="button" class="ceo-upload-remove" data-upload-remove="${index}" aria-label="移除附件">
                            <i data-lucide="x"></i>
                        </button>
                    </div>
                `).join("")}
            </div>
        `;
    }
    if (U.ceoAttach) U.ceoAttach.disabled = !!S.ceoUploadBusy || !!S.ceoSessionBusy || !activeSessionId();
    syncCeoPrimaryButton();
    syncCeoSessionActions();
    icons();
}

function syncCeoPrimaryButton() {
    if (!U.ceoSend) return;
    const isPause = !!S.ceoTurnActive;
    const label = S.ceoPauseBusy ? "暂停中" : isPause ? "暂停" : "发送";
    const icon = isPause ? "pause" : "send";
    U.ceoSend.innerHTML = `<i data-lucide="${icon}"></i> ${label}`;
    U.ceoSend.disabled = !!S.ceoUploadBusy || !!S.ceoPauseBusy || !!S.ceoSessionBusy || !activeSessionId();
    U.ceoSend.setAttribute("aria-label", isPause ? "暂停当前 CEO 会话" : "发送消息");
    icons();
}

function finalizePausedCeoTurn(text = "已暂停") {
    const turn = pullActiveCeoTurn();
    if (!turn?.textEl || !turn.flowEl) return;
    mutateCeoFeed(() => {
        turn.finalized = true;
        turn.textEl.textContent = String(text || "已暂停");
        turn.textEl.classList.remove("pending");
        if (turn.steps > 0) {
            turn.flowEl.hidden = false;
            turn.flowEl.open = false;
            updateCeoTurnMeta(turn, "已暂停");
        } else {
            turn.flowEl.hidden = true;
        }
    }, { scrollMode: "preserve" });
}

function applyCeoState(state = {}, meta = {}) {
    const status = String(state?.status || "").trim().toLowerCase();
    const source = String(meta?.source || state?.source || "").trim().toLowerCase();
    const running = !!state?.is_running || status === "running";
    const paused = !!state?.paused || status === "paused";
    S.ceoTurnActive = running;
    if (!running) S.ceoPauseBusy = false;
    if (running && !(source === "heartbeat" && !getActiveCeoTurn())) {
        ensureActiveCeoTurn({ source });
    }
    if (paused) finalizePausedCeoTurn();
    syncCeoSessionActions();
    syncCeoPrimaryButton();
}

function handleCeoControlAck(payload = {}) {
    const action = String(payload?.action || "").trim().toLowerCase();
    if (action !== "pause") return;
    S.ceoPauseBusy = false;
    if (payload?.accepted === false) {
        syncCeoPrimaryButton();
        showToast({ title: "暂停失败", text: "当前没有可暂停的 CEO 回合。", kind: "error" });
        return;
    }
    S.ceoTurnActive = false;
    finalizePausedCeoTurn();
    syncCeoSessionActions();
    syncCeoPrimaryButton();
}

function handleCeoError(payload = {}) {
    S.ceoTurnActive = false;
    S.ceoPauseBusy = false;
    syncCeoSessionActions();
    syncCeoPrimaryButton();
    finalizeCeoTurn(`运行出错：${String(payload?.message || "unknown error")}`);
}

function requestCeoPause() {
    if (!S.ceoTurnActive || S.ceoPauseBusy) return;
    if (!S.ceoWs || S.ceoWs.readyState !== WebSocket.OPEN) {
        addMsg("Connection is not ready yet. Please try again in a moment.", "system");
        initCeoWs();
        return;
    }
    try {
        S.ceoPauseBusy = true;
        syncCeoPrimaryButton();
        S.ceoWs.send(JSON.stringify({
            type: "client.pause_turn",
            session_id: activeSessionId(),
        }));
    } catch (e) {
        S.ceoPauseBusy = false;
        syncCeoPrimaryButton();
        addMsg(`Failed to pause message: ${e.message || "unknown error"}`, "system");
        initCeoWs();
    }
}

function handleCeoPrimaryAction() {
    if (S.ceoTurnActive) {
        requestCeoPause();
        return;
    }
    sendCeoMessage();
}

async function handleCeoFileSelection(event) {
    const files = [...(event?.target?.files || [])];
    if (!files.length) return;
    S.ceoUploadBusy = true;
    renderPendingCeoUploads();
    try {
        const uploaded = await ApiClient.uploadCeoFiles(files, activeSessionId());
        S.ceoUploads = [...normalizeUploadList(S.ceoUploads), ...normalizeUploadList(uploaded)];
        renderPendingCeoUploads();
        showToast({ title: "上传完成", text: `已添加 ${uploaded.length} 个附件`, kind: "success" });
    } catch (e) {
        addMsg(`附件上传失败：${e.message || "unknown error"}`, "system");
    } finally {
        S.ceoUploadBusy = false;
        renderPendingCeoUploads();
        if (U.ceoFileInput) U.ceoFileInput.value = "";
    }
}

function removePendingCeoUpload(index) {
    const next = normalizeUploadList(S.ceoUploads);
    if (index < 0 || index >= next.length) return;
    next.splice(index, 1);
    S.ceoUploads = next;
    renderPendingCeoUploads();
}

function mutateCeoFeed(mutator, { scrollMode = "preserve" } = {}) {
    if (typeof mutator !== "function") return null;
    if (!U.ceoFeed) return mutator();
    const prevTop = U.ceoFeed.scrollTop;
    const result = mutator();
    if (scrollMode === "bottom") {
        U.ceoFeed.scrollTop = U.ceoFeed.scrollHeight;
    } else {
        const maxTop = Math.max(0, U.ceoFeed.scrollHeight - U.ceoFeed.clientHeight);
        U.ceoFeed.scrollTop = Math.max(0, Math.min(prevTop, maxTop));
    }
    return result;
}

function addMsg(text, role, { markdown = false, attachments = [], scrollMode = "preserve" } = {}) {
    mutateCeoFeed(() => {
        const el = document.createElement("div");
        el.className = `message ${role}`;
        const contentClass = markdown ? "msg-content markdown-content" : "msg-content";
        const content = markdown ? renderMarkdown(text) : esc(text);
        const attachmentMarkup = renderChatAttachments(attachments);
        el.innerHTML = `<div class="avatar"><i data-lucide="${role === "system" ? "cpu" : "user"}"></i></div><div class="${contentClass}">${content}${attachmentMarkup}</div>`;
        U.ceoFeed.appendChild(el);
        icons();
        return el;
    }, { scrollMode });
}

function resetCeoFeed() {
    if (!U.ceoFeed) return;
    U.ceoFeed.innerHTML = "";
    S.ceoPendingTurns = [];
}

function restoreCeoInflightTurn(snapshot = null) {
    if (!snapshot || typeof snapshot !== "object") return;
    const source = String(snapshot.source || "").trim().toLowerCase();
    const isHeartbeat = source === "heartbeat";
    const userMessage = snapshot.user_message && typeof snapshot.user_message === "object" ? snapshot.user_message : null;
    if (userMessage && !isHeartbeat) {
        const attachments = normalizeUploadList(userMessage.attachments);
        const text = hasRenderableText(userMessage.content) ? String(userMessage.content || "") : summarizeUploads(attachments);
        addMsg(text, "user", { attachments, scrollMode: "preserve" });
    }
    const status = String(snapshot.status || "").trim().toLowerCase();
    const toolEvents = Array.isArray(snapshot.tool_events) ? snapshot.tool_events : [];
    const needsAssistantTurn = toolEvents.length > 0 || status === "paused" || status === "error" || (!isHeartbeat && status === "running");
    if (needsAssistantTurn) ensureActiveCeoTurn({ source });
    toolEvents.forEach((event) => appendCeoToolEvent(event));
    if (status === "error") {
        const errorMessage = String(snapshot?.last_error?.message || "").trim() || "unknown error";
        finalizeCeoTurn(`运行出错：${errorMessage}`);
    }
}

function renderCeoSnapshot(messages = [], inflightTurn = null) {
    mutateCeoFeed(() => {
        resetCeoFeed();
        messages.forEach((item) => {
            const role = String(item?.role || "").trim().toLowerCase();
            const content = String(item?.content || "");
            if (!content.trim()) return;
            if (role === "user") {
                addMsg(content, "user", { scrollMode: "preserve" });
                return;
            }
            if (role === "assistant" || role === "system") {
                addMsg(content, "system", { markdown: true, scrollMode: "preserve" });
            }
        });
        restoreCeoInflightTurn(inflightTurn);
    }, { scrollMode: "preserve" });
}

function createPendingCeoTurn(source = "user", { scrollMode = "preserve" } = {}) {
    return mutateCeoFeed(() => {
        if (!U.ceoFeed) return null;
        const el = document.createElement("div");
        el.className = "message system ceo-turn-message";
        el.innerHTML = `
            <div class="avatar"><i data-lucide="cpu"></i></div>
            <div class="msg-content ceo-turn-content">
                <div class="assistant-text pending">处理中...</div>
                <details class="interaction-flow" open hidden>
                    <summary class="interaction-flow-summary">
                        <span class="interaction-flow-title">Interaction Flow</span>
                        <span class="interaction-flow-meta">等待工具开始...</span>
                    </summary>
                    <div class="interaction-flow-list" role="list"></div>
                    <div class="interaction-flow-footer" hidden>
                        <button type="button" class="interaction-flow-toggle">展开全部</button>
                    </div>
                </details>
            </div>
        `;
        U.ceoFeed.appendChild(el);
        const toggleButton = el.querySelector(".interaction-flow-toggle");
        const turn = {
            el,
            textEl: el.querySelector(".assistant-text"),
            flowEl: el.querySelector(".interaction-flow"),
            metaEl: el.querySelector(".interaction-flow-meta"),
            listEl: el.querySelector(".interaction-flow-list"),
            footerEl: el.querySelector(".interaction-flow-footer"),
            toggleEl: toggleButton,
            steps: 0,
            hasError: false,
            finalized: false,
            historyExpanded: false,
            source: String(source || "").trim().toLowerCase() || "user",
        };
        toggleButton?.addEventListener("click", (event) => {
            event.preventDefault();
            event.stopPropagation();
            toggleCeoToolHistory(turn);
        });
        icons();
        return turn;
    }, { scrollMode });
}

function getActiveCeoTurn() {
    return S.ceoPendingTurns.find((turn) => !turn.finalized) || null;
}

function pullActiveCeoTurn(source = "") {
    const expected = String(source || "").trim().toLowerCase();
    const index = S.ceoPendingTurns.findIndex((turn) => {
        if (!turn || turn.finalized) return false;
        if (!expected) return true;
        return String(turn.source || "user").trim().toLowerCase() === expected;
    });
    if (index < 0) return null;
    const [turn] = S.ceoPendingTurns.splice(index, 1);
    return turn || null;
}

function discardActiveCeoTurn({ source = "" } = {}) {
    const turn = pullActiveCeoTurn(source);
    if (!turn) return false;
    return mutateCeoFeed(() => {
        turn.finalized = true;
        turn.el?.remove?.();
        return true;
    }, { scrollMode: "preserve" });
}

function ensureActiveCeoTurn({ source = "" } = {}) {
    const existing = getActiveCeoTurn();
    if (existing) {
        if (!existing.source) existing.source = String(source || "").trim().toLowerCase() || "user";
        return existing;
    }
    const created = createPendingCeoTurn(String(source || "").trim().toLowerCase() || "user");
    if (created) S.ceoPendingTurns.push(created);
    return created;
}

function updateCeoTurnMeta(turn, stateLabel) {
    if (!turn?.metaEl) return;
    const stepLabel = turn.steps > 0 ? `${turn.steps} 个阶段` : "等待工具开始...";
    turn.metaEl.textContent = stateLabel ? `${stepLabel} - ${stateLabel}` : stepLabel;
}

function findCeoToolStep(turn, { toolCallId = "", toolName = "" } = {}) {
    if (!turn?.listEl) return null;
    const items = [...turn.listEl.querySelectorAll(".interaction-step")];
    if (toolCallId) {
        for (let index = items.length - 1; index >= 0; index -= 1) {
            const item = items[index];
            if (String(item?.dataset?.toolCallId || "").trim() === toolCallId) return item;
        }
    }
    if (toolName) {
        for (let index = items.length - 1; index >= 0; index -= 1) {
            const item = items[index];
            if (
                String(item?.dataset?.toolName || "").trim() === toolName
                && String(item?.dataset?.stepState || "").trim() === "running"
            ) {
                return item;
            }
        }
    }
    return null;
}

function ceoFriendlyToolName(toolName = "") {
    const normalized = String(toolName || "").trim().toLowerCase();
    const map = {
        "skill-installer": "技能安装",
        "filesystem": "文件处理",
        "exec": "命令执行",
        "load_tool_context": "工具说明",
        "load_skill_context": "技能说明",
    };
    return map[normalized] || String(toolName || "工具").trim() || "工具";
}

function parseJsonObjectText(raw = "") {
    const text = String(raw || "").trim();
    if (!text || !(text.startsWith("{") || text.startsWith("["))) return null;
    try {
        const parsed = JSON.parse(text);
        return parsed && typeof parsed === "object" && !Array.isArray(parsed) ? parsed : null;
    } catch {
        return null;
    }
}

function ceoFriendlyToolDetail(toolName = "", detail = "", status = "running", kind = "") {
    const normalizedTool = String(toolName || "").trim().toLowerCase();
    const raw = String(detail || "").trim();
    const lower = raw.toLowerCase();
    const normalizedKind = String(kind || "").trim().toLowerCase();
    const payload = parseJsonObjectText(raw);
    if (!raw) {
        if (status === "error") return "处理失败，请检查错误信息";
        if (status === "success") return "处理完成";
        return "正在处理中...";
    }
    if (status === "error") {
        if (payload?.error) return `处理失败：${String(payload.error || "").trim()}`;
        if (lower.includes("timed out")) return "等待超时，已停止当前步骤";
        if (lower.includes("download failed")) return "下载失败，请检查网络或仓库可访问性";
        if (lower.includes("git command timed out")) return "Git 操作超时，已停止当前步骤";
        return `处理失败：${raw}`;
    }
    if (status === "success") {
        if (normalizedTool === "skill-installer" && payload?.ok) {
            const skillId = String(payload.skill_id || "").trim();
            const installedPath = String(payload.installed_path || "").trim();
            if (skillId && installedPath) return `技能 ${skillId} 已安装完成`;
            if (skillId) return `技能 ${skillId} 已安装完成`;
            return "技能已安装完成";
        }
        return raw || "处理完成";
    }
    if (normalizedTool === "skill-installer") {
        if (lower.includes("started")) return "开始安装技能";
        if (lower.includes("resolving")) return "正在确认技能来源和安装位置";
        if (lower.includes("fetching upstream repository")) return "正在从远程仓库获取技能文件";
        if (lower.includes("upstream fetched via git")) return "已获取技能文件，正在准备复制到项目";
        if (lower.includes("upstream fetched via download")) return "已下载技能文件，正在准备复制到项目";
        if (lower.includes("copied files into")) return "已复制技能文件，正在整理本地资源";
        if (lower.includes("installed ")) return "安装已完成，正在刷新本地资源";
    }
    if (normalizedKind === "tool_plan") return "正在规划下一步工具操作";
    if (normalizedKind === "tool") return `正在处理：${raw}`;
    return raw;
}

function ceoToolStage(toolName = "", detail = "", status = "running") {
    const normalizedTool = String(toolName || "").trim().toLowerCase();
    const lower = String(detail || "").trim().toLowerCase();
    if (status === "error") {
        return { icon: "alert-triangle", spinning: false, meta: "处理失败" };
    }
    if (status === "success") {
        return { icon: "check", spinning: false, meta: "处理完成" };
    }
    if (normalizedTool === "skill-installer") {
        if (lower.includes("resolving")) return { icon: "search", spinning: false, meta: "正在确认技能来源" };
        if (lower.includes("fetching upstream repository")) return { icon: "download", spinning: true, meta: "正在获取技能文件" };
        if (lower.includes("fetched via")) return { icon: "package", spinning: false, meta: "已获取技能文件" };
        if (lower.includes("copied files into")) return { icon: "folder-open", spinning: false, meta: "正在整理本地资源" };
        if (lower.includes("installed ")) return { icon: "refresh-cw", spinning: true, meta: "正在刷新资源索引" };
        if (lower.includes("started")) return { icon: "loader", spinning: true, meta: "正在准备安装技能" };
    }
    return { icon: "loader", spinning: true, meta: "正在处理中" };
}

function renderCeoToolIcon(iconWrap, iconName = "loader-circle") {
    if (!(iconWrap instanceof HTMLElement)) return;
    const nextIcon = String(iconName || "loader-circle").trim() || "loader-circle";
    if (iconWrap.dataset.iconName === nextIcon && iconWrap.querySelector("svg")) return;
    iconWrap.dataset.iconName = nextIcon;
    iconWrap.innerHTML = `<i data-lucide="${esc(nextIcon)}"></i>`;
}

function trimInteractionLines(text = "", maxLines = CEO_TOOL_PROGRESS_MAX_LINES) {
    const lines = String(text || "")
        .split("\n")
        .map((line) => line.trim())
        .filter(Boolean);
    return lines.slice(-maxLines);
}

function mergeInteractionDetail(existing = "", next = "", maxLines = CEO_TOOL_PROGRESS_MAX_LINES) {
    const current = trimInteractionLines(existing, maxLines);
    const incoming = trimInteractionLines(next, maxLines);
    const merged = [...current];
    incoming.forEach((line) => {
        if (!merged.includes(line)) merged.push(line);
    });
    return merged.slice(-maxLines).join("\n");
}

function trimCeoToolSteps(turn) {
    if (!turn?.listEl) return;
    const items = [...turn.listEl.querySelectorAll(".interaction-step")];
    const hiddenCount = Math.max(0, items.length - CEO_TOOL_STEP_MAX);
    items.forEach((item, index) => {
        const shouldHide = !turn.historyExpanded && index < hiddenCount;
        item.hidden = shouldHide;
        item.classList.toggle("is-collapsed-history", shouldHide);
    });
    turn.steps = items.length;
    if (!turn.footerEl || !turn.toggleEl) return;
    const hasOverflow = hiddenCount > 0;
    turn.footerEl.hidden = !hasOverflow;
    if (!hasOverflow) {
        turn.toggleEl.textContent = "展开全部";
        turn.toggleEl.setAttribute("aria-expanded", "false");
        return;
    }
    if (turn.historyExpanded) {
        turn.toggleEl.textContent = "收起旧进度";
        turn.toggleEl.setAttribute("aria-expanded", "true");
    } else {
        turn.toggleEl.textContent = `展开全部（还有 ${hiddenCount} 条较早进度）`;
        turn.toggleEl.setAttribute("aria-expanded", "false");
    }
}

function toggleCeoToolHistory(turn) {
    if (!turn?.listEl) return;
    mutateCeoFeed(() => {
        turn.historyExpanded = !turn.historyExpanded;
        trimCeoToolSteps(turn);
        icons();
    }, { scrollMode: "preserve" });
}

function applyCeoToolStepState(item, { status = "running", toolName = "tool", detail = "", toolCallId = "", kind = "", stage = null } = {}) {
    if (!(item instanceof HTMLElement)) return;
    const statusLabel = ({ running: "进行中", success: "完成", error: "出错" })[status] || "更新";
    const resolvedStage = stage || ceoToolStage(toolName, detail, status);
    item.className = `interaction-step ${status}`;
    item.dataset.stepState = status;
    item.dataset.toolName = String(toolName || "tool").trim() || "tool";
    if (toolCallId) item.dataset.toolCallId = toolCallId;
    item.dataset.progressKind = String(kind || "").trim();
    const titleEl = item.querySelector(".interaction-step-title");
    const statusEl = item.querySelector(".interaction-step-status");
    const detailEl = item.querySelector(".interaction-step-detail");
    const iconWrap = item.querySelector(".interaction-step-icon");
    if (titleEl) titleEl.textContent = ceoFriendlyToolName(toolName);
    if (statusEl) statusEl.textContent = statusLabel;
    if (detailEl) detailEl.textContent = detail || `${ceoFriendlyToolName(toolName)}${statusLabel}`;
    if (iconWrap) {
        iconWrap.classList.toggle("is-spinning", !!resolvedStage.spinning);
        renderCeoToolIcon(iconWrap, resolvedStage.icon === "loader" ? "loader-circle" : resolvedStage.icon);
    }
}

function parseIsoTimestamp(value) {
    const text = String(value || "").trim();
    if (!text) return null;
    const parsed = Date.parse(text);
    return Number.isFinite(parsed) ? parsed : null;
}

function formatElapsedDuration(totalSeconds) {
    const value = Number(totalSeconds);
    if (!Number.isFinite(value) || value < 0) return "";
    const seconds = Math.max(0, Math.floor(value));
    const hours = Math.floor(seconds / 3600);
    const minutes = Math.floor((seconds % 3600) / 60);
    const remain = seconds % 60;
    if (hours > 0) return `${hours}小时${minutes}分${remain}秒`;
    if (minutes > 0) return `${minutes}分${remain}秒`;
    return `${remain}秒`;
}

function resolveRuntimeSeconds(element) {
    if (!(element instanceof HTMLElement)) return null;
    const explicitElapsed = Number.parseFloat(String(element.dataset.elapsedSeconds || ""));
    if (Number.isFinite(explicitElapsed) && explicitElapsed >= 0) return explicitElapsed;
    const startedAt = parseIsoTimestamp(element.dataset.startedAt || "");
    if (startedAt === null) return null;
    const finishedAt = parseIsoTimestamp(element.dataset.finishedAt || "");
    const end = finishedAt !== null ? finishedAt : Date.now();
    return Math.max(0, Math.floor((end - startedAt) / 1000));
}

function updateRuntimeBadge(element, runtimeEl, { runningPrefix = "已运行 ", donePrefix = "耗时 " } = {}) {
    if (!(element instanceof HTMLElement) || !(runtimeEl instanceof HTMLElement)) return;
    const seconds = resolveRuntimeSeconds(element);
    if (!Number.isFinite(seconds)) {
        runtimeEl.hidden = true;
        runtimeEl.textContent = "";
        return;
    }
    const status = String(element.dataset.stepState || element.dataset.traceStatus || "").trim().toLowerCase();
    runtimeEl.hidden = false;
    runtimeEl.textContent = `${status === "running" ? runningPrefix : donePrefix}${formatElapsedDuration(seconds)}`;
}

function refreshLiveDurationBadges() {
    document.querySelectorAll(".interaction-step").forEach((item) => {
        if (!(item instanceof HTMLElement)) return;
        const runtimeEl = item.querySelector(".interaction-step-runtime");
        if (!(runtimeEl instanceof HTMLElement)) return;
        updateRuntimeBadge(item, runtimeEl);
    });
    document.querySelectorAll(".task-trace-step").forEach((item) => {
        if (!(item instanceof HTMLElement)) return;
        const runtimeEl = item.querySelector(".task-trace-runtime");
        if (!(runtimeEl instanceof HTMLElement)) return;
        updateRuntimeBadge(item, runtimeEl);
    });
}

function startLiveDurationTicker() {
    if (S.liveDurationIntervalId) return;
    S.liveDurationIntervalId = window.setInterval(refreshLiveDurationBadges, 1000);
    refreshLiveDurationBadges();
}

function stopLiveDurationTicker() {
    if (!S.liveDurationIntervalId) return;
    window.clearInterval(S.liveDurationIntervalId);
    S.liveDurationIntervalId = null;
}

function appendCeoToolEvent(event = {}) {
    const source = String(event?.source || "").trim().toLowerCase();
    const turn = ensureActiveCeoTurn({ source });
    if (!turn?.listEl || !turn.flowEl) return;
    mutateCeoFeed(() => {
        const status = String(event.status || "running").trim().toLowerCase();
        const toolName = String(event.tool_name || "tool").trim() || "tool";
        const rawText = String(event.text || "").trim();
        const detail = ceoFriendlyToolDetail(toolName, rawText, status, event.kind);
        const toolCallId = String(event.tool_call_id || "").trim();
        let item = findCeoToolStep(turn, { toolCallId, toolName });
        const existingDetail = String(item?.querySelector(".interaction-step-detail")?.textContent || "").trim();
        const mergedDetail = mergeInteractionDetail(existingDetail, detail);
        const stage = ceoToolStage(toolName, rawText, status);
        if (!(item instanceof HTMLElement)) {
            item = document.createElement("div");
            item.setAttribute("role", "listitem");
            item.innerHTML = `
                <div class="interaction-step-header">
                    <span class="interaction-step-lead">
                        <span class="interaction-step-icon" data-icon-name="loader-circle"><i data-lucide="loader-circle"></i></span>
                        <span class="interaction-step-title"></span>
                    </span>
                    <span class="interaction-step-side">
                        <span class="interaction-step-runtime" hidden></span>
                        <span class="interaction-step-status"></span>
                    </span>
                </div>
                <div class="interaction-step-detail"></div>
            `;
            turn.listEl.appendChild(item);
        }
        const eventTimestamp = String(event.timestamp || "").trim();
        const eventElapsed = Number.parseFloat(String(event.elapsed_seconds ?? ""));
        if (!item.dataset.startedAt && eventTimestamp) item.dataset.startedAt = eventTimestamp;
        if (event.status === "success" || event.status === "error") {
            if (eventTimestamp) item.dataset.finishedAt = eventTimestamp;
        } else {
            delete item.dataset.finishedAt;
        }
        if (Number.isFinite(eventElapsed) && eventElapsed >= 0) {
            item.dataset.elapsedSeconds = String(eventElapsed);
        } else if (event.status === "running") {
            delete item.dataset.elapsedSeconds;
        }
        applyCeoToolStepState(item, {
            status,
            toolName,
            detail: mergedDetail || detail,
            toolCallId,
            kind: event.kind,
            stage,
        });
        turn.flowEl.hidden = false;
        turn.flowEl.open = true;
        turn.hasError = turn.hasError || status === "error";
        trimCeoToolSteps(turn);
        updateCeoTurnMeta(turn, stage.meta);
        const runtimeEl = item.querySelector(".interaction-step-runtime");
        if (runtimeEl instanceof HTMLElement) updateRuntimeBadge(item, runtimeEl);
        icons();
    }, { scrollMode: "preserve" });
}

function finalizeCeoTurn(text, meta = {}) {
    S.ceoTurnActive = false;
    S.ceoPauseBusy = false;
    syncCeoPrimaryButton();
    const turn = pullActiveCeoTurn(meta?.source || "");
    if (!turn?.textEl || !turn.flowEl) {
        addMsg(text, "system", { markdown: true, scrollMode: "preserve" });
        return;
    }
    mutateCeoFeed(() => {
        turn.finalized = true;
        turn.textEl.innerHTML = renderMarkdown(String(text || "").trim() || "已完成。");
        turn.textEl.classList.remove("pending");
        turn.textEl.classList.add("markdown-content");
        if (turn.steps > 0) {
            turn.flowEl.hidden = false;
            turn.flowEl.open = false;
            updateCeoTurnMeta(turn, turn.hasError ? "处理完成，但有异常" : "处理完成");
        } else {
            turn.flowEl.hidden = true;
        }
        icons();
    }, { scrollMode: "preserve" });
}

function addNotice(notice, _bump = true) {
    const payload = notice && typeof notice === "object" ? notice : {};
    const kind = String(payload.kind || "").toLowerCase();
    showToast({ title: payload.title || "Notice", text: payload.text || "", kind: kind.includes("fail") || kind.includes("error") ? "error" : "success" });
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

function syncDetailSaveButton(kind) {
    const isSkill = kind === "skill";
    const root = isSkill ? U.skillDetail : U.toolDetail;
    const button = root?.querySelector(isSkill ? "#skill-modal-save" : "#tool-modal-save");
    const hint = root?.querySelector(".resource-draft-hint");
    const dirty = isSkill ? S.skillDirty : S.toolDirty;
    const busy = isSkill ? S.skillBusy : S.toolBusy;

    if (button) {
        button.textContent = busy ? "Saving..." : dirty ? "Save changes" : "Save";
        button.disabled = !!busy || !dirty;
    }
    if (hint) {
        hint.classList.toggle("is-dirty", dirty);
        hint.textContent = dirty ? "Changes are staged. Click Save to apply them." : "";
        hint.hidden = !dirty;
    }
}

function setSkillDirty(next = true) {
    S.skillDirty = !!next;
    renderSkillActions();
}

function setToolDirty(next = true) {
    S.toolDirty = !!next;
    renderToolActions();
}

function setCommunicationDirty(next = true) {
    S.communicationDirty = !!next;
    renderCommunicationActions();
}

function openConfirm({ title, text, confirmLabel = "确认", confirmKind = "danger", onConfirm, returnFocus = null, checkbox = null }) {
    S.confirmState = { onConfirm, returnFocus, checkbox: checkbox && typeof checkbox === "object" ? checkbox : null };
    U.confirmTitle.textContent = title;
    U.confirmText.textContent = text;
    if (U.confirmOptions && U.confirmCheckbox && U.confirmCheckboxLabel && U.confirmCheckboxHint) {
        const enabled = !!(checkbox && typeof checkbox === "object");
        U.confirmOptions.hidden = !enabled;
        if (enabled) {
            U.confirmCheckbox.checked = !!checkbox.checked;
            U.confirmCheckbox.disabled = false;
            U.confirmCheckboxLabel.textContent = checkbox.label || "同时删除此对话创建的所有任务记录";
            U.confirmCheckboxHint.textContent = checkbox.hint || "";
        } else {
            U.confirmCheckbox.checked = false;
            U.confirmCheckbox.disabled = false;
            U.confirmCheckboxLabel.textContent = "同时删除此对话创建的所有任务记录";
            U.confirmCheckboxHint.textContent = "";
        }
    }
    U.confirmAccept.textContent = confirmLabel;
    U.confirmAccept.className = `toolbar-btn ${confirmKind}`;
    U.confirmBackdrop.hidden = false;
    U.confirmBackdrop.classList.add("is-open");
    window.requestAnimationFrame(() => U.confirmCancel?.focus());
}

function resourceDeleteErrorText(error) {
    const payload = error?.data;
    if (payload && typeof payload === "object") {
        const message = String(payload.message || "").trim();
        if (message) return message;
    }
    return error?.message || "Unknown error";
}

function configureTaskDetailSections() {
    if (U.adFlowHeading) U.adFlowHeading.innerHTML = '<i data-lucide="workflow"></i> 执行流程';
    if (U.adAcceptanceHeading) U.adAcceptanceHeading.innerHTML = '<i data-lucide="shield-check"></i> 验收结果';
    if (U.adFlow) {
        U.adFlow.classList.remove("code-block");
        U.adFlow.classList.add("task-trace-host");
    }
    if (U.adAcceptance) U.adAcceptance.classList.add("task-trace-acceptance");
    if (U.nodeEmpty) U.nodeEmpty.textContent = "选择任务树中的节点后，这里会显示执行流程、验收结果和工件。";
    if (U.adOutputSection) U.adOutputSection.hidden = true;
    if (U.adLogsSection) U.adLogsSection.hidden = true;
    icons();
}

function resourceSelectLabel(select) {
    const explicitLabel = String(select?.dataset?.resourceSelectLabel || "").trim();
    if (explicitLabel) return explicitLabel;
    const map = {
        "skill-risk-filter": "Skill risk filter",
        "skill-status-filter": "Skill status filter",
        "skill-page-size": "Skill 每页数量",
        "tool-status-filter": "Tool status filter",
        "tool-risk-filter": "Tool risk filter",
        "tool-page-size": "Tool 每页数量",
        "task-page-size": "Task 每页数量",
        "task-depth-select": "Task tree depth",
    };
    return map[String(select?.id || "").trim()] || "Resource filter";
}

function buildResourceSelectOptionButton(select, shell, option) {
    if (!(select instanceof HTMLSelectElement) || !(shell instanceof HTMLElement) || !(option instanceof HTMLOptionElement)) return null;
    const optionButton = document.createElement("button");
    optionButton.type = "button";
    optionButton.className = "resource-select-option";
    optionButton.dataset.value = option.value;
    optionButton.setAttribute("role", "option");
    optionButton.tabIndex = -1;

    const label = document.createElement("span");
    label.className = "resource-select-option-label";
    label.textContent = String(option.textContent || "").trim();

    const check = document.createElement("span");
    check.className = "resource-select-option-check";
    check.innerHTML = `<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="width:14px;height:14px"><polyline points="13 5 7 11 4 8"></polyline></svg>`;
    check.setAttribute("aria-hidden", "true");

    optionButton.append(label, check);
    optionButton.addEventListener("click", () => setResourceSelectValue(select, option.value));
    optionButton.addEventListener("keydown", (e) => {
        if (e.key === "ArrowDown") {
            e.preventDefault();
            focusResourceSelectOption(shell, "next");
        }
        if (e.key === "ArrowUp") {
            e.preventDefault();
            focusResourceSelectOption(shell, "prev");
        }
        if (e.key === "Home") {
            e.preventDefault();
            focusResourceSelectOption(shell, "first");
        }
        if (e.key === "End") {
            e.preventDefault();
            focusResourceSelectOption(shell, "last");
        }
        if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            setResourceSelectValue(select, option.value);
        }
        if (e.key === "Escape") {
            e.preventDefault();
            closeResourceSelects({ restoreFocus: true });
        }
        if (e.key === "Tab") closeResourceSelects();
    });
    return optionButton;
}

function rebuildResourceSelectOptions(select) {
    if (!(select instanceof HTMLSelectElement)) return;
    const shell = select.closest(".resource-select-shell");
    const menu = shell?.querySelector(".resource-select-menu");
    if (!(shell instanceof HTMLElement) || !(menu instanceof HTMLElement)) return;
    const signature = [...select.options]
        .map((option) => `${String(option.value)}\u0000${String(option.textContent || "").trim()}`)
        .join("\u0001");
    if (menu.dataset.optionsSignature === signature) return;
    menu.innerHTML = "";
    [...select.options].forEach((option) => {
        const optionButton = buildResourceSelectOptionButton(select, shell, option);
        if (optionButton) menu.appendChild(optionButton);
    });
    menu.dataset.optionsSignature = signature;
}

function closeResourceSelects({ exceptId = "", restoreFocus = false } = {}) {
    const openShells = [...document.querySelectorAll(".resource-select-shell.is-open")];
    let closed = false;
    openShells.forEach((shell) => {
        const selectId = String(shell.dataset.selectId || "");
        if (exceptId && selectId === exceptId) return;
        const trigger = shell.querySelector(".resource-select-trigger");
        const menu = shell.querySelector(".resource-select-menu");
        shell.classList.remove("is-open");
        if (menu) menu.hidden = true;
        if (trigger) trigger.setAttribute("aria-expanded", "false");
        if (restoreFocus && trigger instanceof HTMLElement) trigger.focus();
        closed = true;
    });
    if (closed && (!exceptId || S.openResourceSelectId !== exceptId)) S.openResourceSelectId = exceptId || "";
    return closed;
}

function syncResourceSelectUI(select) {
    if (!(select instanceof HTMLSelectElement)) return;
    const shell = select.closest(".resource-select-shell");
    if (!shell) return;
    rebuildResourceSelectOptions(select);
    const trigger = shell.querySelector(".resource-select-trigger");
    const valueEl = shell.querySelector(".resource-select-value");
    const menu = shell.querySelector(".resource-select-menu");
    const optionButtons = [...shell.querySelectorAll(".resource-select-option")];
    const selectedOption = select.selectedOptions?.[0] || [...select.options].find((option) => option.value === select.value) || select.options[0];
    const selectedValue = String(selectedOption?.value ?? "");
    const isDisabled = !!select.disabled;

    if (valueEl) valueEl.textContent = String(selectedOption?.textContent || "").trim();
    shell.classList.toggle("is-disabled", isDisabled);
    if (trigger) {
        trigger.disabled = isDisabled;
        trigger.setAttribute("aria-disabled", isDisabled ? "true" : "false");
        trigger.dataset.value = selectedValue;
        trigger.setAttribute("aria-label", `${resourceSelectLabel(select)}: ${String(selectedOption?.textContent || "").trim()}`);
        if (isDisabled) trigger.setAttribute("aria-expanded", "false");
    }
    if (menu && isDisabled) {
        menu.hidden = true;
        shell.classList.remove("is-open");
        if (S.openResourceSelectId === select.id) S.openResourceSelectId = "";
    }
    optionButtons.forEach((button) => {
        const isSelected = String(button.dataset.value || "") === selectedValue;
        button.disabled = isDisabled;
        button.setAttribute("aria-disabled", isDisabled ? "true" : "false");
        button.classList.toggle("is-selected", isSelected);
        button.setAttribute("aria-selected", isSelected ? "true" : "false");
        button.tabIndex = !isDisabled && isSelected ? 0 : -1;
    });
}

function focusResourceSelectOption(shell, direction = "selected") {
    if (!(shell instanceof HTMLElement)) return;
    const options = [...shell.querySelectorAll(".resource-select-option")];
    if (!options.length) return;
    const active = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const currentIndex = active ? options.indexOf(active) : -1;
    let nextIndex = options.findIndex((option) => option.classList.contains("is-selected"));
    if (direction === "first") nextIndex = 0;
    else if (direction === "last") nextIndex = options.length - 1;
    else if (direction === "next") nextIndex = currentIndex >= 0 ? Math.min(options.length - 1, currentIndex + 1) : Math.max(0, nextIndex);
    else if (direction === "prev") nextIndex = currentIndex >= 0 ? Math.max(0, currentIndex - 1) : Math.max(0, nextIndex);
    if (nextIndex < 0) nextIndex = 0;
    options[nextIndex]?.focus();
}

function setResourceSelectValue(select, value, { close = true } = {}) {
    if (!(select instanceof HTMLSelectElement)) return;
    if (select.disabled) return;
    const nextValue = String(value ?? "");
    if (select.value !== nextValue) {
        select.value = nextValue;
        syncResourceSelectUI(select);
        select.dispatchEvent(new Event("change", { bubbles: true }));
    } else {
        syncResourceSelectUI(select);
    }
    if (close) closeResourceSelects({ restoreFocus: true });
}

function openResourceSelect(select, { focus = "selected" } = {}) {
    if (!(select instanceof HTMLSelectElement)) return;
    if (select.disabled) return;
    const shell = select.closest(".resource-select-shell");
    if (!shell) return;
    const trigger = shell.querySelector(".resource-select-trigger");
    const menu = shell.querySelector(".resource-select-menu");
    closeResourceSelects({ exceptId: select.id });
    shell.classList.add("is-open");
    if (menu) menu.hidden = false;
    if (trigger) trigger.setAttribute("aria-expanded", "true");
    S.openResourceSelectId = select.id;
    focusResourceSelectOption(shell, focus);
}

function buildResourceSelect(select) {
    if (!(select instanceof HTMLSelectElement) || select.dataset.customized === "true") return;
    const parent = select.parentElement;
    if (!parent) return;

    const shell = document.createElement("div");
    shell.className = "resource-select-shell";
    shell.dataset.selectId = select.id || `resource-select-${Math.random().toString(36).slice(2, 8)}`;

    const trigger = document.createElement("button");
    trigger.type = "button";
    trigger.className = "resource-select-trigger";
    trigger.setAttribute("aria-haspopup", "listbox");
    trigger.setAttribute("aria-expanded", "false");
    trigger.setAttribute("aria-controls", `${shell.dataset.selectId}-menu`);

    const valueEl = document.createElement("span");
    valueEl.className = "resource-select-value";
    const iconEl = document.createElement("span");
    iconEl.className = "resource-select-icon";
    iconEl.setAttribute("aria-hidden", "true");
    iconEl.innerHTML = `
        <svg viewBox="0 0 18 18" focusable="false" aria-hidden="true">
            <path d="M4.5 6.75L9 11.25L13.5 6.75" />
        </svg>
    `;
    trigger.append(valueEl, iconEl);

    const menu = document.createElement("div");
    menu.className = "resource-select-menu";
    menu.id = `${shell.dataset.selectId}-menu`;
    menu.setAttribute("role", "listbox");
    menu.hidden = true;
    menu.setAttribute("aria-label", resourceSelectLabel(select));

    [...select.options].forEach((option) => {
        const optionButton = document.createElement("button");
        optionButton.type = "button";
        optionButton.className = "resource-select-option";
        optionButton.dataset.value = option.value;
        optionButton.setAttribute("role", "option");
        optionButton.tabIndex = -1;

        const label = document.createElement("span");
        label.className = "resource-select-option-label";
        label.textContent = String(option.textContent || "").trim();

        const check = document.createElement("span");
        check.className = "resource-select-option-check";
        check.innerHTML = `<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="width:14px;height:14px"><polyline points="13 5 7 11 4 8"></polyline></svg>`;
        check.setAttribute("aria-hidden", "true");

        optionButton.append(label, check);
        optionButton.addEventListener("click", () => setResourceSelectValue(select, option.value));
        optionButton.addEventListener("keydown", (e) => {
            if (e.key === "ArrowDown") {
                e.preventDefault();
                focusResourceSelectOption(shell, "next");
            }
            if (e.key === "ArrowUp") {
                e.preventDefault();
                focusResourceSelectOption(shell, "prev");
            }
            if (e.key === "Home") {
                e.preventDefault();
                focusResourceSelectOption(shell, "first");
            }
            if (e.key === "End") {
                e.preventDefault();
                focusResourceSelectOption(shell, "last");
            }
            if (e.key === "Enter" || e.key === " ") {
                e.preventDefault();
                setResourceSelectValue(select, option.value);
            }
            if (e.key === "Escape") {
                e.preventDefault();
                closeResourceSelects({ restoreFocus: true });
            }
            if (e.key === "Tab") closeResourceSelects();
        });
        menu.appendChild(optionButton);
    });

    trigger.addEventListener("click", () => {
        if (select.disabled) return;
        const isOpen = shell.classList.contains("is-open");
        if (isOpen) closeResourceSelects({ restoreFocus: true });
        else openResourceSelect(select);
    });
    trigger.addEventListener("keydown", (e) => {
        if (select.disabled) return;
        if (e.key === "ArrowDown") {
            e.preventDefault();
            openResourceSelect(select, { focus: "first" });
        }
        if (e.key === "ArrowUp") {
            e.preventDefault();
            openResourceSelect(select, { focus: "last" });
        }
        if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            const isOpen = shell.classList.contains("is-open");
            if (isOpen) closeResourceSelects({ restoreFocus: true });
            else openResourceSelect(select);
        }
        if (e.key === "Escape") {
            e.preventDefault();
            closeResourceSelects({ restoreFocus: true });
        }
    });

    select.classList.add("resource-select-native");
    select.dataset.customized = "true";
    select.tabIndex = -1;
    select.setAttribute("aria-hidden", "true");
    select.addEventListener("change", () => syncResourceSelectUI(select));

    parent.insertBefore(shell, select);
    shell.append(select, trigger, menu);
    syncResourceSelectUI(select);
}

function enhanceResourceSelects() {
    document.querySelectorAll("select.resource-select").forEach((select) => buildResourceSelect(select));
}

function closeConfirm({ restoreFocus = true } = {}) {
    const returnFocus = S.confirmState?.returnFocus;
    S.confirmState = null;
    if (U.confirmOptions) U.confirmOptions.hidden = true;
    if (U.confirmCheckbox) {
        U.confirmCheckbox.checked = false;
        U.confirmCheckbox.disabled = false;
    }
    if (U.confirmCheckboxHint) U.confirmCheckboxHint.textContent = "";
    U.confirmBackdrop.hidden = true;
    U.confirmBackdrop.classList.remove("is-open");
    if (restoreFocus) returnFocus?.focus?.();
}

async function acceptConfirm() {
    if (!S.confirmState?.onConfirm) return;
    U.confirmAccept.disabled = true;
    U.confirmCancel.disabled = true;
    if (U.confirmCheckbox) U.confirmCheckbox.disabled = true;
    try {
        await S.confirmState.onConfirm({ checked: !!U.confirmCheckbox?.checked });
        closeConfirm();
    } finally {
        U.confirmAccept.disabled = false;
        U.confirmCancel.disabled = false;
        if (U.confirmCheckbox) U.confirmCheckbox.disabled = false;
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

function activeRoleIterations() {
    return S.modelCatalog.roleEditing ? S.modelCatalog.roleIterationDrafts : S.modelCatalog.roleIterations;
}

function modelScopeChain(scope, source = "active") {
    const roles = source === "draft"
        ? S.modelCatalog.roleDrafts
        : source === "committed"
            ? S.modelCatalog.roles
            : activeModelRoles();
    return Array.isArray(roles?.[scope]) ? [...roles[scope]] : [];
}

function modelScopeIterations(scope, source = "active") {
    const iterations = source === "draft"
        ? S.modelCatalog.roleIterationDrafts
        : source === "committed"
            ? S.modelCatalog.roleIterations
            : activeRoleIterations();
    const defaults = DEFAULT_ROLE_ITERATIONS();
    const value = Number(iterations?.[scope]);
    return Number.isInteger(value) && value >= 2 ? value : defaults[scope];
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

function normalizeRoleIterations(iterations = DEFAULT_ROLE_ITERATIONS()) {
    return cloneRoleIterations(iterations);
}

function modelRolesEqual(left, right) {
    return MODEL_SCOPES.every(({ key }) => {
        const leftChain = normalizeModelRoleChain(left?.[key] || []);
        const rightChain = normalizeModelRoleChain(right?.[key] || []);
        if (leftChain.length !== rightChain.length) return false;
        return leftChain.every((item, index) => modelRefEquivalent(item, rightChain[index]));
    });
}

function modelRoleIterationsEqual(left, right) {
    const leftNormalized = normalizeRoleIterations(left);
    const rightNormalized = normalizeRoleIterations(right);
    return MODEL_SCOPES.every(({ key }) => leftNormalized[key] === rightNormalized[key]);
}

function syncModelRoleDraftState() {
    const rolesChanged = !modelRolesEqual(S.modelCatalog.roleDrafts, S.modelCatalog.roles);
    const iterationsChanged = !modelRoleIterationsEqual(S.modelCatalog.roleIterationDrafts, S.modelCatalog.roleIterations);
    S.modelCatalog.rolesDirty = !!S.modelCatalog.roleEditing && (rolesChanged || iterationsChanged);
}

function modelCatalogHeadersKey(headers) {
    if (!headers || typeof headers !== "object" || Array.isArray(headers)) return "";
    return Object.entries(headers)
        .map(([key, value]) => [String(key), String(value)])
        .sort(([left], [right]) => left.localeCompare(right))
        .map(([key, value]) => `${key}:${value}`)
        .join("\n");
}

function modelCatalogSignature(item) {
    if (!item || typeof item !== "object") return "";
    return [
        String(item.provider_model || "").trim(),
        String(item.api_key || "").trim(),
        String(item.api_base || "").trim(),
        modelCatalogHeadersKey(item.extra_headers),
    ].join("\n");
}

function remapModelRef(ref, aliasMap = {}) {
    const raw = String(ref || "").trim();
    if (!raw) return "";
    return String(aliasMap[raw] || raw).trim();
}

function remapModelRefs(refs, aliasMap = {}) {
    const normalized = [];
    (refs || []).forEach((ref) => {
        const target = remapModelRef(ref, aliasMap);
        if (!target || normalized.some((item) => item === target)) return;
        normalized.push(target);
    });
    return normalized;
}

function normalizeCatalogEntries(items) {
    const catalog = [];
    const aliasMap = {};
    const signatureToKey = new Map();
    (items || []).forEach((item) => {
        if (!item || typeof item !== "object") return;
        const key = String(item.key || "").trim();
        const providerModel = String(item.provider_model || "").trim();
        const signature = modelCatalogSignature(item) || key || providerModel;
        if (!signature) return;
        const canonicalKey = signatureToKey.get(signature);
        if (canonicalKey) {
            if (key && key !== canonicalKey) aliasMap[key] = canonicalKey;
            return;
        }
        const nextKey = key || providerModel;
        if (!nextKey) return;
        signatureToKey.set(signature, nextKey);
        catalog.push({ ...item, key: nextKey });
    });
    return { catalog, aliasMap };
}

function applyModelCatalog(data, { preserveRoleDrafts = false } = {}) {
    const payload = data && typeof data === "object" ? data : {};
    const { catalog, aliasMap } = normalizeCatalogEntries(Array.isArray(payload.catalog) ? payload.catalog : []);
    const rolesPayload = payload.roles && typeof payload.roles === "object" ? payload.roles : {};
    const roleIterationsPayload = payload.roleIterations && typeof payload.roleIterations === "object"
        ? payload.roleIterations
        : payload.role_iterations && typeof payload.role_iterations === "object"
            ? payload.role_iterations
            : {};
    const nextRoles = EMPTY_MODEL_ROLES();
    MODEL_SCOPES.forEach(({ key }) => {
        nextRoles[key] = remapModelRefs(
            Array.isArray(rolesPayload[key])
                ? rolesPayload[key].map((item) => String(item || "").trim()).filter(Boolean)
                : [],
            aliasMap,
        );
    });
    S.modelCatalog.items = Array.isArray(payload.items)
        ? remapModelRefs(payload.items.map((item) => String(item || "").trim()).filter(Boolean), aliasMap)
        : [];
    S.modelCatalog.catalog = catalog;
    S.modelCatalog.roles = normalizeAllModelRoles(nextRoles);
    S.modelCatalog.roleIterations = normalizeRoleIterations(roleIterationsPayload);
    if (preserveRoleDrafts && S.modelCatalog.roleEditing) {
        S.modelCatalog.roleDrafts = normalizeAllModelRoles(S.modelCatalog.roleDrafts);
        S.modelCatalog.roleIterationDrafts = normalizeRoleIterations(S.modelCatalog.roleIterationDrafts);
        syncModelRoleDraftState();
    } else {
        S.modelCatalog.roleDrafts = cloneModelRoles(S.modelCatalog.roles);
        S.modelCatalog.roleIterationDrafts = cloneRoleIterations(S.modelCatalog.roleIterations);
        S.modelCatalog.roleEditing = false;
        S.modelCatalog.rolesDirty = false;
    }
    S.modelCatalog.defaults = { ...DEFAULT_MODEL_DEFAULTS(), ...(payload.defaults || {}) };
    if (S.modelCatalog.mode !== "create") {
        const selectedKey = remapModelRef(S.modelCatalog.selectedModelKey, aliasMap);
        S.modelCatalog.selectedModelKey = selectedKey;
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
        const maxIterations = modelScopeIterations(scope.key);
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
                    <div class="model-chain-card-meta">
                        <span class="policy-chip neutral">${chain.length} 个候选</span>
                        <label class="model-role-iterations-field">
                            <span class="model-role-iterations-label">最大轮数</span>
                            <input
                                class="model-role-iterations-input"
                                type="number"
                                min="2"
                                step="1"
                                inputmode="numeric"
                                value="${esc(String(maxIterations))}"
                                ${editing ? "" : "disabled"}
                                data-model-role-iterations="${scope.key}"
                            >
                        </label>
                    </div>
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
                        <div class="model-form-status-area" style="margin-top: var(--space-4);">
                            ${enabled 
                                ? `<button type="button" class="toolbar-btn danger" data-model-control="disable" data-key="${esc(current?.key || "")}">禁用模型</button>` 
                                : `<button type="button" class="toolbar-btn success" data-model-control="enable" data-key="${esc(current?.key || "")}">启用模型</button>`
                            }
                            ${!isCreate ? `<button type="button" class="toolbar-btn ghost" data-model-control="delete" data-key="${esc(current?.key || "")}">删除模型</button>` : ""}
                            <input type="checkbox" name="enabled" ${enabled ? "checked" : ""} style="display:none">
                        </div>
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
                            <label class="resource-field">
                                <span class="resource-field-label">重试次数</span>
                                <input class="resource-search" type="number" min="0" step="1" name="retryCount" value="${esc(String(current?.retry_count ?? 0))}" placeholder="0">
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

function modelDragZoneContainsPoint(zone, clientX, clientY) {
    if (!zone || !Number.isFinite(clientX) || !Number.isFinite(clientY)) return false;
    const rect = zone.getBoundingClientRect();
    return clientX >= rect.left && clientX <= rect.right && clientY >= rect.top && clientY <= rect.bottom;
}

function didModelDragLeaveZone(zone, event) {
    if (!zone) return true;
    const related = event?.relatedTarget;
    if (related instanceof Node && zone.contains(related)) return false;
    return !modelDragZoneContainsPoint(zone, Number(event?.clientX), Number(event?.clientY));
}

function resolveModelChainDropList(target) {
    if (!(target instanceof Element)) return null;
    const directList = target.closest("[data-model-chain-list]");
    if (directList) return directList;
    const card = target.closest(".model-chain-card");
    return card?.querySelector("[data-model-chain-list]") || null;
}

function resolveModelChainDropTarget(list, clientY, dragState = null) {
    if (!list) return null;
    const scope = String(list.dataset.modelChainList || "");
    const items = [...list.children].filter((child) => {
        if (!child.matches?.("[data-model-chain-ref]")) return false;
        if (
            dragState?.source === "chain"
            && scope === String(dragState.scope || "")
            && String(child.dataset.modelChainRef || "") === String(dragState.ref || "")
        ) {
            return false;
        }
        return true;
    });
    for (const item of items) {
        const rect = item.getBoundingClientRect();
        if (clientY < rect.top + (rect.height / 2)) return item;
    }
    return items[items.length - 1] || null;
}

function resolveModelChainDropIndex(list, dragState, clientY) {
    if (!list) return 0;
    const items = [...list.children].filter((child) => {
        if (!child.matches?.("[data-model-chain-ref]")) return false;
        return String(child.dataset.modelChainRef || "") !== String(dragState?.ref || "");
    });
    const targetItem = resolveModelChainDropTarget(list, clientY, dragState);
    if (!targetItem) return items.length;
    const targetIndex = items.indexOf(targetItem);
    if (targetIndex < 0) return items.length;
    const rect = targetItem.getBoundingClientRect();
    const insertBefore = clientY < rect.top + (rect.height / 2);
    return targetIndex + (insertBefore ? 0 : 1);
}

function ensureModelDropPlaceholder(list, targetItem, clientY) {
    if (!list) return null;
    const placeholder = document.createElement('div');
    placeholder.className = 'model-chain-drop-placeholder';
    placeholder.dataset.modelDropPlaceholder = '1';
    list.classList.add('is-drop-zone');
    list.closest('.model-chain-card')?.classList.add('is-drop-zone');
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

function updateRoleIterationDraft(scope, value, { render = true } = {}) {
    if (!S.modelCatalog.roleEditing) return false;
    const normalizedScope = String(scope || "").trim();
    const cleanValue = Number.parseInt(String(value || "").trim(), 10);
    if (!normalizedScope || !Number.isInteger(cleanValue) || cleanValue < 2) return false;
    S.modelCatalog.roleIterationDrafts[normalizedScope] = cleanValue;
    syncModelRoleDraftState();
    if (render) renderModelCatalog();
    return true;
}

function syncRoleIterationDraftsFromInputs({ requireValid = false } = {}) {
    if (!U.modelRoleEditors) return false;
    let changed = false;
    const fields = [...U.modelRoleEditors.querySelectorAll("[data-model-role-iterations]")];
    fields.forEach((field) => {
        if (!(field instanceof HTMLInputElement)) return;
        const scope = String(field.dataset.modelRoleIterations || "").trim();
        if (!scope) return;
        const rawValue = String(field.value || "").trim();
        const cleanValue = Number.parseInt(rawValue, 10);
        const scopeLabel = MODEL_SCOPES.find((item) => item.key === scope)?.label || scope;
        const invalid = !rawValue || !Number.isInteger(cleanValue) || cleanValue < 2;
        if (invalid) {
            field.classList.add("is-invalid");
            field.setCustomValidity("最大轮数必须是不小于 2 的整数");
            if (requireValid) {
                field.reportValidity();
                throw new Error(`${scopeLabel} 最大轮数必须是不小于 2 的整数`);
            }
            return;
        }
        field.classList.remove("is-invalid");
        field.setCustomValidity("");
        if (modelScopeIterations(scope, "draft") !== cleanValue) {
            S.modelCatalog.roleIterationDrafts[scope] = cleanValue;
            changed = true;
        }
    });
    if (changed) syncModelRoleDraftState();
    return changed;
}

function startModelRoleEditing() {
    S.modelCatalog.roleEditing = true;
    S.modelCatalog.roleDrafts = cloneModelRoles(S.modelCatalog.roles);
    S.modelCatalog.roleIterationDrafts = cloneRoleIterations(S.modelCatalog.roleIterations);
    syncModelRoleDraftState();
    renderModelCatalog();
}

function cancelModelRoleEditing() {
    S.modelCatalog.roleEditing = false;
    S.modelCatalog.roleDrafts = cloneModelRoles(S.modelCatalog.roles);
    S.modelCatalog.roleIterationDrafts = cloneRoleIterations(S.modelCatalog.roleIterations);
    S.modelCatalog.rolesDirty = false;
    finishModelDrag();
    renderModelCatalog();
    hint("已取消模型链修改。", false);
}

async function persistModelRoleChains(scopes = MODEL_SCOPES.map((item) => item.key), successText = "模型链已保存。", { useDrafts = false } = {}) {
    const targets = [...new Set(scopes.map((item) => String(item || "").trim()).filter(Boolean))];
    if (!targets.length) return;
    const roleSource = useDrafts ? S.modelCatalog.roleDrafts : S.modelCatalog.roles;
    const iterationSource = useDrafts ? S.modelCatalog.roleIterationDrafts : S.modelCatalog.roleIterations;
    S.modelCatalog.saving = true;
    renderModelCatalog();
    try {
        let payload = null;
        for (const scope of targets) {
            payload = await ApiClient.updateModelRoleChain(scope, {
                modelKeys: normalizeModelRoleChain(roleSource[scope] || []),
                maxIterations: iterationSource[scope],
            });
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
    try {
        syncRoleIterationDraftsFromInputs({ requireValid: true });
    } catch (e) {
        S.modelCatalog.error = e.message || "save failed";
        hint(`妯″瀷閰嶇疆閿欒锛?{S.modelCatalog.error}`, true);
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
    const retryCountText = String(formData.get("retryCount") || "").trim();
    const description = String(formData.get("description") || "").trim();
    const enabled = formData.get("enabled") === "on";
    const selectedScopes = new Set(MODEL_SCOPES.filter((scope) => formData.get(`scope_${scope.key}`) === "on").map((scope) => scope.key));

    if (!key) throw new Error("模型 Key 不能为空");
    if (!providerModel) throw new Error("Provider / Model 不能为空");
    if (!apiKey) throw new Error("API Key 不能为空");
    if (isCreate && !apiBase) throw new Error("Base URL 不能为空");

    const extraHeaders = parseModelHeaders(formData.get("extraHeaders"));
    const retryOn = retryOnRaw ? parseModelRetryOn(retryOnRaw) : null;
    const retryCount = retryCountText ? Number.parseInt(retryCountText, 10) : 0;
    const maxTokens = maxTokensText ? Number(maxTokensText) : null;
    const temperature = temperatureText ? Number(temperatureText) : null;

    if (maxTokensText && (!Number.isInteger(maxTokens) || maxTokens <= 0)) {
        throw new Error("Max Tokens 必须是正整数");
    }
    if (temperatureText && (!Number.isFinite(temperature) || temperature < 0 || temperature > 2)) {
        throw new Error("Temperature 必须在 0 到 2 之间");
    }
    if (retryCountText && (!Number.isInteger(retryCount) || retryCount < 0)) {
        throw new Error("重试次数必须是不小于 0 的整数");
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
        payload.retryCount = retryCount;
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
    if (retryCount !== Number.parseInt(String(current?.retry_count ?? 0), 10)) patch.retryCount = retryCount;
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
        showToast({ title: draft.isCreate ? "添加成功" : "修改成功", text: draft.isCreate ? "模型已添加成功" : "模型配置已保存", kind: "success" });
        clearModelSelection();

    } catch (e) {
        S.modelCatalog.error = e.message || "save failed";
        hint(`模型配置错误：${S.modelCatalog.error}`, true);
        showToast({ title: "修改失败", text: `模型配置错误：${S.modelCatalog.error}`, kind: "error" });
        clearModelSelection();
    }
}

async function deleteModelDetail(modelKey) {
    const targetKey = String(modelKey || "").trim();
    if (!targetKey) return;
    const confirmed = window.confirm(`删除模型 ${targetKey}？此操作会同时从 catalog 和所有角色链移除它。`);
    if (!confirmed) return;
    try {
        const payload = await ApiClient.deleteManagedModel(targetKey);
        applyModelCatalog(payload, { preserveRoleDrafts: false });
        hint("模型已删除。");
        showToast({ title: "删除成功", text: `模型 ${targetKey} 已删除`, kind: "success" });
        clearModelSelection();
    } catch (e) {
        const message = e.message || "delete failed";
        S.modelCatalog.error = message;
        hint(`模型删除失败：${message}`, true);
        showToast({ title: "删除失败", text: `模型删除失败：${message}`, kind: "error" });
    }
}

function resetCeoComposerState() {
    S.ceoUploads = [];
    S.ceoUploadBusy = false;
    if (U.ceoInput) U.ceoInput.value = "";
    if (U.ceoFileInput) U.ceoFileInput.value = "";
    renderPendingCeoUploads();
    syncCeoInputHeight();
}

function resetCeoSessionState() {
    resetCeoFeed();
    S.ceoPendingTurns = [];
    S.ceoTurnActive = false;
    S.ceoPauseBusy = false;
    syncCeoPrimaryButton();
}

function stopCeoSessionPolling() {
    if (S.ceoSessionPollIntervalId) window.clearInterval(S.ceoSessionPollIntervalId);
    S.ceoSessionPollIntervalId = null;
    S.ceoSessionPollInFlight = false;
}

async function pollCeoSessions() {
    if (document.hidden || S.ceoSessionBusy || S.ceoSessionPollInFlight) return null;
    S.ceoSessionPollInFlight = true;
    try {
        return await refreshCeoSessions({ background: true });
    } catch (error) {
        console.debug("Background CEO session refresh failed.", error);
        return null;
    } finally {
        S.ceoSessionPollInFlight = false;
    }
}

function startCeoSessionPolling() {
    stopCeoSessionPolling();
    S.ceoSessionPollIntervalId = window.setInterval(() => {
        void pollCeoSessions();
    }, CEO_SESSION_POLL_MS);
}

function closeCeoWs() {
    S.ceoWsToken += 1;
    const socket = S.ceoWs;
    S.ceoWs = null;
    if (!socket) return;
    socket.onclose = null;
    socket.close();
}

function renderCeoSessions() {
    if (!U.ceoSessionList || !U.ceoSessionCurrent) return;
    const sessions = Array.isArray(S.ceoSessions) ? S.ceoSessions : [];
    const currentId = activeSessionId();
    const current = sessions.find((item) => String(item?.session_id || "") === currentId) || null;
    U.ceoSessionCurrent.innerHTML = current
        ? `
            <div class="ceo-session-current-title">${esc(String(current.title || current.session_id || "Session"))}</div>
            <div class="ceo-session-current-meta">当前会话 · ${esc(formatSessionTime(current.updated_at))}</div>
        `
        : `
            <div class="ceo-session-current-title">正在准备会话</div>
            <div class="ceo-session-current-meta">会话加载后会自动连接。</div>
        `;

    if (!sessions.length) {
        U.ceoSessionList.innerHTML = '<div class="empty-state ceo-session-empty">No sessions yet.</div>';
        syncCeoSessionActions();
        return;
    }

    U.ceoSessionList.innerHTML = sessions.map((item) => {
        const sessionId = String(item?.session_id || "");
        const isActive = sessionId === currentId;
        const preview = String(item?.preview_text || "").trim() || "No messages yet.";
        const unreadCount = isActive ? 0 : sessionUnreadCount(sessionId);
        const unreadText = unreadCount > 99 ? "99+" : String(unreadCount);
        return `
            <div class="ceo-session-card${isActive ? " is-active" : ""}${unreadCount > 0 ? " has-unread" : ""}" role="listitem">
                <button
                    type="button"
                    class="ceo-session-main ceo-session-select"
                    data-session-activate="${esc(sessionId)}"
                    aria-pressed="${isActive ? "true" : "false"}"
                >
                    <div class="ceo-session-head">
                        <div class="ceo-session-title">${esc(String(item.title || sessionId || "Session"))}</div>
                        ${unreadCount > 0 ? `<span class="ceo-session-unread" aria-label="${esc(`${unreadCount} unread message${unreadCount > 1 ? "s" : ""}`)}">${esc(unreadText)}</span>` : ""}
                    </div>
                    <div class="ceo-session-preview">${esc(preview)}</div>
                    <div class="ceo-session-meta">${esc(formatSessionTime(item.updated_at))}</div>
                </button>
                <div class="ceo-session-actions" aria-label="Session actions">
                    <button type="button" class="ceo-session-action" data-session-rename="${esc(sessionId)}" aria-label="Rename session">
                        <i data-lucide="pencil"></i>
                    </button>
                    <button type="button" class="ceo-session-action danger" data-session-delete="${esc(sessionId)}" aria-label="Delete session">
                        <i data-lucide="trash-2"></i>
                    </button>
                </div>
            </div>
        `;
    }).join("");
    syncCeoSessionActions();
    icons();
}

function applyCeoSessionsPayload(payload = {}, { preferLocalActive = false } = {}) {
    const sessions = Array.isArray(payload?.items) ? payload.items : [];
    const previousActiveId = activeSessionId();
    const localActiveId = preferLocalActive ? previousActiveId : "";
    const localActiveExists = !!localActiveId && sessions.some((item) => String(item?.session_id || "").trim() === localActiveId);
    const nextActiveId =
        (localActiveExists ? localActiveId : "")
        || String(payload?.active_session_id || "").trim()
        || String(sessions.find((item) => item?.is_active)?.session_id || "").trim()
        || activeSessionId();
    syncCeoSessionUnreadState(sessions, nextActiveId);
    S.ceoSessions = sessions;
    S.activeSessionId = nextActiveId;
    if (nextActiveId) ApiClient.setActiveSessionId(nextActiveId);
    renderCeoSessions();
    if (S.view === "tasks" && previousActiveId !== nextActiveId) renderTasks();
    return nextActiveId;
}

async function refreshCeoSessions({ reconnect = false, background = false } = {}) {
    if (!background) {
        S.ceoSessionBusy = true;
        renderCeoSessions();
        syncCeoPrimaryButton();
    }
    try {
        const payload = await ApiClient.listCeoSessions();
        const nextActiveId = applyCeoSessionsPayload(payload, { preferLocalActive: background });
        if (reconnect && nextActiveId) initCeoWs();
        return payload;
    } finally {
        if (!background) {
            S.ceoSessionBusy = false;
            renderCeoSessions();
            syncCeoPrimaryButton();
        }
    }
}

async function activateCeoSession(sessionId) {
    const targetId = String(sessionId || "").trim();
    if (!targetId || targetId === activeSessionId()) return;
    if (!canActivateCeoSessions()) {
        showToast({ title: "会话暂不可切换", text: "请先等待当前回合完成或暂停后再切换。", kind: "warn" });
        return;
    }
    const targetSession = S.ceoSessions.find((item) => String(item?.session_id || "") === targetId) || null;
    if (targetSession) markCeoSessionRead(targetId, { messageCount: sessionMessageCount(targetSession) });
    S.ceoSessionBusy = true;
    renderCeoSessions();
    syncCeoPrimaryButton();
    try {
        const payload = await ApiClient.activateCeoSession(targetId);
        const nextActiveId = applyCeoSessionsPayload(payload);
        closeCeoWs();
        resetCeoComposerState();
        resetCeoSessionState();
        if (nextActiveId) initCeoWs();
    } catch (e) {
        showToast({ title: "切换失败", text: e.message || "Unknown error", kind: "error" });
    } finally {
        S.ceoSessionBusy = false;
        renderCeoSessions();
        syncCeoPrimaryButton();
    }
}

async function createNewCeoSession() {
    if (!canMutateCeoSessions()) {
        showToast({ title: "当前不可新建", text: "请先等待当前回合完成或暂停后再新建会话。", kind: "warn" });
        return;
    }
    S.ceoSessionBusy = true;
    renderCeoSessions();
    syncCeoPrimaryButton();
    try {
        const payload = await ApiClient.createCeoSession({});
        const nextActiveId = applyCeoSessionsPayload(payload);
        closeCeoWs();
        resetCeoComposerState();
        resetCeoSessionState();
        if (nextActiveId) initCeoWs();
    } catch (e) {
        showToast({ title: "新建失败", text: e.message || "Unknown error", kind: "error" });
    } finally {
        S.ceoSessionBusy = false;
        renderCeoSessions();
        syncCeoPrimaryButton();
    }
}

async function renameCeoSession(sessionId) {
    const targetId = String(sessionId || "").trim();
    const current = (S.ceoSessions || []).find((item) => String(item?.session_id || "") === targetId);
    if (!targetId || !current || !U.renameSessionBackdrop || !U.renameSessionInput) return;
    if (!canMutateCeoSessions()) {
        showToast({ title: "当前不可重命名", text: "请先等待当前回合完成或暂停后再操作。", kind: "warn" });
        return;
    }
    U.renameSessionInput.value = current.title || "";
    U.renameSessionBackdrop.hidden = false;
    U.renameSessionBackdrop.classList.add("is-open");
    U.renameSessionInput.focus();
    S.renameContext = { sessionId: targetId };
}

async function handleRenameAccept() {
    const sessionId = S.renameContext?.sessionId;
    const nextTitle = String(U.renameSessionInput?.value || "").trim();
    if (!sessionId || !nextTitle) {
        handleRenameCancel();
        return;
    }
    handleRenameCancel();
    S.ceoSessionBusy = true;
    renderCeoSessions();
    syncCeoPrimaryButton();
    showToast({ title: "正在重命名", text: "请稍候...", kind: "info", persistent: true });
    try {
        const payload = await ApiClient.renameCeoSession(sessionId, { title: nextTitle });
        applyCeoSessionsPayload(payload);
        showToast({ title: "成功", text: "会话已重命名", kind: "success" });
    } catch (e) {
        showToast({ title: "重命名失败", text: e.message || "Unknown error", kind: "error" });
    } finally {
        S.ceoSessionBusy = false;
        renderCeoSessions();
        syncCeoPrimaryButton();
    }
}

function handleRenameCancel() {
    if (U.renameSessionBackdrop) {
        U.renameSessionBackdrop.hidden = true;
        U.renameSessionBackdrop.classList.remove("is-open");
    }
    S.renameContext = null;
}

function formatSessionDeleteHint(payload = {}) {
    const related = payload?.related_tasks && typeof payload.related_tasks === "object" ? payload.related_tasks : {};
    const total = normalizeInt(related.total, 0);
    const terminal = normalizeInt(related.terminal, 0);
    if (total <= 0) return "当前会话没有关联任务记录。";
    return `共 ${total} 条任务记录，其中 ${terminal} 条已完成，可一并清理。`;
}

function formatSessionDeleteBlockedText(payload = {}) {
    const message = String(payload?.message || "").trim();
    const tasks = Array.isArray(payload?.usage?.tasks) ? payload.usage.tasks : [];
    if (!tasks.length) return message || "会话仍有未完成任务，无法删除。";
    const names = tasks
        .slice(0, 3)
        .map((item) => String(item?.title || item?.task_id || "").trim())
        .filter(Boolean);
    const suffix = tasks.length > 3 ? ` 等 ${tasks.length} 个任务` : "";
    if (!names.length) return message || "会话仍有未完成任务，无法删除。";
    return `${message || "会话仍有未完成任务，无法删除。"} ${names.join("、")}${suffix}`;
}

async function performDeleteCeoSession(sessionId, { deleteTaskRecords = false } = {}) {
    const targetId = String(sessionId || "").trim();
    if (!targetId) return;
    const wasActive = targetId === activeSessionId();
    S.ceoSessionBusy = true;
    renderCeoSessions();
    syncCeoPrimaryButton();
    try {
        const payload = await ApiClient.deleteCeoSession(targetId, { delete_task_records: !!deleteTaskRecords });
        const nextActiveId = applyCeoSessionsPayload(payload);
        if (wasActive) {
            closeCeoWs();
            resetCeoComposerState();
            resetCeoSessionState();
            if (nextActiveId) initCeoWs();
        }
        if (S.view === "tasks") await loadTasks();
    } catch (e) {
        const blockedDelete = e?.status === 409 && e?.data && typeof e.data === "object" && e.data.code === "session_has_unfinished_tasks";
        if (blockedDelete) {
            showToast({
                title: "无法删除当前会话",
                text: formatSessionDeleteBlockedText(e.data),
                kind: "warn",
            });
            return;
        }
        showToast({ title: "删除失败", text: e.message || "Unknown error", kind: "error" });
    } finally {
        S.ceoSessionBusy = false;
        renderCeoSessions();
        syncCeoPrimaryButton();
    }
}

async function requestDeleteCeoSession(sessionId) {
    const current = (S.ceoSessions || []).find((item) => String(item?.session_id || "") === String(sessionId || "").trim());
    if (!current) return;
    if (!canMutateCeoSessions()) {
        showToast({ title: "当前不可删除", text: "请先等待当前回合完成或暂停后再操作。", kind: "warn" });
        return;
    }
    S.ceoSessionBusy = true;
    renderCeoSessions();
    syncCeoPrimaryButton();
    let deleteCheck = null;
    try {
        deleteCheck = await ApiClient.getCeoSessionDeleteCheck(current.session_id);
    } catch (e) {
        S.ceoSessionBusy = false;
        renderCeoSessions();
        syncCeoPrimaryButton();
        showToast({ title: "删除失败", text: e.message || "Unknown error", kind: "error" });
        return;
    }
    S.ceoSessionBusy = false;
    renderCeoSessions();
    syncCeoPrimaryButton();
    if (deleteCheck?.can_delete === false) {
        showToast({
            title: "无法删除当前会话",
            text: formatSessionDeleteBlockedText(deleteCheck),
            kind: "warn",
        });
        return;
    }
    openConfirm({
        title: "删除会话",
        text: `将删除会话“${current.title || current.session_id}”的聊天记录与附件。${formatSessionDeleteHint(deleteCheck)}`,
        confirmLabel: "删除",
        confirmKind: "danger",
        returnFocus: U.ceoNewSession,
        checkbox: {
            label: "同时删除此对话创建的所有任务记录",
            hint: formatSessionDeleteHint(deleteCheck),
            checked: false,
        },
        onConfirm: ({ checked } = {}) => performDeleteCeoSession(current.session_id, { deleteTaskRecords: !!checked }),
    });
}

function initCeoWs() {
    const sessionId = activeSessionId();
    if (!sessionId) return;
    if (S.ceoWs && S.ceoWs.readyState <= 1 && S.ceoWs.sessionId === sessionId) return;
    closeCeoWs();
    const token = ++S.ceoWsToken;
    const socket = new WebSocket(ApiClient.getCeoWsUrl(sessionId));
    socket.sessionId = sessionId;
    S.ceoWs = socket;
    S.ceoWs.onmessage = (ev) => {
        const payload = JSON.parse(ev.data);
        if (payload.type === "snapshot.ceo") renderCeoSnapshot(payload.data?.messages || [], payload.data?.inflight_turn || null);
        if (payload.type === "ceo.state") applyCeoState(payload.data?.state || {}, payload.data || {});
        if (payload.type === "ceo.control_ack") handleCeoControlAck(payload.data || {});
        if (payload.type === "ceo.agent.tool") appendCeoToolEvent(payload.data || {});
        if (payload.type === "ceo.error") handleCeoError(payload.data || {});
        if (payload.type === "ceo.reply.final") finalizeCeoTurn(payload.data?.text || "", payload.data || {});
        if (payload.type === "ceo.turn.discard") discardActiveCeoTurn({ source: payload.data?.source || "" });
        if (payload.type === "task.summary.changed" && S.view === "tasks") void loadTasks();
        if (payload.type === "task.artifact.applied" && payload.data?.task_id === S.currentTaskId) void loadTaskArtifacts();
    };
    S.ceoWs.onclose = () => {
        if (token !== S.ceoWsToken) return;
        S.ceoWs = null;
        S.ceoPauseBusy = false;
        syncCeoPrimaryButton();
        window.setTimeout(() => {
            if (token !== S.ceoWsToken) return;
            if (activeSessionId() !== sessionId) return;
            initCeoWs();
        }, 1000);
    };
}

function sendCeoMessage() {
    if (S.ceoTurnActive) {
        requestCeoPause();
        return;
    }
    if (S.ceoSessionBusy || !activeSessionId()) return;
    const text = String(U.ceoInput.value || "");
    const uploads = normalizeUploadList(S.ceoUploads);
    if (!text.trim() && !uploads.length) return;
    if (S.ceoUploadBusy) {
        addMsg("附件仍在上传，请稍候再发送。", "system");
        return;
    }
    if (!S.ceoWs || S.ceoWs.readyState !== WebSocket.OPEN) {
        addMsg("Connection is not ready yet. Please try again in a moment.", "system");
        initCeoWs();
        return;
    }
    try {
        S.ceoWs.send(JSON.stringify({
            type: "client.user_message",
            session_id: activeSessionId(),
            text,
            uploads: uploads.map((item) => ({
                name: item.name,
                path: item.path,
                mime_type: item.mime_type,
                kind: item.kind,
                size: item.size,
            })),
        }));
        addMsg(hasRenderableText(text) ? text : summarizeUploads(uploads), "user", { attachments: uploads });
        U.ceoInput.value = "";
        S.ceoUploads = [];
        syncCeoInputHeight();
        renderPendingCeoUploads();
        const turn = createPendingCeoTurn();
        if (turn) S.ceoPendingTurns.push(turn);
        S.ceoTurnActive = true;
        S.ceoPauseBusy = false;
        syncCeoSessionActions();
        syncCeoPrimaryButton();
    } catch (e) {
        addMsg(`Failed to send message: ${e.message || "unknown error"}`, "system");
        initCeoWs();
    }
}
const canPause = (task) => !!task && !task.is_paused && pStatus(task.status) === "in_progress";
const canResume = (task) => !!task && !!task.is_paused;
const canRetry = (task) => !!task && pStatus(task.status) === "failed";
const canDelete = (task) => !!task && (!!task.is_paused || ["success", "failed"].includes(pStatus(task.status)));
const EMPTY_TOKEN_USAGE = () => ({
    tracked: false,
    input_tokens: 0,
    output_tokens: 0,
    cache_hit_tokens: 0,
    call_count: 0,
    calls_with_usage: 0,
    calls_without_usage: 0,
    is_partial: false,
});

function taskStatusKey(task) {
    if (!task) return "unknown";
    if (task.is_paused) return "blocked";
    return pStatus(task.status) || "unknown";
}

function normalizeTokenUsage(raw) {
    const source = raw && typeof raw === "object" ? raw : {};
    const toInt = (value) => {
        const num = Number(value);
        return Number.isFinite(num) && num >= 0 ? Math.floor(num) : 0;
    };
    return {
        tracked: !!source.tracked,
        input_tokens: toInt(source.input_tokens),
        output_tokens: toInt(source.output_tokens),
        cache_hit_tokens: toInt(source.cache_hit_tokens),
        call_count: toInt(source.call_count),
        calls_with_usage: toInt(source.calls_with_usage),
        calls_without_usage: toInt(source.calls_without_usage),
        is_partial: !!source.is_partial,
    };
}

function normalizeModelTokenUsage(raw) {
    const usage = normalizeTokenUsage(raw);
    return {
        ...usage,
        model_key: String(raw?.model_key || "").trim(),
        provider_id: String(raw?.provider_id || "").trim(),
        provider_model: String(raw?.provider_model || "").trim(),
    };
}

function formatTokenCount(value) {
    const num = Number(value);
    if (!Number.isFinite(num)) return "0";
    return new Intl.NumberFormat("zh-CN").format(Math.max(0, Math.floor(num)));
}

function tokenKnownTotal(usage) {
    const data = normalizeTokenUsage(usage);
    return data.input_tokens + data.output_tokens;
}

function taskTokenUsage(task = null, progress = null) {
    return normalizeTokenUsage(task?.token_usage || progress?.token_usage || EMPTY_TOKEN_USAGE());
}

function taskTokenSummaryLine(usage) {
    const data = normalizeTokenUsage(usage);
    if (!data.tracked) return "历史任务未统计";
    if (!data.call_count) return "尚未发生模型调用";
    const parts = [
        `输入 ${formatTokenCount(data.input_tokens)}`,
        `输出 ${formatTokenCount(data.output_tokens)}`,
        `缓存命中 ${formatTokenCount(data.cache_hit_tokens)}`,
    ];
    if (data.is_partial) parts.push("部分缺失");
    return parts.join(" · ");
}

function ensureTaskTokenUi() {
    const view = U.viewTaskDetails;
    if (!view) return;
    if (!U.taskTokenButton) {
        const headerActions = view.querySelector(".project-header .header-actions");
        if (headerActions) {
            const button = document.createElement("button");
            button.id = "task-token-stats-btn";
            button.className = "toolbar-btn ghost";
            button.type = "button";
            button.textContent = "Token统计";
            button.disabled = true;
            headerActions.appendChild(button);
            U.taskTokenButton = button;
        }
    }
    if (!U.taskTokenBackdrop || !U.taskTokenDrawer) {
        const backdrop = document.createElement("div");
        backdrop.id = "task-token-backdrop";
        backdrop.className = "detail-backdrop";
        backdrop.setAttribute("aria-hidden", "true");
        const drawer = document.createElement("section");
        drawer.id = "task-token-drawer";
        drawer.className = "panel detail-drawer task-token-modal";
        drawer.setAttribute("role", "dialog");
        drawer.setAttribute("aria-modal", "true");
        drawer.setAttribute("aria-hidden", "true");
        drawer.setAttribute("aria-labelledby", "task-token-title");
        drawer.tabIndex = -1;
        drawer.innerHTML = `
            <div class="detail-modal-header">
                <div>
                    <h2 id="task-token-title">Token统计</h2>
                    <p id="task-token-summary-text" class="subtitle">任务级 token 消耗会在这里实时刷新。</p>
                </div>
                <button id="task-token-close-btn" class="toolbar-btn ghost" type="button" data-modal-close>关闭</button>
            </div>
            <div class="detail-modal-body">
                <div id="task-token-content" class="task-token-shell">
                    <div class="empty-state">请选择一个任务后查看 token 统计。</div>
                </div>
            </div>
        `;
        view.querySelector(".project-dashboard")?.appendChild(backdrop);
        view.querySelector(".project-dashboard")?.appendChild(drawer);
        U.taskTokenBackdrop = backdrop;
        U.taskTokenDrawer = drawer;
        U.taskTokenSummaryText = drawer.querySelector("#task-token-summary-text");
        U.taskTokenContent = drawer.querySelector("#task-token-content");
        U.taskTokenClose = drawer.querySelector("#task-token-close-btn");
    }
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
            const focusTarget = drawer.querySelector("[data-modal-close], button, input, textarea, select");
            focusTarget?.focus?.();
        });
    }
    if (!open && drawer?.__returnFocus?.focus) {
        drawer.__returnFocus.focus();
    }
}

function syncActionButton(button, { idleLabel, busyLabel, busy = false, disabled = false } = {}) {
    if (!button) return;
    button.textContent = busy ? (busyLabel || idleLabel || button.textContent || "") : (idleLabel || button.textContent || "");
    button.disabled = !!disabled;
}

function renderSkillActions() {
    syncActionButton(U.skillRefresh, {
        idleLabel: "刷新",
        busyLabel: "刷新中...",
        busy: S.skillBusy,
        disabled: S.skillBusy,
    });
    syncActionButton(U.skillSave, {
        idleLabel: "保存",
        busyLabel: "保存中...",
        busy: S.skillBusy,
        disabled: S.skillBusy || !S.selectedSkill || !S.skillDirty,
    });
    const deleteButton = U.skillDetail?.querySelector("#skill-delete-btn");
    if (deleteButton) {
        deleteButton.textContent = S.skillBusy ? "删除中..." : "删除";
        deleteButton.disabled = S.skillBusy || !S.selectedSkill;
    }
    const toggleButton = U.skillDetail?.querySelector(S.selectedSkill?.enabled ? "#skill-disable-btn" : "#skill-enable-btn");
    if (toggleButton) toggleButton.disabled = S.skillBusy || !S.selectedSkill;
    syncDetailSaveButton("skill");
}

function renderToolActions() {
    syncActionButton(U.toolRefresh, {
        idleLabel: "刷新",
        busyLabel: "刷新中...",
        busy: S.toolBusy,
        disabled: S.toolBusy,
    });
    syncActionButton(U.toolSave, {
        idleLabel: "保存",
        busyLabel: "保存中...",
        busy: S.toolBusy,
        disabled: S.toolBusy || !S.selectedTool || !S.toolDirty,
    });
    const deleteButton = U.toolDetail?.querySelector("#tool-delete-btn");
    if (deleteButton) {
        deleteButton.textContent = S.toolBusy ? "删除中..." : "删除";
        deleteButton.disabled = S.toolBusy || !S.selectedTool;
    }
    const toggleButton = U.toolDetail?.querySelector(S.selectedTool?.enabled ? "#tool-disable-btn" : "#tool-enable-btn");
    if (toggleButton) toggleButton.disabled = S.toolBusy || !S.selectedTool;
    syncDetailSaveButton("tool");
}

function renderCommunicationActions() {
    syncActionButton(U.communicationRefresh, {
        idleLabel: "刷新",
        busyLabel: "刷新中...",
        busy: S.communicationBusy,
        disabled: S.communicationBusy,
    });
    const saveButton = U.communicationDetail?.querySelector("#communication-save-btn");
    const hint = U.communicationDetail?.querySelector(".resource-draft-hint");
    if (saveButton) {
        saveButton.textContent = S.communicationBusy ? "保存中..." : "保存";
        saveButton.disabled = !!S.communicationBusy || !S.communicationDirty;
    }
    if (hint) {
        hint.classList.toggle("is-dirty", S.communicationDirty);
        hint.textContent = S.communicationDirty ? "配置变更已暂存，点击保存后才会写入配置文件并执行连接测试。" : "";
        hint.hidden = !S.communicationDirty;
    }
}

function clearSkillSelection() {
    S.selectedSkill = null;
    S.skillFiles = [];
    S.skillContents = {};
    S.selectedSkillFile = "";
    S.skillDirty = false;
    renderSkills();
    renderSkillDetail();
}

function clearToolSelection() {
    S.selectedTool = null;
    S.toolDirty = false;
    renderTools();
    renderToolDetail();
}

function clearCommunicationSelection() {
    S.selectedCommunication = null;
    S.communicationDirty = false;
    S.communicationDraftEnabled = false;
    S.communicationDraftText = "";
    S.communicationBaselineEnabled = false;
    S.communicationBaselineText = "";
    renderCommunications();
    renderCommunicationDetail();
}

function taskStatusLabel(task) {
    return ({ in_progress: "Running", success: "Done", failed: "Failed", blocked: "Paused", unknown: "Unknown" })[taskStatusKey(task)] || "Unknown";
}

function statusBucketMatches(task, bucketKey) {
    const status = taskStatusKey(task);
    if (bucketKey === "paused") return status === "blocked";
    if (bucketKey === "failed") return status === "failed";
    if (bucketKey === "unread") return !!task?.is_unread;
    if (bucketKey === "running") return status === "in_progress";
    return false;
}

function getSelectedTasks() {
    return S.tasks.filter((task) => S.selectedTaskIds.has(task.task_id));
}

function setTaskMenuVisibility() {
    const filterOpen = !!(S.multiSelectMode && S.taskFilterMenuOpen);
    const batchOpen = !!(S.multiSelectMode && S.taskBatchMenuOpen);
    if (U.taskFilterWrap) U.taskFilterWrap.hidden = !S.multiSelectMode;
    if (U.taskBatchWrap) U.taskBatchWrap.hidden = !S.multiSelectMode;
    if (U.taskFilterMenu) U.taskFilterMenu.hidden = !filterOpen;
    if (U.taskBatchMenu) U.taskBatchMenu.hidden = !batchOpen;
    U.taskFilterTrigger?.setAttribute("aria-expanded", filterOpen ? "true" : "false");
    U.taskBatchTrigger?.setAttribute("aria-expanded", batchOpen ? "true" : "false");
}

function setTaskMenuOpen(kind, open) {
    if (kind === "filter") {
        S.taskFilterMenuOpen = !!open;
        if (open) S.taskBatchMenuOpen = false;
    } else {
        S.taskBatchMenuOpen = !!open;
        if (open) S.taskFilterMenuOpen = false;
    }
    setTaskMenuVisibility();
}

function closeTaskMenus() {
    S.taskFilterMenuOpen = false;
    S.taskBatchMenuOpen = false;
    setTaskMenuVisibility();
}

function setMultiSelectMode(enabled) {
    S.multiSelectMode = !!enabled;
    if (!S.multiSelectMode) S.selectedTaskIds.clear();
    closeTaskMenus();
    renderTasks();
}

function toggleTaskSelection(taskId) {
    if (S.selectedTaskIds.has(taskId)) S.selectedTaskIds.delete(taskId);
    else S.selectedTaskIds.add(taskId);
    renderTasks();
}

function syncTaskSelection() {
    const ids = new Set(S.tasks.map((task) => task.task_id));
    [...S.selectedTaskIds].forEach((id) => !ids.has(id) && S.selectedTaskIds.delete(id));
    if (!S.multiSelectMode) S.selectedTaskIds.clear();
}

function updateTaskToolbar() {
    const selected = getSelectedTasks();
    if (U.taskToolbar) U.taskToolbar.hidden = !S.multiSelectMode;
    if (U.taskMultiToggle) {
        U.taskMultiToggle.setAttribute("aria-pressed", S.multiSelectMode ? "true" : "false");
        U.taskMultiToggle.classList.toggle("active", S.multiSelectMode);
    }
    const filterButtons = [...(U.taskFilterMenu?.querySelectorAll("[data-select-bucket]") || [])];
    filterButtons.forEach((button) => {
        button.disabled = S.taskBusy || !S.tasks.some((task) => statusBucketMatches(task, button.dataset.selectBucket));
    });
    const batchButtons = [...(U.taskBatchMenu?.querySelectorAll("[data-batch-action]") || [])];
    batchButtons.forEach((button) => {
        const action = button.dataset.batchAction;
        const enabled = action === "pause"
            ? selected.some((task) => canPause(task))
            : action === "resume"
                ? selected.some((task) => canResume(task))
                : action === "retry"
                    ? selected.some((task) => canRetry(task))
                    : selected.some((task) => canDelete(task));
        button.disabled = S.taskBusy || !enabled;
    });
    setTaskMenuVisibility();
}

function primaryTaskAction(task) {
    if (canPause(task)) return { action: "pause", label: "暂停", tone: "warn" };
    if (canResume(task)) return { action: "resume", label: "开始", tone: "success" };
    return null;
}

function taskActionText(action) {
    return ({ pause: "暂停", resume: "开始", delete: "删除" }[action] || "操作");
}

function taskActionSuccessTitle(action) {
    return action === "delete" ? "删除成功" : `${taskActionText(action)}成功`;
}

function taskActionFailureTitle(action) {
    return action === "delete" ? "删除失败" : `${taskActionText(action)}失败`;
}

function taskActionErrorText(action, error) {
    const message = String(error?.message || error || "").trim();
    if (action === "delete") {
        if (message.includes("task_still_stopping")) return "任务仍在停止中，请稍后再删";
        if (message.includes("task_not_deletable") || message.includes("task_not_paused")) return "仅已暂停或已完成的任务可删除";
        if (message.includes("task_not_found")) return "任务不存在或已被删除";
    }
    return message || "Unknown error";
}

function isTaskDetailVisible() {
    return !!U.viewTaskDetails?.classList.contains("active");
}

function handleDeletedTasks(taskIds = []) {
    const ids = new Set(taskIds.map((item) => String(item || "")));
    if (!ids.size) return;
    if (!ids.has(String(S.currentTaskId || ""))) return;
    S.currentTaskId = null;
    resetTaskView();
    if (isTaskDetailVisible()) switchView("tasks");
}

async function requestTaskAction(taskId, action) {
    if (action === "pause") return ApiClient.pauseTask(taskId);
    if (action === "resume") return ApiClient.resumeTask(taskId);
    if (action === "delete") return ApiClient.deleteTask(taskId);
    throw new Error(`Unsupported task action: ${action}`);
}

function taskSessionQueryValue() {
    return "all";
}

function taskSessionEmptyText() {
    return "No tasks yet.";
}

function renderTaskSessionScope() {
    return;
}

async function setTaskSessionScope(scope) {
    return;
}

function taskSessionMeta(task) {
    const sessionId = String(task?.session_id || "").trim();
    if (!sessionId) return "";
    const session = (S.ceoSessions || []).find((item) => String(item?.session_id || "").trim() === sessionId);
    const label = String(session?.title || sessionId).trim();
    return `Session ${label}`;
}

function taskMetaText(task) {
    const parts = [];
    if (task.is_unread) parts.push("Unread");
    const sessionMeta = taskSessionMeta(task);
    if (sessionMeta) parts.push(sessionMeta);
    if (task.created_at) parts.push(`Created ${formatSessionTime(task.created_at)}`);
    return parts.join(" · ") || "No timestamp";
}
function renderTasks() {
    U.taskGrid.innerHTML = "";
    const meta = paginateResources(orderedTasks(S.tasks), S.taskPage, S.taskPageSize);
    S.taskPage = meta.currentPage;
    syncTaskPagination(meta);
    if (!meta.total) {
        U.taskGrid.innerHTML = `<div class="empty-state" style="grid-column: 1/-1;">${esc(taskSessionEmptyText())}</div>`;
        return updateTaskToolbar();
    }
    meta.items.forEach((task) => {
        const selected = S.selectedTaskIds.has(task.task_id);
        const primaryAction = primaryTaskAction(task);
        const statusKey = taskStatusKey(task);
        const el = document.createElement("div");
        el.className = `project-card${selected ? " is-selected" : ""}${S.multiSelectMode ? " is-multi-mode" : ""}`;
        el.innerHTML = `
            <div class="pc-topbar">
                <label class="project-select-toggle${S.multiSelectMode ? " is-visible" : ""}"><input type="checkbox" class="project-select-checkbox" ${selected ? "checked" : ""} ${S.taskBusy ? "disabled" : ""}><span>Select</span></label>
                <span class="status-badge" data-status="${esc(statusKey)}">${esc(taskStatusLabel(task))}</span>
            </div>
            <div class="pc-header"><div class="pc-header-left"><h3 class="pc-title">${esc(task.title || task.task_id)}</h3><span class="pc-id">${esc(task.task_id)}</span></div></div>
            <div class="pc-summary">${esc(task.brief || "No summary")}</div>
            <div class="pc-stats">${esc(taskMetaText(task))}</div>
            <div class="pc-token-stats">${esc(taskTokenSummaryLine(task.token_usage))}</div>
            <div class="pc-actions">
                <div class="pc-actions-left">
                    ${primaryAction ? `<button class="project-action-btn ${primaryAction.tone}" type="button" data-action="${primaryAction.action}" ${S.taskBusy ? "disabled" : ""}>${primaryAction.label}</button>` : ""}
                </div>
                <div class="pc-actions-right">
                    ${canDelete(task) ? `<button class="project-action-btn danger" type="button" data-action="delete" ${S.taskBusy ? "disabled" : ""}>删除</button>` : ""}
                </div>
            </div>
        `;
        const toggle = el.querySelector(".project-select-toggle");
        const checkbox = el.querySelector(".project-select-checkbox");
        toggle?.addEventListener("click", (e) => e.stopPropagation());
        checkbox?.addEventListener("change", (e) => {
            e.stopPropagation();
            if (e.target.checked) S.selectedTaskIds.add(task.task_id);
            else S.selectedTaskIds.delete(task.task_id);
            renderTasks();
        });
        el.querySelectorAll(".project-action-btn").forEach((btn) => btn.addEventListener("click", async (e) => {
            e.stopPropagation();
            await runTaskAction(task.task_id, btn.dataset.action, { returnFocus: btn });
        }));
        el.addEventListener("click", () => {
            if (S.multiSelectMode) {
                toggleTaskSelection(task.task_id);
                return;
            }
            void openTask(task.task_id);
        });
        U.taskGrid.appendChild(el);
    });
    updateTaskToolbar();
    icons();
}

async function loadTasks() {
    U.taskGrid.innerHTML = '<div class="empty-state" style="grid-column: 1/-1;">Loading tasks...</div>';
    try {
        S.tasks = await ApiClient.getTasks(1, taskSessionQueryValue());
        syncTaskSelection();
        renderTaskSessionScope();
        renderTasks();
    } catch (e) {
        U.taskGrid.innerHTML = `<div class="empty-state error" style="grid-column: 1/-1;">Failed to load tasks: ${esc(e.message)}</div>`;
        showToast({ title: "Load failed", text: e.message || "Unknown error", kind: "error" });
    }
}

async function runTaskAction(taskId, action, { returnFocus = null } = {}) {
    if (!taskId || !action) return;
    if (action === "delete") {
        openConfirm({
            title: "删除任务",
            text: "删除后将移除任务记录、树快照和工件文件，且无法恢复。",
            confirmLabel: "删除",
            confirmKind: "danger",
            returnFocus,
            onConfirm: () => performTaskAction(taskId, action),
        });
        return;
    }
    await performTaskAction(taskId, action);
}

async function performTaskAction(taskId, action) {
    if (!taskId || !action) return;
    S.taskBusy = true;
    renderTasks();
    try {
        await requestTaskAction(taskId, action);
        showToast({ title: taskActionSuccessTitle(action), text: taskId, kind: "success" });
        await loadTasks();
        if (action === "delete") {
            handleDeletedTasks([taskId]);
        } else if (S.currentTaskId === taskId) {
            await loadTaskDetail(taskId, { preserveView: true, reopenSocket: false });
            await loadTaskArtifacts();
        }
    } catch (e) {
        showToast({ title: taskActionFailureTitle(action), text: taskActionErrorText(action, e), kind: "error" });
    } finally {
        S.taskBusy = false;
        renderTasks();
    }
}

async function runTaskBatchAction(action, { returnFocus = null } = {}) {
    closeTaskMenus();
    const selected = getSelectedTasks();
    const eligible = selected.filter((task) => {
        if (action === "pause") return canPause(task);
        if (action === "resume") return canResume(task);
        if (action === "delete") return canDelete(task);
        return false;
    });
    if (!eligible.length) {
        showToast({ title: "No eligible tasks", text: "Current selection cannot perform this action.", kind: "warn" });
        return;
    }
    if (action === "delete") {
        openConfirm({
            title: "删除任务",
            text: "删除后将移除任务记录、树快照和工件文件，且无法恢复。",
            confirmLabel: "删除",
            confirmKind: "danger",
            returnFocus,
            onConfirm: () => performTaskBatchAction(action, eligible),
        });
        return;
    }
    await performTaskBatchAction(action, eligible);
}

async function performTaskBatchAction(action, eligible) {
    S.taskBusy = true;
    renderTasks();
    try {
        const results = await Promise.allSettled(eligible.map((task) => requestTaskAction(task.task_id, action)));
        const succeeded = results
            .map((result, index) => (result.status === "fulfilled" ? eligible[index].task_id : ""))
            .filter(Boolean);
        const failed = results
            .map((result, index) => (result.status === "rejected" ? { taskId: eligible[index].task_id, error: result.reason } : null))
            .filter(Boolean);
        await loadTasks();
        if (action === "delete") {
            handleDeletedTasks(succeeded);
        } else if (S.currentTaskId && succeeded.includes(S.currentTaskId)) {
            await loadTaskDetail(S.currentTaskId, { preserveView: true, reopenSocket: false });
            await loadTaskArtifacts();
        }
        if (failed.length && !succeeded.length) {
            showToast({
                title: taskActionFailureTitle(action),
                text: taskActionErrorText(action, failed[0].error),
                kind: "error",
            });
            return;
        }
        if (failed.length) {
            showToast({
                title: action === "delete" ? "删除完成" : `${taskActionText(action)}完成`,
                text: `${succeeded.length} 个任务成功，${failed.length} 个失败`,
                kind: "warn",
            });
            return;
        }
        showToast({
            title: taskActionSuccessTitle(action),
            text: action === "delete" ? `已删除 ${succeeded.length} 个任务` : `${succeeded.length} 个任务已更新`,
            kind: "success",
        });
    } finally {
        S.taskBusy = false;
        renderTasks();
    }
}

function updateTaskToolbar() {
    syncTaskSelection();
    const selected = getSelectedTasks();
    if (U.taskToolbar) U.taskToolbar.hidden = !S.multiSelectMode;
    if (U.taskMultiToggle) {
        U.taskMultiToggle.setAttribute("aria-pressed", S.multiSelectMode ? "true" : "false");
        U.taskMultiToggle.classList.toggle("active", S.multiSelectMode);
    }
    const filterButtons = [...(U.taskFilterMenu?.querySelectorAll("[data-select-bucket]") || [])];
    filterButtons.forEach((button) => {
        button.disabled = S.taskBusy || !S.tasks.some((task) => statusBucketMatches(task, button.dataset.selectBucket));
    });
    const batchButtons = [...(U.taskBatchMenu?.querySelectorAll("[data-batch-action]") || [])];
    batchButtons.forEach((button) => {
        const action = button.dataset.batchAction;
        const enabled = action === "pause"
            ? selected.some((task) => canPause(task))
            : action === "resume"
                ? selected.some((task) => canResume(task))
                : action === "retry"
                    ? selected.some((task) => canRetry(task))
                    : selected.some((task) => canDelete(task));
        button.disabled = S.taskBusy || !enabled;
    });
    setTaskMenuVisibility();
}

function primaryTaskAction(task) {
    if (canPause(task)) return { action: "pause", label: "暂停", tone: "warn" };
    if (canResume(task)) return { action: "resume", label: "开始", tone: "success" };
    if (canRetry(task)) return { action: "retry", label: "重试", tone: "success" };
    return null;
}

function taskActionText(action) {
    return ({ pause: "暂停", resume: "开始", retry: "重试", delete: "删除" }[action] || "操作");
}

function taskActionSuccessTitle(action) {
    return action === "delete" ? "删除成功" : `${taskActionText(action)}成功`;
}

function taskActionFailureTitle(action) {
    return action === "delete" ? "删除失败" : `${taskActionText(action)}失败`;
}

function taskActionErrorText(action, error) {
    const message = String(error?.message || error || "").trim();
    if (action === "retry") {
        if (message.includes("task_not_failed")) return "仅失败任务可重试";
        if (message.includes("task_not_found")) return "任务不存在或已被删除";
    }
    if (action === "delete") {
        if (message.includes("task_still_stopping")) return "任务仍在停止中，请稍后再删";
        if (message.includes("task_not_deletable") || message.includes("task_not_paused")) return "仅已暂停或已完成的任务可删除";
        if (message.includes("task_not_found")) return "任务不存在或已被删除";
    }
    return message || "Unknown error";
}

async function requestTaskAction(taskId, action) {
    if (action === "pause") return ApiClient.pauseTask(taskId);
    if (action === "resume") return ApiClient.resumeTask(taskId);
    if (action === "retry") return ApiClient.retryTask(taskId);
    if (action === "delete") return ApiClient.deleteTask(taskId);
    throw new Error(`Unsupported task action: ${action}`);
}

async function runTaskAction(taskId, action, { returnFocus = null } = {}) {
    if (!taskId || !action) return;
    if (action === "delete") {
        openConfirm({
            title: "删除任务",
            text: "删除后将移除任务记录、树快照和工件文件，且无法恢复。",
            confirmLabel: "删除",
            confirmKind: "danger",
            returnFocus,
            onConfirm: () => performTaskAction(taskId, action),
        });
        return;
    }
    await performTaskAction(taskId, action);
}

async function performTaskAction(taskId, action) {
    if (!taskId || !action) return;
    S.taskBusy = true;
    renderTasks();
    try {
        const result = await requestTaskAction(taskId, action);
        const successText = action === "retry" ? (result?.task_id || taskId) : taskId;
        showToast({ title: taskActionSuccessTitle(action), text: successText, kind: "success" });
        await loadTasks();
        if (action === "delete") {
            handleDeletedTasks([taskId]);
        } else if (action !== "retry" && S.currentTaskId === taskId) {
            await loadTaskDetail(taskId, { preserveView: true, reopenSocket: false });
            await loadTaskArtifacts();
        }
    } catch (e) {
        showToast({ title: taskActionFailureTitle(action), text: taskActionErrorText(action, e), kind: "error" });
    } finally {
        S.taskBusy = false;
        renderTasks();
    }
}

async function runTaskBatchAction(action, { returnFocus = null } = {}) {
    closeTaskMenus();
    const selected = getSelectedTasks();
    const eligible = selected.filter((task) => {
        if (action === "pause") return canPause(task);
        if (action === "resume") return canResume(task);
        if (action === "retry") return canRetry(task);
        if (action === "delete") return canDelete(task);
        return false;
    });
    if (!eligible.length) {
        showToast({ title: "No eligible tasks", text: "Current selection cannot perform this action.", kind: "warn" });
        return;
    }
    if (action === "delete") {
        openConfirm({
            title: "删除任务",
            text: "删除后将移除任务记录、树快照和工件文件，且无法恢复。",
            confirmLabel: "删除",
            confirmKind: "danger",
            returnFocus,
            onConfirm: () => performTaskBatchAction(action, eligible),
        });
        return;
    }
    await performTaskBatchAction(action, eligible);
}

async function performTaskBatchAction(action, eligible) {
    S.taskBusy = true;
    renderTasks();
    try {
        const results = await Promise.allSettled(eligible.map((task) => requestTaskAction(task.task_id, action)));
        const succeeded = results
            .map((result, index) => (result.status === "fulfilled" ? eligible[index].task_id : ""))
            .filter(Boolean);
        const failed = results
            .map((result, index) => (result.status === "rejected" ? { taskId: eligible[index].task_id, error: result.reason } : null))
            .filter(Boolean);
        await loadTasks();
        if (action === "delete") {
            handleDeletedTasks(succeeded);
        } else if (action !== "retry" && S.currentTaskId && succeeded.includes(S.currentTaskId)) {
            await loadTaskDetail(S.currentTaskId, { preserveView: true, reopenSocket: false });
            await loadTaskArtifacts();
        }
        if (failed.length && !succeeded.length) {
            showToast({
                title: taskActionFailureTitle(action),
                text: taskActionErrorText(action, failed[0].error),
                kind: "error",
            });
            return;
        }
        if (failed.length) {
            showToast({
                title: action === "delete" ? "删除完成" : `${taskActionText(action)}完成`,
                text: `${succeeded.length} 个任务成功，${failed.length} 个失败`,
                kind: "warn",
            });
            return;
        }
        const successText = action === "delete"
            ? `已删除 ${succeeded.length} 个任务`
            : action === "retry"
                ? `已创建 ${succeeded.length} 个重试任务`
                : `${succeeded.length} 个任务已更新`;
        showToast({
            title: taskActionSuccessTitle(action),
            text: successText,
            kind: "success",
        });
    } finally {
        S.taskBusy = false;
        renderTasks();
    }
}

function resetTaskView() {
    if (S.taskWs) {
        S.taskWs.close();
        S.taskWs = null;
    }
    S.currentTask = null;
    S.currentTaskProgress = null;
    S.taskArtifacts = [];
    S.selectedArtifactId = "";
    S.artifactContent = "";
    S.tree = null;
    S.treeView = null;
    S.selectedNodeId = null;
    S.treePan.active = false;
    S.treePan.originNodeId = null;
    S.treePan.offsetX = 0;
    S.treePan.offsetY = 0;
    S.treePan.baseOffsetX = 0;
    S.treePan.baseOffsetY = 0;
    S.treePan.scale = 1;
    S.treePan.baseScale = 1;
    S.treePan.moved = false;
    S.treePan.suppressClickNodeId = null;
    U.tree.innerHTML = '<div class="empty-state">Waiting for task tree...</div>';
    U.feedTitle.textContent = "Node Details";
    if (U.adFlow) U.adFlow.innerHTML = '<div class="empty-state task-trace-empty">选择任务树中的节点后，这里会显示执行流程。</div>';
    if (U.adAcceptance) U.adAcceptance.textContent = "暂无验收结果";
    if (U.nodeEmpty) U.nodeEmpty.style.display = "block";
    if (U.artifactList) U.artifactList.innerHTML = '<div class="empty-state" style="padding: 10px;">No artifacts yet.</div>';
    if (U.artifactContent) U.artifactContent.textContent = "Select an artifact to view details.";
    if (U.artifactApply) U.artifactApply.hidden = true;
    refreshTaskDetailScrollRegions();
    if (U.taskTokenButton) U.taskTokenButton.disabled = true;
    if (U.taskTokenSummaryText) U.taskTokenSummaryText.textContent = "任务级 token 消耗会在这里实时刷新。";
    if (U.taskTokenContent) U.taskTokenContent.innerHTML = '<div class="empty-state">请选择一个任务后查看 token 统计。</div>';
    setTaskTokenStatsOpen(false);
    setTaskSelectionEmptyVisible(false);
    hideAgent();
}

function setTaskDetailOpen(open) {
    setDrawerOpen(U.taskDetailBackdrop, U.taskDetailDrawer, open);
}

function setTaskTokenStatsOpen(open) {
    S.taskTokenStatsOpen = !!open;
    setDrawerOpen(U.taskTokenBackdrop, U.taskTokenDrawer, !!open);
    if (open) renderTaskTokenStats();
}

function renderTaskTokenStats() {
    if (!U.taskTokenContent || !U.taskTokenSummaryText) return;
    const summary = taskTokenUsage(S.currentTask, S.currentTaskProgress);
    U.taskTokenSummaryText.textContent = taskTokenSummaryLine(summary);
    if (!summary.tracked) {
        U.taskTokenContent.innerHTML = `
            <div class="task-token-topline">
                <div class="task-token-stat"><strong>未统计</strong><span>该任务创建于统计上线前，暂无精确数据。</span></div>
            </div>
        `;
        return;
    }
    const modelRows = Array.isArray(S.currentTaskProgress?.token_usage_by_model)
        ? S.currentTaskProgress.token_usage_by_model.map(normalizeModelTokenUsage).sort((a, b) => {
            const delta = tokenKnownTotal(b) - tokenKnownTotal(a);
            if (delta !== 0) return delta;
            return String(a.model_key || "").localeCompare(String(b.model_key || ""));
        })
        : [];
    const partialNote = summary.is_partial
        ? '<span class="task-token-badge warn">部分模型未返回 usage</span>'
        : '<span class="task-token-badge success">统计完整</span>';
    const topline = `
        <div class="task-token-topline">
            <div class="task-token-stat"><strong>${esc(formatTokenCount(summary.input_tokens))}</strong><span>总输入</span></div>
            <div class="task-token-stat"><strong>${esc(formatTokenCount(summary.output_tokens))}</strong><span>总输出</span></div>
            <div class="task-token-stat"><strong>${esc(formatTokenCount(summary.cache_hit_tokens))}</strong><span>缓存命中</span></div>
            <div class="task-token-stat"><strong>${esc(formatTokenCount(summary.call_count))}</strong><span>模型调用</span></div>
        </div>
        <div class="task-token-meta">
            ${partialNote}
            <span class="task-token-subtle">有 usage ${esc(formatTokenCount(summary.calls_with_usage))} · 缺失 ${esc(formatTokenCount(summary.calls_without_usage))}</span>
        </div>
    `;
    if (!summary.call_count) {
        U.taskTokenContent.innerHTML = `${topline}<div class="empty-state task-token-empty">尚未发生模型调用。</div>`;
        return;
    }
    const rowsMarkup = modelRows.length
        ? modelRows.map((item) => {
            const subtitleParts = [item.provider_id, item.provider_model].filter(Boolean);
            const badges = [];
            if (item.is_partial) badges.push('<span class="task-token-badge warn">部分缺失</span>');
            if (!item.calls_without_usage) badges.push('<span class="task-token-badge success">完整</span>');
            return `
                <div class="task-token-model-item">
                    <div class="task-token-model-head">
                        <div>
                            <h3>${esc(item.model_key || "未命名模型")}</h3>
                            <p>${esc(subtitleParts.join(" · ") || "模型标识未提供")}</p>
                        </div>
                        <div class="task-token-model-badges">${badges.join("")}</div>
                    </div>
                    <div class="task-token-model-stats">
                        <span>输入 ${esc(formatTokenCount(item.input_tokens))}</span>
                        <span>输出 ${esc(formatTokenCount(item.output_tokens))}</span>
                        <span>缓存命中 ${esc(formatTokenCount(item.cache_hit_tokens))}</span>
                    </div>
                    <div class="task-token-model-meta">
                        调用 ${esc(formatTokenCount(item.call_count))} · 有 usage ${esc(formatTokenCount(item.calls_with_usage))} · 缺失 ${esc(formatTokenCount(item.calls_without_usage))}
                    </div>
                </div>
            `;
        }).join("")
        : '<div class="empty-state task-token-empty">当前只有任务级统计，尚无按模型明细。</div>';
    U.taskTokenContent.innerHTML = `${topline}<div class="task-token-model-list">${rowsMarkup}</div>`;
}

function setTaskSelectionEmptyVisible(visible) {
    if (U.taskSelectionEmpty) U.taskSelectionEmpty.hidden = !visible;
}

function clearAgentSelection({ rerender = true } = {}) {
    S.selectedNodeId = null;
    U.feedTitle.textContent = "Node Details";
    hideAgent();
    if (rerender) renderTree();
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

function bindTreePan() {
    if (!U.tree || U.tree.dataset.panBound === "true") return;
    U.tree.dataset.panBound = "true";
    const state = S.treePan;
    const applyPan = () => {
        const canvas = U.tree?.querySelector(".execution-tree");
        if (canvas) {
            canvas.style.transformOrigin = "0 0";
            canvas.style.transform = `translate(${Math.round(state.offsetX)}px, ${Math.round(state.offsetY)}px) scale(${state.scale})`;
        }
    };
    window.addEventListener("mousemove", (e) => {
        if (!state.active) return;
        const dx = e.clientX - state.startX;
        const dy = e.clientY - state.startY;
        if (Math.abs(dx) > 4 || Math.abs(dy) > 4) state.moved = true;
        state.offsetX = state.baseOffsetX + dx;
        state.offsetY = state.baseOffsetY + dy;
        applyPan();
    });
    window.addEventListener("mouseup", () => {
        if (!state.active) return;
        window.setTimeout(() => { state.suppressClickNodeId = null; }, 0);
        state.active = false;
        state.baseOffsetX = state.offsetX;
        state.baseOffsetY = state.offsetY;
        U.tree.classList.remove("is-panning");
        if (state.moved) state.suppressClickNodeId = state.originNodeId;
    });
    U.tree.addEventListener("mousedown", (e) => {
        const node = e.target.closest(".execution-tree-node");
        if (node) return;
        state.active = true;
        state.moved = false;
        state.originNodeId = e.target instanceof Element ? e.target.closest(".execution-tree-node")?.dataset?.id || null : null;
        state.startX = e.clientX;
        state.startY = e.clientY;
        U.tree.classList.add("is-panning");
    });
    U.tree.addEventListener("dragstart", (e) => {
        if (e.target.closest(".execution-tree-node")) e.preventDefault();
    });
    U.tree.addEventListener("wheel", (e) => {
        const canvas = U.tree?.querySelector(".execution-tree");
        if (!canvas) return;
        e.preventDefault();
        const rect = U.tree.getBoundingClientRect();
        const pointerX = e.clientX - rect.left;
        const pointerY = e.clientY - rect.top;
        const nextScale = clamp(
            e.deltaY < 0 ? state.scale * TREE_SCALE_FACTOR : state.scale / TREE_SCALE_FACTOR,
            TREE_SCALE_MIN,
            TREE_SCALE_MAX,
        );
        if (Math.abs(nextScale - state.scale) < 0.001) return;
        const contentX = (pointerX - state.offsetX) / state.scale;
        const contentY = (pointerY - state.offsetY) / state.scale;
        state.scale = nextScale;
        state.baseScale = nextScale;
        state.offsetX = pointerX - contentX * nextScale;
        state.offsetY = pointerY - contentY * nextScale;
        state.baseOffsetX = state.offsetX;
        state.baseOffsetY = state.offsetY;
        applyPan();
    }, { passive: false });
    U.tree.__applyPan = applyPan;
}

function nodeOutputText(node) {
    if (!node) return "";
    if (typeof node.output === "string") return node.output;
    if (Array.isArray(node.output)) return node.output.map((item) => String(item.content || "").trim()).filter(Boolean).join("\n\n");
    return String(node.final_output || "");
}

function truncateNodeTitle(text, maxChars = 20) {
    const chars = Array.from(String(text || ""));
    if (chars.length <= maxChars) return chars.join("");
    return `${chars.slice(0, maxChars).join("")}…`;
}

function resolveNodeTitle(node, detail) {
    const goal = String(detail?.goal || "").trim();
    const fullTitle = goal || String(node?.title || node?.node_id || "").trim() || String(node?.node_id || "");
    return {
        goal: goal || fullTitle,
        fullTitle,
        title: truncateNodeTitle(fullTitle, 20),
    };
}

function buildNodeExecutionTrace(node, detail) {
    const source = detail?.execution_trace && typeof detail.execution_trace === "object" ? detail.execution_trace : {};
    const toolSteps = Array.isArray(source.tool_steps) ? source.tool_steps : [];
    return {
        initial_prompt: String(source.initial_prompt ?? detail?.prompt ?? detail?.goal ?? node?.input ?? ""),
        tool_steps: toolSteps.map((step) => ({
            tool_call_id: String(step?.tool_call_id || ""),
            tool_name: String(step?.tool_name || "tool"),
            arguments_text: String(step?.arguments_text || ""),
            output_text: String(step?.output_text || ""),
            started_at: String(step?.started_at || ""),
            finished_at: String(step?.finished_at || ""),
            elapsed_seconds: Number.isFinite(Number(step?.elapsed_seconds)) ? Number(step.elapsed_seconds) : null,
            status: ["running", "success", "error"].includes(String(step?.status || ""))
                ? String(step.status)
                : "info",
        })),
        final_output: String(source.final_output ?? detail?.final_output ?? nodeOutputText(detail) ?? nodeOutputText(node) ?? ""),
        acceptance_result: String(source.acceptance_result ?? detail?.check_result ?? node?.check_result ?? ""),
    };
}

function traceStatusLabel(status) {
    return ({
        info: "已记录",
        running: "执行中",
        success: "成功",
        error: "失败",
    }[String(status || "")] || "已记录");
}

function nodeFinalTraceStatus(node) {
    if (String(node?.state || "") === "in_progress") return "running";
    if (String(node?.state || "") === "failed") return "error";
    return "success";
}

function renderTraceField(label, value, emptyText = "暂无内容") {
    const text = String(value || "").trim() || emptyText;
    return `
        <div class="task-trace-field">
            <div class="task-trace-label">${esc(label)}</div>
            <div class="code-block task-trace-code">${esc(text)}</div>
        </div>
    `;
}

function renderTraceStep({ title, status = "info", open = false, bodyHtml = "" }) {
    return `
        <details class="interaction-step task-trace-step ${esc(status)}"${open ? " open" : ""}>
            <summary class="task-trace-summary">
                <span class="interaction-step-lead">
                    <span class="interaction-step-title">${esc(title)}</span>
                </span>
                <span class="interaction-step-side">
                    <span class="task-trace-runtime" hidden></span>
                    <span class="interaction-step-status">${esc(traceStatusLabel(status))}</span>
                </span>
            </summary>
            <div class="task-trace-body">${bodyHtml}</div>
        </details>
    `;
}

function toPixels(value) {
    const parsed = Number.parseFloat(String(value || ""));
    return Number.isFinite(parsed) ? parsed : 0;
}

function setScrollViewportLimit(container, itemSelector, visibleCount, measureItem = null) {
    if (!(container instanceof HTMLElement)) return;
    container.style.maxHeight = "";
    const items = Array.from(container.querySelectorAll(itemSelector)).filter((item) => item instanceof HTMLElement);
    if (!items.length || visibleCount <= 0) return;
    const styles = window.getComputedStyle(container);
    const gap = toPixels(styles.rowGap || styles.gap);
    const targetCount = Math.min(visibleCount, items.length);
    let height = 0;
    for (let index = 0; index < targetCount; index += 1) {
        const item = items[index];
        const measured = measureItem ? Number(measureItem(item, index)) : item.getBoundingClientRect().height;
        height += measured > 0 ? measured : 0;
        if (index < targetCount - 1) height += gap;
    }
    if (height > 0) container.style.maxHeight = `${Math.ceil(height)}px`;
}

function traceStepSummaryHeight(step) {
    if (!(step instanceof HTMLElement)) return 0;
    const summary = step.querySelector(".task-trace-summary");
    const summaryHeight = summary instanceof HTMLElement ? summary.getBoundingClientRect().height : step.getBoundingClientRect().height;
    const styles = window.getComputedStyle(step);
    return summaryHeight
        + toPixels(styles.paddingTop)
        + toPixels(styles.paddingBottom)
        + toPixels(styles.borderTopWidth)
        + toPixels(styles.borderBottomWidth);
}

function refreshTaskDetailScrollRegions() {
    const traceList = U.adFlow?.querySelector(".task-trace-list");
    if (traceList instanceof HTMLElement) {
        setScrollViewportLimit(traceList, ".task-trace-step", 10, traceStepSummaryHeight);
    }
    if (U.artifactList instanceof HTMLElement) {
        setScrollViewportLimit(U.artifactList, ".artifact-item", 5);
    }
}

function renderExecutionTrace(node) {
    if (!U.adFlow) return;
    const trace = node?.executionTrace || buildNodeExecutionTrace(node, node);
    const steps = [
        renderTraceStep({
            title: "初始提示词",
            status: "info",
            open: false,
            bodyHtml: renderTraceField("内容", trace.initial_prompt, "暂无初始提示词"),
        }),
        ...trace.tool_steps.map((step) => renderTraceStep({
            title: `使用工具 · ${step.tool_name || "tool"}`,
            status: step.status || "info",
            open: false,
            bodyHtml: [
                renderTraceField("参数", step.arguments_text, "无参数"),
                renderTraceField("工具输出", step.output_text, step.status === "running" ? "等待工具输出…" : "暂无工具输出"),
            ].join(""),
        })),
        renderTraceStep({
            title: "最终输出",
            status: nodeFinalTraceStatus(node),
            open: true,
            bodyHtml: renderTraceField("内容", trace.final_output, "暂无最终输出"),
        }),
    ];
    U.adFlow.innerHTML = `<div class="task-trace-list">${steps.join("")}</div>`;
    const traceItems = Array.from(U.adFlow.querySelectorAll(".task-trace-step"));
    trace.tool_steps.forEach((step, index) => {
        const item = traceItems[index + 1];
        if (!(item instanceof HTMLElement)) return;
        item.dataset.traceStatus = String(step.status || "info");
        if (step.started_at) item.dataset.startedAt = step.started_at;
        if (step.finished_at) item.dataset.finishedAt = step.finished_at;
        if (Number.isFinite(step.elapsed_seconds)) item.dataset.elapsedSeconds = String(step.elapsed_seconds);
        const runtimeEl = item.querySelector(".task-trace-runtime");
        if (runtimeEl instanceof HTMLElement) updateRuntimeBadge(item, runtimeEl);
    });
    refreshTaskDetailScrollRegions();
    refreshLiveDurationBadges();
}

function renderAcceptanceResult(text) {
    if (!U.adAcceptance) return;
    U.adAcceptance.textContent = String(text || "").trim() || "暂无验收结果";
}

function buildExecutionTree(rawTree) {
    if (!rawTree) return null;
    const nodeRecords = Array.isArray(S.currentTaskProgress?.nodes) ? S.currentTaskProgress.nodes : [];
    const nodeMap = new Map(nodeRecords.map((item) => [String(item.node_id || ""), item]));
    const walk = (node) => {
        const detail = nodeMap.get(String(node.node_id || "")) || {};
        const status = String(node.status || detail.status || "unknown").trim().toLowerCase() || "unknown";
        const title = resolveNodeTitle(node, detail);
        return {
            node_id: node.node_id,
            title: title.title,
            fullTitle: title.fullTitle,
            goal: title.goal,
            kind: detail.node_kind || "execution",
            state: status,
            display_state: status.toUpperCase(),
            executionTrace: buildNodeExecutionTrace(node, detail),
            children: Array.isArray(node.children) ? node.children.map(walk) : [],
        };
    };
    return walk(rawTree);
}

function renderTree() {
    if (!S.tree) return;
    S.treeView = buildExecutionTree(S.tree);
    if (!S.treeView) {
        U.tree.innerHTML = '<div class="empty-state">No nodes to display.</div>';
        setTaskSelectionEmptyVisible(false);
        return;
    }
    const wrapper = document.createElement("div");
    wrapper.className = "execution-tree";
    const rootList = document.createElement("ul");
    rootList.className = "execution-tree-list";
    const walk = (node) => {
        const title = String(node.title || node.node_id || "");
        const fullTitle = String(node.fullTitle || title);
        const displayState = String(node.display_state || node.state || "").toUpperCase();
        const item = document.createElement("li");
        item.className = "execution-tree-item";
        const el = document.createElement("button");
        el.type = "button";
        el.className = `execution-tree-node${S.selectedNodeId === node.node_id ? " selected" : ""}`;
        el.dataset.id = node.node_id;
        el.dataset.kind = node.kind || "execution";
        el.title = fullTitle;
        el.setAttribute("aria-pressed", S.selectedNodeId === node.node_id ? "true" : "false");
        el.innerHTML = `<span class="execution-tree-node-head"><span class="execution-tree-node-title">${esc(title)}</span><span class="status-badge" data-status="${esc(node.state || "")}">${esc(displayState)}</span></span>`;
        el.addEventListener("click", (e) => {
            if (S.treePan.suppressClickNodeId && S.treePan.suppressClickNodeId === String(node.node_id || "")) {
                S.treePan.suppressClickNodeId = null;
                return;
            }
            e.stopPropagation();
            S.selectedNodeId = node.node_id;
            showAgent(node);
            renderTree();
        });
        item.appendChild(el);
        if ((node.children || []).length) {
            const branch = document.createElement("ul");
            branch.className = "execution-tree-list";
            (node.children || []).forEach((child) => branch.appendChild(walk(child)));
            item.appendChild(branch);
        }
        return item;
    };
    rootList.appendChild(walk(S.treeView));
    wrapper.appendChild(rootList);
    wrapper.style.transformOrigin = "0 0";
    wrapper.style.transform = `translate(${Math.round(S.treePan.offsetX)}px, ${Math.round(S.treePan.offsetY)}px) scale(${S.treePan.scale})`;
    U.tree.innerHTML = "";
    U.tree.appendChild(wrapper);
    if (S.selectedNodeId) {
        const selected = findTreeNode(S.treeView, S.selectedNodeId);
        if (selected) {
            setTaskSelectionEmptyVisible(false);
            showAgent(selected);
        }
        else {
            S.selectedNodeId = null;
            setTaskSelectionEmptyVisible(true);
            hideAgent();
        }
    } else {
        setTaskSelectionEmptyVisible(true);
        hideAgent();
    }
}
function renderArtifacts() {
    if (!U.artifactList) return;
    U.artifactList.innerHTML = "";
    if (!Array.isArray(S.taskArtifacts) || !S.taskArtifacts.length) {
        U.artifactList.innerHTML = '<div class="empty-state" style="padding: 10px;">No artifacts yet.</div>';
        if (U.artifactContent) U.artifactContent.textContent = "Select an artifact to view details.";
        if (U.artifactApply) U.artifactApply.hidden = true;
        refreshTaskDetailScrollRegions();
        return;
    }
    S.taskArtifacts.forEach((artifact) => {
        const button = document.createElement("button");
        button.type = "button";
        button.className = `artifact-item${S.selectedArtifactId === artifact.artifact_id ? " active" : ""}`;
        button.innerHTML = `<strong>${esc(artifact.title || artifact.artifact_id)}</strong><span>${esc(artifact.kind || "artifact")}</span><small>${esc(artifact.preview_text || artifact.created_at || "")}</small>`;
        button.addEventListener("click", () => void selectArtifact(artifact.artifact_id));
        U.artifactList.appendChild(button);
    });
    refreshTaskDetailScrollRegions();
}

async function loadTaskArtifacts() {
    if (!S.currentTaskId) return [];
    S.taskArtifacts = await ApiClient.getTaskArtifacts(S.currentTaskId);
    renderArtifacts();
    if (S.taskArtifacts.length && !S.selectedArtifactId) {
        await selectArtifact(S.taskArtifacts[0].artifact_id);
    }
    return S.taskArtifacts;
}

async function selectArtifact(artifactId) {
    if (!S.currentTaskId || !artifactId) return;
    S.selectedArtifactId = artifactId;
    renderArtifacts();
    const data = await ApiClient.getTaskArtifact(S.currentTaskId, artifactId);
    S.artifactContent = data.content || "";
    if (U.artifactContent) U.artifactContent.textContent = S.artifactContent || "";
    const artifact = S.taskArtifacts.find((item) => item.artifact_id === artifactId);
    if (U.artifactApply) U.artifactApply.hidden = !(artifact && artifact.kind === "patch");
}

async function applySelectedArtifact() {
    if (!S.currentTaskId || !S.selectedArtifactId) return;
    await ApiClient.applyTaskArtifact(S.currentTaskId, S.selectedArtifactId);
    showToast({ title: "Patch applied", text: S.selectedArtifactId, kind: "success" });
    await loadTaskArtifacts();
}

function showAgent(node) {
    U.detail.style.display = "flex";
    if (U.nodeEmpty) U.nodeEmpty.style.display = "none";
    setTaskSelectionEmptyVisible(false);
    U.adRole.textContent = node.fullTitle || node.title || node.node_id || "Node";
    U.adStatus.textContent = String(node.display_state || node.state || "").toUpperCase();
    U.adStatus.dataset.status = node.state || "";
    renderExecutionTrace(node);
    renderAcceptanceResult(node.executionTrace?.acceptance_result || "");
    U.feedTitle.textContent = `Node: ${node.fullTitle || node.title || node.node_id || ""}`;
    renderArtifacts();
    setTaskDetailOpen(true);
    icons();
}

function hideAgent() {
    if (U.detail) U.detail.style.display = "none";
    setTaskDetailOpen(false);
}

function applyTaskPayload(payload) {
    if (!payload || !payload.task || !payload.progress) return;
    S.currentTask = payload.task;
    S.currentTaskProgress = payload.progress;
    S.tree = payload.progress.root;
    U.tdTitle.textContent = payload.task.title || payload.task.task_id || "Loading...";
    U.tdStatus.textContent = taskStatusLabel(payload.task).toUpperCase();
    U.tdStatus.dataset.status = taskStatusKey(payload.task);
    U.tdSummary.textContent = payload.task.user_request || payload.task.final_output || payload.progress.text || "No summary";
    U.tdActiveCount.textContent = String((payload.progress.nodes || []).filter((node) => String(node.status || "") === "in_progress").length);
    if (U.taskTokenButton) U.taskTokenButton.disabled = !S.currentTask;
    renderTaskTokenStats();
    if (S.tree) renderTree();
    else {
        U.tree.innerHTML = '<div class="empty-state">No task tree.</div>';
        setTaskSelectionEmptyVisible(false);
        hideAgent();
    }
}

async function loadTaskDetail(taskId, { preserveView = false, reopenSocket = true } = {}) {
    if (!taskId) return;
    if (!preserveView) {
        S.currentTaskId = taskId;
        switchView("task-details");
        resetTaskView();
    }
    const payload = await ApiClient.getTask(taskId, true);
    applyTaskPayload(payload);
    if (reopenSocket) {
        if (S.taskWs) {
            S.taskWs.close();
            S.taskWs = null;
        }
        S.taskWs = new WebSocket(ApiClient.getTaskWsUrl(taskId));
        S.taskWs.onmessage = (ev) => handleTaskEvent(JSON.parse(ev.data));
    }
    return payload;
}

async function openTask(taskId) {
    try {
        await loadTaskDetail(taskId);
        await loadTaskArtifacts();
    } catch (e) {
        U.tree.innerHTML = `<div class="empty-state error">Failed to open task: ${esc(e.message)}</div>`;
        showToast({ title: "Task open failed", text: e.message || "Unknown error", kind: "error" });
    }
}

function handleTaskEvent(payload) {
    if (payload.type === "snapshot.task") {
        applyTaskPayload(payload.data || {});
        return;
    }
    if (payload.type === "artifact.applied") {
        showToast({ title: "Artifact applied", text: payload.data?.artifact_id || "", kind: "success" });
        void loadTaskArtifacts();
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
        return true;
    });
}

function displayRoleLabel(role) {
    return ({ ceo: "主Agent", execution: "执行", inspection: "检验" }[roleKey(role)] || String(role || ""));
}

function displayRiskLabel(level) {
    return ({ low: "低风险", medium: "中风险", high: "高风险" }[String(level || "").trim().toLowerCase()] || String(level || "未知风险"));
}

function resourceAvailabilityStatus(item) {
    if (item?.available === false) return "unavailable";
    return item?.enabled ? "enabled" : "disabled";
}

function normalizeResourceAvailabilityReason(reason, item = null) {
    const text = String(reason || "").trim();
    const metadata = item && typeof item.metadata === "object" ? item.metadata : {};
    const requiredBins = Array.isArray(metadata.requires_bins) ? metadata.requires_bins.filter(Boolean) : [];
    const requiredEnv = Array.isArray(metadata.requires_env) ? metadata.requires_env.filter(Boolean) : [];
    const requiredTools = Array.isArray(item?.requires_tools) ? item.requires_tools.filter(Boolean) : [];
    if (!text) return "";
    if (text === "missing required bins") return requiredBins.length ? `缺少必需命令：${requiredBins.join(", ")}` : "缺少必需命令";
    if (text === "missing required env") return requiredEnv.length ? `缺少必需环境变量：${requiredEnv.join(", ")}` : "缺少必需环境变量";
    if (text.startsWith("missing required tools:")) {
        const toolNames = text.slice("missing required tools:".length).trim();
        return `缺少依赖工具：${toolNames || requiredTools.join(", ") || "未知工具"}`;
    }
    return text;
}

function resourceAvailabilityReasons(item) {
    if (!item || item.available !== false) return [];
    const metadata = item && typeof item.metadata === "object" ? item.metadata : {};
    const warnings = Array.isArray(metadata.warnings) ? metadata.warnings : [];
    const errors = Array.isArray(metadata.errors) ? metadata.errors : [];
    const requiredBins = Array.isArray(metadata.requires_bins) ? metadata.requires_bins.filter(Boolean) : [];
    const requiredEnv = Array.isArray(metadata.requires_env) ? metadata.requires_env.filter(Boolean) : [];
    const requiredTools = Array.isArray(item.requires_tools) ? item.requires_tools.filter(Boolean) : [];
    const normalized = [...errors, ...warnings]
        .map((entry) => normalizeResourceAvailabilityReason(entry, item))
        .filter(Boolean);
    if (!normalized.length) {
        if (requiredBins.length) normalized.push(`缺少必需命令：${requiredBins.join(", ")}`);
        if (requiredEnv.length) normalized.push(`缺少必需环境变量：${requiredEnv.join(", ")}`);
        if (requiredTools.length) normalized.push(`缺少依赖工具：${requiredTools.join(", ")}`);
    }
    if (!normalized.length) normalized.push("当前资源依赖未满足，请检查运行环境。");
    return [...new Set(normalized)];
}

function displayEnabledLabel(enabled, available = true) {
    if (!available) return "不可用";
    return enabled ? "已启用" : "已禁用";
}

function renderSkills() {
    U.skillList.innerHTML = "";
    const meta = paginateResources(filterSkills(), S.skillPage, S.skillPageSize);
    S.skillPage = meta.currentPage;
    S.skillPageSize = meta.pageSize;
    syncResourcePagination("skill", meta);
    if (!meta.total) return void (U.skillList.innerHTML = '<div class="empty-state">没有匹配的 Skill。</div>');
    meta.items.forEach((skill) => {
        const el = document.createElement("button");
        el.type = "button";
        el.className = `resource-list-item${S.selectedSkill?.skill_id === skill.skill_id ? " selected" : ""}`;
        const desc = (skill.description || "").trim();
        const subtitle = desc ? (desc.length > 50 ? desc.slice(0, 47) + "..." : desc) : skill.skill_id;
        el.innerHTML = `
            <div class="resource-list-title">${esc(skill.display_name)}</div>
            <div class="resource-list-subtitle">${esc(subtitle)}</div>
            <div class="resource-list-meta">
                <span class="meta-tag risk-${String(skill.risk_level || 'low').toLowerCase()}">${esc(displayRiskLabel(skill.risk_level))}</span>
                <span class="meta-tag status-${resourceAvailabilityStatus(skill)}">${esc(displayEnabledLabel(skill.enabled, skill.available))}</span>
            </div>`;
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
    const meta = paginateResources(filterTools(), S.toolPage, S.toolPageSize);
    S.toolPage = meta.currentPage;
    S.toolPageSize = meta.pageSize;
    syncResourcePagination("tool", meta);
    if (!meta.total) return void (U.toolList.innerHTML = '<div class="empty-state">没有匹配的 Tool。</div>');
    meta.items.forEach((tool) => {
        const el = document.createElement("button");
        el.type = "button";
        el.className = `resource-list-item${S.selectedTool?.tool_id === tool.tool_id ? " selected" : ""}`;
        const desc = (tool.description || "").trim();
        const subtitle = desc ? (desc.length > 50 ? desc.slice(0, 47) + "..." : desc) : tool.tool_id;
        el.innerHTML = `
            <div class="resource-list-title">${esc(tool.display_name)}</div>
            <div class="resource-list-subtitle">${esc(subtitle)}</div>
            <div class="resource-list-meta">
                <span class="meta-tag status-${resourceAvailabilityStatus(tool)}">${esc(displayEnabledLabel(tool.enabled, tool.available))}</span>
                <span class="meta-tag tool-actions">${(tool.actions || []).length} 个 action</span>
            </div>`;
        el.addEventListener("click", () => openTool(tool.tool_id));
        U.toolList.appendChild(el);
    });
}

function communicationToastKind(status) {
    return ({ success: "success", warning: "warn", error: "error", disabled: "info" }[String(status || "").toLowerCase()] || "info");
}

function communicationRuntimeStatusKey(item) {
    const runtime = item?.runtime || {};
    if (!item?.enabled) return "blocked";
    if (runtime.connected) return "success";
    if (runtime.running) return "running";
    return "pending";
}

function communicationRuntimeLabel(item) {
    const runtime = item?.runtime || {};
    if (!item?.enabled) return "已禁用";
    if (runtime.connected) return "已连接";
    if (runtime.running) return "桥接运行中";
    if (runtime.status_exists) return "桥接待连接";
    return "待测试";
}

function normalizeCommunicationJsonText(text) {
    const source = String(text || "").trim();
    if (!source) return "{}";
    try {
        return JSON.stringify(JSON.parse(source), null, 2);
    } catch {
        return source;
    }
}

const COMMUNICATION_JSON_TEMPLATES = {
    qqbot: {
        appId: "your-qq-app-id",
        clientSecret: "your-qq-client-secret",
        webhookPath: "/qqbot/callback",
        mode: "webhook",
        accounts: {
            default: {
                name: "default",
                token: "your-qq-bot-token",
                appId: "your-qq-app-id",
                clientSecret: "your-qq-client-secret",
            },
        },
    },
    dingtalk: {
        clientId: "your-dingtalk-client-id",
        clientSecret: "your-dingtalk-client-secret",
        connectionMode: "stream",
        webhookPath: "/dingtalk/callback",
        accounts: {
            default: {
                name: "default",
                clientId: "your-dingtalk-client-id",
                clientSecret: "your-dingtalk-client-secret",
                connectionMode: "stream",
            },
        },
    },
    wecom: {
        botId: "your-wecom-bot-id",
        secret: "your-wecom-bot-secret",
        token: "your-wecom-token",
        encodingAesKey: "your-wecom-encoding-aes-key",
        mode: "ws",
        webhookPath: "/wecom",
        accounts: {
            default: {
                name: "default",
                botId: "your-wecom-bot-id",
                secret: "your-wecom-bot-secret",
                token: "your-wecom-token",
                encodingAesKey: "your-wecom-encoding-aes-key",
                mode: "ws",
            },
        },
    },
    wecomApp: {
        corpId: "your-wecom-corp-id",
        corpSecret: "your-wecom-corp-secret",
        agentId: 1000001,
        token: "your-wecom-app-token",
        encodingAesKey: "your-wecom-app-encoding-aes-key",
        webhookPath: "/wecom-app",
        accounts: {
            default: {
                name: "default",
                corpId: "your-wecom-corp-id",
                corpSecret: "your-wecom-corp-secret",
                agentId: 1000001,
                token: "your-wecom-app-token",
                encodingAesKey: "your-wecom-app-encoding-aes-key",
            },
        },
    },
    feishuChina: {
        appId: "your-feishu-app-id",
        appSecret: "your-feishu-app-secret",
        token: "your-feishu-verification-token",
        webhookPath: "/feishu",
        mode: "websocket",
        accounts: {
            default: {
                name: "default",
                appId: "your-feishu-app-id",
                appSecret: "your-feishu-app-secret",
                token: "your-feishu-verification-token",
                mode: "websocket",
            },
        },
    },
};

function getCommunicationTemplate(channelId) {
    const key = String(channelId || "").trim();
    const template = COMMUNICATION_JSON_TEMPLATES[key];
    return template ? JSON.parse(JSON.stringify(template)) : {};
}

function syncCommunicationDirtyState() {
    const next =
        S.communicationDraftEnabled !== S.communicationBaselineEnabled ||
        normalizeCommunicationJsonText(S.communicationDraftText) !== normalizeCommunicationJsonText(S.communicationBaselineText);
    setCommunicationDirty(next);
}

function renderCommunicationBridgeSummary() {
    if (!U.communicationBridgeSummary) return;
    const bridge = S.communicationBridge;
    if (!bridge) {
        U.communicationBridgeSummary.innerHTML = '<div class="empty-state">正在加载桥接状态...</div>';
        return;
    }
    const statusKey = bridge.connected ? "success" : bridge.running ? "running" : bridge.enabled ? "pending" : "blocked";
    const statusLabel = bridge.connected ? "已连接" : bridge.running ? "运行中" : bridge.enabled ? "待连接" : "未启用";
    const statusText = bridge.last_error ? `${statusLabel} · ${bridge.last_error}` : statusLabel;
    U.communicationBridgeSummary.innerHTML = `
        <div class="resource-list-item communication-summary-card">
            <div class="panel-header">
                <div>
                    <div class="resource-list-title">中国通信子系统</div>
                    <div class="resource-list-subtitle">统一负责渠道 webhook / ws / 回调通信</div>
                </div>
                <span class="meta-tag status-${esc(statusKey)}">${esc(statusText)}</span>
            </div>
            <div class="resource-list-meta">
                <span class="meta-tag">Port: ${esc(bridge.public_port)} / ${esc(bridge.control_port)}</span>
                <span class="meta-tag status-${bridge.dist_exists ? 'enabled' : 'disabled'}">${bridge.dist_exists ? "Host 已构建" : "Host 未构建"}</span>
                <span class="meta-tag status-${bridge.node_found ? 'enabled' : 'disabled'}">${bridge.node_found ? "Node 已就绪" : "Node 未找到"}</span>
            </div>
        </div>
    `;
}

function renderCommunications() {
    if (!U.communicationList) return;
    U.communicationList.innerHTML = "";
    const items = Array.isArray(S.communications) ? S.communications : [];
    if (!items.length) {
        U.communicationList.innerHTML = '<div class="empty-state">暂无可用通信方式。</div>';
        return;
    }
    items.forEach((item) => {
        const selected = S.selectedCommunication?.id === item.id;
        const el = document.createElement("button");
        el.type = "button";
        el.className = `resource-list-item communication-card${selected ? " selected" : ""}`;
        el.innerHTML = `
            <div class="panel-header">
                <div class="resource-list-title">${esc(item.label)}</div>
                <span class="meta-tag status-${esc(communicationRuntimeStatusKey(item))}">${esc(communicationRuntimeLabel(item))}</span>
            </div>
            <div class="resource-list-subtitle">${esc(item.description || item.config_path || item.id)}</div>
            <div class="resource-list-meta">
                <span class="meta-tag status-${item.enabled ? 'enabled' : 'disabled'}">${esc(displayEnabledLabel(item.enabled))}</span>
                <span class="meta-tag tool-actions">${esc(item.account_count || 0)} 个账号</span>
                <span class="meta-tag tool-actions" style="border-style: solid; opacity: 0.6;">${esc(item.config_path || "")}</span>
            </div>
        `;
        el.addEventListener("click", () => void openCommunication(item.id));
        U.communicationList.appendChild(el);
    });
}

function renderCommunicationDetail() {
    if (!S.selectedCommunication) {
        U.communicationEmpty.style.display = "block";
        U.communicationDetail.innerHTML = "";
        setDrawerOpen(U.communicationBackdrop, U.communicationDrawer, false);
        renderCommunicationActions();
        return;
    }
    U.communicationEmpty.style.display = "none";
    setDrawerOpen(U.communicationBackdrop, U.communicationDrawer, true);
    const item = S.selectedCommunication;
    const runtime = item.runtime || {};
    const toggleLabel = S.communicationDraftEnabled ? "已启用" : "已禁用";
    const statusKey = communicationRuntimeStatusKey({ ...item, enabled: S.communicationDraftEnabled });
    const statusLabel = communicationRuntimeLabel({ ...item, enabled: S.communicationDraftEnabled });
    U.communicationDetail.innerHTML = `
        <article class="resource-detail-card detail-modal-shell">
            <div class="detail-modal-header">
                <div class="detail-modal-title">
                    <h2 id="communication-detail-title">${esc(item.label)}</h2>
                    <p class="subtitle">${esc(item.config_path || item.id)}</p>
                </div>
                <div class="detail-modal-actions">
                    <button type="button" class="toolbar-btn ghost" id="communication-close-btn" data-modal-close>关闭</button>
                    <button type="button" class="toolbar-btn success" id="communication-save-btn">保存</button>
                </div>
            </div>
            <div class="detail-modal-body">
                <div class="communication-detail-meta">
                    <div class="resource-copy-block">
                        <strong>渠道状态</strong><br>
                        <span class="status-badge" data-status="${esc(statusKey)}">${esc(toggleLabel)} · ${esc(statusLabel)}</span>
                    </div>
                    <div class="resource-copy-block">
                        <strong>桥接状态</strong><br>
                        ${esc(runtime.connected ? "内部控制链路已连接" : runtime.running ? "桥接宿主运行中，等待连接" : "桥接宿主未运行或尚未连通")}
                    </div>
                </div>
                <label class="communication-toggle">
                    <input id="communication-enabled-toggle" type="checkbox" ${S.communicationDraftEnabled ? "checked" : ""}>
                    <span class="communication-toggle-track" aria-hidden="true"></span>
                    <span class="communication-toggle-copy">
                        <strong>启用该通信方式</strong>
                        <span>${S.communicationDraftEnabled ? "保存后将参与消息收发" : "保存后将停止该通信方式"}</span>
                    </span>
                </label>
                <div class="resource-draft-hint${S.communicationDirty ? " is-dirty" : ""}" ${S.communicationDirty ? "" : "hidden"}></div>
                <div class="resource-section">
                    <h3>JSON 配置</h3>
                    <div class="resource-copy-block">此处仅编辑该渠道的 JSON 配置对象；启用状态请使用上方开关。</div>
                    <div class="communication-section-head">
                        <span class="communication-section-spacer" aria-hidden="true"></span>
                        <button type="button" class="toolbar-btn ghost small" id="communication-load-template-btn">加载模板</button>
                    </div>
                    <textarea id="communication-json-editor" rows="18" class="resource-editor communication-json-editor">${esc(S.communicationDraftText)}</textarea>
                </div>
            </div>
        </article>
    `;
    const communicationSection = U.communicationDetail.querySelector(".resource-section");
    const sectionHeading = communicationSection?.querySelector("h3");
    const sectionCopy = communicationSection?.querySelector(".resource-copy-block");
    const sectionHead = communicationSection?.querySelector(".communication-section-head");
    const templateButton = U.communicationDetail.querySelector("#communication-load-template-btn");
    if (sectionHead && sectionHeading && sectionCopy) {
        sectionHead.innerHTML = "";
        sectionHead.append(sectionHeading);
        if (templateButton) {
            templateButton.textContent = "加载模板";
            sectionHead.append(templateButton);
        }
        communicationSection.insertBefore(sectionHead, sectionCopy);
    }
    U.communicationDetail.querySelector("#communication-close-btn")?.addEventListener("click", clearCommunicationSelection);
    U.communicationDetail.querySelector("#communication-save-btn")?.addEventListener("click", () => void saveCommunication());
    U.communicationDetail.querySelector("#communication-enabled-toggle")?.addEventListener("change", (e) => {
        S.communicationDraftEnabled = !!e.target.checked;
        syncCommunicationDirtyState();
        renderCommunicationDetail();
    });
    U.communicationDetail.querySelector("#communication-json-editor")?.addEventListener("input", (e) => {
        S.communicationDraftText = String(e.target.value || "");
        syncCommunicationDirtyState();
    });
    U.communicationDetail.querySelector("#communication-load-template-btn")?.addEventListener("click", () => {
        const template = getCommunicationTemplate(item.id);
        S.communicationDraftText = JSON.stringify(template, null, 2);
        const editor = U.communicationDetail.querySelector("#communication-json-editor");
        if (editor) editor.value = S.communicationDraftText;
        syncCommunicationDirtyState();
        renderCommunicationActions();
        showToast({
            title: "模板已加载",
            text: `${item.label} 的预设模板已填入 JSON 表单，确认后点击保存即可生效。`,
            kind: "info",
            durationMs: 2400,
        });
    });
    renderCommunicationActions();
}

async function loadCommunications({ renderDetail = true } = {}) {
    if (U.communicationList) U.communicationList.innerHTML = '<div class="empty-state">正在加载通信方式...</div>';
    const selectedId = S.selectedCommunication?.id || "";
    try {
        const payload = await ApiClient.getChinaChannels();
        S.communicationBridge = payload.bridge || null;
        S.communications = Array.isArray(payload.items) ? payload.items : [];
        if (selectedId) {
            const next = S.communications.find((item) => item.id === selectedId);
            if (next && !renderDetail) S.selectedCommunication = { ...S.selectedCommunication, ...next };
            else if (!next) clearCommunicationSelection();
        }
        renderCommunicationBridgeSummary();
        renderCommunications();
        if (renderDetail) renderCommunicationDetail();
    } catch (e) {
        if (U.communicationList) U.communicationList.innerHTML = `<div class="empty-state error">加载通信方式失败：${esc(e.message)}</div>`;
        if (U.communicationBridgeSummary) U.communicationBridgeSummary.innerHTML = `<div class="empty-state error">桥接状态获取失败：${esc(e.message)}</div>`;
        showToast({ title: "加载失败", text: e.message || "Unknown error", kind: "error" });
    } finally {
        renderCommunicationActions();
    }
}

async function openCommunication(channelId, quiet = false) {
    if (!quiet) {
        setDrawerOpen(U.communicationBackdrop, U.communicationDrawer, true);
        U.communicationEmpty.style.display = "none";
        U.communicationDetail.innerHTML = '<div class="empty-state">正在加载通信配置...</div>';
    }
    try {
        const item = await ApiClient.getChinaChannel(channelId);
        S.selectedCommunication = item;
        S.communicationBaselineEnabled = !!item.enabled;
        S.communicationDraftEnabled = !!item.enabled;
        S.communicationBaselineText = String(item.json_text || JSON.stringify(item.config || {}, null, 2));
        S.communicationDraftText = S.communicationBaselineText;
        S.communicationDirty = false;
        renderCommunications();
        renderCommunicationDetail();
    } catch (e) {
        U.communicationDetail.innerHTML = `<div class="empty-state error">加载通信配置失败：${esc(e.message)}</div>`;
        showToast({ title: "加载失败", text: e.message || "Unknown error", kind: "error" });
    } finally {
        renderCommunicationActions();
    }
}

async function refreshCommunications() {
    const selectedId = S.selectedCommunication?.id || "";
    S.communicationBusy = true;
    renderCommunicationActions();
    try {
        await loadCommunications({ renderDetail: false });
        if (selectedId && S.communications.some((item) => item.id === selectedId)) {
            await openCommunication(selectedId, true);
        }
    } finally {
        S.communicationBusy = false;
        renderCommunicationActions();
    }
}

async function saveCommunication() {
    if (S.communicationBusy) return;
    const item = S.selectedCommunication;
    const channelId = String(item?.id || "").trim();
    if (!channelId || !item) {
        showToast({ title: "保存失败", text: "请先选择一个通信方式。", kind: "error" });
        return;
    }
    if (!S.communicationDirty) {
        showToast({ title: "没有待保存修改", text: "当前通信配置没有未保存改动。", kind: "info", durationMs: 1800 });
        return;
    }
    let configPayload = {};
    try {
        configPayload = JSON.parse(S.communicationDraftText || "{}");
        if (!configPayload || typeof configPayload !== "object" || Array.isArray(configPayload)) {
            throw new Error("JSON 配置必须是对象");
        }
    } catch (e) {
        showToast({ title: "JSON 无法保存", text: e.message || "配置格式错误", kind: "error", durationMs: 2800 });
        return;
    }
    S.communicationBusy = true;
    renderCommunicationActions();
    showToast({ title: "保存中", text: `正在保存 ${item.label} 配置...`, kind: "info", persistent: true });
    try {
        await ApiClient.updateChinaChannel(channelId, {
            enabled: S.communicationDraftEnabled,
            config: configPayload,
        });
        showToast({ title: "测试连接中", text: `正在测试 ${item.label} 的当前连接状态...`, kind: "info", persistent: true });
        const probe = await ApiClient.testChinaChannel(channelId);
        await loadCommunications({ renderDetail: false });
        await openCommunication(channelId, true);
        const result = probe?.result || {};
        const message = [result.message, ...(Array.isArray(result.details) ? result.details : [])].filter(Boolean).join("；");
        showToast({
            title: result.title || "保存成功",
            text: message || `${item.label} 配置已更新`,
            kind: communicationToastKind(result.status),
            durationMs: result.status === "error" ? 3200 : 2600,
        });
    } catch (e) {
        showToast({ title: "保存失败", text: e.message || "Unknown error", kind: "error", durationMs: 2800 });
    } finally {
        S.communicationBusy = false;
        renderCommunicationActions();
    }
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
    const availabilityState = resourceAvailabilityStatus(S.selectedSkill);
    const unavailableReasons = resourceAvailabilityReasons(S.selectedSkill);
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
                <div class="resource-status-row" style="margin-bottom: var(--space-4);">
                    <span class="meta-tag status-${availabilityState}">${esc(displayEnabledLabel(S.selectedSkill.enabled, S.selectedSkill.available))}</span>
                    ${S.selectedSkill.enabled
                        ? `<button type="button" class="toolbar-btn danger" id="skill-disable-btn">禁用技能</button>`
                        : `<button type="button" class="toolbar-btn success" id="skill-enable-btn">启用技能</button>`}
                    <button type="button" class="toolbar-btn danger" id="skill-delete-btn">删除</button>
                </div>
                ${S.selectedSkill.available === false ? `
                    <div class="resource-warning-banner" role="status" aria-live="polite">
                        <div class="resource-warning-title">当前 Skill 不可用</div>
                        <ul class="resource-warning-list">
                            ${unavailableReasons.map((reason) => `<li>${esc(reason)}</li>`).join("")}
                        </ul>
                    </div>
                ` : ""}
                <div class="resource-draft-hint${S.skillDirty ? " is-dirty" : ""}" ${S.skillDirty ? "" : "hidden"}></div>
                <div class="resource-section">
                    <h3>角色可见性</h3>
                    <div class="resource-filter-row">
                        ${roles.map((role) => `
                            <label class="role-toggle ${allowedRoles.includes(role) ? "checked" : ""}">
                                <input type="checkbox" class="skill-role" data-role="${role}" ${allowedRoles.includes(role) ? "checked" : ""}>
                                <span>${esc(displayRoleLabel(role))}</span>
                            </label>
                        `).join("")}
                    </div>
                </div>
                <div class="resource-section">
                    <h3>文件内容</h3>
                    <div class="resource-filter-row">${fileTabs}</div>
                    <textarea id="skill-editor" rows="18" class="resource-editor">${editorValue}</textarea>
                </div>
            </div>
        </article>`;
    U.skillDetail.querySelector("#skill-modal-close")?.addEventListener("click", clearSkillSelection);
    U.skillDetail.querySelector("#skill-modal-save")?.addEventListener("click", () => void saveSkill());
    U.skillDetail.querySelector("#skill-enable-btn")?.addEventListener("click", () => {
        S.selectedSkill.enabled = true;
        setSkillDirty(true);
        renderSkillDetail();
    });
    U.skillDetail.querySelector("#skill-disable-btn")?.addEventListener("click", () => {
        S.selectedSkill.enabled = false;
        setSkillDirty(true);
        renderSkillDetail();
    });
    U.skillDetail.querySelector("#skill-delete-btn")?.addEventListener("click", () => void requestDeleteSkill());
    U.skillDetail.querySelectorAll(".skill-role").forEach((checkbox) => checkbox.addEventListener("change", (e) => {
        const nextRoles = new Set(allowedRoles);
        if (e.target.checked) nextRoles.add(e.target.dataset.role);
        else nextRoles.delete(e.target.dataset.role);
        S.selectedSkill.allowed_roles = [...nextRoles];
        setSkillDirty(true);
        renderSkillDetail();
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
        setSkillDirty(true);
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
            if (next) {
                S.selectedSkill = next;
                ensureSkillPageForItem(selectedId);
            }
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
        ensureSkillPageForItem(skillId);
        S.skillFiles = files;
        S.selectedSkillFile = files[0]?.file_key || "";
        S.skillContents = {};
        S.skillDirty = false;
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
    const selectedId = String(S.selectedSkill?.skill_id || "").trim();
    const displayName = String(S.selectedSkill?.display_name || selectedId || "Skill").trim();
    const enabled = !!S.selectedSkill?.enabled;
    const allowedRoles = Array.isArray(S.selectedSkill?.allowed_roles) ? [...S.selectedSkill.allowed_roles] : [];
    if (!selectedId || !S.selectedSkill) {
        addNotice({ kind: "resource_failed", title: "No skill selected", text: "Select a skill before saving." });
        showToast({ title: "保存失败", text: "未选择 Skill", kind: "error" });
        return;
    }
    if (!S.skillDirty) {
        showToast({ title: "No pending changes", text: "This skill has no unsaved changes.", kind: "info", durationMs: 1800 });
        return;
    }
    S.skillBusy = true;
    renderSkillActions();
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
        await openSkill(selectedId, true);
        setSkillDirty(false);
        addNotice({ kind: "resource_saved", title: "Skill saved", text: displayName || selectedId });
        showToast({ title: "保存成功", text: "Skill 配置已保存", kind: "success", durationMs: 2200 });
    } catch (e) {
        addNotice({ kind: "resource_failed", title: "Skill save failed", text: e.message || "Unknown error" });
        showToast({ title: "保存失败", text: e.message || "Unknown error", kind: "error", durationMs: 2600 });
    } finally {
        S.skillBusy = false;
        renderSkillActions();
    }
}

async function requestDeleteSkill() {
    const selectedId = String(S.selectedSkill?.skill_id || "").trim();
    const displayName = String(S.selectedSkill?.display_name || selectedId || "Skill").trim();
    if (!selectedId || S.skillBusy) return;
    const detail = S.skillDirty
        ? "确认删除该 Skill 并丢弃未保存的修改？相关文件和 catalog 条目也会一起移除。"
        : "确认删除该 Skill？相关文件和 catalog 条目也会一起移除。";
    openConfirm({
        title: "删除 Skill",
        text: detail,
        confirmLabel: "删除",
        confirmKind: "danger",
        returnFocus: U.skillRefresh,
        onConfirm: () => performDeleteSkill(selectedId, displayName),
    });
}

async function performDeleteSkill(skillId, displayName) {
    if (!skillId) return;
    S.skillBusy = true;
    renderSkillActions();
    showToast({ title: "正在删除", text: `正在移除 ${displayName || skillId}...`, kind: "info", persistent: true });
    try {
        await ApiClient.deleteSkill(skillId);
        clearSkillSelection();
        await loadSkills({ renderDetail: false });
        addNotice({ kind: "resource_saved", title: "Skill 已删除", text: displayName || skillId });
        showToast({ title: "已删除", text: `${displayName || skillId} 已移除。`, kind: "success", durationMs: 2200 });
    } catch (e) {
        const message = resourceDeleteErrorText(e);
        addNotice({ kind: "resource_failed", title: "删除 Skill 失败", text: message });
        showToast({ title: "删除失败", text: message, kind: "error", durationMs: 3200 });
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
    const actions = Array.isArray(S.selectedTool.actions) ? S.selectedTool.actions : [];
    const description = String(S.selectedTool.description || "").trim();
    const toolskillContent = String(S.selectedTool.toolskill_content || "").trim();
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
                <div class="resource-status-row" style="margin-bottom: var(--space-4);">
                    ${S.selectedTool.enabled
                        ? `<button type="button" class="toolbar-btn danger" id="tool-disable-btn">禁用工具族</button>`
                        : `<button type="button" class="toolbar-btn success" id="tool-enable-btn">启用工具族</button>`}
                </div>
                <div class="resource-section">
                    <h3>描述</h3>
                    <div class="resource-copy-block">${esc(description || "暂无描述。")}</div>
                </div>
                <div class="resource-draft-hint${S.toolDirty ? " is-dirty" : ""}" ${S.toolDirty ? "" : "hidden"}></div>
                <div class="resource-section">
                    <details class="resource-disclosure toolskill-disclosure">
                        <summary class="resource-disclosure-summary">
                            <span>工具技巧</span>
                            <span class="resource-disclosure-hint">点击展开</span>
                        </summary>
                        <div class="resource-disclosure-body">
                            ${toolskillContent
                                ? `<pre class="resource-preformatted toolskill-content">${esc(toolskillContent)}</pre>`
                                : `<div class="resource-empty-copy">暂无工具技巧说明。</div>`}
                        </div>
                    </details>
                </div>
                <div class="resource-section">
                    <div class="tool-permission-heading">
                        <h3>Action 权限</h3>
                        <p class="subtitle">控制当前工具族下各个 action 对主Agent、执行和检验角色的可见性。</p>
                    </div>
                    <div class="tool-permission-grid">
                        ${actions.length ? actions.map((action) => {
                            const actionName = esc(action.label || action.action_id);
                            const actionId = esc(action.action_id);
                            const riskLevel = esc(displayRiskLabel(action.risk_level || "medium"));
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
                                                <input type="checkbox" class="tool-role tool-role-input" data-action="${actionId}" data-role="${role}" aria-label="${actionName} - ${esc(displayRoleLabel(role))}" ${action.allowed_roles?.includes(role) ? "checked" : ""}>
                                                <span>${esc(displayRoleLabel(role))}</span>
                                            </label>
                                        `).join("")}
                                    </div>
                                </article>`;
                        }).join("") : `<div class="tool-empty-card">当前工具族没有 action。</div>`}
                    </div>
                </div>
            </div>
        </article>`;
    const toolStatusRow = U.toolDetail.querySelector(".resource-status-row");
    if (toolStatusRow && !toolStatusRow.querySelector("#tool-delete-btn")) {
        const deleteButton = document.createElement("button");
        deleteButton.type = "button";
        deleteButton.className = "toolbar-btn danger";
        deleteButton.id = "tool-delete-btn";
        deleteButton.textContent = "删除";
        toolStatusRow.appendChild(deleteButton);
    }
    U.toolDetail.querySelector("#tool-modal-close")?.addEventListener("click", clearToolSelection);
    U.toolDetail.querySelector("#tool-modal-save")?.addEventListener("click", () => void saveTool());
    U.toolDetail.querySelector("#tool-enable-btn")?.addEventListener("click", () => {
        S.selectedTool.enabled = true;
        setToolDirty(true);
        renderToolDetail();
    });
    U.toolDetail.querySelector("#tool-disable-btn")?.addEventListener("click", () => {
        S.selectedTool.enabled = false;
        setToolDirty(true);
        renderToolDetail();
    });
    U.toolDetail.querySelector("#tool-delete-btn")?.addEventListener("click", () => void requestDeleteTool());
    U.toolDetail.querySelectorAll(".tool-role").forEach((checkbox) => checkbox.addEventListener("change", (e) => {
        const action = S.selectedTool.actions.find((item) => item.action_id === e.target.dataset.action);
        if (!action) return;
        const set = new Set(action.allowed_roles || []);
        if (e.target.checked) set.add(e.target.dataset.role);
        else set.delete(e.target.dataset.role);
        action.allowed_roles = [...set];
        e.target.closest(".role-toggle")?.classList.toggle("checked", e.target.checked);
        setToolDirty(true);
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
            if (next) {
                S.selectedTool = {
                    ...next,
                    primary_executor_name: S.selectedTool?.primary_executor_name || next.primary_executor_name || "",
                    toolskill_content: S.selectedTool?.toolskill_content || "",
                };
                ensureToolPageForItem(selectedId);
            }
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
        const [tool, toolskill] = await Promise.all([
            ApiClient.getTool(toolId),
            ApiClient.getToolSkill(toolId).catch(() => ({ content: "", primary_executor_name: "" })),
        ]);
        S.selectedTool = {
            ...tool,
            primary_executor_name: toolskill?.primary_executor_name || tool?.primary_executor_name || "",
            toolskill_content: toolskill?.content || "",
        };
        ensureToolPageForItem(toolId);
        S.toolDirty = false;
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
    const selectedId = String(S.selectedTool?.tool_id || "").trim();
    const displayName = String(S.selectedTool?.display_name || selectedId || "Tool").trim();
    const enabled = !!S.selectedTool?.enabled;
    const actions = Array.isArray(S.selectedTool?.actions)
        ? Object.fromEntries(
            S.selectedTool.actions.map((action) => [
                action.action_id,
                Array.isArray(action.allowed_roles) ? [...action.allowed_roles] : [],
            ])
        )
        : {};
    if (!selectedId || !S.selectedTool) {
        addNotice({ kind: "resource_failed", title: "No tool selected", text: "Select a tool before saving." });
        showToast({ title: "保存失败", text: "未选择工具族", kind: "error" });
        return;
    }
    if (!S.toolDirty) {
        showToast({ title: "No pending changes", text: "This tool has no unsaved changes.", kind: "info", durationMs: 1800 });
        return;
    }
    S.toolBusy = true;
    renderToolActions();
    showToast({ title: "保存中", text: "正在保存工具权限，请稍候…", kind: "info", persistent: true });
    try {
        await ApiClient.updateToolPolicy(selectedId, {
            enabled,
            actions,
        });
        await ApiClient.reloadResources();
        await loadTools({ renderDetail: false });
        await openTool(selectedId, true);
        setToolDirty(false);
        addNotice({ kind: "resource_saved", title: "Tool saved", text: displayName || selectedId });
        showToast({ title: "保存成功", text: "工具权限已保存", kind: "success", durationMs: 2200 });
    } catch (e) {
        addNotice({ kind: "resource_failed", title: "Tool save failed", text: e.message || "Unknown error" });
        showToast({ title: "保存失败", text: e.message || "Unknown error", kind: "error", durationMs: 2600 });
    } finally {
        S.toolBusy = false;
        renderToolActions();
    }
}

function requestDeleteTool() {
    const selectedId = String(S.selectedTool?.tool_id || "").trim();
    const displayName = String(S.selectedTool?.display_name || selectedId || "Tool").trim();
    if (!selectedId || S.toolBusy) return;
    const detail = S.toolDirty
        ? "确认删除该工具并丢弃未保存的权限修改？相关文件和 catalog 条目也会一起移除。"
        : "确认删除该工具？相关文件和 catalog 条目也会一起移除。";
    openConfirm({
        title: "删除工具",
        text: detail,
        confirmLabel: "删除",
        confirmKind: "danger",
        returnFocus: U.toolRefresh,
        onConfirm: () => performDeleteTool(selectedId, displayName),
    });
}

async function performDeleteTool(toolId, displayName) {
    if (!toolId) return;
    S.toolBusy = true;
    renderToolActions();
    showToast({ title: "正在删除", text: `正在移除 ${displayName || toolId}...`, kind: "info", persistent: true });
    try {
        await ApiClient.deleteTool(toolId);
        clearToolSelection();
        await loadTools({ renderDetail: false });
        addNotice({ kind: "resource_saved", title: "工具已删除", text: displayName || toolId });
        showToast({ title: "已删除", text: `${displayName || toolId} 已移除。`, kind: "success", durationMs: 2200 });
    } catch (e) {
        const message = resourceDeleteErrorText(e);
        addNotice({ kind: "resource_failed", title: "删除工具失败", text: message });
        showToast({ title: "删除失败", text: message, kind: "error", durationMs: 3200 });
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
    const map = { ceo: U.viewCeo, tasks: U.viewTasks, skills: U.viewSkills, tools: U.viewTools, models: U.viewModels, communications: U.viewCommunications, "task-details": U.viewTaskDetails };
    const navView = view === "task-details" ? "tasks" : view;
    S.view = navView;
    U.nav.forEach((btn) => btn.classList.toggle("active", btn.dataset.view === navView));
    Object.entries(map).forEach(([key, el]) => {
        if (!el) return;
        const active = key === view;
        el.classList.toggle("active", active);
        el.style.display = active ? "" : "none";
    });
    if (view !== "task-details") {
        setTaskTokenStatsOpen(false);
        clearAgentSelection({ rerender: false });
        if (S.taskWs) {
            S.taskWs.close();
            S.taskWs = null;
        }
    }
    if (view === "tasks") void loadTasks();
    if (view === "skills") void loadSkills();
    if (view === "tools") void loadTools();
    if (view === "models") void loadModels();
    if (view === "communications") void loadCommunications();
}

function bind() {
    U.theme?.addEventListener("click", toggleTheme);
    U.nav.forEach((btn) => btn.addEventListener("click", () => switchView(btn.dataset.view)));
    U.backToTasks?.addEventListener("click", () => switchView("tasks"));
    U.taskTokenButton?.addEventListener("click", () => setTaskTokenStatsOpen(true));
    U.taskTokenClose?.addEventListener("click", () => setTaskTokenStatsOpen(false));
    U.taskTokenBackdrop?.addEventListener("click", () => setTaskTokenStatsOpen(false));
    U.artifactApply?.addEventListener("click", () => void applySelectedArtifact());
    U.ceoNewSession?.addEventListener("click", () => void createNewCeoSession());
    U.renameSessionCancel?.addEventListener("click", handleRenameCancel);
    U.renameSessionAccept?.addEventListener("click", handleRenameAccept);
    U.renameSessionInput?.addEventListener("keydown", (e) => {
        if (e.key === "Enter") handleRenameAccept();
        if (e.key === "Escape") handleRenameCancel();
    });
    U.ceoSessionList?.addEventListener("click", (e) => {
        const activate = e.target.closest("[data-session-activate]");
        if (activate) {
            void activateCeoSession(activate.dataset.sessionActivate);
            return;
        }
        const rename = e.target.closest("[data-session-rename]");
        if (rename) {
            e.stopPropagation();
            void renameCeoSession(rename.dataset.sessionRename);
            return;
        }
        const remove = e.target.closest("[data-session-delete]");
        if (remove) {
            e.stopPropagation();
            requestDeleteCeoSession(remove.dataset.sessionDelete);
        }
    });
    U.ceoSend?.addEventListener("click", handleCeoPrimaryAction);
    U.ceoAttach?.addEventListener("click", () => {
        if (S.ceoUploadBusy) return;
        U.ceoFileInput?.click();
    });
    U.ceoFileInput?.addEventListener("change", (e) => void handleCeoFileSelection(e));
    U.ceoUploadList?.addEventListener("click", (e) => {
        const remove = e.target.closest("[data-upload-remove]");
        if (!remove) return;
        removePendingCeoUpload(Number(remove.dataset.uploadRemove));
    });
    U.ceoInput?.addEventListener("keydown", (e) => {
        if (e.key === "Enter" && !e.shiftKey) {
            e.preventDefault();
            handleCeoPrimaryAction();
        }
    });
    U.ceoInput?.addEventListener("input", () => {
        syncCeoInputHeight();
        syncCeoPrimaryButton();
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
    U.modelRoleEditors?.addEventListener("input", (e) => {
        if (!S.modelCatalog.roleEditing) return;
        const field = e.target.closest("[data-model-role-iterations]");
        if (!(field instanceof HTMLInputElement)) return;
        syncRoleIterationDraftsFromInputs({ requireValid: false });
    });
    U.modelRoleEditors?.addEventListener("change", (e) => {
        if (!S.modelCatalog.roleEditing) return;
        const field = e.target.closest("[data-model-role-iterations]");
        if (!(field instanceof HTMLInputElement)) return;
        try {
            syncRoleIterationDraftsFromInputs({ requireValid: true });
            renderModelCatalog();
        } catch (error) {
            S.modelCatalog.error = error.message || "save failed";
            hint(`妯″瀷閰嶇疆閿欒锛?{S.modelCatalog.error}`, true);
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
        const chainList = resolveModelChainDropList(e.target);
        if (!chainList) return;
        const scope = String(chainList.dataset.modelChainList || "");
        const allowDrop = dragState.source === "available" || scope === dragState.scope;
        if (!scope || !allowDrop) return;
        e.preventDefault();
        e.dataTransfer.dropEffect = dragState.source === "chain" ? "move" : "copy";
        clearModelDragDecorations();
        let targetItem = e.target.closest("[data-model-chain-ref]");
        if (!(targetItem instanceof Element) || targetItem.parentElement !== chainList) {
            targetItem = null;
        }
        if (targetItem && dragState.source === "chain" && scope === dragState.scope && String(targetItem.dataset.modelChainRef || "") === dragState.ref) {
            targetItem = null;
        }
        if (!targetItem) targetItem = resolveModelChainDropTarget(chainList, e.clientY, dragState);
        ensureModelDropPlaceholder(chainList, targetItem, e.clientY);
        startModelAutoScroll(chainList, e.clientY);
    });
    U.modelRoleEditors?.addEventListener("drop", (e) => {
        if (!S.modelCatalog.roleEditing) return;
        const dragState = S.modelCatalog.dragState;
        if (!dragState?.ref) return;
        const chainList = resolveModelChainDropList(e.target);
        if (!chainList) return;
        const scope = String(chainList.dataset.modelChainList || "");
        const allowDrop = dragState.source === "available" || scope === dragState.scope;
        if (!scope || !allowDrop) return;
        e.preventDefault();
        const placeholder = chainList.querySelector('[data-model-drop-placeholder]');
        const children = [...chainList.children];
        const placeholderIndex = children.indexOf(placeholder);
        const targetIndex = placeholderIndex < 0
            ? resolveModelChainDropIndex(chainList, dragState, e.clientY)
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
        const zone = e.target instanceof Element ? (e.target.closest(".model-chain-card") || resolveModelChainDropList(e.target)) : null;
        if (!zone) return;
        if (!didModelDragLeaveZone(zone, e)) return;
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
        const zone = e.target instanceof Element ? e.target.closest("[data-model-available-list]") : null;
        if (!zone) return;
        if (!didModelDragLeaveZone(zone, e)) return;
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
        if (cancel) {
            clearModelSelection();
            return;
        }
        const controlBtn = e.target.closest("[data-model-control]");
        if (controlBtn) {
            const action = controlBtn.dataset.modelControl;
            if (action === "delete") {
                void deleteModelDetail(controlBtn.dataset.key);
                return;
            }
            const checkbox = U.modelDetail.querySelector('input[name="enabled"]');
            if (checkbox) {
                checkbox.checked = action === "enable";
                saveModelDetail();
            }
        }
    });
    U.modelDetail?.addEventListener("change", (e) => {
        const toggle = e.target.closest(".role-toggle");
        if (toggle && e.target instanceof HTMLInputElement && e.target.type === "checkbox") {
            toggle.classList.toggle("checked", e.target.checked);
        }
    });
    U.taskDepthSelect?.addEventListener("change", (e) => {
        void saveTaskDefaultMaxDepth(e.target.value);
    });
    U.taskPageSize?.addEventListener("change", (e) => setTaskPageSize(e.target.value));
    U.taskPagePrev?.addEventListener("click", () => setTaskPage(S.taskPage - 1));
    U.taskPageNext?.addEventListener("click", () => setTaskPage(S.taskPage + 1));
    U.taskMultiToggle?.addEventListener("click", () => setMultiSelectMode(!S.multiSelectMode));
    U.taskFilterTrigger?.addEventListener("click", (e) => {
        e.stopPropagation();
        setTaskMenuOpen("filter", !S.taskFilterMenuOpen);
    });
    U.taskBatchTrigger?.addEventListener("click", (e) => {
        e.stopPropagation();
        setTaskMenuOpen("batch", !S.taskBatchMenuOpen);
    });
    if (U.taskBatchMenu && !U.taskBatchMenu.querySelector('[data-batch-action="retry"]')) {
        const retryButton = document.createElement("button");
        retryButton.className = "toolbar-menu-item success";
        retryButton.type = "button";
        retryButton.setAttribute("role", "menuitem");
        retryButton.dataset.batchAction = "retry";
        retryButton.textContent = "重试";
        const deleteButton = U.taskBatchMenu.querySelector('[data-batch-action="delete"]');
        if (deleteButton) U.taskBatchMenu.insertBefore(retryButton, deleteButton);
        else U.taskBatchMenu.appendChild(retryButton);
    }
    U.taskFilterMenu?.querySelectorAll("[data-select-bucket]")?.forEach((button) => button.addEventListener("click", () => {
        S.selectedTaskIds = new Set(S.tasks.filter((task) => statusBucketMatches(task, button.dataset.selectBucket)).map((task) => task.task_id));
        closeTaskMenus();
        renderTasks();
    }));
    U.taskBatchMenu?.querySelectorAll("[data-batch-action]")?.forEach((button) => button.addEventListener("click", async () => {
        await runTaskBatchAction(button.dataset.batchAction, { returnFocus: button });
    }));
    U.closeAgent?.addEventListener("click", () => clearAgentSelection());
    U.taskDetailBackdrop?.addEventListener("click", () => clearAgentSelection());
    [U.skillSearch, U.skillRisk, U.skillStatus].forEach((el) => el?.addEventListener(el.tagName === "INPUT" ? "input" : "change", resetSkillPagination));
    U.skillPageSize?.addEventListener("change", (e) => setSkillPageSize(e.target.value));
    U.skillPagePrev?.addEventListener("click", () => setSkillPage(S.skillPage - 1));
    U.skillPageNext?.addEventListener("click", () => setSkillPage(S.skillPage + 1));
    U.skillRefresh?.addEventListener("click", () => void refreshSkills());
    U.skillSave?.addEventListener("click", () => void saveSkill());
    [U.toolSearch, U.toolStatus, U.toolRisk].forEach((el) => el?.addEventListener(el.tagName === "INPUT" ? "input" : "change", resetToolPagination));
    U.toolPageSize?.addEventListener("change", (e) => setToolPageSize(e.target.value));
    U.toolPagePrev?.addEventListener("click", () => setToolPage(S.toolPage - 1));
    U.toolPageNext?.addEventListener("click", () => setToolPage(S.toolPage + 1));
    U.toolRefresh?.addEventListener("click", () => void refreshTools());
    U.toolSave?.addEventListener("click", () => void saveTool());
    U.communicationRefresh?.addEventListener("click", () => void refreshCommunications());
    U.modelBackdrop?.addEventListener("click", clearModelSelection);
    U.skillBackdrop?.addEventListener("click", clearSkillSelection);
    U.toolBackdrop?.addEventListener("click", clearToolSelection);
    U.communicationBackdrop?.addEventListener("click", clearCommunicationSelection);
    U.toastClose?.addEventListener("click", closeToast);
    U.confirmBackdrop?.addEventListener("click", (e) => {
        if (e.target === U.confirmBackdrop) closeConfirm();
    });
    U.confirmCancel?.addEventListener("click", () => closeConfirm());
    U.confirmAccept?.addEventListener("click", () => void acceptConfirm());
    document.addEventListener("click", (e) => {
        if (!(e.target instanceof Element)) return;
        if (!e.target.closest(".resource-select-shell")) closeResourceSelects();
        if (!e.target.closest(".toolbar-dropdown")) closeTaskMenus();
    });
    document.addEventListener("keydown", (e) => {
        if (e.key !== "Escape") return;
        if (closeResourceSelects({ restoreFocus: true })) return;
        if (S.confirmState) {
            closeConfirm();
            return;
        }
        if (S.taskFilterMenuOpen || S.taskBatchMenuOpen) {
            closeTaskMenus();
            return;
        }
        if (S.taskTokenStatsOpen) {
            setTaskTokenStatsOpen(false);
            return;
        }
        if (U.taskDetailDrawer?.classList.contains("is-open")) {
            clearAgentSelection();
            return;
        }
        if (S.modelCatalog.mode === "create" || S.modelCatalog.selectedModelKey) {
            clearModelSelection();
            return;
        }
        if (S.selectedSkill) clearSkillSelection();
        if (S.selectedTool) clearToolSelection();
        if (S.selectedCommunication) clearCommunicationSelection();
    });
    document.addEventListener("visibilitychange", () => {
        if (!document.hidden) void pollCeoSessions();
    });
    renderPendingCeoUploads();
    syncCeoInputHeight();
    renderCeoSessions();
    renderTaskSessionScope();
    syncCeoPrimaryButton();
}

function init() {
    ensureTaskTokenUi();
    enhanceResourceSelects();
    configureTaskDetailSections();
    bind();
    startCeoSessionPolling();
    startLiveDurationTicker();
    window.addEventListener("beforeunload", stopCeoSessionPolling);
    window.addEventListener("beforeunload", stopLiveDurationTicker);
    window.addEventListener("resize", refreshTaskDetailScrollRegions);
    bindTreePan();
    icons();
    renderTaskDepthControl();
    void loadTaskDefaults();
    renderSkillActions();
    renderToolActions();
    renderCommunicationActions();
    void loadModels();
    void loadTasks();
    void refreshCeoSessions({ reconnect: true }).catch((error) => {
        showToast({ title: "会话加载失败", text: error.message || "Unknown error", kind: "error" });
    });
}

document.addEventListener("DOMContentLoaded", init);







