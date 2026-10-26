const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");

// 勾选框样式台账：前端每个 `type="checkbox"` 落点都必须落进下面三档之一
//   ui         —— 套通用自绘组件（org_graph.css 的 .ui-checkbox-input + .ui-checkbox-box）
//   specialist —— 面板自己的自绘规则（会话批量、图像多模态、QQ 沙箱）
//   hidden     —— 按设计不画盒子（角色药丸把 input 藏掉）或纯隐藏字段
// 新加勾选框没登记进任何一档 = 界面上会冒出一颗系统默认方框，这条用例负责拦住。
// 「保留原生盒子只染 accent-color」这一档已经清空：那四颗（语音自动发送、记忆页三处）
// 都换成了通用组件，样式面只剩一套自绘盒子可讲。

// 勾选框可能出现在任何前端脚本里；按目录遍历，新增模块不需要再登记一次。
// 布局对比稿（model-layout-*）不在装载清单里、永远不会被渲染，跳过它们，
// 否则每份草稿都逼台账登记一颗界面上不存在的勾选框。
const FRONTEND_DIR = "g3ku/web/frontend";
function collectFrontendFiles(dir) {
    return fs.readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
        const path = `${dir}/${entry.name}`;
        if (entry.isDirectory()) {
            return entry.name === "vendor" ? [] : collectFrontendFiles(path);
        }
        if (!/\.(js|html)$/.test(entry.name)) return [];
        return /^model-layout-/.test(entry.name) ? [] : [path];
    });
}
const FILES = collectFrontendFiles(FRONTEND_DIR);

// 每个落点的稳定标记（不按行号，行号会漂）。
const LEDGER = [
    { token: "boot-legacy-confirm", tier: "ui" },
    { token: "boot-remember-unlock", tier: "ui" },
    { token: "project-settings-auto-unlock", tier: "ui" },
    { token: "config-bundle-use-project-password", tier: "ui" },
    { token: "config-bundle-export-config", tier: "ui" },
    { token: "config-bundle-export-memory", tier: "ui" },
    { token: "config-bundle-import-config", tier: "ui" },
    { token: "config-bundle-import-memory", tier: "ui" },
    { token: "id=\"confirm-checkbox\"", tier: "ui" },
    { token: "data-group-dialog-member", tier: "ui" },
    { token: "id=\"memory-browser-select-all\"", tier: "ui" },
    { token: "project-select-checkbox", tier: "ui" },
    { token: "data-session-bulk-checkbox", tier: "specialist" },
    { token: "llm-binding-image-multimodal-enabled", tier: "specialist" },
    { token: "ceo-voice-autosend", tier: "ui" },
    { token: "data-memory-row-select", tier: "ui" },
    { token: "class=\"skill-role\"", tier: "hidden" },
    { token: "tool-role tool-role-input", tier: "hidden" },
    { token: "name=\"enabled\"", tier: "hidden" },
    { token: "td-expand-all-input", tier: "ui" },
    { token: "ceo-risk-tool-input", tier: "ui" },
];

function collectSites() {
    const sites = [];
    FILES.forEach((path) => {
        const lines = fs.readFileSync(path, "utf8").split(/\r?\n/);
        lines.forEach((line, index) => {
            if (!line.includes("type=\"checkbox\"") && !line.includes("type='checkbox'")) return;
            // 盒子的邻接看 DOM 不看源码行：input 标签自身可以折行，往后让两行。
            sites.push({
                path,
                number: index + 1,
                line,
                after: lines.slice(index + 1, index + 3).join("\n"),
            });
        });
    });
    return sites;
}

test("每个勾选框落点都登记在样式台账里", () => {
    const sites = collectSites();
    assert.ok(sites.length >= LEDGER.length, `落点数少于台账数：${sites.length} < ${LEDGER.length}`);
    const unregistered = sites.filter((site) => !LEDGER.some((entry) => site.line.includes(entry.token)));
    assert.deepEqual(unregistered.map((site) => `${site.path}:${site.number}`), [], "新增勾选框必须先登记它走哪一档样式");
});

test("ui 档的每个落点都带组件 class 与紧邻的自绘盒子", () => {
    const sites = collectSites();
    LEDGER.filter((entry) => entry.tier === "ui").forEach((entry) => {
        const site = sites.find((item) => item.line.includes(entry.token));
        assert.ok(site, `找不到落点：${entry.token}`);
        assert.ok(site.line.includes("ui-checkbox-input"), `${entry.token} 必须套 .ui-checkbox-input`);
        const boxNear = site.line.includes("ui-checkbox-box") || site.after.includes("ui-checkbox-box");
        assert.ok(boxNear, `${entry.token} 缺紧邻的 .ui-checkbox-box（规则是 input:checked + .ui-checkbox-box，隔开就不画了）`);
    });
});

test("通用自绘组件与三档专用规则都还在样式里", () => {
    const base = fs.readFileSync("g3ku/web/frontend/org_graph.css", "utf8");

    assert.match(base, /:root \.ui-checkbox-input:checked \+ \.ui-checkbox-box/);
    assert.match(base, /\.ceo-session-checkbox input:checked \+ \.ceo-session-checkbox__box/);
    assert.match(base, /\.llm-image-checkbox-input input:checked \+ \.llm-image-checkbox-indicator/);
    assert.match(base, /\.qq-bot-sandbox-input:checked::after/);
    // 角色药丸靠藏掉 input 表达选中态，不是缺样式。
    assert.match(base, /\.role-toggle input \{[^}]*opacity: 0/);
});

test("不存在裸 input[type=checkbox] 全局兜底规则", () => {
    // 这条断言钉的是判据本身：一旦有人加了全局兜底，"某颗没样式"的读法就全变了，
    // 台账必须重新分档而不是继续按现状登记。
    const css = fs.readFileSync("g3ku/web/frontend/org_graph.css", "utf8");
    const bare = css.match(/(^[^\n{}]*?)input\[type=["']?checkbox["']?\][^{]*\{/gm) || [];
    const globalOnly = bare.filter((hit) => !/[.\[]/.test(hit.split("input[")[0]));
    assert.deepEqual(globalOnly, []);
});
