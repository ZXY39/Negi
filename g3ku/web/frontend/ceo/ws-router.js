"use strict";

// CEO websocket 的 type -> handler 表。连接生命周期（open/close/重连、token 陈旧守卫）
// 留在 org_graph_app.js 的 initCeoWs；这里只处理已解析帧的分发与各类型守卫。
// 本文件必须在 org_graph_app.js 之前加载。

function handleCeoSnapshotFrame(payload, effectiveSessionId) {
    clearCeoReplyDeltaBuffer(effectiveSessionId);
    S.ceoWsLastErrorCode = "";
    // 整帧快照解析成功 = 这条车道又干净了，重连预算重新充满。
    S.ceoWsParseResyncs = 0;
    const snapshotEntry = {
        session_id: effectiveSessionId,
        messages: payload.data?.messages || [],
        inflight_turn: payload.data?.inflight_turn || null,
        preserved_turn: payload.data?.preserved_turn || null,
        compression: payload.data?.compression || null,
    };
    if (effectiveSessionId) {
        setCeoSessionSnapshotCache(effectiveSessionId, snapshotEntry);
    }
    // 快照自己也带会话级 compression：与 ceo.state 谁先到都能认领，恢复不依赖消息顺序。
    adoptCeoContextCompression(effectiveSessionId, payload.data?.compression);
    if (effectiveSessionId === activeSessionId()) {
        renderCeoSnapshot(
            payload.data?.messages || [],
            payload.data?.inflight_turn || null,
            { sessionId: effectiveSessionId, preservedTurn: payload.data?.preserved_turn || null }
        );
    }
    S.ceoSessionBusy = false;
    renderCeoSessions();
    syncCeoSessionActions();
    syncCeoPrimaryButton();
    if (effectiveSessionId === activeSessionId()) {
        syncCeoApprovalFromSnapshotEntry(effectiveSessionId, snapshotEntry, {
            authoritative: true,
            refreshServer: true,
        });
    }
}

function handleCeoWsErrorFrame(payload) {
    const code = ApiClient.getErrorCode(payload.data || {});
    const message = ApiClient.friendlyErrorMessage(payload.data || {}, payload.data?.message || "连接失败");
    if (code === "ceo_approval_pending") {
        showToast({
            title: "等待审批",
            text: message || "当前存在待审批工具调用，请先完成审批。",
            kind: "warn",
            durationMs: 2600,
        });
        if (typeof refreshCeoApprovalFromServer === "function") {
            void refreshCeoApprovalFromServer(activeSessionId(), { quiet: true });
        }
        return;
    }
    if (code && code !== S.ceoWsLastErrorCode) {
        S.ceoWsLastErrorCode = code;
        showToast({
            title: code === "no_model_configured" ? "未配置模型" : "连接失败",
            text: message,
            kind: code === "no_model_configured" ? "warn" : "error",
            durationMs: 5200,
        });
    }
}

function handleCeoTurnInterruptFrame(payload, effectiveSessionId) {
    if (typeof syncCeoApprovalFromInterrupts === "function") {
        syncCeoApprovalFromInterrupts(payload.data?.interrupts || [], effectiveSessionId, { authoritative: true });
    }
}

function handleCeoTurnPatchFrame(payload, effectiveSessionId) {
    clearCeoReplyDeltaBuffer(effectiveSessionId, {
        turnId: payload.data?.inflight_turn?.turn_id || payload.data?.preserved_turn?.turn_id || "",
    });
    // 会话级压缩载体每帧覆盖一次，缺键就当成"没有在进行中"：缓存是合并语义，
    // 不显式覆盖的话，压缩结束后的那一帧不会把上一帧的 running 摘掉，线会留在屏上。
    if (effectiveSessionId) {
        const patchCompression = normalizeCeoSnapshotCompression(payload.data?.compression);
        const compressionForCache = String(patchCompression?.status || "").trim().toLowerCase() === "running"
            ? { ...patchCompression, seen_at: Date.now() }
            : null;
        setCeoSessionSnapshotCache(effectiveSessionId, { compression: compressionForCache });
    }
    const patchEntry = {
        session_id: effectiveSessionId,
        inflight_turn: payload.data?.inflight_turn || null,
        preserved_turn: payload.data?.preserved_turn || null,
    };
    if (effectiveSessionId === activeSessionId()) {
        patchCeoInflightTurn(payload.data?.preserved_turn || null, {
            sessionId: effectiveSessionId,
            cacheField: "preserved_turn",
        });
        patchCeoInflightTurn(payload.data?.inflight_turn || null, {
            sessionId: effectiveSessionId,
            cacheField: "inflight_turn",
        });
    } else if (effectiveSessionId) {
        setCeoSessionSnapshotCache(effectiveSessionId, patchEntry);
    }
    if (effectiveSessionId === activeSessionId()) {
        const patchInterrupts = ceoApprovalInterruptsFromSnapshotEntry(patchEntry);
        if (patchInterrupts.length) {
            syncCeoApprovalFromSnapshotEntry(effectiveSessionId, patchEntry, {
                authoritative: true,
                refreshServer: false,
            });
        }
    }
}

