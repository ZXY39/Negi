const test = require("node:test");
const assert = require("node:assert/strict");
const vm = require("node:vm");
const { readFrontendSources } = require("./support/frontend_sources.js");
const { StubElement, StubHTMLElement: BaseStubHTMLElement, StubDocument } = require("./support/frontend_stub.js");

const CODE = readFrontendSources(["app"]).join("\n");

// 桩按 id 建子节点：生产代码用 innerHTML + querySelector 拿本层节点，
// 共享桩默认返回 null，这里只补媒体查看器自己那三个 id。
class TestElement extends BaseStubHTMLElement {
    constructor(tag) {
        super(tag);
        this.clicks = [];
    }

    get innerHTML() {
        return this._innerHTML;
    }

    // 真实 DOM 里赋 innerHTML 会换掉子节点；桩不跟着换就会把"关掉本层就摘掉内容"
    // 这条断言变成假绿。
    set innerHTML(value) {
        this._innerHTML = String(value);
        this.children.length = 0;
    }

    click() {
        this.clicks.push(this.href || this.getAttribute("href"));
    }

    querySelector(selector) {
        if (this._qs[selector]) return this._qs[selector];
        const id = /^#([A-Za-z0-9_-]+)$/.exec(String(selector || ""))?.[1];
        if (!id) return null;
        const created = new TestElement("DIV");
        created.id = id;
        this._qs[selector] = created;
        this.appendChild(created);
        return created;
    }
}

class TestDocument extends StubDocument {
    constructor() {
        super();
        this.created = [];
    }

    createElement(tag) {
        const element = new TestElement(String(tag || "div").toUpperCase());
        this.created.push(element);
        return element;
    }
}

function loadApp({ onHydrate = null } = {}) {
    const document = new TestDocument();
    const context = {
        console,
        setTimeout,
        clearTimeout,
        document,
        window: {},
        innerWidth: 1440,
        innerHeight: 900,
        Element: StubElement,
        HTMLElement: TestElement,
        URLSearchParams,
        lucide: { createIcons() {} },
        requestAnimationFrame: (callback) => {
            callback();
            return 1;
        },
        addEventListener() {},
        removeEventListener() {},
        ApiClient: {
            getActiveSessionId: () => "web:test",
            getConfigBundleDownloadUrl: (filename) => `http://localhost/api/bootstrap/config-bundle/download?filename=${filename}`,
        },
    };
    if (onHydrate) context.hydrateTraceOutputBlocks = onHydrate;
    context.window = context;
    vm.createContext(context);
    vm.runInContext(
        `${CODE}\nthis.__testExports = { renderInlineMarkdown, renderStructuredChatAttachmentCard, resolveMediaViewerMode, openMediaAnchor, openMediaViewer, closeMediaViewer, isMediaViewerOpen, bindMediaViewer, mediaViewerState, downloadConfigBundleFile };`,
        context
    );
    return { ...vm.runInContext("this.__testExports", context), context, document };
}

function findCreated(document, predicate) {
    return document.created.find(predicate) || null;
}

test("内联图片与文件 chip 不再要求开新窗口", () => {
    const { renderInlineMarkdown } = loadApp();

    // 后缀决定行为，所以 data-media-open 一律交给 auto：图片仍按图片开，
    // 非图片由 media-viewer 按扩展名决定预览还是保存。
    const imageMarkup = renderInlineMarkdown('![截图](/x.png "/api/ceo/media/original?token=abc")');
    assert.match(imageMarkup, /data-media-open="auto"/);
    assert.match(imageMarkup, /data-media-name="x\.png"/);
    assert.match(imageMarkup, /data-media-title="截图"/);
    assert.match(imageMarkup, /href="\/api\/ceo\/media\/original\?token=abc"/);
    assert.doesNotMatch(imageMarkup, /target="_blank"/);

    const chipMarkup = renderInlineMarkdown("[报告.pdf](/api/ceo/media/original?token=abc)");
    assert.match(chipMarkup, /data-media-open="auto"/);
    assert.match(chipMarkup, /data-media-name="报告\.pdf"/);
    assert.doesNotMatch(chipMarkup, /target="_blank"/);
});

