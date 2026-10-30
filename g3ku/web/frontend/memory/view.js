// 记忆页视图层：三列卡片、note/详情预览、记忆浏览器与删除/编辑弹窗。
// 状态由 memory/store.js 维护，写路径在 memory/actions.js。

function memoryStatusLabel(status) {
    const normalized = String(status || "").trim().toLowerCase();
    if (normalized === "processing") return "处理中";
    return "待处理";
}

function memoryOpLabel(op) {
    return String(op || "").trim().toLowerCase() === "delete" ? "删除" : "增加";
}

function memoryFailedCategoryLabel(item) {
    const normalized = String(item?.category || "").trim().toLowerCase();
    if (normalized === "provider_error") return "provider 瞬时错误";
    if (normalized === "window_refusal") return "超窗拒发";
    if (normalized === "protocol") return "协议违规";
    if (normalized === "capacity") return "库容不下";
    if (normalized === "capacity_noop") return "归并未受理";
    if (normalized === "agent_fault") return "写入形状故障";
    return normalized || "未知类别";
}

function memoryFailedAutoRetryHint(item) {
    const normalized = String(item?.category || "").trim().toLowerCase();
    if (normalized === "provider_error") return "队列下一次成功处理后自动重排队尾";
    if (normalized === "window_refusal") return "载荷变小或模型窗口调大后手动重试";
    if (normalized === "capacity") return "整理出空间后手动重试；这条批次的原文完整保留在这里";
    if (normalized === "capacity_noop") return "放弃它才会重新允许对同一批记忆排归并";
    if (normalized === "agent_fault") return "手动重试会重新问一次模型；不随队列成功自动重排";
    return "仅支持手动重试";
}

function memoryPayloadBlockFromText(text, charCount, requestId, index) {
    const value = String(text || "");
    const total = Number(charCount ?? value.length) || value.length;
    if (!value.trim() && !total) return null;
    // 首屏永远从第 1 个字符起投，续读才由响应里实际交付的 end_char 推进。
    return {
        requestId: String(requestId || `#${index + 1}`).trim(),
        text: value,
        startChar: 1,
        endChar: value.length,
        charCount: total,
        truncated: value.length < total,
    };
}

function memoryPayloadBlocksFromRecord(source, kind) {
    if (kind === "failed") {
        return (Array.isArray(source?.items) ? source.items : [])
            .map((item, index) => memoryPayloadBlockFromText(
                item?.payload_text, item?.payload_char_count, item?.request_id, index,
            ))
            .filter(Boolean);
    }
    if (kind === "processed") {
        const texts = Array.isArray(source?.payload_texts) ? source.payload_texts : [];
        const counts = Array.isArray(source?.payload_char_counts) ? source.payload_char_counts : [];
        const ids = Array.isArray(source?.request_ids) ? source.request_ids : [];
        return texts
            .map((text, index) => memoryPayloadBlockFromText(text, counts[index], ids[index], index))
            .filter(Boolean);
    }
    if (kind === "queue") {
        return [memoryPayloadBlockFromText(
            source?.payload_text, source?.payload_char_count, source?.request_id, 0,
        )].filter(Boolean);
    }
    return [];
}

function memoryPayloadRangeText({ startChar = 1, endChar = 0, charCount = 0 } = {}) {
    if (!charCount) return "无正文";
    if (endChar >= charCount) return `全文 ${charCount} 字符已全部显示`;
    return `已显示第 ${startChar}–${endChar} 字符 / 共 ${charCount} 字符`;
}

function renderMemoryPayloadBlocks(blocks, kind, key) {
    return blocks.map((block) => `
        <div class="memory-detail-payload-item" data-memory-payload-item data-payload-kind="${esc(kind)}" data-payload-key="${esc(key)}" data-request-id="${esc(block.requestId)}" data-start-char="${block.startChar}" data-end-char="${block.endChar}" data-char-count="${block.charCount}">
            <div class="memory-detail-payload-head">
                <span class="memory-detail-payload-request">${esc(block.requestId)}</span>
                <span class="memory-detail-payload-range">${esc(memoryPayloadRangeText(block))}</span>
            </div>
            <div class="memory-detail-payload-text">${renderMemoryTextWithNoteRefs(block.text)}</div>
            ${block.truncated ? '<button type="button" class="toolbar-btn ghost memory-detail-payload-more" data-memory-payload-more>取全文</button>' : ""}
        </div>
    `).join("");
}

async function loadMoreMemoryPayloadBlock(button) {
    if (!(button instanceof HTMLElement)) return;
    const item = typeof button.closest === "function" ? button.closest("[data-memory-payload-item]") : null;
    if (!item) return;
    const kind = String(item.dataset.payloadKind || "").trim();
    const key = String(item.dataset.payloadKey || "").trim();
    const charCount = Number(item.dataset.charCount || 0);
    const previousEnd = Number(item.dataset.endChar || 0);
    const startChar = previousEnd + 1;
    if (!kind || !key || startChar > charCount) {
        button.hidden = true;
        return;
    }
    button.disabled = true;
    try {
        const payload = typeof ApiClient?.getMemoryPayload === "function"
            ? await ApiClient.getMemoryPayload({
                kind,
                key,
                requestId: String(item.dataset.requestId || "").trim(),
                startChar,
                scope: memoryScopeQuery(),
            })
            : null;
        const chunk = payload?.item || {};
        const text = String(chunk.text || "");
        const deliveredEnd = Number(chunk.end_char || 0);
        if (text && item.querySelector(".memory-detail-payload-text")) {
            // 只往这一条的正文尾部追加：整块重绘会把用户的滚动位置打回顶部。
            item.querySelector(".memory-detail-payload-text").insertAdjacentHTML("beforeend", renderMemoryTextWithNoteRefs(text));
        }
        const advanced = deliveredEnd > previousEnd;
        if (advanced) item.dataset.endChar = String(deliveredEnd);
        const range = item.querySelector(".memory-detail-payload-range");
        if (range) {
            range.textContent = memoryPayloadRangeText({
                startChar: Number(item.dataset.startChar || 1),
                endChar: Number(item.dataset.endChar || 0),
                charCount,
            });
        }
        // 推进不了就收口：再点一次只会拿回同一段（与内容车道续读判据一致）。
        button.hidden = !(advanced && deliveredEnd < charCount);
        button.disabled = false;
    } catch (error) {
        button.disabled = false;
        showToast({
            title: "取全文失败",
            text: String(error?.message || error || "未知错误").slice(0, 160),
            kind: "error",
            durationMs: 5000,
        });
    }
}

function memoryProcessedOpKinds(item) {
    if (memoryProcessedIsNoChange(item)) return [];
    const kinds = [];
    const pushKind = (kind) => {
        if (MEMORY_OP_KINDS[kind] && !kinds.includes(kind)) kinds.push(kind);
    };
    for (const change of memoryProcessedStructuredChanges(item)) {
        pushKind(String(change?.type || "").trim().toLowerCase());
    }
    if (kinds.length) return kinds;
    const requestOps = [item?.source_op, item?.op]
        .map((value) => String(value || "").trim().toLowerCase());
    if (requestOps.includes("delete")) return ["delete"];
    const writeMode = String(item?.write_mode || "").trim().toLowerCase();
    if (writeMode === "add") return ["add"];
    if (writeMode === "rewrite") return ["rewrite"];
    if (writeMode === "mixed") return ["add", "rewrite"];
    if (requestOps.includes("write")) return ["add"];
    return [];
}

function memoryProcessedNoopReason(item) {
    const reason = String(item?.noop_reason || item?.already_satisfied || "").trim();
    if (reason) return reason;
    // 容量停车的"原因"就是它停在哪一级：不显示出来，操作员看到的就是一条没头没尾的记录。
    if (String(item?.status || "").trim().toLowerCase() === "parked") {
        return String(item?.discard_reason || item?.error || "").trim();
    }
    return "";
}

function memoryProcessedChangePreview(item) {
    return String(item?.change_preview || item?.document_preview || "").trim();
}

function memoryAttemptedChainText(item) {
    const refs = (Array.isArray(item?.attempted_model_chain) ? item.attempted_model_chain : [])
        .map((ref) => String(ref || "").trim())
        .filter(Boolean);
    return refs.length ? refs.join(" -> ") : "-";
}

function memoryProcessedIsNoChange(item) {
    const normalized = String(item?.status || "").trim().toLowerCase();
    // 容量停车那一级一行都没写：载荷原文留在失败区，台账这行只是把"停在哪一级"记下来。
    if (normalized === "discarded" || normalized === "parked") return true;
    return !!memoryProcessedNoopReason(item);
}

function memoryProcessedStatusLabel(item) {
    const normalizedStatus = String(item?.status || "").trim().toLowerCase();
    // 停下来的批次既不写变更也不是"看过决定不写"：状态位要说清它停在梯子的哪一级。
    if (normalizedStatus === "parked") return "已停车";
    if (memoryProcessedIsNoChange(item)) return "无变更";
    const normalized = normalizedStatus;
    if (normalized === "discarded") return "已废弃";
    if (normalized === "applied") return "已应用";
    return normalized || "已处理";
}

function memoryProcessedOpLabel(item) {
    if (memoryProcessedIsNoChange(item)) return "保持";
    const kinds = memoryProcessedOpKinds(item);
    if (kinds.length) return kinds.map((kind) => MEMORY_OP_KINDS[kind].label).join(" / ");
    return memoryProcessedStatusLabel(item);
}

function renderMemoryNoteRefChip(noteRef) {
    noteRef = String(noteRef || "").trim();
    if (!noteRef) return "";
    return `<button type="button" class="memory-note-ref-trigger" data-memory-note-ref="${esc(noteRef)}">ref:${esc(noteRef)}</button>`;
}

function renderMemoryDowngradedChip(isCompressed) {
    // 页面显示的正文永远是 `memory_body` 全文，这一颗标记说的才是"模型现在看到的其实是
    // 那行最小摘要"——两件事都真，缺一颗就会让人以为改的就是上下文里的样子。
    if (!isCompressed) return "";
    return '<span class="memory-downgraded-chip">上下文里只显示摘要</span>';
}

function renderMemoryNoteOwnershipChip(noteRef) {
    // 归属来自 `note_ref` 列，不是从正文里正则出来的：常驻行现在只展示句柄，扫文本会把
    // 已经不存在的关系当成缺关系。退场行不进这张表，所以不需要 consolidated_into 兜底。
    noteRef = String(noteRef || "").trim();
    if (!noteRef) return "";
    return `<button type="button" class="memory-note-ref-trigger memory-note-ownership-chip" data-memory-note-ref="${esc(noteRef)}">note:${esc(noteRef)}</button>`;
}

function renderMemoryNoteHandleChip(token, rawText) {
    token = String(token || "").trim();
    if (!token) return "";
    const label = String(rawText || token).trim();
    return `<button type="button" class="memory-note-ref-trigger memory-note-ownership-chip" data-memory-note-ref="${esc(token)}">${esc(label)}</button>`;
}

function renderMemoryTextWithNoteTokens(text) {
    const value = String(text || "");
    if (!value) return "";
    NOTE_TOKEN_RE.lastIndex = 0;
    let cursor = 0;
    let html = "";
    let match = NOTE_TOKEN_RE.exec(value);
    while (match) {
        const token = String(match[1] || match[2] || match[3] || "").trim();
        const matchIndex = Number(match.index || 0);
        const matchText = String(match[0] || "");
        html += esc(value.slice(cursor, matchIndex));
        html += token ? renderMemoryNoteHandleChip(token, matchText) : esc(matchText);
        cursor = matchIndex + matchText.length;
        match = NOTE_TOKEN_RE.exec(value);
    }
    NOTE_TOKEN_RE.lastIndex = 0;
    html += esc(value.slice(cursor));
    return html;
}

function renderMemoryNoteRefList(noteRefs) {
    const items = [...new Set((Array.isArray(noteRefs) ? noteRefs : [])
        .map((item) => String(item || "").trim())
        .filter(Boolean))];
    if (!items.length) return "-";
    return `<span class="memory-note-ref-list">${items.map((noteRef) => renderMemoryNoteRefChip(noteRef)).join("")}</span>`;
}

function renderMemoryTextWithNoteRefs(text) {
    const value = String(text || "");
    if (!value) return "";
    NOTE_REF_RE.lastIndex = 0;
    let cursor = 0;
    let html = "";
    let match = NOTE_REF_RE.exec(value);
    while (match) {
        const matchIndex = Number(match.index || 0);
        const matchText = String(match[0] || "");
        const noteRef = String(match[1] || "").trim();
        html += esc(value.slice(cursor, matchIndex));
        html += renderMemoryNoteRefChip(noteRef);
        cursor = matchIndex + matchText.length;
        match = NOTE_REF_RE.exec(value);
    }
    NOTE_REF_RE.lastIndex = 0;
    html += esc(value.slice(cursor));
    return html;
}

