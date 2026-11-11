// 会话右上角的后台工具气泡：只依赖 GET/POST /tool-jobs 与 `ceo.tool_jobs` 帧。
// 本文件在 ws-router.js 之前加载，顶层只允许声明函数/常量，不得读写 S/U/document。

const CEO_BG_JOBS_RECONCILE_MS = 10000;

const CeoBackgroundJobsState = {
    sessionId: "",
    items: [],
    open: false,
    timerId: 0,
    reconcileTimerId: 0,
    requestToken: 0,
    stopping: new Set(),
    stopped: new Set(),
    finishedByCallKey: new Map(),
    lifecycleBound: false,
};

function ceoBgJobsEl(id) {
    return document.getElementById(String(id || ""));
}

function ceoBgJobsFormatSeconds(totalSeconds) {
    const seconds = Math.max(0, Math.floor(Number(totalSeconds) || 0));
    const hours = Math.floor(seconds / 3600);
    const minutes = Math.floor((seconds % 3600) / 60);
    const remain = seconds % 60;
    const pad = (value) => String(value).padStart(2, "0");
    return hours > 0 ? `${hours}:${pad(minutes)}:${pad(remain)}` : `${pad(minutes)}:${pad(remain)}`;
}

function ceoBgJobsElapsedText(startedAt, fallbackSeconds) {
    const fallback = Number(fallbackSeconds);
    let seconds = Number.isFinite(fallback) && fallback >= 0 ? Math.floor(fallback) : 0;
    const startedMs = Date.parse(String(startedAt || ""));
    if (Number.isFinite(startedMs)) {
        seconds = Math.max(0, Math.floor((Date.now() - startedMs) / 1000));
    }
    return ceoBgJobsFormatSeconds(seconds);
}

function ceoBgJobsActiveItems(items = CeoBackgroundJobsState.items) {
    const source = Array.isArray(items) ? items : [];
    return source.filter((item) => {
        if (!item || typeof item !== "object") return false;
        if (CeoBackgroundJobsState.stopped.has(String(item.job_id || ""))) return false;
        const status = String(item.status || "").trim().toLowerCase();
        return status === "running" || status === "stopping";
    });
}

// 账本里的 tool_call_id 可能是 "call_x|fc_y"，实时帧与 job 行只带前半段；
// 两边都按前半段比才能认得出同一次调用（与 org_graph_app.js 的 ceoToolCallKey 同口径）。
function ceoBgJobsCallKey(value = "") {
    const raw = String(value || "").trim();
    if (!raw) return "";
    const separator = raw.indexOf("|");
    return separator >= 0 ? raw.slice(0, separator) : raw;
}

function ceoBgJobsDisplayStatus(jobStatus = "") {
    const normalized = String(jobStatus || "").trim().toLowerCase();
    if (normalized === "success") return "success";
    if (["error", "timeout", "interrupted", "stopped_by_user"].includes(normalized)) return "error";
    return "";
}

function ceoBgJobsRememberFinished(item) {
    if (!item || typeof item !== "object") return;
    const key = ceoBgJobsCallKey(item.tool_call_id);
    const status = ceoBgJobsDisplayStatus(item.status);
    if (!key || !status) return;
    let elapsedSeconds = Number(item.elapsed_seconds);
    if (!Number.isFinite(elapsedSeconds)) {
        const startedAt = Date.parse(String(item.started_at || ""));
        const finishedAt = Date.parse(String(item.finished_at || ""));
        if (Number.isFinite(startedAt) && Number.isFinite(finishedAt) && finishedAt >= startedAt) {
            elapsedSeconds = Math.max(0, Math.floor((finishedAt - startedAt) / 1000));
        }
    }
    CeoBackgroundJobsState.finishedByCallKey.set(key, {
        status,
        jobStatus: String(item.status || "").trim().toLowerCase(),
        finishedAt: String(item.finished_at || ""),
        elapsedSeconds,
    });
}

function ceoBgJobsRememberFinishedItems(items) {
    (Array.isArray(items) ? items : []).forEach(ceoBgJobsRememberFinished);
}

