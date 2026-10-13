const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const RESOURCES_PATH = "g3ku/web/frontend/org_graph_resources.js";
const RESOURCES_CODE = fs.readFileSync(RESOURCES_PATH, "utf8");
const TOOLS_ROOT = path.resolve(__dirname, "..", "..", "tools");

// GET /api/resources/tools 现在一行就是一个工具：记录上没有 actions / executor_names /
// primary_executor_name，风险度与角色直接挂在工具记录上（见 main/governance/models.py
// 的 ToolRecord）。fixture 按那份字段形状构造，不要再造族形数据。
function toolRecord(overrides = {}) {
    return {
        tool_id: "",
        display_name: "",
        description: "",
        risk_level: "medium",
        destructive: false,
        agent_visible: true,
        admin_mode: "editable",
        allowed_roles: ["ceo", "execution", "inspection"],
        is_core: false,
        enabled: true,
        available: true,
        callable: true,
        tool_type: "internal",
        install_dir: null,
        source_path: "",
        metadata: {},
        ...overrides,
    };
}

class StubElement {
    constructor(id = "") {
        this.id = id;
        this.value = "";
        this.hidden = false;
        this.disabled = false;
        this.textContent = "";
        this.className = "";
        this.style = {};
        this.dataset = {};
        this.children = [];
        this.stubQueryTargets = new Map();
        this._innerHTML = "";
        // classList 必须真的记账：空实现会让任何类名断言永真。
        this.classList = {
            add: (...tokens) => {
                const classes = new Set(String(this.className || "").split(/\s+/).filter(Boolean));
                tokens.forEach((token) => classes.add(token));
                this.className = [...classes].join(" ");
            },
            remove: (...tokens) => {
                const classes = new Set(String(this.className || "").split(/\s+/).filter(Boolean));
                tokens.forEach((token) => classes.delete(token));
                this.className = [...classes].join(" ");
            },
            contains: (token) => String(this.className || "").split(/\s+/).includes(token),
            toggle: (token, force) => {
                const shouldAdd = force === undefined ? !this.classList.contains(token) : !!force;
                if (shouldAdd) this.classList.add(token);
                else this.classList.remove(token);
            },
        };
    }

    addEventListener() {}

    // 浏览器语义：写 innerHTML 会丢掉既有子节点。桩不清 children 的话，
    // 同一个实例上第二次 renderTools 会把上一轮的行累加进来，行计数断言就失真了。
    get innerHTML() {
        return this._innerHTML;
    }

    set innerHTML(value) {
        this._innerHTML = String(value);
        this.children.length = 0;
    }

    appendChild(child) {
        this.children.push(child);
        return child;
    }

    // 只有测试显式登记过的选择器才会被解析出来，其余返回 null，实现里
    // 依赖 querySelector 的分支保持短路。
    querySelector(selector) {
        return this.stubQueryTargets.get(String(selector || "")) || null;
    }

