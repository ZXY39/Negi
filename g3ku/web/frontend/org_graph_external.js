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
    // 请求在飞时文案固定在「处理中」：中途回来的状态只更新 aria-pressed，
    // 标签留到 finally 收尾再落，否则会闪一下「已启用」再跳回去。
    if (button.classList.contains("is-busy")) return;
    if (!label) return;
    // 主开关已换成「已启用/已禁用 + 呼吸点」按钮，其余仍是带滑块的开关，两套都认。
    const text = button.querySelector(".tool-governance-switch-label")
        || button.querySelector(".external-api-state-label");
    if (text) text.textContent = label;
}

function _externalBusy(next) {
    EXTERNAL_API_VIEW_STATE.busy = !!next;
    const busy = EXTERNAL_API_VIEW_STATE.busy;
    for (const id of [
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
        for (const control of listEl.querySelectorAll("[data-bridge]")) {
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
            <div class="external-token-head">
                <div class="external-token-main">
                    <div class="resource-list-title">${esc(item.bridge_id)}</div>
                    <div class="resource-list-subtitle">${item.label ? esc(item.label) + " · " : ""}token：<code>${esc(item.token_masked || "（未设置）")}</code></div>
                </div>
                <div class="external-token-tools">
                    <button class="toolbar-btn ghost small" data-action="regenerate" data-bridge="${esc(item.bridge_id)}" type="button">重新生成</button>
                    <button class="toolbar-btn ghost danger small" data-action="delete" data-bridge="${esc(item.bridge_id)}" type="button">删除</button>
                    <button class="tool-governance-switch external-row-switch" type="button"
                        data-bridge="${esc(item.bridge_id)}"
                        aria-pressed="${item.enabled ? "true" : "false"}"
                        aria-label="切换 ${esc(item.bridge_id)} 启用状态">
                        <span class="tool-governance-switch-track" aria-hidden="true"><span class="tool-governance-switch-thumb"></span></span>
                        <span class="tool-governance-switch-label">${item.enabled ? "已启用" : "已停用"}</span>
                    </button>
                </div>
            </div>
            <div class="external-token-actions">
                <span class="external-row-scope"><span class="external-row-scope-text">可会话范围</span>
                    <select id="external-scope-${esc(item.bridge_id)}" class="resource-select external-scope-select"
                        data-bridge="${esc(item.bridge_id)}" data-action="scope"
                        data-resource-select-label="可会话范围" data-resource-select-portal="true"
                        aria-label="${esc(item.bridge_id)} 的会话范围">
                        <option value="allow"${item.cross_session !== false ? " selected" : ""}>全部会话</option>
                        <option value="deny"${item.cross_session === false ? " selected" : ""}>仅本桥</option>
                    </select>
                </span>
                <button class="tool-governance-switch external-row-switch" type="button"
                    data-action="operator"
                    data-bridge="${esc(item.bridge_id)}"
                    aria-pressed="${item.operator ? "true" : "false"}"
                    aria-label="切换 ${esc(item.bridge_id)} 当操作员">
                    <span class="tool-governance-switch-track" aria-hidden="true"><span class="tool-governance-switch-thumb"></span></span>
                    <span class="tool-governance-switch-label">当操作员：${item.operator ? "已开启" : "已关闭"}</span>
                </button>
            </div>`;
        listEl.appendChild(row);
    }
    // 整块 innerHTML 重建会把上一次定制出来的下拉壳一起清掉，重绘后必须重新水合，
    // 否则这一列又退回原生 <select>（应用里没有第二处原生下拉是可见的）。
    if (typeof enhanceResourceSelects === "function") enhanceResourceSelects();
    if (typeof restoreOpenResourceSelect === "function") restoreOpenResourceSelect(listEl);
}

async function loadExternalApiView({ quiet = false } = {}) {
    if (EXTERNAL_API_VIEW_STATE.busy) return;
    try {
        const payload = await ApiClient.getExternalApiSettings();
        _applyExternalPayload(payload);
        _setExternalSwitch(_externalMasterSwitch(), EXTERNAL_API_VIEW_STATE.enabled, EXTERNAL_API_VIEW_STATE.enabled ? "已启用" : "已禁用");
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
    _setExternalApiSwitchBusy(true);
    try {
        const payload = await ApiClient.updateExternalApiSettings({ enabled });
        _applyExternalPayload(payload);
        _setExternalSwitch(_externalMasterSwitch(), EXTERNAL_API_VIEW_STATE.enabled, EXTERNAL_API_VIEW_STATE.enabled ? "已启用" : "已禁用");
        // 会话范围下拉由 token 列表一起重绘，这里不再单独调（旧调用指向的函数已不存在）。
        renderExternalTokenList();
        showToast({
            title: enabled ? "External Agent API 已启用" : "External Agent API 已禁用",
            text: enabled ? "外部桥接现在可以凭 token 访问 /api/v1。" : "外部桥接的请求会被拒绝。",
            kind: "info",
        });
    } catch (error) {
        showToast({ title: "更新失败", text: ApiClient.friendlyErrorMessage(error), kind: "error" });
        await loadExternalApiView({ quiet: true });
    } finally {
        _setExternalApiSwitchBusy(false);
        _externalBusy(false);
    }
}

/** 请求期间状态点换成加载环、文案换成「处理中」：这个开关一次往返好几秒，没有反馈就像没点上。 */
function _setExternalApiSwitchBusy(busy) {
    const button = _externalMasterSwitch();
    if (!button) return;
    button.classList.toggle("is-busy", !!busy);
    button.setAttribute("aria-busy", busy ? "true" : "false");
    const text = button.querySelector(".external-api-state-label");
    if (!text) return;
    text.textContent = busy
        ? "处理中"
        : (EXTERNAL_API_VIEW_STATE.enabled ? "已启用" : "已禁用");
}

async function updateExternalTokenScope(bridgeId, choice) {
    const crossSession = choice !== "deny";
    _externalBusy(true);
    try {
        const payload = await ApiClient.updateExternalApiToken(bridgeId, { crossSession });
        _applyExternalPayload(payload);
        renderExternalTokenList();
    } catch (error) {
        showToast({ title: "更新失败", text: ApiClient.friendlyErrorMessage(error), kind: "error" });
        await loadExternalApiView({ quiet: true });
    } finally {
        _externalBusy(false);
    }
}

async function _submitExternalOperator(bridgeId, next, password = "") {
    _externalBusy(true);
    try {
        const body = { operator: !!next };
        if (next && password) body.unlock_password = password;
        const payload = await ApiClient.updateExternalApiToken(bridgeId, body);
        _applyExternalPayload(payload);
        renderExternalTokenList();
        return payload;
    } finally {
        _externalBusy(false);
    }
}

// 「当操作员」= 该桥的会话被认成操作员（档位 / 记忆 / 人设 / 任务同源）。开启要口令，
// 收回不要——能关的人不该被关在门外。
async function toggleExternalOperator(bridgeId, next) {
    if (!next) {
        try {
            await _submitExternalOperator(bridgeId, false);
            showToast({
                title: "已收回操作员权限",
                text: `${bridgeId} 的会话下一条消息起按第三方处理。`,
                kind: "success",
            });
        } catch (error) {
            showToast({ title: "更新失败", text: ApiClient.friendlyErrorMessage(error), kind: "error" });
            await loadExternalApiView({ quiet: true });
        }
        return;
    }
    openConfirm({
        title: `允许 ${bridgeId} 当操作员`,
        text:
            "开启后，这个桥创建的会话会被当成操作员本人：可以写长期记忆、派发任务、读写整个工作区，"
            + "沙箱档位也随之放开。等于把这个 token 当成你的身份，只在本机自用的 token 上开启。"
            + "下一条消息起生效；关掉后下一条消息即收回。",
        confirmLabel: "开启",
        confirmKind: "danger",
        textFull: true,
        inputPlain: true,
        input: { label: "项目解锁口令", type: "password", placeholder: "仅本次开启使用", required: true },
        onConfirm: async ({ value }) => {
            const password = String(value || "").trim();
            if (!password) throw new Error("请输入项目解锁口令。");
            try {
                await _submitExternalOperator(bridgeId, true, password);
            } catch (error) {
                throw new Error(ApiClient.friendlyErrorMessage(error));
            }
            showToast({
                title: "已允许当操作员",
                text: `${bridgeId} 的会话按操作员处理：可写长期记忆、派发任务、读写工作区。下一条消息起生效。`,
                kind: "warn",
            });
        },
    });
}

function initExternalApiView() {
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
                const next = switchBtn.getAttribute("aria-pressed") !== "true";
                if (switchBtn.dataset.action === "operator") toggleExternalOperator(switchBtn.dataset.bridge, next);
                else void toggleExternalTokenEnabled(switchBtn.dataset.bridge, next);
                return;
            }
            const button = event.target.closest("button[data-action][data-bridge]");
            if (!button) return;
            const bridgeId = button.dataset.bridge;
            if (button.dataset.action === "regenerate") void regenerateExternalToken(bridgeId);
            if (button.dataset.action === "delete") void deleteExternalToken(bridgeId);
        });
        listEl.addEventListener("change", (event) => {
            const select = event.target.closest("select[data-action='scope'][data-bridge]");
            if (!select) return;
            void updateExternalTokenScope(select.dataset.bridge, select.value);
        });
    }

    initQqBotView();
}

// ---- 官方 QQ 机器人面板：一个 AppID 一行账号 ----
const QQ_BOT_STATE = { loaded: false, busy: false, enabled: false, rows: [], editingIndex: -1 };

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
    // 徽标配色沿用状态标签那套语义档：接通=绿、失败=红、连接中=蓝，其余走中性档。
    const badge = { connected: "completed", error: "failed", connecting: "running" }[state] || "pending";
    return { state, text, badge };
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
        const appId = String(row.app_id || "");
        // 未落库的行铺输入框，已保存的行收成单行卡片。class 一次写全，别分两步加。
        el.className = `resource-list-item qq-bot-account-row${appId ? "" : " is-new"}`;
        el.dataset.index = String(index);
        if (!appId) {
            // 临时卡片：三个输入框一行排开，右边一个小叉号撤掉它（不留「删除」按钮，
            // 因为这张卡还没有对象可删）。
            el.innerHTML = `
                <input class="resource-search qq-bot-appid" type="text" value="" autocomplete="off" placeholder="AppID">
                <input class="resource-search qq-bot-secret" type="password" autocomplete="new-password" placeholder="AppSecret">
                <input class="resource-search qq-bot-label" type="text" value="${esc(row.label || "")}" autocomplete="off" placeholder="备注（可选）">
                <button class="qq-bot-account-remove-x" data-action="remove" type="button"
                    aria-label="移除这个临时账号" title="移除">&times;</button>`;
            listEl.appendChild(el);
            return;
        }
        const editing = QQ_BOT_STATE.editingIndex === index;
        const ownerCount = Array.isArray(row.owner_openids) ? row.owner_openids.length : 0;
        // 编辑态就地换成一个输入框：改备注不该为一行字开一整个弹窗。
        const titleParts = editing
            ? `<input class="resource-search qq-bot-label qq-bot-label-inline" type="text"
                    value="${esc(row.label || "")}" data-inline-label="${index}"
                    placeholder="备注，留空显示 AppID" autocomplete="off">`
            : `<button class="qq-bot-account-title" type="button" data-action="edit-label"
                    title="点击修改备注">${esc(row.label || appId)}</button>`;
        el.innerHTML = `
            <input class="qq-bot-appid" type="hidden" value="${esc(appId)}">
            <input class="qq-bot-secret" type="hidden" value="">
            ${editing ? "" : `<input class="qq-bot-label" type="hidden" value="${esc(row.label || "")}">`}
            ${titleParts}
            <span class="qq-bot-account-appid-text">AppID ${esc(appId)}</span>
            <span class="status-badge qq-bot-account-status" data-status="${esc(status.badge)}"
                title="${esc(status.text)}">${esc(status.text)}</span>
            <button class="tool-governance-switch qq-bot-row-switch" type="button"
                aria-pressed="${row.enabled ? "true" : "false"}" aria-label="切换该号启用状态">
                <span class="tool-governance-switch-track" aria-hidden="true"><span class="tool-governance-switch-thumb"></span></span>
                <span class="tool-governance-switch-label">${row.enabled ? "已启用" : "已停用"}</span>
            </button>
            ${ownerCount
                ? `<button class="toolbar-btn ghost danger small" data-action="unregister-master" type="button"
                    title="清空该 QQ 号的所有 Master 身份与标签">注销 Master</button>`
                : `<button class="toolbar-btn ghost small" data-action="register-master" type="button"
                    title="生成 5 分钟一次性绑定码，用 QQ 私聊发送给机器人">注册Master</button>`}
            <button class="toolbar-btn ghost small" data-action="edit-secret" type="button">改密钥</button>
            <button class="toolbar-btn ghost danger small" data-action="remove" type="button">删除</button>`;
        listEl.appendChild(el);
    });
    _renderQqBotStatus();
    _syncQqBotSaveHighlight();
    if (typeof icons === "function") icons(true);
}

/** 保存按钮只有在「有还没落库、且 AppID 与 AppSecret 都填了」的行时才点亮。 */
function _syncQqBotSaveHighlight() {
    const btn = document.getElementById("qq-bot-save-btn");
    if (!btn) return;
    const listEl = _qqBotListEl();
    let ready = false;
    if (listEl) {
        for (const el of listEl.querySelectorAll(".qq-bot-account-row.is-new")) {
            const appId = String(el.querySelector(".qq-bot-appid")?.value || "").trim();
            const secret = String(el.querySelector(".qq-bot-secret")?.value || "").trim();
            if (appId && secret) {
                ready = true;
                break;
            }
        }
    }
    btn.classList.toggle("is-ready", ready);
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
        const appId = String(appIdInput ? appIdInput.value : "").trim();
        if (!appId) continue;
        rows.push({
            app_id: appId,
            app_secret: String(secretInput ? secretInput.value : "").trim(),
            label: String(labelInput ? labelInput.value : "").trim(),
            // 沙箱不再暴露给界面，写死 false：存量勾过沙箱的号会在下次保存时归位。
            sandbox: false,
            enabled: previous.enabled !== false,
            has_secret: !!previous.has_secret,
            owner_openids: Array.isArray(previous.owner_openids) ? previous.owner_openids.slice() : [],
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
            // 与 `_collectQqBotRows` 同一口径：界面没有沙箱开关，恒为关闭。
            sandbox: false,
            enabled: row.enabled !== false,
            owner_openids: Array.isArray(row.owner_openids) ? row.owner_openids.slice() : [],
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
        owner_openids: Array.isArray(row.owner_openids) ? row.owner_openids.map((item) => String(item || "").trim()).filter(Boolean) : [],
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
    QQ_BOT_STATE.editingIndex = -1;
    QQ_BOT_STATE.rows.push({ app_id: "", app_secret: "", label: "", sandbox: false, enabled: true, has_secret: false, owner_openids: [], service: null });
    renderQqBotAccounts();
    const listEl = _qqBotListEl();
    const inputs = listEl ? listEl.querySelectorAll(".qq-bot-appid") : [];
    const last = inputs[inputs.length - 1];
    if (last) last.focus();
}

function _qqBotRowFor(index) {
    // 先把界面上的未保存改动收进 state，再取目标行——否则改备注/密钥会丢上一次输入。
    const rows = _collectQqBotRows();
    if (rows) QQ_BOT_STATE.rows = rows;
    return QQ_BOT_STATE.rows[index] || null;
}

function _beginQqBotLabelEdit(index) {
    const target = _qqBotRowFor(index);
    if (!target) return;
    QQ_BOT_STATE.editingIndex = index;
    renderQqBotAccounts();
    const input = _qqBotListEl()?.querySelector(`[data-inline-label="${index}"]`);
    if (input) {
        input.focus();
        input.select?.();
    }
}

function _cancelQqBotLabelEdit() {
    if (QQ_BOT_STATE.editingIndex < 0) return;
    QQ_BOT_STATE.editingIndex = -1;
    renderQqBotAccounts();
}

async function _commitQqBotLabel(index, value) {
    if (QQ_BOT_STATE.editingIndex !== index) return;
    QQ_BOT_STATE.editingIndex = -1;
    const target = QQ_BOT_STATE.rows[index];
    if (!target) {
        renderQqBotAccounts();
        return;
    }
    const next = String(value || "").trim();
    if (next === String(target.label || "").trim()) {
        renderQqBotAccounts();
        return;
    }
    target.label = next;
    renderQqBotAccounts();
    await _persistQqBotSettings({ silentEmpty: true });
}

function _editQqBotSecret(index) {
    const target = _qqBotRowFor(index);
    if (!target) return;
    openConfirm({
        title: "更新 AppSecret",
        text: "留空表示不修改。改完该号会按新凭证重连。",
        confirmLabel: "保存",
        confirmKind: "",
        input: { label: "新的 AppSecret", type: "password", placeholder: "留空不改" },
        onConfirm: async ({ value }) => {
            const secret = String(value || "").trim();
            if (!secret) return;
            target.app_secret = secret;
            await _persistQqBotSettings({ silentEmpty: true });
        },
    });
}

// ---- Master 绑定码 ---------------------------------------------------------
// 生成前必须验项目口令；码 5 分钟有效，只接受私聊发送。入站匹配由服务端在
// 活动期内完成，注册成功后这里只负责轮询到成功并刷新账号行。
const QQ_MASTER_BIND = {
    appId: "",
    token: "",
    expiresAt: 0,
    countdownTimer: null,
    pollTimer: null,
    active: false,
};

function _qqMasterBindElements() {
    return {
        backdrop: document.getElementById("qq-master-bind-backdrop"),
        code: document.getElementById("qq-master-bind-code"),
        copy: document.getElementById("qq-master-bind-copy"),
        status: document.getElementById("qq-master-bind-status"),
        statusText: document.getElementById("qq-master-bind-status-text"),
        countdown: document.getElementById("qq-master-bind-countdown"),
    };
}

function _stopQqMasterTimers() {
    if (QQ_MASTER_BIND.countdownTimer) {
        window.clearInterval(QQ_MASTER_BIND.countdownTimer);
        QQ_MASTER_BIND.countdownTimer = null;
    }
    if (QQ_MASTER_BIND.pollTimer) {
        window.clearInterval(QQ_MASTER_BIND.pollTimer);
        QQ_MASTER_BIND.pollTimer = null;
    }
}

function _qqMasterBindingRemainingSeconds() {
    return Math.max(0, Math.ceil((Number(QQ_MASTER_BIND.expiresAt || 0) - Date.now()) / 1000));
}

function _setQqMasterBindStatus(state, text) {
    const els = _qqMasterBindElements();
    if (!els.status || !els.statusText) return;
    els.status.dataset.state = String(state || "");
    els.statusText.textContent = String(text || "");
}

function _renderQqMasterBindCountdown() {
    const els = _qqMasterBindElements();
    if (!els.countdown) return;
    const remaining = _qqMasterBindingRemainingSeconds();
    const minutes = Math.floor(remaining / 60);
    const seconds = remaining % 60;
    els.countdown.textContent = remaining > 0
        ? `${minutes}:${String(seconds).padStart(2, "0")}`
        : "0:00";
    if (remaining <= 0 && QQ_MASTER_BIND.active) _expireQqMasterBinding();
}

function _expireQqMasterBinding() {
    if (!QQ_MASTER_BIND.active) return;
    QQ_MASTER_BIND.active = false;
    _stopQqMasterTimers();
    _setQqMasterBindStatus("expired", "绑定码已过期，请关闭后重新生成。");
    const els = _qqMasterBindElements();
    if (els.copy) els.copy.disabled = true;
}

function _closeQqMasterBindingModal() {
    _stopQqMasterTimers();
    QQ_MASTER_BIND.active = false;
    QQ_MASTER_BIND.appId = "";
    QQ_MASTER_BIND.token = "";
    const els = _qqMasterBindElements();
    if (els.backdrop) {
        els.backdrop.hidden = true;
        els.backdrop.classList.remove("is-open");
    }
    if (els.code) els.code.textContent = "";
    if (els.copy) els.copy.disabled = false;
}

async function _pollQqMasterBinding() {
    if (!QQ_MASTER_BIND.active) return;
    if (_qqMasterBindingRemainingSeconds() <= 0) {
        _expireQqMasterBinding();
        return;
    }
    try {
        const payload = await ApiClient.getQqMasterBindingStatus(QQ_MASTER_BIND.appId);
        if (!QQ_MASTER_BIND.active) return;
        if (payload?.registered === true) {
            QQ_MASTER_BIND.active = false;
            _stopQqMasterTimers();
            _setQqMasterBindStatus("success", "已注册 Master。");
            const els = _qqMasterBindElements();
            if (els.countdown) els.countdown.textContent = "已识别";
            await loadQqBotView();
            window.setTimeout(() => _closeQqMasterBindingModal(), 1500);
            return;
        }
        if (payload?.pending === false) {
            _expireQqMasterBinding();
            return;
        }
        const remaining = Number(payload?.expires_in_seconds);
        if (Number.isFinite(remaining) && remaining > 0) {
            QQ_MASTER_BIND.expiresAt = Date.now() + remaining * 1000;
            _renderQqMasterBindCountdown();
        }
    } catch (error) {
        void error;
    }
}

function openQqMasterBindingModal({ appId, token, expiresInSeconds = 300 }) {
    const els = _qqMasterBindElements();
    if (!els.backdrop || !els.code) return;
    _stopQqMasterTimers();
    QQ_MASTER_BIND.appId = String(appId || "").trim();
    QQ_MASTER_BIND.token = String(token || "").trim();
    QQ_MASTER_BIND.expiresAt = Date.now() + Math.max(1, Number(expiresInSeconds) || 300) * 1000;
    QQ_MASTER_BIND.active = true;
    els.code.textContent = QQ_MASTER_BIND.token;
    if (els.copy) els.copy.disabled = false;
    _setQqMasterBindStatus("waiting", "等待识别中…");
    _renderQqMasterBindCountdown();
    els.backdrop.hidden = false;
    els.backdrop.classList.add("is-open");
    QQ_MASTER_BIND.countdownTimer = window.setInterval(_renderQqMasterBindCountdown, 1000);
    QQ_MASTER_BIND.pollTimer = window.setInterval(() => void _pollQqMasterBinding(), 2000);
    void _pollQqMasterBinding();
}

async function _copyQqMasterCode() {
    const token = String(QQ_MASTER_BIND.token || "").trim();
    if (!token) return;
    try {
        await navigator.clipboard.writeText(token);
    } catch (error) {
        void error;
        const area = document.createElement("textarea");
        area.value = token;
        area.setAttribute("readonly", "");
        area.style.position = "fixed";
        area.style.opacity = "0";
        document.body.appendChild(area);
        area.select();
        document.execCommand("copy");
        area.remove();
    }
    showToast({ title: "已复制绑定码", text: "用 QQ 私聊发给机器人即可。", kind: "success" });
}

function _registerQqMaster(index) {
    const target = _qqBotRowFor(index);
    if (!target) return;
    const appId = String(target.app_id || "").trim();
    if (!appId) return;
    openConfirm({
        title: "注册 Master",
        text: "注册后这个 QQ 号的私聊按 owner 档处理：可写本地记忆、读取人设，任务门按 owner。群聊仍按第三方整会话执行，只改标签。\n\n下一步生成一个 5 分钟有效的绑定码，用你的 QQ 私聊发给机器人；只接受私聊，群里发送会作废。",
        confirmLabel: "生成绑定码",
        confirmKind: "",
        textFull: true,
        onConfirm: async () => {
            window.setTimeout(() => _confirmQqMasterPassword(appId), 0);
        },
    });
}

function _confirmQqMasterPassword(appId) {
    openConfirm({
        title: "确认生成绑定码",
        text: "生成绑定码需要项目解锁口令。",
        confirmLabel: "生成",
        confirmKind: "",
        input: { label: "项目解锁口令", type: "password" },
        onConfirm: async ({ value }) => {
            const password = String(value || "").trim();
            if (!password) throw new Error("需要项目解锁口令");
            const payload = await ApiClient.createQqMasterBinding({
                app_id: appId,
                unlock_password: password,
            });
            window.setTimeout(() => openQqMasterBindingModal({
                appId,
                token: payload?.token,
                expiresInSeconds: payload?.expires_in_seconds,
            }), 0);
        },
    });
}

function _unregisterQqMaster(index) {
    const target = _qqBotRowFor(index);
    if (!target) return;
    const appId = String(target.app_id || "").trim();
    if (!appId) return;
    openConfirm({
        title: "注销 Master",
        text: "将清空这个 QQ 号的全部 Master 身份与标签；对应私聊会退回第三方，本地记忆重新只读。群聊仍按第三方处理。确定注销？",
        confirmLabel: "注销",
        confirmKind: "danger",
        textFull: true,
        onConfirm: async () => {
            const payload = await ApiClient.unregisterQqMaster({ app_id: appId });
            _applyQqBotPayload(payload);
            renderQqBotAccounts();
            showToast({ title: "已注销 Master", text: "该 QQ 号已退回第三方私聊。", kind: "success" });
        },
    });
}

function _removeQqBotAccountRow(index) {
    const rows = _collectQqBotRows();
    if (rows) QQ_BOT_STATE.rows = rows;
    const target = QQ_BOT_STATE.rows[index];
    if (!target) return;
    const appId = String(target.app_id || "").trim();
    const label = String(target.label || "").trim();
    const name = label ? `${label}（${appId || "未填 AppID"}）` : (appId || "这个账号");
    // 删除是整表覆盖式写入，误点一次就没了凭证；二次确认是这里唯一的一道门。
    openConfirm({
        title: "删除 QQ 账号",
        text: `删除后 ${name} 不再接收消息，重新加回需要再填一次 AppSecret。确定删除？`,
        confirmLabel: "删除",
        onConfirm: async () => {
            QQ_BOT_STATE.editingIndex = -1;
            QQ_BOT_STATE.rows.splice(index, 1);
            renderQqBotAccounts();
            // 已有 AppID 的行要真的从表里消失，否则刷新后又回来。
            if (appId) await _persistQqBotSettings({ silentEmpty: true });
        },
    });
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

// ---- QQ 机器人申请入口：应用内嵌第三方页面 ----
const QQ_BOT_APPLY_URL = "https://q.qq.com/qqbot/openclaw/login.html";

const qqBotApplyState = { backdrop: null, drawer: null, frame: null, open: false };

function ensureQqBotApplySurface() {
    if (qqBotApplyState.backdrop && qqBotApplyState.drawer) return;
    const backdrop = document.createElement("div");
    backdrop.id = "qq-bot-apply-backdrop";
    backdrop.className = "detail-backdrop";
    backdrop.setAttribute("aria-hidden", "true");

    const drawer = document.createElement("section");
    drawer.id = "qq-bot-apply-drawer";
    drawer.className = "panel detail-drawer qq-bot-apply-drawer";
    drawer.setAttribute("role", "dialog");
    drawer.setAttribute("aria-modal", "true");
    drawer.setAttribute("aria-hidden", "true");
    drawer.setAttribute("aria-labelledby", "qq-bot-apply-title");
    drawer.tabIndex = -1;
    drawer.innerHTML = `
        <div class="detail-modal-header">
            <div>
                <h2 id="qq-bot-apply-title">申请 QQ 机器人</h2>
                <p class="subtitle">在 QQ 开放平台创建机器人，拿到 AppID 与 AppSecret 后填到账号里。登录被拦时点右上角用浏览器打开。</p>
            </div>
            <div class="detail-modal-actions">
                <a id="qq-bot-apply-external" class="toolbar-btn ghost" href="${QQ_BOT_APPLY_URL}"
                    target="_blank" rel="noreferrer noopener">浏览器打开</a>
                <button id="qq-bot-apply-close" class="toolbar-btn ghost" type="button" data-modal-close>关闭</button>
            </div>
        </div>
        <div class="detail-modal-body qq-bot-apply-body">
            <iframe id="qq-bot-apply-frame" class="qq-bot-apply-frame" title="QQ 开放平台 · 创建机器人"></iframe>
        </div>`;

    document.body.appendChild(backdrop);
    document.body.appendChild(drawer);
    qqBotApplyState.backdrop = backdrop;
    qqBotApplyState.drawer = drawer;
    qqBotApplyState.frame = drawer.querySelector("#qq-bot-apply-frame");

    drawer.querySelector("#qq-bot-apply-close")?.addEventListener("click", () => closeQqBotApply());
    backdrop.addEventListener("click", (event) => {
        if (event.target === backdrop) closeQqBotApply();
    });
    document.addEventListener("keydown", (event) => {
        if (event.key !== "Escape" || !qqBotApplyState.open) return;
        event.preventDefault();
        closeQqBotApply();
    });
}

function openQqBotApply() {
    ensureQqBotApplySurface();
    // src 只在打开时写：页面一加载就挂上去会白跑一次第三方请求。
    if (qqBotApplyState.frame && qqBotApplyState.frame.src !== QQ_BOT_APPLY_URL) {
        qqBotApplyState.frame.src = QQ_BOT_APPLY_URL;
    }
    setDrawerOpen(qqBotApplyState.backdrop, qqBotApplyState.drawer, true);
    qqBotApplyState.open = true;
    if (typeof icons === "function") icons(true);
    return true;
}

function closeQqBotApply() {
    if (!qqBotApplyState.open) return false;
    setDrawerOpen(qqBotApplyState.backdrop, qqBotApplyState.drawer, false);
    qqBotApplyState.open = false;
    // 摘掉 src：别把一个已登录的第三方页面留在后台继续跑。
    if (qqBotApplyState.frame) qqBotApplyState.frame.src = "about:blank";
    return true;
}

function initQqBotView() {
    const bindCopy = document.getElementById("qq-master-bind-copy");
    if (bindCopy) bindCopy.addEventListener("click", () => void _copyQqMasterCode());
    const bindClose = document.getElementById("qq-master-bind-close");
    if (bindClose) bindClose.addEventListener("click", () => _closeQqMasterBindingModal());
    const bindBackdrop = document.getElementById("qq-master-bind-backdrop");
    if (bindBackdrop) bindBackdrop.addEventListener("click", (event) => {
        if (event.target === bindBackdrop) _closeQqMasterBindingModal();
    });
    const toggle = document.getElementById("qq-bot-enabled-toggle");
    if (toggle) toggle.addEventListener("click", () => void toggleQqBotEnabled(toggle.getAttribute("aria-pressed") !== "true"));
    const form = document.getElementById("qq-bot-settings-form");
    if (form) form.addEventListener("submit", (event) => void saveQqBotSettings(event));
    const addBtn = document.getElementById("qq-bot-account-add-btn");
    if (addBtn) addBtn.addEventListener("click", (event) => {
        event.preventDefault();
        _addQqBotAccountRow();
    });
    const applyBtn = document.getElementById("qq-bot-apply-entry-btn");
    if (applyBtn) applyBtn.addEventListener("click", (event) => {
        event.preventDefault();
        openQqBotApply();
    });
    const listEl = _qqBotListEl();
    if (listEl) {
        listEl.addEventListener("click", (event) => {
            const rowEl = event.target.closest(".qq-bot-account-row");
            if (!rowEl) return;
            const index = Number(rowEl.dataset.index);
            if (event.target.closest("[data-action='remove']")) {
                if (rowEl.classList.contains("is-new")) {
                    // 临时卡还没落进任何账本，也没有可删的对象：小叉号一点就撤，不弹确认。
                    QQ_BOT_STATE.editingIndex = -1;
                    QQ_BOT_STATE.rows.splice(index, 1);
                    renderQqBotAccounts();
                    return;
                }
                void _removeQqBotAccountRow(index);
                return;
            }
            if (event.target.closest("[data-action='edit-label']")) {
                _beginQqBotLabelEdit(index);
                return;
            }
            if (event.target.closest("[data-action='edit-secret']")) {
                _editQqBotSecret(index);
                return;
            }
            if (event.target.closest("[data-action='register-master']")) {
                _registerQqMaster(index);
                return;
            }
            if (event.target.closest("[data-action='unregister-master']")) {
                _unregisterQqMaster(index);
                return;
            }
            if (event.target.closest(".qq-bot-row-switch")) void _toggleQqBotRowEnabled(index);
        });
        // 临时卡填齐 AppID + AppSecret 时把「保存并连接」点亮。
        listEl.addEventListener("input", () => _syncQqBotSaveHighlight());
        listEl.addEventListener("keydown", (event) => {
            const input = event.target instanceof Element ? event.target.closest(".qq-bot-label-inline") : null;
            if (!input) return;
            if (event.key === "Enter") {
                event.preventDefault();
                void _commitQqBotLabel(Number(input.dataset.inlineLabel), input.value);
            } else if (event.key === "Escape") {
                event.preventDefault();
                _cancelQqBotLabelEdit();
            }
        });
        // blur 不冒泡，用捕获接。重渲染会先摘掉输入框，所以提交口子只认仍在编辑态的那一个。
        listEl.addEventListener("blur", (event) => {
            const input = event.target instanceof Element ? event.target.closest(".qq-bot-label-inline") : null;
            if (!input) return;
            void _commitQqBotLabel(Number(input.dataset.inlineLabel), input.value);
        }, true);
    }
}
