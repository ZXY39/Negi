const MODEL_SCOPES = [
    { key: "ceo", label: "主Agent" },
    { key: "execution", label: "执行" },
    { key: "inspection", label: "检验" },
];

const EMPTY_MODEL_ROLES = () => ({ ceo: [], execution: [], inspection: [] });
const DEFAULT_MODEL_DEFAULTS = () => ({ ceo: "", execution: "", inspection: "" });
const DEFAULT_ROLE_ITERATIONS = () => ({ ceo: null, execution: null, inspection: null });
const DEFAULT_ROLE_CONCURRENCY = () => ({ ceo: null, execution: null, inspection: null });
const TREE_SCALE_MIN = 0.12;
const TREE_SCALE_MAX = 3.5;
const TREE_SCALE_FACTOR = 1.12;
const RESOURCE_PAGE_SIZES = [20, 50, 100];
const TASK_MODEL_CALLS_PAGE_SIZE = 100;
const TASK_DEPTH_PRESET_VALUES = Object.freeze([1, 2, 3, 4, 5]);
const TASK_DEPTH_PRESET_MAX = TASK_DEPTH_PRESET_VALUES[TASK_DEPTH_PRESET_VALUES.length - 1];
const TASK_DEPTH_CUSTOM_VALUE = "__custom__";
const CEO_TOOL_OUTPUT_PREVIEW_LINES = 2;
const CEO_TOOL_OUTPUT_PREVIEW_MAX_CHARS = 240;
const CEO_TOOL_PROGRESS_MAX_LINES = 4;
const CEO_TOOL_STEP_MAX = 5;
const TASK_DETAIL_SESSION_KEY = "g3ku.task-detail.session.v1";
const CEO_SESSION_SNAPSHOT_CACHE_KEY = "g3ku.ceo.session-snapshots.v2";
const CEO_SESSION_SNAPSHOT_CACHE_LIMIT = 6;
const CEO_SESSION_SNAPSHOT_MESSAGE_LIMIT = 24;
const CEO_SESSION_SNAPSHOT_TOOL_EVENT_LIMIT = 12;
const CEO_COMPRESSION_TOAST_TEXT = "上下文压缩中";
const CEO_COMPOSER_DRAFT_CACHE_KEY = "g3ku.ceo.composer-drafts.v1";
const CEO_COMPOSER_DRAFT_CACHE_LIMIT = 24;
const CEO_FOLLOW_UP_QUEUE_CACHE_KEY = "g3ku.ceo.follow-up-queues.v1";
const CEO_FOLLOW_UP_QUEUE_CACHE_LIMIT = 24;
const CEO_FOLLOW_UP_QUEUE_PER_SESSION_LIMIT = 20;
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
        const raw = iterations?.[key];
        if (raw == null || String(raw).trim() === "") {
            next[key] = defaults[key];
            return;
        }
        const value = Number(raw);
        next[key] = Number.isInteger(value) && value >= 0 ? value : defaults[key];
    });
    return next;
};
const cloneRoleConcurrency = (concurrency = DEFAULT_ROLE_CONCURRENCY()) => {
    const defaults = DEFAULT_ROLE_CONCURRENCY();
    const next = DEFAULT_ROLE_CONCURRENCY();
    MODEL_SCOPES.forEach(({ key }) => {
        const raw = concurrency?.[key];
        if (raw == null || String(raw).trim() === "") {
            next[key] = defaults[key];
            return;
        }
        const value = Number(raw);
        next[key] = Number.isInteger(value) && value >= 0 ? value : defaults[key];
    });
    return next;
};

const S = {
    view: "ceo",
    ceoWs: null,
    ceoWsToken: 0,
    ceoWsLastErrorCode: "",
    ceoPendingTurns: [],
    ceoTurnActive: false,
    ceoPauseBusy: false,
    ceoUploads: [],
    ceoUploadBusy: false,
    ceoSessions: [],
    ceoLocalSessions: [],
    ceoChannelGroups: [],
    ceoSessionTab: "local",
    ceoSessionPanelExpanded: false,
    activeSessionFamily: "local",
    ceoSessionUnread: {},
    ceoSessionMessageCounts: {},
    ceoSessionHydrated: false,
    ceoBulkMode: false,
    ceoSelectedSessionIds: new Set(),
    ceoScrollToLatestOnSnapshot: false,
    ceoSnapshotCache: {},
    ceoSnapshotPersistId: null,
    ceoComposerDrafts: {},
    ceoComposerDraftPersistId: null,
    ceoQueuedFollowUps: {},
    ceoQueuedFollowUpsPersistId: null,
    ceoQueuedFollowUpDispatching: false,
    liveDurationIntervalId: null,
    activeSessionId: "",
    ceoSessionBusy: false,
    ceoSessionCatalogBusy: false,
    ceoSessionSwitchToken: 0,
    taskDefaults: {
        scope: "global",
        maxDepth: 1,
        defaultMaxDepth: 1,
        hardMaxDepth: 4,
        customMode: false,
        customDraft: "",
        loading: false,
        saving: false,
        requestToken: 0,
    },
    taskWs: null,
    tasksWs: null,
    currentTaskId: null,
    tasks: [],
    currentTask: null,
    taskSummary: null,
    taskGovernance: null,
    taskGovernanceExpanded: false,
    rootNode: null,
    frontier: [],
    recentModelCalls: [],
    liveFrameMap: {},
    currentNodeDetail: null,
    taskDetailRenderToken: 0,
    taskNodeDetails: {},
    taskNodePatchSummaries: {},
    taskNodeDetailRequests: {},
    taskNodeLatestContexts: {},
    taskNodeLatestContextRequests: {},
    treeRootNodeId: "",
    treeNodesById: {},
    treeSnapshotVersion: "",
    treeDirtyParentsById: {},
    treeBranchSyncInFlightById: {},
    treeBranchSyncQueuedById: {},
    treeBranchSyncTokenById: {},
    treeLargeMode: false,
    taskDetailViewStates: {},
    pendingTaskDetailRestore: null,
    taskNodeBusy: false,
    tasksWorkerOnline: true,
    tasksWorkerReportedOnline: true,
    tasksWorkerState: "online",
    tasksWorkerReportedState: "online",
    tasksWorkerLastSeenAt: "",
    tasksWorkerControlAvailable: true,
    tasksWorker: null,
    tasksWorkerStatusPayload: null,
    tasksWorkerStaleAfterSeconds: 15,
    taskWorkerStatusPollId: null,
    taskPerformanceRefreshId: null,
    taskTokenStatsOpen: false,
    taskModelCallsPage: 1,
    taskModelCallsPageSize: TASK_MODEL_CALLS_PAGE_SIZE,
    taskArtifacts: [],
    selectedArtifactId: "",
    artifactContent: "",
    traceOutputContentByKey: {},
    traceOutputRequestsByKey: {},
    selectedTaskIds: new Set(),
    multiSelectMode: false,
    taskFilterMenuOpen: false,
    taskBatchMenuOpen: false,
    taskBusy: false,
    taskPage: 1,
    taskPageSize: RESOURCE_PAGE_SIZES[0],
    tasksById: {},
    orderedTaskIds: [],
    visibleTaskIds: [],
    pendingTaskCardPatchIds: new Set(),
    taskCardPatchQueuedAt: {},
    taskCardPatchFlushId: null,
    taskListDirtyWhileHidden: false,
    taskListReconcileBusy: false,
    lastTaskSummaryPatchAt: "",
    lastTaskTokenPatchAt: "",
    taskListReconnectNeedsReconcile: false,
    taskGridSignature: "",
    taskMetricSnapshot: {},
    taskMetricAnimationTaskIds: new Set(),
    taskHallStats: {
        task_hall_full_render_count: 0,
        task_hall_card_patch_count: 0,
        task_hall_hidden_defer_count: 0,
        task_hall_max_patch_queue_age_ms: 0,
    },
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
        roleConcurrency: DEFAULT_ROLE_CONCURRENCY(),
        roleConcurrencyDrafts: DEFAULT_ROLE_CONCURRENCY(),
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
    treeView: null,
    treeSelectedRoundByNodeId: {},
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
    skillFileLoads: {},
    selectedSkillFile: "",
    skillBusy: false,
    skillDirty: false,
    skillAutosaveTimerId: null,
    skillAutosavePending: false,
    skillPage: 1,
    skillPageSize: RESOURCE_PAGE_SIZES[0],
    tools: [],
    selectedTool: null,
    toolBusy: false,
    toolDirty: false,
    toolAutosaveTimerId: null,
    toolAutosavePending: false,
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
    ceoShell: document.getElementById("ceo-shell"),
    ceoSessionPanel: document.getElementById("ceo-session-panel"),
    ceoSessionPanelToggle: document.getElementById("ceo-session-panel-toggle"),
    ceoSessionTabs: document.getElementById("ceo-session-tabs"),
    ceoSessionTabLocal: document.getElementById("ceo-session-tab-local"),
    ceoSessionTabChannel: document.getElementById("ceo-session-tab-channel"),
    ceoSessionBulkToggle: document.getElementById("ceo-session-bulk-toggle"),
    ceoSessionList: document.getElementById("ceo-session-list"),
    ceoSessionBulkActions: document.getElementById("ceo-session-bulk-actions"),
    ceoSessionBulkDelete: document.getElementById("ceo-session-bulk-delete"),
    ceoSessionBulkSelectAll: document.getElementById("ceo-session-bulk-select-all"),
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
    ceoFollowUpQueue: document.getElementById("ceo-follow-up-queue"),
    ceoCompressionToast: document.getElementById("ceo-compression-toast"),
    ceoCompressionToastText: document.getElementById("ceo-compression-toast-text"),
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
    taskPerformanceBar: document.getElementById("task-performance-bar"),
    taskToolbar: document.getElementById("task-toolbar"),
    taskDepthSelect: document.getElementById("task-depth-select"),
    taskDepthCustomWrap: document.getElementById("task-depth-custom-wrap"),
    taskDepthCustomInput: document.getElementById("task-depth-custom-input"),
    taskDepthCustomSave: document.getElementById("task-depth-custom-save"),
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
    tdTitle: document.getElementById("td-prompt-text"),
    tdPromptDisclosure: document.getElementById("td-prompt-disclosure"),
    tdStatusPill: document.getElementById("td-status-pill"),
    tdStatus: document.getElementById("td-status"),
    tdActiveCount: document.getElementById("td-active-count"),
    taskTreeResetRounds: document.getElementById("task-tree-reset-rounds-btn"),
    taskGovernancePanel: document.getElementById("task-governance-panel"),
    taskGovernanceToggle: document.getElementById("task-governance-toggle"),
    taskGovernanceSummary: document.getElementById("task-governance-summary"),
    taskGovernanceCount: document.getElementById("task-governance-count"),
    taskGovernanceHistory: document.getElementById("task-governance-history"),
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
    nodeContextDisclosure: document.getElementById("node-context-disclosure"),
    feedTitle: document.getElementById("feed-target-name"),
    detail: document.getElementById("agent-detail-view"),
    adRole: document.getElementById("ad-role"),
    adStatus: document.getElementById("ad-status"),
    adRoundSummary: document.getElementById("ad-round-summary"),
    adFlow: document.getElementById("ad-input"),
    adSpawnReviews: document.getElementById("ad-spawn-reviews"),
    adOutput: document.getElementById("ad-output"),
    adAcceptance: document.getElementById("ad-check"),
    adFlowHeading: document.getElementById("ad-input")?.closest(".agent-detail-section")?.querySelector("h4"),
    adSpawnReviewsHeading: document.getElementById("ad-spawn-reviews")?.closest(".agent-detail-section")?.querySelector("h4"),
    adOutputHeading: document.getElementById("ad-output")?.closest(".agent-detail-section")?.querySelector("h4"),
    adAcceptanceHeading: document.getElementById("ad-check")?.closest(".agent-detail-section")?.querySelector("h4"),
    artifactHeading: document.getElementById("artifact-list")?.closest(".agent-detail-section")?.querySelector("h4"),
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
    confirmCheckboxDetails: document.getElementById("confirm-checkbox-details"),
    confirmCancel: document.getElementById("confirm-cancel"),
    confirmAccept: document.getElementById("confirm-accept"),
    projectExit: document.getElementById("project-exit-btn"),
};

