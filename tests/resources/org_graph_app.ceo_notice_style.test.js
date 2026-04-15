const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");

const CSS_PATH = "g3ku/web/frontend/org_graph.css";
const CSS = fs.readFileSync(CSS_PATH, "utf8");

test("ceo context load notice uses minimal tag styling instead of cloud decoration", () => {
    assert.match(
        CSS,
        /\.ceo-context-load-notice-item\s*\{[\s\S]*background:\s*color-mix\(in srgb,\s*var\(--bg-panel\)/,
    );
    assert.match(
        CSS,
        /\.ceo-context-load-notice-item\s*\{[\s\S]*border:\s*1px solid color-mix\(in srgb,\s*var\(--border-color\)/,
    );
    assert.match(
        CSS,
        /\.ceo-context-load-notice-item::before\s*\{[\s\S]*width:\s*6px;[\s\S]*height:\s*6px;/,
    );
    assert.doesNotMatch(CSS, /\.ceo-context-load-notice-item::after/);
    assert.doesNotMatch(CSS, /radial-gradient\(/);
});
