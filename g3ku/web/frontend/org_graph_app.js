const MODEL_SCOPES = [
    { key: "ceo", label: "主Agent" },
    { key: "execution", label: "执行" },
    { key: "inspection", label: "检验" },
];

const EMPTY_MODEL_ROLES = () => ({ ceo: [], execution: [], inspection: [] });
const DEFAULT_MODEL_DEFAULTS = () => ({ ceo: "", execution: "", inspection: "" });
const cloneModelRoles = (roles = EMPTY_MODEL_ROLES()) => {
    const next = EMPTY_MODEL_ROLES();
    MODEL_SCOPES.forEach(({ key }) => {
        next[key] = Array.isArray(roles?.[key])
            ? roles[key].map((item) => String(item || "").trim()).filter(Boolean)
            : [];
    });
    return next;
};

const S = {
    view: "ceo",
    ceoWs: null,
    ceoPendingTurns: [],
    taskWs: null,
    currentTaskId: null,
    tasks: [],
    currentTask: null,
    currentTaskProgress: null,
    taskArtifacts: [],
    selectedArtifactId: "",
    artifactContent: "",
    selectedTaskIds: new Set(),
    multiSelectMode: false,
    taskFilterMenuOpen: false,
    taskBatchMenuOpen: false,
    taskBusy: false,
    confirmState: null,
    toastState: { timeoutId: null, intervalId: null, remaining: 0 },
    openResourceSelectId: "",
    modelCatalog: {
        items: [],
        catalog: [],
        roles: EMPTY_MODEL_ROLES(),
        roleDrafts: EMPTY_MODEL_ROLES(),
        defaults: DEFAULT_MODEL_DEFAULTS(),
        loading: false,
        saving: false,
        error: "",
        search: "",
        selectedModelKey: "",
        mode: "view",
        roleEditing: false,
        rolesDirty: false,
        dragState: null,
    },
    tree: null,
    treeView: null,
    treePan: {
        active: false,
        originNodeId: null,
        startX: 0,
        startY: 0,
        offsetX: 0,
        offsetY: 0,
        baseOffsetX: 0,
        baseOffsetY: 0,
        moved: false,
        suppressClickNodeId: null,
    },
    selectedNodeId: null,
    skills: [],
    selectedSkill: null,
    skillFiles: [],
    skillContents: {},
    selectedSkillFile: "",
    skillBusy: false,
    skillDirty: false,
    tools: [],
    selectedTool: null,
    toolBusy: false,
    toolDirty: false,
};

const U = {
    nav: [...document.querySelectorAll(".nav-item")],
    theme: document.getElementById("theme-toggle"),
    ceoFeed: document.getElementById("ceo-chat-feed"),
    ceoInput: document.getElementById("ceo-input"),
    ceoSend: document.getElementById("ceo-send-btn"),
    viewCeo: document.getElementById("view-ceo"),
    viewTasks: document.getElementById("view-tasks-list"),
    viewSkills: document.getElementById("view-skills"),
    viewTools: document.getElementById("view-tools"),
    viewModels: document.getElementById("view-models"),
    viewTaskDetails: document.getElementById("view-task-details"),
    modelHint: document.getElementById("sidebar-model-hint"),
    modelRefresh: document.getElementById("model-refresh-btn"),
    modelCreate: document.getElementById("model-create-btn"),
    modelRolesCancel: document.getElementById("model-roles-cancel-btn"),
    modelRolesSave: document.getElementById("model-roles-save-btn"),
    modelRoleEditors: document.getElementById("model-role-editors"),
    modelSearch: document.getElementById("model-search-input"),
    modelList: document.getElementById("model-list"),
    modelDetailEmpty: document.getElementById("model-detail-empty"),
    modelDetail: document.getElementById("model-detail-content"),
    modelBackdrop: document.getElementById("model-detail-backdrop"),
    modelDrawer: document.querySelector(".model-detail-dialog"),
    taskGrid: document.getElementById("task-card-grid"),
    taskSelectionSummary: document.getElementById("task-selection-summary"),
    taskMultiToggle: document.getElementById("task-multi-toggle"),
    taskFilterWrap: document.getElementById("task-filter-wrap"),
    taskFilterTrigger: document.getElementById("task-filter-menu-trigger"),
    taskFilterMenu: document.getElementById("task-filter-menu"),
    taskBatchWrap: document.getElementById("task-batch-wrap"),
    taskBatchTrigger: document.getElementById("task-batch-menu-trigger"),
    taskBatchMenu: document.getElementById("task-batch-menu"),
    backToTasks: document.getElementById("back-to-tasks"),
    tdTitle: document.getElementById("td-title"),
    tdStatus: document.getElementById("td-status"),
    tdSummary: document.getElementById("td-summary"),
    tdActiveCount: document.getElementById("td-active-count"),
    tree: document.getElementById("org-tree-container"),
    taskSelectionEmpty: document.getElementById("task-selection-empty-inline"),
    taskDetailBackdrop: document.getElementById("task-detail-backdrop"),
    taskDetailDrawer: document.getElementById("task-detail-drawer"),
    artifactList: document.getElementById("artifact-list"),
    artifactContent: document.getElementById("artifact-content"),
    artifactApply: document.getElementById("artifact-apply-btn"),
    feedTitle: document.getElementById("feed-target-name"),
    detail: document.getElementById("agent-detail-view"),
    adRole: document.getElementById("ad-role"),
    adStatus: document.getElementById("ad-status"),
    adInput: document.getElementById("ad-input"),
    adOutput: document.getElementById("ad-output"),
    adCheck: document.getElementById("ad-check"),
    adLogs: document.getElementById("ad-logs"),
    nodeEmpty: document.getElementById("task-node-empty"),
    closeAgent: document.getElementById("close-agent-btn"),
    skillSearch: document.getElementById("skill-search-input"),
    skillRisk: document.getElementById("skill-risk-filter"),
    skillStatus: document.getElementById("skill-status-filter"),
    skillList: document.getElementById("skill-list"),
    skillEmpty: document.getElementById("skill-detail-empty"),
    skillDetail: document.getElementById("skill-detail-content"),
    skillBackdrop: document.getElementById("skill-detail-backdrop"),
    skillDrawer: document.querySelector(".skill-detail-panel"),
    skillRefresh: document.getElementById("skill-refresh-btn"),
    skillSave: document.getElementById("skill-save-btn"),
    toolSearch: document.getElementById("tool-search-input"),
    toolStatus: document.getElementById("tool-status-filter"),
    toolRisk: document.getElementById("tool-risk-filter"),
    toolList: document.getElementById("tool-list"),
    toolEmpty: document.getElementById("tool-detail-empty"),
    toolDetail: document.getElementById("tool-detail-content"),
    toolBackdrop: document.getElementById("tool-detail-backdrop"),
    toolDrawer: document.querySelector(".tool-detail-panel"),
    toolRefresh: document.getElementById("tool-refresh-btn"),
    toolSave: document.getElementById("tool-save-btn"),
    toast: document.getElementById("app-toast"),
    toastTitle: document.getElementById("app-toast-title"),
    toastText: document.getElementById("app-toast-text"),
    toastProgress: document.getElementById("app-toast-progress"),
    toastProgressBar: document.getElementById("app-toast-progress-bar"),
    toastClose: document.getElementById("app-toast-close"),
    confirmBackdrop: document.getElementById("confirm-backdrop"),
    confirmTitle: document.getElementById("confirm-title"),
    confirmText: document.getElementById("confirm-text"),
    confirmCancel: document.getElementById("confirm-cancel"),
    confirmAccept: document.getElementById("confirm-accept"),
};

