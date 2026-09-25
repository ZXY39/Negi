const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const APP_PATH = "g3ku/web/frontend/org_graph_app.js";
const APP_CODE = fs.readFileSync(APP_PATH, "utf8");

// 聊天里的 markdown 列表：模型常把每一项用空行断开、正文再另起一段。逐行匹配会把
// 这样一个列表切成 N 个 <ol>，浏览器于是把每一项都编号成 1（实盘 2026-09-25 的
// 每日资讯推送即如此，12 项全是「1.」）。这里锁住「一个连续列表区 = 一个 <ol>，
// 间隙里的段落折进所属条目」。

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
        document: {
            getElementById: () => null,
            createElement: () => ({ style: {}, classList: { add() {}, remove() {} }, dataset: {}, setAttribute() {}, appendChild() {} }),
            querySelector: () => null,
            querySelectorAll: () => [],
            addEventListener() {},
            documentElement: { setAttribute() {}, getAttribute: () => null, style: {}, classList: { add() {}, remove() {}, contains: () => false } },
            body: { appendChild() {}, removeChild() {}, classList: { add() {}, remove() {} } },
        },
        window: {},
        addEventListener() {},
        removeEventListener() {},
        matchMedia: () => ({ matches: false, addEventListener() {}, addListener() {} }),
        requestAnimationFrame: (cb) => setTimeout(() => cb(Date.now()), 0),
        cancelAnimationFrame: () => {},
        getComputedStyle: () => ({ getPropertyValue: () => "" }),
        ResizeObserver: class { observe() {} disconnect() {} },
        IntersectionObserver: class { observe() {} disconnect() {} },
        MutationObserver: class { observe() {} disconnect() {} },
        Element: class {},
        HTMLElement: class {},
        URLSearchParams,
        URL,
        AbortController,
        fetch: async () => ({ ok: true, json: async () => ({}) }),
    };
    context.window = context;
    vm.createContext(context);
    vm.runInContext(
        `${APP_CODE}
        this.__t = { renderMarkdown };`,
        context,
    );
    return context.__t;
}

const app = loadApp();
const html = (value) => app.renderMarkdown(value);

test("资讯推送形状：每项被空行断开也只出一个 <ol>，正文留在所属条目里", () => {
    const out = html([
        "【ACG与游戏】",
        "",
        "1. 《崩坏：星穹铁道》即兴巡演PV",
        "",
        "米哈游发布新版本PV，暗示新剧情走向。",
        "",
        "1. 『飙马野郎』 OP Kroi 「SPIN」",
        "",
        "荒木飞吕彦漫画动画化，OP 由乐队 Kroi 演唱。",
    ].join("\n"));

    assert.equal((out.match(/<ol>/g) || []).length, 1);
    assert.equal((out.match(/<\/ol>/g) || []).length, 1);
    assert.equal((out.match(/<li>/g) || []).length, 2);
    assert.match(out, /<li>《崩坏：星穹铁道》即兴巡演PV<p>米哈游发布新版本PV，暗示新剧情走向。<\/p><\/li>/);
    // 正文不能再作为列表外的独立段落出现，否则它脱离了所属条目。
    assert.equal((out.match(/<p>/g) || []).length, 3);
});

test("连续列表项照旧一项一行，段落与标题不会被吸进列表", () => {
    const out = html("1. 第一步\n2. 第二步\n\n以上是步骤。\n\n## 下一节");
    assert.match(out, /<ol><li>第一步<\/li><li>第二步<\/li><\/ol>/);
    assert.match(out, /<\/ol><p>以上是步骤。<\/p><h2>下一节<\/h2>/);
});

test("间隙撞到标题或其他块时列表就地结束，不把标题折进条目", () => {
    const out = html("1. 甲\n\n## 分隔\n\n2. 乙");
    assert.equal((out.match(/<ol>/g) || []).length, 2);
    assert.match(out, /<ol><li>甲<\/li><\/ol><h2>分隔<\/h2><ol><li>乙<\/li><\/ol>/);
});

test("项目符号列表同样按连续区间合并，正文折进所属条目", () => {
    const out = html("- 标题一\n\n说明一\n\n- 标题二\n\n说明二");
    assert.equal((out.match(/<ul>/g) || []).length, 1);
    assert.match(out, /<li>标题一<p>说明一<\/p><\/li><li>标题二<p>说明二<\/p><\/li>/);
});

test("已经建立「条目+正文」形状时，最后一条的正文也留在条目里", () => {
    const out = html("1. 甲\n\n说明甲\n\n2. 乙\n\n说明乙");
    assert.match(out, /<li>乙<p>说明乙<\/p><\/li><\/ol>$/);
});
