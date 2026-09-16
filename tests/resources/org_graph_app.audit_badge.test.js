const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

// 日志审计:侧栏未读角标渲染契约(99+ 封顶 / 0 隐藏 / 负数归一)与
// lastSeen 首访语义(历史不点亮、严格大于)。

const APP_PATH = "g3ku/web/frontend/org_graph_app.js";
const APP_CODE = fs.readFileSync(APP_PATH, "utf8");

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

    focus() {}
    remove() {}
    addEventListener() {}
}

function baseContext() {
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

function loadApp() {
    const context = baseContext();
    context.window = context;
    vm.createContext(context);
    vm.runInContext(
        `${APP_CODE}\nthis.__testExports = {
            S, U,
            renderAuditNavBadge,
            auditUnreadFromResponse,
            resolveAuditLastSeen,
            auditSubsystemLabel,
            renderAuditExceptionRow,
            switchView,
            stopAuditViewAutoRefresh,
        };`,
        context
    );
    // 屏蔽 toast/DOM 副作用；switchView 的详情拆除/任务大厅收尾依赖卫星文件
    // (org_graph_tasks.js / org_graph_task_view.js) 定义的函数，这里补桩对齐页面加载顺序。
    vm.runInContext(
        `showToast = () => {};
        closeTasksWs = () => {};
        setTaskTokenStatsOpen = () => {};
        clearAgentSelection = () => {};
        closeTaskDetailWs = () => {};`,
        context
    );
    return context.__testExports;
}

test("renderAuditNavBadge hides at 0 and caps at 99+", () => {
    const api = loadApp();
    const badge = new StubHTMLElement();
    api.U.auditNavBadge = badge;

    api.renderAuditNavBadge(0);
    assert.equal(badge.hidden, true);
    assert.equal(badge.textContent, "0");

    api.renderAuditNavBadge(5);
    assert.equal(badge.hidden, false);
    assert.equal(badge.textContent, "5");

    api.renderAuditNavBadge(150);
    assert.equal(badge.textContent, "99+");

    // 负数与非数字一律归一为 0
    api.renderAuditNavBadge(-3);
    assert.equal(badge.hidden, true);
    assert.equal(badge.textContent, "0");
});

test("auditUnreadFromResponse normalizes totals", () => {
    const api = loadApp();
    assert.equal(api.auditUnreadFromResponse(0), 0);
    assert.equal(api.auditUnreadFromResponse(7), 7);
    assert.equal(api.auditUnreadFromResponse(120), 120);
    assert.equal(api.auditUnreadFromResponse("abc"), 0);
    assert.equal(api.auditUnreadFromResponse(), 0);
});

test("resolveAuditLastSeen implements first-visit semantics", () => {
    const api = loadApp();
    const newest = "2026-09-17T10:00:05+08:00";

    // 首访：无 lastSeen → 锚定最新事件时间戳，未读 0（历史不点亮）
    const first = api.resolveAuditLastSeen("", newest);
    assert.equal(first.lastSeen, newest);
    assert.equal(first.unread, 0);

    // 已有 lastSeen 等于最新：保持原值，未读 0
    const equal = api.resolveAuditLastSeen(newest, newest);
    assert.equal(equal.lastSeen, newest);
    assert.equal(equal.unread, 0);

    // 已有 lastSeen 落后：保持原值（未读数由服务端 since 严格大于计算，不在前端推断）
    const behind = api.resolveAuditLastSeen("2026-09-17T10:00:01+08:00", newest);
    assert.equal(behind.lastSeen, "2026-09-17T10:00:01+08:00");
    assert.equal(behind.unread, 0);
});

test("auditSubsystemLabel maps subsystem keys to Chinese source labels", () => {
    const api = loadApp();
    assert.equal(api.auditSubsystemLabel("provider"), "模型调用");
    assert.equal(api.auditSubsystemLabel("task"), "任务执行");
    assert.equal(api.auditSubsystemLabel("web_api"), "Web 接口");
    assert.equal(api.auditSubsystemLabel("memory"), "记忆处理");
    assert.equal(api.auditSubsystemLabel("future_thing"), "future_thing");
    assert.equal(api.auditSubsystemLabel(""), "未知来源");
});

test("renderAuditExceptionRow renders source, summary and time", () => {
    const api = loadApp();
    const html = api.renderAuditExceptionRow({
        timestamp: "2026-09-17T10:00:00+08:00",
        subsystem: "memory",
        summary: "记忆批次停车：provider_error",
    });
    assert.match(html, /audit-exception-row/);
    assert.match(html, /audit-exception-source/);
    assert.match(html, /记忆处理/);
    assert.match(html, /记忆批次停车：provider_error/);
    assert.match(html, /2026-09-17T10:00:00\+08:00/);
    // 摘要全部过 esc：HTML 特殊字符被转义
    const escaped = api.renderAuditExceptionRow({
        timestamp: "t",
        subsystem: "provider",
        summary: "<script>alert(1)</script>",
    });
    assert.match(escaped, /&lt;script&gt;alert\(1\)&lt;\/script&gt;/);
});

test("switchView('audit') moves the view state and activates the nav item", () => {
    const api = loadApp();
    const auditSection = new StubHTMLElement();
    auditSection.classList.contains = (name) => auditSection._active ? name === "active" : false;
    auditSection.classList.toggle = (name, force) => {
        auditSection._active = force === undefined ? !auditSection._active : Boolean(force);
    };
    api.U.viewAudit = auditSection;

    api.switchView("audit");

    assert.equal(api.S.view, "audit");
    assert.equal(auditSection.style.display, "");
    assert.equal(auditSection._active, true);

    // 收尾清掉视图内 15s 轮询定时器，避免挂住 node 进程
    api.stopAuditViewAutoRefresh();
    assert.equal(api.S.auditPollIntervalId, null);
});