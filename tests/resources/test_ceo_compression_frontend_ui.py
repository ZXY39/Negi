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


def test_ceo_compression_toast_stays_in_flow_and_precedes_follow_up_queue() -> None:
    html = (REPO_ROOT / "g3ku/web/frontend/org_graph.html").read_text(encoding="utf-8")
    css = (REPO_ROOT / "g3ku/web/frontend/org_graph.css").read_text(encoding="utf-8")
    assert html.index('id="ceo-compression-toast"') < html.index('id="ceo-follow-up-queue"')

    match = re.search(r"\.ceo-compression-toast\s*\{(?P<body>.*?)\n\}", css, re.S)
    assert match is not None
    body = match.group("body")

    assert "position: absolute;" not in body
    assert "margin-inline-start: calc(var(--ceo-input-leading-width) + var(--ceo-input-row-gap));" in body
    assert "align-self: flex-start;" in body


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
