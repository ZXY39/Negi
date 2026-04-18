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

    assert r"const NOTE_REF_RE = /\bref:(note_[a-z0-9_]+)\b/g;" in app_js
    assert "function renderMemoryTextWithNoteRefs(text)" in app_js
    assert "function renderMemoryNoteRefChip(noteRef)" in app_js
    assert 'class="memory-note-ref-trigger"' in app_js
    assert 'data-memory-note-ref="${esc(noteRef)}"' in app_js
    assert "renderMemoryTextWithNoteRefs(String(item?.payload_text || \"\"))" in app_js
    assert "renderMemoryTextWithNoteRefs(String(item?.document_preview || \"\"))" in app_js
    assert "renderMemoryTextWithNoteRefs(payloadTexts.join(\"\\n\\n---\\n\\n\"))" in app_js
    assert "renderMemoryNoteRefList(noteRefs)" in app_js
    assert 'U.memoryQueueList?.addEventListener("click"' in app_js
    assert 'U.memoryProcessedList?.addEventListener("click"' in app_js
    assert "openMemoryNotePreview(trigger.dataset.memoryNoteRef || \"\")" in app_js


def test_memory_page_keeps_note_preview_read_only() -> None:
    app_js = _source("g3ku/web/frontend/org_graph_app.js")
    preview_fragment = _fragment(
        app_js,
        "function ensureMemoryNotePreviewUi()",
        "function setMemoryCardExpanded(",
    )

    assert "只读 Note 预览" in preview_fragment
    assert "仅展示 note 正文，不支持编辑或保存。" in preview_fragment
    assert 'class="memory-note-preview-shell"' in preview_fragment
    assert 'class="memory-note-preview-body"' in preview_fragment
    assert 'data-memory-note-close' in preview_fragment
    assert "<textarea" not in preview_fragment
    assert "contenteditable" not in preview_fragment
    assert "saveMemoryNote" not in app_js
    assert "updateMemoryNote" not in app_js
    assert "ApiClient.getMemoryNote(noteRef)" in app_js


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
