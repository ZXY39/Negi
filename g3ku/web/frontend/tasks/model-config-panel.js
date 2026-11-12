"use strict";

// 任务详情「模型配置」面板：本任务执行/检验两条车道的模型链覆盖。
// 覆盖链与全局角色链同形（direct 模型 + 负载均衡组槽位任意混排）；组定义只读，
// 唯一写点在模型页。草稿与 handler 全部 data-task-model-* 命名空间，绝不进入
// S.modelCatalog.roleEditing、绝不写 roleDrafts/loadBalanceGroupDrafts、绝不调用
// 模型页保存车道——任务面板的任何操作都不反向同步全局。
// 本文件在 org_graph_app.js 之后加载，只读写 S/U 与既有全局入口。

const TASK_MODEL_CONFIG_LANES = [
    { key: "execution", label: "执行 Agent 模型链", entriesKey: "execution_route_entries" },
    { key: "inspection", label: "检验 Agent 模型链", entriesKey: "inspection_route_entries" },
];

function taskModelConfigLaneState() {
    return {
        followsGlobal: true,
        serverEntries: [],
        draftRefs: [],
        editing: false,
        pickerOpen: false,
        pickerTab: "models",
        pickerSearch: "",
        dragFrom: -1,
    };
}

function taskModelConfigState() {
    if (!S.taskModelConfig) {
        S.taskModelConfig = {
            taskId: "",
            open: false,
            loaded: false,
            loading: false,
            saving: false,
            error: "",
            requestToken: 0,
            lanes: {
                execution: taskModelConfigLaneState(),
                inspection: taskModelConfigLaneState(),
            },
        };
    }
    return S.taskModelConfig;
}

function resetTaskModelConfigPanel() {
    const state = taskModelConfigState();
    state.taskId = "";
    state.open = false;
    state.loaded = false;
    state.loading = false;
    state.saving = false;
    state.error = "";
    state.requestToken += 1;
    state.lanes = { execution: taskModelConfigLaneState(), inspection: taskModelConfigLaneState() };
    if (typeof setDrawerOpen === "function") {
        setDrawerOpen(U.taskModelConfigBackdrop, U.taskModelConfigDrawer, false);
    }
    syncTaskModelConfigBadge();
}

// 覆盖链 entry ↔ 链记号（模型 key 或 group:<key>）。转换复用模型页同一个
// chainToRouteEntries，两条写车道的形状因此不可能分叉。
function taskModelConfigEntriesToRefs(entries) {
    return (Array.isArray(entries) ? entries : []).map((entry) => {
        const type = String(entry?.type || "model").trim();
        if (type === "load_balance") return groupRefToken(String(entry?.group_key || entry?.groupKey || "").trim());
        return String(entry?.model_key || entry?.modelKey || "").trim();
    }).filter(Boolean);
}

function taskModelConfigCleanRefs(refs) {
    return (Array.isArray(refs) ? refs : []).map((ref) => String(ref || "").trim()).filter(Boolean);
}

function taskModelConfigRefsToEntries(refs) {
    return chainToRouteEntries(taskModelConfigCleanRefs(refs));
}

function taskModelConfigRefsEqual(left, right) {
    const a = taskModelConfigCleanRefs(left);
    const b = taskModelConfigCleanRefs(right);
    return a.length === b.length && a.every((ref, index) => ref === b[index]);
}

// 全局链的有序记号视图：S.modelCatalog.roles 已由 route_entries 投影成链序
// （含 group: 记号），不是候选展开。读 committed 态，不碰模型页草稿。
function taskModelConfigGlobalRefs(laneKey) {
    const roles = S.modelCatalog && S.modelCatalog.roles ? S.modelCatalog.roles : null;
    return taskModelConfigCleanRefs(Array.isArray(roles?.[laneKey]) ? roles[laneKey] : []);
}

function taskModelConfigCommittedGroups() {
    return (S.modelCatalog && S.modelCatalog.loadBalanceGroups) || {};
}

