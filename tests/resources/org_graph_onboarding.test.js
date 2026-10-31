"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");
const { readFrontendSources } = require("./support/frontend_sources.js");

const SOURCE = readFrontendSources(["onboarding"]).join("\n").replace(/\r\n/g, "\n");
const HTML = fs
    .readFileSync(path.join(__dirname, "..", "..", "g3ku", "web", "frontend", "org_graph.html"), "utf8")
    .replace(/\r\n/g, "\n");
const CSS = fs
    .readFileSync(path.join(__dirname, "..", "..", "g3ku", "web", "frontend", "org_graph.css"), "utf8")
    .replace(/\r\n/g, "\n");

const PERSONA_CHAIN_TOAST = "为了使人设生效，主agent和记忆agent模型链不能为空，点击保存后生效";

function makeElement(id, { backdrop = false } = {}) {
    const listeners = {};
    const classes = new Set();
    const element = {
        id,
        hidden: backdrop,
        disabled: false,
        value: "",
        textContent: "",
        innerHTML: "",
        className: "",
        dataset: {},
        children: [],
        classList: {
            toggle(token, force) {
                const shouldAdd = typeof force === "boolean" ? force : !classes.has(token);
                if (shouldAdd) classes.add(token);
                else classes.delete(token);
                return shouldAdd;
            },
            add(...tokens) {
                tokens.forEach((token) => classes.add(token));
            },
            remove(...tokens) {
                tokens.forEach((token) => classes.delete(token));
            },
            contains(token) {
                return classes.has(token);
            },
        },
        addEventListener(type, handler) {
            listeners[type] = handler;
        },
        removeEventListener(type) {
            delete listeners[type];
        },
        appendChild(child) {
            this.children.push(child);
        },
        focus() {},
        click() {
            listeners.click?.({ target: this, preventDefault() {}, stopPropagation() {} });
        },
        fire(type, event = {}) {
            listeners[type]?.({ target: this, preventDefault() {}, stopPropagation() {}, ...event });
        },
    };
    return element;
}

