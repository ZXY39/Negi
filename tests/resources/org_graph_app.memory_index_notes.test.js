const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const { readFrontendSources } = require("./support/frontend_sources.js");

// 记忆管理「归并记忆」浏览面：分类列表、面包屑路径、正文里的 note 令牌渲染成下一级入口。

const APP_PATH = "g3ku/web/frontend/org_graph_app.js";
const APP_CODE = readFrontendSources(["app"]).join("\n");

class StubElement {}
class StubHTMLElement extends StubElement {
    constructor() {
        super();
        this.tagName = "DIV";
        this.className = "";
        this.hidden = false;
        this.disabled = false;
        this.checked = false;
        this.value = "";
        this.open = false;
        this.textContent = "";
        this.innerHTML = "";
        this.title = "";
        this.dataset = {};
        this.attributes = {};
        this.children = [];
        this.scrollTop = 0;
        this.scrollLeft = 0;
        this._qs = {};
        this._qsAll = {};
        this.classList = {
            add: () => {},
            remove: () => {},
            contains: () => false,
            toggle: () => {},
        };
        this.style = {};
    }

    setAttribute(name, value) {
        this.attributes[name] = String(value);
    }

    getAttribute(name) {
        return Object.prototype.hasOwnProperty.call(this.attributes, name) ? this.attributes[name] : null;
    }

    querySelector(selector) {
        return this._qs[selector] || null;
    }

    querySelectorAll(selector) {
        return this._qsAll[selector] || [];
    }

    appendChild(child) {
        this.children.push(child);
        return child;
    }

    closest() {
        return this._closest || null;
    }

    insertAdjacentHTML(_position, html) {
        this.innerHTML += String(html);
    }

    focus() {}
    remove() {}
    addEventListener() {}
}

