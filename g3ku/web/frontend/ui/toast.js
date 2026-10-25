"use strict";

// 共享 toast：保留全局入口与现有 DOM id，页面在 app.js 之前加载本文件。
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
}

function showToast({ title = "操作成功", text = "修改已生效", kind = "success", durationMs = 3000, persistent = false, spin = false, closable = true } = {}) {
    if (!U.toast || !U.toastTitle || !U.toastText || !U.toastClose) return;
    clearToastTimers();
    const sticky = persistent || durationMs <= 0;
    U.toastTitle.textContent = title;
    U.toastText.textContent = text;
    U.toast.hidden = false;
    U.toast.setAttribute("role", kind === "error" ? "alert" : "status");
    // 进行中的 toast 是唯一进度信号，给一个关闭键等于让用户自己把它抹掉；
    // 引导期的拖动提示也要求常驻到链路配齐，调用方用 closable=false 表达。
    U.toastClose.hidden = !!spin || closable === false;
    if (U.toastIcon) U.toastIcon.hidden = !spin;
    if (!sticky) {
        S.toastState.timeoutId = window.setTimeout(closeToast, durationMs);
    }
    U.toast.className = `app-toast is-open is-${kind}`;
    icons();
}
