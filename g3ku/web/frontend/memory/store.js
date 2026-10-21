// 记忆页状态层：三列加载与分页、轮询、失败提示。渲染在 org_graph_app.js。

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
