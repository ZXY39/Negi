const S = {
    view: "ceo",
    ceoWs: null,
    projectWs: null,
    currentProjectId: null,
    projects: [],
    selectedProjects: new Set(),
    projectBusy: false,
    modelCatalog: { items: [], defaults: { ceo: "", execution: "", inspection: "" }, loading: false, saving: false, error: "" },
    tree: null,
    selectedUnitId: null,
    events: [],
    skills: [],
    selectedSkill: null,
    skillFiles: [],
    skillContents: {},
    selectedSkillFile: "",
    tools: [],
    selectedTool: null,
};

const U = {
    nav: [...document.querySelectorAll(".nav-item")],
    theme: document.getElementById("theme-toggle"),
    noticeList: document.getElementById("global-notice-list"),
    noticeBadge: document.getElementById("notice-badge"),
    ceoFeed: document.getElementById("ceo-chat-feed"),
    ceoInput: document.getElementById("ceo-input"),
    ceoSend: document.getElementById("ceo-send-btn"),
    viewCeo: document.getElementById("view-ceo"),
    viewProjects: document.getElementById("view-projects-list"),
    viewSkills: document.getElementById("view-skills"),
    viewTools: document.getElementById("view-tools"),
    viewProjectDetails: document.getElementById("view-project-details"),
    modelHint: document.getElementById("sidebar-model-hint"),
    modelCeo: document.getElementById("sidebar-model-ceo"),
    modelExecution: document.getElementById("sidebar-model-execution"),
    modelInspection: document.getElementById("sidebar-model-inspection"),
    modelSave: document.getElementById("sidebar-model-save-btn"),
    modelReset: document.getElementById("sidebar-model-reset-btn"),
    projectGrid: document.getElementById("project-card-grid"),
    projectSummary: document.getElementById("project-selection-summary"),
    selectActive: document.getElementById("project-select-all-active"),
    selectBlocked: document.getElementById("project-select-all-blocked"),
    selectClear: document.getElementById("project-select-clear"),
    pauseBatch: document.getElementById("project-batch-pause"),
    resumeBatch: document.getElementById("project-batch-resume"),
    backToProjects: document.getElementById("back-to-projects"),
    pdTitle: document.getElementById("pd-title"),
    pdStatus: document.getElementById("pd-status"),
    pdSummary: document.getElementById("pd-summary"),
    pdActiveCount: document.getElementById("pd-active-count"),
    tree: document.getElementById("org-tree-container"),
    feed: document.getElementById("project-event-feed"),
    feedTitle: document.getElementById("feed-target-name"),
    detail: document.getElementById("agent-detail-view"),
    adRole: document.getElementById("ad-role"),
    adStatus: document.getElementById("ad-status"),
    adObjective: document.getElementById("ad-objective"),
    adPrompt: document.getElementById("ad-prompt"),
    adResult: document.getElementById("ad-result"),
    adLogs: document.getElementById("ad-logs"),
    closeAgent: document.getElementById("close-agent-btn"),
    skillSearch: document.getElementById("skill-search-input"),
    skillRisk: document.getElementById("skill-risk-filter"),
    skillStatus: document.getElementById("skill-status-filter"),
    skillLegacy: document.getElementById("skill-legacy-filter"),
    skillList: document.getElementById("skill-list"),
    skillEmpty: document.getElementById("skill-detail-empty"),
    skillDetail: document.getElementById("skill-detail-content"),
    skillRefresh: document.getElementById("skill-refresh-btn"),
    skillSave: document.getElementById("skill-save-btn"),
    toolSearch: document.getElementById("tool-search-input"),
    toolStatus: document.getElementById("tool-status-filter"),
    toolRisk: document.getElementById("tool-risk-filter"),
    toolList: document.getElementById("tool-list"),
    toolEmpty: document.getElementById("tool-detail-empty"),
    toolDetail: document.getElementById("tool-detail-content"),
    toolRefresh: document.getElementById("tool-refresh-btn"),
    toolSave: document.getElementById("tool-save-btn"),
};

