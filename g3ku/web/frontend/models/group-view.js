// 负载均衡组视图：组列表、组链卡、组配置弹窗。
// 从 models/role-view.js 迁出；模型页现存的唯一组渲染源。

function loadBalanceGroupWindowSummary(members) {
    const windows = (members || [])
        .map((key) => {
            const item = modelRefItem(key);
            const raw = Number(item?.context_window_tokens ?? item?.contextWindowTokens ?? 0);
            return { key: String(key || "").trim(), window: Number.isFinite(raw) ? raw : 0 };
        })
        .filter((entry) => entry.key && entry.window > 0);
    if (!windows.length) return null;
    const sorted = [...windows].sort((left, right) => left.window - right.window);
    return {
        minKey: sorted[0].key,
        minWindow: sorted[0].window,
        maxWindow: sorted[sorted.length - 1].window,
        spread: sorted[sorted.length - 1].window - sorted[0].window,
    };
}

function renderLoadBalanceGroupWindowAlignment(members) {
    const summary = loadBalanceGroupWindowSummary(members);
    const imageBlockers = (members || [])
        .map((key) => modelRefItem(key))
        .filter((item) => item && ceoModelItemSupportsImage(item) === false)
        .map((item) => String(item.key || "").trim())
        .filter(Boolean);
    const chips = [];
    if (summary) {
        // 数字保持与「编辑模型」里那个窗口输入框同形（裸整数），这台机器的 locale 会给千分位，
        // 显示层不复算格式，两处读数才对得上。
        const tone = summary.spread > 0 ? "risk-high" : "neutral";
        chips.push(
            `<span class="policy-chip ${tone}" title="对齐到最小那颗 ${esc(summary.minKey)}=${summary.minWindow}；组内最大 ${summary.maxWindow}">组内最大上下文对齐: ${summary.minWindow}</span>`,
        );
    }
    // 多模态现在按"这一发落到的那颗"判，混勾的组里带图请求会不会看图取决于粘滞到哪一颗：
    // 这层不确定性要在勾成员的那一刻就看见，但不改任何行为。
    if (imageBlockers.length) {
        chips.push(
            `<span class="policy-chip risk-high" title="${esc(imageBlockers.join(" "))}">组内 ${imageBlockers.length} 颗不收图</span>`,
        );
    }
    return chips.join("");
}

function renderModelGroupChainTile(scopeKey, groupKey, index) {
    const group = activeLoadBalanceGroups()[groupKey] || {};
    const members = group.model_keys || [];
    return `
        <article class="model-chain-slide is-group-tile is-editing${members.length ? "" : " is-incomplete"}" draggable="true" data-model-chain-ref="${esc(groupRefToken(groupKey))}" data-scope="${esc(scopeKey)}" data-group-key="${esc(groupKey)}" data-group-edit="${esc(groupKey)}" title="组内平级 · 按综合负载选成员 · 节点绑定后粘滞">
            <span class="model-chain-index" aria-label="链序 ${index + 1}">${index + 1}</span>
            <div class="model-chain-main">
                <span class="resource-list-title">${esc(groupKey)}</span>
                <span class="resource-list-subtitle">组内平级 · 按负载选成员</span>
                <span class="model-inline-meta">
                    <span class="policy-chip risk-low">组</span>
                    ${group.enabled === false ? '<span class="policy-chip neutral">已禁用</span>' : ""}
                    ${!members.length ? '<span class="policy-chip risk-high">成员为空，保存会被拒绝</span>' : ""}
                </span>
            </div>
            <button type="button" class="model-chain-remove" data-model-chain-action="remove" data-scope="${esc(scopeKey)}" data-index="${index}" title="移出链" aria-label="移出链"><i data-lucide="x"></i></button>
        </article>`;
}

function loadBalanceGroupUsageCounts() {
    const counts = {};
    const roles = activeModelRoles() || {};
    Object.keys(roles).forEach((scope) => {
        (roles[scope] || []).forEach((ref) => {
            if (!isGroupRef(ref)) return;
            const key = groupKeyFromRef(ref);
            counts[key] = (counts[key] || 0) + 1;
        });
    });
    return counts;
}

function loadBalanceGroupCatalogOptions() {
    return (S.modelCatalog.catalog || [])
        .filter((item) => String(item?.key || "").trim())
        .sort((left, right) => String(left.key).localeCompare(String(right.key)));
}

