// External access panel: External Agent API (/api/v1) toggle + bridge tokens.
// The plaintext token is surfaced exactly once (right after create/regenerate);
// every other render only ever shows the server-provided mask.

const EXTERNAL_API_VIEW_STATE = {
    loaded: false,
    busy: false,
    enabled: false,
    items: [],
};

function _externalListEl() {
    return document.getElementById("external-token-list");
}

function _externalMasterSwitch() {
    return document.getElementById("external-api-enabled-toggle");
}

function _externalOnceCard() {
    return document.getElementById("external-token-once");
}

function _externalFormEl() {
    return document.getElementById("external-token-form");
}

function _setExternalSwitch(button, pressed, label) {
    if (!button) return;
    button.setAttribute("aria-pressed", pressed ? "true" : "false");
    if (label) {
        const text = button.querySelector(".tool-governance-switch-label");
        if (text) text.textContent = label;
    }
}

function _externalBusy(next) {
    EXTERNAL_API_VIEW_STATE.busy = !!next;
    const busy = EXTERNAL_API_VIEW_STATE.busy;
    for (const id of [
        "external-refresh-btn",
        "external-token-create-btn",
        "external-token-form-submit",
        "external-token-form-cancel",
        "external-token-once-copy",
    ]) {
        const el = document.getElementById(id);
        if (el) el.disabled = busy;
    }
    const master = _externalMasterSwitch();
    if (master) master.disabled = busy;
    const listEl = _externalListEl();
    if (listEl) {
        for (const control of listEl.querySelectorAll("button[data-bridge]")) {
            control.disabled = busy;
        }
    }
}

function _applyExternalPayload(payload) {
    const data = payload || {};
    EXTERNAL_API_VIEW_STATE.enabled = !!data.enabled;
    EXTERNAL_API_VIEW_STATE.items = Array.isArray(data.items) ? data.items : [];
    EXTERNAL_API_VIEW_STATE.loaded = true;
}

function renderExternalTokenList() {
    const listEl = _externalListEl();
    if (!listEl) return;
    const items = EXTERNAL_API_VIEW_STATE.items;
    if (!EXTERNAL_API_VIEW_STATE.loaded) {
        listEl.innerHTML = '<div class="resource-empty">加载中…</div>';
        return;
    }
    if (!items.length) {
        listEl.innerHTML = '<div class="resource-empty">还没有桥接 token。点击右上角「签发桥接 token」为外部桥接应用创建第一个。</div>';
        return;
    }
    listEl.innerHTML = "";
    for (const item of items) {
        const row = document.createElement("div");
        row.className = "resource-list-item external-token-item";
        row.innerHTML = `
            <div class="external-token-main">
                <div class="resource-list-title">${esc(item.bridge_id)}</div>
                <div class="resource-list-subtitle">${item.label ? esc(item.label) + " · " : ""}token：<code>${esc(item.token_masked || "（未设置）")}</code></div>
            </div>
            <div class="external-token-actions">
                <button class="tool-governance-switch external-row-switch" type="button"
                    data-bridge="${esc(item.bridge_id)}"
                    aria-pressed="${item.enabled ? "true" : "false"}"
                    aria-label="切换 ${esc(item.bridge_id)} 启用状态">
                    <span class="tool-governance-switch-track" aria-hidden="true"><span class="tool-governance-switch-thumb"></span></span>
                    <span class="tool-governance-switch-label">${item.enabled ? "已启用" : "已停用"}</span>
                </button>
                <button class="toolbar-btn ghost small" data-action="regenerate" data-bridge="${esc(item.bridge_id)}" type="button">重新生成</button>
                <button class="toolbar-btn ghost danger small" data-action="delete" data-bridge="${esc(item.bridge_id)}" type="button">删除</button>
            </div>`;
        listEl.appendChild(row);
    }
}

