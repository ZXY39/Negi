"use strict";

// 程序内媒体查看器。界面里的图片/附件锚点过去写成 target="_blank"，那在 WebView2 里是一次
// 新窗请求，而 pywebview 把新窗请求交给系统浏览器——桌面窗口里点图片跳出去就是这样发生的。
// 现在可预览的在本层显示，不可预览的走下载（窗口里是「另存为」，浏览器里是普通下载）。
// 真外链不带这些标记，仍然交系统浏览器。

const MEDIA_VIEWER_IMAGE_SUFFIXES = new Set(["png", "jpg", "jpeg", "gif", "webp", "bmp", "svg"]);
// Markdown 不走 iframe 的 text/plain 原文：同源取回正文后用聊天同款渲染器排版。
const MEDIA_VIEWER_MARKDOWN_SUFFIXES = new Set(["md", "markdown"]);
// 只收服务端确实按 inline 供的类型：/api/ceo/media/original 的 inline 判定是 image（svg 除外）/
// application/pdf/text/*，另加 `_INLINE_TEXT_SUFFIXES`（json/yaml/代码等被显式按 text/plain 供）。
// 收进 iframe 的 attachment 类型不会显示，只会变成一次下载。
// 刻意不收 html / css / js / svg：它们被同源 iframe 加载时会在应用 origin 里执行脚本。
const MEDIA_VIEWER_EMBED_SUFFIXES = new Set([
    "pdf",
    "txt", "log", "rst", "csv", "tsv", "srt", "vtt",
    "json", "jsonl", "yaml", "yml", "toml", "ini", "cfg", "conf", "xml",
    "py", "sh", "bash", "bat", "ps1", "sql", "java", "c", "h", "cpp", "hpp",
    "rs", "go", "rb", "php", "lua", "r", "kt", "swift",
]);

const mediaViewerState = {
    backdrop: null,
    drawer: null,
    title: null,
    subtitle: null,
    body: null,
    saveBtn: null,
    url: "",
    name: "",
    open: false,
    closeTimer: 0,
};

function mediaViewerSuffix(name = "") {
    const base = String(name || "").split(/[\\/]/).pop() || "";
    const dot = base.lastIndexOf(".");
    return dot > 0 ? base.slice(dot + 1).toLowerCase() : "";
}

function resolveMediaViewerMode({ kind = "", name = "" } = {}) {
    if (String(kind || "").trim() === "image") return "image";
    const suffix = mediaViewerSuffix(name);
    if (MEDIA_VIEWER_IMAGE_SUFFIXES.has(suffix)) return "image";
    if (MEDIA_VIEWER_MARKDOWN_SUFFIXES.has(suffix)) return "markdown";
    if (MEDIA_VIEWER_EMBED_SUFFIXES.has(suffix)) return "embed";
    return "download";
}

function resetMediaViewerSize() {
    const drawer = mediaViewerState.drawer;
    if (typeof drawer?.style?.removeProperty !== "function") return;
    drawer.style.removeProperty("width");
    drawer.style.removeProperty("height");
}

