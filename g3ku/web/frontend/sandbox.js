'use strict';

const DIMENSION_LABELS = {
  fs_read: '读',
  fs_write: '写',
  network: '网络',
  process: '进程',
  resource: '资源',
};

// 右上角只留三档：未装 / 已装 / 装了但有问题。装好之前缺后端是常态，不报红。
const STATUS_LABELS = {
  ready: '已安装沙箱',
  partial: '需要修复',
  runtime_missing: '未安装沙箱',
  not_installed: '未安装沙箱',
};

const STATUS_TONE = {
  ready: 'ok',
  partial: 'warn',
  runtime_missing: 'neutral',
  not_installed: 'neutral',
};

const INSTALL_LABELS = {
  ready: '已安装',
  partial: '需要修复',
  runtime_missing: '未安装',
  not_installed: '未安装',
};

const REASON_TEXT = {
  wsl_distro_missing: '这台机器还没有 WSL 发行版，需要先安装一个。',
  bwrap_missing_in_distro: '发行版里没有 bubblewrap，安装器可以代为安装。',
  no_qualified_distro: '已有发行版缺少 bubblewrap 或 python3，暂不能承载沙箱。',
  bwrap_missing: '系统里没有 bubblewrap，安装器可以代为安装。',
  unprivileged_userns_disabled: '系统禁用了非特权 user namespace，需要先放开。',
  seatbelt_unavailable: '当前 macOS 上探测不到 sandbox-exec。',
  unsupported_platform: '当前平台没有可用的沙箱后端。',
};

const MODE_ERROR_TEXT = {
  unlock_password_required: '这次切换需要项目解锁口令',
  unlock_password_invalid: '口令不正确',
  sandbox_mode_invalid: '档位取值不合法',
  session_not_found: '会话不存在',
  writable_roots_owner_only: '只有 owner 会话能配额外可写目录',
};

const state = {
  status: null,
  plan: null,
  current: null,
  sessions: [],
  sessionId: '',
  pollTimer: null,
  pendingMode: '',
  confirmMode: '',
  area: 'workspace',
};

const $ = (id) => document.getElementById(id);

async function api(path, options) {
  const response = await fetch(path, {
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' },
    ...options,
  });
  let payload = null;
  try {
    payload = await response.json();
  } catch (error) {
    payload = null;
  }
  if (!response.ok) {
    const detail = payload && payload.detail ? payload.detail : response.status;
    const error = new Error(String(detail));
    error.status = response.status;
    error.payload = payload;
    throw error;
  }
  return payload;
}

function toast(message) {
  const node = $('toast');
  node.textContent = message;
  node.hidden = false;
  window.clearTimeout(toast.timer);
  toast.timer = window.setTimeout(() => {
    node.hidden = true;
  }, 4200);
}

function badge(node, text, tone) {
  node.textContent = text;
  node.dataset.state = tone || 'neutral';
}

async function copyText(text) {
  const value = String(text || '').trim();
  if (!value) return;
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(value);
    } else {
      const area = document.createElement('textarea');
      area.value = value;
      area.setAttribute('readonly', '');
      area.style.position = 'fixed';
      area.style.opacity = '0';
      document.body.appendChild(area);
      area.select();
      document.execCommand('copy');
      area.remove();
    }
    toast('命令已复制');
  } catch (error) {
    toast('复制失败，请手动选择命令');
  }
}

function isInstalled(reading) {
  const value = String((reading || {}).install_state || '');
  return value === 'ready' || value === 'partial';
}

function renderInstallCta(item) {
  const button = $('install-cta');
  const installState = String(item.install_state || 'not_installed');
  if (installState === 'ready') {
    button.hidden = true;
    return;
  }
  const installable = Boolean(item.install_available);
  const partial = installState === 'partial';
  button.hidden = false;
  button.textContent = installable ? (partial ? '修复沙箱' : '一键安装') : partial ? '修复说明' : '安装说明';
  button.classList.toggle('primary', installable);
}

