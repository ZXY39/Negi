// 节点详情渲染：执行轨迹（回合/工具/参数/输出分页/复制）、消息投递、
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

function renderTraceStep({ traceKey = "", title, status = "info", statusLabel = "", open = false, bodyHtml = "", showRuntime = true, showStatus = true, extraClass = "", leadHtml = "", turnText = "", bare = false }) {
    if (bare) {
        return `
        <div class="task-trace-step task-trace-step--bare" data-trace-key="${esc(traceKey)}">${bodyHtml}</div>
    `;
    }
    const classes = ["interaction-step", "task-trace-step", esc(status)];
    const normalizedExtraClass = String(extraClass || "").trim();
    if (normalizedExtraClass) classes.push(esc(normalizedExtraClass));
    // 模型中途输出：折叠时摘要行只占一行向右溢出；展开后仍是同一段原文，
    // 只是换成固定高度可滚动，体内不再重复一遍正文。状态徽标也不挂在正文右侧。
    const assistantText = String(turnText || "").trim();
    if (assistantText) classes.push("task-trace-step--turn-text");
    const bodyContent = bodyHtml;
    const sideParts = [];
    if (showRuntime) sideParts.push('<span class="task-trace-runtime" hidden></span>');
    if (showStatus && !assistantText) {
        const label = String(statusLabel || "").trim() || traceStatusLabel(status);
        sideParts.push(`<span class="interaction-step-status">${esc(label)}</span>`);
    }
    // 中途正文行最左侧的展开钮：折叠时朝右，展开后转 90° 朝下。箭头用对称的 SVG 折线，
    // 墨迹中心与盒子中心重合——展开旋转绕的是圆心，不会像「>」或 CSS 折角那样转出偏心。
    const turnToggle = assistantText
        ? `<span class="task-trace-turn-toggle" aria-hidden="true">
                <svg viewBox="0 0 16 16" width="16" height="16" focusable="false"><path d="M6 4 L10 8 L6 12" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg>
            </span>`
        : "";
    return `
        <details class="${classes.join(" ")}" data-trace-key="${esc(traceKey)}" data-default-open="${open ? "true" : "false"}"${open ? " open" : ""}>
            <summary class="task-trace-summary">
                ${turnToggle}
                <span class="interaction-step-lead">
                    ${leadHtml}
                    <span class="interaction-step-title"${assistantText ? ` title="${esc(assistantText)}"` : ""}>${esc(title)}</span>
                </span>
                <span class="interaction-step-side">
                    ${sideParts.join("")}
                </span>
            </summary>
            <div class="task-trace-body">${bodyContent}</div>
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














// 消息状态标签的唯一映射：分发链路上的每一档都从这里出，前端不再各自写字面量。
// received/frozen 是投递前的视图层投影（后端 message_list 直接给这两档），
// pending/consumed/merged 是既有三态，判定源仍是后端账本；merged 在消息行上
// 与 consumed 同显示「已消费」（内容是否已并入由账本表达，不再分两个词）。
// 原始账本 token（delivered 等）一律不得作为标签外泄，未知值走中性「待确认」。
const MESSAGE_STATUS_LABELS = {
    received: "已接收",
    frozen: "已冻结待释放",
    pending: "待处理",
    consumed: "已消费",
    merged: "已消费",
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
// 分发窗口已随 epoch 删除，不再有额外的长宽容上限。








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
    (Array.isArray(trace?.turns) ? trace.turns : []).forEach((turn) => {
        steps.push(...(Array.isArray(turn?.tools) ? turn.tools : []));
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

// 工具卡左侧那颗图标：成功 √、失败 ！、在跑转圈。位置就是「加载 tool / 加载 skill」
// 放类型图标的那一格，右侧那条状态药丸因此不再需要。
function traceToolStatusIconName(status) {
    const normalized = String(status || "").trim().toLowerCase();
    if (normalized === "success") return "check";
    if (normalized === "error") return "alert-circle";
    if (normalized === "running" || normalized === "queued") return "loader";
    return "";
}

function renderTraceToolStatusIcon(status) {
    const name = traceToolStatusIconName(status);
    if (!name) return "";
    const tone = name === "check" ? "is-success" : name === "alert-circle" ? "is-error" : "is-running";
    return `<span class="tool-status-icon ${tone}" aria-hidden="true"><i data-lucide="${name}"></i></span>`;
}

function renderExecutionRoundToolChip(round, step, toolIndex) {
    const toolKey = buildExecutionRoundToolKey(round, step, toolIndex);
    const status = String(step?.status || "info").trim() || "info";
    const contextLoad = contextLoadDisplayFor(step);
    const titleText = contextLoad ? contextLoad.title : (String(step?.tool_name || "tool").trim() || "tool");
    // 加载器卡保留类型图标（风险度配色在那颗图标上），只有跑挂了才让位给 ！；
    // 普通工具卡没有类型图标，整格就是状态位。
    const contextLoadIconHtml = renderContextLoadIcon(contextLoad);
    const iconHtml = contextLoad
        ? (String(status).trim().toLowerCase() === "success"
            ? contextLoadIconHtml
            : (renderTraceToolStatusIcon(status) || contextLoadIconHtml))
        : renderTraceToolStatusIcon(status);
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
    // 起止时刻跟着字段走：外层不再印 Started / 已运行，时间只有点开这一层才看得到，
    // 看参数时能对上"什么时候开始"，看输出时能对上"什么时候结束"。
    const compact = (value) => {
        const text = String(value || "").trim();
        if (!text) return "";
        return typeof formatCompactTime === "function" ? formatCompactTime(text) : text;
    };
    const startedText = compact(step?.started_at);
    const finishedText = compact(step?.finished_at);
    const argumentsLabel = startedText ? `参数 · 开始 ${startedText}` : "参数";
    const outputLabel = finishedText ? `工具输出 · 结束 ${finishedText}` : "工具输出";
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
                renderTraceField(argumentsLabel, step?.arguments_full || step?.arguments_text, "无参数", { copyable: true, argumentsLookupId }),
                renderTraceOutputField(
                    outputLabel,
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
        if (isActive) {
            hydrateTraceOutputBlocks(panel);
            playTraceUnfoldWave(Array.from(panel.children || []));
        }
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

// 展开时让内容从上往下逐层浮入：每个条目错开 32ms；中途正文是整块文本，
// 用自上而下的抹开代替逐行拆分。动画类只挂到这一小段结束，流式重绘不会反复触发。
const TRACE_UNFOLD_TIMERS = new WeakMap();

function playTraceUnfoldWave(items, { textMode = false } = {}) {
    const list = Array.from(items || []).filter((el) => el instanceof HTMLElement);
    if (!list.length || typeof window === "undefined") return;
    const className = textMode ? "unfold-wave-text" : "unfold-wave-row";
    const otherClass = textMode ? "unfold-wave-row" : "unfold-wave-text";
    // 阶梯最多排到第 16 层：更靠后的条目在滚动视窗外，没必要再往下数。
    const maxIndex = Math.min(list.length - 1, 15);
    list.forEach((el, index) => {
        el.classList?.remove?.(otherClass);
        el.style?.setProperty?.("--unfold-index", String(Math.min(index, maxIndex)));
        // 摘掉再加回去，同一元素连续展开两次也能重新播。
        el.classList?.remove?.(className);
        void el.offsetWidth;
        el.classList?.add?.(className);
    });
    const timerKey = list[0];
    // 类挂到最后一层的淡入结束再多一点，提前摘会看到动画被截断。
    const totalMs = (textMode ? 440 : 320) + maxIndex * 32 + 80;
    window.clearTimeout(TRACE_UNFOLD_TIMERS.get(timerKey));
    TRACE_UNFOLD_TIMERS.set(timerKey, window.setTimeout(() => {
        list.forEach((el) => {
            el.classList?.remove?.(className);
            el.style?.removeProperty?.("--unfold-index");
        });
        TRACE_UNFOLD_TIMERS.delete(timerKey);
    }, totalMs));
}

function playTraceStepUnfold(traceStep) {
    if (!(traceStep instanceof HTMLElement)) return;
    // 中途正文：整块文本自上而下抹开；其它条目：正文里的每一层依次浮入。
    const turnTitle = traceStep.querySelector?.(".task-trace-summary .interaction-step-title");
    if (traceStep.classList?.contains?.("task-trace-step--turn-text") && turnTitle instanceof HTMLElement) {
        playTraceUnfoldWave([turnTitle], { textMode: true });
        return;
    }
    const body = Array.from(traceStep.children || []).find((el) => el.classList?.contains?.("task-trace-body"));
    if (body) playTraceUnfoldWave(Array.from(body.children || []));
}

function bindTraceOutputAutoLoad(traceList) {
    if (!(traceList instanceof HTMLElement) || traceList.dataset.traceOutputBindings === "true") return;
    traceList.dataset.traceOutputBindings = "true";
    // 波浪在点击时就挂上：details 的 toggle 是异步事件，等它到了再挂会先闪一帧全亮。
    traceList.addEventListener("click", (event) => {
        const summary = event.target instanceof Element ? event.target.closest(".task-trace-summary") : null;
        if (!(summary instanceof HTMLElement)) return;
        const traceStep = summary.parentElement;
        if (!(traceStep instanceof HTMLElement) || !traceList.contains(traceStep)) return;
        if (!traceStep.classList?.contains?.("task-trace-step") || traceStep.open) return;
        playTraceStepUnfold(traceStep);
    }, true);
    traceList.addEventListener("toggle", (event) => {
        const traceStep = event.target instanceof Element ? event.target.closest(".task-trace-step") : null;
        if (!(traceStep instanceof HTMLElement) || !traceList.contains(traceStep) || !traceStep.open) return;
        hydrateTraceOutputBlocks(traceStep);
    }, true);
}

function bindTaskTraceListInteractions(traceList) {
    if (!(traceList instanceof HTMLElement) || traceList.dataset.traceUiBindings === "true") return;
    traceList.dataset.traceUiBindings = "true";
    traceList.addEventListener("click", (event) => {
        const target = event.target instanceof Element ? event.target : null;
        if (!target) return;
        const nodeRef = target.closest("[data-node-jump]");
        if (nodeRef instanceof HTMLElement) {
            event.preventDefault();
            event.stopPropagation();
            void jumpToTaskNodeDetail(nodeRef.dataset.nodeJump || "");
            return;
        }
        // 展开态的中途输出正文：点文字照常折叠；只有拖选文本或点滚动条时才拦下
        // <details> 的默认切换，否则用户没法在滚动框里选中/拖动滚动条。
        const turnTitle = target.closest(".task-trace-step--turn-text[open] .interaction-step-title");
        if (turnTitle instanceof HTMLElement) {
            const selectionText = typeof window.getSelection === "function"
                ? String(window.getSelection() || "").trim()
                : "";
            const scrollbarWidth = Math.max(0, turnTitle.offsetWidth - turnTitle.clientWidth);
            const rect = turnTitle.getBoundingClientRect();
            const onScrollbar = scrollbarWidth > 0 && event.clientX - rect.left >= turnTitle.clientWidth;
            if (selectionText || onScrollbar) event.preventDefault();
        }
    });
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
                rounds: [],
            });
        }
        if (!tools.length) return;
        let target = [...entries].reverse().find((entry) => entry.kind === "text");
        if (!target) {
            target = entries.find((entry) => entry.kind === "preamble");
            if (!target) {
                target = { kind: "preamble", seq: "", text: "", created_at: "", rounds: [] };
                entries.push(target);
            }
        }
        target.rounds.push({
            seq: turn.seq,
            created_at: String(turn?.created_at || ""),
            thinking: turn?.thinking || null,
            tools,
        });
    });

    const steps = [];
    entries.forEach((entry) => {
        const tools = entry.rounds.flatMap((round) => round.tools);
        if (entry.kind === "preamble") {
            // 第一段叙述之前的工具轮不再编一个「开始执行」标题，直接铺工具。
            steps.push({
                traceKey: "turn:preamble",
                title: "",
                bare: true,
                status: roundTraceStatus({ tools }),
                open: false,
                tools,
                bodyHtml: entry.rounds.map(renderTurnRoundBlock).join(""),
            });
            return;
        }
        if (entry.rounds.length) {
            steps.push({
                traceKey: `turn:${entry.seq}`,
                // 标题保留全文：摘要行由 CSS 收成一行省略号，展开后正文给完整原文，
                // 不再按 80 字硬截断（截断会吃掉模型中途输出的后半段要求）。
                title: entry.text,
                turnText: entry.text,
                status: roundTraceStatus({ tools }),
                open: false,
                tools,
                bodyHtml: entry.rounds.map(renderTurnRoundBlock).join(""),
            });
            return;
        }
        steps.push({
            traceKey: `turn:${entry.seq}`,
            title: entry.text,
            turnText: entry.text,
            textOnly: true,
            status: "info",
            open: false,
            tools: [],
            bodyHtml: "",
        });
    });
    return steps;
}

// 这一轮的思考读数：文案口径与前门气泡那一行同出一处（`thinkingReadingParts`），这里只喂数。
// 三个口径都没上报时返回空串，整行不画——空白读作"没上报"，不是"想了 0 秒"。
function taskRoundThinkingText(thinking) {
    if (!thinking || typeof thinking !== "object") return "";
    if (typeof thinkingReadingParts !== "function") return "";
    return thinkingReadingParts({
        seconds: thinking.seconds,
        chars: thinking.chars,
        estTokens: thinking.est_tokens,
        reportedTokens: thinking.tokens,
    }).join(" · ");
}

// 账本一行就是一轮：工具条与思考读数都属于这一轮。多轮叠在组体内纵向排列，
// 不并成一条横向长条，否则"哪一轮调了什么"会被抹平成一行。
// 时间不进外层：起止时刻在工具展开后的参数 / 输出两栏里，外层只留工具本身。
function renderTurnRoundBlock(round) {
    const roundKey = `turn:${String(round?.seq ?? "").trim()}`;
    const thinkingText = taskRoundThinkingText(round?.thinking);
    return `
        <section class="task-trace-round-group" data-round-key="${esc(roundKey)}">
            ${renderExecutionRoundToolStrip({ round_id: roundKey, created_at: round?.created_at || "" }, round?.tools)}
            ${thinkingText ? `<div class="task-trace-round-thinking">${esc(thinkingText)}</div>` : ""}
        </section>
    `;
}

function buildTurnTraceSteps(turns, initialPromptStep) {
    const steps = [initialPromptStep, ...buildTurnGroupDescriptors(turns)];
    const last = steps[steps.length - 1];
    // 中途输出默认只占一行；带正文的末组也不再自动展开，点击才看全文。
    if (last && !last.textOnly && !String(last.turnText || "").trim()) last.open = true;
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
    // 列表条目上的 key 是**视觉档位**（warning/info），与语义 key 是两套词表：
    // 这里只做映射，不得把两套并成一套，否则徽标配色合同会被改掉。
    const key = ["pending", "received", "frozen"].includes(descriptor.key) ? "warning" : "info";
    // merged 与 consumed 共用「已消费」文案，图标也统一成勾选，避免同义词两套图形。
    const icon = descriptor.key === "merged" ? "circle-check" : descriptor.icon;
    return { key, statusKey: descriptor.key, label: descriptor.label, icon };
}

function taskMessagePreviewText(message, maxChars = 140) {
    const text = String(message || "").replace(/\s+/g, " ").trim();
    if (!text) return "（无内容）";
    const chars = Array.from(text);
    return chars.length > maxChars ? `${chars.slice(0, maxChars).join("")}…` : text;
}

function taskMessageTimeText(entry) {
    const time = String(entry?.received_at || entry?.consumed_at || entry?.merged_at || "").trim();
    if (!time) return "暂无时间";
    return typeof formatCompactTime === "function" ? formatCompactTime(time) : time;
}

function renderTaskMessageBody(message) {
    return `<div class="task-trace-message-body">${esc(String(message || "").trim() || "暂无消息内容")}</div>`;
}

function taskNodeDisplayId(nodeId) {
    const raw = String(nodeId || "").trim();
    return raw.startsWith("node:") ? raw.slice("node:".length) : raw;
}

// 节点关系预置词表：后端尚未下发关系字段，先给中性的「节点」占位；
// 接通后按 node_relation=parent/child/sibling 直接落到三档文案。
const TASK_NODE_RELATION_LABELS = {
    parent: "父节点",
    child: "子节点",
    sibling: "同级节点",
};
const TASK_NODE_RELATION_PLACEHOLDER = "节点";

function taskNodeRelationLabel(entry = null) {
    const raw = String(entry?.node_relation || entry?.relation || "").trim().toLowerCase();
    return TASK_NODE_RELATION_LABELS[raw] || TASK_NODE_RELATION_PLACEHOLDER;
}

// 节点 id 药丸：去 node: 前缀、带图标与关系文字，点击直接打开该节点详情
// （消息行与派生记录共用）。
function renderTaskNodeRef(nodeId, { icon = "circle-dot", relation = "" } = {}) {
    const raw = String(nodeId || "").trim();
    if (!raw) return "";
    const relationLabel = String(relation || "").trim() || TASK_NODE_RELATION_PLACEHOLDER;
    return `<button type="button" class="task-node-ref" data-node-jump="${esc(raw)}" title="打开节点 ${esc(raw)} 的详情"><i data-lucide="${esc(icon)}"></i><span class="task-node-ref-type">${esc(relationLabel)}</span><span class="task-node-ref-id">${esc(taskNodeDisplayId(raw))}</span></button>`;
}

function renderTaskMessageStatus(status) {
    const label = String(status?.label || "").trim();
    const icon = String(status?.icon || "circle").trim() || "circle";
    return `<span class="task-message-status" data-status="${esc(status?.statusKey || "unknown")}" data-tone="${esc(status?.key || "info")}" title="${esc(label)}" aria-label="${esc(label)}"><i data-lucide="${esc(icon)}"></i></span>`;
}

function buildNodeMessageListSteps(node = {}) {
    const entries = Array.isArray(node?.message_list) ? node.message_list : [];
    return entries.map((entry, index) => {
        const status = messageListStatusDescriptor(entry?.status);
        const sourceNodeId = String(entry?.source_node_id || "").trim();
        const leadHtml = [
            renderTaskMessageStatus(status),
            `<span class="task-message-time">${esc(taskMessageTimeText(entry))}</span>`,
            sourceNodeId
                ? renderTaskNodeRef(sourceNodeId, {
                    icon: "arrow-down-left",
                    relation: taskNodeRelationLabel(entry),
                })
                : "",
        ].join("");
        return {
            traceKey: `message:${String(entry?.notification_id || entry?.epoch_id || index).trim() || index}`,
            // 折叠态一行：状态图标 · 时间 · 来自节点 id · 内容预览；展开态只给消息正文。
            title: taskMessagePreviewText(entry?.message),
            leadHtml,
            status: status.key,
            showStatus: false,
            open: index === 0,
            extraClass: "task-trace-step--message",
            bodyHtml: renderTaskMessageBody(entry?.message),
        };
    });
}

function buildNodeSentMessageListSteps(node = {}) {
    const entries = Array.isArray(node?.sent_message_list) ? node.sent_message_list : [];
    return entries.map((entry, index) => {
        const status = messageListStatusDescriptor(entry?.status);
        const targetNodeId = String(entry?.target_node_id || "").trim();
        const leadHtml = [
            renderTaskMessageStatus(status),
            `<span class="task-message-time">${esc(taskMessageTimeText(entry))}</span>`,
            targetNodeId
                ? renderTaskNodeRef(targetNodeId, {
                    icon: "arrow-up-right",
                    relation: taskNodeRelationLabel(entry),
                })
                : "",
        ].join("");
        return {
            traceKey: `sent-message:${String(entry?.notification_id || index).trim() || index}`,
            title: taskMessagePreviewText(entry?.message),
            leadHtml,
            status: status.key,
            showStatus: false,
            open: index === 0,
            extraClass: "task-trace-step--message",
            bodyHtml: renderTaskMessageBody(entry?.message),
        };
    });
}

function activeTaskMessageListSteps(node) {
    return S.taskMessageViewMode === "outbox"
        ? buildNodeSentMessageListSteps(node)
        : buildNodeMessageListSteps(node);
}

function spawnReviewRoundStatus(round = {}) {
    const allowedIndexes = Array.isArray(round?.allowed_indexes) ? round.allowed_indexes : [];
    const blockedSpecs = Array.isArray(round?.blocked_specs) ? round.blocked_specs : [];
    if (blockedSpecs.length && !allowedIndexes.length) return "error";
    if (allowedIndexes.length && !blockedSpecs.length) return "success";
    if (allowedIndexes.length || blockedSpecs.length) return "info";
    return "info";
}

function spawnSpecModeDescriptor(spec = null) {
    const mode = String(spec?.execution_policy?.mode || "").trim().toLowerCase();
    if (mode === "coverage") return { key: "coverage", label: "全面", icon: "layers" };
    return { key: "focus", label: "专注", icon: "target" };
}

function renderSpawnNodeItem(entry = {}, spec = null, childResult = null) {
    const childNodeId = String(entry?.child_node_id || "").trim();
    const goal = String(entry?.goal || spec?.goal || childNodeId || "未命名节点").trim();
    const mode = spawnSpecModeDescriptor(spec);
    const prompt = String(spec?.prompt || "").trim() || "暂无提示词";
    const acceptance = String(childResult?.check_result || "").trim() || "暂无验收结果";
    const nodeRef = renderTaskNodeRef(childNodeId, { relation: taskNodeRelationLabel(entry) });
    return `
        <details class="spawn-node" data-spawn-node-id="${esc(childNodeId)}">
            <summary class="spawn-node-summary">
                <span class="spawn-node-mode spawn-node-mode--${mode.key}"><i data-lucide="${mode.icon}"></i>${mode.label}</span>
                <span class="spawn-node-goal">${esc(goal)}</span>
                ${nodeRef || '<span class="spawn-node-id">未派生</span>'}
            </summary>
            <div class="spawn-node-body">
                <div class="spawn-node-field">
                    <span class="spawn-node-label">提示词</span>
                    <div class="spawn-node-text">${esc(prompt)}</div>
                </div>
                <div class="spawn-node-field">
                    <span class="spawn-node-label">验收结果</span>
                    <div class="spawn-node-text">${esc(acceptance)}</div>
                </div>
            </div>
        </details>
    `;
}

function buildSpawnReviewTraceSteps(rounds = [], childResults = []) {
    const results = Array.isArray(childResults) ? childResults : [];
    return (Array.isArray(rounds) ? rounds : []).map((round, index) => {
        const reviewedAt = String(round?.reviewed_at || "").trim();
        const formattedReviewedAt = reviewedAt
            ? (typeof formatCompactTime === "function" ? formatCompactTime(reviewedAt) : reviewedAt)
            : "";
        const titleSuffix = formattedReviewedAt ? ` · ${formattedReviewedAt}` : ` · 第${index + 1}轮`;
        const specs = Array.isArray(round?.requested_specs) ? round.requested_specs : [];
        const specByIndex = new Map(
            specs.map((spec, specIndex) => [
                Number.isFinite(Number(spec?.index)) ? Number(spec.index) : specIndex,
                spec,
            ]),
        );
        const allowedEntries = (Array.isArray(round?.entries) ? round.entries : [])
            .filter((entry) => String(entry?.review_decision || "").trim().toLowerCase() === "allowed");
        const nodeItems = allowedEntries.map((entry, entryIndex) => {
            const specIndex = Number.isFinite(Number(entry?.index)) ? Number(entry.index) : entryIndex;
            const childNodeId = String(entry?.child_node_id || "").trim();
            const childResult = results.find((item) => (
                String(item?.child_node_id || "").trim() === childNodeId
            )) || null;
            return renderSpawnNodeItem(entry, specByIndex.get(specIndex) || null, childResult);
        }).join("");
        return {
            traceKey: `spawn-review:${String(round?.round_id || index).trim() || index}`,
            title: nodeItems
                ? `派生记录${titleSuffix} · ${allowedEntries.length} 个节点`
                : `派生记录${titleSuffix}`,
            status: spawnReviewRoundStatus(round),
            showStatus: false,
            open: false,
            bodyHtml: nodeItems
                ? `<div class="spawn-node-list">${nodeItems}</div>`
                : '<div class="empty-state task-trace-empty">本轮无派生节点。</div>',
        };
    });
}

function jumpToTaskNodeDetail(nodeId) {
    const key = String(nodeId || "").trim();
    if (!key) return;
    const node = (typeof findTreeNode === "function" ? findTreeNode(S.treeView, key) : null)
        || (typeof treeSnapshotNode === "function" ? treeSnapshotNode(key) : null)
        || S.taskNodeDetails?.[key]
        || null;
    if (!node) {
        if (typeof showToast === "function") {
            showToast({ title: "无法打开节点", text: `节点 ${key} 当前不在任务树中`, kind: "warn" });
        }
        return;
    }
    const previousNodeId = String(S.selectedNodeId || "").trim();
    if (previousNodeId && previousNodeId !== key && typeof stashTaskDetailViewState === "function") {
        stashTaskDetailViewState({ nodeId: previousNodeId });
    }
    S.selectedNodeId = key;
    if (typeof syncExecutionTreeSelection === "function") syncExecutionTreeSelection(previousNodeId, key);
    if (typeof showAgent === "function") void showAgent(node, { preserveViewState: false, forceRefresh: true });
    // 详情先开；定位/居中在后台做，不阻塞抽屉（隐藏窗口里 rAF 可能不跑）。
    if (typeof locateTaskNode === "function") {
        void locateTaskNode(key).catch(() => null);
    }
}

function syncTaskMessageTabs() {
    const tabs = U.adMessageTabs;
    if (!tabs) return;
    const mode = S.taskMessageViewMode === "outbox" ? "outbox" : "inbox";
    tabs.querySelectorAll("[data-message-tab]").forEach((button) => {
        const active = String(button.dataset.messageTab || "") === mode;
        button.classList.toggle("is-active", active);
        button.setAttribute("aria-selected", active ? "true" : "false");
    });
}

function setTaskMessageViewMode(mode) {
    const next = String(mode || "").trim() === "outbox" ? "outbox" : "inbox";
    if ((S.taskMessageViewMode === "outbox" ? "outbox" : "inbox") === next) return;
    S.taskMessageViewMode = next;
    syncTaskMessageTabs();
    if (S.currentNodeDetail) renderMessageList(S.currentNodeDetail);
}

function bindTaskMessageTabs() {
    const tabs = U.adMessageTabs;
    if (!tabs || tabs.dataset.bound === "true") {
        syncTaskMessageTabs();
        return;
    }
    tabs.dataset.bound = "true";
    tabs.addEventListener("click", (event) => {
        const button = event.target instanceof Element ? event.target.closest("[data-message-tab]") : null;
        if (!(button instanceof HTMLElement)) return;
        setTaskMessageViewMode(button.dataset.messageTab);
    });
    syncTaskMessageTabs();
}

function renderMessageList(node, { viewState = null } = {}) {
    if (!U.adMessages) return false;
    const effectiveViewState = normalizeTaskDetailViewState(viewState || captureTaskDetailViewState());
    const preservedScrollTop = Number(effectiveViewState?.messageScrollTop || 0);
    const stepDescriptors = activeTaskMessageListSteps(node);
    const isOutbox = S.taskMessageViewMode === "outbox";
    const emptyText = isOutbox ? "当前节点暂无发出消息。" : "当前节点暂无消息。";
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
    const renderSignature = `${isOutbox ? "outbox" : "inbox"}:${buildTraceRenderSignature(stepDescriptors)}`;
    const renderNodeId = String(node?.node_id || "").trim();
    const shouldReplace = traceList.dataset.renderSignature !== renderSignature
        || traceList.dataset.renderNodeId !== renderNodeId;
    if (shouldReplace) {
        traceList.innerHTML = steps.length
            ? steps.join("")
            : `<div class="empty-state task-trace-empty">${esc(emptyText)}</div>`;
        traceList.dataset.renderSignature = renderSignature;
        traceList.dataset.renderNodeId = renderNodeId;
        // 状态图标与节点 id 药丸是 lucide 占位，重建后立即水合（切 tab 时 showAgent 不会再兜底）。
        if (typeof icons === "function") icons();
    }
    bindTaskMessageTabs();
    bindTaskTraceListInteractions(traceList);
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
    const stepDescriptors = buildSpawnReviewTraceSteps(rounds, node?.direct_child_results);
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
        if (typeof icons === "function") icons();
    }
    bindTaskTraceListInteractions(traceList);
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

function bindTaskOutputCopyActions() {
    const targets = [
        { targetId: "ad-output", buttonId: "ad-output-copy", label: "提交输出", emptyText: "暂无提交输出" },
        { targetId: "ad-check", buttonId: "ad-check-copy", label: "验收结果", emptyText: "暂无验收结果" },
    ];
    targets.forEach(({ targetId, buttonId, label, emptyText }) => {
        const button = document.getElementById(buttonId);
        if (!(button instanceof HTMLButtonElement) || button.dataset.copyBound === "true") return;
        button.dataset.copyBound = "true";
        button.addEventListener("click", async (event) => {
            event.preventDefault();
            event.stopPropagation();
            const target = document.getElementById(targetId);
            const text = String(target?.textContent || "").trim();
            if (!text || text === emptyText) {
                flashTraceCopyButton(button, false);
                if (typeof showToast === "function") {
                    showToast({ title: "没有可复制的内容", text: `${label}当前为空。`, kind: "error" });
                }
                return;
            }
            let copied = false;
            try {
                copied = typeof copyTextToClipboard === "function" && await copyTextToClipboard(text);
            } catch {
                copied = false;
            }
            flashTraceCopyButton(button, !!copied);
            if (typeof showToast === "function") {
                showToast({
                    title: copied ? "已复制" : "复制失败",
                    text: copied ? `${label}已复制到剪贴板。` : "请手动选中文本后复制。",
                    kind: copied ? "success" : "error",
                });
            }
        });
    });
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

function renderInitialPromptBox(step, node) {
    if (!U.adInitialPrompt || !step) return false;
    const nodeKey = `${S.currentTaskId || ""}:${node?.node_id || ""}`;
    const existing = U.adInitialPrompt.querySelector("details");
    const keepOpen = U.adInitialPrompt.dataset.renderNodeId === nodeKey && existing
        ? !!existing.open
        : false;
    const html = renderTraceStep({ ...step, open: keepOpen });
    if (U.adInitialPrompt.dataset.renderSignature === html
        && U.adInitialPrompt.dataset.renderNodeId === nodeKey) {
        return false;
    }
    U.adInitialPrompt.innerHTML = html;
    U.adInitialPrompt.dataset.renderSignature = html;
    U.adInitialPrompt.dataset.renderNodeId = nodeKey;
    const details = U.adInitialPrompt.querySelector("details");
    details?.addEventListener("toggle", () => {
        if (U.adInitialPrompt?.dataset.renderNodeId !== nodeKey) return;
        U.adInitialPrompt.dataset.open = details.open ? "true" : "false";
    });
    return true;
}

function renderExecutionTrace(node, { viewState = null } = {}) {
    if (!U.adFlow) return;
    const effectiveViewState = normalizeTaskDetailViewState(viewState || captureTaskDetailViewState());
    const preservedTraceScrollTop = Number(effectiveViewState?.traceScrollTop || 0);
    const liveFrameMap = liveFramesByNodeId();
    const fallbackLiveFrame = liveFrameMap.get(String(node?.node_id || "").trim()) || null;
    const trace = node?.executionTrace || buildNodeExecutionTrace(node, node, fallbackLiveFrame);
    // 流程框头部用量每次重建都回填：切节点时的 loading 清空不会把它永久留在空态。
    if (U.adTokenSummary && typeof taskFlowUsageText === "function") {
        U.adTokenSummary.textContent = taskFlowUsageText(node, trace);
    }
    const allStepDescriptors = buildExecutionTraceSteps(trace, node);
    const initialPromptStep = allStepDescriptors.find((step) => String(step?.traceKey || "") === "initial_prompt") || null;
    // 初始提示词独立成框，Execution Flow 只装中途文本与工具调用。
    // 没有独立宿主（旧页面/单测桩）时仍留在列表里，行为与改造前一致。
    const splitInitialPrompt = !!(U.adInitialPrompt && initialPromptStep);
    if (splitInitialPrompt) renderInitialPromptBox(initialPromptStep, node);
    const stepDescriptors = splitInitialPrompt
        ? allStepDescriptors.filter((step) => step !== initialPromptStep)
        : allStepDescriptors;
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
        traceList.innerHTML = steps.length
            ? steps.join("")
            : '<div class="empty-state task-trace-empty">暂无执行流程。</div>';
        traceList.dataset.renderSignature = renderSignature;
        traceList.dataset.renderNodeId = renderNodeId;
    }
    renderFlowHeading();
    const traceItems = Array.from(traceList.querySelectorAll(".task-trace-step"));
    traceItems.forEach((item) => {
        if (!(item instanceof HTMLElement)) return;
        const runtimeEl = item.querySelector(".task-trace-runtime");
        if (runtimeEl instanceof HTMLElement) updateRuntimeBadge(item, runtimeEl);
    });
    bindTraceRoundToolStrips(traceList);
    bindTraceOutputAutoLoad(traceList);
    bindTraceFieldCopyActions(traceList);
    bindTaskTraceListInteractions(traceList);
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
    const nextText = readableText(text, { decodeEscapes: true, emptyText: "暂无提交输出" });
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
    const wasHidden = toastEl.hidden === true;
    const textChanged = textEl.textContent !== text;
    // 实时帧每秒会打好几次补丁：文本没变就不要再写 DOM，否则每次写 textContent
    // 都会强制重排 + 重新判定 clamp，小框看起来就是高频抖动。
    if (textChanged) textEl.textContent = text;
    const nextHidden = !visible;
    const ariaHidden = visible ? "false" : "true";
    if (toastEl.hidden !== nextHidden) toastEl.hidden = nextHidden;
    if (toastEl.classList?.toggle) toastEl.classList.toggle("is-visible", visible);
    const currentAriaHidden = typeof toastEl.getAttribute === "function" ? toastEl.getAttribute("aria-hidden") : ariaHidden;
    if (currentAriaHidden !== ariaHidden) toastEl.setAttribute("aria-hidden", ariaHidden);
    if (textChanged || wasHidden !== nextHidden) refreshModelRetryToastClamp(toastEl, textEl);
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
    return false;
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
