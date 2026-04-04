// Task tree and task detail view layer extracted from org_graph_app.js.
// Loaded before org_graph_app.js and relies on globals initialized there at runtime.

function setTaskSelectionEmptyVisible(visible) {
    if (U.taskSelectionEmpty) U.taskSelectionEmpty.hidden = !visible;
}

function normalizeTreeRoundSelections(value) {
    const next = {};
    if (!value || typeof value !== "object") return next;
    Object.entries(value).forEach(([nodeId, roundId]) => {
        const normalizedNodeId = String(nodeId || "").trim();
        const normalizedRoundId = String(roundId || "").trim();
        if (!normalizedNodeId || !normalizedRoundId) return;
        next[normalizedNodeId] = normalizedRoundId;
    });
    return next;
}

function treeNormalizeInt(value, fallback = 0) {
    const numeric = Number.parseInt(String(value ?? ""), 10);
    return Number.isFinite(numeric) ? numeric : fallback;
}

function normalizeTaskGovernanceHistoryEntry(value = {}) {
    return {
        triggered_at: String(value?.triggered_at || "").trim(),
        trigger_reason: String(value?.trigger_reason || "").trim(),
        trigger_snapshot: {
            max_depth: Math.max(0, treeNormalizeInt(value?.trigger_snapshot?.max_depth, 0)),
            total_nodes: Math.max(0, treeNormalizeInt(value?.trigger_snapshot?.total_nodes, 0)),
        },
        decision: String(value?.decision || "allow").trim().toLowerCase() || "allow",
        decision_reason: String(value?.decision_reason || "").trim(),
        limited_depth: Math.max(0, treeNormalizeInt(value?.limited_depth, 0)),
        evidence: (Array.isArray(value?.evidence) ? value.evidence : [])
            .map((item) => String(item || "").trim())
            .filter(Boolean),
    };
}

function normalizeTaskGovernanceState(value = {}) {
    return {
        enabled: value?.enabled !== false,
        frozen: !!value?.frozen,
        review_inflight: !!value?.review_inflight,
        depth_baseline: Math.max(1, treeNormalizeInt(value?.depth_baseline, 1)),
        node_count_baseline: Math.max(0, treeNormalizeInt(value?.node_count_baseline, 0)),
        hard_limited_depth: Math.max(0, treeNormalizeInt(value?.hard_limited_depth, 0)),
        supervision_disabled_after_limit: !!value?.supervision_disabled_after_limit,
        last_trigger_reason: String(value?.last_trigger_reason || "").trim(),
        last_decision: String(value?.last_decision || "").trim().toLowerCase(),
        history: (Array.isArray(value?.history) ? value.history : [])
            .map((item) => normalizeTaskGovernanceHistoryEntry(item)),
    };
}

function taskGovernanceDecisionLabel(decision) {
    const normalized = String(decision || "").trim().toLowerCase();
    if (normalized === "cap_current_depth") return "限制深度";
    if (normalized === "allow") return "放行";
    return "未知";
}

function taskGovernanceStatusLabel(governance = {}) {
    if (governance?.frozen || governance?.review_inflight) return "监管中";
    if (governance?.supervision_disabled_after_limit) return "已限深";
    return "监管空闲";
}

function buildTaskGovernanceViewModel(governance = S.taskGovernance) {
    const normalized = normalizeTaskGovernanceState(governance || {});
    const items = normalized.history.map((item) => ({
        ...item,
        decisionLabel: taskGovernanceDecisionLabel(item.decision),
        decisionReason: String(item.decision_reason || "").trim(),
        triggerSummary: `触发: ${String(item.trigger_reason || "unknown")}`,
        snapshotSummary: `深度 ${Math.max(0, treeNormalizeInt(item?.trigger_snapshot?.max_depth, 0))} · 节点 ${Math.max(0, treeNormalizeInt(item?.trigger_snapshot?.total_nodes, 0))}`,
        limitedDepthSummary: Math.max(0, treeNormalizeInt(item?.limited_depth, 0)) > 0 ? `限制深度 ${Math.max(0, treeNormalizeInt(item?.limited_depth, 0))}` : "",
    }));
    const latest = items[items.length - 1] || null;
    return {
        normalized,
        visible: true,
        breathing: !!(normalized.frozen || normalized.review_inflight),
        statusLabel: taskGovernanceStatusLabel(normalized),
        historyCount: items.length,
        latestDecisionLabel: latest ? latest.decisionLabel : "暂无决策",
        items,
    };
}

function normalizeTaskTreeSnapshotRound(value = {}) {
    return {
        round_id: String(value?.round_id || "").trim(),
        label: String(value?.label || "").trim(),
        is_latest: !!value?.is_latest,
        total_children: Math.max(0, treeNormalizeInt(value?.total_children, 0)),
        completed_children: Math.max(0, treeNormalizeInt(value?.completed_children, 0)),
        running_children: Math.max(0, treeNormalizeInt(value?.running_children, 0)),
        failed_children: Math.max(0, treeNormalizeInt(value?.failed_children, 0)),
        child_ids: (Array.isArray(value?.child_ids) ? value.child_ids : [])
            .map((item) => String(item || "").trim())
            .filter(Boolean),
    };
}

function normalizeTaskTreeSnapshotNode(value = {}, existing = null) {
    const prior = existing && typeof existing === "object" ? existing : {};
    return {
        node_id: String(value?.node_id || prior?.node_id || "").trim(),
        parent_node_id: String(value?.parent_node_id || prior?.parent_node_id || "").trim() || null,
        node_kind: String(value?.node_kind || prior?.node_kind || "execution").trim() || "execution",
        status: String(value?.status || prior?.status || "in_progress").trim() || "in_progress",
        title: String(value?.title || prior?.title || value?.goal || "").trim(),
        updated_at: String(value?.updated_at || prior?.updated_at || "").trim(),
        children_fingerprint: String(value?.children_fingerprint || prior?.children_fingerprint || "").trim(),
        default_round_id: String(value?.default_round_id || prior?.default_round_id || "").trim(),
        rounds: (Array.isArray(value?.rounds) ? value.rounds : Array.isArray(prior?.rounds) ? prior.rounds : [])
            .map((item) => normalizeTaskTreeSnapshotRound(item))
            .filter((item) => item.round_id),
        auxiliary_child_ids: (Array.isArray(value?.auxiliary_child_ids) ? value.auxiliary_child_ids : Array.isArray(prior?.auxiliary_child_ids) ? prior.auxiliary_child_ids : [])
            .map((item) => String(item || "").trim())
            .filter(Boolean),
    };
}

function treeSnapshotNode(nodeId) {
    const key = String(nodeId || "").trim();
    if (!key) return null;
    return S.treeNodesById?.[key] || null;
}

function snapshotNodeDefaultRoundId(node) {
    const rounds = Array.isArray(node?.rounds) ? node.rounds : [];
    if (!rounds.length) return "";
    const explicit = String(node?.default_round_id || "").trim();
    if (explicit && rounds.some((round) => round.round_id === explicit)) return explicit;
    return String(rounds.find((round) => round?.is_latest)?.round_id || rounds[rounds.length - 1]?.round_id || "");
}

function snapshotNodeSelectedRoundId(node, selections = S.treeSelectedRoundByNodeId) {
    const rounds = Array.isArray(node?.rounds) ? node.rounds : [];
    if (!rounds.length) return "";
    const nodeId = String(node?.node_id || "").trim();
    const selected = String(selections?.[nodeId] || "").trim();
    if (selected && rounds.some((round) => round.round_id === selected)) return selected;
    return snapshotNodeDefaultRoundId(node);
}

function snapshotNodeVisibleChildIds(node, selections = S.treeSelectedRoundByNodeId) {
    const seen = new Set();
    const out = [];
    (Array.isArray(node?.auxiliary_child_ids) ? node.auxiliary_child_ids : []).forEach((childId) => {
        const normalized = String(childId || "").trim();
        if (!normalized || seen.has(normalized)) return;
        seen.add(normalized);
        out.push(normalized);
    });
    const rounds = Array.isArray(node?.rounds) ? node.rounds : [];
    if (!rounds.length) return out;
    const selectedRoundId = snapshotNodeSelectedRoundId(node, selections);
    const selectedRound = rounds.find((round) => round.round_id === selectedRoundId) || null;
    (Array.isArray(selectedRound?.child_ids) ? selectedRound.child_ids : []).forEach((childId) => {
        const normalized = String(childId || "").trim();
        if (!normalized || seen.has(normalized)) return;
        seen.add(normalized);
        out.push(normalized);
    });
    return out;
}

