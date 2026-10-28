// 模型页视图：角色链卡、模型列表/详情、组弹窗、上下文刷新弹窗与读数。

function syncModelDetailScopeToggles() {
    if (!U.modelDetail || S.modelCatalog.mode === "create") return;
    const selectedKey = String(S.modelCatalog.selectedModelKey || "").trim();
    if (!selectedKey) return;
    MODEL_SCOPES.forEach(({ key }) => {
        const input = U.modelDetail.querySelector(`[name="scope_${key}"]`);
        if (!(input instanceof HTMLInputElement)) return;
        const checked = modelScopeContains(key, selectedKey);
        input.checked = checked;
        input.closest(".role-toggle")?.classList.toggle("checked", checked);
    });
}

function renderModelHint() {
    if (S.modelCatalog.loading) return hint("正在加载模型配置...");
    if (S.modelCatalog.saving) return hint("正在保存...");
    if (S.modelCatalog.error) return hint(`模型配置错误：${S.modelCatalog.error}`, true);
    if (!S.modelCatalog.catalog.length) return hint("当前还没有配置，请先添加配置。", false);
    if (S.modelCatalog.rolesDirty) return hint("模型链有未保存的修改，点“保存”提交，或点“取消修改”放弃。", false);
    return hint("直接拖动模型到角色链、或从角色链拖回模型列表即可调整；改动后点“保存”提交。", false);
}

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
        <article class="model-chain-slide is-group-tile is-editing${members.length ? "" : " is-incomplete"}" draggable="true" data-model-chain-ref="${esc(groupRefToken(groupKey))}" data-scope="${esc(scopeKey)}" data-group-key="${esc(groupKey)}" data-group-edit="${esc(groupKey)}">
            <div class="model-chain-main">
                <span class="resource-list-title">${esc(groupKey)}</span>
                <span class="resource-list-subtitle">组内平级 · 按综合负载选成员 · 节点绑定后粘滞</span>
                <span class="model-inline-meta">
                    <span class="policy-chip risk-low">组</span>
                    ${index === 0 ? '<span class="policy-chip risk-low">链首</span>' : ""}
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

// 组弹窗的确定键：窗口里的内容一变就填重点色；落盘在飞时改文案并禁用，别让人再点一次。
// 每次字段变化都过这里，不等整块重绘——勾选与输入路径只重画局部，靠重绘会把颜色留在旧状态。
function syncLoadBalanceGroupConfirmAffordance() {
    const confirm = U.modelGroupConfirm;
    if (!confirm) return;
    const dialog = S.modelCatalog.groupDialog;
    confirm.classList.toggle("is-dirty", Boolean(dialog.open) && !dialog.saving && loadBalanceGroupDialogDirty());
    confirm.disabled = Boolean(dialog.saving);
    confirm.textContent = dialog.saving ? "保存中…" : "确定";
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
        syncLoadBalanceGroupConfirmAffordance();
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
    syncLoadBalanceGroupConfirmAffordance();
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
    syncLoadBalanceGroupConfirmAffordance();
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
        const memberChips = members.length
            ? members.map((item) => {
                const model = modelRefItem(item);
                return `<span class="policy-chip neutral" title="${esc(item)}">${esc(modelDisplayTitle(model) || item)}</span>`;
            }).join("")
            : '<span class="policy-chip risk-high">成员为空，保存会被拒绝</span>';
        // 整张卡都是打开配置的点击区，只有「删除」例外。
        return `
            <article class="model-group-item is-editing${members.length ? "" : " is-incomplete"}" draggable="true" data-model-group-key="${esc(key)}" data-model-group-ref="${esc(groupRefToken(key))}" data-group-edit="${esc(key)}">
                <div class="model-group-head">
                    <span class="model-group-name">${esc(key)}</span>
                    <span class="policy-chip neutral">${usedIn ? `已在 ${usedIn} 条链` : "未加入链"}</span>
                    <button type="button" class="toolbar-btn ghost small" data-group-delete="${esc(key)}">删除</button>
                </div>
                <div class="model-inline-meta">${memberChips}</div>
                ${group.enabled === false ? '<div class="model-inline-meta"><span class="policy-chip neutral">已禁用</span></div>' : ""}
            </article>`;
    }).join("");
}

