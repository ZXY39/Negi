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
        "client_api/core.js",
        "client_api/bootstrap.js",
        "client_api/ceo.js",
        "client_api/tasks.js",
        "client_api/models.js",
        "client_api/memory.js",
        "client_api/persona.js",
        "client_api/audit.js",
        "client_api/resources.js",
        "client_api/external.js",
        "api_client.js",
    ],
    bootstrap: ["org_graph_bootstrap.js"],
    task_view: ["tasks/tree-store.js", "tasks/tree-view.js", "tasks/node-detail-store.js", "tasks/node-detail.js", "org_graph_task_view.js"],
    app: [
        "ui/toast.js",
        "ui/confirm.js",
        "ui/drawer.js",
        "ui/media-viewer.js",
        "ceo/model-panel.js",
        "ceo/composer.js",
        "ceo/compression.js",
        "ceo/feed-model.js",
        "ceo/feed-view.js",
        "ceo/sessions-panel.js",
        "ceo/session-store.js",
        "ceo/ws-router.js",
        "memory/store.js",
        "memory/actions.js",
        "memory/view.js",
        "models/role-store.js",
        "models/role-view.js",
        "models/actions.js",
        "resources/store.js",
        "org_graph_app.js",
    ],
    tasks: ["tasks/hall-store.js", "tasks/hall-view.js", "tasks/ws-router.js", "org_graph_tasks.js"],
    resources: ["org_graph_resources.js"],
    governance: ["org_graph_ceo_governance.js"],
    llm: ["org_graph_llm.js"],
    onboarding: ["org_graph_onboarding.js"],
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
