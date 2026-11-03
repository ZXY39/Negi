"use strict";

// 模型页「安全审查词替换」：词表由操作员手动维护，一行一个词。
// 这个入口不参与 renderAll——模型页的渲染方是 org_graph_llm.js，重绘会覆盖这里的绑定。
(() => {
    const SOURCE_URL = "https://github.com/konsheng/Sensitive-lexicon";
    const EXPORT_FILENAME = "negi-content-policy.txt";
    const LARGE_WORD_LIST_THRESHOLD = 5000;

    const state = {
        loaded: false,
        enabled: false,
        words: [],
        updatedAt: "",
    };

    const elements = {};

    function parseWordList(text) {
        const seen = new Set();
        const words = [];
        String(text ?? "")
            .replace(/\ufeff/g, "")
            .split(/\r?\n/)
            .forEach((line) => {
                const word = line.trim();
                if (!word) return;
                const folded = word.toLowerCase();
                if (seen.has(folded)) return;
                seen.add(folded);
                words.push(word);
            });
        return words;
    }

    function formatWordList(words) {
        const list = Array.isArray(words) ? words.filter((item) => typeof item === "string") : [];
        return list.length ? `${list.join("\n")}\n` : "";
    }

    function bindElements() {
        elements.button = document.getElementById("content-policy-btn");
        elements.backdrop = document.getElementById("content-policy-backdrop");
        elements.dialog = document.getElementById("content-policy-dialog");
        elements.closeBtn = document.getElementById("content-policy-close-btn");
        elements.cancelBtn = document.getElementById("content-policy-cancel-btn");
        elements.saveBtn = document.getElementById("content-policy-save-btn");
        elements.enabledInput = document.getElementById("content-policy-enabled");
        elements.wordsInput = document.getElementById("content-policy-words");
        elements.countHint = document.getElementById("content-policy-count");
        elements.warningHint = document.getElementById("content-policy-warning");
        elements.importBtn = document.getElementById("content-policy-import-btn");
        elements.importInput = document.getElementById("content-policy-import-input");
        elements.exportBtn = document.getElementById("content-policy-export-btn");
        elements.clearBtn = document.getElementById("content-policy-clear-btn");
    }

    function draftWords() {
        return parseWordList(elements.wordsInput?.value || "");
    }

    function renderDraftStatus() {
        const words = draftWords();
        if (elements.countHint) {
            elements.countHint.textContent = `${words.length} 个词`;
        }
        const shortWords = words.filter((word) => word.length === 1);
        const warnings = [];
        if (shortWords.length) {
            warnings.push(`含 ${shortWords.length} 个单字符词（如 ${shortWords.slice(0, 3).join("、")}）：单字词会命中大量内容，替换后语义可能失真。`);
        }
        if (words.length > LARGE_WORD_LIST_THRESHOLD) {
            warnings.push(`词表有 ${words.length} 个词：匹配成本会明显上升，建议只导入分类子集。`);
        }
        if (elements.warningHint) {
            elements.warningHint.textContent = warnings.join(" ");
            elements.warningHint.hidden = warnings.length === 0;
        }
        return words;
    }

    function applyPolicyToForm(policy) {
        state.enabled = !!policy?.enabled;
        state.words = Array.isArray(policy?.words) ? [...policy.words] : [];
        state.updatedAt = String(policy?.updated_at || "");
        state.loaded = true;
        if (elements.enabledInput) elements.enabledInput.checked = state.enabled;
        if (elements.wordsInput) elements.wordsInput.value = formatWordList(state.words);
        renderDraftStatus();
    }

    function openDialog() {
        if (elements.backdrop) {
            elements.backdrop.hidden = false;
            elements.backdrop.classList.add("is-open");
        }
        renderDraftStatus();
        if (typeof window.icons === "function") window.icons();
        void loadPolicy();
    }

    function closeDialog() {
        if (elements.backdrop) {
            elements.backdrop.hidden = true;
            elements.backdrop.classList.remove("is-open");
        }
    }

    async function loadPolicy() {
        try {
            const policy = await ApiClient.getContentPolicy();
            applyPolicyToForm(policy);
        } catch (error) {
            showToast({ title: "读取词表失败", text: error?.message || "读取词表失败", kind: "error" });
        }
    }

    async function savePolicy() {
        const words = renderDraftStatus();
        const enabled = !!elements.enabledInput?.checked;
        try {
            const policy = await ApiClient.saveContentPolicy({ enabled, words });
            applyPolicyToForm(policy);
            showToast({
                title: enabled ? "安全审查词替换已启用" : "安全审查词替换已停用",
                text: `词表 ${(policy?.words || []).length} 个词，对后续请求生效。`,
            });
            closeDialog();
        } catch (error) {
            showToast({ title: "保存失败", text: error?.message || "保存失败", kind: "error" });
        }
    }

    async function importFile(file) {
        if (!file) return;
        try {
            const text = await file.text();
            if (elements.wordsInput) elements.wordsInput.value = text;
            renderDraftStatus();
        } catch (error) {
            showToast({ title: "导入失败", text: error?.message || "导入失败", kind: "error" });
        }
    }

    function exportWords() {
        const words = draftWords();
        const blob = new Blob([formatWordList(words)], { type: "text/plain;charset=utf-8" });
        const url = URL.createObjectURL(blob);
        const anchor = document.createElement("a");
        anchor.href = url;
        anchor.download = EXPORT_FILENAME;
        document.body.appendChild(anchor);
        anchor.click();
        anchor.remove();
        URL.revokeObjectURL(url);
    }

    function clearWords() {
        if (typeof window.requestInlineConfirm === "function") {
            void window.requestInlineConfirm({
                title: "清空词表",
                text: "清空后保存即不再替换任何词（启用开关保持不变）。",
                confirmLabel: "清空",
                confirmKind: "danger",
            }).then((result) => {
                if (!result?.confirmed) return;
                if (elements.wordsInput) elements.wordsInput.value = "";
                renderDraftStatus();
            });
            return;
        }
        if (elements.wordsInput) elements.wordsInput.value = "";
        renderDraftStatus();
    }

    function bindEvents() {
        elements.button?.addEventListener("click", () => openDialog());
        elements.closeBtn?.addEventListener("click", () => closeDialog());
        elements.cancelBtn?.addEventListener("click", () => closeDialog());
        elements.saveBtn?.addEventListener("click", () => void savePolicy());
        elements.importBtn?.addEventListener("click", () => elements.importInput?.click());
        elements.importInput?.addEventListener("change", (event) => {
            const file = event.target?.files?.[0];
            void importFile(file);
            event.target.value = "";
        });
        elements.exportBtn?.addEventListener("click", () => exportWords());
        elements.clearBtn?.addEventListener("click", () => clearWords());
        elements.wordsInput?.addEventListener("input", () => renderDraftStatus());
        elements.backdrop?.addEventListener("click", (event) => {
            if (event.target === elements.backdrop) closeDialog();
        });
        document.addEventListener("keydown", (event) => {
            if (event.key !== "Escape") return;
            if (elements.backdrop?.hidden) return;
            closeDialog();
        });
    }

    function boot() {
        bindElements();
        bindEvents();
    }

    window.ContentPolicy = {
        parseWordList,
        formatWordList,
        openDialog,
        closeDialog,
        savePolicy,
        sourceUrl: SOURCE_URL,
        state,
    };

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", boot);
    } else {
        boot();
    }
})();
