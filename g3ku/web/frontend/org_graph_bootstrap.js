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
    U.bootSetupPassword = document.getElementById("boot-setup-password");
    U.bootSetupPasswordConfirm = document.getElementById("boot-setup-password-confirm");
    U.bootUnlockForm = document.getElementById("boot-unlock-form");
    U.bootUnlockPassword = document.getElementById("boot-unlock-password");
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
    if (U.bootTitle) U.bootTitle.textContent = mode === "setup" ? "初始化项目口令" : (mode === "locked" ? "项目解锁" : "项目已解锁");
    if (U.bootSubtitle) {
      U.bootSubtitle.textContent = mode === "setup"
        ? "为项目设置唯一口令。完成后系统会进入主界面。"
        : (mode === "locked"
          ? "输入口令后才能进入项目主界面与启动后台能力。"
          : "项目已解锁。");
    }
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
    getStatus: () => state.status,
  };
})();