function collectSnapshotSubtreeIds(rootNodeId, nodesById = S.treeNodesById) {
    const rootId = String(rootNodeId || "").trim();
    if (!rootId) return new Set();
    const collected = new Set();
    const queue = [rootId];
    while (queue.length) {
        const currentId = queue.shift();
        if (!currentId || collected.has(currentId)) continue;
        const node = nodesById?.[currentId];
        if (!node) continue;
        collected.add(currentId);
        snapshotNodeVisibleChildIds(node, S.treeSelectedRoundByNodeId).forEach((childId) => {
            if (!collected.has(childId)) queue.push(childId);
        });
        (Array.isArray(node?.rounds) ? node.rounds : []).forEach((round) => {
            (Array.isArray(round?.child_ids) ? round.child_ids : []).forEach((childId) => {
                if (!collected.has(childId)) queue.push(childId);
            });
        });
        (Array.isArray(node?.auxiliary_child_ids) ? node.auxiliary_child_ids : []).forEach((childId) => {
            if (!collected.has(childId)) queue.push(childId);
        });
    }
    return collected;
}

function resetTaskTreeSnapshotState({ clearDirty = true } = {}) {
    Object.values(S.treeBranchSyncTokenById || {}).forEach((token) => {
        if (token) window.clearTimeout(token);
    });
    S.treeRootNodeId = "";
    S.treeNodesById = {};
    S.treeSnapshotVersion = "";
    S.treeView = null;
    S.treeLargeMode = false;
    S.treeSelectedRoundByNodeId = {};
    S.treeBranchSyncTokenById = {};
    S.treeBranchSyncInFlightById = {};
    S.treeBranchSyncQueuedById = {};
    if (clearDirty) S.treeDirtyParentsById = {};
}

function applyTaskTreeSnapshotPayload(payload = {}) {
    const rootNodeId = String(payload?.root_node_id || "").trim();
    const sourceNodes = payload?.nodes_by_id && typeof payload.nodes_by_id === "object" ? payload.nodes_by_id : {};
    const nextNodesById = {};
    Object.entries(sourceNodes).forEach(([nodeId, node]) => {
        const normalizedNodeId = String(nodeId || node?.node_id || "").trim();
        if (!normalizedNodeId) return;
        nextNodesById[normalizedNodeId] = normalizeTaskTreeSnapshotNode(node);
    });
    S.treeRootNodeId = rootNodeId;
    S.treeNodesById = nextNodesById;
    S.treeSnapshotVersion = String(payload?.snapshot_version || "").trim();
    S.treeView = null;
    S.treeLargeMode = false;
    S.treeSelectedRoundByNodeId = pruneTreeRoundSelections({});
}

