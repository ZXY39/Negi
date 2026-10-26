"use strict";

// 表情包管理页。数据只有一份来源：/api/resources/stickers/*。目录即清单，所以
// 页面不做任何推断——列表里没有的包就是不在了，分类里没有的图就是删掉了。
//
// 每个写操作都由服务端返回刷新后的整包快照，applyStickerPack() 直接替换本地状态，
// 不补发 GET：既省一次往返，也不会读到扫描缓存 TTL 内的旧值。

const STICKER_DEFAULT_PER_CATEGORY = 12;

function stickerLimits() {
    const settings = StickersState.settings || {};
    return {
        theme: Number(settings.theme_max_chars) || 60,
        description: Number(settings.description_max_chars) || 20,
        perReply: Number(settings.max_per_reply_limit) || 4,
        uploadFiles: Number(settings.max_upload_files) || 20,
    };
}

function stickerErrorText(error) {
    const data = error && typeof error === "object" ? error.data : null;
    if (data && typeof data.message === "string" && data.message.trim()) return data.message.trim();
    return String(error?.message || "未知错误");
}

function stickerFail(title, error) {
    addNotice({ kind: "sticker_failed", title, text: stickerErrorText(error) });
}

function stickerKey(packName, categoryName) {
    return `${packName}/${categoryName}`;
}

function stickerSkeleton(pack) {
    return { ...pack, categories: (pack.categories || []).map((category) => ({ ...category, images: [] })) };
}

function stickerFilteredPacks() {
    const query = String(StickersState.search || "").trim().toLowerCase();
    if (!query) return StickersState.packs;
    return StickersState.packs.filter((pack) => {
        const haystack = [pack.key, pack.dir_name, pack.theme]
            .concat((pack.categories || []).map((category) => `${category.key} ${category.description || ""}`))
            .join(" ")
            .toLowerCase();
        return haystack.includes(query);
    });
}

function stickerSetBusy(on, label = "") {
    StickersState.busy = !!on;
    if (U.stickerDetail) U.stickerDetail.classList.toggle("is-busy", !!on);
    if (U.stickerBusyLabel) {
        U.stickerBusyLabel.textContent = on ? label || "处理中…" : "";
        U.stickerBusyLabel.hidden = !on;
    }
}

function applyStickerPack(item, requestedDirName = "") {
    if (!item || !item.dir_name) return;
    StickersState.detail = item;
    StickersState.selected = item.dir_name;
    const nextSkeleton = stickerSkeleton(item);
    const index = StickersState.packs.findIndex(
        (pack) => pack.dir_name === requestedDirName || pack.dir_name === item.dir_name,
    );
    if (index >= 0) StickersState.packs.splice(index, 1, nextSkeleton);
    else StickersState.packs.push(nextSkeleton);
    StickersState.packs.sort((left, right) => left.dir_name.localeCompare(right.dir_name));
    renderStickerPacks();
    renderStickerDetail();
}

function dropStickerPack(dirName) {
    StickersState.detail = null;
    StickersState.expanded = {};
    StickersState.packs = StickersState.packs.filter((pack) => pack.dir_name !== dirName);
    const next = StickersState.packs[0];
    StickersState.selected = next ? next.dir_name : "";
    renderStickerPacks();
    if (next) void loadStickerDetail(next.dir_name);
    else renderStickerDetail();
}

async function runStickerMutation({ label, request, onSuccess }) {
    if (StickersState.busy) return;
    stickerSetBusy(true, label);
    try {
        const payload = await request();
        if (typeof onSuccess === "function") await onSuccess(payload || {});
    } catch (error) {
        if (isAbortLike(error)) return;
        stickerFail(label, error);
    } finally {
        stickerSetBusy(false);
    }
}

// ---------------------------------------------------------------- render

