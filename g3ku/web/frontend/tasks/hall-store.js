// 任务大厅状态层：任务列表条目补丁、删除、终态对账与详情事件分发。
// 与视图共用全局 S/U；视图函数（renderTasks、renderTree 等）在调用时解析。

function scheduleTaskListReconcileSoon(delayMs = 3000) {
    // 终态清理与对账在服务端异步执行：终态转换补丁携带的大小可能落后，
    // 延迟一次全量 loadTasks 拿到对账后的最终占用值。
    if (S.taskTerminalReconcileId) window.clearTimeout(S.taskTerminalReconcileId);
    S.taskTerminalReconcileId = window.setTimeout(() => {
        S.taskTerminalReconcileId = null;
        if (taskListViewVisible()) void loadTasks();
    }, Math.max(500, Number(delayMs) || 3000));
}

function patchTaskCardElement(taskId) {
    const key = String(taskId || "").trim();
    if (!key) return false;
    const task = S.tasksById?.[key];
    if (!task) return false;
    const card = U.taskGrid?.querySelector?.(`.project-card[data-task-id="${CSS.escape(key)}"]`);
    if (!(card instanceof HTMLElement)) return false;
    const titleEl = card.querySelector("[data-task-title]");
    if (titleEl) {
        const title = String(task.title || key);
        titleEl.textContent = title;
        titleEl.setAttribute("title", title);
    }
    const diskUsageEl = card.querySelector("[data-task-disk-usage]");
    if (diskUsageEl) {
        diskUsageEl.textContent = formatTaskBytes(task?.disk_usage_bytes);
    }
    // 总耗时随增量补丁刷新：运行中卡片走该路径避免整网格重建（防滚动清零），
    // 只有在这里同步数字，运行中任务的耗时才会逐分钟推进；暂停/终态卡片
    // taskElapsedText 返回定格值，重复写入也恒定。
    const elapsedEl = card.querySelector("[data-task-elapsed]");
    if (elapsedEl) {
        elapsedEl.textContent = taskElapsedText(task);
    }
    const tokenUsage = taskTokenDisplayUsage(task);
    const previousMetrics = S.taskMetricSnapshot?.[key] || null;
    const nextMetrics = taskMetricSnapshotValue(task);
    ["input_tokens", "output_tokens", "cache_hit_tokens"].forEach((metricKey) => {
        const valueEl = card.querySelector(`[data-task-metric-value="${metricKey}"]`);
        if (!valueEl) return;
        const metricValue = tokenUsage.tracked ? Number(tokenUsage[metricKey] || 0) : null;
        const previousValue = previousMetrics && Number.isFinite(previousMetrics[metricKey]) ? previousMetrics[metricKey] : null;
        const isIncreasing = previousValue !== null && Number.isFinite(metricValue) && metricValue > previousValue;
        // 逐位重写会把数字换成新的 span，动画天生重播，不再需要摘 class 后强制重排。
        valueEl.innerHTML = taskMetricValueMarkup(
            Number.isFinite(metricValue) ? formatTokenCount(metricValue) : "--",
            isIncreasing ? formatTokenCount(previousValue) : "",
        );
    });
    S.taskMetricSnapshot = {
        ...(S.taskMetricSnapshot || {}),
        [key]: nextMetrics,
    };
    const nextStats = { ...(S.taskHallStats || {}) };
    nextStats.task_hall_card_patch_count = Number(nextStats.task_hall_card_patch_count || 0) + 1;
    S.taskHallStats = nextStats;
    trackTaskCardPatchQueueAge(key);
    const nextQueued = { ...(S.taskCardPatchQueuedAt || {}) };
    delete nextQueued[key];
    S.taskCardPatchQueuedAt = nextQueued;
    return true;
}

