// 模型页弹窗视图：上下文信息更新时间对话框。
// 从 models/role-view.js 迁出，保持全局函数名不变。

function renderContextRefreshDialog() {
    const state = S.modelCatalog.contextRefresh;
    const backdrop = U.contextRefreshBackdrop;
    if (!backdrop) return;
    if (!state.open) {
        backdrop.hidden = true;
        backdrop.classList.remove("is-open");
        U.contextRefreshDialog?.setAttribute("aria-hidden", "true");
        return;
    }
    backdrop.hidden = false;
    backdrop.classList.add("is-open");
    U.contextRefreshDialog?.setAttribute("aria-hidden", "false");
    // 输入框只在非焦点时回写：整块重绘会把光标位置和勾选焦点踢掉。
    if (U.contextRefreshEnabled && document.activeElement !== U.contextRefreshEnabled) {
        U.contextRefreshEnabled.checked = Boolean(state.enabled);
    }
    if (U.contextRefreshMinutes && document.activeElement !== U.contextRefreshMinutes) {
        U.contextRefreshMinutes.value = String(state.idleMinutes ?? "");
    }
    if (U.contextRefreshError) {
        U.contextRefreshError.hidden = !state.error;
        U.contextRefreshError.textContent = state.error || "";
    }
    if (U.contextRefreshApply) {
        U.contextRefreshApply.disabled = state.loading || state.saving || !contextRefreshDraftDirty();
        U.contextRefreshApply.textContent = state.saving ? "应用中…" : "应用";
    }
    const lockClose = Boolean(state.saving);
    U.contextRefreshClose?.toggleAttribute("disabled", lockClose);
    U.contextRefreshCancel?.toggleAttribute("disabled", lockClose);
}
