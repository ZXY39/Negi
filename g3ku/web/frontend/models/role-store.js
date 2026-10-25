// 模型页角色链状态：scope 解析、别名改名、目录签名去重、route_entries 顺序与草稿。
// 渲染与交互在 org_graph_app.js；本文件不碰 DOM。

function modelScopeLabel(scope) {
    return (MODEL_SCOPES.find((item) => item.key === scope) || { label: String(scope || "") }).label;
}

function missingRequiredModelRoleLabels(source = "draft") {
    return MODEL_SCOPES
        .filter((item) => item.key !== "memory")
        .map((item) => item.key)
        .filter((scope) => modelScopeChain(scope, source).length < 1)
        .map((scope) => modelScopeLabel(scope));
}

function requiredModelRoleValidationMessage(source = "draft") {
    const labels = missingRequiredModelRoleLabels(source);
    if (!labels.length) return "";
    return `请先为以下角色配置模型链：${labels.join("、")}。全部拖入后才可保存。`;
}

function modelRefItem(ref) {
    const raw = String(ref || "").trim();
    if (!raw) return null;
    return S.modelCatalog.catalog.find((item) => String(item.key || "").trim() === raw || String(item.provider_model || "").trim() === raw) || null;
}

function modelRefEquivalent(left, right) {
    const leftRaw = String(left || "").trim();
    const rightRaw = String(right || "").trim();
    if (!leftRaw || !rightRaw) return false;
    if (leftRaw === rightRaw) return true;
    const leftItem = modelRefItem(leftRaw);
    const rightItem = modelRefItem(rightRaw);
    if (leftItem && rightItem) return String(leftItem.key || "") === String(rightItem.key || "");
    if (leftItem) return rightRaw === String(leftItem.key || "") || rightRaw === String(leftItem.provider_model || "");
    if (rightItem) return leftRaw === String(rightItem.key || "") || leftRaw === String(rightItem.provider_model || "");
    return false;
}

function activeModelRoles() {
    return S.modelCatalog.roleEditing ? S.modelCatalog.roleDrafts : S.modelCatalog.roles;
}

function activeRoleIterations() {
    return S.modelCatalog.roleEditing ? S.modelCatalog.roleIterationDrafts : S.modelCatalog.roleIterations;
}

function activeRoleConcurrency() {
    return S.modelCatalog.roleEditing ? S.modelCatalog.roleConcurrencyDrafts : S.modelCatalog.roleConcurrency;
}

function modelScopeChain(scope, source = "active") {
    const roles = source === "draft"
        ? S.modelCatalog.roleDrafts
        : source === "committed"
            ? S.modelCatalog.roles
            : activeModelRoles();
    return Array.isArray(roles?.[scope]) ? [...roles[scope]] : [];
}

function modelScopeIterations(scope, source = "active") {
    const iterations = source === "draft"
        ? S.modelCatalog.roleIterationDrafts
        : source === "committed"
            ? S.modelCatalog.roleIterations
            : activeRoleIterations();
    const raw = iterations?.[scope];
    if (raw == null || String(raw).trim() === "") return null;
    const value = Number(raw);
    return Number.isInteger(value) && value >= 0 ? value : null;
}

function modelScopeConcurrency(scope, source = "active") {
    const concurrency = source === "draft"
        ? S.modelCatalog.roleConcurrencyDrafts
        : source === "committed"
            ? S.modelCatalog.roleConcurrency
            : activeRoleConcurrency();
    const raw = concurrency?.[scope];
    if (raw == null || String(raw).trim() === "") return null;
    const value = Number(raw);
    return Number.isInteger(value) && value >= 0 ? value : null;
}

function modelScopeContains(scope, ref, source = "active") {
    return modelScopeChain(scope, source).some((item) => modelRefEquivalent(item, ref));
}

function cloneLoadBalanceGroups(groups) {
    const source = groups && typeof groups === "object" ? groups : {};
    const next = {};
    Object.keys(source).forEach((key) => {
        const groupKey = String(key || "").trim();
        const payload = source[key] && typeof source[key] === "object" ? source[key] : {};
        if (!groupKey) return;
        const members = Array.isArray(payload.model_keys || payload.modelKeys)
            ? (payload.model_keys || payload.modelKeys).map((item) => String(item || "").trim()).filter(Boolean)
            : [];
        const rawRounds = payload.max_retry_rounds ?? payload.maxRetryRounds;
        const rounds = Number.isFinite(Number(rawRounds)) && Number(rawRounds) >= 0
            ? Math.trunc(Number(rawRounds))
            : 1;
        next[groupKey] = {
            enabled: payload.enabled !== false,
            max_retry_rounds: rounds,
            model_keys: members,
        };
    });
    return next;
}