function renderModelRoleEditors() {
    if (!U.modelRoleEditors) return;
    U.modelRoleEditors.innerHTML = MODEL_SCOPES.map((scope) => {
        const chain = modelScopeChain(scope.key);
        const maxIterations = modelScopeIterations(scope.key);
        const defaultText = chain.length ? `已配置 ${chain.length} 个模型` : "尚未配置";
        const chainMarkup = chain.length
            ? chain.map((ref, index) => {
                if (isGroupRef(ref)) {
                    return renderModelGroupChainTile(scope.key, groupKeyFromRef(ref), index);
                }
                const item = modelRefItem(ref);
                const modelKey = String(item?.key || ref).trim();
                const badges = [index === 0 ? '<span class="policy-chip risk-low">首选</span>' : ""];
                if (item?.enabled === false) badges.push('<span class="policy-chip neutral">已禁用</span>');
                if (!item) badges.push('<span class="policy-chip neutral">未托管</span>');
                return `
                    <article class="model-chain-slide is-editing" draggable="true" data-model-chain-ref="${esc(modelKey)}" data-scope="${scope.key}">
                        <button type="button" class="model-chain-handle" aria-label="拖动调整顺序"><span class="model-chain-grip" aria-hidden="true">&#9776;</span></button>
                        <button type="button" class="model-chain-main" data-model-open="${esc(modelKey)}">
                            <span class="resource-list-title">${esc(modelKey)}</span>
                            <span class="resource-list-subtitle">${esc(item?.provider_model || ref)}</span>
                            <span class="model-inline-meta">${badges.join("")}</span>
                        </button>
                        <button type="button" class="toolbar-btn ghost small" data-model-chain-action="remove" data-scope="${scope.key}" data-index="${index}">移除</button>
                    </article>`;
            }).join("")
            : `<div class="empty-state compact">把模型从下面的列表拖进来，即可编排当前角色链。</div>`;

        return `
            <section class="model-chain-card">
                <div class="panel-header">
                    <div>
                        <h3>${esc(scope.label)}</h3>
                        <p class="subtitle">${esc(defaultText)}</p>
                    </div>
                    <div class="model-chain-card-meta">
                        <span class="policy-chip neutral">${chain.length} 个候选</span>
                        <label class="model-role-iterations-field">
                            <span class="model-role-iterations-label">最大轮数</span>
                            <input
                                class="model-role-iterations-input spinless-number-input"
                                type="number"
                                min="2"
                                step="1"
                                inputmode="numeric"
                                value="${esc(String(maxIterations))}"
                                data-model-role-iterations="${scope.key}"
                            >
                        </label>
                    </div>
                </div>
                <div class="model-role-section">
                    <div class="model-role-section-title">当前角色链</div>
                    <div class="model-chain-list" data-model-chain-list="${scope.key}">${chainMarkup}</div>
                </div>
            </section>`;
    }).join("");
}

function renderModelList() {
    if (!U.modelList) return;
    const catalog = filterModels().sort((left, right) => String(left.key || "").localeCompare(String(right.key || "")));
    if (!catalog.length) {
        const emptyText = S.modelCatalog.search
            ? "没有匹配的模型，请调整搜索条件。"
            : "暂无可用配置，请先添加配置。";
        U.modelList.innerHTML = `<div class="empty-state compact">${emptyText}</div>`;
        return;
    }
    U.modelList.innerHTML = catalog.map((item) => {
        const usedScopes = MODEL_SCOPES.filter((scope) => modelScopeContains(scope.key, item.key));
        const usageMarkup = usedScopes.length
            ? '<span class="policy-chip neutral">已加入角色链</span>'
            : '<span class="policy-chip neutral">未加入角色链</span>';
        const stateChips = [usageMarkup];
        if (item.enabled === false) stateChips.push('<span class="policy-chip neutral">已禁用</span>');
        return `
            <article class="model-available-item ${usedScopes.length ? "is-in-chain" : ""}" draggable="true" data-model-available-key="${esc(item.key)}">
                <div class="model-shared-item-head">
                    <button type="button" class="model-available-main" data-model-open="${esc(item.key)}">
                        <span class="resource-list-title">${esc(item.key)}</span>
                        <span class="resource-list-subtitle">${esc(item.provider_model)}</span>
                    </button>
                    <button type="button" class="toolbar-btn ghost small" data-model-open="${esc(item.key)}">配置</button>
                </div>
                <div class="model-inline-meta">${stateChips.join("")}</div>
                <div class="resource-empty-copy">拖动当前模型到上方任意角色链即可加入。</div>
            </article>`;
    }).join("");
}

