"use strict";

// CEO 上下文压缩：转录标记的正常化、放置器、实时区分线、手动压缩轮询与终局过渡。
// 依赖 app.js 里的 S/U 与 render 入口；本文件必须在 org_graph_app.js 之前加载。

const CEO_COMPRESSION_POLL_MS = 1000;
// 连续这么多次轮询失败才放弃跟踪；单次超时不算（大会话收尾会占住事件循环数秒）。
const CEO_COMPRESSION_POLL_FAIL_LIMIT = 5;
// 缓存镜像里的 running 超过这个时长未被状态帧/轮询确认，按幽灵线处理。
const CEO_COMPRESSION_STALE_MS = 10 * 60 * 1000;
const CEO_COMPRESSION_DIVIDER_CLASS = "ceo-compression-divider";
const CEO_COMPRESSION_RETRY_LABEL = "重新压缩";
const CEO_COMPRESSION_RETRY_TITLE = "重新压缩当前上下文：会先停手，再按这颗模型的窗口压一次";
const CEO_COMPRESSION_TEXT = {
    running: "上下文压缩中",
    completed: "会话已压缩",
    paused: "压缩已暂停",
};

let ceoCompressionSyncQueued = false;

function normalizeCeoSnapshotCompression(compression = null) {
    if (!compression || typeof compression !== "object") return null;
    const status = String(compression?.status || "").trim().toLowerCase();
    if (!status) return null;
    const next = { status };
    const source = String(compression?.source || "").trim().toLowerCase();
    if (source) next.source = source;
    const text = String(compression?.text || "").trim();
    if (text) next.text = text;
    // 缓存里的 running 需要过期保护；seen_at 由前端在每次状态帧/轮询确认 running 时刷新。
    const seenAt = Number(compression?.seen_at);
    if (Number.isFinite(seenAt)) next.seen_at = seenAt;
    return next;
}

function normalizeCeoSnapshotCompressionMarker(marker = null) {
    if (!marker || typeof marker !== "object") return null;
    const state = String(marker?.state || "").trim().toLowerCase();
    if (state !== "completed" && state !== "paused") return null;
    const next = { state };
    const source = String(marker?.source || "").trim().toLowerCase();
    if (source) next.source = source;
    // turn_id 只用来认出这条线属于哪一回合：重建按转录顺序画，画完把 id 标在元素上，
    // 这样随后才到的实时帧不会在同一回合后面再画一条。
    const turnId = String(marker?.stats?.turn_id || "").trim();
    if (turnId) next.turn_id = turnId;
    // 0 是有效落点（本次压缩只折了更早的历史），不是“没有边界”。
    // 缺键/null/空串才表示没有边界；不要把 Number(null) 的 0 当成显式值。
    const rawBoundary = marker?.stats?.boundary_stage_index;
    if (rawBoundary !== undefined && rawBoundary !== null && rawBoundary !== "") {
        const boundary = Number(rawBoundary);
        if (Number.isFinite(boundary) && boundary >= 0) next.boundary_stage_index = boundary;
    }
    return next;
}

function activeCeoManualCompressionRunning() {
    return String(S.ceoContextCompressionStatus || "").trim().toLowerCase() === "running"
        && String(S.ceoContextCompressionSessionId || "") === String(activeSessionId() || "").trim();
}

function activeCeoSessionCompressionState() {
    // 回合外的手动压缩没有 inflight turn，进度只存在于本机发起状态里；自动压缩仍读快照。
    if (activeCeoManualCompressionRunning()) return { status: "running", source: "manual_context_compression" };
    const cacheEntry = getCeoSessionSnapshotCache(activeSessionId());
    // 缓存里的 running 必须有新鲜度：页面切走/断线/终局回调丢失时，不能让它永久复活。
    const cacheFreshness = (compression) => {
        const seenAt = Number(compression?.seen_at);
        if (Number.isFinite(seenAt)) return seenAt;
        // 测试桩的 Date 没有 parse；生产环境的 cached_at 是 ISO 串。
        const cachedAt = typeof Date.parse === "function"
            ? Date.parse(String(cacheEntry?.cached_at || ""))
            : NaN;
        return Number.isFinite(cachedAt) ? cachedAt : Date.now();
    };
    // 会话级载体优先：回合内自动压缩的进行中状态同时也发在 payload.compression 上，
    // 而 inflight 那条道对内联轮（heartbeat）会被回合闸门整帧丢掉。
    const sessionCompression = normalizeCeoSnapshotCompression(cacheEntry?.compression);
    if (String(sessionCompression?.status || "").trim().toLowerCase() === "running") {
        const freshness = cacheFreshness(sessionCompression);
        if (Number.isFinite(freshness) && Date.now() - freshness <= CEO_COMPRESSION_STALE_MS) return sessionCompression;
    }
    const inflightTurn = normalizeCeoSnapshotInflight(cacheEntry?.inflight_turn);
    const inflightStatus = String(inflightTurn?.status || "").trim().toLowerCase();
    if (inflightStatus && !["running", "in_progress", "active"].includes(inflightStatus)) return null;
    const compression = normalizeCeoSnapshotCompression(inflightTurn?.compression);
    if (!compression) return null;
    if (String(compression.status || "").trim().toLowerCase() !== "running") return null;
    const freshness = cacheFreshness(compression);
    return Number.isFinite(freshness) && Date.now() - freshness <= CEO_COMPRESSION_STALE_MS ? compression : null;
}

