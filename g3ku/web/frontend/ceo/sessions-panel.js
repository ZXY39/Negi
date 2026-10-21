"use strict";

// CEO 会话列表面板：可见性/排序、未读豁免、批量动作、拖拽排序与卡片渲染。
// 只读写 S/U 与既有全局入口；本文件必须在 org_graph_app.js 之前加载。

function visibleCeoChannelGroups() {
    return (Array.isArray(S.ceoChannelGroups) ? S.ceoChannelGroups : [])
        .filter((group) => !isCeoChannelCollapsed(String(group?.channel_id || "").trim()));
}

function visibleCeoSessions() {
    if (S.ceoSessionTab === "channel") return flattenChannelGroups(S.ceoChannelGroups);
    return Array.isArray(S.ceoLocalSessions) ? S.ceoLocalSessions : [];
}

function visibleCeoBulkSelectableSessionIds() {
    // 折叠起来的渠道不在作用集里：「全选」与批量删除的条数只按看得见的行算。
    const rows = S.ceoSessionTab === "channel"
        ? flattenChannelGroups(visibleCeoChannelGroups())
        : visibleCeoSessions();
    return rows
        .map((item) => String(item?.session_id || "").trim())
        .filter(Boolean);
}

function rebuildCeoSessionIndex() {
    S.ceoSessions = [...(Array.isArray(S.ceoLocalSessions) ? S.ceoLocalSessions : []), ...flattenChannelGroups(S.ceoChannelGroups)];
}

function sortCeoSessionsByTime(items = []) {
    return [...(Array.isArray(items) ? items : [])].sort((left, right) => {
        // 手动位次优先；没有位次的（拖动之后新建的）会话仍按创建时间倒序浮在手动区之上。
        const leftRank = ceoSessionManualRank(left?.session_id);
        const rightRank = ceoSessionManualRank(right?.session_id);
        if (leftRank !== rightRank) {
            if (leftRank < 0) return -1;
            if (rightRank < 0) return 1;
            return leftRank - rightRank;
        }
        const leftTime = String(ceoSessionCreatedTime(left) || "");
        const rightTime = String(ceoSessionCreatedTime(right) || "");
        if (leftTime !== rightTime) return rightTime.localeCompare(leftTime);
        return String(right?.session_id || "").localeCompare(String(left?.session_id || ""));
    });
}

function syncCeoSessionPanelState() {
    const expanded = !!S.ceoSessionPanelExpanded;
    U.ceoShell?.classList.toggle("is-session-panel-expanded", expanded);
    U.ceoSessionPanel?.setAttribute("data-panel-state", expanded ? "expanded" : "collapsed");
    if (U.ceoSessionPanelToggle) {
        U.ceoSessionPanelToggle.setAttribute("aria-expanded", expanded ? "true" : "false");
        U.ceoSessionPanelToggle.setAttribute("aria-label", expanded ? "收起会话列表" : "展开会话列表");
        U.ceoSessionPanelToggle.innerHTML = `<i data-lucide="${expanded ? "chevrons-left" : "chevrons-right"}"></i>`;
    }
    icons();
}

function setCeoSessionPanelExpanded(expanded) {
    S.ceoSessionPanelExpanded = !!expanded;
    if (!S.ceoSessionPanelExpanded) {
        S.ceoBulkMode = false;
        clearCeoBulkSelection();
        closeCeoSessionMenus();
    }
    syncCeoSessionPanelState();
    renderCeoSessions();
    syncCeoSessionActions();
}

function setCeoSessionTab(tab) {
    const next = String(tab || "local").trim() === "channel" ? "channel" : "local";
    if (S.ceoSessionTab === next) return;
    S.ceoSessionTab = next;
    clearCeoBulkSelection();
    closeCeoSessionMenus();
    renderCeoSessions();
    syncCeoSessionActions();
}

