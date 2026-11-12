const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const { resolveFrontendSource } = require("./support/frontend_sources.js");

// 任务「模型配置」面板契约：
// 1) 默认跟随全局，两条车道独立；覆盖链与全局同形（direct + group: 记号）；
// 2) 草稿与全局逐位相同 ⇒ 保存载荷发空数组恢复跟随；
// 3) 任何操作都不反向同步全局：不进入 roleEditing、不写模型页草稿、
//    保存只走任务 PATCH；
// 4) 头部圆点按任务 metadata 的覆盖态亮灭；切任务重置。

const PANEL_CODE = fs.readFileSync(resolveFrontendSource("tasks/model-config-panel.js"), "utf8");

// vm 上下文里的数组/对象与测试域原型不同，deepStrictEqual 按原型判等；
// 结构断言一律先 JSON 往返脱域。
const plain = (value) => JSON.parse(JSON.stringify(value));

function makeBodyStub() {
    return {
        innerHTML: "",
        querySelector: () => null,
        querySelectorAll: () => [],
        addEventListener: () => {},
    };
}

function makeContext({ catalog, roles, groups, currentTask, api } = {}) {
    const calls = { patch: [], get: 0, drawer: [], toast: [] };
    const context = {
        console,
        Promise,
        JSON,
        Object,
        Array,
        String,
        Number,
        Element: class Element {},
        HTMLInputElement: class HTMLInputElement {},
        S: {
            currentTaskId: "task:1",
            currentTask: currentTask || { task_id: "task:1", metadata: {} },
            modelCatalog: {
                loading: false,
                catalog: catalog || [
                    { key: "m_a", name: "Alpha", provider_model: "alpha-model", enabled: true },
                    { key: "m_b", name: "Beta", provider_model: "beta-model", enabled: true },
                    { key: "m_off", name: "Off", provider_model: "off-model", enabled: false },
                ],
                roles: roles || { execution: ["m_a"], inspection: ["m_b"] },
                loadBalanceGroups: groups || {
                    g1: { enabled: true, max_retry_rounds: 2, model_keys: ["m_a", "m_b"] },
                },
            },
        },
        U: {
            taskModelConfigButton: { classList: { toggle: (name, on) => { context.__badge = { name, on }; } }, title: "" },
            taskModelConfigBackdrop: {},
            taskModelConfigDrawer: {},
            taskModelConfigClose: {},
            taskModelConfigSave: { disabled: false },
            taskModelConfigStatus: { textContent: "", classList: { toggle: () => {} } },
            taskModelConfigBody: makeBodyStub(),
        },
        esc: (value) => String(value ?? ""),
        icons: () => {},
        modelRefItem: (ref) => (context.S.modelCatalog.catalog || []).find((item) => item.key === String(ref || "").trim()) || null,
        modelDisplayTitle: (item) => String(item?.name || item?.key || "").trim(),
        isGroupRef: (ref) => String(ref || "").startsWith("group:"),
        groupKeyFromRef: (ref) => String(ref || "").slice("group:".length).trim(),
        groupRefToken: (key) => `group:${String(key || "").trim()}`,
        chainToRouteEntries: (chain) => (chain || []).map((ref) => String(ref || "").startsWith("group:")
            ? { type: "load_balance", group_key: String(ref).slice("group:".length).trim() }
            : { type: "model", model_key: String(ref || "").trim() }).filter((entry) => entry.group_key || entry.model_key),
        setDrawerOpen: (backdrop, drawer, open) => calls.drawer.push(open),
        showToast: (payload) => calls.toast.push(payload),
        ApiClient: {
            getTaskModelConfig: async () => { calls.get += 1; return api?.get || { execution: { follows_global: true, route_entries: [] }, inspection: { follows_global: true, route_entries: [] } }; },
            updateTaskModelConfig: async (taskId, payload) => { calls.patch.push({ taskId, payload }); return api?.patchResponse || { execution: { follows_global: true, route_entries: [] }, inspection: { follows_global: true, route_entries: [] } }; },
        },
        __calls: calls,
    };
    context.window = context;
    vm.createContext(context);
    vm.runInContext(PANEL_CODE, context);
    return context;
}