function memoryProcessedStructuredChanges(item) {
    return Array.isArray(item?.changes) ? item.changes : [];
}

function tokenizeMemoryChangeText(text) {
    const tokens = [];
    const pattern = /ref:note_[a-z0-9_]+|\s+|[a-zA-Z0-9_]+|[^\s]/g;
    let match = null;
    while ((match = pattern.exec(String(text || ""))) !== null) tokens.push(match[0]);
    return tokens;
}

function memoryChangeDiffOps(originalTokens, modifiedTokens) {
    const m = originalTokens.length;
    const n = modifiedTokens.length;
    if (!m || !n) return null;
    if (m > MEMORY_CHANGE_DIFF_MAX_TOKENS || n > MEMORY_CHANGE_DIFF_MAX_TOKENS) return null;
    const width = n + 1;
    const table = new Uint32Array((m + 1) * width);
    for (let i = m - 1; i >= 0; i--) {
        const row = i * width;
        const nextRow = row + width;
        for (let j = n - 1; j >= 0; j--) {
            if (originalTokens[i] === modifiedTokens[j]) {
                table[row + j] = table[nextRow + j + 1] + 1;
            } else {
                table[row + j] = table[nextRow + j] >= table[row + j + 1]
                    ? table[nextRow + j]
                    : table[row + j + 1];
            }
        }
    }
    const ops = [];
    let i = 0;
    let j = 0;
    while (i < m && j < n) {
        if (originalTokens[i] === modifiedTokens[j]) {
            ops.push({ kind: "same", token: originalTokens[i] });
            i++;
            j++;
        } else if (table[(i + 1) * width + j] >= table[i * width + j + 1]) {
            ops.push({ kind: "del", token: originalTokens[i] });
            i++;
        } else {
            ops.push({ kind: "add", token: modifiedTokens[j] });
            j++;
        }
    }
    while (i < m) {
        ops.push({ kind: "del", token: originalTokens[i] });
        i++;
    }
    while (j < n) {
        ops.push({ kind: "add", token: modifiedTokens[j] });
        j++;
    }
    return ops;
}

function memoryChangeDiffSegments(originalText, modifiedText) {
    const originalTokens = tokenizeMemoryChangeText(originalText);
    const modifiedTokens = tokenizeMemoryChangeText(modifiedText);
    if (!originalTokens.length || !modifiedTokens.length) return null;
    const ops = memoryChangeDiffOps(originalTokens, modifiedTokens);
    if (!ops) return null;
    const segments = [];
    let current = null;
    for (const op of ops) {
        if (op.kind === "del") continue;
        const changed = op.kind === "add";
        if (!current || current.changed !== changed) {
            current = { changed, text: "" };
            segments.push(current);
        }
        current.text += op.token;
    }
    return segments;
}

function renderMemoryDiffHighlighted(originalText, modifiedText) {
    if (!String(originalText || "").trim() || !String(modifiedText || "").trim()) return "";
    const segments = memoryChangeDiffSegments(originalText, modifiedText);
    if (!segments || !segments.some((segment) => segment.changed)) return "";
    return segments.map((segment) => {
        if (!segment.changed) return renderMemoryTextWithNoteRefs(segment.text);
        const leading = (String(segment.text).match(/^\s+/) || [""])[0];
        const trailing = (String(segment.text).match(/\s+$/) || [""])[0];
        const core = segment.text.slice(leading.length, segment.text.length - trailing.length);
        const coreHtml = core ? renderMemoryTextWithNoteRefs(core) : "";
        return coreHtml ? `${esc(leading)}<mark class="memory-change-hl">${coreHtml}</mark>${esc(trailing)}` : "";
    }).join("");
}

function memoryChangeTypeLabel(type) {
    const normalized = String(type || "").trim().toLowerCase();
    if (normalized === "add") return "新增";
    if (normalized === "rewrite") return "修改";
    if (normalized === "delete") return "删除";
    if (normalized === "note_upsert") return "更新 Note";
    if (normalized === "note_edit") return "编辑 Note";
    if (normalized === "note_reaped") return "回收 Note";
    if (normalized === "category_note") return "归并记忆";
    if (normalized === "consolidate") return "记忆退场";
    if (normalized === "category_purge") return "摘除分类导航";
    if (normalized === "compress_for_capacity") return "容量降级";
    return normalized || "变更";
}

function renderMemoryChangeBlock(change) {
    const type = String(change?.type || "").trim().toLowerCase();
    const typeLabel = memoryChangeTypeLabel(type);
    const memoryId = String(change?.memory_id || "").trim();
    const noteRef = String(change?.note_ref || "").trim();
    const content = String(change?.content || "");
    const original = String(change?.original_content || "");
    const originalMissing = Boolean(change?.original_missing);

    let bodyHtml = "";
    if (type === "rewrite") {
        const originalHtml = originalMissing && !original.trim()
            ? `<div class="memory-change-text memory-change-missing">历史批次未保留原文</div>`
            : `<div class="memory-change-text">${renderMemoryTextWithNoteRefs(original) || "-"}</div>`;
        const highlightedModified = renderMemoryDiffHighlighted(original, content);
        const modifiedHtml = content.trim()
            ? (highlightedModified
                ? `<div class="memory-change-text">${highlightedModified}</div>`
                : `<div class="memory-change-text">${renderMemoryTextWithNoteRefs(content)}</div>`)
            : `<div class="memory-change-text memory-change-missing">历史批次未保留修改后内容</div>`;
        bodyHtml = `
            <div class="memory-change-compare">
                <div class="memory-change-col">
                    <div class="memory-change-col-label">原文</div>
                    ${originalHtml}
                </div>
                <div class="memory-change-col">
                    <div class="memory-change-col-label">修改后</div>
                    ${modifiedHtml}
                </div>
            </div>
        `;
    } else if (type === "delete") {
        const deletedHtml = original.trim()
            ? `<div class="memory-change-text">${renderMemoryTextWithNoteRefs(original)}</div>`
            : `<div class="memory-change-text memory-change-missing">${originalMissing ? "历史批次未保留被删内容，仅记录了 ID" : "-"}</div>`;
        bodyHtml = `
            <div class="memory-change-col">
                <div class="memory-change-col-label">删除的内容</div>
                ${deletedHtml}
            </div>
        `;
    } else if (type === "note_upsert") {
        bodyHtml = `
            <div class="memory-change-col">
                <div class="memory-change-col-label">Note 内容</div>
                <div class="memory-change-text">${renderMemoryTextWithNoteRefs(content) || "-"}</div>
            </div>
        `;
    } else if (type === "note_edit") {
        const applied = Boolean(change?.applied);
        const newText = String(change?.new_text || "");
        const modifiedHtml = applied
            ? (newText.trim()
                ? `<div class="memory-change-text">${renderMemoryTextWithNoteRefs(newText)}</div>`
                : `<div class="memory-change-text memory-change-missing">已删掉这段</div>`)
            : `<div class="memory-change-text memory-change-missing">未生效：${esc(String(change?.error || ""))}</div>`;
        bodyHtml = `
            <div class="memory-change-compare">
                <div class="memory-change-col">
                    <div class="memory-change-col-label">原文</div>
                    <div class="memory-change-text">${renderMemoryTextWithNoteRefs(String(change?.old_text || "")) || "-"}</div>
                </div>
                <div class="memory-change-col">
                    <div class="memory-change-col-label">改为</div>
                    ${modifiedHtml}
                </div>
            </div>
        `;
    } else if (type === "category_note") {
        const edits = Array.isArray(change?.edits) ? change.edits : [];
        const editsHtml = edits.length
            ? `<div class="memory-change-col">
                   <div class="memory-change-col-label">局部改动</div>
                   ${edits.map((op) => `<div class="memory-change-text">${renderMemoryDiffHighlighted(String(op?.old_text || ""), String(op?.new_text || "")) || esc(String(op?.old_text || ""))}</div>`).join("")}
               </div>`
            : "";
        bodyHtml = `
            ${content.trim()
                ? `<div class="memory-change-col">
                       <div class="memory-change-col-label">分类 Note 内容</div>
                       <div class="memory-change-text">${renderMemoryTextWithNoteRefs(content)}</div>
                   </div>`
                : editsHtml}
        `;
    } else if (type === "consolidate") {
        bodyHtml = `
            <div class="memory-change-col">
                <div class="memory-change-col-label">退场的正文</div>
                <div class="memory-change-text">${renderMemoryTextWithNoteRefs(original) || "-"}</div>
            </div>
        `;
    } else if (type === "category_purge") {
        bodyHtml = `
            <div class="memory-change-col">
                <div class="memory-change-col-label">从「${esc(String(change?.note_ref || "-"))}」摘下的正文</div>
                <div class="memory-change-text">${renderMemoryTextWithNoteRefs(original) || "-"}</div>
            </div>
        `;
    } else if (type === "note_reaped") {
        const reason = String(change?.reason || "").trim();
        bodyHtml = `
            <div class="memory-change-col">
                <div class="memory-change-col-label">回收原因</div>
                <div class="memory-change-text">${esc(MEMORY_NOTE_REAP_REASONS[reason] || reason || "-")}</div>
            </div>
        `;
    } else {
        bodyHtml = `
            <div class="memory-change-col">
                <div class="memory-change-col-label">新增内容</div>
                <div class="memory-change-text">${renderMemoryTextWithNoteRefs(content) || "-"}</div>
            </div>
        `;
    }

    const idChip = noteRef
        ? `<span class="policy-chip neutral">ref:${esc(noteRef)}</span>`
        : (memoryId ? `<span class="policy-chip neutral">${esc(memoryId)}</span>` : "");

    return `
        <div class="memory-change-block" data-change-type="${esc(type)}">
            <div class="memory-change-head">
                <span class="memory-change-type">${esc(typeLabel)}</span>
                ${idChip}
            </div>
            ${bodyHtml}
        </div>
    `;
}

function renderMemoryChangeList(changes) {
    const items = Array.isArray(changes) ? changes : [];
    if (!items.length) return "";
    return `<div class="memory-change-list">${items.map((change) => renderMemoryChangeBlock(change)).join("")}</div>`;
}

function memoryPreviewText(text, maxChars = 240) {
    const value = String(text || "").trim();
    if (!value) return "";
    if (value.length <= maxChars) return value;
    return `${value.slice(0, maxChars).trimEnd()}…`;
}

function renderMemoryPreviewBlock(label, text) {
    const preview = memoryPreviewText(text);
    if (!preview) return "";
    return `
        <div class="memory-card-preview">
            <div class="memory-card-preview-label">${esc(label || "预览")}</div>
            <div class="memory-card-preview-text">${esc(preview)}</div>
        </div>
    `;
}

