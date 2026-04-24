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
        pending_notice_count: Math.max(0, treeNormalizeInt(value?.pending_notice_count ?? prior?.pending_notice_count, 0)),
        distribution_status: String(value?.distribution_status || prior?.distribution_status || "").trim(),
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

function normalizeTaskGovernance(value = {}) {
    const current = value && typeof value === "object" ? value : {};
    return {
        enabled: !!current.enabled,
        frozen: !!current.frozen,
        review_inflight: !!current.review_inflight,
        depth_baseline: treeNormalizeInt(current.depth_baseline, 1),
        node_count_baseline: treeNormalizeInt(current.node_count_baseline, 1),
        hard_limited_depth: current.hard_limited_depth == null || String(current.hard_limited_depth).trim() === ""
            ? null
            : treeNormalizeInt(current.hard_limited_depth, 0),
        latest_limit_reason: String(current.latest_limit_reason || "").trim(),
        supervision_disabled_after_limit: !!current.supervision_disabled_after_limit,
        history: (Array.isArray(current.history) ? current.history : []).map((item) => ({
            triggered_at: String(item?.triggered_at || "").trim(),
            trigger_reason: String(item?.trigger_reason || "").trim(),
            trigger_snapshot: {
                max_depth: treeNormalizeInt(item?.trigger_snapshot?.max_depth, 0),
                total_nodes: treeNormalizeInt(item?.trigger_snapshot?.total_nodes, 0),
            },
            decision: String(item?.decision || "").trim(),
            decision_reason: String(item?.decision_reason || "").trim(),
            decision_evidence: (Array.isArray(item?.decision_evidence) ? item.decision_evidence : [])
                .map((line) => String(line || "").trim())
                .filter(Boolean),
            limited_depth: item?.limited_depth == null || String(item?.limited_depth).trim() === ""
                ? null
                : treeNormalizeInt(item?.limited_depth, 0),
            error_text: String(item?.error_text || "").trim(),
        })),
    };
}

function mergeTaskGovernance(nextValue = {}, previousValue = {}) {
    const next = normalizeTaskGovernance(nextValue);
    const previous = normalizeTaskGovernance(previousValue);
    if (!next.enabled && previous.enabled) return previous;
    const nextHistory = Array.isArray(next.history) ? next.history : [];
    const previousHistory = Array.isArray(previous.history) ? previous.history : [];
    const looksLikeEmptyFallback = !next.frozen
        && !next.review_inflight
        && !next.latest_limit_reason
        && next.hard_limited_depth == null
        && !next.supervision_disabled_after_limit
        && nextHistory.length === 0;
    if (looksLikeEmptyFallback && previousHistory.length) {
        return {
            ...next,
            enabled: previous.enabled,
            history: previousHistory,
            latest_limit_reason: previous.latest_limit_reason,
            hard_limited_depth: previous.hard_limited_depth,
            supervision_disabled_after_limit: previous.supervision_disabled_after_limit,
            depth_baseline: Math.max(next.depth_baseline, previous.depth_baseline),
            node_count_baseline: Math.max(next.node_count_baseline, previous.node_count_baseline),
        };
    }
    if (previousHistory.length > nextHistory.length && !next.review_inflight && !next.frozen) {
        return {
            ...next,
            history: previousHistory,
            latest_limit_reason: next.latest_limit_reason || previous.latest_limit_reason,
            hard_limited_depth: next.hard_limited_depth == null ? previous.hard_limited_depth : next.hard_limited_depth,
            supervision_disabled_after_limit: next.supervision_disabled_after_limit || previous.supervision_disabled_after_limit,
        };
    }
    return next;
}

function renderTaskGovernancePanel() {
    if (!U.taskGovernancePanel) return;
    const governance = normalizeTaskGovernance(S.taskGovernance || {});
    const history = Array.isArray(governance.history) ? governance.history : [];
    U.taskGovernancePanel.hidden = !governance.enabled;
    if (U.taskGovernancePanel.hidden) return;
    if (U.taskGovernanceSummary) U.taskGovernanceSummary.textContent = "监管记录";
    if (U.taskGovernanceCount) U.taskGovernanceCount.textContent = `${history.length}次`;
    U.taskGovernancePanel.classList.toggle("is-breathing", !!(governance.frozen || governance.review_inflight));
    U.taskGovernancePanel.classList.toggle("is-expanded", !!S.taskGovernanceExpanded);
    const expanded = !!S.taskGovernanceExpanded;
    if (U.taskGovernanceToggle) U.taskGovernanceToggle.setAttribute("aria-expanded", expanded ? "true" : "false");
    if (U.taskGovernanceStatus) U.taskGovernanceStatus.textContent = "";
    if (U.taskGovernanceDecision) U.taskGovernanceDecision.textContent = "";
    if (U.taskGovernanceHistory) {
        U.taskGovernanceHistory.hidden = !expanded;
        U.taskGovernanceHistory.classList.toggle("is-expanded", expanded);
        U.taskGovernanceHistory.innerHTML = history.length
            ? history.slice().reverse().map((item) => `
                <div class="task-governance-entry">
                    <div class="task-governance-entry-head">
                        <span>${esc(item.triggered_at || "未记录时间")}</span>
                        <span>${esc(item.decision || "unknown")}</span>
                    </div>
                    <div class="task-governance-entry-body">
                        <div class="task-governance-line">触发: ${esc(item.trigger_reason || "unknown")}</div>
                        <div class="task-governance-line">快照: depth=${esc(String(item.trigger_snapshot?.max_depth ?? 0))}, nodes=${esc(String(item.trigger_snapshot?.total_nodes ?? 0))}</div>
                        <div class="task-governance-line">理由: ${esc(item.decision_reason || "无")}</div>
                        <div class="task-governance-line">证据: ${esc((Array.isArray(item.decision_evidence) ? item.decision_evidence : []).join(" | ") || "无")}</div>
                        ${item.limited_depth == null ? "" : `<div class="task-governance-line">限制深度: ${esc(String(item.limited_depth))}</div>`}
                        ${item.error_text ? `<div class="task-governance-line">${esc(item.error_text)}</div>` : ""}
                    </div>
                </div>
            `).join("")
            : '<div class="empty-state task-trace-empty">当前任务暂无监管记录。</div>';
    }
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
        distribution_status: String(snapshotNode.distribution_status || "").trim(),
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
        warning: "warning",
        interrupted: "interrupted",
    }[rawStatus] || "");
    const argumentsText = String(step?.arguments_text || step?.arguments_preview || "");
    const outputText = String(step?.output_text || step?.output_preview || step?.text || "");
    const outputRef = String(step?.output_ref || "");
    const startedAt = String(step?.started_at || "");
    const finishedAt = String(step?.finished_at || "");
    return {
        tool_call_id: String(step?.tool_call_id || `summary-tool-${index + 1}`),
        tool_name: String(step?.tool_name || "tool"),
        arguments_text: argumentsText,
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
        recovery_decision: String(step?.recovery_decision || ""),
        related_tool_call_ids: Array.isArray(step?.related_tool_call_ids) ? step.related_tool_call_ids.map((item) => String(item || "")) : [],
        attempted_tools: Array.isArray(step?.attempted_tools) ? step.attempted_tools.map((item) => String(item || "")) : [],
        evidence: Array.isArray(step?.evidence) ? step.evidence.filter((item) => item && typeof item === "object") : [],
        lost_result_summary: String(step?.lost_result_summary || ""),
    };
}

