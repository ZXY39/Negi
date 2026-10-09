const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

// 节点详情「工具刷新不折掉已展开内容」合同:
// 同一节点重绘时,阶段卡/初始提示词的 details 开合、以及每张卡里**按轮**选中的工具面板
// 都必须回到用户放下的位置——包括请求在飞窗口里才点开的那些。

const TASK_VIEW_PATH = "g3ku/web/frontend/org_graph_task_view.js";
const APP_PATH = "g3ku/web/frontend/org_graph_app.js";
const TASKS_PATH = "g3ku/web/frontend/org_graph_tasks.js";
const TASK_VIEW_CODE = fs.readFileSync(TASK_VIEW_PATH, "utf8");
const APP_CODE = fs.readFileSync(APP_PATH, "utf8");
const TASKS_CODE = fs.readFileSync(TASKS_PATH, "utf8");

function matches(el, selector) {
    const parts = String(selector || "").split(",").map((s) => s.trim()).filter(Boolean);
    return parts.some((part) => matchesOne(el, part));
}

function matchesOne(el, part) {
    if (part.includes("[")) return false;
    const wanted = part.split(".").filter(Boolean);
    const own = String(el.className || "").split(/\s+/).filter(Boolean);
    (el._classes || []).forEach((name) => own.push(name));
    if (el.tagName) own.push(el.tagName.toLowerCase());
    return wanted.every((name) => own.includes(name));
}

class StubElement {}

class StubHTMLElement extends StubElement {
    constructor(tagName = "DIV") {
        super();
        this.tagName = tagName;
        this.className = "";
        this.dataset = {};
        this.attributes = {};
        this.style = {};
        this.textContent = "";
        this.hidden = false;
        this.open = false;
        this.scrollTop = 0;
        this.scrollHeight = 0;
        this.clientHeight = 0;
        this.children = [];
        this.parentElement = null;
        this._html = "";
        this._classes = new Set();
        this.classList = {
            add: (...names) => names.forEach((n) => this._classes.add(n)),
            remove: (...names) => names.forEach((n) => this._classes.delete(n)),
            contains: (n) => this._classes.has(n),
            toggle: (n, on) => {
                const next = on === undefined ? !this._classes.has(n) : !!on;
                if (next) this._classes.add(n);
                else this._classes.delete(n);
                return next;
            },
        };
    }

    get innerHTML() {
        return this._html;
    }

    // 真实 DOM 里 innerHTML 重建会把旧节点全换成新节点:按渲染产物重建成带嵌套的桩树。
    set innerHTML(value) {
        this._html = String(value);
        this.children = parseStepsHtml(this._html).map((step) => {
            const node = buildStepEl(step);
            node.parentElement = this;
            return node;
        });
    }

    setAttribute(name, value) {
        this.attributes[name] = String(value);
    }

    getAttribute(name) {
        return Object.prototype.hasOwnProperty.call(this.attributes, name) ? this.attributes[name] : null;
    }

    querySelector(selector) {
        for (const child of this.children) {
            if (matches(child, selector)) return child;
            const nested = child.querySelector(selector);
            if (nested) return nested;
        }
        return null;
    }

    querySelectorAll(selector) {
        const out = [];
        const walk = (node) => {
            node.children.forEach((child) => {
                if (matches(child, selector)) out.push(child);
                walk(child);
            });
        };
        walk(this);
        return out;
    }

    closest(selector) {
        let node = this;
        while (node) {
            if (matches(node, selector)) return node;
            node = node.parentElement;
        }
        return null;
    }

    contains(node) {
        if (node === this) return true;
        return this.children.some((child) => child.contains(node));
    }

    addEventListener() {}

    appendChild(child) {
        child.parentElement = this;
        this.children.push(child);
        return child;
    }

    getBoundingClientRect() {
        return { top: 0, bottom: 0, height: 0, left: 0, right: 0, width: 0 };
    }
}

class StubHTMLButtonElement extends StubHTMLElement {}
class StubHTMLInputElement extends StubHTMLElement {}
class StubHTMLTextAreaElement extends StubHTMLElement {}
class StubHTMLSelectElement extends StubHTMLElement {}