function renderStatus(item) {
  state.status = item;
  const installState = item.install_state || 'not_installed';
  badge($('status-badge'), STATUS_LABELS[installState] || installState, STATUS_TONE[installState] || 'neutral');
  $('install-state').textContent = INSTALL_LABELS[installState] || installState;
  $('backend').textContent = item.backend || '（未选择）';
  $('reason').textContent = REASON_TEXT[item.reason] || item.reason || '—';

  const dimensions = $('dimensions');
  dimensions.textContent = '';
  Object.entries(DIMENSION_LABELS).forEach(([key, label]) => {
    const value = (item.enforcement || {})[key] || 'missing';
    const chip = document.createElement('span');
    chip.className = 'dimension';
    chip.dataset.state = value === 'full' ? 'ok' : value === 'partial' ? 'warn' : 'missing';
    const name = document.createElement('span');
    name.className = 'dimension-label';
    name.textContent = label;
    const reading = document.createElement('span');
    reading.className = 'dimension-value';
    reading.textContent = value === 'full' ? '完整' : value === 'partial' ? '部分' : '缺失';
    chip.append(name, reading);
    dimensions.appendChild(chip);
  });

  renderInstallCta(item);

  const uninstallBtn = $('uninstall-btn');
  const dedicated = Boolean((item.detail || {}).dedicated);
  uninstallBtn.hidden = !dedicated;
  const runtimeActions = uninstallBtn.closest('.panel-actions');
  if (runtimeActions) runtimeActions.hidden = uninstallBtn.hidden;

  const hint = $('runtime-hint');
  if (installState === 'ready') {
    hint.textContent = item.file_management_available
      ? '沙箱已就绪。会话切到受限档后，数据面工具在 jail 内执行。'
      : '沙箱已就绪。';
  } else {
    // 未就绪时原因已经写在「说明」行，操作入口在页头；卡内不再重复一遍。
    hint.textContent = '';
  }
  // 空提示仍占一个网格项和一道 gap，会把右列无谓撑高。
  hint.hidden = !hint.textContent;
  renderFileSection();
}

function renderPlan(plan) {
  state.plan = plan;
  const installable = Boolean(plan.installable);
  const ready = String(plan.install_state || '') === 'ready';
  const partial = String(plan.install_state || '') === 'partial';
  $('plan-title').textContent = ready
    ? '安装内容'
    : installable
      ? partial
        ? '修复沙箱'
        : '一键安装沙箱'
      : '安装说明';
  $('plan-intro').textContent = ready
    ? '沙箱运行时已就绪，无需安装。'
    : installable
      ? '确认下面的步骤后开始；安装后台执行，关掉页面不会中断。需要授权的步骤会弹出系统授权窗口。'
      : '这些步骤需要你手动完成；完成后点「重新检测」。';

  const list = $('plan-steps');
  list.textContent = '';
  (plan.steps || []).forEach((step) => {
    const row = document.createElement('li');
    row.dataset.status = step.manual ? 'manual' : '';
    const title = document.createElement('div');
    title.textContent = step.title;
    row.appendChild(title);
    if (step.command && step.command.length) {
      const commandRow = document.createElement('div');
      commandRow.className = 'command-row';
      const code = document.createElement('code');
      code.textContent = step.command.join(' ');
      const copy = document.createElement('button');
      copy.type = 'button';
      copy.className = 'toolbar-btn small';
      copy.textContent = '复制';
      copy.addEventListener('click', () => copyText(step.command.join(' ')));
      commandRow.append(code, copy);
      row.appendChild(commandRow);
    }
    if (step.detail) {
      const detail = document.createElement('div');
      detail.className = 'hint';
      detail.textContent = step.detail;
      row.appendChild(detail);
    }
    const flags = document.createElement('div');
    flags.className = 'step-flags';
    if (step.manual) {
      const manual = document.createElement('span');
      manual.className = 'status-pill small';
      manual.dataset.state = 'warn';
      manual.textContent = '手动执行';
      flags.appendChild(manual);
    }
    if (step.elevated) {
      const flag = document.createElement('span');
      flag.className = 'status-pill small';
      flag.dataset.state = 'warn';
      flag.textContent = '需要授权';
      flags.appendChild(flag);
    }
    if (flags.childNodes.length) row.appendChild(flags);
    list.appendChild(row);
  });

  const notes = $('plan-notes');
  notes.textContent = '';
  (plan.notes || []).forEach((note) => {
    const item = document.createElement('li');
    item.textContent = note;
    notes.appendChild(item);
  });

  $('confirm-check').checked = false;
  $('confirm-install-btn').disabled = true;
  $('confirm-install-btn').hidden = !installable;
  $('confirm-row').hidden = !installable;
  $('plan-refresh-btn').hidden = installable || ready;
}

