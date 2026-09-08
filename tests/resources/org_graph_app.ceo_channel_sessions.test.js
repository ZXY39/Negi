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
            isChannelSessionItem,
            deriveCeoChannelId,
            displayChannelGroupLabel,
            applyCeoSessionPatch,
            syncCeoPrimaryButton,
            activeSessionItem,
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
    return context.__testExports;
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
