// 模型页交互：加载/选中、拖拽、角色链草稿与保存、组弹窗动作、模型增删。

function nextStandaloneGroupKey() {
    const taken = new Set([
        ...Object.keys(S.modelCatalog.loadBalanceGroups || {}),
        ...Object.keys(S.modelCatalog.loadBalanceGroupDrafts || {}),
    ]);
    let index = 1;
    while (taken.has(`group_${index}`)) index += 1;
    return `group_${index}`;
}

function groupKeyNameTaken(name) {
    if (!name) return false;
    if ((S.modelCatalog.loadBalanceGroupDrafts || {})[name]) return true;
    return Boolean((S.modelCatalog.loadBalanceGroups || {})[name]);
}

function groupKeyCollidesWithModel(name) {
    if (modelRefItem(name)) return true;
    return (S.modelCatalog.catalog || []).some((item) => String(item?.key || "").trim() === name);
}

function rewriteLoadBalanceGroupToken(fromKey, toKey) {
    const fromToken = groupRefToken(fromKey);
    const toToken = groupRefToken(toKey);
    Object.keys(S.modelCatalog.roleDrafts || {}).forEach((scope) => {
        const chain = S.modelCatalog.roleDrafts[scope] || [];
        if (!chain.includes(fromToken)) return;
        S.modelCatalog.roleDrafts[scope] = chain.map((ref) => (ref === fromToken ? toToken : ref));
    });
}

function removeLoadBalanceGroupToken(groupKey) {
    const token = groupRefToken(groupKey);
    Object.keys(S.modelCatalog.roleDrafts || {}).forEach((scope) => {
        const chain = S.modelCatalog.roleDrafts[scope] || [];
        if (!chain.includes(token)) return;
        S.modelCatalog.roleDrafts[scope] = chain.filter((ref) => ref !== token);
    });
}

function openLoadBalanceGroupDialog(groupKey = "") {
    const key = String(groupKey || "").trim();
    const group = key ? (activeLoadBalanceGroups()[key] || null) : null;
    S.modelCatalog.groupDialog = {
        open: true,
        editingKey: key,
        name: key || nextStandaloneGroupKey(),
        modelKeys: group ? [...(group.model_keys || [])] : [],
        maxRetryRounds: group ? Number(group.max_retry_rounds || 1) : GROUP_DEFAULT_MAX_RETRY_ROUNDS,
        error: "",
    };
    renderModelGroupDialog();
    // 正文容器是常驻节点，换组只换它的 innerHTML：不清滚动位置的话，上一组滚到列表
    // 底部再开下一组，组名那颗输入框会停在可视区上方。
    if (U.modelGroupDialogBody) U.modelGroupDialogBody.scrollTop = 0;
    return true;
}

function closeLoadBalanceGroupDialog() {
    if (!S.modelCatalog.groupDialog.open) return false;
    S.modelCatalog.groupDialog = { open: false, editingKey: "", name: "", modelKeys: [], maxRetryRounds: 1, error: "" };
    renderModelGroupDialog();
    return true;
}

function rejectLoadBalanceGroupDialog(message) {
    S.modelCatalog.groupDialog.error = message;
    renderModelGroupDialog();
    return false;
}

function setLoadBalanceGroupDialogField(kind, value) {
    const dialog = S.modelCatalog.groupDialog;
    if (!dialog.open) return false;
    if (kind === "name") dialog.name = String(value ?? "");
    // 重试次数由用户自填，输入时不静默夹取：越界要在点确定时明确说出来。
    else if (kind === "rounds") dialog.maxRetryRounds = String(value ?? "");
    dialog.error = "";
    // 逐键重绘会把光标从输入框里踢出去，所以这里只同步计数与错误行。
    syncLoadBalanceGroupDialogSummary();
    return true;
}

function toggleLoadBalanceGroupDialogMember(modelRef, checked) {
    const dialog = S.modelCatalog.groupDialog;
    const ref = String(modelRef || "");
    if (!dialog.open || !ref) return false;
    const members = new Set(dialog.modelKeys);
    if (checked) members.add(ref); else members.delete(ref);
    dialog.modelKeys = [...members];
    dialog.error = "";
    syncLoadBalanceGroupDialogSummary();
    return true;
}