const esc = (v) => String(v ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;").replaceAll('"', "&quot;").replaceAll("'", "&#39;");
let __iconsRafId = 0;
const icons = (force = false) => {
    if (!window.lucide || typeof lucide.createIcons !== "function") return;
    if (force) {
        if (__iconsRafId) window.cancelAnimationFrame(__iconsRafId);
        __iconsRafId = 0;
        lucide.createIcons();
        return;
    }
    if (__iconsRafId) return;
    __iconsRafId = window.requestAnimationFrame(() => {
        __iconsRafId = 0;
        if (!window.lucide || typeof lucide.createIcons !== "function") return;
        lucide.createIcons();
    });
};
const roleKey = (v) => (["ceo", "inspection", "checker"].includes(String(v).toLowerCase()) ? (String(v).toLowerCase() === "ceo" ? "ceo" : "inspection") : "execution");
const roleLabel = (v) => ({ ceo: "主Agent", execution: "执行", inspection: "检验" }[roleKey(v)]);
const pStatus = (v) => String(v || "").trim().toLowerCase();
const MD_TOKEN_MARKER = "\uE000";
const clamp = (value, min, max) => Math.max(min, Math.min(max, value));
const activeSessionId = () => String(S.activeSessionId || ApiClient.getActiveSessionId()).trim() || ApiClient.getActiveSessionId();

function activeSessionItem() {
    const key = activeSessionId();
    return (S.ceoSessions || []).find((item) => String(item?.session_id || "").trim() === key) || null;
}

function isChannelSessionItem(item) {
    return String(item?.session_family || "").trim() === "channel" || String(item?.session_origin || "").trim() === "china";
}

function activeSessionIsReadonly() {
    return !!activeSessionItem()?.is_readonly;
}

function displayChinaChannelLabel(channelId) {
    return ({
        qqbot: "QQ Bot",
        dingtalk: "DingTalk",
        wecom: "????",
        "wecom-app": "??????",
        "wecom-kf": "??????",
        "wechat-mp": "?????",
        "feishu-china": "??",
    }[String(channelId || "").trim()] || String(channelId || "??").trim() || "??");
}

function flattenChannelGroups(groups = []) {
    const rows = [];
    (Array.isArray(groups) ? groups : []).forEach((group) => {
        const items = Array.isArray(group?.items) ? group.items : [];
        items.forEach((item) => rows.push(item));
    });
    return rows;
}

function visibleCeoSessions() {
    if (S.ceoSessionTab === "channel") return flattenChannelGroups(S.ceoChannelGroups);
    return Array.isArray(S.ceoLocalSessions) ? S.ceoLocalSessions : [];
}

function clearCeoBulkSelection() {
    S.ceoSelectedSessionIds = new Set();
}

function visibleCeoBulkSelectableSessionIds() {
    return visibleCeoSessions()
        .map((item) => String(item?.session_id || "").trim())
        .filter(Boolean);
}

function isCeoBulkSessionSelected(sessionId) {
    const key = String(sessionId || "").trim();
    return !!key && S.ceoSelectedSessionIds instanceof Set && S.ceoSelectedSessionIds.has(key);
}

function toggleCeoBulkMode(force = null) {
    const next = force == null ? !S.ceoBulkMode : !!force;
    if (next && !S.ceoSessionPanelExpanded) return false;
    if (S.ceoBulkMode === next) return next;
    S.ceoBulkMode = next;
    closeCeoSessionMenus();
    if (!next) clearCeoBulkSelection();
    renderCeoSessions();
    syncCeoSessionActions();
    return next;
}

function toggleCeoBulkSessionSelection(sessionId) {
    const key = String(sessionId || "").trim();
    if (!key) return false;
    const next = new Set(S.ceoSelectedSessionIds instanceof Set ? [...S.ceoSelectedSessionIds] : []);
    if (next.has(key)) next.delete(key);
    else next.add(key);
    S.ceoSelectedSessionIds = next;
    return next.has(key);
}

function areAllVisibleCeoBulkSessionsSelected() {
    const ids = visibleCeoBulkSelectableSessionIds();
    return ids.length > 0 && ids.every((sessionId) => isCeoBulkSessionSelected(sessionId));
}

function toggleCeoBulkSelectAll() {
    const ids = visibleCeoBulkSelectableSessionIds();
    const allSelected = ids.length > 0 && ids.every((sessionId) => isCeoBulkSessionSelected(sessionId));
    S.ceoSelectedSessionIds = allSelected ? new Set() : new Set(ids);
    return S.ceoSelectedSessionIds.size;
}

function rebuildCeoSessionIndex() {
    S.ceoSessions = [...(Array.isArray(S.ceoLocalSessions) ? S.ceoLocalSessions : []), ...flattenChannelGroups(S.ceoChannelGroups)];
}

function ceoSessionCreatedTime(session) {
    const item = session && typeof session === "object" ? session : {};
    return String(item.created_at || item.updated_at || "").trim();
}

function sortCeoSessionsByTime(items = []) {
    return [...(Array.isArray(items) ? items : [])].sort((left, right) => {
        const leftTime = String(ceoSessionCreatedTime(left) || "");
        const rightTime = String(ceoSessionCreatedTime(right) || "");
        if (leftTime !== rightTime) return rightTime.localeCompare(leftTime);
        return String(right?.session_id || "").localeCompare(String(left?.session_id || ""));
    });
}

function sortChannelGroupItems(items = []) {
    const typeOrder = { dm: 0, group: 1, thread: 2 };
    return [...(Array.isArray(items) ? items : [])].sort((left, right) => {
        const leftType = String(left?.chat_type || "dm").trim();
        const rightType = String(right?.chat_type || "dm").trim();
        const typeDiff = (typeOrder[leftType] ?? 9) - (typeOrder[rightType] ?? 9);
        if (typeDiff !== 0) return typeDiff;
        const leftTime = String(ceoSessionDisplayTime(left) || "");
        const rightTime = String(ceoSessionDisplayTime(right) || "");
        if (leftTime !== rightTime) return rightTime.localeCompare(leftTime);
        return String(left?.session_id || "").localeCompare(String(right?.session_id || ""));
    });
}

function normalizeCeoChannelGroups(groups = []) {
    return (Array.isArray(groups) ? groups : []).map((group) => ({
        ...group,
        items: sortChannelGroupItems(group?.items || []),
    }));
}

function ceoSessionGlyph(item = {}) {
    const title = String(item?.title || item?.channel_id || item?.session_id || "").trim();
    const compactChars = [...title].filter((ch) => String(ch || "").trim() && !/^[()[\]{}<>《》【】'"`~!@#$%^&*_=+|\\/:;,.?-]$/.test(ch));
    if (compactChars.length) {
        const cjkChars = compactChars.filter((ch) => /[\u3400-\u9fff]/.test(ch));
        if (cjkChars.length) return cjkChars.slice(0, 2).join("");
        const asciiTokens = title
            .split(/[\s_.\-/:|]+/)
            .map((token) => token.replace(/[^A-Za-z0-9]/g, ""))
            .filter(Boolean);
        if (asciiTokens.length >= 2) return `${asciiTokens[0][0]}${asciiTokens[1][0]}`.toUpperCase();
        const asciiChars = compactChars.filter((ch) => /[A-Za-z0-9]/.test(ch)).join("").toUpperCase();
        if (asciiChars) return asciiChars.slice(0, 2);
        return compactChars.slice(0, 2).join("").toUpperCase();
    }
    const chatType = String(item?.chat_type || "").trim().toLowerCase();
    if (chatType === "group") return "#";
    if (chatType === "thread") return "T";
    if (String(item?.session_family || "").trim() === "channel") return "@";
    return "S";
}

function syncCeoSessionPanelState() {
    const expanded = !!S.ceoSessionPanelExpanded;
    U.ceoShell?.classList.toggle("is-session-panel-expanded", expanded);
    U.ceoSessionPanel?.setAttribute("data-panel-state", expanded ? "expanded" : "collapsed");
    if (U.ceoSessionPanelToggle) {
        U.ceoSessionPanelToggle.setAttribute("aria-expanded", expanded ? "true" : "false");
        U.ceoSessionPanelToggle.setAttribute("aria-label", expanded ? "Collapse session list" : "Expand session list");
        U.ceoSessionPanelToggle.innerHTML = `<i data-lucide="${expanded ? "chevrons-left" : "chevrons-right"}"></i>`;
    }
    icons();
}

function setCeoSessionPanelExpanded(expanded) {
    S.ceoSessionPanelExpanded = !!expanded;
    if (!S.ceoSessionPanelExpanded) {
        S.ceoBulkMode = false;
        clearCeoBulkSelection();
        closeCeoSessionMenus();
    }
    syncCeoSessionPanelState();
    syncCeoSessionActions();
}

function setCeoSessionTab(tab) {
    const next = String(tab || "local").trim() === "channel" ? "channel" : "local";
    if (S.ceoSessionTab === next) return;
    S.ceoSessionTab = next;
    clearCeoBulkSelection();
    closeCeoSessionMenus();
    renderCeoSessions();
    syncCeoSessionActions();
}

function syncCeoComposerReadonlyState() {
    if (!U.ceoInput) return;
    if (activeSessionIsReadonly()) {
        U.ceoInput.setAttribute("readonly", "readonly");
        U.ceoInput.placeholder = "当前为渠道会话，只能查看来自渠道的历史消息";
    } else {
        U.ceoInput.removeAttribute("readonly");
        U.ceoInput.placeholder = "输入你的任务，可保留换行；也可以上传图片或文件作为补充";
    }
}

function patchCeoSessionRuntimeState(sessionId, isRunning) {
    const key = String(sessionId || "").trim();
    if (!key || !Array.isArray(S.ceoSessions)) return false;
    const current = (S.ceoSessions || []).find((item) => String(item?.session_id || "").trim() === key) || null;
    if (!current) return false;
    const nextValue = !!isRunning;
    if (!!current.is_running === nextValue) return false;
    S.ceoLocalSessions = sortCeoSessionsByTime((S.ceoLocalSessions || []).map((item) =>
        String(item?.session_id || "").trim() === key ? { ...item, is_running: nextValue } : item
    ));
    S.ceoChannelGroups = normalizeCeoChannelGroups((S.ceoChannelGroups || []).map((group) => ({
        ...group,
        items: (group?.items || []).map((item) =>
            String(item?.session_id || "").trim() === key ? { ...item, is_running: nextValue } : item
        ),
    })));
    rebuildCeoSessionIndex();
    return true;
}

function formatSessionTime(value) {
    const raw = String(value || "").trim();
    if (!raw) return "No activity yet";
    const parsed = new Date(raw);
    if (Number.isNaN(parsed.getTime())) return raw;
    return parsed.toLocaleString();
}

function formatCompactTime(value) {
    const raw = String(value || "").trim();
    if (!raw) return "";
    const parsed = new Date(raw);
    if (Number.isNaN(parsed.getTime())) return raw;
    const now = new Date();
    const sameDay = parsed.getFullYear() === now.getFullYear()
        && parsed.getMonth() === now.getMonth()
        && parsed.getDate() === now.getDate();
    return sameDay
        ? parsed.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false })
        : parsed.toLocaleString([], {
            month: "2-digit",
            day: "2-digit",
            hour: "2-digit",
            minute: "2-digit",
            second: "2-digit",
            hour12: false,
        });
}

function ceoSessionDisplayTime(session) {
    const item = session && typeof session === "object" ? session : {};
    return String(item.last_llm_output_at || item.updated_at || item.created_at || "").trim();
}

function normalizeInt(value, fallback = 0) {
    const next = Number(value);
    return Number.isFinite(next) ? Math.trunc(next) : Math.trunc(fallback);
}

function parseNonNegativeInteger(value) {
    const trimmed = String(value ?? "").trim();
    if (!/^\d+$/.test(trimmed)) return null;
    const next = Number(trimmed);
    return Number.isSafeInteger(next) ? next : null;
}

function countMatches(text, pattern) {
    if (!(pattern instanceof RegExp)) return 0;
    const matches = String(text || "").match(pattern);
    return Array.isArray(matches) ? matches.length : 0;
}

function decodeJsonStringLiteral(text) {
    const trimmed = String(text || "").trim();
    if (!(trimmed.startsWith('"') && trimmed.endsWith('"'))) return "";
    try {
        const parsed = JSON.parse(trimmed);
        return typeof parsed === "string" ? parsed : "";
    } catch {
        return "";
    }
}

function decodeEscapedDisplayText(value) {
    const raw = String(value ?? "");
    if (!raw.trim()) return "";

    const quotedDecoded = decodeJsonStringLiteral(raw);
    if (quotedDecoded) return quotedDecoded;

    const actualLineBreaks = countMatches(raw, /\r\n|\r|\n/g);
    const escapedLineBreaks = countMatches(raw, /\\r\\n|\\n|\\r/g);
    const plainEscapedLineBreaks = countMatches(raw, /(^|[^\\])(?:\\r\\n|\\n|\\r)/g);
    const escapedQuotes = countMatches(raw, /\\"/g);
    const escapedUnicode = countMatches(raw, /\\u[0-9a-fA-F]{4}/g);
    const likelyStructured = /^[\s"'[{(]/.test(raw);
    const shouldDecodeEscapes = plainEscapedLineBreaks >= 1 || (
        actualLineBreaks === 0 && (
            escapedLineBreaks >= 2
            || (escapedLineBreaks >= 1 && (escapedQuotes > 0 || escapedUnicode > 0 || likelyStructured))
            || escapedUnicode > 0
        )
    );

    if (!shouldDecodeEscapes) return raw;

    try {
        return JSON.parse(`"${raw
            .replace(/\\/g, "\\\\")
            .replace(/"/g, '\\"')
            .replace(/\u2028/g, "\\u2028")
            .replace(/\u2029/g, "\\u2029")}"`);
    } catch {
        return raw
            .replace(/\\r\\n/g, "\n")
            .replace(/\\n/g, "\n")
            .replace(/\\r/g, "\n")
            .replace(/\\t/g, "\t")
            .replace(/\\"/g, '"')
            .replace(/\\\\/g, "\\");
    }
}

function readableText(value, { decodeEscapes = false, emptyText = "" } = {}) {
    const raw = String(value ?? "");
    if (!raw.trim()) return emptyText;
    return decodeEscapes ? decodeEscapedDisplayText(raw) : raw;
}

function shouldDecodeArtifactContent(artifact, content) {
    const kind = String(artifact?.kind || "").trim().toLowerCase();
    const title = String(artifact?.title || "").trim().toLowerCase();
    if (kind === "patch") return false;
    if (/(output|log|trace|result|stdout|stderr)/.test(kind)) return true;
    if (/(output|log|trace|stdout|stderr)/.test(title)) return true;
    const raw = String(content ?? "");
    return !!decodeJsonStringLiteral(raw) || countMatches(raw, /\\r\\n|\\n|\\r/g) >= 2;
}

function tryParseJsonText(text) {
    const raw = String(text ?? "").trim();
    if (!raw) return null;
    try {
        return JSON.parse(raw);
    } catch {
        return null;
    }
}

function hasMeaningfulArtifactValue(value) {
    if (value === null || value === undefined) return false;
    if (typeof value === "string") return !!value.trim();
    if (typeof value === "number" || typeof value === "boolean") return true;
    if (Array.isArray(value)) return value.some((item) => hasMeaningfulArtifactValue(item));
    if (typeof value === "object") return Object.values(value).some((item) => hasMeaningfulArtifactValue(item));
    return false;
}

function extractPrimaryArtifactText(value) {
    if (!value || typeof value !== "object" || Array.isArray(value)) return "";
    const keys = Object.keys(value);
    if (!keys.length || keys.length > 4) return "";
    const preferredFields = [
        "content",
        "text",
        "output",
        "result",
        "stdout",
        "stderr",
        "body",
        "message",
        "final_output",
        "answer",
        "summary",
    ];
    const matchedField = preferredFields.find((field) => typeof value[field] === "string" && String(value[field] || "").trim());
    if (!matchedField) return "";
    const remainingKeys = keys.filter((field) => field !== matchedField && hasMeaningfulArtifactValue(value[field]));
    if (remainingKeys.length > 1) return "";
    return String(value[matchedField] || "");
}

function formatArtifactDisplayValue(value, { depth = 0 } = {}) {
    if (depth > 3) return String(value ?? "");
    if (typeof value === "string") {
        const decoded = decodeEscapedDisplayText(value);
        const parsed = tryParseJsonText(decoded);
        if (parsed === null) return decoded;
        if (typeof parsed === "string") {
            return formatArtifactDisplayValue(parsed, { depth: depth + 1 });
        }
        const extractedText = extractPrimaryArtifactText(parsed);
        if (extractedText) {
            return formatArtifactDisplayValue(extractedText, { depth: depth + 1 });
        }
        try {
            return JSON.stringify(parsed, null, 2);
        } catch {
            return decoded;
        }
    }
    if (typeof value === "number" || typeof value === "boolean") return String(value);
    if (Array.isArray(value) || (value && typeof value === "object")) {
        const extractedText = extractPrimaryArtifactText(value);
        if (extractedText) {
            return formatArtifactDisplayValue(extractedText, { depth: depth + 1 });
        }
        try {
            return JSON.stringify(value, null, 2);
        } catch {
            return String(value);
        }
    }
    return String(value ?? "");
}

function artifactDisplayText(artifact, content) {
    const raw = String(content ?? "");
    if (!raw.trim()) return "Select an artifact to view details.";
    const kind = String(artifact?.kind || "").trim().toLowerCase();
    if (kind === "patch") return raw;
    if (!shouldDecodeArtifactContent(artifact, content)) return formatArtifactDisplayValue(raw);
    return formatArtifactDisplayValue(decodeEscapedDisplayText(raw));
}

function normalizeTraceOutputRef(outputRef = "") {
    return String(outputRef || "").trim();
}

function ensureTraceOutputContentState() {
    if (!S.traceOutputContentByKey || typeof S.traceOutputContentByKey !== "object") {
        S.traceOutputContentByKey = {};
    }
    if (!S.traceOutputRequestsByKey || typeof S.traceOutputRequestsByKey !== "object") {
        S.traceOutputRequestsByKey = {};
    }
}

function traceOutputContentCacheKey(outputRef = "", view = "canonical") {
    const normalizedRef = normalizeTraceOutputRef(outputRef);
    const normalizedView = String(view || "canonical").trim().toLowerCase() || "canonical";
    return normalizedRef ? `${normalizedView}:${normalizedRef}` : "";
}

function extractTraceOutputContentText(payload = null) {
    const raw = String(payload?.content || payload?.excerpt || "");
    return String(formatArtifactDisplayValue(raw) || "").trim();
}

async function getTraceOutputContentByRef(outputRef = "", { view = "canonical" } = {}) {
    const normalizedRef = normalizeTraceOutputRef(outputRef);
    if (!normalizedRef) return "";
    ensureTraceOutputContentState();
    const cacheKey = traceOutputContentCacheKey(normalizedRef, view);
    if (!cacheKey) return "";
    if (Object.prototype.hasOwnProperty.call(S.traceOutputContentByKey, cacheKey)) {
        return String(S.traceOutputContentByKey[cacheKey] || "");
    }
    if (S.traceOutputRequestsByKey[cacheKey]) {
        return S.traceOutputRequestsByKey[cacheKey];
    }
    const request = (async () => {
        const payload = typeof ApiClient?.readContent === "function"
            ? await ApiClient.readContent({ ref: normalizedRef, view })
            : await ApiClient.openContent({ ref: normalizedRef, view, startLine: 1, endLine: 200 });
        const text = extractTraceOutputContentText(payload);
        S.traceOutputContentByKey[cacheKey] = text;
        return text;
    })();
    S.traceOutputRequestsByKey[cacheKey] = request;
    try {
        return await request;
    } finally {
        delete S.traceOutputRequestsByKey[cacheKey];
    }
}

async function ensureTraceOutputCodeBlockContent(
    element,
    {
        loadingText = "正在加载完整输出...",
        errorPrefix = "加载完整输出失败：",
        view = "canonical",
    } = {},
) {
    if (!(element instanceof HTMLElement)) return "";
    const outputRef = normalizeTraceOutputRef(element.dataset.outputRef || "");
    if (!outputRef) return String(element.textContent || "");
    if (element.dataset.outputHydrated === "true") {
        return String(element.textContent || "");
    }
    const previewText = String(element.dataset.previewText || element.textContent || "");
    const emptyText = String(element.dataset.emptyText || "").trim();
    element.dataset.previewText = previewText;
    element.dataset.outputHydrating = "true";
    element.textContent = loadingText;
    try {
        const fullText = await getTraceOutputContentByRef(outputRef, { view });
        const nextText = String(fullText || previewText || emptyText).trim() || emptyText;
        element.textContent = nextText;
        element.dataset.outputHydrated = "true";
        return nextText;
    } catch (error) {
        const message = typeof ApiClient?.friendlyErrorMessage === "function"
            ? ApiClient.friendlyErrorMessage(error, error?.message || "未知错误")
            : String(error?.message || error || "未知错误");
        const fallbackText = String(previewText || emptyText).trim();
        element.textContent = fallbackText
            ? `${fallbackText}\n\n${errorPrefix}${message}`
            : `${errorPrefix}${message}`;
        element.dataset.outputHydrated = "error";
        return fallbackText;
    } finally {
        delete element.dataset.outputHydrating;
    }
}

async function ensureCeoToolStepFullOutput(item, { view = "canonical" } = {}) {
    if (!(item instanceof HTMLElement)) return "";
    const outputRef = normalizeTraceOutputRef(item.dataset.outputRef || "");
    if (!outputRef) return normalizeInteractionDetailText(item.dataset.detailText || "");
    if (item.dataset.outputHydrated === "true") {
        return normalizeInteractionDetailText(item.dataset.detailText || "");
    }
    const previewText = normalizeInteractionDetailText(item.dataset.previewDetailText || item.dataset.detailText || "");
    item.dataset.previewDetailText = previewText;
    item.dataset.outputHydrating = "true";
    setCeoToolStepOutput(item, "正在加载完整输出...");
    try {
        const fullText = await getTraceOutputContentByRef(outputRef, { view });
        const nextText = normalizeInteractionDetailText(fullText) || previewText;
        setCeoToolStepOutput(item, nextText);
        item.dataset.outputHydrated = "true";
        return nextText;
    } catch (error) {
        const message = typeof ApiClient?.friendlyErrorMessage === "function"
            ? ApiClient.friendlyErrorMessage(error, error?.message || "未知错误")
            : String(error?.message || error || "未知错误");
        const fallbackText = previewText
            ? `${previewText}\n\n加载完整输出失败：${message}`
            : `加载完整输出失败：${message}`;
        setCeoToolStepOutput(item, fallbackText);
        item.dataset.outputHydrated = "error";
        return previewText;
    } finally {
        delete item.dataset.outputHydrating;
    }
}

function setElementScrollTop(element, value) {
    if (!(element instanceof HTMLElement)) return;
    const numericValue = Number(value);
    if (!Number.isFinite(numericValue)) return;
    element.scrollTop = Math.max(0, numericValue);
}

async function copyTextToClipboard(text) {
    const value = String(text || "");
    if (!value) return false;
    if (navigator.clipboard?.writeText) {
        await navigator.clipboard.writeText(value);
        return true;
    }
    const textarea = document.createElement("textarea");
    textarea.value = value;
    textarea.setAttribute("readonly", "readonly");
    textarea.style.position = "fixed";
    textarea.style.opacity = "0";
    textarea.style.pointerEvents = "none";
    document.body.appendChild(textarea);
    textarea.focus();
    textarea.select();
    let copied = false;
    try {
        copied = document.execCommand("copy");
    } finally {
        textarea.remove();
    }
    return copied;
}

function readSessionJson(key) {
    try {
        const raw = window.sessionStorage?.getItem?.(key);
        return raw ? JSON.parse(raw) : null;
    } catch {
        return null;
    }
}

function writeSessionJson(key, value) {
    try {
        window.sessionStorage?.setItem?.(key, JSON.stringify(value));
    } catch { }
}

function removeSessionJson(key) {
    try {
        window.sessionStorage?.removeItem?.(key);
    } catch { }
}

function normalizeCeoComposerDraftEntry(sessionId, entry = {}) {
    const key = String(sessionId || entry?.session_id || "").trim();
    if (!key) return null;
    const text = String(entry?.text || "");
    const uploads = cloneCeoSnapshotAttachments(entry?.uploads);
    if (!text.trim() && !uploads.length) return null;
    return {
        session_id: key,
        text,
        uploads,
        cached_at: String(entry?.cached_at || "").trim() || new Date().toISOString(),
    };
}

function cloneCeoComposerDraftEntry(entry = null) {
    if (!entry || typeof entry !== "object") return null;
    return normalizeCeoComposerDraftEntry(entry.session_id, entry);
}

function pruneCeoComposerDraftCache(cache = {}) {
    const entries = Object.values(cache || {})
        .map((entry) => cloneCeoComposerDraftEntry(entry))
        .filter(Boolean)
        .sort((left, right) => String(right.cached_at || "").localeCompare(String(left.cached_at || "")))
        .slice(0, CEO_COMPOSER_DRAFT_CACHE_LIMIT);
    return entries.reduce((acc, entry) => {
        acc[entry.session_id] = entry;
        return acc;
    }, {});
}

function persistCeoComposerDraftCache() {
    const items = Object.values(pruneCeoComposerDraftCache(S.ceoComposerDrafts || {}));
    if (!items.length) {
        removeSessionJson(CEO_COMPOSER_DRAFT_CACHE_KEY);
        return;
    }
    writeSessionJson(CEO_COMPOSER_DRAFT_CACHE_KEY, { items });
}

function schedulePersistCeoComposerDraftCache() {
    if (S.ceoComposerDraftPersistId) window.clearTimeout(S.ceoComposerDraftPersistId);
    S.ceoComposerDraftPersistId = window.setTimeout(() => {
        S.ceoComposerDraftPersistId = null;
        persistCeoComposerDraftCache();
    }, 120);
}

function flushCeoComposerDraftCachePersist() {
    if (S.ceoComposerDraftPersistId) {
        window.clearTimeout(S.ceoComposerDraftPersistId);
        S.ceoComposerDraftPersistId = null;
    }
    persistCeoComposerDraftCache();
}

function hydrateCeoComposerDraftCache() {
    const raw = readSessionJson(CEO_COMPOSER_DRAFT_CACHE_KEY);
    const items = Array.isArray(raw?.items) ? raw.items : (Array.isArray(raw) ? raw : []);
    const next = {};
    items.forEach((entry) => {
        const normalized = normalizeCeoComposerDraftEntry(entry?.session_id, entry);
        if (!normalized) return;
        next[normalized.session_id] = normalized;
    });
    S.ceoComposerDrafts = pruneCeoComposerDraftCache(next);
}

function getCeoComposerDraft(sessionId) {
    const key = String(sessionId || "").trim();
    if (!key) return null;
    return cloneCeoComposerDraftEntry(S.ceoComposerDrafts?.[key] || null);
}

function setCeoComposerDraft(sessionId, entry = {}) {
    const key = String(sessionId || entry?.session_id || "").trim();
    if (!key) return null;
    const previous = S.ceoComposerDrafts?.[key] && typeof S.ceoComposerDrafts[key] === "object"
        ? S.ceoComposerDrafts[key]
        : {};
    const normalized = normalizeCeoComposerDraftEntry(key, {
        ...previous,
        ...(entry && typeof entry === "object" ? entry : {}),
        session_id: key,
        cached_at: new Date().toISOString(),
    });
    if (!normalized) {
        clearCeoComposerDraft(key);
        return null;
    }
    S.ceoComposerDrafts = pruneCeoComposerDraftCache({
        ...(S.ceoComposerDrafts || {}),
        [key]: normalized,
    });
    schedulePersistCeoComposerDraftCache();
    return cloneCeoComposerDraftEntry(normalized);
}

function clearCeoComposerDraft(sessionId) {
    const key = String(sessionId || "").trim();
    if (!key || !S.ceoComposerDrafts?.[key]) return false;
    const next = { ...(S.ceoComposerDrafts || {}) };
    delete next[key];
    S.ceoComposerDrafts = pruneCeoComposerDraftCache(next);
    schedulePersistCeoComposerDraftCache();
    return true;
}

function captureCeoComposerDraftFromUi() {
    return {
        text: String(U.ceoInput?.value || ""),
        uploads: normalizeUploadList(S.ceoUploads),
    };
}

function syncActiveCeoComposerDraft() {
    const sessionId = activeSessionId();
    if (!sessionId) return null;
    return setCeoComposerDraft(sessionId, captureCeoComposerDraftFromUi());
}

function restoreCeoComposerDraftForSession(sessionId) {
    const key = String(sessionId || "").trim();
    const draft = key ? getCeoComposerDraft(key) : null;
    S.ceoUploadBusy = false;
    S.ceoUploads = normalizeUploadList(draft?.uploads);
    if (U.ceoInput) U.ceoInput.value = String(draft?.text || "");
    if (U.ceoFileInput) U.ceoFileInput.value = "";
    renderPendingCeoUploads();
    renderQueuedCeoFollowUps(key);
    syncCeoInputHeight();
}

function switchCeoComposerDraft(previousSessionId, nextSessionId) {
    const previousId = String(previousSessionId || "").trim();
    const nextId = String(nextSessionId || "").trim();
    if (previousId === nextId) return false;
    if (previousId) setCeoComposerDraft(previousId, captureCeoComposerDraftFromUi());
    restoreCeoComposerDraftForSession(nextId);
    return true;
}

function normalizeCeoQueuedFollowUpEntry(entry = {}) {
    const text = String(entry?.text || "");
    const uploads = cloneCeoSnapshotAttachments(entry?.uploads);
    if (!text.trim() && !uploads.length) return null;
    return {
        id: String(entry?.id || `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`).trim(),
        text,
        uploads,
        queued_at: String(entry?.queued_at || "").trim() || new Date().toISOString(),
    };
}

function cloneCeoQueuedFollowUpEntry(entry = null) {
    if (!entry || typeof entry !== "object") return null;
    return normalizeCeoQueuedFollowUpEntry(entry);
}

function normalizeCeoQueuedFollowUpList(items = []) {
    return (Array.isArray(items) ? items : [])
        .map((item) => cloneCeoQueuedFollowUpEntry(item))
        .filter(Boolean)
        .slice(0, CEO_FOLLOW_UP_QUEUE_PER_SESSION_LIMIT);
}

function pruneCeoFollowUpQueueCache(cache = {}) {
    const entries = Object.entries(cache || {})
        .map(([sessionId, items]) => {
            const normalizedItems = normalizeCeoQueuedFollowUpList(items);
            if (!normalizedItems.length) return null;
            return {
                session_id: String(sessionId || "").trim(),
                items: normalizedItems,
                cached_at: normalizedItems[normalizedItems.length - 1]?.queued_at || new Date().toISOString(),
            };
        })
        .filter(Boolean)
        .sort((left, right) => String(right.cached_at || "").localeCompare(String(left.cached_at || "")))
        .slice(0, CEO_FOLLOW_UP_QUEUE_CACHE_LIMIT);
    return entries.reduce((acc, entry) => {
        acc[entry.session_id] = entry.items;
        return acc;
    }, {});
}

function persistCeoFollowUpQueueCache() {
    const cache = pruneCeoFollowUpQueueCache(S.ceoQueuedFollowUps || {});
    const items = Object.entries(cache).map(([sessionId, queuedItems]) => ({
        session_id: sessionId,
        items: queuedItems,
    }));
    if (!items.length) {
        removeSessionJson(CEO_FOLLOW_UP_QUEUE_CACHE_KEY);
        return;
    }
    writeSessionJson(CEO_FOLLOW_UP_QUEUE_CACHE_KEY, { items });
}

function schedulePersistCeoFollowUpQueueCache() {
    if (S.ceoQueuedFollowUpsPersistId) window.clearTimeout(S.ceoQueuedFollowUpsPersistId);
    S.ceoQueuedFollowUpsPersistId = window.setTimeout(() => {
        S.ceoQueuedFollowUpsPersistId = null;
        persistCeoFollowUpQueueCache();
    }, 120);
}

function flushCeoFollowUpQueueCachePersist() {
    if (S.ceoQueuedFollowUpsPersistId) {
        window.clearTimeout(S.ceoQueuedFollowUpsPersistId);
        S.ceoQueuedFollowUpsPersistId = null;
    }
    persistCeoFollowUpQueueCache();
}

function hydrateCeoFollowUpQueueCache() {
    const raw = readSessionJson(CEO_FOLLOW_UP_QUEUE_CACHE_KEY);
    const items = Array.isArray(raw?.items) ? raw.items : [];
    const next = {};
    items.forEach((entry) => {
        const sessionId = String(entry?.session_id || "").trim();
        if (!sessionId) return;
        const normalizedItems = normalizeCeoQueuedFollowUpList(entry?.items);
        if (!normalizedItems.length) return;
        next[sessionId] = normalizedItems;
    });
    S.ceoQueuedFollowUps = pruneCeoFollowUpQueueCache(next);
}

function getCeoQueuedFollowUps(sessionId = activeSessionId()) {
    const key = String(sessionId || "").trim();
    if (!key) return [];
    return normalizeCeoQueuedFollowUpList(S.ceoQueuedFollowUps?.[key] || []);
}

function setCeoQueuedFollowUps(sessionId, items = []) {
    const key = String(sessionId || "").trim();
    if (!key) return [];
    const normalizedItems = normalizeCeoQueuedFollowUpList(items);
    const next = { ...(S.ceoQueuedFollowUps || {}) };
    if (normalizedItems.length) next[key] = normalizedItems;
    else delete next[key];
    S.ceoQueuedFollowUps = pruneCeoFollowUpQueueCache(next);
    schedulePersistCeoFollowUpQueueCache();
    const current = getCeoQueuedFollowUps(key);
    renderQueuedCeoFollowUps(key);
    syncCeoPrimaryButton();
    return current;
}

function enqueueCeoFollowUp(sessionId, entry = {}) {
    const key = String(sessionId || "").trim();
    if (!key) return [];
    const current = getCeoQueuedFollowUps(key);
    const normalized = normalizeCeoQueuedFollowUpEntry(entry);
    if (!normalized) return current;
    return setCeoQueuedFollowUps(key, [...current, normalized]);
}

function removeCeoQueuedFollowUp(sessionId, entryId) {
    const key = String(sessionId || "").trim();
    const targetId = String(entryId || "").trim();
    if (!key || !targetId) return [];
    const current = getCeoQueuedFollowUps(key).filter((item) => String(item?.id || "").trim() !== targetId);
    return setCeoQueuedFollowUps(key, current);
}

function shiftCeoQueuedFollowUp(sessionId) {
    const key = String(sessionId || "").trim();
    const current = getCeoQueuedFollowUps(key);
    const [first, ...rest] = current;
    setCeoQueuedFollowUps(key, rest);
    return first || null;
}

function renderQueuedCeoFollowUps(sessionId = activeSessionId()) {
    if (!U.ceoFollowUpQueue) return;
    const items = getCeoQueuedFollowUps(sessionId);
    U.ceoFollowUpQueue.hidden = !items.length;
    if (!items.length) {
        U.ceoFollowUpQueue.innerHTML = "";
        return;
    }
    U.ceoFollowUpQueue.innerHTML = `
        <div class="ceo-follow-up-queue-title">待发送补充</div>
        ${items.map((item, index) => `
            <div class="ceo-follow-up-chip">
                <span class="ceo-follow-up-chip-index">${index + 1}.</span>
                <span class="ceo-follow-up-chip-text">${esc(String(item?.text || "").trim() || summarizeUploads(item?.uploads || []))}</span>
                <button class="ceo-follow-up-chip-remove" type="button" data-follow-up-remove="${esc(String(item?.id || ""))}" aria-label="删除待发送补充">×</button>
            </div>
        `).join("")}
    `;
}

function cloneCeoSnapshotAttachments(items = []) {
    return normalizeUploadList(items).map((item) => {
        const next = { path: String(item?.path || "").trim() };
        const name = String(item?.name || "").trim();
        const mimeType = String(item?.mime_type || "").trim();
        const kind = String(item?.kind || "").trim();
        const size = Number(item?.size);
        if (name) next.name = name;
        if (mimeType) next.mime_type = mimeType;
        if (kind) next.kind = kind;
        if (Number.isFinite(size) && size > 0) next.size = size;
        return next;
    }).filter((item) => item.path);
}

function normalizeCeoSnapshotToolEvent(event = {}) {
    if (!event || typeof event !== "object") return null;
    const toolName = String(event?.tool_name || "").trim().toLowerCase();
    if (toolName === "submit_next_stage") return null;
    const next = {};
    ["status", "tool_name", "text", "timestamp", "tool_call_id", "kind", "source", "output_ref"].forEach((key) => {
        const value = String(event?.[key] || "").trim();
        if (value) next[key] = value;
    });
    ["is_error", "is_update"].forEach((key) => {
        if (typeof event?.[key] === "boolean") next[key] = event[key];
    });
    const elapsedSeconds = Number(event?.elapsed_seconds);
    if (Number.isFinite(elapsedSeconds) && elapsedSeconds >= 0) next.elapsed_seconds = elapsedSeconds;
    return Object.keys(next).length ? next : null;
}

function normalizeCeoSnapshotToolEvents(events = []) {
    return (Array.isArray(events) ? events : [])
        .map((item) => normalizeCeoSnapshotToolEvent(item))
        .filter(Boolean)
        .slice(-CEO_SESSION_SNAPSHOT_TOOL_EVENT_LIMIT);
}

function isRenderableCeoSnapshotStage(stage = null) {
    if (!stage || typeof stage !== "object") return false;
    const stageGoal = String(stage?.stage_goal || "").trim();
    const stageBudget = Number(stage?.tool_round_budget ?? stage?.stage_total_steps ?? 0);
    const systemGenerated = stage?.system_generated === true;
    if (systemGenerated && !stageGoal && (!Number.isFinite(stageBudget) || stageBudget <= 0)) {
        return false;
    }
    return true;
}

function normalizeCeoSnapshotExecutionTraceSummary(summary = null) {
    if (!summary || typeof summary !== "object") return null;
    const rawStages = Array.isArray(summary?.stages) ? summary.stages : [];
    const hasRenderableRealStages = rawStages.some((stage) => (
        isRenderableCeoSnapshotStage(stage) && stage?.system_generated !== true
    ));
    const stages = rawStages
        .filter((stage) => {
            if (!isRenderableCeoSnapshotStage(stage)) return false;
            const systemGenerated = stage?.system_generated === true;
            const stageKind = String(stage?.stage_kind || "normal").trim() || "normal";
            if (hasRenderableRealStages && systemGenerated && stageKind === "normal") {
                return false;
            }
            return true;
        })
        .map((stage, index) => {
            if (typeof normalizeExecutionStageTrace !== "function") return null;
            return normalizeExecutionStageTrace(stage, index);
        })
        .filter(Boolean);
    if (!stages.length) return null;
    return { stages };
}

function normalizeCeoSnapshotCompression(compression = null) {
    if (!compression || typeof compression !== "object") return null;
    const status = String(compression?.status || "").trim().toLowerCase();
    if (!status) return null;
    const next = { status };
    const source = String(compression?.source || "").trim().toLowerCase();
    if (source) next.source = source;
    const text = String(compression?.text || "").trim();
    if (text) next.text = text;
    return next;
}

function normalizeCeoSnapshotMessage(message = {}) {
    if (!message || typeof message !== "object") return null;
    const role = String(message?.role || "").trim().toLowerCase();
    if (!["user", "assistant", "system"].includes(role)) return null;
    const next = {
        role,
        content: String(message?.content || ""),
    };
    const turnId = String(message?.turn_id || "").trim();
    if (turnId) next.turn_id = turnId;
    const timestamp = String(message?.timestamp || "").trim();
    if (timestamp) next.timestamp = timestamp;
    const attachments = role === "user" ? cloneCeoSnapshotAttachments(message?.attachments) : [];
    if (attachments.length) next.attachments = attachments;
    if (role === "assistant") {
        const toolEvents = normalizeCeoSnapshotToolEvents(message?.tool_events);
        const executionTraceSummary = normalizeCeoSnapshotExecutionTraceSummary(message?.execution_trace_summary);
        if (toolEvents.length) next.tool_events = toolEvents;
        if (executionTraceSummary) next.execution_trace_summary = executionTraceSummary;
        if (!String(next.content || "").trim() && !toolEvents.length && !executionTraceSummary) return null;
        return next;
    }
    if (role === "user" && !String(next.content || "").trim() && !attachments.length) return null;
    if (role === "system" && !String(next.content || "").trim()) return null;
    return next;
}

function normalizeCeoSnapshotInflight(snapshot = null) {
    if (!snapshot || typeof snapshot !== "object") return null;
    const next = {};
    const turnId = String(snapshot?.turn_id || "").trim();
    const source = String(snapshot?.source || "").trim().toLowerCase();
    const status = String(snapshot?.status || "").trim().toLowerCase();
    const assistantText = String(snapshot?.assistant_text || "");
    if (turnId) next.turn_id = turnId;
    if (source) next.source = source;
    if (status) next.status = status;
    if (assistantText.trim()) next.assistant_text = assistantText;
    const userMessage = snapshot?.user_message && typeof snapshot.user_message === "object" ? snapshot.user_message : null;
    if (userMessage) {
        const content = String(userMessage?.content || "");
        const attachments = cloneCeoSnapshotAttachments(userMessage?.attachments);
        if (content.trim() || attachments.length) {
            next.user_message = { content };
            if (attachments.length) next.user_message.attachments = attachments;
        }
    }
    const toolEvents = normalizeCeoSnapshotToolEvents(snapshot?.tool_events);
    const executionTraceSummary = normalizeCeoSnapshotExecutionTraceSummary(snapshot?.execution_trace_summary);
    const compression = normalizeCeoSnapshotCompression(snapshot?.compression);
    const errorMessage = String(snapshot?.last_error?.message || "").trim();
    if (toolEvents.length) next.tool_events = toolEvents;
    if (executionTraceSummary) next.execution_trace_summary = executionTraceSummary;
    if (compression) next.compression = compression;
    if (errorMessage) next.last_error = { message: errorMessage };
    if (!ceoInflightTurnHasVisibleAssistantState(next) && !next.user_message && !next.compression) return null;
    return next;
}

function trimCeoSessionSnapshotMessages(messages = []) {
    const normalized = (Array.isArray(messages) ? messages : [])
        .map((item) => normalizeCeoSnapshotMessage(item))
        .filter(Boolean);
    return normalized.slice(-CEO_SESSION_SNAPSHOT_MESSAGE_LIMIT);
}

function normalizeCeoSessionSnapshotCacheEntry(sessionId, entry = {}) {
    const key = String(sessionId || entry?.session_id || "").trim();
    if (!key) return null;
    const messages = trimCeoSessionSnapshotMessages(entry?.messages);
    const inflightTurn = normalizeCeoSnapshotInflight(entry?.inflight_turn);
    if (!messages.length && !inflightTurn) return null;
    const next = {
        session_id: key,
        messages,
        cached_at: String(entry?.cached_at || "").trim() || new Date().toISOString(),
    };
    if (inflightTurn) next.inflight_turn = inflightTurn;
    const messageCount = Number(entry?.message_count);
    if (Number.isFinite(messageCount) && messageCount >= 0) next.message_count = Math.floor(messageCount);
    const updatedAt = String(entry?.updated_at || "").trim();
    if (updatedAt) next.updated_at = updatedAt;
    return next;
}

function cloneCeoSessionSnapshotCacheEntry(entry = null) {
    if (!entry || typeof entry !== "object") return null;
    return normalizeCeoSessionSnapshotCacheEntry(entry.session_id, entry);
}

function pruneCeoSessionSnapshotCache(cache = {}) {
    const items = Object.values(cache || {})
        .map((entry) => cloneCeoSessionSnapshotCacheEntry(entry))
        .filter(Boolean)
        .sort((left, right) => String(right?.cached_at || "").localeCompare(String(left?.cached_at || "")))
        .slice(0, CEO_SESSION_SNAPSHOT_CACHE_LIMIT);
    return items.reduce((acc, entry) => {
        acc[entry.session_id] = entry;
        return acc;
    }, {});
}

function persistCeoSessionSnapshotCache() {
    const items = Object.values(pruneCeoSessionSnapshotCache(S.ceoSnapshotCache || {}));
    if (!items.length) {
        removeSessionJson(CEO_SESSION_SNAPSHOT_CACHE_KEY);
        return;
    }
    writeSessionJson(CEO_SESSION_SNAPSHOT_CACHE_KEY, { items });
}

function schedulePersistCeoSessionSnapshotCache() {
    if (S.ceoSnapshotPersistId) window.clearTimeout(S.ceoSnapshotPersistId);
    S.ceoSnapshotPersistId = window.setTimeout(() => {
        S.ceoSnapshotPersistId = null;
        persistCeoSessionSnapshotCache();
    }, 160);
}

function flushCeoSessionSnapshotCachePersist() {
    if (S.ceoSnapshotPersistId) {
        window.clearTimeout(S.ceoSnapshotPersistId);
        S.ceoSnapshotPersistId = null;
    }
    persistCeoSessionSnapshotCache();
}

function hydrateCeoSessionSnapshotCache() {
    const raw = readSessionJson(CEO_SESSION_SNAPSHOT_CACHE_KEY);
    const items = Array.isArray(raw?.items) ? raw.items : (Array.isArray(raw) ? raw : []);
    const next = {};
    items.forEach((entry) => {
        const normalized = normalizeCeoSessionSnapshotCacheEntry(entry?.session_id, entry);
        if (!normalized) return;
        next[normalized.session_id] = normalized;
    });
    S.ceoSnapshotCache = pruneCeoSessionSnapshotCache(next);
}

function getCeoSessionSnapshotCache(sessionId) {
    const key = String(sessionId || "").trim();
    if (!key) return null;
    return cloneCeoSessionSnapshotCacheEntry(S.ceoSnapshotCache?.[key] || null);
}

function setCeoSessionSnapshotCache(sessionId, entry = {}) {
    const key = String(sessionId || entry?.session_id || "").trim();
    if (!key) return null;
    const previous = S.ceoSnapshotCache?.[key] && typeof S.ceoSnapshotCache[key] === "object"
        ? S.ceoSnapshotCache[key]
        : {};
    const normalized = normalizeCeoSessionSnapshotCacheEntry(key, {
        ...previous,
        ...(entry && typeof entry === "object" ? entry : {}),
        session_id: key,
        cached_at: new Date().toISOString(),
    });
    if (!normalized) {
        clearCeoSessionSnapshotCache(key);
        return null;
    }
    S.ceoSnapshotCache = pruneCeoSessionSnapshotCache({
        ...(S.ceoSnapshotCache || {}),
        [key]: normalized,
    });
    schedulePersistCeoSessionSnapshotCache();
    syncCeoCompressionToast();
    return cloneCeoSessionSnapshotCacheEntry(normalized);
}

function patchCeoSessionSnapshotCache(sessionId, updater) {
    if (typeof updater !== "function") return null;
    const key = String(sessionId || "").trim();
    if (!key) return null;
    const current = getCeoSessionSnapshotCache(key);
    const next = updater(current);
    if (next === null) {
        clearCeoSessionSnapshotCache(key);
        return null;
    }
    return setCeoSessionSnapshotCache(key, {
        ...(next && typeof next === "object" ? next : {}),
        session_id: key,
    });
}

function clearCeoSessionSnapshotCache(sessionId) {
    const key = String(sessionId || "").trim();
    if (!key || !S.ceoSnapshotCache?.[key]) return false;
    const next = { ...(S.ceoSnapshotCache || {}) };
    delete next[key];
    S.ceoSnapshotCache = pruneCeoSessionSnapshotCache(next);
    schedulePersistCeoSessionSnapshotCache();
    syncCeoCompressionToast();
    return true;
}

function activeCeoSessionCompressionState() {
    const cacheEntry = getCeoSessionSnapshotCache(activeSessionId());
    const inflightTurn = normalizeCeoSnapshotInflight(cacheEntry?.inflight_turn);
    const inflightStatus = String(inflightTurn?.status || "").trim().toLowerCase();
    if (inflightStatus && !["running", "in_progress", "active"].includes(inflightStatus)) return null;
    const compression = normalizeCeoSnapshotCompression(inflightTurn?.compression);
    if (!compression) return null;
    return String(compression.status || "").trim().toLowerCase() === "running" ? compression : null;
}

function syncCeoCompressionToast() {
    const toastEl = U.ceoCompressionToast;
    const textEl = U.ceoCompressionToastText;
    if (!toastEl || !textEl) return;
    const compression = activeCeoSessionCompressionState();
    const visible = !!compression;
    textEl.textContent = visible ? CEO_COMPRESSION_TOAST_TEXT : "";
    toastEl.hidden = !visible;
    if (toastEl.classList?.toggle) toastEl.classList.toggle("is-visible", visible);
    toastEl.setAttribute("aria-hidden", visible ? "false" : "true");
}

function appendCeoSessionSnapshotMessage(messages = [], message = null) {
    const nextMessage = normalizeCeoSnapshotMessage(message);
    const next = trimCeoSessionSnapshotMessages(messages);
    if (!nextMessage) return next;
    const previous = next[next.length - 1] || null;
    const sameAttachments = JSON.stringify(previous?.attachments || []) === JSON.stringify(nextMessage.attachments || []);
    const sameTurnId = String(previous?.turn_id || "") === String(nextMessage?.turn_id || "");
    if (
        previous
        && previous.role === nextMessage.role
        && String(previous.content || "") === String(nextMessage.content || "")
        && sameAttachments
        && sameTurnId
    ) {
        if (nextMessage.tool_events) previous.tool_events = nextMessage.tool_events;
        if (nextMessage.execution_trace_summary) previous.execution_trace_summary = nextMessage.execution_trace_summary;
        return trimCeoSessionSnapshotMessages(next);
    }
    next.push(nextMessage);
    return trimCeoSessionSnapshotMessages(next);
}

function dedupeInflightUserMessageAgainstMessages(messages = [], inflightTurn = null) {
    const normalizedInflight = normalizeCeoSnapshotInflight(inflightTurn);
    if (!normalizedInflight?.user_message) return normalizedInflight;
    const normalizedMessages = trimCeoSessionSnapshotMessages(messages);
    const lastUserMessage = [...normalizedMessages].reverse().find((item) => String(item?.role || "").trim().toLowerCase() === "user");
    if (!lastUserMessage) return normalizedInflight;
    const userContent = String(normalizedInflight.user_message?.content || "");
    const userAttachments = normalizeUploadList(normalizedInflight.user_message?.attachments);
    const lastContent = String(lastUserMessage?.content || "");
    const lastAttachments = normalizeUploadList(lastUserMessage?.attachments);
    const sameAttachments = JSON.stringify(lastAttachments) === JSON.stringify(userAttachments);
    if (lastContent !== userContent || !sameAttachments) return normalizedInflight;
    const deduped = { ...normalizedInflight };
    delete deduped.user_message;
    return ceoNeedsAssistantTurn(deduped) ? deduped : null;
}

function renderCeoSessionLoadingState(sessionId, session = null) {
    resetCeoFeed();
    const title = String(session?.title || session?.channel_id || sessionId || "conversation").trim() || "conversation";
    addMsg(`Loading ${title}...`, "system", { scrollMode: "bottom" });
}

function renderCeoSessionSnapshotFromCache(sessionId, { scrollToLatest = true } = {}) {
    const entry = getCeoSessionSnapshotCache(sessionId);
    if (!entry) return false;
    if (scrollToLatest) S.ceoScrollToLatestOnSnapshot = true;
    renderCeoSnapshot(entry.messages || [], entry.inflight_turn || null);
    return true;
}

function isTaskDetailsViewActive() {
    return !!U.viewTaskDetails?.classList.contains("active");
}

function taskDetailStateKey(taskId = S.currentTaskId, nodeId = S.selectedNodeId) {
    const normalizedTaskId = String(taskId || "").trim();
    const normalizedNodeId = String(nodeId || "").trim();
    if (!normalizedTaskId || !normalizedNodeId) return "";
    return `${normalizedTaskId}::${normalizedNodeId}`;
}

function normalizeTaskDetailViewState(value) {
    if (!value || typeof value !== "object") return null;
    const normalizeScrollTop = (input) => {
        const numericValue = Number(input);
        return Number.isFinite(numericValue) && numericValue > 0 ? numericValue : 0;
    };
    const normalizeTraceItems = (items) => (Array.isArray(items)
        ? items.map((item, index) => ({
            index: Number.isInteger(item?.index) && item.index >= 0 ? item.index : index,
            key: String(item?.key || "").trim(),
            title: String(item?.title || "").trim(),
            open: !!item?.open,
            activeToolKey: String(item?.activeToolKey || "").trim(),
        }))
        : []);
    const traceItems = normalizeTraceItems(value.traceItems);
    const spawnReviewItems = normalizeTraceItems(value.spawnReviewItems);
    return {
        detailScrollTop: normalizeScrollTop(value.detailScrollTop),
        traceScrollTop: normalizeScrollTop(value.traceScrollTop),
        spawnReviewScrollTop: normalizeScrollTop(value.spawnReviewScrollTop),
        artifactListScrollTop: normalizeScrollTop(value.artifactListScrollTop),
        artifactContentScrollTop: normalizeScrollTop(value.artifactContentScrollTop),
        traceItems,
        spawnReviewItems,
    };
}

function captureTaskDetailSessionSnapshot() {
    const currentTaskId = String(S.currentTaskId || "").trim();
    if (!currentTaskId || !isTaskDetailsViewActive()) return null;
    const nodeViewStates = { ...(S.taskDetailViewStates || {}) };
    const currentKey = taskDetailStateKey(currentTaskId, S.selectedNodeId);
    const currentViewState = normalizeTaskDetailViewState(captureTaskDetailViewState());
    if (currentKey && currentViewState) nodeViewStates[currentKey] = currentViewState;
    return {
        currentTaskId,
        selectedNodeId: String(S.selectedNodeId || "").trim(),
        selectedArtifactId: String(S.selectedArtifactId || "").trim(),
        treeSelectedRoundByNodeId: normalizeTreeRoundSelections(S.treeSelectedRoundByNodeId),
        nodeViewStates,
    };
}

let taskDetailSessionPersistTimer = 0;

function persistTaskDetailSessionNow() {
    const snapshot = captureTaskDetailSessionSnapshot();
    if (!snapshot) {
        removeSessionJson(TASK_DETAIL_SESSION_KEY);
        return;
    }
    writeSessionJson(TASK_DETAIL_SESSION_KEY, snapshot);
}

function scheduleTaskDetailSessionPersist() {
    if (taskDetailSessionPersistTimer) return;
    taskDetailSessionPersistTimer = window.setTimeout(() => {
        taskDetailSessionPersistTimer = 0;
        persistTaskDetailSessionNow();
    }, 120);
}

function flushTaskDetailSessionPersist() {
    if (taskDetailSessionPersistTimer) {
        window.clearTimeout(taskDetailSessionPersistTimer);
        taskDetailSessionPersistTimer = 0;
    }
    persistTaskDetailSessionNow();
}

function clearTaskDetailSession() {
    if (taskDetailSessionPersistTimer) {
        window.clearTimeout(taskDetailSessionPersistTimer);
        taskDetailSessionPersistTimer = 0;
    }
    S.taskDetailViewStates = {};
    S.pendingTaskDetailRestore = null;
    removeSessionJson(TASK_DETAIL_SESSION_KEY);
}

function stashTaskDetailViewState({ taskId = S.currentTaskId, nodeId = S.selectedNodeId, viewState = null } = {}) {
    const key = taskDetailStateKey(taskId, nodeId);
    if (!key) return null;
    const normalizedState = normalizeTaskDetailViewState(viewState || captureTaskDetailViewState());
    if (!normalizedState) return null;
    S.taskDetailViewStates = { ...(S.taskDetailViewStates || {}), [key]: normalizedState };
    scheduleTaskDetailSessionPersist();
    return normalizedState;
}

function getStoredTaskDetailViewState(taskId = S.currentTaskId, nodeId = S.selectedNodeId) {
    const key = taskDetailStateKey(taskId, nodeId);
    if (!key) return null;
    return normalizeTaskDetailViewState(S.taskDetailViewStates?.[key]);
}

function consumePendingTaskDetailRestore(nodeId) {
    const pending = S.pendingTaskDetailRestore;
    const normalizedNodeId = String(nodeId || "").trim();
    if (!pending || String(pending.nodeId || "").trim() !== normalizedNodeId) return null;
    S.pendingTaskDetailRestore = null;
    return normalizeTaskDetailViewState(pending.viewState);
}

function readTaskDetailSessionSnapshot() {
    const raw = readSessionJson(TASK_DETAIL_SESSION_KEY);
    const currentTaskId = String(raw?.currentTaskId || "").trim();
    if (!currentTaskId) return null;
    const nodeViewStates = {};
    Object.entries(raw?.nodeViewStates || {}).forEach(([key, value]) => {
        const normalizedKey = String(key || "").trim();
        const normalizedState = normalizeTaskDetailViewState(value);
        if (!normalizedKey || !normalizedState) return;
        nodeViewStates[normalizedKey] = normalizedState;
    });
    return {
        currentTaskId,
        selectedNodeId: String(raw?.selectedNodeId || "").trim(),
        selectedArtifactId: String(raw?.selectedArtifactId || "").trim(),
        treeSelectedRoundByNodeId: normalizeTreeRoundSelections(raw?.treeSelectedRoundByNodeId),
        nodeViewStates,
    };
}

function captureTraceSectionViewState(host) {
    const traceList = host?.querySelector?.(".task-trace-list");
    const traceItems = traceList instanceof HTMLElement
        ? Array.from(traceList.querySelectorAll(".task-trace-step")).map((step, index) => ({
            index,
            key: String(step.dataset.traceKey || "").trim(),
            title: String(step.querySelector(".interaction-step-title")?.textContent || "").trim(),
            open: !!step.open,
            activeToolKey: String(step.querySelector(".task-trace-round-chip.is-active")?.dataset.toolKey || "").trim(),
            roundActiveToolKeys: Array.from(step.querySelectorAll(".task-trace-round-tools")).reduce((acc, roundHost) => {
                const roundKey = String(roundHost.dataset.roundKey || "").trim();
                const activeToolKey = String(roundHost.querySelector(".task-trace-round-chip.is-active")?.dataset.toolKey || "").trim();
                if (roundKey && activeToolKey) acc[roundKey] = activeToolKey;
                return acc;
            }, {}),
        }))
        : [];
    return {
        scrollTop: traceList instanceof HTMLElement ? traceList.scrollTop : 0,
        items: traceItems,
    };
}

function captureTaskDetailViewState() {
    const traceState = captureTraceSectionViewState(U.adFlow);
    const spawnReviewState = captureTraceSectionViewState(U.adSpawnReviews);
    return {
        detailScrollTop: U.detail instanceof HTMLElement ? U.detail.scrollTop : 0,
        traceScrollTop: traceState.scrollTop,
        spawnReviewScrollTop: spawnReviewState.scrollTop,
        artifactListScrollTop: U.artifactList instanceof HTMLElement ? U.artifactList.scrollTop : 0,
        artifactContentScrollTop: U.artifactContent instanceof HTMLElement ? U.artifactContent.scrollTop : 0,
        traceItems: traceState.items,
        spawnReviewItems: spawnReviewState.items,
    };
}

function applyTaskTraceItemViewState(traceList, traceItems) {
    if (!(traceList instanceof HTMLElement) || !Array.isArray(traceItems) || !traceItems.length) return;
    const keyState = new Map();
    const titleState = new Map();
    traceItems.forEach((item) => {
        if (item?.key && !keyState.has(item.key)) keyState.set(item.key, !!item.open);
        if (item?.title && !titleState.has(item.title)) titleState.set(item.title, !!item.open);
    });
    Array.from(traceList.querySelectorAll(".task-trace-step")).forEach((step, index) => {
        const traceKey = String(step.dataset.traceKey || "").trim();
        const title = String(step.querySelector(".interaction-step-title")?.textContent || "").trim();
        const traceState = keyState.has(traceKey)
            ? traceItems.find((item) => item?.key === traceKey)
            : (titleState.has(title) ? traceItems.find((item) => item?.title === title) : traceItems[index]);
        const nextOpen = typeof traceState?.open === "boolean"
            ? traceState.open
            : undefined;
        if (typeof nextOpen === "boolean") step.open = nextOpen;
        const activeToolKey = String(traceState?.activeToolKey || "").trim();
        if (typeof setTraceRoundActiveTool === "function") {
            const roundHosts = Array.from(step.querySelectorAll(".task-trace-round-tools"));
            const roundActiveToolKeys = traceState?.roundActiveToolKeys && typeof traceState.roundActiveToolKeys === "object"
                ? traceState.roundActiveToolKeys
                : null;
            roundHosts.forEach((roundHost, roundIndex) => {
                if (!(roundHost instanceof HTMLElement)) return;
                const roundKey = String(roundHost.dataset.roundKey || "").trim();
                const persistedToolKey = roundKey && roundActiveToolKeys
                    ? String(roundActiveToolKeys[roundKey] || "").trim()
                    : "";
                const fallbackToolKey = roundIndex === 0 ? activeToolKey : "";
                setTraceRoundActiveTool(roundHost, persistedToolKey || fallbackToolKey);
            });
        }
    });
}

function restoreTaskDetailViewState(
    state,
    {
        detail = true,
        trace = true,
        traceItems = true,
        spawnReviews = true,
        spawnReviewItems = true,
        artifactList = true,
        artifactContent = true,
    } = {},
) {
    if (!state || typeof state !== "object") return;
    const getTraceList = () => U.adFlow?.querySelector(".task-trace-list");
    const getSpawnReviewList = () => U.adSpawnReviews?.querySelector(".task-trace-list");
    const getArtifactList = () => U.artifactList;
    const getArtifactContent = () => U.artifactContent;
    const applyScrollPositions = () => {
        const traceList = getTraceList();
        const spawnReviewList = getSpawnReviewList();
        if (detail) setElementScrollTop(U.detail, state.detailScrollTop);
        if (trace) setElementScrollTop(traceList, state.traceScrollTop);
        if (spawnReviews) setElementScrollTop(spawnReviewList, state.spawnReviewScrollTop);
        if (artifactList) setElementScrollTop(getArtifactList(), state.artifactListScrollTop);
        if (artifactContent) setElementScrollTop(getArtifactContent(), state.artifactContentScrollTop);
    };
    if (trace && traceItems) applyTaskTraceItemViewState(getTraceList(), state.traceItems);
    if (spawnReviews && spawnReviewItems) applyTaskTraceItemViewState(getSpawnReviewList(), state.spawnReviewItems);
    applyScrollPositions();
    window.requestAnimationFrame(() => {
        applyScrollPositions();
        window.requestAnimationFrame(applyScrollPositions);
    });
}

function renderTaskSectionHeading(heading, { icon, label, count = 0 } = {}) {
    if (!(heading instanceof HTMLElement)) return;
    heading.innerHTML = `
        <i data-lucide="${esc(icon || "circle")}"></i>
        <span>${esc(label || "")}</span>
        <span class="section-count-badge" data-empty="${count > 0 ? "false" : "true"}">${esc(count)}</span>
    `;
}

function renderFlowHeading(count = 0) {
    renderTaskSectionHeading(U.adFlowHeading, { icon: "workflow", label: "执行流程", count });
    icons();
}

function renderSpawnReviewHeading(count = 0) {
    renderTaskSectionHeading(U.adSpawnReviewsHeading, { icon: "git-branch", label: "派生记录", count });
    icons();
}

function renderArtifactHeading(count = 0) {
    renderTaskSectionHeading(U.artifactHeading, { icon: "files", label: "文件", count });
    icons();
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
    const maxDepth = Math.max(0, normalizeInt(taskDefaults.max_depth ?? taskDefaults.maxDepth, defaultMaxDepth));
    const hardMaxDepth = Math.max(
        defaultMaxDepth,
        normalizeInt(runtime.hard_max_depth ?? runtime.hardMaxDepth, S.taskDefaults.hardMaxDepth),
        TASK_DEPTH_PRESET_MAX,
        maxDepth,
    );
    S.taskDefaults.scope = String(payload?.scope || S.taskDefaults.scope || "global").trim() || "global";
    S.taskDefaults.defaultMaxDepth = defaultMaxDepth;
    S.taskDefaults.hardMaxDepth = hardMaxDepth;
    S.taskDefaults.maxDepth = maxDepth;
    S.taskDefaults.customMode = false;
    S.taskDefaults.customDraft = !TASK_DEPTH_PRESET_VALUES.includes(maxDepth) ? String(maxDepth) : "";
    S.taskDefaults.loading = false;
    S.taskDefaults.saving = false;
    renderTaskDepthControl();
    return S.taskDefaults;
}

function renderTaskDepthControl() {
    if (!U.taskDepthSelect || !U.taskDepthHint || !U.taskDepthCustomWrap || !U.taskDepthCustomInput || !U.taskDepthCustomSave) return;
    const defaultMaxDepth = Math.max(0, normalizeInt(S.taskDefaults.defaultMaxDepth, 1));
    const currentMaxDepth = Math.max(0, normalizeInt(S.taskDefaults.maxDepth, defaultMaxDepth));
    const disabled = S.taskDefaults.loading || S.taskDefaults.saving;
    const currentIsCustomValue = !TASK_DEPTH_PRESET_VALUES.includes(currentMaxDepth);
    const editingCustomValue = !!S.taskDefaults.customMode;
    const customDraft = String(
        editingCustomValue
            ? (S.taskDefaults.customDraft || currentMaxDepth)
            : (S.taskDefaults.customDraft || "")
    );
    const select = U.taskDepthSelect;

    select.innerHTML = "";
    TASK_DEPTH_PRESET_VALUES.forEach((depth) => {
        const option = document.createElement("option");
        option.value = String(depth);
        option.textContent = `${depth} 层`;
        option.selected = !editingCustomValue && depth === currentMaxDepth;
        select.appendChild(option);
    });
    if (currentIsCustomValue && !editingCustomValue) {
        const currentOption = document.createElement("option");
        currentOption.value = String(currentMaxDepth);
        currentOption.textContent = `${currentMaxDepth} 层`;
        currentOption.selected = true;
        select.appendChild(currentOption);
    }
    const customOption = document.createElement("option");
    customOption.value = TASK_DEPTH_CUSTOM_VALUE;
    customOption.textContent = "自定义";
    customOption.selected = editingCustomValue;
    select.appendChild(customOption);
    select.disabled = disabled;
    select.value = editingCustomValue ? TASK_DEPTH_CUSTOM_VALUE : String(currentMaxDepth);
    select.dataset.scope = "global";
    buildResourceSelect(select);
    syncResourceSelectUI(select);

    U.taskDepthCustomWrap.hidden = !editingCustomValue;
    U.taskDepthCustomInput.disabled = disabled;
    U.taskDepthCustomSave.disabled = disabled;
    U.taskDepthCustomInput.value = customDraft;

    if (U.taskDepthHint) {
        U.taskDepthHint.textContent = "";
        U.taskDepthHint.hidden = true;
    }
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
    const nextDepth = Math.max(0, normalizeInt(value, S.taskDefaults.defaultMaxDepth));
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

async function submitCustomTaskDepth() {
    if (!U.taskDepthCustomInput) return;
    if (S.taskDefaults.loading || S.taskDefaults.saving) return;
    const parsed = parseNonNegativeInteger(U.taskDepthCustomInput.value);
    if (parsed === null) {
        showToast({ title: "自定义深度无效", text: "请输入不为负数的整数。", kind: "error" });
        U.taskDepthCustomInput.focus();
        U.taskDepthCustomInput.select();
        return;
    }
    S.taskDefaults.customDraft = String(parsed);
    S.taskDefaults.customMode = !TASK_DEPTH_PRESET_VALUES.includes(parsed);
    await saveTaskDefaultMaxDepth(parsed);
}

function taskCreatedSortValue(task) {
    const parsed = Date.parse(String(task?.created_at || ""));
    return Number.isFinite(parsed) ? parsed : Number.NEGATIVE_INFINITY;
}

function orderedTasks(tasks = S.tasks) {
    if (
        (tasks === S.tasks || tasks == null)
        && S.tasksById
        && typeof S.tasksById === "object"
        && Array.isArray(S.orderedTaskIds)
        && S.orderedTaskIds.length
    ) {
        return S.orderedTaskIds
            .map((taskId) => S.tasksById?.[taskId] || null)
            .filter(Boolean);
    }
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
    return !(S.ceoPauseBusy || S.ceoUploadBusy || S.ceoSessionBusy || S.ceoSessionCatalogBusy);
}

function canCreateCeoSessions() {
    return !(S.ceoPauseBusy || S.ceoUploadBusy || S.ceoSessionBusy || S.ceoSessionCatalogBusy);
}

function canActivateCeoSessions() {
    return !(S.ceoPauseBusy || S.ceoUploadBusy || S.ceoSessionCatalogBusy);
}

function closeCeoSessionMenus({ restoreFocus = false } = {}) {
    const openMenus = [...(U.ceoSessionList?.querySelectorAll(".ceo-session-actions.is-open") || [])];
    let closed = false;
    openMenus.forEach((shell) => {
        shell.classList.remove("is-open");
        shell.closest(".ceo-session-card")?.classList.remove("is-menu-open");
        shell.querySelector(".ceo-session-menu")?.setAttribute("hidden", "hidden");
        const trigger = shell.querySelector("[data-session-menu-toggle]");
        if (trigger) {
            trigger.setAttribute("aria-expanded", "false");
            if (restoreFocus && trigger instanceof HTMLElement) trigger.focus();
        }
        closed = true;
    });
    return closed;
}

function setCeoSessionMenuOpen(sessionId, open, { restoreFocus = false } = {}) {
    const targetId = String(sessionId || "").trim();
    if (!targetId) return false;
    let matched = false;
    [...(U.ceoSessionList?.querySelectorAll(".ceo-session-actions[data-session-menu]") || [])].forEach((shell) => {
        const currentId = String(shell.dataset.sessionMenu || "").trim();
        const shouldOpen = !!open && currentId === targetId;
        const trigger = shell.querySelector("[data-session-menu-toggle]");
        const menu = shell.querySelector(".ceo-session-menu");
        if (currentId === targetId) matched = true;
        shell.classList.toggle("is-open", shouldOpen);
        shell.closest(".ceo-session-card")?.classList.toggle("is-menu-open", shouldOpen);
        if (menu) {
            if (shouldOpen) menu.removeAttribute("hidden");
            else menu.setAttribute("hidden", "hidden");
        }
        if (trigger) {
            trigger.setAttribute("aria-expanded", shouldOpen ? "true" : "false");
            if (!shouldOpen && restoreFocus && currentId === targetId && trigger instanceof HTMLElement) trigger.focus();
        }
    });
    return matched;
}

function syncCeoSessionActions() {
    const mutationDisabled = !canMutateCeoSessions();
    const creationDisabled = !canCreateCeoSessions();
    const activationDisabled = !canActivateCeoSessions();
    const bulkIds = visibleCeoBulkSelectableSessionIds();
    const allSelected = bulkIds.length > 0 && bulkIds.every((sessionId) => isCeoBulkSessionSelected(sessionId));
    if (U.ceoNewSession) U.ceoNewSession.disabled = creationDisabled;
    if (U.ceoSessionBulkToggle) {
        U.ceoSessionBulkToggle.hidden = !S.ceoSessionPanelExpanded;
        U.ceoSessionBulkToggle.disabled = mutationDisabled && !S.ceoBulkMode;
        U.ceoSessionBulkToggle.textContent = S.ceoBulkMode ? "取消" : "多选";
        U.ceoSessionBulkToggle.setAttribute("aria-pressed", S.ceoBulkMode ? "true" : "false");
    }
    if (U.ceoSessionBulkActions) U.ceoSessionBulkActions.hidden = !(S.ceoSessionPanelExpanded && S.ceoBulkMode);
    if (U.ceoSessionBulkDelete) U.ceoSessionBulkDelete.disabled = mutationDisabled || S.ceoSelectedSessionIds.size <= 0;
    if (U.ceoSessionBulkSelectAll) {
        U.ceoSessionBulkSelectAll.disabled = mutationDisabled || bulkIds.length <= 0;
        U.ceoSessionBulkSelectAll.setAttribute("aria-pressed", allSelected ? "true" : "false");
    }
    U.ceoSessionList?.querySelectorAll("[data-session-activate]")?.forEach((button) => {
        const targetId = String(button?.dataset?.sessionActivate || "").trim();
        button.disabled = S.ceoBulkMode ? false : activationDisabled || targetId === activeSessionId();
    });
    U.ceoSessionList?.querySelectorAll("[data-session-bulk-checkbox]")?.forEach((input) => {
        input.disabled = mutationDisabled;
    });
    U.ceoSessionList?.querySelectorAll("[data-session-menu-toggle], [data-session-rename], [data-session-delete]")?.forEach((button) => {
        button.disabled = mutationDisabled;
    });
    if (mutationDisabled || S.ceoBulkMode) closeCeoSessionMenus();
    if (U.ceoSessionTabLocal) U.ceoSessionTabLocal.setAttribute("aria-pressed", S.ceoSessionTab === "local" ? "true" : "false");
    if (U.ceoSessionTabChannel) U.ceoSessionTabChannel.setAttribute("aria-pressed", S.ceoSessionTab === "channel" ? "true" : "false");
    if (U.ceoSessionTabs) {
        U.ceoSessionTabs.style.setProperty("--ceo-session-tab-index", S.ceoSessionTab === "channel" ? "1" : "0");
        U.ceoSessionTabs.dataset.active = S.ceoSessionTab;
    }
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

function syncCeoAttachButton() {
    if (!U.ceoAttach) return;
    U.ceoAttach.disabled = (
        !!S.ceoUploadBusy
        || !!S.ceoSessionBusy
        || !!S.ceoSessionCatalogBusy
        || !activeSessionId()
        || activeSessionIsReadonly()
    );
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
    syncCeoAttachButton();
    syncCeoPrimaryButton();
    syncCeoSessionActions();
    icons();
}

function renderQueuedCeoFollowUps(sessionId = activeSessionId()) {
    if (!U.ceoFollowUpQueue) return;
    const key = String(sessionId || "").trim();
    const items = normalizeCeoQueuedFollowUpList((S.ceoQueuedFollowUps || {})[key] || []);
    U.ceoFollowUpQueue.hidden = !items.length;
    if (!items.length) {
        U.ceoFollowUpQueue.innerHTML = "";
        return;
    }
    U.ceoFollowUpQueue.innerHTML = `
        <div class="ceo-follow-up-queue-title">待发送补充</div>
        <div class="ceo-follow-up-chip-list" role="list">
            ${items.map((item, index) => `
                <div class="ceo-follow-up-chip" role="listitem">
                    <span class="ceo-follow-up-kind">${index + 1}</span>
                    <span class="ceo-follow-up-name">${esc(String(item.text || "").trim() || summarizeUploads(item.uploads || []))}</span>
                    <button type="button" class="ceo-follow-up-remove" data-follow-up-remove="${esc(String(item.id || ""))}" aria-label="删除待发送补充">
                        <i data-lucide="x"></i>
                    </button>
                </div>
            `).join("")}
        </div>
    `;
    icons();
}

function syncCeoPrimaryButton() {
    syncCeoAttachButton();
    if (!U.ceoSend) return;
    if (activeSessionIsReadonly()) {
        U.ceoSend.innerHTML = '<i data-lucide="eye"></i> 渠道会话只读';
        U.ceoSend.disabled = true;
        U.ceoSend.setAttribute("aria-label", "渠道会话只读");
        icons();
        return;
    }
    const composerText = String(U.ceoInput?.value || "").trim();
    const hasComposerPayload = !!composerText || normalizeUploadList(S.ceoUploads).length > 0;
    const isPause = !!S.ceoTurnActive && !hasComposerPayload;
    const label = S.ceoPauseBusy ? "暂停中" : isPause ? "暂停" : "发送";
    const icon = isPause ? "pause" : "send";
    U.ceoSend.innerHTML = `<i data-lucide="${icon}"></i> ${label}`;
    U.ceoSend.disabled = (
        !!S.ceoUploadBusy
        || !!S.ceoPauseBusy
        || !!S.ceoSessionBusy
        || !!S.ceoSessionCatalogBusy
        || !activeSessionId()
        || (!S.ceoTurnActive && !hasComposerPayload)
    );
    U.ceoSend.setAttribute("aria-label", isPause ? "暂停当前 Leader 会话" : "发送消息");
    icons();
}

function finalizePausedCeoTurn(text = "已暂停", { source = null } = {}) {
    const hasExplicitSource = source !== null && source !== undefined && String(source || "").trim();
    const normalizedSource = hasExplicitSource ? normalizeCeoTurnSource(source) : null;
    const normalizedTurnId = normalizeCeoTurnId(arguments?.[1]?.turnId || "");
    const turn = pullActiveCeoTurn(normalizedSource, normalizedTurnId);
    if (!turn?.textEl || !turn.flowEl) return false;
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
    patchCeoSessionSnapshotCache(activeSessionId(), (entry) => {
        const inflightTurn = normalizeCeoSnapshotInflight(entry?.inflight_turn);
        if (!inflightTurn) return entry || {};
        const inflightSource = String(inflightTurn?.source || "").trim().toLowerCase();
        if (normalizedSource && inflightSource && normalizeCeoTurnSource(inflightSource) !== normalizedSource) {
            return entry || {};
        }
        return {
            ...(entry || {}),
            inflight_turn: {
                ...inflightTurn,
                status: "paused",
            },
        };
    });
    return true;
}

function applyCeoState(state = {}, meta = {}) {
    const status = String(state?.status || "").trim().toLowerCase();
    const source = String(meta?.source || state?.source || "").trim().toLowerCase();
    const turnId = String(meta?.turn_id || state?.turn_id || "").trim();
    const running = !!state?.is_running || status === "running";
    const paused = !!state?.paused || status === "paused";
    const activeTurn = source || turnId ? getActiveCeoTurn(source, turnId) : getActiveCeoTurn();
    const hadTurnContext = !!activeTurn || !!S.ceoTurnActive;
    S.ceoTurnActive = running;
    if (patchCeoSessionRuntimeState(activeSessionId(), running)) renderCeoSessions();
    if (!running) S.ceoPauseBusy = false;
    if (running) {
        if (activeTurn) {
            if (source) activeTurn.source = normalizeCeoTurnSource(source);
            if (turnId) activeTurn.turnId = turnId;
        } else if (hadTurnContext && source !== "heartbeat") {
            // Ignore stale running snapshots that arrive after the turn already finished.
            ensureActiveCeoTurn({ source, turnId });
        }
    }
    if (paused) finalizePausedCeoTurn("已暂停", { source, turnId });
    syncCeoSessionActions();
    syncCeoPrimaryButton();
    if (!running && !paused) maybeDispatchQueuedCeoFollowUps();
}

function handleCeoControlAck(payload = {}) {
    const action = String(payload?.action || "").trim().toLowerCase();
    if (action !== "pause") return;
    const source = String(payload?.source || "").trim().toLowerCase();
    const turnId = String(payload?.turn_id || "").trim();
    S.ceoPauseBusy = false;
    if (payload?.accepted === false) {
        syncCeoPrimaryButton();
        showToast({ title: "暂停失败", text: "当前没有可暂停的 Leader 回合。", kind: "error" });
        return;
    }
    S.ceoTurnActive = false;
    if (patchCeoSessionRuntimeState(activeSessionId(), false)) renderCeoSessions();
    finalizePausedCeoTurn("已暂停", { source, turnId });
    syncCeoSessionActions();
    syncCeoPrimaryButton();
    maybeDispatchQueuedCeoFollowUps();
}

function handleCeoError(payload = {}) {
    S.ceoTurnActive = false;
    S.ceoPauseBusy = false;
    if (patchCeoSessionRuntimeState(activeSessionId(), false)) renderCeoSessions();
    syncCeoSessionActions();
    syncCeoPrimaryButton();
    finalizeCeoTurn(`运行出错：${String(payload?.message || "unknown error")}`, payload || {});
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

function sendImmediateCeoMessage({ text = "", uploads = [], scrollMode = "bottom" } = {}) {
    const normalizedText = String(text || "");
    const normalizedUploads = normalizeUploadList(uploads);
    if (!normalizedText.trim() && !normalizedUploads.length) return false;
    if (!S.ceoWs || S.ceoWs.readyState !== WebSocket.OPEN) {
        addMsg("Connection is not ready yet. Please try again in a moment.", "system");
        initCeoWs();
        return false;
    }
    S.ceoWs.send(JSON.stringify({
        type: "client.user_message",
        session_id: activeSessionId(),
        text: normalizedText,
        uploads: normalizedUploads.map((item) => ({
            name: item.name,
            path: item.path,
            mime_type: item.mime_type,
            kind: item.kind,
            size: item.size,
        })),
    }));
    addMsg(hasRenderableText(normalizedText) ? normalizedText : summarizeUploads(normalizedUploads), "user", {
        attachments: normalizedUploads,
        scrollMode,
    });
    const turn = createPendingCeoTurn("user", { scrollMode });
    if (turn) S.ceoPendingTurns.push(turn);
    S.ceoTurnActive = true;
    S.ceoPauseBusy = false;
    setCeoSessionSnapshotCache(activeSessionId(), {
        inflight_turn: {
            source: "user",
            status: "running",
            user_message: {
                content: normalizedText,
                attachments: normalizedUploads,
            },
        },
    });
    if (patchCeoSessionRuntimeState(activeSessionId(), true)) renderCeoSessions();
    syncCeoSessionActions();
    syncCeoPrimaryButton();
    return true;
}

function maybeDispatchQueuedCeoFollowUps() {
    if (S.ceoQueuedFollowUpDispatching || S.ceoTurnActive || S.ceoSessionBusy || S.ceoSessionCatalogBusy) return false;
    const sessionId = activeSessionId();
    if (!sessionId) return false;
    const next = shiftCeoQueuedFollowUp(sessionId);
    if (!next) return false;
    S.ceoQueuedFollowUpDispatching = true;
    try {
        const sent = sendImmediateCeoMessage({
            text: next.text,
            uploads: next.uploads,
            scrollMode: "bottom",
        });
        if (!sent) {
            setCeoQueuedFollowUps(sessionId, [next, ...getCeoQueuedFollowUps(sessionId)]);
            return false;
        }
        return true;
    } finally {
        S.ceoQueuedFollowUpDispatching = false;
    }
}

function handleCeoPrimaryAction() {
    const text = String(U.ceoInput?.value || "");
    const uploads = normalizeUploadList(S.ceoUploads);
    if (S.ceoTurnActive && !text.trim() && !uploads.length) {
        requestCeoPause();
        return;
    }
    if (activeSessionIsReadonly()) {
        showToast({ title: "渠道会话只读", text: "当前只能查看渠道历史消息，不能在 Leader 面板直接发送。", kind: "info" });
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
        syncActiveCeoComposerDraft();
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
    syncActiveCeoComposerDraft();
    renderPendingCeoUploads();
}

function scrollCeoFeedToBottom() {
    if (!U.ceoFeed) return;
    const applyBottom = () => {
        if (!U.ceoFeed) return;
        U.ceoFeed.scrollTop = U.ceoFeed.scrollHeight;
    };
    applyBottom();
    window.requestAnimationFrame(applyBottom);
}

let ceoFeedBatchDepth = 0;

function withCeoFeedBatch(mutator, { scrollMode = "preserve" } = {}) {
    if (typeof mutator !== "function") return null;
    if (!U.ceoFeed) return mutator();
    const prevTop = U.ceoFeed.scrollTop;
    ceoFeedBatchDepth += 1;
    let result = null;
    try {
        result = mutator();
    } finally {
        ceoFeedBatchDepth = Math.max(0, ceoFeedBatchDepth - 1);
    }
    if (ceoFeedBatchDepth > 0) return result;
    if (scrollMode === "bottom") {
        scrollCeoFeedToBottom();
    } else {
        const maxTop = Math.max(0, U.ceoFeed.scrollHeight - U.ceoFeed.clientHeight);
        U.ceoFeed.scrollTop = Math.max(0, Math.min(prevTop, maxTop));
    }
    return result;
}

function mutateCeoFeed(mutator, { scrollMode = "preserve" } = {}) {
    if (typeof mutator !== "function") return null;
    if (!U.ceoFeed) return mutator();
    if (ceoFeedBatchDepth > 0) return mutator();
    const prevTop = U.ceoFeed.scrollTop;
    const result = mutator();
    if (scrollMode === "bottom") {
        scrollCeoFeedToBottom();
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
        el.innerHTML = `<div class="${contentClass}">${content}${attachmentMarkup}</div>`;
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

function ceoInflightTurnHasVisibleAssistantState(snapshot = null) {
    if (!snapshot || typeof snapshot !== "object") return false;
    const status = String(snapshot.status || "").trim().toLowerCase();
    const assistantText = String(snapshot.assistant_text || "").trim();
    const turnId = String(snapshot?.turn_id || "").trim();
    const toolEvents = normalizeCeoSnapshotToolEvents(snapshot.tool_events);
    const executionTraceSummary = normalizeCeoSnapshotExecutionTraceSummary(snapshot.execution_trace_summary);
    return !!assistantText || toolEvents.length > 0 || !!executionTraceSummary || status === "paused" || status === "error";
}

function ceoNeedsAssistantTurn(snapshot = null) {
    if (!snapshot || typeof snapshot !== "object") return false;
    const source = normalizeCeoTurnSource(snapshot?.source || "");
    const status = String(snapshot.status || "").trim().toLowerCase();
    return ceoInflightTurnHasVisibleAssistantState(snapshot) || (source !== "heartbeat" && status === "running");
}

const CEO_ASSISTANT_LOADING_LABEL = "正在处理中";
const CEO_ASSISTANT_LOADING_TEXTS = new Set([
    "处理中...",
    "正在处理中...",
    "正在请求 CEO 模型生成下一步响应...",
]);

function isCeoAssistantLoadingText(text = "") {
    const normalizedText = String(text || "").trim();
    return !!normalizedText && CEO_ASSISTANT_LOADING_TEXTS.has(normalizedText);
}

function syncCeoAssistantLoadingAria(textEl, label = "") {
    if (!textEl || typeof textEl.setAttribute !== "function" || typeof textEl.removeAttribute !== "function") return;
    const normalizedLabel = String(label || "").trim();
    if (normalizedLabel) {
        textEl.setAttribute("role", "status");
        textEl.setAttribute("aria-label", normalizedLabel);
        return;
    }
    textEl.removeAttribute("role");
    textEl.removeAttribute("aria-label");
}

function syncCeoTurnLoadingOnlyState(turn, isLoadingOnly = false) {
    const turnEl = turn?.el;
    if (!turnEl?.classList) return;
    if (isLoadingOnly) turnEl.classList.add("ceo-turn-loading-only");
    else turnEl.classList.remove("ceo-turn-loading-only");
}

function renderCeoAssistantLoadingMarkup() {
    return `<span class="assistant-loading-indicator interaction-step-icon is-spinning" aria-hidden="true"><i data-lucide="loader-circle"></i></span>`;
}

function renderCeoAssistantLoadingState(turn, label = CEO_ASSISTANT_LOADING_LABEL) {
    if (!turn?.textEl) return;
    turn.textEl.textContent = "";
    turn.textEl.innerHTML = renderCeoAssistantLoadingMarkup();
    turn.textEl.classList.add("pending");
    turn.textEl.classList.add("assistant-text-loading");
    turn.textEl.classList.remove("markdown-content");
    syncCeoAssistantLoadingAria(turn.textEl, label);
    syncCeoTurnLoadingOnlyState(turn, true);
    icons();
}

function renderCeoAssistantTextIntoTurn(turn, text = "", { status = "" } = {}) {
    if (!turn?.textEl) return;
    const normalizedText = String(text || "").trim();
    const normalizedStatus = String(status || "").trim().toLowerCase();
    if (!normalizedText) {
        if (normalizedStatus === "paused") {
            turn.textEl.textContent = "已暂停";
            turn.textEl.classList.remove("pending");
            turn.textEl.classList.remove("markdown-content");
            turn.textEl.classList.remove("assistant-text-loading");
            syncCeoAssistantLoadingAria(turn.textEl);
            syncCeoTurnLoadingOnlyState(turn, false);
            return;
        }
        renderCeoAssistantLoadingState(turn);
        return;
    }
    if (normalizedStatus !== "paused" && normalizedStatus !== "error" && isCeoAssistantLoadingText(normalizedText)) {
        renderCeoAssistantLoadingState(turn, normalizedText);
        return;
    }
    turn.textEl.innerHTML = renderMarkdown(normalizedText);
    turn.textEl.classList.remove("pending");
    turn.textEl.classList.remove("assistant-text-loading");
    turn.textEl.classList.add("markdown-content");
    syncCeoAssistantLoadingAria(turn.textEl);
    syncCeoTurnLoadingOnlyState(turn, false);
}

function resetCeoToolFlow(turn) {
    if (!turn?.listEl || !turn?.flowEl) return;
    turn.listEl.innerHTML = "";
    turn.steps = 0;
    turn.hasError = false;
    turn.historyExpanded = false;
    turn.flowEl.hidden = true;
    turn.flowEl.open = false;
    if (turn.footerEl instanceof HTMLElement) turn.footerEl.hidden = true;
    if (turn.toggleEl instanceof HTMLButtonElement) {
        turn.toggleEl.textContent = "展开全部";
        turn.toggleEl.setAttribute("aria-expanded", "false");
    }
}

function renderCeoToolEventsIntoTurn(turn, toolEvents = [], { source = "" } = {}) {
    if (!turn?.listEl || !turn?.flowEl) return 0;
    const normalizedSource = normalizeCeoTurnSource(source || turn.source || "user");
    const events = normalizeCeoSnapshotToolEvents(toolEvents);
    resetCeoToolFlow(turn);
    events.forEach((event) => {
        applyCeoToolEventToTurn(turn, {
            ...(event && typeof event === "object" ? event : {}),
            source: String(event?.source || normalizedSource).trim().toLowerCase() || normalizedSource,
        });
    });
    if (events.length) syncCeoTurnLoadingOnlyState(turn, false);
    if (!events.length) updateCeoTurnMeta(turn, "等待工具开始...");
    return events.length;
}

function renderCeoStageTraceIntoTurn(turn, executionTraceSummary = null) {
    if (!turn?.listEl || !turn?.flowEl) return 0;
    const summary = normalizeCeoSnapshotExecutionTraceSummary(executionTraceSummary);
    if (!summary?.stages?.length) {
        resetCeoToolFlow(turn);
        updateCeoTurnMeta(turn, "绛夊緟宸ュ叿寮€濮?..");
        return 0;
    }
    if (typeof renderTraceStep !== "function"
        || typeof renderExecutionStageRounds !== "function"
        || typeof stageTraceStatus !== "function"
        || typeof formatExecutionStageTitle !== "function"
        || typeof displayTaskStageStatus !== "function") {
        return 0;
    }
    syncCeoTurnLoadingOnlyState(turn, false);
    resetCeoToolFlow(turn);
    turn.listEl.classList?.add?.("task-trace-list");
    turn.listEl.innerHTML = summary.stages.map((stage, index) => renderTraceStep({
        traceKey: `ceo:stage:${stage.stage_id || stage.stage_index || index}`,
        title: formatExecutionStageTitle(stage),
        status: stageTraceStatus(stage),
        statusLabel: displayTaskStageStatus(stage.status),
        open: index === summary.stages.length - 1,
        bodyHtml: renderExecutionStageRounds(stage),
    })).join("");
    if (typeof bindTraceRoundToolStrips === "function") bindTraceRoundToolStrips(turn.listEl);
    const stageCount = summary.stages.length;
    const roundCount = summary.stages.reduce((sum, stage) => sum + (Array.isArray(stage?.rounds) ? stage.rounds.length : 0), 0);
    turn.steps = roundCount || stageCount;
    turn.flowEl.hidden = false;
    turn.flowEl.open = true;
    updateCeoTurnMeta(turn, `${stageCount} 个阶段 · ${roundCount} 轮工具`);
    if (typeof bindTraceOutputAutoLoad === "function") bindTraceOutputAutoLoad(turn.listEl);
    if (typeof hydrateTraceOutputBlocks === "function") {
        Array.from(turn.listEl.querySelectorAll?.(".task-trace-step[open]") || []).forEach((item) => {
            if (item instanceof HTMLElement) hydrateTraceOutputBlocks(item);
        });
    }
    return turn.steps;
}

function patchCeoInflightTurn(snapshot = null, { sessionId = "" } = {}) {
    if (!snapshot || typeof snapshot !== "object") return false;
    const source = normalizeCeoTurnSource(snapshot?.source || "user");
    const turnId = normalizeCeoTurnId(snapshot?.turn_id || "");
    const status = String(snapshot.status || "").trim().toLowerCase();
    const existingTurn = getActiveCeoTurn(source, turnId);
    if (!existingTurn && !ceoNeedsAssistantTurn(snapshot)) return false;
    const turn = existingTurn || ensureActiveCeoTurn({ source, turnId });
    if (!turn?.textEl || !turn?.flowEl) return false;
    if (turnId) turn.turnId = turnId;
    mutateCeoFeed(() => {
        renderCeoAssistantTextIntoTurn(turn, snapshot?.assistant_text || "", { status });
        const stageRoundCount = renderCeoStageTraceIntoTurn(turn, snapshot?.execution_trace_summary || null);
        const toolCount = stageRoundCount || renderCeoToolEventsIntoTurn(turn, snapshot?.tool_events || [], { source });
        if (!stageRoundCount && !toolCount) {
            if (status === "paused") updateCeoTurnMeta(turn, "已暂停");
            else if (status === "error") updateCeoTurnMeta(turn, "运行出错");
            else updateCeoTurnMeta(turn, "等待工具开始...");
        }
        if (stageRoundCount || toolCount) {
            turn.flowEl.hidden = false;
            turn.flowEl.open = status !== "completed";
        }
        icons();
    }, { scrollMode: "preserve" });
    const targetSessionId = String(sessionId || activeSessionId()).trim();
    if (targetSessionId) setCeoSessionSnapshotCache(targetSessionId, { inflight_turn: snapshot });
    return true;
}

function restoreCeoInflightTurn(snapshot = null, { sessionId = "" } = {}) {
    if (!snapshot || typeof snapshot !== "object") return;
    const source = normalizeCeoTurnSource(snapshot?.source || "");
    const isHeartbeat = source === "heartbeat";
    const userMessage = snapshot.user_message && typeof snapshot.user_message === "object" ? snapshot.user_message : null;
    if (userMessage && !isHeartbeat) {
        const attachments = normalizeUploadList(userMessage.attachments);
        const text = hasRenderableText(userMessage.content) ? String(userMessage.content || "") : summarizeUploads(attachments);
        addMsg(text, "user", { attachments, scrollMode: "preserve" });
    }
    patchCeoInflightTurn(snapshot, { sessionId });
    const status = String(snapshot.status || "").trim().toLowerCase();
    const assistantText = String(snapshot.assistant_text || "").trim();
    if (status === "paused") {
        finalizePausedCeoTurn(assistantText || "已暂停", { source, turnId });
        return;
    }
    if (status === "error") {
        const errorMessage = String(snapshot?.last_error?.message || "").trim() || "unknown error";
        finalizeCeoTurn(`运行出错：${errorMessage}`, { source, turnId });
    }
}

function renderPersistedCeoAssistantTurn(item = {}) {
    const executionTraceSummary = normalizeCeoSnapshotExecutionTraceSummary(item?.execution_trace_summary);
    const toolEvents = normalizeCeoSnapshotToolEvents(item?.tool_events);
    const content = String(item?.content || "");
    if (!executionTraceSummary && !toolEvents.length) {
        addMsg(content, "system", { markdown: true, scrollMode: "preserve" });
        return;
    }
    const turn = createPendingCeoTurn("history", { scrollMode: "preserve" });
    if (!turn) {
        addMsg(content, "system", { markdown: true, scrollMode: "preserve" });
        return;
    }
    S.ceoPendingTurns.push(turn);
    withCeoFeedBatch(() => {
        renderCeoAssistantTextIntoTurn(turn, content);
        const stageRoundCount = renderCeoStageTraceIntoTurn(turn, executionTraceSummary);
        if (!stageRoundCount) renderCeoToolEventsIntoTurn(turn, toolEvents, { source: "history" });
        turn.flowEl.hidden = false;
        turn.flowEl.open = false;
        icons();
    }, { scrollMode: "preserve" });
    finalizeCeoTurn(content, { source: "history" });
}

function renderCeoSnapshot(messages = [], inflightTurn = null, { sessionId = "" } = {}) {
    const shouldScrollToLatest = !!S.ceoScrollToLatestOnSnapshot;
    S.ceoScrollToLatestOnSnapshot = false;
    const targetSessionId = String(sessionId || activeSessionId()).trim();
    withCeoFeedBatch(() => {
        resetCeoFeed();
        messages.forEach((item) => {
            const role = String(item?.role || "").trim().toLowerCase();
            const content = String(item?.content || "");
            const attachments = normalizeUploadList(item?.attachments);
            if (role === "user") {
                if (!content.trim() && !attachments.length) return;
                addMsg(hasRenderableText(content) ? content : summarizeUploads(attachments), "user", {
                    attachments,
                    scrollMode: "preserve",
                });
                return;
            }
            if (role === "assistant") {
                renderPersistedCeoAssistantTurn(item);
                return;
            }
            if (role === "system" && content.trim()) {
                addMsg(content, "system", { markdown: true, scrollMode: "preserve" });
            }
        });
        restoreCeoInflightTurn(
            dedupeInflightUserMessageAgainstMessages(messages, inflightTurn),
            { sessionId: targetSessionId }
        );
        if (targetSessionId) {
            setCeoSessionSnapshotCache(targetSessionId, {
                messages,
                inflight_turn: inflightTurn,
            });
        }
    }, {
        scrollMode: shouldScrollToLatest ? "bottom" : "preserve",
    });
}

function createPendingCeoTurn(source = "user", { scrollMode = "preserve" } = {}) {
    return mutateCeoFeed(() => {
        if (!U.ceoFeed) return null;
        const el = document.createElement("div");
        el.className = "message system ceo-turn-message ceo-turn-loading-only";
        el.innerHTML = `
            <div class="msg-content ceo-turn-content">
                <div class="assistant-text pending">${renderCeoAssistantLoadingMarkup()}</div>
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
            turnId: "",
            source: String(source || "").trim().toLowerCase() || "user",
        };
        turn.textEl?.classList?.add?.("assistant-text-loading");
        syncCeoAssistantLoadingAria(turn.textEl, CEO_ASSISTANT_LOADING_LABEL);
        toggleButton?.addEventListener("click", (event) => {
            event.preventDefault();
            event.stopPropagation();
            toggleCeoToolHistory(turn);
        });
        icons();
        return turn;
    }, { scrollMode });
}

function normalizeCeoTurnSource(source = "") {
    const normalized = String(source || "").trim().toLowerCase();
    return normalized || "user";
}

function normalizeCeoTurnId(turnId = "") {
    return String(turnId || "").trim();
}

function findActiveCeoTurnIndex({ source = null, turnId = "" } = {}) {
    const expectedTurnId = normalizeCeoTurnId(turnId);
    if (expectedTurnId) {
        for (let index = S.ceoPendingTurns.length - 1; index >= 0; index -= 1) {
            const turn = S.ceoPendingTurns[index];
            if (!turn || turn.finalized) continue;
            if (normalizeCeoTurnId(turn.turnId) === expectedTurnId) return index;
        }
    }
    const hasExpectedSource = source !== null && source !== undefined;
    const expectedSource = hasExpectedSource ? normalizeCeoTurnSource(source) : "";
    for (let index = S.ceoPendingTurns.length - 1; index >= 0; index -= 1) {
        const turn = S.ceoPendingTurns[index];
        if (!turn || turn.finalized) continue;
        if (!hasExpectedSource) return index;
        if (normalizeCeoTurnSource(turn.source) === expectedSource) return index;
    }
    return -1;
}

function getActiveCeoTurn(source = null, turnId = "") {
    const index = findActiveCeoTurnIndex({ source, turnId });
    return index >= 0 ? S.ceoPendingTurns[index] || null : null;
}

function pullActiveCeoTurn(source = null, turnId = "") {
    const index = findActiveCeoTurnIndex({ source, turnId });
    if (index < 0) return null;
    const [turn] = S.ceoPendingTurns.splice(index, 1);
    return turn || null;
}

function discardActiveCeoTurn({ source = "", turnId = "" } = {}) {
    const normalizedSource = String(source || "").trim() ? normalizeCeoTurnSource(source) : null;
    const normalizedTurnId = normalizeCeoTurnId(turnId);
    const turn = pullActiveCeoTurn(normalizedSource, normalizedTurnId);
    if (!turn) return false;
    const discarded = mutateCeoFeed(() => {
        turn.finalized = true;
        turn.el?.remove?.();
        return true;
    }, { scrollMode: "preserve" });
    if (discarded) {
        patchCeoSessionSnapshotCache(activeSessionId(), (entry) => {
            const inflightTurn = normalizeCeoSnapshotInflight(entry?.inflight_turn);
            if (!inflightTurn) return entry || {};
            const inflightTurnId = normalizeCeoTurnId(inflightTurn?.turn_id);
            if (normalizedTurnId && inflightTurnId && inflightTurnId !== normalizedTurnId) {
                return entry || {};
            }
            const inflightSource = String(inflightTurn?.source || "").trim().toLowerCase();
            if (normalizedSource && inflightSource && normalizeCeoTurnSource(inflightSource) !== normalizedSource) {
                return entry || {};
            }
            return {
                ...(entry || {}),
                inflight_turn: null,
            };
        });
    }
    return discarded;
}

function hasRunningCeoToolStep(turn) {
    return !!turn?.listEl?.querySelector?.(".interaction-step.running");
}

function discardPendingCeoTurns({ force = false, source = null, turnId = "" } = {}) {
    const hasExpectedSource = source !== null && source !== undefined && String(source || "").trim();
    const expectedSource = hasExpectedSource ? normalizeCeoTurnSource(source) : "";
    const expectedTurnId = normalizeCeoTurnId(turnId);
    const removed = [];
    for (let index = S.ceoPendingTurns.length - 1; index >= 0; index -= 1) {
        const turn = S.ceoPendingTurns[index];
        if (!turn || turn.finalized) {
            S.ceoPendingTurns.splice(index, 1);
            continue;
        }
        if (expectedTurnId && normalizeCeoTurnId(turn.turnId) !== expectedTurnId) continue;
        if (hasExpectedSource && normalizeCeoTurnSource(turn.source) !== expectedSource) continue;
        if (!force && hasRunningCeoToolStep(turn)) continue;
        const [removedTurn] = S.ceoPendingTurns.splice(index, 1);
        if (removedTurn) removed.push(removedTurn);
    }
    if (!removed.length) return false;
    return mutateCeoFeed(() => {
        removed.forEach((turn) => {
            turn.finalized = true;
            turn.el?.remove?.();
        });
        return true;
    }, { scrollMode: "preserve" });
}

function ensureActiveCeoTurn({ source = "", turnId = "" } = {}) {
    const normalizedSource = normalizeCeoTurnSource(source);
    const normalizedTurnId = normalizeCeoTurnId(turnId);
    const existing = getActiveCeoTurn(normalizedSource, normalizedTurnId);
    if (existing) {
        existing.source = normalizedSource;
        if (normalizedTurnId) existing.turnId = normalizedTurnId;
        return existing;
    }
    const created = createPendingCeoTurn(normalizedSource);
    if (created && normalizedTurnId) created.turnId = normalizedTurnId;
    if (created) S.ceoPendingTurns.push(created);
    return created;
}

function updateCeoTurnMeta(turn, stateLabel) {
    if (!turn?.metaEl) return;
    const stepLabel = turn.steps > 0 ? `${turn.steps} 个步骤` : "等待工具开始...";
    const nextStateLabel = String(stateLabel || "").trim();
    turn.metaEl.textContent = nextStateLabel && nextStateLabel !== stepLabel ? `${stepLabel} - ${nextStateLabel}` : stepLabel;
}

function findCeoToolStep(turn, { toolCallId = "", toolName = "" } = {}) {
    if (!turn?.listEl) return null;
    const items = [...turn.listEl.querySelectorAll(".interaction-step")];
    if (toolCallId) {
        for (let index = items.length - 1; index >= 0; index -= 1) {
            const item = items[index];
            if (String(item?.dataset?.toolCallId || "").trim() === toolCallId) return item;
        }
        return null;
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

function ceoPayloadStatus(detail = "") {
    const payload = parseJsonObjectText(detail);
    return {
        payload,
        status: String(payload?.status || "").trim().toLowerCase(),
    };
}

function resolveCeoToolEventStatus(event = {}) {
    const fallback = String(event?.status || "running").trim().toLowerCase() || "running";
    const { status } = ceoPayloadStatus(event?.text || "");
    if (status === "background_running") return "running";
    if (status === "completed") return "success";
    if (["stopped", "failed", "error", "not_found", "unavailable"].includes(status)) return "error";
    return fallback;
}

function buildCeoBackgroundPayload({ elapsedSeconds = Number.NaN, snapshotSummary = "", waitSeconds = Number.NaN } = {}) {
    const payload = { status: "background_running" };
    if (Number.isFinite(elapsedSeconds) && elapsedSeconds >= 0) payload.elapsed_seconds = elapsedSeconds;
    if (Number.isFinite(waitSeconds) && waitSeconds > 0) payload.recommended_wait_seconds = waitSeconds;
    if (snapshotSummary) payload.runtime_snapshot = { summary_text: snapshotSummary };
    return payload;
}

function clearCeoBackgroundDetailState(item) {
    if (!(item instanceof HTMLElement)) return;
    delete item.dataset.backgroundRunning;
    delete item.dataset.backgroundSummary;
    delete item.dataset.backgroundWaitSeconds;
}

function updateCeoBackgroundDetail(item) {
    if (!(item instanceof HTMLElement) || item.dataset.backgroundRunning !== "true") return;
    const toolName = String(item.dataset.toolName || "tool").trim() || "tool";
    const kind = String(item.dataset.progressKind || "").trim();
    const snapshotSummary = String(item.dataset.backgroundSummary || "").trim();
    const waitSeconds = Number.parseFloat(String(item.dataset.backgroundWaitSeconds || ""));
    const elapsedSeconds = resolveRuntimeSeconds(item);
    setCeoToolStepOutput(item, ceoFriendlyToolDetail(
        toolName,
        JSON.stringify(
            buildCeoBackgroundPayload({
                elapsedSeconds,
                snapshotSummary,
                waitSeconds,
            })
        ),
        "running",
        kind
    ));
}

function syncCeoBackgroundDetailState(item, { rawDetail = "" } = {}) {
    if (!(item instanceof HTMLElement)) return;
    const { payload, status } = ceoPayloadStatus(rawDetail);
    if (status !== "background_running") {
        clearCeoBackgroundDetailState(item);
        return;
    }
    item.dataset.backgroundRunning = "true";
    item.dataset.backgroundSummary = String(payload?.runtime_snapshot?.summary_text || "").trim();
    const waitSeconds = Number(payload?.recommended_wait_seconds);
    if (Number.isFinite(waitSeconds) && waitSeconds > 0) {
        item.dataset.backgroundWaitSeconds = String(waitSeconds);
    } else {
        delete item.dataset.backgroundWaitSeconds;
    }
}

function ceoFriendlyToolDetail(toolName = "", detail = "", status = "running", kind = "") {
    const normalizedTool = String(toolName || "").trim().toLowerCase();
    const raw = String(detail || "").trim();
    const lower = raw.toLowerCase();
    const normalizedKind = String(kind || "").trim().toLowerCase();
    const { payload, status: payloadStatus } = ceoPayloadStatus(raw);
    if (!raw) {
        if (status === "error") return "处理失败，请检查错误信息";
        if (status === "success") return "处理完成";
        return "正在处理中...";
    }
    if (payloadStatus === "background_running") {
        const snapshotSummary = String(payload?.runtime_snapshot?.summary_text || "").trim();
        const elapsedSeconds = Number(payload?.elapsed_seconds);
        const waitSeconds = Number(payload?.recommended_wait_seconds);
        const fragments = ["已转入后台继续运行"];
        if (Number.isFinite(elapsedSeconds) && elapsedSeconds >= 0) {
            fragments.push(`已等待 ${Math.round(elapsedSeconds)} 秒`);
        }
        if (snapshotSummary) fragments.push(snapshotSummary);
        if (Number.isFinite(waitSeconds) && waitSeconds > 0) {
            fragments.push(`建议约 ${Math.round(waitSeconds)} 秒后再跟进`);
        }
        return fragments.join("。");
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
    const { status: payloadStatus } = ceoPayloadStatus(detail);
    if (payloadStatus === "background_running") {
        return { icon: "clock-3", spinning: false, meta: "后台运行中" };
    }
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

function normalizeInteractionDetailText(text = "") {
    return readableText(text, { decodeEscapes: true, emptyText: "" }).replace(/\r\n?/g, "\n").trim();
}

function buildInteractionPreviewText(text = "", maxLines = CEO_TOOL_OUTPUT_PREVIEW_LINES) {
    const normalized = normalizeInteractionDetailText(text);
    if (!normalized) return "";
    return normalized
        .split("\n")
        .map((line) => line.trimEnd())
        .slice(-maxLines)
        .join("\n");
}

function isInteractionDetailCollapsible(text = "") {
    const normalized = normalizeInteractionDetailText(text);
    if (!normalized) return false;
    return normalized.split("\n").length > CEO_TOOL_OUTPUT_PREVIEW_LINES
        || normalized.length > CEO_TOOL_OUTPUT_PREVIEW_MAX_CHARS;
}

function syncCeoToolStepOutput(item) {
    if (!(item instanceof HTMLElement)) return;
    const previewEl = item.querySelector(".interaction-step-preview");
    const detailEl = item.querySelector(".interaction-step-detail");
    const disclosureEl = item.querySelector(".interaction-step-disclosure");
    const detailText = normalizeInteractionDetailText(item.dataset.detailText || "");
    const previewText = buildInteractionPreviewText(detailText) || detailText;
    const collapsible = isInteractionDetailCollapsible(detailText);
    const expanded = collapsible && item.dataset.outputExpanded === "true";
    if (!collapsible) item.dataset.outputExpanded = "false";
    if (previewEl instanceof HTMLElement) {
        previewEl.textContent = previewText;
        previewEl.hidden = expanded || !previewText;
        previewEl.title = collapsible ? detailText : "";
    }
    if (detailEl instanceof HTMLElement) {
        detailEl.textContent = detailText;
        detailEl.hidden = !expanded || !collapsible;
    }
    item.classList.toggle("is-output-collapsible", collapsible);
    item.classList.toggle("is-output-expanded", expanded);
    if (disclosureEl instanceof HTMLButtonElement) {
        disclosureEl.hidden = !collapsible;
        disclosureEl.setAttribute("aria-expanded", expanded ? "true" : "false");
        disclosureEl.setAttribute("aria-label", expanded ? "Collapse tool output" : "Expand tool output");
        disclosureEl.title = expanded ? "Collapse output" : "Expand output";
    }
}

function setCeoToolStepOutput(item, detail = "") {
    if (!(item instanceof HTMLElement)) return;
    item.dataset.detailText = normalizeInteractionDetailText(detail);
    syncCeoToolStepOutput(item);
}

function toggleCeoToolStepOutput(item) {
    if (!(item instanceof HTMLElement)) return;
    if (!isInteractionDetailCollapsible(item.dataset.detailText || "")) return;
    item.dataset.outputExpanded = item.dataset.outputExpanded === "true" ? "false" : "true";
    syncCeoToolStepOutput(item);
    if (item.dataset.outputExpanded === "true") {
        void ensureCeoToolStepFullOutput(item);
    }
}

function trimCeoToolSteps(turn) {
    if (!turn?.listEl) return;
    const items = Array.from(turn.listEl.children).filter((item) => item instanceof HTMLElement);
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

function applyCeoToolStepState(item, { status = "running", toolName = "tool", detail = "", toolCallId = "", kind = "", stage = null, allowEmptyOutput = false } = {}) {
    if (!(item instanceof HTMLElement)) return;
    const statusLabel = ({ running: "进行中", success: "完成", error: "出错" })[status] || "更新";
    const resolvedStage = stage || ceoToolStage(toolName, detail, status);
    item.className = `interaction-step ${status}`;
    item.dataset.stepState = status;
    item.dataset.toolName = String(toolName || "tool").trim() || "tool";
    if (toolCallId) item.dataset.toolCallId = toolCallId;
    item.dataset.progressKind = String(kind || "").trim();
    const titleEl = item.querySelector(".interaction-step-title");
    const startedEl = item.querySelector(".interaction-step-started");
    const statusEl = item.querySelector(".interaction-step-status");
    const iconWrap = item.querySelector(".interaction-step-icon");
    if (titleEl) titleEl.textContent = ceoFriendlyToolName(toolName);
    if (startedEl instanceof HTMLElement) {
        const startedLabel = formatCompactTime(item.dataset.startedAt || "");
        startedEl.hidden = !startedLabel;
        startedEl.textContent = startedLabel ? `Started ${startedLabel}` : "";
        if (startedLabel) startedEl.title = formatSessionTime(item.dataset.startedAt || "");
        else startedEl.removeAttribute("title");
    }
    if (statusEl) statusEl.textContent = statusLabel;
    setCeoToolStepOutput(item, allowEmptyOutput ? detail : (detail || `${ceoFriendlyToolName(toolName)}${statusLabel}`));
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
    const status = String(element.dataset.stepState || element.dataset.traceStatus || "").trim().toLowerCase();
    const startedAt = parseIsoTimestamp(element.dataset.startedAt || "");
    if (startedAt === null) return null;
    const finishedAt = parseIsoTimestamp(element.dataset.finishedAt || "");
    const liveElapsed = Math.max(0, Math.floor(((finishedAt !== null ? finishedAt : Date.now()) - startedAt) / 1000));
    if (status === "running" && finishedAt === null) {
        return Number.isFinite(explicitElapsed) && explicitElapsed >= 0
            ? Math.max(explicitElapsed, liveElapsed)
            : liveElapsed;
    }
    if (Number.isFinite(explicitElapsed) && explicitElapsed >= 0) return explicitElapsed;
    return liveElapsed;
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
        updateCeoBackgroundDetail(item);
    });
    document.querySelectorAll(".task-trace-step").forEach((item) => {
        if (!(item instanceof HTMLElement)) return;
        const runtimeEl = item.querySelector(".task-trace-runtime");
        if (!(runtimeEl instanceof HTMLElement)) return;
        updateRuntimeBadge(item, runtimeEl);
    });
    if (typeof refreshTaskWorkerState === "function") {
        refreshTaskWorkerState({ render: S.view === "tasks" });
    }
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

function applyCeoToolEventToTurn(turn, event = {}) {
    if (!turn?.listEl || !turn?.flowEl) return null;
    const status = resolveCeoToolEventStatus(event);
    const toolName = String(event.tool_name || "tool").trim() || "tool";
    const rawText = String(event.text || "").trim();
    const eventKind = String(event.kind || "").trim().toLowerCase();
    const detail = status === "running" && eventKind === "tool_start"
        ? ""
        : ceoFriendlyToolDetail(toolName, rawText, status, event.kind);
    const outputRef = normalizeTraceOutputRef(event.output_ref || "");
    const toolCallId = String(event.tool_call_id || "").trim();
    let item = findCeoToolStep(turn, { toolCallId, toolName });
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
                    <time class="interaction-step-started" hidden></time>
                    <span class="interaction-step-runtime" hidden></span>
                    <span class="interaction-step-status"></span>
                    <button type="button" class="interaction-step-disclosure" hidden aria-expanded="false" aria-label="Expand tool output"></button>
                </span>
            </div>
            <div class="interaction-step-preview" hidden></div>
            <div class="interaction-step-detail" hidden></div>
        `;
        item.dataset.outputExpanded = "false";
        item.querySelector(".interaction-step-disclosure")?.addEventListener("click", (interactionEvent) => {
            interactionEvent.preventDefault();
            interactionEvent.stopPropagation();
            mutateCeoFeed(() => {
                toggleCeoToolStepOutput(item);
            }, { scrollMode: "preserve" });
        });
        turn.listEl.appendChild(item);
    }
    const eventTimestamp = String(event.timestamp || "").trim();
    const eventElapsed = Number.parseFloat(String(event.elapsed_seconds ?? ""));
    if (!item.dataset.startedAt && eventTimestamp) item.dataset.startedAt = eventTimestamp;
    if (status === "success" || status === "error") {
        if (eventTimestamp) item.dataset.finishedAt = eventTimestamp;
    } else {
        delete item.dataset.finishedAt;
    }
    if (Number.isFinite(eventElapsed) && eventElapsed >= 0) {
        item.dataset.elapsedSeconds = String(eventElapsed);
    } else if (status === "running") {
        delete item.dataset.elapsedSeconds;
    }
    applyCeoToolStepState(item, {
        status,
        toolName,
        detail,
        toolCallId,
        kind: eventKind,
        stage,
        allowEmptyOutput: status === "running" && eventKind === "tool_start",
    });
    if (outputRef) item.dataset.outputRef = outputRef;
    else delete item.dataset.outputRef;
    item.dataset.outputHydrated = "false";
    item.dataset.previewDetailText = normalizeInteractionDetailText(detail);
    syncCeoBackgroundDetailState(item, { rawDetail: rawText });
    turn.flowEl.hidden = false;
    turn.flowEl.open = true;
    turn.hasError = turn.hasError || status === "error";
    trimCeoToolSteps(turn);
    updateCeoTurnMeta(turn, stage.meta);
    const runtimeEl = item.querySelector(".interaction-step-runtime");
    if (runtimeEl instanceof HTMLElement) updateRuntimeBadge(item, runtimeEl);
    updateCeoBackgroundDetail(item);
    icons();
    return item;
}

function appendCeoToolEvent(event = {}) {
    const explicitSource = String(event?.source || "").trim().toLowerCase();
    const turnId = String(event?.turn_id || "").trim();
    let source = explicitSource ? normalizeCeoTurnSource(explicitSource) : "";
    if (!source) {
        const snapshotSource = String(getCeoSessionSnapshotCache(activeSessionId())?.inflight_turn?.source || "").trim().toLowerCase();
        if (snapshotSource) source = normalizeCeoTurnSource(snapshotSource);
    }
    if (!source) {
        const activeTurns = S.ceoPendingTurns.filter((turn) => turn && !turn.finalized);
        if (activeTurns.length === 1) source = normalizeCeoTurnSource(activeTurns[0]?.source || "");
    }
    if (!source) return;
    const turn = ensureActiveCeoTurn({ source, turnId });
    if (!turn?.listEl || !turn.flowEl) return;
    if (turnId) turn.turnId = turnId;
    mutateCeoFeed(() => {
        applyCeoToolEventToTurn(turn, event);
    }, { scrollMode: "preserve" });
}

function finalizeCeoTurn(text, meta = {}) {
    const sessionId = activeSessionId();
    S.ceoTurnActive = false;
    S.ceoPauseBusy = false;
    if (patchCeoSessionRuntimeState(sessionId, false)) renderCeoSessions();
    syncCeoPrimaryButton();
    const normalizedSource = normalizeCeoTurnSource(meta?.source || "user");
    const normalizedTurnId = normalizeCeoTurnId(meta?.turn_id || "");
    const turn = pullActiveCeoTurn(normalizedSource, normalizedTurnId);
    if (!turn?.textEl || !turn.flowEl) {
        addMsg(text, "system", { markdown: true, scrollMode: "preserve" });
        discardPendingCeoTurns({
            force: normalizedSource === "heartbeat",
            source: normalizedSource,
            turnId: normalizedTurnId,
        });
        patchCeoSessionSnapshotCache(sessionId, (entry) => {
            const inflightTurn = normalizeCeoSnapshotInflight(entry?.inflight_turn);
            const inflightTurnId = normalizeCeoTurnId(inflightTurn?.turn_id);
            const inflightSource = String(inflightTurn?.source || "").trim().toLowerCase();
            const inflightMatchesSource = !inflightTurn
                || (normalizedTurnId && inflightTurnId && inflightTurnId !== normalizedTurnId ? false : true)
                || !inflightSource
                || normalizeCeoTurnSource(inflightSource) === normalizedSource;
            const messages = appendCeoSessionSnapshotMessage(entry?.messages, {
                role: "assistant",
                content: String(text || "").trim() || "Done.",
                tool_events: inflightMatchesSource ? inflightTurn?.tool_events || [] : [],
                execution_trace_summary: inflightMatchesSource ? inflightTurn?.execution_trace_summary || null : null,
            });
            return {
                ...(entry || {}),
                messages,
                inflight_turn: inflightMatchesSource ? null : inflightTurn,
            };
        });
        return;
    }
    mutateCeoFeed(() => {
        turn.finalized = true;
        turn.textEl.innerHTML = renderMarkdown(String(text || "").trim() || "已完成。");
        turn.textEl.classList.remove("pending");
        turn.textEl.classList.add("markdown-content");
        if (turn.steps > 0) {
            const hasRunningStep = hasRunningCeoToolStep(turn);
            turn.flowEl.hidden = false;
            turn.flowEl.open = false;
            updateCeoTurnMeta(
                turn,
                turn.hasError ? "处理完成，但有异常" : (hasRunningStep ? "已返回当前判断，后台任务仍在运行" : "处理完成")
            );
        } else {
            turn.flowEl.hidden = true;
        }
        icons();
    }, { scrollMode: "preserve" });
    discardPendingCeoTurns({
        force: normalizedSource === "heartbeat",
        source: normalizedSource,
        turnId: normalizedTurnId,
    });
    patchCeoSessionSnapshotCache(sessionId, (entry) => {
        const inflightTurn = normalizeCeoSnapshotInflight(entry?.inflight_turn);
        const inflightTurnId = normalizeCeoTurnId(inflightTurn?.turn_id);
        let messages = trimCeoSessionSnapshotMessages(entry?.messages);
        const inflightSource = normalizeCeoTurnSource(inflightTurn?.source || "user");
        const inflightMatchesSource = !inflightTurn
            || (normalizedTurnId && inflightTurnId && inflightTurnId !== normalizedTurnId ? false : true)
            || !String(inflightTurn?.source || "").trim()
            || inflightSource === normalizedSource;
        if (inflightTurn?.user_message && inflightMatchesSource) {
            messages = appendCeoSessionSnapshotMessage(messages, {
                role: "user",
                content: String(inflightTurn.user_message?.content || ""),
                attachments: inflightTurn.user_message?.attachments || [],
            });
        }
        messages = appendCeoSessionSnapshotMessage(messages, {
            role: "assistant",
            content: String(text || "").trim() || "Done.",
            tool_events: inflightTurn?.tool_events || [],
            execution_trace_summary: inflightTurn?.execution_trace_summary || null,
        });
        return {
            ...(entry || {}),
            messages,
            inflight_turn: inflightMatchesSource ? null : inflightTurn,
        };
    });
    maybeDispatchQueuedCeoFollowUps();
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
        hint.textContent = dirty ? (busy ? "正在自动保存..." : "变更已暂存，将自动保存。") : "";
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

function openConfirm({ title, text, confirmLabel = "确认", confirmKind = "danger", onConfirm, onClose = null, returnFocus = null, checkbox = null }) {
    S.confirmState = {
        onConfirm,
        onClose,
        returnFocus,
        checkbox: checkbox && typeof checkbox === "object" ? checkbox : null,
        accepted: false,
    };
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
            if (U.confirmCheckboxDetails) {
                const detailText = String(checkbox.details || "").trim();
                U.confirmCheckboxDetails.hidden = !detailText;
                U.confirmCheckboxDetails.textContent = detailText;
            }
        } else {
            U.confirmCheckbox.checked = false;
            U.confirmCheckbox.disabled = false;
            U.confirmCheckboxLabel.textContent = "同时删除此对话创建的所有任务记录";
            U.confirmCheckboxHint.textContent = "";
            if (U.confirmCheckboxDetails) {
                U.confirmCheckboxDetails.hidden = true;
                U.confirmCheckboxDetails.textContent = "";
            }
        }
    }
    U.confirmAccept.textContent = confirmLabel;
    U.confirmAccept.className = `toolbar-btn ${confirmKind}`;
    U.confirmBackdrop.hidden = false;
    U.confirmBackdrop.classList.add("is-open");
    window.requestAnimationFrame(() => U.confirmCancel?.focus());
}

function requestInlineConfirm({ title, text, confirmLabel = "确认", confirmKind = "danger", returnFocus = null, checkbox = null }) {
    const focusTarget = returnFocus || (document.activeElement instanceof HTMLElement ? document.activeElement : null);
    return new Promise((resolve) => {
        let settled = false;
        openConfirm({
            title,
            text,
            confirmLabel,
            confirmKind,
            returnFocus: focusTarget,
            checkbox,
            onConfirm: async ({ checked }) => {
                if (settled) return;
                settled = true;
                resolve({ confirmed: true, checked: !!checked });
            },
            onClose: () => {
                if (settled) return;
                settled = true;
                resolve({ confirmed: false, checked: false });
            },
        });
    });
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
    renderFlowHeading(0);
    renderSpawnReviewHeading(0);
    renderArtifactHeading(0);
    if (U.taskGovernanceToggle && U.taskGovernanceToggle.dataset.bound !== "true") {
        U.taskGovernanceToggle.dataset.bound = "true";
        U.taskGovernanceToggle.addEventListener("click", () => {
            S.taskGovernanceExpanded = !S.taskGovernanceExpanded;
            if (typeof renderTaskGovernancePanel === "function") renderTaskGovernancePanel();
        });
    }
    if (U.adOutputHeading) U.adOutputHeading.innerHTML = '<i data-lucide="arrow-up-from-line"></i> 最终输出';
    if (U.adOutput) U.adOutput.classList.add("task-trace-output");
    if (U.adAcceptanceHeading) U.adAcceptanceHeading.innerHTML = '<i data-lucide="shield-check"></i> 验收结果';
    if (U.adFlow) {
        U.adFlow.classList.remove("code-block");
        U.adFlow.classList.add("task-trace-host");
    }
    if (U.adSpawnReviews) {
        U.adSpawnReviews.classList.remove("code-block");
        U.adSpawnReviews.classList.add("task-trace-host");
    }
    if (U.adAcceptance) U.adAcceptance.classList.add("task-trace-acceptance");
    if (U.nodeEmpty) U.nodeEmpty.textContent = "选择任务树中的节点后，这里会显示执行流程、最终输出、验收结果和工件。";
    if (U.adOutputSection) U.adOutputSection.hidden = false;
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
    optionButton.append(label);
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
        optionButton.append(label);
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
    const onClose = S.confirmState?.onClose;
    const accepted = !!S.confirmState?.accepted;
    S.confirmState = null;
    if (U.confirmOptions) U.confirmOptions.hidden = true;
    if (U.confirmCheckbox) {
        U.confirmCheckbox.checked = false;
        U.confirmCheckbox.disabled = false;
    }
    if (U.confirmCheckboxHint) U.confirmCheckboxHint.textContent = "";
    U.confirmBackdrop.hidden = true;
    U.confirmBackdrop.classList.remove("is-open");
    if (!accepted && typeof onClose === "function") {
        try {
            onClose();
        } catch (error) {
            void error;
        }
    }
    if (restoreFocus) returnFocus?.focus?.();
}

async function acceptConfirm() {
    if (!S.confirmState?.onConfirm) return;
    U.confirmAccept.disabled = true;
    U.confirmCancel.disabled = true;
    if (U.confirmCheckbox) U.confirmCheckbox.disabled = true;
    try {
        S.confirmState.accepted = true;
        await S.confirmState.onConfirm({ checked: !!U.confirmCheckbox?.checked });
        closeConfirm();
    } catch (error) {
        if (S.confirmState) S.confirmState.accepted = false;
        showToast({ title: "操作失败", text: error?.message || "Unknown error", kind: "error" });
    } finally {
        U.confirmAccept.disabled = false;
        U.confirmCancel.disabled = false;
        if (U.confirmCheckbox) U.confirmCheckbox.disabled = false;
    }
}

function finalizeProjectExit() {
    try {
        window.close();
    } catch (error) {
        void error;
    }
    window.setTimeout(() => {
        try {
            window.location.replace("about:blank");
        } catch (error) {
            window.location.href = "about:blank";
        }
    }, 120);
}

async function requestProjectExit() {
    const payload = await ApiClient.getBootstrapExitCheck();
    const hasRunning = !!payload?.has_running_work;
    const summary = String(payload?.summary_text || "").trim();
    openConfirm({
        title: "确认退出项目？",
        text: hasRunning ? `检测到${summary}。退出前请确认如何处理。` : "确认后会关闭项目服务与当前网页。",
        confirmLabel: "退出项目",
        confirmKind: "danger",
        checkbox: hasRunning ? {
            checked: false,
            label: "暂停正在进行的所有对话和任务",
            hint: summary,
        } : null,
        returnFocus: U.projectExit,
        onConfirm: async ({ checked }) => {
            if (hasRunning && !checked) {
                throw new Error("请先勾选“暂停正在进行的所有对话和任务”。");
            }
            await ApiClient.exitBootstrap({ pause_running_work: !!checked });
            finalizeProjectExit();
        },
    });
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

function activeRoleConcurrency() {
    return S.modelCatalog.roleEditing ? S.modelCatalog.roleConcurrencyDrafts : S.modelCatalog.roleConcurrency;
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
    const raw = iterations?.[scope];
    if (raw == null || String(raw).trim() === "") return null;
    const value = Number(raw);
    return Number.isInteger(value) && value >= 0 ? value : null;
}

function modelScopeConcurrency(scope, source = "active") {
    const concurrency = source === "draft"
        ? S.modelCatalog.roleConcurrencyDrafts
        : source === "committed"
            ? S.modelCatalog.roleConcurrency
            : activeRoleConcurrency();
    const raw = concurrency?.[scope];
    if (raw == null || String(raw).trim() === "") return null;
    const value = Number(raw);
    return Number.isInteger(value) && value >= 0 ? value : null;
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

function normalizeRoleConcurrency(concurrency = DEFAULT_ROLE_CONCURRENCY()) {
    return cloneRoleConcurrency(concurrency);
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

function modelRoleConcurrencyEqual(left, right) {
    const leftNormalized = normalizeRoleConcurrency(left);
    const rightNormalized = normalizeRoleConcurrency(right);
    return MODEL_SCOPES.every(({ key }) => leftNormalized[key] === rightNormalized[key]);
}

function syncModelRoleDraftState() {
    const rolesChanged = !modelRolesEqual(S.modelCatalog.roleDrafts, S.modelCatalog.roles);
    const iterationsChanged = !modelRoleIterationsEqual(S.modelCatalog.roleIterationDrafts, S.modelCatalog.roleIterations);
    const concurrencyChanged = !modelRoleConcurrencyEqual(S.modelCatalog.roleConcurrencyDrafts, S.modelCatalog.roleConcurrency);
    S.modelCatalog.rolesDirty = !!S.modelCatalog.roleEditing && (rolesChanged || iterationsChanged || concurrencyChanged);
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
    const roleConcurrencyPayload = payload.roleConcurrency && typeof payload.roleConcurrency === "object"
        ? payload.roleConcurrency
        : payload.role_concurrency && typeof payload.role_concurrency === "object"
            ? payload.role_concurrency
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
    S.modelCatalog.roleConcurrency = normalizeRoleConcurrency(roleConcurrencyPayload);
    if (preserveRoleDrafts && S.modelCatalog.roleEditing) {
        S.modelCatalog.roleDrafts = normalizeAllModelRoles(S.modelCatalog.roleDrafts);
        S.modelCatalog.roleIterationDrafts = normalizeRoleIterations(S.modelCatalog.roleIterationDrafts);
        S.modelCatalog.roleConcurrencyDrafts = normalizeRoleConcurrency(S.modelCatalog.roleConcurrencyDrafts);
        syncModelRoleDraftState();
    } else {
        S.modelCatalog.roleDrafts = cloneModelRoles(S.modelCatalog.roles);
        S.modelCatalog.roleIterationDrafts = cloneRoleIterations(S.modelCatalog.roleIterations);
        S.modelCatalog.roleConcurrencyDrafts = cloneRoleConcurrency(S.modelCatalog.roleConcurrency);
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
        const defaultText = chain.length ? `已配置 ${chain.length} 个模型` : "尚未配置";
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
                                class="model-role-iterations-input spinless-number-input"
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
                                <span class="resource-field-label">配置名 / 绑定名 *</span>
                                <input class="resource-search" name="key" ${isCreate ? `value=""` : `value="${esc(current.key)}" disabled`} placeholder="如 openai_primary">
                            </label>
                            <label class="resource-field">
                                <span class="resource-field-label">Provider / Model *</span>
                                <input class="resource-search" name="providerModel" value="${esc(current?.provider_model || "")}" placeholder="如 openai:gpt-4.1">
                            </label>
                            <label class="resource-field">
                                <span class="resource-field-label">API Key *</span>
                                <input class="resource-search" name="apiKey" value="${esc(current?.api_key || "")}" placeholder="sk-... or sk-1,sk-2">
                            </label>
                            <label class="resource-field">
                                <span class="resource-field-label">Base URL ${isCreate ? "*" : ""}</span>
                                <input class="resource-search" name="apiBase" value="${esc(current?.api_base || "")}" placeholder="https://api.example.com/v1">
                            </label>
                        </div>
                        <p class="subtitle">API Key 支持用逗号或换行填写多把 key，例如 key1,key2。多个 key 会按顺序轮换。</p>
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
                                <input class="resource-search" type="number" min="1" step="1" name="maxTokens" value="${esc(String(current?.max_tokens ?? ""))}" placeholder="留空则不下发">
                            </label>
                            <label class="resource-field">
                                <span class="resource-field-label">Temperature</span>
                                <input class="resource-search" type="number" min="0" max="2" step="0.1" name="temperature" value="${esc(String(current?.temperature ?? ""))}" placeholder="留空则不下发">
                            </label>
                            <label class="resource-field">
                                <span class="resource-field-label">Reasoning Effort</span>
                                <input class="resource-search" name="reasoningEffort" value="${esc(current?.reasoning_effort || "")}" placeholder="留空则不下发">
                            </label>
                            <label class="resource-field">
                                <span class="resource-field-label">Retry On</span>
                                <input class="resource-search" name="retryOn" value="${esc((current?.retry_on || []).join(", "))}" placeholder="如 network, 429, 5xx">
                            </label>
                            <label class="resource-field">
                                <span class="resource-field-label">重试次数</span>
                                <input class="resource-search spinless-number-input" type="number" min="0" step="1" name="retryCount" value="${esc(String(current?.retry_count ?? 0))}" placeholder="0">
                            </label>
                        </div>
                        <p class="subtitle">配置多个 API Key 时，重试次数按完整轮过所有 key 计算。</p>
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
        root.querySelectorAll('[data-drop-position]').forEach((item) => delete item.dataset.dropPosition);
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
        hoverZoneKey: "",
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
    const clientX = Number(event?.clientX);
    const clientY = Number(event?.clientY);
    if (Number.isFinite(clientX) && Number.isFinite(clientY)) {
        const hovered = document.elementFromPoint(clientX, clientY);
        if (hovered instanceof Node && zone.contains(hovered)) return false;
        return !modelDragZoneContainsPoint(zone, clientX, clientY);
    }
    return false;
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
    list.querySelectorAll('.is-drop-target').forEach((item) => item.classList.remove('is-drop-target'));
    list.querySelectorAll('[data-drop-position]').forEach((item) => delete item.dataset.dropPosition);
    const placeholder = list.querySelector('[data-model-drop-placeholder]') || document.createElement('div');
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
    list.querySelectorAll('.is-drop-target').forEach((item) => item.classList.remove('is-drop-target'));
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
    if (!normalizedScope) return false;
    if (value == null || String(value).trim() === "") {
        S.modelCatalog.roleIterationDrafts[normalizedScope] = null;
    } else {
        const cleanValue = Number.parseInt(String(value || "").trim(), 10);
        if (!Number.isInteger(cleanValue) || cleanValue < 0) return false;
        S.modelCatalog.roleIterationDrafts[normalizedScope] = cleanValue;
    }
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
    const updates = buildModelRoleChainUpdates(scopes, { useDrafts });
    if (!Object.keys(updates).length) return;
    S.modelCatalog.saving = true;
    renderModelCatalog();
    try {
        const payload = await ApiClient.updateModelRoleChains(updates);
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

function renderRoleLimitControl({ scopeKey, kind, label, value, editing }) {
    const modeName = `model-role-${kind}-mode-${scopeKey}`;
    const inputValue = value == null ? "" : String(value);
    const isCustom = value != null;
    return `
        <div class="model-role-limit-field" data-model-role-limit-kind="${esc(kind)}" data-model-role-limit-scope="${esc(scopeKey)}">
            <span class="model-role-iterations-label">${esc(label)}</span>
            <div class="llm-segmented-control">
                <label class="llm-segmented-option">
                    <input type="radio" name="${esc(modeName)}" value="unlimited" ${!isCustom ? "checked" : ""} ${editing ? "" : "disabled"} data-model-role-limit-mode="${esc(kind)}" class="llm-segmented-radio">
                    <span class="llm-segmented-label">无限制</span>
                </label>
                <label class="llm-segmented-option">
                    <input type="radio" name="${esc(modeName)}" value="custom" ${isCustom ? "checked" : ""} ${editing ? "" : "disabled"} data-model-role-limit-mode="${esc(kind)}" class="llm-segmented-radio">
                    <span class="llm-segmented-label">自定义</span>
                </label>
            </div>
            <input class="model-role-iterations-input model-role-limit-input spinless-number-input" type="number" min="0" step="1" inputmode="numeric" value="${esc(inputValue)}" placeholder="0" ${editing && isCustom ? "" : "disabled"} data-model-role-limit-input="${esc(kind)}">
        </div>`;
}

function syncRoleIterationDraftsFromInputs({ requireValid = false } = {}) {
    if (!U.modelRoleEditors) return false;
    let changed = false;
    const groups = [...U.modelRoleEditors.querySelectorAll("[data-model-role-limit-kind][data-model-role-limit-scope]")];
    if (!groups.length) return false;
    groups.forEach((group) => {
        if (!(group instanceof HTMLElement)) return;
        const scope = String(group.dataset.modelRoleLimitScope || "").trim();
        const kind = String(group.dataset.modelRoleLimitKind || "").trim();
        if (!scope || !kind) return;
        const input = group.querySelector("[data-model-role-limit-input]");
        if (!(input instanceof HTMLInputElement)) return;
        const scopeLabel = MODEL_SCOPES.find((item) => item.key === scope)?.label || scope;
        const selectedMode = group.querySelector("[data-model-role-limit-mode]:checked");
        const mode = selectedMode instanceof HTMLInputElement ? String(selectedMode.value || "unlimited") : "unlimited";
        input.disabled = mode !== "custom" || !S.modelCatalog.roleEditing;
        if (mode !== "custom") {
            input.classList.remove("is-invalid");
            input.setCustomValidity("");
            const currentValue = kind === "iterations" ? modelScopeIterations(scope, "draft") : modelScopeConcurrency(scope, "draft");
            if (currentValue !== null) {
                if (kind === "iterations") S.modelCatalog.roleIterationDrafts[scope] = null;
                if (kind === "concurrency") S.modelCatalog.roleConcurrencyDrafts[scope] = null;
                changed = true;
            }
            return;
        }
        let rawValue = String(input.value || "").trim();
        if (rawValue === "" && mode === "custom" && !requireValid) {
            rawValue = "0";
            input.value = "0";
        }
        const cleanValue = Number.parseInt(rawValue, 10);
        const invalid = !rawValue || !Number.isInteger(cleanValue) || cleanValue < 0;
        const label = kind === "iterations" ? "最大轮数" : "最大并发数";
        if (invalid) {
            input.classList.add("is-invalid");
            input.setCustomValidity("请输入不为负数的整数");
            if (requireValid) {
                input.reportValidity();
                throw new Error(`${scopeLabel} ${label}必须是不为负数的整数`);
            }
            return;
        }
        input.classList.remove("is-invalid");
        input.setCustomValidity("");
        const currentValue = kind === "iterations" ? modelScopeIterations(scope, "draft") : modelScopeConcurrency(scope, "draft");
        if (currentValue !== cleanValue) {
            if (kind === "iterations") S.modelCatalog.roleIterationDrafts[scope] = cleanValue;
            if (kind === "concurrency") S.modelCatalog.roleConcurrencyDrafts[scope] = cleanValue;
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
    S.modelCatalog.roleConcurrencyDrafts = cloneRoleConcurrency(S.modelCatalog.roleConcurrency);
    syncModelRoleDraftState();
    renderModelCatalog();
}

function cancelModelRoleEditing() {
    S.modelCatalog.roleEditing = false;
    S.modelCatalog.roleDrafts = cloneModelRoles(S.modelCatalog.roles);
    S.modelCatalog.roleIterationDrafts = cloneRoleIterations(S.modelCatalog.roleIterations);
    S.modelCatalog.roleConcurrencyDrafts = cloneRoleConcurrency(S.modelCatalog.roleConcurrency);
    S.modelCatalog.rolesDirty = false;
    finishModelDrag();
    renderModelCatalog();
    hint("已取消模型链修改。", false);
}

async function persistModelRoleChains(scopes = MODEL_SCOPES.map((item) => item.key), successText = "模型链已保存。", { useDrafts = false } = {}) {
    const updates = buildModelRoleChainUpdates(scopes, { useDrafts });
    if (!Object.keys(updates).length) return;
    S.modelCatalog.saving = true;
    renderModelCatalog();
    try {
        const payload = await ApiClient.updateModelRoleChains(updates);
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

function buildModelRoleChainUpdates(scopes = MODEL_SCOPES.map((item) => item.key), { useDrafts = false } = {}) {
    const targets = [...new Set(scopes.map((item) => String(item || "").trim()).filter(Boolean))];
    if (!targets.length) return {};
    const roleSource = useDrafts ? S.modelCatalog.roleDrafts : S.modelCatalog.roles;
    const iterationSource = useDrafts ? S.modelCatalog.roleIterationDrafts : S.modelCatalog.roleIterations;
    const draftConcurrencySource = S.modelCatalog.roleConcurrencyDrafts || DEFAULT_ROLE_CONCURRENCY();
    const concurrencySource = useDrafts ? draftConcurrencySource : S.modelCatalog.roleConcurrency;
    return Object.fromEntries(targets.map((scope) => [
        scope,
        {
            modelKeys: normalizeModelRoleChain(roleSource[scope] || []),
            maxIterations: iterationSource[scope],
            maxConcurrency: concurrencySource[scope],
        },
    ]));
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
        hint(`模型配置错误：${S.modelCatalog.error}`, true);
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
    const reasoningEffortText = String(formData.get("reasoningEffort") || "").trim();
    const reasoningEffort = reasoningEffortText || null;
    const retryOnRaw = String(formData.get("retryOn") || "").trim();
    const retryCountText = String(formData.get("retryCount") || "").trim();
    const description = String(formData.get("description") || "").trim();
    const enabled = formData.get("enabled") === "on";
    const selectedScopes = new Set(MODEL_SCOPES.filter((scope) => formData.get(`scope_${scope.key}`) === "on").map((scope) => scope.key));
    const hasApiKeyEntries = String(apiKey || "").split(/[\n,]/).some((item) => String(item || "").trim());

    if (!key) throw new Error("配置名 / 绑定名不能为空");
    if (!providerModel) throw new Error("Provider / Model 不能为空");
    if (!hasApiKeyEntries) throw new Error("API Key 不能为空");
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
        if (reasoningEffort !== null) payload.reasoningEffort = reasoningEffort;
        if (retryOn !== null) payload.retryOn = retryOn;
        payload.retryCount = retryCount;
        if (extraHeaders !== null) payload.extraHeaders = extraHeaders;
        return { isCreate, key, enabled, selectedScopes, payload };
    }

    const patch = {};
    if (providerModel !== String(current?.provider_model || "")) patch.providerModel = providerModel;
    if (apiKey !== String(current?.api_key || "")) patch.apiKey = apiKey;
    if (apiBase !== String(current?.api_base || "")) patch.apiBase = apiBase;
    if (maxTokensText) {
        if (maxTokens !== Number(current?.max_tokens ?? NaN)) patch.maxTokens = maxTokens;
    } else if (current?.max_tokens != null) {
        patch.maxTokens = null;
    }
    if (temperatureText) {
        if (temperature !== Number(current?.temperature ?? NaN)) patch.temperature = temperature;
    } else if (current?.temperature != null) {
        patch.temperature = null;
    }
    if (reasoningEffort !== (String(current?.reasoning_effort || "").trim() || null)) patch.reasoningEffort = reasoningEffort;
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
    const { confirmed } = await requestInlineConfirm({
        title: "确认删除模型？",
        text: `删除模型 ${targetKey} 后，会同时从 catalog 和所有角色链移除它。`,
        confirmLabel: "删除模型",
        confirmKind: "danger",
    });
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

function resetCeoComposerState({ clearDraft = false, sessionId = activeSessionId() } = {}) {
    S.ceoUploads = [];
    S.ceoUploadBusy = false;
    if (U.ceoInput) U.ceoInput.value = "";
    if (U.ceoFileInput) U.ceoFileInput.value = "";
    if (clearDraft) clearCeoComposerDraft(sessionId);
    renderPendingCeoUploads();
    renderQueuedCeoFollowUps(sessionId);
    syncCeoInputHeight();
}

function resetCeoComposerForSessionChange(previousSessionId, nextSessionId) {
    return switchCeoComposerDraft(previousSessionId, nextSessionId);
}

function resetCeoSessionState({ scrollToLatest = false } = {}) {
    resetCeoFeed();
    S.ceoPendingTurns = [];
    S.ceoTurnActive = false;
    S.ceoPauseBusy = false;
    if (scrollToLatest) S.ceoScrollToLatestOnSnapshot = true;
    syncCeoCompressionToast();
    syncCeoPrimaryButton();
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
            <div class="ceo-session-current-meta">当前会话 · ${esc(shortSessionIdLabel(current.session_id))} · ${esc(formatSessionTime(ceoSessionDisplayTime(current)))}</div>
        `
        : `
            <div class="ceo-session-current-title">正在准备会话</div>
            <div class="ceo-session-current-meta">会话加载后会自动连接。</div>
        `;

    if (!sessions.length) {
        U.ceoSessionList.innerHTML = '<div class="empty-state ceo-session-empty">No sessions yet.</div>';
        syncCeoSessionActions();
        syncCeoCompressionToast();
        return;
    }

    U.ceoSessionList.innerHTML = sessions.map((item) => {
        const sessionId = String(item?.session_id || "");
        const isActive = sessionId === currentId;
        const isRunning = !!item?.is_running;
        const preview = String(item?.preview_text || "").trim() || "No messages yet.";
        const title = String(item?.title || sessionId || "Session");
        const unreadCount = isActive ? 0 : sessionUnreadCount(sessionId);
        const unreadText = unreadCount > 99 ? "99+" : String(unreadCount);
        const displayTime = ceoSessionDisplayTime(item);
        const shortId = shortSessionIdLabel(sessionId);
        return `
            <div class="ceo-session-card${isActive ? " is-active" : ""}${unreadCount > 0 ? " has-unread" : ""}${isRunning ? " is-running" : ""}" role="listitem">
                <button
                    type="button"
                    class="ceo-session-main ceo-session-select"
                    data-session-activate="${esc(sessionId)}"
                    aria-pressed="${isActive ? "true" : "false"}"
                    aria-label="${esc(title)}"
                >
                    <div class="ceo-session-head">
                        <div class="ceo-session-title">${esc(title)}</div>
                        ${unreadCount > 0 ? `<span class="ceo-session-unread" aria-label="${esc(`${unreadCount} unread message${unreadCount > 1 ? "s" : ""}`)}">${esc(unreadText)}</span>` : ""}
                    </div>
                    <div class="ceo-session-id">${esc(shortId)}</div>
                    <div class="ceo-session-preview">${esc(preview)}</div>
                    <div class="ceo-session-meta">${esc(formatSessionTime(displayTime))}</div>
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
    syncCeoCompressionToast();
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

function applyCeoSessionPatch(payload = {}) {
    const item = payload?.item && typeof payload.item === "object" ? payload.item : null;
    if (!item) return;
    const sessionId = String(item.session_id || "").trim();
    if (!sessionId) return;
    const next = [...(S.ceoSessions || [])];
    const index = next.findIndex((entry) => String(entry?.session_id || "").trim() === sessionId);
    if (index >= 0) next[index] = { ...next[index], ...item };
    else next.unshift(item);
    const activeId = String(payload?.active_session_id || activeSessionId()).trim() || activeSessionId();
    syncCeoSessionUnreadState(next, activeId);
    S.ceoSessions = next;
    S.activeSessionId = activeId;
    if (activeId) ApiClient.setActiveSessionId(activeId);
    renderCeoSessions();
}

function renderCeoSessionCard(item, { allowActions = false } = {}) {
    const sessionId = String(item?.session_id || "");
    const isActive = sessionId === activeSessionId();
    const isRunning = !!item?.is_running;
    const isBulkMode = !!S.ceoBulkMode;
    const isSelected = isCeoBulkSessionSelected(sessionId);
    const preview = String(item?.preview_text || "").trim() || "No messages yet.";
    const title = String(item?.title || sessionId || "Session");
    const glyph = ceoSessionGlyph(item);
    const unreadCount = isActive ? 0 : sessionUnreadCount(sessionId);
    const unreadText = unreadCount > 99 ? "99+" : String(unreadCount);
    const displayTime = ceoSessionDisplayTime(item);
    const shortId = shortSessionIdLabel(sessionId);
    const type = String(item?.chat_type || "").trim();
    const typeLabel = type === "dm" ? "DM merged" : type === "group" ? "Group" : type === "thread" ? "Thread" : "";
    const badges = [
        typeLabel ? `<span class="ceo-session-pill">${esc(typeLabel)}</span>` : "",
        item?.is_readonly ? '<span class="ceo-session-pill readonly">只读</span>' : "",
    ].filter(Boolean).join("");
    return `
        <div class="ceo-session-card${isActive ? " is-active" : ""}${unreadCount > 0 ? " has-unread" : ""}${isRunning ? " is-running" : ""}${isBulkMode ? " is-bulk-mode" : ""}${isSelected ? " is-bulk-selected" : ""}" role="listitem">
            ${isBulkMode ? `
                <label class="ceo-session-checkbox" aria-label="${esc(`选择会话 ${title}`)}">
                    <input type="checkbox" data-session-bulk-checkbox="${esc(sessionId)}" ${isSelected ? "checked" : ""}>
                </label>
            ` : ""}
            <button
                type="button"
                class="ceo-session-main ceo-session-select"
                data-session-activate="${esc(sessionId)}"
                aria-pressed="${isBulkMode ? (isSelected ? "true" : "false") : (isActive ? "true" : "false")}"
                aria-label="${esc(`${title}${isRunning ? "（运行中）" : ""}`)}"
                title="${esc(title)}"
            >
                <span class="ceo-session-glyph" aria-hidden="true">${esc(glyph)}</span>
                <span class="ceo-session-body">
                    <span class="ceo-session-head">
                        <span class="ceo-session-title">${esc(title)}</span>
                    </span>
                    <span class="ceo-session-id">${esc(shortId)}</span>
                    <span class="ceo-session-preview">${esc(preview)}</span>
                    <span class="ceo-session-meta">${esc(formatSessionTime(displayTime))}</span>
                    ${badges ? `<span class="ceo-session-badges">${badges}</span>` : ""}
                </span>
                ${unreadCount > 0 ? `<span class="ceo-session-unread" aria-label="${esc(`${unreadCount} unread message${unreadCount > 1 ? "s" : ""}`)}">${esc(unreadText)}</span>` : ""}
            </button>
            ${allowActions && !isBulkMode ? `
                <div class="ceo-session-actions toolbar-dropdown" data-session-menu="${esc(sessionId)}" aria-label="Session actions">
                    <button type="button" class="ceo-session-action ceo-session-menu-trigger" data-session-menu-toggle="${esc(sessionId)}" aria-label="More session actions" aria-haspopup="menu" aria-expanded="false">
                        <i data-lucide="more-horizontal"></i>
                    </button>
                    <div class="toolbar-menu ceo-session-menu" role="menu" hidden>
                        <button type="button" class="toolbar-menu-item" data-session-rename="${esc(sessionId)}" role="menuitem">命名</button>
                        <button type="button" class="toolbar-menu-item danger" data-session-delete="${esc(sessionId)}" role="menuitem">删除</button>
                    </div>
                </div>
            ` : ""}
        </div>
    `;
}

function renderCeoSessions() {
    if (!U.ceoSessionList) return;
    const sessions = visibleCeoSessions();
    if (U.ceoSessionCurrent) {
        U.ceoSessionCurrent.innerHTML = "";
        U.ceoSessionCurrent.hidden = true;
    }
    if (!sessions.length) {
        U.ceoSessionList.innerHTML = `<div class="empty-state ceo-session-empty">${S.ceoSessionTab === "channel" ? "暂无渠道会话。" : "No sessions yet."}</div>`;
        syncCeoComposerReadonlyState();
        syncCeoAttachButton();
        syncCeoSessionActions();
        syncCeoCompressionToast();
        return;
    }
    if (S.ceoSessionTab === "channel") {
        U.ceoSessionList.innerHTML = (Array.isArray(S.ceoChannelGroups) ? S.ceoChannelGroups : []).map((group) => {
            const items = Array.isArray(group?.items) ? group.items : [];
            return `
                <section class="ceo-session-group">
                    <div class="ceo-session-group-title">${esc(String(group?.label || group?.channel_id || "渠道"))}</div>
                    <div class="ceo-session-group-list" role="list">
                        ${items.map((item) => renderCeoSessionCard(item, { allowActions: false })).join("")}
                    </div>
                </section>
            `;
        }).join("");
    } else {
        U.ceoSessionList.innerHTML = sessions.map((item) => renderCeoSessionCard(item, { allowActions: true })).join("");
    }
    syncCeoComposerReadonlyState();
    syncCeoAttachButton();
    syncCeoSessionActions();
    syncCeoCompressionToast();
    icons();
}

function applyCeoSessionsPayload(payload = {}, { preferLocalActive = false } = {}) {
    const localSessions = sortCeoSessionsByTime(Array.isArray(payload?.items) ? payload.items : []);
    const channelGroups = normalizeCeoChannelGroups(Array.isArray(payload?.channel_groups) ? payload.channel_groups : []);
    const sessions = [...localSessions, ...flattenChannelGroups(channelGroups)];
    const previousActiveId = activeSessionId();
    const preferredActiveId = preferLocalActive ? previousActiveId : "";
    const preferredExists = !!preferredActiveId && sessions.some((item) => String(item?.session_id || "").trim() === preferredActiveId);
    const nextActiveId =
        (preferredExists ? preferredActiveId : "")
        || String(payload?.active_session_id || "").trim()
        || String(sessions.find((item) => item?.is_active)?.session_id || "").trim()
        || activeSessionId();
    syncCeoSessionUnreadState(sessions, nextActiveId);
    S.ceoLocalSessions = localSessions;
    S.ceoChannelGroups = channelGroups;
    S.ceoSessions = sessions;
    S.activeSessionId = nextActiveId;
    const activeItem = sessions.find((item) => String(item?.session_id || "").trim() === nextActiveId) || null;
    S.activeSessionFamily = String(payload?.active_session_family || activeItem?.session_family || "local").trim() || "local";
    S.ceoSessionTab = S.activeSessionFamily === "channel" ? "channel" : "local";
    if (nextActiveId) ApiClient.setActiveSessionId(nextActiveId);
    resetCeoComposerForSessionChange(previousActiveId, nextActiveId);
    renderCeoSessions();
    if (S.view === "tasks" && previousActiveId !== nextActiveId) renderTasks();
    return nextActiveId;
}

function applyCeoSessionPatch(payload = {}) {
    const item = payload?.item && typeof payload.item === "object" ? payload.item : null;
    if (!item) return;
    const sessionId = String(item.session_id || "").trim();
    if (!sessionId) return;
    const previousActiveId = activeSessionId();
    if (isChannelSessionItem(item)) {
        const targetChannelId = String(item.channel_id || "").trim();
        let found = false;
        S.ceoChannelGroups = normalizeCeoChannelGroups((S.ceoChannelGroups || []).map((group) => {
            const items = Array.isArray(group?.items) ? [...group.items] : [];
            const index = items.findIndex((entry) => String(entry?.session_id || "").trim() === sessionId);
            if (index >= 0) {
                items[index] = { ...items[index], ...item };
                found = true;
            } else if (String(group?.channel_id || "").trim() === targetChannelId && !found) {
                items.unshift(item);
                found = true;
            }
            return { ...group, items };
        }));
        if (!found && targetChannelId) {
            S.ceoChannelGroups = normalizeCeoChannelGroups([
                ...(S.ceoChannelGroups || []),
                { channel_id: targetChannelId, label: displayChinaChannelLabel(targetChannelId), items: [item] },
            ]);
        }
    } else {
        const next = [...(S.ceoLocalSessions || [])];
        const index = next.findIndex((entry) => String(entry?.session_id || "").trim() === sessionId);
        if (index >= 0) next[index] = { ...next[index], ...item };
        else next.unshift(item);
        S.ceoLocalSessions = sortCeoSessionsByTime(next);
    }
    rebuildCeoSessionIndex();
    const activeId = String(payload?.active_session_id || activeSessionId()).trim() || activeSessionId();
    syncCeoSessionUnreadState(S.ceoSessions, activeId);
    S.activeSessionId = activeId;
    const activeItem = activeSessionItem();
    S.activeSessionFamily = String(payload?.active_session_family || activeItem?.session_family || "local").trim() || "local";
    if (S.activeSessionFamily === "channel") S.ceoSessionTab = "channel";
    if (activeId) ApiClient.setActiveSessionId(activeId);
    resetCeoComposerForSessionChange(previousActiveId, activeId);
    renderCeoSessions();
}

function applyOptimisticCeoSessionSwitch(sessionId, session = null) {
    const targetId = String(sessionId || "").trim();
    const previousActiveId = activeSessionId();
    if (!targetId || targetId === previousActiveId) {
        return { previousActiveId, switched: false, renderedFromCache: false };
    }
    closeCeoWs();
    S.activeSessionId = targetId;
    S.activeSessionFamily = String(
        session?.session_family
        || (isChannelSessionItem(session) || targetId.startsWith("china:") ? "channel" : "local")
    ).trim() || "local";
    S.ceoSessionTab = S.activeSessionFamily === "channel" ? "channel" : "local";
    ApiClient.setActiveSessionId(targetId);
    resetCeoComposerForSessionChange(previousActiveId, targetId);
    resetCeoSessionState({ scrollToLatest: true });
    const renderedFromCache = renderCeoSessionSnapshotFromCache(targetId, { scrollToLatest: true });
    if (!renderedFromCache) renderCeoSessionLoadingState(targetId, session);
    renderCeoSessions();
    syncCeoComposerReadonlyState();
    syncCeoSessionActions();
    syncCeoPrimaryButton();
    return { previousActiveId, switched: true, renderedFromCache };
}

async function refreshCeoSessions({ reconnect = false, background = false } = {}) {
    if (!background) {
        S.ceoSessionCatalogBusy = true;
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
            S.ceoSessionCatalogBusy = false;
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
    const switchToken = ++S.ceoSessionSwitchToken;
    S.ceoSessionBusy = true;
    const { previousActiveId, switched } = applyOptimisticCeoSessionSwitch(targetId, targetSession);
    if (!switched) {
        S.ceoSessionBusy = false;
        renderCeoSessions();
        syncCeoPrimaryButton();
        return;
    }
    initCeoWs();
    try {
        const payload = await ApiClient.activateCeoSession(targetId);
        if (switchToken !== S.ceoSessionSwitchToken || activeSessionId() !== targetId) return;
        applyCeoSessionsPayload(payload, { preferLocalActive: true });
    } catch (e) {
        if (switchToken !== S.ceoSessionSwitchToken || activeSessionId() !== targetId) return;
        if (e?.status === 404 && previousActiveId && previousActiveId !== targetId) {
            showToast({ title: "切换失败", text: e.message || "Unknown error", kind: "error" });
            const previousSession = (S.ceoSessions || []).find((item) => String(item?.session_id || "").trim() === previousActiveId) || null;
            S.ceoSessionBusy = true;
            applyOptimisticCeoSessionSwitch(previousActiveId, previousSession);
            initCeoWs();
            return;
        }
        void refreshCeoSessions({ background: true });
    }
}

async function createNewCeoSession() {
    if (!canCreateCeoSessions()) {
        showToast({ title: "当前不可新建", text: "请先等待当前上传、暂停请求或会话切换操作完成后再新建会话。", kind: "warn" });
        return;
    }
    S.ceoSessionCatalogBusy = true;
    renderCeoSessions();
    syncCeoPrimaryButton();
    try {
        const payload = await ApiClient.createCeoSession({});
        const nextActiveId = applyCeoSessionsPayload(payload);
        closeCeoWs();
        resetCeoSessionState({ scrollToLatest: true });
        if (nextActiveId) {
            S.ceoSessionBusy = true;
            initCeoWs();
        }
    } catch (e) {
        showToast({ title: "新建失败", text: e.message || "Unknown error", kind: "error" });
    } finally {
        S.ceoSessionCatalogBusy = false;
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
    S.ceoSessionCatalogBusy = true;
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
        S.ceoSessionCatalogBusy = false;
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
    const deletable = normalizeInt(related.deletable, normalizeInt(related.terminal, 0));
    const inProgress = normalizeInt(related.in_progress, normalizeInt(related.unfinished, 0));
    if (total <= 0) return "当前会话没有关联任务记录。";
    if (deletable <= 0) return `共 ${total} 条任务记录，当前均为进行中，本次不会一并清理。`;
    if (inProgress <= 0) return `共 ${total} 条任务记录，可一并清理。`;
    return `共 ${total} 条任务记录，其中 ${deletable} 条可立即清理，${inProgress} 条进行中。进行中任务会保留。`;
}

function normalizeSessionDeleteTaskIds(items = []) {
    const seen = new Set();
    return (Array.isArray(items) ? items : [])
        .map((item) => String(item?.task_id || item || "").trim())
        .filter((taskId) => {
            if (!taskId || seen.has(taskId)) return false;
            seen.add(taskId);
            return true;
        });
}

function formatSessionDeleteTaskDetails(payload = {}) {
    const usage = payload?.usage && typeof payload.usage === "object" ? payload.usage : {};
    const completedIds = normalizeSessionDeleteTaskIds(usage.completed_tasks);
    const pausedIds = normalizeSessionDeleteTaskIds(usage.paused_tasks);
    const inProgressIds = normalizeSessionDeleteTaskIds(
        Array.isArray(usage.in_progress_tasks) ? usage.in_progress_tasks : usage.tasks
    );
    const lines = [];
    if (completedIds.length) {
        lines.push("已完成任务 ID：", ...completedIds);
    }
    if (pausedIds.length) {
        if (lines.length) lines.push("");
        lines.push("已暂停任务 ID：", ...pausedIds);
    }
    if (inProgressIds.length) {
        if (lines.length) lines.push("");
        lines.push("进行中任务 ID：", ...inProgressIds);
    }
    return lines.join("\n");
}

function buildCeoBulkDeleteSummary(items = []) {
    const entries = (Array.isArray(items) ? items : [])
        .map((entry) => {
            const item = entry?.item && typeof entry.item === "object"
                ? entry.item
                : (S.ceoSessions || []).find((session) => String(session?.session_id || "").trim() === String(entry?.session_id || "").trim()) || null;
            const sessionId = String(entry?.session_id || item?.session_id || "").trim();
            if (!sessionId) return null;
            return {
                item,
                sessionId,
                deleteCheck: entry?.deleteCheck && typeof entry.deleteCheck === "object" ? entry.deleteCheck : {},
            };
        })
        .filter(Boolean);
    const completedTaskIds = new Set();
    const pausedTaskIds = new Set();
    const inProgressTaskIds = new Set();
    let fallbackTotal = 0;
    let fallbackDeletable = 0;
    let fallbackInProgress = 0;
    let channelCount = 0;
    let localCount = 0;
    entries.forEach(({ item, sessionId }) => {
        if (isChannelSessionItem(item) || sessionId.startsWith("china:")) channelCount += 1;
        else localCount += 1;
        const deleteCheck = entries.find((entry) => entry.sessionId === sessionId)?.deleteCheck || {};
        const relatedTasks = deleteCheck?.related_tasks && typeof deleteCheck.related_tasks === "object" ? deleteCheck.related_tasks : {};
        fallbackTotal += normalizeInt(relatedTasks.total, 0);
        fallbackDeletable += normalizeInt(relatedTasks.deletable, normalizeInt(relatedTasks.terminal, 0));
        fallbackInProgress += normalizeInt(relatedTasks.in_progress, normalizeInt(relatedTasks.unfinished, 0));
        normalizeSessionDeleteTaskIds(deleteCheck?.usage?.completed_tasks).forEach((taskId) => completedTaskIds.add(taskId));
        normalizeSessionDeleteTaskIds(deleteCheck?.usage?.paused_tasks).forEach((taskId) => pausedTaskIds.add(taskId));
        normalizeSessionDeleteTaskIds(deleteCheck?.usage?.in_progress_tasks).forEach((taskId) => inProgressTaskIds.add(taskId));
        normalizeSessionDeleteTaskIds(deleteCheck?.usage?.tasks).forEach((taskId) => inProgressTaskIds.add(taskId));
    });
    const totalTaskIds = new Set([...completedTaskIds, ...pausedTaskIds, ...inProgressTaskIds]);
    const deletableTaskIds = new Set([...completedTaskIds, ...pausedTaskIds]);
    const relatedPayload = {
        related_tasks: {
            total: totalTaskIds.size || fallbackTotal,
            deletable: deletableTaskIds.size || fallbackDeletable,
            in_progress: inProgressTaskIds.size || fallbackInProgress,
            terminal: deletableTaskIds.size || fallbackDeletable,
            unfinished: inProgressTaskIds.size || fallbackInProgress,
        },
        usage: {
            completed_tasks: [...completedTaskIds],
            paused_tasks: [...pausedTaskIds],
            in_progress_tasks: [...inProgressTaskIds],
        },
    };
    const onlyChannel = channelCount > 0 && localCount === 0;
    const onlyLocal = localCount > 0 && channelCount === 0;
    const title = onlyChannel
        ? (entries.length > 1 ? "批量清空渠道会话" : "清空渠道会话")
        : onlyLocal
            ? (entries.length > 1 ? "批量删除会话" : "删除会话")
            : "批量清理会话";
    const textLines = [];
    if (localCount > 0) {
        textLines.push(`将删除所选 ${localCount} 个本地会话的聊天记录与附件。`);
    }
    if (channelCount > 0) {
        textLines.push(`将清空所选 ${channelCount} 个渠道会话的上下文与附件。`);
    }
    const text = textLines.join("\n");
    return {
        title,
        text,
        checkboxLabel: "清除关联任务",
        checkboxHint: formatSessionDeleteHint(relatedPayload),
        checkboxDetails: formatSessionDeleteTaskDetails(relatedPayload),
        sessionIds: entries.map((entry) => entry.sessionId),
    };
}

function shortSessionIdLabel(sessionId) {
    const raw = String(sessionId || "").trim();
    if (!raw) return "";
    const normalized = raw.replace(/^web:ceo-/, "");
    if (normalized.length <= 12) return normalized;
    return `${normalized.slice(0, 6)}...${normalized.slice(-4)}`;
}

async function performDeleteCeoSession(sessionId, { deleteTaskRecords = false, refreshTasks = true } = {}) {
    const targetId = String(sessionId || "").trim();
    if (!targetId) return false;
    const wasActive = targetId === activeSessionId();
    S.ceoSessionCatalogBusy = true;
    renderCeoSessions();
    syncCeoPrimaryButton();
    try {
        const payload = await ApiClient.deleteCeoSession(targetId, { delete_task_records: !!deleteTaskRecords });
        clearCeoSessionSnapshotCache(targetId);
        const nextActiveId = applyCeoSessionsPayload(payload);
        if (wasActive) {
            closeCeoWs();
            resetCeoSessionState({ scrollToLatest: true });
            if (nextActiveId) {
                S.ceoSessionBusy = true;
                initCeoWs();
            }
        }
        clearCeoComposerDraft(targetId);
        if (refreshTasks && S.view === "tasks") await loadTasks();
        clearCeoBulkSelection();
        return true;
    } catch (e) {
        showToast({ title: "删除失败", text: e.message || "Unknown error", kind: "error" });
        return false;
    } finally {
        S.ceoSessionCatalogBusy = false;
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
    S.ceoSessionCatalogBusy = true;
    renderCeoSessions();
    syncCeoPrimaryButton();
    let deleteCheck = null;
    try {
        deleteCheck = await ApiClient.getCeoSessionDeleteCheck(current.session_id);
    } catch (e) {
        S.ceoSessionCatalogBusy = false;
        renderCeoSessions();
        syncCeoPrimaryButton();
        showToast({ title: "删除失败", text: e.message || "Unknown error", kind: "error" });
        return;
    }
    S.ceoSessionCatalogBusy = false;
    renderCeoSessions();
    syncCeoPrimaryButton();
    const relatedTasks = deleteCheck?.related_tasks && typeof deleteCheck.related_tasks === "object" ? deleteCheck.related_tasks : {};
    const hasRelatedTaskRecords = normalizeInt(relatedTasks.total, 0) > 0;
    openConfirm({
        title: "删除会话",
        text: `将删除会话“${current.title || current.session_id}”（${shortSessionIdLabel(current.session_id)}）的聊天记录与附件。`,
        confirmLabel: "删除",
        confirmKind: "danger",
        returnFocus: U.ceoNewSession,
        checkbox: hasRelatedTaskRecords ? {
            label: "同时删除此对话创建的所有任务记录",
            hint: formatSessionDeleteHint(deleteCheck),
            details: formatSessionDeleteTaskDetails(deleteCheck),
            checked: false,
        } : null,
        onConfirm: ({ checked } = {}) => performDeleteCeoSession(current.session_id, { deleteTaskRecords: !!checked }),
    });
}

async function performDeleteSelectedCeoSessions(sessionIds = [], { deleteTaskRecords = false } = {}) {
    const ids = [...new Set((Array.isArray(sessionIds) ? sessionIds : []).map((sessionId) => String(sessionId || "").trim()).filter(Boolean))];
    if (!ids.length) return;
    let successCount = 0;
    let failureCount = 0;
    for (const sessionId of ids) {
        const success = await performDeleteCeoSession(sessionId, { deleteTaskRecords, refreshTasks: false });
        if (success) successCount += 1;
        else failureCount += 1;
    }
    clearCeoBulkSelection();
    if (S.view === "tasks" && successCount > 0) await loadTasks();
    renderCeoSessions();
    syncCeoSessionActions();
    if (failureCount > 0) {
        showToast({
            title: "部分删除失败",
            text: `已删除 ${successCount} 个会话，${failureCount} 个会话删除失败。`,
            kind: "warn",
        });
        return;
    }
    if (successCount > 0) {
        showToast({
            title: "删除完成",
            text: `已删除 ${successCount} 个会话。`,
            kind: "success",
        });
    }
}

async function requestDeleteSelectedCeoSessions() {
    const selectedIds = [...(S.ceoSelectedSessionIds instanceof Set ? S.ceoSelectedSessionIds : new Set())]
        .map((sessionId) => String(sessionId || "").trim())
        .filter(Boolean);
    if (!selectedIds.length) return;
    if (!canMutateCeoSessions()) {
        showToast({ title: "当前不可删除", text: "请先等待当前回合完成或暂停后再操作。", kind: "warn" });
        return;
    }
    const selectedItems = selectedIds
        .map((sessionId) => (S.ceoSessions || []).find((item) => String(item?.session_id || "").trim() === sessionId) || null)
        .filter(Boolean);
    if (!selectedItems.length) return;
    S.ceoSessionCatalogBusy = true;
    renderCeoSessions();
    syncCeoPrimaryButton();
    let entries = [];
    try {
        entries = await Promise.all(selectedItems.map(async (item) => ({
            item,
            session_id: String(item?.session_id || "").trim(),
            deleteCheck: await ApiClient.getCeoSessionDeleteCheck(String(item?.session_id || "").trim()),
        })));
    } catch (e) {
        S.ceoSessionCatalogBusy = false;
        renderCeoSessions();
        syncCeoPrimaryButton();
        showToast({ title: "删除失败", text: e.message || "Unknown error", kind: "error" });
        return;
    }
    S.ceoSessionCatalogBusy = false;
    renderCeoSessions();
    syncCeoPrimaryButton();
    const summary = buildCeoBulkDeleteSummary(entries);
    openConfirm({
        title: summary.title,
        text: summary.text,
        confirmLabel: "删除",
        confirmKind: "danger",
        returnFocus: U.ceoSessionBulkToggle || U.ceoNewSession,
        checkbox: {
            label: summary.checkboxLabel,
            hint: summary.checkboxHint,
            details: summary.checkboxDetails,
            checked: false,
        },
        onConfirm: ({ checked } = {}) => performDeleteSelectedCeoSessions(summary.sessionIds, { deleteTaskRecords: !!checked }),
    });
}

function initCeoWs() {
    const requestedSessionId = String(S.activeSessionId || "").trim();
    if (S.ceoWs && S.ceoWs.readyState <= 1 && S.ceoWs.sessionId === requestedSessionId) return;
    closeCeoWs();
    const token = ++S.ceoWsToken;
    const socket = new WebSocket(ApiClient.getCeoWsUrl(requestedSessionId));
    socket.sessionId = requestedSessionId;
    S.ceoWs = socket;
    S.ceoWs.onmessage = (ev) => {
        if (token !== S.ceoWsToken || S.ceoWs !== socket) return;
        const payload = JSON.parse(ev.data);
        const payloadSessionId = String(payload?.session_id || payload?.data?.session_id || "").trim();
        const effectiveSessionId = payloadSessionId || requestedSessionId || activeSessionId();
        if (payload.type === "snapshot.ceo") {
            S.ceoWsLastErrorCode = "";
            if (effectiveSessionId) {
                setCeoSessionSnapshotCache(effectiveSessionId, {
                    messages: payload.data?.messages || [],
                    inflight_turn: payload.data?.inflight_turn || null,
                });
            }
            if (effectiveSessionId === activeSessionId()) {
                renderCeoSnapshot(
                    payload.data?.messages || [],
                    payload.data?.inflight_turn || null,
                    { sessionId: effectiveSessionId }
                );
            }
            S.ceoSessionBusy = false;
            renderCeoSessions();
            syncCeoSessionActions();
            syncCeoPrimaryButton();
        }
        if (payload.type === "error") {
            const code = ApiClient.getErrorCode(payload.data || {});
            const message = ApiClient.friendlyErrorMessage(payload.data || {}, payload.data?.message || "连接失败");
            if (code && code !== S.ceoWsLastErrorCode) {
                S.ceoWsLastErrorCode = code;
                showToast({
                    title: code === "no_model_configured" ? "未配置模型" : "连接失败",
                    text: message,
                    kind: code === "no_model_configured" ? "warn" : "error",
                    durationMs: 5200,
                });
            }
            return;
        }
        if (payload.type === "ceo.state") applyCeoState(payload.data?.state || {}, payload.data || {});
        if (payload.type === "ceo.control_ack") handleCeoControlAck(payload.data || {});
        if (payload.type === "ceo.turn.patch") {
            if (effectiveSessionId === activeSessionId()) {
                patchCeoInflightTurn(payload.data?.inflight_turn || null, { sessionId: effectiveSessionId });
            } else if (effectiveSessionId) {
                setCeoSessionSnapshotCache(effectiveSessionId, {
                    inflight_turn: payload.data?.inflight_turn || null,
                });
            }
        }
        if (payload.type === "ceo.error") handleCeoError(payload.data || {});
        if (payload.type === "ceo.reply.final" && effectiveSessionId === activeSessionId()) {
            finalizeCeoTurn(payload.data?.text || "", payload.data || {});
        }
        if (payload.type === "ceo.turn.discard" && effectiveSessionId === activeSessionId()) {
            discardActiveCeoTurn({ source: payload.data?.source || "", turnId: payload.data?.turn_id || "" });
        }
        if (payload.type === "ceo.sessions.snapshot") applyCeoSessionsPayload(payload.data || {});
        if (payload.type === "ceo.sessions.patch") applyCeoSessionPatch(payload.data || {});
        if (payload.type === "task.artifact.applied" && payload.data?.task_id === S.currentTaskId) void loadTaskArtifacts();
    };
    S.ceoWs.onclose = () => {
        if (token !== S.ceoWsToken) return;
        S.ceoWs = null;
        S.ceoPauseBusy = false;
        S.ceoSessionBusy = false;
        renderCeoSessions();
        syncCeoSessionActions();
        syncCeoPrimaryButton();
        window.setTimeout(() => {
            if (token !== S.ceoWsToken) return;
            initCeoWs();
        }, 1000);
    };
}

function sendCeoMessage() {
    if (activeSessionIsReadonly()) return;
    if (S.ceoSessionBusy || S.ceoSessionCatalogBusy || !activeSessionId()) return;
    const text = String(U.ceoInput.value || "");
    const uploads = normalizeUploadList(S.ceoUploads);
    if (!text.trim() && !uploads.length) return;
    if (S.ceoUploadBusy) {
        addMsg("附件仍在上传，请稍候再发送。", "system");
        return;
    }
    try {
        if (S.ceoTurnActive) {
            enqueueCeoFollowUp(activeSessionId(), { text, uploads });
            showToast({ title: "已加入队列", text: "会在当前轮结束后自动继续发送。", kind: "info" });
        } else {
            const sent = sendImmediateCeoMessage({ text, uploads, scrollMode: "bottom" });
            if (!sent) return;
        }
        U.ceoInput.value = "";
        S.ceoUploads = [];
        clearCeoComposerDraft(activeSessionId());
        syncCeoInputHeight();
        renderPendingCeoUploads();
        renderQueuedCeoFollowUps(activeSessionId());
        syncCeoPrimaryButton();
    } catch (e) {
        addMsg(`Failed to send message: ${e.message || "unknown error"}`, "system");
        initCeoWs();
    }
}
const canPause = (task) => !!task && !task.is_paused && pStatus(task.status) === "in_progress";
const canResume = (task) => !!task && !!task.is_paused;
const taskFailureClass = (task) => String(task?.failure_class || task?.metadata?.failure_class || "").trim().toLowerCase();
const taskFinalAcceptanceStatus = (task) => String(task?.final_acceptance?.status || task?.metadata?.final_acceptance?.status || "").trim().toLowerCase();
const taskContinuationState = (task) => String(task?.continuation_state || task?.metadata?.continuation_state || "").trim().toLowerCase();
const taskContinuedByTaskId = (task) => String(task?.continued_by_task_id || task?.metadata?.continued_by_task_id || "").trim();
const taskRetryCount = (task) => {
    const directCount = Number(task?.retry_count);
    if (Number.isInteger(directCount) && directCount >= 0) return directCount;
    const history = Array.isArray(task?.retry_history)
        ? task.retry_history
        : (Array.isArray(task?.metadata?.retry_history) ? task.metadata.retry_history : []);
    return history.length;
};
const taskRecoveryNotice = (task) => String(task?.recovery_notice || task?.metadata?.recovery_notice || "").trim();
const taskIsUnpassed = (task) => !!task && pStatus(task.status) === "success" && taskFinalAcceptanceStatus(task) === "failed";
const taskIsSuperseded = (task) => !!task && taskContinuationState(task) === "recreated" && !!taskContinuedByTaskId(task);
const taskContinuationSummary = (task) => {
    if (!task) return "";
    const continuationState = taskContinuationState(task);
    if (continuationState === "recreated") {
        const continuedByTaskId = taskContinuedByTaskId(task);
        return continuedByTaskId ? `已续跑到 ${continuedByTaskId}` : "已续跑到新任务";
    }
    if (continuationState === "retried_in_place") {
        const parts = [pStatus(task.status) === "in_progress" ? "原任务内续跑中" : "原任务已按原地重试续跑"];
        const retryCount = taskRetryCount(task);
        if (retryCount > 0) parts.push(`第${retryCount}次`);
        if (taskRecoveryNotice(task)) parts.push("恢复自失败快照");
        return parts.join(" · ");
    }
    return "";
};
const canRetry = (task) => !!task && !taskIsSuperseded(task) && pStatus(task.status) === "failed" && taskFailureClass(task) === "engine_failure";
const canContinueEvaluate = (task) => !!task && taskIsUnpassed(task) && taskFailureClass(task) === "business_unpassed";
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
    if (taskIsSuperseded(task)) return "continued";
    if (taskIsUnpassed(task)) return "unpassed";
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

function normalizeTaskModelCall(raw) {
    const source = raw && typeof raw === "object" ? raw : {};
    const toInt = (value) => {
        const num = Number(value);
        return Number.isFinite(num) && num >= 0 ? Math.floor(num) : 0;
    };
    return {
        call_index: toInt(source.call_index),
        created_at: String(source.created_at || "").trim(),
        prepared_message_count: toInt(source.prepared_message_count),
        prepared_message_chars: toInt(source.prepared_message_chars),
        response_tool_call_count: toInt(source.response_tool_call_count),
        delta_usage: normalizeTokenUsage(source.delta_usage),
        delta_usage_by_model: Array.isArray(source.delta_usage_by_model)
            ? source.delta_usage_by_model.map(normalizeModelTokenUsage)
            : [],
    };
}

function modelCallHitRate(call) {
    const data = normalizeTaskModelCall(call);
    const inputTokens = Number(data.delta_usage.input_tokens || 0);
    const cacheHitTokens = Number(data.delta_usage.cache_hit_tokens || 0);
    if (!inputTokens) return 0;
    return cacheHitTokens / inputTokens;
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
        `输入Token ${formatTokenCount(data.input_tokens)}`,
        `输出Token ${formatTokenCount(data.output_tokens)}`,
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
            button.className = "task-detail-pill task-detail-token-btn";
            button.type = "button";
            button.innerHTML = '<i data-lucide="pie-chart"></i><span>Token统计</span>';
            button.disabled = true;
            headerActions.appendChild(button);
            U.taskTokenButton = button;
            icons();
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
        if (S.selectedTool?.is_core) {
            deleteButton.textContent = "核心工具不可删除";
            deleteButton.disabled = true;
        } else {
            deleteButton.textContent = S.toolBusy ? "删除中..." : "删除";
            deleteButton.disabled = S.toolBusy || !S.selectedTool;
        }
    }
    const toggleButton = U.toolDetail?.querySelector(S.selectedTool?.enabled ? "#tool-disable-btn" : "#tool-enable-btn");
    if (toggleButton) {
        if (S.selectedTool?.is_core) toggleButton.disabled = true;
        else toggleButton.disabled = S.toolBusy || !S.selectedTool;
    }
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
    if (S.skillAutosaveTimerId) {
        window.clearTimeout(S.skillAutosaveTimerId);
        S.skillAutosaveTimerId = null;
    }
    S.skillAutosavePending = false;
    S.selectedSkill = null;
    S.skillFiles = [];
    S.skillContents = {};
    S.skillFileLoads = {};
    S.selectedSkillFile = "";
    S.skillDirty = false;
    renderSkills();
    renderSkillDetail();
}

function clearToolSelection() {
    if (S.toolAutosaveTimerId) {
        window.clearTimeout(S.toolAutosaveTimerId);
        S.toolAutosaveTimerId = null;
    }
    S.toolAutosavePending = false;
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
        stashTaskDetailViewState();
        setTaskTokenStatsOpen(false);
        clearAgentSelection({ rerender: false });
        if (S.taskWs) {
            S.taskWs.close();
            S.taskWs = null;
        }
        scheduleTaskDetailSessionPersist();
    }
    if (view === "tasks") {
        void loadTasks();
        initTasksWs();
        if (typeof ensureTaskListVisibleReconcile === "function") ensureTaskListVisibleReconcile();
    } else {
        closeTasksWs();
    }
    if (navView === "tasks" && typeof startTaskWorkerStatusPolling === "function") {
        startTaskWorkerStatusPolling();
    } else if (typeof stopTaskWorkerStatusPolling === "function") {
        stopTaskWorkerStatusPolling();
    }
    if (view === "skills") void loadSkills();
    if (view === "tools") void loadTools();
    if (view === "models") void loadModels();
    if (view === "communications") void loadCommunications();
}

function toggleTheme() {
    const html = document.documentElement;
    const dark = html.getAttribute("data-theme") === "dark";
    html.setAttribute("data-theme", dark ? "light" : "dark");
    const darkIcon = U.theme?.querySelector(".dark-icon");
    const lightIcon = U.theme?.querySelector(".light-icon");
    if (darkIcon && lightIcon) {
        darkIcon.style.display = dark ? "none" : "block";
        lightIcon.style.display = dark ? "block" : "none";
    }
}

function bind() {
    U.theme?.addEventListener("click", toggleTheme);
    U.projectExit?.addEventListener("click", () => void requestProjectExit());
    U.nav.forEach((btn) => btn.addEventListener("click", () => switchView(btn.dataset.view)));
    U.backToTasks?.addEventListener("click", () => switchView("tasks"));
    U.taskTokenButton?.addEventListener("click", () => setTaskTokenStatsOpen(true));
    U.taskTokenClose?.addEventListener("click", () => setTaskTokenStatsOpen(false));
    U.taskTokenBackdrop?.addEventListener("click", () => setTaskTokenStatsOpen(false));
    U.taskTokenContent?.addEventListener("click", (e) => {
        const control = e.target instanceof Element ? e.target.closest("[data-task-model-call-page]") : null;
        if (!control) return;
        const direction = String(control.dataset.taskModelCallPage || "").trim();
        if (direction === "prev") setTaskModelCallsPage((Number(S.taskModelCallsPage || 1) || 1) - 1);
        if (direction === "next") setTaskModelCallsPage((Number(S.taskModelCallsPage || 1) || 1) + 1);
    });
    U.nodeContextDisclosure?.addEventListener("toggle", () => void handleNodeContextDisclosureToggle());
    U.ceoSessionPanelToggle?.addEventListener("click", () => setCeoSessionPanelExpanded(!S.ceoSessionPanelExpanded));
    U.ceoNewSession?.addEventListener("click", () => void createNewCeoSession());
    U.ceoSessionTabLocal?.addEventListener("click", () => setCeoSessionTab("local"));
    U.ceoSessionTabChannel?.addEventListener("click", () => setCeoSessionTab("channel"));
    U.ceoSessionBulkToggle?.addEventListener("click", () => toggleCeoBulkMode());
    U.ceoSessionBulkDelete?.addEventListener("click", () => void requestDeleteSelectedCeoSessions());
    U.ceoSessionBulkSelectAll?.addEventListener("click", () => {
        toggleCeoBulkSelectAll();
        renderCeoSessions();
        syncCeoSessionActions();
    });
    U.renameSessionCancel?.addEventListener("click", handleRenameCancel);
    U.renameSessionAccept?.addEventListener("click", handleRenameAccept);
    U.renameSessionInput?.addEventListener("keydown", (e) => {
        if (e.key === "Enter") handleRenameAccept();
        if (e.key === "Escape") handleRenameCancel();
    });
    U.ceoSessionList?.addEventListener("click", (e) => {
        const bulkCheckbox = e.target.closest("[data-session-bulk-checkbox]");
        if (bulkCheckbox) {
            e.stopPropagation();
            toggleCeoBulkSessionSelection(bulkCheckbox.dataset.sessionBulkCheckbox);
            renderCeoSessions();
            syncCeoSessionActions();
            return;
        }
        const menuToggle = e.target.closest("[data-session-menu-toggle]");
        if (menuToggle) {
            e.stopPropagation();
            const sessionId = String(menuToggle.dataset.sessionMenuToggle || "").trim();
            const shell = menuToggle.closest(".ceo-session-actions");
            const isOpen = !!shell?.classList.contains("is-open");
            setCeoSessionMenuOpen(sessionId, !isOpen);
            return;
        }
        const activate = e.target.closest("[data-session-activate]");
        if (activate) {
            if (S.ceoBulkMode) {
                e.stopPropagation();
                toggleCeoBulkSessionSelection(activate.dataset.sessionActivate);
                renderCeoSessions();
                syncCeoSessionActions();
                return;
            }
            closeCeoSessionMenus();
            void activateCeoSession(activate.dataset.sessionActivate);
            return;
        }
        const rename = e.target.closest("[data-session-rename]");
        if (rename) {
            e.stopPropagation();
            closeCeoSessionMenus();
            void renameCeoSession(rename.dataset.sessionRename);
            return;
        }
        const remove = e.target.closest("[data-session-delete]");
        if (remove) {
            e.stopPropagation();
            closeCeoSessionMenus();
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
    U.ceoFollowUpQueue?.addEventListener("click", (e) => {
        const remove = e.target.closest("[data-follow-up-remove]");
        if (!remove) return;
        removeCeoQueuedFollowUp(activeSessionId(), String(remove.dataset.followUpRemove || ""));
    });
    U.ceoInput?.addEventListener("keydown", (e) => {
        if (e.key === "Enter" && !e.shiftKey) {
            e.preventDefault();
            handleCeoPrimaryAction();
        }
    });
    U.ceoInput?.addEventListener("input", () => {
        syncCeoInputHeight();
        syncActiveCeoComposerDraft();
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
        const field = e.target.closest("[data-model-role-iterations], [data-model-role-limit-input]");
        if (!(field instanceof HTMLElement)) return;
        syncRoleIterationDraftsFromInputs({ requireValid: false });
    });
    U.modelRoleEditors?.addEventListener("change", (e) => {
        if (!S.modelCatalog.roleEditing) return;
        const field = e.target.closest("[data-model-role-iterations], [data-model-role-limit-input], [data-model-role-limit-mode]");
        if (!(field instanceof HTMLElement)) return;
        if (field.matches("[data-model-role-limit-mode]")) {
            syncRoleIterationDraftsFromInputs({ requireValid: false });
            renderModelCatalog();
            return;
        }
        try {
            syncRoleIterationDraftsFromInputs({ requireValid: true });
            renderModelCatalog();
        } catch (error) {
            S.modelCatalog.error = error.message || "save failed";
            hint(`模型配置错误：${S.modelCatalog.error}`, true);
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
        const hoverZoneKey = `chain:${scope}`;
        if (dragState.hoverZoneKey !== hoverZoneKey) {
            clearModelDragDecorations();
            dragState.hoverZoneKey = hoverZoneKey;
        }
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
        dragState.hoverZoneKey = "";
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
        const hoverZoneKey = `available:${String(availableList.dataset.modelAvailableList || "")}`;
        if (dragState.hoverZoneKey !== hoverZoneKey) {
            clearModelDragDecorations();
            dragState.hoverZoneKey = hoverZoneKey;
        }
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
        dragState.hoverZoneKey = "";
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
        const nextValue = String(e.target.value || "").trim();
        if (nextValue === TASK_DEPTH_CUSTOM_VALUE) {
            S.taskDefaults.customMode = true;
            S.taskDefaults.customDraft = String(Math.max(0, normalizeInt(S.taskDefaults.maxDepth, S.taskDefaults.defaultMaxDepth)));
            renderTaskDepthControl();
            queueMicrotask(() => {
                U.taskDepthCustomInput?.focus();
                U.taskDepthCustomInput?.select();
            });
            return;
        }
        S.taskDefaults.customMode = false;
        S.taskDefaults.customDraft = "";
        void saveTaskDefaultMaxDepth(nextValue);
    });
    U.taskDepthCustomInput?.addEventListener("input", (e) => {
        S.taskDefaults.customDraft = String(e.target.value ?? "");
    });
    U.taskDepthCustomSave?.addEventListener("click", () => {
        void submitCustomTaskDepth();
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
    document.addEventListener("visibilitychange", () => {
        if (typeof ensureTaskListVisibleReconcile === "function") ensureTaskListVisibleReconcile();
    });
    U.taskBatchMenu?.querySelectorAll("[data-batch-action]")?.forEach((button) => button.addEventListener("click", async () => {
        await runTaskBatchAction(button.dataset.batchAction, { returnFocus: button });
    }));
    U.taskTreeResetRounds?.addEventListener("click", () => resetTaskTreeRoundSelections());
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
        if (!e.target.closest(".ceo-session-actions.toolbar-dropdown")) closeCeoSessionMenus();
        if (!e.target.closest(".toolbar-dropdown")) closeTaskMenus();
    });
    document.addEventListener("keydown", (e) => {
        if (e.key !== "Escape") return;
        if (closeResourceSelects({ restoreFocus: true })) return;
        if (closeCeoSessionMenus({ restoreFocus: true })) return;
        if (S.confirmState) {
            closeConfirm();
            return;
        }
        if (closeTaskMenus({ restoreFocus: true })) return;
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
    renderPendingCeoUploads();
    syncCeoInputHeight();
    syncCeoComposerReadonlyState();
    renderCeoSessions();
    syncCeoSessionPanelState();
    renderTaskSessionScope();
    syncCeoPrimaryButton();
}

let __g3kuAppInitialized = false;

function maybeInit() {
    if (__g3kuAppInitialized) return;
    if (window.G3kuBoot && typeof window.G3kuBoot.isUnlocked === "function" && !window.G3kuBoot.isUnlocked()) return;
    __g3kuAppInitialized = true;
    init();
}

function init() {
    ensureTaskTokenUi();
    enhanceResourceSelects();
    configureTaskDetailSections();
    bind();
    hydrateCeoComposerDraftCache();
    hydrateCeoFollowUpQueueCache();
    hydrateCeoSessionSnapshotCache();
    restoreCeoComposerDraftForSession(activeSessionId());
    startLiveDurationTicker();
    window.addEventListener("beforeunload", () => {
        flushCeoComposerDraftCachePersist();
        flushCeoFollowUpQueueCachePersist();
        flushCeoSessionSnapshotCachePersist();
        flushTaskDetailSessionPersist();
        stopLiveDurationTicker();
    });
    window.addEventListener("pagehide", () => {
        flushCeoComposerDraftCachePersist();
        flushCeoFollowUpQueueCachePersist();
        flushCeoSessionSnapshotCachePersist();
        flushTaskDetailSessionPersist();
    });
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
    void restoreTaskDetailSession();
    S.ceoScrollToLatestOnSnapshot = true;
    void refreshCeoSessions({ reconnect: true }).catch(() => {
        initCeoWs();
    });
}

document.addEventListener("DOMContentLoaded", maybeInit);
window.addEventListener("g3ku:boot-unlocked", maybeInit);







