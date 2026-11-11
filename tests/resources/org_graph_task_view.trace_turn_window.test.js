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

// 节点详情多轮加载慢的三条车道合同：
// ① 发消息后的 settle 轮询打消息窄口，不重拉 full 详情；
// ② 「轮数 N」读全量总数，不数随包窗口；
// ③ 窗口截断时列表最顶部有「加载更早轮次」，点击前插一页并保住展开态。

const TASK_VIEW_CODE = readFrontendSources(["task_view"]).join("\n");
const APP_CODE = readFrontendSources(["app"]).join("\n");
const TASKS_CODE = readFrontendSources(["tasks"]).join("\n");

function attr(chunk, name) {
    const match = new RegExp(`${name}="([^"]*)"`).exec(chunk);
    return match ? match[1] : "";
}

function classMatches(el, selector) {
    const wanted = String(selector || "").split(".").filter(Boolean);
    if (!wanted.length) return false;
    const own = String(el.className || "").split(/\s+/).filter(Boolean);
    return wanted.every((name) => own.includes(name));
}

// 轨迹列表的重绘是整段 innerHTML 重建：按渲染产物重建成桩树，展开态与点击才有落点。
// 只解析两类顶层行——轮次卡（details）与「加载更早轮次」按钮行。
function parseRows(html) {
    const rows = [];
    const rowRe = /<details class="([^"]*)"([^>]*)>([\s\S]*?)<\/details>|<div class="task-trace-earlier">([\s\S]*?)<\/div>/g;
    let match = rowRe.exec(html);
    while (match) {
        if (match[1] !== undefined) {
            const el = new TraceHost("DETAILS");
            el.className = match[1];
            el.dataset.traceKey = attr(match[2], "data-trace-key");
            el.open = /\sopen\s*$/.test(match[2]);
            rows.push(el);
        } else {
            const chunk = match[4];
            const button = new TraceHost("BUTTON");
            button.className = "task-trace-earlier-btn";
            button.dataset.traceEarlier = attr(chunk, "data-trace-earlier");
            button.dataset.remainingTurns = attr(chunk, "data-remaining-turns");
            button.disabled = /\sdisabled/.test(chunk);
            button.textContent = (/<button[^>]*>([^<]*)<\/button>/.exec(chunk) || [, ""])[1];
            rows.push(button);
        }
        match = rowRe.exec(html);
    }
    return rows;
}

class TraceHost extends StubHTMLElement {
    get innerHTML() {
        return this._innerHTML;
    }

    set innerHTML(value) {
        this._innerHTML = String(value);
        this.children = parseRows(this._innerHTML).map((row) => {
            row.parentElement = this;
            return row;
        });
    }

    querySelector(selector) {
        for (const child of this.children) {
            if (classMatches(child, selector)) return child;
            const nested = child.querySelector(selector);
            if (nested) return nested;
        }
        return null;
    }

    querySelectorAll(selector) {
        const out = [];
        const walk = (node) => {
            node.children.forEach((child) => {
                if (classMatches(child, selector)) out.push(child);
                walk(child);
            });
        };
        walk(this);
        return out;
    }

    // 委托点击用的是属性选择器（[data-trace-earlier]），共享桩只认类名，这里补上。
    closest(selector) {
        const raw = String(selector || "");
        const attrSelector = /^\[([a-zA-Z-]+)\]$/.exec(raw);
        let node = this;
        while (node) {
            if (attrSelector) {
                const key = attrSelector[1]
                    .replace(/^data-/, "")
                    .replace(/-([a-z])/g, (_whole, letter) => letter.toUpperCase());
                if (node.dataset && Object.prototype.hasOwnProperty.call(node.dataset, key)) return node;
            } else if (classMatches(node, raw)) {
                return node;
            }
            node = node.parentElement;
        }
        return null;
    }
}

class TraceDocument extends StubDocument {
    createElement(tag) {
        return new TraceHost(tag || "DIV");
    }
}

