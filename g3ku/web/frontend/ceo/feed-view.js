"use strict";

// CEO 会话流的滚动/窗口层：向上分页、贴底跟随、锚点快照、重建前后的视图状态还原。
// 依赖 app.js 的 S/U 与渲染入口；本文件必须在 org_graph_app.js 之前加载。

const CEO_FEED_WINDOW_SIZE = 80;
const CEO_FEED_ANCHOR_EXPANSION_PAGES = 3;

let ceoFeedUserScrollIntentAt = 0;
let ceoFeedProgrammaticScrollUntil = 0;
let ceoFeedLastMutationAt = 0;
let ceoFeedBatchDepth = 0;

function loadOlderCeoFeedMessages({ upToKey = "", maxPages = 1 } = {}) {
    const source = S.ceoFeedWindowSource;
    if (!source || !U || !U.ceoFeed) return false;
    if (String(S.ceoFeedRenderSessionId || "") !== String(source.sessionId || "")) return false;
    let loaded = false;
    for (let page = 0; page < Math.max(1, maxPages); page += 1) {
        if (source.start <= 0) break;
        const newStart = Math.max(0, source.start - CEO_FEED_WINDOW_SIZE);
        const host = document.createElement("div");
        S.ceoFeedAppendTarget = host;
        try {
            renderCeoSnapshotMessageRange(source.messages, source.keys, newStart, source.start, source.sessionId);
        } finally {
            S.ceoFeedAppendTarget = null;
        }
        const beforeHeight = U.ceoFeed.scrollHeight || 0;
        const anchorChild = U.ceoFeed.firstChild;
        while (host.firstChild) U.ceoFeed.insertBefore(host.firstChild, anchorChild);
        const grew = (U.ceoFeed.scrollHeight || 0) - beforeHeight;
        if (grew > 0) {
            markCeoFeedProgrammaticScroll();
            U.ceoFeed.scrollTop = Math.max(0, (Number(U.ceoFeed.scrollTop || 0) + grew));
        }
        source.start = newStart;
        S.ceoFeedRenderedMessageKeys = source.keys.slice(newStart);
        updateCeoScrollToLatestButton();
        loaded = true;
        if (!upToKey) break;
        if (ceoFeedAnchorRendered(upToKey)) break;
    }
    return loaded;
}

function ceoFeedAppendHost() {
    // 窗口分页补渲染时，渲染器写进临时容器而不是 feed 尾部；平时行为不变。
    return S.ceoFeedAppendTarget || U.ceoFeed;
}

function ceoFeedAnchorRendered(key = "") {
    const needle = String(key || "").trim();
    if (!needle || !U || !U.ceoFeed) return false;
    const children = Array.from(U.ceoFeed.children || []);
    return children.some((child) => ceoFeedElementDataKey(child) === needle);
}

function ceoFeedNearBottom(threshold = 64) {
    if (!U.ceoFeed) return true;
    return U.ceoFeed.scrollHeight - U.ceoFeed.scrollTop - U.ceoFeed.clientHeight <= threshold;
}

function ceoFeedGestureReachesFeed(event, scrollingUp) {
    let node = event?.target;
    while (node && node !== U.ceoFeed) {
        const scrollHeight = Number(node.scrollHeight || 0);
        const clientHeight = Number(node.clientHeight || 0);
        if (scrollHeight > clientHeight) {
            const top = Number(node.scrollTop || 0);
            const canConsume = scrollingUp ? top > 0 : top + clientHeight < scrollHeight - 1;
            if (canConsume) return false;
        }
        node = node.parentElement;
    }
    return true;
}

function markCeoFeedProgrammaticScroll() {
    ceoFeedProgrammaticScrollUntil = Date.now() + 1000;
}

function setCeoFeedFollowLatest(on) {
    S.ceoFeedFollowLatest = !!on;
    updateCeoScrollToLatestButton();
}

