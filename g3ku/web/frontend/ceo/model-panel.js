"use strict";

// CEO composer 的模型链/固定模型面板：目录投影、链草稿、拖拽排序、模式面板与保存。
// 只读写 S/U 与既有全局入口；本文件必须在 org_graph_app.js 之前加载。

function modelDisplayTitle(item) {
    if (!item) return "";
    return String(item.name || "").trim()
        || String(item.key || "").trim()
        || String(item.provider_model || "").trim();
}

function ceoModelDisplayTitle(item) {
    return modelDisplayTitle(item);
}

function ceoModelCatalogItem(modelKey) {
    const key = String(modelKey || "").trim();
    if (!key) return null;
    return (S.modelCatalog.catalog || []).find((item) => String(item?.key || "").trim() === key) || null;
}

function ceoModelUsageHeadlineTitle(raw) {
    const text = String(raw || "").trim();
    if (!text) return "";
    const byKey = ceoModelCatalogItem(text);
    if (byKey) return ceoModelDisplayTitle(byKey) || text;
    const byProviderModel = (S.modelCatalog.catalog || []).find(
        (item) => String(item?.provider_model || "").trim() === text,
    );
    if (byProviderModel) return ceoModelDisplayTitle(byProviderModel) || text;
    return text;
}

function ceoModelChainKeys() {
    const roles = S.modelCatalog && S.modelCatalog.roles ? S.modelCatalog.roles : null;
    return (Array.isArray(roles?.ceo) ? roles.ceo : [])
        .map((ref) => String(ref || "").trim())
        .filter(Boolean);
}

function ceoModelSelectionFor(sessionId) {
    const key = String(sessionId || "").trim();
    if (!key) return null;
    return String(S.ceoModelSelection.sessionId || "").trim() === key ? S.ceoModelSelection : null;
}

function resetCeoModelSelection(sessionId) {
    const key = String(sessionId || "").trim();
    S.ceoModelSelection = {
        ...S.ceoModelSelection,
        sessionId: key,
        mode: "chain",
        modelKey: "",
        pinnedAvailable: true,
        loaded: false,
        loading: false,
        saving: false,
        error: "",
        panelOpen: false,
        pickerOpen: false,
        paneTouched: false,
        pendingChainSwitch: false,
        search: "",
        pickerPurpose: "pin",
        chainKeys: ceoModelChainKeys(),
        chainEditing: false,
        sessionChainKeys: [],
        isSessionChain: false,
        dragFrom: -1,
        dropIndex: null,
    };
    syncCeoModelModeControl();
}

function applyCeoModelSelectionPayload(sessionId, payload, { keepDraft = false } = {}) {
    const key = String(sessionId || "").trim();
    const data = payload && typeof payload === "object" ? payload : {};
    const modelKey = String(data.model_key || data.modelKey || "").trim();
    const mode = String(data.mode || "").trim() === "model" && modelKey ? "model" : "chain";
    const sessionChainKeys = normalizeModelRoleChain(Array.isArray(data.model_keys) ? data.model_keys : []);
    // keepDraft：后台读到的服务端状态不能顺手把用户正在改的那份草稿打回基线。
    const chainKeys = keepDraft
        ? ceoModelChainDraft()
        : (sessionChainKeys.length ? [...sessionChainKeys] : ceoModelChainKeys());
    S.ceoModelSelection = {
        ...S.ceoModelSelection,
        sessionId: String(data.session_id || key).trim() || key,
        mode,
        modelKey: mode === "model" ? modelKey : "",
        pinnedAvailable: data.pinned_available !== false,
        loaded: true,
        loading: false,
        saving: false,
        error: "",
        sessionChainKeys,
        isSessionChain: sessionChainKeys.length > 0,
        chainKeys,
    };
    syncCeoModelModeControl();
    return S.ceoModelSelection;
}

async function refreshCeoModelSelection(sessionId, { force = false } = {}) {
    const key = String(sessionId || "").trim();
    if (!key) {
        resetCeoModelSelection(key);
        return null;
    }
    const current = S.ceoModelSelection;
    // 只有确实加载过该会话才复用；reset 只写 sessionId，不能当成已加载。
    if (!force && String(current.sessionId || "").trim() === key && current.loaded) return current;
    if (String(current.sessionId || "").trim() === key && current.loading) return current;
    current.requestToken += 1;
    const token = current.requestToken;
    S.ceoModelSelection = { ...current, sessionId: key, loading: true, error: "" };
    syncCeoModelModeControl();
    try {
        const payload = await ApiClient.getCeoSessionModelSelection(key);
        if (token !== S.ceoModelSelection.requestToken) return null;
        // 这份回执可能晚于用户的第一次改动：正在编辑时只更新服务端状态，草稿留给用户。
        return applyCeoModelSelectionPayload(key, payload, {
            keepDraft: !!S.ceoModelSelection.chainEditing,
        });
    } catch (error) {
        if (token !== S.ceoModelSelection.requestToken) return null;
        S.ceoModelSelection = {
            ...S.ceoModelSelection,
            sessionId: key,
            loading: false,
            error: String(error?.message || "load_failed"),
        };
        syncCeoModelModeControl();
        return null;
    }
}