function loadApp({ apiClient = null } = {}) {
    const client = apiClient
        ? new Proxy(apiClient, { get: (target, prop) => (prop in target ? target[prop] : async () => null) })
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
        document: new TraceDocument(),
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
        `${TASK_VIEW_CODE}\n${APP_CODE}\n${TASKS_CODE}\nthis.__x = { showAgent, renderExecutionTrace, loadEarlierTraceTurns, settleTaskNoticeAfterSend, runTaskNoticeSettleTick, taskFlowUsageText, S, U };`,
        context,
    );
    const api = context.__x;
    api.U.adFlow = new TraceHost("DIV");
    api.U.adMessages = null;
    api.U.adSpawnReviews = null;
    api.U.adFlowHeading = new TraceHost();
    api.U.detail = new TraceHost();
    api.U.detail.style.display = "flex";
    api.U.nodeEmpty = new TraceHost();
    api.U.feedTitle = new TraceHost();
    api.U.adTokenSummary = new TraceHost();
    api.U.adStatus = new TraceHost();
    api.U.adRole = new TraceHost();
    api.U.adOutput = new TraceHost();
    api.U.adAcceptance = new TraceHost();
    api.U.adErrorHistory = new TraceHost();
    api.U.artifactList = new TraceHost();
    api.U.artifactHeading = new TraceHost();
    api.U.artifactContent = new TraceHost();
    api.U.nodeContextDisclosure = new TraceHost();
    return api;
}

function tool(seq, index) {
    return {
        tool_call_id: `call-${seq}-${index}`,
        tool_name: "exec",
        status: "success",
        arguments_text: '{"command":"ls"}',
        output_text: `preview-${seq}-${index}`,
        output_ref: `artifact:out-${seq}-${index}`,
    };
}

function turn(seq) {
    return {
        seq,
        text: `进展 ${seq}`,
        created_at: `2026-11-11T10:00:0${seq}`,
        status: "completed",
        tools: [tool(seq, 0)],
    };
}

// full 档随包只带窗口内的轮次：默认 5 轮里只发最近 3 轮（seq 3..5）。
function windowedDetail(nodeId, { totalTurns = 5, firstSeq = 3, truncated = true, callCount = 2 } = {}) {
    const turns = [];
    for (let seq = firstSeq; seq <= totalTurns; seq += 1) turns.push(turn(seq));
    return {
        node_id: nodeId,
        state: "in_progress",
        detail_level: "full",
        token_usage: {
            tracked: true,
            input_tokens: 1200,
            output_tokens: 340,
            cache_hit_tokens: 800,
            call_count: callCount,
        },
        execution_trace: { initial_prompt: "prompt", final_output: "", turns },
        execution_trace_total_turns: totalTurns,
        execution_trace_window: {
            total_turns: totalTurns,
            window_turns: turns.length,
            truncated,
            first_seq: firstSeq,
            last_seq: totalTurns,
        },
        message_list: [],
        sent_message_list: [],
    };
}

async function firstPaint(api, detail) {
    api.S.currentTaskId = "t1";
    api.S.selectedNodeId = "n1";
    api.S.taskNodeDetails = { n1: detail };
    api.S.currentNodeDetail = detail;
    await api.showAgent({ node_id: "n1", title: "n1", state: "in_progress" }, { preserveViewState: false });
}

function traceList(api) {
    return api.U.adFlow.querySelector(".task-trace-list");
}

function earlierButton(api) {
    return api.U.adFlow.querySelector(".task-trace-earlier-btn");
}

function stepByKey(api, key) {
    return traceList(api).querySelectorAll(".task-trace-step").find((step) => step.dataset.traceKey === key);
}

function renderedTurnKeys(api) {
    return traceList(api).querySelectorAll(".task-trace-step").map((step) => step.dataset.traceKey);
}

// 缓存里的数组可能是在 vm realm 里重建的：取回本 realm 再比，否则 strict 断言比的是原型。
function turnSeqs(detail) {
    return Array.from(detail.execution_trace.turns, (item) => Number(item.seq));
}

// 点击走的是列表上的委托监听，与真实 DOM 同一条入口。
function dispatchEarlierClick(api) {
    const button = earlierButton(api);
    assert.ok(button, "「加载更早轮次」按钮必须已渲染");
    traceList(api).dispatchEvent({
        type: "click",
        target: button,
        preventDefault() {},
        stopPropagation() {},
    });
    return api.S.taskTraceEarlierRequests["t1:n1"];
}

function earlierPageClient(calls, payload, { gate = null } = {}) {
    return {
        getTaskNodeTraceTurns: async (taskId, nodeId, options) => {
            calls.push([taskId, nodeId, options]);
            if (gate) await gate;
            return payload;
        },
    };
}