async function loadExternalApiView({ quiet = false } = {}) {
    if (EXTERNAL_API_VIEW_STATE.busy) return;
    try {
        const payload = await ApiClient.getExternalApiSettings();
        _applyExternalPayload(payload);
        _setExternalSwitch(_externalMasterSwitch(), EXTERNAL_API_VIEW_STATE.enabled, EXTERNAL_API_VIEW_STATE.enabled ? "已启用" : "已停用");
        renderExternalTokenList();
    } catch (error) {
        if (!quiet) showToast({ title: "加载外部接入配置失败", text: ApiClient.friendlyErrorMessage(error), kind: "error" });
    }
}

function _showExternalTokenOnce(bridgeId, token) {
    const card = _externalOnceCard();
    if (!card || !token) return;
    document.getElementById("external-token-once-id").textContent = bridgeId;
    document.getElementById("external-token-once-text").value = token;
    card.hidden = false;
    card.scrollIntoView({ block: "nearest", behavior: "smooth" });
}

function _closeExternalTokenOnce() {
    const card = _externalOnceCard();
    if (card) card.hidden = true;
    const text = document.getElementById("external-token-once-text");
    if (text) text.value = "";
}

async function _copyExternalTokenOnce() {
    const input = document.getElementById("external-token-once-text");
    if (!input || !input.value) return;
    const value = input.value;
    try {
        await navigator.clipboard.writeText(value);
        showToast({ title: "已复制", text: "token 已复制到剪贴板。", kind: "success" });
    } catch (error) {
        input.focus();
        input.select();
        let ok = false;
        try {
            ok = document.execCommand("copy");
        } catch (fallbackError) {
            ok = false;
        }
        if (ok) {
            showToast({ title: "已复制", text: "token 已复制到剪贴板。", kind: "success" });
        } else {
            showToast({ title: "复制失败", text: "请手动选中 token 后复制。", kind: "error" });
        }
    }
}

function _setExternalFormOpen(open) {
    const form = _externalFormEl();
    const createBtn = document.getElementById("external-token-create-btn");
    if (form) form.hidden = !open;
    if (createBtn) createBtn.setAttribute("aria-expanded", open ? "true" : "false");
    if (open) {
        const first = document.getElementById("external-token-bridge-input");
        if (first) first.focus();
    }
}

async function createExternalToken(event) {
    if (event) event.preventDefault();
    if (EXTERNAL_API_VIEW_STATE.busy) return;
    const bridgeInput = document.getElementById("external-token-bridge-input");
    const labelInput = document.getElementById("external-token-label-input");
    const customInput = document.getElementById("external-token-custom-input");
    const bridgeId = String(bridgeInput ? bridgeInput.value : "").trim();
    if (!bridgeId) {
        showToast({ title: "需要桥接标识", text: "请填写 bridge_id，例如 napcat-home。", kind: "error" });
        if (bridgeInput) bridgeInput.focus();
        return;
    }
    const body = { bridge_id: bridgeId, label: String(labelInput ? labelInput.value : "").trim() };
    const customToken = String(customInput ? customInput.value : "").trim();
    if (customToken) body.token = customToken;
    _externalBusy(true);
    try {
        const payload = await ApiClient.createExternalApiToken(body);
        _applyExternalPayload(payload);
        renderExternalTokenList();
        _setExternalFormOpen(false);
        if (bridgeInput) bridgeInput.value = "";
        if (labelInput) labelInput.value = "";
        if (customInput) customInput.value = "";
        _showExternalTokenOnce(payload.bridge_id, payload.token);
    } catch (error) {
        showToast({ title: "签发失败", text: ApiClient.friendlyErrorMessage(error), kind: "error" });
    } finally {
        _externalBusy(false);
    }
}

async function toggleExternalTokenEnabled(bridgeId, enabled) {
    _externalBusy(true);
    try {
        const payload = await ApiClient.updateExternalApiToken(bridgeId, { enabled });
        _applyExternalPayload(payload);
        renderExternalTokenList();
    } catch (error) {
        showToast({ title: "更新失败", text: ApiClient.friendlyErrorMessage(error), kind: "error" });
        await loadExternalApiView({ quiet: true });
    } finally {
        _externalBusy(false);
    }
}