async function saveCeoModelSelection(mode, modelKey = "") {
    const sessionId = String(activeSessionId() || "").trim();
    const nextMode = mode === "model" ? "model" : "chain";
    const nextKey = nextMode === "model" ? String(modelKey || "").trim() : "";
    if (!sessionId || S.ceoModelSelection.saving) return null;
    if (nextMode === "model" && !nextKey) return null;
    const current = ceoModelSelectionFor(sessionId) || S.ceoModelSelection;
    if (current.mode === nextMode && String(current.modelKey || "") === nextKey) {
        if (nextMode === "model") closeCeoModelModePanel();
        else {
            S.ceoModelSelection = { ...S.ceoModelSelection, pendingChainSwitch: false };
            syncCeoModelModeControl();
        }
        return current;
    }
    S.ceoModelSelection = { ...current, sessionId, saving: true, error: "" };
    syncCeoModelModeControl();
    try {
        const payload = await ApiClient.updateCeoSessionModelSelection(
            sessionId,
            nextMode === "model" ? { mode: "model", model_key: nextKey } : { mode: "chain" },
        );
        if (sessionId !== String(activeSessionId() || "").trim()) return null;
        const applied = applyCeoModelSelectionPayload(sessionId, payload);
        S.ceoModelSelection = { ...S.ceoModelSelection, pendingChainSwitch: false };
        // 选好固定模型即收起面板；切回模型链留在面板里继续看链。
        if (nextMode === "model") closeCeoModelModePanel();
        else syncCeoModelModeControl();
        // 固定模型会改变上下文窗口判定，用量表必须跟着重算。
        scheduleCeoComposerUsageRefresh({ immediate: true });
        showToast({
            title: "模型模式已更新",
            text: nextMode === "model" ? "本会话已固定使用所选模型" : "本会话已恢复全局模型链",
            kind: "success",
        });
        return applied;
    } catch (error) {
        if (sessionId !== String(activeSessionId() || "").trim()) return null;
        S.ceoModelSelection = {
            ...S.ceoModelSelection,
            sessionId,
            saving: false,
            error: String(error?.message || "save_failed"),
        };
        syncCeoModelModeControl();
        showToast({
            title: "模型模式保存失败",
            text: String(error?.message || "请稍后重试"),
            kind: "error",
        });
        return null;
    }
}

function ceoModelItemSupportsImage(item) {
    if (!item) return null;
    const raw = item.image_multimodal_enabled ?? item.imageMultimodalEnabled;
    if (raw === undefined || raw === null) return null;
    return raw === true || String(raw) === "true";
}

function modelChainMemberKeys(chain) {
    const groups = activeLoadBalanceGroups() || {};
    const keys = [];
    normalizeModelRoleChain(chain).forEach((ref) => {
        if (isGroupRef(ref)) {
            const group = groups[groupKeyFromRef(ref)] || {};
            (group.model_keys || []).forEach((key) => keys.push(String(key || "").trim()));
            return;
        }
        keys.push(String(ref || "").trim());
    });
    return keys.filter(Boolean);
}

function modelChainMixedImageCapability(chain) {
    const flags = modelChainMemberKeys(chain)
        .map((key) => ceoModelItemSupportsImage(ceoModelCatalogItem(key)))
        .filter((flag) => flag !== null);
    if (flags.length < 2) return false;
    return new Set(flags).size > 1;
}

function modelChainImageIncapableNames(chain) {
    const names = [];
    const seen = new Set();
    modelChainMemberKeys(chain).forEach((key) => {
        if (!key || seen.has(key)) return;
        seen.add(key);
        const item = ceoModelCatalogItem(key);
        if (ceoModelItemSupportsImage(item) === false) names.push(ceoModelDisplayTitle(item) || key);
    });
    return names;
}

