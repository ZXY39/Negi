// 节点详情状态层：执行轨迹摘要归一化、回合分组与 live 帧投影。
// 纯数据函数；渲染在 org_graph_task_view.js。

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

function normalizeSummaryTraceToolCall(step, index = 0) {
    const rawStatus = String(step?.status || "").trim().toLowerCase();
    const explicitStatus = ({
        queued: "running",
        running: "running",
        success: "success",
        error: "error",
        warning: "warning",
        interrupted: "interrupted",
    }[rawStatus] || "");
    const argumentsText = String(step?.arguments_text || step?.arguments_preview || "");
    const outputText = String(step?.output_text || step?.output_preview || step?.text || "");
    const outputRef = String(step?.output_ref || "");
    const startedAt = String(step?.started_at || "");
    const finishedAt = String(step?.finished_at || "");
    return {
        tool_call_id: String(step?.tool_call_id || `summary-tool-${index + 1}`),
        tool_name: String(step?.tool_name || "tool"),
        arguments_text: argumentsText,
        output_text: outputText,
        output_ref: outputRef,
        started_at: startedAt,
        finished_at: finishedAt,
        elapsed_seconds: Number.isFinite(Number(step?.elapsed_seconds)) ? Number(step.elapsed_seconds) : null,
        status: explicitStatus || (finishedAt
            ? "success"
            : ((outputText.trim() || outputRef.trim())
                ? "success"
                : (startedAt ? "running" : "info"))),
        recovery_decision: String(step?.recovery_decision || ""),
        related_tool_call_ids: Array.isArray(step?.related_tool_call_ids) ? step.related_tool_call_ids.map((item) => String(item || "")) : [],
        attempted_tools: Array.isArray(step?.attempted_tools) ? step.attempted_tools.map((item) => String(item || "")) : [],
        evidence: Array.isArray(step?.evidence) ? step.evidence.filter((item) => item && typeof item === "object") : [],
        lost_result_summary: String(step?.lost_result_summary || ""),
        contract_delivery: String(step?.contract_delivery || ""),
    };
}

function normalizeTurnThinking(thinking) {
    // 轮次上只有那四个标量读数：思考全文从不外发，行上多带的键也不许跟着进渲染。
    if (!thinking || typeof thinking !== "object") return null;
    const toNumber = (value) => {
        const num = Number(value);
        return Number.isFinite(num) && num >= 0 ? num : 0;
    };
    const reading = {
        seconds: toNumber(thinking.seconds),
        chars: toNumber(thinking.chars),
        est_tokens: toNumber(thinking.est_tokens),
        // 缺键＝这一族口径这次没上报，与真实的 0 分开，界面才敢标估算还是真值。
        tokens: thinking.tokens === null || thinking.tokens === undefined ? null : toNumber(thinking.tokens),
    };
    if (!reading.seconds && !reading.chars && !reading.est_tokens && !reading.tokens) return null;
    return reading;
}

function normalizeTraceTurns(turns) {
    return (Array.isArray(turns) ? turns : [])
        .map((turn, turnIndex) => ({
            seq: normalizeInt(turn?.seq, turnIndex + 1),
            text: String(turn?.text || ""),
            text_ref: String(turn?.text_ref || ""),
            created_at: String(turn?.created_at || ""),
            status: String(turn?.status || ""),
            thinking: normalizeTurnThinking(turn?.thinking),
            tools: (Array.isArray(turn?.tools) ? turn.tools : []).map((step, stepIndex) => (
                normalizeSummaryTraceToolCall(step, stepIndex)
            )),
        }))
        .sort((left, right) => left.seq - right.seq);
}

function normalizeSummaryExecutionTrace(summary) {
    const toolSteps = (Array.isArray(summary?.tool_steps) ? summary.tool_steps : [])
        .map((step, stepIndex) => normalizeSummaryTraceToolCall(step, stepIndex));
    return {
        tool_steps: toolSteps,
        turns: normalizeTraceTurns(summary?.turns),
    };
}

