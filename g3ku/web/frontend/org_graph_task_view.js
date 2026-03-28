// Task tree and task detail view layer extracted from org_graph_app.js.
// Loaded before org_graph_app.js and relies on globals initialized there at runtime.

function setTaskSelectionEmptyVisible(visible) {
    if (U.taskSelectionEmpty) U.taskSelectionEmpty.hidden = !visible;
}

function normalizeTreeRoundSelections(value) {
    const next = {};
    if (!value || typeof value !== "object") return next;
    Object.entries(value).forEach(([nodeId, roundId]) => {
        const normalizedNodeId = String(nodeId || "").trim();
        const normalizedRoundId = String(roundId || "").trim();
        if (!normalizedNodeId || !normalizedRoundId) return;
        next[normalizedNodeId] = normalizedRoundId;
    });
    return next;
}

function dedupeTreeNodes(nodes) {
    const seen = new Set();
    const out = [];
    (Array.isArray(nodes) ? nodes : []).forEach((node) => {
        const nodeId = String(node?.node_id || "").trim();
        const key = nodeId || `anon:${out.length}`;
        if (seen.has(key)) return;
        seen.add(key);
        out.push(node);
    });
    return out;
}

function rawNodeRounds(node) {
    return (Array.isArray(node?.spawn_rounds) ? node.spawn_rounds : [])
        .map((round) => ({
            ...round,
            round_id: String(round?.round_id || "").trim(),
            label: String(round?.label || "").trim(),
            children: dedupeTreeNodes(round?.children),
        }))
        .filter((round) => round.round_id);
}

function rawAuxiliaryChildren(node) {
    const explicitAuxiliary = dedupeTreeNodes(node?.auxiliary_children);
    if (explicitAuxiliary.length) return explicitAuxiliary;
    const directChildren = dedupeTreeNodes(node?.children);
    const rounds = rawNodeRounds(node);
    if (!directChildren.length || !rounds.length) return [];
    const roundChildIds = new Set(
        rounds.flatMap((round) => dedupeTreeNodes(round.children)
            .map((child) => String(child?.node_id || "").trim())
            .filter(Boolean)),
    );
    return directChildren.filter((child) => {
        const childId = String(child?.node_id || "").trim();
        return !childId || !roundChildIds.has(childId);
    });
}

function rawTreeDirectChildren(node) {
    const rounds = rawNodeRounds(node);
    const auxiliary = rawAuxiliaryChildren(node);
    const roundChildren = rounds.flatMap((round) => dedupeTreeNodes(round.children));
    if (auxiliary.length || roundChildren.length) return dedupeTreeNodes([...auxiliary, ...roundChildren]);
    return dedupeTreeNodes(node?.children);
}

function walkFullTaskTree(node, visitor, seen = new Set()) {
    if (!node) return;
    const nodeId = String(node?.node_id || "").trim();
    if (nodeId && seen.has(nodeId)) return;
    if (nodeId) seen.add(nodeId);
    visitor(node);
    rawTreeDirectChildren(node).forEach((child) => walkFullTaskTree(child, visitor, seen));
}

function findRawTaskTreeNode(node, nodeId, seen = new Set()) {
    if (!node) return null;
    const currentId = String(node?.node_id || "").trim();
    if (currentId && seen.has(currentId)) return null;
    if (currentId) seen.add(currentId);
    if (currentId === String(nodeId || "").trim()) return node;
    for (const child of rawTreeDirectChildren(node)) {
        const found = findRawTaskTreeNode(child, nodeId, seen);
        if (found) return found;
    }
    return null;
}

function pruneTreeRoundSelections(root, selections) {
    const source = normalizeTreeRoundSelections(selections);
    if (!root) return {};
    const next = {};
    walkFullTaskTree(root, (node) => {
        const nodeId = String(node?.node_id || "").trim();
        if (!nodeId || !source[nodeId]) return;
        const rounds = rawNodeRounds(node);
        if (rounds.some((round) => round.round_id === source[nodeId])) {
            next[nodeId] = source[nodeId];
        }
    });
    return next;
}

function resolveDefaultRoundId(node) {
    const rounds = rawNodeRounds(node);
    if (!rounds.length) return "";
    const explicitDefault = String(node?.default_round_id || "").trim();
    if (rounds.some((round) => round.round_id === explicitDefault)) return explicitDefault;
    return String(rounds.find((round) => round?.is_latest)?.round_id || rounds[rounds.length - 1]?.round_id || "");
}

function resolveSelectedRoundId(node, selections) {
    const rounds = rawNodeRounds(node);
    if (!rounds.length) return "";
    const nodeId = String(node?.node_id || "").trim();
    const selected = String(selections?.[nodeId] || "").trim();
    if (rounds.some((round) => round.round_id === selected)) return selected;
    return resolveDefaultRoundId(node);
}

function projectTaskTree(node, selections) {
    if (!node) return null;
    const rounds = rawNodeRounds(node);
    const projectedAuxiliaryChildren = rawAuxiliaryChildren(node)
        .map((child) => projectTaskTree(child, selections))
        .filter(Boolean);
    const projectedRounds = rounds.map((round) => ({
        ...round,
        children: dedupeTreeNodes(round.children).map((child) => projectTaskTree(child, selections)).filter(Boolean),
    }));
    const fallbackChildren = (!projectedAuxiliaryChildren.length && !projectedRounds.length)
        ? dedupeTreeNodes(node?.children).map((child) => projectTaskTree(child, selections)).filter(Boolean)
        : [];
    const selectedRoundId = resolveSelectedRoundId(node, selections);
    const selectedRound = projectedRounds.find((round) => round.round_id === selectedRoundId) || null;
    const projectedChildren = projectedRounds.length
        ? [...projectedAuxiliaryChildren, ...((selectedRound?.children) || [])]
        : (projectedAuxiliaryChildren.length ? projectedAuxiliaryChildren : fallbackChildren);
    return {
        ...node,
        auxiliary_children: projectedAuxiliaryChildren,
        spawn_rounds: projectedRounds,
        selected_round_id: selectedRoundId,
        children: projectedChildren,
    };
}

function countVisibleTreeNodes(root, predicate = null) {
    let count = 0;
    const walk = (node) => {
        if (!node) return;
        if (!predicate || predicate(node)) count += 1;
        treeViewChildren(node).forEach(walk);
    };
    walk(root);
    return count;
}

function analyzeExecutionTreeLayout(root) {
    const stats = {
        totalItems: 0,
        totalBoxes: 0,
        maxBreadth: 0,
        maxDepth: 0,
        rootChildren: Array.isArray(root?.children) ? root.children.length : 0,
    };
    if (!root) return stats;
    const breadthByDepth = new Map();
    const queue = [{ node: root, depth: 1 }];
    for (let index = 0; index < queue.length; index += 1) {
        const current = queue[index];
        const node = current?.node || null;
        const depth = Number(current?.depth || 1);
        if (!node) continue;
        const inspectionCount = Array.isArray(node?.inspectionNodes) ? node.inspectionNodes.length : 0;
        const children = Array.isArray(node?.children) ? node.children : [];
        stats.totalItems += 1;
        stats.totalBoxes += 1 + inspectionCount;
        stats.maxDepth = Math.max(stats.maxDepth, depth);
        breadthByDepth.set(depth, (breadthByDepth.get(depth) || 0) + 1);
        children.forEach((child) => queue.push({ node: child, depth: depth + 1 }));
    }
    breadthByDepth.forEach((count) => {
        if (count > stats.maxBreadth) stats.maxBreadth = count;
    });
    return stats;
}

function resolveExecutionTreeDensity(root) {
    const stats = analyzeExecutionTreeLayout(root);
    if (
        stats.maxBreadth >= 8
        || stats.totalItems >= 24
        || (stats.maxBreadth >= 6 && stats.totalBoxes >= 18)
        || (stats.rootChildren >= 7 && stats.totalItems >= 12)
    ) {
        return { mode: "dense", stats };
    }
    if (
        stats.maxBreadth >= 5
        || stats.totalItems >= 14
        || stats.totalBoxes >= 16
        || stats.rootChildren >= 5
    ) {
        return { mode: "wide", stats };
    }
    return { mode: "default", stats };
}

