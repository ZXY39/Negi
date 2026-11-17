"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");

// WCAG 1.4.12 文本间距：含文本的控件用 min-height 而非固定 height，用户加大行高/字距/段距时不裁字。
// 纯图标方块与 legacy 层钉住值保留固定 height；composer 底留白由观察器按实测 band 单写。
const CSS = fs.readFileSync("g3ku/web/frontend/org_graph.css", "utf8").replace(/\r\n/g, "\n");
const COMPOSER = fs.readFileSync("g3ku/web/frontend/ceo/composer.js", "utf8").replace(/\r\n/g, "\n");

function ruleBody(firstLine) {
    const escaped = firstLine.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    const head = firstLine.trim().endsWith("{") ? escaped : escaped + "[^{]*\\{";
    const match = new RegExp(`(^|\\n)${head}([\\s\\S]*?)\\n\\}`).exec(CSS);
    assert.ok(match, `缺少规则 ${firstLine}`);
    return match[2];
}

test("文本控件档只钉 min-height，不钉固定 height", () => {
    for (const firstLine of [
        ":root .toolbar-btn {",
        ":root .toolbar-btn.small {",
        ":root select {",
        ".resource-select-trigger {",
        ".btn-primary {",
        ".msg-action-btn {",
        ".pc-topbar .status-badge {",
        ".pc-topbar .pc-task-id-chip {",
        ".resource-pagination .resource-pagination-group select {",
        ".task-detail-header .status-badge.status-badge {",
        ".memory-layout .memory-footer .toolbar-btn {",
    ]) {
        const body = ruleBody(firstLine);
        assert.match(body, /min-height: \d+px/, `${firstLine} 缺 min-height`);
        assert.ok(!/(^|\n)\s*height: \d+px;/.test(body), `${firstLine} 仍钉固定 height`);
    }
});

test("纯图标方块保留固定 height", () => {
    for (const firstLine of ["#ceo-risk-btn {", ".pc-card-menu-trigger {", ".pc-topbar .pc-card-menu-trigger.pc-card-menu-trigger {"]) {
        assert.match(ruleBody(firstLine), /(^|\n)\s*height: \d+px;/, `${firstLine} 图标方块失去固定尺寸`);
    }
});

test("composer 底留白走实测 band 变量，52px 仅作 fallback", () => {
    const occurrences = CSS.match(/padding: 10px var\(--ui-space-3\) var\(--ceo-input-band-pad, 52px\);/g) || [];
    assert.equal(occurrences.length, 2, "textarea 与 #ceo-input 两条 padding 都要走变量");
    assert.ok(!CSS.includes("padding: 10px var(--ui-space-3) 52px;"), "残留写死的 52px 底留白");
    assert.match(COMPOSER, /--ceo-input-band-pad/, "观察器没有写 band 变量");
    assert.match(COMPOSER, /controls\.forEach\(\(el\) => observer\.observe\(el\)\)/, "观察器没挂绝对定位控件");
});