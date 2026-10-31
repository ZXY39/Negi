"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

// 「申请入口」是外链：点一下在浏览器新标签页打开 QQ 开放平台。
// 两条契约：入口本身必须是指向平台的 `target="_blank"` 锚点；应用内不再承载第三方页面的窗口
// （旧实现是 detail-drawer + iframe，把 QQ 登录页嵌进应用里）。
const ROOT = path.resolve(__dirname, "..", "..");
const FRONTEND = path.join(ROOT, "g3ku", "web", "frontend");
const APPLY_URL = "https://q.qq.com/qqbot/openclaw/login.html";

function readFrontend(name) {
    return fs.readFileSync(path.join(FRONTEND, name), "utf8");
}

test("申请入口是指向 QQ 开放平台的新标签页锚点", () => {
    const html = readFrontend("org_graph.html");
    const tag = html.match(/<a\b[^>]*href="https:\/\/q\.qq\.com\/qqbot\/openclaw\/login\.html"[^>]*>申请入口<\/a>/)?.[0];
    assert.ok(tag, `没有指向 ${APPLY_URL} 的「申请入口」锚点`);
    assert.match(tag, /\btarget="_blank"/);
    assert.match(tag, /\brel="[^"]*noopener/);
});

test("应用里不再有承载申请页的内置窗口", () => {
    const marker = /qq[-_]?bot[-_]?apply/i;
    for (const file of ["org_graph.html", "org_graph_external.js", "org_graph.css"]) {
        assert.ok(!marker.test(readFrontend(file)), `${file} 里还留着内置申请窗口的标记`);
    }
});
