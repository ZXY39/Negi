// 节点详情渲染：执行轨迹（阶段/轮次/工具/参数/输出分页/复制）、消息投递、
// 派生评审、上下文与错误历史、模型重试提示。状态投影在 node-detail-store.js。

function taskNodeDisplayState(node, taskPaused = taskPauseDisplayActive()) {
    const status = String(node?.state || node?.status || "").trim().toLowerCase();
    if (taskPaused && !isTerminalTreeNodeStatus(status)) return "任务暂停";
    if (node?.is_paused) {
        return `\u5df2\u6682\u505c\uff08${String(node.pause_reason || "").trim() || "manual"}\uff09`;
    }
    return String(node?.display_state || "").trim() || treeNodeStatusFallbackLabel(status);
}

function treeNodeStatusFallbackLabel(status) {
    const normalized = String(status || "").trim().toLowerCase();
    if (normalized === "success" || normalized === "failed") return normalized.toUpperCase();
    return "\u7b49\u5f85\u4e2d";
}

function singleLineNodeHeading(node) {
    const raw = repairAcceptanceTitle(String(node?.goal || node?.fullTitle || node?.title || node?.node_id || "Node"));
    return raw
        .replace(/\r\n|\r|\n/g, " ")
        .replace(/\s+/g, " ")
        .trim() || "Node";
}

function compactNodeHeading(node, maxChars = 72) {
    const singleLine = singleLineNodeHeading(node);
    const chars = Array.from(singleLine);
    if (chars.length <= maxChars) return singleLine;
    return `${chars.slice(0, maxChars).join("")}...`;
}

function formatNodeDetailHeading(node, { maxChars = 72, compact = true } = {}) {
    const nodeId = String(node?.node_id || "").trim();
    const heading = compact ? compactNodeHeading(node, maxChars) : singleLineNodeHeading(node);
    if (!nodeId) return compact ? `Node: ${heading}` : heading;
    if (!heading || heading === nodeId) return nodeId;
    return `${nodeId} | ${heading}`;
}

