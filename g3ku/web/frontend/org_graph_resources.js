// Resource views (skills, tools, communications) restored from org_graph_app.js.
// Loaded after org_graph_app.js so shared state/helpers are already available.

function filterSkills() {
    const q = String(U.skillSearch.value || "").trim().toLowerCase();
    return S.skills.filter((skill) => {
        if (q && !`${skill.skill_id} ${skill.display_name} ${skill.source_path}`.toLowerCase().includes(q)) return false;
        if (U.skillRisk.value !== "all" && skill.risk_level !== U.skillRisk.value) return false;
        if (U.skillStatus.value === "enabled" && !skill.enabled) return false;
        if (U.skillStatus.value === "disabled" && skill.enabled) return false;
        if (U.skillStatus.value === "unavailable" && skill.available) return false;
        return true;
    });
}

function displayRoleLabel(role) {
    return ({ ceo: "主Agent", execution: "执行", inspection: "检验" }[roleKey(role)] || String(role || ""));
}

function displayRiskLabel(level) {
    return ({ low: "低风险", medium: "中风险", high: "高风险" }[String(level || "").trim().toLowerCase()] || String(level || "未知风险"));
}

function resourceAvailabilityStatus(item) {
    if (toolRepairRequired(item)) return "repair-required";
    if (item?.available === false) return "unavailable";
    return item?.enabled ? "enabled" : "disabled";
}

function toolRepairRequired(item) {
    if (!item) return false;
    if (item.repair_required === true) return true;
    const metadata = item && typeof item.metadata === "object" ? item.metadata : {};
    if (metadata.repair_required === true) return true;
    return item.callable === true && item.available === false;
}

function normalizeResourceAvailabilityReason(reason, item = null) {
    const text = String(reason || "").trim();
    const metadata = item && typeof item.metadata === "object" ? item.metadata : {};
    const requiredBins = Array.isArray(metadata.requires_bins) ? metadata.requires_bins.filter(Boolean) : [];
    const requiredEnv = Array.isArray(metadata.requires_env) ? metadata.requires_env.filter(Boolean) : [];
    const requiredTools = Array.isArray(item?.requires_tools) ? item.requires_tools.filter(Boolean) : [];
    if (!text) return "";
    if (text === "missing required bins") return requiredBins.length ? `缺少必需命令：${requiredBins.join(", ")}` : "缺少必需命令";
    if (text === "missing required env") return requiredEnv.length ? `缺少必需环境变量：${requiredEnv.join(", ")}` : "缺少必需环境变量";
    if (text.startsWith("missing required tools:")) {
        const toolNames = text.slice("missing required tools:".length).trim();
        return `缺少依赖工具：${toolNames || requiredTools.join(", ") || "未知工具"}`;
    }
    return text;
}

function resourceAvailabilityReasons(item) {
    if (!item || item.available !== false) return [];
    const metadata = item && typeof item.metadata === "object" ? item.metadata : {};
    const warnings = Array.isArray(metadata.warnings) ? metadata.warnings : [];
    const errors = Array.isArray(metadata.errors) ? metadata.errors : [];
    const requiredBins = Array.isArray(metadata.requires_bins) ? metadata.requires_bins.filter(Boolean) : [];
    const requiredEnv = Array.isArray(metadata.requires_env) ? metadata.requires_env.filter(Boolean) : [];
    const requiredTools = Array.isArray(item.requires_tools) ? item.requires_tools.filter(Boolean) : [];
    const normalized = [...errors, ...warnings]
        .map((entry) => normalizeResourceAvailabilityReason(entry, item))
        .filter(Boolean);
    if (!normalized.length) {
        if (requiredBins.length) normalized.push(`缺少必需命令：${requiredBins.join(", ")}`);
        if (requiredEnv.length) normalized.push(`缺少必需环境变量：${requiredEnv.join(", ")}`);
        if (requiredTools.length) normalized.push(`缺少依赖工具：${requiredTools.join(", ")}`);
    }
    if (!normalized.length) normalized.push("当前资源依赖未满足，请检查运行环境。");
    return [...new Set(normalized)];
}

function displayEnabledLabel(enabled, available = true, repairRequired = false) {
    if (repairRequired) return "待修复";
    if (!available) return "不可用";
    return enabled ? "已启用" : "已禁用";
}

function dockResourceStatusBadge(root) {
    const shell = root instanceof Element ? root : null;
    const title = shell?.querySelector(".detail-modal-title");
    const subtitle = title?.querySelector(".subtitle");
    const statusRow = shell?.querySelector(".resource-status-row");
    const statusBadge = statusRow?.querySelector(".meta-tag");
    if (!title || !subtitle || !statusBadge) return;
    const meta = document.createElement("div");
    meta.className = "detail-modal-meta";
    subtitle.replaceWith(meta);
    meta.appendChild(subtitle);
    meta.appendChild(statusBadge);
}

function renderSkills() {
    U.skillList.innerHTML = "";
    const meta = paginateResources(filterSkills(), S.skillPage, S.skillPageSize);
    S.skillPage = meta.currentPage;
    S.skillPageSize = meta.pageSize;
    syncResourcePagination("skill", meta);
    if (!meta.total) return void (U.skillList.innerHTML = '<div class="empty-state">没有匹配的 Skill。</div>');
    meta.items.forEach((skill) => {
        const el = document.createElement("button");
        el.type = "button";
        el.className = `resource-list-item${S.selectedSkill?.skill_id === skill.skill_id ? " selected" : ""}`;
        const desc = (skill.description || "").trim();
        const subtitle = desc ? (desc.length > 50 ? desc.slice(0, 47) + "..." : desc) : skill.skill_id;
        el.innerHTML = `
            <div class="resource-list-title">${esc(skill.display_name)}</div>
            <div class="resource-list-subtitle">${esc(subtitle)}</div>
            <div class="resource-list-meta">
                <span class="meta-tag risk-${String(skill.risk_level || 'low').toLowerCase()}">${esc(displayRiskLabel(skill.risk_level))}</span>
                <span class="meta-tag status-${resourceAvailabilityStatus(skill)}">${esc(displayEnabledLabel(skill.enabled, skill.available))}</span>
            </div>`;
        el.addEventListener("click", () => openSkill(skill.skill_id));
        U.skillList.appendChild(el);
    });
}

function hideToolAdminAction(tool, action) {
    const toolId = String(tool?.tool_id || "").trim().toLowerCase();
    const actionId = String(action?.action_id || "").trim().toLowerCase();
    if (!toolId || !actionId) return false;
    if ((toolId === "content_navigation" || toolId === "content") && actionId === "inspect") return true;
    if (toolId === "memory" && actionId === "runtime") return true;
    return false;
}

function toolActionsForDisplay(tool) {
    const actions = Array.isArray(tool?.actions) ? tool.actions : [];
    return actions.filter((action) => !hideToolAdminAction(tool, action));
}

function isExecToolFamily(tool) {
    return String(tool?.tool_id || "").trim().toLowerCase() === "exec_runtime";
}

function execToolExecutionMode(tool) {
    const policyMode = String(tool?.exec_runtime_policy?.mode || "").trim().toLowerCase();
    if (policyMode) return policyMode;
    const metadataMode = String(tool?.metadata?.execution_mode || "").trim().toLowerCase();
    if (metadataMode) return metadataMode;
    return "governed";
}

function filterTools() {
    const q = String(U.toolSearch.value || "").trim().toLowerCase();
    return S.tools.filter((tool) => {
        if (q && !`${tool.tool_id} ${tool.display_name} ${tool.source_path}`.toLowerCase().includes(q)) return false;
        const statusFilter = String(U.toolStatus.value || "all").trim().toLowerCase();
        const statusKey = toolRepairRequired(tool)
            ? "repair_required"
            : (tool.available === false ? "unavailable" : (tool.enabled ? "enabled" : "disabled"));
        if (statusFilter !== "all" && statusKey !== statusFilter) return false;
        const displayActions = toolActionsForDisplay(tool);
        if (U.toolRisk.value !== "all" && !displayActions.some((a) => a.risk_level === U.toolRisk.value)) return false;
        return true;
    });
}

