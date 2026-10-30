const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const CODE = fs.readFileSync("g3ku/web/frontend/ui/confirm.js", "utf8");

function makeClassList() {
    const set = new Set();
    return {
        add: (name) => set.add(name),
        remove: (name) => set.delete(name),
        toggle: (name, on) => (on ? set.add(name) : set.delete(name)),
        contains: (name) => set.has(name),
    };
}

function makeInput() {
    return {
        value: "",
        type: "text",
        disabled: false,
        attrs: {},
        setAttribute(name, value) { this.attrs[name] = String(value); },
        getAttribute(name) { return Object.prototype.hasOwnProperty.call(this.attrs, name) ? this.attrs[name] : null; },
        focus() {},
    };
}

function loadUi() {
    const dialog = { classList: makeClassList() };
    const input = makeInput();
    const U = {
        confirmTitle: { textContent: "" },
        confirmText: { textContent: "" },
        confirmInputRow: { hidden: true },
        confirmInput: input,
        confirmInputLabel: { textContent: "" },
        confirmOptions: { hidden: true },
        confirmCheckbox: { checked: false, disabled: false },
        confirmCheckboxLabel: { textContent: "" },
        confirmCheckboxHint: { textContent: "" },
        confirmCheckboxDetails: { hidden: true, textContent: "" },
        confirmAccept: { textContent: "", className: "", disabled: false },
        confirmCancel: { disabled: false, focus() {} },
        confirmBackdrop: { hidden: true, classList: makeClassList() },
    };
    const context = {
        S: {},
        U,
        console,
        document: { getElementById: (id) => (id === "confirm-dialog" ? dialog : null) },
        window: { requestAnimationFrame: (cb) => cb() },
        showToast: () => {},
    };
    vm.createContext(context);
    vm.runInContext(`${CODE}
        this.__t = { openConfirm, closeConfirm };`, context);
    return { t: context.__t, input, U };
}

test("确认框：占位符跟着本次调用走，不沿用上一个弹窗的", () => {
    const { t, input } = loadUi();
    t.openConfirm({
        title: "注册 Master",
        text: "编号",
        input: { label: "发送者编号", placeholder: "0002" },
        onConfirm: () => {},
    });
    assert.equal(input.getAttribute("placeholder"), "0002");

    t.openConfirm({
        title: "确认注册 Master",
        text: "口令",
        input: { label: "项目解锁口令", type: "password" },
        onConfirm: () => {},
    });
    assert.equal(input.type, "password");
    assert.equal(input.value, "");
    assert.equal(input.getAttribute("placeholder"), "");
});