test("settle 轮询打消息窄口，不重拉 full 详情", async () => {
    const calls = [];
    const detail = windowedDetail("n1");
    const api = loadApp({
        apiClient: {
            getTaskNodeMessages: async (taskId, nodeId) => {
                calls.push(["messages", taskId, nodeId]);
                return {
                    node_id: nodeId,
                    task_id: taskId,
                    message_list: [{ message: "hello", status: "pending" }],
                    sent_message_list: [],
                };
            },
            getTaskNodeDetail: async (taskId, nodeId) => {
                calls.push(["detail", taskId, nodeId]);
                return detail;
            },
        },
    });
    await firstPaint(api, detail);

    api.settleTaskNoticeAfterSend("t1", "n1", "hello");
    await api.runTaskNoticeSettleTick();

    assert.deepEqual(calls, [["messages", "t1", "n1"]], "轮询只判落账，full 详情一次都不该重拉");
    const cached = api.S.taskNodeDetails.n1;
    assert.deepEqual(cached.message_list, [{ message: "hello", status: "pending" }]);
    assert.deepEqual(turnSeqs(cached), [3, 4, 5],
        "两份列表合并进缓存，轨迹字段仍来自缓存而不是被窄口整包替换");
    assert.equal(cached.execution_trace_total_turns, 5);
    assert.deepEqual(api.S.currentNodeDetail.message_list, [{ message: "hello", status: "pending" }],
        "选中节点的当前详情也带上落账后的列表");
});

test("用量行没带轮数时，节点页读全量总数而不是窗口轮数", async () => {
    const api = loadApp();
    await firstPaint(api, windowedDetail("n1", { callCount: 0 }));

    assert.match(api.U.adTokenSummary.textContent, /轮数 5$/,
        "窗口只带 3 轮，读数必须是全量 5 轮");
});

test("没有窗口读数时轮数退回随包轮次数", async () => {
    const detail = windowedDetail("n1", { callCount: 0 });
    delete detail.execution_trace_total_turns;
    delete detail.execution_trace_window;
    const api = loadApp();
    await firstPaint(api, detail);

    assert.match(api.U.adTokenSummary.textContent, /轮数 3$/);
});

test("用量行已带轮数时沿用单点产出，节点页不另抄一份", async () => {
    const api = loadApp();
    await firstPaint(api, windowedDetail("n1", { callCount: 7 }));

    // 「轮数」由 app bundle 的 buildTokenUsageParts 按 call_count 单点产出，
    // 它本来就是全量计数，不受轨迹窗口影响；节点页只在它缺席时兜底。
    assert.match(api.U.adTokenSummary.textContent, /轮数 7$/);
});

test("窗口截断时「加载更早轮次」落在列表最顶部", async () => {
    const api = loadApp();
    await firstPaint(api, windowedDetail("n1"));

    const html = traceList(api).innerHTML;
    const rowIndex = html.indexOf("task-trace-earlier");
    assert.ok(rowIndex >= 0, "截断窗口必须给出翻页入口");
    assert.ok(rowIndex < html.indexOf("<details"), "这一行在初始提示词与所有轮次卡之上");
    const button = earlierButton(api);
    assert.equal(button.dataset.traceEarlier, "n1");
    assert.match(button.textContent, /还剩约 2 轮/, "早轮数 = 总轮数 - 窗口轮数");
});

test("窗口未截断时不画这一行", async () => {
    const api = loadApp();
    await firstPaint(api, windowedDetail("n1", { totalTurns: 3, firstSeq: 1, truncated: false }));

    assert.equal(earlierButton(api), null);
    assert.ok(!traceList(api).innerHTML.includes("task-trace-earlier"));
});