const esc = (v) => String(v ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;").replaceAll('"', "&quot;").replaceAll("'", "&#39;");
const icons = () => window.lucide && lucide.createIcons();
const roleKey = (v) => (["ceo", "inspection", "checker"].includes(String(v).toLowerCase()) ? (String(v).toLowerCase() === "ceo" ? "ceo" : "inspection") : "execution");
const roleLabel = (v) => ({ ceo: "主Agent", execution: "执行", inspection: "检验" }[roleKey(v)]);
const pStatus = (v) => String(v || "").trim().toLowerCase();
const MD_TOKEN_MARKER = "\uE000";

function safeHref(value) {
    const href = String(value || "").trim();
    if (!href) return "#";
    if (/^(https?:|mailto:|tel:)/i.test(href) || href.startsWith("/") || href.startsWith("#")) return esc(href);
    return "#";
}

function createMarkdownToken(tokens, html) {
    const token = `${MD_TOKEN_MARKER}${tokens.length}${MD_TOKEN_MARKER}`;
    tokens.push(html);
    return token;
}

function renderInlineMarkdown(value, { allowLinks = true } = {}) {
    let text = String(value ?? "");
    if (!text) return "";
    const tokens = [];

    text = text.replace(/`([^`\n]+)`/g, (_match, code) => createMarkdownToken(tokens, `<code>${esc(code)}</code>`));
    if (allowLinks) {
        text = text.replace(/\[([^\]]+)\]\(([^)\s]+(?:\s+"[^"]*")?)\)/g, (_match, label, target) => {
            const href = String(target || "").replace(/\s+"[^"]*"$/, "");
            return createMarkdownToken(
                tokens,
                `<a href="${safeHref(href)}" target="_blank" rel="noreferrer noopener">${renderInlineMarkdown(label, { allowLinks: false })}</a>`
            );
        });
    }

    text = esc(text);
    text = text.replace(/\*\*([^*][\s\S]*?)\*\*/g, "<strong>$1</strong>");
    text = text.replace(/__([^_][\s\S]*?)__/g, "<strong>$1</strong>");
    text = text.replace(/~~([^~][\s\S]*?)~~/g, "<del>$1</del>");
    text = text.replace(/(^|[\s(])\*([^*\n][^*\n]*?)\*(?=($|[\s).,!?:;]))/g, "$1<em>$2</em>");
    text = text.replace(/(^|[\s(])_([^_\n][^_\n]*?)_(?=($|[\s).,!?:;]))/g, "$1<em>$2</em>");

    return text.replace(new RegExp(`${MD_TOKEN_MARKER}(\\d+)${MD_TOKEN_MARKER}`, "g"), (_match, index) => tokens[Number(index)] || "");
}

function isMarkdownTableSeparator(line) {
    return /^\s*\|?(?:\s*:?-{3,}:?\s*\|)+(?:\s*:?-{3,}:?\s*)?\|?\s*$/.test(line);
}

function splitMarkdownTableCells(line) {
    let row = String(line || "").trim();
    if (row.startsWith("|")) row = row.slice(1);
    if (row.endsWith("|")) row = row.slice(0, -1);
    return row.split("|").map((cell) => cell.trim());
}

function isMarkdownBlockStart(lines, index) {
    const line = String(lines[index] || "");
    if (!line.trim()) return false;
    if (/^```/.test(line)) return true;
    if (/^ {0,3}(#{1,6})\s+/.test(line)) return true;
    if (/^ {0,3}([-*_]\s*){3,}$/.test(line)) return true;
    if (/^ {0,3}> ?/.test(line)) return true;
    if (/^\s*[-*+]\s+/.test(line)) return true;
    if (/^\s*\d+\.\s+/.test(line)) return true;
    if (line.includes("|") && lines[index + 1] && isMarkdownTableSeparator(lines[index + 1])) return true;
    return false;
}

function renderMarkdownBlocks(value) {
    const text = String(value ?? "").replace(/\r\n?/g, "\n");
    const lines = text.split("\n");
    const blocks = [];

    for (let index = 0; index < lines.length;) {
        const line = String(lines[index] || "");
        if (!line.trim()) {
            index += 1;
            continue;
        }

        const fenceMatch = line.match(/^```([\w-]+)?\s*$/);
        if (fenceMatch) {
            const codeLines = [];
            const lang = String(fenceMatch[1] || "").trim();
            index += 1;
            while (index < lines.length && !/^```/.test(lines[index])) {
                codeLines.push(lines[index]);
                index += 1;
            }
            if (index < lines.length && /^```/.test(lines[index])) index += 1;
            const langClass = lang ? ` class="language-${esc(lang)}"` : "";
            blocks.push(`<pre><code${langClass}>${esc(codeLines.join("\n"))}</code></pre>`);
            continue;
        }

        const headingMatch = line.match(/^ {0,3}(#{1,6})\s+(.*)$/);
        if (headingMatch) {
            const level = headingMatch[1].length;
            blocks.push(`<h${level}>${renderInlineMarkdown(headingMatch[2])}</h${level}>`);
            index += 1;
            continue;
        }

        if (/^ {0,3}([-*_]\s*){3,}$/.test(line)) {
            blocks.push("<hr>");
            index += 1;
            continue;
        }

        if (line.includes("|") && lines[index + 1] && isMarkdownTableSeparator(lines[index + 1])) {
            const headerCells = splitMarkdownTableCells(line);
            const bodyRows = [];
            index += 2;
            while (index < lines.length && String(lines[index] || "").trim().includes("|")) {
                bodyRows.push(splitMarkdownTableCells(lines[index]));
                index += 1;
            }
            const headerHtml = headerCells.map((cell) => `<th>${renderInlineMarkdown(cell)}</th>`).join("");
            const bodyHtml = bodyRows.map((cells) => `<tr>${cells.map((cell) => `<td>${renderInlineMarkdown(cell)}</td>`).join("")}</tr>`).join("");
            blocks.push(`<table><thead><tr>${headerHtml}</tr></thead>${bodyHtml ? `<tbody>${bodyHtml}</tbody>` : ""}</table>`);
            continue;
        }

        if (/^ {0,3}> ?/.test(line)) {
            const quoteLines = [];
            while (index < lines.length) {
                const current = String(lines[index] || "");
                if (!current.trim()) {
                    quoteLines.push("");
                    index += 1;
                    continue;
                }
                if (!/^ {0,3}> ?/.test(current)) break;
                quoteLines.push(current.replace(/^ {0,3}> ?/, ""));
                index += 1;
            }
            blocks.push(`<blockquote>${renderMarkdownBlocks(quoteLines.join("\n")).join("")}</blockquote>`);
            continue;
        }

        if (/^\s*\d+\.\s+/.test(line)) {
            const items = [];
            while (index < lines.length) {
                const current = String(lines[index] || "");
                const match = current.match(/^\s*\d+\.\s+(.*)$/);
                if (!match) break;
                items.push(`<li>${renderInlineMarkdown(match[1])}</li>`);
                index += 1;
            }
            blocks.push(`<ol>${items.join("")}</ol>`);
            continue;
        }

        if (/^\s*[-*+]\s+/.test(line)) {
            const items = [];
            while (index < lines.length) {
                const current = String(lines[index] || "");
                const match = current.match(/^\s*[-*+]\s+(.*)$/);
                if (!match) break;
                items.push(`<li>${renderInlineMarkdown(match[1])}</li>`);
                index += 1;
            }
            blocks.push(`<ul>${items.join("")}</ul>`);
            continue;
        }

        const paragraphLines = [];
        while (index < lines.length) {
            const current = String(lines[index] || "");
            if (!current.trim()) break;
            if (paragraphLines.length && isMarkdownBlockStart(lines, index)) break;
            paragraphLines.push(current.trimEnd());
            index += 1;
        }
        blocks.push(`<p>${renderInlineMarkdown(paragraphLines.join("\n")).replace(/\n/g, "<br>")}</p>`);
    }

    return blocks;
}

function renderMarkdown(value) {
    const blocks = renderMarkdownBlocks(value);
    return blocks.length ? blocks.join("") : "<p>Done.</p>";
}

function hint(text, err = false) {
    U.modelHint.textContent = text;
    U.modelHint.style.color = err ? "var(--danger, #ff6b6b)" : "";
}

function addMsg(text, role, { markdown = false } = {}) {
    const el = document.createElement("div");
    el.className = `message ${role}`;
    const contentClass = markdown ? "msg-content markdown-content" : "msg-content";
    const content = markdown ? renderMarkdown(text) : esc(text);
    el.innerHTML = `<div class="avatar"><i data-lucide="${role === "system" ? "cpu" : "user"}"></i></div><div class="${contentClass}">${content}</div>`;
    U.ceoFeed.appendChild(el);
    icons();
    U.ceoFeed.scrollTop = U.ceoFeed.scrollHeight;
}

function resetCeoFeed() {
    if (!U.ceoFeed) return;
    U.ceoFeed.innerHTML = "";
    S.ceoPendingTurns = [];
}

function renderCeoSnapshot(messages = []) {
    resetCeoFeed();
    messages.forEach((item) => {
        const role = String(item?.role || "").trim().toLowerCase();
        const content = String(item?.content || "").trim();
        if (!content) return;
        if (role === "user") {
            addMsg(content, "user");
            return;
        }
        if (role === "assistant" || role === "system") {
            addMsg(content, "system", { markdown: true });
        }
    });
}

function scrollCeoFeedToBottom() {
    if (!U.ceoFeed) return;
    U.ceoFeed.scrollTop = U.ceoFeed.scrollHeight;
}

function createPendingCeoTurn() {
    if (!U.ceoFeed) return null;
    const el = document.createElement("div");
    el.className = "message system ceo-turn-message";
    el.innerHTML = `
        <div class="avatar"><i data-lucide="cpu"></i></div>
        <div class="msg-content ceo-turn-content">
            <div class="assistant-text pending">Working...</div>
            <details class="interaction-flow" open hidden>
                <summary class="interaction-flow-summary">
                    <span class="interaction-flow-title">Interaction Flow</span>
                    <span class="interaction-flow-meta">Waiting for tool calls...</span>
                </summary>
                <div class="interaction-flow-list" role="list"></div>
            </details>
        </div>
    `;
    U.ceoFeed.appendChild(el);
    const turn = {
        el,
        textEl: el.querySelector(".assistant-text"),
        flowEl: el.querySelector(".interaction-flow"),
        metaEl: el.querySelector(".interaction-flow-meta"),
        listEl: el.querySelector(".interaction-flow-list"),
        steps: 0,
        hasError: false,
        finalized: false,
    };
    icons();
    scrollCeoFeedToBottom();
    return turn;
}

function getActiveCeoTurn() {
    return S.ceoPendingTurns.find((turn) => !turn.finalized) || null;
}

function ensureActiveCeoTurn() {
    const existing = getActiveCeoTurn();
    if (existing) return existing;
    const created = createPendingCeoTurn();
    if (created) S.ceoPendingTurns.push(created);
    return created;
}

function updateCeoTurnMeta(turn, stateLabel) {
    if (!turn?.metaEl) return;
    const stepLabel = turn.steps > 0 ? `${turn.steps} steps` : "Waiting for tool calls...";
    turn.metaEl.textContent = stateLabel ? `${stepLabel} - ${stateLabel}` : stepLabel;
}

function appendCeoToolEvent(event = {}) {
    const turn = ensureActiveCeoTurn();
    if (!turn?.listEl || !turn.flowEl) return;
    const status = String(event.status || "running").trim().toLowerCase();
    const toolName = String(event.tool_name || "tool").trim() || "tool";
    const detail = String(event.text || "").trim();
    const statusLabel = ({ running: "Running", success: "Done", error: "Error" })[status] || "Update";
    const item = document.createElement("div");
    item.className = `interaction-step ${status}`;
    item.setAttribute("role", "listitem");
    item.innerHTML = `
        <div class="interaction-step-header">
            <span class="interaction-step-title">${esc(toolName)}</span>
            <span class="interaction-step-status">${esc(statusLabel)}</span>
        </div>
        <div class="interaction-step-detail">${esc(detail || `${toolName} ${statusLabel}`)}</div>
    `;
    turn.flowEl.hidden = false;
    turn.flowEl.open = true;
    turn.listEl.appendChild(item);
    turn.steps += 1;
    turn.hasError = turn.hasError || status === "error";
    updateCeoTurnMeta(turn, status === "running" ? "In progress" : status === "error" ? "Has errors" : "Processing");
    icons();
    scrollCeoFeedToBottom();
}

function finalizeCeoTurn(text) {
    const turn = S.ceoPendingTurns.shift();
    if (!turn?.textEl || !turn.flowEl) {
        addMsg(text, "system", { markdown: true });
        return;
    }
    turn.finalized = true;
    turn.textEl.innerHTML = renderMarkdown(String(text || "").trim() || "Done.");
    turn.textEl.classList.remove("pending");
    turn.textEl.classList.add("markdown-content");
    if (turn.steps > 0) {
        turn.flowEl.hidden = false;
        turn.flowEl.open = false;
        updateCeoTurnMeta(turn, turn.hasError ? "Completed with errors" : "Completed");
    } else {
        turn.flowEl.hidden = true;
    }
    scrollCeoFeedToBottom();
}

function addNotice(notice, _bump = true) {
    const payload = notice && typeof notice === "object" ? notice : {};
    const kind = String(payload.kind || "").toLowerCase();
    showToast({ title: payload.title || "Notice", text: payload.text || "", kind: kind.includes("fail") || kind.includes("error") ? "error" : "success" });
}


function clearToastTimers() {
    if (S.toastState.timeoutId) window.clearTimeout(S.toastState.timeoutId);
    if (S.toastState.intervalId) window.clearInterval(S.toastState.intervalId);
    S.toastState.timeoutId = null;
    S.toastState.intervalId = null;
}

function closeToast() {
    clearToastTimers();
    if (!U.toast) return;
    U.toast.hidden = true;
    U.toast.className = "app-toast";
    if (U.toastClose) U.toastClose.hidden = false;
    if (U.toastProgressBar) {
        U.toastProgressBar.className = "app-toast-progress-bar";
        U.toastProgressBar.style.transition = "none";
        U.toastProgressBar.style.transform = "scaleX(1)";
    }
}

function showToast({ title = "操作成功", text = "修改已生效", kind = "success", durationMs = 3000, persistent = false } = {}) {
    if (!U.toast || !U.toastTitle || !U.toastText || !U.toastProgress || !U.toastProgressBar || !U.toastClose) return;
    clearToastTimers();
    const sticky = persistent || durationMs <= 0;
    U.toastTitle.textContent = title;
    U.toastText.textContent = text;
    U.toast.hidden = false;
    U.toast.setAttribute("role", kind === "error" ? "alert" : "status");
    U.toastClose.hidden = false;
    U.toastProgress.hidden = false;
    U.toastProgressBar.className = "app-toast-progress-bar";
    U.toastProgressBar.style.transition = "none";
    U.toastProgressBar.style.transform = "scaleX(1)";
    if (sticky) {
        U.toastProgressBar.classList.add("is-indeterminate");
    } else {
        window.requestAnimationFrame(() => {
            window.requestAnimationFrame(() => {
                U.toastProgressBar.style.transition = `transform ${durationMs}ms linear`;
                U.toastProgressBar.style.transform = "scaleX(0)";
            });
        });
        S.toastState.timeoutId = window.setTimeout(closeToast, durationMs);
    }
    U.toast.className = `app-toast is-open is-${kind}`;
    icons();
}

function syncDetailSaveButton(kind) {
    const isSkill = kind === "skill";
    const root = isSkill ? U.skillDetail : U.toolDetail;
    const button = root?.querySelector(isSkill ? "#skill-modal-save" : "#tool-modal-save");
    const hint = root?.querySelector(".resource-draft-hint");
    const dirty = isSkill ? S.skillDirty : S.toolDirty;
    const busy = isSkill ? S.skillBusy : S.toolBusy;

    if (button) {
        button.textContent = busy ? "Saving..." : dirty ? "Save changes" : "Save";
        button.disabled = !!busy || !dirty;
    }
    if (hint) {
        hint.classList.toggle("is-dirty", dirty);
        hint.textContent = dirty ? "Changes are staged. Click Save to apply them." : "";
        hint.hidden = !dirty;
    }
}

function setSkillDirty(next = true) {
    S.skillDirty = !!next;
    renderSkillActions();
    syncDetailSaveButton("skill");
}

function setToolDirty(next = true) {
    S.toolDirty = !!next;
    renderToolActions();
    syncDetailSaveButton("tool");
}

function openConfirm({ title, text, confirmLabel = "确认", confirmKind = "danger", onConfirm, returnFocus = null }) {
    S.confirmState = { onConfirm, returnFocus };
    U.confirmTitle.textContent = title;
    U.confirmText.textContent = text;
    U.confirmAccept.textContent = confirmLabel;
    U.confirmAccept.className = `toolbar-btn ${confirmKind}`;
    U.confirmBackdrop.hidden = false;
    U.confirmBackdrop.classList.add("is-open");
    window.requestAnimationFrame(() => U.confirmCancel?.focus());
}

function resourceSelectLabel(select) {
    const map = {
        "skill-risk-filter": "Skill risk filter",
        "skill-status-filter": "Skill status filter",
        "tool-status-filter": "Tool status filter",
        "tool-risk-filter": "Tool risk filter",
    };
    return map[String(select?.id || "").trim()] || "Resource filter";
}

function closeResourceSelects({ exceptId = "", restoreFocus = false } = {}) {
    const openShells = [...document.querySelectorAll(".resource-select-shell.is-open")];
    let closed = false;
    openShells.forEach((shell) => {
        const selectId = String(shell.dataset.selectId || "");
        if (exceptId && selectId === exceptId) return;
        const trigger = shell.querySelector(".resource-select-trigger");
        const menu = shell.querySelector(".resource-select-menu");
        shell.classList.remove("is-open");
        if (menu) menu.hidden = true;
        if (trigger) trigger.setAttribute("aria-expanded", "false");
        if (restoreFocus && trigger instanceof HTMLElement) trigger.focus();
        closed = true;
    });
    if (closed && (!exceptId || S.openResourceSelectId !== exceptId)) S.openResourceSelectId = exceptId || "";
    return closed;
}

function syncResourceSelectUI(select) {
    if (!(select instanceof HTMLSelectElement)) return;
    const shell = select.closest(".resource-select-shell");
    if (!shell) return;
    const trigger = shell.querySelector(".resource-select-trigger");
    const valueEl = shell.querySelector(".resource-select-value");
    const optionButtons = [...shell.querySelectorAll(".resource-select-option")];
    const selectedOption = select.selectedOptions?.[0] || [...select.options].find((option) => option.value === select.value) || select.options[0];
    const selectedValue = String(selectedOption?.value ?? "");

    if (valueEl) valueEl.textContent = String(selectedOption?.textContent || "").trim();
    if (trigger) {
        trigger.dataset.value = selectedValue;
        trigger.setAttribute("aria-label", `${resourceSelectLabel(select)}: ${String(selectedOption?.textContent || "").trim()}`);
    }
    optionButtons.forEach((button) => {
        const isSelected = String(button.dataset.value || "") === selectedValue;
        button.classList.toggle("is-selected", isSelected);
        button.setAttribute("aria-selected", isSelected ? "true" : "false");
        button.tabIndex = isSelected ? 0 : -1;
    });
}

function focusResourceSelectOption(shell, direction = "selected") {
    if (!(shell instanceof HTMLElement)) return;
    const options = [...shell.querySelectorAll(".resource-select-option")];
    if (!options.length) return;
    const active = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const currentIndex = active ? options.indexOf(active) : -1;
    let nextIndex = options.findIndex((option) => option.classList.contains("is-selected"));
    if (direction === "first") nextIndex = 0;
    else if (direction === "last") nextIndex = options.length - 1;
    else if (direction === "next") nextIndex = currentIndex >= 0 ? Math.min(options.length - 1, currentIndex + 1) : Math.max(0, nextIndex);
    else if (direction === "prev") nextIndex = currentIndex >= 0 ? Math.max(0, currentIndex - 1) : Math.max(0, nextIndex);
    if (nextIndex < 0) nextIndex = 0;
    options[nextIndex]?.focus();
}

function setResourceSelectValue(select, value, { close = true } = {}) {
    if (!(select instanceof HTMLSelectElement)) return;
    const nextValue = String(value ?? "");
    if (select.value !== nextValue) {
        select.value = nextValue;
        syncResourceSelectUI(select);
        select.dispatchEvent(new Event("change", { bubbles: true }));
    } else {
        syncResourceSelectUI(select);
    }
    if (close) closeResourceSelects({ restoreFocus: true });
}

function openResourceSelect(select, { focus = "selected" } = {}) {
    if (!(select instanceof HTMLSelectElement)) return;
    const shell = select.closest(".resource-select-shell");
    if (!shell) return;
    const trigger = shell.querySelector(".resource-select-trigger");
    const menu = shell.querySelector(".resource-select-menu");
    closeResourceSelects({ exceptId: select.id });
    shell.classList.add("is-open");
    if (menu) menu.hidden = false;
    if (trigger) trigger.setAttribute("aria-expanded", "true");
    S.openResourceSelectId = select.id;
    focusResourceSelectOption(shell, focus);
}

function buildResourceSelect(select) {
    if (!(select instanceof HTMLSelectElement) || select.dataset.customized === "true") return;
    const parent = select.parentElement;
    if (!parent) return;

    const shell = document.createElement("div");
    shell.className = "resource-select-shell";
    shell.dataset.selectId = select.id || `resource-select-${Math.random().toString(36).slice(2, 8)}`;

    const trigger = document.createElement("button");
    trigger.type = "button";
    trigger.className = "resource-select-trigger";
    trigger.setAttribute("aria-haspopup", "listbox");
    trigger.setAttribute("aria-expanded", "false");
    trigger.setAttribute("aria-controls", `${shell.dataset.selectId}-menu`);

    const valueEl = document.createElement("span");
    valueEl.className = "resource-select-value";
    const iconEl = document.createElement("span");
    iconEl.className = "resource-select-icon";
    iconEl.setAttribute("aria-hidden", "true");
    iconEl.innerHTML = `
        <svg viewBox="0 0 18 18" focusable="false" aria-hidden="true">
            <path d="M4.5 6.75L9 11.25L13.5 6.75" />
        </svg>
    `;
    trigger.append(valueEl, iconEl);

    const menu = document.createElement("div");
    menu.className = "resource-select-menu";
    menu.id = `${shell.dataset.selectId}-menu`;
    menu.setAttribute("role", "listbox");
    menu.hidden = true;
    menu.setAttribute("aria-label", resourceSelectLabel(select));

    [...select.options].forEach((option) => {
        const optionButton = document.createElement("button");
        optionButton.type = "button";
        optionButton.className = "resource-select-option";
        optionButton.dataset.value = option.value;
        optionButton.setAttribute("role", "option");
        optionButton.tabIndex = -1;

        const label = document.createElement("span");
        label.className = "resource-select-option-label";
        label.textContent = String(option.textContent || "").trim();

        const check = document.createElement("span");
        check.className = "resource-select-option-check";
        check.textContent = "✓";
        check.setAttribute("aria-hidden", "true");

        optionButton.append(label, check);
        optionButton.addEventListener("click", () => setResourceSelectValue(select, option.value));
        optionButton.addEventListener("keydown", (e) => {
            if (e.key === "ArrowDown") {
                e.preventDefault();
                focusResourceSelectOption(shell, "next");
            }
            if (e.key === "ArrowUp") {
                e.preventDefault();
                focusResourceSelectOption(shell, "prev");
            }
            if (e.key === "Home") {
                e.preventDefault();
                focusResourceSelectOption(shell, "first");
            }
            if (e.key === "End") {
                e.preventDefault();
                focusResourceSelectOption(shell, "last");
            }
            if (e.key === "Enter" || e.key === " ") {
                e.preventDefault();
                setResourceSelectValue(select, option.value);
            }
            if (e.key === "Escape") {
                e.preventDefault();
                closeResourceSelects({ restoreFocus: true });
            }
            if (e.key === "Tab") closeResourceSelects();
        });
        menu.appendChild(optionButton);
    });

    trigger.addEventListener("click", () => {
        const isOpen = shell.classList.contains("is-open");
        if (isOpen) closeResourceSelects({ restoreFocus: true });
        else openResourceSelect(select);
    });
    trigger.addEventListener("keydown", (e) => {
        if (e.key === "ArrowDown") {
            e.preventDefault();
            openResourceSelect(select, { focus: "first" });
        }
        if (e.key === "ArrowUp") {
            e.preventDefault();
            openResourceSelect(select, { focus: "last" });
        }
        if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            const isOpen = shell.classList.contains("is-open");
            if (isOpen) closeResourceSelects({ restoreFocus: true });
            else openResourceSelect(select);
        }
        if (e.key === "Escape") {
            e.preventDefault();
            closeResourceSelects({ restoreFocus: true });
        }
    });

    select.classList.add("resource-select-native");
    select.dataset.customized = "true";
    select.tabIndex = -1;
    select.setAttribute("aria-hidden", "true");
    select.addEventListener("change", () => syncResourceSelectUI(select));

    parent.insertBefore(shell, select);
    shell.append(select, trigger, menu);
    syncResourceSelectUI(select);
}

function enhanceResourceSelects() {
    document.querySelectorAll("select.resource-select").forEach((select) => buildResourceSelect(select));
}

function closeConfirm({ restoreFocus = true } = {}) {
    const returnFocus = S.confirmState?.returnFocus;
    S.confirmState = null;
    U.confirmBackdrop.hidden = true;
    U.confirmBackdrop.classList.remove("is-open");
    if (restoreFocus) returnFocus?.focus?.();
}

async function acceptConfirm() {
    if (!S.confirmState?.onConfirm) return;
    U.confirmAccept.disabled = true;
    U.confirmCancel.disabled = true;
    try {
        await S.confirmState.onConfirm();
        closeConfirm();
    } finally {
        U.confirmAccept.disabled = false;
        U.confirmCancel.disabled = false;
    }
}

function modelScopeLabel(scope) {
    return (MODEL_SCOPES.find((item) => item.key === scope) || { label: String(scope || "") }).label;
}

function modelRefItem(ref) {
    const raw = String(ref || "").trim();
    if (!raw) return null;
    return S.modelCatalog.catalog.find((item) => String(item.key || "").trim() === raw || String(item.provider_model || "").trim() === raw) || null;
}

function modelRefEquivalent(left, right) {
    const leftRaw = String(left || "").trim();
    const rightRaw = String(right || "").trim();
    if (!leftRaw || !rightRaw) return false;
    if (leftRaw === rightRaw) return true;
    const leftItem = modelRefItem(leftRaw);
    const rightItem = modelRefItem(rightRaw);
    if (leftItem && rightItem) return String(leftItem.key || "") === String(rightItem.key || "");
    if (leftItem) return rightRaw === String(leftItem.key || "") || rightRaw === String(leftItem.provider_model || "");
    if (rightItem) return leftRaw === String(rightItem.key || "") || leftRaw === String(rightItem.provider_model || "");
    return false;
}

function activeModelRoles() {
    return S.modelCatalog.roleEditing ? S.modelCatalog.roleDrafts : S.modelCatalog.roles;
}

function modelScopeChain(scope, source = "active") {
    const roles = source === "draft"
        ? S.modelCatalog.roleDrafts
        : source === "committed"
            ? S.modelCatalog.roles
            : activeModelRoles();
    return Array.isArray(roles?.[scope]) ? [...roles[scope]] : [];
}

function modelScopeContains(scope, ref, source = "active") {
    return modelScopeChain(scope, source).some((item) => modelRefEquivalent(item, ref));
}

function normalizeModelRoleChain(refs) {
    const normalized = [];
    (refs || []).forEach((ref) => {
        const raw = String(ref || "").trim();
        if (!raw) return;
        const item = modelRefItem(raw);
        const target = String(item?.key || raw).trim();
        if (!target || normalized.some((existing) => modelRefEquivalent(existing, target))) return;
        normalized.push(target);
    });
    return normalized;
}

function normalizeAllModelRoles(roles = EMPTY_MODEL_ROLES()) {
    const next = EMPTY_MODEL_ROLES();
    MODEL_SCOPES.forEach(({ key }) => {
        next[key] = normalizeModelRoleChain(Array.isArray(roles?.[key]) ? roles[key] : []);
    });
    return next;
}

function modelRolesEqual(left, right) {
    return MODEL_SCOPES.every(({ key }) => {
        const leftChain = normalizeModelRoleChain(left?.[key] || []);
        const rightChain = normalizeModelRoleChain(right?.[key] || []);
        if (leftChain.length !== rightChain.length) return false;
        return leftChain.every((item, index) => modelRefEquivalent(item, rightChain[index]));
    });
}

function syncModelRoleDraftState() {
    S.modelCatalog.rolesDirty = !!S.modelCatalog.roleEditing && !modelRolesEqual(S.modelCatalog.roleDrafts, S.modelCatalog.roles);
}

function modelCatalogHeadersKey(headers) {
    if (!headers || typeof headers !== "object" || Array.isArray(headers)) return "";
    return Object.entries(headers)
        .map(([key, value]) => [String(key), String(value)])
        .sort(([left], [right]) => left.localeCompare(right))
        .map(([key, value]) => `${key}:${value}`)
        .join("\n");
}

function modelCatalogSignature(item) {
    if (!item || typeof item !== "object") return "";
    return [
        String(item.provider_model || "").trim(),
        String(item.api_key || "").trim(),
        String(item.api_base || "").trim(),
        modelCatalogHeadersKey(item.extra_headers),
    ].join("\n");
}

function remapModelRef(ref, aliasMap = {}) {
    const raw = String(ref || "").trim();
    if (!raw) return "";
    return String(aliasMap[raw] || raw).trim();
}

function remapModelRefs(refs, aliasMap = {}) {
    const normalized = [];
    (refs || []).forEach((ref) => {
        const target = remapModelRef(ref, aliasMap);
        if (!target || normalized.some((item) => item === target)) return;
        normalized.push(target);
    });
    return normalized;
}

function normalizeCatalogEntries(items) {
    const catalog = [];
    const aliasMap = {};
    const signatureToKey = new Map();
    (items || []).forEach((item) => {
        if (!item || typeof item !== "object") return;
        const key = String(item.key || "").trim();
        const providerModel = String(item.provider_model || "").trim();
        const signature = modelCatalogSignature(item) || key || providerModel;
        if (!signature) return;
        const canonicalKey = signatureToKey.get(signature);
        if (canonicalKey) {
            if (key && key !== canonicalKey) aliasMap[key] = canonicalKey;
            return;
        }
        const nextKey = key || providerModel;
        if (!nextKey) return;
        signatureToKey.set(signature, nextKey);
        catalog.push({ ...item, key: nextKey });
    });
    return { catalog, aliasMap };
}

function applyModelCatalog(data, { preserveRoleDrafts = false } = {}) {
    const payload = data && typeof data === "object" ? data : {};
    const { catalog, aliasMap } = normalizeCatalogEntries(Array.isArray(payload.catalog) ? payload.catalog : []);
    const rolesPayload = payload.roles && typeof payload.roles === "object" ? payload.roles : {};
    const nextRoles = EMPTY_MODEL_ROLES();
    MODEL_SCOPES.forEach(({ key }) => {
        nextRoles[key] = remapModelRefs(
            Array.isArray(rolesPayload[key])
                ? rolesPayload[key].map((item) => String(item || "").trim()).filter(Boolean)
                : [],
            aliasMap,
        );
    });
    S.modelCatalog.items = Array.isArray(payload.items)
        ? remapModelRefs(payload.items.map((item) => String(item || "").trim()).filter(Boolean), aliasMap)
        : [];
    S.modelCatalog.catalog = catalog;
    S.modelCatalog.roles = normalizeAllModelRoles(nextRoles);
    if (preserveRoleDrafts && S.modelCatalog.roleEditing) {
        S.modelCatalog.roleDrafts = normalizeAllModelRoles(S.modelCatalog.roleDrafts);
        syncModelRoleDraftState();
    } else {
        S.modelCatalog.roleDrafts = cloneModelRoles(S.modelCatalog.roles);
        S.modelCatalog.roleEditing = false;
        S.modelCatalog.rolesDirty = false;
    }
    S.modelCatalog.defaults = { ...DEFAULT_MODEL_DEFAULTS(), ...(payload.defaults || {}) };
    if (S.modelCatalog.mode !== "create") {
        const selectedKey = remapModelRef(S.modelCatalog.selectedModelKey, aliasMap);
        S.modelCatalog.selectedModelKey = selectedKey;
        if (selectedKey && !S.modelCatalog.catalog.some((item) => String(item.key || "").trim() === selectedKey)) {
            S.modelCatalog.selectedModelKey = "";
        }
    }
}

function filterModels() {
    const q = String(S.modelCatalog.search || "").trim().toLowerCase();
    if (!q) return [...S.modelCatalog.catalog];
    return S.modelCatalog.catalog.filter((item) => [item.key, item.provider_model, item.description].join("\n").toLowerCase().includes(q));
}

function syncModelDetailScopeToggles() {
    if (!U.modelDetail || S.modelCatalog.mode === "create") return;
    const selectedKey = String(S.modelCatalog.selectedModelKey || "").trim();
    if (!selectedKey) return;
    MODEL_SCOPES.forEach(({ key }) => {
        const input = U.modelDetail.querySelector(`[name="scope_${key}"]`);
        if (!(input instanceof HTMLInputElement)) return;
        const checked = modelScopeContains(key, selectedKey);
        input.checked = checked;
        input.closest(".role-toggle")?.classList.toggle("checked", checked);
    });
}

function renderModelHint() {
    if (S.modelCatalog.loading) return hint("正在加载模型配置...");
    if (S.modelCatalog.saving) return hint("正在保存...");
    if (S.modelCatalog.error) return hint(`模型配置错误：${S.modelCatalog.error}`, true);
    if (!S.modelCatalog.catalog.length) return hint("当前还没有模型，请先添加模型。", false);
    if (S.modelCatalog.roleEditing && S.modelCatalog.rolesDirty) return hint("正在修改模型链，请点击“保存”应用修改。", false);
    if (S.modelCatalog.roleEditing) return hint("已进入模型链编辑模式，可拖动、移除或加入模型后再点击“保存”。", false);
    return hint("点击“修改模型链”后再调整角色链；点击模型可打开配置弹窗。", false);
}

function renderModelRoleEditors() {
    if (!U.modelRoleEditors) return;
    const editing = !!S.modelCatalog.roleEditing;
    U.modelRoleEditors.innerHTML = MODEL_SCOPES.map((scope) => {
        const chain = modelScopeChain(scope.key);
        const defaultText = S.modelCatalog.defaults[scope.key]
            ? `当前首选 ${S.modelCatalog.defaults[scope.key]}`
            : "未配置当前首选";
        const chainMarkup = chain.length
            ? chain.map((ref, index) => {
                const item = modelRefItem(ref);
                const modelKey = String(item?.key || ref).trim();
                const badges = [index === 0 ? '<span class="policy-chip risk-low">首选</span>' : ""];
                if (item?.enabled === false) badges.push('<span class="policy-chip neutral">已禁用</span>');
                if (!item) badges.push('<span class="policy-chip neutral">未托管</span>');
                return `
                    <article class="model-chain-slide${editing ? " is-editing" : ""}"${editing ? ' draggable="true"' : ''} data-model-chain-ref="${esc(modelKey)}" data-scope="${scope.key}">
                        ${editing ? '<button type="button" class="model-chain-handle" aria-label="拖动调整顺序"><span class="model-chain-grip" aria-hidden="true">&#9776;</span></button>' : ''}
                        <button type="button" class="model-chain-main" data-model-open="${esc(modelKey)}">
                            <span class="resource-list-title">${esc(modelKey)}</span>
                            <span class="resource-list-subtitle">${esc(item?.provider_model || ref)}</span>
                            <span class="model-inline-meta">${badges.join("")}</span>
                        </button>
                        ${editing ? `<button type="button" class="toolbar-btn ghost small" data-model-chain-action="remove" data-scope="${scope.key}" data-index="${index}">移除</button>` : ''}
                    </article>`;
            }).join("")
            : `<div class="empty-state compact">${editing ? '从下方共享模型列表拖入，构建当前角色链。' : '点击“修改模型链”后再调整当前角色链。'}</div>`;

        return `
            <section class="model-chain-card">
                <div class="panel-header">
                    <div>
                        <h3>${esc(scope.label)}</h3>
                        <p class="subtitle">${esc(defaultText)}</p>
                    </div>
                    <span class="policy-chip neutral">${chain.length} 个候选</span>
                </div>
                <div class="model-role-section">
                    <div class="model-role-section-title">当前角色链</div>
                    <div class="model-chain-list" data-model-chain-list="${scope.key}">${chainMarkup}</div>
                </div>
            </section>`;
    }).join("");
}

function renderModelList() {
    if (!U.modelList) return;
    const editing = !!S.modelCatalog.roleEditing;
    const catalog = filterModels().sort((left, right) => String(left.key || "").localeCompare(String(right.key || "")));
    if (!catalog.length) {
        const emptyText = S.modelCatalog.search
            ? "没有匹配的模型，请调整搜索条件。"
            : "暂无可用模型，请先添加模型。";
        U.modelList.innerHTML = `<div class="empty-state compact">${emptyText}</div>`;
        return;
    }
    U.modelList.innerHTML = catalog.map((item) => {
        const usedScopes = MODEL_SCOPES.filter((scope) => modelScopeContains(scope.key, item.key));
        const usageMarkup = usedScopes.length
            ? '<span class="policy-chip neutral">已加入角色链</span>'
            : '<span class="policy-chip neutral">未加入角色链</span>';
        const stateChips = [usageMarkup];
        if (item.enabled === false) stateChips.push('<span class="policy-chip neutral">已禁用</span>');
        return `
            <article class="model-available-item ${usedScopes.length ? "is-in-chain" : ""}"${editing ? ' draggable="true"' : ''} data-model-available-key="${esc(item.key)}">
                <div class="model-shared-item-head">
                    <button type="button" class="model-available-main" data-model-open="${esc(item.key)}">
                        <span class="resource-list-title">${esc(item.key)}</span>
                        <span class="resource-list-subtitle">${esc(item.provider_model)}</span>
                    </button>
                    <button type="button" class="toolbar-btn ghost small" data-model-open="${esc(item.key)}">配置</button>
                </div>
                <div class="model-inline-meta">${stateChips.join("")}</div>
                <div class="resource-empty-copy">${editing ? '拖动当前模型到上方任意角色链即可加入。' : '点击“修改模型链”后，可拖动当前模型到任意角色链。'}</div>
            </article>`;
    }).join("");
}

function renderModelDetail() {
    if (!U.modelDetail || !U.modelDetailEmpty) return;
    const isCreate = S.modelCatalog.mode === "create";
    const current = isCreate ? null : modelRefItem(S.modelCatalog.selectedModelKey);
    if (!isCreate && !current) {
        U.modelDetailEmpty.style.display = "none";
        U.modelDetail.innerHTML = "";
        setDrawerOpen(U.modelBackdrop, U.modelDrawer, false);
        return;
    }

    const enabled = isCreate ? true : !!current?.enabled;

    U.modelDetailEmpty.style.display = "none";
    setDrawerOpen(U.modelBackdrop, U.modelDrawer, true);
    U.modelDetail.innerHTML = `
        <article class="model-detail-card model-config-shell">
            <div class="detail-modal-header model-config-header">
                <div class="detail-modal-title">
                    <h2 id="model-detail-title">${isCreate ? "添加模型" : "模型配置"}</h2>
                    <p class="subtitle">${esc(isCreate ? "填写必填项后写入 .g3ku/config.json" : `${current.key} · ${current.provider_model}`)}</p>
                </div>
                <div class="detail-modal-actions">
                    <span class="policy-chip ${enabled ? "risk-low" : "neutral"}">${enabled ? "已启用" : "已禁用"}</span>
                    <button type="submit" form="model-detail-form" class="toolbar-btn success">保存</button>
                    <button type="button" class="toolbar-btn ghost" data-model-detail-cancel="1" data-modal-close>关闭</button>
                </div>
            </div>
            <div class="detail-modal-body model-config-body">
                <form id="model-detail-form" class="model-detail-form" data-mode="${isCreate ? "create" : "edit"}" data-model-key="${esc(current?.key || "")}">
                    <section class="resource-section">
                        <h3>基本信息</h3>
                        <div class="model-form-grid">
                            <label class="resource-field">
                                <span class="resource-field-label">模型 Key *</span>
                                <input class="resource-search" name="key" ${isCreate ? `value=""` : `value="${esc(current.key)}" disabled`} placeholder="如 openai_primary">
                            </label>
                            <label class="resource-field">
                                <span class="resource-field-label">Provider / Model *</span>
                                <input class="resource-search" name="providerModel" value="${esc(current?.provider_model || "")}" placeholder="如 openai:gpt-4.1">
                            </label>
                            <label class="resource-field">
                                <span class="resource-field-label">API Key *</span>
                                <input class="resource-search" name="apiKey" value="${esc(current?.api_key || "")}" placeholder="sk-...">
                            </label>
                            <label class="resource-field">
                                <span class="resource-field-label">Base URL ${isCreate ? "*" : ""}</span>
                                <input class="resource-search" name="apiBase" value="${esc(current?.api_base || "")}" placeholder="https://api.example.com/v1">
                            </label>
                        </div>
                        <div class="model-form-status-area" style="margin-top: var(--space-4);">
                            ${enabled 
                                ? `<button type="button" class="toolbar-btn danger" data-model-control="disable" data-key="${esc(current?.key || "")}">禁用模型</button>` 
                                : `<button type="button" class="toolbar-btn success" data-model-control="enable" data-key="${esc(current?.key || "")}">启用模型</button>`
                            }
                            ${!isCreate ? `<button type="button" class="toolbar-btn ghost" data-model-control="delete" data-key="${esc(current?.key || "")}">删除模型</button>` : ""}
                            <input type="checkbox" name="enabled" ${enabled ? "checked" : ""} style="display:none">
                        </div>
                    </section>
                    <section class="resource-section">
                        <h3>模型参数</h3>
                        <div class="model-form-grid">
                            <label class="resource-field">
                                <span class="resource-field-label">Max Tokens</span>
                                <input class="resource-search" type="number" min="1" step="1" name="maxTokens" value="${esc(String(current?.max_tokens ?? ""))}" placeholder="留空使用默认值">
                            </label>
                            <label class="resource-field">
                                <span class="resource-field-label">Temperature</span>
                                <input class="resource-search" type="number" min="0" max="2" step="0.1" name="temperature" value="${esc(String(current?.temperature ?? ""))}" placeholder="留空使用默认值">
                            </label>
                            <label class="resource-field">
                                <span class="resource-field-label">Reasoning Effort</span>
                                <input class="resource-search" name="reasoningEffort" value="${esc(current?.reasoning_effort || "")}" placeholder="如 low / medium / high">
                            </label>
                            <label class="resource-field">
                                <span class="resource-field-label">Retry On</span>
                                <input class="resource-search" name="retryOn" value="${esc((current?.retry_on || []).join(", "))}" placeholder="如 network, 429, 5xx">
                            </label>
                        </div>
                    </section>
                    <section class="resource-section">
                        <h3>额外请求头</h3>
                        <textarea class="resource-editor model-textarea" name="extraHeaders" rows="6" placeholder='{"X-Trace-Id": "demo"}'>${esc(current?.extra_headers ? JSON.stringify(current.extra_headers, null, 2) : "")}</textarea>
                    </section>
                    <section class="resource-section">
                        <h3>说明</h3>
                        <textarea class="resource-editor model-textarea" name="description" rows="5" placeholder="可填写用途、限制、成本等说明">${esc(current?.description || "")}</textarea>
                    </section>
                    <div class="model-actions">
                        <button type="submit" class="toolbar-btn success">${isCreate ? "添加并保存" : "保存模型"}</button>
                        <button type="button" class="toolbar-btn ghost" data-model-detail-cancel="1">${isCreate ? "取消" : "关闭"}</button>
                    </div>
                </form>
            </div>
        </article>`;
}

function renderModelCatalog() {
    if (U.modelRefresh) U.modelRefresh.disabled = S.modelCatalog.loading || S.modelCatalog.saving;
    if (U.modelCreate) U.modelCreate.disabled = S.modelCatalog.loading || S.modelCatalog.saving;
    if (U.modelRolesCancel) {
        U.modelRolesCancel.hidden = !S.modelCatalog.roleEditing;
        U.modelRolesCancel.disabled = S.modelCatalog.loading || S.modelCatalog.saving;
    }
    if (U.modelRolesSave) {
        U.modelRolesSave.disabled = S.modelCatalog.loading || S.modelCatalog.saving;
        U.modelRolesSave.textContent = S.modelCatalog.saving
            ? "正在保存..."
            : S.modelCatalog.roleEditing
                ? "保存"
                : "修改模型链";
    }
    renderModelHint();
    renderModelRoleEditors();
    renderModelList();
    renderModelDetail();
}

async function loadModels() {
    S.modelCatalog.loading = true;
    S.modelCatalog.error = "";
    renderModelCatalog();
    try {
        const data = await ApiClient.getOrgGraphModels();
        applyModelCatalog(data, { preserveRoleDrafts: !!S.modelCatalog.roleEditing });
    } catch (e) {
        S.modelCatalog.error = e.message || "load failed";
    } finally {
        S.modelCatalog.loading = false;
        renderModelCatalog();
    }
}

function openModel(key) {
    S.modelCatalog.mode = "view";
    S.modelCatalog.selectedModelKey = String(key || "").trim();
    renderModelCatalog();
}

function startCreateModel() {
    S.modelCatalog.mode = "create";
    S.modelCatalog.selectedModelKey = "";
    renderModelCatalog();
}

function clearModelSelection() {
    S.modelCatalog.mode = "view";
    S.modelCatalog.selectedModelKey = "";
    renderModelCatalog();
}


function clearModelDragDecorations() {
    [U.modelRoleEditors, U.modelList].filter(Boolean).forEach((root) => {
        root.querySelectorAll('.is-drop-target').forEach((item) => item.classList.remove('is-drop-target'));
        root.querySelectorAll('.is-drop-zone').forEach((item) => item.classList.remove('is-drop-zone'));
        root.querySelectorAll('[data-model-drop-placeholder]').forEach((item) => item.remove());
    });
}

function beginModelDrag(item, { scope = "", ref = "", source = "available" } = {}, dataTransfer = null) {
    const modelRef = String(ref || "").trim();
    if (!S.modelCatalog.roleEditing || !item || !modelRef) return false;
    S.modelCatalog.dragState = {
        scope: String(scope || ""),
        ref: modelRef,
        source,
        scrollFrameId: null,
        scrollTarget: null,
        scrollStep: 0,
    };
    item.classList.add("is-dragging");
    clearModelDragDecorations();
    if (dataTransfer) {
        dataTransfer.effectAllowed = source === "chain" ? "move" : "copyMove";
        dataTransfer.setData("text/plain", modelRef);
    }
    return true;
}

function finishModelDrag() {
    stopModelAutoScroll();
    [U.modelRoleEditors, U.modelList].filter(Boolean).forEach((root) => {
        root.querySelectorAll(".model-chain-slide.is-dragging, .model-available-item.is-dragging").forEach((item) => item.classList.remove("is-dragging"));
    });
    S.modelCatalog.dragState = null;
    clearModelDragDecorations();
}

function stopModelAutoScroll() {
    const dragState = S.modelCatalog.dragState;
    if (!dragState) return;
    if (dragState.scrollFrameId) window.cancelAnimationFrame(dragState.scrollFrameId);
    dragState.scrollFrameId = null;
    dragState.scrollTarget = null;
    dragState.scrollStep = 0;
}

function startModelAutoScroll(target, clientY) {
    const dragState = S.modelCatalog.dragState;
    if (!dragState || !target) return;
    const rect = target.getBoundingClientRect();
    const threshold = Math.min(48, rect.height / 4);
    let step = 0;
    if (clientY < rect.top + threshold) {
        step = -Math.max(6, Math.round((rect.top + threshold - clientY) / 5));
    } else if (clientY > rect.bottom - threshold) {
        step = Math.max(6, Math.round((clientY - (rect.bottom - threshold)) / 5));
    }
    if (!step) {
        if (dragState.scrollTarget === target) stopModelAutoScroll();
        return;
    }
    dragState.scrollTarget = target;
    dragState.scrollStep = step;
    if (dragState.scrollFrameId) return;
    const tick = () => {
        const state = S.modelCatalog.dragState;
        if (!state?.scrollTarget || !state.scrollStep) {
            stopModelAutoScroll();
            return;
        }
        state.scrollTarget.scrollTop += state.scrollStep;
        state.scrollFrameId = window.requestAnimationFrame(tick);
    };
    dragState.scrollFrameId = window.requestAnimationFrame(tick);
}

function ensureModelDropPlaceholder(list, targetItem, clientY) {
    if (!list) return null;
    const placeholder = document.createElement('div');
    placeholder.className = 'model-chain-drop-placeholder';
    placeholder.dataset.modelDropPlaceholder = '1';
    list.classList.add('is-drop-zone');
    if (targetItem && targetItem.parentElement === list) {
        targetItem.classList.add('is-drop-target');
        const rect = targetItem.getBoundingClientRect();
        const insertBefore = clientY < rect.top + (rect.height / 2);
        placeholder.dataset.dropPosition = insertBefore ? 'before' : 'after';
        targetItem.dataset.dropPosition = insertBefore ? 'before' : 'after';
        list.insertBefore(placeholder, insertBefore ? targetItem : targetItem.nextSibling);
    } else {
        placeholder.dataset.dropPosition = 'append';
        list.appendChild(placeholder);
    }
    return placeholder;
}

function highlightModelAvailableZone(list, targetItem = null) {
    if (!list) return;
    list.classList.add('is-drop-zone');
    if (targetItem && targetItem.parentElement === list) {
        targetItem.classList.add('is-drop-target');
    }
}

function moveRoleChainItem(scope, fromRef, targetIndex = null) {
    const chain = modelScopeChain(scope);
    const sourceIndex = chain.findIndex((item) => modelRefEquivalent(item, fromRef));
    if (sourceIndex < 0) return;
    const nextChain = [...chain];
    const [moving] = nextChain.splice(sourceIndex, 1);
    const boundedIndex = targetIndex === null
        ? nextChain.length
        : Math.max(0, Math.min(Number(targetIndex), nextChain.length));
    nextChain.splice(boundedIndex, 0, moving);
    updateRoleChainDraft(scope, nextChain);
}

function insertRoleChainItem(scope, modelKey, targetIndex = null) {
    const nextChain = modelScopeChain(scope).filter((item) => !modelRefEquivalent(item, modelKey));
    const boundedIndex = targetIndex === null
        ? nextChain.length
        : Math.max(0, Math.min(Number(targetIndex), nextChain.length));
    nextChain.splice(boundedIndex, 0, modelKey);
    updateRoleChainDraft(scope, nextChain);
}

function removeRoleChainItem(scope, modelKey) {
    const nextChain = modelScopeChain(scope).filter((item) => !modelRefEquivalent(item, modelKey));
    updateRoleChainDraft(scope, nextChain);
}

function updateRoleChainDraft(scope, nextChain) {
    if (!S.modelCatalog.roleEditing) return;
    S.modelCatalog.roleDrafts[scope] = normalizeModelRoleChain(nextChain);
    syncModelRoleDraftState();
    renderModelHint();
    renderModelRoleEditors();
    renderModelList();
    syncModelDetailScopeToggles();
}

function startModelRoleEditing() {
    S.modelCatalog.roleEditing = true;
    S.modelCatalog.roleDrafts = cloneModelRoles(S.modelCatalog.roles);
    syncModelRoleDraftState();
    renderModelCatalog();
}

function cancelModelRoleEditing() {
    S.modelCatalog.roleEditing = false;
    S.modelCatalog.roleDrafts = cloneModelRoles(S.modelCatalog.roles);
    S.modelCatalog.rolesDirty = false;
    finishModelDrag();
    renderModelCatalog();
    hint("已取消模型链修改。", false);
}

async function persistModelRoleChains(scopes = MODEL_SCOPES.map((item) => item.key), successText = "模型链已保存。", { useDrafts = false } = {}) {
    const targets = [...new Set(scopes.map((item) => String(item || "").trim()).filter(Boolean))];
    if (!targets.length) return;
    const roleSource = useDrafts ? S.modelCatalog.roleDrafts : S.modelCatalog.roles;
    S.modelCatalog.saving = true;
    renderModelCatalog();
    try {
        let payload = null;
        for (const scope of targets) {
            payload = await ApiClient.updateModelRoleChain(scope, normalizeModelRoleChain(roleSource[scope] || []));
        }
        if (payload) applyModelCatalog(payload);
        hint(successText);
    } catch (e) {
        S.modelCatalog.error = e.message || "save failed";
        hint(`模型配置错误：${S.modelCatalog.error}`, true);
        throw e;
    } finally {
        S.modelCatalog.saving = false;
        renderModelCatalog();
    }
}

async function handleModelRoleEditorAction() {
    if (!S.modelCatalog.roleEditing) {
        startModelRoleEditing();
        hint("已进入模型链编辑模式。");
        return;
    }
    if (!S.modelCatalog.rolesDirty) {
        cancelModelRoleEditing();
        return;
    }
    await persistModelRoleChains(MODEL_SCOPES.map((item) => item.key), "模型链已保存。", { useDrafts: true });
}

function parseModelRetryOn(raw) {
    return String(raw || "").split(/[\n,]/).map((item) => item.trim()).filter(Boolean);
}

function parseModelHeaders(raw) {
    const text = String(raw || "").trim();
    if (!text) return null;
    const parsed = JSON.parse(text);
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
        throw new Error("额外请求头必须是 JSON 对象");
    }
    return Object.fromEntries(Object.entries(parsed).map(([key, value]) => [String(key), String(value)]));
}

function collectModelFormData(form, current) {
    const isCreate = form.dataset.mode === "create";
    const formData = new FormData(form);
    const key = isCreate ? String(formData.get("key") || "").trim() : String(form.dataset.modelKey || "").trim();
    const providerModel = String(formData.get("providerModel") || "").trim();
    const apiKey = String(formData.get("apiKey") || "").trim();
    const apiBase = String(formData.get("apiBase") || "").trim();
    const maxTokensText = String(formData.get("maxTokens") || "").trim();
    const temperatureText = String(formData.get("temperature") || "").trim();
    const reasoningEffort = String(formData.get("reasoningEffort") || "").trim();
    const retryOnRaw = String(formData.get("retryOn") || "").trim();
    const description = String(formData.get("description") || "").trim();
    const enabled = formData.get("enabled") === "on";
    const selectedScopes = new Set(MODEL_SCOPES.filter((scope) => formData.get(`scope_${scope.key}`) === "on").map((scope) => scope.key));

    if (!key) throw new Error("模型 Key 不能为空");
    if (!providerModel) throw new Error("Provider / Model 不能为空");
    if (!apiKey) throw new Error("API Key 不能为空");
    if (isCreate && !apiBase) throw new Error("Base URL 不能为空");

    const extraHeaders = parseModelHeaders(formData.get("extraHeaders"));
    const retryOn = retryOnRaw ? parseModelRetryOn(retryOnRaw) : null;
    const maxTokens = maxTokensText ? Number(maxTokensText) : null;
    const temperature = temperatureText ? Number(temperatureText) : null;

    if (maxTokensText && (!Number.isInteger(maxTokens) || maxTokens <= 0)) {
        throw new Error("Max Tokens 必须是正整数");
    }
    if (temperatureText && (!Number.isFinite(temperature) || temperature < 0 || temperature > 2)) {
        throw new Error("Temperature 必须在 0 到 2 之间");
    }

    if (isCreate) {
        const payload = {
            key,
            providerModel,
            apiKey,
            apiBase,
            enabled,
            scopes: [...selectedScopes],
            description,
        };
        if (maxTokens !== null) payload.maxTokens = maxTokens;
        if (temperature !== null) payload.temperature = temperature;
        if (reasoningEffort) payload.reasoningEffort = reasoningEffort;
        if (retryOn !== null) payload.retryOn = retryOn;
        if (extraHeaders !== null) payload.extraHeaders = extraHeaders;
        return { isCreate, key, enabled, selectedScopes, payload };
    }

    const patch = {};
    if (providerModel !== String(current?.provider_model || "")) patch.providerModel = providerModel;
    if (apiKey !== String(current?.api_key || "")) patch.apiKey = apiKey;
    if (apiBase !== String(current?.api_base || "")) patch.apiBase = apiBase;
    if (maxTokens !== null && maxTokens !== Number(current?.max_tokens ?? NaN)) patch.maxTokens = maxTokens;
    if (temperature !== null && temperature !== Number(current?.temperature ?? NaN)) patch.temperature = temperature;
    if (reasoningEffort !== String(current?.reasoning_effort || "")) patch.reasoningEffort = reasoningEffort;
    if (retryOn !== null && JSON.stringify(retryOn) !== JSON.stringify(current?.retry_on || [])) patch.retryOn = retryOn;
    if (description !== String(current?.description || "")) patch.description = description;
    if (extraHeaders !== null && JSON.stringify(extraHeaders) !== JSON.stringify(current?.extra_headers || null)) patch.extraHeaders = extraHeaders;
    return { isCreate, key, enabled, selectedScopes, patch };
}

async function saveModelDetail() {
    const form = U.modelDetail?.querySelector("#model-detail-form");
    if (!(form instanceof HTMLFormElement)) return;
    const current = form.dataset.mode === "create" ? null : modelRefItem(form.dataset.modelKey);
    try {
        const draft = collectModelFormData(form, current);
        const preserveRoleDrafts = !!S.modelCatalog.roleEditing;
        const enableChanged = !draft.isCreate && draft.enabled !== !!current?.enabled;
        if (!draft.isCreate && !Object.keys(draft.patch).length && !enableChanged) {
            hint("没有需要保存的更改。");
            return;
        }

        if (draft.isCreate) {
            const payload = await ApiClient.createManagedModel({ ...draft.payload, scopes: [] });
            applyModelCatalog(payload, { preserveRoleDrafts });
            S.modelCatalog.mode = "view";
            S.modelCatalog.selectedModelKey = payload.model?.key || draft.key;
        } else {
            if (Object.keys(draft.patch).length) {
                const payload = await ApiClient.updateManagedModel(current.key, draft.patch);
                applyModelCatalog(payload, { preserveRoleDrafts });
            }
            if (enableChanged) {
                const payload = draft.enabled ? await ApiClient.enableManagedModel(current.key) : await ApiClient.disableManagedModel(current.key);
                applyModelCatalog(payload, { preserveRoleDrafts });
            }
            S.modelCatalog.mode = "view";
            S.modelCatalog.selectedModelKey = current.key;
        }

        hint(draft.isCreate ? "模型已添加。" : "模型配置已保存。");
        showToast({ title: draft.isCreate ? "添加成功" : "修改成功", text: draft.isCreate ? "模型已添加成功" : "模型配置已保存", kind: "success" });
        clearModelSelection();

    } catch (e) {
        S.modelCatalog.error = e.message || "save failed";
        hint(`模型配置错误：${S.modelCatalog.error}`, true);
        showToast({ title: "修改失败", text: `模型配置错误：${S.modelCatalog.error}`, kind: "error" });
        clearModelSelection();
    }
}

async function deleteModelDetail(modelKey) {
    const targetKey = String(modelKey || "").trim();
    if (!targetKey) return;
    const confirmed = window.confirm(`删除模型 ${targetKey}？此操作会同时从 catalog 和所有角色链移除它。`);
    if (!confirmed) return;
    try {
        const payload = await ApiClient.deleteManagedModel(targetKey);
        applyModelCatalog(payload, { preserveRoleDrafts: false });
        hint("模型已删除。");
        showToast({ title: "删除成功", text: `模型 ${targetKey} 已删除`, kind: "success" });
        clearModelSelection();
    } catch (e) {
        const message = e.message || "delete failed";
        S.modelCatalog.error = message;
        hint(`模型删除失败：${message}`, true);
        showToast({ title: "删除失败", text: `模型删除失败：${message}`, kind: "error" });
    }
}

function initCeoWs() {
    if (S.ceoWs && S.ceoWs.readyState <= 1) return;
    S.ceoWs = new WebSocket(ApiClient.getCeoWsUrl());
    S.ceoWs.onmessage = (ev) => {
        const payload = JSON.parse(ev.data);
        if (payload.type === "snapshot.ceo") renderCeoSnapshot(payload.data?.messages || []);
        if (payload.type === "ceo.agent.tool") appendCeoToolEvent(payload.data || {});
        if (payload.type === "ceo.reply.final") finalizeCeoTurn(payload.data?.text || "");
        if (payload.type === "task.summary.changed" && S.view === "tasks") void loadTasks();
        if (payload.type === "task.artifact.applied" && payload.data?.task_id === S.currentTaskId) void loadTaskArtifacts();
    };
    S.ceoWs.onclose = () => window.setTimeout(() => S.view === "ceo" && initCeoWs(), 1000);
}

function sendCeoMessage() {
    const text = String(U.ceoInput.value || "").trim();
    if (!text) return;
    addMsg(text, "user");
    U.ceoInput.value = "";
    if (!S.ceoWs || S.ceoWs.readyState !== WebSocket.OPEN) {
        addMsg("Connection is not ready yet. Please try again in a moment.", "system");
        initCeoWs();
        return;
    }
    try {
        S.ceoWs.send(JSON.stringify({ type: "client.user_message", session_id: "web:shared", text }));
        const turn = createPendingCeoTurn();
        if (turn) S.ceoPendingTurns.push(turn);
    } catch (e) {
        addMsg(`Failed to send message: ${e.message || "unknown error"}`, "system");
        initCeoWs();
    }
}
﻿const canPause = (task) => !!task && !task.is_paused && pStatus(task.status) === "in_progress";
const canResume = (task) => !!task && !!task.is_paused;
const canCancel = (task) => !!task && ["in_progress"].includes(pStatus(task.status));

function taskStatusKey(task) {
    if (!task) return "unknown";
    if (task.is_paused) return "blocked";
    return pStatus(task.status) || "unknown";
}



function setDrawerOpen(backdrop, drawer, open) {
    const wasOpen = !!drawer?.classList.contains("is-open");
    backdrop?.classList.toggle("is-open", open);
    drawer?.classList.toggle("is-open", open);
    backdrop?.setAttribute("aria-hidden", open ? "false" : "true");
    drawer?.setAttribute("aria-hidden", open ? "false" : "true");
    if (open && drawer && !wasOpen) {
        drawer.__returnFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;
        window.requestAnimationFrame(() => {
            const focusTarget = drawer.querySelector("[data-modal-close], button, input, textarea, select");
            focusTarget?.focus?.();
        });
    }
    if (!open && drawer?.__returnFocus?.focus) {
        drawer.__returnFocus.focus();
    }
}

function syncActionButton(button, { idleLabel, busyLabel, busy = false, disabled = false } = {}) {
    if (!button) return;
    button.textContent = busy ? (busyLabel || idleLabel || button.textContent || "") : (idleLabel || button.textContent || "");
    button.disabled = !!disabled;
}

function renderSkillActions() {
    syncActionButton(U.skillRefresh, {
        idleLabel: "刷新",
        busyLabel: "刷新中...",
        busy: S.skillBusy,
        disabled: S.skillBusy,
    });
    syncActionButton(U.skillSave, {
        idleLabel: "保存",
        busyLabel: "保存中...",
        busy: S.skillBusy,
        disabled: S.skillBusy || !S.selectedSkill || !S.skillDirty,
    });
    syncDetailSaveButton("skill");
}

function renderToolActions() {
    syncActionButton(U.toolRefresh, {
        idleLabel: "刷新",
        busyLabel: "刷新中...",
        busy: S.toolBusy,
        disabled: S.toolBusy,
    });
    syncActionButton(U.toolSave, {
        idleLabel: "保存",
        busyLabel: "保存中...",
        busy: S.toolBusy,
        disabled: S.toolBusy || !S.selectedTool || !S.toolDirty,
    });
    syncDetailSaveButton("tool");
}

function clearSkillSelection() {
    S.selectedSkill = null;
    S.skillFiles = [];
    S.skillContents = {};
    S.selectedSkillFile = "";
    S.skillDirty = false;
    renderSkills();
    renderSkillDetail();
}

function clearToolSelection() {
    S.selectedTool = null;
    S.toolDirty = false;
    renderTools();
    renderToolDetail();
}

function taskStatusLabel(task) {
    return ({ in_progress: "Running", success: "Done", failed: "Failed", blocked: "Paused", unknown: "Unknown" })[taskStatusKey(task)] || "Unknown";
}

function statusBucketMatches(task, bucketKey) {
    const status = taskStatusKey(task);
    if (bucketKey === "paused") return status === "blocked";
    if (bucketKey === "failed") return status === "failed";
    if (bucketKey === "unread") return !!task?.is_unread;
    if (bucketKey === "running") return status === "in_progress";
    return false;
}

function getSelectedTasks() {
    return S.tasks.filter((task) => S.selectedTaskIds.has(task.task_id));
}

function setTaskMenuVisibility() {
    const filterOpen = !!(S.multiSelectMode && S.taskFilterMenuOpen);
    const batchOpen = !!(S.multiSelectMode && S.taskBatchMenuOpen);
    if (U.taskFilterWrap) U.taskFilterWrap.hidden = !S.multiSelectMode;
    if (U.taskBatchWrap) U.taskBatchWrap.hidden = !S.multiSelectMode;
    if (U.taskFilterMenu) U.taskFilterMenu.hidden = !filterOpen;
    if (U.taskBatchMenu) U.taskBatchMenu.hidden = !batchOpen;
    U.taskFilterTrigger?.setAttribute("aria-expanded", filterOpen ? "true" : "false");
    U.taskBatchTrigger?.setAttribute("aria-expanded", batchOpen ? "true" : "false");
}

function setTaskMenuOpen(kind, open) {
    if (kind === "filter") {
        S.taskFilterMenuOpen = !!open;
        if (open) S.taskBatchMenuOpen = false;
    } else {
        S.taskBatchMenuOpen = !!open;
        if (open) S.taskFilterMenuOpen = false;
    }
    setTaskMenuVisibility();
}

function closeTaskMenus() {
    S.taskFilterMenuOpen = false;
    S.taskBatchMenuOpen = false;
    setTaskMenuVisibility();
}

function setMultiSelectMode(enabled) {
    S.multiSelectMode = !!enabled;
    if (!S.multiSelectMode) S.selectedTaskIds.clear();
    closeTaskMenus();
    renderTasks();
}

function toggleTaskSelection(taskId) {
    if (S.selectedTaskIds.has(taskId)) S.selectedTaskIds.delete(taskId);
    else S.selectedTaskIds.add(taskId);
    renderTasks();
}

function syncTaskSelection() {
    const ids = new Set(S.tasks.map((task) => task.task_id));
    [...S.selectedTaskIds].forEach((id) => !ids.has(id) && S.selectedTaskIds.delete(id));
    if (!S.multiSelectMode) S.selectedTaskIds.clear();
}

function updateTaskToolbar() {
    const selected = getSelectedTasks();
    if (U.taskSelectionSummary) U.taskSelectionSummary.textContent = `Selected ${selected.length}`;
    const filterButtons = [...(U.taskFilterMenu?.querySelectorAll("[data-select-bucket]") || [])];
    filterButtons.forEach((button) => {
        button.disabled = S.taskBusy || !S.tasks.some((task) => statusBucketMatches(task, button.dataset.selectBucket));
    });
    const batchButtons = [...(U.taskBatchMenu?.querySelectorAll("[data-batch-action]") || [])];
    batchButtons.forEach((button) => {
        const action = button.dataset.batchAction;
        const enabled = action === "pause"
            ? selected.some((task) => canPause(task))
            : action === "resume"
                ? selected.some((task) => canResume(task))
                : selected.some((task) => canCancel(task));
        button.disabled = S.taskBusy || !enabled;
    });
    setTaskMenuVisibility();
}

function primaryTaskAction(task) {
    if (canPause(task)) return { action: "pause", label: "Pause", tone: "warn" };
    if (canResume(task)) return { action: "resume", label: "Resume", tone: "success" };
    return null;
}

function taskActionText(action) {
    return ({ pause: "Pause", resume: "Resume", cancel: "Cancel" }[action] || "Action");
}

async function requestTaskAction(taskId, action) {
    if (action === "pause") return ApiClient.pauseTask(taskId);
    if (action === "resume") return ApiClient.resumeTask(taskId);
    if (action === "cancel") return ApiClient.cancelTask(taskId);
    throw new Error(`Unsupported task action: ${action}`);
}

function taskMetaText(task) {
    const parts = [];
    if (task.is_unread) parts.push("Unread");
    if (task.updated_at) parts.push(new Date(task.updated_at).toLocaleString());
    return parts.join(" · ") || "No timestamp";
}
function renderTasks() {
    U.taskGrid.innerHTML = "";
    if (!S.tasks.length) {
        U.taskGrid.innerHTML = '<div class="empty-state" style="grid-column: 1/-1;">No tasks yet.</div>';
        return updateTaskToolbar();
    }
    S.tasks.forEach((task) => {
        const selected = S.selectedTaskIds.has(task.task_id);
        const primaryAction = primaryTaskAction(task);
        const statusKey = taskStatusKey(task);
        const el = document.createElement("div");
        el.className = `project-card${selected ? " is-selected" : ""}${S.multiSelectMode ? " is-multi-mode" : ""}`;
        el.innerHTML = `
            <div class="pc-topbar">
                <label class="project-select-toggle${S.multiSelectMode ? " is-visible" : ""}"><input type="checkbox" class="project-select-checkbox" ${selected ? "checked" : ""} ${S.taskBusy ? "disabled" : ""}><span>Select</span></label>
                <span class="status-badge" data-status="${esc(statusKey)}">${esc(taskStatusLabel(task))}</span>
            </div>
            <div class="pc-header"><div><h3 class="pc-title">${esc(task.title || task.task_id)}</h3><span class="pc-id">${esc(task.task_id)}</span></div></div>
            <div class="pc-summary">${esc(task.brief || "No summary")}</div>
            <div class="pc-stats">${esc(taskMetaText(task))}</div>
            <div class="pc-actions">
                <div class="pc-actions-left">
                    ${primaryAction ? `<button class="project-action-btn ${primaryAction.tone}" type="button" data-action="${primaryAction.action}" ${S.taskBusy ? "disabled" : ""}>${primaryAction.label}</button>` : ""}
                </div>
                <div class="pc-actions-right">
                    <button class="project-action-btn danger" type="button" data-action="cancel" ${S.taskBusy || !canCancel(task) ? "disabled" : ""}>Cancel</button>
                </div>
            </div>
        `;
        const toggle = el.querySelector(".project-select-toggle");
        const checkbox = el.querySelector(".project-select-checkbox");
        toggle?.addEventListener("click", (e) => e.stopPropagation());
        checkbox?.addEventListener("change", (e) => {
            e.stopPropagation();
            if (e.target.checked) S.selectedTaskIds.add(task.task_id);
            else S.selectedTaskIds.delete(task.task_id);
            renderTasks();
        });
        el.querySelectorAll(".project-action-btn").forEach((btn) => btn.addEventListener("click", async (e) => {
            e.stopPropagation();
            await runTaskAction(task.task_id, btn.dataset.action);
        }));
        el.addEventListener("click", () => {
            if (S.multiSelectMode) {
                toggleTaskSelection(task.task_id);
                return;
            }
            void openTask(task.task_id);
        });
        U.taskGrid.appendChild(el);
    });
    updateTaskToolbar();
    icons();
}

async function loadTasks() {
    U.taskGrid.innerHTML = '<div class="empty-state" style="grid-column: 1/-1;">Loading tasks...</div>';
    try {
        S.tasks = await ApiClient.getTasks(1);
        syncTaskSelection();
        renderTasks();
    } catch (e) {
        U.taskGrid.innerHTML = `<div class="empty-state error" style="grid-column: 1/-1;">Failed to load tasks: ${esc(e.message)}</div>`;
        showToast({ title: "Load failed", text: e.message || "Unknown error", kind: "error" });
    }
}

async function runTaskAction(taskId, action) {
    if (!taskId || !action) return;
    S.taskBusy = true;
    renderTasks();
    try {
        await requestTaskAction(taskId, action);
        showToast({ title: `${taskActionText(action)} done`, text: taskId, kind: action === "cancel" ? "warn" : "success" });
        await loadTasks();
        if (S.currentTaskId === taskId) {
            await loadTaskDetail(taskId, { preserveView: true, reopenSocket: false });
            await loadTaskArtifacts();
        }
    } catch (e) {
        showToast({ title: `${taskActionText(action)} failed`, text: e.message || "Unknown error", kind: "error" });
    } finally {
        S.taskBusy = false;
        renderTasks();
    }
}

async function runTaskBatchAction(action) {
    closeTaskMenus();
    const selected = getSelectedTasks();
    const eligible = selected.filter((task) => {
        if (action === "pause") return canPause(task);
        if (action === "resume") return canResume(task);
        if (action === "cancel") return canCancel(task);
        return false;
    });
    if (!eligible.length) {
        showToast({ title: "No eligible tasks", text: "Current selection cannot perform this action.", kind: "warn" });
        return;
    }
    S.taskBusy = true;
    renderTasks();
    try {
        await Promise.allSettled(eligible.map((task) => requestTaskAction(task.task_id, action)));
        showToast({ title: `${taskActionText(action)} batch done`, text: `${eligible.length} tasks updated`, kind: "success" });
        await loadTasks();
    } finally {
        S.taskBusy = false;
        renderTasks();
    }
}

function resetTaskView() {
    if (S.taskWs) {
        S.taskWs.close();
        S.taskWs = null;
    }
    S.currentTask = null;
    S.currentTaskProgress = null;
    S.taskArtifacts = [];
    S.selectedArtifactId = "";
    S.artifactContent = "";
    S.tree = null;
    S.treeView = null;
    S.selectedNodeId = null;
    S.treePan.offsetX = 0;
    S.treePan.offsetY = 0;
    S.treePan.baseOffsetX = 0;
    S.treePan.baseOffsetY = 0;
    U.tree.innerHTML = '<div class="empty-state">Waiting for task tree...</div>';
    U.feedTitle.textContent = "Node Details";
    if (U.nodeEmpty) U.nodeEmpty.style.display = "block";
    if (U.artifactList) U.artifactList.innerHTML = '<div class="empty-state" style="padding: 10px;">No artifacts yet.</div>';
    if (U.artifactContent) U.artifactContent.textContent = "Select an artifact to view details.";
    if (U.artifactApply) U.artifactApply.hidden = true;
    setTaskSelectionEmptyVisible(false);
    hideAgent();
}

function setTaskDetailOpen(open) {
    setDrawerOpen(U.taskDetailBackdrop, U.taskDetailDrawer, open);
}

function setTaskSelectionEmptyVisible(visible) {
    if (U.taskSelectionEmpty) U.taskSelectionEmpty.hidden = !visible;
}

function clearAgentSelection({ rerender = true } = {}) {
    S.selectedNodeId = null;
    U.feedTitle.textContent = "Node Details";
    hideAgent();
    if (rerender) renderTree();
}
function findTreeNode(node, nodeId) {
    if (!node) return null;
    if (String(node.node_id || "") === String(nodeId || "")) return node;
    for (const child of (node.children || [])) {
        const found = findTreeNode(child, nodeId);
        if (found) return found;
    }
    return null;
}

function bindTreePan() {
    if (!U.tree || U.tree.dataset.panBound === "true") return;
    U.tree.dataset.panBound = "true";
    const state = S.treePan;
    const applyPan = () => {
        const canvas = U.tree?.querySelector(".execution-tree");
        if (canvas) canvas.style.transform = `translate(${Math.round(state.offsetX)}px, ${Math.round(state.offsetY)}px)`;
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
        const node = e.target.closest(".execution-tree-node");
        if (node) return;
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
    U.tree.__applyPan = applyPan;
}

function nodeOutputText(node) {
    if (!node) return "";
    if (typeof node.output === "string") return node.output;
    if (Array.isArray(node.output)) return node.output.map((item) => String(item.content || "").trim()).filter(Boolean).join("\n\n");
    return String(node.final_output || "");
}

function buildExecutionTree(rawTree) {
    if (!rawTree) return null;
    const nodeRecords = Array.isArray(S.currentTaskProgress?.nodes) ? S.currentTaskProgress.nodes : [];
    const nodeMap = new Map(nodeRecords.map((item) => [String(item.node_id || ""), item]));
    const walk = (node) => {
        const detail = nodeMap.get(String(node.node_id || "")) || {};
        const status = String(node.status || detail.status || "unknown").trim().toLowerCase() || "unknown";
        return {
            node_id: node.node_id,
            title: node.title || detail.goal || node.node_id,
            kind: detail.node_kind || "execution",
            state: status,
            display_state: status.toUpperCase(),
            input: node.input || detail.input || "",
            output: node.output || detail.final_output || nodeOutputText(detail) || "-",
            check: node.check_result || detail.check_result || "-",
            log: Array.isArray(detail.output) ? detail.output.map((entry) => ({ kind: entry.tool_calls?.length ? "tool" : "log", ts: entry.created_at, content: entry.content || "" })) : [],
            children: Array.isArray(node.children) ? node.children.map(walk) : [],
        };
    };
    return walk(rawTree);
}

function renderNodeLogs(rows) {
    U.adLogs.innerHTML = "";
    if (!Array.isArray(rows) || !rows.length) {
        U.adLogs.innerHTML = '<div class="empty-state" style="padding: 10px;">No logs yet.</div>';
        return;
    }
    rows.forEach((row) => {
        const el = document.createElement("div");
        el.className = "feed-card type-info";
        const kind = String(row.kind || "log");
        const ts = row.ts ? new Date(row.ts).toLocaleTimeString() : "";
        el.innerHTML = `<div class="card-header"><span>${esc(kind.toUpperCase())}</span><span>${esc(ts)}</span></div><div class="card-content">${esc(String(row.content || ""))}</div>`;
        U.adLogs.appendChild(el);
    });
    icons();
}

function renderTree() {
    if (!S.tree) return;
    S.treeView = buildExecutionTree(S.tree);
    if (!S.treeView) {
        U.tree.innerHTML = '<div class="empty-state">No nodes to display.</div>';
        setTaskSelectionEmptyVisible(false);
        return;
    }
    const wrapper = document.createElement("div");
    wrapper.className = "execution-tree";
    const rootList = document.createElement("ul");
    rootList.className = "execution-tree-list";
    const walk = (node) => {
        const title = String(node.title || node.node_id || "");
        const displayState = String(node.display_state || node.state || "").toUpperCase();
        const item = document.createElement("li");
        item.className = "execution-tree-item";
        const el = document.createElement("button");
        el.type = "button";
        el.className = `execution-tree-node${S.selectedNodeId === node.node_id ? " selected" : ""}`;
        el.dataset.id = node.node_id;
        el.dataset.kind = node.kind || "execution";
        el.setAttribute("aria-pressed", S.selectedNodeId === node.node_id ? "true" : "false");
        el.innerHTML = `<span class="execution-tree-node-head"><span class="execution-tree-node-title">${esc(title)}</span><span class="status-badge" data-status="${esc(node.state || "")}">${esc(displayState)}</span></span>`;
        el.addEventListener("click", (e) => {
            if (S.treePan.suppressClickNodeId && S.treePan.suppressClickNodeId === String(node.node_id || "")) {
                S.treePan.suppressClickNodeId = null;
                return;
            }
            e.stopPropagation();
            S.selectedNodeId = node.node_id;
            showAgent(node);
            renderTree();
        });
        item.appendChild(el);
        if ((node.children || []).length) {
            const branch = document.createElement("ul");
            branch.className = "execution-tree-list";
            (node.children || []).forEach((child) => branch.appendChild(walk(child)));
            item.appendChild(branch);
        }
        return item;
    };
    rootList.appendChild(walk(S.treeView));
    wrapper.appendChild(rootList);
    wrapper.style.transform = `translate(${Math.round(S.treePan.offsetX)}px, ${Math.round(S.treePan.offsetY)}px)`;
    U.tree.innerHTML = "";
    U.tree.appendChild(wrapper);
    if (S.selectedNodeId) {
        const selected = findTreeNode(S.treeView, S.selectedNodeId);
        if (selected) {
            setTaskSelectionEmptyVisible(false);
            showAgent(selected);
        }
        else {
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
    U.artifactList.innerHTML = "";
    if (!Array.isArray(S.taskArtifacts) || !S.taskArtifacts.length) {
        U.artifactList.innerHTML = '<div class="empty-state" style="padding: 10px;">No artifacts yet.</div>';
        if (U.artifactContent) U.artifactContent.textContent = "Select an artifact to view details.";
        if (U.artifactApply) U.artifactApply.hidden = true;
        return;
    }
    S.taskArtifacts.forEach((artifact) => {
        const button = document.createElement("button");
        button.type = "button";
        button.className = `artifact-item${S.selectedArtifactId === artifact.artifact_id ? " active" : ""}`;
        button.innerHTML = `<strong>${esc(artifact.title || artifact.artifact_id)}</strong><span>${esc(artifact.kind || "artifact")}</span><small>${esc(artifact.preview_text || artifact.created_at || "")}</small>`;
        button.addEventListener("click", () => void selectArtifact(artifact.artifact_id));
        U.artifactList.appendChild(button);
    });
}

async function loadTaskArtifacts() {
    if (!S.currentTaskId) return [];
    S.taskArtifacts = await ApiClient.getTaskArtifacts(S.currentTaskId);
    renderArtifacts();
    if (S.taskArtifacts.length && !S.selectedArtifactId) {
        await selectArtifact(S.taskArtifacts[0].artifact_id);
    }
    return S.taskArtifacts;
}

async function selectArtifact(artifactId) {
    if (!S.currentTaskId || !artifactId) return;
    S.selectedArtifactId = artifactId;
    renderArtifacts();
    const data = await ApiClient.getTaskArtifact(S.currentTaskId, artifactId);
    S.artifactContent = data.content || "";
    if (U.artifactContent) U.artifactContent.textContent = S.artifactContent || "";
    const artifact = S.taskArtifacts.find((item) => item.artifact_id === artifactId);
    if (U.artifactApply) U.artifactApply.hidden = !(artifact && artifact.kind === "patch");
}

async function applySelectedArtifact() {
    if (!S.currentTaskId || !S.selectedArtifactId) return;
    await ApiClient.applyTaskArtifact(S.currentTaskId, S.selectedArtifactId);
    showToast({ title: "Patch applied", text: S.selectedArtifactId, kind: "success" });
    await loadTaskArtifacts();
}

function showAgent(node) {
    U.detail.style.display = "flex";
    if (U.nodeEmpty) U.nodeEmpty.style.display = "none";
    setTaskSelectionEmptyVisible(false);
    U.adRole.textContent = node.title || node.node_id || "Node";
    U.adStatus.textContent = String(node.display_state || node.state || "").toUpperCase();
    U.adStatus.dataset.status = node.state || "";
    U.adInput.textContent = node.input || "-";
    U.adOutput.textContent = node.output || "-";
    U.adCheck.textContent = node.check || "-";
    U.feedTitle.textContent = `Node: ${node.title || node.node_id || ""}`;
    renderNodeLogs(node.log || []);
    renderArtifacts();
    setTaskDetailOpen(true);
}

function hideAgent() {
    if (U.detail) U.detail.style.display = "none";
    setTaskDetailOpen(false);
}

function applyTaskPayload(payload) {
    if (!payload || !payload.task || !payload.progress) return;
    S.currentTask = payload.task;
    S.currentTaskProgress = payload.progress;
    S.tree = payload.progress.root;
    U.tdTitle.textContent = payload.task.title || payload.task.task_id || "Loading...";
    U.tdStatus.textContent = taskStatusLabel(payload.task).toUpperCase();
    U.tdStatus.dataset.status = taskStatusKey(payload.task);
    U.tdSummary.textContent = payload.task.user_request || payload.task.final_output || payload.progress.text || "No summary";
    U.tdActiveCount.textContent = String((payload.progress.nodes || []).filter((node) => String(node.status || "") === "in_progress").length);
    if (S.tree) renderTree();
    else {
        U.tree.innerHTML = '<div class="empty-state">No task tree.</div>';
        setTaskSelectionEmptyVisible(false);
        hideAgent();
    }
}

async function loadTaskDetail(taskId, { preserveView = false, reopenSocket = true } = {}) {
    if (!taskId) return;
    if (!preserveView) {
        S.currentTaskId = taskId;
        switchView("task-details");
        resetTaskView();
    }
    const payload = await ApiClient.getTask(taskId, true);
    applyTaskPayload(payload);
    if (reopenSocket) {
        if (S.taskWs) {
            S.taskWs.close();
            S.taskWs = null;
        }
        S.taskWs = new WebSocket(ApiClient.getTaskWsUrl(taskId));
        S.taskWs.onmessage = (ev) => handleTaskEvent(JSON.parse(ev.data));
    }
    return payload;
}

async function openTask(taskId) {
    try {
        await loadTaskDetail(taskId);
        await loadTaskArtifacts();
    } catch (e) {
        U.tree.innerHTML = `<div class="empty-state error">Failed to open task: ${esc(e.message)}</div>`;
        showToast({ title: "Task open failed", text: e.message || "Unknown error", kind: "error" });
    }
}

function handleTaskEvent(payload) {
    if (payload.type === "snapshot.task") {
        applyTaskPayload(payload.data || {});
        return;
    }
    if (payload.type === "artifact.applied") {
        showToast({ title: "Artifact applied", text: payload.data?.artifact_id || "", kind: "success" });
        void loadTaskArtifacts();
    }
}


function filterSkills() {
    const q = String(U.skillSearch.value || "").trim().toLowerCase();
    return S.skills.filter((skill) => {
        if (q && !`${skill.skill_id} ${skill.display_name} ${skill.source_path}`.toLowerCase().includes(q)) return false;
        if (U.skillRisk.value !== "all" && skill.risk_level !== U.skillRisk.value) return false;
        if (U.skillStatus.value === "enabled" && !skill.enabled) return false;
        if (U.skillStatus.value === "disabled" && skill.enabled) return false;
        if (U.skillStatus.value === "unavailable" && skill.available) return false;
        return true;
    });
}

function displayRoleLabel(role) {
    return ({ ceo: "主Agent", execution: "执行", inspection: "检验" }[roleKey(role)] || String(role || ""));
}

function displayRiskLabel(level) {
    return ({ low: "低风险", medium: "中风险", high: "高风险" }[String(level || "").trim().toLowerCase()] || String(level || "未知风险"));
}

function displayEnabledLabel(enabled, available = true) {
    if (!available) return "不可用";
    return enabled ? "已启用" : "已禁用";
}

function renderSkills() {
    U.skillList.innerHTML = "";
    const items = filterSkills();
    if (!items.length) return void (U.skillList.innerHTML = '<div class="empty-state">没有匹配的 Skill。</div>');
    items.forEach((skill) => {
        const el = document.createElement("button");
        el.type = "button";
        el.className = `resource-list-item${S.selectedSkill?.skill_id === skill.skill_id ? " selected" : ""}`;
        const desc = (skill.description || "").trim();
        const subtitle = desc ? (desc.length > 50 ? desc.slice(0, 47) + "..." : desc) : skill.skill_id;
        el.innerHTML = `<div class="resource-list-title">${esc(skill.display_name)}</div><div class="resource-list-subtitle">${esc(subtitle)}</div><div class="resource-list-meta">${esc(displayRiskLabel(skill.risk_level))} · ${esc(displayEnabledLabel(skill.enabled, skill.available))}</div>`;
        el.addEventListener("click", () => openSkill(skill.skill_id));
        U.skillList.appendChild(el);
    });
}

function filterTools() {
    const q = String(U.toolSearch.value || "").trim().toLowerCase();
    return S.tools.filter((tool) => {
        if (q && !`${tool.tool_id} ${tool.display_name} ${tool.source_path}`.toLowerCase().includes(q)) return false;
        if (U.toolStatus.value === "enabled" && !tool.enabled) return false;
        if (U.toolStatus.value === "disabled" && tool.enabled) return false;
        if (U.toolStatus.value === "unavailable" && tool.available) return false;
        if (U.toolRisk.value !== "all" && !(tool.actions || []).some((a) => a.risk_level === U.toolRisk.value)) return false;
        return true;
    });
}

function renderTools() {
    U.toolList.innerHTML = "";
    const items = filterTools();
    if (!items.length) return void (U.toolList.innerHTML = '<div class="empty-state">没有匹配的 Tool。</div>');
    items.forEach((tool) => {
        const el = document.createElement("button");
        el.type = "button";
        el.className = `resource-list-item${S.selectedTool?.tool_id === tool.tool_id ? " selected" : ""}`;
        const desc = (tool.description || "").trim();
        const subtitle = desc ? (desc.length > 50 ? desc.slice(0, 47) + "..." : desc) : tool.tool_id;
        el.innerHTML = `<div class="resource-list-title">${esc(tool.display_name)}</div><div class="resource-list-subtitle">${esc(subtitle)}</div><div class="resource-list-meta">${esc(displayEnabledLabel(tool.enabled, tool.available))} · ${(tool.actions || []).length} 个 action</div>`;
        el.addEventListener("click", () => openTool(tool.tool_id));
        U.toolList.appendChild(el);
    });
}

function toggleTheme() {
    const html = document.documentElement;
    const dark = html.getAttribute("data-theme") === "dark";
    html.setAttribute("data-theme", dark ? "light" : "dark");
    const darkIcon = U.theme.querySelector(".dark-icon");
    const lightIcon = U.theme.querySelector(".light-icon");
    if (darkIcon && lightIcon) {
        darkIcon.style.display = dark ? "none" : "block";
        lightIcon.style.display = dark ? "block" : "none";
    }
}

function renderSkillDetail() {
    if (!S.selectedSkill) {
        U.skillEmpty.style.display = "block";
        U.skillDetail.innerHTML = "";
        setDrawerOpen(U.skillBackdrop, U.skillDrawer, false);
        renderSkillActions();
        return;
    }
    U.skillEmpty.style.display = "none";
    setDrawerOpen(U.skillBackdrop, U.skillDrawer, true);
    const roles = ["ceo", "execution", "inspection"];
    const allowedRoles = Array.isArray(S.selectedSkill.allowed_roles) ? S.selectedSkill.allowed_roles : [];
    const editorValue = esc(S.skillContents[S.selectedSkillFile] || "");
    const fileTabs = S.skillFiles.length
        ? S.skillFiles.map((file) => `<button type="button" class="toolbar-btn ghost skill-file ${S.selectedSkillFile === file.file_key ? "active" : ""}" data-file="${esc(file.file_key)}">${esc(file.file_key)}</button>`).join("")
        : '<span class="resource-empty-copy">暂无可编辑文件</span>';
    U.skillDetail.innerHTML = `
        <article class="resource-detail-card detail-modal-shell">
            <div class="detail-modal-header">
                <div class="detail-modal-title">
                    <h2 id="skill-detail-title">${esc(S.selectedSkill.display_name)}</h2>
                    <p class="subtitle">${esc(S.selectedSkill.skill_id)}</p>
                </div>
                <div class="detail-modal-actions">
                    <button type="button" class="toolbar-btn ghost" id="skill-modal-close" data-modal-close>关闭</button>
                    <button type="button" class="toolbar-btn success" id="skill-modal-save">保存</button>
                </div>
            </div>
            <div class="detail-modal-body">
                <div class="resource-status-row" style="margin-bottom: var(--space-4);">
                    ${S.selectedSkill.enabled
                        ? `<button type="button" class="toolbar-btn danger" id="skill-disable-btn">禁用技能</button>`
                        : `<button type="button" class="toolbar-btn success" id="skill-enable-btn">启用技能</button>`}
                </div>
                <div class="resource-draft-hint${S.skillDirty ? " is-dirty" : ""}" ${S.skillDirty ? "" : "hidden"}></div>
                <div class="resource-section">
                    <h3>角色可见性</h3>
                    <div class="resource-filter-row">
                        ${roles.map((role) => `
                            <label class="role-toggle ${allowedRoles.includes(role) ? "checked" : ""}">
                                <input type="checkbox" class="skill-role" data-role="${role}" ${allowedRoles.includes(role) ? "checked" : ""}>
                                <span>${esc(displayRoleLabel(role))}</span>
                            </label>
                        `).join("")}
                    </div>
                </div>
                <div class="resource-section">
                    <h3>文件内容</h3>
                    <div class="resource-filter-row">${fileTabs}</div>
                    <textarea id="skill-editor" rows="18" class="resource-editor">${editorValue}</textarea>
                </div>
            </div>
        </article>`;
    U.skillDetail.querySelector("#skill-modal-close")?.addEventListener("click", clearSkillSelection);
    U.skillDetail.querySelector("#skill-modal-save")?.addEventListener("click", () => void saveSkill());
    U.skillDetail.querySelector("#skill-enable-btn")?.addEventListener("click", () => {
        S.selectedSkill.enabled = true;
        setSkillDirty(true);
        renderSkillDetail();
    });
    U.skillDetail.querySelector("#skill-disable-btn")?.addEventListener("click", () => {
        S.selectedSkill.enabled = false;
        setSkillDirty(true);
        renderSkillDetail();
    });
    U.skillDetail.querySelectorAll(".skill-role").forEach((checkbox) => checkbox.addEventListener("change", (e) => {
        const nextRoles = new Set(allowedRoles);
        if (e.target.checked) nextRoles.add(e.target.dataset.role);
        else nextRoles.delete(e.target.dataset.role);
        S.selectedSkill.allowed_roles = [...nextRoles];
        setSkillDirty(true);
        renderSkillDetail();
    }));
    U.skillDetail.querySelectorAll(".skill-file").forEach((button) => button.addEventListener("click", () => {
        const editor = document.getElementById("skill-editor");
        if (editor && S.selectedSkillFile) S.skillContents[S.selectedSkillFile] = editor.value;
        S.selectedSkillFile = button.dataset.file;
        renderSkillDetail();
    }));
    U.skillDetail.querySelector("#skill-editor")?.addEventListener("input", (e) => {
        if (!S.selectedSkillFile) return;
        S.skillContents[S.selectedSkillFile] = e.target.value;
        setSkillDirty(true);
    });
    renderSkillActions();
}

async function loadSkills({ renderDetail = true } = {}) {
    U.skillList.innerHTML = '<div class="empty-state">Loading skills...</div>';
    const selectedId = S.selectedSkill?.skill_id || "";
    try {
        S.skills = await ApiClient.getSkills(0, 300);
        if (selectedId) {
            const next = S.skills.find((skill) => skill.skill_id === selectedId);
            if (next) S.selectedSkill = next;
            else clearSkillSelection();
        }
        renderSkills();
        if (renderDetail) renderSkillDetail();
    } catch (e) {
        U.skillList.innerHTML = `<div class="empty-state error">Failed to load skills: ${esc(e.message)}</div>`;
        addNotice({ kind: "resource_failed", title: "Skill load failed", text: e.message || "Unknown error" });
    } finally {
        renderSkillActions();
    }
}

async function openSkill(skillId, quiet = false) {
    if (!quiet) {
        setDrawerOpen(U.skillBackdrop, U.skillDrawer, true);
        U.skillEmpty.style.display = "none";
        U.skillDetail.innerHTML = '<div class="empty-state">Loading skill details...</div>';
    }
    try {
        const [skill, files] = await Promise.all([ApiClient.getSkill(skillId), ApiClient.getSkillFiles(skillId)]);
        S.selectedSkill = skill;
        S.skillFiles = files;
        S.selectedSkillFile = files[0]?.file_key || "";
        S.skillContents = {};
        S.skillDirty = false;
        await Promise.all(files.map(async (file) => {
            const data = await ApiClient.getSkillFile(skillId, file.file_key);
            S.skillContents[file.file_key] = data.content || "";
        }));
        renderSkills();
        renderSkillDetail();
    } catch (e) {
        U.skillDetail.innerHTML = `<div class="empty-state error">Failed to load skill details: ${esc(e.message)}</div>`;
        addNotice({ kind: "resource_failed", title: "Skill detail failed", text: e.message || "Unknown error" });
    } finally {
        renderSkillActions();
    }
}

async function saveSkill() {
    if (S.skillBusy) return;
    const selectedId = String(S.selectedSkill?.skill_id || "").trim();
    const displayName = String(S.selectedSkill?.display_name || selectedId || "Skill").trim();
    const enabled = !!S.selectedSkill?.enabled;
    const allowedRoles = Array.isArray(S.selectedSkill?.allowed_roles) ? [...S.selectedSkill.allowed_roles] : [];
    if (!selectedId || !S.selectedSkill) {
        addNotice({ kind: "resource_failed", title: "No skill selected", text: "Select a skill before saving." });
        showToast({ title: "保存失败", text: "未选择 Skill", kind: "error" });
        return;
    }
    if (!S.skillDirty) {
        showToast({ title: "No pending changes", text: "This skill has no unsaved changes.", kind: "info", durationMs: 1800 });
        return;
    }
    S.skillBusy = true;
    renderSkillActions();
    showToast({ title: "保存中", text: "正在保存 Skill，请稍候…", kind: "info", persistent: true });
    try {
        const editor = document.getElementById("skill-editor");
        if (editor && S.selectedSkillFile) S.skillContents[S.selectedSkillFile] = editor.value;
        for (const [fileKey, content] of Object.entries(S.skillContents)) {
            await ApiClient.saveSkillFile(selectedId, fileKey, content);
        }
        await ApiClient.updateSkillPolicy(selectedId, {
            enabled,
            allowed_roles: allowedRoles,
        });
        await ApiClient.reloadResources();
        await loadSkills({ renderDetail: false });
        await openSkill(selectedId, true);
        setSkillDirty(false);
        addNotice({ kind: "resource_saved", title: "Skill saved", text: displayName || selectedId });
        showToast({ title: "保存成功", text: "Skill 配置已保存", kind: "success", durationMs: 2200 });
    } catch (e) {
        addNotice({ kind: "resource_failed", title: "Skill save failed", text: e.message || "Unknown error" });
        showToast({ title: "保存失败", text: e.message || "Unknown error", kind: "error", durationMs: 2600 });
    } finally {
        S.skillBusy = false;
        renderSkillActions();
    }
}

function renderToolDetail() {
    if (!S.selectedTool) {
        U.toolEmpty.style.display = "block";
        U.toolDetail.innerHTML = "";
        setDrawerOpen(U.toolBackdrop, U.toolDrawer, false);
        renderToolActions();
        return;
    }
    U.toolEmpty.style.display = "none";
    setDrawerOpen(U.toolBackdrop, U.toolDrawer, true);
    const roles = ["ceo", "execution", "inspection"];
    const actions = Array.isArray(S.selectedTool.actions) ? S.selectedTool.actions : [];
    const description = String(S.selectedTool.description || "").trim();
    const toolskillContent = String(S.selectedTool.toolskill_content || "").trim();
    U.toolDetail.innerHTML = `
        <article class="resource-detail-card detail-modal-shell">
            <div class="detail-modal-header">
                <div class="detail-modal-title">
                    <h2 id="tool-detail-title">${esc(S.selectedTool.display_name)}</h2>
                    <p class="subtitle">${esc(S.selectedTool.tool_id)}</p>
                </div>
                <div class="detail-modal-actions">
                    <button type="button" class="toolbar-btn ghost" id="tool-modal-close" data-modal-close>关闭</button>
                    <button type="button" class="toolbar-btn success" id="tool-modal-save">保存</button>
                </div>
            </div>
            <div class="detail-modal-body">
                <div class="resource-status-row" style="margin-bottom: var(--space-4);">
                    ${S.selectedTool.enabled
                        ? `<button type="button" class="toolbar-btn danger" id="tool-disable-btn">禁用工具族</button>`
                        : `<button type="button" class="toolbar-btn success" id="tool-enable-btn">启用工具族</button>`}
                </div>
                <div class="resource-section">
                    <h3>描述</h3>
                    <div class="resource-copy-block">${esc(description || "暂无描述。")}</div>
                </div>
                <div class="resource-draft-hint${S.toolDirty ? " is-dirty" : ""}" ${S.toolDirty ? "" : "hidden"}></div>
                <div class="resource-section">
                    <details class="resource-disclosure toolskill-disclosure">
                        <summary class="resource-disclosure-summary">
                            <span>工具技巧</span>
                            <span class="resource-disclosure-hint">点击展开</span>
                        </summary>
                        <div class="resource-disclosure-body">
                            ${toolskillContent
                                ? `<pre class="resource-preformatted toolskill-content">${esc(toolskillContent)}</pre>`
                                : `<div class="resource-empty-copy">暂无工具技巧说明。</div>`}
                        </div>
                    </details>
                </div>
                <div class="resource-section">
                    <div class="tool-permission-heading">
                        <h3>Action 权限</h3>
                        <p class="subtitle">控制当前工具族下各个 action 对主Agent、执行和检验角色的可见性。</p>
                    </div>
                    <div class="tool-permission-grid">
                        ${actions.length ? actions.map((action) => {
                            const actionName = esc(action.label || action.action_id);
                            const actionId = esc(action.action_id);
                            const riskLevel = esc(displayRiskLabel(action.risk_level || "medium"));
                            const riskClass = `risk-${String(action.risk_level || "medium").toLowerCase()}`;
                            return `
                                <article class="tool-permission-card">
                                    <div class="tool-permission-card-head">
                                        <div class="tool-action-meta">
                                            <div class="tool-action-name">${actionName}</div>
                                            <div class="tool-action-id">${actionId}</div>
                                        </div>
                                        <span class="risk-pill ${riskClass}">${riskLevel}</span>
                                    </div>
                                    <div class="tool-role-toggle-group">
                                        ${roles.map((role) => `
                                            <label class="role-toggle tool-role-toggle ${action.allowed_roles?.includes(role) ? "checked" : ""}">
                                                <input type="checkbox" class="tool-role tool-role-input" data-action="${actionId}" data-role="${role}" aria-label="${actionName} - ${esc(displayRoleLabel(role))}" ${action.allowed_roles?.includes(role) ? "checked" : ""}>
                                                <span>${esc(displayRoleLabel(role))}</span>
                                            </label>
                                        `).join("")}
                                    </div>
                                </article>`;
                        }).join("") : `<div class="tool-empty-card">当前工具族没有 action。</div>`}
                    </div>
                </div>
            </div>
        </article>`;
    U.toolDetail.querySelector("#tool-modal-close")?.addEventListener("click", clearToolSelection);
    U.toolDetail.querySelector("#tool-modal-save")?.addEventListener("click", () => void saveTool());
    U.toolDetail.querySelector("#tool-enable-btn")?.addEventListener("click", () => {
        S.selectedTool.enabled = true;
        setToolDirty(true);
        renderToolDetail();
    });
    U.toolDetail.querySelector("#tool-disable-btn")?.addEventListener("click", () => {
        S.selectedTool.enabled = false;
        setToolDirty(true);
        renderToolDetail();
    });
    U.toolDetail.querySelectorAll(".tool-role").forEach((checkbox) => checkbox.addEventListener("change", (e) => {
        const action = S.selectedTool.actions.find((item) => item.action_id === e.target.dataset.action);
        if (!action) return;
        const set = new Set(action.allowed_roles || []);
        if (e.target.checked) set.add(e.target.dataset.role);
        else set.delete(e.target.dataset.role);
        action.allowed_roles = [...set];
        e.target.closest(".role-toggle")?.classList.toggle("checked", e.target.checked);
        setToolDirty(true);
    }));
    renderToolActions();
}

async function loadTools({ renderDetail = true } = {}) {
    U.toolList.innerHTML = '<div class="empty-state">Loading tools...</div>';
    const selectedId = S.selectedTool?.tool_id || "";
    try {
        S.tools = await ApiClient.getTools(0, 300);
        if (selectedId) {
            const next = S.tools.find((tool) => tool.tool_id === selectedId);
            if (next) {
                S.selectedTool = {
                    ...next,
                    primary_executor_name: S.selectedTool?.primary_executor_name || next.primary_executor_name || "",
                    toolskill_content: S.selectedTool?.toolskill_content || "",
                };
            }
            else clearToolSelection();
        }
        renderTools();
        if (renderDetail) renderToolDetail();
    } catch (e) {
        U.toolList.innerHTML = `<div class="empty-state error">Failed to load tools: ${esc(e.message)}</div>`;
        addNotice({ kind: "resource_failed", title: "Tool load failed", text: e.message || "Unknown error" });
    } finally {
        renderToolActions();
    }
}

async function openTool(toolId, quiet = false) {
    if (!quiet) {
        setDrawerOpen(U.toolBackdrop, U.toolDrawer, true);
        U.toolEmpty.style.display = "none";
        U.toolDetail.innerHTML = '<div class="empty-state">Loading tool details...</div>';
    }
    try {
        const [tool, toolskill] = await Promise.all([
            ApiClient.getTool(toolId),
            ApiClient.getToolSkill(toolId).catch(() => ({ content: "", primary_executor_name: "" })),
        ]);
        S.selectedTool = {
            ...tool,
            primary_executor_name: toolskill?.primary_executor_name || tool?.primary_executor_name || "",
            toolskill_content: toolskill?.content || "",
        };
        S.toolDirty = false;
        renderTools();
        renderToolDetail();
    } catch (e) {
        U.toolDetail.innerHTML = `<div class="empty-state error">Failed to load tool details: ${esc(e.message)}</div>`;
        addNotice({ kind: "resource_failed", title: "Tool detail failed", text: e.message || "Unknown error" });
    } finally {
        renderToolActions();
    }
}

async function saveTool() {
    if (S.toolBusy) return;
    const selectedId = String(S.selectedTool?.tool_id || "").trim();
    const displayName = String(S.selectedTool?.display_name || selectedId || "Tool").trim();
    const enabled = !!S.selectedTool?.enabled;
    const actions = Array.isArray(S.selectedTool?.actions)
        ? Object.fromEntries(
            S.selectedTool.actions.map((action) => [
                action.action_id,
                Array.isArray(action.allowed_roles) ? [...action.allowed_roles] : [],
            ])
        )
        : {};
    if (!selectedId || !S.selectedTool) {
        addNotice({ kind: "resource_failed", title: "No tool selected", text: "Select a tool before saving." });
        showToast({ title: "保存失败", text: "未选择工具族", kind: "error" });
        return;
    }
    if (!S.toolDirty) {
        showToast({ title: "No pending changes", text: "This tool has no unsaved changes.", kind: "info", durationMs: 1800 });
        return;
    }
    S.toolBusy = true;
    renderToolActions();
    showToast({ title: "保存中", text: "正在保存工具权限，请稍候…", kind: "info", persistent: true });
    try {
        await ApiClient.updateToolPolicy(selectedId, {
            enabled,
            actions,
        });
        await ApiClient.reloadResources();
        await loadTools({ renderDetail: false });
        await openTool(selectedId, true);
        setToolDirty(false);
        addNotice({ kind: "resource_saved", title: "Tool saved", text: displayName || selectedId });
        showToast({ title: "保存成功", text: "工具权限已保存", kind: "success", durationMs: 2200 });
    } catch (e) {
        addNotice({ kind: "resource_failed", title: "Tool save failed", text: e.message || "Unknown error" });
        showToast({ title: "保存失败", text: e.message || "Unknown error", kind: "error", durationMs: 2600 });
    } finally {
        S.toolBusy = false;
        renderToolActions();
    }
}

async function refreshSkills() {
    const selectedId = S.selectedSkill?.skill_id || "";
    S.skillBusy = true;
    renderSkillActions();
    try {
        await ApiClient.reloadResources();
        await loadSkills();
        if (selectedId && S.skills.some((skill) => skill.skill_id === selectedId)) {
            await openSkill(selectedId);
        }
        addNotice({ kind: "resource_refreshed", title: "Skills refreshed", text: "Resource registry reloaded." });
    } catch (e) {
        addNotice({ kind: "resource_failed", title: "Skill refresh failed", text: e.message || "Unknown error" });
    } finally {
        S.skillBusy = false;
        renderSkillActions();
    }
}

async function refreshTools() {
    const selectedId = S.selectedTool?.tool_id || "";
    S.toolBusy = true;
    renderToolActions();
    try {
        await ApiClient.reloadResources();
        await loadTools();
        if (selectedId && S.tools.some((tool) => tool.tool_id === selectedId)) {
            await openTool(selectedId);
        }
        addNotice({ kind: "resource_refreshed", title: "Tools refreshed", text: "Resource registry reloaded." });
    } catch (e) {
        addNotice({ kind: "resource_failed", title: "Tool refresh failed", text: e.message || "Unknown error" });
    } finally {
        S.toolBusy = false;
        renderToolActions();
    }
}

function switchView(view) {
    const map = { ceo: U.viewCeo, tasks: U.viewTasks, skills: U.viewSkills, tools: U.viewTools, models: U.viewModels, "task-details": U.viewTaskDetails };
    const navView = view === "task-details" ? "tasks" : view;
    S.view = navView;
    U.nav.forEach((btn) => btn.classList.toggle("active", btn.dataset.view === navView));
    Object.entries(map).forEach(([key, el]) => {
        if (!el) return;
        const active = key === view;
        el.classList.toggle("active", active);
        el.style.display = active ? "" : "none";
    });
    if (view !== "task-details") {
        clearAgentSelection({ rerender: false });
        if (S.taskWs) {
            S.taskWs.close();
            S.taskWs = null;
        }
    }
    if (view === "tasks") void loadTasks();
    if (view === "skills") void loadSkills();
    if (view === "tools") void loadTools();
    if (view === "models") void loadModels();
}

function bind() {
    U.theme?.addEventListener("click", toggleTheme);
    U.nav.forEach((btn) => btn.addEventListener("click", () => switchView(btn.dataset.view)));
    U.backToTasks?.addEventListener("click", () => switchView("tasks"));
    U.artifactApply?.addEventListener("click", () => void applySelectedArtifact());
    U.ceoSend?.addEventListener("click", sendCeoMessage);
    U.ceoInput?.addEventListener("keydown", (e) => {
        if (e.key === "Enter" && !e.shiftKey) {
            e.preventDefault();
            sendCeoMessage();
        }
    });
    U.modelRefresh?.addEventListener("click", () => void loadModels());
    U.modelCreate?.addEventListener("click", startCreateModel);
    U.modelRolesCancel?.addEventListener("click", cancelModelRoleEditing);
    U.modelRolesSave?.addEventListener("click", () => void handleModelRoleEditorAction());
    U.modelSearch?.addEventListener("input", (e) => {
        S.modelCatalog.search = String(e.target.value || "");
        renderModelList();
    });
    U.modelList?.addEventListener("click", (e) => {
        const open = e.target.closest("[data-model-open]");
        if (open) {
            openModel(open.dataset.modelOpen);
            return;
        }
    });
    U.modelRoleEditors?.addEventListener("click", (e) => {
        const open = e.target.closest("[data-model-open]");
        if (open) {
            openModel(open.dataset.modelOpen);
            return;
        }
        if (!S.modelCatalog.roleEditing) return;
        const action = e.target.closest("[data-model-chain-action]");
        if (action) {
            const scope = String(action.dataset.scope || "");
            const index = Number(action.dataset.index || -1);
            const chain = modelScopeChain(scope);
            if (!scope || index < 0 || index >= chain.length) return;
            if (action.dataset.modelChainAction === "remove") {
                chain.splice(index, 1);
                updateRoleChainDraft(scope, chain);
            }
            return;
        }
    });
    U.modelRoleEditors?.addEventListener("dragstart", (e) => {
        if (!S.modelCatalog.roleEditing) return;
        const chainItem = e.target.closest("[data-model-chain-ref]");
        if (!chainItem) return;
        beginModelDrag(chainItem, {
            scope: String(chainItem.dataset.scope || ""),
            ref: String(chainItem.dataset.modelChainRef || ""),
            source: "chain",
        }, e.dataTransfer);
    });
    U.modelRoleEditors?.addEventListener("dragover", (e) => {
        if (!S.modelCatalog.roleEditing) return;
        const dragState = S.modelCatalog.dragState;
        if (!dragState?.ref) return;
        const chainList = e.target.closest("[data-model-chain-list]");
        if (!chainList) return;
        const scope = String(chainList.dataset.modelChainList || "");
        const allowDrop = dragState.source === "available" || scope === dragState.scope;
        if (!scope || !allowDrop) return;
        e.preventDefault();
        e.dataTransfer.dropEffect = dragState.source === "chain" ? "move" : "copy";
        clearModelDragDecorations();
        let targetItem = e.target.closest("[data-model-chain-ref]");
        if (targetItem && dragState.source === "chain" && scope === dragState.scope && String(targetItem.dataset.modelChainRef || "") === dragState.ref) {
            targetItem = null;
        }
        ensureModelDropPlaceholder(chainList, targetItem, e.clientY);
        startModelAutoScroll(chainList, e.clientY);
    });
    U.modelRoleEditors?.addEventListener("drop", (e) => {
        if (!S.modelCatalog.roleEditing) return;
        const dragState = S.modelCatalog.dragState;
        if (!dragState?.ref) return;
        const chainList = e.target.closest("[data-model-chain-list]");
        if (!chainList) return;
        const scope = String(chainList.dataset.modelChainList || "");
        const allowDrop = dragState.source === "available" || scope === dragState.scope;
        if (!scope || !allowDrop) return;
        e.preventDefault();
        const placeholder = chainList.querySelector('[data-model-drop-placeholder]');
        const children = [...chainList.children];
        const placeholderIndex = children.indexOf(placeholder);
        const targetIndex = placeholderIndex < 0
            ? children.filter((child) => child.matches?.('[data-model-chain-ref]') && String(child.dataset.modelChainRef || '') !== dragState.ref).length
            : children.slice(0, placeholderIndex).filter((child) => child.matches?.('[data-model-chain-ref]') && String(child.dataset.modelChainRef || '') !== dragState.ref).length;
        clearModelDragDecorations();
        stopModelAutoScroll();
        if (dragState.source === "chain") moveRoleChainItem(scope, dragState.ref, targetIndex);
        else insertRoleChainItem(scope, dragState.ref, targetIndex);
    });
    U.modelRoleEditors?.addEventListener("dragleave", (e) => {
        if (!S.modelCatalog.roleEditing) return;
        const dragState = S.modelCatalog.dragState;
        if (!dragState?.ref) return;
        const zone = e.target.closest("[data-model-chain-list]");
        if (!zone) return;
        const related = e.relatedTarget;
        if (related && zone.contains(related)) return;
        clearModelDragDecorations();
        stopModelAutoScroll();
    });
    U.modelRoleEditors?.addEventListener("dragend", finishModelDrag);
    U.modelList?.addEventListener("dragstart", (e) => {
        if (!S.modelCatalog.roleEditing) return;
        const availableItem = e.target.closest("[data-model-available-key]");
        if (!availableItem) return;
        beginModelDrag(availableItem, {
            ref: String(availableItem.dataset.modelAvailableKey || ""),
            source: "available",
        }, e.dataTransfer);
    });
    U.modelList?.addEventListener("dragover", (e) => {
        if (!S.modelCatalog.roleEditing) return;
        const dragState = S.modelCatalog.dragState;
        if (!dragState?.ref || dragState.source !== "chain") return;
        const availableList = e.target.closest("[data-model-available-list]");
        if (!availableList) return;
        e.preventDefault();
        e.dataTransfer.dropEffect = "move";
        clearModelDragDecorations();
        const targetItem = e.target.closest("[data-model-available-key]");
        highlightModelAvailableZone(availableList, targetItem);
        startModelAutoScroll(availableList, e.clientY);
    });
    U.modelList?.addEventListener("drop", (e) => {
        if (!S.modelCatalog.roleEditing) return;
        const dragState = S.modelCatalog.dragState;
        if (!dragState?.ref || dragState.source !== "chain") return;
        const availableList = e.target.closest("[data-model-available-list]");
        if (!availableList) return;
        e.preventDefault();
        clearModelDragDecorations();
        stopModelAutoScroll();
        removeRoleChainItem(dragState.scope, dragState.ref);
    });
    U.modelList?.addEventListener("dragleave", (e) => {
        if (!S.modelCatalog.roleEditing) return;
        const dragState = S.modelCatalog.dragState;
        if (!dragState?.ref) return;
        const zone = e.target.closest("[data-model-available-list]");
        if (!zone) return;
        const related = e.relatedTarget;
        if (related && zone.contains(related)) return;
        clearModelDragDecorations();
        stopModelAutoScroll();
    });
    U.modelList?.addEventListener("dragend", finishModelDrag);
    U.modelDetail?.addEventListener("submit", (e) => {
        if (e.target?.id !== "model-detail-form") return;
        e.preventDefault();
        void saveModelDetail();
    });
    U.modelDetail?.addEventListener("click", (e) => {
        const cancel = e.target.closest("[data-model-detail-cancel]");
        if (cancel) {
            clearModelSelection();
            return;
        }
        const controlBtn = e.target.closest("[data-model-control]");
        if (controlBtn) {
            const action = controlBtn.dataset.modelControl;
            if (action === "delete") {
                void deleteModelDetail(controlBtn.dataset.key);
                return;
            }
            const checkbox = U.modelDetail.querySelector('input[name="enabled"]');
            if (checkbox) {
                checkbox.checked = action === "enable";
                saveModelDetail();
            }
        }
    });
    U.modelDetail?.addEventListener("change", (e) => {
        const toggle = e.target.closest(".role-toggle");
        if (toggle && e.target instanceof HTMLInputElement && e.target.type === "checkbox") {
            toggle.classList.toggle("checked", e.target.checked);
        }
    });
    U.taskMultiToggle?.addEventListener("click", () => setMultiSelectMode(!S.multiSelectMode));
    U.taskFilterTrigger?.addEventListener("click", (e) => {
        e.stopPropagation();
        setTaskMenuOpen("filter", !S.taskFilterMenuOpen);
    });
    U.taskBatchTrigger?.addEventListener("click", (e) => {
        e.stopPropagation();
        setTaskMenuOpen("batch", !S.taskBatchMenuOpen);
    });
    U.taskFilterMenu?.querySelectorAll("[data-select-bucket]")?.forEach((button) => button.addEventListener("click", () => {
        S.selectedTaskIds = new Set(S.tasks.filter((task) => statusBucketMatches(task, button.dataset.selectBucket)).map((task) => task.task_id));
        closeTaskMenus();
        renderTasks();
    }));
    U.taskBatchMenu?.querySelectorAll("[data-batch-action]")?.forEach((button) => button.addEventListener("click", async () => {
        await runTaskBatchAction(button.dataset.batchAction);
    }));
    U.closeAgent?.addEventListener("click", () => clearAgentSelection());
    U.taskDetailBackdrop?.addEventListener("click", () => clearAgentSelection());
    [U.skillSearch, U.skillRisk, U.skillStatus].forEach((el) => el?.addEventListener(el.tagName === "INPUT" ? "input" : "change", renderSkills));
    U.skillRefresh?.addEventListener("click", () => void refreshSkills());
    U.skillSave?.addEventListener("click", () => void saveSkill());
    [U.toolSearch, U.toolStatus, U.toolRisk].forEach((el) => el?.addEventListener(el.tagName === "INPUT" ? "input" : "change", renderTools));
    U.toolRefresh?.addEventListener("click", () => void refreshTools());
    U.toolSave?.addEventListener("click", () => void saveTool());
    U.modelBackdrop?.addEventListener("click", clearModelSelection);
    U.skillBackdrop?.addEventListener("click", clearSkillSelection);
    U.toolBackdrop?.addEventListener("click", clearToolSelection);
    U.toastClose?.addEventListener("click", closeToast);
    U.confirmBackdrop?.addEventListener("click", (e) => {
        if (e.target === U.confirmBackdrop) closeConfirm();
    });
    U.confirmCancel?.addEventListener("click", () => closeConfirm());
    U.confirmAccept?.addEventListener("click", () => void acceptConfirm());
    document.addEventListener("click", (e) => {
        if (!(e.target instanceof Element)) return;
        if (!e.target.closest(".resource-select-shell")) closeResourceSelects();
        if (!e.target.closest(".toolbar-dropdown")) closeTaskMenus();
    });
    document.addEventListener("keydown", (e) => {
        if (e.key !== "Escape") return;
        if (closeResourceSelects({ restoreFocus: true })) return;
        if (S.confirmState) {
            closeConfirm();
            return;
        }
        if (S.taskFilterMenuOpen || S.taskBatchMenuOpen) {
            closeTaskMenus();
            return;
        }
        if (U.taskDetailDrawer?.classList.contains("is-open")) {
            clearAgentSelection();
            return;
        }
        if (S.modelCatalog.mode === "create" || S.modelCatalog.selectedModelKey) {
            clearModelSelection();
            return;
        }
        if (S.selectedSkill) clearSkillSelection();
        if (S.selectedTool) clearToolSelection();
    });
}

function init() {
    enhanceResourceSelects();
    bind();
    bindTreePan();
    icons();
    renderSkillActions();
    renderToolActions();
    void loadModels();
    void loadTasks();
    initCeoWs();
}

document.addEventListener("DOMContentLoaded", init);