test("`![]()` 包住非图片文件时渲染成文件卡片而不是破图", () => {
    const { renderInlineMarkdown } = loadApp();

    // 服务端会保留 `![]` 形状交给前端判类型；这里盯的是渲染器别把它当 <img>。
    const card = renderInlineMarkdown('![季度报告](report.md "/api/ceo/media/original?token=abc")');
    assert.match(card, /msg-file-chip/);
    assert.doesNotMatch(card, /<img/);
    assert.match(card, /data-media-name="report\.md"/);
    assert.match(card, /data-media-title="季度报告"/);
    assert.match(card, /href="\/api\/ceo\/media\/original\?token=abc"/);

    // 没有签名标题的图片也要可点：过去这条只留一个不可点的 <img>。
    const bare = renderInlineMarkdown("![图](/tmp/a.png)");
    assert.match(bare, /<a class="msg-inline-image-link"/);
    assert.match(bare, /data-media-open="auto"/);
    assert.match(bare, /<img class="msg-inline-image"/);
});

test("会话附件卡按类型标记，图片显式声明 image", () => {
    const { renderStructuredChatAttachmentCard } = loadApp();

    const image = renderStructuredChatAttachmentCard({ kind: "image", name: "a.png", path: "a.png", size: 10 });
    assert.match(image, /data-media-open="image"/);
    assert.match(image, /data-media-name="a\.png"/);
    assert.doesNotMatch(image, /target="_blank"/);

    const file = renderStructuredChatAttachmentCard({ kind: "file", name: "b.docx", path: "b.docx", size: 10 });
    assert.match(file, /data-media-open="auto"/);
    assert.doesNotMatch(file, /target="_blank"/);
});

test("外部链接仍保留新窗口语义", () => {
    const { renderInlineMarkdown } = loadApp();

    assert.match(renderInlineMarkdown("[仓库](https://example.com/a)"), /target="_blank"/);
});

test("按类型分派：图片预览、pdf/文本内嵌、其余下载", () => {
    const { resolveMediaViewerMode } = loadApp();

    assert.equal(resolveMediaViewerMode({ kind: "image", name: "noext" }), "image");
    assert.equal(resolveMediaViewerMode({ name: "photo.PNG" }), "image");
    assert.equal(resolveMediaViewerMode({ name: "a\\b\\c.jpeg" }), "image");
    assert.equal(resolveMediaViewerMode({ name: "spec.md" }), "embed");
    assert.equal(resolveMediaViewerMode({ name: "deck.pdf" }), "embed");
    assert.equal(resolveMediaViewerMode({ name: "book.docx" }), "download");
    assert.equal(resolveMediaViewerMode({ name: "no-suffix" }), "download");
});

test("图片锚点在程序内开预览层显示", () => {
    const { openMediaAnchor, mediaViewerState, document } = loadApp();

    const opened = openMediaAnchor({ url: "http://localhost/api/ceo/media/original?token=abc", name: "截图.png", kind: "image" });

    assert.equal(opened, true);
    assert.ok(findCreated(document, (el) => el.id === "media-viewer-drawer"), "预览抽屉未挂上");
    const image = mediaViewerState.body.children.find((child) => child.tagName === "IMG");
    assert.ok(image, "图片未进本层显示");
    assert.equal(image.src, "http://localhost/api/ceo/media/original?token=abc");
    assert.equal(mediaViewerState.title.textContent, "截图.png");
    // 图片模式要等解码完再开窗，避免首帧先亮一个空白窗。
    image.naturalWidth = 800;
    image.naturalHeight = 600;
    image.dispatchEvent({ type: "load" });
    assert.equal(mediaViewerState.open, true);
});

