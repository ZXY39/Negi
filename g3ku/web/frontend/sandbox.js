'use strict';

const DIMENSION_LABELS = {
  fs_read: '读',
  fs_write: '写',
  network: '网络',
  process: '进程',
  resource: '资源',
};

const STATE_LABELS = {
  ready: '已就绪',
  partial: '部分可用',
  runtime_missing: '需要修复',
  not_installed: '未安装',
};

const STATE_TONE = {
  ready: 'ok',
  partial: 'warn',
  runtime_missing: 'warn',
  not_installed: 'bad',
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

const state = {
  status: null,
  plan: null,
  current: null,
  pollTimer: null,
  pendingMode: '',
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

function renderStatus(item) {
  state.status = item;
  const installState = item.install_state || 'not_installed';
  badge($('status-badge'), STATE_LABELS[installState] || installState, STATE_TONE[installState] || 'neutral');
  $('install-state').textContent = STATE_LABELS[installState] || installState;
  $('backend').textContent = item.backend || '（未选择）';
  $('reason').textContent = REASON_TEXT[item.reason] || item.reason || '—';

  const dimensions = $('dimensions');
  dimensions.textContent = '';
  Object.entries(DIMENSION_LABELS).forEach(([key, label]) => {
    const value = (item.enforcement || {})[key] || 'missing';
    const chip = document.createElement('span');
    chip.className = 'badge';
    chip.dataset.state = value === 'full' ? 'ok' : value === 'partial' ? 'warn' : 'missing';
    chip.textContent = `${label} ${value === 'full' ? '完整' : value === 'partial' ? '部分' : '缺失'}`;
    dimensions.appendChild(chip);
  });

  const installBtn = $('install-btn');
  installBtn.hidden = !item.install_available;
  installBtn.textContent = installState === 'not_installed' ? '安装沙箱' : '修复沙箱';

  const uninstallBtn = $('uninstall-btn');
  const dedicated = Boolean((item.detail || {}).dedicated);
  uninstallBtn.hidden = !dedicated;

  const hint = $('runtime-hint');
  if (installState === 'ready') {
    hint.textContent = item.file_management_available
      ? '沙箱已就绪。数据面工具在会话切到受限档后进 jail 执行。'
      : '沙箱已就绪。';
  } else if (item.install_available) {
    hint.textContent = '点击「查看安装内容」确认要执行的步骤与提权要求。';
  } else {
    hint.textContent = REASON_TEXT[item.reason] || item.install_plan_reason || '当前没有可自动安装的内容。';
  }
  renderFileSection();
}

function renderPlan(plan) {
  state.plan = plan;
  const list = $('plan-steps');
  list.textContent = '';
  (plan.steps || []).forEach((step) => {
    const row = document.createElement('li');
    row.dataset.status = step.manual ? 'manual' : '';
    const title = document.createElement('div');
    title.textContent = step.title;
    row.appendChild(title);
    if (step.command && step.command.length) {
      const code = document.createElement('code');
      code.textContent = step.command.join(' ');
      row.appendChild(code);
    }
    if (step.detail) {
      const detail = document.createElement('div');
      detail.className = 'hint';
      detail.textContent = step.detail;
      row.appendChild(detail);
    }
    if (step.elevated) {
      const flag = document.createElement('span');
      flag.className = 'badge';
      flag.dataset.state = 'warn';
      flag.textContent = '需要授权';
      row.appendChild(flag);
    }
    list.appendChild(row);
  });

  const notes = $('plan-notes');
  notes.textContent = '';
  (plan.notes || []).forEach((note) => {
    const item = document.createElement('li');
    item.textContent = note;
    notes.appendChild(item);
  });

  const confirm = $('confirm-check');
  confirm.checked = false;
  $('confirm-install-btn').disabled = true;
  $('confirm-install-btn').hidden = !plan.installable;
  $('confirm-row').hidden = !plan.installable;
  $('plan-card').hidden = false;
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
    const name = document.createElement('span');
    name.className = 'name';
    name.textContent = `${entry.kind === 'dir' ? '📁' : '📄'} ${entry.name}`;
    const meta = document.createElement('span');
    meta.className = 'meta';
    meta.textContent = entry.kind === 'dir' ? '目录' : `${entry.size || 0} B`;
    row.append(name, meta);
    if (entry.kind !== 'dir') {
      const link = document.createElement('a');
      link.className = 'btn ghost';
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
    node.classList.toggle('active', node.dataset.area === state.area);
  });
}

function renderFileSection() {
  const available = Boolean(state.status && state.status.file_management_available && state.current);
  $('files-card').hidden = !available;
  if (!available) return;
  syncAreaTabs();
  loadFiles();
}

async function loadPlan(reveal) {
  const payload = await api('/api/sandbox/install/plan');
  renderPlan(payload.item || {});
  if (!reveal) {
    $('plan-card').hidden = true;
  }
  if (payload.status && payload.status.running) {
    watchInstall();
  }
}

async function startInstall() {
  try {
    const payload = await api('/api/sandbox/install', {
      method: 'POST',
      body: JSON.stringify({ confirm: true }),
    });
    $('plan-card').hidden = true;
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
        await loadPlan(!$('plan-card').hidden);
      }
    } catch (error) {
      window.clearInterval(state.pollTimer);
      state.pollTimer = null;
      toast(`进度读取失败：${error.message}`);
    }
  }, 1200);
}