async function performTaskBatchAction(action, eligible) {
    if (taskActionRequiresWorker(action) && !taskWorkerControlsAvailable()) {
        showToast({ title: taskActionFailureTitle(action), text: taskWorkerNoticeText(), kind: "warn" });
        void refreshTaskWorkerStatus({ render: S.view === "tasks" });
        return;
    }
    S.taskBusy = true;
    renderTasksIfVisible();
    try {
        let succeeded = [];
        let failed = [];
        if (action === "delete") {
            const payload = await ApiClient.bulkDeleteTasks(
                eligible.map((task) => task.task_id),
                taskActionRequestOptions(action),
            );
            const items = Array.isArray(payload?.items) ? payload.items : [];
            const itemById = new Map(
                items
                    .map((item) => {
                        const taskId = String(item?.task_id || "").trim();
                        return taskId ? [taskId, item] : null;
                    })
                    .filter(Boolean)
            );
            succeeded = eligible
                .map((task) => {
                    const item = itemById.get(task.task_id);
                    return String(item?.result || "").trim().toLowerCase() === "deleted" ? task.task_id : "";
                })
                .filter(Boolean);
            failed = eligible
                .map((task) => {
                    const item = itemById.get(task.task_id);
                    if (String(item?.result || "").trim().toLowerCase() === "deleted") return null;
                    const error = new Error(String(item?.error || item?.result || "delete_failed").trim() || "delete_failed");
                    return { taskId: task.task_id, error };
                })
                .filter(Boolean);
        } else {
            const results = await settleTaskActionBatch(
                eligible,
                (task) => requestTaskAction(task.task_id, action),
                { concurrency: taskBatchActionConcurrency(action) },
            );
            succeeded = results
                .map((result, index) => (result.status === "fulfilled" ? eligible[index].task_id : ""))
                .filter(Boolean);
            failed = results
                .map((result, index) => (result.status === "rejected" ? { taskId: eligible[index].task_id, error: result.reason } : null))
                .filter(Boolean);
        }
        await loadTasks();
        if (action === "delete") {
            handleDeletedTasks(succeeded);
        } else if (S.currentTaskId && succeeded.includes(S.currentTaskId)) {
            await loadTaskDetail(S.currentTaskId, { preserveView: true, reopenSocket: false });
            await loadTaskArtifacts();
        }
        if (action === "pause") {
            // 与单卡暂停一致：每个成功请求暂停的任务在卡片内跟踪真实排水，
            // 成功后由提示框显示「暂停成功」，这里不再提前弹顶部 toast。
            succeeded.forEach((taskId) => beginTaskPauseHint(taskId));
        }
        if (action === "resume") {
            succeeded.forEach((taskId) => clearTaskPauseHint(taskId));
        }
        if (failed.length && !succeeded.length) {
            if (failed.some((item) => Number(item?.error?.status || 0) === 503)) {
                void refreshTaskWorkerStatus({ render: S.view === "tasks" });
            }
            showToast({
                title: taskActionFailureTitle(action),
                text: taskActionErrorText(action, failed[0].error),
                kind: "error",
            });
            return;
        }
        if (failed.length) {
            showToast({
                title: action === "delete" ? "删除完成" : `${taskActionText(action)}完成`,
                text: `${succeeded.length} 个任务成功，${failed.length} 个失败`,
                kind: "warn",
            });
            return;
        }
        const successText = action === "delete"
            ? `已删除 ${succeeded.length} 个任务`
            : `${succeeded.length} 个任务已${taskActionText(action)}`;
        if (action !== "pause") {
            // 批量暂停的成功反馈由各卡片内的「暂停中→暂停成功」提示承担。
            showToast({
                title: taskActionSuccessTitle(action),
                text: successText,
                kind: "success",
            });
        }
        if (failed.some((item) => Number(item?.error?.status || 0) === 503)) {
            void refreshTaskWorkerStatus({ render: S.view === "tasks" });
        }
    } finally {
        S.taskBusy = false;
        renderTasksIfVisible();
    }
}