function hasManualTreeRoundSelections() {
    return Object.keys(normalizeTreeRoundSelections(S.treeRoundSelectionsByNodeId)).length > 0;
}

function taskDetailStatusLabel(task) {
    return ({ in_progress: "运行中", success: "已完成", failed: "失败", blocked: "已暂停", unknown: "未知" })[taskStatusKey(task)] || "未知";
}

function taskInitialPromptText(task = null, progress = null) {
    return String(task?.user_request || task?.title || task?.final_output || progress?.text || "暂无初始提示词").trim() || "暂无初始提示词";
}

function renderTaskDetailHeader({ resetPromptDisclosure = false } = {}) {
    const task = S.currentTask || null;
    const progress = S.currentTaskProgress || null;
    const promptText = taskInitialPromptText(task, progress);
    if (resetPromptDisclosure && U.tdPromptDisclosure) U.tdPromptDisclosure.open = false;
    if (U.tdTitle) {
        U.tdTitle.textContent = promptText;
        U.tdTitle.title = promptText;
    }
    if (U.tdStatus) U.tdStatus.textContent = taskDetailStatusLabel(task);
    if (U.tdStatusPill) U.tdStatusPill.dataset.status = taskStatusKey(task);
}

function syncTaskTreeHeaderState(projectedRoot = null) {
    const hasManual = hasManualTreeRoundSelections();
    if (U.tdActiveCount) {
        U.tdActiveCount.textContent = String(
            projectedRoot
                ? countVisibleTreeNodes(projectedRoot, (node) => String(node?.status || "").trim().toLowerCase() === "in_progress")
                : 0,
        );
    }
    if (U.taskTreeResetRounds) {
        U.taskTreeResetRounds.hidden = !hasManual;
        U.taskTreeResetRounds.disabled = !hasManual;
        U.taskTreeResetRounds.classList.toggle("active", hasManual);
        U.taskTreeResetRounds.title = hasManual
            ? "恢复所有节点的最新树视图"
            : "";
    }
}

function resetTaskTreeRoundSelections() {
    S.treeRoundSelectionsByNodeId = {};
    renderTree();
    scheduleTaskDetailSessionPersist();
}

function setNodeRoundSelection(nodeId, roundId) {
    const normalizedNodeId = String(nodeId || "").trim();
    if (!normalizedNodeId || !S.tree) return;
    const rawNode = findRawTaskTreeNode(S.tree, normalizedNodeId);
    if (!rawNode) return;
    const rounds = rawNodeRounds(rawNode);
    const normalizedRoundId = String(roundId || "").trim();
    const nextSelections = normalizeTreeRoundSelections(S.treeRoundSelectionsByNodeId);
    const defaultRoundId = resolveDefaultRoundId(rawNode);
    if (!normalizedRoundId || normalizedRoundId === defaultRoundId || !rounds.some((round) => round.round_id === normalizedRoundId)) {
        delete nextSelections[normalizedNodeId];
    } else {
        nextSelections[normalizedNodeId] = normalizedRoundId;
    }
    S.treeRoundSelectionsByNodeId = nextSelections;
    renderTree();
    scheduleTaskDetailSessionPersist();
}

function clearAgentSelection({ rerender = true } = {}) {
    const previousNodeId = String(S.selectedNodeId || "").trim();
    if (previousNodeId) stashTaskDetailViewState({ nodeId: previousNodeId });
    S.selectedNodeId = null;
    S.pendingTaskDetailRestore = null;
    U.feedTitle.textContent = "Node Details";
    hideAgent();
    setTaskSelectionEmptyVisible(true);
    scheduleTaskDetailSessionPersist();
    if (rerender) syncExecutionTreeSelection(previousNodeId, "");
}
function findTreeNode(node, nodeId) {
    if (!node) return null;
    if (String(node.node_id || "") === String(nodeId || "")) return node;
    for (const child of treeViewChildren(node)) {
        const found = findTreeNode(child, nodeId);
        if (found) return found;
    }
    return null;
}

function bindTreePan() {
    if (!U.tree || U.tree.dataset.panBound === "true") return;
    U.tree.dataset.panBound = "true";
    const state = S.treePan;
    const applyPan = () => {
        const canvas = U.tree?.querySelector(".execution-tree");
        if (canvas) {
            canvas.style.transformOrigin = "0 0";
            canvas.style.transform = `translate(${Math.round(state.offsetX)}px, ${Math.round(state.offsetY)}px) scale(${state.scale})`;
        }
    };
    window.addEventListener("mousemove", (e) => {
        if (!state.active) return;
        const dx = e.clientX - state.startX;
        const dy = e.clientY - state.startY;
        if (Math.abs(dx) > 4 || Math.abs(dy) > 4) state.moved = true;
        state.offsetX = state.baseOffsetX + dx;
        state.offsetY = state.baseOffsetY + dy;
        applyPan();
    });
    window.addEventListener("mouseup", () => {
        if (!state.active) return;
        window.setTimeout(() => { state.suppressClickNodeId = null; }, 0);
        state.active = false;
        state.baseOffsetX = state.offsetX;
        state.baseOffsetY = state.offsetY;
        U.tree.classList.remove("is-panning");
        if (state.moved) state.suppressClickNodeId = state.originNodeId;
    });
    U.tree.addEventListener("mousedown", (e) => {
        if (e.target.closest(".execution-tree-node, .execution-tree-node-rounds")) return;
        state.active = true;
        state.moved = false;
        state.originNodeId = e.target instanceof Element ? e.target.closest(".execution-tree-node")?.dataset?.id || null : null;
        state.startX = e.clientX;
        state.startY = e.clientY;
        U.tree.classList.add("is-panning");
    });
    U.tree.addEventListener("dragstart", (e) => {
        if (e.target.closest(".execution-tree-node")) e.preventDefault();
    });
    U.tree.addEventListener("wheel", (e) => {
        if (e.target.closest(".execution-tree-node-rounds")) return;
        const canvas = U.tree?.querySelector(".execution-tree");
        if (!canvas) return;
        e.preventDefault();
        const rect = U.tree.getBoundingClientRect();
        const pointerX = e.clientX - rect.left;
        const pointerY = e.clientY - rect.top;
        const nextScale = clamp(
            e.deltaY < 0 ? state.scale * TREE_SCALE_FACTOR : state.scale / TREE_SCALE_FACTOR,
            TREE_SCALE_MIN,
            TREE_SCALE_MAX,
        );
        if (Math.abs(nextScale - state.scale) < 0.001) return;
        const contentX = (pointerX - state.offsetX) / state.scale;
        const contentY = (pointerY - state.offsetY) / state.scale;
        state.scale = nextScale;
        state.baseScale = nextScale;
        state.offsetX = pointerX - contentX * nextScale;
        state.offsetY = pointerY - contentY * nextScale;
        state.baseOffsetX = state.offsetX;
        state.baseOffsetY = state.offsetY;
        applyPan();
    }, { passive: false });
    U.tree.__applyPan = applyPan;
}

function nodeOutputText(node) {
    if (!node) return "";
    if (typeof node.output === "string") return node.output;
    if (Array.isArray(node.output)) return node.output.map((item) => String(item.content || "").trim()).filter(Boolean).join("\n\n");
    return String(node.final_output || "");
}

function truncateNodeTitle(text, maxChars = 20) {
    const chars = Array.from(String(text || ""));
    if (chars.length <= maxChars) return chars.join("");
    return `${chars.slice(0, maxChars).join("")}…`;
}

function resolveNodeTitle(node, detail) {
    const nodeKind = String(detail?.node_kind || node?.node_kind || "").trim().toLowerCase();
    const goal = String(detail?.goal || "").trim();
    const rawTitle = goal || String(node?.title || node?.node_id || "").trim() || String(node?.node_id || "");
    const acceptanceTitle = rawTitle.replace(/^accept\s*:\s*/i, "").trim();
    const fullTitle = nodeKind === "acceptance"
        ? (acceptanceTitle ? `检验 · ${acceptanceTitle}` : "检验")
        : rawTitle;
    return {
        goal: nodeKind === "acceptance" ? fullTitle : (goal || fullTitle),
        fullTitle,
        title: truncateNodeTitle(fullTitle, 20),
    };
}