// 图片窗口跟着图片走：按自然尺寸和视口上限算出实际显示尺寸，再把抽屉收到
// 「图片 + 头部 + 底部按钮条 + 内边距」。开窗前就要量好——打开后再改尺寸会先亮出默认大窗。
function syncMediaViewerImageSize() {
    const drawer = mediaViewerState.drawer;
    const img = mediaViewerState.body?.querySelector?.(".media-viewer-image");
    if (!drawer?.style || !img) {
        resetMediaViewerSize();
        return;
    }
    const naturalWidth = Number(img.naturalWidth) || 0;
    const naturalHeight = Number(img.naturalHeight) || 0;
    if (!(naturalWidth > 0) || !(naturalHeight > 0)) return;
    const headerEl = drawer.querySelector(".detail-modal-header");
    const footerEl = drawer.querySelector(".media-viewer-footer");
    const bodyEl = drawer.querySelector(".detail-modal-body");
    const bodyStyle = bodyEl && typeof getComputedStyle === "function" ? getComputedStyle(bodyEl) : null;
    const padX = bodyStyle ? (parseFloat(bodyStyle.paddingLeft) || 0) + (parseFloat(bodyStyle.paddingRight) || 0) : 0;
    const padY = bodyStyle ? (parseFloat(bodyStyle.paddingTop) || 0) + (parseFloat(bodyStyle.paddingBottom) || 0) : 0;
    const maxWidth = Math.min(1180, window.innerWidth - 32);
    const maxHeight = Math.min(window.innerHeight * 0.86, window.innerHeight - 48);
    const availWidth = Math.max(160, maxWidth - padX);
    // 头部标题会随窗宽折行：先在临时窗宽上量一次头部/底部，再按算出的最终窗宽复量。
    const measure = (width) => {
        drawer.style.width = `${Math.round(width)}px`;
        const headerHeight = headerEl ? headerEl.getBoundingClientRect().height : 0;
        const footerHeight = footerEl ? footerEl.getBoundingClientRect().height : 0;
        const availHeight = Math.max(120, maxHeight - headerHeight - footerHeight - padY - 2);
        const scale = Math.min(1, availWidth / naturalWidth, availHeight / naturalHeight);
        return { headerHeight, footerHeight, scale };
    };
    const provisionalWidth = Math.min(maxWidth, Math.max(300, naturalWidth + padX + 2));
    let pass = measure(provisionalWidth);
    let displayWidth = Math.round(naturalWidth * pass.scale);
    let displayHeight = Math.round(naturalHeight * pass.scale);
    let width = Math.min(maxWidth, Math.max(300, displayWidth + padX + 2));
    if (Math.abs(width - provisionalWidth) > 1) {
        pass = measure(width);
        displayWidth = Math.round(naturalWidth * pass.scale);
        displayHeight = Math.round(naturalHeight * pass.scale);
        width = Math.min(maxWidth, Math.max(300, displayWidth + padX + 2));
    }
    // 图片自己也按算出的尺寸封顶：只收窗框的话，图片会撑满内容宽再把底部按钮顶出去。
    img.style.maxWidth = `${displayWidth}px`;
    img.style.maxHeight = `${displayHeight}px`;
    drawer.style.width = `${Math.round(width)}px`;
    drawer.style.height = `${Math.round(Math.min(maxHeight, Math.max(220, displayHeight + pass.headerHeight + pass.footerHeight + padY + 2)))}px`;
}

function ensureMediaViewerSurface() {
    if (mediaViewerState.backdrop && mediaViewerState.drawer) return;
    const backdrop = document.createElement("div");
    backdrop.id = "media-viewer-backdrop";
    backdrop.className = "detail-backdrop";
    backdrop.setAttribute("aria-hidden", "true");

    const drawer = document.createElement("section");
    drawer.id = "media-viewer-drawer";
    drawer.className = "panel detail-drawer media-viewer-drawer";
    drawer.setAttribute("role", "dialog");
    drawer.setAttribute("aria-modal", "true");
    drawer.setAttribute("aria-hidden", "true");
    drawer.setAttribute("aria-labelledby", "media-viewer-title");
    drawer.tabIndex = -1;
    drawer.innerHTML = `
        <div class="detail-modal-header">
            <div>
                <h2 id="media-viewer-title">内容预览</h2>
                <p id="media-viewer-subtitle" class="subtitle" hidden></p>
            </div>
        </div>
        <div class="detail-modal-body">
            <div id="media-viewer-body" class="media-viewer-body"></div>
        </div>
        <div class="media-viewer-footer">
            <button id="media-viewer-save-btn" class="toolbar-btn ghost" type="button" hidden>保存到本地</button>
            <button id="media-viewer-close-btn" class="toolbar-btn ghost" type="button" data-modal-close>关闭</button>
        </div>
    `;

    document.body.appendChild(backdrop);
    document.body.appendChild(drawer);
    mediaViewerState.backdrop = backdrop;
    mediaViewerState.drawer = drawer;
    mediaViewerState.title = drawer.querySelector("#media-viewer-title");
    mediaViewerState.subtitle = drawer.querySelector("#media-viewer-subtitle");
    mediaViewerState.body = drawer.querySelector("#media-viewer-body");
    mediaViewerState.saveBtn = drawer.querySelector("#media-viewer-save-btn");

    drawer.querySelector("#media-viewer-close-btn")?.addEventListener("click", () => closeMediaViewer());
    mediaViewerState.saveBtn?.addEventListener("click", () => {
        triggerMediaDownload(mediaViewerState.url, mediaViewerState.name);
    });
    backdrop.addEventListener("click", (event) => {
        if (event.target === backdrop) closeMediaViewer();
    });
    // 视口变化后重新量：图片窗口跟着剩余空间收缩，不留下超出屏幕的旧尺寸。
    window.addEventListener("resize", () => {
        if (mediaViewerState.open) syncMediaViewerImageSize();
    });
    // 续读沿用轨迹字段那条车道：正文按服务端一屏取，不整份搬进页面。
    drawer.addEventListener("click", (event) => {
        const more = event.target instanceof Element ? event.target.closest(".task-trace-more") : null;
        if (more && typeof loadMoreTraceOutputBlock === "function") {
            void loadMoreTraceOutputBlock(more);
        }
    });
}