function confirmLoadBalanceGroupDialog() {
    const dialog = S.modelCatalog.groupDialog;
    if (!dialog.open) return false;
    const name = String(dialog.name || "").trim();
    if (!name) return rejectLoadBalanceGroupDialog("组名不能为空");
    if (!dialog.modelKeys.length) return rejectLoadBalanceGroupDialog("至少勾选 1 个模型");
    if (name !== dialog.editingKey && groupKeyNameTaken(name)) {
        return rejectLoadBalanceGroupDialog(`组名已存在：${name}`);
    }
    if (name !== dialog.editingKey && groupKeyCollidesWithModel(name)) {
        return rejectLoadBalanceGroupDialog(`组名不能与模型配置同名：${name}`);
    }
    const rounds = Number(dialog.maxRetryRounds);
    if (!Number.isInteger(rounds) || rounds < 0) {
        return rejectLoadBalanceGroupDialog("每成员重试次数必须是不小于 0 的整数");
    }
    // 建组就是模型链编辑会话的一部分：不在会话里先开一份，草稿才有地方落。
    if (!S.modelCatalog.roleEditing) startModelRoleEditing();
    const drafts = S.modelCatalog.loadBalanceGroupDrafts;
    const payload = { enabled: true, max_retry_rounds: rounds, model_keys: [...dialog.modelKeys] };
    if (dialog.editingKey && dialog.editingKey !== name) {
        drafts[name] = { ...(drafts[dialog.editingKey] || {}), ...payload };
        delete drafts[dialog.editingKey];
        // 改名必须把链上的 group 记号一起带走，否则链会指向一个已经不存在的组。
        rewriteLoadBalanceGroupToken(dialog.editingKey, name);
    } else {
        drafts[name] = payload;
    }
    syncModelRoleDraftState();
    closeLoadBalanceGroupDialog();
    renderModelCatalog();
    return true;
}

function deleteLoadBalanceGroupDraft(groupKey) {
    if (!S.modelCatalog.roleEditing) startModelRoleEditing();
    const drafts = S.modelCatalog.loadBalanceGroupDrafts;
    if (!drafts[groupKey]) return false;
    delete drafts[groupKey];
    removeLoadBalanceGroupToken(groupKey);
    syncModelRoleDraftState();
    renderModelCatalog();
    return true;
}

function contextRefreshDraftDirty() {
    const state = S.modelCatalog.contextRefresh;
    return state.enabled !== state.saved.enabled
        || Number(state.idleMinutes) !== Number(state.saved.idleMinutes);
}

function closeContextRefreshDialog() {
    S.modelCatalog.contextRefresh.open = false;
    S.modelCatalog.contextRefresh.error = "";
    renderContextRefreshDialog();
}

async function loadContextRefreshSettings() {
    const state = S.modelCatalog.contextRefresh;
    state.loading = true;
    state.error = "";
    renderContextRefreshDialog();
    try {
        const payload = await ApiClient.getContextRefreshSettings();
        const section = payload?.context_refresh || {};
        state.saved = {
            enabled: Boolean(section.enabled),
            idleMinutes: Number(section.idle_minutes) || 120,
        };
        state.enabled = state.saved.enabled;
        state.idleMinutes = state.saved.idleMinutes;
    } catch (error) {
        state.error = String(error?.message || "读取失败，请稍后重试");
    } finally {
        state.loading = false;
        renderContextRefreshDialog();
    }
}

async function applyContextRefreshSettings() {
    const state = S.modelCatalog.contextRefresh;
    const minutes = Math.trunc(Number(state.idleMinutes));
    if (!Number.isFinite(minutes) || minutes < 1) {
        state.error = "刷新时间至少为 1 分钟";
        renderContextRefreshDialog();
        return;
    }
    state.error = "";
    state.saving = true;
    renderContextRefreshDialog();
    try {
        const payload = await ApiClient.updateContextRefreshSettings({
            enabled: state.enabled,
            idleMinutes: minutes,
        });
        const section = payload?.context_refresh || {};
        state.saved = {
            enabled: Boolean(section.enabled),
            idleMinutes: Number(section.idle_minutes) || minutes,
        };
        state.enabled = state.saved.enabled;
        state.idleMinutes = state.saved.idleMinutes;
        showToast({
            title: "上下文信息更新时间已保存",
            text: state.enabled ? `距上次请求超过 ${state.saved.idleMinutes} 分钟后，下一次请求前先刷新。` : "已禁用。",
            kind: "success",
        });
        closeContextRefreshDialog();
    } catch (error) {
        state.error = String(error?.message || "保存失败，请稍后重试");
        showToast({ title: "上下文信息更新时间保存失败", text: state.error, kind: "error" });
    } finally {
        state.saving = false;
        renderContextRefreshDialog();
    }
}

async function openContextRefreshDialog() {
    const state = S.modelCatalog.contextRefresh;
    state.open = true;
    state.error = "";
    renderContextRefreshDialog();
    U.contextRefreshDialog?.focus();
    await loadContextRefreshSettings();
}