function handleCeoReplyFinalFrame(payload, effectiveSessionId) {
    clearCeoReplyDeltaBuffer(effectiveSessionId, { turnId: payload.data?.turn_id || "" });
    finalizeCeoTurn(payload.data?.text || "", payload.data || {});
}

function handleCeoReplyDeltaFrame(payload, effectiveSessionId) {
    queueCeoReplyDelta(payload.data || {}, { sessionId: effectiveSessionId });
}

function handleCeoReasoningDeltaFrame(payload, effectiveSessionId) {
    applyCeoReasoningDelta(payload.data || {}, { sessionId: effectiveSessionId });
}

function handleCeoAgentToolFrame(payload) {
    appendCeoToolEvent(payload.data || {});
}

function handleCeoErrorFrame(payload) {
    handleCeoError(payload.data || {});
}

function handleCeoInternalAckFrame(payload) {
    handleCeoInternalAck(payload.data || {});
}

function handleCeoSkillRosterNoticeFrame(payload) {
    handleCeoSkillRosterNotice(payload.data || {});
}

function handleCeoEditForkGatesFrame(payload, effectiveSessionId) {
    applyCeoEditForkGates(payload.data || {}, effectiveSessionId);
}

function handleCeoControlAckFrame(payload) {
    handleCeoControlAck(payload.data || {});
}

function handleCeoStateFrame(payload) {
    applyCeoState(payload.data?.state || {}, payload.data || {});
}

function handleCeoCompressionMarkerFrame(payload, effectiveSessionId) {
    applyCeoCompressionMarker(effectiveSessionId, payload.data || {});
}

function handleCeoTurnDiscardFrame(payload) {
    discardActiveCeoTurn({ source: payload.data?.source || "", turnId: payload.data?.turn_id || "" });
}

function handleCeoSessionsSnapshotFrame(payload) {
    applyCeoSessionsPayload(payload.data || {});
}

function handleCeoSessionsPatchFrame(payload) {
    applyCeoSessionPatch(payload.data || {});
}

function handleTaskArtifactAppliedFrame() {
    void loadTaskArtifacts();
}

// 守卫留在条目内部：上提到 dispatcher 会把"后台会话也更新"的行为一起改掉。
const CEO_WS_HANDLERS = {
    "snapshot.ceo": handleCeoSnapshotFrame,
    error: handleCeoWsErrorFrame,
    "ceo.state": handleCeoStateFrame,
    "ceo.control_ack": handleCeoControlAckFrame,
    "ceo.turn.interrupt": (payload, effectiveSessionId) => {
        if (effectiveSessionId === activeSessionId()) handleCeoTurnInterruptFrame(payload, effectiveSessionId);
    },
    "ceo.compression.marker": handleCeoCompressionMarkerFrame,
    "ceo.turn.patch": handleCeoTurnPatchFrame,
    "ceo.edit_fork.gates": (payload, effectiveSessionId) => {
        if (effectiveSessionId === activeSessionId()) handleCeoEditForkGatesFrame(payload, effectiveSessionId);
    },
    "ceo.agent.tool": (payload, effectiveSessionId) => {
        if (effectiveSessionId === activeSessionId()) handleCeoAgentToolFrame(payload);
    },
    "ceo.error": (payload, effectiveSessionId) => {
        if (effectiveSessionId === activeSessionId()) handleCeoErrorFrame(payload);
    },
    "ceo.internal.ack": (payload, effectiveSessionId) => {
        if (effectiveSessionId === activeSessionId()) handleCeoInternalAckFrame(payload);
    },
    "ceo.skill.roster_notice": (payload, effectiveSessionId) => {
        if (effectiveSessionId === activeSessionId()) handleCeoSkillRosterNoticeFrame(payload);
    },
    "ceo.reasoning.delta": handleCeoReasoningDeltaFrame,
    "ceo.reply.delta": handleCeoReplyDeltaFrame,
    "ceo.reply.final": (payload, effectiveSessionId) => {
        if (effectiveSessionId === activeSessionId()) handleCeoReplyFinalFrame(payload, effectiveSessionId);
    },
    "ceo.turn.discard": (payload, effectiveSessionId) => {
        if (effectiveSessionId === activeSessionId()) handleCeoTurnDiscardFrame(payload);
    },
    "ceo.sessions.snapshot": handleCeoSessionsSnapshotFrame,
    "ceo.sessions.patch": handleCeoSessionsPatchFrame,
    "task.artifact.applied": (payload) => {
        if (payload.data?.task_id === S.currentTaskId) handleTaskArtifactAppliedFrame(payload);
    },
};

function dispatchCeoWsFrame(payload, effectiveSessionId) {
    const handler = CEO_WS_HANDLERS[payload?.type];
    if (handler) handler(payload, effectiveSessionId);
}