function renderTools() {
    U.toolList.innerHTML = "";
    const meta = paginateResources(filterTools(), S.toolPage, S.toolPageSize);
    S.toolPage = meta.currentPage;
    S.toolPageSize = meta.pageSize;
    syncResourcePagination("tool", meta);
    if (!meta.total) return void (U.toolList.innerHTML = '<div class="empty-state">没有匹配的 Tool。</div>');
    meta.items.forEach((tool) => {
        const el = document.createElement("button");
        el.type = "button";
        el.className = `resource-list-item${S.selectedTool?.tool_id === tool.tool_id ? " selected" : ""}`;
        const desc = (tool.description || "").trim();
        const subtitle = desc ? (desc.length > 50 ? desc.slice(0, 47) + "..." : desc) : tool.tool_id;
        el.innerHTML = `
            <div class="resource-list-title">${esc(tool.display_name)}</div>
            <div class="resource-list-subtitle">${esc(subtitle)}</div>
            <div class="resource-list-meta">
                <span class="meta-tag status-${resourceAvailabilityStatus(tool)}">${esc(displayEnabledLabel(tool.enabled, tool.available, toolRepairRequired(tool)))}</span>
                ${tool.is_core ? '<span class="meta-tag">核心工具</span>' : ''}
                <span class="meta-tag tool-actions">${toolActionsForDisplay(tool).length} 个 action</span>
            </div>`;
        el.addEventListener("click", () => openTool(tool.tool_id));
        U.toolList.appendChild(el);
    });
}

function communicationToastKind(status) {
    return ({ success: "success", warning: "warn", error: "error", disabled: "info" }[String(status || "").toLowerCase()] || "info");
}

function communicationBridgeError(runtime) {
    if (runtime?.connected) return "";
    return String(runtime?.last_error || "").trim();
}

function communicationRuntimeStatusKey(item) {
    const runtime = item?.runtime || {};
    if (!item?.enabled) return "blocked";
    if (runtime.connected) return "success";
    if (communicationBridgeError(runtime)) return "failed";
    if (runtime.running) return "running";
    return "pending";
}

function communicationRuntimeLabel(item) {
    const runtime = item?.runtime || {};
    if (!item?.enabled) return "已禁用";
    if (runtime.connected) return "已连接";
    if (communicationBridgeError(runtime)) return "桥接异常";
    if (runtime.running) return "桥接运行中";
    if (runtime.status_exists) return "桥接连接中";
    return "待测试";
}

function communicationBridgeConnectionText(runtime) {
    const error = communicationBridgeError(runtime);
    if (runtime?.connected) return "内部控制链路已连接";
    if (error) return `桥接异常：${error}`;
    if (runtime?.running) return "桥接宿主运行中，等待连接";
    return "桥接宿主未运行或尚未连通";
}

function normalizeCommunicationJsonText(text) {
    const source = String(text || "").trim();
    if (!source) return "{}";
    try {
        return JSON.stringify(parseCommunicationJsonText(source), null, 2);
    } catch {
        return source;
    }
}

function stripJsonComments(text) {
    const source = String(text || "");
    let result = "";
    let quote = "";
    let escaped = false;
    let inLineComment = false;
    let inBlockComment = false;
    for (let index = 0; index < source.length; index += 1) {
        const char = source[index];
        const next = source[index + 1];
        if (inLineComment) {
            if (char === "\n" || char === "\r") {
                inLineComment = false;
                result += char;
            }
            continue;
        }
        if (inBlockComment) {
            if (char === "*" && next === "/") {
                inBlockComment = false;
                index += 1;
                continue;
            }
            if (char === "\n" || char === "\r") result += char;
            continue;
        }
        if (quote) {
            result += char;
            if (escaped) {
                escaped = false;
                continue;
            }
            if (char === "\\") {
                escaped = true;
                continue;
            }
            if (char === quote) {
                quote = "";
            }
            continue;
        }
        if (char === '"' || char === "'") {
            quote = char;
            result += char;
            continue;
        }
        if (char === "/" && next === "/") {
            inLineComment = true;
            index += 1;
            continue;
        }
        if (char === "/" && next === "*") {
            inBlockComment = true;
            index += 1;
            continue;
        }
        result += char;
    }
    return result;
}

function removeJsonTrailingCommas(text) {
    const source = String(text || "");
    let result = "";
    let quote = "";
    let escaped = false;
    for (let index = 0; index < source.length; index += 1) {
        const char = source[index];
        if (quote) {
            result += char;
            if (escaped) {
                escaped = false;
                continue;
            }
            if (char === "\\") {
                escaped = true;
                continue;
            }
            if (char === quote) {
                quote = "";
            }
            continue;
        }
        if (char === '"' || char === "'") {
            quote = char;
            result += char;
            continue;
        }
        if (char === ",") {
            let lookahead = index + 1;
            while (lookahead < source.length && /\s/.test(source[lookahead])) {
                lookahead += 1;
            }
            if (source[lookahead] === "}" || source[lookahead] === "]") {
                continue;
            }
        }
        result += char;
    }
    return result;
}

function sanitizeCommunicationJsonText(text) {
    return removeJsonTrailingCommas(stripJsonComments(text));
}

function parseCommunicationJsonText(text) {
    const source = sanitizeCommunicationJsonText(text).trim();
    if (!source) return {};
    return JSON.parse(source);
}

function buildCommunicationJsonTemplate(text) {
    return String(text || "").trim();
}

const COMMUNICATION_JSON_TEMPLATES = {
    qqbot: buildCommunicationJsonTemplate(`
{
  // 必填：QQ 机器人 AppId
  "appId": "your-qq-app-id",
  // 必填：QQ 机器人 Client Secret
  "clientSecret": "your-qq-client-secret",

  // 可选：是否启用 Markdown 发送，默认 true
  // "markdownSupport": true,

  // 可选：多账号时指定默认账号 ID
  // "defaultAccount": "default",

  // 可选：多账号配置；单账号时可不填
  // "accounts": {
  //   "default": {
  //     "appId": "your-qq-app-id",
  //     "clientSecret": "your-qq-client-secret"
  //   }
  // }
}
`),
    dingtalk: buildCommunicationJsonTemplate(`
{
  // 必填：钉钉应用 AppKey
  "clientId": "your-dingtalk-client-id",
  // 必填：钉钉应用 AppSecret
  "clientSecret": "your-dingtalk-client-secret",

  // 可选：启用 AI Card 回复
  // "enableAICard": true,

  // 可选：Gateway 鉴权 token；未配置时走全局 gateway.auth.token
  // "gatewayToken": "your-dingtalk-gateway-token",

  // 可选：多账号时指定默认账号 ID
  // "defaultAccount": "default",

  // 可选：多账号配置；单账号时可不填
  // "accounts": {
  //   "default": {
  //     "clientId": "your-dingtalk-client-id",
  //     "clientSecret": "your-dingtalk-client-secret"
  //   }
  // }
}
`),
    wecom: buildCommunicationJsonTemplate(`
{
  // 必填（ws 模式，默认）：企业微信机器人 BotId
  "botId": "your-wecom-bot-id",
  // 必填（ws 模式，默认）：企业微信机器人 Secret
  "secret": "your-wecom-bot-secret",

  // 可选：连接模式。默认 "ws"；改成 "webhook" 后需填写 token + encodingAESKey
  // "mode": "ws",

  // 可选（webhook 模式必填）：回调校验 Token
  // "token": "your-wecom-token",
  // 可选（webhook 模式必填）：回调加解密 EncodingAESKey
  // "encodingAESKey": "your-wecom-encoding-aes-key",
  // 可选（webhook 模式）：回调路径，默认 "/wecom"
  // "webhookPath": "/wecom",

  // 可选：多账号时指定默认账号 ID
  // "defaultAccount": "default",

  // 可选：多账号配置；单账号时可不填
  // "accounts": {
  //   "default": {
  //     "botId": "your-wecom-bot-id",
  //     "secret": "your-wecom-bot-secret"
  //   }
  // }
}
`),
    "wecom-app": buildCommunicationJsonTemplate(`
{
  // 必填：企业微信应用回调 Token
  "token": "your-wecom-app-token",
  // 必填：企业微信应用回调 EncodingAESKey
  "encodingAESKey": "your-wecom-app-encoding-aes-key",

  // 可选：回调路径，默认 "/wecom-app"
  // "webhookPath": "/wecom-app",

  // 可选：如需主动发消息，再填写下面 3 项
  // "corpId": "your-wecom-corp-id",
  // "corpSecret": "your-wecom-corp-secret",
  // "agentId": 1000001,

  // 可选：多账号时指定默认账号 ID
  // "defaultAccount": "default",

  // 可选：多账号配置；单账号时可不填
  // "accounts": {
  //   "default": {
  //     "token": "your-wecom-app-token",
  //     "encodingAESKey": "your-wecom-app-encoding-aes-key",
  //     "corpId": "your-wecom-corp-id",
  //     "corpSecret": "your-wecom-corp-secret",
  //     "agentId": 1000001
  //   }
  // }
}
`),
    "wecom-kf": buildCommunicationJsonTemplate(`
{
  "token": "your-wecom-kf-token",
  "encodingAESKey": "your-wecom-kf-encoding-aes-key",
  "corpId": "your-wecom-corp-id",
  "corpSecret": "your-wecom-kf-corp-secret",
  "openKfId": "your-open-kf-id",
  "webhookPath": "/wecom-kf"
}
`),
    "wechat-mp": buildCommunicationJsonTemplate(`
{
  "appId": "your-wechat-mp-app-id",
  "appSecret": "your-wechat-mp-app-secret",
  "token": "your-wechat-mp-token",
  "encodingAESKey": "your-wechat-mp-encoding-aes-key",
  "webhookPath": "/wechat-mp",
  "messageMode": "safe",
  "replyMode": "passive"
}
`),
    "feishu-china": buildCommunicationJsonTemplate(`
{
  // 必填：飞书应用 App ID
  "appId": "your-feishu-app-id",
  // 必填：飞书应用 App Secret
  "appSecret": "your-feishu-app-secret",

  // 可选：连接模式，当前仅支持 websocket
  // "connectionMode": "websocket",

  // 可选：Markdown 消息按卡片发送，默认 true
  // "sendMarkdownAsCard": true,

  // 可选：仅发送最终回复，关闭流式中间消息
  // "replyFinalOnly": false
}
`),
};

