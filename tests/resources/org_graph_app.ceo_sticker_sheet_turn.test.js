const test = require("node:test");
const assert = require("node:assert/strict");
const vm = require("node:vm");
const { readFrontendSources } = require("./support/frontend_sources.js");

// 表情分类总览（show_sticker_category）在会话里的呈现契约：
// - 成功调用不进工具流程，改弹「已加载表情 <pack>/<category>」气泡；
// - 只调用了它的那一轮，正文提升到最终回复里一起显示（收尾/历史重建同源）。

const TASK_VIEW_CODE = readFrontendSources(["task_view"]).join("\n");
const APP_CODE = readFrontendSources(["app"]).join("\n");

class StubElement {}

class StubHTMLElement extends StubElement {
    constructor() {
        super();
        this.tagName = "DIV";
        this.hidden = false;
        this.open = false;
        this.textContent = "";
        this.className = "";
        this.dataset = {};
        this.style = {};
        this.attributes = {};
        this._innerHTML = "";
        this._selectorLists = {};
        this._children = [];
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

    setAttribute(name, value) {
        this.attributes[name] = String(value);
        if (name === "data-trace-key") this.dataset.traceKey = String(value);
        if (name === "data-round-key") this.dataset.roundKey = String(value);
    }

    querySelector() {
        return null;
    }

    querySelectorAll(selector) {
        return this._selectorLists[selector] || [];
    }

    getBoundingClientRect() {
        return { top: 0, height: 10, bottom: 10 };
    }

    addEventListener() {}

    appendChild(child) {
        this._children.push(child);
        return child;
    }

    remove() {}

    get children() {
        return this._children;
    }

    get innerHTML() {
        return this._innerHTML;
    }

    set innerHTML(value) {
        this._innerHTML = String(value);
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

    createElement() {
        return new StubHTMLElement();
    }

    querySelector() {
        return null;
    }

    querySelectorAll() {
        return [];
    }

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
        `${TASK_VIEW_CODE}\n${APP_CODE}
        this.__testExports = {
            renderCeoTurnsIntoTurn,
            renderCeoLiveStreamTextIntoTurn,
            applyCeoToolEventToTurn,
            splitCeoStickerSheetTurns,
            ceoAssistantTextWithStickerSheetTurns,
            maybeShowCeoStickerSheetNotice,
            isCeoStickerSheetToolName,
            createPendingCeoTurn,
            S,
            U,
        };`,
        context
    );
    context.__testExports.U.ceoContextLoadNotice = new StubHTMLElement();
    return context.__testExports;
}

function makeTurn() {
    const textEl = new StubHTMLElement();
    textEl.className = "assistant-text pending";
    return {
        textEl,
        flowEl: { hidden: true, open: false },
        metaEl: { textContent: "" },
        listEl: new StubHTMLElement(),
        el: new StubHTMLElement(),
        liveStreamText: "",
        lastExecutionTraceSummary: null,
        stickerSheetStreamTexts: [],
    };
}

function stickerStep(overrides = {}) {
    return {
        tool_call_id: "call-sticker",
        tool_name: "show_sticker_category",
        status: "success",
        arguments_text: '{\n  "pack": "deepseek-fanart",\n  "category": "happy"\n}',
        output_text: '{"ok": true, "pack": "deepseek-fanart", "category": "happy"}',
        ...overrides,
    };
}

function execStep() {
    return {
        tool_call_id: "call-exec",
        tool_name: "exec",
        status: "success",
        arguments_text: "exec (command=ls)",
        output_text: "ok",
    };
}

function turnFixture(seq, { text = "", tools = [] } = {}) {
    return { seq, turn_id: `turn-${seq}`, text, created_at: "", status: "completed", tools };
}

test("a sticker-only turn leaves the rail and keeps its text for the bubble", () => {
    const { splitCeoStickerSheetTurns } = loadApp();
    const split = splitCeoStickerSheetTurns([
        turnFixture(1, { text: "先看看图", tools: [execStep()] }),
        turnFixture(2, { text: "找到了，这组表情挺全", tools: [stickerStep()] }),
    ]);

    assert.equal(split.railTurns.length, 1);
    assert.equal(split.railTurns[0].seq, 1);
    assert.deepEqual([...split.replyTexts], ["找到了，这组表情挺全"]);
});

test("a mixed turn keeps its text in the rail and only drops the sticker step", () => {
    const { splitCeoStickerSheetTurns } = loadApp();
    const split = splitCeoStickerSheetTurns([
        turnFixture(1, { text: "边看边写", tools: [stickerStep(), execStep()] }),
    ]);

    assert.equal(split.railTurns.length, 1);
    assert.equal(split.railTurns[0].text, "边看边写");
    assert.deepEqual(Array.from(split.railTurns[0].tools, (tool) => tool.tool_name), ["exec"]);
    assert.deepEqual(Array.from(split.replyTexts), []);
});

test("a failed sticker call stays a normal tool row", () => {
    const { splitCeoStickerSheetTurns } = loadApp();
    const split = splitCeoStickerSheetTurns([
        turnFixture(1, { text: "", tools: [stickerStep({ status: "error" })] }),
    ]);

    assert.equal(split.railTurns.length, 1);
    assert.equal(split.railTurns[0].tools[0].tool_name, "show_sticker_category");
    assert.deepEqual(Array.from(split.replyTexts), []);
});

test("the sticker turn text is prepended to the final reply once", () => {
    const { ceoAssistantTextWithStickerSheetTurns } = loadApp();
    assert.equal(
        ceoAssistantTextWithStickerSheetTurns("最终答复", ["中间那段正文"]),
        "中间那段正文\n\n最终答复"
    );
    // 已经在正文里（攒全正文的车道）时不重复。
    assert.equal(
        ceoAssistantTextWithStickerSheetTurns("中间那段正文\n\n最终答复", ["中间那段正文"]),
        "中间那段正文\n\n最终答复"
    );
    assert.equal(ceoAssistantTextWithStickerSheetTurns("最终答复", []), "最终答复");
});

test("renderCeoTurnsIntoTurn strips the sticker row and records the reply text", () => {
    const { renderCeoTurnsIntoTurn } = loadApp();
    const turn = makeTurn();
    const turns = [
        turnFixture(1, { text: "先看看图", tools: [execStep()] }),
        turnFixture(2, { text: "找到了，这组表情挺全", tools: [stickerStep()] }),
    ];

    renderCeoTurnsIntoTurn(turn, turns);

    assert.deepEqual([...turn.stickerSheetStreamTexts], ["找到了，这组表情挺全"]);
    const html = String(turn.listEl.innerHTML || "");
    assert.ok(html.includes("turn:1"), "prior narrative group stays in the rail");
    assert.ok(!html.includes("turn:2"), "sticker-only turn holds no rail group");
    assert.ok(!html.includes("show_sticker_category"), "sticker tool row is not drawn");
    assert.ok(!html.includes("找到了，这组表情挺全"), "sticker turn text is not folded into the rail");
});

test("a sticker notice names the pack and category from the invocation hint", () => {
    const { createPendingCeoTurn, maybeShowCeoStickerSheetNotice, U } = loadApp();
    const turn = createPendingCeoTurn("user");

    const shown = maybeShowCeoStickerSheetNotice(turn, {
        toolName: "show_sticker_category",
        status: "success",
        detailTexts: ['{"ok": true, "pack": "deepseek-fanart", "category": "happy"}', "show_sticker_category (pack=deepseek-fanart, category=happy)"],
    });

    assert.equal(shown, true);
    const notice = U.ceoContextLoadNotice.children[0];
    assert.equal(notice.dataset.noticeKind, "sticker");
    assert.equal(notice.dataset.riskLevel, "low");
    assert.equal(notice.children[0].innerHTML, '<i data-lucide="sticker"></i>');
    assert.equal(notice.children[1].textContent, "已加载表情 deepseek-fanart/happy");
});

test("a failed sticker call raises no notice", () => {
    const { createPendingCeoTurn, maybeShowCeoStickerSheetNotice, U } = loadApp();
    const turn = createPendingCeoTurn("user");

    assert.equal(
        maybeShowCeoStickerSheetNotice(turn, {
            toolName: "show_sticker_category",
            status: "error",
            detailTexts: ['show_sticker_category (pack=deepseek-fanart, category=happy)'],
        }),
        false
    );
    assert.equal(U.ceoContextLoadNotice.children.length, 0);
});

test("interim text survives the sticker hop and keeps streaming after it", () => {
    const { applyCeoToolEventToTurn, renderCeoLiveStreamTextIntoTurn } = loadApp();
    const turn = makeTurn();

    turn.liveStreamText = "找到了，这组表情挺全";
    renderCeoLiveStreamTextIntoTurn(turn);
    assert.equal(turn.textEl.textContent, "找到了，这组表情挺全");

    // 看图工具开跑：这一跳的正文先记在回合一，账本还没落地。
    const handled = applyCeoToolEventToTurn(turn, {
        tool_name: "show_sticker_category",
        status: "running",
        kind: "tool_start",
        text: "",
        source: "user",
    });
    assert.equal(handled, null, "sticker call must not build a live tool row");
    assert.deepEqual([...turn.stickerSheetStreamTexts], ["找到了，这组表情挺全"]);

    // 下一段流式开始：残段换成收尾正文，中途正文垫在前面继续显示。
    turn.liveStreamText = "这些表情包已经就位";
    renderCeoLiveStreamTextIntoTurn(turn);
    assert.equal(turn.textEl.textContent, "找到了，这组表情挺全\n\n这些表情包已经就位");

    turn.liveStreamText = "这些表情包已经就位，随时可以发";
    renderCeoLiveStreamTextIntoTurn(turn);
    assert.equal(
        turn.textEl.textContent,
        "找到了，这组表情挺全\n\n这些表情包已经就位，随时可以发"
    );
});

test("text absorbed into the rail is dropped from the bubble prefix", () => {
    const { renderCeoTurnsIntoTurn } = loadApp();
    const turn = makeTurn();
    turn.liveStreamText = "边看边写";
    turn.stickerSheetStreamTexts = ["边看边写"];

    // 同轮还调了别的工具：正文是轨道里的叙述组，不能同时留在气泡里。
    renderCeoTurnsIntoTurn(turn, [turnFixture(1, { text: "边看边写", tools: [stickerStep(), execStep()] })]);

    assert.deepEqual([...turn.stickerSheetStreamTexts], []);
});