function patchCeoSessionRuntimeState(sessionId, isRunning) {
    const key = String(sessionId || "").trim();
    if (!key || !Array.isArray(S.ceoSessions)) return false;
    const current = (S.ceoSessions || []).find((item) => String(item?.session_id || "").trim() === key) || null;
    if (!current) return false;
    const nextValue = !!isRunning;
    if (!!current.is_running === nextValue) return false;
    S.ceoLocalSessions = sortCeoSessionsByTime((S.ceoLocalSessions || []).map((item) =>
        String(item?.session_id || "").trim() === key ? { ...item, is_running: nextValue } : item
    ));
    S.ceoChannelGroups = normalizeCeoChannelGroups((S.ceoChannelGroups || []).map((group) => ({
        ...group,
        items: (group?.items || []).map((item) =>
            String(item?.session_id || "").trim() === key ? { ...item, is_running: nextValue } : item
        ),
    })));
    rebuildCeoSessionIndex();
    return true;
}

function markCeoSessionRead(sessionId, { messageCount = null } = {}) {
    const key = String(sessionId || "").trim();
    if (!key) return;
    S.ceoSessionUnread = { ...S.ceoSessionUnread, [key]: 0 };
    if (messageCount !== null && messageCount !== undefined) {
        S.ceoSessionMessageCounts = {
            ...S.ceoSessionMessageCounts,
            [key]: Math.max(0, normalizeInt(messageCount, 0)),
        };
    }
}

function armCeoSessionUnreadExemption(previousActiveId) {
    // 仅当页面已水合(存在真实的前序会话基线)才武装:冷启动时 activeSessionId() 会回退到
    // ApiClient 的兜底 id "web:shared",不加此守卫的话每次加载都会为「用户从未看过的会话」误武装。
    if (!S.ceoSessionHydrated) return;
    const key = String(previousActiveId || "").trim();
    if (!key) return;
    S.ceoSessionUnreadExempt = {
        ...(S.ceoSessionUnreadExempt || {}),
        [key]: Date.now() + CEO_SESSION_UNREAD_EXEMPT_WINDOW_MS,
    };
}

function clearCeoSessionUnreadExemption(sessionId) {
    const exempt = S.ceoSessionUnreadExempt || {};
    if (!sessionId) {
        S.ceoSessionUnreadExempt = {};
        return;
    }
    if (!Object.prototype.hasOwnProperty.call(exempt, sessionId)) return;
    const next = { ...exempt };
    delete next[sessionId];
    S.ceoSessionUnreadExempt = next;
}

function syncCeoSessionUnreadState(sessions = [], activeId = activeSessionId()) {
    const previousCounts = S.ceoSessionMessageCounts && typeof S.ceoSessionMessageCounts === "object"
        ? S.ceoSessionMessageCounts
        : {};
    const previousUnread = S.ceoSessionUnread && typeof S.ceoSessionUnread === "object"
        ? S.ceoSessionUnread
        : {};
    const nextCounts = {};
    const nextUnread = {};
    const hydrated = !!S.ceoSessionHydrated;

    if (S.ceoSessionUnreadExempt && Object.keys(S.ceoSessionUnreadExempt).some((key) => S.ceoSessionUnreadExempt[key] <= Date.now())) {
        const next = { ...S.ceoSessionUnreadExempt };
        Object.keys(next).forEach((key) => { if (next[key] <= Date.now()) delete next[key]; });
        S.ceoSessionUnreadExempt = next;
    }

    (Array.isArray(sessions) ? sessions : []).forEach((item) => {
        const sessionId = String(item?.session_id || "").trim();
        if (!sessionId) return;
        const messageCount = sessionMessageCount(item);
        nextCounts[sessionId] = messageCount;

        if (sessionId === activeId) {
            nextUnread[sessionId] = 0;
            return;
        }

        const previousCount = Math.max(0, normalizeInt(previousCounts[sessionId], messageCount));
        const existingUnread = Math.max(0, normalizeInt(previousUnread[sessionId], 0));

        if (!hydrated || !Object.prototype.hasOwnProperty.call(previousCounts, sessionId)) {
            nextUnread[sessionId] = existingUnread;
            return;
        }

        if (messageCount > previousCount) {
            if (Object.prototype.hasOwnProperty.call(S.ceoSessionUnreadExempt || {}, sessionId)) {
                // 离开该会话后的第一个正增量 = 列表计数迟到补算(消息已在聊天区看过),视为已读
                clearCeoSessionUnreadExemption(sessionId);
                nextUnread[sessionId] = 0;
                return;
            }
            nextUnread[sessionId] = existingUnread + (messageCount - previousCount);
            return;
        }

        if (messageCount < previousCount) {
            nextUnread[sessionId] = 0;
            return;
        }

        nextUnread[sessionId] = existingUnread;
    });

    S.ceoSessionMessageCounts = nextCounts;
    S.ceoSessionUnread = nextUnread;
    S.ceoSessionHydrated = true;
}

