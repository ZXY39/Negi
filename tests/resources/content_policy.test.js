"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const FRONTEND_DIR = path.resolve(__dirname, "..", "..", "g3ku", "web", "frontend");
const SOURCE = fs.readFileSync(path.join(FRONTEND_DIR, "models", "content-policy.js"), "utf8");
const HTML = fs.readFileSync(path.join(FRONTEND_DIR, "org_graph.html"), "utf8");

function makeStubElement(extra = {}) {
    return Object.assign(
        {
            value: "",
            textContent: "",
            checked: false,
            hidden: false,
            files: [],
            addEventListener() {},
            removeEventListener() {},
            appendChild() {},
            remove() {},
            click() {},
            classList: { add() {}, remove() {}, contains: () => false, toggle() {} },
        },
        extra,
    );
}

function loadModule({ elements = {}, api = {}, toasts = [] } = {}) {
    const registry = {
        "content-policy-words": makeStubElement(),
        "content-policy-enabled": makeStubElement(),
        "content-policy-count": makeStubElement(),
        "content-policy-warning": makeStubElement(),
        "content-policy-backdrop": makeStubElement(),
        ...elements,
    };
    const context = {
        console,
        setTimeout,
        clearTimeout,
        URL: { createObjectURL: () => "blob:stub", revokeObjectURL: () => {} },
        Blob: class Blob {
            constructor(parts) {
                this.parts = parts;
            }
        },
        document: {
            readyState: "complete",
            getElementById: (id) => registry[id] || null,
            createElement: () => makeStubElement(),
            addEventListener: () => {},
            body: makeStubElement(),
        },
        ApiClient: {
            getContentPolicy: async () => ({ enabled: false, words: [], count: 0, updatedAt: "" }),
            saveContentPolicy: async (payload) => {
                api.saved = payload;
                return { enabled: payload.enabled, words: payload.words, count: payload.words.length, updatedAt: "" };
            },
        },
        showToast: (payload) => toasts.push(payload),
        requestInlineConfirm: async () => ({ confirmed: true }),
    };
    context.window = context;
    vm.createContext(context);
    vm.runInContext(SOURCE, context);
    return { context, registry, api, toasts };
}

test("parseWordList 按行切分，去 BOM、忽略空行、按大小写去重且不按逗号切分", () => {
    const { context } = loadModule();
    const words = [...context.ContentPolicy.parseWordList("﻿WordA\r\n\r\n  wordb  \nWORDA\nsix,four\n卐\n")];
    assert.deepEqual(words, ["WordA", "wordb", "six,four", "卐"]);
});

test("formatWordList 一行一个词，空表给空串", () => {
    const { context } = loadModule();
    assert.equal(context.ContentPolicy.formatWordList(["a", "b"]), "a\nb\n");
    assert.equal(context.ContentPolicy.formatWordList([]), "");
});

test("保存走 PUT 载荷：enabled 是布尔，words 是解析后的词表", async () => {
    const { context, registry, api, toasts } = loadModule();
    registry["content-policy-words"].value = "六四\n\n卐\n六四";
    registry["content-policy-enabled"].checked = true;

    await context.ContentPolicy.savePolicy();

    assert.deepEqual({ ...api.saved, words: [...api.saved.words] }, { enabled: true, words: ["六四", "卐"] });
    assert.equal(toasts.length, 1);
});

test("单字符词与超大词表只给警告，不阻断保存", async () => {
    const { context, registry, api } = loadModule();
    registry["content-policy-words"].value = "卐\nnormal-word";
    registry["content-policy-enabled"].checked = false;

    await context.ContentPolicy.savePolicy();

    assert.deepEqual({ ...api.saved, words: [...api.saved.words] }, { enabled: false, words: ["卐", "normal-word"] });
    assert.match(registry["content-policy-warning"].textContent, /单字符词/);
});

test("模型页不再有刷新按钮，改为安全审查词替换入口", () => {
    assert.equal(HTML.includes('id="model-refresh-btn"'), false);
    assert.ok(HTML.includes('id="content-policy-btn"'));
    assert.ok(HTML.includes('id="content-policy-backdrop"'));
    assert.ok(HTML.includes('id="content-policy-words"'));
    assert.ok(HTML.includes('models/content-policy.js'));
    assert.ok(HTML.includes('models/content-policy.css'));
    assert.ok(HTML.includes("konsheng/Sensitive-lexicon"));
});
