"""日志审计前端契约测试：导航按钮/角标结构、异常板块 + 原始日志区块、JS 职责、api_client 与端点钉约。"""

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
    # 异常板块（有异常才显示）+ 原始日志 + 刷新 + 加载更多
    assert 'id="audit-exception-panel"' in section
    assert 'id="audit-exception-list"' in section
    assert 'id="audit-event-list"' in section
    assert 'id="audit-refresh-btn"' in section
    assert 'id="audit-event-more-btn"' in section
    assert 'id="audit-event-info"' in section
    # 板块标题与简化契约：异常信息来源 + 原始日志
    assert "异常" in section
    assert "原始日志" in section
    assert "记忆问题在此只读" in section


def test_view_audit_has_no_summary_or_level_filters() -> None:
    html = _source("g3ku/web/frontend/org_graph.html")
    section = _fragment(html, 'id="view-audit"', 'id="view-task-details"')
    # 彻底简约：去掉 24h 健康卡与级别筛选，也没有记忆失败面板（操作面在记忆板块）
    assert "audit-summary-grid" not in section
    assert "audit-level-filters" not in section
    assert "memory-failed-list" not in section
    assert "memory-failed-panel" not in section


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
    assert 'auditExceptionPanel: document.getElementById("audit-exception-panel")' in app_js
    assert "audit: U.viewAudit" in app_js


def test_audit_js_lifecycle_follows_memory_view_precedent() -> None:
    app_js = _source("g3ku/web/frontend/org_graph_app.js")

    assert "function startAuditViewAutoRefresh()" in app_js
    assert "function stopAuditViewAutoRefresh()" in app_js
    assert 'if (S.view !== "audit") return;' in app_js
    assert 'if (view === "audit") startAuditViewAutoRefresh();' in app_js
    assert "function loadAuditView(" in app_js
    assert "function loadAuditExceptions(" in app_js
    assert "function loadAuditEvents(" in app_js
    assert "function loadMoreAuditEvents()" in app_js
    assert "function renderAuditExceptionList(" in app_js
    assert "function renderAuditExceptionRow(" in app_js
    assert "function auditSubsystemLabel(subsystem)" in app_js
    assert "function renderAuditEventList(" in app_js
    # 角标常驻轮询在 init 启动（不按视图门控） + 首访刷新
    assert "bindAuditBadge();" in app_js
    assert "void refreshAuditBadge();" in app_js
    # 彻底简约：24h 汇总与级别筛选全部移除
    assert "auditSummaryGeneratedAt" not in app_js
    assert "auditLevelFilter" not in app_js
    assert "renderAuditSummaryCards" not in app_js


def test_audit_js_subclass_labels_cover_memory() -> None:
    app_js = _source("g3ku/web/frontend/org_graph_app.js")

    # 来源标签：模型调用/任务执行/Web 接口/记忆处理（记忆错误只读进入审计池）
    for label in ("模型调用", "任务执行", "Web 接口", "记忆处理"):
        assert label in app_js
    assert 'getAuditEvents({ limit: 20, level: "error" })' in app_js
    assert "U.auditExceptionPanel.hidden = items.length === 0" in app_js


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


def test_api_client_exposes_audit_events_only() -> None:
    api_client_js = _source("g3ku/web/frontend/api_client.js")

    assert "static async getAuditEvents(" in api_client_js
    assert '"/api/audit/events"' in api_client_js
    assert "audit_events_read_failed" in api_client_js
    assert "审计事件暂时不可读取，请稍后刷新。" in api_client_js
    # 角标轮询(requestKey 含 since)与视图分页互不取消
    assert "audit:events:" in api_client_js
    # summary 端点随 24h 汇总一并移除
    assert "getAuditSummary" not in api_client_js
    assert "/api/audit/summary" not in api_client_js
    assert "audit_summary_read_failed" not in api_client_js


def test_audit_endpoint_contract() -> None:
    admin_rest_py = _source("main/api/admin_rest.py")

    assert "@router.get('/audit/events')" in admin_rest_py
    events_fragment = _admin_route_fragment(admin_rest_py, "@router.get('/audit/events')")
    assert "Query(50, ge=1, le=200)" in events_fragment
    assert "Query(0, ge=0)" in events_fragment
    assert "audit_events.list_audit_events(" in events_fragment
    assert "audit_events_read_failed" in events_fragment
    assert "'has_more'" in events_fragment
    # summary 路由已移除
    assert "@router.get('/audit/summary')" not in admin_rest_py


def test_audit_css_selectors() -> None:
    css = _source("g3ku/web/frontend/org_graph.css")

    # 导航内角标（首个出现在 .nav-item 里的徽标）+ 隐藏规则
    assert ".nav-item .nav-badge" in css
    assert ".nav-item .nav-badge[hidden]" in css
    # 异常板块 + 原始日志单行样式
    assert ".audit-exception-panel" in css
    assert ".audit-exception-row" in css
    assert ".audit-exception-source" in css
    assert ".audit-log-line" in css
    assert ".audit-log-line.is-error" in css
    assert ".audit-log-line.is-warning" in css
    # 旧健康卡/筛选样式已清除
    assert ".audit-summary-grid" not in css
    assert ".audit-level-chip" not in css
