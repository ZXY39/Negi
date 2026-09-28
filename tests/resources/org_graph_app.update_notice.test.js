const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

// 新版本提醒的渲染契约：红点只认台账里的 `newer`；"从未检查"与"检查失败"都不亮，
// 也不能被写成"已是最新"。设置面板的「重启并更新」按钮同样只在新版本时出现。

const APP_PATH = "g3ku/web/frontend/org_graph_app.js";
const APP_CODE = fs.readFileSync(APP_PATH, "utf8");

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
        this.classList = { add: () => {}, remove: () => {}, contains: () => false, toggle: () => {} };
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
            updateSettingsLineText,
            updateApplyOutcomeText,
            updateSettingsLineTitle,
            renderProjectSettingsUpdate,
        };`,
        context
    );
    return context.__testExports;
}

function stubbedUpdateApi() {
    const api = loadApp();
    api.U.updateNavDot = new StubHTMLElement();
    api.U.projectSettingsUpdateText = new StubHTMLElement();
    api.U.projectSettingsApplyUpdate = new StubHTMLElement();
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

test("settings line keeps unknown, failed and current apart", () => {
    const api = stubbedUpdateApi();

    api.renderProjectSettingsUpdate({ has_ledger: false });
    assert.match(api.U.projectSettingsUpdateText.textContent, /尚未检查/);
    assert.equal(api.U.projectSettingsApplyUpdate.hidden, true);

    api.renderProjectSettingsUpdate({
        has_ledger: true,
        newer: true,
        current_version: "1.0.1",
        latest_tag: "v1.0.2",
        checked_at: "2026-09-25T17:45:04+08:00",
    });
    const text = api.U.projectSettingsUpdateText.textContent;
    assert.match(text, /当前 v1\.0\.1/);
    assert.match(text, /最新 v1\.0\.2/);
    // 行内不放检查时间：整行可用宽 412px，加上时间就要 443px 会挤到第二行。
    assert.doesNotMatch(text, /检查于/);
    assert.match(api.U.projectSettingsUpdateText.title, /检查于 \d{2}-\d{2} \d{2}:\d{2}/);
    assert.equal(api.U.projectSettingsApplyUpdate.hidden, false);

    api.renderProjectSettingsUpdate({
        has_ledger: true,
        newer: false,
        error: "remote_unreachable",
        checked_at: "2026-09-25T17:45:04+08:00",
    });
    assert.match(api.U.projectSettingsUpdateText.textContent, /上次检查失败/);
    assert.doesNotMatch(api.U.projectSettingsUpdateText.textContent, /已是最新/);
    assert.match(api.U.projectSettingsUpdateText.title, /remote_unreachable/);
});

test("up to date never borrows the newer wording", () => {
    const api = stubbedUpdateApi();
    api.renderProjectSettingsUpdate({ has_ledger: true, newer: false, latest_tag: "v1.0.1", current_version: "1.0.1" });
    assert.match(api.U.projectSettingsUpdateText.textContent, /已是最新/);
    assert.doesNotMatch(api.U.projectSettingsUpdateText.textContent, /最新 v/);
});

test("确认框层级压过叫它起来的弹窗，但仍在 toast 之下", () => {
    const css = fs.readFileSync("g3ku/web/frontend/org_graph.css", "utf8");

    // 按"选择器 + 开括号"定位规则块，再从块里取 z-index 数值（避免正则转义脆弱）。
    const zIndexOf = (blockOpen) => {
        const start = css.indexOf(blockOpen);
        assert.ok(start >= 0, `缺少规则块：${blockOpen}`);
        const body = css.slice(start, css.indexOf("}", start));
        const declared = body.match(/z-index:[ \t]*(\d+)/);
        assert.ok(declared, `规则里没有 z-index：${blockOpen}`);
        return Number(declared[1]);
    };

    const confirm = zIndexOf("#confirm-backdrop {");
    const dialog = zIndexOf(".confirm-backdrop {");
    const rename = zIndexOf("#rename-session-backdrop {");
    const memoryEdit = zIndexOf(".memory-browser-edit-dialog {");
    const toast = zIndexOf(".app-toast-viewport {");
    const approval = zIndexOf(".ceo-approval-viewport {");

    // 同为 1000 时按 DOM 顺序决胜，而 #confirm-backdrop 排在各弹窗之前 ⇒ 必须靠层级压过
    assert.ok(confirm > dialog && confirm > rename && confirm > memoryEdit,
        `确认框必须压过弹窗族：${confirm} vs ${dialog}/${rename}/${memoryEdit}`);
    assert.ok(confirm < toast && confirm < approval,
        `toast 与审批浮层必须仍在确认框之上：${toast}/${approval} vs ${confirm}`);
});

test("升级终态不回落成\"正在重启\"：失败占行、成功让回版本对", () => {
    const api = loadApp();
    const base = { has_ledger: true, newer: true, current_version: "1.0.11", latest_tag: "v1.0.12", checked_at: "2026-09-28T13:15:20+08:00" };

    // 没有记录 / ok：这一行仍归版本对
    assert.equal(api.updateApplyOutcomeText(base), "");
    assert.equal(api.updateApplyOutcomeText({ ...base, last_apply: { outcome: "ok", at: "2026-09-28T13:30:00+08:00" } }), "");
    assert.match(api.updateSettingsLineText(base), /当前 v1\.0\.11 · 最新 v1\.0\.12/);

    // 脏树被守卫拒掉的那次：端口没退干净 ⇒ 代码未动
    const busy = { ...base, last_apply: { outcome: "port_busy", at: "2026-09-28T13:19:05+08:00", detail: "exit_accepted_200; port 18790 still busy" } };
    assert.equal(api.updateApplyOutcomeText(busy), "升级未完成：服务没退干净");
    assert.equal(api.updateSettingsLineText(busy), "升级未完成：服务没退干净");
    assert.match(api.updateSettingsLineTitle(busy), /上次升级 port_busy .*：exit_accepted_200/);

    // 执行体刚起来：只说执行中，不谎报完成
    assert.equal(api.updateApplyOutcomeText({ ...base, last_apply: { outcome: "started", detail: "port=18790" } }), "升级执行中");

    // 未知终态兜底：宁可露出原码，也不要显示成"已是最新"
    assert.equal(api.updateApplyOutcomeText({ ...base, last_apply: { outcome: "weird_code" } }), "升级未完成：weird_code");
});