async function saveCeoModelChain(keys) {
    const sessionId = String(activeSessionId() || "").trim();
    const modelKeys = normalizeModelRoleChain(keys);
    if (!sessionId || S.ceoModelSelection.saving) return null;
    // 草稿与此刻的全局链逐位相同就不必再存一份副本：发不带 model_keys 的 chain 载荷，后端摘掉
    // 会话链键，本会话继续随全局自动更新。在这份底稿上再动过手（增删/换位）才会落显式链。
    const followsGlobal = !modelKeys.length
        || modelKeys.join("\n") === normalizeModelRoleChain(ceoModelChainKeys()).join("\n");
    // 空数组是同一条 PATCH 的合法载荷：后端会摘掉会话链键，等于本会话恢复全局模型链，
    // 所以「把成员删空」有去处，不会变成一个点了没反应的按钮。
    S.ceoModelSelection = { ...S.ceoModelSelection, saving: true, error: "" };
    syncCeoModelModeControl();
    try {
        const payload = await ApiClient.updateCeoSessionModelSelection(
            sessionId,
            followsGlobal ? { mode: "chain" } : { mode: "chain", model_keys: modelKeys },
        );
        if (sessionId !== String(activeSessionId() || "").trim()) return null;
        const applied = applyCeoModelSelectionPayload(sessionId, payload);
        // 落盘成功就回到只读态：面板显示的必须是已经生效的那条链，草稿使命已完成。
        S.ceoModelSelection = {
            ...S.ceoModelSelection,
            pendingChainSwitch: false,
            chainEditing: false,
            dragFrom: -1,
            dropIndex: null,
        };
        // 退出编辑态必须重画：链栏要从"草稿"回到只读那一眼，页签也要跟着换标。
        syncCeoModelModeControl();
        showToast(followsGlobal
            ? { title: "本会话已恢复全局模型链", text: "这个会话不再自带模型链。", kind: "success" }
            : { title: "本会话模型链已保存", text: "这条链只作用于当前会话。", kind: "success" });
        // 链首会改变上下文窗口与图片能力判定，用量表必须跟着重算。
        scheduleCeoComposerUsageRefresh({ immediate: true });
        return applied;
    } catch (error) {
        // 保存失败回到生效中的那条链，避免面板显示一份没落盘的链。
        S.ceoModelSelection = {
            ...S.ceoModelSelection,
            saving: false,
            error: String(error?.message || "save_failed"),
            chainKeys: ceoModelChainBaseline(),
            dragFrom: -1,
            dropIndex: null,
        };
        syncCeoModelModeControl();
        showToast({ title: "本会话模型链保存失败", text: String(error?.message || "请稍后重试"), kind: "error" });
        return null;
    }
}

async function ensureCeoModelCatalog({ force = false } = {}) {
    // 目录只在启动、进「模型配置」页和点那颗刷新时重读；脑图标面板读的就是这份缓存。
    // force 是给面板打开用的：全局链可能在本页打开之后被模型页或另一个标签页改过，
    // 不重读就会把一条已经不生效的链标成「模型链（全局）」。
    if (S.modelCatalog.loading || (!force && (S.modelCatalog.catalog || []).length)) return;
    try {
        await loadModels();
    } catch (error) {
        void error;
    }
    // 只读态的草稿就是"正在生效的那条"，基线换了它必须跟着换；编辑态不动人的草稿。
    if (S.ceoModelSelection.panelOpen && !S.ceoModelSelection.chainEditing) {
        S.ceoModelSelection = { ...S.ceoModelSelection, chainKeys: ceoModelChainBaseline() };
    }
    renderCeoModelPicker();
    renderCeoModelChainPane();
}

function filterCeoModelPickerModels() {
    const query = String(S.ceoModelSelection.search || "").trim().toLowerCase();
    let catalog = [...(S.modelCatalog.catalog || [])];
    if (S.ceoModelSelection.pickerPurpose === "append") {
        // 加入模式只列还没在链里的配置：加两份同一个成员不是这条链支持的形态。
        const inChain = new Set(normalizeModelRoleChain(ceoModelChainDraft()));
        catalog = catalog.filter((item) => !inChain.has(String(item?.key || "").trim()));
    }
    const matched = query
        ? catalog.filter((item) => [ceoModelDisplayTitle(item), item.key, item.provider_model, item.name, item.description]
            .join("\n")
            .toLowerCase()
            .includes(query))
        : catalog;
    return matched.sort((left, right) => ceoModelDisplayTitle(left).localeCompare(ceoModelDisplayTitle(right), "zh-Hans-CN"));
}

function renderCeoModelPicker() {
    if (!U.ceoModelPickerList) return;
    const sessionId = String(activeSessionId() || "").trim();
    const selection = ceoModelSelectionFor(sessionId);
    if (!selection || !selection.panelOpen || !selection.pickerOpen) {
        U.ceoModelPickerList.innerHTML = "";
        return;
    }
    const appendMode = selection.pickerPurpose === "append";
    const pinnedKey = appendMode ? "" : String(selection.modelKey || "");
    const models = filterCeoModelPickerModels();
    if (U.ceoModelPickerEmpty) {
        U.ceoModelPickerEmpty.hidden = models.length > 0;
        U.ceoModelPickerEmpty.textContent = appendMode && !String(selection.search || "").trim()
            ? "全部模型配置都已在链里"
            : "没有匹配的模型配置";
    }
    U.ceoModelPickerList.innerHTML = models.map((item) => {
        const key = String(item.key || "").trim();
        const title = ceoModelDisplayTitle(item) || key;
        const subtitle = String(item.provider_model || "").trim() || key;
        const isDisabled = item.enabled === false;
        const isSelected = key === pinnedKey;
        return `
            <button type="button" class="ceo-model-picker-item${isSelected ? " is-selected" : ""}" role="option"
                aria-selected="${isSelected ? "true" : "false"}"
                aria-label="${appendMode ? esc(`加入模型链：${title}`) : esc(title)}" data-ceo-model-pick="${esc(key)}"${isDisabled ? " disabled" : ""}>
                <span class="ceo-model-picker-item-main">
                    <span class="ceo-model-picker-item-title">${esc(title)}</span>
                    <span class="ceo-model-picker-item-subtitle">${esc(subtitle)}</span>
                </span>
                ${isDisabled ? '<span class="ceo-model-picker-item-chip">已禁用</span>' : ""}
                <i data-lucide="${appendMode ? "plus" : "check"}" class="ceo-model-picker-item-check" aria-hidden="true"></i>
            </button>`;
    }).join("");
    icons();
}

