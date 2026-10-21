"use strict";

// CEO composer：草稿缓存、占用预估、待发送队列、附件列表与语音输入。
// 只读写 S/U 与既有全局入口；本文件必须在 org_graph_app.js 之前加载。

const CEO_COMPOSER_DRAFT_CACHE_KEY = "g3ku.ceo.composer-drafts.v1";
const CEO_COMPOSER_DRAFT_CACHE_LIMIT = 24;
const CEO_FOLLOW_UP_QUEUE_CACHE_KEY = "g3ku.ceo.follow-up-queues.v1";
const CEO_FOLLOW_UP_QUEUE_CACHE_LIMIT = 24;
const CEO_FOLLOW_UP_QUEUE_PER_SESSION_LIMIT = 20;

let ceoFollowUpResizeObserver = null;
let ceoVoiceEngineReady = false;
let ceoVoicePreparing = null;

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
        resolved_model_key: String(item.resolved_model_key || item.resolvedModelKey || "").trim(),
        trigger_tokens: toInt(item.trigger_tokens ?? item.triggerTokens),
        would_trigger_token_compression: !!(item.would_trigger_token_compression ?? item.wouldTriggerTokenCompression),
        would_exceed_context_window: !!(item.would_exceed_context_window ?? item.wouldExceedContextWindow),
        missing_context_window: !!(item.missing_context_window ?? item.missingContextWindow),
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
    const fill = U.ceoComposerUsageBrainFill;
    if (!shell || !fill) return;
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
    if (typeof shell.style?.setProperty === "function") {
        shell.style.setProperty("--ceo-context-usage-color", usageColor);
    } else {
        shell.style["--ceo-context-usage-color"] = usageColor;
    }
    // 只推水位高度：脑的描边整圈常驻，水体在它下面被轮廓裁开，液面才是唯一会动的东西。
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
        ? `${ceoModelBadgeTitle(estimate) || "current-model"} · ${estimate.estimated_total_tokens}/${estimate.context_window_tokens} TOKEN`
        : "等待 Leader 上下文预估";
    // 数字只在面板头部展示；脑图标保留等价的无障碍名称，不用原生 title 浮层。
    shell.removeAttribute?.("title");
    if (shell.setAttribute) shell.setAttribute("aria-label", tipLabel);
    // 面板头部展示同一份预估，两处数字必须同源。
    if (S.ceoModelSelection.panelOpen) syncCeoModelModePanelUsage();
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

function hasActiveCeoComposerUsageEstimate(sessionId = activeSessionId()) {
    const key = String(sessionId || "").trim();
    if (!key) return false;
    return !!(
        S.ceoComposerUsageEstimate
        && String(S.ceoComposerUsageEstimate.session_id || "").trim() === key
    );
}

function syncCeoComposerReadonlyState() {
    if (!U.ceoInput) return;
    if (!activeSessionCanMessage()) {
        U.ceoInput.setAttribute("readonly", "readonly");
        U.ceoInput.placeholder = "当前为渠道会话，只能查看来自渠道的历史消息";
    } else {
        U.ceoInput.removeAttribute("readonly");
        U.ceoInput.placeholder = "输入你的任务，可保留换行；也可以上传图片或文件作为补充";
    }
    syncCeoModelModeControl();
}

