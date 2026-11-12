const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const MODULE_PATH = path.join(__dirname, "../../g3ku/web/frontend/tasks/task-processes.js");
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

    contains(target) {
        return this === target || this.children.some((child) => child.contains(target));
    }

    querySelectorAll() {
        return [];
    }
}

class StubDocument {
    constructor() {
        this._byId = new Map();
        this.body = new StubElement("body");
        this.listeners = {};
        [
            "task-processes",
            "task-processes-btn",
            "task-processes-count",
            "task-processes-popover",
            "task-processes-list",
        ].forEach((id) => {
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

    addEventListener(type, handler) {
        (this.listeners[type] = this.listeners[type] || []).push(handler);
    }
}

function itemsFixture() {
    return [
        {
            pid: 123,
            purpose: "dev server :3000",
            cmd: "node server.js",
            started_at: new Date(Date.now() - 65_000).toISOString(),
            log_path: "C:/log/123.log",
            node_id: "node:a",
            node_goal: "渲染 ep03",
            node_status: "in_progress",
        },
        {
            pid: 124,
            purpose: "",
            cmd: "node legacy.js",
            started_at: new Date(Date.now() - 5_000).toISOString(),
            log_path: "C:/log/124.log",
            node_id: "",
            node_goal: "",
            node_status: "",
        },
    ];
}

function loadModule(overrides = {}) {
    const calls = [];
    const toasts = [];
    const jumps = [];
    const intervals = new Map();
    let nextIntervalId = 1;
    const document = new StubDocument();
    const ApiClient = {
        async getTaskProcesses(taskId) {
            calls.push(["list", taskId]);
            if (overrides.getError) throw overrides.getError;
            return { ok: true, items: overrides.items || [] };
        },
        async getTaskToolJobs(taskId) {
            calls.push(["tool-jobs", taskId]);
            if (overrides.toolJobsError) throw overrides.toolJobsError;
            return { ok: true, items: overrides.toolJobItems || [] };
        },
        async stopTaskProcess(taskId, pid) {
            calls.push(["stop", taskId, pid]);
            if (overrides.stopError) throw overrides.stopError;
            return { ok: true, pid, status: "terminated" };
        },
        async stopTaskToolJob(taskId, jobId) {
            calls.push(["stop-tool-job", taskId, jobId]);
            if (overrides.toolStopError) throw overrides.toolStopError;
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
        ApiClient,
        S: { currentTaskId: "task:abc" },
        isTaskDetailsViewActive: () => overrides.active !== false,
        jumpTaskTreeSearchToNode: async (nodeId) => {
            jumps.push(String(nodeId || ""));
        },
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
    };
    context.window.window = context.window;
    vm.createContext(context);
    vm.runInContext(fs.readFileSync(path.join(__dirname, "../../g3ku/web/frontend/ui/drawer.js"), "utf8"), context);
    vm.runInContext(MODULE_CODE, context);
    return { context, document, calls, toasts, jumps, interruptCount: () => intervals.size };
}

test("REST process purposes and legacy commands keep their full text", async () => {
    const items = itemsFixture();
    items[0].purpose = "render the complete preview for the next review session; ".repeat(3);
    items[1].cmd = "node scripts/preview-with-a-very-long-command-name.js ".repeat(3);
    const { context, document } = loadModule({ items });
    await context.taskProcessesRefresh();
    const rows = document.getElementById("task-processes-list").children;
    // 行结构：[0]=kind 标签，[1]=主列，[2]=耗时，[3]=停止，[4]=节点按钮。
    // 合并视图按 started_at 新→旧排：fixture 里 pid 124（5s 前）在前。
    assert.equal(rows[0].children[0].textContent, "进程");
    assert.equal(rows[0].children[1].textContent, items[1].cmd.trim());
    assert.match(rows[0].children[1].className, /task-processes-main--command/);
    assert.equal(rows[1].children[1].textContent, items[0].purpose.trim());
    assert.doesNotMatch(rows[1].children[1].className, /task-processes-main--command/);
});

test("行版式：来源、时长、停止在第一行，定位节点按钮在停止右侧", async () => {
    const { context, document } = loadModule({ items: itemsFixture() });
    await context.taskProcessesRefresh();
    const row = document.getElementById("task-processes-list").children[1];
    assert.equal(row.children[0].className, "task-processes-kind");
    assert.equal(row.children[1].className, "task-processes-main");
    assert.equal(row.children[2].className, "task-processes-elapsed");
    assert.equal(row.children[3].className, "task-processes-stop");
    assert.equal(row.children[4].className, "task-processes-node");
    assert.equal(row.children[4].dataset.taskProcessNode, "node:a");
});

test("button opens a body-mounted panel; inside clicks stay open and outside clicks close", async () => {
    const { context, document } = loadModule({ items: itemsFixture() });
    await context.taskProcessesRefresh();
    context.bindTaskProcesses();
    const toggle = document.getElementById("task-processes-btn");
    toggle.listeners.click[0]({ stopPropagation() {} });
    const popover = document.getElementById("task-processes-popover");
    assert.equal(popover.hidden, false);
    assert.equal(popover.parentElement, document.body);
    assert.equal(toggle.getAttribute("aria-expanded"), "true");
    for (const handler of document.listeners.click) handler({ target: popover });
    assert.equal(popover.hidden, false);
    for (const handler of document.listeners.click) handler({ target: document.body });
    assert.equal(popover.hidden, true);
});

test("list height stops at row 20 using measured variable-height rows and viewport space", async () => {
    const items = Array.from({ length: 21 }, (_, i) => ({ ...itemsFixture()[0], pid: i + 1 }));
    const { context, document } = loadModule({ items });
    await context.taskProcessesRefresh();
    const list = document.getElementById("task-processes-list");
    let top = 150;
    list.rect.top = top;
    list.children.forEach((row, i) => {
        const height = i % 2 ? 40 : 20;
        row.rect = { top, bottom: top + height, height };
        top += height + 2;
    });
    context.taskProcessesSetOpen(true);
    assert.equal(list.children.length, 21);
    assert.equal(list.style.maxHeight, "638px");
    context.window.innerHeight = 500;
    context.taskProcessesSetOpen(true);
    assert.equal(list.style.maxHeight, "329px");
});

test("portal coordinates account for app zoom and refresh retains the scroll position", async () => {
    const { context, document } = loadModule({ items: itemsFixture() });
    await context.taskProcessesRefresh();
    document.body.style.zoom = "1.25";
    context.taskProcessesSetOpen(true);
    const popover = document.getElementById("task-processes-popover");
    const list = document.getElementById("task-processes-list");
    assert.equal(popover.style.left, "640px");
    assert.equal(popover.style.top, "95.6px");
    list.scrollTop = 64;
    await context.taskProcessesRefresh();
    assert.equal(list.scrollTop, 64);
    assert.equal(popover.hidden, false);
});

test("空列表隐藏气泡，有进程时显示计数与 purpose 行", async () => {
    const { context, document } = loadModule({ items: [] });
    await context.taskProcessesRefresh();
    assert.equal(document.getElementById("task-processes").hidden, true);
    assert.equal(document.getElementById("task-processes-btn").hidden, true);

    const items = itemsFixture();
    const second = loadModule({ items });
    await second.context.taskProcessesRefresh();
    assert.equal(second.document.getElementById("task-processes").hidden, false);
    assert.equal(second.document.getElementById("task-processes-btn").hidden, false);
    assert.equal(second.document.getElementById("task-processes-count").textContent, "2");
    const rows = second.document.getElementById("task-processes-list").children;
    assert.equal(rows.length, 2);
    // 合并视图新→旧：pid 124（5s 前）在前，pid 123（65s 前）在后。
    assert.equal(rows[0].children[1].textContent, "node legacy.js");
    assert.equal(rows[0].children[2].dataset.taskProcessStarted, items[1].started_at);
    assert.equal(rows[0].children[3].textContent, "停止");
    assert.equal(rows[0].children[4].disabled, true);
    assert.equal(rows[0].children[4].textContent, "未归属");
    assert.equal(rows[1].children[1].textContent, "dev server :3000");
    assert.equal(rows[1].children[1].title, "dev server :3000\nnode server.js");
    assert.equal(rows[1].children[2].dataset.taskProcessStarted, items[0].started_at);
    assert.equal(rows[1].children[3].className, "task-processes-stop");
    assert.equal(rows[1].children[4].textContent, "node:a");
    assert.equal(rows[1].children[4].dataset.taskProcessNode, "node:a");
});

test("节点后台工具作业与进程同列显示，kind 标签区分且行不编造 PID", async () => {
    const toolJobItems = [
        {
            kind: "tool_job",
            job_id: "tooljob:abc123",
            tool_name: "filesystem_read",
            status: "running",
            started_at: new Date(Date.now() - 30_000).toISOString(),
            hint: "filesystem_read (path=docs/report.md)",
            node_id: "node:a",
        },
    ];
    const { context, document } = loadModule({ items: itemsFixture(), toolJobItems });
    await context.taskProcessesRefresh();
    assert.equal(document.getElementById("task-processes-count").textContent, "3");
    const rows = document.getElementById("task-processes-list").children;
    assert.equal(rows.length, 3);
    const toolRow = rows.find((row) => row.dataset.taskProcessKey === "tool_job:tooljob:abc123");
    assert.ok(toolRow);
    assert.equal(toolRow.children[0].textContent, "工具");
    assert.equal(toolRow.children[1].textContent, "filesystem_read (path=docs/report.md)");
    assert.equal(toolRow.children[2].className, "task-processes-elapsed");
    assert.equal(toolRow.children[3].className, "task-processes-stop");
    assert.equal(toolRow.children[4].textContent, "node:a");
    assert.equal(toolRow.dataset.taskProcessPid, undefined);
    const processRow = rows.find((row) => row.dataset.taskProcessKey === "process:123");
    assert.equal(processRow.children[0].textContent, "进程");
});

test("工具作业停止走 tool-jobs API，409 提示工具已结束", async () => {
    const toolJobItems = [
        {
            kind: "tool_job",
            job_id: "tooljob:abc123",
            tool_name: "exec",
            status: "running",
            started_at: new Date(Date.now() - 10_000).toISOString(),
            hint: "exec (command=poll)",
            node_id: "node:a",
        },
    ];
    const error = new Error("finished");
    error.status = 409;
    error.code = "tool_job_finished";
    const { context, toasts, calls } = loadModule({ items: [], toolJobItems, toolStopError: error });
    await context.taskProcessesRefresh();
    calls.length = 0;

    await context.taskProcessesStop("tool_job:tooljob:abc123");

    assert.deepEqual(calls[0], ["stop-tool-job", "task:abc", "tooljob:abc123"]);
    assert.equal(toasts[0].title, "工具已结束");
});

test("点开气泡后行内容可见，点节点 id 关闭气泡并走搜索定位入口", async () => {
    const { context, document, jumps } = loadModule({ items: itemsFixture() });
    await context.taskProcessesRefresh();
    context.taskProcessesSetOpen(true);
    assert.equal(document.getElementById("task-processes-popover").hidden, false);

    context.taskProcessesJumpToNode("node:a");
    assert.deepEqual(jumps, ["node:a"]);
    assert.equal(document.getElementById("task-processes-popover").hidden, true);
});

test("停止调用任务进程 API，成功后 toast 并刷新", async () => {
    const { context, toasts, calls } = loadModule({ items: itemsFixture() });
    await context.taskProcessesRefresh();
    calls.length = 0;

    await context.taskProcessesStop(123);

    assert.deepEqual(calls[0], ["stop", "task:abc", 123]);
    assert.equal(calls[1][0], "list");
    assert.equal(toasts[0].title, "已停止进程");
});

test("停止遇到 404 时提示进程已结束，不当作普通失败", async () => {
    const error = new Error("not found");
    error.status = 404;
    error.code = "process_not_found";
    const { context, toasts } = loadModule({ items: itemsFixture(), stopError: error });
    await context.taskProcessesRefresh();

    await context.taskProcessesStop(123);

    assert.equal(toasts[0].title, "进程已结束");
});

test("任务详情不活跃时不发请求并清空气泡", async () => {
    const { context, document, calls } = loadModule({ items: itemsFixture(), active: false });
    await context.taskProcessesRefresh();
    assert.equal(calls.length, 0);
    assert.equal(document.getElementById("task-processes").hidden, true);
});

test("绑定后启动 10 秒对账节拍，且只有详情视图活跃时才拉取", () => {
    const { context, interruptCount } = loadModule({ items: itemsFixture() });
    context.bindTaskProcesses();
    assert.ok(interruptCount() >= 1);
});
