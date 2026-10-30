"use strict";

// CEO 会话流的纯选择器：消息 key、渲染签名、回合 id/usage 归一、思考读数归一。
// 不碰 DOM、不发请求；本文件必须在 org_graph_app.js 之前加载。

function ceoAssistantTurnAlreadyPersisted(turnId = "", { messages = null, sessionId = "" } = {}) {
    const normalizedTurnId = normalizeCeoTurnId(turnId);
    if (!normalizedTurnId) return false;
    const sourceMessages = Array.isArray(messages)
        ? messages
        : (getCeoSessionSnapshotCache(sessionId || activeSessionId())?.messages || []);
    return trimCeoSessionSnapshotMessages(sourceMessages).some((item) => (
        String(item?.role || "").trim().toLowerCase() === "assistant"
        && normalizeCeoTurnId(item?.turn_id || "") === normalizedTurnId
    ));
}

function findCeoSnapshotMessageByTurnId(sessionId, turnId) {
    const key = String(sessionId || "").trim();
    const target = String(turnId || "").trim();
    if (!key || !target) return null;
    const entry = getCeoSessionSnapshotCache(key);
    const messages = Array.isArray(entry?.messages) ? entry.messages : [];
    for (let index = messages.length - 1; index >= 0; index -= 1) {
        const item = messages[index];
        if (!item || typeof item !== "object") continue;
        if (String(item?.role || "").trim().toLowerCase() !== "user") continue;
        if (String(item?.turn_id || "").trim() === target) return item;
    }
    return null;
}

function ceoMessageTimestampMs(value = "") {
    const text = String(value || "").trim();
    if (!text) return NaN;
    return Date.parse(text);
}

function normalizeCeoSnapshotReasoning(raw = null) {
    // 进行中快照里的那份有界读数（重连时恢复这一行用）；全文不在其中，也不该在。
    if (!raw || typeof raw !== "object") return null;
    const seconds = Number(raw.seconds || 0);
    const chars = Number(raw.chars || 0);
    const tailLine = String(raw.tail_line || "");
    if (!seconds && !chars && !tailLine.trim()) return null;
    return {
        attempt: Number(raw.attempt || 0),
        seconds,
        chars,
        est_tokens: Number(raw.est_tokens || 0),
        tail_line: tailLine,
    };
}

function buildCeoMessageKeyList(messages = []) {
    // 与 renderCeoSnapshot 的消息打 key 规则完全一致:m:{turn_id|-}:{role}:{出现次序}。
    const counters = {};
    return (Array.isArray(messages) ? messages : []).map((item) => {
        if (!item || typeof item !== "object") return "";
        const role = String(item.role || "").trim().toLowerCase();
        const base = String(item.turn_id || "-").trim() || "-";
        const counterKey = `${base}:${role}`;
        const occ = Number(counters[counterKey] || 0);
        counters[counterKey] = occ + 1;
        return `m:${base}:${role}:${occ}`;
    });
}

function buildCeoRenderSignature(messages = [], inflightTurn = null, preservedTurn = null) {
    // 快照/重建的内容签名:相同签名 = 同一渲染输入,重建可以安全跳过。
    // 签名覆盖完整渲染面(messages 内容 + live turn 关键字段),漏字段会导致陈旧渲染。
    const projectMessage = (item) => {
        if (!item || typeof item !== "object") return null;
        return [
            String(item.role || "").trim().toLowerCase(),
            String(item.turn_id || "").trim(),
            String(item.status || "").trim().toLowerCase(),
            String(item.content || ""),
            projectTrace(item.turns),
            // usage/timestamp 参与签名:缓存先行渲染(可能缺元数据)后到达的
            // 服务端权威快照必须触发重建,否则元信息永远停留在缺失状态。
            item.usage && typeof item.usage === "object" ? JSON.stringify(item.usage) : "",
            String(item.timestamp || ""),
            // 渠道会话的来源标签参与签名：缓存先行渲染(无标签)后到达的权威快照
            // 必须触发重建，否则气泡上的 `第三方 0002` 永远不出现。
            String(item.sender_label || ""),
            String(item.sender_id || ""),
            // 编辑/Fork 按钮标志参与签名:flag 迟到(缓存渲染无 flag、权威快照
            // 有 flag,或任务派发后 flag 收回)必须触发重建。
            item.can_edit_fork === true ? 1 : 0,
            item.can_fork_reply === true ? 1 : 0,
            item.task_dispatched === true ? 1 : 0,
        ];
    };
    const projectTrace = (turns) => {
        // 回合轨道参与签名:同一回合的 turns 在 assistant_text/usage 未变时也会长出
        // 新的工具组。签名漏掉它就会让切回会话时的缓存先行渲染占住签名,把携带新
        // turns 的权威快照整份跳过。只投影会改像素的骨架字段(seq/状态/正文长度/
        // 工具名与状态/输出长度),不搬输出正文。
        const entries = Array.isArray(turns) ? turns : [];
        if (!entries.length) return null;
        return entries.map((turn) => [
            String(turn?.seq ?? ""),
            String(turn?.status || "").trim().toLowerCase(),
            String(turn?.text || "").length,
            (Array.isArray(turn?.tools) ? turn.tools : []).map((step) => [
                String(step?.tool_name || "").trim(),
                String(step?.status || "").trim().toLowerCase(),
                String(step?.output_text || "").length,
            ]),
        ]);
    };
    const projectTurn = (snapshot) => {
        if (!snapshot || typeof snapshot !== "object") return null;
        const retryStatus = snapshot.model_retry_status && typeof snapshot.model_retry_status === "object"
            ? snapshot.model_retry_status
            : null;
        return [
            String(snapshot.source || "").trim().toLowerCase(),
            String(snapshot.turn_id || "").trim(),
            String(snapshot.status || "").trim().toLowerCase(),
            String(snapshot.assistant_text || "").slice(0, 20000),
            retryStatus ? Number(retryStatus.retry_count || 0) : -1,
            retryStatus ? String(retryStatus.state || "") : "",
            snapshot.usage && typeof snapshot.usage === "object" ? JSON.stringify(snapshot.usage) : "",
            projectTrace(snapshot.turns),
        ];
    };
    try {
        return JSON.stringify({
            messages: (Array.isArray(messages) ? messages : []).map(projectMessage),
            inflight: projectTurn(inflightTurn),
            preserved: projectTurn(preservedTurn),
        });
    } catch (error) {
        void error;
        return "";
    }
}

function normalizeCeoTurnSource(source = "") {
    const normalized = String(source || "").trim().toLowerCase();
    return normalized || "user";
}

function normalizeCeoTurnId(turnId = "") {
    return String(turnId || "").trim();
}

function normalizeCeoTurnUsage(usage = null) {
    if (!usage || typeof usage !== "object") return null;
    const input = Number(usage?.input_tokens ?? usage?.inputTokens ?? 0) || 0;
    const output = Number(usage?.output_tokens ?? usage?.outputTokens ?? 0) || 0;
    const cache = Number(usage?.cache_hit_tokens ?? usage?.cacheHitTokens ?? 0) || 0;
    const calls = Number(usage?.call_count ?? usage?.callCount ?? 0) || 0;
    if (!input && !output && !cache) return null;
    return { input_tokens: input, output_tokens: output, cache_hit_tokens: cache, call_count: calls };
}