function renderMediaViewerImage(url, title, { onReady = null } = {}) {
    const img = document.createElement("img");
    img.className = "media-viewer-image";
    img.src = url;
    img.alt = title;
    img.addEventListener("load", () => {
        if (typeof onReady === "function") onReady();
    });
    img.addEventListener("error", () => {
        mediaViewerState.body.innerHTML = '<div class="empty-state">这个文件已经读不到了。</div>';
        if (typeof onReady === "function") onReady();
    });
    mediaViewerState.body.appendChild(img);
    return img;
}

function renderMediaViewerEmbed(url, title) {
    const frame = document.createElement("iframe");
    frame.className = "media-viewer-frame";
    frame.src = url;
    frame.title = title;
    mediaViewerState.body.appendChild(frame);
}

// .md 附件按 Markdown 排版：iframe 只会把 text/plain 原文贴出来。取数用同源 fetch，
// 失败（外链、文件被回收）退回 iframe 原文，至少还能读到内容。
function renderMediaViewerMarkdown(url, title, openToken = 0) {
    const article = document.createElement("article");
    article.className = "markdown-content media-viewer-markdown";
    article.textContent = "正在加载…";
    mediaViewerState.body.appendChild(article);
    const isCurrent = () => mediaViewerState.open === true && mediaViewerState.openToken === openToken;
    const fallbackToRaw = () => {
        if (!isCurrent()) return;
        mediaViewerState.body.innerHTML = "";
        renderMediaViewerEmbed(url, title);
        resetMediaViewerSize();
        if (typeof icons === "function") icons(true);
    };
    if (typeof fetch !== "function") {
        // 没有 fetch 的环境没有渲染档可言，直接回到原文内嵌；这一步发生在 reveal 之前，
        // 不能走 isCurrent 闸门（那时窗还没标记为打开）。
        mediaViewerState.body.innerHTML = "";
        renderMediaViewerEmbed(url, title);
        return article;
    }
    fetch(url, { credentials: "same-origin" })
        .then((response) => {
            if (!response.ok) throw new Error(`HTTP ${response.status}`);
            return response.text();
        })
        .then((text) => {
            if (!isCurrent()) return;
            if (typeof renderMarkdown === "function") {
                article.innerHTML = renderMarkdown(text);
            } else {
                article.textContent = text;
                article.classList.add("media-viewer-markdown-raw");
            }
            if (typeof icons === "function") icons(true);
        })
        .catch(() => fallbackToRaw());
    return article;
}

function renderMediaViewerContent(outputRef, title) {
    // 与节点详情轨迹字段同形：.task-trace-field + .task-trace-output-value + .task-trace-more，
    // 这样首屏与续读都走既有的取数车道，本层不复刻行窗数学。
    mediaViewerState.body.innerHTML = `
        <div class="task-trace-field">
            <div class="task-trace-label-row">
                <span class="task-trace-label">${esc(title)}</span>
                <button type="button" class="task-trace-copy task-trace-more" data-trace-more="" hidden
                        aria-label="继续加载下一段" title="继续加载下一段"><i data-lucide="chevrons-down"></i></button>
            </div>
            <div class="code-block task-trace-code task-trace-output-value"
                 data-output-ref="${esc(outputRef)}" data-empty-text="暂无内容"></div>
        </div>
    `;
    const block = mediaViewerState.body.querySelector(".task-trace-output-value");
    if (typeof hydrateTraceOutputBlocks === "function") {
        hydrateTraceOutputBlocks(mediaViewerState.body);
        return block;
    }
    if (typeof ensureTraceOutputCodeBlockContent === "function" && block) {
        void ensureTraceOutputCodeBlockContent(block, { loadingText: "正在加载完整内容..." });
    }
    return block;
}