test("默认跟随全局：展示全局链（含组记号），保存载荷为空", () => {
    const context = makeContext({ roles: { execution: ["group:g1", "m_b"], inspection: ["m_a"] } });
    context.applyTaskModelConfigPayload({
        execution: { follows_global: true, route_entries: [] },
        inspection: { follows_global: true, route_entries: [] },
    });
    assert.deepEqual(plain(context.taskModelConfigLaneDisplayRefs("execution")), ["group:g1", "m_b"]);
    assert.equal(context.S.taskModelConfig.lanes.execution.editing, false);
    assert.deepEqual(plain(context.taskModelConfigSavePayload()), {});
    context.renderTaskModelConfigPanel();
    const html = context.U.taskModelConfigBody.innerHTML;
    assert.match(html, /跟随全局/);
    assert.match(html, /g1/);
    assert.match(html, /组定义在模型页维护/);
    // 跟随态不可编辑：没有移除按钮、没有拖拽索引。
    assert.doesNotMatch(html, /data-task-model-remove/);
    assert.doesNotMatch(html, /data-task-model-index/);
});

test("覆盖态：服务端 entries  seeds 草稿，未改动时保存载荷为空", () => {
    const context = makeContext();
    context.applyTaskModelConfigPayload({
        execution: { follows_global: false, route_entries: [{ type: "load_balance", group_key: "g1" }, { type: "model", model_key: "m_b" }] },
        inspection: { follows_global: true, route_entries: [] },
    });
    const lane = context.S.taskModelConfig.lanes.execution;
    assert.equal(lane.editing, true);
    assert.deepEqual(plain(lane.draftRefs), ["group:g1", "m_b"]);
    assert.deepEqual(plain(context.taskModelConfigSavePayload()), {});
    // 检验车道不受执行覆盖影响。
    assert.equal(context.S.taskModelConfig.lanes.inspection.editing, false);
});

test("编辑车道：增删移产生正确 entries；与全局相同自动恢复跟随", () => {
    const context = makeContext({ roles: { execution: ["m_a"], inspection: ["m_b"] } });
    context.applyTaskModelConfigPayload({
        execution: { follows_global: true, route_entries: [] },
        inspection: { follows_global: true, route_entries: [] },
    });
    context.setTaskModelConfigLaneMode("execution", "custom");
    // 进入单独配置以当前生效链为底稿（与模型页同一口径）。
    assert.deepEqual(plain(context.S.taskModelConfig.lanes.execution.draftRefs), ["m_a"]);
    context.addTaskModelConfigRef("execution", "group:g1");
    context.addTaskModelConfigRef("execution", "m_b");
    context.moveTaskModelConfigRef("execution", 2, 0);
    context.removeTaskModelConfigRef("execution", 1);
    assert.deepEqual(plain(context.S.taskModelConfig.lanes.execution.draftRefs), ["m_b", "group:g1"]);
    assert.deepEqual(plain(context.taskModelConfigSavePayload()), {
        execution_route_entries: [{ type: "model", model_key: "m_b" }, { type: "load_balance", group_key: "g1" }],
    });
    // 改回与全局逐位相同：服务端本无覆盖 ⇒ 净变化为零，不发该车道。
    context.S.taskModelConfig.lanes.execution.draftRefs = ["m_a"];
    assert.deepEqual(plain(context.taskModelConfigSavePayload()), {});
    // 服务端有覆盖时，草稿与全局相同 ⇒ 发空数组恢复跟随，而不是落恒等副本。
    context.applyTaskModelConfigPayload({
        execution: { follows_global: false, route_entries: [{ type: "model", model_key: "m_b" }] },
        inspection: { follows_global: true, route_entries: [] },
    });
    context.S.taskModelConfig.lanes.execution.draftRefs = ["m_a"];
    assert.deepEqual(plain(context.taskModelConfigSavePayload()), { execution_route_entries: [] });
});