function taskModelConfigLaneDisplayRefs(laneKey) {
    const lane = taskModelConfigState().lanes[laneKey];
    if (lane.editing) return taskModelConfigCleanRefs(lane.draftRefs);
    if (!lane.followsGlobal) return taskModelConfigEntriesToRefs(lane.serverEntries);
    return taskModelConfigGlobalRefs(laneKey);
}

// 保存载荷：每车道算出目标 entries，与服务端态相同的车道不发。
// 单独配置态下草稿与全局链逐位相同 ⇒ 发空数组恢复跟随（会话面板同一口径）。
function taskModelConfigSavePayload() {
    const state = taskModelConfigState();
    const payload = {};
    TASK_MODEL_CONFIG_LANES.forEach(({ key, entriesKey }) => {
        const lane = state.lanes[key];
        let target = [];
        if (lane.editing) {
            target = taskModelConfigRefsEqual(lane.draftRefs, taskModelConfigGlobalRefs(key))
                ? []
                : taskModelConfigRefsToEntries(lane.draftRefs);
        }
        if (JSON.stringify(target) !== JSON.stringify(lane.serverEntries || [])) {
            payload[entriesKey] = target;
        }
    });
    return payload;
}

function applyTaskModelConfigPayload(payload, { keepDraft = false } = {}) {
    const state = taskModelConfigState();
    const data = payload && typeof payload === "object" ? payload : {};
    TASK_MODEL_CONFIG_LANES.forEach(({ key }) => {
        const laneData = data[key] && typeof data[key] === "object" ? data[key] : {};
        const lane = state.lanes[key];
        const entries = Array.isArray(laneData.route_entries) ? laneData.route_entries : [];
        const followsGlobal = laneData.follows_global !== false && !entries.length;
        lane.serverEntries = entries;
        lane.followsGlobal = followsGlobal;
        if (keepDraft && lane.editing) return;
        lane.editing = !followsGlobal;
        lane.draftRefs = taskModelConfigEntriesToRefs(entries);
        lane.pickerOpen = false;
        lane.pickerSearch = "";
        lane.dragFrom = -1;
    });
    state.loaded = true;
    state.loading = false;
    state.error = "";
    // 头部圆点与其他读者按任务快照的 metadata 判覆盖态，这里就地同步，
    // 不等下一次任务快照刷新。
    const task = S.currentTask;
    if (task && typeof task === "object") {
        const metadata = { ...(task.metadata || {}) };
        const config = {};
        TASK_MODEL_CONFIG_LANES.forEach(({ key, entriesKey }) => {
            const entries = state.lanes[key].serverEntries || [];
            if (entries.length) config[entriesKey] = entries;
        });
        if (Object.keys(config).length) metadata.model_config = config;
        else delete metadata.model_config;
        task.metadata = metadata;
    }
    syncTaskModelConfigBadge();
}

async function refreshTaskModelConfig({ force = false } = {}) {
    const state = taskModelConfigState();
    const taskId = String(S.currentTaskId || "").trim();
    if (!taskId) return null;
    if (!force && state.loaded && state.taskId === taskId) return state;
    state.requestToken += 1;
    const token = state.requestToken;
    state.loading = true;
    state.error = "";
    renderTaskModelConfigPanel();
    try {
        const payload = await ApiClient.getTaskModelConfig(taskId);
        if (token !== state.requestToken || String(S.currentTaskId || "").trim() !== taskId) return null;
        const keepDraft = TASK_MODEL_CONFIG_LANES.some(({ key }) => state.lanes[key].editing);
        applyTaskModelConfigPayload(payload, { keepDraft });
        return state;
    } catch (error) {
        if (token !== state.requestToken) return null;
        state.loading = false;
        state.error = String(error?.message || "模型配置加载失败");
        renderTaskModelConfigPanel();
        return null;
    }
}

