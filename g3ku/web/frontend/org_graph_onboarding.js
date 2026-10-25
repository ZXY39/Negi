(() => {
    "use strict";

    const PERSONA_CHAIN_TOAST = "为了使人设生效，主agent和记忆agent模型链不能为空，点击保存后生效";
    const DRAG_TOAST_TITLE = "可拖动至 Agent";
    const DRAG_TOAST_TEXT = "把模型或负载均衡组拖到右侧任一 Agent 模型链即可加入。";
    const ONBOARDING_DRAG_TOAST_TEXT = "把模型或负载均衡组拖到右侧任一 Agent 模型链即可加入；主Agent和记忆Agent都需要至少一个角色。";

    const state = {
        started: false,
        active: false,
        status: null,
        photos: [],
        uploadedNames: [],
        personaDescription: "",
        personaToastShown: false,
        dragToastActive: false,
        dragToastTimer: null,
        busy: false,
    };

    const U = {};

    function refs() {
        U.onboardingBackdrop = document.getElementById("persona-onboarding-backdrop");
        U.onboardingDialog = document.getElementById("persona-onboarding-dialog");
        U.onboardingPhotoInput = document.getElementById("persona-onboarding-photo-input");
        U.onboardingPhotoPick = document.getElementById("persona-onboarding-photo-pick");
        U.onboardingPhotoStatus = document.getElementById("persona-onboarding-photo-status");
        U.onboardingPhotoGrid = document.getElementById("persona-onboarding-photo-grid");
        U.onboardingDescription = document.getElementById("persona-onboarding-description");
        U.onboardingError = document.getElementById("persona-onboarding-error");
        U.onboardingSkip = document.getElementById("persona-onboarding-skip-btn");
        U.onboardingNext = document.getElementById("persona-onboarding-next-btn");
        U.personaBackdrop = document.getElementById("persona-dialog-backdrop");
        U.personaDialog = document.getElementById("persona-dialog");
        U.personaDescription = document.getElementById("persona-dialog-description");
        U.personaDescriptionEmpty = document.getElementById("persona-dialog-description-empty");
        U.personaDescriptionInput = document.getElementById("persona-dialog-description-input");
        U.personaDescriptionActions = document.getElementById("persona-dialog-description-actions");
        U.personaDescriptionSave = document.getElementById("persona-dialog-description-save");
        U.personaDescriptionCancel = document.getElementById("persona-dialog-description-cancel");
        U.personaPhotoGrid = document.getElementById("persona-dialog-photo-grid");
        U.personaClose = document.getElementById("persona-dialog-close-btn");
        U.personaCloseFooter = document.getElementById("persona-dialog-close");
        U.settingsNavButton = document.getElementById("project-settings-btn");
        U.settingsPersonaButton = document.getElementById("project-settings-open-persona-btn");
        U.personaNavDot = document.getElementById("persona-nav-dot");
        U.personaButtonDot = document.getElementById("persona-button-dot");
    }

    function hasPersonaInput() {
        const description = String(U.onboardingDescription?.value || "").trim();
        const pendingPhotos = Array.isArray(state.status?.photos) ? state.status.photos : [];
        return Boolean(description || state.uploadedNames.length || pendingPhotos.length || state.status?.has_persona_input);
    }

    function setOnboardingError(message = "") {
        if (!U.onboardingError) return;
        const text = String(message || "").trim();
        U.onboardingError.hidden = !text;
        U.onboardingError.textContent = text;
    }

    function setPhotoStatus(message = "", { error = false } = {}) {
        if (!U.onboardingPhotoStatus) return;
        U.onboardingPhotoStatus.textContent = String(message || "");
        U.onboardingPhotoStatus.classList.toggle("is-error", Boolean(error));
    }

    function openBackdrop(backdrop, dialog) {
        if (!backdrop) return;
        backdrop.hidden = false;
        backdrop.classList.add("is-open");
        if (dialog && typeof dialog.focus === "function") dialog.focus();
    }

    function closeBackdrop(backdrop) {
        if (!backdrop) return;
        backdrop.hidden = true;
        backdrop.classList.remove("is-open");
    }

    function renderPhotoGrid(container, photos, emptyText = "还没有照片") {
        if (!container) return;
        const rows = Array.isArray(photos) ? photos : [];
        container.innerHTML = "";
        if (!rows.length) {
            const empty = document.createElement("div");
            empty.className = "empty-state compact persona-photo-empty";
            empty.textContent = emptyText;
            container.appendChild(empty);
            return;
        }
        rows.forEach((item) => {
            const button = document.createElement("button");
            button.type = "button";
            button.className = "persona-photo-tile";
            button.dataset.personaPhotoUrl = String(item.url || "");
            button.dataset.personaPhotoTitle = String(item.name || "照片");
            const image = document.createElement("img");
            image.src = String(item.thumb_url || item.url || "");
            image.alt = String(item.name || "照片");
            image.loading = "lazy";
            const label = document.createElement("span");
            label.textContent = String(item.name || "");
            button.appendChild(image);
            button.appendChild(label);
            container.appendChild(button);
        });
    }

    function openPhotoViewer(container, event) {
        const tile = event.target.closest("[data-persona-photo-url]");
        if (!tile || !container?.contains(tile)) return;
        const url = String(tile.dataset.personaPhotoUrl || "");
        if (!url) return;
        openMediaViewer({
            mode: "image",
            url,
            title: String(tile.dataset.personaPhotoTitle || "照片"),
        });
    }

    function setPersonaEmpty(empty) {
        [U.personaNavDot, U.personaButtonDot].forEach((dot) => {
            if (dot) dot.hidden = !empty;
        });
    }

    async function refreshPersonaIndicator() {
        try {
            const data = await ApiClient.getPersona();
            state.personaDescription = String(data?.description || "").trim();
            setPersonaEmpty(!state.personaDescription);
        } catch (error) {
            void error;
        }
    }

    function renderPersonaDescription(description) {
        const text = String(description || "").trim();
        state.personaDescription = text;
        const empty = !text;
        if (U.personaDescription) {
            U.personaDescription.hidden = empty;
            U.personaDescription.textContent = text;
        }
        if (U.personaDescriptionEmpty) U.personaDescriptionEmpty.hidden = !empty;
        if (U.personaDescriptionInput) {
            U.personaDescriptionInput.hidden = true;
            U.personaDescriptionInput.value = "";
        }
        if (U.personaDescriptionActions) U.personaDescriptionActions.hidden = true;
        setPersonaEmpty(empty);
    }

    function startPersonaDescriptionEdit() {
        if (state.personaDescription) return;
        if (U.personaDescription) U.personaDescription.hidden = true;
        if (U.personaDescriptionEmpty) U.personaDescriptionEmpty.hidden = true;
        if (U.personaDescriptionInput) {
            U.personaDescriptionInput.hidden = false;
            U.personaDescriptionInput.value = "";
            U.personaDescriptionInput.focus();
        }
        if (U.personaDescriptionActions) U.personaDescriptionActions.hidden = false;
    }

    function cancelPersonaDescriptionEdit() {
        renderPersonaDescription(state.personaDescription);
    }

    async function savePersonaDescription() {
        const text = String(U.personaDescriptionInput?.value || "").trim();
        if (!text) {
            showToast({ title: "人设描述不能为空", text: "请先输入内容再保存", kind: "error" });
            return;
        }
        if (U.personaDescriptionSave) U.personaDescriptionSave.disabled = true;
        try {
            const result = await ApiClient.savePersona(text);
            renderPersonaDescription(result?.description || text);
            showToast({ title: "人设已保存", text: "下一次会话会按新的人设说话", kind: "success" });
        } catch (error) {
            showToast({
                title: "人设保存失败",
                text: error?.message || "请稍后重试",
                kind: "error",
            });
        } finally {
            if (U.personaDescriptionSave) U.personaDescriptionSave.disabled = false;
        }
    }

    function showPersonaChainToast() {
        showToast({
            title: "人设暂未生效",
            text: PERSONA_CHAIN_TOAST,
            kind: "info",
            durationMs: 6000,
        });
        state.personaToastShown = true;
    }

    function showDragToast() {
        showToast({
            title: DRAG_TOAST_TITLE,
            text: DRAG_TOAST_TEXT,
            kind: "info",
            durationMs: 2400,
        });
    }

    function chainsReadyForDragToast() {
        if (typeof S === "undefined" || !S.modelCatalog) return false;
        const source = S.modelCatalog.roleEditing ? S.modelCatalog.roleDrafts : S.modelCatalog.roles;
        const ceoChain = Array.isArray(source?.ceo) ? source.ceo.filter(Boolean) : [];
        const memoryChain = Array.isArray(source?.memory) ? source.memory.filter(Boolean) : [];
        return ceoChain.length > 0 && memoryChain.length > 0;
    }

    function closeDragToast() {
        state.dragToastActive = false;
        if (state.dragToastTimer) {
            window.clearInterval(state.dragToastTimer);
            state.dragToastTimer = null;
        }
        if (typeof closeToast === "function") closeToast();
    }

    function syncDragToast() {
        if (!state.dragToastActive) return;
        if (chainsReadyForDragToast()) closeDragToast();
    }

    function showPersistentDragToast() {
        if (state.dragToastActive) return;
        state.dragToastActive = true;
        showToast({
            title: DRAG_TOAST_TITLE,
            text: ONBOARDING_DRAG_TOAST_TEXT,
            kind: "info",
            persistent: true,
            closable: false,
        });
        state.dragToastTimer = window.setInterval(syncDragToast, 500);
    }

    function enterChainEdit({ delayDragToastMs = 0 } = {}) {
        if (typeof startModelRoleEditing === "function") startModelRoleEditing();
        if (state.active) {
            showPersistentDragToast();
            return;
        }
        if (delayDragToastMs > 0) {
            window.setTimeout(showDragToast, delayDragToastMs);
        } else {
            showDragToast();
        }
    }

    function openPersonaStep(status) {
        refs();
        if (U.onboardingDescription) {
            U.onboardingDescription.value = String(status?.description || "");
        }
        setOnboardingError("");
        setPhotoStatus("");
        renderPhotoGrid(U.onboardingPhotoGrid, state.photos);
        openBackdrop(U.onboardingBackdrop, U.onboardingDialog);
    }

    function closePersonaStep() {
        closeBackdrop(U.onboardingBackdrop);
    }

    async function uploadSelectedPhotos(files) {
        const selected = [...files].filter(Boolean);
        if (!selected.length) return;
        setOnboardingError("");
        setPhotoStatus(`正在上传 ${selected.length} 张…`);
        if (U.onboardingPhotoPick) U.onboardingPhotoPick.disabled = true;
        try {
            const items = await ApiClient.uploadPersonaPhotos(selected);
            items.forEach((item) => {
                state.photos.push(item);
                if (item?.name && !state.uploadedNames.includes(item.name)) {
                    state.uploadedNames.push(item.name);
                }
                if (state.status) {
                    state.status.photos = [...state.uploadedNames];
                    state.status.has_persona_input = true;
                }
            });
            renderPhotoGrid(U.onboardingPhotoGrid, state.photos);
            setPhotoStatus(`已上传 ${items.length} 张`);
        } catch (error) {
            setPhotoStatus(error?.message || "上传失败", { error: true });
        } finally {
            if (U.onboardingPhotoPick) U.onboardingPhotoPick.disabled = false;
            if (U.onboardingPhotoInput) U.onboardingPhotoInput.value = "";
        }
    }

    function markPersonaInput() {
        const description = String(U.onboardingDescription?.value || "").trim();
        if (!state.status) state.status = {};
        state.status.description = description;
        state.status.persona_step_done = true;
        state.status.has_persona_input = Boolean(description || state.uploadedNames.length);
    }

    async function submitPersonaStep() {
        if (state.busy) return;
        state.busy = true;
        setOnboardingError("");
        const description = String(U.onboardingDescription?.value || "").trim();
        try {
            if (description) {
                await ApiClient.saveOnboardingPersona(description);
            } else {
                await ApiClient.saveOnboardingPersona("");
            }
            await refreshPersonaIndicator();
            markPersonaInput();
            closePersonaStep();
            if (hasPersonaInput()) showPersonaChainToast();
            await openModelStep({ resumed: true });
        } catch (error) {
            setOnboardingError(error?.message || "保存失败");
        } finally {
            state.busy = false;
        }
    }

    async function skipPersonaStep() {
        if (state.busy) return;
        state.busy = true;
        setOnboardingError("");
        try {
            await ApiClient.skipOnboardingPersona();
            if (U.onboardingDescription) U.onboardingDescription.value = "";
            if (state.status) {
                state.status.description = "";
                state.status.persona_step_done = true;
                state.status.has_persona_input = state.uploadedNames.length > 0;
            }
            closePersonaStep();
            if (hasPersonaInput()) showPersonaChainToast();
            await openModelStep({ resumed: true });
        } catch (error) {
            setOnboardingError(error?.message || "跳过失败");
        } finally {
            state.busy = false;
        }
    }

    function savedChainsReadyForOnboarding() {
        if (typeof S === "undefined" || !S.modelCatalog) return false;
        const roles = S.modelCatalog.roles || {};
        const required = ["ceo", "execution", "inspection"].every((scope) => {
            const chain = Array.isArray(roles[scope]) ? roles[scope].filter(Boolean) : [];
            return chain.length > 0;
        });
        if (!required) return false;
        if (!hasPersonaInput()) return true;
        const memoryChain = Array.isArray(roles.memory) ? roles.memory.filter(Boolean) : [];
        return memoryChain.length > 0;
    }

    async function completeOnboardingNow() {
        const hadPersonaInput = hasPersonaInput();
        try {
            const result = await ApiClient.completeOnboarding();
            state.active = false;
            closeDragToast();
            if (hadPersonaInput && Array.isArray(result?.jobs) && result.jobs.length) {
                showToast({
                    title: "人设已提交",
                    text: "记忆 Agent 正在处理",
                    kind: "success",
                });
            }
        } catch (error) {
            showToast({
                title: "人设尚未生效",
                text: error?.message || PERSONA_CHAIN_TOAST,
                kind: "error",
                durationMs: 6000,
            });
            window.setTimeout(() => {
                if (!state.active || chainsReadyForDragToast()) return;
                state.dragToastActive = false;
                showPersistentDragToast();
            }, 6500);
        }
    }

    async function openModelStep({ resumed = false } = {}) {
        if (typeof switchView === "function") switchView("models");
        const hasModels = Boolean(state.status?.has_models);
        if (!hasModels) {
            try {
                if (typeof window.loadModels === "function") await window.loadModels();
                if (typeof window.startCreateModel === "function") window.startCreateModel();
            } catch (error) {
                showToast({
                    title: "模型配置未打开",
                    text: error?.message || "请稍后重试",
                    kind: "error",
                });
            }
            return;
        }
        try {
            if (typeof window.loadModels === "function") await window.loadModels();
        } catch (error) {
            void error;
        }
        if (savedChainsReadyForOnboarding()) {
            await completeOnboardingNow();
            return;
        }
        enterChainEdit({ delayDragToastMs: state.personaToastShown && resumed ? 5200 : 0 });
    }

    function validateChainsBeforeSave() {
        if (!state.active) return "";
        if (!hasPersonaInput()) return "";
        const drafts = (typeof S !== "undefined" && S.modelCatalog?.roleDrafts) || {};
        const ceoChain = Array.isArray(drafts.ceo) ? drafts.ceo.filter(Boolean) : [];
        const memoryChain = Array.isArray(drafts.memory) ? drafts.memory.filter(Boolean) : [];
        if (ceoChain.length && memoryChain.length) return "";
        return PERSONA_CHAIN_TOAST;
    }

    async function onProviderSaved() {
        if (!state.active) return;
        if (state.status) state.status.has_models = true;
        if (typeof switchView === "function") switchView("models");
        enterChainEdit();
    }

    async function onChainsSaved() {
        if (!state.active) return;
        await completeOnboardingNow();
    }

    async function openPersonaDialog() {
        refs();
        try {
            const data = await ApiClient.getPersona();
            renderPersonaDescription(data?.description);
            renderPhotoGrid(
                U.personaPhotoGrid,
                data?.photos || [],
                "图片为空，如果需要增加，请在会话中发送",
            );
            openBackdrop(U.personaBackdrop, U.personaDialog);
        } catch (error) {
            showToast({
                title: "读取人设失败",
                text: error?.message || "请稍后重试",
                kind: "error",
            });
        }
    }

    function closePersonaDialog() {
        closeBackdrop(U.personaBackdrop);
        U.settingsPersonaButton?.focus?.();
    }

    async function maybeStart() {
        if (state.started) return;
        if (window.G3kuBoot && typeof window.G3kuBoot.isUnlocked === "function" && !window.G3kuBoot.isUnlocked()) {
            return;
        }
        state.started = true;
        try {
            const status = await ApiClient.getOnboardingStatus();
            state.status = status;
            void refreshPersonaIndicator();
            if (!status?.should_run) return;
            state.active = true;
            state.photos = Array.isArray(status.photo_album) ? status.photo_album : [];
            state.uploadedNames = Array.isArray(status.photos) ? [...status.photos] : [];
            if (!status.persona_step_done) {
                openPersonaStep(status);
            } else {
                if (hasPersonaInput() && !state.personaToastShown) showPersonaChainToast();
                await openModelStep({ resumed: true });
            }
        } catch (error) {
            state.started = false;
            void error;
        }
    }

    function bind() {
        refs();
        U.onboardingPhotoPick?.addEventListener("click", () => U.onboardingPhotoInput?.click());
        U.onboardingPhotoInput?.addEventListener("change", (event) => {
            void uploadSelectedPhotos(event.target.files || []);
        });
        U.onboardingNext?.addEventListener("click", () => void submitPersonaStep());
        U.onboardingSkip?.addEventListener("click", () => void skipPersonaStep());
        // The onboarding modal is dismissed only by the explicit 跳过 / 下一步 actions;
        // a backdrop click must not discard the first-run step.
        U.onboardingBackdrop?.addEventListener("click", (event) => {
            if (event.target === U.onboardingBackdrop) event.stopPropagation();
        });
        U.onboardingPhotoGrid?.addEventListener("click", (event) => openPhotoViewer(U.onboardingPhotoGrid, event));

        U.settingsPersonaButton?.addEventListener("click", () => void openPersonaDialog());
        U.settingsNavButton?.addEventListener("click", () => void refreshPersonaIndicator());
        U.personaDescriptionEmpty?.addEventListener("click", startPersonaDescriptionEdit);
        U.personaDescriptionSave?.addEventListener("click", () => void savePersonaDescription());
        U.personaDescriptionCancel?.addEventListener("click", cancelPersonaDescriptionEdit);
        U.personaClose?.addEventListener("click", closePersonaDialog);
        U.personaCloseFooter?.addEventListener("click", closePersonaDialog);
        U.personaBackdrop?.addEventListener("click", (event) => {
            if (event.target === U.personaBackdrop) closePersonaDialog();
        });
        U.personaPhotoGrid?.addEventListener("click", (event) => openPhotoViewer(U.personaPhotoGrid, event));

        document.addEventListener("keydown", (event) => {
            if (event.key !== "Escape") return;
            if (U.personaBackdrop && !U.personaBackdrop.hidden) {
                event.preventDefault();
                event.stopPropagation();
                closePersonaDialog();
                return;
            }
            if (U.onboardingBackdrop && !U.onboardingBackdrop.hidden) {
                // Escape must not dismiss the first-run modal; only its buttons do.
                event.preventDefault();
                event.stopPropagation();
            }
        }, true);
    }

    window.G3kuOnboarding = {
        openPersonaDialog,
        onProviderSaved,
        onChainsSaved,
        validateChainsBeforeSave,
    };

    document.addEventListener("DOMContentLoaded", () => {
        bind();
        void maybeStart();
    });
    window.addEventListener("g3ku:boot-unlocked", () => void maybeStart());
})();
