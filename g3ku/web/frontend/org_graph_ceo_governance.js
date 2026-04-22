const CEO_APPROVAL_DRAFT_STORAGE_PREFIX = "g3ku.ceo.approval-draft.v1";
const CEO_APPROVAL_ARGS_PREVIEW_MAX_CHARS = 260;

function createEmptyCeoApprovalFlow() {
    return {
        active: false,
        submitting: false,
        sessionId: "",
        interruptId: "",
        batchId: "",
        mode: "",
        submissionMode: "",
        reviewItems: [],
        decisions: {},
        currentIndex: 0,
        argsItemId: "",
    };
}

function ensureCeoGovernanceState() {
    if (!S.ceoGovernanceMode || typeof S.ceoGovernanceMode !== "object") {
        S.ceoGovernanceMode = {
            enabled: false,
            loading: false,
            saving: false,
            loaded: false,
            updatedAt: "",
        };
    }
    if (!S.ceoApprovalFlow || typeof S.ceoApprovalFlow !== "object") {
        S.ceoApprovalFlow = createEmptyCeoApprovalFlow();
    }
    if (!U.toolGovernanceBanner) U.toolGovernanceBanner = document.getElementById("tool-governance-banner");
    if (!U.toolGovernanceSwitch) U.toolGovernanceSwitch = document.getElementById("tool-governance-switch");
    if (!U.toolGovernanceSwitchTrack) U.toolGovernanceSwitchTrack = document.getElementById("tool-governance-switch-track");
    if (!U.toolGovernanceSwitchLabel) U.toolGovernanceSwitchLabel = document.getElementById("tool-governance-switch-label");
    if (!U.ceoApprovalViewport) U.ceoApprovalViewport = document.getElementById("ceo-approval-viewport");
    if (!U.ceoApprovalArgsBackdrop) U.ceoApprovalArgsBackdrop = document.getElementById("ceo-approval-args-backdrop");
    if (!U.ceoApprovalArgsDrawer) U.ceoApprovalArgsDrawer = document.getElementById("ceo-approval-args-drawer");
    if (!U.ceoApprovalArgsTitle) U.ceoApprovalArgsTitle = document.getElementById("ceo-approval-args-title");
    if (!U.ceoApprovalArgsSubtitle) U.ceoApprovalArgsSubtitle = document.getElementById("ceo-approval-args-subtitle");
    if (!U.ceoApprovalArgsBody) U.ceoApprovalArgsBody = document.getElementById("ceo-approval-args-body");
    if (!U.ceoApprovalArgsClose) U.ceoApprovalArgsClose = document.getElementById("ceo-approval-args-close");
}

function governanceModeState() {
    ensureCeoGovernanceState();
    return S.ceoGovernanceMode;
}

function approvalFlowState() {
    ensureCeoGovernanceState();
    return S.ceoApprovalFlow;
}

function approvalDraftStorageKey(sessionId, batchId) {
    return `${CEO_APPROVAL_DRAFT_STORAGE_PREFIX}:${String(sessionId || "").trim()}:${String(batchId || "").trim()}`;
}

function readApprovalDraft(sessionId, batchId) {
    const key = approvalDraftStorageKey(sessionId, batchId);
    try {
        const raw = window.sessionStorage?.getItem(key);
        if (!raw) return null;
        const parsed = JSON.parse(raw);
        return parsed && typeof parsed === "object" ? parsed : null;
    } catch (error) {
        void error;
        return null;
    }
}

function persistApprovalDraft(flow = approvalFlowState()) {
    const sessionId = String(flow?.sessionId || "").trim();
    const batchId = String(flow?.batchId || "").trim();
    if (!sessionId || !batchId || (!flow?.active && !flow?.submitting)) return;
    const payload = {
        sessionId,
        batchId,
        currentIndex: Math.max(0, Number(flow.currentIndex) || 0),
        decisions: flow.decisions && typeof flow.decisions === "object" ? flow.decisions : {},
    };
    try {
        window.sessionStorage?.setItem(approvalDraftStorageKey(sessionId, batchId), JSON.stringify(payload));
    } catch (error) {
        void error;
    }
}

function clearApprovalDraft(sessionId, batchId) {
    const key = approvalDraftStorageKey(sessionId, batchId);
    try {
        window.sessionStorage?.removeItem(key);
    } catch (error) {
        void error;
    }
}

function normalizeApprovalDecision(value = "") {
    const normalized = String(value || "").trim().toLowerCase();
    return normalized === "approve" || normalized === "reject" ? normalized : "";
}