test("点「加载更早轮次」前插一页并更新窗口读数", async () => {
    const calls = [];
    const api = loadApp({
        apiClient: earlierPageClient(calls, {
            turns: [turn(1), turn(2)],
            next_before_seq: 1,
            has_more: false,
            total_turns: 5,
        }),
    });
    await firstPaint(api, windowedDetail("n1"));

    const pending = dispatchEarlierClick(api);
    assert.ok(pending, "点击必须真的发了一拍");
    await pending;

    assert.equal(calls.length, 1);
    assert.deepEqual([calls[0][0], calls[0][1]], ["t1", "n1"]);
    // 参数对象在 vm realm 里造，逐字段断言而不是比原型。
    assert.equal(calls[0][2].beforeSeq, 3, "锚点取当前已加载最早一轮的 seq");
    assert.equal(calls[0][2].limit, 200);
    const cached = api.S.taskNodeDetails.n1;
    assert.deepEqual(turnSeqs(cached), [1, 2, 3, 4, 5],
        "返回的轮次老→新前插，不排在已加载轮次之后");
    const nextWindow = cached.execution_trace_window;
    assert.equal(nextWindow.total_turns, 5);
    assert.equal(nextWindow.window_turns, 5);
    assert.equal(nextWindow.truncated, false);
    assert.equal(nextWindow.first_seq, 1);
    assert.equal(nextWindow.last_seq, 5);
    assert.deepEqual(turnSeqs(api.S.currentNodeDetail), [1, 2, 3, 4, 5]);
    // 没有独立初始提示词宿主时它留在列表里：前插的轮次必须落在它之后、按老→新排。
    assert.deepEqual(renderedTurnKeys(api), [
        "initial_prompt", "turn:1", "turn:2", "turn:3", "turn:4", "turn:5",
    ]);
    assert.equal(earlierButton(api), null, "翻到头之后这一行消失");
});

test("还有更早轮次时按钮留着并换新的剩余数", async () => {
    const calls = [];
    const api = loadApp({
        apiClient: earlierPageClient(calls, {
            turns: [turn(2)],
            next_before_seq: 2,
            has_more: true,
            total_turns: 5,
        }),
    });
    await firstPaint(api, windowedDetail("n1"));

    await dispatchEarlierClick(api);

    const button = earlierButton(api);
    assert.ok(button, "has_more=true 时入口必须留着");
    assert.equal(button.dataset.remainingTurns, "1");
    assert.match(button.textContent, /还剩约 1 轮/);
    assert.equal(button.disabled, false, "落账后回到可点状态");
});

test("连点只发一拍，在飞期间按钮是加载态", async () => {
    let release;
    const gate = new Promise((resolve) => { release = resolve; });
    const calls = [];
    const api = loadApp({
        apiClient: earlierPageClient(calls, { turns: [turn(2)], next_before_seq: 2, has_more: true }, { gate }),
    });
    await firstPaint(api, windowedDetail("n1"));

    const pending = dispatchEarlierClick(api);
    assert.equal(earlierButton(api).disabled, true);
    assert.match(earlierButton(api).textContent, /正在加载/);
    dispatchEarlierClick(api);
    dispatchEarlierClick(api);
    assert.equal(calls.length, 1, "在飞防重入：连点不许叠出第二拍");

    release();
    await pending;
    assert.equal(calls.length, 1);
});

test("换节点后在飞的这一页作废", async () => {
    let release;
    const gate = new Promise((resolve) => { release = resolve; });
    const calls = [];
    const api = loadApp({
        apiClient: earlierPageClient(calls, { turns: [turn(1), turn(2)], next_before_seq: 1, has_more: false }, { gate }),
    });
    await firstPaint(api, windowedDetail("n1"));

    const pending = dispatchEarlierClick(api);
    api.S.selectedNodeId = "n2";
    release();
    await pending;

    assert.deepEqual(turnSeqs(api.S.taskNodeDetails.n1), [3, 4, 5],
        "选中节点已换，这一页没有落点");
    assert.equal(api.S.taskNodeDetails.n1.execution_trace_window.first_seq, 3);
});

test("前插重绘保住已展开的轮次卡", async () => {
    const calls = [];
    const api = loadApp({
        apiClient: earlierPageClient(calls, { turns: [turn(1), turn(2)], next_before_seq: 1, has_more: false }),
    });
    await firstPaint(api, windowedDetail("n1"));
    stepByKey(api, "turn:4").open = true;

    await dispatchEarlierClick(api);

    assert.equal(stepByKey(api, "turn:4").open, true,
        "前插重绘走捕获/还原机制，用户展开的卡不许被折回");
    assert.equal(stepByKey(api, "turn:3").open, false);
});

test("窗口内非尾部轮的工具行仍带 output_ref 回取入口", async () => {
    const api = loadApp();
    await firstPaint(api, windowedDetail("n1"));

    // 这些轮的出参是预览，全文由既有的按 ref 回取车道取（与 summary 档同形）。
    assert.match(traceList(api).innerHTML, /data-output-ref="artifact:out-3-0"/);
});