function buildHarness({
    status,
    completeResult = { ok: true, jobs: ["persona_enqueued"] },
    persona = { description: "", photos: [] },
} = {}) {
    const ids = [
        "persona-onboarding-backdrop",
        "persona-onboarding-dialog",
        "persona-onboarding-photo-input",
        "persona-onboarding-photo-pick",
        "persona-onboarding-photo-status",
        "persona-onboarding-photo-grid",
        "persona-onboarding-description",
        "persona-onboarding-error",
        "persona-onboarding-skip-btn",
        "persona-onboarding-next-btn",
        "persona-dialog-backdrop",
        "persona-dialog",
        "persona-dialog-description",
        "persona-dialog-description-empty",
        "persona-dialog-description-input",
        "persona-dialog-description-actions",
        "persona-dialog-description-save",
        "persona-dialog-description-cancel",
        "persona-dialog-photo-grid",
        "persona-dialog-close-btn",
        "persona-dialog-close",
        "persona-button-dot",
        "persona-nav-dot",
        "project-settings-btn",
        "project-settings-open-persona-btn",
    ];
    const elements = Object.fromEntries(
        ids.map((id) => [id, makeElement(id, { backdrop: id.endsWith("-backdrop") })]),
    );
    const documentListeners = {};
    const windowListeners = {};
    const intervals = [];
    const calls = {
        toasts: [],
        skipped: 0,
        savedDescription: [],
        savedPersona: [],
        completed: 0,
        views: [],
        editing: 0,
        loadModels: 0,
        startCreateModel: 0,
        toastClosed: 0,
        viewerOpens: [],
        viewerOpen: false,
    };

    const document = {
        getElementById(id) {
            return elements[id] || null;
        },
        createElement(tag) {
            return makeElement(`created-${tag}`);
        },
        addEventListener(type, handler) {
            documentListeners[type] = handler;
        },
    };

    const context = {
        console,
        document,
        setTimeout: (handler, ms) => {
            const timer = setTimeout(handler, ms);
            timer.unref?.();
            return timer;
        },
        clearTimeout,
        setInterval: (handler, ms) => {
            const timer = setInterval(handler, ms);
            timer.unref?.();
            intervals.push({ handler, timer });
            return timer;
        },
        clearInterval: (timer) => {
            clearInterval(timer);
            const index = intervals.findIndex((entry) => entry.timer === timer);
            if (index >= 0) intervals.splice(index, 1);
        },
        Promise,
        FormData: class FormData {},
    };
    context.window = context;
    context.G3kuBoot = { isUnlocked: () => true };
    context.ApiClient = {
        async getOnboardingStatus() {
            return status;
        },
        async saveOnboardingPersona(description) {
            calls.savedDescription.push(description);
            if (description) persona.description = description;
            return { ok: true };
        },
        async skipOnboardingPersona() {
            calls.skipped += 1;
            return { ok: true };
        },
        async completeOnboarding() {
            calls.completed += 1;
            return completeResult;
        },
        async getPersona() {
            return { ok: true, description: persona.description, photos: persona.photos };
        },
        async savePersona(description) {
            calls.savedPersona.push(description);
            persona.description = description;
            return { ok: true, description };
        },
        async uploadPersonaPhotos() {
            return [];
        },
    };
    context.showToast = (payload) => calls.toasts.push(payload);
    context.closeToast = () => {
        calls.toastClosed += 1;
    };
    context.switchView = (view) => calls.views.push(view);
    context.startModelRoleEditing = () => {
        calls.editing += 1;
    };
    context.openMediaViewer = (payload) => {
        calls.viewerOpens.push(payload);
        calls.viewerOpen = true;
        return true;
    };
    context.closeMediaViewer = () => {
        if (!calls.viewerOpen) return false;
        calls.viewerOpen = false;
        return true;
    };
    context.window.loadModels = async () => {
        calls.loadModels += 1;
    };
    context.window.startCreateModel = () => {
        calls.startCreateModel += 1;
    };
    context.S = { modelCatalog: { roleDrafts: { ceo: [], memory: [] } } };
    context.addEventListener = (type, handler) => {
        windowListeners[type] = handler;
    };

    vm.createContext(context);
    vm.runInContext(SOURCE, context, { filename: "org_graph_onboarding.js" });
    return { context, elements, calls, documentListeners, windowListeners, intervals };
}

function flush() {
    return new Promise((resolve) => setTimeout(resolve, 0));
}

test("persona onboarding opens on first run, skip moves to the model step", async () => {
    const harness = buildHarness({
        status: {
            should_run: true,
            completed: false,
            persona_step_done: false,
            has_models: false,
            has_persona: false,
            has_persona_input: false,
            description: "旧描述",
            photos: [],
            photo_album: [],
        },
    });
    harness.documentListeners.DOMContentLoaded();
    await flush();

    assert.equal(harness.elements["persona-onboarding-backdrop"].hidden, false);
    assert.equal(
        harness.elements["persona-onboarding-backdrop"].classList.contains("is-open"),
        true,
        "the backdrop must be visible and interactive, not only unhidden",
    );
    assert.equal(harness.elements["persona-onboarding-description"].value, "旧描述");

    // A backdrop click must not discard the first-run step; only 跳过 (or 下一步) does.
    harness.elements["persona-onboarding-backdrop"].click();
    assert.equal(harness.elements["persona-onboarding-backdrop"].hidden, false);
    assert.equal(harness.elements["persona-onboarding-backdrop"].classList.contains("is-open"), true);

    harness.elements["persona-onboarding-skip-btn"].click();
    await flush();
    assert.equal(harness.calls.skipped, 1);
    assert.equal(harness.elements["persona-onboarding-backdrop"].hidden, true);
    assert.equal(harness.elements["persona-onboarding-backdrop"].classList.contains("is-open"), false);
    assert.equal(harness.calls.loadModels, 1);
    assert.equal(harness.calls.startCreateModel, 1);
    assert.equal(harness.elements["persona-onboarding-description"].value, "");
    assert.ok(!harness.calls.toasts.some((item) => item.text === PERSONA_CHAIN_TOAST));
});

