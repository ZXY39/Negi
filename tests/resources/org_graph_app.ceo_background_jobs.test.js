const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const MODULE_PATH = path.join(__dirname, "../../g3ku/web/frontend/ceo/background-jobs.js");
const MODULE_CODE = fs.readFileSync(MODULE_PATH, "utf8").replace(/\r\n/g, "\n");

function datasetKey(name) {
    return name
        .slice(5)
        .replace(/-([a-z])/g, (_, ch) => ch.toUpperCase());
}

class StubElement {
    constructor(tag = "DIV") {
        this.tagName = String(tag || "DIV").toUpperCase();
        this.children = [];
        this.dataset = {};
        this.hidden = false;
        this.disabled = false;
        this.textContent = "";
        this.className = "";
        this.title = "";
        this.attributes = {};
        this.listeners = {};
        this.parentElement = null;
    }

    get firstChild() {
        return this.children[0] || null;
    }

    appendChild(child) {
        child.parentElement = this;
        this.children.push(child);
        return child;
    }

    removeChild(child) {
        this.children = this.children.filter((item) => item !== child);
        child.parentElement = null;
        return child;
    }

    setAttribute(name, value) {
        this.attributes[name] = String(value);
        if (name.startsWith("data-")) this.dataset[datasetKey(name)] = String(value);
    }

    getAttribute(name) {
        return Object.prototype.hasOwnProperty.call(this.attributes, name) ? this.attributes[name] : null;
    }

    addEventListener(type, handler) {
        (this.listeners[type] = this.listeners[type] || []).push(handler);
    }

    querySelectorAll(selector) {
        if (selector !== "[data-bg-job-started]") return [];
        const out = [];
        const walk = (node) => {
            if (node.dataset && node.dataset.bgJobStarted !== undefined) out.push(node);
            (node.children || []).forEach(walk);
        };
        this.children.forEach(walk);
        return out;
    }

    closest() {
        return null;
    }
}

class StubDocument {
    constructor() {
        this._byId = new Map();
        ["ceo-bg-jobs", "ceo-bg-jobs-toggle", "ceo-bg-jobs-count", "ceo-bg-jobs-popover", "ceo-bg-jobs-list"].forEach((id) => {
            const el = new StubElement("DIV");
            el.id = id;
            this._byId.set(id, el);
        });
    }

    getElementById(id) {
        return this._byId.get(id) || null;
    }

    createElement(tag) {
        return new StubElement(tag);
    }
}

function loadModule(overrides = {}) {
    const calls = [];
    const toasts = [];
    const intervals = new Map();
    let nextIntervalId = 1;
    let active = "web:a";
    const document = new StubDocument();
    const ApiClient = {
        async fetchCeoToolJobs(sessionId) {
            calls.push(["fetch", sessionId]);
            return overrides.fetchCeoToolJobs
                ? overrides.fetchCeoToolJobs(sessionId)
                : { ok: true, items: [] };
        },
        async stopCeoToolJob(sessionId, jobId) {
            calls.push(["stop", sessionId, jobId]);
            if (overrides.stopCeoToolJob) return overrides.stopCeoToolJob(sessionId, jobId);
            return { ok: true, job_id: jobId, status: "stopping" };
        },
        getErrorCode(error) {
            return String(error?.code || "");
        },
    };
    const context = {
        console,
        Date,
        Number,
        String,
        Array,
        Set,
        Math,
        JSON,
        document,
        Element: StubElement,
        ApiClient,
        activeSessionId: () => active,
        showToast: (options) => toasts.push(options),
        window: {
            setInterval(handler) {
                const id = nextIntervalId++;
                intervals.set(id, handler);
                return id;
            },
            clearInterval(id) {
                intervals.delete(id);
            },
            addEventListener() {},
        },
        setActiveSession: (value) => {
            active = String(value || "");
        },
        flushIntervals: () => intervals.forEach((handler) => handler()),
    };
    context.window.window = context.window;
    vm.createContext(context);
    vm.runInContext(MODULE_CODE, context);
    return { context, calls, toasts, document };
}

function itemsFixture() {
    return [
        {
            job_id: "tooljob:1",
            tool_name: "exec",
            status: "running",
            started_at: new Date(Date.now() - 65_000).toISOString(),
            elapsed_seconds: 65,
            hint: "promo_send5.py",
        },
        {
            job_id: "tooljob:2",
            tool_name: "browser",
            status: "stopping",
            started_at: new Date(Date.now() - 5_000).toISOString(),
            elapsed_seconds: 5,
            hint: "",
        },
    ];
}