function renderProgress(status) {
  $('progress-card').hidden = false;
  const list = $('progress-steps');
  list.textContent = '';
  (status.steps || []).forEach((step) => {
    const row = document.createElement('li');
    row.dataset.status = step.status || '';
    row.textContent = `${step.id}：${step.status}（${step.seconds || 0}s）`;
    list.appendChild(row);
  });
  const outputs = (status.steps || [])
    .filter((step) => step.output)
    .map((step) => `# ${step.id}\n${step.output}`)
    .join('\n\n');
  const output = $('progress-output');
  output.textContent = outputs;
  output.hidden = !outputs;
}

async function loadStatus(refresh) {
  const suffix = refresh ? '?refresh=1' : '';
  const payload = await api(`/api/sandbox/status${suffix}`);
  renderStatus(payload.item || {});
}

function renderFiles(entries) {
  const list = $('file-list');
  list.textContent = '';
  const items = entries || [];
  $('files-empty').hidden = items.length > 0;
  items.forEach((entry) => {
    const row = document.createElement('li');
    const kind = document.createElement('span');
    kind.className = 'kind';
    kind.textContent = entry.kind === 'dir' ? '目录' : '文件';
    const name = document.createElement('span');
    name.className = 'name';
    name.textContent = entry.name;
    const meta = document.createElement('span');
    meta.className = 'meta';
    meta.textContent = entry.kind === 'dir' ? '—' : `${entry.size || 0} B`;
    row.append(kind, name, meta);
    if (entry.kind !== 'dir') {
      const link = document.createElement('a');
      link.className = 'toolbar-btn small';
      link.textContent = '下载';
      link.href = `/api/ceo/sessions/${encodeURIComponent(currentSessionId())}/sandbox/export?area=${encodeURIComponent(
        state.area,
      )}&name=${encodeURIComponent(entry.name)}`;
      row.appendChild(link);
    }
    list.appendChild(row);
  });
}

async function loadFiles() {
  const sessionId = currentSessionId();
  if (!sessionId || !state.status || !state.status.file_management_available) return;
  try {
    const payload = await api(
      `/api/ceo/sessions/${encodeURIComponent(sessionId)}/sandbox/files?area=${encodeURIComponent(state.area)}`,
    );
    renderFiles((payload.item || {}).entries || []);
  } catch (error) {
    renderFiles([]);
    $('files-hint').textContent = error.status === 409 ? '沙箱尚未安装，文件管理不可用。' : `读取失败：${error.message}`;
  }
}

async function uploadFile(file) {
  const sessionId = currentSessionId();
  if (!sessionId || !file) return;
  const form = new FormData();
  form.append('file', file);
  try {
    const response = await fetch(`/api/ceo/sessions/${encodeURIComponent(sessionId)}/sandbox/import`, {
      method: 'POST',
      body: form,
      credentials: 'same-origin',
    });
    if (!response.ok) {
      let detail = response.status;
      try {
        detail = (await response.json()).detail || detail;
      } catch (error) {
        detail = response.status;
      }
      throw new Error(String(detail));
    }
    toast(`已导入 ${file.name}`);
    state.area = 'inbox';
    syncAreaTabs();
    await loadFiles();
  } catch (error) {
    toast(`导入失败：${error.message}`);
  }
}

function syncAreaTabs() {
  document.querySelectorAll('#area-tabs .tab').forEach((node) => {
    const active = node.dataset.area === state.area;
    node.classList.toggle('active', active);
    node.setAttribute('aria-selected', active ? 'true' : 'false');
  });
}

