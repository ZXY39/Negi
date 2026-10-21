const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const { readFrontendSources } = require("./support/frontend_sources.js");

// CEO 思维链实时行这一车道的单测：只切片加载被测函数，其余依赖给最小桩。
// 覆盖四件真实踩过或容易被改回去的事：段号变了要整段重来（同跳重发/换链不能叠两份）、
// token 没真值时必须带 `~`、flex 容器盖掉 UA [hidden] 的那类 CSS 陷阱、
// 以及"点整行头开合"这套形状（没有独立按钮，历史行不带可点能力）。
const APP_PATH = "g3ku/web/frontend/org_graph_app.js";
const CSS_PATH = "g3ku/web/frontend/org_graph.css";
// 这台机器上工作树是 CRLF，按 `\n}\n` 切函数体必须先把换行归一，否则切片全部失配。
const APP_CODE = readFrontendSources(["app"]).join("\n").replace(/\r\n/g, "\n");
const CSS_CODE = fs.readFileSync(CSS_PATH, "utf8").replace(/\r\n/g, "\n");

const NAMES = [
    "normalizeCeoSnapshotReasoning",
    "ensureCeoThinkingLineEl",
    "renderCeoThinkingLineIntoTurn",
    "applyCeoReasoningDelta",
    "applyCeoThinkingFromInflightSnapshot",
    "renderCeoThinkingSummaryFromSnapshot",
];

