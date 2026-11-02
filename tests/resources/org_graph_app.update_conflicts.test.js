const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const MODULE_PATH = path.join(__dirname, "../../g3ku/web/frontend/ceo/update-conflicts.js");
const MODULE_CODE = fs.readFileSync(MODULE_PATH, "utf8").replace(/\r\n/g, "\n");

const BLOCK_IDS = [
    "update-conflict-block",
    "update-conflict-headline",
    "update-conflict-paths",
    "update-conflict-hint",
    "update-conflict-negi-btn",
];

class StubElement {
    constructor(tag) {
        this.tagName = String(tag || "DIV").toUpperCase();
        this.children = [];
        this.dataset = {};
        this.hidden = false;
        this.disabled = false;
        this.textContent = "";
        this.className = "";
        this.parentElement = null;
        this.toggled = {};
        this.classList = {
            toggle: (name, on) => {
                this.toggled[name] = Boolean(on);
            },
            add: () => {},
            remove: () => {},
        };
    }

    appendChild(child) {
        child.parentElement = this;
        this.children.push(child);
        return child;
    }

    get listItems() {
        return this.children.map((item) => ({
            text: item.textContent || (item.children[0] ? item.children[0].textContent : ""),
            className: item.className,
        }));
    }
}

function loadModule(overrides = {}) {
    const calls = [];
    const toasts = [];
    const store = new Map();

    const elements = new Map();
    BLOCK_IDS.forEach((id) => {
        const el = new StubElement(id.endsWith("-btn") ? "BUTTON" : "DIV");
        el.id = id;
        elements.set(id, el);
    });

    const context = {
        console,
        Date,
        Number,
        String,
        Array,
        Set,
        Math,
        JSON,
        document: {
            getElementById: (id) => elements.get(id) || null,
            createElement: (tag) => new StubElement(tag),
        },
        localStorage: {
            getItem: (key) => (store.has(key) ? store.get(key) : null),
            setItem: (key, value) => {
                store.set(key, String(value));
                calls.push(["storage", key]);
            },
        },
        ApiClient: {
            async createCeoSession(payload) {
                calls.push(["create", String(payload?.title || ""), payload?.first_message ? "first-message" : "title-only"]);
                if (overrides.createFails) throw new Error("offline");
                return { ok: true, item: { session_id: "web:new-one" }, active_session_id: "web:new-one" };
            },
        },
        showToast: (options) => toasts.push(options),
        activateCeoSession: async (sessionId) => {
            calls.push(["activate", sessionId]);
        },
        closeProjectSettingsDialog: () => calls.push(["closeSettings"]),
        enqueueCeoFollowUp: (sessionId, entry) => {
            calls.push(["enqueue", sessionId, String(entry?.text || "")]);
            if (overrides.enqueueReturnsEmpty) return [];
            return [{ id: "row-1", text: entry.text, runtime_sent_at: "" }];
        },
        flushCeoQueuedFollowUp: (entryId) => {
            calls.push(["flush", entryId]);
            return true;
        },
        sendImmediateCeoMessage: () => {
            calls.push(["sendImmediate"]);
            return true;
        },
        S: { updateStatus: null, ceoTurnActive: false },
        U: {
            updateConflictBlock: elements.get("update-conflict-block"),
            updateConflictHeadline: elements.get("update-conflict-headline"),
            updateConflictPaths: elements.get("update-conflict-paths"),
            updateConflictHint: elements.get("update-conflict-hint"),
            updateConflictNegiBtn: elements.get("update-conflict-negi-btn"),
        },
    };
    vm.createContext(context);
    vm.runInContext(MODULE_CODE, context);
    return { context, calls, toasts, elements, store };
}

function conflictApply(overrides = {}) {
    return {
        outcome: "ok",
        ref: "1.12.0",
        conflict_at: "2026-11-02T14:22:06Z",
        conflict_lane: "merge-conflict",
        conflict_count: 2,
        conflict_paths: ["tools/foo/main/tool.py", "g3ku/runtime/prompts/ceo_frontdoor.md"],
        conflict_stash: "negi-upgrade-20261102T142206Z",
        conflict_dir: "C:/Negi/.g3ku/update-conflicts/20261102T142206Z",
        ...overrides,
    };
}

function drain() {
    return new Promise((resolve) => setTimeout(resolve, 0));
}

test("没有冲突批次时这一行整块不占位", () => {
    const { context, elements } = loadModule();
    elements.get("update-conflict-block").hidden = false;

    context.renderUpdateConflictBlock({ last_apply: { outcome: "ok" } });
    assert.equal(elements.get("update-conflict-block").hidden, true);

    context.renderUpdateConflictBlock({ last_apply: conflictApply({ conflict_paths: [] }) });
    assert.equal(elements.get("update-conflict-block").hidden, true);
});

