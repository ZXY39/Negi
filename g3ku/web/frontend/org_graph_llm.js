(() => {
  const LABELS = { ceo: "CEO", execution: "Execution", inspection: "Inspection" };
  const RETRY_ON = ["network", "429", "5xx"];
  const EMPTY_MEMORY = () => ({ embedding_model_key: "", rerank_model_key: "" });

  function state() {
    if (!S.llm || typeof S.llm !== "object") S.llm = {};
    const x = S.llm;
    x.loading = !!x.loading;
    x.saving = !!x.saving;
    x.error = String(x.error || "");
    x.templates = Array.isArray(x.templates) ? x.templates : [];
    x.templateMap = x.templateMap && typeof x.templateMap === "object" ? x.templateMap : {};
    x.templateDetailMap = x.templateDetailMap && typeof x.templateDetailMap === "object" ? x.templateDetailMap : {};
    x.configs = Array.isArray(x.configs) ? x.configs : [];
    x.configMap = x.configMap && typeof x.configMap === "object" ? x.configMap : {};
    x.configDetailMap = x.configDetailMap && typeof x.configDetailMap === "object" ? x.configDetailMap : {};
    x.bindings = Array.isArray(x.bindings) ? x.bindings : [];
    x.bindingMap = x.bindingMap && typeof x.bindingMap === "object" ? x.bindingMap : {};
    x.routes = normalizeAllModelRoles(x.routes || EMPTY_MODEL_ROLES());
    x.memory = x.memory && typeof x.memory === "object" ? x.memory : EMPTY_MEMORY();
    x.search = String(x.search || "");
    x.configSearch = String(x.configSearch || "");
    x.selection = x.selection && typeof x.selection === "object" ? x.selection : {};
    x.selection.selectedBindingKey = String(x.selection.selectedBindingKey || "").trim();
    x.editor = normalizeEditor(x.editor);
    x.eventsBound = !!x.eventsBound;
    return x;
  }

  function normalizeEditor(editor = {}) {
    return {
      mode: String(editor.mode || "idle"),
      configSource: String(editor.configSource || "existing"),
      bindTargetKey: String(editor.bindTargetKey || "").trim(),
      bindToExistingConfigId: String(editor.bindToExistingConfigId || "").trim(),
      currentConfigId: String(editor.currentConfigId || "").trim(),
      providerId: String(editor.providerId || "").trim(),
      capability: String(editor.capability || "chat"),
      authMode: String(editor.authMode || "api_key"),
      draft: editor.draft && typeof editor.draft === "object" ? cloneDraft(editor.draft) : null,
      validation: editor.validation && typeof editor.validation === "object" ? editor.validation : null,
      probe: editor.probe && typeof editor.probe === "object" ? editor.probe : null,
      validationAt: String(editor.validationAt || ""),
      probeAt: String(editor.probeAt || ""),
    };
  }

  function refs() {
    U.llmConfigCreate = document.getElementById("llm-config-create-btn");
    U.llmMigrate = document.getElementById("llm-migrate-btn");
    U.llmBindingsList = document.getElementById("llm-bindings-list");
    U.llmEditorShell = document.getElementById("llm-editor-shell");
    U.llmMemoryPanel = document.getElementById("llm-memory-panel");
    U.modelList = U.llmBindingsList || U.modelList;
  }

  function bindingConfigId(binding) {
    return String(binding?.config_id || binding?.llm_config_id || "").trim();
  }

  function cloneJson(value, fallback = {}) {
    try { return JSON.parse(JSON.stringify(value)); } catch { return fallback; }
  }

  function cloneDraft(draft = {}) {
    return {
      provider_id: String(draft.provider_id || "").trim(),
      capability: String(draft.capability || "chat"),
      auth_mode: String(draft.auth_mode || "api_key"),
      display_name: draft.display_name == null ? null : String(draft.display_name || ""),
      api_key: String(draft.api_key || ""),
      base_url: String(draft.base_url || ""),
      default_model: String(draft.default_model || ""),
      parameters: { ...(draft.parameters || {}) },
      extra_headers: { ...(draft.extra_headers || {}) },
      extra_options: cloneJson(draft.extra_options || {}, {}),
    };
  }

  function escv(v) { return esc(String(v == null ? "" : v)); }
  function dt(v) { const d = new Date(String(v || "")); return Number.isNaN(d.getTime()) ? String(v || "-") : d.toLocaleString(); }
  function cap(v) { return ({ chat: "Chat", embedding: "Embedding", rerank: "Rerank" })[String(v || "")] || String(v || "-"); }
  function auth(v) { return ({ api_key: "API Key", token: "Token", oauth_cache: "OAuth Cache", none: "None" })[String(v || "")] || String(v || "-"); }
  function trim(v) { return String(v || "").trim(); }

  function mapify() {
    const x = state();
    x.templateMap = Object.fromEntries(x.templates.map((item) => [trim(item.provider_id), item]));
    x.configMap = Object.fromEntries(x.configs.map((item) => [trim(item.config_id), item]));
    x.bindingMap = Object.fromEntries(x.bindings.map((item) => [trim(item.key), item]));
  }

  function currentBinding() {
    const x = state();
    return x.bindingMap[x.selection.selectedBindingKey] || null;
  }

  function templateSummary(providerId) {
    return state().templateMap[trim(providerId)] || null;
  }

  function currentTemplate() {
    return state().templateDetailMap[trim(state().editor.providerId)] || null;
  }

  function usage(key) {
    const x = state();
    const modelKey = trim(key);
    return {
      scopes: MODEL_SCOPES.filter((scope) => (x.routes?.[scope.key] || []).some((item) => trim(item) === modelKey)).map((scope) => scope.key),
      memory: [x.memory.embedding_model_key === modelKey ? "embedding" : "", x.memory.rerank_model_key === modelKey ? "rerank" : ""].filter(Boolean),
    };
  }

  function reuseCount(configId) {
    const id = trim(configId);
    if (!id) return 0;
    return state().bindings.filter((item) => bindingConfigId(item) === id).length;
  }

  function project({ preserveDrafts = false } = {}) {
    const x = state();
    const chatBindings = x.bindings.filter((item) => String(item.capability || "chat") === "chat");
    S.modelCatalog.catalog = chatBindings.map((item) => ({ ...item, key: trim(item.key) }));
    S.modelCatalog.items = chatBindings.map((item) => trim(item.key));
    S.modelCatalog.roles = normalizeAllModelRoles(x.routes || EMPTY_MODEL_ROLES());
    if (preserveDrafts && S.modelCatalog.roleEditing) {
      S.modelCatalog.roleDrafts = normalizeAllModelRoles(S.modelCatalog.roleDrafts || EMPTY_MODEL_ROLES());
      syncModelRoleDraftState();
    } else {
      S.modelCatalog.roleDrafts = cloneModelRoles(S.modelCatalog.roles);
      S.modelCatalog.roleEditing = false;
      S.modelCatalog.rolesDirty = false;
    }
    S.modelCatalog.defaults = { ...DEFAULT_MODEL_DEFAULTS(), ceo: S.modelCatalog.roles.ceo[0] || "", execution: S.modelCatalog.roles.execution[0] || "", inspection: S.modelCatalog.roles.inspection[0] || "" };
    S.modelCatalog.loading = x.loading;
    S.modelCatalog.saving = x.saving;
    S.modelCatalog.error = x.error;
    S.modelCatalog.search = x.search;
    if (x.selection.selectedBindingKey && !x.bindingMap[x.selection.selectedBindingKey]) {
      x.selection.selectedBindingKey = "";
      S.modelCatalog.selectedModelKey = "";
    }
  }

  function setHint() {
    const x = state();
    if (x.loading) return hint("正在加载新的 LLM 配置中心...");
    if (x.saving) return hint("正在保存 LLM 配置...");
    if (x.error) return hint(`LLM 配置中心出错：${x.error}`, true);
    if (!x.bindings.length) return hint("当前还没有模型绑定。你可以先创建配置记录，再创建 binding。", false);
    if (S.modelCatalog.roleEditing && S.modelCatalog.rolesDirty) return hint("模型链草稿已变更，请点击“保存模型链”提交。", false);
    if (S.modelCatalog.roleEditing) return hint("你正在编辑 Role Routes，可继续拖拽排序或移除模型。", false);
    return hint("左侧管理模型绑定，中间编辑配置记录，右侧维护 Role Routes 和 Memory Models。", false);
  }

  async function ensureTemplate(providerId) {
    const id = trim(providerId);
    if (!id) return null;
    const x = state();
    if (x.templateDetailMap[id]) return x.templateDetailMap[id];
    const detail = await ApiClient.getLlmTemplate(id);
    if (detail) x.templateDetailMap[id] = detail;
    return detail;
  }

  async function getConfigDetail(configId, includeSecrets = true) {
    const id = trim(configId);
    if (!id) return null;
    const x = state();
    if (includeSecrets && x.configDetailMap[id]?.auth) return x.configDetailMap[id];
    const detail = await ApiClient.getLlmConfig(id, { includeSecrets });
    if (detail) x.configDetailMap[id] = detail;
    return detail;
  }

  function firstTemplate(capability = "chat") {
    return [...state().templates]
      .filter((item) => String(item.capability || "chat") === String(capability || "chat"))
      .sort((a, b) => String(a.display_name || a.provider_id || "").localeCompare(String(b.display_name || b.provider_id || "")))[0] || null;
  }

  function setPath(target, path, value) {
    const parts = String(path || "").split(".").filter(Boolean);
    if (!parts.length) return;
    let cursor = target;
    while (parts.length > 1) {
      const part = parts.shift();
      if (!cursor[part] || typeof cursor[part] !== "object" || Array.isArray(cursor[part])) cursor[part] = {};
      cursor = cursor[part];
    }
    cursor[parts[0]] = value;
  }

  function getPath(target, path) {
    return String(path || "").split(".").filter(Boolean).reduce((acc, key) => (acc && typeof acc === "object" ? acc[key] : undefined), target);
  }

  function makeDraft(providerId, capability = "chat") {
    const detail = currentTemplate();
    const summary = templateSummary(providerId) || {};
    const draft = {
      provider_id: trim(providerId),
      capability: String(capability || summary.capability || "chat"),
      auth_mode: String(summary.auth_mode || "api_key"),
      display_name: summary.display_name || trim(providerId),
      api_key: String(summary.auth_mode || "api_key") === "oauth_cache" ? "oauth-cache" : "",
      base_url: String(detail?.provider?.default_base_url || ""),
      default_model: String(detail?.provider?.default_model || summary.default_model || ""),
      parameters: {}, extra_headers: {}, extra_options: {},
    };
    (detail?.fields || []).forEach((field) => {
      if (field.default === undefined || field.default === null || field.default === "") return;
      setPath(draft, field.path || field.key, cloneJson(field.default, field.default));
    });
    return draft;
  }

  function configToDraft(record) {
    return cloneDraft({
      provider_id: record?.provider_id,
      capability: record?.capability,
      auth_mode: record?.auth_mode,
      display_name: record?.display_name,
      api_key: record?.auth?.api_key || record?.api_key || "",
      base_url: record?.base_url || record?.api_base || "",
      default_model: record?.default_model || "",
      parameters: record?.parameters || {},
      extra_headers: record?.headers || record?.extra_headers || {},
      extra_options: record?.extra_options || {},
    });
  }

  function resetChecks() {
    const x = state();
    x.editor.validation = null;
    x.editor.probe = null;
    x.editor.validationAt = "";
    x.editor.probeAt = "";
  }
  function draftCleanup(draft) {
    const cleanMap = (obj) => Object.fromEntries(Object.entries(obj || {}).filter(([key, value]) => {
      if (!trim(key)) return false;
      if (value == null) return false;
      if (typeof value === "string") return trim(value) !== "";
      if (typeof value === "number") return Number.isFinite(value);
      if (typeof value === "boolean") return true;
      if (Array.isArray(value)) return value.length > 0;
      if (typeof value === "object") return Object.keys(value).length > 0;
      return true;
    }));
    const next = cloneDraft(draft);
    next.parameters = cleanMap(next.parameters);
    next.extra_headers = Object.fromEntries(Object.entries(next.extra_headers || {}).map(([k, v]) => [trim(k), trim(v)]).filter(([k, v]) => k && v));
    next.extra_options = cleanMap(next.extra_options);
    if (String(next.auth_mode || "") === "oauth_cache" && !trim(next.api_key)) next.api_key = "oauth-cache";
    return next;
  }

  function parseJson(text) {
    const raw = trim(text);
    if (!raw) return {};
    const parsed = JSON.parse(raw);
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) throw new Error("JSON 字段必须是对象");
    return parsed;
  }

  function parseKv(text) {
    const raw = trim(text);
    if (!raw) return {};
    if (raw.startsWith("{")) return parseJson(raw);
    return Object.fromEntries(raw.split(/\r?\n/).map((line) => trim(line)).filter(Boolean).map((line) => {
      const parts = line.split(/[:=]/, 2);
      if (parts.length < 2) throw new Error(`无法解析键值行: ${line}`);
      return [trim(parts[0]), trim(parts[1])];
    }).filter(([k]) => k));
  }

  function fmtJson(value) { return value && typeof value === "object" && Object.keys(value).length ? JSON.stringify(value, null, 2) : ""; }
  function fmtKv(value) { return value && typeof value === "object" ? Object.entries(value).map(([k, v]) => `${k}: ${v}`).join("\n") : ""; }

  function renderProviderSelect(capability, selected) {
    const options = [...state().templates]
      .filter((item) => String(item.capability || "chat") === String(capability || "chat"))
      .sort((a, b) => String(a.display_name || a.provider_id || "").localeCompare(String(b.display_name || b.provider_id || "")))
      .map((item) => `<option value="${escv(item.provider_id)}"${trim(item.provider_id) === trim(selected) ? " selected" : ""}>${escv(item.display_name || item.provider_id)} (${escv(item.provider_id)})</option>`)
      .join("");
    return `<select class="resource-search" name="llm_provider">${options || '<option value="">No providers</option>'}</select>`;
  }

  function renderCapabilitySelect(selected, disabled = false) {
    const opts = ["chat", "embedding", "rerank"].map((value) => `<option value="${value}"${value === selected ? " selected" : ""}>${cap(value)}</option>`).join("");
    return `<select class="resource-search" name="llm_capability"${disabled ? " disabled" : ""}>${opts}</select>`;
  }

  function renderField(field, draft, summary) {
    if (field.key === "api_key" && (String(summary?.auth_mode || "") === "oauth_cache" || summary?.supports_api_key === false)) {
      return `<div class="llm-config-reuse-banner">当前模板使用 <strong>${escv(auth(summary?.auth_mode))}</strong> 认证，后端会从本地缓存中解析凭据，无需填写真实 API Key。</div>`;
    }
    const value = getPath(draft, field.path || field.key);
    const path = escv(field.path || field.key);
    const type = String(field.input_type || "text");
    const attrs = `data-llm-field="1" data-llm-path="${path}" data-llm-type="${escv(type)}"`;
    const placeholder = escv(field.placeholder || "");
    const hintText = field.help ? `<div class="llm-muted">${escv(field.help)}</div>` : "";
    let control = "";
    if (field.key === "default_model" && Array.isArray(field.options) && field.options.length) {
      const listId = `llm-model-${String(path).replace(/[^a-z0-9]+/gi, "-")}`;
      control = `<input class="resource-search" list="${listId}" ${attrs} value="${escv(value ?? "")}" placeholder="${placeholder}"><datalist id="${listId}">${field.options.map((opt) => `<option value="${escv(opt.value)}">${escv(opt.label || opt.value)}</option>`).join("")}</datalist>`;
    } else if (type === "boolean") {
      control = `<input type="checkbox" ${attrs}${value ? " checked" : ""}>`;
    } else if (type === "number") {
      control = `<input class="resource-search" type="number" ${attrs} value="${escv(value ?? "")}" placeholder="${placeholder}">`;
    } else if (type === "url") {
      control = `<input class="resource-search" type="url" ${attrs} value="${escv(value ?? "")}" placeholder="${placeholder}">`;
    } else if (type === "secret") {
      control = `<input class="resource-search" type="password" ${attrs} value="${escv(value ?? "")}" placeholder="${placeholder}">`;
    } else if (type === "json") {
      control = `<textarea class="llm-json-editor" rows="8" ${attrs} placeholder="${placeholder}">${escv(fmtJson(value))}</textarea>`;
    } else if (type === "kv-list") {
      control = `<textarea class="llm-kv-editor" rows="6" ${attrs} placeholder="key: value">${escv(fmtKv(value))}</textarea>`;
    } else if (type === "select") {
      control = `<select class="resource-search" ${attrs}><option value="">-- Select --</option>${(field.options || []).map((opt) => `<option value="${escv(opt.value)}"${String(opt.value) === String(value ?? "") ? " selected" : ""}>${escv(opt.label || opt.value)}</option>`).join("")}</select>`;
    } else {
      control = `<input class="resource-search" type="text" ${attrs} value="${escv(value ?? "")}" placeholder="${placeholder}">`;
    }
    return `<label class="resource-field"><span class="resource-field-label">${escv(field.label || field.key)}${field.required ? " *" : ""}</span>${control}${hintText}</label>`;
  }

  function renderStatusBox(kind, data, at) {
    if (!data) return `<div class="llm-${kind}-status"><strong>${kind === "validation" ? "验证状态" : "探测状态"}</strong><div class="llm-muted">尚未${kind === "validation" ? "验证" : "探测"}。</div></div>`;
    const ok = kind === "validation" ? !!data.valid : !!data.success;
    const klass = ok ? "is-success" : "is-error";
    const title = kind === "validation" ? "验证状态" : "探测状态";
    const message = kind === "validation"
      ? (data.valid ? "字段校验通过，可以继续测试连接。" : "字段校验未通过，请修正后再测试连接。")
      : (data.message || (data.success ? "连接成功" : "探测失败"));
    const errors = kind === "validation" && Array.isArray(data.errors) && data.errors.length
      ? `<ul>${data.errors.map((item) => `<li>${escv(item.field || "field")} - ${escv(item.message || item.code || "Invalid")}</li>`).join("")}</ul>`
      : (kind === "probe" ? `<div class="llm-muted">状态：${escv(data.status || "-")} · 模型：${escv(data.checked_model || "-")} · 延迟：${escv(data.latency_ms ?? "-")} ms</div>` : "");
    return `<div class="llm-${kind}-status ${klass}"><strong>${title}</strong><div>${escv(message)}</div>${at ? `<div class="llm-muted">最近更新：${escv(dt(at))}</div>` : ""}${errors}</div>`;
  }

  function renderConfigForm(mode) {
    const x = state();
    const draft = x.editor.draft;
    const template = currentTemplate();
    const summary = templateSummary(x.editor.providerId);
    if (!draft || !summary) return '<div class="empty-state compact">请选择 Provider 模板以载入配置表单。</div>';
    const basic = template?.field_groups?.basic || [];
    const advanced = template?.field_groups?.advanced || [];
    const saveAction = mode === "create-binding" ? "llm-save-binding-draft" : mode === "edit-current" ? "llm-save-config" : mode === "switch-new" ? "llm-save-config-switch" : "llm-save-config";
    const saveLabel = mode === "create-binding" ? "保存并绑定" : mode === "edit-current" ? "保存当前配置" : mode === "switch-new" ? "保存配置并切换绑定" : "保存配置";
    return `
      <section class="llm-section">
        <div class="llm-status-row">
          <div><strong>Provider 配置</strong><div class="llm-muted">模板驱动表单，先验证与探测，再保存。</div></div>
          <div class="llm-inline-actions"><span class="llm-capability-badge ${escv(draft.capability)}">${escv(cap(draft.capability))}</span><span class="llm-auth-mode-badge ${escv(draft.auth_mode)}">${escv(auth(draft.auth_mode))}</span></div>
        </div>
        <div class="llm-form-grid">
          <label class="resource-field"><span class="resource-field-label">Capability</span>${renderCapabilitySelect(draft.capability, mode !== "standalone" && mode !== "create-binding")}</label>
          <label class="resource-field"><span class="resource-field-label">Provider</span>${renderProviderSelect(draft.capability, draft.provider_id)}</label>
        </div>
        ${String(summary.auth_mode || "") === "oauth_cache" ? '<div class="llm-config-reuse-banner">当前 Provider 使用 OAuth cache 认证，前端不会要求输入真实 API Key。</div>' : ""}
        <section class="llm-template-section"><div class="model-role-section-title">Basic Fields</div><div class="llm-form-grid">${basic.map((field) => renderField(field, draft, summary)).join("")}</div></section>
        ${advanced.length ? `<section class="llm-template-section"><div class="model-role-section-title">Advanced Fields</div><div class="llm-form-grid">${advanced.map((field) => renderField(field, draft, summary)).join("")}</div></section>` : ""}
        <div class="llm-inline-actions"><button class="toolbar-btn ghost" type="button" data-llm-action="llm-validate">验证</button><button class="toolbar-btn ghost" type="button" data-llm-action="llm-probe">测试连接</button><button class="toolbar-btn success" type="button" data-llm-action="${saveAction}"${x.saving || !x.editor.probe?.success ? " disabled" : ""}>${saveLabel}</button></div>
        ${renderStatusBox("validation", x.editor.validation, x.editor.validationAt)}
        ${renderStatusBox("probe", x.editor.probe, x.editor.probeAt)}
      </section>`;
  }

  function renderConfigPicker(capability, selectedConfigId) {
    const x = state();
    const q = trim(x.configSearch).toLowerCase();
    const configs = [...x.configs].filter((item) => String(item.capability || "chat") === String(capability || "chat")).filter((item) => !q || [item.display_name, item.provider_id, item.default_model, item.config_id].join("\n").toLowerCase().includes(q)).sort((a, b) => String(a.display_name || a.provider_id || "").localeCompare(String(b.display_name || b.provider_id || "")));
    return `<div class="llm-inline-actions"><input class="resource-search" type="search" data-llm-config-search="1" value="${escv(x.configSearch)}" placeholder="搜索配置记录"></div><div class="llm-record-list">${configs.length ? configs.map((item) => `<article class="llm-record-item${trim(item.config_id) === trim(selectedConfigId) ? " is-selected" : ""}" data-llm-config-pick="${escv(item.config_id)}"><div class="llm-binding-card-head"><strong>${escv(item.display_name || item.provider_id)}</strong><span class="llm-config-record-chip">复用 ${reuseCount(item.config_id)}</span></div><div class="llm-config-meta">${escv(item.provider_id)} / ${escv(item.default_model)}</div><div class="llm-muted">最后探测：${escv(item.last_probe_status || "unknown")} · 更新时间：${escv(dt(item.updated_at))}</div></article>`).join("") : '<div class="empty-state compact">当前 capability 下还没有可复用的配置记录。</div>'}</div>`;
  }

  function renderBindingEditor() {
    const x = state();
    const creating = x.editor.mode === "create-binding";
    const binding = creating ? { key: "", description: "", enabled: true, retry_on: [...RETRY_ON], capability: x.editor.capability || "chat" } : currentBinding();
    if (!binding) return '<div class="empty-state resource-empty">请选择一个模型绑定，或点击“新建模型绑定”。</div>';
    const capability = String(binding.capability || x.editor.capability || "chat");
    const currentConfigId = bindingConfigId(binding) || x.editor.currentConfigId || "";
    const selectedConfigId = x.editor.bindToExistingConfigId || currentConfigId;
    const bindUsage = usage(binding.key);
    const safeDelete = creating || reuseCount(currentConfigId) <= 1;
    return `
      <div class="llm-editor-shell">
        <section class="llm-section">
          <div class="llm-binding-card-head"><div><h3>${creating ? "新建模型绑定" : escv(binding.key)}</h3><div class="llm-binding-meta">通过 binding 关联配置记录，再被 routes / memory 引用。</div></div><div class="llm-inline-actions"><span class="llm-capability-badge ${escv(capability)}">${escv(cap(capability))}</span><button class="toolbar-btn ghost" type="button" data-llm-action="llm-cancel">${creating ? "取消" : "关闭"}</button></div></div>
          <form id="llm-binding-form" class="llm-form-grid">
            <label class="resource-field"><span class="resource-field-label">Model Key *</span><input class="resource-search" name="binding_key" value="${escv(binding.key || "")}" placeholder="例如: openai_primary"${creating ? "" : " disabled"}></label>
            <label class="resource-field"><span class="resource-field-label">Capability</span>${renderCapabilitySelect(capability, !creating)}</label>
            <label class="resource-field"><span class="resource-field-label">Description</span><input class="resource-search" name="binding_description" value="${escv(binding.description || "")}" placeholder="用途、成本、约束说明"></label>
            <label class="resource-field"><span class="resource-field-label">Retry On</span><input class="resource-search" name="binding_retry_on" value="${escv((binding.retry_on || RETRY_ON).join(", "))}" placeholder="network, 429, 5xx"></label>
            <label class="resource-field"><span class="resource-field-label">Enabled</span><input type="checkbox" name="binding_enabled"${binding.enabled !== false ? " checked" : ""}></label>
          </form>
          <div class="llm-muted">Routes: ${bindUsage.scopes.length ? bindUsage.scopes.map((key) => LABELS[key] || key).join(", ") : "未加入角色链"} · Memory: ${bindUsage.memory.length ? bindUsage.memory.join(", ") : "未被 Memory 引用"}</div>
        </section>
        <section class="llm-section">
          <div class="llm-status-row"><div><strong>配置记录</strong><div class="llm-muted">支持已有配置复用，或基于模板新建配置。</div></div><div class="llm-inline-actions">${creating ? '<button class="toolbar-btn ghost" type="button" data-llm-action="llm-mode-existing">绑定已有配置</button><button class="toolbar-btn ghost" type="button" data-llm-action="llm-mode-new">创建新配置</button>' : '<button class="toolbar-btn ghost" type="button" data-llm-action="llm-mode-existing">切换到已有配置</button><button class="toolbar-btn ghost" type="button" data-llm-action="llm-mode-edit">编辑当前配置</button><button class="toolbar-btn ghost" type="button" data-llm-action="llm-mode-new">创建新配置</button>'}</div></div>
          ${currentConfigId ? `<div class="llm-config-reuse-banner"><div class="llm-config-record-row"><strong>${escv(state().configMap[currentConfigId]?.display_name || state().configMap[currentConfigId]?.provider_id || currentConfigId)}</strong><span class="llm-config-record-chip">复用 ${reuseCount(currentConfigId)}</span></div><div class="llm-muted">${escv(state().configMap[currentConfigId]?.provider_id || "-")} / ${escv(state().configMap[currentConfigId]?.default_model || "-")}</div></div>` : '<div class="empty-state compact">当前 binding 尚未关联配置记录。</div>'}
          ${x.editor.configSource === "existing" ? renderConfigPicker(capability, selectedConfigId) : renderConfigForm(creating ? "create-binding" : x.editor.configSource === "edit" ? "edit-current" : "switch-new")}
          <div class="llm-inline-actions">${x.editor.configSource === "existing" || x.editor.configSource === "edit" ? `<button class="toolbar-btn success" type="button" data-llm-action="llm-save-binding"${x.saving || !selectedConfigId && x.editor.configSource === "existing" ? " disabled" : ""}>${creating ? "保存绑定" : "保存绑定元数据 / 切换配置"}</button>` : ""}${!creating ? `<button class="toolbar-btn ${safeDelete ? "danger" : "ghost"}" type="button" data-llm-action="llm-delete-binding"${safeDelete ? "" : " disabled"}>删除绑定</button>` : ""}</div>
          ${!creating && !safeDelete ? `<div class="llm-config-reuse-banner">当前配置被多个 binding 复用。由于当前后端删除 binding 会连带删除配置，请先切换到独立配置后再删除。</div>` : ""}
        </section>
      </div>`;
  }

  function renderEditor() {
    refs();
    if (!U.llmEditorShell) return;
    if (state().editor.mode === "create-config") {
      U.llmEditorShell.innerHTML = `<div class="llm-editor-shell"><section class="llm-section"><div class="llm-binding-card-head"><div><h3>新建配置记录</h3><div class="llm-binding-meta">先创建一个可复用的配置记录，保存成功后可继续创建 binding。</div></div><div class="llm-inline-actions"><button class="toolbar-btn ghost" type="button" data-llm-action="llm-cancel">关闭</button></div></div>${renderConfigForm("standalone")}</section></div>`;
    } else if (state().editor.mode === "create-binding" || state().selection.selectedBindingKey) {
      U.llmEditorShell.innerHTML = renderBindingEditor();
    } else {
      U.llmEditorShell.innerHTML = '<div class="empty-state resource-empty">请选择一个模型绑定，或点击“新建配置 / 新建模型绑定”。</div>';
    }
  }

  function renderList() {
    refs();
    if (!U.llmBindingsList) return;
    const x = state();
    x.search = trim(S.modelCatalog.search || x.search || "");
    const q = x.search.toLowerCase();
    const bindings = [...x.bindings].filter((item) => !q || [item.key, item.provider_model, item.description, item.capability, item.auth_mode].join("\n").toLowerCase().includes(q)).sort((a, b) => String(a.key || "").localeCompare(String(b.key || "")));
    if (!bindings.length) {
      U.llmBindingsList.innerHTML = `<div class="empty-state compact">${x.search ? "没有匹配的 binding。" : "还没有创建任何 binding。"}</div>`;
      return;
    }
    U.llmBindingsList.innerHTML = bindings.map((item) => {
      const info = usage(item.key);
      const canDrag = S.modelCatalog.roleEditing && String(item.capability || "") === "chat";
      return `<article class="llm-binding-card model-available-item${trim(item.key) === trim(x.selection.selectedBindingKey) ? " is-selected" : ""}" data-model-available-key="${escv(item.key)}"${canDrag ? ' draggable="true"' : ""}><div class="llm-binding-card-head"><button type="button" class="model-available-main" data-model-open="${escv(item.key)}"><span class="resource-list-title">${escv(item.key)}</span><span class="resource-list-subtitle">${escv(item.provider_model)}</span></button><div class="llm-inline-actions"><span class="llm-capability-badge ${escv(item.capability || "chat")}">${escv(cap(item.capability))}</span><span class="llm-auth-mode-badge ${escv(item.auth_mode || "api_key")}">${escv(auth(item.auth_mode))}</span></div></div><div class="llm-binding-meta">${escv(item.description || "No description")}</div><div class="model-inline-meta">${info.scopes.length ? info.scopes.map((scope) => `<span class="policy-chip neutral">${escv(LABELS[scope] || scope)}</span>`).join("") : '<span class="policy-chip neutral">未进入 Role Route</span>'}${info.memory.length ? info.memory.map((name) => `<span class="policy-chip risk-low">${escv(name)}</span>`).join("") : ""}${item.enabled === false ? '<span class="policy-chip neutral">Disabled</span>' : '<span class="policy-chip risk-low">Enabled</span>'}</div><div class="llm-inline-actions"><button class="toolbar-btn ghost small" type="button" data-model-open="${escv(item.key)}">详情</button><button class="toolbar-btn ghost small" type="button" data-llm-action="llm-toggle-binding" data-key="${escv(item.key)}" data-enabled="${item.enabled === false ? "true" : "false"}">${item.enabled === false ? "启用" : "禁用"}</button></div></article>`;
    }).join("");
  }

  function renderRoutes() {
    if (!U.modelRoleEditors) return;
    const editing = !!S.modelCatalog.roleEditing;
    U.modelRoleEditors.innerHTML = MODEL_SCOPES.map((scope) => {
      const chain = modelScopeChain(scope.key);
      return `<section class="model-chain-card"><div class="panel-header"><div><h3>${escv(LABELS[scope.key] || scope.key)}</h3><p class="subtitle">${escv(chain[0] ? `默认: ${chain[0]}` : "尚未配置")}</p></div><span class="policy-chip neutral">${chain.length} 个 binding</span></div><div class="model-role-section"><div class="model-role-section-title">Role Chain</div><div class="model-chain-list" data-model-chain-list="${scope.key}">${chain.length ? chain.map((ref, index) => { const item = state().bindingMap[trim(ref)] || modelRefItem(ref); const key = trim(item?.key || ref); return `<article class="model-chain-slide${editing ? ' is-editing' : ''}"${editing ? ' draggable="true"' : ''} data-model-chain-ref="${escv(key)}" data-scope="${scope.key}">${editing ? '<button type="button" class="model-chain-handle" aria-label="拖拽排序"><span class="model-chain-grip" aria-hidden="true">&#9776;</span></button>' : ''}<button type="button" class="model-chain-main" data-model-open="${escv(key)}"><span class="resource-list-title">${escv(key)}</span><span class="resource-list-subtitle">${escv(item?.provider_model || ref)}</span><span class="model-inline-meta">${index === 0 ? '<span class="policy-chip risk-low">首选</span>' : ''}${item?.enabled === false ? '<span class="policy-chip neutral">Disabled</span>' : ''}</span></button>${editing ? `<button type="button" class="toolbar-btn ghost small" data-model-chain-action="remove" data-scope="${scope.key}" data-index="${index}">移除</button>` : ''}</article>`; }).join("") : `<div class="empty-state compact">${editing ? '将左侧 Chat binding 拖到这里，构建当前 role chain。' : '点击“编辑模型链”后再调整当前角色链。'}</div>`}</div></div></section>`;
    }).join("");
  }

  function renderMemory() {
    refs();
    if (!U.llmMemoryPanel) return;
    const x = state();
    const embedding = x.bindings.filter((item) => String(item.capability || "") === "embedding");
    const rerank = x.bindings.filter((item) => String(item.capability || "") === "rerank");
    U.llmMemoryPanel.innerHTML = `<div class="llm-section"><label class="resource-field"><span class="resource-field-label">Embedding Model Key</span><select class="resource-search" name="embedding_model_key"><option value="">-- Unset --</option>${embedding.map((item) => `<option value="${escv(item.key)}"${trim(item.key) === trim(x.memory.embedding_model_key) ? " selected" : ""}>${escv(item.key)} · ${escv(item.provider_model)}</option>`).join("")}</select></label><label class="resource-field"><span class="resource-field-label">Rerank Model Key</span><select class="resource-search" name="rerank_model_key"><option value="">-- Unset --</option>${rerank.map((item) => `<option value="${escv(item.key)}"${trim(item.key) === trim(x.memory.rerank_model_key) ? " selected" : ""}>${escv(item.key)} · ${escv(item.provider_model)}</option>`).join("")}</select></label><div class="llm-inline-actions"><button class="toolbar-btn success" type="button" data-llm-action="llm-save-memory">保存 Memory 绑定</button></div></div>`;
  }
  function collectBinding() {
    const form = U.llmEditorShell?.querySelector("#llm-binding-form");
    const binding = state().editor.mode === "create-binding" ? null : currentBinding();
    const key = state().editor.mode === "create-binding" ? trim(form?.querySelector('[name="binding_key"]')?.value) : trim(binding?.key || state().selection.selectedBindingKey);
    if (!key) throw new Error("Model key 不能为空");
    return {
      key,
      config_id: trim(state().editor.bindToExistingConfigId || state().editor.currentConfigId),
      enabled: !!form?.querySelector('[name="binding_enabled"]')?.checked,
      description: trim(form?.querySelector('[name="binding_description"]')?.value),
      retry_on: trim(form?.querySelector('[name="binding_retry_on"]')?.value).split(/[\n,]/).map((item) => trim(item)).filter(Boolean).length ? trim(form?.querySelector('[name="binding_retry_on"]')?.value).split(/[\n,]/).map((item) => trim(item)).filter(Boolean) : [...RETRY_ON],
      capability: trim(form?.querySelector('[name="llm_capability"]')?.value || binding?.capability || state().editor.capability || "chat") || "chat",
    };
  }

  function collectDraft() {
    const x = state();
    const draft = cloneDraft(x.editor.draft || {});
    const template = currentTemplate();
    const summary = templateSummary(x.editor.providerId);
    if (!draft.provider_id) throw new Error("请先选择 Provider");
    if (!template || !summary) throw new Error("Provider 模板尚未加载完成");
    draft.capability = trim(U.llmEditorShell?.querySelector('[name="llm_capability"]')?.value || draft.capability || x.editor.capability || "chat") || "chat";
    draft.provider_id = trim(U.llmEditorShell?.querySelector('[name="llm_provider"]')?.value || draft.provider_id);
    draft.auth_mode = String(summary.auth_mode || draft.auth_mode || "api_key");
    draft.display_name = summary.display_name || draft.display_name || draft.provider_id;
    [...(U.llmEditorShell?.querySelectorAll('[data-llm-field="1"]') || [])].forEach((input) => {
      const path = trim(input.dataset.llmPath);
      const type = String(input.dataset.llmType || "text");
      if (!path) return;
      let value = null;
      if (type === "boolean") value = !!input.checked;
      else if (type === "number") { const raw = trim(input.value); value = raw ? Number(raw) : null; if (raw && !Number.isFinite(value)) throw new Error(`字段 ${path} 必须是数字`); }
      else if (type === "json") value = parseJson(input.value);
      else if (type === "kv-list") value = parseKv(input.value);
      else value = String(input.value || "");
      setPath(draft, path, value);
    });
    return draftCleanup(draft);
  }

  async function refresh(providerOnly = false) {
    const x = state();
    refs();
    x.loading = true;
    x.error = "";
    render();
    try {
      const tasks = providerOnly
        ? [ApiClient.getLlmTemplates()]
        : [ApiClient.getLlmTemplates(), ApiClient.listLlmConfigs(), ApiClient.listLlmBindings(), ApiClient.getLlmRoutes(), ApiClient.getLlmMemoryModels()];
      const results = await Promise.all(tasks);
      x.templates = Array.isArray(results[0]) ? results[0] : [];
      if (!providerOnly) {
        x.configs = Array.isArray(results[1]) ? results[1] : [];
        x.bindings = Array.isArray(results[2]?.items) ? results[2].items : [];
        x.routes = normalizeAllModelRoles((results[3] && Object.keys(results[3]).length ? results[3] : results[2]?.routes) || EMPTY_MODEL_ROLES());
        x.memory = results[4] && typeof results[4] === "object" ? results[4] : EMPTY_MEMORY();
      }
      mapify();
      project({ preserveDrafts: !!S.modelCatalog.roleEditing });
    } catch (error) {
      x.error = error.message || "加载失败";
    } finally {
      x.loading = false;
      render();
    }
  }

  async function setProvider(providerId, capability) {
    const x = state();
    x.editor.providerId = trim(providerId);
    x.editor.capability = String(capability || x.editor.capability || "chat");
    await ensureTemplate(x.editor.providerId);
    x.editor.draft = makeDraft(x.editor.providerId, x.editor.capability);
    x.editor.authMode = x.editor.draft.auth_mode;
    resetChecks();
    render();
  }

  async function openBinding(key) {
    const x = state();
    const bindingKey = trim(key);
    if (!x.bindingMap[bindingKey]) return;
    const binding = x.bindingMap[bindingKey];
    x.selection.selectedBindingKey = bindingKey;
    x.editor = normalizeEditor({ mode: "edit-binding", configSource: "existing", bindTargetKey: bindingKey, bindToExistingConfigId: bindingConfigId(binding), currentConfigId: bindingConfigId(binding), capability: binding.capability || "chat" });
    S.modelCatalog.selectedModelKey = bindingKey;
    render();
  }

  async function createBindingFlow(configId = "", capability = "chat") {
    const x = state();
    const preferred = firstTemplate(capability);
    x.selection.selectedBindingKey = "";
    x.editor = normalizeEditor({ mode: "create-binding", configSource: configId ? "existing" : (x.configs.some((item) => String(item.capability || "chat") === String(capability)) ? "existing" : "new"), bindToExistingConfigId: trim(configId), capability, providerId: preferred?.provider_id || "" });
    S.modelCatalog.selectedModelKey = "";
    if (x.editor.configSource !== "existing" && preferred?.provider_id) await setProvider(preferred.provider_id, capability);
    render();
  }

  async function createConfigFlow(capability = "chat") {
    const preferred = firstTemplate(capability);
    state().selection.selectedBindingKey = "";
    state().editor = normalizeEditor({ mode: "create-config", configSource: "new", capability, providerId: preferred?.provider_id || "" });
    if (preferred?.provider_id) await setProvider(preferred.provider_id, capability);
    render();
  }

  function clearSelection() {
    state().selection.selectedBindingKey = "";
    state().configSearch = "";
    state().editor = normalizeEditor();
    S.modelCatalog.selectedModelKey = "";
    render();
  }

  async function validateDraft() {
    const x = state();
    x.editor.draft = collectDraft();
    x.editor.probe = null;
    x.editor.probeAt = "";
    x.editor.validation = await ApiClient.validateLlmDraft(x.editor.draft);
    x.editor.validationAt = new Date().toISOString();
    render();
    return x.editor.validation;
  }

  async function probeDraft() {
    const v = await validateDraft();
    if (!v?.valid) return v;
    state().editor.draft = collectDraft();
    state().editor.probe = await ApiClient.probeLlmDraft(state().editor.draft);
    state().editor.probeAt = new Date().toISOString();
    render();
    return state().editor.probe;
  }

  async function saveBinding() {
    const x = state();
    const binding = collectBinding();
    x.saving = true; render();
    try {
      if (x.editor.mode === "create-binding") {
        await ApiClient.createLlmBinding({ binding, draft: {} });
        showToast({ title: "绑定已创建", text: `${binding.key} 已绑定到配置记录`, kind: "success" });
      } else {
        await ApiClient.updateLlmBinding(binding.key, { config_id: binding.config_id, enabled: binding.enabled, description: binding.description, retry_on: binding.retry_on });
        showToast({ title: "绑定已更新", text: `${binding.key} 已更新`, kind: "success" });
      }
      await refresh();
      await openBinding(binding.key);
    } finally { x.saving = false; render(); }
  }

  async function saveBindingWithDraft() {
    const x = state();
    const binding = collectBinding();
    const draft = collectDraft();
    if (!x.editor.probe?.success) throw new Error("请先测试连接并确保探测成功");
    x.saving = true; render();
    try {
      await ApiClient.createLlmBinding({ binding: { ...binding, config_id: "" }, draft });
      showToast({ title: "模型已创建", text: `${binding.key} 已保存并绑定配置`, kind: "success" });
      await refresh();
      await openBinding(binding.key);
    } finally { x.saving = false; render(); }
  }

  async function saveConfigOnly() {
    const x = state();
    const draft = collectDraft();
    if (!x.editor.probe?.success) throw new Error("请先测试连接并确保探测成功");
    x.saving = true; render();
    try {
      const created = await ApiClient.createLlmConfig(draft);
      showToast({ title: "配置已创建", text: `${draft.provider_id} / ${draft.default_model} 已保存`, kind: "success" });
      await refresh();
      await createBindingFlow(created?.config_id || "", draft.capability || "chat");
    } finally { x.saving = false; render(); }
  }

  async function saveCurrentConfig() {
    const x = state();
    const draft = collectDraft();
    const configId = trim(x.editor.currentConfigId || bindingConfigId(currentBinding()));
    if (!configId) throw new Error("当前没有可更新的配置记录");
    if (!x.editor.probe?.success) throw new Error("请先测试连接并确保探测成功");
    x.saving = true; render();
    try {
      await ApiClient.updateLlmConfig(configId, draft);
      showToast({ title: "配置已更新", text: `${draft.provider_id} / ${draft.default_model} 已保存`, kind: "success" });
      await refresh();
      if (currentBinding()?.key) await openBinding(currentBinding().key);
    } finally { x.saving = false; render(); }
  }

  async function saveConfigAndSwitch() {
    const x = state();
    const binding = collectBinding();
    const draft = collectDraft();
    if (!x.editor.probe?.success) throw new Error("请先测试连接并确保探测成功");
    x.saving = true; render();
    try {
      const created = await ApiClient.createLlmConfig(draft);
      await ApiClient.updateLlmBinding(binding.key, { config_id: created?.config_id, enabled: binding.enabled, description: binding.description, retry_on: binding.retry_on });
      showToast({ title: "配置已创建", text: `新配置已创建并切换到 ${binding.key}`, kind: "success" });
      await refresh();
      await openBinding(binding.key);
    } finally { x.saving = false; render(); }
  }

  async function toggleBinding(key, enabled) {
    state().saving = true; render();
    try { if (enabled) await ApiClient.enableLlmBinding(key); else await ApiClient.disableLlmBinding(key); await refresh(); if (state().selection.selectedBindingKey === key) await openBinding(key); }
    finally { state().saving = false; render(); }
  }

  async function deleteBinding(key) {
    const binding = state().bindingMap[trim(key)];
    if (!binding) return;
    if (reuseCount(bindingConfigId(binding)) > 1) throw new Error("该 binding 绑定的配置记录正在被多个 binding 复用，当前不能直接删除。");
    const info = usage(binding.key);
    const impacts = [info.scopes.length ? `Role Routes: ${info.scopes.join(", ")}` : "", info.memory.length ? `Memory: ${info.memory.join(", ")}` : ""].filter(Boolean).join("；");
    if (!window.confirm(`确认删除 binding ${binding.key}？${impacts ? `\n影响范围：${impacts}` : ""}`)) return;
    state().saving = true; render();
    try { await ApiClient.deleteLlmBinding(binding.key); showToast({ title: "绑定已删除", text: `${binding.key} 已移除`, kind: "success" }); clearSelection(); await refresh(); }
    finally { state().saving = false; render(); }
  }

  async function saveMemory() {
    state().saving = true; render();
    try {
      await ApiClient.updateLlmMemoryModels({ embedding_model_key: trim(U.llmMemoryPanel?.querySelector('[name="embedding_model_key"]')?.value) || null, rerank_model_key: trim(U.llmMemoryPanel?.querySelector('[name="rerank_model_key"]')?.value) || null });
      showToast({ title: "Memory 绑定已保存", text: "Embedding / Rerank 配置已更新", kind: "success" });
      await refresh();
    } finally { state().saving = false; render(); }
  }

  async function migrate() {
    state().saving = true; render();
    try { await ApiClient.runLlmMigration(); showToast({ title: "迁移已执行", text: "已触发后端迁移并刷新页面数据", kind: "success" }); await refresh(); }
    finally { state().saving = false; render(); }
  }

  async function switchMode(action) {
    const x = state();
    const binding = currentBinding();
    const capability = String(binding?.capability || x.editor.capability || "chat");
    if (action === "llm-mode-existing") { x.editor.configSource = "existing"; x.editor.bindToExistingConfigId = trim(x.editor.bindToExistingConfigId || x.editor.currentConfigId || bindingConfigId(binding)); resetChecks(); render(); return; }
    if (action === "llm-mode-edit") { const configId = trim(x.editor.currentConfigId || bindingConfigId(binding)); if (!configId) throw new Error("当前 binding 没有关联配置记录"); const detail = await getConfigDetail(configId, true); x.editor.configSource = "edit"; x.editor.currentConfigId = configId; x.editor.providerId = detail?.provider_id || ""; x.editor.capability = detail?.capability || capability; await ensureTemplate(x.editor.providerId); x.editor.draft = configToDraft(detail); x.editor.authMode = x.editor.draft.auth_mode; resetChecks(); render(); return; }
    if (action === "llm-mode-new") { x.editor.configSource = "new"; const guess = trim(String(binding?.provider_model || "").split(":", 1)[0]) || firstTemplate(capability)?.provider_id || ""; const provider = templateSummary(guess) ? guess : firstTemplate(capability)?.provider_id || ""; if (provider) await setProvider(provider, capability); render(); }
  }

  function render() {
    refs();
    project({ preserveDrafts: !!S.modelCatalog.roleEditing });
    if (U.modelRefresh) U.modelRefresh.disabled = state().loading || state().saving;
    if (U.modelCreate) U.modelCreate.disabled = state().loading || state().saving;
    if (U.llmConfigCreate) U.llmConfigCreate.disabled = state().loading || state().saving;
    if (U.llmMigrate) U.llmMigrate.disabled = state().loading || state().saving;
    if (U.modelRolesCancel) { U.modelRolesCancel.hidden = !S.modelCatalog.roleEditing; U.modelRolesCancel.disabled = state().loading || state().saving; }
    if (U.modelRolesSave) { U.modelRolesSave.disabled = state().loading || state().saving; U.modelRolesSave.textContent = state().saving ? "保存中..." : (S.modelCatalog.roleEditing ? "保存模型链" : "编辑模型链"); }
    setHint();
    renderRoutes();
    renderList();
    renderEditor();
    renderMemory();
    icons();
  }

  function bindList() {
    refs();
    if (!U.llmBindingsList) return;
    U.llmBindingsList.addEventListener("click", (event) => {
      const open = event.target.closest("[data-model-open]");
      if (open) { void openBinding(open.dataset.modelOpen); return; }
      const action = event.target.closest("[data-llm-action]");
      if (action?.dataset.llmAction === "llm-toggle-binding") void toggleBinding(action.dataset.key, String(action.dataset.enabled || "") === "true");
    });
    U.llmBindingsList.addEventListener("dragstart", (event) => {
      if (!S.modelCatalog.roleEditing) return;
      const item = event.target.closest("[data-model-available-key]");
      if (!item) return;
      beginModelDrag(item, { ref: String(item.dataset.modelAvailableKey || ""), source: "available" }, event.dataTransfer);
    });
    U.llmBindingsList.addEventListener("dragover", (event) => {
      if (!S.modelCatalog.roleEditing) return;
      const dragState = S.modelCatalog.dragState;
      if (!dragState?.ref || dragState.source !== "chain") return;
      const list = event.target.closest("[data-model-available-list]");
      if (!list) return;
      event.preventDefault();
      event.dataTransfer.dropEffect = "move";
      clearModelDragDecorations();
      highlightModelAvailableZone(list, event.target.closest("[data-model-available-key]"));
      startModelAutoScroll(list, event.clientY);
    });
    U.llmBindingsList.addEventListener("drop", (event) => {
      if (!S.modelCatalog.roleEditing) return;
      const dragState = S.modelCatalog.dragState;
      if (!dragState?.ref || dragState.source !== "chain") return;
      const list = event.target.closest("[data-model-available-list]");
      if (!list) return;
      event.preventDefault();
      clearModelDragDecorations();
      stopModelAutoScroll();
      removeRoleChainItem(dragState.scope, dragState.ref);
    });
    U.llmBindingsList.addEventListener("dragleave", (event) => {
      if (!S.modelCatalog.roleEditing) return;
      const dragState = S.modelCatalog.dragState;
      if (!dragState?.ref) return;
      const zone = event.target.closest("[data-model-available-list]");
      if (!zone) return;
      if (event.relatedTarget && zone.contains(event.relatedTarget)) return;
      clearModelDragDecorations();
      stopModelAutoScroll();
    });
    U.llmBindingsList.addEventListener("dragend", finishModelDrag);
  }

  async function onEditorClick(event) {
    const target = event.target.closest("[data-llm-action], [data-llm-config-pick]");
    if (!target) return;
    try {
      if (target.dataset.llmConfigPick) { state().editor.bindToExistingConfigId = trim(target.dataset.llmConfigPick); render(); return; }
      const action = String(target.dataset.llmAction || "");
      if (action === "llm-cancel") { clearSelection(); return; }
      if (action === "llm-validate") { await validateDraft(); return; }
      if (action === "llm-probe") { await probeDraft(); return; }
      if (action === "llm-save-binding") { await saveBinding(); return; }
      if (action === "llm-save-binding-draft") { await saveBindingWithDraft(); return; }
      if (action === "llm-save-config") { if (state().editor.mode === "create-config") await saveConfigOnly(); else await saveCurrentConfig(); return; }
      if (action === "llm-save-config-switch") { await saveConfigAndSwitch(); return; }
      if (action === "llm-delete-binding") { await deleteBinding(currentBinding()?.key || state().selection.selectedBindingKey); return; }
      if (action === "llm-save-memory") { await saveMemory(); return; }
      if (action === "llm-mode-existing" || action === "llm-mode-edit" || action === "llm-mode-new") { await switchMode(action); return; }
    } catch (error) {
      state().error = error.message || "操作失败";
      render();
      showToast({ title: "操作失败", text: state().error, kind: "error" });
    }
  }

  async function onEditorChange(event) {
    const target = event.target;
    if (!(target instanceof HTMLElement)) return;
    try {
      if (target.matches("[data-llm-config-search='1']")) { state().configSearch = String(target.value || ""); render(); return; }
      if (target.matches("[name='llm_capability']")) { state().editor.capability = trim(target.value) || "chat"; if (state().editor.mode === "create-config" || state().editor.configSource === "new") { const next = firstTemplate(state().editor.capability)?.provider_id || ""; if (next) await setProvider(next, state().editor.capability); } render(); return; }
      if (target.matches("[name='llm_provider']")) { const providerId = trim(target.value); if (providerId) await setProvider(providerId, state().editor.capability || "chat"); return; }
      if (target.matches("[data-llm-field='1']")) { resetChecks(); return; }
    } catch (error) {
      state().error = error.message || "操作失败";
      render();
      showToast({ title: "操作失败", text: state().error, kind: "error" });
    }
  }

  async function bootstrap() {
    if (state().eventsBound) return;
    refs();
    bindList();
    U.llmConfigCreate?.addEventListener("click", () => void createConfigFlow("chat"));
    U.llmMigrate?.addEventListener("click", () => void migrate());
    U.llmEditorShell?.addEventListener("click", (event) => { void onEditorClick(event); });
    U.llmEditorShell?.addEventListener("change", (event) => { void onEditorChange(event); });
    U.llmEditorShell?.addEventListener("input", (event) => { void onEditorChange(event); });
    U.llmMemoryPanel?.addEventListener("click", (event) => { if (event.target.closest("[data-llm-action='llm-save-memory']")) void saveMemory(); });
    state().eventsBound = true;
  }

  window.renderModelList = renderList;
  window.renderModelRoleEditors = renderRoutes;
  window.renderModelDetail = renderEditor;
  window.renderModelCatalog = render;
  window.renderModelHint = setHint;
  window.openModel = function openModel(key) { void openBinding(key); };
  window.startCreateModel = function startCreateModel() { void createBindingFlow("", "chat"); };
  window.clearModelSelection = clearSelection;
  window.loadModels = async function loadModels() { await refresh(false); };
  window.persistModelRoleChains = async function persistModelRoleChains(scopes = MODEL_SCOPES.map((item) => item.key), successText = "模型链已保存。", { useDrafts = false } = {}) {
    state().saving = true; render();
    try {
      const roleSource = useDrafts ? S.modelCatalog.roleDrafts : S.modelCatalog.roles;
      let routes = null;
      for (const scope of [...new Set(scopes.map((item) => trim(item)).filter(Boolean))]) routes = await ApiClient.updateLlmRoute(scope, normalizeModelRoleChain(roleSource[scope] || []));
      state().routes = normalizeAllModelRoles(routes || state().routes);
      S.modelCatalog.roleEditing = false;
      S.modelCatalog.rolesDirty = false;
      showToast({ title: "模型链已保存", text: successText, kind: "success" });
    } finally { state().saving = false; render(); }
  };
  window.handleModelRoleEditorAction = async function handleModelRoleEditorAction() {
    if (!S.modelCatalog.roleEditing) { startModelRoleEditing(); setHint(); return; }
    if (!S.modelCatalog.rolesDirty) { cancelModelRoleEditing(); setHint(); return; }
    await window.persistModelRoleChains(MODEL_SCOPES.map((item) => item.key), "Role routes 已保存。", { useDrafts: true });
  };

  state(); refs(); document.addEventListener("DOMContentLoaded", () => { void bootstrap(); });
})();
