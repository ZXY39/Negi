const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const APP_PATH = "g3ku/web/frontend/org_graph_app.js";
const APP_CODE = fs.readFileSync(APP_PATH, "utf8");

// P1/P4 前端契约：渠道会话的识别、分组、补丁路由与「只读但可暂停」按钮语义。
// 这些分支若被 revert，必须在这里失败——此前仅有后端测试覆盖（审查报告 C2）。

class StubElement {}
class StubHTMLElement extends StubElement {}
class StubHTMLButtonElement extends StubHTMLElement {}
class StubHTMLInputElement extends StubHTMLElement {}
class StubHTMLTextAreaElement extends StubHTMLElement {}
class StubHTMLSelectElement extends StubHTMLElement {}

class StubDocument {
    getElementById() {
        return null;
    }

    querySelector() {
        return null;
    }

    querySelectorAll() {
        return [];
    }

    addEventListener() {}

    createElement() {
        return {};
    }
}

function makeButtonElement() {
    return {
        innerHTML: "",
        disabled: false,
        attributes: {},
        setAttribute(name, value) {
            this.attributes[name] = String(value);
        },
        getAttribute(name) {
            return Object.prototype.hasOwnProperty.call(this.attributes, name) ? this.attributes[name] : null;
        },
    };
}

function loadApp() {
    const context = {
        console,
        setTimeout,
        clearTimeout,
        setInterval,
        clearInterval,
        queueMicrotask,
        navigator: { clipboard: { writeText: async () => {} } },
        location: { protocol: "http:", host: "localhost", pathname: "/org_graph.html" },
        localStorage: { getItem: () => null, setItem: () => {}, removeItem: () => {} },
        sessionStorage: { getItem: () => null, setItem: () => {}, removeItem: () => {} },
        document: new StubDocument(),
        window: {},
        Element: StubElement,
        HTMLElement: StubHTMLElement,
        HTMLButtonElement: StubHTMLButtonElement,
        HTMLInputElement: StubHTMLInputElement,
        HTMLTextAreaElement: StubHTMLTextAreaElement,
        HTMLSelectElement: StubHTMLSelectElement,
        URLSearchParams,
        URL,
        AbortController,
        fetch: async () => ({ ok: true, json: async () => ({}) }),
        lucide: { createIcons() {} },
        marked: { parse: (value) => String(value) },
        DOMPurify: { sanitize: (value) => String(value) },
        structuredClone: global.structuredClone,
        performance: { now: () => 0 },
        requestAnimationFrame: (callback) => {
            callback();
            return 1;
        },
        cancelAnimationFrame: () => {},
        addEventListener() {},
        removeEventListener() {},
    };
    context.window = context;
    vm.createContext(context);
    vm.runInContext(
        `${APP_CODE}
        this.__testExports = {
            S,
            U,
            ceoChannelUnreadTotal,
            isChannelSessionItem,
            deriveCeoChannelId,
            displayChannelGroupLabel,
            applyCeoSessionPatch,
            renderCeoSessionCard,
            renderCeoSessions,
            toggleCeoChannelCollapse,
            isCeoChannelCollapsed,
            visibleCeoBulkSelectableSessionIds,
            readStoredCeoChannelCollapse,
            syncCeoPrimaryButton,
            activeSessionItem,
            activeSessionIsReadonly,
            activeSessionCanMessage,
            buildCeoUserMessageEditMarkup,
        };`,
        context
    );
    vm.runInContext(
        `
        renderCeoSessions = () => {};
        syncCeoSessionActions = () => {};
        syncCeoComposerReadonlyState = () => {};
        resetCeoComposerForSessionChange = () => {};
        scheduleCeoComposerUsageRefresh = () => {};
        scheduleSyncCeoComposerUsageOutline = () => {};
        syncCeoApprovalFromSnapshotEntry = () => {};
        ApiClient = {
            setActiveSessionId: () => {},
            getActiveSessionId: () => "",
        };
        `,
        context
    );
    const exports = context.__testExports;
    // 桩环境里的 window 就是这个 context（window.localStorage 等由用例替换）。
    exports.__context = context;
    // 应用用 `instanceof Set` 判多选集，跨沙箱原型的 Set 会被判成不是 Set。
    exports.__makeSet = (values = []) => {
        context.__setSeed = values;
        return vm.runInContext("new Set(this.__setSeed)", context);
    };
    return exports;
}

