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
        this.scrollTop = 0;
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
            formatAuditTimestamp,
            renderAuditEventCard,
            renderAuditEventList,
            auditPageSummary,
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

test("formatAuditTimestamp renders system local time as YYYY-MM-DD HH:mm:ss", () => {
    const api = loadApp();

    // 用本地时间构造再回读：结果与运行时区无关，始终等于本地的 10:00:00。
    const localIso = new Date(2026, 8, 17, 10, 0, 0).toISOString();
    assert.equal(api.formatAuditTimestamp(localIso), "2026-09-17 10:00:00");

    // 带时区偏移的 ISO 串按浏览器本地时间换算，格式固定为 24 小时制。
    assert.match(api.formatAuditTimestamp("2026-09-17T10:00:00+08:00"), /^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$/);

    // 缺失/不可解析一律安全降级，不抛错
    assert.equal(api.formatAuditTimestamp(""), "-");
    assert.equal(api.formatAuditTimestamp(null), "-");
    assert.equal(api.formatAuditTimestamp("not-a-time"), "not-a-time");
});

test("renderAuditEventCard renders one raw-log line with system time", () => {
    const api = loadApp();
    const localIso = new Date(2026, 8, 17, 10, 0, 0).toISOString();
    const html = api.renderAuditEventCard({
        timestamp: localIso,
        level: "error",
        subsystem: "memory",
        event_type: "memory_batch_parked",
        summary: "<script>alert(1)</script>",
        detail: { failed_id: "f-1" },
    });

    assert.match(html, /audit-log-line is-error/);
    assert.match(html, /2026-09-17 10:00:00/);
    assert.match(html, /记忆处理/);
    assert.match(html, /memory_batch_parked/);
    // 摘要/详情全部过 esc
    assert.match(html, /&lt;script&gt;alert\(1\)&lt;\/script&gt;/);
    assert.match(html, /failed_id/);
    // 原始 ISO 串不再直接出现在日志行里
    assert.ok(!html.includes(localIso));
});

test("renderAuditEventList preserves or resets the list scroll offset", () => {
    const api = loadApp();
    const list = new StubHTMLElement();
    api.U.auditEventList = list;
    const items = [{ timestamp: "2026-09-17T10:00:00+08:00", subsystem: "task", summary: "row" }];

    // 静默轮询：滚动位置保留，阅读旧日志不会被打回顶部
    list.scrollTop = 420;
    api.renderAuditEventList(items, { preserveScroll: true });
    assert.equal(list.scrollTop, 420);

    // 换页/显式刷新：回到列表顶部
    api.renderAuditEventList(items);
    assert.equal(list.scrollTop, 0);
});

test("auditPageSummary reports page window and total", () => {
    const api = loadApp();

    assert.equal(api.auditPageSummary(1, 1, 0), "第 1/1 页 · 共 0 条");
    // 每页 100 条：第 2 页显示 101-200
    assert.equal(api.auditPageSummary(2, 3, 250), "第 2/3 页 · 显示 101-200 / 共 250 条");
    // 末页裁剪到总数
    assert.equal(api.auditPageSummary(3, 3, 250), "第 3/3 页 · 显示 201-250 / 共 250 条");
    // 越界页归一化到末页
    assert.equal(api.auditPageSummary(99, 3, 250), "第 3/3 页 · 显示 201-250 / 共 250 条");
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