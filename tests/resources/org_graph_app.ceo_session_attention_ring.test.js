const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const APP_PATH = "g3ku/web/frontend/org_graph_app.js";
const APP_CODE = fs.readFileSync(APP_PATH, "utf8");

class StubElement {}
class StubHTMLElement extends StubElement {}
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
        return {};
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
        ApiClient: { getActiveSessionId: () => "" },
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
        `${APP_CODE}\nthis.__testExports = { S, renderCeoSessionCard };`,
        context
    );
    return { ...context.__testExports, __context: context };
}

function cardClasses(markup) {
    const match = String(markup).match(/class="ceo-session-card([^"]*)"/);
    assert.ok(match, `卡片根节点没渲染出来：${String(markup).slice(0, 120)}`);
    return new Set(` ${match[1].trim()} `.split(/\s+/).filter(Boolean));
}

function cardAriaLabel(markup) {
    const match = String(markup).match(/aria-label="([^"]*)"/);
    assert.ok(match, "卡片没有 aria-label");
    return match[1];
}

test("awaiting-approval and errored sessions get the attention ring, a manual pause does not", () => {
    const { S, renderCeoSessionCard } = loadApp();
    S.activeSessionId = "";

    const awaiting = renderCeoSessionCard({
        session_id: "web:await",
        title: "等审批",
        is_running: false,
        status: "paused",
        has_pending_interrupts: true,
        pending_interrupt_count: 1,
    });
    assert.ok(cardClasses(awaiting).has("is-attention"), "等审批挂起要画黄环");
    assert.equal(cardClasses(awaiting).has("is-running"), false, "挂起时不该再有绿环");
    assert.equal(cardAriaLabel(awaiting), "等审批（等待审批）");

    const errored = renderCeoSessionCard({
        session_id: "web:err",
        title: "出错",
        is_running: false,
        status: "error",
    });
    assert.ok(cardClasses(errored).has("is-attention"), "错误停止要画黄环");
    assert.equal(cardAriaLabel(errored), "出错（异常停止）");

    // 手动暂停与停机暂停同落 status=paused：按口径不算异常，两圈都不画。
    const paused = renderCeoSessionCard({
        session_id: "web:pause",
        title: "我按的暂停",
        is_running: false,
        status: "paused",
    });
    assert.equal(cardClasses(paused).has("is-attention"), false, "手动暂停不该画成异常");
    assert.equal(cardClasses(paused).has("is-running"), false);
    assert.equal(cardAriaLabel(paused), "我按的暂停");
});

test("running stays green and completed stays plain", () => {
    const { S, renderCeoSessionCard } = loadApp();
    S.activeSessionId = "";

    const running = renderCeoSessionCard({
        session_id: "web:run",
        title: "在跑",
        is_running: true,
        status: "running",
    });
    assert.ok(cardClasses(running).has("is-running"));
    assert.equal(cardClasses(running).has("is-attention"), false, "运行中不该被黄环抢掉");
    assert.equal(cardAriaLabel(running), "在跑（运行中）");

    for (const status of ["completed", "idle", ""]) {
        const done = renderCeoSessionCard({ session_id: `web:${status || "blank"}`, title: "收工", status });
        assert.equal(cardClasses(done).has("is-attention"), false, `status=${status} 不该有黄环`);
        assert.equal(cardClasses(done).has("is-running"), false);
    }
});
