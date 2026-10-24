// 执行树状态仓库：快照节点归一化、子节点平铺、状态标签与树模型推导。
// 纯状态函数，不碰 DOM；视图层只消费 buildExecutionTreeFromSnapshot 的结果。
// 节点树不再按派生轮次分组：一个父节点的全部子节点平铺显示（验收节点单独附着）。

function treeNormalizeInt(value, fallback = 0) {
    const numeric = Number.parseInt(String(value ?? ""), 10);
    return Number.isFinite(numeric) ? numeric : fallback;
}

function normalizeTaskTreeSnapshotNode(value = {}, existing = null) {
    const prior = existing && typeof existing === "object" ? existing : {};
    const rounds = (Array.isArray(value?.rounds) ? value.rounds : Array.isArray(prior?.rounds) ? prior.rounds : []);
    // `task.node.patch` 只带状态字段，不带任何子节点列表：合并时必须回退到既有
    // 归一化结果里的 all_child_ids，否则每一次状态补丁都会把这个节点的子节点
    // 从浏览器缓存里抹掉（树枝凭空消失，且 children_fingerprint 没变、不会触发
    // 分支重拉）。
    const priorChildIds = Array.isArray(prior?.auxiliary_child_ids)
        ? prior.auxiliary_child_ids
        : Array.isArray(prior?.all_child_ids)
            ? prior.all_child_ids
            : [];
    const auxiliary = (Array.isArray(value?.auxiliary_child_ids) ? value.auxiliary_child_ids : priorChildIds)
        .map((item) => String(item || "").trim())
        .filter(Boolean);
    const seenChildIds = new Set();
    const allChildIds = [];
    auxiliary.forEach((childId) => {
        if (childId && !seenChildIds.has(childId)) {
            seenChildIds.add(childId);
            allChildIds.push(childId);
        }
    });
    rounds.forEach((round) => {
        (Array.isArray(round?.child_ids) ? round.child_ids : []).forEach((item) => {
            const childId = String(item || "").trim();
            if (childId && !seenChildIds.has(childId)) {
                seenChildIds.add(childId);
                allChildIds.push(childId);
            }
        });
    });
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
        all_child_ids: allChildIds,
        pending_notice_count: Math.max(0, treeNormalizeInt(value?.pending_notice_count ?? prior?.pending_notice_count, 0)),
        parent_visible: value?.parent_visible ?? prior?.parent_visible ?? true,
        acceptance_handshake_state: String(value?.acceptance_handshake_state || prior?.acceptance_handshake_state || "").trim(),
    };
}

function treeSnapshotNode(nodeId) {
    const key = String(nodeId || "").trim();
    if (!key) return null;
    return S.treeNodesById?.[key] || null;
}

function treeTaskIsTerminal(task = S.currentTask) {
    const status = String(task?.status || "").trim().toLowerCase();
    return status === "success" || status === "failed";
}

// 默认只看非终态（决策 C）的前提是任务还在跑：任务本身已结束时不存在"活跃视图"，
// 再按终态过滤只会剩下一根节点。用户手动切换过则以手动值为准（null/undefined = 自动）。
function treeShowTerminalEffective() {
    if (S.treeShowTerminal === true || S.treeShowTerminal === false) return S.treeShowTerminal;
    return treeTaskIsTerminal();
}

// 节点行内小挂件（未读角标 / 展开钮）的渲染签名。就地刷新只能改文字与 class，
// 增删这些元素要重排 DOM；签名一变就说明必须整树重建一次。
function treeNodeChipSignature(node) {
    return [
        node?.expandable ? "1" : "0",
        treeNormalizeInt(node?.hidden_child_count, 0),
        node?.expanded ? "1" : "0",
        treeNormalizeInt(node?.pending_notice_count, 0),
        node?.kind === "acceptance" ? "1" : "0",
    ].join("|");
}

function treeExpandedNodeIds() {
    if (!(S.treeExpandedNodeIds instanceof Set)) S.treeExpandedNodeIds = new Set();
    return S.treeExpandedNodeIds;
}

// 节点行上的展开钮：展开后显示该节点的全部直属子节点（任意状态）。
function toggleTreeNodeExpanded(nodeId) {
    const key = String(nodeId || "").trim();
    if (!key) return false;
    const expanded = treeExpandedNodeIds();
    if (expanded.has(key)) {
        expanded.delete(key);
    } else {
        expanded.add(key);
    }
    return expanded.has(key);
}

function snapshotNodeDefaultRoundId(node) {
    return "";
}

// 兼容旧状态序列化：轮次分组已下线，选择集恒为空。
function normalizeTreeRoundSelections(value) {
    return {};
}

function snapshotNodeSelectedRoundId(node, selections = S.treeSelectedRoundByNodeId) {
    return "";
}

function snapshotNodeVisibleChildIds(node) {
    return (Array.isArray(node?.all_child_ids) ? node.all_child_ids : [])
        .map((item) => String(item || "").trim())
        .filter(Boolean);
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
        snapshotNodeVisibleChildIds(node).forEach((childId) => {
            if (!collected.has(childId)) queue.push(childId);
        });
    }
    return collected;
}

