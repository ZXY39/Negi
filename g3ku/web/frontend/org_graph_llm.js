(() => {
  const DEFAULT_RETRY_ON = ["network", "429"];
  const SCOPE_LABELS = { ceo: "主Agent", execution: "执行Agent", inspection: "检验Agent", memory: "记忆Agent" };


  function emptyEditorState() {
    return {
      open: false,
      mode: "",
      bindingKey: "",
      configId: "",
      modelKey: "",
      providerId: "",
      baseUrl: "",
      apiKey: "",
      defaultModel: "",
      jsonText: "",
      initialJsonText: "",
      retryOn: [...DEFAULT_RETRY_ON],
      retryCount: 0,
      singleApiKeyMaxConcurrency: "",
      contextWindowTokens: "",
      initialContextWindowTokens: "",
      imageMultimodalEnabled: false,
      initialImageMultimodalEnabled: false,
      validation: null,
      probe: null,
      modelList: null,
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

  function missingRequiredModelRoleLabels() {
    return ["ceo", "execution", "inspection"]
      .filter((scope) => !normalizeModelRoleChain(S.modelCatalog.roleDrafts?.[scope] || []).length)
      .map((scope) => SCOPE_LABELS[scope] || scope);
  }

  function requiredModelRoleValidationMessage() {
    const labels = missingRequiredModelRoleLabels();
    if (!labels.length) return "";
    return `请先为以下角色配置模型链：${labels.join("、")}。全部拖入后才可保存。`;
  }

  function bindingNameLabel() {
    return "模型ID";
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
    if (!state.bindings.length) return hint("还没有保存的模型。点击“添加模型”开始添加。", false);
    if (S.modelCatalog.roleEditing && S.modelCatalog.rolesDirty) return hint("模型链已修改，点击“保存模型链”提交。", false);
    if (S.modelCatalog.roleEditing) return hint("拖拽左侧模型到右侧角色列，完成后点击“保存模型链”。", false);
    return hint("点击左侧模型可以查看对应 JSON 配置；点击“添加模型”可添加新模型。", false);
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

  async function openCreateModal() {
    const state = llmState();
    const preferred = state.templates.find((item) => trim(item.provider_id) === "openai") || state.templates[0];
    const provider = preferred?.provider_id || "";
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
      baseUrl: String(draft.base_url || ""),
      apiKey: String(draft.api_key || ""),
      defaultModel: String(draft.default_model || ""),
      jsonText: JSON.stringify(draft, null, 2),
      initialJsonText: JSON.stringify(draft, null, 2),
      retryOn: [...DEFAULT_RETRY_ON],
      retryCount: 0,
      contextWindowTokens: "",
      initialContextWindowTokens: "",
      imageMultimodalEnabled: false,
      initialImageMultimodalEnabled: false,
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
      baseUrl: String(draft.base_url || ""),
      apiKey: String(draft.api_key || ""),
      defaultModel: String(draft.default_model || ""),
      jsonText,
      initialJsonText: jsonText,
      retryOn: Array.isArray(binding.retry_on) ? binding.retry_on.map((item) => trim(item)).filter(Boolean) : [...DEFAULT_RETRY_ON],
      retryCount: Number.isInteger(Number(binding.retry_count)) ? Math.max(0, Number(binding.retry_count)) : 0,
      singleApiKeyMaxConcurrency: expandSingleApiKeyMaxConcurrencyForEditor(
        binding.single_api_key_max_concurrency ?? binding.singleApiKeyMaxConcurrency ?? "",
        draft.api_key || ""
      ),
      contextWindowTokens: trim(binding.context_window_tokens),
      initialContextWindowTokens: trim(binding.context_window_tokens),
      imageMultimodalEnabled: Boolean(binding.image_multimodal_enabled),
      initialImageMultimodalEnabled: Boolean(binding.image_multimodal_enabled),
    };
    renderAll();
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

  function parseBindingRetryOn(raw) {
    return String(raw || "")
      .split(/[\n,]/)
      .map((item) => trim(item))
      .filter(Boolean);
  }

  function syncBindingInputs() {
    const editor = llmState().editor;
    if (!editor) return editor;
    const modelKeyInput = document.getElementById("llm-model-key-input");
    const providerSelect = document.getElementById("llm-provider-select");
    const baseUrlInput = document.getElementById("llm-binding-base-url");
    const apiKeyInput = document.getElementById("llm-binding-api-key");
    const defaultModelInput = document.getElementById("llm-binding-default-model");
    const jsonEditor = document.getElementById("llm-json-editor");
    const retryOnInput = document.getElementById("llm-binding-retry-on");
    const retryCountInput = document.getElementById("llm-binding-retry-count");
    const singleApiKeyMaxConcurrencyInput = document.getElementById("llm-binding-single-api-key-max-concurrency");
    const contextWindowTokensInput = document.getElementById("llm-binding-context-window-tokens");
    const imageMultimodalEnabledInput = document.getElementById("llm-binding-image-multimodal-enabled");
    if (modelKeyInput) editor.modelKey = trim(modelKeyInput.value || editor.modelKey);
    if (providerSelect) editor.providerId = trim(providerSelect.value || editor.providerId);
    if (baseUrlInput) editor.baseUrl = String(baseUrlInput.value ?? editor.baseUrl ?? "");
    if (apiKeyInput) editor.apiKey = String(apiKeyInput.value ?? editor.apiKey ?? "");
    if (defaultModelInput) editor.defaultModel = String(defaultModelInput.value ?? editor.defaultModel ?? "");
    if (jsonEditor) editor.jsonText = String(jsonEditor.value || editor.jsonText || "");
    if (retryOnInput) editor.retryOn = parseBindingRetryOn(retryOnInput.value || "");
    if (retryCountInput) {
      const parsed = Number.parseInt(String(retryCountInput.value || "").trim(), 10);
      editor.retryCount = Number.isInteger(parsed) && parsed >= 0 ? parsed : 0;
    }
    if (singleApiKeyMaxConcurrencyInput) {
      editor.singleApiKeyMaxConcurrency = trim(singleApiKeyMaxConcurrencyInput.value || "");
    }
    if (contextWindowTokensInput) editor.contextWindowTokens = trim(contextWindowTokensInput.value || "");
    if (imageMultimodalEnabledInput) editor.imageMultimodalEnabled = Boolean(imageMultimodalEnabledInput.checked);
    return editor;
  }

  function parseContextWindowTokensValue(raw) {
    const parsed = Number.parseInt(String(raw ?? "").trim(), 10);
    return Number.isInteger(parsed) ? parsed : null;
  }

  function validContextWindowTokensValue(raw) {
    const parsed = parseContextWindowTokensValue(raw);
    return Number.isInteger(parsed) && parsed > 25000 ? parsed : null;
  }

  function contextWindowTokensFromDraftText(raw, providerId) {
    try {
      const draft = parseDraftJson(raw, providerId);
      return validContextWindowTokensValue(draft?.parameters?.context_window_tokens);
    } catch (_error) {
      return null;
    }
  }

  function setBindingJsonEditorValue(nextText) {
    const editor = llmState().editor;
    if (!editor) return false;
    const normalized = String(nextText || "");
    editor.jsonText = normalized;
    const jsonEditor = document.getElementById("llm-json-editor");
    if (jsonEditor && String(jsonEditor.value || "") !== normalized) jsonEditor.value = normalized;
    return true;
  }

  function syncContextWindowTokensIntoJsonEditor(tokens) {
    const editor = llmState().editor;
    if (!editor) return false;
    const resolved = validContextWindowTokensValue(tokens);
    if (!Number.isInteger(resolved)) return false;
    const providerId = trim(document.getElementById("llm-provider-select")?.value || editor.providerId);
    const currentText = String(document.getElementById("llm-json-editor")?.value || editor.jsonText || "");
    let draft;
    try {
      draft = parseDraftJson(currentText, providerId);
    } catch (_error) {
      return false;
    }
    draft.parameters = draft.parameters && typeof draft.parameters === "object" && !Array.isArray(draft.parameters) ? draft.parameters : {};
    if (validContextWindowTokensValue(draft.parameters.context_window_tokens) === resolved) return false;
    draft.parameters.context_window_tokens = resolved;
    setBindingJsonEditorValue(JSON.stringify(draft, null, 2));
    return true;
  }

  function syncContextWindowTokensInputValue(tokens) {
    const editor = llmState().editor;
    if (!editor) return false;
    const normalized = Number.isInteger(validContextWindowTokensValue(tokens)) ? String(validContextWindowTokensValue(tokens)) : "";
    editor.contextWindowTokens = normalized;
    const input = document.getElementById("llm-binding-context-window-tokens");
    if (input && String(input.value || "") !== normalized) input.value = normalized;
    return true;
  }

  function syncBaseUrlInputValue(value) {
    const editor = llmState().editor;
    if (!editor) return false;
    const normalized = String(value ?? "");
    editor.baseUrl = normalized;
    const input = document.getElementById("llm-binding-base-url");
    if (input && String(input.value || "") !== normalized) input.value = normalized;
    return true;
  }

  function syncApiKeyInputValue(value) {
    const editor = llmState().editor;
    if (!editor) return false;
    const normalized = String(value ?? "");
    editor.apiKey = normalized;
    const input = document.getElementById("llm-binding-api-key");
    if (input && String(input.value || "") !== normalized) input.value = normalized;
    return true;
  }

  function syncDefaultModelInputValue(value) {
    const editor = llmState().editor;
    if (!editor) return false;
    const normalized = String(value ?? "");
    editor.defaultModel = normalized;
    const input = document.getElementById("llm-binding-default-model");
    if (input && String(input.value || "") !== normalized) input.value = normalized;
    return true;
  }

  function syncDraftFieldIntoJsonEditor(fieldName, value) {
    const editor = llmState().editor;
    if (!editor) return false;
    const providerId = trim(document.getElementById("llm-provider-select")?.value || editor.providerId);
    const currentText = String(document.getElementById("llm-json-editor")?.value || editor.jsonText || "");
    let draft;
    try {
      draft = parseDraftJson(currentText, providerId);
    } catch (_error) {
      return false;
    }
    if (String(draft[fieldName] == null ? "" : draft[fieldName]) === String(value == null ? "" : value)) return false;
    draft[fieldName] = String(value == null ? "" : value);
    setBindingJsonEditorValue(JSON.stringify(draft, null, 2));
    return true;
  }

  function handleBindingBaseUrlInput() {
    const editor = llmState().editor;
    if (!editor) return;
    const input = document.getElementById("llm-binding-base-url");
    editor.baseUrl = String(input?.value ?? editor.baseUrl ?? "");
    syncDraftFieldIntoJsonEditor("base_url", editor.baseUrl);
  }

  function handleBindingApiKeyInput() {
    const editor = llmState().editor;
    if (!editor) return;
    const input = document.getElementById("llm-binding-api-key");
    editor.apiKey = String(input?.value ?? editor.apiKey ?? "");
    syncDraftFieldIntoJsonEditor("api_key", editor.apiKey);
  }

  function handleBindingDefaultModelInput() {
    const editor = llmState().editor;
    if (!editor) return;
    const input = document.getElementById("llm-binding-default-model");
    editor.defaultModel = String(input?.value ?? editor.defaultModel ?? "");
    syncDraftFieldIntoJsonEditor("default_model", editor.defaultModel);
  }

  function handleBindingContextWindowInput() {
    const editor = llmState().editor;
    if (!editor) return;
    const input = document.getElementById("llm-binding-context-window-tokens");
    editor.contextWindowTokens = trim(input?.value || editor.contextWindowTokens);
    const resolved = validContextWindowTokensValue(editor.contextWindowTokens);
    if (Number.isInteger(resolved)) syncContextWindowTokensIntoJsonEditor(resolved);
  }

  function handleBindingJsonEditorInput() {
    const editor = llmState().editor;
    if (!editor) return;
    const jsonEditor = document.getElementById("llm-json-editor");
    editor.jsonText = String(jsonEditor?.value || editor.jsonText || "");
    let draft = null;
    try {
      draft = parseDraftJson(editor.jsonText, editor.providerId || "");
    } catch (_error) {
      draft = null;
    }
    if (draft) {
      syncBaseUrlInputValue(String(draft.base_url ?? ""));
      syncApiKeyInputValue(String(draft.api_key ?? ""));
      syncDefaultModelInputValue(String(draft.default_model ?? ""));
      const resolved = validContextWindowTokensValue(draft?.parameters?.context_window_tokens);
      if (Number.isInteger(resolved)) syncContextWindowTokensInputValue(resolved);
    }
  }

  function reconcileBindingContextWindowTokens() {
    const editor = syncBindingInputs();
    if (!editor) return editor;
    const providerId = trim(editor.providerId || "");
    const jsonText = String(editor.jsonText || "");
    const inputTokens = validContextWindowTokensValue(editor.contextWindowTokens);
    const jsonTokens = contextWindowTokensFromDraftText(jsonText, providerId);
    const initialJsonTokens = contextWindowTokensFromDraftText(editor.initialJsonText || "", providerId);
    const initialInputTokens = validContextWindowTokensValue(
      editor.initialContextWindowTokens != null && String(editor.initialContextWindowTokens).trim() !== ""
        ? editor.initialContextWindowTokens
        : initialJsonTokens
    );
    let resolved = null;
    if (jsonTokens !== null && inputTokens !== null && jsonTokens === inputTokens) {
      resolved = jsonTokens;
    } else if (jsonTokens !== null && inputTokens === null) {
      resolved = jsonTokens;
    } else if (jsonTokens === null && inputTokens !== null) {
      resolved = inputTokens;
    } else if (jsonTokens !== null && inputTokens !== null) {
      const jsonChanged = jsonTokens !== initialJsonTokens;
      const inputChanged = inputTokens !== initialInputTokens;
      if (jsonChanged && !inputChanged) resolved = jsonTokens;
      else if (inputChanged && !jsonChanged) resolved = inputTokens;
      else resolved = inputTokens;
    }
    if (Number.isInteger(resolved)) {
      syncContextWindowTokensInputValue(resolved);
      syncContextWindowTokensIntoJsonEditor(resolved);
    }
    return editor;
  }

  function bindingDraftPayload({ requireModelKey = false } = {}) {
    const editor = reconcileBindingContextWindowTokens();
    const modelKey = trim(editor?.modelKey);
    const retryOn = Array.isArray(editor?.retryOn) ? editor.retryOn.map((item) => trim(item)).filter(Boolean) : [];
    const retryCount = Number.parseInt(String(editor?.retryCount ?? 0), 10);
    const draft = parseDraftJson(editor?.jsonText || "", editor?.providerId || "");
    const singleApiKeyMaxConcurrency = validateSingleApiKeyMaxConcurrencyInput(
      String(editor?.singleApiKeyMaxConcurrency ?? ""),
      draft.api_key || ""
    );
    const contextWindowTokens = Number.parseInt(String(editor?.contextWindowTokens ?? "").trim(), 10);
    if (requireModelKey && !modelKey) throw new Error(bindingNameRequiredMessage());
    if (!Number.isInteger(retryCount) || retryCount < 0) {
      throw new Error("重试次数必须是不小于 0 的整数");
    }
    if (!Number.isInteger(contextWindowTokens) || contextWindowTokens <= 25000) {
      throw new Error("最大上下文TOKEN必须是大于 25000 的整数");
    }
    if (singleApiKeyMaxConcurrency !== null && (!Number.isInteger(singleApiKeyMaxConcurrency) || singleApiKeyMaxConcurrency < 1)) {
      throw new Error("single_api_key_max_concurrency must be >= 1");
    }
    draft.parameters = draft.parameters && typeof draft.parameters === "object" && !Array.isArray(draft.parameters) ? draft.parameters : {};
    draft.parameters.context_window_tokens = contextWindowTokens;
    return {
      modelKey,
      retryOn: retryOn.length ? retryOn : [...DEFAULT_RETRY_ON],
      retryCount,
      singleApiKeyMaxConcurrency,
      contextWindowTokens,
      imageMultimodalEnabled: Boolean(editor?.imageMultimodalEnabled),
      draft,
    };
  }

  function renderImageMultimodalField({ layout = "default" } = {}) {
    const editor = llmState().editor || emptyEditorState();
    const fieldClasses = ["resource-field", "llm-image-checkbox-field"];
    const controlClasses = ["llm-image-checkbox-control"];
    let spacer = "";
    if (layout === "header") {
      fieldClasses.push("llm-image-checkbox-field--header");
      spacer = '<span class="resource-field-label llm-image-checkbox-spacer" aria-hidden="true">是否为图像多模态</span>';
    } else if (layout === "inline") {
      fieldClasses.push("llm-image-checkbox-field--inline");
      controlClasses.push("llm-image-checkbox-control--inline");
    }
    return `
      <label class="${fieldClasses.join(" ")}" for="llm-binding-image-multimodal-enabled">
        ${spacer}
        <span class="${controlClasses.join(" ")}">
          <span>是否为图像多模态</span>
          <span class="llm-image-checkbox-input">
            <input id="llm-binding-image-multimodal-enabled" type="checkbox"${editor.imageMultimodalEnabled ? " checked" : ""}>
            <span class="llm-image-checkbox-indicator" aria-hidden="true"></span>
          </span>
        </span>
      </label>`;
  }

  function renderBindingPolicyFields() {
    const editor = llmState().editor || emptyEditorState();
    if (editor.mode === "create") {
      return `
        <div class="llm-form-grid">
          <label class="resource-field">
            <span class="resource-field-label">自动重试错误关键词</span>
            <input id="llm-binding-retry-on" class="resource-search" type="text" value="${escv((editor.retryOn || DEFAULT_RETRY_ON).join(", "))}" placeholder="如 network, 429, 502（可自定义关键词，逗号分隔）">
          </label>
          <label class="resource-field">
            <span class="resource-field-label">重试次数</span>
            <input id="llm-binding-retry-count" class="resource-search" type="number" min="0" step="1" inputmode="numeric" value="${escv(String(editor.retryCount ?? 0))}" placeholder="0">
          </label>
          ${renderConcurrencyField(editor)}
        </div>
        ${renderContextWindowField(editor)}`;
    }
    return `
      <div class="llm-form-grid llm-form-grid--binding-detail-policy">
        <label class="resource-field">
          <span class="resource-field-label">自动重试错误关键词</span>
          <input id="llm-binding-retry-on" class="resource-search" type="text" value="${escv((editor.retryOn || DEFAULT_RETRY_ON).join(", "))}" placeholder="如 network, 429, 502（可自定义关键词，逗号分隔）">
        </label>
        <label class="resource-field">
          <span class="resource-field-label">重试次数</span>
          <input id="llm-binding-retry-count" class="resource-search" type="number" min="0" step="1" inputmode="numeric" value="${escv(String(editor.retryCount ?? 0))}" placeholder="0">
        </label>
        ${renderConcurrencyField(editor)}
      </div>`;
  }

  function renderFetchModelListField() {
    return `
        <div class="resource-field llm-model-list-field">
          <span class="resource-field-label" aria-hidden="true">模型列表</span>
          <button type="button" class="toolbar-btn ghost llm-fetch-btn" data-llm-action="fetch-model-list">获取模型列表</button>
        </div>`;
  }

  function renderContextWindowField(editor) {
    return `
        <label class="resource-field">
          <span class="resource-field-label">最大上下文TOKEN *</span>
          <input id="llm-binding-context-window-tokens" class="resource-search" type="number" min="25001" step="1" inputmode="numeric" value="${escv(String(editor.contextWindowTokens || ""))}" placeholder="必须大于 25000">
        </label>`;
  }

  function renderConnectionFields() {
    const state = llmState();
    const isCreate = state.editor.mode === "create";
    const modelIdField = isCreate ? "" : `
        <label class="resource-field">
          <span class="resource-field-label">模型ID</span>
          <input id="llm-binding-default-model" class="resource-search" type="text" value="${escv(state.editor.defaultModel)}" placeholder="模型ID，例如：gpt-4o">
        </label>`;
    return `
      <div class="llm-form-grid llm-form-grid--binding-header">
        ${modelIdField}
        <label class="resource-field">
          <span class="resource-field-label">请求地址 *</span>
          <input id="llm-binding-base-url" class="resource-search" type="text" value="${escv(state.editor.baseUrl)}" placeholder="例如：https://api.example.com/v1">
        </label>
        <label class="resource-field">
          <span class="resource-field-label">Apikey *</span>
          <input id="llm-binding-api-key" class="resource-search" type="text" value="${escv(state.editor.apiKey)}" placeholder="支持用逗号分隔填写多个 key">
        </label>
        ${isCreate ? renderFetchModelListField() : ""}
      </div>
      ${isCreate ? "" : `
      <div class="llm-form-grid llm-form-grid--binding-header">
        ${renderFetchModelListField()}
        ${renderImageMultimodalField()}
        ${renderContextWindowField(state.editor)}
      </div>`}
      <div id="llm-model-list-panel" class="llm-model-list-panel" hidden></div>`;
  }

  function renderConcurrencyField(editor) {
    return `
          <label class="resource-field">
            <span class="resource-field-label">单 API key 最大并发数</span>
            <div class="llm-concurrency-input-row">
              <input id="llm-binding-single-api-key-max-concurrency" class="resource-search" type="text" value="${escv(String(editor.singleApiKeyMaxConcurrency ?? ""))}" placeholder="留空表示不限制；多 key 可写 3,5,7">
              <button type="button" class="icon-btn llm-kebab-btn" data-llm-action="toggle-concurrency-test" title="并发测试" aria-label="显示并发测试"><i data-lucide="more-horizontal"></i></button>
            </div>
            <div class="llm-concurrency-test-row" hidden>
              <button type="button" class="toolbar-btn ghost small" data-llm-action="test-max-concurrency">测试最大并发数</button>
            </div>
          </label>`;
  }

  function renderJsonDetails() {
    const state = llmState();
    return `
      <details class="llm-json-details">
        <summary class="llm-json-summary">JSON 配置</summary>
        <textarea id="llm-json-editor" class="llm-json-editor" rows="18" spellcheck="false">${escv(state.editor.jsonText)}</textarea>
      </details>`;
  }

  function renderModelListPanel() {
    const panel = document.getElementById("llm-model-list-panel");
    if (!panel) return;
    const modelList = llmState().editor?.modelList;
    if (!modelList) {
      panel.hidden = true;
      panel.innerHTML = "";
      return;
    }
    panel.hidden = false;
    if (modelList.loading) {
      panel.innerHTML = '<p class="llm-muted">正在获取模型列表...</p>';
      return;
    }
    if (modelList.error) {
      panel.innerHTML = `
        <div class="llm-model-list-toolbar">
          <span class="llm-muted llm-model-list-error">${escv(modelList.error)}</span>
          <button type="button" class="toolbar-btn ghost small" data-llm-action="model-list-close">收起</button>
        </div>`;
      return;
    }
    panel.innerHTML = `
      <div class="llm-model-list-toolbar">
        <input id="llm-model-list-filter" class="resource-search" type="search" placeholder="筛选模型..." value="${escv(modelList.filter || "")}">
        <span id="llm-model-list-count" class="llm-muted"></span>
        <button type="button" class="toolbar-btn ghost small" data-llm-action="model-list-close">收起</button>
      </div>
      <div class="llm-model-list-items"></div>`;
    renderModelListItems();
  }

  function renderModelListItems() {
    const panel = document.getElementById("llm-model-list-panel");
    if (!panel) return;
    const modelList = llmState().editor?.modelList;
    if (!modelList || modelList.loading || modelList.error) return;
    const itemsHost = panel.querySelector(".llm-model-list-items");
    if (!itemsHost) return;
    const items = Array.isArray(modelList.items) ? modelList.items : [];
    const filterText = trim(modelList.filter || "").toLowerCase();
    const filtered = items.filter((item) => !filterText || String(item).toLowerCase().includes(filterText));
    const countHost = panel.querySelector("#llm-model-list-count");
    if (countHost) countHost.textContent = `${filtered.length} / ${items.length} 个模型`;
    itemsHost.innerHTML = filtered.length
      ? filtered.map((item) => `<button type="button" class="llm-model-list-item" data-llm-model-item="${escv(item)}" title="点击将该模型填入配置">${escv(item)}</button>`).join("")
      : '<p class="llm-muted">没有匹配的模型。</p>';
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
              <h2>添加模型</h2>
              <p class="subtitle">选择协议，填写请求地址与 Apikey 后可获取模型列表、测试连接。</p>
            </div>
            <div class="detail-modal-actions">
              ${renderBindingNoteAction()}
              <button type="button" class="toolbar-btn ghost" data-llm-action="close">关闭</button>
            </div>
          </div>
          <div class="detail-modal-body model-config-body">
            <div class="llm-section">
              <div class="llm-form-grid llm-form-grid--binding-header">
                <label class="resource-field">
                  <span class="resource-field-label">模型ID *</span>
                  <input id="llm-model-key-input" class="resource-search" type="text" value="${escv(state.editor.modelKey)}" placeholder="例如：ceo_primary">
                </label>
                <label class="resource-field">
                  <span class="resource-field-label">协议</span>
                  <select id="llm-provider-select" class="resource-search resource-select" data-resource-select-label="LLM provider">${state.templates.map((item) => `<option value="${escv(item.provider_id)}"${trim(item.provider_id) === trim(state.editor.providerId) ? " selected" : ""}>${escv(item.display_name || item.provider_id)}</option>`).join("")}</select>
                </label>
                ${renderImageMultimodalField({ layout: "header" })}
              </div>
              ${renderConnectionFields()}
              ${renderBindingPolicyFields()}
              ${renderJsonDetails()}
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
              <p class="subtitle">可同时编辑当前模型的 JSON 配置与降级重试策略。</p>
            </div>
            <div class="detail-modal-actions">
              ${renderBindingNoteAction()}
              <button type="button" class="toolbar-btn ghost" data-llm-action="close">关闭</button>
            </div>
          </div>
          <div class="detail-modal-body model-config-body">
            <div class="llm-section">
              ${renderConnectionFields()}
              ${renderBindingPolicyFields()}
              ${renderJsonDetails()}
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
    const jsonDetails = document.getElementById("llm-json-details");
    if (jsonDetails) {
      const validationFailed = !!state.editor.validation && state.editor.validation.valid === false;
      const probeFailed = !!state.editor.probe && !state.editor.probe.success;
      if (validationFailed || probeFailed) jsonDetails.open = true;
    }
    renderModelListPanel();
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
          <div class="model-inline-meta">${item.enabled === false ? '<span class="policy-chip neutral">Disabled</span>' : '<span class="policy-chip risk-low">Enabled</span>'}${scopes.length ? scopes.map((scope) => `<span class="policy-chip neutral">${escv(SCOPE_LABELS[scope] || scope)}</span>`).join("") : '<span class="policy-chip neutral">未进入角色链</span>'}</div>
        </article>`;
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
      state.routes = normalizeAllModelRoles(bindingPayload?.routes || EMPTY_MODEL_ROUTES());
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
    reconcileBindingContextWindowTokens();
    const select = document.getElementById("llm-provider-select");
    const providerId = trim(select?.value);
    if (!providerId) return;
    state.editor.providerId = providerId;
    await ensureTemplate(providerId);
    const currentText = String(document.getElementById("llm-json-editor")?.value || state.editor.jsonText || "");
    let draft = null;
    try {
      draft = parseDraftJson(currentText, providerId);
    } catch (_error) {
      draft = null;
    }
    if (!draft) {
      draft = buildDraftFromTemplate(providerId);
      if (trim(state.editor.baseUrl)) draft.base_url = trim(state.editor.baseUrl);
      if (trim(state.editor.apiKey)) draft.api_key = state.editor.apiKey;
      if (trim(state.editor.defaultModel)) draft.default_model = trim(state.editor.defaultModel);
    }
    draft.provider_id = providerId;
    state.editor.jsonText = JSON.stringify(draft, null, 2);
    state.editor.validation = null;
    state.editor.probe = null;
    renderAll();
  }

  async function handleTest() {
    const state = llmState();
    reconcileBindingContextWindowTokens();
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

  async function handleFetchModelList() {
    const state = llmState();
    syncBindingInputs();
    const editor = state.editor;
    const providerId = trim(document.getElementById("llm-provider-select")?.value || editor.providerId);
    editor.providerId = providerId;
    const jsonText = String(document.getElementById("llm-json-editor")?.value || editor.jsonText || "");
    editor.jsonText = jsonText;
    let draft;
    try {
      draft = parseDraftJson(jsonText, providerId);
    } catch (error) {
      editor.modelList = { loading: false, error: error?.message || "JSON 配置解析失败。", items: [], filter: "" };
      renderModelListPanel();
      return;
    }
    if (trim(editor.baseUrl)) draft.base_url = trim(editor.baseUrl);
    if (trim(editor.apiKey)) draft.api_key = trim(editor.apiKey);
    editor.modelList = { loading: true, error: "", items: [], filter: "" };
    renderModelListPanel();
    try {
      const result = await ApiClient.listLlmDraftModels(draft);
      if (result?.success) {
        const items = Array.isArray(result.models) ? result.models.map((item) => trim(item)).filter(Boolean) : [];
        editor.modelList = { loading: false, error: "", items, filter: "" };
        showToast({ title: "获取模型列表成功", text: `共获取到 ${items.length} 个模型。`, kind: "success", durationMs: 2200 });
      } else {
        const errors = Array.isArray(result?.diagnostics?.errors) ? result.diagnostics.errors : [];
        const message = errors.length
          ? errors.map((item) => `${item.field || "field"}: ${item.message || item.code || "错误"}`).join("；")
          : trim(result?.message) || "获取模型列表失败。";
        editor.modelList = { loading: false, error: message, items: [], filter: "" };
      }
    } catch (error) {
      editor.modelList = { loading: false, error: error?.message || "获取模型列表失败。", items: [], filter: "" };
    }
    renderModelListPanel();
  }

  function applyModelListItem(modelId) {
    const editor = llmState().editor;
    if (!editor) return;
    const value = trim(modelId);
    if (!value) return;
    const providerId = trim(document.getElementById("llm-provider-select")?.value || editor.providerId);
    const jsonText = String(document.getElementById("llm-json-editor")?.value || editor.jsonText || "");
    let draft = null;
    try {
      draft = parseDraftJson(jsonText, providerId);
    } catch (_error) {
      draft = null;
    }
    if (!draft) {
      showToast({ title: "无法填入模型", text: "当前 JSON 配置不可解析，请先修正 JSON 配置。", kind: "error" });
      return;
    }
    draft.default_model = value;
    setBindingJsonEditorValue(JSON.stringify(draft, null, 2));
    syncDefaultModelInputValue(value);
    let filledModelKey = false;
    if (editor.mode === "create") {
      const modelKeyInput = document.getElementById("llm-model-key-input");
      if (modelKeyInput && !trim(modelKeyInput.value)) {
        modelKeyInput.value = value;
        editor.modelKey = value;
        filledModelKey = true;
      }
    }
    editor.modelList = null;
    renderModelListPanel();
    showToast({
      title: "已填入模型",
      text: filledModelKey ? `已将 ${value} 填入模型ID与 JSON 配置。` : `已将模型ID设置为 ${value}。`,
      kind: "success",
      durationMs: 2600,
    });
  }

  async function handleTestMaxConcurrency() {
    const state = llmState();
    syncBindingInputs();
    const { confirmed } = await requestInlineConfirm({
      title: "确认测试最大并发数？",
      text: "测试会向供应商逐步发送递增的并发请求，可能触发限流或消耗 API 配额。是否继续？",
      confirmLabel: "开始测试",
      confirmKind: "danger",
    });
    if (!confirmed) return;
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

  function normalizeRuntimeRefreshStatus(runtimeRefresh) {
    return runtimeRefresh && typeof runtimeRefresh === "object" ? runtimeRefresh : {};
  }

  async function waitForRuntimeRefreshAndUpdateToast(runtimeRefresh) {
    const refresh = normalizeRuntimeRefreshStatus(runtimeRefresh);
    const commandId = trim(refresh.worker_refresh_command_id || refresh.workerRefreshCommandId || "");
    const initialStatus = trim(refresh.worker_refresh_status || refresh.workerRefreshStatus || "");
    const requested = Boolean(refresh.worker_refresh_requested ?? refresh.workerRefreshRequested);
    const acked = Boolean(refresh.worker_refresh_acked ?? refresh.workerRefreshAcked);
    if (!requested || acked || !commandId) {
      if (acked || initialStatus === "completed") {
        showToast({ title: "同步新配置完成", text: "新配置已同步到运行时。", kind: "success", durationMs: 2200 });
        return;
      }
      if (initialStatus === "offline") {
        showToast({ title: "已保存", text: "worker 当前离线，新的后台任务会在 worker 恢复后使用新配置。", kind: "warn", durationMs: 4200 });
        return;
      }
      if (trim(refresh.error || "")) {
        showToast({ title: "已保存", text: `同步新配置失败：${trim(refresh.error)}`, kind: "warn", durationMs: 4200 });
        return;
      }
      showToast({ title: "已保存", text: "新配置已保存。", kind: "success", durationMs: 2200 });
      return;
    }

    showToast({ title: "已保存", text: "同步新配置中", kind: "info", persistent: true });
    const maxPolls = 60;
    for (let pollIndex = 0; pollIndex < maxPolls; pollIndex += 1) {
      await new Promise((resolve) => window.setTimeout(resolve, pollIndex === 0 ? 600 : 1000));
      let current = null;
      try {
        current = await ApiClient.getRuntimeRefreshStatus(commandId);
      } catch (error) {
        const message = trim(error?.message || "");
        if (pollIndex >= 4) {
          showToast({ title: "已保存", text: message ? `同步状态查询失败：${message}` : "同步状态查询失败，请稍后检查。", kind: "warn", durationMs: 4200 });
          return;
        }
        continue;
      }
      const status = trim(current?.status || "").toLowerCase();
      if (status === "completed") {
        showToast({ title: "同步新配置完成", text: "新配置已同步到 worker。", kind: "success", durationMs: 2200 });
        return;
      }
      if (status === "failed") {
        const errorText = trim(current?.error_text || current?.errorText || "");
        showToast({ title: "已保存", text: errorText ? `同步新配置失败：${errorText}` : "同步新配置失败。", kind: "warn", durationMs: 4200 });
        return;
      }
    }
    showToast({ title: "已保存", text: "同步新配置仍在后台进行中。", kind: "warn", durationMs: 4200 });
  }

  async function handleCreateSave() {
    const state = llmState();
    reconcileBindingContextWindowTokens();
    const bindingDraft = bindingDraftPayload({ requireModelKey: true });
    const modelKey = bindingDraft.modelKey;
    const providerId = trim(document.getElementById("llm-provider-select")?.value || state.editor.providerId);
    const jsonText = document.getElementById("llm-json-editor")?.value || state.editor.jsonText;
    if (!modelKey) throw new Error(bindingNameRequiredMessage());
    state.editor.modelKey = modelKey;
    state.editor.providerId = providerId;
    state.editor.jsonText = jsonText;
    const draft = bindingDraft.draft;
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
      const saveResult = await ApiClient.createLlmBinding({
        binding: {
          key: modelKey,
          config_id: "",
          enabled: true,
          retry_on: [...bindingDraft.retryOn],
          retry_count: bindingDraft.retryCount,
          single_api_key_max_concurrency: bindingDraft.singleApiKeyMaxConcurrency,
          image_multimodal_enabled: bindingDraft.imageMultimodalEnabled,
        },
        draft,
      });
      closeEditor();
      await waitForRuntimeRefreshAndUpdateToast(saveResult?.runtimeRefresh);
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
    reconcileBindingContextWindowTokens();
    const bindingDraft = bindingDraftPayload();
    const jsonText = document.getElementById("llm-json-editor")?.value || state.editor.jsonText;
    state.editor.jsonText = jsonText;
    const draft = bindingDraft.draft;
    const configChanged = trim(jsonText) !== trim(state.editor.initialJsonText || "");
    const bindingPatch = {};
    if (JSON.stringify(bindingDraft.retryOn) !== JSON.stringify(binding.retry_on || [])) bindingPatch.retry_on = bindingDraft.retryOn;
    if (bindingDraft.retryCount !== Number.parseInt(String(binding.retry_count ?? 0), 10)) bindingPatch.retry_count = bindingDraft.retryCount;
    if (!singleApiKeyMaxConcurrencyEquals(bindingDraft.singleApiKeyMaxConcurrency, binding.single_api_key_max_concurrency ?? binding.singleApiKeyMaxConcurrency ?? null)) {
      bindingPatch.single_api_key_max_concurrency = bindingDraft.singleApiKeyMaxConcurrency;
    }
    if (bindingDraft.imageMultimodalEnabled !== Boolean(binding.image_multimodal_enabled)) {
      bindingPatch.image_multimodal_enabled = bindingDraft.imageMultimodalEnabled;
    }
    state.saving = true;
    renderAll();
    try {
      let runtimeRefresh = null;
      if (configChanged) {
        showToast({
          title: "Saving",
          text: "Validating current JSON config before applying changes...",
          kind: "success",
          persistent: true,
        });
        const ok = await probeDraft(draft);
        if (!ok) throw new Error("请先修正 JSON 配置并通过连接测试");
        const configSaveResult = await ApiClient.updateLlmConfig(state.editor.configId, draft);
        runtimeRefresh = configSaveResult?.runtimeRefresh || runtimeRefresh;
      }
      if (Object.keys(bindingPatch).length) {
        showToast({
          title: "Saving",
          text: "Applying current retry and fallback policy...",
          kind: "success",
          persistent: true,
        });
        const bindingSaveResult = await ApiClient.updateLlmBinding(state.editor.bindingKey, bindingPatch);
        runtimeRefresh = bindingSaveResult?.runtimeRefresh || runtimeRefresh;
      }
      if (!configChanged && !Object.keys(bindingPatch).length) {
        showToast({ title: "无需保存", text: "当前没有需要应用的修改。", kind: "info" });
        return;
      }
      closeEditor();
      await waitForRuntimeRefreshAndUpdateToast(runtimeRefresh);
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
      const deleteResult = await ApiClient.deleteLlmBinding(binding.key);
      await waitForRuntimeRefreshAndUpdateToast(deleteResult?.runtimeRefresh);
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
    U.llmConfigCreate?.addEventListener("click", () => void openCreateModal());
    U.llmEditorBackdrop?.addEventListener("click", closeEditor);
    U.llmEditorShell?.addEventListener("change", (event) => {
      if (event.target?.id === "llm-provider-select") void handleProviderChange();
    });
    U.llmEditorShell?.addEventListener("input", (event) => {
      const targetId = event.target?.id;
      if (targetId === "llm-binding-context-window-tokens") {
        handleBindingContextWindowInput();
        return;
      }
      if (targetId === "llm-json-editor") {
        handleBindingJsonEditorInput();
        return;
      }
      if (targetId === "llm-binding-base-url") {
        handleBindingBaseUrlInput();
        return;
      }
      if (targetId === "llm-binding-api-key") {
        handleBindingApiKeyInput();
        return;
      }
      if (targetId === "llm-binding-default-model") {
        handleBindingDefaultModelInput();
        return;
      }
      if (targetId === "llm-model-list-filter") {
        const editor = llmState().editor;
        if (editor?.modelList) {
          editor.modelList.filter = String(event.target.value || "");
          renderModelListItems();
        }
      }
    });
    U.llmEditorShell?.addEventListener("click", (event) => {
      const modelItem = event.target.closest("[data-llm-model-item]");
      if (modelItem) {
        applyModelListItem(modelItem.dataset.llmModelItem);
        return;
      }
      const action = event.target.closest("[data-llm-action]")?.dataset.llmAction;
      if (!action) return;
      if (action === "close") { closeEditor(); return; }
      if (action === "toggle-concurrency-test") {
        const field = event.target.closest(".resource-field");
        const row = field?.querySelector(".llm-concurrency-test-row");
        if (row) row.hidden = !row.hidden;
        return;
      }
      if (action === "test-create" || action === "test-detail") { void handleTest().catch((error) => { llmState().error = error.message || "测试失败"; showToast({ title: "测试失败", text: llmState().error, kind: "error" }); renderAll(); }); return; }
      if (action === "test-max-concurrency") { void handleTestMaxConcurrency().catch((error) => { llmState().error = error.message || "测试最大并发数失败"; showToast({ title: "测试最大并发数失败", text: llmState().error, kind: "error" }); renderAll(); }); return; }
      if (action === "fetch-model-list") { void handleFetchModelList().catch((error) => { showToast({ title: "获取模型列表失败", text: error?.message || "获取模型列表失败", kind: "error" }); }); return; }
      if (action === "model-list-close") { const editor = llmState().editor; if (editor) editor.modelList = null; renderModelListPanel(); return; }
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
  window.__llmTestHooks = {
    expandSingleApiKeyMaxConcurrencyForEditor,
    parseSingleApiKeyMaxConcurrencyInput,
    validateSingleApiKeyMaxConcurrencyInput,
    bindingNotesTitle,
    bindingNameLabel,
    bindingNameRequiredMessage,
    normalizeBindingNameText,
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
    const validationMessage = requiredModelRoleValidationMessage();
    if (validationMessage) {
      llmState().error = validationMessage;
      showToast({ title: "保存失败", text: llmState().error, kind: "error" });
      renderAll();
      return;
    }
    llmState().saving = true;
    renderAll();
    try {
      showToast({
        title: "正在保存模型链",
        text: "正在提交当前角色模型链变更...",
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
    } catch (error) {
      llmState().error = error?.message || "保存失败";
      showToast({ title: "保存失败", text: llmState().error, kind: "error" });
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