function approvalRiskLevel(level = "") {
    const normalized = String(level || "").trim().toLowerCase();
    if (normalized === "low" || normalized === "medium" || normalized === "high") return normalized;
    return "medium";
}

function approvalRiskIcon(level = "") {
    const normalized = approvalRiskLevel(level);
    if (normalized === "high") return "triangle-alert";
    if (normalized === "medium") return "shield";
    return "shield-check";
}

function approvalArgsPreview(args) {
    const raw = typeof args === "string"
        ? args
        : JSON.stringify(args ?? {}, null, 2);
    const text = String(raw || "").trim() || "{}";
    if (text.length <= CEO_APPROVAL_ARGS_PREVIEW_MAX_CHARS) return text;
    return `${text.slice(0, CEO_APPROVAL_ARGS_PREVIEW_MAX_CHARS).trimEnd()}...`;
}

function approvalArgsFullText(args) {
    if (typeof args === "string") return String(args || "").trim() || "{}";
    return JSON.stringify(args ?? {}, null, 2);
}

function approvalReviewItem(raw, index = 0) {
    const source = raw && typeof raw === "object" ? raw : {};
    const toolCallId = String(source.tool_call_id || source.id || `approval-call-${index + 1}`).trim() || `approval-call-${index + 1}`;
    const name = String(source.name || source.tool_name || "unknown_tool").trim() || "unknown_tool";
    const riskLevel = approvalRiskLevel(source.risk_level || source.riskLevel || "");
    const args = source.arguments !== undefined ? source.arguments : (source.args !== undefined ? source.args : {});
    return {
        tool_call_id: toolCallId,
        name,
        risk_level: riskLevel,
        arguments: args,
        args_preview: approvalArgsPreview(args),
    };
}

function normalizeApprovalInterrupt(interrupt, sessionId = activeSessionId()) {
    const source = interrupt && typeof interrupt === "object" ? interrupt : {};
    const value = source.value && typeof source.value === "object" ? source.value : source;
    const kind = String(value.kind || "").trim();
    if (kind === "frontdoor_tool_approval_batch") {
        const reviewItems = (Array.isArray(value.review_items) ? value.review_items : [])
            .map((item, index) => approvalReviewItem(item, index))
            .filter((item) => String(item.tool_call_id || "").trim());
        if (!reviewItems.length) return null;
        return {
            active: true,
            submitting: false,
            sessionId: String(sessionId || "").trim(),
            interruptId: String(source.id || "").trim(),
            batchId: String(value.batch_id || "").trim(),
            mode: String(value.mode || "").trim(),
            submissionMode: String(value.submission_mode || "").trim(),
            reviewItems,
            decisions: {},
            currentIndex: 0,
            argsItemId: "",
        };
    }
    if (kind === "frontdoor_tool_approval") {
        const toolCalls = Array.isArray(value.tool_calls) ? value.tool_calls : [];
        if (!toolCalls.length) return null;
        const reviewItems = toolCalls.map((item, index) => approvalReviewItem({
            tool_call_id: item?.id || item?.tool_call_id,
            name: item?.name || item?.tool_name,
            risk_level: item?.risk_level || "medium",
            arguments: item?.arguments,
        }, index));
        return {
            active: true,
            submitting: false,
            sessionId: String(sessionId || "").trim(),
            interruptId: String(source.id || "").trim(),
            batchId: `legacy:${String(source.id || "approval").trim() || "approval"}`,
            mode: "legacy_review",
            submissionMode: "batch_submit_only",
            reviewItems,
            decisions: {},
            currentIndex: 0,
            argsItemId: "",
        };
    }
    return null;
}

function hydrateApprovalDecisions(flow, draft = null) {
    const nextDecisions = {};
    const draftDecisions = draft && typeof draft.decisions === "object" ? draft.decisions : {};
    flow.reviewItems.forEach((item) => {
        const toolCallId = String(item.tool_call_id || "").trim();
        const current = flow.decisions && typeof flow.decisions === "object" ? flow.decisions[toolCallId] : null;
        const fallback = draftDecisions[toolCallId];
        const decision = normalizeApprovalDecision(current?.decision || fallback?.decision || "");
        const note = String(current?.note || fallback?.note || "").trim();
        nextDecisions[toolCallId] = { decision, note };
    });
    flow.decisions = nextDecisions;
    const maxIndex = Math.max(0, flow.reviewItems.length - 1);
    const draftIndex = Math.max(0, Number(draft?.currentIndex) || 0);
    flow.currentIndex = Math.min(maxIndex, Math.max(0, Number(flow.currentIndex) || 0, draftIndex));
    return flow;
}

