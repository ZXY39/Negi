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
    const distributionState = activeTaskDistributionState();
    S.treeView = buildExecutionTreeFromSnapshot(S.treeRootNodeId, S.treeSelectedRoundByNodeId);
    syncTaskTreeHeaderState(S.treeView);
    if (!S.treeView) {
        // 根节点缺失通常是增量合并损坏了缓存：空态之外再触发一次整表自愈重载。
        scheduleTaskTreeSnapshotSelfHeal();
        U.tree.innerHTML = "";
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
    if (distributionState?.ui_mode === "distribution" || distributionState?.ui_mode === "distribution_failed") {
        wrapper.classList.add("execution-tree--distribution-active");
    }
    // 受影响（被冻结子树）节点集合：仅用于连线按子树范围染色，
    // 分发完成/通知消费后该集合随 distribution UI 退出而失效。
    const distributionAffectedIds = distributionAffectedNodeIds(distributionState);
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
        const taskPaused = taskPauseDisplayActive();
        const effectivePaused = effectiveTreeNodePaused(node, taskPaused);
        const displayState = taskNodeDisplayState(node, taskPaused);
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
        button.dataset.paused = effectivePaused ? "true" : "false";
        button.dataset.pausedReason = String(node.pause_reason || "");
        button.title = node.pause_remark ? `${fullTitle} \u2014 ${node.pause_remark}` : fullTitle;
        button.setAttribute("aria-pressed", S.selectedNodeId === node.node_id ? "true" : "false");
        const pauseAction = !isTerminalTreeNodeStatus(node.state) && !taskPaused
            ? `<span class="execution-tree-node-pause-action" data-node-pause-action="true" role="button" tabindex="0" title="${node.is_paused ? "\u6062\u590d\u8282\u70b9" : "\u6682\u505c\u8282\u70b9"}">${node.is_paused ? "\u25b6" : "\u2161"}</span>`
            : "";
        button.innerHTML = `${showStaticSubtreeHint ? '<span class="execution-tree-node-note">\u6682\u65e0\u5176\u4ed6\u53ef\u5207\u6362\u5b50\u6811</span>' : ""}<span class="execution-tree-node-head"><span class="execution-tree-node-title">${esc(title)}</span><span class="status-badge" data-status="${esc(node.visual_state || node.state || "")}">${esc(displayState)}</span></span>${pauseAction}`;
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
        const fullTitle = String(node.fullTitle || node.title || node.node_id || "");
        const nodeStatus = String(node.visual_state || node.state || "").trim().toLowerCase();
        const roundOptions = Array.isArray(node.roundOptions) ? node.roundOptions : [];
        const inspectionNodes = Array.isArray(node.inspectionNodes) ? node.inspectionNodes : [];
        const visibleChildren = Array.isArray(node.children) ? node.children : [];
        const hasSwitchableSubtrees = roundOptions.length > 1;
        const showStaticSubtreeHint = !hasSwitchableSubtrees && visibleChildren.length > 0;
        const nodeAffected = distributionAffectedIds.has(String(node.node_id || "").trim());
        const item = document.createElement("li");
        item.className = "execution-tree-item";
        item.dataset.status = nodeStatus;
        if (nodeAffected) item.classList.add("execution-tree-item--distribution-affected");
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
            select.id = treeRoundSelectId(node.node_id);
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
            // 父节点受影响时，其子列表（父→子连线段）才允许染分发色；
            // 子树根节点与未受影响父节点之间的边界连线保持原状态色。
            if (nodeAffected) branch.classList.add("execution-tree-list--distribution-affected");
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
    if (distributionState) U.tree.appendChild(buildTaskTreeDistributionBubble());
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
