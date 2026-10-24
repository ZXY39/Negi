// Task tree and task detail view layer extracted from org_graph_app.js.
// Loaded before org_graph_app.js and relies on globals initialized there at runtime.

function setTaskSelectionEmptyVisible(visible) {
    if (U.taskSelectionEmpty) U.taskSelectionEmpty.hidden = !visible;
}

function resetTaskTreeSnapshotState({ clearDirty = true } = {}) {
    Object.values(S.treeBranchSyncTokenById || {}).forEach((token) => {
        if (token) window.clearTimeout(token);
    });
    if (S.treeSnapshotSelfHealToken) {
        window.clearTimeout(S.treeSnapshotSelfHealToken);
        S.treeSnapshotSelfHealToken = null;
    }
    S.treeSnapshotSelfHealAttempts = 0;
    S.treeRootNodeId = "";
    S.treeNodesById = {};
    S.treeSnapshotVersion = "";
    S.treeView = null;
    S.treeLargeMode = false;
    S.treeSelectedRoundByNodeId = {};
    S.treeShowTerminal = null;
    S.treeExpandedNodeIds = new Set();
    S.treeBranchSyncTokenById = {};
    S.treeBranchSyncInFlightById = {};
    S.treeBranchSyncQueuedById = {};
    S.treeLocateHighlight = null;
    if (clearDirty) S.treeDirtyParentsById = {};
}

// 分块快照合并：首块重置树缓存（语义同整表快照落位），后续块按 id 增量并入。
// 只增不改删：父节点的 rounds/auxiliary 子 id 列表在服务端本就按整树计算，
// 所以块内缺失的子节点只是还没到，不是没有。
function applyTaskTreeSnapshotChunkPayload(payload = {}, { isFirstChunk = false } = {}) {
    const rootNodeId = String(payload?.root_node_id || "").trim();
    const sourceNodes = payload?.nodes_by_id && typeof payload.nodes_by_id === "object" ? payload.nodes_by_id : {};
    const nextNodesById = isFirstChunk ? {} : { ...(S.treeNodesById || {}) };
    Object.entries(sourceNodes).forEach(([nodeId, node]) => {
        const normalizedNodeId = String(nodeId || node?.node_id || "").trim();
        if (!normalizedNodeId) return;
        nextNodesById[normalizedNodeId] = normalizeTaskTreeSnapshotNode(node, nextNodesById[normalizedNodeId] || null);
    });
    if (isFirstChunk) {
        S.treeRootNodeId = rootNodeId;
        S.treeSnapshotSelfHealAttempts = 0;
        S.treeSelectedRoundByNodeId = pruneTreeRoundSelections({});
        S.treeLargeMode = false;
    } else if (!String(S.treeRootNodeId || "").trim() && rootNodeId) {
        S.treeRootNodeId = rootNodeId;
    }
    S.treeNodesById = nextNodesById;
    if (String(payload?.snapshot_version || "").trim()) {
        S.treeSnapshotVersion = String(payload.snapshot_version || "").trim();
    }
    S.treeView = null;
    if (!isFirstChunk) {
        S.treeSelectedRoundByNodeId = pruneTreeRoundSelections(S.treeSelectedRoundByNodeId);
    }
}

function applyTaskTreeSnapshotPayload(payload = {}) {
    // 整表快照落位 = 分块合并的首块语义；分块加载路径走
    // applyTaskTreeSnapshotChunkPayload + loadTaskTreeSnapshot。
    applyTaskTreeSnapshotChunkPayload(payload, { isFirstChunk: true });
    refreshTaskTreeSearchResultsIfVisible();
}

// 渲染时发现根节点已不在快照缓存中（增量合并把树搞成了残缺态）：展示空态的
// 同时去抖触发一次整表快照重载自愈。限 2 次，避免后端真返回无根快照时无限循环。
function scheduleTaskTreeSnapshotSelfHeal() {
    const taskId = String(S.currentTaskId || "").trim();
    if (!taskId) return;
    if (S.treeSnapshotSelfHealToken) return;
    if (Number(S.treeSnapshotSelfHealAttempts || 0) >= 2) return;
    S.treeSnapshotSelfHealAttempts = Number(S.treeSnapshotSelfHealAttempts || 0) + 1;
    S.treeSnapshotSelfHealToken = window.setTimeout(() => {
        S.treeSnapshotSelfHealToken = null;
        if (String(S.currentTaskId || "").trim() !== taskId) return;
        if (treeSnapshotNode(String(S.treeRootNodeId || "").trim())) return;
        void loadTaskTreeSnapshot(taskId);
    }, 500);
}

function applyTaskTreeSubtreePayload(payload = {}) {
    const subtreeRootId = String(payload?.root_node_id || "").trim();
    if (!subtreeRootId) return;
    const sourceNodes = payload?.nodes_by_id && typeof payload.nodes_by_id === "object" ? payload.nodes_by_id : {};
    // 自愈防线：子树响应必须包含它自己的根节点。本次合并是"先删旧子树再整体
    // 重建"，若响应缺失子树根（退化/半态响应），合并会把该根从缓存中删掉——
    // 当子树根恰好是整树根节点时，树会整体消失而搜索仍能命中旧缓存
    // （2026-09-12 分发期间"节点树消失"事故的破坏路径）。丢弃该响应并回退
    // 整表快照重载。
    const hasSubtreeRoot = Object.entries(sourceNodes).some(([nodeId, node]) => (
        String(nodeId || node?.node_id || "").trim() === subtreeRootId
    ));
    if (!hasSubtreeRoot) {
        const taskId = String(S.currentTaskId || "").trim();
        if (taskId) void loadTaskTreeSnapshot(taskId);
        return;
    }
    const nextNodesById = { ...(S.treeNodesById || {}) };
    collectSnapshotSubtreeIds(subtreeRootId, nextNodesById).forEach((nodeId) => {
        delete nextNodesById[nodeId];
    });
    Object.entries(sourceNodes).forEach(([nodeId, node]) => {
        const normalizedNodeId = String(nodeId || node?.node_id || "").trim();
        if (!normalizedNodeId) return;
        nextNodesById[normalizedNodeId] = normalizeTaskTreeSnapshotNode(node, nextNodesById[normalizedNodeId] || null);
    });
    S.treeNodesById = nextNodesById;
    if (!String(S.treeRootNodeId || "").trim()) {
        S.treeRootNodeId = subtreeRootId;
    }
    if (String(payload?.snapshot_version || "").trim()) {
        S.treeSnapshotVersion = String(payload.snapshot_version || "").trim();
    }
    S.treeView = null;
    S.treeSelectedRoundByNodeId = pruneTreeRoundSelections(S.treeSelectedRoundByNodeId);
    refreshTaskTreeSearchResultsIfVisible();
}

function markTaskTreeParentDirty(nodeId) {
    const normalizedNodeId = String(nodeId || "").trim();
    if (!normalizedNodeId) return false;
    const wasDirty = !!S.treeDirtyParentsById?.[normalizedNodeId];
    S.treeDirtyParentsById = { ...(S.treeDirtyParentsById || {}), [normalizedNodeId]: true };
    return !wasDirty;
}

function clearTaskTreeParentDirty(nodeId) {
    const normalizedNodeId = String(nodeId || "").trim();
    if (!normalizedNodeId || !S.treeDirtyParentsById?.[normalizedNodeId]) return;
    const next = { ...(S.treeDirtyParentsById || {}) };
    delete next[normalizedNodeId];
    S.treeDirtyParentsById = next;
}

function taskTreeParentIsDirty(nodeId) {
    const normalizedNodeId = String(nodeId || "").trim();
    return !!(normalizedNodeId && S.treeDirtyParentsById?.[normalizedNodeId]);
}

function resolveTaskTreeBranchRoundId(nodeId) {
    const normalizedNodeId = String(nodeId || "").trim();
    const node = treeSnapshotNode(normalizedNodeId);
    if (!node) return "";
    return snapshotNodeSelectedRoundId(node, S.treeSelectedRoundByNodeId);
}

// 大树打开超时治理：tree-snapshot 按稳定排序分块加载。每块至多 250 个节点，
// 上限 400 块（10 万节点）纯属防御，正常任务远达不到。
const TASK_TREE_CHUNK_NODES = 250;
const TASK_TREE_MAX_CHUNKS = 400;
// 打开任务 400ms 内没走到树渲染阶段才亮出加载提示条：避免小任务打开时提示一闪而过。
const TASK_TREE_LOAD_NOTICE_DELAY_MS = 400;
// 全部块落位后，已显示数字的提示条保留一小段可见窗口再隐藏：渲染与隐藏同帧
// 没有中间绘制帧，用户永远看不到最后一段数字。
const TASK_TREE_LOAD_NOTICE_DONE_MS = 800;

// 打开任务的加载进度是一行式状态条（U.taskLoadNotice，见 .task-load-notice），
// 与 toast 卡片彻底分离：没有标题、没有关闭按钮、不接收指针事件。它只属于
// 「打开任务」这条引导路径（loadTaskTreeSnapshot 的 announceLoad）。节点暂停/
// 恢复、定向通知、快照自愈同样会重拉整树，但它们发生在已经打开的任务里，
// 报「正在打开任务」会让用户以为任务又被打开了一次。
function taskTreeLoadNoticeText(loadedCount, totalCount) {
    const totalText = Number.isFinite(totalCount) && Number(totalCount) > 0 ? String(totalCount) : "…";
    return `正在打开任务：加载中 (${loadedCount}/${totalText})`;
}

function clearTaskTreeLoadNoticeTimer() {
    if (S.treeLoadNoticeTimer) window.clearTimeout(S.treeLoadNoticeTimer);
    S.treeLoadNoticeTimer = null;
    S.treeLoadNoticeTimerTaskId = "";
}

function hideTaskTreeLoadNotice() {
    if (!U.taskLoadNotice || U.taskLoadNotice.hidden) return;
    U.taskLoadNotice.hidden = true;
    U.taskLoadNotice.className = "task-load-notice";
}

// 一行字提示条与任务树左上角的搜索框对齐（.task-tree-search）。详情页顶栏会
// 随窗口宽度折行，搜索框的 y 因此不是常量（宽窗口单行 ~70px，窄窗口折成两行
// ~150px），所以每次显示时按实测几何算，不写死像素；取不到锚点（非详情视图、
// 独立 vm 环境）就退回 CSS 里的默认偏移。
function alignTaskTreeLoadNoticeToSearchBox() {
    const notice = U.taskLoadNotice;
    if (!notice || typeof notice.getBoundingClientRect !== "function") return;
    const viewport = notice.parentElement;
    const anchor = typeof document === "undefined"
        ? null
        : document.querySelector(".task-tree-search-input") || document.querySelector(".task-tree-search");
    if (!viewport || !anchor || typeof anchor.getBoundingClientRect !== "function") {
        notice.style.marginTop = "";
        return;
    }
    const anchorRect = anchor.getBoundingClientRect();
    const viewportRect = viewport.getBoundingClientRect();
    // 提示条中心线 = 搜索框中心线（两个 rect 同为窗口坐标，差值即偏移）。
    const offset = anchorRect.top + anchorRect.height / 2 - viewportRect.top - notice.getBoundingClientRect().height / 2;
    notice.style.marginTop = `${Math.max(0, Math.round(offset))}px`;
}

