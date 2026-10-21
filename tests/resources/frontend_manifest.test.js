"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const {
    FRONTEND_BUNDLES,
    FRONTEND_DIR,
    readFrontendSources,
} = require("./support/frontend_sources.js");

const ROOT = path.resolve(__dirname, "..", "..");
const HTML_PATH = path.join(ROOT, "g3ku", "web", "frontend", "org_graph.html");

function htmlScriptOrder() {
    const html = fs.readFileSync(HTML_PATH, "utf8");
    return [...html.matchAll(/<script\s+src="([^"]+)"/g)].map((match) => match[1]);
}

test("每个页面脚本都在某个 bundle 里声明，且每个声明都真实存在于页面", () => {
    const order = htmlScriptOrder();
    const declared = new Map();
    for (const [bundleName, files] of Object.entries(FRONTEND_BUNDLES)) {
        for (const file of files) {
            assert.ok(!declared.has(file), `${file} 同时出现在 ${declared.get(file)} 与 ${bundleName}`);
            declared.set(file, bundleName);
        }
    }
    assert.deepEqual(order.filter((src) => !declared.has(src)), []);
    assert.deepEqual([...declared.keys()].filter((src) => !order.includes(src)), []);
});

test("bundle 内顺序与页面加载顺序一致", () => {
    const order = htmlScriptOrder();
    for (const [bundleName, files] of Object.entries(FRONTEND_BUNDLES)) {
        const indexes = files.map((file) => order.indexOf(file));
        const sorted = [...indexes].sort((a, b) => a - b);
        assert.deepEqual(indexes, sorted, `${bundleName} bundle 与页面顺序不一致`);
    }
});

test("readFrontendSources 按 bundle 参数顺序拼接，且保持子集语义", () => {
    const appOnly = readFrontendSources(["app"]);
    assert.equal(appOnly.length, FRONTEND_BUNDLES.app.length);
    const pair = readFrontendSources(["task_view", "app"]);
    assert.equal(pair.length, FRONTEND_BUNDLES.task_view.length + FRONTEND_BUNDLES.app.length);
    assert.match(pair[0], /function|const/);
    assert.throws(() => readFrontendSources(["nope"]), /Unknown frontend bundle/);
});

test("bundle 中声明的每个文件都存在于磁盘", () => {
    for (const files of Object.values(FRONTEND_BUNDLES)) {
        for (const file of files) {
            assert.ok(fs.existsSync(path.join(FRONTEND_DIR, file)), `${file} 不存在`);
        }
    }
});
