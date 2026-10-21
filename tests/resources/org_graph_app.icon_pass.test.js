const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const { readFrontendSources } = require("./support/frontend_sources.js");

// 图标趟的规模契约：lucide 会把 data-lucide 连属性复制到生成的 <svg> 上，于是
// `[data-lucide]` 永不清空、每次 createIcons() 都按整份文档重画所有图标。
// icons() 必须在每趟结束后摘掉已渲染 svg 上的标记，让下一趟只处理真正待渲染的 <i>。

const APP_PATH = "g3ku/web/frontend/org_graph_app.js";
const APP_CODE = readFrontendSources(["app"]).join("\n");

const {
    StubElement,
    StubHTMLElement: BaseStubHTMLElement,
} = require("./support/frontend_stub.js");

class StubHTMLElement extends BaseStubHTMLElement {
    constructor(tag = "DIV", attrs = {}) {
        super(tag, attrs);
        this.removed = [];
    }

    removeAttribute(name) {
        this.removed.push(name);
        super.removeAttribute(name);
    }
}

function makeContext() {
    const rendered = [];
    const pendingIcons = [];
    let createIconsCalls = 0;
    const frameQueue = [];
    const svgSelector = "svg[data-lucide]";
    const context = {
        console,
        setTimeout,
        clearTimeout,
        setInterval,
        clearInterval,
        queueMicrotask,
        structuredClone: global.structuredClone,
        navigator: { clipboard: { writeText: async () => {} } },
        location: { protocol: "http:", host: "localhost", pathname: "/org_graph.html" },
        localStorage: { getItem: () => null, setItem: () => {}, removeItem: () => {} },
        sessionStorage: { getItem: () => null, setItem: () => {}, removeItem: () => {} },
        document: {
            getElementById: () => null,
            querySelector: () => null,
            querySelectorAll: (selector) => (selector === svgSelector ? rendered : pendingIcons),
            createElement: (tag) => new StubHTMLElement(String(tag || "DIV").toUpperCase()),
            addEventListener: () => {},
            body: new StubHTMLElement("BODY"),
        },
        Element: StubElement,
        HTMLElement: StubHTMLElement,
        URLSearchParams,
        URL,
        AbortController,
        fetch: async () => ({ ok: true, json: async () => ({}) }),
        lucide: {
            createIcons() {
                createIconsCalls += 1;
            },
        },
        marked: { parse: (value) => String(value) },
        DOMPurify: { sanitize: (value) => String(value) },
        performance: { now: () => 0 },
        // 帧回调延迟执行，才能验同一帧内多次 icons() 只排一趟
        requestAnimationFrame: (callback) => { frameQueue.push(callback); return frameQueue.length; },
        cancelAnimationFrame: (id) => { if (id >= 1) frameQueue[id - 1] = () => {}; },
        WebSocket: function WebSocket() {},
        addEventListener() {},
        removeEventListener() {},
    };
    context.window = context;
    vm.createContext(context);
    vm.runInContext(`${APP_CODE}\nthis.__testExports = { icons };`, context);
    const flush = () => {
        const queued = frameQueue.splice(0, frameQueue.length);
        queued.forEach((cb) => cb());
    };
    return {
        api: context.__testExports,
        context,
        flush,
        stats: () => ({ createIconsCalls, pendingCount: pendingIcons.length }),
        addRenderedSvg: (name = "check") => {
            const svg = new StubHTMLElement("svg", { "data-lucide": name });
            rendered.push(svg);
            return svg;
        },
        addPendingIcon: (name = "x-circle") => {
            pendingIcons.push(new StubHTMLElement("i", { "data-lucide": name }));
        },
    };
}

test("icons(force) renders then strips the marker from produced svgs", () => {
    const env = makeContext();
    const svg = env.addRenderedSvg("check");
    env.api.icons(true);
    assert.equal(env.stats().createIconsCalls, 1);
    assert.deepEqual(svg.removed, ["data-lucide"]);
    assert.equal(svg.getAttribute("data-lucide"), null);
});

test("the rAF path strips too and coalesces repeated calls into one pass", () => {
    const env = makeContext();
    const svg = env.addRenderedSvg("copy");
    env.api.icons();
    env.api.icons();
    env.api.icons();
    assert.equal(env.stats().createIconsCalls, 0, "pass must wait for the frame");
    env.flush();
    assert.equal(env.stats().createIconsCalls, 1, "three same-frame calls must collapse to one pass");
    assert.deepEqual(svg.removed, ["data-lucide"]);
});

test("the strip only touches svgs, so unreplaced <i data-lucide> stays pending", () => {
    const env = makeContext();
    const svg = env.addRenderedSvg("check");
    env.addPendingIcon();
    env.api.icons(true);
    assert.deepEqual(svg.removed, ["data-lucide"]);
    assert.equal(env.stats().pendingCount, 1, "待渲染的 <i> 不能被摘掉标记");
});

test("without lucide the pass is a no-op and nothing gets stripped", () => {
    const env = makeContext();
    const svg = env.addRenderedSvg("check");
    delete env.context.lucide;
    assert.doesNotThrow(() => env.api.icons(false));
    env.flush();
    assert.deepEqual(svg.removed, [], "lucide 缺席时不该动属性");
});
