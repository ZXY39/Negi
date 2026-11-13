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
        this.style = {};
        this.scrollTop = 0;
        this.rect = { top: 80, bottom: 112, left: 800, right: 1240, width: 440, height: 32 };
    }

    getBoundingClientRect() {
        return this.rect;
    }

    get firstChild() {
        return this.children[0] || null;
    }

    appendChild(child) {
        child.parentElement?.removeChild(child);
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

    querySelector(selector) {
        return selector === ".tool-status-icon" ? this.iconWrap || null : null;
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
        this.body = new StubElement("body");
        this._chips = [];
        this._liveRows = [];
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

    querySelectorAll(selector) {
        if (String(selector).includes(".task-trace-round-chip")) return this._chips.slice();
        if (String(selector).includes(".task-trace-live-steps")) return this._liveRows.slice();
        return [];
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
            innerWidth: 1440,
            innerHeight: 900,
            getComputedStyle: (el) => ({ zoom: el.style.zoom || "1", paddingBottom: "8px", borderBottomWidth: "1px" }),
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
        intervalCount: () => intervals.size,
        flushIntervals: () => intervals.forEach((handler) => handler()),
    };
    context.window.window = context.window;
    vm.createContext(context);
    vm.runInContext(fs.readFileSync(path.join(__dirname, "../../g3ku/web/frontend/ui/drawer.js"), "utf8"), context);
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

test("WS process purpose and tool hint keep their full text", () => {
    const { context, document } = loadModule();
    const purpose = "render preview and keep the server available for review; ".repeat(3);
    const hint = "node scripts/preview-with-a-very-long-command-name.js ".repeat(3);
    context.handleCeoToolJobsFrame({ data: { items: [
        { ...itemsFixture()[0], kind: "process", job_id: "proc:42", purpose, hint },
        { ...itemsFixture()[1], hint },
    ] } }, "web:a");
    const rows = document.getElementById("ceo-bg-jobs-list").children;
    assert.equal(rows[0].children[0].children[0].children[0].textContent, purpose.trim());
    assert.equal(rows[1].children[0].children[1].textContent, hint.trim());
});

test("open list portals out of the chat wrapper and preserves scroll on refresh", () => {
    const { context, document } = loadModule();
    const items = Array.from({ length: 21 }, (_, i) => ({ ...itemsFixture()[0], job_id: `proc:${i}` }));
    context.ceoBgJobsSetItems(items, "web:a");
    context.ceoBgJobsSetOpen(true);
    const popover = document.getElementById("ceo-bg-jobs-popover");
    const list = document.getElementById("ceo-bg-jobs-list");
    assert.equal(popover.parentElement, document.body);
    assert.equal(list.children.length, 21);
    list.scrollTop = 128;
    context.ceoBgJobsSetItems(items, "web:a");
    assert.equal(list.scrollTop, 128);
    assert.equal(popover.hidden, false);
});

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
    assert.equal(list.children[0].children[0].children[0].children[0].textContent, "exec");
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

test("exec background 进程与工具作业同列一行：来源优先 purpose", () => {
    const { context, document } = loadModule();
    const jobHint = "exec (command=$ErrorActionPreference='Continue'\\n& 'C:\\Users\\A...', purpose=rebuild gate, timeout_seconds=900)";
    context.ceoBgJobsSetItems([
        {
            kind: "process",
            job_id: "proc:4242",
            pid: 4242,
            tool_name: "exec",
            status: "running",
            started_at: new Date(Date.now() - 3_000).toISOString(),
            elapsed_seconds: 3,
            hint: "node server.js",
            purpose: "dev server :3000",
            log_path: "owned-processes/x.log",
        },
        {
            kind: "tool_job",
            job_id: "tooljob:7",
            tool_name: "exec",
            status: "running",
            started_at: new Date(Date.now() - 8_000).toISOString(),
            elapsed_seconds: 8,
            hint: jobHint,
            purpose: "rebuild and re-run check gate",
        },
    ], "web:a");
    const list = document.getElementById("ceo-bg-jobs-list");
    assert.equal(document.getElementById("ceo-bg-jobs-count").textContent, "2");

    const processRow = list.children[0];
    assert.equal(processRow.dataset.bgJobId, "proc:4242");
    assert.equal(processRow.dataset.bgJobKind, "process");
    const processLabel = processRow.children[0].children[0];
    assert.equal(processLabel.children[0].textContent, "dev server :3000");
    assert.doesNotMatch(processLabel.children[0].className, /ceo-bg-jobs-command/);
    // 有 purpose 时命令行不再占第二行，只留在 title 里。
    assert.equal(processRow.children[0].children.length, 1);
    assert.equal(processRow.children[0].title, "node server.js");
    // 行内不再有「常驻进程」视觉标记。
    assert.doesNotMatch(processRow.textContent, /常驻进程/);

    // 工具作业行优先显示 purpose（交棒时从入参提取），调用摘要降为下方小字。
    const jobRow = list.children[1];
    assert.equal(jobRow.dataset.bgJobKind, "tool_job");
    const jobMain = jobRow.children[0];
    const jobLabel = jobMain.children[0];
    assert.equal(jobLabel.children.length, 1);
    assert.equal(jobLabel.children[0].textContent, "rebuild and re-run check gate");
    assert.equal(jobMain.children.length, 2);
    assert.equal(jobMain.children[1].className, "ceo-bg-jobs-hint");
    assert.equal(jobMain.children[1].textContent, jobHint);
});

test("没有 purpose 的工具作业行退回工具名，调用摘要当来源", () => {
    const { context, document } = loadModule();
    context.ceoBgJobsSetItems([
        {
            kind: "tool_job",
            job_id: "tooljob:8",
            tool_name: "exec",
            status: "running",
            started_at: new Date(Date.now() - 5_000).toISOString(),
            elapsed_seconds: 5,
            hint: "python probe.py",
        },
    ], "web:a");
    const jobMain = document.getElementById("ceo-bg-jobs-list").children[0].children[0];
    assert.equal(jobMain.children[0].children[0].textContent, "exec");
    assert.equal(jobMain.children[1].textContent, "python probe.py");
});

test("没有 purpose 的 legacy 进程行把命令前两行当来源", () => {
    const { context, document } = loadModule();
    const command = "$env:PYTHONUTF8=\"1\"\n$w=\"C:/temp/watcher\"\n& python watcher.py --loop";
    context.ceoBgJobsSetItems([
        {
            kind: "process",
            job_id: "proc:4243",
            pid: 4243,
            tool_name: "exec",
            status: "running",
            started_at: new Date(Date.now() - 3_000).toISOString(),
            elapsed_seconds: 3,
            hint: command,
        },
    ], "web:a");
    const row = document.getElementById("ceo-bg-jobs-list").children[0];
    const main = row.children[0];
    const label = main.children[0];
    assert.equal(label.children[0].textContent, command);
    assert.match(label.children[0].className, /ceo-bg-jobs-command/);
    // 命令已经作为来源行显示（CSS 两行封顶），不再另起一行；完整命令留在 title。
    assert.equal(main.children.length, 1);
    assert.equal(main.title, command);
    assert.equal(row.children[1].className, "ceo-bg-jobs-elapsed");
    assert.equal(row.children[2].className, "ceo-bg-jobs-stop");
});

test("purpose 与命令都缺席时进程行仍写 exec 进程", () => {
    const { context, document } = loadModule();
    context.ceoBgJobsSetItems([
        {
            kind: "process",
            job_id: "proc:4244",
            pid: 4244,
            tool_name: "exec",
            status: "running",
            started_at: new Date(Date.now() - 3_000).toISOString(),
            elapsed_seconds: 3,
            hint: "",
        },
    ], "web:a");
    const label = document.getElementById("ceo-bg-jobs-list").children[0].children[0].children[0];
    assert.equal(label.children[0].textContent, "exec 进程");
    assert.doesNotMatch(label.children[0].className, /ceo-bg-jobs-command/);
});

test("气泡非空时启动对账轮询，清空后停止", () => {
    const { context } = loadModule();
    assert.equal(context.intervalCount(), 0);
    context.ceoBgJobsSetItems(itemsFixture(), "web:a");
    assert.equal(context.intervalCount(), 1);
    context.ceoBgJobsClear();
    assert.equal(context.intervalCount(), 0);
});

test("终态帧把账本里的后台工具行改画成完成，原账本对象不变", () => {
    const { context } = loadModule();
    context.handleCeoToolJobsFrame({
        data: {
            items: [],
            finished: {
                job_id: "tooljob:1",
                tool_name: "exec",
                status: "success",
                tool_call_id: "call-1",
                finished_at: new Date().toISOString(),
                elapsed_seconds: 12,
            },
        },
    }, "web:a");
    const turns = [{
        seq: 1,
        tools: [
            { tool_call_id: "call-1|fc-1", tool_name: "exec", status: "background" },
            { tool_call_id: "call-2", tool_name: "exec", status: "background" },
        ],
    }];
    const decorated = context.ceoBgJobsDecorateTurns(turns);
    assert.equal(decorated[0].tools[0].status, "success");
    assert.equal(decorated[0].tools[0].background_finished, true);
    assert.equal(decorated[0].tools[1].status, "background");
    assert.equal(turns[0].tools[0].status, "background");
});

test("终态帧把实时行的转圈时钟换成完成图标", () => {
    const { context, document } = loadModule();
    const chip = document.createElement("button");
    chip.dataset.toolCallId = "call-1|fc-1";
    const chipIcon = document.createElement("span");
    chipIcon.className = "tool-status-icon is-running";
    chip.iconWrap = chipIcon;
    document._chips.push(chip);

    const liveRow = document.createElement("div");
    liveRow.dataset.toolCallId = "call-1";
    liveRow.dataset.stepState = "background";
    liveRow.dataset.toolName = "exec";
    document._liveRows.push(liveRow);
    context.applyCeoToolStepState = (item, options) => {
        item.dataset.stepState = options.status;
        item.dataset.appliedTool = options.toolName;
    };

    context.handleCeoToolJobsFrame({
        data: {
            items: [],
            finished: {
                job_id: "tooljob:1",
                tool_name: "exec",
                status: "success",
                tool_call_id: "call-1",
                finished_at: new Date().toISOString(),
            },
        },
    }, "web:a");

    assert.equal(chipIcon.className, "tool-status-icon is-success");
    assert.match(chipIcon.innerHTML, /lucide="check"/);
    assert.equal(liveRow.dataset.stepState, "success");
    assert.equal(liveRow.dataset.appliedTool, "exec");
});

test("刷新回放的终态按失败/中断映射成 error", async () => {
    const { context } = loadModule({
        fetchCeoToolJobs: async () => ({
            items: [],
            finished: [{
                job_id: "tooljob:2",
                tool_name: "browser",
                status: "interrupted",
                tool_call_id: "call-9",
                finished_at: new Date().toISOString(),
            }],
        }),
    });
    await context.refreshCeoBackgroundJobs("web:a");
    const decorated = context.ceoBgJobsDecorateTurns([{
        seq: 1,
        tools: [{ tool_call_id: "call-9", tool_name: "browser", status: "background" }],
    }]);
    assert.equal(decorated[0].tools[0].status, "error");
});
