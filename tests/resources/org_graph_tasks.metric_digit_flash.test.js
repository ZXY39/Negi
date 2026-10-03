const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

// 任务卡片 token 增长的逐位呼吸契约：
// 卡片上的数最多到 10 位，而一次模型调用只动最低几位，所以动画只许落在
// 真的换了的那几位上——未变的前缀保持正文色；整串闪会让人以为每位都在跳。

const TASKS_PATH = "g3ku/web/frontend/org_graph_tasks.js";
const CSS_PATH = "g3ku/web/frontend/org_graph.css";
const TASKS_CODE = fs.readFileSync(TASKS_PATH, "utf8");

function loadHelper() {
    const context = {console, JSON, Number, String};
    context.window = context;
    vm.createContext(context);
    const start = TASKS_CODE.indexOf("function taskMetricValueMarkup");
    const end = TASKS_CODE.indexOf("function taskCardPatchEligible");
    assert.ok(start > 0 && end > start, "metric digit markup helper slice not found");
    vm.runInContext(`const esc = (v) => String(v ?? "");\n${TASKS_CODE.slice(start, end)}`, context);
    return context;
}

// 把返回的 HTML 拆成 [{ch, lit}]，按位断言谁亮谁不亮
function digits(markup) {
    const out = [];
    const re = /<span class="pc-metric-digit is-increasing">(.)<\/span>|([^<])/g;
    let m;
    while ((m = re.exec(markup))) {
        out.push(m[1] !== undefined ? {ch: m[1], lit: true} : {ch: m[2], lit: false});
    }
    return out;
}

function litText(markup) {
    return digits(markup).filter((d) => d.lit).map((d) => d.ch).join("");
}

test("只有低位进动的增长：变化的那几位亮，前缀不亮", () => {
    const {taskMetricValueMarkup} = loadHelper();
    const markup = taskMetricValueMarkup("2,137,643,633", "2,137,642,633");
    const parts = digits(markup);
    assert.equal(parts.map((d) => d.ch).join(""), "2,137,643,633", "拆位后拼回去必须是完整数字");
    assert.equal(litText(markup), "3");
    assert.equal(parts.findIndex((d) => d.lit), 8, "只有千位那一格亮");
});

test("连续进位把中间几位一起带换：这些位都亮，未变的分隔符不亮", () => {
    const {taskMetricValueMarkup} = loadHelper();
    assert.equal(litText(taskMetricValueMarkup("1,703,800,704", "1,703,799,704")), "800");
    assert.equal(litText(taskMetricValueMarkup("2,137,643,633", "2,137,619,057")), "43633");
});

test("位数增加时整串重排，所有位都算变了", () => {
    const {taskMetricValueMarkup} = loadHelper();
    const markup = taskMetricValueMarkup("1,000,000", "999,999");
    assert.equal(litText(markup), "1,000,000");
});

test("没变的数和首次取值都不产亮位", () => {
    const {taskMetricValueMarkup} = loadHelper();
    assert.equal(taskMetricValueMarkup("73,082,829", "73,082,829"), "73,082,829");
    assert.equal(taskMetricValueMarkup("73,082,829", ""), "73,082,829");
});

test("呼吸动画挂在数字位上，不挂在整个数值元素上", () => {
    const css = fs.readFileSync(CSS_PATH, "utf8");
    assert.match(css, /\.pc-metric-digit\.is-increasing\s*\{\s*\n\s*animation: pc-token-breathe/);
    assert.doesNotMatch(css, /\.pc-metric-value\.is-increasing/, "整串动画的选择器已废，留着就是回归");
    assert.match(
        css,
        /@media \(prefers-reduced-motion: reduce\)[\s\S]*?\.pc-metric-digit\.is-increasing\s*\{[\s\S]*?animation: pc-token-breathe[\s\S]*?!important/,
        "reduce 静默下的豁免必须跟着换到数字位选择器"
    );
});

test("两条渲染道都经同一个逐位助手，增量补丁不再摘 class 强排", () => {
    assert.match(TASKS_CODE, /valueEl\.innerHTML = taskMetricValueMarkup\(/, "增量补丁道");
    assert.match(TASKS_CODE, /const valueMarkup = taskMetricValueMarkup\(/, "整网格重建道");
    assert.doesNotMatch(TASKS_CODE, /void card\.offsetWidth/, "span 重建本身就会重播动画");
});