function ceoBgJobsFinishedFor(toolCallId = "") {
    const key = ceoBgJobsCallKey(toolCallId);
    return key ? CeoBackgroundJobsState.finishedByCallKey.get(key) || null : null;
}

// 历史账本仍保留 background 状态（终态不回写），只把显示状态换成终态：
// 完成的工具行不再顶着一颗转动的时钟，成功画 √，失败/超时/中断画 ！。
function ceoBgJobsDecorateTurns(turns) {
    const source = Array.isArray(turns) ? turns : [];
    if (!CeoBackgroundJobsState.finishedByCallKey.size) return source;
    return source.map((turn) => {
        if (!turn || typeof turn !== "object" || !Array.isArray(turn.tools)) return turn;
        let changed = false;
        const tools = turn.tools.map((tool) => {
            const current = String(tool?.status || "").trim().toLowerCase();
            if (current !== "background" && current !== "background_running") return tool;
            const finished = ceoBgJobsFinishedFor(tool?.tool_call_id);
            if (!finished) return tool;
            changed = true;
            const elapsedSeconds = Number.isFinite(finished.elapsedSeconds)
                ? finished.elapsedSeconds
                : tool?.elapsed_seconds;
            return {
                ...tool,
                status: finished.status,
                finished_at: finished.finishedAt || tool?.finished_at || "",
                elapsed_seconds: elapsedSeconds,
                background_finished: true,
            };
        });
        return changed ? { ...turn, tools } : turn;
    });
}

function ceoBgJobsApplyToFeed() {
    if (!CeoBackgroundJobsState.finishedByCallKey.size) return;
    const doc = typeof document !== "undefined" ? document : null;
    if (!doc || typeof doc.querySelectorAll !== "function") return;
    let changed = false;
    Array.from(doc.querySelectorAll(".task-trace-live-steps .interaction-step[data-tool-call-id]")).forEach((item) => {
        if (!(item instanceof Element)) return;
        const current = String(item.dataset?.stepState || "").trim().toLowerCase();
        if (current !== "background" && current !== "background_running") return;
        const finished = ceoBgJobsFinishedFor(item.dataset?.toolCallId);
        if (!finished || typeof applyCeoToolStepState !== "function") return;
        applyCeoToolStepState(item, {
            status: finished.status,
            toolName: item.dataset?.toolName || "tool",
            detail: item.dataset?.detailText || "",
            toolCallId: item.dataset?.toolCallId || "",
            kind: item.dataset?.progressKind || "",
            allowEmptyOutput: true,
        });
        if (finished.finishedAt) item.dataset.finishedAt = finished.finishedAt;
        if (Number.isFinite(finished.elapsedSeconds) && finished.elapsedSeconds >= 0) {
            item.dataset.elapsedSeconds = String(finished.elapsedSeconds);
        }
        changed = true;
    });
    Array.from(doc.querySelectorAll(".task-trace-round-chip[data-tool-call-id]")).forEach((chip) => {
        if (!(chip instanceof Element)) return;
        const finished = ceoBgJobsFinishedFor(chip.dataset?.toolCallId);
        if (!finished) return;
        const iconWrap = chip.querySelector?.(".tool-status-icon");
        if (!iconWrap) return;
        const iconName = finished.status === "success" ? "check" : "alert-circle";
        const tone = finished.status === "success" ? "is-success" : "is-error";
        iconWrap.className = `tool-status-icon ${tone}`;
        if (typeof renderCeoToolIcon === "function") {
            renderCeoToolIcon(iconWrap, iconName);
        } else {
            iconWrap.innerHTML = `<i data-lucide="${iconName}"></i>`;
        }
        chip.dataset.bgJobStatus = finished.jobStatus;
        changed = true;
    });
    if (changed && typeof icons === "function") icons();
}

