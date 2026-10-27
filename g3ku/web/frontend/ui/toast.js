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
    // 跳转动作只活在这一条 toast 里：关掉就撤，下一条不会继承上一颗箭头。
    if (U.toastAction) U.toastAction.hidden = true;
    S.toastState.action = null;
}

function showToast({ title = "操作成功", text = "修改已生效", kind = "success", durationMs = 3000, persistent = false, spin = false, closable = true, action = null } = {}) {
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
    // 行首箭头是可选的跳转入口：只在调用方给了 onClick 时出现。
    if (U.toastAction) {
        const runAction = typeof action?.onClick === "function" ? action.onClick : null;
        const label = String(action?.label || "").trim() || "跳转";
        U.toastAction.hidden = !runAction;
        if (runAction) {
            U.toastAction.setAttribute("aria-label", label);
            U.toastAction.title = label;
        }
        S.toastState.action = runAction;
    }
    if (!sticky) {
        S.toastState.timeoutId = window.setTimeout(closeToast, durationMs);
    }
    U.toast.className = `app-toast is-open is-${kind}`;
    icons();
}