test("chain gate requires both main and memory chains when persona input exists", async () => {
    const harness = buildHarness({
        status: {
            should_run: true,
            completed: false,
            persona_step_done: true,
            has_models: true,
            has_persona: false,
            has_persona_input: true,
            description: "",
            photos: ["me.png"],
            photo_album: [{ name: "me.png", url: "u", thumb_url: "t" }],
        },
    });
    harness.documentListeners.DOMContentLoaded();
    await flush();

    assert.equal(harness.context.G3kuOnboarding.validateChainsBeforeSave(), PERSONA_CHAIN_TOAST);
    harness.context.S.modelCatalog.roleDrafts = { ceo: ["m1"], memory: [] };
    assert.equal(harness.context.G3kuOnboarding.validateChainsBeforeSave(), PERSONA_CHAIN_TOAST);
    harness.context.S.modelCatalog.roleDrafts = { ceo: ["m1"], memory: ["m2"] };
    assert.equal(harness.context.G3kuOnboarding.validateChainsBeforeSave(), "");
});

test("saving chains completes onboarding once and reports the jobs", async () => {
    const harness = buildHarness({
        status: {
            should_run: true,
            completed: false,
            persona_step_done: true,
            has_models: true,
            has_persona: false,
            has_persona_input: true,
            description: "",
            photos: ["me.png"],
            photo_album: [{ name: "me.png", url: "u", thumb_url: "t" }],
        },
    });
    harness.documentListeners.DOMContentLoaded();
    await flush();

    await harness.context.G3kuOnboarding.onChainsSaved();
    assert.equal(harness.calls.completed, 1);
    assert.ok(
        harness.calls.toasts.some((item) => item.title === "人设已提交"),
        JSON.stringify(harness.calls.toasts),
    );
    await harness.context.G3kuOnboarding.onChainsSaved();
    assert.equal(harness.calls.completed, 1);
});

test("already-configured chains complete onboarding without forcing a chain edit", async () => {
    const harness = buildHarness({
        status: {
            should_run: true,
            completed: false,
            persona_step_done: true,
            has_models: true,
            has_persona: false,
            has_persona_input: false,
            description: "",
            photos: [],
            photo_album: [],
        },
    });
    harness.context.S.modelCatalog.roles = {
        ceo: ["m1"],
        execution: ["m1"],
        inspection: ["m1"],
        memory: ["m2"],
    };
    harness.documentListeners.DOMContentLoaded();
    await flush();
    assert.equal(harness.calls.completed, 1);
    assert.equal(harness.calls.editing, 0);
});

test("a persona with an empty saved memory chain still enters chain editing", async () => {
    const harness = buildHarness({
        status: {
            should_run: true,
            completed: false,
            persona_step_done: true,
            has_models: true,
            has_persona: false,
            has_persona_input: true,
            description: "",
            photos: ["me.png"],
            photo_album: [{ name: "me.png", url: "u", thumb_url: "t" }],
        },
    });
    harness.context.S.modelCatalog.roles = {
        ceo: ["m1"],
        execution: ["m1"],
        inspection: ["m1"],
        memory: [],
    };
    harness.documentListeners.DOMContentLoaded();
    await flush();
    assert.equal(harness.calls.completed, 0);
    assert.equal(harness.calls.editing, 1);

    harness.context.S.modelCatalog.roles.memory = ["m2"];
    await harness.context.G3kuOnboarding.onChainsSaved();
    assert.equal(harness.calls.completed, 1);
});