function attr(chunk, name) {
    const match = new RegExp(`${name}="([^"]*)"`).exec(chunk);
    return match ? match[1] : "";
}

function buildStepEl(step) {
    const el = new StubHTMLElement("DETAILS");
    el.className = step.className;
    el.dataset.traceKey = step.traceKey;
    el.dataset.defaultOpen = step.defaultOpen;
    el.open = step.open;

    const summary = new StubHTMLElement("SUMMARY");
    summary.className = "task-trace-summary";
    const titleSpan = new StubHTMLElement("SPAN");
    titleSpan.className = "interaction-step-title";
    titleSpan.textContent = step.title;
    summary.appendChild(titleSpan);
    const side = new StubHTMLElement("SPAN");
    side.className = "interaction-step-side";
    const runtime = new StubHTMLElement("SPAN");
    runtime.className = "task-trace-runtime";
    side.appendChild(runtime);
    summary.appendChild(side);
    el.appendChild(summary);

    const body = new StubHTMLElement("DIV");
    body.className = "task-trace-body";
    step.rounds.forEach((round) => {
        const host = new StubHTMLElement("DIV");
        host.className = "task-trace-round-tools";
        host.dataset.roundKey = round.roundKey;
        host.dataset.activeToolKey = round.activeToolKey;
        round.chips.forEach((chip) => {
            const chipEl = new StubHTMLElement("BUTTON");
            chipEl.className = `task-trace-round-chip ${chip.className}`.trim();
            chipEl.dataset.toolKey = chip.toolKey;
            if (chip.active) chipEl._classes.add("is-active");
            const label = new StubHTMLElement("SPAN");
            label.className = "task-trace-round-chip-label";
            const title = new StubHTMLElement("SPAN");
            title.className = "task-trace-round-chip-title";
            title.textContent = chip.title;
            label.appendChild(title);
            chipEl.appendChild(label);
            host.appendChild(chipEl);
        });
        round.panels.forEach((panel) => {
            const panelEl = new StubHTMLElement("SECTION");
            panelEl.className = "task-trace-round-panel";
            panelEl.dataset.toolKey = panel.toolKey;
            panelEl.hidden = panel.hidden;
            host.appendChild(panelEl);
        });
        body.appendChild(host);
    });
    el.appendChild(body);
    return el;
}

function parseStepsHtml(html) {
    const out = [];
    const stepRe = /<details class="([^"]*)"([^>]*)>([\s\S]*?)<\/details>/g;
    let match = stepRe.exec(html);
    while (match) {
        const [, className, attrTail, body] = match;
        const step = {
            className,
            traceKey: attr(attrTail, "data-trace-key"),
            defaultOpen: attr(attrTail, "data-default-open"),
            open: /\sopen\s*$/.test(attrTail),
            title: (/class="interaction-step-title">([^<]*)</.exec(body) || [, ""])[1],
            rounds: [],
        };
        const roundRe = /<section class="task-trace-round-group" data-round-key="([^"]*)">/g;
        const marks = [];
        let roundMatch = roundRe.exec(body);
        while (roundMatch) {
            marks.push({ roundKey: roundMatch[1], start: roundMatch.index + roundMatch[0].length });
            roundMatch = roundRe.exec(body);
        }
        marks.forEach((mark, index) => {
            const chunk = body.slice(mark.start, index + 1 < marks.length ? marks[index + 1].start : body.length);
            const chipRe = /class="task-trace-round-chip ([^"]*)"\s+data-tool-key="([^"]*)"([\s\S]*?)<span class="task-trace-round-chip-title">([^<]*)</g;
            const chips = [];
            let chipMatch = chipRe.exec(chunk);
            while (chipMatch) {
                chips.push({
                    className: chipMatch[1],
                    toolKey: chipMatch[2],
                    title: chipMatch[4],
                    active: /aria-pressed="true"/.test(chipMatch[3]),
                });
                chipMatch = chipRe.exec(chunk);
            }
            const panelRe = /<section class="task-trace-round-panel([^"]*)" data-tool-key="([^"]*)"( hidden)?>/g;
            const panels = [];
            let panelMatch = panelRe.exec(chunk);
            while (panelMatch) {
                panels.push({ toolKey: panelMatch[2], hidden: panelMatch[3] !== undefined });
                panelMatch = panelRe.exec(chunk);
            }
            step.rounds.push({
                roundKey: mark.roundKey,
                activeToolKey: attr(chunk, "data-active-tool-key"),
                chips,
                panels,
            });
        });
        out.push(step);
        match = stepRe.exec(html);
    }
    return out;
}