// 目录正常在 boot 时已加载；失败过一次会让面板退化成裸 key，
// 打开时补一次加载（与 Token 统计窗口同一口径）。
async function ensureTaskModelConfigCatalog() {
    if (S.modelCatalog?.loading || (S.modelCatalog?.catalog || []).length) return;
    try {
        if (typeof window.loadModels === "function") await window.loadModels();
    } catch (error) {
        void error;
        return;
    }
    if (taskModelConfigState().open) renderTaskModelConfigPanel();
}

async function openTaskModelConfig() {
    const taskId = String(S.currentTaskId || "").trim();
    if (!taskId) return;
    const state = taskModelConfigState();
    if (state.taskId !== taskId) {
        resetTaskModelConfigPanel();
        state.taskId = taskId;
    }
    state.open = true;
    if (typeof setDrawerOpen === "function") {
        setDrawerOpen(U.taskModelConfigBackdrop, U.taskModelConfigDrawer, true);
    }
    renderTaskModelConfigPanel();
    void ensureTaskModelConfigCatalog();
    await refreshTaskModelConfig({ force: true });
    renderTaskModelConfigPanel();
}

function closeTaskModelConfig() {
    const state = taskModelConfigState();
    state.open = false;
    if (typeof setDrawerOpen === "function") {
        setDrawerOpen(U.taskModelConfigBackdrop, U.taskModelConfigDrawer, false);
    }
}

function syncTaskModelConfigBadge() {
    const button = U.taskModelConfigButton;
    if (!button) return;
    const config = (S.currentTask && S.currentTask.metadata && S.currentTask.metadata.model_config) || {};
    const hasOverride = TASK_MODEL_CONFIG_LANES.some(({ entriesKey }) => Array.isArray(config[entriesKey]) && config[entriesKey].length);
    button.classList.toggle("has-override", !!hasOverride);
    button.title = hasOverride ? "模型配置：本任务已单独配置模型链" : "模型配置：跟随全局模型链";
}

// ---- 渲染 -----------------------------------------------------------------

function taskModelConfigModelBadges(ref, index) {
    const item = modelRefItem(ref);
    const badges = [];
    if (index === 0) badges.push('<span class="policy-chip risk-low">链首</span>');
    if (!item) badges.push('<span class="policy-chip risk-high">已删除，发送时跳过</span>');
    else if (item.enabled === false) badges.push('<span class="policy-chip risk-high">已禁用，发送时跳过</span>');
    return badges.join("");
}

function taskModelConfigGroupMemberChips(groupKey) {
    const group = taskModelConfigCommittedGroups()[groupKey];
    if (!group) return '<span class="policy-chip risk-high">组已删除，发送时跳过</span>';
    const members = Array.isArray(group.model_keys) ? group.model_keys : [];
    const chips = members.map((member) => {
        const item = modelRefItem(member);
        return `<span class="policy-chip neutral" title="${esc(member)}">${esc(modelDisplayTitle(item) || member)}</span>`;
    }).join("");
    const disabled = group.enabled === false ? '<span class="policy-chip risk-high">组已禁用，槽位不贡献候选</span>' : "";
    return `${chips || '<span class="policy-chip risk-high">成员为空</span>'}${disabled}`;
}