function buildCeoCompressionPauseGlyph(withSpinnerArc) {
    const className = withSpinnerArc
        ? "ceo-compression-divider-spinner"
        : "ceo-compression-divider-spinner is-bars-only";
    return `<span class="${className}" aria-hidden="true"><i></i><i></i></span>`;
}

function buildCeoCompressionDividerControl(state, interactive) {
    if (state === "running") {
        const glyph = buildCeoCompressionPauseGlyph(true);
        if (interactive) {
            return `<button type="button" class="ceo-compression-divider-action" data-ceo-compress-pause
                   aria-label="暂停压缩" title="暂停压缩">${glyph}</button>`;
        }
        return `<span class="ceo-compression-divider-icon">${glyph}</span>`;
    }
    if (state === "paused") {
        return `<span class="ceo-compression-divider-icon">${buildCeoCompressionPauseGlyph(false)}</span>`;
    }
    // 完成态按需求只留文案，不再挂图标。
    return "";
}

function buildCeoCompressionRetryControl() {
    return `<button type="button" class="ceo-compression-divider-action ceo-compression-divider-retry"
           data-ceo-compress-retry aria-label="${CEO_COMPRESSION_RETRY_LABEL}"
           title="${CEO_COMPRESSION_RETRY_TITLE}">${CEO_COMPRESSION_RETRY_LABEL}</button>`;
}

function ceoCompressionDividerInner(state, { interactive = false, retry = false } = {}) {
    return `<div class="ceo-compression-divider-inner"><span>${CEO_COMPRESSION_TEXT[state]}</span>${
        retry ? buildCeoCompressionRetryControl() : buildCeoCompressionDividerControl(state, interactive)
    }</div>`;
}

function syncCeoCompressionRetryAffordances() {
    if (!U.ceoFeed || typeof U.ceoFeed.querySelectorAll !== "function") return;
    const rows = Array.from(U.ceoFeed.querySelectorAll(`.${CEO_COMPRESSION_DIVIDER_CLASS}`));
    const last = rows.length ? rows[rows.length - 1] : null;
    rows.forEach((row) => {
        const state = String(row.dataset.ceoCompressionState || "");
        const eligible = row === last && state === "paused";
        const current = row.dataset.ceoCompressionRetry === "1";
        if (eligible === current) return;
        row.dataset.ceoCompressionRetry = eligible ? "1" : "";
        row.innerHTML = ceoCompressionDividerInner(state, { retry: eligible });
    });
}

function buildCeoCompressionDividerElement(state, { interactive = true, turnId = "", boundary = "", origin = "" } = {}) {
    if (!CEO_COMPRESSION_TEXT[state]) return null;
    const el = document.createElement("div");
    el.className = `message system ${CEO_COMPRESSION_DIVIDER_CLASS} is-${state}`;
    el.dataset.ceoCompressionState = state;
    if (turnId) el.dataset.ceoCompressionTurn = turnId;
    if (boundary !== "" && boundary !== null) el.dataset.ceoCompressionBoundary = String(boundary);
    if (origin) el.dataset.ceoCompressionOrigin = String(origin);
    el.setAttribute("role", "status");
    el.setAttribute("aria-live", state === "running" ? "polite" : "off");
    el.innerHTML = ceoCompressionDividerInner(state, { interactive });
    return el;
}

function appendCeoCompressionDivider(state, options = {}) {
    const { anchorEl = null, origin = "" } = options;
    if (!U.ceoFeed) return null;
    const el = buildCeoCompressionDividerElement(state, { ...options, origin });
    if (!el) return null;
    const anchorParent = anchorEl?.parentNode || null;
    mutateCeoFeed(() => {
        if (anchorParent) {
            // 落在自己那一回合之后，而不是 feed 尾部：回合内的压缩线是收尾时才落盘的，
            // 追到尾部就会把「压缩之后继续跑出来的内容」整段留在线上方。
            anchorParent.insertBefore(el, anchorEl.nextSibling || null);
        } else {
            ceoFeedAppendHost().appendChild(el);
        }
        icons();
    }, { scrollMode: "preserve" });
    syncCeoCompressionRetryAffordances();
    return el;
}

