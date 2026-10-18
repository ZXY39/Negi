const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const APP_PATH = "g3ku/web/frontend/org_graph_app.js";
const APP_CODE = fs.readFileSync(APP_PATH, "utf8");

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
        this.classList = {
            add: (...tokens) => {
                const classes = new Set(String(this.className || "").split(/\s+/).filter(Boolean));
                tokens.forEach((token) => classes.add(token));
                this.className = [...classes].join(" ");
            },
            remove: (...tokens) => {
                const classes = new Set(String(this.className || "").split(/\s+/).filter(Boolean));
                tokens.forEach((token) => classes.delete(token));
                this.className = [...classes].join(" ");
            },
            contains: (token) => String(this.className || "").split(/\s+/).includes(token),
            toggle: (token, force) => {
                const shouldAdd = force === undefined ? !this.classList.contains(token) : !!force;
                if (shouldAdd) this.classList.add(token);
                else this.classList.remove(token);
            },
        };
    }

    appendChild(child) {
        this.children.push(child);
        return child;
    }

    setAttribute(name, value) {
        this.attributes[name] = String(value);
    }

    querySelector() {
        return null;
    }

    addEventListener() {}
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
        `${APP_CODE}
        this.__testExports = { S, U, createPendingCeoTurn, maybeShowCeoContextLoadNotice, showCeoContextLoadNotice, resolveCeoContextLoadNoticeRiskLevel, isCeoContextLoaderToolName, ceoContextLoaderKind };`,
        context
    );
    context.__testExports.U.ceoContextLoadNotice = new StubHTMLElement();
    return {
        ...context.__testExports,
        __context: context,
    };
}

test("context load notice supports v2 tool loaders", () => {
    const { S, createPendingCeoTurn, maybeShowCeoContextLoadNotice, U } = loadApp();
    const turn = createPendingCeoTurn("user");
    S.ceoPendingTurns = [turn];

    const shown = maybeShowCeoContextLoadNotice(turn, {
        toolName: "load_tool_context_v2",
        status: "success",
        detailTexts: ['{"tool_id":"filesystem_write"}'],
    });

    assert.equal(shown, true);
    assert.equal(U.ceoContextLoadNotice.children.length, 1);
    assert.equal(U.ceoContextLoadNotice.children[0].dataset.noticeKind, "tool");
    assert.equal(U.ceoContextLoadNotice.children[0].children[0].innerHTML, '<i data-lucide="wrench"></i>');
});

test("context load notice renders type icon before text and keeps risk dot metadata", () => {
    const { showCeoContextLoadNotice, U } = loadApp();

    showCeoContextLoadNotice("已加载 skill find-skills", {
        kind: "skill",
        riskLevel: "high",
    });

    assert.equal(U.ceoContextLoadNotice.children.length, 1);
    const notice = U.ceoContextLoadNotice.children[0];
    assert.equal(notice.children[0].className, "ceo-context-load-notice-kind-icon");
    assert.equal(notice.children[1].className, "ceo-context-load-notice-text");
    assert.equal(notice.children[2].className, "ceo-context-load-notice-risk-dot risk-high");
    assert.equal(notice.children[0].innerHTML, '<i data-lucide="sparkles"></i>');
});

test("risk level resolves from the tool record that owns the target id", () => {
    const { S, resolveCeoContextLoadNoticeRiskLevel } = loadApp();
    // 资源目录一行一个工具：风险度直接读工具记录的 risk_level，
    // 没有 actions / executor_names 中间层可以回查。
    S.tools = [
        { tool_id: "filesystem_edit", risk_level: "high" },
        { tool_id: "filesystem_stat", risk_level: "low" },
        { tool_id: "content_open", risk_level: "low" },
        { tool_id: "memory_runtime", risk_level: "" },
    ];
    S.skills = [{ skill_id: "find-skills", risk_level: "high" }];

    assert.equal(resolveCeoContextLoadNoticeRiskLevel("tool", "filesystem_edit"), "high");
    assert.equal(resolveCeoContextLoadNoticeRiskLevel("tool", "filesystem_stat"), "low");
    assert.equal(resolveCeoContextLoadNoticeRiskLevel("tool", "content_open"), "low");
    // 目录里没有风险度的记录回落 medium，而不是去族里取最高。
    assert.equal(resolveCeoContextLoadNoticeRiskLevel("tool", "memory_runtime"), "medium");
    // 旧工具族 id 已经不是任何一条记录，不再从 action 汇总出风险度。
    assert.equal(resolveCeoContextLoadNoticeRiskLevel("tool", "filesystem"), "medium");
    assert.equal(resolveCeoContextLoadNoticeRiskLevel("tool", "content"), "medium");
    assert.equal(resolveCeoContextLoadNoticeRiskLevel("tool", "missing_tool"), "medium");
    assert.equal(resolveCeoContextLoadNoticeRiskLevel("skill", "find-skills"), "high");
    assert.equal(resolveCeoContextLoadNoticeRiskLevel("skill", "missing_skill"), "medium");
});