function ceoModelChainDraft() {
    const keys = S.ceoModelSelection.chainKeys;
    return Array.isArray(keys) ? keys : [];
}

function setCeoModelChainDraft(keys) {
    S.ceoModelSelection = {
        ...S.ceoModelSelection,
        chainKeys: normalizeModelRoleChain(keys),
        dragFrom: -1,
        dropIndex: null,
    };
    renderCeoModelPicker();
    renderCeoModelChainPane();
    syncCeoModelModeControl();
}

function appendCeoModelChainMember(modelKey) {
    const key = String(modelKey || "").trim();
    if (!key || ceoModelChainDraft().some((item) => String(item) === key)) return;
    // 加完回到链那一栏：成员出现在链里才是反馈，把人留在目录列表里只会看到"点了没反应"。
    S.ceoModelSelection = {
        ...S.ceoModelSelection,
        chainKeys: normalizeModelRoleChain([...ceoModelChainDraft(), key]),
        pickerOpen: false,
        paneTouched: true,
        dragFrom: -1,
        dropIndex: null,
    };
    renderCeoModelPicker();
    renderCeoModelChainPane();
    syncCeoModelModeControl();
}

function removeCeoModelChainMember(modelKey) {
    const key = String(modelKey || "").trim();
    if (!key) return;
    setCeoModelChainDraft(ceoModelChainDraft().filter((item) => String(item) !== key));
}

function enterCeoModelChainEdit() {
    const sessionId = String(activeSessionId() || "").trim();
    if (!sessionId) return;
    const selection = ceoModelSelectionFor(sessionId) || S.ceoModelSelection;
    S.ceoModelSelection = {
        ...selection,
        sessionId,
        panelOpen: true,
        pickerOpen: false,
        paneTouched: true,
        chainEditing: true,
        // 已经在草稿上动过手就不能被"重新打种"冲掉；没动过时才用生效中的那条链起稿。
        chainKeys: ceoModelChainDirty() ? ceoModelChainDraft() : ceoModelChainBaseline(),
        dragFrom: -1,
        dropIndex: null,
    };
    syncCeoModelModeControl();
}

async function stageCeoModelChainGlobalSync() {
    if (S.ceoModelSelection.saving) return;
    await loadModels();
    setCeoModelChainDraft(ceoModelChainKeys());
}

function ceoModelChainBaseline() {
    // 会话链为空即跟随全局角色链，所以草稿的基线是「当前生效的那条」而不是「全局那条」。
    const sessionKeys = Array.isArray(S.ceoModelSelection.sessionChainKeys) ? S.ceoModelSelection.sessionChainKeys : [];
    return sessionKeys.length ? [...sessionKeys] : ceoModelChainKeys();
}

function ceoModelChainDirty() {
    const draft = ceoModelChainDraft();
    const serverKeys = ceoModelChainBaseline();
    return draft.length !== serverKeys.length || draft.some((key, index) => key !== serverKeys[index]);
}

function ceoModelEffectivePinnedKey(selection) {
    const mode = String(selection?.mode || "chain");
    const pinnedKey = mode === "model" ? String(selection?.modelKey || "") : "";
    if (!pinnedKey) return "";
    return selection?.pinnedAvailable === false ? "" : pinnedKey;
}

function ceoModelStalePinnedKey(selection) {
    const mode = String(selection?.mode || "chain");
    const pinnedKey = mode === "model" ? String(selection?.modelKey || "") : "";
    return pinnedKey && selection?.pinnedAvailable === false ? pinnedKey : "";
}

