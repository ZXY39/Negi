import sys

with open('d:/zxy/project/G3ku/g3ku/web/frontend/org_graph_app.js', 'r', encoding='utf-8') as f:
    text = f.read()

# Fix 1: The broken 'renderTools' loop and 'toggleTheme' and start of 'renderSkillDetail'
broken1 = """        el    U.skillDetail.innerHTML = `<article class="resource-detail-card"><div class="panel-header" style="display:flex; justify-content:space-between; align-items:flex-start;"><div><h2>${esc(S.selectedSkill.display_name)}</h2><p class="subtitle">${esc(S.selectedSkill.skill_id)}</p></div><div style="display:flex; gap:8px;"><button type="button" class="toolbar-btn ghost" id="skill-modal-close" style="padding: 4px 8px;">关闭</button><button type="button" class="toolbar-btn success" id="skill-modal-save" style="padding: 4px 16px;">保存</button></div></div><label class="role-toggle checked"><input id="skill-enabled" type="checkbox" ${S.selectedSkill.enabled ? "checked" : ""}><span>启用该技能</span></label><div class="resource-section"><h3>允许的角色</h3><div class="resource-filter-row">${roles.map((r) => `<label class="role-toggle ${allowedRoles.includes(r) ? "checked" : ""}"><input type="checkbox" class="skill-role" data-role="${r}" ${allowedRoles.includes(r) ? "checked" : ""}><span>${esc(roleLabel(r))}</span></label>`).join("")}</div></div><div class="resource-section"><h3>可编辑文件</h3><div class="resource-filter-row">${S.skillFiles.map((f) => `<button type="button" class="toolbar-btn ghost skill-file ${S.selectedSkillFile === f.file_key ? "active" : ""}" data-file="${esc(f.file_key)}">${esc(f.file_key)}</button>`).join("")}</div><textarea id="skill-editor" rows="18" class="resource-editor">${esc(S.skillContents[S.selectedSkillFile] || "")}</textarea></div></article>`;
    U.skillDetail.querySelector("#skill-modal-close")?.addEventListener("click", () => clearSkillSelection());
    U.skillDetail.querySelector("#skill-modal-save")?.addEventListener("click", () => saveSkill());
    U.skillDetail.querySelector("#skill-enabled")?.addEventListener("change", (e) => { S.selectedSkill.enabled = !!e.target.checked; queueResourceSave("skill"); });etDrawerOpen(U.skillBackdrop, U.skillDrawer, true);"""

fix1 = """        el.type = "button";
        el.className = `resource-list-item${S.selectedTool?.tool_id === tool.tool_id ? " selected" : ""}`;
        el.innerHTML = `<div class="resource-list-title">${esc(tool.display_name)}</div><div class="resource-list-subtitle">${esc(tool.tool_id)}</div><div class="resource-list-meta">${tool.enabled ? "已启用" : "已禁用"} · ${(tool.actions || []).length} 个动作</div>`;
        el.addEventListener("click", () => openTool(tool.tool_id));
        U.toolList.appendChild(el);
    });
}

function toggleTheme() {
    const html = document.documentElement;
    const dark = html.getAttribute("data-theme") === "dark";
    html.setAttribute("data-theme", dark ? "light" : "dark");
    const darkIcon = U.theme.querySelector(".dark-icon");
    const lightIcon = U.theme.querySelector(".light-icon");
    if (darkIcon && lightIcon) { darkIcon.style.display = dark ? "none" : "block"; lightIcon.style.display = dark ? "block" : "none"; }
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
    setDrawerOpen(U.skillBackdrop, U.skillDrawer, true);"""

text = text.replace(broken1, fix1)

# Fix 2: The innerHTML for skill detail correctly replacing the '???' stuff
broken2 = """    U.skillDetail.innerHTML = `<article class="resource-detail-card"><div class="panel-header"><div><h2>${esc(S.selectedSkill.display_name)}</h2><p class="subtitle">${esc(S.selectedSkill.skill_id)}</p></div></div><label class="role-toggle checked"><input id="skill-enabled" type="checkbox" ${S.selectedSkill.enabled ? "checked" : ""}><span>???</span></label><div class="resource-section"><h3>????</h3><div class="resource-filter-row">${roles.map((r) => `<label class="role-toggle ${allowedRoles.includes(r) ? "checked" : ""}"><input type="checkbox" class="skill-role" data-role="${r}" ${allowedRoles.includes(r) ? "checked" : ""}><span>${esc(roleLabel(r))}</span></label>`).join("")}</div></div><div class="resource-section"><h3>?????</h3><div class="resource-filter-row">${S.skillFiles.map((f) => `<button type="button" class="toolbar-btn ghost skill-file ${S.selectedSkillFile === f.file_key ? "active" : ""}" data-file="${esc(f.file_key)}">${esc(f.file_key)}</button>`).join("")}</div><textarea id="skill-editor" rows="18" class="resource-editor">${esc(S.skillContents[S.selectedSkillFile] || "")}</textarea></div></article>`;
    U.skillDetail.querySelector("#skill-enabled")?.addEventListener("change", (e) => { S.selectedSkill.enabled = !!e.target.checked; queueResourceSave("skill"); });"""

