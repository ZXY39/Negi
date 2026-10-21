const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const { readFrontendSources } = require("./support/frontend_sources.js");

const APP_PATH = "g3ku/web/frontend/org_graph_app.js";
const APP_CODE = readFrontendSources(["app"]).join("\n");
const BASE_CSS_PATH = "g3ku/web/frontend/org_graph.css";
const BASE_CSS_CODE = fs.readFileSync(BASE_CSS_PATH, "utf8");

class StubElement {
    constructor() {
        this.clicks = [];
    }
}

class StubHTMLElement extends StubElement {
    constructor() {
        super();
        this.hidden = false;
        this.disabled = false;
        this.value = "";
        this.checked = false;
        this.files = [];
        this.textContent = "";
        this.className = "";
        this.dataset = {};
        this.attributes = {};
        this.style = { setProperty() {}, removeProperty() {} };
        this.classList = {
            add: () => {},
            remove: () => {},
            contains: () => false,
            toggle: () => false,
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
    click() {
        this.clicks.push(this.href || this.getAttribute("href"));
    }
    remove() {
        this.removed = true;
    }
}

function loadApp(apiClientOverrides = {}) {
    const calls = [];
    const toasts = [];
    const confirmCalls = [];
    const anchors = [];
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
            createElement: (tag) => {
                const element = new StubHTMLElement();
                if (tag === "a") anchors.push(element);
                return element;
            },
            body: { appendChild() {} },
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
            getBootstrapExitCheck: async () => {
                calls.push(["exit-check"]);
                return { has_running_work: false, summary_text: "" };
            },
            exportConfigBundle: async (options) => {
                calls.push(["export", options]);
                return { filename: "g3ku-config-bundle-20260924-010203.g3kucb", entry_count: 6 };
            },
            getConfigBundleDownloadUrl: (filename) => `http://localhost/api/bootstrap/config-bundle/download?filename=${filename}`,
            importConfigBundle: async (file, password, options) => {
                calls.push(["import", file.name, password, options]);
                return { entry_count: 6 };
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
            openConfigBundleDialog,
            closeConfigBundleDialog,
            isConfigBundleOpen,
            submitConfigBundleExport,
            submitConfigBundleImport,
            syncConfigBundleFileName,
            syncConfigBundlePasswordField,
            usesProjectBundlePassword,
            closeProjectSettingsDialog,
            openProjectSettingsDialog,
        };`,
        context
    );
    vm.runInContext(
        `
        showToast = (payload) => { this.__toasts.push(payload); };
        openConfirm = (payload) => {
            this.__confirmCalls.push(payload);
            return { close() {} };
        };
        // 导入收尾用 setTimeout 延后刷新，测试里立即执行以便断言刷新。
        window.setTimeout = (callback) => {
            callback();
            return 1;
        };
    `,
        context
    );
    context.__toasts = toasts;
    context.__confirmCalls = confirmCalls;
    const app = { ...context.__testExports, calls, toasts, confirmCalls, anchors, __context: context };
    mountDialogs(app);
    return app;
}

function mountDialogs(app) {
    const { U } = app;
    U.projectSettings = new StubHTMLElement();
    U.projectSettingsBackdrop = new StubHTMLElement();
    U.projectSettingsBackdrop.hidden = true;
    U.projectSettingsDialog = new StubHTMLElement();
    U.projectSettingsClose = new StubHTMLElement();
    U.projectSettingsOpenPassword = new StubHTMLElement();
    U.projectSettingsOpenBundle = new StubHTMLElement();
    U.passwordChangeBackdrop = new StubHTMLElement();
    U.passwordChangeBackdrop.hidden = true;
    U.passwordChangeDialog = new StubHTMLElement();
    U.passwordChangeClose = new StubHTMLElement();
    U.projectSettingsCurrentPassword = new StubHTMLElement();
    U.projectSettingsNewPassword = new StubHTMLElement();
    U.projectSettingsNewPasswordConfirm = new StubHTMLElement();
    U.projectSettingsChangePassword = new StubHTMLElement();
    U.projectSettingsAutoUnlock = new StubHTMLElement();
    U.projectSettingsLock = new StubHTMLElement();
    U.projectSettingsExit = new StubHTMLElement();
    U.configBundleBackdrop = new StubHTMLElement();
    U.configBundleBackdrop.hidden = true;
    U.configBundleDialog = new StubHTMLElement();
    U.configBundleClose = new StubHTMLElement();
    U.configBundleUseProjectPassword = new StubHTMLElement();
    U.configBundleUseProjectPassword.checked = true;
    U.configBundleExportConfig = new StubHTMLElement();
    U.configBundleExportConfig.checked = true;
    U.configBundleExportMemory = new StubHTMLElement();
    U.configBundleExportMemory.checked = false;
    U.configBundleExportPasswordField = new StubHTMLElement();
    U.configBundleExportPasswordField.hidden = true;
    U.configBundleExportConfirmField = new StubHTMLElement();
    U.configBundleExportConfirmField.hidden = true;
    U.configBundleExportPassword = new StubHTMLElement();
    U.configBundleExportConfirm = new StubHTMLElement();
    U.configBundleExport = new StubHTMLElement();
    U.configBundleImportFile = new StubHTMLElement();
    // 浏览器里清空 file input 的 value 会连带清空 files，桩件要按同一语义走。
    Object.defineProperty(U.configBundleImportFile, "value", {
        get() {
            return this._value || "";
        },
        set(next) {
            this._value = next;
            if (!next) this.files = [];
        },
    });
    U.configBundleImportPick = new StubHTMLElement();
    U.configBundleImportName = new StubHTMLElement();
    U.configBundleImportName.textContent = "未选择文件";
    U.configBundleImportConfig = new StubHTMLElement();
    U.configBundleImportConfig.checked = true;
    U.configBundleImportMemory = new StubHTMLElement();
    U.configBundleImportMemory.checked = false;
    U.configBundleImportPassword = new StubHTMLElement();
    U.configBundleImport = new StubHTMLElement();
}

const flush = () => new Promise((resolve) => setTimeout(resolve, 0));
// vm 内构造的载荷跨 realm，先序列化回宿主对象再做严格比较。
const plain = (value) => JSON.parse(JSON.stringify(value));

test("配置包子窗口开关会收掉口令输入", () => {
    const app = loadApp();
    app.openConfigBundleDialog();
    assert.equal(app.isConfigBundleOpen(), true);

    app.U.configBundleExportPassword.value = "secret-pass-123";
    app.closeConfigBundleDialog();

    assert.equal(app.isConfigBundleOpen(), false);
    assert.equal(app.U.configBundleExportPassword.value, "");
});

test("关闭设置窗口一并收掉配置包子窗口", () => {
    const app = loadApp();
    app.openProjectSettingsDialog();
    app.openConfigBundleDialog();

    app.closeProjectSettingsDialog();

    assert.equal(app.isConfigBundleOpen(), false);
    assert.equal(app.U.projectSettingsBackdrop.hidden, true);
});

test("选中的配置包文件名回显，关窗后复位", () => {
    const app = loadApp();
    assert.equal(app.U.configBundleImportName.textContent, "未选择文件");

    app.U.configBundleImportFile.files = [{ name: "g3ku-config-bundle-20260924-010203.g3kucb" }];
    app.syncConfigBundleFileName();
    assert.equal(app.U.configBundleImportName.textContent, "g3ku-config-bundle-20260924-010203.g3kucb");

    app.closeConfigBundleDialog();
    assert.equal(app.U.configBundleImportFile.files.length, 0);
    assert.equal(app.U.configBundleImportName.textContent, "未选择文件");
});

test("默认勾选时导出口令两栏保持收起", () => {
    const app = loadApp();
    app.openConfigBundleDialog();

    assert.equal(app.usesProjectBundlePassword(), true);
    assert.equal(app.U.configBundleExportPasswordField.hidden, true);
    assert.equal(app.U.configBundleExportConfirmField.hidden, true);
});

test("取消勾选才展开导出口令两栏，关窗后回到默认勾选", () => {
    const app = loadApp();
    app.openConfigBundleDialog();
    app.U.configBundleUseProjectPassword.checked = false;
    app.syncConfigBundlePasswordField();

    assert.equal(app.U.configBundleExportPasswordField.hidden, false);
    assert.equal(app.U.configBundleExportConfirmField.hidden, false);

    app.closeConfigBundleDialog();
    assert.equal(app.U.configBundleUseProjectPassword.checked, true);
    assert.equal(app.U.configBundleExportPasswordField.hidden, true);
});

test("勾选态下不填任何口令就能导出，提交空口令", async () => {
    const app = loadApp();
    app.openConfigBundleDialog();

    await app.submitConfigBundleExport();

    assert.deepEqual(plain(app.calls), [
        ["export", { password: "", useProjectPassword: true, includeConfig: true, includeMemory: false }],
    ]);
    assert.equal(app.anchors.length, 1);
    assert.equal(app.toasts.at(-1).text, "共 6 个文件，导入时输项目解锁密码。");
});

test("自定义口令两次不一致时不发请求", async () => {
    const app = loadApp();
    app.U.configBundleUseProjectPassword.checked = false;
    app.U.configBundleExportPassword.value = "secret-pass-123";
    app.U.configBundleExportConfirm.value = "secret-pass-999";

    await app.submitConfigBundleExport();

    assert.deepEqual(app.calls, []);
    assert.equal(app.toasts.at(-1).title, "两次输入的口令不一致");
});

test("自定义口令导出成功后按服务端返回的文件名触发下载并清空口令", async () => {
    const app = loadApp();
    app.U.configBundleUseProjectPassword.checked = false;
    app.U.configBundleExportPassword.value = "secret-pass-123";
    app.U.configBundleExportConfirm.value = "secret-pass-123";

    await app.submitConfigBundleExport();

    assert.deepEqual(plain(app.calls), [
        ["export", { password: "secret-pass-123", useProjectPassword: false, includeConfig: true, includeMemory: false }],
    ]);
    assert.equal(app.anchors.length, 1);
    assert.deepEqual(app.anchors[0].clicks, [
        "http://localhost/api/bootstrap/config-bundle/download?filename=g3ku-config-bundle-20260924-010203.g3kucb",
    ]);
    assert.equal(app.U.configBundleExportPassword.value, "");
    assert.equal(app.toasts.at(-1).text, "共 6 个文件。口令不会随文件另存，丢了无法导入。");
    assert.equal(app.U.configBundleExport.disabled, false);
});

test("导出失败时按钮恢复可用并提示错误", async () => {
    const app = loadApp({
        exportConfigBundle: async () => {
            throw new Error("这个项目没有设置解锁密码（只用环境变量主密钥解锁），请改用自定义导出口令。");
        },
    });

    await app.submitConfigBundleExport();

    assert.equal(app.toasts.at(-1).title, "导出失败");
    assert.match(app.toasts.at(-1).text, /请改用自定义导出口令/);
    assert.equal(app.U.configBundleExport.disabled, false);
});

test("未选文件或未填包口令时不进入确认弹窗", async () => {
    const noFile = loadApp();
    await noFile.submitConfigBundleImport();
    assert.equal(noFile.toasts.at(-1).title, "请选择配置包文件");
    assert.deepEqual(noFile.confirmCalls, []);

    const app = loadApp();
    app.U.configBundleImportFile.files = [{ name: "bundle.g3kucb" }];
    await app.submitConfigBundleImport();
    assert.equal(app.toasts.at(-1).title, "请输入包口令");
    assert.deepEqual(app.confirmCalls, []);
});

test("导入确认弹窗先收掉两层对话框再弹出", async () => {
    const app = loadApp();
    app.openProjectSettingsDialog();
    app.openConfigBundleDialog();
    app.U.configBundleImportFile.files = [{ name: "bundle.g3kucb" }];
    app.U.configBundleImportPassword.value = "secret-pass-123";

    await app.submitConfigBundleImport();

    const confirm = app.confirmCalls.at(-1);
    assert.equal(app.isConfigBundleOpen(), false);
    assert.equal(app.U.projectSettingsBackdrop.hidden, true);
    assert.equal(confirm.title, "确认导入配置包？");
    assert.equal(confirm.checkbox, null);
    assert.equal(app.toasts.length, 0);
});

test("无在跑工作时确认后按原样导入并刷新页面", async () => {
    const app = loadApp();
    app.U.configBundleImportFile.files = [{ name: "bundle.g3kucb" }];
    app.U.configBundleImportPassword.value = "secret-pass-123";

    await app.submitConfigBundleImport();
    await app.confirmCalls.at(-1).onConfirm({ checked: false, value: "local-pass-1" });
    await flush();

    assert.deepEqual(plain(app.calls), [
        ["exit-check"],
        ["import", "bundle.g3kucb", "secret-pass-123", {
            confirmRunningWork: false,
            currentPassword: "local-pass-1",
            includeConfig: true,
            includeMemory: false,
        }],
        ["reload"],
    ]);
    assert.equal(app.toasts.at(-1).title, "配置包已导入");
    assert.match(app.toasts.at(-1).text, /重启后 worker 才用新配置/);
});

test("导入确认要求本机口令，空值不提交", async () => {
    const app = loadApp();
    app.U.configBundleImportFile.files = [{ name: "bundle.g3kucb" }];
    app.U.configBundleImportPassword.value = "secret-pass-123";

    await app.submitConfigBundleImport();

    const confirm = app.confirmCalls.at(-1);
    assert.deepEqual(plain(confirm.input), {
        label: "当前项目解锁口令",
        placeholder: "导入后继续用它解锁",
        required: true,
    });
    await assert.rejects(() => confirm.onConfirm({ checked: false, value: "   " }), /当前项目解锁口令/);
    assert.deepEqual(app.calls.filter((call) => call[0] === "import"), []);
});

test("保住本机口令时提示文案说口令不变，没口令可保时说改成包口令", async () => {
    const kept = loadApp({
        importConfigBundle: async (file, password, options) => ({ entry_count: 6, password_kept: true, ...options }),
    });
    kept.U.configBundleImportFile.files = [{ name: "bundle.g3kucb" }];
    kept.U.configBundleImportPassword.value = "secret-pass-123";
    await kept.submitConfigBundleImport();
    await kept.confirmCalls.at(-1).onConfirm({ checked: false, value: "local-pass-1" });
    await flush();
    assert.match(kept.toasts.at(-1).text, /解锁口令保持不变/);
    assert.doesNotMatch(kept.toasts.at(-1).text, /改成包口令/);

    const adopted = loadApp({
        importConfigBundle: async () => ({ entry_count: 6, password_kept: false }),
    });
    adopted.U.configBundleImportFile.files = [{ name: "bundle.g3kucb" }];
    adopted.U.configBundleImportPassword.value = "secret-pass-123";
    await adopted.submitConfigBundleImport();
    await adopted.confirmCalls.at(-1).onConfirm({ checked: false, value: "local-pass-1" });
    await flush();
    assert.match(adopted.toasts.at(-1).text, /解锁口令改成包口令/);
});

test("有在跑工作时必须勾选暂停才提交", async () => {
    const app = loadApp({
        getBootstrapExitCheck: async () => ({ has_running_work: true, summary_text: "1 个进行中的对话" }),
    });
    app.U.configBundleImportFile.files = [{ name: "bundle.g3kucb" }];
    app.U.configBundleImportPassword.value = "secret-pass-123";

    await app.submitConfigBundleImport();

    const confirm = app.confirmCalls.at(-1);
    assert.deepEqual(plain(confirm.checkbox), {
        checked: false,
        label: "暂停正在进行的所有对话和任务",
        hint: "1 个进行中的对话",
    });
    assert.match(confirm.text, /检测到1 个进行中的对话/);

    await assert.rejects(() => confirm.onConfirm({ checked: false, value: "local-pass-1" }), /请先勾选/);
    assert.deepEqual(app.calls, []);

    await confirm.onConfirm({ checked: true, value: "local-pass-1" });
    assert.deepEqual(plain(app.calls.at(0)), [
        "import",
        "bundle.g3kucb",
        "secret-pass-123",
        { confirmRunningWork: true, currentPassword: "local-pass-1", includeConfig: true, includeMemory: false },
    ]);
});

test("勾上记忆才提交 includeMemory，提示按面报数", async () => {
    const sent = [];
    const app = loadApp({
        exportConfigBundle: async (options) => {
            sent.push(plain(options));
            return {
                filename: "g3ku-config-bundle-20260924-010203.g3kucb",
                entry_count: 9,
                config_entry_count: 4,
                memory_entry_count: 5,
            };
        },
    });
    app.U.configBundleExportMemory.checked = true;

    await app.submitConfigBundleExport();

    assert.deepEqual(sent, [{
        password: "",
        useProjectPassword: true,
        includeConfig: true,
        includeMemory: true,
    }]);
    assert.equal(app.toasts.at(-1).text, "共 9 个文件（配置 4、记忆 5），导入时输项目解锁密码。");
});

test("范围两颗都不勾时导出与导入都不提交", async () => {
    const app = loadApp();
    app.U.configBundleExportConfig.checked = false;
    await app.submitConfigBundleExport();
    assert.deepEqual(app.calls, []);
    assert.equal(app.toasts.at(-1).title, "请先勾选要导出的范围");

    app.U.configBundleImportFile.files = [{ name: "bundle.g3kucb" }];
    app.U.configBundleImportPassword.value = "secret-pass-123";
    app.U.configBundleImportConfig.checked = false;
    await app.submitConfigBundleImport();
    assert.equal(app.toasts.at(-1).title, "请先勾选要导入的范围");
    assert.equal(app.confirmCalls.length, 0);
});

test("只勾记忆时确认文案与载荷都按记忆面走", async () => {
    const app = loadApp();
    app.U.configBundleImportFile.files = [{ name: "bundle.g3kucb" }];
    app.U.configBundleImportPassword.value = "secret-pass-123";
    app.U.configBundleImportConfig.checked = false;
    app.U.configBundleImportMemory.checked = true;

    await app.submitConfigBundleImport();
    const confirm = app.confirmCalls.at(-1);
    assert.match(confirm.text, /整体替换当前记忆/);

    await confirm.onConfirm({ checked: false, value: "local-pass-1" });
    await flush();
    assert.deepEqual(plain(app.calls.find((call) => call[0] === "import")), [
        "import",
        "bundle.g3kucb",
        "secret-pass-123",
        { confirmRunningWork: false, currentPassword: "local-pass-1", includeConfig: false, includeMemory: true },
    ]);
});

test("关窗把两颗范围复位成配置开、记忆关", () => {
    const app = loadApp();
    app.U.configBundleExportConfig.checked = false;
    app.U.configBundleExportMemory.checked = true;
    app.U.configBundleImportMemory.checked = true;

    app.closeConfigBundleDialog();

    assert.equal(app.U.configBundleExportConfig.checked, true);
    assert.equal(app.U.configBundleExportMemory.checked, false);
    assert.equal(app.U.configBundleImportConfig.checked, true);
    assert.equal(app.U.configBundleImportMemory.checked, false);
});

test("配置包对话框的风险文案不被 max-height 裁剪", () => {
    const rule = (selector) => {
        const match = BASE_CSS_CODE.match(
            new RegExp(`${selector.replace(/[.#]/g, "\\$&")} \\{([^}]*)\\}`)
        );
        return match ? match[1] : null;
    };

    const text = rule("#config-bundle-dialog .confirm-text");
    assert.ok(text, "缺少 #config-bundle-dialog .confirm-text 覆盖，风险文案会退回 24vh 内滚");
    assert.match(text, /max-height:\s*none/);
    assert.match(text, /overflow-y:\s*visible/);

    const body = rule("#config-bundle-dialog .project-settings-body");
    assert.ok(body, "缺少 #config-bundle-dialog .project-settings-body 高度覆盖");
    const cap = body.match(/max-height:\s*min\((\d+)vh,\s*(\d+)px\)/);
    assert.ok(cap, "上限必须是 vh+px 双限：只给 px 会在矮窗口溢出，只给 vh 会在大屏过长");
    assert.ok(Number(cap[1]) >= 80, "vh 上限过低，正常窗口仍要滚动");
    assert.ok(Number(cap[2]) >= 700, "px 上限过低，装不下完整正文");
});

test("锁定态读不到在跑工作时按无在跑工作处理", async () => {
    const app = loadApp({
        getBootstrapExitCheck: async () => {
            throw new Error("locked");
        },
    });
    app.U.configBundleImportFile.files = [{ name: "bundle.g3kucb" }];
    app.U.configBundleImportPassword.value = "secret-pass-123";

    await app.submitConfigBundleImport();

    assert.equal(app.confirmCalls.at(-1).checkbox, null);
});