function activeApprovalItem(flow = approvalFlowState()) {
    const items = Array.isArray(flow?.reviewItems) ? flow.reviewItems : [];
    if (!items.length) return null;
    const index = Math.min(items.length - 1, Math.max(0, Number(flow?.currentIndex) || 0));
    return items[index] || null;
}

function approvalDecisionFor(toolCallId, flow = approvalFlowState()) {
    const decisions = flow?.decisions && typeof flow.decisions === "object" ? flow.decisions : {};
    return decisions[String(toolCallId || "").trim()] || { decision: "", note: "" };
}

function countApprovalSelections(flow = approvalFlowState()) {
    const items = Array.isArray(flow?.reviewItems) ? flow.reviewItems : [];
    return items.filter((item) => !!normalizeApprovalDecision(approvalDecisionFor(item.tool_call_id, flow).decision)).length;
}

function hasActiveCeoApprovalBlockingState(sessionId = activeSessionId()) {
    const flow = approvalFlowState();
    return !!(flow?.active || flow?.submitting) && String(flow?.sessionId || "").trim() === String(sessionId || "").trim();
}

window.hasActiveCeoApprovalBlockingState = hasActiveCeoApprovalBlockingState;

function clearCeoApprovalFlow({ preserveDraft = true, sessionId = "", batchId = "" } = {}) {
    const flow = approvalFlowState();
    const targetSessionId = String(sessionId || flow.sessionId || "").trim();
    const targetBatchId = String(batchId || flow.batchId || "").trim();
    if (!preserveDraft && targetSessionId && targetBatchId) {
        clearApprovalDraft(targetSessionId, targetBatchId);
    }
    S.ceoApprovalFlow = createEmptyCeoApprovalFlow();
    renderCeoApprovalArgsModal();
    renderCeoApprovalFlow();
}

function applyCeoApprovalFlow(flow, { preserveExistingDecisions = false } = {}) {
    const next = flow && typeof flow === "object" ? { ...createEmptyCeoApprovalFlow(), ...flow } : createEmptyCeoApprovalFlow();
    const draft = readApprovalDraft(next.sessionId, next.batchId);
    if (preserveExistingDecisions) {
        const current = approvalFlowState();
        if (current?.sessionId === next.sessionId && current?.batchId === next.batchId) {
            next.decisions = current.decisions || next.decisions;
            next.currentIndex = current.currentIndex;
            next.submitting = current.submitting;
        }
    }
    hydrateApprovalDecisions(next, draft);
    S.ceoApprovalFlow = next;
    persistApprovalDraft(next);
    renderCeoApprovalArgsModal();
    renderCeoApprovalFlow();
}

function syncCeoApprovalFromInterrupts(interrupts = [], sessionId = activeSessionId(), { authoritative = false } = {}) {
    ensureCeoGovernanceState();
    const normalizedSessionId = String(sessionId || "").trim();
    const nextFlow = (Array.isArray(interrupts) ? interrupts : [])
        .map((item) => normalizeApprovalInterrupt(item, normalizedSessionId))
        .find(Boolean);
    if (!nextFlow) {
        if (authoritative && hasActiveCeoApprovalBlockingState(normalizedSessionId)) {
            clearCeoApprovalFlow({
                preserveDraft: approvalFlowState().submitting,
                sessionId: normalizedSessionId,
            });
        } else {
            renderCeoApprovalFlow();
        }
        return null;
    }
    applyCeoApprovalFlow(nextFlow, { preserveExistingDecisions: true });
    return nextFlow;
}

window.syncCeoApprovalFromInterrupts = syncCeoApprovalFromInterrupts;

async function refreshCeoApprovalFromServer(sessionId = activeSessionId(), { quiet = true } = {}) {
    const normalizedSessionId = String(sessionId || "").trim();
    if (!normalizedSessionId) {
        clearCeoApprovalFlow({ preserveDraft: true });
        return [];
    }
    try {
        const items = await ApiClient.getCeoPendingInterrupts(normalizedSessionId);
        syncCeoApprovalFromInterrupts(items, normalizedSessionId, { authoritative: true });
        return items;
    } catch (error) {
        if (!quiet) {
            showToast({
                title: "审批状态获取失败",
                text: error?.message || "无法读取当前审批状态。",
                kind: "error",
                durationMs: 2600,
            });
        }
        return [];
    }
}

