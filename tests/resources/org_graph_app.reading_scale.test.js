const test = require("node:test");
const assert = require("node:assert/strict");
const vm = require("node:vm");
const { readFrontendSources } = require("./support/frontend_sources.js");
const { StubElement, StubHTMLElement, StubDocument } = require("./support/frontend_stub.js");

const CODE = readFrontendSources(["app"]).join("\n");

function loadApp({ stored = null } = {}) {
    const store = new Map();
    if (stored !== null) store.set("g3ku.ui.reading-scale.v1", stored);
    const document = new StubDocument();
    const select = new StubHTMLElement("SELECT");
    document._byId["project-settings-reading-scale"] = select;
    const cssVars = {};
    document.documentElement.style = {
        setProperty(name, value) {
            cssVars[name] = String(value);
        },
        getPropertyValue(name) {
            return cssVars[name] || "";
        },
    };
    const context = {
        console,
        setTimeout,
        clearTimeout,
        document,
        Element: StubElement,
        HTMLElement: StubHTMLElement,
        URLSearchParams,
        lucide: { createIcons() {} },
        localStorage: {
            getItem: (key) => (store.has(key) ? store.get(key) : null),
            setItem: (key, value) => store.set(key, String(value)),
            removeItem: (key) => store.delete(key),
        },
        requestAnimationFrame: (callback) => {
            callback();
            return 1;
        },
        addEventListener() {},
        removeEventListener() {},
    };
    context.window = context;
    vm.createContext(context);
    vm.runInContext(
        `${CODE}\nthis.__testExports = { normalizeReadingScale, applyReadingScale, initializeReadingScale, bindReadingScale, U, READING_SCALE_DEFAULT };`,
        context
    );
    return { ...vm.runInContext("this.__testExports", context), store, cssVars, select };
}

test("字号倍数只收 90%–130% 每档 5%，非法值回默认 110%", () => {
    const { normalizeReadingScale } = loadApp();

    assert.equal(normalizeReadingScale(undefined), 1.1);
    assert.equal(normalizeReadingScale(""), 1.1);
    assert.equal(normalizeReadingScale("abc"), 1.1);
    assert.equal(normalizeReadingScale("1.15"), 1.15);
    assert.equal(normalizeReadingScale(1.2), 1.2);
    assert.equal(normalizeReadingScale("0.5"), 0.9);
    assert.equal(normalizeReadingScale(2), 1.3);
    assert.equal(normalizeReadingScale(1.12), 1.1);
});

test("应用档位时写 CSS 变量并把下拉框同步到同一个值", () => {
    const { applyReadingScale, cssVars, select } = loadApp();

    assert.equal(applyReadingScale("1.25"), 1.25);
    assert.equal(cssVars["--reading-font-scale"], "1.25");
    assert.equal(select.value, "1.25");
});

test("没有存过值时按 110%（比基线大两号）起步", () => {
    const { initializeReadingScale, cssVars, select } = loadApp();

    initializeReadingScale();

    assert.equal(cssVars["--reading-font-scale"], "1.1");
    assert.equal(select.value, "1.1");
});

test("初始化吃已存档位，改动后立刻落盘", () => {
    const { initializeReadingScale, bindReadingScale, cssVars, select, store } = loadApp({ stored: "1.3" });

    initializeReadingScale();
    assert.equal(cssVars["--reading-font-scale"], "1.3");

    bindReadingScale();
    select.value = "0.95";
    select.dispatchEvent({ type: "change", target: select });

    assert.equal(cssVars["--reading-font-scale"], "0.95");
    assert.equal(store.get("g3ku.ui.reading-scale.v1"), "0.95");
});
