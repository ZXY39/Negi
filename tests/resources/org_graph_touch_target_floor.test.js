"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");

// 触摸档合同：pointer:coarse 下控件目标 ≥44×44（WCAG 2.5.5），压过两档高度；
// 徽标与纯标签不是控件，不得被抬成 44px 的块。
const CSS = fs.readFileSync("g3ku/web/frontend/org_graph.css", "utf8").replace(/\r\n/g, "\n");

function coarseBlock() {
    const match = /@media \(pointer: coarse\) \{([\s\S]*?)\n\}/.exec(CSS);
    assert.ok(match, "缺少 pointer:coarse 触摸档");
    return match[1];
}

test("粗指针下控件有 44px 下限且能压过组件层 min-height", () => {
    const block = coarseBlock();
    assert.match(block, /min-block-size: 44px !important/);
    assert.match(block, /min-inline-size: 44px !important/);
    for (const selector of ["button", "select", "textarea", "[role=\"button\"]", "a.toolbar-btn"]) {
        assert.ok(block.includes(selector), `触摸档名单漏了 ${selector}`);
    }
    assert.match(block, /label:has\(\[type="checkbox"\]\)/);
    assert.match(block, /label:has\(\[type="radio"\]\)/);
    assert.match(block, /input:not\(\[type="checkbox"\], \[type="radio"\], \[type="hidden"\]\)/);
});

test("触摸档不抬徽标与纯标签", () => {
    const block = coarseBlock();
    assert.ok(!block.includes(".status-badge"), "状态徽标被抬成触摸块");
    assert.ok(!block.includes(".memory-op-chip"), "记忆操作 chip 被抬成触摸块");
    assert.ok(!block.includes(".task-trace-round-chip"), "轮次 chip 被抬成触摸块");
});

test("桌面档不受触摸档影响：44px 下限只活在 coarse 媒体块里", () => {
    const outside = CSS.replace(/@media \(pointer: coarse\) \{[\s\S]*?\n\}/g, "");
    assert.ok(!outside.includes("min-block-size: 44px"), "44px 下限漏到了桌面档");
});