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
    void loadQqBotView();
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

    initQqBotView();
}

// ---- 官方 QQ 机器人面板：一个 AppID 一行账号 ----
const QQ_BOT_STATE = { loaded: false, busy: false, enabled: false, rows: [] };

function _qqBotListEl() {
    return document.getElementById("qq-bot-accounts");
}

function _qqBotServiceLabel(state) {
    const s = String(state || "stopped").toLowerCase();
    const map = {
        connected: "已连接",
        connecting: "连接中…",
        error: "错误",
        enabled_off: "总开关已关",
        account_disabled: "本号已停用",
        not_configured: "未配置",
        stopped: "已停止",
    };
    return map[s] || s;
}

function _qqBotRowStatus(row) {
    const svc = row.service || {};
    const state = String(svc.state || "stopped").toLowerCase();
    const detail = String(svc.detail || "").trim();
    let text = _qqBotServiceLabel(state);
    if (detail) text += `（${detail}）`;
    else if (!row.has_secret && !String(row.app_secret || "").trim()) text += " · 尚未保存 AppSecret";
    return { state, text };
}

function renderQqBotAccounts() {
    const listEl = _qqBotListEl();
    if (!listEl) return;
    if (!QQ_BOT_STATE.loaded) {
        listEl.innerHTML = '<div class="resource-empty">加载中…</div>';
        return;
    }
    if (!QQ_BOT_STATE.rows.length) {
        listEl.innerHTML = '<div class="resource-empty">还没有 QQ 账号，点下方「添加账号」。</div>';
        return;
    }
    listEl.innerHTML = "";
    QQ_BOT_STATE.rows.forEach((row, index) => {
        const status = _qqBotRowStatus(row);
        const el = document.createElement("div");
        el.className = "resource-list-item qq-bot-account-row";
        el.dataset.index = String(index);
        el.innerHTML = `
            <div class="llm-form-grid qq-bot-account-fields">
                <label class="resource-field"><span class="resource-field-label">AppID</span>
                    <input class="resource-search qq-bot-appid" type="text" value="${esc(row.app_id || "")}" autocomplete="off"></label>
                <label class="resource-field"><span class="resource-field-label">AppSecret</span>
                    <input class="resource-search qq-bot-secret" type="password" autocomplete="new-password"
                        placeholder="${row.has_secret ? "已保存，留空不改" : "仅保存，不回显"}"></label>
                <label class="resource-field"><span class="resource-field-label">备注</span>
                    <input class="resource-search qq-bot-label" type="text" value="${esc(row.label || "")}" autocomplete="off"></label>
                <label class="resource-field qq-bot-sandbox-field"><span class="resource-field-label">环境</span>
                    <label class="qq-bot-sandbox"><input class="qq-bot-sandbox-input" type="checkbox"
                        ${row.sandbox ? "checked" : ""}>沙箱</label></label>
            </div>
            <div class="external-token-actions">
                <button class="tool-governance-switch qq-bot-row-switch" type="button"
                    aria-pressed="${row.enabled ? "true" : "false"}" aria-label="切换该号启用状态">
                    <span class="tool-governance-switch-track" aria-hidden="true"><span class="tool-governance-switch-thumb"></span></span>
                    <span class="tool-governance-switch-label">${row.enabled ? "已启用" : "已停用"}</span>
                </button>
                <span class="qq-bot-status${status.state === "connected" ? " is-ok" : status.state === "error" ? " is-error" : ""}">${esc(status.text)}</span>
                <button class="toolbar-btn ghost danger small" data-action="remove" type="button">删除</button>
            </div>`;
        listEl.appendChild(el);
    });
    _renderQqBotStatus();
}

function _renderQqBotStatus() {
    const el = document.getElementById("qq-bot-status-text");
    if (!el) return;
    if (!QQ_BOT_STATE.loaded) {
        el.textContent = "";
        return;
    }
    if (!QQ_BOT_STATE.enabled) {
        el.textContent = "总开关已关";
        return;
    }
    const rows = QQ_BOT_STATE.rows.filter((row) => row.enabled);
    if (!rows.length) {
        el.textContent = "没有启用中的账号";
        return;
    }
    const connected = rows.filter((row) => String((row.service || {}).state || "").toLowerCase() === "connected").length;
    el.textContent = `已连接 ${connected} / 启用 ${rows.length}`;
}

function _collectQqBotRows() {
    const listEl = _qqBotListEl();
    if (!listEl) return null;
    const rows = [];
    for (const el of listEl.querySelectorAll(".qq-bot-account-row")) {
        const index = Number(el.dataset.index);
        const previous = QQ_BOT_STATE.rows[index] || {};
        const appIdInput = el.querySelector(".qq-bot-appid");
        const secretInput = el.querySelector(".qq-bot-secret");
        const labelInput = el.querySelector(".qq-bot-label");
        const sandboxInput = el.querySelector(".qq-bot-sandbox-input");
        const appId = String(appIdInput ? appIdInput.value : "").trim();
        if (!appId) continue;
        rows.push({
            app_id: appId,
            app_secret: String(secretInput ? secretInput.value : "").trim(),
            label: String(labelInput ? labelInput.value : "").trim(),
            sandbox: !!(sandboxInput && sandboxInput.checked),
            enabled: previous.enabled !== false,
            has_secret: !!previous.has_secret,
            service: previous.service || null,
        });
    }
    return rows;
}

function _qqBotRowsBody() {
    const seen = new Set();
    const accounts = [];
    for (const row of QQ_BOT_STATE.rows) {
        const appId = String(row.app_id || "").trim();
        if (!appId || seen.has(appId)) return null;
        seen.add(appId);
        accounts.push({
            app_id: appId,
            app_secret: String(row.app_secret || "").trim(),
            label: String(row.label || "").trim(),
            sandbox: !!row.sandbox,
            enabled: row.enabled !== false,
        });
    }
    return { enabled: QQ_BOT_STATE.enabled, accounts };
}

