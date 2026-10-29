const test = require("node:test");
const assert = require("node:assert/strict");
const vm = require("node:vm");
const { readFrontendSources } = require("./support/frontend_sources.js");
const { StubElement, StubHTMLElement, StubDocument } = require("./support/frontend_stub.js");

const CODE = readFrontendSources(["app"]).join("\n");

// 设置里的两行档位共用一套控件逻辑，这里跑的是「缩放」那一行；「字号」那一行见
// org_graph_app.reading_scale.test.js。桩环境按 loadApp 的参数决定自己在哪条轨上。
function loadApp({ stored = null, zoomStored = null, shell = false, zoomApi = null } = {}) {
    const store = new Map();
    if (stored !== null) store.set("g3ku.ui.zoom.v1", stored);
    if (zoomStored !== null) store.set("g3ku.ui.reading-scale.v1", zoomStored);
    const document = new StubDocument();
    const range = new StubHTMLElement("INPUT");
    document._byId["project-settings-zoom"] = range;
    // 指针落点写成壳上的变量：轨道填充与印着档位名的按钮都是它的子节点。
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
    document._byId["project-settings-zoom-shell"] = shellEl;
    const number = new StubHTMLElement("INPUT");
    document._byId["project-settings-zoom-value"] = number;
    const cssVars = {};
    document.documentElement.style = {
        setProperty(name, value) {
            cssVars[name] = String(value);
        },
        getPropertyValue(name) {
            return cssVars[name] || "";
        },
    };
    const windowListeners = {};
    const events = [];
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
        addEventListener(type, handler) {
            (windowListeners[type] = windowListeners[type] || []).push(handler);
        },
        removeEventListener() {},
        dispatchEvent(event) {
            events.push(event?.type);
            return true;
        },
        Event: class {
            constructor(type) {
                this.type = type;
            }
        },
    };
    context.window = context;
    if (shell) {
        // WebView2 的浏览器侧桥在页面脚本之前就存在；pywebview 的 window.pywebview 晚到。
        context.chrome = { webview: {} };
        context.pywebview = {
            platform: "edgechromium",
            api: zoomApi ? { set_window_zoom: zoomApi } : {},
        };
    }
    vm.createContext(context);
    vm.runInContext(
        `${CODE}\nthis.__testExports = {
            normalizeScale,
            parseScalePercent,
            scaleToPosition,
            positionToScale,
            desktopShellReady,
            zoomRow,
            readingScaleRow,
            U,
            ZOOM_DEFAULT_DESKTOP,
            ZOOM_DEFAULT_BROWSER,
            SCALE_LOW_END,
            SCALE_HIGH_END,
        };`,
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
        windowListeners,
        events,
    };
}

function shellWithCalls(options = {}) {
    const calls = [];
    const app = loadApp({
        ...options,
        shell: true,
        zoomApi: (factor) => {
            calls.push(factor);
            return Promise.resolve({ ok: true, zoom: factor });
        },
    });
    return { ...app, calls };
}

test("缩放倍数收任意正数并按 1% 取整，非正数与不可解析回默认", () => {
    const browser = loadApp();

    assert.equal(browser.normalizeZoom === undefined, true);
    assert.equal(browser.normalizeScale(undefined, browser.ZOOM_DEFAULT_BROWSER), 1);
    assert.equal(browser.normalizeScale("", 1), 1);
    assert.equal(browser.normalizeScale("abc", 1), 1);
    assert.equal(browser.normalizeScale("0", 1), 1);
    assert.equal(browser.normalizeScale("-0.5", 1), 1);
    assert.equal(browser.normalizeScale("1.15", 1), 1.15);
    // 拖动条两端之外的值照收：数字格可以填 5% 或 400%。
    assert.equal(browser.normalizeScale("0.05", 1), 0.05);
    assert.equal(browser.normalizeScale(4, 1), 4);
    assert.equal(browser.normalizeScale(1.123, 1), 1.12);
    // 两条轨的默认档不同：桌面壳 90%（与宿主 DEFAULT_ZOOM_FACTOR 同档），浏览器 100%。
    assert.equal(browser.ZOOM_DEFAULT_DESKTOP, 0.9);
    assert.equal(loadApp({ shell: true }).ZOOM_DEFAULT_DESKTOP, 0.9);
});

test("拖动条两端是 10% 与 300%，100% 落在轨道正中", () => {
    const { scaleToPosition, positionToScale, SCALE_LOW_END, SCALE_HIGH_END } = loadApp();

    assert.equal(scaleToPosition(SCALE_LOW_END), 0);
    assert.equal(scaleToPosition(1), 50);
    assert.equal(scaleToPosition(SCALE_HIGH_END), 100);
    // 两端之外夹到轨道尽头，滑条不会跑出可视范围。
    assert.equal(scaleToPosition(0.02), 0);
    assert.equal(scaleToPosition(9), 100);
    // 左右两半各自线性，往返同一格。
    assert.equal(positionToScale(25, 1), 0.55);
    assert.equal(positionToScale(75, 1), 2);
    assert.equal(scaleToPosition(positionToScale(75, 1)), 75);
});

test("浏览器轨：缩放写 body 的 CSS zoom 与 --ui-zoom，不碰宿主", () => {
    const app = loadApp();

    assert.equal(app.desktopShellReady(), false);
    assert.equal(app.zoomRow.apply("1.25"), 1.25);
    assert.equal(app.context.document.body.style.zoom, "1.25");
    assert.equal(app.cssVars["--ui-zoom"], "1.25");
    assert.equal(app.range.value, "56.25");
    assert.equal(app.shellCss["--scale-range-frac"], "0.5625");
    assert.equal(app.number.value, "125");
});