function activeLoadBalanceGroups() {
    return S.modelCatalog.roleEditing
        ? (S.modelCatalog.loadBalanceGroupDrafts || {})
        : (S.modelCatalog.loadBalanceGroups || {});
}

function loadBalanceGroupsEqual(left, right) {
    const a = cloneLoadBalanceGroups(left);
    const b = cloneLoadBalanceGroups(right);
    const keys = new Set([...Object.keys(a), ...Object.keys(b)]);
    return [...keys].every((key) => JSON.stringify(a[key] || null) === JSON.stringify(b[key] || null));
}

function chainToRouteEntries(chain) {
    return (chain || []).map((ref) => {
        if (isGroupRef(ref)) return { type: "load_balance", group_key: groupKeyFromRef(ref) };
        return { type: "model", model_key: String(ref || "").trim() };
    }).filter((entry) => entry.group_key || entry.model_key);
}

function chainUsesGroup(chain) {
    return (chain || []).some((ref) => isGroupRef(ref));
}

function normalizeModelRoleChain(refs) {
    const normalized = [];
    (refs || []).forEach((ref) => {
        const raw = String(ref || "").trim();
        if (!raw) return;
        const item = modelRefItem(raw);
        const target = String(item?.key || raw).trim();
        if (!target || normalized.some((existing) => modelRefEquivalent(existing, target))) return;
        normalized.push(target);
    });
    return normalized;
}

function normalizeAllModelRoles(roles = EMPTY_MODEL_ROLES()) {
    const next = EMPTY_MODEL_ROLES();
    MODEL_SCOPES.forEach(({ key }) => {
        next[key] = normalizeModelRoleChain(Array.isArray(roles?.[key]) ? roles[key] : []);
    });
    return next;
}

function normalizeRoleIterations(iterations = DEFAULT_ROLE_ITERATIONS()) {
    return cloneRoleIterations(iterations);
}

function normalizeRoleConcurrency(concurrency = DEFAULT_ROLE_CONCURRENCY()) {
    return cloneRoleConcurrency(concurrency);
}

function modelRolesEqual(left, right) {
    return MODEL_SCOPES.every(({ key }) => {
        const leftChain = normalizeModelRoleChain(left?.[key] || []);
        const rightChain = normalizeModelRoleChain(right?.[key] || []);
        if (leftChain.length !== rightChain.length) return false;
        return leftChain.every((item, index) => modelRefEquivalent(item, rightChain[index]));
    });
}

function modelRoleIterationsEqual(left, right) {
    const leftNormalized = normalizeRoleIterations(left);
    const rightNormalized = normalizeRoleIterations(right);
    return MODEL_SCOPES.every(({ key }) => leftNormalized[key] === rightNormalized[key]);
}

function modelRoleConcurrencyEqual(left, right) {
    const leftNormalized = normalizeRoleConcurrency(left);
    const rightNormalized = normalizeRoleConcurrency(right);
    return MODEL_SCOPES.every(({ key }) => leftNormalized[key] === rightNormalized[key]);
}

// 组弹窗的「窗口内容变了没」：与打开弹窗时那份基线比，而不是与服务端比——
// 编辑已有组时基线就是那组当前值，新建时基线是刚给的默认名与默认轮数。
function loadBalanceGroupDialogDirty() {
    const dialog = S.modelCatalog.groupDialog;
    if (!dialog || !dialog.open) return false;
    const baseline = dialog.baseline || { name: "", rounds: "", modelKeys: [] };
    if (String(dialog.name || "").trim() !== String(baseline.name || "").trim()) return true;
    if (String(dialog.maxRetryRounds ?? "").trim() !== String(baseline.rounds ?? "").trim()) return true;
    const current = [...new Set((dialog.modelKeys || []).map((item) => String(item || "").trim()).filter(Boolean))].sort();
    const stored = [...new Set((baseline.modelKeys || []).map((item) => String(item || "").trim()).filter(Boolean))].sort();
    return JSON.stringify(current) !== JSON.stringify(stored);
}