function isAcceptanceNodeKind(kind) {
    return String(kind || "").trim().toLowerCase() === "acceptance";
}

function isInspectionActiveStatus(status) {
    return ["queued", "running", "pending", "waiting", "in_progress"].includes(String(status || "").trim().toLowerCase());
}

function resolveTreeNodeStatusLabel(status, { kind = "", inspectionActive = false } = {}) {
    if (inspectionActive || (isAcceptanceNodeKind(kind) && isInspectionActiveStatus(status))) {
        return { visualState: "inspecting", displayState: "检验中" };
    }
    const normalizedStatus = String(status || "").trim().toLowerCase() || "unknown";
    return {
        visualState: normalizedStatus,
        displayState: normalizedStatus.toUpperCase(),
    };
}

function treeViewChildren(node) {
    return [
        ...(Array.isArray(node?.inspectionNodes) ? node.inspectionNodes : []),
        ...(Array.isArray(node?.children) ? node.children : []),
    ];
}

function compactNodeHeading(node, maxChars = 72) {
    const raw = String(node?.goal || node?.fullTitle || node?.title || node?.node_id || "Node");
    const singleLine = raw
        .replace(/\r\n|\r|\n/g, " ")
        .replace(/\s+/g, " ")
        .trim();
    if (!singleLine) return "Node";
    const chars = Array.from(singleLine);
    if (chars.length <= maxChars) return singleLine;
    return `${chars.slice(0, maxChars).join("")}...`;
}

function liveFramesByNodeId(progress) {
    const frames = Array.isArray(progress?.live_state?.frames) ? progress.live_state.frames : [];
    return new Map(
        frames
            .map((frame) => [String(frame?.node_id || "").trim(), frame])
            .filter(([nodeId]) => !!nodeId),
    );
}

function normalizeLiveToolCalls(frame) {
    return (Array.isArray(frame?.tool_calls) ? frame.tool_calls : [])
        .map((item) => ({
            tool_call_id: String(item?.tool_call_id || ""),
            tool_name: String(item?.tool_name || "tool"),
            status: ["queued", "running", "success", "error"].includes(String(item?.status || ""))
                ? String(item.status)
                : "info",
            started_at: String(item?.started_at || ""),
            finished_at: String(item?.finished_at || ""),
            elapsed_seconds: Number.isFinite(Number(item?.elapsed_seconds)) ? Number(item.elapsed_seconds) : null,
        }))
        .filter((item) => item.tool_call_id || item.tool_name);
}

function normalizeLiveChildPipelines(frame) {
    return (Array.isArray(frame?.child_pipelines) ? frame.child_pipelines : [])
        .map((item, index) => ({
            index: normalizeInt(item?.index, index),
            goal: String(item?.goal || ""),
            status: ["queued", "running", "success", "error"].includes(String(item?.status || ""))
                ? String(item.status)
                : "info",
            child_node_id: String(item?.child_node_id || ""),
            acceptance_node_id: String(item?.acceptance_node_id || ""),
            check_status: String(item?.check_status || ""),
            started_at: String(item?.started_at || ""),
            finished_at: String(item?.finished_at || ""),
        }))
        .filter((item) => item.goal || item.child_node_id || item.acceptance_node_id);
}

function buildLiveSectionStatus(items) {
    if (!Array.isArray(items) || !items.length) return "info";
    if (items.some((item) => String(item?.status || "") === "running" || String(item?.status || "") === "queued")) {
        return "running";
    }
    if (items.some((item) => String(item?.status || "") === "error")) {
        return "error";
    }
    if (items.some((item) => String(item?.status || "") === "success")) {
        return "success";
    }
    return "info";
}

function buildNodeExecutionTrace(node, detail, liveFrame = null) {
    const source = detail?.execution_trace && typeof detail.execution_trace === "object" ? detail.execution_trace : {};
    const toolSteps = Array.isArray(source.tool_steps) ? source.tool_steps : [];
    const stages = (Array.isArray(source.stages) ? source.stages : []).map((stage, index) => normalizeExecutionStageTrace(stage, index));
    const initialPrompt = [source.initial_prompt, detail?.prompt, detail?.goal, node?.prompt, node?.goal, node?.input]
        .map((value) => String(value ?? ""))
        .find((value) => value.trim())
        || "";
    return {
        initial_prompt: initialPrompt,
        tool_steps: toolSteps.map((step) => ({
            tool_call_id: String(step?.tool_call_id || ""),
            tool_name: String(step?.tool_name || "tool"),
            arguments_text: String(step?.arguments_text || ""),
            output_text: String(step?.output_text || ""),
            started_at: String(step?.started_at || ""),
            finished_at: String(step?.finished_at || ""),
            elapsed_seconds: Number.isFinite(Number(step?.elapsed_seconds)) ? Number(step.elapsed_seconds) : null,
            status: ["running", "success", "error"].includes(String(step?.status || ""))
                ? String(step.status)
                : "info",
        })),
        stages,
        live_tool_calls: normalizeLiveToolCalls(liveFrame),
        live_child_pipelines: normalizeLiveChildPipelines(liveFrame),
        final_output: String(source.final_output ?? detail?.final_output ?? node?.final_output ?? ""),
        acceptance_result: String(source.acceptance_result ?? detail?.check_result ?? node?.check_result ?? ""),
    };
}

function normalizeExecutionStageTrace(stage, index = 0) {
    const rounds = Array.isArray(stage?.rounds) ? stage.rounds : [];
    return {
        stage_id: String(stage?.stage_id || ""),
        stage_index: normalizeInt(stage?.stage_index, index + 1),
        mode: String(stage?.mode || "自主执行").trim() || "自主执行",
        status: String(stage?.status || "进行中").trim() || "进行中",
        stage_goal: String(stage?.stage_goal || "").trim(),
        stage_total_steps: normalizeInt(stage?.tool_round_budget, 0),
        tool_rounds_used: normalizeInt(stage?.tool_rounds_used, 0),
        created_at: String(stage?.created_at || ""),
        finished_at: String(stage?.finished_at || ""),
        rounds: rounds.map((round, roundIndex) => ({
            round_id: String(round?.round_id || ""),
            round_index: normalizeInt(round?.round_index, roundIndex + 1),
            created_at: String(round?.created_at || ""),
            budget_counted: !!round?.budget_counted,
            tools: (Array.isArray(round?.tools) ? round.tools : []).map((step) => ({
                tool_call_id: String(step?.tool_call_id || ""),
                tool_name: String(step?.tool_name || "tool"),
                arguments_text: String(step?.arguments_text || ""),
                output_text: String(step?.output_text || ""),
                started_at: String(step?.started_at || ""),
                finished_at: String(step?.finished_at || ""),
                elapsed_seconds: Number.isFinite(Number(step?.elapsed_seconds)) ? Number(step.elapsed_seconds) : null,
                status: ["running", "success", "error"].includes(String(step?.status || ""))
                    ? String(step.status)
                    : "info",
            })),
        })),
    };
}

function stageTraceStatus(stage) {
    return ({
        "进行中": "running",
        "完成": "success",
        "失败": "error",
    }[String(stage?.status || "").trim()] || "info");
}

function roundTraceStatus(round) {
    const tools = Array.isArray(round?.tools) ? round.tools : [];
    return buildLiveSectionStatus(tools);
}

function nodeFinalTraceStatus(node) {
    if (String(node?.state || "") === "in_progress") return "running";
    if (String(node?.state || "") === "failed") return "error";
    return "success";
}

function renderTraceStep({ traceKey = "", title, status = "info", open = false, bodyHtml = "", showRuntime = true }) {
    return `
        <details class="interaction-step task-trace-step ${esc(status)}" data-trace-key="${esc(traceKey)}" data-default-open="${open ? "true" : "false"}"${open ? " open" : ""}>
            <summary class="task-trace-summary">
                <span class="interaction-step-lead">
                    <span class="interaction-step-title">${esc(title)}</span>
                </span>
                <span class="interaction-step-side">
                    ${showRuntime ? '<span class="task-trace-runtime" hidden></span>' : ''}
                    <span class="interaction-step-status">${esc(traceStatusLabel(status))}</span>
                </span>
            </summary>
            <div class="task-trace-body">${bodyHtml}</div>
        </details>
    `;
}

