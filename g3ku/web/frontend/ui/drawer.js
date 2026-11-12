"use strict";

// 浏览器轨的界面缩放写在 body 的 CSS zoom 上（桌面壳走 WebView2 宿主缩放，不写它）。
// CSS zoom 之下 getBoundingClientRect() 给的是已乘 zoom 的视口坐标，而 body 子树里的
// fixed 浮层写 top/left/width 时坐标会被再乘一次 zoom：视口坐标必须先反除回本地坐标，
// 否则浮层会按缩放倍数飞出弹窗（放大时偏下偏右，缩小时偏上偏左）。
// Ctrl+滚轮与宿主缩放不动 body 的 zoom，这里自然返回 1，不参与换算。
// 所有挂到 body 上手动定位的浮层（resource-select 的 portal 面板、模型列表悬浮、
// 配置说明 popover、positionListPopover）都走这一个入口。
function bodyUiZoomFactor() {
    try {
        const raw = parseFloat(window.getComputedStyle(document.body)?.zoom ?? "");
        return Number.isFinite(raw) && raw > 0 ? raw : 1;
    } catch (error) {
        void error;
        return 1;
    }
}

function positionListPopover(toggle, popover, list, rowLimit = 20) {
    if (popover.hidden || !list.children.length) return;
    if (popover.parentElement !== document.body) document.body.appendChild(popover);
    const zoom = bodyUiZoomFactor();
    const edge = 12;
    const triggerRect = toggle.getBoundingClientRect();
    const width = popover.getBoundingClientRect().width / zoom;
    popover.style.left = `${Math.max(edge, Math.min(triggerRect.right / zoom - width, window.innerWidth / zoom - width - edge))}px`;
    popover.style.top = `${triggerRect.bottom / zoom + 6}px`;

    const first = list.children[0].getBoundingClientRect();
    const last = list.children[Math.min(rowLimit, list.children.length) - 1].getBoundingClientRect();
    const style = window.getComputedStyle(popover);
    const bottomInset = parseFloat(style.paddingBottom) + parseFloat(style.borderBottomWidth);
    const available = (window.innerHeight - list.getBoundingClientRect().top) / zoom - bottomInset - edge;
    list.style.maxHeight = `${Math.max(0, Math.min(Math.ceil((last.bottom - first.top) / zoom), Math.floor(available)))}px`;
}

// 共享抽屉/弹窗原语：开合状态、`aria-hidden`、焦点进入与归还。
// 组件只负责自己的内容与业务动作，不各自复刻这套开关。
function setDrawerOpen(backdrop, drawer, open) {
    const wasOpen = !!drawer?.classList.contains("is-open");
    // 焦点先归还再藏：aria-hidden 落在仍持有焦点的抽屉上会触发浏览器告警，
    // 且读屏会丢焦点位置。归还目标不可聚焦（如 body）时焦点会留在抽屉里，
    // 兜底 blur 把焦点交还文档，避免藏一个持有焦点的面。
    if (!open) {
        if (drawer?.__returnFocus?.focus) {
            drawer.__returnFocus.focus();
        }
        if (drawer && typeof drawer.contains === "function" && drawer.contains(document.activeElement)) {
            document.activeElement?.blur?.();
        }
    }
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
}