function taskModelConfigTileMarkup(laneKey, ref, index, { editable }) {
    const isGroup = isGroupRef(ref);
    const groupKey = isGroup ? groupKeyFromRef(ref) : "";
    const item = isGroup ? null : modelRefItem(ref);
    const title = isGroup ? groupKey : (modelDisplayTitle(item) || ref);
    const subtitle = isGroup
        ? "组内平级 · 按综合负载选成员 · 组定义在模型页维护"
        : String(item?.provider_model || ref);
    const badges = isGroup
        ? (index === 0 ? '<span class="policy-chip risk-low">链首</span>' : "") + '<span class="policy-chip risk-low">组</span>'
        : taskModelConfigModelBadges(ref, index);
    const members = isGroup ? `<div class="tmc-tile-members">${taskModelConfigGroupMemberChips(groupKey)}</div>` : "";
    const invalid = isGroup
        ? !taskModelConfigCommittedGroups()[groupKey]
        : (!item || item.enabled === false);
    return `
        <article class="tmc-tile${isGroup ? " is-group" : ""}${invalid ? " is-invalid" : ""}"
            ${editable ? `draggable="true" data-task-model-index="${index}"` : ""}
            data-task-model-lane="${esc(laneKey)}" data-task-model-ref="${esc(ref)}">
            ${editable ? '<span class="tmc-grip" aria-hidden="true">&#9776;</span>' : ""}
            <div class="tmc-tile-main">
                <span class="tmc-tile-title">${esc(title)}</span>
                <span class="tmc-tile-sub">${esc(subtitle)}</span>
                <span class="tmc-tile-badges">${badges}</span>
                ${members}
            </div>
            ${editable ? `<button type="button" class="toolbar-btn ghost small" data-task-model-remove="${index}" data-task-model-lane="${esc(laneKey)}">移除</button>` : ""}
        </article>`;
}

function taskModelConfigPickerRows(laneKey) {
    const lane = taskModelConfigState().lanes[laneKey];
    const search = String(lane.pickerSearch || "").trim().toLowerCase();
    if (lane.pickerTab === "groups") {
        const groups = taskModelConfigCommittedGroups();
        const keys = Object.keys(groups).sort((a, b) => String(a).localeCompare(String(b)));
        const rows = keys
            .filter((key) => !search || String(key).toLowerCase().includes(search))
            .map((key) => {
                const group = groups[key] || {};
                const members = (group.model_keys || []).map((member) => esc(modelDisplayTitle(modelRefItem(member)) || member)).join("、");
                const disabled = group.enabled === false ? '<span class="policy-chip risk-high">已禁用</span>' : "";
                return `
                    <button type="button" class="tmc-picker-row" data-task-model-add="${esc(groupRefToken(key))}" data-task-model-lane="${esc(laneKey)}">
                        <span class="tmc-picker-row-title">${esc(key)} ${disabled}</span>
                        <span class="tmc-picker-row-sub">${members || "成员为空"}</span>
                    </button>`;
            }).join("");
        return rows || '<div class="empty-state compact">没有匹配的负载均衡组。组的创建与编辑在模型页。</div>';
    }
    const catalog = (S.modelCatalog && S.modelCatalog.catalog) || [];
    const rows = catalog
        .filter((item) => {
            if (!search) return true;
            return [item?.key, item?.name, item?.provider_model].some((value) => String(value || "").toLowerCase().includes(search));
        })
        .sort((a, b) => String(a?.key || "").localeCompare(String(b?.key || "")))
        .map((item) => {
            const key = String(item?.key || "").trim();
            const disabled = item?.enabled === false;
            return `
                <button type="button" class="tmc-picker-row${disabled ? " is-disabled" : ""}" ${disabled ? "disabled" : ""}
                    data-task-model-add="${esc(key)}" data-task-model-lane="${esc(laneKey)}">
                    <span class="tmc-picker-row-title">${esc(modelDisplayTitle(item) || key)}${disabled ? ' <span class="policy-chip neutral">已禁用</span>' : ""}</span>
                    <span class="tmc-picker-row-sub">${esc(item?.provider_model || key)}</span>
                </button>`;
        }).join("");
    return rows || '<div class="empty-state compact">没有匹配的模型。</div>';
}