function getCommunicationTemplate(channelId, item = null) {
    const remoteTemplate = item && item.template_json && typeof item.template_json === "object"
        ? JSON.stringify(item.template_json, null, 2)
        : "";
    if (remoteTemplate.trim()) return remoteTemplate;
    const key = String(channelId || "").trim();
    return String(COMMUNICATION_JSON_TEMPLATES[key] || "{}").trim();
}

function hasCommunicationConfig(item) {
    const config = item && typeof item.config === "object" ? item.config : null;
    return !!(config && !Array.isArray(config) && Object.keys(config).length);
}

function initialCommunicationDraftText(item) {
    if (hasCommunicationConfig(item)) {
        return String(item.json_text || JSON.stringify(item.config || {}, null, 2));
    }
    return getCommunicationTemplate(item?.id, item);
}

function syncCommunicationDirtyState() {
    const next =
        S.communicationDraftEnabled !== S.communicationBaselineEnabled ||
        normalizeCommunicationJsonText(S.communicationDraftText) !== normalizeCommunicationJsonText(S.communicationBaselineText);
    setCommunicationDirty(next);
}

function renderCommunicationBridgeSummary() {
    if (!U.communicationBridgeSummary) return;
    const bridge = S.communicationBridge;
    if (!bridge) {
        U.communicationBridgeSummary.innerHTML = '<div class="empty-state">正在加载桥接状态...</div>';
        return;
    }
    const bridgeError = communicationBridgeError(bridge);
    const statusKey = bridge.connected ? "success" : bridgeError ? "failed" : bridge.running ? "running" : bridge.enabled ? "pending" : "blocked";
    const statusLabel = bridge.connected ? "已连接" : bridgeError ? "异常" : bridge.running ? "运行中" : bridge.enabled ? "连接中" : "未启用";
    const statusText = bridgeError ? `${statusLabel} · ${bridgeError}` : statusLabel;
    U.communicationBridgeSummary.innerHTML = `
        <div class="resource-list-item communication-summary-card">
            <div class="panel-header">
                <div>
                    <div class="resource-list-title">中国通信子系统</div>
                    <div class="resource-list-subtitle">统一负责渠道 webhook / ws / 回调通信</div>
                </div>
                <span class="meta-tag status-${esc(statusKey)}">${esc(statusText)}</span>
            </div>
            <div class="resource-list-meta">
                <span class="meta-tag">Port: ${esc(bridge.public_port)} / ${esc(bridge.control_port)}</span>
                <span class="meta-tag status-${bridge.dist_exists ? 'enabled' : 'disabled'}">${bridge.dist_exists ? "Host 已构建" : "Host 未构建"}</span>
                <span class="meta-tag status-${bridge.node_found ? 'enabled' : 'disabled'}">${bridge.node_found ? "Node 已就绪" : "Node 未找到"}</span>
            </div>
        </div>
    `;
}

function renderCommunications() {
    if (!U.communicationList) return;
    U.communicationList.innerHTML = "";
    const items = Array.isArray(S.communications) ? S.communications : [];
    if (!items.length) {
        U.communicationList.innerHTML = '<div class="empty-state">暂无可用通信方式。</div>';
        return;
    }
    items.forEach((item) => {
        const selected = S.selectedCommunication?.id === item.id;
        const el = document.createElement("button");
        el.type = "button";
        el.className = `resource-list-item communication-card${selected ? " selected" : ""}`;
        el.innerHTML = `
            <div class="panel-header">
                <div class="resource-list-title">${esc(item.label)}</div>
                <span class="meta-tag status-${esc(communicationRuntimeStatusKey(item))}">${esc(communicationRuntimeLabel(item))}</span>
            </div>
            <div class="resource-list-subtitle">${esc(item.description || item.config_path || item.id)}</div>
            <div class="resource-list-meta">
                <span class="meta-tag status-${item.enabled ? 'enabled' : 'disabled'}">${esc(displayEnabledLabel(item.enabled))}</span>
                <span class="meta-tag tool-actions">${esc(item.account_count || 0)} 个账号</span>
                <span class="meta-tag tool-actions" style="border-style: solid; opacity: 0.6;">${esc(item.config_path || "")}</span>
            </div>
        `;
        el.addEventListener("click", () => void openCommunication(item.id));
        U.communicationList.appendChild(el);
    });
}

function renderCommunicationDetail() {
    if (!S.selectedCommunication) {
        U.communicationEmpty.style.display = "block";
        U.communicationDetail.innerHTML = "";
        setDrawerOpen(U.communicationBackdrop, U.communicationDrawer, false);
        renderCommunicationActions();
        return;
    }
    U.communicationEmpty.style.display = "none";
    setDrawerOpen(U.communicationBackdrop, U.communicationDrawer, true);
    const item = S.selectedCommunication;
    const runtime = item.runtime || {};
    const toggleLabel = S.communicationDraftEnabled ? "已启用" : "已禁用";
    const statusKey = communicationRuntimeStatusKey({ ...item, enabled: S.communicationDraftEnabled });
    const statusLabel = communicationRuntimeLabel({ ...item, enabled: S.communicationDraftEnabled });
    U.communicationDetail.innerHTML = `
        <article class="resource-detail-card detail-modal-shell">
            <div class="detail-modal-header">
                <div class="detail-modal-title">
                    <h2 id="communication-detail-title">${esc(item.label)}</h2>
                    <p class="subtitle">${esc(item.config_path || item.id)}</p>
                </div>
                <div class="detail-modal-actions">
                    <button type="button" class="toolbar-btn ghost" id="communication-close-btn" data-modal-close>关闭</button>
                    <button type="button" class="toolbar-btn success" id="communication-save-btn">保存</button>
                </div>
            </div>
            <div class="detail-modal-body">
                <div class="communication-detail-meta">
                    <div class="resource-copy-block">
                        <strong>渠道状态</strong><br>
                        <span class="status-badge" data-status="${esc(statusKey)}">${esc(toggleLabel)} · ${esc(statusLabel)}</span>
                    </div>
                    <div class="resource-copy-block">
                        <strong>桥接状态</strong><br>
                        ${esc(communicationBridgeConnectionText(runtime))}
                    </div>
                </div>
                <label class="communication-toggle">
                    <input id="communication-enabled-toggle" type="checkbox" ${S.communicationDraftEnabled ? "checked" : ""}>
                    <span class="communication-toggle-track" aria-hidden="true"></span>
                    <span class="communication-toggle-copy">
                        <strong>启用该通信方式</strong>
                        <span>${S.communicationDraftEnabled ? "保存后将参与消息收发" : "保存后将停止该通信方式"}</span>
                    </span>
                </label>
                <div class="resource-draft-hint${S.communicationDirty ? " is-dirty" : ""}" ${S.communicationDirty ? "" : "hidden"}></div>
                <div class="resource-section">
                    <div class="communication-section-head">
                        <h3>JSON 配置</h3>
                        <button type="button" class="toolbar-btn ghost small" id="communication-load-template-btn">加载模板</button>
                    </div>
                    <textarea id="communication-json-editor" rows="18" class="resource-editor communication-json-editor">${esc(S.communicationDraftText)}</textarea>
                </div>
            </div>
        </article>
    `;
    U.communicationDetail.querySelector("#communication-close-btn")?.addEventListener("click", clearCommunicationSelection);
    U.communicationDetail.querySelector("#communication-save-btn")?.addEventListener("click", () => void saveCommunication());
    U.communicationDetail.querySelector("#communication-enabled-toggle")?.addEventListener("change", (e) => {
        S.communicationDraftEnabled = !!e.target.checked;
        syncCommunicationDirtyState();
        renderCommunicationDetail();
    });
    U.communicationDetail.querySelector("#communication-json-editor")?.addEventListener("input", (e) => {
        S.communicationDraftText = String(e.target.value || "");
        syncCommunicationDirtyState();
    });
    U.communicationDetail.querySelector("#communication-load-template-btn")?.addEventListener("click", () => {
        const template = getCommunicationTemplate(item.id, item);
        S.communicationDraftText = template;
        const editor = U.communicationDetail.querySelector("#communication-json-editor");
        if (editor) editor.value = S.communicationDraftText;
        syncCommunicationDirtyState();
        renderCommunicationActions();
        showToast({
            title: "模板已加载",
            text: `${item.label} 的预设模板已填入 JSON 表单，确认后点击保存即可生效。`,
            kind: "info",
            durationMs: 2400,
        });
    });
    renderCommunicationActions();
}