function canMutateCeoSessions() {
    return !(S.ceoPauseBusy || S.ceoUploadBusy || S.ceoSessionBusy || S.ceoSessionCatalogBusy);
}

function canCreateCeoSessions() {
    return !(S.ceoPauseBusy || S.ceoUploadBusy || S.ceoSessionCatalogBusy);
}

function canActivateCeoSessions() {
    return !(S.ceoPauseBusy || S.ceoUploadBusy || S.ceoSessionCatalogBusy);
}

function closeCeoSessionMenus({ restoreFocus = false } = {}) {
    const openMenus = [...(U.ceoSessionList?.querySelectorAll(".ceo-session-actions.is-open") || [])];
    let closed = false;
    openMenus.forEach((shell) => {
        shell.classList.remove("is-open");
        shell.closest(".ceo-session-card")?.classList.remove("is-menu-open");
        shell.querySelector(".ceo-session-menu")?.setAttribute("hidden", "hidden");
        const trigger = shell.querySelector("[data-session-menu-toggle]");
        if (trigger) {
            trigger.setAttribute("aria-expanded", "false");
            if (restoreFocus && trigger instanceof HTMLElement) trigger.focus();
        }
        closed = true;
    });
    return closed;
}

function setCeoSessionMenuOpen(sessionId, open, { restoreFocus = false } = {}) {
    const targetId = String(sessionId || "").trim();
    if (!targetId) return false;
    let matched = false;
    [...(U.ceoSessionList?.querySelectorAll(".ceo-session-actions[data-session-menu]") || [])].forEach((shell) => {
        const currentId = String(shell.dataset.sessionMenu || "").trim();
        const shouldOpen = !!open && currentId === targetId;
        const trigger = shell.querySelector("[data-session-menu-toggle]");
        const menu = shell.querySelector(".ceo-session-menu");
        if (currentId === targetId) matched = true;
        shell.classList.toggle("is-open", shouldOpen);
        shell.closest(".ceo-session-card")?.classList.toggle("is-menu-open", shouldOpen);
        if (menu) {
            if (shouldOpen) menu.removeAttribute("hidden");
            else menu.setAttribute("hidden", "hidden");
        }
        if (trigger) {
            trigger.setAttribute("aria-expanded", shouldOpen ? "true" : "false");
            if (!shouldOpen && restoreFocus && currentId === targetId && trigger instanceof HTMLElement) trigger.focus();
        }
    });
    return matched;
}