async function loadSessions() {
  const select = $('session-select');
  try {
    const payload = await api('/api/ceo/sessions');
    const items = payload.items || [];
    select.textContent = '';
    if (!items.length) {
      const option = document.createElement('option');
      option.value = '';
      option.textContent = '没有可用会话';
      select.appendChild(option);
      return;
    }
    items.forEach((item) => {
      const id = item.session_id || item.id || item.session_key || '';
      if (!id) return;
      const option = document.createElement('option');
      option.value = id;
      option.textContent = `${item.title || '(未命名)'} · ${id}`;
      select.appendChild(option);
    });
    $('manual-session-field').hidden = true;
  } catch (error) {
    // 目录接口要运行时就绪才给数据；拿不到时让操作员直接贴会话 ID，页面仍然可用。
    select.textContent = '';
    const option = document.createElement('option');
    option.value = '';
    option.textContent = '会话列表不可用';
    select.appendChild(option);
    $('manual-session-field').hidden = false;
    toast(`会话列表不可用：${error.message}。可以直接粘贴会话 ID。`);
  }
}

function renderSession(item) {
  state.current = item;
  $('session-panel').hidden = false;
  badge($('session-trust'), `信任：${item.trust}`, item.trust === 'owner' ? 'ok' : 'warn');
  badge($('session-tier'), `当前：${item.tier_label || item.resolved_tier}`, item.resolved_tier === 'host_full' ? 'neutral' : 'ok');

  const labels = item.ui_labels || { restricted: '只读', unrestricted: '全权限' };
  const restrictedText = labels.restricted;
  const unrestrictedText = labels.unrestricted;
  const describeRestricted = item.resolved_tier === 'jailed'
    ? '数据面工具在 jail 内执行；控制面仍然拒绝。'
    : '可读本会话的上传与临时目录，不能写文件、不能执行命令。';
  const describeUnrestricted = item.trust === 'owner'
    ? '与今天一致：宿主执行，按现有权限。'
    : '数据面回到宿主执行，控制面拒绝与记忆门仍然生效。';

  const switchNode = $('mode-switch');
  switchNode.textContent = '';
  [['restricted', restrictedText, describeRestricted], ['unrestricted', unrestrictedText, describeUnrestricted]].forEach(
    ([value, label, description]) => {
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
        $('unlock-field').hidden = !(value === 'unrestricted' && item.unlock_required);
      });
      const text = document.createElement('div');
      const strong = document.createElement('strong');
      strong.textContent = label;
      const small = document.createElement('small');
      small.textContent = description;
      text.append(strong, small);
      wrapper.append(input, text);
      switchNode.appendChild(wrapper);
    },
  );
  state.pendingMode = item.mode;
  $('unlock-field').hidden = true;
  $('unlock-password').value = '';
  $('save-result').textContent = '';
  $('tasks-hint').textContent = item.tasks_allowed
    ? '此档可以派异步任务。'
    : '受限档不能派异步任务：任务节点跑在 worker 里，受限意图传不进那条执行面。';
  renderFileSection();
}

function currentSessionId() {
  return $('session-select').value || $('manual-session').value.trim();
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

async function saveMode() {
  const sessionId = currentSessionId();
  if (!sessionId || !state.pendingMode) return;
  const body = { mode: state.pendingMode };
  const unlockField = $('unlock-field');
  if (!unlockField.hidden) {
    const password = $('unlock-password').value;
    if (!password) {
      $('save-result').textContent = '需要项目解锁口令';
      return;
    }
    body.unlock_password = password;
  }
  $('save-mode-btn').disabled = true;
  try {
    const payload = await api(`/api/ceo/sessions/${encodeURIComponent(sessionId)}/sandbox`, {
      method: 'PATCH',
      body: JSON.stringify(body),
    });
    renderSession(payload.item || {});
    $('save-result').textContent = '已保存，下一回合生效';
    toast('档位已更新，下一回合生效');
  } catch (error) {
    const map = {
      unlock_password_required: '这次切换需要项目解锁口令',
      unlock_password_invalid: '口令不正确',
      sandbox_mode_invalid: '档位取值不合法',
      session_not_found: '会话不存在',
    };
    $('save-result').textContent = map[error.message] || `保存失败：${error.message}`;
  } finally {
    $('save-mode-btn').disabled = false;
  }
}

function bindEvents() {
  $('refresh-btn').addEventListener('click', async () => {
    try {
      await loadStatus(true);
      await loadPlan(!$('plan-card').hidden);
      toast('已重新探测');
    } catch (error) {
      toast(`探测失败：${error.message}`);
    }
  });
  $('plan-btn').addEventListener('click', async () => {
    try {
      await loadPlan(true);
    } catch (error) {
      toast(`读取安装内容失败：${error.message}`);
    }
  });
  $('install-btn').addEventListener('click', async () => {
    try {
      await loadPlan(true);
      if (state.plan && !state.plan.installable) {
        toast(REASON_TEXT[state.plan.reason] || '当前没有可自动安装的内容');
      }
    } catch (error) {
      toast(`读取安装内容失败：${error.message}`);
    }
  });
  $('cancel-plan-btn').addEventListener('click', () => {
    $('plan-card').hidden = true;
  });
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
  $('session-select').addEventListener('change', loadSessionDetail);
  $('manual-session-btn').addEventListener('click', loadSessionDetail);
  $('save-mode-btn').addEventListener('click', saveMode);
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

async function main() {
  bindEvents();
  try {
    await loadStatus(false);
    await loadPlan(false);
  } catch (error) {
    toast(`初始化失败：${error.message}`);
  }
  await loadSessions();
  if (currentSessionId()) {
    await loadSessionDetail();
  }
}

document.addEventListener('DOMContentLoaded', main);