async function loadCommunications({ renderDetail = true } = {}) {
    if (U.communicationList) U.communicationList.innerHTML = '<div class="empty-state">正在加载通信方式...</div>';
    const selectedId = S.selectedCommunication?.id || "";
    try {
        const payload = await ApiClient.getChinaChannels();
        S.communicationBridge = payload.bridge || null;
        S.communications = Array.isArray(payload.items) ? payload.items : [];
        if (selectedId) {
            const next = S.communications.find((item) => item.id === selectedId);
            if (next && !renderDetail) S.selectedCommunication = { ...S.selectedCommunication, ...next };
            else if (!next) clearCommunicationSelection();
        }
        renderCommunicationBridgeSummary();
        renderCommunications();
        if (renderDetail) renderCommunicationDetail();
    } catch (e) {
        if (U.communicationList) U.communicationList.innerHTML = `<div class="empty-state error">加载通信方式失败：${esc(e.message)}</div>`;
        if (U.communicationBridgeSummary) U.communicationBridgeSummary.innerHTML = `<div class="empty-state error">桥接状态获取失败：${esc(e.message)}</div>`;
        showToast({ title: "加载失败", text: e.message || "Unknown error", kind: "error" });
    } finally {
        renderCommunicationActions();
    }
}

async function openCommunication(channelId, quiet = false) {
    if (!quiet) {
        setDrawerOpen(U.communicationBackdrop, U.communicationDrawer, true);
        U.communicationEmpty.style.display = "none";
        U.communicationDetail.innerHTML = '<div class="empty-state">正在加载通信配置...</div>';
    }
    try {
        const item = await ApiClient.getChinaChannel(channelId);
        S.selectedCommunication = item;
        S.communicationBaselineEnabled = !!item.enabled;
        S.communicationDraftEnabled = !!item.enabled;
        S.communicationBaselineText = initialCommunicationDraftText(item);
        S.communicationDraftText = S.communicationBaselineText;
        S.communicationDirty = false;
        renderCommunications();
        renderCommunicationDetail();
    } catch (e) {
        U.communicationDetail.innerHTML = `<div class="empty-state error">加载通信配置失败：${esc(e.message)}</div>`;
        showToast({ title: "加载失败", text: e.message || "Unknown error", kind: "error" });
    } finally {
        renderCommunicationActions();
    }
}

async function refreshCommunications() {
    const selectedId = S.selectedCommunication?.id || "";
    S.communicationBusy = true;
    renderCommunicationActions();
    try {
        await loadCommunications({ renderDetail: false });
        if (selectedId && S.communications.some((item) => item.id === selectedId)) {
            await openCommunication(selectedId, true);
        }
    } finally {
        S.communicationBusy = false;
        renderCommunicationActions();
    }
}

async function saveCommunication() {
    if (S.communicationBusy) return;
    const item = S.selectedCommunication;
    const channelId = String(item?.id || "").trim();
    if (!channelId || !item) {
        showToast({ title: "保存失败", text: "请先选择一个通信方式。", kind: "error" });
        return;
    }
    if (!S.communicationDirty) {
        showToast({ title: "没有待保存修改", text: "当前通信配置没有未保存改动。", kind: "info", durationMs: 1800 });
        return;
    }
    let configPayload = {};
    try {
        configPayload = parseCommunicationJsonText(S.communicationDraftText || "{}");
        if (!configPayload || typeof configPayload !== "object" || Array.isArray(configPayload)) {
            throw new Error("JSON 配置必须是对象");
        }
    } catch (e) {
        showToast({ title: "JSON 无法保存", text: e.message || "配置格式错误", kind: "error", durationMs: 2800 });
        return;
    }
    S.communicationBusy = true;
    renderCommunicationActions();
    showToast({ title: "保存中", text: `正在保存 ${item.label} 配置...`, kind: "info", persistent: true });
    try {
        const saveResult = await ApiClient.updateChinaChannel(channelId, {
            enabled: S.communicationDraftEnabled,
            config: configPayload,
        });
        await loadCommunications({ renderDetail: false });
        await openCommunication(channelId, true);
        const result = saveResult?.probe_result || saveResult?.probeResult || {};
        const message = [result.message, ...(Array.isArray(result.details) ? result.details : [])].filter(Boolean).join("；");
        showToast({
            title: result.title || "保存成功",
            text: message || `${item.label} 配置已更新`,
            kind: communicationToastKind(result.status),
            durationMs: result.status === "error" ? 3200 : 2600,
        });
    } catch (e) {
        const probe = e?.data?.probe || null;
        const message = probe
            ? [probe.message, ...(Array.isArray(probe.details) ? probe.details : [])].filter(Boolean).join("；")
            : (e.message || "Unknown error");
        showToast({
            title: probe?.title || "保存失败",
            text: message,
            kind: probe ? communicationToastKind(probe.status) : "error",
            durationMs: 3200,
        });
    } finally {
        S.communicationBusy = false;
        renderCommunicationActions();
    }
}

function cancelSkillAutosave() {
    if (S.skillAutosaveTimerId) {
        window.clearTimeout(S.skillAutosaveTimerId);
        S.skillAutosaveTimerId = null;
    }
}

function queueSkillAutosave(delayMs = 900) {
    cancelSkillAutosave();
    if (!S.selectedSkill || !S.skillDirty) return;
    S.skillAutosaveTimerId = window.setTimeout(() => {
        S.skillAutosaveTimerId = null;
        if (!S.selectedSkill || !S.skillDirty) return;
        void saveSkill({ showProgressToast: false, showSuccessToast: false, reopenDetail: false, silentIfPristine: true, autosave: true });
    }, Math.max(0, Number(delayMs) || 0));
}

function cancelToolAutosave() {
    if (S.toolAutosaveTimerId) {
        window.clearTimeout(S.toolAutosaveTimerId);
        S.toolAutosaveTimerId = null;
    }
}

function queueToolAutosave(delayMs = 300) {
    cancelToolAutosave();
    if (!S.selectedTool || !S.toolDirty) return;
    S.toolAutosaveTimerId = window.setTimeout(() => {
        S.toolAutosaveTimerId = null;
        if (!S.selectedTool || !S.toolDirty) return;
        void saveTool({ showProgressToast: false, showSuccessToast: false, reopenDetail: false, silentIfPristine: true, autosave: true });
    }, Math.max(0, Number(delayMs) || 0));
}