function renderTraceStep({ traceKey = "", title, status = "info", statusLabel = "", open = false, bodyHtml = "", showRuntime = true, showStatus = true, extraClass = "", leadHtml = "", stageId = "", evicted = false, textOnly = false, text = "" }) {
    if (textOnly) {
        return `
        <section class="interaction-step task-trace-step task-trace-text-block" data-trace-key="${esc(traceKey)}">
            <div class="task-trace-round-text">${esc(text)}</div>
        </section>
    `;
    }
    const classes = ["interaction-step", "task-trace-step", esc(status)];
    const normalizedExtraClass = String(extraClass || "").trim();
    if (normalizedExtraClass) classes.push(esc(normalizedExtraClass));
    // 阶段被模型点名移出上下文：生命周期状态（完成/失败）保留在 class 上供状态对账用，
    // 徽章换成中性灰 + eye-off 图标，文案改成"已移出上下文"——它说的是"还在账本里、
    // 但不再进模型上下文"，与"完成"是两件不同的事。
    if (evicted) classes.push("stage-evicted");
    // data-trace-key 的 key 允许退化成 stage_index 乃至序号，跨回合会撞车；
    // 阶段状态对账需要的是真 stage_id，所以单独落一个属性，不做字符串推断。
    const normalizedStageId = String(stageId || "").trim();
    const stageIdAttr = normalizedStageId ? ` data-stage-id="${esc(normalizedStageId)}"` : "";
    const sideParts = [];
    if (showRuntime) sideParts.push('<span class="task-trace-runtime" hidden></span>');
    if (showStatus) {
        const label = String(statusLabel || "").trim() || traceStatusLabel(status);
        const leadIcon = evicted ? '<i data-lucide="eye-off" aria-hidden="true"></i>' : "";
        sideParts.push(`<span class="interaction-step-status">${leadIcon}${esc(label)}</span>`);
    }
    return `
        <details class="${classes.join(" ")}" data-trace-key="${esc(traceKey)}"${stageIdAttr} data-default-open="${open ? "true" : "false"}"${open ? " open" : ""}>
            <summary class="task-trace-summary">
                <span class="interaction-step-lead">
                    ${leadHtml}
                    <span class="interaction-step-title">${esc(title)}</span>
                </span>
                <span class="interaction-step-side">
                    ${sideParts.join("")}
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

function buildExecutionRoundToolKey(round, step, toolIndex) {
    const roundKey = String(round?.round_id || round?.round_index || "round").trim() || "round";
    const stepKey = String(step?.tool_call_id || toolIndex).trim() || String(toolIndex);
    return `${roundKey}:tool:${stepKey}`;
}

function contextLoadIconName(kind) {
    return typeof ceoContextLoadNoticeIconName === "function" ? ceoContextLoadNoticeIconName(kind) : "";
}

function contextLoadBodyFromStep(step) {
    const text = String(step?.output_text || "").trim();
    if (!text.startsWith("{")) return "";
    let parsed = null;
    try {
        parsed = JSON.parse(text);
    } catch {
        return "";
    }
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed) || parsed.ok !== true) return "";
    return String(parsed.content || "").trim();
}

// 上下文加载器按「加载 skill / 加载 tool + 风险度配色」呈现；失败轮保留原始工具名与参数车道，
// 否则错误正文会被当成空正文。哪个名字算加载器、风险度怎么取，都复用会话提醒那一份判定。
// 冲动投递（未水合工具被直接调用）那一行的 tool_name 是目标工具本身，契约由它交回，
// 所以也按同一张「加载 tool」卡片画，目标名从 tool_name 直接取。














const STAGE_EVICTED_LABEL = "已移出上下文";

// 裁撤标记由后端账本落到 UI 投影（canonical_context._normalize_stage 只在成立时写
// context_evicted:true，缺失即未裁撤），前端不做推断。



// 消息状态标签的唯一映射：分发链路上的每一档都从这里出，前端不再各自写字面量。
// received/frozen 是投递前的视图层投影（后端 message_list 直接给这两档），
// pending/consumed/merged 是既有三态，判定源仍是后端账本。
// 原始账本 token（delivered 等）一律不得作为标签外泄，未知值走中性「待确认」。
const MESSAGE_STATUS_LABELS = {
    received: "已接收",
    frozen: "已冻结待释放",
    pending: "待处理",
    consumed: "已消费",
    merged: "已并入上下文",
    skipped: "未下发",
};



// 「请求中」的判据与树徽标同源（isModelRequestInFlightFrame），不另立第二套：
// 同一个标记在两处给出不同结论，正是操作员判断"到底卡在哪"时最致命的误导。


// 分发结果三态映射：不再向前端暴露原始账本状态文本（delivered/consumed），
// 统一成语义标签 + lucide 图标；判定源是后端账本（status + merged_at），前端不做推断。











// 消息列表标题右侧的定向通知输入框：可用状态跟随当前选中节点
//（验收节点/终态节点/已结束任务不可作为定向目标，与后端校验一致）。
// —— 定向通知发送后的局部 settle 轮询 ——
// 分发链路（epoch 排队 → 分发节点决策回合 → 投递记账）是异步的，提交后新消息
// 不会立刻出现在 message_list。这里以固定间隔重拉目标节点的 full 详情，一旦
// 消息落表即局部刷新节点详情；超时（分发失败等）静默退出，由 WS task.node.patch
// 与手动刷新兜底。轮询只更新缓存与当前选中节点的渲染，不动全局视图。
const TASK_NOTICE_SETTLE_INTERVAL_MS = 2500;
const TASK_NOTICE_SETTLE_TIMEOUT_MS = 90000;
// 分发进行中不能按 90 秒收窗：一次根目标分发的排空+控制回合可以跑到 9 分钟以上，
// 固定窗口会让条目在消息真正落地前被丢弃，此后只剩手动刷新。
// 上限用于兜住分发失败/驱动器缺席时的无限轮询。
const TASK_NOTICE_SETTLE_DISTRIBUTION_MAX_MS = 30 * 60 * 1000;








const traceCopyFlashTimers = new WeakMap();














// 轮次下拉框的稳定身份：整树重绘时 shell.dataset.selectId 由它派生，
// 展开状态才能被 restoreOpenResourceSelect 认回来。
// 分发/分发失败期间被冻结（受影响）的节点集合：以 blocked 快照与目标节点
// 为种子，沿当前快照展开各自整棵子树。连线仅在这些节点之间染成分发色；
// epoch 完成（通知被消费/处理）后 distribution UI 退出，集合不再生效。
// 节点终态口径与后端 `main/types.py` 的 NodeStatus 一致（in_progress | success | failed）。
const TREE_TERMINAL_STATUSES = new Set(["success", "failed"]);

// 分发横幅的聚合读数：分母取应冻集中**仍非终态**的节点（blocked_node_ids 是 epoch 创建时的
// 快照，会残留已经跑完的分支），分子取排空账本与该集合的**交集**（账本可能残留已不在应冻集的
// 节点，取交集才不会分子超分母）。树里查不到任何节点状态时退回快照口径，不猜。
// 分子语义上是下界：未经检查点停住的节点不入账，所以这行只能读成「至少这些已经停了」，
// 不是精确进度条。合同见 docs/architecture/runtime-overview.md「frontdoor 与任务运行时的关系」。
// 在当前任务树快照中按节点 ID 或 goal 关键词搜索，返回按匹配度排序的候选节点
// 将已渲染的节点居中到树视口正中，并触发一次性放大脉冲动画；节点不在树中时返回 false
// renderTree 重建 DOM 后，若仍处在上次定位的高亮窗口期内，为新节点元素补挂放大动画；
// 用负 animation-delay 让动画从当前相位继续，避免频繁重渲染时动画反复从头播放
// 从目标节点向上找最近的、当前已在树中渲染的祖先节点 ID（目标本身已确认不可见时使用）
// 展开目标节点的所有祖先后定位该节点；给定 scale 时先固定缩放再居中。
// 祖先按目标所在轮次拉取子树（缺省会拉最新轮次，可能把旧轮节点移出快照）。
// 返回 Promise<{ located, reason?, fallbackNodeId? }>：
// - located=true 表示目标节点已居中；
// - located=false 且带 fallbackNodeId 表示目标当前不可见，已居中其最近可见祖先。
const RECOVERY_NOTICE_DISMISSALS_STORAGE_KEY = "g3ku.taskRecoveryNotice.dismissed.v1";






const TASK_NODE_MODEL_RETRY_LIVE_STATES = new Set(["retrying", "waiting_upstream"]);











// 抽屉的状态行与树徽标同源：task.live.patch 只重绘树、不重拉节点详情，
// 否则选中节点自己的标签会停在打开抽屉那一刻。
let treeVisualRefreshRaf = 0;

function contextLoadDisplayFor(step) {
    if (typeof ceoContextLoaderKind !== "function") return null;
    const toolName = String(step?.tool_name || "").trim();
    const contractDelivery = String(step?.contract_delivery || "").trim();
    const kind = contractDelivery ? "tool" : ceoContextLoaderKind(toolName);
    if (!kind) return null;
    if (CONTEXT_LOAD_HIDDEN_STATUSES.has(String(step?.status || "").trim().toLowerCase())) return null;
    let targetId = contractDelivery ? toolName : "";
    if (!targetId && typeof extractCeoContextLoadTarget === "function") {
        targetId = [step?.output_text, step?.arguments_text]
            .map((raw) => String(extractCeoContextLoadTarget(toolName, String(raw || "")) || "").trim())
            .find(Boolean) || "";
    }
    return {
        kind,
        targetId,
        riskLevel: typeof resolveCeoContextLoadNoticeRiskLevel === "function"
            ? resolveCeoContextLoadNoticeRiskLevel(kind, targetId)
            : "medium",
        iconName: contextLoadIconName(kind),
        title: CONTEXT_LOAD_TITLES[kind] || "加载内容",
        body: contextLoadBodyFromStep(step),
    };
}

function renderContextLoadIcon(contextLoad) {
    if (!contextLoad?.iconName) return "";
    return `<span class="context-load-icon risk-${esc(contextLoad.riskLevel)}" aria-hidden="true"><i data-lucide="${esc(contextLoad.iconName)}"></i></span>`;
}

function traceHasContextLoadStep(trace) {
    const steps = [];
    (Array.isArray(trace?.stages) ? trace.stages : []).forEach((stage) => {
        (Array.isArray(stage?.rounds) ? stage.rounds : []).forEach((round) => {
            steps.push(...(Array.isArray(round?.tools) ? round.tools : []));
        });
    });
    steps.push(...(Array.isArray(trace?.tool_steps) ? trace.tool_steps : []));
    return steps.some((step) => !!contextLoadDisplayFor(step));
}

function renderContextLoadBodyField(contextLoad, step) {
    if (contextLoad.body) {
        return renderTraceField("内容", contextLoad.body, "暂无内容", { copyable: true });
    }
    if (String(step?.status || "").trim().toLowerCase() === "running") {
        return renderTraceOutputField("内容", "", "", "等待内容加载...", { decodeEscapes: true, copyable: true });
    }
    // 正文解不出来（例如结果被外置成信封）时退回输出车道，让 output_ref 水合继续工作。
    return renderTraceOutputField(
        "内容",
        step?.output_text,
        step?.output_ref,
        "暂无内容",
        { decodeEscapes: true, copyable: true },
    );
}

function renderExecutionRoundToolChip(round, step, toolIndex) {
    const toolKey = buildExecutionRoundToolKey(round, step, toolIndex);
    const status = String(step?.status || "info").trim() || "info";
    const contextLoad = contextLoadDisplayFor(step);
    const titleText = contextLoad ? contextLoad.title : (String(step?.tool_name || "tool").trim() || "tool");
    const iconHtml = renderContextLoadIcon(contextLoad);
    const titleAttr = contextLoad?.targetId ? ` title="${esc(contextLoad.targetId)}"` : "";
    return `
        <button
            type="button"
            class="task-trace-round-chip ${esc(status)}${contextLoad ? ` is-context-load is-${esc(contextLoad.kind)}` : ""}"
            data-tool-key="${esc(toolKey)}"
            aria-pressed="false"${titleAttr}
        >
            <span class="task-trace-round-chip-label">
                ${iconHtml}
                <span class="task-trace-round-chip-title">${esc(titleText)}</span>
            </span>
            <span class="task-trace-round-chip-status">${esc(traceStatusLabel(status))}</span>
        </button>
    `;
}

function renderExecutionRoundToolPanel(round, step, toolIndex) {
    const toolKey = buildExecutionRoundToolKey(round, step, toolIndex);
    const contextLoad = contextLoadDisplayFor(step);
    if (contextLoad) {
        return `
        <section class="task-trace-round-panel is-context-load" data-tool-key="${esc(toolKey)}" hidden>
            ${renderContextLoadBodyField(contextLoad, step)}
        </section>
    `;
    }
    const evidenceSummary = (Array.isArray(step?.evidence) ? step.evidence : [])
        .map((item) => [String(item?.kind || "").trim(), String(item?.path || item?.ref || "").trim(), String(item?.note || "").trim()].filter(Boolean).join(" | "))
        .filter(Boolean)
        .join("\n");
    // 入参被投影清空时，面板先印提示、再按 tool_call_id 回取全量。
    const argumentsLookupId = !String(step?.arguments_full || "").trim() && step?.arguments_truncated
        ? String(step?.tool_call_id || "").trim()
        : "";
    const recoveryFields = String(step?.tool_name || "") === "recovery_check"
        ? [
            renderTraceField("恢复检查结论", step?.recovery_decision, "暂无恢复检查结论"),
            renderTraceField("之前尝试执行了", (Array.isArray(step?.attempted_tools) ? step.attempted_tools : []).join(", "), "暂无尝试记录"),
            renderTraceField("证据摘要", evidenceSummary || step?.lost_result_summary, "暂无恢复证据"),
        ].join("")
        : "";
    return `
        <section class="task-trace-round-panel" data-tool-key="${esc(toolKey)}" hidden>
            ${[
                renderTraceField("参数", step?.arguments_full || step?.arguments_text, "无参数", { copyable: true, argumentsLookupId }),
                renderTraceOutputField(
                    "工具输出",
                    step?.output_text,
                    step?.output_ref,
                    String(step?.status || "") === "running" ? "等待工具输出..." : "暂无工具输出",
                    { decodeEscapes: true, copyable: true },
                ),
                recoveryFields,
            ].join("")}
        </section>
    `;
}

function renderExecutionRoundToolStrip(round, tools) {
    const toolList = Array.isArray(tools) ? tools : [];
    if (!toolList.length) {
        return renderTraceField("工具", "", "本轮暂无工具记录");
    }
    const roundKey = String(round?.round_id || round?.round_index || "").trim();
    return `
        <div class="task-trace-round-tools" data-round-key="${esc(roundKey)}" data-active-tool-key="">
            <div class="task-trace-round-strip">
                ${toolList.map((step, toolIndex) => renderExecutionRoundToolChip(round, step, toolIndex)).join("")}
            </div>
            <div class="task-trace-round-panels">
                ${toolList.map((step, toolIndex) => renderExecutionRoundToolPanel(round, step, toolIndex)).join("")}
            </div>
        </div>
    `;
}

function setTraceRoundActiveTool(roundHost, nextToolKey = "") {
    if (!(roundHost instanceof HTMLElement)) return;
    const normalizedToolKey = String(nextToolKey || "").trim();
    roundHost.dataset.activeToolKey = normalizedToolKey;
    const chips = Array.from(roundHost.querySelectorAll(".task-trace-round-chip"));
    const panels = Array.from(roundHost.querySelectorAll(".task-trace-round-panel"));
    chips.forEach((chip) => {
        const isActive = normalizedToolKey && String(chip.dataset.toolKey || "").trim() === normalizedToolKey;
        chip.classList.toggle("is-active", !!isActive);
        chip.setAttribute("aria-pressed", isActive ? "true" : "false");
    });
    panels.forEach((panel) => {
        const isActive = normalizedToolKey && String(panel.dataset.toolKey || "").trim() === normalizedToolKey;
        panel.hidden = !isActive;
        panel.classList.toggle("is-active", !!isActive);
        if (isActive) hydrateTraceOutputBlocks(panel);
    });
}

function bindTraceRoundToolStrips(traceList) {
    if (!(traceList instanceof HTMLElement) || traceList.dataset.roundToolBindings === "true") return;
    traceList.dataset.roundToolBindings = "true";
    traceList.addEventListener("click", (event) => {
        const chip = event.target instanceof Element ? event.target.closest(".task-trace-round-chip") : null;
        if (!(chip instanceof HTMLElement) || !traceList.contains(chip)) return;
        const roundHost = chip.closest(".task-trace-round-tools");
        if (!(roundHost instanceof HTMLElement)) return;
        const toolKey = String(chip.dataset.toolKey || "").trim();
        const currentToolKey = String(roundHost.dataset.activeToolKey || "").trim();
        setTraceRoundActiveTool(roundHost, currentToolKey === toolKey ? "" : toolKey);
        if (typeof scheduleTaskDetailSessionPersist === "function") scheduleTaskDetailSessionPersist();
    });
}

function bindTraceOutputAutoLoad(traceList) {
    if (!(traceList instanceof HTMLElement) || traceList.dataset.traceOutputBindings === "true") return;
    traceList.dataset.traceOutputBindings = "true";
    traceList.addEventListener("toggle", (event) => {
        const traceStep = event.target instanceof Element ? event.target.closest(".task-trace-step") : null;
        if (!(traceStep instanceof HTMLElement) || !traceList.contains(traceStep) || !traceStep.open) return;
        hydrateTraceOutputBlocks(traceStep);
    }, true);
}

function renderExecutionStageRoundBlock(stage, round, index) {
    const tools = Array.isArray(round?.tools) ? round.tools : [];
    const time = round?.created_at ? formatCompactTime(round.created_at) : "";
    const roundText = String(round?.text || "").trim();
    return `
        <section class="task-trace-round-group" data-round-key="${esc(String(round?.round_id || round?.round_index || index + 1).trim())}">
            ${time ? `<div class="task-trace-round-meta">${esc(time)}</div>` : ""}
            ${roundText ? `<div class="task-trace-round-text">${esc(roundText)}</div>` : ""}
            ${renderExecutionRoundToolStrip(round, tools)}
        </section>
    `;
}

function renderExecutionStageSummary(stage) {
    const summaryText = String(stage?.completed_stage_summary || "").trim();
    if (!summaryText) return "";
    return `
        <div class="task-trace-stage-summary">${esc(summaryText)}</div>
    `;
}

function renderExecutionStageRounds(stage) {
    const rounds = Array.isArray(stage?.rounds) ? stage.rounds : [];
    const summaryHtml = renderExecutionStageSummary(stage);
    if (!rounds.length) {
        const archiveRef = String(stage?.rounds_archive_ref || "").trim();
        if (archiveRef) {
            // 阶段被点名裁撤后轨道不再带正文，整段原文留在归档里：这里只给入口，
            // 点开才取档（把正文塞回每一行 checkpoint 会让转录成倍变大）。
            return `
                <div class="task-trace-stage-archive" data-stage-archive="${esc(archiveRef)}">
                    <button type="button" class="task-trace-stage-archive-btn" data-stage-archive-open="${esc(archiveRef)}">
                        <i data-lucide="archive-restore"></i><span>取回本阶段的调用记录</span>
                    </button>
                </div>
            ` + summaryHtml;
        }
        // 既没有轮次也没有可取的档：体内什么都不画，占位句只是把"这张卡没有内容"重说一遍。
        // 在飞工具行照常挂得进这个体内（org_graph_app.js ceoLiveToolStepHost）。
        return summaryHtml;
    }
    return rounds.map((round, index) => renderExecutionStageRoundBlock(stage, round, index)).join("") + summaryHtml;
}

function displayTaskStageStatus(status) {
    return ({
        "进行中": "进行中",
        "in_progress": "进行中",
        "running": "进行中",
        "active": "进行中",
        "完成": "完成",
        "success": "完成",
        "completed": "完成",
        "失败": "失败",
        "failed": "失败",
        "error": "失败",
    }[String(status || "").trim()]) || String(status || "").trim() || "进行中";
}

function stageIsContextEvicted(stage) {
    return Boolean(stage && stage.context_evicted === true);
}

function formatExecutionStageTitle(stage) {
    const stageGoal = String(stage?.stage_goal || "").trim();
    const fallbackTitle = String(stage?.mode || "自主执行").trim() || "自主执行";
    // 标题只带阶段目标：开始时间在卡内逐轮那行已经印过一遍，轮数（用过/预算）
    // 更是账本字段而非显示字段。
    return stageGoal || fallbackTitle;
}

function buildTurnGroupDescriptors(turns) {
    const entries = [];
    (Array.isArray(turns) ? turns : []).forEach((turn) => {
        const text = String(turn?.text || "").trim();
        const tools = Array.isArray(turn?.tools) ? turn.tools : [];
        if (text) {
            entries.push({
                kind: "text",
                seq: turn.seq,
                text,
                created_at: String(turn?.created_at || ""),
                tools: [],
            });
        }
        if (!tools.length) return;
        let target = [...entries].reverse().find((entry) => entry.kind === "text");
        if (!target) {
            target = entries.find((entry) => entry.kind === "preamble");
            if (!target) {
                target = { kind: "preamble", tools: [] };
                entries.push(target);
            }
        }
        target.tools.push(...tools);
    });

    const steps = [];
    entries.forEach((entry) => {
        if (entry.kind === "preamble") {
            steps.push({
                traceKey: "turn:preamble",
                title: "开始执行",
                status: roundTraceStatus({ tools: entry.tools }),
                open: false,
                bodyHtml: renderExecutionRoundToolStrip({ round_id: "turn:preamble" }, entry.tools),
            });
            return;
        }
        if (entry.tools.length) {
            steps.push({
                traceKey: `turn:${entry.seq}`,
                title: compactNodeHeading({ title: entry.text }, 80),
                status: roundTraceStatus({ tools: entry.tools }),
                open: false,
                bodyHtml: renderExecutionRoundToolStrip(
                    { round_id: `turn:${entry.seq}`, created_at: entry.created_at },
                    entry.tools
                ),
            });
            return;
        }
        steps.push({
            traceKey: `turn:${entry.seq}`,
            textOnly: true,
            text: entry.text,
            status: "info",
        });
    });
    return steps;
}

function buildTurnTraceSteps(turns, initialPromptStep) {
    const steps = [initialPromptStep, ...buildTurnGroupDescriptors(turns)];
    const last = steps[steps.length - 1];
    if (last && !last.textOnly) last.open = true;
    return steps;
}

function buildExecutionTraceSteps(trace, node) {
    const initialPromptStep = {
        traceKey: "initial_prompt",
        title: "初始提示词",
        status: "info",
        open: false,
        bodyHtml: renderTraceField("内容", trace.initial_prompt, "暂无初始提示词"),
    };
    if (Array.isArray(trace?.turns) && trace.turns.length) {
        return buildTurnTraceSteps(trace.turns, initialPromptStep);
    }
    if (Array.isArray(trace?.stages) && trace.stages.length) {
        return [
            initialPromptStep,
            ...trace.stages.map((stage, index) => ({
                traceKey: `stage:${stage.stage_id || stage.stage_index || index}`,
                title: formatExecutionStageTitle(stage),
                status: stageTraceStatus(stage),
                statusLabel: stageTraceStatusLabel(stage),
                evicted: stageIsContextEvicted(stage),
                open: index === trace.stages.length - 1,
                bodyHtml: renderExecutionStageRounds(stage),
            })),
        ];
    }
    return [
        initialPromptStep,
        ...trace.tool_steps.map((step, index) => {
            const contextLoad = contextLoadDisplayFor(step);
            return {
                traceKey: `tool:${step.tool_call_id || index}:${step.tool_name || "tool"}`,
                title: contextLoad ? contextLoad.title : `Tool - ${step.tool_name || "tool"}`,
                leadHtml: renderContextLoadIcon(contextLoad),
                status: step.status || "info",
                open: false,
                bodyHtml: contextLoad ? renderContextLoadBodyField(contextLoad, step) : [
                    renderTraceField("Arguments", step.arguments_text, "No arguments", { copyable: true }),
                    renderTraceOutputField(
                        "Output",
                        step.output_text,
                        step.output_ref,
                        step.status === "running" ? "Waiting for tool output..." : "No tool output",
                        { decodeEscapes: true, copyable: true },
                    ),
                ].join(""),
            };
        }),
    ];
}

function messageStatusDescriptor(status) {
    const normalized = String(status || "").trim().toLowerCase();
    if (normalized === "pending" || normalized === "received" || normalized === "frozen") {
        return {
            key: normalized,
            label: MESSAGE_STATUS_LABELS[normalized],
            icon: normalized === "frozen" ? "pause" : "inbox",
        };
    }
    if (normalized === "consumed" || normalized === "merged") {
        return {
            key: normalized,
            label: MESSAGE_STATUS_LABELS[normalized],
            icon: normalized === "merged" ? "merge" : "circle-check",
        };
    }
    if (normalized === "skipped") {
        return { key: "skipped", label: MESSAGE_STATUS_LABELS.skipped, icon: "circle-slash" };
    }
    if (normalized === "delivered") {
        // 投递账本态等价于「已接收、待该节点处理」，但不回显原始 token。
        return { key: "delivered", label: MESSAGE_STATUS_LABELS.received, icon: "inbox" };
    }
    return { key: normalized || "unknown", label: "待确认", icon: "circle" };
}

function messageListStatusDescriptor(status) {
    const descriptor = messageStatusDescriptor(status);
    // 列表条目上的 key 是**视觉档位**（warning/info），与「分发情况」行用的语义 key
    // 是两套词表：这里只做映射，不得把两套并成一套，否则徽标配色合同会被改掉。
    if (["pending", "received", "frozen"].includes(descriptor.key)) {
        return { key: "warning", label: descriptor.label };
    }
    return { key: "info", label: descriptor.label };
}

function formatRequestInFlightSuffix(status, nodeId) {
    if (String(status || "").trim().toLowerCase() !== "received") return "";
    const frame = (S.liveFrameMap || {})[String(nodeId || "").trim()] || null;
    if (!isModelRequestInFlightFrame(frame)) return "";
    const startedAt = Date.parse(String(frame.await_started_at || ""));
    if (Number.isNaN(startedAt)) return " · 请求中";
    const seconds = Math.max(0, Math.round((Date.now() - startedAt) / 1000));
    return ` · 请求中(已等 ${seconds}s)`;
}

function formatMessageListTitle(entry, index) {
    const time = String(entry?.received_at || entry?.consumed_at || "").trim();
    const formattedTime = time ? formatCompactTime(time) : `消息 ${index + 1}`;
    const status = messageListStatusDescriptor(entry?.status);
    return `${formattedTime} · ${status.label}${formatRequestInFlightSuffix(entry?.status, S.selectedNodeId)}`;
}

function messageDeliveryStatusDescriptor(delivery = {}) {
    const decision = String(delivery?.decision || "").trim().toLowerCase();
    if (decision === "skipped") {
        return { key: "skipped", label: "未下发", icon: "circle-slash" };
    }
    const status = String(delivery?.status || "").trim().toLowerCase();
    const mergedAt = String(delivery?.merged_at || "").trim();
    if (status === "delivered") {
        return { key: "delivered", label: "已分发·待处理", icon: "inbox" };
    }
    if (status === "consumed") {
        if (mergedAt) {
            return { key: "merged", label: "已并入上下文", icon: "merge" };
        }
        return { key: "consumed", label: "已消费", icon: "circle-check" };
    }
    return messageStatusDescriptor(status);
}

function renderMessageDeliveriesField(deliveries = []) {
    const items = Array.isArray(deliveries) ? deliveries : [];
    const labelHtml = `<div class="task-trace-label">${esc("分发情况")}</div>`;
    if (!items.length) {
        return `
        <div class="task-trace-field">
            ${labelHtml}
            <div class="code-block task-trace-code">${esc("无")}</div>
        </div>
    `;
    }
    const rows = items.map((item) => {
        const descriptor = messageDeliveryStatusDescriptor(item);
        const skipped = String(item?.decision || "").trim().toLowerCase() === "skipped";
        const targetTitle = String(item?.target_title || item?.target_node_id || "").trim() || "未命名节点";
        const targetNodeId = String(item?.target_node_id || "").trim();
        const showNodeId = targetNodeId && targetNodeId !== targetTitle;
        // 分发情况只展示节点信息与分发状态：distributed 行不再重复消息正文
        // （正文已在条目「消息内容」区展示）；skipped 行保留跳过原因。
        const detail = skipped
            ? (String(item?.reason || "").trim() || "暂无跳过原因")
            : "";
        const detailHtml = detail
            ? `<span class="notice-delivery-detail">${esc(detail)}</span>`
            : "";
        return `
            <div class="notice-delivery-item notice-delivery-item--${esc(descriptor.key)}">
                <span class="notice-delivery-icon" aria-hidden="true"><i data-lucide="${esc(descriptor.icon)}"></i></span>
                <span class="notice-delivery-main">
                    <span class="notice-delivery-head">
                        <span class="notice-delivery-target">${esc(targetTitle)}</span>
                        ${showNodeId ? `<span class="notice-delivery-node-id">${esc(targetNodeId)}</span>` : ""}
                        <span class="notice-delivery-status">${esc(descriptor.label)}</span>
                    </span>
                    ${detailHtml}
                </span>
            </div>
        `;
    }).join("");
    return `
        <div class="task-trace-field">
            ${labelHtml}
            <div class="notice-delivery-list">${rows}</div>
        </div>
    `;
}

function buildNodeMessageListSteps(node = {}) {
    const entries = Array.isArray(node?.message_list) ? node.message_list : [];
    return entries.map((entry, index) => {
        const status = messageListStatusDescriptor(entry?.status);
        return {
            traceKey: `message:${String(entry?.notification_id || entry?.epoch_id || index).trim() || index}`,
            title: formatMessageListTitle(entry, index),
            status: status.key,
            showStatus: false,
            open: index === 0,
            bodyHtml: [
                renderTraceField("状态", status.label, "已接收"),
                renderTraceField("接收时间", entry?.received_at || entry?.consumed_at, "暂无时间"),
                entry?.consumed_at ? renderTraceField("消费时间", entry?.consumed_at, "") : "",
                entry?.merged_at ? renderTraceField("并入上下文时间", entry?.merged_at, "") : "",
                renderTraceField("消息内容", entry?.message, "暂无消息内容"),
                renderMessageDeliveriesField(entry?.deliveries),
            ].join(""),
        };
    });
}

function spawnReviewRoundStatus(round = {}) {
    const allowedIndexes = Array.isArray(round?.allowed_indexes) ? round.allowed_indexes : [];
    const blockedSpecs = Array.isArray(round?.blocked_specs) ? round.blocked_specs : [];
    if (blockedSpecs.length && !allowedIndexes.length) return "error";
    if (allowedIndexes.length && !blockedSpecs.length) return "success";
    if (allowedIndexes.length || blockedSpecs.length) return "info";
    return "info";
}

function spawnSpecHasIndependentAcceptance(spec = null) {
    // 新载荷按 runtime_nodes 判（一个元素=运行时创建的一个独立节点）；历史轮次与
    // log_service 回落到原始 spec 投影片段时，只能照 requires_acceptance 的推断读。
    if (Array.isArray(spec?.runtime_nodes)) {
        return spec.runtime_nodes.some((node) => String(node?.node_kind || "").trim() === "acceptance");
    }
    if (typeof spec?.requires_acceptance === "boolean") {
        return spec.requires_acceptance;
    }
    return String(spec?.acceptance_prompt || "").trim() !== "";
}

function summarizeRequestedSpawnSpecs(specs = []) {
    const lines = (Array.isArray(specs) ? specs : []).map((spec, index) => {
        const goal = String(spec?.goal || "").trim() || `spec ${index + 1}`;
        const mode = String(spec?.execution_policy?.mode || "").trim() || "focus";
        const acceptance = spawnSpecHasIndependentAcceptance(spec) ? " [独立验收]" : "";
        return `#${index + 1} ${goal} [${mode}]${acceptance}`;
    });
    return lines.join("\n");
}