test("切回跟随全局：有覆盖时发空数组清除，无覆盖时不发", () => {
    const context = makeContext();
    context.applyTaskModelConfigPayload({
        execution: { follows_global: false, route_entries: [{ type: "model", model_key: "m_b" }] },
        inspection: { follows_global: true, route_entries: [] },
    });
    context.setTaskModelConfigLaneMode("execution", "follow");
    assert.deepEqual(plain(context.taskModelConfigSavePayload()), { execution_route_entries: [] });
    context.applyTaskModelConfigPayload({
        execution: { follows_global: true, route_entries: [] },
        inspection: { follows_global: true, route_entries: [] },
    });
    assert.deepEqual(plain(context.taskModelConfigSavePayload()), {});
});

test("保存只走任务 PATCH；全局草稿与 roleEditing 零触碰", async () => {
    const context = makeContext();
    context.S.modelCatalog.roleDrafts = { execution: ["m_a"] };
    context.S.modelCatalog.loadBalanceGroupDrafts = { g1: { enabled: true, model_keys: ["m_a"] } };
    context.applyTaskModelConfigPayload({
        execution: { follows_global: true, route_entries: [] },
        inspection: { follows_global: true, route_entries: [] },
    });
    context.setTaskModelConfigLaneMode("inspection", "custom");
    context.addTaskModelConfigRef("inspection", "group:g1");
    await context.saveTaskModelConfig();
    assert.equal(context.__calls.patch.length, 1);
    assert.equal(context.__calls.patch[0].taskId, "task:1");
    // 底稿是全局检验链 m_b，追加组槽位 g1。
    assert.deepEqual(plain(context.__calls.patch[0].payload), {
        inspection_route_entries: [{ type: "model", model_key: "m_b" }, { type: "load_balance", group_key: "g1" }],
    });
    assert.equal(context.S.modelCatalog.roleEditing, undefined);
    assert.deepEqual(plain(context.S.modelCatalog.roleDrafts), { execution: ["m_a"] });
    assert.deepEqual(plain(context.S.modelCatalog.loadBalanceGroupDrafts), { g1: { enabled: true, model_keys: ["m_a"] } });
});

test("头部圆点按 metadata 覆盖态亮灭；切任务重置面板", () => {
    const context = makeContext();
    context.syncTaskModelConfigBadge();
    assert.deepEqual(context.__badge, { name: "has-override", on: false });
    context.S.currentTask.metadata = { model_config: { execution_route_entries: [{ type: "model", model_key: "m_b" }] } };
    context.syncTaskModelConfigBadge();
    assert.deepEqual(context.__badge, { name: "has-override", on: true });
    context.applyTaskModelConfigPayload({
        execution: { follows_global: false, route_entries: [{ type: "model", model_key: "m_b" }] },
        inspection: { follows_global: true, route_entries: [] },
    });
    // apply 就地同步 metadata：覆盖清除后圆点熄灭。
    context.applyTaskModelConfigPayload({
        execution: { follows_global: true, route_entries: [] },
        inspection: { follows_global: true, route_entries: [] },
    });
    assert.equal(context.S.currentTask.metadata.model_config, undefined);
    context.S.taskModelConfig.lanes.execution.editing = true;
    context.resetTaskModelConfigPanel();
    assert.equal(context.S.taskModelConfig.lanes.execution.editing, false);
    assert.equal(context.S.taskModelConfig.taskId, "");
});

test("添加选择器：禁用模型不可添加，组列表读 committed 态", () => {
    const context = makeContext();
    context.applyTaskModelConfigPayload({
        execution: { follows_global: true, route_entries: [] },
        inspection: { follows_global: true, route_entries: [] },
    });
    context.setTaskModelConfigLaneMode("execution", "custom");
    const modelsHtml = context.taskModelConfigPickerRows("execution");
    assert.match(modelsHtml, /data-task-model-add="m_a"/);
    assert.match(modelsHtml, /disabled[^>]*data-task-model-add="m_off"|data-task-model-add="m_off"[^>]*disabled/);
    context.S.taskModelConfig.lanes.execution.pickerTab = "groups";
    const groupsHtml = context.taskModelConfigPickerRows("execution");
    assert.match(groupsHtml, /data-task-model-add="group:g1"/);
    // 模型页草稿态的组不影响面板：面板只读 committed 组表。
    context.S.modelCatalog.roleEditing = true;
    context.S.modelCatalog.loadBalanceGroupDrafts = { g_draft: { enabled: true, model_keys: ["m_b"] } };
    const groupsHtml2 = context.taskModelConfigPickerRows("execution");
    assert.doesNotMatch(groupsHtml2, /g_draft/);
    assert.match(groupsHtml2, /group:g1/);
});