function normalizeSummaryExecutionTrace(summary) {
    const toInt = (value, fallback = 0) => {
        const parsed = Number.parseInt(String(value ?? ""), 10);
        return Number.isFinite(parsed) ? parsed : fallback;
    };
    const stages = (Array.isArray(summary?.stages) ? summary.stages : []).map((stage, stageIndex) => {
        const fallbackTools = (Array.isArray(stage?.tool_calls) ? stage.tool_calls : []).map((step, toolIndex) => (
            normalizeSummaryTraceToolCall(step, toolIndex)
        ));
        const rounds = (Array.isArray(stage?.rounds) ? stage.rounds : []).map((round, roundIndex) => {
            let tools = (Array.isArray(round?.tools) ? round.tools : []).map((step, toolIndex) => (
                normalizeSummaryTraceToolCall(step, toolIndex)
            ));
            if (!tools.length) {
                const roundToolNames = Array.isArray(round?.tool_names) ? round.tool_names : [];
                const roundToolCallIds = Array.isArray(round?.tool_call_ids) ? round.tool_call_ids : [];
                tools = roundToolNames.map((toolName, toolIndex) => normalizeSummaryTraceToolCall({
                    tool_name: toolName,
                    tool_call_id: roundToolCallIds[toolIndex] || "",
                    status: round?.status || stage?.status || "",
                }, toolIndex));
            }
            return {
                round_id: String(round?.round_id || ""),
                round_index: toInt(round?.round_index, roundIndex + 1),
                created_at: String(round?.created_at || ""),
                budget_counted: !!round?.budget_counted,
                tools,
            };
        }).filter((round) => Array.isArray(round?.tools) && round.tools.length);
        return {
            stage_id: String(stage?.stage_id || `summary-stage-${stageIndex + 1}`),
            stage_index: toInt(stage?.stage_index, stageIndex + 1),
            mode: String(stage?.mode || "执行摘要").trim() || "执行摘要",
            status: String(stage?.status || (String(stage?.finished_at || "").trim() ? "完成" : "进行中")).trim() || "进行中",
            stage_goal: String(stage?.stage_goal || "").trim(),
            stage_total_steps: toInt(stage?.tool_round_budget, 0),
            tool_rounds_used: toInt(stage?.tool_rounds_used, rounds.length || (fallbackTools.length ? 1 : 0)),
            created_at: String(stage?.created_at || ""),
            finished_at: String(stage?.finished_at || ""),
            rounds: rounds.length ? rounds : (fallbackTools.length ? [{
                round_id: "",
                round_index: 1,
                created_at: "",
                budget_counted: false,
                tools: fallbackTools,
            }] : []),
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

function firstNonEmptyTraceText(...values) {
    return values
        .map((value) => String(value ?? ""))
        .find((value) => value.trim())
        || "";
}

function buildNodeExecutionTrace(node, detail, liveFrame = null) {
    const fullTrace = detail?.execution_trace && typeof detail.execution_trace === "object" ? detail.execution_trace : {};
    const summaryTrace = normalizeSummaryExecutionTrace(
        detail?.execution_trace_summary && typeof detail.execution_trace_summary === "object"
            ? detail.execution_trace_summary
            : {},
    );
    const source = Object.keys(fullTrace).length ? fullTrace : summaryTrace;
    const normalizedNodeKind = String(source.node_kind ?? detail?.node_kind ?? node?.node_kind ?? "").trim().toLowerCase();
    const normalizedStatus = String(
        source.status
        ?? detail?.status
        ?? node?.status
        ?? node?.state
        ?? ""
    ).trim().toLowerCase();
    const toolSteps = Array.isArray(source.tool_steps) ? source.tool_steps : [];
    const stages = (Array.isArray(source.stages) ? source.stages : []).map((stage, index) => normalizeExecutionStageTrace(stage, index));
    const initialPrompt = firstNonEmptyTraceText(
        source.initial_prompt,
        detail?.prompt,
        detail?.goal,
        node?.prompt,
        node?.goal,
        node?.input,
    );
    const finalOutput = firstNonEmptyTraceText(
        source.final_output,
        detail?.final_output,
        detail?.final_output_preview,
        node?.final_output,
        normalizedStatus === "failed" ? detail?.failure_reason : "",
        normalizedStatus === "failed" ? node?.failure_reason : "",
    );
    const acceptanceResult = firstNonEmptyTraceText(
        source.acceptance_result,
        detail?.check_result,
        node?.check_result,
    );
    return {
        initial_prompt: initialPrompt,
        tool_steps: toolSteps.map((step) => ({
            tool_call_id: String(step?.tool_call_id || ""),
            tool_name: String(step?.tool_name || "tool"),
            arguments_text: String(step?.arguments_text || step?.arguments_preview || ""),
            output_text: String(step?.output_text || step?.output_preview || step?.text || ""),
            output_ref: String(step?.output_ref || ""),
            started_at: String(step?.started_at || ""),
            finished_at: String(step?.finished_at || ""),
            elapsed_seconds: Number.isFinite(Number(step?.elapsed_seconds)) ? Number(step.elapsed_seconds) : null,
            status: ["running", "success", "error", "warning", "interrupted"].includes(String(step?.status || ""))
                ? String(step.status)
                : "info",
            recovery_decision: String(step?.recovery_decision || ""),
            related_tool_call_ids: Array.isArray(step?.related_tool_call_ids) ? step.related_tool_call_ids.map((item) => String(item || "")) : [],
            attempted_tools: Array.isArray(step?.attempted_tools) ? step.attempted_tools.map((item) => String(item || "")) : [],
            evidence: Array.isArray(step?.evidence) ? step.evidence.filter((item) => item && typeof item === "object") : [],
            lost_result_summary: String(step?.lost_result_summary || ""),
        })),
        stages,
        live_tool_calls: normalizeLiveToolCalls(liveFrame),
        live_child_pipelines: normalizeLiveChildPipelines(liveFrame),
        final_output: finalOutput,
        acceptance_result: acceptanceResult || (normalizedNodeKind === "acceptance" ? finalOutput : ""),
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
                arguments_text: String(step?.arguments_text || step?.arguments_preview || ""),
                output_text: String(step?.output_text || step?.output_preview || step?.text || ""),
                output_ref: String(step?.output_ref || ""),
                started_at: String(step?.started_at || ""),
                finished_at: String(step?.finished_at || ""),
                elapsed_seconds: Number.isFinite(Number(step?.elapsed_seconds)) ? Number(step.elapsed_seconds) : null,
                status: ["running", "success", "error", "warning", "interrupted"].includes(String(step?.status || ""))
                    ? String(step.status)
                    : "info",
                recovery_decision: String(step?.recovery_decision || ""),
                related_tool_call_ids: Array.isArray(step?.related_tool_call_ids) ? step.related_tool_call_ids.map((item) => String(item || "")) : [],
                attempted_tools: Array.isArray(step?.attempted_tools) ? step.attempted_tools.map((item) => String(item || "")) : [],
                evidence: Array.isArray(step?.evidence) ? step.evidence.filter((item) => item && typeof item === "object") : [],
                lost_result_summary: String(step?.lost_result_summary || ""),
            })),
        })).filter((round) => Array.isArray(round?.tools) && round.tools.length),
    };
}

