const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const MODULE_PATH = path.join(__dirname, "../../g3ku/web/frontend/ceo/update-conflicts.js");
const MODULE_CODE = fs.readFileSync(MODULE_PATH, "utf8").replace(/\r\n/g, "\n");

const BLOCK_IDS = ["update-conflict-block", "update-conflict-headline", "update-conflict-negi-btn"];

class StubElement {
    constructor(tag) {
        this.tagName = String(tag || "DIV").toUpperCase();
        this.children = [];
        this.dataset = {};
        this.hidden = false;
        this.disabled = false;
        this.textContent = "";
        this.title = "";
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
                calls.push(["storage"]);
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
        // 草稿车道：写进那条会话自己的输入框，发送留给人。
        setCeoComposerDraft: (sessionId, entry) => {
            calls.push(["stage", sessionId, String(entry?.text || "").slice(0, 16)]);
            if (overrides.stageReturnsNull) return null;
            return { session_id: sessionId, text: entry.text, uploads: [] };
        },
        restoreCeoComposerDraftForSession: (sessionId) => {
            calls.push(["restore", sessionId]);
        },
        // 自动发送那两条车道在场也必须一次都不碰：共用 socket 上帧里的 session_id 不路由回合。
        enqueueCeoFollowUp: () => {
            calls.push(["FORBIDDEN-enqueue"]);
            return [];
        },
        sendActiveCeoFollowUpsToRuntime: () => {
            calls.push(["FORBIDDEN-send"]);
            return true;
        },
        flushCeoQueuedFollowUp: () => {
            calls.push(["FORBIDDEN-flush"]);
            return true;
        },
        // 一条"页面上没有激活会话"的车道：隐式 active 读到 null 时任何依赖它的投递都会空转。
        activeSessionId: () => null,
        S: { updateStatus: null, ceoTurnActive: false },
        U: {
            updateConflictBlock: elements.get("update-conflict-block"),
            updateConflictHeadline: elements.get("update-conflict-headline"),
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
        conflict_dir: "C:/Negi/.g3ku/update-conflicts/20261102T154657Z",
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

test("面板只报数量，不铺原始路径", () => {
    const { context, elements } = loadModule();
    context.renderUpdateConflictBlock({ last_apply: conflictApply() });
    const block = elements.get("update-conflict-block");
    const button = elements.get("update-conflict-negi-btn");

    assert.equal(block.hidden, false);
    assert.equal(elements.get("update-conflict-headline").textContent, "这次更新有 2 处本地改动没能自动合上。");
    assert.doesNotMatch(block.textContent, /tools\/foo/);
    // 清单不是删掉，是收进那颗已经在那儿的键上。
    assert.match(button.title, /tools\/foo\/main\/tool\.py/);
    assert.match(button.title, /g3ku\/runtime\/prompts\/ceo_frontdoor\.md/);
    assert.match(button.title, /negi-upgrade-20261102T142206Z/);
    assert.match(button.title, /update-conflicts\/20261102T154657Z/);
    assert.equal(button.textContent, "交给 Negi 解决");
    assert.equal(button.disabled, false);
    assert.equal(button.dataset.conflictAt, "2026-11-02T14:22:06Z");
});

test("覆盖车道说覆盖，不说撞行", () => {
    const { context, elements } = loadModule();
    context.renderUpdateConflictBlock({
        last_apply: conflictApply({ conflict_lane: "copy-overwrite", conflict_stash: "", conflict_paths: ["a.py"], conflict_count: 1 }),
    });
    assert.equal(elements.get("update-conflict-headline").textContent, "这次更新覆盖了 1 处本地改动。");
    assert.doesNotMatch(elements.get("update-conflict-negi-btn").title, /stash/);
});

test("文件很多时 title 里也收到 20 条并按数量收口", () => {
    const { context, elements } = loadModule();
    const many = Array.from({ length: 25 }, (_, index) => `tools/tool${index}/main/tool.py`);
    context.renderUpdateConflictBlock({ last_apply: conflictApply({ conflict_paths: many, conflict_count: many.length }) });
    const title = elements.get("update-conflict-negi-btn").title;
    assert.match(title, /tools\/tool0\/main\/tool\.py/);
    assert.match(title, /tools\/tool19\/main\/tool\.py/);
    assert.doesNotMatch(title, /tools\/tool20\/main\/tool\.py/);
    assert.match(title, /等 25 个文件/);
});

test("点按钮：新建会话、把提示词写成那条会话的草稿，绝不自动发送", async () => {
    const { context, calls, toasts, elements } = loadModule();
    context.S.updateStatus = { last_apply: conflictApply() };

    await context.handUpdateConflictsToNegi();
    await drain();

    assert.deepEqual(calls.map((item) => item[0]), ["create", "stage", "activate", "restore", "storage", "closeSettings"]);
    const create = calls.find((item) => item[0] === "create");
    const stage = calls.find((item) => item[0] === "stage");
    assert.equal(create[1], "合并本地改动 · 1.12.0");
    assert.equal(create[2], "title-only", "不许把提示词塞进建会话接口，那是没有实现的一条车道");
    assert.equal(stage[1], "web:new-one", "草稿必须写在新建的那条会话上");
    assert.match(stage[2], /^这台设备刚做完一次版本更新/);
    assert.deepEqual(toasts.map((item) => item.title), ["提示词已备好"]);
    assert.match(toasts[0].text, /按发送才交给 Negi/);
    assert.match(elements.get("update-conflict-negi-btn").textContent, /已备好，进会话按发送/);
});

test("页面上没有激活会话也照样备得出来（不靠隐式 active，也不靠连接状态）", async () => {
    const { context, calls } = loadModule();
    context.S.updateStatus = { last_apply: conflictApply() };

    await context.handUpdateConflictsToNegi();
    await drain();

    assert.equal(calls.find((item) => item[0] === "stage")[1], "web:new-one");
    assert.equal(calls.filter((item) => item[0] === "storage").length, 1);
});

test("任何一条自动发送的车道都不许被碰", async () => {
    const { context, calls } = loadModule();
    context.S.updateStatus = { last_apply: conflictApply() };

    await context.handUpdateConflictsToNegi();
    await drain();

    for (const forbidden of ["FORBIDDEN-enqueue", "FORBIDDEN-send", "FORBIDDEN-flush"]) {
        assert.equal(calls.filter((item) => item[0] === forbidden).length, 0, forbidden + " 不许出现在这条车道上");
    }
});

test("草稿没写进去就不算备好：不留标记，按钮可重投", async () => {
    const { context, store, elements, toasts } = loadModule({ stageReturnsNull: true });
    context.S.updateStatus = { last_apply: conflictApply() };

    await context.handUpdateConflictsToNegi();
    await drain();

    assert.equal(store.size, 0, "没写成草稿就不许留下「已备好」的标记");
    assert.equal(elements.get("update-conflict-negi-btn").disabled, false);
    assert.deepEqual(toasts.map((item) => item.title), ["没能准备给 Negi"]);
});

test("会话没建成就不留标记", async () => {
    const { context, calls, store, elements, toasts } = loadModule({ createFails: true });
    context.S.updateStatus = { last_apply: conflictApply() };

    await context.handUpdateConflictsToNegi();
    await drain();

    assert.equal(store.size, 0);
    assert.equal(elements.get("update-conflict-negi-btn").disabled, false);
    assert.deepEqual(toasts.map((item) => item.title), ["没能准备给 Negi"]);

    await context.handUpdateConflictsToNegi();
    await drain();
    assert.equal(calls.filter((item) => item[0] === "create").length, 2);
});

test("同一批只准备一次，新批次不受旧标记影响", async () => {
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
