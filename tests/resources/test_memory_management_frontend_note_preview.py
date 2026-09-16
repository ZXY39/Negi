from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from main.api import admin_rest


REPO_ROOT = Path(__file__).resolve().parents[2]


def _source(path: str) -> str:
    return (REPO_ROOT / path).read_text(encoding="utf-8")


def _fragment(source: str, start_marker: str, end_marker: str) -> str:
    start = source.index(start_marker)
    end = source.index(end_marker, start)
    return source[start:end]


def _admin_route_fragment(source: str, route: str) -> str:
    start = source.index(route)
    end = source.find("\n\n@router", start + 1)
    if end == -1:
        end = len(source)
    return source[start:end]


def test_memory_page_renders_note_ref_trigger() -> None:
    app_js = _source("g3ku/web/frontend/org_graph_app.js")
    css = _source("g3ku/web/frontend/org_graph.css")

    assert r"const NOTE_REF_RE = /(?:\bref:|见noteid:)(note_[a-z0-9_]+)\b/g;" in app_js
    assert "function renderMemoryTextWithNoteRefs(text)" in app_js
    assert "function renderMemoryNoteRefChip(noteRef)" in app_js
    assert 'class="memory-note-ref-trigger"' in app_js
    assert 'data-memory-note-ref="${esc(noteRef)}"' in app_js
    # 已处理批次详情（历史视图）打开的 note 窗只读：隐藏编辑开关且保存被守卫
    assert 'const readOnlyNote = String(S.memoryDetailPreview?.kind || "").trim() === "processed";' in app_js
    assert 'void openMemoryNotePreview(noteTrigger.dataset.memoryNoteRef || "", { editable: !readOnlyNote });' in app_js
    assert "U.memoryNoteEditToggle.hidden = !editable;" in app_js
    assert "!preview.editable" in app_js
    # note 窗必须置顶于查看记忆抽屉(100)/详情抽屉(96)之上，否则被遮挡
    assert "#memory-note-preview-drawer {" in css
    assert "z-index: 110;" in css
    assert "#memory-note-preview-backdrop {" in css
    assert "z-index: 105;" in css
    # 卡片精简为 minimal-row 后，note 引用渲染收敛到详情抽屉（正文 + 补充信息区），
    # 且轮询刷新必须保留滚动位置（变更内容滚动条被重置是回归 bug）
    assert "setInnerHtmlPreservingScroll(U.memoryDetailPrimary, renderMemoryTextWithNoteRefs(primaryText));" in app_js
    assert "setInnerHtmlPreservingScroll(U.memoryDetailSecondary, reconstructedHint + changeListHtml);" in app_js
    assert "const wantsSecondaryFirst = preview.kind === \"processed\" || preview.kind === \"failed\";" in app_js
    # 查看记忆表格的记忆内容列同样把 note 引用渲染为可点击按钮
    assert '<td class="memory-browser-cell-body">${renderMemoryTextWithNoteRefs(body)}</td>' in app_js
    assert 'U.memoryBrowserTbody?.addEventListener("click"' in app_js
    assert "function memoryProcessedChangePreview(item)" in app_js
    assert 'U.memoryQueueList?.addEventListener("click"' in app_js
    assert 'U.memoryProcessedList?.addEventListener("click"' in app_js
    assert "openMemoryNotePreview(noteTrigger.dataset.memoryNoteRef || \"\")" in app_js


def test_memory_page_note_preview_edit_is_confirmed_and_has_no_delete() -> None:
    app_js = _source("g3ku/web/frontend/org_graph_app.js")
    api_client_js = _source("g3ku/web/frontend/api_client.js")
    admin_rest_py = _source("main/api/admin_rest.py")
    preview_fragment = _fragment(
        app_js,
        "function ensureMemoryNotePreviewUi()",
        "function ensureMemoryDetailPreviewUi()",
    )

    # note 窗可编辑：编辑开关 + 编辑正文 textarea + 保存（保存前内部弹窗二次确认）
    assert "Note 预览" in preview_fragment
    assert "data-memory-note-edit-toggle" in preview_fragment
    assert "data-memory-note-edit-save" in preview_fragment
    assert 'id="memory-note-edit-body"' in preview_fragment
    assert "<textarea" in preview_fragment
    save_fragment = _fragment(app_js, "function requestMemoryNoteSave()", "async function runMemoryNoteSave(")
    assert "openConfirm({" in save_fragment
    assert 'ApiClient.updateMemoryNote(ref, body, "manual-ui")' in app_js
    assert "updateMemoryNote" in api_client_js
    assert "@router.post('/memory/notes/{ref}/update')" in admin_rest_py
    update_route = _admin_route_fragment(admin_rest_py, "@router.post('/memory/notes/{ref}/update')")
    assert "_append_memory_admin_audit_event(" in update_route
    assert "ApiClient.getMemoryNote(noteRef)" in app_js

    # note 面不提供任何删除入口
    assert "data-memory-note-delete" not in app_js
    assert "deleteMemoryNote" not in app_js
    assert "deleteMemoryNote" not in api_client_js


