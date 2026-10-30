"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const FRONTEND = path.join(__dirname, "..", "..", "g3ku", "web", "frontend", "tasks");
const STORE_CODE = fs.readFileSync(path.join(FRONTEND, "node-detail-store.js"), "utf8");
const DETAIL_CODE = fs.readFileSync(path.join(FRONTEND, "node-detail.js"), "utf8");
// CONTEXT_LOAD_TITLES / CONTEXT_LOAD_HIDDEN_STATUSES 住在前门那一支 bundle，按真页面
// 的加载顺序一起读进来，标题断言才是在证生产映射而不是测试自己抄的一份。
const VIEW_CODE = fs.readFileSync(path.join(FRONTEND, "..", "org_graph_task_view.js"), "utf8");
// 思考文案的唯一产出者住在前门那一支 bundle 里；这里按真页面的加载顺序把它读进同一个
// 沙箱，用例才是在证"节点页没有自己抄一份文案"。
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

function load({ contextLoaderKind = () => "" } = {}) {
    const context = {
        console,
        normalizeInt: (value, fallback = 0) => {
            const parsed = Number(value);
            return Number.isFinite(parsed) ? Math.trunc(parsed) : Math.trunc(fallback);
        },
        esc: (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({
            "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
        }[char])),
        renderMarkdown: (value) => String(value ?? "").replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>"),
        formatCompactTime: () => "10:00",
        readableText: (value) => String(value ?? ""),
        repairAcceptanceTitle: (value) => String(value ?? ""),
        isTerminalTreeNodeStatus: () => false,
        taskPauseDisplayActive: () => false,
        ceoContextLoaderKind: contextLoaderKind,
        ceoContextLoadNoticeIconName: () => "",
        normalizeUploadList: (items) => (Array.isArray(items) ? items : []),
        document: { querySelector: () => null, querySelectorAll: () => [] },
    };
    vm.createContext(context);
    vm.runInContext(
        [
            sliceAppFunction("formatElapsedDuration"),
            sliceAppFunction("formatTokenCount"),
            sliceAppFunction("thinkingReadingParts"),
            STORE_CODE,
            DETAIL_CODE,
            VIEW_CODE,
            "this.__turns = { buildExecutionTraceSteps, buildNodeExecutionTrace, buildTraceRenderSignature, renderTraceStep, taskRoundThinkingText, contextLoadDisplayFor };",
        ].join("\n"),
        context
    );
    return context.__turns;
}

function turn(seq, text, tools = []) {
    return {
        seq,
        text,
        text_ref: "",
        created_at: `2026-10-22T10:0${seq}:00+08:00`,
        status: "completed",
        tools: tools.map((name) => ({
            tool_call_id: `call-${name}`,
            tool_name: name,
            status: "success",
            arguments_text: "{}",
            output_text: "ok",
            output_ref: "",
            started_at: "",
            finished_at: "",
            elapsed_seconds: 1,
        })),
    };
}

test("turns group by progress text and keep the tools that follow", () => {
    const { buildExecutionTraceSteps, renderTraceStep } = load();
    const trace = {
        initial_prompt: "initial",
        turns: [
            turn(1, "", ["tool-a"]),
            turn(2, "第一批完成", ["tool-b", "tool-c"]),
            turn(3, "", ["tool-d"]),
            turn(4, "纯文本进展", []),
            turn(5, "最终检查", ["submit_final_result"]),
        ],
        tool_steps: [],
        stages: [],
    };

    const steps = buildExecutionTraceSteps(trace, {});

    assert.equal(steps.length, 5);
    assert.equal(steps[0].traceKey, "initial_prompt");
    assert.equal(steps[1].traceKey, "turn:preamble");
    // 首个叙述前的工具轮直接铺开，不再自动加「开始执行」标题。
    assert.equal(steps[1].title, "");
    assert.equal(steps[1].bare, true);
    const preambleHtml = renderTraceStep(steps[1]);
    assert.match(preambleHtml, /task-trace-step--bare/);
    assert.doesNotMatch(preambleHtml, /开始执行/);
    assert.doesNotMatch(preambleHtml, /task-trace-summary/);
    assert.equal(steps[2].traceKey, "turn:2");
    assert.equal(steps[2].title, "第一批完成");
    assert.match(steps[2].bodyHtml, /tool-b/);
    assert.match(steps[2].bodyHtml, /tool-c/);
    assert.match(steps[2].bodyHtml, /tool-d/);
    assert.doesNotMatch(steps[2].bodyHtml, /tool-a/);
    assert.equal((steps[2].bodyHtml.match(/task-trace-round-group/g) || []).length, 2);
    assert.equal(steps[3].traceKey, "turn:4");
    assert.equal(steps[3].textOnly, true);
    // 纯文本 turn 也走同一条「一行 + > 展开」的车道。
    assert.equal(steps[3].title, "纯文本进展");
    assert.equal(steps[3].turnText, "纯文本进展");
    assert.equal(steps[4].traceKey, "turn:5");
    assert.match(steps[4].bodyHtml, /submit_final_result/);
    // 末组带中途正文（「最终检查」）也默认收起：摘要一行，点击才展开全文。
    assert.equal(steps[4].open, false);
    assert.equal(steps[2].open, false);
});

