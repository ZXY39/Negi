const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const { readFrontendSources } = require("./support/frontend_sources.js");

// 新版本提醒的渲染契约：红点只认台账里的 `newer`；"从未检查"与"检查失败"都不亮，
// 也不能被写成"已是最新"。有新版时「检查更新」换成「查看新版本」，点它是打开发布说明。

const APP_PATH = "g3ku/web/frontend/org_graph_app.js";
const APP_CODE = readFrontendSources(["app"]).join("\n");

class StubElement {}
class StubHTMLElement extends StubElement {
    constructor() {
        super();
        this.hidden = false;
        this.disabled = false;
        this.title = "";
        this.textContent = "";
        this.attributes = {};
        this.children = [];
        const classes = new Set();
        this.classList = {
            add: (...names) => names.forEach((name) => classes.add(name)),
            remove: (...names) => names.forEach((name) => classes.delete(name)),
            contains: (name) => classes.has(name),
            toggle: (name, force) => {
                const on = force === undefined ? !classes.has(name) : Boolean(force);
                if (on) classes.add(name);
                else classes.delete(name);
                return on;
            },
        };
        this.style = {};
    }

    setAttribute(name, value) {
        this.attributes[name] = String(value);
    }

    getAttribute(name) {
        return Object.prototype.hasOwnProperty.call(this.attributes, name) ? this.attributes[name] : null;
    }