function syncCeoSessionActions() {
    const mutationDisabled = !canMutateCeoSessions();
    const creationDisabled = !canCreateCeoSessions();
    const bulkSelectionDisabled = !canSelectCeoBulkSessions();
    const activationDisabled = !canActivateCeoSessions();
    const bulkIds = visibleCeoBulkSelectableSessionIds();
    const allSelected = bulkIds.length > 0 && bulkIds.every((sessionId) => isCeoBulkSessionSelected(sessionId));
    if (U.ceoNewSession) U.ceoNewSession.disabled = creationDisabled;
    if (U.ceoSessionBulkToggle) {
        U.ceoSessionBulkToggle.hidden = !S.ceoSessionPanelExpanded;
        U.ceoSessionBulkToggle.disabled = bulkSelectionDisabled && !S.ceoBulkMode;
        U.ceoSessionBulkToggle.textContent = S.ceoBulkMode ? "取消" : "多选";
        U.ceoSessionBulkToggle.setAttribute("aria-pressed", S.ceoBulkMode ? "true" : "false");
    }
    if (U.ceoSessionBulkActions) U.ceoSessionBulkActions.hidden = !(S.ceoSessionPanelExpanded && S.ceoBulkMode);
    if (U.ceoSessionBulkDelete) U.ceoSessionBulkDelete.disabled = mutationDisabled || S.ceoSelectedSessionIds.size <= 0;
    if (U.ceoSessionBulkSelectAll) {
        U.ceoSessionBulkSelectAll.disabled = bulkSelectionDisabled || bulkIds.length <= 0;
        U.ceoSessionBulkSelectAll.setAttribute("aria-pressed", allSelected ? "true" : "false");
    }
    U.ceoSessionList?.querySelectorAll("[data-session-activate]")?.forEach((button) => {
        const targetId = String(button?.dataset?.sessionActivate || "").trim();
        button.disabled = S.ceoBulkMode ? false : activationDisabled || targetId === activeSessionId();
    });
    U.ceoSessionList?.querySelectorAll("[data-session-bulk-checkbox]")?.forEach((input) => {
        input.disabled = bulkSelectionDisabled;
    });
    U.ceoSessionList?.querySelectorAll("[data-session-menu-toggle], [data-session-rename], [data-session-delete]")?.forEach((button) => {
        button.disabled = mutationDisabled;
    });
    if (mutationDisabled || S.ceoBulkMode) closeCeoSessionMenus();
    if (U.ceoSessionTabLocal) U.ceoSessionTabLocal.setAttribute("aria-pressed", S.ceoSessionTab === "local" ? "true" : "false");
    if (U.ceoSessionTabChannel) U.ceoSessionTabChannel.setAttribute("aria-pressed", S.ceoSessionTab === "channel" ? "true" : "false");
    if (U.ceoSessionTabs) {
        U.ceoSessionTabs.style.setProperty("--ceo-session-tab-index", S.ceoSessionTab === "channel" ? "1" : "0");
        U.ceoSessionTabs.dataset.active = S.ceoSessionTab;
    }
}

function resetCeoSessionState({ scrollToLatest = false } = {}) {
    resetCeoFeed();
    S.ceoFeedRenderSessionId = "";
    S.ceoPendingTurns = [];
    S.ceoTurnActive = false;
    S.ceoPauseBusy = false;
    if (scrollToLatest) S.ceoScrollToLatestOnSnapshot = true;
    syncCeoCompressionDivider();
    syncCeoPrimaryButton();
}

