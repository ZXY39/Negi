from __future__ import annotations

from pathlib import Path
import re


REPO_ROOT = Path(__file__).resolve().parents[2]


def test_ceo_compression_ui_uses_shared_primary_pause_button() -> None:
    html = (REPO_ROOT / "g3ku/web/frontend/org_graph.html").read_text(encoding="utf-8")
    app_js = (REPO_ROOT / "g3ku/web/frontend/org_graph_app.js").read_text(encoding="utf-8")

    assert "ceo-compression-actions" not in html
    assert "ceo-compression-pause-btn" not in html
    assert "ceoCompressionActions" not in app_js
    assert "ceoCompressionPause" not in app_js


def test_compression_progress_renders_as_feed_divider_not_composer_toast() -> None:
    html = (REPO_ROOT / "g3ku/web/frontend/org_graph.html").read_text(encoding="utf-8")
    app_js = (REPO_ROOT / "g3ku/web/frontend/org_graph_app.js").read_text(encoding="utf-8")
    css = (REPO_ROOT / "g3ku/web/frontend/org_graph.css").read_text(encoding="utf-8")

    assert "ceo-compression-toast" not in html
    assert "ceo-compression-toast" not in css

    match = re.search(r"\.ceo-compression-divider-inner\s*\{(?P<body>.*?)\n\}", css, re.S)
    assert match is not None
    body = match.group("body")
    assert "display: flex;" in body

    # 两条细线夹住文案，进行中/已暂停各带一个图标位。
    assert ".ceo-compression-divider-inner::before" in css
    assert ".ceo-compression-divider-inner::after" in css
    assert "#39c5bb" in css
    assert "ceo-compression-divider" in app_js
    assert '"上下文压缩中"' in app_js
    assert '"会话已压缩"' in app_js
    assert '"压缩已暂停"' in app_js
    # 进行中的那条要留下可点的暂停入口。
    assert "data-ceo-compress-pause" in app_js


def test_ceo_context_load_notice_uses_single_right_aligned_column_and_kind_icons() -> None:
    css = (REPO_ROOT / "g3ku/web/frontend/org_graph.css").read_text(encoding="utf-8")
    app_js = (REPO_ROOT / "g3ku/web/frontend/org_graph_app.js").read_text(encoding="utf-8")

    match = re.search(r"\.ceo-context-load-notice\s*\{(?P<body>.*?)\n\}", css, re.S)
    assert match is not None
    body = match.group("body")

    assert "display: flex;" in body
    assert "flex-direction: column;" in body
    assert "align-items: flex-end;" in body
    assert "right: 0;" in body
    assert "grid-template-columns" not in body

    assert '"wrench"' in app_js
    assert '"sparkles"' in app_js