function extChannelItem(overrides = {}) {
    return {
        session_id: "ext:qq-official:f8a8001865631301",
        title: "外部桥接 · qq:c2c:EB6C",
        session_family: "channel",
        session_origin: "external",
        channel_id: "ext:qq-official",
        is_readonly: true,
        updated_at: "2026-09-08T10:00:00+08:00",
        ...overrides,
    };
}

test("isChannelSessionItem classifies channel items by family, origin and key prefix", () => {
    const { isChannelSessionItem } = loadApp();

    assert.equal(isChannelSessionItem({ session_family: "channel" }), true);
    assert.equal(isChannelSessionItem({ session_origin: "china" }), true);
    assert.equal(isChannelSessionItem({ session_origin: "external" }), true);
    // 防御兜底：条目缺 family/origin 时按键前缀识别（P4 本地形状污染场景）。
    assert.equal(isChannelSessionItem({ session_id: "ext:qq-official:abc" }), true);
    assert.equal(isChannelSessionItem({ session_id: "china:qqbot:default:dm" }), true);
    assert.equal(isChannelSessionItem({ session_id: "web:ceo-1", session_family: "local" }), false);
    assert.equal(isChannelSessionItem(null), false);
});

test("deriveCeoChannelId prefers explicit channel_id and derives from session key otherwise", () => {
    const { deriveCeoChannelId } = loadApp();

    assert.equal(deriveCeoChannelId({ channel_id: "ext:qq-official" }), "ext:qq-official");
    assert.equal(deriveCeoChannelId({ session_id: "ext:qq-official:f8a8001865631301" }), "ext:qq-official");
    assert.equal(deriveCeoChannelId({ session_id: "china:qqbot:default:dm:user-a" }), "qqbot");
    assert.equal(deriveCeoChannelId({ session_id: "web:ceo-1" }), "");
    assert.equal(deriveCeoChannelId({}), "");
});

test("displayChannelGroupLabel renders external bridges and legacy channels distinctly", () => {
    const { displayChannelGroupLabel } = loadApp();

    assert.equal(displayChannelGroupLabel("ext:qq-official"), "外部桥接 · qq-official");
    assert.equal(displayChannelGroupLabel("qqbot"), "QQ Bot");
    assert.equal(displayChannelGroupLabel("wecom"), "企业微信");
});

test("applyCeoSessionPatch routes channel items into channel groups, never the local list", () => {
    const { S, applyCeoSessionPatch } = loadApp();

    S.activeSessionId = "web:local-1";
    S.ceoLocalSessions = [{ session_id: "web:local-1", session_family: "local", title: "本地会话" }];
    S.ceoChannelGroups = [];
    S.ceoSessions = [...S.ceoLocalSessions];

    applyCeoSessionPatch({
        active_session_id: "web:local-1",
        active_session_family: "local",
        item: extChannelItem(),
    });

    // P4 核心验收：渠道条目不得进入本地列表。
    assert.deepEqual(S.ceoLocalSessions.map((item) => item.session_id), ["web:local-1"]);
    assert.equal(S.ceoChannelGroups.length, 1);
    assert.equal(S.ceoChannelGroups[0].channel_id, "ext:qq-official");
    assert.equal(S.ceoChannelGroups[0].label, "外部桥接 · qq-official");
    assert.equal(S.ceoChannelGroups[0].items.length, 1);
    assert.equal(S.ceoChannelGroups[0].items[0].session_id, "ext:qq-official:f8a8001865631301");

    // 重复补丁只更新不新增。
    applyCeoSessionPatch({
        active_session_id: "web:local-1",
        active_session_family: "local",
        item: extChannelItem({ is_running: true }),
    });

    assert.equal(S.ceoChannelGroups.length, 1);
    assert.equal(S.ceoChannelGroups[0].items.length, 1);
    assert.equal(S.ceoChannelGroups[0].items[0].is_running, true);
});

test("applyCeoSessionPatch routes legacy-shaped items with channel key prefixes to channel groups", () => {
    const { S, applyCeoSessionPatch } = loadApp();

    S.activeSessionId = "web:local-1";
    S.ceoLocalSessions = [{ session_id: "web:local-1", session_family: "local", title: "本地会话" }];
    S.ceoChannelGroups = [];
    S.ceoSessions = [...S.ceoLocalSessions];

    // 后端回退形状（无 family/origin）也不得被当成本地条目。
    applyCeoSessionPatch({
        active_session_id: "web:local-1",
        active_session_family: "local",
        item: { session_id: "ext:qq-official:abc123", title: "污染形状" },
    });

    assert.deepEqual(S.ceoLocalSessions.map((item) => item.session_id), ["web:local-1"]);
    assert.equal(S.ceoChannelGroups.length, 1);
    assert.equal(S.ceoChannelGroups[0].channel_id, "ext:qq-official");
    assert.equal(S.ceoChannelGroups[0].items[0].session_id, "ext:qq-official:abc123");
});