function bindContextRefreshDialog() {
    U.modelContextRefresh?.addEventListener("click", () => void openContextRefreshDialog());
    U.contextRefreshCancel?.addEventListener("click", closeContextRefreshDialog);
    U.contextRefreshClose?.addEventListener("click", closeContextRefreshDialog);
    U.contextRefreshApply?.addEventListener("click", () => void applyContextRefreshSettings());
    U.contextRefreshBackdrop?.addEventListener("click", (e) => {
        if (e.target === U.contextRefreshBackdrop && !S.modelCatalog.contextRefresh.saving) closeContextRefreshDialog();
    });
    U.contextRefreshEnabled?.addEventListener("change", (e) => {
        S.modelCatalog.contextRefresh.enabled = Boolean(e.target.checked);
        S.modelCatalog.contextRefresh.error = "";
        renderContextRefreshDialog();
    });
    U.contextRefreshMinutes?.addEventListener("input", (e) => {
        S.modelCatalog.contextRefresh.idleMinutes = e.target.value;
        S.modelCatalog.contextRefresh.error = "";
        renderContextRefreshDialog();
    });
}

async function loadModels() {
    S.modelCatalog.loading = true;
    S.modelCatalog.error = "";
    renderModelCatalog();
    try {
        const data = await ApiClient.getOrgGraphModels();
        applyModelCatalog(data, { preserveRoleDrafts: !!S.modelCatalog.roleEditing });
    } catch (e) {
        S.modelCatalog.error = e.message || "load failed";
    } finally {
        S.modelCatalog.loading = false;
        renderModelCatalog();
    }
}

function openModel(key) {
    S.modelCatalog.mode = "view";
    S.modelCatalog.selectedModelKey = String(key || "").trim();
    renderModelCatalog();
}

function startCreateModel() {
    S.modelCatalog.mode = "create";
    S.modelCatalog.selectedModelKey = "";
    renderModelCatalog();
}

function clearModelSelection() {
    S.modelCatalog.mode = "view";
    S.modelCatalog.selectedModelKey = "";
    renderModelCatalog();
}

function clearModelDragDecorations() {
    [U.modelRoleEditors, U.modelList].filter(Boolean).forEach((root) => {
        root.querySelectorAll('.is-drop-target').forEach((item) => item.classList.remove('is-drop-target'));
        root.querySelectorAll('.is-drop-zone').forEach((item) => item.classList.remove('is-drop-zone'));
        root.querySelectorAll('[data-drop-position]').forEach((item) => delete item.dataset.dropPosition);
        root.querySelectorAll('[data-model-drop-placeholder]').forEach((item) => item.remove());
    });
}

function beginModelDrag(item, { scope = "", ref = "", source = "available" } = {}, dataTransfer = null) {
    const modelRef = String(ref || "").trim();
    if (!S.modelCatalog.roleEditing || !item || !modelRef) return false;
    S.modelCatalog.dragState = {
        scope: String(scope || ""),
        ref: modelRef,
        source,
        hoverZoneKey: "",
        scrollFrameId: null,
        scrollTarget: null,
        scrollStep: 0,
    };
    item.classList.add("is-dragging");
    clearModelDragDecorations();
    if (dataTransfer) {
        dataTransfer.effectAllowed = source === "chain" ? "move" : "copyMove";
        dataTransfer.setData("text/plain", modelRef);
    }
    return true;
}

function finishModelDrag() {
    stopModelAutoScroll();
    [U.modelRoleEditors, U.modelList].filter(Boolean).forEach((root) => {
        root.querySelectorAll(".model-chain-slide.is-dragging, .model-available-item.is-dragging").forEach((item) => item.classList.remove("is-dragging"));
    });
    S.modelCatalog.dragState = null;
    clearModelDragDecorations();
}

function stopModelAutoScroll() {
    const dragState = S.modelCatalog.dragState;
    if (!dragState) return;
    if (dragState.scrollFrameId) window.cancelAnimationFrame(dragState.scrollFrameId);
    dragState.scrollFrameId = null;
    dragState.scrollTarget = null;
    dragState.scrollStep = 0;
}

function startModelAutoScroll(target, clientY) {
    const dragState = S.modelCatalog.dragState;
    if (!dragState || !target) return;
    const rect = target.getBoundingClientRect();
    const threshold = Math.min(48, rect.height / 4);
    let step = 0;
    if (clientY < rect.top + threshold) {
        step = -Math.max(6, Math.round((rect.top + threshold - clientY) / 5));
    } else if (clientY > rect.bottom - threshold) {
        step = Math.max(6, Math.round((clientY - (rect.bottom - threshold)) / 5));
    }
    if (!step) {
        if (dragState.scrollTarget === target) stopModelAutoScroll();
        return;
    }
    dragState.scrollTarget = target;
    dragState.scrollStep = step;
    if (dragState.scrollFrameId) return;
    const tick = () => {
        const state = S.modelCatalog.dragState;
        if (!state?.scrollTarget || !state.scrollStep) {
            stopModelAutoScroll();
            return;
        }
        state.scrollTarget.scrollTop += state.scrollStep;
        state.scrollFrameId = window.requestAnimationFrame(tick);
    };
    dragState.scrollFrameId = window.requestAnimationFrame(tick);
}