async function regenerateExternalToken(bridgeId) {
    _externalBusy(true);
    try {
        const payload = await ApiClient.updateExternalApiToken(bridgeId, { regenerate: true });
        _applyExternalPayload(payload);
        renderExternalTokenList();
        _showExternalTokenOnce(payload.bridge_id || bridgeId, payload.token);
    } catch (error) {
        showToast({ title: "重新生成失败", text: ApiClient.friendlyErrorMessage(error), kind: "error" });
    } finally {
        _externalBusy(false);
    }
}

async function deleteExternalToken(bridgeId) {
    openConfirm({
        title: "删除桥接 token",
        text: `删除后 ${bridgeId} 将无法再访问 External Agent API，且不可恢复。确定删除？`,
        confirmLabel: "删除",
        onConfirm: async () => {
            _externalBusy(true);
            try {
                const payload = await ApiClient.deleteExternalApiToken(bridgeId);
                _applyExternalPayload(payload);
                renderExternalTokenList();
            } catch (error) {
                showToast({ title: "删除失败", text: ApiClient.friendlyErrorMessage(error), kind: "error" });
            } finally {
                _externalBusy(false);
            }
        },
    });
}

async function toggleExternalApiEnabled(enabled) {
    _externalBusy(true);
    try {
        const payload = await ApiClient.updateExternalApiSettings({ enabled });
        _applyExternalPayload(payload);
        _setExternalSwitch(_externalMasterSwitch(), EXTERNAL_API_VIEW_STATE.enabled, EXTERNAL_API_VIEW_STATE.enabled ? "已启用" : "已停用");
        renderExternalTokenList();
        showToast({
            title: enabled ? "External Agent API 已启用" : "External Agent API 已停用",
            text: enabled ? "外部桥接现在可以凭 token 访问 /api/v1。" : "所有外部桥接访问将被拒绝（403）。",
            kind: "info",
        });
    } catch (error) {
        showToast({ title: "更新失败", text: ApiClient.friendlyErrorMessage(error), kind: "error" });
        await loadExternalApiView({ quiet: true });
    } finally {
        _externalBusy(false);
    }
}

function initExternalApiView() {
    const refreshBtn = document.getElementById("external-refresh-btn");
    if (refreshBtn) refreshBtn.addEventListener("click", () => void loadExternalApiView());

    const master = _externalMasterSwitch();
    if (master) {
        master.addEventListener("click", () => {
            void toggleExternalApiEnabled(master.getAttribute("aria-pressed") !== "true");
        });
    }

    const createBtn = document.getElementById("external-token-create-btn");
    if (createBtn) {
        createBtn.addEventListener("click", () => {
            const form = _externalFormEl();
            _setExternalFormOpen(!!form && form.hidden);
        });
    }

    const form = _externalFormEl();
    if (form) form.addEventListener("submit", (event) => void createExternalToken(event));

    const formCancel = document.getElementById("external-token-form-cancel");
    if (formCancel) {
        formCancel.addEventListener("click", () => {
            _setExternalFormOpen(false);
        });
    }

    const onceClose = document.getElementById("external-token-once-close");
    if (onceClose) onceClose.addEventListener("click", _closeExternalTokenOnce);

    const onceCopy = document.getElementById("external-token-once-copy");
    if (onceCopy) onceCopy.addEventListener("click", () => void _copyExternalTokenOnce());

    const listEl = _externalListEl();
    if (listEl) {
        listEl.addEventListener("click", (event) => {
            const switchBtn = event.target.closest("button.external-row-switch[data-bridge]");
            if (switchBtn) {
                void toggleExternalTokenEnabled(
                    switchBtn.dataset.bridge,
                    switchBtn.getAttribute("aria-pressed") !== "true"
                );
                return;
            }
            const button = event.target.closest("button[data-action][data-bridge]");
            if (!button) return;
            const bridgeId = button.dataset.bridge;
            if (button.dataset.action === "regenerate") void regenerateExternalToken(bridgeId);
            if (button.dataset.action === "delete") void deleteExternalToken(bridgeId);
        });
    }
}
