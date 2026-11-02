"use strict";

// 「这次更新没能保住的本地改动」这一行：列文件、给一颗把冲突交给 Negi 的按钮。
//
// 数据来自更新台账的 conflict_* 字段（`g3ku/update_local_edits.py` 写批次、
// `g3ku/update_apply.py` 并进台账）。这里只读、只列、只投递提示词：
// - 换码在执行体里已经完成，按钮不许再触发任何换码/重启动作，那会把当前服务连同
//   新建的会话一起切断；
// - 投递走 composer 那条现成的发送主路径（先落按会话存好的待发送耐久行，再尝试立即投），
//   后端不新增"没有连接也要起一轮"的入口；
// - 一批冲突只投一次：幂等键是台账里的 `conflict_at`，重复点击不堆第二份提示词。

const UPDATE_CONFLICT_HANDOFF_KEY = "g3ku.update-conflicts.handoff.v1";
const UPDATE_CONFLICT_PATH_LIMIT = 20;

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
    if (summary.lane === "copy-overwrite") {
        return `这次更新覆盖了 ${summary.count} 个你改过的文件。`;
    }
    return `这次更新有 ${summary.count} 个文件和新版本撞在同一行，没能自动合上。`;
}

function updateConflictRecoveryHint(summary) {
    if (summary.lane === "copy-overwrite") {
        return `原件副本在 ${summary.batchDir || "数据根的 update-conflicts 目录"}。`;
    }
    return `原件仍在 git stash 里${summary.stashLabel ? `（${summary.stashLabel}）` : ""}，副本在 ${summary.batchDir || "数据根的 update-conflicts 目录"}。`;
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

function deliverUpdateConflictPrompt(sessionId, text) {
    // 走 composer 那条现成的发送主路径：条目先落进按会话存好的待发送队列（这一份是浏览器里
    // 的耐久行，连接没就绪也不会丢），再尝试立刻投递。投成功是"已经交给 Negi"，投不成功
    // 条目仍挂在待发送里由既有车道重投 —— 两种都算送出去了，因为用户在输入框上方看得见它。
    const entries = enqueueCeoFollowUp(sessionId, { text, uploads: [] });
    const stored = (Array.isArray(entries) ? entries : []).find(
        (item) => String(item?.text || "") === String(text || "") && !item?.runtime_sent_at,
    );
    if (!stored) return false;
    try {
        flushCeoQueuedFollowUp(String(stored.id || ""));
    } catch {
        // 投递失败不改判：耐久行还在，composer 会在安全边界自己重投。
    }
    return true;
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

function renderUpdateConflictPaths(container, paths) {
    container.textContent = "";
    paths.slice(0, UPDATE_CONFLICT_PATH_LIMIT).forEach((item) => {
        const li = document.createElement("li");
        const code = document.createElement("code");
        code.textContent = item;
        li.appendChild(code);
        container.appendChild(li);
    });
    const rest = paths.length - Math.min(paths.length, UPDATE_CONFLICT_PATH_LIMIT);
    if (rest > 0) {
        const more = document.createElement("li");
        more.className = "update-conflict-more";
        more.textContent = `还有 ${rest} 个`;
        container.appendChild(more);
    }
}

function renderUpdateConflictBlock(item) {
    const block = U.updateConflictBlock;
    if (!block) return;
    const summary = updateConflictSummary(item?.last_apply);
    if (!summary) {
        block.hidden = true;
        block.classList.remove("is-handed-off");
        return;
    }
    block.hidden = false;
    if (U.updateConflictHeadline) U.updateConflictHeadline.textContent = updateConflictHeadline(summary);
    if (U.updateConflictPaths) renderUpdateConflictPaths(U.updateConflictPaths, summary.paths);
    if (U.updateConflictHint) U.updateConflictHint.textContent = updateConflictRecoveryHint(summary);
    const handedOff = isUpdateConflictHandedOff(summary);
    block.classList.toggle("is-handed-off", handedOff);
    if (U.updateConflictNegiBtn) {
        U.updateConflictNegiBtn.disabled = handedOff;
        U.updateConflictNegiBtn.textContent = handedOff ? "已交给 Negi" : "交给 Negi 解决";
        U.updateConflictNegiBtn.dataset.conflictAt = summary.conflictAt;
    }
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
            title: "没能交给 Negi",
            text: String(error?.message || error || "新建会话失败"),
            kind: "error",
        });
        return;
    }
    const sessionId = String(created?.item?.session_id || created?.active_session_id || "").trim();
    if (!sessionId) {
        if (U.updateConflictNegiBtn) U.updateConflictNegiBtn.disabled = false;
        showToast({ title: "没能交给 Negi", text: "新建会话没返回会话号，按钮可以再点一次。", kind: "warn" });
        return;
    }
    if (typeof activateCeoSession === "function") {
        await activateCeoSession(sessionId);
    }
    const text = updateConflictPrompt(summary);
    if (!deliverUpdateConflictPrompt(sessionId, text)) {
        // 没送出去就不留幂等标记：条目要么已进待发送队列（那边会自己重投），要么连接还没
        // 就绪 —— 两种都还能重试，不能让用户以为已经交给了 Negi。
        if (U.updateConflictNegiBtn) U.updateConflictNegiBtn.disabled = false;
        return;
    }
    markUpdateConflictHandedOff(summary);
    renderUpdateConflictBlock(S.updateStatus);
    closeProjectSettingsDialog?.();
    showToast({
        title: "已交给 Negi",
        text: "新建的会话正在处理这些文件；合完后重启一次服务生效。",
        kind: "info",
    });
}
