const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const TASK_VIEW_PATH = "g3ku/web/frontend/org_graph_task_view.js";
const APP_PATH = "g3ku/web/frontend/org_graph_app.js";
const TASK_VIEW_CODE = fs.readFileSync(TASK_VIEW_PATH, "utf8");
const APP_CODE = fs.readFileSync(APP_PATH, "utf8");

const ARCHIVE_REF = "C:/data/.g3ku/temp/sessions/web-ceo-x/g3ku_stage_archive_1_abcd1234.json";

class StubElement {}
class StubHTMLElement extends StubElement {
    constructor(className = "") {
        super();
        this.className = className;
        this.hidden = false;
        this.disabled = false;
        this.textContent = "";
        this.innerHTML = "";
        this.dataset = {};
        this.style = {};
        this.attributes = {};
        this._selectors = {};
        this._selectorLists = {};
        this._children = [];
        this.parentElement = null;
        const classes = () => new Set(String(this.className || "").split(/\s+/).filter(Boolean));
        const write = (set) => { this.className = [...set].join(" "); };
        this.classList = {
            add: (...tokens) => { const set = classes(); tokens.forEach((t) => set.add(t)); write(set); },
            remove: (...tokens) => { const set = classes(); tokens.forEach((t) => set.delete(t)); write(set); },
            contains: (token) => classes().has(token),
        };
    }