// 组改名/删组要同时改写链上的 `group:` 记号：提交的链里留着已经不存在的组，
// 运行时的 route plan 会静默少一跳（`build_model_route_plan` 对查不到的组直接 continue）。
function rewriteGroupTokensInChain(chain, rewrites) {
    const entries = Array.isArray(chain) ? chain : [];
    const map = rewrites && typeof rewrites === "object" ? rewrites : {};
    const next = [];
    entries.forEach((ref) => {
        if (!isGroupRef(ref)) {
            next.push(ref);
            return;
        }
        const groupKey = groupKeyFromRef(ref);
        if (!(groupKey in map)) {
            next.push(ref);
            return;
        }
        const target = String(map[groupKey] || "").trim();
        if (target) next.push(groupRefToken(target));
    });
    return [...new Set(next.filter(Boolean))];
}

function syncModelRoleDraftState() {
    const rolesChanged = !modelRolesEqual(S.modelCatalog.roleDrafts, S.modelCatalog.roles)
        || !loadBalanceGroupsEqual(S.modelCatalog.loadBalanceGroupDrafts, S.modelCatalog.loadBalanceGroups);
    const iterationsChanged = !modelRoleIterationsEqual(S.modelCatalog.roleIterationDrafts, S.modelCatalog.roleIterations);
    const concurrencyChanged = !modelRoleConcurrencyEqual(S.modelCatalog.roleConcurrencyDrafts, S.modelCatalog.roleConcurrency);
    S.modelCatalog.rolesDirty = !!S.modelCatalog.roleEditing && (rolesChanged || iterationsChanged || concurrencyChanged);
    // 头部那颗「保存 / 取消修改」跟着这颗脏位走：所有改动都从这里收口，
    // 所以不必等整页重绘（拖动与输入路径只重画局部，头部会留在旧状态）。
    const save = document.getElementById("model-roles-save-btn");
    if (save) save.hidden = !S.modelCatalog.rolesDirty;
    const cancel = document.getElementById("model-roles-cancel-btn");
    if (cancel) cancel.hidden = !S.modelCatalog.rolesDirty;
}

function modelCatalogHeadersKey(headers) {
    if (!headers || typeof headers !== "object" || Array.isArray(headers)) return "";
    return Object.entries(headers)
        .map(([key, value]) => [String(key), String(value)])
        .sort(([left], [right]) => left.localeCompare(right))
        .map(([key, value]) => `${key}:${value}`)
        .join("\n");
}

function modelCatalogSignature(item) {
    if (!item || typeof item !== "object") return "";
    return [
        String(item.provider_model || "").trim(),
        String(item.api_key || "").trim(),
        String(item.api_base || "").trim(),
        modelCatalogHeadersKey(item.extra_headers),
    ].join("\n");
}

function remapModelRef(ref, aliasMap = {}) {
    const raw = String(ref || "").trim();
    if (!raw) return "";
    return String(aliasMap[raw] || raw).trim();
}

function remapModelRefs(refs, aliasMap = {}) {
    const normalized = [];
    (refs || []).forEach((ref) => {
        const target = remapModelRef(ref, aliasMap);
        if (!target || normalized.some((item) => item === target)) return;
        normalized.push(target);
    });
    return normalized;
}

function routeTokenChainFromEntries(entries) {
    return (Array.isArray(entries) ? entries : [])
        .map((entry) => {
            const type = String(entry?.type || "model").trim();
            if (type === "load_balance") {
                return groupRefToken(entry?.group_key || entry?.groupKey);
            }
            return String(entry?.model_key || entry?.modelKey || "").trim();
        })
        .filter(Boolean);
}

function normalizeCatalogEntries(items) {
    const catalog = [];
    const aliasMap = {};
    const signatureToKey = new Map();
    (items || []).forEach((item) => {
        if (!item || typeof item !== "object") return;
        const key = String(item.key || "").trim();
        const providerModel = String(item.provider_model || "").trim();
        const signature = modelCatalogSignature(item) || key || providerModel;
        if (!signature) return;
        const canonicalKey = signatureToKey.get(signature);
        if (canonicalKey) {
            if (key && key !== canonicalKey) aliasMap[key] = canonicalKey;
            return;
        }
        const nextKey = key || providerModel;
        if (!nextKey) return;
        signatureToKey.set(signature, nextKey);
        catalog.push({ ...item, key: nextKey });
    });
    return { catalog, aliasMap };
}