function ceoBgJobsRow(item) {
    const row = document.createElement("div");
    row.className = "ceo-bg-jobs-row";
    row.setAttribute("role", "listitem");
    const jobId = String(item.job_id || "").trim();
    row.dataset.bgJobId = jobId;
    const kind = String(item.kind || "").trim().toLowerCase();
    if (kind) row.dataset.bgJobKind = kind;

    const main = document.createElement("span");
    main.className = "ceo-bg-jobs-main";
    const label = document.createElement("span");
    label.className = "ceo-bg-jobs-label";
    const tool = document.createElement("span");
    tool.className = "ceo-bg-jobs-tool";
    // exec background=true 起的 OS 进程与超时交棒的工具作业同列一行，来源不同：
    // 前者没有工具终态、只能按 pid 整树终止，行首标成「exec 进程」避免误读。
    // 进程行优先显示创建时写入的 purpose；命令只在 title 里，不再占第二行。
    const processPurpose = kind === "process" ? String(item.purpose || "").trim() : "";
    if (kind === "process") {
        tool.textContent = processPurpose || "exec 进程";
        if (processPurpose) tool.title = processPurpose;
    } else {
        tool.textContent = String(item.tool_name || "tool").trim() || "tool";
    }
    label.appendChild(tool);
    // 常驻性质的第二眼标记：交棒的工具作业一定有终态，只有 detach 出去的进程没有。
    if (kind === "process") {
        const tag = document.createElement("span");
        tag.className = "ceo-bg-jobs-tag";
        tag.textContent = "常驻进程";
        label.appendChild(tag);
    }
    main.appendChild(label);
    const hint = String(item.hint || "").trim();
    if (hint) {
        if (kind === "process" && processPurpose) {
            main.title = hint;
        } else {
            const hintEl = document.createElement("span");
            hintEl.className = "ceo-bg-jobs-hint";
            hintEl.textContent = hint;
            hintEl.title = hint;
            main.appendChild(hintEl);
        }
    }

    const elapsed = document.createElement("span");
    elapsed.className = "ceo-bg-jobs-elapsed";
    elapsed.dataset.bgJobStarted = String(item.started_at || "");
    elapsed.textContent = ceoBgJobsElapsedText(item.started_at, item.elapsed_seconds);

    const action = document.createElement("button");
    action.type = "button";
    action.className = "ceo-bg-jobs-stop";
    action.dataset.bgJobStop = jobId;
    const stopping = String(item.status || "").trim().toLowerCase() === "stopping"
        || CeoBackgroundJobsState.stopping.has(jobId);
    action.textContent = stopping ? "停止中" : "停止";
    action.disabled = stopping;
    if (stopping) action.setAttribute("aria-busy", "true");

    row.appendChild(main);
    row.appendChild(elapsed);
    row.appendChild(action);
    return row;
}

function ceoBgJobsRender() {
    const root = ceoBgJobsEl("ceo-bg-jobs");
    if (!root) return;
    const items = ceoBgJobsActiveItems();
    CeoBackgroundJobsState.items = items;
    if (!items.length) {
        ceoBgJobsSetOpen(false);
        ceoBgJobsStopReconcile();
        const emptyCountEl = ceoBgJobsEl("ceo-bg-jobs-count");
        if (emptyCountEl) emptyCountEl.textContent = "0";
        root.hidden = true;
        return;
    }
    root.hidden = false;
    // exec background 进程没有“终态帧”：进程自己退出时只能靠读接口 prune。
    // 气泡非空时低频对账，清空即停，不常驻轮询。
    ceoBgJobsStartReconcile();
    const countEl = ceoBgJobsEl("ceo-bg-jobs-count");
    if (countEl) countEl.textContent = String(items.length);
    const list = ceoBgJobsEl("ceo-bg-jobs-list");
    if (!list) return;
    const scrollTop = list.scrollTop;
    while (list.firstChild) list.removeChild(list.firstChild);
    items.forEach((item) => list.appendChild(ceoBgJobsRow(item)));
    ceoBgJobsPosition();
    list.scrollTop = scrollTop;
    if (CeoBackgroundJobsState.open) ceoBgJobsStartTimer();
}

function ceoBgJobsPosition() {
    if (!CeoBackgroundJobsState.open) return;
    positionListPopover(
        ceoBgJobsEl("ceo-bg-jobs-toggle"),
        ceoBgJobsEl("ceo-bg-jobs-popover"),
        ceoBgJobsEl("ceo-bg-jobs-list"),
    );
}