function sliceFunction(name) {
    const start = APP_CODE.indexOf(`function ${name}(`);
    assert.ok(start >= 0, `找不到函数：${name}`);
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
        this.isConnected = true;
        this.listeners = {};
        this.attrs = {};
        this.style = {};
        this.scrollTop = 0;
        this.scrollHeight = 100;
        this.clientHeight = 40;
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

    get nextElementSibling() {
        const siblings = this.parentElement?.children || [];
        return siblings[siblings.indexOf(this) + 1] || null;
    }

    appendChild(child) { child.parentElement = this; this.children.push(child); return child; }

    insertBefore(child, anchor) {
        child.parentElement = this;
        const index = anchor ? this.children.indexOf(anchor) : -1;
        if (index < 0) this.children.push(child); else this.children.splice(index, 0, child);
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
    const created = [];
    const sandbox = {
        document: {
            createElement: (tag) => { const el = new El(tag); created.push(el); return el; },
        },
        // 历史行那条道用 `hostEl instanceof HTMLElement` 挡非元素入参，桩里给出同一个类。
        HTMLElement: El,
        console,
        Math,
        Number,
        String,
        Object,
        Array,
        JSON,
        formatElapsedDuration: (value) => {
            const seconds = Math.max(0, Math.floor(Number(value) || 0));
            if (seconds >= 60) return `${Math.floor(seconds / 60)}分${seconds % 60}秒`;
            return `${seconds}秒`;
        },
        formatTokenCount: (value) => {
            const number = Number(value) || 0;
            return number >= 1000 ? `${(number / 1000).toFixed(1)}k` : String(number);
        },
        normalizeCeoTurnId: (value) => String(value || "").trim(),
        activeSessionId: () => "web:ceo-test",
        calls: { mutate: 0 },
    };
    sandbox.mutateCeoFeed = (fn) => { sandbox.calls.mutate += 1; return fn(); };
    sandbox.ensureActiveCeoTurn = (options = {}) => {
        sandbox.turn = sandbox.turn && sandbox.turn.source === (options.source || "user") ? sandbox.turn : null;
        if (!sandbox.turn) {
            const el = new El("div");
            el.className = "message system ceo-turn-message";
            const content = new El("div");
            content.className = "msg-content ceo-turn-content";
            const textEl = new El("div");
            textEl.className = "assistant-text pending";
            const meta = new El("div");
            meta.className = "ceo-turn-meta";
            content.appendChild(textEl);
            content.appendChild(meta);
            el.appendChild(content);
            sandbox.turn = { el, textEl, source: options.source || "user", turnId: options.turnId || "" };
        }
        return sandbox.turn;
    };
    vm.createContext(sandbox);
    vm.runInContext(NAMES.map(sliceFunction).join("\n\n"), sandbox);
    return { sandbox, created };
}

test("快照读数：空值与无信息值都不建行", () => {
    const { sandbox } = buildHarness();
    assert.equal(vm.runInContext("normalizeCeoSnapshotReasoning(null)", sandbox), null);
    assert.equal(vm.runInContext("normalizeCeoSnapshotReasoning({seconds:0,chars:0,tail_line:'   '})", sandbox), null);
    const ok = vm.runInContext("normalizeCeoSnapshotReasoning({attempt:2,seconds:12.4,chars:880,est_tokens:220,tail_line:'x'})", sandbox);
    // vm 里造的对象跨 realm，比较前先在本测试 realm 复制一份
    assert.deepEqual({ ...ok }, { attempt: 2, seconds: 12.4, chars: 880, est_tokens: 220, tail_line: "x" });
});

test("思考帧按 seq 追加，段号一变整段重来", () => {
    const { sandbox } = buildHarness();
    const apply = (payload) => vm.runInContext(
        `applyCeoReasoningDelta(${JSON.stringify(payload)}, { sessionId: "web:ceo-test" })`,
        sandbox,
    );
    assert.equal(apply({ turn_id: "t1", source: "user", attempt: 1, seq: 1, delta: "第一步：看要求\n", chars: 9, est_tokens: 3, seconds: 2 }), true);
    assert.equal(apply({ turn_id: "t1", source: "user", attempt: 1, seq: 2, delta: "第二步：查文件", chars: 15, est_tokens: 4, seconds: 3 }), true);
    assert.equal(sandbox.turn.reasoningText, "第一步：看要求\n第二步：查文件");
    // 迟到的旧帧不能把已追加的文本再叠一遍
    assert.equal(apply({ turn_id: "t1", source: "user", attempt: 1, seq: 2, delta: "重复", chars: 16, est_tokens: 4, seconds: 3 }), false);
    assert.equal(sandbox.turn.reasoningText, "第一步：看要求\n第二步：查文件");
    // 换发（同跳重发 / 换链降级）：段号变了就整段重来
    assert.equal(apply({ turn_id: "t1", source: "user", attempt: 2, seq: 1, delta: "重来", chars: 2, est_tokens: 1, seconds: 1 }), true);
    assert.equal(sandbox.turn.reasoningText, "重来");
    assert.equal(sandbox.turn.reasoningAttempt, 2);
});

test("折叠行只留最新一行，token 没真值时带 ~", () => {
    const { sandbox } = buildHarness();
    vm.runInContext(`applyCeoReasoningDelta({turn_id:"t1",source:"user",attempt:1,seq:1,delta:"第一行\\n第二行\\n第三行",chars:1200,est_tokens:300,seconds:75}, {sessionId:"web:ceo-test"})`, sandbox);
    const turn = sandbox.turn;
    const line = turn.thinkingLineEl;
    assert.ok(line, "没有建出思维链行");
    assert.equal(line.hidden, false);
    assert.equal(turn.thinkingTailEl.textContent, "第三行");
    assert.match(turn.thinkingMetaEl.textContent, /思考 1分15秒/);
    assert.match(turn.thinkingMetaEl.textContent, /~300 tok/);
    assert.equal(turn.thinkingPaneEl.textContent, "第一行\n第二行\n第三行");
    // 插入位置：正文之后、元信息之前
    const host = turn.textEl.parentElement;
    assert.equal(host.children[0], turn.textEl);
    assert.equal(host.children[1], line);
});

test("provider 真报了思考 token 才去掉波浪号", () => {
    const { sandbox } = buildHarness();
    const turn = { el: new El("div"), textEl: new El("div") };
    turn.textEl.parentElement = turn.el;
    turn.el.appendChild(turn.textEl);
    turn.reasoningText = "在想";
    turn.reasoningSeconds = 40;
    turn.reasoningChars = 2000;
    turn.reasoningEstTokens = 1300;
    turn.reasoningTokensReported = 1780;
    vm.runInContext("renderCeoThinkingLineIntoTurn(globalThis.__turn)", Object.assign(sandbox, { __turn: turn }));
    assert.doesNotMatch(turn.thinkingMetaEl.textContent, /~/);
    assert.match(turn.thinkingMetaEl.textContent, /1\.8k tok/);
    // 没有真值时同一份数据要带 ~
    const estimateOnly = { ...turn, thinkingLineEl: null, thinkingPaneEl: null, thinkingTailEl: null, thinkingHeadEl: null, reasoningTokensReported: 0 };
    vm.runInContext("renderCeoThinkingLineIntoTurn(globalThis.__turn2)", Object.assign(sandbox, { __turn2: estimateOnly }));
    assert.match(estimateOnly.thinkingMetaEl.textContent, /~1\.3k tok/);
});

test("重连只带快照读数时也能恢复这一行", () => {
    const { sandbox } = buildHarness();
    const turn = vm.runInContext("ensureActiveCeoTurn({source:'user',turnId:'t1'})", sandbox);
    vm.runInContext("applyCeoThinkingFromInflightSnapshot(globalThis.__turn, {attempt:3,seconds:12,chars:900,est_tokens:225,tail_line:'恢复的一行'})", Object.assign(sandbox, { __turn: turn }));
    assert.equal(turn.reasoningAttempt, 3);
    assert.equal(turn.thinkingTailEl.textContent, "恢复的一行");
    assert.equal(turn.thinkingLineEl.hidden, false);
    // 已经有更长文本时不被快照截短
    turn.reasoningText = "已经收到很长的思考内容";
    vm.runInContext("applyCeoThinkingFromInflightSnapshot(globalThis.__turn, {attempt:3,seconds:13,chars:990,est_tokens:248,tail_line:'恢复的一行'})", sandbox);
    assert.equal(turn.thinkingTailEl.textContent, "已经收到很长的思考内容".split("\n").pop());
});

test("快照里 reasoning 为空时把这一行收起", () => {
    const { sandbox } = buildHarness();
    const turn = vm.runInContext("ensureActiveCeoTurn({source:'user',turnId:'t1'})", sandbox);
    vm.runInContext("applyCeoReasoningDelta({turn_id:'t1',source:'user',attempt:1,seq:1,delta:'在想',chars:2,est_tokens:1,seconds:1}, {sessionId:'web:ceo-test'})", sandbox);
    assert.equal(turn.thinkingLineEl.hidden, false);
    vm.runInContext("applyCeoThinkingFromInflightSnapshot(globalThis.__turn, null)", Object.assign(sandbox, { __turn: turn }));
    assert.equal(turn.thinkingLineEl.hidden, true);
});

test("车道进口接线：帧名、快照恢复、历史汇总三处都接上", () => {
    // 上面那些用例是把函数单独喂进 vm 的；这条钉的是"帧真能走到函数"那段接线，
    // 断在哪一处，界面上就一个字都不会动。
    assert.match(APP_CODE, /payload\.type === "ceo\.reasoning\.delta"/);
    assert.match(APP_CODE, /payload\.type === "ceo\.reasoning\.delta"[\s\S]{0,120}?applyCeoReasoningDelta\(/);
    assert.match(APP_CODE, /applyCeoThinkingFromInflightSnapshot\(turn, snapshot\?\.reasoning\)/);
    assert.match(APP_CODE, /renderCeoThinkingSummaryFromSnapshot\([^;]{0,80}item\?\.thinking\)/);
    // 进行中快照的可见性判据要认思考读数，否则内部轮仍然只有一条静态转圈
    assert.match(APP_CODE, /function ceoInflightTurnHasVisibleAssistantState[\s\S]{0,900}?!!reasoning/);
    // 快照缓存条目要把有界读数带走，并重连时喂给这一行
    assert.match(APP_CODE, /if \(reasoning\) next\.reasoning = reasoning;/);
});

test("样式契约：flex 容器必须自己补 [hidden]，展开窗高度固定", () => {
    assert.match(CSS_CODE, /\.ceo-thinking-line\s*\{[^}]*display:\s*flex/);
    assert.match(CSS_CODE, /\.ceo-thinking-line\[hidden\]\s*\{\s*display:\s*none/);
    assert.match(CSS_CODE, /\.ceo-thinking-line-pane\s*\{[^}]*max-height:/);
    assert.match(CSS_CODE, /\.ceo-thinking-line-pane\s*\{[^}]*overflow-y:\s*auto/);
    assert.match(CSS_CODE, /\.ceo-thinking-line-tail\s*\{[^}]*text-overflow:\s*ellipsis/);
    // 可点判据用 role 而不是 class：历史行不带 role，就不会有可点错觉。
    assert.match(CSS_CODE, /\.ceo-thinking-line-head\[role="button"\]\s*\{[^}]*cursor:\s*pointer/);
});

test("点整行头展开、再点收起：没有独立按钮", () => {
    const { sandbox } = buildHarness();
    const turn = vm.runInContext("ensureActiveCeoTurn({source:'user',turnId:'t1'})", sandbox);
    vm.runInContext("applyCeoReasoningDelta({turn_id:'t1',source:'user',attempt:1,seq:1,delta:'在想第一件事\\n第二件事',chars:12,est_tokens:4,seconds:6}, {sessionId:'web:ceo-test'})", sandbox);
    const head = turn.thinkingHeadEl;
    const line = turn.thinkingLineEl;
    const pane = turn.thinkingPaneEl;
    assert.ok(head && line && pane, "行头或滚动窗没建出来");
    assert.equal(head.getAttribute("role"), "button");
    assert.equal(head.getAttribute("aria-expanded"), "false");
    assert.equal(pane.hidden, true);
    assert.equal(turn.thinkingLineEl.querySelector(".ceo-thinking-line-caret"), null);
    head.fire("click");
    assert.equal(head.getAttribute("aria-expanded"), "true");
    assert.equal(pane.hidden, false);
    assert.ok(line.classList.contains("is-expanded"));
    head.fire("click");
    assert.equal(head.getAttribute("aria-expanded"), "false");
    assert.equal(pane.hidden, true);
    assert.ok(!line.classList.contains("is-expanded"));
    // 键盘等价：整行头是 role=button 的 tabindex 停靠点
    head.fire("keydown", { key: "Enter", preventDefault() {} });
    assert.equal(head.getAttribute("aria-expanded"), "true");
    // 展开窗自己的点击不冒泡成开关：只在行头上挂了 handler
    assert.equal(pane.listeners.click, undefined);
});

test("历史行只给读数：不带可点形状", () => {
    const { sandbox } = buildHarness();
    const hostEl = new El("div");
    hostEl.className = "msg system";
    const textEl = new El("div");
    textEl.className = "assistant-text";
    hostEl.appendChild(textEl);
    vm.runInContext("renderCeoThinkingSummaryFromSnapshot(globalThis.__host, {seconds:40,chars:2000,est_tokens:1300})", Object.assign(sandbox, { __host: hostEl }));
    const line = hostEl.children[1];
    assert.ok(line, "历史汇总行没建出来");
    assert.ok(line.classList.contains("is-final"));
    const head = line.children[0];
    assert.equal(head.getAttribute("role"), null);
    assert.equal(head.listeners.click, undefined);
    assert.equal(line.children[1].hidden, true);
    assert.match(head.children[0].textContent, /~1\.3k tok/);
});