function renderCeoSessionCard(item, { allowActions = false, index = -1 } = {}) {
    const sessionId = String(item?.session_id || "");
    const isActive = sessionId === activeSessionId();
    const isRunning = !!item?.is_running;
    const isErrored = String(item?.status || "").trim().toLowerCase() === "error";
    // 黄档＝需要人来接手的停住：等审批挂起（有未答复中断）或这一轮异常停止。
    // 手动暂停与停机暂停同落 status=paused，按口径不算异常，不画。
    const isAttention = isErrored || !!item?.has_pending_interrupts;
    const sessionStatusLabel = isErrored
        ? "（异常停止）"
        : (item?.has_pending_interrupts ? "（等待审批）" : (isRunning ? "（运行中）" : ""));
    const isBulkMode = !!S.ceoBulkMode;
    const isSelected = isCeoBulkSessionSelected(sessionId);
    const dragIndex = Number.isInteger(index) ? index : -1;
    const dragAttrs = ceoSessionDragEnabled() && dragIndex >= 0
        ? ` draggable="true" data-ceo-session-index="${dragIndex}"`
        : "";
    const preview = String(item?.preview_text || "").trim() || "No messages yet.";
    const title = String(item?.title || sessionId || "Session");
    const glyph = ceoSessionGlyph(item);
    const unreadCount = isActive ? 0 : sessionUnreadCount(sessionId);
    const unreadText = unreadCount > 99 ? "99+" : String(unreadCount);
    const createdText = formatSessionTime(item?.created_at);
    const type = String(item?.chat_type || "").trim();
    const typeLabel = type === "dm" ? "DM merged" : type === "group" ? "Group" : type === "thread" ? "Thread" : "";
    const badges = [
        typeLabel ? `<span class="ceo-session-pill">${esc(typeLabel)}</span>` : "",
        item?.is_readonly && !item?.can_message ? '<span class="ceo-session-pill readonly">只读</span>' : "",
    ].filter(Boolean).join("");
    return `
        <div class="ceo-session-card${isActive ? " is-active" : ""}${unreadCount > 0 ? " has-unread" : ""}${isRunning ? " is-running" : ""}${isAttention ? " is-attention" : ""}${isBulkMode ? " is-bulk-mode" : ""}${isSelected ? " is-bulk-selected" : ""}" role="listitem"${dragAttrs}>
            ${isBulkMode ? `
                <label class="ceo-session-checkbox" aria-label="${esc(`选择会话 ${title}`)}">
                    <input type="checkbox" data-session-bulk-checkbox="${esc(sessionId)}" ${isSelected ? "checked" : ""}>
                    <span class="ceo-session-checkbox__box" aria-hidden="true"></span>
                </label>
            ` : ""}
            <button
                type="button"
                class="ceo-session-main ceo-session-select"
                data-session-activate="${esc(sessionId)}"
                aria-pressed="${isBulkMode ? (isSelected ? "true" : "false") : (isActive ? "true" : "false")}"
                aria-label="${esc(`${title}${sessionStatusLabel}`)}"
                title="${esc(title)}"
            >
                <span class="ceo-session-glyph" aria-hidden="true">${esc(glyph)}</span>
                <span class="ceo-session-body">
                    <span class="ceo-session-head">
                        <span class="ceo-session-title">${esc(title)}</span>
                    </span>
                    <span class="ceo-session-preview">${esc(preview)}</span>
                    ${badges ? `<span class="ceo-session-badges">${badges}</span>` : ""}
                </span>
                ${unreadCount > 0 ? `<span class="ceo-session-unread" aria-label="${esc(`${unreadCount} unread message${unreadCount > 1 ? "s" : ""}`)}">${esc(unreadText)}</span>` : ""}
            </button>
            ${allowActions && !isBulkMode ? `
                <div class="ceo-session-actions toolbar-dropdown" data-session-menu="${esc(sessionId)}" aria-label="Session actions">
                    <button type="button" class="ceo-session-action ceo-session-menu-trigger" data-session-menu-toggle="${esc(sessionId)}" aria-label="More session actions" aria-haspopup="menu" aria-expanded="false">
                        <i data-lucide="more-horizontal"></i>
                    </button>
                    <div class="toolbar-menu ceo-session-menu" role="menu" hidden>
                        <div class="ceo-session-menu-info">
                            <span class="ceo-session-menu-info-row">
                                <span>会话 ID</span>
                                <span class="ceo-session-menu-info-value">
                                    <code>${esc(sessionId || "-")}</code>
                                    ${sessionId ? `<button type="button" class="ceo-session-menu-copy" data-session-copy-id="${esc(sessionId)}" aria-label="复制会话 ID" title="复制会话 ID"><i data-lucide="copy"></i></button>` : ""}
                                </span>
                            </span>
                            <span class="ceo-session-menu-info-row"><span>创建时间</span><code>${esc(createdText || "-")}</code></span>
                        </div>
                        ${item?.can_rename === false ? "" : `<button type="button" class="toolbar-menu-item" data-session-rename="${esc(sessionId)}" role="menuitem">命名</button>`}
                        ${item?.can_delete === false ? "" : `<button type="button" class="toolbar-menu-item danger" data-session-delete="${esc(sessionId)}" role="menuitem">删除</button>`}
                    </div>
                </div>
            ` : ""}
        </div>
    `;
}

