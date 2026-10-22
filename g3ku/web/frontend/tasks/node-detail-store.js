// 节点详情状态层：执行轨迹摘要归一化、阶段/轮次状态与 live 帧投影。
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

function normalizeTraceTurns(turns) {
    return (Array.isArray(turns) ? turns : [])
        .map((turn, turnIndex) => ({
            seq: normalizeInt(turn?.seq, turnIndex + 1),
            text: String(turn?.text || ""),
            text_ref: String(turn?.text_ref || ""),
            created_at: String(turn?.created_at || ""),
            status: String(turn?.status || ""),
            tools: (Array.isArray(turn?.tools) ? turn.tools : []).map((step, stepIndex) => (
                normalizeSummaryTraceToolCall(step, stepIndex)
            )),
        }))
        .sort((left, right) => left.seq - right.seq);
}

function normalizeSummaryExecutionTrace(summary) {
    const toInt = (value, fallback = 0) => {
        const parsed = Number.parseInt(String(value ?? ""), 10);
        return Number.isFinite(parsed) ? parsed : fallback;
    };
    const stages = (Array.isArray(summary?.stages) ? summary.stages : []).map((stage, stageIndex) => {
        const fallbackTools = (Array.isArray(stage?.tool_calls) ? stage.tool_calls : []).map((step, toolIndex) => (
            normalizeSummaryTraceToolCall(step, toolIndex)
        ));
        const rounds = (Array.isArray(stage?.rounds) ? stage.rounds : []).map((round, roundIndex) => {
            let tools = (Array.isArray(round?.tools) ? round.tools : []).map((step, toolIndex) => (
                normalizeSummaryTraceToolCall(step, toolIndex)
            ));
            if (!tools.length) {
                const roundToolNames = Array.isArray(round?.tool_names) ? round.tool_names : [];
                const roundToolCallIds = Array.isArray(round?.tool_call_ids) ? round.tool_call_ids : [];
                tools = roundToolNames.map((toolName, toolIndex) => normalizeSummaryTraceToolCall({
                    tool_name: toolName,
                    tool_call_id: roundToolCallIds[toolIndex] || "",
                    status: round?.status || stage?.status || "",
                }, toolIndex));
            }
            return {
                round_id: String(round?.round_id || ""),
                round_index: toInt(round?.round_index, roundIndex + 1),
                created_at: String(round?.created_at || ""),
                text: String(round?.text || "").trim(),
                budget_counted: !!round?.budget_counted,
                tools,
            };
        }).filter((round) => Array.isArray(round?.tools) && round.tools.length);
        return {
            stage_id: String(stage?.stage_id || `summary-stage-${stageIndex + 1}`),
            stage_index: toInt(stage?.stage_index, stageIndex + 1),
            mode: String(stage?.mode || "执行摘要").trim() || "执行摘要",
            status: String(stage?.status || (String(stage?.finished_at || "").trim() ? "完成" : "进行中")).trim() || "进行中",
            // 裁撤标记必须穿过这份白名单：阶段卡的状态徽章靠它区分"完成"与"已移出上下文"，
            // 漏在这里就等于前端永远看不到模型点名移出这件事。
            context_evicted: stage?.context_evicted === true,
            // 取档指针同一条教训：轨道按字段重建阶段对象，漏掉它卡片就退回"暂无工具轮次"，
            // 而后端明明给了"原文还能取回来"的凭据。
            rounds_archive_ref: String(stage?.rounds_archive_ref || "").trim(),
            stage_goal: String(stage?.stage_goal || "").trim(),
            preamble_text: String(stage?.preamble_text || "").trim(),
            completed_stage_summary: String(stage?.completed_stage_summary || "").trim(),
            stage_total_steps: toInt(stage?.tool_round_budget, 0),
            tool_rounds_used: toInt(stage?.tool_rounds_used, rounds.length || (fallbackTools.length ? 1 : 0)),
            created_at: String(stage?.created_at || ""),
            finished_at: String(stage?.finished_at || ""),
            rounds: rounds.length ? rounds : (fallbackTools.length ? [{
                round_id: "",
                round_index: 1,
                created_at: "",
                budget_counted: false,
                tools: fallbackTools,
            }] : []),
        };
    });
    const toolSteps = stages.flatMap((stage) => (
        Array.isArray(stage?.rounds)
            ? stage.rounds.flatMap((round) => (Array.isArray(round?.tools) ? round.tools : []))
            : []
    ));
    return {
        tool_steps: toolSteps,
        turns: normalizeTraceTurns(summary?.turns),
        stages,
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
    const stages = (Array.isArray(source.stages) ? source.stages : []).map((stage, index) => normalizeExecutionStageTrace(stage, index));
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
        stages,
        live_tool_calls: normalizeLiveToolCalls(liveFrame),
        live_child_pipelines: normalizeLiveChildPipelines(liveFrame),
        final_output: finalOutput,
        acceptance_result: acceptanceResult || (normalizedNodeKind === "acceptance" ? finalOutput : ""),
    };
}

