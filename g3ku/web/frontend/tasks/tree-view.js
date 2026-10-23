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
        !!S.treeShowTerminal,
        true,
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
        button.innerHTML = `<span class="execution-tree-node-head"><span class="execution-tree-node-title">${esc(title)}</span>${acceptanceBadge}<span class="status-badge" data-status="${esc(node.visual_state || node.state || "")}">${esc(displayState)}</span></span>${pauseAction}`;
        button.addEventListener("click", (event) => {
            if (event.target instanceof Element && event.target.closest("[data-node-pause-action]")) return;
            handleTreeNodeClick(node, event);
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
    U.tree.appendChild(buildTerminalFilterToggle());
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

function buildTerminalFilterToggle() {
    const bar = document.createElement("div");
    bar.className = "execution-tree-toolbar";
    const label = document.createElement("label");
    label.className = "execution-tree-filter";
    const input = document.createElement("input");
    input.type = "checkbox";
    input.checked = !!S.treeShowTerminal;
    input.addEventListener("change", () => {
        S.treeShowTerminal = !!input.checked;
        renderTree();
    });
    label.appendChild(input);
    label.appendChild(document.createTextNode(" 显示已结束节点"));
    bar.appendChild(label);
    return bar;
}