fix2 = """    U.skillDetail.innerHTML = `<article class="resource-detail-card"><div class="panel-header" style="display:flex; justify-content:space-between; align-items:flex-start;"><div><h2>${esc(S.selectedSkill.display_name)}</h2><p class="subtitle">${esc(S.selectedSkill.skill_id)}</p></div><div style="display:flex; gap:8px;"><button type="button" class="toolbar-btn ghost" id="skill-modal-close" style="padding: 4px 8px;">关闭</button><button type="button" class="toolbar-btn success" id="skill-modal-save" style="padding: 4px 16px;">保存</button></div></div><label class="role-toggle checked"><input id="skill-enabled" type="checkbox" ${S.selectedSkill.enabled ? "checked" : ""}><span>启用该技能</span></label><div class="resource-section"><h3>允许的角色</h3><div class="resource-filter-row">${roles.map((r) => `<label class="role-toggle ${allowedRoles.includes(r) ? "checked" : ""}"><input type="checkbox" class="skill-role" data-role="${r}" ${allowedRoles.includes(r) ? "checked" : ""}><span>${esc(roleLabel(r))}</span></label>`).join("")}</div></div><div class="resource-section"><h3>可编辑文件</h3><div class="resource-filter-row">${S.skillFiles.map((f) => `<button type="button" class="toolbar-btn ghost skill-file ${S.selectedSkillFile === f.file_key ? "active" : ""}" data-file="${esc(f.file_key)}">${esc(f.file_key)}</button>`).join("")}</div><textarea id="skill-editor" rows="18" class="resource-editor">${esc(S.skillContents[S.selectedSkillFile] || "")}</textarea></div></article>`;
    U.skillDetail.querySelector("#skill-modal-close")?.addEventListener("click", () => clearSkillSelection());
    U.skillDetail.querySelector("#skill-modal-save")?.addEventListener("click", () => saveSkill());
    U.skillDetail.querySelector("#skill-enabled")?.addEventListener("change", (e) => { S.selectedSkill.enabled = !!e.target.checked; queueResourceSave("skill"); });"""

text = text.replace(broken2, fix2)

# Fix 3: The broken 'saveSkill' end
broken3 = """       U.toolDetail.innerHTML = `<article class="resource-detail-card"><div class="panel-header" style="display:flex; justify-content:space-between; align-items:flex-start;"><div><h2>${esc(S.selectedTool.display_name)}</h2><p class="subtitle">${esc(S.selectedTool.tool_id)}</p></div><div style="display:flex; gap:8px;"><button type="button" class="toolbar-btn ghost" id="tool-modal-close" style="padding: 4px 8px;">关闭</button><button type="button" class="toolbar-btn success" id="tool-modal-save" style="padding: 4px 16px;">保存</button></div></div><label class="role-toggle checked"><input id="tool-enabled" type="checkbox" ${S.selectedTool.enabled ? "checked" : ""}><span>启用工具族</span></label><div class="resource-section"><h3>分配动作权限</h3><div class="matrix-table"><table><thead><tr><th>动作</th><th>风险</th>${roles.map((r) => `<th>${esc(roleLabel(r))}</th>`).join("")}</tr></thead><tbody>${(S.selectedTool.actions || []).map((a) => `<tr><td>${esc(a.label || a.action_id)}</td><td>${esc(a.risk_level || "medium")}</td>${roles.map((r) => `<td><input type="checkbox" class="tool-role" data-action="${esc(a.action_id)}" data-role="${r}" ${a.allowed_roles?.includes(r) ? "checked" : ""}></td>`).join("")}</tr>`).join("")}</tbody></table></div></div></article>`;
    U.toolDetail.querySelector("#tool-modal-close")?.addEventListener("click", () => clearToolSelection());
    U.toolDetail.querySelector("#tool-modal-save")?.addEventListener("click", () => saveTool());
    U.toolDetail.querySelector("#tool-enabled")?.addEventListener("change", (e) => { S.selectedTool.enabled = !!e.target.checked; queueResourceSave("tool"); });     S.skillBusy = false;"""