function resolveTraceStepOpenState(step, state, index) {
    if (!state || typeof state !== "object") return !!step.open;
    const traceItems = Array.isArray(state.traceItems) ? state.traceItems : [];
    const stepKey = String(step.traceKey || "").trim();
    if (stepKey) {
        const keyed = traceItems.find((item) => String(item?.key || "").trim() === stepKey);
        if (keyed && typeof keyed.open === "boolean") return keyed.open;
    }
    const stepTitle = String(step.title || "").trim();
    if (stepTitle) {
        const titled = traceItems.find((item) => String(item?.title || "").trim() === stepTitle);
        if (titled && typeof titled.open === "boolean") return titled.open;
    }
    if (typeof traceItems[index]?.open === "boolean") return traceItems[index].open;
    return !!step.open;
}

function renderTraceMessage(value, emptyText = "暂无内容") {
    const text = String(value || "").trim() || emptyText;
    return `
        <div class="task-trace-field">
            <div class="code-block task-trace-code">${esc(text)}</div>
        </div>
    `;
}

function renderExecutionStageRounds(stage) {
    const rounds = Array.isArray(stage?.rounds) ? stage.rounds : [];
    if (!rounds.length) {
        return renderTraceField("阶段轮次", "", "当前阶段暂无工具轮次");
    }
    return rounds.map((round, index) => {
        const title = round.budget_counted
            ? `第 ${round.round_index || index + 1} 轮`
            : "派生节点";
        const tools = Array.isArray(round.tools) ? round.tools : [];
        const toolDetails = tools.length
            ? tools.map((step, toolIndex) => renderTraceStep({
                traceKey: `stage:${stage.stage_id || stage.stage_index}:round:${round.round_id || round.round_index}:tool:${step.tool_call_id || toolIndex}`,
                title: `工具 · ${step.tool_name || "tool"}`,
                status: step.status || "info",
                open: false,
                bodyHtml: [
                    renderTraceField("参数", step.arguments_text, "无参数"),
                    renderTraceField(
                        "工具输出",
                        step.output_text,
                        step.status === "running" ? "等待工具输出..." : "暂无工具输出",
                        { decodeEscapes: true },
                    ),
                ].join(""),
            })).join("")
            : renderTraceField("工具", "", "本轮暂无工具记录");
        return renderTraceStep({
            traceKey: `stage:${stage.stage_id || stage.stage_index}:round:${round.round_id || round.round_index}`,
            title: `${title}${round.created_at ? ` · ${formatCompactTime(round.created_at)}` : ""}`,
            status: roundTraceStatus(round),
            open: false,
            bodyHtml: toolDetails,
        });
    }).join("");
}

function buildExecutionTraceSteps(trace, node) {
    const initialPromptStep = {
        traceKey: "initial_prompt",
        title: "Initial Prompt",
        status: "info",
        open: false,
        bodyHtml: renderTraceField("Content", trace.initial_prompt, "No initial prompt"),
    };
    if (Array.isArray(trace?.stages) && trace.stages.length) {
        return [
            initialPromptStep,
            ...trace.stages.map((stage, index) => ({
                traceKey: `stage:${stage.stage_id || stage.stage_index || index}`,
                title: `${stage.mode || "自主执行"}${stage.created_at ? ` · ${formatCompactTime(stage.created_at)}` : ""}`,
                status: stageTraceStatus(stage),
                open: index === trace.stages.length - 1,
                bodyHtml: [
                    renderTraceMessage(`本阶段最大轮数为${stage.stage_total_steps || 0}`, "本阶段最大轮数为0"),
                    renderTraceField("状态", String(stage.status || "进行中"), "进行中"),
                    renderTraceField("阶段目标", stage.stage_goal, "暂无阶段目标"),
                    renderExecutionStageRounds(stage),
                ].join(""),
            })),
        ];
    }
    return [
        initialPromptStep,
        ...trace.tool_steps.map((step, index) => ({
            traceKey: `tool:${step.tool_call_id || index}:${step.tool_name || "tool"}`,
            title: `Tool - ${step.tool_name || "tool"}`,
            status: step.status || "info",
            open: false,
            bodyHtml: [
                renderTraceField("Arguments", step.arguments_text, "No arguments"),
                renderTraceField(
                    "Output",
                    step.output_text,
                    step.status === "running" ? "Waiting for tool output..." : "No tool output",
                    { decodeEscapes: true },
                ),
            ].join(""),
        })),
    ];
}

function toPixels(value) {
    const parsed = Number.parseFloat(String(value || ""));
    return Number.isFinite(parsed) ? parsed : 0;
}

function setScrollViewportLimit(container, itemSelector, visibleCount, measureItem = null, fillToVisibleCount = false) {
    if (!(container instanceof HTMLElement)) return;
    container.style.height = "";
    container.style.maxHeight = "";
    const items = Array.from(container.querySelectorAll(itemSelector)).filter((item) => item instanceof HTMLElement);
    if (!items.length || visibleCount <= 0) return;
    const styles = window.getComputedStyle(container);
    const gap = toPixels(styles.rowGap || styles.gap);
    const targetCount = Math.min(visibleCount, items.length);
    const measuredHeights = [];
    let height = 0;
    for (let index = 0; index < targetCount; index += 1) {
        const item = items[index];
        const measured = measureItem ? Number(measureItem(item, index)) : item.getBoundingClientRect().height;
        const resolvedHeight = measured > 0 ? measured : 0;
        measuredHeights.push(resolvedHeight);
        height += resolvedHeight;
        if (index < targetCount - 1) height += gap;
    }
    if (fillToVisibleCount && measuredHeights.length) {
        const averageHeight = measuredHeights.reduce((sum, value) => sum + value, 0) / measuredHeights.length;
        const remainingCount = Math.max(0, visibleCount - measuredHeights.length);
        if (remainingCount > 0) {
            height += averageHeight * remainingCount;
            height += gap * remainingCount;
        }
    }
    if (height > 0) {
        const resolvedHeight = `${Math.ceil(height)}px`;
        if (fillToVisibleCount) container.style.height = resolvedHeight;
        container.style.maxHeight = resolvedHeight;
    }
}

function traceStepSummaryHeight(step) {
    if (!(step instanceof HTMLElement)) return 0;
    const summary = step.querySelector(".task-trace-summary");
    const summaryHeight = summary instanceof HTMLElement ? summary.getBoundingClientRect().height : step.getBoundingClientRect().height;
    const styles = window.getComputedStyle(step);
    return summaryHeight
        + toPixels(styles.paddingTop)
        + toPixels(styles.paddingBottom)
        + toPixels(styles.borderTopWidth)
        + toPixels(styles.borderBottomWidth);
}

function refreshTaskDetailScrollRegions() {
    const traceList = U.adFlow?.querySelector(".task-trace-list");
    const preservedDetailScrollTop = U.detail instanceof HTMLElement ? U.detail.scrollTop : null;
    const preservedTraceScrollTop = traceList instanceof HTMLElement ? traceList.scrollTop : null;
    const preservedArtifactListScrollTop = U.artifactList instanceof HTMLElement ? U.artifactList.scrollTop : null;
    if (traceList instanceof HTMLElement) {
        setScrollViewportLimit(traceList, ".task-trace-step", 10, traceStepSummaryHeight, true);
    }
    if (U.artifactList instanceof HTMLElement) {
        setScrollViewportLimit(U.artifactList, ".artifact-item", 5);
    }
    const restoreScrollPositions = () => {
        if (preservedDetailScrollTop !== null) setElementScrollTop(U.detail, preservedDetailScrollTop);
        if (preservedTraceScrollTop !== null) setElementScrollTop(traceList, preservedTraceScrollTop);
        if (preservedArtifactListScrollTop !== null) setElementScrollTop(U.artifactList, preservedArtifactListScrollTop);
    };
    restoreScrollPositions();
    window.requestAnimationFrame(restoreScrollPositions);
}