function openMediaViewer({ mode = "image", url = "", title = "内容预览", subtitle = "", outputRef = "", downloadName = "" } = {}) {
    ensureMediaViewerSurface();
    // 上一条关闭动画还没走完就重新打开：先撤掉延迟拆除，别把新内容一起清掉。
    if (mediaViewerState.closeTimer) {
        window.clearTimeout(mediaViewerState.closeTimer);
        mediaViewerState.closeTimer = 0;
    }
    const openToken = (mediaViewerState.openToken || 0) + 1;
    mediaViewerState.openToken = openToken;
    mediaViewerState.title.textContent = title;
    mediaViewerState.subtitle.textContent = subtitle;
    if (mediaViewerState.subtitle) mediaViewerState.subtitle.hidden = !subtitle;
    mediaViewerState.body.innerHTML = "";
    // content 模式（轨迹续读）没有文件 URL，此时不该出现「保存到本地」。
    mediaViewerState.url = mode === "content" ? "" : String(url || "");
    mediaViewerState.name = String(downloadName || title || "").trim();
    if (mediaViewerState.saveBtn) mediaViewerState.saveBtn.hidden = !mediaViewerState.url;
    let revealed = false;
    const reveal = () => {
        if (revealed) return;
        revealed = true;
        // 期间又开了别的图/正文：这一枪作废，别把旧内容顶上来。
        if (mediaViewerState.openToken !== openToken) return;
        syncMediaViewerImageSize();
        setDrawerOpen(mediaViewerState.backdrop, mediaViewerState.drawer, true);
        mediaViewerState.open = true;
        if (typeof icons === "function") icons(true);
    };
    if (mode === "image") {
        // 先等图片解码、把窗框量到最终尺寸再开：否则首帧会先亮出一个默认大小的空白窗
        // （图片走缓存时该帧几乎看不到，所以只有刷新后的第一张图会闪）。
        const img = renderMediaViewerImage(url, title, { onReady: reveal });
        if (img.complete && img.naturalWidth) reveal();
        return true;
    }
    if (mode === "embed") renderMediaViewerEmbed(url, title);
    else if (mode === "markdown") renderMediaViewerMarkdown(url, title, openToken);
    else if (mode === "content") renderMediaViewerContent(outputRef, title);
    resetMediaViewerSize();
    reveal();
    return true;
}

function closeMediaViewer() {
    if (!mediaViewerState.open) return false;
    setDrawerOpen(mediaViewerState.backdrop, mediaViewerState.drawer, false);
    mediaViewerState.open = false;
    mediaViewerState.url = "";
    mediaViewerState.name = "";
    // 收起动画（160ms）走完再拆内容和尺寸：立刻清空会在淡出途中露出一张空白窗。
    if (mediaViewerState.closeTimer) window.clearTimeout(mediaViewerState.closeTimer);
    mediaViewerState.closeTimer = window.setTimeout(() => {
        mediaViewerState.closeTimer = 0;
        if (mediaViewerState.open) return;
        resetMediaViewerSize();
        if (mediaViewerState.body) mediaViewerState.body.innerHTML = "";
    }, 200);
    return true;
}

function isMediaViewerOpen() {
    return mediaViewerState.open;
}

function triggerMediaDownload(url, name = "") {
    if (!url) return false;
    const link = document.createElement("a");
    link.href = url;
    link.download = String(name || "");
    document.body.appendChild(link);
    link.click();
    link.remove();
    return true;
}

function openMediaAnchor({ url = "", name = "", kind = "", title = "" } = {}) {
    if (!url || url === "#") return false;
    const mode = resolveMediaViewerMode({ kind, name });
    if (mode === "download") return triggerMediaDownload(url, name || "attachment");
    // name 决定模式与下载文件名（要带扩展名），title 只是给人看的标题（可以是不带扩展名的描述）。
    return openMediaViewer({ mode, url, title: title || name || "内容预览", downloadName: name });
}

function bindMediaViewer() {
    document.addEventListener("click", (event) => {
        if (!(event.target instanceof Element)) return;
        // 带修饰键的点击是"我要在别处打开"，留给浏览器默认行为。
        if (event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
        const anchor = event.target.closest("a[data-media-open]");
        if (!anchor) return;
        event.preventDefault();
        openMediaAnchor({
            url: String(anchor.getAttribute("href") || "").trim(),
            name: String(anchor.dataset?.mediaName || ""),
            kind: String(anchor.dataset?.mediaOpen || ""),
            title: String(anchor.dataset?.mediaTitle || ""),
        });
    });
}