function showTaskTreeLoadNotice(text) {
    if (!U.taskLoadNotice || !U.taskLoadNoticeText) return;
    U.taskLoadNoticeText.textContent = String(text || "");
    // 已经在显示（逐块更新数字的常规路径）：只改文本，不重复写显示状态。
    if (!U.taskLoadNotice.hidden) return;
    U.taskLoadNotice.hidden = false;
    // 对齐要用到提示条自身高度，必须先取消 hidden 再量。
    alignTaskTreeLoadNoticeToSearchBox();
    U.taskLoadNotice.className = "task-load-notice is-open";
}

function cancelTaskTreeLoadNotice(taskId = "") {
    const key = String(taskId || "").trim();
    const ownsNotice = !!key && String(S.treeLoadNoticeTaskId || "").trim() === key;
    const ownsTimer = !!key && String(S.treeLoadNoticeTimerTaskId || "").trim() === key;
    // 带任务 id 却不属于该任务：不动屏幕上别人的提示条。
    if (key && !ownsNotice && !ownsTimer) return;
    clearTaskTreeLoadNoticeTimer();
    S.treeLoadNoticeTaskId = "";
    if (!key || ownsNotice) hideTaskTreeLoadNotice();
}

function beginTaskTreeLoadNotice(taskId) {
    const key = String(taskId || "").trim();
    if (!key) return;
    clearTaskTreeLoadNoticeTimer();
    S.treeLoadNoticeTaskId = key;
    showTaskTreeLoadNotice(taskTreeLoadNoticeText(0, null));
}

// 打开任务时的延迟提示：只负责"详情请求仍在途"的空窗期显示，
// 后续进度数字由 loadTaskTreeSnapshot 的 updateTaskTreeLoadNotice 接管。
function scheduleTaskTreeLoadNotice(taskId) {
    const key = String(taskId || "").trim();
    if (!key || !U.taskLoadNotice) return;
    if (String(S.treeLoadNoticeTaskId || "").trim() === key
        || String(S.treeLoadNoticeTimerTaskId || "").trim() === key) {
        // 同一任务：提示条已在显示（或弹出定时器已排定）。保持所有权不重置，
        // 否则 updateTaskTreeLoadNotice 会因「总数未超单块」的防闪烁闸门不再更新
        // 已显示的数字，进度从此停在 0。
        return;
    }
    // 上一个任务遗留的加载提示失去归属：先收掉，避免切任务后残留在屏幕上。
    cancelTaskTreeLoadNotice();
    S.treeLoadNoticeTimerTaskId = key;
    S.treeLoadNoticeTimer = window.setTimeout(() => {
        S.treeLoadNoticeTimer = null;
        S.treeLoadNoticeTimerTaskId = "";
        if (String(S.currentTaskId || "").trim() !== key) return;
        beginTaskTreeLoadNotice(key);
    }, TASK_TREE_LOAD_NOTICE_DELAY_MS);
}

function updateTaskTreeLoadNotice(taskId, loadedCount, totalCount) {
    const key = String(taskId || "").trim();
    if (!key || !U.taskLoadNotice) return;
    clearTaskTreeLoadNoticeTimer();
    if (String(S.treeLoadNoticeTaskId || "").trim() !== key) {
        // 还没到显示阈值：只有确认是分块大任务（超过一块）才立即弹出，
        // 避免小任务打开时提示条一闪而过。
        const showNow = Number(totalCount || 0) > TASK_TREE_CHUNK_NODES || Number(loadedCount || 0) > TASK_TREE_CHUNK_NODES;
        if (!showNow) return;
        S.treeLoadNoticeTaskId = key;
    }
    showTaskTreeLoadNotice(taskTreeLoadNoticeText(loadedCount, totalCount));
}

function finishTaskTreeLoadNotice(taskId = "") {
    const key = String(taskId || "").trim();
    const owns = !!key && String(S.treeLoadNoticeTaskId || "").trim() === key;
    clearTaskTreeLoadNoticeTimer();
    if (!owns) {
        // 没有显示任何加载提示（快速打开）：只清状态，屏幕上没有可收的东西。
        S.treeLoadNoticeTaskId = "";
        return;
    }
    // 已显示的提示条保留 TASK_TREE_LOAD_NOTICE_DONE_MS 的收尾窗口再隐藏，
    // 保证最后一段进度数字有绘制帧、用户看得见。
    S.treeLoadNoticeTimerTaskId = key;
    S.treeLoadNoticeTimer = window.setTimeout(() => {
        S.treeLoadNoticeTimer = null;
        S.treeLoadNoticeTimerTaskId = "";
        if (String(S.treeLoadNoticeTaskId || "").trim() !== key) return;
        S.treeLoadNoticeTaskId = "";
        hideTaskTreeLoadNotice();
    }, TASK_TREE_LOAD_NOTICE_DONE_MS);
}

function clearTaskTreeBranchSyncTimers() {
    const tokens = S.treeBranchSyncTokenById || {};
    Object.values(tokens).forEach((timeoutId) => {
        if (timeoutId) window.clearTimeout(timeoutId);
    });
    S.treeBranchSyncTokenById = {};
    S.treeBranchSyncQueuedById = {};
}

// 详情视图是否仍在展示：isTaskDetailsViewActive 定义在 org_graph_app.js，
// 独立 vm 测试环境不加载该文件，此时按"仍在展示"处理（不触发掐尾守卫）。
function treeDetailViewActive() {
    return typeof isTaskDetailsViewActive !== "function" || isTaskDetailsViewActive();
}

// 离开任务详情视图的"掐尾"：分块整树加载循环、懒加载分支重同步及其定时器、
// 快照自愈定时器全部失效，在途树请求回来后不再应用快照/重建树 DOM（否则会在
// 任务大厅视图里继续阻塞主线程）。返回大厅后重新打开任何任务都会完整重载，
// 这里丢弃的只是纯浪费的尾巴，不损失任何可见状态。
function cancelTaskTreeLoading() {
    const taskId = String(S.currentTaskId || "").trim();
    S.treeBulkLoadToken = Number(S.treeBulkLoadToken || 0) + 1;
    S.treeDetailGeneration = Number(S.treeDetailGeneration || 0) + 1;
    S.treeBulkLoadingTaskId = "";
    if (taskId) cancelTaskTreeLoadNotice(taskId);
    if (S.treeSnapshotSelfHealToken) {
        window.clearTimeout(S.treeSnapshotSelfHealToken);
        S.treeSnapshotSelfHealToken = null;
    }
    clearTaskTreeBranchSyncTimers();
    S.treeDirtyParentsById = {};
}

// announceLoad 只由「打开任务」的引导路径（loadTaskDetail）开启：那是用户唯一
// 期待看到"正在打开任务"进度条的时刻。原地刷新（节点暂停/恢复、定向通知、
// 快照自愈、子树回退）走默认的 false：既不起提示条，也不接管在途打开流程的提示条。
async function loadTaskTreeSnapshot(taskId = S.currentTaskId, { announceLoad = false } = {}) {
    const normalizedTaskId = String(taskId || "").trim();
    if (!normalizedTaskId) return null;
    // 已离开详情视图：不再启动整树加载（节点暂停/恢复、定向通知等操作的
    // 回调续体会走到这里；此时不应在大厅里重启分块请求流）。
    if (!treeDetailViewActive()) return null;
    if (U.tree) U.tree.innerHTML = '<div class="empty-state">Loading task tree...</div>';
    // 分块加载门闩：加载期间 renderTree 直接返回，等全部块落位再渲染整树。
    // loadToken 用于丢弃被重连/重开覆盖的旧加载循环（块合并按 id 幂等，但渲染
    // 与状态收尾只能由最新一次加载执行）。
    S.treeBulkLoadingTaskId = normalizedTaskId;
    S.treeBulkLoadToken = Number(S.treeBulkLoadToken || 0) + 1;
    const loadToken = S.treeBulkLoadToken;
    if (announceLoad) {
        scheduleTaskTreeLoadNotice(normalizedTaskId);
    } else {
        // 原地刷新抢在打开流程之前（令牌已换新，打开流程的旧循环只会在下一个请求
        // 边界静默返回，永远走不到收尾）：它留下的提示条已失去意义，就地收掉。
        cancelTaskTreeLoadNotice(normalizedTaskId);
    }
    let totalNodeCount = null;
    let chunkIndex = 0;
    let cursor = "";
    let truncated = true;
    try {
        while (chunkIndex < TASK_TREE_MAX_CHUNKS) {
            const payload = await ApiClient.getTaskTreeSnapshot(normalizedTaskId, {
                maxNodes: TASK_TREE_CHUNK_NODES,
                afterNodeId: cursor,
            });
            if (String(S.currentTaskId || "").trim() !== normalizedTaskId
                || Number(S.treeBulkLoadToken || 0) !== Number(loadToken || 0)) return null;
            applyTaskTreeSnapshotChunkPayload(payload || {}, { isFirstChunk: chunkIndex === 0 });
            chunkIndex += 1;
            if (Number.isFinite(Number(payload?.total_node_count)) && Number(payload?.total_node_count) > 0) {
                totalNodeCount = Math.max(1, Number(payload.total_node_count) || 1);
            }
            if (announceLoad) {
                updateTaskTreeLoadNotice(normalizedTaskId, Object.keys(S.treeNodesById || {}).length, totalNodeCount);
            }
            truncated = !!payload?.truncated;
            if (!truncated) break;
            cursor = String(payload?.next_after_node_id || "").trim();
            if (!cursor) break; // 防御：声明未加载完却无续传游标，避免死循环
        }
        if (String(S.currentTaskId || "").trim() !== normalizedTaskId
            || Number(S.treeBulkLoadToken || 0) !== Number(loadToken || 0)) return null;
        S.treeBulkLoadingTaskId = "";
        S.treeSelectedRoundByNodeId = pruneTreeRoundSelections(S.treeSelectedRoundByNodeId);
        if (truncated) {
            // 触及防御上限：展示已加载部分，并明确提示不完整。
            cancelTaskTreeLoadNotice(normalizedTaskId);
            showToast({ title: "任务树加载不完整", text: `节点过多，仅加载了 ${Object.keys(S.treeNodesById || {}).length} 个节点`, kind: "warn" });
        } else if (announceLoad) {
            finishTaskTreeLoadNotice(normalizedTaskId);
        }
        refreshTaskTreeSearchResultsIfVisible();
        renderTree();
        if (S.treeFitOnNextRender) {
            S.treeFitOnNextRender = false;
            fitTaskTreeToView();
            window.requestAnimationFrame(() => fitTaskTreeToView());
            if (document.fonts?.ready?.then) {
                document.fonts.ready
                    .then(() => window.requestAnimationFrame(() => fitTaskTreeToView()))
                    .catch(() => {});
            }
        }
        return chunkIndex > 0 ? { nodes_by_id: S.treeNodesById } : null;
    } catch (error) {
        if (String(S.currentTaskId || "").trim() === normalizedTaskId
            && Number(S.treeBulkLoadToken || 0) === Number(loadToken || 0)) {
            S.treeBulkLoadingTaskId = "";
            cancelTaskTreeLoadNotice(normalizedTaskId);
        }
        if (!isAbortLike(error) && U.tree) {
            U.tree.innerHTML = `<div class="empty-state error">Task tree unavailable: ${esc(error.message || "Unknown error")}</div>`;
        }
        if (!isAbortLike(error) && typeof showToast === "function") {
            showToast({ title: "任务树加载失败", text: error.message || "Unknown error", kind: "error" });
        }
        return null;
    }
}

