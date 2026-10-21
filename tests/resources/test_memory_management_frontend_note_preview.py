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



_MEMORY_JS_FILES = (
    "g3ku/web/frontend/org_graph_app.js",
    "g3ku/web/frontend/memory/store.js",
    "g3ku/web/frontend/memory/actions.js",
    "g3ku/web/frontend/memory/view.js",
)


def _memory_js() -> str:
    return "\n".join(_source(name) for name in _MEMORY_JS_FILES)


def _admin_route_fragment(source: str, route: str) -> str:
    start = source.index(route)
    end = source.find("\n\n@router", start + 1)
    if end == -1:
        end = len(source)
    return source[start:end]


def test_memory_page_renders_note_ref_trigger() -> None:
    app_js = _memory_js()
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
    # 查看记忆表格：正文里历史遗留的 ` ref:` 仍渲染成按钮，而当前行的归属来自
    # `note_ref` / `consolidated_into` 两列（常驻投影只给句柄，扫文本会漏掉关系）。
    # 降级标记排在归属前面：表体显示的是全文，这颗才说明上下文里现在只剩摘要。
    assert (
        '<td class="memory-browser-cell-body">'
        '${renderMemoryTextWithNoteRefs(body)}${renderMemoryDowngradedChip(item?.is_compressed)}'
        '${renderMemoryNoteOwnershipChip(ownershipNoteRef)}</td>'
    ) in app_js
    assert "function renderMemoryDowngradedChip(isCompressed)" in app_js
    assert ".memory-downgraded-chip {" in css
    assert "function renderMemoryNoteOwnershipChip(noteRef)" in app_js
    assert 'const ownershipNoteRef = String(item?.note_ref || "").trim();' in app_js
    assert 'U.memoryBrowserTbody?.addEventListener("click"' in app_js
    assert "function memoryProcessedChangePreview(item)" in app_js
    assert 'U.memoryQueueList?.addEventListener("click"' in app_js
    assert 'U.memoryProcessedList?.addEventListener("click"' in app_js
    assert "openMemoryNotePreview(noteTrigger.dataset.memoryNoteRef || \"\")" in app_js


def test_memory_page_note_preview_edit_is_confirmed_and_has_no_delete() -> None:
    app_js = _memory_js()
    api_client_js = _source("g3ku/web/frontend/client_api/memory.js")
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
    save_fragment = _fragment(app_js, "function requestMemoryNoteSave()", "function closeMemoryNotePreview(")
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
    app_js = _memory_js()
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
    app_js = _memory_js()

    # 86b2a9d0 起已处理详情将原 基础信息/模型与用量 合并为单个 基础信息 分组
    assert "基础信息" in app_js
    assert "运行信息" in app_js
    assert "模型与用量" not in app_js


def test_memory_page_note_preview_admin_api_and_client_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    api_client_js = "\n".join(
        _source(name)
        for name in (
            "g3ku/web/frontend/client_api/core.js",
            "g3ku/web/frontend/client_api/memory.js",
        )
    )
    admin_rest_py = _source("main/api/admin_rest.py")
    route_fragment = _admin_route_fragment(admin_rest_py, "@router.get('/memory/notes/{ref}')")

    assert "async getMemoryNote(ref)" in api_client_js
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