function ensureMemoryNotePreviewUi() {
    if (U.memoryNoteBackdrop && U.memoryNoteDrawer) return;
    const host = U.viewMemory || document.body;
    const backdrop = document.createElement("div");
    backdrop.id = "memory-note-preview-backdrop";
    backdrop.className = "detail-backdrop";
    backdrop.setAttribute("aria-hidden", "true");
    const drawer = document.createElement("section");
    drawer.id = "memory-note-preview-drawer";
    drawer.className = "panel detail-drawer memory-note-preview-drawer";
    drawer.setAttribute("role", "dialog");
    drawer.setAttribute("aria-modal", "true");
    drawer.setAttribute("aria-hidden", "true");
    drawer.setAttribute("aria-labelledby", "memory-note-preview-title");
    drawer.tabIndex = -1;
    drawer.innerHTML = `
        <div class="detail-modal-header">
            <div class="memory-note-preview-head">
                <h2 id="memory-note-preview-title">Note 预览</h2>
                <p id="memory-note-preview-subtitle" class="subtitle"></p>
            </div>
            <div class="memory-note-head-actions">
                <button type="button" class="toolbar-btn ghost" data-memory-note-edit-toggle title="进入编辑模式修改 note 正文">编辑</button>
                <button type="button" class="toolbar-btn ghost" data-memory-note-close data-modal-close>关闭</button>
            </div>
        </div>
        <div class="detail-modal-body">
            <nav id="memory-note-preview-trail" class="memory-note-preview-trail" hidden aria-label="归并记忆浏览路径"></nav>
            <div class="memory-note-preview-shell">
                <div id="memory-note-preview-status" class="memory-note-preview-status"></div>
                <div id="memory-note-index-list" class="memory-note-index-list" hidden></div>
                <pre id="memory-note-preview-body" class="memory-note-preview-body"></pre>
                <textarea id="memory-note-edit-body" class="memory-note-edit-body" rows="12" hidden aria-label="note 正文编辑"></textarea>
            </div>
        </div>
        <div id="memory-note-edit-footer" class="detail-modal-footer memory-note-edit-footer" hidden>
            <div class="memory-note-edit-buttons">
                <button type="button" class="toolbar-btn ghost" data-memory-note-edit-cancel>取消</button>
                <button type="button" class="toolbar-btn success" data-memory-note-edit-save>保存修改</button>
            </div>
        </div>
    `;
    host.appendChild(backdrop);
    host.appendChild(drawer);
    U.memoryNoteBackdrop = backdrop;
    U.memoryNoteDrawer = drawer;
    U.memoryNoteTitle = drawer.querySelector("#memory-note-preview-title");
    U.memoryNoteSubtitle = drawer.querySelector("#memory-note-preview-subtitle");
    U.memoryNoteStatus = drawer.querySelector("#memory-note-preview-status");
    U.memoryNoteBody = drawer.querySelector("#memory-note-preview-body");
    U.memoryNoteTrail = drawer.querySelector("#memory-note-preview-trail");
    U.memoryNoteIndexList = drawer.querySelector("#memory-note-index-list");
    U.memoryNoteEditBody = drawer.querySelector("#memory-note-edit-body");
    U.memoryNoteEditFooter = drawer.querySelector("#memory-note-edit-footer");
    U.memoryNoteEditToggle = drawer.querySelector("[data-memory-note-edit-toggle]");
    U.memoryNoteEditSave = drawer.querySelector("[data-memory-note-edit-save]");
    U.memoryNoteClose = drawer.querySelector("[data-memory-note-close]");
    U.memoryNoteClose?.addEventListener("click", () => closeMemoryNotePreview());
    U.memoryNoteBackdrop?.addEventListener("click", () => closeMemoryNotePreview());
    U.memoryNoteEditToggle?.addEventListener("click", () => toggleMemoryNoteEditMode());
    drawer.querySelector("[data-memory-note-edit-cancel]")?.addEventListener("click", () => {
        S.memoryNotePreview.editMode = false;
        renderMemoryNotePreview();
    });
    U.memoryNoteEditSave?.addEventListener("click", () => requestMemoryNoteSave());
    U.memoryNoteEditBody?.addEventListener("input", () => {
        S.memoryNotePreview.editBody = U.memoryNoteEditBody.value || "";
    });
    // 归并记忆浏览的列表行、面包屑与正文里的 note 令牌都在这个窗内走完：借其它显示面
    // 的委托会让一次点击开出两个窗，两级内容还各说各话。
    drawer.addEventListener("click", (e) => {
        const preview = S.memoryNotePreview;
        if (e.target.closest("[data-memory-note-index-back]")) {
            void openMemoryNoteBrowser();
            return;
        }
        // 这一层是浏览道：分类 note 的写侧只有归并 agent，note 更新端点对 memory/index/
        // 回 404，所以这里的开关一律不给编辑——留着就是点了必失败的假门。
        const readOnly = { editable: false, origin: "index" };
        const crumb = e.target.closest("[data-memory-note-trail-ref]");
        if (crumb) {
            const crumbRef = String(crumb.dataset.memoryNoteTrailRef || "").trim();
            if (!crumbRef) return;
            const trail = Array.isArray(preview.trail) ? preview.trail : [];
            const cut = trail.findIndex((entry) => String(entry?.ref || "").trim() === crumbRef);
            void openMemoryNotePreview(crumbRef, { ...readOnly, trail: cut > 0 ? trail.slice(0, cut) : [] });
            return;
        }
        const noteTrigger = e.target.closest("[data-memory-note-ref]");
        if (!noteTrigger) return;
        const noteRef = String(noteTrigger.dataset.memoryNoteRef || "").trim();
        if (!noteRef) return;
        const trail = Array.isArray(preview.trail) ? [...preview.trail] : [];
        if (preview.mode === "note" && preview.ref && preview.ref !== noteRef) {
            trail.push({ ref: preview.ref });
        }
        void openMemoryNotePreview(noteRef, { ...readOnly, trail });
    });
}

function renderMemoryNotePreview() {
    ensureMemoryNotePreviewUi();
    const preview = S.memoryNotePreview;
    const noteRef = String(preview.ref || "").trim();
    // 同一个窗两种形态：index=归并区分类列表，note=某份 note 正文（含一路点下去的层级）。
    const isIndex = String(preview.mode || "note") === "index";
    const editable = !!preview.editable && !isIndex;
    const editMode = !!preview.editMode && editable;
    if (U.memoryNoteTitle) {
        U.memoryNoteTitle.textContent = isIndex
            ? "归并记忆"
            : (noteRef ? `Note 预览 · ${noteRef}` : "Note 预览");
    }
    if (U.memoryNoteSubtitle) {
        U.memoryNoteSubtitle.textContent = isIndex ? "归并区每分类一行，点分类名看正文。" : "";
    }
    if (U.memoryNoteTrail) {
        const trailHtml = isIndex ? "" : renderMemoryNoteTrailHtml(preview);
        setInnerHtmlPreservingScroll(U.memoryNoteTrail, trailHtml);
        U.memoryNoteTrail.hidden = !trailHtml;
    }
    if (U.memoryNoteStatus) {
        const errorText = String(isIndex ? (preview.indexError || "") : (preview.error || "")).trim();
        const busy = isIndex ? !!preview.indexBusy : !!preview.busy;
        if (busy) {
            U.memoryNoteStatus.textContent = isIndex ? "正在加载归并记忆..." : "正在加载 note 正文...";
            U.memoryNoteStatus.hidden = false;
            U.memoryNoteStatus.classList.remove("is-error");
        } else if (errorText) {
            U.memoryNoteStatus.textContent = errorText;
            U.memoryNoteStatus.hidden = false;
            U.memoryNoteStatus.classList.add("is-error");
        } else {
            U.memoryNoteStatus.textContent = isIndex ? "" : (noteRef ? `当前预览：${noteRef}` : "");
            U.memoryNoteStatus.hidden = isIndex ? true : !noteRef;
            U.memoryNoteStatus.classList.remove("is-error");
        }
    }
    if (U.memoryNoteIndexList) {
        U.memoryNoteIndexList.hidden = !isIndex;
        if (isIndex) {
            setInnerHtmlPreservingScroll(U.memoryNoteIndexList, renderMemoryNoteIndexListHtml(preview));
        }
    }
    if (U.memoryNoteBody) {
        U.memoryNoteBody.hidden = isIndex || editMode;
        if (!isIndex && !editMode) {
            const bodyText = String(preview.body || "");
            setInnerHtmlPreservingScroll(
                U.memoryNoteBody,
                preview.busy
                    ? ""
                    : (bodyText.trim()
                        ? renderMemoryTextWithNoteTokens(bodyText)
                        : esc("当前 note 没有正文。")),
            );
        }
    }
    if (U.memoryNoteEditBody) {
        U.memoryNoteEditBody.hidden = !editMode;
        if (editMode && document.activeElement !== U.memoryNoteEditBody) {
            U.memoryNoteEditBody.value = String(S.memoryNotePreview.editBody || "");
        }
    }
    if (U.memoryNoteEditFooter) U.memoryNoteEditFooter.hidden = !editMode;
    if (U.memoryNoteEditToggle) {
        U.memoryNoteEditToggle.hidden = !editable;
        U.memoryNoteEditToggle.textContent = editMode ? "完成" : "编辑";
        U.memoryNoteEditToggle.disabled = !!S.memoryNotePreview.busy || !!S.memoryNotePreview.saving;
    }
    if (U.memoryNoteEditSave) U.memoryNoteEditSave.disabled = !!S.memoryNotePreview.saving;
    setDrawerOpen(U.memoryNoteBackdrop, U.memoryNoteDrawer, !!S.memoryNotePreview.open);
}

function renderMemoryNoteTrailHtml(preview) {
    // 面包屑只在从「归并记忆」进来的那条道上出现；从记忆行/队列点开的预览仍是一层窗一句标题。
    if (String(preview.origin || "direct") !== "index") return "";
    const crumbs = [{ label: "归并记忆", back: true }];
    (Array.isArray(preview.trail) ? preview.trail : []).forEach((entry) => {
        const ref = String(entry?.ref || "").trim();
        if (ref) crumbs.push({ label: ref, ref });
    });
    const currentRef = String(preview.ref || "").trim();
    if (currentRef) crumbs.push({ label: currentRef, current: true });
    if (crumbs.length <= 1) return "";
    return crumbs.map((crumb) => {
        if (crumb.current) {
            return `<span class="memory-note-trail-crumb is-current">${esc(crumb.label)}</span>`;
        }
        if (crumb.ref) {
            return `<button type="button" class="memory-note-trail-crumb" data-memory-note-trail-ref="${esc(crumb.ref)}">${esc(crumb.label)}</button>`;
        }
        return `<button type="button" class="memory-note-trail-crumb" data-memory-note-index-back aria-label="返回归并记忆列表">${esc(crumb.label)}</button>`;
    }).join(`<span class="memory-note-trail-sep">/</span>`);
}

function renderMemoryNoteIndexListHtml(preview) {
    if (preview.indexBusy) return `<div class="memory-note-index-empty">正在加载归并记忆...</div>`;
    const errorText = String(preview.indexError || "").trim();
    if (errorText) return `<div class="memory-note-index-empty is-error">${esc(errorText)}</div>`;
    const items = Array.isArray(preview.indexItems) ? preview.indexItems : [];
    if (!items.length) return `<div class="memory-note-index-empty">归并区还没有分类 note。</div>`;
    const rows = items.map((item) => {
        const ref = String(item?.note_ref || "").trim();
        const handle = String(item?.handle || "").trim();
        const scope = String(item?.scope_line || "").trim();
        const unusable = Boolean(item?.unusable);
        const overLimit = Boolean(item?.scope_line_over_limit);
        const count = Number(item?.entry_count || 0);
        return `<tr class="memory-note-index-row${unusable ? " is-unusable" : ""}">
            <td><button type="button" class="memory-note-ref-trigger" data-memory-note-ref="${esc(ref)}">${esc(ref)}</button></td>
            <td>${esc(handle) || "-"}</td>
            <td class="memory-note-index-cell-scope">${
                scope && !overLimit
                    ? esc(scope)
                    : overLimit
                        ? `说明行 ${scope.length} 字，过长未进常驻块`
                        : "分类 note 缺失或没有说明行"
            }</td>
            <td class="memory-note-index-cell-count">${count}</td>
        </tr>`;
    }).join("");
    return `<table class="memory-note-index-table">
        <thead><tr><th>分类 note</th><th>常驻句柄</th><th>说明行</th><th>条目</th></tr></thead>
        <tbody>${rows}</tbody>
    </table>`;
}

async function openMemoryNoteBrowser() {
    ensureMemoryNotePreviewUi();
    const preview = S.memoryNotePreview;
    preview.open = true;
    preview.mode = "index";
    preview.origin = "index";
    preview.trail = [];
    preview.ref = "";
    preview.body = "";
    preview.error = "";
    preview.editMode = false;
    preview.editBody = "";
    preview.requestToken += 1;
    renderMemoryNotePreview();
    await loadMemoryIndexNotes();
}

async function loadMemoryIndexNotes() {
    const preview = S.memoryNotePreview;
    preview.indexBusy = true;
    preview.indexToken += 1;
    const requestToken = preview.indexToken;
    renderMemoryNotePreview();
    try {
        const data = await ApiClient.listMemoryIndexNotes(memoryScopeQuery());
        if (requestToken !== preview.indexToken) return;
        preview.indexItems = Array.isArray(data?.items) ? data.items : [];
        preview.indexError = "";
    } catch (error) {
        if (requestToken !== preview.indexToken) return;
        preview.indexItems = [];
        preview.indexError = error?.message || "归并记忆暂时不可读取，请稍后刷新。";
    } finally {
        if (requestToken !== preview.indexToken) return;
        preview.indexBusy = false;
        renderMemoryNotePreview();
    }
}

function toggleMemoryNoteEditMode() {
    const preview = S.memoryNotePreview;
    if (preview.busy || preview.saving || !preview.editable) return;
    preview.editMode = !preview.editMode;
    if (preview.editMode) {
        preview.editBody = String(preview.body || "");
    }
    renderMemoryNotePreview();
    if (preview.editMode) U.memoryNoteEditBody?.focus();
}

function requestMemoryNoteSave() {
    const preview = S.memoryNotePreview;
    const ref = String(preview.ref || "").trim();
    if (!ref || preview.saving || !preview.editMode || !preview.editable) return;
    const body = String(U.memoryNoteEditBody?.value ?? preview.editBody ?? "");
    if (!body.trim()) {
        showToast({ title: "内容不能为空", text: "note 正文为必填项。", kind: "warn" });
        return;
    }
    openConfirm({
        title: "保存 note 修改",
        text: `确定保存对 ${ref} 的修改吗？`,
        confirmLabel: "保存",
        confirmKind: "danger",
        onConfirm: () => void runMemoryNoteSave(ref, body),
    });
}

function closeMemoryNotePreview() {
    S.memoryNotePreview.open = false;
    S.memoryNotePreview.editMode = false;
    S.memoryNotePreview.editBody = "";
    renderMemoryNotePreview();
}