function renderSkillDetail() {
    if (!S.selectedSkill) {
        U.skillEmpty.style.display = "block";
        U.skillDetail.innerHTML = "";
        setDrawerOpen(U.skillBackdrop, U.skillDrawer, false);
        renderSkillActions();
        return;
    }
    U.skillEmpty.style.display = "none";
    setDrawerOpen(U.skillBackdrop, U.skillDrawer, true);
    const selectedFileKey = String(S.selectedSkillFile || "").trim();
    const fileLoaded = selectedFileKey
        ? Object.prototype.hasOwnProperty.call(S.skillContents, selectedFileKey)
        : false;
    const roles = ["ceo", "execution", "inspection"];
    const allowedRoles = Array.isArray(S.selectedSkill.allowed_roles) ? S.selectedSkill.allowed_roles : [];
    const editorValue = esc(fileLoaded ? (S.skillContents[selectedFileKey] || "") : "Loading file...");
    const availabilityState = resourceAvailabilityStatus(S.selectedSkill);
    const unavailableReasons = resourceAvailabilityReasons(S.selectedSkill);
    const fileTabs = S.skillFiles.length
        ? S.skillFiles.map((file) => `<button type="button" class="toolbar-btn ghost skill-file ${S.selectedSkillFile === file.file_key ? "active" : ""}" data-file="${esc(file.file_key)}">${esc(file.file_key)}</button>`).join("")
        : '<span class="resource-empty-copy">暂无可编辑文件</span>';
    U.skillDetail.innerHTML = `
        <article class="resource-detail-card detail-modal-shell">
            <div class="detail-modal-header">
                <div class="detail-modal-title">
                    <h2 id="skill-detail-title">${esc(S.selectedSkill.display_name)}</h2>
                    <p class="subtitle">${esc(S.selectedSkill.skill_id)}</p>
                </div>
                <div class="detail-modal-actions">
                    <button type="button" class="toolbar-btn ghost" id="skill-modal-close" data-modal-close>关闭</button>
                </div>
            </div>
            <div class="detail-modal-body">
                <div class="resource-status-row" style="margin-bottom: var(--space-4);">
                    <span class="meta-tag status-${availabilityState}">${esc(displayEnabledLabel(S.selectedSkill.enabled, S.selectedSkill.available))}</span>
                    ${S.selectedSkill.enabled
                        ? `<button type="button" class="toolbar-btn danger" id="skill-disable-btn">禁用技能</button>`
                        : `<button type="button" class="toolbar-btn success" id="skill-enable-btn">启用技能</button>`}
                    <button type="button" class="toolbar-btn danger" id="skill-delete-btn">删除</button>
                </div>
                ${S.selectedSkill.available === false ? `
                    <div class="resource-warning-banner" role="status" aria-live="polite">
                        <div class="resource-warning-title">当前 Skill 不可用</div>
                        <ul class="resource-warning-list">
                            ${unavailableReasons.map((reason) => `<li>${esc(reason)}</li>`).join("")}
                        </ul>
                    </div>
                ` : ""}
                <div class="resource-draft-hint${S.skillDirty ? " is-dirty" : ""}" ${S.skillDirty ? "" : "hidden"}></div>
                <div class="resource-section">
                    <h3>角色可见性</h3>
                    <div class="resource-filter-row">
                        ${roles.map((role) => `
                            <label class="role-toggle ${allowedRoles.includes(role) ? "checked" : ""}">
                                <input type="checkbox" class="skill-role" data-role="${role}" ${allowedRoles.includes(role) ? "checked" : ""}>
                                <span>${esc(displayRoleLabel(role))}</span>
                            </label>
                        `).join("")}
                    </div>
                </div>
                <div class="resource-section">
                    <h3>文件内容</h3>
                    <div class="resource-filter-row">${fileTabs}</div>
                    <textarea id="skill-editor" rows="18" class="resource-editor"${selectedFileKey && !fileLoaded ? " disabled" : ""}>${editorValue}</textarea>
                </div>
            </div>
        </article>`;
    dockResourceStatusBadge(U.skillDetail);
    U.skillDetail.querySelector("#skill-modal-close")?.addEventListener("click", clearSkillSelection);
    U.skillDetail.querySelector("#skill-enable-btn")?.addEventListener("click", () => {
        S.selectedSkill.enabled = true;
        setSkillDirty(true);
        renderSkillDetail();
        queueSkillAutosave(120);
    });
    U.skillDetail.querySelector("#skill-disable-btn")?.addEventListener("click", () => {
        S.selectedSkill.enabled = false;
        setSkillDirty(true);
        renderSkillDetail();
        queueSkillAutosave(120);
    });
    U.skillDetail.querySelector("#skill-delete-btn")?.addEventListener("click", () => void requestDeleteSkill());
    U.skillDetail.querySelectorAll(".skill-role").forEach((checkbox) => checkbox.addEventListener("change", (e) => {
        const nextRoles = new Set(allowedRoles);
        if (e.target.checked) nextRoles.add(e.target.dataset.role);
        else nextRoles.delete(e.target.dataset.role);
        S.selectedSkill.allowed_roles = [...nextRoles];
        setSkillDirty(true);
        renderSkillDetail();
        queueSkillAutosave(120);
    }));
    U.skillDetail.querySelectorAll(".skill-file").forEach((button) => button.addEventListener("click", () => {
        const nextFileKey = String(button.dataset.file || "").trim();
        if (!nextFileKey || nextFileKey === String(S.selectedSkillFile || "").trim()) return;
        const editor = document.getElementById("skill-editor");
        const currentFileKey = String(S.selectedSkillFile || "").trim();
        if (editor && currentFileKey && Object.prototype.hasOwnProperty.call(S.skillContents, currentFileKey)) {
            S.skillContents[currentFileKey] = editor.value;
        }
        S.selectedSkillFile = nextFileKey;
        renderSkillDetail();
        void ensureSkillFileLoaded(S.selectedSkill?.skill_id, nextFileKey);
    }));
    U.skillDetail.querySelector("#skill-editor")?.addEventListener("input", (e) => {
        if (!S.selectedSkillFile) return;
        S.skillContents[S.selectedSkillFile] = e.target.value;
        setSkillDirty(true);
        queueSkillAutosave(1200);
    });
    renderSkillActions();
}

async function ensureSkillFileLoaded(skillId, fileKey) {
    const normalizedSkillId = String(skillId || S.selectedSkill?.skill_id || "").trim();
    const normalizedFileKey = String(fileKey || "").trim();
    const loadKey = `${normalizedSkillId}::${normalizedFileKey}`;
    if (!normalizedSkillId || !normalizedFileKey) return "";
    if (Object.prototype.hasOwnProperty.call(S.skillContents, normalizedFileKey)) {
        return S.skillContents[normalizedFileKey] || "";
    }
    if (S.skillFileLoads[loadKey]) {
        return S.skillFileLoads[loadKey];
    }
    S.skillFileLoads[loadKey] = ApiClient.getSkillFile(normalizedSkillId, normalizedFileKey)
        .then((data) => {
            const content = String(data?.content || "");
            if (String(S.selectedSkill?.skill_id || "").trim() === normalizedSkillId) {
                S.skillContents[normalizedFileKey] = content;
                if (String(S.selectedSkillFile || "").trim() === normalizedFileKey) {
                    renderSkillDetail();
                }
            }
            return content;
        })
        .catch((error) => {
            if (String(S.selectedSkill?.skill_id || "").trim() === normalizedSkillId) {
                S.skillContents[normalizedFileKey] = "";
                if (String(S.selectedSkillFile || "").trim() === normalizedFileKey) {
                    renderSkillDetail();
                }
                showToast({ title: "Skill file load failed", text: error.message || normalizedFileKey, kind: "error" });
            }
            return "";
        })
        .finally(() => {
            if (S.skillFileLoads[loadKey]) {
                delete S.skillFileLoads[loadKey];
            }
        });
    return S.skillFileLoads[loadKey];
}

