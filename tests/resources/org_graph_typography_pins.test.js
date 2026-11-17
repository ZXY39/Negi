"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");

// 排版合同（第三轮 T1–T3）：正文 pretty / 标题 balance 只挂散文与短标题，不挂 *；
// 活数字容器等宽；散文 break-word、窄块/代码串保留 anywhere。
const CSS = fs.readFileSync("g3ku/web/frontend/org_graph.css", "utf8").replace(/\r\n/g, "\n");

function ruleBody(firstLine) {
    const escaped = firstLine.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    const head = firstLine.trim().endsWith("{") ? escaped : escaped + "[^{]*\\{";
    const match = new RegExp(`(^|\\n)${head}([\\s\\S]*?)\\n\\}`).exec(CSS);
    assert.ok(match, `缺少规则 ${firstLine}`);
    return match[2];
}

test("markdown 标题 balance、正文 pretty", () => {
    const heads = ruleBody(".markdown-content h1,");
    assert.match(heads, /text-wrap: balance/);
    const prose = ruleBody(".markdown-content p,");
    assert.match(prose, /text-wrap: pretty/);
});

test("阅读面散文挂 pretty 且不挂 *", () => {
    for (const selector of [
        ".media-viewer-markdown p,",
        ".memory-note-preview-body,",
        ".task-trace-message-body,",
        ".spawn-node-text,",
        ".interaction-step-detail,",
        ".ceo-thinking-line-pane {",
    ]) {
        assert.ok(CSS.includes(selector), `pretty 名单漏了 ${selector}`);
    }
    const prettyBlock = /\/\* 长正文消孤行[\s\S]*?\n\}/.exec(CSS);
    assert.ok(prettyBlock && prettyBlock[0].includes("text-wrap: pretty"));
    assert.ok(!/\*\s*\{[^}]*text-wrap/.test(CSS), "text-wrap 挂到了 * 上");
});

test("活数字容器等宽数字", () => {
    for (const selector of [
        ".ceo-turn-usage {",
        ".msg-meta {",
        ".pc-metric {",
        ".task-performance-bar {",
        ".pc-token-stats {",
        ".task-token-call-table {",
    ]) {
        assert.match(ruleBody(selector), /font-variant-numeric: tabular-nums/, `${selector} 缺等宽数字`);
    }
});

test("散文 break-word，窄块与代码串保留 anywhere", () => {
    for (const selector of [
        ".media-viewer-markdown {",
        ".task-trace-step--turn-text[open] .interaction-step-title {",
        ".ceo-follow-up-chip.is-expanded .ceo-follow-up-name {",
        ".memory-note-preview-body {",
        ".task-trace-message-body {",
        ".spawn-node-text {",
    ]) {
        const body = ruleBody(selector);
        assert.match(body, /overflow-wrap: break-word/, `${selector} 散文仍 anywhere`);
        assert.ok(!body.includes("overflow-wrap: anywhere"), `${selector} 残留 anywhere`);
    }
    for (const selector of [
        ".ceo-risk-tool-name {",
        ".ceo-risk-tool-warning {",
        ".task-tree-search-item-goal {",
    ]) {
        assert.match(ruleBody(selector), /overflow-wrap: anywhere/, `${selector} 窄块误改`);
    }
});