function summarizeAllowedSpawnEntries(entries = []) {
    const lines = (Array.isArray(entries) ? entries : [])
        .filter((entry) => String(entry?.review_decision || "").trim().toLowerCase() === "allowed")
        .map((entry, index) => {
            const goal = String(entry?.goal || "").trim() || `allowed ${index + 1}`;
            const childNodeId = String(entry?.child_node_id || "").trim();
            return childNodeId ? `${goal} -> ${childNodeId}` : goal;
        });
    return lines.join("\n");
}

function summarizeBlockedSpawnEntries(entries = []) {
    const lines = (Array.isArray(entries) ? entries : [])
        .filter((entry) => String(entry?.review_decision || "").trim().toLowerCase() === "blocked")
        .map((entry, index) => {
            const goal = String(entry?.goal || "").trim() || `blocked ${index + 1}`;
            const reason = String(entry?.blocked_reason || "").trim() || "暂无拦截原因";
            return `${goal}: ${reason}`;
        });
    return lines.join("\n");
}

function summarizeBlockedSpawnSuggestions(entries = []) {
    const lines = (Array.isArray(entries) ? entries : [])
        .filter((entry) => String(entry?.review_decision || "").trim().toLowerCase() === "blocked")
        .map((entry, index) => {
            const goal = String(entry?.goal || "").trim() || `blocked ${index + 1}`;
            const suggestion = String(entry?.blocked_suggestion || "").trim() || "暂无操作建议";
            return `${goal}: ${suggestion}`;
        });
    return lines.join("\n");
}