test("0 条隐藏，2 条显示计数与列表", () => {
    const { context, document } = loadModule();
    const root = document.getElementById("ceo-bg-jobs");
    context.ceoBgJobsSetItems([], "web:a");
    assert.equal(root.hidden, true);

    context.ceoBgJobsSetItems(itemsFixture(), "web:a");
    assert.equal(root.hidden, false);
    assert.equal(document.getElementById("ceo-bg-jobs-count").textContent, "2");
    const list = document.getElementById("ceo-bg-jobs-list");
    assert.equal(list.children.length, 2);
    assert.equal(list.children[0].children[0].children[0].textContent, "exec");
    assert.match(list.children[0].children[1].textContent, /^01:0[5-9]$/);
    assert.equal(list.children[0].children[2].textContent, "停止");
    assert.equal(list.children[1].children[2].textContent, "停止中");
    assert.equal(list.children[1].children[2].disabled, true);

    context.ceoBgJobsSetOpen(true);
    assert.equal(document.getElementById("ceo-bg-jobs-popover").hidden, false);
    assert.equal(document.getElementById("ceo-bg-jobs-toggle").getAttribute("aria-expanded"), "true");
    context.ceoBgJobsClose();
    assert.equal(document.getElementById("ceo-bg-jobs-popover").hidden, true);
});

test("计时器只改文本，不重建列表", () => {
    const { context, document } = loadModule();
    context.ceoBgJobsSetItems(itemsFixture(), "web:a");
    context.ceoBgJobsSetOpen(true);
    const list = document.getElementById("ceo-bg-jobs-list");
    const firstRow = list.children[0];
    context.flushIntervals();
    assert.equal(list.children[0], firstRow);
    assert.match(list.children[0].children[1].textContent, /^01:\d{2}$/);
});

test("停止调用正确 API，成功后移除行并递减计数", async () => {
    const { context, document, calls } = loadModule();
    context.ceoBgJobsSetItems(itemsFixture(), "web:a");
    await context.ceoBgJobsStop("tooljob:1");
    assert.deepEqual(calls.find((item) => item[0] === "stop"), ["stop", "web:a", "tooljob:1"]);
    assert.equal(document.getElementById("ceo-bg-jobs-count").textContent, "1");
    assert.equal(document.getElementById("ceo-bg-jobs-list").children.length, 1);
    assert.equal(document.getElementById("ceo-bg-jobs-list").children[0].dataset.bgJobId, "tooljob:2");
    // 服务端停止收尾前可能再推一帧：已成功停止的行不得被重新加回
    context.handleCeoToolJobsFrame({ data: { items: itemsFixture() } }, "web:a");
    assert.equal(document.getElementById("ceo-bg-jobs-count").textContent, "1");
    assert.equal(document.getElementById("ceo-bg-jobs-list").children.length, 1);
});

test("409 刷新列表并 toast 工具已结束", async () => {
    const error = Object.assign(new Error("finished"), { status: 409, code: "tool_job_finished" });
    const { context, document, calls, toasts } = loadModule({
        stopCeoToolJob: async () => {
            throw error;
        },
    });
    context.ceoBgJobsSetItems(itemsFixture(), "web:a");
    await context.ceoBgJobsStop("tooljob:1");
    assert.deepEqual(calls.find((item) => item[0] === "fetch"), ["fetch", "web:a"]);
    assert.equal(toasts.at(-1).title, "工具已结束");
    assert.equal(document.getElementById("ceo-bg-jobs-count").textContent, "0");
});

test("WS 帧只对当前会话生效", () => {
    const { context, document } = loadModule();
    context.ceoBgJobsSetItems([], "web:a");
    context.handleCeoToolJobsFrame({ data: { items: itemsFixture() } }, "web:b");
    assert.equal(document.getElementById("ceo-bg-jobs-count").textContent, "0");
    context.handleCeoToolJobsFrame({ data: { items: itemsFixture() } }, "web:a");
    assert.equal(document.getElementById("ceo-bg-jobs-count").textContent, "2");
});

test("切换会话会清掉旧会话的条目", async () => {
    const { context, document } = loadModule();
    context.ceoBgJobsSetItems(itemsFixture(), "web:a");
    context.setActiveSession("web:b");
    await context.refreshCeoBackgroundJobs("web:b");
    assert.equal(document.getElementById("ceo-bg-jobs").hidden, true);
    assert.equal(document.getElementById("ceo-bg-jobs-count").textContent, "0");
});
