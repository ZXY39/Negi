const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const APP_PATH = "g3ku/web/frontend/org_graph_app.js";
const APP_CODE = fs.readFileSync(APP_PATH, "utf8");

const ORDER_KEY = "g3ku.ui.ceo.session-order.v1";

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

function loadApp(seedStorage = {}) {
    const store = new Map(Object.entries(seedStorage));
    const context = {
        console,
        setTimeout,
        clearTimeout,
        setInterval,
        clearInterval,
        queueMicrotask,
        navigator: { clipboard: { writeText: async () => {} } },
        location: { protocol: "http:", host: "localhost", pathname: "/org_graph.html" },
        localStorage: {
            getItem: (key) => (store.has(key) ? store.get(key) : null),
            setItem: (key, value) => store.set(key, String(value)),
            removeItem: (key) => store.delete(key),
        },
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
        ApiClient: {
            getActiveSessionId: () => "",
        },
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
        `${APP_CODE}\nthis.__testExports = { S, U, sortCeoSessionsByTime, renderCeoSessionCard, ceoSessionDragEnabled, beginCeoSessionCardDrag, finishCeoSessionCardDrag, cancelCeoSessionCardDrag, setCeoSessionOrder, readStoredCeoSessionOrder };`,
        context
    );
    vm.runInContext(
        `
        renderCeoSessions = () => { this.__renderCalls = (this.__renderCalls || 0) + 1; };
        syncCeoPrimaryButton = () => {};
        syncCeoComposerReadonlyState = () => {};
        syncCeoAttachButton = () => {};
        syncCeoCompressionDivider = () => {};
    `,
        context
    );
    return {
        ...context.__testExports,
        __context: context,
        __store: store,
    };
}

const session = (id, createdAt) => ({
    session_id: id,
    title: id,
    preview_text: "",
    created_at: createdAt,
    session_family: "local",
});

const LOCAL = [
    session("web:first", "2026-04-01T10:00:00+08:00"),
    session("web:middle", "2026-04-02T10:00:00+08:00"),
    session("web:newest", "2026-04-03T10:00:00+08:00"),
];

const idsOf = (items) => items.map((item) => item.session_id);

test("落位把手动顺序写入列表：第一项拖到第二项之前得到 [middle, first, newest]", () => {
    const app = loadApp();
    app.S.ceoLocalSessions = [...LOCAL];
    app.S.ceoSessionOrder = idsOf(LOCAL);
    app.S.ceoSessionDrag = { from: 0, dropIndex: 2 };

    app.finishCeoSessionCardDrag({ preventDefault() {} });

    assert.deepEqual(Array.from(app.S.ceoSessionOrder), ["web:middle", "web:first", "web:newest"]);
    // 落位后源数组立刻按新手顺排好，不等下一次快照。
    assert.deepEqual(Array.from(app.S.ceoLocalSessions.map((item) => item.session_id)), ["web:middle", "web:first", "web:newest"]);
    assert.equal(app.__context.__renderCalls, 1);
    assert.equal(app.S.ceoSessionDrag, null);
});

test("拖到末尾按越过自身补位：第一项拖到列表尾得到 [middle, newest, first]", () => {
    const app = loadApp();
    app.S.ceoLocalSessions = [...LOCAL];
    app.S.ceoSessionOrder = idsOf(LOCAL);
    app.S.ceoSessionDrag = { from: 0, dropIndex: 3 };

    app.finishCeoSessionCardDrag({ preventDefault() {} });

    assert.deepEqual(Array.from(app.S.ceoSessionOrder), ["web:middle", "web:newest", "web:first"]);
});

test("拖回原位不写顺序、不重绘", () => {
    const app = loadApp();
    app.S.ceoLocalSessions = [...LOCAL];
    app.S.ceoSessionOrder = idsOf(LOCAL);
    app.S.ceoSessionDrag = { from: 0, dropIndex: 1 };

    app.finishCeoSessionCardDrag({ preventDefault() {} });

    assert.deepEqual(Array.from(app.S.ceoSessionOrder), ["web:first", "web:middle", "web:newest"]);
    assert.equal(app.__context.__renderCalls, undefined);
    assert.equal(app.__store.get(ORDER_KEY), undefined);
});

test("手动位次优先，未编号的新会话浮在手动区之上", () => {
    const app = loadApp();
    app.S.ceoSessionOrder = ["web:first", "web:newest"];

    const sorted = app.sortCeoSessionsByTime([...LOCAL]);

    // middle 没有位次 → 置顶；first 位次在前，尽管时间上更旧。
    assert.deepEqual(Array.from(idsOf(sorted)), ["web:middle", "web:first", "web:newest"]);
});

test("顺序写入 localStorage，启动时读回；脏数据退化为空顺序", () => {
    const app = loadApp();
    app.S.ceoLocalSessions = [...LOCAL];
    app.S.ceoSessionOrder = idsOf(LOCAL);
    app.S.ceoSessionDrag = { from: 2, dropIndex: 0 };

    app.finishCeoSessionCardDrag({ preventDefault() {} });

    assert.equal(app.__store.get(ORDER_KEY), '["web:newest","web:first","web:middle"]');

    const restored = loadApp({ [ORDER_KEY]: app.__store.get(ORDER_KEY) });
    assert.deepEqual(Array.from(restored.readStoredCeoSessionOrder()), ["web:newest", "web:first", "web:middle"]);

    const broken = loadApp({ [ORDER_KEY]: "{not json" });
    assert.deepEqual(Array.from(broken.readStoredCeoSessionOrder()), []);
});

test("本地页签卡片带序号与 draggable，批量模式与渠道页签不带", () => {
    const app = loadApp();
    const item = session("web:1", "2026-04-01T10:00:00+08:00");

    const localHtml = app.renderCeoSessionCard(item, { allowActions: true, index: 1 });
    assert.match(localHtml, /draggable="true"/);
    assert.match(localHtml, /data-ceo-session-index="1"/);

    app.S.ceoBulkMode = true;
    assert.equal(app.ceoSessionDragEnabled(), false);
    const bulkHtml = app.renderCeoSessionCard(item, { allowActions: true, index: 1 });
    assert.doesNotMatch(bulkHtml, /draggable="true"/);
    assert.doesNotMatch(bulkHtml, /data-ceo-session-index=/);

    app.S.ceoBulkMode = false;
    app.S.ceoSessionTab = "channel";
    assert.equal(app.ceoSessionDragEnabled(), false);
});

test("未开始的拖动（dragFrom 缺失）不改变顺序", () => {
    const app = loadApp();
    app.S.ceoLocalSessions = [...LOCAL];

    app.finishCeoSessionCardDrag({ preventDefault() {} });

    assert.deepEqual(Array.from(app.S.ceoSessionOrder), []);
    assert.equal(app.__context.__renderCalls, undefined);
});