test("浏览器轨没有存过值时按 100% 起步，桌面壳没有存过值时按 90% 起步", () => {
    // bundle 顶层的 initializeUiPreferences() 已经把默认档落过一遍。
    const browser = loadApp();
    assert.equal(browser.context.document.body.style.zoom, "1");
    assert.equal(browser.number.value, "100");
    assert.equal(browser.range.value, "50");

    const desktop = shellWithCalls();
    assert.deepEqual(desktop.calls, [0.9]);
    assert.equal(desktop.number.value, "90");
    assert.equal(desktop.range.value, "44.44");
    // 壳里不叠加 CSS zoom：宿主缩放已经带动视口与布局。
    assert.equal(desktop.context.document.body.style.zoom, undefined);
});

test("桌面壳：缩放交给宿主 ZoomFactor，存档值优先于默认档", () => {
    const stored = loadApp({
        shell: true,
        stored: "1.35",
        zoomApi: () => Promise.resolve({ ok: true }),
    });
    assert.equal(stored.number.value, "135");
    assert.equal(stored.cssVars["--ui-zoom"], undefined);
});

test("壳里的桥晚于页面脚本建好：pywebviewready 到了再补一次当前档位", () => {
    const calls = [];
    const app = loadApp({ shell: true, stored: "1.2" });

    // 顶层初始化时 api 还没建好：这次调用被丢弃，档位显示先落到存档值。
    assert.deepEqual(calls, []);
    assert.equal(app.number.value, "120");

    app.context.pywebview.api.set_window_zoom = (factor) => {
        calls.push(factor);
        return Promise.resolve({ ok: true });
    };
    (app.windowListeners["pywebviewready"] || []).forEach((handler) => handler());

    assert.deepEqual(calls, [1.2]);
});

test("滑条的两条路都不回填指针：一格键盘步进不会把自己弹回上一格", () => {
    const app = shellWithCalls();
    app.zoomRow.bind();
    app.calls.length = 0;

    // 一格键盘步进 = 0.09 个百分点，取整后还是 10%：此时回填指针会把指针弹回原地。
    app.range.value = "0.1";
    app.range.dispatchEvent({ type: "input", target: app.range });
    assert.equal(app.range.value, "0.1");
    assert.equal(app.shellCss["--scale-range-frac"], "0.001");
    assert.deepEqual(app.calls, [0.1]);
    assert.equal(app.number.value, "10");
    assert.equal(app.store.get("g3ku.ui.zoom.v1"), undefined);

    // 键盘步进是 input + change 一起发的：松手那步同样不许动指针（同档重画一遍）。
    app.range.dispatchEvent({ type: "change", target: app.range });
    assert.equal(app.range.value, "0.1");
    assert.equal(app.store.get("g3ku.ui.zoom.v1"), "0.1");
    assert.deepEqual(app.calls, [0.1, 0.1]);

    app.range.value = "70";
    app.range.dispatchEvent({ type: "input", target: app.range });
    assert.deepEqual(app.calls, [0.1, 0.1, 1.8]);

    app.range.dispatchEvent({ type: "change", target: app.range });
    assert.equal(app.store.get("g3ku.ui.zoom.v1"), "1.8");
    assert.equal(app.range.value, "70");
});

test("滑条读不出数（空串）时保留当前档位，不按 0 折到最小档", () => {
    const app = shellWithCalls();
    app.calls.length = 0;
    app.range.value = "";

    assert.equal(app.zoomRow.commitSlider(), 0.9);
    assert.deepEqual(app.calls, []);
    assert.equal(app.number.value, "90");
});

test("浏览器轨松手时补发一次 resize：CSS zoom 不触发视口事件", () => {
    const app = loadApp();
    app.zoomRow.bind();
    // 顶层初始化本身也会补发一次，这里只数松手那一下。
    app.events.length = 0;

    app.range.value = "60";
    app.range.dispatchEvent({ type: "input", target: app.range });
    assert.deepEqual(app.events, []);

    app.range.dispatchEvent({ type: "change", target: app.range });
    assert.deepEqual(app.events, ["resize"]);
});

test("桌面壳松手时不补发 resize：宿主缩放自己会触发", () => {
    const app = shellWithCalls();
    app.zoomRow.bind();

    app.range.value = "60";
    app.range.dispatchEvent({ type: "change", target: app.range });

    assert.deepEqual(app.events, []);
});

test("数字格填任意正数即落盘，并把滑条推到对应格", () => {
    const app = shellWithCalls();
    app.zoomRow.bind();
    app.calls.length = 0;

    app.number.value = "45";
    app.number.dispatchEvent({ type: "change", target: app.number });

    assert.deepEqual(app.calls, [0.45]);
    assert.equal(app.store.get("g3ku.ui.zoom.v1"), "0.45");
    // 0.45 在左半段：(0.45-0.1)/0.9*50 = 19.44…，滑条跟着走但值以数字格为准。
    assert.equal(Number(app.range.value).toFixed(2), "19.44");
    assert.equal(app.number.value, "45");
});

test("数字格填了非正数或空时保留原档位，把那格回显成原值且不落盘", () => {
    const app = shellWithCalls({ stored: "1.35" });
    app.zoomRow.bind();
    app.calls.length = 0;

    for (const bad of ["", "0", "-20", "abc"]) {
        app.number.value = bad;
        app.number.dispatchEvent({ type: "change", target: app.number });
        // 回显只把当前档位再画一遍：宿主收到的仍是 1.35，没有新档位。
        assert.ok(app.calls.every((value) => value === 1.35), `非法值 ${JSON.stringify(bad)} 改掉了档位`);
        assert.equal(app.number.value, "135", `非法值 ${JSON.stringify(bad)} 留在了框里`);
        assert.equal(app.store.get("g3ku.ui.zoom.v1"), "1.35");
    }
});
