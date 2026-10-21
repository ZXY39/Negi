"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const vm = require("node:vm");
const { readFrontendSources } = require("./support/frontend_sources.js");
const {
    StubElement,
    StubHTMLElement,
    StubDocument,
    StubHTMLButtonElement,
    StubHTMLInputElement,
    StubHTMLTextAreaElement,
    StubHTMLSelectElement,
} = require("./support/frontend_stub.js");

const APP_CODE = readFrontendSources(["app"]).join("\n");

function loadUi() {
    const context = {
        console,
        setTimeout,
        clearTimeout,
        setInterval,
        clearInterval,
        queueMicrotask,
        navigator: { clipboard: { writeText: async () => {} } },
        location: { protocol: "http:", host: "localhost", pathname: "/org_graph.html" },
        localStorage: { getItem: () => null, setItem: () => {}, removeItem: () => {} },
        sessionStorage: { getItem: () => null, setItem: () => {}, removeItem: () => {} },
        document: new StubDocument(),
        window: {},
        Element: StubElement,
        HTMLElement: StubHTMLElement,
        HTMLButtonElement: StubHTMLButtonElement,
        HTMLInputElement: StubHTMLInputElement,
        HTMLTextAreaElement: StubHTMLTextAreaElement,
        HTMLSelectElement: StubHTMLSelectElement,
        URLSearchParams,
        URL,
        AbortController,
        ApiClient: { getActiveSessionId: () => "" },
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
        `${APP_CODE}\nthis.__testExports = { S, U, showToast, closeToast, openConfirm, closeConfirm, acceptConfirm, requestInlineConfirm, setDrawerOpen };`,
        context
    );
    return { ...context.__testExports, __context: context };
}

function installToastRefs(ui) {
    const title = new StubHTMLElement();
    const text = new StubHTMLElement();
    const close = new StubHTMLElement();
    const icon = new StubHTMLElement();
    const toast = new StubHTMLElement();
    ui.U.toast = toast;
    ui.U.toastTitle = title;
    ui.U.toastText = text;
    ui.U.toastClose = close;
    ui.U.toastIcon = icon;
    ui.S.toastState = { timeoutId: null, intervalId: null };
    return { toast, title, text, close, icon };
}

function installConfirmRefs(ui) {
    const refs = {
        title: new StubHTMLElement(),
        text: new StubHTMLElement(),
        accept: new StubHTMLElement(),
        cancel: new StubHTMLElement(),
        backdrop: new StubHTMLElement(),
        options: new StubHTMLElement(),
        checkbox: new StubHTMLElement(),
        checkboxLabel: new StubHTMLElement(),
        checkboxHint: new StubHTMLElement(),
        checkboxDetails: new StubHTMLElement(),
        inputRow: new StubHTMLElement(),
        input: new StubHTMLElement(),
        inputLabel: new StubHTMLElement(),
    };
    ui.U.confirmTitle = refs.title;
    ui.U.confirmText = refs.text;
    ui.U.confirmAccept = refs.accept;
    ui.U.confirmCancel = refs.cancel;
    ui.U.confirmBackdrop = refs.backdrop;
    ui.U.confirmOptions = refs.options;
    ui.U.confirmCheckbox = refs.checkbox;
    ui.U.confirmCheckboxLabel = refs.checkboxLabel;
    ui.U.confirmCheckboxHint = refs.checkboxHint;
    ui.U.confirmCheckboxDetails = refs.checkboxDetails;
    ui.U.confirmInputRow = refs.inputRow;
    ui.U.confirmInput = refs.input;
    ui.U.confirmInputLabel = refs.inputLabel;
    ui.S.confirmState = null;
    refs.backdrop.classList.add("is-open");
    return refs;
}

test("toast 写入文案与角色，spin 时摘掉关闭键并显示进度图标", () => {
    const ui = loadUi();
    const refs = installToastRefs(ui);
    ui.showToast({ title: "压缩", text: "进行中", kind: "error", durationMs: 0, spin: true });
    assert.equal(refs.toast.hidden, false);
    assert.equal(refs.title.textContent, "压缩");
    assert.equal(refs.text.textContent, "进行中");
    assert.equal(refs.toast.getAttribute("role"), "alert");
    assert.equal(refs.close.hidden, true);
    assert.equal(refs.icon.hidden, false);
    assert.match(refs.toast.className, /is-open/);
    assert.match(refs.toast.className, /is-error/);
    assert.equal(ui.S.toastState.timeoutId, null);
});