function applyModelCatalog(data, { preserveRoleDrafts = false } = {}) {
    const payload = data && typeof data === "object" ? data : {};
    const { catalog, aliasMap } = normalizeCatalogEntries(Array.isArray(payload.catalog) ? payload.catalog : []);
    const rolesPayload = payload.roles && typeof payload.roles === "object" ? payload.roles : {};
    const roleIterationsPayload = payload.roleIterations && typeof payload.roleIterations === "object"
        ? payload.roleIterations
        : payload.role_iterations && typeof payload.role_iterations === "object"
            ? payload.role_iterations
            : {};
    const roleConcurrencyPayload = payload.roleConcurrency && typeof payload.roleConcurrency === "object"
        ? payload.roleConcurrency
        : payload.role_concurrency && typeof payload.role_concurrency === "object"
            ? payload.role_concurrency
            : {};
    const nextRoles = EMPTY_MODEL_ROLES();
    const routeEntriesPayload = payload.routeEntries && typeof payload.routeEntries === "object"
        ? payload.routeEntries
        : (payload.route_entries && typeof payload.route_entries === "object" ? payload.route_entries : {});
    MODEL_SCOPES.forEach(({ key }) => {
        const entries = Array.isArray(routeEntriesPayload[key]) ? routeEntriesPayload[key] : null;
        if (entries) {
            // route_entries 才是 fallback 顺序；`group:` 记号不是模型 key，不参与改名映射。
            nextRoles[key] = routeTokenChainFromEntries(entries);
            return;
        }
        nextRoles[key] = remapModelRefs(
            Array.isArray(rolesPayload[key])
                ? rolesPayload[key].map((item) => String(item || "").trim()).filter(Boolean)
                : [],
            aliasMap,
        );
    });
    S.modelCatalog.items = Array.isArray(payload.items)
        ? remapModelRefs(payload.items.map((item) => String(item || "").trim()).filter(Boolean), aliasMap)
        : [];
    S.modelCatalog.catalog = catalog;
    S.modelCatalog.roles = normalizeAllModelRoles(nextRoles);
    S.modelCatalog.loadBalanceGroups = cloneLoadBalanceGroups(payload.loadBalanceGroups || payload.load_balance_groups);
    S.modelCatalog.roleIterations = normalizeRoleIterations(roleIterationsPayload);
    S.modelCatalog.roleConcurrency = normalizeRoleConcurrency(roleConcurrencyPayload);
    if (preserveRoleDrafts && S.modelCatalog.roleEditing) {
        S.modelCatalog.roleDrafts = normalizeAllModelRoles(S.modelCatalog.roleDrafts);
        S.modelCatalog.roleIterationDrafts = normalizeRoleIterations(S.modelCatalog.roleIterationDrafts);
        S.modelCatalog.roleConcurrencyDrafts = normalizeRoleConcurrency(S.modelCatalog.roleConcurrencyDrafts);
        syncModelRoleDraftState();
    } else {
        S.modelCatalog.roleDrafts = cloneModelRoles(S.modelCatalog.roles);
        S.modelCatalog.roleIterationDrafts = cloneRoleIterations(S.modelCatalog.roleIterations);
        S.modelCatalog.roleConcurrencyDrafts = cloneRoleConcurrency(S.modelCatalog.roleConcurrency);
        S.modelCatalog.loadBalanceGroupDrafts = cloneLoadBalanceGroups(S.modelCatalog.loadBalanceGroups);
        S.modelCatalog.roleEditing = false;
        S.modelCatalog.rolesDirty = false;
    }
    S.modelCatalog.defaults = { ...DEFAULT_MODEL_DEFAULTS(), ...(payload.defaults || {}) };
    if (S.modelCatalog.mode !== "create") {
        const selectedKey = remapModelRef(S.modelCatalog.selectedModelKey, aliasMap);
        S.modelCatalog.selectedModelKey = selectedKey;
        if (selectedKey && !S.modelCatalog.catalog.some((item) => String(item.key || "").trim() === selectedKey)) {
            S.modelCatalog.selectedModelKey = "";
        }
    }
}

function filterModels() {
    const q = String(S.modelCatalog.search || "").trim().toLowerCase();
    if (!q) return [...S.modelCatalog.catalog];
    return S.modelCatalog.catalog.filter((item) => [item.key, item.provider_model, item.description].join("\n").toLowerCase().includes(q));
}