function renderFileSection() {
  const available = Boolean(state.status && state.status.file_management_available && state.current);
  $('files-card').hidden = !available;
  if (!available) return;
  syncAreaTabs();
  loadFiles();
}

async function loadPlan() {
  const payload = await api('/api/sandbox/install/plan');
  renderPlan(payload.item || {});
  if (payload.status && payload.status.running) {
    watchInstall();
  }
}

async function openPlanModal() {
  try {
    await loadPlan();
    $('plan-backdrop').hidden = false;
    window.requestAnimationFrame(() => $('cancel-plan-btn').focus());
  } catch (error) {
    toast(`读取安装内容失败：${error.message}`);
  }
}

function closePlanModal() {
  $('plan-backdrop').hidden = true;
}

async function startInstall() {
  try {
    const payload = await api('/api/sandbox/install', {
      method: 'POST',
      body: JSON.stringify({ confirm: true }),
    });
    closePlanModal();
    renderProgress({ steps: [] });
    watchInstall();
    toast(payload.started ? '安装已开始' : '安装已提交');
  } catch (error) {
    toast(`安装未开始：${error.message}`);
  }
}

function watchInstall() {
  window.clearInterval(state.pollTimer);
  state.pollTimer = window.setInterval(async () => {
    try {
      const payload = await api('/api/sandbox/install/status');
      const status = payload.item || {};
      renderProgress(status);
      if (!status.running) {
        window.clearInterval(state.pollTimer);
        state.pollTimer = null;
        if (status.phase === 'failed') {
          toast(`安装失败：${status.error || '请查看步骤输出'}`);
        } else if (status.phase === 'done') {
          toast('安装流程结束，正在重新探测');
        }
        await loadStatus(true);
        await loadPlan();
      }
    } catch (error) {
      window.clearInterval(state.pollTimer);
      state.pollTimer = null;
      toast(`进度读取失败：${error.message}`);
    }
  }, 1200);
}

/* ---------------- 会话选择（自绘下拉） ---------------- */

function syncSelectLabel() {
  const label = $('session-select-label');
  const item = state.sessions.find((entry) => entry.id === state.sessionId);
  if (item) {
    label.textContent = `${item.title || '(未命名)'} · ${item.id}`;
  } else {
    label.textContent = state.sessionId || '没有可用会话';
  }
  label.title = label.textContent;
}

function renderSessionOptions() {
  const menu = $('session-select-menu');
  menu.textContent = '';
  if (!state.sessions.length) {
    const empty = document.createElement('div');
    empty.className = 'select-empty';
    empty.textContent = '没有可用会话';
    menu.appendChild(empty);
    syncSelectLabel();
    return;
  }
  state.sessions.forEach((item) => {
    const option = document.createElement('button');
    option.type = 'button';
    option.className = 'select-option';
    option.setAttribute('role', 'option');
    option.setAttribute('aria-selected', item.id === state.sessionId ? 'true' : 'false');
    option.dataset.sessionId = item.id;
    const label = document.createElement('span');
    label.className = 'select-option-label';
    label.textContent = `${item.title || '(未命名)'} · ${item.id}`;
    option.title = label.textContent;
    option.appendChild(label);
    option.addEventListener('click', () => selectSession(item.id));
    menu.appendChild(option);
  });
  syncSelectLabel();
}

function openSelectMenu() {
  if (!state.sessions.length) return;
  $('session-select').classList.add('open');
  $('session-select-trigger').setAttribute('aria-expanded', 'true');
  const menu = $('session-select-menu');
  menu.hidden = false;
  const selected = menu.querySelector('[aria-selected="true"]');
  window.requestAnimationFrame(() => (selected || menu.querySelector('.select-option'))?.focus());
}

function closeSelectMenu() {
  $('session-select').classList.remove('open');
  $('session-select-trigger').setAttribute('aria-expanded', 'false');
  $('session-select-menu').hidden = true;
}

function toggleSelectMenu() {
  if ($('session-select-menu').hidden) openSelectMenu();
  else closeSelectMenu();
}

function selectSession(sessionId) {
  state.sessionId = sessionId;
  closeSelectMenu();
  renderSessionOptions();
  void loadSessionDetail();
}

