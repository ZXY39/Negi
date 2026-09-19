"""日志审计前端契约测试：导航按钮/角标结构、原始日志单面板与分页、JS 职责、api_client 与端点钉约。"""

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
    # 只有原始日志单面板 + 刷新 + 分页（每页 100 条）
    assert 'id="audit-event-list"' in section
    assert 'id="audit-refresh-btn"' in section
    assert 'id="audit-page-prev"' in section
    assert 'id="audit-page-next"' in section
    assert 'id="audit-event-info"' in section
    # 异常栏已移除：只留原始日志
    assert 'id="audit-exception-panel"' not in section
    assert 'id="audit-exception-list"' not in section
    assert "原始日志" not in section
    assert "记忆问题在此只读" in section
    # 面板副标题去掉，把高度让给日志列表
    assert "最新在前" not in section
    assert "每页 100 条" not in section


def test_view_audit_has_no_summary_or_level_filters() -> None:
    html = _source("g3ku/web/frontend/org_graph.html")
    section = _fragment(html, 'id="view-audit"', 'id="view-task-details"')
    # 彻底简约：去掉 24h 健康卡、级别筛选与顶部异常栏，也没有记忆失败面板（操作面在记忆板块）
    assert "audit-summary-grid" not in section
    assert "audit-level-filters" not in section
    assert "audit-exception" not in section
    assert "memory-failed-list" not in section
    assert "memory-failed-panel" not in section


def test_audit_js_constants_state_and_ui_cache() -> None:
    app_js = _source("g3ku/web/frontend/org_graph_app.js")

    assert 'AUDIT_LAST_SEEN_KEY = "g3ku.audit.last-seen.v1"' in app_js
    assert "AUDIT_VIEW_POLL_MS = 15000" in app_js
    assert "AUDIT_BADGE_POLL_MS = 30000" in app_js
    # 原始日志每页 100 条（后端 limit 上限 200）
    assert "AUDIT_PAGE_SIZE = 100" in app_js
    assert "auditLoadedOnce: false" in app_js
    assert "auditBadgePollIntervalId: null" in app_js
    assert "auditPage: 1" in app_js
    assert 'viewAudit: document.getElementById("view-audit")' in app_js
    assert 'auditNavBadge: document.getElementById("audit-nav-badge")' in app_js
    assert 'auditPagePrev: document.getElementById("audit-page-prev")' in app_js
    assert 'auditPageNext: document.getElementById("audit-page-next")' in app_js
    assert "audit: U.viewAudit" in app_js


def test_audit_js_lifecycle_follows_memory_view_precedent() -> None:
    app_js = _source("g3ku/web/frontend/org_graph_app.js")

    assert "function startAuditViewAutoRefresh()" in app_js
    assert "function stopAuditViewAutoRefresh()" in app_js
    assert 'if (S.view !== "audit") return;' in app_js
    assert 'if (view === "audit") startAuditViewAutoRefresh();' in app_js
    assert "function loadAuditView(" in app_js
    assert "function loadAuditEvents(" in app_js
    assert "function goToAuditPage(" in app_js
    assert "function renderAuditPager(" in app_js
    assert "function auditPageSummary(" in app_js
    assert "function auditSubsystemLabel(subsystem)" in app_js
    assert "function renderAuditEventList(" in app_js
    # 角标常驻轮询在 init 启动（不按视图门控） + 首访刷新
    assert "bindAuditBadge();" in app_js
    assert "void refreshAuditBadge();" in app_js
    # 异常栏整体移除：加载/渲染函数不再存在，加载更多改为上一页/下一页
    assert "loadAuditExceptions" not in app_js
    assert "renderAuditExceptionList" not in app_js
    assert "renderAuditExceptionRow" not in app_js
    assert "loadMoreAuditEvents" not in app_js
    # 彻底简约：24h 汇总与级别筛选全部移除
    assert "auditSummaryGeneratedAt" not in app_js
    assert "auditLevelFilter" not in app_js
    assert "renderAuditSummaryCards" not in app_js


def test_audit_list_scrolls_and_preserves_offset_on_quiet_poll() -> None:
    app_js = _source("g3ku/web/frontend/org_graph_app.js")
    css = _source("g3ku/web/frontend/org_graph.css")

    # 列表自身滚动（滚轮可达全部行），分页常驻：静默轮询保留滚动位置，换页回到顶部
    assert "function renderAuditEventList(items = [], { preserveScroll = false } = {})" in app_js
    assert "preserveScroll: quiet" in app_js
    assert "U.auditEventList.scrollTop = previousScrollTop;" in app_js
    list_rule = _fragment(css, ".audit-event-list {", "}")
    assert "overflow-y: auto" in list_rule
    assert "overscroll-behavior: contain" in list_rule
    assert "flex: 1 1 auto" in list_rule
    panel_rule = _fragment(css, ".audit-feed-panel {", "}")
    assert "min-height: 0" in panel_rule
    assert ".audit-feed-panel .panel-header" not in css
    assert ".audit-feed-panel .memory-footer" in css


def test_audit_js_renders_system_local_time() -> None:
    app_js = _source("g3ku/web/frontend/org_graph_app.js")

    # 日志时间按系统本地时间渲染为 YYYY-MM-DD HH:mm:ss（ISO+偏移串不再直接展示）
    assert "function formatAuditTimestamp(value)" in app_js
    assert "formatAuditTimestamp(item.timestamp)" in app_js
    assert "function auditSubsystemLabel(subsystem)" in app_js


def test_audit_js_subclass_labels_cover_memory() -> None:
    app_js = _source("g3ku/web/frontend/org_graph_app.js")

    # 来源标签：模型调用/任务执行/Web 接口/记忆处理（日志行仍按 subsystem 渲染）
    for label in ("模型调用", "任务执行", "Web 接口", "记忆处理"):
        assert label in app_js
    # 异常栏的 level=error 专用请求已移除
    assert 'level: "error"' not in app_js


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
    # 原始日志单行样式 + 可读性增强（隔行底色/悬停/详情独占一行）
    assert ".audit-log-line" in css
    assert ".audit-log-line.is-error" in css
    assert ".audit-log-line.is-warning" in css
    assert ".audit-log-line:nth-child(even)" in css
    assert ".audit-log-line .audit-event-expand" in css
    assert "font-variant-numeric: tabular-nums" in css
    # 异常板块样式随板块一并清除
    assert ".audit-exception-panel" not in css
    assert ".audit-exception-row" not in css
    assert ".audit-exception-source" not in css
    # 旧健康卡/筛选样式已清除
    assert ".audit-summary-grid" not in css
    assert ".audit-level-chip" not in css