function taskModelConfigLaneMarkup(laneKey) {
    const { label } = TASK_MODEL_CONFIG_LANES.find((lane) => lane.key === laneKey);
    const lane = taskModelConfigState().lanes[laneKey];
    const refs = taskModelConfigLaneDisplayRefs(laneKey);
    const editable = lane.editing;
    const globalInspectionEmpty = laneKey === "inspection" && !taskModelConfigGlobalRefs("inspection").length;
    const note = editable
        ? "仅作用于本任务 · 保存后下一个模型调用边界生效"
        : (globalInspectionEmpty
            ? "全局检验链为空：跟随本任务生效的执行链"
            : "跟随全局链，全局变更自动同步");
    const chain = refs.length
        ? refs.map((ref, index) => taskModelConfigTileMarkup(laneKey, ref, index, { editable })).join("")
        : `<div class="empty-state compact">${editable ? "从下方添加模型或负载均衡组。" : "全局链为空。"}</div>`;
    const picker = editable ? `
        <div class="tmc-picker"${lane.pickerOpen ? "" : " hidden"}>
            <div class="tmc-picker-head">
                <div class="tmc-picker-tabs" role="tablist">
                    <button type="button" class="tmc-picker-tab${lane.pickerTab === "models" ? " is-active" : ""}" role="tab"
                        data-task-model-picker-tab="models" data-task-model-lane="${esc(laneKey)}">模型</button>
                    <button type="button" class="tmc-picker-tab${lane.pickerTab === "groups" ? " is-active" : ""}" role="tab"
                        data-task-model-picker-tab="groups" data-task-model-lane="${esc(laneKey)}">负载均衡组</button>
                </div>
                <input type="search" class="tmc-picker-search" placeholder="搜索" autocomplete="off" spellcheck="false"
                    value="${esc(lane.pickerSearch)}" data-task-model-picker-search="${esc(laneKey)}" aria-label="搜索模型或组">
            </div>
            <div class="tmc-picker-list" data-task-model-picker-list="${esc(laneKey)}">${taskModelConfigPickerRows(laneKey)}</div>
        </div>
        <div class="tmc-lane-actions">
            <button type="button" class="toolbar-btn ghost small" data-task-model-picker-toggle="${esc(laneKey)}">${lane.pickerOpen ? "收起添加" : "添加模型 / 组"}</button>
        </div>` : "";
    return `
        <section class="tmc-lane" data-task-model-lane-section="${esc(laneKey)}">
            <div class="tmc-lane-head">
                <div class="tmc-lane-titles">
                    <h3>${esc(label)}</h3>
                    <p class="tmc-lane-note">${esc(note)}</p>
                </div>
                <div class="tmc-mode" role="group" aria-label="${esc(label)}模式">
                    <button type="button" class="tmc-mode-btn${!editable ? " is-active" : ""}"
                        data-task-model-mode="follow" data-task-model-lane="${esc(laneKey)}">跟随全局</button>
                    <button type="button" class="tmc-mode-btn${editable ? " is-active" : ""}"
                        data-task-model-mode="custom" data-task-model-lane="${esc(laneKey)}">单独配置</button>
                </div>
            </div>
            <div class="tmc-chain" data-task-model-chain="${esc(laneKey)}">${chain}</div>
            ${picker}
        </section>`;
}

function syncTaskModelConfigFooter() {
    const state = taskModelConfigState();
    const saveButton = U.taskModelConfigSave;
    const status = U.taskModelConfigStatus;
    const dirty = Object.keys(taskModelConfigSavePayload()).length > 0;
    if (saveButton) saveButton.disabled = !dirty || state.saving || !state.loaded;
    if (status) {
        status.textContent = state.saving ? "正在保存…" : (state.error || (dirty ? "有未保存的修改" : ""));
        status.classList.toggle("is-error", !!state.error && !state.saving);
    }
}

function renderTaskModelConfigPanel() {
    const body = U.taskModelConfigBody;
    if (!body) return;
    const state = taskModelConfigState();
    if (state.loading && !state.loaded) {
        body.innerHTML = '<div class="empty-state">正在加载模型配置...</div>';
        syncTaskModelConfigFooter();
        return;
    }
    if (state.error && !state.loaded) {
        body.innerHTML = `<div class="empty-state error">${esc(state.error)}</div>`;
        syncTaskModelConfigFooter();
        return;
    }
    body.innerHTML = TASK_MODEL_CONFIG_LANES.map(({ key }) => taskModelConfigLaneMarkup(key)).join("");
    syncTaskModelConfigFooter();
    if (typeof icons === "function") icons();
}

