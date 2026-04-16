const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");

const CSS_PATH = "g3ku/web/frontend/org_graph.css";
const CSS = fs.readFileSync(CSS_PATH, "utf8");

test("ceo context load notice uses minimal tag styling instead of cloud decoration", () => {
    assert.match(
        CSS,
        /\.ceo-context-load-notice\s*\{[\s\S]*display:\s*grid;[\s\S]*grid-template-columns:\s*repeat\(2,\s*minmax\(0,\s*1fr\)\)/,
    );
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
    assert.match(CSS, /\.ceo-context-load-notice-item\.is-tool\s*\{[\s\S]*grid-column:\s*1/);
    assert.match(CSS, /\.ceo-context-load-notice-item\.is-tool\s*\{[\s\S]*justify-self:\s*start/);
    assert.match(CSS, /\.ceo-context-load-notice-item\.is-skill\s*\{[\s\S]*grid-column:\s*2/);
    assert.match(CSS, /\.ceo-context-load-notice-item\.is-skill\s*\{[\s\S]*justify-self:\s*end/);
    assert.match(CSS, /ceo-context-load-tag 10000ms/);
    assert.match(CSS, /\.ceo-context-load-notice-risk-dot\.risk-low\s*\{[\s\S]*background:\s*#22c55e/);
    assert.match(CSS, /\.ceo-context-load-notice-risk-dot\.risk-medium\s*\{[\s\S]*background:\s*#f59e0b/);
    assert.match(CSS, /\.ceo-context-load-notice-risk-dot\.risk-high\s*\{[\s\S]*background:\s*#ef4444/);
    assert.doesNotMatch(CSS, /\.ceo-context-load-notice-item::after/);
    assert.doesNotMatch(CSS, /radial-gradient\(/);
});