async function openMemoryNotePreview(noteRef, options = {}) {
    const normalizedRef = String(noteRef || "").trim();
    if (!normalizedRef) return;
    ensureMemoryNotePreviewUi();
    S.memoryNotePreview.open = true;
    S.memoryNotePreview.busy = true;
    S.memoryNotePreview.ref = normalizedRef;
    S.memoryNotePreview.body = "";
    S.memoryNotePreview.error = "";
    S.memoryNotePreview.editMode = false;
    S.memoryNotePreview.editBody = "";
    // 已处理批次的历史入口（变更内容等）打开的 note 窗只读，不提供编辑
    S.memoryNotePreview.editable = options.editable !== false;
    S.memoryNotePreview.mode = "note";
    // 从「归并记忆」浏览层点进来的带路径，面包屑才能一路回退；其它入口保持单层窗。
    S.memoryNotePreview.origin = String(options.origin || "direct");
    S.memoryNotePreview.trail = Array.isArray(options.trail) ? options.trail : [];
    S.memoryNotePreview.requestToken += 1;
    const requestToken = S.memoryNotePreview.requestToken;
    renderMemoryNotePreview();
    try {
        const item = await ApiClient.getMemoryNote(noteRef, memoryScopeQuery());
        if (requestToken !== S.memoryNotePreview.requestToken) return;
        S.memoryNotePreview.ref = String(item?.ref || normalizedRef).trim() || normalizedRef;
        S.memoryNotePreview.body = String(item?.body || "");
        S.memoryNotePreview.error = "";
    } catch (error) {
        if (requestToken !== S.memoryNotePreview.requestToken) return;
        S.memoryNotePreview.error = error?.message || "读取记忆 note 失败";
        showToast({ title: "Note 预览失败", text: S.memoryNotePreview.error, kind: "error" });
    } finally {
        if (requestToken !== S.memoryNotePreview.requestToken) return;
        S.memoryNotePreview.busy = false;
        renderMemoryNotePreview();
    }
}

function ensureMemoryDetailPreviewUi() {
    if (U.memoryDetailBackdrop && U.memoryDetailDrawer) return;
    const host = U.viewMemory || document.body;
    const backdrop = document.createElement("div");
    backdrop.id = "memory-detail-preview-backdrop";
    backdrop.className = "detail-backdrop";
    backdrop.setAttribute("aria-hidden", "true");
    const drawer = document.createElement("section");
    drawer.id = "memory-detail-preview-drawer";
    drawer.className = "panel detail-drawer memory-detail-preview-drawer";
    drawer.setAttribute("role", "dialog");
    drawer.setAttribute("aria-modal", "true");
    drawer.setAttribute("aria-hidden", "true");
    drawer.setAttribute("aria-labelledby", "memory-detail-preview-title");
    drawer.tabIndex = -1;
    drawer.innerHTML = `
        <div class="detail-modal-header">
            <div class="memory-detail-preview-head">
                <h2 id="memory-detail-preview-title">只读记忆详情</h2>
                <p id="memory-detail-preview-subtitle" class="subtitle">完整内容仅供查看，不支持编辑或保存。</p>
            </div>
            <button type="button" class="toolbar-btn ghost" data-memory-detail-close data-modal-close>关闭</button>
        </div>
        <div class="detail-modal-body">
            <div class="memory-detail-preview-shell">
                <div id="memory-detail-preview-meta" class="memory-detail-preview-groups"></div>
                <section id="memory-detail-preview-primary-section" class="memory-detail-preview-text-section">
                    <div id="memory-detail-preview-primary-title" class="memory-detail-preview-text-title">正文内容</div>
                    <div id="memory-detail-preview-primary" class="memory-detail-preview-text-block memory-card-body-text-rich"></div>
                </section>
                <section id="memory-detail-preview-secondary-section" class="memory-detail-preview-text-section" hidden>
                    <div id="memory-detail-preview-secondary-title" class="memory-detail-preview-text-title">补充信息</div>
                    <div id="memory-detail-preview-secondary" class="memory-detail-preview-text-block memory-card-body-text-rich"></div>
                </section>
            </div>
        </div>
        <div id="memory-detail-preview-actions" class="detail-modal-footer memory-detail-preview-actions" hidden>
            <span id="memory-detail-preview-actions-hint" class="memory-detail-actions-hint"></span>
            <div class="memory-detail-actions-buttons">
                <button type="button" class="toolbar-btn danger" data-memory-failed-action="discard">放弃</button>
                <button type="button" class="toolbar-btn success" data-memory-failed-action="retry">
                    <i data-lucide="rotate-ccw" aria-hidden="true"></i>
                    重试（重新入队尾）
                </button>
            </div>
        </div>
    `;
    host.appendChild(backdrop);
    host.appendChild(drawer);
    U.memoryDetailBackdrop = backdrop;
    U.memoryDetailDrawer = drawer;
    U.memoryDetailTitle = drawer.querySelector("#memory-detail-preview-title");
    U.memoryDetailSubtitle = drawer.querySelector("#memory-detail-preview-subtitle");
    U.memoryDetailMeta = drawer.querySelector("#memory-detail-preview-meta");
    U.memoryDetailPrimarySection = drawer.querySelector("#memory-detail-preview-primary-section");
    U.memoryDetailPrimaryTitle = drawer.querySelector("#memory-detail-preview-primary-title");
    U.memoryDetailPrimary = drawer.querySelector("#memory-detail-preview-primary");
    U.memoryDetailSecondarySection = drawer.querySelector("#memory-detail-preview-secondary-section");
    U.memoryDetailSecondaryTitle = drawer.querySelector("#memory-detail-preview-secondary-title");
    U.memoryDetailSecondary = drawer.querySelector("#memory-detail-preview-secondary");
    U.memoryDetailActions = drawer.querySelector("#memory-detail-preview-actions");
    U.memoryDetailActionsHint = drawer.querySelector("#memory-detail-preview-actions-hint");
    U.memoryDetailClose = drawer.querySelector("[data-memory-detail-close]");
    U.memoryDetailClose?.addEventListener("click", () => closeMemoryDetailPreview());
    U.memoryDetailBackdrop?.addEventListener("click", () => closeMemoryDetailPreview());
    U.memoryDetailDrawer?.addEventListener("click", (e) => {
        if (!(e.target instanceof Element)) return;
        const payloadMore = e.target.closest("[data-memory-payload-more]");
        if (payloadMore) {
            e.preventDefault();
            e.stopPropagation();
            if (payloadMore.disabled) return;
            void loadMoreMemoryPayloadBlock(payloadMore);
            return;
        }
        const failedAction = e.target.closest("[data-memory-failed-action]");
        if (failedAction) {
            e.preventDefault();
            e.stopPropagation();
            if (failedAction.disabled) return;
            const failedId = String(S.memoryDetailPreview?.key || "").trim();
            const action = String(failedAction.dataset?.memoryFailedAction || "").trim();
            if (!failedId || !action) return;
            if (action === "discard") {
                requestMemoryFailedDiscard(failedId);
            } else {
                void runMemoryFailedAction("retry", failedId);
            }
            return;
        }
        const noteTrigger = e.target.closest("[data-memory-note-ref]");
        if (!noteTrigger) return;
        e.preventDefault();
        e.stopPropagation();
        // 已处理批次详情（含变更内容）是历史视图：其 note 窗只读，不提供编辑
        const readOnlyNote = String(S.memoryDetailPreview?.kind || "").trim() === "processed";
        void openMemoryNotePreview(noteTrigger.dataset.memoryNoteRef || "", { editable: !readOnlyNote });
    });
}

function renderMemoryDetailPreview() {
    ensureMemoryDetailPreviewUi();
    const preview = S.memoryDetailPreview || {};
    if (U.memoryDetailTitle) U.memoryDetailTitle.textContent = String(preview.title || "").trim() || "只读记忆详情";
    if (U.memoryDetailSubtitle) U.memoryDetailSubtitle.textContent = String(preview.subtitle || "").trim() || "完整内容仅供查看，不支持编辑或保存。";
    if (U.memoryDetailMeta) {
        const explicitGroups = Array.isArray(preview.groups) ? preview.groups : [];
        const fallbackFields = Array.isArray(preview.fields) ? preview.fields : [];
        const normalizedGroups = explicitGroups.length
            ? explicitGroups
            : (preview.kind === "processed"
                ? [
                    { title: "基础信息", items: fallbackFields },
                ]
                : [
                    { title: "基础信息", items: fallbackFields.slice(0, 4) },
                    { title: "运行信息", items: fallbackFields.slice(4) },
                ]);
        U.memoryDetailMeta.innerHTML = normalizedGroups.map((group) => {
            const items = Array.isArray(group?.items) ? group.items : [];
            return `
                <section class="memory-detail-preview-group">
                    <div class="memory-detail-preview-group-head">
                        <div class="memory-detail-preview-group-title">${esc(String(group?.title || "").trim() || "-")}</div>
                    </div>
                    <div class="memory-detail-preview-group-body">
                        ${items.map((item) => `
                            <div class="memory-detail-preview-field">
                                <div class="memory-detail-preview-field-label">${esc(String(item?.label || "").trim() || "-")}</div>
                                <div class="memory-detail-preview-field-value">${esc(String(item?.value || "").trim() || "-")}</div>
                            </div>
                        `).join("")}
                    </div>
                </section>
            `;
        }).join("");
    }
    if (U.memoryDetailPrimaryTitle) {
        U.memoryDetailPrimaryTitle.textContent = preview.kind === "processed" ? "原始请求内容" : "请求正文";
    }
    if (U.memoryDetailPrimary) {
        const blocks = Array.isArray(preview.primaryBlocks) ? preview.primaryBlocks : [];
        if (blocks.length) {
            setInnerHtmlPreservingScroll(
                U.memoryDetailPrimary,
                renderMemoryPayloadBlocks(blocks, String(preview.kind || "").trim(), String(preview.key || "").trim()),
            );
        } else {
            const primaryText = String(preview.primaryText || "").trim() || "当前没有可显示的正文。";
            setInnerHtmlPreservingScroll(U.memoryDetailPrimary, renderMemoryTextWithNoteRefs(primaryText));
        }
    }
    const secondaryText = String(preview.secondaryText || "").trim();
    const changeListHtml = renderMemoryChangeList(preview.changes);
    const reconstructedHint = changeListHtml && preview.changesReconstructed
        ? `<div class="memory-change-reconstructed-hint">以下变更由历史摘要重建；当时的原文与删除内容未被保留。</div>`
        : "";
    const hasSecondary = Boolean(changeListHtml) || Boolean(secondaryText);
    if (U.memoryDetailSecondarySection) U.memoryDetailSecondarySection.hidden = !hasSecondary;
    if (U.memoryDetailSecondaryTitle) {
        U.memoryDetailSecondaryTitle.textContent = String(preview.secondaryTitle || "").trim()
            || (preview.kind === "processed" ? "变更内容" : "最近错误");
    }
    if (U.memoryDetailSecondary) {
        if (changeListHtml) {
            setInnerHtmlPreservingScroll(U.memoryDetailSecondary, reconstructedHint + changeListHtml);
        } else {
            setInnerHtmlPreservingScroll(U.memoryDetailSecondary, secondaryText ? renderMemoryTextWithNoteRefs(secondaryText) : "");
        }
    }
    const shell = U.memoryDetailDrawer?.querySelector(".memory-detail-preview-shell") || null;
    if (shell && U.memoryDetailSecondarySection && U.memoryDetailPrimarySection) {
        const wantsSecondaryFirst = preview.kind === "processed" || preview.kind === "failed";
        const secondaryFirstNow = U.memoryDetailSecondarySection.nextElementSibling === U.memoryDetailPrimarySection;
        // 仅在顺序确实需要改变时才移动节点：每次轮询都 insertBefore/appendChild
        // 会重建 DOM 子树，把变更内容滚动条重置回顶部。
        if (wantsSecondaryFirst && !secondaryFirstNow) {
            shell.insertBefore(U.memoryDetailSecondarySection, U.memoryDetailPrimarySection);
        } else if (!wantsSecondaryFirst && secondaryFirstNow) {
            shell.appendChild(U.memoryDetailSecondarySection);
        }
    }
    if (U.memoryDetailActions) {
        const isFailedOpen = !!preview.open && preview.kind === "failed";
        U.memoryDetailActions.hidden = !isFailedOpen;
        if (isFailedOpen) {
            const failedRecord = Array.isArray(S.memoryFailedItems)
                ? S.memoryFailedItems.find((item) => String(item?.failed_id || "").trim() === String(preview.key || "").trim())
                : null;
            const actionBusy = String(S.memoryFailedActionBusy || "").trim();
            if (U.memoryDetailActionsHint) {
                U.memoryDetailActionsHint.textContent = S.memoryFailedMutationsEnabled
                    ? (failedRecord ? memoryFailedAutoRetryHint(failedRecord) : "")
                    : "服务端未启用 G3KU_ENABLE_MEMORY_ADMIN_MUTATIONS，重试 / 放弃不可用";
            }
            U.memoryDetailActions.querySelectorAll("[data-memory-failed-action]").forEach((button) => {
                button.disabled = !S.memoryFailedMutationsEnabled || !!actionBusy || !failedRecord;
            });
        }
    }
    setDrawerOpen(U.memoryDetailBackdrop, U.memoryDetailDrawer, !!preview.open);
}

