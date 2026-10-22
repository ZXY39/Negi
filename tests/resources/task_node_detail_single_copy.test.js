"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const SOURCE_PATH = path.join(
    __dirname,
    "..",
    "..",
    "g3ku",
    "web",
    "frontend",
    "tasks",
    "node-detail.js"
);

test("node-detail.js declares every top-level function once", () => {
    const source = fs.readFileSync(SOURCE_PATH, "utf8");
    const names = [...source.matchAll(/^(?:async )?function ([A-Za-z0-9_$]+)\(/gm)]
        .map((match) => match[1])
        .sort();
    const duplicates = names.filter((name, index) => names[index - 1] === name);
    assert.deepEqual(duplicates, []);
    for (const name of [
        "buildExecutionTraceSteps",
        "renderExecutionTrace",
        "showTaskNodeLoadingState",
        "renderExecutionStageRoundBlock",
        "renderExecutionStageRounds",
        "formatExecutionStageTitle",
    ]) {
        assert.equal(
            names.filter((candidate) => candidate === name).length,
            1,
            `${name} must have exactly one declaration`
        );
    }
});
