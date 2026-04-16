const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const RESOURCES_PATH = "g3ku/web/frontend/org_graph_resources.js";
const RESOURCES_CODE = fs.readFileSync(RESOURCES_PATH, "utf8");

class StubElement {
    constructor(id = "") {
        this.id = id;
        this.value = "";
        this.hidden = false;
        this.disabled = false;
        this.innerHTML = "";
        this.textContent = "";
        this.style = {};
        this.dataset = {};
        this.children = [];
        this.classList = {
            add() {},
            remove() {},
            toggle() {},
            contains() { return false; },
        };
    }

    addEventListener() {}

    appendChild(child) {
        this.children.push(child);
        return child;
    }

    querySelector() {
        return null;
    }

    querySelectorAll() {
        return [];
    }
}

class StubHTMLElement extends StubElement {}
class StubHTMLInputElement extends StubHTMLElement {}

class StubDocument {
    createElement() {
        return new StubHTMLElement();
    }

    getElementById() {
        return null;
    }

    querySelector() {
        return null;
    }

    querySelectorAll() {
        return [];
    }
}

function loadResources() {
    const context = {
        console,
        document: new StubDocument(),
        Element: StubElement,
        HTMLElement: StubHTMLElement,
        HTMLInputElement: StubHTMLInputElement,
        S: {
            tools: [],
            toolPage: 1,
            toolPageSize: 50,
            selectedTool: null,
            toolDirty: false,
        },
        U: {
            toolSearch: new StubHTMLInputElement("tool-search-input"),
            toolStatus: new StubHTMLInputElement("tool-status-filter"),
            toolRisk: new StubHTMLInputElement("tool-risk-filter"),
            toolList: new StubHTMLElement("tool-list"),
            toolEmpty: new StubHTMLElement("tool-detail-empty"),
            toolDetail: new StubHTMLElement("tool-detail-content"),
            toolBackdrop: new StubHTMLElement("tool-detail-backdrop"),
            toolDrawer: new StubHTMLElement("tool-detail-drawer"),
        },
        paginateResources(items, page, pageSize) {
            return {
                items,
                total: items.length,
                currentPage: Math.max(1, Number(page) || 1),
                pageSize: Math.max(1, Number(pageSize) || 50),
            };
        },
        syncResourcePagination() {},
        esc(value) {
            return String(value == null ? "" : value);
        },
        roleKey(value) {
            return String(value || "").trim().toLowerCase();
        },
        setDrawerOpen() {},
        renderToolActions() {},
        addNotice() {},
        showToast() {},
        openConfirm() {},
        loadTools: async () => {},
        openTool: async () => {},
        resourceDeleteErrorText(error) {
            return String(error?.message || error || "");
        },
        ApiClient: {
            async updateToolPolicy() {
                return { item: {} };
            },
            async reloadResources() {
                return { ok: true };
            },
            async getTools() {
                return [];
            },
        },
    };
    context.U.toolStatus.value = "all";
    context.U.toolRisk.value = "all";
    context.setTimeout = setTimeout;
    context.clearTimeout = clearTimeout;
    context.window = context;
    vm.createContext(context);
    vm.runInContext(
        `${RESOURCES_CODE}
        this.__testExports = {
            applyExecToolExecutionMode,
            execToolModeLabel,
            execToolModeSummary,
            renderTools,
            renderToolDetail,
            saveTool,
        };`,
        context,
    );
    return {
        ...context.__testExports,
        S: context.S,
        U: context.U,
        ApiClient: context.ApiClient,
    };
}

test("renderTools counts only Tool 管理 actions that remain visible in the frontend", () => {
    const { S, U, renderTools } = loadResources();
    S.tools = [
        {
            tool_id: "content_navigation",
            display_name: "Content",
            description: "Describe, search, and open content.",
            source_path: "tools/content",
            enabled: true,
            available: true,
            actions: [
                { action_id: "describe", label: "Describe Content", risk_level: "low" },
                { action_id: "search", label: "Search Content", risk_level: "low" },
                { action_id: "open", label: "Open Content Excerpt", risk_level: "low" },
                { action_id: "inspect", label: "Inspect Content (Legacy)", risk_level: "low" },
            ],
        },
        {
            tool_id: "memory",
            display_name: "Memory",
            description: "Search and write memory.",
            source_path: "tools/memory",
            enabled: true,
            available: true,
            actions: [
                { action_id: "search", label: "Search Memory", risk_level: "low" },
                { action_id: "write", label: "Write Memory", risk_level: "medium" },
                { action_id: "runtime", label: "Memory Runtime", risk_level: "low", admin_mode: "readonly_system", agent_visible: false },
            ],
        },
    ];

    renderTools();

    assert.equal(U.toolList.children.length, 2);
    assert.match(U.toolList.children[0].innerHTML, /3\s*个 action/);
    assert.match(U.toolList.children[1].innerHTML, /2\s*个 action/);
});

test("renderToolDetail hides Inspect Content (Legacy) from Tool 管理 details", () => {
    const { S, U, renderToolDetail } = loadResources();
    S.selectedTool = {
        tool_id: "content_navigation",
        display_name: "Content",
        description: "Describe, search, and open content.",
        enabled: true,
        available: true,
        callable: true,
        is_core: true,
        metadata: {},
        actions: [
            { action_id: "describe", label: "Describe Content", risk_level: "low", allowed_roles: ["ceo", "execution", "inspection"] },
            { action_id: "search", label: "Search Content", risk_level: "low", allowed_roles: ["ceo", "execution", "inspection"] },
            { action_id: "open", label: "Open Content Excerpt", risk_level: "low", allowed_roles: ["ceo", "execution", "inspection"] },
            { action_id: "inspect", label: "Inspect Content (Legacy)", risk_level: "low", allowed_roles: ["ceo", "execution", "inspection"] },
        ],
        toolskill_content: "",
    };

    renderToolDetail();

    assert.match(U.toolDetail.innerHTML, /Describe Content/);
    assert.match(U.toolDetail.innerHTML, /Search Content/);
    assert.match(U.toolDetail.innerHTML, /Open Content Excerpt/);
    assert.doesNotMatch(U.toolDetail.innerHTML, /Inspect Content \(Legacy\)/);
});

