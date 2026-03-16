(() => {
  const DEFAULT_RETRY_ON = ["network", "429", "5xx"];
  const SCOPE_LABELS = { ceo: "CEO", execution: "Execution", inspection: "Inspection" };

  function llmState() {
    if (!S.llmCenter || typeof S.llmCenter !== "object") {
      S.llmCenter = {
        loading: false,
        saving: false,
        error: "",
        templates: [],
        templateMap: {},
        templateDetailMap: {},
        bindings: [],
        bindingMap: {},
        routes: EMPTY_MODEL_ROLES(),
        editor: {
          open: false,
          mode: "",
          bindingKey: "",
          configId: "",
          modelKey: "",
          providerId: "",
          jsonText: "",
          validation: null,
          probe: null,
        },
        eventsBound: false,
      };
    }
    return S.llmCenter;
  }

  function refs() {
    U.llmConfigCreate = document.getElementById("llm-config-create-btn");
    U.llmBindingsList = document.getElementById("llm-bindings-list");
    U.llmEditorPanel = document.querySelector(".llm-editor-panel");
    U.llmEditorShell = document.getElementById("llm-editor-shell");
    U.llmEditorBackdrop = document.getElementById("llm-editor-backdrop");
    U.modelList = U.llmBindingsList || U.modelList;
  }

  function escv(value) {
    return esc(String(value == null ? "" : value));
  }

  function trim(value) {
    return String(value || "").trim();
  }

  function capabilityLabel(value) {
    return ({ chat: "Chat", embedding: "Embedding", rerank: "Rerank" })[String(value || "")] || String(value || "-");
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

  function mapify() {
    const state = llmState();
    state.templateMap = Object.fromEntries(state.templates.map((item) => [trim(item.provider_id), item]));
    state.bindingMap = Object.fromEntries(state.bindings.map((item) => [trim(item.key), item]));
  }

  function currentBinding() {
    return llmState().bindingMap[trim(llmState().editor.bindingKey)] || null;
  }

  function projectRoutes() {
    const state = llmState();
    const chatBindings = state.bindings.filter((item) => String(item.capability || "chat") === "chat");
    S.modelCatalog.catalog = chatBindings.map((item) => ({ ...item, key: trim(item.key) }));
    S.modelCatalog.items = chatBindings.map((item) => trim(item.key));
    S.modelCatalog.roles = normalizeAllModelRoles(state.routes || EMPTY_MODEL_ROLES());
    if (S.modelCatalog.roleEditing) {
      S.modelCatalog.roleDrafts = normalizeAllModelRoles(S.modelCatalog.roleDrafts || EMPTY_MODEL_ROLES());
      syncModelRoleDraftState();
    } else {
      S.modelCatalog.roleDrafts = cloneModelRoles(S.modelCatalog.roles);
      S.modelCatalog.rolesDirty = false;
    }
    S.modelCatalog.defaults = {
      ...DEFAULT_MODEL_DEFAULTS(),
      ceo: S.modelCatalog.roles.ceo[0] || "",
      execution: S.modelCatalog.roles.execution[0] || "",
      inspection: S.modelCatalog.roles.inspection[0] || "",
    };
  }

  function renderHint() {
    const state = llmState();
    if (state.loading) return hint("正在加载模型配置...");
    if (state.saving) return hint("正在保存模型配置...");
    if (state.error) return hint(`模型配置出错：${state.error}`, true);
    if (!state.bindings.length) return hint("还没有保存的模型。点击“新建配置”开始添加。", false);
    if (S.modelCatalog.roleEditing && S.modelCatalog.rolesDirty) return hint("模型链已修改，点击“保存模型链”提交。", false);
    if (S.modelCatalog.roleEditing) return hint("拖拽左侧模型到右侧 Role Routes，完成后点击“保存模型链”。", false);
    return hint("点击左侧模型可以查看对应 JSON 配置；点击“新建配置”可添加新模型。", false);
  }

  async function ensureTemplate(providerId) {
    const id = trim(providerId);
    if (!id) return null;
    const state = llmState();
    if (state.templateDetailMap[id]) return state.templateDetailMap[id];
    const detail = await ApiClient.getLlmTemplate(id);
    if (detail) state.templateDetailMap[id] = detail;
    return detail;
  }

  function buildDraftFromTemplate(providerId) {
    const state = llmState();
    const detail = state.templateDetailMap[trim(providerId)] || null;
    const summary = state.templateMap[trim(providerId)] || {};
    const draft = {
      provider_id: trim(providerId),
      capability: "chat",
      auth_mode: String(summary.auth_mode || "api_key"),
      display_name: summary.display_name || trim(providerId),
      api_key: String(summary.auth_mode || "api_key") === "oauth_cache" ? "oauth-cache" : "",
      base_url: String(detail?.provider?.default_base_url || ""),
      default_model: String(detail?.provider?.default_model || summary.default_model || ""),
      parameters: {},
      extra_headers: {},
      extra_options: {},
    };
    (detail?.fields || []).forEach((field) => {
      if (field.default === undefined || field.default === null || field.default === "") return;
      setPath(draft, field.path || field.key, field.default);
    });
    return draft;
  }

  function draftFromConfig(record) {
    return {
      provider_id: record?.provider_id || "",
      capability: record?.capability || "chat",
      auth_mode: record?.auth_mode || "api_key",
      display_name: record?.display_name || record?.provider_id || "",
      api_key: record?.auth?.api_key || record?.api_key || "",
      base_url: record?.base_url || record?.api_base || "",
      default_model: record?.default_model || "",
      parameters: record?.parameters || {},
      extra_headers: record?.headers || record?.extra_headers || {},
      extra_options: record?.extra_options || {},
    };
  }

  function parseDraftJson(raw, providerId) {
    const text = trim(raw);
    if (!text) throw new Error("JSON 配置不能为空");
    const parsed = JSON.parse(text);
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) throw new Error("JSON 配置必须是对象");
    parsed.provider_id = trim(parsed.provider_id || providerId);
    parsed.capability = trim(parsed.capability || "chat") || "chat";
    parsed.auth_mode = trim(parsed.auth_mode || "api_key") || "api_key";
    parsed.parameters = parsed.parameters && typeof parsed.parameters === "object" && !Array.isArray(parsed.parameters) ? parsed.parameters : {};
    parsed.extra_headers = parsed.extra_headers && typeof parsed.extra_headers === "object" && !Array.isArray(parsed.extra_headers) ? parsed.extra_headers : {};
    parsed.extra_options = parsed.extra_options && typeof parsed.extra_options === "object" && !Array.isArray(parsed.extra_options) ? parsed.extra_options : {};
    return parsed;
  }

  async function probeDraft(draft) {
    const state = llmState();
    state.editor.validation = await ApiClient.validateLlmDraft(draft);
    if (!state.editor.validation?.valid) {
      state.editor.probe = null;
      renderAll();
      return false;
    }
    state.editor.probe = await ApiClient.probeLlmDraft(draft);
    renderAll();
    return !!state.editor.probe?.success;
  }

  async function openCreateModal() {
    const state = llmState();
    const provider = state.templates[0]?.provider_id || "";
    if (provider) await ensureTemplate(provider);
    const draft = provider ? buildDraftFromTemplate(provider) : {
      provider_id: "",
      capability: "chat",
      auth_mode: "api_key",
      display_name: "",
      api_key: "",
      base_url: "",
      default_model: "",
      parameters: {},
      extra_headers: {},
      extra_options: {},
    };
    state.editor = {
      open: true,
      mode: "create",
      bindingKey: "",
      configId: "",
      modelKey: "",
      providerId: provider,
      jsonText: JSON.stringify(draft, null, 2),
      validation: null,
      probe: null,
    };
    renderAll();
  }

  async function openDetailModal(modelKey) {
    const binding = llmState().bindingMap[trim(modelKey)] || null;
    if (!binding) return;
    const record = await ApiClient.getLlmConfig(binding.config_id || binding.llm_config_id, { includeSecrets: true });
    llmState().editor = {
      open: true,
      mode: "detail",
      bindingKey: trim(binding.key),
      configId: trim(binding.config_id || binding.llm_config_id),
      modelKey: trim(binding.key),
      providerId: trim(record?.provider_id || ""),
      jsonText: JSON.stringify(draftFromConfig(record), null, 2),
      validation: null,
      probe: null,
    };
    renderAll();
  }

  function closeEditor() {
    llmState().editor = {
      open: false,
      mode: "",
      bindingKey: "",
      configId: "",
      modelKey: "",
      providerId: "",
      jsonText: "",
      validation: null,
      probe: null,
    };
    renderAll();
  }

  function renderStatus() {
    const { validation, probe } = llmState().editor;
    if (!validation && !probe) return "";
    const validationMarkup = validation
      ? `<div class="llm-validation-status ${validation.valid ? "is-success" : "is-error"}"><strong>验证结果</strong><div>${validation.valid ? "字段校验通过。" : "字段校验未通过。"}</div>${Array.isArray(validation.errors) && validation.errors.length ? `<ul>${validation.errors.map((item) => `<li>${escv(item.field || "field")}：${escv(item.message || item.code || "错误")}</li>`).join("")}</ul>` : ""}</div>`
      : "";
    const probeMarkup = probe
      ? `<div class="llm-probe-status ${probe.success ? "is-success" : "is-error"}"><strong>连接测试</strong><div>${escv(probe.message || (probe.success ? "连接成功" : "连接失败"))}</div></div>`
      : "";
    return validationMarkup + probeMarkup;
  }

  function renderEditor() {
    refs();
    const state = llmState();
    if (!U.llmEditorShell || !U.llmEditorPanel || !U.llmEditorBackdrop) return;
    if (!state.editor.open) {
      U.llmEditorShell.innerHTML = "";
      setDrawerOpen(U.llmEditorBackdrop, U.llmEditorPanel, false);
      return;
    }

    if (state.editor.mode === "create") {
      U.llmEditorShell.innerHTML = `
        <article class="model-detail-card model-config-shell">
          <div class="detail-modal-header model-config-header">
            <div class="detail-modal-title">
              <h2>新建模型</h2>
              <p class="subtitle">先选择供应商，再基于预设 JSON 模板修改参数并测试连接。</p>
            </div>
            <div class="detail-modal-actions">
              <button type="button" class="toolbar-btn ghost" data-llm-action="close">关闭</button>
            </div>
          </div>
          <div class="detail-modal-body model-config-body">
            <div class="llm-section">
              <div class="llm-form-grid">
                <label class="resource-field">
                  <span class="resource-field-label">模型 Key *</span>
                  <input id="llm-model-key-input" class="resource-search" type="text" value="${escv(state.editor.modelKey)}" placeholder="例如：ceo_primary">
                </label>
                <label class="resource-field">
                  <span class="resource-field-label">供应商</span>
                  <select id="llm-provider-select" class="resource-search">${state.templates.map((item) => `<option value="${escv(item.provider_id)}"${trim(item.provider_id) === trim(state.editor.providerId) ? " selected" : ""}>${escv(item.display_name || item.provider_id)}</option>`).join("")}</select>
                </label>
              </div>
              <label class="resource-field">
                <span class="resource-field-label">JSON 配置</span>
                <textarea id="llm-json-editor" class="llm-json-editor" rows="18" spellcheck="false">${escv(state.editor.jsonText)}</textarea>
              </label>
              ${renderStatus()}
              <div class="llm-inline-actions">
                <button type="button" class="toolbar-btn ghost" data-llm-action="test-create">测试连接</button>
                <button type="button" class="toolbar-btn success" data-llm-action="save-create">添加模型</button>
              </div>
            </div>
          </div>
        </article>`;
    } else {
      const binding = currentBinding();
      U.llmEditorShell.innerHTML = `
        <article class="model-detail-card model-config-shell">
          <div class="detail-modal-header model-config-header">
            <div class="detail-modal-title">
              <h2>${escv(binding?.key || state.editor.bindingKey)}</h2>
              <p class="subtitle">仅显示并编辑当前模型对应的 JSON 配置。</p>
            </div>
            <div class="detail-modal-actions">
              <button type="button" class="toolbar-btn ghost" data-llm-action="close">关闭</button>
            </div>
          </div>
          <div class="detail-modal-body model-config-body">
            <div class="llm-section">
              <label class="resource-field">
                <span class="resource-field-label">JSON 配置</span>
                <textarea id="llm-json-editor" class="llm-json-editor" rows="18" spellcheck="false">${escv(state.editor.jsonText)}</textarea>
              </label>
              ${renderStatus()}
              <div class="llm-inline-actions">
                <button type="button" class="toolbar-btn ghost" data-llm-action="test-detail">测试连接</button>
                <button type="button" class="toolbar-btn success" data-llm-action="save-detail">保存修改</button>
                <button type="button" class="toolbar-btn danger" data-llm-action="delete-detail">删除模型</button>
              </div>
            </div>
          </div>
        </article>`;
    }

    setDrawerOpen(U.llmEditorBackdrop, U.llmEditorPanel, true);
    icons();
  }

  function renderBindings() {
    refs();
    if (!U.llmBindingsList) return;
    const query = trim(S.modelCatalog.search || "").toLowerCase();
    const items = [...llmState().bindings]
      .filter((item) => String(item.capability || "chat") === "chat")
      .filter((item) => !query || [item.key, item.provider_model, item.description].join("\n").toLowerCase().includes(query))
      .sort((a, b) => String(a.key || "").localeCompare(String(b.key || "")));

    if (!items.length) {
      U.llmBindingsList.innerHTML = `<div class="empty-state compact">${query ? "没有匹配的模型。" : "还没有保存的模型。"}</div>`;
      return;
    }

    U.llmBindingsList.innerHTML = items.map((item) => {
      const scopes = MODEL_SCOPES.filter((scope) => (llmState().routes?.[scope.key] || []).includes(item.key)).map((scope) => scope.key);
      const canDrag = S.modelCatalog.roleEditing;
      return `
        <article class="llm-binding-card model-available-item${trim(item.key) === trim(llmState().editor.bindingKey) ? " is-selected" : ""}" data-model-available-key="${escv(item.key)}"${canDrag ? ' draggable="true"' : ""}>
          <div class="llm-binding-card-head">
            <button type="button" class="model-available-main" data-model-open="${escv(item.key)}">
              <span class="resource-list-title">${escv(item.key)}</span>
              <span class="resource-list-subtitle">${escv(item.provider_model)}</span>
            </button>
            <span class="llm-capability-badge chat">Chat</span>
          </div>
          <div class="llm-binding-meta">${escv(item.description || "未填写描述")}</div>
          <div class="model-inline-meta">${item.enabled === false ? '<span class="policy-chip neutral">Disabled</span>' : '<span class="policy-chip risk-low">Enabled</span>'}${scopes.length ? scopes.map((scope) => `<span class="policy-chip neutral">${escv(SCOPE_LABELS[scope] || scope)}</span>`).join("") : '<span class="policy-chip neutral">未进入 Role Routes</span>'}</div>
          <div class="llm-inline-actions">
            <button class="toolbar-btn ghost small" type="button" data-model-open="${escv(item.key)}">详情</button>
          </div>
        </article>`;
    }).join("");
  }

  function renderRoutes() {
    if (!U.modelRoleEditors) return;
    const editing = !!S.modelCatalog.roleEditing;
    U.modelRoleEditors.innerHTML = MODEL_SCOPES.map((scope) => {
      const chain = modelScopeChain(scope.key);
      return `
        <section class="model-chain-card">
          <div class="panel-header">
            <div>
              <h3>${escv(SCOPE_LABELS[scope.key] || scope.key)}</h3>
              <p class="subtitle">${escv(chain[0] ? `默认：${chain[0]}` : "尚未配置")}</p>
            </div>
            <span class="policy-chip neutral">${chain.length} 个模型</span>
          </div>
          <div class="model-role-section">
            <div class="model-role-section-title">ROLE CHAIN</div>
            <div class="model-chain-list" data-model-chain-list="${scope.key}">${chain.length ? chain.map((ref, index) => {
              const item = llmState().bindingMap[trim(ref)] || modelRefItem(ref);
              const key = trim(item?.key || ref);
              return `<article class="model-chain-slide${editing ? ' is-editing' : ''}"${editing ? ' draggable="true"' : ''} data-model-chain-ref="${escv(key)}" data-scope="${scope.key}">${editing ? '<button type="button" class="model-chain-handle" aria-label="拖拽排序"><span class="model-chain-grip" aria-hidden="true">&#9776;</span></button>' : ''}<button type="button" class="model-chain-main" data-model-open="${escv(key)}"><span class="resource-list-title">${escv(key)}</span><span class="resource-list-subtitle">${escv(item?.provider_model || ref)}</span><span class="model-inline-meta">${index === 0 ? '<span class="policy-chip risk-low">首选</span>' : ''}</span></button>${editing ? `<button type="button" class="toolbar-btn ghost small" data-model-chain-action="remove" data-scope="${scope.key}" data-index="${index}">移除</button>` : ''}</article>`;
            }).join("") : `<div class="empty-state compact">${editing ? '将左侧模型拖到这里，编排当前角色链。' : '点击“编辑模型链”后再调整角色链。'}</div>`}</div>
          </div>
        </section>`;
    }).join("");
  }

  function renderAll() {
    refs();
    projectRoutes();
    if (U.modelRolesSave) U.modelRolesSave.textContent = S.modelCatalog.roleEditing ? (S.modelCatalog.rolesDirty ? "保存模型链" : "完成编辑") : "编辑模型链";
    renderHint();
    renderBindings();
    renderRoutes();
    renderEditor();
    icons();
  }

  async function loadAll() {
    const state = llmState();
    state.loading = true;
    state.error = "";
    renderAll();
    try {
      const [templates, bindingPayload] = await Promise.all([ApiClient.getLlmTemplates(), ApiClient.listLlmBindings()]);
      state.templates = Array.isArray(templates) ? templates : [];
      state.bindings = Array.isArray(bindingPayload?.items) ? bindingPayload.items : [];
      state.routes = normalizeAllModelRoles(bindingPayload?.routes || EMPTY_MODEL_ROLES());
      mapify();
    } catch (error) {
      state.error = error.message || "加载失败";
    } finally {
      state.loading = false;
      renderAll();
    }
  }

  async function handleProviderChange() {
    const state = llmState();
    const select = document.getElementById("llm-provider-select");
    const providerId = trim(select?.value);
    if (!providerId) return;
    state.editor.providerId = providerId;
    await ensureTemplate(providerId);
    state.editor.jsonText = JSON.stringify(buildDraftFromTemplate(providerId), null, 2);
    state.editor.validation = null;
    state.editor.probe = null;
    renderAll();
  }

    async function handleTest() {
    const state = llmState();
    const jsonText = document.getElementById("llm-json-editor")?.value || state.editor.jsonText;
    const providerId = trim(document.getElementById("llm-provider-select")?.value || state.editor.providerId);
    state.editor.jsonText = jsonText;
    state.editor.providerId = providerId;
    const draft = parseDraftJson(jsonText, providerId);
    showToast({
      title: "检测连接中",
      text: "正在验证当前 JSON 配置并测试连接...",
      kind: "success",
      persistent: true,
    });
    await probeDraft(draft);
    if (state.editor.probe?.success) {
      showToast({
        title: "连接测试成功",
        text: "当前模型配置可用。",
        kind: "success",
      });
      return;
    }
    const validationErrors = Array.isArray(state.editor.validation?.errors) ? state.editor.validation.errors : [];
    const validationMessage = validationErrors.length
      ? validationErrors.map((item) => `${item.field || "field"}: ${item.message || item.code || "错误"}`).join("；")
      : "请检查 JSON 配置中的必填项和字段格式。";
    const probeMessage = state.editor.probe?.message || "连接测试未通过，请检查密钥、地址和模型配置。";
    showToast({
      title: "连接测试失败",
      text: state.editor.validation?.valid === false ? validationMessage : probeMessage,
      kind: "error",
    });
  }

    async function handleCreateSave() {
    const state = llmState();
    const modelKey = trim(document.getElementById("llm-model-key-input")?.value);
    const providerId = trim(document.getElementById("llm-provider-select")?.value || state.editor.providerId);
    const jsonText = document.getElementById("llm-json-editor")?.value || state.editor.jsonText;
    if (!modelKey) throw new Error("模型 Key 不能为空");
    state.editor.modelKey = modelKey;
    state.editor.providerId = providerId;
    state.editor.jsonText = jsonText;
    const draft = parseDraftJson(jsonText, providerId);
    state.saving = true;
    renderAll();
    try {
      showToast({
        title: "检测连接中",
        text: "正在验证当前 JSON 配置并测试连接...",
        kind: "success",
        persistent: true,
      });
      const ok = await probeDraft(draft);
      if (!ok) {
        const validationErrors = Array.isArray(state.editor.validation?.errors) ? state.editor.validation.errors : [];
        const validationMessage = validationErrors.length
          ? validationErrors.map((item) => `${item.field || "field"}: ${item.message || item.code || "错误"}`).join("；")
          : "请检查 JSON 配置中的必填项和字段格式。";
        const probeMessage = state.editor.probe?.message || "连接测试未通过，请检查密钥、地址和模型配置。";
        throw new Error(state.editor.validation?.valid === false ? validationMessage : probeMessage);
      }
      showToast({
        title: "正在保存",
        text: "连接检测通过，正在添加模型...",
        kind: "success",
        persistent: true,
      });
      await ApiClient.createLlmBinding({
        binding: {
          key: modelKey,
          config_id: "",
          enabled: true,
          description: "",
          retry_on: [...DEFAULT_RETRY_ON],
        },
        draft,
      });
      showToast({ title: "添加成功", text: `${modelKey} 已保存`, kind: "success" });
      closeEditor();
      await loadAll();
    } finally {
      state.saving = false;
      renderAll();
    }
  }

  async function handleDetailSave() {
    const state = llmState();
    const jsonText = document.getElementById("llm-json-editor")?.value || state.editor.jsonText;
    state.editor.jsonText = jsonText;
    const draft = parseDraftJson(jsonText, state.editor.providerId);
    state.saving = true;
    renderAll();
    try {
      showToast({
        title: "检测连接中",
        text: "正在验证当前 JSON 配置并测试连接...",
        kind: "success",
        persistent: true,
      });
      const ok = await probeDraft(draft);
      if (!ok) throw new Error("请先修正 JSON 配置并通过连接测试");
      showToast({
        title: "正在保存",
        text: "连接检测通过，正在保存修改...",
        kind: "success",
        persistent: true,
      });
      await ApiClient.updateLlmConfig(state.editor.configId, draft);
      showToast({ title: "修改成功", text: `${state.editor.bindingKey} 已更新`, kind: "success" });
      closeEditor();
      await loadAll();
    } finally {
      state.saving = false;
      renderAll();
    }
  }

  async function handleDelete() {
    const binding = currentBinding();
    if (!binding) return;
    const confirmed = window.confirm(`确认删除模型 ${binding.key} 吗？这会删除模型及其关联配置。`);
    if (!confirmed) return;
    llmState().saving = true;
    renderAll();
    try {
      await ApiClient.deleteLlmBinding(binding.key);
      showToast({ title: "删除成功", text: `${binding.key} 已删除`, kind: "success" });
      closeEditor();
      await loadAll();
    } finally {
      llmState().saving = false;
      renderAll();
    }
  }

  function bindList() {
    refs();
    if (!U.llmBindingsList) return;
    U.llmBindingsList.addEventListener("click", (event) => {
      const open = event.target.closest("[data-model-open]");
      if (open) void openDetailModal(open.dataset.modelOpen);
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

  async function bootstrap() {
    if (llmState().eventsBound) return;
    refs();
    bindList();
    U.llmConfigCreate?.addEventListener("click", () => void openCreateModal());
    U.llmEditorBackdrop?.addEventListener("click", closeEditor);
    U.llmEditorShell?.addEventListener("change", (event) => {
      if (event.target?.id === "llm-provider-select") void handleProviderChange();
    });
    U.llmEditorShell?.addEventListener("click", (event) => {
      const action = event.target.closest("[data-llm-action]")?.dataset.llmAction;
      if (!action) return;
      if (action === "close") { closeEditor(); return; }
      if (action === "test-create" || action === "test-detail") { void handleTest().catch((error) => { llmState().error = error.message || "测试失败"; showToast({ title: "测试失败", text: llmState().error, kind: "error" }); renderAll(); }); return; }
      if (action === "save-create") { void handleCreateSave().catch((error) => { llmState().error = error.message || "保存失败"; showToast({ title: "保存失败", text: llmState().error, kind: "error" }); renderAll(); }); return; }
      if (action === "save-detail") { void handleDetailSave().catch((error) => { llmState().error = error.message || "保存失败"; showToast({ title: "保存失败", text: llmState().error, kind: "error" }); renderAll(); }); return; }
      if (action === "delete-detail") { void handleDelete().catch((error) => { llmState().error = error.message || "删除失败"; showToast({ title: "删除失败", text: llmState().error, kind: "error" }); renderAll(); }); }
    });
    llmState().eventsBound = true;
  }

  window.renderModelList = renderBindings;
  window.renderModelRoleEditors = renderRoutes;
  window.renderModelDetail = renderEditor;
  window.renderModelCatalog = renderAll;
  window.openModel = function openModel(key) { void openDetailModal(key); };
  window.startCreateModel = function startCreateModel() { void openCreateModal(); };
  window.clearModelSelection = closeEditor;
  window.loadModels = async function loadModels() { await loadAll(); };
  window.handleModelRoleEditorAction = async function handleModelRoleEditorAction() {
    if (!S.modelCatalog.roleEditing) {
      startModelRoleEditing();
      renderAll();
      return;
    }
    if (!S.modelCatalog.rolesDirty) {
      cancelModelRoleEditing();
      renderAll();
      return;
    }
    llmState().saving = true;
    renderAll();
    try {
      showToast({
        title: "正在保存模型链",
        text: "正在提交当前 Role Routes 变更...",
        kind: "success",
        persistent: true,
      });
      let routes = null;
      for (const scope of MODEL_SCOPES.map((item) => item.key)) {
        routes = await ApiClient.updateLlmRoute(scope, normalizeModelRoleChain(S.modelCatalog.roleDrafts[scope] || []));
      }
      llmState().routes = normalizeAllModelRoles(routes || EMPTY_MODEL_ROLES());
      S.modelCatalog.roleEditing = false;
      S.modelCatalog.rolesDirty = false;
      showToast({ title: "保存成功", text: "模型链已更新", kind: "success" });
    } finally {
      llmState().saving = false;
      renderAll();
    }
  };

  refs();
  document.addEventListener("DOMContentLoaded", () => { void bootstrap(); });
})();