function _applyQqBotPayload(payload) {
    QQ_BOT_STATE.enabled = !!(payload && payload.enabled);
    QQ_BOT_STATE.rows = ((payload && payload.accounts) || []).map((row) => ({
        app_id: String(row.app_id || ""),
        app_secret: "",
        label: String(row.label || ""),
        sandbox: !!row.sandbox,
        enabled: row.enabled !== false,
        has_secret: !!row.has_secret,
        service: row.service || null,
    }));
    QQ_BOT_STATE.loaded = true;
    _setExternalSwitch(document.getElementById("qq-bot-enabled-toggle"), QQ_BOT_STATE.enabled, QQ_BOT_STATE.enabled ? "已启用" : "已停用");
}

async function loadQqBotView() {
    if (QQ_BOT_STATE.busy) return;
    try {
        _applyQqBotPayload(await ApiClient.getQqBotSettings());
        renderQqBotAccounts();
    } catch (error) {
        showToast({ title: "加载 QQ 机器人配置失败", text: ApiClient.friendlyErrorMessage(error), kind: "error" });
    }
}

async function _persistQqBotSettings({ silentEmpty = false } = {}) {
    if (QQ_BOT_STATE.busy) return;
    const body = _qqBotRowsBody();
    if (body === null) {
        showToast({ title: "AppID 重复", text: "同一个 AppID 只能配一行。", kind: "error" });
        return;
    }
    if (!body.accounts.length && !body.enabled && !silentEmpty) {
        showToast({ title: "还没有账号", text: "先「添加账号」并填 AppID。", kind: "info" });
        return;
    }
    QQ_BOT_STATE.busy = true;
    const saveBtn = document.getElementById("qq-bot-save-btn");
    if (saveBtn) saveBtn.disabled = true;
    try {
        _applyQqBotPayload(await ApiClient.updateQqBotSettings(body));
        renderQqBotAccounts();
        showToast({ title: "已保存", text: "QQ 账号配置已保存，正在按需连接。", kind: "success" });
    } catch (error) {
        showToast({ title: "保存失败", text: ApiClient.friendlyErrorMessage(error), kind: "error" });
        await loadQqBotView();
    } finally {
        QQ_BOT_STATE.busy = false;
        if (saveBtn) saveBtn.disabled = false;
    }
}

async function saveQqBotSettings(event) {
    if (event) event.preventDefault();
    const rows = _collectQqBotRows();
    if (rows) QQ_BOT_STATE.rows = rows;
    await _persistQqBotSettings();
}

async function toggleQqBotEnabled(enabled) {
    if (QQ_BOT_STATE.busy) return;
    QQ_BOT_STATE.enabled = !!enabled;
    _setExternalSwitch(document.getElementById("qq-bot-enabled-toggle"), QQ_BOT_STATE.enabled, QQ_BOT_STATE.enabled ? "已启用" : "已停用");
    await _persistQqBotSettings({ silentEmpty: true });
}

function _addQqBotAccountRow() {
    const rows = _collectQqBotRows();
    if (rows) QQ_BOT_STATE.rows = rows;
    QQ_BOT_STATE.rows.push({ app_id: "", app_secret: "", label: "", sandbox: false, enabled: true, has_secret: false, service: null });
    renderQqBotAccounts();
    const listEl = _qqBotListEl();
    const inputs = listEl ? listEl.querySelectorAll(".qq-bot-appid") : [];
    const last = inputs[inputs.length - 1];
    if (last) last.focus();
}

async function _removeQqBotAccountRow(index) {
    const rows = _collectQqBotRows();
    if (rows) QQ_BOT_STATE.rows = rows;
    const target = QQ_BOT_STATE.rows[index];
    QQ_BOT_STATE.rows.splice(index, 1);
    renderQqBotAccounts();
    // 已有 AppID 的行要真的从表里消失，否则刷新后又回来。
    if (target && String(target.app_id || "").trim()) await _persistQqBotSettings({ silentEmpty: true });
}

async function _toggleQqBotRowEnabled(index) {
    const rows = _collectQqBotRows();
    if (rows) QQ_BOT_STATE.rows = rows;
    const target = QQ_BOT_STATE.rows[index];
    if (!target) return;
    target.enabled = target.enabled === false;
    renderQqBotAccounts();
    await _persistQqBotSettings({ silentEmpty: true });
}

function initQqBotView() {
    const toggle = document.getElementById("qq-bot-enabled-toggle");
    if (toggle) toggle.addEventListener("click", () => void toggleQqBotEnabled(toggle.getAttribute("aria-pressed") !== "true"));
    const form = document.getElementById("qq-bot-settings-form");
    if (form) form.addEventListener("submit", (event) => void saveQqBotSettings(event));
    const addBtn = document.getElementById("qq-bot-account-add-btn");
    if (addBtn) addBtn.addEventListener("click", (event) => {
        event.preventDefault();
        _addQqBotAccountRow();
    });
    const listEl = _qqBotListEl();
    if (listEl) {
        listEl.addEventListener("click", (event) => {
            const rowEl = event.target.closest(".qq-bot-account-row");
            if (!rowEl) return;
            const index = Number(rowEl.dataset.index);
            if (event.target.closest("[data-action='remove']")) {
                void _removeQqBotAccountRow(index);
                return;
            }
            if (event.target.closest(".qq-bot-row-switch")) void _toggleQqBotRowEnabled(index);
        });
    }
}
