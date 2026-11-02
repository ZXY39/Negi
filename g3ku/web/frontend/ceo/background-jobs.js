// 会话右上角的后台工具气泡：只依赖 GET/POST /tool-jobs 与 `ceo.tool_jobs` 帧。
// 本文件在 ws-router.js 之前加载，顶层只允许声明函数/常量，不得读写 S/U/document。

const CeoBackgroundJobsState = {
    sessionId: "",
    items: [],
    open: false,
    timerId: 0,
    requestToken: 0,
    stopping: new Set(),
    stopped: new Set(),
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

function ceoBgJobsRow(item) {
    const row = document.createElement("div");
    row.className = "ceo-bg-jobs-row";
    row.setAttribute("role", "listitem");
    const jobId = String(item.job_id || "").trim();
    row.dataset.bgJobId = jobId;

    const main = document.createElement("span");
    main.className = "ceo-bg-jobs-main";
    const tool = document.createElement("span");
    tool.className = "ceo-bg-jobs-tool";
    tool.textContent = String(item.tool_name || "tool").trim() || "tool";
    main.appendChild(tool);
    const hint = String(item.hint || "").trim();
    if (hint) {
        const hintEl = document.createElement("span");
        hintEl.className = "ceo-bg-jobs-hint";
        hintEl.textContent = hint.length > 48 ? `${hint.slice(0, 47)}…` : hint;
        hintEl.title = hint;
        main.appendChild(hintEl);
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
        const emptyCountEl = ceoBgJobsEl("ceo-bg-jobs-count");
        if (emptyCountEl) emptyCountEl.textContent = "0";
        root.hidden = true;
        return;
    }
    root.hidden = false;
    const countEl = ceoBgJobsEl("ceo-bg-jobs-count");
    if (countEl) countEl.textContent = String(items.length);
    const list = ceoBgJobsEl("ceo-bg-jobs-list");
    if (!list) return;
    while (list.firstChild) list.removeChild(list.firstChild);
    items.forEach((item) => list.appendChild(ceoBgJobsRow(item)));
    if (CeoBackgroundJobsState.open) ceoBgJobsStartTimer();
}

function ceoBgJobsSetOpen(open) {
    const popover = ceoBgJobsEl("ceo-bg-jobs-popover");
    const toggle = ceoBgJobsEl("ceo-bg-jobs-toggle");
    const next = !!open && ceoBgJobsActiveItems().length > 0;
    CeoBackgroundJobsState.open = next;
    if (popover) popover.hidden = !next;
    if (toggle) toggle.setAttribute("aria-expanded", next ? "true" : "false");
    if (next) {
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
        ceoBgJobsSetOpen(false);
        ceoBgJobsRender();
    }
    const token = ++CeoBackgroundJobsState.requestToken;
    try {
        const data = await ApiClient.fetchCeoToolJobs(target);
        if (token !== CeoBackgroundJobsState.requestToken) return;
        if (typeof activeSessionId === "function" && target !== String(activeSessionId() || "").trim()) return;
        ceoBgJobsSetItems(data?.items, target);
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
    ceoBgJobsSetItems(payload?.data?.items, target);
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
        window.addEventListener("pagehide", ceoBgJobsStopTimer);
    }
}
