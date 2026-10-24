"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const FRONTEND = path.join(__dirname, "..", "..", "g3ku", "web", "frontend", "tasks");
const STORE_CODE = fs.readFileSync(path.join(FRONTEND, "node-detail-store.js"), "utf8");
const DETAIL_CODE = fs.readFileSync(path.join(FRONTEND, "node-detail.js"), "utf8");

function load() {
    const context = {
        console,
        normalizeInt: (value, fallback = 0) => {
            const parsed = Number(value);
            return Number.isFinite(parsed) ? Math.trunc(parsed) : Math.trunc(fallback);
        },
        esc: (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({
            "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
        }[char])),
        formatCompactTime: () => "10:00",
        readableText: (value) => String(value ?? ""),
        repairAcceptanceTitle: (value) => String(value ?? ""),
        isTerminalTreeNodeStatus: () => false,
        taskPauseDisplayActive: () => false,
        ceoContextLoaderKind: () => "",
        ceoContextLoadNoticeIconName: () => "",
        normalizeUploadList: (items) => (Array.isArray(items) ? items : []),
        document: { querySelector: () => null, querySelectorAll: () => [] },
    };
    vm.createContext(context);
    vm.runInContext(
        `${STORE_CODE}\n${DETAIL_CODE}\nthis.__turns = { buildExecutionTraceSteps, buildNodeExecutionTrace, buildTraceRenderSignature, renderTraceStep };`,
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
    const { buildExecutionTraceSteps } = load();
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
    assert.equal(steps[1].title, "开始执行");
    assert.equal(steps[2].traceKey, "turn:2");
    assert.equal(steps[2].title, "第一批完成");
    assert.match(steps[2].bodyHtml, /tool-b/);
    assert.match(steps[2].bodyHtml, /tool-c/);
    assert.match(steps[2].bodyHtml, /tool-d/);
    assert.doesNotMatch(steps[2].bodyHtml, /tool-a/);
    assert.equal((steps[2].bodyHtml.match(/task-trace-round-group/g) || []).length, 2);
    assert.equal(steps[3].traceKey, "turn:4");
    assert.equal(steps[3].textOnly, true);
    assert.equal(steps[3].text, "纯文本进展");
    assert.equal(steps[4].traceKey, "turn:5");
    assert.match(steps[4].bodyHtml, /submit_final_result/);
    assert.equal(steps[4].open, true);
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
    assert.doesNotMatch(html, /task-trace-turn-text/);
    assert.doesNotMatch(html, /interaction-step-status/);
    assert.ok(html.includes(longText));
    assert.match(html, /title="[^"]*最后一句要求/);
    assert.match(html, /tool-b/);

    // 展开态的固定高度滚动框就是标题自身，且不铺底色。
    const css = fs.readFileSync(path.join(FRONTEND, "..", "org_graph.css"), "utf8");
    assert.match(
        css,
        /\.task-trace-step--turn-text\[open\] \.interaction-step-title \{[^}]*max-height:\s*180px[^}]*overflow-y:\s*auto[^}]*background:\s*transparent/
    );
    assert.doesNotMatch(css, /\.task-trace-turn-text\s*\{/);
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
