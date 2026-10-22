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
        `${STORE_CODE}\n${DETAIL_CODE}\nthis.__turns = { buildExecutionTraceSteps, buildNodeExecutionTrace, buildTraceRenderSignature };`,
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
    assert.equal(steps[3].traceKey, "turn:4");
    assert.equal(steps[3].textOnly, true);
    assert.equal(steps[3].text, "纯文本进展");
    assert.equal(steps[4].traceKey, "turn:5");
    assert.match(steps[4].bodyHtml, /submit_final_result/);
    assert.equal(steps[4].open, true);
    assert.equal(steps[2].open, false);
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
