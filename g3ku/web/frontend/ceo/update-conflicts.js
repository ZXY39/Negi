"use strict";

// 「这次更新没能保住的本地改动」这一行：只报数量，给一颗把改动交给 Negi 的按钮。
//
// 数据来自更新台账的 conflict_* 字段（`g3ku/update_local_edits.py` 写批次、
// `g3ku/update_apply.py` 并进台账）。这一行不印路径：设备上一次撞行动辄几十个文件，
// 把原始路径铺在设置面板里既读不动也没有下一步动作，清单只在按钮的 title 与批次目录里。
//
// 两条不变量：
// - 换码在执行体里已经完成，按钮不许再触发任何换码/重启动作，那会把当前服务连同新建的
//   会话一起切断；
// - **按钮不自动发送**。`/ws/ceo` 是全页共用的一条连接，回合归服务端"当前激活的会话"所有，
//   帧里带的 `session_id` 不参与路由 —— 实测过一次：新建的本地会话拿到了会话号，消息却落进
//   一条真实对外的渠道会话，模型在那儿把这条指令当真执行了。所以现在只把提示词写成那条
//   新会话的输入框草稿（`setCeoComposerDraft` + `restoreCeoComposerDraftForSession`，
//   本来就按会话存、可跨刷新），发送这一步留给人。
// - 一批只准备一次：幂等键是台账里的 `conflict_at`，重复点击不堆第二份会话。

const UPDATE_CONFLICT_HANDOFF_KEY = "g3ku.update-conflicts.handoff.v1";

function updateConflictSummary(apply) {
    const paths = (Array.isArray(apply?.conflict_paths) ? apply.conflict_paths : [])
        .map((item) => String(item || "").trim())
        .filter(Boolean);
    if (!paths.length) return null;
    const conflictAt = String(apply?.conflict_at || "").trim();
    if (!conflictAt) return null;
    return {
        conflictAt,
        paths,
        count: Number(apply?.conflict_count || paths.length) || paths.length,
        lane: String(apply?.conflict_lane || "").trim(),
        stashLabel: String(apply?.conflict_stash || "").trim(),
        batchDir: String(apply?.conflict_dir || "").trim(),
        ref: String(apply?.ref || "").trim(),
    };
}

function updateConflictHeadline(summary) {
    // 只报数量。车道不同，话说的是不同的事：git 车道是撞在同一行，拷贝车道是被覆盖。
    return summary.lane === "copy-overwrite"
        ? `这次更新覆盖了 ${summary.count} 处本地改动。`
        : `这次更新有 ${summary.count} 处本地改动没能自动合上。`;
}

function updateConflictTooltip(summary) {
    const list = summary.paths.slice(0, 20).join("、");
    const rest = summary.paths.length > 20 ? ` 等 ${summary.paths.length} 个文件` : "";
    const originals = summary.stashLabel
        ? `原件在 git stash（${summary.stashLabel}）与副本目录里`
        : "原件在副本目录里";
    return `${list}${rest}；${originals}：${summary.batchDir || "数据根的 update-conflicts 目录"}`;
}

function readUpdateConflictHandoff() {
    try {
        const raw = localStorage.getItem(UPDATE_CONFLICT_HANDOFF_KEY);
        const parsed = raw ? JSON.parse(raw) : null;
        return Array.isArray(parsed) ? parsed : [];
    } catch {
        return [];
    }
}

function isUpdateConflictHandedOff(summary) {
    if (!summary) return false;
    return readUpdateConflictHandoff().includes(summary.conflictAt);
}

function markUpdateConflictHandedOff(summary) {
    const known = readUpdateConflictHandoff();
    if (known.includes(summary.conflictAt)) return;
    known.push(summary.conflictAt);
    try {
        localStorage.setItem(UPDATE_CONFLICT_HANDOFF_KEY, JSON.stringify(known.slice(-8)));
    } catch {
        // 记不住就允许重投一次；多投一条提示词的代价比拦掉这个动作小。
    }
}

function updateConflictSessionTitle(summary) {
    return `合并本地改动 · ${summary.ref || summary.conflictAt}`;
}