function modelDragZoneContainsPoint(zone, clientX, clientY) {
    if (!zone || !Number.isFinite(clientX) || !Number.isFinite(clientY)) return false;
    const rect = zone.getBoundingClientRect();
    return clientX >= rect.left && clientX <= rect.right && clientY >= rect.top && clientY <= rect.bottom;
}

function didModelDragLeaveZone(zone, event) {
    if (!zone) return true;
    const related = event?.relatedTarget;
    if (related instanceof Node && zone.contains(related)) return false;
    const clientX = Number(event?.clientX);
    const clientY = Number(event?.clientY);
    if (Number.isFinite(clientX) && Number.isFinite(clientY)) {
        const hovered = document.elementFromPoint(clientX, clientY);
        if (hovered instanceof Node && zone.contains(hovered)) return false;
        return !modelDragZoneContainsPoint(zone, clientX, clientY);
    }
    return false;
}

function resolveModelChainDropList(target) {
    if (!(target instanceof Element)) return null;
    const directList = target.closest("[data-model-chain-list]");
    if (directList) return directList;
    const card = target.closest(".model-chain-card");
    return card?.querySelector("[data-model-chain-list]") || null;
}

function resolveModelChainDropTarget(list, clientY, dragState = null) {
    if (!list) return null;
    const scope = String(list.dataset.modelChainList || "");
    const items = [...list.children].filter((child) => {
        if (!child.matches?.("[data-model-chain-ref]")) return false;
        if (
            dragState?.source === "chain"
            && scope === String(dragState.scope || "")
            && String(child.dataset.modelChainRef || "") === String(dragState.ref || "")
        ) {
            return false;
        }
        return true;
    });
    for (const item of items) {
        const rect = item.getBoundingClientRect();
        if (clientY < rect.top + (rect.height / 2)) return item;
    }
    return items[items.length - 1] || null;
}

function resolveModelChainDropIndex(list, dragState, clientY) {
    if (!list) return 0;
    const items = [...list.children].filter((child) => {
        if (!child.matches?.("[data-model-chain-ref]")) return false;
        return String(child.dataset.modelChainRef || "") !== String(dragState?.ref || "");
    });
    const targetItem = resolveModelChainDropTarget(list, clientY, dragState);
    if (!targetItem) return items.length;
    const targetIndex = items.indexOf(targetItem);
    if (targetIndex < 0) return items.length;
    const rect = targetItem.getBoundingClientRect();
    const insertBefore = clientY < rect.top + (rect.height / 2);
    return targetIndex + (insertBefore ? 0 : 1);
}

function ensureModelDropPlaceholder(list, targetItem, clientY) {
    if (!list) return null;
    list.querySelectorAll('.is-drop-target').forEach((item) => item.classList.remove('is-drop-target'));
    list.querySelectorAll('[data-drop-position]').forEach((item) => delete item.dataset.dropPosition);
    const placeholder = list.querySelector('[data-model-drop-placeholder]') || document.createElement('div');
    placeholder.className = 'model-chain-drop-placeholder';
    placeholder.dataset.modelDropPlaceholder = '1';
    list.classList.add('is-drop-zone');
    list.closest('.model-chain-card')?.classList.add('is-drop-zone');
    if (targetItem && targetItem.parentElement === list) {
        targetItem.classList.add('is-drop-target');
        const rect = targetItem.getBoundingClientRect();
        const insertBefore = clientY < rect.top + (rect.height / 2);
        placeholder.dataset.dropPosition = insertBefore ? 'before' : 'after';
        targetItem.dataset.dropPosition = insertBefore ? 'before' : 'after';
        list.insertBefore(placeholder, insertBefore ? targetItem : targetItem.nextSibling);
    } else {
        placeholder.dataset.dropPosition = 'append';
        list.appendChild(placeholder);
    }
    return placeholder;
}

function highlightModelAvailableZone(list, targetItem = null) {
    if (!list) return;
    list.querySelectorAll('.is-drop-target').forEach((item) => item.classList.remove('is-drop-target'));
    list.classList.add('is-drop-zone');
    if (targetItem && targetItem.parentElement === list) {
        targetItem.classList.add('is-drop-target');
    }
}

function moveRoleChainItem(scope, fromRef, targetIndex = null) {
    const chain = modelScopeChain(scope);
    const sourceIndex = chain.findIndex((item) => modelRefEquivalent(item, fromRef));
    if (sourceIndex < 0) return;
    const nextChain = [...chain];
    const [moving] = nextChain.splice(sourceIndex, 1);
    const boundedIndex = targetIndex === null
        ? nextChain.length
        : Math.max(0, Math.min(Number(targetIndex), nextChain.length));
    nextChain.splice(boundedIndex, 0, moving);
    updateRoleChainDraft(scope, nextChain);
}

