const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const APP_PATH = "g3ku/web/frontend/org_graph_app.js";
const APP_CODE = fs.readFileSync(APP_PATH, "utf8");

class StubElement {}
class StubHTMLElement extends StubElement {
    constructor() {
        super();
        this.hidden = false;
        this.disabled = false;
        this.value = "";
        this.checked = false;
        this.textContent = "";
        this.innerHTML = "";
        this.className = "";
        this.dataset = {};
        this.attributes = {};
        this.style = { setProperty() {}, removeProperty() {} };
        this.classList = {
            add: (...tokens) => {
                const classes = new Set(String(this.className || "").split(/\s+/).filter(Boolean));
                tokens.forEach((token) => classes.add(token));
                this.className = [...classes].join(" ");
            },
            remove: (...tokens) => {
                const classes = new Set(String(this.className || "").split(/\s+/).filter(Boolean));
                tokens.forEach((token) => classes.delete(token));
                this.className = [...classes].join(" ");
            },
            contains: (token) => String(this.className || "").split(/\s+/).includes(token),
            toggle: (token, force) => {
                const classes = new Set(String(this.className || "").split(/\s+/).filter(Boolean));
                const shouldAdd = force == null ? !classes.has(token) : !!force;
                if (shouldAdd) classes.add(token);
                else classes.delete(token);
                this.className = [...classes].join(" ");
                return shouldAdd;
            },
        };
    }
    setAttribute(name, value) {
        this.attributes[name] = String(value);
    }
    getAttribute(name) {
        return this.attributes[name];
    }
    addEventListener() {}
    querySelector() {
        return null;
    }
    querySelectorAll() {
        return [];
    }
    focus() {
        this.focused = true;
    }
}