function buildExecutionTreeFromSnapshot(
    nodeId = S.treeRootNodeId,
    seen = new Set(),
    taskPaused = taskPauseDisplayActive(),
    showTerminal = treeShowTerminalEffective(),
    isRoot = true,
    expandedNodeIds = treeExpandedNodeIds(),
) {
    const normalizedNodeId = String(nodeId || "").trim();
    if (!normalizedNodeId || seen.has(normalizedNodeId)) return null;
    const snapshotNode = treeSnapshotNode(normalizedNodeId);
    if (!snapshotNode) return null;
    seen.add(normalizedNodeId);
    const kind = String(snapshotNode.node_kind || "execution").trim().toLowerCase() || "execution";
    const status = String(snapshotNode.status || "unknown").trim().toLowerCase() || "unknown";
    const title = resolveNodeTitle(snapshotNode, snapshotNode);
    const expanded = expandedNodeIds.has(normalizedNodeId);
    const builtChildren = snapshotNodeVisibleChildIds(snapshotNode)
        .map((childId) => buildExecutionTreeFromSnapshot(childId, seen, taskPaused, showTerminal, false, expandedNodeIds))
        .filter(Boolean);
    const inspectionNodes = [];
    const childNodes = [];
    let hiddenChildCount = 0;
    builtChildren.forEach((child) => {
        if (isAcceptanceNodeKind(child?.kind)) {
            // 验收节点无条件附着在被验收节点上：不参与默认过滤。
            inspectionNodes.push(child);
        } else if (expanded || showTerminal || child?.contains_live) {
            // contains_live：终态节点底下还有存活后代时必须显示，否则那些
            // 非终态节点连同祖先链一起消失，违反"非终态全部可见"与祖先链规则。
            childNodes.push(child);
        } else {
            hiddenChildCount += 1;
        }
    });
    const containsLive = !isTerminalTreeNodeStatus(status)
        || builtChildren.some((child) => !!child?.contains_live);
    const inspectionPending = inspectionNodes.some((child) => !isTerminalTreeNodeStatus(child?.state));
    const effectiveIsPaused = effectiveTreeNodePaused(snapshotNode, taskPaused);
    const liveFrame = S.liveFrameMap?.[normalizedNodeId] || null;
    const inTurn = isLiveTurnFrame(liveFrame);
    const stateMeta = resolveTreeNodeStatusLabel(status, {
        kind,
        inTurn,
        modelInFlight: isModelRequestInFlightFrame(liveFrame),
        inspectionPending,
        isPaused: !!snapshotNode.is_paused,
        taskPaused,
        pauseReason: String(snapshotNode.pause_reason || "").trim(),
        phase: String(liveFrame?.phase || "").trim().toLowerCase(),
    });
    // 检验节点常驻显示后，"活跃"必须按真实在执行判定，否则每个未派发的检验节点
    // 都会被计入头部计数。
    const isActiveNode = !isTerminalTreeNodeStatus(status) && (inTurn || effectiveIsPaused);
    const activeNodeCount = (isActiveNode ? 1 : 0)
        + builtChildren.reduce((sum, child) => sum + treeNormalizeInt(child?.activeNodeCount, 0), 0);
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
        inspectionNodes,
        children: childNodes,
        activeNodeCount,
        contains_live: containsLive,
        expanded,
        pending_notice_count: Math.max(0, treeNormalizeInt(snapshotNode.pending_notice_count, 0)),
        hidden_child_count: hiddenChildCount,
        expandable: hiddenChildCount > 0 || (expanded && builtChildren.some((child) => !isAcceptanceNodeKind(child?.kind))),
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

function resolveTreeNodeStatusLabel(status, { kind = "", inTurn = false, modelInFlight = false, inspectionPending = false, isPaused = false, taskPaused = false, pauseReason = "", phase = "" } = {}) {
    const normalizedStatus = String(status || "").trim().toLowerCase() || "unknown";
    if (taskPaused && !isTerminalTreeNodeStatus(normalizedStatus)) {
        return {
            visualState: normalizedStatus,
            displayState: "任务暂停",
        };
    }
    if (isPaused) {
        const abnormal = String(pauseReason || "").trim().toLowerCase() === "error";
        return {
            visualState: abnormal ? "paused_abnormal" : "paused",
            displayState: abnormal ? "\u6682\u505c\u00b7\u5f02\u5e38" : "\u6682\u505c\u00b7\u666e\u901a",
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
    const normalizedPhase = String(phase || "").trim().toLowerCase();
    if (normalizedPhase === "waiting_explicit") {
        return { visualState: "waiting", displayState: "\u7b49\u5f85\uff08\u663e\u5f0f\uff09" };
    }
    if (normalizedPhase === "waiting_acceptance") {
        return { visualState: "waiting", displayState: "\u7b49\u5f85\uff08\u9a8c\u6536\u5f85\u51b3\uff09" };
    }
    if (!inTurn) {
        return { visualState: "waiting", displayState: "\u7b49\u5f85\u4e2d" };
    }
    if (modelInFlight) {
        // \u8bf7\u6c42\u4e2d\u662f\u8fd0\u884c\u4e2d\u7684\u4e00\u79cd\uff0c\u4e0d\u53e6\u8d77\u989c\u8272\uff1a\u6cbf\u7528\u6267\u884c\u84dd\uff0c\u53ea\u628a"\u5728\u7b49 API \u56de\u5b8c"\u8bf4\u51fa\u53e3\u3002
        return { visualState: "running", displayState: "\u8bf7\u6c42\u4e2d" };
    }
    if (normalizedPhase === "waiting_tool_results" || normalizedPhase === "tool_running") {
        return { visualState: "running", displayState: "\u8fd0\u884c\u4e2d\uff08\u5de5\u5177\u5728\u9014\uff09" };
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