function renderStickerPacks() {
    if (!U.stickerPackList) return;
    const packs = stickerFilteredPacks();
    const total = StickersState.packs.length;
    if (!total) {
        U.stickerPackList.innerHTML = '<div class="empty-state">还没有表情包。把图片文件夹发给 agent，说「导入这个表情包」，它会读 meme skill 完成导入与分类。</div>';
    } else if (!packs.length) {
        U.stickerPackList.innerHTML = '<div class="empty-state">没有匹配的表情包。</div>';
    } else {
        U.stickerPackList.innerHTML = packs
            .map((pack) => {
                const active = pack.dir_name === StickersState.selected ? " selected" : "";
                const badge = pack.enabled ? "" : '<span class="meta-tag status-disabled">已停用</span>';
                const theme = pack.theme || "（未设置主题）";
                return `<button type="button" class="sticker-pack-item${active}" data-sticker-pack="${esc(pack.dir_name)}">
                    <span class="sticker-pack-item-head"><strong>${esc(pack.key)}</strong>${badge}</span>
                    <span class="sticker-pack-item-theme">${esc(theme)}</span>
                    <span class="sticker-pack-item-meta">${Number(pack.total || 0)} 张 · ${(pack.categories || []).length} 个分类</span>
                </button>`;
            })
            .join("");
    }
    if (U.stickerPackInfo) {
        U.stickerPackInfo.textContent = StickersState.search
            ? `匹配 ${packs.length} / ${total} 个包`
            : `共 ${total} 个包`;
    }
}

function renderStickerEmptyDetail(text) {
    if (U.stickerDetail) U.stickerDetail.innerHTML = `<div class="empty-state">${text}</div>`;
}

function renderStickerDetail() {
    if (!U.stickerDetail) return;
    const pack = StickersState.detail;
    if (!pack) {
        renderStickerEmptyDetail("选择左边的表情包查看图片。");
        return;
    }
    const limits = stickerLimits();
    const badge = pack.enabled ? "" : '<span class="meta-tag status-disabled">已停用</span>';
    const disabledHint = pack.enabled
        ? ""
        : '<p class="sticker-hint">停用的包不进模型的清单，标记也解析不到；启用后下一轮对话生效。</p>';
    const theme = pack.theme || "";
    const sections = [];
    sections.push(`<div class="sticker-pack-head">
        <div class="sticker-pack-title">
            <h2>${esc(pack.key)}</h2>
            ${badge}
            <span class="sticker-pack-total">${Number(pack.total || 0)} 张 · ${(pack.categories || []).length} 个分类</span>
            <span class="sticker-pack-actions">
                <button type="button" class="toolbar-btn ghost small" data-sticker-action="toggle-pack">${pack.enabled ? "停用" : "启用"}</button>
                <button type="button" class="toolbar-btn ghost small" data-sticker-action="delete-pack">删除</button>
            </span>
        </div>
        <label class="sticker-field-row">
            <span class="sticker-field-label">主题</span>
            <input class="resource-search sticker-field-input" id="sticker-theme-input" maxlength="${limits.theme}"
                value="${esc(theme)}" placeholder="一句话说明这套图是什么、什么调性（模型靠它选包）">
            <button type="button" class="toolbar-btn success small" data-sticker-action="save-theme" hidden>保存</button>
        </label>
        ${disabledHint}
    </div>`);
    if (!(pack.categories || []).length) {
        sections.push('<div class="empty-state">这个包还没有分类。新建一个分类，再往里面上传图片。</div>');
    } else {
        sections.push(pack.categories.map((category) => stickerCategoryHtml(pack, category, limits)).join(""));
    }
    sections.push(`<div class="sticker-new-category">
        <input class="resource-search" id="sticker-new-category-key" maxlength="64"
            placeholder="新分类键，如 happy（字母/数字/点/下划线/连字符，中文不能做键）">
        <input class="resource-search" id="sticker-new-category-desc" maxlength="${limits.description}"
            placeholder="什么时候用（可留空）">
        <button type="button" class="toolbar-btn ghost small" data-sticker-action="create-category">新建分类</button>
    </div>`);
    U.stickerDetail.innerHTML = sections.join("");
}