function closeMemoryDetailPreview() {
    S.memoryDetailPreview.open = false;
    renderMemoryDetailPreview();
}

function openMemoryDetailPreview(kind, key) {
    const normalizedKind = String(kind || "").trim();
    const normalizedKey = String(key || "").trim();
    if (!normalizedKind || !normalizedKey) return;
    const isProcessed = normalizedKind === "processed";
    const isFailed = normalizedKind === "failed";
    const source = isProcessed
        ? (Array.isArray(S.memoryProcessedItems) ? S.memoryProcessedItems.find((item) => String(item?.batch_id || "").trim() === normalizedKey) : null)
        : isFailed
            ? (Array.isArray(S.memoryFailedItems) ? S.memoryFailedItems.find((item) => String(item?.failed_id || "").trim() === normalizedKey) : null)
            : (Array.isArray(S.memoryQueueItems) ? S.memoryQueueItems.find((item) => String(item?.request_id || "").trim() === normalizedKey) : null);
    if (!source) return;
    const usage = source?.usage && typeof source.usage === "object" ? source.usage : {};
    const payloadTexts = Array.isArray(source?.payload_texts) ? source.payload_texts : [];
    const noopReason = isProcessed ? memoryProcessedNoopReason(source) : "";
    const failedUsage = isFailed && source?.usage_total && typeof source.usage_total === "object" ? source.usage_total : {};
    const fields = isProcessed
        ? [
            { label: "批次", value: normalizedKey },
            { label: "状态", value: memoryProcessedStatusLabel(source) || "-" },
            { label: "操作", value: memoryProcessedOpLabel(source) || "-" },
            { label: "处理时间", value: formatCompactTime(source?.processed_at) || String(source?.processed_at || "-") },
            { label: "模型链", value: memoryAttemptedChainText(source) },
            { label: "请求数", value: String(source?.request_count || payloadTexts.length || 0) },
            { label: "输入", value: String(usage.input_tokens || 0) },
            { label: "输出", value: String(usage.output_tokens || 0) },
        ]
        : isFailed
            ? [
                { label: "记录", value: normalizedKey },
                { label: "失败类别", value: memoryFailedCategoryLabel(source) },
                { label: "操作", value: memoryOpLabel(source?.op) },
                { label: "请求数", value: String(Array.isArray(source?.request_ids) ? source.request_ids.length : 0) },
                { label: "首次停车", value: formatCompactTime(source?.first_parked_at) || String(source?.first_parked_at || "-") },
                { label: "最近停车", value: formatCompactTime(source?.parked_at) || String(source?.parked_at || "-") },
                { label: "停车次数", value: String(source?.park_count || 0) },
                { label: "自动重排", value: String(source?.auto_requeue_count || 0) },
                { label: "手动重试", value: String(source?.manual_retry_count || 0) },
                { label: "累计输入", value: String(failedUsage.input_tokens || 0) },
                { label: "累计输出", value: String(failedUsage.output_tokens || 0) },
                { label: "重试策略", value: memoryFailedAutoRetryHint(source) },
            ]
            : [
                { label: "请求", value: normalizedKey },
                { label: "状态", value: memoryStatusLabel(source?.status) || "-" },
                { label: "入队时间", value: formatCompactTime(source?.created_at) || String(source?.created_at || "-") },
                { label: "开始处理", value: formatCompactTime(source?.processing_started_at) || String(source?.processing_started_at || "-") },
                { label: "决策源", value: String(source?.decision_source || "").trim() || "-" },
                { label: "触发来源", value: String(source?.trigger_source || "").trim() || "-" },
                { label: "下次重试", value: formatCompactTime(source?.retry_after) || String(source?.retry_after || "-") },
            ];
    S.memoryDetailPreview = {
        open: true,
        kind: normalizedKind,
        key: normalizedKey,
        title: isFailed ? "失败记忆详情" : "只读记忆详情",
        groups: isFailed
            ? [
                { title: "基础信息", items: fields.slice(0, 4) },
                { title: "停车与重试", items: fields.slice(4, 9) },
                { title: "累计成本与策略", items: fields.slice(9) },
            ]
            : [],
        subtitle: isProcessed
            ? `已处理批次 ${normalizedKey}`
            : isFailed
                ? `失败停车批次 ${normalizedKey}`
                : `队列请求 ${normalizedKey}`,
        fields,
        // 三面正文都按条给块（每块自带 request id、已显示区间与取全文按钮），
        // 正文只投服务端给的首屏，其余留在存储里原样不动。
        primaryBlocks: memoryPayloadBlocksFromRecord(source, normalizedKind),
        primaryText: isProcessed
            ? payloadTexts.join("\n\n---\n\n")
            : String(source?.payload_text || ""),
        secondaryTitle: isProcessed
            ? (noopReason ? "无变更原因" : "变更内容")
            : isFailed
                ? "错误历史"
                : "最近错误",
        secondaryText: isProcessed
            ? String(noopReason || memoryProcessedChangePreview(source) || "")
            : isFailed
                ? memoryFailedErrorHistoryText(source)
                : String(source?.last_error_text || ""),
        changes: isProcessed ? memoryProcessedStructuredChanges(source) : [],
        changesReconstructed: isProcessed ? Boolean(source?.changes_reconstructed) : false,
    };
    renderMemoryDetailPreview();
}

function ensureMemoryBrowserUi() {
    if (U.memoryBrowserBackdrop && U.memoryBrowserDrawer) return;
    const host = U.viewMemory || document.body;
    const backdrop = document.createElement("div");
    backdrop.id = "memory-browser-backdrop";
    backdrop.className = "detail-backdrop";
    backdrop.setAttribute("aria-hidden", "true");
    const drawer = document.createElement("section");
    drawer.id = "memory-browser-drawer";
    drawer.className = "panel detail-drawer memory-browser-drawer";
    drawer.setAttribute("role", "dialog");
    drawer.setAttribute("aria-modal", "true");
    drawer.setAttribute("aria-hidden", "true");
    drawer.setAttribute("aria-labelledby", "memory-browser-title");
    drawer.tabIndex = -1;
    drawer.innerHTML = `
        <div class="detail-modal-header">
            <div class="memory-browser-head">
                <h2 id="memory-browser-title">当前记忆</h2>
                <p id="memory-browser-subtitle" class="subtitle">只列仍在上下文里的记忆；已退场的看「归并记忆」。</p>
            </div>
            <div class="memory-browser-head-actions">
                <button type="button" class="toolbar-btn ghost" id="memory-browser-edit-toggle" title="进入编辑模式：批量选择、修改与删除记忆">编辑</button>
                <button type="button" class="toolbar-btn ghost" data-memory-browser-close data-modal-close>关闭</button>
            </div>
        </div>
        <div class="detail-modal-body">
            <div class="memory-browser-toolbar">
                <input id="memory-browser-search" class="resource-search memory-browser-search-input" type="search" placeholder="搜索关键词（内容 / ID / 来源）" aria-label="搜索当前记忆" />
            </div>
            <div id="memory-browser-bulk-bar" class="memory-browser-bulk-bar" hidden>
                <label class="memory-browser-select-all">
                    <input type="checkbox" class="ui-checkbox-input" id="memory-browser-select-all" aria-label="全选当前筛选结果" />
                    <span class="ui-checkbox-box" aria-hidden="true"></span>
                    <span>全选</span>
                </label>
                <span id="memory-browser-selected-count" class="memory-browser-selected-count">已选 0 条</span>
                <button type="button" id="memory-browser-bulk-delete" class="toolbar-btn danger memory-browser-bulk-delete" disabled>
                    <i data-lucide="trash-2" aria-hidden="true"></i>
                    删除选中
                </button>
            </div>
            <div class="memory-browser-table-wrap">
                <table class="memory-browser-table">
                    <thead>
                        <tr>
                            <th id="memory-browser-th-select" class="memory-browser-col-select" hidden></th>
                            <th class="sortable" data-memory-sort="created_at" tabindex="0" role="button">创建时间<span class="sort-ind" data-sort-ind="created_at"></span></th>
                            <th class="sortable" data-memory-sort="refresh_count" tabindex="0" role="button">刷新值<span class="sort-ind" data-sort-ind="refresh_count"></span></th>
                            <th class="sortable" data-memory-sort="passed_count" tabindex="0" role="button">通过次数<span class="sort-ind" data-sort-ind="passed_count"></span></th>
                            <th>来源</th>
                            <th>ID</th>
                            <th>记忆内容</th>
                            <th id="memory-browser-th-actions" class="memory-browser-col-actions" hidden>操作</th>
                        </tr>
                    </thead>
                    <tbody id="memory-browser-tbody"></tbody>
                </table>
            </div>
            <div id="memory-browser-status" class="resource-page-indicator" aria-live="polite"></div>
        </div>
        <div id="memory-browser-edit-backdrop" class="memory-browser-edit-backdrop" hidden aria-hidden="true"></div>
        <section id="memory-browser-edit-dialog" class="panel memory-browser-edit-dialog" role="dialog" aria-modal="true" aria-hidden="true" aria-labelledby="memory-browser-edit-title" tabindex="-1" hidden>
            <div class="detail-modal-header">
                <div class="memory-browser-head">
                    <h2 id="memory-browser-edit-title">编辑记忆</h2>
                    <p id="memory-browser-edit-subtitle" class="subtitle">保存前会再次确认；修改会同步重建 MEMORY.md 镜像。</p>
                </div>
                <button type="button" class="toolbar-btn ghost" data-memory-browser-edit-close>取消</button>
            </div>
            <div class="detail-modal-body memory-browser-edit-body">
                <label class="memory-browser-edit-field" for="memory-browser-edit-memory-id">
                    <span>记忆 ID</span>
                    <input id="memory-browser-edit-memory-id" type="text" readonly />
                </label>
                <label class="memory-browser-edit-field" for="memory-browser-edit-body">
                    <span>记忆内容</span>
                    <textarea id="memory-browser-edit-body" rows="6" placeholder="记忆正文（必填）"></textarea>
                </label>
                <label class="memory-browser-edit-field" for="memory-browser-edit-minimal">
                    <span>最小记忆（minimal_memory，留空即清空）</span>
                    <input id="memory-browser-edit-minimal" type="text" placeholder="条件->要求关键词" />
                </label>
            </div>
            <div class="detail-modal-footer memory-browser-edit-footer">
                <button type="button" class="toolbar-btn ghost" data-memory-browser-edit-cancel>取消</button>
                <button type="button" class="toolbar-btn success" data-memory-browser-edit-save>保存修改</button>
            </div>
        </section>
        <div id="memory-delete-backdrop" class="memory-browser-edit-backdrop" hidden aria-hidden="true"></div>
        <section id="memory-delete-dialog" class="panel memory-browser-edit-dialog memory-delete-dialog" role="dialog" aria-modal="true" aria-hidden="true" aria-labelledby="memory-delete-title" tabindex="-1" hidden>
            <div class="detail-modal-header">
                <div class="memory-browser-head">
                    <h2 id="memory-delete-title">删除记忆</h2>
                    <p id="memory-delete-subtitle" class="subtitle">删除后 MEMORY.md 镜像会同步重建，操作不可撤销。</p>
                </div>
                <button type="button" class="toolbar-btn ghost" data-memory-delete-close>取消</button>
            </div>
            <div class="detail-modal-body memory-delete-body">
                <p class="memory-delete-hint">笔记不在这里删除：被删记忆独占的笔记会在提交后由所有权回收自动清理，仍被其他记忆持有的保持原样。</p>
                <div id="memory-delete-notes" class="memory-delete-notes"></div>
            </div>
            <div class="detail-modal-footer memory-browser-edit-footer">
                <button type="button" class="toolbar-btn ghost" data-memory-delete-cancel>取消</button>
                <button type="button" class="toolbar-btn danger" data-memory-delete-confirm>
                    <i data-lucide="trash-2" aria-hidden="true"></i>
                    删除
                </button>
            </div>
        </section>
    `;
    host.appendChild(backdrop);
    host.appendChild(drawer);
    U.memoryBrowserBackdrop = backdrop;
    U.memoryBrowserDrawer = drawer;
    U.memoryBrowserTitle = drawer.querySelector("#memory-browser-title");
    U.memoryBrowserSubtitle = drawer.querySelector("#memory-browser-subtitle");
    U.memoryBrowserSearch = drawer.querySelector("#memory-browser-search");
    U.memoryBrowserTbody = drawer.querySelector("#memory-browser-tbody");
    U.memoryBrowserStatus = drawer.querySelector("#memory-browser-status");
    U.memoryBrowserClose = drawer.querySelector("[data-memory-browser-close]");
    U.memoryBrowserEditToggle = drawer.querySelector("#memory-browser-edit-toggle");
    U.memoryBrowserBulkBar = drawer.querySelector("#memory-browser-bulk-bar");
    U.memoryBrowserSelectAll = drawer.querySelector("#memory-browser-select-all");
    U.memoryBrowserSelectedCount = drawer.querySelector("#memory-browser-selected-count");
    U.memoryBrowserBulkDelete = drawer.querySelector("#memory-browser-bulk-delete");
    U.memoryBrowserThSelect = drawer.querySelector("#memory-browser-th-select");
    U.memoryBrowserThActions = drawer.querySelector("#memory-browser-th-actions");
    U.memoryBrowserEditBackdrop = drawer.querySelector("#memory-browser-edit-backdrop");
    U.memoryBrowserEditDialog = drawer.querySelector("#memory-browser-edit-dialog");
    U.memoryBrowserEditMemoryId = drawer.querySelector("#memory-browser-edit-memory-id");
    U.memoryBrowserEditBody = drawer.querySelector("#memory-browser-edit-body");
    U.memoryBrowserEditMinimal = drawer.querySelector("#memory-browser-edit-minimal");
    U.memoryBrowserEditSave = drawer.querySelector("[data-memory-browser-edit-save]");
    U.memoryBrowserClose?.addEventListener("click", () => closeMemoryBrowser());
    U.memoryBrowserBackdrop?.addEventListener("click", () => closeMemoryBrowser());
    U.memoryBrowserEditToggle?.addEventListener("click", () => toggleMemoryBrowserEditMode());
    U.memoryBrowserSelectAll?.addEventListener("change", () => {
        memoryBrowserToggleSelectAll(!!U.memoryBrowserSelectAll?.checked);
    });
    U.memoryBrowserBulkDelete?.addEventListener("click", () => requestMemoryBrowserDeleteSelected());
    U.memoryBrowserTbody?.addEventListener("change", (e) => {
        if (!(e.target instanceof Element)) return;
        const checkbox = e.target.closest("[data-memory-row-select]");
        if (!checkbox) return;
        memoryBrowserToggleRowSelected(checkbox.dataset.memoryRowSelect || "", !!checkbox.checked);
    });
    U.memoryBrowserTbody?.addEventListener("click", (e) => {
        if (!(e.target instanceof Element)) return;
        const noteTrigger = e.target.closest("[data-memory-note-ref]");
        if (noteTrigger) {
            e.preventDefault();
            e.stopPropagation();
            void openMemoryNotePreview(noteTrigger.dataset.memoryNoteRef || "");
            return;
        }
        const editTrigger = e.target.closest("[data-memory-row-edit]");
        if (editTrigger) {
            e.preventDefault();
            e.stopPropagation();
            openMemoryBrowserEditDialog(editTrigger.dataset.memoryRowEdit || "");
            return;
        }
        const deleteTrigger = e.target.closest("[data-memory-row-delete]");
        if (deleteTrigger) {
            e.preventDefault();
            e.stopPropagation();
            requestMemoryBrowserDelete([deleteTrigger.dataset.memoryRowDelete || ""].filter(Boolean), "single");
            return;
        }
        const warmTrigger = e.target.closest("[data-memory-row-uncompress]");
        if (warmTrigger) {
            e.preventDefault();
            e.stopPropagation();
            void runMemoryBrowserUncompress(warmTrigger.dataset.memoryRowUncompress || "");
        }
    });
    drawer.querySelectorAll("[data-memory-browser-edit-close], [data-memory-browser-edit-cancel]").forEach((button) => {
        button.addEventListener("click", () => closeMemoryBrowserEditDialog());
    });
    U.memoryBrowserEditBackdrop?.addEventListener("click", () => closeMemoryBrowserEditDialog());
    U.memoryBrowserEditSave?.addEventListener("click", () => requestMemoryBrowserEditSave());
    U.memoryBrowserDeleteDialog = drawer.querySelector("#memory-delete-dialog");
    U.memoryBrowserDeleteBackdrop = drawer.querySelector("#memory-delete-backdrop");
    U.memoryBrowserDeleteSubtitle = drawer.querySelector("#memory-delete-subtitle");
    U.memoryBrowserDeleteNotes = drawer.querySelector("#memory-delete-notes");
    U.memoryBrowserDeleteHint = drawer.querySelector(".memory-delete-hint");
    U.memoryBrowserDeleteConfirm = drawer.querySelector("[data-memory-delete-confirm]");
    drawer.querySelectorAll("[data-memory-delete-close], [data-memory-delete-cancel]").forEach((button) => {
        button.addEventListener("click", () => closeMemoryDeleteDialog());
    });
    U.memoryBrowserDeleteBackdrop?.addEventListener("click", () => closeMemoryDeleteDialog());
    U.memoryBrowserDeleteNotes?.addEventListener("click", (e) => {
        if (!(e.target instanceof Element)) return;
        const expandTrigger = e.target.closest("[data-delete-note-expand]");
        if (!expandTrigger) return;
        e.preventDefault();
        e.stopPropagation();
        void toggleDeleteNoteExpand(expandTrigger.dataset.deleteNoteExpand || "");
    });
    U.memoryBrowserDeleteConfirm?.addEventListener("click", () => confirmMemoryDeleteDialog());
    U.memoryBrowserSearch?.addEventListener("input", () => {
        S.memoryBrowser.search = U.memoryBrowserSearch.value || "";
        renderMemoryBrowserList();
    });
    drawer.querySelectorAll("[data-memory-sort]").forEach((th) => {
        const key = th.dataset.memorySort || "";
        const activate = () => toggleMemoryBrowserSort(key);
        th.addEventListener("click", activate);
        th.addEventListener("keydown", (e) => {
            if (e.key === "Enter" || e.key === " ") {
                e.preventDefault();
                activate();
            }
        });
    });
}