def test_memory_page_full_detail_preview_uses_centered_grouped_modal_and_scrollable_text_regions() -> None:
    app_js = _source("g3ku/web/frontend/org_graph_app.js")
    css = _source("g3ku/web/frontend/org_graph.css")
    # 详情抽屉本身保持只读；记忆浏览器（含受门控的编辑对话框）在独立函数中
    preview_fragment = _fragment(
        app_js,
        "function ensureMemoryDetailPreviewUi()",
        "function ensureMemoryBrowserUi()",
    )

    assert "只读记忆详情" in preview_fragment
    assert 'class="memory-detail-preview-shell"' in preview_fragment
    assert 'memory-detail-preview-group' in preview_fragment
    assert 'memory-detail-preview-group-body' in preview_fragment
    assert 'memory-detail-preview-text-block' in preview_fragment
    assert 'data-memory-detail-close' in preview_fragment
    assert "<textarea" not in preview_fragment
    assert "contenteditable" not in preview_fragment
    assert "saveMemoryDetail" not in app_js
    assert "updateMemoryDetail" not in app_js
    assert ".memory-detail-preview-drawer" in css
    assert "left: 50%;" in css
    assert "transform: translate(-50%, calc(-50% + 8px));" in css
    assert ".memory-detail-preview-group" in css
    assert ".memory-detail-preview-group-body" in css
    assert ".memory-detail-preview-text-block" in css
    assert "overflow-y: auto;" in css
    assert "linear-gradient" not in _fragment(
        css,
        ".memory-layout {",
        ".resource-section {",
    )


def test_memory_page_detail_group_titles_use_utf8_chinese_labels() -> None:
    app_js = _source("g3ku/web/frontend/org_graph_app.js")

    # 86b2a9d0 起已处理详情将原 基础信息/模型与用量 合并为单个 基础信息 分组
    assert "基础信息" in app_js
    assert "运行信息" in app_js
    assert "模型与用量" not in app_js


def test_memory_page_note_preview_admin_api_and_client_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    api_client_js = _source("g3ku/web/frontend/api_client.js")
    admin_rest_py = _source("main/api/admin_rest.py")
    route_fragment = _admin_route_fragment(admin_rest_py, "@router.get('/memory/notes/{ref}')")

    assert "static async getMemoryNote(ref)" in api_client_js
    assert "`/api/memory/notes/${encodeURIComponent(ref)}`" in api_client_js
    assert "memory_note_not_found" in api_client_js
    assert "未找到对应的记忆 note" in api_client_js
    assert "memory_note_read_failed" in api_client_js
    assert "读取记忆 note 失败" in api_client_js
    assert "@router.get('/memory/notes/{ref}')" in admin_rest_py
    assert "memory_note_unavailable" in route_fragment
    assert "memory_note_not_found" in route_fragment
    assert "memory_note_read_failed" in route_fragment
    assert "memory_note_invalid_ref" in route_fragment
    assert "load_note" in route_fragment
    assert "note_[a-z0-9_]+" in route_fragment

    class _StubMemoryManager:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def load_note(self, ref: str) -> str:
            self.calls.append(ref)
            if ref == "note_missing":
                raise FileNotFoundError("missing")
            return f"# {ref}\n详细 note 正文\n"

        def read_note(self, ref: str) -> str:
            raise AssertionError("get_memory_note should prefer load_note over read_note when available")

    stub_manager = _StubMemoryManager()
    monkeypatch.setattr(admin_rest, "_runtime_memory_manager", lambda: stub_manager)
    app = FastAPI()
    app.include_router(admin_rest.router, prefix="/api")
    client = TestClient(app)

    response = client.get("/api/memory/notes/note_policy")
    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "item": {
            "ref": "note_policy",
            "body": "# note_policy\n详细 note 正文\n",
        },
    }
    assert stub_manager.calls == ["note_policy"]

    missing = client.get("/api/memory/notes/note_missing")
    assert missing.status_code == 404
    assert missing.json()["detail"]["code"] == "memory_note_not_found"
    assert stub_manager.calls == ["note_policy", "note_missing"]

    invalid = client.get("/api/memory/notes/..\\..\\somewhere\\file")
    assert invalid.status_code == 400
    assert invalid.json()["detail"]["code"] == "memory_note_invalid_ref"
    assert stub_manager.calls == ["note_policy", "note_missing"]