function ceoBgJobsSetOpen(open) {
    const popover = ceoBgJobsEl("ceo-bg-jobs-popover");
    const toggle = ceoBgJobsEl("ceo-bg-jobs-toggle");
    const next = !!open && ceoBgJobsActiveItems().length > 0;
    CeoBackgroundJobsState.open = next;
    if (popover) popover.hidden = !next;
    if (toggle) toggle.setAttribute("aria-expanded", next ? "true" : "false");
    if (next) {
        ceoBgJobsPosition();
        ceoBgJobsStartTimer();
    } else {
        ceoBgJobsStopTimer();
    }
}

function ceoBgJobsClose() {
    const wasOpen = !!CeoBackgroundJobsState.open;
    ceoBgJobsSetOpen(false);
    return wasOpen;
}

function ceoBgJobsToggle() {
    ceoBgJobsSetOpen(!CeoBackgroundJobsState.open);
}

function ceoBgJobsUpdateElapsed() {
    const list = ceoBgJobsEl("ceo-bg-jobs-list");
    if (!list || typeof list.querySelectorAll !== "function") return;
    list.querySelectorAll("[data-bg-job-started]").forEach((node) => {
        const text = ceoBgJobsElapsedText(node.dataset?.bgJobStarted || "");
        if (node.textContent !== text) node.textContent = text;
    });
}

function ceoBgJobsStartTimer() {
    ceoBgJobsStopTimer();
    if (!CeoBackgroundJobsState.open || !ceoBgJobsActiveItems().length) return;
    CeoBackgroundJobsState.timerId = window.setInterval(ceoBgJobsUpdateElapsed, 1000);
    ceoBgJobsUpdateElapsed();
}

function ceoBgJobsStopTimer() {
    if (!CeoBackgroundJobsState.timerId) return;
    window.clearInterval(CeoBackgroundJobsState.timerId);
    CeoBackgroundJobsState.timerId = 0;
}

function ceoBgJobsStartReconcile() {
    if (CeoBackgroundJobsState.reconcileTimerId) return;
    CeoBackgroundJobsState.reconcileTimerId = window.setInterval(() => {
        if (!ceoBgJobsActiveItems().length) {
            ceoBgJobsStopReconcile();
            return;
        }
        void refreshCeoBackgroundJobs(
            CeoBackgroundJobsState.sessionId
            || (typeof activeSessionId === "function" ? activeSessionId() : "")
        );
    }, CEO_BG_JOBS_RECONCILE_MS);
}

function ceoBgJobsStopReconcile() {
    if (!CeoBackgroundJobsState.reconcileTimerId) return;
    window.clearInterval(CeoBackgroundJobsState.reconcileTimerId);
    CeoBackgroundJobsState.reconcileTimerId = 0;
}

function ceoBgJobsSetItems(items, sessionId = "") {
    const target = String(sessionId || CeoBackgroundJobsState.sessionId || "").trim();
    if (
        target
        && CeoBackgroundJobsState.sessionId
        && target !== CeoBackgroundJobsState.sessionId
        && typeof activeSessionId === "function"
        && target !== String(activeSessionId() || "").trim()
    ) {
        return;
    }
    if (target) CeoBackgroundJobsState.sessionId = target;
    CeoBackgroundJobsState.items = ceoBgJobsActiveItems(items);
    ceoBgJobsRender();}

function ceoBgJobsClear() {
    CeoBackgroundJobsState.items = [];
    ceoBgJobsStopReconcile();
    ceoBgJobsRender();
}