class StubDocument {
    getElementById() { return null; }
    createElement(tag) { return new StubHTMLElement(tag || "DIV"); }
    querySelector() { return null; }
    querySelectorAll() { return []; }
    addEventListener() {}
}

function loadApp({ apiClient = null } = {}) {
    // 详情接口以外的调用一律给空实现:这条车道只关心节点详情重绘。
    const client = apiClient
        ? new Proxy(apiClient, {
            get: (target, prop) => (prop in target ? target[prop] : async () => null),
        })
        : null;
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
        fetch: async () => ({ ok: true, json: async () => ({}) }),
        lucide: { createIcons() {} },
        marked: { parse: (value) => String(value) },
        DOMPurify: { sanitize: (value) => String(value) },
        structuredClone: global.structuredClone,
        performance: { now: () => 0 },
        requestAnimationFrame: (callback) => { callback(); return 1; },
        cancelAnimationFrame: () => {},
        WebSocket: function WebSocket() {},
        addEventListener() {},
        removeEventListener() {},
    };
    if (client) context.ApiClient = client;
    context.window = context;
    vm.createContext(context);
    vm.runInContext(
        `${TASK_VIEW_CODE}\n${APP_CODE}\n${TASKS_CODE}\nthis.__x = { showAgent, renderExecutionTrace, captureTaskDetailViewState, normalizeTaskDetailViewState, applyTaskTraceItemViewState, restoreTaskDetailViewState, setTraceRoundActiveTool, stashTaskDetailViewState, S, U };`,
        context,
    );
    const api = context.__x;
    const flowHost = new StubHTMLElement("DIV");
    flowHost.className = "task-trace-host";
    api.U.adFlow = flowHost;
    api.U.adMessages = null;
    api.U.adSpawnReviews = null;
    api.U.adFlowHeading = new StubHTMLElement();
    api.U.detail = new StubHTMLElement();
    api.U.detail.style.display = "flex";
    api.U.nodeEmpty = new StubHTMLElement();
    api.U.feedTitle = new StubHTMLElement();
    api.U.adRoundSummary = new StubHTMLElement();
    api.U.adStatus = new StubHTMLElement();
    api.U.adRole = new StubHTMLElement();
    api.U.adOutput = new StubHTMLElement();
    api.U.adAcceptance = new StubHTMLElement();
    api.U.adErrorHistory = new StubHTMLElement();
    api.U.artifactList = new StubHTMLElement();
    api.U.artifactHeading = new StubHTMLElement();
    api.U.artifactContent = new StubHTMLElement();
    api.U.nodeContextDisclosure = new StubHTMLElement();
    return api;
}

function tool(id, name, status, output) {
    return {
        tool_call_id: id,
        tool_name: name,
        status,
        arguments_text: `{"path":"${id}"}`,
        output_text: output,
    };
}

function stage(id, roundSpecs) {
    return {
        stage_id: id,
        stage_index: Number(id.slice(-1)) || 1,
        mode: "自主执行",
        status: "进行中",
        stage_goal: `goal ${id}`,
        rounds: roundSpecs.map((round, index) => ({
            round_id: `${id}-r${index + 1}`,
            round_index: index + 1,
            created_at: `2026-10-09T10:0${index}:00`,
            text: `round ${index + 1}`,
            tools: round,
        })),
    };
}

function detailPayload(nodeId, stageDefs) {
    return {
        node_id: nodeId,
        state: "in_progress",
        // 带 full 档才不会被本地缓存判成"需要重拉"，首帧才走得通。
        detail_level: "full",
        execution_trace: {
            initial_prompt: "prompt",
            final_output: "",
            stages: stageDefs,
        },
    };
}