async function loadSessions() {
  const preset = String(state.preset || '').trim();
  try {
    const payload = await api('/api/ceo/sessions');
    const items = payload.items || [];
    state.sessions = items
      .map((item) => ({
        id: item.session_id || item.id || item.session_key || '',
        title: item.title || '',
      }))
      .filter((item) => item.id);
    if (preset && state.sessions.some((item) => item.id === preset)) {
      state.sessionId = preset;
      $('manual-session-field').hidden = true;
    } else if (preset) {
      $('manual-session').value = preset;
      $('manual-session-field').hidden = false;
    } else {
      $('manual-session-field').hidden = true;
      if (state.sessions.length && !state.sessionId) {
        state.sessionId = state.sessions[0].id;
      }
    }
    renderSessionOptions();
  } catch (error) {
    // 目录接口要运行时就绪才给数据；拿不到时让操作员直接贴会话 ID，页面仍然可用。
    state.sessions = [];
    renderSessionOptions();
    if (preset) $('manual-session').value = preset;
    $('manual-session-field').hidden = false;
    toast(`会话列表不可用：${error.message}。可以直接粘贴会话 ID。`);
  }
}

function currentSessionId() {
  return $('manual-session').value.trim() || state.sessionId;
}

/* ---------------- 档位 ---------------- */

function renderSession(item) {
  state.current = item;
  state.pendingMode = item.mode;
  $('session-panel').hidden = false;
  badge($('session-trust'), `信任：${item.trust}`, item.trust === 'owner' ? 'ok' : 'warn');
  badge(
    $('session-tier'),
    `当前：${item.tier_label || item.resolved_tier}`,
    item.resolved_tier === 'host_full' ? 'neutral' : 'ok',
  );

  const labels = item.ui_labels || { restricted: '只读', unrestricted: '全权限' };
  const describeRestricted = item.resolved_tier === 'jailed'
    ? '数据面工具在 jail 内执行；控制面仍然拒绝。'
    : '可读本会话的上传与临时目录，不能写文件、不能执行命令。';
  const describeUnrestricted = item.trust === 'owner'
    ? '与今天一致：宿主执行，按现有权限。'
    : '数据面回到宿主执行，控制面拒绝与记忆门仍然生效。';

  const switchNode = $('mode-switch');
  switchNode.textContent = '';
  [['restricted', labels.restricted, describeRestricted], ['unrestricted', labels.unrestricted, describeUnrestricted]]
    .forEach(([value, label, description]) => {
      const wrapper = document.createElement('label');
      wrapper.className = 'mode-option';
      if (item.mode === value) wrapper.classList.add('active');
      const input = document.createElement('input');
      input.type = 'radio';
      input.name = 'sandbox-mode';
      input.value = value;
      input.checked = item.mode === value;
      input.addEventListener('change', () => {
        state.pendingMode = value;
        document.querySelectorAll('.mode-option').forEach((node) => node.classList.remove('active'));
        wrapper.classList.add('active');
        syncSaveVisibility();
      });
      const text = document.createElement('div');
      const strong = document.createElement('strong');
      strong.textContent = label;
      const small = document.createElement('small');
      small.textContent = description;
      text.append(strong, small);
      wrapper.append(input, text);
      switchNode.appendChild(wrapper);
    });

  $('save-result').textContent = '';
  const rootsField = $('roots-field');
  rootsField.hidden = item.trust !== 'owner';
  const roots = item.writable_roots || [];
  $('roots-input').value = roots.join('\n');
  rootsField.open = roots.length > 0;
  syncSaveVisibility();
  renderFileSection();
}

function collectRoots() {
  const field = $('roots-field');
  if (!state.current || state.current.trust !== 'owner' || field.hidden) return null;
  return $('roots-input')
    .value.split('\n')
    .map((line) => line.trim())
    .filter(Boolean);
}

function rootsDirty() {
  const current = state.current;
  if (!current || current.trust !== 'owner') return false;
  const roots = collectRoots() || [];
  const saved = (current.writable_roots || []).map((item) => String(item));
  return roots.join('\n') !== saved.join('\n');
}

