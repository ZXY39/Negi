// Resource views (skills, tools) restored from org_graph_app.js.
// Loaded after org_graph_app.js so shared state/helpers are already available.

function filterSkills() {
    const q = String(U.skillSearch.value || "").trim().toLowerCase();
    return ResourcesState.skills.filter((skill) => {
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

function renderSkills() {
    U.skillList.innerHTML = "";
    const meta = paginateResources(filterSkills(), ResourcesState.skillPage, ResourcesState.skillPageSize);
    ResourcesState.skillPage = meta.currentPage;
    ResourcesState.skillPageSize = meta.pageSize;
    syncResourcePagination("skill", meta);
    if (!meta.total) return void (U.skillList.innerHTML = '<div class="empty-state">没有匹配的 Skill。</div>');
    meta.items.forEach((skill) => {
        const el = document.createElement("button");
        el.type = "button";
        el.className = `resource-list-item${ResourcesState.selectedSkill?.skill_id === skill.skill_id ? " selected" : ""}`;
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

function isToolAdminVisible(tool) {
    return !!tool && tool.agent_visible !== false;
}

function isExecTool(tool) {
    return String(tool?.tool_id || "").trim().toLowerCase() === "exec";
}

function execToolExecutionMode(tool) {
    const policyMode = String(tool?.exec_runtime_policy?.mode || "").trim().toLowerCase();
    if (policyMode) return policyMode;
    const metadataMode = String(tool?.metadata?.execution_mode || "").trim().toLowerCase();
    if (metadataMode) return metadataMode;
    return "governed";
}

function execToolModeLabel(mode) {
    return ({
        governed: "监管模式",
        full_access: "全权限模式",
    }[String(mode || "").trim().toLowerCase()] || "监管模式");
}

function execToolModeSummary(mode) {
    return ({
        governed: "exec 将继续应用 exec 侧护栏，包括只读、路径与安全限制。",
        full_access: "exec 将以全权限模式执行 shell 命令，不再应用 exec 侧护栏。",
    }[String(mode || "").trim().toLowerCase()] || "exec 将继续应用 exec 侧护栏，包括只读、路径与安全限制。");
}

function applyExecToolExecutionMode(tool, mode) {
    if (!tool || !isExecTool(tool)) return tool;
    const nextMode = String(mode || "governed").trim().toLowerCase() || "governed";
    tool.metadata = {
        ...(tool.metadata || {}),
        execution_mode: nextMode,
    };
    tool.exec_runtime_policy = {
        ...(tool.exec_runtime_policy || {}),
        mode: nextMode,
        guardrails_enabled: nextMode !== "full_access",
        summary: execToolModeSummary(nextMode),
    };
    return tool;
}

function filterTools() {
    const q = String(U.toolSearch.value || "").trim().toLowerCase();
    return ResourcesState.tools.filter((tool) => {
        if (!isToolAdminVisible(tool)) return false;
        if (q && !`${tool.tool_id} ${tool.display_name} ${tool.description} ${tool.source_path}`.toLowerCase().includes(q)) return false;
        const statusFilter = String(U.toolStatus.value || "all").trim().toLowerCase();
        const statusKey = toolRepairRequired(tool)
            ? "repair_required"
            : (tool.available === false ? "unavailable" : (tool.enabled ? "enabled" : "disabled"));
        if (statusFilter !== "all" && statusKey !== statusFilter) return false;
        if (U.toolRisk.value !== "all" && String(tool.risk_level || "").trim().toLowerCase() !== U.toolRisk.value) return false;
        return true;
    });
}

function renderTools() {
    U.toolList.innerHTML = "";
    const meta = paginateResources(filterTools(), ResourcesState.toolPage, ResourcesState.toolPageSize);
    ResourcesState.toolPage = meta.currentPage;
    ResourcesState.toolPageSize = meta.pageSize;
    syncResourcePagination("tool", meta);
    if (!meta.total) return void (U.toolList.innerHTML = '<div class="empty-state">没有匹配的 Tool。</div>');
    meta.items.forEach((tool) => {
        const el = document.createElement("button");
        el.type = "button";
        el.className = `resource-list-item${ResourcesState.selectedTool?.tool_id === tool.tool_id ? " selected" : ""}`;
        const desc = (tool.description || "").trim();
        const subtitle = desc ? (desc.length > 50 ? desc.slice(0, 47) + "..." : desc) : tool.tool_id;
        el.innerHTML = `
            <div class="resource-list-title">${esc(tool.display_name)}</div>
            <div class="resource-list-subtitle">${esc(subtitle)}</div>
            <div class="resource-list-meta">
                <span class="meta-tag status-${resourceAvailabilityStatus(tool)}">${esc(displayEnabledLabel(tool.enabled, tool.available, toolRepairRequired(tool)))}</span>
                <span class="meta-tag risk-${String(tool.risk_level || "medium").trim().toLowerCase()}">${esc(displayRiskLabel(tool.risk_level || "medium"))}</span>
                ${tool.is_core ? '<span class="meta-tag">核心工具</span>' : ''}
            </div>`;
        el.addEventListener("click", () => openTool(tool.tool_id));
        U.toolList.appendChild(el);
    });
}


function cancelSkillAutosave() {
    if (ResourcesState.skillAutosaveTimerId) {
        window.clearTimeout(ResourcesState.skillAutosaveTimerId);
        ResourcesState.skillAutosaveTimerId = null;
    }
}

function queueSkillAutosave(delayMs = 900) {
    cancelSkillAutosave();
    if (!ResourcesState.selectedSkill || !ResourcesState.skillDirty) return;
    ResourcesState.skillAutosaveTimerId = window.setTimeout(() => {
        ResourcesState.skillAutosaveTimerId = null;
        if (!ResourcesState.selectedSkill || !ResourcesState.skillDirty) return;
        void saveSkill({ showProgressToast: false, showSuccessToast: false, reopenDetail: false, silentIfPristine: true, autosave: true });
    }, Math.max(0, Number(delayMs) || 0));
}

function cancelToolAutosave() {
    if (ResourcesState.toolAutosaveTimerId) {
        window.clearTimeout(ResourcesState.toolAutosaveTimerId);
        ResourcesState.toolAutosaveTimerId = null;
    }
}

function queueToolAutosave(delayMs = 300) {
    cancelToolAutosave();
    if (!ResourcesState.selectedTool || !ResourcesState.toolDirty) return;
    ResourcesState.toolAutosaveTimerId = window.setTimeout(() => {
        ResourcesState.toolAutosaveTimerId = null;
        if (!ResourcesState.selectedTool || !ResourcesState.toolDirty) return;
        void saveTool({ showProgressToast: false, showSuccessToast: false, reopenDetail: false, silentIfPristine: true, autosave: true });
    }, Math.max(0, Number(delayMs) || 0));
}

function renderSkillDetail() {
    if (!ResourcesState.selectedSkill) {
        U.skillEmpty.style.display = "block";
        U.skillDetail.innerHTML = "";
        setDrawerOpen(U.skillBackdrop, U.skillDrawer, false);
        renderSkillActions();
        return;
    }
    U.skillEmpty.style.display = "none";
    setDrawerOpen(U.skillBackdrop, U.skillDrawer, true);
    const selectedFileKey = String(ResourcesState.selectedSkillFile || "").trim();
    const fileLoaded = selectedFileKey
        ? Object.prototype.hasOwnProperty.call(ResourcesState.skillContents, selectedFileKey)
        : false;
    const roles = ["ceo", "execution", "inspection"];
    const allowedRoles = Array.isArray(ResourcesState.selectedSkill.allowed_roles) ? ResourcesState.selectedSkill.allowed_roles : [];
    const editorValue = esc(fileLoaded ? (ResourcesState.skillContents[selectedFileKey] || "") : "Loading file...");
    const availabilityState = resourceAvailabilityStatus(ResourcesState.selectedSkill);
    const skillRisk = String(ResourcesState.selectedSkill.risk_level || "medium").trim().toLowerCase();
    const unavailableReasons = resourceAvailabilityReasons(ResourcesState.selectedSkill);
    const fileTabs = ResourcesState.skillFiles.length
        ? ResourcesState.skillFiles.map((file) => `<button type="button" class="toolbar-btn ghost skill-file ${ResourcesState.selectedSkillFile === file.file_key ? "active" : ""}" data-file="${esc(file.file_key)}">${esc(file.file_key)}</button>`).join("")
        : '<span class="resource-empty-copy">暂无可编辑文件</span>';
    U.skillDetail.innerHTML = `
        <article class="resource-detail-card detail-modal-shell">
            <div class="detail-modal-header">
                <div class="detail-modal-title">
                    <h2 id="skill-detail-title">${esc(ResourcesState.selectedSkill.display_name)}</h2>
                    <div class="detail-modal-meta">
                        <span class="meta-tag risk-${esc(skillRisk)}">${esc(displayRiskLabel(skillRisk))}</span>
                        <span class="meta-tag status-${availabilityState}">${esc(displayEnabledLabel(ResourcesState.selectedSkill.enabled, ResourcesState.selectedSkill.available))}</span>
                    </div>
                </div>
                <div class="detail-modal-actions">
                    <button type="button" class="toolbar-btn" id="skill-modal-save">保存</button>
                    <button type="button" class="toolbar-btn ghost" id="skill-modal-close" data-modal-close>关闭</button>
                </div>
            </div>
            <div class="detail-modal-body">
                <div class="resource-status-row" style="margin-bottom: var(--space-4);">
                    ${ResourcesState.selectedSkill.enabled
                        ? `<button type="button" class="toolbar-btn danger" id="skill-disable-btn">禁用技能</button>`
                        : `<button type="button" class="toolbar-btn success" id="skill-enable-btn">启用技能</button>`}
                    <button type="button" class="toolbar-btn danger" id="skill-delete-btn">删除</button>
                </div>
                ${ResourcesState.selectedSkill.available === false ? `
                    <div class="resource-warning-banner" role="status" aria-live="polite">
                        <div class="resource-warning-title">当前 Skill 不可用</div>
                        <ul class="resource-warning-list">
                            ${unavailableReasons.map((reason) => `<li>${esc(reason)}</li>`).join("")}
                        </ul>
                    </div>
                ` : ""}
                <div class="resource-draft-hint${ResourcesState.skillDirty ? " is-dirty" : ""}" ${ResourcesState.skillDirty ? "" : "hidden"}></div>
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
    U.skillDetail.querySelector("#skill-modal-close")?.addEventListener("click", clearSkillSelection);
    U.skillDetail.querySelector("#skill-modal-save")?.addEventListener("click", () => void saveSkill());
    U.skillDetail.querySelector("#skill-enable-btn")?.addEventListener("click", () => {
        ResourcesState.selectedSkill.enabled = true;
        setSkillDirty(true);
        renderSkillDetail();
    });
    U.skillDetail.querySelector("#skill-disable-btn")?.addEventListener("click", () => {
        ResourcesState.selectedSkill.enabled = false;
        setSkillDirty(true);
        renderSkillDetail();
    });
    U.skillDetail.querySelector("#skill-delete-btn")?.addEventListener("click", () => void requestDeleteSkill());
    U.skillDetail.querySelectorAll(".skill-role").forEach((checkbox) => checkbox.addEventListener("change", (e) => {
        const nextRoles = new Set(allowedRoles);
        if (e.target.checked) nextRoles.add(e.target.dataset.role);
        else nextRoles.delete(e.target.dataset.role);
        ResourcesState.selectedSkill.allowed_roles = [...nextRoles];
        setSkillDirty(true);
        renderSkillDetail();
    }));
    U.skillDetail.querySelectorAll(".skill-file").forEach((button) => button.addEventListener("click", () => {
        const nextFileKey = String(button.dataset.file || "").trim();
        if (!nextFileKey || nextFileKey === String(ResourcesState.selectedSkillFile || "").trim()) return;
        const editor = document.getElementById("skill-editor");
        const currentFileKey = String(ResourcesState.selectedSkillFile || "").trim();
        if (editor && currentFileKey && Object.prototype.hasOwnProperty.call(ResourcesState.skillContents, currentFileKey)) {
            ResourcesState.skillContents[currentFileKey] = editor.value;
        }
        ResourcesState.selectedSkillFile = nextFileKey;
        renderSkillDetail();
        void ensureSkillFileLoaded(ResourcesState.selectedSkill?.skill_id, nextFileKey);
    }));
    U.skillDetail.querySelector("#skill-editor")?.addEventListener("input", (e) => {
        if (!ResourcesState.selectedSkillFile) return;
        ResourcesState.skillContents[ResourcesState.selectedSkillFile] = e.target.value;
        setSkillDirty(true);
    });
    renderSkillActions();
}

async function ensureSkillFileLoaded(skillId, fileKey) {
    const normalizedSkillId = String(skillId || ResourcesState.selectedSkill?.skill_id || "").trim();
    const normalizedFileKey = String(fileKey || "").trim();
    const loadKey = `${normalizedSkillId}::${normalizedFileKey}`;
    if (!normalizedSkillId || !normalizedFileKey) return "";
    if (Object.prototype.hasOwnProperty.call(ResourcesState.skillContents, normalizedFileKey)) {
        return ResourcesState.skillContents[normalizedFileKey] || "";
    }
    if (ResourcesState.skillFileLoads[loadKey]) {
        return ResourcesState.skillFileLoads[loadKey];
    }
    ResourcesState.skillFileLoads[loadKey] = ApiClient.getSkillFile(normalizedSkillId, normalizedFileKey)
        .then((data) => {
            const content = String(data?.content || "");
            if (String(ResourcesState.selectedSkill?.skill_id || "").trim() === normalizedSkillId) {
                ResourcesState.skillContents[normalizedFileKey] = content;
                if (String(ResourcesState.selectedSkillFile || "").trim() === normalizedFileKey) {
                    renderSkillDetail();
                }
            }
            return content;
        })
        .catch((error) => {
            if (String(ResourcesState.selectedSkill?.skill_id || "").trim() === normalizedSkillId) {
                ResourcesState.skillContents[normalizedFileKey] = "";
                if (String(ResourcesState.selectedSkillFile || "").trim() === normalizedFileKey) {
                    renderSkillDetail();
                }
                showToast({ title: "Skill file load failed", text: error.message || normalizedFileKey, kind: "error" });
            }
            return "";
        })
        .finally(() => {
            if (ResourcesState.skillFileLoads[loadKey]) {
                delete ResourcesState.skillFileLoads[loadKey];
            }
        });
    return ResourcesState.skillFileLoads[loadKey];
}

function hasContextRiskCatalog() {
    return (Array.isArray(ResourcesState.skills) ? ResourcesState.skills.length : 0) > 0
        && (Array.isArray(ResourcesState.tools) ? ResourcesState.tools.length : 0) > 0;
}

// 加载 chip 的风险度图标按 skill/tool 目录取色，而目录过去只有资源页手动刷新时才填充。
// 这里只补 S 上的目录缓存、不渲染资源页，让节点详情与会话提醒都能拿到真实风险度。
async function ensureContextRiskCatalog() {
    if (hasContextRiskCatalog()) return true;
    if (ResourcesState.contextRiskCatalogRequest) return ResourcesState.contextRiskCatalogRequest;
    const request = Promise.all([
        (Array.isArray(ResourcesState.skills) && ResourcesState.skills.length) ? Promise.resolve(ResourcesState.skills) : ApiClient.getSkills(0, 300),
        (Array.isArray(ResourcesState.tools) && ResourcesState.tools.length) ? Promise.resolve(ResourcesState.tools) : ApiClient.getTools(0, 300),
    ]).then(([skills, tools]) => {
        if (Array.isArray(skills) && skills.length) ResourcesState.skills = skills;
        if (Array.isArray(tools) && tools.length) ResourcesState.tools = tools;
        return hasContextRiskCatalog();
    }).catch(() => false).finally(() => {
        if (ResourcesState.contextRiskCatalogRequest === request) ResourcesState.contextRiskCatalogRequest = null;
    });
    ResourcesState.contextRiskCatalogRequest = request;
    return request;
}

async function loadSkills({ renderDetail = true } = {}) {
    if (!(ResourcesState.skills || []).length) {
        U.skillList.innerHTML = '<div class="empty-state">Loading skills...</div>';
    }
    const selectedId = ResourcesState.selectedSkill?.skill_id || "";
    try {
        ResourcesState.skills = await ApiClient.getSkills(0, 300);
        if (selectedId) {
            const next = ResourcesState.skills.find((skill) => skill.skill_id === selectedId);
            if (next) {
                ResourcesState.selectedSkill = next;
                ensureSkillPageForItem(selectedId);
            }
            else clearSkillSelection();
        }
        renderSkills();
        if (renderDetail) renderSkillDetail();
    } catch (e) {
        if (isAbortLike(e)) return;
        if (!(ResourcesState.skills || []).length) {
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
        ResourcesState.selectedSkill = skill;
        ensureSkillPageForItem(skillId);
        ResourcesState.skillFiles = files;
        ResourcesState.selectedSkillFile = files[0]?.file_key || "";
        ResourcesState.skillContents = {};
        ResourcesState.skillFileLoads = {};
        ResourcesState.skillDirty = false;
        renderSkills();
        renderSkillDetail();
        if (ResourcesState.selectedSkillFile) {
            await ensureSkillFileLoaded(skillId, ResourcesState.selectedSkillFile);
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
    if (ResourcesState.skillBusy) {
        if (autosave) ResourcesState.skillAutosavePending = true;
        return;
    }
    const selectedId = String(ResourcesState.selectedSkill?.skill_id || "").trim();
    const displayName = String(ResourcesState.selectedSkill?.display_name || selectedId || "Skill").trim();
    const enabled = !!ResourcesState.selectedSkill?.enabled;
    const allowedRoles = Array.isArray(ResourcesState.selectedSkill?.allowed_roles) ? [...ResourcesState.selectedSkill.allowed_roles] : [];
    if (!selectedId || !ResourcesState.selectedSkill) {
        addNotice({ kind: "resource_failed", title: "No skill selected", text: "Select a skill before saving." });
        showToast({ title: "保存失败", text: "未选择 Skill", kind: "error" });
        return;
    }
    if (!ResourcesState.skillDirty) {
        if (!silentIfPristine) {
            showToast({ title: "No pending changes", text: "This skill has no unsaved changes.", kind: "info", durationMs: 1800 });
        }
        return;
    }
    cancelSkillAutosave();
    ResourcesState.skillBusy = true;
    renderSkillActions();
    if (showProgressToast) {
        showToast({ title: "保存中", text: "正在保存 Skill，请稍候…", kind: "info", persistent: true });
    }
    try {
        const editor = document.getElementById("skill-editor");
        if (editor && ResourcesState.selectedSkillFile) ResourcesState.skillContents[ResourcesState.selectedSkillFile] = editor.value;
        for (const [fileKey, content] of Object.entries(ResourcesState.skillContents)) {
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
        ResourcesState.skillBusy = false;
        const queuedAutosave = !!ResourcesState.skillAutosavePending;
        ResourcesState.skillAutosavePending = false;
        renderSkillActions();
        if (queuedAutosave && ResourcesState.skillDirty) queueSkillAutosave(300);
    }
}

async function requestDeleteSkill() {
    const selectedId = String(ResourcesState.selectedSkill?.skill_id || "").trim();
    const displayName = String(ResourcesState.selectedSkill?.display_name || selectedId || "Skill").trim();
    if (!selectedId || ResourcesState.skillBusy) return;
    const detail = ResourcesState.skillDirty
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

// 资源删除的唯一收尾判据：删掉没删掉看磁盘/列表，不看这次请求是否"顺利返回"。
// 服务端删的是真目录（MCP 工具常带 node_modules），Windows 上 rmtree 几十秒很常见，
// 中途还可能撞上浏览器断连或服务重启。所以这里做两件事：
//   1) 请求本身不带客户端超时（ApiClient.deleteTool / deleteSkill 的 timeoutMs=0）；
//   2) 请求报错时先回读一次列表复核，只有"确实还在"才算失败，绝不把已删除报成 timeout。
function deleteProgressText(displayName, startedAt) {
    const elapsedSeconds = Math.max(1, Math.round((Date.now() - startedAt) / 1000));
    return `正在移除 ${displayName}… 已用时 ${elapsedSeconds}s（目录较大时可能需要几十秒，请不要关闭页面）`;
}

// 常驻进度提示：删除期间没有别的信号，秒数在走就说明请求还活着。
// 只改文本节点，不重复走 showToast——那会每秒重建一次 toast 图标。
function startDeleteProgress(displayName) {
    const startedAt = Date.now();
    showToast({
        title: "正在删除",
        text: deleteProgressText(displayName, startedAt),
        kind: "info",
        persistent: true,
        spin: true,
    });
    const ticker = window.setInterval(() => {
        if (!U.toastText || String(U.toastTitle?.textContent || "") !== "正在删除") return;
        U.toastText.textContent = deleteProgressText(displayName, startedAt);
    }, 1000);
    return () => window.clearInterval(ticker);
}

async function runResourceDelete({ request, reload, isGone }) {
    let requestError = null;
    try {
        await request();
    } catch (e) {
        requestError = e;
    }
    if (!requestError) return { removed: true, error: null };
    // 请求失败不等于没删掉：回读一次列表，确认资源是否真的还在。
    let stillPresent = true;
    try {
        await reload();
        stillPresent = !(await isGone());
    } catch (e) {
        void e;
    }
    if (!stillPresent) return { removed: true, error: requestError };
    return { removed: false, error: requestError };
}

async function performDeleteSkill(skillId, displayName) {
    if (!skillId) return;
    ResourcesState.skillBusy = true;
    renderSkillActions();
    const label = displayName || skillId;
    const stopProgress = startDeleteProgress(label);
    try {
        const { removed, error } = await runResourceDelete({
            request: () => ApiClient.deleteSkill(skillId),
            reload: () => loadSkills({ renderDetail: false }),
            isGone: () => !(ResourcesState.skills || []).some((skill) => String(skill.skill_id || "") === skillId),
        });
        if (removed) {
            clearSkillSelection();
            await loadSkills({ renderDetail: false });
            addNotice({ kind: "resource_saved", title: "Skill 已删除", text: label });
            showToast({ title: "已删除", text: `${label} 已移除。`, kind: "success", durationMs: 2200 });
        } else {
            const message = resourceDeleteErrorText(error);
            addNotice({ kind: "resource_failed", title: "删除 Skill 失败", text: message });
            showToast({ title: "删除失败", text: message, kind: "error", durationMs: 3200 });
        }
    } finally {
        stopProgress();
        ResourcesState.skillBusy = false;
        renderSkillActions();
    }
}

function renderToolDetail() {
    if (!ResourcesState.selectedTool) {
        U.toolEmpty.style.display = "block";
        U.toolDetail.innerHTML = "";
        setDrawerOpen(U.toolBackdrop, U.toolDrawer, false);
        renderToolActions();
        return;
    }
    U.toolEmpty.style.display = "none";
    setDrawerOpen(U.toolBackdrop, U.toolDrawer, true);
    const roles = ["ceo", "execution", "inspection"];
    const allowedRoles = Array.isArray(ResourcesState.selectedTool.allowed_roles) ? ResourcesState.selectedTool.allowed_roles : [];
    const toolRisk = String(ResourcesState.selectedTool.risk_level || "medium").trim().toLowerCase();
    const description = String(ResourcesState.selectedTool.description || "").trim();
    const toolskillContent = String(ResourcesState.selectedTool.toolskill_content || "").trim();
    const isCoreTool = !!ResourcesState.selectedTool.is_core;
    const repairRequired = toolRepairRequired(ResourcesState.selectedTool);
    const availabilityState = resourceAvailabilityStatus(ResourcesState.selectedTool);
    const unavailableReasons = resourceAvailabilityReasons(ResourcesState.selectedTool);
    const execMode = execToolExecutionMode(ResourcesState.selectedTool);
    U.toolDetail.innerHTML = `
        <article class="resource-detail-card detail-modal-shell">
            <div class="detail-modal-header">
                <div class="detail-modal-title">
                    <h2 id="tool-detail-title">${esc(ResourcesState.selectedTool.display_name)}</h2>
                    <div class="detail-modal-meta">
                        <span class="meta-tag risk-${esc(toolRisk)}">${esc(displayRiskLabel(toolRisk))}</span>
                        <span class="meta-tag status-${availabilityState}">${esc(displayEnabledLabel(ResourcesState.selectedTool.enabled, ResourcesState.selectedTool.available, repairRequired))}</span>
                        ${isCoreTool ? '<span class="meta-tag">核心工具</span>' : ''}
                    </div>
                </div>
                <div class="detail-modal-actions">
                    <button type="button" class="toolbar-btn" id="tool-modal-save">保存</button>
                    <button type="button" class="toolbar-btn ghost" id="tool-modal-close" data-modal-close>关闭</button>
                </div>
            </div>
            <div class="detail-modal-body">
                <div class="resource-status-row" style="margin-bottom: var(--space-4);">
                    ${ResourcesState.selectedTool.enabled
                        ? `<button type="button" class="toolbar-btn danger" id="tool-disable-btn">禁用工具</button>`
                        : `<button type="button" class="toolbar-btn success" id="tool-enable-btn">启用工具</button>`}
                </div>
                ${!repairRequired && ResourcesState.selectedTool.available === false ? `
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
                ${isExecTool(ResourcesState.selectedTool) ? `
                <div class="resource-section">
                    <div class="tool-permission-heading">
                        <h3>执行模式</h3>
                    </div>
                    <div class="tool-permission-card">
                        <div class="tool-role-toggle-group">
                            <label class="role-toggle tool-role-toggle ${execMode === "governed" ? "checked" : ""}">
                                <input type="radio" class="exec-mode-input" name="exec-mode" value="governed" ${execMode === "governed" ? "checked" : ""}>
                                <span>${esc(execToolModeLabel("governed"))}</span>
                            </label>
                            <label class="role-toggle tool-role-toggle ${execMode === "full_access" ? "checked" : ""}">
                                <input type="radio" class="exec-mode-input" name="exec-mode" value="full_access" ${execMode === "full_access" ? "checked" : ""}>
                                <span>${esc(execToolModeLabel("full_access"))}</span>
                            </label>
                        </div>
                        <div class="resource-copy-block">${esc(execToolModeSummary(execMode))}</div>
                    </div>
                </div>
                ` : ""}
                <div class="resource-draft-hint${ResourcesState.toolDirty ? " is-dirty" : ""}" ${ResourcesState.toolDirty ? "" : "hidden"}></div>
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
                        <h3>角色可见</h3>
                    </div>
                    <div class="tool-permission-card">
                        <div class="tool-role-toggle-group">
                            ${roles.map((role) => `
                                <label class="role-toggle tool-role-toggle ${allowedRoles.includes(role) ? "checked" : ""}">
                                    <input type="checkbox" class="tool-role tool-role-input" data-role="${role}" aria-label="${esc(displayRoleLabel(role))}" ${allowedRoles.includes(role) ? "checked" : ""}>
                                    <span>${esc(displayRoleLabel(role))}</span>
                                </label>
                            `).join("")}
                        </div>
                        ${allowedRoles.length === 0 ? '<div class="resource-copy-block">当前工具对所有角色禁用。</div>' : ''}
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
    U.toolDetail.querySelector("#tool-modal-close")?.addEventListener("click", clearToolSelection);
    U.toolDetail.querySelector("#tool-modal-save")?.addEventListener("click", () => void saveTool());
    U.toolDetail.querySelector("#tool-enable-btn")?.addEventListener("click", () => {
        ResourcesState.selectedTool.enabled = true;
        setToolDirty(true);
        renderToolDetail();
    });
    U.toolDetail.querySelector("#tool-disable-btn")?.addEventListener("click", () => {
        ResourcesState.selectedTool.enabled = false;
        setToolDirty(true);
        renderToolDetail();
    });
    U.toolDetail.querySelector("#tool-delete-btn")?.addEventListener("click", () => void requestDeleteTool());
    U.toolDetail.querySelectorAll(".exec-mode-input").forEach((radio) => radio.addEventListener("change", (e) => {
        if (!e.target?.checked || !ResourcesState.selectedTool) return;
        const nextMode = String(e.target.value || "governed").trim().toLowerCase() || "governed";
        applyExecToolExecutionMode(ResourcesState.selectedTool, nextMode);
        setToolDirty(true);
        renderToolDetail();
    }));
    U.toolDetail.querySelectorAll(".tool-role").forEach((checkbox) => checkbox.addEventListener("change", (e) => {
        const set = new Set(ResourcesState.selectedTool.allowed_roles || []);
        if (e.target.checked) set.add(e.target.dataset.role);
        else set.delete(e.target.dataset.role);
        ResourcesState.selectedTool.allowed_roles = [...set];
        e.target.closest(".role-toggle")?.classList.toggle("checked", e.target.checked);
        setToolDirty(true);
    }));
    renderToolActions();
}

async function loadTools({ renderDetail = true } = {}) {
    if (!(ResourcesState.tools || []).length) {
        U.toolList.innerHTML = '<div class="empty-state">Loading tools...</div>';
    }
    const selectedId = ResourcesState.selectedTool?.tool_id || "";
    try {
        ResourcesState.tools = await ApiClient.getTools(0, 300);
        if (selectedId) {
            const next = ResourcesState.tools.find((tool) => tool.tool_id === selectedId);
            if (next) {
                ResourcesState.selectedTool = {
                    ...next,
                    toolskill_content: ResourcesState.selectedTool?.toolskill_content || "",
                    repair_required: next.repair_required ?? (next?.metadata?.repair_required === true),
                    exec_runtime_policy: ResourcesState.selectedTool?.exec_runtime_policy || next.exec_runtime_policy || null,
                };
                ensureToolPageForItem(selectedId);
            }
            else clearToolSelection();
        }
        renderTools();
        if (renderDetail) renderToolDetail();
    } catch (e) {
        if (isAbortLike(e)) return;
        if (!(ResourcesState.tools || []).length) {
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
            ApiClient.getToolSkill(toolId).catch(() => ({ content: "" })),
        ]);
        ResourcesState.selectedTool = {
            ...tool,
            toolskill_content: toolskill?.content || "",
            repair_required: toolskill?.repair_required ?? tool?.repair_required ?? (tool?.metadata?.repair_required === true),
            exec_runtime_policy: toolskill?.exec_runtime_policy || tool?.exec_runtime_policy || null,
        };
        ensureToolPageForItem(toolId);
        ResourcesState.toolDirty = false;
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
    if (ResourcesState.toolBusy) {
        if (autosave) ResourcesState.toolAutosavePending = true;
        return;
    }
    const selectedId = String(ResourcesState.selectedTool?.tool_id || "").trim();
    const displayName = String(ResourcesState.selectedTool?.display_name || selectedId || "Tool").trim();
    const enabled = !!ResourcesState.selectedTool?.enabled;
    const execution_mode = isExecTool(ResourcesState.selectedTool) ? execToolExecutionMode(ResourcesState.selectedTool) : undefined;
    const allowedRoles = Array.isArray(ResourcesState.selectedTool?.allowed_roles) ? [...ResourcesState.selectedTool.allowed_roles] : [];
    if (!selectedId || !ResourcesState.selectedTool) {
        addNotice({ kind: "resource_failed", title: "No tool selected", text: "Select a tool before saving." });
        showToast({ title: "保存失败", text: "未选择工具", kind: "error" });
        return;
    }
    if (!ResourcesState.toolDirty) {
        if (!silentIfPristine) {
            showToast({ title: "No pending changes", text: "This tool has no unsaved changes.", kind: "info", durationMs: 1800 });
        }
        return;
    }
    cancelToolAutosave();
    ResourcesState.toolBusy = true;
    renderToolActions();
    if (showProgressToast) {
        showToast({ title: "保存中", text: "正在保存工具权限，请稍候…", kind: "info", persistent: true });
    }
    try {
        await ApiClient.updateToolPolicy(selectedId, {
            enabled,
            allowed_roles: allowedRoles,
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
        ResourcesState.toolBusy = false;
        const queuedAutosave = !!ResourcesState.toolAutosavePending;
        ResourcesState.toolAutosavePending = false;
        renderToolActions();
        if (queuedAutosave && ResourcesState.toolDirty) queueToolAutosave(300);
    }
}

function requestDeleteTool() {
    const selectedId = String(ResourcesState.selectedTool?.tool_id || "").trim();
    const displayName = String(ResourcesState.selectedTool?.display_name || selectedId || "Tool").trim();
    if (!selectedId || ResourcesState.toolBusy) return;
    const detail = ResourcesState.toolDirty
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
    if (ResourcesState.selectedTool?.is_core) {
        showToast({ title: "无法删除", text: "核心工具不可删除。", kind: "error", durationMs: 2200 });
        return;
    }
    ResourcesState.toolBusy = true;
    renderToolActions();
    const label = displayName || toolId;
    const stopProgress = startDeleteProgress(label);
    try {
        const { removed, error } = await runResourceDelete({
            request: () => ApiClient.deleteTool(toolId),
            reload: () => loadTools({ renderDetail: false }),
            isGone: () => !(ResourcesState.tools || []).some((tool) => String(tool.tool_id || "") === toolId),
        });
        if (removed) {
            clearToolSelection();
            await loadTools({ renderDetail: false });
            addNotice({ kind: "resource_saved", title: "工具已删除", text: label });
            showToast({ title: "已删除", text: `${label} 已移除。`, kind: "success", durationMs: 2200 });
        } else {
            const message = resourceDeleteErrorText(error);
            addNotice({ kind: "resource_failed", title: "删除工具失败", text: message });
            showToast({ title: "删除失败", text: message, kind: "error", durationMs: 3200 });
        }
    } finally {
        stopProgress();
        ResourcesState.toolBusy = false;
        renderToolActions();
    }
}

async function refreshSkills() {
    const selectedId = ResourcesState.selectedSkill?.skill_id || "";
    ResourcesState.skillBusy = true;
    renderSkillActions();
    try {
        await ApiClient.reloadResources();
        await loadSkills();
        if (selectedId && ResourcesState.skills.some((skill) => skill.skill_id === selectedId)) {
            await openSkill(selectedId);
        }
    } catch (e) {
        addNotice({ kind: "resource_failed", title: "Skill refresh failed", text: e.message || "Unknown error" });
    } finally {
        ResourcesState.skillBusy = false;
        renderSkillActions();
    }
}

async function refreshTools() {
    const selectedId = ResourcesState.selectedTool?.tool_id || "";
    ResourcesState.toolBusy = true;
    renderToolActions();
    try {
        await ApiClient.reloadResources();
        await loadTools();
        if (selectedId && ResourcesState.tools.some((tool) => tool.tool_id === selectedId)) {
            await openTool(selectedId);
        }
    } catch (e) {
        addNotice({ kind: "resource_failed", title: "Tool refresh failed", text: e.message || "Unknown error" });
    } finally {
        ResourcesState.toolBusy = false;
        renderToolActions();
    }
}