test("renderToolDetail hides Memory Runtime from Tool 管理 details", () => {
    const { S, U, renderToolDetail } = loadResources();
    S.selectedTool = {
        tool_id: "memory",
        display_name: "Memory",
        description: "Search and write memory.",
        enabled: true,
        available: true,
        callable: true,
        is_core: true,
        metadata: {},
        actions: [
            { action_id: "search", label: "Search Memory", risk_level: "low", allowed_roles: ["ceo", "execution", "inspection"] },
            { action_id: "write", label: "Write Memory", risk_level: "medium", allowed_roles: ["ceo"] },
            { action_id: "runtime", label: "Memory Runtime", risk_level: "low", allowed_roles: ["ceo", "execution", "inspection"], admin_mode: "readonly_system", agent_visible: false },
        ],
        toolskill_content: "",
    };

    renderToolDetail();

    assert.match(U.toolDetail.innerHTML, /Search Memory/);
    assert.match(U.toolDetail.innerHTML, /Write Memory/);
    assert.doesNotMatch(U.toolDetail.innerHTML, /Memory Runtime/);
});

test("renderToolDetail shows exec execution mode controls for exec_runtime", () => {
    const { S, U, renderToolDetail } = loadResources();
    S.selectedTool = {
        tool_id: "exec_runtime",
        display_name: "Exec Runtime",
        description: "Execute shell commands.",
        enabled: true,
        available: true,
        callable: true,
        is_core: false,
        metadata: { execution_mode: "full_access" },
        exec_runtime_policy: {
            mode: "full_access",
            guardrails_enabled: false,
            summary: "exec will execute shell commands without exec-side guardrails.",
        },
        actions: [
            { action_id: "run", label: "Run Command", risk_level: "high", allowed_roles: ["ceo", "execution"] },
        ],
        toolskill_content: "",
    };

    renderToolDetail();

    assert.match(U.toolDetail.innerHTML, /执行模式/);
    assert.match(U.toolDetail.innerHTML, /全权限模式/);
    assert.match(U.toolDetail.innerHTML, /不再应用 exec 侧护栏/);
    assert.doesNotMatch(U.toolDetail.innerHTML, /Execution Mode/);
    assert.doesNotMatch(U.toolDetail.innerHTML, /无需重启项目/);
});

test("saveTool sends execution_mode for exec_runtime", async () => {
    const { S, saveTool, ApiClient } = loadResources();
    const calls = [];
    ApiClient.updateToolPolicy = async (toolId, payload) => {
        calls.push({ toolId, payload });
        return { item: { tool_id: toolId, metadata: { execution_mode: payload.execution_mode } } };
    };
    ApiClient.reloadResources = async () => ({ ok: true });
    ApiClient.getTools = async () => [
        {
            tool_id: "exec_runtime",
            display_name: "Exec Runtime",
            description: "Execute shell commands.",
            enabled: true,
            available: true,
            actions: [
                { action_id: "run", label: "Run Command", allowed_roles: ["ceo", "execution"] },
            ],
            metadata: { execution_mode: "full_access" },
        },
    ];

    S.selectedTool = {
        tool_id: "exec_runtime",
        display_name: "Exec Runtime",
        description: "Execute shell commands.",
        enabled: true,
        available: true,
        actions: [
            { action_id: "run", label: "Run Command", allowed_roles: ["ceo", "execution"] },
        ],
        metadata: { execution_mode: "full_access" },
        exec_runtime_policy: { mode: "full_access" },
    };
    S.toolDirty = true;

    await saveTool({ showProgressToast: false, showSuccessToast: false, reopenDetail: false });

    assert.equal(calls.length, 1);
    assert.equal(calls[0].toolId, "exec_runtime");
    assert.equal(calls[0].payload.enabled, true);
    assert.deepEqual([...calls[0].payload.actions.run], ["ceo", "execution"]);
    assert.equal(calls[0].payload.execution_mode, "full_access");
});

test("applyExecToolExecutionMode updates localized summary for immediate rerender", () => {
    const { S, U, renderToolDetail, applyExecToolExecutionMode, execToolModeLabel, execToolModeSummary } = loadResources();
    S.selectedTool = {
        tool_id: "exec_runtime",
        display_name: "Exec Runtime",
        description: "Execute shell commands.",
        enabled: true,
        available: true,
        actions: [
            { action_id: "run", label: "Run Command", allowed_roles: ["ceo", "execution"] },
        ],
        metadata: { execution_mode: "full_access" },
        exec_runtime_policy: { mode: "full_access" },
    };

    assert.equal(execToolModeLabel("governed"), "监管模式");
    assert.equal(execToolModeLabel("full_access"), "全权限模式");
    assert.match(execToolModeSummary("governed"), /继续应用 exec 侧护栏/);
    assert.match(execToolModeSummary("full_access"), /不再应用 exec 侧护栏/);

    applyExecToolExecutionMode(S.selectedTool, "governed");
    renderToolDetail();

    assert.equal(S.selectedTool.metadata.execution_mode, "governed");
    assert.equal(S.selectedTool.exec_runtime_policy.mode, "governed");
    assert.match(U.toolDetail.innerHTML, /继续应用 exec 侧护栏/);
});