function formDirty() {
  const current = state.current;
  if (!current) return false;
  if (state.pendingMode && state.pendingMode !== current.mode) return true;
  return rootsDirty();
}

function syncSaveVisibility() {
  const dirty = formDirty();
  $('save-mode-btn').hidden = !dirty;
  if (dirty) $('save-result').textContent = '';
}

function impactItems(targetMode) {
  const current = state.current || {};
  const thirdParty = current.trust !== 'owner';
  const installed = isInstalled(state.status);
  const cacheText = '切档在下一回合生效；暴露给模型的工具清单会变，下一次请求的缓存前缀失效，首回合要重新预热。';
  if (targetMode === 'restricted') {
    return [
      {
        title: '工具面',
        text: installed
          ? '收窄到只读白名单；写文件、执行命令、联网等动作被拒绝。已装沙箱时数据面工具在 jail 内执行。'
          : '收窄到只读：只能读本会话的上传与临时目录；不能写文件、执行命令、联网，也不能派异步任务。',
      },
      { title: '缓存', text: cacheText },
      {
        title: '安全',
        text: thirdParty
          ? '白名单外的工具调用会被拒绝；这是第三方会话，记忆门继续生效。'
          : '白名单外的工具调用会被拒绝；这是操作员自己的会话，随时可以再关掉。',
      },
    ];
  }
  return [
    {
      title: '工具面',
      text: '数据面工具回到宿主执行；写文件、执行命令、联网恢复可用。',
    },
    { title: '缓存', text: cacheText },
    {
      title: '安全',
      text: thirdParty
        ? '控制面拒绝与记忆门仍然生效，但宿主文件与命令只剩 exec 自身的形态守卫；这个群的发送者因此能触达本机能力。'
        : '工具按你当前系统账号的权限执行；误操作没有第二道边界。',
    },
  ];
}

// 没装成后端时没有"沙箱"可开：开关表达的是只读 / 无限制，确认文案跟着安装态走。
function modeLabels(turningOn) {
  if (isInstalled(state.status)) {
    return {
      title: turningOn ? '开启沙箱模式？' : '关闭沙箱模式？',
      confirm: turningOn ? '开启沙箱模式' : '关闭沙箱模式',
      done: turningOn ? '已开启沙箱模式' : '已关闭沙箱模式',
    };
  }
  return {
    title: turningOn ? '进入只读模式？' : '退出只读模式？',
    confirm: turningOn ? '进入只读模式' : '退出只读模式',
    done: turningOn ? '已进入只读模式' : '已退出只读模式',
  };
}

function openModeConfirm(targetMode) {
  state.confirmMode = targetMode;
  const turningOn = targetMode === 'restricted';
  const labels = modeLabels(turningOn);
  $('mode-confirm-title').textContent = labels.title;
  const list = $('mode-confirm-impact');
  list.textContent = '';
  impactItems(targetMode).forEach(({ title, text }) => {
    const row = document.createElement('li');
    const heading = document.createElement('strong');
    heading.textContent = title;
    const body = document.createElement('span');
    body.textContent = text;
    row.append(heading, body);
    list.appendChild(row);
  });
  $('mode-confirm-password').value = '';
  $('mode-confirm-error').hidden = true;
  $('mode-confirm-accept').textContent = labels.confirm;
  $('mode-confirm-backdrop').hidden = false;
  window.requestAnimationFrame(() => $('mode-confirm-password').focus());
}

function closeModeConfirm() {
  $('mode-confirm-backdrop').hidden = true;
  state.confirmMode = '';
}

function modeErrorText(error) {
  return MODE_ERROR_TEXT[error.message] || `切换失败：${error.message}`;
}