function renderCeoSessions() {
    if (!U.ceoSessionList) return;
    const sessions = visibleCeoSessions();
    if (U.ceoSessionCurrent) {
        U.ceoSessionCurrent.innerHTML = "";
        U.ceoSessionCurrent.hidden = true;
    }
    if (!sessions.length) {
        U.ceoSessionList.innerHTML = `<div class="empty-state ceo-session-empty">${S.ceoSessionTab === "channel" ? "暂无渠道会话。" : "No sessions yet."}</div>`;
        syncCeoComposerReadonlyState();
        syncCeoAttachButton();
        syncCeoSessionActions();
        syncCeoCompressionDivider();
        return;
    }
    if (S.ceoSessionTab === "channel") {
        U.ceoSessionList.innerHTML = (Array.isArray(S.ceoChannelGroups) ? S.ceoChannelGroups : []).map((group) => {
            const items = Array.isArray(group?.items) ? group.items : [];
            const channelId = String(group?.channel_id || "").trim();
            const label = String(group?.label || group?.channel_id || "渠道");
            const collapsed = isCeoChannelCollapsed(channelId);
            const unreadTotal = collapsed ? ceoChannelUnreadTotal(items) : 0;
            const unreadText = unreadTotal > 99 ? "99+" : String(unreadTotal);
            // 窄栏只剩一颗字母块，渠道 tile 用它替掉的那张卡片同一个 glyph，读起来是"这组被折起来了"。
            const channelGlyph = collapsed ? (items.length ? ceoSessionGlyph(items[0]) : "@") : "";
            return `
                <section class="ceo-session-group${collapsed ? " is-collapsed" : ""}" data-channel-group="${esc(channelId)}">
                    <button type="button" class="ceo-session-group-title" data-channel-collapse="${esc(channelId)}" aria-expanded="${collapsed ? "false" : "true"}" title="${esc(collapsed ? "展开该渠道" : "折叠该渠道")}">
                        <span class="ceo-session-group-caret" aria-hidden="true"><i data-lucide="chevron-down"></i></span>
                        <span class="ceo-session-group-label">${esc(label)}</span>
                        ${unreadTotal > 0 ? `<span class="ceo-session-group-unread" aria-label="${esc(`新消息 ${unreadTotal} 条`)}">${esc(unreadText)}</span>` : ""}
                    </button>
                    ${collapsed ? `
                    <button type="button" class="ceo-session-channel-tile" data-channel-collapse="${esc(channelId)}" aria-expanded="false" aria-label="${esc(`展开渠道 ${label}`)}" title="${esc(label)}">
                        <span class="ceo-session-glyph" aria-hidden="true">${esc(channelGlyph)}</span>
                        ${unreadTotal > 0 ? `<span class="ceo-session-channel-unread" aria-hidden="true">${esc(unreadText)}</span>` : ""}
                    </button>` : ""}
                    ${collapsed ? "" : `
                    <div class="ceo-session-group-list" role="list">
                        ${items.map((item) => renderCeoSessionCard(item, { allowActions: true })).join("")}
                    </div>`}
                </section>
            `;
        }).join("");
    } else {
        U.ceoSessionList.innerHTML = sessions.map((item, index) => renderCeoSessionCard(item, { allowActions: true, index })).join("");
    }
    syncCeoComposerReadonlyState();
    syncCeoAttachButton();
    syncCeoSessionActions();
    syncCeoCompressionDivider();
    icons();
}

function ceoSessionDragEnabled() {
    // 渠道页签按群类型分组、没有扁平序号；批量模式整卡是勾选区。两者都不拖动。
    return S.ceoSessionTab !== "channel" && !S.ceoBulkMode;
}

function clearCeoSessionDragDecorations() {
    const list = U.ceoSessionList;
    if (!list) return;
    list.querySelectorAll(".is-drop-target").forEach((item) => item.classList.remove("is-drop-target"));
    list.querySelectorAll(".is-dragging").forEach((item) => item.classList.remove("is-dragging"));
    list.classList.remove("is-drop-zone");
}

