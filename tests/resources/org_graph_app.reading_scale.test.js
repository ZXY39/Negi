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
    // 拖动条与数字框是同一个值的两个入口，两个都要活过 applyReadingScale 的回写。
    const range = new StubHTMLElement("INPUT");
    document._byId["project-settings-reading-scale"] = range;
    // 指针落点写成壳上的变量：轨道填充与印着「字号」的按钮都是它的子节点。
    const shell = new StubHTMLElement("SPAN");
    const shellCss = {};
    shell.style = {
        setProperty(name, value) {
            shellCss[name] = String(value);
        },
        removeProperty(name) {
            delete shellCss[name];
        },
    };
    document._byId["project-settings-reading-scale-shell"] = shell;
    const number = new StubHTMLElement("INPUT");
    document._byId["project-settings-reading-scale-value"] = number;
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
        `${CODE}\nthis.__testExports = {
            normalizeReadingScale,
            parseReadingScalePercent,
            readingScaleToPosition,
            positionToReadingScale,
            applyReadingScale,
            initializeReadingScale,
            bindReadingScale,
            U,
            READING_SCALE_DEFAULT,
            READING_SCALE_LOW_END,
            READING_SCALE_HIGH_END,
        };`,
        context
    );
    return {
        ...vm.runInContext("this.__testExports", context),
        store,
        cssVars,
        shellCss,
        range,
        shell,
        number,
    };
}

test("字号倍数收任意正数并按 1% 取整，非正数与不可解析回默认 110%", () => {
    const { normalizeReadingScale } = loadApp();

    assert.equal(normalizeReadingScale(undefined), 1.1);
    assert.equal(normalizeReadingScale(""), 1.1);
    assert.equal(normalizeReadingScale("abc"), 1.1);
    assert.equal(normalizeReadingScale("0"), 1.1);
    assert.equal(normalizeReadingScale("-0.5"), 1.1);
    assert.equal(normalizeReadingScale("1.15"), 1.15);
    // 拖动条两端之外的值照收：输入框可以填 5% 或 400%。
    assert.equal(normalizeReadingScale("0.05"), 0.05);
    assert.equal(normalizeReadingScale(4), 4);
    assert.equal(normalizeReadingScale(1.123), 1.12);
});

test("拖动条两端是 10% 与 300%，100% 落在轨道正中", () => {
    const { readingScaleToPosition, positionToReadingScale, READING_SCALE_LOW_END, READING_SCALE_HIGH_END } = loadApp();

    assert.equal(readingScaleToPosition(READING_SCALE_LOW_END), 0);
    assert.equal(readingScaleToPosition(1), 50);
    assert.equal(readingScaleToPosition(READING_SCALE_HIGH_END), 100);
    // 两端之外夹到轨道尽头，滑条不会跑出可视范围。
    assert.equal(readingScaleToPosition(0.02), 0);
    assert.equal(readingScaleToPosition(9), 100);
    // 左右两半各自线性，往返同一格。
    assert.equal(positionToReadingScale(25), 0.55);
    assert.equal(positionToReadingScale(75), 2);
    assert.equal(readingScaleToPosition(positionToReadingScale(75)), 75);
});

test("应用档位时写 CSS 变量，并把滑条、指针落点与数字框同步到同一个值", () => {
    const { applyReadingScale, cssVars, shellCss, range, number } = loadApp();

    assert.equal(applyReadingScale("1.25"), 1.25);
    assert.equal(cssVars["--reading-font-scale"], "1.25");
    assert.equal(range.value, "56.25");
    assert.equal(shellCss["--font-range-frac"], "0.5625");
    assert.equal(number.value, "125");
});

test("没有存过值时按 110%（比基线大两号）起步", () => {
    const { initializeReadingScale, cssVars, range, number } = loadApp();

    initializeReadingScale();

    assert.equal(cssVars["--reading-font-scale"], "1.1");
    assert.equal(number.value, "110");
    assert.equal(range.value, "52.5");
});

test("滑条的两条路都不回填指针：一格键盘步进不会把自己弹回上一格", () => {
    const { initializeReadingScale, bindReadingScale, cssVars, range, shellCss, number, store } = loadApp();

    initializeReadingScale();
    bindReadingScale();

    // 一格键盘步进 = 0.09 个百分点，取整后还是 10%：此时回填指针会把指针弹回原地。
    range.value = "0.1";
    range.dispatchEvent({ type: "input", target: range });
    assert.equal(range.value, "0.1");
    assert.equal(shellCss["--font-range-frac"], "0.001");
    assert.equal(cssVars["--reading-font-scale"], "0.1");
    assert.equal(number.value, "10");
    assert.equal(store.get("g3ku.ui.reading-scale.v1"), undefined);

    // 键盘步进是 input + change 一起发的：松手那步同样不许动指针。
    range.dispatchEvent({ type: "change", target: range });
    assert.equal(range.value, "0.1");
    assert.equal(store.get("g3ku.ui.reading-scale.v1"), "0.1");

    range.value = "70";
    range.dispatchEvent({ type: "input", target: range });
    assert.equal(cssVars["--reading-font-scale"], "1.8");

    range.dispatchEvent({ type: "change", target: range });
    assert.equal(store.get("g3ku.ui.reading-scale.v1"), "1.8");
    assert.equal(range.value, "70");
});

test("输入框填任意正数即落盘，并把滑条推到对应格", () => {
    const { initializeReadingScale, bindReadingScale, cssVars, range, number, store } = loadApp({ stored: "1.1" });

    initializeReadingScale();
    bindReadingScale();

    number.value = "45";
    number.dispatchEvent({ type: "change", target: number });

    assert.equal(cssVars["--reading-font-scale"], "0.45");
    assert.equal(store.get("g3ku.ui.reading-scale.v1"), "0.45");
    // 0.45 在左半段：(0.45-0.1)/0.9*50 = 19.44…，滑条跟着走但值以数字框为准。
    assert.equal(Number(range.value).toFixed(2), "19.44");
    assert.equal(number.value, "45");
});

test("输入框填了非正数或空时保留原档位，把那格回显成原值且不落盘", () => {
    const { initializeReadingScale, bindReadingScale, cssVars, number, store } = loadApp({ stored: "1.35" });

    initializeReadingScale();
    bindReadingScale();
    assert.equal(cssVars["--reading-font-scale"], "1.35");

    for (const bad of ["", "0", "-20", "abc"]) {
        number.value = bad;
        number.dispatchEvent({ type: "change", target: number });
        assert.equal(cssVars["--reading-font-scale"], "1.35", `非法值 ${JSON.stringify(bad)} 改掉了档位`);
        assert.equal(number.value, "135", `非法值 ${JSON.stringify(bad)} 留在了框里`);
        assert.equal(store.get("g3ku.ui.reading-scale.v1"), "1.35");
    }
});