async function applyModeChange() {
  const sessionId = currentSessionId();
  const targetMode = state.confirmMode;
  if (!sessionId || !targetMode) return;
  const password = $('mode-confirm-password').value.trim();
  const errorNode = $('mode-confirm-error');
  if (!password) {
    errorNode.textContent = '请输入项目解锁口令。';
    errorNode.hidden = false;
    return;
  }
  const body = { mode: targetMode, unlock_password: password };
  const roots = collectRoots();
  if (roots) body.writable_roots = roots;
  const accept = $('mode-confirm-accept');
  accept.disabled = true;
  try {
    const payload = await api(`/api/ceo/sessions/${encodeURIComponent(sessionId)}/sandbox`, {
      method: 'PATCH',
      body: JSON.stringify(body),
    });
    closeModeConfirm();
    renderSession(payload.item || {});
    toast(`${modeLabels(targetMode === 'restricted').done}，下一回合生效`);
  } catch (error) {
    errorNode.textContent = modeErrorText(error);
    errorNode.hidden = false;
  } finally {
    accept.disabled = false;
  }
}

async function saveRootsOnly() {
  const sessionId = currentSessionId();
  const roots = collectRoots();
  if (!sessionId || !roots) return;
  const button = $('save-mode-btn');
  button.disabled = true;
  try {
    const payload = await api(`/api/ceo/sessions/${encodeURIComponent(sessionId)}/sandbox`, {
      method: 'PATCH',
      body: JSON.stringify({ writable_roots: roots }),
    });
    renderSession(payload.item || {});
    toast('可写目录已保存');
  } catch (error) {
    $('save-result').textContent = modeErrorText(error);
  } finally {
    button.disabled = false;
  }
}

async function onSave() {
  const sessionId = currentSessionId();
  if (!sessionId || !state.current) return;
  if (!formDirty()) return;
  $('save-result').textContent = '';
  if (state.pendingMode && state.pendingMode !== state.current.mode) {
    openModeConfirm(state.pendingMode);
    return;
  }
  await saveRootsOnly();
}

async function loadSessionDetail() {
  const sessionId = currentSessionId();
  if (!sessionId) {
    $('session-panel').hidden = true;
    return;
  }
  try {
    const payload = await api(`/api/ceo/sessions/${encodeURIComponent(sessionId)}/sandbox`);
    renderSession(payload.item || {});
  } catch (error) {
    toast(`读取会话档位失败：${error.message}`);
  }
}

/* ---------------- 事件 ---------------- */