function applyTaskTreeSubtreePayload(payload = {}) {
    const subtreeRootId = String(payload?.root_node_id || "").trim();
    if (!subtreeRootId) return;
    const nextNodesById = { ...(S.treeNodesById || {}) };
    collectSnapshotSubtreeIds(subtreeRootId, nextNodesById).forEach((nodeId) => {
        delete nextNodesById[nodeId];
    });
    const sourceNodes = payload?.nodes_by_id && typeof payload.nodes_by_id === "object" ? payload.nodes_by_id : {};
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

async function loadTaskTreeSnapshot(taskId = S.currentTaskId) {
    const normalizedTaskId = String(taskId || "").trim();
    if (!normalizedTaskId) return null;
    if (U.tree) U.tree.innerHTML = '<div class="empty-state">Loading task tree...</div>';
    try {
        const payload = await ApiClient.getTaskTreeSnapshot(normalizedTaskId);
        if (String(S.currentTaskId || "").trim() !== normalizedTaskId) return null;
        applyTaskTreeSnapshotPayload(payload || {});
        renderTree();
        return payload || null;
    } catch (error) {
        if (!isAbortLike(error) && U.tree) {
            U.tree.innerHTML = `<div class="empty-state error">Task tree unavailable: ${esc(error.message || "Unknown error")}</div>`;
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
    const request = (async () => {
        try {
            const payload = await ApiClient.getTaskNodeTreeSubtree(taskId, normalizedNodeId, { roundId: normalizedRoundId });
            if (String(S.currentTaskId || "").trim() !== taskId) return null;
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

function buildNodeRoundState(node, selections = S.treeSelectedRoundByNodeId) {
    const rounds = (Array.isArray(node?.rounds) ? node.rounds : []).map((round, index) => ({
        roundId: String(round?.round_id || ""),
        roundIndex: index + 1,
        label: formatRoundLabel(round?.label, index + 1),
        isLatest: !!round?.is_latest,
        childCount: Array.isArray(round?.child_ids) ? round.child_ids.length : 0,
        createdAt: "",
        totalChildren: Math.max(0, treeNormalizeInt(round?.total_children, Array.isArray(round?.child_ids) ? round.child_ids.length : 0)),
        completedChildren: Math.max(0, treeNormalizeInt(round?.completed_children, 0)),
        runningChildren: Math.max(0, treeNormalizeInt(round?.running_children, 0)),
        failedChildren: Math.max(0, treeNormalizeInt(round?.failed_children, 0)),
    }));
    if (!rounds.length) {
        return {
            options: [],
            selectedRoundId: "",
            defaultRoundId: "",
            summary: "当前节点无派生轮次",
        };
    }
    const defaultRoundId = snapshotNodeDefaultRoundId(node);
    const selectedRoundId = snapshotNodeSelectedRoundId(node, selections);
    const selectedRound = rounds.find((round) => round.roundId === selectedRoundId) || rounds[rounds.length - 1];
    const selectionMode = selectedRound.roundId && selectedRound.roundId !== defaultRoundId ? "手动" : "最新";
    const totalChildren = selectedRound.totalChildren || selectedRound.childCount;
    const counts = [
        `${selectedRound.completedChildren}/${totalChildren || selectedRound.childCount} 完成`,
        selectedRound.runningChildren ? `${selectedRound.runningChildren} 进行中` : "",
        selectedRound.failedChildren ? `${selectedRound.failedChildren} 失败` : "",
    ].filter(Boolean).join("，");
    return {
        options: rounds,
        selectedRoundId,
        defaultRoundId,
        summary: `${selectedRound.label}${selectedRound.isLatest ? "（最新）" : ""} | ${selectionMode} | ${counts || `${selectedRound.childCount} 个子节点`} | 共 ${rounds.length} 轮`,
    };
}

function buildExecutionTreeFromSnapshot(nodeId = S.treeRootNodeId, selections = S.treeSelectedRoundByNodeId, seen = new Set()) {
    const normalizedNodeId = String(nodeId || "").trim();
    if (!normalizedNodeId || seen.has(normalizedNodeId)) return null;
    const snapshotNode = treeSnapshotNode(normalizedNodeId);
    if (!snapshotNode) return null;
    seen.add(normalizedNodeId);
    const kind = String(snapshotNode.node_kind || "execution").trim().toLowerCase() || "execution";
    const status = String(snapshotNode.status || "unknown").trim().toLowerCase() || "unknown";
    const title = resolveNodeTitle(snapshotNode, snapshotNode);
    const roundState = buildNodeRoundState(snapshotNode, selections);
    const visibleChildren = snapshotNodeVisibleChildIds(snapshotNode, selections)
        .map((childId) => buildExecutionTreeFromSnapshot(childId, selections, seen))
        .filter(Boolean);
    const inspectionNodes = [];
    const childNodes = [];
    visibleChildren.forEach((child) => {
        if (isAcceptanceNodeKind(child?.kind)) inspectionNodes.push(child);
        else childNodes.push(child);
    });
    const inspectionActive = inspectionNodes.some((child) => isInspectionActiveStatus(child?.state || child?.visual_state));
    const stateMeta = resolveTreeNodeStatusLabel(status, { kind, inspectionActive });
    const liveFrame = S.liveFrameMap?.[normalizedNodeId] || null;
    const waitingForChildren = isWaitingForChildResultsFrame(liveFrame);
    const isActiveNode = !isTerminalTreeNodeStatus(status) && !waitingForChildren;
    const activeNodeCount = (isActiveNode ? 1 : 0)
        + visibleChildren.reduce((sum, child) => sum + treeNormalizeInt(child?.activeNodeCount, 0), 0);
    return {
        node_id: snapshotNode.node_id,
        parent_node_id: snapshotNode.parent_node_id,
        title: title.title,
        fullTitle: title.fullTitle,
        goal: title.goal,
        kind,
        state: status,
        visual_state: stateMeta.visualState,
        display_state: stateMeta.displayState,
        roundOptions: roundState.options,
        selectedRoundId: roundState.selectedRoundId,
        defaultRoundId: roundState.defaultRoundId,
        roundSummary: roundState.summary,
        inspectionNodes,
        children: childNodes,
        activeNodeCount,
    };
}

function countVisibleTreeNodes(root, predicate = null) {
    let count = 0;
    const walk = (node) => {
        if (!node) return;
        if (!predicate || predicate(node)) count += 1;
        treeViewChildren(node).forEach(walk);
    };
    walk(root);
    return count;
}

function isTerminalTreeNodeStatus(status) {
    const normalized = String(status || "").trim().toLowerCase();
    return normalized === "success" || normalized === "failed";
}

function isActiveChildPipelineStatus(status) {
    const normalized = String(status || "").trim().toLowerCase();
    return normalized === "queued" || normalized === "running";
}

function isWaitingForChildResultsFrame(frame) {
    if (!frame || typeof frame !== "object") return false;
    const phase = String(frame?.phase || "").trim().toLowerCase();
    if (phase === "waiting_children" || phase === "waiting_acceptance") return true;
    return (Array.isArray(frame?.child_pipelines) ? frame.child_pipelines : [])
        .some((item) => isActiveChildPipelineStatus(item?.status || ""));
}

function analyzeExecutionTreeLayout(root) {
    const stats = {
        totalItems: 0,
        totalBoxes: 0,
        maxBreadth: 0,
        maxDepth: 0,
        rootChildren: Array.isArray(root?.children) ? root.children.length : 0,
    };
    if (!root) return stats;
    const breadthByDepth = new Map();
    const queue = [{ node: root, depth: 1 }];
    for (let index = 0; index < queue.length; index += 1) {
        const current = queue[index];
        const node = current?.node || null;
        const depth = Number(current?.depth || 1);
        if (!node) continue;
        const inspectionCount = Array.isArray(node?.inspectionNodes) ? node.inspectionNodes.length : 0;
        const children = Array.isArray(node?.children) ? node.children : [];
        stats.totalItems += 1;
        stats.totalBoxes += 1 + inspectionCount;
        stats.maxDepth = Math.max(stats.maxDepth, depth);
        breadthByDepth.set(depth, (breadthByDepth.get(depth) || 0) + 1);
        children.forEach((child) => queue.push({ node: child, depth: depth + 1 }));
    }
    breadthByDepth.forEach((count) => {
        if (count > stats.maxBreadth) stats.maxBreadth = count;
    });
    return stats;
}

function resolveExecutionTreeDensity(root) {
    const stats = analyzeExecutionTreeLayout(root);
    if (
        stats.maxBreadth >= 8
        || stats.totalItems >= 24
        || (stats.maxBreadth >= 6 && stats.totalBoxes >= 18)
        || (stats.rootChildren >= 7 && stats.totalItems >= 12)
    ) {
        return { mode: "dense", stats };
    }
    if (
        stats.maxBreadth >= 5
        || stats.totalItems >= 14
        || stats.totalBoxes >= 16
        || stats.rootChildren >= 5
    ) {
        return { mode: "wide", stats };
    }
    return { mode: "default", stats };
}

function hasManualTreeRoundSelections() {
    return Object.keys(normalizeTreeRoundSelections(S.treeSelectedRoundByNodeId)).length > 0;
}

function taskDetailStatusLabel(task) {
    return ({ in_progress: "运行中", success: "已完成", failed: "失败", blocked: "已暂停", unknown: "未知" })[taskStatusKey(task)] || "未知";
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

function renderTaskGovernancePanel() {
    if (!U.taskGovernancePanel || !U.taskGovernanceDetails || !U.taskGovernanceStatus || !U.taskGovernanceLastDecision || !U.taskGovernanceHistoryCount || !U.taskGovernanceHistory || !U.taskGovernanceEmpty) return;
    const hasTask = !!String(S.currentTaskId || "").trim();
    if (!hasTask) {
        U.taskGovernancePanel.hidden = true;
        U.taskGovernancePanel.classList.remove("is-breathing");
        U.taskGovernanceDetails.open = false;
        U.taskGovernanceHistory.innerHTML = "";
        U.taskGovernanceEmpty.hidden = false;
        return;
    }
    const view = buildTaskGovernanceViewModel(S.taskGovernance || {});
    U.taskGovernancePanel.hidden = !view.visible;
    U.taskGovernancePanel.classList.toggle("is-breathing", view.breathing);
    U.taskGovernanceStatus.textContent = view.statusLabel;
    U.taskGovernanceLastDecision.textContent = view.latestDecisionLabel;
    U.taskGovernanceHistoryCount.textContent = `${view.historyCount} 次`;
    U.taskGovernanceEmpty.hidden = view.items.length > 0;
    U.taskGovernanceHistory.innerHTML = view.items.map((item) => `
        <article class="task-governance-entry">
            <div class="task-governance-entry-head">
                <strong class="task-governance-entry-title">${esc(item.decisionLabel)}</strong>
                <span class="task-governance-entry-time">${esc(item.triggered_at || "")}</span>
            </div>
            <div class="task-governance-entry-meta">${esc(item.triggerSummary)} · ${esc(item.snapshotSummary)}${item.limitedDepthSummary ? ` · ${esc(item.limitedDepthSummary)}` : ""}</div>
            <div class="task-governance-entry-reason">${esc(item.decisionReason || "暂无说明")}</div>
            ${Array.isArray(item.evidence) && item.evidence.length ? `<ul class="task-governance-evidence">${item.evidence.map((line) => `<li>${esc(line)}</li>`).join("")}</ul>` : ""}
        </article>
    `).join("");
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
    S.treeSelectedRoundByNodeId = {};
    renderTree();
    scheduleTaskDetailSessionPersist();
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

function truncateNodeTitle(text, maxChars = 20) {
    const chars = Array.from(String(text || ""));
    if (chars.length <= maxChars) return chars.join("");
    return `${chars.slice(0, maxChars).join("")}...`;
}

function repairAcceptanceTitle(text) {
    const raw = String(text || "").trim();
    if (!raw) return "";
    return raw.replace(/^鏈€缁堥獙鏀(?:讹細|:|：|\?)?/, "最终验收:");
}

function resolveNodeTitle(node, detail) {
    const nodeKind = String(detail?.node_kind || node?.node_kind || "").trim().toLowerCase();
    const goal = repairAcceptanceTitle(String(detail?.goal || "").trim());
    const rawTitle = goal || repairAcceptanceTitle(String(node?.title || node?.node_id || "").trim()) || String(node?.node_id || "");
    const acceptanceTitle = rawTitle.replace(/^accept\s*:\s*/i, "").trim();
    const fullTitle = nodeKind === "acceptance"
        ? (acceptanceTitle ? `检验 · ${acceptanceTitle}` : "检验")
        : rawTitle;
    return {
        goal: nodeKind === "acceptance" ? fullTitle : (goal || fullTitle),
        fullTitle,
        title: truncateNodeTitle(fullTitle, 20),
    };
}

function isAcceptanceNodeKind(kind) {
    return String(kind || "").trim().toLowerCase() === "acceptance";
}

function isInspectionActiveStatus(status) {
    return ["queued", "running", "pending", "waiting", "in_progress"].includes(String(status || "").trim().toLowerCase());
}

function resolveTreeNodeStatusLabel(status, { kind = "", inspectionActive = false } = {}) {
    if (inspectionActive || (isAcceptanceNodeKind(kind) && isInspectionActiveStatus(status))) {
        return { visualState: "inspecting", displayState: "检验中" };
    }
    const normalizedStatus = String(status || "").trim().toLowerCase() || "unknown";
    return {
        visualState: normalizedStatus,
        displayState: normalizedStatus.toUpperCase(),
    };
}

function treeViewChildren(node) {
    return [
        ...(Array.isArray(node?.inspectionNodes) ? node.inspectionNodes : []),
        ...(Array.isArray(node?.children) ? node.children : []),
    ];
}

function singleLineNodeHeading(node) {
    const raw = repairAcceptanceTitle(String(node?.goal || node?.fullTitle || node?.title || node?.node_id || "Node"));
    return raw
        .replace(/\r\n|\r|\n/g, " ")
        .replace(/\s+/g, " ")
        .trim() || "Node";
}

function compactNodeHeading(node, maxChars = 72) {
    const singleLine = singleLineNodeHeading(node);
    const chars = Array.from(singleLine);
    if (chars.length <= maxChars) return singleLine;
    return `${chars.slice(0, maxChars).join("")}...`;
}

function formatNodeDetailHeading(node, { maxChars = 72, compact = true } = {}) {
    const nodeId = String(node?.node_id || "").trim();
    const heading = compact ? compactNodeHeading(node, maxChars) : singleLineNodeHeading(node);
    if (!nodeId) return compact ? `Node: ${heading}` : heading;
    if (!heading || heading === nodeId) return nodeId;
    return `${nodeId} | ${heading}`;
}

function indexTaskLiveFrames(frames) {
    return Object.fromEntries(
        (Array.isArray(frames) ? frames : [])
            .map((frame) => [String(frame?.node_id || "").trim(), frame])
            .filter(([nodeId]) => !!nodeId),
    );
}

function liveFramesByNodeId() {
    const frames = Object.entries(S.liveFrameMap || {});
    return new Map(
        frames
            .map(([nodeId, frame]) => [String(nodeId || "").trim(), frame])
            .filter(([nodeId]) => !!nodeId),
    );
}

function normalizeLiveToolCalls(frame) {
    return (Array.isArray(frame?.tool_calls) ? frame.tool_calls : [])
        .map((item) => ({
            tool_call_id: String(item?.tool_call_id || ""),
            tool_name: String(item?.tool_name || "tool"),
            status: ["queued", "running", "success", "error"].includes(String(item?.status || ""))
                ? String(item.status)
                : "info",
            started_at: String(item?.started_at || ""),
            finished_at: String(item?.finished_at || ""),
            elapsed_seconds: Number.isFinite(Number(item?.elapsed_seconds)) ? Number(item.elapsed_seconds) : null,
        }))
        .filter((item) => item.tool_call_id || item.tool_name);
}

function normalizeLiveChildPipelines(frame) {
    return (Array.isArray(frame?.child_pipelines) ? frame.child_pipelines : [])
        .map((item, index) => ({
            index: normalizeInt(item?.index, index),
            goal: String(item?.goal || ""),
            status: ["queued", "running", "success", "error"].includes(String(item?.status || ""))
                ? String(item.status)
                : "info",
            child_node_id: String(item?.child_node_id || ""),
            acceptance_node_id: String(item?.acceptance_node_id || ""),
            check_status: String(item?.check_status || ""),
            started_at: String(item?.started_at || ""),
            finished_at: String(item?.finished_at || ""),
        }))
        .filter((item) => item.goal || item.child_node_id || item.acceptance_node_id);
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

function normalizeSummaryTraceToolCall(step, index = 0) {
    const rawStatus = String(step?.status || "").trim().toLowerCase();
    const explicitStatus = ({
        queued: "running",
        running: "running",
        success: "success",
        error: "error",
    }[rawStatus] || "");
    const outputText = String(step?.output_text || "");
    const outputRef = String(step?.output_ref || "");
    const startedAt = String(step?.started_at || "");
    const finishedAt = String(step?.finished_at || "");
    return {
        tool_call_id: String(step?.tool_call_id || `summary-tool-${index + 1}`),
        tool_name: String(step?.tool_name || "tool"),
        arguments_text: String(step?.arguments_text || ""),
        output_text: outputText,
        output_ref: outputRef,
        started_at: startedAt,
        finished_at: finishedAt,
        elapsed_seconds: Number.isFinite(Number(step?.elapsed_seconds)) ? Number(step.elapsed_seconds) : null,
        status: explicitStatus || (finishedAt
            ? "success"
            : ((outputText.trim() || outputRef.trim())
                ? "success"
                : (startedAt ? "running" : "info"))),
    };
}

function normalizeSummaryExecutionTrace(summary) {
    const toInt = (value, fallback = 0) => {
        const parsed = Number.parseInt(String(value ?? ""), 10);
        return Number.isFinite(parsed) ? parsed : fallback;
    };
    const stages = (Array.isArray(summary?.stages) ? summary.stages : []).map((stage, stageIndex) => {
        const tools = (Array.isArray(stage?.tool_calls) ? stage.tool_calls : []).map((step, toolIndex) => (
            normalizeSummaryTraceToolCall(step, toolIndex)
        ));
        return {
            stage_id: String(stage?.stage_id || `summary-stage-${stageIndex + 1}`),
            stage_index: toInt(stage?.stage_index, stageIndex + 1),
            mode: String(stage?.mode || "执行摘要").trim() || "执行摘要",
            status: String(stage?.status || (String(stage?.finished_at || "").trim() ? "完成" : "进行中")).trim() || "进行中",
            stage_goal: String(stage?.stage_goal || "").trim(),
            stage_total_steps: toInt(stage?.tool_round_budget, 0),
            tool_rounds_used: tools.length ? 1 : 0,
            created_at: String(stage?.created_at || ""),
            finished_at: String(stage?.finished_at || ""),
            rounds: tools.length ? [{
                round_id: "",
                round_index: 1,
                created_at: "",
                budget_counted: false,
                tools,
            }] : [],
        };
    });
    const toolSteps = stages.flatMap((stage) => (
        Array.isArray(stage?.rounds)
            ? stage.rounds.flatMap((round) => (Array.isArray(round?.tools) ? round.tools : []))
            : []
    ));
    return {
        tool_steps: toolSteps,
        stages,
    };
}

function buildNodeExecutionTrace(node, detail, liveFrame = null) {
    const fullTrace = detail?.execution_trace && typeof detail.execution_trace === "object" ? detail.execution_trace : {};
    const summaryTrace = normalizeSummaryExecutionTrace(
        detail?.execution_trace_summary && typeof detail.execution_trace_summary === "object"
            ? detail.execution_trace_summary
            : {},
    );
    const source = Object.keys(fullTrace).length ? fullTrace : summaryTrace;
    const toolSteps = Array.isArray(source.tool_steps) ? source.tool_steps : [];
    const stages = (Array.isArray(source.stages) ? source.stages : []).map((stage, index) => normalizeExecutionStageTrace(stage, index));
    const initialPrompt = [source.initial_prompt, detail?.prompt, detail?.goal, node?.prompt, node?.goal, node?.input]
        .map((value) => String(value ?? ""))
        .find((value) => value.trim())
        || "";
    return {
        initial_prompt: initialPrompt,
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
        stages,
        live_tool_calls: normalizeLiveToolCalls(liveFrame),
        live_child_pipelines: normalizeLiveChildPipelines(liveFrame),
        final_output: String(source.final_output ?? detail?.final_output ?? node?.final_output ?? ""),
        acceptance_result: String(source.acceptance_result ?? detail?.check_result ?? node?.check_result ?? ""),
    };
}

function normalizeExecutionStageTrace(stage, index = 0) {
    const rounds = Array.isArray(stage?.rounds) ? stage.rounds : [];
    return {
        stage_id: String(stage?.stage_id || ""),
        stage_index: normalizeInt(stage?.stage_index, index + 1),
        mode: String(stage?.mode || "自主执行").trim() || "自主执行",
        status: String(stage?.status || "进行中").trim() || "进行中",
        stage_goal: String(stage?.stage_goal || "").trim(),
        stage_total_steps: normalizeInt(stage?.tool_round_budget ?? stage?.stage_total_steps, 0),
        tool_rounds_used: normalizeInt(stage?.tool_rounds_used, 0),
        created_at: String(stage?.created_at || ""),
        finished_at: String(stage?.finished_at || ""),
        rounds: rounds.map((round, roundIndex) => ({
            round_id: String(round?.round_id || ""),
            round_index: normalizeInt(round?.round_index, roundIndex + 1),
            created_at: String(round?.created_at || ""),
            budget_counted: !!round?.budget_counted,
            tools: (Array.isArray(round?.tools) ? round.tools : []).map((step) => ({
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
        })),
    };
}

function stageTraceStatus(stage) {
    return ({
        "进行中": "running",
        "in_progress": "running",
        "running": "running",
        "完成": "success",
        "success": "success",
        "completed": "success",
        "失败": "error",
        "failed": "error",
        "error": "error",
    }[String(stage?.status || "").trim()] || "info");
}

function roundTraceStatus(round) {
    const tools = Array.isArray(round?.tools) ? round.tools : [];
    return buildLiveSectionStatus(tools);
}

function nodeFinalTraceStatus(node) {
    if (String(node?.state || "") === "in_progress") return "running";
    if (String(node?.state || "") === "failed") return "error";
    return "success";
}

function renderTraceStep({ traceKey = "", title, status = "info", open = false, bodyHtml = "", showRuntime = true }) {
    return `
        <details class="interaction-step task-trace-step ${esc(status)}" data-trace-key="${esc(traceKey)}" data-default-open="${open ? "true" : "false"}"${open ? " open" : ""}>
            <summary class="task-trace-summary">
                <span class="interaction-step-lead">
                    <span class="interaction-step-title">${esc(title)}</span>
                </span>
                <span class="interaction-step-side">
                    ${showRuntime ? '<span class="task-trace-runtime" hidden></span>' : ''}
                    <span class="interaction-step-status">${esc(traceStatusLabel(status))}</span>
                </span>
            </summary>
            <div class="task-trace-body">${bodyHtml}</div>
        </details>
    `;
}

function resolveTraceStepOpenState(step, state, index) {
    if (!state || typeof state !== "object") return !!step.open;
    const traceItems = Array.isArray(state.traceItems) ? state.traceItems : [];
    const stepKey = String(step.traceKey || "").trim();
    if (stepKey) {
        const keyed = traceItems.find((item) => String(item?.key || "").trim() === stepKey);
        if (keyed && typeof keyed.open === "boolean") return keyed.open;
    }
    const stepTitle = String(step.title || "").trim();
    if (stepTitle) {
        const titled = traceItems.find((item) => String(item?.title || "").trim() === stepTitle);
        if (titled && typeof titled.open === "boolean") return titled.open;
    }
    if (typeof traceItems[index]?.open === "boolean") return traceItems[index].open;
    return !!step.open;
}

function renderTraceMessage(value, emptyText = "暂无内容") {
    const text = String(value || "").trim() || emptyText;
    return `
        <div class="task-trace-field">
            <div class="code-block task-trace-code">${esc(text)}</div>
        </div>
    `;
}

function resolveExecutionStageRoundLabel(stage, round) {
    const tools = Array.isArray(round?.tools) ? round.tools : [];
    const hasSpawnChildren = tools.some((step) => String(step?.tool_name || "").trim() === "spawn_child_nodes");
    if (hasSpawnChildren) return "包含派生";
    const stageMode = String(stage?.mode || "").trim();
    if (stageMode === "包含派生" && !tools.length) return "包含派生";
    return "自主执行";
}

function renderExecutionStageRounds(stage) {
    const rounds = Array.isArray(stage?.rounds) ? stage.rounds : [];
    if (!rounds.length) {
        return renderTraceField("阶段轮次", "", "当前阶段暂无工具轮次");
    }
    return rounds.map((round, index) => {
        const tools = Array.isArray(round.tools) ? round.tools : [];
        const title = resolveExecutionStageRoundLabel(stage, round);
        const toolDetails = tools.length
            ? tools.map((step, toolIndex) => renderTraceStep({
                traceKey: `stage:${stage.stage_id || stage.stage_index}:round:${round.round_id || round.round_index}:tool:${step.tool_call_id || toolIndex}`,
                title: `工具 · ${step.tool_name || "tool"}`,
                status: step.status || "info",
                open: false,
                bodyHtml: [
                    renderTraceField("参数", step.arguments_text, "无参数"),
                    renderTraceField(
                        "工具输出",
                        step.output_text,
                        step.status === "running" ? "等待工具输出..." : "暂无工具输出",
                        { decodeEscapes: true },
                    ),
                ].join(""),
            })).join("")
            : renderTraceField("工具", "", "本轮暂无工具记录");
        return renderTraceStep({
            traceKey: `stage:${stage.stage_id || stage.stage_index}:round:${round.round_id || round.round_index}`,
            title: `${title}${round.created_at ? ` · ${formatCompactTime(round.created_at)}` : ""}`,
            status: roundTraceStatus(round),
            open: false,
            bodyHtml: toolDetails,
        });
    }).join("");
}

function displayTaskStageStatus(status) {
    return ({
        "进行中": "进行中",
        "in_progress": "进行中",
        "running": "进行中",
        "完成": "完成",
        "success": "完成",
        "completed": "完成",
        "失败": "失败",
        "failed": "失败",
        "error": "失败",
    }[String(status || "").trim()]) || String(status || "").trim() || "进行中";
}

function formatExecutionStageTitle(stage) {
    const stageGoal = String(stage?.stage_goal || "").trim();
    const fallbackTitle = String(stage?.mode || "自主执行").trim() || "自主执行";
    const title = stageGoal || fallbackTitle;
    return `${title}${stage?.created_at ? ` · ${formatCompactTime(stage.created_at)}` : ""}`;
}

function buildExecutionTraceSteps(trace, node) {
    const initialPromptStep = {
        traceKey: "initial_prompt",
        title: "初始提示词",
        status: "info",
        open: false,
        bodyHtml: renderTraceField("内容", trace.initial_prompt, "暂无初始提示词"),
    };
    if (Array.isArray(trace?.stages) && trace.stages.length) {
        return [
            initialPromptStep,
            ...trace.stages.map((stage, index) => ({
                traceKey: `stage:${stage.stage_id || stage.stage_index || index}`,
                title: formatExecutionStageTitle(stage),
                status: stageTraceStatus(stage),
                open: index === trace.stages.length - 1,
                bodyHtml: [
                    renderTraceMessage(`本阶段最大轮数为${stage.stage_total_steps || 0}`, "本阶段最大轮数为0"),
                    renderTraceField("状态", displayTaskStageStatus(stage.status), "进行中"),
                    renderExecutionStageRounds(stage),
                ].join(""),
            })),
        ];
    }
    return [
        initialPromptStep,
        ...trace.tool_steps.map((step, index) => ({
            traceKey: `tool:${step.tool_call_id || index}:${step.tool_name || "tool"}`,
            title: `Tool - ${step.tool_name || "tool"}`,
            status: step.status || "info",
            open: false,
            bodyHtml: [
                renderTraceField("Arguments", step.arguments_text, "No arguments"),
                renderTraceField(
                    "Output",
                    step.output_text,
                    step.status === "running" ? "Waiting for tool output..." : "No tool output",
                    { decodeEscapes: true },
                ),
            ].join(""),
        })),
    ];
}

function toPixels(value) {
    const parsed = Number.parseFloat(String(value || ""));
    return Number.isFinite(parsed) ? parsed : 0;
}

function setScrollViewportLimit(container, itemSelector, visibleCount, measureItem = null, fillToVisibleCount = false) {
    if (!(container instanceof HTMLElement)) return;
    container.style.height = "";
    container.style.maxHeight = "";
    const items = Array.from(container.querySelectorAll(itemSelector)).filter((item) => item instanceof HTMLElement);
    if (!items.length || visibleCount <= 0) return;
    const styles = window.getComputedStyle(container);
    const gap = toPixels(styles.rowGap || styles.gap);
    const targetCount = Math.min(visibleCount, items.length);
    const measuredHeights = [];
    let height = 0;
    for (let index = 0; index < targetCount; index += 1) {
        const item = items[index];
        const measured = measureItem ? Number(measureItem(item, index)) : item.getBoundingClientRect().height;
        const resolvedHeight = measured > 0 ? measured : 0;
        measuredHeights.push(resolvedHeight);
        height += resolvedHeight;
        if (index < targetCount - 1) height += gap;
    }
    if (fillToVisibleCount && measuredHeights.length) {
        const averageHeight = measuredHeights.reduce((sum, value) => sum + value, 0) / measuredHeights.length;
        const remainingCount = Math.max(0, visibleCount - measuredHeights.length);
        if (remainingCount > 0) {
            height += averageHeight * remainingCount;
            height += gap * remainingCount;
        }
    }
    if (height > 0) {
        const resolvedHeight = `${Math.ceil(height)}px`;
        if (fillToVisibleCount) container.style.height = resolvedHeight;
        container.style.maxHeight = resolvedHeight;
    }
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
    const preservedDetailScrollTop = U.detail instanceof HTMLElement ? U.detail.scrollTop : null;
    const preservedTraceScrollTop = traceList instanceof HTMLElement ? traceList.scrollTop : null;
    const preservedArtifactListScrollTop = U.artifactList instanceof HTMLElement ? U.artifactList.scrollTop : null;
    if (traceList instanceof HTMLElement) {
        setScrollViewportLimit(traceList, ".task-trace-step", 10, traceStepSummaryHeight, true);
    }
    if (U.artifactList instanceof HTMLElement) {
        setScrollViewportLimit(U.artifactList, ".artifact-item", 5);
    }
    const restoreScrollPositions = () => {
        if (preservedDetailScrollTop !== null) setElementScrollTop(U.detail, preservedDetailScrollTop);
        if (preservedTraceScrollTop !== null) setElementScrollTop(traceList, preservedTraceScrollTop);
        if (preservedArtifactListScrollTop !== null) setElementScrollTop(U.artifactList, preservedArtifactListScrollTop);
    };
    restoreScrollPositions();
    window.requestAnimationFrame(restoreScrollPositions);
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
                renderTraceField("工具输出", step.output_text, step.status === "running" ? "等待工具输出..." : "暂无工具输出"),
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

function traceStatusLabel(status) {
    return ({
        info: "信息",
        running: "进行中",
        success: "完成",
        error: "失败",
    }[String(status || "")] || "信息");
}

function renderTraceField(label, value, emptyText = "暂无内容", { decodeEscapes = false } = {}) {
    const text = readableText(value, { decodeEscapes, emptyText });
    return `
        <div class="task-trace-field">
            <div class="task-trace-label">${esc(label)}</div>
            <div class="code-block task-trace-code">${esc(text)}</div>
        </div>
    `;
}

function renderLiveToolFields(toolCalls) {
    return toolCalls.map((step, index) => [
        renderTraceField(`Tool ${index + 1}`, `${step.tool_name || "tool"} [${step.status}]`, "No tool name"),
        renderTraceField(
            "Timing",
            [step.started_at, step.finished_at, Number.isFinite(step.elapsed_seconds) ? `${step.elapsed_seconds}s` : ""].filter(Boolean).join(" | "),
            "No timing",
        ),
    ].join("")).join("");
}

function renderLiveChildFields(childPipelines) {
    return childPipelines.map((step, index) => [
        renderTraceField(`Pipeline ${index + 1}`, `${step.goal || "(no goal)"} [${step.status}]`, "No child goal"),
        renderTraceField(
            "Nodes",
            [step.child_node_id ? `child=${step.child_node_id}` : "", step.acceptance_node_id ? `accept=${step.acceptance_node_id}` : ""]
                .filter(Boolean)
                .join(" | "),
            "No node ids yet",
        ),
        renderTraceField(
            "Check",
            `${step.check_status || "pending"}${step.started_at || step.finished_at ? ` | ${[step.started_at, step.finished_at].filter(Boolean).join(" -> ")}` : ""}`,
            "No check state",
        ),
    ].join("")).join("");
}

function buildTraceRenderSignature(stepDescriptors) {
    return (Array.isArray(stepDescriptors) ? stepDescriptors : []).map((step) => [
        String(step?.traceKey || ""),
        String(step?.title || ""),
        String(step?.status || ""),
        String(step?.bodyHtml || ""),
    ].join("\u0002")).join("\u0001");
}

function renderExecutionTrace(node, { viewState = null } = {}) {
    if (!U.adFlow) return;
    const effectiveViewState = normalizeTaskDetailViewState(viewState || captureTaskDetailViewState());
    const preservedTraceScrollTop = Number(effectiveViewState?.traceScrollTop || 0);
    const liveFrameMap = liveFramesByNodeId();
    const fallbackLiveFrame = liveFrameMap.get(String(node?.node_id || "").trim()) || null;
    const trace = node?.executionTrace || buildNodeExecutionTrace(node, node, fallbackLiveFrame);
    const stepDescriptors = buildExecutionTraceSteps(trace, node);
    const steps = stepDescriptors.map((step, index) => renderTraceStep({
        ...step,
        open: resolveTraceStepOpenState(step, effectiveViewState, index),
    }));
    let traceList = U.adFlow.querySelector(".task-trace-list");
    if (!(traceList instanceof HTMLElement)) {
        traceList = document.createElement("div");
        traceList.className = "task-trace-list";
        U.adFlow.innerHTML = "";
        U.adFlow.appendChild(traceList);
    }
    const renderSignature = buildTraceRenderSignature(stepDescriptors);
    const renderNodeId = String(node?.node_id || "").trim();
    const shouldReplace = traceList.dataset.renderSignature !== renderSignature
        || traceList.dataset.renderNodeId !== renderNodeId;
    if (shouldReplace) {
        traceList.innerHTML = steps.join("");
        traceList.dataset.renderSignature = renderSignature;
        traceList.dataset.renderNodeId = renderNodeId;
    }
    renderFlowHeading(stepDescriptors.length);
    const traceItems = Array.from(traceList.querySelectorAll(".task-trace-step"));
    traceItems.forEach((item) => {
        if (!(item instanceof HTMLElement)) return;
        const runtimeEl = item.querySelector(".task-trace-runtime");
        if (runtimeEl instanceof HTMLElement) updateRuntimeBadge(item, runtimeEl);
    });
    refreshTaskDetailScrollRegions();
    if (effectiveViewState && shouldReplace) {
        const restoreTraceScroll = () => {
            const currentTraceList = U.adFlow?.querySelector(".task-trace-list");
            if (!(currentTraceList instanceof HTMLElement)) return;
            setElementScrollTop(currentTraceList, preservedTraceScrollTop);
        };
        restoreTraceScroll();
        window.requestAnimationFrame(() => {
            restoreTraceScroll();
            window.requestAnimationFrame(restoreTraceScroll);
        });
    }
    refreshLiveDurationBadges();
    return shouldReplace;
}

function renderFinalOutput(text) {
    if (!U.adOutput) return;
    const nextText = readableText(text, { decodeEscapes: true, emptyText: "暂无最终输出" });
    const changed = U.adOutput.textContent !== nextText;
    if (changed) U.adOutput.textContent = nextText;
    return changed;
}

function renderAcceptanceResult(text) {
    if (!U.adAcceptance) return;
    const nextText = readableText(text, { decodeEscapes: true, emptyText: "暂无验收结果" });
    const changed = U.adAcceptance.textContent !== nextText;
    if (changed) U.adAcceptance.textContent = nextText;
    return changed;
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

function buildTaskTreeRecoveryBubble(text) {
    const bubble = document.createElement("div");
    bubble.className = "task-tree-recovery-bubble";
    bubble.setAttribute("role", "status");
    bubble.setAttribute("aria-live", "polite");
    bubble.textContent = String(text || "").trim();
    return bubble;
}

function renderTree() {
    if (!String(S.treeRootNodeId || "").trim()) return;
    const recoveryNotice = String(S.currentTask?.metadata?.recovery_notice || "").trim();
    S.treeView = buildExecutionTreeFromSnapshot(S.treeRootNodeId, S.treeSelectedRoundByNodeId);
    syncTaskTreeHeaderState(S.treeView);
    if (!S.treeView) {
        U.tree.innerHTML = "";
        if (recoveryNotice) U.tree.appendChild(buildTaskTreeRecoveryBubble(recoveryNotice));
        const emptyState = document.createElement("div");
        emptyState.className = "empty-state";
        emptyState.textContent = "No nodes to display.";
        U.tree.appendChild(emptyState);
        setTaskSelectionEmptyVisible(false);
        return;
    }
    const wrapper = document.createElement("div");
    wrapper.className = "execution-tree";
    const layoutDensity = resolveExecutionTreeDensity(S.treeView);
    const activeLikeCount = Math.max(
        0,
        normalizeInt(S.taskSummary?.active_node_count, 0),
        normalizeInt(S.taskSummary?.runnable_node_count, 0),
        normalizeInt(S.taskSummary?.waiting_node_count, 0),
    );
    S.treeLargeMode = layoutDensity.stats.totalItems > 150 || activeLikeCount > 80;
    wrapper.dataset.layout = layoutDensity.mode;
    wrapper.dataset.totalItems = String(layoutDensity.stats.totalItems);
    wrapper.dataset.maxBreadth = String(layoutDensity.stats.maxBreadth);
    wrapper.dataset.largeTree = S.treeLargeMode ? "true" : "false";
    if (layoutDensity.mode === "wide" || layoutDensity.mode === "dense") {
        wrapper.classList.add("execution-tree--wide");
    }
    if (layoutDensity.mode === "dense") {
        wrapper.classList.add("execution-tree--dense");
    }
    const rootList = document.createElement("ul");
    rootList.className = "execution-tree-list";
    const handleTreeNodeClick = (node, event) => {
        if (S.treePan.suppressClickNodeId && S.treePan.suppressClickNodeId === String(node.node_id || "")) {
            S.treePan.suppressClickNodeId = null;
            return;
        }
        event.stopPropagation();
        const previousNodeId = String(S.selectedNodeId || "").trim();
        const nextNodeId = String(node.node_id || "").trim();
        if (!nextNodeId) return;
        if (previousNodeId && previousNodeId !== nextNodeId) {
            stashTaskDetailViewState({ nodeId: previousNodeId });
        }
        S.selectedNodeId = node.node_id;
        setTaskSelectionEmptyVisible(false);
        syncExecutionTreeSelection(previousNodeId, nextNodeId);
        void showAgent(node, { preserveViewState: false });
    };
    const createTreeNodeButton = (node, { showStaticSubtreeHint = false, mergedBase = false, inspectionBlock = false } = {}) => {
        const title = String(node.title || node.node_id || "");
        const fullTitle = String(node.fullTitle || title);
        const nodeStatus = String(node.visual_state || node.state || "").trim().toLowerCase();
        const displayState = String(node.display_state || node.state || "").trim() || String(node.state || "").toUpperCase();
        const button = document.createElement("button");
        button.type = "button";
        button.className = `execution-tree-node${S.selectedNodeId === node.node_id ? " selected" : ""}`;
        if (mergedBase) button.classList.add("execution-tree-node-base");
        if (inspectionBlock) button.classList.add("is-inspection");
        button.dataset.id = node.node_id;
        button.dataset.kind = node.kind || "execution";
        button.dataset.status = nodeStatus;
        button.title = fullTitle;
        button.setAttribute("aria-pressed", S.selectedNodeId === node.node_id ? "true" : "false");
        button.innerHTML = `${showStaticSubtreeHint ? '<span class="execution-tree-node-note">暂无其他可切换子树</span>' : ""}<span class="execution-tree-node-head"><span class="execution-tree-node-title">${esc(title)}</span><span class="status-badge" data-status="${esc(node.visual_state || node.state || "")}">${esc(displayState)}</span></span>`;
        button.addEventListener("click", (event) => handleTreeNodeClick(node, event));
        return button;
    };
    const walk = (node) => {
        const fullTitle = String(node.fullTitle || node.title || node.node_id || "");
        const nodeStatus = String(node.visual_state || node.state || "").trim().toLowerCase();
        const roundOptions = Array.isArray(node.roundOptions) ? node.roundOptions : [];
        const inspectionNodes = Array.isArray(node.inspectionNodes) ? node.inspectionNodes : [];
        const visibleChildren = Array.isArray(node.children) ? node.children : [];
        const hasSwitchableSubtrees = roundOptions.length > 1;
        const showStaticSubtreeHint = !hasSwitchableSubtrees && visibleChildren.length > 0;
        const item = document.createElement("li");
        item.className = "execution-tree-item";
        item.dataset.status = nodeStatus;
        const stack = document.createElement("div");
        stack.className = "execution-tree-node-stack";
        if (inspectionNodes.length) stack.classList.add("has-inspection");
        inspectionNodes.forEach((inspectionNode) => {
            stack.appendChild(createTreeNodeButton(inspectionNode, { inspectionBlock: true }));
        });
        stack.appendChild(createTreeNodeButton(node, { showStaticSubtreeHint, mergedBase: inspectionNodes.length > 0 }));
        if (hasSwitchableSubtrees) {
            const roundWrap = document.createElement("div");
            roundWrap.className = "execution-tree-node-rounds";
            ["mousedown", "click", "wheel"].forEach((eventName) => {
                roundWrap.addEventListener(eventName, (event) => event.stopPropagation());
            });
            const label = document.createElement("span");
            label.className = "execution-tree-round-label";
            label.textContent = "轮次";
            const select = document.createElement("select");
            select.className = "execution-tree-round-select resource-select";
            select.dataset.resourceSelectLabel = `${fullTitle} 轮次`;
            roundOptions.forEach((round) => {
                const option = document.createElement("option");
                option.value = round.roundId;
                option.textContent = round.isLatest ? `${round.label}（最新）` : round.label;
                select.appendChild(option);
            });
            if (node.selectedRoundId) select.value = node.selectedRoundId;
            select.addEventListener("change", (event) => {
                event.stopPropagation();
                setNodeRoundSelection(node.node_id, event.target.value);
            });
            roundWrap.appendChild(label);
            roundWrap.appendChild(select);
            stack.appendChild(roundWrap);
        }
        item.appendChild(stack);
        if (visibleChildren.length) {
            const branch = document.createElement("ul");
            branch.className = "execution-tree-list";
            branch.dataset.parentStatus = nodeStatus;
            visibleChildren.forEach((child) => branch.appendChild(walk(child)));
            item.appendChild(branch);
        }
        return item;
    };
    rootList.appendChild(walk(S.treeView));
    wrapper.appendChild(rootList);
    wrapper.style.transformOrigin = "0 0";
    wrapper.style.transform = `translate(${Math.round(S.treePan.offsetX)}px, ${Math.round(S.treePan.offsetY)}px) scale(${S.treePan.scale})`;
    U.tree.innerHTML = "";
    if (recoveryNotice) U.tree.appendChild(buildTaskTreeRecoveryBubble(recoveryNotice));
    U.tree.appendChild(wrapper);
    if (typeof enhanceResourceSelects === "function") enhanceResourceSelects();
    if (S.selectedNodeId) {
        const selected = findTreeNode(S.treeView, S.selectedNodeId);
        if (selected) {
            setTaskSelectionEmptyVisible(false);
            const selectedNodeId = String(selected.node_id || "").trim();
            const currentDetailNodeId = String(S.currentNodeDetail?.node_id || "").trim();
            void showAgent(selected, { preserveViewState: selectedNodeId !== "" && selectedNodeId === currentDetailNodeId });
        } else {
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
        const row = document.createElement("div");
        row.className = "artifact-item";
        const changeLabel = String(item?.change_type || "modified") === "created" ? "created" : "modified";
        row.innerHTML = `<strong>${esc(item?.path || "")}</strong><span>${esc(changeLabel)}</span>`;
        U.artifactList.appendChild(row);
    });
    renderArtifactHeading(visibleFiles.length);
    refreshTaskDetailScrollRegions();
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
        change_type: String(item?.change_type || "modified").trim().toLowerCase() === "created" ? "created" : "modified",
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

function renderNodeContextPlaceholder(text = "展开后查看节点完整上下文。") {
    if (U.artifactContent) U.artifactContent.textContent = String(text || "").trim();
}

async function ensureTaskNodeLatestContext(nodeId, { force = false } = {}) {
    const key = String(nodeId || "").trim();
    const taskId = String(S.currentTaskId || "").trim();
    if (!taskId || !key) return null;
    if (!force && S.taskNodeLatestContexts[key]) return S.taskNodeLatestContexts[key];
    if (!force && S.taskNodeLatestContextRequests[key]) return S.taskNodeLatestContextRequests[key];
    const request = (async () => {
        try {
            const payload = await ApiClient.getTaskNodeLatestContext(taskId, key);
            if (!payload) return null;
            if (String(S.currentTaskId || "").trim() !== taskId) return null;
            S.taskNodeLatestContexts = { ...(S.taskNodeLatestContexts || {}), [key]: payload };
            return payload;
        } catch (error) {
            if (!isAbortLike(error)) throw error;
            return null;
        } finally {
            const nextRequests = { ...(S.taskNodeLatestContextRequests || {}) };
            delete nextRequests[key];
            S.taskNodeLatestContextRequests = nextRequests;
        }
    })();
    S.taskNodeLatestContextRequests = { ...(S.taskNodeLatestContextRequests || {}), [key]: request };
    return request;
}

async function loadSelectedNodeLatestContext({ force = false } = {}) {
    const taskId = String(S.currentTaskId || "").trim();
    const nodeId = String(S.selectedNodeId || "").trim();
    if (!taskId || !nodeId) return null;
    renderNodeContextPlaceholder("正在加载节点完整上下文...");
    try {
        const payload = await ensureTaskNodeLatestContext(nodeId, { force });
        if (String(S.currentTaskId || "").trim() !== taskId) return null;
        if (String(S.selectedNodeId || "").trim() !== nodeId) return null;
        const content = String(payload?.content || "");
        renderNodeContextPlaceholder(content.trim() || "当前节点暂无可用上下文快照。");
        return payload;
    } catch (error) {
        renderNodeContextPlaceholder(`加载完整上下文失败：${error?.message || error || "未知错误"}`);
        return null;
    }
}

async function handleNodeContextDisclosureToggle() {
    if (!U.nodeContextDisclosure) return;
    if (!U.nodeContextDisclosure.open) {
        renderNodeContextPlaceholder();
        return;
    }
    await loadSelectedNodeLatestContext({ force: true });
}

function showTaskNodeLoadingState(node) {
    S.currentNodeDetail = { ...node };
    U.detail.style.display = "flex";
    if (U.nodeEmpty) U.nodeEmpty.style.display = "none";
    setTaskSelectionEmptyVisible(false);
    if (U.adRole) U.adRole.hidden = true;
    U.adStatus.textContent = String(node?.display_state || node?.state || node?.status || "");
    U.adStatus.dataset.status = node?.visual_state || node?.state || node?.status || "";
    if (U.adRoundSummary) U.adRoundSummary.textContent = String(node?.roundSummary || "");
    if (U.adFlow) U.adFlow.innerHTML = '<div class="empty-state task-trace-empty">Loading node details...</div>';
    renderFlowHeading(0);
    renderFinalOutput("Loading node output...");
    renderAcceptanceResult("Loading acceptance result...");
    if (U.nodeContextDisclosure) U.nodeContextDisclosure.open = false;
    renderNodeContextPlaceholder();
    U.feedTitle.textContent = formatNodeDetailHeading(node);
    U.feedTitle.title = formatNodeDetailHeading(node, { compact: false });
    setTaskDetailOpen(true);
    icons();
    refreshTaskDetailScrollRegions();
}

async function ensureTaskNodeDetail(nodeId, { force = false } = {}) {
    const key = String(nodeId || "").trim();
    const taskId = String(S.currentTaskId || "").trim();
    if (!taskId || !key) return null;
    if (!force && S.taskNodeDetails[key]) return S.taskNodeDetails[key];
    if (!force && S.taskNodeDetailRequests[key]) return S.taskNodeDetailRequests[key];
    S.taskNodeBusy = true;
    const request = (async () => {
        try {
            const detail = await ApiClient.getTaskNodeDetail(taskId, key);
            if (!detail) return null;
            if (String(S.currentTaskId || "").trim() !== taskId) return null;
            S.taskNodeDetails = { ...S.taskNodeDetails, [key]: detail };
            if (String(S.selectedNodeId || "") === key) S.currentNodeDetail = detail;
            return detail;
        } catch (error) {
            if (!isAbortLike(error)) {
                showToast({ title: "Node load failed", text: error.message || "Unknown error", kind: "error" });
            }
            return S.taskNodeDetails[key] || null;
        } finally {
            const nextRequests = { ...S.taskNodeDetailRequests };
            if (nextRequests[key] === request) delete nextRequests[key];
            S.taskNodeDetailRequests = nextRequests;
            S.taskNodeBusy = Object.keys(nextRequests).length > 0;
        }
    })();
    S.taskNodeDetailRequests = { ...S.taskNodeDetailRequests, [key]: request };
    return request;
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
            const displayState = String(node.display_state || node.state || "").trim() || String(node.state || "").toUpperCase();
            button.dataset.status = nodeStatus;
            button.title = fullTitle;
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
}

function refreshRenderedTreeNodeStatus() {
    scheduleRenderedTreeNodeStatusRefresh();
}

let treeVisualRefreshRaf = 0;

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
    const viewState = consumePendingTaskDetailRestore(nodeId)
        || (preserveViewState ? captureTaskDetailViewState() : getStoredTaskDetailViewState(S.currentTaskId, nodeId));
    if (!S.taskNodeDetails[nodeId] && !hadVisibleCurrentDetail) showTaskNodeLoadingState(node);
    const detail = await ensureTaskNodeDetail(nodeId, { force: forceRefresh });
    if (renderToken !== S.taskDetailRenderToken) return;
    if (String(S.selectedNodeId || "").trim() !== nodeId) return;
    const liveFrameMap = liveFramesByNodeId();
    const mergedNode = {
        ...node,
        ...(detail || {}),
        executionTrace: buildNodeExecutionTrace(node, detail || {}, liveFrameMap.get(nodeId) || null),
    };
    S.currentNodeDetail = mergedNode;
    U.detail.style.display = "flex";
    if (U.nodeEmpty) U.nodeEmpty.style.display = "none";
    setTaskSelectionEmptyVisible(false);
    if (U.adRole) U.adRole.hidden = true;
    if (U.adRoundSummary) U.adRoundSummary.textContent = String(node.roundSummary || "当前节点无派生轮次");
    U.adStatus.textContent = String(mergedNode.display_state || mergedNode.state || mergedNode.status || "");
    U.adStatus.dataset.status = mergedNode.visual_state || mergedNode.state || mergedNode.status || node.visual_state || node.state || "";
    if (U.adRoundSummary) U.adRoundSummary.textContent = String(mergedNode.roundSummary || "");
    const traceChanged = !!renderExecutionTrace(mergedNode, { viewState });
    const outputChanged = !!renderFinalOutput(mergedNode.executionTrace?.final_output || "");
    const acceptanceChanged = !!renderAcceptanceResult(mergedNode.executionTrace?.acceptance_result || "");
    U.feedTitle.textContent = formatNodeDetailHeading(mergedNode);
    U.feedTitle.title = formatNodeDetailHeading(mergedNode, { compact: false });
    setTaskDetailOpen(true);
    icons();
    const nodeChanged = !hadVisibleCurrentDetail || previousDetailNodeId !== nodeId;
    if (U.nodeContextDisclosure) {
        if (nodeChanged) {
            U.nodeContextDisclosure.open = false;
            renderNodeContextPlaceholder();
        } else if (forceRefresh && U.nodeContextDisclosure.open) {
            void loadSelectedNodeLatestContext({ force: true });
        }
    }
    if (!hadVisibleCurrentDetail || traceChanged || outputChanged || acceptanceChanged) {
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
    setTaskDetailOpen(false);
}

function applyTaskPayload(payload) {
    if (!payload || !payload.task) return;
    const previousTaskId = String(S.currentTask?.task_id || "").trim();
    const nextTaskId = String(payload.task?.task_id || "").trim();
    const rootNode = payload.root_node || null;
    const frontier = Array.isArray(payload.frontier) ? payload.frontier : [];
    const recentModelCalls = Array.isArray(payload.recent_model_calls) ? payload.recent_model_calls : [];
    S.currentTask = payload.task;
    S.taskGovernance = normalizeTaskGovernanceState(payload?.governance || payload?.task?.governance || {});
    S.taskSummary = payload.summary || null;
    S.rootNode = rootNode;
    S.frontier = frontier;
    S.recentModelCalls = recentModelCalls;
    S.liveFrameMap = indexTaskLiveFrames(frontier);
    if (rootNode && String(rootNode?.node_id || "").trim()) {
        S.taskNodeDetails = { ...(S.taskNodeDetails || {}), [String(rootNode.node_id || "").trim()]: rootNode };
    }
    resetTaskTreeSnapshotState();
    S.treeSelectedRoundByNodeId = {};
    renderTaskDetailHeader({ resetPromptDisclosure: previousTaskId !== nextTaskId });
    renderTaskGovernancePanel();
    if (U.taskTokenButton) U.taskTokenButton.disabled = !S.currentTask;
    renderTaskTokenStats();
    syncTaskTreeHeaderState(null);
    if (U.tree) U.tree.innerHTML = '<div class="empty-state">Loading task tree...</div>';
    setTaskSelectionEmptyVisible(false);
    hideAgent();
}