function stageTraceStatus(stage) {
    return ({
        "进行中": "running",
        "in_progress": "running",
        "running": "running",
        "active": "running",
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
    if (!tools.length) return "info";
    if (tools.some((item) => ["warning", "interrupted"].includes(String(item?.status || "")))) {
        return "warning";
    }
    if (tools.some((item) => String(item?.status || "") === "running" || String(item?.status || "") === "queued")) {
        return "running";
    }
    return "success";
}

function nodeFinalTraceStatus(node) {
    if (String(node?.state || "") === "in_progress") return "running";
    if (String(node?.state || "") === "failed") return "error";
    return "success";
}

function renderTraceStep({ traceKey = "", title, status = "info", statusLabel = "", open = false, bodyHtml = "", showRuntime = true, showStatus = true, extraClass = "" }) {
    const classes = ["interaction-step", "task-trace-step", esc(status)];
    const normalizedExtraClass = String(extraClass || "").trim();
    if (normalizedExtraClass) classes.push(esc(normalizedExtraClass));
    const sideParts = [];
    if (showRuntime) sideParts.push('<span class="task-trace-runtime" hidden></span>');
    if (showStatus) {
        sideParts.push(`<span class="interaction-step-status">${esc(String(statusLabel || "").trim() || traceStatusLabel(status))}</span>`);
    }
    return `
        <details class="${classes.join(" ")}" data-trace-key="${esc(traceKey)}" data-default-open="${open ? "true" : "false"}"${open ? " open" : ""}>
            <summary class="task-trace-summary">
                <span class="interaction-step-lead">
                    <span class="interaction-step-title">${esc(title)}</span>
                </span>
                <span class="interaction-step-side">
                    ${sideParts.join("")}
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

function buildExecutionRoundToolKey(round, step, toolIndex) {
    const roundKey = String(round?.round_id || round?.round_index || "round").trim() || "round";
    const stepKey = String(step?.tool_call_id || toolIndex).trim() || String(toolIndex);
    return `${roundKey}:tool:${stepKey}`;
}

function renderExecutionRoundToolChip(round, step, toolIndex) {
    const toolKey = buildExecutionRoundToolKey(round, step, toolIndex);
    const status = String(step?.status || "info").trim() || "info";
    const toolName = String(step?.tool_name || "tool").trim() || "tool";
    return `
        <button
            type="button"
            class="task-trace-round-chip ${esc(status)}"
            data-tool-key="${esc(toolKey)}"
            aria-pressed="false"
        >
            <span class="task-trace-round-chip-title">${esc(toolName)}</span>
            <span class="task-trace-round-chip-status">${esc(traceStatusLabel(status))}</span>
        </button>
    `;
}

function renderExecutionRoundToolPanel(round, step, toolIndex) {
    const toolKey = buildExecutionRoundToolKey(round, step, toolIndex);
    const toolName = String(step?.tool_name || "tool").trim() || "tool";
    const evidenceSummary = (Array.isArray(step?.evidence) ? step.evidence : [])
        .map((item) => [String(item?.kind || "").trim(), String(item?.path || item?.ref || "").trim(), String(item?.note || "").trim()].filter(Boolean).join(" | "))
        .filter(Boolean)
        .join("\n");
    const recoveryFields = String(step?.tool_name || "") === "recovery_check"
        ? [
            renderTraceField("恢复检查结论", step?.recovery_decision, "暂无恢复检查结论"),
            renderTraceField("之前尝试执行了", (Array.isArray(step?.attempted_tools) ? step.attempted_tools : []).join(", "), "暂无尝试记录"),
            renderTraceField("证据摘要", evidenceSummary || step?.lost_result_summary, "暂无恢复证据"),
        ].join("")
        : "";
    return `
        <section class="task-trace-round-panel" data-tool-key="${esc(toolKey)}" hidden>
            <div class="task-trace-round-panel-title">${esc(toolName)}</div>
            ${[
                renderTraceField("参数", step?.arguments_text, "无参数"),
                renderTraceOutputField(
                    "工具输出",
                    step?.output_text,
                    step?.output_ref,
                    String(step?.status || "") === "running" ? "等待工具输出..." : "暂无工具输出",
                    { decodeEscapes: true },
                ),
                recoveryFields,
            ].join("")}
        </section>
    `;
}

function renderExecutionRoundToolStrip(round, tools) {
    const toolList = Array.isArray(tools) ? tools : [];
    if (!toolList.length) {
        return renderTraceField("工具", "", "本轮暂无工具记录");
    }
    const roundKey = String(round?.round_id || round?.round_index || "").trim();
    return `
        <div class="task-trace-round-tools" data-round-key="${esc(roundKey)}" data-active-tool-key="">
            <div class="task-trace-round-strip">
                ${toolList.map((step, toolIndex) => renderExecutionRoundToolChip(round, step, toolIndex)).join("")}
            </div>
            <div class="task-trace-round-panels">
                ${toolList.map((step, toolIndex) => renderExecutionRoundToolPanel(round, step, toolIndex)).join("")}
            </div>
        </div>
    `;
}

function setTraceRoundActiveTool(roundHost, nextToolKey = "") {
    if (!(roundHost instanceof HTMLElement)) return;
    const normalizedToolKey = String(nextToolKey || "").trim();
    roundHost.dataset.activeToolKey = normalizedToolKey;
    const chips = Array.from(roundHost.querySelectorAll(".task-trace-round-chip"));
    const panels = Array.from(roundHost.querySelectorAll(".task-trace-round-panel"));
    chips.forEach((chip) => {
        const isActive = normalizedToolKey && String(chip.dataset.toolKey || "").trim() === normalizedToolKey;
        chip.classList.toggle("is-active", !!isActive);
        chip.setAttribute("aria-pressed", isActive ? "true" : "false");
    });
    panels.forEach((panel) => {
        const isActive = normalizedToolKey && String(panel.dataset.toolKey || "").trim() === normalizedToolKey;
        panel.hidden = !isActive;
        panel.classList.toggle("is-active", !!isActive);
        if (isActive) hydrateTraceOutputBlocks(panel);
    });
}

function bindTraceRoundToolStrips(traceList) {
    if (!(traceList instanceof HTMLElement) || traceList.dataset.roundToolBindings === "true") return;
    traceList.dataset.roundToolBindings = "true";
    traceList.addEventListener("click", (event) => {
        const chip = event.target instanceof Element ? event.target.closest(".task-trace-round-chip") : null;
        if (!(chip instanceof HTMLElement) || !traceList.contains(chip)) return;
        const roundHost = chip.closest(".task-trace-round-tools");
        if (!(roundHost instanceof HTMLElement)) return;
        const toolKey = String(chip.dataset.toolKey || "").trim();
        const currentToolKey = String(roundHost.dataset.activeToolKey || "").trim();
        setTraceRoundActiveTool(roundHost, currentToolKey === toolKey ? "" : toolKey);
        if (typeof scheduleTaskDetailSessionPersist === "function") scheduleTaskDetailSessionPersist();
    });
}

function bindTraceOutputAutoLoad(traceList) {
    if (!(traceList instanceof HTMLElement) || traceList.dataset.traceOutputBindings === "true") return;
    traceList.dataset.traceOutputBindings = "true";
    traceList.addEventListener("toggle", (event) => {
        const traceStep = event.target instanceof Element ? event.target.closest(".task-trace-step") : null;
        if (!(traceStep instanceof HTMLElement) || !traceList.contains(traceStep) || !traceStep.open) return;
        hydrateTraceOutputBlocks(traceStep);
    }, true);
}

function renderExecutionStageRoundBlock(stage, round, index) {
    const tools = Array.isArray(round?.tools) ? round.tools : [];
    const time = round?.created_at ? formatCompactTime(round.created_at) : "";
    return `
        <section class="task-trace-round-group" data-round-key="${esc(String(round?.round_id || round?.round_index || index + 1).trim())}">
            ${time ? `<div class="task-trace-round-meta">${esc(time)}</div>` : ""}
            ${renderExecutionRoundToolStrip(round, tools)}
        </section>
    `;
}

function renderExecutionStageRounds(stage) {
    const rounds = Array.isArray(stage?.rounds) ? stage.rounds : [];
    if (!rounds.length) {
        return renderTraceField("阶段轮次", "", "当前阶段暂无工具轮次");
    }
    return rounds.map((round, index) => renderExecutionStageRoundBlock(stage, round, index)).join("");
}

function displayTaskStageStatus(status) {
    return ({
        "进行中": "进行中",
        "in_progress": "进行中",
        "running": "进行中",
        "active": "进行中",
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
    const progress = formatExecutionStageProgress(stage);
    const time = stage?.created_at ? formatCompactTime(stage.created_at) : "";
    const meta = [progress, time].filter(Boolean).join(" · ");
    return `${title}${meta ? ` · ${meta}` : ""}`;
}

function countBudgetedExecutionStageRounds(stage) {
    const rounds = Array.isArray(stage?.rounds) ? stage.rounds : [];
    let sawBudgetMarker = false;
    let countedRounds = 0;
    rounds.forEach((round) => {
        if (!round || typeof round !== "object") return;
        if (!Object.prototype.hasOwnProperty.call(round, "budget_counted")) return;
        sawBudgetMarker = true;
        if (round.budget_counted) countedRounds += 1;
    });
    return sawBudgetMarker ? countedRounds : rounds.length;
}

function formatExecutionStageProgress(stage) {
    const totalRounds = normalizeInt(stage?.stage_total_steps, 0);
    if (totalRounds <= 0) return "";
    const explicitUsed = normalizeInt(stage?.tool_rounds_used, 0);
    const inferredUsed = countBudgetedExecutionStageRounds(stage);
    const usedRounds = Math.max(explicitUsed, inferredUsed);
    return `${Math.min(usedRounds, totalRounds)}/${totalRounds}`;
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
                statusLabel: displayTaskStageStatus(stage.status),
                open: index === trace.stages.length - 1,
                bodyHtml: renderExecutionStageRounds(stage),
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
                renderTraceOutputField(
                    "Output",
                    step.output_text,
                    step.output_ref,
                    step.status === "running" ? "Waiting for tool output..." : "No tool output",
                    { decodeEscapes: true },
                ),
            ].join(""),
        })),
    ];
}

function messageListStatusDescriptor(status) {
    const normalized = String(status || "").trim().toLowerCase();
    if (normalized === "pending") {
        return { key: "warning", label: "待处理" };
    }
    if (normalized === "consumed") {
        return { key: "info", label: "已并入上下文" };
    }
    return { key: "info", label: normalized || "已接收" };
}

function formatMessageListTitle(entry, index) {
    const time = String(entry?.received_at || entry?.consumed_at || "").trim();
    const formattedTime = time ? formatCompactTime(time) : `消息 ${index + 1}`;
    const status = messageListStatusDescriptor(entry?.status);
    return `${formattedTime} · ${status.label}`;
}

function summarizeMessageDeliveries(deliveries = []) {
    const lines = (Array.isArray(deliveries) ? deliveries : [])
        .map((item) => {
            const targetTitle = String(item?.target_title || item?.target_node_id || "").trim() || "未命名节点";
            const targetNodeId = String(item?.target_node_id || "").trim();
            const message = String(item?.message || "").trim();
            const status = String(item?.status || "").trim();
            const parts = [targetTitle];
            if (targetNodeId) parts.push(`(${targetNodeId})`);
            if (message) parts.push(`: ${message}`);
            if (status) parts.push(` [${status}]`);
            return parts.join("");
        })
        .filter(Boolean);
    return lines.join("\n");
}

function buildNodeMessageListSteps(node = {}) {
    const entries = Array.isArray(node?.message_list) ? node.message_list : [];
    return entries.map((entry, index) => {
        const status = messageListStatusDescriptor(entry?.status);
        return {
            traceKey: `message:${String(entry?.notification_id || entry?.epoch_id || index).trim() || index}`,
            title: formatMessageListTitle(entry, index),
            status: status.key,
            showStatus: false,
            open: index === 0,
            bodyHtml: [
                renderTraceField("状态", status.label, "已接收"),
                renderTraceField("接收时间", entry?.received_at || entry?.consumed_at, "暂无时间"),
                renderTraceField("消息内容", entry?.message, "暂无消息内容"),
                renderTraceField("分发情况", summarizeMessageDeliveries(entry?.deliveries), "无"),
            ].join(""),
        };
    });
}

function spawnReviewRoundStatus(round = {}) {
    const allowedIndexes = Array.isArray(round?.allowed_indexes) ? round.allowed_indexes : [];
    const blockedSpecs = Array.isArray(round?.blocked_specs) ? round.blocked_specs : [];
    if (blockedSpecs.length && !allowedIndexes.length) return "error";
    if (allowedIndexes.length && !blockedSpecs.length) return "success";
    if (allowedIndexes.length || blockedSpecs.length) return "info";
    return "info";
}

function summarizeRequestedSpawnSpecs(specs = []) {
    const lines = (Array.isArray(specs) ? specs : []).map((spec, index) => {
        const goal = String(spec?.goal || "").trim() || `spec ${index + 1}`;
        const mode = String(spec?.execution_policy?.mode || "").trim() || "focus";
        return `#${index + 1} ${goal} [${mode}]`;
    });
    return lines.join("\n");
}