async function loadSkills({ renderDetail = true } = {}) {
    if (!(S.skills || []).length) {
        U.skillList.innerHTML = '<div class="empty-state">Loading skills...</div>';
    }
    const selectedId = S.selectedSkill?.skill_id || "";
    try {
        S.skills = await ApiClient.getSkills(0, 300);
        if (selectedId) {
            const next = S.skills.find((skill) => skill.skill_id === selectedId);
            if (next) {
                S.selectedSkill = next;
                ensureSkillPageForItem(selectedId);
            }
            else clearSkillSelection();
        }
        renderSkills();
        if (renderDetail) renderSkillDetail();
    } catch (e) {
        if (isAbortLike(e)) return;
        if (!(S.skills || []).length) {
            U.skillList.innerHTML = `<div class="empty-state error">Failed to load skills: ${esc(e.message)}</div>`;
        }
        addNotice({ kind: "resource_failed", title: "Skill load failed", text: e.message || "Unknown error" });
    } finally {
        renderSkillActions();
    }
}

async function openSkill(skillId, quiet = false) {
    if (!quiet) {
        setDrawerOpen(U.skillBackdrop, U.skillDrawer, true);
        U.skillEmpty.style.display = "none";
        U.skillDetail.innerHTML = '<div class="empty-state">Loading skill details...</div>';
    }
    try {
        const [skill, files] = await Promise.all([ApiClient.getSkill(skillId), ApiClient.getSkillFiles(skillId)]);
        S.selectedSkill = skill;
        ensureSkillPageForItem(skillId);
        S.skillFiles = files;
        S.selectedSkillFile = files[0]?.file_key || "";
        S.skillContents = {};
        S.skillFileLoads = {};
        S.skillDirty = false;
        renderSkills();
        renderSkillDetail();
        if (S.selectedSkillFile) {
            await ensureSkillFileLoaded(skillId, S.selectedSkillFile);
        }
    } catch (e) {
        U.skillDetail.innerHTML = `<div class="empty-state error">Failed to load skill details: ${esc(e.message)}</div>`;
        addNotice({ kind: "resource_failed", title: "Skill detail failed", text: e.message || "Unknown error" });
    } finally {
        renderSkillActions();
    }
}

async function saveSkill({
    showProgressToast = true,
    showSuccessToast = true,
    reopenDetail = true,
    silentIfPristine = false,
    autosave = false,
} = {}) {
    if (S.skillBusy) {
        if (autosave) S.skillAutosavePending = true;
        return;
    }
    const selectedId = String(S.selectedSkill?.skill_id || "").trim();
    const displayName = String(S.selectedSkill?.display_name || selectedId || "Skill").trim();
    const enabled = !!S.selectedSkill?.enabled;
    const allowedRoles = Array.isArray(S.selectedSkill?.allowed_roles) ? [...S.selectedSkill.allowed_roles] : [];
    if (!selectedId || !S.selectedSkill) {
        addNotice({ kind: "resource_failed", title: "No skill selected", text: "Select a skill before saving." });
        showToast({ title: "保存失败", text: "未选择 Skill", kind: "error" });
        return;
    }
    if (!S.skillDirty) {
        if (!silentIfPristine) {
            showToast({ title: "No pending changes", text: "This skill has no unsaved changes.", kind: "info", durationMs: 1800 });
        }
        return;
    }
    cancelSkillAutosave();
    S.skillBusy = true;
    renderSkillActions();
    if (showProgressToast) {
        showToast({ title: "保存中", text: "正在保存 Skill，请稍候…", kind: "info", persistent: true });
    }
    try {
        const editor = document.getElementById("skill-editor");
        if (editor && S.selectedSkillFile) S.skillContents[S.selectedSkillFile] = editor.value;
        for (const [fileKey, content] of Object.entries(S.skillContents)) {
            await ApiClient.saveSkillFile(selectedId, fileKey, content);
        }
        await ApiClient.updateSkillPolicy(selectedId, {
            enabled,
            allowed_roles: allowedRoles,
        });
        await ApiClient.reloadResources();
        await loadSkills({ renderDetail: false });
        if (reopenDetail) {
            await openSkill(selectedId, true);
        }
        setSkillDirty(false);
        if (showSuccessToast) {
            addNotice({ kind: "resource_saved", title: "Skill saved", text: displayName || selectedId });
            showToast({ title: "保存成功", text: "Skill 配置已保存", kind: "success", durationMs: 2200 });
        }
    } catch (e) {
        addNotice({ kind: "resource_failed", title: "Skill save failed", text: e.message || "Unknown error" });
        showToast({ title: "保存失败", text: e.message || "Unknown error", kind: "error", durationMs: 2600 });
    } finally {
        S.skillBusy = false;
        const queuedAutosave = !!S.skillAutosavePending;
        S.skillAutosavePending = false;
        renderSkillActions();
        if (queuedAutosave && S.skillDirty) queueSkillAutosave(300);
    }
}

async function requestDeleteSkill() {
    const selectedId = String(S.selectedSkill?.skill_id || "").trim();
    const displayName = String(S.selectedSkill?.display_name || selectedId || "Skill").trim();
    if (!selectedId || S.skillBusy) return;
    const detail = S.skillDirty
        ? "确认删除该 Skill 并丢弃未保存的修改？相关文件和 catalog 条目也会一起移除。"
        : "确认删除该 Skill？相关文件和 catalog 条目也会一起移除。";
    openConfirm({
        title: "删除 Skill",
        text: detail,
        confirmLabel: "删除",
        confirmKind: "danger",
        returnFocus: U.skillRefresh,
        onConfirm: () => performDeleteSkill(selectedId, displayName),
    });
}

async function performDeleteSkill(skillId, displayName) {
    if (!skillId) return;
    S.skillBusy = true;
    renderSkillActions();
    showToast({ title: "正在删除", text: `正在移除 ${displayName || skillId}...`, kind: "info", persistent: true });
    try {
        await ApiClient.deleteSkill(skillId);
        clearSkillSelection();
        await loadSkills({ renderDetail: false });
        addNotice({ kind: "resource_saved", title: "Skill 已删除", text: displayName || skillId });
        showToast({ title: "已删除", text: `${displayName || skillId} 已移除。`, kind: "success", durationMs: 2200 });
    } catch (e) {
        const message = resourceDeleteErrorText(e);
        addNotice({ kind: "resource_failed", title: "删除 Skill 失败", text: message });
        showToast({ title: "删除失败", text: message, kind: "error", durationMs: 3200 });
    } finally {
        S.skillBusy = false;
        renderSkillActions();
    }
}