function buildSpawnReviewTraceSteps(rounds = []) {
    return (Array.isArray(rounds) ? rounds : []).map((round, index) => {
        const reviewedAt = String(round?.reviewed_at || "").trim();
        const formattedReviewedAt = reviewedAt
            ? (typeof formatCompactTime === "function" ? formatCompactTime(reviewedAt) : reviewedAt)
            : "";
        const titleSuffix = formattedReviewedAt ? ` · ${formattedReviewedAt}` : ` · 第${index + 1}轮`;
        return {
            traceKey: `spawn-review:${String(round?.round_id || index).trim() || index}`,
            title: `派生记录${titleSuffix}`,
            status: spawnReviewRoundStatus(round),
            showStatus: false,
            open: false,
            bodyHtml: [
                renderTraceField("原始请求", summarizeRequestedSpawnSpecs(round?.requested_specs), "本轮无原始派生请求"),
                renderTraceField("放行结果", summarizeAllowedSpawnEntries(round?.entries), "本轮无放行派生"),
                renderTraceField("被拦截原因", summarizeBlockedSpawnEntries(round?.entries), "本轮无被拦截项"),
                renderTraceField("操作建议", summarizeBlockedSpawnSuggestions(round?.entries), "本轮无额外建议"),
            ].join(""),
        };
    });
}

