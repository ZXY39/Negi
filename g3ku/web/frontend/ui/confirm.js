"use strict";

// 共享确认框：保留全局入口与 S.confirmState 结构，页面在 app.js 之前加载本文件。
function openConfirm({
    title,
    text,
    confirmLabel = "确认",
    confirmKind = "danger",
    onConfirm,
    onClose = null,
    returnFocus = null,
    checkbox = null,
    input = null,
    textFull = false,
    inputPlain = false,
}) {
    const dialog = document.getElementById("confirm-dialog");
    if (dialog) {
        // 长的安全影响说明要整段展示，不能塞进 180px 的滚动口；口令行也可以去掉外层框。
        dialog.classList.toggle("is-text-full", !!textFull);
        dialog.classList.toggle("is-input-plain", !!inputPlain);
    }
    S.confirmState = {
        onConfirm,
        onClose,
        returnFocus,
        checkbox: checkbox && typeof checkbox === "object" ? checkbox : null,
        input: input && typeof input === "object" ? input : null,
        accepted: false,
    };
    U.confirmTitle.textContent = title;
    U.confirmText.textContent = text;
    if (U.confirmInputRow && U.confirmInput) {
        const inputEnabled = !!(input && typeof input === "object");
        U.confirmInputRow.hidden = !inputEnabled;
        U.confirmInput.disabled = false;
        // 带初值时用它（改备注这类"在原值上编辑"的场景），否则清空。
        U.confirmInput.value = inputEnabled ? String(input.value || "") : "";
        U.confirmInput.type = inputEnabled && input.type === "password" ? "password" : "text";
        if (inputEnabled && U.confirmInputLabel) {
            U.confirmInputLabel.textContent = input.label || "";
        }
        // 占位符必须跟着这次调用走：上一次弹窗的提示留在这个共享 input 上，
        // 会让下一个口令框看起来已经填了「0002」。
        if (inputEnabled) {
            U.confirmInput.setAttribute("placeholder", String(input.placeholder || ""));
        }
    }
    if (U.confirmOptions && U.confirmCheckbox && U.confirmCheckboxLabel && U.confirmCheckboxHint) {
        const enabled = !!(checkbox && typeof checkbox === "object");
        U.confirmOptions.hidden = !enabled;
        if (enabled) {
            U.confirmCheckbox.checked = !!checkbox.checked;
            U.confirmCheckbox.disabled = false;
            U.confirmCheckboxLabel.textContent = checkbox.label || "同时删除此对话创建的所有任务记录";
            U.confirmCheckboxHint.textContent = checkbox.hint || "";
            if (U.confirmCheckboxDetails) {
                const detailText = String(checkbox.details || "").trim();
                U.confirmCheckboxDetails.hidden = !detailText;
                U.confirmCheckboxDetails.textContent = detailText;
            }
        } else {
            U.confirmCheckbox.checked = false;
            U.confirmCheckbox.disabled = false;
            U.confirmCheckboxLabel.textContent = "同时删除此对话创建的所有任务记录";
            U.confirmCheckboxHint.textContent = "";
            if (U.confirmCheckboxDetails) {
                U.confirmCheckboxDetails.hidden = true;
                U.confirmCheckboxDetails.textContent = "";
            }
        }
    }
    U.confirmAccept.textContent = confirmLabel;
    U.confirmAccept.className = `toolbar-btn ${confirmKind}`;
    U.confirmBackdrop.hidden = false;
    U.confirmBackdrop.classList.add("is-open");
    window.requestAnimationFrame(() => {
        if (S.confirmState?.input) {
            U.confirmInput?.focus();
            return;
        }
        U.confirmCancel?.focus();
    });
}

function requestInlineConfirm({ title, text, confirmLabel = "确认", confirmKind = "danger", returnFocus = null, checkbox = null }) {
    const focusTarget = returnFocus || (document.activeElement instanceof HTMLElement ? document.activeElement : null);
    return new Promise((resolve) => {
        let settled = false;
        openConfirm({
            title,
            text,
            confirmLabel,
            confirmKind,
            returnFocus: focusTarget,
            checkbox,
            onConfirm: async ({ checked }) => {
                if (settled) return;
                settled = true;
                resolve({ confirmed: true, checked: !!checked });
            },
            onClose: () => {
                if (settled) return;
                settled = true;
                resolve({ confirmed: false, checked: false });
            },
        });
    });
}

function closeConfirm({ restoreFocus = true } = {}) {
    const returnFocus = S.confirmState?.returnFocus;
    const onClose = S.confirmState?.onClose;
    const accepted = !!S.confirmState?.accepted;
    S.confirmState = null;
    if (U.confirmOptions) U.confirmOptions.hidden = true;
    if (U.confirmInputRow) U.confirmInputRow.hidden = true;
    if (U.confirmInput) {
        U.confirmInput.value = "";
        U.confirmInput.type = "text";
        U.confirmInput.disabled = false;
    }
    if (U.confirmCheckbox) {
        U.confirmCheckbox.checked = false;
        U.confirmCheckbox.disabled = false;
    }
    if (U.confirmCheckboxHint) U.confirmCheckboxHint.textContent = "";
    U.confirmBackdrop.hidden = true;
    U.confirmBackdrop.classList.remove("is-open");
    const dialog = document.getElementById("confirm-dialog");
    if (dialog) dialog.classList.remove("is-text-full", "is-input-plain");
    if (!accepted && typeof onClose === "function") {
        try {
            onClose();
        } catch (error) {
            void error;
        }
    }
    if (restoreFocus) returnFocus?.focus?.();
}

async function acceptConfirm() {
    if (!S.confirmState?.onConfirm) return;
    U.confirmAccept.disabled = true;
    U.confirmCancel.disabled = true;
    if (U.confirmCheckbox) U.confirmCheckbox.disabled = true;
    try {
        S.confirmState.accepted = true;
        await S.confirmState.onConfirm({ checked: !!U.confirmCheckbox?.checked, value: String(U.confirmInput?.value || "") });
        closeConfirm();
    } catch (error) {
        if (S.confirmState) S.confirmState.accepted = false;
        showToast({ title: "操作失败", text: error?.message || "Unknown error", kind: "error" });
    } finally {
        U.confirmAccept.disabled = false;
        U.confirmCancel.disabled = false;
        if (U.confirmCheckbox) U.confirmCheckbox.disabled = false;
    }
}