function renderModelGroupDialog() {
    const backdrop = U.modelGroupBackdrop;
    const body = U.modelGroupDialogBody;
    if (!backdrop || !body) return;
    const dialog = S.modelCatalog.groupDialog;
    if (!dialog.open) {
        backdrop.hidden = true;
        backdrop.classList.remove("is-open");
        U.modelGroupDialog?.setAttribute("aria-hidden", "true");
        body.innerHTML = "";
        return;
    }
    if (U.modelGroupTitle) U.modelGroupTitle.textContent = dialog.editingKey ? "编辑负载均衡组" : "新建负载均衡组";
    const selected = new Set(dialog.modelKeys);
    const memberOptions = loadBalanceGroupCatalogOptions().map((item) => {
        const title = modelDisplayTitle(item);
        return `
        <label class="model-group-member-option${selected.has(item.key) ? " is-selected" : ""}">
            <input type="checkbox" class="ui-checkbox-input" data-group-dialog-member="${esc(item.key)}" ${selected.has(item.key) ? "checked" : ""}>
            <span class="ui-checkbox-box" aria-hidden="true"></span>
            <span title="${esc(item.key)}">${esc(title)}</span>
        </label>`;
    }).join("");
    body.innerHTML = `
        <label class="resource-field">
            <span class="resource-field-label">组名</span>
            <input class="resource-search" type="text" value="${esc(dialog.name)}" data-group-dialog-name placeholder="组名">
        </label>
        <label class="resource-field">
            <span class="resource-field-label">每成员重试次数</span>
            <input class="resource-search spinless-number-input" type="number" min="0" step="1" inputmode="numeric" value="${esc(String(dialog.maxRetryRounds))}" data-group-dialog-rounds>
        </label>
        <div class="model-inline-meta" data-group-dialog-windows>${renderLoadBalanceGroupWindowAlignment(dialog.modelKeys)}</div>
        <div class="resource-field">
            <span class="resource-field-label" data-group-dialog-count>成员模型（${selected.size}）</span>
            <div class="model-group-dialog-members">${memberOptions || '<span class="policy-chip neutral">没有可选模型</span>'}</div>
        </div>
        ${dialog.error ? `<p class="model-group-dialog-error" role="alert" data-group-dialog-error>${esc(dialog.error)}</p>` : ""}`;
    backdrop.hidden = false;
    backdrop.classList.add("is-open");
    U.modelGroupDialog?.setAttribute("aria-hidden", "false");
}

function syncLoadBalanceGroupDialogSummary() {
    const body = U.modelGroupDialogBody;
    const dialog = S.modelCatalog.groupDialog;
    if (!body || !dialog.open) return;
    const count = body.querySelector("[data-group-dialog-count]");
    if (count) count.textContent = `成员模型（${dialog.modelKeys.length}）`;
    // 这行跟着勾选走，且放在成员列表上方：压低整组上限的那一刻正是在下面勾选的那一颗，
    // 读数要在手的下一步之前出现，而不是等出事那天再回头翻配置。
    const windows = body.querySelector("[data-group-dialog-windows]");
    if (windows) windows.innerHTML = renderLoadBalanceGroupWindowAlignment(dialog.modelKeys);
    if (!dialog.error) body.querySelector("[data-group-dialog-error]")?.remove();
}

function renderModelGroupColumn() {
    if (!U.modelGroupList) return;
    if (U.modelGroupCreate) U.modelGroupCreate.disabled = S.modelCatalog.loading || S.modelCatalog.saving;
    const groups = activeLoadBalanceGroups();
    const usage = loadBalanceGroupUsageCounts();
    const keys = Object.keys(groups).sort((left, right) => String(left).localeCompare(String(right)));
    if (!keys.length) {
        U.modelGroupList.innerHTML = '<div class="empty-state compact">还没有负载均衡组，点“新建组”配置。</div>';
        return;
    }
    U.modelGroupList.innerHTML = keys.map((key) => {
        const group = groups[key] || {};
        const members = group.model_keys || [];
        const usedIn = usage[key] || 0;
        const shown = members.slice(0, 3);
        const memberChips = members.length
            ? shown.map((item) => {
                const model = modelRefItem(item);
                return `<span class="policy-chip neutral" title="${esc(item)}">${esc(modelDisplayTitle(model) || item)}</span>`;
            }).join("")
                + (members.length > shown.length
                    ? `<span class="policy-chip neutral" title="${esc(members.join(" "))}">+${members.length - shown.length}</span>`
                    : "")
            : '<span class="policy-chip risk-high">成员为空，保存会被拒绝</span>';
        // 整张卡都是打开配置的点击区，只有「删除」例外。
        return `
            <article class="model-group-item is-editing${members.length ? "" : " is-incomplete"}" draggable="true" data-model-group-key="${esc(key)}" data-model-group-ref="${esc(groupRefToken(key))}" data-group-edit="${esc(key)}">
                <div class="model-group-head">
                    <span class="model-group-name">${esc(key)}</span>
                    <span class="policy-chip neutral">${usedIn ? `已在 ${usedIn} 条链` : "未加入链"}</span>
                    <button type="button" class="toolbar-btn ghost small" data-group-delete="${esc(key)}">删除</button>
                </div>
                <div class="model-inline-meta" title="组内平级 · 按综合负载选成员 · 节点绑定后粘滞">${memberChips}</div>
                ${group.enabled === false ? '<div class="model-inline-meta"><span class="policy-chip neutral">已禁用</span></div>' : ""}
            </article>`;
    }).join("");
}
