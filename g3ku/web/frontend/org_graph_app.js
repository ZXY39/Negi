const MODEL_SCOPES = [
    { key: "ceo", label: "主Agent" },
    { key: "execution", label: "执行Agent" },
    { key: "inspection", label: "检验Agent" },
    { key: "memory", label: "记忆Agent" },
];

const EMPTY_MODEL_ROLES = () => ({ ceo: [], execution: [], inspection: [], memory: [] });
const DEFAULT_MODEL_DEFAULTS = () => ({ ceo: "", execution: "", inspection: "", memory: "" });
const DEFAULT_ROLE_ITERATIONS = () => ({ ceo: null, execution: null, inspection: null, memory: null });
const DEFAULT_ROLE_CONCURRENCY = () => ({ ceo: null, execution: null, inspection: null, memory: 1 });
const TREE_SCALE_MIN = 0.12;
const TREE_SCALE_MAX = 3.5;
const TREE_SCALE_FACTOR = 1.12;
// 搜索定位节点时统一放大到的固定缩放值，保证节点文字清晰可读
const TREE_FOCUS_SCALE = 1.2;
// 定位命中节点的放大脉冲动画时长，与 .task-tree-node-locate 的 CSS 动画保持一致
const TREE_LOCATE_HIGHLIGHT_MS = 1200;
const RESOURCE_PAGE_SIZES = [20, 50, 100];
const TASK_MODEL_CALLS_PAGE_SIZE = 100;
const TASK_DEPTH_PRESET_VALUES = Object.freeze([0, 1, 2, 3, 4, 5]);
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
// CEO websocket 解析失败后最多强制重连几次（拿到正常快照即清零），避免重连风暴。
const CEO_WS_PARSE_RESYNC_LIMIT = 3;
const CEO_CONTEXT_LOAD_NOTICE_DURATION_MS = 10000;
// 长按上下文脑图标：按住先静置 200ms 起手，之后才开始计时并显示进度环。
// 目的是让普通点击（含手抖的短按）完全不出现压缩进度反馈，计时从起手完成的那一刻算满 2 秒。
const CEO_BRAIN_HOLD_ARM_MS = 200;
// 长按上下文脑图标满 2 秒即发起手动压缩；进度环按住期间连续刷新。
const CEO_BRAIN_LONG_PRESS_MS = 2000;
const CEO_COMPRESSION_POLL_MS = 1000;
// 连续这么多次轮询失败才放弃跟踪；单次超时不算（大会话收尾会占住事件循环数秒）。
const CEO_COMPRESSION_POLL_FAIL_LIMIT = 5;
const CEO_COMPRESSION_DIVIDER_CLASS = "ceo-compression-divider";
const CEO_COMPRESSION_TEXT = {
    running: "上下文压缩中",
    completed: "会话已压缩",
    paused: "压缩已暂停",
};
const CEO_COMPOSER_DRAFT_CACHE_KEY = "g3ku.ceo.composer-drafts.v1";
const CEO_COMPOSER_DRAFT_CACHE_LIMIT = 24;
const CEO_FOLLOW_UP_QUEUE_CACHE_KEY = "g3ku.ceo.follow-up-queues.v1";
const CEO_FOLLOW_UP_QUEUE_CACHE_LIMIT = 24;
const CEO_FOLLOW_UP_QUEUE_PER_SESSION_LIMIT = 20;
const AUDIT_LAST_SEEN_KEY = "g3ku.audit.last-seen.v1";
const AUDIT_VIEW_POLL_MS = 15000;
const AUDIT_BADGE_POLL_MS = 30000;
const AUDIT_PAGE_SIZE = 100;
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
    ceoWsParseResyncs: 0,
    ceoPendingTurns: [],
    ceoTurnActive: false,
    ceoPauseBusy: false,
    ceoUploads: [],
    ceoUploadBusy: false,
    // 编辑重发模式:{sessionId, turnId, prevDraft} | null;Fork/编辑相关辅助状态。
    ceoEditResend: null,
    ceoWsOpenWaiters: [],
    ceoSessions: [],
    ceoLocalSessions: [],
    ceoChannelGroups: [],
    ceoSessionTab: "local",
    ceoSessionPanelExpanded: false,
    activeSessionFamily: "local",
    ceoSessionUnread: {},
    ceoSessionMessageCounts: {},
    ceoSessionHydrated: false,
    ceoSessionUnreadExempt: {},
    ceoBulkMode: false,
    ceoSelectedSessionIds: new Set(),
    // 拖动得到的手动位次：会话没有服务端顺序字段，顺序只在前端与 localStorage 生效。
    ceoSessionOrder: [],
    ceoSessionDrag: null,
    ceoScrollToLatestOnSnapshot: false,
    ceoFeedFollowLatest: true,
    ceoFeedWindowSource: null,
    ceoFeedAppendTarget: null,
    ceoFeedRenderSessionId: "",
    ceoFeedRenderSignature: "",
    ceoFeedRenderedMessageKeys: [],
    ceoSnapshotCache: {},
    ceoReplyDeltaBuffers: {},
    ceoReplyDeltaFrameId: 0,
    ceoSnapshotPersistId: null,
    ceoContextLoadNoticeSeq: 0,
    ceoContextLoadNoticeTimeoutIds: new Map(),
    ceoComposerDrafts: {},
    ceoComposerDraftPersistId: null,
    ceoQueuedFollowUps: {},
    ceoQueuedFollowUpsPersistId: null,
    // runtime 已经受理、正在排队的条目：来源是 ceo.state 的 queued_follow_up_messages。
    // 它是候选列表的真相，浏览器的 sessionStorage 只负责"还没发出去"的那一半。
    ceoServerQueuedFollowUps: {},
    ceoQueuedFollowUpDispatching: false,
    ceoComposerUsageEstimate: null,
    ceoComposerUsagePinnedEntries: null,
    ceoComposerUsageRefreshId: null,
    ceoComposerUsageRequestSeq: 0,
    ceoComposerUsageBusy: false,
    ceoComposerUsageNeedsRefresh: false,
    // 手动上下文压缩：本机按住脑图标满 2 秒后由服务端起跑，这里只记发起方与轮询。
    ceoContextCompressionStatus: "idle",
    ceoContextCompressionCancelRequested: false,
    ceoContextCompressionSessionId: "",
    ceoContextCompressionPollId: null,
    ceoContextCompressionPollFails: 0,
    ceoBrainHold: { active: false, startedAt: 0, rafId: null, sessionId: "" },
    // 长按满阈值后到达的那次 click 是副产物，不能顺带弹开模型面板。
    ceoBrainHoldConsumedClick: false,
    ceoModelSelection: {
        sessionId: "",
        mode: "chain",
        modelKey: "",
        pinnedAvailable: true,
        loaded: false,
        loading: false,
        saving: false,
        requestToken: 0,
        panelOpen: false,
        pickerOpen: false,
        paneTouched: false,
        pendingChainSwitch: false,
        search: "",
        chainKeys: [],
        dragFrom: -1,
        dropIndex: null,
        error: "",
    },
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
    taskWsReconnectTimer: null,
    tasksWs: null,
    currentTaskId: null,
    tasks: [],
    currentTask: null,
    taskSummary: null,
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
    treeSnapshotSelfHealToken: null,
    treeSnapshotSelfHealAttempts: 0,
    treeLargeMode: false,
    // 大树分块加载：加载期间渲染门闩与一行式进度提示条的归属（见
    // org_graph_task_view.js loadTaskTreeSnapshot / renderTree）。
    treeBulkLoadingTaskId: "",
    treeBulkLoadToken: 0,
    // 任务详情视图代次：离开详情视图时 +1，作废在途树请求的落地（见
    // org_graph_task_view.js cancelTaskTreeLoading / ensureTaskTreeSubtree）。
    treeDetailGeneration: 0,
    treeLoadNoticeTaskId: "",
    treeLoadNoticeTimer: null,
    treeLoadNoticeTimerTaskId: "",
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
    taskErrorLogs: [],
    taskErrorLogOpen: false,
    taskModelCallsPage: 1,
    taskModelCallsPageSize: TASK_MODEL_CALLS_PAGE_SIZE,
    taskModelCallsQuery: "",
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
    taskSortMode: "time",
    taskTerminalReconcileId: null,
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
    taskRuntimeSummary: null,
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
    treeFitOnNextRender: false,
    treeLocateHighlight: null,
    selectedNodeId: null,
    contextRiskCatalogRequest: null,
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
    memoryLoadedOnce: false,
    memoryBusy: false,
    memoryError: "",
    memoryQueueItems: [],
    memoryQueueTotal: 0,
    memoryQueueHasMore: false,
    memoryQueuePageSize: 20,
    memoryProcessedItems: [],
    memoryProcessedTotal: 0,
    memoryProcessedHasMore: false,
    memoryProcessedPageSize: 20,
    memoryFailedItems: [],
    memoryFailedTotal: 0,
    memoryFailedHasMore: false,
    memoryFailedPageSize: 20,
    memoryFailedMutationsEnabled: false,
    memoryFailedActionBusy: "",
    memoryQueueExpanded: {},
    memoryProcessedExpanded: {},
    memoryDetailPreview: {
        open: false,
        kind: "",
        key: "",
        title: "",
        subtitle: "",
        fields: [],
        primaryText: "",
        secondaryText: "",
    },
    memoryNotePreview: {
        open: false,
        busy: false,
        ref: "",
        body: "",
        error: "",
        requestToken: 0,
        editMode: false,
        editBody: "",
        saving: false,
        editable: true,
    },
    memoryBrowser: {
        open: false,
        busy: false,
        error: "",
        items: [],
        total: 0,
        search: "",
        sortKey: "created_at",
        sortDir: "desc",
        requestToken: 0,
        mutationsEnabled: false,
        editMode: false,
        selected: {},
        actionBusy: "",
        editDialog: {
            open: false,
            memoryId: "",
            body: "",
            minimal: "",
            busy: false,
        },
        deleteDialog: {
            open: false,
            memoryIds: [],
            syncNotes: true,
            notes: [],
            busy: false,
        },
    },
    memoryLastAlertText: "",
    memoryLastBlockedText: "",
    memoryPollIntervalId: null,
    auditLoadedOnce: false,
    auditPollIntervalId: null,
    auditBadgePollIntervalId: null,
    auditPage: 1,
    auditPageCount: 1,
    auditTotal: 0,
    auditBusy: false,
    auditLatestEventTs: "",
};

const U = {
    nav: [...document.querySelectorAll(".nav-item")],
    theme: document.getElementById("theme-toggle"),
    sidebar: document.querySelector(".sidebar"),
    sidebarToggle: document.getElementById("sidebar-toggle"),
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
    ceoScrollToLatestBtn: document.getElementById("ceo-scroll-to-latest-btn"),
    ceoInput: document.getElementById("ceo-input"),
    ceoAttach: document.getElementById("ceo-attach-btn"),
    ceoFileInput: document.getElementById("ceo-file-input"),
    ceoUploadList: document.getElementById("ceo-upload-list"),
    ceoFollowUpQueue: document.getElementById("ceo-follow-up-queue"),
    ceoEditResendBanner: document.getElementById("ceo-edit-resend-banner"),
    ceoContextLoadNotice: document.getElementById("ceo-context-load-notice"),
    ceoModelModePanel: document.getElementById("ceo-model-mode-panel"),
    ceoModelModeBadge: document.getElementById("ceo-model-mode-badge"),
    ceoModelModeUsageFill: document.getElementById("ceo-model-mode-usage-fill"),
    ceoModelModeUsageText: document.getElementById("ceo-model-mode-usage-text"),
    ceoModelModeChain: document.getElementById("ceo-model-mode-chain"),
    ceoModelModePinned: document.getElementById("ceo-model-mode-pinned"),
    ceoModelChainPane: document.getElementById("ceo-model-chain-pane"),
    ceoModelChainList: document.getElementById("ceo-model-chain-list"),
    ceoModelChainEmpty: document.getElementById("ceo-model-chain-empty"),
    ceoModelChainConfirm: document.getElementById("ceo-model-chain-confirm"),
    ceoModelChainConfirmText: document.getElementById("ceo-model-chain-confirm-text"),
    ceoModelChainConfirmAccept: document.getElementById("ceo-model-chain-confirm-accept"),
    ceoModelChainConfirmCancel: document.getElementById("ceo-model-chain-confirm-cancel"),
    ceoModelChainActions: document.getElementById("ceo-model-chain-actions"),
    ceoModelChainApply: document.getElementById("ceo-model-chain-apply"),
    ceoModelPicker: document.getElementById("ceo-model-picker"),
    ceoModelPickerSearch: document.getElementById("ceo-model-picker-search"),
    ceoModelPickerList: document.getElementById("ceo-model-picker-list"),
    ceoModelPickerEmpty: document.getElementById("ceo-model-picker-empty"),
    ceoModelModeNote: document.getElementById("ceo-model-mode-note"),
    ceoComposerUsageBrain: document.getElementById("ceo-context-usage-brain"),
    ceoComposerUsageBrainBase: document.getElementById("ceo-context-usage-brain-base"),
    ceoComposerUsageBrainFill: document.getElementById("ceo-context-usage-brain-fill"),
    ceoComposerUsageBrainRing: document.getElementById("ceo-context-usage-brain-ring"),
    ceoComposerUsageBrainHint: document.getElementById("ceo-context-usage-brain-hint"),
    ceoModelRetryToast: document.getElementById("ceo-model-retry-toast"),
    ceoModelRetryToastText: document.getElementById("ceo-model-retry-toast-text"),
    ceoSend: document.getElementById("ceo-send-btn"),
    viewCeo: document.getElementById("view-ceo"),
    viewTasks: document.getElementById("view-tasks-list"),
    viewSkills: document.getElementById("view-skills"),
    viewTools: document.getElementById("view-tools"),
    viewMemory: document.getElementById("view-memory"),
    viewModels: document.getElementById("view-models"),
    viewExternal: document.getElementById("view-external"),
    viewTaskDetails: document.getElementById("view-task-details"),
    viewAudit: document.getElementById("view-audit"),
    auditNavBadge: document.getElementById("audit-nav-badge"),
    auditEventList: document.getElementById("audit-event-list"),
    auditEventInfo: document.getElementById("audit-event-info"),
    auditPagePrev: document.getElementById("audit-page-prev"),
    auditPageNext: document.getElementById("audit-page-next"),
    auditRefresh: document.getElementById("audit-refresh-btn"),
    memoryAdminActions: document.getElementById("memory-admin-actions"),
    memoryRefresh: document.getElementById("memory-refresh-btn"),
    memoryViewCurrent: document.getElementById("memory-view-current-btn"),
    memoryQueueList: document.getElementById("memory-queue-list"),
    memoryQueueInfo: document.getElementById("memory-queue-info"),
    memoryQueueMore: document.getElementById("memory-queue-more-btn"),
    memoryProcessedList: document.getElementById("memory-processed-list"),
    memoryProcessedInfo: document.getElementById("memory-processed-info"),
    memoryProcessedMore: document.getElementById("memory-processed-more-btn"),
    memoryFailedList: document.getElementById("memory-failed-list"),
    memoryFailedInfo: document.getElementById("memory-failed-info"),
    memoryFailedMore: document.getElementById("memory-failed-more-btn"),
    memoryFailedPanel: document.getElementById("memory-failed-panel"),
    memoryDetailBackdrop: null,
    memoryDetailDrawer: null,
    memoryDetailTitle: null,
    memoryDetailSubtitle: null,
    memoryDetailMeta: null,
    memoryDetailPrimary: null,
    memoryDetailSecondary: null,
    memoryDetailActions: null,
    memoryDetailClose: null,
    memoryNoteBackdrop: null,
    memoryNoteDrawer: null,
    memoryNoteTitle: null,
    memoryNoteSubtitle: null,
    memoryNoteStatus: null,
    memoryNoteBody: null,
    memoryNoteClose: null,
    modelHint: document.getElementById("sidebar-model-hint"),
    modelRefresh: document.getElementById("model-refresh-btn"),
    modelCreate: document.getElementById("model-create-btn"),
    modelRolesCancel: document.getElementById("model-roles-cancel-btn"),
    modelRolesSave: document.getElementById("model-roles-save-btn"),
    modelRoleEditors: document.getElementById("model-role-editors"),
    modelRoleLimitsBar: document.getElementById("model-role-limits-bar"),
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
    taskSortSelect: document.getElementById("task-sort-select"),
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
    taskErrorLogButton: document.getElementById("task-error-log-btn"),
    taskErrorLogBackdrop: document.getElementById("task-error-log-backdrop"),
    taskErrorLogDrawer: document.getElementById("task-error-log-drawer"),
    taskErrorLogClose: document.getElementById("task-error-log-close"),
    taskErrorLogSummary: document.getElementById("task-error-log-summary"),
    taskErrorLogContent: document.getElementById("task-error-log-content"),
    taskTreeResetRounds: document.getElementById("task-tree-reset-rounds-btn"),
    taskTreeSearch: document.getElementById("task-tree-search"),
    taskTreeSearchInput: document.getElementById("task-tree-search-input"),
    taskTreeSearchResults: document.getElementById("task-tree-search-results"),
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
    adMessages: document.getElementById("ad-messages"),
    adNoticeComposer: document.getElementById("ad-notice-composer"),
    adNoticeInput: document.getElementById("ad-notice-input"),
    adNoticeSend: document.getElementById("ad-notice-send"),
    adSpawnReviews: document.getElementById("ad-spawn-reviews"),
    adOutput: document.getElementById("ad-output"),
    adAcceptance: document.getElementById("ad-check"),
    adErrorHistory: document.getElementById("ad-error-history"),
    adErrorHistoryRefresh: document.getElementById("ad-error-history-refresh"),
    adFlowHeading: document.getElementById("ad-input")?.closest(".agent-detail-section")?.querySelector("h4"),
    adMessagesHeading: document.getElementById("ad-messages")?.closest(".agent-detail-section")?.querySelector("h4"),
    adSpawnReviewsHeading: document.getElementById("ad-spawn-reviews")?.closest(".agent-detail-section")?.querySelector("h4"),
    adOutputHeading: document.getElementById("ad-output")?.closest(".agent-detail-section")?.querySelector("h4"),
    adAcceptanceHeading: document.getElementById("ad-check")?.closest(".agent-detail-section")?.querySelector("h4"),
    artifactHeading: document.getElementById("artifact-list")?.closest(".agent-detail-section")?.querySelector("h4"),
    adOutputSection: document.getElementById("ad-output")?.closest(".agent-detail-section"),
    adLogsSection: document.getElementById("ad-logs")?.closest(".agent-detail-section"),
    taskNodeModelRetryToast: document.getElementById("task-node-model-retry-toast"),
    taskNodeModelRetryToastText: document.getElementById("task-node-model-retry-toast-text"),
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
    toast: document.getElementById("app-toast"),
    toastTitle: document.getElementById("app-toast-title"),
    toastText: document.getElementById("app-toast-text"),
    toastClose: document.getElementById("app-toast-close"),
    taskLoadNotice: document.getElementById("task-load-notice"),
    taskLoadNoticeText: document.getElementById("task-load-notice-text"),
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
    projectSettings: document.getElementById("project-settings-btn"),
    projectSettingsBackdrop: document.getElementById("project-settings-backdrop"),
    projectSettingsDialog: document.getElementById("project-settings-dialog"),
    projectSettingsClose: document.getElementById("project-settings-close-btn"),
    projectSettingsOpenPassword: document.getElementById("project-settings-open-password-btn"),
    passwordChangeBackdrop: document.getElementById("password-change-backdrop"),
    passwordChangeDialog: document.getElementById("password-change-dialog"),
    passwordChangeClose: document.getElementById("password-change-close-btn"),
    projectSettingsCurrentPassword: document.getElementById("project-settings-current-password"),
    projectSettingsNewPassword: document.getElementById("project-settings-new-password"),
    projectSettingsNewPasswordConfirm: document.getElementById("project-settings-new-password-confirm"),
    projectSettingsChangePassword: document.getElementById("project-settings-change-password-btn"),
    projectSettingsAutoUnlock: document.getElementById("project-settings-auto-unlock"),
    projectSettingsLock: document.getElementById("project-settings-lock-btn"),
    projectSettingsExit: document.getElementById("project-settings-exit-btn"),
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
    if (String(item?.session_family || "").trim() === "channel") return true;
    const origin = String(item?.session_origin || "").trim();
    if (origin === "china" || origin === "external") return true;
    const sessionId = String(item?.session_id || "").trim();
    return sessionId.startsWith("china:") || sessionId.startsWith("ext:");
}

function deriveCeoChannelId(item = {}) {
    const explicit = String(item?.channel_id || "").trim();
    if (explicit) return explicit;
    const sessionId = String(item?.session_id || "").trim();
    if (sessionId.startsWith("ext:")) {
        const bridgeId = String(sessionId.split(":")[1] || "").trim();
        return bridgeId ? `ext:${bridgeId}` : "";
    }
    if (sessionId.startsWith("china:")) {
        return String(sessionId.split(":")[1] || "").trim();
    }
    return "";
}

function displayChannelGroupLabel(channelId) {
    const raw = String(channelId || "").trim();
    if (raw.startsWith("ext:")) {
        const bridgeId = raw.slice(4).trim();
        return `外部桥接 · ${bridgeId || "bridge"}`;
    }
    return displayChinaChannelLabel(raw);
}

function activeSessionIsReadonly() {
    return !!activeSessionItem()?.is_readonly;
}

function displayChinaChannelLabel(channelId) {
    return ({
        qqbot: "QQ Bot",
        dingtalk: "DingTalk",
        wecom: "企业微信",
        "wecom-app": "企业微信应用",
        "wecom-kf": "企业微信客服",
        "wechat-mp": "微信公众号",
        "feishu-china": "飞书",
    }[String(channelId || "").trim()] || String(channelId || "未知").trim() || "未知");
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

function ceoSessionManualRank(sessionId) {
    const order = Array.isArray(S.ceoSessionOrder) ? S.ceoSessionOrder : [];
    if (!order.length) return -1;
    return order.indexOf(String(sessionId || "").trim());
}

function sortCeoSessionsByTime(items = []) {
    return [...(Array.isArray(items) ? items : [])].sort((left, right) => {
        // 手动位次优先；没有位次的（拖动之后新建的）会话仍按创建时间倒序浮在手动区之上。
        const leftRank = ceoSessionManualRank(left?.session_id);
        const rightRank = ceoSessionManualRank(right?.session_id);
        if (leftRank !== rightRank) {
            if (leftRank < 0) return -1;
            if (rightRank < 0) return 1;
            return leftRank - rightRank;
        }
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
        U.ceoSessionPanelToggle.setAttribute("aria-label", expanded ? "收起会话列表" : "展开会话列表");
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
    renderCeoSessions();
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
    syncCeoModelModeControl();
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

const TRACE_OUTPUT_CLEANED_TEXT = "完整输出已被清理，仅保留预览片段";

// 只有 404 算终态（目标已被磁盘治理清理）；503/超时/传输失败仍要留重试机会。
function isTraceOutputMissingError(error) {
    return Number(error?.status) === 404;
}

function traceOutputMissingError() {
    const error = new Error("content_not_found");
    error.status = 404;
    return error;
}

async function getTraceOutputContentByRef(outputRef = "", { view = "canonical" } = {}) {
    const normalizedRef = normalizeTraceOutputRef(outputRef);
    if (!normalizedRef) return "";
    ensureTraceOutputContentState();
    const cacheKey = traceOutputContentCacheKey(normalizedRef, view);
    if (!cacheKey) return "";
    if (Object.prototype.hasOwnProperty.call(S.traceOutputContentByKey, cacheKey)) {
        const cached = S.traceOutputContentByKey[cacheKey];
        if (cached && typeof cached === "object" && cached.missing) {
            throw traceOutputMissingError();
        }
        return String(cached || "");
    }
    if (S.traceOutputRequestsByKey[cacheKey]) {
        return S.traceOutputRequestsByKey[cacheKey];
    }
    const request = (async () => {
        try {
            const payload = typeof ApiClient?.readContent === "function"
                ? await ApiClient.readContent({ ref: normalizedRef, view })
                : await ApiClient.openContent({ ref: normalizedRef, view, startLine: 1, endLine: 200 });
            const text = extractTraceOutputContentText(payload);
            S.traceOutputContentByKey[cacheKey] = text;
            return text;
        } catch (error) {
            if (isTraceOutputMissingError(error)) {
                S.traceOutputContentByKey[cacheKey] = { missing: true };
            }
            throw error;
        }
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
    setTextContentPreservingScroll(element, loadingText);
    try {
        const fullText = await getTraceOutputContentByRef(outputRef, { view });
        const nextText = String(fullText || previewText || emptyText).trim() || emptyText;
        setTextContentPreservingScroll(element, nextText);
        element.dataset.outputHydrated = "true";
        return nextText;
    } catch (error) {
        const fallbackText = String(previewText || emptyText).trim();
        if (isTraceOutputMissingError(error)) {
            setTextContentPreservingScroll(
                element,
                fallbackText ? `${fallbackText}\n\n${TRACE_OUTPUT_CLEANED_TEXT}` : TRACE_OUTPUT_CLEANED_TEXT,
            );
            element.dataset.outputHydrated = "cleaned";
            return fallbackText;
        }
        const message = typeof ApiClient?.friendlyErrorMessage === "function"
            ? ApiClient.friendlyErrorMessage(error, error?.message || "未知错误")
            : String(error?.message || error || "未知错误");
        setTextContentPreservingScroll(
            element,
            fallbackText
                ? `${fallbackText}\n\n${errorPrefix}${message}`
                : `${errorPrefix}${message}`
        );
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
        if (isTraceOutputMissingError(error)) {
            setCeoToolStepOutput(
                item,
                previewText ? `${previewText}\n\n${TRACE_OUTPUT_CLEANED_TEXT}` : TRACE_OUTPUT_CLEANED_TEXT,
            );
            item.dataset.outputHydrated = "cleaned";
            return previewText;
        }
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

function setTextContentPreservingScroll(element, text) {
    // 工具输出框自身就是滚动容器(封顶 max-height + overflow auto)。重写 textContent
    // 会替换全部子节点、把内容高度瞬间压到 0,浏览器随之把 scrollTop 归零——用户正在
    // 回翻输出时任何一次新输出/每秒时长刷新都会把阅读位置丢掉。文本没变时整体跳过,
    // 变了则原位恢复,最长不越过新内容的最大滚动量。
    if (!(element instanceof HTMLElement)) return;
    const next = String(text ?? "");
    if (element.textContent === next) return;
    const previousTop = Number(element.scrollTop || 0);
    element.textContent = next;
    if (previousTop > 0) setElementScrollTop(element, previousTop);
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
    // 切离编辑重发所在的会话时静默退出编辑模式(横幅随渲染消失)。
    if (S.ceoEditResend && String(S.ceoEditResend.sessionId || "") !== key) {
        exitCeoEditResendMode({ restoreDraft: false });
    }
    const draft = key ? getCeoComposerDraft(key) : null;
    S.ceoUploadBusy = false;
    S.ceoUploads = normalizeUploadList(draft?.uploads);
    if (U.ceoInput) U.ceoInput.value = String(draft?.text || "");
    if (U.ceoFileInput) U.ceoFileInput.value = "";
    renderPendingCeoUploads();
    renderQueuedCeoFollowUps(key);
    syncCeoInputHeight();
    scheduleCeoComposerUsageRefresh({ immediate: true });
}

function switchCeoComposerDraft(previousSessionId, nextSessionId) {
    const previousId = String(previousSessionId || "").trim();
    const nextId = String(nextSessionId || "").trim();
    if (previousId === nextId) return false;
    if (previousId) setCeoComposerDraft(previousId, captureCeoComposerDraftFromUi());
    restoreCeoComposerDraftForSession(nextId);
    return true;
}

function normalizeCeoComposerUsageEstimate(sessionId, payload = null) {
    const key = String(sessionId || "").trim();
    const item = payload && typeof payload === "object" ? payload : {};
    const toInt = (value) => {
        const num = Number(value);
        return Number.isFinite(num) && num >= 0 ? Math.floor(num) : 0;
    };
    const contextWindowTokens = toInt(item.context_window_tokens ?? item.contextWindowTokens);
    const estimatedTotalTokens = toInt(item.estimated_total_tokens ?? item.estimatedTotalTokens);
    const providerModel = String(item.provider_model || item.providerModel || "").trim();
    if (contextWindowTokens <= 0 || estimatedTotalTokens <= 0 || !providerModel) return null;
    const ratio = contextWindowTokens > 0
        ? Math.max(0, estimatedTotalTokens) / contextWindowTokens
        : Math.max(0, Number(item.ratio) || 0);
    return {
        session_id: key,
        estimated_total_tokens: estimatedTotalTokens,
        context_window_tokens: contextWindowTokens,
        ratio: Math.max(0, Number.isFinite(ratio) ? ratio : 0),
        provider_model: providerModel,
        trigger_tokens: toInt(item.trigger_tokens ?? item.triggerTokens),
        would_trigger_token_compression: !!(item.would_trigger_token_compression ?? item.wouldTriggerTokenCompression),
        would_exceed_context_window: !!(item.would_exceed_context_window ?? item.wouldExceedContextWindow),
        missing_context_window: !!(item.missing_context_window ?? item.missingContextWindow),
    };
}

function normalizeCeoRuntimeUsageDiagnostics(payload = null) {
    const item = payload && typeof payload === "object" ? payload : {};
    const toInt = (value) => {
        const num = Number(value);
        return Number.isFinite(num) && num >= 0 ? Math.floor(num) : 0;
    };
    const finalRequestTokens = toInt(item.final_request_tokens ?? item.finalRequestTokens);
    const maxContextTokens = toInt(
        item.max_context_tokens
        ?? item.maxContextTokens
        ?? item.context_window_tokens
        ?? item.contextWindowTokens
    );
    const providerModel = String(item.provider_model || item.providerModel || "").trim();
    if (finalRequestTokens <= 0 || maxContextTokens <= 0 || !providerModel) return null;
    return {
        final_request_tokens: finalRequestTokens,
        max_context_tokens: maxContextTokens,
        trigger_tokens: toInt(item.trigger_tokens ?? item.triggerTokens),
        effective_trigger_tokens: toInt(item.effective_trigger_tokens ?? item.effectiveTriggerTokens),
        effective_input_tokens: toInt(item.effective_input_tokens ?? item.effectiveInputTokens),
        estimate_source: String(item.estimate_source || item.estimateSource || "").trim(),
        provider_model: providerModel,
        applied: !!item.applied,
    };
}

function normalizeCeoRuntimeUsageEstimate(sessionId, inflightTurn = null) {
    const key = String(sessionId || "").trim();
    if (!key || !inflightTurn || typeof inflightTurn !== "object") return null;
    const diagnostics = normalizeCeoRuntimeUsageDiagnostics(inflightTurn.frontdoor_token_preflight_diagnostics);
    if (!diagnostics) return null;
    const estimatedTotalTokens = Number(diagnostics.final_request_tokens || 0);
    const contextWindowTokens = Number(diagnostics.max_context_tokens || 0);
    if (!Number.isFinite(estimatedTotalTokens) || estimatedTotalTokens <= 0) return null;
    if (!Number.isFinite(contextWindowTokens) || contextWindowTokens <= 0) return null;
    const ratio = Math.max(0, estimatedTotalTokens / contextWindowTokens);
    const effectiveTriggerTokens = Number(diagnostics.effective_trigger_tokens || 0);
    const triggerTokens = Number(diagnostics.trigger_tokens || 0);
    const activeTriggerTokens = effectiveTriggerTokens > 0 ? effectiveTriggerTokens : triggerTokens;
    return {
        session_id: key,
        estimated_total_tokens: Math.floor(estimatedTotalTokens),
        context_window_tokens: Math.floor(contextWindowTokens),
        ratio,
        provider_model: String(diagnostics.provider_model || "").trim(),
        trigger_tokens: activeTriggerTokens > 0 ? Math.floor(activeTriggerTokens) : 0,
        would_trigger_token_compression: activeTriggerTokens > 0 && estimatedTotalTokens >= activeTriggerTokens,
        would_exceed_context_window: estimatedTotalTokens > contextWindowTokens,
        missing_context_window: false,
        effective_input_tokens: Number(diagnostics.effective_input_tokens || 0),
        estimate_source: String(diagnostics.estimate_source || "").trim(),
        source: "runtime_snapshot",
    };
}

function clearCeoComposerUsageEstimate() {
    S.ceoComposerUsageEstimate = null;
    syncCeoComposerUsageOutline();
}

function setCeoComposerUsageEstimate(sessionId, payload) {
    const key = String(sessionId || "").trim();
    if (!key) {
        clearCeoComposerUsageEstimate();
        return null;
    }
    const normalized = normalizeCeoComposerUsageEstimate(key, payload);
    S.ceoComposerUsageEstimate = normalized;
    syncCeoComposerUsageOutline();
    return normalized;
}

function ceoModelDisplayTitle(item) {
    if (!item) return "";
    return String(item.name || "").trim()
        || String(item.key || "").trim()
        || String(item.provider_model || "").trim();
}

function ceoModelCatalogItem(modelKey) {
    const key = String(modelKey || "").trim();
    if (!key) return null;
    return (S.modelCatalog.catalog || []).find((item) => String(item?.key || "").trim() === key) || null;
}

function ceoModelUsageHeadlineTitle(raw) {
    const text = String(raw || "").trim();
    if (!text) return "";
    const byKey = ceoModelCatalogItem(text);
    if (byKey) return ceoModelDisplayTitle(byKey) || text;
    const byProviderModel = (S.modelCatalog.catalog || []).find(
        (item) => String(item?.provider_model || "").trim() === text,
    );
    if (byProviderModel) return ceoModelDisplayTitle(byProviderModel) || text;
    return text;
}

function ceoCurrentUsageEstimate() {
    const activeSession = String(activeSessionId() || "").trim();
    const runtimeEstimate = activeCeoRuntimeUsageEstimate(activeSession);
    const composerEstimate = (
        S.ceoComposerUsageEstimate
        && String(S.ceoComposerUsageEstimate.session_id || "").trim() === activeSession
    ) ? S.ceoComposerUsageEstimate : null;
    // 回合刚起跑时 runtime usage 还没到货，此时仍要拿上一份 composer 读数顶上：
    // 读数归零成「等待 Leader 上下文预估」会让人以为上下文被清空了。
    // runtime 一有新值就自动压过它，所以这里不需要按回合状态分叉。
    return runtimeEstimate || composerEstimate;
}

function ceoModelChainKeys() {
    const roles = S.modelCatalog && S.modelCatalog.roles ? S.modelCatalog.roles : null;
    return (Array.isArray(roles?.ceo) ? roles.ceo : [])
        .map((ref) => String(ref || "").trim())
        .filter(Boolean);
}

function ceoModelSelectionFor(sessionId) {
    const key = String(sessionId || "").trim();
    if (!key) return null;
    return String(S.ceoModelSelection.sessionId || "").trim() === key ? S.ceoModelSelection : null;
}

function resetCeoModelSelection(sessionId) {
    const key = String(sessionId || "").trim();
    S.ceoModelSelection = {
        ...S.ceoModelSelection,
        sessionId: key,
        mode: "chain",
        modelKey: "",
        pinnedAvailable: true,
        loaded: false,
        loading: false,
        saving: false,
        error: "",
        panelOpen: false,
        pickerOpen: false,
        paneTouched: false,
        pendingChainSwitch: false,
        search: "",
        chainKeys: ceoModelChainKeys(),
        dragFrom: -1,
        dropIndex: null,
    };
    syncCeoModelModeControl();
}

function applyCeoModelSelectionPayload(sessionId, payload) {
    const key = String(sessionId || "").trim();
    const data = payload && typeof payload === "object" ? payload : {};
    const modelKey = String(data.model_key || data.modelKey || "").trim();
    const mode = String(data.mode || "").trim() === "model" && modelKey ? "model" : "chain";
    S.ceoModelSelection = {
        ...S.ceoModelSelection,
        sessionId: String(data.session_id || key).trim() || key,
        mode,
        modelKey: mode === "model" ? modelKey : "",
        pinnedAvailable: data.pinned_available !== false,
        loaded: true,
        loading: false,
        saving: false,
        error: "",
        chainKeys: ceoModelChainKeys(),
    };
    syncCeoModelModeControl();
    return S.ceoModelSelection;
}

async function refreshCeoModelSelection(sessionId, { force = false } = {}) {
    const key = String(sessionId || "").trim();
    if (!key) {
        resetCeoModelSelection(key);
        return null;
    }
    const current = S.ceoModelSelection;
    // 只有确实加载过该会话才复用；reset 只写 sessionId，不能当成已加载。
    if (!force && String(current.sessionId || "").trim() === key && current.loaded) return current;
    if (String(current.sessionId || "").trim() === key && current.loading) return current;
    current.requestToken += 1;
    const token = current.requestToken;
    S.ceoModelSelection = { ...current, sessionId: key, loading: true, error: "" };
    syncCeoModelModeControl();
    try {
        const payload = await ApiClient.getCeoSessionModelSelection(key);
        if (token !== S.ceoModelSelection.requestToken) return null;
        return applyCeoModelSelectionPayload(key, payload);
    } catch (error) {
        if (token !== S.ceoModelSelection.requestToken) return null;
        S.ceoModelSelection = {
            ...S.ceoModelSelection,
            sessionId: key,
            loading: false,
            error: String(error?.message || "load_failed"),
        };
        syncCeoModelModeControl();
        return null;
    }
}

async function saveCeoModelSelection(mode, modelKey = "") {
    const sessionId = String(activeSessionId() || "").trim();
    const nextMode = mode === "model" ? "model" : "chain";
    const nextKey = nextMode === "model" ? String(modelKey || "").trim() : "";
    if (!sessionId || S.ceoModelSelection.saving) return null;
    if (nextMode === "model" && !nextKey) return null;
    const current = ceoModelSelectionFor(sessionId) || S.ceoModelSelection;
    if (current.mode === nextMode && String(current.modelKey || "") === nextKey) {
        if (nextMode === "model") closeCeoModelModePanel();
        else {
            S.ceoModelSelection = { ...S.ceoModelSelection, pendingChainSwitch: false };
            syncCeoModelModeControl();
        }
        return current;
    }
    S.ceoModelSelection = { ...current, sessionId, saving: true, error: "" };
    syncCeoModelModeControl();
    try {
        const payload = await ApiClient.updateCeoSessionModelSelection(
            sessionId,
            nextMode === "model" ? { mode: "model", model_key: nextKey } : { mode: "chain" },
        );
        if (sessionId !== String(activeSessionId() || "").trim()) return null;
        const applied = applyCeoModelSelectionPayload(sessionId, payload);
        S.ceoModelSelection = { ...S.ceoModelSelection, pendingChainSwitch: false };
        // 选好固定模型即收起面板；切回模型链留在面板里继续看链。
        if (nextMode === "model") closeCeoModelModePanel();
        else syncCeoModelModeControl();
        // 固定模型会改变上下文窗口判定，用量表必须跟着重算。
        scheduleCeoComposerUsageRefresh({ immediate: true });
        showToast({
            title: "模型模式已更新",
            text: nextMode === "model" ? "本会话已固定使用所选模型" : "本会话已恢复模型链",
            kind: "success",
        });
        return applied;
    } catch (error) {
        if (sessionId !== String(activeSessionId() || "").trim()) return null;
        S.ceoModelSelection = {
            ...S.ceoModelSelection,
            sessionId,
            saving: false,
            error: String(error?.message || "save_failed"),
        };
        syncCeoModelModeControl();
        showToast({
            title: "模型模式保存失败",
            text: String(error?.message || "请稍后重试"),
            kind: "error",
        });
        return null;
    }
}

async function saveCeoModelChain(keys) {
    const sessionId = String(activeSessionId() || "").trim();
    const modelKeys = normalizeModelRoleChain(keys);
    if (!sessionId || S.ceoModelSelection.saving) return null;
    if (!modelKeys.length) return null;
    S.ceoModelSelection = { ...S.ceoModelSelection, saving: true, error: "" };
    syncCeoModelModeControl();
    try {
        const payload = await ApiClient.updateModelRoleChain("ceo", {
            modelKeys,
            maxIterations: S.modelCatalog.roleIterations?.ceo ?? null,
            maxConcurrency: S.modelCatalog.roleConcurrency?.ceo ?? null,
        });
        if (payload) applyModelCatalog(payload, { preserveRoleDrafts: true });
        S.ceoModelSelection = {
            ...S.ceoModelSelection,
            saving: false,
            error: "",
            chainKeys: ceoModelChainKeys(),
            dragFrom: -1,
            dropIndex: null,
        };
        syncCeoModelModeControl();
        scheduleCeoComposerUsageRefresh({ immediate: true });
        showToast({ title: "模型链已更新", text: "新的优先级对全部模型链会话生效", kind: "success" });
        return payload;
    } catch (error) {
        // 保存失败回到服务端顺序，避免面板显示一份没生效的链。
        S.ceoModelSelection = {
            ...S.ceoModelSelection,
            saving: false,
            error: String(error?.message || "save_failed"),
            chainKeys: ceoModelChainKeys(),
            dragFrom: -1,
            dropIndex: null,
        };
        syncCeoModelModeControl();
        showToast({ title: "模型链保存失败", text: String(error?.message || "请稍后重试"), kind: "error" });
        return null;
    }
}

async function ensureCeoModelCatalog() {
    if (S.modelCatalog.loading || (S.modelCatalog.catalog || []).length) return;
    try {
        await loadModels();
    } catch (error) {
        void error;
    }
    renderCeoModelPicker();
    renderCeoModelChainPane();
}

function filterCeoModelPickerModels() {
    const query = String(S.ceoModelSelection.search || "").trim().toLowerCase();
    const catalog = [...(S.modelCatalog.catalog || [])];
    const matched = query
        ? catalog.filter((item) => [ceoModelDisplayTitle(item), item.key, item.provider_model, item.name, item.description]
            .join("\n")
            .toLowerCase()
            .includes(query))
        : catalog;
    return matched.sort((left, right) => ceoModelDisplayTitle(left).localeCompare(ceoModelDisplayTitle(right), "zh-Hans-CN"));
}

function renderCeoModelPicker() {
    if (!U.ceoModelPickerList) return;
    const sessionId = String(activeSessionId() || "").trim();
    const selection = ceoModelSelectionFor(sessionId);
    if (!selection || !selection.panelOpen || !selection.pickerOpen) {
        U.ceoModelPickerList.innerHTML = "";
        return;
    }
    const pinnedKey = String(selection.modelKey || "");
    const models = filterCeoModelPickerModels();
    if (U.ceoModelPickerEmpty) U.ceoModelPickerEmpty.hidden = models.length > 0;
    U.ceoModelPickerList.innerHTML = models.map((item) => {
        const key = String(item.key || "").trim();
        const title = ceoModelDisplayTitle(item) || key;
        const subtitle = String(item.provider_model || "").trim() || key;
        const isDisabled = item.enabled === false;
        const isSelected = key === pinnedKey;
        return `
            <button type="button" class="ceo-model-picker-item${isSelected ? " is-selected" : ""}" role="option"
                aria-selected="${isSelected ? "true" : "false"}" data-ceo-model-pick="${esc(key)}"${isDisabled ? " disabled" : ""}>
                <span class="ceo-model-picker-item-main">
                    <span class="ceo-model-picker-item-title">${esc(title)}</span>
                    <span class="ceo-model-picker-item-subtitle">${esc(subtitle)}</span>
                </span>
                ${isDisabled ? '<span class="ceo-model-picker-item-chip">已禁用</span>' : ""}
                <i data-lucide="check" class="ceo-model-picker-item-check" aria-hidden="true"></i>
            </button>`;
    }).join("");
    icons();
}

function ceoModelChainDraft() {
    const keys = S.ceoModelSelection.chainKeys;
    return Array.isArray(keys) ? keys : [];
}

function ceoModelChainDirty() {
    const draft = ceoModelChainDraft();
    const serverKeys = ceoModelChainKeys();
    return draft.length !== serverKeys.length || draft.some((key, index) => key !== serverKeys[index]);
}

// 固定模型被删除/禁用时运行时已回退模型链：面板按实际生效的模式展示。
function ceoModelEffectivePinnedKey(selection) {
    const mode = String(selection?.mode || "chain");
    const pinnedKey = mode === "model" ? String(selection?.modelKey || "") : "";
    if (!pinnedKey) return "";
    return selection?.pinnedAvailable === false ? "" : pinnedKey;
}

function ceoModelStalePinnedKey(selection) {
    const mode = String(selection?.mode || "chain");
    const pinnedKey = mode === "model" ? String(selection?.modelKey || "") : "";
    return pinnedKey && selection?.pinnedAvailable === false ? pinnedKey : "";
}

function renderCeoModelChainPane() {
    if (!U.ceoModelChainList) return;
    const sessionId = String(activeSessionId() || "").trim();
    const selection = ceoModelSelectionFor(sessionId);
    const visible = !!selection?.panelOpen && !selection?.pickerOpen;
    U.ceoModelChainList.innerHTML = "";
    if (U.ceoModelChainEmpty) U.ceoModelChainEmpty.hidden = true;
    if (U.ceoModelChainActions) U.ceoModelChainActions.hidden = true;
    if (!visible) return;
    const keys = ceoModelChainDraft();
    if (U.ceoModelChainEmpty) U.ceoModelChainEmpty.hidden = keys.length > 0;
    if (U.ceoModelChainActions) U.ceoModelChainActions.hidden = !ceoModelChainDirty();
    if (U.ceoModelChainApply) U.ceoModelChainApply.disabled = !!selection?.saving;
    U.ceoModelChainList.innerHTML = keys.map((key, index) => {
        const item = ceoModelCatalogItem(key);
        const title = ceoModelDisplayTitle(item) || key;
        return `
            <article class="ceo-model-chain-row" draggable="true" role="listitem"
                data-ceo-chain-index="${index}" data-ceo-chain-key="${esc(key)}">
                <span class="ceo-model-chain-grip" aria-hidden="true">&#9776;</span>
                <span class="ceo-model-chain-title">${esc(title)}</span>
            </article>`;
    }).join("");
}

function clearCeoModelChainDragDecorations() {
    const list = U.ceoModelChainList;
    if (!list) return;
    list.querySelectorAll(".is-drop-target").forEach((item) => item.classList.remove("is-drop-target"));
    list.querySelectorAll(".is-dragging").forEach((item) => item.classList.remove("is-dragging"));
    list.querySelectorAll("[data-ceo-chain-placeholder]").forEach((item) => item.remove());
    list.classList.remove("is-drop-zone");
}

function ceoModelChainDropIndex(list, clientY) {
    const cards = [...list.querySelectorAll("[data-ceo-chain-index]")];
    for (const card of cards) {
        const rect = card.getBoundingClientRect();
        if (clientY < rect.top + (rect.height / 2)) return Number(card.dataset.ceoChainIndex);
    }
    return cards.length;
}

function beginCeoModelChainDrag(event) {
    const list = U.ceoModelChainList;
    const card = event.target instanceof Element ? event.target.closest("[data-ceo-chain-index]") : null;
    if (!list || !card || S.ceoModelSelection.saving) return;
    const index = Number(card.dataset.ceoChainIndex);
    if (!Number.isInteger(index) || index < 0) return;
    S.ceoModelSelection = { ...S.ceoModelSelection, dragFrom: index, dropIndex: null };
    card.classList.add("is-dragging");
    if (event.dataTransfer) {
        event.dataTransfer.effectAllowed = "move";
        try {
            event.dataTransfer.setData("text/plain", String(index));
        } catch (error) {
            void error;
        }
    }
}

function updateCeoModelChainDropTarget(event) {
    const list = U.ceoModelChainList;
    const from = Number(S.ceoModelSelection.dragFrom);
    if (!list || !Number.isInteger(from) || from < 0) return;
    event.preventDefault();
    if (event.dataTransfer) event.dataTransfer.dropEffect = "move";
    const targetIndex = ceoModelChainDropIndex(list, event.clientY);
    // 目标位置没变就不重排占位符，避免拖动时列表抖动。
    if (S.ceoModelSelection.dropIndex === targetIndex && list.querySelector("[data-ceo-chain-placeholder]")) return;
    clearCeoModelChainDragDecorations();
    S.ceoModelSelection = { ...S.ceoModelSelection, dropIndex: targetIndex };
    const cards = [...list.querySelectorAll("[data-ceo-chain-index]")];
    const dragging = cards.find((card) => Number(card.dataset.ceoChainIndex) === from);
    if (dragging) dragging.classList.add("is-dragging");
    const anchor = cards.find((card) => Number(card.dataset.ceoChainIndex) === targetIndex) || null;
    const placeholder = document.createElement("div");
    placeholder.className = "model-chain-drop-placeholder";
    placeholder.dataset.ceoChainPlaceholder = "1";
    if (anchor && anchor !== dragging) {
        anchor.classList.add("is-drop-target");
        list.insertBefore(placeholder, anchor);
    } else {
        list.appendChild(placeholder);
    }
    list.classList.add("is-drop-zone");
}

function finishCeoModelChainDrag(event) {
    event.preventDefault();
    const from = Number(S.ceoModelSelection.dragFrom);
    const targetIndex = S.ceoModelSelection.dropIndex;
    clearCeoModelChainDragDecorations();
    if (!Number.isInteger(from) || from < 0) return;
    const keys = [...ceoModelChainDraft()];
    const insertAt = Number.isInteger(targetIndex) && targetIndex > from ? targetIndex - 1 : targetIndex;
    const nextKeys = keys.filter((_key, index) => index !== from);
    if (Number.isInteger(insertAt)) {
        const bounded = Math.max(0, Math.min(nextKeys.length, insertAt));
        nextKeys.splice(bounded, 0, keys[from]);
    } else {
        nextKeys.push(keys[from]);
    }
    // 拖动只改草稿：点「应用」才写回模型链。
    S.ceoModelSelection = { ...S.ceoModelSelection, chainKeys: nextKeys, dragFrom: -1, dropIndex: null };
    renderCeoModelChainPane();
}

function ceoModelBadgeTitle(estimate) {
    const sessionId = String(activeSessionId() || "").trim();
    const effectiveKey = ceoModelEffectivePinnedKey(ceoModelSelectionFor(sessionId));
    const fromEstimate = estimate ? ceoModelUsageHeadlineTitle(estimate.provider_model) : "";
    return fromEstimate || ceoModelDisplayTitle(ceoModelCatalogItem(effectiveKey)) || "";
}

function syncCeoModelModePanelUsage() {
    const panel = U.ceoModelModePanel;
    const badge = U.ceoModelModeBadge;
    const fill = U.ceoModelModeUsageFill;
    const text = U.ceoModelModeUsageText;
    if (!badge || !fill || !text) return;
    const estimate = ceoCurrentUsageEstimate();
    const hasEstimate = !!estimate;
    const ratio = hasEstimate ? Math.max(0, Math.min(1, Number(estimate.ratio) || 0)) : 0;
    const visualRatio = hasEstimate && ratio > 0 ? Math.max(ratio, 0.06) : 0;
    const hue = Math.max(0, Math.min(145, 145 - (visualRatio * 145)));
    if (typeof panel?.style?.setProperty === "function") {
        panel.style.setProperty("--ceo-context-usage-color", `hsl(${hue.toFixed(1)} 82% 58%)`);
    }
    fill.style.width = `${Math.max(0, Math.min(100, visualRatio * 100))}%`;
    const title = ceoModelBadgeTitle(estimate);
    badge.textContent = title || "等待 Leader 上下文预估";
    badge.setAttribute?.("title", title);
    badge.classList?.toggle("is-pending", !title);
    // 进度条下方只留 token 占用值；没有数据时整行隐藏，不再重复脑图标上的占位文案。
    text.hidden = !hasEstimate;
    text.textContent = hasEstimate
        ? `${estimate.estimated_total_tokens}/${estimate.context_window_tokens} TOKEN`
        : "";
}

function ceoModelChainConfirmVisible(selection) {
    return !!selection?.pendingChainSwitch && !!selection?.panelOpen && !selection?.pickerOpen;
}

function syncCeoModelModeControl() {
    const sessionId = String(activeSessionId() || "").trim();
    let selection = ceoModelSelectionFor(sessionId);
    const panelOpen = !!selection?.panelOpen && !!sessionId;
    const effectiveKey = ceoModelEffectivePinnedKey(selection);
    // 板块跟随当前生效的模式：手动切过板块、或正在确认切回模型链时不自动拉回。
    if (panelOpen && !selection?.paneTouched && !selection?.pendingChainSwitch) {
        const nextPickerOpen = !!effectiveKey;
        if (!!selection?.pickerOpen !== nextPickerOpen) {
            S.ceoModelSelection = { ...S.ceoModelSelection, pickerOpen: nextPickerOpen };
            selection = ceoModelSelectionFor(sessionId);
        }
    }
    const staleKey = ceoModelStalePinnedKey(selection);
    const brain = U.ceoComposerUsageBrain;
    if (brain) {
        brain.classList.toggle("is-panel-open", panelOpen);
        brain.setAttribute("aria-expanded", panelOpen ? "true" : "false");
    }
    if (U.ceoModelModePanel) U.ceoModelModePanel.hidden = !panelOpen;
    if (U.ceoModelModeChain) U.ceoModelModeChain.setAttribute("aria-checked", effectiveKey ? "false" : "true");
    if (U.ceoModelModePinned) U.ceoModelModePinned.setAttribute("aria-checked", effectiveKey ? "true" : "false");
    if (U.ceoModelPicker) U.ceoModelPicker.hidden = !(panelOpen && selection?.pickerOpen);
    if (U.ceoModelChainPane) U.ceoModelChainPane.hidden = !(panelOpen && !selection?.pickerOpen);
    if (U.ceoModelModeNote) {
        const note = staleKey
            ? "固定的模型已被删除或禁用，本会话已自动回退模型链。"
            : "";
        U.ceoModelModeNote.textContent = note;
        U.ceoModelModeNote.hidden = !note;
    }
    if (U.ceoModelChainConfirm) {
        const asking = ceoModelChainConfirmVisible(selection);
        U.ceoModelChainConfirm.hidden = !asking;
        if (U.ceoModelChainConfirmText) {
            const currentTitle = ceoModelDisplayTitle(ceoModelCatalogItem(effectiveKey)) || effectiveKey;
            U.ceoModelChainConfirmText.textContent = asking
                ? `当前会话固定使用 ${currentTitle}，切换到模型链？`
                : "";
        }
        if (U.ceoModelChainConfirmAccept) U.ceoModelChainConfirmAccept.disabled = !!selection?.saving;
    }
    if (panelOpen) syncCeoModelModePanelUsage();
    renderCeoModelPicker();
    renderCeoModelChainPane();
}

function openCeoModelModePanel() {
    const sessionId = String(activeSessionId() || "").trim();
    if (!sessionId) return;
    const current = ceoModelSelectionFor(sessionId) || S.ceoModelSelection;
    const effectiveKey = ceoModelEffectivePinnedKey(current);
    S.ceoModelSelection = {
        ...current,
        sessionId,
        panelOpen: true,
        // 打开即停在正在使用的板块：固定生效则直接进指定模型列表。
        pickerOpen: !!effectiveKey,
        paneTouched: false,
        pendingChainSwitch: false,
        search: "",
        chainKeys: ceoModelChainKeys(),
        dragFrom: -1,
        dropIndex: null,
    };
    if (U.ceoModelPickerSearch) U.ceoModelPickerSearch.value = "";
    syncCeoModelModeControl();
    if (S.ceoModelSelection.pickerOpen) U.ceoModelPickerSearch?.focus();
    void refreshCeoModelSelection(sessionId);
    void ensureCeoModelCatalog();
}

function closeCeoModelModePanel() {
    if (!S.ceoModelSelection.panelOpen && !S.ceoModelSelection.pickerOpen) return false;
    S.ceoModelSelection = {
        ...S.ceoModelSelection,
        panelOpen: false,
        pickerOpen: false,
        paneTouched: false,
        pendingChainSwitch: false,
        search: "",
        dragFrom: -1,
        dropIndex: null,
    };
    syncCeoModelModeControl();
    return true;
}

function openCeoModelModePicker() {
    const sessionId = String(activeSessionId() || "").trim();
    if (!sessionId) return;
    S.ceoModelSelection = {
        ...S.ceoModelSelection,
        sessionId,
        panelOpen: true,
        pickerOpen: true,
        paneTouched: true,
        pendingChainSwitch: false,
        search: "",
    };
    if (U.ceoModelPickerSearch) U.ceoModelPickerSearch.value = "";
    syncCeoModelModeControl();
    U.ceoModelPickerSearch?.focus();
    void ensureCeoModelCatalog();
}

function showCeoModelChainPane() {
    const sessionId = String(activeSessionId() || "").trim();
    if (!sessionId) return;
    const selection = ceoModelSelectionFor(sessionId) || S.ceoModelSelection;
    const effectiveKey = ceoModelEffectivePinnedKey(selection);
    S.ceoModelSelection = {
        ...selection,
        sessionId,
        panelOpen: true,
        pickerOpen: false,
        paneTouched: true,
        // 固定生效时先确认再切回模型链；本来就是模型链则无需确认。
        pendingChainSwitch: !!effectiveKey,
        search: "",
        chainKeys: ceoModelChainKeys(),
        dragFrom: -1,
        dropIndex: null,
    };
    syncCeoModelModeControl();
}

function cancelCeoModelChainSwitch() {
    if (!S.ceoModelSelection.pendingChainSwitch) return false;
    // 取消后回到正在使用的板块（固定生效时即指定模型列表）。
    S.ceoModelSelection = { ...S.ceoModelSelection, pendingChainSwitch: false, paneTouched: false };
    syncCeoModelModeControl();
    return true;
}

function confirmCeoModelChainSwitch() {
    if (!S.ceoModelSelection.pendingChainSwitch) return null;
    return saveCeoModelSelection("chain");
}

// ---- 长按上下文脑图标：按住满 2 秒发起手动压缩 -----------------------------

function setCeoBrainHoldProgress(progress) {
    const shell = U.ceoComposerUsageBrain;
    const clamped = Math.max(0, Math.min(1, Number(progress) || 0));
    if (typeof shell?.style?.setProperty === "function") {
        shell.style.setProperty("--ceo-brain-hold", String(clamped));
    }
    shell?.classList?.toggle("is-holding", clamped > 0);
}

function ceoBrainHoldBlockedReason() {
    const sessionId = String(activeSessionId() || "").trim();
    if (!sessionId) return "";
    if (S.ceoUploadBusy) return "附件上传中，请稍后再试";
    if (activeCeoSessionCompressionState()) return "正在压缩上下文";
    return "";
}

function beginCeoBrainHold(event) {
    if (event?.button !== undefined && Number(event.button) !== 0) return;
    if (event?.pointerType === "touch") return;
    const hold = S.ceoBrainHold;
    if (hold.active) return;
    hold.active = true;
    hold.startedAt = Date.now();
    hold.sessionId = String(activeSessionId() || "").trim();
    stepCeoBrainHold();
}

function stepCeoBrainHold() {
    const hold = S.ceoBrainHold;
    if (!hold.active) return;
    const elapsed = Date.now() - Number(hold.startedAt || 0);
    if (elapsed < CEO_BRAIN_HOLD_ARM_MS) {
        // 起手窗口里什么都不画：短按（普通点击）不该先闪出一个进度环再取消。
        hold.rafId = window.requestAnimationFrame(() => stepCeoBrainHold());
        return;
    }
    const progress = Math.min(1, (elapsed - CEO_BRAIN_HOLD_ARM_MS) / CEO_BRAIN_LONG_PRESS_MS);
    setCeoBrainHoldProgress(progress);
    if (progress < 1) {
        hold.rafId = window.requestAnimationFrame(() => stepCeoBrainHold());
        return;
    }
    const sessionId = String(hold.sessionId || "");
    const blocked = ceoBrainHoldBlockedReason();
    // 满阈值后这次按住要吞掉随后到达的 click，否则松手会顺手把模型面板弹开。
    S.ceoBrainHoldConsumedClick = true;
    finishCeoBrainHold();
    if (blocked) {
        showToast({ title: "暂不能压缩上下文", text: blocked, kind: "warn" });
        return;
    }
    void beginCeoContextCompression(sessionId);
}

function finishCeoBrainHold() {
    const hold = S.ceoBrainHold;
    if (!hold.active) return false;
    hold.active = false;
    hold.startedAt = 0;
    hold.sessionId = "";
    if (hold.rafId !== null && hold.rafId !== undefined) {
        window.cancelAnimationFrame(hold.rafId);
    }
    hold.rafId = null;
    setCeoBrainHoldProgress(0);
    return true;
}

function bindCeoModelModeControls() {
    const brain = U.ceoComposerUsageBrain;
    brain?.addEventListener("click", (event) => {
        event.stopPropagation();
        if (S.ceoBrainHoldConsumedClick) {
            S.ceoBrainHoldConsumedClick = false;
            return;
        }
        if (S.ceoModelSelection.panelOpen) closeCeoModelModePanel();
        else openCeoModelModePanel();
    });
    brain?.addEventListener("pointerdown", (event) => beginCeoBrainHold(event));
    brain?.addEventListener("pointerup", () => finishCeoBrainHold());
    brain?.addEventListener("pointerleave", () => finishCeoBrainHold());
    brain?.addEventListener("pointercancel", () => finishCeoBrainHold());
    brain?.addEventListener("keydown", (event) => {
        if (event.key !== "Enter" && event.key !== " ") return;
        event.preventDefault();
        event.stopPropagation();
        if (S.ceoModelSelection.panelOpen) closeCeoModelModePanel();
        else openCeoModelModePanel();
    });
    U.ceoModelModeChain?.addEventListener("click", () => {
        showCeoModelChainPane();
    });
    U.ceoModelModePinned?.addEventListener("click", () => {
        openCeoModelModePicker();
    });
    U.ceoModelChainConfirmAccept?.addEventListener("click", () => {
        void confirmCeoModelChainSwitch();
    });
    U.ceoModelChainConfirmCancel?.addEventListener("click", () => {
        cancelCeoModelChainSwitch();
    });
    U.ceoModelPickerSearch?.addEventListener("input", () => {
        S.ceoModelSelection = { ...S.ceoModelSelection, search: String(U.ceoModelPickerSearch?.value || "") };
        renderCeoModelPicker();
    });
    U.ceoModelPickerList?.addEventListener("click", (event) => {
        const target = event.target instanceof Element ? event.target.closest("[data-ceo-model-pick]") : null;
        if (!target || target.disabled) return;
        void saveCeoModelSelection("model", target.getAttribute("data-ceo-model-pick"));
    });
    U.ceoModelChainList?.addEventListener("dragstart", (event) => beginCeoModelChainDrag(event));
    U.ceoModelChainList?.addEventListener("dragover", (event) => updateCeoModelChainDropTarget(event));
    U.ceoModelChainList?.addEventListener("drop", (event) => finishCeoModelChainDrag(event));
    U.ceoModelChainList?.addEventListener("dragend", () => {
        clearCeoModelChainDragDecorations();
        S.ceoModelSelection = { ...S.ceoModelSelection, dragFrom: -1, dropIndex: null };
        renderCeoModelChainPane();
    });
    U.ceoModelChainApply?.addEventListener("click", () => {
        if (S.ceoModelSelection.saving || !ceoModelChainDirty()) return;
        void saveCeoModelChain(ceoModelChainDraft());
    });
}


function setCeoComposerUsagePinnedEntries(sessionId, entries = []) {
    const key = String(sessionId || "").trim();
    const normalizedEntries = (Array.isArray(entries) ? entries : [])
        .map((entry) => ({
            text: String(entry?.text || ""),
            uploads: normalizeUploadList(entry?.uploads),
        }))
        .filter((entry) => entry.text.trim() || entry.uploads.length > 0);
    if (!key || !normalizedEntries.length) {
        S.ceoComposerUsagePinnedEntries = null;
        return null;
    }
    S.ceoComposerUsagePinnedEntries = {
        session_id: key,
        entries: normalizedEntries,
    };
    return S.ceoComposerUsagePinnedEntries;
}

function clearCeoComposerUsagePinnedEntries(sessionId = activeSessionId()) {
    const key = String(sessionId || "").trim();
    if (!S.ceoComposerUsagePinnedEntries) return;
    if (key && String(S.ceoComposerUsagePinnedEntries.session_id || "").trim() !== key) return;
    S.ceoComposerUsagePinnedEntries = null;
}

function buildCeoComposerPreflightEntries(sessionId = activeSessionId()) {
    const key = String(sessionId || "").trim();
    if (!key) return [];
    const queued = getCeoQueuedFollowUps(key).map((item) => ({
        text: String(item?.text || ""),
        uploads: normalizeUploadList(item?.uploads),
    }));
    const draftText = String(U.ceoInput?.value || "");
    const draftUploads = normalizeUploadList(S.ceoUploads);
    if (draftText.trim() || draftUploads.length) {
        queued.push({ text: draftText, uploads: draftUploads });
    }
    return queued.filter((item) => String(item.text || "").trim() || normalizeUploadList(item.uploads).length > 0);
}

function buildCeoComposerOutlinePath(x, y, width, height, radius) {
    const safeX = Math.max(0, Number(x) || 0);
    const safeY = Math.max(0, Number(y) || 0);
    const safeWidth = Math.max(1, Number(width) || 0);
    const safeHeight = Math.max(1, Number(height) || 0);
    const safeRadius = Math.max(0, Math.min(Number(radius) || 0, safeWidth / 2, safeHeight / 2));
    return [
        `M ${safeX + safeRadius} ${safeY + 1}`,
        `H ${safeX + safeWidth - safeRadius}`,
        `A ${safeRadius} ${safeRadius} 0 0 1 ${safeX + safeWidth - 1} ${safeY + safeRadius}`,
        `V ${safeY + safeHeight - safeRadius}`,
        `A ${safeRadius} ${safeRadius} 0 0 1 ${safeX + safeWidth - safeRadius} ${safeY + safeHeight - 1}`,
        `H ${safeX + safeRadius}`,
        `A ${safeRadius} ${safeRadius} 0 0 1 ${safeX + 1} ${safeY + safeHeight - safeRadius}`,
        `V ${safeY + safeRadius}`,
        `A ${safeRadius} ${safeRadius} 0 0 1 ${safeX + safeRadius} ${safeY + 1}`,
    ].join(" ");
}

function scheduleSyncCeoComposerUsageOutline() {
    window.requestAnimationFrame(() => syncCeoComposerUsageOutline());
}

function syncCeoComposerUsageOutline() {
    const shell = U.ceoComposerUsageBrain;
    const base = U.ceoComposerUsageBrainBase;
    const fill = U.ceoComposerUsageBrainFill;
    if (!shell || !base || !fill) return;
    const estimate = ceoCurrentUsageEstimate();
    const hasEstimate = !!estimate;
    const ratio = hasEstimate ? Math.max(0, Math.min(1, Number(estimate.ratio) || 0)) : 0;
    const visualRatio = hasEstimate && ratio > 0 ? Math.max(ratio, 0.06) : 0;
    const hue = Math.max(0, Math.min(145, 145 - (visualRatio * 145)));
    // 占用色只描述「这一回合正在吃掉多少上下文」：回合没在跑（含手动/自动压缩之外的所有
    // 空闲态）统一用中性灰，避免一个静止的绿长期挂着被读成正在消耗。
    // 填充高度与 aria/面板数值不受影响，仍然按占用率显示。
    const usageLive = !!S.ceoTurnActive || !!activeCeoSessionCompressionState();
    const usageColor = usageLive ? `hsl(${hue.toFixed(1)} 82% 58%)` : "var(--text-muted)";
    const fillPercent = Math.max(0, Math.min(100, visualRatio * 100));
    const basePercent = Math.max(0, Math.min(100, 100 - fillPercent));
    if (typeof shell.style?.setProperty === "function") {
        shell.style.setProperty("--ceo-context-usage-color", usageColor);
    } else {
        shell.style["--ceo-context-usage-color"] = usageColor;
    }
    base.style.height = `${basePercent}%`;
    fill.style.height = `${fillPercent}%`;
    shell.classList.toggle("is-active", hasEstimate);
    shell.classList.toggle("is-pending", false);
    shell.dataset.usageState = (
        !hasEstimate ? "idle"
            : estimate.would_exceed_context_window ? "overflow"
                : estimate.would_trigger_token_compression ? "warning"
                    : "active"
    );
    const tipLabel = hasEstimate
        ? `${estimate.provider_model || "current-model"} · ${estimate.estimated_total_tokens}/${estimate.context_window_tokens} TOKEN`
        : "等待 Leader 上下文预估";
    // 数字只在面板头部展示；脑图标保留等价的无障碍名称，不用原生 title 浮层。
    shell.removeAttribute?.("title");
    if (shell.setAttribute) shell.setAttribute("aria-label", tipLabel);
    // 面板头部展示同一份预估，两处数字必须同源。
    if (S.ceoModelSelection.panelOpen) syncCeoModelModePanelUsage();
}

function activeCeoSessionHasHistory(sessionId = activeSessionId()) {
    const key = String(sessionId || "").trim();
    if (!key) return false;
    const item = (S.ceoSessions || []).find((entry) => String(entry?.session_id || "").trim() === key) || null;
    if (item) return sessionMessageCount(item) > 0;
    // 目录里查不到时（例如刚从缓存渲染）退回已缓存的转录条数。
    const cached = getCeoSessionSnapshotCache(key);
    return !!(cached?.messages || []).length;
}

async function refreshCeoComposerUsageEstimate() {
    const sessionId = String(activeSessionId() || "").trim();
    // 只读门槛不适用在这里：composer-preflight 不改写会话，渠道会话同样要常驻显示自己的
    // 上下文占用值（脑图标空着 = 需求里「非常驻显示 token 量」没做到）。
    if (!sessionId || S.ceoUploadBusy) {
        clearCeoComposerUsageEstimate();
        return null;
    }
    const runtimeEstimate = activeCeoRuntimeUsageEstimate(sessionId);
    if (runtimeEstimate) {
        // 不清 composer 读数：runtime 有值时它天然压过 composer，而这份旧值正是
        // 下一回合起跑、新 usage 还没到货时唯一能顶上的读数。
        return runtimeEstimate;
    }
    if (S.ceoTurnActive) {
        // 回合进行中不再发预检（读数归 runtime 通道），但也不能清零：保留上一份读数显示，
        // 直到 runtime usage 到货刷新（回归零会让 token 数在每次调用模型时闪成「等待预估」）。
        return ceoCurrentUsageEstimate();
    }
    const entries = buildCeoComposerPreflightEntries(sessionId);
    if (!entries.length && !activeCeoSessionHasHistory(sessionId)) {
        // 只有新会话（没有任何历史）才没有可显示的占用值。
        clearCeoComposerUsageEstimate();
        return null;
    }
    const requestSeq = ++S.ceoComposerUsageRequestSeq;
    try {
        const item = await ApiClient.estimateCeoComposerPreflight(sessionId, {
            messages: entries.map((entry) => ({
                text: String(entry.text || ""),
                uploads: normalizeUploadList(entry.uploads).map((upload) => ({
                    name: upload.name,
                    path: upload.path,
                    mime_type: upload.mime_type,
                    kind: upload.kind,
                    size: upload.size,
                })),
            })),
        });
        if (requestSeq !== S.ceoComposerUsageRequestSeq || sessionId !== String(activeSessionId() || "").trim()) {
            return null;
        }
        return setCeoComposerUsageEstimate(sessionId, item || {});
    } catch (error) {
        const name = String(error?.name || "").trim();
        const message = String(error?.message || "").trim();
        if (name === "AbortError" || message === "Stale request" || message === "Request aborted") return null;
        if (requestSeq === S.ceoComposerUsageRequestSeq) clearCeoComposerUsageEstimate();
        return null;
    }
}

async function runCeoComposerUsageRefresh() {
    if (S.ceoComposerUsageBusy) {
        S.ceoComposerUsageNeedsRefresh = true;
        return null;
    }
    S.ceoComposerUsageBusy = true;
    try {
        return await refreshCeoComposerUsageEstimate();
    } finally {
        S.ceoComposerUsageBusy = false;
        if (S.ceoComposerUsageNeedsRefresh) {
            S.ceoComposerUsageNeedsRefresh = false;
            scheduleCeoComposerUsageRefresh({ immediate: true });
        }
    }
}

function scheduleCeoComposerUsageRefresh({ immediate = false } = {}) {
    if (S.ceoComposerUsageRefreshId) window.clearTimeout(S.ceoComposerUsageRefreshId);
    S.ceoComposerUsageRefreshId = window.setTimeout(() => {
        S.ceoComposerUsageRefreshId = null;
        void runCeoComposerUsageRefresh();
    }, immediate ? 0 : 260);
}

function normalizeCeoQueuedFollowUpEntry(entry = {}) {
    const text = String(entry?.text || "");
    const uploads = cloneCeoSnapshotAttachments(entry?.uploads);
    if (!text.trim() && !uploads.length) return null;
    const normalized = {
        id: String(entry?.id || `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`).trim(),
        text,
        uploads,
        queued_at: String(entry?.queued_at || "").trim() || new Date().toISOString(),
    };
    const runtimeSentAt = String(entry?.runtime_sent_at || entry?.runtimeSentAt || "").trim();
    if (runtimeSentAt) normalized.runtime_sent_at = runtimeSentAt;
    return normalized;
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

function ceoServerFollowUpText(content) {
    if (typeof content === "string") return content.trim();
    if (Array.isArray(content)) {
        return content.map((part) => String(part?.text || "")).filter(Boolean).join(" ").trim();
    }
    return "";
}

function normalizeCeoServerQueuedFollowUpList(items = []) {
    return (Array.isArray(items) ? items : [])
        .map((item, index) => {
            const uploads = cloneCeoSnapshotAttachments(item?.attachments);
            const text = ceoServerFollowUpText(item?.content);
            if (!text && !uploads.length) return null;
            const turnId = String(item?.metadata?.["_transcript_turn_id"] || "").trim();
            return {
                id: `server:${turnId || index}`,
                text,
                uploads,
                queued_at: "",
                runtime_sent_at: "server",
                accepted_by_runtime: true,
            };
        })
        .filter(Boolean);
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

function getCeoServerQueuedFollowUps(sessionId = activeSessionId()) {
    const key = String(sessionId || "").trim();
    if (!key) return [];
    return normalizeCeoServerQueuedFollowUpList(S.ceoServerQueuedFollowUps?.[key] || []);
}

function getMergedCeoQueuedFollowUps(sessionId = activeSessionId()) {
    const serverItems = getCeoServerQueuedFollowUps(sessionId);
    const represented = new Set(serverItems.map((item) => String(item.text || "").trim()).filter(Boolean));
    // 浏览器这一侧只保留"还没发给 runtime"的条目：带 runtime_sent_at 的那一半服务端已经
    // 知道了，两份都画就会在候选条里重复出现。
    const localItems = getCeoQueuedFollowUps(sessionId).filter((item) => {
        if (!String(item?.runtime_sent_at || "").trim()) return true;
        return !represented.has(String(item.text || "").trim());
    });
    return [...serverItems, ...localItems];
}

function adoptCeoServerQueuedFollowUpsFromState(state = {}, sessionId = activeSessionId()) {
    const key = String(sessionId || "").trim();
    if (!key) return false;
    // 只存原样数组，读的时候再规范化：存规范化结果会让第二次读把已规范化的条目再过滤一遍
    // （它们没有 content 字段），候选条于是自己把自己清空。
    const next = Array.isArray(state?.queued_follow_up_messages) ? state.queued_follow_up_messages : [];
    const current = Array.isArray(S.ceoServerQueuedFollowUps?.[key]) ? S.ceoServerQueuedFollowUps[key] : [];
    if (JSON.stringify(next) === JSON.stringify(current)) return false;
    S.ceoServerQueuedFollowUps = { ...(S.ceoServerQueuedFollowUps || {}), [key]: next };
    return true;
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

function markCeoQueuedFollowUpsRuntimeSent(sessionId, entryIds = []) {
    const key = String(sessionId || "").trim();
    if (!key) return [];
    const targetIds = new Set(
        (Array.isArray(entryIds) ? entryIds : [])
            .map((item) => String(item || "").trim())
            .filter(Boolean)
    );
    if (!targetIds.size) return getCeoQueuedFollowUps(key);
    const sentAt = new Date().toISOString();
    const current = getCeoQueuedFollowUps(key).map((item) => {
        const entryId = String(item?.id || "").trim();
        if (!entryId || !targetIds.has(entryId)) return item;
        return {
            ...item,
            runtime_sent_at: sentAt,
        };
    });
    return setCeoQueuedFollowUps(key, current);
}

function pruneRuntimeSentCeoFollowUps(sessionId = activeSessionId()) {
    const key = String(sessionId || "").trim();
    if (!key) return [];
    return getCeoQueuedFollowUps(key);
}

function ceoSnapshotMessageMatchesQueuedFollowUp(message = null, followUp = null) {
    const normalizedMessage = normalizeCeoSnapshotMessage(message);
    if (!normalizedMessage || normalizedMessage.role !== "user") return false;
    const normalizedFollowUp = normalizeCeoQueuedFollowUpEntry(followUp);
    if (!normalizedFollowUp) return false;
    const sameAttachments = JSON.stringify(normalizeUploadList(normalizedMessage.attachments)) === JSON.stringify(normalizedFollowUp.uploads);
    return String(normalizedMessage.content || "") === String(normalizedFollowUp.text || "") && sameAttachments;
}

function consumeRepresentedRuntimeSentCeoFollowUps(sessionId = activeSessionId(), representedMessages = []) {
    const key = String(sessionId || "").trim();
    if (!key) return [];
    const normalizedMessages = normalizeCeoSnapshotUserMessages(representedMessages);
    if (!normalizedMessages.length) return getCeoQueuedFollowUps(key);
    const retained = getCeoQueuedFollowUps(key).filter((item) => {
        if (!String(item?.runtime_sent_at || "").trim()) return true;
        return !normalizedMessages.some((message) => ceoSnapshotMessageMatchesQueuedFollowUp(message, item));
    });
    return setCeoQueuedFollowUps(key, retained);
}

function sendActiveCeoFollowUpsToRuntime(sessionId = activeSessionId()) {
    const key = String(sessionId || "").trim();
    if (!key) return null;
    const queued = getCeoQueuedFollowUps(key).filter((item) => !String(item?.runtime_sent_at || "").trim());
    if (!queued.length) return null;
    const wsOpenState = Number(WebSocket?.OPEN ?? 1);
    if (!S.ceoWs || S.ceoWs.readyState !== wsOpenState) {
        addMsg("Connection is not ready yet. Please try again in a moment.", "system");
        initCeoWs();
        return null;
    }
    S.ceoWs.send(JSON.stringify({
        type: "client.user_message",
        session_id: key,
        messages: queued.map((item) => ({
            text: String(item?.text || ""),
            uploads: normalizeUploadList(item?.uploads).map((upload) => ({
                name: upload.name,
                path: upload.path,
                mime_type: upload.mime_type,
                kind: upload.kind,
                size: upload.size,
            })),
        })),
    }));
    const queuedIds = queued.map((item) => String(item?.id || "").trim());
    markCeoQueuedFollowUpsRuntimeSent(key, queuedIds);
    return getCeoQueuedFollowUps(key).filter((item) => queuedIds.includes(String(item?.id || "").trim()));
}

function shiftCeoQueuedFollowUp(sessionId) {
    const key = String(sessionId || "").trim();
    const current = getCeoQueuedFollowUps(key);
    const [first, ...rest] = current;
    setCeoQueuedFollowUps(key, rest);
    return first || null;
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

function normalizeCeoSnapshotCanonicalContext(context = null) {
    if (!context || typeof context !== "object") return null;
    const rawStages = Array.isArray(context?.stages) ? context.stages : [];
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
    const next = { stages };
    const activeStageId = String(context?.active_stage_id || "").trim();
    if (activeStageId) next.active_stage_id = activeStageId;
    if (context?.transition_required === true) next.transition_required = true;
    return next;
}

function resolvePreferredCeoCanonicalContext(context = null, previousContext = null) {
    return normalizeCeoSnapshotCanonicalContext(context)
        || normalizeCeoSnapshotCanonicalContext(previousContext)
        || null;
}

function resolvePreferredCeoTraceContext(deltaContext = null, fullContext = null, previousContext = null) {
    return normalizeCeoSnapshotCanonicalContext(deltaContext)
        || normalizeCeoSnapshotCanonicalContext(fullContext)
        || normalizeCeoSnapshotCanonicalContext(previousContext)
        || null;
}

function mergeCeoStageSummaryDelta(baseSummary = null, deltaContext = null) {
    const mergedStages = [];
    const stageIndexById = new Map();
    const accumulateStage = (stage) => {
        const stageId = String(stage?.stage_id ?? stage?.stage_index ?? "");
        if (stageId) {
            const existingIndex = stageIndexById.get(stageId);
            if (existingIndex !== undefined) {
                const existing = mergedStages[existingIndex];
                const baseRounds = Array.isArray(existing?.rounds) ? [...existing.rounds] : [];
                const roundIndexById = new Map(
                    baseRounds.map((round, roundIndex) => [String(round?.round_id ?? round?.round_index ?? roundIndex), roundIndex])
                );
                (Array.isArray(stage?.rounds) ? stage.rounds : []).forEach((round, roundIndex) => {
                    const roundId = String(round?.round_id ?? round?.round_index ?? roundIndex);
                    const existingRoundIndex = roundIndexById.get(roundId);
                    if (existingRoundIndex === undefined) {
                        roundIndexById.set(roundId, baseRounds.length);
                        baseRounds.push(round);
                    } else {
                        baseRounds[existingRoundIndex] = round;
                    }
                });
                // delta 的轮次只包含"新增/变更";轮次保持基线位置,追加新轮,
                // 阶段头以 delta 为准(后端 delta 轮次为空时代表头变更,保留原轮次)
                mergedStages[existingIndex] = { ...stage, rounds: baseRounds };
                return;
            }
            stageIndexById.set(stageId, mergedStages.length);
        }
        mergedStages.push(stage);
    };
    const normalize = (value) => {
        const summary = normalizeCeoSnapshotCanonicalContext(value);
        return Array.isArray(summary?.stages) ? summary.stages : [];
    };
    normalize(baseSummary).forEach(accumulateStage);
    normalize(deltaContext).forEach(accumulateStage);
    if (!mergedStages.length) return null;
    return { stages: mergedStages };
}

function mergeCeoLiveTraceContext(deltaContext = null, previousSummary = null) {
    if (filterCeoInteractionFlowSummary(deltaContext)?.stages?.length) {
        return mergeCeoStageSummaryDelta(previousSummary, deltaContext);
    }
    // 无新增量:保留本轮已渲染的轨道;从未渲染过则返回空,由渲染层维持占位状态
    return normalizeCeoSnapshotCanonicalContext(previousSummary) || null;
}

function resolveFinalCeoTraceContext(meta = {}) {
    if (meta?.canonical_context_delta) {
        return normalizeCeoSnapshotCanonicalContext(meta.canonical_context_delta) || null;
    }
    if (meta?.canonical_context) {
        return normalizeCeoSnapshotCanonicalContext(meta.canonical_context) || null;
    }
    return null;
}

function normalizeCeoSnapshotToolEvents() {
    return [];
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

function normalizeCeoModelRetryStatus(value = null) {
    if (!value || typeof value !== "object") return null;
    const state = String(value?.state || "").trim().toLowerCase();
    if (state !== "retrying") return null;
    const rawCount = Number.parseInt(String(value?.retry_count ?? ""), 10);
    const retryCount = Number.isFinite(rawCount) && rawCount >= 0 ? rawCount : 0;
    const rawRound = Number.parseInt(String(value?.chain_round ?? ""), 10);
    const chainRound = Number.isFinite(rawRound) && rawRound > 0 ? rawRound : 0;
    const rawDelay = Number(value?.delay_seconds);
    const delaySeconds = Number.isFinite(rawDelay) && rawDelay >= 0 ? rawDelay : 0;
    const next = {
        state: "retrying",
        retry_count: retryCount,
        delay_seconds: delaySeconds,
    };
    if (chainRound) next.chain_round = chainRound;
    const errorMessage = String(value?.error_message || "").trim();
    if (errorMessage) next.error_message = errorMessage;
    const modelRefs = (Array.isArray(value?.model_refs) ? value.model_refs : [])
        .map((item) => String(item || "").trim())
        .filter(Boolean);
    if (modelRefs.length) next.model_refs = modelRefs;
    const lastRetryAt = String(value?.last_retry_at || "").trim();
    if (lastRetryAt) next.last_retry_at = lastRetryAt;
    const nextRetryAt = String(value?.next_retry_at || "").trim();
    if (nextRetryAt) next.next_retry_at = nextRetryAt;
    return next;
}

function normalizeCeoSnapshotCompressionMarker(marker = null) {
    if (!marker || typeof marker !== "object") return null;
    const state = String(marker?.state || "").trim().toLowerCase();
    if (state !== "completed" && state !== "paused") return null;
    const next = { state };
    const source = String(marker?.source || "").trim().toLowerCase();
    if (source) next.source = source;
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
    const compressionMarker = normalizeCeoSnapshotCompressionMarker(message?.compression_marker);
    if (compressionMarker) next.compression_marker = compressionMarker;
    const turnId = String(message?.turn_id || "").trim();
    if (turnId) next.turn_id = turnId;
    const timestamp = String(message?.timestamp || "").trim();
    if (timestamp) next.timestamp = timestamp;
    const attachments = role === "user" ? cloneCeoSnapshotAttachments(message?.attachments) : [];
    if (attachments.length) next.attachments = attachments;
    if (role === "assistant") {
        const status = String(message?.status || "").trim().toLowerCase();
        const canonicalContext = normalizeCeoSnapshotCanonicalContext(message?.canonical_context);
        // {} delta 是后端"本轮无新轨道"的显式信号：必须原样保留键存在性，
        // 否则缓存重渲会按"无 delta"回退全量 cc，把上一轮的旧轨道复活。
        const hasDeltaKey = message?.canonical_context_delta
            && typeof message.canonical_context_delta === "object";
        const canonicalContextDelta = hasDeltaKey
            ? normalizeCeoSnapshotCanonicalContext(message.canonical_context_delta)
            : null;
        const usage = normalizeCeoTurnUsage(message?.usage);
        if (status) next.status = status;
        if (canonicalContext) next.canonical_context = canonicalContext;
        if (hasDeltaKey) next.canonical_context_delta = canonicalContextDelta || {};
        if (usage) next.usage = usage;
        if (message?.task_dispatched === true) next.task_dispatched = true;
        if (message?.silent_reply === true) next.silent_reply = true;
        if (!String(next.content || "").trim() && !canonicalContext && !hasDeltaKey && status !== "paused") return null;
        return next;
    }
    if (role === "user" && message?.can_edit_fork === true) next.can_edit_fork = true;
    if (role === "user" && !String(next.content || "").trim() && !attachments.length) return null;
    if (role === "system" && !String(next.content || "").trim()) return null;
    return next;
}

function normalizeCeoSnapshotUserMessages(messages = [], fallbackMessage = null) {
    const normalized = (Array.isArray(messages) ? messages : [])
        .map((item) => normalizeCeoSnapshotMessage(item))
        .filter((item) => String(item?.role || "").trim().toLowerCase() === "user");
    if (normalized.length) return normalized;
    if (!fallbackMessage || typeof fallbackMessage !== "object") return [];
    const fallback = normalizeCeoSnapshotMessage({
        role: "user",
        ...fallbackMessage,
    });
    return fallback ? [fallback] : [];
}

function normalizeCeoSnapshotInterrupts(interrupts = []) {
    return (Array.isArray(interrupts) ? interrupts : [])
        .map((item) => {
            if (!item || typeof item !== "object") return null;
            const id = String(item.id || "").trim();
            const value = item.value && typeof item.value === "object"
                ? structuredClone(item.value)
                : null;
            if (!id || !value) return null;
            return { id, value };
        })
        .filter(Boolean);
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
    const userMessages = normalizeCeoSnapshotUserMessages(snapshot?.user_messages, userMessage);
    if (userMessages.length) {
        next.user_messages = userMessages;
        const lastUserMessage = userMessages[userMessages.length - 1];
        if (lastUserMessage) {
            next.user_message = { content: String(lastUserMessage.content || "") };
            const attachments = cloneCeoSnapshotAttachments(lastUserMessage.attachments);
            if (attachments.length) next.user_message.attachments = attachments;
            // 发送时间随 inflight 缓存存活,收尾/会话切换后用户气泡悬停仍可显示。
            const userTimestamp = String(lastUserMessage.timestamp || "").trim();
            if (userTimestamp) next.user_message.timestamp = userTimestamp;
        }
    }
    const canonicalContext = normalizeCeoSnapshotCanonicalContext(snapshot?.canonical_context);
    const canonicalContextDelta = normalizeCeoSnapshotCanonicalContext(snapshot?.canonical_context_delta);
    const compression = normalizeCeoSnapshotCompression(snapshot?.compression);
    const modelRetryStatus = normalizeCeoModelRetryStatus(snapshot?.model_retry_status);
    const errorMessage = String(snapshot?.last_error?.message || "").trim();
    const runtimeUsageDiagnostics = normalizeCeoRuntimeUsageDiagnostics(snapshot?.frontdoor_token_preflight_diagnostics);
    const actualRequestMessageCount = Number(snapshot?.actual_request_message_count ?? snapshot?.actualRequestMessageCount);
    if (canonicalContext) next.canonical_context = canonicalContext;
    if (canonicalContextDelta) next.canonical_context_delta = canonicalContextDelta;
    if (compression) next.compression = compression;
    if (modelRetryStatus) next.model_retry_status = modelRetryStatus;
    if (errorMessage) next.last_error = { message: errorMessage };
    if (runtimeUsageDiagnostics) next.frontdoor_token_preflight_diagnostics = runtimeUsageDiagnostics;
    const usage = normalizeCeoTurnUsage(snapshot?.usage);
    if (usage) next.usage = usage;
    if (Number.isFinite(actualRequestMessageCount) && actualRequestMessageCount > 0) {
        next.actual_request_message_count = Math.floor(actualRequestMessageCount);
    }
    const interrupts = normalizeCeoSnapshotInterrupts(snapshot?.interrupts);
    if (interrupts.length) next.interrupts = interrupts;
    if (
        !ceoInflightTurnHasVisibleAssistantState(next)
        && !next.user_message
        && !next.compression
        && !next.model_retry_status
        && !next.frontdoor_token_preflight_diagnostics
    ) return null;
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
    const preservedTurn = normalizeCeoSnapshotInflight(entry?.preserved_turn);
    if (!messages.length && !inflightTurn && !preservedTurn) return null;
    const next = {
        session_id: key,
        messages,
        cached_at: String(entry?.cached_at || "").trim() || new Date().toISOString(),
    };
    if (inflightTurn) next.inflight_turn = inflightTurn;
    if (preservedTurn) next.preserved_turn = preservedTurn;
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

function ceoAssistantTurnAlreadyPersisted(turnId = "", { messages = null, sessionId = "" } = {}) {
    const normalizedTurnId = normalizeCeoTurnId(turnId);
    if (!normalizedTurnId) return false;
    const sourceMessages = Array.isArray(messages)
        ? messages
        : (getCeoSessionSnapshotCache(sessionId || activeSessionId())?.messages || []);
    return trimCeoSessionSnapshotMessages(sourceMessages).some((item) => (
        String(item?.role || "").trim().toLowerCase() === "assistant"
        && normalizeCeoTurnId(item?.turn_id || "") === normalizedTurnId
    ));
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

function hasActiveCeoComposerUsageEstimate(sessionId = activeSessionId()) {
    const key = String(sessionId || "").trim();
    if (!key) return false;
    return !!(
        S.ceoComposerUsageEstimate
        && String(S.ceoComposerUsageEstimate.session_id || "").trim() === key
    );
}

function ceoRunningInflightTurnForSession(sessionId = activeSessionId()) {
    const key = String(sessionId || "").trim();
    if (!key) return null;
    const cacheEntry = getCeoSessionSnapshotCache(key);
    const inflightTurn = normalizeCeoSnapshotInflight(cacheEntry?.inflight_turn);
    const status = String(inflightTurn?.status || "").trim().toLowerCase();
    if (status && !["running", "in_progress", "active"].includes(status)) return null;
    return inflightTurn || null;
}

function activeCeoRuntimeUsageEstimate(sessionId = activeSessionId()) {
    const key = String(sessionId || "").trim();
    if (!key) return null;
    return normalizeCeoRuntimeUsageEstimate(key, ceoRunningInflightTurnForSession(key));
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
    syncCeoCompressionDivider();
    syncCeoModelRetryToast();
    syncCeoComposerUsageOutline();
    if (!ceoRunningInflightTurnForSession(key) && !hasActiveCeoComposerUsageEstimate(key)) {
        scheduleCeoComposerUsageRefresh();
    }
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
    syncCeoCompressionDivider();
    syncCeoModelRetryToast();
    syncCeoComposerUsageOutline();
    if (!hasActiveCeoComposerUsageEstimate(key)) scheduleCeoComposerUsageRefresh();
    return true;
}

function activeCeoManualCompressionRunning() {
    return String(S.ceoContextCompressionStatus || "").trim().toLowerCase() === "running"
        && String(S.ceoContextCompressionSessionId || "") === String(activeSessionId() || "").trim();
}

function activeCeoSessionCompressionState() {
    // 回合外的手动压缩没有 inflight turn，进度只存在于本机发起状态里；自动压缩仍读快照。
    if (activeCeoManualCompressionRunning()) return { status: "running", source: "manual_context_compression" };
    const cacheEntry = getCeoSessionSnapshotCache(activeSessionId());
    const inflightTurn = normalizeCeoSnapshotInflight(cacheEntry?.inflight_turn);
    const inflightStatus = String(inflightTurn?.status || "").trim().toLowerCase();
    if (inflightStatus && !["running", "in_progress", "active"].includes(inflightStatus)) return null;
    const compression = normalizeCeoSnapshotCompression(inflightTurn?.compression);
    if (!compression) return null;
    return String(compression.status || "").trim().toLowerCase() === "running" ? compression : null;
}

// 图标自绘而不是取 lucide 字形：loader-circle 是中心对称圆环，转起来看不出动静；
// 这里的旋转环负责「还在跑」，中间两条竖杠负责「点这里可以暂停」。
function buildCeoCompressionPauseGlyph(withSpinnerRing) {
    const className = withSpinnerRing
        ? "ceo-compression-divider-spinner"
        : "ceo-compression-divider-spinner is-bars-only";
    return `<span class="${className}" aria-hidden="true"><i></i><i></i></span>`;
}

function buildCeoCompressionDividerControl(state, interactive) {
    if (state === "running") {
        const glyph = buildCeoCompressionPauseGlyph(true);
        if (interactive) {
            return `<button type="button" class="ceo-compression-divider-action" data-ceo-compress-pause
                   aria-label="暂停压缩" title="暂停压缩">${glyph}</button>`;
        }
        return `<span class="ceo-compression-divider-icon">${glyph}</span>`;
    }
    if (state === "paused") {
        return `<span class="ceo-compression-divider-icon">${buildCeoCompressionPauseGlyph(false)}</span>`;
    }
    // 完成态按需求只留文案，不再挂图标。
    return "";
}

function appendCeoCompressionDivider(state, options = {}) {
    const { interactive = true } = options;
    if (!U.ceoFeed || !CEO_COMPRESSION_TEXT[state]) return null;
    const el = document.createElement("div");
    el.className = `message system ${CEO_COMPRESSION_DIVIDER_CLASS} is-${state}`;
    el.dataset.ceoCompressionState = state;
    el.setAttribute("role", "status");
    el.setAttribute("aria-live", state === "running" ? "polite" : "off");
    el.innerHTML = `<div class="ceo-compression-divider-inner"><span>${
        CEO_COMPRESSION_TEXT[state]
    }</span>${buildCeoCompressionDividerControl(state, interactive)}</div>`;
    mutateCeoFeed(() => {
        ceoFeedAppendHost().appendChild(el);
        icons();
    }, { scrollMode: "preserve" });
    return el;
}

function removeCeoCompressionLiveDivider() {
    if (typeof U.ceoFeed?.querySelector !== "function") return false;
    const el = U.ceoFeed.querySelector(`.${CEO_COMPRESSION_DIVIDER_CLASS}.is-running`);
    if (!el) return false;
    el.remove();
    return true;
}

function syncCeoCompressionDivider() {
    if (!U.ceoFeed || typeof U.ceoFeed.querySelector !== "function") return;
    ensureCeoContextCompressionPolling();
    const running = !!activeCeoSessionCompressionState();
    const existing = U.ceoFeed.querySelector(`.${CEO_COMPRESSION_DIVIDER_CLASS}.is-running`);
    if (running && !existing) {
        appendCeoCompressionDivider("running");
        // 只有本来就在跟随最新时才钉底：这条线现在也由 renderCeoSnapshot 在每次整页重建后
        // 重挂，若无条件钉底会把正在往上翻历史的用户每次重建都拽回底部。
        if (S.ceoFeedFollowLatest !== false) scrollCeoFeedToBottom();
        return;
    }
    if (!running && existing) removeCeoCompressionLiveDivider();
}

function stopCeoContextCompressionPolling() {
    if (S.ceoContextCompressionPollId !== null) {
        window.clearInterval(S.ceoContextCompressionPollId);
        S.ceoContextCompressionPollId = null;
    }
}

function ceoContextCompressionTerminalText(status, reason) {
    const normalizedReason = String(reason || "").trim().toLowerCase();
    if (normalizedReason === "baseline_advanced") {
        // 摘要算完了，但基线在这期间被另一个回合改写过：不落盘也不落区分线，
        // 说成「没有可压缩的历史」会把人引向错误的下一步。
        return "压缩期间这条会话的上下文基线被另一个回合改写了。为避免丢掉那条回合的内容，本次摘要没有落盘；请等这条回复结束后再压缩一次。";
    }
    if (status === "not_needed") {
        return "没有可压缩的历史，或所选模型没有可用的上下文窗口（需大于 25000 token）。";
    }
    return `压缩任务未能开始或中途中止：${String(reason || "unknown")}`;
}

function applyCeoContextCompressionStatus(payload = {}) {
    const status = String(payload?.status || "idle").trim().toLowerCase();
    const sessionId = String(S.ceoContextCompressionSessionId || activeSessionId() || "").trim();
    const previous = String(S.ceoContextCompressionStatus || "").trim().toLowerCase();
    S.ceoContextCompressionStatus = status;
    S.ceoContextCompressionCancelRequested = payload?.cancel_requested === true;
    if (status !== "running") {
        stopCeoContextCompressionPolling();
        S.ceoContextCompressionSessionId = "";
    }
    if (previous === "running" && status !== "running") {
        if (status === "not_needed" || status === "failed") {
            // 这两种终局都不会落区分线，不解释一句的话用户看到的就是「线莫名其妙没了」。
            showToast({
                title: "上下文未压缩",
                text: ceoContextCompressionTerminalText(status, payload?.reason),
                kind: "warn",
            });
        }
        // 区分线由转录里的持久标记行承载；终局后重开会话才能拿到它，这里强制刷新一次。
        S.ceoFeedRenderSignature = "";
        if (sessionId) void reloadCeoSessionSnapshot(sessionId);
    }
    syncCeoCompressionDivider();
    syncCeoComposerUsageOutline();
}

async function reloadCeoSessionSnapshot(sessionId) {
    const key = String(sessionId || "").trim();
    if (!key || key !== String(activeSessionId() || "").trim()) return;
    // 区分线只随 snapshot.ceo 的转录行下发；重开这条 WS 才能拿到刚落盘的标记行。
    clearCeoSessionSnapshotCache(key);
    closeCeoWs();
    initCeoWs();
}

function startCeoContextCompressionPolling(sessionId) {
    stopCeoContextCompressionPolling();
    const key = String(sessionId || "").trim();
    if (!key) return;
    S.ceoContextCompressionPollFails = 0;
    let inFlight = false;
    S.ceoContextCompressionPollId = window.setInterval(async () => {
        if (String(activeSessionId() || "").trim() !== key) {
            // 切走会话时停轮询；切回来由 ensureCeoContextCompressionPolling 续上。
            stopCeoContextCompressionPolling();
            return;
        }
        // 大会话收尾会把事件循环占住数秒，单次请求可能叠在上一条后面，别并发打出去。
        if (inFlight) return;
        inFlight = true;
        try {
            const payload = await ApiClient.getCeoContextCompression(key);
            S.ceoContextCompressionPollFails = 0;
            applyCeoContextCompressionStatus(payload || {});
        } catch (error) {
            // 一次超时不能当作终局：停掉轮询就等于永久收不到 completed，区分线会挂在
            // 「压缩中」直到刷新。攒到连续多次失败才放弃。
            S.ceoContextCompressionPollFails += 1;
            if (S.ceoContextCompressionPollFails >= CEO_COMPRESSION_POLL_FAIL_LIMIT) {
                stopCeoContextCompressionPolling();
            }
        } finally {
            inFlight = false;
        }
    }, CEO_COMPRESSION_POLL_MS);
}

function ensureCeoContextCompressionPolling() {
    const key = String(S.ceoContextCompressionSessionId || "").trim();
    if (!key || S.ceoContextCompressionPollId !== null) return;
    if (String(S.ceoContextCompressionStatus || "").trim().toLowerCase() !== "running") return;
    if (String(activeSessionId() || "").trim() !== key) return;
    startCeoContextCompressionPolling(key);
}

async function beginCeoContextCompression(sessionId) {
    const key = String(sessionId || "").trim();
    if (!key) return;
    if (String(S.ceoContextCompressionStatus || "").trim().toLowerCase() === "running") return;
    S.ceoContextCompressionSessionId = key;
    S.ceoContextCompressionStatus = "running";
    S.ceoContextCompressionCancelRequested = false;
    syncCeoCompressionDivider();
    try {
        const payload = await ApiClient.startCeoContextCompression(key);
        applyCeoContextCompressionStatus(payload || {});
        if (String(S.ceoContextCompressionStatus || "").trim().toLowerCase() === "running") {
            startCeoContextCompressionPolling(key);
        }
    } catch (error) {
        S.ceoContextCompressionStatus = "idle";
        S.ceoContextCompressionSessionId = "";
        syncCeoCompressionDivider();
        showToast({
            title: "压缩失败",
            text: String(error?.message || "上下文压缩未能开始。"),
            kind: "error",
        });
    }
}

function requestCeoContextCompressionPause() {
    const key = String(S.ceoContextCompressionSessionId || activeSessionId() || "").trim();
    if (!key) return;
    openConfirm({
        title: "暂停上下文压缩",
        text: "正在压缩上下文。暂停后本次压缩不会写回摘要，历史保持原样。",
        confirmLabel: "暂停压缩",
        confirmKind: "danger",
        onConfirm: async () => {
            S.ceoContextCompressionCancelRequested = true;
            syncCeoCompressionDivider();
            try {
                const payload = await ApiClient.cancelCeoContextCompression(key);
                applyCeoContextCompressionStatus(payload || {});
            } catch (error) {
                S.ceoContextCompressionCancelRequested = false;
                showToast({
                    title: "暂停失败",
                    text: String(error?.message || "当前没有可暂停的上下文压缩。"),
                    kind: "error",
                });
            }
        },
    });
}

function activeCeoSessionModelRetryStatus() {
    const cacheEntry = getCeoSessionSnapshotCache(activeSessionId());
    const inflightTurn = normalizeCeoSnapshotInflight(cacheEntry?.inflight_turn);
    const inflightStatus = String(inflightTurn?.status || "").trim().toLowerCase();
    if (inflightStatus && !["running", "in_progress", "active"].includes(inflightStatus)) return null;
    return normalizeCeoModelRetryStatus(inflightTurn?.model_retry_status);
}

function modelRetryToastText(status = null, label = "") {
    const normalized = normalizeCeoModelRetryStatus(status);
    if (!normalized) return "";
    label = String(label || "").trim();
    const count = Math.max(0, Number(normalized.retry_count || 0));
    const countText = count > 0 ? `第 ${count} 次重试` : "自动重试";
    const parts = [label, countText];
    // 时间戳后端已按本地带偏移下发，这里只取 HH:MM:SS 钟点显示。
    const lastClock = (String(normalized.last_retry_at || "").match(/T(\d{2}:\d{2}:\d{2})/) || [])[1] || "";
    if (lastClock) parts.push(`最新 ${lastClock}`);
    const nextClock = (String(normalized.next_retry_at || "").match(/T(\d{2}:\d{2}:\d{2})/) || [])[1] || "";
    if (nextClock) parts.push(`下次 ${nextClock}`);
    const errorText = String(normalized.error_message || "").trim();
    if (errorText) parts.push(errorText);
    return parts.filter(Boolean).join(" · ");
}

// 错误全文进 DOM，折叠态由 CSS line-clamp 截断；点击/回车展开靠 is-expanded 切换。
function refreshModelRetryToastClamp(toastEl, textEl) {
    if (!toastEl || !textEl) return;
    if (toastEl.hidden) {
        toastEl.classList.remove("is-expanded", "is-clamped");
        toastEl.removeAttribute("tabindex");
        toastEl.removeAttribute("title");
        return;
    }
    if (toastEl.classList.contains("is-expanded")) {
        toastEl.setAttribute("tabindex", "0");
        toastEl.title = "点击收起";
        return;
    }
    const clamped = (textEl.scrollHeight || 0) > (textEl.clientHeight || 0) + 1;
    toastEl.classList.toggle("is-clamped", clamped);
    if (clamped) {
        toastEl.setAttribute("tabindex", "0");
        toastEl.title = "点击展开完整错误信息";
    } else {
        toastEl.removeAttribute("tabindex");
        toastEl.removeAttribute("title");
    }
}

function toggleModelRetryToastExpanded(toastEl) {
    if (!toastEl || !toastEl.classList.contains("is-clamped")) return;
    const expanded = toastEl.classList.toggle("is-expanded");
    toastEl.title = expanded ? "点击收起" : "点击展开完整错误信息";
}

function syncCeoModelRetryToast() {
    const toastEl = U.ceoModelRetryToast;
    const textEl = U.ceoModelRetryToastText;
    if (!toastEl || !textEl) return;
    const status = activeCeoSessionModelRetryStatus();
    const visible = !!status;
    const text = modelRetryToastText(status);
    textEl.textContent = text;
    toastEl.hidden = !visible;
    if (toastEl.classList?.toggle) toastEl.classList.toggle("is-visible", visible);
    toastEl.setAttribute("aria-hidden", visible ? "false" : "true");
    refreshModelRetryToastClamp(toastEl, textEl);
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
        if (nextMessage.canonical_context) previous.canonical_context = nextMessage.canonical_context;
        if (nextMessage.canonical_context_delta) previous.canonical_context_delta = nextMessage.canonical_context_delta;
        // 悬停元数据同样参与合并:重复 finalize/快照回写时新值覆盖旧值,不丢失。
        if (nextMessage.usage) previous.usage = nextMessage.usage;
        if (nextMessage.timestamp) previous.timestamp = nextMessage.timestamp;
        return trimCeoSessionSnapshotMessages(next);
    }
    next.push(nextMessage);
    return trimCeoSessionSnapshotMessages(next);
}

function ceoSnapshotHasEquivalentUserMessage(messages = [], candidate = null) {
    const normalizedCandidate = normalizeCeoSnapshotMessage(candidate);
    if (!normalizedCandidate || normalizedCandidate.role !== "user") return false;
    const candidateTurnId = normalizeCeoTurnId(normalizedCandidate.turn_id || "");
    const candidateAttachments = normalizeUploadList(normalizedCandidate.attachments);
    const candidateContent = String(normalizedCandidate.content || "");
    return trimCeoSessionSnapshotMessages(messages).some((item) => {
        if (String(item?.role || "").trim().toLowerCase() !== "user") return false;
        const itemTurnId = normalizeCeoTurnId(item?.turn_id || "");
        // 同一轮可以存在多条不同内容的 user 消息(中途补充被消费后)。只有
        // turn_id(若双方都有)、内容、附件三者全部一致才视为"已存在",避免把内容
        // 不同的补充消息误判成重复而跳过气泡渲染(表现为"消息凭空消失")。
        if ((candidateTurnId || itemTurnId) && itemTurnId !== candidateTurnId) return false;
        const sameAttachments = JSON.stringify(normalizeUploadList(item?.attachments)) === JSON.stringify(candidateAttachments);
        return String(item?.content || "") === candidateContent && sameAttachments;
    });
}

function appendMissingCeoUserMessages(messages = [], userMessages = []) {
    let next = trimCeoSessionSnapshotMessages(messages);
    normalizeCeoSnapshotUserMessages(userMessages).forEach((message) => {
        if (ceoSnapshotHasEquivalentUserMessage(next, message)) return;
        next = appendCeoSessionSnapshotMessage(next, message);
    });
    return next;
}

function dedupeInflightUserMessageAgainstMessages(messages = [], inflightTurn = null) {
    const normalizedInflight = normalizeCeoSnapshotInflight(inflightTurn);
    if (!normalizedInflight?.user_message && !(normalizedInflight?.user_messages || []).length) return normalizedInflight;
    const normalizedMessages = trimCeoSessionSnapshotMessages(messages);
    const inflightUserMessages = normalizeCeoSnapshotUserMessages(
        normalizedInflight?.user_messages,
        normalizedInflight?.user_message
    );
    const remainingUserMessages = inflightUserMessages.filter((message) => !ceoSnapshotHasEquivalentUserMessage(normalizedMessages, message));
    if (inflightUserMessages.length) {
        const deduped = { ...normalizedInflight };
        if (remainingUserMessages.length) {
            deduped.user_messages = remainingUserMessages;
            const lastUserMessage = remainingUserMessages[remainingUserMessages.length - 1];
            if (lastUserMessage) {
                deduped.user_message = { content: String(lastUserMessage.content || "") };
                const attachments = cloneCeoSnapshotAttachments(lastUserMessage.attachments);
                if (attachments.length) deduped.user_message.attachments = attachments;
            } else {
                delete deduped.user_message;
            }
            return deduped;
        }
        delete deduped.user_message;
        delete deduped.user_messages;
        return ceoNeedsAssistantTurn(deduped) ? deduped : null;
    }
    const inflightTurnId = normalizeCeoTurnId(normalizedInflight.turn_id || "");
    if (inflightTurnId) {
        const alreadyTracked = normalizedMessages.some((item) => (
            String(item?.role || "").trim().toLowerCase() === "user"
            && normalizeCeoTurnId(item?.turn_id || "") === inflightTurnId
        ));
        if (alreadyTracked) {
            const deduped = { ...normalizedInflight };
            delete deduped.user_message;
            delete deduped.user_messages;
            return ceoNeedsAssistantTurn(deduped) ? deduped : null;
        }
    }
    const lastUserMessage = [...normalizedMessages].reverse().find((item) => String(item?.role || "").trim().toLowerCase() === "user");
    if (!lastUserMessage || !normalizedInflight?.user_message) return normalizedInflight;
    const userContent = String(normalizedInflight.user_message?.content || "");
    const userAttachments = normalizeUploadList(normalizedInflight.user_message?.attachments);
    const lastContent = String(lastUserMessage?.content || "");
    const lastAttachments = normalizeUploadList(lastUserMessage?.attachments);
    const sameAttachments = JSON.stringify(lastAttachments) === JSON.stringify(userAttachments);
    if (lastContent !== userContent || !sameAttachments) return normalizedInflight;
    const deduped = { ...normalizedInflight };
    delete deduped.user_message;
    delete deduped.user_messages;
    return ceoNeedsAssistantTurn(deduped) ? deduped : null;
}

function promoteRepresentedRuntimeSentCeoFollowUps(sessionId = activeSessionId(), representedMessages = [], { scrollMode = "preserve", insertBefore = null } = {}) {
    const key = String(sessionId || "").trim();
    if (!key) return [];
    const normalizedRepresented = normalizeCeoSnapshotUserMessages(representedMessages);
    if (!normalizedRepresented.length) return [];
    const current = getCeoQueuedFollowUps(key);
    const promoted = current.filter((item) => (
        String(item?.runtime_sent_at || "").trim()
        && normalizedRepresented.some((message) => ceoSnapshotMessageMatchesQueuedFollowUp(message, item))
    ));
    if (!promoted.length) return [];
    promoted.forEach((item) => {
        // 发送时间优先取服务端快照消息的 timestamp,回退本地记录的 runtime_sent_at。
        const matched = normalizedRepresented.find((message) => ceoSnapshotMessageMatchesQueuedFollowUp(message, item));
        addCeoUserMessage(item.text, {
            attachments: item.uploads,
            scrollMode,
            sessionId: key,
            timestamp: String(matched?.timestamp || item?.runtime_sent_at || ""),
        });
        // 补充消息应落在当前 live 回合之前,而不是 append 到流式输出之后;
        // 与 finalize 增量路径的 insertBefore 语义保持一致。
        if (U && U.ceoFeed && insertBefore && typeof U.ceoFeed.insertBefore === "function") {
            const el = U.ceoFeed.lastElementChild || (Array.from(U.ceoFeed.children || []).slice(-1)[0] || null);
            if (el && el !== insertBefore) {
                try {
                    U.ceoFeed.insertBefore(el, insertBefore);
                } catch (error) {
                    void error;
                }
            }
        }
    });
    const promotedIds = new Set(promoted.map((item) => String(item?.id || "").trim()).filter(Boolean));
    setCeoQueuedFollowUps(key, current.filter((item) => !promotedIds.has(String(item?.id || "").trim())));
    return promoted;
}

function renderCeoSessionLoadingState(sessionId, session = null) {
    resetCeoFeed();
    hideCeoContextLoadNotice();
    const title = String(session?.title || session?.channel_id || sessionId || "conversation").trim() || "conversation";
    addMsg(`Loading ${title}...`, "system", { scrollMode: "bottom" });
}

function renderCeoSessionSnapshotFromCache(sessionId, { scrollToLatest = true } = {}) {
    const entry = getCeoSessionSnapshotCache(sessionId);
    if (!entry) return false;
    if (scrollToLatest) S.ceoScrollToLatestOnSnapshot = true;
    renderCeoSnapshot(entry.messages || [], entry.inflight_turn || null, { preservedTurn: entry.preserved_turn || null });
    if (String(sessionId || "").trim() === activeSessionId()) {
        syncCeoApprovalFromSnapshotEntry(sessionId, entry, { authoritative: true, refreshServer: true });
    }
    return true;
}

function ceoApprovalInterruptsFromSnapshotEntry(entry = null) {
    const snapshotEntry = entry && typeof entry === "object" ? entry : null;
    const inflightInterrupts = Array.isArray(snapshotEntry?.inflight_turn?.interrupts)
        ? snapshotEntry.inflight_turn.interrupts
        : [];
    if (inflightInterrupts.length) return inflightInterrupts;
    return Array.isArray(snapshotEntry?.preserved_turn?.interrupts)
        ? snapshotEntry.preserved_turn.interrupts
        : [];
}

function syncCeoApprovalFromSnapshotEntry(
    sessionId,
    entry = null,
    { authoritative = false, refreshServer = false } = {},
) {
    const normalizedSessionId = String(sessionId || "").trim();
    if (!normalizedSessionId) return;
    const snapshotEntry = entry && typeof entry === "object"
        ? entry
        : getCeoSessionSnapshotCache(normalizedSessionId);
    const interrupts = ceoApprovalInterruptsFromSnapshotEntry(snapshotEntry);
    if (typeof syncCeoApprovalFromInterrupts === "function") {
        syncCeoApprovalFromInterrupts(interrupts, normalizedSessionId, { authoritative });
    }
    if (refreshServer && typeof refreshCeoApprovalFromServer === "function") {
        void refreshCeoApprovalFromServer(normalizedSessionId, { quiet: true });
    }
}

function isTaskDetailsViewActive() {
    // 详情视图元素缺失（无真实 DOM 的测试环境）按可见处理：任务树管线的
    // "离开视图"守卫不能把元素缺失误判为未查看，否则无 DOM 环境语义全变。
    if (!U.viewTaskDetails) return true;
    return !!U.viewTaskDetails.classList.contains("active");
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
    const messageItems = normalizeTraceItems(value.messageItems);
    const spawnReviewItems = normalizeTraceItems(value.spawnReviewItems);
    return {
        detailScrollTop: normalizeScrollTop(value.detailScrollTop),
        traceScrollTop: normalizeScrollTop(value.traceScrollTop),
        messageScrollTop: normalizeScrollTop(value.messageScrollTop),
        spawnReviewScrollTop: normalizeScrollTop(value.spawnReviewScrollTop),
        artifactListScrollTop: normalizeScrollTop(value.artifactListScrollTop),
        artifactContentScrollTop: normalizeScrollTop(value.artifactContentScrollTop),
        traceItems,
        messageItems,
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
    const messageState = captureTraceSectionViewState(U.adMessages);
    const spawnReviewState = captureTraceSectionViewState(U.adSpawnReviews);
    return {
        detailScrollTop: U.detail instanceof HTMLElement ? U.detail.scrollTop : 0,
        traceScrollTop: traceState.scrollTop,
        messageScrollTop: messageState.scrollTop,
        spawnReviewScrollTop: spawnReviewState.scrollTop,
        artifactListScrollTop: U.artifactList instanceof HTMLElement ? U.artifactList.scrollTop : 0,
        artifactContentScrollTop: U.artifactContent instanceof HTMLElement ? U.artifactContent.scrollTop : 0,
        traceItems: traceState.items,
        messageItems: messageState.items,
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
        messages = true,
        messageItems = true,
        spawnReviews = true,
        spawnReviewItems = true,
        artifactList = true,
        artifactContent = true,
    } = {},
) {
    if (!state || typeof state !== "object") return;
    const getTraceList = () => U.adFlow?.querySelector(".task-trace-list");
    const getMessageList = () => U.adMessages?.querySelector(".task-trace-list");
    const getSpawnReviewList = () => U.adSpawnReviews?.querySelector(".task-trace-list");
    const getArtifactList = () => U.artifactList;
    const getArtifactContent = () => U.artifactContent;
    const applyScrollPositions = () => {
        const traceList = getTraceList();
        const messageList = getMessageList();
        const spawnReviewList = getSpawnReviewList();
        if (detail) setElementScrollTop(U.detail, state.detailScrollTop);
        if (trace) setElementScrollTop(traceList, state.traceScrollTop);
        if (messages) setElementScrollTop(messageList, state.messageScrollTop);
        if (spawnReviews) setElementScrollTop(spawnReviewList, state.spawnReviewScrollTop);
        if (artifactList) setElementScrollTop(getArtifactList(), state.artifactListScrollTop);
        if (artifactContent) setElementScrollTop(getArtifactContent(), state.artifactContentScrollTop);
    };
    if (trace && traceItems) applyTaskTraceItemViewState(getTraceList(), state.traceItems);
    if (messages && messageItems) applyTaskTraceItemViewState(getMessageList(), state.messageItems);
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

function renderMessageHeading(count = 0) {
    renderTaskSectionHeading(U.adMessagesHeading, { icon: "inbox", label: "消息列表", count });
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

/* 会话切换后的一次性 unread 豁免:
   列表通道(ceo.sessions.patch/snapshot、REST)的 message_count 更新滞后于聊天通道渲染,
   且切换瞬间 closeCeoWs 会丢掉在途 patch;切走之后迟到的计数补算会把用户已经看过的
   旧消息误判为原会话的 unread。用户发起的切换(切换/新建会话)时为被离开的会话武装一条
   窗口期豁免(多槽、互不覆盖):窗口期内其第一个正增量视为已读(只抬 baseline 不计 unread),
   消费即失效;窗口过期自动作废,不误吞真新消息。 */
const CEO_SESSION_UNREAD_EXEMPT_WINDOW_MS = 10000;

function armCeoSessionUnreadExemption(previousActiveId) {
    // 仅当页面已水合(存在真实的前序会话基线)才武装:冷启动时 activeSessionId() 会回退到
    // ApiClient 的兜底 id "web:shared",不加此守卫的话每次加载都会为「用户从未看过的会话」误武装。
    if (!S.ceoSessionHydrated) return;
    const key = String(previousActiveId || "").trim();
    if (!key) return;
    S.ceoSessionUnreadExempt = {
        ...(S.ceoSessionUnreadExempt || {}),
        [key]: Date.now() + CEO_SESSION_UNREAD_EXEMPT_WINDOW_MS,
    };
}

function clearCeoSessionUnreadExemption(sessionId) {
    const exempt = S.ceoSessionUnreadExempt || {};
    if (!sessionId) {
        S.ceoSessionUnreadExempt = {};
        return;
    }
    if (!Object.prototype.hasOwnProperty.call(exempt, sessionId)) return;
    const next = { ...exempt };
    delete next[sessionId];
    S.ceoSessionUnreadExempt = next;
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

    if (S.ceoSessionUnreadExempt && Object.keys(S.ceoSessionUnreadExempt).some((key) => S.ceoSessionUnreadExempt[key] <= Date.now())) {
        const next = { ...S.ceoSessionUnreadExempt };
        Object.keys(next).forEach((key) => { if (next[key] <= Date.now()) delete next[key]; });
        S.ceoSessionUnreadExempt = next;
    }

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
            if (Object.prototype.hasOwnProperty.call(S.ceoSessionUnreadExempt || {}, sessionId)) {
                // 离开该会话后的第一个正增量 = 列表计数迟到补算(消息已在聊天区看过),视为已读
                clearCeoSessionUnreadExemption(sessionId);
                nextUnread[sessionId] = 0;
                return;
            }
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
    return !(S.ceoPauseBusy || S.ceoUploadBusy || S.ceoSessionCatalogBusy);
}

function canSelectCeoBulkSessions() {
    return !(S.ceoPauseBusy || S.ceoUploadBusy || S.ceoSessionCatalogBusy);
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
    const bulkSelectionDisabled = !canSelectCeoBulkSessions();
    const activationDisabled = !canActivateCeoSessions();
    const bulkIds = visibleCeoBulkSelectableSessionIds();
    const allSelected = bulkIds.length > 0 && bulkIds.every((sessionId) => isCeoBulkSessionSelected(sessionId));
    if (U.ceoNewSession) U.ceoNewSession.disabled = creationDisabled;
    if (U.ceoSessionBulkToggle) {
        U.ceoSessionBulkToggle.hidden = !S.ceoSessionPanelExpanded;
        U.ceoSessionBulkToggle.disabled = bulkSelectionDisabled && !S.ceoBulkMode;
        U.ceoSessionBulkToggle.textContent = S.ceoBulkMode ? "取消" : "多选";
        U.ceoSessionBulkToggle.setAttribute("aria-pressed", S.ceoBulkMode ? "true" : "false");
    }
    if (U.ceoSessionBulkActions) U.ceoSessionBulkActions.hidden = !(S.ceoSessionPanelExpanded && S.ceoBulkMode);
    if (U.ceoSessionBulkDelete) U.ceoSessionBulkDelete.disabled = mutationDisabled || S.ceoSelectedSessionIds.size <= 0;
    if (U.ceoSessionBulkSelectAll) {
        U.ceoSessionBulkSelectAll.disabled = bulkSelectionDisabled || bulkIds.length <= 0;
        U.ceoSessionBulkSelectAll.setAttribute("aria-pressed", allSelected ? "true" : "false");
    }
    U.ceoSessionList?.querySelectorAll("[data-session-activate]")?.forEach((button) => {
        const targetId = String(button?.dataset?.sessionActivate || "").trim();
        button.disabled = S.ceoBulkMode ? false : activationDisabled || targetId === activeSessionId();
    });
    U.ceoSessionList?.querySelectorAll("[data-session-bulk-checkbox]")?.forEach((input) => {
        input.disabled = bulkSelectionDisabled;
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

function ceoMarkdownImageSrc(href) {
    const src = String(href || "").trim();
    if (!src) return "";
    if (/^https?:\/\//i.test(src)) return esc(src);
    if (/^data:/i.test(src)) return "";
    if (src.startsWith("/") && !/^[A-Za-z]:[\\/]/.test(src)) return esc(src);
    const params = new URLSearchParams({
        session_id: String(activeSessionId() || "").trim(),
        path: src,
    });
    return esc(`/api/ceo/uploads/file?${params.toString()}`);
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
        text = text.replace(/!\[([^\]]*)\]\(([^)\s]+(?:\s+"[^"]*")?)\)/g, (_match, alt, target) => {
            const titleMatch = /\s+"([^"]*)"$/.exec(String(target || ""));
            const rawSrc = String(target || "").replace(/\s+"[^"]*"$/, "");
            const safeSrc = ceoMarkdownImageSrc(rawSrc);
            if (!safeSrc) return _match;
            const label = esc(String(alt || "image").trim() || "image");
            const imgTag = `<img class="msg-inline-image" src="${safeSrc}" alt="${label}" title="${label}" loading="lazy">`;
            const clickHref =
                titleMatch && /^\/api\/ceo\/media\/original\?/.test(String(titleMatch[1] || "").trim())
                    ? esc(String(titleMatch[1]).trim())
                    : "";
            if (!clickHref) return createMarkdownToken(tokens, imgTag);
            return createMarkdownToken(
                tokens,
                `<a class="msg-inline-image-link" href="${clickHref}" target="_blank" rel="noreferrer noopener">${imgTag}</a>`
            );
        });
        text = text.replace(/\[([^\]]+)\]\(([^)\s]+(?:\s+"[^"]*")?)\)/g, (_match, label, target) => {
            const href = String(target || "").replace(/\s+"[^"]*"$/, "");
            if (/^\/api\/ceo\/media\/original\?/.test(href)) {
                const chipLabel = esc(String(label || "file").trim() || "file");
                return createMarkdownToken(
                    tokens,
                    `<a class="msg-file-chip" href="${esc(href)}" target="_blank" rel="noreferrer noopener">` +
                        `<svg class="msg-file-chip-icon" viewBox="0 0 16 16" width="14" height="14" aria-hidden="true">` +
                        `<path d="M4.5 1.5h5l2.5 2.5v10.5h-7.5z" fill="none" stroke="currentColor" stroke-width="1.1"/>` +
                        `<path d="M9.5 1.5V4h2.5" fill="none" stroke="currentColor" stroke-width="1.1"/></svg>` +
                        `<span>${chipLabel}</span></a>`
                );
            }
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

function ceoAttachmentHref(item = {}, sessionId = activeSessionId()) {
    const explicitUrl = String(item?.url || "").trim();
    if (explicitUrl) return explicitUrl;
    const path = String(item?.path || "").trim();
    if (!path) return "";
    const params = new URLSearchParams({
        session_id: String(sessionId || activeSessionId() || "").trim(),
        path,
    });
    return `/api/ceo/uploads/file?${params.toString()}`;
}

function renderStructuredChatAttachmentCard(item = {}, { sessionId = activeSessionId() } = {}) {
    const href = ceoAttachmentHref(item, sessionId);
    const safeHref = esc(href || "#");
    const label = esc(String(item.name || item.path || "attachment"));
    const size = esc(formatFileSize(item.size));
    if (String(item.kind || "").trim() === "image") {
        return `
            <a class="chat-attachment-link chat-attachment-image" role="listitem" href="${safeHref}" target="_blank" rel="noreferrer noopener">
                <img class="chat-attachment-thumb" src="${safeHref}" alt="${label}" loading="lazy">
                <span class="chat-attachment-image-meta">
                    <span class="chat-attachment-name">${label}</span>
                    <span class="chat-attachment-size">${size}</span>
                </span>
            </a>
        `;
    }
    return `
        <a class="chat-attachment-link chat-attachment-file" role="listitem" href="${safeHref}" target="_blank" rel="noreferrer noopener">
            <span class="chat-attachment-kind">文件</span>
            <span class="chat-attachment-name">${label}</span>
            <span class="chat-attachment-size">${size}</span>
        </a>
    `;
}

function renderStructuredChatAttachments(items = [], { sessionId = activeSessionId() } = {}) {
    const uploads = normalizeUploadList(items);
    if (!uploads.length) return "";
    return `
        <div class="chat-attachment-stack" role="list">
            ${uploads.map((item) => renderStructuredChatAttachmentCard(item, { sessionId })).join("")}
        </div>
    `;
}

function addCeoUserMessage(text = "", { attachments = [], scrollMode = "preserve", sessionId = activeSessionId(), timestamp = "", turnId = "", canEditFork = false } = {}) {
    return addMsg(String(text || ""), "user", { attachments, scrollMode, sessionId, timestamp, turnId, canEditFork });
}

function buildCeoUserMessageActionsMarkup({ turnId = "", canEditFork = false, sessionId = "" } = {}) {
    // 用户气泡下方的编辑重发/Fork 按钮行。渲染条件三重防御:
    // 服务端 can_edit_fork 门槛(任务派发/run 首条/边界快照/稳定态) + web: 会话 + 非只读。
    // 回合进行中由 feed 级 .ceo-turn-active class 整体隐藏(防御型显示)。
    const key = String(turnId || "").trim();
    if (!key || canEditFork !== true) return "";
    if (!String(sessionId || "").trim().startsWith("web:")) return "";
    if (typeof activeSessionIsReadonly === "function" && activeSessionIsReadonly()) return "";
    const safeTurn = esc(key);
    return `
        <div class="msg-actions">
            <button type="button" class="msg-action-btn" data-ceo-edit-resend="${safeTurn}" title="编辑重发：发送后该消息及其后所有内容将被清空" aria-label="编辑重发">
                <i data-lucide="pencil"></i><span>编辑</span>
            </button>
            <button type="button" class="msg-action-btn" data-ceo-fork="${safeTurn}" title="Fork：把此消息之前的内容复制成新会话，此消息回填输入框" aria-label="Fork 会话">
                <i data-lucide="git-fork"></i><span>Fork</span>
            </button>
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
    scheduleCeoComposerUsageRefresh();
    icons();
}

function renderQueuedCeoFollowUps(sessionId = activeSessionId()) {
    if (!U.ceoFollowUpQueue) return;
    const key = String(sessionId || "").trim();
    const items = getMergedCeoQueuedFollowUps(key);
    U.ceoFollowUpQueue.hidden = !items.length;
    if (!items.length) {
        U.ceoFollowUpQueue.innerHTML = "";
        return;
    }
    U.ceoFollowUpQueue.innerHTML = `
        <div class="ceo-follow-up-chip-list" role="list">
            ${items.map((item, index) => {
                // runtime 已受理的条目不能删：它已经落进队列（重启也还在），撤回它没有对应操作。
                const trailing = item.accepted_by_runtime
                    ? `<span class="ceo-follow-up-state">已受理</span>`
                    : `<button type="button" class="ceo-follow-up-remove" data-follow-up-remove="${esc(String(item.id || ""))}" aria-label="删除待发送补充">
                            <i data-lucide="x"></i>
                        </button>`;
                return `
                <div class="ceo-follow-up-chip" role="listitem">
                    <span class="ceo-follow-up-kind">${index + 1}</span>
                    <span class="ceo-follow-up-name">${esc(String(item.text || "").trim() || summarizeUploads(item.uploads || []))}</span>
                    ${trailing}
                </div>
            `;
            }).join("")}
        </div>
    `;
    scheduleCeoComposerUsageRefresh();
    icons();
}

function syncCeoPrimaryButton() {
    syncCeoFeedTurnActiveClass();
    syncCeoAttachButton();
    if (!U.ceoSend) return;
    if (activeSessionIsReadonly()) {
        if (S.ceoTurnActive) {
            // 渠道会话禁止发送，但运行中的回合必须能暂停：只读早期返回
            // 曾把唯一的暂停入口也禁掉，导致渠道回合无法在网页暂停。
            const label = S.ceoPauseBusy ? "暂停中" : "暂停";
            U.ceoSend.innerHTML = `<i data-lucide="pause"></i> ${label}`;
            U.ceoSend.disabled = !!S.ceoPauseBusy;
            U.ceoSend.setAttribute("aria-label", "暂停当前渠道会话回合");
        } else {
            U.ceoSend.innerHTML = '<i data-lucide="eye"></i> 渠道会话只读';
            U.ceoSend.disabled = true;
            U.ceoSend.setAttribute("aria-label", "渠道会话只读");
        }
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
        turn.liveStreamText = "";
        renderCeoLiveStreamTextIntoTurn(turn);
        turn.textEl.textContent = String(text || "已暂停");
        turn.textEl.classList.remove("pending");
        if (turn.steps > 0) {
            turn.flowEl.hidden = false;
            turn.flowEl.open = true;
            updateCeoTurnMeta(turn, "已暂停");
        } else {
            turn.flowEl.hidden = true;
        }
        setCeoTurnUsageCollapsed(turn, true);
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

function isCeoApprovalPauseSourceLegacy(source = "") {
    return normalizeCeoTurnSource(source) === "approval";
}

function holdApprovalPausedCeoTurnLegacy(text = "", { source = "", turnId = "" } = {}) {
    const normalizedSource = normalizeCeoTurnSource(source || "approval");
    const normalizedTurnId = normalizeCeoTurnId(turnId);
    const turn = ensureActiveCeoTurn({ source: normalizedSource, turnId: normalizedTurnId });
    if (!turn?.textEl || !turn.flowEl) return false;
    mutateCeoFeed(() => {
        renderCeoAssistantTextIntoTurn(turn, String(text || ""), { status: "paused" });
        turn.finalized = false;
        if (turn.steps > 0) {
            turn.flowEl.hidden = false;
            turn.flowEl.open = true;
        }
        updateCeoTurnMeta(turn, "绛夊緟瀹℃壒");
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

function adoptCeoContextCompression(sessionId, compression) {
    // 回合外的手动压缩没有 inflight turn，进行中状态只活在服务端会话级 `compression` 里
    // （state 快照与 snapshot.ceo 都带）。刷新会清空本机 S.ceoContextCompression*，不认领
    // 回来就再也看不到区分线，也收不到终局——必须再刷一次才看得到「会话已压缩」。
    const key = String(sessionId || "").trim();
    const normalized = normalizeCeoSnapshotCompression(compression);
    if (!key) return;
    if (String(normalized?.status || "").trim().toLowerCase() !== "running") return;
    if (String(normalized?.source || "").trim().toLowerCase() !== "manual_context_compression") return;
    if (String(S.ceoContextCompressionSessionId || "").trim() === key
        && String(S.ceoContextCompressionStatus || "").trim().toLowerCase() === "running") {
        return;
    }
    S.ceoContextCompressionSessionId = key;
    S.ceoContextCompressionStatus = "running";
    S.ceoContextCompressionCancelRequested = false;
    syncCeoCompressionDivider();
}

function applyCeoState(state = {}, meta = {}) {
    const status = String(state?.status || "").trim().toLowerCase();
    const source = String(meta?.source || state?.source || "").trim().toLowerCase();
    const turnId = String(meta?.turn_id || state?.turn_id || "").trim();
    const running = !!state?.is_running || status === "running";
    const paused = !!state?.paused || status === "paused";
    adoptCeoContextCompression(activeSessionId(), state?.compression);
    // 候选条以 runtime 的队列为真相：换标签页/重启后 sessionStorage 里没有的东西，
    // 也要在输入框上方看得见（它已经落在转录里，只是还没成回合）。
    if (adoptCeoServerQueuedFollowUpsFromState(state)) renderQueuedCeoFollowUps(activeSessionId());
    const activeTurn = source || turnId ? getActiveCeoTurn(source, turnId) : getActiveCeoTurn();
    const hadTurnContext = !!activeTurn || !!S.ceoTurnActive;
    S.ceoTurnActive = running;
    if (patchCeoSessionRuntimeState(activeSessionId(), running)) renderCeoSessions();
    if (!running) S.ceoPauseBusy = false;
    if (running) {
        if (activeTurn) {
            if (source) activeTurn.source = normalizeCeoTurnSource(source);
            if (turnId) activeTurn.turnId = turnId;
        } else if (hadTurnContext && (source || turnId) && source !== "heartbeat") {
            // Ignore stale running snapshots that arrive after the turn already finished.
            ensureActiveCeoTurn({ source, turnId });
        }
    }
    if (paused) finalizePausedCeoTurn("已暂停", { source, turnId });
    scheduleSyncCeoComposerUsageOutline();
    syncCeoSessionActions();
    syncCeoPrimaryButton();
    if (!running && !paused) {
        pruneRuntimeSentCeoFollowUps(activeSessionId());
        maybeDispatchQueuedCeoFollowUps();
    }
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
    pruneRuntimeSentCeoFollowUps(activeSessionId());
    maybeDispatchQueuedCeoFollowUps();
}

function handleCeoError(payload = {}) {
    S.ceoTurnActive = false;
    S.ceoPauseBusy = false;
    if (patchCeoSessionRuntimeState(activeSessionId(), false)) renderCeoSessions();
    syncCeoSessionActions();
    syncCeoPrimaryButton();
    pruneRuntimeSentCeoFollowUps(activeSessionId());
    if (["frontdoor_context_window_exceeded", "model_context_window_missing"].includes(String(payload?.code || "").trim())) {
        showToast({
            title: "上下文超限",
            text: String(payload?.message || "上下文大小超出当前模型，请更改模型链配置后继续"),
            kind: "error",
        });
    }
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
    const sentAt = new Date().toISOString();
    addCeoUserMessage(normalizedText, {
        attachments: normalizedUploads,
        scrollMode,
        sessionId: activeSessionId(),
        timestamp: sentAt,
    });
    const turn = createPendingCeoTurn("user", { scrollMode });
    if (turn) S.ceoPendingTurns.push(turn);
    S.ceoTurnActive = true;
    S.ceoPauseBusy = false;
    setCeoComposerUsagePinnedEntries(activeSessionId(), [{ text: normalizedText, uploads: normalizedUploads }]);
    setCeoSessionSnapshotCache(activeSessionId(), {
        inflight_turn: {
            source: "user",
            status: "running",
            user_message: {
                content: normalizedText,
                attachments: normalizedUploads,
                timestamp: sentAt,
            },
        },
    });
    if (patchCeoSessionRuntimeState(activeSessionId(), true)) renderCeoSessions();
    syncCeoSessionActions();
    syncCeoPrimaryButton();
    scheduleSyncCeoComposerUsageOutline();
    scheduleCeoComposerUsageRefresh({ immediate: true });
    return true;
}

function sendImmediateCeoMessageBatch(entries = [], { scrollMode = "bottom" } = {}) {
    const normalizedEntries = (Array.isArray(entries) ? entries : [])
        .map((entry) => ({
            text: String(entry?.text || ""),
            uploads: normalizeUploadList(entry?.uploads),
        }))
        .filter((entry) => entry.text.trim() || entry.uploads.length > 0);
    if (!normalizedEntries.length) return false;
    if (!S.ceoWs || S.ceoWs.readyState !== WebSocket.OPEN) {
        addMsg("Connection is not ready yet. Please try again in a moment.", "system");
        initCeoWs();
        return false;
    }
    S.ceoWs.send(JSON.stringify({
        type: "client.user_message",
        session_id: activeSessionId(),
        messages: normalizedEntries.map((entry) => ({
            text: entry.text,
            uploads: entry.uploads.map((item) => ({
                name: item.name,
                path: item.path,
                mime_type: item.mime_type,
                kind: item.kind,
                size: item.size,
            })),
        })),
    }));
    const batchSentAt = new Date().toISOString();
    normalizedEntries.forEach((entry) => {
        addCeoUserMessage(entry.text, {
            attachments: entry.uploads,
            scrollMode,
            sessionId: activeSessionId(),
            timestamp: batchSentAt,
        });
    });
    const turn = createPendingCeoTurn("user", { scrollMode });
    if (turn) S.ceoPendingTurns.push(turn);
    const lastEntry = normalizedEntries[normalizedEntries.length - 1];
    S.ceoTurnActive = true;
    S.ceoPauseBusy = false;
    setCeoComposerUsagePinnedEntries(activeSessionId(), normalizedEntries);
    setCeoSessionSnapshotCache(activeSessionId(), {
        inflight_turn: {
            source: "user",
            status: "running",
            user_message: {
                content: lastEntry.text,
                attachments: lastEntry.uploads,
                timestamp: batchSentAt,
            },
        },
    });
    if (patchCeoSessionRuntimeState(activeSessionId(), true)) renderCeoSessions();
    syncCeoSessionActions();
    syncCeoPrimaryButton();
    scheduleSyncCeoComposerUsageOutline();
    scheduleCeoComposerUsageRefresh({ immediate: true });
    return true;
}

function maybeDispatchQueuedCeoFollowUps() {
    if (S.ceoQueuedFollowUpDispatching || S.ceoTurnActive || S.ceoSessionBusy || S.ceoSessionCatalogBusy) return false;
    const sessionId = activeSessionId();
    if (!sessionId) return false;
    const current = getCeoQueuedFollowUps(sessionId);
    const queued = current.filter((item) => !String(item?.runtime_sent_at || "").trim());
    if (!queued.length) return false;
    S.ceoQueuedFollowUpDispatching = true;
    try {
        const retained = current.filter((item) => String(item?.runtime_sent_at || "").trim());
        setCeoQueuedFollowUps(sessionId, retained);
        const sent = sendImmediateCeoMessageBatch(
            queued.map((item) => ({ text: item.text, uploads: item.uploads })),
            { scrollMode: "bottom" }
        );
        if (!sent) {
            setCeoQueuedFollowUps(sessionId, current);
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

// ===== 用户消息编辑重发 / Fork 会话 =====
// 按钮可见性由服务端 can_edit_fork 门槛驱动(任务派发严格判定 / user-run 首条 /
// 边界快照可用 / 会话完全稳定);前端另有 .ceo-turn-active 防御性隐藏与点击守卫。

function syncCeoFeedTurnActiveClass() {
    // 防御型显示:任何回合进行中(含 heartbeat/cron 内部轮)整体隐藏编辑/Fork 按钮。
    if (!U.ceoFeed || !U.ceoFeed.classList) return;
    U.ceoFeed.classList.toggle("ceo-turn-active", !!S.ceoTurnActive);
}

function findCeoSnapshotMessageByTurnId(sessionId, turnId) {
    const key = String(sessionId || "").trim();
    const target = String(turnId || "").trim();
    if (!key || !target) return null;
    const entry = getCeoSessionSnapshotCache(key);
    const messages = Array.isArray(entry?.messages) ? entry.messages : [];
    for (let index = messages.length - 1; index >= 0; index -= 1) {
        const item = messages[index];
        if (!item || typeof item !== "object") continue;
        if (String(item?.role || "").trim().toLowerCase() !== "user") continue;
        if (String(item?.turn_id || "").trim() === target) return item;
    }
    return null;
}

function applyCeoEditForkGates(payload = {}, sessionId = "") {
    // 回合收尾后服务端补发的权威门槛：把它落到缓存里对应的用户行上，再走
    // renderCeoSnapshot 的签名重建，编辑/Fork 按钮就不必等手动刷新才出现。
    // 整份替换语义（不在列表里的行一律收 flag）与 snapshot.ceo 一致：新回合会让
    // 上一轮失去"最近 3 轮"窗口，任务派发会收回整批资格。
    const key = String(sessionId || activeSessionId() || "").trim();
    if (!key || key !== String(activeSessionId() || "").trim()) return;
    const eligibleTurnIds = new Set((Array.isArray(payload?.turn_ids) ? payload.turn_ids : [])
        .map((item) => String(item || "").trim())
        .filter(Boolean));
    const entry = getCeoSessionSnapshotCache(key);
    const messages = Array.isArray(entry?.messages) ? entry.messages : [];
    if (!messages.length) return;
    const claimedTurnIds = new Set();
    let changed = false;
    const nextMessages = messages.map((item) => {
        if (!item || typeof item !== "object") return item;
        if (String(item.role || "").trim().toLowerCase() !== "user") return item;
        const turnId = String(item.turn_id || "").trim();
        // 同一 run 的连续消息共享 turn_id，门槛只属首条行——与按下标编码的服务端一致。
        const allowed = !!turnId && eligibleTurnIds.has(turnId) && !claimedTurnIds.has(turnId);
        if (allowed) claimedTurnIds.add(turnId);
        if (allowed === (item.can_edit_fork === true)) return item;
        changed = true;
        const next = { ...item };
        if (allowed) next.can_edit_fork = true;
        else delete next.can_edit_fork;
        return next;
    });
    if (!changed) return;
    const updatedEntry = patchCeoSessionSnapshotCache(key, (current) => ({
        ...(current || {}),
        messages: nextMessages,
    }));
    const renderEntry = updatedEntry || getCeoSessionSnapshotCache(key);
    renderCeoSnapshot(
        renderEntry?.messages || nextMessages,
        renderEntry?.inflight_turn || null,
        { sessionId: key, preservedTurn: renderEntry?.preserved_turn || null }
    );
}

function ceoHistoryEditBusyReason() {
    if (S.ceoTurnActive) return "回合进行中（含心跳内部轮），请等待结束或先暂停。";
    if (S.ceoSessionBusy || S.ceoSessionCatalogBusy) return "会话操作进行中，请稍后再试。";
    if (S.ceoUploadBusy) return "附件仍在上传，请稍候再试。";
    if (S.ceoPauseBusy) return "暂停请求进行中，请稍后再试。";
    return "";
}

function editForkErrorText(error) {
    const fallbackCode = typeof ApiClient !== "undefined" && ApiClient?.getErrorCode
        ? ApiClient.getErrorCode(error?.data || error?.payload)
        : "";
    const code = String(error?.code || fallbackCode || "").trim();
    const known = {
        edit_fork_blocked_by_async_task: "该消息之前（或其回复轮中）已创建过异步任务，不能再编辑或 Fork。",
        turn_not_run_first: "同批连续消息只支持在第一条上编辑/Fork。",
        boundary_unavailable: "该消息的上下文边界快照已超出保留窗口（最近 3 轮），无法编辑/Fork。",
        turn_not_editable: "该消息当前不支持编辑/Fork。",
        turn_not_found: "消息不存在或已被清空，请刷新后重试。",
        ceo_turn_in_progress: "回合进行中，请等待结束或先暂停后再操作。",
        channel_session_readonly: "渠道会话只读，不支持编辑或 Fork。",
        session_not_found: "会话不存在或已被删除。",
        no_model_configured: "尚未配置模型，无法执行该操作。",
    };
    if (code && known[code]) return known[code];
    return String(error?.message || "unknown error");
}

function renderCeoEditResendBanner() {
    const banner = U.ceoEditResendBanner;
    if (!banner) return;
    const active = !!S.ceoEditResend;
    banner.hidden = !active;
    if (!active) {
        banner.innerHTML = "";
        return;
    }
    banner.innerHTML = `
        <div class="ceo-edit-resend-chip">
            <i data-lucide="pencil"></i>
            <span class="ceo-edit-resend-text">正在编辑历史消息 · 发送后该消息及其后所有内容将被清空，并以新一轮重新执行</span>
            <button type="button" class="ceo-edit-resend-cancel" data-ceo-edit-resend-cancel="1" aria-label="取消编辑">
                <i data-lucide="x"></i><span>取消</span>
            </button>
        </div>
    `;
    icons();
}

function enterCeoEditResendMode(message = {}, turnId = "") {
    const sessionId = activeSessionId();
    const key = String(turnId || "").trim();
    if (!sessionId || !key) return;
    const prevDraft = captureCeoComposerDraftFromUi();
    S.ceoEditResend = { sessionId, turnId: key, prevDraft };
    if (U.ceoInput) U.ceoInput.value = String(message?.content || "");
    S.ceoUploads = normalizeUploadList(message?.attachments);
    renderPendingCeoUploads();
    renderCeoEditResendBanner();
    syncCeoInputHeight();
    syncCeoPrimaryButton();
    U.ceoInput?.focus();
}

function exitCeoEditResendMode({ restoreDraft = false } = {}) {
    const state = S.ceoEditResend;
    S.ceoEditResend = null;
    if (restoreDraft && state) {
        if (U.ceoInput) U.ceoInput.value = String(state.prevDraft?.text || "");
        S.ceoUploads = normalizeUploadList(state.prevDraft?.uploads);
        renderPendingCeoUploads();
        syncCeoInputHeight();
        syncCeoPrimaryButton();
    }
    renderCeoEditResendBanner();
}

function handleCeoEditResendClick(turnId) {
    const key = String(turnId || "").trim();
    if (!key) return;
    const busyReason = ceoHistoryEditBusyReason();
    if (busyReason) {
        showToast({ title: "当前不可编辑", text: busyReason, kind: "warn" });
        return;
    }
    if (activeSessionIsReadonly()) return;
    const sessionId = activeSessionId();
    const message = findCeoSnapshotMessageByTurnId(sessionId, key);
    if (!message) {
        showToast({ title: "无法编辑", text: "本地快照中未找到该消息，请刷新页面后重试。", kind: "warn" });
        return;
    }
    if (message?.can_edit_fork !== true) {
        // 陈旧快照兜底:服务端仍会复验,这里先行拦截给出可读提示。
        showToast({
            title: "无法编辑",
            text: "该消息当前不可编辑重发（已创建异步任务、非批次首条，或已超出最近 3 轮的可编辑窗口）。",
            kind: "warn",
            durationMs: 4200,
        });
        return;
    }
    if (S.ceoEditResend) exitCeoEditResendMode({ restoreDraft: true });
    enterCeoEditResendMode(message, key);
}

function whenCeoWsOpen(timeoutMs = 10000) {
    return new Promise((resolve, reject) => {
        const socket = S.ceoWs;
        if (socket && socket.readyState === WebSocket.OPEN) {
            resolve();
            return;
        }
        if (!socket || socket.readyState !== WebSocket.CONNECTING) {
            reject(new Error("Connection is not ready"));
            return;
        }
        if (!Array.isArray(S.ceoWsOpenWaiters)) S.ceoWsOpenWaiters = [];
        let settled = false;
        const entry = { timer: 0 };
        entry.resolve = () => {
            if (settled) return;
            settled = true;
            window.clearTimeout(entry.timer);
            resolve();
        };
        entry.reject = (reason) => {
            if (settled) return;
            settled = true;
            window.clearTimeout(entry.timer);
            reject(reason instanceof Error ? reason : new Error(String(reason || "ws_closed")));
        };
        entry.timer = window.setTimeout(
            () => entry.reject(new Error("等待连接超时")),
            Math.max(1000, Number(timeoutMs) || 10000),
        );
        S.ceoWsOpenWaiters.push(entry);
    });
}

function settleCeoWsOpenWaiters(opened) {
    if (!Array.isArray(S.ceoWsOpenWaiters) || !S.ceoWsOpenWaiters.length) return;
    const waiters = S.ceoWsOpenWaiters.splice(0, S.ceoWsOpenWaiters.length);
    for (const waiter of waiters) {
        if (opened) waiter.resolve();
        else waiter.reject(new Error("连接已关闭"));
    }
}

async function submitCeoEditResend({ text = "", uploads = [] } = {}) {
    const state = S.ceoEditResend;
    if (!state) return false;
    const sessionId = String(state.sessionId || "").trim();
    if (!sessionId || sessionId !== activeSessionId()) {
        showToast({ title: "无法编辑", text: "会话已切换，已退出编辑模式。", kind: "warn" });
        exitCeoEditResendMode({ restoreDraft: false });
        renderCeoEditResendBanner();
        return false;
    }
    const normalizedText = String(text || "");
    const normalizedUploads = normalizeUploadList(uploads);
    if (!normalizedText.trim() && !normalizedUploads.length) return false;
    const busyReason = ceoHistoryEditBusyReason();
    if (busyReason) {
        showToast({ title: "当前不可发送", text: busyReason, kind: "warn" });
        return false;
    }
    S.ceoSessionBusy = true;
    renderCeoSessions();
    syncCeoPrimaryButton();
    let truncated = false;
    const restoreComposerPayload = () => {
        if (U.ceoInput) U.ceoInput.value = normalizedText;
        S.ceoUploads = normalizedUploads;
        renderPendingCeoUploads();
        syncCeoInputHeight();
        syncCeoPrimaryButton();
    };
    try {
        // 时序:关旧 WS → REST 截断 → 清本地轮状态 → 重连 → 等 open → 既有 WS 发送链。
        closeCeoWs();
        await ApiClient.truncateCeoSession(sessionId, { turn_id: String(state.turnId || "") });
        truncated = true;
        S.ceoQueuedFollowUps = { ...(S.ceoQueuedFollowUps || {}), [sessionId]: [] };
        renderQueuedCeoFollowUps(sessionId);
        clearCeoSessionSnapshotCache(sessionId);
        exitCeoEditResendMode({ restoreDraft: false });
        if (U.ceoInput) U.ceoInput.value = "";
        S.ceoUploads = [];
        clearCeoComposerDraft(sessionId);
        syncCeoInputHeight();
        renderPendingCeoUploads();
        resetCeoSessionState({ scrollToLatest: true });
        S.ceoSessionBusy = true;
        initCeoWs();
        await whenCeoWsOpen(10000);
        const sent = sendImmediateCeoMessage({ text: normalizedText, uploads: normalizedUploads, scrollMode: "bottom" });
        if (!sent) {
            restoreComposerPayload();
            showToast({
                title: "已清空，请重新发送",
                text: "历史已截断，但新消息发送失败；内容已回填输入框，请再次点击发送。",
                kind: "warn",
                durationMs: 6000,
            });
        }
        return sent;
    } catch (e) {
        if (!truncated) {
            showToast({ title: "编辑重发失败", text: editForkErrorText(e), kind: "error", durationMs: 5200 });
            initCeoWs();
            return false;
        }
        // 截断已生效但重连/发送失败:内容回填输入框,用户可手动重发。
        restoreComposerPayload();
        initCeoWs();
        showToast({
            title: "已清空，请重新发送",
            text: "历史已截断，但新消息发送失败；内容已回填输入框，请再次点击发送。",
            kind: "warn",
            durationMs: 6000,
        });
        return false;
    } finally {
        S.ceoSessionBusy = false;
        renderCeoSessions();
        syncCeoPrimaryButton();
    }
}

async function handleCeoForkClick(turnId) {
    const key = String(turnId || "").trim();
    if (!key) return;
    const sessionId = activeSessionId();
    if (!sessionId) return;
    const busyReason = ceoHistoryEditBusyReason();
    if (busyReason) {
        showToast({ title: "当前不可 Fork", text: busyReason, kind: "warn" });
        return;
    }
    if (typeof canCreateCeoSessions === "function" && !canCreateCeoSessions()) {
        showToast({ title: "当前不可新建", text: "请先等待当前上传、暂停请求或会话切换操作完成后再 Fork。", kind: "warn" });
        return;
    }
    S.ceoSessionCatalogBusy = true;
    renderCeoSessions();
    syncCeoPrimaryButton();
    try {
        armCeoSessionUnreadExemption(sessionId);
        const payload = await ApiClient.forkCeoSession(sessionId, { turn_id: key });
        const nextActiveId = applyCeoSessionsPayload(payload);
        closeCeoWs();
        resetCeoSessionState({ scrollToLatest: true });
        const fork = payload?.fork && typeof payload.fork === "object" ? payload.fork : {};
        const newId = String(fork.session_id || nextActiveId || "").trim();
        if (newId) {
            // 被点击消息回填输入框(不自动发送);applyCeoSessionsPayload 已切好草稿上下文。
            setCeoComposerDraft(newId, {
                text: String(fork.composer?.text || ""),
                uploads: normalizeUploadList(fork.composer?.uploads),
            });
            restoreCeoComposerDraftForSession(newId);
            S.ceoSessionBusy = true;
            initCeoWs();
        }
        showToast({ title: "Fork 完成", text: "已复制到新会话，原消息已回填输入框（不会自动发送）。", kind: "success" });
    } catch (e) {
        showToast({ title: "Fork 失败", text: editForkErrorText(e), kind: "error", durationMs: 5200 });
    } finally {
        S.ceoSessionCatalogBusy = false;
        renderCeoSessions();
        syncCeoPrimaryButton();
    }
}

function ceoFeedAppendHost() {
    // 窗口分页补渲染时，渲染器写进临时容器而不是 feed 尾部；平时行为不变。
    return S.ceoFeedAppendTarget || U.ceoFeed;
}

// 首屏窗口化：全量消息可达数百条（渠道会话实测 771 条/4 万节点，整列重建
// 2-7s）；首帧只渲染尾部 N 条，向上滚动或锚点需要时再从内存数组补渲染分片。
const CEO_FEED_WINDOW_SIZE = 80;
const CEO_FEED_ANCHOR_EXPANSION_PAGES = 3;

function loadOlderCeoFeedMessages({ upToKey = "", maxPages = 1 } = {}) {
    const source = S.ceoFeedWindowSource;
    if (!source || !U || !U.ceoFeed) return false;
    if (String(S.ceoFeedRenderSessionId || "") !== String(source.sessionId || "")) return false;
    let loaded = false;
    for (let page = 0; page < Math.max(1, maxPages); page += 1) {
        if (source.start <= 0) break;
        const newStart = Math.max(0, source.start - CEO_FEED_WINDOW_SIZE);
        const host = document.createElement("div");
        S.ceoFeedAppendTarget = host;
        try {
            renderCeoSnapshotMessageRange(source.messages, source.keys, newStart, source.start, source.sessionId);
        } finally {
            S.ceoFeedAppendTarget = null;
        }
        const beforeHeight = U.ceoFeed.scrollHeight || 0;
        const anchorChild = U.ceoFeed.firstChild;
        while (host.firstChild) U.ceoFeed.insertBefore(host.firstChild, anchorChild);
        const grew = (U.ceoFeed.scrollHeight || 0) - beforeHeight;
        if (grew > 0) {
            markCeoFeedProgrammaticScroll();
            U.ceoFeed.scrollTop = Math.max(0, (Number(U.ceoFeed.scrollTop || 0) + grew));
        }
        source.start = newStart;
        S.ceoFeedRenderedMessageKeys = source.keys.slice(newStart);
        updateCeoScrollToLatestButton();
        loaded = true;
        if (!upToKey) break;
        if (ceoFeedAnchorRendered(upToKey)) break;
    }
    return loaded;
}

function ceoFeedAnchorRendered(key = "") {
    const needle = String(key || "").trim();
    if (!needle || !U || !U.ceoFeed) return false;
    const children = Array.from(U.ceoFeed.children || []);
    return children.some((child) => ceoFeedElementDataKey(child) === needle);
}

function ceoFeedNearBottom(threshold = 64) {
    if (!U.ceoFeed) return true;
    return U.ceoFeed.scrollHeight - U.ceoFeed.scrollTop - U.ceoFeed.clientHeight <= threshold;
}

// 用户「跟随最新」意图：true=贴底跟随；false=用户正在读历史，任何直播突变都不得
// 再动滚动位置。判定只认用户手势（滚轮/指针/触摸/键盘）引发的滚动——程序写
// scrollTop 触发的 scroll 事件不改变意图，这正是「上翻被反复拽回底部」的根因。
let ceoFeedUserScrollIntentAt = 0;
// 程序钉底(含 batch bottom 模式与图片异步 re-pin)派发的 scroll 绝不能重新武装
// 跟随；突变静默期之前的滚动事件一律不改意图位。
let ceoFeedProgrammaticScrollUntil = 0;
let ceoFeedLastMutationAt = 0;

function markCeoFeedProgrammaticScroll() {
    ceoFeedProgrammaticScrollUntil = Date.now() + 1000;
}

function setCeoFeedFollowLatest(on) {
    S.ceoFeedFollowLatest = !!on;
    updateCeoScrollToLatestButton();
}

function handleCeoFeedUserGesture() {
    ceoFeedUserScrollIntentAt = Date.now();
}

function handleCeoFeedScrollEvent() {
    // 接近顶部且还有未渲染的历史：从内存数组补一片（补偿滚动，视口不动）。
    if (U.ceoFeed && Number(U.ceoFeed.scrollTop || 0) < 400) {
        loadOlderCeoFeedMessages();
    }
    // 意图位只由"突变静默期之外、且非程序钉底派发"的滚动改写：
    // atBottom 分支的钉底/图片异步 re-pin 都会派发 scroll，若不隔离，
    // 手势后窗口内的程序滚动会被误判成"用户回到底部"而重新武装跟随，
    // 直播高频突变下用户永远翻不出底部。
    const now = Date.now();
    if (now < ceoFeedProgrammaticScrollUntil || now - ceoFeedLastMutationAt < 250) {
        updateCeoScrollToLatestButton();
        return;
    }
    if (ceoFeedNearBottom()) {
        setCeoFeedFollowLatest(true);
    } else if (now - ceoFeedUserScrollIntentAt < 1500) {
        setCeoFeedFollowLatest(false);
    }
    updateCeoScrollToLatestButton();
}

function updateCeoScrollToLatestButton() {
    if (!U.ceoScrollToLatestBtn) return;
    const atLatest = S.ceoFeedFollowLatest !== false && ceoFeedNearBottom();
    U.ceoScrollToLatestBtn.hidden = atLatest;
}

function scrollCeoFeedToBottom() {
    if (!U.ceoFeed) return;
    S.ceoFeedFollowLatest = true;
    ceoFeedLastMutationAt = Date.now();
    const applyBottom = () => {
        if (!U.ceoFeed) return;
        // 挂起的 rAF/图片 re-pin 迟到时用户可能已上滚：跟随位一旦脱离立即作废本次钉底。
        if (S.ceoFeedFollowLatest === false) return;
        markCeoFeedProgrammaticScroll();
        U.ceoFeed.scrollTop = U.ceoFeed.scrollHeight;
        updateCeoScrollToLatestButton();
    };
    applyBottom();
    window.requestAnimationFrame(applyBottom);

    // Lazy/async media can grow the feed after the initial scroll: attachment
    // and inline images render with no reserved space and load asynchronously,
    // and webfonts can reflow text. Without a re-apply, a freshly opened session
    // stops short of the true bottom once those settle, so re-pin the feed as
    // each pending image finishes and once fonts are ready.
    const images = Array.from(U.ceoFeed.querySelectorAll?.("img") || []);
    for (const img of images) {
        if (!img || img.complete || typeof img.addEventListener !== "function") continue;
        img.addEventListener("load", applyBottom, { once: true });
        img.addEventListener("error", applyBottom, { once: true });
    }
    if (typeof document.fonts?.ready?.then === "function") {
        document.fonts.ready.then(applyBottom).catch(() => {});
    }
}

let ceoFeedBatchDepth = 0;

// preserve 模式的滚动快照:贴底标志 + 上滚时的元素锚点 + 像素兜底。
// 旧实现是纯像素 clamp(min(prevTop, maxTop)):直播重建导致内容高度变化(展开的
// 阶段被折叠、新轮次撑高)时视口会跳变;锚点按「当前可见的 feed 子元素 + 元素内
// 偏移」还原,高度增减都稳定。用户原本贴底时视为跟随最新内容(聊天 stick-to-bottom),
// 不再被新产出的内容留在原地。
function captureCeoFeedScrollSnapshot() {
    if (!U || !U.ceoFeed) return null;
    const feed = U.ceoFeed;
    const prevTop = Math.max(0, Number(feed.scrollTop || 0));
    // 用意图位而非瞬时几何：直播突变每几百毫秒一次，用户拖动滚动条的过程中
    // 几何判定会反复误判「还在底部」并把视口钉回去（“翻许多次才翻得动”的根因）。
    const atBottom = S.ceoFeedFollowLatest !== false;
    return {
        prevTop,
        atBottom,
        anchor: atBottom ? null : ceoFeedNearScrollTop(feed, prevTop),
    };
}

function restoreCeoFeedScrollSnapshot(snapshot = null) {
    if (!U || !U.ceoFeed) return;
    const feed = U.ceoFeed;
    ceoFeedLastMutationAt = Date.now();
    if (!snapshot) {
        updateCeoScrollToLatestButton();
        return;
    }
    if (snapshot.atBottom) {
        // 直播跟随直接钉底,不走 scrollCeoFeedToBottom 的异步 re-pin:流式期间每次
        // mutation 都会重新判定,而挂起的 rAF 会在用户上滚离开后把视口拽回底部。
        markCeoFeedProgrammaticScroll();
        feed.scrollTop = feed.scrollHeight;
        updateCeoScrollToLatestButton();
        return;
    }
    const maxTop = Math.max(0, (feed.scrollHeight || 0) - (feed.clientHeight || 0));
    const anchored = ceoFeedAnchoredScrollTop(feed, snapshot.anchor);
    const nextTop = Number.isFinite(anchored) ? anchored : Number(snapshot.prevTop || 0);
    markCeoFeedProgrammaticScroll();
    feed.scrollTop = Math.max(0, Math.min(nextTop, maxTop));
    updateCeoScrollToLatestButton();
}

function withCeoFeedBatch(mutator, { scrollMode = "preserve" } = {}) {
    if (typeof mutator !== "function") return null;
    if (!U.ceoFeed) return mutator();
    // 只有最外层帧捕获/还原滚动快照;嵌套帧对 mutator 透明(与旧 prevTop 语义一致)。
    const snapshot = ceoFeedBatchDepth === 0 && scrollMode !== "bottom"
        ? captureCeoFeedScrollSnapshot()
        : null;
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
        restoreCeoFeedScrollSnapshot(snapshot);
    }
    return result;
}

function mutateCeoFeed(mutator, { scrollMode = "preserve" } = {}) {
    if (typeof mutator !== "function") return null;
    if (!U.ceoFeed) return mutator();
    if (ceoFeedBatchDepth > 0) return mutator();
    const snapshot = scrollMode === "bottom" ? null : captureCeoFeedScrollSnapshot();
    const result = mutator();
    if (scrollMode === "bottom") {
        scrollCeoFeedToBottom();
    } else {
        restoreCeoFeedScrollSnapshot(snapshot);
    }
    return result;
}

function addMsg(text, role, { markdown = false, attachments = [], scrollMode = "preserve", sessionId = activeSessionId(), timestamp = "", usage = null, turnId = "", canEditFork = false } = {}) {
    return mutateCeoFeed(() => {
        const el = document.createElement("div");
        el.className = `message ${role}`;
        const contentClass = markdown ? "msg-content markdown-content" : "msg-content";
        const content = markdown ? renderMarkdown(text) : esc(text);
        const attachmentMarkup = renderStructuredChatAttachments(attachments, { sessionId });
        // 悬停元信息行(发送/完成时间 + token 用量):仅在调用方提供数据时渲染,
        // 显隐由 CSS 的 .msg-meta 悬停规则控制。有 meta 时用 message-stack 纵向
        // 包裹,保证元信息落在气泡下方而不是 flex 行内并排。
        const metaText = buildCeoMessageMetaText({ role, timestamp, usage });
        const metaMarkup = metaText ? `<div class="msg-meta">${esc(metaText)}</div>` : "";
        const actionsMarkup = role === "user"
            ? buildCeoUserMessageActionsMarkup({ turnId, canEditFork, sessionId })
            : "";
        if (role === "user" && (attachmentMarkup || metaMarkup || actionsMarkup)) {
            const textBubble = hasRenderableText(text)
                ? `<div class="${contentClass}">${content}</div>`
                : "";
            el.innerHTML = `<div class="message-stack">${textBubble}${attachmentMarkup}${metaMarkup}${actionsMarkup}</div>`;
        } else if (metaMarkup) {
            el.innerHTML = `<div class="message-stack"><div class="${contentClass}">${content}${attachmentMarkup}</div>${metaMarkup}</div>`;
        } else {
            el.innerHTML = `<div class="${contentClass}">${content}${attachmentMarkup}</div>`;
        }
        const stamp = String(timestamp || "").trim();
        if (stamp) el.dataset.ceoTimestamp = stamp;
        ceoFeedAppendHost().appendChild(el);
        icons();
        return el;
    }, { scrollMode });
}

function ceoMessageTimestampMs(value = "") {
    const text = String(value || "").trim();
    if (!text) return NaN;
    return Date.parse(text);
}

function placeCeoUserBubbleByTimestamp(el, timestamp = "") {
    // inflight/preserved 回合携带的 user_messages 是"当前批次"而不是"刚刚发送"：批次里
    // 可能混着更早排队的消息，一律 append 会让旧提问冒到最新回复下面，读起来像用户重发。
    // 有可解析的原始时间戳时按时间落位到已渲染气泡之间；实时新输入没有时间戳，保持追加。
    const stamp = ceoMessageTimestampMs(timestamp);
    if (!Number.isFinite(stamp) || !el || !U || !U.ceoFeed || typeof U.ceoFeed.children === "undefined") return;
    const children = Array.from(U.ceoFeed.children || []);
    const anchor = children.find((child) => {
        if (child === el) return false;
        const other = ceoMessageTimestampMs(child?.dataset?.ceoTimestamp || "");
        return Number.isFinite(other) && other > stamp;
    });
    if (!anchor || anchor === el) return;
    try {
        U.ceoFeed.insertBefore(el, anchor);
    } catch (error) {
        void error;
    }
}

function defaultCeoInternalAckLabel({ source = "", reason = "" } = {}) {
    const normalizedSource = normalizeCeoTurnSource(source || "heartbeat");
    const normalizedReason = String(reason || "").trim() || "heartbeat_ok";
    return `已接收来自类型：${normalizedReason}的${normalizedSource === "cron" ? "cron" : "心跳"}`;
}

function handleCeoInternalAck(payload = {}) {
    const source = normalizeCeoTurnSource(payload?.source || "heartbeat");
    const label = String(payload?.label || "").trim() || defaultCeoInternalAckLabel({
        source,
        reason: payload?.reason || "",
    });
    if (!label || !U.ceoFeed) return null;
    return mutateCeoFeed(() => {
        const el = document.createElement("div");
        el.className = `message system ceo-internal-ack ${source === "cron" ? "is-cron" : "is-heartbeat"}`;
        el.innerHTML = `<div class="msg-content ceo-internal-ack-content">${esc(label)}</div>`;
        if (payload?.turn_id && typeof el.setAttribute === "function") {
            el.setAttribute("data-turn-id", String(payload.turn_id || "").trim());
        }
        ceoFeedAppendHost().appendChild(el);
        icons();
        return el;
    }, { scrollMode: "preserve" });
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
    const canonicalContext = normalizeCeoSnapshotCanonicalContext(snapshot.canonical_context);
    const canonicalContextDelta = normalizeCeoSnapshotCanonicalContext(snapshot?.canonical_context_delta);
    return !!assistantText || !!canonicalContext || !!canonicalContextDelta || status === "paused" || status === "error";
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

function renderCeoLiveStreamTextIntoTurn(turn) {
    if (!turn?.listEl || !turn?.flowEl) return;
    const text = ceoLiveStreamResidual(turn, turn.lastExecutionTraceSummary);
    const existing = typeof turn.listEl.querySelector === "function"
        ? turn.listEl.querySelector(".task-trace-live-text")
        : null;
    if (!text.trim()) {
        if (existing?.remove) existing.remove();
        return;
    }
    const lastStep = (() => {
        if (typeof turn.listEl.querySelectorAll !== "function") return null;
        const steps = turn.listEl.querySelectorAll(".task-trace-step");
        return steps && steps.length ? steps[steps.length - 1] : null;
    })();
    if (!lastStep) {
        // 尚无阶段（纯问答轮或建阶段前）：流式文本退回气泡展示
        if (!turn.textEl) return;
        turn.textEl.textContent = text;
        turn.textEl.classList?.add?.("pending");
        turn.textEl.classList?.remove?.("assistant-text-loading");
        turn.textEl.classList?.remove?.("markdown-content");
        if (typeof syncCeoAssistantLoadingAria === "function") syncCeoAssistantLoadingAria(turn.textEl);
        if (typeof syncCeoTurnLoadingOnlyState === "function") syncCeoTurnLoadingOnlyState(turn, false);
        return;
    }
    // 有阶段：live 文本挂进最后一个阶段体内，气泡回到 loading，避免中途文本外显
    if (turn.textEl?.classList?.contains?.("pending")) {
        renderCeoAssistantTextIntoTurn(turn, "", { status: "running" });
    }
    if (existing?.remove) existing.remove();
    const body = typeof lastStep.querySelector === "function"
        ? lastStep.querySelector(".task-trace-body")
        : null;
    if (!body || typeof body.appendChild !== "function") return;
    const block = document.createElement("div");
    block.className = "task-trace-round-text task-trace-live-text";
    block.textContent = text;
    body.appendChild(block);
    if (typeof syncCeoTurnLoadingOnlyState === "function") syncCeoTurnLoadingOnlyState(turn, false);
}

function ceoLiveStreamResidual(turn, summary) {
    let residual = String(turn?.liveStreamText || "");
    if (!residual.trim()) return "";
    // 轨道上已渲染的文本（preamble + 各 round.text）从 live 流里按
    // 出现位置删掉，剩下的才是真正还没进轨的尾巴。子串级删除对「间隙旁白、
    // 建阶段 preamble、空白符差异」都不再断链；按轨道顺序找最早出现位置，
    // 避免 echo 场景删错新的那一段。
    const absorb = (text) => {
        const normalized = String(text || "").trim();
        if (!normalized) return;
        const index = residual.indexOf(normalized);
        if (index >= 0) {
            residual = residual.slice(0, index) + residual.slice(index + normalized.length);
        }
    };
    for (const stage of summary?.stages || []) {
        absorb(stage?.preamble_text);
        for (const round of stage?.rounds || []) {
            absorb(round?.text);
        }
    }
    return residual.trim();
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

// 静默回合（模型输出 [G3KU_SILENT]）没有可见回复文本，只隐藏回复气泡本身；
// 阶段轨道与工具步骤照常保留，否则本回合的活动记录会随气泡一起消失。
function hideCeoAssistantText(turn) {
    if (!turn?.textEl) return;
    turn.textEl.hidden = true;
    turn.textEl.innerHTML = "";
    turn.textEl.classList.remove("pending");
    turn.textEl.classList.remove("assistant-text-loading");
    syncCeoAssistantLoadingAria(turn.textEl);
    syncCeoTurnLoadingOnlyState(turn, false);
}

function clearCeoReplyDeltaBuffer(sessionId = "", { turnId = "" } = {}) {
    const key = String(sessionId || "").trim();
    if (!key) return false;
    const current = S.ceoReplyDeltaBuffers && typeof S.ceoReplyDeltaBuffers === "object"
        ? S.ceoReplyDeltaBuffers[key]
        : null;
    if (!current || typeof current !== "object") return false;
    const expectedTurnId = normalizeCeoTurnId(turnId);
    const currentTurnId = normalizeCeoTurnId(current?.turnId || "");
    if (expectedTurnId && currentTurnId && expectedTurnId !== currentTurnId) return false;
    const next = { ...(S.ceoReplyDeltaBuffers || {}) };
    delete next[key];
    S.ceoReplyDeltaBuffers = next;
    if (!Object.keys(next).length && S.ceoReplyDeltaFrameId) {
        cancelAnimationFrame(S.ceoReplyDeltaFrameId);
        S.ceoReplyDeltaFrameId = 0;
    }
    return true;
}

function flushCeoReplyDeltaBuffers() {
    const buffered = S.ceoReplyDeltaBuffers && typeof S.ceoReplyDeltaBuffers === "object"
        ? { ...S.ceoReplyDeltaBuffers }
        : {};
    S.ceoReplyDeltaBuffers = {};
    S.ceoReplyDeltaFrameId = 0;
    const entries = Object.entries(buffered);
    if (!entries.length) return;
    entries.forEach(([sessionId, payload]) => {
        const normalizedSessionId = String(sessionId || "").trim();
        if (!normalizedSessionId || !payload || typeof payload !== "object") return;
        const source = normalizeCeoTurnSource(payload?.source || "user");
        const turnId = normalizeCeoTurnId(payload?.turnId || "");
        const text = String(payload?.text || "");
        if (normalizedSessionId === activeSessionId()) {
            const turn = ensureActiveCeoTurn({ source, turnId });
            if (turn?.textEl) {
                mutateCeoFeed(() => {
                    if (turnId) turn.turnId = turnId;
                    if (source) turn.source = source;
                    turn.liveStreamText = text;
                    renderCeoLiveStreamTextIntoTurn(turn);
                    icons();
                }, { scrollMode: "preserve" });
            }
        }
        patchCeoSessionSnapshotCache(normalizedSessionId, (entry) => {
            const inflightTurn = normalizeCeoSnapshotInflight(entry?.inflight_turn) || {};
            return {
                ...(entry || {}),
                inflight_turn: {
                    ...inflightTurn,
                    source: source || String(inflightTurn?.source || "").trim().toLowerCase() || "user",
                    turn_id: turnId || inflightTurn?.turn_id || "",
                    status: String(inflightTurn?.status || "running").trim().toLowerCase() || "running",
                    assistant_text: text,
                    assistant_stream_seq: Number(payload?.seq || 0),
                },
            };
        });
    });
}

function queueCeoReplyDelta(payload = {}, { sessionId = activeSessionId() } = {}) {
    const key = String(sessionId || "").trim();
    if (!key || !payload || typeof payload !== "object") return false;
    const seq = Number(payload?.seq || 0);
    const current = S.ceoReplyDeltaBuffers && typeof S.ceoReplyDeltaBuffers === "object"
        ? S.ceoReplyDeltaBuffers[key]
        : null;
    const currentSeq = Number(current?.seq || 0);
    if (current && currentSeq > seq) return false;
    S.ceoReplyDeltaBuffers = {
        ...(S.ceoReplyDeltaBuffers || {}),
        [key]: {
            sessionId: key,
            turnId: normalizeCeoTurnId(payload?.turn_id || payload?.turnId || ""),
            source: normalizeCeoTurnSource(payload?.source || "user"),
            text: String(payload?.text || ""),
            seq,
        },
    };
    if (!S.ceoReplyDeltaFrameId) {
        S.ceoReplyDeltaFrameId = requestAnimationFrame(() => flushCeoReplyDeltaBuffers());
    }
    return true;
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
    const wasFlowOpen = !!turn.flowEl.open;
    resetCeoToolFlow(turn);
    events.forEach((event) => {
        applyCeoToolEventToTurn(turn, {
            ...(event && typeof event === "object" ? event : {}),
            source: String(event?.source || normalizedSource).trim().toLowerCase() || normalizedSource,
        });
    });
    if (events.length) syncCeoTurnLoadingOnlyState(turn, false);
    if (events.length) {
        turn.flowEl.hidden = false;
        turn.flowEl.open = wasFlowOpen;
    }
    if (!events.length) updateCeoTurnMeta(turn, "等待工具开始...");
    return events.length;
}

function ceoTurnTraceRoundHostKey(host = null, hostIndex = 0) {
    // 轮次工具条还原键:按所属阶段的 traceKey 分域(round_index 跨阶段会重复);
    // round 缺 data-round-key 时用回合内 DOM 顺序兜底(与 feed 级 capture 一致)。
    const stepEl = host && typeof host.closest === "function" ? host.closest(".task-trace-step") : null;
    const scope = String(stepEl?.dataset?.traceKey || "").trim();
    const roundKey = String(host?.dataset?.roundKey
        || (host && typeof host.getAttribute === "function" ? host.getAttribute("data-round-key") : "")
        || "").trim();
    return `${scope}::${roundKey || `idx:${hostIndex}`}`;
}

function captureCeoTurnTraceViewState(turn = null) {
    // 直播重建(renderCeoStageTraceIntoTurn 整段 wipe listEl.innerHTML)前捕获回合内
    // 展开态:Interaction Flow 开合、各阶段 <details> 开合(data-trace-key)、轮次工具
    // 条选中(data-active-tool-key)。全是纯 DOM 态,重建即丢,必须在 resetCeoToolFlow
    // 之前捕获。仅对已渲染过轨道的回合生效:换新 turnId 时 patchCeoInflightTurn 会先
    // 清空 lastExecutionTraceSummary,此时不能沿用上一轮的展开态(stage id 跨轮会重复)。
    if (!turn?.listEl || typeof turn.listEl.querySelectorAll !== "function") return null;
    if (!turn.lastExecutionTraceSummary) return null;
    const state = {
        flowOpen: !!(turn.flowEl && turn.flowEl.open),
        steps: {},
        roundTools: {},
        scrolls: {},
    };
    Array.from(turn.listEl.querySelectorAll(".task-trace-step") || []).forEach((stepEl) => {
        const traceKey = String(stepEl?.dataset?.traceKey || "").trim();
        if (traceKey) state.steps[traceKey] = !!stepEl.open;
        captureCeoNestedScrollState(stepEl, traceKey, state.scrolls);
    });
    Array.from(turn.listEl.querySelectorAll(".task-trace-round-tools") || []).forEach((host, hostIndex) => {
        const active = String(host?.dataset?.activeToolKey
            || (host && typeof host.getAttribute === "function" ? host.getAttribute("data-active-tool-key") : "")
            || "").trim();
        if (active) state.roundTools[ceoTurnTraceRoundHostKey(host, hostIndex)] = active;
    });
    return state;
}

function ceoNestedScrollTargets(scopeEl = null) {
    // 阶段轨道重建只复活 <details> 开合,输出框自身的滚动位置同样需要带过去:
    // .interaction-step-detail 与 .task-trace-code 都是封顶滚动容器,DOM 换代即归零。
    if (!(scopeEl instanceof HTMLElement)) return [];
    return Array.from(scopeEl.querySelectorAll(".interaction-step-detail, .task-trace-code") || [])
        .filter((el) => el instanceof HTMLElement);
}

function captureCeoNestedScrollState(stepEl = null, traceKey = "", sink = null) {
    if (!sink || !traceKey) return;
    const counters = { d: 0, c: 0 };
    ceoNestedScrollTargets(stepEl).forEach((el) => {
        const kind = el.classList.contains("interaction-step-detail") ? "d" : "c";
        const key = `${traceKey}::${kind}::${counters[kind]}`;
        counters[kind] += 1;
        const scrollTop = Number(el.scrollTop || 0);
        if (scrollTop > 0) sink[key] = scrollTop;
    });
}

function applyCeoNestedScrollState(stepEl = null, traceKey = "", source = null) {
    if (!source || !traceKey) return;
    const counters = { d: 0, c: 0 };
    ceoNestedScrollTargets(stepEl).forEach((el) => {
        const kind = el.classList.contains("interaction-step-detail") ? "d" : "c";
        const key = `${traceKey}::${kind}::${counters[kind]}`;
        counters[kind] += 1;
        const scrollTop = Number(source[key] || 0);
        if (scrollTop > 0) setElementScrollTop(el, scrollTop);
    });
}

function applyCeoTurnTraceViewState(turn = null, viewState = null) {
    // 重建后按稳定键还原;直接赋 open 不触发 toggle 事件,阶段输出的懒加载副作用由
    // 调用方随后的 hydrateTraceOutputBlocks 遍历 + setTraceRoundActiveTool 内部的面板
    // hydration 补齐(与 applyCeoFeedViewState 行为一致)。
    if (!viewState || !turn?.listEl || typeof turn.listEl.querySelectorAll !== "function") return;
    if (turn.flowEl) turn.flowEl.open = !!viewState.flowOpen;
    Array.from(turn.listEl.querySelectorAll(".task-trace-step") || []).forEach((stepEl) => {
        const traceKey = String(stepEl?.dataset?.traceKey || "").trim();
        if (traceKey && typeof viewState.steps?.[traceKey] === "boolean") stepEl.open = viewState.steps[traceKey];
        applyCeoNestedScrollState(stepEl, traceKey, viewState.scrolls);
    });
    Array.from(turn.listEl.querySelectorAll(".task-trace-round-tools") || []).forEach((host, hostIndex) => {
        if (!(host instanceof HTMLElement)) return;
        const toolKey = viewState.roundTools?.[ceoTurnTraceRoundHostKey(host, hostIndex)] || "";
        if (toolKey && typeof setTraceRoundActiveTool === "function") setTraceRoundActiveTool(host, toolKey);
    });
}

function renderCeoStageTraceIntoTurn(turn, canonicalContext = null, { interruptedStageMarker = false } = {}) {
    if (!turn?.listEl || !turn?.flowEl) return 0;
    const summary = filterCeoInteractionFlowSummary(canonicalContext);
    if (!summary?.stages?.length) {
        // 无新增量(如阶段刚提交但轮次尚未进入增量)时保留已渲染的时间线,
        // 避免清空正在显示的新阶段/工具步骤;只有从未有内容时才显示占位
        const hasExistingTrace = !!turn?.lastExecutionTraceSummary
            || (turn?.listEl instanceof HTMLElement && turn.listEl.children.length > 0);
        if (!hasExistingTrace) {
            resetCeoToolFlow(turn);
            updateCeoTurnMeta(turn, "等待工具开始...");
            renderCeoLiveStreamTextIntoTurn(turn);
        }
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
    // 整段重建会 wipe 掉回合内全部 DOM 态,先在 reset 之前捕获用户展开态快照。
    const turnViewState = captureCeoTurnTraceViewState(turn);
    resetCeoToolFlow(turn);
    turn.el?.classList?.add?.("ceo-timeline");
    turn.listEl.classList?.add?.("task-trace-list");
    turn.listEl.innerHTML = summary.stages.map((stage, index) => {
        const isInterrupted = interruptedStageMarker && index === summary.stages.length - 1;
        const preamble = String(stage?.preamble_text || "").trim();
        const preambleHtml = preamble ? `<div class="ceo-stage-preamble">${esc(preamble)}</div>` : "";
        return renderTraceStep({
            traceKey: `ceo:stage:${stage.stage_id || stage.stage_index || index}`,
            title: `${formatExecutionStageTitle(stage)}${isInterrupted ? " · 收到补充，续跑见下" : ""}`,
            status: stageTraceStatus(stage),
            statusLabel: displayTaskStageStatus(stage.status),
            open: false,
            extraClass: isInterrupted ? "ceo-stage-interrupted" : "",
            bodyHtml: preambleHtml + renderExecutionStageRounds(stage),
        });
    }).join("");
    if (typeof bindTraceRoundToolStrips === "function") bindTraceRoundToolStrips(turn.listEl);
    if (typeof bindTraceFieldCopyActions === "function") bindTraceFieldCopyActions(turn.listEl);
    const stageCount = summary.stages.length;
    const roundCount = summary.stages.reduce((sum, stage) => sum + (Array.isArray(stage?.rounds) ? stage.rounds.length : 0), 0);
    turn.steps = roundCount || stageCount;
    turn.lastExecutionTraceSummary = summary;
    turn.flowEl.hidden = false;
    turn.flowEl.open = true;
    // 有既有轨道时按重建前捕获的快照还原用户展开态(Flow 开合、阶段 details、
    // 轮次工具条选中),覆盖上面的默认展开;必须发生在下方 [open] 懒加载遍历之前,
    // 让用户此前展开的阶段重新水合输出块。
    applyCeoTurnTraceViewState(turn, turnViewState);
    updateCeoTurnMeta(turn, `${stageCount} 个阶段 · ${roundCount} 轮工具`);
    if (typeof bindTraceOutputAutoLoad === "function") bindTraceOutputAutoLoad(turn.listEl);
    if (typeof hydrateTraceOutputBlocks === "function") {
        Array.from(turn.listEl.querySelectorAll?.(".task-trace-step[open]") || []).forEach((item) => {
            if (item instanceof HTMLElement) hydrateTraceOutputBlocks(item);
        });
    }
    renderCeoLiveStreamTextIntoTurn(turn);
    return turn.steps;
}

function patchCeoInflightTurn(snapshot = null, { sessionId = "", cacheField = "inflight_turn" } = {}) {
    if (!snapshot || typeof snapshot !== "object") {
        const targetSessionId = String(sessionId || activeSessionId()).trim();
        if (targetSessionId) setCeoSessionSnapshotCache(targetSessionId, { [cacheField]: null });
        return false;
    }
    const source = normalizeCeoTurnSource(snapshot?.source || "user");
    const turnId = normalizeCeoTurnId(snapshot?.turn_id || "");
    const targetSessionId = String(sessionId || activeSessionId()).trim();
    if (
        cacheField === "preserved_turn"
        && turnId
        && ceoAssistantTurnAlreadyPersisted(turnId, { sessionId: targetSessionId })
    ) {
        if (targetSessionId) setCeoSessionSnapshotCache(targetSessionId, { [cacheField]: null });
        return false;
    }
    const status = String(snapshot.status || "").trim().toLowerCase();
    const existingTurn = getActiveCeoTurn(source, turnId);
    const existingTurnSource = normalizeCeoTurnSource(existingTurn?.source || "user");
    const shouldReuseExistingTurn = !!existingTurn && (
        !!turnId
        || !normalizeCeoTurnId(existingTurn?.turnId || "")
        || source === "approval"
        || existingTurnSource === "approval"
    );
    if (!shouldReuseExistingTurn && !ceoNeedsAssistantTurn(snapshot)) return false;
    let turn = shouldReuseExistingTurn ? existingTurn : null;
    if (!turn) {
        turn = createPendingCeoTurn(source, { scrollMode: "preserve" });
        if (turnId && turn) turn.turnId = turnId;
        if (turn) S.ceoPendingTurns.push(turn);
    }
    if (!turn?.textEl || !turn?.flowEl) return false;
    if (turnId && turn.turnId && turnId !== turn.turnId) {
        turn.lastExecutionTraceSummary = null;
        turn.liveStreamText = "";
        // 跨 turn 复用同一回合对象时清空 sticky 元数据,避免上一轮的
        // token 用量/完成时间泄漏到新轮次的悬停信息里。
        turn.usage = null;
        turn.completedAt = "";
    }
    if (turnId) {
        turn.turnId = turnId;
        // 视图状态保持依赖回合元素的稳定 key(reconnect/快照重渲染时锚定与展开态还原)。
        if (turn.el && typeof turn.el.setAttribute === "function") {
            turn.el.setAttribute("data-ceo-key", `turn:${normalizeCeoTurnId(turnId)}`);
        }
    }
    // live inflight 只渲染本轮 delta（或本轮已渲染的 summary），不回退 full，
    // 避免新 turn 继承上一轮阶段；跨 turn 时 lastExecutionTraceSummary 已在上面被清空；
    // preserved 保留 full 回退。delta 与已渲染轨道做增量合并,保证阶段累计可见
    const preferredCanonicalContext = cacheField === "inflight_turn"
        ? mergeCeoLiveTraceContext(
            snapshot?.canonical_context_delta || null,
            turn?.lastExecutionTraceSummary || null
        )
        : resolvePreferredCeoTraceContext(
            snapshot?.canonical_context_delta || null,
            snapshot?.canonical_context || null,
            turn?.lastExecutionTraceSummary || null
        );
    mutateCeoFeed(() => {
        if (status === "running") {
            turn.liveStreamText = String(snapshot?.assistant_text || "");
            renderCeoAssistantTextIntoTurn(turn, "", { status });
        } else {
            turn.liveStreamText = "";
            renderCeoAssistantTextIntoTurn(turn, snapshot?.assistant_text || "", { status });
        }
        const stageRoundCount = renderCeoStageTraceIntoTurn(turn, preferredCanonicalContext);
        if (!stageRoundCount) {
            const timelineActive = !!turn?.lastExecutionTraceSummary
                || (turn?.listEl instanceof HTMLElement && turn.listEl.children.length > 0);
            if (status === "paused") updateCeoTurnMeta(turn, "已暂停");
            else if (status === "error") updateCeoTurnMeta(turn, "运行出错");
            else if (!timelineActive) updateCeoTurnMeta(turn, "等待工具开始...");
        }
        if (stageRoundCount) {
            turn.flowEl.hidden = false;
        }
        setCeoTurnUsage(turn, snapshot?.usage);
        setCeoTurnUsageCollapsed(turn, status !== "running");
        icons();
    }, { scrollMode: "preserve" });
    if (targetSessionId) {
        const inflightTurn = dedupeInflightUserMessageAgainstMessages(
            getCeoSessionSnapshotCache(targetSessionId)?.messages || [],
            preferredCanonicalContext
            ? {
                ...(snapshot || {}),
                canonical_context: normalizeCeoSnapshotCanonicalContext(snapshot?.canonical_context || null),
                canonical_context_delta: preferredCanonicalContext,
            }
            : snapshot
        );
        setCeoSessionSnapshotCache(targetSessionId, { [cacheField]: inflightTurn });
        if (cacheField === "inflight_turn") {
            promoteRepresentedRuntimeSentCeoFollowUps(
                targetSessionId,
                normalizeCeoSnapshotUserMessages(inflightTurn?.user_messages, inflightTurn?.user_message),
                { scrollMode: "preserve", insertBefore: turn?.el || null }
            );
        }
    }
    return true;
}

function restoreCeoInflightTurn(snapshot = null, { sessionId = "", cacheField = "inflight_turn" } = {}) {
    if (!snapshot || typeof snapshot !== "object") return;
    const source = normalizeCeoTurnSource(snapshot?.source || "");
    const turnId = normalizeCeoTurnId(snapshot?.turn_id || "");
    const isHeartbeat = source === "heartbeat";
    const userMessages = normalizeCeoSnapshotUserMessages(snapshot?.user_messages, snapshot?.user_message);
    if (userMessages.length && !isHeartbeat) {
        userMessages.forEach((userMessage) => {
            const attachments = normalizeUploadList(userMessage.attachments);
            const timestamp = String(userMessage.timestamp || "");
            const el = addCeoUserMessage(String(userMessage.content || ""), {
                attachments,
                scrollMode: "preserve",
                sessionId,
                timestamp,
            });
            placeCeoUserBubbleByTimestamp(el, timestamp);
        });
    } else {
        const userMessage = snapshot.user_message && typeof snapshot.user_message === "object" ? snapshot.user_message : null;
        if (!userMessage || isHeartbeat) {
            patchCeoInflightTurn(snapshot, { sessionId, cacheField });
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
            return;
        }
        const attachments = normalizeUploadList(userMessage.attachments);
        const timestamp = String(userMessage.timestamp || "");
        const el = addCeoUserMessage(String(userMessage.content || ""), {
            attachments,
            scrollMode: "preserve",
            sessionId,
            timestamp,
        });
        placeCeoUserBubbleByTimestamp(el, timestamp);
    }
    patchCeoInflightTurn(snapshot, { sessionId, cacheField });
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

function isCeoApprovalPauseSource(source = "") {
    return normalizeCeoTurnSource(source) === "approval";
}

function holdApprovalPausedCeoTurn({ source = "", turnId = "", text = "" } = {}) {
    const normalizedSource = normalizeCeoTurnSource(source || "approval");
    const normalizedTurnId = normalizeCeoTurnId(turnId);
    const turn = ensureActiveCeoTurn({ source: normalizedSource, turnId: normalizedTurnId });
    if (!turn?.textEl || !turn?.flowEl) return false;
    mutateCeoFeed(() => {
        renderCeoAssistantTextIntoTurn(turn, String(text || ""), { status: "paused" });
        turn.finalized = false;
        if (turn.steps > 0) {
            turn.flowEl.hidden = false;
            turn.flowEl.open = true;
        }
        updateCeoTurnMeta(turn, "等待审批");
        icons();
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

const __approvalPauseBaseApplyCeoState = applyCeoState;
applyCeoState = function approvalAwareApplyCeoState(state = {}, meta = {}) {
    const status = String(state?.status || "").trim().toLowerCase();
    const source = String(meta?.source || state?.source || "").trim().toLowerCase();
    if (!(status === "paused" && shouldTreatCeoPauseAsApproval(state, { source }))) {
        return __approvalPauseBaseApplyCeoState.call(this, state, meta);
    }
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
        } else if (hadTurnContext && (source || turnId) && source !== "heartbeat") {
            ensureActiveCeoTurn({ source, turnId });
        }
    }
    if (paused) holdApprovalPausedCeoTurn({ source, turnId });
    scheduleSyncCeoComposerUsageOutline();
    syncCeoSessionActions();
    syncCeoPrimaryButton();
    if (!running && !paused) {
        pruneRuntimeSentCeoFollowUps(activeSessionId());
        maybeDispatchQueuedCeoFollowUps();
    }
};

const __approvalPauseBaseRestoreCeoInflightTurn = restoreCeoInflightTurn;
restoreCeoInflightTurn = function approvalAwareRestoreCeoInflightTurn(
    snapshot = null,
    { sessionId = "", cacheField = "inflight_turn" } = {},
) {
    const status = String(snapshot?.status || "").trim().toLowerCase();
    const source = normalizeCeoTurnSource(snapshot?.source || "");
    if (!(status === "paused" && shouldTreatCeoPauseAsApproval(snapshot, { source, sessionId }))) {
        return __approvalPauseBaseRestoreCeoInflightTurn.call(this, snapshot, { sessionId, cacheField });
    }
    patchCeoInflightTurn(snapshot, { sessionId, cacheField });
    holdApprovalPausedCeoTurn({
        source,
        turnId: snapshot?.turn_id || "",
        text: snapshot?.assistant_text || "",
    });
};

function renderPersistedCeoAssistantTurn(item = {}) {
    const hasDelta = item?.canonical_context_delta !== null && item?.canonical_context_delta !== undefined;
    const canonicalContext = hasDelta
        ? normalizeCeoSnapshotCanonicalContext(item.canonical_context_delta)
        : (normalizeCeoSnapshotCanonicalContext(item.canonical_context) || null);
    const content = String(item?.content || "");
    const silentReply = item?.silent_reply === true;
    const status = String(item?.status || "").trim().toLowerCase();
    // follow-up 归档半截回合(后端 archive turn_id = `{原 turn_id}:followup:{随机}`)：
    // 其最后一个阶段在收到补充消息时被拦腰打断,需要打上打断标记。
    const isFollowUpArchive = String(item?.turn_id || "").includes(":followup:");
    const historyTimestamp = String(item?.timestamp || "").trim();
    const historyUsage = item?.usage || null;
    if (status !== "paused" && !canonicalContext) {
        // 静默回合没有任何可展示内容时不出气泡。
        if (silentReply) return;
        // 无轨道兜底气泡同样携带悬停元信息(完成时间 + token 用量)。
        addMsg(content, "system", { markdown: true, scrollMode: "preserve", timestamp: historyTimestamp, usage: historyUsage });
        return;
    }
    const turn = createPendingCeoTurn("history", { scrollMode: "preserve" });
    if (!turn) {
        if (!silentReply) {
            addMsg(content, "system", { markdown: true, scrollMode: "preserve", timestamp: historyTimestamp, usage: historyUsage });
        }
        return;
    }
    const historyTurnId = normalizeCeoTurnId(item?.turn_id || "");
    if (historyTurnId && turn.el && typeof turn.el.setAttribute === "function") {
        turn.el.setAttribute("data-ceo-key", `turn:${historyTurnId}`);
    }
    S.ceoPendingTurns.push(turn);
    withCeoFeedBatch(() => {
        if (silentReply) hideCeoAssistantText(turn);
        else renderCeoAssistantTextIntoTurn(turn, content || (status === "paused" ? "已暂停" : ""), { status });
        renderCeoStageTraceIntoTurn(turn, canonicalContext, { interruptedStageMarker: isFollowUpArchive });
        turn.flowEl.hidden = false;
        turn.flowEl.open = true;
        setCeoTurnUsage(turn, historyUsage, { completedAt: historyTimestamp });
        setCeoTurnUsageCollapsed(turn, true);
        icons();
    }, { scrollMode: "preserve" });
    if (status === "paused") {
        finalizePausedCeoTurn(content || "已暂停", { source: "history" });
        return;
    }
    // meta 带上 usage/timestamp:随后的 finalizeCeoTurn 写缓存与 usage 行时
    // 用历史值而非当前时间,保证刷新后完成时间稳定。
    finalizeCeoTurn(content, { source: "history", usage: historyUsage, timestamp: historyTimestamp, silent_reply: silentReply });
}

// ---- CEO 会话视图状态保持 -----------------------------------------------------
// 整页重建(renderCeoSnapshot → resetCeoFeed)会丢三样东西:展开的阶段/轮次工具/全部历史、
// 以及滚动锚点。重建前从 DOM 捕获、重建后按稳定 key 还原,key 失效时回退为像素 clamp。
// 身份 key:消息/回合元素上的 data-ceo-key;未打 key 的元素用"从底部计数"的位置兜底。
// 阶段 step 用 data-trace-key,但 stage_index 兜底会在不同回合间撞车,所以按回合身份分域。

function ceoFeedElementDataKey(el = null) {
    return (el && el.dataset && typeof el.dataset.ceoKey === "string") ? String(el.dataset.ceoKey || "") : "";
}

function ceoFeedTurnIdentity(turnEl = null, endIndex = 0) {
    const dataKey = ceoFeedElementDataKey(turnEl);
    return dataKey ? `k:${dataKey}` : `e:${endIndex}`;
}

function ceoFeedNearScrollTop(feed, scrollTop) {
    // 找出当前滚动位置所锚定的第一个子元素:内容顶+高度压过 scrollTop 即命中。
    const children = Array.from((feed && feed.children) || []);
    if (!children.length || typeof feed.getBoundingClientRect !== "function") return null;
    const feedRect = feed.getBoundingClientRect();
    for (let index = 0; index < children.length; index += 1) {
        const child = children[index];
        if (!child || typeof child.getBoundingClientRect !== "function") continue;
        const rect = child.getBoundingClientRect();
        const contentTop = rect.top - feedRect.top + (feed.scrollTop || 0);
        const height = Math.max(1, Number(rect.height || 1));
        if (contentTop + height > scrollTop) {
            const dataKey = ceoFeedElementDataKey(child);
            return dataKey
                ? { key: dataKey, offsetInElement: Math.max(0, scrollTop - contentTop) }
                : { endIndex: children.length - 1 - index, offsetInElement: Math.max(0, scrollTop - contentTop) };
        }
    }
    return null;
}

function captureCeoFeedViewState(sessionId = "") {
    const key = String(sessionId || "").trim();
    if (!key) return null;
    // 跨会话保护:feed 当前渲染的会话与 target 不一致时(切换会话的瞬间)不捕获,避免把
    // 上一个会话的展开态/锚点套到新会话上。
    if (String(S.ceoFeedRenderSessionId || "") !== key) return null;
    if (!U || !U.ceoFeed || typeof U.ceoFeed.querySelectorAll !== "function") return null;
    const feed = U.ceoFeed;
    const state = {
        sessionId: key,
        atBottom: S.ceoFeedFollowLatest !== false,
        prevTop: Math.max(0, Number(feed.scrollTop || 0)),
        turnFlows: {},
        steps: {},
        roundTools: {},
        anchor: null,
    };
    state.anchor = ceoFeedNearScrollTop(feed, state.prevTop);
    const turns = Array.from(feed.querySelectorAll(".ceo-turn-message") || []);
    turns.forEach((turnEl, index) => {
        if (!turnEl) return;
        const identity = ceoFeedTurnIdentity(turnEl, turns.length - 1 - index);
        const flowEl = turnEl.querySelector(".interaction-flow");
        const toggleEl = turnEl.querySelector(".interaction-flow-toggle");
        state.turnFlows[identity] = {
            flowOpen: !!(flowEl && flowEl.open),
            historyExpanded: !!(toggleEl && typeof toggleEl.getAttribute === "function" && toggleEl.getAttribute("aria-expanded") === "true"),
        };
        Array.from(turnEl.querySelectorAll(".task-trace-step") || []).forEach((stepEl) => {
            if (!stepEl?.dataset || typeof stepEl.dataset.traceKey !== "string") return;
            const traceKey = String(stepEl.dataset.traceKey || "").trim();
            if (traceKey) state.steps[`${identity}::${traceKey}`] = !!stepEl.open;
        });
        // activeToolKey 按回合内 DOM 顺序分域,规避 roundKey 为空时跨轮次 tool key 撞车。
        Array.from(turnEl.querySelectorAll(".task-trace-round-tools") || []).forEach((host, hostIndex) => {
            if (!(host instanceof HTMLElement)) {
                const attr = host && typeof host.getAttribute === "function" ? host.getAttribute("data-active-tool-key") : "";
                const active = String(attr || "").trim();
                if (active) state.roundTools[`${identity}::${hostIndex}`] = active;
                return;
            }
            const active = String(host.dataset?.activeToolKey || host.getAttribute?.("data-active-tool-key") || "").trim();
            if (active) state.roundTools[`${identity}::${hostIndex}`] = active;
        });
    });
    return state;
}

function applyCeoFeedViewState(viewState = null) {
    if (!viewState || typeof viewState !== "object") return;
    if (!U || !U.ceoFeed || typeof U.ceoFeed.querySelectorAll !== "function") return;
    if (String(viewState.sessionId || "").trim() !== String(activeSessionId() || "").trim()) return;
    const feed = U.ceoFeed;
    const turns = Array.from(feed.querySelectorAll(".ceo-turn-message") || []);
    turns.forEach((turnEl, index) => {
        if (!turnEl) return;
        const identity = ceoFeedTurnIdentity(turnEl, turns.length - 1 - index);
        const flowState = viewState.turnFlows && viewState.turnFlows[identity];
        if (flowState) {
            const flowEl = turnEl.querySelector(".interaction-flow");
            if (flowEl && typeof flowState.flowOpen === "boolean") flowEl.open = flowState.flowOpen;
            if (flowState.historyExpanded) {
                const footerEl = turnEl.querySelector(".interaction-flow-footer");
                if (footerEl && "hidden" in footerEl) footerEl.hidden = false;
                const toggleEl = turnEl.querySelector(".interaction-flow-toggle");
                if (toggleEl) {
                    if ("textContent" in toggleEl) toggleEl.textContent = "收起旧进度";
                    if (typeof toggleEl.setAttribute === "function") toggleEl.setAttribute("aria-expanded", "true");
                }
                Array.from(turnEl.querySelectorAll(".interaction-step") || []).forEach((item) => {
                    if (item && "hidden" in item) item.hidden = false;
                    if (item?.classList && typeof item.classList.remove === "function") item.classList.remove("is-collapsed-history");
                });
            }
        }
        Array.from(turnEl.querySelectorAll(".task-trace-step") || []).forEach((stepEl) => {
            if (!stepEl?.dataset || typeof stepEl.dataset.traceKey !== "string") return;
            const traceKey = String(stepEl.dataset.traceKey || "").trim();
            const captured = traceKey && viewState.steps ? viewState.steps[`${identity}::${traceKey}`] : undefined;
            if (typeof captured === "boolean") stepEl.open = captured;
        });
        Array.from(turnEl.querySelectorAll(".task-trace-round-tools") || []).forEach((host, hostIndex) => {
            if (!(host instanceof HTMLElement)) return;
            const toolKey = viewState.roundTools ? viewState.roundTools[`${identity}::${hostIndex}`] : "";
            if (toolKey && typeof setTraceRoundActiveTool === "function") setTraceRoundActiveTool(host, toolKey);
        });
    });
    // 直接赋 open 属性不触发 toggle 事件,补一次懒加载副作用,与任务详情视图还原行为一致。
    Array.from(feed.querySelectorAll(".task-trace-step") || []).forEach((stepEl) => {
        if (stepEl?.open && typeof hydrateTraceOutputBlocks === "function") hydrateTraceOutputBlocks(stepEl);
    });
    restoreCeoFeedScroll(viewState);
}

function ceoFeedAnchoredScrollTop(feed, anchor = null) {
    if (!anchor || !feed || typeof feed.getBoundingClientRect !== "function") return null;
    const children = Array.from(feed.children || []);
    let el = null;
    if (anchor.key) el = children.find((child) => ceoFeedElementDataKey(child) === anchor.key) || null;
    if (!el && Number.isInteger(anchor.endIndex)) {
        el = children[children.length - 1 - anchor.endIndex] || null;
    }
    if (!el || typeof el.getBoundingClientRect !== "function") return null;
    const elRect = el.getBoundingClientRect();
    const feedRect = feed.getBoundingClientRect();
    // 元素内容顶坐标 = rect.top - feedRect.top + scrollTop;还原时按当前 rect 重新计算,
    // 上方内容高度变化不会把锚点漂走。
    return Math.max(0, elRect.top - feedRect.top + (feed.scrollTop || 0) + (anchor.offsetInElement || 0));
}

function restoreCeoFeedScroll(viewState = null) {
    if (!viewState || !U || !U.ceoFeed) return;
    ceoFeedLastMutationAt = Date.now();
    if (viewState.atBottom) {
        scrollCeoFeedToBottom();
        return;
    }
    const applyAnchored = () => {
        if (!U || !U.ceoFeed) return;
        const feed = U.ceoFeed;
        const maxTop = Math.max(0, (feed.scrollHeight || 0) - (feed.clientHeight || 0));
        const anchored = ceoFeedAnchoredScrollTop(feed, viewState.anchor);
        const nextTop = Number.isFinite(anchored) ? anchored : Number(viewState.prevTop || 0);
        markCeoFeedProgrammaticScroll();
        feed.scrollTop = Math.max(0, Math.min(nextTop, maxTop));
        updateCeoScrollToLatestButton();
    };
    applyAnchored();
    // 异步资源(图片/输出块)稍后撑开高度会改变几何,双重 rAF 再校正两次,与任务详情视图一致。
    const raf = typeof requestAnimationFrame === "function" ? requestAnimationFrame : (fn) => fn();
    raf(() => {
        applyAnchored();
        raf(applyAnchored);
    });
}

function buildCeoMessageKeyList(messages = []) {
    // 与 renderCeoSnapshot 的消息打 key 规则完全一致:m:{turn_id|-}:{role}:{出现次序}。
    const counters = {};
    return (Array.isArray(messages) ? messages : []).map((item) => {
        if (!item || typeof item !== "object") return "";
        const role = String(item.role || "").trim().toLowerCase();
        const base = String(item.turn_id || "-").trim() || "-";
        const counterKey = `${base}:${role}`;
        const occ = Number(counters[counterKey] || 0);
        counters[counterKey] = occ + 1;
        return `m:${base}:${role}:${occ}`;
    });
}

function buildCeoRenderSignature(messages = [], inflightTurn = null, preservedTurn = null) {
    // 快照/重建的内容签名:相同签名 = 同一渲染输入,重建可以安全跳过。
    // 签名覆盖完整渲染面(messages 内容 + live turn 关键字段),漏字段会导致陈旧渲染。
    const projectMessage = (item) => {
        if (!item || typeof item !== "object") return null;
        return [
            String(item.role || "").trim().toLowerCase(),
            String(item.turn_id || "").trim(),
            String(item.status || "").trim().toLowerCase(),
            item.canonical_context ? 1 : 0,
            item.canonical_context_delta ? 1 : 0,
            String(item.content || ""),
            // usage/timestamp 参与签名:缓存先行渲染(可能缺元数据)后到达的
            // 服务端权威快照必须触发重建,否则悬停元信息永远停留在缺失状态。
            item.usage && typeof item.usage === "object" ? JSON.stringify(item.usage) : "",
            String(item.timestamp || ""),
            // 编辑/Fork 按钮标志参与签名:flag 迟到(缓存渲染无 flag、权威快照
            // 有 flag,或任务派发后 flag 收回)必须触发重建。
            item.can_edit_fork === true ? 1 : 0,
            item.task_dispatched === true ? 1 : 0,
        ];
    };
    const projectTrace = (context) => {
        // 阶段轨道参与签名:live 回合只渲染 canonical_context_delta,而该增量在
        // assistant_text/usage 未变时也会长出新的阶段与工具轮。开关失效就会让
        // 切回会话时的缓存先行渲染占住签名,把携带新阶段的权威快照整份跳过。
        // 只投影会改像素的骨架字段(阶段/状态/工具名与状态/输出长度),不搬输出正文。
        const stages = Array.isArray(context?.stages) ? context.stages : [];
        if (!stages.length) return null;
        return stages.map((stage) => [
            String(stage?.stage_id || "").trim() || String(stage?.stage_index ?? ""),
            String(stage?.status || "").trim().toLowerCase(),
            (Array.isArray(stage?.rounds) ? stage.rounds : []).map((round) => (
                (Array.isArray(round?.tools) ? round.tools : []).map((step) => [
                    String(step?.tool_name || "").trim(),
                    String(step?.status || "").trim().toLowerCase(),
                    String(step?.output_text || "").length,
                ])
            )),
        ]);
    };
    const projectTurn = (snapshot) => {
        if (!snapshot || typeof snapshot !== "object") return null;
        const retryStatus = snapshot.model_retry_status && typeof snapshot.model_retry_status === "object"
            ? snapshot.model_retry_status
            : null;
        return [
            String(snapshot.source || "").trim().toLowerCase(),
            String(snapshot.turn_id || "").trim(),
            String(snapshot.status || "").trim().toLowerCase(),
            String(snapshot.assistant_text || "").slice(0, 20000),
            retryStatus ? Number(retryStatus.retry_count || 0) : -1,
            retryStatus ? String(retryStatus.state || "") : "",
            snapshot.usage && typeof snapshot.usage === "object" ? JSON.stringify(snapshot.usage) : "",
            projectTrace(snapshot.canonical_context_delta || snapshot.canonical_context || null),
        ];
    };
    try {
        return JSON.stringify({
            messages: (Array.isArray(messages) ? messages : []).map(projectMessage),
            inflight: projectTurn(inflightTurn),
            preserved: projectTurn(preservedTurn),
        });
    } catch (error) {
        void error;
        return "";
    }
}

function ceoFeedMatchesIncrementalFinalize(messageKeys = [], turn = null) {
    // 增量 finalize 的前置契约:DOM 与记录的消息 key 完全一致,且最后一个
    // 子元素就是待收尾的回合元素。任何分歧(promote 过 follow-up、手动发送、
    // 多回合并存)都回退全量重建,保留旧路径的权威对齐语义。
    if (!U || !U.ceoFeed || !turn || !turn.el) return false;
    const children = Array.from(U.ceoFeed.children || []);
    if (!children.length || children[children.length - 1] !== turn.el) return false;
    if (children.length !== messageKeys.length + 1) return false;
    for (let index = 0; index < messageKeys.length; index += 1) {
        if (ceoFeedElementDataKey(children[index]) !== messageKeys[index]) return false;
    }
    return true;
}

function buildFinalizedCeoTurnPayload(sessionId, { normalizedSource = "", normalizedTurnId = "", finalUserMessages = [], finalTraceContext = null, finalCanonicalContext = null, text = "", meta = null, completedAt = "" } = {}) {
    // finalize 三个旧分支的缓存写入统一为一次计算:消息列表与 inflight 清空只在此处构建。
    const entry = getCeoSessionSnapshotCache(sessionId);
    const inflightTurn = normalizeCeoSnapshotInflight(entry?.inflight_turn);
    const inflightTurnId = normalizeCeoTurnId(inflightTurn?.turn_id);
    const inflightSource = normalizeCeoTurnSource(inflightTurn?.source || "user");
    const inflightMatchesSource = !inflightTurn
        || (normalizedTurnId && inflightTurnId && inflightTurnId !== normalizedTurnId ? false : true)
        || !String(inflightTurn?.source || "").trim()
        || ceoTurnSourceMatches(normalizedSource, inflightSource);
    const persistedCanonicalContext = finalTraceContext
        ? (finalCanonicalContext || finalTraceContext)
        : null;
    const userMessagesToAppend = finalUserMessages.length
        ? finalUserMessages
        : (inflightMatchesSource
            ? normalizeCeoSnapshotUserMessages(inflightTurn?.user_messages, inflightTurn?.user_message)
            : []);
    let messages = trimCeoSessionSnapshotMessages(entry?.messages);
    messages = appendMissingCeoUserMessages(messages, userMessagesToAppend);
    // usage/timestamp 无条件写入缓存:会话切换/刷新后从缓存渲染时,
    // 悬停元信息(token 用量 + 完成时间)不再依赖服务端快照补齐。
    const completedTimestamp = String(completedAt || "").trim();
    const silentReply = meta?.silent_reply === true;
    messages = appendCeoSessionSnapshotMessage(messages, {
        role: "assistant",
        content: silentReply ? "" : (String(text || "").trim() || "Done."),
        ...(silentReply ? { silent_reply: true } : {}),
        canonical_context: persistedCanonicalContext,
        canonical_context_delta: finalTraceContext,
        usage: meta?.usage || null,
        ...(completedTimestamp ? { timestamp: completedTimestamp } : {}),
    });
    return { messages, inflight_turn: inflightMatchesSource ? null : inflightTurn };
}

function renderCeoSnapshotMessageRange(messages, keys, fromIndex, toIndex, targetSessionId) {
    // 渲染 messages[fromIndex,toIndex) 到 ceoFeedAppendHost()；首屏窗口与
    // 分页补渲染共用。key 由 buildCeoMessageKeyList 全局算好传入，occ 计数
    // 与整列一致，窗口起点无关。
    for (let index = fromIndex; index < toIndex; index += 1) {
        const item = messages[index];
        const role = String(item?.role || "").trim().toLowerCase();
        const content = String(item?.content || "");
        const attachments = normalizeUploadList(item?.attachments);
        const tagRendered = () => {
            const host = ceoFeedAppendHost();
            const child = host?.lastElementChild || (host?.children || [])[ (host?.children || []).length - 1 ] || null;
            const key = String(keys[index] || "");
            if (!child || !key || typeof child.setAttribute !== "function") return;
            child.setAttribute("data-ceo-key", key);
            if (child.dataset && typeof child.dataset.ceoKey !== "string") {
                try { child.dataset.ceoKey = key; } catch (error) { void error; }
            }
        };
        if (role === "user") {
            if (!content.trim() && !attachments.length) continue;
            addCeoUserMessage(content, {
                attachments,
                scrollMode: "preserve",
                sessionId: targetSessionId,
                timestamp: String(item?.timestamp || ""),
                turnId: String(item?.turn_id || ""),
                canEditFork: item?.can_edit_fork === true,
            });
            tagRendered();
            continue;
        }
        if (role === "assistant") {
            renderPersistedCeoAssistantTurn(item);
            tagRendered();
            continue;
        }
        if (role === "system" && content.trim()) {
            const marker = normalizeCeoSnapshotCompressionMarker(item?.compression_marker);
            if (marker) {
                appendCeoCompressionDivider(marker.state, { interactive: false });
            } else {
                addMsg(content, "system", { markdown: true, scrollMode: "preserve" });
            }
            tagRendered();
        }
    }
}

function renderCeoSnapshot(messages = [], inflightTurn = null, { sessionId = "", preservedTurn = null } = {}) {
    const shouldScrollToLatest = !!S.ceoScrollToLatestOnSnapshot;
    S.ceoScrollToLatestOnSnapshot = false;
    const targetSessionId = String(sessionId || activeSessionId()).trim();
    // 跨会话渲染或显式"回到最新"：回到贴底跟随态，再走各自的滚动还原。
    if (shouldScrollToLatest || String(S.ceoFeedRenderSessionId || "") !== targetSessionId) {
        S.ceoFeedFollowLatest = true;
    }
    const normalizedPreservedTurn = (
        preservedTurn
        && ceoAssistantTurnAlreadyPersisted(preservedTurn?.turn_id || "", { messages, sessionId: targetSessionId })
    ) ? null : preservedTurn;
    const normalizedInflightTurn = dedupeInflightUserMessageAgainstMessages(messages, inflightTurn);
    const renderSignature = buildCeoRenderSignature(
        messages,
        normalizedInflightTurn,
        normalizedPreservedTurn
    );
    // 同一会话收到与当前渲染完全相同的快照(重连引导重复推送等)时跳过整页重建,
    // 滚动与展开位置零抖动。初次加载(scrollToLatest)与跨会话渲染不跳过。
    if (
        !shouldScrollToLatest
        && renderSignature
        && String(S.ceoFeedRenderSessionId || "") === targetSessionId
        && S.ceoFeedRenderSignature === renderSignature
    ) {
        S.ceoScrollToLatestOnSnapshot = false;
        return;
    }
    // 重建前捕获用户视觉状态(展开项/锚点);跨会话时内部自动跳过。
    const viewState = captureCeoFeedViewState(targetSessionId);
    hideCeoContextLoadNotice();
    const messageKeys = buildCeoMessageKeyList(messages);
    const windowStart = Math.max(0, (Array.isArray(messages) ? messages.length : 0) - CEO_FEED_WINDOW_SIZE);
    withCeoFeedBatch(() => {
        resetCeoFeed();
        renderCeoSnapshotMessageRange(messages, messageKeys, windowStart, (Array.isArray(messages) ? messages.length : 0), targetSessionId);
        restoreCeoInflightTurn(
            dedupeInflightUserMessageAgainstMessages(messages, normalizedPreservedTurn),
            { sessionId: targetSessionId, cacheField: "preserved_turn" }
        );
        restoreCeoInflightTurn(
            normalizedInflightTurn,
            { sessionId: targetSessionId, cacheField: "inflight_turn" }
        );
        const representedMessages = [
            ...normalizeCeoSnapshotUserMessages(messages),
            ...normalizeCeoSnapshotUserMessages(
                normalizedInflightTurn?.user_messages,
                normalizedInflightTurn?.user_message
            ),
        ];
        consumeRepresentedRuntimeSentCeoFollowUps(targetSessionId, representedMessages);
        if (targetSessionId) {
            setCeoSessionSnapshotCache(targetSessionId, {
                messages,
                inflight_turn: normalizedInflightTurn,
                preserved_turn: normalizedPreservedTurn,
            });
        }
        S.ceoFeedRenderSessionId = targetSessionId;
        S.ceoFeedRenderSignature = renderSignature;
        S.ceoFeedRenderedMessageKeys = messageKeys.slice(windowStart);
        S.ceoFeedWindowSource = {
            sessionId: targetSessionId,
            messages: Array.isArray(messages) ? messages : [],
            keys: messageKeys,
            start: windowStart,
        };
    }, {
        scrollMode: shouldScrollToLatest ? "bottom" : "preserve",
    });
    // 阅读位置锚点在首屏窗口之外：先补渲染历史分片（有页数上限），再精校。
    if (viewState?.anchor?.key && !ceoFeedAnchorRendered(viewState.anchor.key)) {
        loadOlderCeoFeedMessages({ upToKey: viewState.anchor.key, maxPages: CEO_FEED_ANCHOR_EXPANSION_PAGES });
    }
    // 「压缩中」那条实时线不属于转录数据，resetCeoFeed 每次重建都会把它抹掉：
    // 刷新页面时服务端先推 ceo.state（认领进行中、挂线）再推 snapshot.ceo（整页重建），
    // 不在这里重挂就会稳定地看不见。必须在滚动精校之前，补进去的那一行要参与锚定。
    syncCeoCompressionDivider();
    // 批次内的像素 clamp 之后再按捕获状态精校(锚定滚动/展开项)。
    applyCeoFeedViewState(viewState);
}

function createPendingCeoTurn(source = "user", { scrollMode = "preserve" } = {}) {
    return mutateCeoFeed(() => {
        if (!U.ceoFeed) return null;
        const el = document.createElement("div");
        el.className = "message system ceo-turn-message ceo-turn-loading-only";
        el.innerHTML = `
            <div class="msg-content ceo-turn-content">
                <div class="assistant-text pending">${renderCeoAssistantLoadingMarkup()}</div>
                <div class="ceo-turn-usage" hidden></div>
                <details class="interaction-flow" hidden>
                    <summary class="interaction-flow-summary">
                        <span class="interaction-flow-title">Interaction Flow</span>
                        <span class="interaction-flow-meta">等待工具开始...</span>
                    </summary>
                    <div class="interaction-flow-list" role="list"></div>
                    <div class="interaction-flow-footer" hidden>
                        <button type="button" class="interaction-flow-toggle">展开全部</button>
                    </div>
                </details>
                <div class="ceo-tool-reminder" hidden></div>
            </div>
        `;
        ceoFeedAppendHost().appendChild(el);
        const toggleButton = el.querySelector(".interaction-flow-toggle");
        const turn = {
            el,
            textEl: el.querySelector(".assistant-text"),
            usageEl: el.querySelector(".ceo-turn-usage"),
            flowEl: el.querySelector(".interaction-flow"),
            metaEl: el.querySelector(".interaction-flow-meta"),
            listEl: el.querySelector(".interaction-flow-list"),
            footerEl: el.querySelector(".interaction-flow-footer"),
            toggleEl: toggleButton,
            reminderEl: el.querySelector(".ceo-tool-reminder"),
            steps: 0,
            hasError: false,
            finalized: false,
            historyExpanded: false,
            lastExecutionTraceSummary: null,
            liveStreamText: "",
            // 悬停元数据(sticky):token 用量与完成时间,由 setCeoTurnUsage 维护。
            usage: null,
            completedAt: "",
            contextLoadNoticeKeys: new Set(),
            turnId: "",
            reminderExecutionId: "",
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

function isCeoApprovalInterruptValue(value = null) {
    const kind = String(value?.kind || "").trim();
    return kind === "frontdoor_tool_approval" || kind === "frontdoor_tool_approval_batch";
}

function ceoSnapshotHasApprovalInterrupts(snapshot = null) {
    if (!snapshot || typeof snapshot !== "object") return false;
    return normalizeCeoSnapshotInterrupts(snapshot?.interrupts)
        .some((item) => isCeoApprovalInterruptValue(item?.value || null));
}

function ceoSessionHasActiveApprovalBlockingState(sessionId = activeSessionId()) {
    try {
        return typeof hasActiveCeoApprovalBlockingState === "function"
            && !!hasActiveCeoApprovalBlockingState(sessionId);
    } catch (error) {
        void error;
        return false;
    }
}

function shouldTreatCeoPauseAsApproval(stateOrSnapshot = null, { source = "", sessionId = "" } = {}) {
    const normalizedSource = normalizeCeoTurnSource(source || stateOrSnapshot?.source || "");
    if (normalizedSource === "approval") return true;
    if (ceoSnapshotHasApprovalInterrupts(stateOrSnapshot)) return true;
    const normalizedSessionId = String(sessionId || activeSessionId()).trim();
    return !!normalizedSessionId && ceoSessionHasActiveApprovalBlockingState(normalizedSessionId);
}

function ceoTurnSourceMatches(expectedSource = "", actualSource = "") {
    const expected = normalizeCeoTurnSource(expectedSource);
    const actual = normalizeCeoTurnSource(actualSource);
    if (expected === actual) return true;
    const sameVisibleUserLane = (expected === "user" || expected === "approval")
        && (actual === "user" || actual === "approval");
    return sameVisibleUserLane;
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
        if (ceoTurnSourceMatches(expectedSource, turn.source)) return index;
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
        clearCeoToolReminder(turn, { force: true });
        turn.finalized = true;
        turn.el?.remove?.();
        return true;
    }, { scrollMode: "preserve" });
    if (discarded) {
        patchCeoSessionSnapshotCache(activeSessionId(), (entry) => {
            const inflightTurn = normalizeCeoSnapshotInflight(entry?.inflight_turn);
            const preservedTurn = normalizeCeoSnapshotInflight(entry?.preserved_turn);
            const matchesSnapshot = (snapshot) => {
                if (!snapshot) return false;
                const snapshotTurnId = normalizeCeoTurnId(snapshot?.turn_id);
                if (normalizedTurnId && snapshotTurnId && snapshotTurnId !== normalizedTurnId) return false;
                const snapshotSource = String(snapshot?.source || "").trim().toLowerCase();
                if (normalizedSource && snapshotSource && !ceoTurnSourceMatches(normalizedSource, snapshotSource)) return false;
                return true;
            };
            const next = { ...(entry || {}) };
            let changed = false;
            if (matchesSnapshot(inflightTurn)) {
                next.inflight_turn = null;
                changed = true;
            }
            if (matchesSnapshot(preservedTurn)) {
                next.preserved_turn = null;
                changed = true;
            }
            return changed ? next : (entry || {});
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
        if (hasExpectedSource && !ceoTurnSourceMatches(expectedSource, turn.source)) continue;
        if (!force && hasRunningCeoToolStep(turn)) continue;
        const [removedTurn] = S.ceoPendingTurns.splice(index, 1);
        if (removedTurn) removed.push(removedTurn);
    }
    if (!removed.length) return false;
    return mutateCeoFeed(() => {
        removed.forEach((turn) => {
            clearCeoToolReminder(turn, { force: true });
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

function formatCeoTokenCount(value) {
    const n = Number(value) || 0;
    if (n < 1000) return String(n);
    if (n < 1000000) {
        const k = n / 1000;
        return `${k >= 100 ? Math.round(k) : k.toFixed(1)}k`;
    }
    const m = n / 1000000;
    return `${m >= 100 ? Math.round(m) : m.toFixed(1)}M`;
}

function normalizeCeoTurnUsage(usage = null) {
    if (!usage || typeof usage !== "object") return null;
    const input = Number(usage?.input_tokens ?? usage?.inputTokens ?? 0) || 0;
    const output = Number(usage?.output_tokens ?? usage?.outputTokens ?? 0) || 0;
    const cache = Number(usage?.cache_hit_tokens ?? usage?.cacheHitTokens ?? 0) || 0;
    const calls = Number(usage?.call_count ?? usage?.callCount ?? 0) || 0;
    if (!input && !output && !cache) return null;
    return { input_tokens: input, output_tokens: output, cache_hit_tokens: cache, call_count: calls };
}

function setCeoTurnUsage(turn, usage = null, { completedAt = "" } = {}) {
    if (!turn) return;
    // 元数据记忆在 turn 对象上（sticky）：历史渲染与 finalize 复用同一 turn 时，
    // 后续调用即使不带 usage 也能回填，避免历史回合的 usage 行被收尾流程清空。
    const normalized = normalizeCeoTurnUsage(usage);
    if (normalized) turn.usage = normalized;
    const nextCompletedAt = String(completedAt || "").trim();
    if (nextCompletedAt) turn.completedAt = nextCompletedAt;
    if (!turn.usageEl) return;
    const parts = [];
    const currentUsage = normalizeCeoTurnUsage(turn.usage);
    if (currentUsage) {
        parts.push(
            `输入 ${formatCeoTokenCount(currentUsage.input_tokens)}`,
            `缓存命中 ${formatCeoTokenCount(currentUsage.cache_hit_tokens)}`,
            `输出 ${formatCeoTokenCount(currentUsage.output_tokens)}`
        );
    }
    const completedText = formatCompactTime(turn.completedAt);
    if (completedText) parts.push(`完成于 ${completedText}`);
    if (!parts.length) {
        turn.usageEl.textContent = "";
        turn.usageEl.hidden = true;
        turn.usageEl.setAttribute?.("aria-hidden", "true");
        return;
    }
    turn.usageEl.textContent = parts.join(" · ");
    turn.usageEl.hidden = false;
    turn.usageEl.removeAttribute?.("aria-hidden");
}

// 普通消息气泡(用户/无轨道助手兜底)的悬停元信息文本:时间必带角色语义,
// usage 仅在数据可用时附加(用户消息只有发送时间)。
function buildCeoMessageMetaText({ role = "", timestamp = "", usage = null } = {}) {
    const parts = [];
    const timeText = formatCompactTime(timestamp);
    if (timeText) {
        const isUser = String(role || "").trim().toLowerCase() === "user";
        parts.push(`${isUser ? "发送于" : "完成于"} ${timeText}`);
    }
    const normalizedUsage = normalizeCeoTurnUsage(usage);
    if (normalizedUsage) {
        parts.push(
            `输入 ${formatCeoTokenCount(normalizedUsage.input_tokens)}`,
            `缓存命中 ${formatCeoTokenCount(normalizedUsage.cache_hit_tokens)}`,
            `输出 ${formatCeoTokenCount(normalizedUsage.output_tokens)}`
        );
    }
    return parts.join(" · ");
}

function setCeoTurnUsageCollapsed(turn, collapsed = true) {
    if (!turn?.el?.classList) return;
    turn.el.classList.toggle("usage-collapsed", !!collapsed);
}

function clearCeoToolReminder(turn, { executionId = "", force = false } = {}) {
    if (!turn) return false;
    const expectedExecutionId = String(executionId || "").trim();
    const currentExecutionId = String(turn.reminderExecutionId || "").trim();
    if (!force && expectedExecutionId && currentExecutionId && currentExecutionId !== expectedExecutionId) return false;
    turn.reminderExecutionId = "";
    if (!turn.reminderEl) return false;
    turn.reminderEl.textContent = "";
    turn.reminderEl.hidden = true;
    turn.reminderEl.setAttribute?.("aria-hidden", "true");
    return true;
}

function findCeoReminderTurn({ turnId = "", executionId = "" } = {}) {
    const normalizedTurnId = normalizeCeoTurnId(turnId);
    const normalizedExecutionId = String(executionId || "").trim();
    if (normalizedTurnId) {
        const turn = getActiveCeoTurn(null, normalizedTurnId);
        if (turn) return turn;
    }
    if (normalizedExecutionId) {
        for (let index = S.ceoPendingTurns.length - 1; index >= 0; index -= 1) {
            const turn = S.ceoPendingTurns[index];
            if (!turn || turn.finalized) continue;
            if (String(turn.reminderExecutionId || "").trim() === normalizedExecutionId) return turn;
        }
    }
    const activeTurns = S.ceoPendingTurns.filter((turn) => turn && !turn.finalized);
    return activeTurns.length === 1 ? activeTurns[0] : null;
}

function handleCeoToolReminder(payload = {}) {
    const executionId = String(payload?.execution_id || "").trim();
    const turn = findCeoReminderTurn({
        turnId: payload?.turn_id || "",
        executionId,
    });
    if (!turn) return;
    const label = String(payload?.label || "").trim();
    const terminal = Boolean(payload?.terminal);
    mutateCeoFeed(() => {
        if (terminal || !label) {
            clearCeoToolReminder(turn, { executionId, force: terminal || !label });
            return true;
        }
        turn.reminderExecutionId = executionId || String(turn.reminderExecutionId || "").trim();
        if (turn.reminderEl) {
            turn.reminderEl.textContent = "";
            turn.reminderEl.hidden = true;
            turn.reminderEl.setAttribute?.("aria-hidden", "true");
        }
        return true;
    }, { scrollMode: "preserve" });
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

function ceoContextLoaderKind(toolName = "") {
    const normalized = String(toolName || "").trim().toLowerCase();
    if (normalized === "load_tool_context" || normalized === "load_tool_context_v2") return "tool";
    if (normalized === "load_skill_context" || normalized === "load_skill_context_v2") return "skill";
    return "";
}

function isCeoContextLoaderToolName(toolName = "") {
    return !!ceoContextLoaderKind(toolName);
}

function clearCeoContextLoadNoticeTimer(noticeId = "") {
    const normalizedNoticeId = String(noticeId || "").trim();
    const timers = S.ceoContextLoadNoticeTimeoutIds instanceof Map
        ? S.ceoContextLoadNoticeTimeoutIds
        : new Map();
    S.ceoContextLoadNoticeTimeoutIds = timers;
    if (!normalizedNoticeId) {
        timers.forEach((timeoutId) => {
            window.clearTimeout(timeoutId);
        });
        timers.clear();
        return;
    }
    if (!timers.has(normalizedNoticeId)) return;
    window.clearTimeout(timers.get(normalizedNoticeId));
    timers.delete(normalizedNoticeId);
}

function syncCeoContextLoadNoticeVisibility() {
    const noticeEl = U.ceoContextLoadNotice;
    if (!noticeEl) return;
    const hasChildren = Number(noticeEl.children?.length || 0) > 0;
    noticeEl.hidden = !hasChildren;
    noticeEl.setAttribute("aria-hidden", hasChildren ? "false" : "true");
}

function removeCeoContextLoadNoticeItem(noticeId = "") {
    const normalizedNoticeId = String(noticeId || "").trim();
    if (!normalizedNoticeId) return;
    clearCeoContextLoadNoticeTimer(normalizedNoticeId);
    const noticeEl = U.ceoContextLoadNotice;
    if (!noticeEl) return;
    const target = Array.from(noticeEl.children || []).find((item) => (
        String(item?.dataset?.noticeId || "").trim() === normalizedNoticeId
    ));
    target?.remove?.();
    syncCeoContextLoadNoticeVisibility();
}

function hideCeoContextLoadNotice() {
    clearCeoContextLoadNoticeTimer();
    const noticeEl = U.ceoContextLoadNotice;
    if (!noticeEl) return;
    Array.from(noticeEl.children || []).forEach((item) => item?.remove?.());
    noticeEl.innerHTML = "";
    syncCeoContextLoadNoticeVisibility();
}

function normalizeNoticeRiskLevel(level = "") {
    const normalized = String(level || "").trim().toLowerCase();
    return ["low", "medium", "high"].includes(normalized) ? normalized : "medium";
}

function noticeRiskRank(level = "") {
    return ({ low: 1, medium: 2, high: 3 })[normalizeNoticeRiskLevel(level)] || 2;
}

function highestNoticeRiskLevel(levels = []) {
    let highest = "medium";
    (Array.isArray(levels) ? levels : []).forEach((level) => {
        const normalized = normalizeNoticeRiskLevel(level);
        if (noticeRiskRank(normalized) > noticeRiskRank(highest)) highest = normalized;
    });
    return highest;
}

function ceoContextLoadNoticeIconName(kind = "") {
    const normalizedKind = String(kind || "").trim().toLowerCase();
    if (normalizedKind === "tool") return "wrench";
    if (normalizedKind === "skill") return "sparkles";
    return "";
}

function showCeoContextLoadNotice(text = "", { durationMs = CEO_CONTEXT_LOAD_NOTICE_DURATION_MS, kind = "", riskLevel = "medium" } = {}) {
    const normalizedText = String(text || "").trim();
    if (!normalizedText) {
        hideCeoContextLoadNotice();
        return null;
    }
    const noticeEl = U.ceoContextLoadNotice;
    if (!noticeEl || typeof document?.createElement !== "function") return null;
    const noticeId = `ceo-context-load-${(Number(S.ceoContextLoadNoticeSeq) || 0) + 1}`;
    S.ceoContextLoadNoticeSeq = (Number(S.ceoContextLoadNoticeSeq) || 0) + 1;
    const item = document.createElement("div");
    item.className = "ceo-context-load-notice-item";
    const normalizedKind = String(kind || "").trim().toLowerCase();
    if (normalizedKind === "tool" || normalizedKind === "skill") {
        item.classList?.add?.(`is-${normalizedKind}`);
        item.dataset.noticeKind = normalizedKind;
    }
    const normalizedRiskLevel = normalizeNoticeRiskLevel(riskLevel);
    item.classList?.add?.(`risk-${normalizedRiskLevel}`);
    item.dataset.riskLevel = normalizedRiskLevel;
    item.dataset.noticeId = noticeId;
    const iconName = ceoContextLoadNoticeIconName(normalizedKind);
    if (iconName) {
        const kindIconEl = document.createElement("span");
        kindIconEl.className = "ceo-context-load-notice-kind-icon";
        kindIconEl.setAttribute("aria-hidden", "true");
        kindIconEl.innerHTML = `<i data-lucide="${iconName}"></i>`;
        item.appendChild(kindIconEl);
    }
    const textEl = document.createElement("span");
    textEl.className = "ceo-context-load-notice-text";
    textEl.textContent = normalizedText;
    item.appendChild(textEl);
    const riskDotEl = document.createElement("span");
    riskDotEl.className = `ceo-context-load-notice-risk-dot risk-${normalizedRiskLevel}`;
    riskDotEl.setAttribute("aria-hidden", "true");
    item.appendChild(riskDotEl);
    noticeEl.appendChild(item);
    syncCeoContextLoadNoticeVisibility();
    icons();
    if (Number.isFinite(durationMs) && durationMs > 0) {
        const timeoutId = window.setTimeout(() => {
            removeCeoContextLoadNoticeItem(noticeId);
        }, durationMs);
        const timers = S.ceoContextLoadNoticeTimeoutIds instanceof Map
            ? S.ceoContextLoadNoticeTimeoutIds
            : new Map();
        timers.set(noticeId, timeoutId);
        S.ceoContextLoadNoticeTimeoutIds = timers;
    }
    return item;
}

function extractCeoContextLoadTarget(toolName = "", rawText = "") {
    const loaderKind = ceoContextLoaderKind(toolName);
    const key = loaderKind === "skill" ? "skill_id" : "tool_id";
    const text = String(rawText || "").trim();
    if (!text) return "";
    const payload = parseJsonObjectText(text);
    const directCandidates = [
        payload?.[key],
        payload?.target_id,
        payload?.id,
        payload?.resource_id,
        payload?.resource?.[key],
        payload?.resource?.id,
        payload?.candidate?.[key],
        payload?.candidate?.id,
    ];
    for (const candidate of directCandidates) {
        const normalized = String(candidate || "").trim();
        if (normalized) return normalized;
    }
    const hydrationTargets = Array.isArray(payload?.hydration_targets) ? payload.hydration_targets : [];
    for (const candidate of hydrationTargets) {
        const normalized = String(candidate || "").trim();
        if (normalized) return normalized;
    }
    const match = text.match(new RegExp(`${key}"?\\s*[:=]\\s*"?(?<id>[A-Za-z0-9_.:/-]+)`));
    return String(match?.groups?.id || "").trim();
}

function buildCeoContextLoadNotice(toolName = "", targetId = "") {
    const loaderKind = ceoContextLoaderKind(toolName);
    const normalizedTargetId = String(targetId || "").trim();
    if (loaderKind === "skill") {
        return normalizedTargetId
            ? `\u5df2\u52a0\u8f7d skill ${normalizedTargetId}`
            : "\u5df2\u52a0\u8f7d skill";
    }
    return normalizedTargetId
        ? `\u5df2\u52a0\u8f7d\u5de5\u5177 ${normalizedTargetId}`
        : "\u5df2\u52a0\u8f7d\u5de5\u5177\u4e0a\u4e0b\u6587";
}

// 资源目录按 surfaced family 记账，actions[].executor_names 才是 concrete executor。
// concrete id 命中单个 action 时取该 action 的风险度；只有 family 级加载才取整族最高风险度。
function toolCatalogRiskLevel(record, targetId = "") {
    const normalizedTargetId = String(targetId || "").trim();
    if (!normalizedTargetId) return "";
    const actions = Array.isArray(record?.actions) ? record.actions : [];
    if (String(record?.tool_id || "").trim() === normalizedTargetId) {
        const actionLevels = actions
            .map((action) => String(action?.risk_level || "").trim().toLowerCase())
            .filter(Boolean);
        return highestNoticeRiskLevel([
            String(record?.risk_level || "").trim().toLowerCase(),
            ...actionLevels,
        ]);
    }
    const executorAction = actions.find((action) => (
        (Array.isArray(action?.executor_names) ? action.executor_names : [])
            .map((name) => String(name || "").trim())
            .includes(normalizedTargetId)
    ));
    return executorAction ? normalizeNoticeRiskLevel(executorAction.risk_level) : "";
}

function resolveCeoContextLoadNoticeRiskLevel(kind = "", targetId = "") {
    const normalizedKind = String(kind || "").trim().toLowerCase();
    const normalizedTargetId = String(targetId || "").trim();
    if (normalizedKind === "skill") {
        const match = (Array.isArray(S.skills) ? S.skills : []).find((item) => (
            String(item?.skill_id || "").trim() === normalizedTargetId
        ));
        return normalizeNoticeRiskLevel(match?.risk_level || "medium");
    }
    if (normalizedKind === "tool") {
        const records = Array.isArray(S.tools) ? S.tools : [];
        for (const record of records) {
            const level = toolCatalogRiskLevel(record, normalizedTargetId);
            if (level) return level;
        }
        return "medium";
    }
    return "medium";
}

function ensureCeoContextLoadNoticeKeys(turn) {
    if (!turn || turn.contextLoadNoticeKeys instanceof Set) return turn?.contextLoadNoticeKeys || null;
    turn.contextLoadNoticeKeys = new Set();
    return turn.contextLoadNoticeKeys;
}

function maybeShowCeoContextLoadNotice(turn, { toolName = "", status = "", detailTexts = [] } = {}) {
    const normalizedTool = String(toolName || "").trim().toLowerCase();
    const loaderKind = ceoContextLoaderKind(normalizedTool);
    const normalizedStatus = String(status || "").trim().toLowerCase();
    if (!loaderKind || normalizedStatus !== "success") return false;
    if (String(turn?.source || "").trim().toLowerCase() === "history") return false;
    const keys = ensureCeoContextLoadNoticeKeys(turn);
    let targetId = "";
    for (const candidate of Array.isArray(detailTexts) ? detailTexts : []) {
        targetId = extractCeoContextLoadTarget(normalizedTool, candidate);
        if (targetId) break;
    }
    const noticeText = buildCeoContextLoadNotice(normalizedTool, targetId);
    const signature = `${normalizedTool}:${targetId || noticeText}`;
    if (keys?.has(signature)) return true;
    keys?.add(signature);
    showCeoContextLoadNotice(noticeText, {
        kind: loaderKind,
        riskLevel: resolveCeoContextLoadNoticeRiskLevel(loaderKind, targetId),
    });
    return true;
}

function filterCeoInteractionFlowSummary(summary = null) {
    const normalizedSummary = normalizeCeoSnapshotCanonicalContext(summary);
    if (!normalizedSummary?.stages?.length) return null;
    return {
        stages: normalizedSummary.stages.map((stage) => {
            const originalRounds = Array.isArray(stage?.rounds) ? stage.rounds : [];
            const inferredUsed = typeof countBudgetedExecutionStageRounds === "function"
                ? countBudgetedExecutionStageRounds(stage)
                : originalRounds.length;
            return {
                ...stage,
                tool_rounds_used: Math.max(Number(stage?.tool_rounds_used || 0), inferredUsed),
                rounds: originalRounds
                    .map((round) => ({
                        ...round,
                        tools: (Array.isArray(round?.tools) ? round.tools : []).filter((step) => {
                            const toolName = String(step?.tool_name || "").trim().toLowerCase();
                            const status = String(step?.status || "").trim().toLowerCase();
                            return !isCeoContextLoaderToolName(toolName) || status === "error";
                        }),
                    }))
                    .filter((round) => Array.isArray(round?.tools) && round.tools.length),
            };
        }),
    };
}

function maybeShowCeoContextLoadNoticesFromSummary(turn, summary = null) {
    const normalizedSummary = normalizeCeoSnapshotCanonicalContext(summary);
    if (!normalizedSummary?.stages?.length) return;
    normalizedSummary.stages.forEach((stage) => {
        (Array.isArray(stage?.rounds) ? stage.rounds : []).forEach((round) => {
            (Array.isArray(round?.tools) ? round.tools : []).forEach((step) => {
                maybeShowCeoContextLoadNotice(turn, {
                    toolName: step?.tool_name || "",
                    status: step?.status || "",
                    detailTexts: [step?.output_text || "", step?.arguments_text || ""],
                });
            });
        });
    });
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
    const copyEl = item.querySelector(".interaction-step-copy");
    const detailText = normalizeInteractionDetailText(item.dataset.detailText || "");
    const previewText = buildInteractionPreviewText(detailText) || detailText;
    const collapsible = isInteractionDetailCollapsible(detailText);
    const expanded = collapsible && item.dataset.outputExpanded === "true";
    if (!collapsible) item.dataset.outputExpanded = "false";
    if (copyEl instanceof HTMLButtonElement) {
        copyEl.hidden = !detailText;
    }
    if (previewEl instanceof HTMLElement) {
        setTextContentPreservingScroll(previewEl, previewText);
        const hidePreview = expanded || !previewText;
        // hidden 的往返赋值同样会触发浏览器回收滚动位置,状态没变就不写。
        if (previewEl.hidden !== hidePreview) previewEl.hidden = hidePreview;
        previewEl.title = collapsible ? detailText : "";
    }
    if (detailEl instanceof HTMLElement) {
        setTextContentPreservingScroll(detailEl, detailText);
        const hideDetail = !expanded || !collapsible;
        if (detailEl.hidden !== hideDetail) detailEl.hidden = hideDetail;
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

async function copyCeoToolStepOutput(item) {
    // 会话工具步骤的复制入口:优先取已展开/水合过的完整结果,
    // 带 outputRef 且未水合时先拉取全量输出,保证复制的是完整内容。
    if (!(item instanceof HTMLElement)) return;
    const button = item.querySelector(".interaction-step-copy");
    let text = normalizeInteractionDetailText(item.dataset.detailText || "");
    if (item.dataset.outputRef) {
        if (typeof ensureCeoToolStepFullOutput === "function") {
            text = await ensureCeoToolStepFullOutput(item);
        }
    }
    text = String(text || "").trim();
    if (!text) {
        if (typeof flashTraceCopyButton === "function" && button instanceof HTMLButtonElement) flashTraceCopyButton(button, false);
        showToast({ title: "没有可复制的内容", text: "该工具步骤暂无输出内容。", kind: "error" });
        return;
    }
    const copied = await copyTextToClipboard(text);
    if (typeof flashTraceCopyButton === "function" && button instanceof HTMLButtonElement) flashTraceCopyButton(button, !!copied);
    showToast({
        title: copied ? "已复制" : "复制失败",
        text: copied ? "工具结果已复制到剪贴板。" : "请手动选中文本后复制。",
        kind: copied ? "success" : "error",
    });
}

function trimCeoToolSteps(turn) {
    if (!turn?.listEl) return;
    const items = Array.from(turn.listEl.children).filter((item) => (
        item instanceof HTMLElement && !item.classList.contains("task-trace-live-text")
    ));
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

function parseCeoSubmittedStage(event = {}) {
    const candidates = [event?.text, event?.output_preview_text, event?.output_text, event?.arguments_text];
    for (const raw of candidates) {
        const text = String(raw || "").trim();
        if (!text.startsWith("{") || !text.includes("stage_id")) continue;
        let parsed = null;
        try {
            parsed = JSON.parse(text);
        } catch (error) {
            // 工具输出被预览截断时不是完整 JSON，交回普通工具卡片渲染。
            continue;
        }
        if (!parsed || typeof parsed !== "object") continue;
        if (!parsed.stage_id && !parsed.stage_index) continue;
        if (!String(parsed.stage_goal || parsed.preamble_text || "").trim()) continue;
        return parsed;
    }
    return null;
}

function extractCeoSubmittedStageContext(toolName = "", event = {}) {
    if (String(toolName || "").trim().toLowerCase() !== "submit_next_stage") return null;
    const stage = parseCeoSubmittedStage(event);
    return stage ? { stages: [stage] } : null;
}

function applyCeoToolEventToTurn(turn, event = {}) {
    if (!turn?.listEl || !turn?.flowEl) return null;
    const status = resolveCeoToolEventStatus(event);
    const toolName = String(event.tool_name || "tool").trim() || "tool";
    const rawText = String(event.text || "").trim();
    if (isCeoContextLoaderToolName(toolName) && status !== "error") {
        maybeShowCeoContextLoadNotice(turn, {
            toolName,
            status,
            detailTexts: [
                rawText,
                String(event.output_text || "").trim(),
                String(event.output_preview_text || "").trim(),
                String(event.arguments_text || "").trim(),
            ],
        });
        return null;
    }
    if (status !== "error") {
        const submittedStageContext = extractCeoSubmittedStageContext(toolName, event);
        if (submittedStageContext && renderCeoStageTraceIntoTurn(turn, mergeCeoLiveTraceContext(
            submittedStageContext,
            turn.lastExecutionTraceSummary
        ))) {
            return null;
        }
    }
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
                    <button type="button" class="interaction-step-copy" hidden aria-label="复制工具结果" title="复制工具结果"><i data-lucide="copy"></i></button>
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
        item.querySelector(".interaction-step-copy")?.addEventListener("click", (interactionEvent) => {
            interactionEvent.preventDefault();
            interactionEvent.stopPropagation();
            void copyCeoToolStepOutput(item);
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
    const silentReply = meta?.silent_reply === true;
    const finalCanonicalContext = normalizeCeoSnapshotCanonicalContext(meta?.canonical_context || null);
    const finalUserMessages = normalizeCeoSnapshotUserMessages(meta?.user_messages, meta?.user_message);
    const turn = pullActiveCeoTurn(normalizedSource, normalizedTurnId);
    // final 事件带的 canonical 数据优先（后端在 delta 为空时会连 canonical_context 一起省略）；
    // 缺失时回退到本轮在 run 中自己渲染出的轨道（lastExecutionTraceSummary），
    // 避免「阶段在最终答复后消失、刷新才回来」。该兜底是当前轮的 per-turn delta，
    // 不会回填旧轮次全量 trace；心跳轮没有 live 轨道（null）不受影响。
    const finalTraceContext = resolveFinalCeoTraceContext(meta || {})
        || turn?.lastExecutionTraceSummary
        || null;
    // 三个旧分支的缓存写入统一为一次计算:消息列表与 inflight 清空只在处构建,
    // 渲染层再决定走增量更新还是全量快照重建。完成时间:历史回合用消息自带
    // timestamp,live 回合用收尾时刻的本地时间。
    const completedAt = String(meta?.timestamp || "").trim() || new Date().toISOString();
    const finalPayload = buildFinalizedCeoTurnPayload(sessionId, {
        normalizedSource,
        normalizedTurnId,
        finalUserMessages,
        finalTraceContext,
        finalCanonicalContext,
        text,
        meta,
        completedAt,
    });
    const updatedEntry = patchCeoSessionSnapshotCache(sessionId, (entry) => ({
        ...(entry || {}),
        messages: finalPayload.messages,
        inflight_turn: finalPayload.inflight_turn,
    }));
    const renderEntry = updatedEntry || getCeoSessionSnapshotCache(sessionId);
    const renderedKeys = Array.isArray(S.ceoFeedRenderedMessageKeys) ? S.ceoFeedRenderedMessageKeys : [];
    const nextKeys = buildCeoMessageKeyList(finalPayload.messages || []);
    // 无 user_messages 的收尾保持原位语义:回合元素就地 finalize(或 addMsg 兜底),
    // 不做全量重建;增量路径只用于 final 带 user_messages 的场景。
    if (!finalUserMessages.length) {
        if (!turn?.textEl || !turn?.flowEl) {
            if (!silentReply) addMsg(text, "system", { markdown: true, scrollMode: "preserve" });
            discardPendingCeoTurns({
                force: normalizedSource === "heartbeat",
                source: normalizedSource,
                turnId: normalizedTurnId,
            });
            maybeDispatchQueuedCeoFollowUps();
            return;
        }
        mutateCeoFeed(() => {
            clearCeoToolReminder(turn, { force: true });
            turn.finalized = true;
            turn.liveStreamText = "";
            renderCeoLiveStreamTextIntoTurn(turn);
            if (silentReply) {
                hideCeoAssistantText(turn);
            } else {
                turn.textEl.hidden = false;
                turn.textEl.innerHTML = renderMarkdown(String(text || "").trim() || "已完成。");
                turn.textEl.classList.remove("pending");
                turn.textEl.classList.add("markdown-content");
                turn.textEl.classList.remove("assistant-text-loading");
                syncCeoAssistantLoadingAria(turn.textEl);
            }
            if (finalTraceContext) {
                renderCeoStageTraceIntoTurn(turn, finalTraceContext);
            }
            if (turn.steps > 0) {
                const hasRunningStep = hasRunningCeoToolStep(turn);
                turn.flowEl.hidden = false;
                // 阶段轨道的展开态已由 renderCeoStageTraceIntoTurn 按用户选择还原,
                // 收尾不再强制展开;仅无阶段轨道(纯工具事件回合)保留默认展开旧行为。
                if (!finalTraceContext) turn.flowEl.open = true;
                updateCeoTurnMeta(
                    turn,
                    turn.hasError ? "处理完成，但有异常" : (hasRunningStep ? "已返回当前判断，后台任务仍在运行" : "处理完成")
                );
            } else {
                turn.flowEl.hidden = true;
            }
            setCeoTurnUsage(turn, meta?.usage || turn.usage || null, { completedAt: turn.completedAt || completedAt });
            setCeoTurnUsageCollapsed(turn, true);
            icons();
        }, { scrollMode: "preserve" });
        discardPendingCeoTurns({
            force: normalizedSource === "heartbeat",
            source: normalizedSource,
            turnId: normalizedTurnId,
        });
        maybeDispatchQueuedCeoFollowUps();
        return;
    }
    const headMatches = nextKeys.length >= renderedKeys.length
        && nextKeys.slice(0, renderedKeys.length).every((key, index) => key === renderedKeys[index]);
    // 增量更新只在 DOM 与记录完全对齐且最后一个子元素就是本回合时启用;
    // 任何偏差(promote 过补充消息、手动发送、多回合并存)都回退全量重建,
    // 保留旧路径的权威对齐语义。
    const canIncremental = !!(turn?.textEl && turn?.flowEl)
        && headMatches
        && ceoFeedMatchesIncrementalFinalize(renderedKeys, turn);
    if (canIncremental) {
        const addedMessages = (finalPayload.messages || []).slice(renderedKeys.length, Math.max(renderedKeys.length, nextKeys.length - 1));
        const addedKeys = nextKeys.slice(renderedKeys.length, nextKeys.length - 1);
        mutateCeoFeed(() => {
            addedMessages.forEach((message, index) => {
                addMsg(String(message?.content || ""), "user", {
                    attachments: normalizeUploadList(message?.attachments),
                    scrollMode: "preserve",
                    sessionId,
                    timestamp: String(message?.timestamp || ""),
                });
                // addMsg 不返回元素;同一同步批次内它一定是 feed 的最后一个子节点,
                // 取出来补稳定 key 并移到回合元素之前。
                const feedChildren = U.ceoFeed && U.ceoFeed.children ? Array.from(U.ceoFeed.children) : [];
                const el = (U.ceoFeed && U.ceoFeed.lastElementChild) || feedChildren[feedChildren.length - 1] || null;
                if (!el) return;
                if (typeof el.setAttribute === "function") {
                    el.setAttribute("data-ceo-key", addedKeys[index] || "");
                }
                if (U.ceoFeed && turn.el && typeof U.ceoFeed.insertBefore === "function") {
                    try {
                        U.ceoFeed.insertBefore(el, turn.el);
                    } catch (error) {
                        void error;
                    }
                }
            });
            clearCeoToolReminder(turn, { force: true });
            turn.finalized = true;
            turn.liveStreamText = "";
            renderCeoLiveStreamTextIntoTurn(turn);
            if (silentReply) {
                hideCeoAssistantText(turn);
            } else {
                turn.textEl.hidden = false;
                turn.textEl.innerHTML = renderMarkdown(String(text || "").trim() || "已完成。");
                turn.textEl.classList.remove("pending");
                turn.textEl.classList.add("markdown-content");
                turn.textEl.classList.remove("assistant-text-loading");
                syncCeoAssistantLoadingAria(turn.textEl);
            }
            if (finalTraceContext) {
                renderCeoStageTraceIntoTurn(turn, finalTraceContext);
            }
            if (turn.steps > 0) {
                const hasRunningStep = hasRunningCeoToolStep(turn);
                turn.flowEl.hidden = false;
                // 阶段轨道的展开态已由 renderCeoStageTraceIntoTurn 按用户选择还原,
                // 收尾不再强制展开;仅无阶段轨道(纯工具事件回合)保留默认展开旧行为。
                if (!finalTraceContext) turn.flowEl.open = true;
                updateCeoTurnMeta(
                    turn,
                    turn.hasError ? "处理完成，但有异常" : (hasRunningStep ? "已返回当前判断，后台任务仍在运行" : "处理完成")
                );
            } else {
                turn.flowEl.hidden = true;
            }
            setCeoTurnUsage(turn, meta?.usage || turn.usage || null, { completedAt: turn.completedAt || completedAt });
            setCeoTurnUsageCollapsed(turn, true);
            icons();
        }, { scrollMode: "preserve" });
        discardPendingCeoTurns({
            force: normalizedSource === "heartbeat",
            source: normalizedSource,
            turnId: normalizedTurnId,
        });
        S.ceoFeedRenderedMessageKeys = nextKeys;
        S.ceoFeedRenderSignature = buildCeoRenderSignature(
            finalPayload.messages || [],
            finalPayload.inflight_turn || null,
            (renderEntry && renderEntry.preserved_turn) || null
        );
        consumeRepresentedRuntimeSentCeoFollowUps(sessionId, finalUserMessages);
        maybeDispatchQueuedCeoFollowUps();
        return;
    }
    if (renderEntry) {
        renderCeoSnapshot(renderEntry.messages || [], renderEntry.inflight_turn || null, {
            sessionId,
            preservedTurn: renderEntry.preserved_turn || null,
        });
    } else {
        addMsg(text, "system", { markdown: true, scrollMode: "preserve" });
    }
    consumeRepresentedRuntimeSentCeoFollowUps(sessionId, finalUserMessages);
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
}

function showToast({ title = "操作成功", text = "修改已生效", kind = "success", durationMs = 3000, persistent = false } = {}) {
    if (!U.toast || !U.toastTitle || !U.toastText || !U.toastClose) return;
    clearToastTimers();
    const sticky = persistent || durationMs <= 0;
    U.toastTitle.textContent = title;
    U.toastText.textContent = text;
    U.toast.hidden = false;
    U.toast.setAttribute("role", kind === "error" ? "alert" : "status");
    U.toastClose.hidden = false;
    if (!sticky) {
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
        button.textContent = busy ? "保存中…" : "保存";
        button.disabled = !!busy || !dirty;
    }
    if (hint) {
        hint.classList.toggle("is-dirty", dirty);
        hint.textContent = dirty ? (busy ? "正在保存…" : "有未保存的修改，请点击「保存」。") : "";
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
    renderMessageHeading(0);
    renderSpawnReviewHeading(0);
    renderArtifactHeading(0);
    if (U.adOutputHeading) U.adOutputHeading.innerHTML = '<i data-lucide="arrow-up-from-line"></i> 最终输出';
    if (U.adOutput) U.adOutput.classList.add("task-trace-output");
    if (U.adAcceptanceHeading) U.adAcceptanceHeading.innerHTML = '<i data-lucide="shield-check"></i> 验收结果';
    if (U.adFlow) {
        U.adFlow.classList.remove("code-block");
        U.adFlow.classList.add("task-trace-host");
    }
    if (U.adMessages) {
        U.adMessages.classList.remove("code-block");
        U.adMessages.classList.add("task-trace-host");
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

// 容器重绘后把用户正开着的那个下拉框重新打开。下拉框的身份必须稳定
// （select.id），否则整块 DOM 重建等于把它关掉；S.openResourceSelectId 只在
// 真正开着时非空，所以选完值、按 Esc、点别处之后不会再被"恢复"打开。
function restoreOpenResourceSelect(container = document) {
    const openId = String(S.openResourceSelectId || "").trim();
    if (!openId) return false;
    if (!container || typeof container.querySelector !== "function") return false;
    const shell = container.querySelector(`.resource-select-shell[data-select-id="${openId}"]`);
    if (!shell || shell.classList.contains("is-open")) return false;
    const select = shell.querySelector("select.resource-select");
    if (!(select instanceof HTMLSelectElement)) return false;
    openResourceSelect(select);
    return true;
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
        returnFocus: U.projectSettings,
        onConfirm: async ({ checked }) => {
            if (hasRunning && !checked) {
                throw new Error("请先勾选“暂停正在进行的所有对话和任务”。");
            }
            await ApiClient.exitBootstrap({ pause_running_work: !!checked });
            finalizeProjectExit();
        },
    });
}

const PROJECT_SETTINGS_ERROR_TEXT = {
    "invalid password": "当前密码不正确",
    "password is required": "请输入新密码",
    "password_confirmation_mismatch": "两次输入的新密码不一致",
    "project is locked": "项目已锁定，请先解锁",
    "secret key is not configured": "项目还没有设置密码",
};

function projectSettingsErrorText(error) {
    const key = String(error?.message || error?.code || "").trim();
    return PROJECT_SETTINGS_ERROR_TEXT[key] || key || "操作失败";
}

function isProjectSettingsOpen() {
    return !!U.projectSettingsBackdrop && !U.projectSettingsBackdrop.hidden;
}

function setProjectSettingsBusy(busy) {
    const disabled = Boolean(busy);
    [U.projectSettingsChangePassword, U.projectSettingsAutoUnlock, U.projectSettingsLock, U.projectSettingsExit]
        .forEach((element) => {
            if (element) element.disabled = disabled;
        });
}

function clearProjectSettingsPasswords() {
    [U.projectSettingsCurrentPassword, U.projectSettingsNewPassword, U.projectSettingsNewPasswordConfirm]
        .forEach((element) => {
            if (element) element.value = "";
        });
}

function isPasswordChangeOpen() {
    return !!U.passwordChangeBackdrop && !U.passwordChangeBackdrop.hidden;
}

function openPasswordChangeDialog() {
    if (!U.passwordChangeBackdrop) return;
    U.passwordChangeBackdrop.hidden = false;
    U.passwordChangeBackdrop.classList.add("is-open");
    window.requestAnimationFrame(() => U.projectSettingsCurrentPassword?.focus?.());
}

function closePasswordChangeDialog() {
    if (!U.passwordChangeBackdrop) return;
    U.passwordChangeBackdrop.hidden = true;
    U.passwordChangeBackdrop.classList.remove("is-open");
    clearProjectSettingsPasswords();
}

function openProjectSettingsDialog() {
    if (!U.projectSettingsBackdrop) return;
    U.projectSettingsBackdrop.hidden = false;
    U.projectSettingsBackdrop.classList.add("is-open");
    U.projectSettings?.setAttribute("aria-expanded", "true");
    void syncProjectSettingsAutoUnlock();
    window.requestAnimationFrame(() => U.projectSettingsDialog?.focus?.());
}

function closeProjectSettingsDialog() {
    if (!U.projectSettingsBackdrop) return;
    closePasswordChangeDialog();
    U.projectSettingsBackdrop.hidden = true;
    U.projectSettingsBackdrop.classList.remove("is-open");
    U.projectSettings?.setAttribute("aria-expanded", "false");
    clearProjectSettingsPasswords();
}

async function syncProjectSettingsAutoUnlock() {
    try {
        const status = await ApiClient.getBootstrapStatus();
        if (U.projectSettingsAutoUnlock) {
            U.projectSettingsAutoUnlock.checked = Boolean(status?.auto_unlock);
        }
    } catch (error) {
        // 状态读不到时保持勾选框原样，让操作者自己决定。
    }
}

async function submitProjectPasswordChange() {
    const currentPassword = String(U.projectSettingsCurrentPassword?.value || "");
    const newPassword = String(U.projectSettingsNewPassword?.value || "");
    const passwordConfirm = String(U.projectSettingsNewPasswordConfirm?.value || "");
    if (!currentPassword || !newPassword) {
        showToast({ title: "请填写完整", text: "当前密码与新密码都不能为空。", kind: "error" });
        return;
    }
    if (newPassword !== passwordConfirm) {
        showToast({ title: "两次输入的新密码不一致", kind: "error" });
        return;
    }
    setProjectSettingsBusy(true);
    try {
        await ApiClient.changeBootstrapPassword({
            current_password: currentPassword,
            new_password: newPassword,
            password_confirm: passwordConfirm,
        });
        closePasswordChangeDialog();
        showToast({ title: "密码已修改", text: "自动解锁保存的是主密钥，改密后仍然有效。", kind: "success" });
    } catch (error) {
        showToast({ title: "修改密码失败", text: projectSettingsErrorText(error), kind: "error" });
    } finally {
        setProjectSettingsBusy(false);
    }
}

async function applyProjectAutoUnlockChange(enabled) {
    setProjectSettingsBusy(true);
    try {
        const status = await ApiClient.setBootstrapAutoUnlock(enabled);
        if (U.projectSettingsAutoUnlock) {
            U.projectSettingsAutoUnlock.checked = Boolean(status?.auto_unlock);
        }
        showToast({
            title: enabled ? "已开启自动解锁" : "已关闭自动解锁",
            text: enabled
                ? "解锁凭据已写入环境变量与 .g3ku 本地文件，下次启动自动解锁。"
                : "已删除环境变量与 .g3ku 本地保存的解锁凭据。",
            kind: "success",
        });
    } catch (error) {
        if (U.projectSettingsAutoUnlock) U.projectSettingsAutoUnlock.checked = !enabled;
        showToast({ title: "自动解锁设置失败", text: projectSettingsErrorText(error), kind: "error" });
    } finally {
        setProjectSettingsBusy(false);
    }
}

async function lockProjectFromSettings() {
    setProjectSettingsBusy(true);
    try {
        await ApiClient.lockBootstrap();
        closeProjectSettingsDialog();
        // 锁定后所有 /api 都会 423，直接回到解锁界面，而不是让界面留着报错。
        window.location.reload();
    } catch (error) {
        setProjectSettingsBusy(false);
        showToast({ title: "锁定失败", text: projectSettingsErrorText(error), kind: "error" });
    }
}

function modelScopeLabel(scope) {
    return (MODEL_SCOPES.find((item) => item.key === scope) || { label: String(scope || "") }).label;
}

function missingRequiredModelRoleLabels(source = "draft") {
    return MODEL_SCOPES
        .filter((item) => item.key !== "memory")
        .map((item) => item.key)
        .filter((scope) => modelScopeChain(scope, source).length < 1)
        .map((scope) => modelScopeLabel(scope));
}

function requiredModelRoleValidationMessage(source = "draft") {
    const labels = missingRequiredModelRoleLabels(source);
    if (!labels.length) return "";
    return `请先为以下角色配置模型链：${labels.join("、")}。全部拖入后才可保存。`;
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
    if (!S.modelCatalog.catalog.length) return hint("当前还没有配置，请先添加配置。", false);
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
            : "暂无可用配置，请先添加配置。";
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
                    <h2 id="model-detail-title">${isCreate ? "添加配置" : "配置详情"}</h2>
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
                                <input class="resource-search" name="apiKey" value="${esc(current?.api_key || "")}" placeholder="sk-...">
                            </label>
                            <label class="resource-field">
                                <span class="resource-field-label">Base URL ${isCreate ? "*" : ""}</span>
                                <input class="resource-search" name="apiBase" value="${esc(current?.api_base || "")}" placeholder="https://api.example.com/v1">
                            </label>
                        </div>
                        <p class="subtitle">每个配置只使用一个 API Key（单 key）；需要多个模型或容灾回退时，请添加多个配置并组成模型链。</p>
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
                                <span class="resource-field-label">最大上下文TOKEN *</span>
                                <input class="resource-search" type="number" min="25001" step="1" name="contextWindowTokens" value="${esc(String(current?.context_window_tokens ?? ""))}" placeholder="必须大于 25000">
                            </label>
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
                                <span class="resource-field-label">自动重试错误关键词(空格间隔)</span>
                                <input class="resource-search" name="retryOn" value="${esc((current?.retry_on || []).join(" "))}" placeholder="如 network 429 502（可自定义关键词，空格间隔）">
                            </label>
                            <label class="resource-field">
                                <span class="resource-field-label">重试次数</span>
                                <input class="resource-search spinless-number-input" type="number" min="0" step="1" name="retryCount" value="${esc(String(current?.retry_count ?? 0))}" placeholder="0" title="命中自动重试关键词时的最大重试轮数（一轮 = 完整轮过该模型所有 Key），轮间按指数退避加抖动；填 0 使用默认 10 轮。未命中关键词的错误每个 Key 只试一次即切换下一模型。">
                            </label>
                        </div>
                        <p class="subtitle">重试次数 = 命中自动重试关键词时该模型的最大重试轮数（一轮完整轮过所有 Key，轮间动态退避）；0 表示默认 10 轮，预算耗尽后切换下一模型，全链耗尽报错停止。</p>
                    </section>
                    <section class="resource-section">
                        <h3>额外请求头</h3>
                        <textarea class="resource-editor model-textarea" name="extraHeaders" rows="6" placeholder='{"X-Trace-Id": "demo"}'>${esc(current?.extra_headers ? JSON.stringify(current.extra_headers, null, 2) : "")}</textarea>
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

function renderRoleLimitControl({ scopeKey, kind, label, value, editing }) {
    const isFixedMemoryConcurrency = scopeKey === "memory" && kind === "concurrency";
    if (isFixedMemoryConcurrency) {
        return `
        <div class="model-role-limit-field" data-model-role-limit-kind="${esc(kind)}" data-model-role-limit-scope="${esc(scopeKey)}" data-model-role-fixed="1" data-model-role-fixed-value="1">
            <span class="model-role-iterations-label">${esc(label)}</span>
            <div class="llm-segmented-control model-role-limit-fixed-track" aria-hidden="true">
                <span class="llm-segmented-label model-role-limit-fixed-pill">固定为1</span>
            </div>
            <input type="hidden" value="1" data-model-role-limit-input="${esc(kind)}">
        </div>`;
    }
    const inputValue = value == null ? "-1" : String(value);
    return `
        <div class="model-role-limit-field" data-model-role-limit-kind="${esc(kind)}" data-model-role-limit-scope="${esc(scopeKey)}">
            <span class="model-role-iterations-label">${esc(label)}</span>
            <input class="model-role-limit-input spinless-number-input" type="number" min="-1" step="1" inputmode="numeric" value="${esc(inputValue)}" placeholder="-1" title="-1 代表不限制" ${editing ? "" : "disabled"} data-model-role-limit-input="${esc(kind)}">
        </div>`;
}

function syncRoleIterationDraftsFromInputs({ requireValid = false } = {}) {
    const roots = [U.modelRoleLimitsBar, U.modelRoleEditors].filter(Boolean);
    const groups = roots.flatMap((root) => [...root.querySelectorAll("[data-model-role-limit-kind][data-model-role-limit-scope]")]);
    if (!groups.length) return false;
    let changed = false;
    groups.forEach((group) => {
        if (!(group instanceof HTMLElement)) return;
        const scope = String(group.dataset.modelRoleLimitScope || "").trim();
        const kind = String(group.dataset.modelRoleLimitKind || "").trim();
        if (!scope || !kind) return;
        const input = group.querySelector("[data-model-role-limit-input]");
        if (!(input instanceof HTMLInputElement)) return;
        const scopeLabel = MODEL_SCOPES.find((item) => item.key === scope)?.label || scope;
        const label = kind === "iterations" ? "最大轮数" : "最大并发数";
        const fixed = String(group.dataset.modelRoleFixed || "").trim() === "1";
        if (fixed) {
            const fixedValue = normalizeInt(group.dataset.modelRoleFixedValue, 1);
            if (kind === "concurrency" && modelScopeConcurrency(scope, "draft") !== fixedValue) {
                S.modelCatalog.roleConcurrencyDrafts[scope] = fixedValue;
                changed = true;
            }
            input.disabled = true;
            input.value = String(fixedValue);
            input.classList.remove("is-invalid");
            input.setCustomValidity("");
            return;
        }
        let rawValue = String(input.value || "").trim();
        if (rawValue === "") rawValue = "-1";
        const cleanValue = Number.parseInt(rawValue, 10);
        const invalid = !Number.isInteger(cleanValue) || cleanValue < -1;
        if (invalid) {
            input.classList.add("is-invalid");
            input.setCustomValidity(`${label}必须是不小于 -1 的整数，-1 代表不限制`);
            if (requireValid) {
                input.reportValidity();
                throw new Error(`${scopeLabel} ${label}必须是不小于 -1 的整数`);
            }
            return;
        }
        input.classList.remove("is-invalid");
        input.setCustomValidity("");
        const nextValue = cleanValue < 0 ? null : cleanValue;
        const currentValue = kind === "iterations" ? modelScopeIterations(scope, "draft") : modelScopeConcurrency(scope, "draft");
        if (currentValue !== nextValue) {
            if (kind === "iterations") S.modelCatalog.roleIterationDrafts[scope] = nextValue;
            if (kind === "concurrency") S.modelCatalog.roleConcurrencyDrafts[scope] = nextValue;
            changed = true;
        }
    });
    if (changed) syncModelRoleDraftState();
    return changed;
}

function roleLimitSummary(kind) {
    const customized = [];
    MODEL_SCOPES.forEach((scope) => {
        if (kind === "concurrency" && scope.key === "memory") return;
        const value = kind === "iterations" ? modelScopeIterations(scope.key) : modelScopeConcurrency(scope.key);
        if (value != null) customized.push(`${scope.label} ${value}`);
    });
    if (!customized.length) return "默认 -1 · 不限";
    return `${customized.join(" / ")}，其余 -1`;
}

function renderRoleLimitsBar() {
    const bar = U.modelRoleLimitsBar;
    if (!bar) return;
    bar.hidden = true;
    bar.innerHTML = "";
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
    const validationMessage = requiredModelRoleValidationMessage("draft");
    if (validationMessage) {
        S.modelCatalog.error = validationMessage;
        hint(`模型配置错误：${S.modelCatalog.error}`, true);
        showToast({ title: "保存失败", text: S.modelCatalog.error, kind: "error" });
        return;
    }
    await persistModelRoleChains(MODEL_SCOPES.map((item) => item.key), "模型链已保存。", { useDrafts: true });
}

function parseModelRetryOn(raw) {
    return String(raw || "").split(/[\s,]+/).map((item) => item.trim()).filter(Boolean);
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
    const contextWindowTokensText = String(formData.get("contextWindowTokens") || "").trim();
    const reasoningEffort = reasoningEffortText || null;
    const retryOnRaw = String(formData.get("retryOn") || "").trim();
    const retryCountText = String(formData.get("retryCount") || "").trim();
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
    const contextWindowTokens = contextWindowTokensText ? Number.parseInt(contextWindowTokensText, 10) : NaN;

    if (maxTokensText && (!Number.isInteger(maxTokens) || maxTokens <= 0)) {
        throw new Error("Max Tokens 必须是正整数");
    }
    if (!contextWindowTokensText || !Number.isInteger(contextWindowTokens) || contextWindowTokens <= 25000) {
        throw new Error("最大上下文TOKEN必须是大于 25000 的整数");
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
            contextWindowTokens,
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
    if (contextWindowTokens !== Number.parseInt(String(current?.context_window_tokens ?? 0), 10)) {
        patch.contextWindowTokens = contextWindowTokens;
    }
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
    closeCeoModelModePanel();
    resetCeoModelSelection(nextSessionId);
    void refreshCeoModelSelection(nextSessionId);
    return switchCeoComposerDraft(previousSessionId, nextSessionId);
}

function resetCeoSessionState({ scrollToLatest = false } = {}) {
    resetCeoFeed();
    S.ceoFeedRenderSessionId = "";
    S.ceoPendingTurns = [];
    S.ceoTurnActive = false;
    S.ceoPauseBusy = false;
    if (scrollToLatest) S.ceoScrollToLatestOnSnapshot = true;
    syncCeoCompressionDivider();
    syncCeoPrimaryButton();
}

function closeCeoWs() {
    S.ceoWsToken += 1;
    const socket = S.ceoWs;
    S.ceoWs = null;
    settleCeoWsOpenWaiters(false);
    if (!socket) return;
    socket.onclose = null;
    socket.close();
}

function renderCeoSessionCard(item, { allowActions = false, index = -1 } = {}) {
    const sessionId = String(item?.session_id || "");
    const isActive = sessionId === activeSessionId();
    const isRunning = !!item?.is_running;
    const isBulkMode = !!S.ceoBulkMode;
    const isSelected = isCeoBulkSessionSelected(sessionId);
    const dragIndex = Number.isInteger(index) ? index : -1;
    const dragAttrs = ceoSessionDragEnabled() && dragIndex >= 0
        ? ` draggable="true" data-ceo-session-index="${dragIndex}"`
        : "";
    const preview = String(item?.preview_text || "").trim() || "No messages yet.";
    const title = String(item?.title || sessionId || "Session");
    const glyph = ceoSessionGlyph(item);
    const unreadCount = isActive ? 0 : sessionUnreadCount(sessionId);
    const unreadText = unreadCount > 99 ? "99+" : String(unreadCount);
    const createdText = formatSessionTime(item?.created_at);
    const type = String(item?.chat_type || "").trim();
    const typeLabel = type === "dm" ? "DM merged" : type === "group" ? "Group" : type === "thread" ? "Thread" : "";
    const badges = [
        typeLabel ? `<span class="ceo-session-pill">${esc(typeLabel)}</span>` : "",
        item?.is_readonly ? '<span class="ceo-session-pill readonly">只读</span>' : "",
    ].filter(Boolean).join("");
    return `
        <div class="ceo-session-card${isActive ? " is-active" : ""}${unreadCount > 0 ? " has-unread" : ""}${isRunning ? " is-running" : ""}${isBulkMode ? " is-bulk-mode" : ""}${isSelected ? " is-bulk-selected" : ""}" role="listitem"${dragAttrs}>
            ${isBulkMode ? `
                <label class="ceo-session-checkbox" aria-label="${esc(`选择会话 ${title}`)}">
                    <input type="checkbox" data-session-bulk-checkbox="${esc(sessionId)}" ${isSelected ? "checked" : ""}>
                    <span class="ceo-session-checkbox__box" aria-hidden="true"></span>
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
                    <span class="ceo-session-preview">${esc(preview)}</span>
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
                        <div class="ceo-session-menu-info">
                            <span class="ceo-session-menu-info-row"><span>会话 ID</span><code>${esc(sessionId || "-")}</code></span>
                            <span class="ceo-session-menu-info-row"><span>创建时间</span><code>${esc(createdText || "-")}</code></span>
                        </div>
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
        syncCeoCompressionDivider();
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
        U.ceoSessionList.innerHTML = sessions.map((item, index) => renderCeoSessionCard(item, { allowActions: true, index })).join("");
    }
    syncCeoComposerReadonlyState();
    syncCeoAttachButton();
    syncCeoSessionActions();
    syncCeoCompressionDivider();
    icons();
}

// ---- 会话卡片拖动排序（照 CEO 模型链那一套：容器代理 + 垂直中点位次）------

function ceoSessionDragEnabled() {
    // 渠道页签按群类型分组、没有扁平序号；批量模式整卡是勾选区。两者都不拖动。
    return S.ceoSessionTab !== "channel" && !S.ceoBulkMode;
}

function clearCeoSessionDragDecorations() {
    const list = U.ceoSessionList;
    if (!list) return;
    list.querySelectorAll(".is-drop-target").forEach((item) => item.classList.remove("is-drop-target"));
    list.querySelectorAll(".is-dragging").forEach((item) => item.classList.remove("is-dragging"));
    list.classList.remove("is-drop-zone");
}

function ceoSessionDropIndex(list, clientY) {
    const cards = [...list.querySelectorAll("[data-ceo-session-index]")];
    for (const card of cards) {
        const rect = card.getBoundingClientRect();
        if (clientY < rect.top + (rect.height / 2)) return Number(card.dataset.ceoSessionIndex);
    }
    return cards.length;
}

function beginCeoSessionCardDrag(event) {
    const list = U.ceoSessionList;
    const card = event.target instanceof Element ? event.target.closest("[data-ceo-session-index]") : null;
    if (!list || !card || !ceoSessionDragEnabled()) return;
    const index = Number(card.dataset.ceoSessionIndex);
    if (!Number.isInteger(index) || index < 0) return;
    S.ceoSessionDrag = { from: index, dropIndex: null };
    card.classList.add("is-dragging");
    if (event.dataTransfer) {
        event.dataTransfer.effectAllowed = "move";
        try {
            event.dataTransfer.setData("text/plain", String(index));
        } catch (error) {
            void error;
        }
    }
}

function updateCeoSessionCardDropTarget(event) {
    const list = U.ceoSessionList;
    const drag = S.ceoSessionDrag;
    const from = Number(drag?.from);
    if (!list || !Number.isInteger(from) || from < 0) return;
    event.preventDefault();
    if (event.dataTransfer) event.dataTransfer.dropEffect = "move";
    const targetIndex = ceoSessionDropIndex(list, event.clientY);
    // 目标没变就不重画指示，避免拖动时列表抖动。
    if (drag.dropIndex === targetIndex) return;
    S.ceoSessionDrag = { ...drag, dropIndex: targetIndex };
    clearCeoSessionDragDecorations();
    const cards = [...list.querySelectorAll("[data-ceo-session-index]")];
    const dragging = cards.find((card) => Number(card.dataset.ceoSessionIndex) === from);
    if (dragging) dragging.classList.add("is-dragging");
    const anchor = cards.find((card) => Number(card.dataset.ceoSessionIndex) === targetIndex) || cards[cards.length - 1];
    if (anchor) anchor.classList.add("is-drop-target");
    list.classList.add("is-drop-zone");
}

function cancelCeoSessionCardDrag() {
    clearCeoSessionDragDecorations();
    S.ceoSessionDrag = null;
}

function finishCeoSessionCardDrag(event) {
    event.preventDefault();
    const drag = S.ceoSessionDrag;
    cancelCeoSessionCardDrag();
    const from = Number(drag?.from);
    if (!Number.isInteger(from) || from < 0) return;
    const ids = visibleCeoSessions()
        .map((item) => String(item?.session_id || "").trim())
        .filter(Boolean);
    if (!ids[from]) return;
    // 位次按“插到锚点之前”计，越过自身时补回一格（与模型链同一算法）。
    const target = Number.isInteger(drag.dropIndex)
        ? (drag.dropIndex > from ? drag.dropIndex - 1 : drag.dropIndex)
        : from;
    if (target === from) return;
    const next = ids.filter((_id, index) => index !== from);
    next.splice(Math.max(0, Math.min(next.length, target)), 0, ids[from]);
    if (next.join("\n") === ids.join("\n")) return;
    setCeoSessionOrder(next);
    // 顺序只在入站时重算，这里立刻按新手顺排一次，否则要到下一次快照才看得见。
    S.ceoLocalSessions = sortCeoSessionsByTime(S.ceoLocalSessions);
    rebuildCeoSessionIndex();
    renderCeoSessions();
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
    if (nextActiveId) {
        const approvalSnapshotEntry = getCeoSessionSnapshotCache(nextActiveId);
        syncCeoApprovalFromSnapshotEntry(nextActiveId, approvalSnapshotEntry, {
            authoritative: true,
            refreshServer: true,
        });
        syncCeoComposerReadonlyState();
        syncCeoPrimaryButton();
    }
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
        const targetChannelId = deriveCeoChannelId(item);
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
                { channel_id: targetChannelId, label: displayChannelGroupLabel(targetChannelId), items: [item] },
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
    if (activeId) {
        const approvalSnapshotEntry = getCeoSessionSnapshotCache(activeId);
        syncCeoApprovalFromSnapshotEntry(activeId, approvalSnapshotEntry, {
            authoritative: true,
            refreshServer: true,
        });
        syncCeoComposerReadonlyState();
        syncCeoPrimaryButton();
    }
}

function applyOptimisticCeoSessionSwitch(sessionId, session = null) {
    const targetId = String(sessionId || "").trim();
    const previousActiveId = activeSessionId();
    if (!targetId || targetId === previousActiveId) {
        return { previousActiveId, switched: false, renderedFromCache: false };
    }
    closeCeoWs();
    // closeCeoWs 会丢掉原会话在途的 sessions.patch;武装一次性豁免,防止切换后迟到的计数补算生成假 unread
    armCeoSessionUnreadExemption(previousActiveId);
    S.activeSessionId = targetId;
    S.activeSessionFamily = String(
        session?.session_family
        || (isChannelSessionItem(session) || targetId.startsWith("china:") || targetId.startsWith("ext:") ? "channel" : "local")
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
        // 新建会话等于离开当前会话:同样武装一条豁免,防止当前会话在离开瞬间被误判 unread
        armCeoSessionUnreadExemption(activeSessionId());
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
        hasRelatedTaskRecords: normalizeInt(relatedPayload?.related_tasks?.total, 0) > 0,
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
    const previousActiveId = activeSessionId();
    S.ceoSessionCatalogBusy = true;
    renderCeoSessions();
    syncCeoPrimaryButton();
    let successCount = 0;
    let failureCount = 0;
    try {
        const payload = await ApiClient.bulkDeleteCeoSessions(ids, { delete_task_records: !!deleteTaskRecords });
        const results = Array.isArray(payload?.results) ? payload.results : [];
        const succeededIds = results
            .map((item) => {
                const result = String(item?.result || "").trim().toLowerCase();
                return (result === "deleted" || result === "cleared") ? String(item?.session_id || "").trim() : "";
            })
            .filter(Boolean);
        successCount = Number.isFinite(Number(payload?.deleted_count)) ? Number(payload.deleted_count) : succeededIds.length;
        failureCount = Number.isFinite(Number(payload?.failed_count))
            ? Number(payload.failed_count)
            : Math.max(0, ids.length - successCount);
        succeededIds.forEach((sessionId) => {
            clearCeoSessionSnapshotCache(sessionId);
            clearCeoComposerDraft(sessionId);
        });
        const nextActiveId = applyCeoSessionsPayload(payload);
        if (succeededIds.includes(previousActiveId)) {
            closeCeoWs();
            resetCeoSessionState({ scrollToLatest: true });
            if (nextActiveId) {
                S.ceoSessionBusy = true;
                initCeoWs();
            }
        }
    } catch (e) {
        showToast({ title: "删除失败", text: e.message || "Unknown error", kind: "error" });
        return;
    } finally {
        S.ceoSessionCatalogBusy = false;
        renderCeoSessions();
        syncCeoPrimaryButton();
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
        const payload = await ApiClient.getCeoSessionsBulkDeleteCheck(selectedIds);
        const deleteCheckItems = Array.isArray(payload?.items) ? payload.items : [];
        const selectedById = new Map(
            selectedItems.map((item) => [String(item?.session_id || "").trim(), item]).filter((entry) => !!entry[0])
        );
        entries = deleteCheckItems.map((deleteCheck) => ({
            item: selectedById.get(String(deleteCheck?.session_id || "").trim()) || null,
            session_id: String(deleteCheck?.session_id || "").trim(),
            deleteCheck,
        }));
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
        checkbox: summary.hasRelatedTaskRecords ? {
            label: summary.checkboxLabel,
            hint: summary.checkboxHint,
            details: summary.checkboxDetails,
            checked: false,
        } : null,
        onConfirm: ({ checked } = {}) => performDeleteSelectedCeoSessions(summary.sessionIds, { deleteTaskRecords: !!checked }),
    });
}

function handleCeoWsUnparsableFrame(error) {
    // 服务端只会发 JSON：解析失败说明这条车道已经不干净，之后每一帧都会被这个异常吞掉，
    // 界面就永久停在半截回合上转圈 —— socket 还是开的，onclose 不触发，自动重连也就
    // 不会发生。所以主动重连一次重取快照；连着几次都还解析不了就不再重试（避免重连风暴）。
    S.ceoWsParseResyncs = Number(S.ceoWsParseResyncs || 0) + 1;
    if (S.ceoWsParseResyncs > CEO_WS_PARSE_RESYNC_LIMIT) {
        console.warn("ceo ws: frames still unparsable after resyncs, stop resyncing", error);
        return;
    }
    console.warn("ceo ws: unparsable frame, resyncing session snapshot", error);
    closeCeoWs();
    initCeoWs();
}

function initCeoWs() {
    const requestedSessionId = String(S.activeSessionId || "").trim();
    if (S.ceoWs && S.ceoWs.readyState <= 1 && S.ceoWs.sessionId === requestedSessionId) return;
    closeCeoWs();
    const token = ++S.ceoWsToken;
    const socket = new WebSocket(ApiClient.getCeoWsUrl(requestedSessionId));
    socket.sessionId = requestedSessionId;
    S.ceoWs = socket;
    socket.onopen = () => {
        if (token !== S.ceoWsToken || S.ceoWs !== socket) return;
        // 编辑重发的"截断→重连→发送"时序依赖 open 信号(whenCeoWsOpen)。
        settleCeoWsOpenWaiters(true);
    };
    S.ceoWs.onmessage = (ev) => {
        if (token !== S.ceoWsToken || S.ceoWs !== socket) return;
        let payload = null;
        try {
            payload = JSON.parse(ev.data);
        } catch (error) {
            handleCeoWsUnparsableFrame(error);
            return;
        }
        const payloadSessionId = String(payload?.session_id || payload?.data?.session_id || "").trim();
        const effectiveSessionId = payloadSessionId || requestedSessionId || activeSessionId();
        if (payload.type === "snapshot.ceo") {
            clearCeoReplyDeltaBuffer(effectiveSessionId);
            S.ceoWsLastErrorCode = "";
            // 整帧快照解析成功 = 这条车道又干净了，重连预算重新充满。
            S.ceoWsParseResyncs = 0;
            const snapshotEntry = {
                session_id: effectiveSessionId,
                messages: payload.data?.messages || [],
                inflight_turn: payload.data?.inflight_turn || null,
                preserved_turn: payload.data?.preserved_turn || null,
            };
            if (effectiveSessionId) {
                setCeoSessionSnapshotCache(effectiveSessionId, snapshotEntry);
            }
            // 快照自己也带会话级 compression：与 ceo.state 谁先到都能认领，恢复不依赖消息顺序。
            adoptCeoContextCompression(effectiveSessionId, payload.data?.compression);
            if (effectiveSessionId === activeSessionId()) {
                renderCeoSnapshot(
                    payload.data?.messages || [],
                    payload.data?.inflight_turn || null,
                    { sessionId: effectiveSessionId, preservedTurn: payload.data?.preserved_turn || null }
                );
            }
            S.ceoSessionBusy = false;
            renderCeoSessions();
            syncCeoSessionActions();
            syncCeoPrimaryButton();
            if (effectiveSessionId === activeSessionId()) {
                syncCeoApprovalFromSnapshotEntry(effectiveSessionId, snapshotEntry, {
                    authoritative: true,
                    refreshServer: true,
                });
            }
        }
        if (payload.type === "error") {
            const code = ApiClient.getErrorCode(payload.data || {});
            const message = ApiClient.friendlyErrorMessage(payload.data || {}, payload.data?.message || "连接失败");
            if (code === "ceo_approval_pending") {
                showToast({
                    title: "等待审批",
                    text: message || "当前存在待审批工具调用，请先完成审批。",
                    kind: "warn",
                    durationMs: 2600,
                });
                if (typeof refreshCeoApprovalFromServer === "function") {
                    void refreshCeoApprovalFromServer(activeSessionId(), { quiet: true });
                }
                return;
            }
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
        if (payload.type === "ceo.turn.interrupt" && effectiveSessionId === activeSessionId()) {
            if (typeof syncCeoApprovalFromInterrupts === "function") {
                syncCeoApprovalFromInterrupts(payload.data?.interrupts || [], effectiveSessionId, { authoritative: true });
            }
        }
        if (payload.type === "ceo.turn.patch") {
            clearCeoReplyDeltaBuffer(effectiveSessionId, {
                turnId: payload.data?.inflight_turn?.turn_id || payload.data?.preserved_turn?.turn_id || "",
            });
            const patchEntry = {
                session_id: effectiveSessionId,
                inflight_turn: payload.data?.inflight_turn || null,
                preserved_turn: payload.data?.preserved_turn || null,
            };
            if (effectiveSessionId === activeSessionId()) {
                patchCeoInflightTurn(payload.data?.preserved_turn || null, {
                    sessionId: effectiveSessionId,
                    cacheField: "preserved_turn",
                });
                patchCeoInflightTurn(payload.data?.inflight_turn || null, {
                    sessionId: effectiveSessionId,
                    cacheField: "inflight_turn",
                });
            } else if (effectiveSessionId) {
                setCeoSessionSnapshotCache(effectiveSessionId, patchEntry);
            }
            if (effectiveSessionId === activeSessionId()) {
                const patchInterrupts = ceoApprovalInterruptsFromSnapshotEntry(patchEntry);
                if (patchInterrupts.length) {
                    syncCeoApprovalFromSnapshotEntry(effectiveSessionId, patchEntry, {
                        authoritative: true,
                        refreshServer: false,
                    });
                }
            }
        }
        if (payload.type === "ceo.edit_fork.gates" && effectiveSessionId === activeSessionId()) {
            applyCeoEditForkGates(payload.data || {}, effectiveSessionId);
        }
        if (payload.type === "ceo.agent.tool" && effectiveSessionId === activeSessionId()) {
            appendCeoToolEvent(payload.data || {});
        }
        if (payload.type === "ceo.tool.reminder" && effectiveSessionId === activeSessionId()) {
            handleCeoToolReminder(payload.data || {});
        }
        if (payload.type === "ceo.error") handleCeoError(payload.data || {});
        if (payload.type === "ceo.internal.ack" && effectiveSessionId === activeSessionId()) {
            handleCeoInternalAck(payload.data || {});
        }
        if (payload.type === "ceo.reply.delta") {
            queueCeoReplyDelta(payload.data || {}, { sessionId: effectiveSessionId });
        }
        if (payload.type === "ceo.reply.final" && effectiveSessionId === activeSessionId()) {
            clearCeoReplyDeltaBuffer(effectiveSessionId, { turnId: payload.data?.turn_id || "" });
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
    if (S.ceoEditResend) {
        // 编辑重发模式:走截断→重连→发送的专用时序,绝不进 follow-up 队列。
        const editState = S.ceoEditResend;
        if (String(editState.sessionId || "") === activeSessionId()) {
            void submitCeoEditResend({ text, uploads });
            return;
        }
        exitCeoEditResendMode({ restoreDraft: false });
    }
    try {
        if (S.ceoTurnActive) {
            enqueueCeoFollowUp(activeSessionId(), { text, uploads });
            sendActiveCeoFollowUpsToRuntime(activeSessionId());
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
const taskContinuationSummary = () => "";
const canRetry = () => false;
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
    if (taskIsUnpassed(task)) return "unpassed";
    return pStatus(task.status) || "unknown";
}

function normalizeTokenUsage(raw) {
    const source = raw && typeof raw === "object" ? raw : {};
    const toInt = (value) => {
        const num = Number(value);
        return Number.isFinite(num) && num >= 0 ? Math.floor(num) : 0;
    };
    const inputTokens = toInt(source.input_tokens);
    const cacheHitTokens = toInt(source.cache_hit_tokens);
    const effectiveInputTokens = Math.max(
        toInt(source.effective_input_tokens),
        inputTokens + cacheHitTokens,
    );
    return {
        tracked: !!source.tracked,
        input_tokens: inputTokens,
        output_tokens: toInt(source.output_tokens),
        cache_hit_tokens: cacheHitTokens,
        effective_input_tokens: effectiveInputTokens,
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
    // 耗时/思考 token 是可选口径：null 表示 provider 未上报（旧记录、非流式请求、
    // 不回传 reasoning_tokens 的 provider），与真实的 0 区分，渲染为 "--"。
    const toOptionalInt = (value) => {
        if (value === null || value === undefined || value === "") return null;
        const num = Number(value);
        return Number.isFinite(num) && num >= 0 ? Math.floor(num) : null;
    };
    return {
        call_index: toInt(source.call_index),
        node_id: String(source.node_id || "").trim(),
        created_at: String(source.created_at || "").trim(),
        prepared_message_count: toInt(source.prepared_message_count),
        prepared_message_chars: toInt(source.prepared_message_chars),
        response_tool_call_count: toInt(source.response_tool_call_count),
        duration_ms: toOptionalInt(source.duration_ms),
        first_token_ms: toOptionalInt(source.first_token_ms),
        thinking_tokens: toOptionalInt(source.thinking_tokens),
        delta_usage: normalizeTokenUsage(source.delta_usage),
        delta_usage_by_model: Array.isArray(source.delta_usage_by_model)
            ? source.delta_usage_by_model.map(normalizeModelTokenUsage)
            : [],
    };
}

function modelCallHitRate(call) {
    const data = normalizeTaskModelCall(call);
    const inputTokens = Number(data.delta_usage.effective_input_tokens || 0);
    const cacheHitTokens = Number(data.delta_usage.cache_hit_tokens || 0);
    if (!inputTokens) return 0;
    return cacheHitTokens / inputTokens;
}

function formatTokenCount(value) {
    const num = Number(value);
    if (!Number.isFinite(num)) return "0";
    return new Intl.NumberFormat("zh-CN").format(Math.max(0, Math.floor(num)));
}

// 耗时列统一口径：<1s 用毫秒，<1min 用秒（一位小数），更长用 mSSs。
// null/undefined/负数/非法值一律 "--"，区分「未上报」与「0ms」。
function formatDurationMs(value) {
    const num = Number(value);
    if (value === null || value === undefined || value === "" || !Number.isFinite(num) || num < 0) return "--";
    if (num < 1000) return `${Math.round(num)}ms`;
    if (num < 60_000) return `${(num / 1000).toFixed(1)}s`;
    const minutes = Math.floor(num / 60_000);
    const seconds = Math.round((num % 60_000) / 1000);
    return `${minutes}m${String(seconds).padStart(2, "0")}s`;
}

function tokenKnownTotal(usage) {
    const data = normalizeTokenUsage(usage);
    return data.effective_input_tokens + data.output_tokens;
}

function taskTokenUsage(task = null, progress = null) {
    return normalizeTokenUsage(task?.token_usage || progress?.token_usage || EMPTY_TOKEN_USAGE());
}

function tokenDisplayUsage(usage) {
    const data = normalizeTokenUsage(usage);
    return {
        ...data,
        input_tokens: data.effective_input_tokens,
    };
}

function taskTokenDisplayUsage(task = null, progress = null) {
    return tokenDisplayUsage(taskTokenUsage(task, progress));
}

function taskTokenSummaryLine(usage) {
    const data = tokenDisplayUsage(usage);
    if (!data.tracked) return "历史任务未统计";
    if (!data.call_count) return "尚未发生模型调用";
    const parts = [
        `总输入 ${formatTokenCount(data.input_tokens)}`,
        `总输出 ${formatTokenCount(data.output_tokens)}`,
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
                    <p id="task-token-summary-text" class="subtitle">任务级 token 消耗统计；窗口打开期间不自动刷新，可点「刷新」手动更新。</p>
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

function memoryStatusLabel(status) {
    const normalized = String(status || "").trim().toLowerCase();
    if (normalized === "processing") return "处理中";
    return "待处理";
}

function memoryOpLabel(op) {
    return String(op || "").trim().toLowerCase() === "delete" ? "删除" : "增加";
}

function memoryFailedCategoryLabel(item) {
    const normalized = String(item?.category || "").trim().toLowerCase();
    if (normalized === "provider_error") return "provider 瞬时错误";
    if (normalized === "protocol") return "协议违规";
    return normalized || "未知类别";
}

function memoryFailedAutoRetryHint(item) {
    const normalized = String(item?.category || "").trim().toLowerCase();
    return normalized === "provider_error"
        ? "队列下一次成功处理后自动重排队尾"
        : "仅支持手动重试";
}

const MEMORY_OP_KINDS = {
    add: { label: "增加", icon: "plus" },
    rewrite: { label: "修改", icon: "pencil" },
    delete: { label: "删除", icon: "trash-2" },
    note_upsert: { label: "更新 Note", icon: "notebook-pen" },
};

// write_mode 把删除并进 rewrite 桶、把多种操作压成 mixed；只有持久化的 changes
// 逐笔记录了操作类型与顺序，因此它是显示的第一来源。
function memoryProcessedOpKinds(item) {
    if (memoryProcessedIsNoChange(item)) return [];
    const kinds = [];
    const pushKind = (kind) => {
        if (MEMORY_OP_KINDS[kind] && !kinds.includes(kind)) kinds.push(kind);
    };
    for (const change of memoryProcessedStructuredChanges(item)) {
        pushKind(String(change?.type || "").trim().toLowerCase());
    }
    if (kinds.length) return kinds;
    const requestOps = [item?.source_op, item?.op]
        .map((value) => String(value || "").trim().toLowerCase());
    if (requestOps.includes("delete")) return ["delete"];
    const writeMode = String(item?.write_mode || "").trim().toLowerCase();
    if (writeMode === "add") return ["add"];
    if (writeMode === "rewrite") return ["rewrite"];
    if (writeMode === "mixed") return ["add", "rewrite"];
    if (requestOps.includes("write")) return ["add"];
    return [];
}

function memoryProcessedNoopReason(item) {
    return String(item?.noop_reason || item?.already_satisfied || "").trim();
}

function memoryProcessedChangePreview(item) {
    return String(item?.change_preview || item?.document_preview || "").trim();
}

function memoryProcessedIsNoChange(item) {
    const normalized = String(item?.status || "").trim().toLowerCase();
    if (normalized === "discarded") return true;
    return !!memoryProcessedNoopReason(item);
}

function memoryProcessedStatusLabel(item) {
    if (memoryProcessedIsNoChange(item)) return "无变更";
    const normalized = String(item?.status || "").trim().toLowerCase();
    if (normalized === "discarded") return "已废弃";
    if (normalized === "applied") return "已应用";
    return normalized || "已处理";
}

function memoryProcessedOpLabel(item) {
    if (memoryProcessedIsNoChange(item)) return "保持";
    const kinds = memoryProcessedOpKinds(item);
    if (kinds.length) return kinds.map((kind) => MEMORY_OP_KINDS[kind].label).join(" / ");
    return memoryProcessedStatusLabel(item);
}

const NOTE_REF_RE = /(?:\bref:|见noteid:)(note_[a-z0-9_]+)\b/g;
const MEMORY_VIEW_POLL_MS = 15000;

function renderMemoryNoteRefChip(noteRef) {
    noteRef = String(noteRef || "").trim();
    if (!noteRef) return "";
    return `<button type="button" class="memory-note-ref-trigger" data-memory-note-ref="${esc(noteRef)}">ref:${esc(noteRef)}</button>`;
}

function renderMemoryNoteRefList(noteRefs) {
    const items = [...new Set((Array.isArray(noteRefs) ? noteRefs : [])
        .map((item) => String(item || "").trim())
        .filter(Boolean))];
    if (!items.length) return "-";
    return `<span class="memory-note-ref-list">${items.map((noteRef) => renderMemoryNoteRefChip(noteRef)).join("")}</span>`;
}

function renderMemoryTextWithNoteRefs(text) {
    const value = String(text || "");
    if (!value) return "";
    NOTE_REF_RE.lastIndex = 0;
    let cursor = 0;
    let html = "";
    let match = NOTE_REF_RE.exec(value);
    while (match) {
        const matchIndex = Number(match.index || 0);
        const matchText = String(match[0] || "");
        const noteRef = String(match[1] || "").trim();
        html += esc(value.slice(cursor, matchIndex));
        html += renderMemoryNoteRefChip(noteRef);
        cursor = matchIndex + matchText.length;
        match = NOTE_REF_RE.exec(value);
    }
    NOTE_REF_RE.lastIndex = 0;
    html += esc(value.slice(cursor));
    return html;
}

function memoryProcessedStructuredChanges(item) {
    return Array.isArray(item?.changes) ? item.changes : [];
}

/* --- 记忆变更 diff 高亮：对"修改后"内容定位变化点 --- */
const MEMORY_CHANGE_DIFF_MAX_TOKENS = 1000;

function tokenizeMemoryChangeText(text) {
    const tokens = [];
    const pattern = /ref:note_[a-z0-9_]+|\s+|[a-zA-Z0-9_]+|[^\s]/g;
    let match = null;
    while ((match = pattern.exec(String(text || ""))) !== null) tokens.push(match[0]);
    return tokens;
}

function memoryChangeDiffOps(originalTokens, modifiedTokens) {
    const m = originalTokens.length;
    const n = modifiedTokens.length;
    if (!m || !n) return null;
    if (m > MEMORY_CHANGE_DIFF_MAX_TOKENS || n > MEMORY_CHANGE_DIFF_MAX_TOKENS) return null;
    const width = n + 1;
    const table = new Uint32Array((m + 1) * width);
    for (let i = m - 1; i >= 0; i--) {
        const row = i * width;
        const nextRow = row + width;
        for (let j = n - 1; j >= 0; j--) {
            if (originalTokens[i] === modifiedTokens[j]) {
                table[row + j] = table[nextRow + j + 1] + 1;
            } else {
                table[row + j] = table[nextRow + j] >= table[row + j + 1]
                    ? table[nextRow + j]
                    : table[row + j + 1];
            }
        }
    }
    const ops = [];
    let i = 0;
    let j = 0;
    while (i < m && j < n) {
        if (originalTokens[i] === modifiedTokens[j]) {
            ops.push({ kind: "same", token: originalTokens[i] });
            i++;
            j++;
        } else if (table[(i + 1) * width + j] >= table[i * width + j + 1]) {
            ops.push({ kind: "del", token: originalTokens[i] });
            i++;
        } else {
            ops.push({ kind: "add", token: modifiedTokens[j] });
            j++;
        }
    }
    while (i < m) {
        ops.push({ kind: "del", token: originalTokens[i] });
        i++;
    }
    while (j < n) {
        ops.push({ kind: "add", token: modifiedTokens[j] });
        j++;
    }
    return ops;
}

function memoryChangeDiffSegments(originalText, modifiedText) {
    const originalTokens = tokenizeMemoryChangeText(originalText);
    const modifiedTokens = tokenizeMemoryChangeText(modifiedText);
    if (!originalTokens.length || !modifiedTokens.length) return null;
    const ops = memoryChangeDiffOps(originalTokens, modifiedTokens);
    if (!ops) return null;
    const segments = [];
    let current = null;
    for (const op of ops) {
        if (op.kind === "del") continue;
        const changed = op.kind === "add";
        if (!current || current.changed !== changed) {
            current = { changed, text: "" };
            segments.push(current);
        }
        current.text += op.token;
    }
    return segments;
}

function renderMemoryDiffHighlighted(originalText, modifiedText) {
    if (!String(originalText || "").trim() || !String(modifiedText || "").trim()) return "";
    const segments = memoryChangeDiffSegments(originalText, modifiedText);
    if (!segments || !segments.some((segment) => segment.changed)) return "";
    return segments.map((segment) => {
        if (!segment.changed) return renderMemoryTextWithNoteRefs(segment.text);
        const leading = (String(segment.text).match(/^\s+/) || [""])[0];
        const trailing = (String(segment.text).match(/\s+$/) || [""])[0];
        const core = segment.text.slice(leading.length, segment.text.length - trailing.length);
        const coreHtml = core ? renderMemoryTextWithNoteRefs(core) : "";
        return coreHtml ? `${esc(leading)}<mark class="memory-change-hl">${coreHtml}</mark>${esc(trailing)}` : "";
    }).join("");
}

function memoryChangeTypeLabel(type) {
    const normalized = String(type || "").trim().toLowerCase();
    if (normalized === "add") return "新增";
    if (normalized === "rewrite") return "修改";
    if (normalized === "delete") return "删除";
    if (normalized === "note_upsert") return "更新 Note";
    return normalized || "变更";
}

function renderMemoryChangeBlock(change) {
    const type = String(change?.type || "").trim().toLowerCase();
    const typeLabel = memoryChangeTypeLabel(type);
    const memoryId = String(change?.memory_id || "").trim();
    const noteRef = String(change?.note_ref || "").trim();
    const content = String(change?.content || "");
    const original = String(change?.original_content || "");
    const originalMissing = Boolean(change?.original_missing);

    let bodyHtml = "";
    if (type === "rewrite") {
        const originalHtml = originalMissing && !original.trim()
            ? `<div class="memory-change-text memory-change-missing">历史批次未保留原文</div>`
            : `<div class="memory-change-text">${renderMemoryTextWithNoteRefs(original) || "-"}</div>`;
        const highlightedModified = renderMemoryDiffHighlighted(original, content);
        const modifiedHtml = content.trim()
            ? (highlightedModified
                ? `<div class="memory-change-text">${highlightedModified}</div>`
                : `<div class="memory-change-text">${renderMemoryTextWithNoteRefs(content)}</div>`)
            : `<div class="memory-change-text memory-change-missing">历史批次未保留修改后内容</div>`;
        bodyHtml = `
            <div class="memory-change-compare">
                <div class="memory-change-col">
                    <div class="memory-change-col-label">原文</div>
                    ${originalHtml}
                </div>
                <div class="memory-change-col">
                    <div class="memory-change-col-label">修改后</div>
                    ${modifiedHtml}
                </div>
            </div>
        `;
    } else if (type === "delete") {
        const deletedHtml = original.trim()
            ? `<div class="memory-change-text">${renderMemoryTextWithNoteRefs(original)}</div>`
            : `<div class="memory-change-text memory-change-missing">${originalMissing ? "历史批次未保留被删内容，仅记录了 ID" : "-"}</div>`;
        bodyHtml = `
            <div class="memory-change-col">
                <div class="memory-change-col-label">删除的内容</div>
                ${deletedHtml}
            </div>
        `;
    } else if (type === "note_upsert") {
        bodyHtml = `
            <div class="memory-change-col">
                <div class="memory-change-col-label">Note 内容</div>
                <div class="memory-change-text">${renderMemoryTextWithNoteRefs(content) || "-"}</div>
            </div>
        `;
    } else {
        bodyHtml = `
            <div class="memory-change-col">
                <div class="memory-change-col-label">新增内容</div>
                <div class="memory-change-text">${renderMemoryTextWithNoteRefs(content) || "-"}</div>
            </div>
        `;
    }

    const idChip = type === "note_upsert" && noteRef
        ? `<span class="policy-chip neutral">ref:${esc(noteRef)}</span>`
        : (memoryId ? `<span class="policy-chip neutral">${esc(memoryId)}</span>` : "");

    return `
        <div class="memory-change-block" data-change-type="${esc(type)}">
            <div class="memory-change-head">
                <span class="memory-change-type">${esc(typeLabel)}</span>
                ${idChip}
            </div>
            ${bodyHtml}
        </div>
    `;
}

function renderMemoryChangeList(changes) {
    const items = Array.isArray(changes) ? changes : [];
    if (!items.length) return "";
    return `<div class="memory-change-list">${items.map((change) => renderMemoryChangeBlock(change)).join("")}</div>`;
}

function memoryPreviewText(text, maxChars = 240) {
    const value = String(text || "").trim();
    if (!value) return "";
    if (value.length <= maxChars) return value;
    return `${value.slice(0, maxChars).trimEnd()}…`;
}

function renderMemoryPreviewBlock(label, text) {
    const preview = memoryPreviewText(text);
    if (!preview) return "";
    return `
        <div class="memory-card-preview">
            <div class="memory-card-preview-label">${esc(label || "预览")}</div>
            <div class="memory-card-preview-text">${esc(preview)}</div>
        </div>
    `;
}

function ensureMemoryNotePreviewUi() {
    if (U.memoryNoteBackdrop && U.memoryNoteDrawer) return;
    const host = U.viewMemory || document.body;
    const backdrop = document.createElement("div");
    backdrop.id = "memory-note-preview-backdrop";
    backdrop.className = "detail-backdrop";
    backdrop.setAttribute("aria-hidden", "true");
    const drawer = document.createElement("section");
    drawer.id = "memory-note-preview-drawer";
    drawer.className = "panel detail-drawer memory-note-preview-drawer";
    drawer.setAttribute("role", "dialog");
    drawer.setAttribute("aria-modal", "true");
    drawer.setAttribute("aria-hidden", "true");
    drawer.setAttribute("aria-labelledby", "memory-note-preview-title");
    drawer.tabIndex = -1;
    drawer.innerHTML = `
        <div class="detail-modal-header">
            <div class="memory-note-preview-head">
                <h2 id="memory-note-preview-title">Note 预览</h2>
                <p id="memory-note-preview-subtitle" class="subtitle"></p>
            </div>
            <div class="memory-note-head-actions">
                <button type="button" class="toolbar-btn ghost" data-memory-note-edit-toggle title="进入编辑模式修改 note 正文">编辑</button>
                <button type="button" class="toolbar-btn ghost" data-memory-note-close data-modal-close>关闭</button>
            </div>
        </div>
        <div class="detail-modal-body">
            <div class="memory-note-preview-shell">
                <div id="memory-note-preview-status" class="memory-note-preview-status"></div>
                <pre id="memory-note-preview-body" class="memory-note-preview-body"></pre>
                <textarea id="memory-note-edit-body" class="memory-note-edit-body" rows="12" hidden aria-label="note 正文编辑"></textarea>
            </div>
        </div>
        <div id="memory-note-edit-footer" class="detail-modal-footer memory-note-edit-footer" hidden>
            <span class="memory-note-edit-hint">保存前会再次确认；note 不提供删除入口。</span>
            <div class="memory-note-edit-buttons">
                <button type="button" class="toolbar-btn ghost" data-memory-note-edit-cancel>取消</button>
                <button type="button" class="toolbar-btn success" data-memory-note-edit-save>保存修改</button>
            </div>
        </div>
    `;
    host.appendChild(backdrop);
    host.appendChild(drawer);
    U.memoryNoteBackdrop = backdrop;
    U.memoryNoteDrawer = drawer;
    U.memoryNoteTitle = drawer.querySelector("#memory-note-preview-title");
    U.memoryNoteSubtitle = drawer.querySelector("#memory-note-preview-subtitle");
    U.memoryNoteStatus = drawer.querySelector("#memory-note-preview-status");
    U.memoryNoteBody = drawer.querySelector("#memory-note-preview-body");
    U.memoryNoteEditBody = drawer.querySelector("#memory-note-edit-body");
    U.memoryNoteEditFooter = drawer.querySelector("#memory-note-edit-footer");
    U.memoryNoteEditToggle = drawer.querySelector("[data-memory-note-edit-toggle]");
    U.memoryNoteEditSave = drawer.querySelector("[data-memory-note-edit-save]");
    U.memoryNoteClose = drawer.querySelector("[data-memory-note-close]");
    U.memoryNoteClose?.addEventListener("click", () => closeMemoryNotePreview());
    U.memoryNoteBackdrop?.addEventListener("click", () => closeMemoryNotePreview());
    U.memoryNoteEditToggle?.addEventListener("click", () => toggleMemoryNoteEditMode());
    drawer.querySelector("[data-memory-note-edit-cancel]")?.addEventListener("click", () => {
        S.memoryNotePreview.editMode = false;
        renderMemoryNotePreview();
    });
    U.memoryNoteEditSave?.addEventListener("click", () => requestMemoryNoteSave());
    U.memoryNoteEditBody?.addEventListener("input", () => {
        S.memoryNotePreview.editBody = U.memoryNoteEditBody.value || "";
    });
}

function renderMemoryNotePreview() {
    ensureMemoryNotePreviewUi();
    const noteRef = String(S.memoryNotePreview.ref || "").trim();
    const editable = !!S.memoryNotePreview.editable;
    const editMode = !!S.memoryNotePreview.editMode && editable;
    if (U.memoryNoteTitle) U.memoryNoteTitle.textContent = noteRef ? `Note 预览 · ${noteRef}` : "Note 预览";
    if (U.memoryNoteSubtitle) {
        U.memoryNoteSubtitle.textContent = editMode ? "编辑 note 正文，保存前会再次确认。" : "";
    }
    if (U.memoryNoteStatus) {
        const errorText = String(S.memoryNotePreview.error || "").trim();
        if (S.memoryNotePreview.busy) {
            U.memoryNoteStatus.textContent = "正在加载 note 正文...";
            U.memoryNoteStatus.hidden = false;
            U.memoryNoteStatus.classList.remove("is-error");
        } else if (errorText) {
            U.memoryNoteStatus.textContent = errorText;
            U.memoryNoteStatus.hidden = false;
            U.memoryNoteStatus.classList.add("is-error");
        } else {
            U.memoryNoteStatus.textContent = noteRef ? `当前预览：${noteRef}` : "";
            U.memoryNoteStatus.hidden = !noteRef;
            U.memoryNoteStatus.classList.remove("is-error");
        }
    }
    if (U.memoryNoteBody) {
        U.memoryNoteBody.hidden = editMode;
        setTextPreservingScroll(
            U.memoryNoteBody,
            S.memoryNotePreview.busy
                ? ""
                : String(S.memoryNotePreview.body || "").trim() || "当前 note 没有正文。",
        );
    }
    if (U.memoryNoteEditBody) {
        U.memoryNoteEditBody.hidden = !editMode;
        if (editMode && document.activeElement !== U.memoryNoteEditBody) {
            U.memoryNoteEditBody.value = String(S.memoryNotePreview.editBody || "");
        }
    }
    if (U.memoryNoteEditFooter) U.memoryNoteEditFooter.hidden = !editMode;
    if (U.memoryNoteEditToggle) {
        U.memoryNoteEditToggle.hidden = !editable;
        U.memoryNoteEditToggle.textContent = editMode ? "完成" : "编辑";
        U.memoryNoteEditToggle.disabled = !!S.memoryNotePreview.busy || !!S.memoryNotePreview.saving;
    }
    if (U.memoryNoteEditSave) U.memoryNoteEditSave.disabled = !!S.memoryNotePreview.saving;
    setDrawerOpen(U.memoryNoteBackdrop, U.memoryNoteDrawer, !!S.memoryNotePreview.open);
}

function toggleMemoryNoteEditMode() {
    const preview = S.memoryNotePreview;
    if (preview.busy || preview.saving || !preview.editable) return;
    preview.editMode = !preview.editMode;
    if (preview.editMode) {
        preview.editBody = String(preview.body || "");
    }
    renderMemoryNotePreview();
    if (preview.editMode) U.memoryNoteEditBody?.focus();
}

function requestMemoryNoteSave() {
    const preview = S.memoryNotePreview;
    const ref = String(preview.ref || "").trim();
    if (!ref || preview.saving || !preview.editMode || !preview.editable) return;
    const body = String(U.memoryNoteEditBody?.value ?? preview.editBody ?? "");
    if (!body.trim()) {
        showToast({ title: "内容不能为空", text: "note 正文为必填项。", kind: "warn" });
        return;
    }
    openConfirm({
        title: "保存 note 修改",
        text: `确定保存对 ${ref} 的修改吗？`,
        confirmLabel: "保存",
        confirmKind: "danger",
        onConfirm: () => void runMemoryNoteSave(ref, body),
    });
}

async function runMemoryNoteSave(ref, body) {
    const preview = S.memoryNotePreview;
    if (preview.saving) return;
    preview.saving = true;
    renderMemoryNotePreview();
    try {
        await ApiClient.updateMemoryNote(ref, body, "manual-ui");
        showToast({ title: "已保存", text: `note ${ref} 修改已保存。`, kind: "success" });
        preview.editMode = false;
        preview.body = body;
        preview.editBody = "";
    } catch (error) {
        showToast({ title: "保存失败", text: error?.message || "note 修改未成功，请稍后重试", kind: "error", durationMs: 5000 });
    } finally {
        preview.saving = false;
        renderMemoryNotePreview();
    }
}

function closeMemoryNotePreview() {
    S.memoryNotePreview.open = false;
    S.memoryNotePreview.editMode = false;
    S.memoryNotePreview.editBody = "";
    renderMemoryNotePreview();
}

async function openMemoryNotePreview(noteRef, options = {}) {
    const normalizedRef = String(noteRef || "").trim();
    if (!normalizedRef) return;
    ensureMemoryNotePreviewUi();
    S.memoryNotePreview.open = true;
    S.memoryNotePreview.busy = true;
    S.memoryNotePreview.ref = normalizedRef;
    S.memoryNotePreview.body = "";
    S.memoryNotePreview.error = "";
    S.memoryNotePreview.editMode = false;
    S.memoryNotePreview.editBody = "";
    // 已处理批次的历史入口（变更内容等）打开的 note 窗只读，不提供编辑
    S.memoryNotePreview.editable = options.editable !== false;
    S.memoryNotePreview.requestToken += 1;
    const requestToken = S.memoryNotePreview.requestToken;
    renderMemoryNotePreview();
    try {
        const item = await ApiClient.getMemoryNote(noteRef);
        if (requestToken !== S.memoryNotePreview.requestToken) return;
        S.memoryNotePreview.ref = String(item?.ref || normalizedRef).trim() || normalizedRef;
        S.memoryNotePreview.body = String(item?.body || "");
        S.memoryNotePreview.error = "";
    } catch (error) {
        if (requestToken !== S.memoryNotePreview.requestToken) return;
        S.memoryNotePreview.error = error?.message || "读取记忆 note 失败";
        showToast({ title: "Note 预览失败", text: S.memoryNotePreview.error, kind: "error" });
    } finally {
        if (requestToken !== S.memoryNotePreview.requestToken) return;
        S.memoryNotePreview.busy = false;
        renderMemoryNotePreview();
    }
}

function ensureMemoryDetailPreviewUi() {
    if (U.memoryDetailBackdrop && U.memoryDetailDrawer) return;
    const host = U.viewMemory || document.body;
    const backdrop = document.createElement("div");
    backdrop.id = "memory-detail-preview-backdrop";
    backdrop.className = "detail-backdrop";
    backdrop.setAttribute("aria-hidden", "true");
    const drawer = document.createElement("section");
    drawer.id = "memory-detail-preview-drawer";
    drawer.className = "panel detail-drawer memory-detail-preview-drawer";
    drawer.setAttribute("role", "dialog");
    drawer.setAttribute("aria-modal", "true");
    drawer.setAttribute("aria-hidden", "true");
    drawer.setAttribute("aria-labelledby", "memory-detail-preview-title");
    drawer.tabIndex = -1;
    drawer.innerHTML = `
        <div class="detail-modal-header">
            <div class="memory-detail-preview-head">
                <h2 id="memory-detail-preview-title">只读记忆详情</h2>
                <p id="memory-detail-preview-subtitle" class="subtitle">完整内容仅供查看，不支持编辑或保存。</p>
            </div>
            <button type="button" class="toolbar-btn ghost" data-memory-detail-close data-modal-close>关闭</button>
        </div>
        <div class="detail-modal-body">
            <div class="memory-detail-preview-shell">
                <div id="memory-detail-preview-meta" class="memory-detail-preview-groups"></div>
                <section id="memory-detail-preview-primary-section" class="memory-detail-preview-text-section">
                    <div id="memory-detail-preview-primary-title" class="memory-detail-preview-text-title">正文内容</div>
                    <div id="memory-detail-preview-primary" class="memory-detail-preview-text-block memory-card-body-text-rich"></div>
                </section>
                <section id="memory-detail-preview-secondary-section" class="memory-detail-preview-text-section" hidden>
                    <div id="memory-detail-preview-secondary-title" class="memory-detail-preview-text-title">补充信息</div>
                    <div id="memory-detail-preview-secondary" class="memory-detail-preview-text-block memory-card-body-text-rich"></div>
                </section>
            </div>
        </div>
        <div id="memory-detail-preview-actions" class="detail-modal-footer memory-detail-preview-actions" hidden>
            <span id="memory-detail-preview-actions-hint" class="memory-detail-actions-hint"></span>
            <div class="memory-detail-actions-buttons">
                <button type="button" class="toolbar-btn danger" data-memory-failed-action="discard">放弃</button>
                <button type="button" class="toolbar-btn success" data-memory-failed-action="retry">
                    <i data-lucide="rotate-ccw" aria-hidden="true"></i>
                    重试（重新入队尾）
                </button>
            </div>
        </div>
    `;
    host.appendChild(backdrop);
    host.appendChild(drawer);
    U.memoryDetailBackdrop = backdrop;
    U.memoryDetailDrawer = drawer;
    U.memoryDetailTitle = drawer.querySelector("#memory-detail-preview-title");
    U.memoryDetailSubtitle = drawer.querySelector("#memory-detail-preview-subtitle");
    U.memoryDetailMeta = drawer.querySelector("#memory-detail-preview-meta");
    U.memoryDetailPrimarySection = drawer.querySelector("#memory-detail-preview-primary-section");
    U.memoryDetailPrimaryTitle = drawer.querySelector("#memory-detail-preview-primary-title");
    U.memoryDetailPrimary = drawer.querySelector("#memory-detail-preview-primary");
    U.memoryDetailSecondarySection = drawer.querySelector("#memory-detail-preview-secondary-section");
    U.memoryDetailSecondaryTitle = drawer.querySelector("#memory-detail-preview-secondary-title");
    U.memoryDetailSecondary = drawer.querySelector("#memory-detail-preview-secondary");
    U.memoryDetailActions = drawer.querySelector("#memory-detail-preview-actions");
    U.memoryDetailActionsHint = drawer.querySelector("#memory-detail-preview-actions-hint");
    U.memoryDetailClose = drawer.querySelector("[data-memory-detail-close]");
    U.memoryDetailClose?.addEventListener("click", () => closeMemoryDetailPreview());
    U.memoryDetailBackdrop?.addEventListener("click", () => closeMemoryDetailPreview());
    U.memoryDetailDrawer?.addEventListener("click", (e) => {
        if (!(e.target instanceof Element)) return;
        const failedAction = e.target.closest("[data-memory-failed-action]");
        if (failedAction) {
            e.preventDefault();
            e.stopPropagation();
            if (failedAction.disabled) return;
            const failedId = String(S.memoryDetailPreview?.key || "").trim();
            const action = String(failedAction.dataset?.memoryFailedAction || "").trim();
            if (!failedId || !action) return;
            if (action === "discard") {
                requestMemoryFailedDiscard(failedId);
            } else {
                void runMemoryFailedAction("retry", failedId);
            }
            return;
        }
        const noteTrigger = e.target.closest("[data-memory-note-ref]");
        if (!noteTrigger) return;
        e.preventDefault();
        e.stopPropagation();
        // 已处理批次详情（含变更内容）是历史视图：其 note 窗只读，不提供编辑
        const readOnlyNote = String(S.memoryDetailPreview?.kind || "").trim() === "processed";
        void openMemoryNotePreview(noteTrigger.dataset.memoryNoteRef || "", { editable: !readOnlyNote });
    });
}

function renderMemoryDetailPreview() {
    ensureMemoryDetailPreviewUi();
    const preview = S.memoryDetailPreview || {};
    if (U.memoryDetailTitle) U.memoryDetailTitle.textContent = String(preview.title || "").trim() || "只读记忆详情";
    if (U.memoryDetailSubtitle) U.memoryDetailSubtitle.textContent = String(preview.subtitle || "").trim() || "完整内容仅供查看，不支持编辑或保存。";
    if (U.memoryDetailMeta) {
        const explicitGroups = Array.isArray(preview.groups) ? preview.groups : [];
        const fallbackFields = Array.isArray(preview.fields) ? preview.fields : [];
        const normalizedGroups = explicitGroups.length
            ? explicitGroups
            : (preview.kind === "processed"
                ? [
                    { title: "基础信息", items: fallbackFields },
                ]
                : [
                    { title: "基础信息", items: fallbackFields.slice(0, 4) },
                    { title: "运行信息", items: fallbackFields.slice(4) },
                ]);
        U.memoryDetailMeta.innerHTML = normalizedGroups.map((group) => {
            const items = Array.isArray(group?.items) ? group.items : [];
            return `
                <section class="memory-detail-preview-group">
                    <div class="memory-detail-preview-group-head">
                        <div class="memory-detail-preview-group-title">${esc(String(group?.title || "").trim() || "-")}</div>
                    </div>
                    <div class="memory-detail-preview-group-body">
                        ${items.map((item) => `
                            <div class="memory-detail-preview-field">
                                <div class="memory-detail-preview-field-label">${esc(String(item?.label || "").trim() || "-")}</div>
                                <div class="memory-detail-preview-field-value">${esc(String(item?.value || "").trim() || "-")}</div>
                            </div>
                        `).join("")}
                    </div>
                </section>
            `;
        }).join("");
    }
    if (U.memoryDetailPrimaryTitle) {
        U.memoryDetailPrimaryTitle.textContent = preview.kind === "processed" ? "原始请求内容" : "请求正文";
    }
    if (U.memoryDetailPrimary) {
        const primaryText = String(preview.primaryText || "").trim() || "当前没有可显示的正文。";
        setInnerHtmlPreservingScroll(U.memoryDetailPrimary, renderMemoryTextWithNoteRefs(primaryText));
    }
    const secondaryText = String(preview.secondaryText || "").trim();
    const changeListHtml = renderMemoryChangeList(preview.changes);
    const reconstructedHint = changeListHtml && preview.changesReconstructed
        ? `<div class="memory-change-reconstructed-hint">以下变更由历史摘要重建；当时的原文与删除内容未被保留。</div>`
        : "";
    const hasSecondary = Boolean(changeListHtml) || Boolean(secondaryText);
    if (U.memoryDetailSecondarySection) U.memoryDetailSecondarySection.hidden = !hasSecondary;
    if (U.memoryDetailSecondaryTitle) {
        U.memoryDetailSecondaryTitle.textContent = String(preview.secondaryTitle || "").trim()
            || (preview.kind === "processed" ? "变更内容" : "最近错误");
    }
    if (U.memoryDetailSecondary) {
        if (changeListHtml) {
            setInnerHtmlPreservingScroll(U.memoryDetailSecondary, reconstructedHint + changeListHtml);
        } else {
            setInnerHtmlPreservingScroll(U.memoryDetailSecondary, secondaryText ? renderMemoryTextWithNoteRefs(secondaryText) : "");
        }
    }
    const shell = U.memoryDetailDrawer?.querySelector(".memory-detail-preview-shell") || null;
    if (shell && U.memoryDetailSecondarySection && U.memoryDetailPrimarySection) {
        const wantsSecondaryFirst = preview.kind === "processed" || preview.kind === "failed";
        const secondaryFirstNow = U.memoryDetailSecondarySection.nextElementSibling === U.memoryDetailPrimarySection;
        // 仅在顺序确实需要改变时才移动节点：每次轮询都 insertBefore/appendChild
        // 会重建 DOM 子树，把变更内容滚动条重置回顶部。
        if (wantsSecondaryFirst && !secondaryFirstNow) {
            shell.insertBefore(U.memoryDetailSecondarySection, U.memoryDetailPrimarySection);
        } else if (!wantsSecondaryFirst && secondaryFirstNow) {
            shell.appendChild(U.memoryDetailSecondarySection);
        }
    }
    if (U.memoryDetailActions) {
        const isFailedOpen = !!preview.open && preview.kind === "failed";
        U.memoryDetailActions.hidden = !isFailedOpen;
        if (isFailedOpen) {
            const failedRecord = Array.isArray(S.memoryFailedItems)
                ? S.memoryFailedItems.find((item) => String(item?.failed_id || "").trim() === String(preview.key || "").trim())
                : null;
            const actionBusy = String(S.memoryFailedActionBusy || "").trim();
            if (U.memoryDetailActionsHint) {
                U.memoryDetailActionsHint.textContent = S.memoryFailedMutationsEnabled
                    ? (failedRecord ? memoryFailedAutoRetryHint(failedRecord) : "")
                    : "服务端未启用 G3KU_ENABLE_MEMORY_ADMIN_MUTATIONS，重试 / 放弃不可用";
            }
            U.memoryDetailActions.querySelectorAll("[data-memory-failed-action]").forEach((button) => {
                button.disabled = !S.memoryFailedMutationsEnabled || !!actionBusy || !failedRecord;
            });
        }
    }
    setDrawerOpen(U.memoryDetailBackdrop, U.memoryDetailDrawer, !!preview.open);
}

function closeMemoryDetailPreview() {
    S.memoryDetailPreview.open = false;
    renderMemoryDetailPreview();
}

function openMemoryDetailPreview(kind, key) {
    const normalizedKind = String(kind || "").trim();
    const normalizedKey = String(key || "").trim();
    if (!normalizedKind || !normalizedKey) return;
    const isProcessed = normalizedKind === "processed";
    const isFailed = normalizedKind === "failed";
    const source = isProcessed
        ? (Array.isArray(S.memoryProcessedItems) ? S.memoryProcessedItems.find((item) => String(item?.batch_id || "").trim() === normalizedKey) : null)
        : isFailed
            ? (Array.isArray(S.memoryFailedItems) ? S.memoryFailedItems.find((item) => String(item?.failed_id || "").trim() === normalizedKey) : null)
            : (Array.isArray(S.memoryQueueItems) ? S.memoryQueueItems.find((item) => String(item?.request_id || "").trim() === normalizedKey) : null);
    if (!source) return;
    const usage = source?.usage && typeof source.usage === "object" ? source.usage : {};
    const payloadTexts = Array.isArray(source?.payload_texts) ? source.payload_texts : [];
    const noopReason = isProcessed ? memoryProcessedNoopReason(source) : "";
    const failedUsage = isFailed && source?.usage_total && typeof source.usage_total === "object" ? source.usage_total : {};
    const failedPayloads = isFailed && Array.isArray(source?.items)
        ? source.items.map((item) => String(item?.payload_text || "")).filter((text) => text.trim())
        : [];
    const fields = isProcessed
        ? [
            { label: "批次", value: normalizedKey },
            { label: "状态", value: memoryProcessedStatusLabel(source) || "-" },
            { label: "操作", value: memoryProcessedOpLabel(source) || "-" },
            { label: "处理时间", value: formatCompactTime(source?.processed_at) || String(source?.processed_at || "-") },
            { label: "模型链", value: (Array.isArray(source?.model_chain) ? source.model_chain.join(" -> ") : "") || "-" },
            { label: "请求数", value: String(source?.request_count || payloadTexts.length || 0) },
            { label: "输入", value: String(usage.input_tokens || 0) },
            { label: "输出", value: String(usage.output_tokens || 0) },
        ]
        : isFailed
            ? [
                { label: "记录", value: normalizedKey },
                { label: "失败类别", value: memoryFailedCategoryLabel(source) },
                { label: "操作", value: memoryOpLabel(source?.op) },
                { label: "请求数", value: String(Array.isArray(source?.request_ids) ? source.request_ids.length : 0) },
                { label: "首次停车", value: formatCompactTime(source?.first_parked_at) || String(source?.first_parked_at || "-") },
                { label: "最近停车", value: formatCompactTime(source?.parked_at) || String(source?.parked_at || "-") },
                { label: "停车次数", value: String(source?.park_count || 0) },
                { label: "自动重排", value: String(source?.auto_requeue_count || 0) },
                { label: "手动重试", value: String(source?.manual_retry_count || 0) },
                { label: "累计输入", value: String(failedUsage.input_tokens || 0) },
                { label: "累计输出", value: String(failedUsage.output_tokens || 0) },
                { label: "重试策略", value: memoryFailedAutoRetryHint(source) },
            ]
            : [
                { label: "请求", value: normalizedKey },
                { label: "状态", value: memoryStatusLabel(source?.status) || "-" },
                { label: "入队时间", value: formatCompactTime(source?.created_at) || String(source?.created_at || "-") },
                { label: "开始处理", value: formatCompactTime(source?.processing_started_at) || String(source?.processing_started_at || "-") },
                { label: "决策源", value: String(source?.decision_source || "").trim() || "-" },
                { label: "触发来源", value: String(source?.trigger_source || "").trim() || "-" },
                { label: "下次重试", value: formatCompactTime(source?.retry_after) || String(source?.retry_after || "-") },
            ];
    S.memoryDetailPreview = {
        open: true,
        kind: normalizedKind,
        key: normalizedKey,
        title: isFailed ? "失败记忆详情" : "只读记忆详情",
        groups: isFailed
            ? [
                { title: "基础信息", items: fields.slice(0, 4) },
                { title: "停车与重试", items: fields.slice(4, 9) },
                { title: "累计成本与策略", items: fields.slice(9) },
            ]
            : [],
        subtitle: isProcessed
            ? `已处理批次 ${normalizedKey}`
            : isFailed
                ? `失败停车批次 ${normalizedKey}`
                : `队列请求 ${normalizedKey}`,
        fields,
        primaryText: isProcessed
            ? payloadTexts.join("\n\n---\n\n")
            : isFailed
                ? failedPayloads.join("\n\n---\n\n")
                : String(source?.payload_text || ""),
        secondaryTitle: isProcessed
            ? (noopReason ? "无变更原因" : "变更内容")
            : isFailed
                ? "错误历史"
                : "最近错误",
        secondaryText: isProcessed
            ? String(noopReason || memoryProcessedChangePreview(source) || "")
            : isFailed
                ? memoryFailedErrorHistoryText(source)
                : String(source?.last_error_text || ""),
        changes: isProcessed ? memoryProcessedStructuredChanges(source) : [],
        changesReconstructed: isProcessed ? Boolean(source?.changes_reconstructed) : false,
    };
    renderMemoryDetailPreview();
}

function ensureMemoryBrowserUi() {
    if (U.memoryBrowserBackdrop && U.memoryBrowserDrawer) return;
    const host = U.viewMemory || document.body;
    const backdrop = document.createElement("div");
    backdrop.id = "memory-browser-backdrop";
    backdrop.className = "detail-backdrop";
    backdrop.setAttribute("aria-hidden", "true");
    const drawer = document.createElement("section");
    drawer.id = "memory-browser-drawer";
    drawer.className = "panel detail-drawer memory-browser-drawer";
    drawer.setAttribute("role", "dialog");
    drawer.setAttribute("aria-modal", "true");
    drawer.setAttribute("aria-hidden", "true");
    drawer.setAttribute("aria-labelledby", "memory-browser-title");
    drawer.tabIndex = -1;
    drawer.innerHTML = `
        <div class="detail-modal-header">
            <div class="memory-browser-head">
                <h2 id="memory-browser-title">当前记忆</h2>
                <p id="memory-browser-subtitle" class="subtitle">数据来自 sqlite 持久化存储。</p>
            </div>
            <div class="memory-browser-head-actions">
                <button type="button" class="toolbar-btn ghost" id="memory-browser-edit-toggle" title="进入编辑模式：批量选择、修改与删除记忆">编辑</button>
                <button type="button" class="toolbar-btn ghost" data-memory-browser-close data-modal-close>关闭</button>
            </div>
        </div>
        <div class="detail-modal-body">
            <div class="memory-browser-toolbar">
                <input id="memory-browser-search" class="resource-search memory-browser-search-input" type="search" placeholder="搜索关键词（内容 / ID / 来源）" aria-label="搜索当前记忆" />
            </div>
            <div id="memory-browser-bulk-bar" class="memory-browser-bulk-bar" hidden>
                <label class="memory-browser-select-all">
                    <input type="checkbox" id="memory-browser-select-all" aria-label="全选当前筛选结果" />
                    <span>全选</span>
                </label>
                <span id="memory-browser-selected-count" class="memory-browser-selected-count">已选 0 条</span>
                <button type="button" id="memory-browser-bulk-delete" class="toolbar-btn danger memory-browser-bulk-delete" disabled>
                    <i data-lucide="trash-2" aria-hidden="true"></i>
                    删除选中
                </button>
            </div>
            <div class="memory-browser-table-wrap">
                <table class="memory-browser-table">
                    <thead>
                        <tr>
                            <th id="memory-browser-th-select" class="memory-browser-col-select" hidden></th>
                            <th class="sortable" data-memory-sort="created_at" tabindex="0" role="button">创建时间<span class="sort-ind" data-sort-ind="created_at"></span></th>
                            <th class="sortable" data-memory-sort="refresh_count" tabindex="0" role="button">刷新值<span class="sort-ind" data-sort-ind="refresh_count"></span></th>
                            <th class="sortable" data-memory-sort="passed_count" tabindex="0" role="button">通过次数<span class="sort-ind" data-sort-ind="passed_count"></span></th>
                            <th>来源</th>
                            <th>ID</th>
                            <th>记忆内容</th>
                            <th id="memory-browser-th-actions" class="memory-browser-col-actions" hidden>操作</th>
                        </tr>
                    </thead>
                    <tbody id="memory-browser-tbody"></tbody>
                </table>
            </div>
            <div id="memory-browser-status" class="resource-page-indicator" aria-live="polite"></div>
        </div>
        <div id="memory-browser-edit-backdrop" class="memory-browser-edit-backdrop" hidden aria-hidden="true"></div>
        <section id="memory-browser-edit-dialog" class="panel memory-browser-edit-dialog" role="dialog" aria-modal="true" aria-hidden="true" aria-labelledby="memory-browser-edit-title" tabindex="-1" hidden>
            <div class="detail-modal-header">
                <div class="memory-browser-head">
                    <h2 id="memory-browser-edit-title">编辑记忆</h2>
                    <p id="memory-browser-edit-subtitle" class="subtitle">保存前会再次确认；修改会同步重建 MEMORY.md 镜像。</p>
                </div>
                <button type="button" class="toolbar-btn ghost" data-memory-browser-edit-close>取消</button>
            </div>
            <div class="detail-modal-body memory-browser-edit-body">
                <label class="memory-browser-edit-field" for="memory-browser-edit-memory-id">
                    <span>记忆 ID</span>
                    <input id="memory-browser-edit-memory-id" type="text" readonly />
                </label>
                <label class="memory-browser-edit-field" for="memory-browser-edit-body">
                    <span>记忆内容</span>
                    <textarea id="memory-browser-edit-body" rows="6" placeholder="记忆正文（必填）"></textarea>
                </label>
                <label class="memory-browser-edit-field" for="memory-browser-edit-minimal">
                    <span>最小记忆（minimal_memory，可留空保持不变）</span>
                    <input id="memory-browser-edit-minimal" type="text" placeholder="条件->要求关键词" />
                </label>
            </div>
            <div class="detail-modal-footer memory-browser-edit-footer">
                <button type="button" class="toolbar-btn ghost" data-memory-browser-edit-cancel>取消</button>
                <button type="button" class="toolbar-btn success" data-memory-browser-edit-save>保存修改</button>
            </div>
        </section>
        <div id="memory-delete-backdrop" class="memory-browser-edit-backdrop" hidden aria-hidden="true"></div>
        <section id="memory-delete-dialog" class="panel memory-browser-edit-dialog memory-delete-dialog" role="dialog" aria-modal="true" aria-hidden="true" aria-labelledby="memory-delete-title" tabindex="-1" hidden>
            <div class="detail-modal-header">
                <div class="memory-browser-head">
                    <h2 id="memory-delete-title">删除记忆</h2>
                    <p id="memory-delete-subtitle" class="subtitle">删除后 MEMORY.md 镜像会同步重建，操作不可撤销。</p>
                </div>
                <button type="button" class="toolbar-btn ghost" data-memory-delete-close>取消</button>
            </div>
            <div class="detail-modal-body memory-delete-body">
                <label class="memory-delete-master">
                    <input type="checkbox" data-delete-notes-master aria-label="同步删除关联笔记" />
                    <span>同步删除关联笔记</span>
                </label>
                <div id="memory-delete-notes" class="memory-delete-notes"></div>
            </div>
            <div class="detail-modal-footer memory-browser-edit-footer">
                <button type="button" class="toolbar-btn ghost" data-memory-delete-cancel>取消</button>
                <button type="button" class="toolbar-btn danger" data-memory-delete-confirm>
                    <i data-lucide="trash-2" aria-hidden="true"></i>
                    删除
                </button>
            </div>
        </section>
    `;
    host.appendChild(backdrop);
    host.appendChild(drawer);
    U.memoryBrowserBackdrop = backdrop;
    U.memoryBrowserDrawer = drawer;
    U.memoryBrowserTitle = drawer.querySelector("#memory-browser-title");
    U.memoryBrowserSubtitle = drawer.querySelector("#memory-browser-subtitle");
    U.memoryBrowserSearch = drawer.querySelector("#memory-browser-search");
    U.memoryBrowserTbody = drawer.querySelector("#memory-browser-tbody");
    U.memoryBrowserStatus = drawer.querySelector("#memory-browser-status");
    U.memoryBrowserClose = drawer.querySelector("[data-memory-browser-close]");
    U.memoryBrowserEditToggle = drawer.querySelector("#memory-browser-edit-toggle");
    U.memoryBrowserBulkBar = drawer.querySelector("#memory-browser-bulk-bar");
    U.memoryBrowserSelectAll = drawer.querySelector("#memory-browser-select-all");
    U.memoryBrowserSelectedCount = drawer.querySelector("#memory-browser-selected-count");
    U.memoryBrowserBulkDelete = drawer.querySelector("#memory-browser-bulk-delete");
    U.memoryBrowserThSelect = drawer.querySelector("#memory-browser-th-select");
    U.memoryBrowserThActions = drawer.querySelector("#memory-browser-th-actions");
    U.memoryBrowserEditBackdrop = drawer.querySelector("#memory-browser-edit-backdrop");
    U.memoryBrowserEditDialog = drawer.querySelector("#memory-browser-edit-dialog");
    U.memoryBrowserEditMemoryId = drawer.querySelector("#memory-browser-edit-memory-id");
    U.memoryBrowserEditBody = drawer.querySelector("#memory-browser-edit-body");
    U.memoryBrowserEditMinimal = drawer.querySelector("#memory-browser-edit-minimal");
    U.memoryBrowserEditSave = drawer.querySelector("[data-memory-browser-edit-save]");
    U.memoryBrowserClose?.addEventListener("click", () => closeMemoryBrowser());
    U.memoryBrowserBackdrop?.addEventListener("click", () => closeMemoryBrowser());
    U.memoryBrowserEditToggle?.addEventListener("click", () => toggleMemoryBrowserEditMode());
    U.memoryBrowserSelectAll?.addEventListener("change", () => {
        memoryBrowserToggleSelectAll(!!U.memoryBrowserSelectAll?.checked);
    });
    U.memoryBrowserBulkDelete?.addEventListener("click", () => requestMemoryBrowserDeleteSelected());
    U.memoryBrowserTbody?.addEventListener("change", (e) => {
        if (!(e.target instanceof Element)) return;
        const checkbox = e.target.closest("[data-memory-row-select]");
        if (!checkbox) return;
        memoryBrowserToggleRowSelected(checkbox.dataset.memoryRowSelect || "", !!checkbox.checked);
    });
    U.memoryBrowserTbody?.addEventListener("click", (e) => {
        if (!(e.target instanceof Element)) return;
        const noteTrigger = e.target.closest("[data-memory-note-ref]");
        if (noteTrigger) {
            e.preventDefault();
            e.stopPropagation();
            void openMemoryNotePreview(noteTrigger.dataset.memoryNoteRef || "");
            return;
        }
        const editTrigger = e.target.closest("[data-memory-row-edit]");
        if (editTrigger) {
            e.preventDefault();
            e.stopPropagation();
            openMemoryBrowserEditDialog(editTrigger.dataset.memoryRowEdit || "");
            return;
        }
        const deleteTrigger = e.target.closest("[data-memory-row-delete]");
        if (deleteTrigger) {
            e.preventDefault();
            e.stopPropagation();
            requestMemoryBrowserDelete([deleteTrigger.dataset.memoryRowDelete || ""].filter(Boolean), "single");
        }
    });
    drawer.querySelectorAll("[data-memory-browser-edit-close], [data-memory-browser-edit-cancel]").forEach((button) => {
        button.addEventListener("click", () => closeMemoryBrowserEditDialog());
    });
    U.memoryBrowserEditBackdrop?.addEventListener("click", () => closeMemoryBrowserEditDialog());
    U.memoryBrowserEditSave?.addEventListener("click", () => requestMemoryBrowserEditSave());
    U.memoryBrowserDeleteDialog = drawer.querySelector("#memory-delete-dialog");
    U.memoryBrowserDeleteBackdrop = drawer.querySelector("#memory-delete-backdrop");
    U.memoryBrowserDeleteSubtitle = drawer.querySelector("#memory-delete-subtitle");
    U.memoryBrowserDeleteMaster = drawer.querySelector("[data-delete-notes-master]");
    U.memoryBrowserDeleteNotes = drawer.querySelector("#memory-delete-notes");
    U.memoryBrowserDeleteConfirm = drawer.querySelector("[data-memory-delete-confirm]");
    drawer.querySelectorAll("[data-memory-delete-close], [data-memory-delete-cancel]").forEach((button) => {
        button.addEventListener("click", () => closeMemoryDeleteDialog());
    });
    U.memoryBrowserDeleteBackdrop?.addEventListener("click", () => closeMemoryDeleteDialog());
    U.memoryBrowserDeleteMaster?.addEventListener("change", () => {
        S.memoryBrowser.deleteDialog.syncNotes = !!U.memoryBrowserDeleteMaster.checked;
        renderMemoryDeleteDialog();
    });
    U.memoryBrowserDeleteNotes?.addEventListener("change", (e) => {
        if (!(e.target instanceof Element)) return;
        const checkbox = e.target.closest("[data-delete-note-ref]");
        if (!checkbox) return;
        const ref = String(checkbox.dataset.deleteNoteRef || "").trim();
        const note = (S.memoryBrowser.deleteDialog.notes || []).find((item) => item.ref === ref);
        if (!note) return;
        note.checked = !!checkbox.checked;
    });
    U.memoryBrowserDeleteNotes?.addEventListener("click", (e) => {
        if (!(e.target instanceof Element)) return;
        const expandTrigger = e.target.closest("[data-delete-note-expand]");
        if (!expandTrigger) return;
        e.preventDefault();
        e.stopPropagation();
        void toggleDeleteNoteExpand(expandTrigger.dataset.deleteNoteExpand || "");
    });
    U.memoryBrowserDeleteConfirm?.addEventListener("click", () => confirmMemoryDeleteDialog());
    U.memoryBrowserSearch?.addEventListener("input", () => {
        S.memoryBrowser.search = U.memoryBrowserSearch.value || "";
        renderMemoryBrowserList();
    });
    drawer.querySelectorAll("[data-memory-sort]").forEach((th) => {
        const key = th.dataset.memorySort || "";
        const activate = () => toggleMemoryBrowserSort(key);
        th.addEventListener("click", activate);
        th.addEventListener("keydown", (e) => {
            if (e.key === "Enter" || e.key === " ") {
                e.preventDefault();
                activate();
            }
        });
    });
}

function memoryBrowserFilteredItems() {
    const items = Array.isArray(S.memoryBrowser.items) ? S.memoryBrowser.items : [];
    const needle = String(S.memoryBrowser.search || "").trim().toLowerCase();
    if (!needle) return items;
    return items.filter((item) => {
        const haystack = [
            item?.memory_body,
            item?.minimal_memory,
            item?.memory_id,
            item?.source,
        ].map((value) => String(value || "").toLowerCase()).join("\n");
        return haystack.includes(needle);
    });
}

function memoryBrowserSortedItems(items) {
    const key = String(S.memoryBrowser.sortKey || "created_at");
    const dir = S.memoryBrowser.sortDir === "asc" ? 1 : -1;
    const list = [...items];
    list.sort((a, b) => {
        if (key === "refresh_count" || key === "passed_count") {
            const av = Number(a?.[key] || 0);
            const bv = Number(b?.[key] || 0);
            if (av === bv) return 0;
            return av < bv ? -dir : dir;
        }
        const av = String(a?.[key] || "");
        const bv = String(b?.[key] || "");
        const cmp = av.localeCompare(bv);
        if (cmp === 0) return 0;
        return cmp < 0 ? -dir : dir;
    });
    return list;
}

function renderMemoryBrowserList() {
    ensureMemoryBrowserUi();
    if (!U.memoryBrowserTbody) return;
    const filtered = memoryBrowserSortedItems(memoryBrowserFilteredItems());
    const editMode = !!S.memoryBrowser.editMode && !!S.memoryBrowser.mutationsEnabled;
    const columnCount = editMode ? 8 : 6;
    if (S.memoryBrowser.busy) {
        setInnerHtmlPreservingScroll(U.memoryBrowserTbody, `<tr><td colspan="${columnCount}" class="memory-browser-empty">正在加载当前记忆...</td></tr>`);
    } else if (!filtered.length) {
        const hasAny = Array.isArray(S.memoryBrowser.items) && S.memoryBrowser.items.length;
        setInnerHtmlPreservingScroll(U.memoryBrowserTbody, `<tr><td colspan="${columnCount}" class="memory-browser-empty">${hasAny ? "没有匹配的记忆。" : "当前没有记忆。"}</td></tr>`);
    } else {
        setInnerHtmlPreservingScroll(U.memoryBrowserTbody, filtered.map((item) => {
            const createdAt = formatCompactTime(item?.created_at) || String(item?.created_at || "-");
            const source = String(item?.source || "").trim() || "-";
            const memoryId = String(item?.memory_id || "").trim() || "-";
            const body = String(item?.memory_body || "").trim() || "-";
            const selectableId = String(item?.memory_id || "").trim();
            const checked = !!S.memoryBrowser.selected[selectableId];
            const actionBusy = String(S.memoryBrowser.actionBusy || "").trim();
            const rowBusy = !!actionBusy;
            const selectCell = editMode
                ? `<td class="memory-browser-cell-select"><input type="checkbox" data-memory-row-select="${esc(selectableId)}"${checked ? " checked" : ""}${rowBusy ? " disabled" : ""} aria-label="选择记忆 ${esc(memoryId)}" /></td>`
                : "";
            const actionsCell = editMode
                ? `
                <td class="memory-browser-cell-actions">
                    <button type="button" class="toolbar-btn ghost memory-browser-row-btn" data-memory-row-edit="${esc(selectableId)}"${rowBusy ? " disabled" : ""} title="修改这条记忆">
                        <i data-lucide="pencil" aria-hidden="true"></i>
                        修改
                    </button>
                    <button type="button" class="toolbar-btn danger memory-browser-row-btn" data-memory-row-delete="${esc(selectableId)}"${rowBusy ? " disabled" : ""} title="删除这条记忆">
                        <i data-lucide="trash-2" aria-hidden="true"></i>
                        删除
                    </button>
                </td>
                `
                : "";
            return `
                <tr${checked ? ' class="memory-browser-row-selected"' : ""}>
                    ${selectCell}
                    <td class="memory-browser-cell-nowrap">${esc(createdAt)}</td>
                    <td class="memory-browser-cell-nowrap">${esc(String(item?.refresh_count ?? 0))}</td>
                    <td class="memory-browser-cell-nowrap">${esc(String(item?.passed_count ?? 0))}</td>
                    <td class="memory-browser-cell-nowrap">${esc(source)}</td>
                    <td class="memory-browser-cell-nowrap">${esc(memoryId)}</td>
                    <td class="memory-browser-cell-body">${renderMemoryTextWithNoteRefs(body)}</td>
                    ${actionsCell}
                </tr>
            `;
        }).join(""));
    }
    const total = Array.isArray(S.memoryBrowser.items) ? S.memoryBrowser.items.length : 0;
    if (U.memoryBrowserStatus) {
        const errorText = String(S.memoryBrowser.error || "").trim();
        if (errorText) {
            U.memoryBrowserStatus.textContent = errorText;
            U.memoryBrowserStatus.classList.add("is-error");
        } else {
            U.memoryBrowserStatus.textContent = `共 ${total} 条记忆${S.memoryBrowser.search ? `，筛选出 ${filtered.length} 条` : ""}`;
            U.memoryBrowserStatus.classList.remove("is-error");
        }
    }
    if (U.memoryBrowserSubtitle) {
        U.memoryBrowserSubtitle.textContent = `共 ${total} 条 · 点击「创建时间 / 刷新值 / 通过次数」表头可排序。`;
    }
    U.memoryBrowserDrawer?.querySelectorAll("[data-sort-ind]").forEach((ind) => {
        const key = ind.dataset.sortInd || "";
        const active = key === S.memoryBrowser.sortKey;
        ind.textContent = active ? (S.memoryBrowser.sortDir === "asc" ? " ↑" : " ↓") : "";
    });
    renderMemoryBrowserEditState();
    icons();
}

function memoryBrowserSelectedIds() {
    const selected = S.memoryBrowser.selected || {};
    const items = Array.isArray(S.memoryBrowser.items) ? S.memoryBrowser.items : [];
    const knownIds = new Set(items.map((item) => String(item?.memory_id || "").trim()).filter(Boolean));
    return Object.keys(selected).filter((id) => selected[id] && knownIds.has(id));
}

function renderMemoryBrowserEditState() {
    const editMode = !!S.memoryBrowser.editMode && !!S.memoryBrowser.mutationsEnabled;
    if (U.memoryBrowserThSelect) U.memoryBrowserThSelect.hidden = !editMode;
    if (U.memoryBrowserThActions) U.memoryBrowserThActions.hidden = !editMode;
    if (U.memoryBrowserBulkBar) U.memoryBrowserBulkBar.hidden = !editMode;
    if (U.memoryBrowserEditToggle) {
        U.memoryBrowserEditToggle.disabled = !S.memoryBrowser.mutationsEnabled;
        U.memoryBrowserEditToggle.textContent = editMode ? "完成" : "编辑";
        U.memoryBrowserEditToggle.title = S.memoryBrowser.mutationsEnabled
            ? (editMode ? "退出编辑模式" : "进入编辑模式：批量选择、修改与删除记忆")
            : "编辑不可用：服务端未启用 G3KU_ENABLE_MEMORY_ADMIN_MUTATIONS";
    }
    const selectedIds = memoryBrowserSelectedIds();
    const filtered = memoryBrowserSortedItems(memoryBrowserFilteredItems());
    if (U.memoryBrowserSelectAll) {
        const filteredIds = filtered.map((item) => String(item?.memory_id || "").trim()).filter(Boolean);
        U.memoryBrowserSelectAll.checked = filteredIds.length > 0 && filteredIds.every((id) => selectedIds.includes(id));
        U.memoryBrowserSelectAll.disabled = !editMode || !filteredIds.length || !!S.memoryBrowser.actionBusy;
    }
    if (U.memoryBrowserSelectedCount) {
        U.memoryBrowserSelectedCount.textContent = `已选 ${selectedIds.length} 条`;
    }
    if (U.memoryBrowserBulkDelete) {
        U.memoryBrowserBulkDelete.disabled = !editMode || !selectedIds.length || !!S.memoryBrowser.actionBusy;
    }
    if (U.memoryBrowserEditDialog) {
        const dialogOpen = !!S.memoryBrowser.editDialog.open;
        U.memoryBrowserEditDialog.hidden = !dialogOpen;
        U.memoryBrowserEditDialog.setAttribute("aria-hidden", dialogOpen ? "false" : "true");
        if (U.memoryBrowserEditBackdrop) {
            U.memoryBrowserEditBackdrop.hidden = !dialogOpen;
            U.memoryBrowserEditBackdrop.setAttribute("aria-hidden", dialogOpen ? "false" : "true");
        }
        if (U.memoryBrowserEditSave) U.memoryBrowserEditSave.disabled = !!S.memoryBrowser.editDialog.busy;
    }
}

function toggleMemoryBrowserEditMode() {
    if (!S.memoryBrowser.mutationsEnabled) {
        showToast({
            title: "编辑不可用",
            text: "服务端未启用记忆运维变更（G3KU_ENABLE_MEMORY_ADMIN_MUTATIONS）。",
            kind: "warn",
            durationMs: 5000,
        });
        return;
    }
    S.memoryBrowser.editMode = !S.memoryBrowser.editMode;
    S.memoryBrowser.selected = {};
    renderMemoryBrowserList();
}

function memoryBrowserToggleRowSelected(memoryId, checked) {
    const normalized = String(memoryId || "").trim();
    if (!normalized) return;
    const selected = { ...(S.memoryBrowser.selected || {}) };
    if (checked) {
        selected[normalized] = true;
    } else {
        delete selected[normalized];
    }
    S.memoryBrowser.selected = selected;
    renderMemoryBrowserList();
}

function memoryBrowserToggleSelectAll(checked) {
    const filtered = memoryBrowserSortedItems(memoryBrowserFilteredItems());
    const selected = { ...(S.memoryBrowser.selected || {}) };
    filtered.forEach((item) => {
        const id = String(item?.memory_id || "").trim();
        if (!id) return;
        if (checked) {
            selected[id] = true;
        } else {
            delete selected[id];
        }
    });
    S.memoryBrowser.selected = selected;
    renderMemoryBrowserList();
}

function requestMemoryBrowserDeleteSelected() {
    const selectedIds = memoryBrowserSelectedIds();
    if (!selectedIds.length) return;
    requestMemoryBrowserDelete(selectedIds, "bulk");
}

function requestMemoryBrowserDelete(memoryIds, scope = "bulk") {
    const ids = [...new Set((Array.isArray(memoryIds) ? memoryIds : [memoryIds]).map((id) => String(id || "").trim()).filter(Boolean))];
    if (!ids.length || S.memoryBrowser.actionBusy) return;
    if (!S.memoryBrowser.mutationsEnabled) {
        showToast({
            title: "删除不可用",
            text: "服务端未启用记忆运维变更（G3KU_ENABLE_MEMORY_ADMIN_MUTATIONS）。",
            kind: "warn",
            durationMs: 5000,
        });
        return;
    }
    openMemoryDeleteDialog(ids);
}

// 解析记忆正文里的 note 引用（ref:note_xxx 与 见noteid:note_xxx 两种写法）
function memoryNoteRefsInText(text) {
    const refs = [];
    const pattern = /(?:\bref:|见noteid:)(note_[a-z0-9_]+)/g;
    const value = String(text || "");
    let match = pattern.exec(value);
    while (match) {
        const ref = String(match[1] || "").trim();
        if (ref && !refs.includes(ref)) refs.push(ref);
        match = pattern.exec(value);
    }
    return refs;
}

function openMemoryDeleteDialog(memoryIds) {
    const items = Array.isArray(S.memoryBrowser.items) ? S.memoryBrowser.items : [];
    const idSet = new Set(memoryIds);
    const selectedBodies = items.filter((item) => idSet.has(String(item?.memory_id || "").trim()));
    const otherBodies = items.filter((item) => !idSet.has(String(item?.memory_id || "").trim()));
    const refs = [];
    selectedBodies.forEach((item) => {
        memoryNoteRefsInText(item?.memory_body).forEach((ref) => {
            if (!refs.includes(ref)) refs.push(ref);
        });
    });
    const sharedRefs = [];
    otherBodies.forEach((item) => {
        memoryNoteRefsInText(item?.memory_body).forEach((ref) => {
            if (!sharedRefs.includes(ref)) sharedRefs.push(ref);
        });
    });
    S.memoryBrowser.deleteDialog = {
        open: true,
        memoryIds,
        syncNotes: refs.length > 0,
        busy: false,
        notes: refs.map((ref) => ({
            ref,
            checked: !sharedRefs.includes(ref),
            expanded: false,
            body: "",
            loaded: false,
            loading: false,
            shared: sharedRefs.includes(ref),
        })),
    };
    renderMemoryDeleteDialog();
}

function closeMemoryDeleteDialog() {
    if (S.memoryBrowser.deleteDialog.busy) return;
    S.memoryBrowser.deleteDialog = { open: false, memoryIds: [], syncNotes: true, notes: [], busy: false };
    renderMemoryDeleteDialog();
}

function renderMemoryDeleteDialog() {
    ensureMemoryBrowserUi();
    const dialog = U.memoryBrowserDeleteDialog;
    const backdrop = U.memoryBrowserDeleteBackdrop;
    const state = S.memoryBrowser.deleteDialog || {};
    const open = !!state.open;
    if (dialog) {
        dialog.hidden = !open;
        dialog.setAttribute("aria-hidden", open ? "false" : "true");
    }
    if (backdrop) {
        backdrop.hidden = !open;
        backdrop.setAttribute("aria-hidden", open ? "false" : "true");
    }
    if (!open) return;
    const subtitle = U.memoryBrowserDeleteSubtitle;
    if (subtitle) {
        subtitle.textContent = `将删除 ${state.memoryIds.length} 条记忆；删除后 MEMORY.md 镜像会同步重建，操作不可撤销。`;
    }
    const master = U.memoryBrowserDeleteMaster;
    if (master) {
        master.checked = !!state.syncNotes;
        master.disabled = !!state.busy || !state.notes.length;
    }
    const notesHost = U.memoryBrowserDeleteNotes;
    if (notesHost) {
        if (!state.notes.length) {
            notesHost.innerHTML = '<div class="memory-delete-notes-empty">这批记忆没有关联笔记。</div>';
        } else {
            notesHost.innerHTML = state.notes.map((note) => `
                <div class="memory-delete-note-row${note.expanded ? " is-expanded" : ""}">
                    <label class="memory-delete-note-check">
                        <input type="checkbox" data-delete-note-ref="${esc(note.ref)}"${note.checked ? " checked" : ""}${!state.syncNotes || state.busy ? " disabled" : ""} aria-label="同步删除笔记 ${esc(note.ref)}" />
                    </label>
                    <span class="memory-delete-note-ref">${esc(note.ref)}</span>
                    ${note.shared ? '<span class="memory-delete-note-shared" title="删除后其他记忆仍引用该笔记，引用将悬空">仍被其他记忆引用</span>' : ""}
                    <button type="button" class="toolbar-btn ghost memory-delete-note-expand" data-delete-note-expand="${esc(note.ref)}" aria-expanded="${note.expanded ? "true" : "false"}">
                        ${note.expanded ? "收起" : "展开内容"}
                    </button>
                    ${note.expanded ? `<pre class="memory-delete-note-body">${note.loading ? "正在加载 note 正文..." : esc(String(note.body || "").trim() || "（空 note）")}</pre>` : ""}
                </div>
            `).join("");
        }
    }
    const confirmButton = U.memoryBrowserDeleteConfirm;
    if (confirmButton) confirmButton.disabled = !!state.busy;
    icons();
}

async function toggleDeleteNoteExpand(ref) {
    const state = S.memoryBrowser.deleteDialog;
    const note = (state.notes || []).find((item) => item.ref === ref);
    if (!note || note.loading) return;
    note.expanded = !note.expanded;
    if (note.expanded && !note.loaded) {
        note.loading = true;
        renderMemoryDeleteDialog();
        try {
            const item = await ApiClient.getMemoryNote(ref);
            note.body = String(item?.body || "");
            note.loaded = true;
        } catch (error) {
            note.body = `加载失败：${error?.message || "读取记忆 note 失败"}`;
            note.loaded = true;
        } finally {
            note.loading = false;
            renderMemoryDeleteDialog();
        }
        return;
    }
    renderMemoryDeleteDialog();
}

function confirmMemoryDeleteDialog() {
    const state = S.memoryBrowser.deleteDialog;
    if (!state.open || state.busy) return;
    const noteRefs = state.syncNotes
        ? (state.notes || []).filter((note) => note.checked).map((note) => note.ref)
        : [];
    state.busy = true;
    renderMemoryDeleteDialog();
    void runMemoryBrowserDelete(state.memoryIds, noteRefs);
}

async function runMemoryBrowserDelete(memoryIds, noteRefs = []) {
    const ids = [...new Set((Array.isArray(memoryIds) ? memoryIds : []).map((id) => String(id || "").trim()).filter(Boolean))];
    if (!ids.length || S.memoryBrowser.actionBusy) return;
    S.memoryBrowser.actionBusy = `delete:${ids.length}`;
    renderMemoryBrowserList();
    try {
        const result = await ApiClient.deleteCurrentMemories(ids, "manual-ui", noteRefs);
        const deletedCount = Array.isArray(result?.item?.deleted) ? result.item.deleted.length : ids.length;
        const missingCount = Array.isArray(result?.item?.missing) ? result.item.missing.length : 0;
        const notesDeleted = Array.isArray(result?.item?.notes_deleted) ? result.item.notes_deleted.length : 0;
        showToast({
            title: "删除完成",
            text: [
                missingCount ? `已删除 ${deletedCount} 条记忆，${missingCount} 条未找到（可能已被删除）。` : `已删除 ${deletedCount} 条记忆，镜像已同步。`,
                notesDeleted ? `同步删除 ${notesDeleted} 个关联笔记。` : "",
            ].filter(Boolean).join(" "),
            kind: missingCount ? "warn" : "success",
            durationMs: 4200,
        });
        const selected = { ...(S.memoryBrowser.selected || {}) };
        ids.forEach((id) => delete selected[id]);
        S.memoryBrowser.selected = selected;
        S.memoryBrowser.deleteDialog = { open: false, memoryIds: [], syncNotes: true, notes: [], busy: false };
        renderMemoryDeleteDialog();
        await loadMemoryBrowser();
    } catch (error) {
        S.memoryBrowser.deleteDialog.busy = false;
        renderMemoryDeleteDialog();
        showToast({ title: "删除失败", text: error?.message || "记忆删除未成功，请稍后重试", kind: "error", durationMs: 5000 });
    } finally {
        S.memoryBrowser.actionBusy = "";
        renderMemoryBrowserList();
    }
}

function openMemoryBrowserEditDialog(memoryId) {
    const normalized = String(memoryId || "").trim();
    if (!normalized || S.memoryBrowser.actionBusy) return;
    if (!S.memoryBrowser.mutationsEnabled) {
        showToast({
            title: "修改不可用",
            text: "服务端未启用记忆运维变更（G3KU_ENABLE_MEMORY_ADMIN_MUTATIONS）。",
            kind: "warn",
            durationMs: 5000,
        });
        return;
    }
    const items = Array.isArray(S.memoryBrowser.items) ? S.memoryBrowser.items : [];
    const target = items.find((item) => String(item?.memory_id || "").trim() === normalized);
    if (!target) {
        showToast({ title: "未找到记忆", text: "该记忆可能已被删除，请刷新列表。", kind: "warn" });
        return;
    }
    S.memoryBrowser.editDialog = {
        open: true,
        memoryId: normalized,
        body: String(target?.memory_body || ""),
        minimal: String(target?.minimal_memory || ""),
        busy: false,
    };
    renderMemoryBrowserEditState();
    if (U.memoryBrowserEditMemoryId) U.memoryBrowserEditMemoryId.value = normalized;
    if (U.memoryBrowserEditBody) U.memoryBrowserEditBody.value = S.memoryBrowser.editDialog.body;
    if (U.memoryBrowserEditMinimal) U.memoryBrowserEditMinimal.value = S.memoryBrowser.editDialog.minimal;
    U.memoryBrowserEditBody?.focus();
}

function closeMemoryBrowserEditDialog() {
    if (S.memoryBrowser.editDialog.busy) return;
    S.memoryBrowser.editDialog = { ...S.memoryBrowser.editDialog, open: false };
    renderMemoryBrowserEditState();
}

function requestMemoryBrowserEditSave() {
    const dialog = S.memoryBrowser.editDialog || {};
    if (!dialog.open || dialog.busy) return;
    const memoryId = String(dialog.memoryId || "").trim();
    const body = String(U.memoryBrowserEditBody?.value ?? dialog.body ?? "");
    const minimal = String(U.memoryBrowserEditMinimal?.value ?? dialog.minimal ?? "");
    if (!memoryId) return;
    if (!body.trim()) {
        showToast({ title: "内容不能为空", text: "记忆内容为必填项。", kind: "warn" });
        return;
    }
    // 二次确认：内部弹窗确认后才真正提交修改
    openConfirm({
        title: "保存记忆修改",
        text: "确定保存对这条记忆的修改吗？保存后 MEMORY.md 镜像会同步重建。",
        confirmLabel: "保存",
        confirmKind: "danger",
        onConfirm: () => void runMemoryBrowserEditSave(memoryId, body, minimal),
    });
}

async function runMemoryBrowserEditSave(memoryId, body, minimal) {
    if (S.memoryBrowser.actionBusy) return;
    S.memoryBrowser.editDialog = { ...S.memoryBrowser.editDialog, busy: true };
    S.memoryBrowser.actionBusy = `update:${memoryId}`;
    renderMemoryBrowserEditState();
    try {
        const original = (Array.isArray(S.memoryBrowser.items) ? S.memoryBrowser.items : [])
            .find((item) => String(item?.memory_id || "").trim() === memoryId);
        const originalMinimal = String(original?.minimal_memory || "");
        const minimalChanged = minimal.trim() && minimal.trim() !== originalMinimal;
        await ApiClient.updateCurrentMemory(memoryId, {
            memoryBody: body.trim(),
            minimalMemory: minimalChanged ? minimal.trim() : null,
            reason: "manual-ui",
        });
        showToast({ title: "已保存", text: "记忆修改已保存，镜像已同步。", kind: "success" });
        S.memoryBrowser.editDialog = { open: false, memoryId: "", body: "", minimal: "", busy: false };
        await loadMemoryBrowser();
    } catch (error) {
        showToast({ title: "保存失败", text: error?.message || "记忆修改未成功，请稍后重试", kind: "error", durationMs: 5000 });
        S.memoryBrowser.editDialog = { ...S.memoryBrowser.editDialog, busy: false };
    } finally {
        S.memoryBrowser.actionBusy = "";
        renderMemoryBrowserEditState();
    }
}

function toggleMemoryBrowserSort(key) {
    const normalized = String(key || "").trim();
    if (!normalized) return;
    if (S.memoryBrowser.sortKey === normalized) {
        S.memoryBrowser.sortDir = S.memoryBrowser.sortDir === "asc" ? "desc" : "asc";
    } else {
        S.memoryBrowser.sortKey = normalized;
        S.memoryBrowser.sortDir = normalized === "created_at" ? "desc" : "desc";
    }
    renderMemoryBrowserList();
}

function renderMemoryBrowser() {
    ensureMemoryBrowserUi();
    renderMemoryBrowserList();
    setDrawerOpen(U.memoryBrowserBackdrop, U.memoryBrowserDrawer, !!S.memoryBrowser.open);
}

function closeMemoryBrowser() {
    S.memoryBrowser.open = false;
    S.memoryBrowser.editMode = false;
    S.memoryBrowser.selected = {};
    S.memoryBrowser.editDialog = { open: false, memoryId: "", body: "", minimal: "", busy: false };
    renderMemoryBrowser();
}

async function loadMemoryBrowser() {
    ensureMemoryBrowserUi();
    S.memoryBrowser.busy = true;
    S.memoryBrowser.error = "";
    S.memoryBrowser.requestToken += 1;
    const requestToken = S.memoryBrowser.requestToken;
    renderMemoryBrowser();
    try {
        const payload = await ApiClient.getCurrentMemories();
        if (requestToken !== S.memoryBrowser.requestToken) return;
        S.memoryBrowser.items = Array.isArray(payload?.items) ? payload.items : [];
        S.memoryBrowser.total = normalizeInt(payload?.total, S.memoryBrowser.items.length);
        S.memoryBrowser.mutationsEnabled = !!payload?.mutationsEnabled;
        if (!S.memoryBrowser.mutationsEnabled) {
            S.memoryBrowser.editMode = false;
            S.memoryBrowser.selected = {};
            S.memoryBrowser.editDialog = { open: false, memoryId: "", body: "", minimal: "", busy: false };
        }
        S.memoryBrowser.error = "";
    } catch (error) {
        if (requestToken !== S.memoryBrowser.requestToken) return;
        S.memoryBrowser.error = error?.message || "当前记忆加载失败";
        showToast({ title: "记忆加载失败", text: S.memoryBrowser.error, kind: "error" });
    } finally {
        if (requestToken === S.memoryBrowser.requestToken) {
            S.memoryBrowser.busy = false;
            renderMemoryBrowser();
        }
    }
}

async function openMemoryBrowser() {
    ensureMemoryBrowserUi();
    S.memoryBrowser.open = true;
    renderMemoryBrowser();
    await loadMemoryBrowser();
}

function setMemoryCardExpanded(kind, key, expanded) {
    const target = kind === "processed" ? S.memoryProcessedExpanded : S.memoryQueueExpanded;
    if (!key) return;
    target[key] = !!expanded;
}

// 轮询刷新会重渲染详情/列表；直接写 innerHTML 会把滚动条重置到顶部。
// 内容未变化时跳过重渲染，变化时保留原滚动位置（超出新内容高度时由浏览器钳制）。
function setInnerHtmlPreservingScroll(el, html) {
    if (!el) return;
    if (el.innerHTML === html) return;
    const previousTop = el.scrollTop;
    const previousLeft = el.scrollLeft;
    el.innerHTML = html;
    el.scrollTop = previousTop;
    el.scrollLeft = previousLeft;
}

function setTextPreservingScroll(el, text) {
    if (!el) return;
    if (el.textContent === text) return;
    const previousTop = el.scrollTop;
    el.textContent = text;
    el.scrollTop = previousTop;
}

function stopMemoryViewAutoRefresh() {
    if (S.memoryPollIntervalId) {
        window.clearInterval(S.memoryPollIntervalId);
        S.memoryPollIntervalId = null;
    }
}

function startMemoryViewAutoRefresh() {
    if (S.memoryPollIntervalId) return;
    S.memoryPollIntervalId = window.setInterval(() => {
        if (S.view !== "memory") return;
        void loadMemoryView({ quiet: true });
    }, MEMORY_VIEW_POLL_MS);
}

// 日志审计：视图内 15s 自动刷新（与记忆视图同一模式，保留滚动按零内容变化策略处理）

function stopAuditViewAutoRefresh() {
    if (S.auditPollIntervalId) {
        window.clearInterval(S.auditPollIntervalId);
        S.auditPollIntervalId = null;
    }
}

function startAuditViewAutoRefresh() {
    if (S.auditPollIntervalId) return;
    S.auditPollIntervalId = window.setInterval(() => {
        if (S.view !== "audit") return;
        void loadAuditView({ quiet: true });
    }, AUDIT_VIEW_POLL_MS);
}

async function loadAuditView({ quiet = false } = {}) {
    if (S.auditBusy) return;
    S.auditBusy = true;
    // 静默轮询停留在当前页；显式刷新（按钮/首次进入）回到第 1 页（最新）。
    const page = quiet ? S.auditPage : 1;
    try {
        await loadAuditEvents({ quiet, page, preserveScroll: quiet });
        // 非首页的列表里没有最新事件，角标已读锚点需要单独取一次。
        if (page !== 1) await refreshAuditLatestEventTs();
        S.auditLoadedOnce = true;
        markAuditRead();
    } finally {
        S.auditBusy = false;
        renderAuditPager();
    }
}

function auditSubsystemLabel(subsystem) {
    const labels = {
        provider: "模型调用",
        task: "任务执行",
        web_api: "Web 接口",
        memory: "记忆处理",
    };
    return Object.prototype.hasOwnProperty.call(labels, subsystem) ? labels[subsystem] : String(subsystem || "未知来源");
}

async function refreshAuditLatestEventTs() {
    try {
        const latest = await ApiClient.getAuditEvents({ limit: 1 });
        const newest = String(latest?.items?.[0]?.timestamp || "");
        if (newest) S.auditLatestEventTs = newest;
    } catch {
        // 静默：锚点取不到时保持原值
    }
}

async function loadAuditEvents({ quiet = false, page = 1, preserveScroll = false } = {}) {
    const requested = Math.max(1, Math.floor(Number(page) || 1));
    try {
        const data = await ApiClient.getAuditEvents({
            limit: AUDIT_PAGE_SIZE,
            offset: (requested - 1) * AUDIT_PAGE_SIZE,
        });
        const items = Array.isArray(data.items) ? data.items : [];
        const total = Math.max(0, Number(data.total) || 0);
        const pageCount = Math.max(1, Math.ceil(total / AUDIT_PAGE_SIZE));
        // 事件被修剪/丢弃后当前页可能越界：回落到最后一页，避免空白页。
        if (total > 0 && requested > pageCount) {
            await loadAuditEvents({ quiet, page: pageCount, preserveScroll });
            return;
        }
        S.auditPage = requested;
        S.auditTotal = total;
        S.auditPageCount = pageCount;
        renderAuditEventList(items, { preserveScroll });
        renderAuditPager();
        if (requested === 1 && items.length) S.auditLatestEventTs = String(items[0]?.timestamp || "");
    } catch (error) {
        if (!quiet) {
            showToast({
                title: "审计事件加载失败",
                text: String(error?.message || ""),
                kind: "error",
                durationMs: 2600,
            });
        }
    }
}

async function goToAuditPage(page) {
    if (S.auditBusy) return;
    const target = Math.max(1, Math.floor(Number(page) || 1));
    if (target === S.auditPage) return;
    S.auditBusy = true;
    try {
        // 换页回到列表顶部（preserveScroll 为假）。
        await loadAuditEvents({ quiet: true, page: target });
    } finally {
        S.auditBusy = false;
        renderAuditPager();
    }
}

function auditPageSummary(page, pageCount, total) {
    if (!total) return "第 1/1 页 · 共 0 条";
    const current = Math.min(Math.max(1, Number(page) || 1), pageCount);
    const start = ((current - 1) * AUDIT_PAGE_SIZE) + 1;
    const end = Math.min(current * AUDIT_PAGE_SIZE, total);
    return `第 ${current}/${pageCount} 页 · 显示 ${start}-${end} / 共 ${total} 条`;
}

function renderAuditPager() {
    if (U.auditEventInfo) {
        U.auditEventInfo.textContent = auditPageSummary(S.auditPage, S.auditPageCount, S.auditTotal);
    }
    if (U.auditPagePrev) U.auditPagePrev.disabled = S.auditBusy || S.auditPage <= 1;
    if (U.auditPageNext) U.auditPageNext.disabled = S.auditBusy || S.auditPage >= S.auditPageCount;
}

function auditEventLevelClass(level) {
    if (level === "error") return "is-error";
    if (level === "warning") return "is-warning";
    return "";
}

function auditEventLevelLabel(level) {
    if (level === "error") return "错误";
    if (level === "warning") return "警告";
    return "信息";
}

// 日志时间统一渲染为浏览器所在系统的本地时间（YYYY-MM-DD HH:mm:ss），
// 事件本身带时区偏移的 ISO 串只用于角标 since 比较，不直接展示。
function formatAuditTimestamp(value) {
    const raw = String(value || "").trim();
    if (!raw) return "-";
    const parsed = new Date(raw);
    if (Number.isNaN(parsed.getTime())) return raw;
    const pad = (part) => String(part).padStart(2, "0");
    return `${parsed.getFullYear()}-${pad(parsed.getMonth() + 1)}-${pad(parsed.getDate())}`
        + ` ${pad(parsed.getHours())}:${pad(parsed.getMinutes())}:${pad(parsed.getSeconds())}`;
}

function renderAuditEventCard(item = {}) {
    // 原始日志：一行一条（时间 | 级别 | 来源 | 摘要），detail 可展开
    const level = String(item.level || "info");
    const timestamp = formatAuditTimestamp(item.timestamp);
    const source = auditSubsystemLabel(String(item.subsystem || "unknown"));
    const eventType = String(item.event_type || "");
    const summary = String(item.summary || "");
    const detail = item.detail;
    let detailHtml = "";
    if (detail && typeof detail === "object" && Object.keys(detail).length) {
        let pretty = "";
        try {
            pretty = JSON.stringify(detail, null, 2);
        } catch {
            pretty = String(detail);
        }
        detailHtml = `<details class="audit-event-expand"><summary>详情</summary><pre class="audit-event-detail">${esc(pretty)}</pre></details>`;
    }
    return `<article class="audit-log-line ${auditEventLevelClass(level)}">
        <span class="audit-log-time">${esc(timestamp)}</span>
        <span class="audit-log-level">${esc(auditEventLevelLabel(level))}</span>
        <span class="audit-log-source">${esc(source)}</span>
        <span class="audit-log-summary">${esc(summary)}</span>
        ${eventType ? `<span class="audit-log-type">${esc(eventType)}</span>` : ""}
        ${detailHtml}
    </article>`;
}

function renderAuditEventList(items = [], { preserveScroll = false } = {}) {
    if (!U.auditEventList) return;
    // 静默轮询保留滚动位置，避免每 15s 把正在翻看旧日志的人弹回顶部；
    // 换页/显式刷新（preserveScroll 为假）回到列表顶部。
    const previousScrollTop = preserveScroll ? U.auditEventList.scrollTop : 0;
    U.auditEventList.innerHTML = items.map((item) => renderAuditEventCard(item)).join("");
    U.auditEventList.scrollTop = previousScrollTop;
}

// 未读角标：lastSeen 存 sessionStorage（tab 作用域）。
// 首访（无 lastSeen）以最新事件时间戳初始化，历史不点亮；打开审计视图即已读。

function renderAuditNavBadge(count = 0) {
    if (!U.auditNavBadge) return;
    const unread = Math.max(0, Number(count) || 0);
    U.auditNavBadge.textContent = unread > 99 ? "99+" : String(unread);
    U.auditNavBadge.hidden = unread <= 0;
}

function auditUnreadFromResponse(total) {
    return Math.max(0, Number(total) || 0);
}

function resolveAuditLastSeen(storedLastSeen, newestTimestamp) {
    // 首访语义：尚无 lastSeen 时锚定到最新事件时间戳（未读 0）；
    // 已有 lastSeen 原样保持。严格大于过滤由服务端 since 完成。
    return {
        lastSeen: String(storedLastSeen || "") || String(newestTimestamp || ""),
        unread: 0,
    };
}

function auditBadgeStateFromStorage() {
    return readSessionJson(AUDIT_LAST_SEEN_KEY) || null;
}

async function refreshAuditBadge() {
    try {
        const stored = auditBadgeStateFromStorage();
        const lastSeen = String(stored?.lastSeen || "");
        if (!lastSeen) {
            const latest = await ApiClient.getAuditEvents({ limit: 1 });
            const newest = String(latest?.items?.[0]?.timestamp || "");
            const resolved = resolveAuditLastSeen("", newest);
            writeSessionJson(AUDIT_LAST_SEEN_KEY, { lastSeen: resolved.lastSeen });
            renderAuditNavBadge(0);
            return;
        }
        const data = await ApiClient.getAuditEvents({ limit: 1, since: lastSeen });
        renderAuditNavBadge(auditUnreadFromResponse(data.total));
    } catch {
        // 静默：保持上一次角标状态
    }
}

function bindAuditBadge() {
    if (S.auditBadgePollIntervalId) return;
    S.auditBadgePollIntervalId = window.setInterval(() => {
        void refreshAuditBadge();
    }, AUDIT_BADGE_POLL_MS);
}

function markAuditRead() {
    const latest = S.auditLatestEventTs || "";
    writeSessionJson(AUDIT_LAST_SEEN_KEY, { lastSeen: latest });
    renderAuditNavBadge(0);
}

function bindMemoryCardToggles() {
    if (U.memoryQueueList instanceof HTMLElement) {
        U.memoryQueueList.querySelectorAll("details[data-memory-card='queue']").forEach((card) => {
            if (!(card instanceof HTMLDetailsElement)) return;
            if (card.dataset.memoryToggleBound === "1") return;
            card.dataset.memoryToggleBound = "1";
            card.addEventListener("toggle", () => {
                setMemoryCardExpanded("queue", card.dataset.memoryKey || "", card.open);
            });
        });
    }
    if (U.memoryProcessedList instanceof HTMLElement) {
        U.memoryProcessedList.querySelectorAll("details[data-memory-card='processed']").forEach((card) => {
            if (!(card instanceof HTMLDetailsElement)) return;
            if (card.dataset.memoryToggleBound === "1") return;
            card.dataset.memoryToggleBound = "1";
            card.addEventListener("toggle", () => {
                setMemoryCardExpanded("processed", card.dataset.memoryKey || "", card.open);
            });
        });
    }
}

function renderMemoryAdminActions() {
    if (!U.memoryAdminActions) return;
    U.memoryAdminActions.hidden = true;
    U.memoryAdminActions.setAttribute("aria-hidden", "true");
    U.memoryAdminActions.innerHTML = "";
}

async function loadMoreMemoryQueue() {
    if (S.memoryBusy || !S.memoryQueueHasMore) return;
    S.memoryBusy = true;
    renderMemoryView();
    try {
        const payload = await ApiClient.getMemoryQueue({
            limit: S.memoryQueuePageSize,
            offset: S.memoryQueueItems.length,
        });
        S.memoryQueueItems = [...S.memoryQueueItems, ...(Array.isArray(payload?.items) ? payload.items : [])];
        S.memoryQueueTotal = normalizeInt(payload?.total, S.memoryQueueTotal);
        S.memoryQueueHasMore = !!payload?.hasMore;
    } catch (error) {
        showToast({ title: "加载失败", text: error?.message || "记忆队列加载失败", kind: "error" });
    } finally {
        S.memoryBusy = false;
        renderMemoryView();
    }
}

async function loadMoreMemoryProcessed() {
    if (S.memoryBusy || !S.memoryProcessedHasMore) return;
    S.memoryBusy = true;
    renderMemoryView();
    try {
        const payload = await ApiClient.getMemoryProcessed({
            limit: S.memoryProcessedPageSize,
            offset: S.memoryProcessedItems.length,
        });
        S.memoryProcessedItems = [...S.memoryProcessedItems, ...(Array.isArray(payload?.items) ? payload.items : [])];
        S.memoryProcessedTotal = normalizeInt(payload?.total, S.memoryProcessedTotal);
        S.memoryProcessedHasMore = !!payload?.hasMore;
    } catch (error) {
        showToast({ title: "加载失败", text: error?.message || "已处理记忆加载失败", kind: "error" });
    } finally {
        S.memoryBusy = false;
        renderMemoryView();
    }
}

function renderMemoryMetaItem(label, value) {
    return `<span class="memory-card-meta-item"><span class="memory-card-meta-label">${esc(label)}</span><span class="memory-card-meta-value">${esc(value)}</span></span>`;
}

function currentMemoryQueueBlockedText() {
    const queueHead = Array.isArray(S.memoryQueueItems) && S.memoryQueueItems.length ? S.memoryQueueItems[0] : null;
    const errorText = String(queueHead?.last_error_text || "").trim();
    if (!queueHead || String(queueHead?.status || "").trim().toLowerCase() !== "processing" || !errorText) return "";
    const retryAfter = String(queueHead?.retry_after || "").trim();
    const retryText = retryAfter ? `, 下次重试：${formatCompactTime(retryAfter) || retryAfter}` : "";
    return `队首阻塞：${errorText}${retryText}`;
}

function maybeToastMemoryAlerts({ quiet = false } = {}) {
    const errorText = String(S.memoryError || "").trim();
    const blockedText = currentMemoryQueueBlockedText();
    if (errorText && errorText !== S.memoryLastAlertText) {
        S.memoryLastAlertText = errorText;
        if (!quiet) showToast({ title: "记忆加载失败", text: errorText, kind: "error" });
    }
    if (!errorText) {
        S.memoryLastAlertText = "";
    }
    if (blockedText && blockedText !== S.memoryLastBlockedText) {
        S.memoryLastBlockedText = blockedText;
        showToast({ title: "队列阻塞", text: blockedText, kind: "warn", durationMs: 4200 });
    }
    if (!blockedText) {
        S.memoryLastBlockedText = "";
    }
}

function renderMemoryView() {
    renderMemoryAdminActions();
    if (U.memoryQueueList) {
        if (S.memoryBusy && !S.memoryQueueItems.length) {
            setInnerHtmlPreservingScroll(U.memoryQueueList, '<div class="empty-state compact">正在加载记忆队列...</div>');
        } else if (!S.memoryQueueItems.length) {
            setInnerHtmlPreservingScroll(U.memoryQueueList, '<div class="empty-state compact">当前没有未出队记忆。</div>');
        } else {
            setInnerHtmlPreservingScroll(U.memoryQueueList, S.memoryQueueItems.map((item) => renderMemoryQueueCard(item)).join(""));
        }
    }
    if (U.memoryProcessedList) {
        if (S.memoryBusy && !S.memoryProcessedItems.length) {
            setInnerHtmlPreservingScroll(U.memoryProcessedList, '<div class="empty-state compact">正在加载已处理批次...</div>');
        } else if (!S.memoryProcessedItems.length) {
            setInnerHtmlPreservingScroll(U.memoryProcessedList, '<div class="empty-state compact">当前还没有已处理记忆。</div>');
        } else {
            setInnerHtmlPreservingScroll(U.memoryProcessedList, S.memoryProcessedItems.map((item) => renderMemoryProcessedCard(item)).join(""));
        }
    }
    // 失败记忆板块按需显示：仅当存在停车记录时出现，避免常态下挤占待处理队列
    const hasFailedMemories = S.memoryFailedTotal > 0 || S.memoryFailedItems.length > 0;
    if (U.memoryFailedPanel) U.memoryFailedPanel.hidden = !hasFailedMemories;
    if (U.memoryFailedList) {
        if (S.memoryBusy && !S.memoryFailedItems.length) {
            setInnerHtmlPreservingScroll(U.memoryFailedList, '<div class="empty-state compact">正在加载失败记忆...</div>');
        } else if (!S.memoryFailedItems.length) {
            setInnerHtmlPreservingScroll(U.memoryFailedList, '<div class="empty-state compact">没有失败停车的记忆。</div>');
        } else {
            setInnerHtmlPreservingScroll(U.memoryFailedList, S.memoryFailedItems.map((item) => renderMemoryFailedCard(item)).join(""));
        }
    }
    if (U.memoryQueueInfo) U.memoryQueueInfo.textContent = `共 ${S.memoryQueueTotal} 项`;
    if (U.memoryProcessedInfo) U.memoryProcessedInfo.textContent = `共 ${S.memoryProcessedTotal} 项`;
    if (U.memoryFailedInfo) U.memoryFailedInfo.textContent = `共 ${S.memoryFailedTotal} 项`;
    if (U.memoryQueueMore) U.memoryQueueMore.disabled = S.memoryBusy || !S.memoryQueueHasMore;
    if (U.memoryProcessedMore) U.memoryProcessedMore.disabled = S.memoryBusy || !S.memoryProcessedHasMore;
    if (U.memoryFailedMore) U.memoryFailedMore.disabled = S.memoryBusy || !S.memoryFailedHasMore;
    renderMemoryDetailPreview();
    icons();
}

async function loadMemoryView({ force = false, quiet = false } = {}) {
    if (S.memoryBusy) return;
    S.memoryBusy = true;
    if (force) S.memoryError = "";
    renderMemoryView();
    try {
        const queueLimit = Math.max(normalizeInt(S.memoryQueueItems.length, 0), S.memoryQueuePageSize);
        const processedLimit = Math.max(normalizeInt(S.memoryProcessedItems.length, 0), S.memoryProcessedPageSize);
        const failedLimit = Math.max(normalizeInt(S.memoryFailedItems.length, 0), S.memoryFailedPageSize);
        const [queue, processed, failed] = await Promise.all([
            ApiClient.getMemoryQueue({ limit: queueLimit, offset: 0 }),
            ApiClient.getMemoryProcessed({ limit: processedLimit, offset: 0 }),
            ApiClient.getMemoryFailed({ limit: failedLimit, offset: 0 }),
        ]);
        S.memoryQueueItems = Array.isArray(queue?.items) ? queue.items : [];
        S.memoryQueueTotal = normalizeInt(queue?.total, 0);
        S.memoryQueueHasMore = !!queue?.hasMore;
        S.memoryProcessedItems = Array.isArray(processed?.items) ? processed.items : [];
        S.memoryProcessedTotal = normalizeInt(processed?.total, 0);
        S.memoryProcessedHasMore = !!processed?.hasMore;
        S.memoryFailedItems = Array.isArray(failed?.items) ? failed.items : [];
        S.memoryFailedTotal = normalizeInt(failed?.total, 0);
        S.memoryFailedHasMore = !!failed?.hasMore;
        S.memoryFailedMutationsEnabled = !!failed?.mutationsEnabled;
        S.memoryLoadedOnce = true;
        S.memoryError = "";
    } catch (error) {
        S.memoryLoadedOnce = true;
        S.memoryError = error?.message || "记忆数据加载失败";
    } finally {
        S.memoryBusy = false;
        renderMemoryView();
        maybeToastMemoryAlerts({ quiet });
    }
}

// 队列卡片正文固定渲染三行（.memory-card-queue .memory-card-body 的 line-clamp），
// 截断长度按三行的可见字数取，省略号才对应真正看不到的部分。
const MEMORY_QUEUE_BODY_CHARS = 220;

function memoryQueueSourceLabel(item) {
    const decision = String(item?.decision_source || "").trim().toLowerCase();
    const trigger = String(item?.trigger_source || "").trim().toLowerCase();
    if (decision === "user") return "用户指令";
    if (trigger.startsWith("autonomous_review")) return "自动复核";
    if (trigger) return "工具调用";
    return "自动触发";
}

function renderMemoryQueueCard(item) {
    const requestId = String(item?.request_id || "").trim();
    const statusText = memoryStatusLabel(item?.status);
    const createdAt = formatCompactTime(item?.created_at) || String(item?.created_at || "");
    const body = String(item?.payload_text || "").replace(/\s+/g, " ").trim();
    const preview = body.length > MEMORY_QUEUE_BODY_CHARS
        ? `${body.slice(0, MEMORY_QUEUE_BODY_CHARS)}…`
        : body;
    return `
        <article class="memory-card memory-card-queue" data-memory-card="queue" data-memory-detail-open="queue" data-memory-detail-key="${esc(requestId)}" role="button" tabindex="0" aria-label="打开队列请求详情">
            <div class="memory-card-head">
                <div class="memory-card-minimal-status">
                    <span class="status-badge" data-status="${esc(String(item?.status || "pending"))}">${esc(statusText)}</span>
                </div>
                <span class="memory-card-source">${esc(memoryQueueSourceLabel(item))}</span>
                <span class="memory-card-time">${esc(createdAt || "-")}</span>
                <span class="memory-card-arrow" aria-hidden="true">›</span>
            </div>
            <p class="memory-card-body">${esc(preview || "（无正文）")}</p>
        </article>
    `;
}

function renderMemoryOpChips(kinds) {
    return kinds.map((kind) => {
        const { label, icon } = MEMORY_OP_KINDS[kind];
        return `<span class="memory-op-chip" data-op="${esc(kind)}" title="${esc(label)}"><i data-lucide="${icon}" aria-hidden="true"></i>${esc(label)}</span>`;
    }).join("");
}

function renderMemoryProcessedCard(item) {
    const batchId = String(item?.batch_id || "").trim();
    const statusLabel = memoryProcessedOpLabel(item);
    const opKinds = memoryProcessedOpKinds(item);
    const processedAt = formatCompactTime(item?.processed_at) || String(item?.processed_at || "");
    const statusSlot = opKinds.length
        ? renderMemoryOpChips(opKinds)
        : `<span class="memory-op-chip" data-op="nochange"><i data-lucide="minus" aria-hidden="true"></i>${esc(statusLabel)}</span>`;
    return `
        <article class="memory-card memory-card-compact" data-memory-card="processed" data-memory-detail-open="processed" data-memory-detail-key="${esc(batchId)}" role="button" tabindex="0" aria-label="打开已处理批次详情">
            <div class="memory-card-summary">
                <div class="memory-card-minimal-row">
                    <div class="memory-card-minimal-status">
                        ${statusSlot}
                    </div>
                    <div class="memory-card-minimal-trailing">
                        <span class="memory-card-time">${esc(processedAt || "-")}</span>
                        <span class="memory-card-arrow" aria-hidden="true">›</span>
                    </div>
                </div>
            </div>
        </article>
    `;
}


async function loadMoreMemoryFailed() {
    if (S.memoryBusy || !S.memoryFailedHasMore) return;
    S.memoryBusy = true;
    renderMemoryView();
    try {
        const payload = await ApiClient.getMemoryFailed({
            limit: S.memoryFailedPageSize,
            offset: S.memoryFailedItems.length,
        });
        S.memoryFailedItems = [...S.memoryFailedItems, ...(Array.isArray(payload?.items) ? payload.items : [])];
        S.memoryFailedTotal = normalizeInt(payload?.total, S.memoryFailedTotal);
        S.memoryFailedHasMore = !!payload?.hasMore;
        S.memoryFailedMutationsEnabled = !!payload?.mutationsEnabled;
    } catch (error) {
        showToast({ title: "加载失败", text: error?.message || "失败记忆加载失败", kind: "error" });
    } finally {
        S.memoryBusy = false;
        renderMemoryView();
    }
}

async function runMemoryFailedAction(action, failedId) {
    const target = String(failedId || "").trim();
    if (!target || S.memoryFailedActionBusy) return;
    S.memoryFailedActionBusy = `${action}:${target}`;
    renderMemoryView();
    try {
        if (action === "retry") {
            await ApiClient.retryMemoryFailed(target, "manual-ui");
            showToast({ title: "已重试", text: "失败记忆已重新入队尾，等待再次处理。", kind: "success" });
        } else {
            await ApiClient.discardMemoryFailed(target, "manual-ui");
            showToast({ title: "已放弃", text: "该失败记忆已放弃，终态已记入已处理历史。", kind: "warn", durationMs: 4200 });
        }
        closeMemoryDetailPreview();
        await loadMemoryView({ force: true, quiet: true });
    } catch (error) {
        showToast({
            title: action === "retry" ? "重试失败" : "放弃失败",
            text: error?.message || "操作未成功，请稍后重试",
            kind: "error",
            durationMs: 5000,
        });
    } finally {
        S.memoryFailedActionBusy = "";
        renderMemoryView();
    }
}

function requestMemoryFailedDiscard(failedId) {
    const target = String(failedId || "").trim();
    if (!target) return;
    openConfirm({
        title: "放弃失败记忆",
        text: "放弃后这批记忆将不再重试，并写入已处理历史的废弃终态记录。确定放弃吗？",
        confirmLabel: "放弃",
        confirmKind: "danger",
        onConfirm: () => void runMemoryFailedAction("discard", target),
    });
}

function memoryFailedErrorHistoryText(item) {
    const history = Array.isArray(item?.error_history) ? item.error_history : [];
    if (!history.length) {
        return String(item?.last_error_text || "").trim() || "没有记录到错误详情。";
    }
    return history.map((entry, index) => {
        const at = formatCompactTime(entry?.at) || String(entry?.at || "");
        if (String(entry?.event || "").trim() === "requeued") {
            const trigger = String(entry?.trigger || "").trim() === "manual" ? "手动重试" : "成功信号自动";
            return `#${index + 1} [${at}] 重新入队（${trigger}）`;
        }
        const category = memoryFailedCategoryLabel(entry);
        const trigger = String(entry?.trigger || "").trim();
        const triggerLabel = trigger === "manual" ? "手动重试后失败" : trigger === "exception" ? "运行时异常" : "处理失败";
        const errorText = String(entry?.error || "").trim() || "（无错误文本）";
        return `#${index + 1} [${at}] ${category} · ${triggerLabel}\n${errorText}`;
    }).join("\n\n");
}

function renderMemoryFailedCard(item) {
    const failedId = String(item?.failed_id || "").trim();
    const categoryLabel = memoryFailedCategoryLabel(item);
    const parkedAt = formatCompactTime(item?.parked_at) || String(item?.parked_at || "");
    const errorText = String(item?.last_error_text || "").trim();
    const requestCount = Array.isArray(item?.request_ids) ? item.request_ids.length : 0;
    const actionBusy = String(S.memoryFailedActionBusy || "").trim();
    const retryDisabled = !S.memoryFailedMutationsEnabled || (actionBusy && actionBusy.endsWith(`:${failedId}`));
    const retryTitle = S.memoryFailedMutationsEnabled
        ? `重试：重新入队尾等待处理（${memoryFailedAutoRetryHint(item)}）`
        : "重试不可用：服务端未启用 G3KU_ENABLE_MEMORY_ADMIN_MUTATIONS";
    return `
        <article class="memory-card memory-card-compact memory-card-failed" data-memory-card="failed" data-memory-detail-open="failed" data-memory-detail-key="${esc(failedId)}" role="button" tabindex="0" aria-label="打开失败记忆详情">
            <div class="memory-card-summary">
                <div class="memory-card-minimal-row">
                    <div class="memory-card-minimal-status">
                        <span class="status-badge" data-status="failed">失败</span>
                        <span class="memory-failed-category" title="${esc(memoryFailedAutoRetryHint(item))}">${esc(categoryLabel)}</span>
                        ${requestCount > 1 ? `<span class="policy-chip neutral">${esc(String(requestCount))} 条</span>` : ""}
                    </div>
                    <div class="memory-card-minimal-trailing">
                        <span class="memory-card-time">${esc(parkedAt || "-")}</span>
                        <button type="button" class="memory-failed-retry-btn" data-memory-failed-retry="${esc(failedId)}" title="${esc(retryTitle)}" aria-label="重试失败记忆 ${esc(failedId)}"${retryDisabled ? " disabled" : ""}>
                            <i data-lucide="rotate-ccw" aria-hidden="true"></i>
                        </button>
                        <span class="memory-card-arrow" aria-hidden="true">›</span>
                    </div>
                </div>
                ${errorText ? `<div class="memory-card-error">${esc(memoryPreviewText(errorText, 120))}</div>` : ""}
            </div>
        </article>
    `;
}


function switchView(view) {
    const map = { ceo: U.viewCeo, tasks: U.viewTasks, skills: U.viewSkills, tools: U.viewTools, memory: U.viewMemory, models: U.viewModels, external: U.viewExternal, audit: U.viewAudit, "task-details": U.viewTaskDetails };
    const navView = view === "task-details" ? "tasks" : view;
    const leavingTaskDetails = view !== "task-details" && !!U.viewTaskDetails?.classList.contains("active");
    S.view = navView;
    U.nav.forEach((btn) => btn.classList.toggle("active", btn.dataset.view === navView));
    Object.entries(map).forEach(([key, el]) => {
        if (!el) return;
        const active = key === view;
        el.classList.toggle("active", active);
        el.style.display = active ? "" : "none";
    });
    if (view !== "task-details") {
        // 离开详情视图属于主动关闭，必须摘掉 onclose 防止触发重连。
        // 同时掐掉树加载尾巴：分块整树加载、分支重同步、快照自愈全部失效，
        // 避免回到任务大厅后剩余的树请求与整树 DOM 重建继续阻塞主线程。
        if (typeof cancelTaskTreeLoading === "function") cancelTaskTreeLoading();
        if (leavingTaskDetails) {
            // 立即掐掉详情 live 流，拆除期间不允许 live 事件继续改驻留状态/DOM。
            closeTaskDetailWs();
            // 详情退出的重步骤（视图状态捕获、大 DOM 拆除、持久化调度）推迟到下一个
            // 宏任务，时序上对齐"侧栏绕行两跳回大厅"：点击帧只做视图切换与大厅请求
            // （两者发起的请求完全一致：/api/tasks、/api/ws/tasks、worker-status），
            // 大片冷内存的集中触碰挪到下一帧，不再与大厅重绘、WS 重连同帧叠加。
            // 卸载语义见 releaseTaskDetailRetainedState：重开任务整包重载，无可见状态损失。
            window.setTimeout(() => {
                // 快速来回切换：用户已重新进入详情视图，本次拆除整体作废，状态留给新视图。
                if (U.viewTaskDetails?.classList.contains("active")) return;
                stashTaskDetailViewState();
                setTaskTokenStatsOpen(false);
                clearAgentSelection({ rerender: false });
                scheduleTaskDetailSessionPersist();
                if (typeof releaseTaskDetailRetainedState === "function") releaseTaskDetailRetainedState();
            }, 0);
        } else {
            stashTaskDetailViewState();
            setTaskTokenStatsOpen(false);
            clearAgentSelection({ rerender: false });
            closeTaskDetailWs();
            scheduleTaskDetailSessionPersist();
            // 卸载详情驻留大状态与 DOM（见 releaseTaskDetailRetainedState 注释）：
            // 降低标签页驻留内存，减轻内存吃紧机器上整页被裁剪换出造成的冻结。
            if (typeof releaseTaskDetailRetainedState === "function") releaseTaskDetailRetainedState();
        }
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
    if (view !== "memory" && S.memoryNotePreview.open) closeMemoryNotePreview();
    if (view !== "memory" && S.memoryDetailPreview.open) closeMemoryDetailPreview();
    if (view !== "memory" && S.memoryBrowser.open) closeMemoryBrowser();
    if (view === "memory") startMemoryViewAutoRefresh();
    else stopMemoryViewAutoRefresh();
    if (view === "skills") void loadSkills();
    if (view === "tools") {
        void loadTools();
        if (typeof loadToolGovernanceMode === "function") void loadToolGovernanceMode();
    }
    if (view === "memory") {
        if (!S.memoryLoadedOnce) void loadMemoryView();
        else void loadMemoryView({ quiet: true });
    }
    if (view === "models") void loadModels();
    if (view === "external") void loadExternalApiView();
    if (view === "audit") startAuditViewAutoRefresh();
    else stopAuditViewAutoRefresh();
    if (view === "audit") {
        if (!S.auditLoadedOnce) void loadAuditView();
        else void loadAuditView({ quiet: true });
    }
}

function toggleTheme() {
    const html = document.documentElement;
    const theme = html.getAttribute("data-theme") === "light" ? "dark" : "light";
    html.setAttribute("data-theme", theme);
    syncThemeToggleIcons();
    updateThemeToggleA11y();
    writeStoredUiPreference(THEME_KEY, theme);
}

function bindModelRetryToastExpansion() {
    document.addEventListener("click", (event) => {
        if (!(event.target instanceof Element)) return;
        const retryToast = event.target.closest(".model-retry-toast");
        if (retryToast) toggleModelRetryToastExpanded(retryToast);
    });
    document.addEventListener("keydown", (event) => {
        if (event.key !== "Enter" && event.key !== " ") return;
        if (!(event.target instanceof Element)) return;
        const retryToast = event.target.closest(".model-retry-toast");
        if (!retryToast) return;
        event.preventDefault();
        toggleModelRetryToastExpanded(retryToast);
    });
}

const SIDEBAR_COLLAPSED_KEY = "g3ku.ui.sidebar.collapsed.v1";
const THEME_KEY = "g3ku.ui.theme.v1";
let uiSidebarCollapsed = false;

function readStoredUiPreference(key) {
    try {
        return window.localStorage.getItem(key);
    } catch (error) {
        return null;
    }
}

function writeStoredUiPreference(key, value) {
    try {
        window.localStorage.setItem(key, value);
    } catch (error) {
        // 隐私模式或存储不可用：偏好退化为本次会话内有效。
    }
}

function readSidebarPreference() {
    const raw = readStoredUiPreference(SIDEBAR_COLLAPSED_KEY);
    if (raw === "true") return true;
    if (raw === "false") return false;
    return null;
}

const CEO_SESSION_ORDER_KEY = "g3ku.ui.ceo.session-order.v1";

function readStoredCeoSessionOrder() {
    try {
        const parsed = JSON.parse(readStoredUiPreference(CEO_SESSION_ORDER_KEY) || "[]");
        if (!Array.isArray(parsed)) return [];
        return parsed.map((id) => String(id || "").trim()).filter(Boolean);
    } catch (error) {
        return [];
    }
}

function setCeoSessionOrder(order) {
    S.ceoSessionOrder = [...(Array.isArray(order) ? order : [])];
    writeStoredUiPreference(CEO_SESSION_ORDER_KEY, JSON.stringify(S.ceoSessionOrder));
}

function updateSidebarButtonA11y() {
    if (!U.sidebarToggle) return;
    const label = uiSidebarCollapsed ? "显示名称" : "紧凑模式";
    U.sidebarToggle.setAttribute("aria-label", label);
    U.sidebarToggle.setAttribute("title", label);
    U.sidebarToggle.setAttribute("aria-expanded", String(!uiSidebarCollapsed));
}

function applySidebarState(state) {
    uiSidebarCollapsed = !!state;
    U.sidebar?.classList.toggle("is-collapsed", uiSidebarCollapsed);
    updateSidebarButtonA11y();
}

function toggleSidebar() {
    const next = !uiSidebarCollapsed;
    applySidebarState(next);
    writeStoredUiPreference(SIDEBAR_COLLAPSED_KEY, String(next));
}

function syncThemeToggleIcons() {
    const dark = document.documentElement.getAttribute("data-theme") !== "light";
    const darkIcon = U.theme?.querySelector(".dark-icon");
    const lightIcon = U.theme?.querySelector(".light-icon");
    if (darkIcon) darkIcon.style.display = dark ? "block" : "none";
    if (lightIcon) lightIcon.style.display = dark ? "none" : "block";
}

function updateThemeToggleA11y() {
    if (!U.theme) return;
    const label = document.documentElement.getAttribute("data-theme") === "light" ? "切换到深色主题" : "切换到亮色主题";
    U.theme.setAttribute("aria-label", label);
    U.theme.setAttribute("title", label);
}

function initializeTheme() {
    const theme = readStoredUiPreference(THEME_KEY) === "light" ? "light" : "dark";
    document.documentElement.setAttribute("data-theme", theme);
    syncThemeToggleIcons();
    updateThemeToggleA11y();
}

function initializeUiPreferences() {
    // 本模块会被 tests/resources 的 vm 桩环境直接求值，那里没有 documentElement。
    if (!document.documentElement) return;
    initializeTheme();
    applySidebarState(readSidebarPreference() === true);
    U.sidebarToggle?.addEventListener("click", toggleSidebar);
    S.ceoSessionOrder = readStoredCeoSessionOrder();
}

function bind() {
    U.theme?.addEventListener("click", toggleTheme);
    bindModelRetryToastExpansion();
    U.projectSettings?.addEventListener("click", () => openProjectSettingsDialog());
    U.projectSettingsClose?.addEventListener("click", () => closeProjectSettingsDialog());
    U.projectSettingsBackdrop?.addEventListener("click", (e) => {
        if (e.target === U.projectSettingsBackdrop) closeProjectSettingsDialog();
    });
    U.projectSettingsOpenPassword?.addEventListener("click", () => openPasswordChangeDialog());
    U.passwordChangeClose?.addEventListener("click", () => closePasswordChangeDialog());
    U.passwordChangeBackdrop?.addEventListener("click", (e) => {
        if (e.target === U.passwordChangeBackdrop) closePasswordChangeDialog();
    });
    U.projectSettingsChangePassword?.addEventListener("click", () => void submitProjectPasswordChange());
    U.projectSettingsAutoUnlock?.addEventListener("change", (e) => void applyProjectAutoUnlockChange(Boolean(e.target?.checked)));
    U.projectSettingsLock?.addEventListener("click", () => void lockProjectFromSettings());
    U.projectSettingsExit?.addEventListener("click", () => {
        closeProjectSettingsDialog();
        void requestProjectExit();
    });
    U.ceoFeed?.addEventListener("scroll", handleCeoFeedScrollEvent, { passive: true });
    ["wheel", "pointerdown", "keydown", "touchstart"].forEach((type) => {
        U.ceoFeed?.addEventListener(type, handleCeoFeedUserGesture, { passive: true });
    });
    U.ceoScrollToLatestBtn?.addEventListener("click", () => scrollCeoFeedToBottom());
    updateCeoScrollToLatestButton();
    U.nav.forEach((btn) => btn.addEventListener("click", () => switchView(btn.dataset.view)));
    U.backToTasks?.addEventListener("click", () => switchView("tasks"));
    U.memoryRefresh?.addEventListener("click", () => void loadMemoryView({ force: true }));
    U.memoryViewCurrent?.addEventListener("click", () => void openMemoryBrowser());
    U.memoryQueueMore?.addEventListener("click", () => void loadMoreMemoryQueue());
    U.memoryProcessedMore?.addEventListener("click", () => void loadMoreMemoryProcessed());
    U.memoryFailedMore?.addEventListener("click", () => void loadMoreMemoryFailed());
    U.auditRefresh?.addEventListener("click", () => void loadAuditView());
    U.auditPagePrev?.addEventListener("click", () => void goToAuditPage(S.auditPage - 1));
    U.auditPageNext?.addEventListener("click", () => void goToAuditPage(S.auditPage + 1));
    U.memoryQueueList?.addEventListener("click", (e) => {
        if (!(e.target instanceof Element)) return;
        const noteTrigger = e.target.closest("[data-memory-note-ref]");
        if (noteTrigger) {
            e.preventDefault();
            e.stopPropagation();
            void openMemoryNotePreview(noteTrigger.dataset.memoryNoteRef || "");
            return;
        }
        const detailTrigger = e.target.closest("[data-memory-detail-open]");
        if (!detailTrigger) return;
        e.preventDefault();
        openMemoryDetailPreview(detailTrigger.dataset.memoryDetailOpen || "", detailTrigger.dataset.memoryDetailKey || "");
    });
    U.memoryProcessedList?.addEventListener("click", (e) => {
        if (!(e.target instanceof Element)) return;
        const noteTrigger = e.target.closest("[data-memory-note-ref]");
        if (noteTrigger) {
            e.preventDefault();
            e.stopPropagation();
            void openMemoryNotePreview(noteTrigger.dataset.memoryNoteRef || "");
            return;
        }
        const detailTrigger = e.target.closest("[data-memory-detail-open]");
        if (!detailTrigger) return;
        e.preventDefault();
        openMemoryDetailPreview(detailTrigger.dataset.memoryDetailOpen || "", detailTrigger.dataset.memoryDetailKey || "");
    });
    U.memoryFailedList?.addEventListener("click", (e) => {
        if (!(e.target instanceof Element)) return;
        const retryTrigger = e.target.closest("[data-memory-failed-retry]");
        if (retryTrigger) {
            e.preventDefault();
            e.stopPropagation();
            if (retryTrigger.disabled) return;
            if (!S.memoryFailedMutationsEnabled) {
                showToast({
                    title: "重试不可用",
                    text: "服务端未启用记忆运维变更（G3KU_ENABLE_MEMORY_ADMIN_MUTATIONS）。",
                    kind: "warn",
                    durationMs: 5000,
                });
                return;
            }
            void runMemoryFailedAction("retry", retryTrigger.dataset.memoryFailedRetry || "");
            return;
        }
        const noteTrigger = e.target.closest("[data-memory-note-ref]");
        if (noteTrigger) {
            e.preventDefault();
            e.stopPropagation();
            void openMemoryNotePreview(noteTrigger.dataset.memoryNoteRef || "");
            return;
        }
        const detailTrigger = e.target.closest("[data-memory-detail-open]");
        if (!detailTrigger) return;
        e.preventDefault();
        openMemoryDetailPreview(detailTrigger.dataset.memoryDetailOpen || "", detailTrigger.dataset.memoryDetailKey || "");
    });
    [U.memoryQueueList, U.memoryProcessedList, U.memoryFailedList].forEach((root) => root?.addEventListener("keydown", (e) => {
        if (!(e.target instanceof Element)) return;
        if (e.key !== "Enter" && e.key !== " ") return;
        if (e.target.closest("[data-memory-failed-retry]")) return;
        const detailTrigger = e.target.closest("[data-memory-detail-open]");
        if (!detailTrigger) return;
        e.preventDefault();
        openMemoryDetailPreview(detailTrigger.dataset.memoryDetailOpen || "", detailTrigger.dataset.memoryDetailKey || "");
    }));
    U.taskTokenButton?.addEventListener("click", () => setTaskTokenStatsOpen(true));
    U.taskTokenClose?.addEventListener("click", () => setTaskTokenStatsOpen(false));
    U.taskTokenBackdrop?.addEventListener("click", () => setTaskTokenStatsOpen(false));
    U.taskTokenContent?.addEventListener("click", (e) => {
        const target = e.target instanceof Element ? e.target : null;
        if (!target) return;
        const control = target.closest("[data-task-model-call-page]");
        if (control) {
            const direction = String(control.dataset.taskModelCallPage || "").trim();
            if (direction === "prev") setTaskModelCallsPage((Number(S.taskModelCallsPage || 1) || 1) - 1);
            if (direction === "next") setTaskModelCallsPage((Number(S.taskModelCallsPage || 1) || 1) + 1);
            return;
        }
        if (target.closest("[data-task-model-call-refresh]")) {
            // 手动刷新：窗口打开期间唯一的数据更新入口（强制重建，保留搜索条件）。
            renderTaskTokenStats({ force: true });
        }
    });
    // 搜索输入走事件委托：只重建表格区域，输入框本身不销毁，焦点与内容不丢失。
    // 搜索作用于全部记录（而非当前页），输入即筛选并回到第 1 页。
    U.taskTokenContent?.addEventListener("input", (e) => {
        const input = e.target instanceof Element ? e.target.closest("[data-task-model-call-search]") : null;
        if (!input) return;
        S.taskModelCallsQuery = input.value;
        S.taskModelCallsPage = 1;
        refreshTaskTokenCallTable();
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
    U.ceoSessionList?.addEventListener("dragstart", (event) => beginCeoSessionCardDrag(event));
    U.ceoSessionList?.addEventListener("dragover", (event) => updateCeoSessionCardDropTarget(event));
    U.ceoSessionList?.addEventListener("drop", (event) => finishCeoSessionCardDrag(event));
    U.ceoSessionList?.addEventListener("dragend", () => cancelCeoSessionCardDrag());
    U.ceoSend?.addEventListener("click", handleCeoPrimaryAction);
    U.ceoAttach?.addEventListener("click", () => {
        if (S.ceoUploadBusy) return;
        U.ceoFileInput?.click();
    });
    U.ceoFileInput?.addEventListener("change", (e) => void handleCeoFileSelection(e));
    bindCeoModelModeControls();
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
    U.ceoFeed?.addEventListener("click", (e) => {
        const pauseCompressionBtn = e.target.closest("[data-ceo-compress-pause]");
        if (pauseCompressionBtn) {
            e.preventDefault();
            e.stopPropagation();
            requestCeoContextCompressionPause();
            return;
        }
        const editBtn = e.target.closest("[data-ceo-edit-resend]");
        if (editBtn) {
            e.preventDefault();
            e.stopPropagation();
            handleCeoEditResendClick(String(editBtn.dataset.ceoEditResend || ""));
            return;
        }
        const forkBtn = e.target.closest("[data-ceo-fork]");
        if (forkBtn) {
            e.preventDefault();
            e.stopPropagation();
            void handleCeoForkClick(String(forkBtn.dataset.ceoFork || ""));
        }
    });
    U.ceoEditResendBanner?.addEventListener("click", (e) => {
        const cancel = e.target.closest("[data-ceo-edit-resend-cancel]");
        if (!cancel) return;
        exitCeoEditResendMode({ restoreDraft: true });
        syncActiveCeoComposerDraft();
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
        scheduleSyncCeoComposerUsageOutline();
        scheduleCeoComposerUsageRefresh();
    });
    window.addEventListener("resize", () => scheduleSyncCeoComposerUsageOutline());
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
    U.modelRoleLimitsBar?.addEventListener("click", (e) => {
        const toggle = e.target.closest("[data-role-limit-toggle]");
        if (!toggle) return;
        const kind = String(toggle.dataset.roleLimitToggle || "").trim();
        if (!kind) return;
        const expanded = S.modelCatalog.roleLimitsExpanded || (S.modelCatalog.roleLimitsExpanded = {});
        expanded[kind] = !expanded[kind];
        renderRoleLimitsBar();
    });
    U.modelRoleLimitsBar?.addEventListener("input", (e) => {
        if (!S.modelCatalog.roleEditing) return;
        if (!e.target.closest("[data-model-role-limit-input]")) return;
        syncRoleIterationDraftsFromInputs({ requireValid: false });
    });
    U.modelRoleLimitsBar?.addEventListener("change", (e) => {
        if (!S.modelCatalog.roleEditing) return;
        if (!e.target.closest("[data-model-role-limit-input]")) return;
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
    U.taskSortSelect?.addEventListener("change", (e) => {
        setTaskSortMode(String(e.target.value || "time"));
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
    U.taskErrorLogButton?.addEventListener("click", () => void openTaskErrorLog());
    U.taskErrorLogClose?.addEventListener("click", () => setTaskErrorLogOpen(false));
    U.taskErrorLogBackdrop?.addEventListener("click", () => setTaskErrorLogOpen(false));
    U.closeAgent?.addEventListener("click", () => clearAgentSelection());
    U.adErrorHistoryRefresh?.addEventListener("click", () => {
        const nodeId = String(S.selectedNodeId || "").trim();
        const taskId = String(S.currentTaskId || "").trim();
        if (!taskId || !nodeId) return;
        if (S.taskNodeErrorHistories) delete S.taskNodeErrorHistories[`${taskId}:${nodeId}`];
        void renderNodeErrorHistory({ node_id: nodeId }, { force: true });
    });
    // 消息列表标题右侧的定向通知输入框（效果 = task_append_notice 定向通知）。
    U.adNoticeSend?.addEventListener("click", () => { void submitNodeNoticeComposer(); });
    U.adNoticeInput?.addEventListener("keydown", (event) => {
        if (event.key === "Enter" && !event.isComposing) {
            event.preventDefault();
            void submitNodeNoticeComposer();
        }
    });
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
        if (!e.target.closest(".resource-select-shell")) closeResourceSelects();
        if (!e.target.closest("#ceo-model-mode-panel") && !e.target.closest("#ceo-context-usage-brain")) {
            closeCeoModelModePanel();
        }
        if (!e.target.closest(".ceo-session-actions.toolbar-dropdown")) closeCeoSessionMenus();
        if (!e.target.closest(".toolbar-dropdown")) closeTaskMenus();
    });
    document.addEventListener("keydown", (e) => {
        if (e.key !== "Escape") return;
        if (closeResourceSelects({ restoreFocus: true })) return;
        if (closeCeoModelModePanel()) {
            U.ceoComposerUsageBrain?.focus?.();
            return;
        }
        if (closeCeoSessionMenus({ restoreFocus: true })) return;
        if (isPasswordChangeOpen()) {
            closePasswordChangeDialog();
            return;
        }
        if (isProjectSettingsOpen()) {
            closeProjectSettingsDialog();
            U.projectSettings?.focus?.();
            return;
        }
        if (S.confirmState) {
            closeConfirm();
            return;
        }
        if (closeTaskMenus({ restoreFocus: true })) return;
        if (S.taskErrorLogOpen) {
            setTaskErrorLogOpen(false);
            return;
        }
        if (S.taskTokenStatsOpen) {
            setTaskTokenStatsOpen(false);
            return;
        }
        if (S.memoryDetailPreview.open) {
            closeMemoryDetailPreview();
            return;
        }
        if (S.memoryNotePreview.open) {
            closeMemoryNotePreview();
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
    bindAuditBadge();
    void refreshAuditBadge();
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
    if (typeof bindTaskTreeSearch === "function") bindTaskTreeSearch();
    icons();
    renderTaskDepthControl();
    void loadTaskDefaults();
    renderSkillActions();
    renderToolActions();
    void loadModels();
    if (typeof initExternalApiView === "function") initExternalApiView();
    void loadTasks();
    void restoreTaskDetailSession();
    S.ceoScrollToLatestOnSnapshot = true;
    void refreshCeoSessions({ reconnect: true }).catch(() => {
        initCeoWs();
    });
}

initializeUiPreferences();
document.addEventListener("DOMContentLoaded", maybeInit);
window.addEventListener("g3ku:boot-unlocked", maybeInit);
