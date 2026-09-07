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

function _externalMasterToggle() {
    return document.getElementById("external-api-enabled-toggle");
}

function _externalOnceCard() {
    return document.getElementById("external-token-once");
}

function _externalBusy(next) {
    EXTERNAL_API_VIEW_STATE.busy = !!next;
    const createBtn = document.getElementById("external-token-create-btn");
    if (createBtn) createBtn.disabled = EXTERNAL_API_VIEW_STATE.busy;
    const master = _externalMasterToggle();
    if (master) master.disabled = EXTERNAL_API_VIEW_STATE.busy;
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
        listEl.innerHTML = '<div class="resource-empty">还没有桥接 token。点击「签发桥接 token」为外部桥接应用创建一个。</div>';
        return;
    }
    listEl.innerHTML = "";
    for (const item of items) {
        const row = document.createElement("div");
        row.className = "resource-list-item external-token-item";
        row.innerHTML = `
            <div class="external-token-main">
                <div class="resource-list-title">${esc(item.bridge_id)}</div>
                <div class="resource-list-subtitle">
                    ${item.label ? esc(item.label) + " · " : ""}token：<code>${esc(item.token_masked || "（未设置）")}</code>
                </div>
            </div>
            <div class="external-token-actions">
                <label class="external-token-enabled">
                    <input type="checkbox" data-bridge="${esc(item.bridge_id)}" ${item.enabled ? "checked" : ""}>
                    <span>${item.enabled ? "已启用" : "已停用"}</span>
                </label>
                <button class="toolbar-btn ghost" data-action="regenerate" data-bridge="${esc(item.bridge_id)}" type="button">重新生成</button>
                <button class="toolbar-btn ghost danger" data-action="delete" data-bridge="${esc(item.bridge_id)}" type="button">删除</button>
            </div>`;
        listEl.appendChild(row);
    }
}

async function loadExternalApiView({ quiet = false } = {}) {
    if (EXTERNAL_API_VIEW_STATE.busy) return;
    try {
        const payload = await ApiClient.getExternalApiSettings();
        _applyExternalPayload(payload);
        const master = _externalMasterToggle();
        if (master) master.checked = EXTERNAL_API_VIEW_STATE.enabled;
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
}

async function createExternalToken() {
    const bridgeId = window.prompt("桥接标识（bridge_id，小写字母/数字/-/_）：");
    if (bridgeId === null) return;
    const normalized = String(bridgeId).trim();
    if (!normalized) {
        showToast({ title: "需要桥接标识", text: "bridge_id 不能为空。", kind: "error" });
        return;
    }
    const label = window.prompt("备注名（可选，例如：家里 NapCat 桥）：") || "";
    _externalBusy(true);
    try {
        const payload = await ApiClient.createExternalApiToken({ bridge_id: normalized, label: String(label).trim() });
        _applyExternalPayload(payload);
        renderExternalTokenList();
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
        const master = _externalMasterToggle();
        if (master) master.checked = EXTERNAL_API_VIEW_STATE.enabled;
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

    const master = _externalMasterToggle();
    if (master) master.addEventListener("change", (event) => void toggleExternalApiEnabled(!!event.target.checked));

    const createBtn = document.getElementById("external-token-create-btn");
    if (createBtn) createBtn.addEventListener("click", () => void createExternalToken());

    const onceClose = document.getElementById("external-token-once-close");
    if (onceClose) {
        onceClose.addEventListener("click", () => {
            const card = _externalOnceCard();
            if (card) card.hidden = true;
            const text = document.getElementById("external-token-once-text");
            if (text) text.value = "";
        });
    }

    const listEl = _externalListEl();
    if (listEl) {
        listEl.addEventListener("change", (event) => {
            const target = event.target;
            if (target && target.matches('input[type="checkbox"][data-bridge]')) {
                void toggleExternalTokenEnabled(target.dataset.bridge, !!target.checked);
            }
        });
        listEl.addEventListener("click", (event) => {
            const button = event.target.closest("button[data-action][data-bridge]");
            if (!button) return;
            const bridgeId = button.dataset.bridge;
            if (button.dataset.action === "regenerate") void regenerateExternalToken(bridgeId);
            if (button.dataset.action === "delete") void deleteExternalToken(bridgeId);
        });
    }
}