function watchCeoComposerInset() {
    const area = U.ceoInput?.closest(".chat-input-area");
    const wrapper = area?.closest(".chat-wrapper");
    if (!area || !wrapper || !window.ResizeObserver) return;
    const apply = () => wrapper.style.setProperty("--ceo-composer-inset", `${area.offsetHeight + 24}px`);
    apply();
    new ResizeObserver(apply).observe(area);
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
    // 已被回合接走的条目在这里就出局：镜像只是每帧的整表读数，而"接走"之后服务端还会
    // 在若干帧里继续报它。判据只用 turn_id（条目 id 是 `server:<turn_id>`），不读正文。
    return normalizeCeoServerQueuedFollowUpList(S.ceoServerQueuedFollowUps?.[key] || [])
        .filter((item) => !isCeoTakenFollowUpTurn(key, ceoServerQueuedFollowUpTurnId(item?.id)));
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

function sendActiveCeoFollowUpsToRuntime(sessionId = activeSessionId(), onlyIds = null) {
    const key = String(sessionId || "").trim();
    if (!key) return null;
    const targetIds = Array.isArray(onlyIds)
        ? new Set(onlyIds.map((item) => String(item || "").trim()).filter(Boolean))
        : null;
    const queued = getCeoQueuedFollowUps(key).filter((item) => {
        if (String(item?.runtime_sent_at || "").trim()) return false;
        return !targetIds || targetIds.has(String(item?.id || "").trim());
    });
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

function promoteRepresentedRuntimeSentCeoFollowUps(sessionId = activeSessionId(), representedMessages = [], { scrollMode = "preserve", insertBefore = null } = {}) {
    const key = String(sessionId || "").trim();
    if (!key) return [];
    const normalizedRepresented = normalizeCeoSnapshotUserMessages(representedMessages);
    if (!normalizedRepresented.length) return [];
    // 这条帧同时负责服务端那一半的退场：本地条目早就被文本去重交给镜像画了，
    // 只摘本地那份的话，消息都进气泡了候选条还挂着。
    retireCeoTakenFollowUpsFromRepresentedMessages(key, normalizedRepresented);
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

function noteCeoTakenFollowUpTurn(sessionId, turnId) {
    const key = String(sessionId || "").trim();
    const target = String(turnId || "").trim();
    if (!key || !target) return false;
    const taken = ceoTakenFollowUpTurnIds.get(key) || new Set();
    if (taken.has(target)) return false;
    taken.add(target);
    ceoTakenFollowUpTurnIds.set(key, taken);
    return true;
}

function isCeoTakenFollowUpTurn(sessionId, turnId) {
    const key = String(sessionId || "").trim();
    const target = String(turnId || "").trim();
    if (!key || !target) return false;
    return !!ceoTakenFollowUpTurnIds.get(key)?.has(target);
}

function toggleCeoQueuedFollowUpExpansion(entryId) {
    const key = String(entryId || "").trim();
    if (!key) return;
    if (ceoFollowUpExpandedIds.has(key)) ceoFollowUpExpandedIds.delete(key);
    else ceoFollowUpExpandedIds.add(key);
    renderQueuedCeoFollowUps();
}

function measureCeoFollowUpSingleLine() {
    const chips = Array.from(U.ceoFollowUpQueue?.querySelectorAll?.(".ceo-follow-up-chip") || []);
    chips.forEach((chip) => {
        if (!(chip instanceof HTMLElement)) return;
        const entryId = String(chip.dataset.followUpId || "").trim();
        const name = chip.querySelector(".ceo-follow-up-name");
        const button = chip.querySelector(".ceo-follow-up-expand");
        if (!entryId || !(name instanceof HTMLElement) || !(button instanceof HTMLElement)) return;
        // 展开态的正文盒是换行排版的，量出来的宽度永远"放得下"——再按这个结论收回去
        // 就成了点开即塌。只量收起着的行。
        if (chip.classList?.contains("is-expanded")) return;
        const width = Number(name.clientWidth || 0);
        if (width <= 0) return;
        if (name.scrollWidth <= width + 1) {
            ceoFollowUpSingleLineIds.add(entryId);
            ceoFollowUpExpandedIds.delete(entryId);
            chip.classList.remove("is-expanded");
            button.hidden = true;
        } else {
            ceoFollowUpSingleLineIds.delete(entryId);
            button.hidden = false;
        }
    });
}

function watchCeoFollowUpWidth() {
    if (ceoFollowUpResizeObserver || !U.ceoFollowUpQueue || !window.ResizeObserver) return;
    ceoFollowUpResizeObserver = new ResizeObserver(() => measureCeoFollowUpSingleLine());
    ceoFollowUpResizeObserver.observe(U.ceoFollowUpQueue);
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
                const rawId = String(item.id || "");
                const entryId = esc(rawId);
                const singleLine = ceoFollowUpSingleLineIds.has(rawId);
                const expanded = !singleLine && ceoFollowUpExpandedIds.has(rawId);
                const expandLabel = expanded ? "收起正文" : "展开正文";
                const actions = [`
                    <button type="button" class="ceo-follow-up-action ceo-follow-up-expand" data-follow-up-expand="${entryId}"
                        aria-expanded="${expanded ? "true" : "false"}" aria-label="${expandLabel}" title="${expandLabel}"${singleLine ? " hidden" : ""}>
                        <i data-lucide="${expanded ? "chevron-up" : "chevron-down"}"></i>
                    </button>
                `];
                // 三态分开：服务端队列里的条目（重启也还在，编辑与删除都对应服务端那行）／
                // 已转出去在飞的条目（runtime 已持有，既不能"插话"也没有可撤的对象，只能等它
                // 被那一轮代表后由 consumeRepresentedRuntimeSentCeoFollowUps 收掉）／还没出门的本地草稿。
                // 行上不写状态字：条目还挂在输入区上面就代表没发出去。
                const inFlight = !item.accepted_by_runtime && !!String(item.runtime_sent_at || "").trim();
                if (!inFlight) {
                    if (S.ceoTurnActive && !item.accepted_by_runtime) {
                        actions.push(`<button type="button" class="ceo-follow-up-action ceo-follow-up-flush" data-follow-up-flush="${entryId}"
                                aria-label="插话" title="插话：现在就并进正在跑的这一轮">
                                <i data-lucide="send"></i><span>插话</span>
                            </button>`);
                    }
                    actions.push(`<button type="button" class="ceo-follow-up-action" data-follow-up-withdraw="${entryId}" aria-label="编辑" title="编辑：把正文取回输入框继续改">
                            <i data-lucide="pencil"></i>
                        </button>`);
                    actions.push(`<button type="button" class="ceo-follow-up-action ceo-follow-up-remove" data-follow-up-remove="${entryId}" aria-label="删除" title="删除这条待发送补充">
                            <i data-lucide="trash-2"></i>
                        </button>`);
                }
                return `
                <div class="ceo-follow-up-chip${expanded ? " is-expanded" : ""}" role="listitem" data-follow-up-id="${entryId}">
                    <span class="ceo-follow-up-kind">${index + 1}</span>
                    <span class="ceo-follow-up-name">${esc(String(item.text || "").trim() || summarizeUploads(item.uploads || []))}</span>
                    ${actions.join("")}
                </div>
            `;
            }).join("")}
        </div>
    `;
    scheduleCeoComposerUsageRefresh();
    icons();
    watchCeoFollowUpWidth();
    measureCeoFollowUpSingleLine();
}

function findCeoQueuedFollowUpEntry(sessionId, entryId) {
    const key = String(sessionId || "").trim();
    const target = String(entryId || "").trim();
    if (!key || !target) return null;
    return getMergedCeoQueuedFollowUps(key).find((item) => String(item?.id || "").trim() === target) || null;
}

function ceoServerQueuedFollowUpTurnId(entryId) {
    const raw = String(entryId || "").trim();
    return raw.startsWith("server:") ? raw.slice("server:".length).trim() : "";
}

function flushCeoQueuedFollowUp(entryId) {
    const sessionId = activeSessionId();
    const item = findCeoQueuedFollowUpEntry(sessionId, entryId);
    if (!sessionId || !item || item.accepted_by_runtime) return false;
    const sent = sendActiveCeoFollowUpsToRuntime(sessionId, [String(item.id || "")]);
    if (!sent) {
        showToast({ title: "未能并入本轮", text: "连接未就绪，条目仍留在待发送里。", kind: "warn" });
        return false;
    }
    renderQueuedCeoFollowUps(sessionId);
    return true;
}

function pruneCeoServerQueuedFollowUpByTurn(sessionId, turnId) {
    const serverKey = String(sessionId || "").trim();
    const remaining = (Array.isArray(S.ceoServerQueuedFollowUps?.[serverKey]) ? S.ceoServerQueuedFollowUps[serverKey] : [])
        .filter((raw) => String(raw?.metadata?.["_transcript_turn_id"] || "").trim() !== turnId);
    S.ceoServerQueuedFollowUps = {
        ...(S.ceoServerQueuedFollowUps || {}),
        [serverKey]: remaining,
    };
}

function retireCeoTakenFollowUpsFromRepresentedMessages(sessionId = activeSessionId(), representedMessages = []) {
    const key = String(sessionId || "").trim();
    if (!key) return false;
    const mirrored = new Set(
        normalizeCeoServerQueuedFollowUpList(S.ceoServerQueuedFollowUps?.[key] || [])
            .map((item) => ceoServerQueuedFollowUpTurnId(item?.id))
            .filter(Boolean)
    );
    if (!mirrored.size) return false;
    let retired = false;
    (Array.isArray(representedMessages) ? representedMessages : []).forEach((message) => {
        const turnId = String(message?.turn_id || "").trim();
        if (!turnId || !mirrored.has(turnId)) return;
        if (noteCeoTakenFollowUpTurn(key, turnId)) retired = true;
    });
    if (!retired) return false;
    renderQueuedCeoFollowUps(key);
    return true;
}

async function releaseCeoQueuedFollowUp(entryId, { refillComposer }) {
    const sessionId = activeSessionId();
    const item = findCeoQueuedFollowUpEntry(sessionId, entryId);
    if (!sessionId || !item) return false;
    const turnId = ceoServerQueuedFollowUpTurnId(item.id);
    if (item.accepted_by_runtime && turnId) {
        // 已受理的条目活在服务端（内存队列 + 转录 pending 行），必须先删那边，
        // 否则下一次 state 帧会把它原样画回来。
        try {
            await ApiClient.withdrawCeoQueuedFollowUp(sessionId, { turn_id: turnId });
        } catch (error) {
            const code = typeof ApiClient?.getErrorCode === "function" ? ApiClient.getErrorCode(error) : "";
            if (Number(error?.status) === 409 || code === "follow_up_not_queued") {
                // 409 说的是"这条已经不在服务端队列里"（被某个安全边界接走了），
                // 那是退场信号而不是失败：条目留着只会让人再点一次同样的错。
                pruneCeoServerQueuedFollowUpByTurn(sessionId, turnId);
                removeCeoQueuedFollowUp(sessionId, item.id);
                showToast({
                    title: "已被本轮接走",
                    text: "这条补充已经交给当前回合，不再排在待发送里。",
                    kind: "info",
                });
                return false;
            }
            showToast({
                title: "撤下失败",
                text: String(error?.message || "该条补充可能已被本轮接走，无法撤回。"),
                kind: "error",
            });
            return false;
        }
        pruneCeoServerQueuedFollowUpByTurn(sessionId, turnId);
    } else {
        removeCeoQueuedFollowUp(sessionId, item.id);
    }
    ceoFollowUpExpandedIds.delete(String(item.id || ""));
    if (refillComposer) {
        // 编辑的落点是输入框，不是转录：回填后可继续编辑，不自动发送。
        setCeoComposerDraft(sessionId, {
            text: String(item.text || ""),
            uploads: normalizeUploadList(item.uploads),
        });
        restoreCeoComposerDraftForSession(sessionId);
    }
    renderQueuedCeoFollowUps(sessionId);
    syncCeoPrimaryButton();
    return true;
}

function withdrawCeoQueuedFollowUp(entryId) {
    return releaseCeoQueuedFollowUp(entryId, { refillComposer: true });
}

function discardCeoQueuedFollowUp(entryId) {
    return releaseCeoQueuedFollowUp(entryId, { refillComposer: false });
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

function syncCeoAttachButton() {
    if (!U.ceoAttach) return;
    U.ceoAttach.disabled = (
        !!S.ceoUploadBusy
        || !!S.ceoSessionBusy
        || !!S.ceoSessionCatalogBusy
        || !activeSessionId()
        || !activeSessionCanMessage()
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

function removePendingCeoUpload(index) {
    const next = normalizeUploadList(S.ceoUploads);
    if (index < 0 || index >= next.length) return;
    next.splice(index, 1);
    S.ceoUploads = next;
    syncActiveCeoComposerDraft();
    renderPendingCeoUploads();
}

function syncCeoVoiceButton() {
    const button = U.ceoVoiceBtn;
    syncCeoVoiceOptions();
    if (!button) return;
    const recording = Boolean(S.ceoVoice);
    button.setAttribute("aria-pressed", recording ? "true" : "false");
    button.classList.toggle("is-recording", recording);
    button.innerHTML = `<i data-lucide="${recording ? "mic-off" : "mic"}"></i>`;
    button.title = recording ? "停止并识别" : "语音输入";
    icons();
}

function stopCeoVoiceCapture() {
    const capture = S.ceoVoice;
    if (!capture) return;
    S.ceoVoice = null;
    if (capture.timer) window.clearTimeout(capture.timer);
    capture.stream.getTracks().forEach((track) => track.stop());
    syncCeoVoiceButton();
    // 不在这里取数据：MediaRecorder 的最后一片要等 stop() 之后的 ondataavailable
    // 才落地，提前读会吞掉尾音。收尾统一挂在 onstop。
    try {
        capture.recorder.stop();
    } catch (error) {
        void error;
    }
}

function formatVoiceBytes(bytes) {
    const value = Number(bytes);
    if (!Number.isFinite(value) || value <= 0) return "";
    const mb = value / (1024 * 1024);
    return mb >= 10 ? `${Math.round(mb)} MB` : `${Math.round(mb * 10) / 10} MB`;
}

function provisionCeoVoiceEngine() {
    if (ceoVoicePreparing) return ceoVoicePreparing;
    const run = async () => {
        const startedAt = Date.now();
        try {
            await ApiClient.prepareCeoVoice();
            while (true) {
                const status = await ApiClient.getCeoVoiceStatus();
                if (status && status.ready) {
                    closeToast();
                    ceoVoiceEngineReady = true;
                    showToast({ title: "语音已就绪", text: "点麦克风开始录音，说完再点一次。", kind: "success", durationMs: 4000 });
                    return true;
                }
                const prep = (status && status.prepare) || {};
                if (prep.state === "error") {
                    showToast({ title: "语音下载失败", text: String(prep.error || "未知错误"), kind: "error", durationMs: 9000 });
                    return false;
                }
                if (Date.now() - startedAt > VOICE_PREPARE_MAX_MS) {
                    showToast({ title: "语音下载超时", text: "这台机器到下载源太慢，稍后再点一次麦克风会继续下载（支持断点续传）。", kind: "error", durationMs: 9000 });
                    return false;
                }
                showToast({
                    title: "首次使用需要下载语音",
                    text: voicePrepareText(status),
                    kind: "info",
                    persistent: true,
                });
                await new Promise((resolve) => window.setTimeout(resolve, VOICE_PREPARE_POLL_MS));
            }
        } catch (error) {
            showToast({
                title: "语音下载失败",
                text: String(error && error.message ? error.message : error),
                kind: "error",
                durationMs: 9000,
            });
            return false;
        }
    };
    ceoVoicePreparing = run().finally(() => { ceoVoicePreparing = null; });
    return ceoVoicePreparing;
}

async function ensureCeoVoiceEngine() {
    if (ceoVoiceEngineReady) return true;
    let status;
    try {
        status = await ApiClient.getCeoVoiceStatus();
    } catch (error) {
        showToast({
            title: "无法确认语音状态",
            text: String(error && error.message ? error.message : error),
            kind: "error",
            durationMs: 8000,
        });
        return false;
    }
    const gate = voiceEngineGate(status);
    if (gate === "ready") {
        ceoVoiceEngineReady = true;
        return true;
    }
    if (gate === "disabled") {
        showToast({
            title: "语音识别已关闭",
            text: "这台设备的配置里 stt.enabled 是关的，打开后才能用麦克风。",
            kind: "error",
            durationMs: 8000,
        });
        return false;
    }
    showToast({
        title: "首次使用需要下载语音",
        text: `${voiceDownloadTotalText(status)}下载完自动开始录音。`,
        kind: "info",
        persistent: true,
    });
    return await provisionCeoVoiceEngine();
}

async function startCeoVoiceCapture() {
    if (S.ceoVoice || S.ceoVoiceBusy) return;
    if (!voiceCaptureSupported()) {
        // getUserMedia 只在安全上下文可用：用局域网 IP 走 http 打开面板时浏览器
        // 直接禁麦克风，这里必须把原因说出来，否则按钮看起来像坏了。
        showToast({
            title: "无法录音",
            text: "当前页面不是安全上下文，浏览器禁止访问麦克风。请用 http://127.0.0.1 或 HTTPS 地址打开面板。",
            kind: "error",
            durationMs: 8000,
        });
        return;
    }
    // 新设备上那 150 MB 还没下来：先下、下完再开录音。反过来做就是用户说完一段话，
    // 才被告知"其实还没装模型"。
    if (!(await ensureCeoVoiceEngine())) return;
    let stream;
    try {
        stream = await navigator.mediaDevices.getUserMedia({
            audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true },
        });
    } catch (error) {
        showToast({ title: "麦克风不可用", text: String(error && error.message ? error.message : error), kind: "error" });
        return;
    }
    const recorder = new window.MediaRecorder(stream);
    const capture = { recorder, stream, chunks: [], startedAt: Date.now(), timer: null };
    recorder.ondataavailable = (event) => {
        if (event && event.data && event.data.size) capture.chunks.push(event.data);
    };
    recorder.onstop = () => { void finishCeoVoiceTranscription(capture); };
    capture.mimeType = recorder.mimeType || "audio/webm";
    // 到点自己停：上限同时受后端 stt.max_audio_seconds 约束，超了会被拒。
    capture.timer = window.setTimeout(() => stopCeoVoiceCapture(), VOICE_MAX_MS);
    S.ceoVoice = capture;
    recorder.start();
    syncCeoVoiceButton();
}

async function finishCeoVoiceTranscription(capture) {
    S.ceoVoiceBusy = true;
    syncCeoVoiceOptions();
    showToast({ title: "识别中", text: "正在本机转写，通常需要几秒", kind: "info", persistent: true });
    try {
        const blob = new Blob(capture.chunks, { type: capture.mimeType });
        const wav = await audioBlobToWav16k(blob);
        const result = await ApiClient.transcribeCeoVoice(wav);
        closeToast();
        if (result && result.ok && result.text) {
            // 字节留不留，取决于这条会不会以"语音"的身份发出去：自动发送才带标记，
            // 才配挂一段可回放的录音；手动模式会被编辑，留下字节就是让音频和文本各说
            // 一套，所以用完即扔。判定与上传都在 deliverVoiceText 里一次做完。
            await deliverVoiceText(String(result.text), wav);
            return;
        }
        const missingCode = String((result && result.error_code) || "");
        if (missingCode === "stt_binary_missing" || missingCode === "stt_model_missing") {
            // 过了状态那道门还会缺件，只可能是刚被删掉或换过数据目录：同样按下载处理，
            // 而不是把一句 CLI 命令甩给用户。
            ceoVoiceEngineReady = false;
            closeToast();
            await provisionCeoVoiceEngine();
            return;
        }
        showToast({
            title: "没有识别到文字",
            text: voiceFailureText(result),
            kind: "error",
            durationMs: 9000,
        });
    } catch (error) {
        closeToast();
        showToast({ title: "识别失败", text: String(error && error.message ? error.message : error), kind: "error" });
    } finally {
        S.ceoVoiceBusy = false;
        syncCeoVoiceOptions();
    }
}

function attachVoiceClip(wavBlob) {
    // 复用附件车道：服务端按 audio/wav 判出 kind='audio'，渲染层据此画语音气泡。
    // 上传失败不吞掉转写文字——顶多退化成一条普通文字气泡，比整条消息消失好。
    const file = new File([wavBlob], `voice-${Date.now()}.wav`, { type: "audio/wav" });
    return ApiClient.uploadCeoFiles([file], activeSessionId())
        .then((uploaded) => {
            const clips = normalizeUploadList(uploaded);
            if (!clips.length) return;
            S.ceoUploads = [...normalizeUploadList(S.ceoUploads), ...clips];
            syncActiveCeoComposerDraft();
            renderPendingCeoUploads();
        })
        .catch(() => {});
}

function ceoVoiceAutoSendEnabled() {
    if (S.ceoVoiceAutoSend === null || S.ceoVoiceAutoSend === undefined) return true;
    return Boolean(S.ceoVoiceAutoSend);
}

function setCeoVoiceAutoSend(enabled) {
    S.ceoVoiceAutoSend = Boolean(enabled);
    try {
        window.localStorage.setItem(VOICE_AUTO_SEND_STORAGE_KEY, S.ceoVoiceAutoSend ? "1" : "0");
    } catch (error) {
        void error; // 隐私模式写不进去不影响本次使用
    }
}

function restoreCeoVoiceAutoSend() {
    let stored = null;
    try {
        stored = window.localStorage.getItem(VOICE_AUTO_SEND_STORAGE_KEY);
    } catch (error) {
        void error;
    }
    S.ceoVoiceAutoSend = stored === null ? true : stored === "1";
    if (U.ceoVoiceAutoSend) U.ceoVoiceAutoSend.checked = S.ceoVoiceAutoSend;
}

function syncCeoVoiceOptions() {
    const box = U.ceoVoiceOptions;
    if (!box) return;
    box.hidden = !(S.ceoVoice || S.ceoVoiceBusy);
    if (U.ceoVoiceAutoSend) U.ceoVoiceAutoSend.checked = ceoVoiceAutoSendEnabled();
}

function insertVoiceTextAtCursor(text) {
    const el = U.ceoInput;
    if (!el) return;
    const value = String(el.value || "");
    // 插在光标处、不动两侧：已有草稿不能被语音结果覆盖，也不能被推到末尾。
    const from = Number.isFinite(el.selectionStart) ? el.selectionStart : value.length;
    const to = Number.isFinite(el.selectionEnd) ? el.selectionEnd : from;
    el.value = `${value.slice(0, from)}${text}${value.slice(to)}`;
    const caret = from + text.length;
    if (typeof el.setSelectionRange === "function") {
        try {
            el.setSelectionRange(caret, caret);
        } catch (error) {
            void error;
        }
    }
    syncCeoInputHeight();
    syncActiveCeoComposerDraft();
    syncCeoPrimaryButton();
    el.focus();
}

async function deliverVoiceText(text, clip) {
    const draftBefore = String(U.ceoInput ? U.ceoInput.value : "");
    if (ceoVoiceAutoSendEnabled() && !draftBefore.trim()) {
        U.ceoInput.value = `${VOICE_AUTO_SEND_PREFIX}${text}`;
        if (clip) {
            // 带标记发出的这一条才留字节：语音气泡要能回放，而服务端
            // /api/ceo/transcribe 转写完就把字节丢了（不落盘是它的契约）。
            await attachVoiceClip(clip);
        }
        sendCeoMessage();
        // sendCeoMessage 成功时会清空输入框；没清空说明这条发不出去
        // （会话忙、只读、没选中会话），把文本留在框里比吞掉它好。
        if (String(U.ceoInput.value || "") !== "") {
            showToast({ title: "未能自动发送", text: "语音文字已留在输入框，可手动发送。", kind: "info", durationMs: 7000 });
            syncCeoPrimaryButton();
        }
        return;
    }
    // 走到这里就是不落字节的那条道：识别文字进输入框由用户改，改完当普通文本发。
    insertVoiceTextAtCursor(text);
    if (ceoVoiceAutoSendEnabled()) {
        // 自动发送开着但框里已有草稿：不替用户决定要不要把草稿一起发出去。
        showToast({ title: "已插入语音文字", text: "输入框里原有内容，本次未自动发送。", kind: "info", durationMs: 7000 });
    }
}

function handleCeoVoiceClick() {
    if (S.ceoVoice) {
        stopCeoVoiceCapture();
        return;
    }
    void startCeoVoiceCapture();
}