    addEventListener() {}
    focus() {}
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
            U,
            renderUpdateNavDot,
            updateSettingsButtonTitle,
            updateApplyOutcomeText,
            renderProjectSettingsUpdate,
        };`,
        context
    );
    return context.__testExports;
}

function stubbedUpdateApi() {
    const api = loadApp();
    api.U.updateNavDot = new StubHTMLElement();
    api.U.projectSettingsCheckUpdate = new StubHTMLElement();
    api.U.projectSettingsCheckUpdateLabel = new StubHTMLElement();
    api.U.projectSettingsCheckDot = new StubHTMLElement();
    api.U.projectSettingsVersion = new StubHTMLElement();
    return api;
}

test("nav dot lights only on a newer ledger entry", () => {
    const api = stubbedUpdateApi();

    api.renderUpdateNavDot(null);
    assert.equal(api.U.updateNavDot.hidden, true, "无台账不得亮");

    api.renderUpdateNavDot({ has_ledger: true, newer: false, latest_tag: "v1.0.1" });
    assert.equal(api.U.updateNavDot.hidden, true, "已是最新不得亮");

    api.renderUpdateNavDot({ has_ledger: true, newer: true, latest_tag: "v1.0.2" });
    assert.equal(api.U.updateNavDot.hidden, false, "有新版必须亮");

    api.renderUpdateNavDot({ has_ledger: true, newer: false, error: "remote_unreachable" });
    assert.equal(api.U.updateNavDot.hidden, true, "检查失败不得亮");
});

test("弹窗里只有红点认新版，按钮文案与版本对跟着台账走", () => {
    const api = stubbedUpdateApi();

    api.renderProjectSettingsUpdate({ has_ledger: false });
    assert.match(api.U.projectSettingsCheckUpdate.title, /还没查过远端标签/);
    assert.equal(api.U.projectSettingsCheckUpdateLabel.textContent, "检查更新");
    assert.equal(api.U.projectSettingsCheckDot.hidden, true, "没查过不得亮");
    assert.equal(api.U.projectSettingsVersion.textContent, "");

    api.renderProjectSettingsUpdate({
        has_ledger: true,
        newer: true,
        current_version: "1.0.1",
        latest_tag: "v1.0.2",
        checked_at: "2026-09-25T17:45:04+08:00",
    });
    assert.equal(api.U.projectSettingsCheckDot.hidden, false, "有新版必须亮");
    assert.equal(api.U.projectSettingsCheckUpdateLabel.textContent, "查看新版本");
    assert.equal(api.U.projectSettingsVersion.textContent, "v1.0.1 → v1.0.2");
    const newerTitle = api.U.projectSettingsCheckUpdate.title;
    assert.match(newerTitle, /发现新版本 v1\.0\.2（当前 v1\.0\.1）/);
    assert.match(newerTitle, /点开看发布说明/);
    assert.match(newerTitle, /检查于 \d{2}-\d{2} \d{2}:\d{2}/);

    api.renderProjectSettingsUpdate({
        has_ledger: true,
        newer: false,
        current_version: "1.0.1",
        error: "remote_unreachable",
        checked_at: "2026-09-25T17:45:04+08:00",
    });
    assert.equal(api.U.projectSettingsCheckDot.hidden, true, "检查失败不得亮");
    assert.equal(api.U.projectSettingsCheckUpdateLabel.textContent, "检查更新");
    assert.equal(api.U.projectSettingsVersion.textContent, "v1.0.1");
    const failedTitle = api.U.projectSettingsCheckUpdate.title;
    assert.match(failedTitle, /上次检查失败：remote_unreachable/);
    assert.doesNotMatch(failedTitle, /已是最新/);
});

test("up to date never borrows the newer wording", () => {
    const api = stubbedUpdateApi();
    api.renderProjectSettingsUpdate({ has_ledger: true, newer: false, latest_tag: "v1.0.1", current_version: "1.0.1", checked_at: "2026-11-04T09:12:00+08:00" });
    // "已是最新"这一档由行内说话，title 不再把同一句印第二遍。
    assert.equal(api.U.projectSettingsVersion.textContent, "v1.0.1 · 已是最新");
    assert.equal(api.U.projectSettingsVersion.classList.contains("is-newer"), false);
    assert.doesNotMatch(api.U.projectSettingsCheckUpdate.title, /发现新版本/);
    assert.doesNotMatch(api.U.projectSettingsCheckUpdate.title, /已是最新/, "同一句只在一处");
    assert.match(api.U.projectSettingsCheckUpdate.title, /检查于 \d{2}-\d{2} \d{2}:\d{2}/, "title 仍留检查时间");
    assert.equal(api.U.projectSettingsCheckDot.hidden, true);
});

test("没查过与查失败都不许借「已是最新」", () => {
    const api = stubbedUpdateApi();

    // 后端即使从没查过也照回 current_version，所以这一档必须在行内就说"没查过"的反面：只报版本号。
    api.renderProjectSettingsUpdate({ has_ledger: false, newer: false, current_version: "1.0.1", checked_at: "" });
    assert.equal(api.U.projectSettingsVersion.textContent, "v1.0.1");
    assert.match(api.U.projectSettingsCheckUpdate.title, /还没查过远端标签/);

    api.renderProjectSettingsUpdate({ has_ledger: true, newer: false, current_version: "1.0.1", error: "timeout", detail: "timeout" });
    assert.equal(api.U.projectSettingsVersion.textContent, "v1.0.1");
    assert.match(api.U.projectSettingsCheckUpdate.title, /上次检查失败：timeout/);

    // 换车道读到版本且没有更新：这一档是"查成功了"，可以点亮行内提示。
    api.renderProjectSettingsUpdate({ has_ledger: true, newer: false, current_version: "1.0.1", error: "", degraded: true });
    assert.equal(api.U.projectSettingsVersion.textContent, "v1.0.1 · 已是最新");
});

function loadDialogContext() {
    const context = baseContext();
    context.window = context;
    vm.createContext(context);
    vm.runInContext(
        `${APP_CODE}\nthis.__testExports = {
            U,
            S,
            handleCheckUpdateClick,
            openUpdateReleaseDialog,
            closeUpdateReleaseDialog,
            isUpdateReleaseOpen,
        };`,
        context
    );
    const api = context.__testExports;
    api.U.updateReleaseBackdrop = new StubHTMLElement();
    api.U.updateReleaseBackdrop.hidden = true;
    api.U.updateReleaseDialog = new StubHTMLElement();
    api.U.updateReleaseTitle = new StubHTMLElement();
    api.U.updateReleaseNotes = new StubHTMLElement();
    api.U.updateReleaseCancel = new StubHTMLElement();
    api.U.updateReleaseApply = new StubHTMLElement();
    api.U.projectSettingsCheckUpdate = new StubHTMLElement();
    return { context, api };
}

const tick = () => new Promise((resolve) => setTimeout(resolve, 10));

test("有新版时点按钮打开说明，没有新版才重新检查", () => {
    const { context, api } = loadDialogContext();
    context.__opened = 0;
    context.__checked = 0;
    vm.runInContext(
        `openUpdateReleaseDialog = () => { __opened += 1; };
         checkForUpdatesNow = async () => { __checked += 1; };`,
        context
    );

    api.S.updateStatus = { newer: true, latest_tag: "v1.9.0" };
    api.handleCheckUpdateClick();
    assert.equal(context.__opened, 1, "有新版时这颗按钮是查看说明");
    assert.equal(context.__checked, 0, "不许在同一次点击里再联网检查");

    api.S.updateStatus = { newer: false, latest_tag: "v1.9.0" };
    api.handleCheckUpdateClick();
    assert.equal(context.__checked, 1, "没有新版时仍是检查更新");
    assert.equal(context.__opened, 1);
});

test("查看新版本弹窗先占位再填入 release 正文", async () => {
    const { context, api } = loadDialogContext();
    api.S.updateStatus = { newer: true, latest_tag: "v1.9.0", current_version: "1.8.0" };
    let asked = "";
    context.ApiClient = {
        getUpdateReleaseNotes: async (ref) => {
            asked = ref;
            return { body: "这一版修了 A 和 B。", error: "" };
        },
    };

    api.openUpdateReleaseDialog();
    assert.equal(api.U.updateReleaseBackdrop.hidden, false);
    assert.equal(api.U.updateReleaseTitle.textContent, "新版本 v1.9.0");
    assert.equal(api.U.updateReleaseNotes.textContent, "正在读取发布说明…");

    await tick();
    assert.equal(asked, "v1.9.0", "按台账里的标签取说明");
    assert.equal(api.U.updateReleaseNotes.textContent, "这一版修了 A 和 B。");
    assert.equal(api.U.updateReleaseNotes.classList.contains("is-unavailable"), false);
});

test("读不到说明时照实说，更新按钮仍然可用", async () => {
    const { context, api } = loadDialogContext();
    api.S.updateStatus = { newer: true, latest_tag: "v1.9.0", current_version: "1.8.0" };
    context.ApiClient = {
        getUpdateReleaseNotes: async () => ({ body: "", error: "release_notes_unavailable" }),
    };

    api.openUpdateReleaseDialog();
    await tick();
    assert.match(api.U.updateReleaseNotes.textContent, /读不到这一版的发布说明/);
    assert.equal(api.U.updateReleaseNotes.classList.contains("is-unavailable"), true);
    assert.equal(api.U.updateReleaseApply.disabled, false, "说明取不到不影响更新");
});

test("关掉弹窗后迟到的说明不回填", async () => {
    const { context, api } = loadDialogContext();
    api.S.updateStatus = { newer: true, latest_tag: "v1.9.0", current_version: "1.8.0" };
    let release;
    context.ApiClient = {
        getUpdateReleaseNotes: () => new Promise((resolve) => {
            release = () => resolve({ body: "迟到的正文", error: "" });
        }),
    };

    api.openUpdateReleaseDialog();
    api.closeUpdateReleaseDialog({ restoreFocus: false });
    assert.equal(api.isUpdateReleaseOpen(), false);
    release();
    await tick();
    assert.equal(api.U.updateReleaseNotes.textContent, "正在读取发布说明…", "关了就不再动这一页");
});

test("重新打开另一版时，先发出的慢响应不许盖掉后一版", async () => {
    const { context, api } = loadDialogContext();
    api.S.updateStatus = { newer: true, latest_tag: "v1.9.0", current_version: "1.8.0" };
    const pending = [];
    context.ApiClient = {
        getUpdateReleaseNotes: (ref) => new Promise((resolve) => {
            pending.push({ ref, resolve });
        }),
    };

    api.openUpdateReleaseDialog();
    api.closeUpdateReleaseDialog({ restoreFocus: false });
    api.S.updateStatus = { newer: true, latest_tag: "v1.9.1", current_version: "1.8.0" };
    api.openUpdateReleaseDialog();
    assert.equal(pending.at(-1).ref, "v1.9.1");

    pending[0].resolve({ body: "旧版说明", error: "" });
    await tick();
    assert.equal(api.U.updateReleaseNotes.textContent, "正在读取发布说明…", "旧响应不得回填");

    pending[1].resolve({ body: "新版说明", error: "" });
    await tick();
    assert.equal(api.U.updateReleaseNotes.textContent, "新版说明");
});

// 层级契约：按"选择器 + 开括号"定位规则块，再从块里取 z-index 数值（避免正则转义脆弱）。
function zIndexOf(css, blockOpen) {
    const start = css.indexOf(blockOpen);
    assert.ok(start >= 0, `缺少规则块：${blockOpen}`);
    const body = css.slice(start, css.indexOf("}", start));
    const declared = body.match(/z-index:[ \t]*(\d+)/);
    assert.ok(declared, `规则里没有 z-index：${blockOpen}`);
    return Number(declared[1]);
}

const MODAL_CSS = fs.readFileSync("g3ku/web/frontend/org_graph.css", "utf8");

test("确认框层级压过叫它起来的弹窗，但仍在 toast 之下", () => {
    const css = MODAL_CSS;

    const confirm = zIndexOf(css, "#confirm-backdrop {");
    const dialog = zIndexOf(css, ".confirm-backdrop {");
    const rename = zIndexOf(css, "#rename-session-backdrop {");
    const memoryEdit = zIndexOf(css, ".memory-browser-edit-dialog {");
    const toast = zIndexOf(css, ".app-toast-viewport {");
    const approval = zIndexOf(css, ".ceo-approval-viewport {");

    // 同为 1000 时按 DOM 顺序决胜，而 #confirm-backdrop 排在各弹窗之前 ⇒ 必须靠层级压过
    assert.ok(confirm > dialog && confirm > rename && confirm > memoryEdit,
        `确认框必须压过弹窗族：${confirm} vs ${dialog}/${rename}/${memoryEdit}`);
    assert.ok(confirm < toast && confirm < approval,
        `toast 与审批浮层必须仍在确认框之上：${toast}/${approval} vs ${confirm}`);
});

test("预览层压过叫它起来的弹窗族，但仍在 toast 之下", () => {
    const css = MODAL_CSS;

    const viewer = zIndexOf(css, "#media-viewer-drawer.media-viewer-drawer {");
    const viewerBackdrop = zIndexOf(css, "#media-viewer-backdrop.detail-backdrop {");
    const drawer = zIndexOf(css, ".detail-drawer.project-detail-drawer {");
    const dialog = zIndexOf(css, ".confirm-backdrop {");
    const rename = zIndexOf(css, "#rename-session-backdrop {");
    const memoryEdit = zIndexOf(css, ".memory-browser-edit-dialog {");
    const confirm = zIndexOf(css, "#confirm-backdrop {");
    const toast = zIndexOf(css, ".app-toast-viewport {");
    const approval = zIndexOf(css, ".ceo-approval-viewport {");

    // 人设照片是从弹窗里点开的：预览层若留在抽屉族那一档（96～112），整扇窗会沉到
    // 叫它起来的那张弹窗后面，点了像没反应。它不叫起任何弹窗，所以按叶子层压在弹窗族之上。
    assert.ok(viewer > dialog && viewer > rename && viewer > memoryEdit && viewer > confirm
            && viewer > drawer,
        `预览层必须压过叫它起来的弹窗族：${viewer} vs ${dialog}/${rename}/${memoryEdit}/${confirm}/${drawer}`);
    assert.ok(viewerBackdrop < viewer, `遮罩必须在预览窗之下：${viewerBackdrop} vs ${viewer}`);
    assert.ok(viewer < toast && viewer < approval,
        `toast 与审批浮层必须仍在预览层之上：${toast}/${approval} vs ${viewer}`);
});

test("Escape 阶梯按层位收：预览层排在弹窗族之前", () => {
    const guard = 'if (e.key !== "Escape") return;';
    const start = APP_CODE.indexOf(guard);
    assert.ok(start >= 0, "找不到 Escape 阶梯入口");
    const end = APP_CODE.indexOf("\n    });", start);
    assert.ok(end > start, "Escape 阶梯整块没取到");
    const ladder = APP_CODE.slice(start, end);

    const order = [
        "closeMediaViewer()",
        "closeResourceSelects(",
        "closeCeoModelModePanel()",
        "isPasswordChangeOpen()",
        "isProjectSettingsOpen()",
        "closeConfirm()",
    ].map((token) => {
        const at = ladder.indexOf(token);
        assert.ok(at >= 0, `Escape 阶梯里找不到 ${token}`);
        return [token, at];
    });

    // 预览层压在弹窗族之上，所以它也第一个收：反过来会先关掉被盖住的那一层，
    // 屏幕上留下一扇关不掉的图片窗。
    const [viewerToken, viewerAt] = order[0];
    order.slice(1).forEach(([token, at]) => {
        assert.ok(viewerAt < at, `预览层必须排在 ${token} 之前：${viewerToken}@${viewerAt} vs ${at}`);
    });
});

test("升级终态不回落成\"正在重启\"：失败占行、成功让回版本对", () => {
    const api = loadApp();
    const base = { has_ledger: true, newer: true, current_version: "1.0.11", latest_tag: "v1.0.12", checked_at: "2026-09-28T13:15:20+08:00" };

    // 没有记录 / ok：title 仍归版本对
    assert.equal(api.updateApplyOutcomeText(base), "");
    assert.equal(api.updateApplyOutcomeText({ ...base, last_apply: { outcome: "ok", at: "2026-09-28T13:30:00+08:00" } }), "");
    assert.match(api.updateSettingsButtonTitle(base), /发现新版本 v1\.0\.12（当前 v1\.0\.11）/);

    // 脏树被守卫拒掉的那次：端口没退干净 ⇒ 代码未动。终态顶到 title 最前面，不藏在版本对后面。
    const busy = { ...base, last_apply: { outcome: "port_busy", at: "2026-09-28T13:19:05+08:00", detail: "exit_accepted_200; port 18790 still busy" } };
    assert.equal(api.updateApplyOutcomeText(busy), "升级未完成：服务没退干净");
    assert.match(api.updateSettingsButtonTitle(busy), /^升级未完成：服务没退干净 · /);
    assert.match(api.updateSettingsButtonTitle(busy), /上次升级 port_busy .*：exit_accepted_200/);

    // 执行体刚起来：只说执行中，不谎报完成
    assert.equal(api.updateApplyOutcomeText({ ...base, last_apply: { outcome: "started", detail: "port=18790" } }), "升级执行中");

    // 未知终态兜底：宁可露出原码，也不要显示成"已是最新"
    assert.equal(api.updateApplyOutcomeText({ ...base, last_apply: { outcome: "weird_code" } }), "升级未完成：weird_code");
});

function loadWatchContext() {
    const context = baseContext();
    context.window = context;
    context.__toasts = [];
    context.__reloads = 0;
    vm.createContext(context);
    vm.runInContext(
        `${APP_CODE}\nthis.__testExports = { watchApplyUpdateFinish, stopUpdateApplyWatch, updateApplyTargetVersion, updateApplyProgressText };`,
        context
    );
    vm.runInContext(
        `showToast = (opts) => { __toasts.push(opts); };
         refreshUpdateStatus = async () => {};`,
        context
    );
    context.location.reload = () => {
        context.__reloads += 1;
    };
    return context;
}

const nap = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

test("apply 的自动恢复是真的：目标版本回来就刷新页面", async () => {
    const ctx = loadWatchContext();
    ctx.ApiClient = {
        getUpdateStatus: async () => ({ current_version: "1.0.12", last_apply: { outcome: "started" } }),
    };

    ctx.__testExports.watchApplyUpdateFinish("1.0.12", { intervalMs: 5, timeoutMs: 5000 });
    await nap(40);
    assert.equal(ctx.__reloads, 0, "还在 started 不该刷新");
    // started 期间只许有进度 toast，不许出现报错或成功 toast。
    assert.deepEqual(ctx.__toasts.filter((item) => item.kind !== "info"), [], "started 不该报错");
    assert.match(ctx.__toasts.at(-1).text, /已等 \d+ 秒/, "进行中的 toast 要带页面自己计的秒数");

    ctx.ApiClient = {
        getUpdateStatus: async () => ({ current_version: "1.0.12", last_apply: { outcome: "ok", ref: "v1.0.12" } }),
    };
    await nap(60);
    assert.equal(ctx.__reloads, 1, "版本对上且终态 ok ⇒ 自动刷新一次");
    assert.equal(ctx.__toasts.at(-1).title, "已更新到 v1.0.12");
    ctx.__testExports.stopUpdateApplyWatch();
});

test("执行体落终态失败时立刻说实话，不再假装正在重启", async () => {
    const ctx = loadWatchContext();
    ctx.ApiClient = {
        getUpdateStatus: async () => ({
            current_version: "1.0.11",
            last_apply: { outcome: "upgrade_failed", detail: "exit_accepted_200; install -Upgrade 非零退出" },
        }),
    };

    ctx.__testExports.watchApplyUpdateFinish("1.0.12", { intervalMs: 5, timeoutMs: 5000 });
    await nap(40);
    assert.equal(ctx.__reloads, 0, "失败不刷新");
    assert.equal(ctx.__toasts.at(-1).kind, "error");
    assert.match(ctx.__toasts.at(-1).text, /install -Upgrade 非零退出/);

    const seen = ctx.__toasts.length;
    await nap(40);
    assert.equal(ctx.__toasts.length, seen, "报过一次就停表，不反复刷 toast");
});

test("目标版本号容得下 v 前缀", () => {
    const ctx = loadWatchContext();
    assert.equal(ctx.__testExports.updateApplyTargetVersion("v1.0.12"), "1.0.12");
    assert.equal(ctx.__testExports.updateApplyTargetVersion("1.0.12"), "1.0.12");
    assert.equal(ctx.__testExports.updateApplyTargetVersion(""), "");
});

test("设置行跟着阶段说话，没有阶段时仍只说\"升级执行中\"", () => {
    const api = loadApp();
    const base = { has_ledger: true, newer: true, current_version: "1.0.11", latest_tag: "v1.0.12" };

    assert.equal(api.updateApplyOutcomeText({ ...base, last_apply: { outcome: "started", detail: "port=18790" } }), "升级执行中");
    assert.equal(
        api.updateApplyOutcomeText({ ...base, last_apply: { outcome: "started", stage: "upgrading" } }),
        "升级执行中：正在下载新版本并同步依赖"
    );
});

test("进度文案：一个阶段名 + 页面自己计的秒数，不叠执行体的那份耗时", () => {
    const ctx = loadWatchContext();
    const text = ctx.__testExports.updateApplyProgressText;

    assert.equal(text(null, 0), "正在更新 · 已等 0 秒");
    assert.equal(text({ stage: "requesting_exit" }, 3), "正在暂停进行中的对话与任务 · 已等 3 秒");
    assert.equal(text({ stage: "upgrading", detail: "已 42s" }, 42), "正在下载新版本并同步依赖 · 已等 42 秒");
    assert.equal(text({ stage: "nope" }, 7), "正在更新 · 已等 7 秒");
});

test("停机窗口里 toast 继续走字，阶段停在最后一次读到的那一步", async () => {
    const ctx = loadWatchContext();
    let reachable = true;
    ctx.ApiClient = {
        getUpdateStatus: async () => {
            if (!reachable) throw new TypeError("Failed to fetch");
            return { current_version: "1.0.11", last_apply: { outcome: "started", stage: "upgrading" } };
        },
    };

    ctx.__testExports.watchApplyUpdateFinish("1.0.12", { intervalMs: 100, timeoutMs: 8000 });
    await nap(1100);
    reachable = false;
    const before = ctx.__toasts.at(-1).text;
    await nap(1200);
    const after = ctx.__toasts.at(-1).text;

    assert.match(before, /正在下载新版本并同步依赖 · 已等 \d+ 秒/);
    assert.match(after, /正在下载新版本并同步依赖 · 已等 \d+ 秒/);
    assert.notEqual(after, before, "服务不可达期间秒数要继续涨，界面不能像卡住");
    assert.deepEqual(ctx.__toasts.filter((item) => item.kind === "error"), [], "停机窗口不是失败");
    ctx.__testExports.stopUpdateApplyWatch();
});

const UPDATE_RELEASE_HINT_PAGE_TEXT = "更新会先暂停正在进行的对话与任务，完成后服务自动重启。";

// 检查失败与发布说明弹窗这一族用例需要真的元素桩：按钮开合与提示句都落在 DOM 上。
function loadUpdateDialogContext(item) {
    const elements = {};
    for (const id of [
        "project-settings-check-update-btn",
        "project-settings-check-update-label",
        "project-settings-check-dot",
        "project-settings-version",
        "update-nav-dot",
        "update-release-backdrop",
        "update-release-dialog",
        "update-release-title",
        "update-release-notes",
        "update-release-hint",
        "update-release-manual-link",
        "update-release-cancel-btn",
        "update-release-apply-btn",
    ]) {
        elements[id] = new StubHTMLElement();
    }
    // 页面里那句提示是底稿：JS 从 DOM 读它，不抄第二份，所以桩必须先带上原文。
    elements["update-release-hint"].textContent = UPDATE_RELEASE_HINT_PAGE_TEXT;

    const context = baseContext();
    context.window = context;
    context.__toasts = [];
    context.document.getElementById = (id) => elements[String(id)] || null;
    vm.createContext(context);
    vm.runInContext(
        `${APP_CODE}\nthis.__testExports = {
            U,
            checkForUpdatesNow,
            openUpdateReleaseDialog,
            closeUpdateReleaseDialog,
        };`,
        context
    );
    vm.runInContext(`showToast = (opts) => { __toasts.push(opts); };`, context);
    context.ApiClient = {
        checkUpdateNow: async () => item,
        getUpdateStatus: async () => item,
    };
    vm.runInContext(`S.updateStatus = ${JSON.stringify(item)};`, context);
    return context;
}

test("检查失败按档说话：这台设备没有 git 不是一句「稍后再试」", async () => {
    const ctx = loadUpdateDialogContext({
        has_ledger: true,
        newer: false,
        error: "git_missing",
        detail: "git_missing: PATH、git_path 与常见安装位置都没有 git；remote_unreachable: HTTPS 失败",
    });
    await ctx.__testExports.checkForUpdatesNow();

    const toast = ctx.__toasts.at(-1);
    assert.equal(toast.title, "检查失败");
    assert.match(toast.text, /找不着 git/);
    assert.doesNotMatch(toast.text, /稍后再试/, "结构性失败让人反复点是同一句谎话");
    assert.match(ctx.__testExports.U.projectSettingsCheckUpdate.title, /上次检查失败：git_missing/, "档位原文仍进 title");
});

test("每一档各说一句：没通与超时说稍后再试，没有发布标签说没有标签", async () => {
    const cases = [
        ["remote_unreachable", /GitHub/, /稍后再试/],
        ["timeout", /超时/, /稍后再试/],
        ["no_release_tag", /vX\.Y\.Z/, null],
    ];
    for (const [code, expected, retry] of cases) {
        const ctx = loadUpdateDialogContext({ has_ledger: true, newer: false, error: code, detail: "" });
        await ctx.__testExports.checkForUpdatesNow();
        assert.match(ctx.__toasts.at(-1).text, expected, `${code} 这一档要说清是哪件事`);
        if (retry) assert.match(ctx.__toasts.at(-1).text, retry, `${code} 是瞬时失败，许说稍后再试`);
        else assert.doesNotMatch(ctx.__toasts.at(-1).text, /稍后再试/, `${code} 不是瞬时失败`);
    }
});

test("报出的版本这台设备升不动时，弹窗不给「更新并重启」而是说一句能照着做的", () => {
    const ctx = loadUpdateDialogContext({
        has_ledger: true,
        newer: true,
        latest_tag: "v9.9.9",
        current_version: "1.0.0",
        error: "",
        lane: "http",
        degraded: true,
        detail: "git_missing: PATH 里没有 git",
        actionable: false,
        release_url: "https://github.com/acme/Negi-fork/releases/tag/v9.9.9",
    });
    ctx.__testExports.openUpdateReleaseDialog();

    assert.equal(ctx.__testExports.U.updateReleaseApply.disabled, true, "升不动的新版本不该递出那颗按钮");
    assert.match(ctx.__testExports.U.updateReleaseHint.textContent, /找不着 git/);
    assert.match(ctx.__testExports.U.updateReleaseHint.textContent, /PATH/);
    // 那颗按钮被禁用时，这一枚是这一步仅剩的出口：禁用按钮不许把它一起带走。
    assert.equal(ctx.__testExports.U.updateReleaseManualLink.hidden, false, "升不动时更要给手动安装");

    ctx.__testExports.closeUpdateReleaseDialog();
    assert.equal(
        ctx.__testExports.U.updateReleaseHint.textContent,
        UPDATE_RELEASE_HINT_PAGE_TEXT,
        "关掉弹窗要换回页面自己那句，不改掉下一次打开的默认"
    );
});

test("「手动安装」指向报出来的那一版，地址来自服务端的发布页读数", () => {
    const ctx = loadUpdateDialogContext({
        has_ledger: true,
        newer: true,
        latest_tag: "v1.9.0",
        current_version: "1.8.0",
        actionable: true,
        release_url: "https://github.com/acme/Negi-fork/releases/tag/v1.9.0",
    });
    ctx.__testExports.openUpdateReleaseDialog();

    const link = ctx.__testExports.U.updateReleaseManualLink;
    assert.equal(link.hidden, false);
    assert.equal(link.href, "https://github.com/acme/Negi-fork/releases/tag/v1.9.0");
});

test("读不出可用地址时不递一颗点了停在原页的链接", () => {
    for (const url of ["", "  ", "javascript:alert(1)", "/releases/tag/v1.9.0"]) {
        const ctx = loadUpdateDialogContext({
            has_ledger: true, newer: true, latest_tag: "v1.9.0", current_version: "1.8.0", release_url: url,
        });
        ctx.__testExports.openUpdateReleaseDialog();
        const link = ctx.__testExports.U.updateReleaseManualLink;
        assert.equal(link.hidden, true, `${url || "(空)"} 不是能点开的发布页`);
        assert.equal(link.href, "#");
    }
});

test("手动安装是一颗真的外链，开在弹窗之外", () => {
    const html = fs.readFileSync("g3ku/web/frontend/org_graph.html", "utf8");
    const at = html.indexOf('id="update-release-manual-link"');
    assert.ok(at > 0, "org_graph.html 缺了「手动安装」那一枚");
    const anchor = html.slice(html.lastIndexOf("<a", at), html.indexOf(">", at) + 1);
    assert.match(anchor, /target="_blank"/);
    assert.match(anchor, /rel="noreferrer noopener"/);
    assert.ok(
        html.indexOf('id="update-release-apply-btn"') < at,
        "它写在「更新并重启」旁边，不是另起一处",
    );
});

test("换车道读到的版本仍点亮，但 title 留下为什么换了车道", () => {
    const api = stubbedUpdateApi();
    const title = api.updateSettingsButtonTitle({
        has_ledger: true,
        newer: true,
        latest_tag: "v9.9.9",
        current_version: "1.0.0",
        error: "",
        degraded: true,
        detail: "git_missing: PATH 里没有 git",
        actionable: false,
        checked_at: "2026-11-01T23:33:43+08:00",
    });
    assert.match(title, /发现新版本 v9\.9\.9/);
    assert.match(title, /主车道没读通，这一版读自发布页：git_missing/);
    assert.match(title, /本机更新车道跑不起来/);
});

