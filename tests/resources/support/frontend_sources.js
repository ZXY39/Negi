"use strict";

const fs = require("node:fs");
const path = require("node:path");

const FRONTEND_DIR = path.resolve(__dirname, "..", "..", "..", "g3ku", "web", "frontend");

// Bundle 顺序镜像 org_graph.html 的 <script src> 顺序；bundle 允许子集，
// 测试只加载自己实际需要的脚本。拆出模块时把它加进所属 bundle（按页面顺序），
// 不要为了省事把有顶层副作用的脚本塞进别的 bundle。
const FRONTEND_BUNDLES = Object.freeze({
    vendor: ["vendor/lucide.min.js"],
    api_client: [
        "api/core.js",
        "api/bootstrap.js",
        "api/ceo.js",
        "api/tasks.js",
        "api/models.js",
        "api/memory.js",
        "api/audit.js",
        "api/resources.js",
        "api/external.js",
        "api_client.js",
    ],
    bootstrap: ["org_graph_bootstrap.js"],
    task_view: ["org_graph_task_view.js"],
    app: ["ui/toast.js", "ui/confirm.js", "ceo/compression.js", "org_graph_app.js"],
    tasks: ["org_graph_tasks.js"],
    resources: ["org_graph_resources.js"],
    governance: ["org_graph_ceo_governance.js"],
    llm: ["org_graph_llm.js"],
    external: ["org_graph_external.js"],
});

function resolveFrontendSource(name) {
    // 基线对照测试用 ORG_GRAPH_APP 指向未修改的副本；loader 保持这条入口。
    if (name === "org_graph_app.js" && process.env.ORG_GRAPH_APP) {
        return path.resolve(process.env.ORG_GRAPH_APP);
    }
    return path.join(FRONTEND_DIR, name);
}

function readFrontendSources(bundleNames) {
    const names = Array.isArray(bundleNames) ? bundleNames : [bundleNames];
    const files = [];
    for (const bundleName of names) {
        const bundle = FRONTEND_BUNDLES[bundleName];
        if (!bundle) throw new Error(`Unknown frontend bundle: ${bundleName}`);
        files.push(...bundle);
    }
    return files.map((name) => fs.readFileSync(resolveFrontendSource(name), "utf8"));
}

module.exports = {
    FRONTEND_BUNDLES,
    FRONTEND_DIR,
    readFrontendSources,
    resolveFrontendSource,
};
