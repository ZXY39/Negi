const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { readFrontendSources } = require("./support/frontend_sources.js");
const { StubElement, StubHTMLElement, StubDocument } = require("./support/frontend_stub.js");

const CODE = readFrontendSources(["app"]).join("\n");
const HTML_PATH = path.resolve(__dirname, "..", "..", "g3ku", "web", "frontend", "org_graph.html");
const CSS_PATH = path.resolve(__dirname, "..", "..", "g3ku", "web", "frontend", "org_graph.css");

const DISMISS_KEY = "g3ku.ui.star-guide.dismiss.v1";
const DAY_MS = 24 * 60 * 60 * 1000;

function loadApp({
    version = "2.8.0",
    stored = null,
    storageBroken = false,
    updateStatusOverride,
} = {}) {
    const store = new Map();
    if (stored !== null) store.set(DISMISS_KEY, stored);
    const document = new StubDocument();
    const banner = new StubHTMLElement("DIV");
    banner.hidden = true;
    document._byId["star-guide-banner"] = banner;
    const dismiss = new StubHTMLElement("BUTTON", { type: "button" });
    document._byId["star-guide-dismiss-btn"] = dismiss;
    const backdrop = new StubHTMLElement("DIV");
    backdrop.hidden = true;
    document._byId["project-settings-backdrop"] = backdrop;
    document._byId["project-settings-dialog"] = new StubHTMLElement("SECTION");
    document._byId["project-settings-btn"] = new StubHTMLElement("BUTTON", { type: "button" });
    document._byId["project-settings-close-btn"] = new StubHTMLElement("BUTTON", { type: "button" });
    document.documentElement.style = {
        setProperty() {},
        getPropertyValue: () => "",
    };
    const context = {
        console,
        setTimeout,
        clearTimeout,
        setInterval,
        clearInterval,
        queueMicrotask,
        document,
        Element: StubElement,
        HTMLElement: StubHTMLElement,
        URLSearchParams,
        URL,
        AbortController,
        lucide: { createIcons() {} },
        marked: { parse: (value) => String(value) },
        DOMPurify: { sanitize: (value) => String(value) },
        structuredClone: global.structuredClone,
        performance: { now: () => 0 },
        localStorage: {
            getItem: (key) => {
                if (storageBroken) throw new Error("SecurityError");
                return store.has(key) ? store.get(key) : null;
            },
            setItem: (key, value) => {
                if (storageBroken) throw new Error("SecurityError");
                store.set(key, String(value));
            },
            removeItem: (key) => store.delete(key),
        },
        ApiClient: {
            getBootstrapStatus: async () => ({ mode: "unlocked", auto_unlock: false }),
            getBootstrapAutostart: async () => ({ supported: false, backend: "none", installed: false }),
            getUpdateStatus: async () =>
                updateStatusOverride === undefined
                    ? (version ? { current_version: version, has_ledger: true } : null)
                    : updateStatusOverride,
        },
        fetch: async () => ({ ok: true, json: async () => ({}) }),
        requestAnimationFrame: (callback) => {
            callback();
            return 1;
        },
        cancelAnimationFrame: () => {},
        WebSocket: function WebSocket() {},
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
    context.window = context;
    vm.createContext(context);
    vm.runInContext(
        `${CODE}\nthis.__testExports = {
            S,
            U,
            STAR_GUIDE_SILENCE_MS,
            bindProjectSettings,
            openProjectSettingsDialog,
            closeProjectSettingsDialog,
            renderStarGuide,
            dismissStarGuide,
            isStarGuideDismissed,
        };`,
        context
    );
    const app = {
        ...context.__testExports,
        context,
        store,
        banner,
        dismiss,
        backdrop,
    };
    app.bindProjectSettings();
    return app;
}

const flush = () => new Promise((resolve) => setTimeout(resolve, 0));
const dismissedAt = (app) => Number(JSON.parse(app.store.get(DISMISS_KEY)).at);

test("没关过的用户第一次打开设置就能看到 Star 引导", async () => {
    const app = loadApp();
    app.openProjectSettingsDialog();
    await flush();

    assert.equal(app.banner.hidden, false);
});

test("叉号当场收起引导并把时间与版本写进存档，重开不再出现", async () => {
    const app = loadApp();
    app.openProjectSettingsDialog();
    await flush();

    app.dismiss.click();
    assert.equal(app.banner.hidden, true);
    const written = JSON.parse(app.store.get(DISMISS_KEY));
    assert.equal(written.v, "2.8.0");
    assert.ok(Math.abs(written.at - Date.now()) < 5 * 1000, "落盘的应当是刚刚这一次关闭");

    app.closeProjectSettingsDialog();
    app.openProjectSettingsDialog();
    await flush();
    assert.equal(app.banner.hidden, true);
});

test("七天内同版本保持静默，第八天回到弹窗上方", async () => {
    const quiet = loadApp({ stored: JSON.stringify({ at: Date.now() - 6 * DAY_MS, v: "2.8.0" }) });
    quiet.openProjectSettingsDialog();
    await flush();
    assert.equal(quiet.banner.hidden, true);

    const due = loadApp({ stored: JSON.stringify({ at: Date.now() - 8 * DAY_MS, v: "2.8.0" }) });
    due.openProjectSettingsDialog();
    await flush();
    assert.equal(due.banner.hidden, false);
});

test("本机换了版本号就再问一次，哪怕上一次关闭还在七天内", async () => {
    const app = loadApp({ stored: JSON.stringify({ at: Date.now() - DAY_MS, v: "2.7.0" }) });
    app.openProjectSettingsDialog();
    await flush();

    assert.equal(app.banner.hidden, false);
});

test("更新台账还没回来时只看时间，不把刚关掉的引导翻回来", async () => {
    // 关闭那一次读不到 current_version，存的是空版本；随后台账回来报 2.8.0，
    // 若判据把"空 → 有"当成换档，用户刚点掉的引导下一秒就会回到屏幕上。
    const app = loadApp({ version: "", stored: JSON.stringify({ at: Date.now() - DAY_MS, v: "" }) });
    app.openProjectSettingsDialog();
    await flush();
    assert.equal(app.banner.hidden, true);

    app.S.updateStatus = { current_version: "2.8.0" };
    app.renderStarGuide();
    assert.equal(app.banner.hidden, true);
});

test("存档写不进去时本次会话仍然收着，重绘不会把引导叫回来", async () => {
    const app = loadApp({ storageBroken: true });
    app.openProjectSettingsDialog();
    await flush();
    assert.equal(app.banner.hidden, false);

    app.dismiss.click();
    assert.equal(app.store.has(DISMISS_KEY), false);

    // 30 秒那一拍的重绘走的同一条路：读不到存档也不能反悔。
    app.renderStarGuide();
    assert.equal(app.banner.hidden, true);
});

test("坏掉的存档按没关过处理，而不是永久沉默", async () => {
    const app = loadApp({ stored: "{ not json" });
    app.openProjectSettingsDialog();
    await flush();
    assert.equal(app.banner.hidden, false, "读不出关闭记录时要把引导交回用户，而不是当作已关");

    // 记录形状不对（没有可用的落盘时刻）同样按没关过处理。
    assert.equal(app.isStarGuideDismissed({ at: "not-a-time", v: "2.8.0" }, "2.8.0"), false);
    assert.equal(app.isStarGuideDismissed(null, "2.8.0"), false);
});

test("静默窗口是一整七天", () => {
    const app = loadApp();
    assert.equal(app.STAR_GUIDE_SILENCE_MS, 7 * DAY_MS);
});

test("横幅坐在设置弹窗之上，不混进正文；文案整句就是那枚外链", () => {
    const html = fs.readFileSync(HTML_PATH, "utf8");
    const backdropAt = html.indexOf('id="project-settings-backdrop"');
    const shellAt = html.indexOf('class="project-settings-shell"');
    const bannerAt = html.indexOf('id="star-guide-banner"');
    const dialogAt = html.indexOf('id="project-settings-dialog"');
    const bodyAt = html.indexOf('<div class="project-settings-body">');

    assert.ok(bannerAt > shellAt && bannerAt < dialogAt, "横幅与弹窗要同住在一个定位外壳里，横幅在前");
    assert.ok(bodyAt > dialogAt, "正文必须在弹窗节点之后：横幅住在正文之前才算在窗口外");
    assert.ok(html.includes("如果好用的话，GitHub 点点 Star 让项目更加进化！"));
    assert.ok(
        /<a class="star-guide-text" href="https:\/\/github\.com\/ZXY39\/Negi" target="_blank"\s*rel="noreferrer noopener">/.test(html),
        "整句文案要交系统浏览器打开 GitHub 仓库"
    );
    assert.ok(html.includes('id="star-guide-dismiss-btn"'), "缺少关闭引导的叉号");
});

test("横幅不占布局：窗口位置与它出现与否无关", () => {
    const html = fs.readFileSync(HTML_PATH, "utf8");
    const css = fs.readFileSync(CSS_PATH, "utf8");

    assert.ok(/<div id="star-guide-banner"[^>]*\bhidden\b/.test(html), "默认不许画出来");
    assert.ok(/\.project-settings-shell\s*\{[^}]*position:\s*relative/.test(css), "外壳得当定位父");
    const banner = css.match(/#project-settings-backdrop \.star-guide-banner\s*\{([^}]*)\}/)[1];
    assert.match(banner, /position:\s*absolute/, "绝对定位才不占遮罩的格子");
    assert.match(banner, /bottom:\s*calc\(100% \+ 10px\)/, "贴在弹窗上边之外，留一格缝");
    // 同宽靠 100% 而不是把弹窗那条 min 再抄一遍：抄第二份就会有两边对不齐的一天。
    assert.match(banner, /width:\s*100%/);
    // 页头那条全局 reset 压得住这里的 display:flex，所以不必按类再补一份 [hidden]。
    assert.ok(/\[hidden\]\s*\{\s*display:\s*none\s*!important/.test(css));
    assert.doesNotMatch(css, /#project-settings-backdrop\s*\{[^}]*row-gap/, "遮罩不该再分第二格");
});

test("关闭写的是带命名空间的 v1 键，值只有落盘时刻与版本", async () => {
    const app = loadApp();
    app.dismiss.click();

    const raw = app.store.get(DISMISS_KEY);
    assert.match(DISMISS_KEY, /^g3ku\..*\.v1$/);
    assert.deepEqual(Object.keys(JSON.parse(raw)).sort(), ["at", "v"]);
    assert.ok(Math.abs(dismissedAt(app) - Date.now()) < 5 * 1000);
});
