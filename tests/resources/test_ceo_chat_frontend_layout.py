from __future__ import annotations

import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


def test_ceo_chat_feed_uses_compact_left_padding_without_avatar_gap() -> None:
    css = (REPO_ROOT / "g3ku/web/frontend/org_graph.css").read_text(encoding="utf-8")

    assert re.search(
        r"\.chat-feed\s*\{[^}]*padding:\s*clamp\(24px,\s*3vw,\s*36px\)\s+clamp\(28px,\s*6vw,\s*80px\)\s+calc\(var\(--space-6\)\s*\+\s*156px\)\s+clamp\(16px,\s*2\.4vw,\s*32px\);",
        css,
        flags=re.MULTILINE,
    )


def test_ceo_approval_viewport_is_centered_above_chat_input() -> None:
    css = (REPO_ROOT / "g3ku/web/frontend/org_graph.css").read_text(encoding="utf-8")

    assert re.search(
        r"\.ceo-approval-viewport\s*\{[^}]*position:\s*absolute;[^}]*left:\s*0;[^}]*right:\s*0;[^}]*bottom:\s*calc\(100%\s*\+\s*12px\);[^}]*justify-content:\s*center;",
        css,
        flags=re.MULTILINE,
    )


def test_v2_approval_viewport_stays_a_positioning_container() -> None:
    """v2 里审批浮层只许有一张面：容器画底会在卡片两侧拉出一块比弹窗更宽的空白。"""
    css = (REPO_ROOT / "g3ku/web/frontend/org_graph_redesign.css").read_text(encoding="utf-8")

    def block(selector: str) -> str:
        assert selector in css, f"缺少规则块：{selector}"
        start = css.index(selector)
        return css[start:css.index("}", start)]

    viewport = block("[data-ui-version=\"v2\"] .ceo-approval-viewport {")
    assert "background: transparent;" in viewport
    assert "--ui-bg-elevated" not in viewport

    # 重试提示条自己是可见的面，不能被同一条"容器不画底"顺手清空。
    assert "background: var(--ui-bg-elevated);" in block("[data-ui-version=\"v2\"] .model-retry-toast {")


def test_app_sidebars_use_compact_160px_width_before_mobile_stack() -> None:
    css_files = [
        REPO_ROOT / "g3ku/web/frontend/org_graph.css",
        REPO_ROOT / "g3ku/web/frontend/search.css",
    ]

    for css_path in css_files:
        css = css_path.read_text(encoding="utf-8")

        assert re.search(
            r"\.sidebar\s*\{[^}]*width:\s*160px;",
            css,
            flags=re.MULTILINE,
        ), css_path.as_posix()
        assert re.search(
            r"@media\s*\(max-width:\s*768px\)\s*\{[^{}]*\.sidebar\s*\{[^}]*width:\s*160px;",
            css,
            flags=re.MULTILINE | re.DOTALL,
        ), css_path.as_posix()


def test_navigation_bar_always_shows_labels_without_compact_mode() -> None:
    """顶部那条栏没有"收起名称"这档：品牌名与每个导航项的文字恒显示，也没有那颗按钮。"""
    html = (REPO_ROOT / "g3ku/web/frontend/org_graph.html").read_text(encoding="utf-8")
    app_js = (REPO_ROOT / "g3ku/web/frontend/org_graph_app.js").read_text(encoding="utf-8")
    v2_css = (REPO_ROOT / "g3ku/web/frontend/org_graph_redesign.css").read_text(encoding="utf-8")

    assert 'id="sidebar-toggle"' not in html
    for needle in (
        "sidebarToggle",
        "uiSidebarCollapsed",
        "SIDEBAR_COLLAPSED_KEY",
        "g3ku.ui.sidebar.collapsed",
        "toggleSidebar",
        "applySidebarState",
        "readSidebarPreference",
    ):
        assert needle not in app_js, needle
    assert ".sidebar.is-collapsed" not in v2_css