function handleCeoFeedUserGesture(event = null) {
    ceoFeedUserScrollIntentAt = Date.now();
    // 直播钉底在下一帧就覆盖掉滚轮产生的滚动量，被派发的 scroll 事件读到的仍是底部，
    // 所以「上翻」必须在手势本身上脱离跟随，否则流式期间用户被按住无法滚动。
    if (S.ceoFeedFollowLatest === false) return;
    const type = String(event?.type || "");
    const scrollingUp = type === "wheel"
        ? Number(event?.deltaY || 0) < 0
        : (type === "keydown" && CEO_FEED_SCROLL_UP_KEYS.has(String(event?.key || "")));
    if (scrollingUp && ceoFeedGestureReachesFeed(event, true)) setCeoFeedFollowLatest(false);
}

function handleCeoFeedScrollEvent() {
    // 接近顶部且还有未渲染的历史：从内存数组补一片（补偿滚动，视口不动）。
    if (U.ceoFeed && Number(U.ceoFeed.scrollTop || 0) < 400) {
        loadOlderCeoFeedMessages();
    }
    // 意图位只由"突变静默期之外、且非程序钉底派发"的滚动改写：
    // atBottom 分支的钉底/图片异步 re-pin 都会派发 scroll，若不隔离，
    // 手势后窗口内的程序滚动会被误判成"用户回到底部"而重新武装跟随，
    // 直播高频突变下用户永远翻不出底部。
    const now = Date.now();
    if (now < ceoFeedProgrammaticScrollUntil || now - ceoFeedLastMutationAt < 250) {
        updateCeoScrollToLatestButton();
        return;
    }
    if (ceoFeedNearBottom()) {
        setCeoFeedFollowLatest(true);
    } else if (now - ceoFeedUserScrollIntentAt < 1500) {
        setCeoFeedFollowLatest(false);
    }
    updateCeoScrollToLatestButton();
}

function updateCeoScrollToLatestButton() {
    if (!U.ceoScrollToLatestBtn) return;
    const atLatest = S.ceoFeedFollowLatest !== false && ceoFeedNearBottom();
    U.ceoScrollToLatestBtn.hidden = atLatest;
    // 按钮常显但只在回合进行中呼吸:回合收尾后才出现的"上翻读历史"没有新内容要追。
    U.ceoScrollToLatestBtn.classList?.toggle("is-breathing", !!S.ceoTurnActive);
}

function scrollCeoFeedToBottom() {
    if (!U.ceoFeed) return;
    S.ceoFeedFollowLatest = true;
    ceoFeedLastMutationAt = Date.now();
    const applyBottom = () => {
        if (!U.ceoFeed) return;
        // 挂起的 rAF/图片 re-pin 迟到时用户可能已上滚：跟随位一旦脱离立即作废本次钉底。
        if (S.ceoFeedFollowLatest === false) return;
        markCeoFeedProgrammaticScroll();
        U.ceoFeed.scrollTop = U.ceoFeed.scrollHeight;
        updateCeoScrollToLatestButton();
    };
    applyBottom();
    window.requestAnimationFrame(applyBottom);

    // Lazy/async media can grow the feed after the initial scroll: attachment
    // and inline images render with no reserved space and load asynchronously,
    // and webfonts can reflow text. Without a re-apply, a freshly opened session
    // stops short of the true bottom once those settle, so re-pin the feed as
    // each pending image finishes and once fonts are ready.
    const images = Array.from(U.ceoFeed.querySelectorAll?.("img") || []);
    for (const img of images) {
        if (!img || img.complete || typeof img.addEventListener !== "function") continue;
        img.addEventListener("load", applyBottom, { once: true });
        img.addEventListener("error", applyBottom, { once: true });
    }
    if (typeof document.fonts?.ready?.then === "function") {
        document.fonts.ready.then(applyBottom).catch(() => {});
    }
}

function captureCeoFeedScrollSnapshot() {
    if (!U || !U.ceoFeed) return null;
    const feed = U.ceoFeed;
    const prevTop = Math.max(0, Number(feed.scrollTop || 0));
    // 用意图位而非瞬时几何：直播突变每几百毫秒一次，用户拖动滚动条的过程中
    // 几何判定会反复误判「还在底部」并把视口钉回去（“翻许多次才翻得动”的根因）。
    let atBottom = S.ceoFeedFollowLatest !== false;
    // 拖滚动条/触摸滑动没有方向可判，只在突变前的这一眼几何上补判一次：
    // 手势之后已经离开底部即视为脱离，本帧不再钉底。捕获发生在 DOM 突变之前，
    // 所以「点击展开卡片」那一类增高不会被误读成用户上翻。
    if (atBottom && ceoFeedUserScrollIntentAt > ceoFeedLastMutationAt && !ceoFeedNearBottom()) {
        setCeoFeedFollowLatest(false);
        atBottom = false;
    }
    return {
        prevTop,
        atBottom,
        anchor: atBottom ? null : ceoFeedNearScrollTop(feed, prevTop),
    };
}