function memoryBrowserFilteredItems() {
    const items = Array.isArray(S.memoryBrowser.items) ? S.memoryBrowser.items : [];
    const needle = String(S.memoryBrowser.search || "").trim().toLowerCase();
    if (!needle) return items;
    return items.filter((item) => {
        const haystack = [
            item?.memory_body,
            item?.minimal_memory,
            item?.memory_id,
            item?.source,
        ].map((value) => String(value || "").toLowerCase()).join("\n");
        return haystack.includes(needle);
    });
}

function memoryBrowserSortedItems(items) {
    const key = String(S.memoryBrowser.sortKey || "created_at");
    const dir = S.memoryBrowser.sortDir === "asc" ? 1 : -1;
    const list = [...items];
    list.sort((a, b) => {
        if (key === "refresh_count" || key === "passed_count") {
            const av = Number(a?.[key] || 0);
            const bv = Number(b?.[key] || 0);
            if (av === bv) return 0;
            return av < bv ? -dir : dir;
        }
        const av = String(a?.[key] || "");
        const bv = String(b?.[key] || "");
        const cmp = av.localeCompare(bv);
        if (cmp === 0) return 0;
        return cmp < 0 ? -dir : dir;
    });
    return list;
}

function renderMemoryBrowserList() {
    ensureMemoryBrowserUi();
    if (!U.memoryBrowserTbody) return;
    const filtered = memoryBrowserSortedItems(memoryBrowserFilteredItems());
    const editMode = !!S.memoryBrowser.editMode && !!S.memoryBrowser.mutationsEnabled;
    const columnCount = editMode ? 8 : 6;
    if (S.memoryBrowser.busy) {
        setInnerHtmlPreservingScroll(U.memoryBrowserTbody, `<tr><td colspan="${columnCount}" class="memory-browser-empty">正在加载当前记忆...</td></tr>`);
    } else if (!filtered.length) {
        const hasAny = Array.isArray(S.memoryBrowser.items) && S.memoryBrowser.items.length;
        setInnerHtmlPreservingScroll(U.memoryBrowserTbody, `<tr><td colspan="${columnCount}" class="memory-browser-empty">${hasAny ? "没有匹配的记忆。" : "当前没有记忆。"}</td></tr>`);
    } else {
        setInnerHtmlPreservingScroll(U.memoryBrowserTbody, filtered.map((item) => {
            const createdAt = formatCompactTime(item?.created_at) || String(item?.created_at || "-");
            const source = String(item?.source || "").trim() || "-";
            const memoryId = String(item?.memory_id || "").trim() || "-";
            const body = String(item?.memory_body || "").trim() || "-";
            const ownershipNoteRef = String(item?.note_ref || "").trim();
            const selectableId = String(item?.memory_id || "").trim();
            const checked = !!S.memoryBrowser.selected[selectableId];
            const actionBusy = String(S.memoryBrowser.actionBusy || "").trim();
            const rowBusy = !!actionBusy;
            // input 被藏成 1px 且 pointer-events:none，可点的盒子是兄弟 span；
            // 没有 label 承接点击时这一行只能靠全选，单条永远点不中。
            const selectCell = editMode
                ? `<td class="memory-browser-cell-select"><label class="memory-browser-row-select"><input type="checkbox" class="ui-checkbox-input" data-memory-row-select="${esc(selectableId)}"${checked ? " checked" : ""}${rowBusy ? " disabled" : ""} aria-label="选择记忆 ${esc(memoryId)}" /><span class="ui-checkbox-box" aria-hidden="true"></span></label></td>`
                : "";
            const actionsCell = editMode
                ? `
                <td class="memory-browser-cell-actions">
                    <button type="button" class="toolbar-btn ghost memory-browser-row-btn" data-memory-row-edit="${esc(selectableId)}"${rowBusy ? " disabled" : ""} title="修改这条记忆">
                        <i data-lucide="pencil" aria-hidden="true"></i>
                        修改
                    </button>
                    <button type="button" class="toolbar-btn danger memory-browser-row-btn" data-memory-row-delete="${esc(selectableId)}"${rowBusy ? " disabled" : ""} title="删除这条记忆">
                        <i data-lucide="trash-2" aria-hidden="true"></i>
                        删除
                    </button>
                    ${item?.is_compressed ? `
                    <button type="button" class="toolbar-btn ghost memory-browser-row-btn" data-memory-row-uncompress="${esc(selectableId)}"${rowBusy ? " disabled" : ""} title="回暖：让这条记忆在上下文里恢复全文">
                        <i data-lucide="sunrise" aria-hidden="true"></i>
                        回暖
                    </button>` : ""}
                </td>
                `
                : "";
            return `
                <tr${checked ? ' class="memory-browser-row-selected"' : ""}>
                    ${selectCell}
                    <td class="memory-browser-cell-nowrap">${esc(createdAt)}</td>
                    <td class="memory-browser-cell-nowrap">${esc(String(item?.refresh_count ?? 0))}</td>
                    <td class="memory-browser-cell-nowrap">${esc(String(item?.passed_count ?? 0))}</td>
                    <td class="memory-browser-cell-nowrap">${esc(source)}</td>
                    <td class="memory-browser-cell-nowrap">${esc(memoryId)}</td>
                    <td class="memory-browser-cell-body">${renderMemoryTextWithNoteRefs(body)}${renderMemoryDowngradedChip(item?.is_compressed)}${renderMemoryNoteOwnershipChip(ownershipNoteRef)}</td>
                    ${actionsCell}
                </tr>
            `;
        }).join(""));
    }
    const total = Array.isArray(S.memoryBrowser.items) ? S.memoryBrowser.items.length : 0;
    if (U.memoryBrowserStatus) {
        const errorText = String(S.memoryBrowser.error || "").trim();
        if (errorText) {
            U.memoryBrowserStatus.textContent = errorText;
            U.memoryBrowserStatus.classList.add("is-error");
        } else {
            U.memoryBrowserStatus.textContent = `共 ${total} 条记忆${S.memoryBrowser.search ? `，筛选出 ${filtered.length} 条` : ""}`;
            U.memoryBrowserStatus.classList.remove("is-error");
        }
    }
    if (U.memoryBrowserSubtitle) {
        U.memoryBrowserSubtitle.textContent = `共 ${total} 条 · 点击「创建时间 / 刷新值 / 通过次数」表头可排序。`;
    }
    U.memoryBrowserDrawer?.querySelectorAll("[data-sort-ind]").forEach((ind) => {
        const key = ind.dataset.sortInd || "";
        const active = key === S.memoryBrowser.sortKey;
        ind.textContent = active ? (S.memoryBrowser.sortDir === "asc" ? " ↑" : " ↓") : "";
    });
    renderMemoryBrowserEditState();
    icons();
}