function renderExecutionTrace(node) {
    if (!U.adFlow) return;
    const trace = node?.executionTrace || buildNodeExecutionTrace(node, node);
    const steps = [
        renderTraceStep({
            title: "初始提示词",
            status: "info",
            open: false,
            bodyHtml: renderTraceField("内容", trace.initial_prompt, "暂无初始提示词"),
        }),
        ...trace.tool_steps.map((step) => renderTraceStep({
            title: `使用工具 · ${step.tool_name || "tool"}`,
            status: step.status || "info",
            open: false,
            bodyHtml: [
                renderTraceField("参数", step.arguments_text, "无参数"),
                renderTraceField("工具输出", step.output_text, step.status === "running" ? "等待工具输出…" : "暂无工具输出"),
            ].join(""),
        })),
        renderTraceStep({
            title: "最终输出",
            status: nodeFinalTraceStatus(node),
            open: true,
            bodyHtml: renderTraceField("内容", trace.final_output, "暂无最终输出"),
        }),
    ];
    U.adFlow.innerHTML = `<div class="task-trace-list">${steps.join("")}</div>`;
    const traceItems = Array.from(U.adFlow.querySelectorAll(".task-trace-step"));
    trace.tool_steps.forEach((step, index) => {
        const item = traceItems[index + 1];
        if (!(item instanceof HTMLElement)) return;
        item.dataset.traceStatus = String(step.status || "info");
        if (step.started_at) item.dataset.startedAt = step.started_at;
        if (step.finished_at) item.dataset.finishedAt = step.finished_at;
        if (Number.isFinite(step.elapsed_seconds)) item.dataset.elapsedSeconds = String(step.elapsed_seconds);
        const runtimeEl = item.querySelector(".task-trace-runtime");
        if (runtimeEl instanceof HTMLElement) updateRuntimeBadge(item, runtimeEl);
    });
    refreshTaskDetailScrollRegions();
    refreshLiveDurationBadges();
}

function renderAcceptanceResult(text) {
    if (!U.adAcceptance) return;
    U.adAcceptance.textContent = String(text || "").trim() || "暂无验收结果";
}

function traceStatusLabel(status) {
    return ({
        info: "Info",
        running: "Running",
        success: "Success",
        error: "Error",
    }[String(status || "")] || "Info");
}

function renderTraceField(label, value, emptyText = "No content", { decodeEscapes = false } = {}) {
    const text = readableText(value, { decodeEscapes, emptyText });
    return `
        <div class="task-trace-field">
            <div class="task-trace-label">${esc(label)}</div>
            <div class="code-block task-trace-code">${esc(text)}</div>
        </div>
    `;
}

function renderLiveToolFields(toolCalls) {
    return toolCalls.map((step, index) => [
        renderTraceField(`Tool ${index + 1}`, `${step.tool_name || "tool"} [${step.status}]`, "No tool name"),
        renderTraceField(
            "Timing",
            [step.started_at, step.finished_at, Number.isFinite(step.elapsed_seconds) ? `${step.elapsed_seconds}s` : ""].filter(Boolean).join(" | "),
            "No timing",
        ),
    ].join("")).join("");
}

function renderLiveChildFields(childPipelines) {
    return childPipelines.map((step, index) => [
        renderTraceField(`Pipeline ${index + 1}`, `${step.goal || "(no goal)"} [${step.status}]`, "No child goal"),
        renderTraceField(
            "Nodes",
            [step.child_node_id ? `child=${step.child_node_id}` : "", step.acceptance_node_id ? `accept=${step.acceptance_node_id}` : ""]
                .filter(Boolean)
                .join(" | "),
            "No node ids yet",
        ),
        renderTraceField(
            "Check",
            `${step.check_status || "pending"}${step.started_at || step.finished_at ? ` | ${[step.started_at, step.finished_at].filter(Boolean).join(" -> ")}` : ""}`,
            "No check state",
        ),
    ].join("")).join("");
}

function buildTraceRenderSignature(stepDescriptors) {
    return (Array.isArray(stepDescriptors) ? stepDescriptors : []).map((step) => [
        String(step?.traceKey || ""),
        String(step?.title || ""),
        String(step?.status || ""),
        String(step?.bodyHtml || ""),
    ].join("\u0002")).join("\u0001");
}

function renderExecutionTrace(node, { viewState = null } = {}) {
    if (!U.adFlow) return;
    const effectiveViewState = normalizeTaskDetailViewState(viewState || captureTaskDetailViewState());
    const preservedTraceScrollTop = Number(effectiveViewState?.traceScrollTop || 0);
    const liveFrameMap = liveFramesByNodeId(S.currentTaskProgress);
    const fallbackLiveFrame = liveFrameMap.get(String(node?.node_id || "").trim()) || null;
    const trace = node?.executionTrace || buildNodeExecutionTrace(node, node, fallbackLiveFrame);
    const stepDescriptors = buildExecutionTraceSteps(trace, node);
    const steps = stepDescriptors.map((step, index) => renderTraceStep({
        ...step,
        open: resolveTraceStepOpenState(step, effectiveViewState, index),
    }));
    let traceList = U.adFlow.querySelector(".task-trace-list");
    if (!(traceList instanceof HTMLElement)) {
        traceList = document.createElement("div");
        traceList.className = "task-trace-list";
        U.adFlow.innerHTML = "";
        U.adFlow.appendChild(traceList);
    }
    const renderSignature = buildTraceRenderSignature(stepDescriptors);
    const renderNodeId = String(node?.node_id || "").trim();
    const shouldReplace = traceList.dataset.renderSignature !== renderSignature
        || traceList.dataset.renderNodeId !== renderNodeId;
    if (shouldReplace) {
        traceList.innerHTML = steps.join("");
        traceList.dataset.renderSignature = renderSignature;
        traceList.dataset.renderNodeId = renderNodeId;
    }
    renderFlowHeading(stepDescriptors.length);
    const traceItems = Array.from(traceList.querySelectorAll(".task-trace-step"));
    traceItems.forEach((item) => {
        if (!(item instanceof HTMLElement)) return;
        const runtimeEl = item.querySelector(".task-trace-runtime");
        if (runtimeEl instanceof HTMLElement) updateRuntimeBadge(item, runtimeEl);
    });
    refreshTaskDetailScrollRegions();
    if (effectiveViewState && shouldReplace) {
        const restoreTraceScroll = () => {
            const currentTraceList = U.adFlow?.querySelector(".task-trace-list");
            if (!(currentTraceList instanceof HTMLElement)) return;
            setElementScrollTop(currentTraceList, preservedTraceScrollTop);
        };
        restoreTraceScroll();
        window.requestAnimationFrame(() => {
            restoreTraceScroll();
            window.requestAnimationFrame(restoreTraceScroll);
        });
    }
    refreshLiveDurationBadges();
    return shouldReplace;
}

function renderFinalOutput(text) {
    if (!U.adOutput) return;
    const nextText = readableText(text, { decodeEscapes: true, emptyText: "暂无最终输出" });
    const changed = U.adOutput.textContent !== nextText;
    if (changed) U.adOutput.textContent = nextText;
    return changed;
}

function renderAcceptanceResult(text) {
    if (!U.adAcceptance) return;
    const nextText = readableText(text, { decodeEscapes: true, emptyText: "暂无验收结果" });
    const changed = U.adAcceptance.textContent !== nextText;
    if (changed) U.adAcceptance.textContent = nextText;
    return changed;
}

function formatRoundLabel(label, roundIndex) {
    const normalizedLabel = String(label || "").trim();
    const normalizedIndex = normalizeInt(roundIndex, 0);
    const fallbackLabel = normalizedIndex > 0 ? `第${normalizedIndex}轮树` : "轮次";
    if (!normalizedLabel) return fallbackLabel;
    const matchedRound = normalizedLabel.match(/^Round\s+(\d+)$/i);
    if (matchedRound) return `第${matchedRound[1]}轮树`;
    return normalizedLabel;
}

