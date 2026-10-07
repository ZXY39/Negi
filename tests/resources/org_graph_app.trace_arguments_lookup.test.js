const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const TASK_VIEW_PATH = "g3ku/web/frontend/org_graph_task_view.js";
const APP_PATH = "g3ku/web/frontend/org_graph_app.js";
const TASK_VIEW_CODE = fs.readFileSync(TASK_VIEW_PATH, "utf8");
const APP_CODE = fs.readFileSync(APP_PATH, "utf8");

const MISSING_TEXT = "完整入参已不在账本里，这里只有调用提示";
const LONG_COMMAND = "Get-ChildItem -Path C:\\ab\\h2\\packages -Recurse -Filter package.json | Select-Object -ExpandProperty FullName";
const HINT = "exec (command=Get-ChildItem -Path C:\\ab\\h2\\packa..., timeout_seconds=60)";

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

    querySelector() { return null; }
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

function loadApp({ getCeoToolArguments }) {
    // 入参缓存挂在 S 上，每个用例必须从干净状态起算。
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
        requestAnimationFrame: (callback) => { callback(); return 1; },
        cancelAnimationFrame: () => {},
        WebSocket: function WebSocket() {},
        addEventListener() {},
        removeEventListener() {},
    };
    context.window = context;
    context.ApiClient = {
        getCeoToolArguments,
        getActiveSessionId: () => "web:test",
        getErrorCode: (value) => (value && typeof value === "object" ? String(value.code || "") : ""),
        friendlyErrorMessage: (_value, fallback = "") => String(fallback || ""),
    };
    vm.createContext(context);
    vm.runInContext(
        `${TASK_VIEW_CODE}\n${APP_CODE}
        this.__testExports = {
            S,
            esc: (v) => String(v ?? ""),
            normalizeExecutionStageTrace,
            renderExecutionRoundToolPanel,
            ensureTraceArgumentsCodeBlockContent,
            hydrateTraceOutputBlocks,
        };`,
        context
    );
    return context.__testExports;
}

function httpError(message, status) {
    const error = new Error(message);
    error.status = status;
    return error;
}

// 投影把超长入参清空后的那一行：arguments 空、arguments_text 是 48 字提示、带截断标记。
function cappedStage() {
    return {
        stage_id: "stage:1",
        stage_goal: "enumerate packages",
        rounds: [{
            round_id: "round:1",
            round_index: 1,
            created_at: "2026-10-07T10:44:25",
            tools: [{
                tool_call_id: "call-42",
                tool_name: "exec",
                status: "success",
                arguments: {},
                arguments_text: HINT,
                arguments_truncated: true,
                output_ref: "artifact:tool-output-1",
            }],
        }],
    };
}

function argsBlock(lookupId, text = HINT) {
    const element = new StubHTMLElement();
    element.className = "code-block task-trace-code";
    element.textContent = text;
    if (lookupId) element.dataset.argumentsLookup = lookupId;
    element.dataset.emptyText = "无参数";
    return element;
}

test("面板只对被清空的那一行留按需取档标记", () => {
    const { normalizeExecutionStageTrace, renderExecutionRoundToolPanel } = loadApp({
        getCeoToolArguments: async () => { throw httpError("unused", 500); },
    });

    const capped = normalizeExecutionStageTrace(cappedStage(), 0);
    const cappedHtml = renderExecutionRoundToolPanel(capped.rounds[0], capped.rounds[0].tools[0], 0);
    assert.match(cappedHtml, /data-arguments-lookup="call-42"/);
    assert.match(cappedHtml, new RegExp(HINT.replace(/[\\.*+?^${}()|[\]\\]/g, "\\$&")));

    // 内联入参还在的时候不该多发一次请求：直接用对象渲染全量。
    const inline = normalizeExecutionStageTrace({
        stage_id: "stage:2",
        rounds: [{
            round_id: "round:1",
            tools: [{ tool_call_id: "call-7", tool_name: "exec", status: "success", arguments: { command: LONG_COMMAND } }],
        }],
    }, 0);
    const inlineHtml = renderExecutionRoundToolPanel(inline.rounds[0], inline.rounds[0].tools[0], 0);
    assert.doesNotMatch(inlineHtml, /data-arguments-lookup/);
    assert.match(inlineHtml, /Get-ChildItem -Path C:/);
});

test("点开工具面板时按 tool_call_id 回取全量入参，重绘只发一次", async () => {
    let calls = 0;
    const { ensureTraceArgumentsCodeBlockContent } = loadApp({
        getCeoToolArguments: async (sessionId, toolCallId) => {
            calls += 1;
            assert.equal(sessionId, "web:test");
            assert.equal(toolCallId, "call-42");
            return { ok: true, arguments_text: JSON.stringify({ command: LONG_COMMAND, timeout_seconds: 60 }, null, 2) };
        },
    });

    // 面板每帧重建 code 块，所以去重按 tool_call_id 而不是按元素。
    for (let render = 0; render < 4; render += 1) {
        await ensureTraceArgumentsCodeBlockContent(argsBlock("call-42"));
    }
    assert.equal(calls, 1);
    const element = argsBlock("call-42");
    const text = await ensureTraceArgumentsCodeBlockContent(element);
    assert.match(element.textContent, /Get-ChildItem -Path C:/);
    assert.match(element.textContent, /timeout_seconds/);
    assert.doesNotMatch(element.textContent, /\.\.\.\)/);
    assert.equal(element.dataset.outputHydrated, "true");
    assert.match(text, /command/);
});

test("账本里已经没有原文时退回提示并说明原因", async () => {
    let calls = 0;
    const { ensureTraceArgumentsCodeBlockContent } = loadApp({
        getCeoToolArguments: async () => {
            calls += 1;
            throw httpError("HTTP 404", 404);
        },
    });

    const element = argsBlock("call-gone");
    const returned = await ensureTraceArgumentsCodeBlockContent(element);

    assert.match(element.textContent, /exec \(command=/);
    assert.match(element.textContent, new RegExp(MISSING_TEXT));
    assert.doesNotMatch(element.textContent, /加载完整参数失败/);
    assert.equal(element.dataset.outputHydrated, "cleaned");
    assert.equal(returned, HINT);
    // 终态：换一个新元素重绘不再碰网络。
    await ensureTraceArgumentsCodeBlockContent(argsBlock("call-gone"));
    assert.equal(calls, 1);
});

test("一次传输失败仍可重试，重试成功后换成全量", async () => {
    let calls = 0;
    const { ensureTraceArgumentsCodeBlockContent } = loadApp({
        getCeoToolArguments: async () => {
            calls += 1;
            if (calls === 1) throw httpError("HTTP 503", 503);
            return { arguments_text: "{\"command\": \"full\"}" };
        },
    });

    const failing = argsBlock("call-9");
    await ensureTraceArgumentsCodeBlockContent(failing);
    assert.match(failing.textContent, /加载完整参数失败/);
    assert.equal(failing.dataset.outputHydrated, "error");

    const reloaded = argsBlock("call-9");
    const text = await ensureTraceArgumentsCodeBlockContent(reloaded);
    assert.equal(calls, 2);
    assert.equal(text, '{"command": "full"}');
});
