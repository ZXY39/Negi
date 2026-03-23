(() => {
  const state = {
    status: null,
    unlockedInitDispatched: false,
    busy: false,
  };

  const U = {};

  function refs() {
    U.bootShell = document.getElementById("boot-shell");
    U.appLayout = document.getElementById("app-layout");
    U.bootTitle = document.getElementById("boot-title");
    U.bootSubtitle = document.getElementById("boot-subtitle");
    U.bootBanner = document.getElementById("boot-banner");
    U.bootLegacyPanel = document.getElementById("boot-legacy-panel");
    U.bootLegacyConfirm = document.getElementById("boot-legacy-confirm");
    U.bootLegacyPreview = document.getElementById("boot-legacy-preview");
    U.bootSetupForm = document.getElementById("boot-setup-form");
    U.bootSetupDisplayName = document.getElementById("boot-setup-display-name");
    U.bootSetupPassword = document.getElementById("boot-setup-password");
    U.bootSetupPasswordConfirm = document.getElementById("boot-setup-password-confirm");
    U.bootUnlockForm = document.getElementById("boot-unlock-form");
    U.bootUnlockPassword = document.getElementById("boot-unlock-password");
    U.securityPanel = document.getElementById("security-panel");
  }

  function isUnlocked() {
    return String(state.status?.mode || "") === "unlocked";
  }

  function setBanner(message = "", { error = false } = {}) {
    if (!U.bootBanner) return;
    const text = String(message || "").trim();
    U.bootBanner.hidden = !text;
    U.bootBanner.textContent = text;
    U.bootBanner.classList.toggle("is-error", Boolean(text && error));
  }

  function maybeCreateIcons() {
    if (window.lucide && typeof window.lucide.createIcons === "function") {
      window.lucide.createIcons();
    }
  }

  function notifyUnlocked() {
    if (state.unlockedInitDispatched) return;
    state.unlockedInitDispatched = true;
    window.dispatchEvent(new CustomEvent("g3ku:boot-unlocked", { detail: state.status || {} }));
  }

  function renderSecurityView() {
    if (!U.securityPanel) return;
    if (!isUnlocked()) {
      U.securityPanel.innerHTML = '<div class="empty-state">项目处于锁定状态，解锁后可管理安全设置。</div>';
      return;
    }
    const name = String(state.status?.active_realm_display_name || "Secret Realm");
    const realmCount = Number(state.status?.realm_count || 0);
    U.securityPanel.innerHTML = `
      <div class="security-grid">
        <section class="security-card">
          <h2>当前分区</h2>
          <p class="subtitle">当前已解锁分区名称会显示在这里，可直接重命名。</p>
          <label class="resource-field">
            <span class="resource-field-label">分区名称</span>
            <input id="security-display-name" class="resource-search" type="text" value="${name.replace(/"/g, "&quot;")}">
          </label>
          <div class="security-actions">
            <button id="security-rename-btn" class="toolbar-btn success" type="button">保存名称</button>
            <button id="security-lock-btn" class="toolbar-btn ghost" type="button">全局锁定</button>
          </div>
        </section>
        <section class="security-card">
          <h2>追加口令分区</h2>
          <p class="subtitle">新增一个空的秘密分区，创建后不会自动切换当前分区。</p>
          <label class="resource-field">
            <span class="resource-field-label">新分区名称</span>
            <input id="security-new-display-name" class="resource-search" type="text" placeholder="例如 备用分区">
          </label>
          <label class="resource-field">
            <span class="resource-field-label">新口令</span>
            <input id="security-new-password" class="resource-search" type="password" autocomplete="new-password">
          </label>
          <label class="resource-field">
            <span class="resource-field-label">确认新口令</span>
            <input id="security-new-password-confirm" class="resource-search" type="password" autocomplete="new-password">
          </label>
          <div class="security-actions">
            <button id="security-create-realm-btn" class="toolbar-btn success" type="button">创建分区</button>
          </div>
        </section>
      </div>
      <section class="security-card">
        <h2>危险操作</h2>
        <p class="subtitle">当前共有 ${realmCount} 个秘密分区。销毁会删除全部口令分区和所有秘密覆盖层，且无法恢复。</p>
        <div class="security-actions">
          <button id="security-destroy-btn" class="toolbar-btn danger" type="button">销毁全部秘密分区</button>
        </div>
      </section>
    `;

    document.getElementById("security-rename-btn")?.addEventListener("click", async () => {
      try {
        await ApiClient.renameBootstrapRealm({
          display_name: document.getElementById("security-display-name")?.value || "",
        });
        await refreshStatus({ silent: true });
      } catch (error) {
        window.alert(error.message || "保存名称失败");
      }
    });

    document.getElementById("security-lock-btn")?.addEventListener("click", async () => {
      try {
        await ApiClient.lockBootstrap();
        window.location.reload();
      } catch (error) {
        window.alert(error.message || "锁定失败");
      }
    });

    document.getElementById("security-create-realm-btn")?.addEventListener("click", async () => {
      try {
        await ApiClient.createBootstrapRealm({
          display_name: document.getElementById("security-new-display-name")?.value || "",
          password: document.getElementById("security-new-password")?.value || "",
          password_confirm: document.getElementById("security-new-password-confirm")?.value || "",
        });
        await refreshStatus({ silent: true });
      } catch (error) {
        window.alert(error.message || "创建分区失败");
      }
    });

    document.getElementById("security-destroy-btn")?.addEventListener("click", async () => {
      const confirmText = String(state.status?.destroy_confirm_text || "");
      const typed = window.prompt(`请输入确认文本以继续：\n${confirmText}`, "");
      if (typed == null) return;
      try {
        await ApiClient.destroyAllBootstrapSecrets({ confirm_text: typed });
        window.location.reload();
      } catch (error) {
        window.alert(error.message || "销毁失败");
      }
    });
  }

  function renderStatus() {
    const mode = String(state.status?.mode || "setup");
    if (U.bootShell) U.bootShell.hidden = isUnlocked();
    if (U.appLayout) U.appLayout.classList.toggle("boot-hidden", !isUnlocked());
    if (U.bootLegacyPanel) {
      const showLegacy = mode === "setup" && Boolean(state.status?.legacy_detected);
      U.bootLegacyPanel.hidden = !showLegacy;
      if (showLegacy && U.bootLegacyPreview) {
        const preview = state.status?.legacy_preview || null;
        U.bootLegacyPreview.textContent = preview ? JSON.stringify(preview, null, 2) : "";
      }
    }
    if (U.bootSetupForm) U.bootSetupForm.hidden = mode !== "setup";
    if (U.bootUnlockForm) U.bootUnlockForm.hidden = mode !== "locked";
    if (U.bootTitle) U.bootTitle.textContent = mode === "setup" ? "初始化秘密分区" : (mode === "locked" ? "项目已锁定" : "项目解锁");
    if (U.bootSubtitle) {
      U.bootSubtitle.textContent = mode === "setup"
        ? "为项目设置第一个口令分区。完成后系统会进入主界面。"
        : (mode === "locked"
          ? "输入已有口令后，系统才会启动后台能力并进入主界面。"
          : "项目已解锁。");
    }
    renderSecurityView();
    maybeCreateIcons();
    if (isUnlocked()) notifyUnlocked();
  }

  async function refreshStatus({ silent = false } = {}) {
    try {
      state.status = await ApiClient.getBootstrapStatus();
      renderStatus();
      if (!silent) setBanner("");
    } catch (error) {
      setBanner(error.message || "启动状态加载失败", { error: true });
    }
  }

  async function handleSetup(event) {
    event.preventDefault();
    try {
      state.busy = true;
      setBanner("");
      state.status = await ApiClient.setupBootstrap({
        display_name: U.bootSetupDisplayName?.value || "",
        password: U.bootSetupPassword?.value || "",
        password_confirm: U.bootSetupPasswordConfirm?.value || "",
        confirm_legacy_reset: Boolean(U.bootLegacyConfirm?.checked),
      });
      renderStatus();
    } catch (error) {
      setBanner(error.message || "初始化失败", { error: true });
    } finally {
      state.busy = false;
    }
  }

  async function handleUnlock(event) {
    event.preventDefault();
    try {
      state.busy = true;
      setBanner("");
      state.status = await ApiClient.unlockBootstrap({
        password: U.bootUnlockPassword?.value || "",
      });
      renderStatus();
    } catch (error) {
      setBanner(error.message || "解锁失败", { error: true });
    } finally {
      state.busy = false;
    }
  }

  function bind() {
    U.bootSetupForm?.addEventListener("submit", handleSetup);
    U.bootUnlockForm?.addEventListener("submit", handleUnlock);
  }

  document.addEventListener("DOMContentLoaded", () => {
    refs();
    bind();
    void refreshStatus();
  });

  window.G3kuBoot = {
    isUnlocked,
    refreshStatus,
    renderSecurityView,
    getStatus: () => state.status,
  };
})();