window.refreshCeoApprovalFromServer = refreshCeoApprovalFromServer;

function isApprovalInterruptKind(value = null) {
    const kind = String(value?.kind || "").trim();
    return kind === "frontdoor_tool_approval" || kind === "frontdoor_tool_approval_batch";
}

function clearApprovalInterruptsFromSnapshotCache(sessionId = activeSessionId()) {
    const normalizedSessionId = String(sessionId || "").trim();
    if (!normalizedSessionId || typeof patchCeoSessionSnapshotCache !== "function") return;
    patchCeoSessionSnapshotCache(normalizedSessionId, (entry) => {
        const currentEntry = entry && typeof entry === "object" ? entry : {};
        const stripInterrupts = (snapshot) => {
            if (!snapshot || typeof snapshot !== "object") return { snapshot, changed: false };
            const interrupts = Array.isArray(snapshot?.interrupts) ? snapshot.interrupts : [];
            if (!interrupts.length) return { snapshot, changed: false };
            const kept = interrupts.filter((item) => !isApprovalInterruptKind(item?.value || null));
            if (kept.length === interrupts.length) return { snapshot, changed: false };
            const nextSnapshot = { ...snapshot };
            if (kept.length) nextSnapshot.interrupts = kept;
            else delete nextSnapshot.interrupts;
            return { snapshot: nextSnapshot, changed: true };
        };
        const inflight = stripInterrupts(currentEntry.inflight_turn);
        const preserved = stripInterrupts(currentEntry.preserved_turn);
        if (!inflight.changed && !preserved.changed) return currentEntry;
        return {
            ...currentEntry,
            inflight_turn: inflight.snapshot,
            preserved_turn: preserved.snapshot,
        };
    });
}

function rebindActiveApprovalTurnToUserLane() {
    if (typeof getActiveCeoTurn !== "function") return;
    const turn = getActiveCeoTurn("approval");
    if (!turn || turn.finalized) return;
    turn.source = "user";
}

function renderToolGovernanceModeLegacy() {
    ensureCeoGovernanceState();
    const state = governanceModeState();
    const banner = U.toolGovernanceBanner;
    const toggle = U.toolGovernanceSwitch;
    const label = U.toolGovernanceSwitchLabel;
    const text = U.toolGovernanceBannerText;
    if (!banner || !toggle || !label || !text) return;
    const enabled = !!state.enabled;
    toggle.setAttribute("aria-pressed", enabled ? "true" : "false");
    toggle.disabled = !!state.loading || !!state.saving;
    label.textContent = state.saving
        ? "保存中..."
        : state.loading
            ? "加载中..."
            : enabled
                ? "已开启"
                : "已关闭";
    text.textContent = enabled
        ? "开启后，主 Agent 调用中/高风险 Tool 会进入逐个审批，并在最后统一提交。"
        : "关闭后，主 Agent 将按现有运行方式直接处理 Tool 调用，不再进入监管审批。";
}

async function loadToolGovernanceMode({ quiet = true } = {}) {
    ensureCeoGovernanceState();
    const state = governanceModeState();
    if (state.loading) return state;
    state.loading = true;
    renderToolGovernanceMode();
    try {
        const item = await ApiClient.getToolGovernanceMode();
        state.enabled = !!item?.enabled;
        state.updatedAt = String(item?.updated_at || "").trim();
        state.loaded = true;
        return state;
    } catch (error) {
        if (!quiet) {
            showToast({
                title: "监管模式加载失败",
                text: error?.message || "无法读取监管模式状态。",
                kind: "error",
                durationMs: 2600,
            });
        }
        return state;
    } finally {
        state.loading = false;
        renderToolGovernanceMode();
    }
}

window.loadToolGovernanceMode = loadToolGovernanceMode;

function renderToolGovernanceMode() {
    ensureCeoGovernanceState();
    const state = governanceModeState();
    const banner = U.toolGovernanceBanner;
    const toggle = U.toolGovernanceSwitch;
    const label = U.toolGovernanceSwitchLabel;
    if (!banner || !toggle || !label) return;
    const enabled = !!state.enabled;
    const description = enabled
        ? "开启后，主 Agent 调用中/高风险 Tool 会进入逐个审批，并在最后统一提交。"
        : "关闭后，后续 Tool 调用将按非监管模式直接继续执行。";
    toggle.setAttribute("aria-pressed", enabled ? "true" : "false");
    toggle.setAttribute("aria-label", enabled ? "关闭监管模式" : "开启监管模式");
    toggle.title = description;
    toggle.disabled = !!state.loading || !!state.saving;
    banner.dataset.enabled = enabled ? "true" : "false";
    banner.title = description;
    label.textContent = state.saving
        ? "保存中"
        : state.loading
            ? "加载中"
            : enabled
                ? "开启"
                : "关闭";
}

