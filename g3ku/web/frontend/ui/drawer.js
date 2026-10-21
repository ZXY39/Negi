"use strict";

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