// 详情接口挂起等待放行，测试据此在「请求在飞」这段窗口里模拟用户操作。
function makeClient(payloadFor) {
    const gate = { resolve: null };
    return {
        letThrough() {
            const resolve = gate.resolve;
            gate.resolve = null;
            if (resolve) resolve();
        },
        getTaskNodeDetail: async (taskId, nodeId) => {
            await new Promise((resolve) => { gate.resolve = resolve; });
            return payloadFor(nodeId);
        },
    };
}

function traceList(api) {
    return api.U.adFlow.querySelector(".task-trace-list");
}

function readOut(api) {
    const steps = traceList(api).querySelectorAll(".task-trace-step");
    return steps.map((step) => ({
        key: step.dataset.traceKey,
        open: step.open,
        panels: step.querySelectorAll(".task-trace-round-tools").flatMap((host) => (
            host.querySelectorAll(".task-trace-round-panel").filter((panel) => !panel.hidden).map((panel) => (
                `${host.dataset.roundKey}=${panel.dataset.toolKey}`
            ))
        )),
    }));
}

function stepByKey(api, key) {
    return traceList(api).querySelectorAll(".task-trace-step").find((step) => step.dataset.traceKey === key);
}

function openChip(api, stepKey, roundKey, toolKey) {
    const step = stepByKey(api, stepKey);
    assert.ok(step, `step ${stepKey} 必须已渲染`);
    const host = step.querySelectorAll(".task-trace-round-tools")
        .find((item) => item.dataset.roundKey === roundKey);
    assert.ok(host, `round ${roundKey} 必须已渲染`);
    const chip = host.querySelectorAll(".task-trace-round-chip")
        .find((item) => item.dataset.toolKey === toolKey);
    assert.ok(chip, `chip ${toolKey} 必须已渲染`);
    // 点击 chip 走的就是这条选中道
    api.setTraceRoundActiveTool(host, toolKey);
}

function v1Stages() {
    return [
        stage("stage-1", [[tool("call-a", "filesystem_read", "success", "aaa")]]),
        stage("stage-2", [
            [tool("call-b", "filesystem_read", "success", "bbb")],
            [tool("call-c", "exec", "running", "")],
        ]),
    ];
}

function v2Stages() {
    // 工具刷新:在飞的那次调用返回,正文与状态都变了 ⇒ 轨道整段重建
    return [
        stage("stage-1", [[tool("call-a", "filesystem_read", "success", "aaa")]]),
        stage("stage-2", [
            [tool("call-b", "filesystem_read", "success", "bbb")],
            [tool("call-c", "exec", "success", "ccc done")],
        ]),
    ];
}

async function firstPaint(api) {
    api.S.currentTaskId = "t1";
    api.S.selectedNodeId = "n1";
    api.S.taskNodeDetails = { n1: detailPayload("n1", v1Stages()) };
    await api.showAgent({ node_id: "n1", title: "n1", state: "in_progress" }, { preserveViewState: false });
}

async function refresh(api, client, node, options) {
    const pending = api.showAgent(node, options);
    client.letThrough();
    await pending;
}

test("工具刷新后:展开的阶段卡与按轮选中的工具面板回到同一轮", async () => {
    const client = makeClient((nodeId) => detailPayload(nodeId, v2Stages()));
    const api = loadApp({ apiClient: client });
    await firstPaint(api);

    // 用户:展开第一张阶段卡,并在 stage-2 的第 2 轮点开工具面板。
    stepByKey(api, "stage:stage-1").open = true;
    openChip(api, "stage:stage-2", "stage-2-r2", "stage-2-r2:tool:call-c");
    assert.deepEqual(readOut(api).find((item) => item.key === "stage:stage-2").panels, [
        "stage-2-r2=stage-2-r2:tool:call-c",
    ]);

    // task.node.patch 那一步:先捕获+存档,再把同一节点按 forceRefresh 重绘。
    const patchViewState = api.captureTaskDetailViewState();
    api.stashTaskDetailViewState({ nodeId: "n1", viewState: patchViewState });
    api.S.pendingTaskDetailRestore = { nodeId: "n1", viewState: patchViewState };
    await refresh(api, client, { node_id: "n1", state: "in_progress" }, { preserveViewState: true, forceRefresh: true });

    const out = readOut(api);
    const stage1 = out.find((item) => item.key === "stage:stage-1");
    const stage2 = out.find((item) => item.key === "stage:stage-2");
    assert.equal(stage1.open, true, "用户展开的阶段卡在刷新后必须仍展开");
    assert.deepEqual(stage2.panels, ["stage-2-r2=stage-2-r2:tool:call-c"],
        "工具面板必须还原到用户选中的那一轮,不是兜到第一轮");
    assert.equal(out.find((item) => item.key === "initial_prompt").open, false,
        "还原只搬用户放下的位置,不能顺手替用户开别的卡片");
});