function renderMessageList(node, { viewState = null } = {}) {
    if (!U.adMessages) return false;
    const effectiveViewState = normalizeTaskDetailViewState(viewState || captureTaskDetailViewState());
    const preservedScrollTop = Number(effectiveViewState?.messageScrollTop || 0);
    const stepDescriptors = buildNodeMessageListSteps(node);
    const steps = stepDescriptors.map((step, index) => renderTraceStep({
        ...step,
        open: resolveTraceStepOpenState(
            step,
            {
                traceItems: Array.isArray(effectiveViewState?.messageItems) ? effectiveViewState.messageItems : [],
            },
            index,
        ),
    }));
    let traceList = U.adMessages.querySelector(".task-trace-list");
    if (!(traceList instanceof HTMLElement)) {
        traceList = document.createElement("div");
        traceList.className = "task-trace-list";
        U.adMessages.innerHTML = "";
        U.adMessages.appendChild(traceList);
    }
    const renderSignature = buildTraceRenderSignature(stepDescriptors);
    const renderNodeId = String(node?.node_id || "").trim();
    const shouldReplace = traceList.dataset.renderSignature !== renderSignature
        || traceList.dataset.renderNodeId !== renderNodeId;
    if (shouldReplace) {
        traceList.innerHTML = steps.length
            ? steps.join("")
            : '<div class="empty-state task-trace-empty">当前节点暂无消息。</div>';
        traceList.dataset.renderSignature = renderSignature;
        traceList.dataset.renderNodeId = renderNodeId;
    }
    renderMessageHeading(stepDescriptors.length);
    syncNoticeComposerState();
    if (shouldReplace) {
        const restoreScroll = () => {
            const currentTraceList = U.adMessages?.querySelector(".task-trace-list");
            if (!(currentTraceList instanceof HTMLElement)) return;
            setElementScrollTop(currentTraceList, preservedScrollTop);
        };
        restoreScroll();
        window.requestAnimationFrame(() => {
            restoreScroll();
            window.requestAnimationFrame(restoreScroll);
        });
    }
    return shouldReplace;
}

function renderSpawnReviewTrace(node, { viewState = null } = {}) {
    if (!U.adSpawnReviews) return false;
    const effectiveViewState = normalizeTaskDetailViewState(viewState || captureTaskDetailViewState());
    const preservedScrollTop = Number(effectiveViewState?.spawnReviewScrollTop || 0);
    const rounds = Array.isArray(node?.spawn_review_rounds) ? node.spawn_review_rounds : [];
    const stepDescriptors = buildSpawnReviewTraceSteps(rounds);
    const steps = stepDescriptors.map((step, index) => renderTraceStep({
        ...step,
        open: resolveTraceStepOpenState(
            step,
            {
                traceItems: Array.isArray(effectiveViewState?.spawnReviewItems) ? effectiveViewState.spawnReviewItems : [],
            },
            index,
        ),
    }));
    let traceList = U.adSpawnReviews.querySelector(".task-trace-list");
    if (!(traceList instanceof HTMLElement)) {
        traceList = document.createElement("div");
        traceList.className = "task-trace-list";
        U.adSpawnReviews.innerHTML = "";
        U.adSpawnReviews.appendChild(traceList);
    }
    const renderSignature = buildTraceRenderSignature(stepDescriptors);
    const renderNodeId = String(node?.node_id || "").trim();
    const shouldReplace = traceList.dataset.renderSignature !== renderSignature
        || traceList.dataset.renderNodeId !== renderNodeId;
    if (shouldReplace) {
        traceList.innerHTML = steps.length
            ? steps.join("")
            : '<div class="empty-state task-trace-empty">当前节点暂无派生记录。</div>';
        traceList.dataset.renderSignature = renderSignature;
        traceList.dataset.renderNodeId = renderNodeId;
    }
    renderSpawnReviewHeading(stepDescriptors.length);
    if (shouldReplace) {
        const restoreScroll = () => {
            const currentTraceList = U.adSpawnReviews?.querySelector(".task-trace-list");
            if (!(currentTraceList instanceof HTMLElement)) return;
            setElementScrollTop(currentTraceList, preservedScrollTop);
        };
        restoreScroll();
        window.requestAnimationFrame(() => {
            restoreScroll();
            window.requestAnimationFrame(restoreScroll);
        });
    }
    return shouldReplace;
}

function traceStatusLabel(status) {
    return ({
        info: "信息",
        running: "进行中",
        success: "成功",
        error: "失败",
        warning: "需处理",
        interrupted: "已中断",
    }[String(status || "")] || "信息");
}

