// 任务大厅视图：网格渲染与 token 读数区。状态由 tasks/hall-store.js 维护。

function renderTasks() {
    // 重建前记录滚动位置：本网格自身就是滚动容器（overflow-y:auto），
    // innerHTML 清空会把 scrollTop 归零，重建后需还原，避免用户被拽回顶部。
    const prevScrollTop = Number(U.taskGrid?.scrollTop || 0);
    const prevPage = S.taskPage;
    const meta = paginateResources(orderedTasks(S.tasks), S.taskPage, S.taskPageSize);
    S.taskPage = meta.currentPage;
    S.visibleTaskIds = meta.items.map((task) => String(task?.task_id || "").trim()).filter(Boolean);
    syncTaskPagination(meta);
    renderTaskPerformanceBar();
    const signature = taskGridRenderSignature(meta);
    if (signature === S.taskGridSignature) {
        S.taskMetricAnimationTaskIds?.clear?.();
        updateTaskToolbar();
        return;
    }
    S.taskGridSignature = signature;
    S.taskHallStats = {
        ...(S.taskHallStats || {}),
        task_hall_full_render_count: Number(S.taskHallStats?.task_hall_full_render_count || 0) + 1,
    };
    U.taskGrid.innerHTML = "";
    const metricAnimationTaskIds = S.taskMetricAnimationTaskIds || new Set();
    const workerState = normalizeTaskWorkerState(S.tasksWorkerState);
    const workerNotice = taskWorkerNoticeText(workerState);
    if (workerNotice) {
        const warning = document.createElement("div");
        warning.className = `empty-state${workerState === "stale" || workerState === "starting" ? "" : " error"}`;
        warning.style.gridColumn = "1/-1";
        warning.textContent = workerNotice;
        U.taskGrid.appendChild(warning);
    }
    // 磁盘治理（P1）：紧急态全局横幅（数据来自 worker-status 快照，1s 轮询刷新）。
    const diskMetrics = taskWorkerStatusMetrics();
    if (diskMetrics?.disk_emergency_active) {
        const diskWarning = document.createElement("div");
        diskWarning.className = "empty-state error";
        diskWarning.style.gridColumn = "1/-1";
        const sinceText = String(diskMetrics?.disk_emergency_since || "").trim();
        diskWarning.textContent = `🚨 磁盘空间不足：运行中任务已自动暂停，新工具调用进入排队等待。请清理磁盘，空间恢复后手动恢复任务。${sinceText ? `（紧急态自 ${sinceText} 起）` : ""}`;
        U.taskGrid.appendChild(diskWarning);
    }
    if (!meta.total) {
        if (workerNotice) {
            const empty = document.createElement("div");
            empty.className = "empty-state";
            empty.style.gridColumn = "1/-1";
            empty.textContent = taskSessionEmptyText();
            U.taskGrid.appendChild(empty);
        } else {
            U.taskGrid.innerHTML = `<div class="empty-state" style="grid-column: 1/-1;">${esc(taskSessionEmptyText())}</div>`;
        }
        return updateTaskToolbar();
    }
    const nextTaskMetricSnapshot = {};
    meta.items.forEach((task) => {
        const taskId = String(task?.task_id || "");
        const selected = S.selectedTaskIds.has(taskId);
        const statusKey = taskStatusKey(task);
        const tokenUsage = taskTokenDisplayUsage(task);
        const previousMetrics = S.taskMetricSnapshot?.[taskId] || null;
        const metricItems = [
            { key: "input_tokens", label: "输入", value: tokenUsage.tracked ? tokenUsage.input_tokens : null },
            { key: "output_tokens", label: "输出", value: tokenUsage.tracked ? tokenUsage.output_tokens : null },
            { key: "cache_hit_tokens", label: "缓存", value: tokenUsage.tracked ? tokenUsage.cache_hit_tokens : null },
        ];
        nextTaskMetricSnapshot[taskId] = tokenUsage.tracked ? {
            input_tokens: Number(tokenUsage.input_tokens || 0),
            output_tokens: Number(tokenUsage.output_tokens || 0),
            cache_hit_tokens: Number(tokenUsage.cache_hit_tokens || 0),
        } : null;
        const metricsMarkup = metricItems.map((item) => {
            const hasValue = Number.isFinite(item.value);
            const previousValue = previousMetrics && Number.isFinite(previousMetrics[item.key]) ? previousMetrics[item.key] : null;
            const isIncreasing = metricAnimationTaskIds.has(taskId) && previousValue !== null && hasValue && item.value > previousValue;
            const valueMarkup = taskMetricValueMarkup(
                hasValue ? formatTokenCount(item.value) : "--",
                isIncreasing ? formatTokenCount(previousValue) : "",
            );
            return `<div class="pc-metric" data-task-metric="${item.key}"><span class="pc-metric-label">${item.label}</span><strong class="pc-metric-value" data-task-metric-value="${item.key}">${valueMarkup}</strong></div>`;
        }).join("");
        const cardActions = taskCardActions(task);
        const el = document.createElement("div");
        el.className = `project-card${selected ? " is-selected" : ""}${S.multiSelectMode ? " is-multi-mode" : ""}`;
        el.dataset.taskId = taskId;
        el.innerHTML = `
            <div class="pc-topbar">
                <div class="pc-topbar-left">
                    <label class="project-select-toggle${S.multiSelectMode ? " is-visible" : ""}"><input type="checkbox" class="project-select-checkbox ui-checkbox-input" aria-label="选择任务" ${selected ? "checked" : ""} ${S.taskBusy ? "disabled" : ""}><span class="ui-checkbox-box" aria-hidden="true"></span></label>
                    <div class="pc-topbar-meta">
                        <span class="status-badge" data-status="${esc(statusKey)}" data-task-status>${esc(taskStatusLabel(task))}</span>
                        <button class="pc-task-id-chip" type="button" title="复制任务 ID" aria-label="复制任务 ID">
                            <span class="pc-task-id-label">Task</span>
                            <span class="pc-task-id-value">${esc(taskId)}</span>
                            <i data-lucide="copy" class="pc-task-id-copy" aria-hidden="true"></i>
                        </button>
                    </div>
                </div>
                ${cardActions.length ? `
                    <div class="pc-card-menu-shell toolbar-dropdown" data-task-menu="${esc(taskId)}">
                        <button class="icon-btn pc-card-menu-trigger" type="button" data-task-menu-toggle="${esc(taskId)}" title="更多操作" aria-label="更多操作" aria-haspopup="menu" aria-expanded="false" ${S.taskBusy ? "disabled" : ""}>
                            <i data-lucide="more-horizontal"></i>
                        </button>
                        <div class="toolbar-menu pc-card-menu" role="menu" hidden>
                            ${cardActions.map((action) => `<button class="toolbar-menu-item ${action.tone}" type="button" data-task-card-action="${action.action}" role="menuitem" ${S.taskBusy ? "disabled" : ""}>${esc(action.label)}</button>`).join("")}
                        </div>
                    </div>
                ` : ""}
            </div>
            <div class="pc-header"><div class="pc-header-left"><h3 class="pc-title" data-task-title title="${esc(task.title || taskId)}">${esc(task.title || taskId)}</h3></div></div>
            <div class="pc-created-at"><span class="pc-field-label">开始</span><span class="pc-field-value">${esc(taskCreatedAtText(task))}</span>${taskIsTerminal(task) ? `<span class="pc-field-label pc-field-label--end">结束</span><span class="pc-field-value" data-task-finished-at>${esc(taskFinishedAtText(task))}</span>` : ""}</div>
            <div class="pc-created-at"><span class="pc-field-label">占用大小</span><span class="pc-field-value" data-task-disk-usage>${esc(formatTaskBytes(task?.disk_usage_bytes))}</span><span class="pc-field-label pc-field-label--end">节点数</span><span class="pc-field-value" data-task-node-count>${esc(formatTaskNodeCount(task?.node_count))}</span><span class="pc-field-value pc-elapsed" data-task-elapsed title="总耗时">${esc(taskElapsedText(task))}</span></div>
            <div class="pc-metrics">${metricsMarkup}</div>
            ${taskPauseHintMarkup(taskId)}
        `;
        const toggle = el.querySelector(".project-select-toggle");
        const checkbox = el.querySelector(".project-select-checkbox");
        toggle?.addEventListener("click", (e) => e.stopPropagation());
        checkbox?.addEventListener("change", (e) => {
            e.stopPropagation();
            if (e.target.checked) S.selectedTaskIds.add(taskId);
            else S.selectedTaskIds.delete(taskId);
            renderTasks();
        });
        el.querySelector(".pc-task-id-chip")?.addEventListener("click", async (e) => {
            e.stopPropagation();
            await copyTaskId(taskId);
        });
        const menuTrigger = el.querySelector("[data-task-menu-toggle]");
        menuTrigger?.addEventListener("click", (e) => {
            e.stopPropagation();
            const shell = menuTrigger.closest(".pc-card-menu-shell");
            const isOpen = !!shell?.classList.contains("is-open");
            setTaskCardMenuOpen(taskId, !isOpen, { restoreFocus: isOpen });
        });
        el.querySelectorAll("[data-task-card-action]").forEach((btn) => btn.addEventListener("click", async (e) => {
            e.stopPropagation();
            closeTaskCardMenus();
            await runTaskAction(taskId, btn.dataset.taskCardAction, { returnFocus: menuTrigger || btn });
        }));
        el.querySelectorAll("[data-task-pause-resume]").forEach((btn) => btn.addEventListener("click", async (e) => {
            e.stopPropagation();
            // 暂停排水期间点击恢复：立即撤提示并恢复任务（后端命令 FIFO 保证
            // resume 排在未处理的 pause 之后生效）。
            clearTaskPauseHint(taskId);
            renderTasksIfVisible();
            await performTaskAction(taskId, "resume");
        }));
        // 多选态里点整张卡就是加/减选；关掉多选后点卡照常打开详情。
        el.addEventListener("click", () => {
            if (S.multiSelectMode) {
                toggleTaskSelection(taskId);
                return;
            }
            void openTask(taskId);
        });
        U.taskGrid.appendChild(el);
    });
    S.taskMetricSnapshot = nextTaskMetricSnapshot;
    S.pendingTaskCardPatchIds = new Set([...(S.pendingTaskCardPatchIds || new Set())].filter((taskId) => !S.visibleTaskIds.includes(taskId)));
    const nextQueuedAt = { ...(S.taskCardPatchQueuedAt || {}) };
    S.visibleTaskIds.forEach((taskId) => { delete nextQueuedAt[taskId]; });
    S.taskCardPatchQueuedAt = nextQueuedAt;
    S.taskMetricAnimationTaskIds?.clear?.();
    updateTaskToolbar();
    icons();
    // 同页重建还原滚动位置；翻页（页码变化）维持从顶部开始的既有行为。
    if (U.taskGrid && meta.currentPage === prevPage) U.taskGrid.scrollTop = prevScrollTop;
}

