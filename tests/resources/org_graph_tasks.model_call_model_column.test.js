const test = require("node:test");
const assert = require("node:assert/strict");
const vm = require("node:vm");
const { readFrontendSources } = require("./support/frontend_sources.js");

// 模型调用明细的「模型」列只报这一发真正被服务（拿到 usage、call_count>0）的成员：
// 一次调用沿链试过 flash1 → flash2 → flash3 时，列里读成一次调用一颗模型，而不是
// 把整条尝试链摊开。旧账本（升级前每跳都记 call_count=1，或根本没有该键）没有这个
// 区分，退回整链展示，不吞历史事实。搜索与展示同口径：匹配的必须是列里看得见的模型。

const TASKS_CODE = readFrontendSources(["tasks"]).join("\n");

const SLICE_START = "function taskModelDisplayName";
const SLICE_END = "async function loadTaskDetail";

function makeContext() {
    const start = TASKS_CODE.indexOf(SLICE_START);
    const end = TASKS_CODE.indexOf(SLICE_END);
    assert.ok(start > 0 && end > start, "org_graph_tasks.js 明细车道函数段未找到");
    const context = {
        console,
        Number,
        String,
        Boolean,
        Array,
        Object,
        Math,
        Date,
        JSON,
        esc: (value) => String(value ?? ""),
        formatTokenCount: (value) => String(Number(value || 0)),
        formatDurationMs: (value) => (value === null ? "--" : `${value}ms`),
        modelCallHitRate: () => 0,
        // 模型目录不在这里加载：显示名回退成裸配置 key，正好让断言能看见链上的每一颗。
        ceoModelUsageHeadlineTitle: () => "",
        S: { taskModelCallPageLoading: false },
        U: {},
    };
    vm.createContext(context);
    vm.runInContext(TASKS_CODE.slice(start, end), context);
    return context;
}

function modelRow(modelKey, callCount, tokens = 0) {
    return {
        model_key: modelKey,
        provider_id: `${modelKey}-provider`,
        provider_model: `${modelKey}-model`,
        tracked: true,
        call_count: callCount,
        calls_with_usage: callCount ? 1 : 0,
        calls_without_usage: 0,
        is_partial: false,
        input_tokens: tokens,
        output_tokens: 0,
        cache_hit_tokens: 0,
    };
}

function makeCall(deltaUsageByModel) {
    return {
        call_index: 739,
        node_id: "node:d401fb116e61",
        created_at: "2026-11-12T16:38:49+08:00",
        call_kind: "",
        prepared_message_chars: 100,
        response_tool_call_count: 1,
        duration_ms: 10300,
        first_token_ms: 2100,
        thinking_tokens: 71,
        stream_incomplete: false,
        delta_usage: { input_tokens: 100, cache_hit_tokens: 900 },
        delta_usage_by_model: deltaUsageByModel,
    };
}

function renderSingle(call) {
    const context = makeContext();
    const meta = {
        items: [call],
        total: 1,
        grandTotal: 1,
        pageSize: 100,
        totalPages: 1,
        currentPage: 1,
        startIndex: 1,
        endIndex: 1,
        historical: false,
    };
    return { context, html: context.renderTaskTokenCallTableMarkup(meta, [call], "") };
}

test("模型列只显示真正被服务的成员，不摊开尝试链", () => {
    const { html } = renderSingle(makeCall([
        modelRow("flash1", 0),
        modelRow("flash2", 0),
        modelRow("flash3", 1, 100),
    ]));

    assert.match(html, /class="task-token-call-model" title="flash3">flash3<\/td>/);
    assert.doesNotMatch(html, /flash1|flash2/, "未中选的尝试成员不得出现在模型列与悬停 key 里");
});

test("同一颗成员重试后命中，只显示一次", () => {
    const { html } = renderSingle(makeCall([
        modelRow("flash1", 0),
        modelRow("flash1", 0),
        modelRow("flash1", 1, 100),
    ]));

    assert.doesNotMatch(html, /flash1, flash1/);
    assert.match(html, /title="flash1">flash1<\/td>/);
});

test("旧账本没有服务行标记时退回整链展示", () => {
    const legacy = makeCall([modelRow("flash1", 1), modelRow("flash2", 1)]);
    legacy.delta_usage_by_model.forEach((row) => { delete row.call_count; });
    const { html } = renderSingle(legacy);
    assert.match(html, /title="flash1, flash2">flash1, flash2<\/td>/);

    // 升级前的账本每跳都记 call_count=1，同样退回整链。
    const upgraded = renderSingle(makeCall([modelRow("flash1", 1), modelRow("flash2", 1)]));
    assert.match(upgraded.html, /title="flash1, flash2">flash1, flash2<\/td>/);
});

test("搜索跟着模型列走：链上未中选的成员不再是匹配词", () => {
    const context = makeContext();
    const call = makeCall([
        modelRow("flash1", 0),
        modelRow("flash2", 0),
        modelRow("flash3", 1, 100),
    ]);

    assert.equal(context.taskModelCallMatchesQuery(call, "flash3"), true);
    assert.equal(context.taskModelCallMatchesQuery(call, "flash1"), false, "隐藏的尝试成员不应把行搜出来");
    assert.equal(context.taskModelCallMatchesQuery(call, "739"), true);
});