function renderModelDetail() {
    if (!U.modelDetail || !U.modelDetailEmpty) return;
    const isCreate = S.modelCatalog.mode === "create";
    const current = isCreate ? null : modelRefItem(S.modelCatalog.selectedModelKey);
    if (!isCreate && !current) {
        U.modelDetailEmpty.style.display = "none";
        U.modelDetail.innerHTML = "";
        setDrawerOpen(U.modelBackdrop, U.modelDrawer, false);
        return;
    }

    const enabled = isCreate ? true : !!current?.enabled;

    U.modelDetailEmpty.style.display = "none";
    setDrawerOpen(U.modelBackdrop, U.modelDrawer, true);
    U.modelDetail.innerHTML = `
        <article class="model-detail-card model-config-shell">
            <div class="detail-modal-header model-config-header">
                <div class="detail-modal-title">
                    <h2 id="model-detail-title">${isCreate ? "添加配置" : "配置详情"}</h2>
                    <p class="subtitle">${esc(isCreate ? "填写必填项后写入 .g3ku/config.json" : `${current.key} · ${current.provider_model}`)}</p>
                </div>
                <div class="detail-modal-actions">
                    <span class="policy-chip ${enabled ? "risk-low" : "neutral"}">${enabled ? "已启用" : "已禁用"}</span>
                    <button type="submit" form="model-detail-form" class="toolbar-btn success">保存</button>
                    <button type="button" class="toolbar-btn ghost" data-model-detail-cancel="1" data-modal-close>关闭</button>
                </div>
            </div>
            <div class="detail-modal-body model-config-body">
                <form id="model-detail-form" class="model-detail-form" data-mode="${isCreate ? "create" : "edit"}" data-model-key="${esc(current?.key || "")}">
                    <section class="resource-section">
                        <h3>基本信息</h3>
                        <div class="model-form-grid">
                            <label class="resource-field">
                                <span class="resource-field-label">配置名 / 绑定名 *</span>
                                <input class="resource-search" name="key" ${isCreate ? `value=""` : `value="${esc(current.key)}" disabled`} placeholder="如 openai_primary">
                            </label>
                            <label class="resource-field">
                                <span class="resource-field-label">Provider / Model *</span>
                                <input class="resource-search" name="providerModel" value="${esc(current?.provider_model || "")}" placeholder="如 openai:gpt-4.1">
                            </label>
                            <label class="resource-field">
                                <span class="resource-field-label">API Key *</span>
                                <input class="resource-search" name="apiKey" value="${esc(current?.api_key || "")}" placeholder="sk-...">
                            </label>
                            <label class="resource-field">
                                <span class="resource-field-label">Base URL ${isCreate ? "*" : ""}</span>
                                <input class="resource-search" name="apiBase" value="${esc(current?.api_base || "")}" placeholder="https://api.example.com/v1">
                            </label>
                        </div>
                        <p class="subtitle">每个配置只使用一个 API Key（单 key）；需要多个模型或容灾回退时，请添加多个配置并组成模型链。</p>
                        <div class="model-form-status-area" style="margin-top: var(--space-4);">
                            ${enabled
            ? `<button type="button" class="toolbar-btn danger" data-model-control="disable" data-key="${esc(current?.key || "")}">禁用模型</button>`
            : `<button type="button" class="toolbar-btn success" data-model-control="enable" data-key="${esc(current?.key || "")}">启用模型</button>`
        }
                            ${!isCreate ? `<button type="button" class="toolbar-btn ghost" data-model-control="delete" data-key="${esc(current?.key || "")}">删除模型</button>` : ""}
                            <input type="checkbox" name="enabled" ${enabled ? "checked" : ""} style="display:none">
                        </div>
                    </section>
                    <section class="resource-section">
                        <h3>模型参数</h3>
                        <div class="model-form-grid">
                            <label class="resource-field">
                                <span class="resource-field-label">最大上下文TOKEN *</span>
                                <input class="resource-search" type="number" min="25001" step="1" name="contextWindowTokens" value="${esc(String(current?.context_window_tokens ?? ""))}" placeholder="必须大于 25000">
                            </label>
                            <label class="resource-field">
                                <span class="resource-field-label">Max Tokens</span>
                                <input class="resource-search" type="number" min="1" step="1" name="maxTokens" value="${esc(String(current?.max_tokens ?? ""))}" placeholder="留空则不下发">
                            </label>
                            <label class="resource-field">
                                <span class="resource-field-label">Temperature</span>
                                <input class="resource-search" type="number" min="0" max="2" step="0.1" name="temperature" value="${esc(String(current?.temperature ?? ""))}" placeholder="留空则不下发">
                            </label>
                            <label class="resource-field">
                                <span class="resource-field-label">Reasoning Effort</span>
                                <input class="resource-search" name="reasoningEffort" value="${esc(current?.reasoning_effort || "")}" placeholder="留空则不下发">
                            </label>
                            <label class="resource-field">
                                <span class="resource-field-label">自动重试错误关键词(空格间隔)</span>
                                <input class="resource-search" name="retryOn" value="${esc((current?.retry_on || []).join(" "))}" placeholder="如 network 429 502（可自定义关键词，空格间隔）">
                            </label>
                            <label class="resource-field">
                                <span class="resource-field-label">重试次数</span>
                                <input class="resource-search spinless-number-input" type="number" min="0" step="1" name="retryCount" value="${esc(String(current?.retry_count ?? 0))}" placeholder="0" title="命中自动重试关键词时该模型的重试轮数预算（一轮 = 完整轮过该模型所有 Key），轮间按指数退避加抖动；填 0 就是不重试（只发首那一趟）。未命中关键词的错误每个 Key 只试一次即切换下一模型。">
                            </label>
                        </div>
                        <p class="subtitle">重试次数 = 命中自动重试关键词时该模型的重试轮数预算（一轮完整轮过所有 Key，轮间动态退避）；填 0 就是不重试（只发首那一趟），预算耗尽后切换下一模型，全链耗尽报错停止。</p>
                    </section>
                    <section class="resource-section">
                        <h3>额外请求头</h3>
                        <textarea class="resource-editor model-textarea" name="extraHeaders" rows="6" placeholder='{"X-Trace-Id": "demo"}'>${esc(current?.extra_headers ? JSON.stringify(current.extra_headers, null, 2) : "")}</textarea>
                    </section>
                    <div class="model-actions">
                        <button type="submit" class="toolbar-btn success">${isCreate ? "添加并保存" : "保存模型"}</button>
                        <button type="button" class="toolbar-btn ghost" data-model-detail-cancel="1">${isCreate ? "取消" : "关闭"}</button>
                    </div>
                </form>
            </div>
        </article>`;
}