function renderCeoModelChainPane() {
    if (!U.ceoModelChainList) return;
    const sessionId = String(activeSessionId() || "").trim();
    const selection = ceoModelSelectionFor(sessionId);
    const visible = !!selection?.panelOpen && !selection?.pickerOpen;
    const editing = !!selection?.chainEditing;
    U.ceoModelChainList.innerHTML = "";
    if (U.ceoModelChainHint) U.ceoModelChainHint.hidden = true;
    if (U.ceoModelChainEmpty) U.ceoModelChainEmpty.hidden = true;
    if (U.ceoModelChainActions) U.ceoModelChainActions.hidden = true;
    if (!visible) return;
    const keys = ceoModelChainDraft();
    if (U.ceoModelChainActions) U.ceoModelChainActions.hidden = false;
    // 「编辑」是只读态唯一的入口，其余三颗只在编辑态里出现；行常驻不跳版。
    if (U.ceoModelChainEdit) {
        U.ceoModelChainEdit.hidden = editing;
        U.ceoModelChainEdit.disabled = !!selection?.saving;
    }
    if (U.ceoModelChainReset) U.ceoModelChainReset.hidden = !editing;
    if (U.ceoModelChainAdd) {
        U.ceoModelChainAdd.hidden = !editing;
        U.ceoModelChainAdd.disabled = !!selection?.saving;
    }
    if (U.ceoModelChainApply) {
        U.ceoModelChainApply.hidden = !editing;
        U.ceoModelChainApply.disabled = !editing || !!selection?.saving || !ceoModelChainDirty();
    }
    // 拖拽说明只在编辑态讲：只读态没有可拖的东西，写了就是噪声。
    if (U.ceoModelChainHint) U.ceoModelChainHint.hidden = !editing;
    if (U.ceoModelChainEmpty) U.ceoModelChainEmpty.hidden = keys.length > 0;
    U.ceoModelChainList.innerHTML = keys.map((key, index) => {
        const item = ceoModelCatalogItem(key);
        const title = ceoModelDisplayTitle(item) || key;
        const grip = editing ? '<span class="ceo-model-chain-grip" aria-hidden="true">&#9776;</span>' : "";
        const remove = editing
            ? `<button type="button" class="ceo-model-chain-remove" draggable="false"
                    data-ceo-chain-remove="${esc(key)}" aria-label="${esc(`从模型链移除 ${title}`)}">&times;</button>`
            : "";
        return `
            <article class="ceo-model-chain-row"${editing ? ' draggable="true"' : ""} role="listitem"
                data-ceo-chain-index="${index}" data-ceo-chain-key="${esc(key)}">
                ${grip}<span class="ceo-model-chain-title">${esc(title)}</span>${remove}
            </article>`;
    }).join("");
}

function clearCeoModelChainDragDecorations() {
    const list = U.ceoModelChainList;
    if (!list) return;
    list.querySelectorAll(".is-drop-target").forEach((item) => item.classList.remove("is-drop-target"));
    list.querySelectorAll(".is-dragging").forEach((item) => item.classList.remove("is-dragging"));
    list.querySelectorAll("[data-ceo-chain-placeholder]").forEach((item) => item.remove());
    list.classList.remove("is-drop-zone");
}

function ceoModelChainDropIndex(list, clientY) {
    const cards = [...list.querySelectorAll("[data-ceo-chain-index]")];
    for (const card of cards) {
        const rect = card.getBoundingClientRect();
        if (clientY < rect.top + (rect.height / 2)) return Number(card.dataset.ceoChainIndex);
    }
    return cards.length;
}

function beginCeoModelChainDrag(event) {
    const list = U.ceoModelChainList;
    const card = event.target instanceof Element ? event.target.closest("[data-ceo-chain-index]") : null;
    // 只读态不许拖：行上的 data-ceo-chain-index 两态都在，只靠"没有把手"不够。
    if (!list || !card || !S.ceoModelSelection.chainEditing || S.ceoModelSelection.saving) return;
    const index = Number(card.dataset.ceoChainIndex);
    if (!Number.isInteger(index) || index < 0) return;
    S.ceoModelSelection = { ...S.ceoModelSelection, dragFrom: index, dropIndex: null };
    card.classList.add("is-dragging");
    if (event.dataTransfer) {
        event.dataTransfer.effectAllowed = "move";
        try {
            event.dataTransfer.setData("text/plain", String(index));
        } catch (error) {
            void error;
        }
    }
}

function updateCeoModelChainDropTarget(event) {
    const list = U.ceoModelChainList;
    const from = Number(S.ceoModelSelection.dragFrom);
    if (!list || !Number.isInteger(from) || from < 0) return;
    event.preventDefault();
    if (event.dataTransfer) event.dataTransfer.dropEffect = "move";
    const targetIndex = ceoModelChainDropIndex(list, event.clientY);
    // 目标位置没变就不重排占位符，避免拖动时列表抖动。
    if (S.ceoModelSelection.dropIndex === targetIndex && list.querySelector("[data-ceo-chain-placeholder]")) return;
    clearCeoModelChainDragDecorations();
    S.ceoModelSelection = { ...S.ceoModelSelection, dropIndex: targetIndex };
    const cards = [...list.querySelectorAll("[data-ceo-chain-index]")];
    const dragging = cards.find((card) => Number(card.dataset.ceoChainIndex) === from);
    if (dragging) dragging.classList.add("is-dragging");
    const anchor = cards.find((card) => Number(card.dataset.ceoChainIndex) === targetIndex) || null;
    const placeholder = document.createElement("div");
    placeholder.className = "model-chain-drop-placeholder";
    placeholder.dataset.ceoChainPlaceholder = "1";
    if (anchor && anchor !== dragging) {
        anchor.classList.add("is-drop-target");
        list.insertBefore(placeholder, anchor);
    } else {
        list.appendChild(placeholder);
    }
    list.classList.add("is-drop-zone");
}