test("请求在飞窗口里新展开的内容不被上一次捕获折回", async () => {
    const client = makeClient((nodeId) => detailPayload(nodeId, v2Stages()));
    const api = loadApp({ apiClient: client });
    await firstPaint(api);

    stepByKey(api, "stage:stage-1").open = true;
    openChip(api, "stage:stage-2", "stage-2-r2", "stage-2-r2:tool:call-c");

    const pending = api.showAgent(
        { node_id: "n1", state: "in_progress" },
        { preserveViewState: true, forceRefresh: true },
    );
    // 详情请求在飞:用户在这段窗口里又展开了初始提示词与 stage-1 的工具面板。
    stepByKey(api, "initial_prompt").open = true;
    openChip(api, "stage:stage-1", "stage-1-r1", "stage-1-r1:tool:call-a");
    client.letThrough();
    await pending;

    const out = readOut(api);
    assert.equal(out.find((item) => item.key === "initial_prompt").open, true,
        "在飞窗口里刚展开的卡片不能被刷新折回");
    assert.deepEqual(out.find((item) => item.key === "stage:stage-1").panels, [
        "stage-1-r1=stage-1-r1:tool:call-a",
    ], "在飞窗口里刚点开的工具面板必须保持打开");
    assert.deepEqual(out.find((item) => item.key === "stage:stage-2").panels, [
        "stage-2-r2=stage-2-r2:tool:call-c",
    ], "窗口之前放下的面板同样要保持");
});

test("切走再切回:按轮选中的工具面板从该节点自己的存档里回来", async () => {
    const client = makeClient((nodeId) => detailPayload(nodeId, v1Stages()));
    const api = loadApp({ apiClient: client });
    await firstPaint(api);

    stepByKey(api, "stage:stage-1").open = true;
    openChip(api, "stage:stage-2", "stage-2-r2", "stage-2-r2:tool:call-c");
    // 树上来回换节点时,离开的那一步会把当前节点的展开态存档（存档是要过一遍归一的）。
    api.stashTaskDetailViewState({ nodeId: "n1" });

    api.S.selectedNodeId = "n2";
    api.S.taskNodeDetails = {};
    await refresh(api, client, { node_id: "n2", state: "in_progress" }, { preserveViewState: false });

    api.S.selectedNodeId = "n1";
    await refresh(api, client, { node_id: "n1", state: "in_progress" }, { preserveViewState: false });

    const out = readOut(api);
    assert.equal(out.find((item) => item.key === "stage:stage-1").open, true,
        "切回来时用户展开的阶段卡必须按存档回来");
    assert.deepEqual(out.find((item) => item.key === "stage:stage-2").panels, [
        "stage-2-r2=stage-2-r2:tool:call-c",
    ], "按轮选中的工具面板必须一起进存档,否则切回来只剩第一轮");
});

test("换成另一个节点时按该节点自己的存档起板", async () => {
    const client = makeClient((nodeId) => detailPayload(nodeId, v1Stages()));
    const api = loadApp({ apiClient: client });
    await firstPaint(api);
    stepByKey(api, "stage:stage-1").open = true;
    openChip(api, "stage:stage-2", "stage-2-r2", "stage-2-r2:tool:call-c");

    api.S.selectedNodeId = "n2";
    api.S.taskNodeDetails = {};
    await refresh(api, client, { node_id: "n2", state: "in_progress" }, { preserveViewState: false });

    const out = readOut(api);
    assert.equal(out.find((item) => item.key === "stage:stage-1").open, false,
        "另一个节点必须按自己的默认档起板,不能继承上一节点的展开");
    assert.deepEqual(out.find((item) => item.key === "stage:stage-2").panels, [],
        "另一个节点不能继承上一节点选中的工具面板");
});