test("context load notice renders the risk level of the tool record it loaded", () => {
    const { S, createPendingCeoTurn, maybeShowCeoContextLoadNotice, U } = loadApp();
    S.tools = [
        { tool_id: "filesystem_write", risk_level: "high" },
        { tool_id: "content_open", risk_level: "low" },
    ];
    const turn = createPendingCeoTurn("user");

    const shown = maybeShowCeoContextLoadNotice(turn, {
        toolName: "load_tool_context_v2",
        status: "success",
        detailTexts: ['{"tool_id":"filesystem_write"}'],
    });

    assert.equal(shown, true);
    const notice = U.ceoContextLoadNotice.children[0];
    assert.equal(notice.dataset.noticeKind, "tool");
    assert.equal(notice.dataset.riskLevel, "high");
    assert.ok(notice.className.split(" ").includes("risk-high"), "unexpected classes: " + notice.className);
    assert.equal(notice.children[2].className, "ceo-context-load-notice-risk-dot risk-high");
});

test("memory_note loads surface as a note notice instead of a tool row", () => {
    const { S, createPendingCeoTurn, maybeShowCeoContextLoadNotice, U, ceoContextLoaderKind, isCeoContextLoaderToolName } = loadApp();
    const turn = createPendingCeoTurn("user");
    S.ceoPendingTurns = [turn];

    // 行抑制走的就是这个谓词：实时道与快照道都用它把工具行摘掉
    assert.equal(ceoContextLoaderKind("memory_note"), "note");
    assert.equal(isCeoContextLoaderToolName("memory_note"), true);

    const shown = maybeShowCeoContextLoadNotice(turn, {
        toolName: "memory_note",
        status: "success",
        detailTexts: ['{"ref":"nb16a5"}'],
    });

    assert.equal(shown, true);
    const notice = U.ceoContextLoadNotice.children[0];
    assert.equal(notice.dataset.noticeKind, "note");
    assert.ok(notice.className.split(" ").includes("is-note"), "unexpected classes: " + notice.className);
    assert.equal(notice.children[0].innerHTML, '<i data-lucide="notebook-pen"></i>');
    assert.equal(notice.children[1].textContent, "已加载 note:nb16a5");
});

test("a failed note read shows no notice and keeps its own target out of the text", () => {
    const { S, createPendingCeoTurn, maybeShowCeoContextLoadNotice, U } = loadApp();
    const turn = createPendingCeoTurn("user");
    S.ceoPendingTurns = [turn];

    assert.equal(
        maybeShowCeoContextLoadNotice(turn, {
            toolName: "memory_note",
            status: "error",
            detailTexts: ['{"ref":"zzzzzz"}'],
        }),
        false
    );
    assert.equal(U.ceoContextLoadNotice.children.length, 0);
});

test("memory_update queues into a memory notice instead of a tool row", () => {
    const { S, createPendingCeoTurn, maybeShowCeoContextLoadNotice, isCeoContextLoaderToolName, ceoContextLoaderKind, U } = loadApp();
    const turn = createPendingCeoTurn("user");
    S.ceoPendingTurns = [turn];

    assert.equal(ceoContextLoaderKind("memory_update"), "memory");
    assert.equal(isCeoContextLoaderToolName("memory_update"), true);

    // 入参照抄实时道那四格：arguments_text 在前、结果 JSON 在 output_text，
    // 中间还夹着一格进度预览——pill 取的是能读出 status 的那一格，不是第一格。
    const shown = maybeShowCeoContextLoadNotice(turn, {
        toolName: "memory_update",
        status: "success",
        detailTexts: [
            "",
            '{"ok": true, "request_id": "write_e1f332303721", "status": "queued"}',
            "",
            "memory_update (content=记住反代端口)",
        ],
    });

    assert.equal(shown, true);
    const notice = U.ceoContextLoadNotice.children[0];
    assert.equal(notice.dataset.noticeKind, "memory");
    assert.ok(notice.className.split(" ").includes("is-memory"), "unexpected classes: " + notice.className);
    assert.equal(notice.children[0].innerHTML, '<i data-lucide="brain"></i>');
    assert.equal(notice.children[1].textContent, "记忆请求已入队 write_e1f332303721");
    assert.equal(notice.children[2].className, "ceo-context-load-notice-risk-dot risk-low");
});

test("an empty memory request says it was not queued", () => {
    const { S, createPendingCeoTurn, maybeShowCeoContextLoadNotice, U } = loadApp();
    const turn = createPendingCeoTurn("user");

    assert.equal(maybeShowCeoContextLoadNotice(turn, {
        toolName: "memory_update",
        status: "success",
        detailTexts: ['{"ok":true,"status":"ignored","reason":"empty_payload"}'],
    }), true);
    const notice = U.ceoContextLoadNotice.children[0];
    assert.equal(notice.children[1].textContent, "这条记忆请求内容为空，没有入队");
    assert.equal(notice.children[2].className, "ceo-context-load-notice-risk-dot risk-medium");
});

test("an unreadable memory result raises no notice at all", () => {
    const { S, createPendingCeoTurn, maybeShowCeoContextLoadNotice, U } = loadApp();
    const turn = createPendingCeoTurn("user");

    assert.equal(maybeShowCeoContextLoadNotice(turn, {
        toolName: "memory_update",
        status: "success",
        detailTexts: ["memory request accepted"],
    }), false);
    assert.equal(U.ceoContextLoadNotice.children.length, 0);
});
