"""日志审计前端契约测试：导航按钮/角标结构、view-audit 区块、JS 职责、api_client 与端点钉约。"""

from pathlib import Path

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


def test_audit_nav_button_carries_badge_before_spacer() -> None:
    html = _source("g3ku/web/frontend/org_graph.html")

    assert 'data-view="audit"' in html
    assert "日志审计" in html
    assert 'id="audit-nav-badge"' in html
    assert 'class="nav-badge"' in html
    # 角标初始隐藏；导航按钮位于 sidebar-spacer 之前
    assert html.index('data-view="audit"') < html.index('class="sidebar-spacer"')
    nav_fragment = _fragment(html, 'data-view="audit"', "sidebar-spacer")
    assert "hidden" in nav_fragment
    assert 'data-lucide="scroll-text"' in nav_fragment


def test_view_audit_section_sits_between_external_and_task_details() -> None:
    html = _source("g3ku/web/frontend/org_graph.html")

    assert (
        html.index('id="view-external"')
        < html.index('id="view-audit"')
        < html.index('id="view-task-details"')
    )
    section = _fragment(html, 'id="view-audit"', 'id="view-task-details"')
    # 健康卡 + 事件流 + 刷新 + 级别筛选 + 加载更多
    assert 'id="audit-summary-grid"' in section
    assert 'id="audit-event-list"' in section
    assert 'id="audit-refresh-btn"' in section
    assert 'id="audit-level-filters"' in section
    assert 'id="audit-event-more-btn"' in section
    assert 'id="audit-event-info"' in section
    for chip in ("全部", "错误", "警告", "信息"):
        assert chip in section
    assert "记忆错误仍在记忆管理中查看" in section


def test_view_audit_does_not_duplicate_memory_surface() -> None:
    html = _source("g3ku/web/frontend/org_graph.html")
    audit_section = _fragment(html, 'id="view-audit"', 'id="view-task-details"')
    # 记忆错误面板/列表id 不得进入审计视图——记忆错误只存在于记忆板块
    assert "memory-failed-list" not in audit_section
    assert "memory-failed-panel" not in audit_section


def test_audit_js_constants_state_and_ui_cache() -> None:
    app_js = _source("g3ku/web/frontend/org_graph_app.js")

    assert 'AUDIT_LAST_SEEN_KEY = "g3ku.audit.last-seen.v1"' in app_js
    assert "AUDIT_VIEW_POLL_MS = 15000" in app_js
    assert "AUDIT_BADGE_POLL_MS = 30000" in app_js
    assert "AUDIT_PAGE_SIZE = 30" in app_js
    assert "auditLoadedOnce: false" in app_js
    assert "auditBadgePollIntervalId: null" in app_js
    assert 'viewAudit: document.getElementById("view-audit")' in app_js
    assert 'auditNavBadge: document.getElementById("audit-nav-badge")' in app_js
    assert "audit: U.viewAudit" in app_js


def test_audit_js_lifecycle_follows_memory_view_precedent() -> None:
    app_js = _source("g3ku/web/frontend/org_graph_app.js")

    assert "function startAuditViewAutoRefresh()" in app_js
    assert "function stopAuditViewAutoRefresh()" in app_js
    assert 'if (S.view !== "audit") return;' in app_js
    assert 'if (view === "audit") startAuditViewAutoRefresh();' in app_js
    assert "function loadAuditView(" in app_js
    assert "function loadAuditSummary(" in app_js
    assert "function loadAuditEvents(" in app_js
    assert "function loadMoreAuditEvents()" in app_js
    assert "function renderAuditSummaryCards(" in app_js
    assert "function renderAuditEventList(" in app_js
    # 角标常驻轮询在 init 启动（不按视图门控） + 首访刷新
    assert "bindAuditBadge();" in app_js
    assert "void refreshAuditBadge();" in app_js


def test_audit_js_badge_semantics() -> None:
    app_js = _source("g3ku/web/frontend/org_graph_app.js")

    assert "function renderAuditNavBadge(count = 0)" in app_js
    assert '"99+"' in app_js
    assert "function auditUnreadFromResponse(total)" in app_js
    assert "function resolveAuditLastSeen(storedLastSeen, newestTimestamp)" in app_js
    assert "function markAuditRead()" in app_js
    assert "function auditBadgeStateFromStorage()" in app_js
    # 未读数契约：since + limit=1 用 total 计未读
    assert "getAuditEvents({ limit: 1, since: lastSeen })" in app_js
    # 首访初始化：取最新事件时间戳锚定，历史不点亮
    assert "getAuditEvents({ limit: 1 })" in app_js
    assert "writeSessionJson(AUDIT_LAST_SEEN_KEY" in app_js


def test_api_client_exposes_audit_methods_and_friendly_codes() -> None:
    api_client_js = _source("g3ku/web/frontend/api_client.js")

    assert "static async getAuditEvents(" in api_client_js
    assert "static async getAuditSummary()" in api_client_js
    assert '"/api/audit/events"' in api_client_js
    assert '"/api/audit/summary"' in api_client_js
    assert "audit_events_read_failed" in api_client_js
    assert "audit_summary_read_failed" in api_client_js
    assert "审计事件暂时不可读取，请稍后刷新。" in api_client_js
    assert "审计概览暂时不可读取，请稍后刷新。" in api_client_js
    # 角标轮询(requestKey 含 since)与视图分页互不取消
    assert "audit:events:" in api_client_js


def test_audit_endpoint_contract() -> None:
    admin_rest_py = _source("main/api/admin_rest.py")

    assert "@router.get('/audit/events')" in admin_rest_py
    events_fragment = _admin_route_fragment(admin_rest_py, "@router.get('/audit/events')")
    assert "Query(50, ge=1, le=200)" in events_fragment
    assert "Query(0, ge=0)" in events_fragment
    assert "audit_events.list_audit_events(" in events_fragment
    assert "audit_events_read_failed" in events_fragment
    assert "'has_more'" in events_fragment

    assert "@router.get('/audit/summary')" in admin_rest_py
    summary_fragment = _admin_route_fragment(admin_rest_py, "@router.get('/audit/summary')")
    assert "audit_events.audit_summary()" in summary_fragment
    assert "audit_summary_read_failed" in summary_fragment


def test_audit_css_selectors() -> None:
    css = _source("g3ku/web/frontend/org_graph.css")

    # 导航内角标（首个出现在 .nav-item 里的徽标）+ 隐藏规则
    assert ".nav-item .nav-badge" in css
    assert ".nav-item .nav-badge[hidden]" in css
    # 健康卡网格/状态/事件流筛选
    assert ".audit-summary-grid" in css
    assert ".audit-summary-card" in css
    assert ".audit-status.is-ok" in css
    assert ".audit-status.is-error" in css
    assert ".audit-level-chip.active" in css
    assert ".audit-event-card.is-error" in css
    assert ".audit-event-card.is-warning" in css