function findCeoCompressionAnchor(turnId) {
    if (!turnId || !U.ceoFeed) return null;
    const prefixes = [`turn:${turnId}`, `m:${turnId}:`];
    let match = null;
    Array.from(U.ceoFeed.children || []).forEach((el) => {
        const key = String(el.getAttribute?.("data-ceo-key") || "");
        if (prefixes.some((prefix) => key === prefix || key.startsWith(prefix))) match = el;
    });
    return match;
}

function ceoCompressionDividerExists(turnId, state, boundary = "") {
    if (!turnId || !U.ceoFeed || typeof U.ceoFeed.querySelectorAll !== "function") return false;
    const wantedBoundary = boundary === null || boundary === undefined ? "" : String(boundary);
    return Array.from(U.ceoFeed.querySelectorAll(`.${CEO_COMPRESSION_DIVIDER_CLASS}`)).some(
        (el) => String(el.dataset?.ceoCompressionTurn || "") === turnId
            && String(el.dataset?.ceoCompressionState || "") === state
            && String(el.dataset?.ceoCompressionBoundary || "") === wantedBoundary,
    );
}

function removeCeoCompressionLiveDivider() {
    if (!U.ceoFeed) return false;
    const selector = `.${CEO_COMPRESSION_DIVIDER_CLASS}.is-running`;
    const first = typeof U.ceoFeed.querySelector === "function" ? U.ceoFeed.querySelector(selector) : null;
    const all = typeof U.ceoFeed.querySelectorAll === "function"
        ? Array.from(U.ceoFeed.querySelectorAll(selector))
        : [];
    // 测试桩的 querySelector 会造节点、querySelectorAll 不会；两边取并集，
    // 真实 DOM 里 Set 去重，桩里也能把 querySelector 造出的那条摘掉。
    const rows = [...new Set([first, ...all].filter(Boolean))];
    if (!rows.length) return false;
    rows.forEach((el) => el?.remove?.());
    return true;
}

function syncCeoCompressionDivider() {
    if (!U.ceoFeed || typeof U.ceoFeed.querySelector !== "function") return;
    // 整页重建/回合收尾会在批次中间写缓存并触发这里；若当场 append，线会落在
    // 第一条已渲染消息之后，随后 79 条消息都长在它下面。批次内只记位，等最外层
    // 批次结束再挂，线才始终在 feed 尾部。
    if (ceoFeedBatchDepth > 0) {
        ceoCompressionSyncQueued = true;
        return;
    }
    ceoCompressionSyncQueued = false;
    ensureCeoContextCompressionPolling();
    const state = activeCeoSessionCompressionState();
    const running = !!state;
    const existing = U.ceoFeed.querySelector(`.${CEO_COMPRESSION_DIVIDER_CLASS}.is-running`);
    if (running && !existing) {
        // 只有手动压缩那条线挂暂停按钮：它打的是 /compress-context/cancel，而回合内的
        // 自动压缩由那条端点返回 409（它只认手动任务的状态），点了只会得到一个假承诺。
        // 自动压缩的停手本来就是 composer 那颗主暂停键（服务端 pause() 掐同一颗代际）。
        appendCeoCompressionDivider("running", {
            interactive: String(state.source || "").trim().toLowerCase() === "manual_context_compression",
        });
        // 只有本来就在跟随最新时才钉底：这条线现在也由 renderCeoSnapshot 在每次整页重建后
        // 重挂，若无条件钉底会把正在往上翻历史的用户每次重建都拽回底部。
        if (S.ceoFeedFollowLatest !== false) scrollCeoFeedToBottom();
        syncCeoCompressionRetryAffordances();
        return;
    }
    if (!running && existing) removeCeoCompressionLiveDivider();
    syncCeoCompressionRetryAffordances();
}

function ceoCompressionStagesAroundBoundary(stages, boundary) {
    const indexes = (Array.isArray(stages) ? stages : [])
        .map((stage) => Number(stage?.stage_index))
        .filter((value) => Number.isFinite(value));
    return {
        total: indexes.length,
        pre: indexes.filter((value) => value <= boundary).length,
    };
}