test("撞行批次：列文件、说一句真话、给一颗动作键", () => {
    const { context, elements } = loadModule();
    context.renderUpdateConflictBlock({ last_apply: conflictApply() });

    assert.equal(elements.get("update-conflict-block").hidden, false);
    assert.match(elements.get("update-conflict-headline").textContent, /2 个文件和新版本撞在同一行/);
    assert.deepEqual(elements.get("update-conflict-paths").listItems.map((item) => item.text), [
        "tools/foo/main/tool.py",
        "g3ku/runtime/prompts/ceo_frontdoor.md",
    ]);
    assert.match(elements.get("update-conflict-hint").textContent, /stash/);
    assert.match(elements.get("update-conflict-hint").textContent, /negi-upgrade-20261102T142206Z/);
    const button = elements.get("update-conflict-negi-btn");
    assert.equal(button.textContent, "交给 Negi 解决");
    assert.equal(button.disabled, false);
    assert.equal(button.dataset.conflictAt, "2026-11-02T14:22:06Z");
});

test("覆盖车道说的是覆盖，不把 stash 扯进来", () => {
    const { context, elements } = loadModule();
    context.renderUpdateConflictBlock({
        last_apply: conflictApply({ conflict_lane: "copy-overwrite", conflict_stash: "", conflict_paths: ["a.py"], conflict_count: 1 }),
    });

    assert.match(elements.get("update-conflict-headline").textContent, /覆盖了 1 个你改过的文件/);
    assert.doesNotMatch(elements.get("update-conflict-hint").textContent, /stash/);
    assert.match(elements.get("update-conflict-hint").textContent, /update-conflicts/);
});

test("文件很多时列表只画前 20 条，剩下按数量收口", () => {
    const { context, elements } = loadModule();
    const many = Array.from({ length: 25 }, (_, index) => `tools/tool${index}/main/tool.py`);
    context.renderUpdateConflictBlock({ last_apply: conflictApply({ conflict_paths: many, conflict_count: many.length }) });

    const items = elements.get("update-conflict-paths").listItems;
    assert.equal(items.length, 21);
    assert.equal(items[0].text, "tools/tool0/main/tool.py");
    assert.match(items[20].text, /还有 5 个/);
});

test("点按钮：建会话、落耐久行、记下这一批已交给 Negi", async () => {
    const { context, calls, toasts, elements } = loadModule();
    context.S.updateStatus = { last_apply: conflictApply() };

    await context.handUpdateConflictsToNegi();
    await drain();

    assert.deepEqual(calls.map((item) => item[0]), ["create", "activate", "enqueue", "flush", "storage", "closeSettings"]);
    assert.equal(calls[0][1], "合并本地改动 · 1.12.0");
    assert.equal(calls[0][2], "title-only", "不许把提示词塞进建会话接口，那是另一条没有实现的车道");
    assert.equal(calls[2][1], "web:new-one");
    assert.equal(calls[3][1], "row-1");
    assert.match(calls[2][2], /^这台设备刚做完一次版本更新/);
    assert.deepEqual(toasts.map((item) => item.title), ["已交给 Negi"]);
    assert.match(elements.get("update-conflict-negi-btn").textContent, /已交给 Negi/);
});

test("提示词把动作面限死：只动列出的文件，不碰换码与重启，不删凭据", () => {
    const { context } = loadModule();
    const full = context.updateConflictPrompt(context.updateConflictSummary(conflictApply()));

    assert.match(full, /tools\/foo\/main\/tool\.py/);
    assert.match(full, /g3ku\/runtime\/prompts\/ceo_frontdoor\.md/);
    assert.match(full, /只允许修改清单里列出的文件/);
    assert.match(full, /不要执行任何换码、更新或重启动作/);
    assert.match(full, /不要删除 stash/);
    assert.match(full, /点名单个文件，禁止跑全量/);
    assert.match(full, /重启一次服务/);
    assert.match(full, /negi-upgrade-20261102T142206Z/);
});

test("同一批只投一次，新批次不受旧标记影响", async () => {
    const { context, calls } = loadModule();
    context.S.updateStatus = { last_apply: conflictApply() };
    await context.handUpdateConflictsToNegi();
    await drain();

    await context.handUpdateConflictsToNegi();
    await drain();
    assert.equal(calls.filter((item) => item[0] === "create").length, 1);

    context.S.updateStatus = { last_apply: conflictApply({ conflict_at: "2026-11-03T09:00:00Z" }) };
    await context.handUpdateConflictsToNegi();
    await drain();
    assert.equal(calls.filter((item) => item[0] === "create").length, 2);
});

test("会话没建成就不留标记，按钮可以重投", async () => {
    const { context, calls, store, elements, toasts } = loadModule({ createFails: true });
    context.S.updateStatus = { last_apply: conflictApply() };

    await context.handUpdateConflictsToNegi();
    await drain();

    assert.equal(store.size, 0);
    assert.equal(elements.get("update-conflict-negi-btn").disabled, false);
    assert.deepEqual(toasts.map((item) => item.title), ["没能交给 Negi"]);

    await context.handUpdateConflictsToNegi();
    await drain();
    assert.equal(calls.filter((item) => item[0] === "create").length, 2);
});

test("耐久行没接住就不算交给 Negi", async () => {
    const { context, store, elements } = loadModule({ enqueueReturnsEmpty: true });
    context.S.updateStatus = { last_apply: conflictApply() };

    await context.handUpdateConflictsToNegi();
    await drain();

    assert.equal(store.size, 0);
    assert.equal(elements.get("update-conflict-negi-btn").disabled, false);
});
