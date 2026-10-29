const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

// 节点详情执行流程最底部那一行「最新思考」：与会话气泡里的思考行同款
// （读数在左、正文最后一行在右，点整行头展开全文）。四条轴各钉一次：
// 取哪一轮（最近一次有正文的）、正文是否活过归一化、折叠/展开的形状、换轮之后折叠重来。

const FRONTEND = path.join(__dirname, "..", "..", "g3ku", "web", "frontend", "tasks");
const STORE_CODE = fs.readFileSync(path.join(FRONTEND, "node-detail-store.js"), "utf8");
const DETAIL_CODE = fs.readFileSync(path.join(FRONTEND, "node-detail.js"), "utf8");
const APP_CODE = fs.readFileSync(
    path.join(__dirname, "..", "..", "g3ku", "web", "frontend", "org_graph_app.js"),
    "utf8",
).replace(/\r\n/g, "\n");

function sliceAppFunction(name) {
    const start = APP_CODE.indexOf(`function ${name}(`);
    assert.ok(start >= 0, `找不到 app bundle 里的 ${name}`);
    const end = APP_CODE.indexOf("\n}\n", start);
    assert.ok(end > start, `${name} 的结尾无法定位`);
    return APP_CODE.slice(start, end + 2);
}

class El {
    constructor(tag = "div") {
        this.tagName = tag;
        this.className = "";
        this.hidden = false;
        this.textContent = "";
        this.title = "";
        this.children = [];
        this.parentElement = null;
        this.dataset = {};
        this.listeners = {};
        this.attrs = {};
        this.scrollTop = 0;
        this.scrollHeight = 100;
        this.classList = {
            add: (...tokens) => { this.className = [...new Set([...this.className.split(/\s+/).filter(Boolean), ...tokens])].join(" "); },
            remove: (...tokens) => { this.className = this.className.split(/\s+/).filter((t) => t && !tokens.includes(t)).join(" "); },
            toggle: (token, force) => {
                const has = this.className.split(/\s+/).includes(token);
                const want = force === undefined ? !has : !!force;
                if (want) this.classList.add(token); else this.classList.remove(token);
                return want;
            },
            contains: (token) => this.className.split(/\s+/).includes(token),
        };
    }

    appendChild(child) {
        // 真 DOM 里 appendChild 会把已有子节点挪到末尾，桩里同样处理——
        // 否则"每次渲染把这一行压到最下面"会变成复制出第二行。
        const existing = this.children.indexOf(child);
        if (existing >= 0) this.children.splice(existing, 1);
        child.parentElement = this;
        this.children.push(child);
        return child;
    }

    remove() {
        const siblings = this.parentElement?.children || [];
        const index = siblings.indexOf(this);
        if (index >= 0) siblings.splice(index, 1);
        this.parentElement = null;
    }

    addEventListener(type, handler) { (this.listeners[type] = this.listeners[type] || []).push(handler); }
    fire(type, event = null) {
        const payload = event || { target: this, preventDefault() {} };
        (this.listeners[type] || []).forEach((handler) => handler(payload));
    }
    setAttribute(key, value) { this.attrs[key] = String(value); }
    getAttribute(key) { return Object.prototype.hasOwnProperty.call(this.attrs, key) ? this.attrs[key] : null; }
    querySelector(selector) {
        const name = String(selector || "").replace(/^\./, "");
        const walk = (node) => {
            for (const child of node.children) {
                if (child.className.split(/\s+/).includes(name)) return child;
                const found = walk(child);
                if (found) return found;
            }
            return null;
        };
        return walk(this);
    }
}

function buildHarness() {
    const host = new El("div");
    host.className = "task-trace-host";
    const sandbox = {
        console,
        U: { adFlow: host },
        document: { createElement: (tag) => new El(tag) },
        HTMLElement: El,
        normalizeInt: (value, fallback = 0) => {
            const parsed = Number(value);
            return Number.isFinite(parsed) ? Math.trunc(parsed) : Math.trunc(fallback);
        },
        esc: (value) => String(value ?? ""),
        formatCompactTime: () => "10:00",
        readableText: (value) => String(value ?? ""),
        repairAcceptanceTitle: (value) => String(value ?? ""),
        isTerminalTreeNodeStatus: () => false,
        taskPauseDisplayActive: () => false,
        ceoContextLoaderKind: () => "",
        ceoContextLoadNoticeIconName: () => "",
        normalizeUploadList: (items) => (Array.isArray(items) ? items : []),
    };
    vm.createContext(sandbox);
    vm.runInContext(
        [
            sliceAppFunction("formatElapsedDuration"),
            sliceAppFunction("formatTokenCount"),
            sliceAppFunction("thinkingReadingParts"),
            STORE_CODE,
            DETAIL_CODE,
            "this.__lane = { latestTraceThinking, renderLatestThinkingLine, normalizeTurnThinking };",
        ].join("\n"),
        sandbox
    );
    return { sandbox, host, lane: sandbox.__lane };
}

function turn(seq, thinking) {
    return { seq, text: "", text_ref: "", created_at: "", status: "completed", thinking, tools: [] };
}

test("取最近一次有正文的那一轮，读数与最后一行都来自那一轮", () => {
    const { lane } = buildHarness();
    const latest = lane.latestTraceThinking({
        turns: [
            turn(1, { seconds: 12, chars: 400, est_tokens: 100, text: "旧的一轮思考" }),
            turn(2, { seconds: 8, chars: 240, est_tokens: 60, tokens: 59, text: "第一行\n第二行，最新" }),
            // 最后一轮只有工具、没有正文：这一行仍取上一轮，不整行消失。
            turn(3, { seconds: 2, chars: 20, est_tokens: 5 }),
            turn(4, null),
        ],
    });
    assert.equal(latest.key, "thinking:2");
    assert.equal(latest.tail, "第二行，最新");
    assert.equal(latest.meta, "思考 8秒 · 59 tok");
    assert.equal(latest.text, "第一行\n第二行，最新");
    assert.equal(latest.truncated, false);
});

