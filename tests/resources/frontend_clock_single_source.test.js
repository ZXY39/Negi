const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { FRONTEND_BUNDLES, FRONTEND_DIR, readFrontendSources } = require("./support/frontend_sources.js");

// 时钟读数的唯一出口。这一族用例钉两条：形状本身（`formatClock` 拼出来的三种），
// 以及"自有前端源码里不许再出现一次不问 locale 的 `toLocale*()` 调用"。
// 成因是实测的：`toLocaleString()` 交给宿主 locale，英文浏览器给 `9/23/2026, 11:55:11 PM`
// （连 12 小时制一起换）、德文给 `23.9.2026, 23:55:11`，而这一串被静默行、回合轨道、
// 记忆行、节点详情、任务卡与会话列表共用；判据只能写正则时，英文 runner 就长期红着。

const APP_CODE = readFrontendSources(["app"]).join("\n");

function baseContext() {
    const elements = {};
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
            getElementById: (id) => elements[String(id)] || null,
            querySelector: () => null,
            querySelectorAll: () => [],
            createElement: () => ({ style: {} }),
            addEventListener: () => {},
            body: {},
        },
        Element: function Element() {},
        HTMLElement: function HTMLElement() {},
        HTMLInputElement: function HTMLInputElement() {},
        HTMLSelectElement: function HTMLSelectElement() {},
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

function loadClock() {
    const context = baseContext();
    context.window = context;
    vm.createContext(context);
    vm.runInContext(
        `${APP_CODE}\nthis.__testExports = { formatClock, formatCompactTime, formatSessionTime };`,
        context
    );
    return context.__testExports;
}

function todayStamp(clock) {
    const now = new Date();
    const pad = (part) => String(part).padStart(2, "0");
    return `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}T${clock}`;
}

test("formatClock 自己拼形状：三种档位各给一条确定的串", () => {
    const clock = loadClock();
    assert.equal(clock.formatClock("2026-09-23T23:55:11"), "23:55:11");
    assert.equal(clock.formatClock("2026-09-23T23:55:11", { withDate: true }), "09/23 23:55:11");
    assert.equal(clock.formatClock("2026-09-23T23:55:11", { withDate: true, withYear: true }), "2026-09-23 23:55:11");
    // 个位补零：月/日/时/分/秒都是两位，午夜是 00:07:05 而不是 0:7:5 或 12:07 AM。
    assert.equal(clock.formatClock("2026-01-02T00:07:05", { withDate: true, withYear: true }), "2026-01-02 00:07:05");
    // 读不出的一律原样回显，不编一个时间出来。
    assert.equal(clock.formatClock("not-a-time"), "not-a-time");
    assert.equal(clock.formatClock(""), "");
});

test("会话与任务卡的创建时间走同一个出口，带年份且不掺 locale 痕迹", () => {
    const clock = loadClock();
    assert.equal(clock.formatSessionTime("2026-09-23T23:55:11"), "2026-09-23 23:55:11");
    assert.equal(clock.formatSessionTime(""), "No activity yet");
    assert.equal(clock.formatSessionTime("not-a-time"), "not-a-time");
    const drawn = clock.formatSessionTime("2026-09-23T23:55:11");
    assert.doesNotMatch(drawn, /,/, "locale 分隔符（英文给 `9/23/2026, 23:55:11`）不许出现");
    assert.doesNotMatch(drawn, /(AM|PM|上午|下午)/, "12 小时制不许从宿主 locale 漏进来");
});

test("compact 那面保持原形状：同日只留时分秒，跨日补月日", () => {
    const clock = loadClock();
    assert.equal(clock.formatCompactTime(todayStamp("00:07:05")), "00:07:05");
    assert.equal(clock.formatCompactTime("2026-09-23T23:55:11"), "09/23 23:55:11");
    assert.equal(clock.formatCompactTime(""), "");
    assert.equal(clock.formatCompactTime("not-a-time"), "not-a-time");
});

test("自有前端源码里不许出现不问 locale 的 toLocale*() 或裸 Intl 构造", () => {
    const files = new Set();
    for (const [name, list] of Object.entries(FRONTEND_BUNDLES)) {
        if (name === "vendor") continue;
        for (const item of list) files.add(item);
    }
    const patterns = [
        [/\.\s*toLocale(?:String|TimeString|DateString)\s*\(\s*\)/g, "不带 locale 参数的 toLocale*()"],
        [/\.\s*toLocale(?:String|TimeString|DateString)\s*\(\s*\[\s*\]/g, "locale 传成空数组的 toLocale*()"],
        [/new\s+Intl\.(?:DateTimeFormat|NumberFormat|RelativeTimeFormat)\s*\(\s*\)/g, "不带 locale 参数的 Intl 构造"],
    ];
    const violations = [];
    for (const relative of [...files].sort()) {
        const target = path.join(FRONTEND_DIR, relative);
        if (!fs.existsSync(target)) continue;
        const text = fs.readFileSync(target, "utf8").replace(/\r\n/g, "\n");
        const lines = text.split("\n");
        for (const [pattern, label] of patterns) {
            lines.forEach((line, index) => {
                pattern.lastIndex = 0;
                if (pattern.test(line)) violations.push(`${relative}:${index + 1} ${label} → ${line.trim().slice(0, 90)}`);
            });
        }
    }
    assert.deepEqual(violations, [], "时钟与数字读数必须走钉住形状的出口（`formatClock`）或显式传 locale");
});