function stickerCategoryHtml(pack, category, limits) {
    const expanded = StickersState.expanded[stickerKey(pack.dir_name, category.dir_name)];
    const images = expanded || category.images || [];
    const badge = category.enabled ? "" : '<span class="meta-tag status-disabled">已停用</span>';
    const hidden = Number(category.count || 0) - images.length;
    const thumbs = images.length
        ? `<div class="sticker-thumb-grid">${images.map((image) => stickerThumbHtml(pack, category, image)).join("")}</div>`
        : '<p class="sticker-hint">这个分类还没有图片。</p>';
    const more = hidden > 0
        ? `<button type="button" class="toolbar-btn ghost small" data-sticker-action="expand-category">查看全部 ${Number(category.count)} 张</button>`
        : "";
    return `<section class="sticker-category-card${category.enabled ? "" : " is-disabled"}" data-sticker-category="${esc(category.dir_name)}">
        <header class="sticker-category-head">
            <div class="sticker-category-title">
                <strong>${esc(category.key)}</strong>
                ${badge}
                <span class="sticker-pack-item-meta">${Number(category.count || 0)} 张</span>
            </div>
            <div class="sticker-category-actions">
                <button type="button" class="toolbar-btn ghost small" data-sticker-action="upload">上传</button>
                <button type="button" class="toolbar-btn ghost small" data-sticker-action="toggle-category">${category.enabled ? "停用" : "启用"}</button>
                <button type="button" class="toolbar-btn ghost small" data-sticker-action="delete-category">删除</button>
            </div>
        </header>
        <label class="sticker-field-row">
            <span class="sticker-field-label">描述</span>
            <input class="resource-search sticker-field-input" data-sticker-description maxlength="${limits.description}"
                value="${esc(category.description || "")}" placeholder="什么时候用，如「开心大笑、庆祝」">
            <button type="button" class="toolbar-btn success small" data-sticker-action="save-description" hidden>保存</button>
        </label>
        ${thumbs}
        ${more}
    </section>`;
}

function stickerThumbHtml(pack, category, image) {
    const title = `${pack.key} / ${category.key}`;
    return `<figure class="sticker-thumb">
        <button type="button" class="sticker-thumb-open" data-sticker-action="preview"
            data-url="${esc(image.url)}" data-name="${esc(image.name)}" data-title="${esc(title)}"
            aria-label="查看 ${esc(image.name)}">
            <img src="${esc(image.url)}" alt="${esc(image.name)}" loading="lazy" decoding="async">
        </button>
        <button type="button" class="sticker-thumb-remove" data-sticker-action="delete-image"
            data-name="${esc(image.name)}" title="删除这张" aria-label="删除 ${esc(image.name)}">×</button>
    </figure>`;
}

function renderStickerSettings() {
    const settings = StickersState.settings;
    if (!settings) return;
    if (U.stickerMaxPerReply) {
        U.stickerMaxPerReply.value = String(Number(settings.max_per_reply) || 0);
        // .resource-select 会被 enhanceResourceSelects() 换成自定义壳，壳上的标签是
        // 快照：只改原生 select.value 不会让界面跟着变。
        if (typeof syncResourceSelectUI === "function") syncResourceSelectUI(U.stickerMaxPerReply);
    }
    if (U.stickerSemanticToggle) U.stickerSemanticToggle.checked = !!settings.semantic_search;
}

// ---------------------------------------------------------------- load

async function loadStickerView() {
    if (!StickersState.loadedOnce) {
        void loadStickerSettings();
        await loadStickerPacks();
        return;
    }
    await loadStickerPacks({ select: false, quiet: true });
}

async function loadStickerSettings() {
    try {
        StickersState.settings = await ApiClient.getStickerSettings();
        StickersState.settingsLoadedOnce = true;
        renderStickerSettings();
    } catch (error) {
        if (isAbortLike(error)) return;
        stickerFail("发送策略加载失败", error);
    }
}