function capTraceFieldBody(text) {
    // 封顶函数住在 app.js（那里是取数车道），这里按存在性调用：单独加载
    // task_view 的 harness 与 legacy 页面都还在渲染这些字段。
    const value = String(text ?? "");
    return typeof capTraceDisplayText === "function" ? capTraceDisplayText(value) : value;
}

function renderTraceLabelRow(label, { copyable = false, fullViewRef = "", loadMore = false } = {}) {
    // 字段标题行:需要时在标题旁挂一个复制按钮,点击复制下方代码块内容。
    const copyTitle = `复制 ${String(label || "").trim()}`.trim();
    const copyMarkup = copyable
        ? `<button type="button" class="task-trace-copy" aria-label="${esc(copyTitle)}" title="${esc(copyTitle)}"><i data-lucide="copy"></i></button>`
        : "";
    const fullViewMarkup = fullViewRef
        ? `<button type="button" class="task-trace-copy task-trace-full-view" data-full-view-ref="${esc(fullViewRef)}" aria-label="查看完整内容" title="查看完整内容"><i data-lucide="eye"></i></button>`
        : "";
    const loadMoreMarkup = loadMore
        ? `<button type="button" class="task-trace-copy task-trace-more" data-trace-more="" hidden aria-label="继续加载下一段" title="继续加载下一段"><i data-lucide="chevrons-down"></i></button>`
        : "";
    return `
        <div class="task-trace-label-row">
            <span class="task-trace-label">${esc(label)}</span>
            ${copyMarkup}
            ${fullViewMarkup}
            ${loadMoreMarkup}
        </div>
    `;
}

function renderTraceField(label, value, emptyText = "暂无内容", { decodeEscapes = false, copyable = false, argumentsLookupId = "" } = {}) {
    const text = capTraceFieldBody(readableText(value, { decodeEscapes, emptyText }));
    const lookupId = String(argumentsLookupId || "").trim();
    const lookupAttr = lookupId ? ` data-arguments-lookup="${esc(lookupId)}"` : "";
    return `
        <div class="task-trace-field">
            ${renderTraceLabelRow(label, { copyable })}
            <div class="code-block task-trace-code"${lookupAttr} data-empty-text="${esc(String(emptyText || ""))}">${esc(text)}</div>
        </div>
    `;
}

function renderTraceOutputField(label, value, outputRef = "", emptyText = "暂无内容", { decodeEscapes = false, copyable = false } = {}) {
    const text = capTraceFieldBody(readableText(value, { decodeEscapes, emptyText }));
    const normalizedRef = String(outputRef || "").trim();
    const refAttrs = normalizedRef
        ? ` data-output-ref="${esc(normalizedRef)}" data-empty-text="${esc(String(emptyText || ""))}"`
        : ` data-empty-text="${esc(String(emptyText || ""))}"`;
    return `
        <div class="task-trace-field">
            ${renderTraceLabelRow(label, { copyable, fullViewRef: normalizedRef, loadMore: !!normalizedRef })}
            <div class="code-block task-trace-code task-trace-output-value"${refAttrs}>${esc(text)}</div>
        </div>
    `;
}

function hydrateTraceOutputBlocks(root) {
    if (!(root instanceof HTMLElement)) return;
    if (typeof ensureTraceOutputCodeBlockContent !== "function") return;
    const selector = ".task-trace-output-value[data-output-ref]";
    const outputBlocks = Array.from(root.querySelectorAll(selector));
    if (!outputBlocks.length) {
        const single = root.querySelector(selector);
        if (single instanceof HTMLElement) outputBlocks.push(single);
    }
    outputBlocks.forEach((block) => {
        if (!(block instanceof HTMLElement)) return;
        void ensureTraceOutputCodeBlockContent(block);
    });
    if (typeof ensureTraceArgumentsCodeBlockContent !== "function") return;
    Array.from(root.querySelectorAll(".task-trace-code[data-arguments-lookup]")).forEach((block) => {
        if (!(block instanceof HTMLElement) || block.dataset.outputHydrated === "true") return;
        void ensureTraceArgumentsCodeBlockContent(block);
    });
}

function setTraceCopyButtonIcon(button, iconName) {
    if (!(button instanceof HTMLButtonElement)) return;
    button.innerHTML = `<i data-lucide="${esc(iconName)}"></i>`;
    if (typeof icons === "function") icons(true);
}

function flashTraceCopyButton(button, succeeded = true) {
    // 复制结果反馈:短暂换成 check/x 图标,1.4s 后恢复 copy 图标。
    if (!(button instanceof HTMLButtonElement)) return;
    const previousTimer = traceCopyFlashTimers.get(button);
    if (previousTimer) window.clearTimeout(previousTimer);
    setTraceCopyButtonIcon(button, succeeded ? "check" : "x");
    button.classList.toggle("is-copied", !!succeeded);
    button.classList.toggle("is-copy-failed", !succeeded);
    traceCopyFlashTimers.set(button, window.setTimeout(() => {
        traceCopyFlashTimers.delete(button);
        if (!(button instanceof HTMLButtonElement)) return;
        setTraceCopyButtonIcon(button, "copy");
        button.classList.remove("is-copied", "is-copy-failed");
    }, 1400));
}

async function resolveTraceFieldCopyText(code) {
    if (!(code instanceof HTMLElement)) return "";
    // 入参：显示面按一屏封顶过，DOM 里那句已经不是全量，复制一律按 call_id
    // 回取账本原文（同一份缓存，不再打网络）；取不到才退回屏幕上的文本。
    const lookupId = String(code.dataset.argumentsLookup || "").trim();
    if (lookupId && typeof getTraceArgumentsContentByCallId === "function") {
        try {
            const fullText = String(await getTraceArgumentsContentByCallId(lookupId) || "").trim();
            if (fullText) return fullText;
        } catch {
            // 终态缺失（404）等：留屏幕上那份提示可复制。
        }
    }
    // 懒加载输出块：复制走的是这条车道已经取到的那一屏正文（服务端行窗），
    // 整份不在这里搬——要全文用「查看完整」，它把渲染交给浏览器自己的新标签页。
    if (code.dataset.outputRef && code.dataset.outputHydrated !== "true"
        && typeof ensureTraceOutputCodeBlockContent === "function") {
        return String(await ensureTraceOutputCodeBlockContent(code) || "");
    }
    return String(code.textContent || "");
}

async function copyTraceFieldValue(button, label) {
    const field = button instanceof HTMLElement ? button.closest(".task-trace-field") : null;
    const code = field instanceof HTMLElement ? field.querySelector(".task-trace-code") : null;
    const fieldLabel = String(label || "").trim() || "内容";
    const emptyText = String(code?.dataset?.emptyText || "").trim();
    let text = String(await resolveTraceFieldCopyText(code) || "").trim();
    if (!text || (emptyText && text === emptyText)) text = "";
    if (!text) {
        flashTraceCopyButton(button, false);
        if (typeof showToast === "function") {
            showToast({ title: "没有可复制的内容", text: `${fieldLabel}当前为空。`, kind: "error" });
        }
        return;
    }
    const copied = typeof copyTextToClipboard === "function" && await copyTextToClipboard(text);
    flashTraceCopyButton(button, !!copied);
    if (typeof showToast === "function") {
        showToast({
            title: copied ? "已复制" : "复制失败",
            text: copied ? `${fieldLabel}已复制到剪贴板。` : "请手动选中文本后复制。",
            kind: copied ? "success" : "error",
        });
    }
}

