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
    const S = {};
    const context = {
        S,
        U,
        console,
        document: { getElementById: (id) => (id === "confirm-dialog" ? dialog : null) },
        window: { requestAnimationFrame: (cb) => cb() },
        showToast: () => {},
    };
    vm.createContext(context);
    vm.runInContext(`${CODE}
        this.__t = { openConfirm, closeConfirm, acceptConfirm };`, context);
    return { t: context.__t, input, U, S };
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

// 与 requestInlineConfirm 同形的 promise 封装：resolve 由 acceptConfirm 触发。
function askConfirm(ui, title) {
    return new Promise((resolve) => {
        ui.t.openConfirm({
            title,
            text: title,
            confirmLabel: "继续",
            onConfirm: async () => resolve({ confirmed: true }),
            onClose: () => resolve({ confirmed: false }),
        });
    });
}

test("确认框：串联的第二张弹窗不被上一张的收尾关掉", async () => {
    const ui = loadUi();
    const { U } = ui;

    const first = askConfirm(ui, "清除该群历史文件");
    const acceptFirst = ui.t.acceptConfirm(); // 用户点「继续」
    const firstResult = await first; // onConfirm 里 resolve，调用方拿到的是第一个微任务
    assert.equal(firstResult.confirmed, true);

    const second = askConfirm(ui, "再次确认"); // 调用方在这里建好第二张并换掉 S.confirmState
    await acceptFirst; // 上一张的收尾这时才落地

    // 收尾不认状态就会拆掉这张新弹窗，并触发它的 onClose 把用户判成"取消"：
    // 清除群历史因此永远发不出请求，也不留任何回执。
    assert.equal(U.confirmBackdrop.hidden, false);
    assert.equal(U.confirmTitle.textContent, "再次确认");
    assert.equal(ui.S.confirmState?.accepted, false);

    const acceptSecond = ui.t.acceptConfirm();
    assert.equal((await second).confirmed, true);
    await acceptSecond;
    assert.equal(U.confirmBackdrop.hidden, true); // 没人接棒时仍按原样收干净
});