function memoryBrowserSelectedIds() {
    const selected = S.memoryBrowser.selected || {};
    const items = Array.isArray(S.memoryBrowser.items) ? S.memoryBrowser.items : [];
    const knownIds = new Set(items.map((item) => String(item?.memory_id || "").trim()).filter(Boolean));
    return Object.keys(selected).filter((id) => selected[id] && knownIds.has(id));
}

function renderMemoryBrowserEditState() {
    const editMode = !!S.memoryBrowser.editMode && !!S.memoryBrowser.mutationsEnabled;
    if (U.memoryBrowserThSelect) U.memoryBrowserThSelect.hidden = !editMode;
    if (U.memoryBrowserThActions) U.memoryBrowserThActions.hidden = !editMode;
    if (U.memoryBrowserBulkBar) U.memoryBrowserBulkBar.hidden = !editMode;
    if (U.memoryBrowserEditToggle) {
        U.memoryBrowserEditToggle.disabled = !S.memoryBrowser.mutationsEnabled;
        U.memoryBrowserEditToggle.textContent = editMode ? "完成" : "编辑";
        U.memoryBrowserEditToggle.title = S.memoryBrowser.mutationsEnabled
            ? (editMode ? "退出编辑模式" : "进入编辑模式：批量选择、修改与删除记忆")
            : "编辑不可用：服务端未启用 G3KU_ENABLE_MEMORY_ADMIN_MUTATIONS";
    }
    const selectedIds = memoryBrowserSelectedIds();
    const filtered = memoryBrowserSortedItems(memoryBrowserFilteredItems());
    if (U.memoryBrowserSelectAll) {
        const filteredIds = filtered.map((item) => String(item?.memory_id || "").trim()).filter(Boolean);
        U.memoryBrowserSelectAll.checked = filteredIds.length > 0 && filteredIds.every((id) => selectedIds.includes(id));
        U.memoryBrowserSelectAll.disabled = !editMode || !filteredIds.length || !!S.memoryBrowser.actionBusy;
    }
    if (U.memoryBrowserSelectedCount) {
        U.memoryBrowserSelectedCount.textContent = `已选 ${selectedIds.length} 条`;
    }
    if (U.memoryBrowserBulkDelete) {
        U.memoryBrowserBulkDelete.disabled = !editMode || !selectedIds.length || !!S.memoryBrowser.actionBusy;
    }
    if (U.memoryBrowserEditDialog) {
        const dialogOpen = !!S.memoryBrowser.editDialog.open;
        U.memoryBrowserEditDialog.hidden = !dialogOpen;
        U.memoryBrowserEditDialog.setAttribute("aria-hidden", dialogOpen ? "false" : "true");
        if (U.memoryBrowserEditBackdrop) {
            U.memoryBrowserEditBackdrop.hidden = !dialogOpen;
            U.memoryBrowserEditBackdrop.setAttribute("aria-hidden", dialogOpen ? "false" : "true");
        }
        if (U.memoryBrowserEditSave) U.memoryBrowserEditSave.disabled = !!S.memoryBrowser.editDialog.busy;
    }
}

function toggleMemoryBrowserEditMode() {
    if (!S.memoryBrowser.mutationsEnabled) {
        showToast({
            title: "编辑不可用",
            text: "服务端未启用记忆运维变更（G3KU_ENABLE_MEMORY_ADMIN_MUTATIONS）。",
            kind: "warn",
            durationMs: 5000,
        });
        return;
    }
    S.memoryBrowser.editMode = !S.memoryBrowser.editMode;
    S.memoryBrowser.selected = {};
    renderMemoryBrowserList();
}

function memoryBrowserToggleRowSelected(memoryId, checked) {
    const normalized = String(memoryId || "").trim();
    if (!normalized) return;
    const selected = { ...(S.memoryBrowser.selected || {}) };
    if (checked) {
        selected[normalized] = true;
    } else {
        delete selected[normalized];
    }
    S.memoryBrowser.selected = selected;
    renderMemoryBrowserList();
}

function memoryBrowserToggleSelectAll(checked) {
    const filtered = memoryBrowserSortedItems(memoryBrowserFilteredItems());
    const selected = { ...(S.memoryBrowser.selected || {}) };
    filtered.forEach((item) => {
        const id = String(item?.memory_id || "").trim();
        if (!id) return;
        if (checked) {
            selected[id] = true;
        } else {
            delete selected[id];
        }
    });
    S.memoryBrowser.selected = selected;
    renderMemoryBrowserList();
}

function requestMemoryBrowserDeleteSelected() {
    const selectedIds = memoryBrowserSelectedIds();
    if (!selectedIds.length) return;
    requestMemoryBrowserDelete(selectedIds, "bulk");
}

function requestMemoryBrowserDelete(memoryIds, scope = "bulk") {
    const ids = [...new Set((Array.isArray(memoryIds) ? memoryIds : [memoryIds]).map((id) => String(id || "").trim()).filter(Boolean))];
    if (!ids.length || S.memoryBrowser.actionBusy) return;
    if (!S.memoryBrowser.mutationsEnabled) {
        showToast({
            title: "删除不可用",
            text: "服务端未启用记忆运维变更（G3KU_ENABLE_MEMORY_ADMIN_MUTATIONS）。",
            kind: "warn",
            durationMs: 5000,
        });
        return;
    }
    openMemoryDeleteDialog(ids);
}

function openMemoryDeleteDialog(memoryIds) {
    const items = Array.isArray(S.memoryBrowser.items) ? S.memoryBrowser.items : [];
    const idSet = new Set(memoryIds);
    const refs = [];
    items.forEach((item) => {
        if (!idSet.has(String(item?.memory_id || "").trim())) return;
        // 归属读 `note_ref` 列：正文里的记号已被写闸禁止、启动时也被剥掉，扫文本恒得空集。
        const ref = String(item?.note_ref || "").trim();
        if (ref && !refs.includes(ref)) refs.push(ref);
    });
    S.memoryBrowser.deleteDialog = {
        open: true,
        memoryIds,
        busy: false,
        notes: refs.map((ref) => ({
            ref,
            expanded: false,
            body: "",
            loaded: false,
            loading: false,
        })),
    };
    renderMemoryDeleteDialog();
}

function closeMemoryDeleteDialog() {
    if (S.memoryBrowser.deleteDialog.busy) return;
    S.memoryBrowser.deleteDialog = { open: false, memoryIds: [], notes: [], busy: false };
    renderMemoryDeleteDialog();
}

function renderMemoryDeleteDialog() {
    ensureMemoryBrowserUi();
    const dialog = U.memoryBrowserDeleteDialog;
    const backdrop = U.memoryBrowserDeleteBackdrop;
    const state = S.memoryBrowser.deleteDialog || {};
    const open = !!state.open;
    if (dialog) {
        dialog.hidden = !open;
        dialog.setAttribute("aria-hidden", open ? "false" : "true");
    }
    if (backdrop) {
        backdrop.hidden = !open;
        backdrop.setAttribute("aria-hidden", open ? "false" : "true");
    }
    if (!open) return;
    const subtitle = U.memoryBrowserDeleteSubtitle;
    if (subtitle) {
        subtitle.textContent = `将删除 ${state.memoryIds.length} 条记忆；删除后 MEMORY.md 镜像会同步重建，操作不可撤销。`;
    }
    const notesHost = U.memoryBrowserDeleteNotes;
    const hasNotes = !!(state.notes || []).length;
    if (U.memoryBrowserDeleteHint) U.memoryBrowserDeleteHint.hidden = !hasNotes;
    if (notesHost) {
        // 这批记忆不持有 note 时整段不出现：没有动作也没有内容的段落只会把确认框撑高
        notesHost.innerHTML = hasNotes ? state.notes.map((note) => `
            <div class="memory-delete-note-row${note.expanded ? " is-expanded" : ""}">
                <span class="memory-delete-note-ref">${esc(note.ref)}</span>
                <button type="button" class="toolbar-btn ghost memory-delete-note-expand" data-delete-note-expand="${esc(note.ref)}" aria-expanded="${note.expanded ? "true" : "false"}">
                    ${note.expanded ? "收起" : "展开内容"}
                </button>
                ${note.expanded ? `<pre class="memory-delete-note-body">${note.loading ? "正在加载 note 正文..." : esc(String(note.body || "").trim() || "（空 note）")}</pre>` : ""}
            </div>
        `).join("") : "";
    }
    const confirmButton = U.memoryBrowserDeleteConfirm;
    if (confirmButton) confirmButton.disabled = !!state.busy;
    icons();
}

async function toggleDeleteNoteExpand(ref) {
    const state = S.memoryBrowser.deleteDialog;
    const note = (state.notes || []).find((item) => item.ref === ref);
    if (!note || note.loading) return;
    note.expanded = !note.expanded;
    if (note.expanded && !note.loaded) {
        note.loading = true;
        renderMemoryDeleteDialog();
        try {
            const item = await ApiClient.getMemoryNote(ref, memoryScopeQuery());
            note.body = String(item?.body || "");
            note.loaded = true;
        } catch (error) {
            note.body = `加载失败：${error?.message || "读取记忆 note 失败"}`;
            note.loaded = true;
        } finally {
            note.loading = false;
            renderMemoryDeleteDialog();
        }
        return;
    }
    renderMemoryDeleteDialog();
}

function confirmMemoryDeleteDialog() {
    const state = S.memoryBrowser.deleteDialog;
    if (!state.open || state.busy) return;
    state.busy = true;
    renderMemoryDeleteDialog();
    void runMemoryBrowserDelete(state.memoryIds);
}

function openMemoryBrowserEditDialog(memoryId) {
    const normalized = String(memoryId || "").trim();
    if (!normalized || S.memoryBrowser.actionBusy) return;
    if (!S.memoryBrowser.mutationsEnabled) {
        showToast({
            title: "修改不可用",
            text: "服务端未启用记忆运维变更（G3KU_ENABLE_MEMORY_ADMIN_MUTATIONS）。",
            kind: "warn",
            durationMs: 5000,
        });
        return;
    }
    const items = Array.isArray(S.memoryBrowser.items) ? S.memoryBrowser.items : [];
    const target = items.find((item) => String(item?.memory_id || "").trim() === normalized);
    if (!target) {
        showToast({ title: "未找到记忆", text: "该记忆可能已被删除，请刷新列表。", kind: "warn" });
        return;
    }
    S.memoryBrowser.editDialog = {
        open: true,
        memoryId: normalized,
        body: String(target?.memory_body || ""),
        minimal: String(target?.minimal_memory || ""),
        busy: false,
    };
    renderMemoryBrowserEditState();
    if (U.memoryBrowserEditMemoryId) U.memoryBrowserEditMemoryId.value = normalized;
    if (U.memoryBrowserEditBody) U.memoryBrowserEditBody.value = S.memoryBrowser.editDialog.body;
    if (U.memoryBrowserEditMinimal) U.memoryBrowserEditMinimal.value = S.memoryBrowser.editDialog.minimal;
    U.memoryBrowserEditBody?.focus();
}

function closeMemoryBrowserEditDialog() {
    if (S.memoryBrowser.editDialog.busy) return;
    S.memoryBrowser.editDialog = { ...S.memoryBrowser.editDialog, open: false };
    renderMemoryBrowserEditState();
}

function requestMemoryBrowserEditSave() {
    const dialog = S.memoryBrowser.editDialog || {};
    if (!dialog.open || dialog.busy) return;
    const memoryId = String(dialog.memoryId || "").trim();
    const body = String(U.memoryBrowserEditBody?.value ?? dialog.body ?? "");
    const minimal = String(U.memoryBrowserEditMinimal?.value ?? dialog.minimal ?? "");
    if (!memoryId) return;
    if (!body.trim()) {
        showToast({ title: "内容不能为空", text: "记忆内容为必填项。", kind: "warn" });
        return;
    }
    // 二次确认：内部弹窗确认后才真正提交修改
    openConfirm({
        title: "保存记忆修改",
        text: "确定保存对这条记忆的修改吗？保存后 MEMORY.md 镜像会同步重建。",
        confirmLabel: "保存",
        confirmKind: "danger",
        onConfirm: () => void runMemoryBrowserEditSave(memoryId, body, minimal),
    });
}

function toggleMemoryBrowserSort(key) {
    const normalized = String(key || "").trim();
    if (!normalized) return;
    if (S.memoryBrowser.sortKey === normalized) {
        S.memoryBrowser.sortDir = S.memoryBrowser.sortDir === "asc" ? "desc" : "asc";
    } else {
        S.memoryBrowser.sortKey = normalized;
        S.memoryBrowser.sortDir = normalized === "created_at" ? "desc" : "desc";
    }
    renderMemoryBrowserList();
}

function renderMemoryBrowser() {
    ensureMemoryBrowserUi();
    renderMemoryBrowserList();
    setDrawerOpen(U.memoryBrowserBackdrop, U.memoryBrowserDrawer, !!S.memoryBrowser.open);
}