test("保存失败：错误进状态行与 toast，草稿保留", async () => {
    const context = makeContext();
    context.ApiClient.updateTaskModelConfig = async () => { throw new Error("模型 m_gone 不存在，请从链里移除后再保存。"); };
    context.applyTaskModelConfigPayload({
        execution: { follows_global: true, route_entries: [] },
        inspection: { follows_global: true, route_entries: [] },
    });
    context.setTaskModelConfigLaneMode("execution", "custom");
    context.addTaskModelConfigRef("execution", "m_b");
    await context.saveTaskModelConfig();
    assert.equal(context.S.taskModelConfig.saving, false);
    assert.match(context.S.taskModelConfig.error, /m_gone/);
    assert.equal(context.__calls.toast.length, 1);
    assert.equal(context.__calls.toast[0].kind, "error");
    assert.deepEqual(plain(context.S.taskModelConfig.lanes.execution.draftRefs), ["m_a", "m_b"]);
});

test("面板两列并排：body 是双轨 grid，列头固定、链身列内滚动", () => {
    const css = fs.readFileSync(resolveFrontendSource("org_graph.css"), "utf8");
    const bodyStart = css.indexOf(".task-model-config-body {");
    assert.ok(bodyStart > 0, "body rule missing");
    const bodyBlock = css.slice(bodyStart, css.indexOf("}", bodyStart));
    assert.match(bodyBlock, /display:\s*grid/);
    assert.match(bodyBlock, /grid-template-columns:\s*minmax\(0,\s*1fr\)\s+minmax\(0,\s*1fr\)/);
    assert.doesNotMatch(bodyBlock, /flex-direction:\s*column/);
    const laneStart = css.indexOf(".tmc-lane-scroll {");
    assert.ok(laneStart > 0, "lane scroll rule missing");
    const laneBlock = css.slice(laneStart, css.indexOf("}", laneStart));
    assert.match(laneBlock, /overflow-y:\s*auto/);
    // 渲染结构：两节各自带固定列头与独立滚动区。
    const context = makeContext();
    context.applyTaskModelConfigPayload({
        execution: { follows_global: true, route_entries: [] },
        inspection: { follows_global: true, route_entries: [] },
    });
    context.renderTaskModelConfigPanel();
    const html = context.U.taskModelConfigBody.innerHTML;
    assert.equal((html.match(/class="tmc-lane"/g) || []).length, 2);
    assert.equal((html.match(/class="tmc-lane-scroll"/g) || []).length, 2);
    assert.equal((html.match(/class="tmc-lane-head"/g) || []).length, 2);
});

test("client_api 载荷键与面板同形：snake_case entries 进 body，缺席车道不进", async () => {
    const code = fs.readFileSync(resolveFrontendSource("client_api/tasks.js"), "utf8");
    const context = { console };
    context.window = context;
    vm.createContext(context);
    vm.runInContext(code, context);
    const calls = [];
    const host = { _request: async (method, url, options) => { calls.push({ method, url, options }); return { ok: true }; } };
    const api = context.window.ApiDomains.tasks;
    await api.updateTaskModelConfig.call(host, "task:1", { execution_route_entries: [{ type: "model", model_key: "m_b" }] });
    await api.updateTaskModelConfig.call(host, "task:1", { inspection_route_entries: [] });
    await api.updateTaskModelConfig.call(host, "task:1", {});
    assert.equal(calls.length, 3);
    assert.equal(calls[0].method, "PATCH");
    assert.match(calls[0].url, /\/api\/tasks\/task:1\/model-config$/);
    assert.deepEqual(plain(calls[0].options.body), { execution_route_entries: [{ type: "model", model_key: "m_b" }] });
    assert.deepEqual(plain(calls[1].options.body), { inspection_route_entries: [] });
    assert.deepEqual(plain(calls[2].options.body), {});
});