function resolveCeoCompressionPlacement(rule, turn = null) {
    const boundary = Number(rule?.boundary);
    if (!Number.isFinite(boundary)) return { mode: "row-position" };
    if (boundary === 0) return { mode: "before-turn" };
    const { total, pre } = ceoCompressionStagesAroundBoundary(turn?.stages, boundary);
    if (!total) {
        return turn?.live && !turn?.finalized
            ? { mode: "split", headWindowTo: null, tailWindowFrom: null }
            : { mode: "after-turn" };
    }
    if (pre === 0) return { mode: "before-turn" };
    if (pre >= total) {
        return { mode: "after-turn", headWindowTo: boundary, tailWindowFrom: boundary + 1 };
    }
    return { mode: "split", headWindowTo: boundary, tailWindowFrom: boundary + 1 };
}

function freezeCeoCompressionHeadKey(el, boundary) {
    const key = el?.getAttribute?.("data-ceo-key") || "";
    if (key && !key.endsWith(`#pre-${boundary}`)) el.setAttribute("data-ceo-key", `${key}#pre-${boundary}`);
}

function settleCeoCompressionHeadText(turn) {
    const textEl = turn?.textEl;
    if (!textEl) return;
    if (!String(textEl.textContent || "").trim()) {
        textEl.innerHTML = "";
        textEl.hidden = true;
        return;
    }
    textEl.classList?.remove?.("pending");
    textEl.classList?.remove?.("assistant-text-loading");
    syncCeoAssistantLoadingAria(textEl);
    syncCeoTurnLoadingOnlyState(turn, false);
}

function splitCeoTurnAtCompression(turn, boundary, state) {
    if (!turn?.el?.parentNode || !Number.isFinite(boundary) || boundary < 0) return false;
    if (turn.finalized) return false;
    // 这条规则已经在这个回合身上落过一次（重建吸收、或同一帧早前切过）：再切就是重复的线。
    if (turn.compressionBoundary === boundary) return true;
    const stages = turn.lastExecutionTraceSummary?.stages;
    const placement = resolveCeoCompressionPlacement({ boundary }, {
        stages,
        live: true,
        finalized: false,
    });
    if (placement.mode === "row-position") return false;
    const dividerState = CEO_COMPRESSION_TEXT[state] ? state : "completed";
    const turnId = String(turn.turnId || "");
    const divider = buildCeoCompressionDividerElement(dividerState, {
        interactive: false,
        turnId,
        boundary: boundary > 0 ? boundary : "",
    });
    if (!divider) return false;
    if (placement.mode === "before-turn") {
        // 这一回合的阶段全在压缩之后（或 boundary 0）：线挪到气泡之前，不切。
        turn.compressionBoundary = boundary;
        mutateCeoFeed(() => {
            turn.el.parentNode.insertBefore(divider, turn.el);
            icons();
        }, { scrollMode: "preserve" });
        syncCeoCompressionRetryAffordances();
        return true;
    }
    const continuation = createPendingCeoTurn(turn.source, { scrollMode: "preserve" });
    if (!continuation?.el?.parentNode) return false;
    const head = turn;
    const summary = head.lastExecutionTraceSummary;
    // 轨道未知时两头都不设窗口：被折的那几张卡随时可能才第一次到达，按 boundary 把它们
    // 筛掉就等于让它们彻底不出现；宁可让续跑段先把它们画出来，等轨道真到了再补切。
    const knownStages = !!ceoCompressionStagesAroundBoundary(stages, boundary).total;
    if (knownStages) head.stageWindowTo = boundary;
    freezeCeoCompressionHeadKey(head.el, boundary);
    // 头段不再收帧：回合身份让给续跑段，之后的 finalize / 插话锚点都落在它身上。
    head.finalized = true;
    continuation.stageWindowFrom = knownStages ? boundary + 1 : null;
    continuation.turnId = head.turnId;
    continuation.compressionBoundary = boundary;
    // 还没切的那一半先记在续跑段身上：轨道第一次到达时由
    // reconcileDeferredCeoCompressionSplit 把 <= boundary 的卡补回头段。
    if (!knownStages) continuation.compressionHead = head;
    // 续跑段立刻顶上门牌：视图状态还原与"线插在哪一回合后面"的查找都按 key 认，
    // 等下一帧才补的话，中间这段时间这条气泡是匿名的。
    if (head.turnId && continuation.el?.setAttribute) {
        continuation.el.setAttribute("data-ceo-key", `turn:${head.turnId}`);
    }
    S.ceoPendingTurns.push(continuation);
    mutateCeoFeed(() => {
        const parent = head.el.parentNode;
        parent.removeChild(continuation.el);
        parent.insertBefore(divider, head.el.nextSibling || null);
        parent.insertBefore(continuation.el, divider.nextSibling || null);
        icons();
    }, { scrollMode: "preserve" });
    renderCeoStageTraceIntoTurn(head, summary);
    settleCeoCompressionHeadText(head);
    renderCeoStageTraceIntoTurn(continuation, summary);
    syncCeoCompressionRetryAffordances();
    return true;
}

