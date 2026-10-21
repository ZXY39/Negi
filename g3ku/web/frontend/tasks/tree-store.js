// 执行树状态仓库：快照节点归一化、轮次选择、状态标签与树模型推导。
// 纯状态函数，不碰 DOM；视图层只消费 buildExecutionTreeFromSnapshot 的结果。

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
        is_paused: value?.is_paused ?? prior?.is_paused ?? false,
        pause_requested: value?.pause_requested ?? prior?.pause_requested ?? false,
        pause_reason: String(value?.pause_reason || prior?.pause_reason || "").trim(),
        pause_remark: String(value?.pause_remark || prior?.pause_remark || "").trim(),
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
        parent_visible: value?.parent_visible ?? prior?.parent_visible ?? true,
        acceptance_handshake_state: String(value?.acceptance_handshake_state || prior?.acceptance_handshake_state || "").trim(),
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

function buildExecutionTreeFromSnapshot(
    nodeId = S.treeRootNodeId,
    selections = S.treeSelectedRoundByNodeId,
    seen = new Set(),
    taskPaused = taskPauseDisplayActive(),
) {
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
        .map((childId) => buildExecutionTreeFromSnapshot(childId, selections, seen, taskPaused))
        .filter(Boolean);
    const inspectionNodes = [];
    const childNodes = [];
    visibleChildren.forEach((child) => {
        if (isAcceptanceNodeKind(child?.kind)) inspectionNodes.push(child);
        else childNodes.push(child);
    });
    const inspectionPending = inspectionNodes.some((child) => !isTerminalTreeNodeStatus(child?.state));
    const effectiveIsPaused = effectiveTreeNodePaused(snapshotNode, taskPaused);
    const liveFrame = S.liveFrameMap?.[normalizedNodeId] || null;
    const inTurn = isLiveTurnFrame(liveFrame);
    const stateMeta = resolveTreeNodeStatusLabel(status, {
        kind,
        inTurn,
        modelInFlight: isModelRequestInFlightFrame(liveFrame),
        distributionFrozen: String(snapshotNode?.distribution_status || "").trim() === "barrier_frozen",
        inspectionPending,
        isPaused: !!snapshotNode.is_paused,
        taskPaused,
        pauseReason: String(snapshotNode.pause_reason || "").trim(),
    });
    // 检验节点常驻显示后，"活跃"必须按真实在执行判定，否则每个未派发的检验节点
    // 都会被计入头部计数。
    const isActiveNode = !isTerminalTreeNodeStatus(status) && (inTurn || effectiveIsPaused);
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
        parent_visible: snapshotNode.parent_visible !== false,
        acceptance_handshake_state: String(snapshotNode.acceptance_handshake_state || "").trim(),
        task_paused: taskPaused,
        effective_is_paused: effectiveIsPaused,
        is_paused: !!snapshotNode.is_paused,
        pause_requested: !!snapshotNode.pause_requested,
        pause_reason: String(snapshotNode.pause_reason || "").trim(),
        pause_remark: String(snapshotNode.pause_remark || "").trim(),
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

function isLiveTurnFrame(frame) {
    if (!frame || typeof frame !== "object") return false;
    if (frame.stale === true) return false;
    if (isWaitingForChildResultsFrame(frame)) return false;
    const phase = String(frame?.phase || "").trim().toLowerCase();
    if (phase === "before_model" || phase === "after_model" || phase === "message_distribution") return true;
    return (Array.isArray(frame?.tool_calls) ? frame.tool_calls : [])
        .some((item) => isActiveChildPipelineStatus(item?.status || ""));
}

function isWaitingForChildResultsFrame(frame) {
    if (!frame || typeof frame !== "object") return false;
    const phase = String(frame?.phase || "").trim().toLowerCase();
    if (phase === "waiting_children" || phase === "waiting_acceptance") return true;
    return (Array.isArray(frame?.child_pipelines) ? frame.child_pipelines : [])
        .some((item) => isActiveChildPipelineStatus(item?.status || ""));
}

const MODEL_REQUEST_IN_FLIGHT_MARKERS = new Set(["model.chat.dispatch", "model.chat.await_response"]);

function isModelRequestInFlightFrame(frame) {
    if (!frame || typeof frame !== "object" || frame.stale === true) return false;
    return MODEL_REQUEST_IN_FLIGHT_MARKERS.has(String(frame.await_marker || "").trim());
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

function taskPauseDisplayActive(task = S.currentTask) {
    return !!(task?.is_paused || task?.pause_requested);
}

function effectiveTreeNodePaused(node, taskPaused = taskPauseDisplayActive()) {
    const status = String(node?.state || node?.status || "").trim().toLowerCase();
    return !isTerminalTreeNodeStatus(status) && (taskPaused || !!node?.is_paused);
}

function resolveTreeNodeStatusLabel(status, { kind = "", inTurn = false, modelInFlight = false, inspectionPending = false, isPaused = false, taskPaused = false, pauseReason = "", distributionFrozen = false } = {}) {
    const normalizedStatus = String(status || "").trim().toLowerCase() || "unknown";
    if (taskPaused && !isTerminalTreeNodeStatus(normalizedStatus)) {
        return {
            visualState: normalizedStatus,
            displayState: "任务暂停",
        };
    }
    if (isPaused) {
        return {
            visualState: normalizedStatus,
            displayState: pauseReason ? `\u5df2\u6682\u505c\uff08${pauseReason}\uff09` : "\u5df2\u6682\u505c",
        };
    }
    if (isTerminalTreeNodeStatus(normalizedStatus)) {
        // 已提交但验收子节点还没结论：交付物还没被接受，不算完成。
        if (normalizedStatus === "success" && inspectionPending) {
            return { visualState: "waiting", displayState: "\u7b49\u5f85\u4e2d" };
        }
        return {
            visualState: normalizedStatus,
            displayState: normalizedStatus.toUpperCase(),
        };
    }
    if (distributionFrozen) {
        // 排空账本已记到该节点：它停在检查点上，等的只有屏障释放。与节点详情消息
        // 条目同一串文案，避免同一个事实出现两种说法。
        return { visualState: "waiting", displayState: "已冻结待释放" };
    }
    if (!inTurn) {
        return { visualState: "waiting", displayState: "\u7b49\u5f85\u4e2d" };
    }
    if (modelInFlight) {
        // \u8bf7\u6c42\u4e2d\u662f\u8fd0\u884c\u4e2d\u7684\u4e00\u79cd\uff0c\u4e0d\u53e6\u8d77\u989c\u8272\uff1a\u6cbf\u7528\u6267\u884c\u84dd\uff0c\u53ea\u628a"\u5728\u7b49 API \u56de\u5b8c"\u8bf4\u51fa\u53e3\u3002
        return { visualState: "running", displayState: "\u8bf7\u6c42\u4e2d" };
    }
    return isAcceptanceNodeKind(kind)
        ? { visualState: "inspecting", displayState: "\u68c0\u9a8c\u4e2d" }
        : { visualState: "running", displayState: "\u8fd0\u884c\u4e2d" };
}

function treeViewChildren(node) {
    return [
        ...(Array.isArray(node?.inspectionNodes) ? node.inspectionNodes : []),
        ...(Array.isArray(node?.children) ? node.children : []),
    ];
}
