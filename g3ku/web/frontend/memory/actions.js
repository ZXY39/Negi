// 记忆写路径：失败停车重试/放弃、note 保存、浏览器解压/删除/编辑保存。
// 状态与渲染仍在 memory/store.js 与 org_graph_app.js。

async function runMemoryNoteSave(ref, body) {
    const preview = S.memoryNotePreview;
    if (preview.saving) return;
    preview.saving = true;
    renderMemoryNotePreview();
    try {
        await ApiClient.updateMemoryNote(ref, body, "manual-ui", memoryScopeQuery());
        preview.editMode = false;
        preview.body = body;
        preview.editBody = "";
    } catch (error) {
        showToast({ title: "保存失败", text: error?.message || "note 修改未成功，请稍后重试", kind: "error", durationMs: 5000 });
    } finally {
        preview.saving = false;
        renderMemoryNotePreview();
    }
}

async function runMemoryBrowserUncompress(memoryId) {
    const id = String(memoryId || "").trim();
    if (!id || S.memoryBrowser.actionBusy) return;
    S.memoryBrowser.actionBusy = `uncompress:${id}`;
    renderMemoryBrowserList();
    try {
        const result = await ApiClient.uncompressCurrentMemory(id, "manual-ui", memoryScopeQuery());
        const item = result?.item || {};
        showToast({
            title: item.changed ? "已回暖" : "这条本来就是全文",
            text: item.changed
                ? `这条记忆已恢复全文常驻，当前常驻 ${item.resident_chars_after} 字（触发线 ${item.compress_trigger_chars}）。`
                : "",
            kind: "success",
            durationMs: 4200,
        });
        await loadMemoryBrowser();
    } catch (error) {
        showToast({
            title: "回暖失败",
            text: error?.message || "这条记忆没能放回去，请稍后重试",
            kind: "error",
            durationMs: 5200,
        });
    } finally {
        S.memoryBrowser.actionBusy = "";
        renderMemoryBrowserList();
    }
}

async function runMemoryBrowserDelete(memoryIds) {
    const ids = [...new Set((Array.isArray(memoryIds) ? memoryIds : []).map((id) => String(id || "").trim()).filter(Boolean))];
    if (!ids.length || S.memoryBrowser.actionBusy) return;
    S.memoryBrowser.actionBusy = `delete:${ids.length}`;
    renderMemoryBrowserList();
    try {
        const result = await ApiClient.deleteCurrentMemories(ids, "manual-ui", memoryScopeQuery());
        const status = String(result?.item?.status || "").trim();
        showToast({
            title: status === "queued" ? "已提交删除" : "删除请求未被受理",
            text: status === "queued"
                ? `${ids.length} 条记忆的删除请求已交给记忆 agent，处理结果见「已处理批次」。`
                : "这条请求没有进入队列，记忆内容未改动。",
            kind: status === "queued" ? "success" : "warn",
            durationMs: 4200,
        });
        const selected = { ...(S.memoryBrowser.selected || {}) };
        ids.forEach((id) => delete selected[id]);
        S.memoryBrowser.selected = selected;
        S.memoryBrowser.deleteDialog = { open: false, memoryIds: [], notes: [], busy: false };
        renderMemoryDeleteDialog();
        await loadMemoryBrowser();
    } catch (error) {
        S.memoryBrowser.deleteDialog.busy = false;
        renderMemoryDeleteDialog();
        showToast({ title: "删除失败", text: error?.message || "记忆删除未成功，请稍后重试", kind: "error", durationMs: 5000 });
    } finally {
        S.memoryBrowser.actionBusy = "";
        renderMemoryBrowserList();
    }
}

async function runMemoryBrowserEditSave(memoryId, body, minimal) {
    if (S.memoryBrowser.actionBusy) return;
    S.memoryBrowser.editDialog = { ...S.memoryBrowser.editDialog, busy: true };
    S.memoryBrowser.actionBusy = `update:${memoryId}`;
    renderMemoryBrowserEditState();
    try {
        const original = (Array.isArray(S.memoryBrowser.items) ? S.memoryBrowser.items : [])
            .find((item) => String(item?.memory_id || "").trim() === memoryId);
        const originalMinimal = String(original?.minimal_memory || "");
        const minimalChanged = minimal.trim() !== originalMinimal;
        await ApiClient.updateCurrentMemory(memoryId, {
            memoryBody: body.trim(),
            minimalMemory: minimalChanged ? minimal.trim() : null,
            reason: "manual-ui",
            scope: memoryScopeQuery(),
        });
        showToast({ title: "已提交修改", text: "修改请求已交给记忆 agent 处理，落库结果以「已处理批次」的台账为准。", kind: "success" });
        S.memoryBrowser.editDialog = { open: false, memoryId: "", body: "", minimal: "", busy: false };
        await loadMemoryBrowser();
    } catch (error) {
        showToast({ title: "保存失败", text: error?.message || "记忆修改未成功，请稍后重试", kind: "error", durationMs: 5000 });
        S.memoryBrowser.editDialog = { ...S.memoryBrowser.editDialog, busy: false };
    } finally {
        S.memoryBrowser.actionBusy = "";
        renderMemoryBrowserEditState();
    }
}

async function runMemoryFailedAction(action, failedId) {
    const target = String(failedId || "").trim();
    if (!target || S.memoryFailedActionBusy) return;
    const isRetry = action === "retry";
    S.memoryFailedActionBusy = `${action}:${target}`;
    renderMemoryView();
    showToast({
        title: isRetry ? "正在重新入队" : "正在删除待处理记忆",
        text: "",
        kind: "info",
        durationMs: 0,
        persistent: true,
        spin: true,
    });
    try {
        if (isRetry) {
            await ApiClient.retryMemoryFailed(target, "manual-ui", memoryScopeQuery());
        } else {
            await ApiClient.discardMemoryFailed(target, "manual-ui", memoryScopeQuery());
        }
        closeMemoryDetailPreview();
        if (isRetry) {
            showToast({ title: "已重试", text: "失败记忆已重新入队尾，等待再次处理。", kind: "success" });
        } else {
            showToast({ title: "删除成功", text: "", kind: "success" });
        }
        await loadMemoryView({ force: true, quiet: true });
    } catch (error) {
        showToast({
            title: isRetry ? "重试失败" : "放弃失败",
            text: error?.message || "操作未成功，请稍后重试",
            kind: "error",
            durationMs: 5000,
        });
    } finally {
        S.memoryFailedActionBusy = "";
        renderMemoryView();
    }
}