async function loadStickerPacks({ select = true, quiet = false } = {}) {
    if (!quiet && !StickersState.packs.length && U.stickerPackList) {
        U.stickerPackList.innerHTML = '<div class="empty-state">正在读取表情包…</div>';
    }
    try {
        StickersState.packs = await ApiClient.getStickerPacks();
        StickersState.loadedOnce = true;
        if (select || !StickersState.selected) {
            const keep = StickersState.packs.find((pack) => pack.dir_name === StickersState.selected);
            StickersState.selected = (keep || StickersState.packs[0] || {}).dir_name || "";
        }
        renderStickerPacks();
        if (StickersState.selected) {
            const known = StickersState.packs.some((pack) => pack.dir_name === StickersState.selected);
            if (!known) StickersState.selected = (StickersState.packs[0] || {}).dir_name || "";
        }
        if (StickersState.selected) await loadStickerDetail(StickersState.selected, { quiet: true });
        else renderStickerDetail();
    } catch (error) {
        if (isAbortLike(error)) return;
        if (!StickersState.packs.length && U.stickerPackList) {
            U.stickerPackList.innerHTML = `<div class="empty-state error">表情包加载失败：${esc(stickerErrorText(error))}</div>`;
        }
        stickerFail("表情包加载失败", error);
    }
}

async function loadStickerDetail(dirName, { quiet = false } = {}) {
    if (!dirName) return;
    if (!quiet && (!StickersState.detail || StickersState.detail.dir_name !== dirName)) {
        renderStickerEmptyDetail("正在读取图片…");
    }
    try {
        const pack = await ApiClient.getStickerPack(dirName, STICKER_DEFAULT_PER_CATEGORY);
        if (!pack) return;
        StickersState.detail = pack;
        StickersState.selected = pack.dir_name;
        renderStickerPacks();
        renderStickerDetail();
    } catch (error) {
        if (isAbortLike(error)) return;
        renderStickerEmptyDetail(`读取失败：${esc(stickerErrorText(error))}`);
        stickerFail("表情包读取失败", error);
    }
}

// ---------------------------------------------------------------- actions

function stickerSelectedPack() {
    return StickersState.detail;
}

function stickerConfirmDelete({ title, text, onConfirm }) {
    openConfirm({
        title,
        text,
        confirmLabel: "彻底删除",
        confirmKind: "danger",
        returnFocus: U.stickerRefresh,
        onConfirm,
    });
}

function onStickerListClick(event) {
    const trigger = event.target.closest("[data-sticker-pack]");
    if (!trigger) return;
    const dirName = trigger.dataset.stickerPack;
    if (!dirName || dirName === StickersState.selected) return;
    StickersState.selected = dirName;
    renderStickerPacks();
    void loadStickerDetail(dirName);
}

function onStickerDetailKeydown(event) {
    if (event.key !== "Enter") return;
    const field = event.target.closest("#sticker-theme-input, [data-sticker-description]");
    if (!field) return;
    event.preventDefault();
    const saveTrigger = field.parentElement?.querySelector("[data-sticker-action^='save-']");
    if (saveTrigger) saveTrigger.click();
}

function onStickerDetailInput(event) {
    const theme = event.target.closest("#sticker-theme-input");
    if (theme) {
        const button = theme.parentElement?.querySelector("[data-sticker-action='save-theme']");
        const saved = String(stickerSelectedPack()?.theme || "");
        if (button) button.hidden = theme.value.trim() === saved.trim();
        return;
    }
    const description = event.target.closest("[data-sticker-description]");
    if (description) {
        const button = description.parentElement?.querySelector("[data-sticker-action='save-description']");
        const card = description.closest(".sticker-category-card");
        const dirName = card?.dataset.stickerCategory || "";
        const category = (stickerSelectedPack()?.categories || []).find((item) => item.dir_name === dirName);
        if (button) button.hidden = description.value.trim() === String(category?.description || "").trim();
    }
}