function renderTaskTokenStats(options = {}) {
    const force = !!(options && options.force);
    if (!U.taskTokenContent) return;
    // 窗口打开期间冻结自动刷新：实时事件不重建表格，避免打断滚动位置、
    // 清空搜索框内容与筛选结果；手动更新走「刷新」按钮（force）。
    if (!force && S.taskTokenStatsOpen) return;
    const summary = taskTokenDisplayUsage(S.currentTask, null);
    // taskTokenSummaryLine 自己会换算成显示口径，这里必须喂未换算的用量：传 summary
    // 会把缓存命中再加一次。窗口内的数字只由下面那五格印一次，标题副文字是固定说明。
    if (U.taskTokenButton) U.taskTokenButton.title = taskTokenSummaryLine(taskTokenUsage(S.currentTask, null));
    if (!summary.tracked) {
        U.taskTokenContent.innerHTML = `
            <div class="task-token-topline">
                <div class="task-token-stat"><strong>未统计</strong><span>该任务创建于统计上线前，暂无精确数据。</span></div>
            </div>
        `;
        return;
    }
    const modelRows = Array.isArray(S.taskSummary?.token_usage_by_model)
        ? S.taskSummary.token_usage_by_model.map(normalizeModelTokenUsage).sort((a, b) => {
            const delta = tokenKnownTotal(b) - tokenKnownTotal(a);
            if (delta !== 0) return delta;
            return taskModelDisplayName(a).localeCompare(taskModelDisplayName(b));
        })
        : [];
    const callViewState = taskModelCallViewState();
    const recentModelCalls = callViewState.calls;
    const filteredModelCalls = callViewState.filtered;
    const modelCallPageMeta = callViewState.meta;
    const modelCallQuery = callViewState.query;
    const partialNote = summary.is_partial
        ? '<span class="task-token-badge warn">部分模型未返回 usage</span>'
        : '<span class="task-token-badge success">统计完整</span>';
    // 与逐次调用明细表的 modelCallHitRate 同口径：缓存命中 / 整份 prompt（新增输入 + 缓存命中）。
    const cacheHitRate = summary.effective_input_tokens
        ? (summary.cache_hit_tokens / summary.effective_input_tokens) * 100
        : 0;
    const topline = `
        <div class="task-token-topline">
            <div class="task-token-stat"><strong>${esc(formatTokenCount(summary.input_tokens))}</strong><span>总输入</span></div>
            <div class="task-token-stat"><strong>${esc(formatTokenCount(summary.output_tokens))}</strong><span>总输出</span></div>
            <div class="task-token-stat"><strong>${esc(formatTokenCount(summary.cache_hit_tokens))}</strong><span>缓存命中</span></div>
            <div class="task-token-stat"><strong>${cacheHitRate.toFixed(1)}%</strong><span>缓存命中率</span></div>
            <div class="task-token-stat"><strong>${esc(formatTokenCount(summary.call_count))}</strong><span>模型调用</span></div>
        </div>
        <div class="task-token-meta">
            ${partialNote}
        </div>
    `;
    if (!summary.call_count) {
        U.taskTokenContent.innerHTML = `${topline}<div class="empty-state task-token-empty">尚未发生模型调用。</div>`;
        return;
    }
    const rowsMarkup = modelRows.length
        ? modelRows.map((item) => {
            const displayTitle = taskModelDisplayName(item) || "未命名配置";
            const configKey = String(item.model_key || "").trim();
            const subtitleParts = [
                configKey && configKey !== displayTitle ? configKey : "",
                item.provider_id,
                item.provider_model,
            ].filter(Boolean);
            const displayUsage = tokenDisplayUsage(item);
            const badges = [];
            if (item.is_partial) badges.push('<span class="task-token-badge warn">部分缺失</span>');
            if (!item.calls_without_usage) badges.push('<span class="task-token-badge success">完整</span>');
            return `
                <div class="task-token-model-item">
                    <div class="task-token-model-head">
                        <div>
                            <h3>${esc(displayTitle)}</h3>
                            <p>${esc(subtitleParts.join(" · ") || "模型标识未提供")}</p>
                        </div>
                        <div class="task-token-model-badges">${badges.join("")}</div>
                    </div>
                    <div class="task-token-model-stats">
                        <span>输入 ${esc(formatTokenCount(displayUsage.input_tokens))}</span>
                        <span>输出 ${esc(formatTokenCount(displayUsage.output_tokens))}</span>
                        <span>缓存命中 ${esc(formatTokenCount(displayUsage.cache_hit_tokens))}</span>
                    </div>
                    <div class="task-token-model-meta">
                        调用 ${esc(formatTokenCount(item.call_count))} · 有 usage ${esc(formatTokenCount(item.calls_with_usage))} · 缺失 ${esc(formatTokenCount(item.calls_without_usage))}
                    </div>
                </div>
            `;
        }).join("")
        : '<div class="empty-state task-token-empty">当前只有任务级统计，尚无按模型明细。</div>';
    // 按模型明细现在由窗口自己取（任务详情快照不再带）。没落到本任务之前说"正在取数"，
    // 不能套用上面那句——那是在宣称服务端没有这份数据，而它只是在飞。
    const modelRowsMarkup = String(S.taskTokenLedgerTaskId || "").trim()
        === String(S.currentTaskId || "").trim()
        ? rowsMarkup
        : '<div class="empty-state task-token-empty">按模型明细正在取数…</div>';
    // 快照态即使取回空页也要保留卡片：底栏的「回到最新」是唯一出口。
    const recentCallMarkup = (recentModelCalls.length || modelCallPageMeta.historical)
        ? `
            <div class="task-token-call-card">
                <div class="task-token-call-head">
                    <div class="task-token-call-head-title">
                        <h3>模型调用明细</h3>
                        <p>${esc(taskModelCallLedgerNote(
                            modelCallPageMeta.historical ? modelCallPageMeta.grandTotal : recentModelCalls.length,
                            modelCallPageMeta.historical ? modelCallPageMeta.grandTotal : S.taskSummary?.total_model_calls,
                            modelCallPageMeta.pageSize,
                        ))}</p>
                    </div>
                    <div class="task-token-call-tools">
                        <input type="search" class="task-token-call-search" data-task-model-call-search
                            placeholder="${modelCallPageMeta.historical ? "搜索本页" : "搜索序号 / 节点 ID / 类型 / 模型名称"}"
                            aria-label="搜索模型调用明细"
                            value="${esc(modelCallQuery)}">
                        <button class="toolbar-btn ghost" type="button" data-task-model-call-refresh title="${modelCallPageMeta.historical ? "按最新账本重算页号并重取本页" : "重新取数：总量与最近 300 条调用明细"}">刷新</button>
                    </div>
                </div>
                <div class="task-token-call-table-region" data-task-model-call-region>
                    ${renderTaskTokenCallTableMarkup(modelCallPageMeta, filteredModelCalls, modelCallQuery)}
                </div>
            </div>
        `
        : '<div class="empty-state task-token-empty">暂无逐次调用明细。</div>';
    U.taskTokenContent.innerHTML = `
        ${topline}
        <div class="task-token-model-list">${modelRowsMarkup}</div>
        ${recentCallMarkup}
    `;
}
