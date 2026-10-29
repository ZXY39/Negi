"""设置里的缩放/字号档位：CSS 必须消费 JS 写的落点变量，缩放两端钉在 50%–250%。"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _source(path: str) -> str:
    return (REPO_ROOT / path).read_text(encoding="utf-8")


def test_knob_and_track_read_the_fraction_the_app_writes() -> None:
    css = _source("g3ku/web/frontend/org_graph.css")
    app_js = _source("g3ku/web/frontend/org_graph_app.js")

    assert 'setProperty?.("--scale-range-frac"' in app_js
    # 变量名对不上时缺省 0.5 永远生效：指针和轨道填充停在正中，拖动只有数字在变。
    assert "var(--scale-range-frac, 0.5)" in css
    assert "--zoom-range-frac" not in css


def test_zoom_row_ends_and_number_cell_bounds() -> None:
    app_js = _source("g3ku/web/frontend/org_graph_app.js")
    html = _source("g3ku/web/frontend/org_graph.html")

    assert "const ZOOM_LOW_END = 0.5;" in app_js
    assert "const ZOOM_HIGH_END = 2.5;" in app_js
    zoom_cell = html[html.index('id="project-settings-zoom-value"') :]
    zoom_cell = zoom_cell[: zoom_cell.index(">")]
    assert 'min="50"' in zoom_cell
    assert 'max="250"' in zoom_cell
