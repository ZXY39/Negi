"use strict";

// 程序内媒体查看器。界面里的图片/附件锚点过去写成 target="_blank"，那在 WebView2 里是一次
// 新窗请求，而 pywebview 把新窗请求交给系统浏览器——桌面窗口里点图片跳出去就是这样发生的。
// 现在可预览的在本层显示，不可预览的走下载（窗口里是「另存为」，浏览器里是普通下载）。
// 真外链不带这些标记，仍然交系统浏览器。

const MEDIA_VIEWER_IMAGE_SUFFIXES = new Set(["png", "jpg", "jpeg", "gif", "webp", "bmp", "svg"]);
// 只收服务端确实按 inline 供的类型：/api/ceo/media/original 的 inline 判定是 image（svg 除外）/
// application/pdf/text/*，另加 `_INLINE_TEXT_SUFFIXES`（json/yaml/代码等被显式按 text/plain 供）。
// 收进 iframe 的 attachment 类型不会显示，只会变成一次下载。
// 刻意不收 html / css / js / svg：它们被同源 iframe 加载时会在应用 origin 里执行脚本。
const MEDIA_VIEWER_EMBED_SUFFIXES = new Set([
    "pdf",
    "txt", "log", "md", "markdown", "rst", "csv", "tsv", "srt", "vtt",
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
    if (MEDIA_VIEWER_EMBED_SUFFIXES.has(suffix)) return "embed";
    return "download";
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
            <div class="detail-modal-actions">
                <button id="media-viewer-save-btn" class="toolbar-btn ghost" type="button" hidden>保存到本地</button>
                <button id="media-viewer-close-btn" class="toolbar-btn ghost" type="button" data-modal-close>关闭</button>
            </div>
        </div>
        <div class="detail-modal-body">
            <div id="media-viewer-body" class="media-viewer-body"></div>
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
    // 续读沿用轨迹字段那条车道：正文按服务端一屏取，不整份搬进页面。
    drawer.addEventListener("click", (event) => {
        const more = event.target instanceof Element ? event.target.closest(".task-trace-more") : null;
        if (more && typeof loadMoreTraceOutputBlock === "function") {
            void loadMoreTraceOutputBlock(more);
        }
    });
}

function renderMediaViewerImage(url, title) {
    const img = document.createElement("img");
    img.className = "media-viewer-image";
    img.src = url;
    img.alt = title;
    img.addEventListener("error", () => {
        mediaViewerState.body.innerHTML = '<div class="empty-state">这个文件已经读不到了。</div>';
    });
    mediaViewerState.body.appendChild(img);
}

function renderMediaViewerEmbed(url, title) {
    const frame = document.createElement("iframe");
    frame.className = "media-viewer-frame";
    frame.src = url;
    frame.title = title;
    mediaViewerState.body.appendChild(frame);
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
    mediaViewerState.title.textContent = title;
    mediaViewerState.subtitle.textContent = subtitle;
    if (mediaViewerState.subtitle) mediaViewerState.subtitle.hidden = !subtitle;
    mediaViewerState.body.innerHTML = "";
    // content 模式（轨迹续读）没有文件 URL，此时不该出现「保存到本地」。
    mediaViewerState.url = mode === "content" ? "" : String(url || "");
    mediaViewerState.name = String(downloadName || title || "").trim();
    if (mediaViewerState.saveBtn) mediaViewerState.saveBtn.hidden = !mediaViewerState.url;
    if (mode === "image") renderMediaViewerImage(url, title);
    else if (mode === "embed") renderMediaViewerEmbed(url, title);
    else if (mode === "content") renderMediaViewerContent(outputRef, title);
    setDrawerOpen(mediaViewerState.backdrop, mediaViewerState.drawer, true);
    mediaViewerState.open = true;
    if (typeof icons === "function") icons(true);
    return true;
}

function closeMediaViewer() {
    if (!mediaViewerState.open) return false;
    setDrawerOpen(mediaViewerState.backdrop, mediaViewerState.drawer, false);
    mediaViewerState.open = false;
    mediaViewerState.url = "";
    mediaViewerState.name = "";
    // 收掉本层内容：iframe 里的 pdf/正文还在解码，留着就是下一层的卡顿。
    if (mediaViewerState.body) mediaViewerState.body.innerHTML = "";
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