function insertRoleChainItem(scope, modelKey, targetIndex = null) {
    const nextChain = modelScopeChain(scope).filter((item) => !modelRefEquivalent(item, modelKey));
    const boundedIndex = targetIndex === null
        ? nextChain.length
        : Math.max(0, Math.min(Number(targetIndex), nextChain.length));
    nextChain.splice(boundedIndex, 0, modelKey);
    updateRoleChainDraft(scope, nextChain);
}

function removeRoleChainItem(scope, modelKey) {
    const nextChain = modelScopeChain(scope).filter((item) => !modelRefEquivalent(item, modelKey));
    updateRoleChainDraft(scope, nextChain);
}

function updateRoleChainDraft(scope, nextChain) {
    if (!S.modelCatalog.roleEditing) return;
    S.modelCatalog.roleDrafts[scope] = normalizeModelRoleChain(nextChain);
    syncModelRoleDraftState();
    renderModelHint();
    renderModelRoleEditors();
    renderModelList();
    syncModelDetailScopeToggles();
}

function updateRoleIterationDraft(scope, value, { render = true } = {}) {
    if (!S.modelCatalog.roleEditing) return false;
    const normalizedScope = String(scope || "").trim();
    if (!normalizedScope) return false;
    if (value == null || String(value).trim() === "") {
        S.modelCatalog.roleIterationDrafts[normalizedScope] = null;
    } else {
        const cleanValue = Number.parseInt(String(value || "").trim(), 10);
        if (!Number.isInteger(cleanValue) || cleanValue < 0) return false;
        S.modelCatalog.roleIterationDrafts[normalizedScope] = cleanValue;
    }
    syncModelRoleDraftState();
    if (render) renderModelCatalog();
    return true;
}

function syncRoleIterationDraftsFromInputs({ requireValid = false } = {}) {
    const roots = [U.modelRoleLimitsBar, U.modelRoleEditors].filter(Boolean);
    const groups = roots.flatMap((root) => [...root.querySelectorAll("[data-model-role-limit-kind][data-model-role-limit-scope]")]);
    if (!groups.length) return false;
    let changed = false;
    groups.forEach((group) => {
        if (!(group instanceof HTMLElement)) return;
        const scope = String(group.dataset.modelRoleLimitScope || "").trim();
        const kind = String(group.dataset.modelRoleLimitKind || "").trim();
        if (!scope || !kind) return;
        const input = group.querySelector("[data-model-role-limit-input]");
        if (!(input instanceof HTMLInputElement)) return;
        const scopeLabel = MODEL_SCOPES.find((item) => item.key === scope)?.label || scope;
        const label = kind === "iterations" ? "最大轮数" : "最大并发数";
        const fixed = String(group.dataset.modelRoleFixed || "").trim() === "1";
        if (fixed) {
            const fixedValue = normalizeInt(group.dataset.modelRoleFixedValue, 1);
            if (kind === "concurrency" && modelScopeConcurrency(scope, "draft") !== fixedValue) {
                S.modelCatalog.roleConcurrencyDrafts[scope] = fixedValue;
                changed = true;
            }
            input.disabled = true;
            input.value = String(fixedValue);
            input.classList.remove("is-invalid");
            input.setCustomValidity("");
            return;
        }
        let rawValue = String(input.value || "").trim();
        if (rawValue === "") rawValue = "-1";
        const cleanValue = Number.parseInt(rawValue, 10);
        const invalid = !Number.isInteger(cleanValue) || cleanValue < -1;
        if (invalid) {
            input.classList.add("is-invalid");
            input.setCustomValidity(`${label}必须是不小于 -1 的整数，-1 代表不限制`);
            if (requireValid) {
                input.reportValidity();
                throw new Error(`${scopeLabel} ${label}必须是不小于 -1 的整数`);
            }
            return;
        }
        input.classList.remove("is-invalid");
        input.setCustomValidity("");
        const nextValue = cleanValue < 0 ? null : cleanValue;
        const currentValue = kind === "iterations" ? modelScopeIterations(scope, "draft") : modelScopeConcurrency(scope, "draft");
        if (currentValue !== nextValue) {
            if (kind === "iterations") S.modelCatalog.roleIterationDrafts[scope] = nextValue;
            if (kind === "concurrency") S.modelCatalog.roleConcurrencyDrafts[scope] = nextValue;
            changed = true;
        }
    });
    if (changed) syncModelRoleDraftState();
    return changed;
}