test("turn text expands in place without a duplicate block or a status badge", () => {
    const { buildExecutionTraceSteps, renderTraceStep } = load();
    const longText = `${"模型中途输出".repeat(20)}——最后一句要求：必须把四个角色全部接好。`;
    const trace = {
        initial_prompt: "initial",
        turns: [turn(2, longText, ["tool-b"])],
        tool_steps: [],
        stages: [],
    };

    const steps = buildExecutionTraceSteps(trace, {});
    const turnStep = steps.find((step) => step.traceKey === "turn:2");
    // 不再按 80 字硬截断：标题与正文都是完整原文。
    assert.equal(turnStep.title, longText);
    assert.equal(turnStep.turnText, longText);
    assert.doesNotMatch(turnStep.title, /\.\.\.$/);

    const html = renderTraceStep({ ...turnStep, open: false });
    // 展开只是原文本换形态：体内不再重复一份正文，右侧也不再挂成功/失败状态。
    assert.match(html, /task-trace-step--turn-text/);
    assert.match(html, /task-trace-turn-toggle/);
    assert.doesNotMatch(html, /task-trace-turn-text/);
    assert.doesNotMatch(html, /interaction-step-status/);
    assert.ok(html.includes(longText));
    assert.match(html, /title="[^"]*最后一句要求/);
    assert.match(html, /tool-b/);

    // 展开态的固定高度滚动框就是标题自身，且不铺底色。
    const css = fs.readFileSync(path.join(FRONTEND, "..", "org_graph.css"), "utf8");
    assert.match(
        css,
        /\.task-trace-step--turn-text\[open\] \.interaction-step-title \{[^}]*max-height:\s*calc\(16\.5 \* 1\.6em\)[^}]*overflow-y:\s*auto[^}]*background:\s*transparent/
    );
    assert.doesNotMatch(css, /\.task-trace-turn-text\s*\{/);
});

test("expanded turn text renders markdown on the same scroll box", () => {
    const { buildExecutionTraceSteps, renderTraceStep } = load();
    const text = "先确认：**饭就是 Token**，再看下一段。";
    const steps = buildExecutionTraceSteps({
        initial_prompt: "initial",
        turns: [turn(2, text, ["tool-b"])],
        tool_steps: [],
        stages: [],
    }, {});
    const step = steps.find((item) => item.traceKey === "turn:2");
    const html = renderTraceStep({ ...step, open: true });

    // 折叠行仍是原始文本，展开块才是渲染结果；两份都挂在同一个标题滚动框里。
    assert.match(html, /task-trace-turn-plain/);
    assert.match(html, /task-trace-turn-md markdown-content/);
    assert.match(html, /<strong>饭就是 Token<\/strong>/);

    const css = fs.readFileSync(path.join(FRONTEND, "..", "org_graph.css"), "utf8");
    assert.match(css, /\.task-trace-step--turn-text\[open\] \.task-trace-turn-plain \{[^}]*display:\s*none/);
    assert.match(css, /\.task-trace-step--turn-text\[open\] \.task-trace-turn-md \{[^}]*display:\s*flex/);
});