async function ensureTaskTreeSubtree(nodeId, { roundId = "", force = false } = {}) {
    const taskId = String(S.currentTaskId || "").trim();
    const normalizedNodeId = String(nodeId || "").trim();
    const normalizedRoundId = String(roundId || "").trim();
    if (!taskId || !normalizedNodeId) return null;
    const requestKey = `${normalizedNodeId}::${normalizedRoundId || "default"}`;
    if (!force && S.treeBranchSyncInFlightById?.[requestKey]) return S.treeBranchSyncInFlightById[requestKey];
    const generation = Number(S.treeDetailGeneration || 0);
    const request = (async () => {
        try {
            const payload = await ApiClient.getTaskNodeTreeSubtree(taskId, normalizedNodeId, { roundId: normalizedRoundId });
            if (String(S.currentTaskId || "").trim() !== taskId) return null;
            if (Number(S.treeDetailGeneration || 0) !== generation) return null; // 已离开详情视图：丢弃。
            applyTaskTreeSubtreePayload(payload || {});
            clearTaskTreeParentDirty(normalizedNodeId);
            renderTree();
            return payload || null;
        } catch (error) {
            if (!isAbortLike(error)) {
                showToast({ title: "Task subtree load failed", text: error.message || "Unknown error", kind: "error" });
            }
            return null;
        } finally {
            const next = { ...(S.treeBranchSyncInFlightById || {}) };
            if (next[requestKey] === request) delete next[requestKey];
            S.treeBranchSyncInFlightById = next;
        }
    })();
    S.treeBranchSyncInFlightById = { ...(S.treeBranchSyncInFlightById || {}), [requestKey]: request };
    return request;
}

async function syncTaskTreeDirtyBranch(nodeId) {
    const normalizedNodeId = String(nodeId || "").trim();
    const taskId = String(S.currentTaskId || "").trim();
    if (!normalizedNodeId || !S.currentTaskId || !String(S.treeRootNodeId || "").trim()) return;
    if (!taskTreeParentIsDirty(normalizedNodeId)) return;
    const queuedBefore = { ...(S.treeBranchSyncQueuedById || {}) };
    delete queuedBefore[normalizedNodeId];
    S.treeBranchSyncQueuedById = queuedBefore;
    try {
        await ensureTaskTreeSubtree(normalizedNodeId, {
            roundId: resolveTaskTreeBranchRoundId(normalizedNodeId),
            force: true,
        });
    } finally {
        if (String(S.currentTaskId || "").trim() !== taskId) return;
        if (S.treeBranchSyncQueuedById?.[normalizedNodeId] || taskTreeParentIsDirty(normalizedNodeId)) {
            scheduleTaskTreeBranchSync(normalizedNodeId, { delayMs: 0 });
        }
    }
}

function scheduleTaskTreeBranchSync(nodeId, { delayMs = 120 } = {}) {
    const normalizedNodeId = String(nodeId || "").trim();
    if (!normalizedNodeId || !S.currentTaskId || !String(S.treeRootNodeId || "").trim()) return;
    // 已离开详情视图：不再发起懒加载分支修正（返回大厅后重新打开任务会完整重载整树）。
    if (!treeDetailViewActive()) return;
    // 初始分块加载期间跳过懒加载分支修正：等整树块全部落位后由后续 live 事件补同步。
    if (String(S.treeBulkLoadingTaskId || "").trim()
        && String(S.treeBulkLoadingTaskId || "").trim() === String(S.currentTaskId || "").trim()) return;
    markTaskTreeParentDirty(normalizedNodeId);
    if (Object.keys(S.treeBranchSyncInFlightById || {}).some((key) => key.startsWith(`${normalizedNodeId}::`))) {
        S.treeBranchSyncQueuedById = { ...(S.treeBranchSyncQueuedById || {}), [normalizedNodeId]: true };
        return;
    }
    const existingToken = S.treeBranchSyncTokenById?.[normalizedNodeId];
    if (existingToken) window.clearTimeout(existingToken);
    const timeoutId = window.setTimeout(() => {
        const nextTokens = { ...(S.treeBranchSyncTokenById || {}) };
        delete nextTokens[normalizedNodeId];
        S.treeBranchSyncTokenById = nextTokens;
        void syncTaskTreeDirtyBranch(normalizedNodeId);
    }, Math.max(0, Number(delayMs) || 0));
    S.treeBranchSyncTokenById = { ...(S.treeBranchSyncTokenById || {}), [normalizedNodeId]: timeoutId };
}

function pruneTreeRoundSelections(selections, { rootNodeId = S.treeRootNodeId, nodesById = S.treeNodesById } = {}) {
    const source = normalizeTreeRoundSelections(selections);
    const normalizedRootNodeId = String(rootNodeId || "").trim();
    if (!normalizedRootNodeId) return {};
    const next = {};

    const walk = (nodeId, seen = new Set()) => {
        const normalizedNodeId = String(nodeId || "").trim();
        if (!normalizedNodeId || seen.has(normalizedNodeId)) return;
        const node = nodesById?.[normalizedNodeId] || null;
        if (!node) return;
        seen.add(normalizedNodeId);
        if (source[normalizedNodeId] && (Array.isArray(node?.rounds) ? node.rounds : []).some((round) => round.round_id === source[normalizedNodeId])) {
            next[normalizedNodeId] = source[normalizedNodeId];
        }
        snapshotNodeVisibleChildIds(node, source).forEach((childId) => walk(childId, seen));
    };

    walk(normalizedRootNodeId);
    return next;
}

// 节点是否还在自己的回合里：等子节点（派生工具未返回、或等验收结论）与后端标记的
// 陈旧帧都不算在执行。相位集合须与 log_service._frame_index_payload 的 runnable 同源。
// 模型请求在途：await_marker 停在派发或等流两档。response_postprocess 时流已经收尾，
// 不算还在等 API。marker 由 react_loop 在每次 await 前后写入/清空，帧里一直带着。

function hasManualTreeRoundSelections() {
    return Object.keys(normalizeTreeRoundSelections(S.treeSelectedRoundByNodeId)).length > 0;
}

function taskDetailStatusLabel(task) {
    return ({ in_progress: "\u8fd0\u884c\u4e2d", success: "\u5df2\u5b8c\u6210", failed: "\u5931\u8d25", blocked: "\u5df2\u6682\u505c", unpassed: "\u672a\u901a\u8fc7", unknown: "\u672a\u77e5" })[taskStatusKey(task)] || "\u672a\u77e5";
}

function taskInitialPromptText(task = null) {
    return String(task?.user_request || task?.title || task?.final_output || "暂无初始提示词").trim() || "暂无初始提示词";
}

function renderTaskDetailHeader({ resetPromptDisclosure = false } = {}) {
    const task = S.currentTask || null;
    const promptText = taskInitialPromptText(task);
    if (resetPromptDisclosure && U.tdPromptDisclosure) U.tdPromptDisclosure.open = false;
    if (U.tdTitle) {
        U.tdTitle.textContent = promptText;
        U.tdTitle.title = promptText;
    }
    if (U.tdStatus) U.tdStatus.textContent = taskDetailStatusLabel(task);
    if (U.tdStatusPill) U.tdStatusPill.dataset.status = taskStatusKey(task);
}

function syncTaskTreeHeaderState(projectedRoot = null) {
    const hasManual = hasManualTreeRoundSelections();
    const activeNodeCount = projectedRoot ? treeNormalizeInt(projectedRoot?.activeNodeCount, 0) : 0;
    if (U.tdActiveCount) {
        U.tdActiveCount.textContent = String(activeNodeCount);
    }
    if (S.taskSummary && typeof S.taskSummary === "object") {
        S.taskSummary = {
            ...(S.taskSummary || {}),
            active_node_count: activeNodeCount,
        };
    }
    if (U.taskTreeResetRounds) {
        U.taskTreeResetRounds.hidden = !hasManual;
        U.taskTreeResetRounds.disabled = !hasManual;
        U.taskTreeResetRounds.classList.toggle("active", hasManual);
        U.taskTreeResetRounds.title = hasManual
            ? "恢复所有节点的最新树视图"
            : "";
    }
}

function resetTaskTreeRoundSelections() {
    const hadSelections = Object.keys(normalizeTreeRoundSelections(S.treeSelectedRoundByNodeId)).length > 0;
    S.treeSelectedRoundByNodeId = {};
    renderTree();
    scheduleTaskDetailSessionPersist();
    // 选轮次时子树按「先删旧子树再整体重建」合并进缓存，默认轮次的孩子已经被
    // 换掉了；只清选择会让这些节点渲染成空子树（整棵树看着没加载出来），
    // 所以回到最新必须补一次整树快照，让默认轮次的孩子回来。
    if (hadSelections && String(S.currentTaskId || "").trim()) {
        void loadTaskTreeSnapshot(S.currentTaskId);
    }
}

function setNodeRoundSelection(nodeId, roundId) {
    const normalizedNodeId = String(nodeId || "").trim();
    const snapshotNode = treeSnapshotNode(normalizedNodeId);
    if (!normalizedNodeId || !snapshotNode) return;
    const rounds = Array.isArray(snapshotNode?.rounds) ? snapshotNode.rounds : [];
    const normalizedRoundId = String(roundId || "").trim();
    const nextSelections = normalizeTreeRoundSelections(S.treeSelectedRoundByNodeId);
    const defaultRoundId = snapshotNodeDefaultRoundId(snapshotNode);
    if (!normalizedRoundId || normalizedRoundId === defaultRoundId || !rounds.some((round) => round.round_id === normalizedRoundId)) {
        delete nextSelections[normalizedNodeId];
    } else {
        nextSelections[normalizedNodeId] = normalizedRoundId;
    }
    S.treeSelectedRoundByNodeId = nextSelections;
    renderTree();
    scheduleTaskDetailSessionPersist();
    void ensureTaskTreeSubtree(normalizedNodeId, { roundId: normalizedRoundId, force: true });
}