function bindEvents() {
  $('install-cta').addEventListener('click', openPlanModal);
  $('plan-refresh-btn').addEventListener('click', async () => {
    const button = $('plan-refresh-btn');
    button.disabled = true;
    toast('正在检测沙箱环境状态…');
    try {
      await loadStatus(true);
      await loadPlan();
      const reading = state.status || {};
      const tone = reading.install_state || 'not_installed';
      toast(`沙箱环境：${STATUS_LABELS[tone] || tone}`);
    } catch (error) {
      toast(`检测失败：${error.message}`);
    } finally {
      button.disabled = false;
    }
  });
  $('cancel-plan-btn').addEventListener('click', closePlanModal);
  $('confirm-check').addEventListener('change', (event) => {
    $('confirm-install-btn').disabled = !event.target.checked;
  });
  $('confirm-install-btn').addEventListener('click', startInstall);
  $('uninstall-btn').addEventListener('click', async () => {
    if (!window.confirm('卸载会注销本项目导入的专用发行版；会话卷保留。继续？')) return;
    try {
      const payload = await api('/api/sandbox/uninstall', {
        method: 'POST',
        body: JSON.stringify({ confirm: true }),
      });
      toast(payload.ok ? '已卸载' : '没有可卸载的内容');
      await loadStatus(true);
      await loadPlan();
    } catch (error) {
      toast(`卸载失败：${error.message}`);
    }
  });

  $('session-select-trigger').addEventListener('click', (event) => {
    event.stopPropagation();
    toggleSelectMenu();
  });
  $('session-select-trigger').addEventListener('keydown', (event) => {
    if (event.key === 'Escape') {
      closeSelectMenu();
      return;
    }
    if (event.key === 'ArrowDown' || event.key === 'Enter' || event.key === ' ') {
      event.preventDefault();
      if ($('session-select-menu').hidden) openSelectMenu();
    }
  });
  document.addEventListener('click', (event) => {
    if (!event.target.closest('#session-select')) closeSelectMenu();
  });
  $('manual-session-btn').addEventListener('click', loadSessionDetail);
  $('manual-session').addEventListener('keydown', (event) => {
    if (event.key === 'Enter') loadSessionDetail();
  });

  $('save-mode-btn').addEventListener('click', onSave);
  $('roots-input').addEventListener('input', syncSaveVisibility);
  $('mode-confirm-cancel').addEventListener('click', closeModeConfirm);
  $('mode-confirm-accept').addEventListener('click', applyModeChange);
  $('mode-confirm-password').addEventListener('keydown', (event) => {
    if (event.key === 'Enter') applyModeChange();
  });
  $('mode-confirm-backdrop').addEventListener('click', (event) => {
    if (event.target === $('mode-confirm-backdrop')) closeModeConfirm();
  });
  $('plan-backdrop').addEventListener('click', (event) => {
    if (event.target === $('plan-backdrop')) closePlanModal();
  });
  document.addEventListener('keydown', (event) => {
    if (event.key !== 'Escape') return;
    if (!$('plan-backdrop').hidden) closePlanModal();
    else if (!$('mode-confirm-backdrop').hidden) closeModeConfirm();
  });

  $('persona-save-btn').addEventListener('click', savePersona);
  document.querySelectorAll('#area-tabs .tab').forEach((node) => {
    node.addEventListener('click', () => {
      state.area = node.dataset.area || 'workspace';
      syncAreaTabs();
      loadFiles();
    });
  });
  $('files-refresh-btn').addEventListener('click', loadFiles);
  $('upload-input').addEventListener('change', async (event) => {
    const file = event.target.files && event.target.files[0];
    event.target.value = '';
    if (file) await uploadFile(file);
  });
  $('reset-volume-btn').addEventListener('click', async () => {
    const sessionId = currentSessionId();
    if (!sessionId) return;
    if (!window.confirm('重置会清空该会话的沙箱卷（workspace 与 shared 都删）。确定继续？')) return;
    try {
      await api(`/api/ceo/sessions/${encodeURIComponent(sessionId)}/sandbox/reset`, {
        method: 'POST',
        body: JSON.stringify({ confirm: true }),
      });
      toast('会话卷已重置');
      await loadFiles();
    } catch (error) {
      toast(`重置失败：${error.message}`);
    }
  });
}

async function loadPersona() {
  try {
    const payload = await api('/api/sandbox/persona');
    $('persona-input').value = (payload.item || {}).persona || '';
  } catch (error) {
    $('persona-result').textContent = `读取失败：${error.message}`;
  }
}

async function savePersona() {
  $('persona-save-btn').disabled = true;
  try {
    const payload = await api('/api/sandbox/persona', {
      method: 'PUT',
      body: JSON.stringify({ persona: $('persona-input').value }),
    });
    $('persona-result').textContent = (payload.item || {}).persona ? '已保存' : '已清空（群聊不带人设）';
    toast('群聊人设已更新');
  } catch (error) {
    $('persona-result').textContent = `保存失败：${error.message}`;
  } finally {
    $('persona-save-btn').disabled = false;
  }
}

async function main() {
  bindEvents();
  try {
    await loadStatus(true);
    await loadPlan();
  } catch (error) {
    toast(`初始化失败：${error.message}`);
  }
  // 从权限面板/别处跳进来时带着会话 ID：列表里有就选中，没有才退回手填。
  state.preset = (new URLSearchParams(window.location.search).get('session') || '').trim();
  await loadPersona();
  await loadSessions();
  if (currentSessionId()) {
    await loadSessionDetail();
  }
}

document.addEventListener('DOMContentLoaded', main);

window.addEventListener('message', (event) => {
  const data = event.data || {};
  if (data.type === 'negi-theme' && (data.theme === 'light' || data.theme === 'dark')) {
    document.documentElement.setAttribute('data-theme', data.theme);
    return;
  }
  if (data.type === 'negi-sandbox-refresh') {
    // 外壳里改了档位就通知本页重读；不是当前会话的改动不动选择。
    const changed = String(data.sessionId || '').trim();
    if (changed && changed !== currentSessionId()) return;
    void loadStatus(true).catch(() => {});
    void loadSessionDetail();
  }
});