function loadApp(apiClientOverrides = {}) {
    const calls = [];
    const toasts = [];
    const context = {
        console,
        setTimeout,
        clearTimeout,
        setInterval,
        clearInterval,
        queueMicrotask,
        navigator: { clipboard: { writeText: async () => {} } },
        location: {
            protocol: "http:",
            host: "localhost",
            pathname: "/org_graph.html",
            reload() {
                calls.push(["reload"]);
            },
        },
        localStorage: { getItem: () => null, setItem: () => {}, removeItem: () => {} },
        sessionStorage: { getItem: () => null, setItem: () => {}, removeItem: () => {} },
        document: {
            getElementById: () => null,
            querySelector: () => null,
            querySelectorAll: () => [],
            addEventListener() {},
            createElement: () => new StubHTMLElement(),
        },
        window: {},
        Element: StubElement,
        HTMLElement: StubHTMLElement,
        HTMLButtonElement: StubHTMLElement,
        HTMLInputElement: StubHTMLElement,
        HTMLTextAreaElement: StubHTMLElement,
        HTMLSelectElement: StubHTMLElement,
        URLSearchParams,
        URL,
        AbortController,
        ApiClient: {
            getBootstrapStatus: async () => {
                calls.push(["status"]);
                return { mode: "unlocked", auto_unlock: false };
            },
            changeBootstrapPassword: async (payload) => {
                calls.push(["change", payload]);
                return { mode: "unlocked" };
            },
            setBootstrapAutoUnlock: async (enabled) => {
                calls.push(["auto", enabled]);
                return { mode: "unlocked", auto_unlock: !!enabled };
            },
            lockBootstrap: async () => {
                calls.push(["lock"]);
                return { mode: "locked" };
            },
            ...apiClientOverrides,
        },
        fetch: async () => ({ ok: true, json: async () => ({}) }),
        lucide: { createIcons() {} },
        marked: { parse: (value) => String(value) },
        DOMPurify: { sanitize: (value) => String(value) },
        structuredClone: global.structuredClone,
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
    context.window = context;
    vm.createContext(context);
    vm.runInContext(
        `${APP_CODE}
        this.__testExports = {
            U,
            openProjectSettingsDialog,
            closeProjectSettingsDialog,
            isProjectSettingsOpen,
            submitProjectPasswordChange,
            applyProjectAutoUnlockChange,
            lockProjectFromSettings,
        };`,
        context
    );
    vm.runInContext(
        `
        showToast = (payload) => { this.__toasts.push(payload); };
        requestProjectExit = () => { this.__exitCalls = (this.__exitCalls || 0) + 1; };
    `,
        context
    );
    context.__toasts = toasts;
    const app = { ...context.__testExports, calls, toasts, __context: context };
    mountDialog(app);
    return app;
}

function mountDialog(app) {
    const { U } = app;
    U.projectSettings = new StubHTMLElement();
    U.projectSettingsBackdrop = new StubHTMLElement();
    U.projectSettingsDialog = new StubHTMLElement();
    U.projectSettingsClose = new StubHTMLElement();
    U.projectSettingsCurrentPassword = new StubHTMLElement();
    U.projectSettingsNewPassword = new StubHTMLElement();
    U.projectSettingsNewPasswordConfirm = new StubHTMLElement();
    U.projectSettingsChangePassword = new StubHTMLElement();
    U.projectSettingsAutoUnlock = new StubHTMLElement();
    U.projectSettingsLock = new StubHTMLElement();
    U.projectSettingsExit = new StubHTMLElement();
}

const flush = () => new Promise((resolve) => setTimeout(resolve, 0));
// vm 内构造的载荷跨 realm，先序列化回宿主对象再做严格比较。
const callsOf = (app) => JSON.parse(JSON.stringify(app.calls));

test("打开设置弹窗会拉一次解锁状态并按状态回填自动解锁勾选", async () => {
    const app = loadApp({
        getBootstrapStatus: async () => ({ mode: "unlocked", auto_unlock: true }),
    });

    app.openProjectSettingsDialog();
    await flush();

    assert.equal(app.U.projectSettingsBackdrop.hidden, false);
    assert.equal(app.U.projectSettings.attributes["aria-expanded"], "true");
    assert.equal(app.U.projectSettingsAutoUnlock.checked, true);
});

test("关闭设置弹窗清空密码输入并复位 aria", () => {
    const app = loadApp();
    app.U.projectSettingsCurrentPassword.value = "current";
    app.U.projectSettingsNewPassword.value = "next";
    app.U.projectSettingsNewPasswordConfirm.value = "next";

    app.closeProjectSettingsDialog();

    assert.equal(app.U.projectSettingsBackdrop.hidden, true);
    assert.equal(app.U.projectSettings.attributes["aria-expanded"], "false");
    assert.equal(app.U.projectSettingsCurrentPassword.value, "");
    assert.equal(app.U.projectSettingsNewPassword.value, "");
    assert.equal(app.U.projectSettingsNewPasswordConfirm.value, "");
});

test("新密码两次不一致时不发请求", async () => {
    const app = loadApp();
    app.U.projectSettingsCurrentPassword.value = "current";
    app.U.projectSettingsNewPassword.value = "next";
    app.U.projectSettingsNewPasswordConfirm.value = "other";

    await app.submitProjectPasswordChange();

    assert.deepEqual(app.calls, []);
    assert.equal(app.toasts.at(-1).title, "两次输入的新密码不一致");
});

test("改密成功按 snake_case 提交并清空输入", async () => {
    const app = loadApp();
    app.U.projectSettingsCurrentPassword.value = "current";
    app.U.projectSettingsNewPassword.value = "next";
    app.U.projectSettingsNewPasswordConfirm.value = "next";

    await app.submitProjectPasswordChange();

    assert.deepEqual(callsOf(app), [["change", {
        current_password: "current",
        new_password: "next",
        password_confirm: "next",
    }]]);
    assert.equal(app.U.projectSettingsNewPassword.value, "");
    assert.equal(app.U.projectSettingsChangePassword.disabled, false);
});

test("后端报错文案翻成中文提示", async () => {
    const app = loadApp({
        changeBootstrapPassword: async () => {
            throw new Error("invalid password");
        },
    });
    app.U.projectSettingsCurrentPassword.value = "current";
    app.U.projectSettingsNewPassword.value = "next";
    app.U.projectSettingsNewPasswordConfirm.value = "next";

    await app.submitProjectPasswordChange();

    assert.equal(app.toasts.at(-1).text, "当前密码不正确");
});

test("自动解锁失败时回滚勾选", async () => {
    const app = loadApp({
        setBootstrapAutoUnlock: async () => {
            throw new Error("project is locked");
        },
    });
    app.U.projectSettingsAutoUnlock.checked = false;

    await app.applyProjectAutoUnlockChange(true);

    assert.equal(app.U.projectSettingsAutoUnlock.checked, false);
    assert.equal(app.toasts.at(-1).text, "项目已锁定，请先解锁");
});

test("自动解锁成功按服务端返回的状态回填", async () => {
    const app = loadApp();

    await app.applyProjectAutoUnlockChange(true);

    assert.deepEqual(app.calls, [["auto", true]]);
    assert.equal(app.U.projectSettingsAutoUnlock.checked, true);
});

test("锁定项目成功后关闭弹窗并刷新页面", async () => {
    const app = loadApp();
    app.openProjectSettingsDialog();

    await app.lockProjectFromSettings();

    assert.ok(app.calls.some((call) => call[0] === "lock"));
    assert.equal(app.U.projectSettingsBackdrop.hidden, true);
    assert.deepEqual(app.calls.at(-1), ["reload"]);
});
