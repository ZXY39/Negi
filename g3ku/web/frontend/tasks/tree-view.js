// 执行树视图：整树渲染入口。状态由 tasks/tree-store.js 推导，DOM 注册表在全局 U。

function renderTree() {
    if (!String(S.treeRootNodeId || "").trim()) return;
    // 已离开详情视图：跳过整树重建（含各在途请求与交互回调的收尾渲染），
    // 避免在任务大厅里阻塞主线程；重新进入详情视图必然完整重载。
    if (!treeDetailViewActive()) return;
    // 大任务初始分块加载期间不渲染残缺树，全部块落位后由 loadTaskTreeSnapshot 渲染。
    if (String(S.treeBulkLoadingTaskId || "").trim()
        && String(S.treeBulkLoadingTaskId || "").trim() === String(S.currentTaskId || "").trim()) return;
    maybeShowTaskRecoveryNoticeToast();
    S.treeView = buildExecutionTreeFromSnapshot(
        S.treeRootNodeId,
        new Set(),
        taskPauseDisplayActive(),
        treeShowTerminalEffective(),
        true,
        treeExpandedNodeIds(),
    );
    syncTaskTreeHeaderState(S.treeView);
    if (!S.treeView) {
        // 根节点缺失通常是增量合并损坏了缓存：空态之外再触发一次整表自愈重载。
        scheduleTaskTreeSnapshotSelfHeal();
        U.tree.innerHTML = "";
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
    const createTreeNodeButton = (node, { mergedBase = false, inspectionBlock = false } = {}) => {
        const title = String(node.title || node.node_id || "");
        const fullTitle = String(node.fullTitle || title);
        const nodeStatus = String(node.visual_state || node.state || "").trim().toLowerCase();
        const taskPaused = taskPauseDisplayActive();
        const effectivePaused = effectiveTreeNodePaused(node, taskPaused);
        const displayState = taskNodeDisplayState(node, taskPaused);
        const button = document.createElement("button");
        button.type = "button";
        button.className = `execution-tree-node${S.selectedNodeId === node.node_id ? " selected" : ""}`;
        if (mergedBase) button.classList.add("execution-tree-node-base");
        if (inspectionBlock) button.classList.add("is-inspection");
        button.dataset.id = node.node_id;
        button.dataset.kind = node.kind || "execution";
        button.dataset.treeChips = treeNodeChipSignature(node);
        button.dataset.status = nodeStatus;
        button.dataset.paused = effectivePaused ? "true" : "false";
        button.dataset.pausedReason = String(node.pause_reason || "");
        button.title = node.pause_remark ? `${fullTitle} \u2014 ${node.pause_remark}` : fullTitle;
        button.setAttribute("aria-pressed", S.selectedNodeId === node.node_id ? "true" : "false");
        const pauseAction = !isTerminalTreeNodeStatus(node.state) && !taskPaused
            ? `<span class="execution-tree-node-pause-action" data-node-pause-action="true" role="button" tabindex="0" title="${node.is_paused ? "\u6062\u590d\u8282\u70b9" : "\u6682\u505c\u8282\u70b9"}">${node.is_paused ? "\u25b6" : "\u2161"}</span>`
            : "";
        const acceptanceBadge = String(node.kind || "").trim().toLowerCase() === "acceptance"
            ? '<span class="status-badge acceptance-badge" data-status="acceptance">\u9a8c\u6536</span>'
            : "";
        // 折叠的已结束子节点必须留出入口：默认过滤把它们藏起来后，节点行上看不出
        // 底下还有东西，任务结束后整棵树只剩根节点。
        const hiddenChildCount = treeNormalizeInt(node.hidden_child_count, 0);
        const expandAction = node.expandable
            ? `<span class="execution-tree-expand${node.expanded ? " is-expanded" : ""}" data-node-expand-action="true" role="button" tabindex="0" title="${node.expanded ? "\u6536\u8d77\u5df2\u7ed3\u675f\u5b50\u8282\u70b9" : `\u5c55\u5f00 ${hiddenChildCount} \u4e2a\u5df2\u7ed3\u675f\u5b50\u8282\u70b9`}">${node.expanded ? "\u25be" : "\u25b8"}${hiddenChildCount > 0 ? `<span class="execution-tree-expand-count">${hiddenChildCount}</span>` : ""}</span>`
            : "";
        // 未读消息角标（设计 §10）：信箱里有待并入的消息时，节点行必须自己说出来，
        // 否则用户只能靠进详情页才发现"它在等一条没人看的消息"。
        const unreadCount = treeNormalizeInt(node.pending_notice_count, 0);
        const unreadBadge = unreadCount > 0
            ? `<span class="execution-tree-unread" title="\u672a\u8bfb\u6d88\u606f ${unreadCount} \u6761">${unreadCount}</span>`
            : "";
        button.innerHTML = `<span class="execution-tree-node-head"><span class="execution-tree-node-title">${esc(title)}</span>${unreadBadge}${expandAction}${acceptanceBadge}<span class="status-badge" data-status="${esc(node.visual_state || node.state || "")}">${esc(displayState)}</span></span>${pauseAction}`;
        button.addEventListener("click", (event) => {
            if (event.target instanceof Element && event.target.closest("[data-node-pause-action]")) return;
            if (event.target instanceof Element && event.target.closest("[data-node-expand-action]")) return;
            handleTreeNodeClick(node, event);
        });
        const expandEl = button.querySelector("[data-node-expand-action]");
        expandEl?.addEventListener("click", (event) => {
            event.stopPropagation();
            toggleTreeNodeExpanded(node.node_id);
            renderTree();
        });
        expandEl?.addEventListener("keydown", (event) => {
            if (event.key !== "Enter" && event.key !== " ") return;
            event.preventDefault();
            event.stopPropagation();
            toggleTreeNodeExpanded(node.node_id);
            renderTree();
        });
        const pauseEl = button.querySelector("[data-node-pause-action]");
        pauseEl?.addEventListener("click", (event) => handleTreeNodePauseAction(node, event));
        pauseEl?.addEventListener("keydown", (event) => {
            if (event.key === "Enter" || event.key === " ") handleTreeNodePauseAction(node, event);
        });
        return button;
    };
    const walk = (node) => {
        const nodeStatus = String(node.visual_state || node.state || "").trim().toLowerCase();
        const inspectionNodes = Array.isArray(node.inspectionNodes) ? node.inspectionNodes : [];
        const visibleChildren = Array.isArray(node.children) ? node.children : [];
        const item = document.createElement("li");
        item.className = "execution-tree-item";
        item.dataset.status = nodeStatus;
        const stack = document.createElement("div");
        stack.className = "execution-tree-node-stack";
        if (inspectionNodes.length) stack.classList.add("has-inspection");
        inspectionNodes.forEach((inspectionNode) => {
            stack.appendChild(createTreeNodeButton(inspectionNode, { inspectionBlock: true }));
        });
        stack.appendChild(createTreeNodeButton(node, { mergedBase: inspectionNodes.length > 0 }));
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
    mountTerminalFilterToggle();
    U.tree.appendChild(wrapper);
    if (typeof enhanceResourceSelects === "function") enhanceResourceSelects();
    if (typeof restoreOpenResourceSelect === "function") restoreOpenResourceSelect(U.tree);
    maybeReapplyTaskTreeLocateHighlight();
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

// "展开全部"开关常驻任务详情头部（活跃节点右侧），状态由 S.treeShowTerminal 表达：
// null/undefined = 跟随任务状态自动推导，true/false = 用户手动指定。
function mountTerminalFilterToggle() {
    const input = U.taskExpandAllInput;
    if (!(input instanceof HTMLInputElement)) return;
    if (input.dataset.bound !== "true") {
        input.dataset.bound = "true";
        input.addEventListener("change", () => {
            S.treeShowTerminal = !!input.checked;
            renderTree();
        });
    }
    input.checked = treeShowTerminalEffective();
}