async function toggleToolGovernanceMode() {
    ensureCeoGovernanceState();
    const state = governanceModeState();
    if (state.loading || state.saving) return;
    state.saving = true;
    renderToolGovernanceMode();
    try {
        const item = await ApiClient.updateToolGovernanceMode(!state.enabled);
        state.enabled = !!item?.enabled;
        state.updatedAt = String(item?.updated_at || "").trim();
        state.loaded = true;
        showToast({
            title: "监管模式已更新",
            text: state.enabled ? "后续中/高风险 Tool 调用将进入审批。" : "后续 Tool 调用将按非监管模式运行。",
            kind: "success",
            durationMs: 2200,
        });
    } catch (error) {
        showToast({
            title: "监管模式更新失败",
            text: error?.message || "保存监管模式失败。",
            kind: "error",
            durationMs: 2800,
        });
    } finally {
        state.saving = false;
        renderToolGovernanceMode();
    }
}

function setApprovalDecision(toolCallId, decision) {
    const flow = approvalFlowState();
    const key = String(toolCallId || "").trim();
    if (!key || !flow.decisions || typeof flow.decisions !== "object") return;
    const current = approvalDecisionFor(key, flow);
    flow.decisions[key] = {
        decision: normalizeApprovalDecision(decision),
        note: String(current?.note || "").trim(),
    };
    persistApprovalDraft(flow);
    renderCeoApprovalFlow();
}

function setApprovalDecisionNote(toolCallId, note = "") {
    const flow = approvalFlowState();
    const key = String(toolCallId || "").trim();
    if (!key || !flow.decisions || typeof flow.decisions !== "object") return;
    const current = approvalDecisionFor(key, flow);
    flow.decisions[key] = {
        decision: normalizeApprovalDecision(current?.decision || ""),
        note: String(note || "").trim(),
    };
    persistApprovalDraft(flow);
}

function openCeoApprovalArgsModal(toolCallId = "") {
    const flow = approvalFlowState();
    flow.argsItemId = String(toolCallId || "").trim();
    renderCeoApprovalArgsModal();
}

function closeCeoApprovalArgsModal() {
    const flow = approvalFlowState();
    flow.argsItemId = "";
    renderCeoApprovalArgsModal();
}

function renderCeoApprovalArgsModal() {
    ensureCeoGovernanceState();
    const flow = approvalFlowState();
    const currentItem = (Array.isArray(flow.reviewItems) ? flow.reviewItems : [])
        .find((item) => String(item.tool_call_id || "").trim() === String(flow.argsItemId || "").trim()) || null;
    if (U.ceoApprovalArgsTitle) {
        U.ceoApprovalArgsTitle.textContent = currentItem ? `${currentItem.name} 工具入参` : "工具入参";
    }
    if (U.ceoApprovalArgsSubtitle) {
        U.ceoApprovalArgsSubtitle.textContent = currentItem
            ? `tool_call_id: ${currentItem.tool_call_id}`
            : "完整参数仅供审批查看，不会在这里修改。";
    }
    if (U.ceoApprovalArgsBody) {
        U.ceoApprovalArgsBody.textContent = currentItem ? approvalArgsFullText(currentItem.arguments) : "";
    }
    setDrawerOpen(U.ceoApprovalArgsBackdrop, U.ceoApprovalArgsDrawer, !!currentItem);
}