function closeMemoryBrowser() {
    S.memoryBrowser.open = false;
    S.memoryBrowser.editMode = false;
    S.memoryBrowser.selected = {};
    S.memoryBrowser.editDialog = { open: false, memoryId: "", body: "", minimal: "", busy: false };
    renderMemoryBrowser();
}

async function loadMemoryBrowser() {
    ensureMemoryBrowserUi();
    S.memoryBrowser.busy = true;
    S.memoryBrowser.error = "";
    S.memoryBrowser.requestToken += 1;
    const requestToken = S.memoryBrowser.requestToken;
    renderMemoryBrowser();
    try {
        const payload = await ApiClient.getCurrentMemories(memoryScopeQuery());
        if (requestToken !== S.memoryBrowser.requestToken) return;
        S.memoryBrowser.items = Array.isArray(payload?.items) ? payload.items : [];
        S.memoryBrowser.total = normalizeInt(payload?.total, S.memoryBrowser.items.length);
        S.memoryBrowser.mutationsEnabled = !!payload?.mutationsEnabled;
        if (!S.memoryBrowser.mutationsEnabled) {
            S.memoryBrowser.editMode = false;
            S.memoryBrowser.selected = {};
            S.memoryBrowser.editDialog = { open: false, memoryId: "", body: "", minimal: "", busy: false };
        }
        S.memoryBrowser.error = "";
    } catch (error) {
        if (requestToken !== S.memoryBrowser.requestToken) return;
        S.memoryBrowser.error = error?.message || "当前记忆加载失败";
        showToast({ title: "记忆加载失败", text: S.memoryBrowser.error, kind: "error" });
    } finally {
        if (requestToken === S.memoryBrowser.requestToken) {
            S.memoryBrowser.busy = false;
            renderMemoryBrowser();
        }
    }
}

async function openMemoryBrowser() {
    ensureMemoryBrowserUi();
    S.memoryBrowser.open = true;
    renderMemoryBrowser();
    await loadMemoryBrowser();
}

function setMemoryCardExpanded(kind, key, expanded) {
    const target = kind === "processed" ? S.memoryProcessedExpanded : S.memoryQueueExpanded;
    if (!key) return;
    target[key] = !!expanded;
}

function setInnerHtmlPreservingScroll(el, html) {
    if (!el) return;
    if (el.innerHTML === html) return;
    const previousTop = el.scrollTop;
    const previousLeft = el.scrollLeft;
    el.innerHTML = html;
    el.scrollTop = previousTop;
    el.scrollLeft = previousLeft;
}

function renderMemoryAdminActions() {
    if (!U.memoryAdminActions) return;
    U.memoryAdminActions.hidden = true;
    U.memoryAdminActions.setAttribute("aria-hidden", "true");
    U.memoryAdminActions.innerHTML = "";
}

function renderMemoryMetaItem(label, value) {
    return `<span class="memory-card-meta-item"><span class="memory-card-meta-label">${esc(label)}</span><span class="memory-card-meta-value">${esc(value)}</span></span>`;
}

function renderMemoryView() {
    renderMemoryAdminActions();
    if (U.memoryQueueList) {
        if (S.memoryBusy && !S.memoryQueueItems.length) {
            setInnerHtmlPreservingScroll(U.memoryQueueList, '<div class="empty-state compact">正在加载记忆队列...</div>');
        } else if (!S.memoryQueueItems.length) {
            setInnerHtmlPreservingScroll(U.memoryQueueList, '<div class="empty-state compact">当前没有未出队记忆。</div>');
        } else {
            setInnerHtmlPreservingScroll(U.memoryQueueList, S.memoryQueueItems.map((item) => renderMemoryQueueCard(item)).join(""));
        }
    }
    if (U.memoryProcessedList) {
        if (S.memoryBusy && !S.memoryProcessedItems.length) {
            setInnerHtmlPreservingScroll(U.memoryProcessedList, '<div class="empty-state compact">正在加载已处理批次...</div>');
        } else if (!S.memoryProcessedItems.length) {
            setInnerHtmlPreservingScroll(U.memoryProcessedList, '<div class="empty-state compact">当前还没有已处理记忆。</div>');
        } else {
            setInnerHtmlPreservingScroll(U.memoryProcessedList, S.memoryProcessedItems.map((item) => renderMemoryProcessedCard(item)).join(""));
        }
    }
    // 失败记忆板块按需显示：仅当存在停车记录时出现，避免常态下挤占待处理队列
    const hasFailedMemories = S.memoryFailedTotal > 0 || S.memoryFailedItems.length > 0;
    if (U.memoryFailedPanel) U.memoryFailedPanel.hidden = !hasFailedMemories;
    if (U.memoryFailedList) {
        if (S.memoryBusy && !S.memoryFailedItems.length) {
            setInnerHtmlPreservingScroll(U.memoryFailedList, '<div class="empty-state compact">正在加载失败记忆...</div>');
        } else if (!S.memoryFailedItems.length) {
            setInnerHtmlPreservingScroll(U.memoryFailedList, '<div class="empty-state compact">没有失败停车的记忆。</div>');
        } else {
            setInnerHtmlPreservingScroll(U.memoryFailedList, S.memoryFailedItems.map((item) => renderMemoryFailedCard(item)).join(""));
        }
    }
    if (U.memoryQueueInfo) U.memoryQueueInfo.textContent = `共 ${S.memoryQueueTotal} 项`;
    if (U.memoryProcessedInfo) U.memoryProcessedInfo.textContent = `共 ${S.memoryProcessedTotal} 项`;
    if (U.memoryFailedInfo) U.memoryFailedInfo.textContent = `共 ${S.memoryFailedTotal} 项`;
    if (U.memoryQueueMore) U.memoryQueueMore.disabled = S.memoryBusy || !S.memoryQueueHasMore;
    if (U.memoryProcessedMore) U.memoryProcessedMore.disabled = S.memoryBusy || !S.memoryProcessedHasMore;
    if (U.memoryFailedMore) U.memoryFailedMore.disabled = S.memoryBusy || !S.memoryFailedHasMore;
    renderMemoryDetailPreview();
    icons();
}

function memoryQueueSourceLabel(item) {
    const decision = String(item?.decision_source || "").trim().toLowerCase();
    const trigger = String(item?.trigger_source || "").trim().toLowerCase();
    if (decision === "user") return "用户指令";
    if (trigger.startsWith("autonomous_review")) return "自动复核";
    if (trigger) return "工具调用";
    return "自动触发";
}

function renderMemoryQueueCard(item) {
    const requestId = String(item?.request_id || "").trim();
    const statusText = memoryStatusLabel(item?.status);
    const createdAt = formatCompactTime(item?.created_at) || String(item?.created_at || "");
    const body = String(item?.payload_text || "").replace(/\s+/g, " ").trim();
    const preview = body.length > MEMORY_QUEUE_BODY_CHARS
        ? `${body.slice(0, MEMORY_QUEUE_BODY_CHARS)}…`
        : body;
    return `
        <article class="memory-card memory-card-queue" data-memory-card="queue" data-memory-detail-open="queue" data-memory-detail-key="${esc(requestId)}" role="button" tabindex="0" aria-label="打开队列请求详情">
            <div class="memory-card-head">
                <div class="memory-card-minimal-status">
                    <span class="status-badge" data-status="${esc(String(item?.status || "pending"))}">${esc(statusText)}</span>
                </div>
                <span class="memory-card-source">${esc(memoryQueueSourceLabel(item))}</span>
                <span class="memory-card-time">${esc(createdAt || "-")}</span>
                <span class="memory-card-arrow" aria-hidden="true">›</span>
            </div>
            <p class="memory-card-body">${esc(preview || "（无正文）")}</p>
        </article>
    `;
}

function renderMemoryOpChips(kinds) {
    return kinds.map((kind) => {
        const { label, icon } = MEMORY_OP_KINDS[kind];
        return `<span class="memory-op-chip" data-op="${esc(kind)}" title="${esc(label)}"><i data-lucide="${icon}" aria-hidden="true"></i>${esc(label)}</span>`;
    }).join("");
}

function renderMemoryProcessedCard(item) {
    const batchId = String(item?.batch_id || "").trim();
    const statusLabel = memoryProcessedOpLabel(item);
    const opKinds = memoryProcessedOpKinds(item);
    const processedAt = formatCompactTime(item?.processed_at) || String(item?.processed_at || "");
    const statusSlot = opKinds.length
        ? renderMemoryOpChips(opKinds)
        : `<span class="memory-op-chip" data-op="nochange"><i data-lucide="minus" aria-hidden="true"></i>${esc(statusLabel)}</span>`;
    return `
        <article class="memory-card memory-card-compact" data-memory-card="processed" data-memory-detail-open="processed" data-memory-detail-key="${esc(batchId)}" role="button" tabindex="0" aria-label="打开已处理批次详情">
            <div class="memory-card-summary">
                <div class="memory-card-minimal-row">
                    <div class="memory-card-minimal-status">
                        ${statusSlot}
                    </div>
                    <div class="memory-card-minimal-trailing">
                        <span class="memory-card-time">${esc(processedAt || "-")}</span>
                        <span class="memory-card-arrow" aria-hidden="true">›</span>
                    </div>
                </div>
            </div>
        </article>
    `;
}

function requestMemoryFailedDiscard(failedId) {
    const target = String(failedId || "").trim();
    if (!target) return;
    openConfirm({
        title: "放弃失败记忆",
        text: "放弃后这批记忆将不再重试，并写入已处理历史的废弃终态记录。确定放弃吗？",
        confirmLabel: "放弃",
        confirmKind: "danger",
        onConfirm: () => void runMemoryFailedAction("discard", target),
    });
}

function memoryFailedErrorHistoryText(item) {
    const history = Array.isArray(item?.error_history) ? item.error_history : [];
    if (!history.length) {
        return String(item?.last_error_text || "").trim() || "没有记录到错误详情。";
    }
    return history.map((entry, index) => {
        const at = formatCompactTime(entry?.at) || String(entry?.at || "");
        if (String(entry?.event || "").trim() === "requeued") {
            const trigger = String(entry?.trigger || "").trim() === "manual" ? "手动重试" : "成功信号自动";
            return `#${index + 1} [${at}] 重新入队（${trigger}）`;
        }
        const category = memoryFailedCategoryLabel(entry);
        const trigger = String(entry?.trigger || "").trim();
        const triggerLabel = trigger === "manual" ? "手动重试后失败" : trigger === "exception" ? "运行时异常" : "处理失败";
        const errorText = String(entry?.error || "").trim() || "（无错误文本）";
        return `#${index + 1} [${at}] ${category} · ${triggerLabel}\n${errorText}`;
    }).join("\n\n");
}

function renderMemoryFailedCard(item) {
    const failedId = String(item?.failed_id || "").trim();
    const categoryLabel = memoryFailedCategoryLabel(item);
    const parkedAt = formatCompactTime(item?.parked_at) || String(item?.parked_at || "");
    const errorText = String(item?.last_error_text || "").trim();
    const requestCount = Array.isArray(item?.request_ids) ? item.request_ids.length : 0;
    const actionBusy = String(S.memoryFailedActionBusy || "").trim();
    const retryDisabled = !S.memoryFailedMutationsEnabled || (actionBusy && actionBusy.endsWith(`:${failedId}`));
    const retryTitle = S.memoryFailedMutationsEnabled
        ? `重试：重新入队尾等待处理（${memoryFailedAutoRetryHint(item)}）`
        : "重试不可用：服务端未启用 G3KU_ENABLE_MEMORY_ADMIN_MUTATIONS";
    return `
        <article class="memory-card memory-card-compact memory-card-failed" data-memory-card="failed" data-memory-detail-open="failed" data-memory-detail-key="${esc(failedId)}" role="button" tabindex="0" aria-label="打开失败记忆详情">
            <div class="memory-card-summary">
                <div class="memory-card-minimal-row">
                    <div class="memory-card-minimal-status">
                        <span class="status-badge" data-status="failed">失败</span>
                        <span class="memory-failed-category" title="${esc(memoryFailedAutoRetryHint(item))}">${esc(categoryLabel)}</span>
                        ${requestCount > 1 ? `<span class="policy-chip neutral">${esc(String(requestCount))} 条</span>` : ""}
                    </div>
                    <div class="memory-card-minimal-trailing">
                        <span class="memory-card-time">${esc(parkedAt || "-")}</span>
                        <button type="button" class="memory-failed-retry-btn" data-memory-failed-retry="${esc(failedId)}" title="${esc(retryTitle)}" aria-label="重试失败记忆 ${esc(failedId)}"${retryDisabled ? " disabled" : ""}>
                            <i data-lucide="rotate-ccw" aria-hidden="true"></i>
                        </button>
                        <span class="memory-card-arrow" aria-hidden="true">›</span>
                    </div>
                </div>
                ${errorText ? `<div class="memory-card-error">${esc(memoryPreviewText(errorText, 120))}</div>` : ""}
            </div>
        </article>
    `;
}