function summarizeAllowedSpawnEntries(entries = []) {
    const lines = (Array.isArray(entries) ? entries : [])
        .filter((entry) => String(entry?.review_decision || "").trim().toLowerCase() === "allowed")
        .map((entry, index) => {
            const goal = String(entry?.goal || "").trim() || `allowed ${index + 1}`;
            const childNodeId = String(entry?.child_node_id || "").trim();
            return childNodeId ? `${goal} -> ${childNodeId}` : goal;
        });
    return lines.join("\n");
}

function summarizeBlockedSpawnEntries(entries = []) {
    const lines = (Array.isArray(entries) ? entries : [])
        .filter((entry) => String(entry?.review_decision || "").trim().toLowerCase() === "blocked")
        .map((entry, index) => {
            const goal = String(entry?.goal || "").trim() || `blocked ${index + 1}`;
            const reason = String(entry?.blocked_reason || "").trim() || "暂无拦截原因";
            return `${goal}: ${reason}`;
        });
    return lines.join("\n");
}

function summarizeBlockedSpawnSuggestions(entries = []) {
    const lines = (Array.isArray(entries) ? entries : [])
        .filter((entry) => String(entry?.review_decision || "").trim().toLowerCase() === "blocked")
        .map((entry, index) => {
            const goal = String(entry?.goal || "").trim() || `blocked ${index + 1}`;
            const suggestion = String(entry?.blocked_suggestion || "").trim() || "暂无操作建议";
            return `${goal}: ${suggestion}`;
        });
    return lines.join("\n");
}