function finishCeoModelChainDrag(event) {
    event.preventDefault();
    const from = Number(S.ceoModelSelection.dragFrom);
    const targetIndex = S.ceoModelSelection.dropIndex;
    clearCeoModelChainDragDecorations();
    if (!Number.isInteger(from) || from < 0) return;
    const keys = [...ceoModelChainDraft()];
    const insertAt = Number.isInteger(targetIndex) && targetIndex > from ? targetIndex - 1 : targetIndex;
    const nextKeys = keys.filter((_key, index) => index !== from);
    if (Number.isInteger(insertAt)) {
        const bounded = Math.max(0, Math.min(nextKeys.length, insertAt));
        nextKeys.splice(bounded, 0, keys[from]);
    } else {
        nextKeys.push(keys[from]);
    }
    // 拖动只改草稿：点「应用」才写回模型链。
    S.ceoModelSelection = { ...S.ceoModelSelection, chainKeys: nextKeys, dragFrom: -1, dropIndex: null };
    renderCeoModelChainPane();
}

function ceoModelBadgeTitle(estimate) {
    const sessionId = String(activeSessionId() || "").trim();
    const effectiveKey = ceoModelEffectivePinnedKey(ceoModelSelectionFor(sessionId));
    // 绑定 key 优先：多条绑定可以共用同一个 provider 模型名，只按模型名解析必然撞名，
    // 显示出来的会是另一条绑定的标题。
    const fromEstimate = estimate
        ? (ceoModelUsageHeadlineTitle(estimate.resolved_model_key)
            || ceoModelUsageHeadlineTitle(estimate.provider_model))
        : "";
    return fromEstimate || ceoModelDisplayTitle(ceoModelCatalogItem(effectiveKey)) || "";
}

function syncCeoModelModePanelUsage() {
    const panel = U.ceoModelModePanel;
    const badge = U.ceoModelModeBadge;
    const fill = U.ceoModelModeUsageFill;
    const text = U.ceoModelModeUsageText;
    if (!badge || !fill || !text) return;
    const estimate = ceoCurrentUsageEstimate();
    const hasEstimate = !!estimate;
    const ratio = hasEstimate ? Math.max(0, Math.min(1, Number(estimate.ratio) || 0)) : 0;
    const visualRatio = hasEstimate && ratio > 0 ? Math.max(ratio, 0.06) : 0;
    const hue = Math.max(0, Math.min(145, 145 - (visualRatio * 145)));
    if (typeof panel?.style?.setProperty === "function") {
        panel.style.setProperty("--ceo-context-usage-color", `hsl(${hue.toFixed(1)} 82% 58%)`);
    }
    fill.style.width = `${Math.max(0, Math.min(100, visualRatio * 100))}%`;
    const title = ceoModelBadgeTitle(estimate);
    badge.textContent = title || "等待 Leader 上下文预估";
    badge.setAttribute?.("title", title);
    badge.classList?.toggle("is-pending", !title);
    // 进度条下方只留 token 占用值；没有数据时整行隐藏，不再重复脑图标上的占位文案。
    text.hidden = !hasEstimate;
    text.textContent = hasEstimate
        ? `${estimate.estimated_total_tokens}/${estimate.context_window_tokens} TOKEN`
        : "";
}

function ceoModelChainConfirmVisible(selection) {
    return !!selection?.pendingChainSwitch && !!selection?.panelOpen && !selection?.pickerOpen;
}