function renderCeoApprovalFlow() {
    ensureCeoGovernanceState();
    const viewport = U.ceoApprovalViewport;
    if (!viewport) return;
    const flow = approvalFlowState();
    const shouldShow = hasActiveCeoApprovalBlockingState(activeSessionId());
    viewport.hidden = !shouldShow;
    if (!shouldShow) {
        viewport.innerHTML = "";
        renderCeoApprovalArgsModal();
        return;
    }
    if (flow.submitting) {
        viewport.innerHTML = `
            <div class="ceo-approval-toast is-submitting">
                <div class="ceo-approval-copy">
                    <div class="ceo-approval-kicker">监管模式</div>
                    <div class="ceo-approval-title">审批结果提交中</div>
                    <div class="ceo-approval-helper">本批次选择已发送到后端，等待主 Agent 继续处理。</div>
                </div>
            </div>
        `;
        icons();
        return;
    }
    const item = activeApprovalItem(flow);
    if (!item) {
        viewport.innerHTML = "";
        return;
    }
    const currentIndex = Math.min((flow.reviewItems?.length || 1) - 1, Math.max(0, Number(flow.currentIndex) || 0));
    const total = flow.reviewItems.length || 1;
    const selectedCount = countApprovalSelections(flow);
    const decision = approvalDecisionFor(item.tool_call_id, flow);
    const canProceed = !!normalizeApprovalDecision(decision.decision);
    const isLast = currentIndex >= total - 1;
    const riskLabel = typeof displayRiskLabel === "function"
        ? displayRiskLabel(item.risk_level)
        : item.risk_level;
    const previewText = item.args_preview || "{}";
    viewport.innerHTML = `
        <div class="ceo-approval-toast">
            <div class="ceo-approval-head">
                <button type="button" class="icon-btn ceo-approval-nav" data-approval-back aria-label="回退到上一项" ${currentIndex <= 0 ? "disabled" : ""}>
                    <i data-lucide="arrow-left"></i>
                </button>
                <div class="ceo-approval-copy">
                    <div class="ceo-approval-kicker">监管模式</div>
                    <div class="ceo-approval-title">请求调用工具 ${esc(item.name)}</div>
                    <div class="ceo-approval-subtitle">第 ${currentIndex + 1} / ${total} 项 · 批次 ${esc(flow.batchId || "-")}</div>
                </div>
                <span class="risk-pill ceo-approval-risk-pill risk-${esc(item.risk_level)}">
                    <i data-lucide="${approvalRiskIcon(item.risk_level)}"></i>
                    <span>${esc(riskLabel)}</span>
                </span>
            </div>
            <div class="ceo-approval-body">
                <button type="button" class="ceo-approval-args-preview" data-approval-open-args="${esc(item.tool_call_id)}">
                    <span class="ceo-approval-args-preview-label">入参预览</span>
                    <span class="ceo-approval-args-preview-text">${esc(previewText)}</span>
                </button>
                <div class="ceo-approval-choice-list">
                    <button type="button" class="ceo-approval-choice ${decision.decision === "approve" ? "is-selected" : ""}" data-approval-decision="approve" data-tool-call-id="${esc(item.tool_call_id)}">
                        <span class="ceo-approval-choice-main">
                            <i data-lucide="check-circle-2"></i>
                            <span>同意</span>
                        </span>
                    </button>
                    <div class="ceo-approval-choice-note-wrap">
                        <button type="button" class="ceo-approval-choice is-reject ${decision.decision === "reject" ? "is-selected" : ""}" data-approval-decision="reject" data-tool-call-id="${esc(item.tool_call_id)}">
                            <span class="ceo-approval-choice-main">
                                <i data-lucide="ban"></i>
                                <span>拒绝</span>
                            </span>
                        </button>
                        <textarea
                            class="ceo-approval-choice-note"
                            data-approval-note="${esc(item.tool_call_id)}"
                            placeholder="补充信息（可选）"
                            rows="2"
                            ${decision.decision === "reject" ? "" : "hidden"}
                        >${esc(decision.note || "")}</textarea>
                    </div>
                </div>
            </div>
            <div class="ceo-approval-footer">
                <div class="ceo-approval-progress">已选择 ${selectedCount} / ${total}</div>
                <div class="ceo-approval-actions">
                    <button type="button" class="toolbar-btn ${isLast ? "success ceo-approval-submit" : "ghost"}" data-approval-next ${canProceed ? "" : "disabled"}>
                        ${isLast ? "提交" : "下一项"}
                    </button>
                </div>
            </div>
        </div>
    `;
    icons();
}

