const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { readFrontendSources } = require("./support/frontend_sources.js");
const { StubElement, StubHTMLElement, StubDocument } = require("./support/frontend_stub.js");

const CODE = readFrontendSources(["app"]).join("\n");
const HTML_PATH = path.resolve(__dirname, "..", "..", "g3ku", "web", "frontend", "org_graph.html");

function loadApp({ imageComplete = false, naturalWidth = 0 } = {}) {
    const document = new StubDocument();
    const button = new StubHTMLElement("BUTTON");
    const popover = new StubHTMLElement("DIV");
    popover.hidden = true;
    const image = new StubHTMLElement("IMG");
    image.complete = imageComplete;
    image.naturalWidth = naturalWidth;
    const placeholder = new StubHTMLElement("DIV");
    placeholder.hidden = true;
    document._byId["project-settings-qq-qr-btn"] = button;
    document._byId["project-settings-qq-qr-popover"] = popover;
    document._byId["project-settings-qq-qr-image"] = image;
    document._byId["project-settings-qq-qr-placeholder"] = placeholder;
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
            getItem: () => null,
            setItem() {},
            removeItem() {},
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
    context.window = context;
    vm.createContext(context);
    vm.runInContext(
        `${CODE}\nthis.__testExports = {
            isProjectSettingsQrOpen,
            setProjectSettingsQrOpen,
            closeProjectSettingsQrPopover,
            syncProjectSettingsQrImage,
            bindProjectSettingsQr,
        };`,
        context,
    );
    return { ...context.__testExports, button, popover, image, placeholder };
}

test("设置标题旁的信息行与二维码入口写在页面骨架里", () => {
    const html = fs.readFileSync(HTML_PATH, "utf8");
    assert.ok(html.includes('href="https://github.com/ZXY39/Negi"'), "缺少 GitHub 仓库链接");
    assert.ok(html.includes('target="_blank" rel="noreferrer noopener"'), "外链必须交系统浏览器打开");
    assert.ok(html.includes("交流QQ群：1130255086"), "缺少交流 QQ 群号");
    assert.ok(html.includes('id="project-settings-qq-qr-btn"'), "缺少二维码按钮");
    assert.ok(html.includes('id="project-settings-qq-qr-popover"'), "缺少二维码弹层");
    assert.ok(html.includes('src="assets/qq-group-qrcode.png"'), "二维码图片地址没有预置");
});

test("二维码按钮切换弹层并把展开态写回 aria-expanded", () => {
    const app = loadApp();
    app.bindProjectSettingsQr();
    app.button.dispatchEvent({ type: "click", target: app.button });
    assert.equal(app.popover.hidden, false);
    assert.equal(app.button.getAttribute("aria-expanded"), "true");
    app.button.dispatchEvent({ type: "click", target: app.button });
    assert.equal(app.popover.hidden, true);
    assert.equal(app.button.getAttribute("aria-expanded"), "false");
});

test("关闭弹层是幂等的，返回值告诉调用方这次有没有真的收起", () => {
    const app = loadApp();
    app.bindProjectSettingsQr();
    app.setProjectSettingsQrOpen(true);
    assert.equal(app.closeProjectSettingsQrPopover(), true);
    assert.equal(app.closeProjectSettingsQrPopover(), false);
    assert.equal(app.popover.hidden, true);
});

test("二维码文件缺席时显示占位，不出现破图", () => {
    const app = loadApp();
    app.bindProjectSettingsQr();
    app.image.dispatchEvent({ type: "error", target: app.image });
    assert.equal(app.image.hidden, true);
    assert.equal(app.placeholder.hidden, false);
});

test("二维码文件已加载时露出图片并收起占位", () => {
    const app = loadApp({ imageComplete: true, naturalWidth: 196 });
    app.bindProjectSettingsQr();
    assert.equal(app.image.hidden, false);
    assert.equal(app.placeholder.hidden, true);
});