async function refreshCeoBackgroundJobs(sessionId = "") {
    const target = String(sessionId || (typeof activeSessionId === "function" ? activeSessionId() : "")).trim();
    if (!target) {
        ceoBgJobsClear();
        return;
    }
    if (CeoBackgroundJobsState.sessionId !== target) {
        CeoBackgroundJobsState.sessionId = target;
        CeoBackgroundJobsState.items = [];
        CeoBackgroundJobsState.stopping.clear();
        CeoBackgroundJobsState.stopped.clear();
        CeoBackgroundJobsState.finishedByCallKey.clear();
        ceoBgJobsSetOpen(false);
        ceoBgJobsRender();
    }
    const token = ++CeoBackgroundJobsState.requestToken;
    try {
        const data = await ApiClient.fetchCeoToolJobs(target);
        if (token !== CeoBackgroundJobsState.requestToken) return;
        if (typeof activeSessionId === "function" && target !== String(activeSessionId() || "").trim()) return;
        ceoBgJobsRememberFinishedItems(data?.finished);
        ceoBgJobsSetItems(data?.items, target);
        ceoBgJobsApplyToFeed();
    } catch (error) {
        if (token !== CeoBackgroundJobsState.requestToken) return;
        void error;
        ceoBgJobsClear();
    }
}

function handleCeoToolJobsFrame(payload, effectiveSessionId) {
    const target = String(effectiveSessionId || payload?.session_id || "").trim();
    if (!target) return;
    const active = typeof activeSessionId === "function" ? String(activeSessionId() || "").trim() : "";
    if (active && target !== active) return;
    ceoBgJobsRememberFinished(payload?.data?.finished);
    ceoBgJobsSetItems(payload?.data?.items, target);
    ceoBgJobsApplyToFeed();
}

async function ceoBgJobsStop(jobId) {
    const normalizedJobId = String(jobId || "").trim();
    if (!normalizedJobId) return;
    const sessionId = String(
        CeoBackgroundJobsState.sessionId
        || (typeof activeSessionId === "function" ? activeSessionId() : "")
    ).trim();
    if (!sessionId) return;
    CeoBackgroundJobsState.stopping.add(normalizedJobId);
    ceoBgJobsRender();
    try {
        await ApiClient.stopCeoToolJob(sessionId, normalizedJobId);
        CeoBackgroundJobsState.stopping.delete(normalizedJobId);
        CeoBackgroundJobsState.stopped.add(normalizedJobId);
        CeoBackgroundJobsState.items = CeoBackgroundJobsState.items.filter(
            (item) => String(item.job_id || "") !== normalizedJobId
        );
        ceoBgJobsRender();
    } catch (error) {
        CeoBackgroundJobsState.stopping.delete(normalizedJobId);
        const code = typeof ApiClient.getErrorCode === "function"
            ? String(ApiClient.getErrorCode(error) || "")
            : String(error?.code || "");
        if (code === "tool_job_finished" || Number(error?.status) === 409) {
            if (typeof showToast === "function") {
                showToast({ title: "工具已结束", text: "该后台工具已经完成。", kind: "info" });
            }
        } else if (typeof showToast === "function") {
            showToast({ title: "停止失败", text: "未能停止该后台工具，请重试。", kind: "error" });
        }
        await refreshCeoBackgroundJobs(sessionId);
    }
}

function bindCeoBackgroundJobs() {
    const toggle = ceoBgJobsEl("ceo-bg-jobs-toggle");
    if (toggle && toggle.dataset.bgJobsBound !== "true") {
        toggle.dataset.bgJobsBound = "true";
        toggle.addEventListener("click", (event) => {
            event.stopPropagation();
            ceoBgJobsToggle();
        });
    }
    const list = ceoBgJobsEl("ceo-bg-jobs-list");
    if (list && list.dataset.bgJobsBound !== "true") {
        list.dataset.bgJobsBound = "true";
        list.addEventListener("click", (event) => {
            const target = event.target instanceof Element ? event.target.closest("[data-bg-job-stop]") : null;
            if (!target) return;
            event.stopPropagation();
            void ceoBgJobsStop(target.dataset.bgJobStop);
        });
    }
    if (!CeoBackgroundJobsState.lifecycleBound) {
        CeoBackgroundJobsState.lifecycleBound = true;
        window.addEventListener("resize", ceoBgJobsPosition);
        window.addEventListener("pagehide", ceoBgJobsStopTimer);
        window.addEventListener("pagehide", ceoBgJobsStopReconcile);
    }
}