test("syncCeoPrimaryButton shows an enabled pause button for readonly channel sessions with a running turn", () => {
    const { S, U, syncCeoPrimaryButton } = loadApp();

    const channelSession = extChannelItem();
    S.activeSessionId = channelSession.session_id;
    S.ceoSessions = [channelSession];
    S.ceoLocalSessions = [];
    S.ceoChannelGroups = [{ channel_id: "ext:qq-official", label: "外部桥接 · qq-official", items: [channelSession] }];
    U.ceoSend = makeButtonElement();
    U.ceoInput = { value: "" };

    S.ceoTurnActive = true;
    S.ceoPauseBusy = false;
    syncCeoPrimaryButton();

    assert.equal(U.ceoSend.disabled, false);
    assert.ok(U.ceoSend.innerHTML.includes("暂停"));
    assert.equal(U.ceoSend.getAttribute("aria-label"), "暂停当前渠道会话回合");

    // 暂停请求进行中：按钮置灰但保持暂停语义。
    S.ceoPauseBusy = true;
    syncCeoPrimaryButton();
    assert.equal(U.ceoSend.disabled, true);
    assert.ok(U.ceoSend.innerHTML.includes("暂停中"));

    // 空闲渠道会话：回到只读禁用态。
    S.ceoTurnActive = false;
    S.ceoPauseBusy = false;
    syncCeoPrimaryButton();
    assert.equal(U.ceoSend.disabled, true);
    assert.ok(U.ceoSend.innerHTML.includes("渠道会话只读"));
});

// --- 两轴分离：can_message 管输入，is_readonly 管改历史 -----------------------

test("registered ext session accepts composer input but keeps history actions hidden", () => {
    const ctx = loadApp();
    const { S, U, activeSessionCanMessage, activeSessionIsReadonly, buildCeoUserMessageEditMarkup } = ctx;
    const item = extChannelItem({ can_message: true });
    S.activeSessionId = item.session_id;
    S.ceoSessions = [item];

    assert.equal(activeSessionCanMessage(), true, "注册过的渠道会话必须能输入");
    assert.equal(activeSessionIsReadonly(), true, "同一行历史仍然不可改：两轴不得合并");

    // 主按钮走普通车道形状：有文本即可发送，不再是「渠道会话只读」禁用态。
    U.ceoSend = makeButtonElement();
    U.ceoInput = { value: "帮我把报告改成表格" };
    S.ceoTurnActive = false;
    ctx.syncCeoPrimaryButton();
    assert.equal(U.ceoSend.disabled, false);
    assert.ok(U.ceoSend.innerHTML.includes("发送"), U.ceoSend.innerHTML);
    assert.ok(!U.ceoSend.innerHTML.includes("只读"), U.ceoSend.innerHTML);

    // 编辑重发必须继续缺席（渠道转录只读），Fork 的入口在回复行、同样由 web: 前缀挡掉：
    // 输入闸门 can_message 放宽不等于历史可改，两轴不得合并。
    assert.equal(
        buildCeoUserMessageEditMarkup({ turnId: "t1", canEditFork: true, sessionId: item.session_id }),
        ""
    );
});

test("china archive row keeps both axes closed", () => {
    const { S, activeSessionCanMessage } = loadApp();
    const item = {
        session_id: "china:qqbot:default:dm",
        session_family: "channel",
        session_origin: "china",
        is_readonly: true,
        can_message: false,
    };
    S.activeSessionId = item.session_id;
    S.ceoSessions = [item];

    assert.equal(activeSessionCanMessage(), false);
});

test("catalog rows predating can_message fall back to the readonly semantics", () => {
    const { S, activeSessionCanMessage } = loadApp();
    const item = extChannelItem(); // 无 can_message 字段：旧目录快照形状
    S.activeSessionId = item.session_id;
    S.ceoSessions = [item];

    assert.equal(activeSessionCanMessage(), false, "未下发字段时不得放开输入");
});

