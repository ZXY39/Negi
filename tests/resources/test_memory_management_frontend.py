from __future__ import annotations

import html as html_module
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


def _memory_view_fragment(source: str) -> str:
    start = source.index('<section id="view-memory"')
    end = source.index('<section id="view-models"', start)
    return source[start:end]


def _admin_route_fragment(source: str, route: str) -> str:
    start = source.index(route)
    end = source.find("\n\n@router", start + 1)
    if end == -1:
        end = len(source)
    return source[start:end]


def test_memory_management_view_uses_page_level_error_and_blocked_banners() -> None:
    html_source = (REPO_ROOT / "g3ku/web/frontend/org_graph.html").read_text(encoding="utf-8")
    memory_html = _memory_view_fragment(html_source)
    rendered_memory_html = html_module.unescape(memory_html)

    assert "项目解锁" in html_source
    assert "输入口令后才能进入项目主界面与启动后台能力。" in html_source
    assert "Leader 会话" in html_source
    assert "记忆管理" in rendered_memory_html
    assert "查看未出队记忆队列与已成功处理批次。当前页面默认只读，不开放副作用运维按钮。" in rendered_memory_html
    assert 'id="memory-refresh-btn"' in memory_html
    assert "刷新" in rendered_memory_html
    assert 'id="memory-page-error-banner"' in memory_html
    assert 'id="memory-queue-blocked-banner"' in memory_html
    assert "队首阻塞整个队列" in rendered_memory_html

    blocked_index = memory_html.index('id="memory-queue-blocked-banner"')
    error_index = memory_html.index('id="memory-page-error-banner"')
    queue_column_index = memory_html.index('<section class="resource-detail-panel memory-column">')

    assert blocked_index < queue_column_index
    assert error_index < queue_column_index
    assert 'id="memory-error-banner"' not in memory_html


def test_memory_management_view_preserves_expand_state_and_memory_only_auto_refresh() -> None:
    app_js = (REPO_ROOT / "g3ku/web/frontend/org_graph_app.js").read_text(encoding="utf-8")

    assert '{ key: "memory", label: "记忆" }' in app_js
    assert 'viewMemory: document.getElementById("view-memory")' in app_js
    assert 'memory: U.viewMemory' in app_js
    assert "MEMORY_VIEW_POLL_MS = 15000" in app_js
    assert "function startMemoryViewAutoRefresh()" in app_js
    assert "function stopMemoryViewAutoRefresh()" in app_js
    assert 'if (S.view !== "memory") return;' in app_js
    assert 'void loadMemoryView({ quiet: true });' in app_js
    assert 'if (view === "memory") startMemoryViewAutoRefresh();' in app_js
    assert 'else stopMemoryViewAutoRefresh();' in app_js
    assert "function bindMemoryCardToggles()" in app_js
    assert 'querySelectorAll("details[data-memory-card=\'queue\']")' in app_js
    assert 'querySelectorAll("details[data-memory-card=\'processed\']")' in app_js
    assert 'card.dataset.memoryToggleBound = "1";' in app_js
    assert 'setMemoryCardExpanded("queue", card.dataset.memoryKey || "", card.open);' in app_js
    assert 'setMemoryCardExpanded("processed", card.dataset.memoryKey || "", card.open);' in app_js
    assert "bindMemoryCardToggles();" in app_js
    assert "memoryPageErrorBanner" in app_js
    assert "memoryQueueBlockedBanner" in app_js
    assert 'U.memoryPageErrorBanner.hidden = !hasError;' in app_js
    assert 'U.memoryQueueBlockedBanner.hidden = !blockedText;' in app_js
    assert "状态：" in app_js
    assert "入队时间：" in app_js
    assert "开始处理：" in app_js
    assert "最近错误：" in app_js
    assert "最近报错时间：" in app_js
    assert "下次重试：" in app_js


def test_memory_management_view_uses_read_only_queue_endpoints_with_safe_error_messages() -> None:
    api_client_js = (REPO_ROOT / "g3ku/web/frontend/api_client.js").read_text(encoding="utf-8")
    admin_rest_py = (REPO_ROOT / "main/api/admin_rest.py").read_text(encoding="utf-8")
    queue_route = _admin_route_fragment(admin_rest_py, "@router.get('/memory/queue')")
    processed_route = _admin_route_fragment(admin_rest_py, "@router.get('/memory/processed')")

    assert "getMemoryQueue" in api_client_js
    assert '"/api/memory/queue"' in api_client_js
    assert "getMemoryProcessed" in api_client_js
    assert '"/api/memory/processed"' in api_client_js
    assert "memory_queue_read_failed" in api_client_js
    assert "记忆队列暂时不可读取，请稍后刷新。" in api_client_js
    assert "memory_processed_read_failed" in api_client_js
    assert "已处理记忆暂时不可读取，请稍后刷新。" in api_client_js
    assert "memory_manager_unavailable" in api_client_js
    assert "记忆服务暂不可用，请稍后刷新。" in api_client_js
    assert "memory_queue_unavailable" in api_client_js
    assert "记忆队列暂不可用，请稍后刷新。" in api_client_js
    assert "memory_processed_unavailable" in api_client_js
    assert "已处理记忆列表暂不可用，请稍后刷新。" in api_client_js

    assert "@router.get('/memory/queue')" in admin_rest_py
    assert "@router.get('/memory/processed')" in admin_rest_py
    assert "memory_queue_read_failed" in queue_route
    assert "memory_processed_read_failed" in processed_route
    assert "记忆队列暂时不可读取，请稍后刷新。" in queue_route
    assert "已处理记忆暂时不可读取，请稍后刷新。" in processed_route
    assert "detail=str(exc)" not in queue_route
    assert "detail=str(exc)" not in processed_route


def test_memory_management_view_keeps_admin_mutations_hidden_by_default() -> None:
    html_source = (REPO_ROOT / "g3ku/web/frontend/org_graph.html").read_text(encoding="utf-8")
    memory_html = _memory_view_fragment(html_source)
    app_js = (REPO_ROOT / "g3ku/web/frontend/org_graph_app.js").read_text(encoding="utf-8")

    assert 'id="memory-admin-actions"' in memory_html
    assert 'id="memory-admin-actions" hidden aria-hidden="true"' in memory_html
    assert "当前页面默认只读，不开放副作用运维按钮。" in html_module.unescape(memory_html)
    assert "data-memory-admin-action" not in memory_html

    assert 'memoryAdminActions: document.getElementById("memory-admin-actions")' in app_js
    assert "function renderMemoryAdminActions()" in app_js
    assert "U.memoryAdminActions.hidden = true;" in app_js
    assert 'U.memoryAdminActions.setAttribute("aria-hidden", "true");' in app_js
    assert 'U.memoryAdminActions.innerHTML = "";' in app_js
    assert "renderMemoryAdminActions();" in app_js
    assert "data-memory-admin-action" not in app_js
    assert '"/api/memory/admin/retry-head"' not in app_js
    assert "/api/memory/admin/retry-head" not in html_source