function beginCeoSessionCardDrag(event) {
    const list = U.ceoSessionList;
    const card = event.target instanceof Element ? event.target.closest("[data-ceo-session-index]") : null;
    if (!list || !card || !ceoSessionDragEnabled()) return;
    const index = Number(card.dataset.ceoSessionIndex);
    if (!Number.isInteger(index) || index < 0) return;
    S.ceoSessionDrag = { from: index, dropIndex: null };
    card.classList.add("is-dragging");
    if (event.dataTransfer) {
        event.dataTransfer.effectAllowed = "move";
        try {
            event.dataTransfer.setData("text/plain", String(index));
        } catch (error) {
            void error;
        }
    }
}

function updateCeoSessionCardDropTarget(event) {
    const list = U.ceoSessionList;
    const drag = S.ceoSessionDrag;
    const from = Number(drag?.from);
    if (!list || !Number.isInteger(from) || from < 0) return;
    event.preventDefault();
    if (event.dataTransfer) event.dataTransfer.dropEffect = "move";
    const targetIndex = ceoSessionDropIndex(list, event.clientY);
    // 目标没变就不重画指示，避免拖动时列表抖动。
    if (drag.dropIndex === targetIndex) return;
    S.ceoSessionDrag = { ...drag, dropIndex: targetIndex };
    clearCeoSessionDragDecorations();
    const cards = [...list.querySelectorAll("[data-ceo-session-index]")];
    const dragging = cards.find((card) => Number(card.dataset.ceoSessionIndex) === from);
    if (dragging) dragging.classList.add("is-dragging");
    const anchor = cards.find((card) => Number(card.dataset.ceoSessionIndex) === targetIndex) || cards[cards.length - 1];
    if (anchor) anchor.classList.add("is-drop-target");
    list.classList.add("is-drop-zone");
}

function cancelCeoSessionCardDrag() {
    clearCeoSessionDragDecorations();
    S.ceoSessionDrag = null;
}

function finishCeoSessionCardDrag(event) {
    event.preventDefault();
    const drag = S.ceoSessionDrag;
    cancelCeoSessionCardDrag();
    const from = Number(drag?.from);
    if (!Number.isInteger(from) || from < 0) return;
    const ids = visibleCeoSessions()
        .map((item) => String(item?.session_id || "").trim())
        .filter(Boolean);
    if (!ids[from]) return;
    // 位次按“插到锚点之前”计，越过自身时补回一格（与模型链同一算法）。
    const target = Number.isInteger(drag.dropIndex)
        ? (drag.dropIndex > from ? drag.dropIndex - 1 : drag.dropIndex)
        : from;
    if (target === from) return;
    const next = ids.filter((_id, index) => index !== from);
    next.splice(Math.max(0, Math.min(next.length, target)), 0, ids[from]);
    if (next.join("\n") === ids.join("\n")) return;
    setCeoSessionOrder(next);
    // 顺序只在入站时重算，这里立刻按新手顺排一次，否则要到下一次快照才看得见。
    S.ceoLocalSessions = sortCeoSessionsByTime(S.ceoLocalSessions);
    rebuildCeoSessionIndex();
    renderCeoSessions();
}

function readStoredCeoSessionOrder() {
    try {
        const parsed = JSON.parse(readStoredUiPreference(CEO_SESSION_ORDER_KEY) || "[]");
        if (!Array.isArray(parsed)) return [];
        return parsed.map((id) => String(id || "").trim()).filter(Boolean);
    } catch (error) {
        return [];
    }
}

function setCeoSessionOrder(order) {
    S.ceoSessionOrder = [...(Array.isArray(order) ? order : [])];
    writeStoredUiPreference(CEO_SESSION_ORDER_KEY, JSON.stringify(S.ceoSessionOrder));
}

