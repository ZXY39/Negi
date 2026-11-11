"use strict";

function positionListPopover(toggle, popover, list, rowLimit = 20) {
    if (popover.hidden || !list.children.length) return;
    if (popover.parentElement !== document.body) document.body.appendChild(popover);
    const zoom = parseFloat(window.getComputedStyle(document.body).zoom) || 1;
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
