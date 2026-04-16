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
        this.children = [];
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

    appendChild(child) {
        this.children.push(child);
        return child;
    }

    set innerHTML(value) {
        this._innerHTML = String(value);
        if (!this._innerHTML.includes("assistant-text")) return;
        this._selectors[".assistant-text"] = new StubHTMLElement();
        this._selectors[".interaction-flow"] = new StubHTMLElement();
        this._selectors[".interaction-flow-meta"] = new StubHTMLElement();
        this._selectors[".interaction-flow-list"] = new StubHTMLElement();
        this._selectors[".interaction-flow-footer"] = new StubHTMLElement();
        this._selectors[".interaction-flow-toggle"] = new StubHTMLButtonElement();
        const reminder = new StubHTMLElement();
        reminder.className = "ceo-tool-reminder";
        reminder.hidden = true;
        this._selectors[".ceo-tool-reminder"] = reminder;
    }

    get innerHTML() {
        return this._innerHTML;
    }

    querySelector(selector) {
        return this._selectors[selector] || null;
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
        `${APP_CODE}\nthis.__testExports = { createPendingCeoTurn, handleCeoToolReminder, S, U };`,
        context
    );
    context.__testExports.U.ceoFeed = {
        appended: [],
        appendChild(element) {
            this.appended.push(element);
            return element;
        },
    };
    return context.__testExports;
}

test("handleCeoToolReminder renders and updates a live-only reminder at the bottom of the active turn", () => {
    const { createPendingCeoTurn, handleCeoToolReminder, S } = loadApp();

    const turn = createPendingCeoTurn("user");
    turn.turnId = "turn-user-1";
    S.ceoPendingTurns = [turn];

    handleCeoToolReminder({
        source: "reminder",
        turn_id: "turn-user-1",
        execution_id: "inline-tool-exec:1",
        tool_name: "exec",
        elapsed_seconds: 60,
        reminder_count: 1,
        decision: "continue",
        label: "上方的 exec 工具已运行 60 秒，之前已提醒 1 次，选择继续等待。",
    });

    assert.equal(turn.reminderEl.hidden, false);
    assert.match(String(turn.reminderEl.textContent || ""), /exec 工具已运行 60 秒/);

    handleCeoToolReminder({
        source: "reminder",
        turn_id: "turn-user-1",
        execution_id: "inline-tool-exec:1",
        tool_name: "exec",
        elapsed_seconds: 120,
        reminder_count: 2,
        decision: "stop",
        label: "上方的 exec 工具已运行 120 秒，之前已提醒 2 次，本次决定停止工具调用。",
    });

    assert.equal(turn.reminderEl.hidden, false);
    assert.match(String(turn.reminderEl.textContent || ""), /120 秒/);
    assert.match(String(turn.reminderEl.textContent || ""), /本次决定停止工具调用/);

    handleCeoToolReminder({
        source: "reminder",
        turn_id: "turn-user-1",
        execution_id: "inline-tool-exec:1",
        terminal: true,
    });

    assert.equal(turn.reminderEl.hidden, true);
});