function ceoLiveTurnForCompressionMarker(turnId = "") {
    const normalizedTurnId = normalizeCeoTurnId(turnId);
    if (!normalizedTurnId) return null;
    // 必须自己核对 id：getActiveCeoTurn(null, turnId) 在 id 对不上时会退化成
    // "随便给我一条活跃回合"，拿它当命中就等于把线切到了别的回合身上。
    const matched = getActiveCeoTurn(null, normalizedTurnId);
    if (matched && normalizeCeoTurnId(matched.turnId) === normalizedTurnId) return matched;
    const anonymous = S.ceoPendingTurns.filter(
        (turn) => turn && !turn.finalized && !normalizeCeoTurnId(turn.turnId),
    );
    if (anonymous.length !== 1) return null;
    const claimed = anonymous[0];
    claimed.turnId = normalizedTurnId;
    if (claimed.el?.setAttribute) claimed.el.setAttribute("data-ceo-key", `turn:${normalizedTurnId}`);
    return claimed;
}

function reconcileDeferredCeoCompressionSplit(turn) {
    const head = turn?.compressionHead;
    if (!head || turn.finalized) return false;
    const boundary = Number(turn.compressionBoundary);
    if (!Number.isFinite(boundary) || boundary < 0) return false;
    const summary = turn.lastExecutionTraceSummary;
    const { total, pre } = ceoCompressionStagesAroundBoundary(summary?.stages, boundary);
    // 轨道还没到、或一张被折的卡都没有（压缩折的是更早的历史）：整段留在续跑段就是对的。
    if (!total || !pre) return false;
    turn.compressionHead = null;
    head.stageWindowTo = boundary;
    turn.stageWindowFrom = boundary + 1;
    mutateCeoFeed(() => {
        renderCeoStageTraceIntoTurn(head, summary);
        settleCeoCompressionHeadText(head);
        renderCeoStageTraceIntoTurn(turn, summary);
    }, { scrollMode: "preserve" });
    return true;
}

function applyCeoCompressionMarker(sessionId = "", marker = null) {
    // 终态那条线的本体是刚落盘的转录行；这一帧只负责让它在实时车道当场出现，
    // 不等整页重建。事件在写盘成功之后才广播，所以"画了线"不会先于"有这条行"。
    const state = String((marker || {}).state || "").trim().toLowerCase();
    if (state !== "completed" && state !== "paused") return false;
    const key = String(sessionId || "").trim();
    if (!key || key !== String(activeSessionId() || "").trim()) return false;
    if (!U.ceoFeed) return false;
    removeCeoCompressionLiveDivider();
    const stats = (marker || {}).stats || {};
    const turnId = String((marker || {}).turn_id || stats.turn_id || "").trim();
    const boundary = Number(stats.boundary_stage_index);
    // boundary 0 也是有效落点（本次压缩折的是更早的历史，整条回合都在线下方）：
    // 把它当成"没有边界"就会退回追 feed 尾部，压缩之后跑出来的内容全在线上方。
    if (state === "completed" && Number.isFinite(boundary) && boundary >= 0) {
        const turn = ceoLiveTurnForCompressionMarker(turnId);
        if (turn && splitCeoTurnAtCompression(turn, boundary, state)) return true;
    }
    const dividerBoundary = Number.isFinite(boundary) && boundary > 0 ? boundary : "";
    if (ceoCompressionDividerExists(turnId, state, dividerBoundary)) return true;
    const anchorEl = findCeoCompressionAnchor(turnId);
    appendCeoCompressionDivider(state, {
        interactive: false,
        anchorEl,
        turnId,
        boundary: dividerBoundary,
    });
    // 钉底只在"线就是最后一条"时成立：插进历史里的那条不该把正在往上翻的人拽回底部。
    if (!anchorEl && S.ceoFeedFollowLatest !== false) scrollCeoFeedToBottom();
    return true;
}