function onStickerDetailClick(event) {
    const trigger = event.target.closest("[data-sticker-action]");
    if (!trigger) return;
    const action = trigger.dataset.stickerAction;
    const pack = stickerSelectedPack();
    if (!pack) return;
    switch (action) {
        case "preview":
            openMediaViewer({
                mode: "image",
                url: trigger.dataset.url || "",
                title: trigger.dataset.title || pack.key,
                subtitle: trigger.dataset.name || "",
                downloadName: trigger.dataset.name || "",
            });
            return;
        case "save-theme": {
            const input = U.stickerDetail.querySelector("#sticker-theme-input");
            void saveStickerTheme(pack, input ? input.value : "");
            return;
        }
        case "toggle-pack":
            void runStickerMutation({
                label: pack.enabled ? "正在停用…" : "正在启用…",
                request: () => ApiClient.setStickerPackEnabled(pack.dir_name, !pack.enabled),
                onSuccess: (data) => applyStickerPack(data.item, pack.dir_name),
            });
            return;
        case "delete-pack":
            stickerConfirmDelete({
                title: `删除表情包 ${pack.key}`,
                text: `会删掉整个包：${Number(pack.total || 0)} 张图、${(pack.categories || []).length} 个分类，不可恢复。只想暂时不出现请改用「停用」。`,
                onConfirm: () =>
                    runStickerMutation({
                        label: "正在删除…",
                        request: () => ApiClient.deleteStickerPack(pack.dir_name),
                        onSuccess: () => dropStickerPack(pack.dir_name),
                    }),
            });
            return;
        case "create-category": {
            const keyInput = U.stickerDetail.querySelector("#sticker-new-category-key");
            const descInput = U.stickerDetail.querySelector("#sticker-new-category-desc");
            void createStickerCategory(pack, keyInput ? keyInput.value : "", descInput ? descInput.value : "");
            return;
        }
        case "upload": {
            const card = trigger.closest(".sticker-category-card");
            const dirName = card?.dataset.stickerCategory || "";
            if (dirName) pickStickerUploads(pack.dir_name, dirName);
            return;
        }
        default:
            break;
    }
    const card = trigger.closest(".sticker-category-card");
    const categoryDirName = card?.dataset.stickerCategory || "";
    if (!categoryDirName) return;
    const category = (pack.categories || []).find((item) => item.dir_name === categoryDirName);
    if (!category) return;
    switch (action) {
        case "save-description": {
            const input = card.querySelector("[data-sticker-description]");
            void saveStickerDescription(pack, category, input ? input.value : "");
            return;
        }
        case "toggle-category":
            void runStickerMutation({
                label: category.enabled ? "正在停用…" : "正在启用…",
                request: () => ApiClient.setStickerCategoryEnabled(pack.dir_name, category.dir_name, !category.enabled),
                onSuccess: (data) => applyStickerPack(data.item, pack.dir_name),
            });
            return;
        case "delete-category":
            stickerConfirmDelete({
                title: `删除分类 ${category.key}`,
                text: `会删掉 ${Number(category.count || 0)} 张图，不可恢复。只想暂时不出现请改用「停用」。`,
                onConfirm: () =>
                    runStickerMutation({
                        label: "正在删除…",
                        request: () => ApiClient.deleteStickerCategory(pack.dir_name, category.dir_name),
                        onSuccess: (data) => applyStickerPack(data.item, pack.dir_name),
                    }),
            });
            return;
        case "delete-image": {
            const name = trigger.dataset.name || "";
            if (!name) return;
            stickerConfirmDelete({
                title: "删除这张图",
                text: `会从磁盘删掉 ${name}，不可恢复。`,
                onConfirm: () =>
                    runStickerMutation({
                        label: "正在删除…",
                        request: () => ApiClient.deleteStickerImage(pack.dir_name, category.dir_name, name),
                        onSuccess: (data) => {
                            delete StickersState.expanded[stickerKey(pack.dir_name, category.dir_name)];
                            applyStickerPack(data.item, pack.dir_name);
                        },
                    }),
            });
            return;
        }
        case "expand-category":
            void expandStickerCategory(pack, category);
            return;
        default:
            break;
    }
}

async function expandStickerCategory(pack, category) {
    stickerSetBusy(true, "正在读取全部图片…");
    try {
        const data = await ApiClient.getStickerCategoryImages(pack.dir_name, category.dir_name, 0, 500);
        StickersState.expanded[stickerKey(pack.dir_name, category.dir_name)] = data.items;
        renderStickerDetail();
    } catch (error) {
        if (!isAbortLike(error)) stickerFail("读取图片失败", error);
    } finally {
        stickerSetBusy(false);
    }
}