function syncCeoModelModeControl() {
    const sessionId = String(activeSessionId() || "").trim();
    let selection = ceoModelSelectionFor(sessionId);
    const panelOpen = !!selection?.panelOpen && !!sessionId;
    const effectiveKey = ceoModelEffectivePinnedKey(selection);
    // 板块跟随当前生效的模式：手动切过板块、或正在确认切回模型链时不自动拉回。
    if (panelOpen && !selection?.paneTouched && !selection?.pendingChainSwitch) {
        const nextPickerOpen = !!effectiveKey;
        if (!!selection?.pickerOpen !== nextPickerOpen) {
            S.ceoModelSelection = { ...S.ceoModelSelection, pickerOpen: nextPickerOpen };
            selection = ceoModelSelectionFor(sessionId);
        }
    }
    const staleKey = ceoModelStalePinnedKey(selection);
    const brain = U.ceoComposerUsageBrain;
    if (brain) {
        brain.classList.toggle("is-panel-open", panelOpen);
        brain.setAttribute("aria-expanded", panelOpen ? "true" : "false");
    }
    if (U.ceoModelModePanel) U.ceoModelModePanel.hidden = !panelOpen;
    if (U.ceoModelModeChain) {
        U.ceoModelModeChain.setAttribute("aria-checked", effectiveKey ? "false" : "true");
        // 页签自己回答「这条链是谁的」：没有会话链时列表就是全局链，并随全局自动更新。
        U.ceoModelModeChain.textContent = selection?.isSessionChain ? "模型链（此会话）" : "模型链（全局）";
    }
    if (U.ceoModelModePinned) U.ceoModelModePinned.setAttribute("aria-checked", effectiveKey ? "true" : "false");
    if (U.ceoModelPicker) U.ceoModelPicker.hidden = !(panelOpen && selection?.pickerOpen);
    if (U.ceoModelChainPane) U.ceoModelChainPane.hidden = !(panelOpen && !selection?.pickerOpen);
    if (U.ceoModelModeNote) {
        const note = staleKey
            ? "固定的模型已被删除或禁用，本会话已自动回退模型链。"
            : "";
        U.ceoModelModeNote.textContent = note;
        U.ceoModelModeNote.hidden = !note;
    }
    if (U.ceoModelChainConfirm) {
        const asking = ceoModelChainConfirmVisible(selection);
        U.ceoModelChainConfirm.hidden = !asking;
        if (U.ceoModelChainConfirmText) {
            const currentTitle = ceoModelDisplayTitle(ceoModelCatalogItem(effectiveKey)) || effectiveKey;
            U.ceoModelChainConfirmText.textContent = asking
                ? `当前会话固定使用 ${currentTitle}，切换到模型链？`
                : "";
        }
        if (U.ceoModelChainConfirmAccept) U.ceoModelChainConfirmAccept.disabled = !!selection?.saving;
    }
    if (panelOpen) syncCeoModelModePanelUsage();
    renderCeoModelPicker();
    renderCeoModelChainPane();
}

function openCeoModelModePanel() {
    const sessionId = String(activeSessionId() || "").trim();
    if (!sessionId) return;
    const current = ceoModelSelectionFor(sessionId) || S.ceoModelSelection;
    const effectiveKey = ceoModelEffectivePinnedKey(current);
    S.ceoModelSelection = {
        ...current,
        sessionId,
        panelOpen: true,
        // 打开即停在正在使用的板块：固定生效则直接进指定模型列表。
        pickerOpen: !!effectiveKey,
        paneTouched: false,
        pendingChainSwitch: false,
        search: "",
        // 打开永远先给一眼生效中的链，改动要靠点「编辑」。
        chainEditing: false,
        // 面板自己展开的 picker 永远是「选固定模型」，append 只由「加入模型」按钮进入。
        pickerPurpose: "pin",
        chainKeys: ceoModelChainBaseline(),
        dragFrom: -1,
        dropIndex: null,
    };
    if (U.ceoModelPickerSearch) U.ceoModelPickerSearch.value = "";
    syncCeoModelModeControl();
    if (S.ceoModelSelection.pickerOpen) U.ceoModelPickerSearch?.focus();
    void refreshCeoModelSelection(sessionId);
    // 每次打开都重读目录：面板显示的是"此刻生效的那条链"，不能是本页上次加载时的快照。
    void ensureCeoModelCatalog({ force: true });
}

function closeCeoModelModePanel() {
    if (!S.ceoModelSelection.panelOpen && !S.ceoModelSelection.pickerOpen) return false;
    S.ceoModelSelection = {
        ...S.ceoModelSelection,
        panelOpen: false,
        pickerOpen: false,
        paneTouched: false,
        pendingChainSwitch: false,
        search: "",
        chainEditing: false,
        dragFrom: -1,
        dropIndex: null,
    };
    syncCeoModelModeControl();
    return true;
}

function openCeoModelModePicker(purpose = "pin") {
    const sessionId = String(activeSessionId() || "").trim();
    if (!sessionId) return;
    S.ceoModelSelection = {
        ...S.ceoModelSelection,
        sessionId,
        panelOpen: true,
        pickerOpen: true,
        pickerPurpose: purpose === "append" ? "append" : "pin",
        paneTouched: true,
        pendingChainSwitch: false,
        search: "",
    };
    if (U.ceoModelPickerSearch) U.ceoModelPickerSearch.value = "";
    syncCeoModelModeControl();
    U.ceoModelPickerSearch?.focus();
    void ensureCeoModelCatalog();
}

function showCeoModelChainPane() {
    const sessionId = String(activeSessionId() || "").trim();
    if (!sessionId) return;
    const selection = ceoModelSelectionFor(sessionId) || S.ceoModelSelection;
    const effectiveKey = ceoModelEffectivePinnedKey(selection);
    S.ceoModelSelection = {
        ...selection,
        sessionId,
        panelOpen: true,
        pickerOpen: false,
        paneTouched: true,
        // 固定生效时先确认再切回模型链；本来就是模型链则无需确认。
        pendingChainSwitch: !!effectiveKey,
        search: "",
        // 板块之间来回切不能丢草稿：只有还没改过时才用生效中的那条链重新打种。
        chainKeys: ceoModelChainDirty() ? ceoModelChainDraft() : ceoModelChainBaseline(),
        dragFrom: -1,
        dropIndex: null,
    };
    syncCeoModelModeControl();
}