// 搜索只重绘列表，不重建输入框——整块重绘会把光标与焦点踢掉。
function renderTaskModelConfigPickerList(laneKey) {
    const list = U.taskModelConfigBody?.querySelector(`[data-task-model-picker-list="${laneKey}"]`);
    if (list) list.innerHTML = taskModelConfigPickerRows(laneKey);
}

// ---- 交互 -----------------------------------------------------------------

function setTaskModelConfigLaneMode(laneKey, mode) {
    const lane = taskModelConfigState().lanes[laneKey];
    if (!lane) return;
    if (mode === "custom") {
        if (!lane.editing) {
            lane.draftRefs = taskModelConfigLaneDisplayRefs(laneKey);
            lane.editing = true;
        }
    } else {
        lane.editing = false;
        lane.pickerOpen = false;
        lane.dragFrom = -1;
    }
    renderTaskModelConfigPanel();
}

function addTaskModelConfigRef(laneKey, ref) {
    const lane = taskModelConfigState().lanes[laneKey];
    const clean = String(ref || "").trim();
    if (!lane || !lane.editing || !clean) return;
    lane.draftRefs = [...taskModelConfigCleanRefs(lane.draftRefs), clean];
    renderTaskModelConfigPanel();
}

function removeTaskModelConfigRef(laneKey, index) {
    const lane = taskModelConfigState().lanes[laneKey];
    if (!lane || !lane.editing) return;
    const refs = taskModelConfigCleanRefs(lane.draftRefs);
    if (index < 0 || index >= refs.length) return;
    refs.splice(index, 1);
    lane.draftRefs = refs;
    renderTaskModelConfigPanel();
}

function moveTaskModelConfigRef(laneKey, from, to) {
    const lane = taskModelConfigState().lanes[laneKey];
    if (!lane || !lane.editing) return;
    const refs = taskModelConfigCleanRefs(lane.draftRefs);
    if (from < 0 || from >= refs.length || to < 0 || to >= refs.length || from === to) return;
    const [moved] = refs.splice(from, 1);
    refs.splice(to, 0, moved);
    lane.draftRefs = refs;
    renderTaskModelConfigPanel();
}

async function saveTaskModelConfig() {
    const state = taskModelConfigState();
    const taskId = String(S.currentTaskId || "").trim();
    if (!taskId || state.saving || !state.loaded) return;
    const payload = taskModelConfigSavePayload();
    if (!Object.keys(payload).length) return;
    state.saving = true;
    state.error = "";
    syncTaskModelConfigFooter();
    try {
        const response = await ApiClient.updateTaskModelConfig(taskId, payload);
        if (String(S.currentTaskId || "").trim() !== taskId) return;
        applyTaskModelConfigPayload(response);
    } catch (error) {
        if (String(S.currentTaskId || "").trim() !== taskId) return;
        state.error = String(error?.message || "保存失败");
        if (typeof showToast === "function") {
            showToast({ title: "模型配置保存失败", text: state.error, kind: "error" });
        }
    } finally {
        state.saving = false;
        renderTaskModelConfigPanel();
    }
}