function renderToolDetail() {
    if (!S.selectedTool) {
        U.toolEmpty.style.display = "block";
        U.toolDetail.innerHTML = "";
        setDrawerOpen(U.toolBackdrop, U.toolDrawer, false);
        renderToolActions();
        return;
    }
    U.toolEmpty.style.display = "none";
    setDrawerOpen(U.toolBackdrop, U.toolDrawer, true);
    const roles = ["ceo", "execution", "inspection"];
    const actions = toolActionsForDisplay(S.selectedTool);
    const description = String(S.selectedTool.description || "").trim();
    const toolskillContent = String(S.selectedTool.toolskill_content || "").trim();
    const isCoreTool = !!S.selectedTool.is_core;
    const repairRequired = toolRepairRequired(S.selectedTool);
    const availabilityState = resourceAvailabilityStatus(S.selectedTool);
    const unavailableReasons = resourceAvailabilityReasons(S.selectedTool);
    const execMode = execToolExecutionMode(S.selectedTool);
    const execPolicy = S.selectedTool?.exec_runtime_policy && typeof S.selectedTool.exec_runtime_policy === "object"
        ? S.selectedTool.exec_runtime_policy
        : null;
    U.toolDetail.innerHTML = `
        <article class="resource-detail-card detail-modal-shell">
            <div class="detail-modal-header">
                <div class="detail-modal-title">
                    <h2 id="tool-detail-title">${esc(S.selectedTool.display_name)}${isCoreTool ? ' <span class="meta-tag">核心工具</span>' : ''}</h2>
                    <p class="subtitle">${esc(S.selectedTool.tool_id)}</p>
                </div>
                <div class="detail-modal-actions">
                    <button type="button" class="toolbar-btn ghost" id="tool-modal-close" data-modal-close>关闭</button>
                </div>
            </div>
            <div class="detail-modal-body">
                <div class="resource-status-row" style="margin-bottom: var(--space-4);">
                    <span class="meta-tag status-${availabilityState}">${esc(displayEnabledLabel(S.selectedTool.enabled, S.selectedTool.available, repairRequired))}</span>
                    ${isCoreTool
                        ? `<button type="button" class="toolbar-btn ghost" id="tool-disable-btn" disabled>核心工具不可禁用</button>`
                        : (S.selectedTool.enabled
                            ? `<button type="button" class="toolbar-btn danger" id="tool-disable-btn">禁用工具族</button>`
                            : `<button type="button" class="toolbar-btn success" id="tool-enable-btn">启用工具族</button>`)}
                </div>
                ${!repairRequired && S.selectedTool.available === false ? `
                    <div class="resource-warning-banner" role="status" aria-live="polite">
                        <div class="resource-warning-title">当前 Tool 不可用</div>
                        <ul class="resource-warning-list">
                            ${unavailableReasons.map((reason) => `<li>${esc(reason)}</li>`).join("")}
                        </ul>
                    </div>
                ` : ""}
                <div class="resource-section">
                    <h3>描述</h3>
                    <div class="resource-copy-block">${esc(description || "暂无描述。")}</div>
                </div>
                ${isExecToolFamily(S.selectedTool) ? `
                <div class="resource-section">
                    <div class="tool-permission-heading">
                        <h3>Execution Mode</h3>
                        <p class="subtitle">保存后后续新的 exec 调用会立即应用该模式，无需重启项目。</p>
                    </div>
                    <div class="tool-permission-card">
                        <div class="tool-role-toggle-group">
                            <label class="role-toggle tool-role-toggle ${execMode === "governed" ? "checked" : ""}">
                                <input type="radio" class="exec-mode-input" name="exec-mode" value="governed" ${execMode === "governed" ? "checked" : ""}>
                                <span>governed</span>
                            </label>
                            <label class="role-toggle tool-role-toggle ${execMode === "full_access" ? "checked" : ""}">
                                <input type="radio" class="exec-mode-input" name="exec-mode" value="full_access" ${execMode === "full_access" ? "checked" : ""}>
                                <span>full_access</span>
                            </label>
                        </div>
                        ${execPolicy?.summary ? `<div class="resource-copy-block">${esc(execPolicy.summary)}</div>` : ""}
                    </div>
                </div>
                ` : ""}
                <div class="resource-draft-hint${S.toolDirty ? " is-dirty" : ""}" ${S.toolDirty ? "" : "hidden"}></div>
                <div class="resource-section">
                    <details class="resource-disclosure toolskill-disclosure">
                        <summary class="resource-disclosure-summary">
                            <span>工具技巧</span>
                            <span class="resource-disclosure-hint">点击展开</span>
                        </summary>
                        <div class="resource-disclosure-body">
                            ${toolskillContent
                                ? `<pre class="resource-preformatted toolskill-content">${esc(toolskillContent)}</pre>`
                                : `<div class="resource-empty-copy">暂无工具技巧说明。</div>`}
                        </div>
                    </details>
                </div>
                <div class="resource-section">
                    <div class="tool-permission-heading">
                        <h3>Action 权限</h3>
                        <p class="subtitle">控制当前工具族下各个 action 对主Agent、执行和检验角色的可见性。</p>
                    </div>
                    <div class="tool-permission-grid">
                        ${actions.length ? actions.map((action) => {
                            const actionName = esc(action.label || action.action_id);
                            const actionId = esc(action.action_id);
                            const riskLevel = esc(displayRiskLabel(action.risk_level || "medium"));
                            const riskClass = `risk-${String(action.risk_level || "medium").toLowerCase()}`;
                            const adminMode = String(action.admin_mode || "editable").trim().toLowerCase();
                            const agentVisible = action.agent_visible !== false;
                            if (adminMode === "readonly_system") {
                                return `
                                <article class="tool-permission-card">
                                    <div class="tool-permission-card-head">
                                        <div class="tool-action-meta">
                                            <div class="tool-action-name">${actionName}</div>
                                            <div class="tool-action-id">${actionId}</div>
                                        </div>
                                        <span class="risk-pill ${riskClass}">${riskLevel}</span>
                                    </div>
                                    <div class="resource-copy-block">系统只读项；不参与 Agent 工具可见性，不支持角色权限编辑。</div>
                                </article>`;
                            }
                            return `
                                <article class="tool-permission-card">
                                    <div class="tool-permission-card-head">
                                        <div class="tool-action-meta">
                                            <div class="tool-action-name">${actionName}</div>
                                            <div class="tool-action-id">${actionId}</div>
                                        </div>
                                        <span class="risk-pill ${riskClass}">${riskLevel}</span>
                                    </div>
                                    <div class="tool-role-toggle-group">
                                        ${roles.map((role) => `
                                            <label class="role-toggle tool-role-toggle ${action.allowed_roles?.includes(role) ? "checked" : ""}">
                                                <input type="checkbox" class="tool-role tool-role-input" data-action="${actionId}" data-role="${role}" aria-label="${actionName} - ${esc(displayRoleLabel(role))}" ${action.allowed_roles?.includes(role) ? "checked" : ""}>
                                                <span>${esc(displayRoleLabel(role))}</span>
                                            </label>
                                        `).join("")}
                                    </div>
                                    ${Array.isArray(action.allowed_roles) && action.allowed_roles.length === 0
                                        ? '<div class="resource-copy-block">当前 action 对所有角色禁用。</div>'
                                        : ''}
                                </article>`;
                        }).join("") : `<div class="tool-empty-card">当前工具族没有 action。</div>`}
                    </div>
                </div>
            </div>
        </article>`;
    const toolStatusRow = U.toolDetail.querySelector(".resource-status-row");
    if (toolStatusRow && !toolStatusRow.querySelector("#tool-delete-btn")) {
        const deleteButton = document.createElement("button");
        deleteButton.type = "button";
        deleteButton.className = `toolbar-btn ${isCoreTool ? "ghost" : "danger"}`;
        deleteButton.id = "tool-delete-btn";
        deleteButton.textContent = isCoreTool ? "核心工具不可删除" : "删除";
        deleteButton.disabled = isCoreTool;
        toolStatusRow.appendChild(deleteButton);
    }
    dockResourceStatusBadge(U.toolDetail);
    U.toolDetail.querySelector("#tool-modal-close")?.addEventListener("click", clearToolSelection);
    U.toolDetail.querySelector("#tool-enable-btn")?.addEventListener("click", () => {
        S.selectedTool.enabled = true;
        setToolDirty(true);
        renderToolDetail();
        queueToolAutosave(120);
    });
    U.toolDetail.querySelector("#tool-disable-btn")?.addEventListener("click", () => {
        S.selectedTool.enabled = false;
        setToolDirty(true);
        renderToolDetail();
        queueToolAutosave(120);
    });
    U.toolDetail.querySelector("#tool-delete-btn")?.addEventListener("click", () => void requestDeleteTool());
    U.toolDetail.querySelectorAll(".exec-mode-input").forEach((radio) => radio.addEventListener("change", (e) => {
        if (!e.target?.checked || !S.selectedTool) return;
        const nextMode = String(e.target.value || "governed").trim().toLowerCase() || "governed";
        S.selectedTool.metadata = {
            ...(S.selectedTool.metadata || {}),
            execution_mode: nextMode,
        };
        S.selectedTool.exec_runtime_policy = {
            ...(S.selectedTool.exec_runtime_policy || {}),
            mode: nextMode,
            guardrails_enabled: nextMode !== "full_access",
            summary: nextMode === "full_access"
                ? "exec will execute shell commands without exec-side guardrails."
                : "exec will enforce exec-side guardrails before running shell commands.",
        };
        setToolDirty(true);
        queueToolAutosave(120);
    }));
    U.toolDetail.querySelectorAll(".tool-role").forEach((checkbox) => checkbox.addEventListener("change", (e) => {
        const action = S.selectedTool.actions.find((item) => item.action_id === e.target.dataset.action);
        if (!action) return;
        const set = new Set(action.allowed_roles || []);
        if (e.target.checked) set.add(e.target.dataset.role);
        else set.delete(e.target.dataset.role);
        action.allowed_roles = [...set];
        e.target.closest(".role-toggle")?.classList.toggle("checked", e.target.checked);
        setToolDirty(true);
        queueToolAutosave(120);
    }));
    renderToolActions();
}