function updateConflictPrompt(summary) {
    return [
        `这台设备刚做完一次版本更新${summary.ref ? `（${summary.ref}）` : ""}：代码已经在新版本上，但你之前改过的一部分文件没能自动合回来。`,
        "",
        "请把这些本地改动合到当前新版码上：",
        "1. 先读下面的冲突文件清单和那份副本，再逐个文件对照当前内容做合并；判断依据是「用户想达到的效果」，不是文本行号。",
        "2. 只允许修改清单里列出的文件。不要改全局配置、模型链、任务默认值。",
        "3. 不要执行任何换码、更新或重启动作：代码已经是新版，再点一次更新会把这个服务连同你自己这条会话一起切断。",
        "4. 不要删除 stash，也不要删那份副本 —— 那是原始凭据，你合坏了还要从它重来。",
        "5. 合完跑一遍与这些文件相关的测试（点名单个文件，禁止跑全量），然后逐个文件说明合成了什么、为什么那处不算冲突。",
        "6. 最后提醒用户：改动已合上，重启一次服务就生效。",
        "",
        `冲突文件：\n${summary.paths.map((item) => `- ${item}`).join("\n")}`,
        `副本目录：${summary.batchDir || "（未提供）"}`,
        summary.stashLabel ? `stash 标签：${summary.stashLabel}` : "",
    ].filter((line) => line !== "").join("\n");
}

function renderUpdateConflictBlock(item) {
    const block = U.updateConflictBlock;
    if (!block) return;
    const summary = updateConflictSummary(item?.last_apply);
    if (!summary) {
        block.hidden = true;
        block.classList.toggle("is-handed-off", false);
        return;
    }
    block.hidden = false;
    if (U.updateConflictHeadline) U.updateConflictHeadline.textContent = updateConflictHeadline(summary);
    const handedOff = isUpdateConflictHandedOff(summary);
    block.classList.toggle("is-handed-off", handedOff);
    if (U.updateConflictNegiBtn) {
        U.updateConflictNegiBtn.disabled = handedOff;
        U.updateConflictNegiBtn.textContent = handedOff ? "已备好，进会话按发送" : "交给 Negi 解决";
        U.updateConflictNegiBtn.title = updateConflictTooltip(summary);
        U.updateConflictNegiBtn.dataset.conflictAt = summary.conflictAt;
    }
}

function stageUpdateConflictPrompt(sessionId, text) {
    // 只把提示词存成那条会话自己的草稿。**不在这里碰可见输入框**：此刻页面上激活的可能
    // 还是别的会话，把它的草稿刷进去等于再造一次跨会话投递。刷输入框留到激活之后那一步。
    return Boolean(setCeoComposerDraft(sessionId, { text, uploads: [] }));
}

async function handUpdateConflictsToNegi() {
    const summary = updateConflictSummary(S.updateStatus?.last_apply);
    if (!summary) return;
    if (isUpdateConflictHandedOff(summary)) return;
    if (U.updateConflictNegiBtn) U.updateConflictNegiBtn.disabled = true;
    let created = null;
    try {
        created = await ApiClient.createCeoSession({ title: updateConflictSessionTitle(summary) });
    } catch (error) {
        if (U.updateConflictNegiBtn) U.updateConflictNegiBtn.disabled = false;
        showToast({
            title: "没能准备给 Negi",
            text: String(error?.message || error || "新建会话失败"),
            kind: "error",
        });
        return;
    }
    const sessionId = String(created?.item?.session_id || "").trim();
    if (!sessionId) {
        if (U.updateConflictNegiBtn) U.updateConflictNegiBtn.disabled = false;
        showToast({ title: "没能准备给 Negi", text: "新建会话没返回会话号，可以再点一次。", kind: "warn" });
        return;
    }
    if (!stageUpdateConflictPrompt(sessionId, updateConflictPrompt(summary))) {
        if (U.updateConflictNegiBtn) U.updateConflictNegiBtn.disabled = false;
        showToast({ title: "没能准备给 Negi", text: "提示词没写进那条会话的输入框，可以再点一次。", kind: "warn" });
        return;
    }
    if (typeof activateCeoSession === "function") {
        await activateCeoSession(sessionId);
        if (typeof restoreCeoComposerDraftForSession === "function") {
            restoreCeoComposerDraftForSession(sessionId);
        }
    }
    markUpdateConflictHandedOff(summary);
    renderUpdateConflictBlock(S.updateStatus);
    closeProjectSettingsDialog?.();
    showToast({
        title: "提示词已备好",
        text: "已填进新建的那条会话，按发送才交给 Negi；合完后重启一次服务生效。",
        kind: "info",
    });
}