function buildCeoBulkDeleteSummary(items = []) {
    const entries = (Array.isArray(items) ? items : [])
        .map((entry) => {
            const item = entry?.item && typeof entry.item === "object"
                ? entry.item
                : (S.ceoSessions || []).find((session) => String(session?.session_id || "").trim() === String(entry?.session_id || "").trim()) || null;
            const sessionId = String(entry?.session_id || item?.session_id || "").trim();
            if (!sessionId) return null;
            return {
                item,
                sessionId,
                deleteCheck: entry?.deleteCheck && typeof entry.deleteCheck === "object" ? entry.deleteCheck : {},
            };
        })
        .filter(Boolean);
    const completedTaskIds = new Set();
    const pausedTaskIds = new Set();
    const inProgressTaskIds = new Set();
    let fallbackTotal = 0;
    let fallbackDeletable = 0;
    let fallbackInProgress = 0;
    let channelCount = 0;
    let channelHideCount = 0;
    let channelRetireCount = 0;
    let localCount = 0;
    entries.forEach(({ item, sessionId }) => {
        if (isChannelSessionItem(item) || sessionId.startsWith("china:")) {
            channelCount += 1;
            if (item?.hides_on_delete === true) channelHideCount += 1;
            else channelRetireCount += 1;
        } else localCount += 1;
        const deleteCheck = entries.find((entry) => entry.sessionId === sessionId)?.deleteCheck || {};
        const relatedTasks = deleteCheck?.related_tasks && typeof deleteCheck.related_tasks === "object" ? deleteCheck.related_tasks : {};
        fallbackTotal += normalizeInt(relatedTasks.total, 0);
        fallbackDeletable += normalizeInt(relatedTasks.deletable, normalizeInt(relatedTasks.terminal, 0));
        fallbackInProgress += normalizeInt(relatedTasks.in_progress, normalizeInt(relatedTasks.unfinished, 0));
        normalizeSessionDeleteTaskIds(deleteCheck?.usage?.completed_tasks).forEach((taskId) => completedTaskIds.add(taskId));
        normalizeSessionDeleteTaskIds(deleteCheck?.usage?.paused_tasks).forEach((taskId) => pausedTaskIds.add(taskId));
        normalizeSessionDeleteTaskIds(deleteCheck?.usage?.in_progress_tasks).forEach((taskId) => inProgressTaskIds.add(taskId));
        normalizeSessionDeleteTaskIds(deleteCheck?.usage?.tasks).forEach((taskId) => inProgressTaskIds.add(taskId));
    });
    const totalTaskIds = new Set([...completedTaskIds, ...pausedTaskIds, ...inProgressTaskIds]);
    const deletableTaskIds = new Set([...completedTaskIds, ...pausedTaskIds]);
    const relatedPayload = {
        related_tasks: {
            total: totalTaskIds.size || fallbackTotal,
            deletable: deletableTaskIds.size || fallbackDeletable,
            in_progress: inProgressTaskIds.size || fallbackInProgress,
            terminal: deletableTaskIds.size || fallbackDeletable,
            unfinished: inProgressTaskIds.size || fallbackInProgress,
        },
        usage: {
            completed_tasks: [...completedTaskIds],
            paused_tasks: [...pausedTaskIds],
            in_progress_tasks: [...inProgressTaskIds],
        },
    };
    const onlyChannel = channelCount > 0 && localCount === 0;
    const onlyLocal = localCount > 0 && channelCount === 0;
    const title = onlyChannel
        ? (entries.length > 1 ? "批量删除渠道会话" : "删除渠道会话")
        : onlyLocal
            ? (entries.length > 1 ? "批量删除会话" : "删除会话")
            : "批量清理会话";
    const textLines = [];
    if (localCount > 0) {
        textLines.push(`将删除所选 ${localCount} 个本地会话的聊天记录与附件。`);
    }
    if (channelHideCount > 0) {
        textLines.push(`将隐藏 ${channelHideCount} 个渠道会话，对方再发消息时它们会回到列表。`);
    }
    if (channelRetireCount > 0) {
        textLines.push(`将删除 ${channelRetireCount} 个渠道会话的上下文与附件，卡片不会回到列表。`);
    }
    const text = textLines.join("\n");
    return {
        title,
        text,
        checkboxLabel: "清除关联任务",
        checkboxHint: formatSessionDeleteHint(relatedPayload),
        checkboxDetails: formatSessionDeleteTaskDetails(relatedPayload),
        hasRelatedTaskRecords: normalizeInt(relatedPayload?.related_tasks?.total, 0) > 0,
        sessionIds: entries.map((entry) => entry.sessionId),
    };
}