async function loadTools({ renderDetail = true } = {}) {
    if (!(S.tools || []).length) {
        U.toolList.innerHTML = '<div class="empty-state">Loading tools...</div>';
    }
    const selectedId = S.selectedTool?.tool_id || "";
    try {
        S.tools = await ApiClient.getTools(0, 300);
        if (selectedId) {
            const next = S.tools.find((tool) => tool.tool_id === selectedId);
            if (next) {
                S.selectedTool = {
                    ...next,
                    primary_executor_name: S.selectedTool?.primary_executor_name || next.primary_executor_name || "",
                    toolskill_content: S.selectedTool?.toolskill_content || "",
                    repair_required: next.repair_required ?? (next?.metadata?.repair_required === true),
                    exec_runtime_policy: S.selectedTool?.exec_runtime_policy || next.exec_runtime_policy || null,
                };
                ensureToolPageForItem(selectedId);
            }
            else clearToolSelection();
        }
        renderTools();
        if (renderDetail) renderToolDetail();
    } catch (e) {
        if (isAbortLike(e)) return;
        if (!(S.tools || []).length) {
            U.toolList.innerHTML = `<div class="empty-state error">Failed to load tools: ${esc(e.message)}</div>`;
        }
        addNotice({ kind: "resource_failed", title: "Tool load failed", text: e.message || "Unknown error" });
    } finally {
        renderToolActions();
    }
}

async function openTool(toolId, quiet = false) {
    if (!quiet) {
        setDrawerOpen(U.toolBackdrop, U.toolDrawer, true);
        U.toolEmpty.style.display = "none";
        U.toolDetail.innerHTML = '<div class="empty-state">Loading tool details...</div>';
    }
    try {
        const [tool, toolskill] = await Promise.all([
            ApiClient.getTool(toolId),
            ApiClient.getToolSkill(toolId).catch(() => ({ content: "", primary_executor_name: "" })),
        ]);
        S.selectedTool = {
            ...tool,
            primary_executor_name: toolskill?.primary_executor_name || tool?.primary_executor_name || "",
            toolskill_content: toolskill?.content || "",
            repair_required: toolskill?.repair_required ?? tool?.repair_required ?? (tool?.metadata?.repair_required === true),
            exec_runtime_policy: toolskill?.exec_runtime_policy || tool?.exec_runtime_policy || null,
        };
        ensureToolPageForItem(toolId);
        S.toolDirty = false;
        renderTools();
        renderToolDetail();
    } catch (e) {
        U.toolDetail.innerHTML = `<div class="empty-state error">Failed to load tool details: ${esc(e.message)}</div>`;
        addNotice({ kind: "resource_failed", title: "Tool detail failed", text: e.message || "Unknown error" });
    } finally {
        renderToolActions();
    }
}

async function saveTool({
    showProgressToast = true,
    showSuccessToast = true,
    reopenDetail = true,
    silentIfPristine = false,
    autosave = false,
} = {}) {
    if (S.toolBusy) {
        if (autosave) S.toolAutosavePending = true;
        return;
    }
    const selectedId = String(S.selectedTool?.tool_id || "").trim();
    const displayName = String(S.selectedTool?.display_name || selectedId || "Tool").trim();
    const enabled = !!S.selectedTool?.enabled;
    const execution_mode = isExecToolFamily(S.selectedTool) ? execToolExecutionMode(S.selectedTool) : undefined;
    const actions = Array.isArray(S.selectedTool?.actions)
        ? Object.fromEntries(
            S.selectedTool.actions.map((action) => [
                action.action_id,
                Array.isArray(action.allowed_roles) ? [...action.allowed_roles] : [],
            ])
        )
        : {};
    if (!selectedId || !S.selectedTool) {
        addNotice({ kind: "resource_failed", title: "No tool selected", text: "Select a tool before saving." });
        showToast({ title: "保存失败", text: "未选择工具族", kind: "error" });
        return;
    }
    if (!S.toolDirty) {
        if (!silentIfPristine) {
            showToast({ title: "No pending changes", text: "This tool has no unsaved changes.", kind: "info", durationMs: 1800 });
        }
        return;
    }
    cancelToolAutosave();
    S.toolBusy = true;
    renderToolActions();
    if (showProgressToast) {
        showToast({ title: "保存中", text: "正在保存工具权限，请稍候…", kind: "info", persistent: true });
    }
    try {
        await ApiClient.updateToolPolicy(selectedId, {
            enabled,
            actions,
            execution_mode,
        });
        await ApiClient.reloadResources();
        await loadTools({ renderDetail: false });
        if (reopenDetail) {
            await openTool(selectedId, true);
        }
        setToolDirty(false);
        if (showSuccessToast) {
            addNotice({ kind: "resource_saved", title: "Tool saved", text: displayName || selectedId });
            showToast({ title: "保存成功", text: "工具权限已保存", kind: "success", durationMs: 2200 });
        }
    } catch (e) {
        addNotice({ kind: "resource_failed", title: "Tool save failed", text: e.message || "Unknown error" });
        showToast({ title: "保存失败", text: e.message || "Unknown error", kind: "error", durationMs: 2600 });
    } finally {
        S.toolBusy = false;
        const queuedAutosave = !!S.toolAutosavePending;
        S.toolAutosavePending = false;
        renderToolActions();
        if (queuedAutosave && S.toolDirty) queueToolAutosave(300);
    }
}

function requestDeleteTool() {
    const selectedId = String(S.selectedTool?.tool_id || "").trim();
    const displayName = String(S.selectedTool?.display_name || selectedId || "Tool").trim();
    if (!selectedId || S.toolBusy) return;
    const detail = S.toolDirty
        ? "确认删除该工具并丢弃未保存的权限修改？相关文件和 catalog 条目也会一起移除。"
        : "确认删除该工具？相关文件和 catalog 条目也会一起移除。";
    openConfirm({
        title: "删除工具",
        text: detail,
        confirmLabel: "删除",
        confirmKind: "danger",
        returnFocus: U.toolRefresh,
        onConfirm: () => performDeleteTool(selectedId, displayName),
    });
}

async function performDeleteTool(toolId, displayName) {
    if (!toolId) return;
    if (S.selectedTool?.is_core) {
        showToast({ title: "无法删除", text: "核心工具不可删除。", kind: "error", durationMs: 2200 });
        return;
    }
    S.toolBusy = true;
    renderToolActions();
    showToast({ title: "正在删除", text: `正在移除 ${displayName || toolId}...`, kind: "info", persistent: true });
    try {
        await ApiClient.deleteTool(toolId);
        clearToolSelection();
        await loadTools({ renderDetail: false });
        addNotice({ kind: "resource_saved", title: "工具已删除", text: displayName || toolId });
        showToast({ title: "已删除", text: `${displayName || toolId} 已移除。`, kind: "success", durationMs: 2200 });
    } catch (e) {
        const message = resourceDeleteErrorText(e);
        addNotice({ kind: "resource_failed", title: "删除工具失败", text: message });
        showToast({ title: "删除失败", text: message, kind: "error", durationMs: 3200 });
    } finally {
        S.toolBusy = false;
        renderToolActions();
    }
}

async function refreshSkills() {
    const selectedId = S.selectedSkill?.skill_id || "";
    S.skillBusy = true;
    renderSkillActions();
    try {
        await ApiClient.reloadResources();
        await loadSkills();
        if (selectedId && S.skills.some((skill) => skill.skill_id === selectedId)) {
            await openSkill(selectedId);
        }
        addNotice({ kind: "resource_refreshed", title: "Skills refreshed", text: "Resource registry reloaded." });
    } catch (e) {
        addNotice({ kind: "resource_failed", title: "Skill refresh failed", text: e.message || "Unknown error" });
    } finally {
        S.skillBusy = false;
        renderSkillActions();
    }
}

async function refreshTools() {
    const selectedId = S.selectedTool?.tool_id || "";
    S.toolBusy = true;
    renderToolActions();
    try {
        await ApiClient.reloadResources();
        await loadTools();
        if (selectedId && S.tools.some((tool) => tool.tool_id === selectedId)) {
            await openTool(selectedId);
        }
        addNotice({ kind: "resource_refreshed", title: "Tools refreshed", text: "Resource registry reloaded." });
    } catch (e) {
        addNotice({ kind: "resource_failed", title: "Tool refresh failed", text: e.message || "Unknown error" });
    } finally {
        S.toolBusy = false;
        renderToolActions();
    }
}