function restoreCeoFeedScrollSnapshot(snapshot = null) {
    if (!U || !U.ceoFeed) return;
    const feed = U.ceoFeed;
    ceoFeedLastMutationAt = Date.now();
    if (!snapshot) {
        updateCeoScrollToLatestButton();
        return;
    }
    if (snapshot.atBottom) {
        // 直播跟随直接钉底,不走 scrollCeoFeedToBottom 的异步 re-pin:流式期间每次
        // mutation 都会重新判定,而挂起的 rAF 会在用户上滚离开后把视口拽回底部。
        markCeoFeedProgrammaticScroll();
        feed.scrollTop = feed.scrollHeight;
        updateCeoScrollToLatestButton();
        return;
    }
    const maxTop = Math.max(0, (feed.scrollHeight || 0) - (feed.clientHeight || 0));
    const anchored = ceoFeedAnchoredScrollTop(feed, snapshot.anchor);
    const nextTop = Number.isFinite(anchored) ? anchored : Number(snapshot.prevTop || 0);
    markCeoFeedProgrammaticScroll();
    feed.scrollTop = Math.max(0, Math.min(nextTop, maxTop));
    updateCeoScrollToLatestButton();
}

function withCeoFeedBatch(mutator, { scrollMode = "preserve" } = {}) {
    if (typeof mutator !== "function") return null;
    if (!U.ceoFeed) return mutator();
    // 只有最外层帧捕获/还原滚动快照;嵌套帧对 mutator 透明(与旧 prevTop 语义一致)。
    const snapshot = ceoFeedBatchDepth === 0 && scrollMode !== "bottom"
        ? captureCeoFeedScrollSnapshot()
        : null;
    ceoFeedBatchDepth += 1;
    let result = null;
    try {
        result = mutator();
    } finally {
        ceoFeedBatchDepth = Math.max(0, ceoFeedBatchDepth - 1);
    }
    if (ceoFeedBatchDepth > 0) return result;
    // 批次内只排队一次；把 live-only 的 running 线放到所有消息之后，再算滚动。
    if (ceoCompressionSyncQueued) {
        ceoCompressionSyncQueued = false;
        syncCeoCompressionDivider();
    }
    if (scrollMode === "bottom") {
        scrollCeoFeedToBottom();
    } else {
        restoreCeoFeedScrollSnapshot(snapshot);
    }
    return result;
}

function mutateCeoFeed(mutator, { scrollMode = "preserve" } = {}) {
    if (typeof mutator !== "function") return null;
    if (!U.ceoFeed) return mutator();
    if (ceoFeedBatchDepth > 0) return mutator();
    const snapshot = scrollMode === "bottom" ? null : captureCeoFeedScrollSnapshot();
    const result = mutator();
    if (scrollMode === "bottom") {
        scrollCeoFeedToBottom();
    } else {
        restoreCeoFeedScrollSnapshot(snapshot);
    }
    return result;
}

function ceoFeedElementDataKey(el = null) {
    return (el && el.dataset && typeof el.dataset.ceoKey === "string") ? String(el.dataset.ceoKey || "") : "";
}

function ceoFeedTurnIdentity(turnEl = null, endIndex = 0) {
    const dataKey = ceoFeedElementDataKey(turnEl);
    return dataKey ? `k:${dataKey}` : `e:${endIndex}`;
}