test("toast 到期自动关闭并清掉定时器", () => {
    const ui = loadUi();
    const refs = installToastRefs(ui);
    const timers = [];
    ui.__context.setTimeout = (fn, ms) => {
        timers.push({ fn, ms, cleared: false });
        return timers.length;
    };
    ui.__context.clearTimeout = (id) => {
        if (timers[id - 1]) timers[id - 1].cleared = true;
    };
    ui.showToast({ title: "完成", text: "已保存", durationMs: 3000 });
    assert.equal(timers.length, 1);
    assert.equal(timers[0].ms, 3000);
    timers[0].fn();
    assert.equal(refs.toast.hidden, true);
    assert.equal(refs.toast.className, "app-toast");
    assert.equal(ui.S.toastState.timeoutId, null);
});

test("确认框打开时聚焦取消键，关闭时回到触发元素并回调 onClose", () => {
    const ui = loadUi();
    const refs = installConfirmRefs(ui);
    const trigger = new StubHTMLElement();
    let closed = 0;
    ui.openConfirm({
        title: "删除",
        text: "确认删除？",
        onConfirm: async () => {},
        onClose: () => { closed += 1; },
        returnFocus: trigger,
    });
    assert.equal(refs.title.textContent, "删除");
    assert.equal(refs.text.textContent, "确认删除？");
    assert.equal(refs.backdrop.hidden, false);
    assert.equal(refs.cancel._focused, true);
    ui.closeConfirm();
    assert.equal(refs.backdrop.hidden, true);
    assert.equal(closed, 1);
    assert.equal(trigger._focused, true);
});

test("确认框接受时把勾选与输入值交给 onConfirm，失败则走错误 toast", async () => {
    const ui = loadUi();
    const refs = installConfirmRefs(ui);
    let payload = null;
    ui.openConfirm({
        title: "清理",
        text: "确认清理？",
        checkbox: { label: "同时删除任务", checked: true },
        input: { label: "备注", placeholder: "原因" },
        onConfirm: async (value) => { payload = value; },
    });
    assert.equal(refs.options.hidden, false);
    assert.equal(refs.checkbox.checked, true);
    assert.equal(refs.inputRow.hidden, false);
    ui.U.confirmInput.value = "测试";
    await ui.acceptConfirm();
    assert.equal(payload.checked, true);
    assert.equal(payload.value, "测试");
    assert.equal(refs.backdrop.hidden, true);

    const toastRefs = installToastRefs(ui);
    ui.openConfirm({
        title: "失败",
        text: "会抛错",
        onConfirm: async () => { throw new Error("boom"); },
    });
    await ui.acceptConfirm();
    assert.equal(toastRefs.title.textContent, "操作失败");
    assert.equal(toastRefs.text.textContent, "boom");
    assert.equal(ui.S.confirmState.accepted, false);
    ui.closeToast();
});

test("抽屉原语开关都写 is-open 与 aria-hidden，并在开时接手、关时归还焦点", () => {
    const ui = loadUi();
    const backdrop = new StubHTMLElement();
    const drawer = new StubHTMLElement();
    const focusable = new StubHTMLElement();
    const trigger = new StubHTMLElement();
    let focusableFocused = 0;
    let triggerFocused = 0;
    focusable.focus = () => { focusableFocused += 1; };
    trigger.focus = () => { triggerFocused += 1; };
    drawer.querySelector = (selector) => String(selector).includes("data-modal-close") ? focusable : null;
    ui.__context.document.activeElement = trigger;

    ui.setDrawerOpen(backdrop, drawer, false);
    assert.equal(backdrop.classList.contains("is-open"), false);
    assert.equal(backdrop.getAttribute("aria-hidden"), "true");
    assert.equal(triggerFocused, 0, "从未打开过就没有可归还的焦点");

    ui.setDrawerOpen(backdrop, drawer, true);
    assert.equal(backdrop.classList.contains("is-open"), true);
    assert.equal(drawer.classList.contains("is-open"), true);
    assert.equal(backdrop.getAttribute("aria-hidden"), "false");
    assert.equal(drawer.getAttribute("aria-hidden"), "false");
    assert.equal(focusableFocused, 1);

    ui.setDrawerOpen(backdrop, drawer, false);
    assert.equal(backdrop.classList.contains("is-open"), false);
    assert.equal(drawer.getAttribute("aria-hidden"), "true");
    assert.equal(triggerFocused, 1);

    // 已打开时重复 open 不再抢焦点，焦点仍留给用户正在操作的元素。
    ui.setDrawerOpen(backdrop, drawer, true);
    const focusCountAfterReopen = focusableFocused;
    ui.setDrawerOpen(backdrop, drawer, true);
    assert.equal(focusableFocused, focusCountAfterReopen);
    assert.equal(triggerFocused, 1);
});