function startModelRoleEditing() {
    S.modelCatalog.roleEditing = true;
    S.modelCatalog.roleDrafts = cloneModelRoles(S.modelCatalog.roles);
    S.modelCatalog.roleIterationDrafts = cloneRoleIterations(S.modelCatalog.roleIterations);
    S.modelCatalog.roleConcurrencyDrafts = cloneRoleConcurrency(S.modelCatalog.roleConcurrency);
    S.modelCatalog.loadBalanceGroupDrafts = cloneLoadBalanceGroups(S.modelCatalog.loadBalanceGroups);
    syncModelRoleDraftState();
    renderModelCatalog();
}

function cancelModelRoleEditing() {
    S.modelCatalog.roleEditing = false;
    S.modelCatalog.roleDrafts = cloneModelRoles(S.modelCatalog.roles);
    S.modelCatalog.roleIterationDrafts = cloneRoleIterations(S.modelCatalog.roleIterations);
    S.modelCatalog.roleConcurrencyDrafts = cloneRoleConcurrency(S.modelCatalog.roleConcurrency);
    S.modelCatalog.loadBalanceGroupDrafts = cloneLoadBalanceGroups(S.modelCatalog.loadBalanceGroups);
    S.modelCatalog.rolesDirty = false;
    // 组草稿也一起回滚，弹窗留着只会指向一份已经不存在的草稿。
    closeLoadBalanceGroupDialog();
    finishModelDrag();
    renderModelCatalog();
    hint("已取消模型链修改。", false);
}

async function persistModelRoleChains(scopes = MODEL_SCOPES.map((item) => item.key), successText = "模型链已保存。", { useDrafts = false } = {}) {
    const updates = buildModelRoleChainUpdates(scopes, { useDrafts });
    if (!Object.keys(updates).length) return;
    const submittingChains = Object.entries(updates)
        .filter(([scope]) => IMAGE_JUDGED_MODEL_SCOPES.includes(scope))
        .map(([, payload]) => roleChainUpdateToChain(payload));
    if (!(await confirmChainsImageCapabilityMix(submittingChains))) return;
    S.modelCatalog.saving = true;
    renderModelCatalog();
    try {
        const payload = await ApiClient.updateModelRoleChains(updates);
        if (payload) applyModelCatalog(payload);
        hint(successText);
    } catch (e) {
        S.modelCatalog.error = e.message || "save failed";
        hint(`模型配置错误：${S.modelCatalog.error}`, true);
        throw e;
    } finally {
        S.modelCatalog.saving = false;
        renderModelCatalog();
    }
}

function buildModelRoleChainUpdates(scopes = MODEL_SCOPES.map((item) => item.key), { useDrafts = false } = {}) {
    const targets = [...new Set(scopes.map((item) => String(item || "").trim()).filter(Boolean))];
    if (!targets.length) return {};
    const roleSource = useDrafts ? S.modelCatalog.roleDrafts : S.modelCatalog.roles;
    const iterationSource = useDrafts ? S.modelCatalog.roleIterationDrafts : S.modelCatalog.roleIterations;
    const draftConcurrencySource = S.modelCatalog.roleConcurrencyDrafts || DEFAULT_ROLE_CONCURRENCY();
    const concurrencySource = useDrafts ? draftConcurrencySource : S.modelCatalog.roleConcurrency;
    const touchedGroups = collectTouchedLoadBalanceGroups();
    // 组是全局资源，但只有 execution / inspection 能引用它。没有任何链用到组时，把整份
    // 集合挂在第一个可承载的 scope 上，「刚建好还没拖进链」的组才不会丢。
    const groupCarrier = targets.find((scope) => LOAD_BALANCE_ROUTE_SCOPES.includes(scope));
    const anyChainUsesGroup = targets.some((scope) => chainUsesGroup(normalizeModelRoleChain(roleSource[scope] || [])));
    return Object.fromEntries(targets.map((scope) => {
        const chain = normalizeModelRoleChain(roleSource[scope] || []);
        const base = {
            maxIterations: iterationSource[scope],
            maxConcurrency: concurrencySource[scope],
        };
        if (!chainUsesGroup(chain)) {
            if (!anyChainUsesGroup && scope === groupCarrier && Object.keys(touchedGroups).length) {
                return [scope, { ...base, modelKeys: chain, loadBalanceGroups: touchedGroups }];
            }
            return [scope, { ...base, modelKeys: chain }];
        }
        // 含组的链必须走 route_entries：扁平 modelKeys 表达不了「一跳是一个组」。
        return [scope, {
            ...base,
            routeEntries: chainToRouteEntries(chain),
            loadBalanceGroups: touchedGroups,
        }];
    }));
}