    stubQueryTargetFor(selector) {
        if (!this.stubQueryTargets.has(selector)) {
            this.stubQueryTargets.set(selector, new StubHTMLElement());
        }
        return this.stubQueryTargets.get(selector);
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
            execToolExecutionMode,
            execToolModeLabel,
            execToolModeSummary,
            filterTools,
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

test("renderTools renders one row per tool with the tool's own status, risk and core tags", () => {
    const { S, U, renderTools } = loadResources();
    S.tools = [
        toolRecord({
            tool_id: "content_open",
            display_name: "Content Open",
            description: "Open one excerpt from a content ref or absolute file path.",
            risk_level: "low",
            is_core: true,
            source_path: "tools/content_open",
        }),
        toolRecord({
            tool_id: "filesystem_write",
            display_name: "Filesystem Write",
            description: "Write file content to disk.",
            risk_level: "high",
            destructive: true,
            allowed_roles: ["ceo", "execution"],
            source_path: "tools/filesystem_write",
        }),
    ];

    renderTools();

    // 一行一个工具：两条记录就是两行，不再折叠成工具族。
    assert.equal(U.toolList.children.length, 2);
    const [openRow, writeRow] = U.toolList.children.map((row) => row.innerHTML);

    assert.match(openRow, /Content Open/);
    assert.match(openRow, /class="meta-tag risk-low">低风险</);
    assert.match(openRow, /<span class="meta-tag">核心工具<\/span>/);

    assert.match(writeRow, /Filesystem Write/);
    assert.match(writeRow, /class="meta-tag risk-high">高风险</);
    assert.doesNotMatch(writeRow, /核心工具/);

    // 族形时代的「N 个 action」统计已经随中间层一起删掉。
    assert.doesNotMatch(openRow, /个\s*action/);
    assert.doesNotMatch(writeRow, /个\s*action/);
});

test("renderTools hides tools the agent cannot see instead of hiding single actions by name", () => {
    const { S, U, renderTools } = loadResources();
    S.tools = [
        toolRecord({
            tool_id: "memory_update",
            display_name: "Update Memory",
            description: "Queue natural-language memory requests.",
            risk_level: "medium",
            allowed_roles: ["ceo"],
            source_path: "tools/memory_update",
        }),
        // 旧形状里被 hideToolAdminAction(memory, runtime) 藏起来的 action，
        // 现在整条工具记录就是隐藏的：agent_visible 是唯一规则。
        toolRecord({
            tool_id: "memory_runtime",
            display_name: "Memory Runtime",
            description: "Expose queued markdown memory runtime internals.",
            risk_level: "low",
            agent_visible: false,
            admin_mode: "readonly_system",
            source_path: "tools/memory_runtime",
        }),
    ];

    renderTools();

    assert.equal(U.toolList.children.length, 1);
    assert.match(U.toolList.children[0].innerHTML, /Update Memory/);
    assert.doesNotMatch(U.toolList.children[0].innerHTML, /Memory Runtime/);

    // 搜索、风险、状态筛选都不能把隐藏工具放回列表。
    U.toolSearch.value = "memory";
    U.toolRisk.value = "low";
    U.toolStatus.value = "enabled";
    renderTools();
    assert.equal(U.toolList.children.length, 0);
    assert.doesNotMatch(U.toolList.innerHTML, /Memory Runtime/);

    // 反过来：agent_visible 没被关掉的记录一律进列表，不再按 tool_id 名单挑掉。
    U.toolSearch.value = "";
    U.toolRisk.value = "all";
    U.toolStatus.value = "all";
    S.tools = [toolRecord({ tool_id: "content", display_name: "Legacy Content", risk_level: "low" })];
    renderTools();
    assert.equal(U.toolList.children.length, 1);
    assert.match(U.toolList.children[0].innerHTML, /Legacy Content/);
});

test("filterTools matches the tool record's own risk level and searchable fields", () => {
    const { S, U, filterTools } = loadResources();
    S.tools = [
        toolRecord({
            tool_id: "content_open",
            display_name: "Content Open",
            description: "Open one excerpt from a content ref or absolute file path.",
            risk_level: "low",
            source_path: "tools/content_open",
        }),
        toolRecord({
            tool_id: "filesystem_write",
            display_name: "Filesystem Write",
            description: "Write file content to disk.",
            risk_level: "high",
            allowed_roles: ["ceo", "execution"],
            source_path: "tools/filesystem_write",
        }),
        toolRecord({
            tool_id: "memory_runtime",
            display_name: "Memory Runtime",
            risk_level: "low",
            agent_visible: false,
            admin_mode: "readonly_system",
            source_path: "tools/memory_runtime",
        }),
    ];

    const ids = () => filterTools().map((tool) => tool.tool_id);

    // 风险筛选比的是这条工具记录自己的 risk_level，不是族下任一 action。
    U.toolRisk.value = "high";
    assert.deepEqual(ids(), ["filesystem_write"]);
    U.toolRisk.value = "low";
    assert.deepEqual(ids(), ["content_open"]);
    U.toolRisk.value = "all";

    // 搜索命中 tool_id / display_name / description / source_path。
    U.toolSearch.value = "filesystem_write";
    assert.deepEqual(ids(), ["filesystem_write"]);
    U.toolSearch.value = "excerpt";
    assert.deepEqual(ids(), ["content_open"]);
    U.toolSearch.value = "tools/content_open";
    assert.deepEqual(ids(), ["content_open"]);

    // 状态筛选逻辑不变：禁用/不可用各自独立成档。
    U.toolSearch.value = "";
    S.tools.push(toolRecord({ tool_id: "cron", display_name: "Cron Schedule", enabled: false }));
    U.toolStatus.value = "disabled";
    assert.deepEqual(ids(), ["cron"]);
    U.toolStatus.value = "all";
});

test("the legacy content tool family is gone from both the repository and the frontend", () => {
    // 旧用例断言 content_navigation 的 inspect action 被前端藏起来；legacy content
    // 工具族已从仓库删除，藏名单也一起删掉了，所以这里断言它确实不存在。
    assert.equal(fs.existsSync(path.join(TOOLS_ROOT, "content")), false);
    assert.equal(fs.existsSync(path.join(TOOLS_ROOT, "content_open")), true);
    assert.equal(fs.existsSync(path.join(TOOLS_ROOT, "content_search")), true);
    assert.equal(fs.existsSync(path.join(TOOLS_ROOT, "content_describe")), true);

    assert.equal(RESOURCES_CODE.includes("hideToolAdminAction"), false);
    assert.equal(RESOURCES_CODE.includes("toolActionsForDisplay"), false);
});

test("renderToolDetail shows the split content tool without any action layer", () => {
    const { S, U, renderToolDetail } = loadResources();
    S.selectedTool = toolRecord({
        tool_id: "content_open",
        display_name: "Content Open",
        description: "Open one excerpt from a content ref or absolute file path.",
        risk_level: "low",
        is_core: true,
        callable: true,
        toolskill_content: "",
    });

    renderToolDetail();

    const html = U.toolDetail.innerHTML;
    // 标题下只有一行徽标：风险、启用状态、核心工具。tool_id 不再重复印一遍。
    assert.match(html, /<h2 id="tool-detail-title">Content Open<\/h2>/);
    assert.match(
        html,
        /<div class="detail-modal-meta">\s*<span class="meta-tag risk-low">低风险<\/span>\s*<span class="meta-tag status-enabled">已启用<\/span>\s*<span class="meta-tag">核心工具<\/span>\s*<\/div>/
    );
    assert.doesNotMatch(html, /class="subtitle"/);
    assert.doesNotMatch(html, /Inspect Content \(Legacy\)/);
    // 族形时代的 action 区块不复存在。
    assert.doesNotMatch(html, /Action 权限/);
    assert.doesNotMatch(html, /tool-action-name/);
    assert.doesNotMatch(html, /个\s*action/);
});

test("renderToolDetail renders one role visibility group driven by the tool's own allowed_roles", () => {
    const { S, U, renderToolDetail } = loadResources();
    S.selectedTool = toolRecord({
        tool_id: "filesystem_write",
        display_name: "Filesystem Write",
        description: "Write file content to disk.",
        risk_level: "high",
        destructive: true,
        allowed_roles: ["ceo", "execution"],
    });

    renderToolDetail();

    const html = U.toolDetail.innerHTML;
    assert.match(html, /<h3>角色可见<\/h3>/);
    // 一组三个 checkbox，只有 data-role，没有旧的 data-action 维度。
    const roles = [...html.matchAll(/data-role="(ceo|execution|inspection)"/g)].map((match) => match[1]);
    assert.deepEqual(roles, ["ceo", "execution", "inspection"]);
    assert.equal(html.includes("data-action="), false);
    assert.match(html, /class="role-toggle tool-role-toggle checked">\s*<input type="checkbox" class="tool-role tool-role-input" data-role="ceo"/);
    assert.match(html, /data-role="execution"[^>]*checked/);
    assert.doesNotMatch(html, /data-role="inspection"[^>]*checked/);
    assert.doesNotMatch(html, /当前工具对所有角色禁用。/);

    // 角色全空时给出整工具级提示，而不是 per-action 提示。
    S.selectedTool.allowed_roles = [];
    renderToolDetail();
    assert.match(U.toolDetail.innerHTML, /当前工具对所有角色禁用。/);
    assert.doesNotMatch(U.toolDetail.innerHTML, /当前 action 对所有角色禁用。/);
});

test("renderToolDetail lets operators disable a core tool but never delete it", () => {
    const { S, U, renderToolDetail } = loadResources();
    S.selectedTool = toolRecord({
        tool_id: "content_open",
        display_name: "Content Open",
        description: "Open one excerpt from a content ref or absolute file path.",
        risk_level: "low",
        is_core: true,
    });
    const statusRow = U.toolDetail.stubQueryTargetFor(".resource-status-row");

    renderToolDetail();

    // 新合同：核心工具可以停用，启停按钮不再被 disabled。
    assert.match(U.toolDetail.innerHTML, /<button type="button" class="toolbar-btn danger" id="tool-disable-btn">禁用工具<\/button>/);
    assert.doesNotMatch(U.toolDetail.innerHTML, /id="tool-disable-btn"[^>]*disabled/);
    assert.doesNotMatch(U.toolDetail.innerHTML, /Core tool cannot be disabled/);
    // 删除仍然锁死。
    const deleteButton = statusRow.children.find((child) => child.id === "tool-delete-btn");
    assert.ok(deleteButton, "delete button is appended into the status row");
    assert.equal(deleteButton.textContent, "核心工具不可删除");
    assert.equal(deleteButton.disabled, true);

    // 非核心工具：启停与删除都可点。
    const plain = loadResources();
    plain.S.selectedTool = toolRecord({
        tool_id: "filesystem_write",
        display_name: "Filesystem Write",
        description: "Write file content to disk.",
        risk_level: "high",
        destructive: true,
        allowed_roles: ["ceo", "execution"],
    });
    const plainStatusRow = plain.U.toolDetail.stubQueryTargetFor(".resource-status-row");
    plain.renderToolDetail();

    assert.match(plain.U.toolDetail.innerHTML, /id="tool-disable-btn">禁用工具<\/button>/);
    const plainDeleteButton = plainStatusRow.children.find((child) => child.id === "tool-delete-btn");
    assert.equal(plainDeleteButton.textContent, "删除");
    assert.equal(plainDeleteButton.disabled, false);
});

test("renderToolDetail shows exec execution mode controls for exec", () => {
    const { S, U, renderToolDetail } = loadResources();
    S.selectedTool = toolRecord({
        tool_id: "exec",
        display_name: "Exec Runtime",
        description: "Execute shell commands.",
        risk_level: "high",
        destructive: true,
        allowed_roles: ["ceo", "execution"],
        is_core: false,
        metadata: { execution_mode: "full_access" },
        exec_runtime_policy: {
            mode: "full_access",
            guardrails_enabled: false,
            summary: "exec will execute shell commands without exec-side guardrails.",
        },
        toolskill_content: "",
    });

    renderToolDetail();

    assert.match(U.toolDetail.innerHTML, /执行模式/);
    assert.match(U.toolDetail.innerHTML, /全权限模式/);
    assert.match(U.toolDetail.innerHTML, /不再应用 exec 侧护栏/);
    assert.doesNotMatch(U.toolDetail.innerHTML, /Execution Mode/);
    assert.doesNotMatch(U.toolDetail.innerHTML, /无需重启项目/);
});

test("saveTool sends allowed_roles and execution_mode for exec", async () => {
    const { S, saveTool, ApiClient } = loadResources();
    const calls = [];
    ApiClient.updateToolPolicy = async (toolId, payload) => {
        calls.push({ toolId, payload });
        return { item: { tool_id: toolId, metadata: { execution_mode: payload.execution_mode } } };
    };
    ApiClient.reloadResources = async () => ({ ok: true });
    ApiClient.getTools = async () => [
        toolRecord({
            tool_id: "exec",
            display_name: "Exec Runtime",
            description: "Execute shell commands.",
            risk_level: "high",
            destructive: true,
            allowed_roles: ["ceo", "execution"],
            metadata: { execution_mode: "full_access" },
        }),
    ];

    S.selectedTool = toolRecord({
        tool_id: "exec",
        display_name: "Exec Runtime",
        description: "Execute shell commands.",
        risk_level: "high",
        destructive: true,
        allowed_roles: ["ceo", "execution"],
        metadata: { execution_mode: "full_access" },
        exec_runtime_policy: { mode: "full_access" },
    });
    S.toolDirty = true;

    await saveTool({ showProgressToast: false, showSuccessToast: false, reopenDetail: false });

    assert.equal(calls.length, 1);
    assert.equal(calls[0].toolId, "exec");
    assert.equal(calls[0].payload.enabled, true);
    assert.deepEqual([...calls[0].payload.allowed_roles], ["ceo", "execution"]);
    assert.equal(calls[0].payload.execution_mode, "full_access");
    // 旧族形 payload（按 action_id 分组的角色）不再是合同的一部分。
    assert.equal(calls[0].payload.actions, undefined);
});

test("saveTool sends allowed_roles without execution_mode for a non-exec tool", async () => {
    const { S, saveTool, ApiClient } = loadResources();
    const calls = [];
    ApiClient.updateToolPolicy = async (toolId, payload) => {
        calls.push({ toolId, payload });
        return { item: { tool_id: toolId, allowed_roles: payload.allowed_roles } };
    };
    ApiClient.reloadResources = async () => ({ ok: true });
    ApiClient.getTools = async () => [];

    S.selectedTool = toolRecord({
        tool_id: "filesystem_write",
        display_name: "Filesystem Write",
        description: "Write file content to disk.",
        risk_level: "high",
        destructive: true,
        allowed_roles: ["ceo", "execution", "inspection"],
        enabled: false,
    });
    S.toolDirty = true;

    await saveTool({ showProgressToast: false, showSuccessToast: false, reopenDetail: false });

    assert.equal(calls.length, 1);
    assert.equal(calls[0].toolId, "filesystem_write");
    assert.equal(calls[0].payload.enabled, false);
    assert.deepEqual([...calls[0].payload.allowed_roles], ["ceo", "execution", "inspection"]);
    assert.equal(calls[0].payload.execution_mode, undefined);
});

test("applyExecToolExecutionMode updates localized summary for immediate rerender", () => {
    const { S, U, renderToolDetail, applyExecToolExecutionMode, execToolExecutionMode, execToolModeLabel, execToolModeSummary } = loadResources();
    S.selectedTool = toolRecord({
        tool_id: "exec",
        display_name: "Exec Runtime",
        description: "Execute shell commands.",
        risk_level: "high",
        destructive: true,
        allowed_roles: ["ceo", "execution"],
        metadata: { execution_mode: "full_access" },
        exec_runtime_policy: { mode: "full_access" },
    });

    assert.equal(execToolModeLabel("governed"), "监管模式");
    assert.equal(execToolModeLabel("full_access"), "全权限模式");
    assert.match(execToolModeSummary("governed"), /继续应用 exec 侧护栏/);
    assert.match(execToolModeSummary("full_access"), /不再应用 exec 侧护栏/);
    // 没有 policy 也没有 metadata 时回落 governed。
    assert.equal(execToolExecutionMode(toolRecord({ tool_id: "exec" })), "governed");

    applyExecToolExecutionMode(S.selectedTool, "governed");
    renderToolDetail();

    assert.equal(S.selectedTool.metadata.execution_mode, "governed");
    assert.equal(S.selectedTool.exec_runtime_policy.mode, "governed");
    assert.match(U.toolDetail.innerHTML, /继续应用 exec 侧护栏/);

    // 非 exec 工具不受影响：旧值 exec_runtime 已废弃。
    const other = toolRecord({ tool_id: "exec_runtime", display_name: "Exec Runtime Legacy" });
    applyExecToolExecutionMode(other, "governed");
    assert.equal(other.metadata.execution_mode, undefined);
});