function clearAgentSelection({ rerender = true } = {}) {
    const previousNodeId = String(S.selectedNodeId || "").trim();
    if (previousNodeId) stashTaskDetailViewState({ nodeId: previousNodeId });
    S.selectedNodeId = null;
    S.pendingTaskDetailRestore = null;
    U.feedTitle.textContent = "Node Details";
    U.feedTitle.title = "";
    hideAgent();
    setTaskSelectionEmptyVisible(true);
    scheduleTaskDetailSessionPersist();
    if (rerender) syncExecutionTreeSelection(previousNodeId, "");
}
function findTreeNode(node, nodeId) {
    if (!node) return null;
    if (String(node.node_id || "") === String(nodeId || "")) return node;
    for (const child of treeViewChildren(node)) {
        const found = findTreeNode(child, nodeId);
        if (found) return found;
    }
    return null;
}

function bindTreePan() {
    if (!U.tree || U.tree.dataset.panBound === "true") return;
    U.tree.dataset.panBound = "true";
    const state = S.treePan;
    let panRaf = 0;
    const commitPan = () => {
        const canvas = U.tree?.querySelector(".execution-tree");
        if (canvas) {
            canvas.style.transformOrigin = "0 0";
            canvas.style.transform = `translate(${Math.round(state.offsetX)}px, ${Math.round(state.offsetY)}px) scale(${state.scale})`;
        }
    };
    const applyPan = () => {
        if (panRaf) return;
        panRaf = window.requestAnimationFrame(() => {
            panRaf = 0;
            commitPan();
        });
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
        if (e.target.closest(".execution-tree-node, .execution-tree-node-rounds")) return;
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
        if (e.target.closest(".execution-tree-node-rounds")) return;
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

// 树节点的中文状态标签。判据只有两条：节点自己的运行相位（live 帧）和验收子节点
// 是否还没结论——两者都由 task.live.patch / task.node.patch 实时推送，不依赖
// 整树重拉。非终态节点一律不落英文状态原文：没在执行就是等待中。
function indexTaskLiveFrames(frames) {
    return Object.fromEntries(
        (Array.isArray(frames) ? frames : [])
            .map((frame) => [String(frame?.node_id || "").trim(), frame])
            .filter(([nodeId]) => !!nodeId),
    );
}

// live.patch 有两种载荷：全量（带 frames 数组）与增量（frames_partial=true，只带变化的
// 那一帧 + 三个角色名单 + stale 名单）。实盘一份全量摘要 4.3 MB / 173 帧，而单帧几十 KB，
// 所以节点集合不变时服务端不再重发整表。增量并进现有帧表：顺序保持原样，stale 就地刷新
// （后端每帧的 stale 是按写入选的当下时刻算的，增量档靠 stale_node_ids 带过来）。
function mergeTaskLiveFrameDelta({ frames, frame, staleNodeIds } = {}) {
    if (Array.isArray(frames)) {
        S.frontier = frames;
        S.liveFrameMap = indexTaskLiveFrames(frames);
        return frames;
    }
    const frontier = Array.isArray(S.frontier) ? S.frontier : [];
    const stale = new Set(
        (Array.isArray(staleNodeIds) ? staleNodeIds : [])
            .map((item) => String(item || "").trim())
            .filter(Boolean),
    );
    frontier.forEach((item) => {
        if (!item || item === frame) return;
        item.stale = stale.has(String(item.node_id || "").trim());
    });
    const nodeId = String(frame?.node_id || "").trim();
    if (!nodeId) return frontier;
    let replaced = false;
    frontier.forEach((item, index) => {
        if (replaced || String(item?.node_id || "").trim() !== nodeId) return;
        frontier[index] = frame;
        replaced = true;
    });
    S.frontier = replaced ? frontier : [...frontier, frame];
    S.liveFrameMap = { ...(S.liveFrameMap || {}), [nodeId]: frame };
    return S.frontier;
}

function liveFramesByNodeId() {
    const frames = Object.entries(S.liveFrameMap || {});
    return new Map(
        frames
            .map(([nodeId, frame]) => [String(nodeId || "").trim(), frame])
            .filter(([nodeId]) => !!nodeId),
    );
}

function buildLiveSectionStatus(items) {
    if (!Array.isArray(items) || !items.length) return "info";
    if (items.some((item) => String(item?.status || "") === "running" || String(item?.status || "") === "queued")) {
        return "running";
    }
    if (items.some((item) => String(item?.status || "") === "error")) {
        return "error";
    }
    if (items.some((item) => String(item?.status || "") === "success")) {
        return "success";
    }
    return "info";
}

// CEO 轨道的 arguments_text 是 `_tool_invocation_hint` 的 48 字截断提示（模型-facing
// 同一份），展开面板要的是原始入参对象，不能拿提示当正文印。
const CONTEXT_LOAD_TITLES = { skill: "加载 skill", tool: "加载 tool" };
const CONTEXT_LOAD_HIDDEN_STATUSES = new Set(["error", "warning", "interrupted"]);

function syncNoticeComposerState() {
    const composer = U.adNoticeComposer;
    const input = U.adNoticeInput;
    const send = U.adNoticeSend;
    if (!composer || !input || !send) return;
    const nodeId = String(S.selectedNodeId || S.currentNodeDetail?.node_id || "").trim();
    const node = nodeId ? (treeSnapshotNode(nodeId) || S.taskNodeDetails?.[nodeId] || null) : null;
    const kind = String(node?.node_kind || node?.kind || "execution").trim().toLowerCase();
    const status = String(node?.status || node?.state || "").trim().toLowerCase();
    const taskStatus = String(S.currentTask?.status || "").trim().toLowerCase();
    const nodeTerminal = status === "success" || status === "failed";
    const taskDone = taskStatus === "success" || taskStatus === "failed";
    const isAcceptance = isAcceptanceNodeKind(kind);
    const usable = !!nodeId && !!node && !isAcceptance && !nodeTerminal && !taskDone;
    composer.hidden = !nodeId;
    input.disabled = !usable;
    if (String(send.dataset.busy || "") === "true") {
        send.disabled = true;
    } else {
        send.disabled = !usable;
    }
    input.placeholder = !nodeId
        ? "选择节点后可追加定向通知…"
        : isAcceptance
            ? "验收节点不能作为通知目标"
            : nodeTerminal
                ? "终态节点不接收通知"
                : taskDone
                    ? "任务已结束"
                    : "向该节点子树追加定向通知…";
}


async function submitNodeNoticeComposer() {
    const input = U.adNoticeInput;
    const send = U.adNoticeSend;
    if (!input || !send || input.disabled || send.disabled) return;
    const message = String(input.value || "").trim();
    const nodeId = String(S.selectedNodeId || S.currentNodeDetail?.node_id || "").trim();
    const taskId = String(S.currentTaskId || "").trim();
    if (!message || !nodeId || !taskId) return;
    send.disabled = true;
    send.dataset.busy = "true";
    try {
        await ApiClient.appendTaskNodeNotice(taskId, nodeId, message);
        input.value = "";
        showToast({
            title: "定向通知已提交",
            text: `已对节点 ${nodeId} 的子树发起消息分发`,
            kind: "success",
        });
        await loadTaskTreeSnapshot(taskId);
        renderTree();
        const selected = findTreeNode(S.treeView, nodeId);
        if (selected) void showAgent(selected, { preserveViewState: true, forceRefresh: true });
        // 分发是异步的：POST 返回时新消息通常还没落到目标节点的消息列表，
        // 启动局部 settle 轮询，新消息一经记账即局部刷新展示（见下方实现）。
        settleTaskNoticeAfterSend(taskId, nodeId, message);
    } catch (error) {
        showToast({ title: "定向通知提交失败", text: error?.message || "提交未完成", kind: "error" });
    } finally {
        send.dataset.busy = "";
        syncNoticeComposerState();
    }
}


function taskNoticeSettleWindowOpen(now) {
    // 分发 epoch 已删除：不再存在"分发窗口内不收窗"的特殊宽容，按普通超时收窗。
    void now;
    return false;
}


function taskNoticeSettleState() {
    if (!S.noticeSettle || typeof S.noticeSettle !== "object") {
        S.noticeSettle = { timer: null, taskId: "", nodeId: "", pending: [] };
    }
    return S.noticeSettle;
}


function stopTaskNoticeSettle() {
    const state = S.noticeSettle;
    if (!state) return;
    if (state.timer) {
        window.clearTimeout(state.timer);
        state.timer = null;
    }
    state.pending = [];
}


function settleTaskNoticeAfterSend(taskId, nodeId, message) {
    const trimmed = String(message || "").trim();
    const targetNodeId = String(nodeId || "").trim();
    const targetTaskId = String(taskId || "").trim();
    if (!trimmed || !targetNodeId || !targetTaskId) return;
    const state = taskNoticeSettleState();
    if (String(state.taskId || "").trim() !== targetTaskId || String(state.nodeId || "").trim() !== targetNodeId) {
        // 换了任务/节点：旧的轮询作废，从当前节点重新开始。
        stopTaskNoticeSettle();
        state.taskId = targetTaskId;
        state.nodeId = targetNodeId;
    }
    const alreadyPending = state.pending.some(
        (item) => item.nodeId === targetNodeId && item.message === trimmed,
    );
    if (!alreadyPending) {
        state.pending.push({ nodeId: targetNodeId, message: trimmed, addedAt: Date.now() });
    }
    if (!state.timer) {
        state.timer = window.setTimeout(() => {
            void runTaskNoticeSettleTick();
        }, TASK_NOTICE_SETTLE_INTERVAL_MS);
    }
}


async function runTaskNoticeSettleTick() {
    const state = S.noticeSettle;
    if (!state) return;
    state.timer = null;
    const taskId = String(state.taskId || "").trim();
    const nodeId = String(state.nodeId || "").trim();
    if (!taskId || !nodeId) {
        stopTaskNoticeSettle();
        return;
    }
    if (String(S.currentTaskId || "").trim() !== taskId || !treeDetailViewActive()) {
        stopTaskNoticeSettle();
        return;
    }
    try {
        const detail = await ApiClient.getTaskNodeDetail(taskId, nodeId, { detailLevel: "full" });
        if (!S.noticeSettle || !S.noticeSettle.pending.length) return;
        if (String(S.currentTaskId || "").trim() !== taskId) {
            stopTaskNoticeSettle();
            return;
        }
        if (detail && typeof detail === "object") {
            S.taskNodeDetails = { ...(S.taskNodeDetails || {}), [nodeId]: detail };
            if (String(S.selectedNodeId || "").trim() === nodeId) S.currentNodeDetail = detail;
            const messages = Array.isArray(detail.message_list) ? detail.message_list : [];
            const remaining = [];
            let matched = false;
            const now = Date.now();
            const settleWindowOpen = taskNoticeSettleWindowOpen(now);
            for (const item of state.pending) {
                if (!settleWindowOpen && now - item.addedAt > TASK_NOTICE_SETTLE_TIMEOUT_MS) continue;
                if (messages.some((entry) => String(entry?.message || "").trim() === item.message)) {
                    matched = true;
                } else {
                    remaining.push(item);
                }
            }
            state.pending = remaining;
            if (matched && String(S.selectedNodeId || "").trim() === nodeId) {
                const selected = findTreeNode(S.treeView, nodeId)
                    || { node_id: nodeId, title: nodeId, state: "in_progress" };
                void showAgent(selected, { preserveViewState: true });
                // 这不是动作回执而是"新行到屏"的通知：列表原地重绘且不吃滚动位置，
                // 他停在上面几行时新行落在屏外，所以这条浮层要留着兜底。
                if (!remaining.length) {
                    showToast({
                        title: "消息已更新",
                        text: "新增消息已出现在节点消息列表",
                        kind: "success",
                    });
                }
            }
        }
    } catch (error) {
        // 单轮失败不中断局部刷新（接口抖动）；条目按各自超时退出，
        // 持续失败时轮询仍需按超时终止而不是无限重试。
        // 分发窗口内不收窗：分发期间恰恰是最容易因负载抖动的一轮。
        const now = Date.now();
        const settleWindowOpen = taskNoticeSettleWindowOpen(now);
        state.pending = state.pending.filter(
            (item) => settleWindowOpen || now - item.addedAt <= TASK_NOTICE_SETTLE_TIMEOUT_MS,
        );
    }
    if (!state.pending.length) return;
    state.timer = window.setTimeout(() => {
        void runTaskNoticeSettleTick();
    }, TASK_NOTICE_SETTLE_INTERVAL_MS);
}


function refreshTaskDetailScrollRegions() {
    const traceList = U.adFlow?.querySelector(".task-trace-list");
    const messageList = U.adMessages?.querySelector(".task-trace-list");
    const spawnReviewList = U.adSpawnReviews?.querySelector(".task-trace-list");
    if (traceList instanceof HTMLElement) {
        traceList.style.height = "";
        traceList.style.maxHeight = "";
    }
    if (messageList instanceof HTMLElement) {
        messageList.style.height = "";
        messageList.style.maxHeight = "";
    }
    if (spawnReviewList instanceof HTMLElement) {
        spawnReviewList.style.height = "";
        spawnReviewList.style.maxHeight = "";
    }
    if (U.artifactList instanceof HTMLElement) {
        U.artifactList.style.height = "";
        U.artifactList.style.maxHeight = "";
    }
}


function formatRoundLabel(label, roundIndex) {
    const normalizedLabel = String(label || "").trim();
    const normalizedIndex = treeNormalizeInt(roundIndex, 0);
    const fallbackLabel = normalizedIndex > 0 ? `第${normalizedIndex}轮树` : "轮次";
    if (!normalizedLabel) return fallbackLabel;
    const matchedRound = normalizedLabel.match(/^Round\s+(\d+)$/i);
    if (matchedRound) return `第${matchedRound[1]}轮树`;
    return normalizedLabel;
}


function executionTreeNodeSelector(nodeId) {
    const key = String(nodeId || "").trim();
    if (!key || !U.tree) return "";
    const escaped = key
        .replace(/\\/g, "\\\\")
        .replace(/"/g, '\\"');
    return `.execution-tree-node[data-id="${escaped}"]`;
}


function treeRoundSelectId(nodeId) {
    const key = String(nodeId || "").trim();
    if (!key) return "";
    return `tree-round-${key.replace(/[^a-zA-Z0-9_-]/g, "-")}`;
}


function refreshTreeViewFromSnapshot() {
    S.treeView = buildExecutionTreeFromSnapshot(S.treeRootNodeId, S.treeSelectedRoundByNodeId);
    return S.treeView;
}


function syncExecutionTreeSelection(previousNodeId, nextNodeId) {
    if (!U.tree) return;
    const previousKey = String(previousNodeId || "").trim();
    const nextKey = String(nextNodeId || "").trim();
    if (previousKey) {
        const previousButton = U.tree.querySelector(executionTreeNodeSelector(previousKey));
        if (previousButton instanceof HTMLElement) {
            previousButton.classList.remove("selected");
            previousButton.setAttribute("aria-pressed", "false");
        }
    }
    if (nextKey) {
        const nextButton = U.tree.querySelector(executionTreeNodeSelector(nextKey));
        if (nextButton instanceof HTMLElement) {
            nextButton.classList.add("selected");
            nextButton.setAttribute("aria-pressed", "true");
        }
    }
}




function treeNodeHasActiveChildren(node) {
    return treeViewChildren(node).some((child) => {
        const status = String(child?.state || child?.status || "").trim().toLowerCase();
        return !isTerminalTreeNodeStatus(status)
            && !child?.is_paused
            && !child?.effective_is_paused;
    });
}


async function submitTreeNodePause(taskId, nodeId, { cascade = false } = {}) {
    try {
        await ApiClient.pauseTaskNode(taskId, nodeId, { cascade: !!cascade });
        showToast({
            title: "节点暂停请求已提交",
            text: cascade ? "已包含所有子节点" : nodeId,
            kind: "success",
        });
        await loadTaskTreeSnapshot(taskId);
        renderTree();
    } catch (error) {
        showToast({ title: "节点操作失败", text: error?.message || "操作未完成", kind: "error" });
    }
}


async function handleTreeNodePauseAction(node, event) {
    event?.preventDefault?.();
    event?.stopPropagation?.();
    if (taskPauseDisplayActive()) {
        showToast({
            title: "任务已暂停",
            text: "请先恢复任务后再操作单个节点",
            kind: "warn",
        });
        return;
    }
    const taskId = String(S.currentTaskId || "").trim();
    const nodeId = String(node?.node_id || "").trim();
    if (!taskId || !nodeId) return;
    try {
        if (node?.is_paused) {
            await ApiClient.resumeTaskNode(taskId, nodeId);
            showToast({ title: "节点已恢复", text: nodeId, kind: "success" });
            await loadTaskTreeSnapshot(taskId);
            renderTree();
            return;
        }
        // 暂停一律二次确认：单节点暂停同样会打断在跑的执行，不能点一下就走。
        // 只有存在活跃子节点时才提供级联勾选（没有子节点就没有可级联的对象）。
        const hasActiveChildren = treeNodeHasActiveChildren(node);
        openConfirm({
            title: "暂停节点",
            text: hasActiveChildren
                ? "暂停父节点本身不会自动停止子节点。"
                : "暂停后该节点将停止执行，需要手动恢复。",
            confirmLabel: "暂停节点",
            confirmKind: "danger",
            returnFocus: event?.currentTarget || null,
            checkbox: hasActiveChildren
                ? {
                    label: "同时暂停所有子节点（包括检验节点）",
                    checked: false,
                }
                : null,
            onConfirm: async ({ checked } = {}) => submitTreeNodePause(taskId, nodeId, { cascade: !!checked }),
        });
    } catch (error) {
        showToast({ title: "节点操作失败", text: error?.message || "操作未完成", kind: "error" });
    }
}


function taskTreeTopOverlayInset(viewport) {
    let inset = 0;
    [U.taskTreeSearch, U.treeFloatingTools].forEach((element) => {
        if (!(element instanceof Element)) return;
        const rect = element.getBoundingClientRect();
        if (!rect.height || !rect.width) return;
        inset = Math.max(inset, rect.bottom - viewport.top + 8);
    });
    return Math.min(Math.max(0, inset), viewport.height * 0.6);
}

function fitTaskTreeToView({ marginPx = 40 } = {}) {
    if (!U.tree) return false;
    const wrapper = U.tree.querySelector(".execution-tree");
    if (!wrapper) return false;
    // 内容容器可能是块级元素,宽度默认为容器宽度;必须测量树的真实内容盒
    const content = wrapper.firstElementChild instanceof Element ? wrapper.firstElementChild : wrapper;
    const state = S.treePan;
    if (!state.scale || state.scale <= 0) return false;
    const viewport = U.tree.getBoundingClientRect();
    if (!viewport.width || !viewport.height) return false;
    const wrapRect = wrapper.getBoundingClientRect();
    const contentRect = content.getBoundingClientRect();
    if (!contentRect.width || !contentRect.height) return false;
    // 布局位置在变换下是不变量。变换只缩放内容在包装器内的偏移,
    // 包装器自身的布局偏移不参与缩放,须分开计算:
    // W = 包装器相对视口的布局偏移;C = 内容相对包装器的布局偏移。
    // 包装器自身的布局偏移不参与 scale（transform-origin 0 0 下 rendered = layout + offset），
    // 只有容器内部的 C/D 与树尺寸需要除以 scale。此前 W/T 也被除了一次，布局偏移因此
    // 被放大 1/scale 倍——内边距一变大，树就被推离视口（整棵树看起来是空的）。
    const W = wrapRect.left - viewport.left - state.offsetX;
    const T = wrapRect.top - viewport.top - state.offsetY;
    const C = (contentRect.left - wrapRect.left) / state.scale;
    const D = (contentRect.top - wrapRect.top) / state.scale;
    const treeW = contentRect.width / state.scale;
    const treeH = contentRect.height / state.scale;
    // 悬浮搜索框与悬浮工具钉在树容器的上半部，适配时把它们的高度从可用区域里扣掉：
    // 否则树按整块容器居中，第一行会正好压在搜索框底下（顶部越挤越明显）。
    const topInset = taskTreeTopOverlayInset(viewport);
    const areaTop = viewport.top + topInset;
    const areaHeight = viewport.height - topInset;
    const margin = Math.min(marginPx, Math.max(8, areaHeight * 0.1));
    const availW = viewport.width - margin * 2;
    const availH = areaHeight - margin * 2;
    if (availW <= 0 || availH <= 0 || treeW <= 0 || treeH <= 0) return false;
    const fitScale = Math.min(availW / treeW, availH / treeH, 1);
    const nextScale = clamp(fitScale, TREE_SCALE_MIN, TREE_SCALE_MAX);
    // 树大到连 TREE_SCALE_MIN 都放不下时，居中"内容盒"等于把用户对准分支之间的空白：
    // 几百节点的宽幅组织图会整片看不见（实测内容 40488×2347、视口 611×131 ⇒ 需要 0.013，
    // 被夹到 0.12）。这时改为把根节点摆进视野：先按原公式落一次 transform，再按渲染像素
    // 差值平移——差值法不依赖 min-scale 夹住时的 T/D 布局不变量口径。
    const anchorRootId = fitScale < TREE_SCALE_MIN - 1e-6
        ? String(S.treeRootNodeId || "").trim()
        : "";
    const anchorButton = anchorRootId
        ? U.tree.querySelector(executionTreeNodeSelector(anchorRootId))
        : null;
    const nextOffsetX = viewport.width / 2 - W - nextScale * (C + treeW / 2);
    const nextOffsetY = (areaTop - viewport.top) + areaHeight / 2 - T - nextScale * (D + treeH / 2);
    if (!anchorButton
        && Math.abs(nextScale - state.scale) < 0.001
        && Math.abs(nextOffsetX - state.offsetX) < 0.5
        && Math.abs(nextOffsetY - state.offsetY) < 0.5
    ) return true;
    state.scale = nextScale;
    state.baseScale = nextScale;
    state.offsetX = nextOffsetX;
    state.offsetY = nextOffsetY;
    state.baseOffsetX = nextOffsetX;
    state.baseOffsetY = nextOffsetY;
    wrapper.style.transform = `translate(${Math.round(state.offsetX)}px, ${Math.round(state.offsetY)}px) scale(${state.scale})`;
    if (anchorButton instanceof HTMLElement) {
        const nodeRect = anchorButton.getBoundingClientRect();
        const dx = (viewport.left + viewport.width / 2) - (nodeRect.left + nodeRect.width / 2);
        const dy = (viewport.top + viewport.height / 2) - (nodeRect.top + nodeRect.height / 2);
        if (Math.abs(dx) > 0.5 || Math.abs(dy) > 0.5) {
            state.offsetX += dx;
            state.offsetY += dy;
            state.baseOffsetX = state.offsetX;
            state.baseOffsetY = state.offsetY;
            wrapper.style.transform = `translate(${Math.round(state.offsetX)}px, ${Math.round(state.offsetY)}px) scale(${state.scale})`;
        }
    }
    return true;
}


function substringMatchScore(textLower, needleLower) {
    if (!textLower || !needleLower) return -1;
    if (textLower === needleLower) return 3;
    if (textLower.startsWith(needleLower)) return 2;
    if (textLower.includes(needleLower)) return 1;
    return -1;
}


function searchTaskNodes(query, { limit = 30 } = {}) {
    const needle = String(query ?? "").trim().toLowerCase();
    if (!needle) return [];
    const nodesById = S.treeNodesById && typeof S.treeNodesById === "object" ? S.treeNodesById : {};
    const matches = [];
    Object.values(nodesById).forEach((node) => {
        if (!node || typeof node !== "object") return;
        const nodeId = String(node.node_id || "").trim();
        if (!nodeId) return;
        const title = String(node.title || "").trim();
        const idScore = substringMatchScore(nodeId.toLowerCase(), needle);
        const titleScore = substringMatchScore(title.toLowerCase(), needle);
        if (idScore < 0 && titleScore < 0) return;
        matches.push({ node, nodeId, title, score: Math.max(idScore, titleScore) });
    });
    matches.sort((a, b) => (b.score - a.score) || (a.title.length - b.title.length) || a.nodeId.localeCompare(b.nodeId));
    return matches.slice(0, Math.max(1, Number(limit) || 30));
}


function centerTaskNodeInView(targetId) {
    const normalizedTargetId = String(targetId || "").trim();
    if (!normalizedTargetId || !U.tree) return false;
    const button = U.tree.querySelector(executionTreeNodeSelector(normalizedTargetId));
    if (!(button instanceof HTMLElement)) return false;
    U.tree.scrollTop = 0;
    const containerRect = U.tree.getBoundingClientRect();
    const nodeRect = button.getBoundingClientRect();
    S.treePan.offsetX += (containerRect.left + containerRect.width / 2) - (nodeRect.left + nodeRect.width / 2);
    S.treePan.offsetY += (containerRect.top + containerRect.height / 2) - (nodeRect.top + nodeRect.height / 2);
    S.treePan.baseOffsetX = S.treePan.offsetX;
    S.treePan.baseOffsetY = S.treePan.offsetY;
    const canvas = U.tree.querySelector(".execution-tree");
    if (canvas) canvas.style.transform = `translate(${Math.round(S.treePan.offsetX)}px, ${Math.round(S.treePan.offsetY)}px) scale(${S.treePan.scale})`;
    // 运行中的任务会频繁整树重渲染，记录高亮窗口期，重渲染后补挂动画 class
    const until = Date.now() + TREE_LOCATE_HIGHLIGHT_MS;
    S.treeLocateHighlight = { nodeId: normalizedTargetId, until };
    applyTaskTreeLocateHighlight(button);
    window.setTimeout(() => {
        const current = S.treeLocateHighlight;
        if (!current || current.nodeId !== normalizedTargetId || Number(current.until) !== until) return;
        S.treeLocateHighlight = null;
        const stale = U.tree?.querySelector(executionTreeNodeSelector(normalizedTargetId));
        if (stale instanceof HTMLElement) {
            stale.classList.remove("task-tree-node-locate");
            stale.style.animationDelay = "";
        }
    }, TREE_LOCATE_HIGHLIGHT_MS + 80);
    return true;
}


function applyTaskTreeLocateHighlight(button) {
    if (!(button instanceof HTMLElement)) return;
    button.style.animationDelay = "";
    button.classList.remove("task-tree-node-locate");
    void button.offsetWidth;
    button.classList.add("task-tree-node-locate");
}


function maybeReapplyTaskTreeLocateHighlight() {
    const pending = S.treeLocateHighlight;
    if (!pending?.nodeId) return;
    const remaining = Number(pending.until || 0) - Date.now();
    if (remaining <= 0) {
        S.treeLocateHighlight = null;
        return;
    }
    const button = U.tree?.querySelector(executionTreeNodeSelector(pending.nodeId));
    if (!(button instanceof HTMLElement) || button.classList.contains("task-tree-node-locate")) return;
    button.style.animationDelay = `-${TREE_LOCATE_HIGHLIGHT_MS - remaining}ms`;
    button.classList.add("task-tree-node-locate");
}


function nearestVisibleTreeNodeId(nodeId) {
    let currentId = String(nodeId || "").trim();
    const seen = new Set();
    while (currentId && !seen.has(currentId)) {
        seen.add(currentId);
        const node = treeSnapshotNode(currentId) || S.taskNodeDetails?.[currentId] || null;
        const parentId = String(node?.parent_node_id || "").trim();
        if (!parentId) return "";
        if (U.tree?.querySelector(executionTreeNodeSelector(parentId)) instanceof HTMLElement) return parentId;
        currentId = parentId;
    }
    return "";
}


async function locateTaskNode(targetId, { scale = null } = {}) {
    const normalizedTargetId = String(targetId || "").trim();
    if (!normalizedTargetId) return { located: false, reason: "missing" };
    const target = treeSnapshotNode(normalizedTargetId) || S.taskNodeDetails?.[normalizedTargetId] || null;
    if (!target) return { located: false, reason: "missing" };
    const path = [];
    let current = target;
    while (current) {
        const currentId = String(current?.node_id || "").trim();
        if (!currentId || path.includes(currentId)) break;
        path.push(currentId);
        const parentId = String(current?.parent_node_id || "").trim();
        current = parentId ? (treeSnapshotNode(parentId) || S.taskNodeDetails?.[parentId] || null) : null;
    }
    for (const parentId of path.slice(1).reverse()) {
        const parent = treeSnapshotNode(parentId);
        const childId = path[path.indexOf(parentId) - 1];
        // 定位一个被默认过滤藏起来的终态节点：把它所在的每一级父节点展开，
        // 否则命中后仍无处高亮，只能退回到最近的可见祖先。
        treeExpandedNodeIds().add(parentId);
        const round = (Array.isArray(parent?.rounds) ? parent.rounds : []).find((item) => Array.isArray(item?.child_ids) && item.child_ids.includes(childId));
        if (round?.round_id) S.treeSelectedRoundByNodeId[parentId] = round.round_id;
        if (typeof ensureTaskTreeSubtree === "function") {
            await ensureTaskTreeSubtree(parentId, { roundId: round?.round_id || "", force: true }).catch(() => null);
        }
    }
    if (scale !== null && scale !== undefined) {
        const nextScale = clamp(Number(scale) || 1, TREE_SCALE_MIN, TREE_SCALE_MAX);
        S.treePan.scale = nextScale;
        S.treePan.baseScale = nextScale;
    }
    renderTree();
    return new Promise((resolve) => {
        window.requestAnimationFrame(() => {
            if (centerTaskNodeInView(normalizedTargetId)) {
                resolve({ located: true, nodeId: normalizedTargetId });
                return;
            }
            const fallbackNodeId = nearestVisibleTreeNodeId(normalizedTargetId);
            if (fallbackNodeId && centerTaskNodeInView(fallbackNodeId)) {
                resolve({ located: false, reason: "hidden", fallbackNodeId });
                return;
            }
            resolve({ located: false, reason: "hidden" });
        });
    });
}


function createTaskTreeSearchItem(match) {
    const { node, nodeId, title } = match;
    const item = document.createElement("button");
    item.type = "button";
    item.className = "task-tree-search-item";
    item.dataset.nodeId = nodeId;
    item.setAttribute("role", "option");
    const status = String(node?.status || "").trim();
    const goal = title || nodeId;
    const statusBadge = status
        ? `<span class="status-badge task-tree-search-item-status" data-status="${esc(status)}">${esc(status)}</span>`
        : "";
    item.innerHTML = `<span class="task-tree-search-item-goal">${esc(goal)}</span>
        <span class="task-tree-search-item-meta"><span class="task-tree-search-item-id">${esc(nodeId)}</span>${statusBadge}</span>`;
    // preventDefault 保持输入框焦点，点击结果后搜索框内容不被重置
    item.addEventListener("mousedown", (event) => event.preventDefault());
    item.addEventListener("click", () => { void jumpTaskTreeSearchToNode(nodeId); });
    return item;
}


function renderTaskTreeSearchResults() {
    const input = U.taskTreeSearchInput;
    const results = U.taskTreeSearchResults;
    if (!input || !results) return;
    const trimmed = String(input.value || "").trim();
    const previousActiveIndex = Number(results.dataset.activeIndex || -1);
    results.innerHTML = "";
    if (!trimmed) {
        results.hidden = true;
        results.dataset.activeIndex = "-1";
        return;
    }
    const matches = searchTaskNodes(trimmed);
    if (!matches.length) {
        const empty = document.createElement("div");
        empty.className = "task-tree-search-empty";
        empty.textContent = "未找到匹配的节点";
        results.appendChild(empty);
        results.dataset.activeIndex = "-1";
    } else {
        matches.forEach((match) => results.appendChild(createTaskTreeSearchItem(match)));
        results.dataset.activeIndex = String(
            previousActiveIndex >= 0 && previousActiveIndex < matches.length ? previousActiveIndex : -1,
        );
    }
    results.hidden = false;
}


function setTaskTreeSearchActiveIndex(index) {
    const results = U.taskTreeSearchResults;
    if (!results) return;
    const items = Array.from(results.querySelectorAll(".task-tree-search-item"));
    if (!items.length) {
        results.dataset.activeIndex = "-1";
        return;
    }
    const nextIndex = (index + items.length) % items.length;
    results.dataset.activeIndex = String(nextIndex);
    items.forEach((item, itemIndex) => item.classList.toggle("is-active", itemIndex === nextIndex));
    items[nextIndex]?.scrollIntoView({ block: "nearest" });
}


async function jumpTaskTreeSearchToNode(nodeId) {
    const normalizedNodeId = String(nodeId || "").trim();
    if (!normalizedNodeId) return;
    // 定位不清空搜索框与结果列表，方便在候选节点间连续跳转
    const result = await locateTaskNode(normalizedNodeId, { scale: TREE_FOCUS_SCALE });
    if (typeof showToast !== "function") return;
    if (result?.located) return;
    if (result?.fallbackNodeId) {
        const fallback = treeSnapshotNode(result.fallbackNodeId) || S.taskNodeDetails?.[result.fallbackNodeId] || null;
        const fallbackTitle = truncateNodeTitle(String(fallback?.title || result.fallbackNodeId).trim() || result.fallbackNodeId, 24);
        showToast({
            title: "目标节点当前未显示",
            text: `已改为定位到其父节点「${fallbackTitle}」`,
            kind: "warn",
        });
        return;
    }
    showToast({ title: "无法定位节点", text: "该节点当前未在任务树中显示", kind: "warn" });
}


function refreshTaskTreeSearchResultsIfVisible() {
    const results = U.taskTreeSearchResults;
    const input = U.taskTreeSearchInput;
    if (!results || !input || results.hidden) return;
    renderTaskTreeSearchResults();
}


function bindTaskTreeSearch() {
    const input = U.taskTreeSearchInput;
    const results = U.taskTreeSearchResults;
    if (!input || !results || input.dataset.searchBound === "true") return;
    input.dataset.searchBound = "true";
    input.addEventListener("input", renderTaskTreeSearchResults);
    input.addEventListener("focus", () => {
        if (String(input.value || "").trim()) renderTaskTreeSearchResults();
    });
    input.addEventListener("keydown", (event) => {
        if (event.key === "ArrowDown" || event.key === "ArrowUp") {
            if (results.hidden) return;
            event.preventDefault();
            const current = Number(results.dataset.activeIndex || -1);
            setTaskTreeSearchActiveIndex(event.key === "ArrowDown" ? current + 1 : current - 1);
            return;
        }
        if (event.key === "Enter") {
            if (results.hidden) return;
            event.preventDefault();
            const items = Array.from(results.querySelectorAll(".task-tree-search-item"));
            if (!items.length) return;
            const activeIndex = Number(results.dataset.activeIndex || -1);
            const target = items[activeIndex] || items[0];
            target.click();
            return;
        }
        if (event.key === "Escape") {
            // 阻止 type="search" 的原生清空行为：仅收起下拉，保留已输入内容
            event.preventDefault();
            if (!results.hidden) {
                results.hidden = true;
                results.dataset.activeIndex = "-1";
            }
        }
    });
    document.addEventListener("mousedown", (event) => {
        const targetEl = event.target instanceof Element ? event.target : null;
        if (targetEl?.closest("#task-tree-search")) return;
        if (!results.hidden) {
            results.hidden = true;
            results.dataset.activeIndex = "-1";
        }
    });
}



function currentTaskRecoveryNotice() {
    return String(S.currentTask?.metadata?.recovery_notice || "").trim();
}


function appToastTextEl() {
    return document.getElementById("app-toast-text");
}


function recoveryNoticeDismissalsFromStorage() {
    try {
        const raw = window.localStorage?.getItem?.(RECOVERY_NOTICE_DISMISSALS_STORAGE_KEY);
        const parsed = raw ? JSON.parse(raw) : null;
        return parsed && typeof parsed === "object" ? parsed : {};
    } catch {
        return {};
    }
}


function writeRecoveryNoticeDismissalsToStorage(value) {
    try {
        window.localStorage?.setItem?.(RECOVERY_NOTICE_DISMISSALS_STORAGE_KEY, JSON.stringify(value));
    } catch { }
}


function taskRecoveryNoticeDismissals() {
    // The tree re-arms the notice on every render, so an in-memory set would
    // re-toast a notice the user already closed after a reload or a server
    // restart. Dismissals are durable per task; the stored notice text is the
    // signature, so a reworded notice shows once again.
    if (!S.taskRecoveryNoticeDismissals) {
        S.taskRecoveryNoticeDismissals = recoveryNoticeDismissalsFromStorage();
    }
    return S.taskRecoveryNoticeDismissals;
}


function isTaskRecoveryNoticeDismissed(taskId, notice) {
    return taskRecoveryNoticeDismissals()[taskId] === notice;
}


function dismissTaskRecoveryNotice(taskId, notice) {
    taskRecoveryNoticeDismissals()[taskId] = notice;
    writeRecoveryNoticeDismissalsToStorage(S.taskRecoveryNoticeDismissals);
}


function recoveryNoticeToastCurrentlyShown(notice) {
    // The global toast is a singleton: other notifications may overwrite it.
    // Treat the recovery notice as on-screen only while the toast actually
    // displays this text, so an overwrite re-arms the notice on a later
    // render instead of silently losing it.
    const toast = document.getElementById("app-toast");
    if (!toast || !toast.classList.contains("is-open")) return false;
    const textEl = appToastTextEl();
    return !!textEl && String(textEl.textContent || "").trim() === notice;
}


function ensureTaskRecoveryNoticeToastBound() {
    // One-time binding: clicking anywhere on the toast dismisses the recovery
    // notice for the task that produced it. The built-in close button keeps
    // working through its own handler; the bubbling click below also runs.
    if (S.taskRecoveryNoticeToastBound) return;
    S.taskRecoveryNoticeToastBound = true;
    const toast = document.getElementById("app-toast");
    if (!toast) return;
    toast.addEventListener("click", () => {
        const active = S.taskRecoveryNoticeActive;
        const textEl = appToastTextEl();
        const isOurToast = !!active && !!textEl && String(textEl.textContent || "").trim() === active.text;
        if (isOurToast) dismissTaskRecoveryNotice(active.taskId, active.text);
        S.taskRecoveryNoticeActive = null;
        if (typeof closeToast === "function") closeToast();
    });
}


function maybeShowTaskRecoveryNoticeToast() {
    // "本任务遇到异常停止..." renders as a dismissible toast instead of a
    // sticky in-tree bubble. Shown once per task until the user closes it;
    // re-armed if another toast overwrote it before dismissal.
    const notice = currentTaskRecoveryNotice();
    const taskId = String(S.currentTask?.task_id || "").trim();
    if (!notice || !taskId) return;
    if (isTaskRecoveryNoticeDismissed(taskId, notice)) return;
    if (recoveryNoticeToastCurrentlyShown(notice)) return;
    ensureTaskRecoveryNoticeToastBound();
    S.taskRecoveryNoticeActive = { taskId, text: notice };
    if (typeof showToast === "function") {
        showToast({ title: "任务自动恢复", text: notice, kind: "warn", persistent: true });
    }
}


function renderArtifacts() {
    if (!U.artifactList) return;
    const visibleFiles = getFileChangesForSelectedNode();
    const emptyText = S.selectedNodeId ? "This node has no tracked file changes yet." : "Select a node to view file changes.";
    U.artifactList.innerHTML = "";
    if (!visibleFiles.length) {
        U.artifactList.innerHTML = `<div class="empty-state" style="padding: 10px;">${esc(emptyText)}</div>`;
        renderArtifactHeading(0);
        refreshTaskDetailScrollRegions();
        return;
    }
    visibleFiles.forEach((item) => {
        const changeVisual = describeFileChange(item?.change_type);
        const row = document.createElement("div");
        row.className = "artifact-item";
        row.dataset.changeType = changeVisual.type;
        row.innerHTML = `
            <strong class="artifact-item-path" title="${esc(item?.path || "")}">${esc(item?.path || "")}</strong>
            <span class="artifact-item-state artifact-item-state--${esc(changeVisual.type)}" role="img" aria-label="${esc(changeVisual.label)}" title="${esc(changeVisual.label)}">
                ${changeVisual.iconSvg}
            </span>
        `;
        U.artifactList.appendChild(row);
    });
    renderArtifactHeading(visibleFiles.length);
    refreshTaskDetailScrollRegions();
}


function normalizeFileChangeType(changeType) {
    const normalized = String(changeType || "modified").trim().toLowerCase();
    if (normalized === "created" || normalized === "deleted") return normalized;
    return "modified";
}


function describeFileChange(changeType) {
    const type = normalizeFileChangeType(changeType);
    if (type === "created") {
        return {
            type,
            label: "Created file",
            iconSvg: `
                <svg viewBox="0 0 24 24" fill="none" aria-hidden="true">
                    <path d="M14 2H7a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V7z"></path>
                    <path d="M14 2v5h5"></path>
                    <path d="M12 12v6"></path>
                    <path d="M9 15h6"></path>
                </svg>
            `.trim(),
        };
    }
    if (type === "deleted") {
        return {
            type,
            label: "Deleted file",
            iconSvg: `
                <svg viewBox="0 0 24 24" fill="none" aria-hidden="true">
                    <path d="M3 6h18"></path>
                    <path d="M8 6V4a1 1 0 0 1 1-1h6a1 1 0 0 1 1 1v2"></path>
                    <path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"></path>
                    <path d="M10 11v6"></path>
                    <path d="M14 11v6"></path>
                </svg>
            `.trim(),
        };
    }
    return {
        type: "modified",
        label: "Modified file",
        iconSvg: `
            <svg viewBox="0 0 24 24" fill="none" aria-hidden="true">
                <path d="M14 2H7a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V7z"></path>
                <path d="M14 2v5h5"></path>
                <path d="M9 15.5 15.5 9a1.414 1.414 0 0 1 2 2L11 17.5 8 18l.5-3z"></path>
            </svg>
        `.trim(),
    };
}


function getFileChangesForSelectedNode() {
    const nodeId = String(S.selectedNodeId || "").trim();
    if (!nodeId) return [];
    const currentDetailNodeId = String(S.currentNodeDetail?.node_id || "").trim();
    const source = currentDetailNodeId === nodeId
        ? S.currentNodeDetail
        : ((S.taskNodeDetails || {})[nodeId] || null);
    const items = Array.isArray(source?.tool_file_changes) ? source.tool_file_changes : [];
    return items.map((item) => ({
        path: String(item?.path || "").trim(),
        change_type: normalizeFileChangeType(item?.change_type),
    })).filter((item) => item.path);
}


async function syncArtifactsForSelectedNode({ preserveViewState = true, autoSelect = true } = {}) {
    const viewState = preserveViewState ? captureTaskDetailViewState() : null;
    const visibleFiles = getFileChangesForSelectedNode();
    void autoSelect;
    renderArtifacts();
    try {
        return visibleFiles;
    } finally {
        restoreTaskDetailViewState(viewState);
        scheduleTaskDetailSessionPersist();
    }
}


async function loadTaskArtifacts() {
    return syncArtifactsForSelectedNode();
}


async function selectArtifact(artifactId, { preserveViewState = true } = {}) {
    void artifactId;
    void preserveViewState;
}


function refreshRenderedTreeNodeStatuses() {
    if (!U.tree || !String(S.treeRootNodeId || "").trim()) return;
    const nextTreeView = refreshTreeViewFromSnapshot();
    if (!nextTreeView) return;
    syncTaskTreeHeaderState(nextTreeView);
    const buttons = new Map(
        Array.from(U.tree.querySelectorAll(".execution-tree-node[data-id]"))
            .map((button) => [String(button.dataset.id || "").trim(), button])
            .filter(([nodeId]) => !!nodeId),
    );
    const walk = (node) => {
        if (!node) return;
        const button = buttons.get(String(node.node_id || "").trim());
        if (button instanceof HTMLElement) {
            const title = String(node.title || node.node_id || "");
            const fullTitle = String(node.fullTitle || title);
            const nodeStatus = String(node.visual_state || node.state || "").trim().toLowerCase();
            const taskPaused = taskPauseDisplayActive();
            const effectivePaused = effectiveTreeNodePaused(node, taskPaused);
            const displayState = taskNodeDisplayState(node, taskPaused);
            button.dataset.status = nodeStatus;
            button.dataset.paused = effectivePaused ? "true" : "false";
            button.dataset.pausedReason = String(node.pause_reason || "");
            button.title = node.pause_remark ? `${fullTitle} \u2014 ${node.pause_remark}` : fullTitle;
            const pauseEl = button.querySelector("[data-node-pause-action]");
            if (pauseEl instanceof HTMLElement) {
                pauseEl.hidden = taskPaused;
                pauseEl.setAttribute("aria-hidden", taskPaused ? "true" : "false");
                pauseEl.textContent = node.is_paused ? "\u25b6" : "\u2161";
                pauseEl.setAttribute("title", node.is_paused ? "\u6062\u590d\u8282\u70b9" : "\u6682\u505c\u8282\u70b9");
            }
            const titleEl = button.querySelector(".execution-tree-node-title");
            if (titleEl instanceof HTMLElement) titleEl.textContent = title;
            const badgeEl = button.querySelector(".status-badge");
            if (badgeEl instanceof HTMLElement) {
                badgeEl.dataset.status = String(node.visual_state || node.state || "");
                badgeEl.textContent = displayState;
            }
            const item = button.closest(".execution-tree-item");
            if (item instanceof HTMLElement) item.dataset.status = nodeStatus;
            const branch = item?.querySelector(":scope > .execution-tree-list");
            if (branch instanceof HTMLElement) branch.dataset.parentStatus = nodeStatus;
        }
        treeViewChildren(node).forEach(walk);
    };
    walk(nextTreeView);
    syncSelectedTaskNodeDetailStatus(nextTreeView);
}


function syncSelectedTaskNodeDetailStatus(treeView) {
    const selectedNodeId = String(S.selectedNodeId || "").trim();
    if (!selectedNodeId) return;
    if (String(S.currentNodeDetail?.node_id || "").trim() !== selectedNodeId) return;
    const selectedNode = findTreeNode(treeView, selectedNodeId);
    if (!selectedNode) return;
    renderTaskNodeDetailStatus({ ...selectedNode, ...S.currentNodeDetail });
}


function refreshRenderedTreeNodeStatus() {
    scheduleRenderedTreeNodeStatusRefresh();
}


function scheduleRenderedTreeNodeStatusRefresh() {
    if (treeVisualRefreshRaf) return;
    treeVisualRefreshRaf = window.requestAnimationFrame(() => {
        treeVisualRefreshRaf = 0;
        refreshRenderedTreeNodeStatuses();
    });
}


async function reconcileTaskTreeForNode(nodeId) {
    const normalizedNodeId = String(nodeId || "").trim();
    const taskId = String(S.currentTaskId || "").trim();
    if (!normalizedNodeId || !taskId || !String(S.treeRootNodeId || "").trim()) return;
    if (treeSnapshotNode(normalizedNodeId)) return;
    const detail = await ensureTaskNodeDetail(normalizedNodeId);
    if (String(S.currentTaskId || "").trim() !== taskId) return;
    const parentNodeId = String(detail?.parent_node_id || "").trim();
    if (parentNodeId) {
        scheduleTaskTreeBranchSync(parentNodeId);
    }
}


async function showAgent(node, { preserveViewState = true, forceRefresh = false } = {}) {
    const nodeId = String(node?.node_id || "").trim();
    if (!nodeId) return;
    const renderToken = (Number(S.taskDetailRenderToken || 0) || 0) + 1;
    S.taskDetailRenderToken = renderToken;
    const previousDetailNodeId = String(S.currentNodeDetail?.node_id || "").trim();
    const hadVisibleCurrentDetail = U.detail?.style?.display !== "none"
        && String(S.currentNodeDetail?.node_id || "").trim() === nodeId;
    const entryViewState = consumePendingTaskDetailRestore(nodeId)
        || (preserveViewState ? captureTaskDetailViewState() : getStoredTaskDetailViewState(S.currentTaskId, nodeId));
    if (!S.taskNodeDetails[nodeId] && !hadVisibleCurrentDetail) showTaskNodeLoadingState(node);
    const detail = await ensureTaskNodeDetail(nodeId, { force: forceRefresh });
    if (renderToken !== S.taskDetailRenderToken) return;
    if (String(S.selectedNodeId || "").trim() !== nodeId) return;
    const liveFrameMap = liveFramesByNodeId();
    const liveFrame = liveFrameMap.get(nodeId) || null;
    const mergedNode = {
        ...node,
        ...(detail || {}),
        executionTrace: buildNodeExecutionTrace(node, detail || {}, liveFrame),
        model_retry_status: liveFrame?.model_retry_status || null,
    };
    // 加载 chip 的风险度取自 skill/tool 目录，而目录过去只有资源页手动刷新时才填充：按需补一次才不至于全部退成兜底色。
    if (typeof ensureContextRiskCatalog === "function"
        && typeof hasContextRiskCatalog === "function"
        && !hasContextRiskCatalog()
        && traceHasContextLoadStep(mergedNode.executionTrace)) {
        await ensureContextRiskCatalog();
        if (renderToken !== S.taskDetailRenderToken) return;
        if (String(S.selectedNodeId || "").trim() !== nodeId) return;
    }
    S.currentNodeDetail = mergedNode;
    U.detail.style.display = "flex";
    if (U.nodeEmpty) U.nodeEmpty.style.display = "none";
    setTaskSelectionEmptyVisible(false);
    if (U.adRole) U.adRole.hidden = true;
    renderTaskNodeDetailStatus(mergedNode);
    renderTaskNodeModelRetryToast(mergedNode);
    if (U.adTokenSummary) U.adTokenSummary.textContent = buildTokenUsageParts(mergedNode.token_usage).join(" · ");
    // 同一节点刷新时，展开态取渲染前 DOM 的当下状态：入口那次捕获在请求之前，
    // 在飞窗口里用户刚点开的卡片和工具面板不在里面，沿用会把它们折回上一次的样子。
    const liveViewState = hadVisibleCurrentDetail ? captureTaskDetailViewState() : null;
    const hasLiveRows = !!(liveViewState
        && (liveViewState.traceItems.length
            || liveViewState.messageItems.length
            || liveViewState.spawnReviewItems.length));
    const viewState = hasLiveRows ? liveViewState : entryViewState;
    const traceChanged = !!renderExecutionTrace(mergedNode, { viewState });
    const messagesChanged = !!renderMessageList(mergedNode, { viewState });
    const spawnReviewChanged = !!renderSpawnReviewTrace(mergedNode, { viewState });
    const outputChanged = !!renderFinalOutput(mergedNode.executionTrace?.final_output || "");
    const acceptanceChanged = !!renderAcceptanceResult(mergedNode.executionTrace?.acceptance_result || "");
    void renderNodeErrorHistory(mergedNode);
    U.feedTitle.textContent = formatNodeDetailHeading(mergedNode);
    U.feedTitle.title = formatNodeDetailHeading(mergedNode, { compact: false });
    setTaskDetailOpen(true);
    icons();
    const nodeChanged = !hadVisibleCurrentDetail || previousDetailNodeId !== nodeId;
    if (U.nodeContextDisclosure) {
        if (nodeChanged) {
            U.nodeContextDisclosure.open = false;
            renderNodeContextPlaceholder();
        }
    }
    if (!hadVisibleCurrentDetail || traceChanged || messagesChanged || spawnReviewChanged || outputChanged || acceptanceChanged) {
        restoreTaskDetailViewState(viewState);
        stashTaskDetailViewState({ nodeId, viewState });
    } else {
        stashTaskDetailViewState({ nodeId });
    }
    if (nodeChanged || forceRefresh) {
        void syncArtifactsForSelectedNode();
    }
}


function hideAgent() {
    if (U.detail) U.detail.style.display = "none";
    renderTaskNodeModelRetryToast(null);
    setTaskDetailOpen(false);
}


function applyTaskPayload(payload) {
    if (!payload || !payload.task) return;
    const previousTaskId = String(S.currentTask?.task_id || "").trim();
    const nextTaskId = String(payload.task?.task_id || "").trim();
    const taskChanged = previousTaskId !== nextTaskId;
    const rootNode = payload.root_node || null;
    const frontier = Array.isArray(payload.frontier) ? payload.frontier : [];
    const recentModelCalls = Array.isArray(payload.recent_model_calls) ? payload.recent_model_calls : [];
    S.currentTask = payload.task;
    S.taskSummary = payload.summary || null;
    S.taskRuntimeSummary = payload.runtime_summary || null;
    S.rootNode = rootNode;
    S.frontier = frontier;
    S.recentModelCalls = recentModelCalls;
    if (taskChanged) {
        // 换任务必须退出历史快照态：anchor 与页号是按上一本账本算的。
        S.taskModelCallPaging = null;
        S.taskModelCallPageRows = [];
    }
    S.taskModelCallsPageSize = typeof TASK_MODEL_CALLS_PAGE_SIZE === "number" && TASK_MODEL_CALLS_PAGE_SIZE > 0
        ? TASK_MODEL_CALLS_PAGE_SIZE
        : 100;
    if (taskChanged) S.taskModelCallsPage = 1;
    if (taskChanged) S.taskModelCallsQuery = "";
    // 快照不整表替换帧索引：快照帧只带投影模型上的字段，整表换会把 live 车道
    // 已经收到的实时字段（重试态等）在每次树刷新时抹掉。旧帧作底、快照在后，
    // 快照仍权威于它覆盖到的那些字段；节点终态时快照自带完整帧，不依赖旧值。
    const previousLiveFrameMap = S.liveFrameMap || {};
    const nextLiveFrameMap = indexTaskLiveFrames(frontier);
    S.liveFrameMap = Object.fromEntries(
        Object.entries(nextLiveFrameMap).map(([nodeId, frame]) => [
            nodeId,
            { ...(previousLiveFrameMap[nodeId] || {}), ...(frame || {}) },
        ]),
    );
    if (rootNode && String(rootNode?.node_id || "").trim()) {
        S.taskNodeDetails = { ...(S.taskNodeDetails || {}), [String(rootNode.node_id || "").trim()]: rootNode };
    }
    resetTaskTreeSnapshotState();
    S.treeSelectedRoundByNodeId = {};
    renderTaskDetailHeader({ resetPromptDisclosure: taskChanged });
    if (U.taskTokenButton) U.taskTokenButton.disabled = !S.currentTask;
    renderTaskTokenStats();
    syncTaskTreeHeaderState(null);
    if (U.tree) U.tree.innerHTML = '<div class="empty-state">Loading task tree...</div>';
    setTaskSelectionEmptyVisible(false);
    hideAgent();
}

