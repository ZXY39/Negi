const test = require("node:test");
const assert = require("node:assert/strict");
const vm = require("node:vm");
const { readFrontendSources } = require("./support/frontend_sources.js");

// 渠道附件在前端这一路的显示契约：描述符按字段重建的每一层都必须留住 `url`。
// 渠道附件不在 web 上传根下，丢了 url 就回退到 `/api/ceo/uploads/file`，
// 那条路由的边界是 web 上传目录 ⇒ 拿到 404 的破图。

const APP_CODE = readFrontendSources(["app"]).join("\n");

const {
    StubElement,
    StubHTMLElement,
    StubDocument,
    StubHTMLButtonElement,
    StubHTMLInputElement,
    StubHTMLTextAreaElement,
    StubHTMLSelectElement,
} = require("./support/frontend_stub.js");

function loadApp() {
    const context = {
        console,
        setTimeout,
        clearTimeout,
        setInterval,
        clearInterval,
        queueMicrotask,
        navigator: { clipboard: { writeText: async () => {} } },
        location: { protocol: "http:", host: "localhost", pathname: "/org_graph.html" },
        localStorage: { getItem: () => null, setItem: () => {}, removeItem: () => {} },
        sessionStorage: { getItem: () => null, setItem: () => {}, removeItem: () => {} },
        document: new StubDocument(),
        Element: StubElement,
        HTMLElement: StubHTMLElement,
        HTMLButtonElement: StubHTMLButtonElement,
        HTMLInputElement: StubHTMLInputElement,
        HTMLTextAreaElement: StubHTMLTextAreaElement,
        HTMLSelectElement: StubHTMLSelectElement,
        URLSearchParams,
        URL,
        AbortController,
        fetch: async () => ({ ok: true, json: async () => ({}) }),
        lucide: { createIcons() {} },
        marked: { parse: (value) => String(value) },
        DOMPurify: { sanitize: (value) => String(value) },
        structuredClone: global.structuredClone,
        performance: { now: () => 0 },
        requestAnimationFrame: (callback) => {
            callback();
            return 1;
        },
        cancelAnimationFrame: () => {},
        addEventListener() {},
        removeEventListener() {},
    };
    context.window = context;
    vm.createContext(context);
    vm.runInContext(
        `${APP_CODE}
        this.__testExports = {
            cloneCeoSnapshotAttachments,
            normalizeUploadList,
            ceoAttachmentHref,
            renderStructuredChatAttachments,
        };`,
        context
    );
    return context.__testExports;
}

const SESSION_ID = "ext:qq-official-1903529517:f8a8001865631301";
const CHANNEL_IMAGE = {
    path: "C:\\data\\.g3ku\\external-uploads\\ext_qq-official-1903529517_f8a8001865631301\\0f41be4e-90123D503E4852183A12CC49156B18B7.jpg",
    name: "90123D503E4852183A12CC49156B18B7.jpg",
    mime_type: "image/jpeg",
    kind: "image",
    size: 72196,
    url: "/api/ceo/external-upload-file?session_id=ext%3Aqq-official-1903529517%3Af8a8001865631301&path=C%3A%5Cdata",
};

test("克隆附件描述符时保住 url：缓存与待发送 chip 都靠它取图", () => {
    const { cloneCeoSnapshotAttachments } = loadApp();
    const [kept] = cloneCeoSnapshotAttachments([CHANNEL_IMAGE]);
    assert.equal(kept.url, CHANNEL_IMAGE.url);
    assert.equal(kept.kind, "image");
    assert.equal(kept.name, CHANNEL_IMAGE.name);
    assert.equal(kept.size, 72196);
});

test("渠道图片画成缩略图，地址是 external-upload-file 而不是 uploads/file", () => {
    const { cloneCeoSnapshotAttachments, renderStructuredChatAttachments } = loadApp();
    const markup = renderStructuredChatAttachments(
        cloneCeoSnapshotAttachments([CHANNEL_IMAGE]),
        { sessionId: SESSION_ID }
    );
    assert.match(markup, /chat-attachment-image/);
    assert.ok(markup.includes("/api/ceo/external-upload-file?"));
    assert.ok(!markup.includes("/api/ceo/uploads/file"));
});

test("没有 url 的历史描述符仍回退到 web 上传车道", () => {
    const { ceoAttachmentHref } = loadApp();
    const href = ceoAttachmentHref({ path: "C:/tmp/web-ceo-uploads/sess/pic.png" }, "web:ceo-demo");
    assert.ok(href.startsWith("/api/ceo/uploads/file?"));
    assert.ok(href.includes("session_id=web%3Aceo-demo"));
});