async function submitCeoApprovalFlow() {
    const flow = approvalFlowState();
    const sessionId = String(flow.sessionId || activeSessionId() || "").trim();
    if (!flow.active || !sessionId || !Array.isArray(flow.reviewItems) || !flow.reviewItems.length) return;
    const openState = Number(window.WebSocket?.OPEN ?? 1);
    if (!S.ceoWs || S.ceoWs.readyState !== openState) {
        showToast({
            title: "连接未就绪",
            text: "审批结果暂时无法提交，请等待连接恢复后重试。",
            kind: "error",
            durationMs: 2800,
        });
        initCeoWs();
        return;
    }
    const decisions = flow.reviewItems.map((item) => {
        const current = approvalDecisionFor(item.tool_call_id, flow);
        const decision = normalizeApprovalDecision(current.decision || "");
        const payload = {
            tool_call_id: item.tool_call_id,
            decision,
        };
        const note = String(current.note || "").trim();
        if (decision === "reject" && note) payload.note = note;
        return payload;
    });
    if (decisions.some((item) => !item.decision)) {
        showToast({
            title: "审批未完成",
            text: "请先为当前批次中的每个工具调用选择同意或拒绝。",
            kind: "warn",
            durationMs: 2400,
        });
        return;
    }
    try {
        S.ceoWs.send(JSON.stringify({
            type: "client.resume_interrupt",
            session_id: sessionId,
            resume: {
                type: "submit_batch_review",
                batch_id: flow.batchId,
                decisions,
            },
        }));
        rebindActiveApprovalTurnToUserLane();
        clearApprovalInterruptsFromSnapshotCache(sessionId);
        clearApprovalDraft(sessionId, flow.batchId);
        flow.submitting = true;
        renderCeoApprovalFlow();
    } catch (error) {
        showToast({
            title: "审批提交失败",
            text: error?.message || "无法提交当前审批结果。",
            kind: "error",
            durationMs: 2800,
        });
    }
}

function bindToolGovernanceUi() {
    ensureCeoGovernanceState();
    if (U.toolGovernanceSwitch && !U.toolGovernanceSwitch.dataset.bound) {
        U.toolGovernanceSwitch.dataset.bound = "true";
        U.toolGovernanceSwitch.addEventListener("click", () => void toggleToolGovernanceMode());
    }
}

