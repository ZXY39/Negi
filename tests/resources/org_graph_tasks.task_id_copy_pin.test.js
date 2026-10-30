"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");

// 任务卡 ID 胶囊会 flex 撑到 220px，而 ID 文本有自己的最大宽度。复制图标必须用
// auto 外边距钉在胶囊右端，否则剩余空间落在图标右边，缩放一变图标就跟着左移。
const CSS = fs.readFileSync("g3ku/web/frontend/org_graph.css", "utf8").replace(/\r\n/g, "\n");

test("任务 ID 胶囊的复制图标固定在右端", () => {
    const rule = /\.pc-task-id-copy\s*\{([\s\S]*?)\n\}/.exec(CSS);
    assert.ok(rule, "缺少 .pc-task-id-copy 规则");
    assert.match(rule[1], /flex:\s*0 0 auto/, "复制图标必须不参与收缩");
    assert.match(rule[1], /margin-inline-start:\s*auto/, "复制图标没有钉在胶囊右端");

    const chip = /\.pc-task-id-chip\s*\{([\s\S]*?)\n\}/.exec(CSS);
    assert.ok(chip && /flex:\s*1 1 auto/.test(chip[1]), "胶囊的成长前提变了，需要重新核对这条回归");
});