function buildNodeRoundState(node) {
    const rounds = rawNodeRounds(node).map((round) => ({
        roundId: String(round.round_id || ""),
        roundIndex: normalizeInt(round.round_index, 0),
        label: formatRoundLabel(round.label, round.round_index),
        isLatest: !!round.is_latest,
        childCount: Array.isArray(round.children) ? round.children.length : Math.max(0, normalizeInt(round.child_node_ids?.length, 0)),
        createdAt: String(round.created_at || ""),
        totalChildren: Math.max(0, normalizeInt(round.total_children, Array.isArray(round.children) ? round.children.length : 0)),
        completedChildren: Math.max(0, normalizeInt(round.completed_children, 0)),
        runningChildren: Math.max(0, normalizeInt(round.running_children, 0)),
        failedChildren: Math.max(0, normalizeInt(round.failed_children, 0)),
    }));
    if (!rounds.length) {
        return {
            options: [],
            selectedRoundId: "",
            defaultRoundId: "",
            summary: "No child rounds",
        };
    }
    const defaultRoundId = resolveDefaultRoundId(node);
    const selectedRoundId = String(node?.selected_round_id || defaultRoundId || "");
    const selectedRound = rounds.find((round) => round.roundId === selectedRoundId) || rounds[rounds.length - 1];
    const selectionMode = selectedRound.roundId && selectedRound.roundId !== defaultRoundId ? "manual" : "latest";
    const totalChildren = selectedRound.totalChildren || selectedRound.childCount;
    const counts = [
        `${selectedRound.completedChildren}/${totalChildren || selectedRound.childCount} completed`,
        selectedRound.runningChildren ? `${selectedRound.runningChildren} running` : "",
        selectedRound.failedChildren ? `${selectedRound.failedChildren} failed` : "",
    ].filter(Boolean).join(", ");
    return {
        options: rounds,
        selectedRoundId,
        defaultRoundId,
        summary: `${selectedRound.label}${selectedRound.isLatest ? " (latest)" : ""} | ${selectionMode} | ${counts || `${selectedRound.childCount} children`} | ${rounds.length} rounds`,
    };
}

function buildExecutionTree(rawTree) {
    if (!rawTree) return null;
    const nodeRecords = Array.isArray(S.currentTaskProgress?.nodes) ? S.currentTaskProgress.nodes : [];
    const nodeMap = new Map(nodeRecords.map((item) => [String(item.node_id || ""), item]));
    const liveFrameMap = liveFramesByNodeId(S.currentTaskProgress);
    const walk = (node) => {
        const nodeId = String(node.node_id || "");
        const detail = nodeMap.get(nodeId) || {};
        const kind = String(detail.node_kind || node.node_kind || "execution").trim().toLowerCase() || "execution";
        const status = String(node.status || detail.status || "unknown").trim().toLowerCase() || "unknown";
        const title = resolveNodeTitle(node, detail);
        const roundState = buildNodeRoundState(node);
        const allChildren = Array.isArray(node.children) ? node.children.map(walk) : [];
        const inspectionNodes = [];
        const childNodes = [];
        allChildren.forEach((child) => {
            if (isAcceptanceNodeKind(child?.kind)) inspectionNodes.push(child);
            else childNodes.push(child);
        });
        const inspectionActive = inspectionNodes.some((child) => isInspectionActiveStatus(child?.state || child?.visual_state));
        const stateMeta = resolveTreeNodeStatusLabel(status, { kind, inspectionActive });
        return {
            node_id: node.node_id,
            title: title.title,
            fullTitle: title.fullTitle,
            goal: title.goal,
            kind,
            state: status,
            visual_state: stateMeta.visualState,
            display_state: stateMeta.displayState,
            executionTrace: buildNodeExecutionTrace(node, detail, liveFrameMap.get(nodeId) || null),
            roundOptions: roundState.options,
            selectedRoundId: roundState.selectedRoundId,
            defaultRoundId: roundState.defaultRoundId,
            roundSummary: roundState.summary,
            inspectionNodes,
            children: childNodes,
        };
    };
    return walk(rawTree);
}

