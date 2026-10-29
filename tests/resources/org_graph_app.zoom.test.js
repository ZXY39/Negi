const test = require("node:test");
const assert = require("node:assert/strict");
const vm = require("node:vm");
const { readFrontendSources } = require("./support/frontend_sources.js");
const { StubElement, StubHTMLElement, StubDocument } = require("./support/frontend_stub.js");

const CODE = readFrontendSources(["app"]).join("\n");

function loadApp({ stored = null, shell = false, zoomApi = null } = {}) {
    const store = new Map();
    if (stored !== null) store.set("g3ku.ui.zoom.v1", stored);
    const document = new StubDocument();
    // 这一行只在桌面壳里出现，块本体在 HTML 上就带着 hidden。
    const block = new StubHTMLElement("DIV", { hidden: "" });
    document._byId["project-settings-zoom-block"] = block;
    // 拖动条与数字框是同一个值的两个入口，两个都要活过 applyZoom 的回写。
    const range = new StubHTMLElement("INPUT");
    document._byId["project-settings-zoom"] = range;
    // 指针落点写成壳上的变量：轨道填充与印着「缩放」的按钮都是它的子节点。
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
        dispatchEvent() {
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
            normalizeZoom,
            parseZoomPercent,
            zoomToPosition,
            positionToZoom,
            applyZoom,
            initializeZoom,
            bindZoom,
            commitSliderZoom,
            writeDesktopZoom,
            desktopShellReady,
            U,
            ZOOM_DEFAULT,
            ZOOM_LOW_END,
            ZOOM_HIGH_END,
        };`,
        context
    );
    return {
        ...vm.runInContext("this.__testExports", context),
        context,
        store,
        cssVars,
        shellCss,
        block,
        range,
        shell: shellEl,
        number,
        windowListeners,
    };
}

test("缩放倍数收任意正数并按 1% 取整，非正数与不可解析回默认 90%", () => {
    const { normalizeZoom } = loadApp();

    assert.equal(normalizeZoom(undefined), 0.9);
    assert.equal(normalizeZoom(""), 0.9);
    assert.equal(normalizeZoom("abc"), 0.9);
    assert.equal(normalizeZoom("0"), 0.9);
    assert.equal(normalizeZoom("-0.5"), 0.9);
    assert.equal(normalizeZoom("1.15"), 1.15);
    // 拖动条两端之外的值照收：输入框可以填 5% 或 400%。
    assert.equal(normalizeZoom("0.05"), 0.05);
    assert.equal(normalizeZoom(4), 4);
    assert.equal(normalizeZoom(1.123), 1.12);
});

test("拖动条两端是 10% 与 300%，100% 落在轨道正中", () => {
    const { zoomToPosition, positionToZoom, ZOOM_LOW_END, ZOOM_HIGH_END } = loadApp();

    assert.equal(zoomToPosition(ZOOM_LOW_END), 0);
    assert.equal(zoomToPosition(1), 50);
    assert.equal(zoomToPosition(ZOOM_HIGH_END), 100);
    // 两端之外夹到轨道尽头，滑条不会跑出可视范围。
    assert.equal(zoomToPosition(0.02), 0);
    assert.equal(zoomToPosition(9), 100);
    // 左右两半各自线性，往返同一格。
    assert.equal(positionToZoom(25), 0.55);
    assert.equal(positionToZoom(75), 2);
    assert.equal(zoomToPosition(positionToZoom(75)), 75);
});

test("浏览器轨没有宿主缩放：这一行整块留在 hidden 上，不写存档也不碰 CSS", () => {
    // bundle 顶层的 initializeUiPreferences() 已经跑过 initializeZoom()。
    const app = loadApp();

    assert.equal(app.desktopShellReady(), false);
    assert.equal(app.block.hasAttribute("hidden"), true);
    assert.equal(app.context.document.body.style.zoom, undefined);
    assert.equal(app.cssVars["--ui-zoom"], undefined);
    assert.equal(app.store.get("g3ku.ui.zoom.v1"), undefined);
});

test("桌面壳：缩放交给宿主 ZoomFactor，并同步滑条、指针落点与数字格", () => {
    const calls = [];
    const app = loadApp({
        shell: true,
        zoomApi: (factor) => {
            calls.push(factor);
            return Promise.resolve({ ok: true, zoom: factor });
        },
    });
    // 顶层初始化已按默认 90% 调过一次；这里只关心改档这一次。
    calls.length = 0;

    assert.equal(app.applyZoom("1.25"), 1.25);
    assert.deepEqual(calls, [1.25]);
    assert.equal(app.block.hasAttribute("hidden"), false);
    assert.equal(app.range.value, "56.25");
    assert.equal(app.shellCss["--zoom-range-frac"], "0.5625");
    assert.equal(app.number.value, "125");
    // 浏览器轨的 CSS zoom 那套不在壳里出现：布局由宿主按新视口重排。
    assert.equal(app.context.document.body.style.zoom, undefined);
});

test("桌面壳没有存档时按 90% 起步，有存档时按存档调宿主", () => {
    const freshCalls = [];
    const fresh = loadApp({
        shell: true,
        zoomApi: (factor) => {
            freshCalls.push(factor);
            return Promise.resolve({ ok: true });
        },
    });
    assert.deepEqual(freshCalls, [0.9]);
    assert.equal(fresh.number.value, "90");
    assert.equal(fresh.range.value, "44.44");

    const storedCalls = [];
    const stored = loadApp({
        shell: true,
        stored: "1.35",
        zoomApi: (factor) => {
            storedCalls.push(factor);
            return Promise.resolve({ ok: true });
        },
    });
    assert.deepEqual(storedCalls, [1.35]);
    assert.equal(stored.number.value, "135");
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
    const calls = [];
    const app = loadApp({
        shell: true,
        zoomApi: (factor) => {
            calls.push(factor);
            return Promise.resolve({ ok: true });
        },
    });
    app.bindZoom();
    calls.length = 0;

    // 一格键盘步进 = 0.09 个百分点，取整后还是 10%：此时回填指针会把指针弹回原地。
    app.range.value = "0.1";
    app.range.dispatchEvent({ type: "input", target: app.range });
    assert.equal(app.range.value, "0.1");
    assert.equal(app.shellCss["--zoom-range-frac"], "0.001");
    assert.deepEqual(calls, [0.1]);
    assert.equal(app.number.value, "10");
    assert.equal(app.store.get("g3ku.ui.zoom.v1"), undefined);

    // 键盘步进是 input + change 一起发的：松手那步同样不许动指针（同档重画一遍）。
    app.range.dispatchEvent({ type: "change", target: app.range });
    assert.equal(app.range.value, "0.1");
    assert.equal(app.store.get("g3ku.ui.zoom.v1"), "0.1");
    assert.deepEqual(calls, [0.1, 0.1]);

    app.range.value = "70";
    app.range.dispatchEvent({ type: "input", target: app.range });
    assert.deepEqual(calls, [0.1, 0.1, 1.8]);

    app.range.dispatchEvent({ type: "change", target: app.range });
    assert.equal(app.store.get("g3ku.ui.zoom.v1"), "1.8");
    assert.equal(app.range.value, "70");
});

test("滑条读不出数（空串）时保留当前档位，不按 0 折到最小档", () => {
    const calls = [];
    const app = loadApp({
        shell: true,
        stored: "1.1",
        zoomApi: (factor) => {
            calls.push(factor);
            return Promise.resolve({ ok: true });
        },
    });
    calls.length = 0;
    app.range.value = "";

    assert.equal(app.commitSliderZoom(), 1.1);
    assert.deepEqual(calls, []);
    assert.equal(app.number.value, "110");
});

test("输入框填任意正数即落盘，并把滑条推到对应格", () => {
    const calls = [];
    const app = loadApp({
        shell: true,
        stored: "1.1",
        zoomApi: (factor) => {
            calls.push(factor);
            return Promise.resolve({ ok: true });
        },
    });
    app.bindZoom();
    calls.length = 0;

    app.number.value = "45";
    app.number.dispatchEvent({ type: "change", target: app.number });

    assert.deepEqual(calls, [0.45]);
    assert.equal(app.store.get("g3ku.ui.zoom.v1"), "0.45");
    // 0.45 在左半段：(0.45-0.1)/0.9*50 = 19.44…，滑条跟着走但值以数字框为准。
    assert.equal(Number(app.range.value).toFixed(2), "19.44");
    assert.equal(app.number.value, "45");
});

test("输入框填了非正数或空时保留原档位，把那格回显成原值且不落盘", () => {
    const calls = [];
    const app = loadApp({
        shell: true,
        stored: "1.35",
        zoomApi: (factor) => {
            calls.push(factor);
            return Promise.resolve({ ok: true });
        },
    });
    app.bindZoom();
    calls.length = 0;

    for (const bad of ["", "0", "-20", "abc"]) {
        app.number.value = bad;
        app.number.dispatchEvent({ type: "change", target: app.number });
        // 回显只把当前档位再画一遍：宿主收到的仍是 1.35，没有新档位。
        assert.ok(calls.every((value) => value === 1.35), `非法值 ${JSON.stringify(bad)} 改掉了档位`);
        assert.equal(app.number.value, "135", `非法值 ${JSON.stringify(bad)} 留在了框里`);
        assert.equal(app.store.get("g3ku.ui.zoom.v1"), "1.35");
    }
});