test("empty persona shows red dots and can be initialized from the dialog", async () => {
    const harness = buildHarness({
        status: {
            should_run: false,
            completed: true,
            persona_step_done: true,
            has_models: true,
            has_persona: false,
            has_persona_input: false,
            description: "",
            photos: [],
            photo_album: [],
        },
    });
    harness.documentListeners.DOMContentLoaded();
    await flush();
    assert.equal(harness.elements["persona-nav-dot"].hidden, false);
    assert.equal(harness.elements["persona-button-dot"].hidden, false);

    await harness.context.G3kuOnboarding.openPersonaDialog();
    assert.equal(harness.elements["persona-dialog-backdrop"].hidden, false);
    assert.equal(harness.elements["persona-dialog-backdrop"].classList.contains("is-open"), true);
    assert.equal(harness.elements["persona-dialog-description-empty"].hidden, false);
    assert.equal(harness.elements["persona-dialog-description"].hidden, true);
    assert.equal(
        harness.elements["persona-dialog-photo-grid"].children[0].textContent,
        "图片为空，如果需要增加，请在会话中发送",
    );
    assert.match(
        harness.elements["persona-dialog-photo-grid"].children[0].className,
        /persona-photo-empty/,
    );

    harness.elements["persona-dialog-description-empty"].click();
    assert.equal(harness.elements["persona-dialog-description-input"].hidden, false);
    harness.elements["persona-dialog-description-input"].value = "你是测试助手，回答先给结论。";
    harness.elements["persona-dialog-description-save"].click();
    await flush();
    await flush();

    assert.deepEqual(harness.calls.savedPersona, ["你是测试助手，回答先给结论。"]);
    assert.equal(harness.elements["persona-nav-dot"].hidden, true);
    assert.equal(harness.elements["persona-button-dot"].hidden, true);
    assert.equal(harness.elements["persona-dialog-description"].textContent, "你是测试助手，回答先给结论。");
    assert.equal(harness.elements["persona-dialog-description"].hidden, false);
    assert.equal(harness.elements["persona-dialog-description-empty"].hidden, true);
});

test("submitting the onboarding description writes it and clears the red dots immediately", async () => {
    const harness = buildHarness({
        status: {
            should_run: true,
            completed: false,
            persona_step_done: false,
            has_models: false,
            has_persona: false,
            has_persona_input: false,
            description: "",
            photos: [],
            photo_album: [],
        },
    });
    harness.documentListeners.DOMContentLoaded();
    await flush();
    assert.equal(harness.elements["persona-nav-dot"].hidden, false);
    assert.equal(harness.elements["persona-button-dot"].hidden, false);

    harness.elements["persona-onboarding-description"].value = "你是测试助手。";
    harness.elements["persona-onboarding-next-btn"].click();
    await flush();
    await flush();

    assert.deepEqual(harness.calls.savedDescription, ["你是测试助手。"]);
    assert.equal(harness.elements["persona-nav-dot"].hidden, true);
    assert.equal(harness.elements["persona-button-dot"].hidden, true);
    await harness.context.G3kuOnboarding.openPersonaDialog();
    assert.equal(harness.elements["persona-dialog-description"].hidden, false);
    assert.equal(harness.elements["persona-dialog-description"].textContent, "你是测试助手。");
});

test("the onboarding drag hint waits for all four required chains", async () => {
    const harness = buildHarness({
        status: {
            should_run: true,
            completed: false,
            persona_step_done: true,
            has_models: true,
            has_persona: false,
            has_persona_input: true,
            description: "",
            photos: ["me.png"],
            photo_album: [{ name: "me.png", url: "u", thumb_url: "t" }],
        },
    });
    harness.documentListeners.DOMContentLoaded();
    await flush();
    await flush();

    const dragToast = harness.calls.toasts.find((item) => item.title === "可拖动至 Agent");
    assert.ok(dragToast, JSON.stringify(harness.calls.toasts));
    assert.equal(dragToast.persistent, true);
    assert.equal(dragToast.closable, false);
    assert.ok(String(dragToast.text || "").includes("记忆Agent"));
    assert.ok(harness.intervals.length >= 1);

    harness.context.S.modelCatalog.roleEditing = true;
    harness.context.S.modelCatalog.roleDrafts = {
        ceo: ["m1"],
        execution: ["m1"],
        inspection: ["m1"],
        memory: [],
    };
    harness.intervals[0].handler();
    assert.equal(harness.calls.toastClosed, 0, "空记忆链不许关掉拖拽提示");

    harness.context.S.modelCatalog.roleDrafts.memory = ["m2"];
    harness.intervals[0].handler();
    assert.equal(harness.calls.toastClosed, 1);
});