function cancelCeoModelChainSwitch() {
    if (!S.ceoModelSelection.pendingChainSwitch) return false;
    // 取消后回到正在使用的板块（固定生效时即指定模型列表）。
    S.ceoModelSelection = { ...S.ceoModelSelection, pendingChainSwitch: false, paneTouched: false };
    syncCeoModelModeControl();
    return true;
}

function confirmCeoModelChainSwitch() {
    if (!S.ceoModelSelection.pendingChainSwitch) return null;
    return saveCeoModelSelection("chain");
}

function bindCeoModelModeControls() {
    const brain = U.ceoComposerUsageBrain;
    brain?.addEventListener("click", (event) => {
        event.stopPropagation();
        if (S.ceoBrainHoldConsumedClick) {
            S.ceoBrainHoldConsumedClick = false;
            return;
        }
        if (S.ceoModelSelection.panelOpen) closeCeoModelModePanel();
        else openCeoModelModePanel();
    });
    brain?.addEventListener("pointerdown", (event) => beginCeoBrainHold(event));
    brain?.addEventListener("pointerup", () => finishCeoBrainHold());
    brain?.addEventListener("pointerleave", () => finishCeoBrainHold());
    brain?.addEventListener("pointercancel", () => finishCeoBrainHold());
    brain?.addEventListener("keydown", (event) => {
        if (event.key !== "Enter" && event.key !== " ") return;
        event.preventDefault();
        event.stopPropagation();
        if (S.ceoModelSelection.panelOpen) closeCeoModelModePanel();
        else openCeoModelModePanel();
    });
    U.ceoModelModeChain?.addEventListener("click", () => {
        showCeoModelChainPane();
    });
    U.ceoModelModePinned?.addEventListener("click", () => {
        openCeoModelModePicker();
    });
    U.ceoModelChainConfirmAccept?.addEventListener("click", () => {
        void confirmCeoModelChainSwitch();
    });
    U.ceoModelChainConfirmCancel?.addEventListener("click", () => {
        cancelCeoModelChainSwitch();
    });
    U.ceoModelPickerSearch?.addEventListener("input", () => {
        S.ceoModelSelection = { ...S.ceoModelSelection, search: String(U.ceoModelPickerSearch?.value || "") };
        renderCeoModelPicker();
    });
    U.ceoModelPickerList?.addEventListener("click", (event) => {
        const target = event.target instanceof Element ? event.target.closest("[data-ceo-model-pick]") : null;
        if (!target || target.disabled) return;
        // 加入模式会在处理中重画这份列表，事件目标随即脱离文档；而「点了面板外面就关面板」
        // 那条全局判定用的是 e.target.closest("#ceo-model-mode-panel")，目标一脱离就一律
        // 返回 null，于是这一下点击会被当成面板外，整个面板被关掉、草稿也被丢弃。
        // 所以这里必须截断冒泡，而不是指望渲染时机。
        event.stopPropagation();
        const pickedKey = target.getAttribute("data-ceo-model-pick");
        if (S.ceoModelSelection.pickerPurpose === "append") appendCeoModelChainMember(pickedKey);
        else void saveCeoModelSelection("model", pickedKey);
    });
    U.ceoModelChainList?.addEventListener("click", (event) => {
        const target = event.target instanceof Element ? event.target.closest("[data-ceo-chain-remove]") : null;
        if (!target) return;
        event.stopPropagation();
        removeCeoModelChainMember(target.getAttribute("data-ceo-chain-remove"));
    });
    U.ceoModelChainEdit?.addEventListener("click", (event) => {
        // 这三颗都会在处理中重画链栏，祖先那条「点了面板外就关面板」的全局判定会把这一下
        // 当成面板外，所以面板内的控件一律截断冒泡。
        event.stopPropagation();
        enterCeoModelChainEdit();
    });
    U.ceoModelChainReset?.addEventListener("click", (event) => {
        event.stopPropagation();
        void stageCeoModelChainGlobalSync();
    });
    U.ceoModelChainAdd?.addEventListener("click", (event) => {
        event.stopPropagation();
        openCeoModelModePicker("append");
    });
    U.ceoModelChainList?.addEventListener("dragstart", (event) => beginCeoModelChainDrag(event));
    U.ceoModelChainList?.addEventListener("dragover", (event) => updateCeoModelChainDropTarget(event));
    U.ceoModelChainList?.addEventListener("drop", (event) => finishCeoModelChainDrag(event));
    U.ceoModelChainList?.addEventListener("dragend", () => {
        clearCeoModelChainDragDecorations();
        S.ceoModelSelection = { ...S.ceoModelSelection, dragFrom: -1, dropIndex: null };
        renderCeoModelChainPane();
    });
    U.ceoModelChainApply?.addEventListener("click", () => {
        if (S.ceoModelSelection.saving || !ceoModelChainDirty()) return;
        const draft = ceoModelChainDraft();
        void confirmChainsImageCapabilityMix([draft]).then((confirmed) => {
            if (confirmed) void saveCeoModelChain(draft);
        });
    });
}