    querySelector(selector) { return this._selectors[selector] || null; }
    querySelectorAll(selector) { return this._selectorLists[selector] || []; }
    addEventListener() {}
    closest(selector) { return this._closest?.[selector] || null; }
    insertAdjacentHTML(_position, html) { this.innerHTML += html; }
    appendChild(child) {
        child.parentElement = this;
        this._children.push(child);
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

function loadApp(readContent) {
    const context = {
        console,
        setTimeout,
        clearTimeout,
        setInterval,
        clearInterval,
        queueMicrotask,
        JSON,
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
        getActiveSessionId: () => "web:test",
        readContent,
        getCeoToolArguments: async () => ({ arguments_text: "" }),
    };
    vm.createContext(context);
    vm.runInContext(
        `${TASK_VIEW_CODE}\n${APP_CODE}
        this.__testExports = {
            renderExecutionStageRounds,
            getCeoStageArchiveHtml,
            bindStageArchiveOpens,
        };`,
        context,
    );
    return context.__testExports;
}

function archiveDocument({ rounds, summary = "阶段收口总结：权限判定入口在 guard 包。" } = {}) {
    return JSON.stringify({
        kind: "frontdoor_stage_eviction",
        owner: "web:ceo-x",
        created_at: "2026-10-08T02:11:00+00:00",
        stage_count: 1,
        stages: [
            {
                stage_id: "frontdoor-stage-4",
                stage_index: 4,
                stage_goal: "D4 权限与审批",
                status: "completed",
                representation: "compact",
                context_evicted: true,
                completed_stage_summary: summary,
                rounds,
            },
        ],
    });
}

const ARCHIVE_ROUNDS = [
    {
        round_index: 1,
        created_at: "2026-10-08T02:09:00+00:00",
        tools: [
            {
                tool_call_id: "call-a:1",
                tool_name: "exec",
                status: "success",
                arguments: { command: "$n = @(Get-ChildItem 'C:\\ab\\h2\\packages' -Recurse)" },
                arguments_text: "exec (command=$n = @(Get-ChildItem 'C:\\ab\\h2\\packages' -Recurs...",
                output_text: "3 items",
            },
        ],
    },
    {
        round_index: 2,
        created_at: "2026-10-08T02:10:00+00:00",
        tools: [
            {
                tool_call_id: "call-b:2",
                tool_name: "read_file",
                status: "success",
                arguments: { path: "main/auth.py" },
                arguments_text: "read_file (path=main/auth.py)",
                output_text: "def guard(): ...",
            },
        ],
    },
];

const flush = () => new Promise((resolve) => setTimeout(resolve, 0));

function archiveEntry(ref = ARCHIVE_REF) {
    const host = new StubHTMLElement("task-trace-stage-archive");
    host.dataset.stageArchive = ref;
    const button = new StubHTMLElement("task-trace-stage-archive-btn");
    button.dataset.stageArchiveOpen = ref;
    button._closest = { ".task-trace-stage-archive": host, "[data-stage-archive-open]": button };
    return { host, button };
}

function boundScope() {
    const scope = new StubHTMLElement("task-trace-list");
    scope._listeners = {};
    scope.addEventListener = (type, handler) => { scope._listeners[type] = handler; };
    return scope;
}

test("裁撤阶段没带正文时画取档入口，并带上归档指针", () => {
    const { renderExecutionStageRounds } = loadApp(async () => ({ content: "" }));
    const html = renderExecutionStageRounds({
        rounds: [],
        rounds_archive_ref: ARCHIVE_REF,
        completed_stage_summary: "收口总结。",
    });

    assert.match(html, /class="task-trace-stage-archive"/);
    assert.match(html, new RegExp(`data-stage-archive-open="${ARCHIVE_REF.replaceAll("/", "\\/")}"`));
    assert.match(html, /取回本阶段的调用记录/);
    // 指针在场就不该再骗用户"暂无工具轮次"——原文是取得回来的。
    assert.doesNotMatch(html, /当前阶段暂无工具轮次/);
    assert.match(html, /收口总结/);
});

test("没有归档可取的空阶段仍画占位，不画点了没反应的入口", () => {
    const { renderExecutionStageRounds } = loadApp(async () => ({ content: "" }));
    const html = renderExecutionStageRounds({ rounds: [] });

    assert.match(html, /当前阶段暂无工具轮次/);
    assert.doesNotMatch(html, /data-stage-archive-open/);
});

test("取档把归档里的轮次与全量入参画出来，并按指针缓存只请求一次", async () => {
    let calls = 0;
    const { getCeoStageArchiveHtml } = loadApp(async () => {
        calls += 1;
        return { content: archiveDocument({ rounds: ARCHIVE_ROUNDS }) };
    });

    const html = await getCeoStageArchiveHtml(ARCHIVE_REF);

    assert.match(html, /data-round-key="1"/);
    assert.match(html, /data-round-key="2"/);
    const titles = html.match(/task-trace-round-chip-title/g) || [];
    assert.equal(titles.length, 2);
    assert.match(html, /exec/);
    // 归档存的是未裁原文：面板直接印 arguments 复原的全量入参，不再挂回取车道。
    assert.match(html, /-Recurse/);
    // 48 字提示只是模型-facing 的截断，取回来的原文不该长那样。
    assert.doesNotMatch(html, /-Recurs\.\.\./);
    assert.doesNotMatch(html, /data-arguments-lookup/);
    // 总结不重复画：卡片自己那半已经有一份。
    assert.doesNotMatch(html, /阶段收口总结/);

    await getCeoStageArchiveHtml(ARCHIVE_REF);
    assert.equal(calls, 1);
});

test("归档已被清理时走终态文案，且不把失败缓存成永久", async () => {
    let calls = 0;
    const missing = Object.assign(new Error("path not found"), { status: 404 });
    const { getCeoStageArchiveHtml } = loadApp(async () => {
        calls += 1;
        throw calls === 1 ? missing : { status: 500, message: "boom" };
    });

    await assert.rejects(() => getCeoStageArchiveHtml(ARCHIVE_REF), (error) => error.status === 404);
    assert.equal(calls, 1);
    // 失败不留表：下一次点开还得再取一次（成功的那次才配缓存）。
    await assert.rejects(() => getCeoStageArchiveHtml(ARCHIVE_REF));
    assert.equal(calls, 2);
});

test("点开后归档轮次填进原入口，404 只留终态文案不留重试", async () => {
    const { bindStageArchiveOpens } = loadApp(async () => ({ content: archiveDocument({ rounds: ARCHIVE_ROUNDS }) }));
    const scope = boundScope();
    bindStageArchiveOpens(scope);
    const { host, button } = archiveEntry();

    scope._listeners.click({ target: button });
    await flush();

    assert.equal(button.disabled, true);
    assert.equal(host.classList.contains("is-filled"), true);
    assert.match(host.innerHTML, /data-round-key="1"/);
    assert.match(host.innerHTML, /data-round-key="2"/);
    assert.doesNotMatch(host.innerHTML, /data-stage-archive-open/);
});

test("取档失败（非 404）留下重试入口", async () => {
    const { bindStageArchiveOpens } = loadApp(async () => {
        throw Object.assign(new Error("接口暂时不可用"), { status: 503 });
    });
    const scope = boundScope();
    bindStageArchiveOpens(scope);
    const { host, button } = archiveEntry();

    scope._listeners.click({ target: button });
    await flush();

    assert.match(host.innerHTML, /取档失败：接口暂时不可用/);
    const retryButtons = host.innerHTML.match(/data-stage-archive-open/g) || [];
    assert.equal(retryButtons.length, 1);
});

test("归档空文档按 404 终态处理，不画半张卡", async () => {
    const { bindStageArchiveOpens } = loadApp(async () => ({ content: archiveDocument({ rounds: [] }) }));
    const scope = boundScope();
    bindStageArchiveOpens(scope);
    const { host, button } = archiveEntry();

    scope._listeners.click({ target: button });
    await flush();

    assert.match(host.innerHTML, /归档已被清理/);
    assert.doesNotMatch(host.innerHTML, /data-stage-archive-open/);
});