function buildSpawnReviewTraceSteps(rounds = []) {
    return (Array.isArray(rounds) ? rounds : []).map((round, index) => {
        const reviewedAt = String(round?.reviewed_at || "").trim();
        const formattedReviewedAt = reviewedAt
            ? (typeof formatCompactTime === "function" ? formatCompactTime(reviewedAt) : reviewedAt)
            : "";
        const titleSuffix = formattedReviewedAt ? ` · ${formattedReviewedAt}` : ` · 第${index + 1}轮`;
        return {
            traceKey: `spawn-review:${String(round?.round_id || index).trim() || index}`,
            title: `派生记录${titleSuffix}`,
            status: spawnReviewRoundStatus(round),
            showStatus: false,
            open: false,
            bodyHtml: [
                renderTraceField("原始请求", summarizeRequestedSpawnSpecs(round?.requested_specs), "本轮无原始派生请求"),
                renderTraceField("放行结果", summarizeAllowedSpawnEntries(round?.entries), "本轮无放行派生"),
                renderTraceField("被拦截原因", summarizeBlockedSpawnEntries(round?.entries), "本轮无被拦截项"),
                renderTraceField("操作建议", summarizeBlockedSpawnSuggestions(round?.entries), "本轮无额外建议"),
            ].join(""),
        };
    });
}