function ceoFeedNearScrollTop(feed, scrollTop) {
    // 找出当前滚动位置所锚定的第一个子元素:内容顶+高度压过 scrollTop 即命中。
    const children = Array.from((feed && feed.children) || []);
    if (!children.length || typeof feed.getBoundingClientRect !== "function") return null;
    const feedRect = feed.getBoundingClientRect();
    for (let index = 0; index < children.length; index += 1) {
        const child = children[index];
        if (!child || typeof child.getBoundingClientRect !== "function") continue;
        const rect = child.getBoundingClientRect();
        const contentTop = rect.top - feedRect.top + (feed.scrollTop || 0);
        const height = Math.max(1, Number(rect.height || 1));
        if (contentTop + height > scrollTop) {
            const dataKey = ceoFeedElementDataKey(child);
            return dataKey
                ? { key: dataKey, offsetInElement: Math.max(0, scrollTop - contentTop) }
                : { endIndex: children.length - 1 - index, offsetInElement: Math.max(0, scrollTop - contentTop) };
        }
    }
    return null;
}

function ceoFeedAnchoredScrollTop(feed, anchor = null) {
    if (!anchor || !feed || typeof feed.getBoundingClientRect !== "function") return null;
    const children = Array.from(feed.children || []);
    let el = null;
    if (anchor.key) el = children.find((child) => ceoFeedElementDataKey(child) === anchor.key) || null;
    if (!el && Number.isInteger(anchor.endIndex)) {
        el = children[children.length - 1 - anchor.endIndex] || null;
    }
    if (!el || typeof el.getBoundingClientRect !== "function") return null;
    const elRect = el.getBoundingClientRect();
    const feedRect = feed.getBoundingClientRect();
    // 元素内容顶坐标 = rect.top - feedRect.top + scrollTop;还原时按当前 rect 重新计算,
    // 上方内容高度变化不会把锚点漂走。
    return Math.max(0, elRect.top - feedRect.top + (feed.scrollTop || 0) + (anchor.offsetInElement || 0));
}

function captureCeoFeedViewState(sessionId = "") {
    const key = String(sessionId || "").trim();
    if (!key) return null;
    // 跨会话保护:feed 当前渲染的会话与 target 不一致时(切换会话的瞬间)不捕获,避免把
    // 上一个会话的展开态/锚点套到新会话上。
    if (String(S.ceoFeedRenderSessionId || "") !== key) return null;
    if (!U || !U.ceoFeed || typeof U.ceoFeed.querySelectorAll !== "function") return null;
    const feed = U.ceoFeed;
    const state = {
        sessionId: key,
        atBottom: S.ceoFeedFollowLatest !== false,
        prevTop: Math.max(0, Number(feed.scrollTop || 0)),
        turnFlows: {},
        steps: {},
        roundTools: {},
        anchor: null,
    };
    state.anchor = ceoFeedNearScrollTop(feed, state.prevTop);
    const turns = Array.from(feed.querySelectorAll(".ceo-turn-message") || []);
    turns.forEach((turnEl, index) => {
        if (!turnEl) return;
        const identity = ceoFeedTurnIdentity(turnEl, turns.length - 1 - index);
        const flowEl = turnEl.querySelector(".interaction-flow");
        const toggleEl = turnEl.querySelector(".interaction-flow-toggle");
        state.turnFlows[identity] = {
            flowOpen: !!(flowEl && flowEl.open),
            historyExpanded: !!(toggleEl && typeof toggleEl.getAttribute === "function" && toggleEl.getAttribute("aria-expanded") === "true"),
        };
        Array.from(turnEl.querySelectorAll(".task-trace-step") || []).forEach((stepEl) => {
            if (!stepEl?.dataset || typeof stepEl.dataset.traceKey !== "string") return;
            const traceKey = String(stepEl.dataset.traceKey || "").trim();
            if (traceKey) state.steps[`${identity}::${traceKey}`] = !!stepEl.open;
        });
        // activeToolKey 按回合内 DOM 顺序分域,规避 roundKey 为空时跨轮次 tool key 撞车。
        Array.from(turnEl.querySelectorAll(".task-trace-round-tools") || []).forEach((host, hostIndex) => {
            if (!(host instanceof HTMLElement)) {
                const attr = host && typeof host.getAttribute === "function" ? host.getAttribute("data-active-tool-key") : "";
                const active = String(attr || "").trim();
                if (active) state.roundTools[`${identity}::${hostIndex}`] = active;
                return;
            }
            const active = String(host.dataset?.activeToolKey || host.getAttribute?.("data-active-tool-key") || "").trim();
            if (active) state.roundTools[`${identity}::${hostIndex}`] = active;
        });
    });
    return state;
}