test("memory tools carry their own loader title instead of 加载内容", () => {
    const { contextLoadDisplayFor } = load({
        contextLoaderKind: (name) => {
            if (name === "memory_update") return "memory";
            if (name === "memory_note") return "note";
            return "";
        },
    });

    const memory = contextLoadDisplayFor({ tool_name: "memory_update", status: "success", output_text: '{"ok":true}' });
    assert.equal(memory.title, "更新记忆");
    assert.equal(memory.kind, "memory");

    const note = contextLoadDisplayFor({ tool_name: "memory_note", status: "success", output_text: '{"ok":true}' });
    assert.equal(note.title, "加载笔记");
});

test("textless rounds stay separate sections instead of one merged strip", () => {
    const { buildExecutionTraceSteps } = load();
    const trace = {
        initial_prompt: "initial",
        turns: [turn(1, "", ["tool-a"]), turn(2, "", ["tool-b"]), turn(3, "", ["tool-c", "tool-d"])],
        tool_steps: [],
        stages: [],
    };

    const steps = buildExecutionTraceSteps(trace, {});

    assert.equal(steps.length, 2);
    assert.equal(steps[1].traceKey, "turn:preamble");
    assert.equal((steps[1].bodyHtml.match(/task-trace-round-group/g) || []).length, 3);
    assert.equal((steps[1].bodyHtml.match(/task-trace-round-tools/g) || []).length, 3);
});

test("buildNodeExecutionTrace keeps turns from full trace and summary", () => {
    const { buildNodeExecutionTrace } = load();
    const turns = [turn(1, "进展", ["tool-a"]), turn(2, "收尾", ["submit_final_result"])];
    const full = buildNodeExecutionTrace(
        { node_id: "node:1" },
        { execution_trace: { initial_prompt: "p", turns, tool_steps: [], stages: [] } }
    );
    const summary = buildNodeExecutionTrace(
        { node_id: "node:1" },
        { execution_trace_summary: { turns, stages: [] } }
    );

    assert.deepEqual(full.turns.map((item) => item.seq), [1, 2]);
    assert.deepEqual(summary.turns.map((item) => item.seq), [1, 2]);
    assert.equal(summary.turns[0].tools[0].tool_name, "tool-a");
});

test("render signature changes when progress text changes", () => {
    const { buildTraceRenderSignature } = load();
    const first = buildTraceRenderSignature([{ traceKey: "turn:1", title: "a", textOnly: true, text: "a" }]);
    const second = buildTraceRenderSignature([{ traceKey: "turn:1", title: "a", textOnly: true, text: "b" }]);
    assert.notEqual(first, second);
});

test("每轮块底部带这一轮的思考读数，文案取自前门那一份口径", () => {
    const { buildNodeExecutionTrace, buildExecutionTraceSteps, taskRoundThinkingText } = load();
    const turns = [
        { ...turn(1, "", ["tool-a"]), thinking: { seconds: 75, chars: 5200, est_tokens: 1300 } },
        { ...turn(2, "", ["tool-b"]), thinking: { chars: 40, tokens: 30 } },
        // 只有非读数键（写侧多塞的正文）：整行不画，正文也不许出现在详情里。
        { ...turn(3, "", ["tool-c"]), thinking: { reasoning_content: "思考全文不该出现在这里" } },
        { ...turn(4, "", ["tool-d"]) },
    ];
    const trace = buildNodeExecutionTrace(
        { node_id: "node:thinking" },
        { execution_trace: { initial_prompt: "p", turns, tool_steps: [] } }
    );
    const body = buildExecutionTraceSteps(trace, { node_id: "node:thinking" })
        .map((step) => step.bodyHtml || "")
        .join("");

    assert.match(body, /task-trace-round-thinking">思考 1分15秒 · ~1,300 tok</);
    // provider 报过真值就不带 `~`，且这一轮没思考时长时不印"思考 0秒"。
    assert.match(body, /task-trace-round-thinking">30 tok</);
    assert.equal((body.match(/task-trace-round-thinking/g) || []).length, 2);
    assert.ok(!body.includes("思考全文不该出现在这里"));
    // 没上报的那一轮连空 div 都不留，两条读路都在同一处判空。
    assert.equal(taskRoundThinkingText(null), "");
    assert.equal(taskRoundThinkingText({}), "");
});
