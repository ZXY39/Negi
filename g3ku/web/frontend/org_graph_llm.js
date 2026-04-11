(() => {
  const DEFAULT_RETRY_ON = ["network", "429", "5xx"];
  const SCOPE_LABELS = { ceo: "Leader", execution: "Execution", inspection: "Inspection" };

  function emptyMemorySection(label, capability) {
    return {
      label,
      capability,
      configId: "",
      providerId: "",
      providerModel: "",
      templateProviderId: "",
      jsonText: "",
      initialJsonText: "",
      validation: null,
      probe: null,
      error: "",
    };
  }

  function emptyEditorState() {
    return {
      open: false,
      mode: "",
      bindingKey: "",
      configId: "",
      modelKey: "",
      providerId: "",
      jsonText: "",
      initialJsonText: "",
      retryOn: [...DEFAULT_RETRY_ON],
      retryCount: 0,
      singleApiKeyMaxConcurrency: "",
      description: "",
      validation: null,
      probe: null,
      memory: {
        loading: false,
        error: "",
        embedding: emptyMemorySection("Embedding", "embedding"),
        rerank: emptyMemorySection("Rerank", "rerank"),
      },
    };
  }

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
        roleIterations: DEFAULT_ROLE_ITERATIONS(),
        roleConcurrency: DEFAULT_ROLE_CONCURRENCY(),
        editor: emptyEditorState(),
        eventsBound: false,
      };
    }
    return S.llmCenter;
  }

  function refs() {
    U.llmMemorySettings = document.getElementById("llm-memory-settings-btn");
    U.llmConfigCreate = document.getElementById("llm-config-create-btn");
    U.llmBindingsList = document.getElementById("llm-bindings-list");
    U.llmEditorPanel = document.querySelector(".llm-editor-panel");
    U.llmEditorShell = document.getElementById("llm-editor-shell");
    U.llmEditorBackdrop = document.getElementById("llm-editor-backdrop");
    U.modelRolesCancel = document.getElementById("model-roles-cancel-btn");
    U.modelList = U.llmBindingsList || U.modelList;
  }

  function escv(value) {
    return esc(String(value == null ? "" : value));
  }

  function trim(value) {
    return String(value || "").trim();
  }

  function bindingNameLabel() {
    return "配置名 / 绑定名";
  }

  function bindingNameRequiredMessage() {
    return `${bindingNameLabel()}不能为空`;
  }

  function normalizeBindingNameText(value) {
    let text = String(value == null ? "" : value);
    const variants = [
      "模型 Key",
      "妯″瀷 Key",
      "ДЈРН Key",
    ];
    variants.forEach((variant) => {
      text = text.replaceAll(variant, bindingNameLabel());
    });
    text = text.replaceAll(`${bindingNameLabel()} 不能为空`, bindingNameRequiredMessage());
    return text;
  }

  function capabilityLabel(value) {
    return ({ chat: "Chat", embedding: "Embedding", rerank: "Rerank" })[String(value || "")] || String(value || "-");
  }

  function parseApiKeysFromValue(value) {
    return String(value || "")
      .split(/[\n,]/)
      .map((item) => trim(item))
      .filter(Boolean);
  }

  function apiKeyCountFromValue(value) {
    return parseApiKeysFromValue(value).length;
  }

  function formatSingleApiKeyMaxConcurrencyValue(value) {
    if (Array.isArray(value)) return value.join(",");
    if (value === null || value === undefined || value === "") return "";
    return String(value);
  }

  function expandSingleApiKeyMaxConcurrencyForEditor(value, apiKeyValue) {
    if (Array.isArray(value)) return value.join(",");
    if (value === null || value === undefined || value === "") return "";
    const parsed = Number.parseInt(String(value), 10);
    if (!Number.isInteger(parsed) || parsed < 1) return trim(value);
    const apiKeyCount = apiKeyCountFromValue(apiKeyValue);
    if (apiKeyCount > 1) return Array(apiKeyCount).fill(parsed).join(",");
    return String(parsed);
  }

  function parseSingleApiKeyMaxConcurrencyInput(raw) {
    const text = trim(raw);
    if (!text) return null;
    if (/[\n,]/.test(text)) {
      const parts = text.split(/[\n,]/).map((item) => trim(item)).filter(Boolean);
      if (!parts.length) return null;
      return parts.map((item) => {
        const parsed = Number.parseInt(item, 10);
        if (!Number.isInteger(parsed) || parsed < 0) {
          throw new Error("单 API key 最大并发数列表必须是大于等于 0 的整数");
        }
        return parsed;
      });
    }
    const parsed = Number.parseInt(text, 10);
    if (!Number.isInteger(parsed) || parsed < 1) {
      throw new Error("single_api_key_max_concurrency must be >= 1");
    }
    return parsed;
  }

  function validateSingleApiKeyMaxConcurrencyInput(raw, apiKeyValue) {
    const parsed = parseSingleApiKeyMaxConcurrencyInput(raw);
    if (!Array.isArray(parsed)) return parsed;
    const apiKeyCount = apiKeyCountFromValue(apiKeyValue);
    if (parsed.length !== apiKeyCount) {
      throw new Error(`单 API key 最大并发数数量必须与 API key 数量一致，当前共有 ${apiKeyCount} 个 key`);
    }
    if (apiKeyCount > 0 && parsed.every((item) => item === 0)) {
      throw new Error("单 API key 最大并发数至少保留一个大于 0 的值");
    }
    return parsed;
  }

  function bindingNotesTitle() {
    return [
      "最大并发数填写 0 时，对应的 API Key 不会投入使用。",
      "多个 API Key 时，“重试次数”表示完整轮过所有 key 的次数，不是单次请求重试次数。",
      "“api_key” 支持用逗号或换行填写多个 key，例如 key1,key2，注意可能会导致缓存命中率下降。多个 key 会按并发数上限轮换；设置多个 key 时，“重试次数”以完整轮过所有 key 为一次。",
    ].join("\n");
  }

  function singleApiKeyMaxConcurrencyEquals(left, right) {
    return JSON.stringify(left ?? null) === JSON.stringify(right ?? null);
  }

  function renderBindingNoteAction() {
    return `<button type="button" class="icon-btn llm-note-btn" title="${escv(bindingNotesTitle())}" aria-label="配置备注"><i data-lucide="info"></i></button>`;
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
    S.modelCatalog.roleIterations = normalizeRoleIterations(state.roleIterations || DEFAULT_ROLE_ITERATIONS());
    S.modelCatalog.roleConcurrency = normalizeRoleConcurrency(state.roleConcurrency || DEFAULT_ROLE_CONCURRENCY());
    if (S.modelCatalog.roleEditing) {
      S.modelCatalog.roleDrafts = normalizeAllModelRoles(S.modelCatalog.roleDrafts || EMPTY_MODEL_ROLES());
      S.modelCatalog.roleIterationDrafts = normalizeRoleIterations(S.modelCatalog.roleIterationDrafts || DEFAULT_ROLE_ITERATIONS());
      S.modelCatalog.roleConcurrencyDrafts = normalizeRoleConcurrency(S.modelCatalog.roleConcurrencyDrafts || DEFAULT_ROLE_CONCURRENCY());
      syncModelRoleDraftState();
    } else {
      S.modelCatalog.roleDrafts = cloneModelRoles(S.modelCatalog.roles);
      S.modelCatalog.roleIterationDrafts = cloneRoleIterations(S.modelCatalog.roleIterations);
      S.modelCatalog.roleConcurrencyDrafts = cloneRoleConcurrency(S.modelCatalog.roleConcurrency);
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

  function buildDraftFromTemplate(providerId, capabilityOverride = "") {
    const state = llmState();
    const detail = state.templateDetailMap[trim(providerId)] || null;
    const summary = state.templateMap[trim(providerId)] || {};
    const draft = {
      provider_id: trim(providerId),
      capability: trim(capabilityOverride || summary.capability || "chat") || "chat",
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

  function providerModelFromDraft(draft) {
    const providerId = trim(draft?.provider_id);
    const defaultModel = trim(draft?.default_model);
    if (!providerId && !defaultModel) return "";
    const displayProvider = providerId === "dashscope_embedding" || providerId === "dashscope_rerank"
      ? "dashscope"
      : providerId;
    if (!displayProvider) return defaultModel;
    return defaultModel ? `${displayProvider}:${defaultModel}` : displayProvider;
  }

  function memoryTemplates(capability) {
    const expectedCapability = trim(capability);
    return llmState().templates.filter((item) => trim(item.capability || "chat") === expectedCapability);
  }

  function defaultMemoryTemplateProviderId(capability, providerId = "") {
    const requested = trim(providerId);
    const templates = memoryTemplates(capability);
    if (!templates.length) return "";
    if (requested && templates.some((item) => trim(item.provider_id) === requested)) return requested;
    return trim(templates[0]?.provider_id);
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

  function parseMemoryDraftJson(raw, providerId, capability, label) {
    const parsed = parseDraftJson(raw, providerId);
    const normalizedCapability = trim(parsed.capability || capability) || capability;
    if (normalizedCapability !== capability) {
      throw new Error(`${label} JSON 的 capability 必须保持为 ${capability}`);
    }
    parsed.capability = capability;
    return parsed;
  }

  function memoryTextareaId(sectionKey) {
    return `llm-memory-${sectionKey}-json`;
  }

  function memoryTemplateSelectId(sectionKey) {
    return `llm-memory-${sectionKey}-template`;
  }

  function draftFailureMessage(target, fallback = "请检查 JSON 配置中的必填项、地址和密钥。") {
    const validationErrors = Array.isArray(target?.validation?.errors) ? target.validation.errors : [];
    if (target?.validation?.valid === false) {
      return validationErrors.length
        ? validationErrors.map((item) => `${item.field || "field"}: ${item.message || item.code || "错误"}`).join("；")
        : fallback;
    }
    return target?.probe?.message || fallback;
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

  async function probeMemoryDraft(sectionKey, draft) {
    const section = llmState().editor.memory?.[sectionKey];
    if (!section) return false;
    section.validation = await ApiClient.validateLlmDraft(draft);
    if (!section.validation?.valid) {
      section.probe = null;
      renderAll();
      return false;
    }
    section.probe = await ApiClient.probeLlmDraft(draft);
    renderAll();
    return !!section.probe?.success;
  }

  async function loadMemorySection(editor, sectionKey, meta) {
    const section = editor.memory?.[sectionKey];
    if (!section) return;
    section.configId = trim(meta?.configId);
    section.providerId = "";
    section.providerModel = trim(meta?.providerModel);
    section.templateProviderId = "";
    section.jsonText = "";
    section.initialJsonText = "";
    section.validation = null;
    section.probe = null;
    section.error = "";
    section.modelKey = "";
    if (!section.configId) {
      section.error = `${section.label} 配置记录尚未创建。`;
      return;
    }
    {
      const record = await ApiClient.getLlmConfig(section.configId, { includeSecrets: true });
      const draft = draftFromConfig(record);
      section.providerId = trim(draft.provider_id);
      section.providerModel = trim(meta?.providerModel || providerModelFromDraft(draft));
      section.templateProviderId = defaultMemoryTemplateProviderId(section.capability, section.providerId);
      section.jsonText = JSON.stringify(draft, null, 2);
      section.initialJsonText = section.jsonText;
      return;
    }
    if (!section.modelKey) {
      section.error = `${section.label} 模型尚未配置。`;
      return;
    }
    const binding = llmState().bindingMap[section.modelKey] || null;
    if (!binding) {
      section.error = `${section.label} 绑定不存在：${section.modelKey}`;
      return;
    }
    section.configId = trim(binding.config_id || binding.llm_config_id);
    if (!section.configId) {
      section.error = `${section.label} 绑定缺少配置记录。`;
      return;
    }
    const record = await ApiClient.getLlmConfig(section.configId, { includeSecrets: true });
    const draft = draftFromConfig(record);
    section.providerId = trim(draft.provider_id);
    section.jsonText = JSON.stringify(draft, null, 2);
    section.initialJsonText = section.jsonText;
  }

  async function loadMemorySectionConfig(editor, sectionKey, meta) {
    const section = editor.memory?.[sectionKey];
    if (!section) return;
    section.configId = trim(meta?.configId);
    section.providerId = "";
    section.providerModel = trim(meta?.providerModel);
    section.templateProviderId = "";
    section.jsonText = "";
    section.initialJsonText = "";
    section.validation = null;
    section.probe = null;
    section.error = "";
    if (!section.configId) {
      section.templateProviderId = defaultMemoryTemplateProviderId(section.capability);
      if (!section.templateProviderId) {
        section.error = `${section.label} 暂无可用模板。`;
        return;
      }
      await ensureTemplate(section.templateProviderId);
      const draft = buildDraftFromTemplate(section.templateProviderId, section.capability);
      section.providerId = trim(draft.provider_id);
      section.providerModel = providerModelFromDraft(draft);
      section.jsonText = JSON.stringify(draft, null, 2);
      section.initialJsonText = section.jsonText;
      return;
    }
    const record = await ApiClient.getLlmConfig(section.configId, { includeSecrets: true });
    const draft = draftFromConfig(record);
    section.providerId = trim(draft.provider_id);
    section.providerModel = trim(meta?.providerModel || providerModelFromDraft(draft));
    section.templateProviderId = defaultMemoryTemplateProviderId(section.capability, section.providerId);
    section.jsonText = JSON.stringify(draft, null, 2);
    section.initialJsonText = section.jsonText;
  }

  function syncMemorySectionText(sectionKey) {
    const section = llmState().editor.memory?.[sectionKey];
    if (!section) return null;
    const textarea = document.getElementById(memoryTextareaId(sectionKey));
    if (textarea) section.jsonText = textarea.value || "";
    return section;
  }

  function memorySectionIsModified(section) {
    if (!section) return false;
    return trim(section.jsonText) !== trim(section.initialJsonText);
  }

  function memorySectionNeedsInitialSave(section) {
    if (!section) return false;
    return !trim(section.configId) && !trim(section.error) && !!trim(section.jsonText) && !trim(section.initialJsonText);
  }

  function memorySectionHasPendingChanges(section) {
    return memorySectionNeedsInitialSave(section) || memorySectionIsModified(section);
  }

  function modifiedMemorySectionKeys(memory = llmState().editor.memory) {
    return ["embedding", "rerank"].filter((sectionKey) => memorySectionHasPendingChanges(memory?.[sectionKey]));
  }

  function initialMemorySectionProviderModel(section) {
    if (!section) return "";
    const initialText = trim(section.initialJsonText || "");
    if (!initialText) return trim(section.providerModel || "");
    try {
      const draft = parseMemoryDraftJson(
        initialText,
        section.providerId || "",
        section.capability,
        section.label || section.capability || "memory"
      );
      return providerModelFromDraft(draft);
    } catch (_error) {
      return trim(section.providerModel || "");
    }
  }

  function memorySaveRequiresRebuildConfirmation({
    currentEmbeddingProviderModel = "",
    nextEmbeddingProviderModel = "",
    embeddingModified = false,
    rerankModified = false,
  } = {}) {
    void rerankModified;
    const currentModel = trim(currentEmbeddingProviderModel);
    const nextModel = trim(nextEmbeddingProviderModel);
    return Boolean(embeddingModified && currentModel && nextModel && currentModel !== nextModel);
  }

  function memorySectionKeyFromTextareaId(elementId) {
    const text = trim(elementId);
    if (!text.startsWith("llm-memory-") || !text.endsWith("-json")) return "";
    return trim(text.slice("llm-memory-".length, -"-json".length));
  }

  function refreshMemorySaveButtonState() {
    const button = U.llmEditorShell?.querySelector('[data-llm-action="save-memory"]');
    if (!button) return;
    button.disabled = !canSaveMemoryEditor() || !!llmState().saving;
  }

  function handleMemorySectionInput(sectionKey) {
    const section = syncMemorySectionText(sectionKey);
    if (!section) return;
    section.validation = null;
    section.probe = null;
    refreshMemorySaveButtonState();
  }

  function setMemorySectionValidationError(section, message, field = "json") {
    if (!section) return;
    section.validation = {
      valid: false,
      errors: [{ field, code: "invalid", message: String(message || "Validation failed.") }],
    };
    section.probe = null;
  }

  function prepareMemorySectionDraft(sectionKey) {
    const section = syncMemorySectionText(sectionKey);
    if (!section) {
      return { section: null, draft: null, message: "Section not found." };
    }
    if (!trim(section.jsonText)) {
      const message = `${section.label || sectionKey} JSON is empty.`;
      setMemorySectionValidationError(section, message);
      return { section, draft: null, message };
    }
    try {
      const draft = parseMemoryDraftJson(
        section.jsonText || "",
        section.providerId || "",
        section.capability,
        section.label || sectionKey
      );
      section.providerId = trim(draft.provider_id);
      section.providerModel = providerModelFromDraft(draft);
      section.templateProviderId = defaultMemoryTemplateProviderId(section.capability, section.providerId);
      return { section, draft, message: "" };
    } catch (error) {
      const message = error?.message || `${section.label || sectionKey} JSON is invalid.`;
      setMemorySectionValidationError(section, message);
      return { section, draft: null, message };
    }
  }

  async function persistMemorySectionDraft(section, draft) {
    if (!section || !draft) return null;
    const config = trim(section.configId)
      ? await ApiClient.updateLlmConfig(section.configId, draft)
      : await ApiClient.createLlmConfig(draft);
    section.configId = trim(config?.config_id || section.configId);
    return config;
  }

  async function handleMemoryTemplateChange(sectionKey) {
    const section = llmState().editor.memory?.[sectionKey];
    if (!section) return;
    const providerId = trim(document.getElementById(memoryTemplateSelectId(sectionKey))?.value || section.templateProviderId);
    if (!providerId) return;
    await ensureTemplate(providerId);
    const draft = buildDraftFromTemplate(providerId, section.capability);
    section.templateProviderId = providerId;
    section.providerId = trim(draft.provider_id);
    section.providerModel = providerModelFromDraft(draft);
    section.jsonText = JSON.stringify(draft, null, 2);
    section.validation = null;
    section.probe = null;
    section.error = "";
    renderAll();
  }

  function canSaveMemoryEditor() {
    const memory = llmState().editor.memory;
    return Boolean(
      memory
      && !memory.loading
      && !trim(memory.error)
      && modifiedMemorySectionKeys(memory).length
    );
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
      ...emptyEditorState(),
      open: true,
      mode: "create",
      providerId: provider,
      jsonText: JSON.stringify(draft, null, 2),
      initialJsonText: JSON.stringify(draft, null, 2),
      retryOn: [...DEFAULT_RETRY_ON],
      retryCount: 0,
      description: "",
    };
    renderAll();
  }

  async function openDetailModal(modelKey) {
    const binding = llmState().bindingMap[trim(modelKey)] || null;
    if (!binding) return;
    const record = await ApiClient.getLlmConfig(binding.config_id || binding.llm_config_id, { includeSecrets: true });
    const draft = draftFromConfig(record);
    const jsonText = JSON.stringify(draft, null, 2);
    llmState().editor = {
      ...emptyEditorState(),
      open: true,
      mode: "detail",
      bindingKey: trim(binding.key),
      configId: trim(binding.config_id || binding.llm_config_id),
      modelKey: trim(binding.key),
      providerId: trim(record?.provider_id || ""),
      jsonText,
      initialJsonText: jsonText,
      retryOn: Array.isArray(binding.retry_on) ? binding.retry_on.map((item) => trim(item)).filter(Boolean) : [...DEFAULT_RETRY_ON],
      retryCount: Number.isInteger(Number(binding.retry_count)) ? Math.max(0, Number(binding.retry_count)) : 0,
      singleApiKeyMaxConcurrency: expandSingleApiKeyMaxConcurrencyForEditor(
        binding.single_api_key_max_concurrency ?? binding.singleApiKeyMaxConcurrency ?? "",
        draft.api_key || ""
      ),
      description: trim(binding.description),
    };
    renderAll();
  }

  async function openMemoryModal() {
    const state = llmState();
    if (!state.loading && !state.bindings.length) {
      await loadAll();
    }
    const editor = {
      ...emptyEditorState(),
      open: true,
      mode: "memory",
      memory: {
        loading: true,
        error: "",
        embedding: emptyMemorySection("Embedding", "embedding"),
        rerank: emptyMemorySection("Rerank", "rerank"),
      },
    };
    state.editor = editor;
    renderAll();
    try {
      const memoryBinding = await ApiClient.getLlmMemoryModels();
      if (llmState().editor !== editor) return;
      await Promise.all([
        loadMemorySectionConfig(editor, "embedding", {
          configId: memoryBinding?.embedding_config_id,
          providerModel: memoryBinding?.embedding_provider_model,
        }),
        loadMemorySectionConfig(editor, "rerank", {
          configId: memoryBinding?.rerank_config_id,
          providerModel: memoryBinding?.rerank_provider_model,
        }),
      ]);
    } catch (error) {
      if (llmState().editor !== editor) return;
      editor.memory.error = error.message || "记忆模型配置加载失败";
    } finally {
      if (llmState().editor !== editor) return;
      editor.memory.loading = false;
      renderAll();
    }
  }

  function closeEditor() {
    llmState().editor = emptyEditorState();
    renderAll();
  }

  function renderStatus(target = llmState().editor) {
    const { validation, probe } = target || {};
    if (!validation && !probe) return "";
    const validationMarkup = validation
      ? `<div class="llm-validation-status ${validation.valid ? "is-success" : "is-error"}"><strong>验证结果</strong><div>${validation.valid ? "字段校验通过。" : "字段校验未通过。"}</div>${Array.isArray(validation.errors) && validation.errors.length ? `<ul>${validation.errors.map((item) => `<li>${escv(item.field || "field")}：${escv(item.message || item.code || "错误")}</li>`).join("")}</ul>` : ""}</div>`
      : "";
    const probeMarkup = probe
      ? `<div class="llm-probe-status ${probe.success ? "is-success" : "is-error"}"><strong>连接测试</strong><div>${escv(probe.message || (probe.success ? "连接成功" : "连接失败"))}</div></div>`
      : "";
    return validationMarkup + probeMarkup;
  }

  function renderMemorySection(sectionKey, section) {
    const templates = memoryTemplates(section?.capability);
    return `
      <section class="llm-section llm-memory-section">
        <div class="llm-memory-row">
          <div>
            <h3>${escv(section?.label || sectionKey)}</h3>
            <p class="llm-muted">保存时会自动验证 JSON 并测试当前连接。</p>
          </div>
          <span class="llm-capability-badge ${escv(section?.capability || sectionKey)}">${escv(capabilityLabel(section?.capability || sectionKey))}</span>
        </div>
        <div class="llm-memory-meta">
          <span class="policy-chip neutral">Config: ${escv(section?.configId || "-")}</span>
          <span class="policy-chip neutral">${escv(section?.providerModel || section?.providerId || "-")}</span>
        </div>
        ${section?.error ? `<div class="llm-memory-banner">${escv(section.error)}</div>` : ""}
        <div class="llm-form-grid single">
          <label class="resource-field">
            <span class="resource-field-label">模板</span>
            <select id="${escv(memoryTemplateSelectId(sectionKey))}" class="resource-search resource-select" data-llm-memory-template="${escv(sectionKey)}" data-resource-select-label="${escv(`${section?.label || sectionKey} template`)}"${section?.error || !templates.length ? " disabled" : ""}>
              ${templates.length
                ? templates.map((item) => `<option value="${escv(item.provider_id)}"${trim(item.provider_id) === trim(section?.templateProviderId) ? " selected" : ""}>${escv(item.display_name || item.provider_id)}</option>`).join("")
                : '<option value="">暂无可用模板</option>'}
            </select>
          </label>
        </div>
        <label class="resource-field">
          <span class="resource-field-label">JSON 配置</span>
          <textarea id="${escv(memoryTextareaId(sectionKey))}" class="llm-json-editor" rows="18" spellcheck="false"${section?.error ? " disabled" : ""}>${escv(section?.jsonText || "")}</textarea>
        </label>
        <p class="llm-muted">"api_key" 支持用逗号或换行填写多把 key，例如 key1,key2。多个 key 会按顺序轮换。</p>
        ${renderStatus(section)}
      </section>`;
  }

  function parseBindingRetryOn(raw) {
    return String(raw || "")
      .split(/[\n,]/)
      .map((item) => trim(item))
      .filter(Boolean);
  }

  function syncBindingInputs() {
    const editor = llmState().editor;
    if (!editor || editor.mode === "memory") return editor;
    const modelKeyInput = document.getElementById("llm-model-key-input");
    const retryOnInput = document.getElementById("llm-binding-retry-on");
    const retryCountInput = document.getElementById("llm-binding-retry-count");
    const singleApiKeyMaxConcurrencyInput = document.getElementById("llm-binding-single-api-key-max-concurrency");
    const descriptionInput = document.getElementById("llm-binding-description");
    if (modelKeyInput) editor.modelKey = trim(modelKeyInput.value || editor.modelKey);
    if (retryOnInput) editor.retryOn = parseBindingRetryOn(retryOnInput.value || "");
    if (retryCountInput) {
      const parsed = Number.parseInt(String(retryCountInput.value || "").trim(), 10);
      editor.retryCount = Number.isInteger(parsed) && parsed >= 0 ? parsed : 0;
    }
    if (singleApiKeyMaxConcurrencyInput) {
      editor.singleApiKeyMaxConcurrency = trim(singleApiKeyMaxConcurrencyInput.value || "");
    }
    if (descriptionInput) editor.description = trim(descriptionInput.value || "");
    return editor;
  }

  function bindingDraftPayload({ requireModelKey = false } = {}) {
    const editor = syncBindingInputs();
    const modelKey = trim(editor?.modelKey);
    const retryOn = Array.isArray(editor?.retryOn) ? editor.retryOn.map((item) => trim(item)).filter(Boolean) : [];
    const retryCount = Number.parseInt(String(editor?.retryCount ?? 0), 10);
    const draft = parseDraftJson(editor?.jsonText || "", editor?.providerId || "");
    const singleApiKeyMaxConcurrency = validateSingleApiKeyMaxConcurrencyInput(
      String(editor?.singleApiKeyMaxConcurrency ?? ""),
      draft.api_key || ""
    );
    const description = trim(editor?.description);
    if (requireModelKey && !modelKey) throw new Error("模型 Key 不能为空");
    if (!Number.isInteger(retryCount) || retryCount < 0) {
      throw new Error("重试次数必须是不小于 0 的整数");
    }
    if (singleApiKeyMaxConcurrency !== null && (!Number.isInteger(singleApiKeyMaxConcurrency) || singleApiKeyMaxConcurrency < 1)) {
      throw new Error("single_api_key_max_concurrency must be >= 1");
    }
    return {
      modelKey,
      retryOn: retryOn.length ? retryOn : [...DEFAULT_RETRY_ON],
      retryCount,
      singleApiKeyMaxConcurrency,
      description,
    };
  }

  function renderBindingPolicyFields() {
    const editor = llmState().editor || emptyEditorState();
    return `
      <div class="llm-form-grid">
        <label class="resource-field">
          <span class="resource-field-label">Retry On</span>
          <input id="llm-binding-retry-on" class="resource-search" type="text" value="${escv((editor.retryOn || DEFAULT_RETRY_ON).join(", "))}" placeholder="如 network, 429, 5xx">
        </label>
        <label class="resource-field">
          <span class="resource-field-label">重试次数</span>
          <input id="llm-binding-retry-count" class="resource-search" type="number" min="0" step="1" inputmode="numeric" value="${escv(String(editor.retryCount ?? 0))}" placeholder="0">
        </label>
        <label class="resource-field">
          <span class="resource-field-label">单 API key 最大并发数</span>
          <input id="llm-binding-single-api-key-max-concurrency" class="resource-search" type="number" min="1" step="1" inputmode="numeric" value="${escv(String(editor.singleApiKeyMaxConcurrency ?? ""))}" placeholder="留空表示不限制">
        </label>
      </div>
      <p class="llm-muted">多个 API Key 时，"retry_count" 表示完整轮过所有 key 的次数，不是单次请求重试次数。</p>
      <label class="resource-field">
        <span class="resource-field-label">说明</span>
        <textarea id="llm-binding-description" class="resource-editor model-textarea" rows="4" placeholder="用于备注当前模型的用途、成本或降级策略。">${escv(editor.description || "")}</textarea>
      </label>`;
  }

  function renderApiKeyJsonHint() {
    return '<p class="llm-muted">"api_key" 支持用逗号或换行填写多把 key，例如 key1,key2。多个 key 会按顺序轮换；设置多个 key 时，"retry_count" 以完整轮过所有 key 为一次。</p>';
  }

  function bindingDraftPayload({ requireModelKey = false } = {}) {
    const editor = syncBindingInputs();
    const modelKey = trim(editor?.modelKey);
    const retryOn = Array.isArray(editor?.retryOn) ? editor.retryOn.map((item) => trim(item)).filter(Boolean) : [];
    const retryCount = Number.parseInt(String(editor?.retryCount ?? 0), 10);
    const draft = parseDraftJson(editor?.jsonText || "", editor?.providerId || "");
    const singleApiKeyMaxConcurrency = validateSingleApiKeyMaxConcurrencyInput(
      String(editor?.singleApiKeyMaxConcurrency ?? ""),
      draft.api_key || ""
    );
    const description = trim(editor?.description);
    if (requireModelKey && !modelKey) throw new Error("妯″瀷 Key 涓嶈兘涓虹┖");
    if (!Number.isInteger(retryCount) || retryCount < 0) {
      throw new Error("重试次数必须是不小于 0 的整数");
    }
    return {
      modelKey,
      retryOn: retryOn.length ? retryOn : [...DEFAULT_RETRY_ON],
      retryCount,
      singleApiKeyMaxConcurrency,
      description,
    };
  }

  function renderBindingPolicyFields() {
    const editor = llmState().editor || emptyEditorState();
    return `
      <div class="llm-form-grid">
        <label class="resource-field">
          <span class="resource-field-label">Retry On</span>
          <input id="llm-binding-retry-on" class="resource-search" type="text" value="${escv((editor.retryOn || DEFAULT_RETRY_ON).join(", "))}" placeholder="如 network, 429, 5xx">
        </label>
        <label class="resource-field">
          <span class="resource-field-label">重试次数</span>
          <input id="llm-binding-retry-count" class="resource-search" type="number" min="0" step="1" inputmode="numeric" value="${escv(String(editor.retryCount ?? 0))}" placeholder="0">
        </label>
        <label class="resource-field">
          <span class="resource-field-label">单 API key 最大并发数</span>
          <div class="llm-inline-field-actions">
            <input id="llm-binding-single-api-key-max-concurrency" class="resource-search" type="text" value="${escv(String(editor.singleApiKeyMaxConcurrency ?? ""))}" placeholder="留空表示不限制；多 key 可写 3,5,7">
            <button type="button" class="toolbar-btn ghost small" data-llm-action="test-max-concurrency">测试最大并发数</button>
          </div>
        </label>
      </div>
      <label class="resource-field">
        <span class="resource-field-label">说明</span>
        <textarea id="llm-binding-description" class="resource-editor model-textarea" rows="4" placeholder="用于备注当前模型的用途、成本或降级策略。">${escv(editor.description || "")}</textarea>
      </label>`;
  }

  function renderApiKeyJsonHint() {
    return "";
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
                  <select id="llm-provider-select" class="resource-search resource-select" data-resource-select-label="LLM provider">${state.templates.map((item) => `<option value="${escv(item.provider_id)}"${trim(item.provider_id) === trim(state.editor.providerId) ? " selected" : ""}>${escv(item.display_name || item.provider_id)}</option>`).join("")}</select>
                </label>
              </div>
              ${renderBindingPolicyFields()}
              <label class="resource-field">
                <span class="resource-field-label">JSON 配置</span>
                <textarea id="llm-json-editor" class="llm-json-editor" rows="18" spellcheck="false">${escv(state.editor.jsonText)}</textarea>
              </label>
              ${renderApiKeyJsonHint()}
              ${renderStatus()}
              <div class="llm-inline-actions">
                <button type="button" class="toolbar-btn ghost" data-llm-action="test-create">测试连接</button>
                <button type="button" class="toolbar-btn success" data-llm-action="save-create">添加模型</button>
              </div>
            </div>
          </div>
        </article>`;
    } else if (state.editor.mode === "memory") {
      const memory = state.editor.memory;
      const saveDisabled = !canSaveMemoryEditor() || state.saving;
      U.llmEditorShell.innerHTML = `
        <article class="model-detail-card model-config-shell llm-memory-shell">
          <div class="detail-modal-header model-config-header">
            <div class="detail-modal-title">
              <h2>记忆模型设置</h2>
              <p class="subtitle">编辑 Memory Runtime 当前使用的 Embedding 与 Rerank JSON，保存时会自动测试连通性并刷新运行时。</p>
            </div>
            <div class="detail-modal-actions">
              <button type="button" class="toolbar-btn ghost" data-llm-action="close">关闭</button>
              <button type="button" class="toolbar-btn success" data-llm-action="save-memory"${saveDisabled ? " disabled" : ""}>保存并测试</button>
            </div>
          </div>
          <div class="detail-modal-body model-config-body">
            ${memory?.error ? `<div class="llm-memory-banner">${escv(memory.error)}</div>` : ""}
            ${memory?.loading
              ? '<div class="empty-state compact">正在加载记忆模型配置...</div>'
              : `<div class="llm-memory-grid">${renderMemorySection("embedding", memory.embedding)}${renderMemorySection("rerank", memory.rerank)}</div>`}
          </div>
        </article>`;
    } else {
      const binding = currentBinding();
      U.llmEditorShell.innerHTML = `
        <article class="model-detail-card model-config-shell">
          <div class="detail-modal-header model-config-header">
            <div class="detail-modal-title">
              <h2>${escv(binding?.key || state.editor.bindingKey)}</h2>
              <p class="subtitle">可同时编辑当前模型的 JSON 配置与降级重试策略。</p>
            </div>
            <div class="detail-modal-actions">
              <button type="button" class="toolbar-btn ghost" data-llm-action="close">关闭</button>
            </div>
          </div>
          <div class="detail-modal-body model-config-body">
            <div class="llm-section">
              ${renderBindingPolicyFields()}
              <label class="resource-field">
                <span class="resource-field-label">JSON 配置</span>
                <textarea id="llm-json-editor" class="llm-json-editor" rows="18" spellcheck="false">${escv(state.editor.jsonText)}</textarea>
              </label>
              ${renderApiKeyJsonHint()}
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
    if (typeof enhanceResourceSelects === "function") enhanceResourceSelects();
    icons();
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
              ${renderBindingNoteAction()}
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
                  <select id="llm-provider-select" class="resource-search resource-select" data-resource-select-label="LLM provider">${state.templates.map((item) => `<option value="${escv(item.provider_id)}"${trim(item.provider_id) === trim(state.editor.providerId) ? " selected" : ""}>${escv(item.display_name || item.provider_id)}</option>`).join("")}</select>
                </label>
              </div>
              ${renderBindingPolicyFields()}
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
    } else if (state.editor.mode === "memory") {
      const memory = state.editor.memory;
      const saveDisabled = !canSaveMemoryEditor() || state.saving;
      U.llmEditorShell.innerHTML = `
        <article class="model-detail-card model-config-shell llm-memory-shell">
          <div class="detail-modal-header model-config-header">
            <div class="detail-modal-title">
              <h2>记忆模型设置</h2>
              <p class="subtitle">编辑 Memory Runtime 当前使用的 Embedding 与 Rerank JSON，保存时会自动测试连通性并刷新运行时。</p>
            </div>
            <div class="detail-modal-actions">
              <button type="button" class="toolbar-btn ghost" data-llm-action="close">关闭</button>
              <button type="button" class="toolbar-btn success" data-llm-action="save-memory"${saveDisabled ? " disabled" : ""}>保存并测试</button>
            </div>
          </div>
          <div class="detail-modal-body model-config-body">
            ${memory?.error ? `<div class="llm-memory-banner">${escv(memory.error)}</div>` : ""}
            ${memory?.loading
              ? '<div class="empty-state compact">正在加载记忆模型配置...</div>'
              : `<div class="llm-memory-grid">${renderMemorySection("embedding", memory.embedding)}${renderMemorySection("rerank", memory.rerank)}</div>`}
          </div>
        </article>`;
    } else {
      const binding = currentBinding();
      U.llmEditorShell.innerHTML = `
        <article class="model-detail-card model-config-shell">
          <div class="detail-modal-header model-config-header">
            <div class="detail-modal-title">
              <h2>${escv(binding?.key || state.editor.bindingKey)}</h2>
              <p class="subtitle">可同时编辑当前模型的 JSON 配置与降级重试策略。</p>
            </div>
            <div class="detail-modal-actions">
              ${renderBindingNoteAction()}
              <button type="button" class="toolbar-btn ghost" data-llm-action="close">关闭</button>
            </div>
          </div>
          <div class="detail-modal-body model-config-body">
            <div class="llm-section">
              ${renderBindingPolicyFields()}
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

    U.llmEditorShell.innerHTML = normalizeBindingNameText(U.llmEditorShell.innerHTML);
    setDrawerOpen(U.llmEditorBackdrop, U.llmEditorPanel, true);
    if (typeof enhanceResourceSelects === "function") enhanceResourceSelects();
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
      const description = trim(item.description);
      return `
        <article class="llm-binding-card model-available-item${trim(item.key) === trim(llmState().editor.bindingKey) ? " is-selected" : ""}" data-model-available-key="${escv(item.key)}" data-model-open="${escv(item.key)}"${canDrag ? ' draggable="true"' : ""}>
          <div class="llm-binding-card-head">
            <button type="button" class="model-available-main" data-model-open="${escv(item.key)}">
              <span class="resource-list-title">${escv(item.key)}</span>
              <span class="resource-list-subtitle">${escv(item.provider_model)}</span>
            </button>
            <span class="llm-capability-badge chat">Chat</span>
          </div>
          ${description ? `<div class="llm-binding-meta">${escv(description)}</div>` : ""}
          <div class="model-inline-meta">${item.enabled === false ? '<span class="policy-chip neutral">Disabled</span>' : '<span class="policy-chip risk-low">Enabled</span>'}${scopes.length ? scopes.map((scope) => `<span class="policy-chip neutral">${escv(SCOPE_LABELS[scope] || scope)}</span>`).join("") : '<span class="policy-chip neutral">未进入 Role Routes</span>'}</div>
        </article>`;
    }).join("");
  }

  function renderRoutes() {
    if (!U.modelRoleEditors) return;
    const editing = !!S.modelCatalog.roleEditing;
    U.modelRoleEditors.innerHTML = MODEL_SCOPES.map((scope) => {
      const chain = modelScopeChain(scope.key);
      const maxIterations = modelScopeIterations(scope.key);
      return `
        <section class="model-chain-card">
          <div class="panel-header">
            <div>
              <h3>${escv(SCOPE_LABELS[scope.key] || scope.key)}</h3>
              <p class="subtitle">${escv(chain[0] ? `默认：${chain[0]}` : "尚未配置")}</p>
            </div>
            <div class="model-chain-card-meta">
              <span class="policy-chip neutral">${chain.length} 个模型</span>
              <label class="model-role-iterations-field">
                <span class="model-role-iterations-label">最大轮数</span>
                <input
                  class="model-role-iterations-input"
                  type="number"
                  min="2"
                  step="1"
                  inputmode="numeric"
                  value="${escv(maxIterations)}"
                  ${editing ? "" : "disabled"}
                  data-model-role-iterations="${scope.key}"
                >
              </label>
            </div>
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

  function renderRoutes() {
    if (!U.modelRoleEditors) return;
    const editing = !!S.modelCatalog.roleEditing;
    U.modelRoleEditors.innerHTML = MODEL_SCOPES.map((scope) => {
      const chain = modelScopeChain(scope.key);
      const maxIterations = modelScopeIterations(scope.key);
      const maxConcurrency = modelScopeConcurrency(scope.key);
      return `
        <section class="model-chain-card">
          <div class="card-header">
            <h3>${escv(SCOPE_LABELS[scope.key] || scope.key)}</h3>
            <p class="subtitle">${escv(chain.length ? `已配置 ${chain.length} 个模型` : "尚未配置")}</p>
          </div>
          ${renderRoleLimitControl({ scopeKey: scope.key, kind: "iterations", label: "最大轮数", value: maxIterations, editing })}
          ${renderRoleLimitControl({ scopeKey: scope.key, kind: "concurrency", label: "最大并发数", value: maxConcurrency, editing })}
          <div class="role-chain-section">
            <div class="role-chain-title">ROLE CHAIN · ${chain.length} 个模型</div>
            <div class="model-chain-list" data-model-chain-list="${scope.key}">${chain.length ? chain.map((ref, index) => {
              const item = llmState().bindingMap[trim(ref)] || modelRefItem(ref);
              const key = trim(item?.key || ref);
              return `<article class="model-chain-slide${editing ? ' is-editing' : ''}"${editing ? ' draggable="true"' : ''} data-model-chain-ref="${escv(key)}" data-scope="${scope.key}">${editing ? '<button type="button" class="model-chain-handle" aria-label="拖拽排序"><span class="model-chain-grip" aria-hidden="true">&#9776;</span></button>' : ''}<button type="button" class="model-chain-main" data-model-open="${escv(key)}"><span class="resource-list-title">${escv(key)}</span><span class="resource-list-subtitle">${escv(item?.provider_model || ref)}</span><span class="model-inline-meta">${index === 0 ? '<span class="policy-chip risk-low">首选</span>' : ''}</span></button>${editing ? `<button type="button" class="toolbar-btn ghost small" data-model-chain-action="remove" data-scope="${scope.key}" data-index="${index}">移除</button>` : ''}</article>`;
            }).join("") : `<div class="empty-state compact">${editing ? '把左侧模型拖到这里，编排当前角色链。' : '点击“编辑模型链”后再调整角色链。'}</div>`}</div>
          </div>
        </section>`;
    }).join("");
  }

  function renderAll() {
    const state = llmState();
    refs();
    projectRoutes();
    if (U.modelRolesCancel) {
      U.modelRolesCancel.hidden = !S.modelCatalog.roleEditing;
      U.modelRolesCancel.disabled = state.loading || state.saving;
    }
    if (U.modelRolesSave) {
      U.modelRolesSave.disabled = state.loading || state.saving;
      U.modelRolesSave.textContent = S.modelCatalog.roleEditing ? (S.modelCatalog.rolesDirty ? "保存模型链" : "完成编辑") : "编辑模型链";
    }
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
      state.roleIterations = normalizeRoleIterations(bindingPayload?.roleIterations || bindingPayload?.role_iterations || DEFAULT_ROLE_ITERATIONS());
      state.roleConcurrency = normalizeRoleConcurrency(bindingPayload?.roleConcurrency || bindingPayload?.role_concurrency || DEFAULT_ROLE_CONCURRENCY());
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
    syncBindingInputs();
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
    syncBindingInputs();
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

    async function handleTestMaxConcurrency() {
    const state = llmState();
    syncBindingInputs();
    const jsonText = document.getElementById("llm-json-editor")?.value || state.editor.jsonText;
    const providerId = trim(document.getElementById("llm-provider-select")?.value || state.editor.providerId);
    state.editor.jsonText = jsonText;
    state.editor.providerId = providerId;
    const draft = parseDraftJson(jsonText, providerId);
    validateSingleApiKeyMaxConcurrencyInput(String(state.editor.singleApiKeyMaxConcurrency ?? ""), draft.api_key || "");
    showToast({
      title: "测试最大并发数中",
      text: "先测试连接，再测试每个 API key 的最大并发数...",
      kind: "info",
      persistent: true,
    });
    const ok = await probeDraft(draft);
    if (!ok) {
      throw new Error(draftFailureMessage(state.editor, "连接测试未通过，请先修正当前 JSON 配置。"));
    }
    const result = await ApiClient.probeLlmDraftMaxConcurrency(draft);
    const suggestedLimits = Array.isArray(result?.suggested_limits) ? result.suggested_limits : [];
    state.editor.singleApiKeyMaxConcurrency = formatSingleApiKeyMaxConcurrencyValue(suggestedLimits);
    renderAll();
    showToast({
      title: result?.success ? "最大并发数测试完成" : "最大并发数部分完成",
      text: result?.message || "已根据测试结果回填每个 API key 的最大并发数。",
      kind: result?.success ? "success" : "info",
    });
  }

    async function handleCreateSave() {
    const state = llmState();
    const bindingDraft = bindingDraftPayload({ requireModelKey: true });
    const modelKey = bindingDraft.modelKey;
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
          description: bindingDraft.description,
          retry_on: [...bindingDraft.retryOn],
          retry_count: bindingDraft.retryCount,
          single_api_key_max_concurrency: bindingDraft.singleApiKeyMaxConcurrency,
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

  async function handleDetailSave() {
    const state = llmState();
    const binding = currentBinding();
    if (!binding) throw new Error("当前模型绑定不存在");
    const bindingDraft = bindingDraftPayload();
    const jsonText = document.getElementById("llm-json-editor")?.value || state.editor.jsonText;
    state.editor.jsonText = jsonText;
    const draft = parseDraftJson(jsonText, state.editor.providerId);
    const configChanged = trim(jsonText) !== trim(state.editor.initialJsonText || "");
    const bindingPatch = {};
    if (JSON.stringify(bindingDraft.retryOn) !== JSON.stringify(binding.retry_on || [])) bindingPatch.retry_on = bindingDraft.retryOn;
    if (bindingDraft.retryCount !== Number.parseInt(String(binding.retry_count ?? 0), 10)) bindingPatch.retry_count = bindingDraft.retryCount;
    if (!singleApiKeyMaxConcurrencyEquals(bindingDraft.singleApiKeyMaxConcurrency, binding.single_api_key_max_concurrency ?? binding.singleApiKeyMaxConcurrency ?? null)) {
      bindingPatch.single_api_key_max_concurrency = bindingDraft.singleApiKeyMaxConcurrency;
    }
    if (bindingDraft.description !== trim(binding.description)) bindingPatch.description = bindingDraft.description;
    state.saving = true;
    renderAll();
    try {
      if (configChanged) {
        showToast({
          title: "Saving",
          text: "Validating current JSON config before applying changes...",
          kind: "success",
          persistent: true,
        });
        const ok = await probeDraft(draft);
        if (!ok) throw new Error("请先修正 JSON 配置并通过连接测试");
        await ApiClient.updateLlmConfig(state.editor.configId, draft);
      }
      if (Object.keys(bindingPatch).length) {
        showToast({
          title: "Saving",
          text: "Applying current retry and fallback policy...",
          kind: "success",
          persistent: true,
        });
        await ApiClient.updateLlmBinding(state.editor.bindingKey, bindingPatch);
      }
      if (!configChanged && !Object.keys(bindingPatch).length) {
        showToast({ title: "无需保存", text: "当前没有需要应用的修改。", kind: "info" });
        return;
      }
      showToast({ title: "修改成功", text: `${state.editor.bindingKey} 已更新`, kind: "success" });
      closeEditor();
      await loadAll();
    } finally {
      state.saving = false;
      renderAll();
    }
  }

  async function handleMemorySave() {
    const state = llmState();
    if (!canSaveMemoryEditor()) {
      throw new Error(
        state.editor.memory?.error
        || state.editor.memory?.embedding?.error
        || state.editor.memory?.rerank?.error
        || "记忆模型配置尚未就绪"
      );
    }
    const embeddingSection = syncMemorySectionText("embedding");
    const rerankSection = syncMemorySectionText("rerank");
    const embeddingDraft = parseMemoryDraftJson(
      embeddingSection?.jsonText || "",
      embeddingSection?.providerId || "",
      "embedding",
      embeddingSection?.label || "Embedding"
    );
    const rerankDraft = parseMemoryDraftJson(
      rerankSection?.jsonText || "",
      rerankSection?.providerId || "",
      "rerank",
      rerankSection?.label || "Rerank"
    );
    embeddingSection.providerId = trim(embeddingDraft.provider_id);
    embeddingSection.providerModel = providerModelFromDraft(embeddingDraft);
    embeddingSection.templateProviderId = defaultMemoryTemplateProviderId("embedding", embeddingSection.providerId);
    rerankSection.providerId = trim(rerankDraft.provider_id);
    rerankSection.providerModel = providerModelFromDraft(rerankDraft);
    rerankSection.templateProviderId = defaultMemoryTemplateProviderId("rerank", rerankSection.providerId);
    state.saving = true;
    renderAll();
    try {
      showToast({
        title: "测试中",
        text: "正在测试 Embedding 配置连接...",
        kind: "info",
        persistent: true,
      });
      const embeddingOk = await probeMemoryDraft("embedding", embeddingDraft);
      if (!embeddingOk) {
        throw new Error(`Embedding 测试未通过：${draftFailureMessage(embeddingSection)}`);
      }
      showToast({
        title: "测试中",
        text: "Embedding 已通过，正在测试 Rerank 配置连接...",
        kind: "info",
        persistent: true,
      });
      const rerankOk = await probeMemoryDraft("rerank", rerankDraft);
      if (!rerankOk) {
        throw new Error(`Rerank 测试未通过：${draftFailureMessage(rerankSection)}`);
      }
      showToast({
        title: "保存中",
        text: "连接测试通过，正在保存记忆模型配置...",
        kind: "info",
        persistent: true,
      });
      const embeddingConfig = trim(embeddingSection.configId)
        ? await ApiClient.updateLlmConfig(embeddingSection.configId, embeddingDraft)
        : await ApiClient.createLlmConfig(embeddingDraft);
      const rerankConfig = trim(rerankSection.configId)
        ? await ApiClient.updateLlmConfig(rerankSection.configId, rerankDraft)
        : await ApiClient.createLlmConfig(rerankDraft);
      embeddingSection.configId = trim(embeddingConfig?.config_id || embeddingSection.configId);
      rerankSection.configId = trim(rerankConfig?.config_id || rerankSection.configId);
      await ApiClient.updateLlmMemoryModels({
        embedding_config_id: embeddingSection.configId,
        rerank_config_id: rerankSection.configId,
      });
      showToast({
        title: "保存成功",
        text: "记忆模型配置已更新",
        kind: "success",
      });
      closeEditor();
      await loadAll();
    } finally {
      state.saving = false;
      renderAll();
    }
  }

  async function handleMemorySavePartial() {
    const state = llmState();
    if (!canSaveMemoryEditor()) {
      const modifiedKeys = modifiedMemorySectionKeys(state.editor.memory);
      throw new Error(
        (!modifiedKeys.length ? "当前没有已修改的记忆模型配置。" : "")
        || state.editor.memory?.error
        || state.editor.memory?.embedding?.error
        || state.editor.memory?.rerank?.error
        || "Memory model config is not ready."
      );
    }
    // Persist current textarea edits into state before any rerender happens.
    syncMemorySectionText("embedding");
    syncMemorySectionText("rerank");
    const sectionKeys = modifiedMemorySectionKeys(state.editor.memory);
    if (!sectionKeys.length) {
      throw new Error("当前没有已修改的记忆模型配置。");
    }
    state.saving = true;
    renderAll();
    try {
      const preparedBySection = new Map();
      sectionKeys.forEach((sectionKey) => {
        preparedBySection.set(sectionKey, prepareMemorySectionDraft(sectionKey));
      });
      const embeddingSection = state.editor.memory?.embedding;
      const preparedEmbedding = preparedBySection.get("embedding");
      const rebuildRequired = memorySaveRequiresRebuildConfirmation({
        currentEmbeddingProviderModel: initialMemorySectionProviderModel(embeddingSection),
        nextEmbeddingProviderModel: preparedEmbedding?.draft ? providerModelFromDraft(preparedEmbedding.draft) : "",
        embeddingModified: sectionKeys.includes("embedding"),
        rerankModified: sectionKeys.includes("rerank"),
      });
      if (rebuildRequired) {
        const { confirmed } = await requestInlineConfirm({
          title: "确认重建向量数据库？",
          text: "检测到 Embedding 模型已更改，需要立即全量重建数据库。确认后将保存新模型，并立即清空本地 Qdrant 后重新全量构建。",
          confirmLabel: "保存并重建",
          confirmKind: "danger",
        });
        if (!confirmed) {
          return;
        }
      }

      if (rebuildRequired) {
        const embeddingPrepared = preparedBySection.get("embedding");
        const rerankPrepared = sectionKeys.includes("rerank")
          ? (preparedBySection.get("rerank") || prepareMemorySectionDraft("rerank"))
          : null;
        const validationResults = [];
        for (const [sectionKey, prepared] of preparedBySection.entries()) {
          const section = prepared.section;
          const label = section?.label || capabilityLabel(sectionKey);
          if (!section || !prepared.draft) {
            validationResults.push({ sectionKey, label, ok: false, message: prepared.message || `${label} is invalid.` });
            continue;
          }
          showToast({
            title: "Testing",
            text: `Testing ${label} connection...`,
            kind: "info",
            persistent: true,
          });
          const ok = await probeMemoryDraft(sectionKey, prepared.draft);
          validationResults.push({ sectionKey, label, ok, message: ok ? "" : draftFailureMessage(section) });
        }
        const failedValidation = validationResults.filter((item) => !item.ok);
        if (failedValidation.length) {
          throw new Error(failedValidation.map((item) => `${item.label}: ${item.message}`).join("; "));
        }

        showToast({
          title: "Saving",
          text: "Saving memory embedding config...",
          kind: "info",
          persistent: true,
        });
        const atomicResult = await ApiClient.atomicSaveMemoryEmbedding({
          embedding: {
            config_id: trim(embeddingPrepared?.section?.configId || ""),
            draft: embeddingPrepared?.draft || null,
          },
          rerank: rerankPrepared?.draft
            ? {
                config_id: trim(rerankPrepared.section?.configId || ""),
                draft: rerankPrepared.draft,
              }
            : null,
        });
        showToast({
          title: "Save Complete",
          text: `Memory embedding updated. Dense rebuild indexed ${Number(atomicResult?.rebuild?.indexed || 0)} records.`,
          kind: "success",
        });
        closeEditor();
        await loadAll();
        return;
      }

      const results = [];
      const bindingPayload = {};
      for (const sectionKey of sectionKeys) {
        const prepared = preparedBySection.get(sectionKey) || prepareMemorySectionDraft(sectionKey);
        const section = prepared.section;
        const label = section?.label || capabilityLabel(sectionKey);
        if (!section || !prepared.draft) {
          results.push({ sectionKey, label, saved: false, message: prepared.message || `${label} is invalid.` });
          continue;
        }
        showToast({
          title: "Testing",
          text: `Testing ${label} connection...`,
          kind: "info",
          persistent: true,
        });
        const ok = await probeMemoryDraft(sectionKey, prepared.draft);
        if (!ok) {
          results.push({ sectionKey, label, saved: false, message: draftFailureMessage(section) });
          continue;
        }
        try {
          showToast({
            title: "Saving",
            text: `Saving ${label} config...`,
            kind: "info",
            persistent: true,
          });
          await persistMemorySectionDraft(section, prepared.draft);
          section.initialJsonText = section.jsonText;
          if (trim(section.configId)) {
            bindingPayload[`${sectionKey}_config_id`] = section.configId;
          }
          results.push({ sectionKey, label, saved: true, message: `${label} saved.` });
        } catch (error) {
          const message = error?.message || `${label} save failed.`;
          section.probe = { success: false, message };
          results.push({ sectionKey, label, saved: false, message });
        }
      }

      const savedItems = results.filter((item) => item.saved);
      const failedItems = results.filter((item) => !item.saved);
      let rebuildResult = null;

      if (Object.keys(bindingPayload).length) {
        const updatedBinding = await ApiClient.updateLlmMemoryModels(bindingPayload);
        if (updatedBinding) {
          const embeddingSection = state.editor.memory?.embedding;
          const rerankSection = state.editor.memory?.rerank;
          if (embeddingSection && Object.prototype.hasOwnProperty.call(bindingPayload, "embedding_config_id")) {
            embeddingSection.configId = trim(updatedBinding.embedding_config_id || embeddingSection.configId);
            embeddingSection.providerModel = trim(updatedBinding.embedding_provider_model || embeddingSection.providerModel);
          }
          if (rerankSection && Object.prototype.hasOwnProperty.call(bindingPayload, "rerank_config_id")) {
            rerankSection.configId = trim(updatedBinding.rerank_config_id || rerankSection.configId);
            rerankSection.providerModel = trim(updatedBinding.rerank_provider_model || rerankSection.providerModel);
          }
        }
      }

      if (rebuildRequired && savedItems.some((item) => item.sectionKey === "embedding")) {
        showToast({
          title: "\u91cd\u5efa\u4e2d",
          text: "\u6b63\u5728\u6e05\u7a7a\u672c\u5730 Qdrant...",
          kind: "info",
          persistent: true,
        });
        await ApiClient.resetMemoryDenseIndex({ reason: "embedding_model_changed" });
        showToast({
          title: "\u91cd\u5efa\u4e2d",
          text: "\u6b63\u5728\u5168\u91cf\u91cd\u5efa\u5411\u91cf\u6570\u636e\u5e93...",
          kind: "info",
          persistent: true,
        });
        rebuildResult = await ApiClient.rebuildMemoryDenseIndex({ reason: "embedding_model_changed" });
      }

      if (!savedItems.length) {
        throw new Error(failedItems.map((item) => `${item.label}: ${item.message}`).join("; ") || "No memory configs were saved.");
      }

      if (!failedItems.length) {
        showToast({
          title: "Save Complete",
          text: rebuildResult
            ? `Memory model settings updated. Dense rebuild indexed ${Number(rebuildResult.indexed || 0)} records.`
            : "Memory model settings updated.",
          kind: "success",
        });
        closeEditor();
        await loadAll();
        return;
      }

      showToast({
        title: "Partial Save",
        text: `Saved: ${savedItems.map((item) => item.label).join(", ")}. Pending: ${failedItems.map((item) => `${item.label} (${item.message})`).join("; ")}`,
        kind: "info",
      });
      await loadAll();
    } finally {
      state.saving = false;
      renderAll();
    }
  }

  async function handleDelete() {
    const binding = currentBinding();
    if (!binding) return;
    const { confirmed } = await requestInlineConfirm({
      title: "确认删除模型？",
      text: `删除模型 ${binding.key} 后，会删除该模型及其关联配置。`,
      confirmLabel: "删除模型",
      confirmKind: "danger",
    });
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
      const zone = event.target instanceof Element ? event.target.closest("[data-model-available-list]") : null;
      if (!zone) return;
      if (!didModelDragLeaveZone(zone, event)) return;
      clearModelDragDecorations();
      stopModelAutoScroll();
    });
    U.llmBindingsList.addEventListener("dragend", finishModelDrag);
  }

  async function bootstrap() {
    if (llmState().eventsBound) return;
    refs();
    bindList();
    U.llmMemorySettings?.addEventListener("click", () => void openMemoryModal().catch((error) => {
      llmState().error = error.message || "加载失败";
      showToast({ title: "打开失败", text: llmState().error, kind: "error" });
      renderAll();
    }));
    U.llmConfigCreate?.addEventListener("click", () => void openCreateModal());
    U.llmEditorBackdrop?.addEventListener("click", closeEditor);
    U.llmEditorShell?.addEventListener("change", (event) => {
      if (event.target?.id === "llm-provider-select") void handleProviderChange();
      const memoryTemplateSection = event.target?.dataset?.llmMemoryTemplate;
      if (memoryTemplateSection) void handleMemoryTemplateChange(memoryTemplateSection);
    });
    U.llmEditorShell?.addEventListener("input", (event) => {
      const sectionKey = memorySectionKeyFromTextareaId(event.target?.id);
      if (!sectionKey) return;
      handleMemorySectionInput(sectionKey);
    });
    U.llmEditorShell?.addEventListener("click", (event) => {
      const action = event.target.closest("[data-llm-action]")?.dataset.llmAction;
      if (!action) return;
      if (action === "close") { closeEditor(); return; }
      if (action === "test-create" || action === "test-detail") { void handleTest().catch((error) => { llmState().error = error.message || "测试失败"; showToast({ title: "测试失败", text: llmState().error, kind: "error" }); renderAll(); }); return; }
      if (action === "save-create") { void handleCreateSave().catch((error) => { llmState().error = error.message || "保存失败"; showToast({ title: "保存失败", text: llmState().error, kind: "error" }); renderAll(); }); return; }
      if (action === "save-detail") { void handleDetailSave().catch((error) => { llmState().error = error.message || "保存失败"; showToast({ title: "保存失败", text: llmState().error, kind: "error" }); renderAll(); }); return; }
      if (action === "save-memory") { void handleMemorySavePartial().catch((error) => { llmState().error = error.message || "保存失败"; showToast({ title: "保存失败", text: llmState().error, kind: "error" }); renderAll(); }); return; }
      if (action === "delete-detail") { void handleDelete().catch((error) => { llmState().error = error.message || "删除失败"; showToast({ title: "删除失败", text: llmState().error, kind: "error" }); renderAll(); }); }
    });
    llmState().eventsBound = true;
  }

  async function bootstrap() {
    if (llmState().eventsBound) return;
    refs();
    bindList();
    U.llmMemorySettings?.addEventListener("click", () => void openMemoryModal().catch((error) => {
      llmState().error = error.message || "加载失败";
      showToast({ title: "打开失败", text: llmState().error, kind: "error" });
      renderAll();
    }));
    U.llmConfigCreate?.addEventListener("click", () => void openCreateModal());
    U.llmEditorBackdrop?.addEventListener("click", closeEditor);
    U.llmEditorShell?.addEventListener("change", (event) => {
      if (event.target?.id === "llm-provider-select") void handleProviderChange();
      const memoryTemplateSection = event.target?.dataset?.llmMemoryTemplate;
      if (memoryTemplateSection) void handleMemoryTemplateChange(memoryTemplateSection);
    });
    U.llmEditorShell?.addEventListener("input", (event) => {
      const sectionKey = memorySectionKeyFromTextareaId(event.target?.id);
      if (!sectionKey) return;
      handleMemorySectionInput(sectionKey);
    });
    U.llmEditorShell?.addEventListener("click", (event) => {
      const action = event.target.closest("[data-llm-action]")?.dataset.llmAction;
      if (!action) return;
      if (action === "close") { closeEditor(); return; }
      if (action === "test-create" || action === "test-detail") { void handleTest().catch((error) => { llmState().error = error.message || "测试失败"; showToast({ title: "测试失败", text: llmState().error, kind: "error" }); renderAll(); }); return; }
      if (action === "test-max-concurrency") { void handleTestMaxConcurrency().catch((error) => { llmState().error = error.message || "测试最大并发数失败"; showToast({ title: "测试最大并发数失败", text: llmState().error, kind: "error" }); renderAll(); }); return; }
      if (action === "save-create") { void handleCreateSave().catch((error) => { llmState().error = error.message || "保存失败"; showToast({ title: "保存失败", text: llmState().error, kind: "error" }); renderAll(); }); return; }
      if (action === "save-detail") { void handleDetailSave().catch((error) => { llmState().error = error.message || "保存失败"; showToast({ title: "保存失败", text: llmState().error, kind: "error" }); renderAll(); }); return; }
      if (action === "save-memory") { void handleMemorySavePartial().catch((error) => { llmState().error = error.message || "保存失败"; showToast({ title: "保存失败", text: llmState().error, kind: "error" }); renderAll(); }); return; }
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
  window.__llmTestHooks = {
    expandSingleApiKeyMaxConcurrencyForEditor,
    parseSingleApiKeyMaxConcurrencyInput,
    validateSingleApiKeyMaxConcurrencyInput,
    bindingNotesTitle,
    bindingNameLabel,
    bindingNameRequiredMessage,
    normalizeBindingNameText,
    memorySaveRequiresRebuildConfirmation,
    modifiedMemorySectionKeys,
  };
  window.handleModelRoleEditorAction = async function handleModelRoleEditorAction() {
    if (!S.modelCatalog.roleEditing) {
      startModelRoleEditing();
      renderAll();
      return;
    }
    try {
      syncRoleIterationDraftsFromInputs({ requireValid: true });
    } catch (error) {
      llmState().error = error.message || "保存失败";
      showToast({ title: "保存失败", text: llmState().error, kind: "error" });
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
      const routes = await ApiClient.updateLlmRoutes(Object.fromEntries(
        MODEL_SCOPES.map((item) => [
          item.key,
          {
            modelKeys: normalizeModelRoleChain(S.modelCatalog.roleDrafts[item.key] || []),
            maxIterations: modelScopeIterations(item.key, "draft"),
            maxConcurrency: modelScopeConcurrency(item.key, "draft"),
          },
        ])
      ));
      llmState().routes = normalizeAllModelRoles(routes?.routes || EMPTY_MODEL_ROLES());
      llmState().roleIterations = normalizeRoleIterations(routes?.roleIterations || DEFAULT_ROLE_ITERATIONS());
      llmState().roleConcurrency = normalizeRoleConcurrency(routes?.roleConcurrency || routes?.role_concurrency || DEFAULT_ROLE_CONCURRENCY());
      S.modelCatalog.roleEditing = false;
      S.modelCatalog.rolesDirty = false;
      showToast({ title: "保存成功", text: "模型链已更新", kind: "success" });
    } finally {
      llmState().saving = false;
      renderAll();
    }
  };

  refs();
  let llmBootstrapped = false;
  function maybeBootstrap() {
    if (llmBootstrapped) return;
    if (window.G3kuBoot && typeof window.G3kuBoot.isUnlocked === "function" && !window.G3kuBoot.isUnlocked()) return;
    llmBootstrapped = true;
    void bootstrap();
  }

  document.addEventListener("DOMContentLoaded", maybeBootstrap);
  window.addEventListener("g3ku:boot-unlocked", maybeBootstrap);
})();