test("active session missing from the catalog stays writable", () => {
    const { S, activeSessionCanMessage } = loadApp();
    // 刚建好、还没进目录的本地会话：谓词不能把它判成不可输入。
    S.activeSessionId = "web:ceo-brand-new";
    S.ceoSessions = [];

    assert.equal(activeSessionCanMessage(), true);
});

test("channel cards expose 删除 and hide 命名 on the row flags", () => {
    const { renderCeoSessionCard } = loadApp();

    const html = renderCeoSessionCard(
        extChannelItem({ can_rename: false, can_delete: true, hides_on_delete: true }),
        { allowActions: true }
    );
    assert.ok(html.includes("data-session-delete="), "渠道卡片要有一颗逐卡「删除」");
    assert.ok(!html.includes("data-session-rename="), "渠道历史不可改名");

    const legacy = renderCeoSessionCard(extChannelItem({ can_rename: false, can_delete: false }), { allowActions: true });
    assert.ok(!legacy.includes("data-session-delete="), "行旗标关掉时不能凭空造出入口");

    const local = renderCeoSessionCard({ session_id: "web:ceo-1", title: "Local", can_rename: true, can_delete: true }, { allowActions: true });
    assert.ok(local.includes("data-session-rename=") && local.includes("data-session-delete="));
});

test("hides_on_delete survives the patch lane down to the stored row", () => {
    const { S, applyCeoSessionPatch } = loadApp();

    S.activeSessionId = "web:local-1";
    S.ceoLocalSessions = [{ session_id: "web:local-1", title: "Local", session_family: "local" }];
    S.ceoChannelGroups = [];

    // 判据由服务端算进目录行；从 WS 补丁这条道喂进来后不能在半路被按字段重建吃掉。
    applyCeoSessionPatch({ item: extChannelItem({ can_delete: true, hides_on_delete: true }), active_session_id: "" });
    const row = (S.ceoSessions || []).find((item) => item.session_id === "ext:qq-official:f8a8001865631301");
    assert.ok(row, "补丁行要落到渠道分组");
    assert.equal(row.hides_on_delete, true);
    assert.equal(row.can_delete, true);
});

function channelGroupFixture() {
    return [{
        channel_id: "ext:qq",
        label: "外部桥接 · qq",
        items: [
            extChannelItem({ session_id: "ext:qq:a", channel_id: "ext:qq", title: "A" }),
            extChannelItem({ session_id: "ext:qq:b", channel_id: "ext:qq", title: "B" }),
        ],
    }];
}

function railHarness() {
    const app = loadApp();
    app.U.ceoSessionList = { innerHTML: "" };
    app.S.ceoSessionTab = "channel";
    app.S.ceoChannelGroups = channelGroupFixture();
    app.S.ceoLocalSessions = [];
    app.S.activeSessionId = "ext:qq:a";
    app.S.ceoSessions = app.S.ceoChannelGroups[0].items;
    return app;
}

test("collapsed channel group turns the title into a caret button and stops rendering its cards", () => {
    const { S, U, renderCeoSessions, toggleCeoChannelCollapse } = railHarness();

    S.ceoChannelCollapsed = ["ext:qq"];
    renderCeoSessions();
    let html = U.ceoSessionList.innerHTML;
    assert.match(html, /class="ceo-session-group is-collapsed"/);
    assert.match(html, /data-channel-collapse="ext:qq"[^>]*aria-expanded="false"/);
    assert.ok(html.includes("ceo-session-group-caret"), "标题上要带指向明确的折叠标记");
    assert.doesNotMatch(html, /ceo-session-group-list/);
    assert.doesNotMatch(html, /data-session-activate=/);

    toggleCeoChannelCollapse("ext:qq");
    renderCeoSessions();
    html = U.ceoSessionList.innerHTML;
    assert.match(html, /data-channel-collapse="ext:qq"[^>]*aria-expanded="true"/);
    assert.equal((html.match(/data-session-activate=/g) || []).length, 2);
});

test("collapsed group counts only what the cards would have shown", () => {
    const { S, U, renderCeoSessions, ceoChannelUnreadTotal } = railHarness();
    // 当前会话 a 的角标本来就是 0，组内合计也不能把它算进去。
    S.ceoSessionUnread = { "ext:qq:a": 5, "ext:qq:b": 3 };
    S.ceoChannelCollapsed = [];

    renderCeoSessions();
    assert.doesNotMatch(U.ceoSessionList.innerHTML, /ceo-session-group-unread/, "展开时标题不带计数");
    assert.equal(ceoChannelUnreadTotal(S.ceoChannelGroups[0].items), 3);

    S.ceoChannelCollapsed = ["ext:qq"];
    renderCeoSessions();
    assert.match(U.ceoSessionList.innerHTML, /<span class="ceo-session-group-unread"[^>]*>3<\/span>/);

    S.ceoSessionUnread = { "ext:qq:b": 120 };
    renderCeoSessions();
    assert.match(U.ceoSessionList.innerHTML, /<span class="ceo-session-group-unread"[^>]*>99\+<\/span>/);
});