function executionTreeNodeSelector(nodeId) {
    const key = String(nodeId || "").trim();
    if (!key || !U.tree) return "";
    const escaped = key
        .replace(/\\/g, "\\\\")
        .replace(/"/g, '\\"');
    return `.execution-tree-node[data-id="${escaped}"]`;
}

function syncExecutionTreeSelection(previousNodeId, nextNodeId) {
    if (!U.tree) return;
    const previousKey = String(previousNodeId || "").trim();
    const nextKey = String(nextNodeId || "").trim();
    if (previousKey) {
        const previousButton = U.tree.querySelector(executionTreeNodeSelector(previousKey));
        if (previousButton instanceof HTMLElement) {
            previousButton.classList.remove("selected");
            previousButton.setAttribute("aria-pressed", "false");
        }
    }
    if (nextKey) {
        const nextButton = U.tree.querySelector(executionTreeNodeSelector(nextKey));
        if (nextButton instanceof HTMLElement) {
            nextButton.classList.add("selected");
            nextButton.setAttribute("aria-pressed", "true");
        }
    }
}

function buildTaskTreeRecoveryBubble(text) {
    const bubble = document.createElement("div");
    bubble.className = "task-tree-recovery-bubble";
    bubble.setAttribute("role", "status");
    bubble.setAttribute("aria-live", "polite");
    bubble.textContent = String(text || "").trim();
    return bubble;
}

function renderTree() {
    if (!S.tree) return;
    const recoveryNotice = String(S.currentTask?.metadata?.recovery_notice || "").trim();
    const projectedTree = projectTaskTree(S.tree, S.treeRoundSelectionsByNodeId);
    syncTaskTreeHeaderState(projectedTree);
    S.treeView = buildExecutionTree(projectedTree);
    if (!S.treeView) {
        U.tree.innerHTML = "";
        if (recoveryNotice) U.tree.appendChild(buildTaskTreeRecoveryBubble(recoveryNotice));
        const emptyState = document.createElement("div");
        emptyState.className = "empty-state";
        emptyState.textContent = "No nodes to display.";
        U.tree.appendChild(emptyState);
        setTaskSelectionEmptyVisible(false);
        return;
    }
    const wrapper = document.createElement("div");
    wrapper.className = "execution-tree";
    const layoutDensity = resolveExecutionTreeDensity(S.treeView);
    wrapper.dataset.layout = layoutDensity.mode;
    wrapper.dataset.totalItems = String(layoutDensity.stats.totalItems);
    wrapper.dataset.maxBreadth = String(layoutDensity.stats.maxBreadth);
    if (layoutDensity.mode === "wide" || layoutDensity.mode === "dense") {
        wrapper.classList.add("execution-tree--wide");
    }
    if (layoutDensity.mode === "dense") {
        wrapper.classList.add("execution-tree--dense");
    }
    const rootList = document.createElement("ul");
    rootList.className = "execution-tree-list";
    const handleTreeNodeClick = (node, event) => {
        if (S.treePan.suppressClickNodeId && S.treePan.suppressClickNodeId === String(node.node_id || "")) {
            S.treePan.suppressClickNodeId = null;
            return;
        }
        event.stopPropagation();
        const previousNodeId = String(S.selectedNodeId || "").trim();
        const nextNodeId = String(node.node_id || "").trim();
        if (!nextNodeId) return;
        if (previousNodeId && previousNodeId !== nextNodeId) {
            stashTaskDetailViewState({ nodeId: previousNodeId });
        }
        S.selectedNodeId = node.node_id;
        setTaskSelectionEmptyVisible(false);
        syncExecutionTreeSelection(previousNodeId, nextNodeId);
        void showAgent(node, { preserveViewState: false });
    };
    const createTreeNodeButton = (node, { showStaticSubtreeHint = false, mergedBase = false, inspectionBlock = false } = {}) => {
        const title = String(node.title || node.node_id || "");
        const fullTitle = String(node.fullTitle || title);
        const nodeStatus = String(node.visual_state || node.state || "").trim().toLowerCase();
        const displayState = String(node.display_state || node.state || "").trim() || String(node.state || "").toUpperCase();
        const button = document.createElement("button");
        button.type = "button";
        button.className = `execution-tree-node${S.selectedNodeId === node.node_id ? " selected" : ""}`;
        if (mergedBase) button.classList.add("execution-tree-node-base");
        if (inspectionBlock) button.classList.add("is-inspection");
        button.dataset.id = node.node_id;
        button.dataset.kind = node.kind || "execution";
        button.dataset.status = nodeStatus;
        button.title = fullTitle;
        button.setAttribute("aria-pressed", S.selectedNodeId === node.node_id ? "true" : "false");
        button.innerHTML = `${showStaticSubtreeHint ? '<span class="execution-tree-node-note">暂无其他可切换子树</span>' : ""}<span class="execution-tree-node-head"><span class="execution-tree-node-title">${esc(title)}</span><span class="status-badge" data-status="${esc(node.visual_state || node.state || "")}">${esc(displayState)}</span></span>`;
        button.addEventListener("click", (event) => handleTreeNodeClick(node, event));
        return button;
    };
    const walk = (node) => {
        const fullTitle = String(node.fullTitle || node.title || node.node_id || "");
        const nodeStatus = String(node.visual_state || node.state || "").trim().toLowerCase();
        const roundOptions = Array.isArray(node.roundOptions) ? node.roundOptions : [];
        const inspectionNodes = Array.isArray(node.inspectionNodes) ? node.inspectionNodes : [];
        const visibleChildren = Array.isArray(node.children) ? node.children : [];
        const hasSwitchableSubtrees = roundOptions.length > 1;
        const showStaticSubtreeHint = !hasSwitchableSubtrees && visibleChildren.length > 0;
        const item = document.createElement("li");
        item.className = "execution-tree-item";
        item.dataset.status = nodeStatus;
        const stack = document.createElement("div");
        stack.className = "execution-tree-node-stack";
        if (inspectionNodes.length) stack.classList.add("has-inspection");
        inspectionNodes.forEach((inspectionNode) => {
            stack.appendChild(createTreeNodeButton(inspectionNode, { inspectionBlock: true }));
        });
        stack.appendChild(createTreeNodeButton(node, { showStaticSubtreeHint, mergedBase: inspectionNodes.length > 0 }));
        if (hasSwitchableSubtrees) {
            const roundWrap = document.createElement("div");
            roundWrap.className = "execution-tree-node-rounds";
            ["mousedown", "click", "wheel"].forEach((eventName) => {
                roundWrap.addEventListener(eventName, (event) => event.stopPropagation());
            });
            const label = document.createElement("span");
            label.className = "execution-tree-round-label";
            label.textContent = "轮次";
            const select = document.createElement("select");
            select.className = "execution-tree-round-select resource-select";
            select.dataset.resourceSelectLabel = `${fullTitle} 轮次`;
            roundOptions.forEach((round) => {
                const option = document.createElement("option");
                option.value = round.roundId;
                option.textContent = round.isLatest ? `${round.label}（最新）` : round.label;
                select.appendChild(option);
            });
            if (node.selectedRoundId) select.value = node.selectedRoundId;
            select.addEventListener("change", (event) => {
                event.stopPropagation();
                setNodeRoundSelection(node.node_id, event.target.value);
            });
            roundWrap.appendChild(label);
            roundWrap.appendChild(select);
            stack.appendChild(roundWrap);
        }
        item.appendChild(stack);
        if (visibleChildren.length) {
            const branch = document.createElement("ul");
            branch.className = "execution-tree-list";
            branch.dataset.parentStatus = nodeStatus;
            visibleChildren.forEach((child) => branch.appendChild(walk(child)));
            item.appendChild(branch);
        }
        return item;
    };
    rootList.appendChild(walk(S.treeView));
    wrapper.appendChild(rootList);
    wrapper.style.transformOrigin = "0 0";
    wrapper.style.transform = `translate(${Math.round(S.treePan.offsetX)}px, ${Math.round(S.treePan.offsetY)}px) scale(${S.treePan.scale})`;
    U.tree.innerHTML = "";
    if (recoveryNotice) U.tree.appendChild(buildTaskTreeRecoveryBubble(recoveryNotice));
    U.tree.appendChild(wrapper);
    if (typeof enhanceResourceSelects === "function") enhanceResourceSelects();
    if (S.selectedNodeId) {
        const selected = findTreeNode(S.treeView, S.selectedNodeId);
        if (selected) {
            setTaskSelectionEmptyVisible(false);
            const selectedNodeId = String(selected.node_id || "").trim();
            const currentDetailNodeId = String(S.currentNodeDetail?.node_id || "").trim();
            void showAgent(selected, { preserveViewState: selectedNodeId !== "" && selectedNodeId === currentDetailNodeId });
        } else {
            S.selectedNodeId = null;
            setTaskSelectionEmptyVisible(true);
            hideAgent();
        }
    } else {
        setTaskSelectionEmptyVisible(true);
        hideAgent();
    }
}
function renderArtifacts() {
    if (!U.artifactList) return;
    const visibleArtifacts = getArtifactsForSelectedNode();
    const emptyText = S.selectedNodeId ? "No artifacts for this node yet." : "Select a node to view artifacts.";
    U.artifactList.innerHTML = "";
    if (!visibleArtifacts.length) {
        U.artifactList.innerHTML = `<div class="empty-state" style="padding: 10px;">${esc(emptyText)}</div>`;
        if (U.artifactContent) U.artifactContent.textContent = emptyText;
        if (U.artifactApply) U.artifactApply.hidden = true;
        renderArtifactHeading(0);
        refreshTaskDetailScrollRegions();
        return;
    }
    visibleArtifacts.forEach((artifact) => {
        const button = document.createElement("button");
        button.type = "button";
        button.className = `artifact-item${S.selectedArtifactId === artifact.artifact_id ? " active" : ""}`;
        button.innerHTML = `<strong>${esc(artifact.title || artifact.artifact_id)}</strong><span>${esc(artifact.kind || "artifact")}</span><small>${esc(artifact.preview_text || artifact.created_at || "")}</small>`;
        button.addEventListener("click", () => void selectArtifact(artifact.artifact_id));
        U.artifactList.appendChild(button);
    });
    renderArtifactHeading(visibleArtifacts.length);
    refreshTaskDetailScrollRegions();
}

function getArtifactsForSelectedNode() {
    if (!Array.isArray(S.taskArtifacts) || !S.taskArtifacts.length) return [];
    const nodeId = String(S.selectedNodeId || "").trim();
    if (!nodeId) return [];
    return S.taskArtifacts.filter((artifact) => String(artifact?.node_id || "").trim() === nodeId);
}

function getSelectedVisibleArtifact(artifacts = getArtifactsForSelectedNode()) {
    if (!Array.isArray(artifacts) || !artifacts.length) return null;
    const artifactId = String(S.selectedArtifactId || "").trim();
    if (!artifactId) return null;
    return artifacts.find((item) => String(item?.artifact_id || "").trim() === artifactId) || null;
}

async function syncArtifactsForSelectedNode({ preserveViewState = true, autoSelect = true } = {}) {
    const viewState = preserveViewState ? captureTaskDetailViewState() : null;
    const visibleArtifacts = getArtifactsForSelectedNode();
    const selectedArtifact = getSelectedVisibleArtifact(visibleArtifacts);
    if (!selectedArtifact) {
        S.selectedArtifactId = "";
        S.artifactContent = "";
    }
    renderArtifacts();
    try {
        if (selectedArtifact && U.artifactContent) {
            U.artifactContent.textContent = artifactDisplayText(selectedArtifact, S.artifactContent);
            if (U.artifactApply) U.artifactApply.hidden = selectedArtifact.kind !== "patch";
            return visibleArtifacts;
        }
        if (!visibleArtifacts.length) {
            if (U.artifactContent) U.artifactContent.textContent = S.selectedNodeId ? "This node has no artifacts yet." : "Select a node to view artifacts.";
            if (U.artifactApply) U.artifactApply.hidden = true;
            return visibleArtifacts;
        }
        if (U.artifactContent) U.artifactContent.textContent = "Select an artifact to view details.";
        if (U.artifactApply) U.artifactApply.hidden = true;
        if (autoSelect) {
            await selectArtifact(visibleArtifacts[0].artifact_id, { preserveViewState: false });
        }
        return visibleArtifacts;
    } finally {
        restoreTaskDetailViewState(viewState);
        scheduleTaskDetailSessionPersist();
    }
}

async function loadTaskArtifacts() {
    if (!S.currentTaskId) return [];
    const artifacts = await ApiClient.getTaskArtifacts(S.currentTaskId);
    S.taskArtifacts = artifacts;
    return syncArtifactsForSelectedNode();
}

async function selectArtifact(artifactId, { preserveViewState = true } = {}) {
    if (!S.currentTaskId || !artifactId) return;
    const viewState = preserveViewState ? captureTaskDetailViewState() : null;
    S.selectedArtifactId = artifactId;
    renderArtifacts();
    try {
        const data = await ApiClient.getTaskArtifact(S.currentTaskId, artifactId);
        if (String(S.selectedArtifactId || "") !== String(artifactId || "")) return;
        S.artifactContent = String(data.content || "");
        const artifact = getSelectedVisibleArtifact() || null;
        if (U.artifactContent) U.artifactContent.textContent = artifactDisplayText(artifact, S.artifactContent);
        if (U.artifactApply) U.artifactApply.hidden = !(artifact && artifact.kind === "patch");
    } finally {
        restoreTaskDetailViewState(viewState);
        scheduleTaskDetailSessionPersist();
    }
}

async function applySelectedArtifact() {
    if (!S.currentTaskId || !S.selectedArtifactId) return;
    await ApiClient.applyTaskArtifact(S.currentTaskId, S.selectedArtifactId);
    showToast({ title: "Patch applied", text: S.selectedArtifactId, kind: "success" });
    await loadTaskArtifacts();
}

function showTaskNodeLoadingState(node) {
    const compactHeading = compactNodeHeading(node);
    S.currentNodeDetail = { ...node };
    U.detail.style.display = "flex";
    if (U.nodeEmpty) U.nodeEmpty.style.display = "none";
    setTaskSelectionEmptyVisible(false);
    if (U.adRole) U.adRole.hidden = true;
    U.adStatus.textContent = String(node?.display_state || node?.state || node?.status || "");
    U.adStatus.dataset.status = node?.visual_state || node?.state || node?.status || "";
    if (U.adRoundSummary) U.adRoundSummary.textContent = String(node?.roundSummary || "");
    if (U.adFlow) U.adFlow.innerHTML = '<div class="empty-state task-trace-empty">Loading node details...</div>';
    renderFlowHeading(0);
    renderFinalOutput("Loading node output...");
    renderAcceptanceResult("Loading acceptance result...");
    U.feedTitle.textContent = `Node: ${compactHeading}`;
    U.feedTitle.title = compactHeading;
    setTaskDetailOpen(true);
    icons();
    refreshTaskDetailScrollRegions();
}

async function ensureTaskNodeDetail(nodeId, { force = false } = {}) {
    const key = String(nodeId || "").trim();
    const taskId = String(S.currentTaskId || "").trim();
    if (!taskId || !key) return null;
    if (!force && S.taskNodeDetails[key]) return S.taskNodeDetails[key];
    if (!force && S.taskNodeDetailRequests[key]) return S.taskNodeDetailRequests[key];
    S.taskNodeBusy = true;
    const request = (async () => {
        try {
            const detail = await ApiClient.getTaskNodeDetail(taskId, key);
            if (!detail) return null;
            if (String(S.currentTaskId || "").trim() !== taskId) return null;
            S.taskNodeDetails = { ...S.taskNodeDetails, [key]: detail };
            if (String(S.selectedNodeId || "") === key) S.currentNodeDetail = detail;
            return detail;
        } catch (error) {
            if (!isAbortLike(error)) {
                showToast({ title: "Node load failed", text: error.message || "Unknown error", kind: "error" });
            }
            return S.taskNodeDetails[key] || null;
        } finally {
            const nextRequests = { ...S.taskNodeDetailRequests };
            if (nextRequests[key] === request) delete nextRequests[key];
            S.taskNodeDetailRequests = nextRequests;
            S.taskNodeBusy = Object.keys(nextRequests).length > 0;
        }
    })();
    S.taskNodeDetailRequests = { ...S.taskNodeDetailRequests, [key]: request };
    return request;
}

async function showAgent(node, { preserveViewState = true, forceRefresh = false } = {}) {
    const nodeId = String(node?.node_id || "").trim();
    if (!nodeId) return;
    const renderToken = (Number(S.taskDetailRenderToken || 0) || 0) + 1;
    S.taskDetailRenderToken = renderToken;
    const previousDetailNodeId = String(S.currentNodeDetail?.node_id || "").trim();
    const hadVisibleCurrentDetail = U.detail?.style?.display !== "none"
        && String(S.currentNodeDetail?.node_id || "").trim() === nodeId;
    const viewState = consumePendingTaskDetailRestore(nodeId)
        || (preserveViewState ? captureTaskDetailViewState() : getStoredTaskDetailViewState(S.currentTaskId, nodeId));
    if (!S.taskNodeDetails[nodeId] && !hadVisibleCurrentDetail) showTaskNodeLoadingState(node);
    const detail = await ensureTaskNodeDetail(nodeId, { force: forceRefresh });
    if (renderToken !== S.taskDetailRenderToken) return;
    if (String(S.selectedNodeId || "").trim() !== nodeId) return;
    const liveFrameMap = liveFramesByNodeId(S.currentTaskProgress);
    const mergedNode = {
        ...node,
        ...(detail || {}),
        executionTrace: buildNodeExecutionTrace(node, detail || {}, liveFrameMap.get(nodeId) || null),
    };
    S.currentNodeDetail = mergedNode;
    const compactHeading = compactNodeHeading(mergedNode);
    U.detail.style.display = "flex";
    if (U.nodeEmpty) U.nodeEmpty.style.display = "none";
    setTaskSelectionEmptyVisible(false);
    if (U.adRole) U.adRole.hidden = true;
    if (U.adRoundSummary) U.adRoundSummary.textContent = String(node.roundSummary || "当前节点无派生轮次");
    U.adStatus.textContent = String(mergedNode.display_state || mergedNode.state || mergedNode.status || "");
    U.adStatus.dataset.status = mergedNode.visual_state || mergedNode.state || mergedNode.status || node.visual_state || node.state || "";
    if (U.adRoundSummary) U.adRoundSummary.textContent = String(mergedNode.roundSummary || "");
    const traceChanged = !!renderExecutionTrace(mergedNode, { viewState });
    const outputChanged = !!renderFinalOutput(mergedNode.executionTrace?.final_output || "");
    const acceptanceChanged = !!renderAcceptanceResult(mergedNode.executionTrace?.acceptance_result || "");
    U.feedTitle.textContent = `Node: ${compactHeading}`;
    U.feedTitle.title = compactHeading;
    setTaskDetailOpen(true);
    icons();
    if (!hadVisibleCurrentDetail || traceChanged || outputChanged || acceptanceChanged) {
        restoreTaskDetailViewState(viewState);
        stashTaskDetailViewState({ nodeId, viewState });
    } else {
        stashTaskDetailViewState({ nodeId });
    }
    if (!hadVisibleCurrentDetail || previousDetailNodeId !== nodeId) {
        void syncArtifactsForSelectedNode();
    }
}

function hideAgent() {
    if (U.detail) U.detail.style.display = "none";
    setTaskDetailOpen(false);
}

function applyTaskPayload(payload) {
    if (!payload || !payload.task) return;
    const previousTaskId = String(S.currentTask?.task_id || "").trim();
    const nextTaskId = String(payload.task?.task_id || "").trim();
    const treeRoot = payload.tree_root || payload.progress?.root || null;
    const runtimeSummary = payload.runtime_summary || payload.progress?.live_state || null;
    S.currentTask = payload.task;
    S.currentTaskTreeRoot = treeRoot;
    S.currentTaskRuntimeSummary = runtimeSummary;
    S.currentTaskProgress = {
        ...(payload.progress || {}),
        root: treeRoot,
        live_state: runtimeSummary,
        nodes: Array.isArray(payload.progress?.nodes) ? payload.progress.nodes : [],
        model_calls: Array.isArray(payload.progress?.model_calls) ? payload.progress.model_calls : [],
    };
    S.tree = treeRoot;
    S.treeRoundSelectionsByNodeId = pruneTreeRoundSelections(S.tree, S.treeRoundSelectionsByNodeId);
    renderTaskDetailHeader({ resetPromptDisclosure: previousTaskId !== nextTaskId });
    if (U.taskTokenButton) U.taskTokenButton.disabled = !S.currentTask;
    renderTaskTokenStats();
    if (S.tree) renderTree();
    else {
        syncTaskTreeHeaderState(null);
        U.tree.innerHTML = '<div class="empty-state">No task tree.</div>';
        setTaskSelectionEmptyVisible(false);
        hideAgent();
    }
}