function collectTouchedLoadBalanceGroups() {
    const groups = S.modelCatalog.roleEditing
        ? (S.modelCatalog.loadBalanceGroupDrafts || {})
        : (S.modelCatalog.loadBalanceGroups || {});
    const next = {};
    Object.keys(groups).forEach((key) => {
        const group = groups[key] || {};
        if (!(group.model_keys || []).length) return;
        next[key] = group;
    });
    return next;
}

async function handleModelRoleEditorAction() {
    if (!S.modelCatalog.roleEditing) {
        startModelRoleEditing();
        hint("已进入模型链编辑模式。");
        return;
    }
    try {
        syncRoleIterationDraftsFromInputs({ requireValid: true });
    } catch (e) {
        S.modelCatalog.error = e.message || "save failed";
        hint(`模型配置错误：${S.modelCatalog.error}`, true);
        return;
    }
    if (!S.modelCatalog.rolesDirty) {
        cancelModelRoleEditing();
        return;
    }
    const validationMessage = requiredModelRoleValidationMessage("draft");
    if (validationMessage) {
        S.modelCatalog.error = validationMessage;
        hint(`模型配置错误：${S.modelCatalog.error}`, true);
        showToast({ title: "保存失败", text: S.modelCatalog.error, kind: "error" });
        return;
    }
    await persistModelRoleChains(MODEL_SCOPES.map((item) => item.key), "模型链已保存。", { useDrafts: true });
}

function parseModelRetryOn(raw) {
    return String(raw || "").split(/[\s,]+/).map((item) => item.trim()).filter(Boolean);
}

function parseModelHeaders(raw) {
    const text = String(raw || "").trim();
    if (!text) return null;
    const parsed = JSON.parse(text);
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
        throw new Error("额外请求头必须是 JSON 对象");
    }
    return Object.fromEntries(Object.entries(parsed).map(([key, value]) => [String(key), String(value)]));
}

function collectModelFormData(form, current) {
    const isCreate = form.dataset.mode === "create";
    const formData = new FormData(form);
    const key = isCreate ? String(formData.get("key") || "").trim() : String(form.dataset.modelKey || "").trim();
    const providerModel = String(formData.get("providerModel") || "").trim();
    const apiKey = String(formData.get("apiKey") || "").trim();
    const apiBase = String(formData.get("apiBase") || "").trim();
    const maxTokensText = String(formData.get("maxTokens") || "").trim();
    const temperatureText = String(formData.get("temperature") || "").trim();
    const reasoningEffortText = String(formData.get("reasoningEffort") || "").trim();
    const contextWindowTokensText = String(formData.get("contextWindowTokens") || "").trim();
    const reasoningEffort = reasoningEffortText || null;
    const retryOnRaw = String(formData.get("retryOn") || "").trim();
    const retryCountText = String(formData.get("retryCount") || "").trim();
    const enabled = formData.get("enabled") === "on";
    const selectedScopes = new Set(MODEL_SCOPES.filter((scope) => formData.get(`scope_${scope.key}`) === "on").map((scope) => scope.key));
    const hasApiKeyEntries = String(apiKey || "").split(/[\n,]/).some((item) => String(item || "").trim());

    if (!key) throw new Error("配置名 / 绑定名不能为空");
    if (!providerModel) throw new Error("Provider / Model 不能为空");
    if (!hasApiKeyEntries) throw new Error("API Key 不能为空");
    if (!apiKey) throw new Error("API Key 不能为空");
    if (isCreate && !apiBase) throw new Error("Base URL 不能为空");

    const extraHeaders = parseModelHeaders(formData.get("extraHeaders"));
    const retryOn = retryOnRaw ? parseModelRetryOn(retryOnRaw) : null;
    const retryCount = retryCountText ? Number.parseInt(retryCountText, 10) : 0;
    const maxTokens = maxTokensText ? Number(maxTokensText) : null;
    const temperature = temperatureText ? Number(temperatureText) : null;
    const contextWindowTokens = contextWindowTokensText ? Number.parseInt(contextWindowTokensText, 10) : NaN;

    if (maxTokensText && (!Number.isInteger(maxTokens) || maxTokens <= 0)) {
        throw new Error("Max Tokens 必须是正整数");
    }
    if (!contextWindowTokensText || !Number.isInteger(contextWindowTokens) || contextWindowTokens <= 25000) {
        throw new Error("最大上下文TOKEN必须是大于 25000 的整数");
    }
    if (temperatureText && (!Number.isFinite(temperature) || temperature < 0 || temperature > 2)) {
        throw new Error("Temperature 必须在 0 到 2 之间");
    }
    if (retryCountText && (!Number.isInteger(retryCount) || retryCount < 0)) {
        throw new Error("重试次数必须是不小于 0 的整数");
    }

    if (isCreate) {
        const payload = {
            key,
            providerModel,
            apiKey,
            apiBase,
            enabled,
            scopes: [...selectedScopes],
            contextWindowTokens,
        };
        if (maxTokens !== null) payload.maxTokens = maxTokens;
        if (temperature !== null) payload.temperature = temperature;
        if (reasoningEffort !== null) payload.reasoningEffort = reasoningEffort;
        if (retryOn !== null) payload.retryOn = retryOn;
        payload.retryCount = retryCount;
        if (extraHeaders !== null) payload.extraHeaders = extraHeaders;
        return { isCreate, key, enabled, selectedScopes, payload };
    }

    const patch = {};
    if (providerModel !== String(current?.provider_model || "")) patch.providerModel = providerModel;
    if (apiKey !== String(current?.api_key || "")) patch.apiKey = apiKey;
    if (apiBase !== String(current?.api_base || "")) patch.apiBase = apiBase;
    if (maxTokensText) {
        if (maxTokens !== Number(current?.max_tokens ?? NaN)) patch.maxTokens = maxTokens;
    } else if (current?.max_tokens != null) {
        patch.maxTokens = null;
    }
    if (temperatureText) {
        if (temperature !== Number(current?.temperature ?? NaN)) patch.temperature = temperature;
    } else if (current?.temperature != null) {
        patch.temperature = null;
    }
    if (reasoningEffort !== (String(current?.reasoning_effort || "").trim() || null)) patch.reasoningEffort = reasoningEffort;
    if (retryOn !== null && JSON.stringify(retryOn) !== JSON.stringify(current?.retry_on || [])) patch.retryOn = retryOn;
    if (retryCount !== Number.parseInt(String(current?.retry_count ?? 0), 10)) patch.retryCount = retryCount;
    if (contextWindowTokens !== Number.parseInt(String(current?.context_window_tokens ?? 0), 10)) {
        patch.contextWindowTokens = contextWindowTokens;
    }
    if (extraHeaders !== null && JSON.stringify(extraHeaders) !== JSON.stringify(current?.extra_headers || null)) patch.extraHeaders = extraHeaders;
    return { isCreate, key, enabled, selectedScopes, patch };
}