def test_memory_note_read_endpoint_admits_the_resident_handle(monkeypatch: pytest.MonkeyPatch) -> None:
    """常驻行现在只给句柄，预览端点必须收句柄；写端点仍只认实名。

    形状闸门在这一层：句柄进得去，穿越串照旧 400。名字→句柄的解析交给
    `resolve_note_ref`，响应回实名——前端拿句柄回显再去保存会撞只认实名的写端点。
    """
    class _StubMemoryManager:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def resolve_note_ref(self, ref: str) -> str:
            return "note_ports" if ref == "n106dd" else ref

        def load_note(self, ref: str) -> str:
            self.calls.append(ref)
            return "# note_ports 端口正文"

    stub_manager = _StubMemoryManager()
    monkeypatch.setattr(admin_rest, "_runtime_memory_manager", lambda: stub_manager)
    app = FastAPI()
    app.include_router(admin_rest.router, prefix="/api")
    client = TestClient(app)

    response = client.get("/api/memory/notes/n106dd")
    assert response.status_code == 200
    assert response.json()["item"]["ref"] == "note_ports"
    assert stub_manager.calls == ["note_ports"]

    # 归并区那行给的是 c 前缀的分类句柄，形状闸门两种都要放过
    category = client.get("/api/memory/notes/c106dd")
    assert category.status_code == 200

    invalid = client.get("/api/memory/notes/n106dd/../x")
    assert invalid.status_code == 400
    assert invalid.json()["detail"]["code"] == "memory_note_invalid_ref"
    assert stub_manager.calls == ["note_ports", "c106dd"]


def test_consolidation_note_browser_wires_the_read_only_lane() -> None:
    html = _source("g3ku/web/frontend/org_graph.html")
    app_js = _memory_js()
    api_js = _source("g3ku/web/frontend/client_api/memory.js")
    admin_py = _source("main/api/admin_rest.py")
    runtime_py = _source("g3ku/agent/memory_agent_runtime.py")

    # 入口在「查看记忆」旁边，读的是同一份 sqlite 派生出的归并区分类
    assert '<button id="memory-index-notes-btn" class="toolbar-btn ghost" type="button">归并记忆</button>' in html
    assert 'memoryIndexNotes: document.getElementById("memory-index-notes-btn")' in app_js
    assert 'U.memoryIndexNotes?.addEventListener("click", () => void openMemoryNoteBrowser());' in app_js
    assert '"memory:index-notes"' in api_js
    assert '"/api/memory/index-notes"' in api_js

    # 列表与正文共用一个窗：index/note 两种形态在同一个 DOM 槽位里换，不叠第二层抽屉
    assert '<div id="memory-note-index-list" class="memory-note-index-list" hidden></div>' in app_js
    assert '<nav id="memory-note-preview-trail" class="memory-note-preview-trail"' in app_js
    assert 'const isIndex = String(preview.mode || "note") === "index";' in app_js
    # 轮询/重绘必须保住滚动位置（变更内容滚动条被重置是回归 bug）
    assert "setInnerHtmlPreservingScroll(U.memoryNoteIndexList, renderMemoryNoteIndexListHtml(preview));" in app_js
    assert "setInnerHtmlPreservingScroll(" in app_js
    # 正文里的下一级：句柄形状与旧标记都渲染成可点令牌，其余文本一律转义
    assert "renderMemoryTextWithNoteTokens(bodyText)" in app_js
    # 浏览层整条只读：分类 note 没有管理面写路径，编辑开关留着就是点了必失败的假门
    assert 'const readOnly = { editable: false, origin: "index" };' in app_js

    # 只读面：端点不调任何写方法，断链的分类也照样列出（少一类比多一类更难查）
    route = _admin_route_fragment(admin_py, "@router.get('/memory/index-notes')")
    assert "list_consolidation_categories" in route
    for writer in ("enqueue_operator_request", "uncompress_current_memory", "update_note", "apply_batch"):
        assert writer not in route

    # 句柄分两棵树：`n` 是 memory/notes/，`c` 是 memory/index/，闸门与前端令牌都要认两种
    assert 'MEMORY_NOTE_HANDLE_RE = re.compile(r"^[nc][0-9a-f]{5}$")' in admin_py
    assert "[nc][0-9a-f]{5}" in app_js

    assert "def list_consolidation_categories(self)" in runtime_py
    # 常驻行仍只渲染可用分类：浏览面放宽不能顺手把断链印进上下文
    assert 'if not entry["unusable"]' in runtime_py