function bindTraceFieldCopyActions(traceList) {
    // 与 bindTraceRoundToolStrips 一样按轨道根元素做一次委托绑定:
    // 内容重建(innerHTML 替换)不会丢失监听,通过 dataset 标记防重复绑定。
    if (!(traceList instanceof HTMLElement) || traceList.dataset.traceCopyBindings === "true") return;
    traceList.dataset.traceCopyBindings = "true";
    traceList.addEventListener("click", (event) => {
        const origin = event.target instanceof Element ? event.target : null;
        if (origin && typeof origin.closest === "function") {
            const fullView = origin.closest("[data-full-view-ref]");
            if (fullView && traceList.contains(fullView)) {
                event.preventDefault();
                event.stopPropagation();
                const ref = String(fullView.dataset?.fullViewRef || "").trim();
                if (ref && typeof openMediaViewer === "function") {
                    openMediaViewer({ mode: "content", title: "完整内容", outputRef: ref });
                }
                return;
            }
            const more = origin.closest("[data-trace-more]");
            if (more && traceList.contains(more)) {
                event.preventDefault();
                event.stopPropagation();
                if (typeof loadMoreTraceOutputBlock === "function") void loadMoreTraceOutputBlock(more);
                return;
            }
        }
        const button = event.target instanceof Element ? event.target.closest(".task-trace-copy") : null;
        if (!(button instanceof HTMLButtonElement) || !traceList.contains(button)) return;
        event.preventDefault();
        event.stopPropagation();
        const field = button.closest(".task-trace-field");
        const label = String(field?.querySelector?.(".task-trace-label")?.textContent || "").trim() || "内容";
        void copyTraceFieldValue(button, label);
    });
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
        String(step?.text || ""),
        step?.textOnly === true ? "1" : "0",
    ].join("\u0002")).join("\u0001");
}