function firstNonEmptyTraceText(...values) {
    return values
        .map((value) => String(value ?? ""))
        .find((value) => value.trim())
        || "";
}

function buildNodeExecutionTrace(node, detail, liveFrame = null) {
    const fullTrace = detail?.execution_trace && typeof detail.execution_trace === "object" ? detail.execution_trace : {};
    const summaryTrace = normalizeSummaryExecutionTrace(
        detail?.execution_trace_summary && typeof detail.execution_trace_summary === "object"
            ? detail.execution_trace_summary
            : {},
    );
    const source = Object.keys(fullTrace).length ? fullTrace : summaryTrace;
    const normalizedNodeKind = String(source.node_kind ?? detail?.node_kind ?? node?.node_kind ?? "").trim().toLowerCase();
    const normalizedStatus = String(
        source.status
        ?? detail?.status
        ?? node?.status
        ?? node?.state
        ?? ""
    ).trim().toLowerCase();
    const toolSteps = Array.isArray(source.tool_steps) ? source.tool_steps : [];
    const initialPrompt = firstNonEmptyTraceText(
        source.initial_prompt,
        detail?.prompt,
        detail?.goal,
        node?.prompt,
        node?.goal,
        node?.input,
    );
    const finalOutput = firstNonEmptyTraceText(
        source.final_output,
        detail?.final_output,
        detail?.final_output_preview,
        node?.final_output,
        normalizedStatus === "failed" ? detail?.failure_reason : "",
        normalizedStatus === "failed" ? node?.failure_reason : "",
    );
    const acceptanceResult = firstNonEmptyTraceText(
        source.acceptance_result,
        detail?.check_result,
        node?.check_result,
    );
    return {
        initial_prompt: initialPrompt,
        turns: normalizeTraceTurns(source.turns ?? summaryTrace.turns),
        tool_steps: toolSteps.map((step) => ({
            tool_call_id: String(step?.tool_call_id || ""),
            tool_name: String(step?.tool_name || "tool"),
            arguments_text: String(step?.arguments_text || step?.arguments_preview || ""),
            output_text: String(step?.output_text || step?.output_preview || step?.text || ""),
            output_ref: String(step?.output_ref || ""),
            started_at: String(step?.started_at || ""),
            finished_at: String(step?.finished_at || ""),
            elapsed_seconds: Number.isFinite(Number(step?.elapsed_seconds)) ? Number(step.elapsed_seconds) : null,
            status: ["running", "success", "error", "warning", "interrupted"].includes(String(step?.status || ""))
                ? String(step.status)
                : "info",
            recovery_decision: String(step?.recovery_decision || ""),
            related_tool_call_ids: Array.isArray(step?.related_tool_call_ids) ? step.related_tool_call_ids.map((item) => String(item || "")) : [],
            attempted_tools: Array.isArray(step?.attempted_tools) ? step.attempted_tools.map((item) => String(item || "")) : [],
            evidence: Array.isArray(step?.evidence) ? step.evidence.filter((item) => item && typeof item === "object") : [],
            lost_result_summary: String(step?.lost_result_summary || ""),
            contract_delivery: String(step?.contract_delivery || ""),
        })),
        live_tool_calls: normalizeLiveToolCalls(liveFrame),
        live_child_pipelines: normalizeLiveChildPipelines(liveFrame),
        final_output: finalOutput,
        acceptance_result: acceptanceResult || (normalizedNodeKind === "acceptance" ? finalOutput : ""),
    };
}

function roundTraceStatus(round) {
    const tools = Array.isArray(round?.tools) ? round.tools : [];
    if (!tools.length) return "info";
    if (tools.some((item) => ["warning", "interrupted"].includes(String(item?.status || "")))) {
        return "warning";
    }
    if (tools.some((item) => String(item?.status || "") === "running" || String(item?.status || "") === "queued")) {
        return "running";
    }
    return "success";
}

function nodeFinalTraceStatus(node) {
    if (String(node?.state || "") === "in_progress") return "running";
    if (String(node?.state || "") === "failed") return "error";
    return "success";
}
