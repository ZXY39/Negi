"use strict";

// CEO 会话快照缓存：normalize/trim、会话级持久化、缓存读写与缓存渲染入口。
// 依赖 app.js 的 S/U 与渲染入口；本文件必须在 org_graph_app.js 之前加载。

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

function normalizeCeoSnapshotToolEvents() {
    return [];
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
        if (message?.can_fork_reply === true) next.can_fork_reply = true;
        if (message?.silent_reply === true) next.silent_reply = true;
        if (message?.silent_reason) next.silent_reason = String(message.silent_reason);
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
            // 发送时间随 inflight 缓存存活,收尾/会话切换后点击用户气泡仍可显示。
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
    const reasoning = normalizeCeoSnapshotReasoning(snapshot?.reasoning);
    if (reasoning) next.reasoning = reasoning;
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
    // 会话级压缩载体必须留在缓存里：inflight 那道"这帧值不值得有个回合"的闸门会把内联
    // 轮的进行中帧整帧丢掉，只读 inflight 时心跳/cron 轮的压缩全程没有信号。
    const compression = normalizeCeoSnapshotCompression(entry?.compression);
    const compressionRunning = String(compression?.status || "").trim().toLowerCase() === "running";
    if (!messages.length && !inflightTurn && !preservedTurn && !compressionRunning) return null;
    const next = {
        session_id: key,
        messages,
        cached_at: String(entry?.cached_at || "").trim() || new Date().toISOString(),
    };
    if (inflightTurn) next.inflight_turn = inflightTurn;
    if (preservedTurn) next.preserved_turn = preservedTurn;
    if (compression) next.compression = compression;
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

async function reloadCeoSessionSnapshot(sessionId) {
    const key = String(sessionId || "").trim();
    if (!key || key !== String(activeSessionId() || "").trim()) return;
    // 区分线只随 snapshot.ceo 的转录行下发；重开这条 WS 才能拿到刚落盘的标记行。
    clearCeoSessionSnapshotCache(key);
    closeCeoWs();
    initCeoWs();
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
        // 点击显形的元数据同样参与合并:重复 finalize/快照回写时新值覆盖旧值,不丢失。
        if (nextMessage.usage) previous.usage = nextMessage.usage;
        if (nextMessage.timestamp) previous.timestamp = nextMessage.timestamp;
        return trimCeoSessionSnapshotMessages(next);
    }
    next.push(nextMessage);
    return trimCeoSessionSnapshotMessages(next);
}

function renderCeoSessionLoadingState(sessionId, session = null) {
    resetCeoFeed();
    hideCeoContextLoadNotice();
    const title = String(session?.title || session?.channel_id || sessionId || "conversation").trim() || "conversation";
    addMsg(`Loading ${title}...`, "system", { scrollMode: "bottom" });
    // resetCeoFeed 会抹掉 live-only 的 running 线；无缓存时的冷启动必须在这里补挂。
    syncCeoCompressionDivider();
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