function renderExecutionTrace(node, { viewState = null } = {}) {
    if (!U.adFlow) return;
    const effectiveViewState = normalizeTaskDetailViewState(viewState || captureTaskDetailViewState());
    const preservedTraceScrollTop = Number(effectiveViewState?.traceScrollTop || 0);
    const liveFrameMap = liveFramesByNodeId();
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
    bindTraceRoundToolStrips(traceList);
    if (typeof bindStageArchiveOpens === "function") bindStageArchiveOpens(traceList);
    bindTraceOutputAutoLoad(traceList);
    bindTraceFieldCopyActions(traceList);
    traceItems.filter((item) => item instanceof HTMLElement && item.open).forEach((item) => hydrateTraceOutputBlocks(item));
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

function paintTaskNodeErrorHistory(items) {
    if (!U.adErrorHistory) return false;
    const rows = Array.isArray(items) ? items : [];
    if (!rows.length) {
        U.adErrorHistory.innerHTML = '<div class="empty-state">暂无历史错误。</div>';
        return false;
    }
    U.adErrorHistory.innerHTML = rows.map((item) => `
        <article class="node-error-item">
            <div class="node-error-meta"><time>${esc(item.created_at || "")}</time>${item.node_title ? `<span>${esc(item.node_title)}</span>` : ""}</div>
            <pre class="node-error-text">${esc(item.error_text || "")}</pre>
        </article>`).join("");
    return true;
}

async function renderNodeErrorHistory(node, { force = false } = {}) {
    if (!U.adErrorHistory) return false;
    const nodeId = String(node?.node_id || "").trim();
    const taskId = String(S.currentTaskId || "").trim();
    if (!nodeId || !taskId) {
        U.adErrorHistory.innerHTML = '<div class="empty-state">请先选择节点。</div>';
        return false;
    }
    if (!S.taskNodeErrorHistories) S.taskNodeErrorHistories = {};
    const cacheKey = `${taskId}:${nodeId}`;
    if (!force && cacheKey in S.taskNodeErrorHistories) {
        return paintTaskNodeErrorHistory(S.taskNodeErrorHistories[cacheKey]);
    }
    U.adErrorHistory.innerHTML = '<div class="empty-state">正在加载历史错误...</div>';
    try {
        const payload = await ApiClient.getTaskNodeErrorLog(taskId, nodeId);
        if (String(S.currentTaskId || "").trim() !== taskId || String(S.selectedNodeId || "").trim() !== nodeId) return false;
        const items = Array.isArray(payload?.items) ? payload.items : [];
        S.taskNodeErrorHistories[cacheKey] = items;
        return paintTaskNodeErrorHistory(items);
    } catch (error) {
        if (isAbortLike(error)) return false;
        U.adErrorHistory.innerHTML = `<div class="empty-state error">${esc(error?.message || "历史错误加载失败")}</div>`;
        return true;
    }
}

function renderNodeContextPlaceholder(text = "展开后查看节点完整上下文。", { preserveViewState = false } = {}) {
    const viewState = preserveViewState && typeof captureTaskDetailViewState === "function"
        ? captureTaskDetailViewState()
        : null;
    if (U.artifactContent) {
        const decodedText = readableText(text, {
            decodeEscapes: true,
            emptyText: "展开后查看节点完整上下文。",
        });
        U.artifactContent.textContent = String(decodedText || "")
            .replace(/\\r\\n/g, "\n")
            .replace(/\\n/g, "\n")
            .replace(/\\r/g, "\n")
            .replace(/\\t/g, "\t")
            .replace(/\\"/g, '"')
            .replace(/\\\\/g, "\\")
            .trim();
    }
    if (viewState && typeof restoreTaskDetailViewState === "function") {
        restoreTaskDetailViewState(viewState, {
            detail: true,
            trace: false,
            traceItems: false,
            artifactList: false,
            artifactContent: true,
        });
    }
}

async function ensureTaskNodeLatestContext(nodeId, { force = false } = {}) {
    const key = String(nodeId || "").trim();
    const taskId = String(S.currentTaskId || "").trim();
    if (!taskId || !key) return null;
    if (!force && S.taskNodeLatestContexts[key]) return S.taskNodeLatestContexts[key];
    if (!force && S.taskNodeLatestContextRequests[key]) return S.taskNodeLatestContextRequests[key];
    const request = (async () => {
        try {
            const payload = await ApiClient.getTaskNodeLatestContext(taskId, key);
            if (!payload) return null;
            if (String(S.currentTaskId || "").trim() !== taskId) return null;
            S.taskNodeLatestContexts = { ...(S.taskNodeLatestContexts || {}), [key]: payload };
            return payload;
        } catch (error) {
            if (!isAbortLike(error)) throw error;
            return null;
        } finally {
            const nextRequests = { ...(S.taskNodeLatestContextRequests || {}) };
            delete nextRequests[key];
            S.taskNodeLatestContextRequests = nextRequests;
        }
    })();
    S.taskNodeLatestContextRequests = { ...(S.taskNodeLatestContextRequests || {}), [key]: request };
    return request;
}

async function loadSelectedNodeLatestContext({ force = false } = {}) {
    const taskId = String(S.currentTaskId || "").trim();
    const nodeId = String(S.selectedNodeId || "").trim();
    if (!taskId || !nodeId) return null;
    renderNodeContextPlaceholder("正在加载节点完整上下文...", { preserveViewState: true });
    try {
        const payload = await ensureTaskNodeLatestContext(nodeId, { force });
        if (String(S.currentTaskId || "").trim() !== taskId) return null;
        if (String(S.selectedNodeId || "").trim() !== nodeId) return null;
        const content = String(payload?.content || "");
        renderNodeContextPlaceholder(content.trim() || "当前节点暂无可用上下文快照。", { preserveViewState: true });
        return payload;
    } catch (error) {
        renderNodeContextPlaceholder(`加载完整上下文失败：${error?.message || error || "未知错误"}`, { preserveViewState: true });
        return null;
    }
}

async function handleNodeContextDisclosureToggle() {
    if (!U.nodeContextDisclosure) return;
    if (!U.nodeContextDisclosure.open) {
        renderNodeContextPlaceholder();
        return;
    }
    await loadSelectedNodeLatestContext({ force: true });
}

function renderTaskNodeDetailStatus(node) {
    if (!U.adStatus || !node) return;
    const taskPaused = taskPauseDisplayActive();
    const effectivePaused = effectiveTreeNodePaused(node, taskPaused);
    U.adStatus.textContent = taskNodeDisplayState(node, taskPaused);
    U.adStatus.dataset.status = node.visual_state || node.state || node.status || "";
    U.adStatus.dataset.paused = effectivePaused ? "true" : "false";
}

function normalizeTaskNodeModelRetryStatus(value = null) {
    if (!value || typeof value !== "object") return null;
    const state = String(value?.state || "").trim().toLowerCase();
    if (!TASK_NODE_MODEL_RETRY_LIVE_STATES.has(state)) return null;
    const rawCount = Number.parseInt(String(value?.retry_count ?? ""), 10);
    const retryCount = Number.isFinite(rawCount) && rawCount >= 0 ? rawCount : 0;
    const next = {
        state,
        retry_count: retryCount,
        error_message: String(value?.error_message || "").trim(),
    };
    const rawDelay = Number(value?.delay_seconds);
    if (Number.isFinite(rawDelay) && rawDelay >= 0) next.delay_seconds = rawDelay;
    const modelRefs = (Array.isArray(value?.model_refs) ? value.model_refs : [])
        .map((item) => String(item || "").trim())
        .filter(Boolean);
    if (modelRefs.length) next.model_refs = modelRefs;
    const lastRetryAt = String(value?.last_retry_at || "").trim();
    if (lastRetryAt) next.last_retry_at = lastRetryAt;
    const nextRetryAt = String(value?.next_retry_at || "").trim();
    if (nextRetryAt) next.next_retry_at = nextRetryAt;
    if (state === "waiting_upstream") {
        const rawWaiting = Number.parseInt(String(value?.waiting_seconds ?? ""), 10);
        if (Number.isFinite(rawWaiting) && rawWaiting > 0) next.waiting_seconds = rawWaiting;
        const rawChunks = Number.parseInt(String(value?.chunk_count ?? ""), 10);
        if (Number.isFinite(rawChunks) && rawChunks > 0) next.chunk_count = rawChunks;
    }
    return next;
}

function taskNodeModelRetryToastText(status = null) {
    const normalized = normalizeTaskNodeModelRetryStatus(status);
    if (!normalized) return "";
    if (normalized.state === "waiting_upstream") {
        // 与 CEO 会话同一读数口径：秒数 + 分片数，不写成重试。
        const parts = [`等待上游响应 ${Math.max(0, Number(normalized.waiting_seconds || 0))} 秒`];
        const chunks = Math.max(0, Number(normalized.chunk_count || 0));
        if (chunks) parts.push(`已收 ${chunks} 分片`);
        return parts.join(" · ");
    }
    const count = Math.max(0, Number(normalized.retry_count || 0));
    const countText = count > 0 ? `第 ${count} 次重试` : "自动重试";
    const parts = [countText];
    // 时间戳后端已按本地带偏移下发，这里只取 HH:MM:SS 钟点显示。
    const lastClock = (String(normalized.last_retry_at || "").match(/T(\d{2}:\d{2}:\d{2})/) || [])[1] || "";
    if (lastClock) parts.push(`最新 ${lastClock}`);
    const nextClock = (String(normalized.next_retry_at || "").match(/T(\d{2}:\d{2}:\d{2})/) || [])[1] || "";
    if (nextClock) parts.push(`下次 ${nextClock}`);
    const errorText = String(normalized.error_message || "").trim();
    if (errorText) parts.push(errorText);
    return parts.filter(Boolean).join(" · ");
}

function renderTaskNodeModelRetryToast(nodeOrStatus = null) {
    const toastEl = U.taskNodeModelRetryToast;
    const textEl = U.taskNodeModelRetryToastText;
    if (!toastEl || !textEl) return;
    const rawStatus = nodeOrStatus && typeof nodeOrStatus === "object"
        ? (nodeOrStatus.model_retry_status ?? nodeOrStatus)
        : null;
    const status = normalizeTaskNodeModelRetryStatus(rawStatus);
    const visible = !!status;
    const text = taskNodeModelRetryToastText(status);
    textEl.textContent = text;
    toastEl.hidden = !visible;
    if (toastEl.classList?.toggle) toastEl.classList.toggle("is-visible", visible);
    toastEl.setAttribute("aria-hidden", visible ? "false" : "true");
    refreshModelRetryToastClamp(toastEl, textEl);
}

function showTaskNodeLoadingState(node) {
    S.currentNodeDetail = { ...node };
    U.detail.style.display = "flex";
    if (U.nodeEmpty) U.nodeEmpty.style.display = "none";
    setTaskSelectionEmptyVisible(false);
    if (U.adRole) U.adRole.hidden = true;
    renderTaskNodeDetailStatus(node);
    renderTaskNodeModelRetryToast(null);
    if (U.adTokenSummary) U.adTokenSummary.textContent = "";
    if (U.adFlow) U.adFlow.innerHTML = '<div class="empty-state task-trace-empty">Loading node details...</div>';
    if (U.adMessages) U.adMessages.innerHTML = '<div class="empty-state task-trace-empty">Loading node details...</div>';
    if (U.adSpawnReviews) U.adSpawnReviews.innerHTML = '<div class="empty-state task-trace-empty">Loading node details...</div>';
    renderFlowHeading(0);
    renderMessageHeading(0);
    renderSpawnReviewHeading(0);
    syncNoticeComposerState();
    renderFinalOutput("Loading node output...");
    renderAcceptanceResult("Loading acceptance result...");
    if (U.nodeContextDisclosure) U.nodeContextDisclosure.open = false;
    renderNodeContextPlaceholder();
    U.feedTitle.textContent = formatNodeDetailHeading(node);
    U.feedTitle.title = formatNodeDetailHeading(node, { compact: false });
    setTaskDetailOpen(true);
    icons();
    refreshTaskDetailScrollRegions();
}

function executionTraceSummaryHasRoundBoundaries(summary) {
    const stages = Array.isArray(summary?.stages) ? summary.stages : [];
    return stages.some((stage) => Array.isArray(stage?.rounds) && stage.rounds.length > 0);
}

function taskNodePatchSummary(nodeId) {
    const key = String(nodeId || "").trim();
    return key ? ((S.taskNodePatchSummaries || {})[key] || null) : null;
}

function taskNodeTerminalSummaryIncomplete(detail) {
    if (!detail || typeof detail !== "object") return true;
    const normalizedStatus = String(detail?.status || detail?.state || "").trim().toLowerCase();
    if (!["success", "failed"].includes(normalizedStatus)) return false;
    return !String(detail?.final_output || "").trim()
        && !String(detail?.failure_reason || "").trim()
        && !String(detail?.check_result || "").trim();
}

function taskNodePatchSummaryIsNewer(detail, patchSummary) {
    if (!detail || typeof detail !== "object" || !patchSummary || typeof patchSummary !== "object") return false;
    const detailUpdatedAt = String(detail?.updated_at || "").trim();
    const patchUpdatedAt = String(patchSummary?.updated_at || "").trim();
    if (patchUpdatedAt && detailUpdatedAt && patchUpdatedAt !== detailUpdatedAt) return true;
    const normalizedPatchStatus = String(patchSummary?.status || "").trim().toLowerCase();
    const normalizedDetailStatus = String(detail?.status || detail?.state || "").trim().toLowerCase();
    if (normalizedPatchStatus && normalizedPatchStatus !== normalizedDetailStatus) return true;
    if (String(patchSummary?.final_output || "").trim() && String(patchSummary?.final_output || "").trim() !== String(detail?.final_output || "").trim()) {
        return true;
    }
    if (String(patchSummary?.failure_reason || "").trim() && String(patchSummary?.failure_reason || "").trim() !== String(detail?.failure_reason || "").trim()) {
        return true;
    }
    if (String(patchSummary?.check_result || "").trim() && String(patchSummary?.check_result || "").trim() !== String(detail?.check_result || "").trim()) {
        return true;
    }
    return false;
}

function taskNodeDetailNeedsRefresh(detail, { patchSummary = null } = {}) {
    if (!detail || typeof detail !== "object") return true;
    if (String(detail?.detail_level || "").trim().toLowerCase() !== "full") return true;
    if (taskNodeTerminalSummaryIncomplete(detail) && taskNodePatchSummaryIsNewer(detail, patchSummary)) return true;
    const summary = detail.execution_trace_summary;
    if (!summary || typeof summary !== "object") return false;
    const stages = Array.isArray(summary.stages) ? summary.stages : [];
    if (!stages.length) return false;
    const hasToolCalls = stages.some((stage) => Array.isArray(stage?.tool_calls) && stage.tool_calls.length > 0);
    if (!hasToolCalls) return false;
    return !executionTraceSummaryHasRoundBoundaries(summary);
}

async function ensureTaskNodeDetail(nodeId, { force = false } = {}) {
    const key = String(nodeId || "").trim();
    const taskId = String(S.currentTaskId || "").trim();
    if (!taskId || !key) return null;
    const cachedDetail = S.taskNodeDetails[key];
    const patchSummary = taskNodePatchSummary(key);
    if (!force && cachedDetail && !taskNodeDetailNeedsRefresh(cachedDetail, { patchSummary })) return cachedDetail;
    if (!force && S.taskNodeDetailRequests[key]) return S.taskNodeDetailRequests[key];
    S.taskNodeBusy = true;
    const request = (async () => {
        try {
            const detail = await ApiClient.getTaskNodeDetail(taskId, key, { detailLevel: "full" });
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