const esc = (v) => String(v ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;").replaceAll('"', "&quot;").replaceAll("'", "&#39;");
const icons = () => window.lucide && lucide.createIcons();
const roleKey = (v) => (["ceo", "inspection", "checker"].includes(String(v).toLowerCase()) ? (String(v).toLowerCase() === "ceo" ? "ceo" : "inspection") : "execution");
const roleLabel = (v) => ({ ceo: "CEO", execution: "执行", inspection: "检验" }[roleKey(v)]);
const pStatus = (v) => String(v || "").trim().toLowerCase();
const canPause = (v) => ["queued", "planning", "running", "checking"].includes(pStatus(v));
const canResume = (v) => pStatus(v) === "blocked";

function hint(text, err = false) {
    U.modelHint.textContent = text;
    U.modelHint.style.color = err ? "var(--danger, #ff6b6b)" : "";
}

function addMsg(text, role) {
    const el = document.createElement("div");
    el.className = `message ${role}`;
    el.innerHTML = `<div class="avatar"><i data-lucide="${role === "system" ? "cpu" : "user"}"></i></div><div class="msg-content">${esc(text)}</div>`;
    U.ceoFeed.appendChild(el);
    icons();
    U.ceoFeed.scrollTop = U.ceoFeed.scrollHeight;
}

function addNotice(notice, bump = true) {
    const li = document.createElement("li");
    li.className = `notice-item ${String(notice.kind || "").includes("fail") ? "error" : "success"}`;
    li.innerHTML = `<div class="notice-title">${esc(notice.title || "系统通知")}</div><div class="notice-text">${esc(notice.text || "")}</div>`;
    U.noticeList.prepend(li);
    if (bump) U.noticeBadge.textContent = String(Number(U.noticeBadge.textContent || 0) + 1);
}

function renderModelCatalog() {
    const controls = { ceo: U.modelCeo, execution: U.modelExecution, inspection: U.modelInspection };
    const items = S.modelCatalog.items;
    const defaults = S.modelCatalog.defaults;
    const disabled = S.modelCatalog.loading || S.modelCatalog.saving || !items.length;
    Object.entries(controls).forEach(([key, select]) => {
        select.innerHTML = "";
        const base = document.createElement("option");
        base.value = "";
        base.textContent = `使用当前默认 (${defaults[key] || "未配置"})`;
        select.appendChild(base);
        items.forEach((m) => {
            const option = document.createElement("option");
            option.value = m;
            option.textContent = m;
            select.appendChild(option);
        });
        select.disabled = disabled;
    });
    if (S.modelCatalog.loading) return hint("正在加载全局默认模型...");
    if (S.modelCatalog.saving) return hint("正在保存全局默认模型...");
    if (S.modelCatalog.error) return hint(`模型配置错误：${S.modelCatalog.error}`, true);
    hint(items.length ? "修改后点击“保存默认模型”生效。" : "当前没有可用模型。");
}

async function loadModels() {
    S.modelCatalog.loading = true;
    S.modelCatalog.error = "";
    renderModelCatalog();
    try {
        const data = await ApiClient.getOrgGraphModels();
        S.modelCatalog.items = data.items || [];
        S.modelCatalog.defaults = data.defaults || { ceo: "", execution: "", inspection: "" };
    } catch (e) {
        S.modelCatalog.error = e.message || "load failed";
    } finally {
        S.modelCatalog.loading = false;
        renderModelCatalog();
    }
}

function resetModels() {
    [U.modelCeo, U.modelExecution, U.modelInspection].forEach((el) => { el.value = ""; });
    hint("已重置为当前默认值，尚未保存。");
}

async function saveModels() {
    const body = {};
    [["ceo", U.modelCeo], ["execution", U.modelExecution], ["inspection", U.modelInspection]].forEach(([k, el]) => {
        const value = String(el.value || "").trim();
        if (value) body[k] = value;
    });
    S.modelCatalog.saving = true;
    renderModelCatalog();
    try {
        const data = await ApiClient.updateOrgGraphModelDefaults(body);
        S.modelCatalog.items = data.items || S.modelCatalog.items;
        S.modelCatalog.defaults = data.defaults || S.modelCatalog.defaults;
        resetModels();
        hint("全局默认模型已保存。");
    } catch (e) {
        S.modelCatalog.error = e.message || "save failed";
    } finally {
        S.modelCatalog.saving = false;
        renderModelCatalog();
    }
}

async function loadNotices() {
    try {
        const notices = await ApiClient.getNotices(0, 50);
        U.noticeList.innerHTML = "";
        let count = 0;
        notices.forEach((n) => { addNotice(n, false); if (!n.acknowledged) count += 1; });
        U.noticeBadge.textContent = String(count);
    } catch (e) {
        console.error("加载通知失败:", e);
    }
}

function initCeoWs() {
    if (S.ceoWs && S.ceoWs.readyState <= 1) return;
    S.ceoWs = new WebSocket(ApiClient.getCeoWsUrl());
    S.ceoWs.onmessage = (ev) => {
        const payload = JSON.parse(ev.data);
        if (payload.type === "ceo.reply.final") addMsg(payload.data?.text || "", "system");
        if (payload.type === "project.notice") addNotice(payload.data || {});
    };
    S.ceoWs.onclose = () => window.setTimeout(() => S.view === "ceo" && initCeoWs(), 1000);
}

function sendCeoMessage() {
    const text = String(U.ceoInput.value || "").trim();
    if (!text) return;
    addMsg(text, "user");
    U.ceoInput.value = "";
    if (!S.ceoWs || S.ceoWs.readyState !== WebSocket.OPEN) {
        addMsg("连接尚未建立，请稍后重试。", "system");
        initCeoWs();
        return;
    }
    S.ceoWs.send(JSON.stringify({ type: "client.user_message", session_id: "web:shared", text }));
}

function syncProjectSelection() {
    const ids = new Set(S.projects.map((p) => p.project_id));
    [...S.selectedProjects].forEach((id) => !ids.has(id) && S.selectedProjects.delete(id));
}

function updateProjectToolbar() {
    const selected = S.projects.filter((p) => S.selectedProjects.has(p.project_id));
    U.projectSummary.textContent = `已选择 ${selected.length} 项`;
    U.pauseBatch.disabled = S.projectBusy || !selected.some((p) => canPause(p.status));
    U.resumeBatch.disabled = S.projectBusy || !selected.some((p) => canResume(p.status));
}

function renderProjects() {
    U.projectGrid.innerHTML = "";
    if (!S.projects.length) {
        U.projectGrid.innerHTML = '<div class="empty-state" style="grid-column: 1/-1;">当前没有项目。</div>';
        return updateProjectToolbar();
    }
    S.projects.forEach((p) => {
        const selected = S.selectedProjects.has(p.project_id);
        const el = document.createElement("div");
        el.className = `project-card${selected ? " is-selected" : ""}`;
        el.innerHTML = `
            <div class="pc-topbar">
                <label class="project-select-toggle"><input type="checkbox" class="project-select-checkbox" ${selected ? "checked" : ""}><span>选择</span></label>
                <span class="status-badge" data-status="${esc(p.status)}">${esc(String(p.status || "").toUpperCase())}</span>
            </div>
            <div class="pc-header"><div><h3 class="pc-title">${esc(p.title)}</h3><span class="pc-id">${esc(p.project_id)}</span></div></div>
            <div class="pc-summary">${esc(p.summary || "暂无摘要")}</div>
            <div class="pc-stats">${esc(String(p.active_unit_count || 0))} 个活动单元</div>
            <div class="pc-actions">
                <button class="project-action-btn warn" type="button" data-action="pause" ${canPause(p.status) ? "" : "disabled"}>暂停</button>
                <button class="project-action-btn success" type="button" data-action="resume" ${canResume(p.status) ? "" : "disabled"}>恢复</button>
            </div>
        `;
        el.querySelector(".project-select-checkbox")?.addEventListener("change", (e) => {
            if (e.target.checked) S.selectedProjects.add(p.project_id);
            else S.selectedProjects.delete(p.project_id);
            renderProjects();
        });
        el.querySelectorAll(".project-action-btn").forEach((btn) => btn.addEventListener("click", async (e) => {
            e.stopPropagation();
            await runProjectAction(p.project_id, btn.dataset.action);
        }));
        el.addEventListener("click", () => openProject(p.project_id));
        U.projectGrid.appendChild(el);
    });
    updateProjectToolbar();
    icons();
}

async function loadProjects() {
    U.projectGrid.innerHTML = '<div class="empty-state" style="grid-column: 1/-1;">正在加载项目列表...</div>';
    try {
        S.projects = await ApiClient.getProjects(0, 50);
        syncProjectSelection();
        renderProjects();
    } catch (e) {
        U.projectGrid.innerHTML = `<div class="empty-state error" style="grid-column: 1/-1;">加载项目失败：${esc(e.message)}</div>`;
    }
}

async function runProjectAction(projectId, action) {
    S.projectBusy = true;
    updateProjectToolbar();
    try {
        if (action === "pause") await ApiClient.pauseProject(projectId);
        if (action === "resume") await ApiClient.resumeProject(projectId);
        await loadProjects();
    } catch (e) {
        addMsg(`项目操作失败：${projectId} - ${e.message}`, "system");
    } finally {
        S.projectBusy = false;
        updateProjectToolbar();
    }
}

function resetProjectView() {
    S.tree = null;
    S.selectedUnitId = null;
    S.events = [];
    U.tree.innerHTML = '<div class="empty-state">等待获取组织结构...</div>';
    U.feed.innerHTML = "";
    U.feedTitle.textContent = "项目全局动态 / 详情";
    hideAgent();
}

function eventType(evt) {
    const name = String(evt.event_name || "");
    if (name.startsWith("checker.")) return "checker";
    if (name.startsWith("tool.")) return "tool";
    if (name.startsWith("stage.")) return "stage";
    if (name.startsWith("unit.")) return "unit";
    return "info";
}

function eventCard(evt) {
    const type = eventType(evt);
    const icon = { unit: "user", tool: "wrench", stage: "git-commit", checker: "shield-check", info: "hash" }[type];
    const el = document.createElement("div");
    el.className = `feed-card type-${type}`;
    el.innerHTML = `<div class="card-header"><span style="display:flex; gap:4px; align-items:center;"><i data-lucide="${icon}" style="width:14px;height:14px;"></i>${esc(type.toUpperCase())}</span><span>${esc(new Date(evt.created_at || Date.now()).toLocaleTimeString())}</span></div><div class="card-title">${esc(evt.event_name || "project.event")}</div><div class="card-content">${esc(evt.text || "")}</div>`;
    return el;
}

function renderFeed() {
    U.feed.innerHTML = "";
    const rows = S.selectedUnitId ? S.events.filter((evt) => String(evt.unit_id || "") === S.selectedUnitId || !evt.unit_id) : S.events;
    rows.forEach((evt) => U.feed.appendChild(eventCard(evt)));
    icons();
}

function patchTreeUnit(unit) {
    const visit = (node) => {
        if (!node) return false;
        if (node.unit_id === unit.unit_id) { Object.assign(node, unit); return true; }
        return Array.isArray(node.children) && node.children.some(visit);
    };
    visit(S.tree);
}

function renderTree() {
    if (!S.tree) return;
    const levels = [];
    const walk = (node, depth) => {
        if (!levels[depth]) levels[depth] = [];
        levels[depth].push(node);
        (node.children || []).forEach((child) => walk(child, depth + 1));
    };
    walk(S.tree, 0);
    const wrapper = document.createElement("div");
    wrapper.className = "tree-flow-wrapper";
    levels.forEach((level) => {
        const col = document.createElement("div");
        col.className = "tree-level";
        level.forEach((node) => {
            const wrap = document.createElement("div");
            wrap.className = `flow-node-wrapper${node.children?.length ? " has-children" : ""} line-status-${esc(node.status)}`;
            const el = document.createElement("div");
            el.className = `tree-node${S.selectedUnitId === node.unit_id ? " selected" : ""}`;
            el.dataset.id = node.unit_id;
            el.dataset.role = node.role_title || roleLabel(node.role_kind);
            el.dataset.status = node.status;
            el.dataset.objective = node.objective_summary || "";
            el.dataset.prompt = node.prompt_preview || "";
            el.dataset.result = node.result_summary || node.error_summary || "暂无结果";
            el.innerHTML = `<div class="node-header"><span class="node-title">${esc(node.role_title || roleLabel(node.role_kind))}</span><span class="status-badge" data-status="${esc(node.status)}">${esc(String(node.status || "").toUpperCase())}</span></div><div class="node-desc">${esc(node.objective_summary || roleLabel(node.role_kind))}</div>`;
            el.addEventListener("click", (e) => { e.stopPropagation(); S.selectedUnitId = node.unit_id; showAgent(el.dataset); renderTree(); renderFeed(); });
            wrap.appendChild(el);
            col.appendChild(wrap);
        });
        wrapper.appendChild(col);
    });
    U.tree.innerHTML = "";
    U.tree.appendChild(wrapper);
}

function showAgent(data) {
    U.detail.style.display = "flex";
    U.adRole.textContent = data.role || "执行";
    U.adStatus.textContent = String(data.status || "").toUpperCase();
    U.adStatus.dataset.status = data.status || "";
    U.adObjective.textContent = data.objective || "-";
    U.adPrompt.textContent = data.prompt || "-";
    U.adResult.textContent = data.result || "-";
    U.feedTitle.textContent = `限定视角: ${data.role || "执行"}`;
    const logs = S.events.filter((evt) => String(evt.unit_id || "") === String(data.id || ""));
    U.adLogs.innerHTML = logs.length ? "" : '<div class="empty-state" style="padding: 10px;">暂无动作日志...</div>';
    logs.forEach((evt) => U.adLogs.appendChild(eventCard(evt)));
    icons();
}

function hideAgent() {
    if (U.detail) U.detail.style.display = "none";
}

async function openProject(projectId) {
    S.currentProjectId = projectId;
    switchView("project-details");
    resetProjectView();
    if (S.projectWs) { S.projectWs.close(); S.projectWs = null; }
    try {
        const [meta, tree, events] = await Promise.all([ApiClient.getProjectDetails(projectId), ApiClient.getProjectTree(projectId), ApiClient.getProjectEvents(projectId, 0, 200)]);
        if (meta.project) {
            U.pdTitle.textContent = meta.project.title;
            U.pdStatus.textContent = String(meta.project.status || "").toUpperCase();
            U.pdStatus.dataset.status = meta.project.status || "";
            U.pdSummary.textContent = meta.project.summary || "暂无摘要";
            U.pdActiveCount.textContent = String(meta.project.active_unit_count || 0);
        }
        if (tree.root) { S.tree = tree.root; renderTree(); }
        S.events = Array.isArray(events) ? events : [];
        renderFeed();
        const afterSeq = S.events.reduce((max, evt) => Math.max(max, Number(evt.seq || 0)), 0);
        S.projectWs = new WebSocket(ApiClient.getProjectWsUrl(projectId, afterSeq));
        S.projectWs.onmessage = (ev) => handleProjectEvent(JSON.parse(ev.data));
    } catch (e) {
        U.tree.innerHTML = `<div class="empty-state error">初始化项目失败：${esc(e.message)}</div>`;
    }
}

function handleProjectEvent(payload) {
    if (payload.type === "snapshot.project") {
        U.pdTitle.textContent = payload.data.title;
        U.pdStatus.textContent = String(payload.data.status || "").toUpperCase();
        U.pdStatus.dataset.status = payload.data.status || "";
        U.pdSummary.textContent = payload.data.summary || "暂无摘要";
        U.pdActiveCount.textContent = String(payload.data.active_unit_count || 0);
        return;
    }
    if (payload.type === "snapshot.tree") {
        S.tree = payload.data;
        return renderTree();
    }
    if (payload.type === "project.event") {
        S.events.push(payload.data);
        if (["unit.created", "unit.updated", "unit.completed", "unit.failed"].includes(payload.data?.event_name)) {
            const unit = payload.data?.data?.unit;
            if (unit && S.tree) patchTreeUnit(unit);
            renderTree();
        }
        return renderFeed();
    }
    if (payload.type === "artifact.created") {
        S.events.push({ event_name: "artifact.created", text: payload.data?.title || "新产物", created_at: new Date().toISOString(), seq: Date.now() });
        return renderFeed();
    }
    if (payload.type === "project.finished") {
        S.events.push({ event_name: "project.finished", text: payload.data?.final_result || "项目执行完成", created_at: new Date().toISOString(), seq: Date.now() });
        return renderFeed();
    }
}

function filterSkills() {
    const q = String(U.skillSearch.value || "").trim().toLowerCase();
    return S.skills.filter((skill) => {
        if (q && !`${skill.skill_id} ${skill.display_name} ${skill.source_path}`.toLowerCase().includes(q)) return false;
        if (U.skillRisk.value !== "all" && skill.risk_level !== U.skillRisk.value) return false;
        if (U.skillStatus.value === "enabled" && !skill.enabled) return false;
        if (U.skillStatus.value === "disabled" && skill.enabled) return false;
        if (U.skillStatus.value === "unavailable" && skill.available) return false;
        if (U.skillLegacy.value === "legacy" && !skill.legacy) return false;
        if (U.skillLegacy.value === "standard" && skill.legacy) return false;
        return true;
    });
}

function renderSkills() {
    U.skillList.innerHTML = "";
    const items = filterSkills();
    if (!items.length) return void (U.skillList.innerHTML = '<div class="empty-state">没有匹配的 Skill。</div>');
    items.forEach((skill) => {
        const el = document.createElement("button");
        el.type = "button";
        el.className = `resource-list-item${S.selectedSkill?.skill_id === skill.skill_id ? " selected" : ""}`;
        el.innerHTML = `<div class="resource-list-title">${esc(skill.display_name)}</div><div class="resource-list-subtitle">${esc(skill.skill_id)}</div><div class="resource-list-meta">${esc(skill.risk_level)} · ${skill.enabled ? "已启用" : "已禁用"}</div>`;
        el.addEventListener("click", () => openSkill(skill.skill_id));
        U.skillList.appendChild(el);
    });
}

function renderSkillDetail() {
    if (!S.selectedSkill) { U.skillEmpty.style.display = "block"; U.skillDetail.innerHTML = ""; return; }
    U.skillEmpty.style.display = "none";
    const roles = ["ceo", "execution", "inspection"];
    U.skillDetail.innerHTML = `<article class="resource-detail-card"><div class="panel-header"><div><h2>${esc(S.selectedSkill.display_name)}</h2><p class="subtitle">${esc(S.selectedSkill.skill_id)}</p></div></div><label class="role-toggle checked"><input id="skill-enabled" type="checkbox" ${S.selectedSkill.enabled ? "checked" : ""}><span>已启用</span></label><div class="resource-section"><h3>允许角色</h3><div class="resource-filter-row">${roles.map((r) => `<label class="role-toggle ${S.selectedSkill.allowed_roles.includes(r) ? "checked" : ""}"><input type="checkbox" class="skill-role" data-role="${r}" ${S.selectedSkill.allowed_roles.includes(r) ? "checked" : ""}><span>${esc(roleLabel(r))}</span></label>`).join("")}</div></div><div class="resource-section"><h3>可编辑文件</h3><div class="resource-filter-row">${S.skillFiles.map((f) => `<button type="button" class="toolbar-btn ghost skill-file ${S.selectedSkillFile === f.file_key ? "active" : ""}" data-file="${esc(f.file_key)}">${esc(f.file_key)}</button>`).join("")}</div><textarea id="skill-editor" rows="18" class="resource-editor">${esc(S.skillContents[S.selectedSkillFile] || "")}</textarea></div></article>`;
    U.skillDetail.querySelector("#skill-enabled")?.addEventListener("change", (e) => { S.selectedSkill.enabled = !!e.target.checked; });
    U.skillDetail.querySelectorAll(".skill-role").forEach((cb) => cb.addEventListener("change", (e) => {
        const set = new Set(S.selectedSkill.allowed_roles || []);
        if (e.target.checked) set.add(e.target.dataset.role); else set.delete(e.target.dataset.role);
        S.selectedSkill.allowed_roles = [...set];
        renderSkillDetail();
    }));
    U.skillDetail.querySelectorAll(".skill-file").forEach((btn) => btn.addEventListener("click", () => {
        const editor = document.getElementById("skill-editor");
        if (editor && S.selectedSkillFile) S.skillContents[S.selectedSkillFile] = editor.value;
        S.selectedSkillFile = btn.dataset.file;
        renderSkillDetail();
    }));
}

async function loadSkills() { S.skills = await ApiClient.getSkills(0, 300); renderSkills(); }

async function openSkill(skillId) {
    const [skill, files] = await Promise.all([ApiClient.getSkill(skillId), ApiClient.getSkillFiles(skillId)]);
    S.selectedSkill = skill;
    S.skillFiles = files;
    S.selectedSkillFile = files[0]?.file_key || "";
    S.skillContents = {};
    await Promise.all(files.map(async (file) => {
        const data = await ApiClient.getSkillFile(skillId, file.file_key);
        S.skillContents[file.file_key] = data.content || "";
    }));
    renderSkills();
    renderSkillDetail();
}

async function saveSkill() {
    if (!S.selectedSkill) return;
    const editor = document.getElementById("skill-editor");
    if (editor && S.selectedSkillFile) S.skillContents[S.selectedSkillFile] = editor.value;
    for (const [key, content] of Object.entries(S.skillContents)) await ApiClient.saveSkillFile(S.selectedSkill.skill_id, key, content);
    await ApiClient.updateSkillPolicy(S.selectedSkill.skill_id, { enabled: !!S.selectedSkill.enabled, allowed_roles: S.selectedSkill.allowed_roles || [] });
    await loadSkills();
    await openSkill(S.selectedSkill.skill_id);
}

function filterTools() {
    const q = String(U.toolSearch.value || "").trim().toLowerCase();
    return S.tools.filter((tool) => {
        if (q && !`${tool.tool_id} ${tool.display_name} ${tool.source_path}`.toLowerCase().includes(q)) return false;
        if (U.toolStatus.value === "enabled" && !tool.enabled) return false;
        if (U.toolStatus.value === "disabled" && tool.enabled) return false;
        if (U.toolStatus.value === "unavailable" && tool.available) return false;
        if (U.toolRisk.value !== "all" && !(tool.actions || []).some((a) => a.risk_level === U.toolRisk.value)) return false;
        return true;
    });
}

function renderTools() {
    U.toolList.innerHTML = "";
    const items = filterTools();
    if (!items.length) return void (U.toolList.innerHTML = '<div class="empty-state">没有匹配的工具族。</div>');
    items.forEach((tool) => {
        const el = document.createElement("button");
        el.type = "button";
        el.className = `resource-list-item${S.selectedTool?.tool_id === tool.tool_id ? " selected" : ""}`;
        el.innerHTML = `<div class="resource-list-title">${esc(tool.display_name)}</div><div class="resource-list-subtitle">${esc(tool.tool_id)}</div><div class="resource-list-meta">${tool.enabled ? "已启用" : "已禁用"} · ${(tool.actions || []).length} 个动作</div>`;
        el.addEventListener("click", () => openTool(tool.tool_id));
        U.toolList.appendChild(el);
    });
}

function renderToolDetail() {
    if (!S.selectedTool) { U.toolEmpty.style.display = "block"; U.toolDetail.innerHTML = ""; return; }
    U.toolEmpty.style.display = "none";
    const roles = ["ceo", "execution", "inspection"];
    U.toolDetail.innerHTML = `<article class="resource-detail-card"><div class="panel-header"><div><h2>${esc(S.selectedTool.display_name)}</h2><p class="subtitle">${esc(S.selectedTool.tool_id)}</p></div></div><label class="role-toggle checked"><input id="tool-enabled" type="checkbox" ${S.selectedTool.enabled ? "checked" : ""}><span>已启用</span></label><div class="resource-section"><h3>动作矩阵</h3><div class="matrix-table"><table><thead><tr><th>动作</th><th>风险</th>${roles.map((r) => `<th>${esc(roleLabel(r))}</th>`).join("")}</tr></thead><tbody>${(S.selectedTool.actions || []).map((a) => `<tr><td>${esc(a.label || a.action_id)}</td><td>${esc(a.risk_level || "medium")}</td>${roles.map((r) => `<td><input type="checkbox" class="tool-role" data-action="${esc(a.action_id)}" data-role="${r}" ${a.allowed_roles?.includes(r) ? "checked" : ""}></td>`).join("")}</tr>`).join("")}</tbody></table></div></div></article>`;
    U.toolDetail.querySelector("#tool-enabled")?.addEventListener("change", (e) => { S.selectedTool.enabled = !!e.target.checked; });
    U.toolDetail.querySelectorAll(".tool-role").forEach((cb) => cb.addEventListener("change", (e) => {
        const action = S.selectedTool.actions.find((item) => item.action_id === e.target.dataset.action);
        if (!action) return;
        const set = new Set(action.allowed_roles || []);
        if (e.target.checked) set.add(e.target.dataset.role); else set.delete(e.target.dataset.role);
        action.allowed_roles = [...set];
    }));
}

async function loadTools() { S.tools = await ApiClient.getTools(0, 300); renderTools(); }
async function openTool(toolId) { S.selectedTool = await ApiClient.getTool(toolId); renderTools(); renderToolDetail(); }
async function saveTool() {
    if (!S.selectedTool) return;
    await ApiClient.updateToolPolicy(S.selectedTool.tool_id, { enabled: !!S.selectedTool.enabled, actions: (S.selectedTool.actions || []).map((a) => ({ action_id: a.action_id, allowed_roles: a.allowed_roles || [] })) });
    await loadTools();
    await openTool(S.selectedTool.tool_id);
}

function toggleTheme() {
    const html = document.documentElement;
    const dark = html.getAttribute("data-theme") === "dark";
    html.setAttribute("data-theme", dark ? "light" : "dark");
    const darkIcon = U.theme.querySelector(".dark-icon");
    const lightIcon = U.theme.querySelector(".light-icon");
    if (darkIcon && lightIcon) { darkIcon.style.display = dark ? "none" : "block"; lightIcon.style.display = dark ? "block" : "none"; }
}

function switchView(view) {
    const map = { ceo: U.viewCeo, projects: U.viewProjects, skills: U.viewSkills, tools: U.viewTools, "project-details": U.viewProjectDetails };
    const navView = view === "project-details" ? "projects" : view;
    U.nav.forEach((btn) => btn.classList.toggle("active", btn.dataset.view === navView));
    Object.entries(map).forEach(([key, el]) => { if (el) el.style.display = key === view ? (key === "ceo" || key === "project-details" ? "flex" : "block") : "none"; });
    if (view !== "project-details" && S.projectWs) { S.projectWs.close(); S.projectWs = null; }
    S.view = view;
    if (view === "projects") void loadProjects();
    if (view === "skills") void loadSkills();
    if (view === "tools") void loadTools();
}

function bind() {
    U.theme?.addEventListener("click", toggleTheme);
    U.nav.forEach((btn) => btn.addEventListener("click", () => switchView(btn.dataset.view)));
    U.backToProjects?.addEventListener("click", () => switchView("projects"));
    U.ceoSend?.addEventListener("click", sendCeoMessage);
    U.ceoInput?.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendCeoMessage(); } });
    U.modelSave?.addEventListener("click", () => void saveModels());
    U.modelReset?.addEventListener("click", resetModels);
    U.selectActive?.addEventListener("click", () => { S.selectedProjects = new Set(S.projects.filter((p) => canPause(p.status)).map((p) => p.project_id)); renderProjects(); });
    U.selectBlocked?.addEventListener("click", () => { S.selectedProjects = new Set(S.projects.filter((p) => canResume(p.status)).map((p) => p.project_id)); renderProjects(); });
    U.selectClear?.addEventListener("click", () => { S.selectedProjects.clear(); renderProjects(); });
    U.pauseBatch?.addEventListener("click", async () => { for (const id of [...S.selectedProjects]) await runProjectAction(id, "pause"); });
    U.resumeBatch?.addEventListener("click", async () => { for (const id of [...S.selectedProjects]) await runProjectAction(id, "resume"); });
    U.closeAgent?.addEventListener("click", () => { S.selectedUnitId = null; U.feedTitle.textContent = "项目全局动态 / 详情"; hideAgent(); renderTree(); renderFeed(); });
    [U.skillSearch, U.skillRisk, U.skillStatus, U.skillLegacy].forEach((el) => el?.addEventListener(el.tagName === "INPUT" ? "input" : "change", renderSkills));
    U.skillRefresh?.addEventListener("click", () => void loadSkills());
    U.skillSave?.addEventListener("click", () => void saveSkill());
    [U.toolSearch, U.toolStatus, U.toolRisk].forEach((el) => el?.addEventListener(el.tagName === "INPUT" ? "input" : "change", renderTools));
    U.toolRefresh?.addEventListener("click", () => void loadTools());
    U.toolSave?.addEventListener("click", () => void saveTool());
}

function init() {
    bind();
    icons();
    void loadModels();
    void loadNotices();
    initCeoWs();
}

document.addEventListener("DOMContentLoaded", init);