function applyCeoInflightCompressionRule(messages = [], inflightTurn = null) {
    const turnId = normalizeCeoTurnId(inflightTurn?.turn_id || "");
    if (!turnId || !U.ceoFeed || typeof U.ceoFeed.querySelectorAll !== "function") return;
    const rules = (buildCeoCompressionPlan(messages).rulesByTurn.get(turnId) || [])
        .filter((rule) => rule.state === "completed" && Number.isFinite(Number(rule.boundary)));
    if (!rules.length) return;
    rules.forEach((rule) => {
        const boundary = Number(rule.boundary);
        const turn = getActiveCeoTurn(null, turnId);
        if (!turn) return;
        const dividerBoundary = boundary > 0 ? String(boundary) : "";
        const candidates = Array.from(U.ceoFeed.querySelectorAll(`.${CEO_COMPRESSION_DIVIDER_CLASS}`))
            .filter((el) => String(el.dataset?.ceoCompressionTurn || "") === turnId
                && String(el.dataset?.ceoCompressionState || "") === "completed"
                && String(el.dataset?.ceoCompressionBoundary || "") === dividerBoundary);
        const misplaced = candidates.find((el) => el.dataset?.ceoCompressionOrigin === "row")
            || candidates[0];
        // 先切后摘：切不开（比如这一帧还没有阶段轨道）就保留按行位置画的那条，
        // 宁可位置退一档也不能让线凭空消失。
        const split = splitCeoTurnAtCompression(turn, boundary, rule.state);
        if (split && misplaced?.parentNode) misplaced.remove();
    });
}

function stopCeoContextCompressionPolling() {
    if (S.ceoContextCompressionPollId !== null) {
        window.clearInterval(S.ceoContextCompressionPollId);
        S.ceoContextCompressionPollId = null;
    }
}

function ceoContextCompressionTerminalText(status, reason) {
    const normalizedReason = String(reason || "").trim().toLowerCase();
    if (normalizedReason === "baseline_advanced") {
        // 摘要算完了，但基线在这期间被另一个回合改写过：不落盘也不落区分线，
        // 说成「没有可压缩的历史」会把人引向错误的下一步。
        return "压缩期间这条会话的上下文基线被另一个回合改写了。为避免丢掉那条回合的内容，本次摘要没有落盘；请等这条回复结束后再压缩一次。";
    }
    if (status === "not_needed") {
        return "没有可压缩的历史，或所选模型没有可用的上下文窗口（需大于 25000 token）。";
    }
    return `压缩任务未能开始或中途中止：${String(reason || "unknown")}`;
}

function applyCeoContextCompressionStatus(payload = {}) {
    const status = String(payload?.status || "idle").trim().toLowerCase();
    const sessionId = String(S.ceoContextCompressionSessionId || activeSessionId() || "").trim();
    const previous = String(S.ceoContextCompressionStatus || "").trim().toLowerCase();
    S.ceoContextCompressionStatus = status;
    S.ceoContextCompressionCancelRequested = payload?.cancel_requested === true;
    if (status !== "running") {
        stopCeoContextCompressionPolling();
        S.ceoContextCompressionSessionId = "";
    }
    if (status === "running") {
        mirrorCeoContextCompression(sessionId, {
            status: "running",
            source: "manual_context_compression",
        });
    } else {
        if (previous === "running") {
            // 先就地改成终态，避免“先删线、再等重连”的空白窗口。
            if (status === "completed" || status === "paused") {
                convertCeoRunningDividerToTerminal(status);
            } else {
                removeCeoCompressionLiveDivider();
            }
        }
        // 终局写 null 清缓存，防止 activeCeoSessionCompressionState 从旧镜像复活 running。
        if (sessionId) setCeoSessionSnapshotCache(sessionId, { compression: null });
    }
    if (previous === "running" && status !== "running") {
        if (status === "not_needed" || status === "failed") {
            // 这两种终局都不会落区分线，不解释一句的话用户看到的就是「线莫名其妙没了」。
            showToast({
                title: "上下文未压缩",
                text: ceoContextCompressionTerminalText(status, payload?.reason),
                kind: "warn",
            });
        }
        // 区分线由转录里的持久标记行承载；终局后重开会话才能拿到它，这里强制刷新一次。
        S.ceoFeedRenderSignature = "";
        if (sessionId) void reloadCeoSessionSnapshot(sessionId);
    }
    syncCeoCompressionDivider();
    syncCeoComposerUsageOutline();
}

function startCeoContextCompressionPolling(sessionId) {
    stopCeoContextCompressionPolling();
    const key = String(sessionId || "").trim();
    if (!key) return;
    S.ceoContextCompressionPollFails = 0;
    let inFlight = false;
    S.ceoContextCompressionPollId = window.setInterval(async () => {
        if (String(activeSessionId() || "").trim() !== key) {
            // 切走会话时停轮询；切回来由 ensureCeoContextCompressionPolling 续上。
            stopCeoContextCompressionPolling();
            return;
        }
        // 大会话收尾会把事件循环占住数秒，单次请求可能叠在上一条后面，别并发打出去。
        if (inFlight) return;
        inFlight = true;
        try {
            const payload = await ApiClient.getCeoContextCompression(key);
            S.ceoContextCompressionPollFails = 0;
            applyCeoContextCompressionStatus(payload || {});
        } catch (error) {
            // 一次超时不能当作终局：停掉轮询就等于永久收不到 completed，区分线会挂在
            // 「压缩中」直到刷新。攒到连续多次失败才放弃。
            S.ceoContextCompressionPollFails += 1;
            if (S.ceoContextCompressionPollFails >= CEO_COMPRESSION_POLL_FAIL_LIMIT) {
                stopCeoContextCompressionPolling();
            }
        } finally {
            inFlight = false;
        }
    }, CEO_COMPRESSION_POLL_MS);
}