fix3 = """    try {
        const editor = document.getElementById("skill-editor");
        if (editor && S.selectedSkillFile) S.skillContents[S.selectedSkillFile] = editor.value;
        const fileEntries = Object.entries({ ...S.skillContents });
        for (const [key, content] of fileEntries) {
            await ApiClient.saveSkillFile(selectedId, key, content);
        }
        await ApiClient.updateSkillPolicy(selectedId, {
            enabled,
            allowed_roles: allowedRoles,
        });
        await ApiClient.reloadResources();
        await loadSkills();
        await openSkill(selectedId, true);
        addNotice({ kind: "resource_saved", title: "Skill saved", text: displayName || selectedId });
        showToast({ title: "保存成功", text: "Skill 配置已保存", kind: "success" });
    } catch (e) {
        addNotice({ kind: "resource_failed", title: "Skill save failed", text: e.message || "Unknown error" });
        showToast({ title: "保存失败", text: e.message || "Unknown error", kind: "error" });
    } finally {
        S.skillBusy = false;"""

text = text.replace(broken3, fix3)

# Fix 4: Tool detail innerHTML replacement
broken4 = """    U.toolDetail.innerHTML = `<article class="resource-detail-card"><div class="panel-header"><div><h2>${esc(S.selectedTool.display_name)}</h2><p class="subtitle">${esc(S.selectedTool.tool_id)}</p></div></div><label class="role-toggle checked"><input id="tool-enabled" type="checkbox" ${S.selectedTool.enabled ? "checked" : ""}><span>???</span></label><div class="resource-section"><h3>????</h3><div class="matrix-table"><table><thead><tr><th>??</th><th>??</th>${roles.map((r) => `<th>${esc(roleLabel(r))}</th>`).join("")}</tr></thead><tbody>${(S.selectedTool.actions || []).map((a) => `<tr><td>${esc(a.label || a.action_id)}</td><td>${esc(a.risk_level || "medium")}</td>${roles.map((r) => `<td><input type="checkbox" class="tool-role" data-action="${esc(a.action_id)}" data-role="${r}" ${a.allowed_roles?.includes(r) ? "checked" : ""}></td>`).join("")}</tr>`).join("")}</tbody></table></div></div></article>`;
    U.toolDetail.querySelector("#tool-enabled")?.addEventListener("change", (e) => { S.selectedTool.enabled = !!e.target.checked; queueResourceSave("tool"); });"""

fix4 = """    U.toolDetail.innerHTML = `<article class="resource-detail-card"><div class="panel-header" style="display:flex; justify-content:space-between; align-items:flex-start;"><div><h2>${esc(S.selectedTool.display_name)}</h2><p class="subtitle">${esc(S.selectedTool.tool_id)}</p></div><div style="display:flex; gap:8px;"><button type="button" class="toolbar-btn ghost" id="tool-modal-close" style="padding: 4px 8px;">关闭</button><button type="button" class="toolbar-btn success" id="tool-modal-save" style="padding: 4px 16px;">保存</button></div></div><label class="role-toggle checked"><input id="tool-enabled" type="checkbox" ${S.selectedTool.enabled ? "checked" : ""}><span>启用工具族</span></label><div class="resource-section"><h3>分配动作权限</h3><div class="matrix-table"><table><thead><tr><th>动作</th><th>风险</th>${roles.map((r) => `<th>${esc(roleLabel(r))}</th>`).join("")}</tr></thead><tbody>${(S.selectedTool.actions || []).map((a) => `<tr><td>${esc(a.label || a.action_id)}</td><td>${esc(a.risk_level || "medium")}</td>${roles.map((r) => `<td><input type="checkbox" class="tool-role" data-action="${esc(a.action_id)}" data-role="${r}" ${a.allowed_roles?.includes(r) ? "checked" : ""}></td>`).join("")}</tr>`).join("")}</tbody></table></div></div></article>`;
    U.toolDetail.querySelector("#tool-modal-close")?.addEventListener("click", () => clearToolSelection());
    U.toolDetail.querySelector("#tool-modal-save")?.addEventListener("click", () => saveTool());
    U.toolDetail.querySelector("#tool-enabled")?.addEventListener("change", (e) => { S.selectedTool.enabled = !!e.target.checked; queueResourceSave("tool"); });"""

text = text.replace(broken4, fix4)

with open('d:/zxy/project/G3ku/g3ku/web/frontend/org_graph_app.js', 'w', encoding='utf-8') as f:
    f.write(text)
print("done")