test("一轮正文都没有时整行不画", () => {
    const { lane } = buildHarness();
    assert.equal(lane.latestTraceThinking({ turns: [turn(1, { seconds: 3, chars: 30, est_tokens: 8 })] }), null);
    assert.equal(lane.latestTraceThinking({ turns: [] }), null);
    assert.equal(lane.latestTraceThinking(null), null);
});

test("截断的正文标出来", () => {
    const { lane } = buildHarness();
    const latest = lane.latestTraceThinking({
        turns: [turn(1, { seconds: 1, text: "前段", text_truncated: true })],
    });
    assert.equal(latest.truncated, true);
});

test("正文活过归一化，写侧多塞的键不许进来", () => {
    const { lane } = buildHarness();
    const reading = lane.normalizeTurnThinking({
        seconds: 4.5,
        chars: 200,
        est_tokens: 50,
        tokens: 48,
        text: "想了什么",
        text_truncated: true,
        reasoning_content: "不该出现的副本",
    });
    assert.equal(reading.text, "想了什么");
    assert.equal(reading.text_truncated, true);
    assert.equal(reading.seconds, 4.5);
    assert.equal(reading.tokens, 48);
    assert.ok(!("reasoning_content" in reading));
    // 只有正文、没有任何读数的一轮也要留下：底部那一行靠它出字。
    const textOnly = lane.normalizeTurnThinking({ text: "只有正文" });
    assert.equal(textOnly.text, "只有正文");
    assert.equal(textOnly.seconds, 0);
});

test("行挂在流程最下面，折叠一行、点开全文、再点收起", () => {
    const { host, lane, sandbox } = buildHarness();
    const list = new El("div");
    list.className = "task-trace-list";
    host.appendChild(list);

    lane.renderLatestThinkingLine({
        turns: [turn(1, { seconds: 75, chars: 5200, est_tokens: 1300, text: "第一步：看要求\n第二步：查文件后收口" })],
    });

    const line = host.querySelector(".task-trace-latest-thinking");
    assert.ok(line, "没有建出最新思考行");
    assert.ok(line.className.includes("ceo-thinking-line"), "要复用会话那一行的样式");
    assert.equal(host.children[host.children.length - 1], line, "必须压在流程最下面");

    const head = line.querySelector(".ceo-thinking-line-head");
    const meta = line.querySelector(".ceo-thinking-line-meta");
    const tail = line.querySelector(".ceo-thinking-line-tail");
    const pane = line.querySelector(".ceo-thinking-line-pane");
    assert.equal(meta.textContent, "思考 1分15秒 · ~1,300 tok");
    assert.equal(tail.textContent, "第二步：查文件后收口");
    // 折叠态：正文窗不占位，不是靠透明度藏起来。
    assert.equal(pane.hidden, true);
    assert.equal(head.getAttribute("aria-expanded"), "false");
    assert.equal(pane.textContent, "第一步：看要求\n第二步：查文件后收口");

    head.fire("click");
    assert.equal(pane.hidden, false);
    assert.equal(head.getAttribute("aria-expanded"), "true");
    assert.equal(line.classList.contains("is-expanded"), true);

    head.fire("click");
    assert.equal(pane.hidden, true);
    assert.equal(head.getAttribute("aria-expanded"), "false");

    // 再次渲染同一条思考：不重建、不重置折叠姿态之外的东西。
    sandbox.U.adFlow.children = host.children;
    lane.renderLatestThinkingLine({
        turns: [turn(1, { seconds: 75, chars: 5200, est_tokens: 1300, text: "第一步：看要求\n第二步：查文件后收口" })],
    });
    assert.equal(host.querySelector(".task-trace-latest-thinking"), line, "同一条思考不许重建");
});

test("换到新一轮就折叠重来；没有正文时整行摘掉", () => {
    const { host, lane } = buildHarness();
    lane.renderLatestThinkingLine({ turns: [turn(1, { text: "旧一轮" })] });
    const line = host.querySelector(".task-trace-latest-thinking");
    const head = line.querySelector(".ceo-thinking-line-head");
    head.fire("click");
    assert.equal(head.getAttribute("aria-expanded"), "true");

    lane.renderLatestThinkingLine({ turns: [turn(1, { text: "旧一轮" }), turn(2, { text: "新一轮正文" })] });
    assert.equal(host.querySelector(".task-trace-latest-thinking"), line, "同一行就地更新");
    assert.equal(head.getAttribute("aria-expanded"), "false", "换了思考必须收回折叠态");
    assert.equal(head.title, "点击展开思考过程");

    lane.renderLatestThinkingLine({ turns: [turn(1, { seconds: 3 })] });
    assert.equal(host.querySelector(".task-trace-latest-thinking"), null, "没有正文时整行摘掉");
});

test("截断的正文在展开窗里说明只显示前段", () => {
    const { host, lane } = buildHarness();
    lane.renderLatestThinkingLine({ turns: [turn(1, { text: "前段", text_truncated: true })] });
    const pane = host.querySelector(".ceo-thinking-line-pane");
    assert.match(pane.textContent, /^前段\n\n……（思考过长，仅显示前段）$/);
});