function baseContext(apiClient) {
    return {
        console,
        setTimeout,
        clearTimeout,
        setInterval,
        clearInterval,
        queueMicrotask,
        structuredClone: global.structuredClone,
        navigator: { clipboard: { writeText: async () => {} } },
        location: { protocol: "http:", host: "localhost", pathname: "/org_graph.html" },
        localStorage: { getItem: () => null, setItem: () => {}, removeItem: () => {} },
        sessionStorage: { getItem: () => null, setItem: () => {}, removeItem: () => {} },
        document: {
            getElementById: () => null,
            querySelector: () => null,
            querySelectorAll: () => [],
            createElement: () => new StubHTMLElement(),
            addEventListener: () => {},
            body: new StubHTMLElement(),
        },
        Element: StubElement,
        HTMLElement: StubHTMLElement,
        URLSearchParams,
        URL,
        AbortController,
        fetch: async () => ({ ok: true, json: async () => ({}) }),
        lucide: { createIcons() {} },
        ApiClient: apiClient || {},
        marked: { parse: (value) => String(value) },
        DOMPurify: { sanitize: (value) => String(value) },
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
}

function loadApp(apiClient) {
    const context = baseContext(apiClient);
    context.window = context;
    vm.createContext(context);
    vm.runInContext(
        `${APP_CODE}\nthis.__testExports = {
            S, U,
            renderMemoryNoteIndexListHtml,
            renderMemoryNoteTrailHtml,
            renderMemoryTextWithNoteTokens,
            openMemoryNoteBrowser,
            openMemoryNotePreview,
            closeMemoryNotePreview,
        };`,
        context
    );
    vm.runInContext(`showToast = () => {};`, context);
    return context.__testExports;
}

test("index list keeps every class row with handle, scope line and count", () => {
    const { renderMemoryNoteIndexListHtml } = loadApp();
    const html = renderMemoryNoteIndexListHtml({
        indexBusy: false,
        indexError: "",
        indexItems: [
            { note_ref: "note_local_env", handle: "n0bfc7", scope_line: "要改本机端口时打开", entry_count: 2, unusable: false },
            { note_ref: "note_gone", handle: "n1a2b3", scope_line: "", entry_count: 1, unusable: true },
        ],
    });

    assert.match(html, /data-memory-note-ref="note_local_env"/);
    assert.match(html, /要改本机端口时打开/);
    assert.match(html, /n0bfc7/);
    assert.match(html, /entry|条目/);
    assert.match(html, /2/);
    // 断链的分类不能从列表里消失，操作员要看到的是"链断了"而不是"少一类"
    assert.match(html, /data-memory-note-ref="note_gone"/);
    assert.match(html, /is-unusable/);
    assert.match(html, /分类 note 缺失或没有说明行/);
});

test("index list marks a scope line over the row cap without printing it", () => {
    const { renderMemoryNoteIndexListHtml } = loadApp();
    const scope = "大类:" + "词".repeat(600);
    const html = renderMemoryNoteIndexListHtml({
        indexBusy: false,
        indexError: "",
        indexItems: [{ note_ref: "note_long", handle: "c0ab12", scope_line: scope, scope_line_over_limit: true, unusable: true }],
    });

    assert.match(html, /is-unusable/);
    assert.match(html, /过长未进常驻块/);
    assert.doesNotMatch(html, /大类:/);
});

test("index list separates loading, error and empty states", () => {
    const { renderMemoryNoteIndexListHtml } = loadApp();

    assert.match(renderMemoryNoteIndexListHtml({ indexBusy: true, indexItems: [] }), /正在加载归并记忆/);
    assert.match(
        renderMemoryNoteIndexListHtml({ indexBusy: false, indexError: "归并记忆暂时不可读取", indexItems: [] }),
        /归并记忆暂时不可读取/
    );
    assert.match(renderMemoryNoteIndexListHtml({ indexBusy: false, indexError: "", indexItems: [] }), /还没有分类 note/);
});

test("note body tokens become the next level, escaped text stays text", () => {
    const { renderMemoryTextWithNoteTokens } = loadApp();

    const html = renderMemoryTextWithNoteTokens(
        "反代见 (note: n0bfc7) 与 note:n1a2b3；分类句柄走 c 前缀 (note: c1a2b3) 和 note:c4d5e6；旧标记 ref:note_deep_link 也要能点开\n\n<script>x</script>"
    );

    assert.match(html, /data-memory-note-ref="n0bfc7"/);
    assert.match(html, /data-memory-note-ref="n1a2b3"/);
    // 两棵树的句柄都要认：`c` 是 memory/index/ 的分类 note
    assert.match(html, /data-memory-note-ref="c1a2b3"/);
    assert.match(html, /data-memory-note-ref="c4d5e6"/);
    assert.match(html, /data-memory-note-ref="note_deep_link"/);
    // 正文里除令牌外一律转义：可点入口不能顺带把正文当 HTML 注入
    assert.doesNotMatch(html, /<script>/);
    assert.match(html, /&lt;script&gt;/);
});

test("breadcrumb shows the path only on the consolidation lane", () => {
    const { renderMemoryNoteTrailHtml } = loadApp();

    const trail = renderMemoryNoteTrailHtml({
        origin: "index",
        trail: [{ ref: "note_local_env" }],
        ref: "note_deep_link",
    });
    assert.match(trail, /data-memory-note-index-back/);
    assert.match(trail, /data-memory-note-trail-ref="note_local_env"/);
    assert.match(trail, /is-current[^]*note_deep_link/);
    // 从记忆行/队列点开的单层窗不显示路径条
    assert.equal(renderMemoryNoteTrailHtml({ origin: "direct", trail: [], ref: "note_x" }), "");
});

test("opening the browser fills the list and a stale reload cannot overwrite it", async () => {
    const calls = [];
    let releaseFirst;
    const firstGate = new Promise((resolve) => {
        releaseFirst = resolve;
    });
    const { S, openMemoryNoteBrowser } = loadApp({
        async listMemoryIndexNotes() {
            calls.push(calls.length);
            if (calls.length === 1) {
                await firstGate;
                return { items: [{ note_ref: "note_stale", handle: "n00000", scope_line: "旧的", entry_count: 1 }], total: 1 };
            }
            return { items: [{ note_ref: "note_fresh", handle: "n11111", scope_line: "新的", entry_count: 3 }], total: 1 };
        },
    });

    const first = openMemoryNoteBrowser();
    assert.equal(S.memoryNotePreview.mode, "index");
    assert.equal(S.memoryNotePreview.origin, "index");
    assert.equal(S.memoryNotePreview.indexBusy, true);

    const second = openMemoryNoteBrowser();
    await second;
    releaseFirst();
    await first;

    assert.equal(S.memoryNotePreview.indexBusy, false);
    assert.equal(S.memoryNotePreview.indexItems[0].note_ref, "note_fresh");
    assert.equal(S.memoryNotePreview.indexItems[0].entry_count, 3);
});

test("drilling from the list clears the index state and seeds the trail", async () => {
    const seen = [];
    const { S, openMemoryNotePreview } = loadApp({
        async listMemoryIndexNotes() {
            return { items: [], total: 0 };
        },
        async getMemoryNote(ref) {
            seen.push(ref);
            return { ref, body: "正文里有 (note: n0bfc7)" };
        },
    });

    S.memoryNotePreview.mode = "index";
    S.memoryNotePreview.origin = "index";
    S.memoryNotePreview.trail = [];
    await openMemoryNotePreview("note_local_env", { trail: [], origin: "index" });

    assert.deepEqual(seen, ["note_local_env"]);
    assert.equal(S.memoryNotePreview.mode, "note");
    assert.equal(S.memoryNotePreview.origin, "index");
    assert.match(S.memoryNotePreview.body, /note: n0bfc7/);

    // 再往下一级：当前 ref 进路径，面包屑才有得回退
    await openMemoryNotePreview("n0bfc7", { trail: [{ ref: "note_local_env" }], origin: "index" });
    assert.equal(S.memoryNotePreview.trail.length, 1);
    assert.equal(S.memoryNotePreview.trail[0].ref, "note_local_env");
    assert.equal(S.memoryNotePreview.ref, "n0bfc7");

    // 别的显示面（记忆行、队列）点开时不带路径
    await openMemoryNotePreview("note_plain");
    assert.equal(S.memoryNotePreview.origin, "direct");
    assert.equal(S.memoryNotePreview.trail.length, 0);
});
