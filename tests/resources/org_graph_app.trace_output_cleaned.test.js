const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const TASK_VIEW_PATH = "g3ku/web/frontend/org_graph_task_view.js";
const APP_PATH = "g3ku/web/frontend/org_graph_app.js";
const TASK_VIEW_CODE = fs.readFileSync(TASK_VIEW_PATH, "utf8");
const APP_CODE = fs.readFileSync(APP_PATH, "utf8");

const DEAD_REF = "path:temp/tasks/task_c7f1dbfae6e2/verify6_out.txt";
const CLEANED_TEXT = "完整输出已被清理，仅保留预览片段";

class StubElement {}
class StubHTMLElement extends StubElement {
    constructor() {
        super();
        this.hidden = false;
        this.textContent = "";
        this.innerHTML = "";
        this.className = "";
        this.dataset = {};
        this.style = {};
        this.attributes = {};
        this.children = [];
        this.parentElement = null;
        this.scrollTop = 0;
        this.classList = {
            add() {},
            remove() {},
            contains() { return false; },
            toggle() { return false; },
        };
    }

    querySelector(selector) {
        const wanted = String(selector || "").replace(/^\./, "");
        for (const child of this.children) {
            if (String(child.className || "").split(/\s+/).includes(wanted)) return child;
            const nested = child.querySelector ? child.querySelector(selector) : null;
            if (nested) return nested;
        }
        return null;
    }

    querySelectorAll() { return []; }
    addEventListener() {}

    appendChild(child) {
        child.parentElement = this;
        this.children.push(child);
        return child;
    }
}
class StubHTMLButtonElement extends StubHTMLElement {}
class StubHTMLInputElement extends StubHTMLElement {}
class StubHTMLTextAreaElement extends StubHTMLElement {}
class StubHTMLSelectElement extends StubHTMLElement {}

class StubDocument {
    getElementById() { return null; }
    createElement() { return new StubHTMLElement(); }
    querySelector() { return null; }
    querySelectorAll() { return []; }
    addEventListener() {}
}

function loadApp({ readContent }) {
    // 每个用例重新执行一遍源码:输出缓存挂在 S 上,必须从干净状态起算。
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
        window: {},
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
        requestAnimationFrame: (callback) => { callback(); return 1; },
        cancelAnimationFrame: () => {},
        WebSocket: function WebSocket() {},
        addEventListener() {},
        removeEventListener() {},
    };
    context.window = context;
    context.ApiClient = {
        readContent,
        openContent: readContent,
        getErrorCode: (value) => (value && typeof value === "object" ? String(value.code || "") : ""),
        friendlyErrorMessage: (_value, fallback = "") => String(fallback || ""),
    };
    vm.createContext(context);
    vm.runInContext(
        `${TASK_VIEW_CODE}\n${APP_CODE}\nthis.__testExports = { ensureTraceOutputCodeBlockContent, ensureCeoToolStepFullOutput, getTraceOutputContentByRef };`,
        context
    );
    return context.__testExports;
}

function httpError(message, status) {
    const error = new Error(message);
    error.status = status;
    if (status === 404) error.code = "content_not_found";
    return error;
}

function outputBlock(text = "preview snippet") {
    const element = new StubHTMLElement();
    element.className = "code-block task-trace-output-value";
    element.textContent = text;
    element.dataset.outputRef = DEAD_REF;
    element.dataset.emptyText = "暂无内容";
    return element;
}

test("a cleaned output ref costs one request across re-renders", async () => {
    let calls = 0;
    const { ensureTraceOutputCodeBlockContent } = loadApp({
        readContent: async () => {
            calls += 1;
            throw httpError("HTTP 404", 404);
        },
    });

    // 面板每次渲染都会重建 code 块,所以去重必须按 ref 而不是按元素。
    for (let render = 0; render < 5; render += 1) {
        await ensureTraceOutputCodeBlockContent(outputBlock());
    }
    assert.equal(calls, 1);
});

test("a cleaned output ref renders a neutral placeholder, not an error line", async () => {
    const { ensureTraceOutputCodeBlockContent } = loadApp({
        readContent: async () => { throw httpError("HTTP 404", 404); },
    });

    const element = outputBlock("预览片段");
    const returned = await ensureTraceOutputCodeBlockContent(element);

    assert.match(element.textContent, /预览片段/);
    assert.match(element.textContent, new RegExp(CLEANED_TEXT));
    assert.doesNotMatch(element.textContent, /加载完整输出失败/);
    assert.equal(element.dataset.outputHydrated, "cleaned");
    // 复制出来的是纯预览正文，不带占位说明。
    assert.equal(returned, "预览片段");
});

test("a transient content read failure stays retryable", async () => {
    let calls = 0;
    const { ensureTraceOutputCodeBlockContent } = loadApp({
        readContent: async () => {
            calls += 1;
            if (calls === 1) throw httpError("HTTP 503", 503);
            return { content: "完整正文" };
        },
    });

    const failing = outputBlock();
    await ensureTraceOutputCodeBlockContent(failing);
    assert.match(failing.textContent, /加载完整输出失败/);
    assert.equal(failing.dataset.outputHydrated, "error");

    const reloaded = outputBlock();
    const text = await ensureTraceOutputCodeBlockContent(reloaded);
    assert.equal(calls, 2);
    assert.equal(text, "完整正文");
    assert.equal(reloaded.dataset.outputHydrated, "true");
});

test("a live output ref is fetched once and served from cache", async () => {
    let calls = 0;
    const { getTraceOutputContentByRef } = loadApp({
        readContent: async ({ ref }) => {
            calls += 1;
            return { content: `body of ${ref}` };
        },
    });

    const first = await getTraceOutputContentByRef(DEAD_REF);
    const second = await getTraceOutputContentByRef(DEAD_REF);
    assert.equal(first, `body of ${DEAD_REF}`);
    assert.equal(second, first);
    assert.equal(calls, 1);
});

function ceoStepItem(previewText = "预览片段") {
    const item = new StubHTMLElement();
    item.className = "interaction-step";
    const preview = new StubHTMLElement();
    preview.className = "interaction-step-preview";
    const detail = new StubHTMLElement();
    detail.className = "interaction-step-detail";
    detail.textContent = previewText;
    item.appendChild(preview);
    item.appendChild(detail);
    item.dataset.outputRef = DEAD_REF;
    item.dataset.detailText = previewText;
    item.dataset.previewDetailText = previewText;
    return { item, detail };
}

test("the CEO feed shows the cleaned placeholder and refetches once", async () => {
    let calls = 0;
    const { ensureCeoToolStepFullOutput } = loadApp({
        readContent: async () => {
            calls += 1;
            throw httpError("HTTP 404", 404);
        },
    });

    const { item, detail } = ceoStepItem();
    const returned = await ensureCeoToolStepFullOutput(item);

    assert.equal(item.dataset.outputHydrated, "cleaned");
    assert.match(item.dataset.detailText, new RegExp(CLEANED_TEXT));
    assert.doesNotMatch(item.dataset.detailText, /加载完整输出失败/);
    assert.match(detail.textContent, new RegExp(CLEANED_TEXT));
    // 喂给复制/摘要的是纯预览正文。
    assert.equal(returned, "预览片段");

    // 回合重放同一个 step 时不再打网络。
    const replayed = ceoStepItem();
    await ensureCeoToolStepFullOutput(replayed.item);
    assert.equal(calls, 1);
});