test("collapse state is stored and the renderer never reopens a folded group", () => {
    const app = railHarness();
    const { S, U, renderCeoSessions, toggleCeoChannelCollapse, isCeoChannelCollapsed } = app;
    const store = {};
    app.__context.localStorage = {
        getItem: (key) => (Object.prototype.hasOwnProperty.call(store, key) ? store[key] : null),
        setItem: (key, value) => { store[key] = String(value); },
        removeItem: (key) => { delete store[key]; },
    };

    assert.equal(toggleCeoChannelCollapse("ext:qq"), true);
    assert.equal(store["g3ku.ui.ceo.channel-collapse.v1"], '["ext:qq"]');

    // 目录刷新（新消息进来、卡片重绘）不能替操作员把折叠的组打开。
    renderCeoSessions();
    assert.ok(isCeoChannelCollapsed("ext:qq"));
    assert.doesNotMatch(U.ceoSessionList.innerHTML, /ceo-session-group-list/);
    // 沙箱里的数组与测试进程的不是同一个原型，按值比而不是 deepEqual。
    assert.equal(app.readStoredCeoChannelCollapse().join("|"), "ext:qq");
});

test("folded channel keeps a rail tile that unfolds in place", () => {
    const { S, U, renderCeoSessions, toggleCeoChannelCollapse } = railHarness();

    // 先记下未折叠时第一张卡片的字母，折叠后渠道 tile 必须画同一颗字母。
    renderCeoSessions();
    const cardGlyph = /class="ceo-session-glyph"[\s\S]*?>([^<]*)</.exec(U.ceoSessionList.innerHTML)?.[1];
    assert.ok(cardGlyph, "未折叠时卡片字母要能读到");

    S.ceoSessionUnread = { "ext:qq:a": 2, "ext:qq:b": 3 };
    S.activeSessionId = "ext:qq:b";   // b 是当前会话，它的 3 条不计入
    S.ceoChannelCollapsed = ["ext:qq"];
    renderCeoSessions();
    let html = U.ceoSessionList.innerHTML;
    assert.match(html, /<button type="button" class="ceo-session-channel-tile" data-channel-collapse="ext:qq" aria-expanded="false"/);
    assert.match(html, new RegExp(`class="ceo-session-glyph" aria-hidden="true">${cardGlyph}</span>`));
    assert.match(html, /class="ceo-session-channel-unread" aria-hidden="true">2</);
    assert.match(html, /aria-label="展开渠道 外部桥接 · qq"/);
    assert.doesNotMatch(html, /data-session-activate=/, "窄栏 tile 不该把会话卡片也画出来");

    // 窄栏里点这颗 tile 走的是同一颗折叠判据：折起来 → 解开 → 卡片回来、tile 消失。
    toggleCeoChannelCollapse("ext:qq");
    renderCeoSessions();
    html = U.ceoSessionList.innerHTML;
    assert.doesNotMatch(html, /ceo-session-channel-tile/);
    assert.match(html, /ceo-session-group-list/);
    assert.equal((html.match(/data-session-activate=/g) || []).length, 2);
});

test("collapsed groups leave the bulk selection set and their checked rows with it", () => {
    const { S, __makeSet, toggleCeoChannelCollapse, visibleCeoBulkSelectableSessionIds } = railHarness();
    S.ceoBulkMode = true;
    S.ceoSelectedSessionIds = __makeSet(["ext:qq:a", "ext:qq:b", "web:keep"]);

    assert.equal(visibleCeoBulkSelectableSessionIds().slice().sort().join("|"), "ext:qq:a|ext:qq:b");

    toggleCeoChannelCollapse("ext:qq");
    assert.deepEqual([...S.ceoSelectedSessionIds], ["web:keep"], "看不见的行不能继续留在批量作用集里");
    assert.equal(visibleCeoBulkSelectableSessionIds().length, 0);

    toggleCeoChannelCollapse("ext:qq");
    assert.deepEqual([...S.ceoSelectedSessionIds], ["web:keep"], "展开不复活被摘掉的勾选");
});