function toolArgumentsFullText(step) {
    const args = step?.arguments;
    if (!args || typeof args !== "object" || Array.isArray(args)) return "";
    if (!Object.keys(args).length) return "";
    // 归档里存的是未裁原文，一格能带到几 MB；显示面按服务端那一屏封顶，复制仍走全量。
    const text = JSON.stringify(args, null, 2);
    return typeof capTraceDisplayText === "function" ? capTraceDisplayText(text) : text;
}

function normalizeExecutionStageTrace(stage, index = 0) {
    const rounds = Array.isArray(stage?.rounds) ? stage.rounds : [];
    return {
        stage_id: String(stage?.stage_id || ""),
        stage_index: normalizeInt(stage?.stage_index, index + 1),
        mode: String(stage?.mode || "自主执行").trim() || "自主执行",
        status: String(stage?.status || "进行中").trim() || "进行中",
        // 这份白名单决定了阶段卡能看到哪些字段：漏掉裁撤标记，前端就只能显示"完成"，
        // 而模型点名移出上下文这件事恰恰只有这个标记能表达。
        context_evicted: stage?.context_evicted === true,
        // 取档指针同一条教训：CEO 轨道先过这份重建再进渲染器，漏一个字段就等于
        // 后端那句"原文还能取回来"从来没说过。
        rounds_archive_ref: String(stage?.rounds_archive_ref || "").trim(),
        stage_goal: String(stage?.stage_goal || "").trim(),
        preamble_text: String(stage?.preamble_text || "").trim(),
        completed_stage_summary: String(stage?.completed_stage_summary || "").trim(),
        stage_total_steps: normalizeInt(stage?.tool_round_budget ?? stage?.stage_total_steps, 0),
        tool_rounds_used: normalizeInt(stage?.tool_rounds_used, 0),
        created_at: String(stage?.created_at || ""),
        finished_at: String(stage?.finished_at || ""),
        rounds: rounds.map((round, roundIndex) => ({
            round_id: String(round?.round_id || ""),
            round_index: normalizeInt(round?.round_index, roundIndex + 1),
            created_at: String(round?.created_at || ""),
            text: String(round?.text || "").trim(),
            budget_counted: !!round?.budget_counted,
            tools: (Array.isArray(round?.tools) ? round.tools : []).map((step) => ({
                tool_call_id: String(step?.tool_call_id || ""),
                tool_name: String(step?.tool_name || "tool"),
                arguments_text: String(step?.arguments_text || step?.arguments_preview || ""),
                arguments_full: toolArgumentsFullText(step),
                // 投影把超长入参清空时落这颗标记：面板据此去账本按需回取全量。
                arguments_truncated: step?.arguments_truncated === true,
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
        })).filter((round) => Array.isArray(round?.tools) && round.tools.length),
    };
}

function stageTraceStatus(stage) {
    return ({
        "进行中": "running",
        "in_progress": "running",
        "running": "running",
        "active": "running",
        "完成": "success",
        "success": "success",
        "completed": "success",
        "失败": "error",
        "failed": "error",
        "error": "error",
    }[String(stage?.status || "").trim()] || "info");
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

function stageTraceStatusLabel(stage) {
    if (stageIsContextEvicted(stage)) return STAGE_EVICTED_LABEL;
    return displayTaskStageStatus(stage?.status);
}