function renderMessageList(node, { viewState = null } = {}) {
    if (!U.adMessages) return false;
    const effectiveViewState = normalizeTaskDetailViewState(viewState || captureTaskDetailViewState());
    const preservedScrollTop = Number(effectiveViewState?.messageScrollTop || 0);
    const stepDescriptors = buildNodeMessageListSteps(node);
    const steps = stepDescriptors.map((step, index) => renderTraceStep({
        ...step,
        open: resolveTraceStepOpenState(
            step,
            {
                traceItems: Array.isArray(effectiveViewState?.messageItems) ? effectiveViewState.messageItems : [],
            },
            index,
        ),
    }));
    let traceList = U.adMessages.querySelector(".task-trace-list");
    if (!(traceList instanceof HTMLElement)) {
        traceList = document.createElement("div");
        traceList.className = "task-trace-list";
        U.adMessages.innerHTML = "";
        U.adMessages.appendChild(traceList);
    }
    const renderSignature = buildTraceRenderSignature(stepDescriptors);
    const renderNodeId = String(node?.node_id || "").trim();
    const shouldReplace = traceList.dataset.renderSignature !== renderSignature
        || traceList.dataset.renderNodeId !== renderNodeId;
    if (shouldReplace) {
        traceList.innerHTML = steps.length
            ? steps.join("")
            : '<div class="empty-state task-trace-empty">当前节点暂无消息。</div>';
        traceList.dataset.renderSignature = renderSignature;
        traceList.dataset.renderNodeId = renderNodeId;
    }
    renderMessageHeading(stepDescriptors.length);
    if (shouldReplace) {
        const restoreScroll = () => {
            const currentTraceList = U.adMessages?.querySelector(".task-trace-list");
            if (!(currentTraceList instanceof HTMLElement)) return;
            setElementScrollTop(currentTraceList, preservedScrollTop);
        };
        restoreScroll();
        window.requestAnimationFrame(() => {
            restoreScroll();
            window.requestAnimationFrame(restoreScroll);
        });
    }
    return shouldReplace;
}