function bindTaskModelConfigPanel() {
    U.taskModelConfigButton?.addEventListener("click", () => void openTaskModelConfig());
    U.taskModelConfigClose?.addEventListener("click", () => closeTaskModelConfig());
    U.taskModelConfigBackdrop?.addEventListener("click", () => closeTaskModelConfig());
    U.taskModelConfigSave?.addEventListener("click", () => void saveTaskModelConfig());
    const body = U.taskModelConfigBody;
    if (!body) return;
    body.addEventListener("click", (event) => {
        const target = event.target instanceof Element ? event.target : null;
        if (!target) return;
        const modeButton = target.closest("[data-task-model-mode]");
        if (modeButton) {
            setTaskModelConfigLaneMode(modeButton.dataset.taskModelLane, modeButton.dataset.taskModelMode);
            return;
        }
        const removeButton = target.closest("[data-task-model-remove]");
        if (removeButton) {
            removeTaskModelConfigRef(removeButton.dataset.taskModelLane, Number(removeButton.dataset.taskModelRemove));
            return;
        }
        const addButton = target.closest("[data-task-model-add]");
        if (addButton && !addButton.disabled) {
            addTaskModelConfigRef(addButton.dataset.taskModelLane, addButton.dataset.taskModelAdd);
            return;
        }
        const tabButton = target.closest("[data-task-model-picker-tab]");
        if (tabButton) {
            const lane = taskModelConfigState().lanes[tabButton.dataset.taskModelLane];
            if (lane) {
                lane.pickerTab = tabButton.dataset.taskModelPickerTab;
                renderTaskModelConfigPanel();
            }
            return;
        }
        const toggleButton = target.closest("[data-task-model-picker-toggle]");
        if (toggleButton) {
            const lane = taskModelConfigState().lanes[toggleButton.dataset.taskModelPickerToggle];
            if (lane) {
                lane.pickerOpen = !lane.pickerOpen;
                renderTaskModelConfigPanel();
            }
        }
    });
    body.addEventListener("input", (event) => {
        const input = event.target instanceof HTMLInputElement ? event.target : null;
        const laneKey = input?.dataset?.taskModelPickerSearch;
        if (!laneKey) return;
        const lane = taskModelConfigState().lanes[laneKey];
        if (!lane) return;
        lane.pickerSearch = input.value;
        renderTaskModelConfigPickerList(laneKey);
    });
    body.addEventListener("dragstart", (event) => {
        const tile = event.target instanceof Element ? event.target.closest("[data-task-model-index]") : null;
        if (!tile) return;
        const lane = taskModelConfigState().lanes[tile.dataset.taskModelLane];
        if (!lane?.editing) return;
        lane.dragFrom = Number(tile.dataset.taskModelIndex);
        tile.classList.add("is-dragging");
        if (event.dataTransfer) {
            event.dataTransfer.effectAllowed = "move";
            try { event.dataTransfer.setData("text/plain", tile.dataset.taskModelIndex); } catch (error) { void error; }
        }
    });
    body.addEventListener("dragover", (event) => {
        const tile = event.target instanceof Element ? event.target.closest("[data-task-model-index]") : null;
        if (!tile) return;
        const lane = taskModelConfigState().lanes[tile.dataset.taskModelLane];
        if (!lane?.editing || lane.dragFrom < 0) return;
        event.preventDefault();
        if (event.dataTransfer) event.dataTransfer.dropEffect = "move";
        body.querySelectorAll(".tmc-tile.is-drop-target").forEach((node) => node.classList.remove("is-drop-target"));
        tile.classList.add("is-drop-target");
    });
    body.addEventListener("drop", (event) => {
        const tile = event.target instanceof Element ? event.target.closest("[data-task-model-index]") : null;
        if (!tile) return;
        const laneKey = tile.dataset.taskModelLane;
        const lane = taskModelConfigState().lanes[laneKey];
        if (!lane?.editing || lane.dragFrom < 0) return;
        event.preventDefault();
        const to = Number(tile.dataset.taskModelIndex);
        const from = lane.dragFrom;
        lane.dragFrom = -1;
        moveTaskModelConfigRef(laneKey, from, to);
    });
    body.addEventListener("dragend", () => {
        const state = taskModelConfigState();
        TASK_MODEL_CONFIG_LANES.forEach(({ key }) => { state.lanes[key].dragFrom = -1; });
        body.querySelectorAll(".tmc-tile.is-dragging, .tmc-tile.is-drop-target").forEach((node) => {
            node.classList.remove("is-dragging", "is-drop-target");
        });
    });
}