function renderModelCatalog() {
    if (U.modelRefresh) U.modelRefresh.disabled = S.modelCatalog.loading || S.modelCatalog.saving;
    if (U.modelCreate) U.modelCreate.disabled = S.modelCatalog.loading || S.modelCatalog.saving;
    if (U.modelRolesCancel) {
        U.modelRolesCancel.hidden = !S.modelCatalog.rolesDirty;
        U.modelRolesCancel.disabled = S.modelCatalog.loading || S.modelCatalog.saving;
    }
    if (U.modelRolesSave) {
        U.modelRolesSave.disabled = S.modelCatalog.loading || S.modelCatalog.saving;
        U.modelRolesSave.hidden = !S.modelCatalog.rolesDirty;
        U.modelRolesSave.textContent = S.modelCatalog.saving ? "正在保存..." : "保存";
    }
    renderModelHint();
    renderModelGroupColumn();
    renderModelRoleEditors();
    renderModelList();
    renderModelDetail();
}

function renderRoleLimitControl({ scopeKey, kind, label, value, editing }) {
    const isFixedMemoryConcurrency = scopeKey === "memory" && kind === "concurrency";
    if (isFixedMemoryConcurrency) {
        return `
        <div class="model-role-limit-field" data-model-role-limit-kind="${esc(kind)}" data-model-role-limit-scope="${esc(scopeKey)}" data-model-role-fixed="1" data-model-role-fixed-value="1">
            <span class="model-role-iterations-label">${esc(label)}</span>
            <div class="llm-segmented-control model-role-limit-fixed-track" aria-hidden="true">
                <span class="llm-segmented-label model-role-limit-fixed-pill">固定为1</span>
            </div>
            <input type="hidden" value="1" data-model-role-limit-input="${esc(kind)}">
        </div>`;
    }
    const inputValue = value == null ? "-1" : String(value);
    return `
        <div class="model-role-limit-field" data-model-role-limit-kind="${esc(kind)}" data-model-role-limit-scope="${esc(scopeKey)}">
            <span class="model-role-iterations-label">${esc(label)}</span>
            <input class="model-role-limit-input spinless-number-input" type="number" min="-1" step="1" inputmode="numeric" value="${esc(inputValue)}" placeholder="-1" title="-1 代表不限制" ${editing ? "" : "disabled"} data-model-role-limit-input="${esc(kind)}">
        </div>`;
}

function roleLimitSummary(kind) {
    const customized = [];
    MODEL_SCOPES.forEach((scope) => {
        if (kind === "concurrency" && scope.key === "memory") return;
        const value = kind === "iterations" ? modelScopeIterations(scope.key) : modelScopeConcurrency(scope.key);
        if (value != null) customized.push(`${scope.label} ${value}`);
    });
    if (!customized.length) return "默认 -1 · 不限";
    return `${customized.join(" / ")}，其余 -1`;
}

function renderRoleLimitsBar() {
    const bar = U.modelRoleLimitsBar;
    if (!bar) return;
    bar.hidden = true;
    bar.innerHTML = "";
}