function renderSpawnReviewTrace(node, { viewState = null } = {}) {
    if (!U.adSpawnReviews) return false;
    const effectiveViewState = normalizeTaskDetailViewState(viewState || captureTaskDetailViewState());
    const preservedScrollTop = Number(effectiveViewState?.spawnReviewScrollTop || 0);
    const rounds = Array.isArray(node?.spawn_review_rounds) ? node.spawn_review_rounds : [];
    const stepDescriptors = buildSpawnReviewTraceSteps(rounds);
    const steps = stepDescriptors.map((step, index) => renderTraceStep({
        ...step,
        open: resolveTraceStepOpenState(
            step,
            {
                traceItems: Array.isArray(effectiveViewState?.spawnReviewItems) ? effectiveViewState.spawnReviewItems : [],
            },
            index,
        ),
    }));
    let traceList = U.adSpawnReviews.querySelector(".task-trace-list");
    if (!(traceList instanceof HTMLElement)) {
        traceList = document.createElement("div");
        traceList.className = "task-trace-list";
        U.adSpawnReviews.innerHTML = "";
        U.adSpawnReviews.appendChild(traceList);
    }
    const renderSignature = buildTraceRenderSignature(stepDescriptors);
    const renderNodeId = String(node?.node_id || "").trim();
    const shouldReplace = traceList.dataset.renderSignature !== renderSignature
        || traceList.dataset.renderNodeId !== renderNodeId;
    if (shouldReplace) {
        traceList.innerHTML = steps.length
            ? steps.join("")
            : '<div class="empty-state task-trace-empty">当前节点暂无派生记录。</div>';
        traceList.dataset.renderSignature = renderSignature;
        traceList.dataset.renderNodeId = renderNodeId;
    }
    renderSpawnReviewHeading(stepDescriptors.length);
    if (shouldReplace) {
        const restoreScroll = () => {
            const currentTraceList = U.adSpawnReviews?.querySelector(".task-trace-list");
            if (!(currentTraceList instanceof HTMLElement)) return;
            setElementScrollTop(currentTraceList, preservedScrollTop);
        };
        restoreScroll();
        window.requestAnimationFrame(() => {
            restoreScroll();
            window.requestAnimationFrame(restoreScroll);
        });
    }
    return shouldReplace;
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
        success: "成功",
        error: "失败",
        warning: "需处理",
        interrupted: "已中断",
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

function renderTraceOutputField(label, value, outputRef = "", emptyText = "暂无内容", { decodeEscapes = false } = {}) {
    const text = readableText(value, { decodeEscapes, emptyText });
    const normalizedRef = String(outputRef || "").trim();
    const refAttrs = normalizedRef
        ? ` data-output-ref="${esc(normalizedRef)}" data-empty-text="${esc(String(emptyText || ""))}"`
        : "";
    return `
        <div class="task-trace-field">
            <div class="task-trace-label">${esc(label)}</div>
            <div class="code-block task-trace-code task-trace-output-value"${refAttrs}>${esc(text)}</div>
        </div>
    `;
}

function hydrateTraceOutputBlocks(root) {
    if (!(root instanceof HTMLElement)) return;
    if (typeof ensureTraceOutputCodeBlockContent !== "function") return;
    const selector = ".task-trace-output-value[data-output-ref]";
    const outputBlocks = Array.from(root.querySelectorAll(selector));
    if (!outputBlocks.length) {
        const single = root.querySelector(selector);
        if (single instanceof HTMLElement) outputBlocks.push(single);
    }
    outputBlocks.forEach((block) => {
        if (!(block instanceof HTMLElement)) return;
        void ensureTraceOutputCodeBlockContent(block);
    });
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
    bindTraceRoundToolStrips(traceList);
    bindTraceOutputAutoLoad(traceList);
    traceItems.filter((item) => item instanceof HTMLElement && item.open).forEach((item) => hydrateTraceOutputBlocks(item));
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

function activeTaskDistributionState() {
    const distribution = S.taskRuntimeSummary?.distribution;
    if (distribution && typeof distribution === "object") {
        const activeEpochId = String(distribution.active_epoch_id || "").trim();
        const state = String(distribution.state || "").trim();
        const mode = String(distribution.mode || "").trim();
        if (state === "resume_ready") {
            return { ...distribution, ui_mode: "pending_notice" };
        }
        if (mode === "task_wide_barrier") {
            if (
                activeEpochId
                || state
                || (Array.isArray(distribution.blocked_node_ids) && distribution.blocked_node_ids.length)
                || (Array.isArray(distribution.pending_notice_node_ids) && distribution.pending_notice_node_ids.length)
            ) {
                return { ...distribution, ui_mode: "distribution" };
            }
        }
        if (activeEpochId || state) {
            return { ...distribution, ui_mode: "distribution" };
        }
    }
    const rootNode = treeSnapshotNode(S.treeRootNodeId);
    const pendingNoticeCount = Math.max(0, treeNormalizeInt(rootNode?.pending_notice_count, 0));
    if (pendingNoticeCount > 0) {
        return {
            active_epoch_id: "",
            state: "",
            frontier_node_ids: [],
            pending_notice_count: pendingNoticeCount,
            ui_mode: "pending_notice",
        };
    }
    return null;
}

function buildTaskTreeDistributionBubble(text = "") {
    const distributionState = activeTaskDistributionState();
    const fallbackText = distributionState?.ui_mode === "pending_notice"
        ? "接收到新消息，等待节点处理"
        : "接收到新消息，分发中";
    const bubble = document.createElement("div");
    bubble.className = "task-tree-distribution-bubble";
    bubble.setAttribute("role", "status");
    bubble.setAttribute("aria-live", "polite");
    bubble.textContent = String(text || "").trim() || fallbackText;
    return bubble;
}

function renderTree() {
    if (!String(S.treeRootNodeId || "").trim()) return;
    const recoveryNotice = String(S.currentTask?.metadata?.recovery_notice || "").trim();
    const distributionState = activeTaskDistributionState();
    S.treeView = buildExecutionTreeFromSnapshot(S.treeRootNodeId, S.treeSelectedRoundByNodeId);
    syncTaskTreeHeaderState(S.treeView);
    if (!S.treeView) {
        U.tree.innerHTML = "";
        if (recoveryNotice) U.tree.appendChild(buildTaskTreeRecoveryBubble(recoveryNotice));
        if (distributionState) U.tree.appendChild(buildTaskTreeDistributionBubble());
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
    if (distributionState) {
        wrapper.classList.add("execution-tree--distribution-active");
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
        if (String(node.distribution_status || "").trim() === "barrier_blocked") {
            button.classList.add("execution-tree-node--distribution-blocked");
        }
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
    if (distributionState) U.tree.appendChild(buildTaskTreeDistributionBubble());
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

function renderNodeContextPlaceholder(text = "展开后查看节点完整上下文。", { preserveViewState = false } = {}) {
    const viewState = preserveViewState && typeof captureTaskDetailViewState === "function"
        ? captureTaskDetailViewState()
        : null;
    if (U.artifactContent) {
        const decodedText = readableText(text, {
            decodeEscapes: true,
            emptyText: "展开后查看节点完整上下文。",
        });
        U.artifactContent.textContent = String(decodedText || "")
            .replace(/\\r\\n/g, "\n")
            .replace(/\\n/g, "\n")
            .replace(/\\r/g, "\n")
            .replace(/\\t/g, "\t")
            .replace(/\\"/g, '"')
            .replace(/\\\\/g, "\\")
            .trim();
    }
    if (viewState && typeof restoreTaskDetailViewState === "function") {
        restoreTaskDetailViewState(viewState, {
            detail: true,
            trace: false,
            traceItems: false,
            artifactList: false,
            artifactContent: true,
        });
    }
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
    renderNodeContextPlaceholder("正在加载节点完整上下文...", { preserveViewState: true });
    try {
        const payload = await ensureTaskNodeLatestContext(nodeId, { force });
        if (String(S.currentTaskId || "").trim() !== taskId) return null;
        if (String(S.selectedNodeId || "").trim() !== nodeId) return null;
        const content = String(payload?.content || "");
        renderNodeContextPlaceholder(content.trim() || "当前节点暂无可用上下文快照。", { preserveViewState: true });
        return payload;
    } catch (error) {
        renderNodeContextPlaceholder(`加载完整上下文失败：${error?.message || error || "未知错误"}`, { preserveViewState: true });
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
    if (U.adMessages) U.adMessages.innerHTML = '<div class="empty-state task-trace-empty">Loading node details...</div>';
    if (U.adSpawnReviews) U.adSpawnReviews.innerHTML = '<div class="empty-state task-trace-empty">Loading node details...</div>';
    renderFlowHeading(0);
    renderMessageHeading(0);
    renderSpawnReviewHeading(0);
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

function executionTraceSummaryHasRoundBoundaries(summary) {
    const stages = Array.isArray(summary?.stages) ? summary.stages : [];
    return stages.some((stage) => Array.isArray(stage?.rounds) && stage.rounds.length > 0);
}

function taskNodePatchSummary(nodeId) {
    const key = String(nodeId || "").trim();
    return key ? ((S.taskNodePatchSummaries || {})[key] || null) : null;
}

function taskNodeTerminalSummaryIncomplete(detail) {
    if (!detail || typeof detail !== "object") return true;
    const normalizedStatus = String(detail?.status || detail?.state || "").trim().toLowerCase();
    if (!["success", "failed"].includes(normalizedStatus)) return false;
    return !String(detail?.final_output || "").trim()
        && !String(detail?.failure_reason || "").trim()
        && !String(detail?.check_result || "").trim();
}

function taskNodePatchSummaryIsNewer(detail, patchSummary) {
    if (!detail || typeof detail !== "object" || !patchSummary || typeof patchSummary !== "object") return false;
    const detailUpdatedAt = String(detail?.updated_at || "").trim();
    const patchUpdatedAt = String(patchSummary?.updated_at || "").trim();
    if (patchUpdatedAt && detailUpdatedAt && patchUpdatedAt !== detailUpdatedAt) return true;
    const normalizedPatchStatus = String(patchSummary?.status || "").trim().toLowerCase();
    const normalizedDetailStatus = String(detail?.status || detail?.state || "").trim().toLowerCase();
    if (normalizedPatchStatus && normalizedPatchStatus !== normalizedDetailStatus) return true;
    if (String(patchSummary?.final_output || "").trim() && String(patchSummary?.final_output || "").trim() !== String(detail?.final_output || "").trim()) {
        return true;
    }
    if (String(patchSummary?.failure_reason || "").trim() && String(patchSummary?.failure_reason || "").trim() !== String(detail?.failure_reason || "").trim()) {
        return true;
    }
    if (String(patchSummary?.check_result || "").trim() && String(patchSummary?.check_result || "").trim() !== String(detail?.check_result || "").trim()) {
        return true;
    }
    return false;
}

function taskNodeDetailNeedsRefresh(detail, { patchSummary = null } = {}) {
    if (!detail || typeof detail !== "object") return true;
    if (String(detail?.detail_level || "").trim().toLowerCase() !== "full") return true;
    if (taskNodeTerminalSummaryIncomplete(detail) && taskNodePatchSummaryIsNewer(detail, patchSummary)) return true;
    const summary = detail.execution_trace_summary;
    if (!summary || typeof summary !== "object") return false;
    const stages = Array.isArray(summary.stages) ? summary.stages : [];
    if (!stages.length) return false;
    const hasToolCalls = stages.some((stage) => Array.isArray(stage?.tool_calls) && stage.tool_calls.length > 0);
    if (!hasToolCalls) return false;
    return !executionTraceSummaryHasRoundBoundaries(summary);
}

async function ensureTaskNodeDetail(nodeId, { force = false } = {}) {
    const key = String(nodeId || "").trim();
    const taskId = String(S.currentTaskId || "").trim();
    if (!taskId || !key) return null;
    const cachedDetail = S.taskNodeDetails[key];
    const patchSummary = taskNodePatchSummary(key);
    if (!force && cachedDetail && !taskNodeDetailNeedsRefresh(cachedDetail, { patchSummary })) return cachedDetail;
    if (!force && S.taskNodeDetailRequests[key]) return S.taskNodeDetailRequests[key];
    S.taskNodeBusy = true;
    const request = (async () => {
        try {
            const detail = await ApiClient.getTaskNodeDetail(taskId, key, { detailLevel: "full" });
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
    const messagesChanged = !!renderMessageList(mergedNode, { viewState });
    const spawnReviewChanged = !!renderSpawnReviewTrace(mergedNode, { viewState });
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
    S.taskGovernance = mergeTaskGovernance(
        payload.governance || payload.runtime_summary?.governance || {},
        taskChanged ? {} : (S.taskGovernance || {}),
    );
    if (taskChanged) S.taskGovernanceExpanded = false;
    S.rootNode = rootNode;
    S.frontier = frontier;
    S.recentModelCalls = recentModelCalls;
    S.taskModelCallsPageSize = typeof TASK_MODEL_CALLS_PAGE_SIZE === "number" && TASK_MODEL_CALLS_PAGE_SIZE > 0
        ? TASK_MODEL_CALLS_PAGE_SIZE
        : 100;
    if (taskChanged) S.taskModelCallsPage = 1;
    S.liveFrameMap = indexTaskLiveFrames(frontier);
    if (rootNode && String(rootNode?.node_id || "").trim()) {
        S.taskNodeDetails = { ...(S.taskNodeDetails || {}), [String(rootNode.node_id || "").trim()]: rootNode };
    }
    resetTaskTreeSnapshotState();
    S.treeSelectedRoundByNodeId = {};
    renderTaskDetailHeader({ resetPromptDisclosure: taskChanged });
    renderTaskGovernancePanel();
    if (U.taskTokenButton) U.taskTokenButton.disabled = !S.currentTask;
    renderTaskTokenStats();
    syncTaskTreeHeaderState(null);
    if (U.tree) U.tree.innerHTML = '<div class="empty-state">Loading task tree...</div>';
    setTaskSelectionEmptyVisible(false);
    hideAgent();
}
