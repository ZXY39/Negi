(() => {
  const state = {
    status: null,
    unlockedInitDispatched: false,
    busy: false,
  };

  const U = {};
  const TEXT = {
    titleSetup: "\u521d\u59cb\u5316\u9879\u76ee\u53e3\u4ee4",
    titleLocked: "\u9879\u76ee\u89e3\u9501",
    titleUnlocked: "\u9879\u76ee\u5df2\u89e3\u9501",
    subtitleSetup: "\u4e3a\u9879\u76ee\u8bbe\u7f6e\u552f\u4e00\u53e3\u4ee4\u3002\u5b8c\u6210\u540e\u7cfb\u7edf\u4f1a\u8fdb\u5165\u4e3b\u754c\u9762\u3002",
    subtitleUnlocked: "\u9879\u76ee\u5df2\u89e3\u9501\u3002",
    initIdle: "\u521d\u59cb\u5316\u5e76\u8fdb\u5165\u9879\u76ee",
    initBusy: "\u521d\u59cb\u5316\u4e2d...",
    unlockIdle: "\u89e3\u9501\u9879\u76ee",
    unlockBusy: "\u89e3\u9501\u4e2d...",
    setupLoading: "\u6b63\u5728\u521d\u59cb\u5316\u9879\u76ee\u5e76\u542f\u52a8\u8fd0\u884c\u65f6\uff0c\u8bf7\u7a0d\u5019...",
    unlockLoading: "\u6b63\u5728\u9a8c\u8bc1\u53e3\u4ee4\u5e76\u542f\u52a8\u8fd0\u884c\u65f6\uff0c\u8bf7\u7a0d\u5019...",
    unlockRetrying: "\u6b63\u5728\u8fde\u63a5\u8fd0\u884c\u65f6\uff0c\u6b63\u5728\u81ea\u52a8\u91cd\u8bd5...",
    unlockBootstrapping: "\u8fd0\u884c\u65f6\u6b63\u5728\u542f\u52a8\uff0c\u8bf7\u518d\u7a0d\u5019\u4e00\u4e0b...",
    refreshFailed: "\u542f\u52a8\u72b6\u6001\u52a0\u8f7d\u5931\u8d25",
    setupFailed: "\u521d\u59cb\u5316\u5931\u8d25",
    unlockFailed: "\u89e3\u9501\u5931\u8d25",
    dataDirPlaceholder: "\u9ed8\u8ba4\uff1a",
  };

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
    U.bootSetupDataDir = document.getElementById("boot-setup-data-dir");
    U.bootSetupSubmit = document.getElementById("boot-setup-submit");
    U.bootUnlockForm = document.getElementById("boot-unlock-form");
    U.bootUnlockPassword = document.getElementById("boot-unlock-password");
    U.bootRememberUnlock = document.getElementById("boot-remember-unlock");
    U.bootUnlockSubmit = document.getElementById("boot-unlock-submit");
    if (U.bootBanner) {
      U.bootBanner.setAttribute("role", "status");
      U.bootBanner.setAttribute("aria-live", "polite");
    }
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

  function setDisabled(element, disabled) {
    if (!element) return;
    element.disabled = Boolean(disabled);
  }

  function renderBusyState() {
    const mode = String(state.status?.mode || "setup");
    const setupBusy = state.busy && mode === "setup";
    const unlockBusy = state.busy && mode === "locked";

    setDisabled(U.bootSetupPassword, setupBusy);
    setDisabled(U.bootSetupPasswordConfirm, setupBusy);
    setDisabled(U.bootSetupDataDir, setupBusy);
    setDisabled(U.bootLegacyConfirm, setupBusy);
    setDisabled(U.bootUnlockPassword, unlockBusy);
    setDisabled(U.bootRememberUnlock, unlockBusy);

    if (U.bootSetupSubmit) {
      U.bootSetupSubmit.disabled = setupBusy;
      U.bootSetupSubmit.classList.toggle("is-busy", setupBusy);
      U.bootSetupSubmit.setAttribute("aria-busy", setupBusy ? "true" : "false");
      U.bootSetupSubmit.textContent = setupBusy ? TEXT.initBusy : TEXT.initIdle;
    }

    if (U.bootUnlockSubmit) {
      U.bootUnlockSubmit.disabled = unlockBusy;
      U.bootUnlockSubmit.classList.toggle("is-busy", unlockBusy);
      U.bootUnlockSubmit.setAttribute("aria-busy", unlockBusy ? "true" : "false");
      U.bootUnlockSubmit.textContent = unlockBusy ? TEXT.unlockBusy : TEXT.unlockIdle;
    }
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
    if (U.bootSetupDataDir && mode === "setup") {
      const dataRoot = state.status?.data_root || {};
      if (dataRoot.source && dataRoot.source !== "default") {
        U.bootSetupDataDir.value = String(dataRoot.data_root || "");
      }
      U.bootSetupDataDir.placeholder = `${TEXT.dataDirPlaceholder}${dataRoot.default_root || ""}`;
    }
    if (U.bootUnlockForm) U.bootUnlockForm.hidden = mode !== "locked";
    if (U.bootRememberUnlock && mode === "locked") {
      U.bootRememberUnlock.checked = Boolean(state.status?.auto_unlock);
    }
    if (U.bootTitle) {
      U.bootTitle.textContent = mode === "setup"
        ? TEXT.titleSetup
        : (mode === "locked" ? TEXT.titleLocked : TEXT.titleUnlocked);
    }
    if (U.bootSubtitle) {
      // 解锁界面只留「请输入密码」一条提示，解释性副标题不再显示。
      const subtitle = mode === "setup" ? TEXT.subtitleSetup : TEXT.subtitleUnlocked;
      U.bootSubtitle.hidden = mode === "locked";
      U.bootSubtitle.textContent = subtitle;
    }
    maybeCreateIcons();
    renderBusyState();
    if (isUnlocked()) notifyUnlocked();
  }

  async function refreshStatus({ silent = false } = {}) {
    try {
      state.status = await ApiClient.getBootstrapStatus();
      renderStatus();
      if (!silent && !state.busy) setBanner("");
    } catch (error) {
      setBanner(error.message || TEXT.refreshFailed, { error: true });
    }
  }

  async function handleSetup(event) {
    event.preventDefault();
    if (state.busy) return;
    try {
      state.busy = true;
      renderBusyState();
      setBanner(TEXT.setupLoading);
      state.status = await ApiClient.setupBootstrap({
        password: U.bootSetupPassword?.value || "",
        password_confirm: U.bootSetupPasswordConfirm?.value || "",
        data_dir: String(U.bootSetupDataDir?.value || "").trim(),
        confirm_legacy_reset: Boolean(U.bootLegacyConfirm?.checked),
      });
      renderStatus();
      setBanner("");
    } catch (error) {
      setBanner(error.message || TEXT.setupFailed, { error: true });
    } finally {
      state.busy = false;
      renderBusyState();
    }
  }

  async function handleUnlock(event) {
    event.preventDefault();
    if (state.busy) return;
    try {
      state.busy = true;
      renderBusyState();
      setBanner(TEXT.unlockLoading);
      state.status = await ApiClient.unlockBootstrap({
        password: U.bootUnlockPassword?.value || "",
        remember: Boolean(U.bootRememberUnlock?.checked),
      }, {
        onRetry: () => {
          if (!state.busy) return;
          setBanner(TEXT.unlockRetrying);
        },
        onProgress: ({ runtimeBootstrapping = false } = {}) => {
          if (!state.busy) return;
          setBanner(runtimeBootstrapping ? TEXT.unlockBootstrapping : TEXT.unlockRetrying);
        },
      });
      if (U.bootUnlockPassword) U.bootUnlockPassword.value = "";
      renderStatus();
      setBanner("");
    } catch (error) {
      setBanner(error.message || TEXT.unlockFailed, { error: true });
    } finally {
      state.busy = false;
      renderBusyState();
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
