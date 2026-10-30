const test = require("node:test");
const assert = require("node:assert/strict");
const vm = require("node:vm");
const { readFrontendSources } = require("./support/frontend_sources.js");
const { StubElement, StubHTMLElement, StubDocument } = require("./support/frontend_stub.js");

const CODE = readFrontendSources(["app"]).join("\n");

// 设置里的两行档位共用一套控件逻辑，这里跑的是「字号」那一行（只乘阅读面）。
// 两行的元素都注册：顶层 initializeScaleRows() 会同时初始化缩放那一行。
function loadApp({ stored = null, shell = false, zoomApi = null, resizeObserver = false } = {}) {
    const store = new Map();
    let observer = null;
    if (stored !== null) store.set("g3ku.ui.reading-scale.v1", stored);
    const document = new StubDocument();
    const range = new StubHTMLElement("INPUT");
    document._byId["project-settings-reading-scale"] = range;
    const shellEl = new StubHTMLElement("SPAN");
    const shellCss = {};
    shellEl.style = {
        setProperty(name, value) {
            shellCss[name] = String(value);
        },
        removeProperty(name) {
            delete shellCss[name];
        },
    };
    document._byId["project-settings-reading-scale-shell"] = shellEl;
    const number = new StubHTMLElement("INPUT");
    document._byId["project-settings-reading-scale-value"] = number;
    for (const id of ["project-settings-zoom", "project-settings-zoom-shell", "project-settings-zoom-value"]) {
        document._byId[id] = new StubHTMLElement("INPUT");
    }
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
        dispatchEvent() {
            return true;
        },
        Event: class {
            constructor(type) {
                this.type = type;
            }
        },
    };
    if (resizeObserver) {
        context.ResizeObserver = class {
            constructor(callback) {
                this.callback = callback;
                this.observed = [];
                observer = this;
            }

            observe(target) {
                this.observed.push(target);
            }
        };
    }
    context.window = context;
    if (shell) {
        context.chrome = { webview: {} };
        context.pywebview = {
            platform: "edgechromium",
            api: zoomApi ? { set_window_zoom: zoomApi } : {},
        };
    }
    vm.createContext(context);
    vm.runInContext(
        `${CODE}\nthis.__testExports = { readingScaleRow, zoomRow, READING_SCALE_DEFAULT, syncReadingTextGeometry, observeCeoFlowResize };`,
        context
    );
    return {
        ...vm.runInContext("this.__testExports", context),
        context,
        store,
        cssVars,
        shellCss,
        range,
        shell: shellEl,
        number,
        get observer() {
            return observer;
        },
    };
}

test("字号没有存过值时按 110%（比基线大两号）起步", () => {
    const app = loadApp();

    assert.equal(app.READING_SCALE_DEFAULT, 1.1);
    assert.equal(app.cssVars["--reading-font-scale"], "1.1");
    assert.equal(app.number.value, "110");
    assert.equal(app.range.value, "52.5");
    assert.equal(app.shellCss["--scale-range-frac"], "0.525");
});

test("字号存档值直接落成阅读面变量", () => {
    const app = loadApp({ stored: "0.52" });

    assert.equal(app.cssVars["--reading-font-scale"], "0.52");
    assert.equal(app.number.value, "52");
    // 0.52 在左半段：(0.52-0.1)/0.9*50 = 23.33。
    assert.equal(Number(app.range.value).toFixed(2), "23.33");
});

test("字号只动阅读面：不写窗口缩放，也不调宿主", () => {
    const calls = [];
    const app = loadApp({
        shell: true,
        zoomApi: (factor) => {
            calls.push(factor);
            return Promise.resolve({ ok: true });
        },
    });
    calls.length = 0;

    assert.equal(app.readingScaleRow.apply("1.5"), 1.5);
    assert.equal(app.cssVars["--reading-font-scale"], "1.5");
    assert.equal(app.cssVars["--ui-zoom"], undefined);
    assert.equal(app.context.document.body.style.zoom, undefined);
    assert.deepEqual(calls, []);
});

test("字号滑条的两条路都不回填指针，落盘只在松手那一次", () => {
    const app = loadApp();
    app.readingScaleRow.bind();

    app.range.value = "0.1";
    app.range.dispatchEvent({ type: "input", target: app.range });
    assert.equal(app.range.value, "0.1");
    assert.equal(app.cssVars["--reading-font-scale"], "0.1");
    assert.equal(app.store.get("g3ku.ui.reading-scale.v1"), undefined);

    app.range.dispatchEvent({ type: "change", target: app.range });
    assert.equal(app.range.value, "0.1");
    assert.equal(app.store.get("g3ku.ui.reading-scale.v1"), "0.1");
});

function installFlowStub(app) {
    const flow = new StubHTMLElement("DETAILS");
    flow.open = true;
    const guide = new StubHTMLElement("SPAN");
    const list = new StubHTMLElement("DIV");
    const events = [];
    guide.style = {
        removeProperty(name) {
            events.push(`guide:${name}`);
        },
    };
    flow.querySelector = (selector) => {
        if (selector === ".interaction-flow-guide") return guide;
        if (selector === ".interaction-flow-list") return list;
        return null;
    };
    app.context.document._qsAll[".interaction-flow[open]"] = [flow];
    app.context.syncCeoFlowDotGeometry = () => {
        events.push("dot");
    };
    return { flow, guide, list, events };
}

test("字号落定后先校圆点再量引导线", () => {
    const app = loadApp();
    const { events } = installFlowStub(app);
    app.readingScaleRow.bind();

    app.number.value = "150";
    app.number.dispatchEvent({ type: "change", target: app.number });

    // 引导线的 top 读的是圆点变量：先量线后移点，线尾会差一个墨迹中线的位移。
    assert.deepEqual(events, ["dot", "guide:height"], "圆点必须先于引导线重量");
});

test("列表盒子一变就在下一帧重量几何，同一列表只观察一次", () => {
    const app = loadApp({ resizeObserver: true });
    const list = new StubHTMLElement("DIV");
    app.observeCeoFlowResize({ listEl: list });
    app.observeCeoFlowResize({ listEl: list });
    assert.equal(app.observer.observed.length, 1, "同一列表被重复观察");

    let dotSyncs = 0;
    app.context.syncCeoFlowDotGeometry = () => {
        dotSyncs += 1;
    };
    app.context.document._qsAll[".interaction-flow[open]"] = [];
    app.observer.callback([], app.observer);

    assert.equal(dotSyncs, 1, "列表尺寸变化后没有重量几何");
});

test("字号数字格填任意正数即落盘，非正数与空保留原档位", () => {
    const app = loadApp({ stored: "1.35" });
    app.readingScaleRow.bind();

    app.number.value = "45";
    app.number.dispatchEvent({ type: "change", target: app.number });
    assert.equal(app.cssVars["--reading-font-scale"], "0.45");
    assert.equal(app.store.get("g3ku.ui.reading-scale.v1"), "0.45");

    for (const bad of ["", "0", "-20", "abc"]) {
        app.number.value = bad;
        app.number.dispatchEvent({ type: "change", target: app.number });
        assert.equal(app.cssVars["--reading-font-scale"], "0.45", `非法值 ${JSON.stringify(bad)} 改掉了档位`);
        assert.equal(app.number.value, "45", `非法值 ${JSON.stringify(bad)} 留在了格里`);
        assert.equal(app.store.get("g3ku.ui.reading-scale.v1"), "0.45");
    }
});