function handleTaskEvent(payload) {
    if (payload.type === "task.summary.patch" && payload.data?.task) {
        S.currentTask = { ...(S.currentTask || {}), ...payload.data.task };
        S.taskSummary = { ...(S.taskSummary || {}), ...payload.data.task };
        patchTaskListItem(payload.data.task);
        renderTaskDetailHeader();
        renderTaskTokenStats();
        if (String(S.treeRootNodeId || "").trim() && typeof renderTree === "function") {
            renderTree();
            const selectedNodeId = String(S.selectedNodeId || "").trim();
            const selectedNode = selectedNodeId && typeof findTreeNode === "function"
                ? findTreeNode(S.treeView, selectedNodeId)
                : null;
            if (
                selectedNode
                && String(S.currentNodeDetail?.node_id || "").trim() === selectedNodeId
                && typeof renderTaskNodeDetailStatus === "function"
            ) {
                renderTaskNodeDetailStatus({ ...selectedNode, ...S.currentNodeDetail });
            }
        }
        return;
    }
    if (payload.type === "task.model.call") {
        // 历史快照态不收实时行：页号按锚点算，插进窗口会让已加载的行整体往后漂。
        if (isTaskModelCallHistorical()) return;
        const nextCall = normalizeTaskModelCall(payload.data || {});
        S.recentModelCalls = mergeTaskModelCallRows(S.recentModelCalls, [nextCall]);
        renderTaskTokenStats();
        return;
    }
    if (payload.type === "task.live.patch") {
        const runtimeSummary = payload.data?.runtime_summary || {};
        const isDelta = runtimeSummary.frames_partial === true;
        const deltaFrame = payload.data?.frame && typeof payload.data.frame === "object" ? payload.data.frame : null;
        const activeNodeIds = Array.isArray(runtimeSummary?.active_node_ids) ? runtimeSummary.active_node_ids : [];
        const runnableNodeIds = Array.isArray(runtimeSummary?.runnable_node_ids) ? runtimeSummary.runnable_node_ids : [];
        const waitingNodeIds = Array.isArray(runtimeSummary?.waiting_node_ids) ? runtimeSummary.waiting_node_ids : [];
        const hasTreeContext = !!String(S.treeRootNodeId || "").trim();
        if (isDelta && !Object.keys(S.liveFrameMap || {}).length) {
            // 手里还没有帧表（详情没打开过、或断线窗口里错过了全量）：增量并不出正确状态，
            // 直接重拉一次详情，让全量摘要补齐。
            const currentTaskId = String(S.currentTaskId || "").trim();
            if (currentTaskId) void reconcileTaskDetailAfterWsReconnect(currentTaskId);
            return;
        }
        const frames = isDelta ? null : (Array.isArray(runtimeSummary?.frames) ? runtimeSummary.frames : []);
        const frameList = mergeTaskLiveFrameDelta({
            frames,
            frame: deltaFrame,
            staleNodeIds: runtimeSummary.stale_node_ids,
        });
        S.taskRuntimeSummary = runtimeSummary || null;
        const selectedNodeId = String(S.selectedNodeId || "").trim();
        if (selectedNodeId && typeof renderTaskNodeModelRetryToast === "function") {
            const matchingFrames = frameList.filter((item) => (
                String(item?.node_id || "").trim() === selectedNodeId
            ));
            const selectedFrame = matchingFrames[matchingFrames.length - 1] || null;
            renderTaskNodeModelRetryToast({
                model_retry_status: selectedFrame?.model_retry_status || null,
            });
        }
        S.taskSummary = {
            ...(S.taskSummary || {}),
            active_node_count: hasTreeContext
                ? normalizeInt(S.taskSummary?.active_node_count, 0)
                : activeNodeIds.length,
            runnable_node_count: runnableNodeIds.length,
            waiting_node_count: waitingNodeIds.length,
        };
        if (String(S.treeRootNodeId || "").trim()) {
            const candidateNodeIds = [...new Set([...activeNodeIds, ...runnableNodeIds, ...waitingNodeIds]
                .map((item) => String(item || "").trim())
                .filter(Boolean))];
            candidateNodeIds
                .filter((item) => !treeSnapshotNode(item))
                .slice(0, 3)
                .forEach((item) => { void reconcileTaskTreeForNode(item); });
            if (typeof scheduleRenderedTreeNodeStatusRefresh === "function") scheduleRenderedTreeNodeStatusRefresh();
        }
        return;
    }
    if (payload.type === "task.node.patch") {
        const nextNode = payload.data?.node && typeof payload.data.node === "object" ? payload.data.node : {};
        const nodeId = String(nextNode?.node_id || "").trim();
        if (nodeId) {
            S.taskNodePatchSummaries = {
                ...(S.taskNodePatchSummaries || {}),
                [nodeId]: { ...nextNode },
            };
            const parentNodeId = String(nextNode?.parent_node_id || "").trim();
            const rootNodeId = String(S.rootNode?.node_id || "").trim();
            if (rootNodeId && rootNodeId === nodeId) {
                S.rootNode = { ...(S.rootNode || {}), ...nextNode };
            }
            if (S.taskNodeDetails[nodeId]) {
                S.taskNodeDetails = {
                    ...(S.taskNodeDetails || {}),
                    [nodeId]: { ...(S.taskNodeDetails[nodeId] || {}), ...nextNode },
                };
            }
            if (String(S.treeRootNodeId || "").trim()) {
                const existingTreeNode = treeSnapshotNode(nodeId);
                if (existingTreeNode) {
                    const previousFingerprint = String(existingTreeNode?.children_fingerprint || "").trim();
                    S.treeNodesById = {
                        ...(S.treeNodesById || {}),
                        [nodeId]: normalizeTaskTreeSnapshotNode({
                            ...existingTreeNode,
                            ...nextNode,
                        }, existingTreeNode),
                    };
                    if (typeof refreshRenderedTreeNodeStatus === "function") refreshRenderedTreeNodeStatus(nodeId);
                    if (previousFingerprint !== String(nextNode?.children_fingerprint || "").trim()) {
                        scheduleTaskTreeBranchSync(nodeId);
                    }
                    if (String(S.treeRootNodeId || "").trim() === nodeId && Number(existingTreeNode?.pending_notice_count || 0) > 0) {
                        scheduleTaskTreeBranchSync(nodeId, { delayMs: 0 });
                    }
                } else if (parentNodeId) {
                    scheduleTaskTreeBranchSync(parentNodeId);
                }
            }
            if (String(S.selectedNodeId || "") === nodeId) {
                const currentViewState = captureTaskDetailViewState();
                stashTaskDetailViewState({ nodeId, viewState: currentViewState });
                S.pendingTaskDetailRestore = { nodeId, viewState: currentViewState };
                const selected = findTreeNode(S.treeView, nodeId) || { node_id: nodeId, title: nodeId, state: "in_progress" };
                void showAgent(selected, { preserveViewState: true, forceRefresh: true });
            } else {
                const nextDetails = { ...(S.taskNodeDetails || {}) };
                delete nextDetails[nodeId];
                S.taskNodeDetails = nextDetails;
            }
        }
        return;
    }
    if (payload.type === "task.artifact.added" || payload.type === "task.artifact.applied" || payload.type === "artifact.applied") {
        void loadTaskArtifacts();
        return;
    }
    if (payload.type === "task.deleted") {
        removeTaskListItem(payload.data?.task_id || payload.task_id || "");
        return;
    }
    if (payload.type === "task.terminal" && payload.data?.task) {
        S.currentTask = { ...(S.currentTask || {}), ...payload.data.task };
        S.taskSummary = { ...(S.taskSummary || {}), ...payload.data.task };
        renderTaskTokenStats();
        const selectedNodeId = String(S.selectedNodeId || "").trim();
        if (selectedNodeId && typeof renderTaskNodeModelRetryToast === "function") {
            renderTaskNodeModelRetryToast(null);
        }
        if (selectedNodeId) {
            const currentViewState = captureTaskDetailViewState();
            stashTaskDetailViewState({ nodeId: selectedNodeId, viewState: currentViewState });
            S.pendingTaskDetailRestore = { nodeId: selectedNodeId, viewState: currentViewState };
            const selected = findTreeNode(S.treeView, selectedNodeId) || {
                node_id: selectedNodeId,
                title: selectedNodeId,
                state: String(payload.data?.task?.status || S.currentTask?.status || ""),
            };
            void showAgent(selected, { preserveViewState: true, forceRefresh: true });
        }
    }
}

function patchTaskListItem(task) {
    const change = upsertTaskState(task);
    const taskId = String(change?.taskId || "").trim();
    if (!taskId) return;
    S.lastTaskSummaryPatchAt = new Date().toISOString();
    S.taskMetricAnimationTaskIds = new Set([taskId]);
    syncTaskSelection();
    if (!taskListViewVisible()) {
        noteTaskHallHiddenDefer();
        return;
    }
    const visibleIds = new Set(Array.isArray(S.visibleTaskIds) ? S.visibleTaskIds : []);
    if (change.added || change.orderChanged || !visibleIds.has(taskId) || !taskCardPatchEligible(change.previousTask, change.nextTask)) {
        renderTasks();
        return;
    }
    queueTaskCardPatch(taskId);
}

function removeTaskListItem(taskId) {
    const key = String(taskId || "").trim();
    if (!key) return;
    if (!removeTaskState(key)) return;
    handleDeletedTasks([key]);
    syncTaskSelection();
    if (!taskListViewVisible()) {
        noteTaskHallHiddenDefer();
        return;
    }
    renderTasks();
}
