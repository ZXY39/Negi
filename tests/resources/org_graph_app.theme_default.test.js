const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

// 主题默认档：首访（无存储/存储不可用/存了非法值）一律浅色，只有显式存过 dark
// 才回到深色；静态 HTML 的初值要和 JS 的默认档一致，否则解锁页会先闪一帧深色。

const APP_PATH = "g3ku/web/frontend/org_graph_app.js";
const HTML_PATH = "g3ku/web/frontend/org_graph.html";
const APP_CODE = fs.readFileSync(APP_PATH, "utf8");
const HTML_CODE = fs.readFileSync(HTML_PATH, "utf8");

function makeContext({ storedTheme = undefined, storageBroken = false } = {}) {
    const store = new Map();
    if (storedTheme !== undefined) store.set("g3ku.ui.theme.v1", storedTheme);
    const icons = {
        ".dark-icon": { style: {} },
        ".light-icon": { style: {} },
    };
    const html = {
        attributes: {},
        setAttribute(name, value) {
            this.attributes[name] = String(value);
        },
        getAttribute(name) {
            return Object.prototype.hasOwnProperty.call(this.attributes, name) ? this.attributes[name] : null;
        },
    };
    const labels = {};
    const context = {
        document: {
            documentElement: html,
            addEventListener: () => {},
        },
        window: {
            localStorage: {
                getItem: (key) => {
                    if (storageBroken) throw new Error("storage blocked");
                    return store.has(key) ? store.get(key) : null;
                },
                setItem: (key, value) => {
                    if (storageBroken) throw new Error("storage blocked");
                    store.set(key, String(value));
                },
            },
        },
        U: {
            theme: {
                setAttribute(name, value) {
                    labels[name] = String(value);
                },
                querySelector: (selector) => icons[selector] || null,
            },
        },
        __store: store,
        __html: html,
        __icons: icons,
        __labels: labels,
    };
    vm.createContext(context);
    const start = APP_CODE.indexOf("function toggleTheme()");
    const end = APP_CODE.indexOf("function initializeUiPreferences()");
    assert.ok(start > 0 && end > start, "theme slice not found");
    vm.runInContext(APP_CODE.slice(start, end), context);
    return context;
}

test("首访无存储：默认浅色并同步图标与可达名", () => {
    const context = makeContext();
    context.initializeTheme();
    assert.equal(context.__html.getAttribute("data-theme"), "light");
    assert.equal(context.__icons[".light-icon"].style.display, "block");
    assert.equal(context.__icons[".dark-icon"].style.display, "none");
    assert.equal(context.__labels["aria-label"], "切换到深色主题");
});

test("存过 dark：老用户的显式选择不被默认档翻转", () => {
    const context = makeContext({ storedTheme: "dark" });
    context.initializeTheme();
    assert.equal(context.__html.getAttribute("data-theme"), "dark");
    assert.equal(context.__icons[".dark-icon"].style.display, "block");
    assert.equal(context.__labels["aria-label"], "切换到亮色主题");
});

test("存过 light 与存了非法值：都落在浅色", () => {
    for (const storedTheme of ["light", "sepia", ""]) {
        const context = makeContext({ storedTheme });
        context.initializeTheme();
        assert.equal(context.__html.getAttribute("data-theme"), "light", `stored=${JSON.stringify(storedTheme)}`);
    }
});

test("存储不可用：退化为浅色而不是打断启动", () => {
    const context = makeContext({ storageBroken: true });
    context.initializeTheme();
    assert.equal(context.__html.getAttribute("data-theme"), "light");
});

test("点击切换：light 起手翻到 dark 并写回偏好", () => {
    const context = makeContext();
    context.initializeTheme();
    context.toggleTheme();
    assert.equal(context.__html.getAttribute("data-theme"), "dark");
    assert.equal(context.__store.get("g3ku.ui.theme.v1"), "dark");
    context.toggleTheme();
    assert.equal(context.__html.getAttribute("data-theme"), "light");
    assert.equal(context.__store.get("g3ku.ui.theme.v1"), "light");
});

test("静态 HTML 初值与 JS 默认档同向：解锁页不闪深色", () => {
    const htmlTag = HTML_CODE.slice(0, HTML_CODE.indexOf(">", HTML_CODE.indexOf("<html")) + 1);
    assert.match(htmlTag, /data-theme="light"/);
    assert.match(HTML_CODE, /data-lucide="sun" class="light-icon"><\/i>/);
    assert.match(HTML_CODE, /data-lucide="moon" class="dark-icon" style="display: none;"/);
});