test("Escape 先收压在弹窗之上的预览层，再收弹窗自己", async () => {
    const harness = buildHarness({
        status: {
            should_run: false,
            completed: true,
            persona_step_done: true,
            has_models: true,
            has_persona: true,
            has_persona_input: true,
            description: "测试人设",
            photos: [],
            photo_album: [],
        },
        persona: { description: "测试人设", photos: [{ name: "a.png", url: "/media/a.png" }] },
    });
    harness.documentListeners.DOMContentLoaded();
    await harness.context.G3kuOnboarding.openPersonaDialog();
    assert.equal(harness.elements["persona-dialog-backdrop"].hidden, false);

    const pressEscape = () => {
        const event = { key: "Escape" };
        let stopped = false;
        event.preventDefault = () => {};
        event.stopPropagation = () => {
            stopped = true;
        };
        harness.documentListeners.keydown(event);
        return stopped;
    };

    // 预览层是这一串里最上面那层（层位归 org_graph.css 的媒体查看器那一档）：
    // Escape 必须先收它，否则关掉的是被图片窗盖住、看不见的人设弹窗。
    harness.context.openMediaViewer({ mode: "image", url: "/media/a.png", title: "a.png" });
    assert.equal(pressEscape(), true, "预览层收掉后不许把同一次 Escape 再交给下层");
    assert.equal(harness.calls.viewerOpen, false);
    assert.equal(harness.elements["persona-dialog-backdrop"].hidden, false, "人设弹窗仍要在");

    // 预览层没了，才轮到弹窗自己。
    assert.equal(pressEscape(), true);
    assert.equal(harness.elements["persona-dialog-backdrop"].hidden, true);
});

test("persona settings dialog initializes an empty description and keeps photos chat-only", () => {
    assert.ok(HTML.includes('id="project-settings-open-persona-btn"'));
    assert.ok(HTML.includes('id="persona-nav-dot"'));
    assert.ok(HTML.includes('id="persona-button-dot"'));
    assert.ok(HTML.includes('id="persona-onboarding-backdrop"'));
    const onboardingStart = HTML.indexOf('id="persona-onboarding-backdrop"');
    const onboardingEnd = HTML.indexOf('id="persona-dialog-backdrop"');
    assert.ok(onboardingStart > 0 && onboardingEnd > onboardingStart);
    const onboardingDialog = HTML.slice(onboardingStart, onboardingEnd);
    assert.ok(!onboardingDialog.includes("persona-onboarding-close-btn"));
    assert.ok(onboardingDialog.includes("persona-onboarding-skip-btn"));
    assert.ok(onboardingDialog.includes("persona-onboarding-next-btn"));
    const start = HTML.indexOf('id="persona-dialog-backdrop"');
    const end = HTML.indexOf('id="password-change-backdrop"');
    assert.ok(start > 0 && end > start);
    const personaDialog = HTML.slice(start, end);
    assert.ok(!/<input\b/i.test(personaDialog), "persona dialog must not contain inputs");
    assert.ok(!personaDialog.includes("type=\"file\""));
    assert.ok(!personaDialog.includes("上传"));
    assert.ok(!personaDialog.includes("删除"));
    assert.ok(personaDialog.includes("persona-dialog-description-empty"));
    assert.ok(personaDialog.includes("persona-dialog-description-input"));
    assert.ok(personaDialog.includes("persona-dialog-description-save"));
    assert.ok(personaDialog.includes("persona-dialog-description"));
    assert.ok(personaDialog.includes("persona-dialog-photo-grid"));
});

test("frontend scripts keep the page order", () => {
    const personaApi = HTML.indexOf('src="client_api/persona.js"');
    const apiClient = HTML.indexOf('src="api_client.js"');
    const llm = HTML.indexOf('src="org_graph_llm.js"');
    const onboarding = HTML.indexOf('src="org_graph_onboarding.js"');
    assert.ok(personaApi > 0 && personaApi < apiClient);
    assert.ok(llm > 0 && onboarding > llm);
});

test("empty persona photos span the whole grid instead of one narrow column", () => {
    assert.match(
        CSS,
        /\.persona-photo-grid > \.persona-photo-empty \{[^}]*grid-column:\s*1\s*\/\s*-1;/,
    );
});