function applyCeoFeedViewState(viewState = null) {
    if (!viewState || typeof viewState !== "object") return;
    if (!U || !U.ceoFeed || typeof U.ceoFeed.querySelectorAll !== "function") return;
    if (String(viewState.sessionId || "").trim() !== String(activeSessionId() || "").trim()) return;
    const feed = U.ceoFeed;
    const turns = Array.from(feed.querySelectorAll(".ceo-turn-message") || []);
    turns.forEach((turnEl, index) => {
        if (!turnEl) return;
        const identity = ceoFeedTurnIdentity(turnEl, turns.length - 1 - index);
        const flowState = viewState.turnFlows && viewState.turnFlows[identity];
        if (flowState) {
            const flowEl = turnEl.querySelector(".interaction-flow");
            if (flowEl && typeof flowState.flowOpen === "boolean") flowEl.open = flowState.flowOpen;
            if (flowState.historyExpanded) {
                const footerEl = turnEl.querySelector(".interaction-flow-footer");
                if (footerEl && "hidden" in footerEl) footerEl.hidden = false;
                const toggleEl = turnEl.querySelector(".interaction-flow-toggle");
                if (toggleEl) {
                    if ("textContent" in toggleEl) toggleEl.textContent = "收起旧进度";
                    if (typeof toggleEl.setAttribute === "function") toggleEl.setAttribute("aria-expanded", "true");
                }
                Array.from(turnEl.querySelectorAll(".interaction-step") || []).forEach((item) => {
                    if (item && "hidden" in item) item.hidden = false;
                    if (item?.classList && typeof item.classList.remove === "function") item.classList.remove("is-collapsed-history");
                });
            }
        }
        Array.from(turnEl.querySelectorAll(".task-trace-step") || []).forEach((stepEl) => {
            if (!stepEl?.dataset || typeof stepEl.dataset.traceKey !== "string") return;
            const traceKey = String(stepEl.dataset.traceKey || "").trim();
            const captured = traceKey && viewState.steps ? viewState.steps[`${identity}::${traceKey}`] : undefined;
            if (typeof captured === "boolean") stepEl.open = captured;
        });
        Array.from(turnEl.querySelectorAll(".task-trace-round-tools") || []).forEach((host, hostIndex) => {
            if (!(host instanceof HTMLElement)) return;
            const toolKey = viewState.roundTools ? viewState.roundTools[`${identity}::${hostIndex}`] : "";
            if (toolKey && typeof setTraceRoundActiveTool === "function") setTraceRoundActiveTool(host, toolKey);
        });
    });
    // 直接赋 open 属性不触发 toggle 事件,补一次懒加载副作用,与任务详情视图还原行为一致。
    Array.from(feed.querySelectorAll(".task-trace-step") || []).forEach((stepEl) => {
        if (stepEl?.open && typeof hydrateTraceOutputBlocks === "function") hydrateTraceOutputBlocks(stepEl);
    });
    restoreCeoFeedScroll(viewState);
}

function restoreCeoFeedScroll(viewState = null) {
    if (!viewState || !U || !U.ceoFeed) return;
    ceoFeedLastMutationAt = Date.now();
    if (viewState.atBottom) {
        scrollCeoFeedToBottom();
        return;
    }
    const applyAnchored = () => {
        if (!U || !U.ceoFeed) return;
        const feed = U.ceoFeed;
        const maxTop = Math.max(0, (feed.scrollHeight || 0) - (feed.clientHeight || 0));
        const anchored = ceoFeedAnchoredScrollTop(feed, viewState.anchor);
        const nextTop = Number.isFinite(anchored) ? anchored : Number(viewState.prevTop || 0);
        markCeoFeedProgrammaticScroll();
        feed.scrollTop = Math.max(0, Math.min(nextTop, maxTop));
        updateCeoScrollToLatestButton();
    };
    applyAnchored();
    // 异步资源(图片/输出块)稍后撑开高度会改变几何,双重 rAF 再校正两次,与任务详情视图一致。
    const raf = typeof requestAnimationFrame === "function" ? requestAnimationFrame : (fn) => fn();
    raf(() => {
        applyAnchored();
        raf(applyAnchored);
    });
}
