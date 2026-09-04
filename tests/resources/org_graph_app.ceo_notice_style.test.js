const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");

const CSS_PATH = "g3ku/web/frontend/org_graph.css";
// Windows 上经 autocrlf 检出为 CRLF；断言按 LF 匹配，读入后统一归一化行尾。
const CSS = fs.readFileSync(CSS_PATH, "utf8").replace(/\r\n/g, "\n");

test("ceo context load notice uses minimal tag styling instead of cloud decoration", () => {
    // 当前契约为纵向堆叠的极简胶囊标签（右上角），禁止回退为网格云朵装饰
    const noticeBlock = (() => {
        const match = /\.ceo-context-load-notice\s*\{([\s\S]*?)\n\}/.exec(CSS);
        return match ? match[1] : "";
    })();
    assert.ok(noticeBlock, "notice css block not found");

    assert.match(noticeBlock, /display:\s*flex;/);
    assert.match(noticeBlock, /flex-direction:\s*column;/);
    assert.match(noticeBlock, /align-items:\s*flex-end;/);
    assert.doesNotMatch(noticeBlock, /display:\s*grid/);

    const itemBlock = (() => {
        const match = /\.ceo-context-load-notice-item\s*\{([\s\S]*?)\n\}/.exec(CSS);
        return match ? match[1] : "";
    })();
    assert.ok(itemBlock, "notice item css block not found");

    assert.match(itemBlock, /display:\s*inline-flex;[\s\S]*align-items:\s*center;/);
    assert.match(itemBlock, /border:\s*1px solid color-mix\(in srgb,\s*var\(--border-color\)/);
    assert.match(itemBlock, /background:\s*color-mix\(in srgb,\s*var\(--bg-panel\)/);
    assert.match(itemBlock, /border-radius:\s*999px/);

    assert.match(CSS, /\.ceo-context-load-notice-text\s*\{[\s\S]*?flex:\s*1 1 auto/);
    assert.match(CSS, /ceo-context-load-tag 10000ms/);
    assert.match(CSS, /\.ceo-context-load-notice-risk-dot\.risk-low\s*\{[\s\S]*?background:\s*#22c55e/);
    assert.match(CSS, /\.ceo-context-load-notice-risk-dot\.risk-medium\s*\{[\s\S]*?background:\s*#f59e0b/);
    assert.match(CSS, /\.ceo-context-load-notice-risk-dot\.risk-high\s*\{[\s\S]*?background:\s*#ef4444/);
    assert.doesNotMatch(CSS, /\.ceo-context-load-notice-item::after/);
    assert.doesNotMatch(CSS, /\.ceo-context-load-notice-item::before/);
    assert.doesNotMatch(noticeBlock, /radial-gradient\(/);
    assert.doesNotMatch(itemBlock, /radial-gradient\(/);
});