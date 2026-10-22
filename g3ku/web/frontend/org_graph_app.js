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
// 快照忙标志的兜底解除时长，见 initCeoWs 的 onopen。
const CEO_SNAPSHOT_BUSY_GUARD_MS = 10000;
const CEO_CONTEXT_LOAD_NOTICE_DURATION_MS = 10000;
// 长按上下文脑图标：按住先静置 200ms 起手，之后才开始计时并显示进度环。
// 目的是让普通点击（含手抖的短按）完全不出现压缩进度反馈，计时从起手完成的那一刻算满 2 秒。
const CEO_BRAIN_HOLD_ARM_MS = 200;
// 长按上下文脑图标满 2 秒即发起手动压缩；进度环按住期间连续刷新。
const CEO_BRAIN_LONG_PRESS_MS = 2000;
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
    updateStatus: null,
    ceoWs: null,
    ceoWsToken: 0,
    ceoWsLastErrorCode: "",
    ceoWsParseResyncs: 0,
    ceoPendingTurns: [],
    ceoPausedArchiveTurns: {},
    ceoTurnActive: false,
    ceoPauseBusy: false,
    ceoUploads: [],
    ceoUploadBusy: false,
    // 语音输入：S.ceoVoice 只在录音期间非空（按钮即录音态开关），
    // S.ceoVoiceBusy 覆盖"已停止、正在本机转写"这段没有录音但也不能重开的窗口。
    ceoVoice: null,
    ceoVoiceBusy: false,
    // 自动发送开关：默认开（点麦克风的人就是要"说完就走"），实际取值由
    // restoreCeoVoiceAutoSend 从 localStorage 覆盖；null 表示还没读过存储值。
    ceoVoiceAutoSend: null,
    // 顶边拖拽得到的输入框高度；0 = 未拖过，仍按内容自动增高。
    ceoInputManualHeight: 0,
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
    // 渠道分组折叠态同样只在前端与 localStorage 生效，键是 channel_id。
    ceoChannelCollapsed: [],
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
        // 打开 picker 的目的：pin=选一个固定模型，append=往会话链里加成员。
        pickerPurpose: "pin",
        chainKeys: [],
        // 链栏两态：false=只读（只有「编辑」），true=编辑（把手、叉号与三点动作行）。
        // 只读态永远显示当前生效的那条链，会话没自带链时就是全局链，随全局自动更新。
        chainEditing: false,
        // 会话链原文（空数组=跟随全局角色链）；chainKeys 是面板草稿，基线由两者共同决定。
        sessionChainKeys: [],
        isSessionChain: false,
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
        maxDepth: 10,
        defaultMaxDepth: 10,
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
    taskModelCallPaging: null,
    taskModelCallPageRows: [],
    taskModelCallPageLoading: false,
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
    taskPerfFreshness: null,
    taskTokenStatsOpen: false,
    taskTokenLedgerRefreshing: false,
    // 按模型明细是按任务取的：存"这份属于哪个任务"，窗口用它区分还在取数与真的没有。
    taskTokenLedgerTaskId: "",
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
    // 卡片数字的"谁写的"与"有没有回退过"两条观测（S.taskHallStats 同族，只在控制台读）。
    taskItemStateSource: {},
    taskMetricRollbacks: [],
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
        // 负载均衡组：链里用 `group:<key>` 记号占位，成员与预算放在这里。
        loadBalanceGroups: {},
        loadBalanceGroupDrafts: {},
        // 组配置弹窗的表单态：open=false 时不显示；editingKey="" 表示新建。
        groupDialog: {
            open: false,
            editingKey: "",
            name: "",
            modelKeys: [],
            maxRetryRounds: 1,
            error: "",
        },
        // 空闲刷新设置：`saved` 是服务端读数，草稿不等它——只有「应用」才写回。
        contextRefresh: {
            open: false,
            enabled: true,
            idleMinutes: 120,
            saved: { enabled: true, idleMinutes: 120 },
            loading: false,
            saving: false,
            error: "",
        },
        defaults: DEFAULT_MODEL_DEFAULTS(),
        loading: false,
        saving: false,
        error: "",
        search: "",
        selectedModelKey: "",
        mode: "view",
        // 草稿会话由第一次交互自动开启（models/actions.js 的 ensureModelRoleDraftSession）：
        // 页面上没有「进入编辑」这一步。读侧仍按「有会话读草稿、没会话读服务端」分流，
        // 所以这颗初始值是 false，不是漏改。
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
        // 归并记忆浏览用同一个窗切换两种形态：index=分类列表，note=某份 note 正文。
        // trail 是从列表进来的那一串点开的 ref，最后一项即当前；非 index 入口不填它。
        mode: "note",
        origin: "direct",
        trail: [],
        indexItems: [],
        indexBusy: false,
        indexError: "",
        indexToken: 0,
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
    ceoInputResizeHandle: document.getElementById("ceo-input-resize-handle"),
    ceoAttach: document.getElementById("ceo-attach-btn"),
    ceoVoiceBtn: document.getElementById("ceo-voice-btn"),
    ceoVoiceOptions: document.getElementById("ceo-voice-options"),
    ceoVoiceAutoSend: document.getElementById("ceo-voice-autosend"),
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
    ceoModelChainHint: document.getElementById("ceo-model-chain-hint"),
    ceoModelChainList: document.getElementById("ceo-model-chain-list"),
    ceoModelChainEmpty: document.getElementById("ceo-model-chain-empty"),
    ceoModelChainConfirm: document.getElementById("ceo-model-chain-confirm"),
    ceoModelChainConfirmText: document.getElementById("ceo-model-chain-confirm-text"),
    ceoModelChainConfirmAccept: document.getElementById("ceo-model-chain-confirm-accept"),
    ceoModelChainConfirmCancel: document.getElementById("ceo-model-chain-confirm-cancel"),
    ceoModelChainActions: document.getElementById("ceo-model-chain-actions"),
    ceoModelChainEdit: document.getElementById("ceo-model-chain-edit"),
    ceoModelChainReset: document.getElementById("ceo-model-chain-reset"),
    ceoModelChainAdd: document.getElementById("ceo-model-chain-add"),
    ceoModelChainApply: document.getElementById("ceo-model-chain-apply"),
    ceoModelPicker: document.getElementById("ceo-model-picker"),
    ceoModelPickerSearch: document.getElementById("ceo-model-picker-search"),
    ceoModelPickerList: document.getElementById("ceo-model-picker-list"),
    ceoModelPickerEmpty: document.getElementById("ceo-model-picker-empty"),
    ceoModelModeNote: document.getElementById("ceo-model-mode-note"),
    ceoComposerUsageBrain: document.getElementById("ceo-context-usage-brain"),
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
    memoryIndexNotes: document.getElementById("memory-index-notes-btn"),
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
    modelContextRefresh: document.getElementById("model-context-refresh-btn"),
    contextRefreshBackdrop: document.getElementById("context-refresh-backdrop"),
    contextRefreshDialog: document.getElementById("context-refresh-dialog"),
    contextRefreshEnabled: document.getElementById("context-refresh-enabled"),
    contextRefreshMinutes: document.getElementById("context-refresh-minutes"),
    contextRefreshApply: document.getElementById("context-refresh-apply-btn"),
    contextRefreshError: document.getElementById("context-refresh-error"),
    contextRefreshCancel: document.getElementById("context-refresh-cancel-btn"),
    contextRefreshClose: document.getElementById("context-refresh-close-btn"),
    modelRoleEditors: document.getElementById("model-role-editors"),
    modelRoleLimitsBar: document.getElementById("model-role-limits-bar"),
    modelSearch: document.getElementById("model-search-input"),
    modelList: document.getElementById("model-list"),
    modelGroupList: document.getElementById("model-group-list"),
    modelGroupCreate: document.getElementById("model-group-create-btn"),
    modelGroupBackdrop: document.getElementById("model-group-backdrop"),
    modelGroupDialog: document.getElementById("model-group-dialog"),
    modelGroupDialogBody: document.getElementById("model-group-dialog-body"),
    modelGroupClose: document.getElementById("model-group-close-btn"),
    modelGroupTitle: document.getElementById("model-group-title"),
    modelGroupCancel: document.getElementById("model-group-cancel-btn"),
    modelGroupConfirm: document.getElementById("model-group-confirm-btn"),
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
    taskSelectionBar: document.getElementById("task-selection-bar"),
    taskSelectionCount: document.getElementById("task-selection-count"),
    taskClearSelection: document.getElementById("task-clear-selection"),
    taskSortSelect: document.getElementById("task-sort-select"),
    taskFilterWrap: document.getElementById("task-filter-wrap"),
    taskFilterTrigger: document.getElementById("task-filter-menu-trigger"),
    taskFilterMenu: document.getElementById("task-filter-menu"),
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
    adTokenSummary: document.getElementById("ad-token-summary"),
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
    toastIcon: document.getElementById("app-toast-icon"),
    taskLoadNotice: document.getElementById("task-load-notice"),
    taskLoadNoticeText: document.getElementById("task-load-notice-text"),
    confirmBackdrop: document.getElementById("confirm-backdrop"),
    confirmTitle: document.getElementById("confirm-title"),
    confirmText: document.getElementById("confirm-text"),
    confirmOptions: document.getElementById("confirm-options"),
    confirmInputRow: document.getElementById("confirm-input-row"),
    confirmInputLabel: document.getElementById("confirm-input-label"),
    confirmInput: document.getElementById("confirm-input"),
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
    projectSettingsOpenBundle: document.getElementById("project-settings-open-bundle-btn"),
    readingScale: document.getElementById("project-settings-reading-scale"),
    updateNavDot: document.getElementById("update-nav-dot"),
    projectSettingsUpdateText: document.getElementById("project-settings-update-text"),
    projectSettingsCheckUpdate: document.getElementById("project-settings-check-update-btn"),
    projectSettingsApplyUpdate: document.getElementById("project-settings-apply-update-btn"),
    configBundleBackdrop: document.getElementById("config-bundle-backdrop"),
    configBundleDialog: document.getElementById("config-bundle-dialog"),
    configBundleClose: document.getElementById("config-bundle-close-btn"),
    configBundleUseProjectPassword: document.getElementById("config-bundle-use-project-password"),
    configBundleExportConfig: document.getElementById("config-bundle-export-config"),
    configBundleExportMemory: document.getElementById("config-bundle-export-memory"),
    configBundleExportPasswordField: document.getElementById("config-bundle-export-password-field"),
    configBundleExportConfirmField: document.getElementById("config-bundle-export-confirm-field"),
    configBundleExportPassword: document.getElementById("config-bundle-export-password"),
    configBundleExportConfirm: document.getElementById("config-bundle-export-confirm"),
    configBundleExport: document.getElementById("config-bundle-export-btn"),
    configBundleImportFile: document.getElementById("config-bundle-import-file"),
    configBundleImportPick: document.getElementById("config-bundle-import-pick-btn"),
    configBundleImportName: document.getElementById("config-bundle-import-name"),
    configBundleImportConfig: document.getElementById("config-bundle-import-config"),
    configBundleImportMemory: document.getElementById("config-bundle-import-memory"),
    configBundleImportPassword: document.getElementById("config-bundle-import-password"),
    configBundleImport: document.getElementById("config-bundle-import-btn"),
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
// lucide 把 `<i data-lucide>` 换成 `<svg>` 时连属性一起复制，所以 `[data-lucide]` 这个待渲染
// 集合永远非空：每一次 createIcons() 都会把整份文档的图标重画一遍（实测 1654 个 = 38–58 ms，
// 且随文档变大线性上涨：633→11–25 ms、2885→46–103 ms）。画完摘掉 svg 上的标记，下一趟只剩
// 真正待渲染的 `<i>`。没有代码读这个属性的值，CSS 也不选 `svg[data-lucide]`。
const ICON_MARKER_ATTR = "data-lucide";
const clearRenderedIconMarkers = () => {
    document.querySelectorAll(`svg[${ICON_MARKER_ATTR}]`).forEach((node) => node.removeAttribute(ICON_MARKER_ATTR));
};
const runIconPass = () => {
    if (!window.lucide || typeof lucide.createIcons !== "function") return;
    lucide.createIcons();
    clearRenderedIconMarkers();
};
let __iconsRafId = 0;
const icons = (force = false) => {
    if (!window.lucide || typeof lucide.createIcons !== "function") return;
    if (force) {
        if (__iconsRafId) window.cancelAnimationFrame(__iconsRafId);
        __iconsRafId = 0;
        runIconPass();
        return;
    }
    if (__iconsRafId) return;
    __iconsRafId = window.requestAnimationFrame(() => {
        __iconsRafId = 0;
        runIconPass();
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

// 两条轴各判各的：can_message 管"能不能往里发"，is_readonly 管"历史能不能改"。
// 目录里查不到该行（刚建的本地会话）按可写处理，与服务端未下发时同样回退旧语义。
function activeSessionCanMessage() {
    const item = activeSessionItem();
    if (!item) return true;
    return typeof item.can_message === "boolean" ? item.can_message : !item.is_readonly;
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

function ceoChannelUnreadTotal(items = []) {
    // 与卡片角标同一口径：当前会话的 unread 恒为 0，所以合计里也剔掉它。
    const activeId = activeSessionId();
    return (Array.isArray(items) ? items : [])
        .map((item) => String(item?.session_id || "").trim())
        .filter((sessionId) => sessionId && sessionId !== activeId)
        .reduce((total, sessionId) => total + sessionUnreadCount(sessionId), 0);
}

function clearCeoBulkSelection() {
    S.ceoSelectedSessionIds = new Set();
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

function ceoSessionCreatedTime(session) {
    const item = session && typeof session === "object" ? session : {};
    return String(item.created_at || item.updated_at || "").trim();
}

function ceoSessionManualRank(sessionId) {
    const order = Array.isArray(S.ceoSessionOrder) ? S.ceoSessionOrder : [];
    if (!order.length) return -1;
    return order.indexOf(String(sessionId || "").trim());
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

// 一格显示的正文上限与服务端「一屏内容」契约同源（g3ku/content/navigation.py 的
// OPEN_EXCERPT_CHAR_LIMIT），不是新档：超过它就不交给浏览器排版，改走「查看完整」。
const TRACE_DISPLAY_CHAR_LIMIT = 16000;
const TRACE_DISPLAY_OMITTED_PATTERN = /\n\n……（本面板只显示前[\s\S]*$/;

function capTraceDisplayText(text) {
    const value = String(text ?? "");
    if (value.length <= TRACE_DISPLAY_CHAR_LIMIT) return value;
    return `${value.slice(0, TRACE_DISPLAY_CHAR_LIMIT)}\n\n……（本面板只显示前 ${TRACE_DISPLAY_CHAR_LIMIT} 字符，点「查看完整」看全文）`;
}

function decodeEscapedDisplayText(value) {
    const raw = String(value ?? "");
    if (!raw.trim()) return "";
    // 巨型正文原样返回：下面五遍全文扫描 + 两次整串 replace 只是为了判断转义形态，
    // 几 MB 的载荷光这几步就够把主线程占满（2026-10-11 点开 7 MB 节点详情实测）。
    if (raw.length > TRACE_DISPLAY_CHAR_LIMIT) return raw;

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
        // 超一屏就不再做 parse + indent 重排：那两步会把 7 MB 变成两份内存副本。
        if (decoded.length > TRACE_DISPLAY_CHAR_LIMIT) return decoded;
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

function traceOutputWindowCacheKey(outputRef = "", view = "canonical", startLine = null, endLine = null) {
    const normalizedRef = normalizeTraceOutputRef(outputRef);
    if (!normalizedRef) return "";
    const normalizedView = String(view || "canonical").trim().toLowerCase() || "canonical";
    return `${normalizedView}:${normalizedRef}:${startLine ?? ""}-${endLine ?? ""}`;
}

// 输出正文按「服务端一屏」取，不再整份搬到浏览器：/api/content/read 会把 artifact
// 全文（实测单行 7,024,971 字符的节点详情）一次性交给页面，decode + indent 重排 +
// 一次性 DOM 注入三步都在主线程，整页就是这样冻住的。
// 首屏必须一个行参数都不传：navigation.open() 只要看到 start_line 就走区间分支，
// 缺 end_line 时把 finish 收成 start —— 只回一行。续读才带两端，跨度用服务端
// 首屏自己给出的行数，省得前端复刻 MAX_OPEN_LINES。
async function getTraceOutputWindowByRef(outputRef = "", { view = "canonical", startLine = null, endLine = null } = {}) {
    const emptyWindow = { text: "", startLine: 1, endLine: 0, lineCount: 0, truncated: false };
    const normalizedRef = normalizeTraceOutputRef(outputRef);
    if (!normalizedRef) return emptyWindow;
    ensureTraceOutputContentState();
    const cacheKey = traceOutputWindowCacheKey(normalizedRef, view, startLine, endLine);
    if (!cacheKey) return emptyWindow;
    if (Object.prototype.hasOwnProperty.call(S.traceOutputContentByKey, cacheKey)) {
        const cached = S.traceOutputContentByKey[cacheKey];
        if (cached && typeof cached === "object" && cached.missing) {
            throw traceOutputMissingError();
        }
        return cached || emptyWindow;
    }
    if (S.traceOutputRequestsByKey[cacheKey]) {
        return S.traceOutputRequestsByKey[cacheKey];
    }
    const request = (async () => {
        try {
            const payload = typeof ApiClient?.openContent === "function"
                ? await ApiClient.openContent({ ref: normalizedRef, view, startLine, endLine })
                : null;
            const chunk = {
                text: extractTraceOutputContentText(payload),
                startLine: Number(payload?.start_line ?? startLine ?? 1),
                endLine: Number(payload?.end_line || 0),
                lineCount: Number(payload?.line_count || 0),
                truncated: !!payload?.truncated,
            };
            S.traceOutputContentByKey[cacheKey] = chunk;
            return chunk;
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

async function getTraceOutputContentByRef(outputRef = "", { view = "canonical" } = {}) {
    const chunk = await getTraceOutputWindowByRef(outputRef, { view });
    return String(chunk?.text || "");
}

// 继续加载的判据是"文件还有下文"，不是服务端的 truncated：后者只说"这一段没给全"，
// 正好发满一屏时它是 false，而 420 行里这才读到第 168 行。单行巨型 artifact 的
// end_line 恒等于 start_line（行窗推不动，再要一次只会拿回同一段），所以按推进过
// 才算数。
function setTraceOutputMoreState(element, chunk = null) {
    if (!(element instanceof HTMLElement) || typeof element.closest !== "function") return;
    const field = element.closest(".task-trace-field");
    const button = field?.querySelector?.(".task-trace-more");
    if (!(button instanceof HTMLElement)) return;
    const endLine = Number(chunk?.endLine || 0);
    const lineCount = Number(chunk?.lineCount || 0);
    const advanced = endLine > Number(chunk?.startLine || 1);
    const hasMore = lineCount ? endLine < lineCount : !!chunk?.truncated;
    button.hidden = !(advanced && hasMore);
    if (button.hidden) button.disabled = false;
}

async function loadMoreTraceOutputBlock(button) {
    if (!(button instanceof HTMLElement)) return;
    const field = typeof button.closest === "function" ? button.closest(".task-trace-field") : null;
    const block = field?.querySelector?.(".task-trace-output-value");
    if (!(block instanceof HTMLElement)) return;
    const outputRef = normalizeTraceOutputRef(block.dataset.outputRef || "");
    const startLine = Number(block.dataset.outputEndLine || 0) + 1;
    // 跨度取上一屏自己给出的行宽（首屏 endLine-startLine+1），不复制服务端的 200。
    const span = Math.max(1, Number(block.dataset.outputWindowSpan || 0));
    if (!outputRef || startLine < 2 || !span) return;
    const endLine = startLine + span - 1;
    button.disabled = true;
    try {
        const chunk = await getTraceOutputWindowByRef(outputRef, { startLine, endLine });
        const body = String(block.textContent || "").replace(TRACE_DISPLAY_OMITTED_PATTERN, "");
        const next = `${body}${body ? "\n" : ""}${String(chunk?.text || "").replace(/^\n+/, "")}`;
        setTextContentPreservingScroll(block, next);
        block.dataset.outputEndLine = String(chunk?.endLine || block.dataset.outputEndLine || "");
        setTraceOutputMoreState(block, chunk);
    } catch (error) {
        button.disabled = false;
        throw error;
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
        const chunk = await getTraceOutputWindowByRef(outputRef, { view });
        const nextText = String(chunk?.text || previewText || emptyText).trim() || emptyText;
        setTextContentPreservingScroll(element, nextText);
        element.dataset.outputHydrated = "true";
        element.dataset.outputEndLine = String(chunk?.endLine || "");
        element.dataset.outputWindowSpan = String(Math.max(0,
            Number(chunk?.endLine || 0) - Number(chunk?.startLine || 1) + 1));
        setTraceOutputMoreState(element, chunk);
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

const TRACE_ARGUMENTS_MISSING_TEXT = "完整入参已不在账本里，这里只有调用提示";

// 入参的按需车道：转录投影会把超长入参清空，
// 原文仍在 frontdoor 账本里，所以点开工具面板时按 tool_call_id 回取一次。
async function getTraceArgumentsContentByCallId(toolCallId = "") {
    const normalized = String(toolCallId || "").trim();
    if (!normalized) return "";
    ensureTraceOutputContentState();
    const cacheKey = `arguments:${normalized}`;
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
            const payload = await ApiClient.getCeoToolArguments(activeSessionId(), normalized);
            const text = String(payload?.arguments_text || "").trim();
            if (!text) {
                S.traceOutputContentByKey[cacheKey] = { missing: true };
                throw traceOutputMissingError();
            }
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

async function ensureTraceArgumentsCodeBlockContent(element) {
    if (!(element instanceof HTMLElement)) return "";
    const lookupId = String(element.dataset.argumentsLookup || "").trim();
    if (!lookupId) return String(element.textContent || "");
    if (element.dataset.outputHydrated === "true") return String(element.textContent || "");
    const previewText = String(element.dataset.previewText || element.textContent || "");
    element.dataset.previewText = previewText;
    element.dataset.outputHydrating = "true";
    setTextContentPreservingScroll(element, "正在加载完整参数...");
    try {
        const fullText = await getTraceArgumentsContentByCallId(lookupId);
        const nextText = String(fullText || previewText).trim() || previewText;
        // 入参没有 ref 可以再取一次，只能就地封顶显示；复制仍拿全文。
        setTextContentPreservingScroll(element, capTraceDisplayText(nextText));
        element.dataset.outputHydrated = "true";
        return nextText;
    } catch (error) {
        if (isTraceOutputMissingError(error)) {
            setTextContentPreservingScroll(element, `${previewText}\n\n${TRACE_ARGUMENTS_MISSING_TEXT}`);
            element.dataset.outputHydrated = "cleaned";
            return previewText;
        }
        const message = typeof ApiClient?.friendlyErrorMessage === "function"
            ? ApiClient.friendlyErrorMessage(error, error?.message || "未知错误")
            : String(error?.message || error || "未知错误");
        setTextContentPreservingScroll(element, `${previewText}\n\n加载完整参数失败：${message}`);
        element.dataset.outputHydrated = "error";
        return previewText;
    } finally {
        delete element.dataset.outputHydrating;
    }
}

async function ensureCeoToolStepFullOutput(item, { view = "canonical" } = {}) {    if (!(item instanceof HTMLElement)) return "";
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
        resolved_model_key: String(item.resolved_model_key || item.resolvedModelKey || "").trim(),
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
        resolved_model_key: String(diagnostics.resolved_model_key || "").trim(),
        trigger_tokens: activeTriggerTokens > 0 ? Math.floor(activeTriggerTokens) : 0,
        would_trigger_token_compression: activeTriggerTokens > 0 && estimatedTotalTokens >= activeTriggerTokens,
        would_exceed_context_window: estimatedTotalTokens > contextWindowTokens,
        missing_context_window: false,
        effective_input_tokens: Number(diagnostics.effective_input_tokens || 0),
        estimate_source: String(diagnostics.estimate_source || "").trim(),
        source: "runtime_snapshot",
    };
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

// 组槽位按它当前的成员展开：一个组里混进不收图的成员，和链上直接写一个不收图的模型，
// 在运行时是同一个后果（整条链按不支持处理）。
// 混链＝链上既有收图的也有不收图的：收图的那些会被整条判掉，这才是这次保存让人失去的东西。
// 全链都不收图时不提示——那条链本来也看不了图，保存没有让它变得更差。
// 只有这三条链的图片能力会被运行时读到：记忆车道没有任何图片面，提示它等于说谎。
const IMAGE_JUDGED_MODEL_SCOPES = ["ceo", "execution", "inspection"];

function roleChainUpdateToChain(payload = {}) {
    if (Array.isArray(payload.modelKeys)) return payload.modelKeys;
    return (payload.routeEntries || []).map((entry) => (
        String(entry?.type || "") === "load_balance" ? groupRefToken(entry.group_key) : String(entry?.model_key || "")
    ));
}

// 一次批量保存可能改到多条链：把混链的成员名并成一个列表，只弹一次。
function chainsWithImageMix(chains) {
    const names = [];
    (chains || []).forEach((chain) => {
        if (!modelChainMixedImageCapability(chain)) return;
        modelChainImageIncapableNames(chain).forEach((name) => {
            if (!names.includes(name)) names.push(name);
        });
    });
    return names;
}

async function confirmChainsImageCapabilityMix(chains) {
    const names = chainsWithImageMix(chains);
    if (!names.length) return true;
    return await new Promise((resolve) => {
        openConfirm({
            title: "链中包含不支持图片的成员",
            text: `链中包含不支持图片的成员：${names.join("、")}，agent 将无法查看图片。是否确定？`,
            confirmLabel: "保存",
            confirmKind: "danger",
            onConfirm: () => resolve(true),
            onClose: () => resolve(false),
        });
    });
}

// 「同步全局模型链」＝把服务端此刻的那条全局链拉下来当底稿：本地缓存可能已经被模型页改过，
// 所以先重读一次目录，再按最新的全局链铺列表，而不是留一个空列表让人猜同步成功了没有。
// 固定模型被删除/禁用时运行时已回退模型链：面板按实际生效的模式展示。
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


function activeCeoSessionHasHistory(sessionId = activeSessionId()) {
    const key = String(sessionId || "").trim();
    if (!key) return false;
    const item = (S.ceoSessions || []).find((entry) => String(entry?.session_id || "").trim() === key) || null;
    if (item) return sessionMessageCount(item) > 0;
    // 目录里查不到时（例如刚从缓存渲染）退回已缓存的转录条数。
    const cached = getCeoSessionSnapshotCache(key);
    return !!(cached?.messages || []).length;
}

const CEO_MODEL_RETRY_LIVE_STATES = new Set(["retrying", "waiting_upstream"]);

function normalizeCeoModelRetryStatus(value = null) {
    if (!value || typeof value !== "object") return null;
    const state = String(value?.state || "").trim().toLowerCase();
    if (!CEO_MODEL_RETRY_LIVE_STATES.has(state)) return null;
    const rawCount = Number.parseInt(String(value?.retry_count ?? ""), 10);
    const retryCount = Number.isFinite(rawCount) && rawCount >= 0 ? rawCount : 0;
    const rawRound = Number.parseInt(String(value?.chain_round ?? ""), 10);
    const chainRound = Number.isFinite(rawRound) && rawRound > 0 ? rawRound : 0;
    const rawDelay = Number(value?.delay_seconds);
    const delaySeconds = Number.isFinite(rawDelay) && rawDelay >= 0 ? rawDelay : 0;
    const next = {
        state,
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
    if (state === "waiting_upstream") {
        const rawWaiting = Number.parseInt(String(value?.waiting_seconds ?? ""), 10);
        if (Number.isFinite(rawWaiting) && rawWaiting > 0) next.waiting_seconds = rawWaiting;
        const rawChunks = Number.parseInt(String(value?.chunk_count ?? ""), 10);
        if (Number.isFinite(rawChunks) && rawChunks > 0) next.chunk_count = rawChunks;
        const chunkKinds = String(value?.chunk_kinds || "").trim();
        if (chunkKinds) next.chunk_kinds = chunkKinds;
    }
    return next;
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

// 图标自绘而不是取 lucide 字形：loader-circle 是中心对称圆环，转起来看不出动静；
// 这里的旋转弧负责「还在跑」，中间两条竖杠负责「点这里可以暂停」。
// 只有最后一条中断线带重试动作。它排在最后这件事本身就是安全条件：有回合在跑时新气泡
// 会插在它后面，往上翻历史时它又不在视口里；更早的中断线只是历史，不该长成控件。
// 转录里的回合行 key 是 `m:<turn_id>:assistant:<n>`（含 `:followup:` 变体），实时气泡是
// `turn:<turn_id>`。同一个回合可能留下多行，线跟着最后渲染的那一行。key 只打在 feed 的
// 直接子元素上，所以这里扫 children 而不是 querySelectorAll。
// 一条线的身份 = (回合, 状态, 边界)：同一回合压两次会得到两条边界不同的线，都该留下；
// 而"当场画的实时帧"与"重建按转录画的那一条"是同一件事，不能画两遍。
// 放置唯一判据：实时道与重建道都只消费这里的结果。
// boundary 0 是有效落点；null/缺失表示“没有边界”，退回行位置。
// 头段是冻结的那半：它不再收帧，所以没有正文可显示时不能把加载动画留在线上头。
// 实时道：把正在跑的那个回合就地切成两段——头段冻结在边界之前那几组，续跑段接住之后
// 的每一帧（轨道、流式文字、用量）。中间夹一条 feed 级的线，于是"压缩之后才发进来的用户
// 插话"按它既有的"插到当前气泡之前"规则落点，天然就落到了线下方。
//
// 判据分三种，都只为同一件事服务：**线上方定住，线下方继续长**。
//   - boundary 0，或已知轨道全在边界之下 ⇒ 这一回合没有任何组被折进摘要，整条气泡都在
//     线下方，线插在气泡之前，不切；
//   - 轨道还没到（total 0 且 boundary > 0）⇒ 不知道哪几组被折，但"当前已渲染的这一截"
//     就是压缩那一刻的界面，必须整段冻在线上方；照切，续跑段不设轨道窗口兜住之后的一切；
//   - 有组在线上方 ⇒ 按回合内 seq 切，头段只留 <= boundary 的那几组。
// 唯一不切的是已收尾的历史回合：它之后不会再有内容，线交给 findCeoCompressionAnchor 兜底。
// 压缩帧要切的那个实时回合。turn_id 对得上就用它；对不上时认下**唯一那条还没拿到
// turn_id 的实时气泡**——手动暂停/首帧竞态里，标记帧可能比第一条 turn patch 先到，
// 此时本地气泡还是匿名的，放过它就只能把线追到 feed 尾部，之后的新内容全在线上方。
// 压缩帧到达时这一回合还没有轨道（首跳就压缩、或轨道帧还没到）：线已经画在气泡
// 之间了，但两半都没法按 seq 分——于是续跑段先把整份轨道画出来，等轨道第一次
// 到达时再补切一次：<= boundary 的组搬回头段，续跑段只留边界之后的。
// 不补这一步，压缩之后新跑出来的内容会跟被折进去的那些混在同一段里，界面上看不出
// 哪些是"已经压过的历史"、哪些是"压缩之后的新内容"——正是用户报的那个形状。
// 刷新/重连后的整页重建：进行中回合由实时道画在 feed 尾部，而转录里的压缩标记行只能
// 落在它前面（这一回合自己的行还没落盘）。按行位置画的那条线会把"压缩之后才跑出来的
// 内容"整段留在线上方。所以重建后要把这条规则从行位置摘下来，改按回合边界切进进行中
// 回合里——与实时帧 announce 当场的切法保持同一落点。
// running 状态随快照缓存落 sessionStorage：刷新后能先挂线，不必等整份 snapshot.ceo。
// seen_at 每次确认 running 都刷新；终局写 null 清键。
// 终局不再先删线等重连：把当前 running 线就地改成终态，重连快照到达后由整页重建替换。
function convertCeoRunningDividerToTerminal(state) {
    if (!U.ceoFeed) return false;
    if (state !== "completed" && state !== "paused") return false;
    const selector = `.${CEO_COMPRESSION_DIVIDER_CLASS}.is-running`;
    const first = typeof U.ceoFeed.querySelector === "function" ? U.ceoFeed.querySelector(selector) : null;
    const all = typeof U.ceoFeed.querySelectorAll === "function"
        ? Array.from(U.ceoFeed.querySelectorAll(selector))
        : [];
    const el = [first, ...all].find(Boolean);
    if (!el) return false;
    const classes = new Set(String(el.className || "").split(/\s+/).filter(Boolean));
    classes.delete("is-running");
    classes.add(`is-${state}`);
    el.className = [...classes].join(" ");
    if (el.dataset) el.dataset.ceoCompressionState = state;
    el.setAttribute?.("aria-live", "off");
    el.innerHTML = ceoCompressionDividerInner(state, { interactive: false });
    syncCeoCompressionRetryAffordances();
    return true;
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
    if (normalized.state === "waiting_upstream") {
        // 上游还在滴分片、只是没有任何可显示的产出：报秒数与分片数，不谎称在重试。
        const parts = [`等待上游响应 ${Math.max(0, Number(normalized.waiting_seconds || 0))} 秒`];
        const chunks = Math.max(0, Number(normalized.chunk_count || 0));
        if (chunks) parts.push(`已收 ${chunks} 分片`);
        return parts.join(" · ");
    }
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
    const normalizeRoundActiveToolKeys = (input) => (input && typeof input === "object" && !Array.isArray(input)
        ? Object.fromEntries(Object.entries(input)
            .map(([roundKey, toolKey]) => [String(roundKey || "").trim(), String(toolKey || "").trim()])
            .filter(([roundKey, toolKey]) => roundKey && toolKey))
        : {});
    const normalizeTraceItems = (items) => (Array.isArray(items)
        ? items.map((item, index) => ({
            index: Number.isInteger(item?.index) && item.index >= 0 ? item.index : index,
            key: String(item?.key || "").trim(),
            title: String(item?.title || "").trim(),
            open: !!item?.open,
            activeToolKey: String(item?.activeToolKey || "").trim(),
            // 一个分组卡里可以有多轮，每轮各自选中一个工具面板。这份按轮账本是
            // 唯一能把选中态放回正确那一轮的依据：只留 activeToolKey 时还原器会把它
            // 兜到第一轮上，于是用户展开的工具面板在刷新后换轮、等于被折掉。
            roundActiveToolKeys: normalizeRoundActiveToolKeys(item?.roundActiveToolKeys),
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
            const hasRoundActiveToolKeys = !!roundActiveToolKeys && Object.keys(roundActiveToolKeys).length > 0;
            roundHosts.forEach((roundHost, roundIndex) => {
                if (!(roundHost instanceof HTMLElement)) return;
                const roundKey = String(roundHost.dataset.roundKey || "").trim();
                const persistedToolKey = roundKey && roundActiveToolKeys
                    ? String(roundActiveToolKeys[roundKey] || "").trim()
                    : "";
                // 只有拿不到按轮账本（旧快照）时才退回"把这个卡里第一个选中项兜到第一轮"；
                // 有账本时再把同一个 key 抹到第一轮，等于替别的轮开面板。
                const fallbackToolKey = !hasRoundActiveToolKeys && roundIndex === 0 ? activeToolKey : "";
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

/* 会话切换后的一次性 unread 豁免:
   列表通道(ceo.sessions.patch/snapshot、REST)的 message_count 更新滞后于聊天通道渲染,
   且切换瞬间 closeCeoWs 会丢掉在途 patch;切走之后迟到的计数补算会把用户已经看过的
   旧消息误判为原会话的 unread。用户发起的切换(切换/新建会话)时为被离开的会话武装一条
   窗口期豁免(多槽、互不覆盖):窗口期内其第一个正增量视为已读(只抬 baseline 不计 unread),
   消费即失效;窗口过期自动作废,不误吞真新消息。 */
const CEO_SESSION_UNREAD_EXEMPT_WINDOW_MS = 10000;

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
    const pageSize = isSkill ? ResourcesState.skillPageSize : ResourcesState.toolPageSize;

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
    ResourcesState.skillPage = 1;
    renderSkills();
}

function resetToolPagination() {
    ResourcesState.toolPage = 1;
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
    const meta = paginateResources(filterSkills(), page, ResourcesState.skillPageSize);
    ResourcesState.skillPage = meta.currentPage;
    renderSkills();
    U.skillList?.scrollTo?.({ top: 0, behavior: "auto" });
}

function setToolPage(page) {
    const meta = paginateResources(filterTools(), page, ResourcesState.toolPageSize);
    ResourcesState.toolPage = meta.currentPage;
    renderTools();
    U.toolList?.scrollTo?.({ top: 0, behavior: "auto" });
}

function setSkillPageSize(value) {
    ResourcesState.skillPageSize = normalizeResourcePageSize(value, ResourcesState.skillPageSize);
    ResourcesState.skillPage = 1;
    renderSkills();
    U.skillList?.scrollTo?.({ top: 0, behavior: "auto" });
}

function setToolPageSize(value) {
    ResourcesState.toolPageSize = normalizeResourcePageSize(value, ResourcesState.toolPageSize);
    ResourcesState.toolPage = 1;
    renderTools();
    U.toolList?.scrollTo?.({ top: 0, behavior: "auto" });
}

function ensureSkillPageForItem(skillId) {
    const targetId = String(skillId || "").trim();
    if (!targetId) return;
    const items = filterSkills();
    const index = items.findIndex((item) => item.skill_id === targetId);
    if (index < 0) return;
    ResourcesState.skillPage = Math.floor(index / ResourcesState.skillPageSize) + 1;
}

function ensureToolPageForItem(toolId) {
    const targetId = String(toolId || "").trim();
    if (!targetId) return;
    const items = filterTools();
    const index = items.findIndex((item) => item.tool_id === targetId);
    if (index < 0) return;
    ResourcesState.toolPage = Math.floor(index / ResourcesState.toolPageSize) + 1;
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

function canSelectCeoBulkSessions() {
    return !(S.ceoPauseBusy || S.ceoUploadBusy || S.ceoSessionCatalogBusy);
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
                `<a class="msg-inline-image-link" href="${clickHref}" data-media-open="image" data-media-name="${label}">${imgTag}</a>`
            );
        });
        text = text.replace(/\[([^\]]+)\]\(([^)\s]+(?:\s+"[^"]*")?)\)/g, (_match, label, target) => {
            const href = String(target || "").replace(/\s+"[^"]*"$/, "");
            if (/^\/api\/ceo\/media\/original\?/.test(href)) {
                const chipLabel = esc(String(label || "file").trim() || "file");
                return createMarkdownToken(
                    tokens,
                    `<a class="msg-file-chip" href="${esc(href)}" data-media-open="auto" data-media-name="${chipLabel}">` +
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

const MD_ORDERED_ITEM = /^\s*\d+\.\s+(.*)$/;
const MD_BULLET_ITEM = /^\s*[-*+]\s+(.*)$/;

// 列表项之间的那段间隙。continues=true 表示它以另一个同类列表项收尾，间隙里的段落
// 就是上一条的正文；撞到别的块（标题、引用、表格、另一类列表）或文本结尾时
// continues=false，段落是否仍算正文由调用方按「这条列表有没有正文形状」决定。
function _collectMarkdownListGap(lines, from, itemPattern) {
    const paragraphs = [];
    let current = [];
    let index = from;
    const flush = () => {
        if (current.length) {
            paragraphs.push(current.join("\n"));
            current = [];
        }
    };
    while (index < lines.length) {
        const line = String(lines[index] || "");
        if (itemPattern.test(line)) {
            flush();
            return { paragraphs, nextIndex: index, continues: true };
        }
        if (!line.trim()) {
            flush();
            index += 1;
            continue;
        }
        if (isMarkdownBlockStart(lines, index)) {
            flush();
            return { paragraphs, nextIndex: index, continues: false };
        }
        current.push(line.trimEnd());
        index += 1;
    }
    flush();
    return { paragraphs, nextIndex: index, continues: false };
}

// 一个连续列表区只出一个 <ol>/<ul>：模型爱用空行把每一项隔开，逐行匹配会把列表切成
// N 个单元素 <ol>，浏览器于是把每一项都编号成 1。
function _collectMarkdownListItems(lines, from, itemPattern) {
    const items = [];
    let index = from;
    while (index < lines.length) {
        const match = String(lines[index] || "").match(itemPattern);
        if (match) {
            items.push({ text: match[1], bodies: [] });
            index += 1;
            continue;
        }
        const gap = _collectMarkdownListGap(lines, index, itemPattern);
        // 列表末尾的段落只有在前面已经出现过「条目 + 正文」形状时才跟着条目走，
        // 否则它是列表之后的一段独立文字。
        const ownsTrailingParagraph = gap.paragraphs.length && items.some((item) => item.bodies.length);
        if (!gap.continues && !ownsTrailingParagraph) break;
        items[items.length - 1].bodies.push(...gap.paragraphs);
        index = gap.nextIndex;
        if (!gap.continues) break;
    }
    return { items, nextIndex: index };
}

function _markdownListBlock(tag, items) {
    const markup = items.map((item) => {
        const bodies = item.bodies
            .map((paragraph) => `<p>${renderInlineMarkdown(paragraph).replace(/\n/g, "<br>")}</p>`)
            .join("");
        return `<li>${renderInlineMarkdown(item.text)}${bodies}</li>`;
    }).join("");
    return `<${tag}>${markup}</${tag}>`;
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

        if (MD_ORDERED_ITEM.test(line)) {
            const collected = _collectMarkdownListItems(lines, index, MD_ORDERED_ITEM);
            blocks.push(_markdownListBlock("ol", collected.items));
            index = collected.nextIndex;
            continue;
        }

        if (MD_BULLET_ITEM.test(line)) {
            const collected = _collectMarkdownListItems(lines, index, MD_BULLET_ITEM);
            blocks.push(_markdownListBlock("ul", collected.items));
            index = collected.nextIndex;
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
    const audioCount = uploads.filter((item) => String(item.kind || "") === "audio").length;
    const fileCount = uploads.length - imageCount - audioCount;
    const parts = [];
    if (audioCount) parts.push(`${audioCount} 段语音`);
    if (imageCount) parts.push(`${imageCount} 张图片`);
    if (fileCount) parts.push(`${fileCount} 个文件`);
    return parts.length ? `已附加 ${parts.join("，")}` : "已附加附件";
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
            <a class="chat-attachment-link chat-attachment-image" role="listitem" href="${safeHref}" data-media-open="image" data-media-name="${label}">
                <img class="chat-attachment-thumb" src="${safeHref}" alt="${label}" loading="lazy">
                <span class="chat-attachment-image-meta">
                    <span class="chat-attachment-name">${label}</span>
                    <span class="chat-attachment-size">${size}</span>
                </span>
            </a>
        `;
    }
    return `
        <a class="chat-attachment-link chat-attachment-file" role="listitem" href="${safeHref}" data-media-open="auto" data-media-name="${label}">
            <span class="chat-attachment-kind">文件</span>
            <span class="chat-attachment-name">${label}</span>
            <span class="chat-attachment-size">${size}</span>
        </a>
    `;
}

function renderStructuredChatAttachments(items = [], { sessionId = activeSessionId() } = {}) {
    const uploads = normalizeUploadList(items).filter((item) => String(item.kind || "") !== "audio");
    if (!uploads.length) return "";
    return `
        <div class="chat-attachment-stack" role="list">
            ${uploads.map((item) => renderStructuredChatAttachmentCard(item, { sessionId })).join("")}
        </div>
    `;
}

function pickVoiceClip(items = []) {
    return normalizeUploadList(items).find((item) => String(item.kind || "") === "audio") || null;
}

// 语音转写的标记必须留在持久文本里给模型看（前门那条规则靠它触发），但不画在气泡上：
// 气泡改成一枚语音条，识别文字收到「转文字」后面。
function stripVoiceTranscriptMarkers(text) {
    const kept = [];
    let hasVoice = false;
    for (const line of String(text || "").split("\n")) {
        const lead = line.slice(0, line.length - line.trimStart().length);
        const body = line.trimStart();
        if (body.startsWith(VOICE_AUTO_SEND_PREFIX)) {
            hasVoice = true;
            const rest = body.slice(VOICE_AUTO_SEND_PREFIX.length).trim();
            if (rest) kept.push(`${lead}${rest}`);
            continue;
        }
        kept.push(line);
    }
    return { text: kept.join("\n").trim(), hasVoice };
}

function buildCeoVoiceBubbleMarkup(clip, transcript) {
    const src = String(clip.url || ceoAttachmentHref(clip) || "");
    const seconds = formatVoiceDuration("");
    return `
        <div class="msg-voice-bubble" role="group" aria-label="语音消息">
            <button type="button" class="msg-voice-play" data-ceo-voice-play aria-label="播放语音" aria-pressed="false">
                <i data-lucide="play"></i>
            </button>
            <span class="msg-voice-duration" data-ceo-voice-duration>${esc(seconds)}</span>
            ${transcript ? `<button type="button" class="msg-voice-transcript-toggle" data-ceo-voice-toggle aria-expanded="false" aria-label="转文字" title="转文字">T</button>` : ""}
            <audio class="msg-voice-audio" src="${esc(src)}" preload="metadata" data-ceo-voice-audio></audio>
        </div>
        ${transcript ? `<div class="msg-voice-transcript" data-ceo-voice-transcript hidden>${esc(transcript)}</div>` : ""}
    `;
}

function formatVoiceDuration(seconds) {
    const total = Number(seconds);
    if (!Number.isFinite(total) || total <= 0) return "--";
    const whole = Math.round(total);
    return `${Math.floor(whole / 60)}:${String(whole % 60).padStart(2, "0")}`;
}

function wireCeoVoiceBubble(host) {
    host.querySelectorAll?.("audio[data-ceo-voice-audio]").forEach((audio) => {
        if (audio.dataset.ceoVoiceWired === "1") return;
        audio.dataset.ceoVoiceWired = "1";
        const bubble = audio.closest?.(".msg-voice-bubble");
        const label = bubble?.querySelector?.("[data-ceo-voice-duration]");
        const button = bubble?.querySelector?.("[data-ceo-voice-play]");
        const setIcon = (name) => {
            if (!button) return;
            button.innerHTML = `<i data-lucide="${name}"></i>`;
            icons();
        };
        // 秒数以浏览器解出的真实时长为准：识别链路给的秒数与解码长度可能差零点几秒，
        // 回放气泡上写一个和音频对不上的数字比不写更糟。
        audio.addEventListener("loadedmetadata", () => {
            if (label) label.textContent = formatVoiceDuration(audio.duration);
        });
        audio.addEventListener("play", () => {
            if (button) button.setAttribute("aria-pressed", "true");
            setIcon("pause");
        });
        audio.addEventListener("pause", () => {
            if (button) button.setAttribute("aria-pressed", "false");
            setIcon("play");
        });
        audio.addEventListener("ended", () => {
            if (button) button.setAttribute("aria-pressed", "false");
            setIcon("play");
        });
        audio.addEventListener("error", () => {
            if (label) label.textContent = "无法播放";
        });
    });
}

function handleCeoVoiceBubbleClick(event) {
    const play = event.target?.closest?.("[data-ceo-voice-play]");
    if (play) {
        const audio = play.parentElement?.querySelector?.("audio[data-ceo-voice-audio]");
        if (!audio) return;
        if (audio.paused) {
            void audio.play();
        } else {
            audio.pause();
        }
        return;
    }
    const toggle = event.target?.closest?.("[data-ceo-voice-toggle]");
    if (!toggle) return;
    // T 在语音条里面，转写块在语音条下面，两者只在消息栈这一层相遇；
    // 一条消息只画一颗语音气泡，所以栈里那一个转写块就是它自己的。
    const panel = toggle.closest?.(".message-stack")?.querySelector?.("[data-ceo-voice-transcript]");
    if (!panel) return;
    panel.hidden = !panel.hidden;
    toggle.setAttribute("aria-expanded", panel.hidden ? "false" : "true");
}

function addCeoUserMessage(text = "", { attachments = [], scrollMode = "preserve", sessionId = activeSessionId(), timestamp = "", turnId = "", canEditFork = false } = {}) {
    return addMsg(String(text || ""), "user", { attachments, scrollMode, sessionId, timestamp, turnId, canEditFork });
}

// 气泡复制按钮:与元信息同行,点击这条消息才显形,按下后复制气泡里已渲染的可见文本。
// 轨道回合与 addMsg 气泡共用同一份标记,位置由 CSS 按角色分角。
function buildCeoBubbleCopyMarkup(label = "复制内容") {
    const safeLabel = esc(label);
    return `<button type="button" class="msg-copy-btn" data-ceo-copy="1" title="${safeLabel}" aria-label="${safeLabel}"><i data-lucide="copy"></i></button>`;
}

function buildCeoUserMessageEditMarkup({ turnId = "", canEditFork = false, sessionId = "" } = {}) {
    // 用户气泡的编辑重发：不单独成行，落在元信息行最右（时间在它左边），常驻显示。
    // can_edit_fork 要求会话没有活的执行体（它要改源转录），另有 web: 前缀 + 非只读
    // + feed 级 .ceo-turn-active 隐藏按钮三重防御。Fork 不在这里：那颗在回复行。
    const key = String(turnId || "").trim();
    if (!key || canEditFork !== true) return "";
    if (!String(sessionId || "").trim().startsWith("web:")) return "";
    if (typeof activeSessionIsReadonly === "function" && activeSessionIsReadonly()) return "";
    const safeTurn = esc(key);
    return `<button type="button" class="msg-action-btn msg-action-edit" data-ceo-edit-resend="${safeTurn}" title="编辑重发：发送后该消息及其后所有内容将被清空" aria-label="编辑重发"><i data-lucide="pencil"></i><span>编辑</span></button>`;
}

function buildCeoAssistantForkMarkup({ turnId = "", canForkReply = false, sessionId = "" } = {}) {
    // 模型回复的 Fork 按钮：与 token 用量同行，落在用量文字左边（它自己常驻，
    // 同行那些信息要点开消息才显示）。服务端只有一个切点——这条回复之后：复制
    // "这条回复及之前"的内容，尾部回复同样有按钮，不必等下一条提问发出来。
    const key = String(turnId || "").trim();
    if (canForkReply !== true || !key) return "";
    if (!String(sessionId || "").trim().startsWith("web:")) return "";
    if (typeof activeSessionIsReadonly === "function" && activeSessionIsReadonly()) return "";
    const safeTurn = esc(key);
    return `<button type="button" class="msg-action-btn msg-action-fork" data-ceo-fork="${safeTurn}" title="Fork：把这条回复及之前的内容复制成新会话，从它后面继续问" aria-label="Fork 会话"><i data-lucide="git-fork"></i><span>Fork</span></button>`;
}

function renderCeoAssistantForkAction(turn, { turnId = "", canForkReply = false, sessionId = "" } = {}) {
    // 轨道回合的元信息行是静态装配的（createPendingCeoTurn），这里按权威 flag 把
    // Fork 插到 .ceo-turn-usage 左边（或摘掉）；行本身常驻，用量文字的点开显隐在 CSS。
    const metaEl = turn?.el?.querySelector?.(".ceo-turn-meta");
    if (!metaEl || typeof metaEl.querySelector !== "function") return;
    const existing = metaEl.querySelector(".msg-action-fork");
    if (existing?.remove) existing.remove();
    const markup = buildCeoAssistantForkMarkup({ turnId, canForkReply, sessionId });
    if (!markup) return;
    const holder = document.createElement("div");
    holder.innerHTML = markup;
    const buttonEl = (holder.children && holder.children[0]) || null;
    if (!buttonEl) return;
    const usageEl = metaEl.querySelector(".ceo-turn-usage");
    if (usageEl && typeof metaEl.insertBefore === "function") metaEl.insertBefore(buttonEl, usageEl);
    else if (typeof metaEl.appendChild === "function") metaEl.appendChild(buttonEl);
}

function syncCeoInputHeight() {
    if (!U.ceoInput) return;
    // 拖出来的高度是用户的显式意图：不再按内容重排，内容超出时靠 overflow-y 滚动。
    if (S.ceoInputManualHeight) return;
    U.ceoInput.style.height = "auto";
    U.ceoInput.style.height = `${Math.min(U.ceoInput.scrollHeight, 200)}px`;
}

function beginCeoInputResize(event) {
    if (!U.ceoInput || event.button !== 0) return;
    event.preventDefault();
    const handle = event.currentTarget;
    const startY = event.clientY;
    const startHeight = U.ceoInput.getBoundingClientRect().height;
    const floor = Math.round(parseFloat(getComputedStyle(U.ceoInput).minHeight)) || 0;
    // 聊天列高度是唯一合理的上限：输入框最多吃掉六成，消息流不能被顶没。
    const wrapper = U.ceoInput.closest(".chat-wrapper");
    const ceiling = Math.floor((wrapper?.clientHeight || 0) * 0.6);
    handle.classList.add("is-resizing");
    const onMove = (moveEvent) => {
        const desired = startHeight + (startY - moveEvent.clientY);
        const height = Math.round(Math.min(Math.max(desired, floor), ceiling));
        S.ceoInputManualHeight = height;
        // 输入框带 CSS max-height，不抬起来刚写进去的高度就会被裁回原样。
        U.ceoInput.style.maxHeight = "none";
        U.ceoInput.style.height = `${height}px`;
    };
    const stop = () => {
        window.removeEventListener("pointermove", onMove);
        window.removeEventListener("pointerup", stop);
        handle.classList.remove("is-resizing");
    };
    // 把手只有 8px 高，鼠标一出发就会离开它；监听挂在 window 上才接得住后续事件。
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", stop);
}

// 输入区悬浮后不占布局，消息流得按它的实测高度留底部空间，否则最后几条永远压在
// 框底下。拖高、排队条、附件条都会改变这个高度，所以观察尺寸而不是在每条路径上补写。
// 队列每帧重画，展开态因此只能存在这里；DOM 上的开合下一次 render 就没了。
const ceoFollowUpExpandedIds = new Set();
// 实测"这一条一行放得下"的条目：放得下就没有可展开的东西，那颗三角也不该占位。
const ceoFollowUpSingleLineIds = new Set();
// 已被某个回合接走的补充消息 turn_id。镜像那一半只由 ceo.state 整表替换来摘，而"接走"
// 这一刻不在那条广播里；不记这份账，一条迟到的旧 state 帧会把已经画成气泡的补充消息
// 重新当"待发送"画一遍（实测双画 5.8 秒）。
const ceoTakenFollowUpTurnIds = new Map();

// 溢出要量出来，CSS 判不出"这一行放得不下"。宽度为 0（面板没显示、还没排版）时
// 不下结论：宁可留着那颗三角，也不能把唯一的展开入口藏掉。

// 输入区可拖宽窄、侧栏可折叠，放得下与否因此会变；观察队列容器而不是在每条改宽路径上补写。
// 服务端队列条目的 id 由 normalizeCeoServerQueuedFollowUpList 生成：server:<turn_id>。
// 一个判据、两处落点（在飞帧与收尾帧）：帧里带来的用户消息 turn_id 与镜像条目同号，
// 就说明这条补充已经被某个回合接走——候选条从此不再画它。
// 编辑与删除走同一条退场：服务端队列里的条目必须先删那边（否则下一帧原样画回来），
// 两者的差别只在正文还不还给输入框。
// 暂停入口的可见性判据：这条会话此刻是否还「不在稳定态」。
// 只认权威事实，不认某一次帧有没有带上下文——S.ceoTurnActive 是逐帧被赋值的一个
// 布尔量，心跳帧、内部轮收尾、缓存重建都会把它抹成 false，于是暂停按钮在回合仍在
// 跑的时候一闪一闪（按不下去）。反过来，报错停下的会话没有任何东西会把那个布尔量
// 置回 false，按钮就永久挂在屏幕上。所以这里每次现算：服务端状态、在飞快照、目录里
// 的运行标记、压缩在途、待审批中断，任一为真就是非稳定态。
function activeCeoSessionIsNonStable() {
    const sessionId = activeSessionId();
    if (!sessionId) return false;
    // 目录里的 is_running 是服务端会话目录的读数（含心跳内部轮），但它会滞后：
    // 只当作「在跑」的正向证据，不当否定证据。
    if (activeSessionItem()?.is_running === true) return true;
    const entry = getCeoSessionSnapshotCache(sessionId);
    const inflight = normalizeCeoSnapshotInflight(entry?.inflight_turn);
    const inflightStatus = String(inflight?.status || "").trim().toLowerCase();
    if (inflightStatus === "running" || inflightStatus === "in_progress" || inflightStatus === "active") return true;
    // 等审批是「回合没结束」的另一种形态：`is_running` 已经是 false，但这一轮还没走完，
    // 必须留着暂停入口（此时按下去走的是中断/放弃那条路）。
    if (ceoApprovalInterruptsFromSnapshotEntry(entry).length) return true;
    if (typeof ceoSessionHasActiveApprovalBlockingState === "function"
        && ceoSessionHasActiveApprovalBlockingState(sessionId)) return true;
    // 压缩在途同样是「没停稳」：回合外的压缩 is_running 为假，但那段时间里新消息
    // 会被挡住，用户需要一个可见的暂停入口。
    if (!!activeCeoSessionCompressionState()) return true;
    // 本地已经建了回合元素、还在等第一段内容：这一条是上面那些读数全都滞后时的兜底
    // （刚发出消息、服务端还没回第一帧）。只认仍挂着加载态的回合——已经流式出正文的
    // 回合有服务端帧撑着，而一个「未定稿但早已有内容」的回合如果永远等不到 final
    // （内部轮），拿它当判据会让暂停按钮重新变成永久挂着。
    return S.ceoPendingTurns.some((turn) => {
        if (!turn || turn.finalized) return false;
        return !!turn.textEl?.classList?.contains?.("assistant-text-loading");
    });
}

// 暂停入口能不能按：本地回合未收尾（含派发闸门认的"还有未了结的活"）或权威读数说
// 会话没停稳，两者取并——任一为真都必须能按下去。这里不写回 S.ceoTurnActive：那个
// 布尔量还兼着"草稿入队还是直发"的判据，见 applyCeoState 的注释。
function ceoSessionPauseAvailable() {
    return !!S.ceoTurnActive || activeCeoSessionIsNonStable();
}

function syncCeoPrimaryButton() {
    syncCeoFeedTurnActiveClass();
    syncCeoAttachButton();
    if (!U.ceoSend) return;
    const pauseAvailable = ceoSessionPauseAvailable();
    if (!activeSessionCanMessage()) {
        if (pauseAvailable) {
            // 归档渠道会话禁止发送，但运行中的回合必须能暂停：只读早期返回
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
    const isPause = pauseAvailable && !hasComposerPayload;
    const label = S.ceoPauseBusy ? "暂停中" : isPause ? "暂停" : "发送";
    const icon = isPause ? "pause" : "send";
    U.ceoSend.innerHTML = `<i data-lucide="${icon}"></i> ${label}`;
    U.ceoSend.disabled = (
        !!S.ceoUploadBusy
        || !!S.ceoPauseBusy
        || !!S.ceoSessionBusy
        || !!S.ceoSessionCatalogBusy
        || !activeSessionId()
        || (!pauseAvailable && !hasComposerPayload)
    );
    U.ceoSend.setAttribute("aria-label", isPause ? "暂停当前 Leader 会话" : "发送消息");
    icons();
}

function finalizePausedCeoTurn(text = "已暂停", { source = null, landTranscriptRows = false, turnId = "" } = {}) {
    const hasExplicitSource = source !== null && source !== undefined && String(source || "").trim();
    const normalizedSource = hasExplicitSource ? normalizeCeoTurnSource(source) : null;
    const normalizedTurnId = normalizeCeoTurnId(turnId);
    const turn = pullActiveCeoTurn(normalizedSource, normalizedTurnId);
    // live 元素可能已经不在（心跳内部轮、或这一跳在别处被摘掉），但那不代表这一回合
    // 没发生过。收尾的两半——气泡就地定稿、缓存补转录行——必须各自独立：早退一次，
    // 随后补发的门槛帧会按缓存整页重建，把这一回合的用户行、回复行和分组卡一起抹掉，
    // 用户只能刷新才重新看见。
    if (turn?.textEl && turn?.flowEl) {
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
    }
    const sessionId = activeSessionId();
    const landedRows = patchCeoSessionSnapshotCache(sessionId, (entry) => {
        const inflightTurn = normalizeCeoSnapshotInflight(entry?.inflight_turn);
        if (!inflightTurn) return entry || {};
        const inflightSource = String(inflightTurn?.source || "").trim().toLowerCase();
        if (normalizedSource && inflightSource && normalizeCeoTurnSource(inflightSource) !== normalizedSource) {
            return entry || {};
        }
        if (!landTranscriptRows) {
            return {
                ...(entry || {}),
                inflight_turn: {
                    ...inflightTurn,
                    status: "paused",
                },
            };
        }
        // 手动暂停没有 ceo.reply.final，缓存里因此一行都没有，而补发的门槛帧只能给
        // 转录行打 flag ⇒ 刚暂停那条的编辑/Fork 按钮永远等不到，只能刷新。服务端这一轮
        // 已归档成转录行并清掉 inflight sidecar，这里按 finalizeCeoTurn 同一套构造补齐，
        // 让缓存形状与刷新后拿到的那份一致。
        // lane 的 user_message 不带 turn_id（normalizeCeoSnapshotInflight 只留 content/
        // timestamp），而门槛帧按 turn_id 匹配，所以行上的 turn_id 由本帧的回合 id 补上。
        const laneTurnId = normalizedTurnId || String(inflightTurn?.turn_id || "").trim();
        const laneUserMessages = normalizeCeoSnapshotUserMessages(
            inflightTurn?.user_messages,
            inflightTurn?.user_message
        ).map((item) => (item.turn_id || !laneTurnId ? item : { ...item, turn_id: laneTurnId }));
        // 回合轨道优先取 live 回合自己渲染出的那份；live 元素已经不在时回退到在飞快照
        // 携带的 turns。少了这一步，暂停后重建出来的回复行没有轨道，工具组整段消失。
        const pausedTurns = (Array.isArray(turn?.turns) && turn.turns.length)
            ? turn.turns
            : (Array.isArray(inflightTurn?.turns) ? inflightTurn.turns : []);
        const payload = buildFinalizedCeoTurnPayload(sessionId, {
            normalizedSource: normalizedSource || inflightSource,
            normalizedTurnId: laneTurnId,
            finalUserMessages: laneUserMessages,
            finalTurns: pausedTurns,
            text,
            meta: turn?.usage ? { usage: turn.usage } : {},
            completedAt: new Date().toISOString(),
            status: "paused",
        });
        return {
            ...(entry || {}),
            messages: payload.messages,
            inflight_turn: payload.inflight_turn,
        };
    });
    return !!turn?.textEl || !!landedRows;
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

function applyCeoState(state = {}, meta = {}) {
    const status = String(state?.status || "").trim().toLowerCase();
    const source = String(meta?.source || state?.source || "").trim().toLowerCase();
    const turnId = String(meta?.turn_id || state?.turn_id || "").trim();
    const running = !!state?.is_running || status === "running";
    const paused = !!state?.paused || status === "paused";
    adoptCeoContextCompression(activeSessionId(), state?.compression);
    // 自动压缩不经过 adopt（没有手动任务），但每次 state 帧报告 running 时都要刷新
    // 缓存新鲜度，否则长压缩会被 TTL 误判成幽灵线。
    const stateCompression = normalizeCeoSnapshotCompression(state?.compression);
    if (String(stateCompression?.status || "").trim().toLowerCase() === "running") {
        mirrorCeoContextCompression(activeSessionId(), stateCompression);
    }
    // 候选条以 runtime 的队列为真相：换标签页/重启后 sessionStorage 里没有的东西，
    // 也要在输入框上方看得见（它已经落在转录里，只是还没成回合）。
    if (adoptCeoServerQueuedFollowUpsFromState(state)) renderQueuedCeoFollowUps(activeSessionId());
    const activeTurn = source || turnId ? getActiveCeoTurn(source, turnId) : getActiveCeoTurn();
    const hadTurnContext = !!activeTurn || !!S.ceoTurnActive;
    // 一帧 status 不为 running 不足以证明"这条会话没有回合在跑"：两个可见轮之间、
    // 服务端刚收下补充还没把 is_running 置起来时都会发这种帧，而本地那条还没等到 reply.final。
    // 这个标记是"草稿入待发送队列还是直接发出去"的唯一判据（sendCeoMessage 与
    // maybeDispatchQueuedCeoFollowUps 都只看它），它假一次，草稿就整批上线一次。
    // 所以只在帧里还留着未了结的活时维持 true；全空就是服务端自己说没事了，照常放行派发。
    const outstandingWork = (Array.isArray(state?.pending_tool_calls) ? state.pending_tool_calls.length : 0)
        + (Array.isArray(state?.pending_interrupts) ? state.pending_interrupts.length : 0)
        + (Array.isArray(state?.queued_follow_up_messages) ? state.queued_follow_up_messages.length : 0)
        + (String(state?.compression?.status || "").trim().toLowerCase() === "running" ? 1 : 0);
    S.ceoTurnActive = running || (!!activeTurn && activeTurn.finalized !== true && outstandingWork > 0);
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
    finalizePausedCeoTurn("已暂停", { source, turnId, landTranscriptRows: true });
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
            text: String(payload?.message || "上下文大小超出当前模型，按这颗模型的窗口压缩后仍装不下"),
            kind: "error",
        });
    }
    finalizeCeoTurn(`运行出错：${String(payload?.message || "unknown error")}`, payload || {});
}

function requestCeoPause() {
    if (S.ceoPauseBusy) return;
    if (!ceoSessionPauseAvailable()) {
        syncCeoPrimaryButton();
        return;
    }
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

function handleCeoPrimaryAction() {
    const text = String(U.ceoInput?.value || "");
    const uploads = normalizeUploadList(S.ceoUploads);
    // 主按钮的两种语义由「会话是否还在跑」决定，而那个判据要现算：只看缓存的
    // S.ceoTurnActive，会在它被某一帧抹掉的瞬间把暂停点成一次空发送。
    if (ceoSessionPauseAvailable() && !text.trim() && !uploads.length) {
        requestCeoPause();
        return;
    }
    if (!activeSessionCanMessage()) {
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

// ===== 语音输入（本机 whisper.cpp 转写） =====
// 采样率固定 16k：whisper 的工作率，也是 60 秒音频能压进 2MiB 上传上限的原因
// （16k/单声道/16bit ≈ 32KB/s）。重采样在浏览器里用 OfflineAudioContext 做完，
// 后端因此不需要 ffmpeg 也能吃下网页录音。
const VOICE_SAMPLE_RATE = 16000;
const VOICE_MAX_MS = 60000;

function encodePcmWav(samples, sampleRate) {
    const buffer = new ArrayBuffer(44 + samples.length * 2);
    const view = new DataView(buffer);
    const writeText = (offset, text) => {
        for (let i = 0; i < text.length; i += 1) view.setUint8(offset + i, text.charCodeAt(i));
    };
    writeText(0, "RIFF");
    view.setUint32(4, 36 + samples.length * 2, true);
    writeText(8, "WAVE");
    writeText(12, "fmt ");
    view.setUint32(16, 16, true);
    view.setUint16(20, 1, true);
    view.setUint16(22, 1, true);
    view.setUint32(24, sampleRate, true);
    view.setUint32(28, sampleRate * 2, true);
    view.setUint16(32, 2, true);
    view.setUint16(34, 16, true);
    writeText(36, "data");
    view.setUint32(40, samples.length * 2, true);
    let offset = 44;
    for (let i = 0; i < samples.length; i += 1) {
        const clamped = Math.max(-1, Math.min(1, samples[i]));
        view.setInt16(offset, clamped < 0 ? clamped * 0x8000 : clamped * 0x7fff, true);
        offset += 2;
    }
    return new Blob([buffer], { type: "audio/wav" });
}

async function audioBlobToWav16k(blob) {
    const raw = await blob.arrayBuffer();
    // OfflineAudioContext 也提供 decodeAudioData，用它就不用真的打开音频输出设备。
    const scratch = new OfflineAudioContext(1, 1, VOICE_SAMPLE_RATE);
    const decoded = await scratch.decodeAudioData(raw);
    const frames = Math.max(1, Math.round(decoded.duration * VOICE_SAMPLE_RATE));
    const resampled = new OfflineAudioContext(1, frames, VOICE_SAMPLE_RATE);
    const source = resampled.createBufferSource();
    source.buffer = decoded;
    source.connect(resampled.destination);
    source.start();
    const rendered = await resampled.startRendering();
    return encodePcmWav(rendered.getChannelData(0), VOICE_SAMPLE_RATE);
}

function voiceCaptureSupported() {
    return Boolean(
        navigator.mediaDevices && navigator.mediaDevices.getUserMedia
        && typeof window.MediaRecorder !== "undefined"
        && typeof window.OfflineAudioContext !== "undefined"
    );
}

const VOICE_PREPARE_POLL_MS = 1200;
// 二进制走 GitHub CDN（实测这类网络 ~20 KB/s，8.2 MB 要几分钟起步），模型走
// hf-mirror（实测 20 MB/s）。30 分钟是给最坏那条通道留的止损，不是预期耗时。
const VOICE_PREPARE_MAX_MS = 30 * 60 * 1000;

function voiceEngineGate(status) {
    if (!status) return "unknown";
    if (status.ready) return "ready";
    // 开关是关的：这是操作员明确关掉的能力，不许替他打开，更不许开始下载。
    if (!status.enabled) return "disabled";
    return "provision";
}

function voicePrepareText(status) {
    const prep = (status && status.prepare) || {};
    const label = prep.stage === "model" ? "语音模型" : "语音程序";
    const total = Number(prep.total_bytes) || 0;
    const done = Number(prep.done_bytes) || 0;
    if (!total) return `正在下载${label}…`;
    const percent = Math.max(0, Math.min(99, Math.floor((done / total) * 100)));
    return `正在下载${label} ${formatVoiceBytes(done)} / ${formatVoiceBytes(total)}（${percent}%）`;
}

function voiceDownloadTotalText(status) {
    const plan = (status && status.download) || {};
    const total = (Number(plan.binary_bytes) || 0) + (Number(plan.model_bytes) || 0);
    return total ? `约 ${formatVoiceBytes(total)}，` : "";
}

function voiceFailureText(result) {
    const code = String((result && result.error_code) || "");
    if (code === "stt_disabled") return "语音识别未启用：在配置里打开 stt.enabled。";
    if (code === "stt_binary_missing" || code === "stt_model_missing") {
        return "语音识别未就绪：执行 negi stt prepare 下载 whisper.cpp 与模型。";
    }
    if (code === "stt_silent" || code === "stt_empty") return "这段录音几乎是静音，没有可识别的声音。";
    if (code === "stt_busy") return "正在处理另一段音频，请稍候再试。";
    if (code === "stt_too_long") return "录音超过时长上限，请说短一些。";
    if (code === "audio_decoder_missing") return "本机缺少 ffmpeg，无法解码这种音频格式。";
    return String((result && result.error) || "未知原因");
}

// 自动发送模式下用户没有复核机会，所以正文必须带上来源标识——前门提示词里那条
// 「看到该前缀就先复述确认」全靠它触发。手动模式不带：文本会被用户编辑，标着
// "机器识别结果"的那段很可能已经是用户自己改过的了。
const VOICE_AUTO_SEND_PREFIX = "用户语音，机器识别结果：";
const VOICE_AUTO_SEND_STORAGE_KEY = "g3ku.ceoVoiceAutoSend";

// ===== 用户消息编辑重发 / 模型回复 Fork =====
// 两颗按钮各吃一份服务端门槛：用户行的 can_edit_fork（内容判据 + 会话必须没有活的
// 执行体，因为它要改源转录），回复行的 can_fork_reply（只看内容判据，只读前缀）。
// 前端另有 .ceo-turn-active 隐藏编辑按钮与点击守卫。

function syncCeoFeedTurnActiveClass() {
    // 防御型显示:任何回合进行中(含 heartbeat/cron 内部轮)隐藏编辑按钮(Fork 不受影响)。
    if (!U.ceoFeed || !U.ceoFeed.classList) return;
    U.ceoFeed.classList.toggle("ceo-turn-active", !!S.ceoTurnActive);
    // 同一状态位驱动「回到最新」的呼吸:收尾后按钮只是常显,没有新内容要追就不该继续闪。
    U.ceoScrollToLatestBtn?.classList?.toggle("is-breathing", !!S.ceoTurnActive);
}

function applyCeoEditForkGates(payload = {}, sessionId = "") {
    // 回合收尾后服务端补发的权威门槛：把它落到缓存里对应的行上，再走
    // renderCeoSnapshot 的签名重建，编辑/Fork 按钮就不必等手动刷新才出现。
    // 整份替换语义（不在列表里的行一律收 flag）与 snapshot.ceo 一致：新回合会让
    // 上一轮失去"最近 12 轮"窗口，其后新建且仍未跑完的任务会收回整批资格。
    // 两份列表各自吃不同的闸门：turn_ids 额外吃会话稳定态（要改源转录），
    // fork_reply_turn_ids 只看内容判据（只读前缀）。
    const key = String(sessionId || activeSessionId() || "").trim();
    if (!key || key !== String(activeSessionId() || "").trim()) return;
    const entry = getCeoSessionSnapshotCache(key);
    const messages = Array.isArray(entry?.messages) ? entry.messages : [];
    if (!messages.length) return;
    let nextMessages = messages;
    let changed = false;
    [
        { flag: "can_edit_fork", ids: payload?.turn_ids, role: "user" },
        // Fork 资格只落 assistant 行：turn_id 在提问行和它的回复行上同值，
        // 共用一个键会让"这条提问可 Fork"冒充"上面那条回复之后可 Fork"。
        { flag: "can_fork_reply", ids: payload?.fork_reply_turn_ids, role: "assistant" },
    ].forEach(({ flag, ids, role }) => {
        const eligibleTurnIds = new Set((Array.isArray(ids) ? ids : [])
            .map((item) => String(item || "").trim())
            .filter(Boolean));
        const claimedTurnIds = new Set();
        nextMessages = nextMessages.map((item) => {
            if (!item || typeof item !== "object") return item;
            if (String(item.role || "").trim().toLowerCase() !== role) return item;
            const turnId = String(item.turn_id || "").trim();
            // 同一 run 的连续消息共享 turn_id，门槛只属首条行——与按下标编码的服务端一致。
            const allowed = !!turnId && eligibleTurnIds.has(turnId) && !claimedTurnIds.has(turnId);
            if (allowed) claimedTurnIds.add(turnId);
            if (allowed === (item[flag] === true)) return item;
            changed = true;
            const next = { ...item };
            if (allowed) next[flag] = true;
            else delete next[flag];
            return next;
        });
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
    // 现算而不是读 S.ceoTurnActive：那个布尔量是逐帧写的，被心跳帧抹掉时这里会
    // 放行一次必然被服务端 409 拒掉的点击；反过来它卡住时又会挡住已经停稳的会话。
    if (activeCeoSessionIsNonStable()) return "回合进行中（含心跳内部轮），请等待结束或先暂停。";
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
        edit_fork_blocked_by_async_task: "这条消息之后新建的任务还没跑完，编辑或 Fork 会截掉它的执行记录。",
        turn_not_run_first: "同批连续消息只支持在第一条上编辑/Fork。",
        boundary_unavailable: "该消息的上下文边界快照已超出保留窗口（最近 12 轮），无法编辑/Fork。",
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
            text: "该消息当前不可编辑重发（其后创建的任务仍未跑完、非批次首条，或已超出最近 12 轮的可编辑窗口）。",
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
    // Fork 不吃 ceoHistoryEditBusyReason()：那条守卫的语义是"回合在跑/转录缓存可能陈旧"，
    // 而 Fork 是只读前缀复制、源会话零变更，服务端 fork 端点也不查运行态。
    // 只留下 canCreateCeoSessions()（上传/暂停请求/会话目录操作在飞）这一道。
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
        // 成功不再弹 toast：切到新会话 + 输入框里的原文就是回执，
        // 一层浮层只会盖住刚切好的会话列表。失败照旧要说话。
    } catch (e) {
        showToast({ title: "Fork 失败", text: editForkErrorText(e), kind: "error", durationMs: 5200 });
    } finally {
        S.ceoSessionCatalogBusy = false;
        renderCeoSessions();
        syncCeoPrimaryButton();
    }
}

// 首屏窗口化：全量消息可达数百条（渠道会话实测 771 条/4 万节点，整列重建
// 2-7s）；首帧只渲染尾部 N 条，向上滚动或锚点需要时再从内存数组补渲染分片。

// 用户「跟随最新」意图：true=贴底跟随；false=用户正在读历史，任何直播突变都不得
// 再动滚动位置。判定只认用户手势（滚轮/指针/触摸/键盘）引发的滚动——程序写
// scrollTop 触发的 scroll 事件不改变意图，这正是「上翻被反复拽回底部」的根因。
// 程序钉底(含 batch bottom 模式与图片异步 re-pin)派发的 scroll 绝不能重新武装
// 跟随；突变静默期之前的滚动事件一律不改意图位。

const CEO_FEED_SCROLL_UP_KEYS = new Set(["PageUp", "Home", "ArrowUp", "Up"]);

// 这次滚动会不会真的落到会话面：内层滚动容器（工具输出框、组代码框）还能吃掉
// 就轮不到会话面，已经滚到头才继续往外链。

// preserve 模式的滚动快照:贴底标志 + 上滚时的元素锚点 + 像素兜底。
// 旧实现是纯像素 clamp(min(prevTop, maxTop)):直播重建导致内容高度变化(展开的
// 分组被折叠、工具条撑高)时视口会跳变;锚点按「当前可见的 feed 子元素 + 元素内
// 偏移」还原,高度增减都稳定。用户原本贴底时视为跟随最新内容(聊天 stick-to-bottom),
// 不再被新产出的内容留在原地。
function addMsg(text, role, { markdown = false, attachments = [], scrollMode = "preserve", sessionId = activeSessionId(), timestamp = "", usage = null, turnId = "", canEditFork = false, canForkReply = false } = {}) {
    return mutateCeoFeed(() => {
        const el = document.createElement("div");
        el.className = `message ${role}`;
        const voiceClip = role === "user" ? pickVoiceClip(attachments) : null;
        const voiceView = role === "user" ? stripVoiceTranscriptMarkers(text) : { text: String(text || ""), hasVoice: false };
        const displayText = role === "user" ? voiceView.text : String(text || "");
        const contentClass = markdown ? "msg-content markdown-content" : "msg-content";
        const content = markdown ? renderMarkdown(displayText) : esc(displayText);
        const attachmentMarkup = renderStructuredChatAttachments(attachments, { sessionId });
        // 元信息行(发送/完成时间 + token 用量 + 复制按钮):时间仅在调用方提供数据
        // 时渲染,复制按钮只要有正文就跟着出现,两者显隐由 CSS 的 .meta-open 点击门控制。
        // 两颗动作按钮也挂在这一行、不吃点击门：Fork 排在读数左边（回复行），
        // 编辑排在读数右边（用户行）。有 meta 时用 message-stack 纵向包裹,
        // 保证这一行落在气泡下方而不是 flex 行内并排。
        const metaText = buildCeoMessageMetaText({ role, timestamp, usage });
        const copyMarkup = hasRenderableText(displayText) ? buildCeoBubbleCopyMarkup() : "";
        const forkMarkup = role === "user"
            ? ""
            : buildCeoAssistantForkMarkup({ turnId, canForkReply, sessionId });
        const editMarkup = role === "user"
            ? buildCeoUserMessageEditMarkup({ turnId, canEditFork, sessionId })
            : "";
        const metaMarkup = metaText || copyMarkup || forkMarkup || editMarkup
            ? `<div class="msg-meta">${forkMarkup}${metaText ? `<span class="msg-meta-text">${esc(metaText)}</span>` : ""}${copyMarkup}${editMarkup}</div>`
            : "";
        if (voiceClip) {
            el.innerHTML = `<div class="message-stack">${buildCeoVoiceBubbleMarkup(voiceClip, voiceView.text)}${attachmentMarkup}${metaMarkup}</div>`;
        } else if (role === "user" && (attachmentMarkup || metaMarkup)) {
            const textBubble = hasRenderableText(displayText)
                ? `<div class="${contentClass}">${content}</div>`
                : "";
            el.innerHTML = `<div class="message-stack">${textBubble}${attachmentMarkup}${metaMarkup}</div>`;
        } else if (metaMarkup) {
            el.innerHTML = `<div class="message-stack"><div class="${contentClass}">${content}${attachmentMarkup}</div>${metaMarkup}</div>`;
        } else {
            el.innerHTML = `<div class="${contentClass}">${content}${attachmentMarkup}</div>`;
        }
        const stamp = String(timestamp || "").trim();
        if (stamp) el.dataset.ceoTimestamp = stamp;
        ceoFeedAppendHost().appendChild(el);
        wireCeoVoiceBubble(el);
        icons();
        return el;
    }, { scrollMode });
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

function ceoSkillRosterNoticeLabel(items = []) {
    const names = (Array.isArray(items) ? items : [])
        .map((item) => String(item?.skill_id || "").trim())
        .filter(Boolean);
    if (!names.length) return "";
    if (names.length <= 3) return `Skills newly readable in this session: ${names.join(", ")}`;
    return `Skills newly readable in this session: ${names.slice(0, 3).join(", ")} +${names.length - 3} more`;
}

// 会话中途新增可读技能：模型侧那份提示落在历史里（只投一次），这里只是同一件事的
// 人可见内部行——套用户气泡会把系统注入写成用户说过的话，与 provenance 规矩打架。
function handleCeoSkillRosterNotice(payload = {}) {
    const label = ceoSkillRosterNoticeLabel(payload?.items || []);
    if (!label || !U.ceoFeed) return null;
    return mutateCeoFeed(() => {
        const el = document.createElement("div");
        el.className = "message system ceo-internal-ack is-skill-notice";
        const content = document.createElement("div");
        content.className = "msg-content ceo-internal-ack-content";
        content.textContent = label;
        el.appendChild(content);
        ceoFeedAppendHost().appendChild(el);
        icons();
        return el;
    }, { scrollMode: "preserve" });
}

function resetCeoFeed() {
    if (!U.ceoFeed) return;
    U.ceoFeed.innerHTML = "";
    S.ceoPendingTurns = [];
    S.ceoPausedArchiveTurns = {};
}

function ceoInflightTurnHasVisibleAssistantState(snapshot = null) {
    if (!snapshot || typeof snapshot !== "object") return false;
    const status = String(snapshot.status || "").trim().toLowerCase();
    const assistantText = String(snapshot.assistant_text || "").trim();
    const turnId = String(snapshot?.turn_id || "").trim();
    const turns = Array.isArray(snapshot?.turns) ? snapshot.turns : [];
    // 只有思考读数、正文还空的进行中帧也要建行：那一行就是这一跳的全部进度。
    const reasoning = normalizeCeoSnapshotReasoning(snapshot?.reasoning);
    return !!assistantText || !!turns.length || !!reasoning || status === "paused" || status === "error";
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
    if (!turn?.textEl) return;
    // pending 是「这行内容归直播车道」的标记：收尾 markdown、静默正文、已暂停都把它摘掉。
    // 没有这道闸门，旁白全部进轨之后的空 residual 会把那一份权威正文换成转圈。
    if (!turn.textEl.classList?.contains?.("pending")) return;
    // 流式文本一律走气泡车道（纯文本，ceo.reply.final 才换 markdown），不再挂进末位分组卡体。
    const text = ceoLiveStreamResidual(turn, turn.lastExecutionTraceSummary);
    if (!text.trim()) {
        // 旁白都已经进轨：回到等待态。只在状态真的变化时重写 markup——
        // 这一句每帧都会跑到，而 loading markup 里带 <i data-lucide>，重写就牵动全文档图标扫描。
        if (!turn.textEl.classList?.contains?.("assistant-text-loading")) renderCeoAssistantLoadingState(turn);
        return;
    }
    turn.textEl.textContent = text;
    turn.textEl.classList.remove("assistant-text-loading");
    turn.textEl.classList.remove("markdown-content");
    if (typeof syncCeoAssistantLoadingAria === "function") syncCeoAssistantLoadingAria(turn.textEl);
    if (typeof syncCeoTurnLoadingOnlyState === "function") syncCeoTurnLoadingOnlyState(turn, false);
}

function ceoLiveStreamResidual(turn, summary) {
    let residual = String(turn?.liveStreamText || "");
    if (!residual.trim()) return "";
    // 轨道上已渲染的文本（各组的正文）从 live 流里按
    // 出现位置删掉，剩下的才是真正还没进轨的尾巴。子串级删除对「间隙旁白、
    // 空白符差异」都不再断链；按轨道顺序找最早出现位置，
    // 避免 echo 场景删错新的那一段。
    const absorb = (text) => {
        const normalized = String(text || "").trim();
        if (!normalized) return;
        const index = residual.indexOf(normalized);
        if (index >= 0) {
            residual = residual.slice(0, index) + residual.slice(index + normalized.length);
        }
    };
    for (const turnItem of summary?.turns || []) {
        absorb(turnItem?.text);
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

// 静默回合（模型调用 silent 工具）不外发，但正文与回合轨道是留给模型和运维的痕迹。
// 呈现方式：整条响应折成一行「静默消息 HH:MM:SS」+ 下箭头，这一行在气泡**外面**——
// 折叠时气泡整体不出现（不给它套一层壳），点开才露出正文、回合轨道与工具步骤。
// 不单独渲染静默原因：工具静默时正文本身就是 reason（finalize 取 accompanying_text or
// reason），再补一行会重复。折叠行用 DOM 节点 + textContent 装配，本文件没有 HTML
// 转义助手，拼进 innerHTML 等于开一个注入面。
function renderCeoSilentTurn(turn, { text = "", timestamp = "" } = {}) {
    if (!turn?.textEl || !turn?.el) return;
    const normalizedText = String(text || "").trim();
    turn.textEl.hidden = false;
    turn.textEl.classList.remove("pending");
    turn.textEl.classList.remove("assistant-text-loading");
    turn.textEl.innerHTML = normalizedText ? renderMarkdown(normalizedText) : "";
    syncCeoAssistantLoadingAria(turn.textEl);
    syncCeoTurnLoadingOnlyState(turn, false);
    turn.el.classList.add("ceo-silent-message");

    let line = turn.silentLineEl;
    if (!line) {
        line = document.createElement("button");
        line.type = "button";
        line.className = "ceo-silent-line";
        const label = document.createElement("span");
        label.className = "ceo-silent-label";
        const rule = document.createElement("span");
        rule.className = "ceo-silent-rule";
        const caret = document.createElement("span");
        caret.className = "ceo-silent-caret";
        line.appendChild(label);
        line.appendChild(rule);
        line.appendChild(caret);
        line.addEventListener("click", () => {
            const expanded = turn.el.classList.toggle("ceo-silent-expanded");
            line.setAttribute("aria-expanded", expanded ? "true" : "false");
        });
        turn.el.insertBefore(line, turn.el.children[0] || null);
        turn.silentLineEl = line;
    }
    const clock = formatCompactTime(timestamp || turn.completedAt || "");
    line.children[0].textContent = clock ? `静默消息 ${clock}` : "静默消息";
    line.setAttribute("aria-expanded", turn.el.classList.contains("ceo-silent-expanded") ? "true" : "false");
}

// --- 思维链实时行（正文下方一行）--------------------------------------------------
// 为什么需要它：xhigh 档一跳能想 84 秒而界面只有一个转圈（实测 3,037 output 中 2,591
// token 是不可见思考）。这一行折叠时只占一行：耗时 + 思考 token + 最新一行；展开才给
// 固定高度的滚动窗。全文只活在内存里，回合结束即弃，所以历史行不再带展开能力。
function ensureCeoThinkingLineEl(turn) {
    if (turn?.thinkingLineEl?.isConnected) return turn.thinkingLineEl;
    if (!turn?.el) return null;
    const line = document.createElement("div");
    line.className = "ceo-thinking-line";
    line.hidden = true;
    const head = document.createElement("div");
    head.className = "ceo-thinking-line-head";
    const meta = document.createElement("span");
    meta.className = "ceo-thinking-line-meta";
    const tail = document.createElement("span");
    tail.className = "ceo-thinking-line-tail";
    const pane = document.createElement("div");
    pane.className = "ceo-thinking-line-pane";
    pane.setAttribute("role", "log");
    pane.hidden = true;
    pane.addEventListener("scroll", () => {
        // 用户手动上滑就停止自动跟随，滑回底部再恢复：滚动位置归用户，渲染层不抢。
        turn.thinkingFollowTail = pane.scrollHeight - pane.scrollTop - pane.clientHeight <= 4;
    });
    if (turn.thinkingExpandable === false) {
        // 历史行不给展开能力：思考全文从不落盘，那一格只有读数。
        line.classList.add("is-final");
    } else {
        // 整行头就是开关，不设独立按钮：折叠态只有一行，按钮要么占宽要么叠在行尾，
        // 两种都把"一行"撑成两格。aria-expanded 是状态位，也是 CSS 的 cursor 判据。
        head.setAttribute("role", "button");
        head.setAttribute("tabindex", "0");
        head.setAttribute("aria-expanded", "false");
        head.title = "点击展开思维链";
        const toggle = () => {
            const expanded = head.getAttribute("aria-expanded") !== "true";
            head.setAttribute("aria-expanded", expanded ? "true" : "false");
            head.title = expanded ? "点击收起思维链" : "点击展开思维链";
            pane.hidden = !expanded;
            line.classList.toggle("is-expanded", expanded);
            if (expanded) {
                turn.thinkingFollowTail = true;
                pane.scrollTop = pane.scrollHeight;
            }
        };
        head.addEventListener("click", toggle);
        head.addEventListener("keydown", (event) => {
            if (event.key === "Enter" || event.key === " ") {
                event.preventDefault();
                toggle();
            }
        });
    }
    head.appendChild(meta);
    head.appendChild(tail);
    line.appendChild(head);
    line.appendChild(pane);
    const anchor = turn.textEl?.nextElementSibling || null;
    const host = turn.textEl?.parentElement || turn.el;
    host.insertBefore(line, anchor);
    turn.thinkingLineEl = line;
    turn.thinkingHeadEl = head;
    turn.thinkingMetaEl = meta;
    turn.thinkingTailEl = tail;
    turn.thinkingPaneEl = pane;
    turn.thinkingFollowTail = true;
    return line;
}

function renderCeoThinkingLineIntoTurn(turn) {
    if (!turn?.el) return;
    const text = String(turn.reasoningText || "");
    const seconds = Number(turn.reasoningSeconds || 0);
    const chars = Number(turn.reasoningChars || 0);
    const estTokens = Number(turn.reasoningEstTokens || 0);
    if (!text.trim() && !seconds && !chars) {
        if (turn.thinkingLineEl) turn.thinkingLineEl.hidden = true;
        return;
    }
    const line = ensureCeoThinkingLineEl(turn);
    if (!line) return;
    line.hidden = false;
    const parts = [];
    if (seconds > 0) parts.push(`思考 ${formatElapsedDuration(seconds)}`);
    // token 是估算：provider 的 usage 只在流尾给，不少网关连 reasoning_tokens 都不报，
    // 所以一律带 `~`；真值只有回合结束落进转录行那一刻才可能不带。
    const tokens = estTokens || Math.round(chars / 4);
    if (tokens > 0) {
        // provider 真报了思考 token 才去掉 `~`；否则一律标明是估算。
        const reported = Number(turn.reasoningTokensReported || 0);
        parts.push(reported > 0
            ? `${formatTokenCount(reported)} tok`
            : `~${formatTokenCount(tokens)} tok`);
    }
    turn.thinkingMetaEl.textContent = parts.join(" · ");
    const lines = text.replace(/\s+$/, "").split("\n");
    turn.thinkingTailEl.textContent = lines[lines.length - 1] || "";
    turn.thinkingTailEl.title = lines[lines.length - 1] || "";
    if (turn.thinkingPaneEl.textContent !== text) {
        turn.thinkingPaneEl.textContent = text;
        if (turn.thinkingFollowTail && !turn.thinkingPaneEl.hidden) {
            turn.thinkingPaneEl.scrollTop = turn.thinkingPaneEl.scrollHeight;
        }
    }
}

function applyCeoReasoningDelta(payload = {}, { sessionId = activeSessionId() } = {}) {
    const key = String(sessionId || "").trim();
    const turnId = normalizeCeoTurnId(payload?.turn_id || "");
    if (!key || key !== activeSessionId() || !payload || typeof payload !== "object") return false;
    const delta = String(payload.delta || "");
    const attempt = Number(payload.attempt || 0);
    const seq = Number(payload.seq || 0);
    if (!delta) return false;
    const turn = ensureActiveCeoTurn({
        source: String(payload.source || "user").trim().toLowerCase() || "user",
        turnId,
    });
    if (!turn) return false;
    if (Number(turn.reasoningAttempt || 0) !== attempt) {
        // 新的一段（同跳重发或换链降级）：整段重来，绝不与上一发拼接。
        turn.reasoningAttempt = attempt;
        turn.reasoningText = "";
        turn.reasoningLastSeq = 0;
    }
    if (seq && Number(turn.reasoningLastSeq || 0) >= seq) return false;
    turn.reasoningLastSeq = seq;
    turn.reasoningText = `${turn.reasoningText || ""}${delta}`;
    turn.reasoningChars = Number(payload.chars || 0);
    turn.reasoningEstTokens = Number(payload.est_tokens || 0);
    turn.reasoningSeconds = Number(payload.seconds || 0);
    mutateCeoFeed(() => renderCeoThinkingLineIntoTurn(turn), { scrollMode: "preserve" });
    return true;
}

function applyCeoThinkingFromInflightSnapshot(turn, raw = null) {
    // 重连时只有这份有界读数：思考帧是 append-only，错过的那几帧不补，
    // 但耗时/体量/最后一行必须立刻回到界面上。
    if (!turn) return;
    const reasoning = normalizeCeoSnapshotReasoning(raw);
    if (!reasoning) {
        if (turn.thinkingLineEl) turn.thinkingLineEl.hidden = true;
        return;
    }
    const attempt = Number(reasoning.attempt || 0);
    if (Number(turn.reasoningAttempt || 0) !== attempt) {
        turn.reasoningAttempt = attempt;
        turn.reasoningText = reasoning.tail_line || "";
    } else if (!String(turn.reasoningText || "").trim()) {
        turn.reasoningText = reasoning.tail_line || "";
    }
    turn.reasoningSeconds = reasoning.seconds;
    turn.reasoningChars = reasoning.chars;
    turn.reasoningEstTokens = reasoning.est_tokens;
    renderCeoThinkingLineIntoTurn(turn);
}

function renderCeoThinkingSummaryFromSnapshot(hostEl, thinking = null) {
    // 历史行只给读数、不给展开：思考全文从不落盘，能恢复的是"它想了多久、多大体量"。
    if (!(hostEl instanceof HTMLElement) || !thinking || typeof thinking !== "object") return;
    const reported = Number(thinking.tokens || 0);
    const seconds = Number(thinking.seconds || 0);
    const chars = Number(thinking.chars || 0);
    if (!seconds && !chars && !reported) return;
    const textEl = hostEl.querySelector(".assistant-text") || hostEl.querySelector(".msg-content") || hostEl;
    const anchorParent = textEl.parentElement || hostEl;
    const fakeTurn = {
        el: hostEl,
        textEl,
        thinkingExpandable: false,
        reasoningText: " ",
        reasoningSeconds: seconds,
        reasoningChars: chars,
        reasoningEstTokens: Number(thinking.est_tokens || 0),
        reasoningTokensReported: reported,
    };
    renderCeoThinkingLineIntoTurn(fakeTurn);
    if (fakeTurn.thinkingLineEl) {
        if (anchorParent !== hostEl && fakeTurn.thinkingLineEl.parentElement === hostEl) {
            anchorParent.insertBefore(fakeTurn.thinkingLineEl, textEl.nextSibling || null);
        }
    }
}

function clearCeoReplyDeltaBuffer(sessionId = "", { turnId = "" } = {}) {    const key = String(sessionId || "").trim();
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
            const isNewTurnBubble = !getActiveCeoTurn(source, turnId);
            const turn = ensureActiveCeoTurn({ source, turnId });
            if (turn?.textEl) {
                mutateCeoFeed(() => {
                    if (turnId) turn.turnId = turnId;
                    if (source) turn.source = source;
                    turn.liveStreamText = text;
                    // 全文档 lucide 扫描只在真要新建气泡（带未实例化图标）的这一帧付；
                    // 每帧都扫一遍会把这一车道压到出帧节奏以下，表现为整页卡住不能滚。
                    if (isNewTurnBubble) icons();
                    renderCeoLiveStreamTextIntoTurn(turn);
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
    // seq 是会话级单调自增的，但运行时一重启它就从头再数，而页面缓冲里还留着上一个回合
    // 的高 seq。只比 seq 会把重启后新回合的每一帧静默丢掉（回合在跑、气泡一直空）。
    // 换了回合就整块接管，不比序号；同一回合内才用"序号必须递增"防乱序回退。
    const incomingTurnId = normalizeCeoTurnId(payload?.turn_id || payload?.turnId || "");
    const bufferTurnId = normalizeCeoTurnId(current?.turnId || "");
    const turnChanged = !!(incomingTurnId && bufferTurnId && incomingTurnId !== bufferTurnId);
    if (current && !turnChanged && currentSeq > seq) return false;
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
    // 轮次工具条还原键:按所属组的 traceKey 分域(工具位置跨组会重复);
    // round 缺 data-round-key 时用回合内 DOM 顺序兜底(与 feed 级 capture 一致)。
    const stepEl = host && typeof host.closest === "function" ? host.closest(".task-trace-step") : null;
    const scope = String(stepEl?.dataset?.traceKey || "").trim();
    const roundKey = String(host?.dataset?.roundKey
        || (host && typeof host.getAttribute === "function" ? host.getAttribute("data-round-key") : "")
        || "").trim();
    return `${scope}::${roundKey || `idx:${hostIndex}`}`;
}

function captureCeoTurnTraceViewState(turn = null) {
    // 直播重建(renderCeoTurnsIntoTurn 整段 wipe listEl.innerHTML)前捕获回合内
    // 展开态:Interaction Flow 开合、各组 <details> 开合(data-trace-key)、组内工具
    // 条选中(data-active-tool-key)。全是纯 DOM 态,重建即丢,必须在 resetCeoToolFlow
    // 之前捕获。仅对已渲染过轨道的回合生效:换新 turnId 时 patchCeoInflightTurn 会先
    // 清空 lastExecutionTraceSummary,此时不能沿用上一轮的展开态(组 key 跨轮会重复)。
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
    // 回合轨道重建只复活 <details> 开合,输出框自身的滚动位置同样需要带过去:
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
    // 重建后按稳定键还原;直接赋 open 不触发 toggle 事件,组输出的懒加载副作用由
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

// 一个回合被压缩线切成两段时，两段各渲染自己那截：头段 seq <= 边界，续跑段 > 边界。
// 缺 seq 的条目（非前门来源）不参与切分，整段照旧渲染。
function sliceCeoRailTurns(turns, turn) {
    const from = Number.isFinite(turn?.turnWindowFrom) ? turn.turnWindowFrom : null;
    const to = Number.isFinite(turn?.turnWindowTo) ? turn.turnWindowTo : null;
    if (from === null && to === null) return turns;
    return turns.filter((item) => {
        const seq = Number(item?.seq);
        if (!Number.isFinite(seq)) return true;
        if (to !== null && seq > to) return false;
        if (from !== null && seq < from) return false;
        return true;
    });
}

function renderCeoTurnsIntoTurn(turn, turns = [], { interruptedMarker = false } = {}) {
    if (!turn?.listEl || !turn?.flowEl) return 0;
    if (typeof buildTurnGroupDescriptors !== "function" || typeof renderTraceStep !== "function") return 0;
    const visibleTurns = sliceCeoRailTurns(Array.isArray(turns) ? turns : [], turn);
    if (!visibleTurns.length) return 0;
    const descriptors = buildTurnGroupDescriptors(visibleTurns);
    if (!descriptors.length) return 0;
    syncCeoTurnLoadingOnlyState(turn, false);
    const turnViewState = captureCeoTurnTraceViewState(turn);
    resetCeoToolFlow(turn);
    turn.el?.classList?.add?.("ceo-timeline");
    turn.listEl.classList?.add?.("task-trace-list");
    let lastGroupIndex = -1;
    descriptors.forEach((step, index) => {
        if (!step.textOnly) lastGroupIndex = index;
    });
    turn.listEl.innerHTML = descriptors.map((step, index) => renderTraceStep({
        ...step,
        open: index === lastGroupIndex,
        extraClass: interruptedMarker && index === descriptors.length - 1 ? "ceo-turn-interrupted" : "",
    })).join("");
    if (typeof bindTraceRoundToolStrips === "function") bindTraceRoundToolStrips(turn.listEl);
    if (typeof bindTraceFieldCopyActions === "function") bindTraceFieldCopyActions(turn.listEl);
    turn.steps = descriptors.length;
    turn.lastExecutionTraceSummary = { turns: descriptors };
    turn.flowEl.hidden = false;
    turn.flowEl.open = true;
    applyCeoTurnTraceViewState(turn, turnViewState);
    // 整帧重建换掉了 listEl，实时行先躲在容器里；这一帧账本已经画上的调用让它退场，
    // 还在跑的挂回新的组体内——同一件事不再因为换帧而改变位置。
    pruneCeoLiveToolSteps(turn, turn.lastExecutionTraceSummary);
    if (turn?.liveToolStepsEl?.children?.length) ceoLiveToolStepHost(turn);
    updateCeoTurnMeta(turn, ceoRailMetaLabel(turn) || `${descriptors.length} 组`);
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
        // 续跑轮的 turn patch 与被截断那一跳同 turn_id：先摘掉那条已渲染的暂停气泡，
        // 否则实时回合与它并排就是同一个回合两段轨道。
        if (turnId) discardPausedCeoArchiveTurn(turnId);
        turn = createPendingCeoTurn(source, { scrollMode: "preserve" });
        if (turnId && turn) turn.turnId = turnId;
        if (turn) S.ceoPendingTurns.push(turn);
    }
    if (!turn?.textEl || !turn?.flowEl) return false;
    if (turnId && turn.turnId && turnId !== turn.turnId) {
        turn.lastExecutionTraceSummary = null;
        turn.turns = [];
        turn.liveStreamText = "";
        // 跨 turn 复用同一回合对象时清空 sticky 元数据,避免上一轮的
        // token 用量/完成时间泄漏到新轮次的元信息里。
        turn.usage = null;
        turn.completedAt = "";
        // 上一轮没收尾的实时行不能跟着回合对象带到新轮次里
        clearCeoLiveToolSteps(turn);
    }
    if (turnId) {
        turn.turnId = turnId;
        // 视图状态保持依赖回合元素的稳定 key(reconnect/快照重渲染时锚定与展开态还原)。
        if (turn.el && typeof turn.el.setAttribute === "function") {
            turn.el.setAttribute("data-ceo-key", `turn:${normalizeCeoTurnId(turnId)}`);
        }
    }
    turn.turns = Array.isArray(snapshot?.turns) ? snapshot.turns : [];
    mutateCeoFeed(() => {
        if (status === "running") {
            turn.liveStreamText = String(snapshot?.assistant_text || "");
            renderCeoAssistantTextIntoTurn(turn, "", { status });
        } else {
            turn.liveStreamText = "";
            renderCeoAssistantTextIntoTurn(turn, snapshot?.assistant_text || "", { status });
        }
        const renderedGroups = renderCeoTurnsIntoTurn(turn, turn.turns);
        // 压缩帧早于轨道到达时留下的半切状态：轨道一落地就把 <= boundary 的组补回头段。
        reconcileDeferredCeoCompressionSplit(turn);
        if (!renderedGroups) {
            const timelineActive = !!turn?.lastExecutionTraceSummary
                || (turn?.listEl instanceof HTMLElement && turn.listEl.children.length > 0);
            if (status === "paused") updateCeoTurnMeta(turn, "已暂停");
            else if (status === "error") updateCeoTurnMeta(turn, "运行出错");
            else if (!timelineActive) updateCeoTurnMeta(turn, "等待工具开始...");
        }
        if (renderedGroups) {
            turn.flowEl.hidden = false;
        }
        setCeoTurnUsage(turn, snapshot?.usage);
        setCeoTurnUsageCollapsed(turn, status !== "running");
        applyCeoThinkingFromInflightSnapshot(turn, snapshot?.reasoning);
        icons();
    }, { scrollMode: "preserve" });
    if (targetSessionId) {
        const inflightTurn = dedupeInflightUserMessageAgainstMessages(
            getCeoSessionSnapshotCache(targetSessionId)?.messages || [],
            snapshot
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

function renderPersistedCeoAssistantTurn(item = {}, options = {}) {
    const content = String(item?.content || "");
    const turns = Array.isArray(item?.turns) ? item.turns : [];
    const silentReply = item?.silent_reply === true;
    const silentReason = String(item?.silent_reason || "");
    const status = String(item?.status || "").trim().toLowerCase();
    // follow-up 归档半截回合(后端 archive turn_id = `{原 turn_id}:followup:{随机}`)：
    // 其最后一组工具在收到补充消息时被拦腰打断,需要打上打断标记。
    const isFollowUpArchive = String(item?.turn_id || "").includes(":followup:");
    const historyTimestamp = String(item?.timestamp || "").trim();
    const historyUsage = item?.usage || null;
    const canForkReply = item?.can_fork_reply === true;
    const forkTurnId = String(item?.turn_id || "").trim();
    const messageKey = String(options.key || "");
    const rules = (Array.isArray(options.compressionRules) ? options.compressionRules : [])
        .filter((rule) => rule && rule.state === "completed" && Number.isFinite(Number(rule.boundary)));
    const tagMessage = (el = null) => {
        const host = ceoFeedAppendHost();
        const target = el
            || host?.lastElementChild
            || (host?.children || [])[Math.max(0, (host?.children || []).length - 1)]
            || null;
        if (!target || !messageKey || typeof target.setAttribute !== "function") return;
        target.setAttribute("data-ceo-key", messageKey);
        if (target.dataset && typeof target.dataset.ceoKey !== "string") {
            try { target.dataset.ceoKey = messageKey; } catch (error) { void error; }
        }
    };
    if (status !== "paused" && !turns.length && !silentReply) {
        // 无轨道兜底气泡同样携带元信息(完成时间 + token 用量),同样是点击才显形。
        // 静默回合不走这条：它有自己的折叠行，落到下方共用路径渲染，避免两处装配。
        const el = addMsg(content, "system", { markdown: true, scrollMode: "preserve", timestamp: historyTimestamp, usage: historyUsage, turnId: forkTurnId, canForkReply });
        renderCeoThinkingSummaryFromSnapshot(el, item?.thinking);
        if (rules.length) placeHistoryCompressionAroundElement(el, rules, forkTurnId);
        tagMessage(el);
        return rules.length > 0;
    }
    const turn = createPendingCeoTurn("history", { scrollMode: "preserve" });
    if (!turn) {
        if (!silentReply) {
            const el = addMsg(content, "system", { markdown: true, scrollMode: "preserve", timestamp: historyTimestamp, usage: historyUsage, turnId: forkTurnId, canForkReply });
            if (rules.length) placeHistoryCompressionAroundElement(el, rules, forkTurnId);
            tagMessage(el);
        }
        return rules.length > 0;
    }
    const historyTurnId = normalizeCeoTurnId(item?.turn_id || "");
    if (historyTurnId && turn.el && typeof turn.el.setAttribute === "function") {
        turn.el.setAttribute("data-ceo-key", `turn:${historyTurnId}`);
    }
    S.ceoPendingTurns.push(turn);
    // 重建道：区分线按转录里的边界键放回这一回合的轨道中间（或气泡之前/之后），
    // 而不是照抄转录行的位置——自动压缩的行是在摘要落地时落的，可能在回复行之前。
    const segments = planHistoryCompressionSegments(turns, rules);
    const headEntries = [];
    for (const split of segments.splits) {
        const head = createHistoryCompressionHead(historyTurnId, split);
        if (head) headEntries.push({ head, split });
        else segments.after.push(split.rule);
    }
    const lastBoundary = headEntries.length
        ? headEntries[headEntries.length - 1].split.boundary
        : null;
    turn.turnWindowFrom = lastBoundary === null ? null : lastBoundary + 1;
    if (lastBoundary !== null) turn.compressionBoundary = lastBoundary;
    withCeoFeedBatch(() => {
        if (silentReply) renderCeoSilentTurn(turn, { text: content, timestamp: historyTimestamp });
        else renderCeoAssistantTextIntoTurn(turn, content || (status === "paused" ? "已暂停" : ""), { status });
        renderCeoTurnsIntoTurn(turn, turns, { interruptedMarker: isFollowUpArchive });
        turn.flowEl.hidden = false;
        turn.flowEl.open = true;
        setCeoTurnUsage(turn, historyUsage, { completedAt: historyTimestamp });
        setCeoTurnUsageCollapsed(turn, true);
        renderCeoThinkingSummaryFromSnapshot(turn.el, item?.thinking);
        renderCeoAssistantForkAction(turn, {
            turnId: forkTurnId,
            canForkReply,
            sessionId: activeSessionId(),
        });
        const parent = turn.el?.parentNode;
        if (parent) {
            // createPendingCeoTurn 先把每个头段追加到了 feed 尾部；这里按顺序搬到基础气泡之前。
            headEntries.forEach(({ head }) => {
                if (head.el?.parentNode === parent) parent.removeChild(head.el);
            });
            const firstEl = headEntries[0]?.head?.el || turn.el;
            // 倒序插到 firstEl 之前，最终顺序仍是升序。
            [...segments.before].reverse().forEach((rule) => {
                const divider = buildHistoryCompressionDivider(rule, historyTurnId);
                if (divider) parent.insertBefore(divider, firstEl);
            });
            headEntries.forEach(({ head, split }) => {
                head.turns = turns;
                renderCeoTurnsIntoTurn(head, turns);
                settleCeoCompressionHeadText(head);
                parent.insertBefore(head.el, turn.el);
                const divider = buildHistoryCompressionDivider(split.rule, historyTurnId);
                if (divider) parent.insertBefore(divider, turn.el);
            });
            let anchor = turn.el;
            segments.after.forEach((rule) => {
                const divider = buildHistoryCompressionDivider(rule, historyTurnId);
                if (!divider) return;
                parent.insertBefore(divider, anchor.nextSibling || null);
                anchor = divider;
            });
            icons();
        }
    }, { scrollMode: "preserve" });
    if (messageKey) tagMessage(turn.el);
    if (status === "paused") {
        if (historyTurnId) {
            // 留档给续跑轮接管：后端复用同一个 turn_id 续跑，这条气泡就是那一轮的上一半。
            S.ceoPausedArchiveTurns[historyTurnId] = turn;
        }
        finalizePausedCeoTurn(content || "已暂停", { source: "history" });
        return headEntries.length > 0;
    }
    // meta 带上 usage/timestamp:随后的 finalizeCeoTurn 写缓存与 usage 行时
    // 用历史值而非当前时间,保证刷新后完成时间稳定。
    // silent_reason 必须一起带上：finalize 走 buildFinalizedCeoTurnPayload 重写缓存行，
    // 缺了它这一行的 silent_reason 就落成空串，刷新后审计面看不到静默理由。
    finalizeCeoTurn(content, { source: "history", usage: historyUsage, timestamp: historyTimestamp, silent_reply: silentReply, silent_reason: silentReason });
    return headEntries.length > 0;
}

// ---- CEO 会话视图状态保持 -----------------------------------------------------
// 整页重建(renderCeoSnapshot → resetCeoFeed)会丢三样东西:展开的分组/组内工具条/全部历史、
// 以及滚动锚点。重建前从 DOM 捕获、重建后按稳定 key 还原,key 失效时回退为像素 clamp。
// 身份 key:消息/回合元素上的 data-ceo-key;未打 key 的元素用"从底部计数"的位置兜底。
// 分组 step 用 data-trace-key,但纯序号兜底会在不同回合间撞车,所以按回合身份分域。

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

function buildFinalizedCeoTurnPayload(sessionId, { normalizedSource = "", normalizedTurnId = "", finalUserMessages = [], finalTurns = [], text = "", meta = null, completedAt = "", status = "" } = {}) {
    // finalize 三个旧分支的缓存写入统一为一次计算:消息列表与 inflight 清空只在此处构建。
    const entry = getCeoSessionSnapshotCache(sessionId);
    const inflightTurn = normalizeCeoSnapshotInflight(entry?.inflight_turn);
    const inflightTurnId = normalizeCeoTurnId(inflightTurn?.turn_id);
    const inflightSource = normalizeCeoTurnSource(inflightTurn?.source || "user");
    const inflightMatchesSource = !inflightTurn
        || (normalizedTurnId && inflightTurnId && inflightTurnId !== normalizedTurnId ? false : true)
        || !String(inflightTurn?.source || "").trim()
        || ceoTurnSourceMatches(normalizedSource, inflightSource);
    const persistedTurns = Array.isArray(finalTurns) ? finalTurns : [];
    const userMessagesToAppend = finalUserMessages.length
        ? finalUserMessages
        : (inflightMatchesSource
            ? normalizeCeoSnapshotUserMessages(inflightTurn?.user_messages, inflightTurn?.user_message)
            : []);
    let messages = trimCeoSessionSnapshotMessages(entry?.messages);
    messages = appendMissingCeoUserMessages(messages, userMessagesToAppend);
    // usage/timestamp 无条件写入缓存:会话切换/刷新后从缓存渲染时,
    // 元信息(token 用量 + 完成时间)不再依赖服务端快照补齐。
    const completedTimestamp = String(completedAt || "").trim();
    const silentReply = meta?.silent_reply === true;
    // 暂停收尾写的这一行必须是「暂停的回复行」而不是「正常的最终回复」：刷新后服务端
    // 归档行带 status=paused，缓存行不带的话，同一回合在暂停后与刷新后是两种画法
    // （前者普通气泡、后者带暂停标记），分组卡的渲染分支也不同。
    const rowStatus = String(status || "").trim().toLowerCase();
    const rowTurnId = normalizeCeoTurnId(normalizedTurnId || inflightTurn?.turn_id || "");
    messages = appendCeoSessionSnapshotMessage(messages, {
        role: "assistant",
        // 静默回合的正文要留在缓存行里：那是折叠行「展开」的内容，也是与后端快照
        // 一致的形状（后端已不再抹空）。抹成 "" 会让刷新后折叠行展开出空白。
        content: String(text || "").trim() || (silentReply ? "" : "Done."),
        ...(silentReply ? { silent_reply: true, silent_reason: String(meta?.silent_reason || "") } : {}),
        ...(rowStatus ? { status: rowStatus } : {}),
        // turn_id 是这一行参与门槛帧（回复行 Fork）与去重（同 turn 的 preserved_turn）
        // 的唯一身份。缺了它，暂停收尾补出来的回复行既等不到 Fork 章，也不会阻止同一
        // 回合被 preserved_turn 再画一遍。
        ...(rowTurnId ? { turn_id: rowTurnId } : {}),
        ...(persistedTurns.length ? { turns: persistedTurns } : {}),
        usage: meta?.usage || null,
        ...(completedTimestamp ? { timestamp: completedTimestamp } : {}),
    });
    return { messages, inflight_turn: inflightMatchesSource ? null : inflightTurn };
}

// 转录行里的位置不代表线的真正位置：自动压缩的行是在摘要落地时落的，可能在回复行之前。
// 先把完成态标记按 turn_id 收成规则表，再把规则交给该回合**最后一条** assistant 行；
// 被它接管的标记行在按行渲染时直接跳过，避免“先按行画一条、再按边界画一条”。
// 输入必须是全量 messages（分页窗口只决定渲染哪一段，不参与规则计算）。
function renderCeoSnapshotMessageRange(messages, keys, fromIndex, toIndex, targetSessionId) {
    // 渲染 messages[fromIndex,toIndex) 到 ceoFeedAppendHost()；首屏窗口与
    // 分页补渲染共用。key 由 buildCeoMessageKeyList 全局算好传入，occ 计数
    // 与整列一致，窗口起点无关。
    const compressionPlan = buildCeoCompressionPlan(messages);
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
            const turnId = String(item?.turn_id || "").trim();
            const isLastRowForTurn = !!turnId && compressionPlan.lastAssistantRowByTurn.get(turnId) === index;
            const rules = isLastRowForTurn ? (compressionPlan.rulesByTurn.get(turnId) || []) : [];
            renderPersistedCeoAssistantTurn(item, {
                compressionRules: rules,
                key: String(keys[index] || ""),
            });
            // assistant 行的 key 由 renderPersistedCeoAssistantTurn 打在基础气泡上；
            // 这里不能再 tagRendered：线可能在气泡之后，lastElementChild 会是线而不是气泡。
            continue;
        }
        if (role === "system" && content.trim()) {
            const marker = normalizeCeoSnapshotCompressionMarker(item?.compression_marker);
            if (marker) {
                // 被 assistant 行接管的标记不再按行位置画；消费方可能不在当前窗口，
                // 它进入窗口时会在边界处画线（plan 按全量 messages 计算）。
                if (compressionPlan.consumedMarkerRows.has(index)) continue;
                const rawBoundary = Number(marker.boundary_turn_seq);
                appendCeoCompressionDivider(marker.state, {
                    interactive: false,
                    turnId: String(marker.turn_id || ""),
                    boundary: Number.isFinite(rawBoundary) && rawBoundary > 0 ? rawBoundary : "",
                    origin: "row",
                });
                tagRendered();
            } else {
                addMsg(content, "system", { markdown: true, scrollMode: "preserve" });
                tagRendered();
            }
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
        // 转录里进行中回合的压缩标记行改切进回合回合轨道（见函数注）。
        applyCeoInflightCompressionRule(messages, normalizedInflightTurn);
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
                <div class="ceo-turn-meta">
                    <div class="ceo-turn-usage" hidden></div>
                    ${buildCeoBubbleCopyMarkup("复制回复")}
                </div>
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
            // 静默折叠行（在气泡外面），由 renderCeoSilentTurn 建一次。
            silentLineEl: null,
            steps: 0,
            hasError: false,
            finalized: false,
            historyExpanded: false,
            lastExecutionTraceSummary: null,
            // 在飞工具行的容器（见 ceoLiveToolStepHost），卡片开合不由它决定
            liveToolStepsEl: null,
            liveStreamText: "",
            // 元数据(sticky):token 用量与完成时间,由 setCeoTurnUsage 维护。
            usage: null,
            completedAt: "",
            contextLoadNoticeKeys: new Set(),
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
    // 收尾本身就是"这条会话没有回合在跑"的证据，而一个会话同时只跑一条回合。所以只挂着
    // 加载图标、一个字符都没有的气泡不可能属于另一条还在跑的回合：它某一跳没等到 final
    // （内部轮的最终回复为空时服务端就不转发那一帧），按 source/turnId 筛永远筛不到它，
    // 于是加载图标永久留在会话面上，刷新才被服务端的干净快照抹掉。
    for (let index = S.ceoPendingTurns.length - 1; index >= 0; index -= 1) {
        const turn = S.ceoPendingTurns[index];
        if (!turn || turn.finalized) continue;
        if (!turn.textEl?.classList?.contains?.("assistant-text-loading")) continue;
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

function discardPausedCeoArchiveTurn(turnId = "") {
    // 停机截断那一跳的气泡是转录里的历史行，续跑轮与它同 turn_id（后端复用），
    // 不摘掉它就会和实时回合并排画出同一个回合两段轨道——正是"一个回合被切成两个气泡"。
    const normalizedTurnId = normalizeCeoTurnId(turnId);
    if (!normalizedTurnId) return false;
    const pausedTurn = S.ceoPausedArchiveTurns?.[normalizedTurnId];
    if (!pausedTurn) return false;
    delete S.ceoPausedArchiveTurns[normalizedTurnId];
    return mutateCeoFeed(() => {
        pausedTurn.finalized = true;
        pausedTurn.el?.remove?.();
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
    if (normalizedTurnId) discardPausedCeoArchiveTurn(normalizedTurnId);
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

// 轨道在气泡上唯一一直在动的一行：组数、轮数、以及此刻还在跑的工具数。
// 工具帧不许把它换成"正在处理中"这种静态句子——一个十几秒的并行批次里那读起来
// 就是"一直在加载、不刷新"，而刷新后快照重渲又把计数放了回来。
function ceoRailMetaLabel(turn, turns = null) {
    const items = Array.isArray(turns)
        ? turns
        : (Array.isArray(turn?.lastExecutionTraceSummary?.turns) ? turn.lastExecutionTraceSummary.turns : []);
    if (!items.length) return "";
    const toolCount = items.reduce((sum, item) => sum + (Array.isArray(item?.tools) ? item.tools.length : 0), 0);
    const running = turn.liveToolStepsEl
        ? Array.from(turn.liveToolStepsEl.children).filter((item) => String(item?.dataset?.stepState || "") === "running").length
        : 0;
    return `${items.length} 组 · ${toolCount} 次调用${running ? ` · 正在跑 ${running} 个工具` : ""}`;
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

// 会话回合行、消息元信息、节点详情头部共用这一份读数：同一串 token 文案只在一处印，
// 改口径时三处同时跟着变。
function buildTokenUsageParts(usage) {
    const normalized = normalizeCeoTurnUsage(usage);
    if (!normalized) return [];
    // 命中率按整份 prompt 算（未命中新增 + 缓存命中），与逐次调用明细表的 modelCallHitRate 同口径。
    const totalInput = normalized.input_tokens + normalized.cache_hit_tokens;
    const hitRateText = totalInput
        ? ` / ${((normalized.cache_hit_tokens / totalInput) * 100).toFixed(1)}%`
        : "";
    const parts = [
        `输入 ${formatCeoTokenCount(normalized.input_tokens)}`,
        `缓存命中 ${formatCeoTokenCount(normalized.cache_hit_tokens)}${hitRateText}`,
        `输出 ${formatCeoTokenCount(normalized.output_tokens)}`,
    ];
    if (normalized.call_count > 0) parts.push(`轮数 ${normalized.call_count}`);
    return parts;
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
    parts.push(...buildTokenUsageParts(turn.usage));
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

// 普通消息气泡(用户/无轨道助手兜底)的元信息文本:时间必带角色语义,
// usage 仅在数据可用时附加(用户消息只有发送时间)。
function buildCeoMessageMetaText({ role = "", timestamp = "", usage = null } = {}) {
    const parts = [];
    const timeText = formatCompactTime(timestamp);
    if (timeText) {
        const isUser = String(role || "").trim().toLowerCase() === "user";
        parts.push(`${isUser ? "发送于" : "完成于"} ${timeText}`);
    }
    parts.push(...buildTokenUsageParts(usage));
    return parts.join(" · ");
}

function setCeoTurnUsageCollapsed(turn, collapsed = true) {
    if (!turn?.el?.classList) return;
    turn.el.classList.toggle("usage-collapsed", !!collapsed);
}

// 元信息行（完成时间 / 输入·缓存命中·输出 / 复制）改为"点击这条消息才显示"。
// 悬停门在长会话里会把相邻两条同时点亮，而且鼠标扫过就闪；Fork 按钮不吃这道门
// （它常驻，见 .msg-action-fork 的样式）。
function toggleCeoMessageMetaReveal(target = null) {
    if (!target || typeof target.closest !== "function") return;
    if (target.closest("button, a, summary, input, textarea, select, label")) return;
    const messageEl = target.closest(".message");
    if (!messageEl?.classList || typeof messageEl.classList.toggle !== "function") return;
    // 选中正文收尾的那次 click 不算"点开"——复制按钮之外的人常在选字
    const selection = typeof window.getSelection === "function" ? window.getSelection() : null;
    if (selection && String(selection.toString() || "").trim()) return;
    messageEl.classList.toggle("meta-open");
}

function findCeoToolStep(turn, { toolCallId = "", toolName = "" } = {}) {
    const items = Array.from(turn?.liveToolStepsEl?.children || []);
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

// 这一族记的是"模型对上下文动的一次手"：三类载入加上记忆写入。它们都不值得占一条工具行——
// 载入的内容就躺在身后那段上下文里，写入的内容就是用户刚说的那句话；失败才要回到工具行，
// 因为失败是一个需要被看见的结果。
function ceoContextLoaderKind(toolName = "") {
    const normalized = String(toolName || "").trim().toLowerCase();
    if (normalized === "load_tool_context" || normalized === "load_tool_context_v2") return "tool";
    if (normalized === "load_skill_context" || normalized === "load_skill_context_v2") return "skill";
    if (normalized === "memory_note") return "note";
    if (normalized === "memory_update") return "memory";
    return "";
}

function isCeoContextLoaderToolName(toolName = "") {
    return !!ceoContextLoaderKind(toolName);
}

// 未水合工具被直接调用时，那一跳没有执行工具，只把契约交回。它与 loader 同一族：
// 会话不画普通工具行、改弹 notice；节点详情画「加载 tool」卡片。标记由运行时按调用行落盘。
function isCeoContractDeliveryStep(step = null) {
    return !!String(step?.contract_delivery || "").trim();
}

function isCeoContextLoadStep(step = null) {
    return isCeoContextLoaderToolName(step?.tool_name) || isCeoContractDeliveryStep(step);
}

// `silent` 是控制信号不是活动：它既不进 live 步骤也不进回合轨道。展开气泡里看到
// "模型调用了 silent"没有信息量（静默本身已由折叠行表达），失败时还会误导成任务出错。
function isCeoSilentToolName(toolName = "") {
    return String(toolName || "").trim().toLowerCase() === "silent";
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
    if (normalizedKind === "note") return "notebook-pen";
    if (normalizedKind === "memory") return "brain";
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
    if (["tool", "skill", "note", "memory"].includes(normalizedKind)) {
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
    const key = loaderKind === "skill" ? "skill_id" : loaderKind === "note" ? "ref" : loaderKind === "memory" ? "request_id" : "tool_id";
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
    if (loaderKind === "note") {
        return normalizedTargetId
            ? `\u5df2\u52a0\u8f7d note:${normalizedTargetId}`
            : "\u5df2\u52a0\u8f7d\u8bb0\u5fc6\u7b14\u8bb0";
    }
    return normalizedTargetId
        ? `\u5df2\u52a0\u8f7d\u5de5\u5177 ${normalizedTargetId}`
        : "\u5df2\u52a0\u8f7d\u5de5\u5177\u4e0a\u4e0b\u6587";
}

// 写侧的真实结局都来自同一个入队门的返回：queued / ignored。
// 气泡只承诺这一层——入队不等于记住，成不成由记忆 worker 判定，所以文案不许写成"已记住"。
function buildCeoMemoryNotice(payload = null) {
    const status = String(payload?.status || "").trim().toLowerCase();
    const requestId = String(payload?.request_id || "").trim();
    if (status === "queued") {
        return { text: requestId ? `记忆请求已入队 ${requestId}` : "记忆请求已入队", riskLevel: "low" };
    }
    if (status === "ignored") {
        return { text: "这条记忆请求内容为空，没有入队", riskLevel: "medium" };
    }
    return null;
}
// 资源目录一行一个工具，风险度直接挂在工具记录上。
function toolCatalogRiskLevel(record, targetId = "") {
    const normalizedTargetId = String(targetId || "").trim();
    if (!normalizedTargetId) return "";
    if (String(record?.tool_id || "").trim() !== normalizedTargetId) return "";
    return normalizeNoticeRiskLevel(record?.risk_level);
}

function resolveCeoContextLoadNoticeRiskLevel(kind = "", targetId = "") {
    const normalizedKind = String(kind || "").trim().toLowerCase();
    const normalizedTargetId = String(targetId || "").trim();
    if (normalizedKind === "skill") {
        const match = (Array.isArray(ResourcesState.skills) ? ResourcesState.skills : []).find((item) => (
            String(item?.skill_id || "").trim() === normalizedTargetId
        ));
        return normalizeNoticeRiskLevel(match?.risk_level || "medium");
    }
    if (normalizedKind === "tool") {
        const records = Array.isArray(ResourcesState.tools) ? ResourcesState.tools : [];
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

function maybeShowCeoContextLoadNotice(turn, { toolName = "", status = "", detailTexts = [], targetId: providedTargetId = "" } = {}) {
    const normalizedTool = String(toolName || "").trim().toLowerCase();
    const loaderKind = ceoContextLoaderKind(normalizedTool);
    const normalizedStatus = String(status || "").trim().toLowerCase();
    if (!loaderKind || normalizedStatus !== "success") return false;
    if (String(turn?.source || "").trim().toLowerCase() === "history") return false;
    const keys = ensureCeoContextLoadNoticeKeys(turn);
    // 直接调用投递的那一行，tool_name 是被加载的工具本身；目标由 marked step 直接给出。
    let resolvedTargetId = String(providedTargetId || "").trim();
    let payload = null;
    for (const candidate of Array.isArray(detailTexts) ? detailTexts : []) {
        payload = parseJsonObjectText(candidate) || payload;
        if (!resolvedTargetId) {
            resolvedTargetId = extractCeoContextLoadTarget(normalizedTool, candidate);
        }
    }
    const targetId = resolvedTargetId;
    const memoryNotice = loaderKind === "memory" ? buildCeoMemoryNotice(payload) : null;
    // 读不出 status 就不弹：写记忆这件事宁可没提示，也不能给出一条编出来的"已提交"。
    if (loaderKind === "memory" && !memoryNotice) return false;
    const noticeText = memoryNotice ? memoryNotice.text : buildCeoContextLoadNotice(normalizedTool, targetId);
    const signature = `${normalizedTool}:${targetId || noticeText}`;
    if (keys?.has(signature)) return true;
    keys?.add(signature);
    showCeoContextLoadNotice(noticeText, {
        kind: loaderKind,
        riskLevel: memoryNotice ? memoryNotice.riskLevel : resolveCeoContextLoadNoticeRiskLevel(loaderKind, targetId),
    });
    return true;
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

function renderCeoToolIcon(iconWrap, iconName = "loader") {
    if (!(iconWrap instanceof HTMLElement)) return;
    const nextIcon = String(iconName || "loader").trim() || "loader";
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

async function copyCeoBubbleContent(button) {
    if (!(button instanceof HTMLElement)) return;
    // 复制的是气泡里渲染出来的可见文本:轨道回合只取正文(不含回合轨道),
    // 普通气泡取 msg-content。历史回放与 live 收尾都走同一份 DOM,无需回源。
    const message = button.closest(".message");
    const source = message?.querySelector?.(".assistant-text") || message?.querySelector?.(".msg-content");
    const text = String(source?.innerText ?? source?.textContent ?? "").trim();
    if (!text) {
        if (typeof flashTraceCopyButton === "function") flashTraceCopyButton(button, false);
        showToast({ title: "没有可复制的内容", text: "该消息暂无正文。", kind: "error" });
        return;
    }
    const copied = await copyTextToClipboard(text);
    if (typeof flashTraceCopyButton === "function") flashTraceCopyButton(button, !!copied);
    showToast({
        title: copied ? "已复制" : "复制失败",
        text: copied ? "消息内容已复制到剪贴板。" : "请手动选中文本后复制。",
        kind: copied ? "success" : "error",
    });
}

async function copyCeoSessionId(button) {
    if (!(button instanceof HTMLElement)) return;
    const text = String(button.dataset.sessionCopyId || "").trim();
    if (!text) return;
    let copied = false;
    // copyTextToClipboard 不吞异常:剪贴板被拒时也必须落到失败反馈，否则点了毫无反应。
    try {
        copied = await copyTextToClipboard(text);
    } catch (error) {
        void error;
    }
    if (typeof flashTraceCopyButton === "function") flashTraceCopyButton(button, !!copied);
    showToast({
        title: copied ? "已复制" : "复制失败",
        text: copied ? "会话 ID 已复制到剪贴板。" : "请手动选中文本后复制。",
        kind: copied ? "success" : "error",
    });
}

function trimCeoToolSteps(turn) {
    if (!turn?.liveToolStepsEl) return;
    // 实时行现在住在分组卡体内的容器里，"较早进度"的封顶只按这些行算，不再数分组卡。
    const items = Array.from(turn.liveToolStepsEl.children || []).filter((item) => (
        item instanceof HTMLElement
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
        renderCeoToolIcon(iconWrap, resolvedStage.icon);
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

function ceoLastTraceStepEl(turn) {
    if (!turn?.listEl || typeof turn.listEl.querySelectorAll !== "function") return null;
    const steps = turn.listEl.querySelectorAll(".task-trace-step");
    return steps && steps.length ? steps[steps.length - 1] : null;
}

// 账本里的 tool_call_id 是 "call_x|fc_y" 这种合成形
// （g3ku/providers/responses_protocol_helpers.py 拼的），实时帧可能只带前半段，
// 两边都按前半段比才认得出同一次调用。与 g3ku/runtime/tool_history.py 的
// extract_call_id 同一个口径。
function ceoToolCallKey(value = "") {
    const raw = String(value || "").trim();
    if (!raw) return "";
    const separator = raw.indexOf("|");
    return separator >= 0 ? raw.slice(0, separator) : raw;
}

function ceoCommittedToolCallKeys(summary = null) {
    const keys = new Set();
    (Array.isArray(summary?.turns) ? summary.turns : []).forEach((turn) => {
        (Array.isArray(turn?.tools) ? turn.tools : []).forEach((tool) => {
            const key = ceoToolCallKey(tool?.tool_call_id);
            if (key) keys.add(key);
        });
    });
    return keys;
}

// 在飞工具行的家：最后一个文本组的体内。画在组外面时，它对应的工具一进账本，
// 整帧重建就把它从组外搬进组里——同一条工具在界面上跳一次位置。
// 卡片开合是用户的选择：这里只挂行，不替他展开（收起时行随卡一起藏起来）。
function ceoLiveToolStepHost(turn) {
    if (!turn?.listEl) return null;
    if (!(turn.liveToolStepsEl instanceof HTMLElement)) {
        const container = document.createElement("div");
        container.className = "task-trace-live-steps";
        // 行本身带 role=listitem，挂进组体后父节点换成这个容器，列表语义要跟着落在这里
        container.setAttribute("role", "list");
        turn.liveToolStepsEl = container;
    }
    const host = turn.liveToolStepsEl;
    const groupEl = ceoLastTraceStepEl(turn);
    const body = groupEl instanceof HTMLElement && typeof groupEl.querySelector === "function"
        ? groupEl.querySelector(".task-trace-body")
        : null;
    const target = body instanceof HTMLElement ? body : turn.listEl;
    if (host.parentElement !== target) target.appendChild(host);
    return host;
}

// 实时行的退场时机：账本已经画上这次调用的，或这一条已经跑完（终态行由正式轮次接管，
// 只在卡里活到下一帧重建）——只有还在跑的行需要跨帧活着，否则工具行会在返回前消失。
function pruneCeoLiveToolSteps(turn, summary = null) {
    const host = turn?.liveToolStepsEl;
    if (!host || typeof host.children === "undefined") return;
    const committed = ceoCommittedToolCallKeys(summary || turn?.lastExecutionTraceSummary);
    Array.from(host.children).forEach((item) => {
        if (typeof item.remove !== "function") return;
        const key = ceoToolCallKey(item?.dataset?.toolCallId);
        const state = String(item?.dataset?.stepState || "").trim();
        if ((key && committed.has(key)) || state === "success" || state === "error") item.remove();
    });
    // 空容器留在 grid 里会占一格行距，行都退场后顺手摘掉（下一条工具帧再挂回来）
    if (!host.children.length && host.parentElement && typeof host.remove === "function") host.remove();
}

function clearCeoLiveToolSteps(turn) {
    const host = turn?.liveToolStepsEl;
    if (host && typeof host.children !== "undefined") {
        Array.from(host.children).forEach((item) => {
            if (typeof item.remove === "function") item.remove();
        });
    }
    if (host?.parentElement && typeof host.remove === "function") host.remove();
}

function applyCeoToolEventToTurn(turn, event = {}) {
    if (!turn?.listEl || !turn?.flowEl) return null;
    const status = resolveCeoToolEventStatus(event);
    const toolName = String(event.tool_name || "tool").trim() || "tool";
    const rawText = String(event.text || "").trim();
    if (isCeoSilentToolName(toolName)) return null;
    // 直接调用投递：tool_name 是被加载的工具本身，按 loader 的同一画法弹「已加载工具 X」，
    // 并且不建普通工具行（那一跳没有执行工具）。
    const contractDelivery = String(event.contract_delivery || "").trim();
    if (contractDelivery && status !== "error") {
        maybeShowCeoContextLoadNotice(turn, {
            toolName: "load_tool_context",
            status,
            targetId: toolName,
            detailTexts: [
                rawText,
                String(event.output_text || "").trim(),
                String(event.output_preview_text || "").trim(),
                String(event.arguments_text || "").trim(),
            ],
        });
        return null;
    }
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
    const eventKind = String(event.kind || "").trim().toLowerCase();
    const detail = status === "running" && eventKind === "tool_start"
        ? ""
        : ceoFriendlyToolDetail(toolName, rawText, status, event.kind);
    const outputRef = normalizeTraceOutputRef(event.output_ref || "");
    const toolCallId = String(event.tool_call_id || "").trim();
    const callKey = ceoToolCallKey(toolCallId);
    // 账本已经画上这次调用（补丁按帧先到、工具帧后到）就不再建行，
    // 否则分组卡里会同时挂着正式轮次和一条实时行。
    if (callKey && ceoCommittedToolCallKeys(turn.lastExecutionTraceSummary).has(callKey)) return null;
    const host = ceoLiveToolStepHost(turn);
    let item = findCeoToolStep(turn, { toolCallId, toolName });
    const stage = ceoToolStage(toolName, rawText, status);
    if (!(item instanceof HTMLElement)) {
        item = document.createElement("div");
        item.setAttribute("role", "listitem");
        item.innerHTML = `
            <div class="interaction-step-header">
                <span class="interaction-step-lead">
                    <span class="interaction-step-icon" data-icon-name="loader"><i data-lucide="loader"></i></span>
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
        host.appendChild(item);
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
    // 有轨道时保住那行一直在动的计数，只在没有轨道（纯工具回合）才用状态句子
    updateCeoTurnMeta(turn, ceoRailMetaLabel(turn) || stage.meta);
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
    const finalUserMessages = normalizeCeoSnapshotUserMessages(meta?.user_messages, meta?.user_message);
    let turn = pullActiveCeoTurn(normalizedSource, normalizedTurnId);
    // final 事件带的 turns 优先；缺失时回退到本轮 run 中自己渲染出的那份，
    // 避免「工具组在最终答复后消失、刷新才回来」。该兜底是当前轮的 turns，
    // 不会回填旧轮次全量轨迹；心跳轮没有 live turns（空）不受影响。
    const metaTurns = Array.isArray(meta?.turns) ? meta.turns : [];
    const finalTurns = metaTurns.length
        ? metaTurns
        : ((Array.isArray(turn?.turns) && turn.turns.length) ? turn.turns : []);
    // 三个旧分支的缓存写入统一为一次计算:消息列表与 inflight 清空只在处构建,
    // 渲染层再决定走增量更新还是全量快照重建。完成时间:历史回合用消息自带
    // timestamp,live 回合用收尾时刻的本地时间。
    const completedAt = String(meta?.timestamp || "").trim() || new Date().toISOString();
    const finalPayload = buildFinalizedCeoTurnPayload(sessionId, {
        normalizedSource,
        normalizedTurnId,
        finalUserMessages,
        finalTurns,
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
            // 内部轮（heartbeat / cron）不为"处理中"建 live 回合元素，模型选静默时往往
            // 无元素可收尾。这里按需补一个再画折叠行，让内部轮与用户轮同一种画法——
            // 否则同一轮静默在实时视图里什么都不留、刷新后才冒出来。用户轮保持原样：
            // 它的静默由转录行与快照渲染，找不到元素说明这一轮的元素已被丢弃。
            if (silentReply && (normalizedSource === "heartbeat" || normalizedSource === "cron")) {
                turn = ensureActiveCeoTurn({ source: normalizedSource, turnId: normalizedTurnId });
            }
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
        }
        mutateCeoFeed(() => {
            turn.finalized = true;
            turn.liveStreamText = "";
            renderCeoLiveStreamTextIntoTurn(turn);
            if (silentReply) {
                renderCeoSilentTurn(turn, { text, timestamp: completedAt });
            } else {
                turn.textEl.hidden = false;
                turn.textEl.innerHTML = renderMarkdown(String(text || "").trim() || "已完成。");
                turn.textEl.classList.remove("pending");
                turn.textEl.classList.add("markdown-content");
                turn.textEl.classList.remove("assistant-text-loading");
                syncCeoAssistantLoadingAria(turn.textEl);
            }
            if (finalTurns.length) {
                // 收尾帧先摘掉实时行：正式工具这时已经在这组里，留着一份就是同一件事画两遍
                clearCeoLiveToolSteps(turn);
                renderCeoTurnsIntoTurn(turn, finalTurns);
            }
            if (turn.steps > 0) {
                const hasRunningStep = hasRunningCeoToolStep(turn);
                turn.flowEl.hidden = false;
                // 轨道的展开态已由 renderCeoTurnsIntoTurn 按用户选择还原,
                // 收尾不再强制展开;仅无轨道(纯工具事件回合)保留默认展开旧行为。
                if (!finalTurns.length) turn.flowEl.open = true;
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
        // 收尾后才对账：discardPendingCeoTurns / hasRunningCeoToolStep 都按 .running 判活，
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
            turn.finalized = true;
            turn.liveStreamText = "";
            renderCeoLiveStreamTextIntoTurn(turn);
            if (silentReply) {
                renderCeoSilentTurn(turn, { text, timestamp: completedAt });
            } else {
                turn.textEl.hidden = false;
                turn.textEl.innerHTML = renderMarkdown(String(text || "").trim() || "已完成。");
                turn.textEl.classList.remove("pending");
                turn.textEl.classList.add("markdown-content");
                turn.textEl.classList.remove("assistant-text-loading");
                syncCeoAssistantLoadingAria(turn.textEl);
            }
            if (finalTurns.length) {
                // 收尾帧先摘掉实时行：正式工具这时已经在这组里，留着一份就是同一件事画两遍
                clearCeoLiveToolSteps(turn);
                renderCeoTurnsIntoTurn(turn, finalTurns);
            }
            if (turn.steps > 0) {
                const hasRunningStep = hasRunningCeoToolStep(turn);
                turn.flowEl.hidden = false;
                // 轨道的展开态已由 renderCeoTurnsIntoTurn 按用户选择还原,
                // 收尾不再强制展开;仅无轨道(纯工具事件回合)保留默认展开旧行为。
                if (!finalTurns.length) turn.flowEl.open = true;
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
        retireCeoTakenFollowUpsFromRepresentedMessages(sessionId, finalUserMessages);
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
    retireCeoTakenFollowUpsFromRepresentedMessages(sessionId, finalUserMessages);
    maybeDispatchQueuedCeoFollowUps();
}

function addNotice(notice, _bump = true) {
    const payload = notice && typeof notice === "object" ? notice : {};
    const kind = String(payload.kind || "").toLowerCase();
    showToast({ title: payload.title || "Notice", text: payload.text || "", kind: kind.includes("fail") || kind.includes("error") ? "error" : "success" });
}


function syncDetailSaveButton(kind) {
    const isSkill = kind === "skill";
    const root = isSkill ? U.skillDetail : U.toolDetail;
    const button = root?.querySelector(isSkill ? "#skill-modal-save" : "#tool-modal-save");
    const hint = root?.querySelector(".resource-draft-hint");
    const dirty = isSkill ? ResourcesState.skillDirty : ResourcesState.toolDirty;
    const busy = isSkill ? ResourcesState.skillBusy : ResourcesState.toolBusy;

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
    ResourcesState.skillDirty = !!next;
    renderSkillActions();
}

function setToolDirty(next = true) {
    ResourcesState.toolDirty = !!next;
    renderToolActions();
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
    const menu = getResourceSelectMenu(shell);
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
        const menu = getResourceSelectMenu(shell);
        shell.classList.remove("is-open");
        if (menu) {
            menu.hidden = true;
            returnResourceSelectMenuPortal(shell, menu);
        }
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
    const menu = getResourceSelectMenu(shell);
    const optionButtons = menu ? [...menu.querySelectorAll(".resource-select-option")] : [];
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
    const menu = getResourceSelectMenu(shell);
    const options = menu ? [...menu.querySelectorAll(".resource-select-option")] : [];
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

const RESOURCE_SELECT_PORTAL_GAP = 10;

// 展开态的下拉面板默认绝对定位在 .resource-select-shell 里，一旦这个 shell 处在
// 滚动容器内（模型配置弹窗的 body），面板会把容器撑高、并被聚焦的选中项滚动到可视区，
// 整张表单就往上跳。带 data-resource-select-portal 的下拉改为挂到 body 上按视口定位。
function getResourceSelectMenu(shell) {
    if (!(shell instanceof HTMLElement)) return null;
    const own = shell.querySelector(".resource-select-menu");
    if (own) return own;
    const selectId = String(shell.dataset.selectId || "");
    return selectId ? document.getElementById(`${selectId}-menu`) : null;
}

function positionResourceSelectMenuPortal(trigger, menu) {
    const rect = trigger.getBoundingClientRect();
    menu.style.left = `${Math.round(rect.left)}px`;
    menu.style.width = `${Math.round(rect.width)}px`;
    menu.style.right = "auto";
    const menuHeight = menu.getBoundingClientRect().height;
    const below = window.innerHeight - rect.bottom;
    if (below < menuHeight + RESOURCE_SELECT_PORTAL_GAP && rect.top > below) {
        menu.style.top = "auto";
        menu.style.bottom = `${Math.round(window.innerHeight - rect.top + RESOURCE_SELECT_PORTAL_GAP)}px`;
    } else {
        menu.style.bottom = "auto";
        menu.style.top = `${Math.round(rect.bottom + RESOURCE_SELECT_PORTAL_GAP)}px`;
    }
}

function detachResourceSelectMenuPortal(menu) {
    menu.classList.remove("resource-select-menu--portal");
    menu.style.left = "";
    menu.style.top = "";
    menu.style.right = "";
    menu.style.bottom = "";
    menu.style.width = "";
    menu.__resourceSelectOwner = null;
    menu.remove();
}

function syncResourceSelectMenuPortals() {
    let liveCount = 0;
    document.querySelectorAll("body > .resource-select-menu--portal").forEach((menu) => {
        const owner = menu.__resourceSelectOwner;
        const trigger = owner?.querySelector(".resource-select-trigger");
        if (!owner || !owner.isConnected || !(trigger instanceof HTMLElement)) {
            detachResourceSelectMenuPortal(menu);
            return;
        }
        liveCount += 1;
        positionResourceSelectMenuPortal(trigger, menu);
    });
    if (!liveCount) {
        window.removeEventListener("scroll", syncResourceSelectMenuPortals, true);
        window.removeEventListener("resize", syncResourceSelectMenuPortals, true);
    }
}

function portalResourceSelectMenu(shell, trigger, menu) {
    if (!(menu instanceof HTMLElement) || !(trigger instanceof HTMLElement)) return;
    if (menu.parentElement !== document.body) document.body.appendChild(menu);
    menu.__resourceSelectOwner = shell;
    menu.classList.add("resource-select-menu--portal");
    positionResourceSelectMenuPortal(trigger, menu);
    window.addEventListener("scroll", syncResourceSelectMenuPortals, true);
    window.addEventListener("resize", syncResourceSelectMenuPortals, true);
}

function returnResourceSelectMenuPortal(shell, menu) {
    if (!(menu instanceof HTMLElement) || !menu.classList.contains("resource-select-menu--portal")) return;
    detachResourceSelectMenuPortal(menu);
    if (shell?.isConnected) shell.appendChild(menu);
    syncResourceSelectMenuPortals();
}

function openResourceSelect(select, { focus = "selected" } = {}) {
    if (!(select instanceof HTMLSelectElement)) return;
    if (select.disabled) return;
    const shell = select.closest(".resource-select-shell");
    if (!shell) return;
    const trigger = shell.querySelector(".resource-select-trigger");
    const menu = getResourceSelectMenu(shell);
    closeResourceSelects({ exceptId: select.id });
    shell.classList.add("is-open");
    if (menu) menu.hidden = false;
    if (trigger) trigger.setAttribute("aria-expanded", "true");
    if (select.dataset.resourceSelectPortal === "true") portalResourceSelectMenu(shell, trigger, menu);
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
    syncResourceSelectMenuPortals();
    document.querySelectorAll("select.resource-select").forEach((select) => buildResourceSelect(select));
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
    renderProjectSettingsUpdate(S.updateStatus);
    void refreshUpdateStatus();
    window.requestAnimationFrame(() => U.projectSettingsDialog?.focus?.());
}

function closeProjectSettingsDialog() {
    if (!U.projectSettingsBackdrop) return;
    closePasswordChangeDialog();
    closeConfigBundleDialog();
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

function isConfigBundleOpen() {
    return !!U.configBundleBackdrop && !U.configBundleBackdrop.hidden;
}

function setConfigBundleBusy(busy) {
    const disabled = Boolean(busy);
    [U.configBundleExport, U.configBundleImport]
        .forEach((element) => {
            if (element) element.disabled = disabled;
        });
}

function syncConfigBundleFileName() {
    if (!U.configBundleImportName) return;
    const file = U.configBundleImportFile?.files?.[0];
    U.configBundleImportName.textContent = file ? String(file.name || "已选择") : "未选择文件";
}

function usesProjectBundlePassword() {
    return U.configBundleUseProjectPassword ? U.configBundleUseProjectPassword.checked !== false : true;
}

// 缺元素（未渲染/被裁掉）一律回默认值：配置在、记忆不在，与 HTML 的 checked 初始态一致。
function bundleScopeState(configElement, memoryElement) {
    const includeConfig = configElement ? configElement.checked !== false : true;
    const includeMemory = memoryElement ? memoryElement.checked === true : false;
    return { includeConfig, includeMemory };
}

function configBundleExportScopes() {
    return bundleScopeState(U.configBundleExportConfig, U.configBundleExportMemory);
}

function configBundleImportScopes() {
    return bundleScopeState(U.configBundleImportConfig, U.configBundleImportMemory);
}

function bundleScopeLabels(scopes) {
    return [scopes.includeConfig ? "配置" : "", scopes.includeMemory ? "记忆" : ""].filter(Boolean).join("、");
}

function bundleScopeSummary(item) {
    const memory = Number(item?.memory_entry_count || 0);
    if (!memory) return "";
    return `（配置 ${Number(item?.config_entry_count || 0)}、记忆 ${memory}）`;
}

function resetBundleScopeCheckboxes() {
    [U.configBundleExportConfig, U.configBundleImportConfig].forEach((element) => {
        if (element) element.checked = true;
    });
    [U.configBundleExportMemory, U.configBundleImportMemory].forEach((element) => {
        if (element) element.checked = false;
    });
}

function syncConfigBundlePasswordField() {
    const useProject = usesProjectBundlePassword();
    if (U.configBundleExportPasswordField) U.configBundleExportPasswordField.hidden = useProject;
    if (U.configBundleExportConfirmField) U.configBundleExportConfirmField.hidden = useProject;
}

function clearConfigBundleInputs() {
    [U.configBundleExportPassword, U.configBundleExportConfirm, U.configBundleImportPassword]
        .forEach((element) => {
            if (element) element.value = "";
        });
    if (U.configBundleImportFile) U.configBundleImportFile.value = "";
    if (U.configBundleUseProjectPassword) U.configBundleUseProjectPassword.checked = true;
    resetBundleScopeCheckboxes();
    syncConfigBundleFileName();
    syncConfigBundlePasswordField();
}

function openConfigBundleDialog() {
    if (!U.configBundleBackdrop) return;
    U.configBundleBackdrop.hidden = false;
    U.configBundleBackdrop.classList.add("is-open");
    syncConfigBundlePasswordField();
    window.requestAnimationFrame(() => U.configBundleDialog?.focus?.());
}

function closeConfigBundleDialog() {
    if (!U.configBundleBackdrop) return;
    U.configBundleBackdrop.hidden = true;
    U.configBundleBackdrop.classList.remove("is-open");
    clearConfigBundleInputs();
}

function downloadConfigBundleFile(filename) {
    triggerMediaDownload(ApiClient.getConfigBundleDownloadUrl(filename), filename);
}

async function submitConfigBundleExport() {
    const useProject = usesProjectBundlePassword();
    const scopes = configBundleExportScopes();
    const scopeLabels = bundleScopeLabels(scopes);
    if (!scopeLabels) {
        showToast({ title: "请先勾选要导出的范围", kind: "error" });
        return;
    }
    const password = useProject ? "" : String(U.configBundleExportPassword?.value || "");
    const passwordConfirm = String(U.configBundleExportConfirm?.value || "");
    if (!useProject) {
        if (!password) {
            showToast({ title: "请输入导出口令", kind: "error" });
            return;
        }
        if (password !== passwordConfirm) {
            showToast({ title: "两次输入的口令不一致", kind: "error" });
            return;
        }
    }
    setConfigBundleBusy(true);
    try {
        const item = await ApiClient.exportConfigBundle({
            password,
            useProjectPassword: useProject,
            includeConfig: scopes.includeConfig,
            includeMemory: scopes.includeMemory,
        });
        downloadConfigBundleFile(item?.filename);
        clearConfigBundleInputs();
        const counts = `共 ${item?.entry_count || 0} 个文件${bundleScopeSummary(item)}`;
        showToast({
            title: "配置包已生成",
            text: useProject
                ? `${counts}。下载已发起，取消保存则不会落盘；导入时输项目解锁密码。`
                : `${counts}。下载已发起，取消保存则不会落盘；口令不会随文件另存，丢了无法导入。`,
            kind: "success",
        });
    } catch (error) {
        showToast({ title: "导出失败", text: projectSettingsErrorText(error), kind: "error" });
    } finally {
        setConfigBundleBusy(false);
    }
}

function applyConfigBundleImportResult(item) {
    closeConfigBundleDialog();
    closeProjectSettingsDialog();
    const source = String(item?.source_app_version || "");
    const local = String(item?.local_app_version || "");
    const versionNote = source && local && source !== local ? `包来自 ${source}，本机 ${local}。` : "";
    showToast({
        title: "配置包已导入",
        text: `${versionNote}已还原 ${item?.entry_count || 0} 个文件${bundleScopeSummary(item)}，${item?.password_kept ? "解锁口令保持不变" : "解锁口令改成包口令"}；重启后 worker 才用新配置。`,
        kind: "success",
    });
    window.setTimeout(() => window.location.reload(), 1500);
}

async function submitConfigBundleImport() {
    const file = U.configBundleImportFile?.files?.[0];
    const password = String(U.configBundleImportPassword?.value || "");
    const scopes = configBundleImportScopes();
    const scopeLabels = bundleScopeLabels(scopes);
    if (!file) {
        showToast({ title: "请选择配置包文件", kind: "error" });
        return;
    }
    if (!scopeLabels) {
        showToast({ title: "请先勾选要导入的范围", kind: "error" });
        return;
    }
    if (!password) {
        showToast({ title: "请输入包口令", kind: "error" });
        return;
    }
    // 锁定态读不到在跑的工作，按"没有"处理：新机器上本来就没有。
    const snapshot = await ApiClient.getBootstrapExitCheck().catch(() => null);
    const hasRunning = !!snapshot?.has_running_work;
    const summary = String(snapshot?.summary_text || "").trim();
    // confirm 弹窗在层叠里排在配置包之后，必须先收掉本层才会露出来。
    closeConfigBundleDialog();
    closeProjectSettingsDialog();
    openConfirm({
        title: "确认导入配置包？",
        text: hasRunning
            ? `检测到${summary}。导入会整体替换${scopeLabels}，需要先暂停它们。`
            : `导入会整体替换当前${scopeLabels}；输入当前项目解锁口令，导入后仍用它解锁。`,
        confirmLabel: "导入配置包",
        confirmKind: "danger",
        // 本机口令是这台机器自己的秘密，包不带着它走：先验一遍证明来的人知道，
        // 再把包里的新主密钥重裹到这把口令下——导入因此不再改项目口令。
        // 只导记忆时这一步同样要验：往这台机写知识必须由知道本机口令的人发起。
        input: {
            label: "当前项目解锁口令",
            placeholder: "导入后继续用它解锁",
            required: true,
        },
        checkbox: hasRunning ? {
            checked: false,
            label: "暂停正在进行的所有对话和任务",
            hint: summary,
        } : null,
        returnFocus: U.projectSettings,
        onConfirm: async ({ checked, value }) => {
            if (hasRunning && !checked) {
                throw new Error("请先勾选“暂停正在进行的所有对话和任务”。");
            }
            const currentPassword = String(value || "").trim();
            if (!currentPassword) {
                throw new Error("请输入当前项目解锁口令。");
            }
            const item = await ApiClient.importConfigBundle(file, password, {
                confirmRunningWork: !!checked,
                currentPassword,
                includeConfig: scopes.includeConfig,
                includeMemory: scopes.includeMemory,
            });
            applyConfigBundleImportResult(item);
        },
    });
}

const GROUP_REF_PREFIX = "group:";
// 新建组弹窗里的默认值；配置侧不填时后端按 1 处理。上限刻意不设。
const GROUP_DEFAULT_MAX_RETRY_ROUNDS = 3;
// 与后端 LOAD_BALANCE_ROUTE_SCOPES 同步：只有这两条车道能引用组。
const LOAD_BALANCE_ROUTE_SCOPES = ["execution", "inspection"];

const groupRefToken = (key) => `${GROUP_REF_PREFIX}${String(key || "").trim()}`;
const isGroupRef = (ref) => String(ref || "").startsWith(GROUP_REF_PREFIX);
const groupKeyFromRef = (ref) => String(ref || "").slice(GROUP_REF_PREFIX.length).trim();

// 链 → 后端 route_entries：`group:x` 记号翻译成组引用，其余是 direct model。
// 压缩尺子按"这一跳可能被达到的成员里最小那颗窗口"取（`chat_backend._hop_context_window_tokens`），
// 所以组里加一颗小窗口模型会压低整组的压缩目标。配置面必须把这颗成员点出来，否则这层
// 连带影响只有出事那天才看得见。
// 组弹窗里那一行「组内最大上下文对齐」：整组能用的上限是对齐后的那颗最小窗口。卡片（链上
// 组卡、组列卡）不显示这行——位置留给成员与链状态，窗口口径只在正改成员的那一刻要看。
// 链上「一跳是一个组」的那张卡。模型页的角色链由 org_graph_llm.js 渲染（它覆盖了
// window.renderModelRoleEditors），所以这张卡由两边共用，不要再各写一份。
// 组的增删改只走这一个弹窗：链与列表负责展示、拖入和打开配置，不再就地编辑。
// 只同步计数与错误行：整块重绘会把输入框光标和复选框焦点踢掉。
// ---- 上下文信息更新时间（空闲刷新边界）---------------------------------------
// 提交整份组集合，而不是只提交被链引用的那份：配置侧是按 scope 整份替换
// `models.loadBalanceGroups` 的，交子集会把没在这条链上用到的组（包括刚建好还没拖进链的）
// 一起删掉。成员为空的组不提交——后端会拒，而它也没被任何链引用。
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
    const previousId = String(previousSessionId || "").trim();
    const nextId = String(nextSessionId || "").trim();
    // 会话没换就不是"换会话"：`ceo.sessions.patch` / `ceo.sessions.snapshot` 在回合运行中
    // 会随每一帧状态反复到达（同一 session_id），这里若无条件当切换处理，操作者刚点开的
    // 模型与上下文面板会在下一次补丁时被关掉、面板里未保存的模型链草稿也一起丢掉。
    // 真正切换会话时两个 id 必然不同，仍走下面的关闭 + 重读。
    if (previousId && previousId === nextId) return false;
    closeCeoModelModePanel();
    resetCeoModelSelection(nextId);
    void refreshCeoModelSelection(nextId);
    return switchCeoComposerDraft(previousId, nextId);
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

// ---- 会话卡片拖动排序（照 CEO 模型链那一套：容器代理 + 垂直中点位次）------

function ceoSessionDropIndex(list, clientY) {
    const cards = [...list.querySelectorAll("[data-ceo-session-index]")];
    for (const card of cards) {
        const rect = card.getBoundingClientRect();
        if (clientY < rect.top + (rect.height / 2)) return Number(card.dataset.ceoSessionIndex);
    }
    return cards.length;
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
        if (payload?.identity_hidden) {
            showToast({ title: "已隐藏", text: "该渠道 token 还在，对方再次发消息时会话会回到列表。", kind: "success" });
        } else if (payload?.identity_forgotten) {
            showToast({ title: "已删除", text: "该渠道的 token 已不存在，外部身份一并注销。", kind: "success" });
        }
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
    const isChannel = isChannelSessionItem(current);
    // 渠道会话的「删除」按该桥的 token 条目分流，读数由目录带下来（判据在服务端一处）。
    const hidesOnDelete = isChannel && current?.hides_on_delete === true;
    openConfirm({
        title: isChannel ? (hidesOnDelete ? "隐藏渠道会话" : "删除渠道会话") : "删除会话",
        text: isChannel
            ? (hidesOnDelete
                ? `将清空渠道会话“${current.title || current.session_id}”的上下文并从列表隐藏，对方再次发消息时它会回来。`
                : `将删除渠道会话“${current.title || current.session_id}”的上下文与附件，卡片不会回到列表。`)
            : `将删除会话“${current.title || current.session_id}”（${shortSessionIdLabel(current.session_id)}）的聊天记录与附件。`,
        confirmLabel: isChannel && hidesOnDelete ? "隐藏" : "删除",
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
        // S.ceoSessionBusy 的两个解除点都依赖服务端产帧（snapshot.ceo / onclose），
        // 而这条车道已观测到静默不产出。忙标志挂住会把编辑/Fork 的点击整个吃掉
        // （ceoHistoryEditBusyReason），表现为"等一会儿才能点"。再挂一个不依赖对端的兜底。
        window.setTimeout(() => {
            if (token !== S.ceoWsToken || S.ceoWs !== socket) return;
            if (!S.ceoSessionBusy) return;
            S.ceoSessionBusy = false;
            renderCeoSessions();
            syncCeoPrimaryButton();
        }, CEO_SNAPSHOT_BUSY_GUARD_MS);
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
        dispatchCeoWsFrame(payload, effectiveSessionId);
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
    if (!activeSessionCanMessage()) return;
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
            // 默认只留在浏览器队列里，等本轮最终输出后由 maybeDispatchQueuedCeoFollowUps
            // 按顺序起新回合；要现在就并进正在跑的这一轮，点条目上的"立即发送"。
            enqueueCeoFollowUp(activeSessionId(), { text, uploads });
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
// 与后端 clear_task_temp_files 的门槛逐字一致：只认终态，不含 paused
// （paused 任务可能被 resume，临时目录仍是工具默认落点）。
const canClearTemp = (task) => !!task && ["success", "failed"].includes(pStatus(task.status));
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
        call_kind: String(source.call_kind || "").trim(),
        prepared_message_count: toInt(source.prepared_message_count),
        prepared_message_chars: toInt(source.prepared_message_chars),
        response_tool_call_count: toInt(source.response_tool_call_count),
        duration_ms: toOptionalInt(source.duration_ms),
        first_token_ms: toOptionalInt(source.first_token_ms),
        thinking_tokens: toOptionalInt(source.thinking_tokens),
        stream_incomplete: !!source.stream_incomplete,
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



function syncActionButton(button, { idleLabel, busyLabel, busy = false, disabled = false } = {}) {
    if (!button) return;
    button.textContent = busy ? (busyLabel || idleLabel || button.textContent || "") : (idleLabel || button.textContent || "");
    button.disabled = !!disabled;
}

function renderSkillActions() {
    syncActionButton(U.skillRefresh, {
        idleLabel: "刷新",
        busyLabel: "刷新中...",
        busy: ResourcesState.skillBusy,
        disabled: ResourcesState.skillBusy,
    });
    syncActionButton(U.skillSave, {
        idleLabel: "保存",
        busyLabel: "保存中...",
        busy: ResourcesState.skillBusy,
        disabled: ResourcesState.skillBusy || !ResourcesState.selectedSkill || !ResourcesState.skillDirty,
    });
    const deleteButton = U.skillDetail?.querySelector("#skill-delete-btn");
    if (deleteButton) {
        deleteButton.textContent = ResourcesState.skillBusy ? "删除中..." : "删除";
        deleteButton.disabled = ResourcesState.skillBusy || !ResourcesState.selectedSkill;
    }
    const toggleButton = U.skillDetail?.querySelector(ResourcesState.selectedSkill?.enabled ? "#skill-disable-btn" : "#skill-enable-btn");
    if (toggleButton) toggleButton.disabled = ResourcesState.skillBusy || !ResourcesState.selectedSkill;
    syncDetailSaveButton("skill");
}

function renderToolActions() {
    syncActionButton(U.toolRefresh, {
        idleLabel: "刷新",
        busyLabel: "刷新中...",
        busy: ResourcesState.toolBusy,
        disabled: ResourcesState.toolBusy,
    });
    syncActionButton(U.toolSave, {
        idleLabel: "保存",
        busyLabel: "保存中...",
        busy: ResourcesState.toolBusy,
        disabled: ResourcesState.toolBusy || !ResourcesState.selectedTool || !ResourcesState.toolDirty,
    });
    const deleteButton = U.toolDetail?.querySelector("#tool-delete-btn");
    if (deleteButton) {
        if (ResourcesState.selectedTool?.is_core) {
            deleteButton.textContent = "核心工具不可删除";
            deleteButton.disabled = true;
        } else {
            deleteButton.textContent = ResourcesState.toolBusy ? "删除中..." : "删除";
            deleteButton.disabled = ResourcesState.toolBusy || !ResourcesState.selectedTool;
        }
    }
    const toggleButton = U.toolDetail?.querySelector(ResourcesState.selectedTool?.enabled ? "#tool-disable-btn" : "#tool-enable-btn");
    if (toggleButton) {
        toggleButton.disabled = ResourcesState.toolBusy || !ResourcesState.selectedTool;
    }
    syncDetailSaveButton("tool");
}


function clearSkillSelection() {
    if (ResourcesState.skillAutosaveTimerId) {
        window.clearTimeout(ResourcesState.skillAutosaveTimerId);
        ResourcesState.skillAutosaveTimerId = null;
    }
    ResourcesState.skillAutosavePending = false;
    ResourcesState.selectedSkill = null;
    ResourcesState.skillFiles = [];
    ResourcesState.skillContents = {};
    ResourcesState.skillFileLoads = {};
    ResourcesState.selectedSkillFile = "";
    ResourcesState.skillDirty = false;
    renderSkills();
    renderSkillDetail();
}

function clearToolSelection() {
    if (ResourcesState.toolAutosaveTimerId) {
        window.clearTimeout(ResourcesState.toolAutosaveTimerId);
        ResourcesState.toolAutosaveTimerId = null;
    }
    ResourcesState.toolAutosavePending = false;
    ResourcesState.selectedTool = null;
    ResourcesState.toolDirty = false;
    renderTools();
    renderToolDetail();
}

// 记忆三面（队列 / 已处理批次 / 失败停车区）的正文都只投首屏，取全文按字符一屏一屏点。
// 整份灌进正文节点的成本是排版：实盘一条 1.28MB 载荷让"点开一张卡片"占住主线程 2.9 秒。
const MEMORY_OP_KINDS = {
    add: { label: "增加", icon: "plus" },
    rewrite: { label: "修改", icon: "pencil" },
    delete: { label: "删除", icon: "trash-2" },
    note_upsert: { label: "更新 Note", icon: "notebook-pen" },
    note_edit: { label: "编辑 Note", icon: "notebook-pen" },
    note_reaped: { label: "回收 Note", icon: "trash-2" },
    category_note: { label: "归并记忆", icon: "notebook-pen" },
    consolidate: { label: "记忆退场", icon: "pencil" },
    category_purge: { label: "摘除分类导航", icon: "trash-2" },
    compress_for_capacity: { label: "容量降级", icon: "arrow-down-wide-narrow" },
};

// write_mode 把删除并进 rewrite 桶、把多种操作压成 mixed；只有持久化的 changes
// 逐笔记录了操作类型与顺序，因此它是显示的第一来源。
// 模型链这一行读的是这一批真实走过的 ref，末位就是最后一次答话的那颗：长度 > 1 即
// 链上降级过。整条配置链看不出这件事，旧记录没有途经账时显示 "-"，不拿配置链冒充。
const NOTE_REF_RE = /(?:\bref:|见noteid:)(note_[a-z0-9_]+)\b/g;
// note 正文里指回别的 note 的两种形状：旧标记 `ref:note_xxx`，以及常驻行同款句柄形状
// `note:n0bfc7` / `(note: n0bfc7)`。归并记忆浏览层用它在正文里画出"下一级"。
const NOTE_TOKEN_RE = /(?:\bref:|见noteid:)(note_[a-z0-9_]+)\b|\(\s*note:\s*([nc][0-9a-f]{5})\s*\)|\bnote:([nc][0-9a-f]{5})\b/g;
const MEMORY_VIEW_POLL_MS = 15000;

/* --- 记忆变更 diff 高亮：对"修改后"内容定位变化点 --- */
const MEMORY_CHANGE_DIFF_MAX_TOKENS = 1000;

const MEMORY_NOTE_REAP_REASONS = {
    memory_deleted: "拥有它的记忆被删除",
    ref_dropped: "改写后不再指向它",
    unreferenced: "没有记忆指向它",
};

// 轮询刷新会重渲染详情/列表；直接写 innerHTML 会把滚动条重置到顶部。
// 内容未变化时跳过重渲染，变化时保留原滚动位置（超出新内容高度时由浏览器钳制）。
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

const AUDIT_EVENT_EXPAND_SELECTOR = "details.audit-event-expand";

function renderAuditEventCard(item = {}, { open = false } = {}) {
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
        detailHtml = `<details class="audit-event-expand"${open ? " open" : ""}><summary>详情</summary><pre class="audit-event-detail">${esc(pretty)}</pre></details>`;
    }
    return `<article class="audit-log-line ${auditEventLevelClass(level)}" data-audit-event-id="${esc(String(item.event_id || ""))}">
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
    // 展开态必须跟着 event_id 走：整表 innerHTML 重建会把已展开的「详情」收起，
    // 而一份 detail JSON 能有几万像素高，收起瞬间会把下方内容整体上提，
    // 数字 scrollTop 没变、眼里却是跳了一屏。
    const openEventIds = new Set(
        Array.from(U.auditEventList.querySelectorAll(`${AUDIT_EVENT_EXPAND_SELECTOR}[open]`))
            .map((node) => String(node.closest?.("[data-audit-event-id]")?.dataset.auditEventId || "").trim())
            .filter(Boolean)
    );
    U.auditEventList.innerHTML = items.map((item) => renderAuditEventCard(item, {
        open: openEventIds.has(String(item.event_id || "").trim()),
    })).join("");
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
        // 版本红点搭同一拍：它读的是服务端台账（本地文件），不额外产生外网请求。
        void refreshUpdateStatus();
    }, AUDIT_BADGE_POLL_MS);
}

function markAuditRead() {
    const latest = S.auditLatestEventTs || "";
    writeSessionJson(AUDIT_LAST_SEEN_KEY, { lastSeen: latest });
    renderAuditNavBadge(0);
}

// 新版本提醒：红点只跟台账里的 `newer` 走。"从未检查"与"检查失败"都不亮 ——
// 把未知渲染成"已是最新"或凭空亮点，都会让用户无法区分这两种状态。
function renderUpdateNavDot(item) {
    if (!U.updateNavDot) return;
    U.updateNavDot.hidden = !Boolean(item?.newer);
}

function formatUpdateCheckedAt(value) {
    const raw = String(value || "").trim();
    if (!raw) return "从未";
    const stamp = new Date(raw);
    if (Number.isNaN(stamp.getTime())) return "未知";
    const month = String(stamp.getMonth() + 1).padStart(2, "0");
    const day = String(stamp.getDate()).padStart(2, "0");
    const time = stamp.toLocaleTimeString("zh-CN", { hour12: false, hour: "2-digit", minute: "2-digit" });
    return `${month}-${day} ${time}`;
}

const UPDATE_APPLY_OUTCOME_TEXT = {
    started: "升级执行中",
    upgrade_failed: "升级未完成：安装脚本非零退出",
    exit_refused: "升级未完成：服务拒绝退出",
    port_busy: "升级未完成：服务没退干净",
    port_unknown: "升级未完成：端口未知",
    not_this_service: "升级未完成：端口上没有本服务",
};

function updateApplyOutcomeText(item) {
    // apply 端点回的是"已踢起执行体"，不是"升级成功"。执行体把终态写进结果文件，
    // 这里照实说，否则失败时界面永远停在"正在重启"。ok 与没有记录都不占这一行。
    const apply = item?.last_apply;
    const outcome = apply?.outcome;
    if (!outcome || outcome === "ok") return "";
    if (outcome === "started") {
        const stage = UPDATE_APPLY_STAGE_TEXT[String(apply?.stage || "").trim()] || "";
        return stage ? `升级执行中：${stage}` : "升级执行中";
    }
    return UPDATE_APPLY_OUTCOME_TEXT[outcome] || `升级未完成：${outcome}`;
}

function updateSettingsLineText(item) {
    // 一行只放版本对：整行可用宽 412px，两个控件占 78+91+12，带上检查时间就要
    // 443px 必然换行。时间、错误码这些细节走 title。
    const outcomeText = updateApplyOutcomeText(item);
    if (outcomeText) return outcomeText;
    if (!item?.has_ledger) return "尚未检查过版本";
    if (item.error) return "上次检查失败";
    if (item.newer) return `当前 v${item.current_version} · 最新 ${item.latest_tag}`;
    return `当前 v${item.current_version} · 已是最新`;
}

function updateSettingsLineTitle(item) {
    if (!item?.has_ledger) return "还没查过远端标签；点「检查更新」立即查一次。";
    const checked = `检查于 ${formatUpdateCheckedAt(item.checked_at)}`;
    const apply = item?.last_apply;
    const applyNote = apply && apply.outcome && apply.outcome !== "ok"
        ? ` · 上次升级 ${apply.outcome} ${formatUpdateCheckedAt(apply.at)}${apply.detail ? `：${apply.detail}` : ""}`
        : "";
    if (item.error) return `${item.error} · ${checked}${applyNote}`;
    return `${item.latest_tag || "无标签"} · ${checked}${applyNote}`;
}

function renderProjectSettingsUpdate(item) {
    if (U.projectSettingsUpdateText) {
        U.projectSettingsUpdateText.textContent = updateSettingsLineText(item);
        U.projectSettingsUpdateText.title = updateSettingsLineTitle(item);
    }
    if (U.projectSettingsApplyUpdate) {
        U.projectSettingsApplyUpdate.hidden = !Boolean(item?.newer);
    }
}

async function refreshUpdateStatus() {
    try {
        const item = await ApiClient.getUpdateStatus();
        S.updateStatus = item || null;
    } catch {
        return;
    }
    renderUpdateNavDot(S.updateStatus);
    if (U.projectSettingsBackdrop && !U.projectSettingsBackdrop.hidden) {
        renderProjectSettingsUpdate(S.updateStatus);
    }
}

async function checkForUpdatesNow() {
    if (!U.projectSettingsCheckUpdate) return;
    U.projectSettingsCheckUpdate.disabled = true;
    try {
        const item = await ApiClient.checkUpdateNow();
        S.updateStatus = item || null;
        renderUpdateNavDot(item);
        renderProjectSettingsUpdate(item);
        if (item?.newer) {
            showToast({ title: "发现新版本", text: `${item.latest_tag}（当前 v${item.current_version}）`, kind: "info" });
        } else if (item?.error) {
            showToast({ title: "检查失败", text: "读不到远端标签，稍后再试。", kind: "warn" });
        }
    } catch (error) {
        showToast({ title: "检查失败", text: String(error?.message || error), kind: "error" });
    } finally {
        U.projectSettingsCheckUpdate.disabled = false;
    }
}

const UPDATE_APPLY_WATCH_INTERVAL_MS = 3000;
const UPDATE_APPLY_WATCH_TIMEOUT_MS = 11 * 60 * 1000;
let updateApplyWatchTimer = null;

function updateApplyTargetVersion(ref) {
    return String(ref || "").replace(/^v/, "").trim();
}

function stopUpdateApplyWatch() {
    if (updateApplyWatchTimer) {
        clearInterval(updateApplyWatchTimer);
        updateApplyWatchTimer = null;
    }
}

const UPDATE_APPLY_STAGE_TEXT = {
    requested: "已排入更新队列",
    requesting_exit: "正在暂停进行中的对话与任务",
    waiting_exit: "等待服务退出",
    upgrading: "正在下载新版本并同步依赖",
    relaunching: "正在重新拉起服务",
    ok: "更新完成",
};

function updateApplyProgressText(apply, waitedSeconds) {
    // 执行体在服务停着的时段里仍会续写阶段记录，所以这里读到的可能是几秒前的
    // 阶段；秒数是页面自己从点击那刻起的。一个概念只放一个带标签的数：进度用
    // 页面的计时，执行体那句"已 42s"的 detail 不再叠上来。
    const stage = String(apply?.stage || "").trim();
    const label = UPDATE_APPLY_STAGE_TEXT[stage] || "正在更新";
    const seconds = Math.max(0, Math.floor(Number(waitedSeconds) || 0));
    return `${label} · 已等 ${seconds} 秒`;
}

function watchApplyUpdateFinish(targetVersion, { intervalMs = UPDATE_APPLY_WATCH_INTERVAL_MS, timeoutMs = UPDATE_APPLY_WATCH_TIMEOUT_MS } = {}) {
    // 确认框那句"服务回来本页会自动恢复"要真的做到：目标版本回来就自己刷新一次。
    // 停机期间 getUpdateStatus 会直接抛，吞掉继续等即可。
    const deadline = Date.now() + timeoutMs;
    const startedAt = Date.now();
    let lastApply = null;
    stopUpdateApplyWatch();
    showToast({ title: `更新到 v${targetVersion}`, text: updateApplyProgressText(null, 0), kind: "info", persistent: true, spin: true });
    updateApplyWatchTimer = setInterval(async () => {
        const waited = (Date.now() - startedAt) / 1000;
        if (Date.now() > deadline) {
            stopUpdateApplyWatch();
            showToast({ title: "更新未完成", text: `服务没在预期时间内回到 v${targetVersion}，请看 .g3ku/logs/update-apply.log。`, kind: "error", persistent: true });
            return;
        }
        let item = null;
        try {
            item = await ApiClient.getUpdateStatus();
        } catch {
            // 服务正在停机窗口里：阶段读数停在最后一次成功获取的那一步。
            showToast({ title: `更新到 v${targetVersion}`, text: updateApplyProgressText(lastApply, waited), kind: "info", persistent: true, spin: true });
            return;
        }
        if (!item) return;
        lastApply = item.last_apply || lastApply;
        const outcome = String(item.last_apply?.outcome || "");
        if (outcome && outcome !== "ok" && outcome !== "started") {
            stopUpdateApplyWatch();
            const stageLabel = UPDATE_APPLY_STAGE_TEXT[String(item.last_apply?.stage || "")] || "";
            showToast({
                title: "更新未完成",
                text: `${item.last_apply?.detail || `执行体停在 ${outcome}`}${stageLabel ? ` · 停在${stageLabel}` : ""}`,
                kind: "error",
                persistent: true,
            });
            void refreshUpdateStatus();
            return;
        }
        if (outcome === "ok" && updateApplyTargetVersion(item.current_version) === targetVersion) {
            stopUpdateApplyWatch();
            showToast({ title: `已更新到 v${targetVersion}`, text: "正在刷新页面。", kind: "success" });
            window.location.reload();
            // reload() 立刻返回：不 return 的话下面那句进度 toast 会把成功提示盖掉。
            return;
        }
        showToast({ title: `更新到 v${targetVersion}`, text: updateApplyProgressText(item.last_apply, waited), kind: "info", persistent: true, spin: true });
    }, intervalMs);
}

function requestApplyUpdate() {
    const item = S.updateStatus || {};
    openConfirm({
        title: `重启并更新到 ${item.latest_tag || "新版本"}`,
        text: "服务会先暂停正在进行的对话与任务，更新完成后自动重启。",
        confirmLabel: "重启并更新",
        confirmKind: "danger",
        onConfirm: async () => {
            try {
                const result = await ApiClient.applyUpdate({ ref: item.latest_tag || "", pause_running_work: true });
                const target = updateApplyTargetVersion(result?.ref || item.latest_tag);
                showToast({ title: "正在重启", text: "服务回来本页会自动恢复。", kind: "info", persistent: true });
                if (target) watchApplyUpdateFinish(target);
            } catch (error) {
                showToast({ title: "更新未启动", text: String(error?.message || error), kind: "error" });
            }
        },
    });
}

function bindUpdateNotice() {
    U.projectSettingsCheckUpdate?.addEventListener("click", () => void checkForUpdatesNow());
    U.projectSettingsApplyUpdate?.addEventListener("click", () => requestApplyUpdate());
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

// 队列卡片正文固定显示两行（.memory-card-queue .memory-card-body 的 line-clamp），
// 这里的长度只是进 DOM 的上限，截断出的省略号落在两行之外。
const MEMORY_QUEUE_BODY_CHARS = 220;

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

const THEME_KEY = "g3ku.ui.theme.v1";

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

const CEO_SESSION_ORDER_KEY = "g3ku.ui.ceo.session-order.v1";

const CEO_CHANNEL_COLLAPSE_KEY = "g3ku.ui.ceo.channel-collapse.v1";

function readStoredCeoChannelCollapse() {
    try {
        const parsed = JSON.parse(readStoredUiPreference(CEO_CHANNEL_COLLAPSE_KEY) || "[]");
        if (!Array.isArray(parsed)) return [];
        return [...new Set(parsed.map((id) => String(id || "").trim()).filter(Boolean))];
    } catch (error) {
        return [];
    }
}

function isCeoChannelCollapsed(channelId) {
    const key = String(channelId || "").trim();
    if (!key) return false;
    return (Array.isArray(S.ceoChannelCollapsed) ? S.ceoChannelCollapsed : []).includes(key);
}

function ceoChannelGroupSessionIds(channelId) {
    const key = String(channelId || "").trim();
    const ids = (Array.isArray(S.ceoChannelGroups) ? S.ceoChannelGroups : [])
        .filter((group) => String(group?.channel_id || "").trim() === key)
        .flatMap((group) => (Array.isArray(group?.items) ? group.items : []))
        .map((item) => String(item?.session_id || "").trim())
        .filter(Boolean);
    return new Set(ids);
}

function toggleCeoChannelCollapse(channelId) {
    const key = String(channelId || "").trim();
    if (!key) return false;
    const wasCollapsed = isCeoChannelCollapsed(key);
    const nextCollapsed = wasCollapsed
        ? S.ceoChannelCollapsed.filter((id) => id !== key)
        : [...(Array.isArray(S.ceoChannelCollapsed) ? S.ceoChannelCollapsed : []), key];
    S.ceoChannelCollapsed = nextCollapsed;
    writeStoredUiPreference(CEO_CHANNEL_COLLAPSE_KEY, JSON.stringify(nextCollapsed));
    if (!wasCollapsed && S.ceoSelectedSessionIds instanceof Set) {
        // 折叠把行从界面上拿走，多选作用集跟着退出，否则「删除 N 个」会数到看不见的会话。
        const hiddenIds = ceoChannelGroupSessionIds(key);
        S.ceoSelectedSessionIds = new Set([...S.ceoSelectedSessionIds].filter((id) => !hiddenIds.has(id)));
    }
    renderCeoSessions();
    return !wasCollapsed;
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
    const theme = readStoredUiPreference(THEME_KEY) === "dark" ? "dark" : "light";
    document.documentElement.setAttribute("data-theme", theme);
    syncThemeToggleIcons();
    updateThemeToggleA11y();
}

const READING_SCALE_KEY = "g3ku.ui.reading-scale.v1";
const READING_SCALE_DEFAULT = 1.1;
const READING_SCALE_MIN = 0.9;
const READING_SCALE_MAX = 1.3;
const READING_SCALE_STEP = 0.05;

// 字号档位只写 --reading-font-scale：CSS 里乘在正文/回复/代码块/笔记正文的字号上，
// 控件字号与间距不跟，所以它不同于窗口缩放（桌面壳的 ZoomFactor）。
function normalizeReadingScale(raw) {
    const text = String(raw ?? "").trim();
    // 空串要当"没设过"：下拉框没有匹配项时读出来就是空串，而 Number("") 是 0。
    if (!text) return READING_SCALE_DEFAULT;
    const value = Number(text);
    if (!Number.isFinite(value)) return READING_SCALE_DEFAULT;
    const clamped = Math.min(READING_SCALE_MAX, Math.max(READING_SCALE_MIN, value));
    const snapped = Math.round(clamped / READING_SCALE_STEP) * READING_SCALE_STEP;
    return Math.round(snapped * 100) / 100;
}

function applyReadingScale(raw) {
    const scale = normalizeReadingScale(raw);
    if (document.documentElement?.style?.setProperty) {
        document.documentElement.style.setProperty("--reading-font-scale", String(scale));
    }
    if (U.readingScale) U.readingScale.value = String(scale);
    return scale;
}

function initializeReadingScale() {
    const stored = readStoredUiPreference(READING_SCALE_KEY);
    applyReadingScale(stored ? stored : READING_SCALE_DEFAULT);
}

function bindReadingScale() {
    U.readingScale?.addEventListener("change", () => {
        const scale = applyReadingScale(U.readingScale.value);
        writeStoredUiPreference(READING_SCALE_KEY, String(scale));
    });
}

function initializeUiPreferences() {
    // 本模块会被 tests/resources 的 vm 桩环境直接求值，那里没有 documentElement。
    if (!document.documentElement) return;
    initializeTheme();
    initializeReadingScale();
    S.ceoSessionOrder = readStoredCeoSessionOrder();
    S.ceoChannelCollapsed = readStoredCeoChannelCollapse();
}

function bind() {
    bindProjectSettings();
    bindCeoFeedAndNav();
    bindMemoryAndAudit();
    bindTaskDetailLanes();
    bindCeoSessionsPanel();
    bindCeoComposerAndFeedActions();
    bindModelPage();
    bindTaskHall();
    bindResourcePages();
    bindMediaViewer();
    bindGlobalOverlays();
    renderPendingCeoUploads();
    syncCeoInputHeight();
    syncCeoComposerReadonlyState();
    renderCeoSessions();
    syncCeoSessionPanelState();
    renderTaskSessionScope();
    syncCeoPrimaryButton();
}

function bindProjectSettings() {
    U.theme?.addEventListener("click", toggleTheme);
    bindModelRetryToastExpansion();
    bindReadingScale();
    U.projectSettings?.addEventListener("click", () => openProjectSettingsDialog());
    U.projectSettingsClose?.addEventListener("click", () => closeProjectSettingsDialog());
    U.projectSettingsBackdrop?.addEventListener("click", (e) => {
        if (e.target === U.projectSettingsBackdrop) closeProjectSettingsDialog();
    });
    U.projectSettingsOpenPassword?.addEventListener("click", () => openPasswordChangeDialog());
    U.projectSettingsOpenBundle?.addEventListener("click", () => openConfigBundleDialog());
    U.configBundleClose?.addEventListener("click", () => closeConfigBundleDialog());
    U.configBundleBackdrop?.addEventListener("click", (e) => {
        if (e.target === U.configBundleBackdrop) closeConfigBundleDialog();
    });
    U.configBundleExport?.addEventListener("click", () => void submitConfigBundleExport());
    U.configBundleUseProjectPassword?.addEventListener("change", () => syncConfigBundlePasswordField());
    U.configBundleImport?.addEventListener("click", () => void submitConfigBundleImport());
    U.configBundleImportPick?.addEventListener("click", () => U.configBundleImportFile?.click());
    U.configBundleImportFile?.addEventListener("change", () => syncConfigBundleFileName());
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
}


function bindCeoFeedAndNav() {
    U.ceoFeed?.addEventListener("scroll", handleCeoFeedScrollEvent, { passive: true });
    ["wheel", "pointerdown", "keydown", "touchstart"].forEach((type) => {
        U.ceoFeed?.addEventListener(type, handleCeoFeedUserGesture, { passive: true });
    });
    U.ceoScrollToLatestBtn?.addEventListener("click", () => scrollCeoFeedToBottom());
    updateCeoScrollToLatestButton();
    U.nav.forEach((btn) => btn.addEventListener("click", () => switchView(btn.dataset.view)));
    U.backToTasks?.addEventListener("click", () => switchView("tasks"));
}


function bindMemoryAndAudit() {
    U.memoryRefresh?.addEventListener("click", () => void loadMemoryView({ force: true }));
    U.memoryViewCurrent?.addEventListener("click", () => void openMemoryBrowser());
    U.memoryIndexNotes?.addEventListener("click", () => void openMemoryNoteBrowser());
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
}


function bindTaskDetailLanes() {
    U.taskTokenButton?.addEventListener("click", () => setTaskTokenStatsOpen(true));
    U.taskTokenClose?.addEventListener("click", () => setTaskTokenStatsOpen(false));
    U.taskTokenBackdrop?.addEventListener("click", () => setTaskTokenStatsOpen(false));
    U.taskTokenContent?.addEventListener("click", (e) => {
        const target = e.target instanceof Element ? e.target : null;
        if (!target) return;
        const control = target.closest("[data-task-model-call-page]");
        if (control) {
            const direction = String(control.dataset.taskModelCallPage || "").trim();
            const current = Number(S.taskModelCallsPage || 1) || 1;
            if (direction === "prev") setTaskModelCallsPage(current - 1);
            if (direction === "next") setTaskModelCallsPage(current + 1);
            return;
        }
        // 翻页是"点了才取"：窗口内的页本地切片，越过窗口的那一跳走按页取数口。
        if (target.closest("[data-task-model-call-goto]")) {
            const box = U.taskTokenContent?.querySelector?.("[data-task-model-call-jump]");
            const value = Number(box?.value || 0);
            if (value >= 1) setTaskModelCallsPage(value);
            return;
        }
        if (target.closest("[data-task-model-call-live]")) {
            exitTaskModelCallHistory();
            return;
        }
        if (target.closest("[data-task-model-call-refresh]")) {
            // 快照态下「刷新」= 按最新账本重算页号并重取本页，不能把用户弹回最新。
            if (isTaskModelCallHistorical()) void loadTaskModelCallPage(Number(S.taskModelCallsPage || 1), { rebase: true });
            else void refreshTaskTokenLedger();
            return;
        }
    });
    // 搜索输入走事件委托：只重建表格区域，输入框本身不销毁，焦点与内容不丢失。
    // 搜索作用于全部记录（而非当前页），输入即筛选并回到第 1 页；快照态只搜已加载那一页、不动页号。
    U.taskTokenContent?.addEventListener("input", (e) => {
        const input = e.target instanceof Element ? e.target.closest("[data-task-model-call-search]") : null;
        if (!input) return;
        S.taskModelCallsQuery = input.value;
        if (!isTaskModelCallHistorical()) S.taskModelCallsPage = 1;
        refreshTaskTokenCallTable();
    });
    U.nodeContextDisclosure?.addEventListener("toggle", () => void handleNodeContextDisclosureToggle());
}


function bindCeoSessionsPanel() {
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
        const collapse = e.target.closest("[data-channel-collapse]");
        if (collapse) {
            e.stopPropagation();
            toggleCeoChannelCollapse(collapse.dataset.channelCollapse);
            return;
        }
        const copyId = e.target.closest("[data-session-copy-id]");
        if (copyId) {
            e.stopPropagation();
            void copyCeoSessionId(copyId);
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
}


function bindCeoComposerAndFeedActions() {
    U.ceoSend?.addEventListener("click", handleCeoPrimaryAction);
    U.ceoAttach?.addEventListener("click", () => {
        if (S.ceoUploadBusy) return;
        U.ceoFileInput?.click();
    });
    U.ceoFileInput?.addEventListener("change", (e) => void handleCeoFileSelection(e));
    U.ceoVoiceBtn?.addEventListener("click", handleCeoVoiceClick);
    U.ceoVoiceAutoSend?.addEventListener("change", (e) => {
        setCeoVoiceAutoSend(Boolean(e?.target?.checked));
    });
    restoreCeoVoiceAutoSend();
    bindCeoModelModeControls();
    U.ceoUploadList?.addEventListener("click", (e) => {
        const remove = e.target.closest("[data-upload-remove]");
        if (!remove) return;
        removePendingCeoUpload(Number(remove.dataset.uploadRemove));
    });
    U.ceoFollowUpQueue?.addEventListener("click", (e) => {
        const expand = e.target.closest("[data-follow-up-expand]");
        if (expand) {
            toggleCeoQueuedFollowUpExpansion(String(expand.dataset.followUpExpand || ""));
            return;
        }
        const flush = e.target.closest("[data-follow-up-flush]");
        if (flush) {
            flushCeoQueuedFollowUp(String(flush.dataset.followUpFlush || ""));
            return;
        }
        const withdraw = e.target.closest("[data-follow-up-withdraw]");
        if (withdraw) {
            void withdrawCeoQueuedFollowUp(String(withdraw.dataset.followUpWithdraw || ""));
            return;
        }
        const remove = e.target.closest("[data-follow-up-remove]");
        if (!remove) return;
        void discardCeoQueuedFollowUp(String(remove.dataset.followUpRemove || ""));
    });
    U.ceoFeed?.addEventListener("click", (e) => {
        handleCeoVoiceBubbleClick(e);
        const copyBtn = e.target.closest("[data-ceo-copy]");
        if (copyBtn) {
            e.preventDefault();
            e.stopPropagation();
            void copyCeoBubbleContent(copyBtn);
            return;
        }
        const pauseCompressionBtn = e.target.closest("[data-ceo-compress-pause]");
        if (pauseCompressionBtn) {
            e.preventDefault();
            e.stopPropagation();
            requestCeoContextCompressionPause();
            return;
        }
        const retryCompressionBtn = e.target.closest("[data-ceo-compress-retry]");
        if (retryCompressionBtn) {
            e.preventDefault();
            e.stopPropagation();
            // 与长按脑图标同一条发起路径、同一道闸门、同一句拒绝文案；这里不再加二次确认：
            // 这条线是会话里最后一条线才存在，有回合在跑时它就不是最后一条了。
            const blocked = ceoBrainHoldBlockedReason();
            if (blocked) {
                showToast({ title: "暂不能压缩上下文", text: blocked, kind: "warn" });
                return;
            }
            void beginCeoContextCompression(activeSessionId());
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
            return;
        }
        // 落到消息正文上的点击：展开/收起这一条的元信息行
        toggleCeoMessageMetaReveal(e.target);
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
    U.ceoInputResizeHandle?.addEventListener("pointerdown", beginCeoInputResize);
    watchCeoComposerInset();
    window.addEventListener("resize", () => scheduleSyncCeoComposerUsageOutline());
}


function bindModelPage() {
    U.modelRefresh?.addEventListener("click", () => void loadModels());
    U.modelCreate?.addEventListener("click", startCreateModel);
    U.modelRolesCancel?.addEventListener("click", cancelModelRoleEditing);
    U.modelRolesSave?.addEventListener("click", () => void handleModelRoleEditorAction());
    bindContextRefreshDialog();
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
        // 移出链的叉号在组卡内部，必须先判它；否则点叉号会被下面那条"整张卡都可点"的
        // 分支吃掉，只会把配置弹窗弹出来。
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
        // 组卡的整张卡都是点击区，查看态也允许打开配置看成员与预算。
        const groupEdit = e.target.closest("[data-group-edit]");
        if (groupEdit) {
            openLoadBalanceGroupDialog(String(groupEdit.dataset.groupEdit || ""));
            return;
        }
    });
    U.modelGroupCreate?.addEventListener("click", () => openLoadBalanceGroupDialog());
    U.modelGroupList?.addEventListener("click", (e) => {
        // 「删除」在卡片内部，必须先判它，否则点删除会顺手把配置弹窗弹出来。
        const del = e.target.closest("[data-group-delete]");
        if (del) {
            deleteLoadBalanceGroupDraft(String(del.dataset.groupDelete || ""));
            return;
        }
        const edit = e.target.closest("[data-group-edit]");
        if (edit) openLoadBalanceGroupDialog(String(edit.dataset.groupEdit || ""));
    });
    U.modelGroupDialogBody?.addEventListener("click", (e) => {
        const toggle = e.target.closest("[data-group-dialog-member]");
        if (!(toggle instanceof HTMLInputElement)) return;
        if (!toggleLoadBalanceGroupDialogMember(String(toggle.dataset.groupDialogMember || ""), toggle.checked)) return;
        toggle.closest("label")?.classList.toggle("is-selected", toggle.checked);
    });
    U.modelGroupDialogBody?.addEventListener("input", (e) => {
        const nameField = e.target.closest("[data-group-dialog-name]");
        if (nameField instanceof HTMLInputElement) setLoadBalanceGroupDialogField("name", nameField.value);
        const roundsInput = e.target.closest("[data-group-dialog-rounds]");
        if (roundsInput instanceof HTMLInputElement) setLoadBalanceGroupDialogField("rounds", roundsInput.value);
    });
    U.modelGroupDialogBody?.addEventListener("change", (e) => {
        const roundsField = e.target.closest("[data-group-dialog-rounds]");
        if (roundsField instanceof HTMLInputElement) setLoadBalanceGroupDialogField("rounds", roundsField.value);
    });
    U.modelGroupConfirm?.addEventListener("click", () => confirmLoadBalanceGroupDialog());
    U.modelGroupCancel?.addEventListener("click", () => closeLoadBalanceGroupDialog());
    U.modelGroupClose?.addEventListener("click", () => closeLoadBalanceGroupDialog());
    U.modelGroupBackdrop?.addEventListener("click", (e) => {
        if (e.target === U.modelGroupBackdrop) closeLoadBalanceGroupDialog();
    });
    // 组行拖进右侧任意角色链：ref 用 group token，链上的落位与模型走同一套 drop 逻辑。
    U.modelGroupList?.addEventListener("dragstart", (e) => {
        const item = e.target.closest("[data-model-group-ref]");
        if (!item) return;
        beginModelDrag(
            item,
            { ref: String(item.dataset.modelGroupRef || ""), source: "available" },
            e.dataTransfer,
        );
    });
    U.modelGroupList?.addEventListener("dragend", finishModelDrag);
    U.modelRoleEditors?.addEventListener("input", (e) => {
        const field = e.target.closest("[data-model-role-iterations], [data-model-role-limit-input]");
        if (!(field instanceof HTMLElement)) return;
        syncRoleIterationDraftsFromInputs({ requireValid: false });
    });
    U.modelRoleEditors?.addEventListener("change", (e) => {
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
        if (!e.target.closest("[data-model-role-limit-input]")) return;
        syncRoleIterationDraftsFromInputs({ requireValid: false });
    });
    U.modelRoleLimitsBar?.addEventListener("change", (e) => {
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
        const chainItem = e.target.closest("[data-model-chain-ref]");
        if (!chainItem) return;
        beginModelDrag(chainItem, {
            scope: String(chainItem.dataset.scope || ""),
            ref: String(chainItem.dataset.modelChainRef || ""),
            source: "chain",
        }, e.dataTransfer);
    });
    U.modelRoleEditors?.addEventListener("dragover", (e) => {
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
        const availableItem = e.target.closest("[data-model-available-key]");
        if (!availableItem) return;
        beginModelDrag(availableItem, {
            ref: String(availableItem.dataset.modelAvailableKey || ""),
            source: "available",
        }, e.dataTransfer);
    });
    U.modelList?.addEventListener("dragover", (e) => {
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
}


function bindTaskHall() {
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
    U.taskClearSelection?.addEventListener("click", () => clearTaskSelection());
    U.taskFilterTrigger?.addEventListener("click", (e) => {
        e.stopPropagation();
        setTaskMenuOpen("filter", !S.taskFilterMenuOpen);
    });
    U.taskFilterMenu?.querySelectorAll("[data-select-bucket]")?.forEach((button) => button.addEventListener("click", () => {
        S.selectedTaskIds = new Set(S.tasks.filter((task) => statusBucketMatches(task, button.dataset.selectBucket)).map((task) => task.task_id));
        closeTaskMenus();
        renderTasks();
    }));
    document.addEventListener("visibilitychange", () => {
        if (typeof ensureTaskListVisibleReconcile === "function") ensureTaskListVisibleReconcile();
    });
    U.taskSelectionBar?.querySelectorAll("[data-batch-action]")?.forEach((button) => button.addEventListener("click", async () => {
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
}


function bindResourcePages() {
    [U.skillSearch, U.skillRisk, U.skillStatus].forEach((el) => el?.addEventListener(el.tagName === "INPUT" ? "input" : "change", resetSkillPagination));
    U.skillPageSize?.addEventListener("change", (e) => setSkillPageSize(e.target.value));
    U.skillPagePrev?.addEventListener("click", () => setSkillPage(ResourcesState.skillPage - 1));
    U.skillPageNext?.addEventListener("click", () => setSkillPage(ResourcesState.skillPage + 1));
    U.skillRefresh?.addEventListener("click", () => void refreshSkills());
    U.skillSave?.addEventListener("click", () => void saveSkill());
    [U.toolSearch, U.toolStatus, U.toolRisk].forEach((el) => el?.addEventListener(el.tagName === "INPUT" ? "input" : "change", resetToolPagination));
    U.toolPageSize?.addEventListener("change", (e) => setToolPageSize(e.target.value));
    U.toolPagePrev?.addEventListener("click", () => setToolPage(ResourcesState.toolPage - 1));
    U.toolPageNext?.addEventListener("click", () => setToolPage(ResourcesState.toolPage + 1));
    U.toolRefresh?.addEventListener("click", () => void refreshTools());
    U.toolSave?.addEventListener("click", () => void saveTool());
    U.modelBackdrop?.addEventListener("click", clearModelSelection);
    U.skillBackdrop?.addEventListener("click", clearSkillSelection);
    U.toolBackdrop?.addEventListener("click", clearToolSelection);
}


function bindGlobalOverlays() {
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
        if (closeMediaViewer()) return;
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
        if (ResourcesState.selectedSkill) clearSkillSelection();
        if (ResourcesState.selectedTool) clearToolSelection();
    });
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
    bindUpdateNotice();
    void refreshUpdateStatus();
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