function bindCeoApprovalUi() {
    ensureCeoGovernanceState();
    if (U.ceoApprovalViewport && !U.ceoApprovalViewport.dataset.bound) {
        U.ceoApprovalViewport.dataset.bound = "true";
        U.ceoApprovalViewport.addEventListener("click", (event) => {
            const target = event.target instanceof Element ? event.target : null;
            if (!target) return;
            const backButton = target.closest("[data-approval-back]");
            if (backButton) {
                const flow = approvalFlowState();
                flow.currentIndex = Math.max(0, (Number(flow.currentIndex) || 0) - 1);
                persistApprovalDraft(flow);
                renderCeoApprovalFlow();
                return;
            }
            const argsTrigger = target.closest("[data-approval-open-args]");
            if (argsTrigger) {
                openCeoApprovalArgsModal(argsTrigger.getAttribute("data-approval-open-args") || "");
                return;
            }
            const decisionButton = target.closest("[data-approval-decision]");
            if (decisionButton) {
                const toolCallId = decisionButton.getAttribute("data-tool-call-id") || "";
                const nextDecision = decisionButton.getAttribute("data-approval-decision") || "";
                setApprovalDecision(
                    toolCallId,
                    nextDecision,
                );
                if (nextDecision === "reject") {
                    window.requestAnimationFrame(() => {
                        const escapedToolCallId = window.CSS?.escape
                            ? window.CSS.escape(toolCallId)
                            : toolCallId.replace(/["\\]/g, "\\$&");
                        const noteField = U.ceoApprovalViewport?.querySelector(`[data-approval-note="${escapedToolCallId}"]`);
                        noteField?.focus?.();
                    });
                }
                return;
            }
            const nextButton = target.closest("[data-approval-next]");
            if (nextButton) {
                const flow = approvalFlowState();
                const isLast = (Number(flow.currentIndex) || 0) >= ((flow.reviewItems?.length || 1) - 1);
                if (isLast) void submitCeoApprovalFlow();
                else {
                    flow.currentIndex = Math.min((flow.reviewItems?.length || 1) - 1, (Number(flow.currentIndex) || 0) + 1);
                    persistApprovalDraft(flow);
                    renderCeoApprovalFlow();
                }
            }
        });
        U.ceoApprovalViewport.addEventListener("input", (event) => {
            const target = event.target instanceof HTMLTextAreaElement ? event.target : null;
            if (!target || !target.matches("[data-approval-note]")) return;
            setApprovalDecisionNote(target.getAttribute("data-approval-note") || "", target.value || "");
        });
    }
    if (U.ceoApprovalArgsBackdrop && !U.ceoApprovalArgsBackdrop.dataset.bound) {
        U.ceoApprovalArgsBackdrop.dataset.bound = "true";
        U.ceoApprovalArgsBackdrop.addEventListener("click", (event) => {
            if (event.target === U.ceoApprovalArgsBackdrop) closeCeoApprovalArgsModal();
        });
    }
    if (U.ceoApprovalArgsClose && !U.ceoApprovalArgsClose.dataset.bound) {
        U.ceoApprovalArgsClose.dataset.bound = "true";
        U.ceoApprovalArgsClose.addEventListener("click", () => closeCeoApprovalArgsModal());
    }
}

bindToolGovernanceUi();
bindCeoApprovalUi();
renderToolGovernanceMode();
renderCeoApprovalFlow();
if (typeof syncCeoApprovalFromSnapshotEntry === "function") {
    syncCeoApprovalFromSnapshotEntry(activeSessionId(), null, {
        authoritative: true,
        refreshServer: true,
    });
} else if (typeof refreshCeoApprovalFromServer === "function") {
    void refreshCeoApprovalFromServer(activeSessionId(), { quiet: true });
}
if (S.view === "tools") {
    void loadToolGovernanceMode({ quiet: true });
}

const __baseCanMutateCeoSessions = canMutateCeoSessions;
canMutateCeoSessions = function wrappedCanMutateCeoSessions(...args) {
    return __baseCanMutateCeoSessions.apply(this, args);
};

const __baseSwitchView = switchView;
switchView = function approvalNonBlockingSwitchView(view, ...args) {
    return __baseSwitchView.call(this, view, ...args);
};

const __baseCanCreateCeoSessions = canCreateCeoSessions;
canCreateCeoSessions = function wrappedCanCreateCeoSessions(...args) {
    return __baseCanCreateCeoSessions.apply(this, args);
};

const __baseCanActivateCeoSessions = canActivateCeoSessions;
canActivateCeoSessions = function wrappedCanActivateCeoSessions(...args) {
    return __baseCanActivateCeoSessions.apply(this, args);
};

const __baseSyncCeoPrimaryButton = syncCeoPrimaryButton;
syncCeoPrimaryButton = function wrappedSyncCeoPrimaryButton(...args) {
    __baseSyncCeoPrimaryButton.apply(this, args);
    if (!hasActiveCeoApprovalBlockingState() || activeSessionIsReadonly()) return;
    if (!U.ceoSend) return;
    U.ceoSend.innerHTML = '<i data-lucide="shield"></i> 审批中';
    U.ceoSend.disabled = true;
    U.ceoSend.setAttribute("aria-label", "当前存在待审批工具调用");
    icons();
};

const __baseSendCeoMessage = sendCeoMessage;
sendCeoMessage = function wrappedSendCeoMessage(...args) {
    if (hasActiveCeoApprovalBlockingState()) {
        showToast({
            title: "等待审批",
            text: "请先完成当前工具审批，再继续发送新消息。",
            kind: "warn",
            durationMs: 2400,
        });
        void refreshCeoApprovalFromServer(activeSessionId(), { quiet: true });
        return;
    }
    return __baseSendCeoMessage.apply(this, args);
};

const __baseMaybeDispatchQueuedCeoFollowUps = maybeDispatchQueuedCeoFollowUps;
maybeDispatchQueuedCeoFollowUps = function wrappedMaybeDispatchQueuedCeoFollowUps(...args) {
    if (hasActiveCeoApprovalBlockingState()) return false;
    return __baseMaybeDispatchQueuedCeoFollowUps.apply(this, args);
};

const __baseSendActiveCeoFollowUpsToRuntime = sendActiveCeoFollowUpsToRuntime;
sendActiveCeoFollowUpsToRuntime = function wrappedSendActiveCeoFollowUpsToRuntime(...args) {
    if (hasActiveCeoApprovalBlockingState()) return null;
    return __baseSendActiveCeoFollowUpsToRuntime.apply(this, args);
};

const __baseApplyCeoState = applyCeoState;
applyCeoState = function wrappedApplyCeoState(state = {}, meta = {}) {
    const result = __baseApplyCeoState.call(this, state, meta);
    const status = String(state?.status || "").trim().toLowerCase();
    if (status && status !== "paused" && hasActiveCeoApprovalBlockingState()) {
        clearCeoApprovalFlow({
            preserveDraft: false,
            sessionId: activeSessionId(),
        });
    }
    return result;
};

const __baseResetCeoSessionState = resetCeoSessionState;
resetCeoSessionState = function wrappedResetCeoSessionState(...args) {
    const result = __baseResetCeoSessionState.apply(this, args);
    renderCeoApprovalFlow();
    return result;
};