async function saveStickerTheme(pack, theme) {
    await runStickerMutation({
        label: "正在保存主题…",
        request: () => ApiClient.updateStickerTheme(pack.dir_name, String(theme || "").trim()),
        onSuccess: (data) => applyStickerPack(data.item, pack.dir_name),
    });
}

async function saveStickerDescription(pack, category, description) {
    await runStickerMutation({
        label: "正在保存描述…",
        request: () => ApiClient.updateStickerCategoryDescription(pack.dir_name, category.dir_name, String(description || "").trim()),
        onSuccess: (data) => applyStickerPack(data.item, pack.dir_name),
    });
}

async function createStickerCategory(pack, key, description) {
    const trimmed = String(key || "").trim();
    if (!trimmed) {
        stickerFail("新建分类", new Error("先填分类键，如 happy"));
        return;
    }
    await runStickerMutation({
        label: "正在新建分类…",
        request: () => ApiClient.createStickerCategory(pack.dir_name, trimmed, String(description || "").trim()),
        onSuccess: (data) => applyStickerPack(data.item, pack.dir_name),
    });
}

function pickStickerUploads(packDirName, categoryDirName) {
    if (!U.stickerUploadInput) return;
    StickersState.uploadTarget = { pack: packDirName, category: categoryDirName };
    U.stickerUploadInput.value = "";
    U.stickerUploadInput.click();
}

async function onStickerUploadPicked(event) {
    const files = Array.from(event.target.files || []);
    const target = StickersState.uploadTarget;
    if (!files.length || !target) return;
    await runStickerMutation({
        label: `正在上传 ${files.length} 张…`,
        request: () => ApiClient.uploadStickerImages(target.pack, target.category, files),
        onSuccess: (data) => {
            delete StickersState.expanded[stickerKey(target.pack, target.category)];
            applyStickerPack(data.item, target.pack);
        },
    });
}

async function onStickerMaxPerReplyChanged() {
    const raw = Number(U.stickerMaxPerReply?.value);
    const value = Number.isFinite(raw) ? raw : 1;
    await saveStickerSettings({ max_per_reply: value }, "每轮张数保存失败");
}

async function onStickerSemanticChanged() {
    const enabled = !!U.stickerSemanticToggle?.checked;
    await saveStickerSettings({ semantic_search: enabled }, "精确取图开关保存失败");
}

async function saveStickerSettings(patch, failureTitle) {
    const previous = StickersState.settings;
    const pending = { ...(previous || {}), ...patch };
    if (patch.max_per_reply !== undefined) pending.max_per_reply = patch.max_per_reply;
    if (patch.semantic_search !== undefined) pending.semantic_search = patch.semantic_search;
    StickersState.settings = pending;
    renderStickerSettings();
    try {
        StickersState.settings = await ApiClient.updateStickerSettings(patch);
        StickersState.settingsLoadedOnce = true;
        renderStickerSettings();
    } catch (error) {
        StickersState.settings = previous;
        renderStickerSettings();
        if (!isAbortLike(error)) stickerFail(failureTitle, error);
    }
}

function bindStickerPage() {
    if (U.stickerRefresh) {
        U.stickerRefresh.addEventListener("click", () => void loadStickerPacks({ select: false }));
    }
    if (U.stickerSearch) {
        U.stickerSearch.addEventListener("input", () => {
            StickersState.search = U.stickerSearch.value || "";
            renderStickerPacks();
        });
    }
    if (U.stickerPackList) U.stickerPackList.addEventListener("click", onStickerListClick);
    if (U.stickerDetail) {
        U.stickerDetail.addEventListener("click", onStickerDetailClick);
        U.stickerDetail.addEventListener("input", onStickerDetailInput);
        U.stickerDetail.addEventListener("keydown", onStickerDetailKeydown);
    }
    if (U.stickerUploadInput) U.stickerUploadInput.addEventListener("change", onStickerUploadPicked);
    if (U.stickerMaxPerReply) U.stickerMaxPerReply.addEventListener("change", onStickerMaxPerReplyChanged);
    if (U.stickerSemanticToggle) U.stickerSemanticToggle.addEventListener("change", onStickerSemanticChanged);
}