async function saveModelDetail() {
    const form = U.modelDetail?.querySelector("#model-detail-form");
    if (!(form instanceof HTMLFormElement)) return;
    const current = form.dataset.mode === "create" ? null : modelRefItem(form.dataset.modelKey);
    try {
        const draft = collectModelFormData(form, current);
        const preserveRoleDrafts = !!S.modelCatalog.roleEditing;
        const enableChanged = !draft.isCreate && draft.enabled !== !!current?.enabled;
        if (!draft.isCreate && !Object.keys(draft.patch).length && !enableChanged) {
            hint("没有需要保存的更改。");
            return;
        }

        if (draft.isCreate) {
            const payload = await ApiClient.createManagedModel({ ...draft.payload, scopes: [] });
            applyModelCatalog(payload, { preserveRoleDrafts });
            S.modelCatalog.mode = "view";
            S.modelCatalog.selectedModelKey = payload.model?.key || draft.key;
        } else {
            if (Object.keys(draft.patch).length) {
                const payload = await ApiClient.updateManagedModel(current.key, draft.patch);
                applyModelCatalog(payload, { preserveRoleDrafts });
            }
            if (enableChanged) {
                const payload = draft.enabled ? await ApiClient.enableManagedModel(current.key) : await ApiClient.disableManagedModel(current.key);
                applyModelCatalog(payload, { preserveRoleDrafts });
            }
            S.modelCatalog.mode = "view";
            S.modelCatalog.selectedModelKey = current.key;
        }

        hint(draft.isCreate ? "模型已添加。" : "模型配置已保存。");
        clearModelSelection();

    } catch (e) {
        S.modelCatalog.error = e.message || "save failed";
        hint(`模型配置错误：${S.modelCatalog.error}`, true);
        showToast({ title: "修改失败", text: `模型配置错误：${S.modelCatalog.error}`, kind: "error" });
        clearModelSelection();
    }
}

async function deleteModelDetail(modelKey) {
    const targetKey = String(modelKey || "").trim();
    if (!targetKey) return;
    const { confirmed } = await requestInlineConfirm({
        title: "确认删除模型？",
        text: `删除模型 ${targetKey} 后，会同时从 catalog 和所有角色链移除它。`,
        confirmLabel: "删除模型",
        confirmKind: "danger",
    });
    if (!confirmed) return;
    try {
        const payload = await ApiClient.deleteManagedModel(targetKey);
        applyModelCatalog(payload, { preserveRoleDrafts: false });
        hint("模型已删除。");
        clearModelSelection();
    } catch (e) {
        const message = e.message || "delete failed";
        S.modelCatalog.error = message;
        hint(`模型删除失败：${message}`, true);
        showToast({ title: "删除失败", text: `模型删除失败：${message}`, kind: "error" });
    }
}