function ensureCeoContextCompressionPolling() {
    const key = String(S.ceoContextCompressionSessionId || "").trim();
    if (!key || S.ceoContextCompressionPollId !== null) return;
    if (String(S.ceoContextCompressionStatus || "").trim().toLowerCase() !== "running") return;
    if (String(activeSessionId() || "").trim() !== key) return;
    startCeoContextCompressionPolling(key);
}

function mirrorCeoContextCompression(sessionId, compression) {
    const key = String(sessionId || "").trim();
    if (!key) return;
    const normalized = normalizeCeoSnapshotCompression(compression);
    if (!normalized) {
        setCeoSessionSnapshotCache(key, { compression: null });
        return;
    }
    setCeoSessionSnapshotCache(key, {
        compression: { ...normalized, seen_at: Date.now() },
    });
}

async function beginCeoContextCompression(sessionId) {
    const key = String(sessionId || "").trim();
    if (!key) return;
    if (String(S.ceoContextCompressionStatus || "").trim().toLowerCase() === "running") return;
    S.ceoContextCompressionSessionId = key;
    S.ceoContextCompressionStatus = "running";
    S.ceoContextCompressionCancelRequested = false;
    mirrorCeoContextCompression(key, {
        status: "running",
        source: "manual_context_compression",
    });
    syncCeoCompressionDivider();
    try {
        const payload = await ApiClient.startCeoContextCompression(key);
        applyCeoContextCompressionStatus(payload || {});
        if (String(S.ceoContextCompressionStatus || "").trim().toLowerCase() === "running") {
            startCeoContextCompressionPolling(key);
        }
    } catch (error) {
        S.ceoContextCompressionStatus = "idle";
        S.ceoContextCompressionSessionId = "";
        syncCeoCompressionDivider();
        showToast({
            title: "压缩失败",
            text: String(error?.message || "上下文压缩未能开始。"),
            kind: "error",
        });
    }
}

function requestCeoContextCompressionPause() {
    const key = String(S.ceoContextCompressionSessionId || activeSessionId() || "").trim();
    if (!key) return;
    openConfirm({
        title: "暂停上下文压缩",
        text: "正在压缩上下文。暂停后本次压缩不会写回摘要，历史保持原样。",
        confirmLabel: "暂停压缩",
        confirmKind: "danger",
        onConfirm: async () => {
            S.ceoContextCompressionCancelRequested = true;
            syncCeoCompressionDivider();
            try {
                const payload = await ApiClient.cancelCeoContextCompression(key);
                applyCeoContextCompressionStatus(payload || {});
            } catch (error) {
                S.ceoContextCompressionCancelRequested = false;
                showToast({
                    title: "暂停失败",
                    text: String(error?.message || "当前没有可暂停的上下文压缩。"),
                    kind: "error",
                });
            }
        },
    });
}

function adoptCeoContextCompression(sessionId, compression) {
    // 回合外的手动压缩没有 inflight turn，进行中状态只活在服务端会话级 `compression` 里
    // （state 快照与 snapshot.ceo 都带）。刷新会清空本机 S.ceoContextCompression*，不认领
    // 回来就再也看不到区分线，也收不到终局——必须再刷一次才看得到「会话已压缩」。
    const key = String(sessionId || "").trim();
    const normalized = normalizeCeoSnapshotCompression(compression);
    if (!key) return;
    if (String(normalized?.status || "").trim().toLowerCase() !== "running") return;
    if (String(normalized?.source || "").trim().toLowerCase() !== "manual_context_compression") return;
    if (String(S.ceoContextCompressionSessionId || "").trim() === key
        && String(S.ceoContextCompressionStatus || "").trim().toLowerCase() === "running") {
        return;
    }
    S.ceoContextCompressionSessionId = key;
    S.ceoContextCompressionStatus = "running";
    S.ceoContextCompressionCancelRequested = false;
    mirrorCeoContextCompression(key, normalized);
    syncCeoCompressionDivider();
}

