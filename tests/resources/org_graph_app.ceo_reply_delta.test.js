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
        this._innerHTML = "";
        this._selectors = {};
        this.attributes = {};
        this.style = {};
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
        };
    }

    set innerHTML(value) {
        this._innerHTML = String(value);
        if (!this._innerHTML.includes("assistant-text")) return;
        const assistantText = new StubHTMLElement();
        const assistantMatch = this._innerHTML.match(/<div class="assistant-text pending">([\s\S]*?)<\/div>/);
        assistantText.className = "assistant-text pending";
        assistantText.textContent = assistantMatch ? assistantMatch[1].replace(/<[^>]+>/g, "").trim() : "";
        assistantText._innerHTML = assistantMatch ? assistantMatch[1] : "";
        this._selectors[".assistant-text"] = assistantText;
        this._selectors[".interaction-flow"] = new StubHTMLElement();
        this._selectors[".interaction-flow-meta"] = new StubHTMLElement();
        this._selectors[".interaction-flow-list"] = new StubHTMLElement();
        this._selectors[".interaction-flow-footer"] = new StubHTMLElement();
        this._selectors[".interaction-flow-toggle"] = new StubHTMLButtonElement();
    }

    get innerHTML() {
        return this._innerHTML;
    }

    querySelector(selector) {
        return this._selectors[selector] || null;
    }

    querySelectorAll() {
        return [];
    }

    addEventListener() {}

    setAttribute(name, value) {
        this.attributes[name] = String(value);
    }

    removeAttribute(name) {
        delete this.attributes[name];
    }

    appendChild(child) {
        this._lastChild = child;
        return child;
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
    const markdownCalls = [];
    const rafQueue = [];
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
        marked: {
            parse: (value) => {
                markdownCalls.push(String(value));
                return String(value);
            },
        },
        DOMPurify: { sanitize: (value) => String(value) },
        structuredClone: global.structuredClone,
        performance: { now: () => 0 },
        requestAnimationFrame: (callback) => {
            rafQueue.push(callback);
            return rafQueue.length;
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
        this.__testExports = {
            S,
            U,
            createPendingCeoTurn,
            queueCeoReplyDelta,
            flushCeoReplyDeltaBuffers,
            renderCeoAssistantTextIntoTurn,
            getCeoSessionSnapshotCache,
            setCeoSessionSnapshotCache,
        };`,
        context
    );
    vm.runInContext(
        `
        renderMarkdown = (value) => {
            globalThis.__markdownCalls.push(String(value));
            return String(value);
        };
        `,
        Object.assign(context, { __markdownCalls: markdownCalls })
    );
    context.__testExports.U.ceoFeed = {
        scrollTop: 0,
        scrollHeight: 0,
        clientHeight: 0,
        appended: [],
        appendChild(element) {
            this.appended.push(element);
            return element;
        },
    };
    return {
        ...context.__testExports,
        markdownCalls,
        flushFrames() {
            while (rafQueue.length) {
                const callback = rafQueue.shift();
                callback();
            }
        },
    };
}

test("queueCeoReplyDelta updates assistant text without markdown rendering and coalesces by frame", () => {
    const {
        S,
        createPendingCeoTurn,
        queueCeoReplyDelta,
        getCeoSessionSnapshotCache,
        setCeoSessionSnapshotCache,
        markdownCalls,
        flushFrames,
    } = loadApp();

    S.activeSessionId = "web:shared";
    setCeoSessionSnapshotCache("web:shared", {
        inflight_turn: {
            source: "user",
            turn_id: "turn-stream-1",
            status: "running",
            assistant_text: "",
        },
    });
    const turn = createPendingCeoTurn("user");
    turn.turnId = "turn-stream-1";
    S.ceoPendingTurns.push(turn);

    queueCeoReplyDelta({ turn_id: "turn-stream-1", source: "user", text: "O", seq: 1 }, { sessionId: "web:shared" });
    queueCeoReplyDelta({ turn_id: "turn-stream-1", source: "user", text: "OK", seq: 2 }, { sessionId: "web:shared" });

    assert.equal(markdownCalls.length, 0);
    assert.notEqual(S.ceoReplyDeltaFrameId, 0);

    flushFrames();

    assert.equal(String(turn.textEl.textContent || ""), "OK");
    assert.equal(markdownCalls.length, 0);
    assert.equal(getCeoSessionSnapshotCache("web:shared")?.inflight_turn?.assistant_text, "OK");
});

test("final assistant renderer still uses markdown after streamed plain text", () => {
    const {
        S,
        createPendingCeoTurn,
        queueCeoReplyDelta,
        renderCeoAssistantTextIntoTurn,
        markdownCalls,
        flushFrames,
    } = loadApp();

    S.activeSessionId = "web:shared";
    const turn = createPendingCeoTurn("user");
    turn.turnId = "turn-stream-2";
    S.ceoPendingTurns.push(turn);

    queueCeoReplyDelta({ turn_id: "turn-stream-2", source: "user", text: "streamed text", seq: 1 }, { sessionId: "web:shared" });
    flushFrames();

    assert.equal(markdownCalls.length, 0);

    renderCeoAssistantTextIntoTurn(turn, "**done**", { status: "completed" });

    assert.deepEqual(markdownCalls, ["**done**"]);
    assert.match(String(turn.textEl.innerHTML || ""), /\*\*done\*\*/);
});
