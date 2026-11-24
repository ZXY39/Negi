const test = require("node:test");
const assert = require("node:assert/strict");
const vm = require("node:vm");
const { readFrontendSources } = require("./support/frontend_sources.js");

const TASK_VIEW_CODE = readFrontendSources(["task_view"]).join("\n");
const APP_CODE = readFrontendSources(["app"]).join("\n");

class StubElement {}

class StubHTMLElement extends StubElement {
    constructor() {
        super();
        this.className = "";
        this.hidden = false;
        this.textContent = "";
        this.innerHTML = "";
        this.children = [];
        this.dataset = {};
        this.attributes = {};
        this.style = {};
        this.parentElement = null;
        this.classList = {
            add() {},
            remove() {},
            contains() { return false; },
            toggle() { return false; },
        };
    }

    appendChild(child) {
        child.parentElement = this;
        this.children.push(child);
        return child;
    }

    setAttribute(name, value) {
        this.attributes[name] = String(value);
    }

    querySelector() { return null; }
    querySelectorAll() { return []; }
    addEventListener() {}
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
    vm.createContext(context);
    vm.runInContext(
        `${TASK_VIEW_CODE}
        ${APP_CODE}
        this.__testExports = {
            U,
            execPurposeFromStep,
            toolDisplayTitle,
            renderExecutionRoundToolChip,
            applyCeoToolStepState,
        };`,
        context
    );
    return context.__testExports;
}

// 在飞的工具行：applyCeoToolStepState 只按类名取子节点，桩件按同一份选择器回。
function stubToolRow() {
    const item = new StubHTMLElement();
    const parts = {
        ".interaction-step-title": new StubHTMLElement(),
        ".interaction-step-started": null,
        ".interaction-step-status": new StubHTMLElement(),
        ".tool-status-icon": new StubHTMLElement(),
    };
    item.querySelector = (selector) => (selector in parts ? parts[selector] : null);
    return { item, titleEl: parts[".interaction-step-title"] };
}

const PURPOSE = "启动本地开发服务器并保持运行，后面的验收步骤还要连它";

test("exec 带 purpose 时工具卡标题只留 purpose，不挂 exec: 前缀，并挂上可折行的 has-purpose", () => {
    const { renderExecutionRoundToolChip } = loadApp();
    const step = {
        tool_call_id: "call-1",
        tool_name: "exec",
        status: "success",
        // 账本存的是整份 JSON（frontdoor/turns.py 的 json.dumps(indent=2)）。
        arguments_text: JSON.stringify(
            { command: "python -m http.server 8000", background: true, purpose: PURPOSE },
            null,
            2,
        ),
    };
    const html = renderExecutionRoundToolChip({ round_id: "r1" }, step, 0);
    assert.ok(html.includes(`<span class="task-trace-round-chip-title">${PURPOSE}</span>`), html);
    assert.ok(!html.includes("exec:"), html);
    assert.ok(html.includes("has-purpose"), html);
});

test("摘要档把入参压平成一行后，purpose 仍然读得出来", () => {
    const { execPurposeFromStep } = loadApp();
    const flattened = `{ "command": "python -m http.server 8000", "background": true, "purpose": "${PURPOSE}" }`;
    assert.equal(execPurposeFromStep({ tool_name: "exec", arguments_text: flattened }), PURPOSE);
    // 转义过的引号与换行要还原成人读的那一句。
    assert.equal(
        execPurposeFromStep({ tool_name: "exec", arguments_text: '{"purpose": "keep the \\"server\\" up\\nfor later"}' }),
        'keep the "server" up for later',
    );
});

test("没有 purpose 的 exec、以及别的工具，标题保持原样", () => {
    const { execPurposeFromStep, renderExecutionRoundToolChip } = loadApp();
    assert.equal(execPurposeFromStep({ tool_name: "exec", arguments_text: '{"command": "ls"}' }), "");
    // purpose 是 exec 独有的参数：别的工具即使带同名键也不改标题。
    assert.equal(execPurposeFromStep({ tool_name: "read_file", arguments_text: '{"purpose": "x"}' }), "");

    const html = renderExecutionRoundToolChip(
        { round_id: "r1" },
        { tool_call_id: "call-2", tool_name: "exec", status: "success", arguments_text: '{"command": "ls"}' },
        0,
    );
    assert.ok(html.includes(">exec</span>"), html);
    assert.ok(!html.includes("has-purpose"), html);
});

test("实时帧的 hint 串不是 JSON，不会把截断的参数当成 purpose", () => {
    const { execPurposeFromStep } = loadApp();
    const step = {
        tool_name: "exec",
        // tool_start 帧带的是 _tool_invocation_hint 那条串，purpose 常被 48 字上限砍断。
        arguments_text: "exec (command=python -m http.server 8000, background=True, purpose=启动本地开发服务器并保持运行，后面的验收...)",
    };
    assert.equal(execPurposeFromStep(step), "");
});

test("在飞的工具行用 event 里的 purpose 拼标题，收尾帧不带也不会擦回去", () => {
    const { applyCeoToolStepState } = loadApp();
    const { item, titleEl } = stubToolRow();
    applyCeoToolStepState(item, { status: "running", toolName: "exec", purpose: PURPOSE, toolCallId: "call-1" });
    assert.equal(titleEl.textContent, PURPOSE);
    // 匹配在飞步骤靠的是原始 tool_id，不能跟着标题一起变。
    assert.equal(item.dataset.toolName, "exec");
    assert.equal(item.dataset.toolPurpose, PURPOSE);

    // background-jobs.js 收尾时重贴状态，不带 purpose。
    applyCeoToolStepState(item, { status: "background", toolName: "exec", toolCallId: "call-1" });
    assert.equal(titleEl.textContent, PURPOSE);
});
