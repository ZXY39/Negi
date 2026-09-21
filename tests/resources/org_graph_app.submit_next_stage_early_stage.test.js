const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

// submit_next_stage 的结果体就是新阶段的完整头信息：前端要在这一帧立刻切到
// 阶段显示，而不是继续挂工具卡片等下一个 canonical 增量帧。

const APP_PATH = "g3ku/web/frontend/org_graph_app.js";
const APP_CODE = fs.readFileSync(APP_PATH, "utf8");

class StubElement {}
class StubHTMLElement extends StubElement {
    constructor() {
        super();
        this.className = "";
        this.hidden = false;
        this.textContent = "";
        this._innerHTML = "";
        this.children = [];
        this.classList = {
            add: () => {},
            remove: () => {},
            contains: () => false,
        };
    }

    appendChild(child) {
        this.children.push(child);
        return child;
    }

    querySelector() {
        return null;
    }

    querySelectorAll() {
        return [];
    }

    addEventListener() {}

    set innerHTML(value) {
        this._innerHTML = String(value);
    }

    get innerHTML() {
        return this._innerHTML;
    }
}

class StubHTMLButtonElement extends StubHTMLElement {}
class StubHTMLInputElement extends StubHTMLElement {}
class StubHTMLTextAreaElement extends StubHTMLElement {}
class StubHTMLSelectElement extends StubHTMLElement {}

class StubDocument {
    getElementById() {
        return null;
    }

    querySelector() {
        return null;
    }

    querySelectorAll() {
        return [];
    }

    addEventListener() {}

    createElement() {
        return new StubHTMLElement();
    }
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
        requestAnimationFrame: (callback) => {
            callback();
            return 1;
        },
        cancelAnimationFrame: () => {},
        WebSocket: function WebSocket() {},
        addEventListener() {},
        removeEventListener() {},
    };
    context.window = context;
    vm.createContext(context);
    vm.runInContext(
        `${APP_CODE}\nthis.__exports = { extractCeoSubmittedStageContext };`,
        context
    );
    return context.__exports;
}

const { extractCeoSubmittedStageContext } = loadApp();

const stagePayload = JSON.stringify({
    stage_id: "frontdoor-stage-441",
    stage_index: 441,
    stage_goal: "读取 resume_cn_optimizer skill 全文，补入背景色区分度检查项",
    stage_kind: "normal",
    status: "active",
    system_generated: false,
    tool_round_budget: 3,
    tool_rounds_used: 0,
    rounds: [],
    preamble_text: "",
    completed_stage_summary: "",
    final_stage: false,
    key_refs: [],
});

test("submit_next_stage 的结果帧被当成阶段头，而不是工具卡片", () => {
    const out = extractCeoSubmittedStageContext("submit_next_stage", { text: stagePayload });
    assert.ok(out, "expected a stage context");
    assert.equal(out.stages.length, 1);
    assert.equal(out.stages[0].stage_id, "frontdoor-stage-441");
    assert.equal(out.stages[0].tool_round_budget, 3);
});

test("非 submit_next_stage 的工具即使输出同构也不切换显示模式", () => {
    assert.equal(extractCeoSubmittedStageContext("filesystem_read", { text: stagePayload }), null);
});

test("预览被截断的 JSON 退回普通工具卡片", () => {
    assert.equal(extractCeoSubmittedStageContext("submit_next_stage", { text: stagePayload.slice(0, 80) }), null);
});

test("没有阶段标题的头信息不算阶段", () => {
    const bare = JSON.stringify({ stage_id: "frontdoor-stage-9", stage_index: 9, status: "active", rounds: [] });
    assert.equal(extractCeoSubmittedStageContext("submit_next_stage", { text: bare }), null);
});

test("text 不是 JSON 时回退到预览字段", () => {
    const out = extractCeoSubmittedStageContext("submit_next_stage", {
        text: "not json",
        output_preview_text: stagePayload,
    });
    assert.equal(out?.stages?.[0]?.stage_id, "frontdoor-stage-441");
});