test("文档类锚点走下载车道而不是开预览层", () => {
    const { openMediaAnchor, mediaViewerState, document } = loadApp();

    openMediaAnchor({ url: "http://localhost/a.docx", name: "book.docx", kind: "auto" });

    assert.equal(mediaViewerState.open, false);
    assert.equal(findCreated(document, (el) => el.id === "media-viewer-drawer"), null);
    const anchor = findCreated(document, (el) => el.tagName === "A");
    assert.ok(anchor, "文档类锚点未触发下载");
    assert.equal(anchor.download, "book.docx");
    assert.deepEqual(anchor.clicks, ["http://localhost/a.docx"]);
});

test("文本 ref 走服务端一屏那条车道，不整份搬进页面", () => {
    const hydrated = [];
    const { openMediaViewer, mediaViewerState } = loadApp({
        onHydrate: (root) => {
            hydrated.push(root);
        },
    });

    openMediaViewer({ mode: "content", title: "完整内容", outputRef: "artifact:e9f4b91c3391" });

    assert.match(mediaViewerState.body.innerHTML, /task-trace-output-value/);
    assert.match(mediaViewerState.body.innerHTML, /data-output-ref="artifact:e9f4b91c3391"/);
    assert.match(mediaViewerState.body.innerHTML, /task-trace-more/);
    assert.deepEqual(hydrated, [mediaViewerState.body], "未用既有取数车道水合首屏");
});

test("委托点击接管媒体锚点，带修饰键的点击留给浏览器", () => {
    const { bindMediaViewer, mediaViewerState, document } = loadApp();
    bindMediaViewer();

    const anchor = new TestElement("A");
    anchor.setAttribute("href", "http://localhost/pic.jpg");
    anchor.setAttribute("data-media-open", "image");
    anchor.setAttribute("data-media-name", "pic.jpg");

    const img = new TestElement("IMG");
    img.closest = (selector) => (selector === "a[data-media-open]" ? anchor : null);
    let prevented = 0;
    const preventDefault = () => {
        prevented += 1;
    };
    document.dispatchEvent({ type: "click", button: 0, target: img, preventDefault });
    assert.equal(prevented, 1);
    // 图片模式等解码完、量好窗框再开：桩里手动补一次 load。
    const opener = mediaViewerState.body.children[0];
    opener.naturalWidth = 640;
    opener.naturalHeight = 480;
    opener.dispatchEvent({ type: "load" });
    assert.equal(mediaViewerState.open, true);

    const plain = new TestElement("A");
    plain.closest = () => anchor;
    document.dispatchEvent({ type: "click", button: 0, ctrlKey: true, target: plain, preventDefault });
    assert.equal(prevented, 1, "修饰键点击不该被接管");
});

test("关闭预览层等淡出走完再收掉本层内容并回报是否真的关过", async () => {
    const { openMediaViewer, closeMediaViewer, mediaViewerState } = loadApp();

    assert.equal(closeMediaViewer(), false, "没开过就不该报告关闭");
    openMediaViewer({ mode: "image", url: "http://localhost/a.png", title: "a.png" });
    const opener = mediaViewerState.body.children[0];
    opener.naturalWidth = 400;
    opener.naturalHeight = 300;
    opener.dispatchEvent({ type: "load" });
    assert.equal(closeMediaViewer(), true);
    assert.equal(mediaViewerState.open, false);
    // 收起动画期间内容还在：立刻清空会在淡出途中露出一张空白窗。
    assert.equal(mediaViewerState.body.children.length, 1);
    await new Promise((resolve) => setTimeout(resolve, 240));
    assert.equal(mediaViewerState.body.children.length, 0, "淡出结束后 iframe/图片该被摘掉");
});

test("导出配置包共用同一条下载车道", () => {
    const { downloadConfigBundleFile, document } = loadApp();

    downloadConfigBundleFile("g3ku-config-bundle-20260924-010203.g3kucb");

    const anchor = findCreated(document, (el) => el.tagName === "A");
    assert.ok(anchor, "导出未触发下载");
    assert.equal(anchor.download, "g3ku-config-bundle-20260924-010203.g3kucb");
    assert.deepEqual(anchor.clicks, [
        "http://localhost/api/bootstrap/config-bundle/download?filename=g3ku-config-bundle-20260924-010203.g3kucb",
    ]);
});
