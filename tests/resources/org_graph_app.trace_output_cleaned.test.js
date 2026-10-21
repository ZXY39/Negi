const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const { readFrontendSources } = require("./support/frontend_sources.js");

const TASK_VIEW_PATH = "g3ku/web/frontend/org_graph_task_view.js";
const APP_PATH = "g3ku/web/frontend/org_graph_app.js";
const CSS_PATH = "g3ku/web/frontend/org_graph.css";
const TASK_VIEW_CODE = readFrontendSources(["task_view"]).join("\n");
const APP_CODE = readFrontendSources(["app"]).join("\n");

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

    matchesClassName(name) {
        return String(this.className || "").split(/\s+/).includes(name);
    }

    closest(selector) {
        const wanted = String(selector || "").replace(/^\./, "");
        let node = this;
        while (node instanceof StubHTMLElement) {
            if (node.matchesClassName(wanted)) return node;
            node = node.parentElement;
        }
        return null;
    }

    contains(node) {
        let cursor = node;
        while (cursor) {
            if (cursor === this) return true;
            cursor = cursor.parentElement;
        }
        return false;
    }

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

function loadApp({ readContent, openContent, getToolArguments } = {}) {
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
        openContent: openContent || readContent,
        getCeoToolArguments: getToolArguments,
        getErrorCode: (value) => (value && typeof value === "object" ? String(value.code || "") : ""),
        friendlyErrorMessage: (_value, fallback = "") => String(fallback || ""),
    };
    vm.createContext(context);
    vm.runInContext(
        `${TASK_VIEW_CODE}\n${APP_CODE}\nthis.__testExports = { ensureTraceOutputCodeBlockContent, ensureTraceArgumentsCodeBlockContent, ensureCeoToolStepFullOutput, getTraceOutputContentByRef, getTraceOutputWindowByRef, loadMoreTraceOutputBlock, traceContentTextUrl, capTraceDisplayText, renderTraceOutputField, toolArgumentsFullText, TRACE_DISPLAY_CHAR_LIMIT };`,
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

const ARTIFACT_REF = "artifact:e9f4b91c3391";

function outputField(ref = ARTIFACT_REF) {
    const field = new StubHTMLElement();
    field.className = "task-trace-field";
    const block = new StubHTMLElement();
    block.className = "code-block task-trace-code task-trace-output-value";
    block.dataset.outputRef = ref;
    block.dataset.emptyText = "暂无内容";
    block.textContent = "预览片段";
    const button = new StubHTMLElement();
    button.className = "task-trace-copy task-trace-more";
    button.hidden = true;
    field.appendChild(block);
    field.appendChild(button);
    return { field, block, button };
}

test("点开输出只向服务端要一屏，不再拿整份", async () => {
    const calls = { read: 0, open: [] };
    const { ensureTraceOutputCodeBlockContent } = loadApp({
        readContent: async () => {
            calls.read += 1;
            return { content: "整份正文".repeat(4000) };
        },
        openContent: async ({ ref, startLine, endLine }) => {
            calls.open.push({ ref, startLine, endLine });
            return { excerpt: "第一段正文", start_line: 1, end_line: 84, truncated: true };
        },
    });

    const { block, button } = outputField();
    const text = await ensureTraceOutputCodeBlockContent(block);

    assert.equal(calls.read, 0);
    // 首屏必须两个行参数都不传：navigation.open() 一见 start_line 就走区间分支，
    // 缺 end_line 时把 finish 收成 start —— 只回一行。
    assert.deepEqual(calls.open, [{ ref: ARTIFACT_REF, startLine: null, endLine: null }]);
    assert.equal(text, "第一段正文");
    assert.equal(block.dataset.outputEndLine, "84");
    assert.equal(block.dataset.outputWindowSpan, "84");
    assert.equal(button.hidden, false);
});

test("继续加载按首屏那一屏的行宽接下一段", async () => {
    const asked = [];
    const { ensureTraceOutputCodeBlockContent, loadMoreTraceOutputBlock } = loadApp({
        openContent: async ({ startLine, endLine }) => {
            asked.push({ startLine, endLine });
            return startLine
                ? { excerpt: "第二段", start_line: 85, end_line: 168, line_count: 420, truncated: false }
                : { excerpt: "第一段", start_line: 1, end_line: 84, line_count: 420, truncated: true };
        },
    });

    const { block, button } = outputField();
    await ensureTraceOutputCodeBlockContent(block);
    await loadMoreTraceOutputBlock(button);

    // 跨度用服务端首屏自己给出的那 84 行，前端不复制 MAX_OPEN_LINES。
    assert.deepEqual(asked[1], { startLine: 85, endLine: 168 });
    assert.equal(block.textContent, "第一段\n第二段");
    assert.equal(block.dataset.outputEndLine, "168");
    // truncated=false 只说"这一屏发满了"，420 行才读到 168 行时按钮不许消失。
    assert.equal(button.hidden, false);
});

test("读到 line_count 那一行才收起继续加载", async () => {
    const { ensureTraceOutputCodeBlockContent, loadMoreTraceOutputBlock } = loadApp({
        openContent: async ({ startLine }) => (startLine
            ? { excerpt: "第二段", start_line: 85, end_line: 420, line_count: 420, truncated: false }
            : { excerpt: "第一段", start_line: 1, end_line: 84, line_count: 420, truncated: true }),
    });

    const { block, button } = outputField();
    await ensureTraceOutputCodeBlockContent(block);
    await loadMoreTraceOutputBlock(button);

    assert.equal(button.hidden, true);
});

test("单行巨型 artifact 的行窗推不动，继续加载保持隐藏", async () => {
    const { ensureTraceOutputCodeBlockContent } = loadApp({
        openContent: async () => ({ excerpt: "前 15600 字符", start_line: 1, end_line: 1, truncated: true }),
    });

    const { block, button } = outputField();
    await ensureTraceOutputCodeBlockContent(block);

    // 再点一次只会拿回同一段，所以这里必须没有第二次请求。
    assert.equal(button.hidden, true);
});

test("归档参数按一屏封顶显示，复制仍拿全文", () => {
    const { capTraceDisplayText, toolArgumentsFullText, TRACE_DISPLAY_CHAR_LIMIT } = loadApp({});
    const big = "y".repeat(TRACE_DISPLAY_CHAR_LIMIT + 5000);

    const shown = toolArgumentsFullText({ arguments: { command: big } });

    assert.ok(shown.length < big.length, `capped ${shown.length} should be under ${big.length}`);
    assert.match(shown, /查看完整/);
    assert.equal(capTraceDisplayText("short"), "short");
});

test("查看完整走纯文本路由并转义 ref", () => {
    const { traceContentTextUrl } = loadApp({});

    assert.equal(traceContentTextUrl(ARTIFACT_REF), "/api/content/text?ref=artifact%3Ae9f4b91c3391");
    assert.equal(traceContentTextUrl(""), "");
    const encoded = traceContentTextUrl("path:temp/中 文报告.txt");
    assert.ok(encoded.includes(encodeURIComponent("中 文报告")));
});

function cssRuleBody(selectorText) {
    const css = fs.readFileSync(CSS_PATH, "utf8");
    const at = css.indexOf(`${selectorText} {`);
    assert.notEqual(at, -1, `CSS 里找不到 ${selectorText} 规则`);
    const open = css.indexOf("{", at);
    return css.slice(open + 1, css.indexOf("}", open));
}

test("标签行的动作按钮成组靠右，不随颗粒数被摊开", () => {
    // 两颗粒件时 space-between 看不出来；加到第三、第四颗（查看完整 / 继续加载）
    // 就把它们沿整行平均摊开。剩余宽度归标签，按钮才成组。
    const row = cssRuleBody(".task-trace-label-row");
    const label = cssRuleBody(".task-trace-label");

    assert.doesNotMatch(row, /justify-content:\s*space-between/, "标签行仍按 space-between 平摊动作按钮");
    assert.match(label, /margin-right:\s*auto/, "标签没有吃掉剩余宽度");
});