function planHistoryCompressionSegments(stages, rules = []) {
    const before = [];
    const splits = [];
    const after = [];
    let previousBoundary = null;
    [...rules]
        .filter((rule) => rule && rule.state === "completed" && Number.isFinite(Number(rule.boundary)))
        .sort((left, right) => Number(left.boundary) - Number(right.boundary))
        .forEach((rule) => {
            const placement = resolveCeoCompressionPlacement(rule, {
                stages,
                live: false,
                finalized: true,
            });
            if (placement.mode === "before-turn") {
                before.push(rule);
                return;
            }
            if (placement.mode !== "split") {
                after.push(rule);
                return;
            }
            const boundary = Number(rule.boundary);
            if (previousBoundary !== null && boundary <= previousBoundary) return;
            splits.push({
                rule,
                boundary,
                windowFrom: previousBoundary === null ? null : previousBoundary + 1,
                windowTo: boundary,
            });
            previousBoundary = boundary;
        });
    return { before, splits, after };
}

function createHistoryCompressionHead(turnId, split) {
    const head = createPendingCeoTurn("history", { scrollMode: "preserve" });
    if (!head) return null;
    head.stageWindowFrom = split.windowFrom;
    head.stageWindowTo = split.windowTo;
    head.finalized = true;
    if (turnId && head.el?.setAttribute) {
        head.el.setAttribute("data-ceo-key", `turn:${turnId}#pre-${split.boundary}`);
    }
    S.ceoPendingTurns.push(head);
    return head;
}

function buildHistoryCompressionDivider(rule, turnId) {
    return buildCeoCompressionDividerElement(rule?.state || "completed", {
        interactive: false,
        turnId,
        boundary: Number(rule?.boundary) > 0 ? Number(rule.boundary) : "",
    });
}

function placeHistoryCompressionAroundElement(el, rules, turnId) {
    if (!el?.parentNode || !rules?.length) return false;
    const segments = planHistoryCompressionSegments([], rules);
    const parent = el.parentNode;
    [...segments.before].reverse().forEach((rule) => {
        const divider = buildHistoryCompressionDivider(rule, turnId);
        if (divider) parent.insertBefore(divider, el);
    });
    let anchor = el;
    segments.after.forEach((rule) => {
        const divider = buildHistoryCompressionDivider(rule, turnId);
        if (!divider) return;
        parent.insertBefore(divider, anchor.nextSibling || null);
        anchor = divider;
    });
    icons();
    return true;
}

function assistantRowHostsCompressionRule(item = {}) {
    const status = String(item?.status || "").trim().toLowerCase();
    if (status === "paused" || item?.silent_reply === true) return true;
    const hasDelta = item?.canonical_context_delta !== null && item?.canonical_context_delta !== undefined;
    const canonical = hasDelta
        ? normalizeCeoSnapshotCanonicalContext(item.canonical_context_delta)
        : normalizeCeoSnapshotCanonicalContext(item.canonical_context);
    if (canonical) return true;
    // 无轨道的纯文本行也能承载 completed 线：total=0 时按“回合结束”约定放前/后。
    return !!String(item?.turn_id || "").trim();
}

function buildCeoCompressionPlan(messages) {
    const markers = [];
    const rulesByTurn = new Map();
    const lastAssistantRowByTurn = new Map();
    (Array.isArray(messages) ? messages : []).forEach((item, index) => {
        const turnId = String(item?.turn_id || "").trim();
        if (item?.role === "assistant" && turnId && assistantRowHostsCompressionRule(item)) {
            lastAssistantRowByTurn.set(turnId, index);
        }
        const marker = normalizeCeoSnapshotCompressionMarker(item?.compression_marker);
        if (!marker || marker.state !== "completed") return;
        const markerTurnId = String(marker.turn_id || "").trim();
        if (!markerTurnId) return;
        const boundary = Number(marker.boundary_stage_index);
        if (!Number.isFinite(boundary)) return;
        const rule = {
            rowIndex: index,
            state: marker.state,
            turnId: markerTurnId,
            boundary,
            source: marker.source || "",
        };
        const list = rulesByTurn.get(markerTurnId) || [];
        list.push(rule);
        rulesByTurn.set(markerTurnId, list);
        markers.push(rule);
    });
    rulesByTurn.forEach((list) => list.sort((left, right) => left.boundary - right.boundary));
    const consumedMarkerRows = new Set();
    markers.forEach((rule) => {
        if (lastAssistantRowByTurn.has(rule.turnId)) consumedMarkerRows.add(rule.rowIndex);
    });
    return { rulesByTurn, lastAssistantRowByTurn, consumedMarkerRows };
}
